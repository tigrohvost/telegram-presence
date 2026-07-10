"""Transport-aware outbound message and delivery records."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import math
import time
from typing import Any, Mapping, Optional, Union
from uuid import uuid4

from telegram_presence.content import MediaDescriptor


Peer = Union[int, str]
ENVELOPE_KINDS = frozenset({"media", "message", "notification", "reaction", "reply"})


class DeliveryState(str, Enum):
    PENDING = "pending"
    SENDING = "sending"
    ACKED = "acked"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


def _identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    cleaned = value.strip()
    if len(cleaned) > 256:
        raise ValueError(f"{field_name} exceeds 256 characters")
    return cleaned


def _timestamp(value: Optional[float], field_name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite timestamp")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field_name} must be a finite timestamp")
    return result


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    """A durable outbound intent independent of a specific SDK.

    Correlation identifies a wider operation, causation points at the event
    that caused this message, and idempotency prevents duplicate enqueue when
    the caller supplies the same stable key after a restart.
    """

    transport: str
    peer: Peer
    kind: str = "message"
    text: str = ""
    reply_to_message_id: Optional[int] = None
    media: Optional[MediaDescriptor] = None
    correlation_id: str = field(default_factory=lambda: uuid4().hex)
    causation_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    owner_user_id: Optional[int] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    envelope_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        transport = _identifier(self.transport, "transport").lower()
        kind = _identifier(self.kind, "kind").lower()
        if kind not in ENVELOPE_KINDS:
            raise ValueError(f"unsupported envelope kind: {kind}")
        if (isinstance(self.peer, bool)
                or not isinstance(self.peer, (int, str))
                or (isinstance(self.peer, int) and self.peer == 0)
                or (isinstance(self.peer, str) and not self.peer.strip())):
            raise ValueError("peer must be a numeric id or non-empty handle")
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        if kind in ("message", "notification", "reaction", "reply") and not self.text.strip():
            raise ValueError(f"{kind} envelope requires non-empty text")
        if kind == "media" and self.media is None:
            raise ValueError("media envelope requires a media descriptor")
        if self.media is not None and not isinstance(self.media, MediaDescriptor):
            raise ValueError("media must be a MediaDescriptor")
        if self.media is not None and kind != "media":
            raise ValueError("media descriptor is only valid for kind=media")
        if kind == "reaction" and not self.text.strip():
            raise ValueError("reaction envelope requires emoji text")

        reply_to = self.reply_to_message_id
        if reply_to is not None:
            if isinstance(reply_to, bool) or not isinstance(reply_to, int) or reply_to < 1:
                raise ValueError("reply_to_message_id must be a positive integer")
        if kind == "reaction" and reply_to is None:
            raise ValueError("reaction envelope requires reply_to_message_id")
        if kind == "reply" and reply_to is None:
            raise ValueError("reply envelope requires reply_to_message_id")
        owner = self.owner_user_id
        if owner is not None:
            if isinstance(owner, bool) or not isinstance(owner, int) or owner < 1:
                raise ValueError("owner_user_id must be a positive numeric id")

        envelope_id = _identifier(self.envelope_id, "envelope_id")
        correlation_id = _identifier(self.correlation_id, "correlation_id")
        causation_id = (None if self.causation_id is None
                        else _identifier(self.causation_id, "causation_id"))
        idempotency_key = (envelope_id if self.idempotency_key is None
                           else _identifier(self.idempotency_key, "idempotency_key"))
        created_at = _timestamp(self.created_at, "created_at")
        if created_at is None:
            raise ValueError("created_at must be a finite timestamp")

        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        metadata = dict(self.metadata)
        if any(not isinstance(key, str) for key in metadata):
            raise ValueError("metadata keys must be strings")
        try:
            metadata = json.loads(json.dumps(metadata, ensure_ascii=False,
                                             allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata must be JSON-serializable") from exc

        object.__setattr__(self, "transport", transport)
        object.__setattr__(self, "peer", self.peer.strip() if isinstance(self.peer, str)
                           else self.peer)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "envelope_id", envelope_id)
        object.__setattr__(self, "correlation_id", correlation_id)
        object.__setattr__(self, "causation_id", causation_id)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "metadata", metadata)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "envelope_id": self.envelope_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "idempotency_key": self.idempotency_key,
            "transport": self.transport,
            "peer": self.peer,
            "kind": self.kind,
            "text": self.text,
            "reply_to_message_id": self.reply_to_message_id,
            "media": self.media.to_dict() if self.media else None,
            "owner_user_id": self.owner_user_id,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MessageEnvelope":
        if value.get("schema_version") != 1:
            raise ValueError("unsupported MessageEnvelope schema_version")
        media = value.get("media")
        return cls(
            transport=value["transport"],
            peer=value["peer"],
            kind=value.get("kind", "message"),
            text=value.get("text", ""),
            reply_to_message_id=value.get("reply_to_message_id"),
            media=MediaDescriptor.from_dict(media) if media else None,
            correlation_id=value["correlation_id"],
            causation_id=value.get("causation_id"),
            idempotency_key=value.get("idempotency_key"),
            owner_user_id=value.get("owner_user_id"),
            metadata=value.get("metadata") or {},
            created_at=value["created_at"],
            envelope_id=value["envelope_id"],
        )


@dataclass(frozen=True, slots=True)
class TransportReceipt:
    """Explicit transport result consumed by the durable outbox."""

    success: bool
    transport: str
    correlation_id: str
    transport_message_id: Optional[str] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise ValueError("success must be boolean")
        object.__setattr__(self, "transport", _identifier(self.transport, "transport").lower())
        object.__setattr__(self, "correlation_id",
                           _identifier(self.correlation_id, "correlation_id"))
        if self.transport_message_id is not None:
            object.__setattr__(self, "transport_message_id",
                               str(self.transport_message_id))
        if self.error is not None:
            object.__setattr__(self, "error", str(self.error)[:1000])

    def __bool__(self) -> bool:
        return self.success


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    """Persisted state for one attemptable envelope."""

    envelope: MessageEnvelope
    state: DeliveryState = DeliveryState.PENDING
    attempts: int = 0
    next_attempt_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    sending_started_at: Optional[float] = None
    acked_at: Optional[float] = None
    last_error: Optional[str] = None
    transport_message_id: Optional[str] = None
    lease_id: Optional[str] = None
    delivery_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, MessageEnvelope):
            raise ValueError("envelope must be a MessageEnvelope")
        state = self.state
        if not isinstance(state, DeliveryState):
            try:
                state = DeliveryState(str(state))
            except ValueError as exc:
                raise ValueError(f"unsupported delivery state: {state}") from exc
        if (isinstance(self.attempts, bool) or not isinstance(self.attempts, int)
                or self.attempts < 0):
            raise ValueError("attempts must be a non-negative integer")

        object.__setattr__(self, "state", state)
        object.__setattr__(self, "delivery_id",
                           _identifier(self.delivery_id, "delivery_id"))
        for name in ("next_attempt_at", "created_at", "updated_at",
                     "sending_started_at", "acked_at"):
            object.__setattr__(self, name, _timestamp(getattr(self, name), name))
        if self.created_at is None or self.updated_at is None:
            raise ValueError("created_at and updated_at must be finite timestamps")
        if self.last_error is not None:
            object.__setattr__(self, "last_error", str(self.last_error)[:1000])
        if self.transport_message_id is not None:
            object.__setattr__(self, "transport_message_id",
                               str(self.transport_message_id))
        if self.lease_id is not None:
            object.__setattr__(self, "lease_id", _identifier(self.lease_id, "lease_id"))

        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if state == DeliveryState.SENDING:
            if (self.attempts < 1 or self.sending_started_at is None
                    or self.lease_id is None or self.next_attempt_at is not None
                    or self.acked_at is not None):
                raise ValueError("sending delivery requires an active attempt and lease")
        elif state == DeliveryState.ACKED:
            if (self.attempts < 1 or self.acked_at is None
                    or self.sending_started_at is not None or self.lease_id is not None
                    or self.next_attempt_at is not None):
                raise ValueError("acked delivery requires acked_at and no active lease")
        elif state == DeliveryState.FAILED:
            if (self.next_attempt_at is None or self.sending_started_at is not None
                    or self.acked_at is not None or self.lease_id is not None):
                raise ValueError("failed delivery requires a retry time and no active lease")
        elif state == DeliveryState.DEAD_LETTER:
            if (self.attempts < 1 or self.next_attempt_at is not None
                    or self.sending_started_at is not None or self.acked_at is not None
                    or self.lease_id is not None):
                raise ValueError("dead_letter delivery cannot be retryable or leased")
        elif (self.sending_started_at is not None or self.acked_at is not None
              or self.lease_id is not None):
            raise ValueError("pending delivery cannot be acknowledged or leased")

    @property
    def correlation_id(self) -> str:
        return self.envelope.correlation_id

    @property
    def transport(self) -> str:
        return self.envelope.transport

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "delivery_id": self.delivery_id,
            "state": self.state.value,
            "attempts": self.attempts,
            "next_attempt_at": self.next_attempt_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "sending_started_at": self.sending_started_at,
            "acked_at": self.acked_at,
            "last_error": self.last_error,
            "transport_message_id": self.transport_message_id,
            "lease_id": self.lease_id,
            "envelope": self.envelope.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DeliveryRecord":
        if value.get("schema_version") != 1:
            raise ValueError("unsupported DeliveryRecord schema_version")
        return cls(
            envelope=MessageEnvelope.from_dict(value["envelope"]),
            state=DeliveryState(value["state"]),
            attempts=value.get("attempts", 0),
            next_attempt_at=value.get("next_attempt_at"),
            created_at=value["created_at"],
            updated_at=value["updated_at"],
            sending_started_at=value.get("sending_started_at"),
            acked_at=value.get("acked_at"),
            last_error=value.get("last_error"),
            transport_message_id=value.get("transport_message_id"),
            lease_id=value.get("lease_id"),
            delivery_id=value["delivery_id"],
        )
