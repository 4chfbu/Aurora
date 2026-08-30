from __future__ import annotations

import time
from collections.abc import Callable


class SuspendGapDetector:
    """Detect wall-clock jumps that are not reflected by CLOCK_MONOTONIC.

    Linux pauses CLOCK_MONOTONIC while the host is suspended. Absolute task
    leases and benchmark deadlines keep advancing, so continuing a run after
    such a jump produces invalid evaluation data. Call ``poll`` periodically
    and invalidate the active run when it returns a gap.
    """

    def __init__(
        self,
        *,
        threshold_seconds: float = 30.0,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold_seconds = threshold_seconds
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._last_wall = wall_clock()
        self._last_monotonic = monotonic_clock()

    def poll(self) -> float | None:
        wall_now = self._wall_clock()
        monotonic_now = self._monotonic_clock()
        wall_elapsed = max(0.0, wall_now - self._last_wall)
        monotonic_elapsed = max(0.0, monotonic_now - self._last_monotonic)
        self._last_wall = wall_now
        self._last_monotonic = monotonic_now
        gap = wall_elapsed - monotonic_elapsed
        return gap if gap >= self.threshold_seconds else None
