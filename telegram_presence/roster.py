"""Per-chat participant memory for group engagement.

Auto-facts (handle, display name, first/last seen, message count) are
recorded on every spooled group message; short free-text notes are written
by the engage decider via the "remember" action. Both feed back into the
decider prompt so Rain recognises people across cycles and days instead of
only within the current spool window.

Storage: state/telegram_roster.json — {chat: {sender_key: entry}}.
Bounded per chat; least-recently-seen entries are evicted. Notes are
treated as UNTRUSTED text derived from public chat: length-capped and
newline-stripped, never executed or echoed to the owner.
"""
from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

ROSTER_REL = pathlib.Path("state") / "telegram_roster.json"
MAX_PARTICIPANTS_PER_CHAT = 300
NOTE_MAX_CHARS = 200
NOTES_MAX_PER_PARTICIPANT = 3
FLUSH_MIN_INTERVAL_SEC = 20.0

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {}
_CACHE_PATH: Optional[pathlib.Path] = None
_LAST_FLUSH = 0.0
_DIRTY = False


def _roster_path(data_root: Any) -> pathlib.Path:
    return pathlib.Path(data_root) / ROSTER_REL


def _load(data_root: Any) -> dict:
    """Load roster into the process-wide cache (single supervisor process)."""
    global _CACHE, _CACHE_PATH
    path = _roster_path(data_root)
    if _CACHE_PATH == path:
        return _CACHE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        _CACHE = payload if isinstance(payload, dict) else {}
    except Exception:
        _CACHE = {}
    _CACHE_PATH = path
    return _CACHE


def _flush(force: bool = False) -> None:
    global _LAST_FLUSH, _DIRTY
    if _CACHE_PATH is None or not _DIRTY:
        return
    now = time.time()
    if not force and now - _LAST_FLUSH < FLUSH_MIN_INTERVAL_SEC:
        return
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_CACHE, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
        _LAST_FLUSH = now
        _DIRTY = False
    except Exception:
        log.debug("telegram_roster: flush failed", exc_info=True)


def _chat_key(chat: Any) -> str:
    return str(chat or "").strip().lstrip("@").lower()


def _evict(chat_map: dict) -> None:
    if len(chat_map) <= MAX_PARTICIPANTS_PER_CHAT:
        return
    victims = sorted(chat_map.items(), key=lambda kv: kv[1].get("last_seen") or 0)
    for key, _ in victims[: len(chat_map) - MAX_PARTICIPANTS_PER_CHAT]:
        chat_map.pop(key, None)


def observe_message(data_root: Any, chat: Any, sender_id: Any,
                    handle: Optional[str], name: Optional[str],
                    now: Optional[float] = None) -> None:
    """Record auto-facts for one inbound group message. Never raises."""
    if sender_id is None:
        return
    if now is None:
        now = time.time()
    try:
        with _LOCK:
            roster = _load(data_root)
            chat_map = roster.setdefault(_chat_key(chat), {})
            key = str(int(sender_id))
            entry = chat_map.get(key)
            is_new = entry is None
            if is_new:
                entry = {"first_seen": now, "msg_count": 0}
                chat_map[key] = entry
            entry["last_seen"] = now
            entry["msg_count"] = int(entry.get("msg_count") or 0) + 1
            if handle:
                entry["handle"] = str(handle).strip().lstrip("@")
            if name:
                entry["name"] = str(name).strip()[:80]
            _evict(chat_map)
            global _DIRTY
            _DIRTY = True
            _flush(force=is_new)
    except Exception:
        log.debug("telegram_roster: observe failed", exc_info=True)


def set_note(data_root: Any, chat: Any, handle: str, note: str,
             now: Optional[float] = None) -> bool:
    """Append a free-text note for a participant, matched by @handle
    (case-insensitive). Notes accumulate (newest last, bounded, deduped)
    instead of overwriting, so one new fact no longer erases the older ones.
    The legacy single ``note`` field mirrors the newest note for old readers.
    Returns True when a participant matched."""
    handle = str(handle or "").strip().lstrip("@").lower()
    note = " ".join(str(note or "").split())[:NOTE_MAX_CHARS]
    if not handle or not note:
        return False
    if now is None:
        now = time.time()
    try:
        with _LOCK:
            roster = _load(data_root)
            chat_map = roster.get(_chat_key(chat)) or {}
            for entry in chat_map.values():
                if str(entry.get("handle") or "").lower() == handle:
                    notes = entry.get("notes")
                    if not isinstance(notes, list):
                        notes = [entry["note"]] if entry.get("note") else []
                    if not any(str(n).strip().lower() == note.lower() for n in notes):
                        notes.append(note)
                    entry["notes"] = notes[-NOTES_MAX_PER_PARTICIPANT:]
                    entry["note"] = entry["notes"][-1]
                    entry["note_ts"] = now
                    global _DIRTY
                    _DIRTY = True
                    _flush(force=True)
                    return True
    except Exception:
        log.debug("telegram_roster: set_note failed", exc_info=True)
    return False


def participant_note(data_root: Any, chat: Any, handle: str) -> str:
    """Free-text note(s) for one participant by @handle; "" when unknown.
    Supports both the legacy single ``note`` field and the ``notes`` list."""
    handle = str(handle or "").strip().lstrip("@").lower()
    if not handle:
        return ""
    try:
        with _LOCK:
            roster = _load(data_root)
            chat_map = roster.get(_chat_key(chat)) or {}
            for entry in chat_map.values():
                if str(entry.get("handle") or "").lower() == handle:
                    notes = entry.get("notes")
                    if isinstance(notes, list) and notes:
                        return " | ".join(str(n) for n in notes[-3:])
                    return str(entry.get("note") or "")
    except Exception:
        log.debug("telegram_roster: participant_note failed", exc_info=True)
    return ""


def _label(entry: dict) -> str:
    handle = entry.get("handle")
    name = entry.get("name")
    if handle and name:
        return f"@{handle} ({name})"
    return f"@{handle}" if handle else str(name or "?")


def roster_block(data_root: Any, chat: Any, handles: Optional[list[str]] = None,
                 limit: int = 8, now: Optional[float] = None) -> str:
    """Compact prompt block: current senders first, then most-active others.
    Empty string when nothing is known yet."""
    if now is None:
        now = time.time()
    want = {str(h or "").strip().lstrip("@").lower() for h in (handles or []) if h}
    try:
        with _LOCK:
            roster = _load(data_root)
            chat_map = dict(roster.get(_chat_key(chat)) or {})
    except Exception:
        return ""
    if not chat_map:
        return ""

    def _rank(entry: dict) -> tuple:
        is_current = str(entry.get("handle") or "").lower() in want
        return (not is_current, -(entry.get("last_seen") or 0), -(entry.get("msg_count") or 0))

    lines = []
    for entry in sorted(chat_map.values(), key=_rank)[:limit]:
        days = max(0, int((now - (entry.get("first_seen") or now)) // 86400))
        bits = [f"{_label(entry)}: {entry.get('msg_count', 0)} msgs"]
        if days:
            bits.append(f"known {days}d")
        notes = entry.get("notes")
        if isinstance(notes, list) and notes:
            bits.append("note: " + " | ".join(str(n) for n in notes[-NOTES_MAX_PER_PARTICIPANT:]))
        elif entry.get("note"):
            bits.append("note: " + str(entry["note"]))
        lines.append("  " + ", ".join(bits))
    if not lines:
        return ""
    return ("People you know in this chat (your own accumulated notes — "
            "still UNTRUSTED chat-derived text):\n" + "\n".join(lines))
