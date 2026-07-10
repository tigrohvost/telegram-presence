"""Bot API transport: stdlib-only adapter over https://api.telegram.org.

Notes for bot mode:
- The bot must have **privacy mode disabled** (BotFather → /setprivacy → Off)
  or be an admin of the group, otherwise it only receives commands and
  replies to itself — the inbox would miss the conversation.
- ``peer`` may be a numeric chat id or a public ``@groupname``.
- Reactions use ``setMessageReaction`` (Bot API 7.0+); Telegram restricts
  which emoji are allowed — the engage validator's allowlist is compatible.
"""
from __future__ import annotations

import json
import logging
import math
import urllib.request
from typing import Any, Callable, Optional

from telegram_presence.content import DEFAULT_MAX_TEXT_CHUNKS, semantic_chunks
from telegram_presence.delivery import MessageEnvelope, TransportReceipt

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"


def _default_http(url: str, data: Optional[bytes] = None, timeout: Optional[int] = None):
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout or 30)


class BotApiTransport:
    """Long-poll updates into a GroupInbox; reply/react via Bot API calls.

    ``http`` is injectable for tests and proxies: any callable with the
    signature ``(url, data=None, timeout=None) -> file-like`` works.
    """

    transport_name = "bot_api"

    def __init__(self, *, token: str, inbox: Any,
                 http: Callable = _default_http,
                 api_root: str = API_ROOT,
                 self_id: Optional[int] = None,
                 timeout: float = 30,
                 max_text_chunks: int = DEFAULT_MAX_TEXT_CHUNKS) -> None:
        if (isinstance(max_text_chunks, bool) or not isinstance(max_text_chunks, int)
                or max_text_chunks < 1):
            raise ValueError("max_text_chunks must be a positive integer")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout)) or timeout <= 0):
            raise ValueError("timeout must be positive finite seconds")
        self._token = token
        self._inbox = inbox
        self._http = http
        self._root = api_root.rstrip("/")
        self._self_id = self_id
        self._timeout = float(timeout)
        self._max_text_chunks = max_text_chunks
        self._offset = 0

    # -- plumbing --------------------------------------------------------
    def _call(self, method: str, payload: Optional[dict] = None) -> dict:
        url = f"{self._root}/bot{self._token}/{method}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            with self._http(url, data=data, timeout=self._timeout) as fh:
                body = json.loads(fh.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            # The request URL contains /bot<TOKEN>/; traceback text from urllib
            # can reproduce that URL and leak the credential into logs.
            log.warning("bot_api %s failed: %s", method, type(exc).__name__)
            return {}
        if not body.get("ok"):
            log.warning("bot_api %s not ok: %s", method, str(body)[:200])
            return {}
        return body

    # -- inbound ---------------------------------------------------------
    def poll_updates(self, limit: int = 50) -> int:
        """One getUpdates pass; spool allowed-chat group messages. Returns
        the number of rows written."""
        from telegram_presence.inbox import GroupInbox, allowed_chats, chat_matches_any
        body = self._call("getUpdates", {"offset": self._offset, "limit": limit,
                                         "timeout": 0,
                                         "allowed_updates": ["message"]})
        written = 0
        for upd in body.get("result") or []:
            try:
                next_offset = int(upd["update_id"]) + 1
            except (KeyError, TypeError, ValueError):
                continue
            raw_message = upd.get("message")
            if raw_message is None:
                self._offset = max(self._offset, next_offset)
                continue
            if not isinstance(raw_message, dict):
                log.warning("bot_api ignored malformed message update")
                self._offset = max(self._offset, next_offset)
                continue
            msg = raw_message
            chat = msg.get("chat")
            if not isinstance(chat, dict):
                log.warning("bot_api ignored malformed message update")
                self._offset = max(self._offset, next_offset)
                continue
            if chat.get("type") not in ("group", "supergroup"):
                self._offset = max(self._offset, next_offset)
                continue
            allowed = chat_matches_any(chat.get("username"), chat.get("id"),
                                       allowed_chats())
            if allowed is None:
                self._offset = max(self._offset, next_offset)
                continue
            try:
                message_id = msg["message_id"]
                if (isinstance(message_id, bool) or not isinstance(message_id, int)
                        or message_id < 1):
                    raise ValueError("invalid message_id")
                frm = msg.get("from") or {}
                if not isinstance(frm, dict):
                    raise ValueError("invalid sender")
                reply_message = msg.get("reply_to_message") or {}
                if not isinstance(reply_message, dict):
                    raise ValueError("invalid reply target")
                reply_to = reply_message.get("message_id")
                if (reply_to is not None
                        and (isinstance(reply_to, bool)
                             or not isinstance(reply_to, int) or reply_to < 1)):
                    raise ValueError("invalid reply target")
                text = msg.get("text")
                if text is None:
                    text = msg.get("caption")
                if text is None:
                    text = ""
                if not isinstance(text, str):
                    raise ValueError("invalid message text")
            except (KeyError, TypeError, ValueError):
                log.warning("bot_api ignored malformed message update")
                self._offset = max(self._offset, next_offset)
                continue
            names = [
                item for item in (frm.get("first_name"), frm.get("last_name"))
                if isinstance(item, str) and item
            ]
            values = {
                "chat": allowed,
                "message_id": message_id,
                "sender_id": frm.get("id"),
                "sender_username": frm.get("username"),
                "sender_name": " ".join(names) or None,
                "text": text,
                "reply_to_msg_id": reply_to,
                "self_id": self._self_id,
            }
            try:
                ingest = getattr(self._inbox, "ingest_message", None)
                inbox_type = type(self._inbox)
                add_override = getattr(inbox_type, "add_message", None)
                instance_values = getattr(self._inbox, "__dict__", {})
                ingest_override = (
                    getattr(inbox_type, "ingest_message", None)
                    is not GroupInbox.ingest_message
                )
                use_ingest = callable(ingest) and (
                    not isinstance(self._inbox, GroupInbox)
                    or ingest_override
                    or "ingest_message" in instance_values
                    or (add_override is GroupInbox.add_message
                        and "add_message" not in instance_values)
                )
                if use_ingest:
                    result = ingest(**values)
                    ok = bool(result.written)
                    safe_to_ack = bool(result.safe_to_ack)
                else:
                    ok = bool(self._inbox.add_message(**values))
                    # The legacy boolean inbox API cannot distinguish a
                    # duplicate/intentional ignore from a write failure. Keep
                    # its historical cursor semantics; durable fail-closed
                    # ordering requires GroupInbox.ingest_message().
                    safe_to_ack = True
            except Exception as exc:
                log.warning("bot_api inbox persistence failed: %s", type(exc).__name__)
                break
            if not safe_to_ack:
                log.warning("bot_api inbox persistence failed; retaining update offset")
                break
            self._offset = max(self._offset, next_offset)
            written += 1 if ok else 0
        return written

    # -- outbound (the engage cycle's do_reply / do_react) ---------------
    def do_reply(self, peer: Any, msg_id: int, text: str) -> bool:
        try:
            chunks = semantic_chunks(str(text), max_chunks=self._max_text_chunks) or [""]
        except ValueError:
            log.warning("bot_api reply exceeds safe chunk bound", exc_info=True)
            return False
        for index, chunk in enumerate(chunks):
            payload: dict = {"chat_id": peer, "text": chunk}
            if index == 0 and msg_id and int(msg_id) > 0:
                payload["reply_parameters"] = {
                    "message_id": int(msg_id),
                    "allow_sending_without_reply": False,
                }
            if not self._call("sendMessage", payload):
                return False
        return True

    def do_react(self, peer: Any, msg_id: int, emoji: str) -> bool:
        payload = {"chat_id": peer, "message_id": int(msg_id),
                   "reaction": [{"type": "emoji", "emoji": str(emoji)}]}
        return bool(self._call("setMessageReaction", payload))

    def send_envelope(self, envelope: MessageEnvelope) -> TransportReceipt:
        """Send a durable envelope and return an explicit transport ACK."""
        if not isinstance(envelope, MessageEnvelope):
            raise ValueError("envelope must be a MessageEnvelope")
        if envelope.transport != "bot_api":
            return TransportReceipt(
                success=False,
                transport="bot_api",
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
                transport="bot_api",
                correlation_id=envelope.correlation_id,
                error="media envelopes require a host-provided multipart sender",
            )
        return TransportReceipt(
            success=bool(success),
            transport="bot_api",
            correlation_id=envelope.correlation_id,
            error=None if success else "Bot API send failed",
        )
