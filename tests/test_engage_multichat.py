"""Multi-chat engage: allowed_chats, per-chat fetch cursors, per-chat pauses."""
from __future__ import annotations

import json
import re

from telegram_presence import engage as te
from telegram_presence import inbox as gi


# --- allowed chats / matching ---

def test_allowed_chats_merges_state_list(monkeypatch):
    monkeypatch.delenv("TELEGRAM_MENTIONS_CHAT", raising=False)
    from telegram_presence import hooks
    monkeypatch.setattr(hooks, "_state_loader", lambda: {
        "telegram_mentions_chat": "@primary",
        "telegram_engage_chats": ["@second", "@PRIMARY", "@third", ""],
    })
    chats = gi.allowed_chats()
    assert chats == ["@primary", "@second", "@third"]


def test_allowed_chat_still_first(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MENTIONS_CHAT", "@envchat")
    assert gi.allowed_chat() == "@envchat"


def test_chat_matches_any_returns_canonical():
    allowed = ["@primary", "@second"]
    assert gi.chat_matches_any("second", 123, allowed) == "@second"
    assert gi.chat_matches_any("other", 123, allowed) is None


def test_engage_chats_from_state(monkeypatch):
    monkeypatch.delenv("TELEGRAM_MENTIONS_CHAT", raising=False)
    st = {"telegram_mentions_chat": "@main",
          "telegram_engage_chats": ["@extra", "@main"]}
    assert gi.allowed_chats(st=st) == ["@main", "@extra"]


def test_allowed_chats_canonicalizes_alias_spelling(monkeypatch):
    monkeypatch.delenv("TELEGRAM_MENTIONS_CHAT", raising=False)
    st = {"telegram_mentions_chat": "Chat", "telegram_engage_chats": ["@CHAT"]}
    assert gi.allowed_chats(st=st) == ["@chat"]


def test_per_chat_ledger_merges_aliases_without_cursor_or_budget_reset():
    led = {
        "per_chat": {
            "@Chat": {"reply_count": 1, "addressed_count": 2,
                      "spool_consumed_seq": 10,
                      "topics": {"topic:7": {"spool_consumed_seq": 8}}},
            "chat": {"reply_count": 3, "addressed_count": 4,
                     "spool_consumed_seq": 12,
                     "topics": {"topic:7": {"spool_consumed_seq": 11,
                                                "retry_failure_count": 2}}},
        },
    }

    te._normalize_per_chat_ledger(led)

    assert list(led["per_chat"]) == ["@chat"]
    assert led["per_chat"]["@chat"]["reply_count"] == 4
    assert led["per_chat"]["@chat"]["addressed_count"] == 6
    assert led["per_chat"]["@chat"]["spool_consumed_seq"] == 12
    topic = led["per_chat"]["@chat"]["topics"]["topic:7"]
    assert topic["spool_consumed_seq"] == 11
    assert topic["retry_failure_count"] == 2


# --- cycle over two chats ---

def _assessment(message_id: int, action: str, *, text: str = "",
                addressed_to: str = "self") -> dict:
    speaking = action in {"reply", "delegate"}
    row = {
        "message_id": message_id,
        "addressed_to": addressed_to,
        "addressed_to_entity": "",
        "self_is_addressee": "yes" if addressed_to == "self" else "no",
        "self_is_referent": "unclear",
        "address_confidence": 1.0,
        "context_sufficient": 1.0,
        "referent": "the current test conversation",
        "inner_thought": "I have a concise contribution." if speaking else "",
        "motivation": "Worth answering." if speaking else "No useful contribution.",
        "want": "yes" if speaking else "no",
        "action": action,
        "text": text,
        "emoji": "",
        "reason": "test fixture",
    }
    if speaking:
        row["depth"] = "deep" if action == "delegate" else "quick"
    return row


def _decision(*assessments: dict) -> str:
    return json.dumps(assessments, ensure_ascii=False)


def _decider(prompt: str) -> str:
    ids = {int(m) for m in re.findall(r'"message_id": (\d+)', prompt)}
    return _decision(*(_assessment(i, "reply", text=f"hi-{i}")
                       for i in sorted(ids)))


def _two_chat_state():
    return {"telegram_engage_enabled": True, "autonomy_enabled": True,
            "owner_chat_id": 99, "telegram_mentions_chat": "@chata",
            "telegram_engage_chats": ["@chatb"],
            "telegram_engage": {"per_chat": {
                "@chata": {"day_key": "", "reply_count": 0, "addressed_count": 0,
                           "spool_consumed_ts": 50.0},
                "@chatb": {"day_key": "", "reply_count": 0, "addressed_count": 0,
                           "spool_consumed_ts": 70.0},
            }}}


def _packets():
    return {
        "@chata": {"status": "ok", "max_ts": 100.0,
                   "matches": [{"message_id": 1, "snippet": "@rain q-a", "sender_id": 7,
                                "matched_terms": ["@rain"]}],
                   "recent": []},
        "@chatb": {"status": "ok", "max_ts": 200.0,
                   "matches": [{"message_id": 2, "snippet": "@rain q-b", "sender_id": 8,
                                "matched_terms": ["@rain"]}],
                   "recent": []},
    }


def _run(st, *, drive_root, replies, fetch_calls):
    packets = _packets()

    def fetch(dr, chat=None, after_ts=None):
        fetch_calls.append((chat, after_ts))
        return packets.get(str(chat), {"status": "ok", "matches": [], "recent": []})

    return te.run_telegram_engage_cycle(
        drive_root=drive_root,
        load_state=lambda: dict(st),
        save_state=lambda s: st.update(s),
        fetch_candidates=fetch,
        run_decider=_decider,
        do_reply=lambda peer, mid, text: replies.append((peer, mid, text)) or True,
        do_react=lambda peer, mid, emoji: True,
        notify=lambda t: None,
        now=1000.0,
    )


def test_cycle_acts_in_both_chats_with_own_cursors(tmp_path):
    st = _two_chat_state()
    replies: list = []
    fetch_calls: list = []
    res = _run(st, drive_root=tmp_path, replies=replies, fetch_calls=fetch_calls)
    assert res["status"] == "acted" and res["acted"] == 2
    assert ("@chata", 1, "hi-1") in replies and ("@chatb", 2, "hi-2") in replies
    # per-chat cursor passed to fetch and advanced to each packet's max_ts
    assert ("@chata", 50.0) in fetch_calls and ("@chatb", 70.0) in fetch_calls
    per_chat = st["telegram_engage"]["per_chat"]
    assert per_chat["@chata"]["spool_consumed_ts"] == 100.0
    assert per_chat["@chatb"]["spool_consumed_ts"] == 200.0
    assert res["packet_max_ts"] == 200.0


def test_pause_blocks_one_chat_not_the_other(tmp_path):
    st = _two_chat_state()
    st["telegram_chat_pauses"] = {"chatb": {"paused": True}}
    replies: list = []
    res = _run(st, drive_root=tmp_path, replies=replies, fetch_calls=[])
    assert res["status"] == "acted" and res["acted"] == 1
    assert [r[0] for r in replies] == ["@chata"]


def test_legacy_single_arg_fetch_still_served(tmp_path):
    st = {"telegram_engage_enabled": True, "autonomy_enabled": True,
          "owner_chat_id": 99, "telegram_mentions_chat": "@chata",
          "telegram_engage": {}}
    replies: list = []
    res = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(st),
        save_state=lambda s: st.update(s),
        fetch_candidates=lambda dr: _packets()["@chata"],
        run_decider=_decider,
        do_reply=lambda peer, mid, text: replies.append((peer, mid, text)) or True,
        do_react=lambda peer, mid, emoji: True,
        notify=lambda t: None,
        now=1000.0,
    )
    assert res["status"] == "acted" and replies == [("@chata", 1, "hi-1")]


