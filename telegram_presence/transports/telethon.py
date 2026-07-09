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
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


class TelethonTransport:
    """Bridge an injected Telethon client to the engage cycle."""

    def __init__(self, *, client: Any, inbox: Any,
                 loop: Optional[asyncio.AbstractEventLoop] = None,
                 self_id: Optional[int] = None,
                 react_request: Optional[Callable] = None,
                 send_timeout: float = 30.0) -> None:
        self._client = client
        self._inbox = inbox
        self._loop = loop
        self._self_id = self_id
        self._react_request = react_request
        self._timeout = send_timeout

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
        try:
            loop = self._loop
            if loop is not None and loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                fut.result(timeout=self._timeout)
            elif loop is not None:
                loop.run_until_complete(coro)
            else:
                asyncio.run(coro)
            return True
        except Exception:
            log.warning("telethon transport send failed", exc_info=True)
            return False

    async def _reply(self, peer: Any, msg_id: int, text: str) -> None:
        entity = await self._client.get_entity(peer)
        reply_to = int(msg_id) if msg_id and int(msg_id) > 0 else None
        await self._client.send_message(entity, str(text)[:4096], reply_to=reply_to)

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
