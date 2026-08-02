"""Perspective correction: map the TV quad onto a true rectangle."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from processor.config.schema import PerspectiveConfig
from processor.pipeline.context import FrameContext, PipelineState
from processor.pipeline.stage import Stage
from processor.utils.geometry import homography_to_rect
from processor.utils.logging import get_logger

log = get_logger(__name__)

INTERPOLATION = {
    "nearest": cv2.INTER_NEAREST,
    "linear": cv2.INTER_LINEAR,
    "cubic": cv2.INTER_CUBIC,
    "area": cv2.INTER_AREA,
}


class PerspectiveStage(Stage):
    name = "perspective"

    def __init__(self, config: PerspectiveConfig, state: PipelineState):
        super().__init__(config, state)
        self.config: PerspectiveConfig = config
        self._matrix: np.ndarray | None = None
        self._matrix_for: np.ndarray | None = None

    def reset(self) -> None:
        self._matrix = None
        self._matrix_for = None

    def on_config_changed(self) -> None:
        self.reset()

    @property
    def _flags(self) -> int:
        return INTERPOLATION.get((self.config.interpolation or "linear").lower(), cv2.INTER_LINEAR)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "size": [self.config.width, self.config.height],
            "have_matrix": self._matrix is not None,
        }

    def process(self, ctx: FrameContext) -> None:
        corners = self.state.corners
        if corners is None:
            ctx.skipped[self.name] = "no corners"
            return

        width = max(16, int(self.config.width))
        height = max(16, int(self.config.height))

        # The homography only changes when the corners do, which -- thanks to
        # the deadband on the corner smoother -- is rarely.  Caching it keeps
        # this stage down to a single warpPerspective call.
        if (
            self._matrix is None
            or self._matrix_for is None
            or not np.array_equal(self._matrix_for, corners)
        ):
            self._matrix = homography_to_rect(corners, width, height)
            self._matrix_for = corners.copy()

        warped = cv2.warpPerspective(
            ctx.source,
            self._matrix,
            (width, height),
            flags=self._flags,
            borderMode=cv2.BORDER_REPLICATE,
        )
        ctx.set_image(warped)
        ctx.record(
            self.name,
            size=[width, height],
            aspect=round(width / height, 4),
        )
        ctx.add_debug(self.name, warped)
