"""Fixed inset crop.

Runs on the rectified image, so trimming "2 %" removes 2 % of the TV panel
regardless of how off-axis the camera is.  The point is to lose the bezel, the
sliver of wall that inevitably sneaks into the corners, and the bright rim
where the panel's own edge glows.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from processor.config.schema import CropConfig
from processor.pipeline.context import FrameContext, PipelineState
from processor.pipeline.stage import Stage


def resolve_insets(config: CropConfig) -> tuple[float, float, float, float]:
    """Per-edge inset fractions, falling back to the uniform percentage."""
    default = max(0.0, config.inset_percent) / 100.0
    edges = []
    for value in (config.inset.top, config.inset.bottom, config.inset.left, config.inset.right):
        edges.append(default if value < 0 else max(0.0, value) / 100.0)
    return tuple(min(v, 0.45) for v in edges)  # type: ignore[return-value]


class CropStage(Stage):
    name = "crop"

    def __init__(self, config: CropConfig, state: PipelineState):
        super().__init__(config, state)
        self.config: CropConfig = config
        self._last: tuple[int, int, int, int] = (0, 0, 0, 0)

    def status(self) -> dict[str, Any]:
        top, bottom, left, right = resolve_insets(self.config)
        return {
            "enabled": self.enabled,
            "inset_percent": self.config.inset_percent,
            "effective": {
                "top": round(top * 100, 2),
                "bottom": round(bottom * 100, 2),
                "left": round(left * 100, 2),
                "right": round(right * 100, 2),
            },
            "last_pixels": list(self._last),
        }

    def process(self, ctx: FrameContext) -> None:
        image = ctx.image
        height, width = image.shape[:2]
        top_f, bottom_f, left_f, right_f = resolve_insets(self.config)

        top = int(round(height * top_f))
        bottom = int(round(height * bottom_f))
        left = int(round(width * left_f))
        right = int(round(width * right_f))

        y0, y1 = top, height - bottom
        x0, x1 = left, width - right
        if y1 - y0 < 8 or x1 - x0 < 8:
            ctx.skipped[self.name] = "inset too large"
            return

        self._last = (top, bottom, left, right)
        # A slice is a view; nothing is copied until something needs it to be
        # contiguous, which is exactly the behaviour we want on a slow CPU.
        ctx.set_image(image[y0:y1, x0:x1])
        ctx.record(
            self.name,
            pixels={"top": top, "bottom": bottom, "left": left, "right": right},
            size=[x1 - x0, y1 - y0],
        )

    def debug_view(self, ctx: FrameContext) -> np.ndarray | None:
        image = ctx.debug_images.get("perspective")
        if image is None:
            return None
        canvas = image.copy()
        top, bottom, left, right = self._last
        height, width = canvas.shape[:2]
        cv2.rectangle(
            canvas, (left, top), (width - right - 1, height - bottom - 1), (0, 200, 255), 2
        )
        return canvas
