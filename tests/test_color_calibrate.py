"""Solid-patch colour calibration solver and session."""

from __future__ import annotations

import numpy as np
import pytest

from processor.app import Processor
from processor.config.schema import Config
from processor.utils.color_calibrate import (
    ColorCalibrationSession,
    sample_center_roi,
    solve_gains,
)


def test_sample_center_roi_means_solid_patch():
    image = np.zeros((100, 200, 3), dtype=np.uint8)
    image[:] = (40, 80, 200)  # BGR
    mean = sample_center_roi(image, fraction=0.3)
    assert mean.shape == (3,)
    assert np.allclose(mean, [40, 80, 200], atol=0.5)


def test_solve_gains_recovers_channel_cast():
    # Camera sees white with a green cast and grey similarly scaled.
    white = np.array([180.0, 220.0, 160.0])  # B, G, R
    grey = white * (128 / 255)
    red = np.array([30.0, 40.0, 200.0])
    green = np.array([40.0, 210.0, 35.0])
    blue = np.array([200.0, 45.0, 30.0])
    black = np.array([5.0, 5.0, 5.0])

    solution = solve_gains(
        {
            "black": black,
            "white": white,
            "grey": grey,
            "red": red,
            "green": green,
            "blue": blue,
        }
    )
    gains = np.array(solution.gains_bgr)
    corrected = white * gains
    # After correction, channels should be nearly equal.
    assert corrected.max() - corrected.min() < 8.0
    assert 0.5 <= gains.min() <= gains.max() <= 2.0
    assert 0.6 <= solution.gamma <= 1.8


def test_solve_gains_rejects_dark_white():
    with pytest.raises(ValueError, match="too dark"):
        solve_gains({"white": np.array([2.0, 2.0, 2.0])})


def test_session_runs_to_ready_with_synthetic_frames():
    session = ColorCalibrationSession(settle_sec=0.0, sample_frames=2)
    session.start()
    assert session.state == "running"

    # Feed a distinct solid for each patch name in order.
    solids = {
        "black": (5, 5, 5),
        "white": (170, 210, 150),
        "grey": (85, 105, 75),
        "red": (30, 40, 200),
        "green": (40, 200, 35),
        "blue": (200, 45, 30),
    }
    # Many ticks: settle is 0 so each tick can sample.
    for _ in range(80):
        if session.state != "running":
            break
        name = session.display_name()
        bgr = solids.get(name, (0, 0, 0))
        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        frame[:] = bgr
        session.tick(frame)

    assert session.state == "ready"
    assert session.solution is not None
    assert "white" in session.measurements


def test_processor_apply_color_calibration(monkeypatch, tmp_path):
    config = Config.from_dict(
        {
            "camera": {"source": "synthetic", "replay_fps": 60},
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
            "boundary": {
                "mode": "manual",
                "corners": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
            },
        }
    )
    app = Processor(config, config_path=tmp_path / "config.yaml")
    app.start()
    try:
        # Install corners into live state (manual config alone does not).
        corners = np.array(
            [[32, 18], [288, 18], [288, 162], [32, 162]], dtype=np.float32
        )
        app.state.set_corners(corners, 1.0, "manual")

        started = app.start_color_calibration()
        assert started["ok"] is True

        session = app._color_cal
        session.settle_sec = 0.0
        session.sample_frames = 2
        solids = {
            "black": (5, 5, 5),
            "white": (170, 210, 150),
            "grey": (85, 105, 75),
            "red": (30, 40, 200),
            "green": (40, 200, 35),
            "blue": (200, 45, 30),
        }
        for _ in range(80):
            if session.state != "running":
                break
            name = session.display_name()
            frame = np.zeros((180, 320, 3), dtype=np.uint8)
            frame[:] = solids.get(name, (0, 0, 0))
            # Perspective debug image is what tick reads.
            app._last_ctx = type(
                "C",
                (),
                {"debug_images": {"perspective": frame}, "skipped": {}},
            )()
            app._tick_color_calibration()

        assert session.state == "ready"
        result = app.apply_color_calibration(save=True)
        assert result["ok"] is True
        assert app.config.color.white_balance == "manual"
        assert app.config.color.calibration.calibrated_at
        assert (tmp_path / "config.yaml").exists()
    finally:
        app.shutdown()
