"""Temporal filters used to keep the output calm.

Ambient lighting amplifies instability: a black-bar edge that wobbles by two
pixels, or a corner that jitters between two contour fits, turns into visible
flicker on the LEDs.  Every value that can change over time therefore goes
through one of the filters here rather than being used raw.
"""

from __future__ import annotations

from collections import deque

import numpy as np


class EMA:
    """Exponential moving average that also works on numpy arrays."""

    def __init__(self, alpha: float = 0.2, initial=None):
        self.alpha = float(np.clip(alpha, 0.0, 1.0))
        self.value = initial

    def update(self, sample):
        if sample is None:
            return self.value
        if self.value is None:
            self.value = sample if np.isscalar(sample) else np.array(sample, dtype=np.float64)
            return self.value
        if np.isscalar(sample):
            self.value = (1.0 - self.alpha) * self.value + self.alpha * float(sample)
        else:
            self.value = (1.0 - self.alpha) * self.value + self.alpha * np.asarray(
                sample, dtype=np.float64
            )
        return self.value

    def reset(self, value=None) -> None:
        self.value = value


class StableValue:
    """Median filter + hysteresis + rate limiting for a single scalar.

    Three defences against flicker, in order:

    1. ``window`` frames of median filtering removes single-frame outliers
       (a bright flash, one badly decoded frame).
    2. A new target is only *committed* once the median has disagreed with the
       current commitment by more than ``change_threshold`` for
       ``hold_frames`` consecutive frames.  This is the hysteresis: brief
       content changes (a dark scene in a 16:9 film) never move the crop.
    3. The reported value walks toward the commitment by at most ``max_step``
       per frame, so committed changes animate instead of snapping.
    """

    def __init__(
        self,
        window: int = 15,
        change_threshold: float = 4.0,
        hold_frames: int = 8,
        max_step: float = 3.0,
        initial: float = 0.0,
    ):
        self.window = max(1, int(window))
        self.change_threshold = float(change_threshold)
        self.hold_frames = max(1, int(hold_frames))
        self.max_step = max(1e-6, float(max_step))
        self._samples: deque[float] = deque(maxlen=self.window)
        self.committed = float(initial)
        self.current = float(initial)
        self._pending_frames = 0
        self._pending_value = float(initial)

    @property
    def value(self) -> float:
        return self.current

    def update(self, sample: float) -> float:
        self._samples.append(float(sample))
        median = float(np.median(self._samples))

        if abs(median - self.committed) > self.change_threshold:
            # Restart the countdown if the proposal itself is still moving
            # around; we only want to commit to something that has settled.
            if abs(median - self._pending_value) > self.change_threshold:
                self._pending_frames = 0
            self._pending_value = median
            self._pending_frames += 1
            if self._pending_frames >= self.hold_frames:
                self.committed = median
                self._pending_frames = 0
        else:
            self._pending_frames = 0
            self._pending_value = self.committed

        delta = self.committed - self.current
        if abs(delta) <= self.max_step:
            self.current = self.committed
        else:
            self.current += self.max_step * (1.0 if delta > 0 else -1.0)
        return self.current

    def force(self, value: float) -> None:
        """Jump straight to a value (used on reset / recalibration)."""
        value = float(value)
        self._samples.clear()
        self.committed = value
        self.current = value
        self._pending_value = value
        self._pending_frames = 0

    def reset(self) -> None:
        self.force(0.0)


class DeadbandEMA:
    """Smoothed value that ignores movement below a threshold.

    Used for the TV corners: without a deadband the contour fit oscillates by a
    pixel or two every frame, which re-warps the image slightly and shimmers
    along the LED edges.
    """

    def __init__(self, alpha: float = 0.25, deadband: float = 1.5, snap: float = 40.0):
        self.alpha = float(np.clip(alpha, 0.0, 1.0))
        self.deadband = float(deadband)
        self.snap = float(snap)
        self.value: np.ndarray | None = None

    def update(self, sample) -> np.ndarray:
        arr = np.asarray(sample, dtype=np.float64)
        if self.value is None:
            self.value = arr.copy()
            return self.value

        shift = np.linalg.norm(arr - self.value, axis=-1)
        if np.max(shift) >= self.snap:
            # A jump this big is a real change (camera bumped, recalibration),
            # not noise -- following it slowly would smear the image for
            # seconds, so take it immediately.
            self.value = arr.copy()
            return self.value

        moving = shift > self.deadband
        if np.any(moving):
            blended = (1.0 - self.alpha) * self.value + self.alpha * arr
            self.value = np.where(moving[..., None], blended, self.value)
        return self.value

    def force(self, sample) -> np.ndarray:
        self.value = np.asarray(sample, dtype=np.float64).copy()
        return self.value

    def reset(self) -> None:
        self.value = None


class Debouncer:
    """Fires once a boolean condition has held for N consecutive checks."""

    def __init__(self, required: int = 3):
        self.required = max(1, int(required))
        self.count = 0

    def update(self, condition: bool) -> bool:
        if condition:
            self.count += 1
            if self.count >= self.required:
                self.count = 0
                return True
        else:
            self.count = 0
        return False

    def reset(self) -> None:
        self.count = 0
