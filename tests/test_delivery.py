import pytest

from telegram_presence.content import MediaDescriptor
from telegram_presence.delivery import (
    DeliveryRecord,
    DeliveryState,
    MessageEnvelope,
    TransportReceipt,
)


def test_envelope_and_delivery_round_trip_preserve_correlation():
    envelope = MessageEnvelope(
        transport="BOT_API",
        peer=-10042,
        kind="media",
        text="caption",
        reply_to_message_id=7,
        media=MediaDescriptor("image/png", 512, "blob:42", "photo.png"),
        correlation_id="operation-42",
        causation_id="incoming-7",
        idempotency_key="reply-7",
        owner_user_id=123,
        metadata={"trace": [1, 2]},
        created_at=10.0,
        envelope_id="envelope-42",
    )
    assert envelope.transport == "bot_api"
    assert MessageEnvelope.from_dict(envelope.to_dict()) == envelope

    record = DeliveryRecord(
        envelope=envelope,
        state=DeliveryState.FAILED,
        attempts=2,
        next_attempt_at=20.0,
        created_at=10.0,
        updated_at=12.0,
        last_error="timeout",
        delivery_id="delivery-42",
    )
    restored = DeliveryRecord.from_dict(record.to_dict())
    assert restored == record
    assert restored.transport == "bot_api"
    assert restored.correlation_id == "operation-42"


def test_envelope_rejects_unsafe_identity_and_incomplete_reaction():
    for owner in (True, "123", 0, -1):
        try:
            MessageEnvelope(transport="bot_api", peer=1, text="hello",
                            owner_user_id=owner)
        except ValueError:
            pass
        else:
            raise AssertionError("non-numeric or non-positive owner id accepted")

    try:
        MessageEnvelope(transport="bot_api", peer=1, kind="reaction", text="👍")
    except ValueError as exc:
        assert "reply_to_message_id" in str(exc)
    else:
        raise AssertionError("unanchored reaction accepted")

    with pytest.raises(ValueError, match="kind=media"):
        MessageEnvelope(
            transport="bot_api",
            peer=1,
            kind="reply",
            text="caption",
            media=MediaDescriptor("image/png", 10, "blob:photo"),
        )


def test_transport_receipt_has_explicit_boolean_semantics():
    ok = TransportReceipt(True, "bot_api", "corr", transport_message_id="99")
    failed = TransportReceipt(False, "bot_api", "corr", error="no ACK")
    assert bool(ok) is True
    assert bool(failed) is False


def test_schema_versions_and_impossible_delivery_states_are_rejected():
    envelope = MessageEnvelope(transport="bot_api", peer=1, text="hello")
    bad_envelope = envelope.to_dict()
    bad_envelope["schema_version"] = 99
    with pytest.raises(ValueError, match="schema_version"):
        MessageEnvelope.from_dict(bad_envelope)

    with pytest.raises(ValueError, match="acked_at"):
        DeliveryRecord(envelope=envelope, state=DeliveryState.ACKED, attempts=1)
    with pytest.raises(ValueError, match="active attempt"):
        DeliveryRecord(envelope=envelope, state=DeliveryState.SENDING, attempts=1)

    bad_record = DeliveryRecord(envelope=envelope).to_dict()
    bad_record["schema_version"] = 99
    with pytest.raises(ValueError, match="schema_version"):
        DeliveryRecord.from_dict(bad_record)


def test_text_envelopes_require_real_destination_and_payload():
    with pytest.raises(ValueError, match="peer"):
        MessageEnvelope(transport="bot_api", peer=0, text="hello")
    with pytest.raises(ValueError, match="non-empty text"):
        MessageEnvelope(transport="bot_api", peer=1, text="  ")
    with pytest.raises(ValueError, match="reply_to_message_id"):
        MessageEnvelope(transport="bot_api", peer=1, kind="reply", text="hello")
