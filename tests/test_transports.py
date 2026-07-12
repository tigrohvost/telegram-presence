"""Both transports must drive the same engage cycle: Bot API (stdlib urllib)
and Telethon (user client, injected object — no telethon import needed here).

The package core is transport-agnostic (do_reply/do_react callables); these
tests prove each adapter (a) feeds group messages into GroupInbox and (b)
performs an anchored reply and a reaction through its own wire format.
"""
import asyncio
import gc
import json
import threading
import time
import warnings

from telegram_presence import hooks
from telegram_presence.delivery import DeliveryState, MessageEnvelope
from telegram_presence.inbox import GroupInbox
from telegram_presence.outbox import DurableOutbox
from telegram_presence.transports.bot_api import BotApiTransport
from telegram_presence.transports.telethon import TelethonTransport
from telegram_presence import engage as te


# --- Bot API -----------------------------------------------------------------

class _FakeHttp:
    """Records Bot API calls; returns canned getUpdates JSON."""

    def __init__(self, updates):
        self.updates = updates
        self.calls = []

    def __call__(self, url, data=None, timeout=None):
        self.calls.append((url, json.loads(data.decode("utf-8")) if data else None))
        if "getUpdates" in url:
            body = {"ok": True, "result": self.updates}
        else:
            body = {"ok": True, "result": {}}
        import io
        return io.BytesIO(json.dumps(body).encode("utf-8"))


def _bot_update(mid, text, uid=7, username="anatoli", chat="@examplechat"):
    return {
        "update_id": 1000 + mid,
        "message": {
            "message_id": mid,
            "from": {"id": uid, "username": username, "first_name": "Anatoli"},
            "chat": {"id": -100123, "type": "supergroup", "username": chat.lstrip("@")},
            "date": 1783590000,
            "text": text,
        },
    }


def test_bot_api_poll_feeds_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_state_loader",
                        lambda: {"telegram_mentions_chat": "@examplechat"})
    http = _FakeHttp([_bot_update(11, "@rain what do you think?")])
    inbox = GroupInbox(tmp_path)
    t = BotApiTransport(token="TESTTOKEN", inbox=inbox, http=http, self_id=999)
    n = t.poll_updates()
    assert n == 1
    rows = inbox.pending(after_ts=0.0)
    assert rows and rows[0]["message_id"] == 11
    assert rows[0]["chat"] == "@examplechat"
    assert rows[0]["addressed"] is True


def test_bot_api_retains_offset_until_inbox_append_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_state_loader",
                        lambda: {"telegram_mentions_chat": "@examplechat"})
    updates = [_bot_update(11, "@rain first"), _bot_update(12, "@rain second")]
    http = _FakeHttp(updates)
    inbox = GroupInbox(tmp_path)
    real_append = inbox._append
    monkeypatch.setattr(
        inbox,
        "_append",
        lambda _row: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    transport = BotApiTransport(token="TESTTOKEN", inbox=inbox, http=http)

    assert transport.poll_updates() == 0
    assert transport._offset == 0
    assert inbox.pending() == []

    monkeypatch.setattr(inbox, "_append", real_append)
    assert transport.poll_updates() == 2
    assert transport._offset == 1013
    assert [row["message_id"] for row in inbox.pending()] == [11, 12]


def test_bot_api_advances_offset_for_durable_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_state_loader",
                        lambda: {"telegram_mentions_chat": "@examplechat"})
    update = _bot_update(13, "@rain replay")
    original = GroupInbox(tmp_path)
    assert original.add_message(
        chat="@examplechat",
        message_id=13,
        sender_id=7,
        sender_username="anatoli",
        sender_name="Anatoli",
        text="@rain replay",
        reply_to_msg_id=None,
        self_id=None,
    ) is True

    restarted = GroupInbox(tmp_path)
    transport = BotApiTransport(
        token="TESTTOKEN", inbox=restarted, http=_FakeHttp([update])
    )
    assert transport.poll_updates() == 0
    assert transport._offset == 1014
    assert [row["message_id"] for row in restarted.pending()] == [13]


def test_bot_api_preserves_custom_add_message_override(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_state_loader",
                        lambda: {"telegram_mentions_chat": "@examplechat"})

    class _CustomInbox(GroupInbox):
        def __init__(self, root):
            super().__init__(root)
            self.calls = 0

        def add_message(self, **_values):
            self.calls += 1
            return False

    inbox = _CustomInbox(tmp_path)
    transport = BotApiTransport(
        token="TESTTOKEN", inbox=inbox, http=_FakeHttp([_bot_update(14, "@rain")])
    )

    assert transport.poll_updates() == 0
    assert inbox.calls == 1
    assert transport._offset == 1015


