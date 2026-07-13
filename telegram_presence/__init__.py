"""telegram-presence: a group-chat presence organ for LLM agents.

Extracted from Rain (the Ouroboros project) — a live autonomous agent whose
group-chat behavior this package powered. The core loop decides *whether* the
agent wants to speak at all (want/depth gate), answers shallow things with a
light model draft, escalates substantive questions to a full delegate
composer, coalesces message bursts, respects per-chat caps and pauses, and
writes a decision log so under-delegation stays observable.

Since 0.3.0 the light decider runs a full *social read* per message
(addressed_to / referent / self_is_addressee / inner_thought / motivation),
keeps a per-chat entity glossary of third-party bots and personas so
sound-alike names are never self-attributed, treats Telegram forum topics as
hard scheduling and context lanes with their own cursors and quarantine, can
``wait`` on a moving conversational floor, and sends group replies/reactions
through a durable SQLite outbox with dead-letter tombstones.

Since 0.3.1 bundled transports preserve real Telegram forum-topic roots,
legacy group-action databases upgrade in place before the current natural-key
constraint is installed, and engage-cycle state persistence failures are
reported to the scheduler instead of looking like a fully durable success.

Everything host-specific (LLM calls, Telegram I/O, persona, state store) is
injected. Reusable outbound primitives add correlated envelopes and a durable,
bounded-retry outbox without importing Rain or a Telegram SDK.
"""
from telegram_presence import hooks
from telegram_presence.delivery import DeliveryRecord, DeliveryState, MessageEnvelope
from telegram_presence.engage import run_telegram_engage_cycle
from telegram_presence.inbox import GroupInbox, InboxAddResult, allowed_chats, matched_terms
from telegram_presence.group_delivery import (
    GroupActionDeadLettered,
    GroupActionOutbox,
    deliver_group_action,
)
from telegram_presence.outbox import DurableOutbox
from telegram_presence.roster import entities_block, list_entities, remember_entity

__version__ = "0.3.1"
__all__ = [
    "hooks",
    "run_telegram_engage_cycle",
    "GroupInbox",
    "InboxAddResult",
    "allowed_chats",
    "matched_terms",
    "MessageEnvelope",
    "DeliveryRecord",
    "DeliveryState",
    "DurableOutbox",
    "GroupActionOutbox",
    "GroupActionDeadLettered",
    "deliver_group_action",
    "remember_entity",
    "list_entities",
    "entities_block",
    "__version__",
]
