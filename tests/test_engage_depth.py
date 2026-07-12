"""Tests for the want/depth gate: does Rain actually want to answer, and how
deeply — plus delegate-by-default for substantive addressed questions, the
decision log, and burst-anchor selection."""
from __future__ import annotations

import json

from telegram_presence import engage as te


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
    row.update(fields)
    return row


def _cand(mid, snippet, *, addressed=True, kind="mention", sender=7, ts=100.0):
    return te.Candidate(mid, addressed, snippet, sender_id=sender,
                        sender_username=f"u{sender}", addressed_kind=kind, ts=ts)


# --- parsing want/depth ---

def test_parse_captures_want_and_depth():
    raw = ('[{"message_id":1,"action":"reply","text":"привет",'
           '"want":"yes","depth":"quick"}]')
    plans = te.parse_action_plan(raw)
    assert plans[0].want == "yes" and plans[0].depth == "quick"


def test_parse_normalizes_unknown_want_depth_to_empty():
    raw = ('[{"message_id":1,"action":"reply","text":"t",'
           '"want":"maybe","depth":"bottomless"}]')
    plans = te.parse_action_plan(raw)
    assert plans[0].want == "" and plans[0].depth == ""


# --- depth policy ---

def test_want_no_reply_is_dropped():
    plans = [te.ActionPlan(1, "reply", text="ответ из вежливости", want="no")]
    out, trace = te.apply_depth_policy(plans, cand_by_id={1: _cand(1, "hi?")},
                                       addressed_ids={1})
    assert out == []
    assert trace and trace[0]["mid"] == 1 and trace[0]["why"] == "want_no"


def test_deep_addressed_reply_converts_to_delegate_keeping_fallback_text():
    plans = [te.ActionPlan(1, "reply", text="черновик", depth="deep")]
    out, trace = te.apply_depth_policy(plans, cand_by_id={1: _cand(1, "как ты учишься?")},
                                       addressed_ids={1})
    assert out[0].action == "delegate" and out[0].text == "черновик"
    assert trace[0]["why"] == "depth_deep"


def test_substantive_addressed_question_auto_delegates():
    q = "Расскажи, как устроен твой цикл самоулучшения и что было сложнее всего? " * 3
    plans = [te.ActionPlan(1, "reply", text="коротко")]
    out, trace = te.apply_depth_policy(plans, cand_by_id={1: _cand(1, q)},
                                       addressed_ids={1})
    assert out[0].action == "delegate" and out[0].text == "коротко"
    assert trace[0]["why"] == "substantive"


def test_short_banter_stays_light_reply():
    plans = [te.ActionPlan(1, "reply", text="привет!", want="yes", depth="quick")]
    out, trace = te.apply_depth_policy(plans, cand_by_id={1: _cand(1, "как дела?")},
                                       addressed_ids={1})
    assert out[0].action == "reply" and trace == []


def test_self_selected_deep_reply_can_use_delegate():
    q = "очень длинный вопрос в общий эфир? " * 10
    plans = [te.ActionPlan(1, "reply", text="t", depth="deep")]
    out, _ = te.apply_depth_policy(plans, cand_by_id={1: _cand(1, q, addressed=False, kind="none")},
                                   addressed_ids=set())
    assert out[0].action == "delegate"


# --- validate: delegate over cap downgrades instead of dropping ---

def test_validate_delegate_over_cap_downgrades_to_reply():
    plans = [te.ActionPlan(i, "delegate", text=f"fallback {i}", reason="q")
             for i in (1, 2, 3)]
    out = te.validate_actions(plans, addressed_ids={1, 2, 3})
    delegates = [p for p in out if p.action == "delegate"]
    replies = [p for p in out if p.action == "reply"]
    assert len(delegates) == te.DELEGATE_PER_CYCLE
    assert [p.message_id for p in replies] == [3] and replies[0].text == "fallback 3"


def test_validate_keeps_delegate_fallback_text():
    out = te.validate_actions([te.ActionPlan(1, "delegate", text="draft", reason="q")],
                              addressed_ids={1})
    assert out[0].action == "delegate" and out[0].text == "draft"


def test_side_participant_may_choose_deep_composer():
    plan = te.ActionPlan(
        9, "delegate", reason="важная новая деталь",
        addressed_to="other", addressed_to_entity="@alice",
        want="yes", depth="deep",
    )
    out = te.validate_actions([plan], addressed_ids=set())
    assert out[0].action == "delegate"
    assert out[0].addressed_to == "other"


# --- resolve: composer failure falls back to the light draft ---

def test_resolve_falls_back_to_light_text_when_composer_fails():
    def boom(candidate, thread_text, chat=""):
        raise RuntimeError("llm down")
    plans = [te.ActionPlan(1, "delegate", text="светлый черновик", reason="q")]
    out = te._resolve_delegate_plans(plans, cand_by_id={1: _cand(1, "q?")},
                                     chat="@c", completed_actions=set(),
                                     compose_delegate=boom, history=[], own_rows=[])
    assert out and out[0].action == "reply" and out[0].text == "светлый черновик"
    assert out[0].delegated is False


def test_resolve_falls_back_when_no_composer_wired():
    plans = [te.ActionPlan(1, "delegate", text="черновик", reason="q")]
    out = te._resolve_delegate_plans(plans, cand_by_id={1: _cand(1, "q?")},
                                     chat="@c", completed_actions=set(),
                                     compose_delegate=None, history=[], own_rows=[])
    assert out and out[0].action == "reply" and out[0].text == "черновик"


# --- coalesce anchor: reply quotes the addressed message, not the burst tail ---

def test_coalesce_anchors_merged_burst_to_addressed_message():
    cands = [
        te.Candidate(1, True, "как ты устроена?", sender_id=7, sender_username="u7",
                     addressed_kind="mention", ts=100.0),
        te.Candidate(2, False, "ну то есть внутри", sender_id=7, sender_username="u7",
                     addressed_kind="none", ts=110.0),
    ]
    out = te.coalesce_candidates(cands)
    assert len(out) == 1 and out[0].message_id == 1


# --- decider prompt carries the want/depth contract ---

def test_decider_prompt_explains_want_and_depth():
    prompt = te.build_decider_prompt([_cand(1, "hi")])
    assert '"want"' in prompt and '"depth"' in prompt


# --- decision log ---

def test_cycle_writes_decision_log(tmp_path):
    st = {"telegram_engage_enabled": True, "autonomy_enabled": True,
          "owner_chat_id": 1, "telegram_engage": {},
          "telegram_mentions_chat": "@testchat"}
    packet = {"status": "ok",
              "matches": [{"message_id": 1, "snippet": "@rain hi", "sender_id": 7}],
              "recent": []}
    decider = lambda prompt: json.dumps([
        _assessment(1, "ignore", want="no", reason="не хочу отвечать")
    ])
    replies = []
    res = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(st), save_state=lambda s: None,
        fetch_candidates=lambda dr: packet,
        run_decider=decider,
        do_reply=lambda peer, mid, text: replies.append((peer, mid, text)) or True,
        do_react=lambda peer, mid, emoji: True,
        notify=lambda t: None, now=1000.0)
    assert replies == []
    path = tmp_path / "state" / "telegram_engage_decisions.jsonl"
    assert path.exists()
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert row["raw"][0]["want"] == "no"
    assert row["raw"][0]["action"] == "ignore"
    assert row["final"] == []
