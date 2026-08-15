"""Sink interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable

import numpy as np

from processor.utils.logging import get_logger

log = get_logger(__name__)


class Sink(ABC):
    name: str = "sink"

    @abstractmethod
    def open(self, width: int, height: int) -> None:
        """Prepare for frames of the given size."""

    @abstractmethod
    def write(self, image: np.ndarray) -> bool:
        """Consume one BGR frame.  Returns False if the sink is broken."""

    def close(self) -> None:
        return None

    @property
    def stats(self) -> dict[str, Any]:
        return {}

    def __enter__(self) -> "Sink":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class SinkGroup(Sink):
    """Fan a frame out to several sinks, isolating failures.

    One dead sink -- an MJPEG client that vanished, a full disk -- must never
    stop the virtual camera from being fed.
    """

    name = "group"

    def __init__(self, sinks: Iterable[Sink]):
        self.sinks: list[Sink] = list(sinks)
        self._failed: set[str] = set()

    def open(self, width: int, height: int) -> None:
        alive: list[Sink] = []
        for sink in self.sinks:
            try:
                sink.open(width, height)
                alive.append(sink)
            except Exception as exc:
                log.error("Sink %s failed to open: %s", sink.name, exc)
        self.sinks = alive

    def write(self, image: np.ndarray) -> bool:
        for sink in self.sinks:
            try:
                ok = sink.write(image)
                if ok:
                    self._failed.discard(sink.name)
                elif sink.name not in self._failed:
                    self._failed.add(sink.name)
                    log.warning("Sink %s stopped accepting frames", sink.name)
            except Exception as exc:
                if sink.name not in self._failed:
                    self._failed.add(sink.name)
                    log.error("Sink %s write failed: %s", sink.name, exc)
        return True

    def close(self) -> None:
        for sink in self.sinks:
            try:
                sink.close()
            except Exception:
                log.exception("Sink %s failed to close", sink.name)

    @property
    def stats(self) -> dict[str, Any]:
        return {sink.name: sink.stats for sink in self.sinks}


class NullSink(Sink):
    """Discards frames.  Useful for benchmarking the pipeline in isolation."""

    name = "null"

    def __init__(self) -> None:
        self.frames = 0

    def open(self, width: int, height: int) -> None:
        return None

    def write(self, image: np.ndarray) -> bool:
        self.frames += 1
        return True

    @property
    def stats(self) -> dict[str, Any]:
        return {"frames": self.frames}
