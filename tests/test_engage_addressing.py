"""A+B: group decider must see WHO speaks, HOW it addresses Rain, and WHO it
replies to — so she stops confusing messages aimed at others as aimed at her.
I/O layer only (voice card untouched)."""
from telegram_presence import engage as te
from telegram_presence.inbox import GroupInbox


def test_candidate_carries_sender_label():
    pkt = {"status": "ok", "recent": [], "matches": [
        {"message_id": 1, "snippet": "@rain hi", "sender_id": 7,
         "sender_username": "alice", "sender_name": "Alice", "matched_terms": ["@rain"]}]}
    c = te.candidates_from_packet(pkt)[0]
    assert c.sender_username == "alice"
    assert c.sender_name == "Alice"


def test_addressed_kind_mention_reply_name_only_none():
    pkt = {"status": "ok", "matches": [
        {"message_id": 1, "snippet": "@rain_ouroboros hey", "sender_id": 7, "matched_terms": ["@rain_ouroboros"]},
        {"message_id": 2, "snippet": "yes ok", "sender_id": 8, "matched_terms": ["reply_to_me"]},
        {"message_id": 3, "snippet": "Рейн молодец сегодня", "sender_id": 9, "matched_terms": ["рейн"]}],
        "recent": [{"message_id": 4, "snippet": "random chatter", "sender_id": 10}]}
    by = {c.message_id: c for c in te.candidates_from_packet(pkt)}
    assert by[1].addressed_kind == "mention"
    assert by[2].addressed_kind == "reply"
    assert by[3].addressed_kind == "name_only"
    assert by[4].addressed_kind == "none"


def test_mentions_other_flag():
    pkt = {"status": "ok",
           "matches": [{"message_id": 2, "snippet": "@rain and @bob look", "sender_id": 8, "matched_terms": ["@rain"]}],
           "recent": [{"message_id": 1, "snippet": "@gpt_bot what do you think", "sender_id": 7, "matched_terms": []}]}
    by = {c.message_id: c for c in te.candidates_from_packet(pkt)}
    assert by[1].mentions_other is True          # addressed to someone else
    assert by[2].mentions_other is True          # @bob present even though @rain too


def test_reply_target_resolved_from_packet():
    pkt = {"status": "ok", "recent": [], "matches": [
        {"message_id": 50, "snippet": "hi all", "sender_id": 7, "sender_username": "alice"},
        {"message_id": 51, "snippet": "agree", "sender_id": 8, "sender_username": "bob",
         "reply_to_msg_id": 50, "matched_terms": []}]}
    by = {c.message_id: c for c in te.candidates_from_packet(pkt)}
    assert by[51].reply_to_username == "alice"


def test_reply_to_me_flag():
    pkt = {"status": "ok", "recent": [], "matches": [
        {"message_id": 1, "snippet": "thanks", "sender_id": 7, "matched_terms": ["reply_to_me"]}]}
    assert te.candidates_from_packet(pkt)[0].reply_to_me is True


def test_decider_prompt_shows_sender_kind_replyto():
    pkt = {"status": "ok", "recent": [], "matches": [
        {"message_id": 1, "snippet": "@rain hi", "sender_id": 7,
         "sender_username": "alice", "sender_name": "Alice", "matched_terms": ["@rain"]}]}
    prompt = te.build_decider_prompt(te.candidates_from_packet(pkt))
    assert "alice" in prompt
    assert "mention" in prompt
    assert "from" in prompt


def test_inbox_row_stores_reply_to_msg_id(tmp_path):
    ib = GroupInbox(tmp_path)
    ib.add_message(chat="@c", message_id=2, sender_id=7, text="hi", reply_to_msg_id=1,
                   self_id=42, sender_username="alice", sender_name="Alice")
    assert ib.pending()[-1]["reply_to_msg_id"] == 1
