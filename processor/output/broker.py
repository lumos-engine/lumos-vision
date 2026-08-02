"""Latest-frame distribution to any number of readers.

Same rule as the camera side: readers never queue up frames.  A slow browser
tab on the calibration wizard gets fewer frames, never stale ones, and never
slows down the pipeline thread.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np


class FrameBroker:
    """One writer, many readers, only the newest frame is retained."""

    def __init__(self, name: str = "broker"):
        self.name = name
        self._condition = threading.Condition()
        self._frame: np.ndarray | None = None
        self._sequence = 0
        self._subscribers = 0

    def publish(self, image: np.ndarray | None) -> None:
        if image is None:
            return
        with self._condition:
            self._frame = image
            self._sequence += 1
            self._condition.notify_all()

    def latest(self) -> tuple[int, np.ndarray | None]:
        with self._condition:
            return self._sequence, self._frame

    def wait(self, since: int, timeout: float = 1.0) -> tuple[int, np.ndarray] | None:
        """Block until a frame newer than ``since`` exists."""
        with self._condition:
            if self._sequence <= since:
                self._condition.wait(timeout)
            if self._frame is None or self._sequence <= since:
                return None
            return self._sequence, self._frame

    def subscribe(self) -> None:
        with self._condition:
            self._subscribers += 1

    def unsubscribe(self) -> None:
        with self._condition:
            self._subscribers = max(0, self._subscribers - 1)

    @property
    def subscribers(self) -> int:
        return self._subscribers

    @property
    def has_subscribers(self) -> bool:
        return self._subscribers > 0

    @property
    def stats(self) -> dict[str, Any]:
        return {"sequence": self._sequence, "subscribers": self._subscribers}


class BrokerHub:
    """Named brokers, created on first use.

    The web UI asks for ``/stream/perspective`` and the pipeline publishes to
    whatever names exist; nothing has to agree on the list up front.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._brokers: dict[str, FrameBroker] = {}

    def get(self, name: str) -> FrameBroker:
        with self._lock:
            broker = self._brokers.get(name)
            if broker is None:
                broker = FrameBroker(name)
                self._brokers[name] = broker
            return broker

    def publish(self, name: str, image: np.ndarray | None) -> None:
        self.get(name).publish(image)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._brokers)

    def any_subscribers(self) -> bool:
        with self._lock:
            return any(b.has_subscribers for b in self._brokers.values())

    def subscribed_names(self) -> set[str]:
        with self._lock:
            return {name for name, b in self._brokers.items() if b.has_subscribers}
