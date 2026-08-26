"""Pipeline runner."""

from __future__ import annotations

import threading
from typing import Any, Iterable, Iterator

import numpy as np

from processor.camera.base import Frame
from processor.pipeline.context import FrameContext, PipelineState
from processor.pipeline.stage import Stage
from processor.utils.logging import get_logger
from processor.utils.timing import StageTimings

log = get_logger(__name__)


class Pipeline:
    """Runs stages in order, isolating failures and timing each one.

    A stage that raises is logged (rate limited) and skipped for that frame.
    Losing one stage degrades the picture; letting the exception escape would
    take the light behind the TV out entirely, which is worse.
    """

    def __init__(self, stages: Iterable[Stage], state: PipelineState | None = None):
        self._stages: list[Stage] = list(stages)
        self.state = state or PipelineState()
        self.timings = StageTimings()
        self.collect_debug = False
        self._lock = threading.RLock()
        self._error_counts: dict[str, int] = {}
        self.frames_processed = 0

    # -- stage access ------------------------------------------------------

    @property
    def stages(self) -> list[Stage]:
        with self._lock:
            return list(self._stages)

    @property
    def stage_names(self) -> list[str]:
        return [s.name for s in self.stages]

    def get(self, name: str) -> Stage | None:
        for stage in self.stages:
            if stage.name == name:
                return stage
        return None

    def __iter__(self) -> Iterator[Stage]:
        return iter(self.stages)

    def __len__(self) -> int:
        return len(self._stages)

    def set_enabled(self, name: str, enabled: bool) -> bool:
        stage = self.get(name)
        if stage is None:
            return False
        stage.enabled = enabled
        return True

    def toggle(self, name: str) -> bool | None:
        stage = self.get(name)
        return None if stage is None else stage.toggle()

    def replace_stages(self, stages: Iterable[Stage]) -> None:
        with self._lock:
            self._stages = list(stages)

    def reset(self) -> None:
        """Forget all temporal state; used after a recalibration request."""
        for stage in self.stages:
            try:
                stage.reset()
            except Exception:
                log.exception("Stage %s failed to reset", stage.name)
        self.timings.reset()

    # -- execution ---------------------------------------------------------

    def process(self, frame: Frame) -> FrameContext:
        ctx = FrameContext(
            source=frame.image,
            image=frame.image,
            index=frame.index,
            captured_at=frame.captured_at,
            collect_debug=self.collect_debug,
        )
        self.state.frame_size = (frame.width, frame.height)

        if self.collect_debug:
            ctx.add_debug("source", frame.image)

        for stage in self.stages:
            if not stage.enabled:
                ctx.skipped[stage.name] = "disabled"
                continue
            try:
                with self.timings.measure(stage.name):
                    stage.process(ctx)
            except Exception as exc:
                self._report(stage, exc)
                ctx.skipped[stage.name] = f"error: {type(exc).__name__}"

        if self.collect_debug:
            ctx.add_debug("output", ctx.image)

        ctx.meta["_timings_ms"] = self.timings.as_dict()
        ctx.meta["_latency_ms"] = round(ctx.latency_ms, 2)
        self.frames_processed += 1
        return ctx

    def _report(self, stage: Stage, exc: Exception) -> None:
        count = self._error_counts.get(stage.name, 0) + 1
        self._error_counts[stage.name] = count
        # Log the first few in full, then only powers of ten: a stage that
        # fails on every frame must not drown the journal.
        if count <= 3 or count % 100 == 0:
            log.exception("Stage %s failed (%d times): %s", stage.name, count, exc)

    # -- introspection -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "frames": self.frames_processed,
            "timings_ms": self.timings.as_dict(),
            "total_ms": round(self.timings.total_ms, 2),
            "stages": [
                {"name": s.name, "enabled": s.enabled, **s.status()} for s in self.stages
            ],
            "errors": dict(self._error_counts),
            "state": self.state.snapshot(),
        }

    def debug_views(self, ctx: FrameContext) -> dict[str, np.ndarray]:
        views: dict[str, np.ndarray] = {}
        if "source" in ctx.debug_images:
            views["source"] = ctx.debug_images["source"]
        for stage in self.stages:
            try:
                view = stage.debug_view(ctx)
            except Exception:
                view = None
            if view is not None:
                views[stage.name] = view
        views["output"] = ctx.image
        return views
