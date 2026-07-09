"""Tests for the realtime group-mention inbox (no LLM, untrusted text)."""
import json

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