def test_bot_api_acks_and_ignores_malformed_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_state_loader",
                        lambda: {"telegram_mentions_chat": "@examplechat"})
    malformed_message = {"update_id": 2001, "message": "not-a-dict"}
    missing_id = _bot_update(15, "@rain missing id")
    missing_id["update_id"] = 2002
    missing_id["message"].pop("message_id")
    bad_reply = _bot_update(16, "@rain bad reply")
    bad_reply["update_id"] = 2003
    bad_reply["message"]["reply_to_message"] = "not-a-dict"
    valid = _bot_update(17, "@rain valid")
    valid["update_id"] = 2004
    inbox = GroupInbox(tmp_path)
    transport = BotApiTransport(
        token="TESTTOKEN",
        inbox=inbox,
        http=_FakeHttp([malformed_message, missing_id, bad_reply, valid]),
    )

    assert transport.poll_updates() == 1
    assert transport._offset == 2005
    assert [row["message_id"] for row in inbox.pending()] == [17]


def test_bot_api_reply_is_anchored_and_react_hits_endpoint(tmp_path):
    http = _FakeHttp([])
    t = BotApiTransport(token="TESTTOKEN", inbox=GroupInbox(tmp_path), http=http)
    assert t.do_reply("@examplechat", 42, "hello") is True
    url, payload = http.calls[-1]
    assert "sendMessage" in url and "TESTTOKEN" in url
    assert payload["chat_id"] == "@examplechat"
    assert payload["reply_parameters"]["message_id"] == 42
    assert payload["text"] == "hello"

    assert t.do_react("@examplechat", 42, "👍") is True
    url, payload = http.calls[-1]
    assert "setMessageReaction" in url
    assert payload["message_id"] == 42
    assert payload["reaction"] == [{"type": "emoji", "emoji": "👍"}]


def test_bot_api_standalone_message_has_no_anchor(tmp_path):
    http = _FakeHttp([])
    t = BotApiTransport(token="TESTTOKEN", inbox=GroupInbox(tmp_path), http=http)
    assert t.do_reply("@examplechat", 0, "standalone") is True
    _url, payload = http.calls[-1]
    assert "reply_parameters" not in payload


def test_bot_api_semantically_chunks_without_silent_truncation(tmp_path):
    http = _FakeHttp([])
    t = BotApiTransport(token="TESTTOKEN", inbox=GroupInbox(tmp_path), http=http)
    text = ("слово " * 900).strip()
    assert len(text) > 4096
    assert t.do_reply("@examplechat", 42, text) is True
    sends = [payload for url, payload in http.calls if "sendMessage" in url]
    assert len(sends) == 2
    assert all(len(payload["text"]) <= 4096 for payload in sends)
    assert sends[0]["reply_parameters"]["message_id"] == 42
    assert "reply_parameters" not in sends[1]
    assert " ".join(payload["text"] for payload in sends) == text


def test_bot_api_rejects_over_chunk_bound_before_transport_io(tmp_path):
    http = _FakeHttp([])
    t = BotApiTransport(token="TESTTOKEN", inbox=GroupInbox(tmp_path), http=http,
                        max_text_chunks=1)
    assert t.do_reply("@examplechat", 42, "x" * 5000) is False
    assert http.calls == []


def test_bot_api_partial_chunk_failure_retries_whole_envelope(tmp_path):
    class _PartialHttp:
        def __init__(self):
            self.calls = []
            self.accepted = []
            self.fail_on = 2
            self.send_count = 0

        def __call__(self, url, data=None, timeout=None):
            import io
            payload = json.loads(data.decode("utf-8"))
            self.calls.append((url, payload))
            self.send_count += 1
            if self.send_count == self.fail_on:
                body = {"ok": False, "description": "temporary"}
            else:
                self.accepted.append(payload["text"])
                body = {"ok": True, "result": {}}
            return io.BytesIO(json.dumps(body).encode("utf-8"))

    now = [0.0]
    http = _PartialHttp()
    transport = BotApiTransport(token="TESTTOKEN", inbox=GroupInbox(tmp_path / "inbox"),
                                http=http)
    outbox = DurableOutbox(tmp_path / "outbox", clock=lambda: now[0],
                           base_retry_seconds=1)
    text = ("слово " * 900).strip()
    queued = outbox.enqueue(MessageEnvelope(
        transport="bot_api", peer="@examplechat", kind="reply", text=text,
        reply_to_message_id=42, idempotency_key="partial-prefix", created_at=0,
    ))
    assert outbox.dispatch_one(queued.delivery_id,
                               transport.send_envelope).state == DeliveryState.FAILED
    assert len(http.accepted) == 1

    http.fail_on = None
    now[0] = 1.0
    assert outbox.dispatch_one(queued.delivery_id,
                               transport.send_envelope).state == DeliveryState.ACKED
    assert len(http.accepted) == 3
    assert http.accepted[0] == http.accepted[1]  # at-least-once prefix duplicate


