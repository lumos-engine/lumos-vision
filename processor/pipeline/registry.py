"""Stage registry.

Adding a future module (subtitle masking, adaptive edge weighting, ...) means
writing the stage class and calling :func:`register_stage`; nothing else in the
pipeline needs to change, and the new name becomes available in
``pipeline.stages`` in the YAML.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from processor.config.schema import Config
from processor.pipeline.context import PipelineState
from processor.pipeline.pipeline import Pipeline
from processor.pipeline.stage import Stage
from processor.stages.blackbars import BlackBarStage
from processor.stages.boundary import BoundaryStage
from processor.stages.color import ColorStage
from processor.stages.crop import CropStage
from processor.stages.movement import MovementStage
from processor.stages.perspective import PerspectiveStage
from processor.stages.reflection import ReflectionStage
from processor.stages.resize import ResizeStage
from processor.utils.logging import get_logger

log = get_logger(__name__)

Factory = Callable[[Config, PipelineState], Stage]
Applier = Callable[[Stage, Config], None]


def _default_apply(stage: Stage, config: Config) -> None:
    section = getattr(config, stage.name, None)
    if section is not None:
        stage.apply_config(section)


def _apply_resize(stage: Stage, config: Config) -> None:
    stage.output = config.output  # type: ignore[attr-defined]
    stage.apply_config(config.resize)


@dataclass(frozen=True)
class StageSpec:
    name: str
    factory: Factory
    apply: Applier = _default_apply
    description: str = ""


STAGE_REGISTRY: dict[str, StageSpec] = {}


def register_stage(spec: StageSpec) -> None:
    STAGE_REGISTRY[spec.name] = spec


def _register_builtins() -> None:
    builtins = [
        StageSpec(
            "movement",
            lambda c, s: MovementStage(c.movement, s),
            description="Detect that the camera itself moved",
        ),
        StageSpec(
            "boundary",
            lambda c, s: BoundaryStage(c.boundary, s),
            description="Locate the TV within the camera frame",
        ),
        StageSpec(
            "perspective",
            lambda c, s: PerspectiveStage(c.perspective, s),
            description="Warp the TV quad into a rectangle",
        ),
        StageSpec(
            "crop",
            lambda c, s: CropStage(c.crop, s),
            description="Trim a fixed inset from the panel edges",
        ),
        StageSpec(
            "blackbars",
            lambda c, s: BlackBarStage(c.blackbars, s),
            description="Detect and remove letterbox / pillarbox bars",
        ),
        StageSpec(
            "reflection",
            lambda c, s: ReflectionStage(c.reflection, s),
            description="Reject reflections and static overlays near the edges",
        ),
        StageSpec(
            "color",
            lambda c, s: ColorStage(c.color, s),
            description="White balance, exposure, gamma, saturation",
        ),
        StageSpec(
            "resize",
            lambda c, s: ResizeStage(c.resize, s, c.output),
            apply=_apply_resize,
            description="Scale to the output resolution",
        ),
    ]
    for spec in builtins:
        register_stage(spec)


_register_builtins()


def build_pipeline(config: Config, state: PipelineState | None = None) -> Pipeline:
    state = state or PipelineState()
    stages: list[Stage] = []
    for name in config.pipeline.stages:
        spec = STAGE_REGISTRY.get(name)
        if spec is None:
            log.warning("Unknown stage %r in pipeline.stages -- skipping", name)
            continue
        stages.append(spec.factory(config, state))

    pipeline = Pipeline(stages, state)
    pipeline.collect_debug = config.pipeline.collect_debug
    return pipeline


def apply_config(pipeline: Pipeline, config: Config) -> None:
    """Push a new config into a running pipeline.

    Stages present in both the old and new configuration keep their temporal
    state, so tweaking a slider in the wizard does not make the picture jump.
    A change to ``pipeline.stages`` rebuilds the list instead.
    """
    existing = {stage.name: stage for stage in pipeline.stages}
    wanted = [n for n in config.pipeline.stages if n in STAGE_REGISTRY]

    rebuilt: list[Stage] = []
    for name in wanted:
        spec = STAGE_REGISTRY[name]
        stage = existing.get(name)
        if stage is None:
            stage = spec.factory(config, pipeline.state)
        else:
            spec.apply(stage, config)
        rebuilt.append(stage)

    pipeline.replace_stages(rebuilt)
    # Do not force collect_debug off here: process_frame enables it whenever
    # the web UI (or another broker) has subscribers.


def describe_stages() -> list[dict[str, Any]]:
    return [
        {"name": spec.name, "description": spec.description} for spec in STAGE_REGISTRY.values()
    ]
