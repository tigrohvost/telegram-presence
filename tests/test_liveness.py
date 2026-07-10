import pytest

from telegram_presence.liveness import LivenessCadence, validate_liveness_cadence


def test_liveness_cadence_covers_poller_plus_cycle_delay():
    cadence = validate_liveness_cadence(
        poll_interval_seconds=30,
        cycle_interval_seconds=300,
        stale_after_seconds=360,
    )
    assert cadence.worst_case_processing_delay_seconds == 330
    assert cadence.is_stale(100, now=460) is False
    assert cadence.is_stale(100, now=460.001) is True
    assert cadence.is_stale(None, now=100) is True


def test_liveness_cadence_fails_fast_on_impossible_threshold():
    with pytest.raises(ValueError, match="at least"):
        LivenessCadence(30, 300, 299)
    with pytest.raises(ValueError, match="positive"):
        LivenessCadence(0, 300, 400)
