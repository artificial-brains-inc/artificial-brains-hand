"""Choose when to acquire the next current observation, without buffering it.

The native manager has a finite FIFO and consumes at most one external frame
per completed iteration.  Receipt throughput therefore informs a conservative
wall-clock sampling rate.  This is not an input-consumption acknowledgement:
delayed output delivery can report old progress, and another producer can use
the same native queue.  Native rejections must still be reported by the caller.
"""

from __future__ import annotations

from collections import deque
import math
import time
from typing import Callable


NATIVE_INPUT_QUEUE_CAPACITY = 64
STARTUP_DRAIN_EVENTS = NATIVE_INPUT_QUEUE_CAPACITY + 1

OBSERVATION_WINDOW_S = 1.0
THROUGHPUT_FRACTION = 0.5
MAX_ACQUISITION_HZ = 20.0


def _finite_time(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Acquisition timing must be a finite monotonic timestamp.")
    return float(value)


class AcquisitionCadence:
    """A wall-clock acquisition limiter with no accumulated catch-up credits.

    Call ``observe`` once for each distinct decoded output event, passing its
    local receipt timestamp.  ``ready`` admits the next sensor acquisition;
    after that acquisition call ``acquired`` and publish the fresh observation
    immediately.  Command changes do not reset this object.

    Only timing is retained here.  There are no step numbers, motor decisions,
    sensor payloads, or input-to-output causal claims.  At steady throughput the
    requested sampling rate is half the recent output receipt rate, limited by
    the configured maximum and a 20 Hz transport ceiling.  A long pause allows
    one acquisition when ready, never a batch of missed acquisitions.
    """

    def __init__(self, max_hz: float, *, clock: Callable[[], float] = time.monotonic):
        if (isinstance(max_hz, bool) or not isinstance(max_hz, (int, float))
                or not math.isfinite(max_hz) or max_hz <= 0):
            raise ValueError("Maximum acquisition rate must be finite and positive.")
        self.max_hz = min(float(max_hz), MAX_ACQUISITION_HZ)
        self.clock = clock
        self._receipts: deque[float] = deque()
        self._last_observed: float | None = None
        self._last_acquired: float | None = None

    def _trim(self, now: float) -> None:
        cutoff = now - OBSERVATION_WINDOW_S
        while self._receipts and self._receipts[0] <= cutoff:
            self._receipts.popleft()

    def observe(self, received_at: float) -> None:
        """Record one event already accepted by the output decoder."""
        received_at = _finite_time(received_at)
        if self._last_observed is not None and received_at < self._last_observed:
            raise ValueError("Output receipt timestamps must be in monotonic order.")
        self._last_observed = received_at
        self._trim(received_at)
        self._receipts.append(received_at)

    def rate_hz(self, now: float | None = None) -> float:
        now = _finite_time(self.clock() if now is None else now)
        self._trim(now)
        return min(self.max_hz, THROUGHPUT_FRACTION * len(self._receipts) / OBSERVATION_WINDOW_S)

    def ready(self, now: float | None = None) -> bool:
        now = _finite_time(self.clock() if now is None else now)
        rate = self.rate_hz(now)
        if rate == 0:
            return False
        return self._last_acquired is None or now - self._last_acquired >= 1.0 / rate

    def acquired(self, now: float | None = None) -> None:
        """Record the real acquisition time; do not bank unused intervals.

        Admission is checked before reading a sensor.  The receipt window may
        change while that read is in progress, so this method records the read
        without retrospectively rejecting an observation already captured.
        """
        now = _finite_time(self.clock() if now is None else now)
        if self._last_acquired is not None and now < self._last_acquired:
            raise ValueError("Acquisition timestamps must be in monotonic order.")
        self._last_acquired = now
