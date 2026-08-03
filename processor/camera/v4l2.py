"""USB / V4L2 webcam input.

Same drop-old rule as the RTSP source: a reader thread keeps one frame, the
pipeline always gets the newest, and nothing queues up behind a slow stage.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import cv2
import numpy as np

from processor.camera.base import Frame, FrameSource
from processor.config.schema import CameraConfig
from processor.utils.logging import get_logger

log = get_logger(__name__)


def resolve_device(device: str) -> str | int:
    """Accept ``/dev/video2`` or a bare index ``2``."""
    text = (device or "").strip()
    if not text:
        raise ValueError("camera.device is required for the v4l2 source")
    if text.isdigit():
        return int(text)
    return text


class V4l2Source(FrameSource):
    name = "v4l2"

    def __init__(self, config: CameraConfig):
        self.config = config
        self.device = resolve_device(config.device)

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

    def start(self) -> "V4l2Source":
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="v4l2-reader", daemon=True)
        self._thread.start()
        log.info("V4L2 source started: %s", self.device)
        return self

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)
        self._release()
        log.info("V4L2 source stopped")

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
            "device": str(self.device),
        }

    def _run(self) -> None:
        delay = self.config.reconnect_delay
        while not self._stop.is_set():
            if not self._open():
                self._last_error = "open failed"
                self._sleep(delay)
                delay = min(delay * 1.6, self.config.max_reconnect_delay)
                self._reconnects += 1
                continue

            delay = self.config.reconnect_delay
            self._connected = True
            log.info("V4L2 connected: %s", self.device)

            try:
                self._pump()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.warning("V4L2 reader error: %s", self._last_error)

            self._connected = False
            self._release()
            if not self._stop.is_set():
                self._reconnects += 1
                log.warning("V4L2 disconnected, retrying in %.1fs", delay)
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
                time.sleep(0.005)
                continue

            last_ok = now
            self._last_frame_at = now
            self._publish(image, now)

    def _publish(self, image: np.ndarray, now: float) -> None:
        image = self._downscale(image)
        with self._lock:
            if not self._latest_consumed:
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

    def _open(self) -> bool:
        self._release()
        capture = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not capture.isOpened():
            # Some builds only accept the index form.
            if isinstance(self.device, str) and self.device.startswith("/dev/video"):
                try:
                    index = int(self.device.rsplit("video", 1)[1])
                except ValueError:
                    index = None
                if index is not None:
                    capture.release()
                    capture = cv2.VideoCapture(index, cv2.CAP_V4L2)
        if not capture.isOpened():
            capture.release()
            log.warning("Failed to open V4L2 device %s", self.device)
            return False

        try:
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except cv2.error:
            pass

        # Prefer MJPEG when available: USB webcams usually deliver higher
        # resolution/fps that way than raw YUYV.
        try:
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        except cv2.error:
            pass

        width = int(self.config.capture_width or 0)
        height = int(self.config.capture_height or 0)
        if width > 0:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height > 0:
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if self.config.capture_fps > 0:
            capture.set(cv2.CAP_PROP_FPS, self.config.capture_fps)

        self._capture = capture
        got_w = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        got_h = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        got_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        log.info("V4L2 mode: %dx%d @ %.1f fps", got_w, got_h, got_fps)
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
