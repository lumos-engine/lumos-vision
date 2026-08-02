"""TV boundary stage.

Owns the decision of *which* corners the rest of the pipeline uses, and when
to spend CPU looking for new ones.  It does not modify the image -- it only
publishes corners to the shared pipeline state, which the perspective stage
consumes.  That separation is what lets you calibrate by hand, by wizard, or
automatically without any of the other stages knowing the difference.
"""

from __future__ import annotations

import time
from typing import Any

import cv2
import numpy as np

from processor.config.schema import BoundaryConfig
from processor.pipeline.context import FrameContext, PipelineState
from processor.pipeline.stage import Stage
from processor.stages.detection import TvQuadDetector
from processor.utils.geometry import (
    full_frame_quad,
    order_corners,
    quad_from_normalised,
    quad_to_normalised,
)
from processor.utils.logging import get_logger
from processor.utils.smoothing import DeadbandEMA

log = get_logger(__name__)

#: While unlocked, attempt detection every Nth frame.  Counting frames rather
#: than seconds ties the cost to the frame rate (at 15 fps this is five
#: attempts a second) and keeps behaviour identical when frames arrive faster
#: than real time, as they do when replaying a file or running tests.
DETECT_FRAME_INTERVAL = 3


class BoundaryStage(Stage):
    name = "boundary"

    def __init__(self, config: BoundaryConfig, state: PipelineState):
        super().__init__(config, state)
        self.config: BoundaryConfig = config
        self.detector = TvQuadDetector(config)
        self._smoother = DeadbandEMA(
            alpha=config.smoothing_alpha,
            deadband=config.corner_deadband_px,
            snap=config.corner_snap_px,
        )
        self._locked = False
        self._confidence = 0.0
        self._origin = "none"
        self._last_success = 0.0
        self._frames_seen = 0
        self._attempts = 0
        self._successes = 0
        self._manual_stale = False
        self._warned_manual = False

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        self._locked = False
        self._confidence = 0.0
        self._origin = "none"
        self._smoother.reset()
        self.detector.reset()

    def on_config_changed(self) -> None:
        self.detector.config = self.config
        self._smoother = DeadbandEMA(
            alpha=self.config.smoothing_alpha,
            deadband=self.config.corner_deadband_px,
            snap=self.config.corner_snap_px,
        )
        self._locked = False
        self._manual_stale = False

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.config.mode,
            "locked": self._locked,
            "confidence": round(self._confidence, 3),
            "origin": self._origin,
            "attempts": self._attempts,
            "successes": self._successes,
            "activity_frames": self.detector.observations,
            "has_manual_corners": bool(self.config.corners),
        }

    # -- public control ----------------------------------------------------

    def force_recalibration(self) -> None:
        """Drop the lock so the next frame re-detects (keyboard 'r', web UI)."""
        log.info("Boundary recalibration forced")
        self._unlock()

    def _unlock(self) -> None:
        self._locked = False
        self._manual_stale = True
        self._smoother.reset()
        # The activity map records where the TV *was*.  If we are recalibrating
        # then that is exactly the assumption that just became false, so start
        # accumulating again rather than fitting to a union of both positions.
        self.detector.reset()

    def set_manual_corners(self, normalised: list[list[float]] | None) -> None:
        """Install corners from the calibration wizard (normalised 0..1)."""
        self.config.corners = normalised
        self._manual_stale = False
        self._locked = False
        self._smoother.reset()

    def detect_now(self, frame_bgr: np.ndarray) -> tuple[np.ndarray | None, float]:
        """One-shot detection for the wizard's "auto detect" button."""
        self.detector.observe(frame_bgr)
        result = self.detector.detect(frame_bgr)
        if result is None:
            return None, 0.0
        return result.quad, result.confidence

    # -- main --------------------------------------------------------------

    def process(self, ctx: FrameContext) -> None:
        height, width = ctx.source.shape[:2]
        now = time.monotonic()
        self._frames_seen += 1

        if self.state.take_recalibration_request() and self.config.auto_recalibrate:
            self._unlock()

        # Always keep the activity map warm.  It costs one absdiff on a small
        # greyscale image, and it means a recalibration triggered at 2am has
        # usable history immediately instead of after another two seconds.
        self.detector.observe(ctx.source)

        corners = self._manual_corners(width, height)
        if corners is not None:
            self._publish(ctx, corners, confidence=1.0, origin="manual")
            return

        if self._should_detect(now):
            self._attempt_detection(ctx, now)

        if self.state.corners is not None:
            self._publish(ctx, self.state.corners, self._confidence, self._origin)
        else:
            self._publish(ctx, full_frame_quad(width, height), 0.0, "fallback")

    # -- internals ---------------------------------------------------------

    def _manual_corners(self, width: int, height: int) -> np.ndarray | None:
        mode = (self.config.mode or "hybrid").lower()
        configured = self.config.corners

        if mode == "auto":
            return None
        if not configured:
            if mode == "manual" and not self._warned_manual:
                log.warning("boundary.mode is 'manual' but no corners are set; detecting instead")
                self._warned_manual = True
            return None
        if mode == "hybrid" and self._manual_stale:
            # The camera moved, so the saved corners are no longer where the
            # TV is.  Hand over to the detector until someone recalibrates.
            return None

        try:
            return order_corners(quad_from_normalised(configured, width, height))
        except (ValueError, TypeError):
            log.warning("Ignoring malformed boundary.corners: %r", configured)
            self.config.corners = None
            return None

    def _should_detect(self, now: float) -> bool:
        if not self._locked:
            return self._frames_seen % DETECT_FRAME_INTERVAL == 0
        interval = self.config.recalibrate_interval
        return interval > 0 and (now - self._last_success) >= interval

    def _attempt_detection(self, ctx: FrameContext, now: float) -> None:
        if not self.detector.ready:
            return

        self._attempts += 1
        result = self.detector.detect(ctx.source, collect_debug=False)
        if result is None or result.confidence < self.config.min_confidence:
            ctx.record(
                self.name,
                detection="rejected",
                candidate_confidence=None if result is None else round(result.confidence, 3),
            )
            return

        self._successes += 1
        self._last_success = now
        self._confidence = result.confidence
        self._origin = result.origin
        smoothed = order_corners(self._smoother.update(result.quad))
        self.state.set_corners(smoothed, result.confidence, result.origin)
        ctx.record(self.name, detection="accepted", parts=result.parts)

        # Publish early results so the output is usable within a second, but
        # keep searching until the activity map has a full window behind it --
        # an early lock freezes whatever the first partial map happened to
        # suggest, and nothing would ever revisit it.
        if not self.detector.mature:
            return

        self._locked = True
        log.info(
            "TV boundary locked (confidence %.2f, source=%s) after %d attempts",
            result.confidence,
            result.origin,
            self._attempts,
        )

    def _publish(
        self, ctx: FrameContext, corners: np.ndarray, confidence: float, origin: str
    ) -> None:
        corners = order_corners(corners)
        self.state.set_corners(corners, confidence, origin)
        self._confidence = confidence
        self._origin = origin

        height, width = ctx.source.shape[:2]
        ctx.record(
            self.name,
            corners=corners.tolist(),
            corners_normalised=quad_to_normalised(corners, width, height),
            confidence=round(confidence, 3),
            origin=origin,
            locked=self._locked or origin == "manual",
        )

    # -- debug -------------------------------------------------------------

    def debug_view(self, ctx: FrameContext) -> np.ndarray | None:
        corners = self.state.corners
        if corners is None:
            return None
        canvas = ctx.source.copy()
        pts = corners.astype(np.int32)
        cv2.polylines(canvas, [pts], True, (0, 235, 255), 2, cv2.LINE_AA)
        for i, (x, y) in enumerate(pts):
            cv2.circle(canvas, (int(x), int(y)), 6, (0, 128, 255), -1, cv2.LINE_AA)
            cv2.putText(
                canvas,
                "TL TR BR BL".split()[i],
                (int(x) + 8, int(y) - 8),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (0, 235, 255),
                1,
                cv2.LINE_AA,
            )
        cv2.putText(
            canvas,
            f"{self._origin} conf={self._confidence:.2f}",
            (10, canvas.shape[0] - 12),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 235, 255),
            1,
            cv2.LINE_AA,
        )
        return canvas
