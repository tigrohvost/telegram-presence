"""List 16 slice 2b: per-chat engage caps with state override."""
from telegram_presence.engage import (
    ENGAGE_ADDRESSED_REPLY_DAILY_CAP,
    ENGAGE_REPLY_DAILY_CAP,
    PER_CHAT_ADDRESSED_REPLY_DAILY_CAP,
    PER_CHAT_REPLY_DAILY_CAP,
    _chat_caps,
    _check_caps,
    _global_caps,
)


def test_defaults_without_override():
    assert _chat_caps({}, "@chat") == (
        PER_CHAT_REPLY_DAILY_CAP, PER_CHAT_ADDRESSED_REPLY_DAILY_CAP)


def test_override_by_handle_with_and_without_at():
    st = {"telegram_engage_chat_caps": {"@ouroborosagent": {"addressed": 20, "reply": 6}}}
    assert _chat_caps(st, "@ouroborosagent") == (6, 20)
    st2 = {"telegram_engage_chat_caps": {"ouroborosagent": {"addressed": 15}}}
    assert _chat_caps(st2, "@ouroborosagent") == (PER_CHAT_REPLY_DAILY_CAP, 15)


def test_override_clamped_against_bad_state_writes():
    st = {"telegram_engage_chat_caps": {"@c": {"addressed": 100000, "reply": -5}}}
    reply, addressed = _chat_caps(st, "@c")
    assert reply == 0 and addressed == 100


def test_override_garbage_falls_back():
    st = {"telegram_engage_chat_caps": {"@c": {"addressed": "many", "reply": None}}}
    assert _chat_caps(st, "@c") == (
        PER_CHAT_REPLY_DAILY_CAP, PER_CHAT_ADDRESSED_REPLY_DAILY_CAP)


def test_check_caps_uses_override():
    st = {
        "telegram_engage": {
            "day_key": "2026-07-06",
            "addressed_reply_count_today": 6,
            "per_chat": {"@busy": {"day_key": "2026-07-06",
                                   "reply_count": 0, "addressed_count": 6}},
        },
        "telegram_engage_chat_caps": {"@busy": {"addressed": 20}},
    }
    ok, reason = _check_caps(st, "@busy", is_addressed=True)
    assert ok, reason  # default cap (6) would block; override (20) lets it through


def test_check_caps_global_ceiling_still_wins():
    st = {
        "telegram_engage": {
            "day_key": "2026-07-06",
            "addressed_reply_count_today": ENGAGE_ADDRESSED_REPLY_DAILY_CAP,
            "per_chat": {"@busy": {"day_key": "2026-07-06",
                                   "reply_count": 0, "addressed_count": 6}},
        },
        "telegram_engage_chat_caps": {"@busy": {"addressed": 90}},
    }
    ok, reason = _check_caps(st, "@busy", is_addressed=True)
    assert not ok and "global" in reason


# --- adaptive global caps (2026-07-08) ---

def test_addressed_defaults_raised_for_demand():
    assert ENGAGE_ADDRESSED_REPLY_DAILY_CAP == 40
    assert PER_CHAT_ADDRESSED_REPLY_DAILY_CAP == 12


def test_global_caps_defaults():
    assert _global_caps({}) == (ENGAGE_REPLY_DAILY_CAP, ENGAGE_ADDRESSED_REPLY_DAILY_CAP)


def test_global_caps_override_and_clamp():
    st = {"telegram_engage_global_caps": {"reply": 50, "addressed": 500}}
    assert _global_caps(st) == (20, 100)
    st2 = {"telegram_engage_global_caps": {"addressed": 60}}
    assert _global_caps(st2) == (ENGAGE_REPLY_DAILY_CAP, 60)
    st3 = {"telegram_engage_global_caps": {"addressed": "many"}}
    assert _global_caps(st3) == (ENGAGE_REPLY_DAILY_CAP, ENGAGE_ADDRESSED_REPLY_DAILY_CAP)


def test_check_caps_respects_global_override():
    st = {
        "telegram_engage": {
            "day_key": "2026-07-06",
            "addressed_reply_count_today": ENGAGE_ADDRESSED_REPLY_DAILY_CAP + 5,
            "per_chat": {"@busy": {"day_key": "2026-07-06",
                                   "reply_count": 0, "addressed_count": 0}},
        },
        "telegram_engage_chat_caps": {"@busy": {"addressed": 90}},
        "telegram_engage_global_caps": {"addressed": 80},
    }
    ok, reason = _check_caps(st, "@busy", is_addressed=True)
    assert ok, reason
    st["telegram_engage_global_caps"] = {"addressed": 10}
    ok, reason = _check_caps(st, "@busy", is_addressed=True)
    assert not ok and "global" in reason
