"""Tests for the realtime group-mention inbox (no LLM, untrusted text)."""
import json

from telegram_presence import hooks
from telegram_presence.inbox import (
    GroupInbox,
    chat_matches,
    matched_terms,
    sanitize_snippet,
)


def test_matched_terms_mentions_and_inflections():
    # Reader parity: the @-form and the bare form are both reported when the
    # @mention matches (the bare pattern is not preceded by a \w char).
    assert matched_terms("привет @rain_ouroboros", ("rain_ouroboros",)) == [
        "@rain_ouroboros", "rain_ouroboros",
    ]
    assert "рейн" in matched_terms("спросите у Рейну про это", ())
    assert "ороборос" in matched_terms("Ороборосом интересуюсь", ())
    assert matched_terms("дождь и rainbow", ("rain",)) == []
    assert matched_terms(None, ("rain",)) == []


def test_chat_matches_username_and_id():
    assert chat_matches("abstractdl_chat", -100123, "@abstractdl_chat") is True
    assert chat_matches("ABSTRACTDL_CHAT", -100123, "abstractdl_chat") is True
    assert chat_matches("other_chat", -100123, "@abstractdl_chat") is False
    assert chat_matches(None, -100123, "-100123") is True


def test_sanitize_snippet_strips_and_caps():
    assert sanitize_snippet("a\x00b   c") == "a b c"
    assert len(sanitize_snippet("x" * 600)) == 500


def test_inbox_spools_addressed_and_reply_to_own(tmp_path):
    inbox = GroupInbox(tmp_path)
    inbox.remember_own_message(500)
    assert inbox.add_message(chat="@abstractdl_chat", message_id=1, sender_id=7,
                             text="эй @rain_ouroboros как дела", reply_to_msg_id=None,
                             self_id=99) is True
    assert inbox.add_message(chat="@abstractdl_chat", message_id=2, sender_id=7,
                             text="отвечаю на твоё", reply_to_msg_id=500,
                             self_id=99) is True
    # unaddressed chatter is spooled as context but flagged unaddressed
    assert inbox.add_message(chat="@abstractdl_chat", message_id=3, sender_id=7,
                             text="просто болтовня", reply_to_msg_id=None,
                             self_id=99) is True
    # own message never spooled
    assert inbox.add_message(chat="@abstractdl_chat", message_id=4, sender_id=99,
                             text="@rain_ouroboros сама себе", reply_to_msg_id=None,
                             self_id=99) is False
    rows = inbox.pending(after_ts=0.0)
    addressed = {r["message_id"]: r["addressed"] for r in rows}
    assert addressed == {1: True, 2: True, 3: False}
    assert "reply_to_me" in [t for r in rows if r["message_id"] == 2
                             for t in r["matched_terms"]]


def test_inbox_dedups_by_message_id(tmp_path):
    inbox = GroupInbox(tmp_path)
    assert inbox.add_message(chat="@c", message_id=1, sender_id=7,
                             text="@rain_ouroboros hi", reply_to_msg_id=None, self_id=9) is True
    assert inbox.add_message(chat="@c", message_id=1, sender_id=7,
                             text="@rain_ouroboros hi", reply_to_msg_id=None, self_id=9) is False


def test_inbox_marks_seen_only_after_durable_append(monkeypatch, tmp_path):
    inbox = GroupInbox(tmp_path)
    real_append = inbox._append

    def fail_append(_row):
        raise OSError("disk unavailable")

    monkeypatch.setattr(inbox, "_append", fail_append)
    values = {
        "chat": "@c",
        "message_id": 2,
        "sender_id": 7,
        "text": "@rain retry me",
        "reply_to_msg_id": None,
        "self_id": 9,
    }
    assert inbox.add_message(**values) is False
    assert inbox.pending() == []

    monkeypatch.setattr(inbox, "_append", real_append)
    assert inbox.add_message(**values) is True
    assert [row["message_id"] for row in inbox.pending()] == [2]


def test_inbox_recovers_seen_ids_after_restart(tmp_path):
    values = {
        "chat": "@c",
        "message_id": 3,
        "sender_id": 7,
        "text": "@rain only once",
        "reply_to_msg_id": None,
        "self_id": 9,
    }
    assert GroupInbox(tmp_path).add_message(**values) is True
    assert GroupInbox(tmp_path).add_message(**values) is False
    assert [row["message_id"] for row in GroupInbox(tmp_path).pending()] == [3]


def test_inbox_repairs_torn_jsonl_tail_before_acknowledging(tmp_path):
    spool = tmp_path / "state" / "telegram_group_inbox.jsonl"
    spool.parent.mkdir(parents=True)
    spool.write_bytes(b'{"chat":"@c"')

    inbox = GroupInbox(tmp_path)
    result = inbox.ingest_message(
        chat="@c",
        message_id=7,
        sender_id=7,
        text="@rain survives torn tail",
        reply_to_msg_id=None,
        self_id=9,
    )

    assert result.written is True and result.safe_to_ack is True
    assert [row["message_id"] for row in inbox.pending()] == [7]
    assert [json.loads(line)["message_id"] for line in spool.read_text().splitlines()] == [7]


