"""Authorization primitives for optional owner-only private transports.

The group presence APIs remain governed by :func:`inbox.allowed_chats`; this
module intentionally does not add a private route to the engage cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _numeric_user_id(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive numeric Telegram user id")
    return value


@dataclass(frozen=True, slots=True)
class OwnerPrivateChatPolicy:
    """Immutable policy that authorizes exactly one numeric owner user id.

    Usernames and display names are deliberately absent from this contract:
    both can change and neither is an authorization identity.
    """

    owner_user_id: int
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner_user_id",
                           _numeric_user_id(self.owner_user_id, "owner_user_id"))
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be boolean")

    def allows_private(self, sender_user_id: Any, *, chat_type: str = "private") -> bool:
        """Return true only for the configured owner in a Telegram private chat."""
        if not self.enabled or chat_type != "private":
            return False
        try:
            sender = _numeric_user_id(sender_user_id, "sender_user_id")
        except ValueError:
            return False
        return sender == self.owner_user_id

    def require_private(self, sender_user_id: Any, *, chat_type: str = "private") -> None:
        """Raise ``PermissionError`` unless :meth:`allows_private` succeeds."""
        if not self.allows_private(sender_user_id, chat_type=chat_type):
            raise PermissionError("private Telegram chat is not authorized")
