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


# --- cycle over two chats ---

def _decider(prompt: str) -> str:
    ids = {int(m) for m in re.findall(r'"message_id": (\d+)', prompt)}
    return json.dumps([{"message_id": i, "action": "reply", "text": f"hi-{i}"}
                       for i in sorted(ids)])


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
