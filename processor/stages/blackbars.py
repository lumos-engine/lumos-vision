"""Automatic letterbox / pillarbox removal.

No aspect-ratio profiles and no per-film configuration: the bars are measured
from the picture itself, every frame, and then aggressively stabilised.

Measurement uses a high percentile of each row rather than its mean.  A row of
a real image almost always contains *something* bright; a letterbox bar does
not, even when a subtitle sits a few rows below it.  The percentile makes that
distinction robust without being fooled by a handful of noisy pixels.

Stabilisation is where the real work is.  A crop that moves by two pixels is
invisible on a monitor and glaringly obvious on a light strip, so every
measurement passes through a median window, then a hysteresis gate that
demands the new value hold for most of a second, and finally a rate limiter so
even a committed change eases in over a second or two.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from processor.config.schema import BlackBarsConfig
from processor.pipeline.context import FrameContext, PipelineState
from processor.pipeline.stage import Stage
from processor.utils.smoothing import StableValue

EDGES = ("top", "bottom", "left", "right")


def _leading_run(is_bar: np.ndarray) -> int:
    """Length of the run of ``True`` at the start of the array."""
    if is_bar.size == 0 or not is_bar[0]:
        return 0
    nonbar = np.flatnonzero(~is_bar)
    return int(nonbar[0]) if nonbar.size else int(is_bar.size)


def _symmetric_pair(a: float, b: float, agree: float = 0.45) -> float:
    """Common crop for opposite edges, or 0 if they do not both look like bars.

    ``agree`` is the minimum ratio of the smaller measurement to the larger.
    Letterbox with a subtitle in the lower bar still agrees (~0.5–1.0);
    a single dark UI strip on one edge does not.
    """
    hi = max(float(a), float(b))
    lo = min(float(a), float(b))
    if hi < 0.015:
        return 0.0
    if lo < hi * agree:
        return 0.0
    return hi


def measure_bars(
    gray: np.ndarray,
    luma_threshold: float,
    percentile: float,
    detect_top_bottom: bool = True,
    detect_left_right: bool = True,
) -> dict[str, float]:
    """Raw bar sizes as a fraction of each dimension (before smoothing).

    "The Nth percentile of this row is below the threshold" is the same
    statement as "no more than (100-N)% of the row's pixels are above it", and
    the second form is a compare-and-sum instead of a sort per row.  On a
    540-row image that is the difference between 7 ms and 0.3 ms, which at
    15 fps is a tenth of the entire CPU budget.
    """
    height, width = gray.shape[:2]
    result = {edge: 0.0 for edge in EDGES}
    tail = max(0.0, min(1.0, 1.0 - percentile / 100.0))

    # Striding rather than resizing: averaging would dim a thin bright object
    # into the noise floor, and we want to notice those.
    col_step = max(1, width // 320)
    row_step = max(1, height // 320)

    if detect_top_bottom and height >= 8:
        sampled = gray[:, ::col_step]
        is_bar = _bar_mask(sampled, axis=1, luma_threshold=luma_threshold, tail=tail)
        if not is_bar.all():
            result["top"] = _leading_run(is_bar) / height
            result["bottom"] = _leading_run(is_bar[::-1]) / height

    if detect_left_right and width >= 8:
        sampled = gray[::row_step, :]
        is_bar = _bar_mask(sampled, axis=0, luma_threshold=luma_threshold, tail=tail)
        if not is_bar.all():
            result["left"] = _leading_run(is_bar) / width
            result["right"] = _leading_run(is_bar[::-1]) / width

    return result


def _bar_mask(
    sampled: np.ndarray,
    axis: int,
    luma_threshold: float,
    tail: float,
) -> np.ndarray:
    """Rows/cols that look like letterbox on a real camera, not a clean PNG.

    USB cams put noise and a faint glow into "black" bars, so a strict
    threshold alone misses them.  A row counts as bar when either:

    * almost no pixels exceed the threshold (subtitle-tolerant), or
    * its mean is dark *and* its brightest pixel is still modest (noisy bar).
    """
    bright_counts = np.count_nonzero(sampled > luma_threshold, axis=axis)
    length = sampled.shape[axis]
    few_bright = bright_counts <= tail * length
    means = sampled.mean(axis=axis)
    peaks = sampled.max(axis=axis)
    peak_cap = max(float(luma_threshold) * 2.5, 55.0)
    dark_noisy = (means < luma_threshold) & (peaks < peak_cap)
    return few_bright | dark_noisy


class BlackBarStage(Stage):
    name = "blackbars"

    def __init__(self, config: BlackBarsConfig, state: PipelineState):
        super().__init__(config, state)
        self.config: BlackBarsConfig = config
        self._filters: dict[str, StableValue] = {}
        self._raw: dict[str, float] = {edge: 0.0 for edge in EDGES}
        self._pixels: dict[str, int] = {edge: 0 for edge in EDGES}
        self._dark_frames = 0
        self._build_filters()

    # -- lifecycle ---------------------------------------------------------

    def _build_filters(self) -> None:
        cfg = self.config
        self._filters = {
            edge: StableValue(
                window=cfg.window,
                change_threshold=cfg.change_threshold_percent / 100.0,
                hold_frames=cfg.hold_frames,
                max_step=cfg.max_step_percent / 100.0,
                initial=0.0,
            )
            for edge in EDGES
        }

    def reset(self) -> None:
        self._build_filters()
        self._raw = {edge: 0.0 for edge in EDGES}
        self._pixels = {edge: 0 for edge in EDGES}
        self._dark_frames = 0

    def on_config_changed(self) -> None:
        # Keep the current crop so a slider nudge does not make the picture
        # jump; only the filter behaviour changes.
        previous = {edge: f.value for edge, f in self._filters.items()}
        self._build_filters()
        for edge, value in previous.items():
            self._filters[edge].force(value)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "raw_percent": {k: round(v * 100, 2) for k, v in self._raw.items()},
            "applied_percent": {
                k: round(f.value * 100, 2) for k, f in self._filters.items()
            },
            "pixels": dict(self._pixels),
            "content_aspect": self._content_aspect(),
        }

    def _content_aspect(self) -> float | None:
        top = self._filters["top"].value
        bottom = self._filters["bottom"].value
        left = self._filters["left"].value
        right = self._filters["right"].value
        remaining_h = 1.0 - top - bottom
        remaining_w = 1.0 - left - right
        if remaining_h <= 0 or remaining_w <= 0:
            return None
        return round((16.0 / 9.0) * (remaining_w / remaining_h), 3)

    # -- main --------------------------------------------------------------

    def process(self, ctx: FrameContext) -> None:
        image = ctx.image
        height, width = image.shape[:2]
        if height < 16 or width < 16:
            ctx.skipped[self.name] = "image too small"
            return

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        bright = float(np.percentile(gray[::4, ::4], 99.0))

        if bright < self.config.dark_frame_luma:
            # Fade to black, or the TV is off.  Every row looks like a bar, so
            # measuring now would collapse the crop; hold what we have.
            self._dark_frames += 1
            measured = None
        else:
            self._dark_frames = 0
            measured = measure_bars(
                gray,
                self.config.luma_threshold,
                self.config.percentile,
                self.config.detect_top_bottom,
                self.config.detect_left_right,
            )
            measured = self._postprocess(measured)
            self._raw = measured

        limit = max(0.0, self.config.max_crop_percent) / 100.0
        applied: dict[str, float] = {}
        for edge in EDGES:
            sample = self._raw[edge] if measured is not None else self._filters[edge].committed
            applied[edge] = float(np.clip(self._filters[edge].update(sample), 0.0, limit))

        top = int(round(height * applied["top"]))
        bottom = int(round(height * applied["bottom"]))
        left = int(round(width * applied["left"]))
        right = int(round(width * applied["right"]))
        self._pixels = {"top": top, "bottom": bottom, "left": left, "right": right}

        y0, y1 = top, height - bottom
        x0, x1 = left, width - right
        if y1 - y0 < 16 or x1 - x0 < 16:
            ctx.skipped[self.name] = "crop would leave nothing"
            return

        ctx.set_image(image[y0:y1, x0:x1])
        ctx.record(
            self.name,
            pixels=dict(self._pixels),
            raw_percent={k: round(v * 100, 2) for k, v in self._raw.items()},
            applied_percent={k: round(v * 100, 2) for k, v in applied.items()},
            content_aspect=self._content_aspect(),
            dark_frame=measured is None,
        )

    def _postprocess(self, measured: dict[str, float]) -> dict[str, float]:
        limit = max(0.0, self.config.max_crop_percent) / 100.0

        if self.config.symmetric:
            # Real letterbox/pillarbox is on *both* opposite edges.  Taking the
            # larger of a agreeing pair handles subtitles that shrink one side;
            # if only one side looks dark (Jellyfin cast row, UI chrome, a
            # gradient) that is not letterbox -- cropping it eats real content.
            measured = {
                "top": _symmetric_pair(measured["top"], measured["bottom"]),
                "bottom": _symmetric_pair(measured["top"], measured["bottom"]),
                "left": _symmetric_pair(measured["left"], measured["right"]),
                "right": _symmetric_pair(measured["left"], measured["right"]),
            }

        # Letterbox and pillarbox at the same time means the measurement is
        # confused (a dark scene, usually), so trust the larger pair only.
        if measured["top"] > 0.01 and measured["left"] > 0.01:
            if measured["top"] >= measured["left"]:
                measured["left"] = measured["right"] = 0.0
            else:
                measured["top"] = measured["bottom"] = 0.0

        return {edge: float(np.clip(value, 0.0, limit)) for edge, value in measured.items()}

    # -- debug -------------------------------------------------------------

    def debug_view(self, ctx: FrameContext) -> np.ndarray | None:
        base = ctx.debug_images.get("perspective")
        if base is None:
            return None
        canvas = base.copy()
        height, width = canvas.shape[:2]
        top, bottom = self._pixels["top"], self._pixels["bottom"]
        left, right = self._pixels["left"], self._pixels["right"]

        overlay = canvas.copy()
        if top:
            cv2.rectangle(overlay, (0, 0), (width, top), (40, 40, 220), -1)
        if bottom:
            cv2.rectangle(overlay, (0, height - bottom), (width, height), (40, 40, 220), -1)
        if left:
            cv2.rectangle(overlay, (0, 0), (left, height), (40, 40, 220), -1)
        if right:
            cv2.rectangle(overlay, (width - right, 0), (width, height), (40, 40, 220), -1)
        canvas = cv2.addWeighted(overlay, 0.35, canvas, 0.65, 0)

        aspect = self._content_aspect()
        cv2.putText(
            canvas,
            f"bars t{top} b{bottom} l{left} r{right}"
            + (f"  ~{aspect:.2f}:1" if aspect else ""),
            (10, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        return canvas
