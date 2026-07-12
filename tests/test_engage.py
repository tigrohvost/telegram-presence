import concurrent.futures
import json

from telegram_presence import engage as te


def _assessment(message_id, action="ignore", **fields):
    row = {
        "message_id": message_id,
        "addressed_to": "self",
        "addressed_to_entity": "",
        "self_is_addressee": "yes",
        "self_is_referent": "yes",
        "address_confidence": 0.9,
        "context_sufficient": 0.9,
        "referent": "Rain",
        "inner_thought": "test thought",
        "motivation": "test motivation",
        "want": "no" if action == "ignore" else "yes",
        "action": action,
    }
    if action in {"reply", "delegate"}:
        row["depth"] = "deep" if action == "delegate" else "quick"
    row.update(fields)
    return row


def _assessment_json(message_id, action="ignore", **fields):
    return json.dumps([_assessment(message_id, action, **fields)])


# --- T3 skeleton ---
def test_constants_present():
    assert te.ENGAGE_THROTTLE_SECONDS == 300
    assert te.ENGAGE_MIN_GAP_SECONDS == 120
    assert te.ENGAGE_REPLY_DAILY_CAP == 8
    assert te.ENGAGE_REACT_DAILY_CAP == 3
    assert "👍" in te.EMOJI_ALLOWLIST


def test_dataclasses():
    c = te.Candidate(message_id=5, addressed=True, snippet="hi rain", sender_id=9)
    a = te.ActionPlan(message_id=5, action="reply", text="hello", emoji="", reason="greet")
    assert c.addressed and a.action == "reply"


# --- T4 decision helpers ---
def test_candidates_from_reader_packet():
    pkt = {"status": "ok",
           "matches": [{"message_id": 1, "snippet": "@rain glance", "sender_id": 7, "matched_terms": ["@rain"]}],
           "recent": [{"message_id": 2, "snippet": "new AI agent paper", "sender_id": 8}]}
    cands = te.candidates_from_packet(pkt)
    assert {c.message_id for c in cands} == {1, 2}
    assert next(c for c in cands if c.message_id == 1).addressed is True
    assert next(c for c in cands if c.message_id == 2).addressed is False


def test_candidates_from_non_ok_packet_empty():
    assert te.candidates_from_packet({"status": "timed_out"}) == []


def test_candidates_ignore_slash_commands():
    pkt = {"status": "ok",
           "matches": [{"message_id": 1, "snippet": "/tg on", "sender_id": 7},
                       {"message_id": 2, "snippet": "  /start", "sender_id": 8},
                       {"message_id": 3, "snippet": "Рейн, привет", "sender_id": 9}],
           "recent": [{"message_id": 4, "snippet": "/help", "sender_id": 10},
                      {"message_id": 5, "snippet": "про агентов", "sender_id": 11}]}
    cands = te.candidates_from_packet(pkt)
    assert [c.message_id for c in cands] == [3, 5]




def test_candidates_drop_own_messages_by_flag_and_self_id():
    pkt = {"status": "ok", "self_id": 42,
           "matches": [
               {"message_id": 1, "snippet": "@rain my own mention", "sender_id": 42},
               {"message_id": 2, "snippet": "@rain outgoing", "sender_id": 7, "outgoing": True},
               {"message_id": 3, "snippet": "@rain external", "sender_id": 7}],
           "recent": [
               {"message_id": 4, "snippet": "own recent", "sender_id": 42},
               {"message_id": 5, "snippet": "external recent", "sender_id": 8}]}
    cands = te.candidates_from_packet(pkt)
    assert [c.message_id for c in cands] == [3, 5]


def test_decider_cannot_act_on_non_candidate_message_id(tmp_path):
    pkt = {"status": "ok", "self_id": 42,
           "matches": [{"message_id": 1, "snippet": "@rain own", "sender_id": 42}],
           "recent": [{"message_id": 2, "snippet": "external", "sender_id": 7}]}
    res, _saved, replies, reacts, notifs = _run(
        _base_state(), pkt, drive_root=tmp_path,
        decider=lambda p: _assessment_json(1, "reply", text="self-loop"))
    assert res["status"] == "skipped"
    assert replies == []
    assert reacts == []
    assert notifs == []

def test_parse_action_plan_json_array():
    raw = '[{"message_id":1,"action":"reply","text":"hi","reason":"greet"},{"message_id":2,"action":"react","emoji":"👍"}]'
    plans = te.parse_action_plan(raw)
    assert [p.action for p in plans] == ["reply", "react"]


