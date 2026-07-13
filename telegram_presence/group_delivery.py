"""Durable ACK-aware Telegram group actions (reply/react outbox).

One inbound message/action is one durable send intent, persisted in SQLite
before any transport call. A process restart resets in-flight rows to
``retry`` (at-least-once delivery); a stable ``transport_random_id`` derived
from the intent lets Telegram-side deduplication suppress the rare duplicate.
Exhausted intents become dead-letter rows — a terminal, audited outcome the
engage cycle can tombstone instead of retrying forever.

Stdlib-only; the actual send callables are injected.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import inspect
import json
import logging
import os
import pathlib
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .inbox import canonical_chat_peer


log = logging.getLogger(__name__)
GROUP_ACTION_SENDING_LEASE_SECONDS = 120.0
GROUP_ACTION_TERMINAL_RETENTION_SECONDS = 30 * 24 * 60 * 60
GROUP_ACTION_TERMINAL_MAX_ROWS = 5000
GROUP_ACTION_PRUNE_INTERVAL_SECONDS = 6 * 60 * 60
DEFAULT_SEND_TIMEOUT_SEC = 45.0


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_telegram_message_id(result: Any) -> Optional[int]:
    if isinstance(result, (list, tuple)):
        for item in reversed(result):
            parsed = extract_telegram_message_id(item)
            if parsed is not None:
                return parsed
        return None
    for name in ("message_id", "id"):
        value = getattr(result, name, None)
        parsed = _optional_int(value)
        if parsed is not None:
            return parsed
    if isinstance(result, dict):
        for name in ("message_id", "id"):
            parsed = _optional_int(result.get(name))
            if parsed is not None:
                return parsed
    return None


def extract_telegram_message_ids(result: Any) -> tuple[int, ...]:
    """Return every ordered Telegram message id from a multipart ACK result."""
    if isinstance(result, dict) and isinstance(result.get("message_ids"), (list, tuple)):
        return tuple(
            parsed for value in result["message_ids"]
            if (parsed := _optional_int(value)) is not None
        )
    if isinstance(result, (list, tuple)):
        found: list[int] = []
        for item in result:
            found.extend(extract_telegram_message_ids(item))
        return tuple(found)
    parsed = extract_telegram_message_id(result)
    return (parsed,) if parsed is not None else ()


def resolve_transport_ack(
    submission: Any,
    *,
    timeout: float = DEFAULT_SEND_TIMEOUT_SEC,
) -> Any:
    """Resolve a scheduled transport operation into a real remote ACK.

    Synchronous ``True`` remains supported for small injected adapters, while
    Bot API/Telethon Futures are awaited and cancelled on timeout.
    """
    if submission is False or submission is None:
        raise RuntimeError("transport did not accept delivery")
    result = submission
    waiter = getattr(result, "result", None)
    if callable(waiter):
        try:
            result = waiter(timeout=max(0.05, float(timeout)))
        except concurrent.futures.TimeoutError:
            cancel = getattr(submission, "cancel", None)
            if callable(cancel):
                cancel()
            raise
    if result is False or result is None:
        raise RuntimeError("transport completed without an ACK")
    return result


def _transport_random_id(action_id: str) -> int:
    """Stable signed int64 used by Telegram to deduplicate one send intent."""
    value = int.from_bytes(
        hashlib.sha256(str(action_id).encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )
    return value or 1


def _prune_terminal_actions(
    conn: sqlite3.Connection,
    *,
    now: float,
    retention_seconds: float = GROUP_ACTION_TERMINAL_RETENTION_SECONDS,
    max_rows: int = GROUP_ACTION_TERMINAL_MAX_ROWS,
) -> int:
    """Bound terminal audit rows without touching pending delivery intents."""
    rows = conn.execute(
        "SELECT action_id,updated_at FROM group_actions "
        "WHERE status IN ('acked','dead','superseded') "
        "ORDER BY updated_at DESC,created_at DESC"
    ).fetchall()
    cutoff = float(now) - max(0.0, float(retention_seconds))
    retained = 0
    removed = 0
    for row in rows:
        keep = (
            float(row["updated_at"] or 0) >= cutoff
            and retained < max(0, int(max_rows))
        )
        if keep:
            retained += 1
            continue
        conn.execute(
            "DELETE FROM group_actions WHERE action_id=? "
            "AND status IN ('acked','dead','superseded')",
            (str(row["action_id"]),),
        )
        removed += 1
    return removed


def _maybe_prune_terminal_actions(conn: sqlite3.Connection, *, now: float) -> int:
    row = conn.execute(
        "SELECT value FROM group_action_meta WHERE key='last_terminal_prune_at'"
    ).fetchone()
    last = float(row["value"] or 0) if row is not None else 0.0
    elapsed = float(now) - last
    if row is not None and 0 <= elapsed < GROUP_ACTION_PRUNE_INTERVAL_SECONDS:
        return 0
    removed = _prune_terminal_actions(conn, now=now)
    conn.execute(
        "INSERT INTO group_action_meta(key,value) VALUES ('last_terminal_prune_at',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(float(now)),),
    )
    return removed


def _sender_accepts_random_id(sender: Callable[..., Any]) -> bool:
    """Check the adapter contract before invoking it; never retry on TypeError."""
    try:
        parameters = inspect.signature(sender).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "random_id"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _invoke_sender(record: "GroupAction", sender: Callable[..., Any]) -> Any:
    if (
        record.action == "reply"
        and record.transport_random_id
        and _sender_accepts_random_id(sender)
    ):
        return sender(
            record.chat,
            record.msg_id,
            record.payload,
            random_id=record.transport_random_id,
        )
    return sender(record.chat, record.msg_id, record.payload)


@dataclass(frozen=True)
class GroupAction:
    action_id: str
    chat: str
    msg_id: int
    action: str
    intent_key: str
    transport_random_id: int
    payload: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: float
    telegram_message_ids: tuple[int, ...] = ()
    last_error: str = ""


class GroupActionDeadLettered(RuntimeError):
    """Terminal durable outcome: retrying this stable action cannot succeed."""

    def __init__(self, record: GroupAction) -> None:
        super().__init__("group action is dead-lettered")
        self.record = record


class GroupActionOutbox:
    def __init__(
        self,
        drive_root: str | pathlib.Path,
        *,
        max_attempts: int = 8,
        base_backoff_sec: float = 2.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.drive_root = pathlib.Path(drive_root)
        self.db_path = self.drive_root / "state" / "telegram_group_delivery.sqlite3"
        self.max_attempts = max(1, int(max_attempts))
        self.base_backoff_sec = max(0.05, float(base_backoff_sec))
        self._now = now
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.db_path.parent, 0o700)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS group_actions ("
                "action_id TEXT PRIMARY KEY, chat TEXT NOT NULL, msg_id INTEGER NOT NULL, "
                "action TEXT NOT NULL, intent_key TEXT NOT NULL DEFAULT '', "
                "transport_random_id INTEGER NOT NULL DEFAULT 0, "
                "payload TEXT NOT NULL, status TEXT NOT NULL, "
                "attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, "
                "next_attempt_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
                "telegram_message_ids TEXT NOT NULL DEFAULT '[]', last_error TEXT NOT NULL DEFAULT '')"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS group_action_due_idx "
                "ON group_actions(status,next_attempt_at,created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS group_action_terminal_idx "
                "ON group_actions(status,updated_at)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS group_action_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(group_actions)").fetchall()
            }
            added_intent_key = "intent_key" not in columns
            if added_intent_key:
                conn.execute(
                    "ALTER TABLE group_actions ADD COLUMN intent_key "
                    "TEXT NOT NULL DEFAULT ''"
                )
            if "transport_random_id" not in columns:
                conn.execute(
                    "ALTER TABLE group_actions ADD COLUMN transport_random_id "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            now_value = self._now()
            conn.execute(
                "UPDATE group_actions SET status='retry', next_attempt_at=?, updated_at=?, "
                "last_error=CASE WHEN last_error='' THEN 'process_restarted_inflight' ELSE last_error END "
                "WHERE status='sending' AND updated_at<=?",
                (
                    now_value,
                    now_value,
                    now_value - GROUP_ACTION_SENDING_LEASE_SECONDS,
                ),
            )
            # Older Rain-compatible schemas identified an action by a digest
            # that included its payload. Rephrasing the same inbound reply
            # could therefore leave multiple active rows. Canonicalize peer
            # aliases and retain one natural intent before adding the current
            # unique index. Standalone msg_id=0 posts remain distinct by their
            # historical payload-derived identity.
            action_rows = conn.execute(
                "SELECT action_id,chat,msg_id,intent_key,transport_random_id,payload "
                "FROM group_actions"
            ).fetchall()
            canonical_updates = [
                (canonical_chat_peer(row["chat"]), str(row["action_id"]))
                for row in action_rows
                if canonical_chat_peer(row["chat"])
                and canonical_chat_peer(row["chat"]) != str(row["chat"])
            ]
            legacy_intent_updates = [
                (
                    "legacy-payload:" + hashlib.sha256(
                        str(row["payload"] or "").encode("utf-8")
                    ).hexdigest(),
                    str(row["action_id"]),
                )
                for row in action_rows
                if int(row["msg_id"]) == 0 and not str(row["intent_key"] or "")
            ]
            random_id_updates = [
                (_transport_random_id(str(row["action_id"])), str(row["action_id"]))
                for row in action_rows
                if not int(row["transport_random_id"] or 0)
            ]
            index_columns = [
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA index_info(group_action_natural_key_idx)"
                ).fetchall()
            ]
            rebuild_natural_index = (
                added_intent_key
                or bool(canonical_updates)
                or bool(legacy_intent_updates)
                or index_columns != ["chat", "msg_id", "action", "intent_key"]
            )
            if rebuild_natural_index:
                conn.execute("DROP INDEX IF EXISTS group_action_natural_key_idx")
            for canonical, action_id in canonical_updates:
                conn.execute(
                    "UPDATE group_actions SET chat=? WHERE action_id=?",
                    (canonical, action_id),
                )
            for intent_key, action_id in legacy_intent_updates:
                conn.execute(
                    "UPDATE group_actions SET intent_key=? WHERE action_id=?",
                    (intent_key, action_id),
                )
            for random_id, action_id in random_id_updates:
                conn.execute(
                    "UPDATE group_actions SET transport_random_id=? WHERE action_id=?",
                    (random_id, action_id),
                )
            if rebuild_natural_index:
                duplicates = conn.execute(
                    "SELECT chat,msg_id,action,intent_key FROM group_actions "
                    "WHERE status!='superseded' GROUP BY chat,msg_id,action,intent_key "
                    "HAVING COUNT(*)>1"
                ).fetchall()
                for duplicate in duplicates:
                    rows = conn.execute(
                        "SELECT action_id,status FROM group_actions "
                        "WHERE chat=? AND msg_id=? AND action=? AND intent_key=? "
                        "AND status!='superseded' "
                        "ORDER BY CASE status WHEN 'acked' THEN 0 WHEN 'pending' THEN 1 "
                        "WHEN 'retry' THEN 2 WHEN 'sending' THEN 3 ELSE 4 END, "
                        "created_at, action_id",
                        (
                            duplicate["chat"], duplicate["msg_id"],
                            duplicate["action"], duplicate["intent_key"],
                        ),
                    ).fetchall()
                    winner = str(rows[0]["action_id"])
                    for stale in rows[1:]:
                        conn.execute(
                            "UPDATE group_actions SET status='superseded', updated_at=?, "
                            "last_error=? WHERE action_id=?",
                            (now_value, f"superseded_by:{winner}", stale["action_id"]),
                        )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS group_action_natural_key_idx "
                    "ON group_actions(chat,msg_id,action,intent_key) "
                    "WHERE status!='superseded'"
                )
            _maybe_prune_terminal_actions(conn, now=now_value)
        os.chmod(self.db_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def enqueue(
        self,
        chat: Any,
        msg_id: int,
        action: str,
        payload: str,
        *,
        idempotency_key: str = "",
    ) -> GroupAction:
        clean_action = str(action)
        if clean_action not in {"reply", "react"}:
            raise ValueError("unsupported Telegram group action")
        clean_chat = canonical_chat_peer(chat)
        clean_payload = str(payload or "")
        clean_msg_id = max(0, int(msg_id))
        intent_key = ""
        if clean_msg_id == 0:
            raw_intent = str(idempotency_key or "").strip()
            if raw_intent:
                intent_key = "request:" + hashlib.sha256(
                    raw_intent.encode("utf-8")
                ).hexdigest()
            else:
                # Compatibility for legacy/manual events. New tool events carry
                # a request UUID, so identical text may still be posted twice
                # intentionally while a replay of one event stays idempotent.
                intent_key = "legacy-payload:" + hashlib.sha256(
                    clean_payload.encode("utf-8")
                ).hexdigest()
        # One inbound message/action is one durable intent.  Payload belongs to
        # that record, not its identity: a retrying LLM may rephrase the answer,
        # but must not create a second send while the first wording is pending.
        digest = hashlib.sha256(
            f"{clean_chat}\0{clean_msg_id}\0{clean_action}\0{intent_key}".encode()
        ).hexdigest()[:40]
        action_id = "tga-" + digest
        transport_random_id = _transport_random_id(action_id)
        now = self._now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM group_actions WHERE chat=? AND msg_id=? AND action=? "
                "AND intent_key=? AND status!='superseded' LIMIT 1",
                (clean_chat, clean_msg_id, clean_action, intent_key),
            ).fetchone()
            if existing is not None:
                return self._record(existing)
            conn.execute(
                "INSERT OR IGNORE INTO group_actions "
                "(action_id,chat,msg_id,action,intent_key,transport_random_id,"
                "payload,status,attempts,"
                "max_attempts,next_attempt_at,created_at,updated_at,"
                "telegram_message_ids,last_error) VALUES "
                "(?,?,?,?,?,?,?,'pending',0,?,?,?,?,'[]','')",
                (
                    action_id, clean_chat, clean_msg_id, clean_action, intent_key,
                    transport_random_id, clean_payload,
                    self.max_attempts, now, now, now,
                ),
            )
        action_row = self.get(action_id)
        if action_row is None:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM group_actions WHERE chat=? AND msg_id=? AND action=? "
                    "AND intent_key=? AND status!='superseded' LIMIT 1",
                    (clean_chat, clean_msg_id, clean_action, intent_key),
                ).fetchone()
            action_row = self._record(row) if row else None
        if action_row is None:
            raise RuntimeError("group action enqueue failed")
        return action_row

    def get(self, action_id: str) -> GroupAction | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM group_actions WHERE action_id=?", (str(action_id),)
            ).fetchone()
        return self._record(row) if row else None

    def due(self, limit: int = 4) -> list[GroupAction]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM group_actions WHERE status IN ('pending','retry') "
                "AND next_attempt_at<=? ORDER BY created_at LIMIT ?",
                (self._now(), max(1, int(limit))),
            ).fetchall()
        return [self._record(row) for row in rows]

    def deliver(self, action_id: str, sender: Callable[[Any, int, str], Any]) -> Any:
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM group_actions WHERE action_id=?", (str(action_id),)
            ).fetchone()
            if row is None:
                raise RuntimeError("group action is missing")
            if row["status"] == "acked":
                return {"message_ids": json.loads(row["telegram_message_ids"] or "[]")}
            if row["status"] == "dead":
                raise GroupActionDeadLettered(self._record(row))
            if row["status"] == "sending":
                raise RuntimeError("group action is already in flight")
            if float(row["next_attempt_at"]) > now:
                raise RuntimeError("group action retry is deferred")
            conn.execute(
                "UPDATE group_actions SET status='sending', attempts=attempts+1, updated_at=? "
                "WHERE action_id=?",
                (now, str(action_id)),
            )
            conn.commit()
        current = self.get(action_id)
        assert current is not None
        try:
            ack = resolve_transport_ack(
                _invoke_sender(current, sender)
            )
        except BaseException as exc:
            current = self.get(action_id) or current
            dead = current.attempts >= current.max_attempts
            status = "dead" if dead else "retry"
            delay = self.base_backoff_sec * (2 ** max(0, current.attempts - 1))
            with self._connect() as conn:
                conn.execute(
                    "UPDATE group_actions SET status=?, next_attempt_at=?, updated_at=?, last_error=? "
                    "WHERE action_id=?",
                    (
                        status, now if dead else now + min(300.0, delay), now,
                        f"{type(exc).__name__}: {exc}"[:500], action_id,
                    ),
                )
            raise
        message_ids = list(extract_telegram_message_ids(ack))
        with self._connect() as conn:
            conn.execute(
                "UPDATE group_actions SET status='acked', telegram_message_ids=?, "
                "last_error='', updated_at=? WHERE action_id=?",
                (json.dumps(message_ids), self._now(), action_id),
            )
        return ack

    @staticmethod
    def _record(row: sqlite3.Row) -> GroupAction:
        return GroupAction(
            action_id=str(row["action_id"]), chat=str(row["chat"]),
            msg_id=int(row["msg_id"]), action=str(row["action"]),
            intent_key=str(row["intent_key"] or ""),
            transport_random_id=int(row["transport_random_id"] or 0),
            payload=str(row["payload"]), status=str(row["status"]),
            attempts=int(row["attempts"]), max_attempts=int(row["max_attempts"]),
            next_attempt_at=float(row["next_attempt_at"]),
            telegram_message_ids=tuple(json.loads(row["telegram_message_ids"] or "[]")),
            last_error=str(row["last_error"] or ""),
        )


def deliver_group_action(
    drive_root: Any,
    *,
    chat: Any,
    msg_id: int,
    action: str,
    payload: str,
    sender: Callable[[Any, int, str], Any],
    return_record: bool = False,
    idempotency_key: str = "",
    action_id: str = "",
) -> Any:
    outbox = GroupActionOutbox(drive_root)
    if action_id:
        record = outbox.get(action_id)
        if record is None:
            raise RuntimeError("durably queued group action is missing")
        expected = (
            canonical_chat_peer(chat), max(0, int(msg_id)), str(action), str(payload or "")
        )
        actual = (record.chat, record.msg_id, record.action, record.payload)
        if actual != expected:
            raise RuntimeError("durably queued group action does not match event")
    else:
        record = outbox.enqueue(
            chat, msg_id, action, payload, idempotency_key=idempotency_key,
        )
    ack = outbox.deliver(record.action_id, sender)
    if return_record:
        return {"ack": ack, "record": outbox.get(record.action_id) or record}
    return ack


def drain_group_actions(
    drive_root: Any,
    *,
    do_reply: Callable[[Any, int, str], Any],
    do_react: Callable[[Any, int, str], Any],
    limit: int = 4,
) -> int:
    outbox = GroupActionOutbox(drive_root)
    completed = 0
    for record in outbox.due(limit=limit):
        sender = do_reply if record.action == "reply" else do_react
        try:
            outbox.deliver(record.action_id, sender)
            completed += 1
        except Exception:
            continue
    return completed
