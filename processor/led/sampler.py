"""Sample border colours for an LED strip running around the TV."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from processor.utils.geometry import UNIT_SQUARE, compose_panel_insets, inset_quad, order_corners

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


def panel_insets_from_meta(meta: dict[str, Any] | None) -> tuple[float, float, float, float]:
    """Compose crop + black-bar + reflection insets in panel UV (t, b, l, r)."""
    meta = meta or {}
    crop = (meta.get("crop") or {}).get("fractions") or {}
    bars_node = meta.get("blackbars") or {}
    bars = bars_node.get("applied_fractions")
    if not bars:
        percents = bars_node.get("applied_percent") or {}
        bars = {edge: float(percents.get(edge, 0.0)) / 100.0 for edge in ("top", "bottom", "left", "right")}
    reflection = float((meta.get("reflection") or {}).get("margin_fraction") or 0.0)
    return compose_panel_insets(
        (
            float(crop.get("top", 0.0)),
            float(crop.get("bottom", 0.0)),
            float(crop.get("left", 0.0)),
            float(crop.get("right", 0.0)),
        ),
        (
            float(bars.get("top", 0.0)),
            float(bars.get("bottom", 0.0)),
            float(bars.get("left", 0.0)),
            float(bars.get("right", 0.0)),
        ),
        reflection,
    )


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
        return self._finish(chain)

    def sample_quad(
        self,
        image_bgr: np.ndarray,
        corners: np.ndarray,
        insets: tuple[float, float, float, float] | None = None,
        *,
        black_level: tuple[float, float, float] | None = None,
        matrix: np.ndarray | None = None,
        lut: np.ndarray | None = None,
        saturation: float = 1.0,
    ) -> np.ndarray:
        """Sample LED colours from a perspective TV quad in the camera frame.

        ``insets`` are panel-UV ``(top, bottom, left, right)`` fractions already
        composed from crop + bars + reflection. Spacing is uniform in that UV,
        so foreshortening on an off-axis camera is handled by the homography.
        Colour correction runs on the N samples, not the camera frame.
        """
        layout = self.layout
        if layout.count == 0:
            return np.zeros((0, 3), dtype=np.uint8)

        content = order_corners(corners)
        if insets is not None:
            top, bottom, left, right = insets
            if max(top, bottom, left, right) > 1e-6:
                content = inset_quad(content, left, top, right, bottom)

        homography = cv2.getPerspectiveTransform(
            UNIT_SQUARE, np.asarray(content, dtype=np.float32).reshape(4, 2)
        )
        depth = float(np.clip(layout.depth, 0.01, 0.45))
        n_depth = max(2, int(round(depth * 100.0)))

        top = self._remap_edge(image_bgr, homography, layout.top, depth, n_depth, "top")
        right = self._remap_edge(image_bgr, homography, layout.right, depth, n_depth, "right")
        bottom = self._remap_edge(image_bgr, homography, layout.bottom, depth, n_depth, "bottom")
        left = self._remap_edge(image_bgr, homography, layout.left, depth, n_depth, "left")
        chain = np.concatenate([top, right, bottom, left], axis=0)
        if (
            black_level is not None
            or matrix is not None
            or lut is not None
            or abs(float(saturation) - 1.0) > 1e-3
        ):
            from processor.stages.color import apply_colour_bgr

            chain = apply_colour_bgr(
                chain,
                black_level=black_level,
                matrix=matrix,
                lut=lut,
                saturation=saturation,
            )
        return self._finish(chain)

    def _remap_edge(
        self,
        image: np.ndarray,
        homography: np.ndarray,
        count: int,
        depth: float,
        n_depth: int,
        edge: str,
    ) -> np.ndarray:
        if count <= 0:
            return np.zeros((0, 3), dtype=np.uint8)
        if edge == "top":
            u = np.linspace(0.0, 1.0, count, dtype=np.float32)
            v = np.linspace(0.0, depth, n_depth, dtype=np.float32)
            uu = np.broadcast_to(u[None, :], (n_depth, count))
            vv = np.broadcast_to(v[:, None], (n_depth, count))
        elif edge == "right":
            u = np.linspace(1.0 - depth, 1.0, n_depth, dtype=np.float32)
            v = np.linspace(0.0, 1.0, count, dtype=np.float32)
            uu = np.broadcast_to(u[:, None], (n_depth, count))
            vv = np.broadcast_to(v[None, :], (n_depth, count))
        elif edge == "bottom":
            u = np.linspace(1.0, 0.0, count, dtype=np.float32)
            v = np.linspace(1.0 - depth, 1.0, n_depth, dtype=np.float32)
            uu = np.broadcast_to(u[None, :], (n_depth, count))
            vv = np.broadcast_to(v[:, None], (n_depth, count))
        else:
            u = np.linspace(0.0, depth, n_depth, dtype=np.float32)
            v = np.linspace(1.0, 0.0, count, dtype=np.float32)
            uu = np.broadcast_to(u[:, None], (n_depth, count))
            vv = np.broadcast_to(v[None, :], (n_depth, count))

        pts = np.stack(
            [np.ascontiguousarray(uu).ravel(), np.ascontiguousarray(vv).ravel()],
            axis=-1,
        ).reshape(-1, 1, 2)
        mapped = cv2.perspectiveTransform(pts, homography)
        map_x = mapped[:, 0, 0].reshape(n_depth, count).astype(np.float32)
        map_y = mapped[:, 0, 1].reshape(n_depth, count).astype(np.float32)
        strip = cv2.remap(
            image,
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )
        return strip.mean(axis=0)

    def _finish(self, chain_bgr: np.ndarray) -> np.ndarray:
        layout = self.layout
        if chain_bgr.size == 0:
            return np.zeros((0, 3), dtype=np.uint8)

        if not layout.clockwise:
            chain_bgr = chain_bgr[::-1]

        offset = self._start_offset()
        if offset:
            chain_bgr = np.roll(chain_bgr, -offset, axis=0)

        rgb = np.clip(chain_bgr[:, ::-1], 0, 255).astype(np.uint8)

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
