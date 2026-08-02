"""Frame-rate measurement and per-stage timing."""

from __future__ import annotations

import time
from collections import deque
from contextlib import contextmanager


class FpsMeter:
    """Rolling frame rate over the last ``window`` frame intervals."""

    def __init__(self, window: int = 30):
        self.window = max(2, int(window))
        self._times: deque[float] = deque(maxlen=self.window)

    def tick(self, now: float | None = None) -> None:
        self._times.append(time.monotonic() if now is None else now)

    @property
    def fps(self) -> float:
        if len(self._times) < 2:
            return 0.0
        span = self._times[-1] - self._times[0]
        if span <= 0:
            return 0.0
        return (len(self._times) - 1) / span

    def reset(self) -> None:
        self._times.clear()


class StageTimings:
    """Smoothed wall-clock cost of each pipeline stage, in milliseconds."""

    def __init__(self, alpha: float = 0.1):
        self.alpha = float(alpha)
        self._values: dict[str, float] = {}

    @contextmanager
    def measure(self, name: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            previous = self._values.get(name)
            if previous is None:
                self._values[name] = elapsed_ms
            else:
                self._values[name] = (1.0 - self.alpha) * previous + self.alpha * elapsed_ms

    def record(self, name: str, elapsed_ms: float) -> None:
        previous = self._values.get(name)
        if previous is None:
            self._values[name] = elapsed_ms
        else:
            self._values[name] = (1.0 - self.alpha) * previous + self.alpha * elapsed_ms

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 3) for k, v in self._values.items()}

    @property
    def total_ms(self) -> float:
        return sum(self._values.values())

    def reset(self) -> None:
        self._values.clear()


class Periodic:
    """True at most once every ``interval`` seconds."""

    def __init__(self, interval: float):
        self.interval = float(interval)
        self._last = 0.0

    def ready(self, now: float | None = None) -> bool:
        if self.interval <= 0:
            return True
        now = time.monotonic() if now is None else now
        if now - self._last >= self.interval:
            self._last = now
            return True
        return False

    def reset(self) -> None:
        self._last = 0.0
