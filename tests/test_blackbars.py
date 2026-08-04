"""Black bar detection: accuracy first, then the far more important question
of whether the crop stays still when it should."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from processor.config.schema import BlackBarsConfig
from processor.pipeline.context import FrameContext, PipelineState
from processor.stages.blackbars import BlackBarStage, _symmetric_pair, measure_bars
from processor.testing.scene import render_panel


def test_symmetric_pair_ignores_one_sided_dark_strips():
    # Cinema letterbox: both sides present → keep the larger reading.
    assert _symmetric_pair(0.13, 0.10) == pytest.approx(0.13)
    # Uneven sides (bright content against one bar) still crop.
    assert _symmetric_pair(0.14, 0.06) == pytest.approx(0.14)
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


@pytest.mark.parametrize(
    "filename",
    ["minions_glow.png", "minions_red_bars.png"],
)
def test_noisy_camera_letterbox_frames_are_detected(filename):
    """Captured Logitech frames: bars are dark red/gray, not true black."""
    path = Path(__file__).parent / "fixtures" / "blackbars" / filename
    gray = cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2GRAY)
    bars = measure_bars(gray, luma_threshold=48, percentile=96.0)
    assert bars["top"] == pytest.approx(0.125, abs=0.03)
    assert bars["bottom"] == pytest.approx(0.125, abs=0.03)
    assert bars["left"] < 0.02
    assert bars["right"] < 0.02


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

    # Sticky release + hold needs a long clean streak before cinema unlocks.
    run(stage, widescreen, frames=220)
    assert stage.status()["pixels"]["top"] <= 2, "never followed the change"


def test_symmetric_crop_keeps_top_and_bottom_equal():
    stage = make_stage()
    panel = render_panel(3.0, PANEL, 2.39)
    run(stage, panel, frames=80)
    pixels = stage.status()["pixels"]
    assert pixels["top"] == pixels["bottom"]


def test_crop_does_not_strobe_when_detection_flaps():
    """Noisy cams alternate 'bars' / 'no bars'; the applied crop must stick."""
    stage = make_stage()
    cinema = render_panel(3.0, PANEL, 2.39)
    flat = render_panel(3.0, PANEL, 16 / 9)
    run(stage, cinema, frames=80)
    locked = stage.status()["pixels"]["top"]
    assert locked > 30

    history = []
    for i in range(90):
        panel = flat if i % 3 == 0 else cinema
        ctx = FrameContext(source=panel, image=panel)
        stage.process(ctx)
        history.append(stage.status()["pixels"]["top"])

    assert min(history) >= locked - 3, f"crop strobed off: {history[:20]}..."
    assert max(history) - min(history) <= 4


def test_overcrop_shrinks_back_quickly():
    """A brief too-large crop (dark scene) must give the picture back fast."""
    stage = make_stage()
    cinema = render_panel(3.0, PANEL, 2.39)
    run(stage, cinema, frames=80)
    good = stage.status()["pixels"]["top"]
    assert good > 30

    # Force an over-aggressive letterbox lock (dark-scene false positive).
    assert stage._vertical is not None
    stage._vertical.force(0.22)
    stage._letterbox_locked = True
    run(stage, cinema, frames=1)
    assert stage.status()["pixels"]["top"] > good + 10

    run(stage, cinema, frames=20)
    recovered = stage.status()["pixels"]["top"]
    assert recovered == pytest.approx(good, abs=4), f"still over-cropped: {recovered} vs {good}"


def test_max_crop_percent_is_respected():
    stage = make_stage(max_crop_top_bottom_percent=5.0, max_crop_left_right_percent=5.0)
    panel = render_panel(3.0, PANEL, 2.39)
    run(stage, panel, frames=80)
    assert stage.status()["applied_percent"]["top"] <= 5.0 + 1e-6


def test_letterbox_and_pillarbox_have_separate_caps():
    stage = make_stage(
        max_crop_top_bottom_percent=16.0, max_crop_left_right_percent=12.5
    )
    assert stage._crop_limits() == pytest.approx((0.16, 0.125))


def test_letterbox_and_pillarbox_are_not_applied_together():
    stage = make_stage()
    # A frame with a bright square in the middle looks letterboxed *and*
    # pillarboxed; only the larger pair should win.
    panel = np.zeros((432, 768, 3), dtype=np.uint8)
    panel[150:280, 100:660] = 200
    run(stage, panel, frames=60)
    pixels = stage.status()["pixels"]
    assert not (pixels["top"] > 0 and pixels["left"] > 0)


def test_sticky_filters_cannot_keep_both_axes_cropping():
    """Even if both axis filters were locked, applied crop is either/or."""
    stage = make_stage()
    stage._vertical.force(0.12)
    stage._horizontal.force(0.08)
    stage._letterbox_locked = True
    stage._pillarbox_locked = True
    # Full-frame content: measurement wants no bars, but sticky would hold.
    panel = np.full((432, 768, 3), 180, dtype=np.uint8)
    stage.process(FrameContext(source=panel, image=panel.copy()))
    pixels = stage.status()["pixels"]
    assert not (pixels["top"] > 0 and pixels["left"] > 0)
    # Dominant letterbox lock wins over the smaller pillarbox hold.
    assert pixels["top"] > 0
    assert pixels["left"] == 0


def test_reset_clears_the_crop():
    stage = make_stage()
    run(stage, render_panel(3.0, PANEL, 2.39), frames=60)
    assert stage.status()["pixels"]["top"] > 0
    stage.reset()
    assert stage.status()["pixels"]["top"] == 0