# --- single source of truth for chat resolution (live incident 2026-07-09:
# three divergent resolutions — allowed_chats, _engage_chats, raw env read in
# the reader fallback — let the engage loop serve a retired chat) ---

def test_engage_cycle_serves_allowed_chats(monkeypatch, tmp_path):
    monkeypatch.setattr(gi, "allowed_chats", lambda st=None: ["@one", "@two"])
    seen = []

    def fetch(dr, chat=None, after_ts=None):
        seen.append(chat)
        return {"status": "ok", "matches": [], "recent": []}

    te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: {"telegram_engage_enabled": True, "autonomy_enabled": True,
                            "owner_chat_id": 99, "telegram_engage": {}},
        save_state=lambda s: None,
        fetch_candidates=fetch,
        run_decider=lambda p: "[]",
        do_reply=lambda *a: True, do_react=lambda *a: True,
        notify=lambda t: None, now=1000.0)
    assert seen == ["@one", "@two"]


def test_allowed_chats_accepts_state_dict(monkeypatch):
    monkeypatch.delenv("TELEGRAM_MENTIONS_CHAT", raising=False)
    st = {"telegram_mentions_chat": "@main", "telegram_engage_chats": ["@extra", "@main"]}
    assert gi.allowed_chats(st=st) == ["@main", "@extra"]
    assert gi.allowed_chats(st={}) == []


