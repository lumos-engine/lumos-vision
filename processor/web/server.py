"""HTTP server for the calibration wizard.

Standard library only.  It is a handful of JSON endpoints plus MJPEG previews,
and pulling in a web framework for that would add more startup cost and more
memory than the entire processing pipeline uses.

Every mutation is dispatched onto the pipeline thread through
``Processor.submit``, so the browser can never race the video path.
"""

from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import cv2

from processor.app import Processor
from processor.camera.devices import list_capture_devices
from processor.camera.rtsp import redact_url
from processor.config.loader import config_to_dict
from processor.output.mjpeg import write_mjpeg_stream
from processor.pipeline.registry import describe_stages
from processor.utils.logging import get_logger

log = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 256 * 1024


def public_config(processor: Processor) -> dict[str, Any]:
    """Config for the browser, with camera credentials removed."""
    data = config_to_dict(processor.config)
    url = data.get("camera", {}).get("rtsp_url", "")
    if url:
        data["camera"]["rtsp_url"] = redact_url(url)
    return data


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ScreenSight/0.1"

    @property
    def processor(self) -> Processor:
        return self.server.processor  # type: ignore[attr-defined]

    # -- helpers -----------------------------------------------------------

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        try:
            if route in ("/", "/index.html"):
                return self._serve_static("index.html")
            if route.startswith("/static/"):
                return self._serve_static(route[len("/static/") :])
            if route.startswith("/stream/"):
                return self._serve_stream(route[len("/stream/") :])
            if route == "/api/status":
                return self._send_json(self._status())
            if route == "/api/config":
                return self._send_json(public_config(self.processor))
            if route == "/api/camera/controls":
                return self._send_json(self.processor.list_camera_controls())
            if route == "/api/camera/devices":
                return self._send_json(self._camera_devices())
            if route == "/api/scrcpy":
                return self._send_json(
                    {"ok": True, "scrcpy": self.processor.scrcpy_status()}
                )
            if route == "/api/snapshot":
                return self._serve_snapshot(query.get("view", ["source"])[0])
            self.send_error(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log.exception("GET %s failed", route)
            self._safe_error(500, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        try:
            body = self._read_json()
        except ValueError as exc:
            return self._send_json({"ok": False, "error": str(exc)}, status=400)

        try:
            if route == "/api/config":
                return self._update_config(body)
            if route == "/api/config/save":
                path = self.processor.save(body.get("path") or None)
                return self._send_json({"ok": True, "path": path})
            if route == "/api/calibrate/auto":
                return self._send_json(self.processor.auto_detect())
            if route == "/api/calibrate/corners":
                return self._set_corners(body)
            if route == "/api/recalibrate":
                self.processor.force_recalibration()
                return self._send_json({"ok": True})
            if route == "/api/stage":
                return self._toggle_stage(body)
            if route == "/api/camera/controls":
                controls = body.get("controls")
                if not isinstance(controls, dict) or not controls:
                    return self._send_json(
                        {"ok": False, "error": "controls mapping required"}, status=400
                    )
                return self._send_json(self.processor.set_camera_controls(controls))
            if route == "/api/camera/source":
                return self._apply_camera_source(body)
            if route == "/api/scrcpy":
                return self._apply_scrcpy(body)
            self.send_error(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log.exception("POST %s failed", route)
            self._safe_error(500, str(exc))

    def _safe_error(self, status: int, message: str) -> None:
        try:
            self._send_json({"ok": False, "error": message}, status=status)
        except Exception:
            pass

    # -- endpoints ---------------------------------------------------------

    def _status(self) -> dict[str, Any]:
        status = self.processor.status()
        status["stages_available"] = describe_stages()
        status["config"] = public_config(self.processor)
        return status

    def _camera_devices(self) -> dict[str, Any]:
        selected = self.processor.config.camera.device
        devices = list_capture_devices(selected=selected)
        return {
            "ok": True,
            "devices": devices,
            "selected": selected,
            "source": self.processor.config.camera.source,
        }

    def _apply_scrcpy(self, body: dict[str, Any]) -> None:
        action = str(body.get("action") or "apply")
        save = bool(body.get("save"))
        fields = body.get("fields")
        if fields is None:
            fields = {
                key: body[key]
                for key in (
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
                )
                if key in body
            }
        if not isinstance(fields, dict):
            return self._send_json(
                {"ok": False, "error": "fields must be an object"}, status=400
            )
        try:
            result = self.processor.apply_scrcpy(fields, action=action, save=save)
        except Exception as exc:
            return self._send_json({"ok": False, "error": str(exc)}, status=400)
        # Always 200 when the config mutation ran: a scrcpy spawn failure should
        # still leave enabled/zoom/pan reflected in the wizard, not roll the UI back.
        self._send_json(result, status=200)

    def _apply_camera_source(self, body: dict[str, Any]) -> None:
        save = bool(body.get("save"))
        fields = {
            key: body[key]
            for key in (
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
            )
            if key in body
        }
        # Never persist a redacted credential placeholder from the browser.
        url = fields.get("rtsp_url")
        if isinstance(url, str) and ("***" in url or "…" in url or "..." in url):
            fields.pop("rtsp_url", None)
        if not fields:
            return self._send_json(
                {"ok": False, "error": "source fields required"}, status=400
            )
        try:
            result = self.processor.apply_camera_source(fields, save=save)
        except Exception as exc:
            return self._send_json({"ok": False, "error": str(exc)}, status=400)
        config = result.get("config") or {}
        url = config.get("camera", {}).get("rtsp_url", "")
        if url:
            config["camera"]["rtsp_url"] = redact_url(url)
        devices = list_capture_devices(selected=self.processor.config.camera.device)
        self._send_json(
            {
                "ok": True,
                "config": config,
                "saved": result.get("saved"),
                "recreated": result.get("recreated"),
                "devices": devices,
            }
        )

    def _update_config(self, body: dict[str, Any]) -> None:
        updates = body.get("updates")
        if not isinstance(updates, dict) or not updates:
            return self._send_json({"ok": False, "error": "no updates given"}, status=400)
        try:
            config = self.processor.update_config(updates)
        except Exception as exc:
            return self._send_json({"ok": False, "error": str(exc)}, status=400)
        url = config.get("camera", {}).get("rtsp_url", "")
        if url:
            config["camera"]["rtsp_url"] = redact_url(url)
        self._send_json({"ok": True, "config": config})

    def _set_corners(self, body: dict[str, Any]) -> None:
        corners = body.get("corners")
        if corners is not None:
            if (
                not isinstance(corners, list)
                or len(corners) != 4
                or any(not isinstance(p, list) or len(p) != 2 for p in corners)
            ):
                return self._send_json(
                    {"ok": False, "error": "corners must be four [x, y] pairs"}, status=400
                )
            corners = [[float(x), float(y)] for x, y in corners]
        result = self.processor.set_manual_corners(corners)
        self._send_json({"ok": True, **result})

    def _toggle_stage(self, body: dict[str, Any]) -> None:
        name = body.get("name")
        if not isinstance(name, str):
            return self._send_json({"ok": False, "error": "name is required"}, status=400)
        enabled = body.get("enabled")
        processor = self.processor

        def run_toggle() -> tuple[bool, bool]:
            stage = processor.pipeline.get(name)
            if stage is None:
                return False, False
            if enabled is None:
                state = stage.toggle()
            else:
                stage.enabled = bool(enabled)
                state = stage.enabled

            # Persist into config so a later slider update does not revive the
            # stage via apply_config(section.enabled).
            section = getattr(processor.config, name, None)
            if section is not None and hasattr(section, "enabled"):
                section.enabled = state
            return True, state

        found, state = processor.call(run_toggle)
        if not found:
            return self._send_json({"ok": False, "error": f"unknown stage {name}"}, status=404)
        self._send_json({"ok": True, "name": name, "enabled": state})

    def _serve_stream(self, view: str) -> None:
        view = view.strip("/") or "output"
        if view not in self.processor.available_views():
            return self.send_error(404, f"unknown view {view}")
        config = self.processor.config.web
        broker = self.processor.brokers.get(view)
        write_mjpeg_stream(
            self,
            broker,
            fps=config.stream_fps,
            quality=config.stream_quality,
            max_width=config.stream_max_width,
        )

    def _serve_snapshot(self, view: str) -> None:
        image = self.processor.call(lambda: self.processor.snapshot(view), timeout=3.0)
        if image is None:
            return self._send_json({"ok": False, "error": "no frame available"}, status=503)
        ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            return self._send_json({"ok": False, "error": "encode failed"}, status=500)
        self._send_bytes(buffer.tobytes(), "image/jpeg")

    def _serve_static(self, relative: str) -> None:
        # Resolve inside the static directory only; a path like
        # "../../etc/passwd" must not escape it.
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return self.send_error(403, "forbidden")
        if not target.is_file():
            return self.send_error(404, "not found")

        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._send_bytes(target.read_bytes(), content_type)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("web %s - %s", self.address_string(), fmt % args)


class CalibrationServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, processor: Processor):
        super().__init__(address, _Handler)
        self.processor = processor


def start_web_server(processor: Processor) -> tuple[CalibrationServer, threading.Thread]:
    config = processor.config.web
    server = CalibrationServer((config.host, config.port), processor)
    thread = threading.Thread(target=server.serve_forever, name="web-server", daemon=True)
    thread.start()

    host = "localhost" if config.host in ("0.0.0.0", "") else config.host
    log.info("Calibration wizard: http://%s:%d/", host, config.port)
    if config.host == "0.0.0.0":
        log.warning(
            "web.host is 0.0.0.0 -- the tuner is reachable from the whole network "
            "and has no authentication"
        )
    return server, thread
