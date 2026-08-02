"""TV detection against the synthetic living room, where ground truth is known."""

import numpy as np
import pytest

from processor.config.schema import BoundaryConfig, MovementConfig
from processor.pipeline.context import FrameContext, PipelineState
from processor.stages.boundary import BoundaryStage
from processor.stages.detection import (
    TvQuadDetector,
    complete_to_aspect,
    quads_from_contour,
)
from processor.stages.movement import MovementStage
from processor.testing.generate import POSES
from processor.testing.scene import SceneParams, SyntheticScene
from processor.utils.geometry import (
    max_corner_shift,
    order_corners,
    rectangle_aspect_ratio,
)


def warm_up(detector: TvQuadDetector, scene: SyntheticScene, frames: int = 30) -> None:
    for i in range(frames):
        detector.observe(scene.frame(i * 0.12))


#: Accuracy target: every corner within 2.5 % of the frame width of truth.
TOLERANCE = 0.025


def assert_detects(scene: SyntheticScene, tolerance: float = TOLERANCE):
    detector = TvQuadDetector(BoundaryConfig())
    warm_up(detector, scene)

    result = detector.detect(scene.frame(4.0))
    assert result is not None, "no TV found"
    assert result.confidence >= 0.3
    error = max_corner_shift(result.quad, scene.true_quad)
    assert error < tolerance * scene.params.width, (
        f"corners off by {error:.1f}px (limit {tolerance * scene.params.width:.1f}px)"
    )
    return result


@pytest.mark.parametrize("aspect", [2.39, 2.35, 1.85, 16 / 9, 4 / 3])
def test_detects_the_panel_for_any_content_aspect(aspect):
    """The panel, not the picture.

    A letterboxed film only *moves* in the middle of the screen, and the
    strongest edge in the frame is the bar/picture boundary rather than the
    panel border, so this is the case where a naive detector locks onto the
    wrong rectangle -- and then gets it wrong again when the next programme
    has a different shape.
    """
    assert_detects(SyntheticScene(SceneParams(shake_px=0.0, content_aspect=aspect)))


@pytest.mark.parametrize("pose", ["steep", "small", "offcentre"])
def test_detects_the_tv_from_awkward_camera_angles(pose):
    assert_detects(SyntheticScene(SceneParams(shake_px=0.0, quad=POSES[pose])))


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(dict(noise_sigma=9.0), id="noisy sensor"),
        pytest.param(dict(reflection_strength=0.55), id="heavy glare"),
        pytest.param(dict(exposure=0.35, reflection_strength=0.05), id="dark room"),
        pytest.param(dict(exposure=1.4), id="bright room"),
        pytest.param(dict(bezel_px=1), id="bezel-less TV"),
        pytest.param(dict(bezel_px=16), id="thick bezel"),
    ],
)
def test_detects_the_tv_in_difficult_conditions(overrides):
    assert_detects(SyntheticScene(SceneParams(shake_px=0.0, **overrides)))


def test_detected_shape_really_is_16_by_9():
    """The recovered rectangle aspect is the check that matters; the
    on-screen ratio of a correct detection is not 16:9 and never will be."""
    scene = SyntheticScene(SceneParams(shake_px=0.0, content_aspect=2.39))
    result = assert_detects(scene)
    recovered = rectangle_aspect_ratio(result.quad, (scene.params.width, scene.params.height))
    assert recovered == pytest.approx(16 / 9, rel=0.10)


def test_corners_are_ordered_top_left_first():
    scene = SyntheticScene(SceneParams(shake_px=0.0))
    detector = TvQuadDetector(BoundaryConfig())
    warm_up(detector, scene)

    quad = detector.detect(scene.frame(4.0)).quad
    assert np.allclose(quad, order_corners(quad))
    assert quad[0][0] < quad[1][0]  # TL left of TR
    assert quad[0][1] < quad[3][1]  # TL above BL


def test_a_blank_wall_yields_no_detection():
    detector = TvQuadDetector(BoundaryConfig())
    blank = np.full((540, 960, 3), 60, dtype=np.uint8)
    for _ in range(20):
        detector.observe(blank)
    result = detector.detect(blank)
    assert result is None or result.confidence < 0.3