def test_engage_private_resolution_is_gone():
    assert not hasattr(te, "_engage_chats")


def test_invalid_decider_response_does_not_advance_chat_cursor(tmp_path):
    st = _two_chat_state()
    st["telegram_engage_chats"] = []

    res = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(st),
        save_state=lambda saved: st.update(saved),
        fetch_candidates=lambda *_args, **_kwargs: _packets()["@chata"],
        run_decider=lambda _prompt: "",
        do_reply=lambda *a: True,
        do_react=lambda *a: True,
        notify=lambda _text: None,
        now=1000.0,
    )

    assert res["status"] == "skipped"
    assert res["retryable_failure"] is True
    assert res.get("packet_max_ts") is None
    assert st["telegram_engage"]["per_chat"]["@chata"]["spool_consumed_ts"] == 50.0
    retry = st["telegram_engage"]["per_chat"]["@chata"]["topics"]["main"]
    assert retry["retry_failure_count"] == 1
    assert retry["retry_not_before_ts"] == 1015.0
    assert "retry_not_before_ts" not in st["telegram_engage"]


def test_one_chat_decider_failure_does_not_block_other_chat(tmp_path):
    st = _two_chat_state()
    replies = []

    def decider(prompt):
        if '"message_id": 1' in prompt:
            return ""
        return _decision(_assessment(2, "reply", text="chat-b ok"))

    res = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(st),
        save_state=lambda saved: st.update(saved),
        fetch_candidates=lambda _root, chat=None, **_kwargs: _packets()[str(chat)],
        run_decider=decider,
        do_reply=lambda *args: replies.append(args) or True,
        do_react=lambda *args: True,
        notify=lambda _text: None,
        now=1000.0,
    )

    assert res["status"] == "acted"
    assert res["retryable_failure"] is True
    assert replies == [("@chatb", 2, "chat-b ok")]
    per_chat = st["telegram_engage"]["per_chat"]
    assert per_chat["@chata"]["topics"]["main"]["retry_not_before_ts"] == 1015.0
    assert "retry_not_before_ts" not in per_chat["@chatb"]
    assert per_chat["@chata"]["spool_consumed_ts"] == 50.0
    assert per_chat["@chatb"]["spool_consumed_ts"] == 200.0


