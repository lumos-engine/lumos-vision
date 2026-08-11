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
from datetime import datetime
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
from processor.utils.color_calibrate import ColorCalibrationSession, iso_now
from processor.utils.hyperhdr_leds import set_led_device
from processor.utils.logging import get_logger
from processor.utils.scrcpy import (
    ScrcpyManager,
    adb_device_ready,
    clamp_pan,
    clamp_view_zoom,
    clamp_zoom,
    step_pan,
    step_zoom,
)
from processor.utils.timing import FpsMeter
from processor.utils.tv_presence import PresenceMonitor, ping_host

log = get_logger(__name__)

#: Dotted paths that require tearing down and recreating the capture source.
_SOURCE_RECREATE_KEYS = frozenset(
    {
        "camera.source",
        "camera.device",
        "camera.rtsp_url",
        "camera.path",
        "camera.transport",
        "camera.capture_width",
        "camera.capture_height",
        "camera.capture_fps",
        "camera.ffmpeg_options",
        "camera.loop",
        "camera.replay_fps",
        "camera.process_width",
        "camera.read_timeout",
        "camera.reconnect_delay",
        "camera.max_reconnect_delay",
    }
)

#: Scrcpy CLI options that need a child restart (absolute zoom is start-only).
_SCRCPY_RESTART_KEYS = frozenset(
    {
        "scrcpy.enabled",
        "scrcpy.binary",
        "scrcpy.serial",
        "scrcpy.camera_id",
        "scrcpy.camera_size",
        "scrcpy.camera_fps",
        "scrcpy.camera_zoom",
        "scrcpy.zoom_min",
        "scrcpy.zoom_max",
        "scrcpy.view_zoom",
        "scrcpy.pan_x",
        "scrcpy.pan_y",
        "scrcpy.v4l2_sink",
        "scrcpy.no_playback",
        "scrcpy.no_audio",
        "scrcpy.extra_args",
        "scrcpy.startup_timeout_sec",
    }
)


