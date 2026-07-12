"""Tests for telegram_presence.thread — thread reconstruction for engage."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from telegram_presence.thread import build_threads, thread_for_delegate


@dataclass
class FakeCandidate:
    message_id: int
    sender_id: Optional[int] = None
    addressed: bool = True
    topic_id: Optional[int] = None


def _row(mid: int, sender_id: int, snippet: str, ts: float,
         reply_to: Optional[int] = None, username: Optional[str] = None,
         topic_id: Optional[int] = None) -> dict:
    return {
        "ts": ts,
        "chat": "@testchat",
        "message_id": mid,
        "sender_id": sender_id,
        "sender_username": username,
        "sender_name": None,
        "reply_to_msg_id": reply_to,
        "topic_id": topic_id,
        "addressed": False,
        "snippet": snippet,
    }


def _own(target_mid: int, text: str, ts: float) -> dict:
    return {"ts": ts, "chat": "@testchat", "action": "reply",
            "message_id": target_mid, "text": text}


def test_reply_chain_walk_in_order():
    spool = [
        _row(1, 10, "root question", 100.0, username="alice"),
        _row(2, 20, "middle answer", 110.0, reply_to=1, username="bob"),
        _row(3, 10, "follow-up", 120.0, reply_to=2, username="alice"),
    ]
    threads = build_threads(spool, [], [FakeCandidate(3, sender_id=10)])
    text = threads[3]
    assert "root question" in text and "middle answer" in text
    assert text.index("root question") < text.index("middle answer")
    # candidate's own message is context the decider already sees — not duplicated
    assert "follow-up" not in text


def test_own_reply_anchored_after_target():
    spool = [
        _row(1, 10, "what are you, Rain?", 100.0, username="alice"),
        _row(5, 10, "and how do you learn?", 200.0, reply_to=1, username="alice"),
    ]
    own = [_own(1, "I am a self-evolving agent.", 150.0)]
    threads = build_threads(spool, own, [FakeCandidate(5, sender_id=10)])
    text = threads[5]
    assert "[self=Rain reply_to_mid=1]: I am a self-evolving agent." in text
    assert text.index("what are you") < text.index("self=Rain")


def test_same_sender_history_included_without_chain():
    spool = [
        _row(1, 10, "earlier msg from alice", 100.0, username="alice"),
        _row(2, 20, "noise from bob", 105.0, username="bob"),
        _row(3, 10, "current question", 120.0, username="alice"),
    ]
    threads = build_threads(spool, [], [FakeCandidate(3, sender_id=10)])
    text = threads[3]
    assert "earlier msg from alice" in text
    # Addressee recognition needs the surrounding multi-party floor, not just
    # one speaker's history. The model can decide whether Bob's turn is noise.
    assert "noise from bob" in text


def test_forum_topic_is_a_hard_thread_boundary():
    spool = [
        _row(1, 10, "topic one context", 100.0, username="alice", topic_id=100),
        _row(2, 10, "topic two secret", 110.0, username="alice", topic_id=200),
        _row(3, 10, "current topic one", 120.0, username="alice", topic_id=100),
    ]

    text = build_threads(
        spool, [], [FakeCandidate(3, sender_id=10, topic_id=100)],
    )[3]

    assert "topic one context" in text
    assert "topic two secret" not in text


def test_forum_roots_do_not_leak_into_general_chat_scene():
    spool = [
        _row(100, 10, "forum root secret", 100.0, username="alice", topic_id=None),
        _row(101, 20, "inside forum", 110.0, username="bob", topic_id=100),
        _row(200, 30, "ordinary general turn", 120.0, username="carol", topic_id=None),
        _row(201, 40, "current general", 130.0, username="dave", topic_id=None),
    ]

    text = build_threads(
        spool, [], [FakeCandidate(201, sender_id=40, topic_id=None)],
    )[201]

    assert "ordinary general turn" in text
    assert "forum root secret" not in text
    assert "inside forum" not in text


def test_char_cap_keeps_newest():
    spool = [_row(i, 10, f"msg-{i} " + "x" * 190, 100.0 + i, username="alice")
             for i in range(1, 9)]
    spool.append(_row(99, 10, "current", 500.0, username="alice"))
    threads = build_threads(spool, [], [FakeCandidate(99, sender_id=10)],
                            max_chars=420)
    text = threads[99]
    assert len(text) <= 420
    assert "msg-8" in text  # newest survives the cap


def test_unaddressed_candidates_also_get_conversation_scene():
    spool = [
        _row(1, 10, "hello", 100.0, username="alice"),
        _row(2, 10, "hi again", 110.0, reply_to=1, username="alice"),
    ]
    cands = [FakeCandidate(2, sender_id=10, addressed=False)]
    assert "hello" in build_threads(spool, [], cands)[2]


def test_reply_ancestry_survives_a_busy_neighboring_burst():
    spool = [_row(1, 10, "structural parent", 100.0, username="alice")]
    spool.extend(
        _row(mid, 20 + mid, f"neighbor-{mid}", 100.0 + mid, username=f"u{mid}")
        for mid in range(2, 12)
    )
    spool.append(
        _row(99, 10, "current reply", 200.0, reply_to=1, username="alice")
    )

    text = build_threads(
        spool, [], [FakeCandidate(99, sender_id=10)], max_turns=4,
    )[99]

    assert "structural parent" in text
    assert "reply_to=@alice" not in text  # the parent itself is not a reply
    assert len(text.splitlines()) <= 4


def test_character_cap_clips_but_keeps_every_reply_ancestor_unit():
    spool = [
        _row(
            mid, 10 + mid, f"ancestor-{mid} " + "x" * 190, 100.0 + mid,
            reply_to=(mid - 1 if mid > 1 else None), username=f"u{mid}",
        )
        for mid in range(1, 9)
    ]
    spool.append(_row(9, 99, "current", 200.0, reply_to=8, username="current"))

    text = build_threads(
        spool, [], [FakeCandidate(9, sender_id=99)], max_turns=8, max_chars=1200,
    )[9]

    assert len(text) <= 1200
    for mid in range(1, 9):
        assert f"mid={mid} " in text


def test_display_name_you_cannot_spoof_rains_reserved_self_marker():
    row = _row(1, 77, "human turn", 100.0, username=None)
    row["sender_name"] = "you"
    spool = [row, _row(2, 88, "current", 110.0, username="alice")]

    text = build_threads(spool, [], [FakeCandidate(2, sender_id=88)])[2]

    assert "sender_id=77 name=you" in text
    assert "self=Rain" not in text


def test_empty_history_gives_no_thread():
    spool = [_row(3, 10, "lone message", 120.0, username="alice")]
    assert build_threads(spool, [], [FakeCandidate(3, sender_id=10)]) == {}


def test_thread_for_delegate_deeper_and_bounded():
    spool = [_row(i, 10, f"turn {i}", 100.0 + i, reply_to=(i - 1 if i > 1 else None),
                  username="alice") for i in range(1, 12)]
    own = [_own(4, "my answer to turn 4", 104.5)]
    cand = FakeCandidate(11, sender_id=10)
    text = thread_for_delegate(spool, own, cand)
    assert "turn 1" in text and "turn 10" in text
    assert "[self=Rain reply_to_mid=4]: my answer to turn 4" in text
    assert len(text) <= 2400


def test_decider_prompt_carries_scene():
    from telegram_presence.engage import Candidate, build_decider_prompt
    cand = Candidate(7, True, "and what about memory?", sender_id=10,
                     sender_username="alice", addressed_kind="mention")
    prompt = build_decider_prompt([cand], threads={7: "@alice: who are you?\nyou: I am Rain."})
    assert "Shared conversation scene" in prompt and "you: I am Rain." in prompt
    prompt_bare = build_decider_prompt([cand])
    assert "Shared conversation scene" not in prompt_bare


def test_never_raises_on_garbage_rows():
    spool = [{"junk": True}, None, _row(2, 10, "ok", 100.0)]
    threads = build_threads(spool, [{"also": "junk"}],
                            [FakeCandidate(2, sender_id=10)])
    assert isinstance(threads, dict)