def test_forum_topics_use_separate_decider_invocations(tmp_path):
    st = _two_chat_state()
    st["telegram_engage_chats"] = []
    prompts = []
    replies = []
    packet = {
        "status": "ok", "max_ts": 120.0, "max_seq": 12,
        "matches": [
            {"message_id": 10, "snippet": "@rain alpha topic", "sender_id": 7,
             "matched_terms": ["@rain"], "topic_id": 101},
            {"message_id": 20, "snippet": "@rain beta topic", "sender_id": 8,
             "matched_terms": ["@rain"], "topic_id": 202},
        ],
        "recent": [],
    }

    def decider(prompt):
        prompts.append(prompt)
        mid = 10 if '"message_id": 10' in prompt else 20
        return _decision(_assessment(mid, "reply", text=f"reply-{mid}"))

    res = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(st),
        save_state=lambda saved: st.update(saved),
        fetch_candidates=lambda *_args, **_kwargs: packet,
        run_decider=decider,
        do_reply=lambda *args: replies.append(args) or True,
        do_react=lambda *args: True,
        notify=lambda _text: None,
        now=1000.0,
    )

    assert res["acted"] == 2
    assert len(prompts) == 2
    assert any("alpha topic" in prompt and "beta topic" not in prompt for prompt in prompts)
    assert any("beta topic" in prompt and "alpha topic" not in prompt for prompt in prompts)
    assert replies == [("@chata", 10, "reply-10"), ("@chata", 20, "reply-20")]


def test_invalid_forum_topic_does_not_consume_chat_cursor(tmp_path):
    st = _two_chat_state()
    st["telegram_engage_chats"] = []
    replies = []
    packet = {
        "status": "ok", "max_ts": 120.0, "max_seq": 12,
        "matches": [
            {"message_id": 10, "snippet": "@rain broken topic", "sender_id": 7,
             "matched_terms": ["@rain"], "topic_id": 101},
            {"message_id": 20, "snippet": "@rain healthy topic", "sender_id": 8,
             "matched_terms": ["@rain"], "topic_id": 202},
        ],
        "recent": [],
    }

    def decider(prompt):
        if "broken topic" in prompt:
            return ""
        return _decision(_assessment(20, "reply", text="healthy reply"))

    res = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(st),
        save_state=lambda saved: st.update(saved),
        fetch_candidates=lambda *_args, **_kwargs: packet,
        run_decider=decider,
        do_reply=lambda *args: replies.append(args) or True,
        do_react=lambda *args: True,
        notify=lambda _text: None,
        now=1000.0,
    )

    assert res["status"] == "acted"
    assert res["retryable_failure"] is True
    assert replies == [("@chata", 20, "healthy reply")]
    per_chat = st["telegram_engage"]["per_chat"]["@chata"]
    assert per_chat["spool_consumed_ts"] == 50.0
    topic_retry = per_chat["topics"]["topic:101"]
    assert topic_retry["retry_not_before_ts"] == 1015.0
    assert per_chat["topics"]["topic:202"]["retry_failure_count"] == 0


def test_schema_invalid_decider_plan_does_not_advance_chat_cursor(tmp_path):
    for raw in (
        '[{"message_id": 81, "action": "repy", "text": "опечатка"}]',
        '[{"action": "reply", "text": "нет id"}]',
        '[{"message_id": 999, "action": "reply", "text": "чужой id"}]',
        '[{"message_id": 81, "action": "react", "emoji": "💣"}]',
    ):
        st = _two_chat_state()
        st["telegram_engage_chats"] = []

        res = te.run_telegram_engage_cycle(
            drive_root=tmp_path,
            load_state=lambda: dict(st),
            save_state=lambda saved: st.update(saved),
            fetch_candidates=lambda *_args, **_kwargs: _packets()["@chata"],
            run_decider=lambda _prompt, payload=raw: payload,
            do_reply=lambda *a: True,
            do_react=lambda *a: True,
            notify=lambda _text: None,
            now=1000.0,
        )

        assert res["status"] == "skipped"
        assert res["retryable_failure"] is True
        assert res.get("packet_max_ts") is None
        assert st["telegram_engage"]["per_chat"]["@chata"]["spool_consumed_ts"] == 50.0


def test_explicit_ignore_advances_cursor_and_empty_array_is_invalid(tmp_path):
    st = _two_chat_state()
    st["telegram_engage_chats"] = []
    assert not te._action_plan_payload_valid(
        "[]", allowed_message_ids={1}, required_message_ids={1},
    )

    res = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(st),
        save_state=lambda saved: st.update(saved),
        fetch_candidates=lambda *_args, **_kwargs: _packets()["@chata"],
        run_decider=lambda _prompt: (
            f"```json\n{_decision(_assessment(1, 'ignore'))}\n```"
        ),
        do_reply=lambda *a: True,
        do_react=lambda *a: True,
        notify=lambda _text: None,
        now=1000.0,
    )

    assert res["status"] == "skipped"
    assert res.get("retryable_failure") is None
    assert st["telegram_engage"]["per_chat"]["@chata"]["spool_consumed_ts"] == 100.0


