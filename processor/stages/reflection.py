"""Reflection rejection.

Two related jobs, both about refusing to trust the edges of the picture:

* A small margin is trimmed from every side.  Wall reflections and bezel bleed
  concentrate right at the border, and that border is precisely what an
  ambient light system samples, so a couple of percent of inset buys a
  disproportionate improvement.
* Optional rectangular exclusions neutralise things that never change -- a
  channel logo, a permanent clock -- by flooding them with the surrounding
  colour so they stop dragging one LED toward the same hue all evening.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from processor.config.schema import ReflectionConfig
from processor.pipeline.context import FrameContext, PipelineState
from processor.pipeline.stage import Stage


class ReflectionStage(Stage):
    name = "reflection"

    def __init__(self, config: ReflectionConfig, state: PipelineState):
        super().__init__(config, state)
        self.config: ReflectionConfig = config
        self._margin_px = 0

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "margin_percent": self.config.margin_percent,
            "margin_px": self._margin_px,
            "exclusions": len(self.config.exclusions),
        }

    def process(self, ctx: FrameContext) -> None:
        image = ctx.image
        height, width = image.shape[:2]

        if self.config.exclusions:
            image = self._apply_exclusions(image)

        margin = max(0.0, min(self.config.margin_percent, 20.0)) / 100.0
        dx = int(round(width * margin))
        dy = int(round(height * margin))
        self._margin_px = max(dx, dy)

        if width - 2 * dx < 8 or height - 2 * dy < 8:
            ctx.skipped[self.name] = "margin too large"
            ctx.set_image(image)
            return

        if dx or dy:
            image = image[dy : height - dy, dx : width - dx]

        ctx.set_image(image)
        ctx.record(
            self.name,
            margin_px={"x": dx, "y": dy},
            size=[image.shape[1], image.shape[0]],
            exclusions=len(self.config.exclusions),
        )

    def _apply_exclusions(self, image: np.ndarray) -> np.ndarray:
        """Flood each excluded rect with the colour of the ring around it."""
        height, width = image.shape[:2]
        out = image.copy()

        for rect in self.config.exclusions:
            if len(rect) != 4:
                continue
            nx, ny, nw, nh = (float(v) for v in rect)
            x0 = int(np.clip(nx * width, 0, width - 1))
            y0 = int(np.clip(ny * height, 0, height - 1))
            x1 = int(np.clip((nx + nw) * width, x0 + 1, width))
            y1 = int(np.clip((ny + nh) * height, y0 + 1, height))

            pad_x = max(2, (x1 - x0) // 4)
            pad_y = max(2, (y1 - y0) // 4)
            rx0 = max(0, x0 - pad_x)
            ry0 = max(0, y0 - pad_y)
            rx1 = min(width, x1 + pad_x)
            ry1 = min(height, y1 + pad_y)

            ring = image[ry0:ry1, rx0:rx1]
            mask = np.ones(ring.shape[:2], dtype=bool)
            mask[y0 - ry0 : y1 - ry0, x0 - rx0 : x1 - rx0] = False
            if not mask.any():
                continue
            out[y0:y1, x0:x1] = ring[mask].mean(axis=0).astype(np.uint8)

        return out

    def debug_view(self, ctx: FrameContext) -> np.ndarray | None:
        return None