def test_parse_action_plan_strips_reasoning_from_reply_text():
    raw = (
        '[{"message_id":1,"action":"reply",'
        '"text":"**Thinking**\\nI should answer in Russian.\\n\\nЯ готова ответить.",'
        '"reason":"greet"}]'
    )
    plans = te.parse_action_plan(raw)
    assert plans[0].text == "Я готова ответить."


def test_parse_action_plan_tolerates_prose_wrapping():
    raw = 'Sure! [{"message_id":1,"action":"react","emoji":"🔥"}] done'
    plans = te.parse_action_plan(raw)
    assert plans and plans[0].emoji == "🔥"


def test_validate_clamps_to_one_reply_and_filters_emoji():
    plans = [te.ActionPlan(1, "reply", text="a"), te.ActionPlan(2, "reply", text="b"),
             te.ActionPlan(3, "react", emoji="💣"), te.ActionPlan(4, "react", emoji="🔥")]
    out = te.validate_actions(plans)
    assert sum(1 for p in out if p.action == "reply") == 1
    assert not any(p.action == "react" and p.emoji == "💣" for p in out)
    assert any(p.action == "react" and p.emoji == "🔥" for p in out)


def test_validate_drops_oversize_and_empty_reply():
    plans = [te.ActionPlan(1, "reply", text=""), te.ActionPlan(2, "reply", text="x" * 5000)]
    out = te.validate_actions(plans)
    assert all(p.action != "reply" or (0 < len(p.text) <= te.ENGAGE_MAX_REPLY_CHARS) for p in out)


def test_threaded_addressed_reply_does_not_prepend_sender_handle():
    plan = te.ActionPlan(42, "reply", text="hello")
    candidate = te.Candidate(42, True, "question", sender_username="alice")

    result = te.apply_reply_addressing([plan], cand_by_id={42: candidate})

    assert result[0].text == "hello"


def test_standalone_addressed_reply_prepends_sender_handle_within_limit():
    plan = te.ActionPlan(0, "reply", text="x" * te.ENGAGE_MAX_REPLY_CHARS)
    candidate = te.Candidate(0, True, "question", sender_username="alice")

    result = te.apply_reply_addressing([plan], cand_by_id={0: candidate})

    assert result[0].text.startswith("@alice ")
    assert len(result[0].text) == te.ENGAGE_MAX_REPLY_CHARS


def test_existing_sender_handle_match_is_case_insensitive_and_bounded():
    candidate = te.Candidate(0, True, "question", sender_username="alice")
    exact = te.ActionPlan(0, "reply", text="@Alice hello")
    prefix_collision = te.ActionPlan(0, "reply", text="@alice2 hello")

    exact_result = te.apply_reply_addressing([exact], cand_by_id={0: candidate})[0]
    collision_result = te.apply_reply_addressing([prefix_collision], cand_by_id={0: candidate})[0]

    assert exact_result.text == "@Alice hello"
    assert collision_result.text == "@alice @alice2 hello"


def test_delegated_reply_uses_same_addressing_policy():
    plan = te.ActionPlan(0, "reply", text="deep answer", delegated=True)
    candidate = te.Candidate(0, True, "question", sender_username="alice")

    result = te.apply_reply_addressing([plan], cand_by_id={0: candidate})

    assert result[0].text == "@alice deep answer"
    assert result[0].delegated is True


def test_standalone_delegated_reply_preserves_delegate_length_budget():
    plan = te.ActionPlan(0, "reply", text="x" * 1000, delegated=True)
    candidate = te.Candidate(0, True, "question", sender_username="alice")

    result = te.apply_reply_addressing([plan], cand_by_id={0: candidate})

    assert result[0].text.startswith("@alice ")
    assert len(result[0].text) == 1007


# --- T5 gate + orchestrator ---
def _base_state():
    return {"telegram_engage_enabled": True, "autonomy_enabled": True,
            "owner_chat_id": 99, "telegram_mentions_chat": "@examplechat",
            "telegram_engage": {}}


def _ok_packet():
    return {"status": "ok",
            "matches": [{"message_id": 1, "snippet": "@rain hi", "sender_id": 7,
                         "matched_terms": ["@rain"]}],
            "recent": []}