def test_dead_letter_is_terminal_and_does_not_poison_chat_cursor(tmp_path):
    from telegram_presence.group_delivery import GroupActionOutbox

    outbox = GroupActionOutbox(tmp_path, max_attempts=1, base_backoff_sec=0.05)
    record = outbox.enqueue("@chata", 1, "reply", "ответ")
    try:
        outbox.deliver(record.action_id, lambda *_args: (_ for _ in ()).throw(OSError("down")))
    except OSError:
        pass
    assert outbox.get(record.action_id).status == "dead"

    st = _two_chat_state()
    st["telegram_engage_chats"] = []
    sends = []
    alerts = []
    res = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(st),
        save_state=lambda saved: st.update(saved),
        fetch_candidates=lambda *_args, **_kwargs: _packets()["@chata"],
        run_decider=lambda _prompt: _decision(
            _assessment(1, "reply", text="ответ"),
        ),
        do_reply=lambda *args: sends.append(args) or True,
        do_react=lambda *args: True,
        notify=alerts.append,
        now=1000.0,
    )

    assert sends == []
    assert res.get("retryable_failure") is None
    assert st["telegram_engage"]["per_chat"]["@chata"]["spool_consumed_ts"] == 100.0
    assert len(alerts) == 1
    assert "permanently failed" in alerts[0]


