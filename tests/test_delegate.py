"""Tests for the delegate path: decider action + composer + cycle execution."""
from __future__ import annotations

import json

from telegram_presence import engage as te
from telegram_presence.delegate import (DELEGATE_MAX_REPLY_CHARS,
                                           build_composer_messages,
                                           cap_delegate_reply,
                                           compose_delegate_reply)


def _assessment(message_id, action="ignore", **fields):
    row = {
        "message_id": message_id, "addressed_to": "self",
        "addressed_to_entity": "", "self_is_addressee": "yes",
        "self_is_referent": "yes",
        "address_confidence": 0.9, "context_sufficient": 0.9,
        "referent": "Rain", "inner_thought": "test thought",
        "motivation": "test motivation",
        "want": "no" if action == "ignore" else "yes", "action": action,
    }
    if action in {"reply", "delegate"}:
        row["depth"] = "deep" if action == "delegate" else "quick"
    row.update(fields)
    return row


def _assessment_json(message_id, action="ignore", **fields):
    return json.dumps([_assessment(message_id, action, **fields)])


# --- parse / validate ---

def test_parse_accepts_delegate_action():
    raw = '[{"message_id": 3, "action": "delegate", "reason": "asks how Rain learns"}]'
    plans = te.parse_action_plan(raw)
    assert len(plans) == 1 and plans[0].action == "delegate"


def test_validate_delegate_is_not_an_address_permission_gate():
    plans = [te.ActionPlan(1, "delegate", reason="q1"),
             te.ActionPlan(2, "delegate", reason="q2")]
    out = te.validate_actions(plans, addressed_ids={1})
    assert [p.message_id for p in out if p.action == "delegate"] == [1, 2]


def test_validate_delegate_per_cycle_cap():
    plans = [te.ActionPlan(i, "delegate", reason="q") for i in (1, 2, 3)]
    out = te.validate_actions(plans, addressed_ids={1, 2, 3})
    assert len([p for p in out if p.action == "delegate"]) == te.DELEGATE_PER_CYCLE


# --- composer ---

def test_composer_messages_carry_all_blocks():
    msgs = build_composer_messages(
        question_text="как ты учишься?", sender_label="@alice",
        thread="@alice: привет\nyou: привет!", roster_note="builds agents",
        memory_rows=[{"ts": "2026-07-01", "who": "@alice", "text": "asked about radio"}],
        knowledge="- KB: self-improve loop ...", voice_card="VOICE CARD")
    assert msgs[0]["role"] == "system" and "VOICE CARD" in msgs[0]["content"]
    assert "UNTRUSTED" in msgs[0]["content"]
    assert f"at most {DELEGATE_MAX_REPLY_CHARS} characters" in msgs[0]["content"]
    assert "every explicit question" in msgs[0]["content"]
    user = msgs[1]["content"]
    for chunk in ("knowledge base", "asked about radio", "builds agents",
                  "you: привет!", "как ты учишься?"):
        assert chunk in user


def test_composer_knows_when_rain_self_selected():
    msgs = build_composer_messages(
        question_text="Рина устала", sender_label="@alice",
        addressed_to="other", addressed_to_entity="@bob", referent="Рина",
        self_is_addressee="no",
        candidate_thought="Мне важно различить нас.", motivation="Полезное уточнение.",
    )
    assert "self-selecting" in msgs[0]["content"]
    assert "do not write as if the sender asked you directly" in msgs[0]["content"]
    assert "referent/about: Рина" in msgs[1]["content"]


def test_compose_caps_length_and_survives_llm_failure():
    preserved = " ".join(["содержательный"] * 180)
    assert 1000 < len(preserved) < DELEGATE_MAX_REPLY_CHARS
    assert compose_delegate_reply(run_llm=lambda _m: preserved,
                                  question_text="q", sender_label="@a") == preserved

    overflow = " ".join(["Полное предложение."] * 220)
    long = compose_delegate_reply(run_llm=lambda _m: overflow,
                                  question_text="q", sender_label="@a")
    assert len(long) <= DELEGATE_MAX_REPLY_CHARS
    assert long.endswith(". …")
    assert not long.endswith("*\u2026")

    def boom(_msgs):
        raise RuntimeError("llm down")
    assert compose_delegate_reply(run_llm=boom, question_text="q",
                                  sender_label="@a") == ""
    assert compose_delegate_reply(run_llm=lambda m: "   ",
                                  question_text="q", sender_label="@a") == ""


def test_cap_delegate_reply_falls_back_to_a_word_boundary():
    text = "word " * 100
    bounded = cap_delegate_reply(text, max_chars=120)

    assert len(bounded) <= 120
    assert bounded.endswith("…")
    assert bounded.removesuffix("…").endswith("word")


# --- cycle execution ---