def _updates_require_source_recreate(updates: dict[str, Any]) -> bool:
    return any(key in _SOURCE_RECREATE_KEYS for key in updates)


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

        self._idle = False
        self._leds_off = False
        self._last_power_check = 0.0
        self._black_frame: np.ndarray | None = None
        self._logged_reconnect_ping = False
        self._presence = PresenceMonitor(
            offline_checks=config.power.failed_pings,
            online_checks=config.power.success_pings,
        )
        self._scrcpy = ScrcpyManager()
        self._scrcpy_next_retry = 0.0
        self._color_cal = ColorCalibrationSession()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> "Processor":
        self.sinks = SinkGroup(create_sinks(self.config.output))
        self.sinks.open(self.config.output.width, self.config.output.height)
        self._started_at = time.monotonic()
        # Start the clock now so the first stats line reports a real interval
        # instead of the cold-start numbers.
        self._last_stats_log = self._started_at
        self._running = True
        self._sync_presence_config()

        if self._power_enabled() and not ping_host(
            self.config.power.tv_host, self.config.power.ping_timeout_sec
        ):
            self._presence.reset(online=False)
            self._enter_idle_unlocked(initial=True)
            log.info(
                "Pipeline idle at start (TV %s offline) -> %dx%d black @ %.1f fps",
                self.config.power.tv_host,
                self.config.output.width,
                self.config.output.height,
                self.config.power.idle_fps,
            )
        else:
            self._presence.reset(online=True)
            self._start_scrcpy_unlocked()
            self.source = create_source(self.config.camera).start()
            self._set_leds_unlocked(True)
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
        self._scrcpy.stop()
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
        if self.sinks is None:
            self.start()
        assert self.sinks is not None

        min_interval = (
            1.0 / self.config.output.fps if self.config.output.fps > 0 else 0.0
        )
        last_processed = 0.0
        self._loop_active = True

        try:
            while not self._stop.is_set():
                self._drain_commands()
                self._tick_power()
                self._tick_scrcpy_watchdog()

                if self._idle:
                    self._write_idle_frame()
                    idle_fps = max(0.2, float(self.config.power.idle_fps or 2.0))
                    self._stop.wait(timeout=1.0 / idle_fps)
                    self._maybe_log_stats(time.monotonic())
                    continue

                if self.source is None:
                    self._on_no_frame()
                    self._stop.wait(timeout=0.25)
                    continue

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
                self._tick_color_calibration()
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
            or self._color_cal.state in {"running", "ready"}
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

    # -- TV presence / idle ------------------------------------------------

    def _power_enabled(self) -> bool:
        return bool((self.config.power.tv_host or "").strip())

    def _sync_presence_config(self) -> None:
        power = self.config.power
        self._presence.offline_checks = power.failed_pings
        self._presence.online_checks = power.success_pings

    def _tick_power(self) -> None:
        if not self._power_enabled():
            if self._idle:
                self._leave_idle_unlocked()
            return

        now = time.monotonic()
        interval = max(1.0, float(self.config.power.check_interval_sec or 15.0))
        if self._last_power_check and (now - self._last_power_check) < interval:
            return
        self._last_power_check = now

        host = (self.config.power.tv_host or "").strip()
        need = max(1, int(self.config.power.failed_pings))
        was_idle = self._idle
        fails_before = self._presence.fail_streak
        reachable = ping_host(host, self.config.power.ping_timeout_sec)
        transition = self._presence.update(reachable)
        clock = datetime.now().strftime("%H:%M:%S")

        if not reachable:
            # Log every miss until idle shuts the camera down; stay quiet after.
            if not was_idle:
                log.info(
                    "TV ping failed at %s (%s) %d/%d",
                    clock,
                    host,
                    self._presence.fail_streak,
                    need,
                )
            # Next success (before or after idle) should log once as reconnect.
            self._logged_reconnect_ping = False
        elif not self._logged_reconnect_ping and (was_idle or fails_before > 0):
            log.info(
                "TV ping ok at %s (%s) — first reconnect after %d failed ping(s)",
                clock,
                host,
                fails_before,
            )
            self._logged_reconnect_ping = True

        if transition == "offline" and not self._idle:
            self._enter_idle_unlocked()
        elif transition == "online" and self._idle:
            self._leave_idle_unlocked()

    def _enter_idle_unlocked(self, *, initial: bool = False) -> None:
        if self.source is not None:
            try:
                self.source.stop()
            except Exception:
                log.exception("Failed to stop capture source for idle")
            self.source = None
        try:
            self._scrcpy.stop()
        except Exception:
            log.exception("Failed to stop scrcpy for idle")
        self._last_source = None
        self._last_ctx = None
        self._idle = True
        self._logged_reconnect_ping = False
        self._set_leds_unlocked(False)
        clock = datetime.now().strftime("%H:%M:%S")
        if not initial:
            log.info(
                "Entered idle at %s: TV %s offline — camera released, black frames, LEDs off",
                clock,
                self.config.power.tv_host,
            )
        else:
            log.info(
                "Idle at start %s: TV %s offline — camera not opened, black frames, LEDs off",
                clock,
                self.config.power.tv_host,
            )

    def _leave_idle_unlocked(self) -> None:
        self._set_leds_unlocked(True)
        self._idle = False
        try:
            self._start_scrcpy_unlocked()
            self._recreate_source_unlocked()
        except Exception:
            log.exception("Failed to reopen capture source after idle")
            self._idle = True
            self._scrcpy.stop()
            self._set_leds_unlocked(False)
            return
        clock = datetime.now().strftime("%H:%M:%S")
        log.info(
            "Left idle at %s: TV %s online — camera and LEDs resumed",
            clock,
            self.config.power.tv_host or "(power disabled)",
        )

    def _start_scrcpy_unlocked(self, *, restart: bool = False) -> dict[str, Any]:
        cfg = self.config.scrcpy
        if not cfg.enabled:
            self._scrcpy.stop()
            return {"ok": True, "running": False, "skipped": True}
        if cfg.bind_camera:
            sink = (cfg.v4l2_sink or "").strip()
            if sink:
                updates: dict[str, Any] = {}
                if self.config.camera.source not in {"v4l2", "usb"}:
                    updates["camera.source"] = "v4l2"
                if self.config.camera.device != sink:
                    updates["camera.device"] = sink
                if updates:
                    self.config = apply_updates(self.config, updates)
        if restart:
            result = self._scrcpy.restart(cfg)
        else:
            result = self._scrcpy.ensure_running(cfg)
        if not result.get("ok"):
            log.warning("scrcpy start incomplete: %s", result.get("error"))
        return result

    def _tick_scrcpy_watchdog(self) -> None:
        """Restart scrcpy after phone unplug/replug when auto_restart is on."""
        cfg = self.config.scrcpy
        if self._idle or not cfg.enabled or not cfg.auto_restart:
            return
        if self._scrcpy.running:
            return
        now = time.monotonic()
        if now < self._scrcpy_next_retry:
            return
        interval = max(2.0, float(cfg.restart_interval_sec or 5.0))
        self._scrcpy_next_retry = now + interval
        if not adb_device_ready(cfg.serial):
            return
        log.info("scrcpy not running — restarting (phone reconnected?)")
        result = self._start_scrcpy_unlocked()
        if not result.get("ok") or not result.get("running"):
            return
        try:
            self._recreate_source_unlocked()
        except Exception:
            log.exception("Failed to recreate capture source after scrcpy restart")

    def _set_leds_unlocked(self, enabled: bool) -> None:
        url = (self.config.power.hyperhdr_url or "").strip()
        if not url:
            self._leds_off = not enabled
            return
        result = set_led_device(url, enabled)
        # Track requested state even if HyperHDR is briefly unreachable.
        self._leds_off = not enabled
        if not result.get("ok") and not result.get("skipped"):
            log.warning("LEDDEVICE toggle incomplete: %s", result.get("error"))

    def _write_idle_frame(self) -> None:
        sinks = self.sinks
        if sinks is None:
            return
        width = int(self.config.output.width)
        height = int(self.config.output.height)
        if (
            self._black_frame is None
            or self._black_frame.shape[1] != width
            or self._black_frame.shape[0] != height
        ):
            self._black_frame = np.zeros((height, width, 3), dtype=np.uint8)
        sinks.write(self._black_frame)
        self._frames_out += 1
        self.output_fps.tick()

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
            if any(key == "power" or key.startswith("power.") for key in updates):
                self._sync_presence_config()
                self._last_power_check = 0.0
                if not self._power_enabled() and self._idle:
                    self._leave_idle_unlocked()
            scrcpy_touched = any(
                key in _SCRCPY_RESTART_KEYS or key.startswith("scrcpy.")
                for key in updates
            )
            if scrcpy_touched:
                if self._idle:
                    log.info("Skipping scrcpy restart while idle")
                else:
                    self._start_scrcpy_unlocked(restart=True)
            if _updates_require_source_recreate(updates) or scrcpy_touched:
                if self._idle:
                    log.info("Skipping source recreate while idle")
                else:
                    self._recreate_source_unlocked()
            elif any(
                key == "camera.controls" or key.startswith("camera.controls.")
                for key in updates
            ):
                # Hardware UVC knobs live on the camera, not in the colour stage.
                self._apply_camera_controls(dict(new_config.camera.controls))
            log.info("Config updated: %s", ", ".join(sorted(updates)))
            return config_to_dict(new_config)

        return self.call(apply)

    def recreate_source(self) -> dict[str, Any]:
        """Stop the current capture source and open a new one from config."""
        return self.call(self._recreate_source_unlocked)

    def _recreate_source_unlocked(self) -> dict[str, Any]:
        if self._idle:
            return {"ok": False, "error": "capture source is idle (TV offline)"}
        old = self.source
        if old is not None:
            try:
                old.stop()
            except Exception:
                log.exception("Failed to stop previous capture source")
        self.source = None
        self._last_source = None
        self._last_ctx = None
        self.source = create_source(self.config.camera).start()
        log.info(
            "Capture source recreated: %s (%s)",
            self.config.camera.source,
            self.source.name,
        )
        return {
            "ok": True,
            "source": self.config.camera.source,
            "stats": dict(self.source.stats),
        }

    def apply_camera_source(
        self, fields: dict[str, Any], *, save: bool = False
    ) -> dict[str, Any]:
        """Update camera source fields, recreate capture, optionally save YAML."""
        allowed = {
            "source",
            "device",
            "rtsp_url",
            "path",
            "transport",
            "capture_width",
            "capture_height",
            "capture_fps",
            "ffmpeg_options",
            "loop",
            "replay_fps",
            "process_width",
        }
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "source" and isinstance(value, str):
                value = value.strip().lower()
                if value == "usb":
                    value = "v4l2"
            updates[f"camera.{key}"] = value
        if not updates:
            raise ValueError("no recognised camera source fields given")

        def apply() -> dict[str, Any]:
            self.config = apply_updates(self.config, updates)
            apply_pipeline_config(self.pipeline, self.config)
            recreated = self._recreate_source_unlocked()
            saved_path = None
            if save:
                saved_path = self.save()
            log.info("Camera source applied: %s", ", ".join(sorted(updates)))
            return {
                "ok": True,
                "config": config_to_dict(self.config),
                "recreated": recreated,
                "saved": saved_path,
            }

        return self.call(apply)

    def _apply_camera_controls(self, values: dict[str, int]) -> dict[str, Any]:
        source = self.source
        apply = getattr(source, "apply_controls", None)
        if not callable(apply):
            return {"ok": False, "error": "current source has no hardware controls"}
        return apply(values)

    def list_camera_controls(self) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            source = self.source
            lister = getattr(source, "list_controls", None)
            if not callable(lister):
                return {
                    "ok": False,
                    "supported": False,
                    "device": self.config.camera.device,
                    "controls": [],
                    "error": "hardware controls require --source v4l2",
                }
            controls = lister()
            return {
                "ok": True,
                "supported": True,
                "device": self.config.camera.device,
                "controls": controls,
                "saved": dict(self.config.camera.controls),
            }

        return self.call(run)

    def set_camera_controls(self, values: dict[str, Any]) -> dict[str, Any]:
        """Set UVC controls live and remember them in config.camera.controls."""

        def run() -> dict[str, Any]:
            cleaned: dict[str, int] = {}
            for name, value in values.items():
                if not isinstance(name, str) or not name:
                    continue
                try:
                    cleaned[name] = int(value)
                except (TypeError, ValueError):
                    return {
                        "ok": False,
                        "error": f"control {name!r} must be an integer",
                    }

            result = self._apply_camera_controls(cleaned)
            if result.get("applied"):
                merged = dict(self.config.camera.controls)
                merged.update(result["applied"])
                self.config = apply_updates(self.config, {"camera.controls": merged})
            return {
                "ok": bool(result.get("ok")),
                "applied": result.get("applied", {}),
                "errors": result.get("errors", {}),
                "error": result.get("error"),
                "controls": dict(self.config.camera.controls),
            }

        return self.call(run)

    def save(self, path: str | Path | None = None) -> str:
        target = Path(path) if path else self.config_path or Path("config.yaml")
        saved = save_config(self.config, target)
        self.config_path = saved
        log.info("Configuration saved to %s", saved)
        return str(saved)

    def scrcpy_status(self) -> dict[str, Any]:
        return self._scrcpy.status(self.config.scrcpy).as_dict()

    def apply_scrcpy(
        self,
        fields: dict[str, Any] | None = None,
        *,
        action: str = "apply",
        save: bool = False,
    ) -> dict[str, Any]:
        """Start/stop/restart scrcpy or apply zoom/size fields from the wizard."""

        def run() -> dict[str, Any]:
            action_name = (action or "apply").strip().lower()
            updates: dict[str, Any] = {}
            for key, value in (fields or {}).items():
                if key in {
                    "enabled",
                    "binary",
                    "serial",
                    "camera_id",
                    "camera_size",
                    "camera_fps",
                    "camera_zoom",
                    "zoom_min",
                    "zoom_max",
                    "view_zoom",
                    "pan_x",
                    "pan_y",
                    "v4l2_sink",
                    "no_playback",
                    "no_audio",
                    "bind_camera",
                    "startup_timeout_sec",
                    "extra_args",
                }:
                    updates[f"scrcpy.{key}"] = value

            if "scrcpy.pan_x" in updates:
                updates["scrcpy.pan_x"] = clamp_pan(float(updates["scrcpy.pan_x"]))
            if "scrcpy.pan_y" in updates:
                updates["scrcpy.pan_y"] = clamp_pan(float(updates["scrcpy.pan_y"]))
            if "scrcpy.view_zoom" in updates:
                updates["scrcpy.view_zoom"] = clamp_view_zoom(
                    float(updates["scrcpy.view_zoom"])
                )

            if action_name == "zoom_in":
                updates["scrcpy.camera_zoom"] = step_zoom(
                    self.config.scrcpy.camera_zoom, inward=True, cfg=self.config.scrcpy
                )
                updates.setdefault("scrcpy.enabled", True)
            elif action_name == "zoom_out":
                updates["scrcpy.camera_zoom"] = step_zoom(
                    self.config.scrcpy.camera_zoom, inward=False, cfg=self.config.scrcpy
                )
                updates.setdefault("scrcpy.enabled", True)
            elif action_name == "set_zoom":
                if "scrcpy.camera_zoom" not in updates:
                    raise ValueError("camera_zoom required for set_zoom")
                updates["scrcpy.camera_zoom"] = clamp_zoom(
                    float(updates["scrcpy.camera_zoom"]), self.config.scrcpy
                )
                updates.setdefault("scrcpy.enabled", True)
            elif action_name.startswith("pan_"):
                direction = action_name[len("pan_") :]
                pan_x, pan_y, view_zoom = step_pan(
                    self.config.scrcpy, direction=direction
                )
                updates["scrcpy.pan_x"] = pan_x
                updates["scrcpy.pan_y"] = pan_y
                updates["scrcpy.view_zoom"] = view_zoom
                updates.setdefault("scrcpy.enabled", True)
            elif action_name == "start":
                updates["scrcpy.enabled"] = True
            elif action_name == "stop":
                updates["scrcpy.enabled"] = False
            elif action_name in {"apply", "restart"}:
                pass
            else:
                raise ValueError(f"unknown scrcpy action: {action_name}")

            if updates:
                self.config = apply_updates(self.config, updates)

            scrcpy_result: dict[str, Any]
            if self._idle and action_name != "stop":
                scrcpy_result = {
                    "ok": True,
                    "skipped": True,
                    "error": "TV idle — scrcpy will start on resume",
                }
            elif action_name == "stop" or not self.config.scrcpy.enabled:
                scrcpy_result = self._scrcpy.stop()
            elif action_name == "restart" or updates:
                scrcpy_result = self._scrcpy.restart(self.config.scrcpy)
            else:
                scrcpy_result = self._start_scrcpy_unlocked()

            source_result: dict[str, Any] | None = None
            if (
                not self._idle
                and self.config.scrcpy.enabled
                and self.config.scrcpy.bind_camera
                and action_name != "stop"
            ):
                # Reopen capture after the loopback producer respawns.
                try:
                    source_result = self._recreate_source_unlocked()
                except Exception as exc:
                    source_result = {"ok": False, "error": str(exc)}

            saved_path = None
            if save:
                saved_path = str(self.save())

            return {
                "ok": bool(scrcpy_result.get("ok", True)),
                "action": action_name,
                "scrcpy": self._scrcpy.status(self.config.scrcpy).as_dict(),
                "result": scrcpy_result,
                "source": source_result,
                "config": config_to_dict(self.config),
                "saved": saved_path,
                "error": scrcpy_result.get("error"),
            }

        return self.call(run)

    def _tick_color_calibration(self) -> None:
        if self._color_cal.state not in {"running", "ready"}:
            return
        ctx = self._last_ctx
        image = None if ctx is None else ctx.debug_images.get("perspective")
        self._color_cal.tick(image)

    def color_calibration_status(self) -> dict[str, Any]:
        return self._color_cal.status()

    def start_color_calibration(
        self,
        *,
        settle_sec: float | None = None,
        mode: str | None = None,
        advance_after_capture: bool | None = None,
    ) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            if self._idle:
                return {"ok": False, "error": "cannot calibrate while TV idle"}
            if self.state.corners is None:
                return {
                    "ok": False,
                    "error": "mark TV corners first so the sample ROI is on-panel",
                }
            # Measuring must see the uncorrected panel (no WB / matrix / gamma).
            needs_bypass = (
                self.config.color.white_balance != "off"
                or self.config.color.matrix_enabled
                or abs(self.config.color.gamma - 1.0) > 1e-3
            )
            if needs_bypass:
                self.config = apply_updates(
                    self.config,
                    {
                        "color.white_balance": "off",
                        "color.matrix_enabled": False,
                        "color.gamma": 1.0,
                        "color.exposure.enabled": False,
                    },
                )
                apply_pipeline_config(self.pipeline, self.config)
            try:
                status = self._color_cal.start(
                    settle_sec=settle_sec,
                    mode=mode,
                    advance_after_capture=advance_after_capture,
                )
            except (TypeError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
            log.info(
                "Colour calibration started (%s, %d patches, settle %.1fs)",
                status["mode"],
                status["total"],
                status["settle_sec"],
            )
            return {"ok": True, **status}

        return self.call(run)

    def abort_color_calibration(self) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            status = self._color_cal.abort()
            log.info("Colour calibration aborted")
            return {"ok": True, **status}

        return self.call(run)

    def capture_color_calibration(self) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            try:
                status = self._color_cal.request_capture()
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, **status}

        return self.call(run)

    def navigate_color_calibration(
        self,
        *,
        action: str | None = None,
        index: int | None = None,
        patch: str | None = None,
    ) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            try:
                if action == "next":
                    status = self._color_cal.next_patch()
                elif action == "prev":
                    status = self._color_cal.prev_patch()
                else:
                    status = self._color_cal.goto(index=index, patch=patch)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, **status}

        return self.call(run)

    def set_color_calibration_options(
        self, *, advance_after_capture: bool | None = None
    ) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            if advance_after_capture is not None:
                self._color_cal.set_advance_after_capture(advance_after_capture)
            return {"ok": True, **self._color_cal.status()}

        return self.call(run)

    def solve_color_calibration(self) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            try:
                status = self._color_cal.solve_now()
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if status.get("state") == "error":
                return {"ok": False, **status}
            return {"ok": True, **status}

        return self.call(run)

    def apply_color_calibration(self, *, save: bool = False) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            solution = self._color_cal.solution
            if solution is None or self._color_cal.state not in {"ready"}:
                return {
                    "ok": False,
                    "error": "no calibration solution yet — run Colour calibrate first",
                }
            r, g, b = solution.gains_rgb()
            matrix = solution.matrix_flat()
            updates = {
                "color.white_balance": "manual",
                "color.gains.r": r,
                "color.gains.g": g,
                "color.gains.b": b,
                "color.matrix_enabled": True,
                "color.matrix": matrix,
                "color.gamma": solution.gamma,
                "color.exposure.enabled": False,
                "color.calibration.calibrated_at": iso_now(),
                "color.calibration.patch_means_bgr": solution.patch_means_bgr,
                "color.calibration.matrix": matrix,
                "color.calibration.notes": list(solution.notes),
            }
            self.config = apply_updates(self.config, updates)
            apply_pipeline_config(self.pipeline, self.config)
            saved_path = str(self.save()) if save else None
            log.info(
                "Colour calibration applied (3×3 matrix, gains R%.3f G%.3f B%.3f gamma %.3f)%s",
                r,
                g,
                b,
                solution.gamma,
                f" → {saved_path}" if saved_path else "",
            )
            return {
                "ok": True,
                "solution": solution.as_dict(),
                "config": config_to_dict(self.config),
                "saved": saved_path,
                "calibration": self._color_cal.status(),
            }

        return self.call(run)

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
                return {
                    "ok": False,
                    "error": "the boundary stage is not in the pipeline",
                }

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
        power = self.config.power
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
            "power": {
                "enabled": self._power_enabled(),
                "tv_host": (power.tv_host or "").strip(),
                "hyperhdr_url": (power.hyperhdr_url or "").strip(),
                "online": self._presence.online if self._power_enabled() else True,
                "idle": self._idle,
                "leds_off": self._leds_off,
            },
            "scrcpy": self._scrcpy.status(self.config.scrcpy).as_dict(),
            "color_calibration": self._color_cal.status(),
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


def create_processor(
    config: Config, config_path: str | Path | None = None
) -> Processor:
    return Processor(config, config_path)


__all__ = ["Processor", "create_processor"]
