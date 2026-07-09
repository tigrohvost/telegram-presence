"""C: give the group decider thread continuity — chronological order, a
participant roster, and Rain's own recent replies as read-only context."""
import json
from telegram_presence import engage as te


def _c(mid, **kw):
    return te.Candidate(message_id=mid, addressed=kw.get("addressed", False),
                        snippet=kw.get("snippet", "x"),
                        sender_username=kw.get("u"), sender_name=kw.get("n"))


def test_prompt_orders_messages_chronologically():
    prompt = te.build_decider_prompt([_c(30), _c(10), _c(20)])
    p10, p20, p30 = (prompt.find('"message_id": 10'),
                     prompt.find('"message_id": 20'),
                     prompt.find('"message_id": 30'))
    assert -1 < p10 < p20 < p30


def test_prompt_has_participant_roster():
    prompt = te.build_decider_prompt([_c(1, u="alice", n="Alice"), _c(2, u="bob"), _c(3, u="alice")])
    assert "Participants" in prompt
    roster_line = next(l for l in prompt.splitlines() if l.startswith("Participants"))
    assert "@alice" in roster_line and "Alice" in roster_line and "@bob" in roster_line
    assert roster_line.count("@alice") == 1  # deduped in the roster


def test_prompt_includes_own_recent_context():
    prompt = te.build_decider_prompt([_c(1, u="alice")], own_recent=["earlier I said hello"])
    assert "earlier I said hello" in prompt


def test_recent_own_replies_reads_action_log(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    (d / "telegram_engage_actions.jsonl").write_text(
        json.dumps({"ts": 1, "chat": "@c", "action": "reply", "message_id": 5, "text": "first"}) + "\n" +
        json.dumps({"ts": 2, "chat": "@c", "action": "react", "message_id": 6, "emoji": "🔥"}) + "\n" +
        json.dumps({"ts": 3, "chat": "@other", "action": "reply", "message_id": 7, "text": "elsewhere"}) + "\n" +
        json.dumps({"ts": 4, "chat": "@c", "action": "reply", "message_id": 8, "text": "second"}) + "\n",
        encoding="utf-8")
    assert te._recent_own_replies(tmp_path, "@c", limit=5) == ["first", "second"]
