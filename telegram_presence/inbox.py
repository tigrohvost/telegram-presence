"""Realtime group-mention inbox: addressed detection + bounded jsonl spool.

Fed by TelegramUserBridge group events; consumed by the engage organ.
No LLM here; group text is untrusted evidence — sanitized snippets only.
Detection logic mirrors scripts/rain_telethon_mentions.py (kept as the
backfill/diagnostic tool).
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

SPOOL_REL = Path("state") / "telegram_group_inbox.jsonl"
RECEIPT_REL = Path("state") / "telegram_addressed_mentions_monitor.json"
MAX_SPOOL_BYTES = 1_000_000
MAX_SNIPPET_CHARS = 500
MAX_SEEN_IDS = 2048
MAX_OWN_IDS = 200

DEFAULT_NAMES = ("rain", "rain_ouroboros", "рейн", "рэйн", "ouroboros", "ороборос")
INFLECTED_ROOTS = ("рейн", "рэйн", "ороборос")
_CYR = "а-яёА-ЯЁ"


def _compile_term_patterns(term: str) -> list[tuple[str, re.Pattern[str]]]:
    escaped = re.escape(term.strip())
    if not escaped:
        return []
    flags = re.IGNORECASE
    return [
        (f"@{term}", re.compile(rf"(?<!\w)@{escaped}(?!\w)", flags)),
        (term, re.compile(rf"(?<!\w){escaped}(?!\w)", flags)),
    ]


def _inflected_label(text: str, root: str) -> str | None:
    escaped = re.escape(root)
    pattern = re.compile(rf"(?<![\w{_CYR}]){escaped}[{_CYR}]{{0,3}}(?![{_CYR}])", re.IGNORECASE)
    return root if pattern.search(text) else None


def matched_terms(text: str | None, names: tuple[str, ...] | None = None) -> list[str]:
    if names is None:
        from .hooks import name_terms
        names = name_terms()
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for raw_name in names:
        for label, pattern in _compile_term_patterns(raw_name.strip()):
            key = label.casefold()
            if key not in seen and pattern.search(text):
                found.append(label)
                seen.add(key)
    for root in INFLECTED_ROOTS:
        label = _inflected_label(text or "", root)
        if label is not None and label.casefold() not in seen:
            found.append(label)
            seen.add(label.casefold())
    return found


def sanitize_snippet(text: str | None, max_chars: int = MAX_SNIPPET_CHARS) -> str:
    if not text:
        return ""
    cleaned = "".join(ch if (ch.isprintable() or ch in "\r\n\t") else " " for ch in str(text))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_chars:
        return cleaned[: max_chars - 1].rstrip() + "…"
    return cleaned


def clean_handle(value: Optional[str]) -> Optional[str]:
    """Normalize a Telegram @username: strip @/space, drop empties."""
    if not value:
        return None
    handle = str(value).strip().lstrip("@").strip()
    return handle or None


def allowed_chats(st: Optional[dict] = None) -> list[str]:
    """All chats the telegram group stack serves: env/state primary mentions
    chat first, then the optional state list ``telegram_engage_chats``.
    Deduped, order-stable. THE single source of truth — the engage loop, the
    live-event allowlist, the backfill reader and the reader fallback must
    all resolve chats here (live incident 2026-07-09: three divergent
    resolutions let the engage loop serve a retired chat).

    Pass ``st`` when the caller already holds a loaded state dict; otherwise
    the state is loaded here."""
    chats: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        key = text.lstrip("@").lower()
        if key and key not in seen:
            seen.add(key)
            chats.append(text)

    _add(os.environ.get("TELEGRAM_MENTIONS_CHAT", ""))
    try:
        if st is None:
            from .hooks import load_state
            st = load_state() or {}
        _add(st.get("telegram_mentions_chat"))
        extra = st.get("telegram_engage_chats")
        if isinstance(extra, list):
            for item in extra[:8]:
                _add(item)
    except Exception:
        pass
    # No default chat: an unconfigured inbox must stay silent. The old
    # @abstractdl_chat fallback kept re-attaching readers to a retired chat
    # (live incident 2026-07-09: cross-chat ghost replies).
    return chats


def allowed_chat() -> str:
    chats = allowed_chats()
    return chats[0] if chats else ""


def chat_matches(chat_username: Optional[str], chat_id: Any, allowed: str) -> bool:
    want = str(allowed or "").strip().lstrip("@").lower()
    if not want:
        return False
    if chat_username and str(chat_username).strip().lstrip("@").lower() == want:
        return True
    return str(chat_id) == want or str(chat_id).lstrip("-") == want.lstrip("-")


def chat_matches_any(chat_username: Optional[str], chat_id: Any,
                     allowed: list[str]) -> Optional[str]:
    """Canonical allowed-chat value this event belongs to, or None."""
    for candidate in allowed or []:
        if chat_matches(chat_username, chat_id, candidate):
            return candidate
    return None


class GroupInbox:
    """Bounded jsonl spool of group candidates + legacy receipt refresh."""

    def __init__(self, drive_root: Any):
        self._root = Path(drive_root)
        self._spool = self._root / SPOOL_REL
        self._receipt = self._root / RECEIPT_REL
        self._lock = threading.Lock()
        self._seen: set[tuple[str, int]] = set()
        self._seen_order: deque[tuple[str, int]] = deque(maxlen=MAX_SEEN_IDS)
        self._own_ids: deque[int] = deque(maxlen=MAX_OWN_IDS)

    def remember_own_message(self, message_id: int) -> None:
        with self._lock:
            self._own_ids.append(int(message_id))

    def add_message(self, *, chat: str, message_id: int, sender_id: Optional[int],
                    text: str, reply_to_msg_id: Optional[int], self_id: Optional[int],
                    sender_username: Optional[str] = None, sender_name: Optional[str] = None,
                    now: Optional[float] = None) -> bool:
        """Spool one group message. Returns True when a row was written."""
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return False
        if self_id is not None and sender_id == self_id:
            self.remember_own_message(mid)
            return False
        key = (str(chat), mid)
        with self._lock:
            if key in self._seen:
                return False
            if len(self._seen_order) == self._seen_order.maxlen and self._seen_order:
                self._seen.discard(self._seen_order[0])
            self._seen_order.append(key)
            self._seen.add(key)
            own_ids = set(self._own_ids)

        terms = matched_terms(text)
        reply_to_me = reply_to_msg_id is not None and int(reply_to_msg_id) in own_ids
        addressed = bool(terms) or reply_to_me
        row = {
            "ts": time.time() if now is None else float(now),
            "chat": str(chat),
            "message_id": mid,
            "sender_id": sender_id,
            "sender_username": clean_handle(sender_username),
            "sender_name": (sanitize_snippet(sender_name, 64) or None),
            "reply_to_msg_id": (int(reply_to_msg_id) if reply_to_msg_id is not None else None),
            "addressed": addressed,
            "matched_terms": terms + (["reply_to_me"] if reply_to_me else []),
            "snippet": sanitize_snippet(text),
            "untrusted_external_text": True,
        }
        try:
            self._append(row)
        except Exception:
            log.warning("telegram_group_inbox: spool append failed", exc_info=True)
            return False
        if addressed:
            self._refresh_receipt(str(chat))
        try:
            from .roster import observe_message
            observe_message(self._root, chat, sender_id,
                            row["sender_username"], row["sender_name"],
                            now=row["ts"])
        except Exception:
            log.debug("telegram_group_inbox: roster observe failed", exc_info=True)
        return True

    def pending(self, after_ts: float = 0.0, limit: int = 50) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            with self._spool.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(row, dict) and float(row.get("ts") or 0) > after_ts:
                        rows.append(row)
        except FileNotFoundError:
            return []
        except Exception:
            log.warning("telegram_group_inbox: spool read failed", exc_info=True)
            return []
        return rows[-limit:]

    def has_unconsumed_addressed(self, after_ts: float = 0.0) -> bool:
        return any(r.get("addressed") for r in self.pending(after_ts=after_ts))

    # -- internals --

    def _append(self, row: dict[str, Any]) -> None:
        self._spool.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self._spool.stat().st_size > MAX_SPOOL_BYTES:
                keep = self._spool.read_text(encoding="utf-8").splitlines()[-500:]
                self._spool.write_text("\n".join(keep) + "\n", encoding="utf-8")
        except FileNotFoundError:
            pass
        with self._spool.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _refresh_receipt(self, chat: str) -> None:
        """Keep the legacy monitor receipt alive for dashboards/contracts."""
        try:
            addressed = [r["message_id"] for r in self.pending(after_ts=0.0, limit=200)
                         if r.get("addressed")]
            payload = {
                "status": "new_addressed_signal",
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "chat": chat,
                "source": "telegram_presence.inbox (realtime)",
                "addressed_ids": addressed[-50:],
                "handled_message_ids": addressed[-50:],
                "new_addressed_signal": True,
                "no_new_addressed_signal": False,
                "blocker": None,
            }
            self._receipt.parent.mkdir(parents=True, exist_ok=True)
            self._receipt.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
        except Exception:
            log.warning("telegram_group_inbox: receipt refresh failed", exc_info=True)


_INBOX: Optional[GroupInbox] = None
_INBOX_LOCK = threading.Lock()


def get_inbox(drive_root: Any = None) -> Optional[GroupInbox]:
    global _INBOX
    with _INBOX_LOCK:
        if _INBOX is None and drive_root is not None:
            _INBOX = GroupInbox(drive_root)
        return _INBOX