def _run(st, packet, *, drive_root, replies=None, reacts=None, notifs=None,
         decider=None, now=1000.0):
    replies = replies if replies is not None else []
    reacts = reacts if reacts is not None else []
    notifs = notifs if notifs is not None else []
    saved = {}
    def save(s):
        saved.clear()
        saved.update(s)
    res = te.run_telegram_engage_cycle(
        drive_root=drive_root,
        load_state=lambda: dict(st),
        save_state=save,
        fetch_candidates=lambda dr: packet,
        run_decider=decider or (lambda prompt: _assessment_json(1, "reply", text="hi")),
        do_reply=lambda peer, mid, text: replies.append((peer, mid, text)) or True,
        do_react=lambda peer, mid, emoji: reacts.append((peer, mid, emoji)) or True,
        notify=lambda t: notifs.append(t),
        now=now,
    )
    return res, saved, replies, reacts, notifs


def test_skips_when_flag_off(tmp_path):
    st = _base_state(); st["telegram_engage_enabled"] = False
    res, *_ = _run(st, _ok_packet(), drive_root=tmp_path)
    assert res["status"] == "skipped"


def test_skips_on_kill_file(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "telegram_engage_off.flag").write_text("x")
    res, *_ = _run(_base_state(), _ok_packet(), drive_root=tmp_path)
    assert res["status"] == "skipped"


def test_autonomy_master_off_blocks_proactive_but_not_addressed(tmp_path):
    # Addressed mentions still get answered with the master switch off…
    st = _base_state(); st["autonomy_enabled"] = False
    res, _saved, replies, *_ = _run(st, _ok_packet(), drive_root=tmp_path)
    assert res["status"] == "acted" and replies
    # …but un-addressed chatter is dropped before the decider.
    st2 = _base_state(); st2["autonomy_enabled"] = False
    recent_packet = {"status": "ok", "matches": [],
                     "recent": [{"message_id": 1, "snippet": "chatter", "sender_id": 7}]}
    res2, _saved2, replies2, *_ = _run(st2, recent_packet, drive_root=tmp_path)
    assert res2["status"] == "skipped" and not replies2


def test_reply_executes_and_logs(tmp_path):
    res, saved, replies, reacts, notifs = _run(_base_state(), _ok_packet(), drive_root=tmp_path)
    assert res["status"] == "acted"
    assert replies and replies[0][1] == 1
    assert saved["telegram_engage"]["addressed_reply_count_today"] == 1
    assert (tmp_path / "state" / "telegram_engage_actions.jsonl").exists()


def test_late_reply_and_react_failures_do_not_complete_or_advance_cursor(tmp_path):
    for action in ("reply", "react"):
        root = tmp_path / action
        root.mkdir()
        state = _base_state()
        saved = {}
        packet = {
            "status": "ok",
            "max_ts": 1234.0,
            "matches": [{
                "message_id": 1,
                "snippet": "@rain hi",
                "sender_id": 7,
                "matched_terms": ["@rain"],
            }],
            "recent": [],
        }
        plan = _assessment(1, action, reason="test")
        if action == "reply":
            plan["text"] = "hello"
        else:
            plan["emoji"] = "👍"
        failed: concurrent.futures.Future = concurrent.futures.Future()
        failed.set_exception(RuntimeError(f"late {action} outage"))

        result = te.run_telegram_engage_cycle(
            drive_root=root,
            load_state=lambda: dict(state),
            save_state=lambda value: saved.update(value),
            fetch_candidates=lambda _root, **_kwargs: packet,
            run_decider=lambda _prompt: json.dumps([plan]),
            do_reply=lambda *_args: failed,
            do_react=lambda *_args: failed,
            notify=lambda _text: None,
            now=1000.0,
        )

        assert result["acted"] == 0 and result["retryable_failure"] is True
        assert "packet_max_ts" not in result
        assert not (root / te.ACTION_LOG_REL).exists()
        ledger = saved["telegram_engage"]
        assert ledger["reply_count_today"] == 0
        assert ledger["react_count_today"] == 0
        assert ledger.get("spool_consumed_ts", 0) == 0




def test_does_not_reply_twice_to_same_message_id(tmp_path):
    log_dir = tmp_path / "state"
    log_dir.mkdir()
    (log_dir / "telegram_engage_actions.jsonl").write_text(
        '{"ts": 999.0, "action": "reply", "message_id": 1, "text": "old"}\n',
        encoding="utf-8",
    )
    res, _saved, replies, reacts, notifs = _run(_base_state(), _ok_packet(), drive_root=tmp_path)
    assert res["status"] == "skipped"
    assert replies == []