def test_complete_to_aspect_recovers_the_panel_from_the_picture():
    """Given the picture area of a 2.39:1 film, adding the bars back must
    reproduce the panel it is playing on."""
    scene = SyntheticScene(SceneParams(shake_px=0.0, content_aspect=2.39))
    size = (scene.params.width, scene.params.height)
    panel = scene.true_quad

    pad = (1 - (16 / 9) / 2.39) / 2
    from processor.utils.geometry import inset_quad

    picture = inset_quad(panel, left=0, right=0, top=pad, bottom=pad)
    completed = complete_to_aspect(picture, 16 / 9, size)

    assert completed is not None
    assert max_corner_shift(completed, panel) < 0.01 * scene.params.width


def test_complete_to_aspect_leaves_a_16_by_9_quad_alone():
    scene = SyntheticScene(SceneParams(shake_px=0.0))
    size = (scene.params.width, scene.params.height)
    assert complete_to_aspect(scene.true_quad, 16 / 9, size) is None


def test_quads_from_contour_always_returns_four_point_quads():
    import cv2

    contour = np.array([[[10, 10]], [[200, 14]], [[205, 120]], [[6, 110]]], dtype=np.int32)
    quads = quads_from_contour(contour)
    assert quads
    assert all(q.shape == (4, 2) for q in quads)
    assert cv2.contourArea(quads[0].astype(np.float32)) > 0


# ------------------------------------------------------------------- stage


def run_stage(stage: BoundaryStage, scene: SyntheticScene, frames: int, t0: float = 0.0):
    ctx = None
    for i in range(frames):
        image = scene.frame(t0 + i * 0.12)
        ctx = FrameContext(source=image, image=image)
        stage.process(ctx)
    return ctx


def test_stage_locks_on_and_then_stops_detecting():
    """Once the activity map is mature and a fit is accepted, detection stops.

    Continuing to burn CPU on a camera that has not moved since Tuesday is the
    thing this whole stage exists to avoid.
    """
    state = PipelineState()
    stage = BoundaryStage(BoundaryConfig(mode="auto"), state)
    scene = SyntheticScene(SceneParams(shake_px=0.0))

    run_stage(stage, scene, 60)
    assert stage.status()["locked"]
    attempts = stage.status()["attempts"]

    run_stage(stage, scene, 40, t0=8.0)
    assert stage.status()["attempts"] == attempts, "kept re-detecting after locking on"


def test_stage_publishes_corners_before_it_locks():
    """The output should be usable within a second, not only once mature."""
    state = PipelineState()
    stage = BoundaryStage(BoundaryConfig(mode="auto"), state)
    scene = SyntheticScene(SceneParams(shake_px=0.0))

    run_stage(stage, scene, 16)
    assert state.corners is not None
    assert state.corners_source != "fallback"
    assert not stage.status()["locked"]


def test_manual_corners_are_used_verbatim():
    state = PipelineState()
    corners = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
    stage = BoundaryStage(BoundaryConfig(mode="manual", corners=corners), state)
    scene = SyntheticScene(SceneParams(shake_px=0.0))

    run_stage(stage, scene, 5)
    assert state.corners_source == "manual"
    assert np.allclose(state.corners[0], [0.2 * 960, 0.2 * 540], atol=1)


def test_forced_recalibration_re_detects():
    state = PipelineState()
    stage = BoundaryStage(BoundaryConfig(mode="auto"), state)
    scene = SyntheticScene(SceneParams(shake_px=0.0))
    run_stage(stage, scene, 60)
    before = stage.status()["successes"]
    assert stage.status()["locked"]

    stage.force_recalibration()
    assert not stage.status()["locked"]

    # A recalibration throws away the activity history, so it takes a fresh
    # window to build a new one.
    run_stage(stage, scene, 60, t0=8.0)
    assert stage.status()["successes"] > before
    assert stage.status()["locked"]


def test_corners_are_stable_across_frames_despite_shake():
    state = PipelineState()
    stage = BoundaryStage(BoundaryConfig(mode="auto"), state)
    scene = SyntheticScene(SceneParams(shake_px=0.6))

    run_stage(stage, scene, 40)
    locked = state.corners.copy()
    run_stage(stage, scene, 60, t0=6.0)
    assert max_corner_shift(state.corners, locked) < 3.0


