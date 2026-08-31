"""The application: source -> pipeline -> sinks, plus the control surface.

Everything that mutates running state (config edits from the wizard, manual
corners, forced recalibration) is funnelled through a command queue and
executed between frames on the pipeline thread.  There is exactly one thread
touching the stages, which removes a whole category of bug and costs nothing.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable

import numpy as np

from processor.camera.base import Frame, FrameSource
from processor.camera.lumos import LumosPipeSource
from processor.camera.factory import create_source
from processor.config.loader import apply_updates, config_to_dict, save_config
from processor.config.schema import Config, LumosCamConfig, normalize_led_path
from processor.output.base import SinkGroup
from processor.output.broker import BrokerHub
from processor.output.factory import create_sinks
from processor.output.ddp import DdpSink
from processor.output.v4l2 import V4L2Sink
from processor.pipeline.context import FrameContext, PipelineState
from processor.pipeline.pipeline import Pipeline
from processor.pipeline.registry import apply_config as apply_pipeline_config
from processor.pipeline.registry import build_pipeline
from processor.stages.boundary import BoundaryStage
from processor.utils.color_calibrate import ColorCalibrationSession, iso_now
from processor.utils.led_calibrate import LedCalibrationSession
from processor.led.rgbw import IDENTITY_RGB_FLAT, bytes_per_led
from processor.utils.color_profiles import (
    bind_config,
    camera_for_slot,
    camera_live_updates,
    empty_slot,
    lookup_slot,
    profile_status,
    profiles_touch_selection,
    resolve_selection,
    slot_from_solution,
    slot_key,
    store_slot_updates,
)
from processor.utils.lumos_os import (
    apply_led_brightness,
    clamp_led_brightness,
    sync_vision_output,
)
from processor.utils.hyperhdr_leds import refresh_video_grabber, set_led_device, set_video_grabber
from processor.utils.logging import get_logger
from processor.utils.loopback import (
    OUTPUT_LABEL,
    ensure_processor_loopbacks,
    needed_loopbacks,
    repair_loopback,
)
from processor.utils.lumos_cam import (
    LumosCamManager,
    frame_stream_stalled,
    step_lumos_pan,
    step_lumos_zoom,
)
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

#: Lumos Cam stream options that need ffmpeg/adb restart (locks/zoom are live).
_LUMOS_RESTART_KEYS = frozenset(
    {
        "lumos_cam.enabled",
        "lumos_cam.serial",
        "lumos_cam.adb",
        "lumos_cam.package",
        "lumos_cam.activity",
        "lumos_cam.camera_id",
        "lumos_cam.camera_size",
        "lumos_cam.camera_fps",
        "lumos_cam.codec",
        "lumos_cam.ffmpeg",
        "lumos_cam.control_device_port",
        "lumos_cam.video_device_port",
        "lumos_cam.control_host_port",
        "lumos_cam.video_host_port",
        "lumos_cam.startup_timeout_sec",
    }
)
_LUMOS_LIVE_KEYS = frozenset(
    {
        "lumos_cam.camera_zoom",
        "lumos_cam.zoom_min",
        "lumos_cam.zoom_max",
        "lumos_cam.pan_x",
        "lumos_cam.pan_y",
        "lumos_cam.af",
        "lumos_cam.ae",
        "lumos_cam.awb",
        "lumos_cam.iso",
        "lumos_cam.exposure_ns",
        "lumos_cam.focus_distance",
        "lumos_cam.awb_gains",
    }
)
_LUMOS_STREAM_ATTRS = tuple(
    key.split(".", 1)[1]
    for key in _LUMOS_RESTART_KEYS
    if key.split(".", 1)[1] not in {"enabled"}
)


def _lumos_stream_changed(old: LumosCamConfig, new: LumosCamConfig) -> bool:
    """True when ffmpeg/adb must restart (ignore live keys and unused sink)."""
    return any(getattr(old, name) != getattr(new, name) for name in _LUMOS_STREAM_ATTRS)


def _updates_require_source_recreate(updates: dict[str, Any]) -> bool:
    return any(key in _SOURCE_RECREATE_KEYS for key in updates)


def _updates_require_sink_recreate(updates: dict[str, Any]) -> bool:
    for key in updates:
        if key in {"output.led_path", "output.width", "output.height"}:
            return True
        if key.startswith("output.v4l2.") or key.startswith("output.ddp."):
            return True
        if key.startswith("output.mjpeg.") or key.startswith("output.file."):
            return True
    return False


class Processor:
    def __init__(self, config: Config, config_path: str | Path | None = None):
        self.config = bind_config(config)
        self.config_path = Path(config_path) if config_path else None

        self.state = PipelineState()
        self.pipeline: Pipeline = build_pipeline(self.config, self.state)
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
        #: None, ``"feed_stall"``, or ``"tv_idle"``. Only that event may turn LEDs back on.
        self._leds_hold_reason: str | None = None
        self._last_frame_at = 0.0
        self._last_power_check = 0.0
        self._black_frame: np.ndarray | None = None
        self._logged_reconnect_ping = False
        self._presence = PresenceMonitor(
            offline_checks=config.power.failed_pings,
            online_checks=config.power.success_pings,
        )
        self._scrcpy = ScrcpyManager()
        self._scrcpy_next_retry = 0.0
        self._lumos = LumosCamManager()
        self._lumos_next_retry = 0.0
        self._lumos_transform_at = 0.0
        self._last_no_frame_log = 0.0
        self._v4l2_next_repair = 0.0
        self._color_cal = ColorCalibrationSession()
        self._led_cal = LedCalibrationSession()
        self._led_test: str | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> "Processor":
        self._ensure_loopbacks_unlocked()
        # HyperHDR must drop /dev/video10 before S_FMT; otherwise the loopback
        # returns EINVAL and exclusive_caps never flips to capture.
        self._release_hyperhdr_grabber_unlocked()
        self.sinks = SinkGroup(create_sinks(self.config.output))
        self.sinks.open(self.config.output.width, self.config.output.height)
        self._recover_v4l2_unlocked()
        self._nudge_hyperhdr_grabber_unlocked()
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
            self._start_phone_capture_unlocked()
            self.source = self._make_source_unlocked().start()
            self._apply_lumos_os_brightness_unlocked()
            self._sync_lumos_vision_output_unlocked()
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
        self._lumos.stop()
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

        last_processed = 0.0
        self._loop_active = True

        try:
            while not self._stop.is_set():
                self._drain_commands()
                self._tick_power()
                self._tick_scrcpy_watchdog()
                self._tick_lumos_watchdog()
                self._tick_v4l2_watchdog()

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
                    self._maybe_log_stats(time.monotonic())
                    continue

                self._note_feed_alive_unlocked()
                self._frames_in += 1
                self.input_fps.tick()
                self._last_source = frame.image

                target_fps = float(self.config.output.fps or 0.0)
                min_interval = 1.0 / target_fps if target_fps > 0 else 0.0
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

        self.sinks.write(ctx.image, ctx)
        self._frames_out += 1
        self.output_fps.tick()
        if self._frames_out == 1:
            log.info(
                "First processed frame %dx%d (open http://localhost:%d/ if the wizard is blank)",
                ctx.image.shape[1],
                ctx.image.shape[0],
                int(self.config.web.port or 7660),
            )

        self._publish_previews(ctx)
        return ctx

    def _publish_previews(self, ctx: FrameContext) -> None:
        # Always keep the last source/output so /stream/* is not blank when
        # the wizard opens after the first frame (no subscribers yet).
        self.brokers.publish("source", ctx.source)
        self.brokers.publish("output", ctx.image)
        wanted = self.brokers.subscribed_names() - {"source", "output"}
        if not wanted:
            return
        views = self.pipeline.debug_views(ctx)
        for name in wanted:
            image = views.get(name)
            if image is not None:
                self.brokers.publish(name, image)

    def _on_no_frame(self) -> None:
        self._maybe_hold_leds_for_feed_stall_unlocked()
        source = self.source
        now = time.monotonic()
        if now - self._last_no_frame_log < 15.0:
            return
        if source is None:
            self._last_no_frame_log = now
            log.warning("No camera source — waiting for Lumos Cam / capture to come back")
            return
        stats = getattr(source, "stats", {}) or {}
        age = stats.get("last_frame_age")
        if source.is_connected and (age is None or float(age) < 3.0):
            return
        self._last_no_frame_log = now
        if age is not None:
            log.warning(
                "No camera frame for %.0fs (source %s)",
                float(age),
                getattr(source, "name", "?"),
            )
        elif not source.is_connected:
            log.warning(
                "Waiting for the camera to reconnect (source %s)",
                getattr(source, "name", "?"),
            )

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
        try:
            self._lumos.stop()
        except Exception:
            log.exception("Failed to stop Lumos Cam for idle")
        self._last_source = None
        self._last_ctx = None
        self._idle = True
        self._logged_reconnect_ping = False
        self._hold_leds_off_unlocked("tv_idle")
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
        self._idle = False
        try:
            self._start_phone_capture_unlocked()
            self._recreate_source_unlocked()
            self._release_leds_hold_unlocked("tv_idle")
            self._apply_lumos_os_brightness_unlocked()
            self._sync_lumos_vision_output_unlocked()
        except Exception:
            log.exception("Failed to reopen capture source after idle")
            self._idle = True
            self._scrcpy.stop()
            self._lumos.stop()
            self._hold_leds_off_unlocked("tv_idle")
            return
        clock = datetime.now().strftime("%H:%M:%S")
        log.info(
            "Left idle at %s: TV %s online — camera and LEDs resumed",
            clock,
            self.config.power.tv_host or "(power disabled)",
        )

    def _capture_source_kind(self) -> str:
        return (self.config.camera.source or "").strip().lower()

    def _lumos_is_primary(self) -> bool:
        return self._capture_source_kind() == "lumos"

    def _ensure_loopbacks_unlocked(self) -> None:
        try:
            ensure_processor_loopbacks(self.config)
        except Exception:
            log.exception("Failed to ensure v4l2loopback nodes")

    def _recreate_sinks_unlocked(self) -> None:
        old = self.sinks
        if old is not None:
            try:
                old.close()
            except Exception:
                log.exception("Failed to close previous output sinks")
        self._ensure_loopbacks_unlocked()
        self.sinks = SinkGroup(create_sinks(self.config.output))
        self.sinks.open(self.config.output.width, self.config.output.height)
        self._recover_v4l2_unlocked()
        self._nudge_hyperhdr_grabber_unlocked()
        if self._leds_hold_reason:
            self._apply_led_output_unlocked(False)
        self._push_led_flood_unlocked()
        log.info(
            "Output sinks recreated: %s (led_path=%s)",
            ", ".join(s.name for s in self.sinks.sinks) or "none",
            self.config.output.led_path,
        )

    def _v4l2_sink_unlocked(self) -> V4L2Sink | None:
        if self.sinks is None:
            return None
        for sink in self.sinks.sinks:
            if isinstance(sink, V4L2Sink):
                return sink
        return None

    def _v4l2_is_open_unlocked(self) -> bool:
        sink = self._v4l2_sink_unlocked()
        return bool(sink is not None and sink.stats.get("open"))

    def _release_hyperhdr_grabber_unlocked(self) -> None:
        """Ask HyperHDR to close /dev/video10 and wait for the fd to drop."""
        if sys.platform != "linux" or not self.config.output.v4l2.enabled:
            return
        result = set_video_grabber(
            self.config.power.hyperhdr_url, False, quiet=False
        )
        if result.get("skipped"):
            return
        if result.get("ok"):
            time.sleep(0.35)

    def _recover_v4l2_unlocked(self) -> None:
        """If the HyperHDR-facing sink failed to open, repair the loopback and retry."""
        if sys.platform != "linux" or not self.config.output.v4l2.enabled:
            return
        if self.sinks is None:
            return
        if self._v4l2_is_open_unlocked():
            return
        sink = self._v4l2_sink_unlocked()
        if sink is None:
            sink = V4L2Sink(self.config.output.v4l2)
            self.sinks.sinks.append(sink)
        log.warning(
            "V4L2 output is not open — releasing HyperHDR's grabber and repairing %s",
            sink.device,
        )
        self._release_hyperhdr_grabber_unlocked()
        try:
            repair_loopback(
                sink.device,
                label=OUTPUT_LABEL,
                keep=needed_loopbacks(self.config),
            )
            sink._next_repair = 0.0
            sink.close()
            sink.open(self.config.output.width, self.config.output.height)
        except Exception:
            log.exception("V4L2 repair of %s failed", sink.device)

    def _nudge_hyperhdr_grabber_unlocked(self) -> None:
        """Make HyperHDR rescan /dev/video* after we are producing frames."""
        if not self._v4l2_is_open_unlocked():
            return
        refresh_video_grabber(self.config.power.hyperhdr_url)

    def _tick_v4l2_watchdog(self) -> None:
        if sys.platform != "linux" or not self.config.output.v4l2.enabled:
            return
        if self._v4l2_is_open_unlocked():
            return
        now = time.monotonic()
        if now < self._v4l2_next_repair:
            return
        self._v4l2_next_repair = now + 10.0
        self._recover_v4l2_unlocked()
        self._nudge_hyperhdr_grabber_unlocked()

    def _release_loopback_reader_unlocked(self, sink: str, *, bound: bool, label: str) -> None:
        """Drop capture on a v4l2loopback node so the producer can reopen it."""
        sink = (sink or "").strip()
        if not sink or self.source is None:
            return
        device = (self.config.camera.device or "").strip()
        if not bound and device != sink:
            return
        log.info("Releasing capture on %s so %s can reopen it", sink, label)
        try:
            self.source.stop()
        except Exception:
            log.exception("Failed to stop capture source before %s start", label)
        self.source = None
        self._last_source = None
        self._last_ctx = None

    def _bind_camera_to_sink(self, sink: str) -> None:
        sink = (sink or "").strip()
        if not sink or self._lumos_is_primary():
            return
        updates: dict[str, Any] = {}
        if self._capture_source_kind() != "scrcpy":
            updates["camera.source"] = "scrcpy"
        if self.config.camera.device != sink:
            updates["camera.device"] = sink
        if updates:
            self.config = apply_updates(self.config, updates)

    def _start_phone_capture_unlocked(self, *, restart: bool = False) -> dict[str, Any]:
        kind = self._capture_source_kind()
        if kind == "lumos":
            self._scrcpy.stop()
            return self._start_lumos_unlocked(restart=restart)
        if kind == "scrcpy":
            self._lumos.release_app(self.config.lumos_cam)
            return self._start_scrcpy_unlocked(restart=restart)
        self._lumos.stop()
        self._scrcpy.stop()
        return {"ok": True, "running": False, "skipped": True}

    def _release_scrcpy_sink_reader_unlocked(self) -> None:
        cfg = self.config.scrcpy
        self._release_loopback_reader_unlocked(
            cfg.v4l2_sink, bound=self._capture_source_kind() == "scrcpy", label="scrcpy"
        )

    def _start_scrcpy_unlocked(self, *, restart: bool = False) -> dict[str, Any]:
        cfg = self.config.scrcpy
        if self._capture_source_kind() != "scrcpy" or not cfg.enabled:
            self._scrcpy.stop()
            skipped = "Lumos Cam is primary" if self._lumos_is_primary() else None
            return {"ok": True, "running": False, "skipped": True, "error": skipped}
        self._ensure_loopbacks_unlocked()
        self._bind_camera_to_sink(cfg.v4l2_sink)
        if restart or not self._scrcpy.running:
            self._release_scrcpy_sink_reader_unlocked()
        if restart:
            result = self._scrcpy.restart(cfg)
        else:
            result = self._scrcpy.ensure_running(cfg)
        if not result.get("ok"):
            log.warning("scrcpy start incomplete: %s", result.get("error"))
        return result

    def _start_lumos_unlocked(self, *, restart: bool = False) -> dict[str, Any]:
        cfg = self.config.lumos_cam
        if self._capture_source_kind() != "lumos" or not cfg.enabled:
            self._lumos.stop()
            return {"ok": True, "running": False, "skipped": True}
        if restart or not self._lumos.running:
            self._release_lumos_reader_unlocked()
        if restart:
            result = self._lumos.restart(cfg)
        else:
            result = self._lumos.ensure_running(cfg)
        if not result.get("ok"):
            log.warning("Lumos Cam start incomplete: %s", result.get("error"))
        return result

    def _release_lumos_reader_unlocked(self) -> None:
        """Stop the pipe reader so ffmpeg can be replaced."""
        if self.source is None:
            return
        if getattr(self.source, "name", "") != "lumos" and not self._lumos_is_primary():
            return
        log.info("Stopping Lumos Cam capture while ffmpeg restarts")
        try:
            self.source.stop()
        except Exception:
            log.exception("Failed to stop capture source before Lumos Cam start")
        self.source = None
        self._last_source = None
        self._last_ctx = None

    def _tick_scrcpy_watchdog(self) -> None:
        """Restart scrcpy after phone unplug/replug when auto_restart is on."""
        cfg = self.config.scrcpy
        if (
            self._idle
            or self._capture_source_kind() != "scrcpy"
            or not cfg.enabled
            or not cfg.auto_restart
        ):
            return
        if self._scrcpy.running:
            return
        now = time.monotonic()
        if now < self._scrcpy_next_retry:
            return
        sink = (cfg.v4l2_sink or "").strip()
        if sink and not os.path.exists(sink):
            self._ensure_loopbacks_unlocked()
        if sink and not os.path.exists(sink):
            self._scrcpy_next_retry = now + max(30.0, float(cfg.restart_interval_sec or 5.0) * 6)
            log.error(
                "scrcpy sink %s is missing — Screen Sight could not create the "
                "v4l2loopback node (devices=2 video_nr=10,11)",
                sink,
            )
            return
        interval = max(2.0, float(cfg.restart_interval_sec or 5.0))
        self._scrcpy_next_retry = now + interval
        if not adb_device_ready(cfg.serial, adb="adb"):
            return
        log.info("scrcpy not running — restarting (phone reconnected?)")
        result = self._start_scrcpy_unlocked(restart=True)
        if not result.get("ok") or not result.get("running"):
            error = result.get("error") or self._scrcpy.status(cfg).last_error
            if error:
                log.warning("scrcpy restart failed: %s", error)
            return
        if result.get("ready") is False:
            log.warning(
                "scrcpy is up but %s is not capturing yet — not opening the reader",
                (cfg.v4l2_sink or "/dev/video11"),
            )
            return
        try:
            self._recreate_source_unlocked()
        except Exception:
            log.exception("Failed to recreate capture source after scrcpy restart")

    def _lumos_stall_timeout_sec(self) -> float:
        configured = float(self.config.lumos_cam.stall_timeout_sec or 0.0)
        if configured > 0:
            return configured
        return max(8.0, float(self.config.camera.read_timeout or 8.0))

    def _restart_lumos_capture_unlocked(self, reason: str) -> None:
        log.warning("%s — restarting Lumos Cam", reason)
        result = self._start_lumos_unlocked(restart=True)
        if not result.get("ok") or not result.get("running"):
            return
        if result.get("ready") is False:
            log.warning(
                "Lumos Cam ffmpeg is up but no frames yet — opening the reader anyway"
            )
        try:
            self._recreate_source_unlocked()
        except Exception:
            log.exception("Failed to recreate capture source after Lumos Cam restart")

    def _tick_lumos_watchdog(self) -> None:
        cfg = self.config.lumos_cam
        if self._idle or not cfg.enabled or not cfg.auto_restart:
            return
        now = time.monotonic()
        if self._lumos.running:
            sync = getattr(self._lumos, "sync_output_transform", None)
            if callable(sync) and now >= self._lumos_transform_at:
                self._lumos_transform_at = now + 1.5
                sync(cfg)
            source = self.source
            if source is None:
                if now < self._lumos_next_retry:
                    return
                interval = max(2.0, float(cfg.restart_interval_sec or 5.0))
                self._lumos_next_retry = now + interval
                log.warning("Lumos Cam is running but capture source is gone — reopening")
                try:
                    self._recreate_source_unlocked()
                except Exception:
                    log.exception("Failed to reopen Lumos Cam capture source")
                return
            stats = getattr(source, "stats", None)
            if (
                isinstance(stats, dict)
                and int(stats.get("frames") or 0) > 0
                and frame_stream_stalled(
                    stats.get("last_frame_age"), self._lumos_stall_timeout_sec()
                )
            ):
                if now < self._lumos_next_retry:
                    return
                interval = max(2.0, float(cfg.restart_interval_sec or 5.0))
                self._lumos_next_retry = now + interval
                self._hold_leds_off_unlocked("feed_stall")
                self._restart_lumos_capture_unlocked(
                    f"Lumos Cam stream stalled "
                    f"(no frames for {float(stats.get('last_frame_age') or 0):.0f}s)"
                )
            return
        if now < self._lumos_next_retry:
            return
        interval = max(2.0, float(cfg.restart_interval_sec or 5.0))
        self._lumos_next_retry = now + interval
        if not adb_device_ready(cfg.serial, adb=(cfg.adb or "adb")):
            return
        self._restart_lumos_capture_unlocked("Lumos Cam not running (phone reconnected?)")

    def _led_path(self) -> str:
        return normalize_led_path(self.config.output.led_path)

    def _feed_stall_timeout_sec(self) -> float:
        if self.config.lumos_cam.enabled:
            return self._lumos_stall_timeout_sec()
        return max(8.0, float(self.config.camera.read_timeout or 8.0))

    def _note_feed_alive_unlocked(self) -> None:
        self._last_frame_at = time.monotonic()
        self._release_leds_hold_unlocked("feed_stall")

    def _maybe_hold_leds_for_feed_stall_unlocked(self) -> None:
        if self._idle or not self._last_frame_at:
            return
        if time.monotonic() - self._last_frame_at <= self._feed_stall_timeout_sec():
            return
        self._hold_leds_off_unlocked("feed_stall")

    def _hold_leds_off_unlocked(self, reason: str) -> None:
        """Turn LEDs off for ``reason`` without changing Lumos OS plugin/mode.

        If LEDs are already off for another reason (or we never owned them),
        do not take ownership — only the original holder may turn them back on.
        """
        if self._leds_hold_reason is not None:
            return
        if self._leds_off:
            return
        self._apply_led_output_unlocked(False)
        self._leds_hold_reason = reason
        log.info("LEDs held off (%s)", reason)

    def _release_leds_hold_unlocked(self, reason: str) -> None:
        if self._leds_hold_reason != reason:
            return
        self._apply_led_output_unlocked(True)
        self._leds_hold_reason = None
        log.info("LEDs restored after %s", reason)

    def _apply_led_output_unlocked(self, enabled: bool) -> None:
        if self._led_path() == "ddp":
            self._set_ddp_leds_unlocked(enabled)
            self._leds_off = not enabled
            return
        self._set_leds_unlocked(enabled)

    def _set_ddp_leds_unlocked(self, enabled: bool) -> None:
        sinks = self.sinks
        if sinks is None:
            return
        for sink in sinks.sinks:
            if getattr(sink, "name", "") != "ddp":
                continue
            if enabled:
                resume = getattr(sink, "resume", None)
                if callable(resume):
                    resume()
            else:
                hold_off = getattr(sink, "hold_off", None)
                if callable(hold_off):
                    hold_off()

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
            if "lumos_os.led_brightness" in updates:
                value = clamp_led_brightness(new_config.lumos_os.led_brightness)
                key = slot_key(new_config.color.profiles)
                existing = lookup_slot(new_config.color.profiles) or empty_slot()
                new_config = apply_updates(
                    new_config,
                    {
                        "lumos_os.led_brightness": value,
                        **store_slot_updates(
                            key, replace(existing, led_brightness=value)
                        ),
                    },
                )
            if profiles_touch_selection(updates):
                new_config = bind_config(new_config)
            old_lumos = self.config.lumos_cam
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
            lumos_restart = _lumos_stream_changed(old_lumos, new_config.lumos_cam) or (
                old_lumos.enabled != new_config.lumos_cam.enabled
            )
            lumos_live = any(key in _LUMOS_LIVE_KEYS for key in updates) or (
                profiles_touch_selection(updates)
            )
            if lumos_restart:
                if self._idle:
                    log.info("Skipping Lumos Cam restart while idle")
                else:
                    self._start_phone_capture_unlocked(restart=True)
            elif lumos_live and self.config.lumos_cam.enabled and not self._idle:
                self._lumos.apply_live(self.config.lumos_cam)
            if scrcpy_touched and not self._lumos_is_primary():
                if self._idle:
                    log.info("Skipping scrcpy restart while idle")
                else:
                    self._start_scrcpy_unlocked(restart=True)
            phone_touched = scrcpy_touched or lumos_restart
            if _updates_require_source_recreate(updates) or phone_touched:
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
            if _updates_require_sink_recreate(updates):
                self._recreate_sinks_unlocked()
            if "output.led_path" in updates:
                self._sync_lumos_vision_output_unlocked()
                if normalize_led_path(self.config.output.led_path) != "ddp":
                    self._led_test = None
                    if self._led_cal.state in {"running", "ready"}:
                        self._led_cal.abort()
            log.info("Config updated: %s", ", ".join(sorted(updates)))
            if any(
                key == "lumos_os" or key.startswith("lumos_os.") for key in updates
            ) or profiles_touch_selection(updates):
                self._apply_lumos_os_brightness_unlocked()
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
        self.source = self._make_source_unlocked().start()
        log.info(
            "Capture source recreated: %s (%s)",
            self.source.name,
            self.config.camera.source,
        )
        return {
            "ok": True,
            "source": self.source.name,
            "stats": dict(self.source.stats),
        }

    def _make_source_unlocked(self) -> FrameSource:
        kind = self._capture_source_kind()
        if kind == "lumos":
            return LumosPipeSource(self._lumos, self.config.camera)
        if kind == "scrcpy":
            sink = (self.config.scrcpy.v4l2_sink or "").strip() or "/dev/video11"
            camera = replace(self.config.camera, source="v4l2", device=sink)
            return create_source(camera)
        return create_source(self.config.camera)

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
        lumos_fields = {
            "serial",
            "camera_id",
            "camera_size",
            "camera_fps",
            "codec",
            "camera_zoom",
            "pan_x",
            "pan_y",
            "af",
            "ae",
            "awb",
        }
        scrcpy_fields = {
            "binary",
            "serial",
            "camera_id",
            "camera_size",
            "camera_fps",
            "camera_zoom",
            "view_zoom",
            "pan_x",
            "pan_y",
            "v4l2_sink",
        }
        source = str(fields.get("source") or self.config.camera.source or "").strip().lower()
        if source == "usb":
            source = "v4l2"

        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "source" and isinstance(value, str):
                value = source
            updates[f"camera.{key}"] = value
        updates["camera.source"] = source

        if source == "lumos":
            for key, value in fields.items():
                if key in lumos_fields:
                    updates[f"lumos_cam.{key}"] = value
            updates["lumos_cam.enabled"] = True
            updates["scrcpy.enabled"] = False
        elif source == "scrcpy":
            for key, value in fields.items():
                if key in scrcpy_fields:
                    updates[f"scrcpy.{key}"] = value
            sink = str(
                fields.get("v4l2_sink") or self.config.scrcpy.v4l2_sink or "/dev/video11"
            ).strip()
            updates["scrcpy.v4l2_sink"] = sink
            updates["camera.device"] = sink
            updates["scrcpy.enabled"] = True
            updates["lumos_cam.enabled"] = False
            updates["scrcpy.no_audio"] = True
            updates["scrcpy.no_playback"] = True
        if not any(k != "camera.source" for k in updates) and "source" not in fields:
            raise ValueError("no recognised camera source fields given")

        def apply() -> dict[str, Any]:
            self.config = apply_updates(self.config, updates)
            apply_pipeline_config(self.pipeline, self.config)
            self._ensure_loopbacks_unlocked()
            phone = {"ok": True, "skipped": True}
            recreated: dict[str, Any] = {"ok": True, "skipped": True}
            if not self._idle:
                phone = self._start_phone_capture_unlocked(restart=True)
                scrcpy_ready = (
                    self._capture_source_kind() != "scrcpy"
                    or (
                        bool(phone.get("ok"))
                        and bool(phone.get("running"))
                        and phone.get("ready", True)
                    )
                )
                if scrcpy_ready:
                    recreated = self._recreate_source_unlocked()
                else:
                    log.warning(
                        "Not opening %s until scrcpy is writing frames: %s",
                        self.config.scrcpy.v4l2_sink or "/dev/video11",
                        phone.get("error") or "not ready",
                    )
                    recreated = {
                        "ok": False,
                        "skipped": True,
                        "error": phone.get("error") or "scrcpy not ready",
                    }
            saved_path = None
            if save:
                saved_path = self.save()
            log.info("Camera source applied: %s", ", ".join(sorted(updates)))
            return {
                "ok": True,
                "source": self.config.camera.source,
                "config": config_to_dict(self.config),
                "recreated": recreated,
                "phone": phone,
                "saved": saved_path,
            }

        return self.call(apply, timeout=30.0)

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

    def lumos_cam_status(self) -> dict[str, Any]:
        return self._lumos.status(self.config.lumos_cam).as_dict()

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
                updates["camera.source"] = "scrcpy"
            elif action_name == "stop":
                updates["scrcpy.enabled"] = False
            elif action_name in {"apply", "restart"}:
                pass
            else:
                raise ValueError(f"unknown scrcpy action: {action_name}")

            if updates:
                if updates.get("scrcpy.enabled") is True:
                    updates["camera.source"] = "scrcpy"
                    updates["lumos_cam.enabled"] = False
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
            else:
                # Always go through _start_scrcpy_unlocked so the V4L2 reader
                # releases the sink before scrcpy reattaches (exclusive_caps).
                scrcpy_result = self._start_scrcpy_unlocked(restart=True)

            source_result: dict[str, Any] | None = None
            if (
                not self._idle
                and self.config.scrcpy.enabled
                and self._capture_source_kind() == "scrcpy"
                and action_name != "stop"
                and scrcpy_result.get("ok")
                and scrcpy_result.get("running")
                and scrcpy_result.get("ready", True)
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

    def apply_lumos_cam(
        self,
        fields: dict[str, Any] | None = None,
        *,
        action: str = "apply",
        save: bool = False,
    ) -> dict[str, Any]:
        """Start/stop Lumos Cam or apply live zoom/pan/locks from the wizard."""

        def run() -> dict[str, Any]:
            action_name = (action or "apply").strip().lower()
            updates: dict[str, Any] = {}
            for key, value in (fields or {}).items():
                if key in {
                    "enabled",
                    "serial",
                    "adb",
                    "package",
                    "camera_id",
                    "camera_size",
                    "camera_fps",
                    "codec",
                    "camera_zoom",
                    "zoom_min",
                    "zoom_max",
                    "pan_x",
                    "pan_y",
                    "af",
                    "ae",
                    "awb",
                    "ffmpeg",
                    "startup_timeout_sec",
                }:
                    updates[f"lumos_cam.{key}"] = value

            if "lumos_cam.pan_x" in updates:
                updates["lumos_cam.pan_x"] = clamp_pan(float(updates["lumos_cam.pan_x"]))
            if "lumos_cam.pan_y" in updates:
                updates["lumos_cam.pan_y"] = clamp_pan(float(updates["lumos_cam.pan_y"]))

            if action_name == "zoom_in":
                updates["lumos_cam.camera_zoom"] = step_lumos_zoom(
                    self.config.lumos_cam.camera_zoom, inward=True, cfg=self.config.lumos_cam
                )
                updates.setdefault("lumos_cam.enabled", True)
            elif action_name == "zoom_out":
                updates["lumos_cam.camera_zoom"] = step_lumos_zoom(
                    self.config.lumos_cam.camera_zoom, inward=False, cfg=self.config.lumos_cam
                )
                updates.setdefault("lumos_cam.enabled", True)
            elif action_name == "set_zoom":
                if "lumos_cam.camera_zoom" not in updates:
                    raise ValueError("camera_zoom required for set_zoom")
                updates["lumos_cam.camera_zoom"] = clamp_zoom(
                    float(updates["lumos_cam.camera_zoom"]), self.config.lumos_cam
                )
                updates.setdefault("lumos_cam.enabled", True)
            elif action_name.startswith("pan_"):
                pan_x, pan_y = step_lumos_pan(
                    self.config.lumos_cam, direction=action_name[len("pan_") :]
                )
                updates["lumos_cam.pan_x"] = pan_x
                updates["lumos_cam.pan_y"] = pan_y
                updates.setdefault("lumos_cam.enabled", True)
            elif action_name in {"lock_af", "unlock_af"}:
                updates["lumos_cam.af"] = "locked" if action_name == "lock_af" else "auto"
                if action_name == "unlock_af":
                    updates["lumos_cam.focus_distance"] = -1.0
            elif action_name in {"lock_ae", "unlock_ae"}:
                updates["lumos_cam.ae"] = "locked" if action_name == "lock_ae" else "auto"
                if action_name == "unlock_ae":
                    updates["lumos_cam.iso"] = 0
                    updates["lumos_cam.exposure_ns"] = 0
            elif action_name in {"lock_awb", "unlock_awb"}:
                updates["lumos_cam.awb"] = "locked" if action_name == "lock_awb" else "auto"
                if action_name == "unlock_awb":
                    updates["lumos_cam.awb_gains"] = []
            elif action_name == "cal_mode_on":
                if updates:
                    self.config = apply_updates(self.config, updates)
                result = self._set_lumos_cal_mode(True)
                return {
                    "ok": bool(result.get("ok", True)),
                    "action": action_name,
                    "lumos_cam": self._lumos.status(self.config.lumos_cam).as_dict(),
                    "result": result,
                    "config": config_to_dict(self.config),
                    "error": result.get("error"),
                }
            elif action_name == "cal_mode_off":
                if updates:
                    self.config = apply_updates(self.config, updates)
                result = self._set_lumos_cal_mode(False)
                return {
                    "ok": bool(result.get("ok", True)),
                    "action": action_name,
                    "lumos_cam": self._lumos.status(self.config.lumos_cam).as_dict(),
                    "result": result,
                    "config": config_to_dict(self.config),
                    "error": result.get("error"),
                }
            elif action_name in {"ui_rotate", "frame_rotate", "flip_h", "flip_v"}:
                payload = {
                    "ui_rotate": {"ui_rotate": 90},
                    "frame_rotate": {"frame_rotate": 90},
                    "flip_h": {"toggle_flip_h": True},
                    "flip_v": {"toggle_flip_v": True},
                }[action_name]
                result = self._lumos.set_display(payload)
                if action_name != "ui_rotate" and result.get("ok") and self._lumos.running:
                    self._lumos.sync_output_transform(self.config.lumos_cam)
                return {
                    "ok": bool(result.get("ok", True)),
                    "action": action_name,
                    "lumos_cam": self._lumos.status(self.config.lumos_cam).as_dict(),
                    "result": result,
                    "config": config_to_dict(self.config),
                    "error": result.get("error"),
                }
            elif action_name == "start":
                updates["lumos_cam.enabled"] = True
                updates["camera.source"] = "lumos"
            elif action_name == "stop":
                updates["lumos_cam.enabled"] = False
            elif action_name in {"apply", "restart"}:
                pass
            else:
                raise ValueError(f"unknown lumos_cam action: {action_name}")

            old_lumos = self.config.lumos_cam
            if updates:
                if updates.get("lumos_cam.enabled") is True:
                    updates["camera.source"] = "lumos"
                    updates["scrcpy.enabled"] = False
                self.config = apply_updates(self.config, updates)
            stream_changed = _lumos_stream_changed(old_lumos, self.config.lumos_cam)
            lumos_result: dict[str, Any]
            sidecar_restarted = False
            if self._idle and action_name != "stop":
                lumos_result = {
                    "ok": True,
                    "skipped": True,
                    "error": "TV idle — Lumos Cam will start on resume",
                }
            elif action_name == "stop" or not self.config.lumos_cam.enabled:
                lumos_result = self._lumos.stop()
            elif (
                self._lumos.running
                and not stream_changed
                and action_name != "restart"
            ):
                lumos_result = self._lumos.apply_live(self.config.lumos_cam)
            else:
                lumos_result = self._start_lumos_unlocked(restart=True)
                sidecar_restarted = True
                if lumos_result.get("ok") and lumos_result.get("running"):
                    self._lumos.apply_live(self.config.lumos_cam)

            source_result: dict[str, Any] | None = None
            if (
                not self._idle
                and self.config.lumos_cam.enabled
                and self._capture_source_kind() == "lumos"
                and action_name != "stop"
                and (sidecar_restarted or stream_changed)
                and lumos_result.get("ok")
                and lumos_result.get("running")
            ):
                try:
                    source_result = self._recreate_source_unlocked()
                except Exception as exc:
                    source_result = {"ok": False, "error": str(exc)}

            if action_name in {
                "lock_af",
                "unlock_af",
                "lock_ae",
                "unlock_ae",
                "lock_awb",
                "unlock_awb",
            } and lumos_result.get("ok"):
                self._merge_phone_camera_into_slot_unlocked()

            saved_path = None
            if save:
                saved_path = str(self.save())

            return {
                "ok": bool(lumos_result.get("ok", True)),
                "action": action_name,
                "lumos_cam": self._lumos.status(self.config.lumos_cam).as_dict(),
                "result": lumos_result,
                "source": source_result,
                "config": config_to_dict(self.config),
                "saved": saved_path,
                "error": lumos_result.get("error"),
            }

        return self.call(run, timeout=30.0)

    def _set_lumos_cal_mode(self, enabled: bool) -> dict[str, Any]:
        if not self.config.lumos_cam.enabled:
            return {"ok": True, "skipped": True}
        result = self._lumos.set_cal_mode(enabled)
        if not result.get("ok"):
            log.warning("Lumos Cam cal_mode(%s) failed: %s", enabled, result.get("error"))
        return result

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
            # Measuring must see the uncorrected panel (no WB / matrix / black / gamma).
            needs_bypass = (
                self.config.color.white_balance != "off"
                or self.config.color.matrix_enabled
                or self.config.color.black_level_enabled
                or abs(self.config.color.gamma - 1.0) > 1e-3
            )
            if needs_bypass:
                self.config = apply_updates(
                    self.config,
                    {
                        "color.white_balance": "off",
                        "color.matrix_enabled": False,
                        "color.black_level_enabled": False,
                        "color.gamma": 1.0,
                        "color.exposure.enabled": False,
                    },
                )
                apply_pipeline_config(self.pipeline, self.config)
            # Do not force AF/AE/AWB — the wizard checkboxes are the 3A control.
            # Clear leftover cal-mode so a previous run cannot keep all three locked.
            self._set_lumos_cal_mode(False)
            self._apply_profile_camera_unlocked()
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
            return {
                "ok": True,
                **status,
                "lumos_cal_mode": False,
                "color_profiles": profile_status(self.config.color),
            }

        return self.call(run)

    def abort_color_calibration(self) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            status = self._color_cal.abort()
            self._set_lumos_cal_mode(False)
            self._sync_color_profile_unlocked()
            log.info("Colour calibration aborted")
            return {
                "ok": True,
                **status,
                "color_profiles": profile_status(self.config.color),
            }

        return self.call(run)

    def _sync_color_profile_unlocked(self) -> None:
        self.config = bind_config(self.config)
        apply_pipeline_config(self.pipeline, self.config)
        self._apply_profile_camera_unlocked()
        self._apply_lumos_os_brightness_unlocked()

    def _apply_profile_camera_unlocked(self) -> None:
        if self.config.lumos_cam.enabled and self._lumos.running and not self._idle:
            result = self._lumos.apply_live(self.config.lumos_cam)
            if not result.get("ok"):
                log.warning("Profile camera apply failed: %s", result.get("error"))

    def _apply_lumos_os_brightness_unlocked(self) -> dict[str, Any]:
        """Push the active slot's LED brightness to Lumos OS if HyperHDR is on."""
        if self._idle or self._leds_hold_reason:
            return {"ok": True, "skipped": True, "reason": "idle"}
        value = clamp_led_brightness(self.config.lumos_os.led_brightness)
        return apply_led_brightness(self.config.lumos_os.url, value)

    def _sync_lumos_vision_output_unlocked(self) -> dict[str, Any]:
        """Match HyperHDR vs DDP only if Lumos OS is already on a vision plugin."""
        return sync_vision_output(
            self.config.lumos_os.url, self.config.output.led_path
        )

    def _merge_phone_camera_into_slot_unlocked(self) -> None:
        """Store checkbox lock flags plus phone numbers onto the active slot."""
        cam = camera_for_slot(
            self._lumos.status(self.config.lumos_cam).as_dict(),
            self.config.lumos_cam,
        )
        key = slot_key(self.config.color.profiles)
        existing = lookup_slot(self.config.color.profiles) or empty_slot()
        merged = replace(existing, camera=cam)
        self.config = apply_updates(
            self.config,
            {
                **store_slot_updates(key, merged),
                "lumos_cam.iso": cam.iso,
                "lumos_cam.exposure_ns": cam.exposure_ns,
                "lumos_cam.focus_distance": cam.focus_distance,
                "lumos_cam.awb_gains": list(cam.awb_gains),
            },
        )

    def set_color_profile(
        self,
        selection: dict[str, str] | None = None,
        *,
        save: bool = False,
    ) -> dict[str, Any]:
        """Switch the active environment combo; uncalibrated combos are passthrough."""

        def run() -> dict[str, Any]:
            try:
                resolved = resolve_selection(self.config.color.profiles, selection)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            updates = {
                f"color.profiles.selection.{key}": value
                for key, value in resolved.items()
            }
            self.config = apply_updates(self.config, updates)
            self._sync_color_profile_unlocked()
            saved_path = str(self.save()) if save else None
            status = profile_status(self.config.color)
            log.info(
                "Colour profile %s (%s)",
                status["label"],
                "calibrated" if status["calibrated"] else "no calibration",
            )
            return {
                "ok": True,
                **status,
                "saved": saved_path,
                "config": config_to_dict(self.config),
            }

        return self.call(run)

    def color_profile_status(self) -> dict[str, Any]:
        return profile_status(self.config.color)

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
            br, bg, bb = solution.black_level_rgb()
            matrix = solution.matrix_flat()
            black_enabled = any(v > 0.5 for v in solution.black_level_bgr)
            slot = slot_from_solution(solution)
            self._set_lumos_cal_mode(False)
            phone = self._lumos.status(self.config.lumos_cam).as_dict()
            cam = camera_for_slot(phone, self.config.lumos_cam)
            slot = replace(
                slot,
                camera=cam,
                led_brightness=clamp_led_brightness(
                    self.config.lumos_os.led_brightness
                ),
            )
            key = slot_key(self.config.color.profiles)
            updates = {
                "color.white_balance": "manual",
                "color.gains.r": r,
                "color.gains.g": g,
                "color.gains.b": b,
                "color.matrix_enabled": True,
                "color.matrix": matrix,
                "color.black_level_enabled": black_enabled,
                "color.black_level.r": br,
                "color.black_level.g": bg,
                "color.black_level.b": bb,
                "color.gamma": solution.gamma,
                # Saturation >1 turns a slightly warm camera white into orange-brown.
                "color.saturation": 1.0,
                "color.exposure.enabled": False,
                "color.calibration.calibrated_at": iso_now(),
                "color.calibration.patch_means_bgr": solution.patch_means_bgr,
                "color.calibration.matrix": matrix,
                "color.calibration.black_level.r": br,
                "color.calibration.black_level.g": bg,
                "color.calibration.black_level.b": bb,
                "color.calibration.notes": list(solution.notes),
            }
            updates.update(store_slot_updates(key, slot))
            updates.update(camera_live_updates(slot))
            self.config = apply_updates(self.config, updates)
            apply_pipeline_config(self.pipeline, self.config)
            saved_path = str(self.save()) if save else None
            log.info(
                "Colour calibration applied to %s (3×3 matrix, black R%.1f G%.1f B%.1f, gamma %.3f)%s",
                key,
                br,
                bg,
                bb,
                solution.gamma,
                f" → {saved_path}" if saved_path else "",
            )
            self._apply_profile_camera_unlocked()
            return {
                "ok": True,
                "solution": solution.as_dict(),
                "config": config_to_dict(self.config),
                "saved": saved_path,
                "calibration": self._color_cal.status(),
                "lumos_cal_mode": False,
                "color_profiles": profile_status(self.config.color),
            }

        return self.call(run)

    def _ddp_sink_unlocked(self) -> DdpSink | None:
        if self.sinks is None:
            return None
        for sink in self.sinks.sinks:
            if isinstance(sink, DdpSink):
                return sink
        return None

    def _led_color_status_unlocked(self) -> dict[str, Any]:
        ddp = self.config.output.ddp
        return {
            **self._led_cal.status(),
            "led_path": normalize_led_path(self.config.output.led_path),
            "rgb_order": ddp.rgb_order,
            "color_mode": ddp.color_mode,
            "test": self._led_test or "off",
            "config_matrix": list(ddp.color_matrix),
            "config_calibrated_at": ddp.calibrated_at or "",
        }

    def _push_led_flood_unlocked(self) -> dict[str, Any]:
        sink = self._ddp_sink_unlocked()
        if sink is None:
            if self._led_test or self._led_cal.state in {"running", "ready"}:
                return {"ok": False, "error": "Direct DDP is not the live LED path"}
            return {"ok": True, "skipped": True}
        if self._led_test:
            if self._led_test == "w":
                if bytes_per_led(self.config.output.ddp.color_mode) < 4:
                    return {"ok": False, "error": "W is only on RGBW strips"}
                sink.flood((0, 0, 0), apply_matrix=False, white=True)
            else:
                rgb = {
                    "r": (255, 0, 0),
                    "g": (0, 255, 0),
                    "b": (0, 0, 255),
                }[self._led_test]
                sink.flood(rgb, apply_matrix=False, white=False)
            return {"ok": True, "test": self._led_test}
        if self._led_cal.state in {"running", "ready"}:
            sink.flood(self._led_cal.drive_rgb(), apply_matrix=False, white=False)
            return {"ok": True, "patch": self._led_cal.patch[0]}
        sink.clear_flood()
        return {"ok": True}

    def led_color_status(self) -> dict[str, Any]:
        return self.call(lambda: {"ok": True, **self._led_color_status_unlocked()})

    def _led_color_reply(
        self, *, ok: bool, error: str = "", extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = {**self._led_color_status_unlocked(), **(extra or {})}
        payload["ok"] = ok
        if error:
            payload["error"] = error
        return payload

    def apply_led_color(self, body: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            action = str(body.get("action") or "").strip().lower()
            if normalize_led_path(self.config.output.led_path) != "ddp" and action not in {
                "",
                "status",
            }:
                return self._led_color_reply(
                    ok=False, error="LED colour sync is only for Direct (DDP)"
                )
            try:
                if action in {"", "status"}:
                    pass
                elif action == "test":
                    channel = str(body.get("channel") or "off").strip().lower()
                    if channel in {"", "off", "none"}:
                        self._led_test = None
                    elif channel in {"r", "g", "b", "w"}:
                        self._led_test = channel
                    else:
                        raise ValueError("channel must be r, g, b, w, or off")
                    pushed = self._push_led_flood_unlocked()
                    if not pushed.get("ok"):
                        self._led_test = None
                        return self._led_color_reply(
                            ok=False, error=str(pushed.get("error") or "flood failed")
                        )
                elif action == "start":
                    self._led_test = None
                    self._led_cal.start(self.config.output.ddp.color_matrix)
                    self._push_led_flood_unlocked()
                elif action == "match":
                    self._led_cal.match()
                    self._push_led_flood_unlocked()
                elif action == "adjust":
                    self._led_cal.begin_adjust()
                    self._push_led_flood_unlocked()
                elif action == "drive":
                    self._led_cal.set_drive(
                        int(body.get("r", 0)),
                        int(body.get("g", 0)),
                        int(body.get("b", 0)),
                    )
                    self._push_led_flood_unlocked()
                elif action == "commit":
                    self._led_cal.commit_adjust()
                    self._push_led_flood_unlocked()
                elif action == "next":
                    self._led_cal.next_patch()
                    self._push_led_flood_unlocked()
                elif action == "prev":
                    self._led_cal.prev_patch()
                    self._push_led_flood_unlocked()
                elif action == "goto":
                    index = body.get("index")
                    patch = body.get("patch")
                    self._led_cal.goto(
                        index=None if index is None else int(index),
                        patch=None if patch is None else str(patch),
                    )
                    self._push_led_flood_unlocked()
                elif action == "solve":
                    self._led_cal.solve()
                    self._push_led_flood_unlocked()
                elif action == "apply":
                    solution = self._led_cal.solution
                    if solution is None or self._led_cal.state != "ready":
                        return self._led_color_reply(
                            ok=False, error="solve the LED patches first"
                        )
                    updates = {
                        "output.ddp.color_matrix": list(solution),
                        "output.ddp.calibrated_at": iso_now(),
                    }
                    self.config = apply_updates(self.config, updates)
                    saved_path = str(self.save()) if body.get("save") else None
                    self._led_test = None
                    self._led_cal.abort()
                    self._led_cal.state = "idle"
                    if _updates_require_sink_recreate(updates):
                        self._recreate_sinks_unlocked()
                    else:
                        self._push_led_flood_unlocked()
                    log.info(
                        "LED colour matrix applied%s",
                        f" → {saved_path}" if saved_path else "",
                    )
                    return self._led_color_reply(ok=True, extra={"saved": saved_path})
                elif action == "reset":
                    updates = {
                        "output.ddp.color_matrix": list(IDENTITY_RGB_FLAT),
                        "output.ddp.calibrated_at": "",
                    }
                    self.config = apply_updates(self.config, updates)
                    saved_path = str(self.save()) if body.get("save") else None
                    self._led_test = None
                    if self._led_cal.state in {"running", "ready"}:
                        self._led_cal.start(IDENTITY_RGB_FLAT)
                    if _updates_require_sink_recreate(updates):
                        self._recreate_sinks_unlocked()
                    else:
                        self._push_led_flood_unlocked()
                    return self._led_color_reply(ok=True, extra={"saved": saved_path})
                elif action == "abort":
                    self._led_test = None
                    self._led_cal.abort()
                    self._push_led_flood_unlocked()
                else:
                    raise ValueError(f"unknown LED colour action: {action}")
            except (TypeError, ValueError) as exc:
                return self._led_color_reply(ok=False, error=str(exc))
            return self._led_color_reply(ok=True)

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
        if view == "source":
            if ctx is not None:
                return ctx.source
            return self._last_source
        if ctx is None:
            return None
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
                "leds_hold_reason": self._leds_hold_reason,
            },
            "scrcpy": self._scrcpy.status(self.config.scrcpy).as_dict(),
            "lumos_cam": self._lumos.status(self.config.lumos_cam).as_dict(),
            "color_calibration": self._color_cal.status(),
            "led_calibration": self._led_color_status_unlocked(),
            "color_profiles": profile_status(self.config.color),
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
