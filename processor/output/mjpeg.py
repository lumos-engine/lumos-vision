"""MJPEG over HTTP.

Serves as a development stand-in for the virtual camera (works on any OS, view
it in a browser or with VLC) and as the transport for the calibration wizard's
live previews.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import cv2
import numpy as np

from processor.config.schema import MjpegConfig
from processor.output.base import Sink
from processor.output.broker import FrameBroker
from processor.utils.logging import get_logger

log = get_logger(__name__)

BOUNDARY = "screensightframe"


def encode_jpeg(image: np.ndarray, quality: int = 70) -> bytes | None:
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buffer.tobytes() if ok else None


def write_mjpeg_stream(
    handler: BaseHTTPRequestHandler,
    broker: FrameBroker,
    fps: float = 10.0,
    quality: int = 70,
    max_width: int = 0,
) -> None:
    """Stream a broker's frames to an HTTP client until it disconnects."""
    handler.send_response(200)
    handler.send_header("Age", "0")
    handler.send_header("Cache-Control", "no-cache, private")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
    handler.end_headers()

    interval = 1.0 / fps if fps > 0 else 0.0
    broker.subscribe()
    sequence = 0
    try:
        while True:
            item = broker.wait(sequence, timeout=2.0)
            if item is None:
                # Keep the connection alive through a stalled camera so the
                # browser does not have to reconnect when frames resume.
                if not _write_chunk(handler, b""):
                    return
                continue

            sequence, image = item
            if max_width and image.shape[1] > max_width:
                scale = max_width / float(image.shape[1])
                image = cv2.resize(
                    image,
                    (max_width, max(1, int(round(image.shape[0] * scale)))),
                    interpolation=cv2.INTER_AREA,
                )

            payload = encode_jpeg(image, quality)
            if payload is None:
                continue
            if not _write_chunk(handler, payload):
                return
            if interval:
                time.sleep(interval)
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        broker.unsubscribe()


def _write_chunk(handler: BaseHTTPRequestHandler, payload: bytes) -> bool:
    try:
        if not payload:
            handler.wfile.write(f"--{BOUNDARY}\r\n".encode())
            handler.wfile.write(b"Content-Type: text/plain\r\nContent-Length: 0\r\n\r\n")
            handler.wfile.flush()
            return True
        handler.wfile.write(f"--{BOUNDARY}\r\n".encode())
        handler.wfile.write(b"Content-Type: image/jpeg\r\n")
        handler.wfile.write(f"Content-Length: {len(payload)}\r\n\r\n".encode())
        handler.wfile.write(payload)
        handler.wfile.write(b"\r\n")
        handler.wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
        return False


class _MjpegHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ScreenSight/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        server: "MjpegServer" = self.server  # type: ignore[assignment]
        if self.path in ("/", "/index.html"):
            body = (
                "<!doctype html><meta charset=utf-8>"
                "<title>Screen Sight</title>"
                "<style>body{margin:0;background:#111;display:grid;place-items:center;"
                "height:100vh}img{max-width:100%;image-rendering:pixelated}</style>"
                '<img src="/stream.mjpg">'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/stream"):
            write_mjpeg_stream(self, server.broker, server.fps, server.quality)
            return

        self.send_error(404)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("mjpeg %s - %s", self.address_string(), fmt % args)


class MjpegServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, broker: FrameBroker, fps: float, quality: int):
        super().__init__(address, _MjpegHandler)
        self.broker = broker
        self.fps = fps
        self.quality = quality

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class MjpegSink(Sink):
    name = "mjpeg"

    def __init__(self, config: MjpegConfig, fps: float = 15.0):
        self.config = config
        self.fps = fps
        self.broker = FrameBroker("mjpeg")
        self._server: MjpegServer | None = None
        self._thread: threading.Thread | None = None
        self._frames = 0

    def open(self, width: int, height: int) -> None:
        self._server = MjpegServer(
            (self.config.host, self.config.port),
            self.broker,
            self.fps,
            self.config.quality,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="mjpeg-server", daemon=True
        )
        self._thread.start()
        log.info(
            "MJPEG output on http://%s:%d/ (%dx%d)",
            self.config.host,
            self.config.port,
            width,
            height,
        )

    def write(self, image: np.ndarray, ctx: Any | None = None) -> bool:
        self._frames += 1
        # Skip the copy when nobody is watching -- this sink is usually idle.
        if self.broker.has_subscribers:
            self.broker.publish(image)
        return True

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "url": f"http://{self.config.host}:{self.config.port}/",
            "frames": self._frames,
            "clients": self.broker.subscribers,
        }
