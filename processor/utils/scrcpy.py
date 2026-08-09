"""Manage an scrcpy camera → v4l2loopback sidecar for Android phone capture.

Scrcpy only accepts an absolute ``--camera-zoom`` at process start.  Live
MOD+↑/↓ control stays inside the scrcpy process, so Screen Sight applies zoom
(and size/fps changes) by restarting the child quickly while the pipeline
keeps running and reconnects to the loopback.
"""

from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from processor.config.schema import ScrcpyConfig
from processor.utils.logging import get_logger

log = get_logger(__name__)

#: Matches scrcpy's CameraCapture.ZOOM_FACTOR (1 + 1/16).
ZOOM_STEP = 1.0 + 1.0 / 16.0

_DEFAULT_BINARIES = (
    "scrcpy",
    "/opt/scrcpy/scrcpy",
    "/usr/local/bin/scrcpy",
    "/usr/bin/scrcpy",
)


@dataclass
class ScrcpyStatus:
    enabled: bool
    running: bool
    pid: int | None
    zoom: float
    camera_id: str
    camera_size: str
    camera_fps: int
    v4l2_sink: str
    binary: str
    last_error: str
    command: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "pid": self.pid,
            "zoom": self.zoom,
            "camera_id": self.camera_id,
            "camera_size": self.camera_size,
            "camera_fps": self.camera_fps,
            "v4l2_sink": self.v4l2_sink,
            "binary": self.binary,
            "last_error": self.last_error,
            "command": list(self.command),
        }


def resolve_scrcpy_binary(configured: str) -> str:
    text = (configured or "").strip() or "scrcpy"
    if os.path.isfile(text) and os.access(text, os.X_OK):
        return text
    found = shutil.which(text)
    if found:
        return found
    for candidate in _DEFAULT_BINARIES:
        if candidate == text:
            continue
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        which = shutil.which(candidate)
        if which:
            return which
    return text


def clamp_zoom(value: float, cfg: ScrcpyConfig) -> float:
    lo = max(0.01, float(cfg.zoom_min))
    hi = max(lo, float(cfg.zoom_max))
    return max(lo, min(hi, float(value)))


def step_zoom(current: float, *, inward: bool, cfg: ScrcpyConfig) -> float:
    """One scrcpy-equivalent zoom step (exponential)."""
    z = max(1e-6, float(current))
    index = round(math.log(z) / math.log(ZOOM_STEP))
    index += 1 if inward else -1
    return clamp_zoom(ZOOM_STEP**index, cfg)


def build_scrcpy_command(cfg: ScrcpyConfig, *, binary: str | None = None) -> list[str]:
    exe = binary or resolve_scrcpy_binary(cfg.binary)
    cmd = [
        exe,
        "--video-source=camera",
        f"--camera-id={cfg.camera_id}",
        f"--camera-size={cfg.camera_size}",
        f"--camera-fps={int(cfg.camera_fps)}",
        f"--camera-zoom={clamp_zoom(cfg.camera_zoom, cfg):g}",
        f"--v4l2-sink={cfg.v4l2_sink}",
    ]
    if cfg.no_audio:
        cmd.append("--no-audio")
    if cfg.no_playback:
        cmd.append("--no-playback")
    serial = (cfg.serial or "").strip()
    if serial:
        cmd.extend(["-s", serial])
    for arg in cfg.extra_args:
        text = str(arg).strip()
        if text:
            cmd.append(text)
    return cmd


