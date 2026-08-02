"""The anti-flicker filters get the most scrutiny: they are what stands
between a correct measurement and a visibly twitching light strip."""

import numpy as np
import pytest

from processor.utils.smoothing import Debouncer, DeadbandEMA, EMA, StableValue


def test_ema_converges():
    ema = EMA(alpha=0.5)
    ema.update(0.0)
    for _ in range(20):
        ema.update(10.0)
    assert ema.value == pytest.approx(10.0, abs=0.01)


def test_ema_handles_arrays():
    ema = EMA(alpha=0.5, initial=np.zeros(3))
    ema.update(np.array([2.0, 4.0, 6.0]))
    assert np.allclose(ema.value, [1.0, 2.0, 3.0])


def test_stable_value_ignores_a_single_frame_spike():
    value = StableValue(window=9, change_threshold=0.01, hold_frames=5, max_step=0.02)
    for _ in range(12):
        value.update(0.10)
    before = value.value
    value.update(0.40)  # one bad frame
    assert value.value == pytest.approx(before, abs=1e-9)


def test_stable_value_ignores_a_short_burst():
    value = StableValue(window=9, change_threshold=0.01, hold_frames=8, max_step=0.02)
    for _ in range(12):
        value.update(0.10)
    for _ in range(3):
        value.update(0.30)
    for _ in range(12):
        value.update(0.10)
    assert value.value == pytest.approx(0.10, abs=1e-3)


def test_stable_value_commits_a_sustained_change():
    value = StableValue(window=5, change_threshold=0.01, hold_frames=5, max_step=0.05)
    for _ in range(10):
        value.update(0.10)
    for _ in range(40):
        value.update(0.30)
    assert value.value == pytest.approx(0.30, abs=1e-3)


def test_stable_value_animates_instead_of_snapping():
    value = StableValue(window=3, change_threshold=0.01, hold_frames=3, max_step=0.02)
    for _ in range(5):
        value.update(0.0)
    steps = [value.update(0.50) for _ in range(30)]
    deltas = np.abs(np.diff([0.0] + steps))
    assert deltas.max() <= 0.02 + 1e-9
    assert steps[-1] == pytest.approx(0.50, abs=1e-3)


def test_stable_value_force_resets_history():
    value = StableValue(window=5, change_threshold=0.01, hold_frames=3, max_step=0.01)
    for _ in range(10):
        value.update(0.25)
    value.force(0.0)
    assert value.value == 0.0
    assert value.committed == 0.0


def test_deadband_ema_ignores_sub_pixel_jitter():
    smoother = DeadbandEMA(alpha=0.5, deadband=2.0, snap=50.0)
    base = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]])
    smoother.update(base)
    jittered = base + np.array([0.4, -0.3])
    for _ in range(10):
        smoother.update(jittered)
    assert np.allclose(smoother.value, base)


def test_deadband_ema_follows_real_movement():
    """Tracking stops once the remaining error is inside the deadband, which
    is the deliberate trade: up to `deadband` pixels of bias in exchange for a
    corner that does not shimmer."""
    deadband = 2.0
    smoother = DeadbandEMA(alpha=0.5, deadband=deadband, snap=1000.0)
    base = np.zeros((4, 2))
    smoother.update(base)
    target = base + 10.0
    for _ in range(30):
        smoother.update(target)

    residual = np.linalg.norm(smoother.value - target, axis=-1)
    assert np.all(residual <= deadband)
    assert np.all(smoother.value > 8.0)


def test_deadband_ema_snaps_on_a_large_jump():
    smoother = DeadbandEMA(alpha=0.1, deadband=1.0, snap=25.0)
    smoother.update(np.zeros((4, 2)))
    far = np.full((4, 2), 100.0)
    assert np.allclose(smoother.update(far), far)


def test_debouncer_requires_consecutive_hits():
    debouncer = Debouncer(required=3)
    assert not debouncer.update(True)
    assert not debouncer.update(True)
    debouncer.update(False)  # resets the run
    assert not debouncer.update(True)
    assert not debouncer.update(True)
    assert debouncer.update(True)
