"""Transport adapters: the engage cycle is transport-agnostic — these wire it
to a concrete Telegram client. Two are provided:

- ``bot_api``  — official Bot API over stdlib urllib (no dependencies).
- ``telethon`` — a user-account client via an injected Telethon client
  (telethon itself is imported only by your wiring, never by this package).

Both expose the same surface: feed incoming group messages into a
``GroupInbox`` and provide ``do_reply(peer, msg_id, text)`` /
``do_react(peer, msg_id, emoji)`` for ``run_telegram_engage_cycle``.
"""