def test_acked_crash_gap_logs_durable_payload_not_rephrased_plan(tmp_path):
    from telegram_presence.group_delivery import GroupActionOutbox

    outbox = GroupActionOutbox(tmp_path)
    record = outbox.enqueue("@chata", 1, "reply", "фактически отправлено")
    outbox.deliver(record.action_id, lambda *_args: {"message_id": 777})

    st = _two_chat_state()
    st["telegram_engage_chats"] = []
    sends = []
    res = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(st),
        save_state=lambda saved: st.update(saved),
        fetch_candidates=lambda *_args, **_kwargs: _packets()["@chata"],
        run_decider=lambda _prompt: _decision(
            _assessment(1, "reply", text="новая формулировка"),
        ),
        do_reply=lambda *args: sends.append(args) or True,
        do_react=lambda *args: True,
        notify=lambda _text: None,
        now=1000.0,
    )

    assert res["status"] == "acted"
    assert sends == []
    rows = [
        json.loads(line)
        for line in (tmp_path / te.ACTION_LOG_REL).read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["text"] == "фактически отправлено"


def test_twenty_topics_are_bounded_prioritized_and_drained_without_cursor_loss(tmp_path):
    st = _two_chat_state()
    st["telegram_engage_chats"] = []
    prompts: list[int] = []
    addressed = [
        {"message_id": mid, "snippet": f"@rain addressed-{mid}",
         "sender_id": mid, "matched_terms": ["@rain"],
         "topic_id": mid, "spool_seq": mid, "ts": 100.0 + mid}
        for mid in range(11, 21)
    ]
    unaddressed = [
        {"message_id": mid, "snippet": f"ambient-{mid}",
         "sender_id": mid, "topic_id": mid,
         "spool_seq": mid, "ts": 100.0 + mid}
        for mid in range(1, 11)
    ]
    packet = {"status": "ok", "max_ts": 120.0, "max_seq": 20,
              "matches": addressed, "recent": unaddressed}

    def decider(prompt):
        mid = int(re.search(r'"message_id": (\d+)', prompt).group(1))
        prompts.append(mid)
        addressed_to = "self" if mid >= 11 else "group"
        return _decision(_assessment(mid, "ignore", addressed_to=addressed_to))

    for offset in range(5):
        before = len(prompts)
        result = te.run_telegram_engage_cycle(
            drive_root=tmp_path,
            load_state=lambda: dict(st),
            save_state=lambda saved: st.update(saved),
            fetch_candidates=lambda *_args, **_kwargs: packet,
            run_decider=decider,
            do_reply=lambda *args: True,
            do_react=lambda *args: True,
            notify=lambda _text: None,
            now=1000.0 + offset,
        )
        assert len(prompts) - before <= te.ENGAGE_DECIDER_CALLS_PER_CHAT
        assert result["decider_calls"] <= te.ENGAGE_DECIDER_CALLS_PER_CYCLE
        if offset < 4:
            assert st["telegram_engage"]["per_chat"]["@chata"]["spool_consumed_ts"] == 50.0

    assert prompts[:4] == [11, 12, 13, 14]
    assert sorted(prompts) == list(range(1, 21))
    lane = st["telegram_engage"]["per_chat"]["@chata"]
    assert lane["spool_consumed_seq"] == 20
    assert lane["spool_consumed_ts"] == 120.0


def test_decider_budget_is_strict_per_chat_and_per_cycle(tmp_path):
    st = {
        "telegram_engage_enabled": True,
        "autonomy_enabled": True,
        "owner_chat_id": 99,
        "telegram_mentions_chat": "@one",
        "telegram_engage_chats": ["@two", "@three"],
        "telegram_engage": {},
    }
    packets = {}
    for chat_index, chat in enumerate(("@one", "@two", "@three"), start=1):
        base = chat_index * 100
        rows = [
            {"message_id": base + i, "snippet": f"@rain {chat}-{i}",
             "sender_id": base + i, "matched_terms": ["@rain"],
             "topic_id": i, "spool_seq": base + i, "ts": 1000.0 + base + i}
            for i in range(1, 5)
        ]
        packets[chat] = {
            "status": "ok", "max_seq": base + 4, "max_ts": 1000.0 + base + 4,
            "matches": rows, "recent": [],
        }
    called: list[int] = []

    def decider(prompt):
        mid = int(re.search(r'"message_id": (\d+)', prompt).group(1))
        called.append(mid)
        return _decision(_assessment(mid, "ignore"))

    result = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(st),
        save_state=lambda saved: st.update(saved),
        fetch_candidates=lambda _root, chat=None, **_kwargs: packets[str(chat)],
        run_decider=decider,
        do_reply=lambda *args: True,
        do_react=lambda *args: True,
        notify=lambda _text: None,
        now=1000.0,
    )

    assert len(called) == te.ENGAGE_DECIDER_CALLS_PER_CYCLE == 8
    assert sum(100 < mid < 200 for mid in called) == te.ENGAGE_DECIDER_CALLS_PER_CHAT
    assert sum(200 < mid < 300 for mid in called) == te.ENGAGE_DECIDER_CALLS_PER_CHAT
    assert not any(300 < mid < 400 for mid in called)
    assert result["decider_calls"] == 8


def test_fourth_addressed_reply_is_deferred_then_delivered_once(tmp_path):
    st = _two_chat_state()
    st["telegram_engage_chats"] = []
    rows = [
        {"message_id": mid, "snippet": f"@rain question-{mid}",
         "sender_id": mid, "matched_terms": ["@rain"],
         "topic_id": 77, "spool_seq": mid, "ts": 100.0 + mid}
        for mid in range(1, 5)
    ]
    packet = {"status": "ok", "max_seq": 4, "max_ts": 104.0,
              "matches": rows, "recent": []}
    replies = []

    def decider(prompt):
        mids = [int(value) for value in re.findall(r'"message_id": (\d+)', prompt)]
        return _decision(*(
            _assessment(mid, "reply", text=f"answer-{mid}") for mid in mids
        ))

    def cycle(now):
        return te.run_telegram_engage_cycle(
            drive_root=tmp_path,
            load_state=lambda: dict(st),
            save_state=lambda saved: st.update(saved),
            fetch_candidates=lambda *_args, **_kwargs: packet,
            run_decider=decider,
            do_reply=lambda *args: replies.append(args) or True,
            do_react=lambda *args: True,
            notify=lambda _text: None,
            now=now,
        )

    first = cycle(1000.0)
    assert [row[1] for row in replies] == [1, 2, 3]
    assert first.get("packet_max_seq") is None
    assert st["telegram_engage"]["per_chat"]["@chata"]["spool_consumed_ts"] == 50.0

    second = cycle(1001.0)
    assert [row[1] for row in replies] == [1, 2, 3, 4]
    assert second["packet_max_seq"] == 4
    assert st["telegram_engage"]["per_chat"]["@chata"]["spool_consumed_seq"] == 4


