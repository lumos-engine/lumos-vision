"""Per-frame and cross-frame pipeline state."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class FrameContext:
    """Everything a stage may read or write for one frame.

    ``image`` is the working image and is replaced by each stage; ``source`` is
    kept untouched so the boundary detector and the debug UI can always refer
    back to the original camera view.
    """

    source: np.ndarray
    image: np.ndarray
    index: int = 0
    captured_at: float = field(default_factory=time.monotonic)

    #: Structured stage output (corners, confidence, bar sizes, gains...).
    #: This is what the web UI and the overlay render from.
    meta: dict[str, Any] = field(default_factory=dict)
    #: Named intermediate images, only populated when debug collection is on.
    debug_images: dict[str, np.ndarray] = field(default_factory=dict)
    #: Stages that declined to run this frame, with a reason.
    skipped: dict[str, str] = field(default_factory=dict)
    #: Copied from the pipeline so stages can skip full-frame work in DDP mode
    #: unless the wizard (or another subscriber) actually wants a preview.
    collect_debug: bool = False
    #: Tiny warped panel used for letterbox probing / auto-WB in DDP mode.
    bar_probe: np.ndarray | None = None
    #: Colour params for LED samples when the full-frame colour stage is skipped.
    color_lut: np.ndarray | None = None
    color_matrix: np.ndarray | None = None
    color_black_level: tuple[float, float, float] | None = None
    color_saturation: float = 1.0

    @property
    def latency_ms(self) -> float:
        return (time.monotonic() - self.captured_at) * 1000.0

    def set_image(self, image: np.ndarray) -> None:
        self.image = image

    def record(self, stage: str, **values: Any) -> None:
        node = self.meta.setdefault(stage, {})
        node.update(values)

    def add_debug(self, name: str, image: np.ndarray | None) -> None:
        if image is not None:
            self.debug_images[name] = image


class PipelineState:
    """State that outlives a single frame and is shared between stages.

    The movement detector writes ``recalibrate_requested`` here and the
    boundary detector reads it; keeping that in one small, explicitly
    documented object avoids stages importing each other.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: Latest accepted TV corners in *source* pixel coordinates.
        self.corners: np.ndarray | None = None
        self.corner_confidence: float = 0.0
        self.corners_source: str = "none"  # manual | detected | fallback
        self.calibrated_at: float = 0.0
        self.frame_size: tuple[int, int] = (0, 0)

        self._recalibrate = False
        self.last_movement_at: float = 0.0
        self.movement_score: float = 0.0
        #: ``hyperhdr`` (full warp → V4L2) or ``ddp`` (sample the camera quad).
        self.led_path: str = "hyperhdr"

    def request_recalibration(self, reason: str = "") -> None:
        with self._lock:
            self._recalibrate = True
            self.recalibration_reason = reason

    def take_recalibration_request(self) -> bool:
        with self._lock:
            requested, self._recalibrate = self._recalibrate, False
            return requested

    @property
    def recalibration_pending(self) -> bool:
        return self._recalibrate

    def set_corners(self, corners: np.ndarray, confidence: float, origin: str) -> None:
        with self._lock:
            self.corners = np.asarray(corners, dtype=np.float32).reshape(4, 2)
            self.corner_confidence = float(confidence)
            self.corners_source = origin
            self.calibrated_at = time.monotonic()

    def clear_corners(self) -> None:
        with self._lock:
            self.corners = None
            self.corner_confidence = 0.0
            self.corners_source = "none"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "corners": None if self.corners is None else self.corners.tolist(),
                "confidence": round(self.corner_confidence, 3),
                "corners_source": self.corners_source,
                "frame_size": list(self.frame_size),
                "recalibration_pending": self._recalibrate,
                "movement_score": round(self.movement_score, 2),
                "last_movement_at": self.last_movement_at,
                "led_path": self.led_path,
            }
