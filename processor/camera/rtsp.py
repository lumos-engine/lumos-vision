"""RTSP input.

The decoder runs on its own thread and keeps exactly one frame in hand.  The
pipeline thread always gets the newest frame and never waits on the network;
if the pipeline is slower than the camera, intermediate frames are discarded
rather than queued.  That is the whole trick to keeping latency flat over
hours of runtime.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

import cv2
import numpy as np

from processor.camera.base import Frame, FrameSource
from processor.config.schema import CameraConfig
from processor.utils.logging import get_logger

log = get_logger(__name__)


def redact_url(url: str) -> str:
    """Strip credentials so URLs can be logged and shown in the web UI."""
    if not url:
        return ""
    try:
        parts = urlparse(url)
    except ValueError:
        return "<unparseable url>"
    if not parts.netloc or "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunparse(parts._replace(netloc=f"***:***@{host}"))


def _ffmpeg_capture_options(config: CameraConfig) -> str:
    """FFmpeg options tuned for latency over completeness.

    ``nobuffer`` + ``low_delay`` + a zero reorder queue tell FFmpeg to hand us
    frames as soon as they decode instead of building a smoothing buffer.
    """
    options = [
        f"rtsp_transport;{config.transport}",
        "fflags;nobuffer",
        "flags;low_delay",
        "reorder_queue_size;0",
        "max_delay;0",
        f"stimeout;{int(max(config.read_timeout, 1.0) * 1_000_000)}",
    ]
    if config.ffmpeg_options:
        options.append(config.ffmpeg_options.strip("|"))
    return "|".join(options)


class RtspSource(FrameSource):
    name = "rtsp"

    def __init__(self, config: CameraConfig):
        self.config = config
        self.url = config.rtsp_url
        if not self.url:
            raise ValueError("camera.rtsp_url is required for the rtsp source")

        self._capture: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frame_ready = threading.Event()
        self._lock = threading.Lock()

        self._latest: Frame | None = None
        self._latest_consumed = True
        self._connected = False

        self._frame_index = 0
        self._dropped = 0
        self._reconnects = 0
        self._last_frame_at = 0.0
        self._last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "RtspSource":
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rtsp-reader", daemon=True)
        self._thread.start()
        log.info("RTSP source started: %s (%s)", redact_url(self.url), self.config.transport)
        return self

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)
        self._release()
        log.info("RTSP source stopped")

    # -- consumer side -----------------------------------------------------

    def read(self, timeout: float = 1.0) -> Frame | None:
        if not self._frame_ready.wait(timeout=timeout):
            return None
        with self._lock:
            frame = self._latest
            self._latest_consumed = True
            self._frame_ready.clear()
        return frame

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "frames": self._frame_index,
            "dropped": self._dropped,
            "reconnects": self._reconnects,
            "last_frame_age": (
                round(time.monotonic() - self._last_frame_at, 3) if self._last_frame_at else None
            ),
            "last_error": self._last_error,
            "url": redact_url(self.url),
        }

    # -- reader thread -----------------------------------------------------

    def _run(self) -> None:
        delay = self.config.reconnect_delay
        while not self._stop.is_set():
            if not self._open():
                self._last_error = "connection failed"
                self._sleep(delay)
                delay = min(delay * 1.6, self.config.max_reconnect_delay)
                self._reconnects += 1
                continue

            delay = self.config.reconnect_delay
            self._connected = True
            log.info("RTSP connected: %s", redact_url(self.url))

            try:
                self._pump()
            except Exception as exc:  # a decoder blow-up must not kill the app
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.warning("RTSP reader error: %s", self._last_error)

            self._connected = False
            self._release()
            if not self._stop.is_set():
                self._reconnects += 1
                log.warning("RTSP disconnected, reconnecting in %.1fs", delay)
                self._sleep(delay)
                delay = min(delay * 1.6, self.config.max_reconnect_delay)

    def _pump(self) -> None:
        capture = self._capture
        assert capture is not None
        last_ok = time.monotonic()

        while not self._stop.is_set():
            ok, image = capture.read()
            now = time.monotonic()

            if not ok or image is None:
                if now - last_ok > self.config.read_timeout:
                    self._last_error = f"no frames for {self.config.read_timeout:.0f}s"
                    return
                # Short hiccup: back off a touch so we do not spin the CPU.
                time.sleep(0.005)
                continue

            last_ok = now
            self._last_frame_at = now
            self._publish(image, now)

    def _publish(self, image: np.ndarray, now: float) -> None:
        image = self._downscale(image)
        with self._lock:
            if not self._latest_consumed:
                # The pipeline has not picked up the previous frame yet, so it
                # is already stale.  Overwrite it: newest frame always wins.
                self._dropped += 1
            self._frame_index += 1
            self._latest = Frame(image=image, index=self._frame_index, captured_at=now)
            self._latest_consumed = False
            self._frame_ready.set()

    def _downscale(self, image: np.ndarray) -> np.ndarray:
        target = self.config.process_width
        if target <= 0 or image.shape[1] <= target:
            return image
        scale = target / float(image.shape[1])
        size = (target, max(1, int(round(image.shape[0] * scale))))
        return cv2.resize(image, size, interpolation=cv2.INTER_AREA)

    # -- capture handling --------------------------------------------------

    def _open(self) -> bool:
        self._release()
        previous = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = _ffmpeg_capture_options(self.config)
        try:
            capture = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        except Exception as exc:
            log.warning("Failed to open RTSP stream: %s", exc)
            capture = None
        finally:
            if previous is None:
                os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
            else:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = previous

        if capture is None or not capture.isOpened():
            if capture is not None:
                capture.release()
            return False

        # Belt and braces: even with nobuffer, some backends keep a queue.
        try:
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except cv2.error:
            pass

        self._capture = capture
        return True

    def _release(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(timeout=max(0.0, seconds))
