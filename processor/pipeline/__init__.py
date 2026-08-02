"""The processing pipeline: an ordered list of independent stages."""

from typing import Any

from processor.pipeline.context import FrameContext, PipelineState
from processor.pipeline.pipeline import Pipeline
from processor.pipeline.stage import Stage

__all__ = [
    "FrameContext",
    "Pipeline",
    "PipelineState",
    "STAGE_REGISTRY",
    "Stage",
    "apply_config",
    "build_pipeline",
    "describe_stages",
    "register_stage",
]

_LAZY = {
    "STAGE_REGISTRY",
    "apply_config",
    "build_pipeline",
    "describe_stages",
    "register_stage",
}


def __getattr__(name: str) -> Any:
    # The registry imports every stage, and stages import this package for the
    # Stage base class.  Resolving those names on first use instead of at
    # import time breaks the cycle without splitting the public API apart.
    if name in _LAZY:
        from processor.pipeline import registry

        return getattr(registry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
