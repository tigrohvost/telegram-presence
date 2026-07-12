"""Regression coverage for multi-party social perception and Rain's agency.

These cases deliberately separate ABOUT Rain, ADDRESSED TO Rain, and Rain's
own choice to enter a conversational floor.  Transport metadata is evidence;
it is not the semantic verdict or a speech permission bit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from telegram_presence import engage as te
from telegram_presence.thread import build_threads


@dataclass
class SceneCandidate:
    message_id: int
    sender_id: Optional[int]
    addressed: bool = False
    topic_id: Optional[int] = None
    ts: float = 0.0


def _row(mid: int, sender: int, text: str, ts: float, *,
         username: str, reply_to: Optional[int] = None,
         topic_id: Optional[int] = None) -> dict:
    return {
        "message_id": mid,
        "sender_id": sender,
        "sender_username": username,
        "snippet": text,
        "ts": ts,
        "reply_to_msg_id": reply_to,
        "topic_id": topic_id,
        "matched_terms": [],
    }


def test_flat_scene_includes_other_speakers_for_pronoun_resolution():
    spool = [
        _row(1, 10, "Я создала Рину — отдельного чатбота", 100, username="alice"),
        _row(2, 20, "а у неё что случилось?", 110, username="bob"),
    ]
    scene = build_threads(
        spool, [], [SceneCandidate(2, sender_id=20, ts=110)],
    )[2]
    assert "Рину" in scene and "@alice" in scene


def test_unaddressed_reply_scene_carries_the_rina_correction_chain():
    spool = [
        _row(14357, 1, "Как-то печально за Рину", 100, username="ashe"),
        _row(14359, 1, "я не про тебя, а про чатбота Асуры", 110,
             username="ashe", reply_to=14358, topic_id=14357),
        _row(14373, 2, "она почему-то её имя принимает за обращение к себе", 120,
             username="asura", reply_to=14359, topic_id=14357),
    ]
    scene = build_threads(
        spool, [], [SceneCandidate(14373, sender_id=2, addressed=False,
                                   topic_id=14357, ts=120)],
    )[14373]
    assert "печально за Рину" in scene  # thread root has topic_id=None
    assert "не про тебя" in scene
    assert "user=@ashe" in scene and "reply_to_mid=14358" in scene


def test_scene_never_leaks_future_turns_into_the_decision():
    spool = [
        _row(1, 10, "before", 100, username="alice"),
        _row(2, 20, "current", 110, username="bob"),
        _row(3, 30, "future answer", 120, username="carol"),
    ]
    scene = build_threads(
        spool, [], [SceneCandidate(2, sender_id=20, ts=110)],
    )[2]
    assert "before" in scene
    assert "future answer" not in scene


def test_parse_social_read_keeps_orthogonal_axes_thought_and_motivation():
    raw = json.dumps([{
        "message_id": 7,
        "addressed_to": "other",
        "addressed_to_entity": "@alice",
        "self_is_addressee": "no",
        "self_is_referent": "yes",
        "address_confidence": 0.82,
        "context_sufficient": 0.74,
        "referent": "Rain",
        "inner_thought": "Я понимаю, почему это выглядит как путаница.",
        "motivation": "Могу пояснить, но люди уже разобрались без меня.",
        "want": "no",
        "action": "ignore",
        "reason": "обо мне, но говорят не мне",
    }], ensure_ascii=False)
    plan = te.parse_action_plan(raw)[0]
    assert plan.addressed_to == "other"
    assert plan.addressed_to_entity == "@alice"
    assert plan.self_is_addressee == "no"
    assert plan.self_is_referent == "yes"
    assert plan.address_confidence == 0.82 and plan.context_sufficient == 0.74
    assert plan.referent == "Rain"
    assert "пояснить" in plan.motivation and plan.want == "no"


def test_transport_fast_lane_never_uses_semantic_address_axis():
    candidates = {
        1: te.Candidate(1, True, "Rain вчера сказала", addressed_kind="name_only"),
        2: te.Candidate(2, False, "Рейн, ответь", addressed_kind="none"),
        3: te.Candidate(3, True, "Иван, это тебе", addressed_kind="reply"),
        4: te.Candidate(4, True, "@rain привет", addressed_kind="mention"),
    }
    assert te.transport_addressed_ids(cand_by_id=candidates) == {3, 4}


def test_operator_pause_exception_requires_transport_and_social_demand():
    candidates = {
        1: te.Candidate(1, True, "@rain was quoted to @alice", addressed_kind="mention"),
    }
    about_plan = te.ActionPlan(
        1, "reply", addressed_to="other", self_is_addressee="no", want="yes",
    )
    direct_plan = te.ActionPlan(
        1, "reply", addressed_to="group", self_is_addressee="yes", want="yes",
    )

    assert te.confirmed_demand_ids([about_plan], cand_by_id=candidates) == set()
    assert te.confirmed_demand_ids([direct_plan], cand_by_id=candidates) == {1}


def test_open_autonomy_does_not_forbid_side_participant_choice():
    plans = [te.ActionPlan(
        1, "reply", text="мне есть что добавить", addressed_to="other",
        inner_thought="важная новая деталь", motivation="она изменит вывод", want="yes",
    )]
    out, trace = te.apply_autonomy_boundary(
        plans, addressed_ids=set(), proactive_ok=True,
    )
    assert out == plans and trace == []


def _run_name_only(tmp_path, *, addressed_to: str) -> list[tuple]:
    state = {
        "telegram_engage_enabled": True,
        "autonomy_enabled": False,
        "owner_chat_id": 1,
        "telegram_mentions_chat": "@chat",
        "telegram_engage": {},
    }
    packet = {
        "status": "ok",
        "matches": [{
            "message_id": 9, "sender_id": 7,
            "snippet": "Рейн вчера говорила об этом",
            "matched_terms": ["рейн"],
        }],
        "recent": [],
    }
    raw = json.dumps([{
        "message_id": 9,
        "addressed_to": addressed_to,
        "addressed_to_entity": "",
        "self_is_addressee": "yes" if addressed_to == "self" else "no",
        "self_is_referent": "yes",
        "address_confidence": 0.9,
        "context_sufficient": 0.9,
        "referent": "Rain",
        "inner_thought": "могу ответить",
        "motivation": "проверка",
        "want": "yes",
        "depth": "quick",
        "action": "reply",
        "text": "ответ",
        "reason": "test",
    }])
    replies: list[tuple] = []
    te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(state),
        save_state=lambda value: state.update(value),
        fetch_candidates=lambda root: packet,
        run_decider=lambda prompt: raw,
        do_reply=lambda *args: replies.append(args) or True,
        do_react=lambda *args: True,
        notify=lambda text: None,
        now=1000.0,
    )
    return replies


def test_bare_name_does_not_bypass_paused_proactive_presence(tmp_path):
    assert _run_name_only(tmp_path / "about", addressed_to="other") == []
    assert _run_name_only(tmp_path / "to", addressed_to="self") == []


def test_valid_silence_is_an_explicit_logged_assessment(tmp_path):
    state = {
        "telegram_engage_enabled": True,
        "autonomy_enabled": True,
        "owner_chat_id": 1,
        "telegram_mentions_chat": "@chat",
        "telegram_engage": {},
    }
    packet = {
        "status": "ok", "matches": [],
        "recent": [{"message_id": 11, "sender_id": 7, "snippet": "люди беседуют"}],
    }
    te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(state), save_state=lambda value: state.update(value),
        fetch_candidates=lambda root: packet,
        run_decider=lambda prompt: json.dumps([{
            "message_id": 11, "addressed_to": "other",
            "addressed_to_entity": "@people", "self_is_addressee": "no",
            "self_is_referent": "no",
            "address_confidence": 0.8, "context_sufficient": 0.9,
            "referent": "their conversation", "inner_thought": "",
            "motivation": "They are talking naturally without needing me.",
            "want": "no", "action": "ignore", "reason": "graceful silence",
        }]),
        do_reply=lambda *args: True, do_react=lambda *args: True,
        notify=lambda text: None, now=1000.0,
    )
    row = json.loads(
        (tmp_path / "state" / "telegram_engage_decisions.jsonl")
        .read_text(encoding="utf-8").splitlines()[-1]
    )
    assert row["candidate_message_ids"] == [11]
    assert row["raw"][0]["action"] == "ignore" and row["final"] == []
    assert "inner_thought" not in row["raw"][0]
    assert row["raw"][0]["inner_thought_present"] is False
    assert len(row["raw"][0]["inner_thought_sha256"]) == 64


def _assessment(mid: int, action: str = "ignore", **overrides) -> dict:
    row = {
        "message_id": mid,
        "addressed_to": "unclear",
        "addressed_to_entity": "",
        "self_is_addressee": "unclear",
        "self_is_referent": "unclear",
        "address_confidence": 0.5,
        "context_sufficient": 0.5,
        "referent": "unclear",
        "inner_thought": "possible thought",
        "motivation": "bounded test decision",
        "want": "no" if action == "ignore" else "yes",
        "action": action,
    }
    if action == "reply":
        row.update({"text": "answer", "depth": "quick"})
    row.update(overrides)
    return row


def test_strict_social_contract_requires_exact_unique_coverage():
    valid = json.dumps([_assessment(1), _assessment(2)])
    assert te._action_plan_payload_valid(
        valid, allowed_message_ids={1, 2}, required_message_ids={1, 2},
    )
    assert not te._action_plan_payload_valid(
        "[]", allowed_message_ids={1}, required_message_ids={1},
    )
    assert not te._action_plan_payload_valid(
        json.dumps([_assessment(1)]),
        allowed_message_ids={1, 2}, required_message_ids={1, 2},
    )
    assert not te._action_plan_payload_valid(
        json.dumps([_assessment(1), _assessment(1)]),
        allowed_message_ids={1}, required_message_ids={1},
    )
    missing_motivation = _assessment(1)
    missing_motivation.pop("motivation")
    assert not te._action_plan_payload_valid(
        json.dumps([missing_motivation]),
        allowed_message_ids={1}, required_message_ids={1},
    )
    contradictory_ignore = _assessment(1, want="yes")
    assert not te._action_plan_payload_valid(
        json.dumps([contradictory_ignore]),
        allowed_message_ids={1}, required_message_ids={1},
    )
    contradictory_react = _assessment(1, "react", want="no", emoji="👍")
    assert not te._action_plan_payload_valid(
        json.dumps([contradictory_react]),
        allowed_message_ids={1}, required_message_ids={1},
    )
    missing_depth = _assessment(1, "delegate")
    assert te._action_plan_payload_valid(
        json.dumps([missing_depth]),
        allowed_message_ids={1}, required_message_ids={1},
    )
    assert te.parse_action_plan(json.dumps([missing_depth]))[0].depth == "deep"


def test_optional_memory_is_a_side_effect_of_one_social_assessment():
    raw = _assessment(
        1, "reply", memory={
            "kind": "entity", "entity": "Рина", "note": "Отдельный чатбот Асуры",
        },
    )
    assert te._action_plan_payload_valid(
        json.dumps([raw], ensure_ascii=False),
        allowed_message_ids={1}, required_message_ids={1},
    )
    expanded = te.expand_memory_side_effects(
        te.parse_action_plan(json.dumps([raw], ensure_ascii=False))
    )
    assert [plan.action for plan in expanded] == ["reply", "remember_entity"]
    assert expanded[1].entity == "Рина"


def test_empty_optional_memory_object_is_treated_as_absent():
    raw = _assessment(1, memory={})
    assert te._action_plan_payload_valid(
        json.dumps([raw]),
        allowed_message_ids={1}, required_message_ids={1},
    )
    plan = te.parse_action_plan(json.dumps([raw]))[0]
    assert plan.memory_kind == "" and plan.note == ""


def test_wait_is_bounded_and_expires_to_silence():
    entry: dict = {}
    candidates = [te.Candidate(1, False, "moving floor", spool_seq=7)]
    wait = [te.ActionPlan(1, "wait", want="yes")]

    for expected_count in (1, 2):
        plans, held, trace = te.apply_wait_policy(
            wait, topic_entry=entry, candidates=candidates,
        )
        assert plans == [] and held == {1}
        assert trace[0]["attempts"] == expected_count

    plans, held, trace = te.apply_wait_policy(
        wait, topic_entry=entry, candidates=candidates,
    )
    assert plans == [] and held == set()
    assert trace == [{"mid": 1, "why": "wait_expired_to_silence", "attempts": 3}]
    assert "social_wait_anchor" not in entry


def test_wait_holds_optional_memory_until_context_is_sufficient(tmp_path):
    state = {
        "telegram_engage_enabled": True, "autonomy_enabled": True,
        "owner_chat_id": 1, "telegram_mentions_chat": "@chat",
        "telegram_engage": {},
    }
    packet = {
        "status": "ok", "max_seq": 1, "max_ts": 100.0,
        "matches": [],
        "recent": [{
            "message_id": 1, "spool_seq": 1, "ts": 100.0,
            "sender_id": 7, "snippet": "подождите, я дополню",
        }],
    }
    raw = _assessment(
        1, "wait", memory={
            "kind": "entity", "entity": "Рина", "note": "отдельный чатбот",
        },
    )

    result = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(state), save_state=lambda value: state.update(value),
        fetch_candidates=lambda *_args, **_kwargs: packet,
        run_decider=lambda _prompt: json.dumps([raw], ensure_ascii=False),
        do_reply=lambda *args: True, do_react=lambda *args: True,
        notify=lambda _text: None, now=1000.0,
    )

    from telegram_presence.roster import list_entities
    assert result["reason"] == "topic work deferred"
    assert list_entities(tmp_path, "@chat") == []


def test_new_turn_in_same_topic_cancels_send_for_reconsideration(tmp_path):
    state = {
        "telegram_engage_enabled": True,
        "autonomy_enabled": True,
        "owner_chat_id": 1,
        "telegram_mentions_chat": "@chat",
        "telegram_engage": {},
    }
    initial = {
        "status": "ok", "max_ts": 100.0, "max_seq": 1,
        "matches": [{
            "message_id": 1, "spool_seq": 1, "ts": 100.0,
            "sender_id": 7, "snippet": "@rain привет?", "matched_terms": ["@rain"],
        }],
        "recent": [],
    }
    newer = {
        "status": "ok", "max_ts": 101.0, "max_seq": 2,
        "matches": [],
        "recent": [{
            "message_id": 2, "spool_seq": 2, "ts": 101.0,
            "sender_id": 8, "snippet": "секунду, я дополню", "matched_terms": [],
        }],
    }
    fetches = 0

    def fetch(_root, **_kwargs):
        nonlocal fetches
        fetches += 1
        return initial if fetches == 1 else newer

    replies: list[tuple] = []
    result = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(state), save_state=lambda value: state.update(value),
        fetch_candidates=fetch,
        run_decider=lambda _prompt: json.dumps([_assessment(
            1, "reply", memory={
                "kind": "entity", "entity": "Рина", "note": "отдельный чатбот",
            },
        )]),
        do_reply=lambda *args: replies.append(args) or True,
        do_react=lambda *args: True, notify=lambda _text: None, now=1000.0,
    )

    assert result["status"] == "skipped" and replies == []
    assert result["reason"] == "topic work deferred"
    decision = json.loads(
        (tmp_path / "state" / "telegram_engage_decisions.jsonl")
        .read_text(encoding="utf-8").splitlines()[-1]
    )
    assert any(row.get("why") == "scene_changed_before_send"
               for row in decision["trace"])
    from telegram_presence.roster import list_entities
    assert list_entities(tmp_path, "@chat") == []


def test_memory_companion_is_not_reapplied_while_reply_stays_deferred(tmp_path):
    day = te._day_key(1000.0)
    state = {
        "telegram_engage_enabled": True, "autonomy_enabled": True,
        "owner_chat_id": 1, "telegram_mentions_chat": "@chat",
        "telegram_engage": {
            "day_key": day,
            "addressed_reply_count_today": te.ENGAGE_ADDRESSED_REPLY_DAILY_CAP,
            "per_chat": {
                "@chat": {
                    "day_key": day, "reply_count": 0,
                    "addressed_count": te.PER_CHAT_ADDRESSED_REPLY_DAILY_CAP,
                },
            },
        },
    }
    packet = {
        "status": "ok", "max_seq": 1, "max_ts": 100.0,
        "matches": [{
            "message_id": 1, "spool_seq": 1, "ts": 100.0,
            "sender_id": 7, "snippet": "@rain вопрос", "matched_terms": ["@rain"],
        }],
        "recent": [],
    }
    raw = json.dumps([_assessment(
        1, "reply", addressed_to="self", memory={
            "kind": "entity", "entity": "Рина", "note": "отдельный чатбот",
        },
    )], ensure_ascii=False)

    def cycle(now):
        return te.run_telegram_engage_cycle(
            drive_root=tmp_path,
            load_state=lambda: dict(state), save_state=lambda value: state.update(value),
            fetch_candidates=lambda *_args, **_kwargs: packet,
            run_decider=lambda _prompt: raw,
            do_reply=lambda *args: True, do_react=lambda *args: True,
            notify=lambda _text: None, now=now,
        )

    first = cycle(1000.0)
    second = cycle(1001.0)
    action_rows = [
        json.loads(line)
        for line in (tmp_path / te.ACTION_LOG_REL).read_text(encoding="utf-8").splitlines()
    ]
    memory_rows = [row for row in action_rows if row["action"] == "remember_entity"]
    assert first["applied_side_effects"] == 1
    assert second["applied_side_effects"] == 0
    assert len(memory_rows) == 1


def test_send_fails_closed_when_fresh_scene_cannot_be_verified(tmp_path):
    state = {
        "telegram_engage_enabled": True, "autonomy_enabled": True,
        "owner_chat_id": 1, "telegram_mentions_chat": "@chat",
        "telegram_engage": {},
    }
    packet = {
        "status": "ok", "max_seq": 1, "max_ts": 100.0,
        "matches": [{
            "message_id": 1, "spool_seq": 1, "ts": 100.0,
            "sender_id": 7, "snippet": "@rain привет", "matched_terms": ["@rain"],
        }],
        "recent": [],
    }
    calls = 0

    def fetch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return packet
        raise OSError("spool temporarily unavailable")

    replies: list[tuple] = []
    result = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(state), save_state=lambda value: state.update(value),
        fetch_candidates=fetch,
        run_decider=lambda _prompt: json.dumps([_assessment(
            1, "reply", addressed_to="self", self_is_addressee="yes",
        )]),
        do_reply=lambda *args: replies.append(args) or True,
        do_react=lambda *args: True, notify=lambda _text: None, now=1000.0,
    )

    assert replies == [] and result["reason"] == "topic work deferred"
    decision = json.loads(
        (tmp_path / te.DECISION_LOG_REL).read_text(encoding="utf-8").splitlines()[-1]
    )
    assert any(row.get("why") == "scene_revalidation_unavailable"
               for row in decision["trace"])


def test_prompt_makes_perception_and_agency_independent():
    prompt = te.build_decider_prompt([
        te.Candidate(1, True, "Rain вчера сказала...", addressed_kind="name_only"),
    ])
    assert "not a verdict and not permission to speak" in prompt
    assert "ABOUT you but addressed to another person" in prompt
    assert "never forces you to answer" in prompt
    assert "never forbids you" in prompt
    assert "self_is_referent" in prompt and "context_sufficient" in prompt
    assert "self_is_addressee" in prompt
    assert "meaning demonstrations, not keyword rules" in prompt
    assert "socially expects Rain to answer" in prompt
    assert "inner_thought" in prompt and "motivation" in prompt
    assert "will be downgraded" not in prompt
    assert '"addressed": true' not in prompt.lower()


def test_wait_can_honestly_hold_an_unformed_desire():
    raw = _assessment(1, "wait", want="no")
    assert te._action_plan_payload_valid(
        json.dumps([raw]),
        allowed_message_ids={1}, required_message_ids={1},
    )
