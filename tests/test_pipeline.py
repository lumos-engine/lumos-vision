"""Pipeline mechanics: ordering, optionality, and failure isolation."""

import numpy as np

from processor.camera.base import Frame
from processor.config.schema import Config
from processor.pipeline.context import FrameContext, PipelineState
from processor.pipeline.pipeline import Pipeline
from processor.pipeline.registry import apply_config, build_pipeline, describe_stages
from processor.pipeline.stage import Stage


class RecordingStage(Stage):
    def __init__(self, name, state, log, fail=False):
        super().__init__(type("Cfg", (), {"enabled": True})(), state)
        self.name = name
        self._log = log
        self._fail = fail
        self.calls = 0

    def process(self, ctx: FrameContext) -> None:
        self.calls += 1
        if self._fail:
            raise RuntimeError(f"{self.name} exploded")
        self._log.append(self.name)
        ctx.record(self.name, calls=self.calls)


def make_frame(width=64, height=36) -> Frame:
    return Frame(image=np.full((height, width, 3), 128, dtype=np.uint8), index=1)


def test_stages_run_in_order():
    log: list[str] = []
    state = PipelineState()
    pipeline = Pipeline([RecordingStage(n, state, log) for n in "abc"], state)
    pipeline.process(make_frame())
    assert log == ["a", "b", "c"]


def test_disabled_stages_are_skipped_but_kept():
    log: list[str] = []
    state = PipelineState()
    pipeline = Pipeline([RecordingStage(n, state, log) for n in "abc"], state)
    pipeline.set_enabled("b", False)

    ctx = pipeline.process(make_frame())
    assert log == ["a", "c"]
    assert ctx.skipped["b"] == "disabled"
    assert pipeline.get("b") is not None

    assert pipeline.toggle("b") is True
    pipeline.process(make_frame())
    assert log == ["a", "c", "a", "b", "c"]


def test_toggling_an_unknown_stage_reports_it():
    assert Pipeline([], PipelineState()).toggle("nope") is None
    assert Pipeline([], PipelineState()).set_enabled("nope", True) is False


def test_a_failing_stage_does_not_stop_the_others():
    log: list[str] = []
    state = PipelineState()
    stages = [
        RecordingStage("a", state, log),
        RecordingStage("boom", state, log, fail=True),
        RecordingStage("c", state, log),
    ]
    pipeline = Pipeline(stages, state)

    ctx = pipeline.process(make_frame())
    assert log == ["a", "c"], "a failing stage took the pipeline down with it"
    assert ctx.skipped["boom"].startswith("error")
    assert pipeline.status()["errors"]["boom"] == 1


def test_timings_are_recorded_per_stage():
    state = PipelineState()
    log: list[str] = []
    pipeline = Pipeline([RecordingStage(n, state, log) for n in "ab"], state)
    pipeline.process(make_frame())
    timings = pipeline.timings.as_dict()
    assert set(timings) == {"a", "b"}
    assert all(v >= 0 for v in timings.values())


# ------------------------------------------------------------------ registry


def test_build_pipeline_follows_the_configured_order():
    config = Config.from_dict({"pipeline": {"stages": ["color", "resize", "boundary"]}})
    pipeline = build_pipeline(config)
    assert pipeline.stage_names == ["color", "resize", "boundary"]


def test_unknown_stage_names_are_ignored():
    config = Config.from_dict({"pipeline": {"stages": ["color", "teleport", "resize"]}})
    assert build_pipeline(config).stage_names == ["color", "resize"]


def test_an_empty_pipeline_is_a_passthrough():
    config = Config.from_dict({"pipeline": {"stages": []}})
    pipeline = build_pipeline(config)
    frame = make_frame()
    ctx = pipeline.process(frame)
    assert ctx.image is frame.image


def test_apply_config_preserves_stage_instances():
    config = Config()
    pipeline = build_pipeline(config)
    before = pipeline.get("color")

    updated = Config.from_dict({"color": {"gamma": 1.5}})
    apply_config(pipeline, updated)

    assert pipeline.get("color") is before, "stage was rebuilt, losing its temporal state"
    assert pipeline.get("color").config.gamma == 1.5


def test_apply_config_can_change_the_stage_list():
    pipeline = build_pipeline(Config())
    apply_config(pipeline, Config.from_dict({"pipeline": {"stages": ["color", "resize"]}}))
    assert pipeline.stage_names == ["color", "resize"]


def test_apply_config_updates_the_resize_output_size():
    pipeline = build_pipeline(Config())
    apply_config(pipeline, Config.from_dict({"output": {"width": 320, "height": 180}}))
    assert pipeline.get("resize").status()["size"] == [320, 180]


def test_every_registered_stage_is_described():
    for spec in describe_stages():
        assert spec["description"], f"{spec['name']} has no description"


def test_default_pipeline_covers_the_documented_stages():
    names = build_pipeline(Config()).stage_names
    for expected in ("boundary", "perspective", "crop", "blackbars", "reflection", "color"):
        assert expected in names


# ------------------------------------------------------------------- context


def test_pipeline_state_recalibration_request_is_one_shot():
    state = PipelineState()
    assert not state.take_recalibration_request()
    state.request_recalibration("test")
    assert state.take_recalibration_request()
    assert not state.take_recalibration_request()


def test_pipeline_state_snapshot_is_json_friendly():
    import json

    state = PipelineState()
    state.set_corners(np.array([[0, 0], [10, 0], [10, 5], [0, 5]], np.float32), 0.9, "manual")
    json.dumps(state.snapshot())


def test_frame_context_latency_is_positive():
    ctx = FrameContext(source=np.zeros((4, 4, 3), np.uint8), image=np.zeros((4, 4, 3), np.uint8))
    assert ctx.latency_ms >= 0