def test_does_not_react_twice_to_same_chat_message_id(tmp_path):
    log_dir = tmp_path / "state"
    log_dir.mkdir()
    (log_dir / "telegram_engage_actions.jsonl").write_text(
        '{"ts": 999.0, "chat": "@examplechat", "action": "react", "message_id": 1, "emoji": "👍"}\n',
        encoding="utf-8",
    )
    res, _saved, replies, reacts, notifs = _run(
        _base_state(), _ok_packet(), drive_root=tmp_path,
        decider=lambda p: _assessment_json(1, "react", emoji="🔥"))
    assert res["status"] == "skipped"
    assert replies == []
    assert reacts == []


def test_dedup_is_scoped_by_chat(tmp_path):
    log_dir = tmp_path / "state"
    log_dir.mkdir()
    (log_dir / "telegram_engage_actions.jsonl").write_text(
        '{"ts": 999.0, "chat": "@other_chat", "action": "reply", "message_id": 1, "text": "old"}\n',
        encoding="utf-8",
    )
    res, _saved, replies, reacts, notifs = _run(_base_state(), _ok_packet(), drive_root=tmp_path)
    assert res["status"] == "acted"
    assert replies == [("@examplechat", 1, "hi")]


def test_action_log_records_chat_for_future_dedup(tmp_path):
    res, saved, replies, reacts, notifs = _run(_base_state(), _ok_packet(), drive_root=tmp_path)
    row = (tmp_path / "state" / "telegram_engage_actions.jsonl").read_text(encoding="utf-8")
    assert '"chat": "@examplechat"' in row

def test_min_gap_blocks_second_proactive_action(tmp_path):
    st = _base_state(); st["telegram_engage"] = {"last_action_ts": 1000.0}
    recent_packet = {"status": "ok", "matches": [],
                     "recent": [{"message_id": 1, "snippet": "chatter", "sender_id": 7}]}
    res, _saved, replies, *_ = _run(st, recent_packet, drive_root=tmp_path, now=1050.0)
    assert res["status"] == "skipped" and not replies


def test_min_gap_does_not_block_addressed_reply(tmp_path):
    st = _base_state(); st["telegram_engage"] = {"last_action_ts": 1000.0}
    res, _saved, replies, *_ = _run(st, _ok_packet(), drive_root=tmp_path, now=1050.0)
    assert res["status"] == "acted" and replies


def test_addressed_reply_daily_cap(tmp_path):
    st = _base_state()
    st["telegram_engage"] = {"addressed_reply_count_today": te.ENGAGE_ADDRESSED_REPLY_DAILY_CAP,
                              "day_key": te._day_key(1000.0)}
    res, _saved, replies, *_ = _run(st, _ok_packet(), drive_root=tmp_path, now=1000.0)
    assert not replies


def test_unaddressed_reply_daily_cap(tmp_path):
    st = _base_state()
    st["telegram_engage"] = {"reply_count_today": te.ENGAGE_REPLY_DAILY_CAP, "day_key": te._day_key(1000.0)}
    recent_packet = {"status": "ok", "matches": [],
                     "recent": [{"message_id": 1, "snippet": "chatter", "sender_id": 7}]}
    res, _saved, replies, *_ = _run(st, recent_packet, drive_root=tmp_path, now=1000.0)
    assert not replies


def test_empty_candidates_skip(tmp_path):
    res, *_ = _run(_base_state(), {"status": "ok", "matches": [], "recent": []}, drive_root=tmp_path)
    assert res["status"] == "skipped"


def test_never_raises_on_bad_decider(tmp_path):
    res, *_ = _run(_base_state(), _ok_packet(), drive_root=tmp_path,
                   decider=lambda p: (_ for _ in ()).throw(RuntimeError("llm down")))
    assert res["status"] in ("skipped", "acted", "error")


def test_react_action_executes(tmp_path):
    res, saved, replies, reacts, notifs = _run(
        _base_state(), _ok_packet(), drive_root=tmp_path,
        decider=lambda p: _assessment_json(1, "react", emoji="🔥"))
    assert res["status"] == "acted"
    assert reacts and reacts[0][2] == "🔥"


def test_notify_owner_action(tmp_path):
    res, saved, replies, reacts, notifs = _run(
        _base_state(), _ok_packet(), drive_root=tmp_path,
        decider=lambda p: _assessment_json(
            1, "notify_owner", reason="relevant AI paper",
        ))
    assert notifs and "relevant AI paper" in notifs[0]


