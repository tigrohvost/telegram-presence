"""Conversation-scene reconstruction for the Telegram engage organ.

Builds a compact, chronological scene for every candidate from two
ground-truth sources: the group inbox spool (other people's messages,
sanitized snippets) and the engage action log (the agent's own sent replies,
keyed by the message they answered).  A scene combines reply ancestry, the
sender's side of the exchange, and nearby turns from other participants.  It
does not pre-decide who is being addressed; reply/mention/name data remains
evidence for the semantic decider.  Pure functions, no I/O, never raise;
group text stays untrusted evidence.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .hooks import agent_name

log = logging.getLogger(__name__)

THREAD_MAX_TURNS = 8
THREAD_MAX_CHARS = 1200
DELEGATE_MAX_TURNS = 12
DELEGATE_MAX_CHARS = 2400
LINE_SNIPPET_CHARS = 200
CHAIN_DEPTH_CAP = 20


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clip(text: Any, cap: int = LINE_SNIPPET_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= cap else value[: cap - 1].rstrip() + "…"


def _index_spool(spool_rows: Any) -> tuple[dict[int, dict], dict[int, list[dict]]]:
    """(by message_id, by sender_id) over well-formed, de-duplicated rows."""
    by_id: dict[int, dict] = {}
    for row in spool_rows or []:
        if not isinstance(row, dict):
            continue
        mid = _safe_int(row.get("message_id"))
        if mid is None or not str(row.get("snippet") or "").strip():
            continue
        # Reader backfills can re-observe the same Telegram message with
        # richer metadata.  The newest spool copy is the useful one; duplicate
        # rows must not crowd real turns out of the scene budget.
        by_id[mid] = row
    by_sender: dict[int, list[dict]] = {}
    for row in by_id.values():
        sid = _safe_int(row.get("sender_id"))
        if sid is not None:
            by_sender.setdefault(sid, []).append(row)
    for rows in by_sender.values():
        rows.sort(key=lambda r: (float(r.get("ts") or 0),
                                 _safe_int(r.get("message_id")) or 0))
    return by_id, by_sender


def _own_by_target(own_replies: Any) -> dict[int, dict]:
    """The agent's sent replies keyed by the message_id they answered."""
    out: dict[int, dict] = {}
    for row in own_replies or []:
        if not isinstance(row, dict) or row.get("action") not in (None, "reply"):
            continue
        target = _safe_int(row.get("message_id"))
        if target is None or not str(row.get("text") or "").strip():
            continue
        out[target] = row  # newest wins
    return out


def _is_before_candidate(row: dict, candidate: Any) -> bool:
    """Never leak later group messages into an earlier decision scene."""
    mid = _safe_int(row.get("message_id"))
    cand_mid = _safe_int(getattr(candidate, "message_id", None))
    try:
        row_ts = float(row.get("ts") or 0)
        cand_ts = float(getattr(candidate, "ts", 0) or 0)
    except (TypeError, ValueError):
        row_ts = cand_ts = 0.0
    if cand_ts and row_ts:
        return row_ts < cand_ts or (row_ts == cand_ts and (mid or -1) < (cand_mid or -1))
    return mid is not None and cand_mid is not None and mid < cand_mid


def _collect_rows(by_id: dict[int, dict], by_sender: dict[int, list[dict]],
                  candidate: Any, max_turns: int) -> list[dict]:
    cand_mid = _safe_int(getattr(candidate, "message_id", None))
    ancestry: dict[int, dict] = {}
    context: dict[int, dict] = {}
    # 1. reply_to chain upward from the candidate.
    cursor = by_id.get(cand_mid or -1)
    depth = 0
    while cursor is not None and depth < CHAIN_DEPTH_CAP:
        parent_mid = _safe_int(cursor.get("reply_to_msg_id"))
        cursor = by_id.get(parent_mid) if parent_mid is not None else None
        if cursor is not None:
            ancestry[_safe_int(cursor.get("message_id"))] = cursor  # type: ignore[index]
        depth += 1
    # 2. recent messages from the same sender (their side of the dialog).
    sid = _safe_int(getattr(candidate, "sender_id", None))
    if sid is not None:
        for row in by_sender.get(sid, []):
            mid = _safe_int(row.get("message_id"))
            if mid is not None and mid != cand_mid and _is_before_candidate(row, candidate):
                context[mid] = row
    # 3. nearby turns from every participant.  Addressee recognition depends
    # on the interaction graph, not only on lexical similarity or one
    # speaker's history.  The topic boundary is applied by _thread_text.
    neighborhood = sorted(
        (row for row in by_id.values() if _is_before_candidate(row, candidate)),
        key=lambda r: (float(r.get("ts") or 0),
                       _safe_int(r.get("message_id")) or 0),
    )
    for row in neighborhood[-max_turns:]:
        mid = _safe_int(row.get("message_id"))
        if mid is not None and mid != cand_mid:
            context[mid] = row
    ancestry.pop(cand_mid, None)
    context.pop(cand_mid, None)  # candidate text is already in the prompt
    for mid in ancestry:
        context.pop(mid, None)

    def _ordered(rows: Any) -> list[dict]:
        return sorted(rows, key=lambda r: (float(r.get("ts") or 0),
                                           _safe_int(r.get("message_id")) or 0))

    # Reserve the scene budget for the structural reply path first. A burst in
    # a neighboring floor must never evict the parent/correction chain.
    chain_rows = _ordered(ancestry.values())[-max_turns:]
    remaining = max(0, max_turns - len(chain_rows))
    context_rows = _ordered(context.values())[-remaining:] if remaining else []
    return _ordered(chain_rows + context_rows)


