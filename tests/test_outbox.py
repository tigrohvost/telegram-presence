import json

import pytest

from telegram_presence.delivery import DeliveryState, MessageEnvelope, TransportReceipt
from telegram_presence.outbox import DurableOutbox, OutboxCorruptionError


class _Clock:
    def __init__(self, value=100.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _message(**overrides):
    values = {
        "transport": "bot_api",
        "peer": -1001,
        "kind": "reply",
        "text": "hello",
        "reply_to_message_id": 42,
        "correlation_id": "cycle-1",
        "idempotency_key": "reply-42",
        "created_at": 100.0,
    }
    values.update(overrides)
    return MessageEnvelope(**values)


def test_enqueue_is_durable_and_idempotent(tmp_path):
    clock = _Clock()
    outbox = DurableOutbox(tmp_path, clock=clock)
    first = outbox.enqueue(_message())
    second = outbox.enqueue(_message(correlation_id="retry-process"))

    assert first == second
    assert first.state == DeliveryState.PENDING
    assert first.attempts == 0
    assert outbox.get(first.delivery_id) == first
    assert list(tmp_path.glob("*.delivery.json"))

    with pytest.raises(ValueError, match="idempotency_key"):
        outbox.enqueue(_message(text="different request"))


def test_ack_is_written_only_after_explicit_transport_success(tmp_path):
    clock = _Clock()
    outbox = DurableOutbox(tmp_path, clock=clock, base_retry_seconds=5,
                           max_retry_seconds=20)
    queued = outbox.enqueue(_message())
    observed = []

    def sender(envelope):
        observed.append(outbox.get(queued.delivery_id).state)
        return TransportReceipt(True, "bot_api", envelope.correlation_id,
                                transport_message_id="remote-7")

    settled = outbox.dispatch_one(queued.delivery_id, sender)
    assert observed == [DeliveryState.SENDING]
    assert settled.state == DeliveryState.ACKED
    assert settled.attempts == 1
    assert settled.transport_message_id == "remote-7"
    assert outbox.get(queued.delivery_id).state == DeliveryState.ACKED


def test_wall_clock_rollback_during_send_does_not_strand_delivery(tmp_path):
    clock = _Clock()
    outbox = DurableOutbox(tmp_path, clock=clock)
    queued = outbox.enqueue(_message())

    def sender(_envelope):
        clock.value = 99.0
        return True

    settled = outbox.dispatch_one(queued.delivery_id, sender)
    assert settled.state == DeliveryState.ACKED
    assert settled.updated_at == 100.0
    assert settled.acked_at == 100.0


def test_failed_attempt_backs_off_then_retries(tmp_path):
    clock = _Clock()
    outbox = DurableOutbox(tmp_path, clock=clock, max_attempts=3,
                           base_retry_seconds=5, max_retry_seconds=8)
    queued = outbox.enqueue(_message())

    first = outbox.dispatch_one(queued.delivery_id, lambda _envelope: False)
    assert first.state == DeliveryState.FAILED
    assert first.attempts == 1
    assert first.next_attempt_at == 105.0
    assert outbox.dispatch_one(queued.delivery_id, lambda _envelope: True) is None

    clock.advance(5)
    second = outbox.dispatch_one(queued.delivery_id, lambda _envelope: True)
    assert second.state == DeliveryState.ACKED
    assert second.attempts == 2


def test_retry_is_bounded_and_exhaustion_is_dead_letter(tmp_path):
    clock = _Clock()
    outbox = DurableOutbox(tmp_path, clock=clock, max_attempts=2,
                           base_retry_seconds=2, max_retry_seconds=3)
    queued = outbox.enqueue(_message())
    first = outbox.dispatch_one(queued.delivery_id, lambda _envelope: False)
    assert first.state == DeliveryState.FAILED
    clock.advance(2)
    second = outbox.dispatch_one(queued.delivery_id, lambda _envelope: False)
    assert second.state == DeliveryState.DEAD_LETTER
    assert second.next_attempt_at is None
    assert outbox.dispatch_due(lambda _envelope: True) == []

    requeued = outbox.requeue_dead_letter(queued.delivery_id)
    assert requeued.state == DeliveryState.FAILED and requeued.attempts == 0


def test_lowered_attempt_limit_dead_letters_before_another_send(tmp_path):
    clock = _Clock()
    original = DurableOutbox(tmp_path, clock=clock, max_attempts=5,
                             base_retry_seconds=1)
    queued = original.enqueue(_message())
    assert original.dispatch_one(queued.delivery_id,
                                 lambda _envelope: False).state == DeliveryState.FAILED
    clock.advance(1)

    restarted = DurableOutbox(tmp_path, clock=clock, max_attempts=1,
                              base_retry_seconds=1)
    normalized = restarted.get(queued.delivery_id)
    assert normalized.state == DeliveryState.DEAD_LETTER
    called = []
    assert restarted.dispatch_one(queued.delivery_id,
                                  lambda envelope: called.append(envelope) or True) is None
    assert called == []


def test_restart_recovers_stale_sending_lease(tmp_path):
    clock = _Clock(0)
    first_process = DurableOutbox(tmp_path, clock=clock,
                                  sending_timeout_seconds=10)
    queued = first_process.enqueue(_message(created_at=0))
    claimed = first_process.claim_due()
    assert claimed.state == DeliveryState.SENDING

    clock.advance(11)
    restarted = DurableOutbox(tmp_path, clock=clock,
                              sending_timeout_seconds=10)
    recovered = restarted.get(queued.delivery_id)
    assert recovered.state == DeliveryState.FAILED
    assert recovered.next_attempt_at == 11.0
    assert "recovered" in recovered.last_error
    assert restarted.dispatch_one(queued.delivery_id,
                                  lambda _envelope: True).state == DeliveryState.ACKED


def test_dispatch_reaps_lease_that_became_stale_after_restart(tmp_path):
    clock = _Clock(0)
    first_process = DurableOutbox(tmp_path, clock=clock,
                                  sending_timeout_seconds=10)
    queued = first_process.enqueue(_message(created_at=0))
    first_process.claim_due()

    clock.advance(5)
    restarted = DurableOutbox(tmp_path, clock=clock,
                              sending_timeout_seconds=10)
    assert restarted.get(queued.delivery_id).state == DeliveryState.SENDING
    clock.advance(6)
    settled = restarted.dispatch_one(queued.delivery_id, lambda _envelope: True)
    assert settled.state == DeliveryState.ACKED
    assert settled.attempts == 2


def test_mismatched_or_ambiguous_result_never_acks(tmp_path):
    clock = _Clock()
    outbox = DurableOutbox(tmp_path, clock=clock, base_retry_seconds=1)
    queued = outbox.enqueue(_message())
    settled = outbox.dispatch_one(
        queued.delivery_id,
        lambda _envelope: {"ok": True, "correlation_id": "wrong"},
    )
    assert settled.state == DeliveryState.FAILED
    assert "correlation_id" in settled.last_error


def test_dispatch_routes_mixed_transports_without_consuming_wrong_attempts(tmp_path):
    clock = _Clock()
    outbox = DurableOutbox(tmp_path, clock=clock)
    bot = outbox.enqueue(_message(idempotency_key="bot"))
    user = outbox.enqueue(_message(
        transport="telethon", idempotency_key="user", correlation_id="cycle-user",
    ))

    class _BotSender:
        transport_name = "bot_api"

        def send(self, _envelope):
            return True

    bot_results = outbox.dispatch_due(_BotSender().send)
    assert [record.delivery_id for record in bot_results] == [bot.delivery_id]
    assert outbox.get(user.delivery_id).state == DeliveryState.PENDING
    assert outbox.get(user.delivery_id).attempts == 0

    user_results = outbox.dispatch_due({"telethon": lambda _envelope: True})
    assert [record.delivery_id for record in user_results] == [user.delivery_id]


def test_corrupt_record_is_surfaced_not_skipped(tmp_path):
    path = tmp_path / "bad.delivery.json"
    path.write_text(json.dumps({"not": "a record"}), encoding="utf-8")
    outbox = DurableOutbox(tmp_path, recover=False)
    with pytest.raises(OutboxCorruptionError):
        outbox.list()