class ScrcpyManager:
    """Own one scrcpy child process tied to :class:`ScrcpyConfig`."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._command: list[str] = []
        self._last_error = ""
        self._binary = "scrcpy"

    @property
    def running(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        code = proc.poll()
        if code is not None:
            if code != 0 and not self._last_error:
                self._last_error = f"scrcpy exited with code {code}"
            self._proc = None
            return False
        return True

    def status(self, cfg: ScrcpyConfig) -> ScrcpyStatus:
        alive = self.running
        pid = self._proc.pid if alive and self._proc is not None else None
        return ScrcpyStatus(
            enabled=bool(cfg.enabled),
            running=alive,
            pid=pid,
            zoom=float(cfg.camera_zoom),
            camera_id=str(cfg.camera_id),
            camera_size=str(cfg.camera_size),
            camera_fps=int(cfg.camera_fps),
            v4l2_sink=str(cfg.v4l2_sink),
            binary=self._binary or cfg.binary,
            last_error=self._last_error,
            command=list(self._command),
        )

    def ensure_running(self, cfg: ScrcpyConfig) -> dict[str, Any]:
        if not cfg.enabled:
            self.stop()
            return {"ok": True, "running": False, "skipped": True}
        if self.running:
            return {"ok": True, "running": True, "pid": self._proc.pid if self._proc else None}
        return self.start(cfg)

    def start(self, cfg: ScrcpyConfig) -> dict[str, Any]:
        self.stop()
        self._last_error = ""
        if not cfg.enabled:
            return {"ok": True, "running": False, "skipped": True}

        sink = (cfg.v4l2_sink or "").strip()
        if sink and not os.path.exists(sink):
            self._last_error = f"v4l2 sink missing: {sink}"
            log.error("scrcpy: %s", self._last_error)
            return {"ok": False, "error": self._last_error, "running": False}

        self._binary = resolve_scrcpy_binary(cfg.binary)
        cmd = build_scrcpy_command(cfg, binary=self._binary)
        self._command = cmd
        log.info("Starting scrcpy: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError:
            self._last_error = f"scrcpy binary not found: {self._binary}"
            log.error("scrcpy: %s", self._last_error)
            self._proc = None
            return {"ok": False, "error": self._last_error, "running": False}
        except OSError as exc:
            self._last_error = str(exc)
            log.error("scrcpy failed to start: %s", exc)
            self._proc = None
            return {"ok": False, "error": self._last_error, "running": False}

        ready = self._wait_until_ready(cfg)
        proc = self._proc
        if proc is not None and proc.poll() is not None:
            output = self._read_process_output(proc)
            self._proc = None
            self._last_error = output.strip() or f"scrcpy exited with code {proc.returncode}"
            log.error("scrcpy startup failed: %s", self._last_error)
            return {"ok": False, "error": self._last_error, "running": False}

        if not self.running or proc is None:
            self._last_error = self._last_error or "scrcpy failed to stay running"
            return {"ok": False, "error": self._last_error, "running": False}

        if not ready:
            log.warning(
                "scrcpy is running (pid %s) but %s did not become ready within %.1fs",
                proc.pid,
                sink or "(no sink)",
                float(cfg.startup_timeout_sec),
            )
        return {
            "ok": True,
            "running": True,
            "pid": proc.pid,
            "ready": ready,
            "command": list(cmd),
        }

    def stop(self) -> dict[str, Any]:
        proc = self._proc
        self._proc = None
        if proc is None:
            return {"ok": True, "running": False}
        if proc.poll() is not None:
            return {"ok": True, "running": False}
        log.info("Stopping scrcpy (pid %s)", proc.pid)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        return {"ok": True, "running": False}

    def restart(self, cfg: ScrcpyConfig) -> dict[str, Any]:
        self.stop()
        return self.start(cfg)

    def apply_zoom(self, cfg: ScrcpyConfig, zoom: float) -> tuple[ScrcpyConfig, dict[str, Any]]:
        """Return config with clamped zoom and restart result."""
        from dataclasses import replace

        new_cfg = replace(cfg, camera_zoom=clamp_zoom(zoom, cfg), enabled=True)
        result = self.restart(new_cfg) if new_cfg.enabled else self.stop()
        return new_cfg, result

    def _wait_until_ready(self, cfg: ScrcpyConfig) -> bool:
        sink = (cfg.v4l2_sink or "").strip()
        deadline = time.monotonic() + max(0.5, float(cfg.startup_timeout_sec))
        while time.monotonic() < deadline:
            if not self.running:
                return False
            if sink and os.path.exists(sink) and _sink_has_capture(sink):
                return True
            if not sink:
                time.sleep(0.4)
                return self.running
            time.sleep(0.25)
        return False

    @staticmethod
    def _read_process_output(proc: subprocess.Popen[str]) -> str:
        try:
            out, _ = proc.communicate(timeout=0.5)
            return out or ""
        except Exception:
            try:
                if proc.stdout:
                    return proc.stdout.read() or ""
            except Exception:
                return ""
            return ""


def _sink_has_capture(device: str) -> bool:
    """Best-effort: device exists and (optionally) advertises capture."""
    if not os.path.exists(device):
        return False
    try:
        result = subprocess.run(
            ["v4l2-ctl", "-d", device, "--all"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Device node present is enough when v4l2-ctl is missing.
        return True
    text = (result.stdout or "").lower()
    if "video capture" in text:
        return True
    # exclusive_caps: before producer attaches, capture may be absent briefly.
    return result.returncode == 0


__all__ = [
    "ZOOM_STEP",
    "ScrcpyManager",
    "ScrcpyStatus",
    "build_scrcpy_command",
    "clamp_zoom",
    "resolve_scrcpy_binary",
    "step_zoom",
]
