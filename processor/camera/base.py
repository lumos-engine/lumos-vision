"""Frame source interface."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class Frame:
    """One decoded image plus the metadata the pipeline cares about."""

    image: np.ndarray  # BGR uint8
    index: int = 0
    #: monotonic timestamp of when the frame was pulled from the decoder --
    #: the basis for the end-to-end latency figure.
    captured_at: float = field(default_factory=time.monotonic)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])

    @property
    def age(self) -> float:
        return time.monotonic() - self.captured_at


class FrameSource(ABC):
    """A source of frames.

    Implementations must never block indefinitely in :meth:`read` and must
    never queue frames up: when the consumer is slower than the source, old
    frames are dropped, not buffered.  Accumulated latency is the one failure
    mode an ambient light system cannot tolerate.
    """

    name: str = "source"

    @abstractmethod
    def start(self) -> "FrameSource":
        """Begin producing frames.  Must be non-blocking."""

    @abstractmethod
    def read(self, timeout: float = 1.0) -> Frame | None:
        """Return the newest unseen frame, or ``None`` if none arrived in time."""

    @abstractmethod
    def stop(self) -> None:
        """Release the underlying device/socket."""

    @property
    def is_connected(self) -> bool:
        return True

    @property
    def stats(self) -> dict[str, Any]:
        return {}

    def __enter__(self) -> "FrameSource":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()
