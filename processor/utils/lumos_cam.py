"""Manage Lumos Cam (Camera2 Android app) → ffmpeg pipe for phone capture.

Unlike scrcpy, zoom / pan / AF-AE-AWB locks are live HTTP to the phone.
ffmpeg is only restarted when the stream itself changes (codec, size, ports).
Decoded frames go to stdout as MJPEG — v4l2loopback is not used for capture.
"""

from __future__ import annotations

import fcntl
import http.client
import json
import math
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any

import cv2
import numpy as np

from processor.config.schema import LumosCamConfig
from processor.utils.logging import get_logger
from processor.utils.scrcpy import (
    PAN_STEP,
    ZOOM_STEP,
    adb_device_ready,
    clamp_pan,
    clamp_zoom,
    parse_camera_size,
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
    app_version: str
    protocol: int
    package_installed: bool
    last_error: str
    command: list[str]
    ui_rotation: int = 0
    frame_rotation: int = 0
    flip_h: bool = False
    flip_v: bool = False
    iso: int | None = None
    exposure_ns: int | None = None
    focus_distance: float | None = None
    awb_gains: list[float] = field(default_factory=list)

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
            "app_version": self.app_version,
            "protocol": self.protocol,
            "package_installed": self.package_installed,
            "last_error": self.last_error,
            "command": list(self.command),
            "ui_rotation": self.ui_rotation,
            "frame_rotation": self.frame_rotation,
            "flip_h": self.flip_h,
            "flip_v": self.flip_v,
            "iso": self.iso,
            "exposure_ns": self.exposure_ns,
            "focus_distance": self.focus_distance,
            "awb_gains": list(self.awb_gains),
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


_WLAN_INET = re.compile(r"inet\s+(\d{1,3}(?:\.\d{1,3}){3})")


def phone_lan_ipv4(cfg: LumosCamConfig) -> str:
    """Wi-Fi address of the phone, for a direct TCP video path that skips adb."""
    serial = (cfg.serial or "").strip()
    if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}:\d+$", serial):
        return serial.rsplit(":", 1)[0]
    try:
        result = subprocess.run(
            adb_argv(cfg, "shell", "ip", "-o", "-4", "addr", "show", "wlan0"),
            capture_output=True,
            text=True,
            check=False,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    match = _WLAN_INET.search(result.stdout or "")
    return match.group(1) if match else ""


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


# Long-edge cap for the phone JPEG. 1080p Camera2 stills on this Xiaomi
# cannot hold 30 fps, so the socket queues and playback goes slow-motion.
# 1280 is sharp enough for detect_width 480 and 640x360 output.
PIPE_MAX_EDGE = 1280


def build_ffmpeg_command(
    cfg: LumosCamConfig,
    *,
    binary: str | None = None,
    rotation: int = 0,
    max_edge: int = PIPE_MAX_EDGE,
    flip_h: bool = False,
    flip_v: bool = False,
) -> list[str]:
    exe = binary or resolve_ffmpeg(cfg.ffmpeg)
    codec = (cfg.codec or "h264").strip().lower()
    demux = "mjpeg" if codec == "mjpeg" else "h264"
    url = f"tcp://127.0.0.1:{int(cfg.video_host_port)}"
    fps = max(1, int(cfg.camera_fps or 30))
    cmd = [
        exe,
        "-hide_banner",
        "-loglevel",
        "warning",
        # Do not use -fflags nobuffer: it forces analyzeduration=0 even when
        # we set -analyzeduration, so ffmpeg exits before the phone encoder
        # attaches (bytes_sent=0 → "unspecified size").
        "-fflags",
        "discardcorrupt+genpts",
        "-flags",
        "low_delay",
        "-flush_packets",
        "1",
        # Annex-B over TCP has no PTS. ffmpeg then assumes 25 fps, so a 30 fps
        # phone plays in slow motion and the queue grows — USB or Wi-Fi.
        "-use_wallclock_as_timestamps",
        "1",
        "-analyzeduration",
        "300000",
        "-probesize",
        "65536",
        "-f",
        demux,
        "-framerate",
        str(fps),
        "-i",
        url,
        "-an",
        "-fps_mode",
        "passthrough",
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
    ]
    filters: list[str] = []
    rot = _rotation_filter(rotation)
    if rot:
        filters.append(rot)
    src_w, src_h = output_frame_size(cfg, rotation, max_edge=0)
    if max_edge > 0 and max(src_w, src_h) > int(max_edge):
        filters.append(
            f"scale={int(max_edge)}:{int(max_edge)}:force_original_aspect_ratio=decrease"
        )
    if flip_h:
        filters.append("hflip")
    if flip_v:
        filters.append("vflip")
    if filters:
        cmd.extend(["-vf", ",".join(filters)])
    # MJPEG on the pipe is framed (~50–150KiB after scale) so we never block
    # on a 6MiB raw BGR write. ffmpeg also flushes each JPEG; rawvideo often
    # emitted one frame and then stalled.
    cmd.extend(
        [
            "-q:v",
            "5",
            "-f",
            "mjpeg",
            "pipe:1",
        ]
    )
    return cmd


_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"


def _pop_newest_jpeg(buf: bytearray) -> bytes | None:
    """Return the newest complete JPEG in ``buf`` and drop consumed bytes."""
    newest: bytes | None = None
    while True:
        start = buf.find(_JPEG_SOI)
        if start < 0:
            buf.clear()
            return newest
        if start:
            del buf[:start]
        end = buf.find(_JPEG_EOI, 2)
        if end < 0:
            return newest
        newest = bytes(buf[: end + 2])
        del buf[: end + 2]


def _enlarge_pipe(fd: int) -> None:
    """Give ffmpeg room for one JPEG. 8MiB held ~1s of 1080p and looked delayed."""
    for size in (256 << 10, 64 << 10):
        try:
            fcntl.fcntl(fd, getattr(fcntl, "F_SETPIPE_SZ", 1031), size)
            return
        except (OSError, ValueError, AttributeError):
            continue


def output_frame_size(
    cfg: LumosCamConfig, rotation: int = 0, *, max_edge: int = PIPE_MAX_EDGE
) -> tuple[int, int]:
    width, height = parse_camera_size(cfg.camera_size or "1920x1080")
    if int(rotation) % 180 == 90:
        width, height = height, width
    edge = int(max_edge)
    long_edge = max(width, height)
    if edge > 0 and long_edge > edge:
        scale = edge / float(long_edge)
        width = max(2, int(round(width * scale)))
        height = max(2, int(round(height * scale)))
    return width, height


def _rotation_filter(degrees: int) -> str:
    turn = int(degrees) % 360
    if turn == 90:
        return "transpose=1"
    if turn == 180:
        return "transpose=1,transpose=1"
    if turn == 270:
        return "transpose=2"
    return ""


def _apply_output_transform(
    image: np.ndarray, rotation: int, flip_h: bool, flip_v: bool
) -> np.ndarray:
    turn = int(rotation) % 360
    if turn == 90:
        image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    elif turn == 180:
        image = cv2.rotate(image, cv2.ROTATE_180)
    elif turn == 270:
        image = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if flip_h:
        image = cv2.flip(image, 1)
    if flip_v:
        image = cv2.flip(image, 0)
    return image


def _phone_transform(phone: dict[str, Any]) -> tuple[int, bool, bool]:
    return (
        int(phone.get("orientation") or 0) % 360,
        bool(phone.get("flip_h")),
        bool(phone.get("flip_v")),
    )


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


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _optional_gains(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return []
    try:
        return [float(v) for v in value[:4]]
    except (TypeError, ValueError):
        return []


def _capture_lock_kwargs(cfg: LumosCamConfig) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "af": cfg.af,
        "ae": cfg.ae,
        "awb": cfg.awb,
    }
    if str(cfg.ae or "").lower() == "locked" and int(cfg.iso or 0) > 0 and int(cfg.exposure_ns or 0) > 0:
        kwargs["iso"] = int(cfg.iso)
        kwargs["exposure_ns"] = int(cfg.exposure_ns)
    if str(cfg.af or "").lower() == "locked" and float(cfg.focus_distance) >= 0:
        kwargs["focus_distance"] = float(cfg.focus_distance)
    if str(cfg.awb or "").lower() == "locked" and len(cfg.awb_gains or []) >= 4:
        kwargs["awb_gains"] = [float(v) for v in cfg.awb_gains[:4]]
    return kwargs


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

    def set_locks(
        self,
        *,
        af: str | None = None,
        ae: str | None = None,
        awb: str | None = None,
        iso: int | None = None,
        exposure_ns: int | None = None,
        focus_distance: float | None = None,
        awb_gains: list[float] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if af is not None:
            payload["af"] = af
        if ae is not None:
            payload["ae"] = ae
        if awb is not None:
            payload["awb"] = awb
        if iso is not None and int(iso) > 0:
            payload["iso"] = int(iso)
        if exposure_ns is not None and int(exposure_ns) > 0:
            payload["exposure_ns"] = int(exposure_ns)
        if focus_distance is not None and float(focus_distance) >= 0:
            payload["focus_distance"] = float(focus_distance)
        if awb_gains and len(awb_gains) >= 4:
            payload["awb_gains"] = [float(v) for v in awb_gains[:4]]
        return self.request("POST", "/locks", payload)

    def set_cal_mode(self, enabled: bool) -> dict[str, Any]:
        return self.request("POST", "/cal_mode", {"enabled": bool(enabled)})

    def set_display(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/display", fields)

    def set_stream(self, cfg: LumosCamConfig, *, enabled: bool = True) -> dict[str, Any]:
        width, height = output_frame_size(cfg, 0)
        return self.request(
            "POST",
            "/stream",
            {
                "enabled": enabled,
                "codec": (cfg.codec or "mjpeg").strip().lower(),
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
        self._proc: subprocess.Popen[bytes] | None = None
        self._command: list[str] = []
        self._last_error = ""
        self._ffmpeg = "ffmpeg"
        self._phone: dict[str, Any] = {}
        self.client = LumosCamClient()
        self._forwards_up = False
        self._cfg: LumosCamConfig | None = None
        self._frame_wh: tuple[int, int] = (1920, 1080)
        self._pending = b""
        self._held_frame: np.ndarray | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail: list[str] = []
        self._logged_first = False
        self._stream_transform: tuple[int, bool, bool] = (0, False, False)
        self._sock: socket.socket | None = None
        self._host_transform = False

    @property
    def running(self) -> bool:
        sock = self._sock
        if sock is not None:
            try:
                sock.getpeername()
                return True
            except OSError:
                self._sock = None
                return False
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
            app_version=str(phone.get("app_version", "")),
            protocol=int(phone.get("protocol", PROTOCOL_VERSION)),
            package_installed=bool(phone.get("package_installed", False)),
            last_error=last_error,
            command=list(self._command),
            ui_rotation=int(phone.get("ui_rotation", 0) or 0),
            frame_rotation=int(phone.get("frame_rotation", 0) or 0),
            flip_h=bool(phone.get("flip_h")),
            flip_v=bool(phone.get("flip_v")),
            iso=_optional_int(phone.get("iso")),
            exposure_ns=_optional_int(phone.get("exposure_ns")),
            focus_distance=_optional_float(phone.get("focus_distance")),
            awb_gains=_optional_gains(phone.get("awb_gains")),
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
            self.client.set_stream(cfg, enabled=True)
            self.client.set_locks(**_capture_lock_kwargs(cfg))
            phone = {**phone, **self.client.status()}
        except RuntimeError as exc:
            self._last_error = str(exc)
            log.error("lumos-cam control: %s", exc)
            return {"ok": False, "error": self._last_error, "running": False}

        codec = (cfg.codec or "mjpeg").strip().lower()
        if codec == "mjpeg":
            return self._start_jpeg_socket(cfg, phone)
        return self._start_ffmpeg(cfg, phone)

    def _close_sock(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.close()
        except OSError:
            pass

    def _connect_video(self, cfg: LumosCamConfig) -> socket.socket | None:
        candidates: list[tuple[str, int, str]] = []
        lan = phone_lan_ipv4(cfg)
        if lan:
            candidates.append((lan, int(cfg.video_device_port), "lan"))
        candidates.append(("127.0.0.1", int(cfg.video_host_port), "adb"))
        last = "video connect failed"
        for host, port, via in candidates:
            try:
                sock = socket.create_connection((host, port), timeout=2.0)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
                sock.setblocking(False)
                log.info("Lumos Cam video %s:%s via %s", host, port, via)
                return sock
            except OSError as exc:
                last = f"{host}:{port} ({via}): {exc}"
                log.warning("Lumos Cam video %s", last)
        self._last_error = last
        return None

    def _start_jpeg_socket(
        self, cfg: LumosCamConfig, phone: dict[str, Any], *, wait: bool = True
    ) -> dict[str, Any]:
        """Read Camera2 JPEGs ourselves so ffmpeg cannot queue H.264/TCP."""
        self._kill_ffmpeg_unlocked()
        self._close_sock()
        rotation, flip_h, flip_v = _phone_transform(phone)
        self._stream_transform = (rotation, flip_h, flip_v)
        self._host_transform = True
        self._frame_wh = output_frame_size(cfg, rotation)
        self._pending = b""
        self._held_frame = None
        self._logged_first = False
        sock = self._connect_video(cfg)
        if sock is None:
            return {"ok": False, "error": self._last_error, "running": False}
        self._sock = sock
        self._command = ["socket", f"{sock.getpeername()[0]}:{sock.getpeername()[1]}"]
        ready = self._wait_until_ready(cfg) if wait else True
        if not self.running:
            return {"ok": False, "error": self._last_error or "video socket closed", "running": False}
        if not ready:
            log.warning(
                "lumos-cam: no JPEG yet after %.0fs — leaving socket open",
                float(cfg.startup_timeout_sec),
            )
            return {
                "ok": True,
                "running": True,
                "ready": False,
                "command": list(self._command),
            }
        return {
            "ok": True,
            "running": True,
            "ready": True,
            "command": list(self._command),
        }

    def _start_ffmpeg(
        self,
        cfg: LumosCamConfig,
        phone: dict[str, Any],
        *,
        wait: bool = True,
    ) -> dict[str, Any]:
        self._close_sock()
        self._host_transform = False
        self._ffmpeg = resolve_ffmpeg(cfg.ffmpeg)
        rotation, flip_h, flip_v = _phone_transform(phone)
        self._stream_transform = (rotation, flip_h, flip_v)
        self._frame_wh = output_frame_size(cfg, rotation)
        self._pending = b""
        self._held_frame = None
        self._logged_first = False
        cmd = build_ffmpeg_command(
            cfg,
            binary=self._ffmpeg,
            rotation=rotation,
            flip_h=flip_h,
            flip_v=flip_v,
        )
        self._command = cmd
        log.info(
            "Starting Lumos Cam receiver %dx%d: %s",
            self._frame_wh[0],
            self._frame_wh[1],
            " ".join(cmd),
        )
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                bufsize=0,
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

        self._start_stderr_drain(self._proc)
        if self._proc.stdout is not None:
            try:
                fd = self._proc.stdout.fileno()
                os.set_blocking(fd, False)
                _enlarge_pipe(fd)
            except OSError:
                pass
        ready = self._wait_until_ready(cfg) if wait else True
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
            phone = self._phone or {}
            codec = (cfg.codec or "h264").strip().lower()
            detail = (
                f"no decoded frames from ffmpeg after {float(cfg.startup_timeout_sec):.0f}s "
                f"(phone streaming={phone.get('streaming')!r} "
                f"video_clients={phone.get('video_clients', 0)} "
                f"bytes_sent={phone.get('bytes_sent', 0)} "
                f"encoder_attached={phone.get('encoder_attached')!r})"
            )
            if codec == "mjpeg" and phone.get("encoder_attached") is False:
                detail += (
                    " — this phone never attached an MJPEG encoder; switch codec to h264"
                )
            if self.running:
                # Killing ffmpeg here made the watchdog restart-loop and the
                # wizard POST /api/camera/source time out (5s vs 15s wait).
                log.warning(
                    "lumos-cam: %s — leaving ffmpeg running; frames may still arrive",
                    detail,
                )
                return {
                    "ok": True,
                    "running": True,
                    "pid": proc.pid,
                    "ready": False,
                    "command": list(cmd),
                    "error": detail,
                }
            self._last_error = (
                f"{detail}. Keep Lumos Cam on screen. "
                "Sideload Lumos Cam ≥ 0.1.11 if bytes_sent stays 0."
            )
            log.error("lumos-cam: %s", self._last_error)
            self.stop()
            return {
                "ok": False,
                "error": self._last_error,
                "running": False,
                "ready": False,
                "command": list(cmd),
            }
        return {
            "ok": True,
            "running": True,
            "pid": proc.pid,
            "ready": True,
            "command": list(cmd),
        }

    def _kill_ffmpeg_unlocked(self) -> None:
        proc = self._proc
        self._proc = None
        self._pending = b""
        self._held_frame = None
        if proc is None:
            return
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        if proc.poll() is not None:
            return
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

    def stop(self) -> dict[str, Any]:
        if self._forwards_up:
            try:
                self.client.request("POST", "/stream", {"enabled": False}, timeout=1.5)
            except Exception:
                pass
        self._drop_forwards()
        self._close_sock()
        self._kill_ffmpeg_unlocked()
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
            self.client.set_locks(**_capture_lock_kwargs(cfg))
            self._phone = {**self._phone, **self.client.status()}
        except RuntimeError as exc:
            self._last_error = str(exc)
            return {"ok": False, "error": self._last_error, "running": self.running}
        return {"ok": True, "running": self.running, "live": True}

    def set_display(self, fields: dict[str, Any]) -> dict[str, Any]:
        try:
            phone = self.client.set_display(fields)
            self._phone = {**self._phone, **phone}
            return {"ok": True, **phone}
        except RuntimeError as exc:
            self._last_error = str(exc)
            return {"ok": False, "error": str(exc)}

    def sync_output_transform(self, cfg: LumosCamConfig) -> dict[str, Any]:
        """Respawn ffmpeg when the phone's orientation/flip changed."""
        if not self.running:
            return {"ok": True, "skipped": True}
        try:
            phone = self.client.status()
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        self._phone = {**self._phone, **phone}
        wanted = _phone_transform(phone)
        if wanted == self._stream_transform:
            return {"ok": True, "unchanged": True, "running": True}
        log.info(
            "Lumos Cam output transform changed %s -> %s — restarting ffmpeg",
            self._stream_transform,
            wanted,
        )
        return self._respawn_ffmpeg(cfg, phone)

    def _respawn_ffmpeg(self, cfg: LumosCamConfig, phone: dict[str, Any]) -> dict[str, Any]:
        if self._sock is not None:
            self._stream_transform = _phone_transform(phone)
            return {"ok": True, "running": True, "ready": True}
        self._kill_ffmpeg_unlocked()
        return self._start_ffmpeg(cfg, phone, wait=False)

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
            "Rebuild/sideload ≥ 0.1.11 if this APK is older."
        )
        return None

    def frame_size(self) -> tuple[int, int]:
        return self._frame_wh

    def _video_fd(self) -> int | None:
        sock = self._sock
        if sock is not None:
            try:
                return sock.fileno()
            except OSError:
                return None
        proc = self._proc
        stdout = None if proc is None else proc.stdout
        if proc is None or stdout is None or proc.poll() is not None:
            return None
        try:
            return stdout.fileno()
        except Exception:
            return None

    def read_bgr(self, timeout: float = 0.25) -> np.ndarray | None:
        """Read the newest JPEG and decode it to BGR."""
        if self._held_frame is not None:
            frame, self._held_frame = self._held_frame, None
            return frame
        fd = self._video_fd()
        if fd is None:
            return None
        deadline = time.monotonic() + max(0.0, timeout)
        buf = bytearray(self._pending)
        self._pending = b""
        jpeg: bytes | None = None
        while True:
            newest = _pop_newest_jpeg(buf)
            if newest is not None:
                jpeg = newest
                while True:
                    try:
                        extra = os.read(fd, 262144)
                    except (BlockingIOError, OSError):
                        break
                    if not extra:
                        break
                    buf += extra
                drained = _pop_newest_jpeg(buf)
                if drained is not None:
                    jpeg = drained
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._pending = bytes(buf)
                return None
            try:
                chunk = os.read(fd, 262144)
            except BlockingIOError:
                try:
                    ready, _, _ = select.select([fd], [], [], remaining)
                except (OSError, ValueError):
                    self._pending = bytes(buf)
                    return None
                if not ready:
                    self._pending = bytes(buf)
                    return None
                continue
            except OSError:
                return None
            if not chunk:
                self._pending = bytes(buf)
                return None
            buf += chunk
        self._pending = bytes(buf)
        if jpeg is None:
            return None
        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return None
        if self._host_transform:
            rot, flip_h, flip_v = self._stream_transform
            image = _apply_output_transform(image, rot, flip_h, flip_v)
        if not self._logged_first:
            self._logged_first = True
            height, width = image.shape[:2]
            log.info("Lumos Cam first frame %dx%d", width, height)
            self._frame_wh = (width, height)
        return image

    def _start_stderr_drain(self, proc: subprocess.Popen[bytes]) -> None:
        err = proc.stderr
        if err is None:
            return

        def drain() -> None:
            try:
                for raw in iter(err.readline, b""):
                    line = raw.decode("utf-8", "replace").rstrip()
                    if not line:
                        continue
                    self._stderr_tail.append(line)
                    if len(self._stderr_tail) > 40:
                        del self._stderr_tail[:10]
                    log.warning("lumos ffmpeg: %s", line)
            except Exception:
                return

        self._stderr_thread = threading.Thread(target=drain, name="lumos-ffmpeg-err", daemon=True)
        self._stderr_thread.start()

    def _wait_until_ready(self, cfg: LumosCamConfig) -> bool:
        deadline = time.monotonic() + max(0.5, float(cfg.startup_timeout_sec))
        last_log = 0.0
        while time.monotonic() < deadline:
            if not self.running:
                return False
            try:
                st = self.client.status()
                self._phone = {**self._phone, **st}
            except RuntimeError:
                st = {}
            if self._sock is None and (
                self._proc is None or self._proc.stdout is None
            ):
                return True
            # Drain from the first select: waiting on phone bytes_sent while
            # ffmpeg's 64KiB stdout pipe is full deadlocks the decoder.
            frame = self.read_bgr(timeout=0.4)
            if frame is not None:
                self._held_frame = frame
                return True
            now = time.monotonic()
            if now - last_log > 2.0:
                last_log = now
                log.info(
                    "Lumos Cam waiting for ffmpeg JPEG (%d bytes buffered, phone bytes_sent=%s)",
                    len(self._pending),
                    st.get("bytes_sent", "?"),
                )
        return False

    @staticmethod
    def _read_process_output(proc: subprocess.Popen[bytes]) -> str:
        chunks: list[str] = []
        try:
            out, err = proc.communicate(timeout=0.5)
        except Exception:
            out, err = b"", b""
            try:
                if proc.stderr:
                    err = proc.stderr.read() or b""
            except Exception:
                pass
        for blob in (err, out):
            if not blob:
                continue
            chunks.append(blob.decode("utf-8", "replace") if isinstance(blob, bytes) else str(blob))
        return "\n".join(chunks).strip()


__all__ = [
    "MIN_APP_VERSION",
    "PROTOCOL_VERSION",
    "LumosCamClient",
    "LumosCamManager",
    "LumosCamStatus",
    "build_ffmpeg_command",
    "output_frame_size",
    "package_installed",
    "phone_lan_ipv4",
    "step_lumos_pan",
    "step_lumos_zoom",
]
