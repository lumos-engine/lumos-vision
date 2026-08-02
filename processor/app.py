"""The application: source -> pipeline -> sinks, plus the control surface.

Everything that mutates running state (config edits from the wizard, manual
corners, forced recalibration) is funnelled through a command queue and
executed between frames on the pipeline thread.  There is exactly one thread
touching the stages, which removes a whole category of bug and costs nothing.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable

import numpy as np

from processor.camera.base import Frame, FrameSource
from processor.camera.factory import create_source
from processor.config.loader import apply_updates, config_to_dict, save_config
from processor.config.schema import Config
from processor.output.base import SinkGroup
from processor.output.broker import BrokerHub
from processor.output.factory import create_sinks
from processor.pipeline.context import FrameContext, PipelineState
from processor.pipeline.pipeline import Pipeline
from processor.pipeline.registry import apply_config as apply_pipeline_config
from processor.pipeline.registry import build_pipeline
from processor.stages.boundary import BoundaryStage
from processor.utils.logging import get_logger
from processor.utils.timing import FpsMeter

log = get_logger(__name__)


class Processor:
    def __init__(self, config: Config, config_path: str | Path | None = None):
        self.config = config
        self.config_path = Path(config_path) if config_path else None

        self.state = PipelineState()
        self.pipeline: Pipeline = build_pipeline(config, self.state)
        self.source: FrameSource | None = None
        self.sinks: SinkGroup | None = None
        self.brokers = BrokerHub()

        self._commands: Queue[tuple[Callable[[], Any], Future]] = Queue()
        self._stop = threading.Event()
        self._running = False
        #: True only while :meth:`run` is actually draining the command queue.
        #: Distinct from ``_running``, because a started-but-not-yet-looping
        #: processor has nobody to execute queued commands.
        self._loop_active = False
        self._thread: threading.Thread | None = None

        self.input_fps = FpsMeter()
        self.output_fps = FpsMeter()
        self._latency_ms = 0.0
        self._frames_in = 0
        self._frames_out = 0
        self._frames_dropped = 0
        self._started_at = 0.0
        self._last_ctx: FrameContext | None = None
        self._last_source: np.ndarray | None = None
        self._last_stats_log = 0.0
        #: Set by the debug viewer so intermediate images get collected.
        self.want_debug_views = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> "Processor":
        self.source = create_source(self.config.camera).start()
        self.sinks = SinkGroup(create_sinks(self.config.output))
        self.sinks.open(self.config.output.width, self.config.output.height)
        self._started_at = time.monotonic()
        # Start the clock now so the first stats line reports a real interval
        # instead of the cold-start numbers.
        self._last_stats_log = self._started_at
        self._running = True
        log.info(
            "Pipeline: %s -> %dx%d @ %.0f fps",
            " -> ".join(self.pipeline.stage_names) or "(empty)",
            self.config.output.width,
            self.config.output.height,
            self.config.output.fps,
        )
        return self

    def run_in_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="pipeline", daemon=True)
        self._thread = thread
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)
        self.shutdown()

    def shutdown(self) -> None:
        self._running = False
        if self.source is not None:
            self.source.stop()
            self.source = None
        if self.sinks is not None:
            self.sinks.close()
            self.sinks = None

    @property
    def running(self) -> bool:
        return self._running and not self._stop.is_set()

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        if self.source is None or self.sinks is None:
            self.start()
        assert self.source is not None and self.sinks is not None

        min_interval = 1.0 / self.config.output.fps if self.config.output.fps > 0 else 0.0
        last_processed = 0.0
        self._loop_active = True

        try:
            while not self._stop.is_set():
                self._drain_commands()

                frame = self.source.read(timeout=1.0)
                if frame is None:
                    self._on_no_frame()
                    continue

                self._frames_in += 1
                self.input_fps.tick()
                self._last_source = frame.image

                now = time.monotonic()
                if min_interval and (now - last_processed) < min_interval * 0.92:
                    # The camera is faster than our target rate.  Dropping here
                    # rather than processing and throwing away later is where
                    # most of the CPU headroom on an old i5 comes from.
                    self._frames_dropped += 1
                    continue
                last_processed = now

                self.process_frame(frame)
                self._maybe_log_stats(now)
        finally:
            self._loop_active = False
            # Anything still queued will never run; fail it rather than
            # leaving a web request hanging until its timeout.
            self._abandon_commands()
            self.shutdown()

    def process_frame(self, frame: Frame) -> FrameContext:
        assert self.sinks is not None
        self._last_source = frame.image
        self.pipeline.collect_debug = (
            self.config.pipeline.collect_debug
            or self.want_debug_views
            or self.brokers.any_subscribers()
        )

        ctx = self.pipeline.process(frame)
        self._last_ctx = ctx
        self._latency_ms = ctx.latency_ms

        self.sinks.write(ctx.image)
        self._frames_out += 1
        self.output_fps.tick()

        self._publish_previews(ctx)
        return ctx

    def _publish_previews(self, ctx: FrameContext) -> None:
        wanted = self.brokers.subscribed_names()
        if not wanted:
            return
        views = self.pipeline.debug_views(ctx)
        views.setdefault("source", ctx.source)
        for name in wanted:
            image = views.get(name)
            if image is not None:
                self.brokers.publish(name, image)

    def _on_no_frame(self) -> None:
        source = self.source
        if source is not None and not source.is_connected:
            log.debug("Waiting for the camera to reconnect...")

    def _maybe_log_stats(self, now: float) -> None:
        interval = self.config.logging.stats_interval
        if interval <= 0:
            return
        if now - self._last_stats_log < interval:
            return
        self._last_stats_log = now
        timings = self.pipeline.timings.as_dict()
        slowest = max(timings.items(), key=lambda kv: kv[1], default=("-", 0.0))
        log.info(
            "in %.1f fps | out %.1f fps | latency %.0f ms | pipeline %.1f ms "
            "(slowest %s %.1f ms) | dropped %d",
            self.input_fps.fps,
            self.output_fps.fps,
            self._latency_ms,
            self.pipeline.timings.total_ms,
            slowest[0],
            slowest[1],
            self._frames_dropped,
        )

    # ------------------------------------------------------------------
    # control surface (thread safe)
    # ------------------------------------------------------------------

    def submit(self, fn: Callable[[], Any]) -> Future:
        """Run ``fn`` on the pipeline thread and return a Future for its result."""
        future: Future = Future()
        if not self._loop_active or self._stop.is_set():
            # Nobody is draining the queue (tests, or during shutdown), so run
            # it here rather than blocking the caller until its timeout.
            try:
                future.set_result(fn())
            except Exception as exc:
                future.set_exception(exc)
            return future
        self._commands.put((fn, future))
        return future

    def call(self, fn: Callable[[], Any], timeout: float = 5.0) -> Any:
        return self.submit(fn).result(timeout=timeout)

    def _drain_commands(self) -> None:
        while True:
            try:
                fn, future = self._commands.get_nowait()
            except Empty:
                return
            try:
                future.set_result(fn())
            except Exception as exc:
                log.exception("Command failed")
                future.set_exception(exc)

    def _abandon_commands(self) -> None:
        while True:
            try:
                _, future = self._commands.get_nowait()
            except Empty:
                return
            future.set_exception(RuntimeError("the processor stopped before this ran"))

    # -- operations --------------------------------------------------------

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Apply dotted-path updates to the live pipeline (validated first)."""

        def apply() -> dict[str, Any]:
            new_config = apply_updates(self.config, updates)
            self.config = new_config
            apply_pipeline_config(self.pipeline, new_config)
            log.info("Config updated: %s", ", ".join(sorted(updates)))
            return config_to_dict(new_config)

        return self.call(apply)

    def save(self, path: str | Path | None = None) -> str:
        target = Path(path) if path else self.config_path or Path("config.yaml")
        saved = save_config(self.config, target)
        self.config_path = saved
        log.info("Configuration saved to %s", saved)
        return str(saved)

    def force_recalibration(self) -> bool:
        def run() -> bool:
            stage = self.pipeline.get("boundary")
            if isinstance(stage, BoundaryStage):
                stage.force_recalibration()
                return True
            self.state.request_recalibration("manual")
            return False

        return self.call(run)

    def set_manual_corners(self, corners: list[list[float]] | None) -> dict[str, Any]:
        """Install wizard-picked corners (normalised) and switch to manual use."""

        def run() -> dict[str, Any]:
            mode = "manual" if corners else "auto"
            self.config = apply_updates(
                self.config, {"boundary.corners": corners, "boundary.mode": mode}
            )
            apply_pipeline_config(self.pipeline, self.config)
            stage = self.pipeline.get("boundary")
            if isinstance(stage, BoundaryStage):
                stage.set_manual_corners(corners)
            return {"corners": corners, "mode": mode}

        return self.call(run)

    def auto_detect(self) -> dict[str, Any]:
        """Run one detection pass on the newest frame, for the wizard."""

        def run() -> dict[str, Any]:
            stage = self.pipeline.get("boundary")
            frame = self._last_source
            if frame is None:
                return {"ok": False, "error": "no frame available yet"}
            if not isinstance(stage, BoundaryStage):
                return {"ok": False, "error": "the boundary stage is not in the pipeline"}

            quad, confidence = stage.detect_now(frame)
            if quad is None:
                return {"ok": False, "error": "no TV-like quadrilateral found"}

            height, width = frame.shape[:2]
            normalised = [
                [float(x) / width, float(y) / height] for x, y in quad.reshape(4, 2)
            ]
            return {
                "ok": True,
                "corners": normalised,
                "corners_px": quad.tolist(),
                "confidence": round(confidence, 3),
                "frame_size": [width, height],
            }

        return self.call(run)

    def snapshot(self, view: str = "output") -> np.ndarray | None:
        ctx = self._last_ctx
        if ctx is None:
            return None
        if view == "source":
            return ctx.source
        if view == "output":
            return ctx.image
        return self.pipeline.debug_views(ctx).get(view)

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------

    @property
    def last_context(self) -> FrameContext | None:
        return self._last_ctx

    def status(self) -> dict[str, Any]:
        uptime = time.monotonic() - self._started_at if self._started_at else 0.0
        ctx = self._last_ctx
        return {
            "running": self.running,
            "uptime_s": round(uptime, 1),
            "input_fps": round(self.input_fps.fps, 2),
            "output_fps": round(self.output_fps.fps, 2),
            "latency_ms": round(self._latency_ms, 1),
            "frames_in": self._frames_in,
            "frames_out": self._frames_out,
            "frames_dropped": self._frames_dropped,
            "source": self.source.stats if self.source else {},
            "sinks": self.sinks.stats if self.sinks else {},
            "pipeline": self.pipeline.status(),
            "output_size": [self.config.output.width, self.config.output.height],
            "meta": {} if ctx is None else _jsonable(ctx.meta),
            "views": self.available_views(),
        }

    def available_views(self) -> list[str]:
        views = ["source"]
        views += [s.name for s in self.pipeline.stages]
        views.append("output")
        seen: set[str] = set()
        return [v for v in views if not (v in seen or seen.add(v))]


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def create_processor(config: Config, config_path: str | Path | None = None) -> Processor:
    return Processor(config, config_path)


__all__ = ["Processor", "create_processor"]
