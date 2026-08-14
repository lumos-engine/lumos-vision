"""Manage Lumos Cam (Camera2 Android app) → v4l2loopback for phone capture.

Unlike scrcpy, zoom / pan / AF-AE-AWB locks are live HTTP to the phone.
ffmpeg is only restarted when the stream itself changes (codec, size, sink).
"""

from __future__ import annotations

import http.client
import json
import math
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, replace
from typing import Any

from processor.config.schema import LumosCamConfig
from processor.utils.logging import get_logger
from processor.utils.scrcpy import (
    PAN_STEP,
    ZOOM_STEP,
    adb_device_ready,
    clamp_pan,
    clamp_zoom,
    parse_camera_size,
    sink_has_capture,
)

log = get_logger(__name__)

PROTOCOL_VERSION = 1
MIN_APP_VERSION = "0.1.0"
PACKAGE = "dev.lumos.cam"
ACTIVITY = "dev.lumos.cam.MainActivity"
PROTOCOL_HEADER = "Lumos-Cam-Protocol"


@dataclass
class LumosCamStatus:
    enabled: bool
    running: bool
    pid: int | None
    zoom: float
    pan_x: float
    pan_y: float
    af: str
    ae: str
    awb: str
    cal_mode: bool
    camera_id: str
    camera_size: str
    camera_fps: int
    codec: str
    v4l2_sink: str
    app_version: str
    protocol: int
    package_installed: bool
    last_error: str
    command: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "pid": self.pid,
            "zoom": self.zoom,
            "pan_x": self.pan_x,
            "pan_y": self.pan_y,
            "af": self.af,
            "ae": self.ae,
            "awb": self.awb,
            "cal_mode": self.cal_mode,
            "camera_id": self.camera_id,
            "camera_size": self.camera_size,
            "camera_fps": self.camera_fps,
            "codec": self.codec,
            "v4l2_sink": self.v4l2_sink,
            "app_version": self.app_version,
            "protocol": self.protocol,
            "package_installed": self.package_installed,
            "last_error": self.last_error,
            "command": list(self.command),
            "min_app_version": MIN_APP_VERSION,
        }