def test_one_busy_topic_is_split_into_exact_bounded_social_reads(tmp_path):
    st = _two_chat_state()
    st["telegram_engage_chats"] = []
    rows = [
        {
            "message_id": mid, "snippet": f"ambient-{mid}", "sender_id": mid,
            "topic_id": 77, "spool_seq": mid, "ts": 100.0 + mid,
        }
        for mid in range(1, 18)
    ]
    packet = {
        "status": "ok", "max_seq": 17, "max_ts": 117.0,
        "matches": [], "recent": rows,
    }
    batches: list[list[int]] = []
    prompts: list[str] = []

    def decider(prompt):
        prompts.append(prompt)
        mids = [int(value) for value in re.findall(r'"message_id": (\d+)', prompt)]
        batches.append(mids)
        return _decision(*(_assessment(mid, "ignore", addressed_to="group") for mid in mids))

    result = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(st), save_state=lambda saved: st.update(saved),
        fetch_candidates=lambda *_args, **_kwargs: packet,
        run_decider=decider,
        do_reply=lambda *args: True, do_react=lambda *args: True,
        notify=lambda _text: None, now=1000.0,
    )

    assert batches == [list(range(1, 9)), list(range(9, 17)), [17]]
    assert all(len(batch) <= te.ENGAGE_DECIDER_CANDIDATES_PER_CALL for batch in batches)
    assert "Later turns already observed" in prompts[0]
    assert "later[mid=17 " in prompts[0]
    assert result["packet_max_seq"] == 17
    assert st["telegram_engage"]["per_chat"]["@chata"]["spool_consumed_seq"] == 17


def test_later_batch_never_advances_past_earlier_deferred_action(tmp_path):
    st = _two_chat_state()
    st["telegram_engage_chats"] = []
    rows = [
        {
            "message_id": mid, "snippet": f"@rain question-{mid}",
            "sender_id": mid, "matched_terms": ["@rain"], "topic_id": 77,
            "spool_seq": mid, "ts": 100.0 + mid,
        }
        for mid in range(1, 11)
    ]
    packet = {
        "status": "ok", "max_seq": 10, "max_ts": 110.0,
        "matches": rows, "recent": [],
    }

    def decider(prompt):
        mids = [int(value) for value in re.findall(r'"message_id": (\d+)', prompt)]
        return _decision(*(
            _assessment(mid, "reply", text=f"answer-{mid}") for mid in mids
        ))

    result = te.run_telegram_engage_cycle(
        drive_root=tmp_path,
        load_state=lambda: dict(st), save_state=lambda saved: st.update(saved),
        fetch_candidates=lambda *_args, **_kwargs: packet,
        run_decider=decider,
        do_reply=lambda *args: True, do_react=lambda *args: True,
        notify=lambda _text: None, now=1000.0,
    )

    # Only three replies fit the per-cycle cap. Batch 2 is valid, but its
    # shared topic cursor must remain behind deferred message 4 from batch 1.
    assert result.get("packet_max_seq") is None
    topic = st["telegram_engage"]["per_chat"]["@chata"]["topics"]["topic:77"]
    assert topic.get("spool_consumed_seq", 0) < 4


