"""Solid-patch colour calibration solver and session."""

from __future__ import annotations

import numpy as np
import pytest
import yaml

from processor.app import Processor
from processor.config.schema import Config
from processor.utils.color_calibrate import (
    DEFAULT_PATCHES,
    ColorCalibrationSession,
    is_identity_matrix,
    patch_targets_bgr,
    sample_center_roi,
    solve_calibration,
    solve_gains,
)


def _cast_means(cam_matrix: np.ndarray) -> dict[str, np.ndarray]:
    """Simulate camera seeing ``target @ cam_matrix`` for every default patch."""
    means: dict[str, np.ndarray] = {}
    for name, tgt in patch_targets_bgr().items():
        means[name] = np.clip(tgt @ cam_matrix, 0.0, 255.0)
    return means


def test_sample_center_roi_means_solid_patch():
    image = np.zeros((100, 200, 3), dtype=np.uint8)
    image[:] = (40, 80, 200)  # BGR
    mean = sample_center_roi(image, fraction=0.3)
    assert mean.shape == (3,)
    assert np.allclose(mean, [40, 80, 200], atol=0.5)


def test_solve_identity_when_measure_equals_target():
    solution = solve_calibration(patch_targets_bgr())
    assert is_identity_matrix(solution.matrix_bgr, atol=0.08)
    assert solution.gains_bgr == (1.0, 1.0, 1.0)
    assert abs(solution.gamma - 1.0) < 0.1


def test_solve_matrix_reduces_skin_error_vs_gains_only():
    # Hue skew: green bleeds into red/blue (diagonal gains cannot fix this).
    cam = np.array(
        [
            [1.05, 0.12, 0.02],
            [0.08, 0.92, 0.10],
            [0.03, 0.15, 1.00],
        ],
        dtype=np.float64,
    )
    means = _cast_means(cam)
    targets = patch_targets_bgr()
    skin = means["skin_medium"]
    tgt = targets["skin_medium"]

    # Gains-only baseline: scale channels from white.
    white = means["white"]
    gains = float(white.mean()) / np.maximum(white, 1.0)
    gains = gains / float(np.mean(gains))
    gains_err = float(np.linalg.norm(skin * gains - tgt))

    solution = solve_calibration(means)
    matrix_err = float(np.linalg.norm(skin @ solution.matrix_bgr - tgt))
    assert matrix_err < gains_err
    assert matrix_err < 35.0
    assert solution.gains_bgr == (1.0, 1.0, 1.0)
    assert len(solution.matrix_flat()) == 9


def test_solve_gains_alias_rejects_dark_white():
    with pytest.raises(ValueError, match="too dark"):
        solve_gains({"white": np.array([2.0, 2.0, 2.0])})


def _synthetic_solids(channel_cast: np.ndarray | None = None) -> dict[str, tuple[int, int, int]]:
    cast = channel_cast if channel_cast is not None else np.array([1.05, 1.12, 0.92])
    solids: dict[str, tuple[int, int, int]] = {}
    for name, rgb in DEFAULT_PATCHES:
        r, g, b = rgb
        bgr = np.array([b, g, r], dtype=np.float64) * cast
        solids[name] = tuple(int(np.clip(v, 0, 255)) for v in bgr)
    return solids


def test_session_runs_to_ready_with_synthetic_frames():
    session = ColorCalibrationSession(settle_sec=0.0, sample_frames=2)
    session.start()
    assert session.state == "running"
    assert session.status()["total"] == len(DEFAULT_PATCHES)

    solids = _synthetic_solids()
    for _ in range(400):
        if session.state != "running":
            break
        name = session.display_name()
        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        frame[:] = solids.get(name, (0, 0, 0))
        session.tick(frame)

    assert session.state == "ready"
    assert session.solution is not None
    assert "white" in session.measurements
    assert "skin_medium" in session.measurements
    assert "yellow" in session.measurements
    assert session.solution.as_dict()["matrix_enabled"] is True


def test_processor_apply_color_calibration_persists_matrix(tmp_path):
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
        corners = np.array(
            [[32, 18], [288, 18], [288, 162], [32, 162]], dtype=np.float32
        )
        app.state.set_corners(corners, 1.0, "manual")

        started = app.start_color_calibration()
        assert started["ok"] is True

        session = app._color_cal
        session.settle_sec = 0.0
        session.sample_frames = 2
        solids = _synthetic_solids()
        for _ in range(400):
            if session.state != "running":
                break
            name = session.display_name()
            frame = np.zeros((180, 320, 3), dtype=np.uint8)
            frame[:] = solids.get(name, (0, 0, 0))
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
        assert app.config.color.matrix_enabled is True
        assert len(app.config.color.matrix) == 9
        assert app.config.color.gains.r == pytest.approx(1.0)
        assert app.config.color.calibration.calibrated_at
        assert "skin_medium" in app.config.color.calibration.patch_means_bgr

        path = tmp_path / "config.yaml"
        assert path.exists()
        saved = yaml.safe_load(path.read_text())
        assert saved["color"]["matrix_enabled"] is True
        assert len(saved["color"]["matrix"]) == 9
        assert saved["color"]["calibration"]["matrix"]
    finally:
        app.shutdown()
