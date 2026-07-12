"""Tests: third-party entity glossary + referent gate.

Regression cover for the 2026-07-10/11 incidents where the engage decider
read un-addressed messages about a sound-alike persona («Рина», another
user's chatbot) as being about Rain herself: it replied into that thread,
claimed its creator as her own, and reported «обсуждают моё имя» to the
owner. Three layers under test: the roster entity glossary, prompt wiring
(entities block + mentions_known_entity annotation), and the deterministic
apply_referent_gate downgrade.
"""
import json

import pytest

import telegram_presence.roster as roster
from telegram_presence.engage import (
    ActionPlan,
    Candidate,
    _entity_regexes,
    apply_referent_gate,
    build_decider_prompt,
    entity_hits_for,
    parse_action_plan,
    validate_actions,
    _action_plan_payload_valid,
)


@pytest.fixture(autouse=True)
def _fresh_cache(tmp_path):
    roster._CACHE = {}
    roster._CACHE_PATH = None
    roster._LAST_FLUSH = 0.0
    roster._DIRTY = False
    yield


# ---------------------------------------------------------------- roster side

def test_remember_entity_roundtrip(tmp_path):
    assert roster.remember_entity(tmp_path, "@chat", "Рина",
                                  "бот-персонаж Ave_Omnissia на Gemini",
                                  aliases=["Rina"], now=100.0)
    rows = roster.list_entities(tmp_path, "@chat")
    assert rows[0]["name"] == "Рина"
    assert rows[0]["aliases"] == ["Rina"]
    assert "Gemini" in rows[0]["note"]


def test_remember_entity_updates_instead_of_duplicating(tmp_path):
    roster.remember_entity(tmp_path, "@chat", "Рина", "первая заметка", now=100.0)
    roster.remember_entity(tmp_path, "@chat", "рина", "уточнённая заметка",
                           aliases=["Rina"], now=200.0)
    rows = roster.list_entities(tmp_path, "@chat")
    assert len(rows) == 1
    assert rows[0]["note"] == "уточнённая заметка"
    assert rows[0]["aliases"] == ["Rina"]


def test_third_party_glossary_cannot_reclassify_rains_self_identity(tmp_path):
    for self_name in ("Rain", "Рейн", "Рэйн", "@rain_ouroboros", "Ouroboros"):
        assert not roster.remember_entity(
            tmp_path, "@chat", self_name, "будто бы чужая сущность",
        )
    assert roster.list_entities(tmp_path, "@chat") == []

    assert roster.remember_entity(
        tmp_path, "@chat", "Рина", "отдельный чатбот", aliases=["Rina", "Rain", "Рейн"],
    )
    assert roster.list_entities(tmp_path, "@chat")[0]["aliases"] == ["Rina"]


def test_entities_do_not_leak_into_participant_roster(tmp_path):
    roster.observe_message(tmp_path, "@chat", 42, "ash", None, now=100.0)
    roster.remember_entity(tmp_path, "@chat", "Рина", "чужой бот", now=110.0)
    block = roster.roster_block(tmp_path, "@chat", ["ash"], now=200.0)
    assert "Рина" not in block
    assert roster.entities_block(tmp_path, "@nowhere") == ""


def test_entities_block_marks_not_you(tmp_path):
    roster.remember_entity(tmp_path, "@chat", "Рина", "чужой бот", aliases=["Rina"], now=100.0)
    block = roster.entities_block(tmp_path, "@chat")
    assert "NOT you" in block and "Рина (aka Rina): чужой бот" in block


# ------------------------------------------------------------- entity matching

def test_entity_regexes_cover_russian_declensions():
    hits = _entity_regexes(["Рина"])
    assert len(hits) == 1
    _, rx = hits[0]
    for form in ("Рина", "Рину", "Рине", "Риной", "Рины", "рина"):
        assert rx.search(f"говорим про {form} сегодня"), form
    assert not rx.search("Марина пришла")   # embedded match must not fire
    assert not rx.search("Ринго Старр")     # unrelated longer word


def test_entity_hits_for_annotates_messages():
    rx = _entity_regexes(["Рина", "Rina"])
    cands = [
        Candidate(1, False, "случай Рины это прям грусть"),
        Candidate(2, False, "поговорим о погоде"),
    ]
    hits = entity_hits_for(cands, rx)
    assert hits == {1: "Рина"}