def _render(rows: list[dict], own_by_target: dict[int, dict],
            max_chars: int) -> str:
    self_label = agent_name()
    entries: list[tuple[str, str]] = []
    for row in rows:
        mid = _safe_int(row.get("message_id"))
        sender_id = _safe_int(row.get("sender_id"))
        metadata = [f"mid={mid}"]
        if sender_id is not None:
            metadata.append(f"sender_id={sender_id}")
        username = row.get("sender_username")
        if username:
            metadata.append("user=@" + str(username).lstrip("@"))
        elif row.get("sender_name"):
            metadata.append("name=" + _clip(row.get("sender_name"), 48))
        terms = {str(term).lower() for term in (row.get("matched_terms") or [])}
        parent_mid = _safe_int(row.get("reply_to_msg_id"))
        if "reply_to_me" in terms:
            metadata.append(f"reply_to={self_label}")
        elif parent_mid is not None:
            metadata.append(f"reply_to_mid={parent_mid}")
        entries.append(("[" + " ".join(metadata) + "]: ", str(row.get("snippet") or "")))
        own = own_by_target.get(mid or -1)
        if own is not None:
            entries.append((f"[self={self_label} reply_to_mid={mid}]: ",
                            str(own.get("text") or "")))
    if not entries:
        return ""

    # Preserve structural units under the character budget. Dropping the
    # oldest rendered line used to erase reply roots (or leave a dangling own
    # reply); clipping every unit keeps the interaction graph intact.
    overhead = sum(len(prefix) for prefix, _text in entries) + len(entries) - 1
    text_cap = max(1, min(
        LINE_SNIPPET_CHARS,
        (max_chars - overhead) // len(entries) if max_chars > overhead else 1,
    ))
    lines = [prefix + _clip(value, text_cap) for prefix, value in entries]
    # Default budgets leave ample metadata room. This final bound is only for
    # pathological caller-supplied tiny caps and never drops a whole unit.
    rendered = "\n".join(lines)
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars].rstrip()
    return rendered


def _thread_text(spool_rows: Any, own_replies: Any, candidate: Any,
                 max_turns: int, max_chars: int) -> str:
    try:
        topic_id = _safe_int(getattr(candidate, "topic_id", None))
        all_rows = [
            row for row in list(spool_rows or []) + list(own_replies or [])
            if isinstance(row, dict)
        ]
        forum_roots = {
            value for row in all_rows
            if (value := _safe_int(row.get("topic_id"))) is not None
        }

        def _in_topic(row: dict) -> bool:
            row_topic = _safe_int(row.get("topic_id"))
            row_mid = _safe_int(row.get("message_id"))
            # Telegram's root message often has topic_id=None while replies
            # carry reply_to_top_id == root message_id. The root is part of
            # that floor, not cross-topic leakage.
            if topic_id is None:
                # A forum root also carries topic_id=None. If another row names
                # its message_id as a topic, it is not general-chat context.
                return row_topic is None and row_mid not in forum_roots
            return row_topic == topic_id or row_mid == topic_id
        topic_spool = [
            row for row in (spool_rows or [])
            if isinstance(row, dict) and _in_topic(row)
        ]
        topic_own = [
            row for row in (own_replies or [])
            if isinstance(row, dict) and _in_topic(row)
        ]
        by_id, by_sender = _index_spool(topic_spool)
        rows = _collect_rows(by_id, by_sender, candidate, max_turns)
        if not rows:
            return ""
        return _render(rows, _own_by_target(topic_own), max_chars)
    except Exception:
        log.debug("telegram_thread: build failed", exc_info=True)
        return ""


def build_threads(spool_rows: Any, own_replies: Any, candidates: Any, *,
                  max_turns: int = THREAD_MAX_TURNS,
                  max_chars: int = THREAD_MAX_CHARS) -> dict[int, str]:
    """message_id -> compact multi-party scene for every candidate."""
    threads: dict[int, str] = {}
    for candidate in candidates or []:
        try:
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
    """Deeper scene context for the delegate composer."""
    return _thread_text(spool_rows, own_replies, candidate, max_turns, max_chars)
