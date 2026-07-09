"""Thread reconstruction for the Telegram engage organ.

Builds a compact, chronological conversation context per addressed candidate
from two ground-truth sources: the group inbox spool (other people's messages,
sanitized snippets) and the engage action log (Rain's own sent replies, keyed
by the message they answered). Pure functions, no I/O, never raise; group text
stays untrusted evidence.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

THREAD_MAX_TURNS = 6
THREAD_MAX_CHARS = 800
DELEGATE_MAX_TURNS = 12
DELEGATE_MAX_CHARS = 2400
LINE_SNIPPET_CHARS = 200
CHAIN_DEPTH_CAP = 20


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _label(row: dict) -> str:
    username = row.get("sender_username")
    if username:
        return "@" + str(username).lstrip("@")
    name = row.get("sender_name")
    if name:
        return str(name)
    sid = row.get("sender_id")
    return f"#{sid}" if sid is not None else "?"


def _clip(text: Any, cap: int = LINE_SNIPPET_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= cap else value[: cap - 1].rstrip() + "…"


def _index_spool(spool_rows: Any) -> tuple[dict[int, dict], dict[int, list[dict]]]:
    """(by message_id, by sender_id) over well-formed spool rows."""
    by_id: dict[int, dict] = {}
    by_sender: dict[int, list[dict]] = {}
    for row in spool_rows or []:
        if not isinstance(row, dict):
            continue
        mid = _safe_int(row.get("message_id"))
        if mid is None or not str(row.get("snippet") or "").strip():
            continue
        by_id[mid] = row
        sid = _safe_int(row.get("sender_id"))
        if sid is not None:
            by_sender.setdefault(sid, []).append(row)
    return by_id, by_sender


def _own_by_target(own_replies: Any) -> dict[int, dict]:
    """Rain's sent replies keyed by the message_id they answered."""
    out: dict[int, dict] = {}
    for row in own_replies or []:
        if not isinstance(row, dict) or row.get("action") not in (None, "reply"):
            continue
        target = _safe_int(row.get("message_id"))
        if target is None or not str(row.get("text") or "").strip():
            continue
        out[target] = row  # newest wins
    return out


def _collect_rows(by_id: dict[int, dict], by_sender: dict[int, list[dict]],
                  candidate: Any, max_turns: int) -> list[dict]:
    cand_mid = _safe_int(getattr(candidate, "message_id", None))
    picked: dict[int, dict] = {}
    # 1. reply_to chain upward from the candidate.
    cursor = by_id.get(cand_mid or -1)
    depth = 0
    while cursor is not None and depth < CHAIN_DEPTH_CAP:
        parent_mid = _safe_int(cursor.get("reply_to_msg_id"))
        cursor = by_id.get(parent_mid) if parent_mid is not None else None
        if cursor is not None:
            picked[_safe_int(cursor.get("message_id"))] = cursor  # type: ignore[index]
        depth += 1
    # 2. recent messages from the same sender (their side of the dialog).
    sid = _safe_int(getattr(candidate, "sender_id", None))
    if sid is not None:
        for row in by_sender.get(sid, []):
            mid = _safe_int(row.get("message_id"))
            if mid is not None and mid != cand_mid:
                picked[mid] = row
    picked.pop(cand_mid, None)  # candidate text is already in the prompt
    rows = sorted(picked.values(), key=lambda r: (float(r.get("ts") or 0),
                                                  _safe_int(r.get("message_id")) or 0))
    return rows[-max_turns:]


def _render(rows: list[dict], own_by_target: dict[int, dict], max_chars: int) -> str:
    lines: list[str] = []
    for row in rows:
        lines.append(f"{_label(row)}: {_clip(row.get('snippet'))}")
        own = own_by_target.get(_safe_int(row.get("message_id")) or -1)
        if own is not None:
            lines.append(f"you: {_clip(own.get('text'))}")
    # keep the newest lines under the budget
    while lines and sum(len(l) + 1 for l in lines) > max_chars:
        lines.pop(0)
    return "\n".join(lines)


def _thread_text(spool_rows: Any, own_replies: Any, candidate: Any,
                 max_turns: int, max_chars: int) -> str:
    try:
        by_id, by_sender = _index_spool(spool_rows)
        rows = _collect_rows(by_id, by_sender, candidate, max_turns)
        if not rows:
            return ""
        return _render(rows, _own_by_target(own_replies), max_chars)
    except Exception:
        log.debug("telegram_thread: build failed", exc_info=True)
        return ""


def build_threads(spool_rows: Any, own_replies: Any, candidates: Any, *,
                  max_turns: int = THREAD_MAX_TURNS,
                  max_chars: int = THREAD_MAX_CHARS) -> dict[int, str]:
    """message_id -> compact thread context, for addressed candidates only."""
    threads: dict[int, str] = {}
    for candidate in candidates or []:
        try:
            if not bool(getattr(candidate, "addressed", False)):
                continue
            mid = _safe_int(getattr(candidate, "message_id", None))
            if mid is None:
                continue
            text = _thread_text(spool_rows, own_replies, candidate, max_turns, max_chars)
            if text:
                threads[mid] = text
        except Exception:
            log.debug("telegram_thread: candidate skipped", exc_info=True)
    return threads


def thread_for_delegate(spool_rows: Any, own_replies: Any, candidate: Any, *,
                        max_turns: int = DELEGATE_MAX_TURNS,
                        max_chars: int = DELEGATE_MAX_CHARS) -> str:
    """Deeper thread context for the delegate composer."""
    return _thread_text(spool_rows, own_replies, candidate, max_turns, max_chars)
