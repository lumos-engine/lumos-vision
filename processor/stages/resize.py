"""Final resize to the output resolution."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from processor.config.schema import OutputConfig, ResizeConfig
from processor.pipeline.context import FrameContext, PipelineState
from processor.pipeline.stage import Stage


def fit_image(image: np.ndarray, width: int, height: int, mode: str = "stretch") -> np.ndarray:
    """Resize to exactly ``width x height`` using the requested fit mode."""
    src_h, src_w = image.shape[:2]
    if src_w == width and src_h == height:
        return np.ascontiguousarray(image)

    mode = (mode or "stretch").lower()

    if mode == "stretch":
        interpolation = cv2.INTER_AREA if (src_w > width or src_h > height) else cv2.INTER_LINEAR
        return cv2.resize(image, (width, height), interpolation=interpolation)

    scale = (
        min(width / src_w, height / src_h)
        if mode == "letterbox"
        else max(width / src_w, height / src_h)
    )
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    resized = cv2.resize(image, (new_w, new_h), interpolation=interpolation)

    if mode == "letterbox":
        canvas = np.zeros((height, width, image.shape[2]), dtype=image.dtype)
        y0 = (height - new_h) // 2
        x0 = (width - new_w) // 2
        canvas[y0 : y0 + new_h, x0 : x0 + new_w] = resized
        return canvas

    y0 = max(0, (new_h - height) // 2)
    x0 = max(0, (new_w - width) // 2)
    return np.ascontiguousarray(resized[y0 : y0 + height, x0 : x0 + width])


class ResizeStage(Stage):
    name = "resize"

    def __init__(self, config: ResizeConfig, state: PipelineState, output: OutputConfig):
        super().__init__(config, state)
        self.config: ResizeConfig = config
        self.output = output

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.config.mode,
            "size": [self.output.width, self.output.height],
        }

    def process(self, ctx: FrameContext) -> None:
        width = max(2, int(self.output.width))
        height = max(2, int(self.output.height))
        ctx.set_image(fit_image(ctx.image, width, height, self.config.mode))
        ctx.record(self.name, size=[width, height], mode=self.config.mode)
