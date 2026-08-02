"""Camera movement detection.

Re-running quadrilateral detection on every frame is wasteful when the camera
is bolted to a shelf and the TV has not moved since last Tuesday.  This stage
answers one much cheaper question, twice a second: *is the calibration still
valid?*

The default method exploits an asymmetry in the scene: TV content changes
constantly, the room around it does not.  So the TV is masked out and the
remaining scenery -- wall, picture frame, door, sideboard -- is compared with a
reference by normalised cross correlation.

NCC rather than a brightness difference, because the two things we must tell
apart both change every pixel in the frame.  Turning the room lights on scales
the whole image and moves nothing; nudging the camera preserves brightness and
moves everything.  A difference metric ranks the lighting change as the larger
event by a factor of twenty, which is exactly backwards.  Correlation is
invariant to that scaling.  Measured on the reference scene: TV content scores
0.08 and a 15 % lighting change 0.05, while a 34 px camera bump scores 8.8.

Two more obvious approaches were tried and rejected, both worth recording so
they do not get re-attempted:

* **Phase correlation** measures the translation directly, in pixels, which is
  exactly the quantity of interest.  But a panning shot on screen is a large
  coherent translation, and the TV cannot be masked out of it: a fixed mask is
  identical in both frames, so its own edges correlate perfectly at zero shift
  and pin the answer there.  Unmasked, it reported up to 19 px of "movement"
  on a perfectly still camera.
* **ORB feature matching** needs texture in the static region, and there is
  almost none.  Anyone using this aims the camera at the TV, so the screen
  fills most of the frame and what is left is a smooth wall -- zero keypoints
  in the reference scene.

A real bump also *stays* bumped, so a detection additionally requires
consecutive checks to agree, which rejects someone walking through the shot.
"""

from __future__ import annotations

import time
from typing import Any

import cv2
import numpy as np

from processor.config.schema import MovementConfig
from processor.pipeline.context import FrameContext, PipelineState
from processor.pipeline.stage import Stage
from processor.utils.geometry import quad_mask
from processor.utils.logging import get_logger
from processor.utils.smoothing import Debouncer
from processor.utils.timing import Periodic

log = get_logger(__name__)

#: Width the comparison runs at.  Movement worth reacting to is measured in
#: whole degrees of camera rotation, which is many pixels even at this size.
CHECK_WIDTH = 240


class MovementStage(Stage):
    name = "movement"

    def __init__(self, config: MovementConfig, state: PipelineState):
        super().__init__(config, state)
        self.config: MovementConfig = config
        self._reference: np.ndarray | None = None
        self._timer = Periodic(config.check_interval)
        self._debouncer = Debouncer(config.consecutive)
        self._settle_until = 0.0
        self._score = 0.0
        self._detections = 0

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        self._reference = None
        self._debouncer.reset()
        self._timer.reset()
        self._settle_until = time.monotonic() + self.config.settle_time

    def on_config_changed(self) -> None:
        self._timer = Periodic(self.config.check_interval)
        self._debouncer = Debouncer(self.config.consecutive)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "method": self.config.method,
            "score": round(self._score, 2),
            "detections": self._detections,
            "has_reference": self._reference is not None,
        }

    # -- main --------------------------------------------------------------

    def process(self, ctx: FrameContext) -> None:
        method = (self.config.method or "ncc").lower()
        if method == "none":
            return

        if self.state.corners is None:
            # The question this stage answers is "is the calibration still
            # valid?".  With no calibration there is nothing to invalidate.
            ctx.record(self.name, skipped="no calibration")
            return

        now = time.monotonic()
        if now < self._settle_until:
            ctx.record(self.name, skipped="settling")
            return
        if not self._timer.ready(now):
            ctx.record(self.name, score=round(self._score, 2), moved=False)
            return

        small = self._prepare(ctx.source)
        mask = self._static_mask(ctx, small.shape)
        if np.count_nonzero(mask) < 0.05 * mask.size:
            # The TV fills the frame; there is no static scenery left to
            # compare, so any answer we gave would be noise.
            ctx.record(self.name, skipped="no static region")
            return

        confirmed, score = self._check_ncc(small, mask)

        self._score = score
        self.state.movement_score = score

        if confirmed:
            self._detections += 1
            self.state.last_movement_at = now
            self._settle_until = now + self.config.settle_time
            self._reference = None
            self._debouncer.reset()
            log.info("Camera movement detected (%s score %.2f) -- recalibrating", method, score)
            self.state.request_recalibration("camera movement")

        ctx.record(self.name, score=round(score, 2), moved=confirmed, method=method)

    # -- helpers -----------------------------------------------------------

    def _prepare(self, image: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if gray.shape[1] > CHECK_WIDTH:
            scale = CHECK_WIDTH / float(gray.shape[1])
            gray = cv2.resize(
                gray,
                (CHECK_WIDTH, max(1, int(round(gray.shape[0] * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        return cv2.GaussianBlur(gray, (3, 3), 0)

    def _static_mask(self, ctx: FrameContext, shape: tuple[int, ...]) -> np.ndarray:
        """255 where the scene should be static (everything but the TV+glow)."""
        height, width = shape[:2]
        mask = np.full((height, width), 255, dtype=np.uint8)

        corners = self.state.corners
        if corners is None:
            return mask

        src_h, src_w = ctx.source.shape[:2]
        if not src_w or not src_h:
            return mask
        scaled = np.asarray(corners, dtype=np.float32).copy()
        scaled[:, 0] *= width / float(src_w)
        scaled[:, 1] *= height / float(src_h)

        tv = quad_mask(scaled, width, height)
        # The TV lights the wall around it, so the halo is not static either.
        halo = max(3, int(round(width * 0.05))) | 1
        tv = cv2.dilate(tv, np.ones((halo, halo), np.uint8))
        return cv2.bitwise_not(tv)

    def _confirm(self, score: float, threshold: float) -> bool:
        """True once `consecutive` checks in a row have exceeded the threshold.

        The score is a decorrelation percentage and fluctuates with content, so
        only its persistence is meaningful.  A brief obstruction (someone
        crossing the shot) clears within a check or two; a moved camera never
        does.
        """
        return self._debouncer.update(score > threshold)

    # -- methods -----------------------------------------------------------

    def _check_ncc(self, small: np.ndarray, mask: np.ndarray) -> tuple[bool, float]:
        current = small.astype(np.float32)
        if self._reference is None or self._reference.shape != current.shape:
            self._reference = current
            self._debouncer.reset()
            return False, 0.0

        selected = mask > 0
        a = current[selected]
        b = self._reference[selected]
        a = a - a.mean()
        b = b - b.mean()
        denominator = float(np.sqrt(float(a @ a) * float(b @ b)))
        if denominator < 1e-6:
            return False, 0.0

        score = 100.0 * (1.0 - float(a @ b) / denominator)
        return self._confirm(score, max(0.05, self.config.ncc_threshold)), score