def test_third_delegate_without_draft_is_deferred_not_dropped(tmp_path):
    st = _two_chat_state()
    st["telegram_engage_chats"] = []
    rows = [
        {"message_id": mid, "snippet": f"@rain deep question {mid}?", "sender_id": mid,
         "matched_terms": ["@rain"], "topic_id": 88,
         "spool_seq": mid, "ts": 100.0 + mid}
        for mid in range(1, 4)
    ]
    packet = {"status": "ok", "max_seq": 3, "max_ts": 103.0,
              "matches": rows, "recent": []}
    replies = []

    def decider(prompt):
        mids = [int(value) for value in re.findall(r'"message_id": (\d+)', prompt)]
        return _decision(*(_assessment(mid, "delegate") for mid in mids))

    def cycle(now):
        return te.run_telegram_engage_cycle(
            drive_root=tmp_path,
            load_state=lambda: dict(st),
            save_state=lambda saved: st.update(saved),
            fetch_candidates=lambda *_args, **_kwargs: packet,
            run_decider=decider,
            compose_delegate=lambda candidate, *_args, **_kwargs: f"deep-{candidate.message_id}",
            do_reply=lambda *args: replies.append(args) or True,
            do_react=lambda *args: True,
            notify=lambda _text: None,
            now=now,
        )

    first = cycle(1000.0)
    assert [row[1] for row in replies] == [1, 2]
    assert first.get("packet_max_seq") is None
    second = cycle(1001.0)
    assert [row[1] for row in replies] == [1, 2, 3]
    assert second["packet_max_seq"] == 3


def test_bad_topic_does_not_block_later_healthy_and_is_quarantined(tmp_path):
    st = _two_chat_state()
    st["telegram_engage_chats"] = []
    alerts = []
    replies = []
    decider_calls = []

    def packet(include_later=True):
        rows = [
            {"message_id": 10, "snippet": "@rain broken topic", "sender_id": 10,
             "matched_terms": ["@rain"], "topic_id": 101,
             "spool_seq": 1, "ts": 101.0},
            {"message_id": 20, "snippet": "@rain healthy topic", "sender_id": 20,
             "matched_terms": ["@rain"], "topic_id": 202,
             "spool_seq": 2, "ts": 102.0},
        ]
        if include_later:
            rows.append(
                {"message_id": 30, "snippet": "@rain later healthy topic", "sender_id": 30,
                 "matched_terms": ["@rain"], "topic_id": 303,
                 "spool_seq": 3, "ts": 103.0}
            )
        return {"status": "ok", "max_seq": len(rows),
                "max_ts": 100.0 + len(rows), "matches": rows, "recent": []}

    current_packet = packet(include_later=False)

    def decider(prompt):
        if "broken topic" in prompt:
            decider_calls.append("bad")
            return ""
        mid = int(re.search(r'"message_id": (\d+)', prompt).group(1))
        decider_calls.append(mid)
        return _decision(_assessment(mid, "reply", text=f"ok-{mid}"))

    def cycle(now):
        return te.run_telegram_engage_cycle(
            drive_root=tmp_path,
            load_state=lambda: dict(st),
            save_state=lambda saved: st.update(saved),
            fetch_candidates=lambda *_args, **_kwargs: current_packet,
            run_decider=decider,
            do_reply=lambda *args: replies.append(args) or True,
            do_react=lambda *args: True,
            notify=alerts.append,
            now=now,
        )

    cycle(1000.0)
    assert [row[1] for row in replies] == [20]
    current_packet = packet(include_later=True)
    decider_calls.clear()
    cycle(1001.0)
    assert decider_calls == [30]
    assert [row[1] for row in replies] == [20, 30]

    cycle(1015.0)
    final = cycle(1045.0)
    assert final["packet_max_seq"] == 3
    assert final.get("retryable_failure") is None
    assert st["telegram_engage"]["per_chat"]["@chata"]["spool_consumed_seq"] == 3
    assert len(alerts) == 1 and "quarantined" in alerts[0]
    audit_rows = [
        json.loads(line)
        for line in (tmp_path / te.ACTION_LOG_REL).read_text(encoding="utf-8").splitlines()
    ]
    quarantine = [row for row in audit_rows if row.get("action") == "topic_quarantine"]
    assert len(quarantine) == 1
    assert quarantine[0]["topic_id"] == 101
    assert quarantine[0]["attempts"] == te.ENGAGE_TOPIC_INVALID_MAX_ATTEMPTS