def test_falls_back_to_the_full_frame_when_nothing_is_found():
    state = PipelineState()
    stage = BoundaryStage(BoundaryConfig(mode="auto"), state)
    blank = np.full((540, 960, 3), 60, dtype=np.uint8)
    for _ in range(10):
        stage.process(FrameContext(source=blank, image=blank))

    assert state.corners is not None
    assert state.corners_source == "fallback"
    assert state.corner_confidence == 0.0


# ---------------------------------------------------------------- movement


def calibrated_movement_stage(scene: SyntheticScene, **overrides):
    state = PipelineState()
    state.set_corners(scene.true_quad, 1.0, "manual")
    config = MovementConfig(check_interval=0.0, consecutive=2, settle_time=0.0, **overrides)
    return MovementStage(config, state), state


def feed(stage: MovementStage, scene: SyntheticScene, count: int, t0: float = 0.0) -> None:
    for i in range(count):
        image = scene.frame(t0 + i * 0.2)
        stage.process(FrameContext(source=image, image=image))


def test_movement_stage_does_nothing_without_a_calibration():
    state = PipelineState()
    stage = MovementStage(MovementConfig(check_interval=0.0, consecutive=2), state)
    scene = SyntheticScene(SceneParams(shake_px=0.0))
    feed(stage, scene, 20)
    assert not state.recalibration_pending


def test_movement_stage_ignores_a_static_camera():
    scene = SyntheticScene(SceneParams(shake_px=0.0))
    stage, state = calibrated_movement_stage(scene)
    feed(stage, scene, 25)
    assert not state.recalibration_pending, "TV content was mistaken for camera movement"


def test_movement_stage_tolerates_small_shake():
    scene = SyntheticScene(SceneParams(shake_px=1.0))
    stage, state = calibrated_movement_stage(scene)
    feed(stage, scene, 25)
    assert not state.recalibration_pending


def test_movement_stage_notices_a_bump():
    scene = SyntheticScene(SceneParams(shake_px=0.0, bump_at=None))
    stage, state = calibrated_movement_stage(scene)
    feed(stage, scene, 10)
    assert not state.recalibration_pending

    # Shift the whole scene, which is what a knocked camera looks like.
    scene.params.bump_at = 0.0
    scene.params.bump_offset = (0.05, 0.03)
    feed(stage, scene, 10, t0=3.0)
    assert state.recalibration_pending


def test_movement_stage_ignores_a_lighting_change():
    """Turning the room lights on changes every pixel but moves nothing.

    This is the case a brightness-difference metric gets backwards, so it is
    worth an explicit test rather than trusting the threshold.
    """
    scene = SyntheticScene(SceneParams(shake_px=0.0))
    stage, state = calibrated_movement_stage(scene)
    feed(stage, scene, 10)

    brighter = SyntheticScene(SceneParams(shake_px=0.0, exposure=1.35, seed=scene.params.seed))
    for i in range(12):
        image = brighter.frame(3.0 + i * 0.2)
        stage.process(FrameContext(source=image, image=image))

    assert not state.recalibration_pending


def test_movement_score_is_far_larger_for_a_bump_than_for_lighting():
    scene = SyntheticScene(SceneParams(shake_px=0.0, bump_at=None))
    stage, _ = calibrated_movement_stage(scene, ncc_threshold=1e6)
    feed(stage, scene, 6)

    brighter = SyntheticScene(SceneParams(shake_px=0.0, exposure=1.35, seed=scene.params.seed))
    for i in range(3):
        image = brighter.frame(3.0 + i * 0.2)
        stage.process(FrameContext(source=image, image=image))
    lighting_score = stage.status()["score"]

    bumped = SyntheticScene(
        SceneParams(shake_px=0.0, bump_at=0.0, bump_offset=(0.05, 0.03), seed=scene.params.seed)
    )
    for i in range(3):
        image = bumped.frame(3.0 + i * 0.2)
        stage.process(FrameContext(source=image, image=image))
    bump_score = stage.status()["score"]

    assert bump_score > 10 * max(lighting_score, 0.01)