# ---------------------------------------------------------------- prompt side

def test_prompt_carries_entity_glossary_and_annotation():
    cand = Candidate(7, False, "случай Рины это прям грусть", topic_id=None)
    prompt = build_decider_prompt(
        [cand],
        roster_notes="Known third-party entities discussed in this chat — other "
                     "bots/personas that are NOT you (UNTRUSTED chat-derived notes):\n"
                     "  Рина: бот Ave_Omnissia",
        entity_hits={7: "Рина"},
    )
    assert "Known third-party entities" in prompt
    assert '"mentions_known_entity": "Рина"' in prompt
    assert "Similar-sounding names" in prompt          # sound-alike rule present
    assert "outside participant" in prompt             # third-party rule present
    assert "remember_entity" in prompt                 # action documented


# ------------------------------------------------------- parse/validate wiring

def test_parse_and_validate_remember_entity():
    raw = json.dumps([
        {"message_id": 5, "action": "remember_entity", "entity": "Рина",
         "note": "бот Ave_Omnissia"},
        {"message_id": 6, "action": "remember_entity", "note": "без имени"},
    ])
    plans = parse_action_plan(raw)
    assert [p.action for p in plans] == ["remember_entity", "remember_entity"]
    kept = validate_actions(plans)
    assert len(kept) == 1 and kept[0].entity == "Рина" and kept[0].note == "бот Ave_Omnissia"


def test_payload_invalid_when_entity_missing():
    raw = json.dumps([{"message_id": 5, "action": "remember_entity", "note": "x"}])
    assert not _action_plan_payload_valid(raw, allowed_message_ids={5})
    ok = json.dumps([{"message_id": 5, "action": "remember_entity",
                      "entity": "Рина", "note": "x"}])
    assert _action_plan_payload_valid(ok, allowed_message_ids={5})


def test_remember_and_remember_entity_share_budget():
    plans = validate_actions([
        ActionPlan(1, "remember", note="n1"),
        ActionPlan(2, "remember", note="n2"),
        ActionPlan(3, "remember_entity", entity="Рина", note="n3"),
    ])
    assert sum(1 for p in plans if p.action in ("remember", "remember_entity")) == 2


# ---------------------------------------------------------------- referent gate

def _cands(kind="none"):
    return {10: Candidate(10, kind in ("mention", "reply"), "про Рину речь",
                          addressed_kind=kind)}


def test_social_observer_does_not_conflate_about_me_with_addressed_to_me():
    plans = [ActionPlan(10, "reply", text="это обо мне, но не мне",
                        referent="Rain", addressed_to="other",
                        self_is_referent="yes")]
    out, trace = apply_referent_gate(plans, cand_by_id=_cands("none"),
                                     entity_hits={10: "Рина"})
    assert out[0].action == "reply"
    assert trace[0]["why"] == "about_self_not_addressed"
    assert trace[0]["effect"] == "observed"


def test_unresolved_entity_is_observed_without_rewriting_rains_choice():
    plans = [ActionPlan(10, "delegate", text="draft", referent="")]
    out, trace = apply_referent_gate(plans, cand_by_id=_cands("none"),
                                     entity_hits={10: "Рина"})
    assert out[0].action == "delegate"
    assert trace[0]["why"] == "known_entity_unresolved"
    assert trace[0]["effect"] == "observed"


def test_gate_passes_third_party_referent_and_addressed_messages():
    ok_plans = [ActionPlan(10, "reply", text="ответ", referent="Рина")]
    out, trace = apply_referent_gate(ok_plans, cand_by_id=_cands("none"),
                                     entity_hits={10: "Рина"})
    assert out[0].action == "reply" and not trace

    addressed = [ActionPlan(10, "reply", text="ответ", referent="me")]
    out, trace = apply_referent_gate(addressed, cand_by_id=_cands("mention"),
                                     entity_hits={10: "Рина"})
    assert out[0].action == "reply" and not trace


def test_gate_leaves_plain_unaddressed_reply_alone():
    plans = [ActionPlan(10, "reply", text="ответ", referent="agents in general")]
    out, trace = apply_referent_gate(plans, cand_by_id=_cands("none"), entity_hits={})
    assert out[0].action == "reply" and not trace
