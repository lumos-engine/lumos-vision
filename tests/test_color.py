import numpy as np
import pytest

from processor.config.schema import ColorConfig, CropConfig, InsetConfig, ReflectionConfig
from processor.pipeline.context import FrameContext, PipelineState
from processor.stages.color import ColorStage, build_lut
from processor.stages.crop import CropStage, resolve_insets
from processor.stages.reflection import ReflectionStage
from processor.stages.resize import fit_image


def run(stage, image):
    ctx = FrameContext(source=image, image=image)
    stage.process(ctx)
    return ctx


# ------------------------------------------------------------------ the LUT


def test_identity_lut_is_the_identity():
    lut = build_lut((1.0, 1.0, 1.0))
    assert np.array_equal(lut[0, :, 0], np.arange(256, dtype=np.uint8))


def test_gamma_above_one_brightens_midtones():
    dark = build_lut((1, 1, 1), gamma=0.6)[0, 128, 0]
    bright = build_lut((1, 1, 1), gamma=1.6)[0, 128, 0]
    assert bright > 128 > dark


def test_gamma_preserves_black_and_white():
    lut = build_lut((1, 1, 1), gamma=1.8)
    assert lut[0, 0, 0] == 0
    assert lut[0, 255, 0] == 255


def test_channel_gains_are_independent():
    lut = build_lut((1.5, 1.0, 0.5))  # BGR
    assert lut[0, 100, 0] > lut[0, 100, 1] > lut[0, 100, 2]


def test_lut_never_overflows():
    lut = build_lut((3.0, 3.0, 3.0), exposure=3.0, brightness=3.0, contrast=3.0)
    assert lut.dtype == np.uint8
    assert lut.max() <= 255


# ---------------------------------------------------------------- the stage


def grey_image(value=120, size=(60, 80)):
    return np.full((*size, 3), value, dtype=np.uint8)


def test_disabled_corrections_leave_the_image_untouched():
    stage = ColorStage(ColorConfig(), PipelineState())
    image = grey_image()
    assert np.array_equal(run(stage, image).image, image)


def test_saturation_zero_produces_greyscale():
    stage = ColorStage(ColorConfig(saturation=0.0), PipelineState())
    image = np.zeros((10, 10, 3), np.uint8)
    image[:, :, 2] = 200  # pure red
    out = run(stage, image).image
    assert out[:, :, 0].std() == 0
    assert abs(int(out[0, 0, 0]) - int(out[0, 0, 2])) <= 1


def test_saturation_above_one_pushes_colours_apart():
    stage = ColorStage(ColorConfig(saturation=1.8), PipelineState())
    image = np.full((10, 10, 3), 100, np.uint8)
    image[:, :, 2] = 150
    out = run(stage, image).image
    assert int(out[0, 0, 2]) - int(out[0, 0, 0]) > 50


def test_auto_white_balance_neutralises_a_colour_cast():
    stage = ColorStage(
        ColorConfig(white_balance="auto", wb_strength=1.0, wb_smoothing=1.0), PipelineState()
    )
    cast = np.zeros((40, 40, 3), np.uint8)
    cast[:, :, 0] = 160  # far too much blue
    cast[:, :, 1] = 120
    cast[:, :, 2] = 80

    out = run(stage, cast).image
    spread_before = int(cast[0, 0, 0]) - int(cast[0, 0, 2])
    spread_after = int(out[0, 0, 0]) - int(out[0, 0, 2])
    assert abs(spread_after) < abs(spread_before)


def test_white_balance_does_not_change_overall_brightness():
    stage = ColorStage(
        ColorConfig(white_balance="auto", wb_strength=1.0, wb_smoothing=1.0), PipelineState()
    )
    image = np.dstack(
        [np.full((40, 40), v, np.uint8) for v in (150, 120, 90)]
    )
    out = run(stage, image).image
    assert out.mean() == pytest.approx(image.mean(), rel=0.12)


