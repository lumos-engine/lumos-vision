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
    assert solution.black_level_bgr == pytest.approx((0.0, 0.0, 0.0), abs=0.5)


def test_solve_recovers_black_pedestal_and_near_identity_matrix():
    pedestal = np.array([18.0, 12.0, 22.0], dtype=np.float64)
    means = {
        name: np.clip(tgt + pedestal, 0.0, 255.0)
        for name, tgt in patch_targets_bgr().items()
    }
    solution = solve_calibration(means)
    assert solution.black_level_bgr == pytest.approx(tuple(pedestal), abs=1.5)
    assert is_identity_matrix(solution.matrix_bgr, atol=0.12)
    assert solution.as_dict()["black_level_enabled"] is True


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
    adj = np.maximum(skin - np.asarray(solution.black_level_bgr), 0.0)
    matrix_err = float(np.linalg.norm(adj @ solution.matrix_bgr - tgt))
    assert matrix_err < gains_err
    assert matrix_err < 35.0
    assert solution.gains_bgr == (1.0, 1.0, 1.0)
    assert len(solution.matrix_flat()) == 9


def test_solve_gains_alias_rejects_dark_white():
    with pytest.raises(ValueError, match="too dark"):
        solve_gains({"white": np.array([2.0, 2.0, 2.0])})


def test_solve_rejects_white_barely_above_black():
    means = patch_targets_bgr()
    means = {k: v.copy() for k, v in means.items()}
    means["black"] = np.array([40.0, 35.0, 45.0])
    means["white"] = np.array([90.0, 85.0, 95.0])  # only ~50 above black
    with pytest.raises(ValueError, match="barely brighter"):
        solve_calibration(means)


def _synthetic_solids(channel_cast: np.ndarray | None = None) -> dict[str, tuple[int, int, int]]:
    cast = channel_cast if channel_cast is not None else np.array([1.05, 1.12, 0.92])
    solids: dict[str, tuple[int, int, int]] = {}
    for name, rgb in DEFAULT_PATCHES:
        r, g, b = rgb
        bgr = np.array([b, g, r], dtype=np.float64) * cast
        solids[name] = tuple(int(np.clip(v, 0, 255)) for v in bgr)
    return solids


def test_session_settle_sec_is_clamped_on_start():
    session = ColorCalibrationSession()
    status = session.start(settle_sec=8.0, mode="auto")
    assert status["settle_sec"] == 8.0
    session.abort()
    status = session.start(settle_sec=0.1, mode="auto")
    assert status["settle_sec"] == pytest.approx(0.5)
    session.abort()
    status = session.start(settle_sec=99.0, mode="auto")
    assert status["settle_sec"] == pytest.approx(30.0)


def test_session_auto_runs_to_ready_with_synthetic_frames():
    session = ColorCalibrationSession(settle_sec=0.0, sample_frames=2)
    session.start(settle_sec=0.5, mode="auto")
    session.settle_sec = 0.0  # tests run as fast as possible after start
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


def _manual_capture(session: ColorCalibrationSession, bgr: tuple[int, int, int]) -> None:
    session.request_capture()
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    frame[:] = bgr
    for _ in range(session.sample_frames + 2):
        if session.phase != "sampling":
            break
        session.tick(frame)


def test_manual_capture_replaces_in_place_without_advance():
    session = ColorCalibrationSession(sample_frames=2)
    session.start(mode="manual", advance_after_capture=False)
    assert session.phase == "waiting"
    assert session.display_name() == "black"

    _manual_capture(session, (5, 5, 5))
    assert session.measurements["black"].tolist() == pytest.approx([5, 5, 5])
    assert session.display_name() == "black"  # stayed put

    _manual_capture(session, (12, 12, 12))
    assert session.measurements["black"].tolist() == pytest.approx([12, 12, 12])
    assert session.index == 0


def test_manual_advance_after_capture_and_goto():
    session = ColorCalibrationSession(sample_frames=2)
    session.start(mode="manual", advance_after_capture=True)
    _manual_capture(session, (5, 5, 5))
    assert session.display_name() == "white"

    session.set_advance_after_capture(False)
    session.goto(patch="skin_medium")
    assert session.display_name() == "skin_medium"
    _manual_capture(session, (80, 110, 160))
    assert "skin_medium" in session.measurements
    assert session.display_name() == "skin_medium"


def test_manual_solve_after_partial_captures():
    session = ColorCalibrationSession(sample_frames=2)
    session.start(mode="manual", advance_after_capture=False)
    solids = _synthetic_solids()
    for name, bgr in solids.items():
        session.goto(patch=name)
        _manual_capture(session, bgr)
    status = session.solve_now()
    assert status["state"] == "ready"
    assert session.solution is not None

    # Re-capture one patch — with all patches already measured, session re-solves.
    session.goto(patch="yellow")
    _manual_capture(session, solids["yellow"])
    assert session.state == "ready"
    assert session.solution is not None


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

        started = app.start_color_calibration(mode="manual", advance_after_capture=True)
        assert started["ok"] is True

        session = app._color_cal
        session.sample_frames = 2
        glow = np.array([16, 10, 20], dtype=np.float64)  # BGR backlight floor
        solids = {}
        for name, bgr in _synthetic_solids().items():
            solids[name] = tuple(
                int(np.clip(v + g, 0, 255)) for v, g in zip(bgr, glow)
            )
        for name, bgr in solids.items():
            app.navigate_color_calibration(patch=name)
            captured = app.capture_color_calibration()
            assert captured["ok"] is True
            frame = np.zeros((180, 320, 3), dtype=np.uint8)
            frame[:] = bgr
            for _ in range(8):
                if session.phase != "sampling":
                    break
                app._last_ctx = type(
                    "C",
                    (),
                    {"debug_images": {"perspective": frame}, "skipped": {}},
                )()
                app._tick_color_calibration()

        if session.state != "ready":
            solved = app.solve_color_calibration()
            assert solved["ok"] is True
        assert session.state == "ready"
        result = app.apply_color_calibration(save=True)
        assert result["ok"] is True
        assert app.config.color.white_balance == "manual"
        assert app.config.color.matrix_enabled is True
        assert len(app.config.color.matrix) == 9
        assert app.config.color.gains.r == pytest.approx(1.0)
        assert app.config.color.calibration.calibrated_at
        assert "skin_medium" in app.config.color.calibration.patch_means_bgr
        assert app.config.color.black_level_enabled is True
        assert app.config.color.black_level.r == pytest.approx(20.0, abs=2.0)
        assert app.config.color.black_level.b == pytest.approx(16.0, abs=2.0)

        path = tmp_path / "config.yaml"
        assert path.exists()
        saved = yaml.safe_load(path.read_text())
        assert saved["color"]["matrix_enabled"] is True
        assert len(saved["color"]["matrix"]) == 9
        assert saved["color"]["calibration"]["matrix"]
        assert saved["color"]["black_level_enabled"] is True
        assert "r" in saved["color"]["black_level"]
    finally:
        app.shutdown()
