"""Telethon (user-account) transport.

The Telethon client is **injected** — this module never imports telethon, so
the package stays stdlib-only and the adapter is testable with a fake client.
Wire it like:

    from telethon import TelegramClient, events
    from telegram_presence.inbox import GroupInbox
    from telegram_presence.transports.telethon import TelethonTransport

    client = TelegramClient("session", api_id, api_hash)
    transport = TelethonTransport(client=client, inbox=GroupInbox("data"),
                                  loop=client.loop, self_id=me.id)
    client.add_event_handler(transport.on_group_message,
                             events.NewMessage(func=lambda e: e.is_group))

Notes for user mode:
- A user account sees the whole group with no privacy-mode caveats, and its
  replies read as a person's (typing indicators, read receipts are yours to
  manage).
- Reactions need a raw ``SendReactionRequest``; pass a ``react_request``
  factory if you want reactions (kept optional so the fake-client path and
  reaction-less deployments stay simple).
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Callable, Optional

from telegram_presence.content import DEFAULT_MAX_TEXT_CHUNKS, semantic_chunks
from telegram_presence.delivery import MessageEnvelope, TransportReceipt

log = logging.getLogger(__name__)


class TelethonTransport:
    """Bridge an injected Telethon client to the engage cycle."""

    transport_name = "telethon"

    def __init__(self, *, client: Any, inbox: Any,
                 loop: Optional[asyncio.AbstractEventLoop] = None,
                 self_id: Optional[int] = None,
                 react_request: Optional[Callable] = None,
                 send_timeout: float = 30.0,
                 max_text_chunks: int = DEFAULT_MAX_TEXT_CHUNKS) -> None:
        if (isinstance(max_text_chunks, bool) or not isinstance(max_text_chunks, int)
                or max_text_chunks < 1):
            raise ValueError("max_text_chunks must be a positive integer")
        if (isinstance(send_timeout, bool) or not isinstance(send_timeout, (int, float))
                or not math.isfinite(float(send_timeout)) or send_timeout <= 0):
            raise ValueError("send_timeout must be positive finite seconds")
        self._client = client
        self._inbox = inbox
        self._loop = loop
        self._self_id = self_id
        self._react_request = react_request
        self._timeout = float(send_timeout)
        self._max_text_chunks = max_text_chunks

    # -- inbound: register as a NewMessage handler ------------------------
    async def on_group_message(self, event: Any) -> bool:
        """Spool one allowed-chat group message. Safe to register directly."""
        try:
            from telegram_presence.inbox import allowed_chats, chat_matches_any
            chat = getattr(event, "chat", None)
            if chat is None and hasattr(event, "get_chat"):
                chat = await event.get_chat()
            allowed = chat_matches_any(getattr(chat, "username", None),
                                       getattr(event, "chat_id", None),
                                       allowed_chats())
            if allowed is None:
                return False
            msg = getattr(event, "message", None)
            sender = await event.get_sender()
            first = getattr(sender, "first_name", "") or ""
            last = getattr(sender, "last_name", "") or ""
            display = (f"{first} {last}".strip()
                       or getattr(sender, "title", None) or "") or None
            return bool(self._inbox.add_message(
                chat=allowed,
                message_id=int(getattr(msg, "id", None) or 0),
                sender_id=getattr(sender, "id", None),
                sender_username=getattr(sender, "username", None),
                sender_name=display,
                text=(getattr(event, "raw_text", None)
                      or getattr(msg, "message", None) or ""),
                reply_to_msg_id=getattr(msg, "reply_to_msg_id", None),
                self_id=self._self_id,
            ))
        except Exception:
            log.warning("telethon transport ingest failed", exc_info=True)
            return False

    # -- outbound ----------------------------------------------------------
    def _run(self, coro) -> bool:
        """Run a coroutine from sync engage code, on the client's loop when
        it is already running (the normal Telethon case) or directly."""
        future = None
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                # Blocking the thread that owns a running event loop can time
                # out and then execute the queued send later, producing a
                # false failure followed by a ghost delivery. Fail truthfully;
                # sync engage/outbox work must run in a worker thread.
                close = getattr(coro, "close", None)
                if callable(close):
                    close()
                log.warning("telethon sync send called from a running event loop; "
                            "run it in a worker thread")
                return False
            loop = self._loop
            if loop is not None and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                future.result(timeout=self._timeout)
            elif loop is not None:
                loop.run_until_complete(coro)
            else:
                asyncio.run(coro)
            return True
        except Exception:
            if future is not None:
                future.cancel()
            else:
                close = getattr(coro, "close", None)
                if callable(close):
                    close()
            log.warning("telethon transport send failed", exc_info=True)
            return False

    async def _reply(self, peer: Any, msg_id: int, text: str) -> None:
        chunks = semantic_chunks(str(text), max_chunks=self._max_text_chunks) or [""]
        entity = await self._client.get_entity(peer)
        reply_to = int(msg_id) if msg_id and int(msg_id) > 0 else None
        for index, chunk in enumerate(chunks):
            await self._client.send_message(
                entity,
                chunk,
                reply_to=reply_to if index == 0 else None,
            )

    def do_reply(self, peer: Any, msg_id: int, text: str) -> bool:
        return self._run(self._reply(peer, msg_id, text))

    async def _react(self, peer: Any, msg_id: int, emoji: str) -> None:
        if self._react_request is None:
            raise RuntimeError("no react_request factory configured")
        entity = await self._client.get_entity(peer)
        await self._client(self._react_request(entity, int(msg_id), str(emoji)))

    def do_react(self, peer: Any, msg_id: int, emoji: str) -> bool:
        if self._react_request is None:
            log.info("telethon transport: reactions not configured; skipping")
            return False
        return self._run(self._react(peer, msg_id, emoji))

    def send_envelope(self, envelope: MessageEnvelope) -> TransportReceipt:
        """Send a durable envelope and return an explicit transport ACK."""
        if not isinstance(envelope, MessageEnvelope):
            raise ValueError("envelope must be a MessageEnvelope")
        if envelope.transport != "telethon":
            return TransportReceipt(
                success=False,
                transport="telethon",
                correlation_id=envelope.correlation_id,
                error=f"transport mismatch: {envelope.transport}",
            )
        if envelope.kind == "reaction":
            success = bool(envelope.reply_to_message_id) and self.do_react(
                envelope.peer, envelope.reply_to_message_id or 0, envelope.text,
            )
        elif envelope.kind in ("message", "notification", "reply"):
            success = self.do_reply(
                envelope.peer, envelope.reply_to_message_id or 0, envelope.text,
            )
        else:
            return TransportReceipt(
                success=False,
                transport="telethon",
                correlation_id=envelope.correlation_id,
                error="media envelopes require a host-provided Telethon sender",
            )
        return TransportReceipt(
            success=bool(success),
            transport="telethon",
            correlation_id=envelope.correlation_id,
            error=None if success else "Telethon send failed",
        )
