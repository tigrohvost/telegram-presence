"""Both transports must drive the same engage cycle: Bot API (stdlib urllib)
and Telethon (user client, injected object — no telethon import needed here).

The package core is transport-agnostic (do_reply/do_react callables); these
tests prove each adapter (a) feeds group messages into GroupInbox and (b)
performs an anchored reply and a reaction through its own wire format.
"""
import asyncio
import json

from telegram_presence import hooks
from telegram_presence.inbox import GroupInbox
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


# --- same engage cycle drives either transport -------------------------------

def _run_cycle_with(do_reply, packet, drive_root):
    return te.run_telegram_engage_cycle(
        drive_root=drive_root,
        load_state=lambda: {"telegram_engage_enabled": True, "autonomy_enabled": True,
                            "owner_chat_id": 1, "telegram_engage": {},
                            "telegram_mentions_chat": "@examplechat"},
        save_state=lambda s: None,
        fetch_candidates=lambda dr, chat=None, after_ts=None: packet,
        run_decider=lambda p: '[{"message_id":11,"action":"reply","text":"ответ","want":"yes","depth":"quick"}]',
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
