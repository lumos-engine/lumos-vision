"""Replay sources so the whole system can be developed without a camera."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from processor.camera.base import Frame, FrameSource
from processor.config.schema import CameraConfig
from processor.utils.logging import get_logger

log = get_logger(__name__)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}


def _downscale(image: np.ndarray, target_width: int) -> np.ndarray:
    if target_width <= 0 or image.shape[1] <= target_width:
        return image
    scale = target_width / float(image.shape[1])
    size = (target_width, max(1, int(round(image.shape[0] * scale))))
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)


class FileSource(FrameSource):
    """Replay a recorded video file.

    Reads synchronously -- there is no network to fall behind on, and pacing
    the file to a fixed rate makes the temporal stages (black bars, movement
    detection) behave the way they will against a live camera.
    """

    name = "file"

    def __init__(self, config: CameraConfig):
        self.config = config
        self.path = Path(config.path).expanduser()
        if not self.path.exists():
            raise FileNotFoundError(f"camera.path does not exist: {self.path}")

        self._capture: cv2.VideoCapture | None = None
        self._index = 0
        self._loops = 0
        self._next_due = 0.0
        self._native_fps = 0.0
        self._finished = False

    def start(self) -> "FileSource":
        self._open()
        return self

    def _open(self) -> None:
        if self._capture is not None:
            self._capture.release()
        capture = cv2.VideoCapture(str(self.path))
        if not capture.isOpened():
            raise RuntimeError(f"could not open video file: {self.path}")
        self._capture = capture
        self._native_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        self._next_due = 0.0
        log.info("Replaying %s (%.1f fps native)", self.path, self._native_fps)

    @property
    def _interval(self) -> float:
        if self.config.replay_fps > 0:
            return 1.0 / self.config.replay_fps
        if self._native_fps > 0:
            return 1.0 / self._native_fps
        return 0.0

    def read(self, timeout: float = 1.0) -> Frame | None:
        if self._capture is None or self._finished:
            return None

        interval = self._interval
        if interval > 0:
            now = time.monotonic()
            if self._next_due == 0.0:
                self._next_due = now
            wait = self._next_due - now
            if wait > 0:
                time.sleep(min(wait, timeout))
            self._next_due = max(self._next_due + interval, time.monotonic() - interval)

        ok, image = self._capture.read()
        if not ok or image is None:
            if self.config.loop:
                self._loops += 1
                self._capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, image = self._capture.read()
            if not ok or image is None:
                self._finished = True
                return None

        self._index += 1
        return Frame(
            image=_downscale(image, self.config.process_width),
            index=self._index,
            captured_at=time.monotonic(),
        )

    def stop(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    @property
    def is_connected(self) -> bool:
        return self._capture is not None and not self._finished

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "connected": self.is_connected,
            "frames": self._index,
            "loops": self._loops,
            "path": str(self.path),
        }


class ImageSource(FrameSource):
    """Serve a still image, or cycle through a directory of them.

    Handy for unit-testing detection against a handful of tricky stills.
    """

    name = "image"

    def __init__(self, config: CameraConfig, fps: float = 10.0):
        self.config = config
        path = Path(config.path).expanduser()
        if path.is_dir():
            self.paths = sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
            if not self.paths:
                raise FileNotFoundError(f"no images found in {path}")
        elif path.exists():
            self.paths = [path]
        else:
            raise FileNotFoundError(f"camera.path does not exist: {path}")

        self.fps = config.replay_fps if config.replay_fps > 0 else fps
        self._images: list[np.ndarray] = []
        self._index = 0
        self._cursor = 0
        self._next_due = 0.0

    def start(self) -> "ImageSource":
        for path in self.paths:
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                log.warning("Skipping unreadable image: %s", path)
                continue
            self._images.append(_downscale(image, self.config.process_width))
        if not self._images:
            raise RuntimeError("no readable images in the configured path")
        log.info("Image source ready: %d image(s)", len(self._images))
        return self

    def read(self, timeout: float = 1.0) -> Frame | None:
        if not self._images:
            return None

        interval = 1.0 / self.fps if self.fps > 0 else 0.0
        if interval > 0:
            now = time.monotonic()
            if self._next_due == 0.0:
                self._next_due = now
            wait = self._next_due - now
            if wait > 0:
                time.sleep(min(wait, timeout))
            self._next_due = max(self._next_due + interval, time.monotonic() - interval)

        image = self._images[self._cursor]
        if len(self._images) > 1:
            self._cursor = (self._cursor + 1) % len(self._images)
        self._index += 1
        # Copy: stages are allowed to write in place, and the cached original
        # has to survive for the next lap around the directory.
        return Frame(image=image.copy(), index=self._index, captured_at=time.monotonic())

    def stop(self) -> None:
        self._images.clear()

    @property
    def stats(self) -> dict[str, Any]:
        return {"connected": True, "frames": self._index, "images": len(self._images)}
