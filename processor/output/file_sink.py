"""Write the processed stream to a video file (for debugging and demos)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

from processor.config.schema import FileSinkConfig
from processor.output.base import Sink
from processor.utils.logging import get_logger

log = get_logger(__name__)


class FileSink(Sink):
    name = "file"

    def __init__(self, config: FileSinkConfig, fps: float = 15.0):
        self.config = config
        self.fps = fps if fps > 0 else 15.0
        self._writer: cv2.VideoWriter | None = None
        self._frames = 0
        self._size = (0, 0)

    def open(self, width: int, height: int) -> None:
        path = Path(self.config.path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        fourcc = cv2.VideoWriter_fourcc(*self.config.fourcc)
        writer = cv2.VideoWriter(str(path), fourcc, self.fps, (width, height))
        if not writer.isOpened():
            raise RuntimeError(f"could not open {path} for writing with fourcc {self.config.fourcc}")
        self._writer = writer
        self._size = (width, height)
        log.info("Recording output to %s (%dx%d @ %.1f fps)", path, width, height, self.fps)

    def write(self, image: np.ndarray, ctx: Any | None = None) -> bool:
        if self._writer is None:
            return False
        if (image.shape[1], image.shape[0]) != self._size:
            image = cv2.resize(image, self._size, interpolation=cv2.INTER_AREA)
        self._writer.write(image)
        self._frames += 1
        return True

    def close(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
            log.info("Wrote %d frames to %s", self._frames, self.config.path)

    @property
    def stats(self) -> dict[str, Any]:
        return {"path": self.config.path, "frames": self._frames}
