"""Fail-fast validation for poller/scheduler liveness timing."""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Optional


def _positive_seconds(value: float, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be positive finite seconds")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field_name} must be positive finite seconds")
    return result


@dataclass(frozen=True, slots=True)
class LivenessCadence:
    """Timing contract for an inbound poller and an engage-cycle scheduler.

    ``stale_after_seconds`` must cover the worst-case poll plus cycle delay;
    otherwise a healthy low-frequency worker can be declared dead before it
    has had one complete opportunity to observe and process work.
    """

    poll_interval_seconds: float
    cycle_interval_seconds: float
    stale_after_seconds: float

    def __post_init__(self) -> None:
        poll = _positive_seconds(self.poll_interval_seconds,
                                 "poll_interval_seconds")
        cycle = _positive_seconds(self.cycle_interval_seconds,
                                  "cycle_interval_seconds")
        stale = _positive_seconds(self.stale_after_seconds,
                                  "stale_after_seconds")
        minimum = poll + cycle
        if stale < minimum:
            raise ValueError(
                "stale_after_seconds must be at least poll_interval_seconds + "
                "cycle_interval_seconds"
            )
        object.__setattr__(self, "poll_interval_seconds", poll)
        object.__setattr__(self, "cycle_interval_seconds", cycle)
        object.__setattr__(self, "stale_after_seconds", stale)

    @property
    def worst_case_processing_delay_seconds(self) -> float:
        return self.poll_interval_seconds + self.cycle_interval_seconds

    def is_stale(self, last_success_at: Optional[float], *, now: Optional[float] = None) -> bool:
        if last_success_at is None:
            return True
        if isinstance(last_success_at, bool) or not isinstance(last_success_at, (int, float)):
            raise ValueError("last_success_at must be a finite timestamp")
        current = time.time() if now is None else now
        if (isinstance(current, bool) or not isinstance(current, (int, float))
                or not math.isfinite(float(current))
                or not math.isfinite(float(last_success_at))
                or float(current) < 0 or float(last_success_at) < 0):
            raise ValueError("timestamps must be finite")
        if float(current) < float(last_success_at):
            raise ValueError("now cannot precede last_success_at")
        return float(current) - float(last_success_at) > self.stale_after_seconds


def validate_liveness_cadence(
    *,
    poll_interval_seconds: float,
    cycle_interval_seconds: float,
    stale_after_seconds: float,
) -> LivenessCadence:
    """Build and validate a reusable liveness timing contract."""
    return LivenessCadence(
        poll_interval_seconds=poll_interval_seconds,
        cycle_interval_seconds=cycle_interval_seconds,
        stale_after_seconds=stale_after_seconds,
    )
