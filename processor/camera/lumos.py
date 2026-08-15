"""Lumos Cam frames from ffmpeg stdout (MJPEG), not v4l2loopback.

ffmpeg's v4l2 muxer used to write into ``/dev/video11``, but OpenCV often never
saw those buffers. Decoding into a pipe skips the loopback entirely.
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


class LumosPipeSource(FrameSource):
    name = "lumos"

    def __init__(self, manager: Any, config: CameraConfig):
        self._manager = manager
        self.config = config
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frame_ready = threading.Event()
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._latest_consumed = True
        self._connected = False
        self._frame_index = 0
        self._dropped = 0
        self._last_frame_at = 0.0
        self._last_error: str | None = None

    def start(self) -> "LumosPipeSource":
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lumos-pipe", daemon=True)
        self._thread.start()
        log.info("Lumos Cam pipe source started %s", self._manager.frame_size())
        return self

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)
        log.info("Lumos Cam pipe source stopped")

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
        return self._connected and bool(getattr(self._manager, "running", False))

    @property
    def stats(self) -> dict[str, Any]:
        width, height = self._manager.frame_size()
        return {
            "connected": self.is_connected,
            "frames": self._frame_index,
            "dropped": self._dropped,
            "last_frame_age": (
                round(time.monotonic() - self._last_frame_at, 3) if self._last_frame_at else None
            ),
            "last_error": self._last_error,
            "size": [width, height],
        }

    def _run(self) -> None:
        self._connected = True
        while not self._stop.is_set():
            if not getattr(self._manager, "running", False):
                self._last_error = "ffmpeg not running"
                self._connected = False
                self._stop.wait(0.2)
                continue
            self._connected = True
            image = self._manager.read_bgr(timeout=1.0)
            if image is None:
                continue
            now = time.monotonic()
            self._last_frame_at = now
            self._last_error = None
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
