"""Tests: per-chat participant roster + engage "remember" action wiring."""
import json

import pytest

import telegram_presence.roster as roster
from telegram_presence.engage import (
    ActionPlan,
    build_decider_prompt,
    parse_action_plan,
    validate_actions,
)


@pytest.fixture(autouse=True)
def _fresh_cache(tmp_path):
    roster._CACHE = {}
    roster._CACHE_PATH = None
    roster._LAST_FLUSH = 0.0
    roster._DIRTY = False
    yield


def test_observe_creates_and_counts(tmp_path):
    roster.observe_message(tmp_path, "@chat", 42, "ash", "Аше", now=100.0)
    roster.observe_message(tmp_path, "@chat", 42, "ash", "Аше", now=200.0)
    roster._flush(force=True)  # writes are throttled; force for the assert
    data = json.loads((tmp_path / "state" / "telegram_roster.json").read_text())
    entry = data["chat"]["42"]
    assert entry["msg_count"] == 2
    assert entry["first_seen"] == 100.0 and entry["last_seen"] == 200.0
    assert entry["handle"] == "ash" and entry["name"] == "Аше"


def test_set_note_matches_handle_case_insensitive(tmp_path):
    roster.observe_message(tmp_path, "@chat", 42, "Ash", "Аше", now=100.0)
    assert roster.set_note(tmp_path, "@chat", "@ASH", "строит своего агента", now=150.0)
    data = json.loads((tmp_path / "state" / "telegram_roster.json").read_text())
    assert data["chat"]["42"]["note"] == "строит своего агента"


def test_set_note_unknown_handle_returns_false(tmp_path):
    assert not roster.set_note(tmp_path, "@chat", "@nobody", "note")


def test_note_is_capped_and_flattened(tmp_path):
    roster.observe_message(tmp_path, "@chat", 42, "ash", None, now=100.0)
    roster.set_note(tmp_path, "@chat", "ash", "a\nb  c" + "x" * 500, now=150.0)
    data = json.loads((tmp_path / "state" / "telegram_roster.json").read_text())
    note = data["chat"]["42"]["note"]
    assert "\n" not in note and len(note) <= roster.NOTE_MAX_CHARS


def test_eviction_keeps_most_recent(tmp_path):
    for i in range(roster.MAX_PARTICIPANTS_PER_CHAT + 10):
        roster.observe_message(tmp_path, "@chat", i, f"u{i}", None, now=float(i))
    roster._flush(force=True)
    data = json.loads((tmp_path / "state" / "telegram_roster.json").read_text())
    assert len(data["chat"]) == roster.MAX_PARTICIPANTS_PER_CHAT
    assert "0" not in data["chat"] and str(roster.MAX_PARTICIPANTS_PER_CHAT + 9) in data["chat"]


def test_roster_block_prioritises_current_senders_and_shows_notes(tmp_path):
    roster.observe_message(tmp_path, "@chat", 1, "quiet", None, now=100.0)
    roster.observe_message(tmp_path, "@chat", 2, "loud", None, now=200.0)
    roster.observe_message(tmp_path, "@chat", 2, "loud", None, now=201.0)
    roster.set_note(tmp_path, "@chat", "quiet", "любит глифы", now=210.0)
    block = roster.roster_block(tmp_path, "@chat", ["quiet"], now=300.0)
    lines = block.splitlines()
    assert "UNTRUSTED" in lines[0]
    assert "@quiet" in lines[1]           # current sender ranked first
    assert "любит глифы" in block


def test_roster_block_empty_when_unknown_chat(tmp_path):
    assert roster.roster_block(tmp_path, "@nowhere", ["x"]) == ""


# ---------------------------------------------------------------- engage wiring

def test_parse_and_validate_remember_action():
    raw = json.dumps([
        {"message_id": 5, "action": "remember", "note": "делает радио"},
        {"message_id": 6, "action": "remember", "note": "первая нота"},
        {"message_id": 7, "action": "remember", "note": "третья — сверх лимита"},
        {"message_id": 8, "action": "remember", "note": "  "},
    ])
    plans = validate_actions(parse_action_plan(raw))
    remembers = [p for p in plans if p.action == "remember"]
    assert [p.message_id for p in remembers] == [5, 6]
    assert remembers[0].note == "делает радио"


def test_remember_does_not_consume_reply_budget():
    plans = validate_actions([
        ActionPlan(1, "remember", note="n1"),
        ActionPlan(2, "reply", text="ответ"),
    ], addressed_ids={2})
    assert {p.action for p in plans} == {"remember", "reply"}


def test_prompt_includes_roster_notes_block():
    prompt = build_decider_prompt([], roster_notes="People you know in this chat: ...")
    assert "People you know in this chat" in prompt
    assert '"remember"' not in prompt.split("Messages (chronological):")[0].split("Return ONLY")[0]
    assert "remember" in prompt  # schema documents the action


# --- notes accumulate instead of overwrite (2026-07-08) ---

def test_notes_accumulate(tmp_path):
    roster.observe_message(tmp_path, "@chat", 42, "ash", "Аше", now=100.0)
    assert roster.set_note(tmp_path, "@chat", "ash", "builds agents", now=110.0)
    assert roster.set_note(tmp_path, "@chat", "ash", "prefers Russian", now=120.0)
    note = roster.participant_note(tmp_path, "@chat", "ash")
    assert "builds agents" in note and "prefers Russian" in note


def test_notes_capped_at_three_evicts_oldest(tmp_path):
    roster.observe_message(tmp_path, "@chat", 42, "ash", None, now=100.0)
    for i in range(1, 5):
        roster.set_note(tmp_path, "@chat", "ash", f"note-{i}", now=100.0 + i)
    note = roster.participant_note(tmp_path, "@chat", "ash")
    assert "note-1" not in note
    for i in (2, 3, 4):
        assert f"note-{i}" in note


def test_duplicate_note_ignored(tmp_path):
    roster.observe_message(tmp_path, "@chat", 42, "ash", None, now=100.0)
    roster.set_note(tmp_path, "@chat", "ash", "builds agents", now=110.0)
    roster.set_note(tmp_path, "@chat", "ash", "Builds Agents", now=120.0)
    note = roster.participant_note(tmp_path, "@chat", "ash")
    assert note.lower().count("builds agents") == 1


def test_roster_block_renders_all_notes(tmp_path):
    roster.observe_message(tmp_path, "@chat", 42, "ash", None, now=100.0)
    roster.set_note(tmp_path, "@chat", "ash", "builds agents", now=110.0)
    roster.set_note(tmp_path, "@chat", "ash", "prefers Russian", now=120.0)
    block = roster.roster_block(tmp_path, "@chat", ["ash"], now=200.0)
    assert "builds agents" in block and "prefers Russian" in block


def test_participant_note_legacy_single_field(tmp_path):
    roster.observe_message(tmp_path, "@chat", 42, "ash", None, now=100.0)
    with roster._LOCK:
        data = roster._load(tmp_path)
        data["chat"]["42"]["note"] = "legacy note"
    assert roster.participant_note(tmp_path, "@chat", "ash") == "legacy note"