def test_bot_api_exception_log_does_not_leak_token_or_url(tmp_path, caplog):
    def failing_http(_url, data=None, timeout=None):
        raise RuntimeError("SUPERSECRET exception detail")

    transport = BotApiTransport(token="SUPERSECRET", inbox=GroupInbox(tmp_path),
                                http=failing_http)
    assert transport.do_reply("@examplechat", 42, "hello") is False
    assert "SUPERSECRET" not in caplog.text
    assert "api.telegram.org" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_transport_timeouts_must_be_positive_and_finite(tmp_path):
    import math
    import pytest

    for value in (0, -1, math.inf, math.nan, True):
        with pytest.raises(ValueError):
            BotApiTransport(token="T", inbox=GroupInbox(tmp_path / "b"), timeout=value)
        with pytest.raises(ValueError):
            TelethonTransport(client=_FakeTelethonClient(),
                              inbox=GroupInbox(tmp_path / "t"), send_timeout=value)


def test_bot_api_envelope_round_trip_through_durable_outbox(tmp_path):
    http = _FakeHttp([])
    transport = BotApiTransport(token="TESTTOKEN", inbox=GroupInbox(tmp_path / "inbox"),
                                http=http)
    outbox = DurableOutbox(tmp_path / "outbox")
    queued = outbox.enqueue(MessageEnvelope(
        transport="bot_api", peer="@examplechat", kind="reply", text="durable",
        reply_to_message_id=42, idempotency_key="example-42",
    ))
    settled = outbox.dispatch_one(queued.delivery_id, transport.send_envelope)
    assert settled.state == DeliveryState.ACKED
    assert any(payload and payload.get("text") == "durable"
               for _url, payload in http.calls)


# --- Telethon ----------------------------------------------------------------

class _FakeTelethonClient:
    def __init__(self):
        self.sent = []

    async def get_entity(self, peer):
        return f"entity:{peer}"

    async def send_message(self, entity, text, reply_to=None):
        self.sent.append((entity, text, reply_to))

    async def __call__(self, request):
        self.sent.append(("raw_request", request))


def test_telethon_reply_is_anchored(tmp_path):
    client = _FakeTelethonClient()
    loop = asyncio.new_event_loop()
    try:
        t = TelethonTransport(client=client, inbox=GroupInbox(tmp_path), loop=loop)
        assert t.do_reply("@examplechat", 42, "hello") is True
        assert client.sent == [("entity:@examplechat", "hello", 42)]
        assert t.do_reply("@examplechat", 0, "standalone") is True
        assert client.sent[-1] == ("entity:@examplechat", "standalone", None)
    finally:
        loop.close()


def test_telethon_semantically_chunks_and_sends_envelope(tmp_path):
    client = _FakeTelethonClient()
    loop = asyncio.new_event_loop()
    try:
        t = TelethonTransport(client=client, inbox=GroupInbox(tmp_path), loop=loop)
        text = ("слово " * 900).strip()
        receipt = t.send_envelope(MessageEnvelope(
            transport="telethon", peer="@examplechat", kind="reply", text=text,
            reply_to_message_id=42,
        ))
        assert receipt.success is True
        assert len(client.sent) == 2
        assert client.sent[0][2] == 42 and client.sent[1][2] is None
        assert " ".join(item[1] for item in client.sent) == text
    finally:
        loop.close()


def test_telethon_sync_send_on_running_loop_fails_without_ghost_send(tmp_path):
    client = _FakeTelethonClient()

    async def scenario():
        loop = asyncio.get_running_loop()
        t = TelethonTransport(client=client, inbox=GroupInbox(tmp_path), loop=loop,
                              send_timeout=0.01)
        assert t.do_reply("@examplechat", 42, "must not escape later") is False
        await asyncio.sleep(0)
        assert client.sent == []

    asyncio.run(scenario())


def test_telethon_cross_thread_timeout_cancels_pending_send(tmp_path):
    class _SlowClient(_FakeTelethonClient):
        async def send_message(self, entity, text, reply_to=None):
            await asyncio.sleep(0.05)
            self.sent.append((entity, text, reply_to))

    client = _SlowClient()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    try:
        t = TelethonTransport(client=client, inbox=GroupInbox(tmp_path), loop=loop,
                              send_timeout=0.01)
        assert t.do_reply("@examplechat", 42, "cancel me") is False
        time.sleep(0.08)
        assert client.sent == []
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1)
        loop.close()