def test_inbox_rechecks_disk_under_cross_instance_lock(tmp_path):
    first = GroupInbox(tmp_path)
    second = GroupInbox(tmp_path)
    values = {
        "chat": "@c",
        "message_id": 8,
        "sender_id": 7,
        "text": "@rain cross instance",
        "reply_to_msg_id": None,
        "self_id": 9,
    }

    assert first.add_message(**values) is True
    assert second.add_message(**values) is False
    assert [row["message_id"] for row in second.pending()] == [8]


def test_inbox_reconfirms_on_disk_row_after_directory_fsync_failure(
    monkeypatch, tmp_path
):
    (tmp_path / "state").mkdir()
    inbox = GroupInbox(tmp_path)
    real_fsync_directory = inbox._fsync_directory
    monkeypatch.setattr(
        inbox,
        "_fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("directory fsync failed")),
    )
    values = {
        "chat": "@c",
        "message_id": 9,
        "sender_id": 7,
        "text": "@rain fsync retry",
        "reply_to_msg_id": None,
        "self_id": 9,
    }

    failed = inbox.ingest_message(**values)
    assert failed.safe_to_ack is False
    monkeypatch.setattr(inbox, "_fsync_directory", real_fsync_directory)
    duplicate = inbox.ingest_message(**values)

    assert duplicate.written is False and duplicate.safe_to_ack is True
    assert [row["message_id"] for row in inbox.pending()] == [9]


def test_inbox_redacts_secret_before_jsonl_write(monkeypatch, tmp_path):
    monkeypatch.setattr(hooks, "_redactor", hooks._default_redact)
    inbox = GroupInbox(tmp_path)
    assert inbox.add_message(
        chat="@c",
        message_id=4,
        sender_id=7,
        sender_username="ghp_ABCDEFGHIJ",
        text="@rain token=SUPERSECRET123",
        reply_to_msg_id=None,
        self_id=9,
    ) is True

    raw = (tmp_path / "state" / "telegram_group_inbox.jsonl").read_text(
        encoding="utf-8"
    )
    assert "SUPERSECRET123" not in raw
    assert "ghp_ABCDEFGHIJ" not in raw
    assert "«redacted»" in raw
    assert (tmp_path / "state").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "state" / "telegram_group_inbox.jsonl").stat().st_mode & 0o777 == 0o600


def test_inbox_preserves_existing_shared_state_directory_mode(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o750)
    state.chmod(0o750)

    assert GroupInbox(tmp_path).add_message(
        chat="@c",
        message_id=10,
        sender_id=7,
        text="@rain shared state",
        reply_to_msg_id=None,
        self_id=9,
    ) is True
    assert state.stat().st_mode & 0o777 == 0o750


def test_inbox_refreshes_receipt_on_addressed(tmp_path):
    inbox = GroupInbox(tmp_path)
    inbox.add_message(chat="@c", message_id=11, sender_id=7,
                      text="@rain_ouroboros ping", reply_to_msg_id=None, self_id=9)
    receipt = json.loads((tmp_path / "state" / "telegram_addressed_mentions_monitor.json").read_text())
    assert receipt["status"] == "new_addressed_signal"
    assert 11 in receipt["addressed_ids"]


def test_pending_after_ts_filters(tmp_path):
    inbox = GroupInbox(tmp_path)
    inbox.add_message(chat="@c", message_id=1, sender_id=7,
                      text="@rain_ouroboros a", reply_to_msg_id=None, self_id=9, now=100.0)
    inbox.add_message(chat="@c", message_id=2, sender_id=7,
                      text="@rain_ouroboros b", reply_to_msg_id=None, self_id=9, now=200.0)
    assert [r["message_id"] for r in inbox.pending(after_ts=150.0)] == [2]
    assert inbox.has_unconsumed_addressed(after_ts=150.0) is True
    assert inbox.has_unconsumed_addressed(after_ts=250.0) is False


def test_inbox_captures_sender_username_and_name(tmp_path):
    inbox = GroupInbox(tmp_path)
    inbox.add_message(chat="@abstractdl_chat", message_id=1, sender_id=7,
                      text="@rain_ouroboros hi", reply_to_msg_id=None, self_id=9,
                      sender_username="@RealHandle", sender_name="Ashe Display")
    inbox.add_message(chat="@abstractdl_chat", message_id=2, sender_id=8,
                      text="@rain_ouroboros yo", reply_to_msg_id=None, self_id=9,
                      sender_username=None, sender_name="No Handle Guy")
    rows = {r["message_id"]: r for r in inbox.pending(after_ts=0.0)}
    assert rows[1]["sender_username"] == "RealHandle"   # @ stripped
    assert rows[1]["sender_name"] == "Ashe Display"
    assert rows[2]["sender_username"] is None
    assert rows[2]["sender_name"] == "No Handle Guy"


def test_allowed_chats_empty_when_unconfigured(monkeypatch):
    """No env, no state -> NO chats. The old @abstractdl_chat default
    silently re-attached readers to a retired chat (live incident
    2026-07-09: cross-chat ghost replies)."""
    import telegram_presence.inbox as tgi
    from telegram_presence import hooks

    monkeypatch.delenv("TELEGRAM_MENTIONS_CHAT", raising=False)
    monkeypatch.setattr(hooks, "_state_loader", lambda: {})
    assert tgi.allowed_chats() == []
    assert tgi.allowed_chat() == ""