# --- gate split + addressed budgets (telegram I/O refactor) ---

from telegram_presence.engage import (
    ActionPlan as _AP,
    gate_open as _gate_open,
    validate_actions as _validate_actions,
)


def _gate_state(**over):
    st = {"telegram_engage_enabled": True, "autonomy_enabled": False}
    st.update(over)
    return st


def test_gate_addressed_open_without_autonomy(tmp_path):
    verdict = _gate_open(_gate_state(), tmp_path, now=1000.0)
    assert verdict.addressed_ok is True
    assert verdict.proactive_ok is False
    assert "autonomy" in verdict.proactive_reason


def test_gate_all_closed_when_engage_off(tmp_path):
    verdict = _gate_open(_gate_state(telegram_engage_enabled=False), tmp_path, now=1000.0)
    assert verdict.addressed_ok is False and verdict.proactive_ok is False


def test_gate_kill_file_closes_everything(tmp_path):
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "panic_stop.flag").touch()
    verdict = _gate_open(_gate_state(autonomy_enabled=True), tmp_path, now=1000.0)
    assert verdict.addressed_ok is False and verdict.proactive_ok is False


def test_gate_min_gap_blocks_proactive_not_addressed(tmp_path):
    st = _gate_state(autonomy_enabled=True,
                     telegram_engage={"last_action_ts": 990.0})
    verdict = _gate_open(st, tmp_path, now=1000.0)
    assert verdict.addressed_ok is True
    assert verdict.proactive_ok is False


def test_validate_actions_addressed_reply_budget():
    addressed = {1, 2, 3, 4}
    plans = [_AP(i, "reply", text=f"r{i}") for i in (1, 2, 3, 4)] + [
        _AP(9, "reply", text="unaddr")
    ]
    out = _validate_actions(plans, addressed_ids=addressed)
    replies = [p for p in out if p.action == "reply"]
    # up to 3 addressed replies + 1 unaddressed
    assert [p.message_id for p in replies] == [1, 2, 3, 9]
    plans2 = [_AP(i, "reply", text="x") for i in (8, 9)]
    out2 = _validate_actions(plans2, addressed_ids=set())
    assert len([p for p in out2 if p.action == "reply"]) == 1


def test_action_log_records_addressed_kind_and_mentions_other(tmp_path):
    """Audit trail for the A+B fix: each action row records how the message
    addressed Rain (kind) and whether it tagged someone else."""
    import json as _json
    pkt = {"status": "ok", "recent": [], "matches": [
        {"message_id": 1, "snippet": "@rain and @bob hi", "sender_id": 7, "matched_terms": ["@rain"]}]}
    res, _saved, replies, *_ = _run(_base_state(), pkt, drive_root=tmp_path)
    assert res["status"] == "acted" and replies
    row = _json.loads((tmp_path / "state" / "telegram_engage_actions.jsonl")
                      .read_text(encoding="utf-8").splitlines()[-1])
    assert row["addressed_kind"] == "mention"
    assert row["mentions_other"] is True


# --- Per-chat budget tests ---

def test_per_chat_reply_cap():
    """Unaddressed reply is blocked when per-chat cap is reached."""
    from telegram_presence.engage import _check_caps
    state = {
        'telegram_engage': {
            'day_key': '2026-07-06',
            'reply_count_today': 0,
            'react_count_today': 0,
            'addressed_reply_count_today': 0,
            'per_chat': {
                '-1001234567890': {
                    'day_key': '2026-07-06',
                    'reply_count': 3,
                    'addressed_count': 0,
                }
            }
        }
    }
    ok, reason = _check_caps(state, chat_id='-1001234567890', is_addressed=False)
    assert not ok
    assert 'per-chat' in reason.lower()


def test_per_chat_addressed_cap():
    """Addressed reply is blocked when per-chat addressed cap is reached."""
    from telegram_presence.engage import PER_CHAT_ADDRESSED_REPLY_DAILY_CAP, _check_caps
    state = {
        'telegram_engage': {
            'day_key': '2026-07-06',
            'reply_count_today': 0,
            'react_count_today': 0,
            'addressed_reply_count_today': 0,
            'per_chat': {
                '-1001234567890': {
                    'day_key': '2026-07-06',
                    'reply_count': 0,
                    'addressed_count': PER_CHAT_ADDRESSED_REPLY_DAILY_CAP,
                }
            }
        }
    }
    ok, reason = _check_caps(state, chat_id='-1001234567890', is_addressed=True)
    assert not ok
    assert 'per-chat' in reason.lower()


