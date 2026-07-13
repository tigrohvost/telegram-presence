"""Schema-upgrade and natural-key regressions for the group-action outbox."""
from __future__ import annotations

import sqlite3

from telegram_presence.group_delivery import GroupActionOutbox


def _create_legacy_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE group_actions ("
        "action_id TEXT PRIMARY KEY, chat TEXT NOT NULL, msg_id INTEGER NOT NULL, "
        "action TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, "
        "attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, "
        "next_attempt_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
        "telegram_message_ids TEXT NOT NULL DEFAULT '[]', last_error TEXT NOT NULL DEFAULT '')"
    )


def _insert_legacy(
    connection: sqlite3.Connection,
    *,
    action_id: str,
    chat: str,
    msg_id: int,
    payload: str,
    status: str = "pending",
    created_at: float = 1.0,
) -> None:
    connection.execute(
        "INSERT INTO group_actions VALUES (?,?,?,?,?,?,0,8,0,?,?, '[]','')",
        (
            action_id,
            chat,
            msg_id,
            "reply",
            payload,
            status,
            created_at,
            created_at,
        ),
    )


def test_legacy_schema_migration_is_idempotent_and_deduplicates_aliases(tmp_path):
    db = tmp_path / "state" / "telegram_group_delivery.sqlite3"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        _create_legacy_schema(connection)
        # Same priority and timestamp exercise the stable action_id tie-break.
        _insert_legacy(
            connection,
            action_id="legacy-b",
            chat="@Chat",
            msg_id=7,
            payload="second wording",
            status="acked",
        )
        _insert_legacy(
            connection,
            action_id="legacy-a",
            chat="chat",
            msg_id=7,
            payload="first wording",
            status="acked",
        )

    first = GroupActionOutbox(tmp_path, now=lambda: 10.0)
    winner = first.enqueue("@CHAT", 7, "reply", "third wording")
    assert winner.action_id == "legacy-a"
    assert winner.chat == "@chat"
    assert winner.payload == "first wording"
    assert winner.transport_random_id != 0

    with sqlite3.connect(db) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(group_actions)")
        }
        assert {"intent_key", "transport_random_id"} <= columns
        assert connection.execute(
            "SELECT COUNT(*) FROM group_actions WHERE status='superseded'"
        ).fetchone()[0] == 1
        assert [
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(group_action_natural_key_idx)"
            )
        ] == ["chat", "msg_id", "action", "intent_key"]

    # A completed or interrupted migration must be safe to open repeatedly.
    second = GroupActionOutbox(tmp_path, now=lambda: 11.0)
    assert second.enqueue("chat", 7, "reply", "fourth wording").action_id == "legacy-a"


def test_legacy_migration_preserves_distinct_standalone_posts(tmp_path):
    db = tmp_path / "state" / "telegram_group_delivery.sqlite3"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        _create_legacy_schema(connection)
        _insert_legacy(
            connection,
            action_id="legacy-standalone-one",
            chat="@Chat",
            msg_id=0,
            payload="first post",
            created_at=1.0,
        )
        _insert_legacy(
            connection,
            action_id="legacy-standalone-two",
            chat="chat",
            msg_id=0,
            payload="second post",
            created_at=2.0,
        )

    box = GroupActionOutbox(tmp_path, now=lambda: 10.0)
    due = box.due()
    replay = box.enqueue("@chat", 0, "reply", "first post")

    assert [row.action_id for row in due] == [
        "legacy-standalone-one",
        "legacy-standalone-two",
    ]
    assert len({row.intent_key for row in due}) == 2
    assert all(row.intent_key.startswith("legacy-payload:") for row in due)
    assert replay.action_id == "legacy-standalone-one"
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM group_actions WHERE status='superseded'"
        ).fetchone()[0] == 0
