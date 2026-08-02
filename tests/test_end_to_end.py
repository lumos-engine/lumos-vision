"""Whole-pipeline behaviour, from a camera frame to what a consumer sees."""

import numpy as np
import pytest

from processor.app import Processor
from processor.camera.base import Frame
from processor.config.schema import Config
from processor.testing.scene import SceneParams, SyntheticScene
from processor.utils.geometry import max_corner_shift


def build(**overrides) -> Processor:
    base = {
        "camera": {"source": "synthetic", "replay_fps": 200},
        "output": {"width": 640, "height": 360, "fps": 15, "v4l2": {"enabled": False}},
        "boundary": {"mode": "auto"},
        "logging": {"stats_interval": 0},
    }
    base.update(overrides)
    return Processor(Config.from_dict(base)).start()


def drive(processor: Processor, scene: SyntheticScene, frames: int = 60, step: float = 0.12):
    """Feed the pipeline at controlled scene times (fast and deterministic)."""
    ctx = None
    for i in range(frames):
        ctx = processor.process_frame(Frame(image=scene.frame(i * step), index=i))
    return ctx


@pytest.fixture
def scene() -> SyntheticScene:
    return SyntheticScene(SceneParams(shake_px=0.3, content_aspect=2.39))


def test_output_has_the_configured_size_and_format(scene):
    processor = build()
    try:
        ctx = drive(processor, scene, frames=30)
    finally:
        processor.shutdown()

    assert ctx.image.shape == (360, 640, 3)
    assert ctx.image.dtype == np.uint8
    assert ctx.image.flags["C_CONTIGUOUS"], "sinks write raw bytes; must be contiguous"


def test_pipeline_locks_onto_the_tv(scene):
    processor = build()
    try:
        drive(processor, scene, frames=60)
        state = processor.state
        assert state.corners is not None
        assert state.corner_confidence > 0.3
        error = max_corner_shift(state.corners, scene.true_quad)
        assert error < 0.03 * scene.params.width
    finally:
        processor.shutdown()


def test_black_bars_are_removed_from_the_output(scene):
    processor = build()
    try:
        drive(processor, scene, frames=120)
        bars = processor.pipeline.get("blackbars").status()
    finally:
        processor.shutdown()

    assert bars["pixels"]["top"] > 0, "2.39:1 content should be cropped"
    assert bars["content_aspect"] == pytest.approx(2.39, rel=0.15)


def test_16_by_9_content_is_not_cropped():
    processor = build()
    scene = SyntheticScene(SceneParams(shake_px=0.0, content_aspect=16 / 9))
    try:
        drive(processor, scene, frames=100)
        bars = processor.pipeline.get("blackbars").status()
    finally:
        processor.shutdown()
    assert bars["pixels"]["top"] <= 2
    assert bars["pixels"]["left"] <= 2


def test_the_output_is_mostly_picture_not_wall(scene):
    """The whole point: after rectification and cropping, the frame should be
    the TV image, with the room gone."""
    processor = build()
    try:
        ctx = drive(processor, scene, frames=120)
    finally:
        processor.shutdown()

    # The synthetic wall is dark and desaturated; the picture is neither.
    hsv_saturation = ctx.image.max(axis=2).astype(int) - ctx.image.min(axis=2).astype(int)
    assert ctx.image.mean() > 60, "output looks like a dark wall"
    assert hsv_saturation.mean() > 25, "output lost the picture's colour"

    border = np.concatenate(
        [ctx.image[:6].ravel(), ctx.image[-6:].ravel(),
         ctx.image[:, :6].ravel(), ctx.image[:, -6:].ravel()]
    )
    assert border.mean() > 40, "the edges still show black bars or bezel"


def test_stages_can_be_disabled_at_runtime(scene):
    processor = build()
    try:
        drive(processor, scene, frames=30)
        processor.pipeline.set_enabled("perspective", False)
        ctx = drive(processor, scene, frames=5)
        assert ctx.skipped["perspective"] == "disabled"
        assert ctx.image.shape == (360, 640, 3), "output size must survive"
    finally:
        processor.shutdown()


def test_pipeline_stays_within_its_latency_budget(scene):
    """Processing cost per frame, excluding the camera and the renderer."""
    processor = build()
    try:
        drive(processor, scene, frames=60)
        total = processor.pipeline.timings.total_ms
    finally:
        processor.shutdown()

    # Generous: the target machine is roughly 4x slower than a dev laptop and
    # the budget at 15 fps is 66 ms.
    assert total < 15.0, f"pipeline took {total:.1f} ms/frame"


def test_manual_corners_survive_a_config_round_trip(tmp_path, scene):
    from processor.config.loader import load_config

    processor = build()
    try:
        corners = [[0.15, 0.2], [0.85, 0.18], [0.86, 0.8], [0.14, 0.78]]
        processor.set_manual_corners(corners)
        drive(processor, scene, frames=5)
        path = processor.save(tmp_path / "config.yaml")
    finally:
        processor.shutdown()

    reloaded = load_config(path)
    assert reloaded.boundary.corners == corners
    assert reloaded.boundary.mode == "manual"


def test_recalibration_recovers_after_the_camera_is_bumped():
    processor = build(movement={"check_interval": 0.0, "consecutive": 2, "settle_time": 0.0})
    before = SyntheticScene(SceneParams(shake_px=0.0, bump_at=None))
    try:
        drive(processor, before, frames=60)
        assert processor.state.corners is not None

        bumped = SyntheticScene(
            SceneParams(shake_px=0.0, bump_at=0.0, bump_offset=(0.06, 0.04))
        )
        drive(processor, bumped, frames=90, step=0.12)

        error = max_corner_shift(processor.state.corners, bumped.true_quad)
        assert error < 0.04 * bumped.params.width, (
            f"corners still {error:.0f}px from the moved TV"
        )
    finally:
        processor.shutdown()


def test_status_is_json_serialisable(scene):
    import json

    processor = build()
    try:
        drive(processor, scene, frames=20)
        json.dumps(processor.status())
    finally:
        processor.shutdown()


def test_file_output_records_the_processed_stream(tmp_path, scene):
    import cv2

    target = tmp_path / "out.mp4"
    processor = build(
        output={
            "width": 320,
            "height": 180,
            "fps": 15,
            "v4l2": {"enabled": False},
            "file": {"enabled": True, "path": str(target)},
        }
    )
    try:
        drive(processor, scene, frames=30)
    finally:
        processor.shutdown()

    assert target.exists()
    capture = cv2.VideoCapture(str(target))
    ok, frame = capture.read()
    capture.release()
    assert ok and frame.shape[:2] == (180, 320)
