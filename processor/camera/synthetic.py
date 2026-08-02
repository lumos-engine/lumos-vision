"""A camera source that renders a fake living room.  No hardware required."""

from __future__ import annotations

import time
from typing import Any

from processor.camera.base import Frame, FrameSource
from processor.config.schema import CameraConfig
from processor.testing.scene import SceneParams, SyntheticScene


class SyntheticSource(FrameSource):
    name = "synthetic"

    def __init__(self, config: CameraConfig, params: SceneParams | None = None):
        self.config = config
        self.scene = SyntheticScene(params)
        self.fps = config.replay_fps if config.replay_fps > 0 else 15.0
        self._index = 0
        self._start = 0.0
        self._next_due = 0.0

    def start(self) -> "SyntheticSource":
        self._start = time.monotonic()
        self._next_due = self._start
        return self

    def read(self, timeout: float = 1.0) -> Frame | None:
        interval = 1.0 / self.fps if self.fps > 0 else 0.0
        if interval > 0:
            wait = self._next_due - time.monotonic()
            if wait > 0:
                time.sleep(min(wait, timeout))
            self._next_due = max(self._next_due + interval, time.monotonic() - interval)

        now = time.monotonic()
        self._index += 1
        return Frame(
            image=self.scene.frame(now - self._start),
            index=self._index,
            captured_at=now,
        )

    def stop(self) -> None:
        return None

    @property
    def stats(self) -> dict[str, Any]:
        return {"connected": True, "frames": self._index, "scene": "synthetic"}
