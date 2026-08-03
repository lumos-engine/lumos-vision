"""Black bar detection: accuracy first, then the far more important question
of whether the crop stays still when it should."""

import cv2
import numpy as np
import pytest

from processor.config.schema import BlackBarsConfig
from processor.pipeline.context import FrameContext, PipelineState
from processor.stages.blackbars import BlackBarStage, _symmetric_pair, measure_bars
from processor.testing.scene import render_panel


def test_symmetric_pair_ignores_one_sided_dark_strips():
    # Cinema letterbox: both sides agree → keep the larger reading.
    assert _symmetric_pair(0.13, 0.10) == pytest.approx(0.13)
    # Jellyfin-style dark cast row only at the bottom → do not crop.
    assert _symmetric_pair(0.0, 0.18) == 0.0
    assert _symmetric_pair(0.02, 0.20) == 0.0

PANEL = (768, 432)


def bars_for(aspect: float, **kwargs) -> dict[str, float]:
    panel = render_panel(3.0, PANEL, aspect, **kwargs)
    gray = cv2.cvtColor(panel, cv2.COLOR_BGR2GRAY)
    return measure_bars(gray, luma_threshold=22, percentile=96.0)


@pytest.mark.parametrize(
    "aspect,expected",
    [
        (2.39, (1 - (16 / 9) / 2.39) / 2),
        (2.35, (1 - (16 / 9) / 2.35) / 2),
        (1.85, (1 - (16 / 9) / 1.85) / 2),
        (16 / 9, 0.0),
    ],
)
def test_letterbox_measurement_is_accurate(aspect, expected):
    bars = bars_for(aspect)
    assert bars["top"] == pytest.approx(expected, abs=0.005)
    assert bars["bottom"] == pytest.approx(expected, abs=0.005)
    assert bars["left"] == 0.0
    assert bars["right"] == 0.0


def test_pillarbox_is_detected():
    bars = bars_for(4 / 3)
    expected = (1 - (4 / 3) / (16 / 9)) / 2
    assert bars["left"] == pytest.approx(expected, abs=0.005)
    assert bars["right"] == pytest.approx(expected, abs=0.005)
    assert bars["top"] == 0.0


def test_subtitles_and_logos_do_not_defeat_detection():
    with_overlays = bars_for(2.39, show_logo=True, show_subtitles=True)
    without = bars_for(2.39, show_logo=False, show_subtitles=False)
    assert with_overlays["top"] == pytest.approx(without["top"], abs=0.01)


def test_a_fully_black_frame_reports_nothing():
    black = np.zeros((432, 768), dtype=np.uint8)
    assert measure_bars(black, 22, 96.0) == {"top": 0.0, "bottom": 0.0, "left": 0.0, "right": 0.0}


# ---------------------------------------------------------------- the stage


def make_stage(**overrides) -> BlackBarStage:
    config = BlackBarsConfig(
        window=9, hold_frames=6, change_threshold_percent=0.8, max_step_percent=1.0, **overrides
    )
    return BlackBarStage(config, PipelineState())


def run(stage: BlackBarStage, panel: np.ndarray, frames: int = 1) -> FrameContext:
    ctx = FrameContext(source=panel, image=panel)
    for _ in range(frames):
        ctx = FrameContext(source=panel, image=panel)
        stage.process(ctx)
    return ctx


def test_stage_converges_on_the_true_crop():
    stage = make_stage()
    panel = render_panel(3.0, PANEL, 2.39)
    run(stage, panel, frames=80)
    expected = (1 - (16 / 9) / 2.39) / 2
    assert stage.status()["applied_percent"]["top"] == pytest.approx(expected * 100, abs=1.0)
    assert stage.status()["content_aspect"] == pytest.approx(2.39, abs=0.06)


def test_stage_output_is_cropped():
    stage = make_stage()
    panel = render_panel(3.0, PANEL, 2.39)
    ctx = run(stage, panel, frames=80)
    assert ctx.image.shape[0] < panel.shape[0]
    assert ctx.image.shape[1] == panel.shape[1]


def test_crop_does_not_flicker_on_changing_content():
    """The bar size must hold still while the picture inside it moves."""
    stage = make_stage()
    history = []
    for i in range(120):
        panel = render_panel(i * 0.1, PANEL, 2.39)
        ctx = FrameContext(source=panel, image=panel)
        stage.process(ctx)
        history.append(stage.status()["pixels"]["top"])

    settled = history[60:]
    assert max(settled) - min(settled) <= 2, f"crop wandered over {settled}"


def test_a_brief_dark_scene_does_not_collapse_the_crop():
    stage = make_stage()
    panel = render_panel(3.0, PANEL, 2.39)
    run(stage, panel, frames=80)
    before = stage.status()["pixels"]["top"]

    black = np.zeros_like(panel)
    run(stage, black, frames=20)
    assert stage.status()["pixels"]["top"] == pytest.approx(before, abs=1)


def test_aspect_change_is_followed_but_gradually():
    stage = make_stage()
    scope = render_panel(3.0, PANEL, 2.39)
    run(stage, scope, frames=80)
    start = stage.status()["pixels"]["top"]
    assert start > 30

    widescreen = render_panel(3.0, PANEL, 16 / 9)
    ctx = FrameContext(source=widescreen, image=widescreen)
    stage.process(ctx)
    assert stage.status()["pixels"]["top"] == pytest.approx(start, abs=2), "reacted too fast"

    run(stage, widescreen, frames=80)
    assert stage.status()["pixels"]["top"] <= 2, "never followed the change"


def test_max_crop_percent_is_respected():
    stage = make_stage(max_crop_percent=5.0)
    panel = render_panel(3.0, PANEL, 2.39)
    run(stage, panel, frames=80)
    assert stage.status()["applied_percent"]["top"] <= 5.0 + 1e-6


def test_letterbox_and_pillarbox_are_not_applied_together():
    stage = make_stage()
    # A frame with a bright square in the middle looks letterboxed *and*
    # pillarboxed; only the larger pair should win.
    panel = np.zeros((432, 768, 3), dtype=np.uint8)
    panel[150:280, 100:660] = 200
    run(stage, panel, frames=60)
    pixels = stage.status()["pixels"]
    assert not (pixels["top"] > 0 and pixels["left"] > 0)


def test_reset_clears_the_crop():
    stage = make_stage()
    run(stage, render_panel(3.0, PANEL, 2.39), frames=60)
    assert stage.status()["pixels"]["top"] > 0
    stage.reset()
    assert stage.status()["pixels"]["top"] == 0