def test_per_chat_resets_on_new_day():
    """Per-chat counters reset when day changes."""
    from telegram_presence.engage import _check_caps
    state = {
        'telegram_engage': {
            'day_key': '2026-07-06',
            'reply_count_today': 0,
            'react_count_today': 0,
            'addressed_reply_count_today': 0,
            'per_chat': {
                '-1001234567890': {
                    'day_key': '2026-07-05',
                    'reply_count': 3,
                    'addressed_count': 6,
                }
            }
        }
    }
    ok, reason = _check_caps(state, chat_id='-1001234567890', is_addressed=False)
    assert ok


# --- burst coalescing (2026-07-08) ---

def _cand(mid, sender, snippet, ts, addressed=False, kind="none", username=None,
          topic_id=None):
    return te.Candidate(mid, addressed, snippet, sender_id=sender,
                        sender_username=username or f"u{sender}",
                        addressed_kind=kind, ts=ts, topic_id=topic_id)


def test_coalesce_merges_same_sender_burst():
    cands = [
        _cand(1, 7, "how do you", 100.0),
        _cand(2, 7, "learn new things?", 130.0, addressed=True, kind="mention"),
        _cand(3, 8, "unrelated", 140.0),
    ]
    out = te.coalesce_candidates(cands)
    assert [c.message_id for c in out] == [2, 3]
    merged = out[0]
    assert "how do you" in merged.snippet and "learn new things?" in merged.snippet
    assert merged.addressed is True and merged.addressed_kind == "mention"


def test_coalesce_respects_time_gap():
    cands = [_cand(1, 7, "first", 100.0), _cand(2, 7, "much later", 100.0 + 3600)]
    out = te.coalesce_candidates(cands)
    assert [c.message_id for c in out] == [1, 2]


def test_coalesce_caps_join_size():
    cands = [_cand(i, 7, f"m{i}", 100.0 + i) for i in range(1, 6)]
    out = te.coalesce_candidates(cands, max_join=3)
    assert len(out) == 2  # 5 messages -> group of 3 + group of 2
    assert out[0].message_id == 3 and out[1].message_id == 5


def test_coalesce_strongest_kind_wins():
    cands = [
        _cand(1, 7, "replying to you", 100.0, addressed=True, kind="reply"),
        _cand(2, 7, "and a mention", 110.0, addressed=True, kind="mention"),
    ]
    out = te.coalesce_candidates(cands)
    assert len(out) == 1 and out[0].addressed_kind == "reply"


def test_coalesce_splits_same_sender_when_explicit_target_changes():
    cands = [
        te.Candidate(
            1, True, "@rain first", sender_id=7, sender_username="u7",
            addressed_kind="mention", mentioned_handles=("@rain",), ts=100.0,
        ),
        te.Candidate(
            2, False, "@bob your turn", sender_id=7, sender_username="u7",
            addressed_kind="none", mentions_other=True,
            mentioned_handles=("@bob",), ts=110.0,
        ),
    ]

    out = te.coalesce_candidates(cands)

    assert [candidate.message_id for candidate in out] == [1, 2]


def test_coalesce_never_merges_across_senders():
    cands = [_cand(1, 7, "a", 100.0), _cand(2, 8, "b", 101.0), _cand(3, 7, "c", 102.0)]
    out = te.coalesce_candidates(cands)
    assert [c.message_id for c in out] == [1, 2, 3]


def test_coalesce_never_merges_across_forum_topics():
    cands = [
        _cand(1, 7, "topic one", 100.0, topic_id=10),
        _cand(2, 7, "topic two", 101.0, topic_id=20),
    ]

    assert [c.message_id for c in te.coalesce_candidates(cands)] == [1, 2]


def test_decider_prompt_warns_about_third_person_referents():
    """Regression: Ashe's «Я тоже хочу с ней поговорить» (about another bot,
    Рина) was read as being about Rain herself (2026-07-11)."""
    c = te.Candidate(message_id=1, addressed=False, snippet="Я тоже хочу с ней поговорить",
                     sender_id=9, addressed_kind="none")
    prompt = te.build_decider_prompt([c])
    assert "Third-person pronouns" in prompt
    assert "Do not default them to yourself" in prompt
    assert "transport/structure CUES, not a verdict" in prompt