def list_adb_serials(*, adb: str = "adb") -> list[str]:
    try:
        result = subprocess.run(
            [adb, "devices"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    found: list[str] = []
    for line in (result.stdout or "").splitlines():
        text = line.strip()
        if not text or text.startswith("List of devices"):
            continue
        parts = text.split()
        if len(parts) >= 2 and parts[1] == "device":
            found.append(parts[0])
    return found


def resolve_adb_serial(cfg: LumosCamConfig) -> str:
    """Wireless debugging changes the TCP port; keep the same IP if possible."""
    wanted = (cfg.serial or "").strip()
    adb = (cfg.adb or "adb").strip() or "adb"
    devices = list_adb_serials(adb=adb)
    if wanted and wanted in devices:
        return wanted
    if wanted and ":" in wanted:
        host = wanted.rsplit(":", 1)[0]
        matches = [serial for serial in devices if serial.startswith(f"{host}:")]
        if len(matches) == 1:
            log.info("adb serial %s not connected; using %s", wanted, matches[0])
            return matches[0]
    if len(devices) == 1:
        if wanted and wanted != devices[0]:
            log.info("adb serial %s not connected; using %s", wanted, devices[0])
        return devices[0]
    return wanted


def adb_argv(cfg: LumosCamConfig, *args: str) -> list[str]:
    cmd = [(cfg.adb or "adb").strip() or "adb"]
    serial = (cfg.serial or "").strip()
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return cmd


def _adb_launch_error(result: subprocess.CompletedProcess[str]) -> str | None:
    combined = f"{result.stdout or ''}\n{result.stderr or ''}".strip()
    low = combined.lower()
    failed = (
        result.returncode != 0
        or "error type" in low
        or "does not exist" in low
        or "unable to resolve" in low
        or "activity not started" in low
        or "no activities found" in low
        or "monkey aborted" in low
    )
    if not failed:
        return None
    return combined or f"exit {result.returncode}"


def package_installed(cfg: LumosCamConfig) -> bool:
    try:
        result = subprocess.run(
            adb_argv(cfg, "shell", "pm", "path", cfg.package or PACKAGE),
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "package:" in (result.stdout or "")


def resolve_ffmpeg(configured: str) -> str:
    text = (configured or "").strip() or "ffmpeg"
    if os.path.isfile(text) and os.access(text, os.X_OK):
        return text
    found = shutil.which(text)
    return found or text


def build_ffmpeg_command(
    cfg: LumosCamConfig, *, binary: str | None = None, rotation: int = 0
) -> list[str]:
    exe = binary or resolve_ffmpeg(cfg.ffmpeg)
    codec = (cfg.codec or "h264").strip().lower()
    demux = "mjpeg" if codec == "mjpeg" else "h264"
    url = f"tcp://127.0.0.1:{int(cfg.video_host_port)}"
    cmd = [
        exe,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-f",
        demux,
        "-i",
        url,
    ]
    vf = _rotation_filter(rotation)
    if vf:
        cmd.extend(["-vf", vf])
    cmd.extend(
        [
            "-pix_fmt",
            "yuyv422",
            "-f",
            "v4l2",
            str(cfg.v4l2_sink),
        ]
    )
    return cmd


def _rotation_filter(degrees: int) -> str:
    turn = int(degrees) % 360
    if turn == 90:
        return "transpose=1"
    if turn == 180:
        return "transpose=1,transpose=1"
    if turn == 270:
        return "transpose=2"
    return ""


def step_lumos_zoom(current: float, *, inward: bool, cfg: LumosCamConfig) -> float:
    z = max(1e-6, float(current))
    index = round(math.log(z) / math.log(ZOOM_STEP))
    index += 1 if inward else -1
    return clamp_zoom(ZOOM_STEP**index, cfg)


def step_lumos_pan(cfg: LumosCamConfig, *, direction: str) -> tuple[float, float]:
    pan_x = clamp_pan(cfg.pan_x)
    pan_y = clamp_pan(cfg.pan_y)
    key = (direction or "").strip().lower()
    if key in {"left", "west", "l"}:
        pan_x = clamp_pan(pan_x - PAN_STEP)
    elif key in {"right", "east", "r"}:
        pan_x = clamp_pan(pan_x + PAN_STEP)
    elif key in {"up", "north", "u"}:
        pan_y = clamp_pan(pan_y - PAN_STEP)
    elif key in {"down", "south", "d"}:
        pan_y = clamp_pan(pan_y + PAN_STEP)
    elif key in {"center", "centre", "reset"}:
        pan_x, pan_y = 0.0, 0.0
    else:
        raise ValueError(f"unknown pan direction: {direction!r}")
    return pan_x, pan_y


class LumosCamClient:
    """HTTP client for the phone control port (after adb forward)."""

    def __init__(self) -> None:
        self.host_port = 18765

    def configure(self, cfg: LumosCamConfig) -> None:
        self.host_port = int(cfg.control_host_port)

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 3.0,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            PROTOCOL_HEADER: str(PROTOCOL_VERSION),
            "Connection": "close",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", int(self.host_port), timeout=timeout)
        try:
            conn.request(method.upper(), path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise RuntimeError(f"Lumos Cam control failed: {exc}") from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid JSON from Lumos Cam: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("Lumos Cam returned a non-object")
        if status >= 400 and not parsed.get("ok", False):
            raise RuntimeError(parsed.get("error") or f"HTTP {status}")
        return parsed

    def status(self) -> dict[str, Any]:
        return self.request("GET", "/status")

    def set_zoom(self, ratio: float) -> dict[str, Any]:
        return self.request("POST", "/zoom", {"ratio": float(ratio)})

    def set_pan(self, x: float, y: float) -> dict[str, Any]:
        return self.request("POST", "/pan", {"x": float(x), "y": float(y)})

    def set_locks(self, *, af: str | None = None, ae: str | None = None, awb: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if af is not None:
            payload["af"] = af
        if ae is not None:
            payload["ae"] = ae
        if awb is not None:
            payload["awb"] = awb
        return self.request("POST", "/locks", payload)

    def set_cal_mode(self, enabled: bool) -> dict[str, Any]:
        return self.request("POST", "/cal_mode", {"enabled": bool(enabled)})

    def set_stream(self, cfg: LumosCamConfig, *, enabled: bool = True) -> dict[str, Any]:
        width, height = parse_camera_size(cfg.camera_size)
        return self.request(
            "POST",
            "/stream",
            {
                "enabled": enabled,
                "codec": (cfg.codec or "h264").strip().lower(),
                "width": width,
                "height": height,
                "fps": int(cfg.camera_fps),
            },
            timeout=8.0,
        )

    def set_camera(self, camera_id: str) -> dict[str, Any]:
        return self.request("POST", "/camera", {"id": str(camera_id)})


class LumosCamManager:
    """Own adb forwards + ffmpeg receiver + HTTP control for Lumos Cam."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._command: list[str] = []
        self._last_error = ""
        self._ffmpeg = "ffmpeg"
        self._phone: dict[str, Any] = {}
        self.client = LumosCamClient()
        self._forwards_up = False
        self._cfg: LumosCamConfig | None = None

    @property
    def running(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        code = proc.poll()
        if code is not None:
            if code != 0 and not self._last_error:
                self._last_error = f"ffmpeg exited with code {code}"
            self._proc = None
            return False
        return True

    def status(self, cfg: LumosCamConfig) -> LumosCamStatus:
        alive = self.running
        pid = self._proc.pid if alive and self._proc is not None else None
        phone = self._phone
        waiting_adb = (
            bool(cfg.enabled)
            and bool(cfg.auto_restart)
            and not alive
            and not adb_device_ready(cfg.serial, adb=(cfg.adb or "adb"))
        )
        last_error = self._last_error
        if waiting_adb and not last_error:
            last_error = "waiting for adb device (reconnect phone / accept USB debugging)"
        return LumosCamStatus(
            enabled=bool(cfg.enabled),
            running=alive,
            pid=pid,
            zoom=float(phone.get("zoom", cfg.camera_zoom)),
            pan_x=float(phone.get("pan_x", cfg.pan_x)),
            pan_y=float(phone.get("pan_y", cfg.pan_y)),
            af=str(phone.get("af", cfg.af)),
            ae=str(phone.get("ae", cfg.ae)),
            awb=str(phone.get("awb", cfg.awb)),
            cal_mode=bool(phone.get("cal_mode", False)),
            camera_id=str(phone.get("camera_id", cfg.camera_id)),
            camera_size=str(phone.get("size", cfg.camera_size)),
            camera_fps=int(phone.get("fps", cfg.camera_fps)),
            codec=str(phone.get("codec", cfg.codec)),
            v4l2_sink=str(cfg.v4l2_sink),
            app_version=str(phone.get("app_version", "")),
            protocol=int(phone.get("protocol", PROTOCOL_VERSION)),
            package_installed=bool(phone.get("package_installed", False)),
            last_error=last_error,
            command=list(self._command),
        )

    def ensure_running(self, cfg: LumosCamConfig) -> dict[str, Any]:
        if not cfg.enabled:
            self.stop()
            return {"ok": True, "running": False, "skipped": True}
        if self.running:
            return {"ok": True, "running": True, "pid": self._proc.pid if self._proc else None}
        return self.start(cfg)

    def start(self, cfg: LumosCamConfig) -> dict[str, Any]:
        self.stop()
        self._last_error = ""
        if not cfg.enabled:
            return {"ok": True, "running": False, "skipped": True}

        sink = (cfg.v4l2_sink or "").strip()
        if sink and not os.path.exists(sink):
            self._last_error = (
                f"v4l2 sink missing: {sink} — recreate Android Cam loopback, e.g.\n"
                "  sudo modprobe -r v4l2loopback\n"
                "  sudo modprobe v4l2loopback devices=2 video_nr=10,11 "
                'card_label="Screen Sight","Android Cam" exclusive_caps=1\n'
                "(stop Screen Sight / HyperHDR first; they use video10)"
            )
            log.error("lumos-cam: %s", self._last_error.replace("\n", " | "))
            return {"ok": False, "error": self._last_error, "running": False}

        cfg = replace(cfg, serial=resolve_adb_serial(cfg))
        if not adb_device_ready(cfg.serial, adb=(cfg.adb or "adb")):
            self._last_error = "no authorized adb device"
            return {"ok": False, "error": self._last_error, "running": False}

        installed = package_installed(cfg)
        self._phone["package_installed"] = installed
        if not installed:
            self._last_error = (
                f"Lumos Cam not installed (needs ≥ {MIN_APP_VERSION}) — "
                f"sideload package {cfg.package or PACKAGE}"
            )
            return {"ok": False, "error": self._last_error, "running": False}

        self._cfg = cfg
        launch = self._launch_app(cfg)
        if not launch.get("ok"):
            log.warning(
                "Lumos Cam launch: %s — continuing if the app is already open",
                launch.get("error"),
            )

        if not self._setup_forwards(cfg):
            return {"ok": False, "error": self._last_error, "running": False}

        self.client.configure(cfg)
        phone = self._wait_for_control(cfg)
        if phone is None:
            extra = ""
            if not launch.get("ok"):
                extra = f" Launch: {launch.get('error')}"
            self._last_error = (self._last_error or "control not responding") + extra
            return {"ok": False, "error": self._last_error, "running": False}
        self._phone = phone
        self._phone["package_installed"] = True

        try:
            self.client.set_camera(cfg.camera_id)
            self.client.set_zoom(clamp_zoom(cfg.camera_zoom, cfg))
            self.client.set_pan(clamp_pan(cfg.pan_x), clamp_pan(cfg.pan_y))
            self.client.set_locks(af=cfg.af, ae=cfg.ae, awb=cfg.awb)
            self.client.set_stream(cfg, enabled=True)
            phone = {**phone, **self.client.status()}
        except RuntimeError as exc:
            self._last_error = str(exc)
            log.error("lumos-cam control: %s", exc)
            return {"ok": False, "error": self._last_error, "running": False}

        self._ffmpeg = resolve_ffmpeg(cfg.ffmpeg)
        rotation = int(phone.get("orientation") or 0)
        cmd = build_ffmpeg_command(cfg, binary=self._ffmpeg, rotation=rotation)
        self._command = cmd
        log.info("Starting Lumos Cam receiver: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError:
            self._last_error = f"ffmpeg binary not found: {self._ffmpeg}"
            log.error("lumos-cam: %s", self._last_error)
            self._proc = None
            return {"ok": False, "error": self._last_error, "running": False}
        except OSError as exc:
            self._last_error = str(exc)
            self._proc = None
            return {"ok": False, "error": self._last_error, "running": False}

        ready = self._wait_until_ready(cfg)
        proc = self._proc
        if proc is not None and proc.poll() is not None:
            output = self._read_process_output(proc)
            self._proc = None
            self._last_error = output.strip() or f"ffmpeg exited with code {proc.returncode}"
            log.error("lumos-cam ffmpeg failed: %s", self._last_error)
            return {"ok": False, "error": self._last_error, "running": False}

        if not self.running or proc is None:
            self._last_error = self._last_error or "ffmpeg failed to stay running"
            return {"ok": False, "error": self._last_error, "running": False}

        try:
            self._phone = {**self._phone, **self.client.status()}
        except RuntimeError:
            pass

        if not ready:
            log.warning(
                "Lumos Cam ffmpeg is running (pid %s) but %s did not become ready within %.1fs",
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
        if self._forwards_up:
            try:
                self.client.request("POST", "/stream", {"enabled": False}, timeout=1.5)
            except Exception:
                pass
        proc = self._proc
        self._proc = None
        self._drop_forwards()
        if proc is None:
            return {"ok": True, "running": False}
        if proc.poll() is not None:
            return {"ok": True, "running": False}
        log.info("Stopping Lumos Cam ffmpeg (pid %s)", proc.pid)
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

    def restart(self, cfg: LumosCamConfig) -> dict[str, Any]:
        self.stop()
        return self.start(cfg)

    def apply_live(self, cfg: LumosCamConfig) -> dict[str, Any]:
        """Push zoom/pan/locks without restarting ffmpeg."""
        if not cfg.enabled:
            return self.stop()
        self.client.configure(cfg)
        try:
            self.client.set_zoom(clamp_zoom(cfg.camera_zoom, cfg))
            self.client.set_pan(clamp_pan(cfg.pan_x), clamp_pan(cfg.pan_y))
            self.client.set_locks(af=cfg.af, ae=cfg.ae, awb=cfg.awb)
            self._phone = {**self._phone, **self.client.status()}
        except RuntimeError as exc:
            self._last_error = str(exc)
            return {"ok": False, "error": self._last_error, "running": self.running}
        return {"ok": True, "running": self.running, "live": True}

    def set_cal_mode(self, enabled: bool) -> dict[str, Any]:
        try:
            phone = self.client.set_cal_mode(enabled)
            self._phone = {**self._phone, **phone}
            return {"ok": True, **phone}
        except RuntimeError as exc:
            self._last_error = str(exc)
            return {"ok": False, "error": str(exc)}

    def _launch_app(self, cfg: LumosCamConfig) -> dict[str, Any]:
        pkg = (cfg.package or PACKAGE).strip()
        activity = (cfg.activity or ACTIVITY).strip()
        component = f"{pkg}/{activity}"
        # MIUI ActivityStarterImpl: component-only `am start -n` logs
        # "aInfo is null" / Error type 3 even when dumpsys lists the launcher.
        # MAIN+LAUNCHER must be on the Intent so resolveActivity fills aInfo.
        attempts: tuple[tuple[str, ...], ...] = (
            (
                "am",
                "start",
                "--user",
                "0",
                "-W",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
                "-n",
                component,
            ),
            ("monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"),
            ("am", "start", "--user", "0", "-W", "-n", f"{pkg}/.MainActivity"),
        )
        last_err = "launch failed"
        launched = False
        for args in attempts:
            try:
                result = subprocess.run(
                    adb_argv(cfg, "shell", *args),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20.0,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_err = str(exc)
                continue
            fail = _adb_launch_error(result)
            if fail is None:
                launched = True
                break
            last_err = fail
        if not launched:
            return {"ok": False, "error": last_err}
        try:
            subprocess.run(
                adb_argv(cfg, "shell", "am", "start-foreground-service", "-n", f"{pkg}/.CaptureService"),
                capture_output=True,
                text=True,
                check=False,
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.4)
        return {"ok": True, "package": pkg}

    def _setup_forwards(self, cfg: LumosCamConfig) -> bool:
        self._drop_forwards()
        pairs = (
            (int(cfg.control_host_port), int(cfg.control_device_port)),
            (int(cfg.video_host_port), int(cfg.video_device_port)),
        )
        for host, device in pairs:
            try:
                result = subprocess.run(
                    adb_argv(cfg, "forward", f"tcp:{host}", f"tcp:{device}"),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5.0,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                self._last_error = f"adb forward failed: {exc}"
                return False
            if result.returncode != 0:
                self._last_error = (result.stderr or result.stdout or "adb forward failed").strip()
                return False
        self._forwards_up = True
        return True

    def _drop_forwards(self) -> None:
        cfg = self._cfg
        if cfg is None:
            self._forwards_up = False
            return
        for host in (int(cfg.control_host_port), int(cfg.video_host_port)):
            try:
                subprocess.run(
                    adb_argv(cfg, "forward", "--remove", f"tcp:{host}"),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=3.0,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._forwards_up = False

    def _wait_for_control(self, cfg: LumosCamConfig) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(2.0, float(cfg.startup_timeout_sec))
        last = "Lumos Cam control not responding"
        while time.monotonic() < deadline:
            try:
                phone = self.client.status()
                if phone.get("ok") or phone.get("protocol"):
                    return phone
                last = phone.get("error") or last
            except RuntimeError as exc:
                last = str(exc)
            time.sleep(0.35)
        self._last_error = (
            f"{last} — nothing is listening on device port {int(cfg.control_device_port)}. "
            "Open Lumos Cam, grant camera, keep it on screen, then retry. "
            "Rebuild/sideload ≥ 0.1.10 if this APK is older."
        )
        return None

    def _wait_until_ready(self, cfg: LumosCamConfig) -> bool:
        sink = (cfg.v4l2_sink or "").strip()
        deadline = time.monotonic() + max(0.5, float(cfg.startup_timeout_sec))
        while time.monotonic() < deadline:
            if not self.running:
                return False
            if sink and os.path.exists(sink) and sink_has_capture(sink):
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


__all__ = [
    "MIN_APP_VERSION",
    "PROTOCOL_VERSION",
    "LumosCamClient",
    "LumosCamManager",
    "LumosCamStatus",
    "build_ffmpeg_command",
    "package_installed",
    "step_lumos_pan",
    "step_lumos_zoom",
]