def test_auto_exposure_moves_towards_the_target():
    config = ColorConfig()
    config.exposure.enabled = True
    config.exposure.target_luma = 140.0
    config.exposure.smoothing = 1.0
    stage = ColorStage(config, PipelineState())

    dark = grey_image(60)
    for _ in range(10):
        out = run(stage, dark).image
    assert out.mean() > dark.mean()
    assert stage.status()["exposure_gain"] > 1.0


def test_auto_exposure_holds_its_gain_on_a_black_frame():
    config = ColorConfig()
    config.exposure.enabled = True
    config.exposure.smoothing = 1.0
    stage = ColorStage(config, PipelineState())
    for _ in range(5):
        run(stage, grey_image(70))
    gain = stage.status()["exposure_gain"]

    run(stage, np.zeros((60, 80, 3), np.uint8))
    assert stage.status()["exposure_gain"] == pytest.approx(gain, abs=0.01)


# ----------------------------------------------------------- crop / reflect


def test_resolve_insets_uses_the_uniform_percentage():
    assert resolve_insets(CropConfig(inset_percent=4.0)) == (0.04, 0.04, 0.04, 0.04)


def test_per_edge_inset_overrides_win():
    config = CropConfig(inset_percent=2.0, inset=InsetConfig(top=8.0))
    top, bottom, left, right = resolve_insets(config)
    assert (top, bottom, left, right) == (0.08, 0.02, 0.02, 0.02)


def test_crop_stage_trims_the_requested_fraction():
    stage = CropStage(CropConfig(inset_percent=10.0), PipelineState())
    out = run(stage, np.zeros((100, 200, 3), np.uint8)).image
    assert out.shape[:2] == (80, 160)


def test_absurd_inset_is_refused_rather_than_producing_nothing():
    stage = CropStage(CropConfig(inset_percent=49.0), PipelineState())
    ctx = run(stage, np.zeros((20, 20, 3), np.uint8))
    assert ctx.skipped.get("crop") == "inset too large"
    assert ctx.image.shape[:2] == (20, 20)


def test_reflection_margin_trims_all_four_edges():
    stage = ReflectionStage(ReflectionConfig(margin_percent=5.0), PipelineState())
    out = run(stage, np.zeros((100, 200, 3), np.uint8)).image
    assert out.shape[:2] == (90, 180)


def test_exclusion_zone_is_flooded_with_its_surroundings():
    stage = ReflectionStage(
        ReflectionConfig(margin_percent=0.0, exclusions=[[0.4, 0.4, 0.2, 0.2]]), PipelineState()
    )
    image = np.full((100, 100, 3), 40, np.uint8)
    image[40:60, 40:60] = (0, 0, 255)  # a bright static logo

    out = run(stage, image).image
    assert out[50, 50, 2] < 100, "the logo survived"
    assert out[50, 50].std() < 20


# ------------------------------------------------------------------- resize


def test_stretch_fills_the_target_exactly():
    out = fit_image(np.zeros((100, 100, 3), np.uint8), 640, 360, "stretch")
    assert out.shape[:2] == (360, 640)


def test_letterbox_preserves_aspect_and_pads():
    source = np.full((100, 100, 3), 255, np.uint8)
    out = fit_image(source, 640, 360, "letterbox")
    assert out.shape[:2] == (360, 640)
    assert out[:, 0].max() == 0, "expected black padding at the sides"
    assert out[180, 320].max() == 255


def test_crop_mode_preserves_aspect_and_fills():
    out = fit_image(np.full((100, 100, 3), 255, np.uint8), 640, 360, "crop")
    assert out.shape[:2] == (360, 640)
    assert out.min() == 255, "crop mode should leave no padding"


def test_resize_output_is_contiguous():
    # V4L2 writes raw bytes, so a non-contiguous view would corrupt the frame.
    view = np.zeros((100, 200, 3), np.uint8)[10:90, 20:180]
    assert fit_image(view, 640, 360).flags["C_CONTIGUOUS"]
    assert fit_image(view, view.shape[1], view.shape[0]).flags["C_CONTIGUOUS"]