def test_telethon_closed_loop_failure_closes_unscheduled_coroutine(tmp_path):
    loop = asyncio.new_event_loop()
    loop.close()
    transport = TelethonTransport(client=_FakeTelethonClient(),
                                  inbox=GroupInbox(tmp_path), loop=loop)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert transport.do_reply("@examplechat", 42, "closed") is False
        gc.collect()
    assert not any("never awaited" in str(item.message) for item in caught)


def test_telethon_event_feeds_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_state_loader",
                        lambda: {"telegram_mentions_chat": "@examplechat"})
    client = _FakeTelethonClient()
    loop = asyncio.new_event_loop()
    try:
        inbox = GroupInbox(tmp_path)
        t = TelethonTransport(client=client, inbox=inbox, loop=loop, self_id=999)

        class _Msg:
            id = 21
            message = "@rain и как оно?"
            reply_to_msg_id = None

        class _Sender:
            id = 7
            username = "anatoli"
            first_name = "Anatoli"
            last_name = ""

        class _Chat:
            username = "examplechat"

        class _Event:
            message = _Msg()
            chat = _Chat()
            chat_id = -100123
            raw_text = _Msg.message

            async def get_sender(self):
                return _Sender()

        loop.run_until_complete(t.on_group_message(_Event()))
        rows = inbox.pending(after_ts=0.0)
        assert rows and rows[0]["message_id"] == 21 and rows[0]["addressed"] is True
    finally:
        loop.close()


def test_telethon_event_can_retry_after_inbox_append_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_state_loader",
                        lambda: {"telegram_mentions_chat": "@examplechat"})
    client = _FakeTelethonClient()
    loop = asyncio.new_event_loop()
    try:
        inbox = GroupInbox(tmp_path)
        real_append = inbox._append
        monkeypatch.setattr(
            inbox,
            "_append",
            lambda _row: (_ for _ in ()).throw(OSError("disk unavailable")),
        )
        transport = TelethonTransport(client=client, inbox=inbox, loop=loop)

        class _Msg:
            id = 22
            message = "@rain retry"
            reply_to_msg_id = None

        class _Sender:
            id = 7
            username = "anatoli"
            first_name = "Anatoli"
            last_name = ""

        class _Chat:
            username = "examplechat"

        class _Event:
            message = _Msg()
            chat = _Chat()
            chat_id = -100123
            raw_text = _Msg.message

            async def get_sender(self):
                return _Sender()

        event = _Event()
        assert loop.run_until_complete(transport.on_group_message(event)) is False
        monkeypatch.setattr(inbox, "_append", real_append)
        assert loop.run_until_complete(transport.on_group_message(event)) is True
        assert [row["message_id"] for row in inbox.pending()] == [22]
    finally:
        loop.close()


# --- same engage cycle drives either transport -------------------------------

def _run_cycle_with(do_reply, packet, drive_root):
    return te.run_telegram_engage_cycle(
        drive_root=drive_root,
        load_state=lambda: {"telegram_engage_enabled": True, "autonomy_enabled": True,
                            "owner_chat_id": 1, "telegram_engage": {},
                            "telegram_mentions_chat": "@examplechat"},
        save_state=lambda s: None,
        fetch_candidates=lambda dr, chat=None, after_ts=None: packet,
        run_decider=lambda p: (
            '[{"message_id":11,"action":"reply","text":"ответ","want":"yes","depth":"quick",'
            '"addressed_to":"self","addressed_to_entity":"","self_is_addressee":"yes",'
            '"self_is_referent":"yes","address_confidence":0.9,"context_sufficient":0.9,'
            '"referent":"the agent","inner_thought":"greet back","motivation":"direct hello"}]'
        ),
        do_reply=do_reply,
        do_react=lambda *a: True,
        notify=lambda t: None, now=1000.0)


def test_cycle_replies_through_bot_api_and_telethon(tmp_path):
    packet = {"status": "ok",
              "matches": [{"message_id": 11, "snippet": "@rain hi", "sender_id": 7,
                           "chat": "@examplechat", "addressed": True}],
              "recent": []}

    http = _FakeHttp([])
    bot = BotApiTransport(token="TESTTOKEN", inbox=GroupInbox(tmp_path / "b"), http=http)
    res = _run_cycle_with(bot.do_reply, packet, tmp_path / "b")
    assert res["status"] == "acted"
    assert any("sendMessage" in u for u, _ in http.calls)

    client = _FakeTelethonClient()
    loop = asyncio.new_event_loop()
    try:
        tt = TelethonTransport(client=client, inbox=GroupInbox(tmp_path / "t"), loop=loop)
        res = _run_cycle_with(tt.do_reply, packet, tmp_path / "t")
        assert res["status"] == "acted"
        assert client.sent and client.sent[0][2] == 11  # anchored to msg 11
    finally:
        loop.close()
