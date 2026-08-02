"""Sample border colours for an LED strip running around the TV."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

CORNERS = ("top-left", "top-right", "bottom-right", "bottom-left")


@dataclass
class LedLayout:
    """How many LEDs sit along each edge, and where the strip starts."""

    top: int = 0
    right: int = 0
    bottom: int = 0
    left: int = 0
    #: Fraction of the image sampled inward from each edge.
    depth: float = 0.08
    start_corner: str = "top-left"
    clockwise: bool = True

    @property
    def count(self) -> int:
        return self.top + self.right + self.bottom + self.left


def _edge_means(strip: np.ndarray, count: int, horizontal: bool) -> np.ndarray:
    """Split a border strip into ``count`` blocks and average each.

    ``cv2.resize`` with ``INTER_AREA`` is exactly a block mean, and it runs in
    C++, so this is far cheaper than slicing in a Python loop.
    """
    if count <= 0 or strip.size == 0:
        return np.zeros((0, 3), dtype=np.uint8)
    size = (count, 1) if horizontal else (1, count)
    reduced = cv2.resize(strip, size, interpolation=cv2.INTER_AREA)
    return reduced.reshape(-1, 3)


class LedSampler:
    """Turn a processed frame into one RGB triple per LED."""

    def __init__(self, layout: LedLayout, smoothing: float = 0.0):
        self.layout = layout
        self.smoothing = float(np.clip(smoothing, 0.0, 0.99))
        self._previous: np.ndarray | None = None

    def reset(self) -> None:
        self._previous = None

    def sample(self, image_bgr: np.ndarray) -> np.ndarray:
        """``(count, 3)`` uint8 RGB, ordered along the physical strip."""
        layout = self.layout
        if layout.count == 0:
            return np.zeros((0, 3), dtype=np.uint8)

        height, width = image_bgr.shape[:2]
        depth_y = max(1, int(round(height * layout.depth)))
        depth_x = max(1, int(round(width * layout.depth)))

        top = _edge_means(image_bgr[:depth_y, :], layout.top, horizontal=True)
        bottom = _edge_means(image_bgr[height - depth_y :, :], layout.bottom, horizontal=True)
        left = _edge_means(image_bgr[:, :depth_x], layout.left, horizontal=False)
        right = _edge_means(image_bgr[:, width - depth_x :], layout.right, horizontal=False)

        # Canonical order: clockwise starting at the top-left corner.
        chain = np.concatenate([top, right, bottom[::-1], left[::-1]], axis=0)

        if not layout.clockwise:
            chain = chain[::-1]

        offset = self._start_offset()
        if offset:
            chain = np.roll(chain, -offset, axis=0)

        rgb = chain[:, ::-1].astype(np.uint8)  # BGR -> RGB

        if self.smoothing > 0:
            if self._previous is None or self._previous.shape != rgb.shape:
                self._previous = rgb.astype(np.float32)
            else:
                self._previous = (
                    self.smoothing * self._previous + (1.0 - self.smoothing) * rgb
                )
            rgb = np.clip(self._previous, 0, 255).astype(np.uint8)

        return rgb

    def _start_offset(self) -> int:
        layout = self.layout
        try:
            index = CORNERS.index(layout.start_corner)
        except ValueError:
            return 0
        cumulative = [0, layout.top, layout.top + layout.right,
                      layout.top + layout.right + layout.bottom]
        return cumulative[index]