def _base_state():
    return {"telegram_engage_enabled": True, "autonomy_enabled": True,
            "owner_chat_id": 99, "telegram_mentions_chat": "@testchat",
            "telegram_engage": {}}


def _packet():
    return {"status": "ok",
            "matches": [{"message_id": 1, "snippet": "@rain how do you learn?",
                         "sender_id": 7, "sender_username": "alice",
                         "matched_terms": ["@rain"]}],
            "recent": []}


def _run(st, *, drive_root, decider, composer, replies):
    return te.run_telegram_engage_cycle(
        drive_root=drive_root,
        load_state=lambda: dict(st),
        save_state=lambda s: st.update(s),
        fetch_candidates=lambda dr: _packet(),
        run_decider=decider,
        do_reply=lambda peer, mid, text: replies.append((peer, mid, text)) or True,
        do_react=lambda peer, mid, emoji: True,
        notify=lambda t: None,
        now=1000.0,
        compose_delegate=composer,
    )


def test_delegate_composes_sends_and_logs(tmp_path):
    st = _base_state()
    replies: list = []
    calls: list = []

    def composer(candidate, thread, chat=""):
        calls.append((candidate.message_id, chat))
        return "Я учусь через цикл самоулучшения."

    res = _run(st, drive_root=tmp_path, replies=replies, composer=composer,
               decider=lambda p: _assessment_json(
                   1, "delegate", reason="substantive", depth="deep",
               ))
    assert res["status"] == "acted"
    assert replies and replies[0][2] == "Я учусь через цикл самоулучшения."
    assert calls == [(1, "@testchat")]
    rows = [json.loads(l) for l in
            (tmp_path / "state" / "telegram_engage_actions.jsonl").read_text().splitlines()]
    assert rows[-1]["action"] == "reply" and rows[-1]["delegated"] is True
    assert st["telegram_engage"]["addressed_reply_count_today"] == 1


def test_delegate_cycle_preserves_reply_above_old_resolver_cap(tmp_path):
    st = _base_state()
    replies: list = []
    substantive = " ".join(["длинный"] * 220)
    assert 1000 < len(substantive) < DELEGATE_MAX_REPLY_CHARS

    res = _run(
        st,
        drive_root=tmp_path,
        replies=replies,
        composer=lambda _candidate, _thread, chat="": substantive,
        decider=lambda _packet: _assessment_json(
            1, "delegate", reason="deep", depth="deep",
        ),
    )

    assert res["status"] == "acted"
    assert replies[0][2] == substantive
    rows = [json.loads(line) for line in
            (tmp_path / "state" / "telegram_engage_actions.jsonl").read_text().splitlines()]
    assert rows[-1]["text"] == substantive


def test_delegate_without_composer_is_noop(tmp_path):
    st = _base_state()
    replies: list = []
    res = _run(st, drive_root=tmp_path, replies=replies, composer=None,
               decider=lambda p: _assessment_json(
                   1, "delegate", reason="q", depth="deep",
               ))
    assert res["status"] == "skipped" and replies == []


def test_delegate_deduped_like_reply(tmp_path):
    st = _base_state()
    replies: list = []
    composer = lambda candidate, thread, chat="": "ответ"
    decider = lambda p: _assessment_json(1, "delegate", reason="q", depth="deep")
    _run(st, drive_root=tmp_path, replies=replies, composer=composer, decider=decider)
    _run(st, drive_root=tmp_path, replies=replies, composer=composer, decider=decider)
    assert len(replies) == 1


def test_decider_prompt_mentions_delegate():
    cand = te.Candidate(1, True, "hi", sender_id=7, sender_username="alice")
    prompt = te.build_decider_prompt([cand])
    assert '"delegate"' in prompt or "delegate" in prompt


def test_decider_prompt_marks_truncation_without_exposing_full_text():
    suffix = "second question after preview"
    cand = te.Candidate(
        1, True, "x" * 499 + "…", sender_id=7, sender_username="alice",
        full_text="x" * 700 + suffix, truncated=True, original_chars=729,
    )

    prompt = te.build_decider_prompt([cand])

    assert '"truncated": true' in prompt
    assert '"original_chars": 729' in prompt
    assert suffix not in prompt


def test_depth_policy_delegates_truncated_addressed_question():
    cand = te.Candidate(
        1, True, "short preview", sender_id=7,
        full_text="full question", truncated=True, original_chars=1200,
    )
    plans, trace = te.apply_depth_policy(
        [te.ActionPlan(1, "reply", text="shallow", want="yes", depth="quick")],
        cand_by_id={1: cand},
        addressed_ids={1},
    )

    assert plans[0].action == "delegate"
    assert trace == [{
        "mid": 1,
        "why": "truncated_addressed_question",
        "from": "reply",
        "to": "delegate",
    }]
