"""telegram-presence: a group-chat presence organ for LLM agents.

Extracted from Rain (the Ouroboros project) — a live autonomous agent whose
group-chat behavior this package powered. The core loop decides *whether* the
agent wants to speak at all (want/depth gate), answers shallow things with a
light model draft, escalates substantive questions to a full delegate
composer, coalesces message bursts, respects per-chat caps and pauses, and
writes a decision log so under-delegation stays observable.

Everything host-specific (LLM calls, Telegram I/O, persona, state store) is
injected: see `telegram_presence.hooks.configure` and the callables taken by
`telegram_presence.engage.run_telegram_engage_cycle`.
"""
from telegram_presence import hooks
from telegram_presence.engage import run_telegram_engage_cycle
from telegram_presence.inbox import GroupInbox, allowed_chats, matched_terms

__version__ = "0.1.0"
__all__ = ["hooks", "run_telegram_engage_cycle", "GroupInbox",
           "allowed_chats", "matched_terms", "__version__"]
