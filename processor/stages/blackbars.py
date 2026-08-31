"""Automatic letterbox / pillarbox removal.

Default is still measured from the picture every frame, then aggressively
stabilised (no per-film profiles required).

``direction`` can pin an axis when Dolby Vision brightness pumping or
subtitles sitting on the bars fool auto. ``target_aspect`` (e.g. 2.39 or
21:9) skips measurement and crops the 16:9 panel to that rectangle.

Measurement combines an absolute/few-bright row mask (clean blacks) with a
luma-profile edge finder (USB cams that render cinema bars as dark red/gray).
Results are forced symmetric per axis, ignored until the TV boundary is
trusted, and then frozen through noisy "no bars" blips so the crop cannot
strobe.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from processor.config.schema import BlackBarsConfig
from processor.pipeline.context import FrameContext, PipelineState
from processor.pipeline.stage import Stage
from processor.utils.geometry import homography_to_rect, inset_quad
from processor.utils.smoothing import StableValue

EDGES = ("top", "bottom", "left", "right")
_DIRECTION_LETTERBOX = {"top_bottom", "topbottom", "letterbox", "horizontal"}
_DIRECTION_PILLARBOX = {"left_right", "leftright", "pillarbox", "vertical"}
#: Cheap remap target for letterbox detection when we skip the full warp.
DDP_PROBE_WIDTH = 160
DDP_PROBE_HEIGHT = 90


def parse_aspect_ratio(value: Any) -> float | None:
    """``2.39``, ``2.39:1``, ``21:9``, ``16/9`` to width/height, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if 0.5 <= number <= 4.0 else None
    text = str(value).strip().lower().replace(" ", "")
    if not text or text in {"off", "auto", "none", "0"}:
        return None
    if ":" in text or "/" in text:
        sep = ":" if ":" in text else "/"
        left, right = text.split(sep, 1)
        try:
            num, den = float(left), float(right)
        except ValueError:
            return None
        if den == 0:
            return None
        number = num / den
    else:
        try:
            number = float(text)
        except ValueError:
            return None
    if not (0.5 <= number <= 4.0):
        return None
    return number


def crop_fractions_for_aspect(
    width: int, height: int, target: float
) -> dict[str, float]:
    """Symmetric bar sizes that leave ``target`` on a ``width`` x ``height`` panel."""
    result = {edge: 0.0 for edge in EDGES}
    if width < 8 or height < 8 or target <= 0:
        return result
    panel = width / height
    if target > panel * 1.002:
        remaining_h = panel / target
        bar = max(0.0, (1.0 - remaining_h) / 2.0)
        result["top"] = result["bottom"] = bar
    elif target < panel / 1.002:
        remaining_w = target / panel
        bar = max(0.0, (1.0 - remaining_w) / 2.0)
        result["left"] = result["right"] = bar
    return result


def detect_axes(config: BlackBarsConfig) -> tuple[bool, bool]:
    """Return (letterbox, pillarbox) detection flags from ``direction``."""
    mode = str(getattr(config, "direction", "auto") or "auto").strip().lower()
    mode = mode.replace("-", "_").replace(" ", "_")
    if mode in _DIRECTION_LETTERBOX:
        return True, False
    if mode in _DIRECTION_PILLARBOX:
        return False, True
    return bool(config.detect_top_bottom), bool(config.detect_left_right)


def _leading_run(is_bar: np.ndarray) -> int:
    """Length of the run of ``True`` at the start of the array."""
    if is_bar.size == 0 or not is_bar[0]:
        return 0
    nonbar = np.flatnonzero(~is_bar)
    return int(nonbar[0]) if nonbar.size else int(is_bar.size)


def _symmetric_pair(a: float, b: float, min_side: float = 0.03) -> float:
    """Common crop for opposite edges, or 0 if they do not both look like bars.

    Real letterbox has a bar on *both* sides.  We only require each side to
    clear a small absolute floor (``min_side``), then take the larger reading
    so a subtitle that shrinks the lower bar does not under-crop.  A single
    dark UI strip (Jellyfin cast row) leaves the other side near 0 and is
    ignored.  A ratio-based "agree" check was tried and rejected: bright
    content against the bar often makes the two sides unequal enough to
    disable cinema crops entirely.
    """
    hi = max(float(a), float(b))
    lo = min(float(a), float(b))
    if hi < 0.015 or lo < min_side:
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

    Two detectors, take the larger reading per edge:

    1. Absolute / few-bright mask -- reliable on clean blacks and the
       synthetic pipeline tests.
    2. Profile edge -- finds the bar→picture luma jump when USB cams render
       cinema bars as dark red/gray (luma 45-55) that miss the absolute cut.
    """
    height, width = gray.shape[:2]
    result = {edge: 0.0 for edge in EDGES}
    if height < 8 or width < 8:
        return result

    tail = max(0.0, min(1.0, 1.0 - percentile / 100.0))
    col_step = max(1, width // 320)
    row_step = max(1, height // 320)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)

    if detect_top_bottom:
        sampled = gray[:, ::col_step]
        is_bar = _bar_mask(sampled, axis=1, luma_threshold=luma_threshold, tail=tail)
        abs_top = abs_bottom = 0.0
        if not is_bar.all():
            abs_top = _leading_run(is_bar) / height
            abs_bottom = _leading_run(is_bar[::-1]) / height
        row_means = blur.mean(axis=1)
        edge_top = _profile_edge_depth(row_means, luma_threshold) / height
        edge_bottom = _profile_edge_depth(row_means[::-1], luma_threshold) / height
        result["top"] = max(abs_top, edge_top)
        result["bottom"] = max(abs_bottom, edge_bottom)

    if detect_left_right:
        sampled = gray[::row_step, :]
        is_bar = _bar_mask(sampled, axis=0, luma_threshold=luma_threshold, tail=tail)
        abs_left = abs_right = 0.0
        if not is_bar.all():
            abs_left = _leading_run(is_bar) / width
            abs_right = _leading_run(is_bar[::-1]) / width
        col_means = blur.mean(axis=0)
        edge_left = _profile_edge_depth(col_means, luma_threshold) / width
        edge_right = _profile_edge_depth(col_means[::-1], luma_threshold) / width
        result["left"] = max(abs_left, edge_left)
        result["right"] = max(abs_right, edge_right)

    return result


def _profile_edge_depth(means: np.ndarray, luma_threshold: float) -> int:
    """Pixels of letterbox from one edge, via the strongest luma rise."""
    n = int(means.size)
    if n < 8:
        return 0

    mid = means[n // 5 : 4 * n // 5]
    if mid.size == 0:
        return 0
    content = float(np.percentile(mid, 80))
    # Whole-frame black / TV off: every row looks the same.
    if content < max(8.0, float(luma_threshold) * 0.35):
        return 0

    search = max(8, int(n * 0.38))
    deltas = np.diff(means.astype(np.float64))
    region = deltas[:search]
    if region.size == 0:
        return 0

    idx = int(np.argmax(region))
    jump = float(region[idx])
    if jump < max(10.0, content * 0.10):
        return 0

    before = float(means[: max(1, idx + 1)].mean())
    after = float(means[idx + 1 : idx + 1 + max(8, n // 25)].mean())
    if before >= after * 0.9:
        return 0
    # Outside must look like a bar relative to the picture, not just a soft
    # gradient inside the scene.  Blue-black bars (#201C58 → gray ~36) need
    # a looser absolute cap than true black.
    if before > max(float(luma_threshold) * 1.35, content * 0.50):
        return 0
    return idx + 1


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
    * its mean is dark *and* its high percentile is still modest (noisy bar;
      use a percentile instead of max so single sparkle pixels do not veto).
    """
    thr = float(luma_threshold)
    bright_counts = np.count_nonzero(sampled > thr, axis=axis)
    length = sampled.shape[axis]
    few_bright = bright_counts <= tail * length
    means = sampled.mean(axis=axis)
    peaks = np.percentile(sampled, 92, axis=axis)
    # Blue-tinted bars push the gray peak above a tight cap; allow headroom.
    peak_cap = max(thr * 2.2, 90.0)
    dark_noisy = (means < thr * 1.25) & (peaks < peak_cap)
    return few_bright | dark_noisy


class BlackBarStage(Stage):
    name = "blackbars"

    def __init__(self, config: BlackBarsConfig, state: PipelineState):
        super().__init__(config, state)
        self.config: BlackBarsConfig = config
        self._filters: dict[str, StableValue] = {}
        self._vertical: StableValue | None = None
        self._horizontal: StableValue | None = None
        self._letterbox_locked = False
        self._pillarbox_locked = False
        self._vertical_misses = 0
        self._horizontal_misses = 0
        self._vertical_shrinks = 0
        self._horizontal_shrinks = 0
        self._raw: dict[str, float] = {edge: 0.0 for edge in EDGES}
        self._pixels: dict[str, int] = {edge: 0 for edge in EDGES}
        self._dark_frames = 0
        self._frame_wh: tuple[int, int] = (16, 9)
        self._build_filters()

    # -- lifecycle ---------------------------------------------------------

    def _filter_kwargs(self) -> dict[str, float | int]:
        cfg = self.config
        # Noisy USB cams flap below the hold window; bias stickier than the
        # YAML minimum so cinema bars do not strobe on and off.
        return {
            "window": max(cfg.window, 21),
            "change_threshold": cfg.change_threshold_percent / 100.0,
            "hold_frames": max(cfg.hold_frames, 14),
            "max_step": cfg.max_step_percent / 100.0,
            "initial": 0.0,
        }

    def _release_frames(self) -> int:
        # Once cinema bars are on, demand a long streak of "no bars" before
        # we even *propose* releasing.  Median+hold alone still strobes when
        # noisy frames alternate bar / no-bar every few samples.
        return max(self.config.hold_frames * 6, 60)

    def _build_filters(self) -> None:
        kwargs = self._filter_kwargs()
        if self.config.symmetric:
            # One filter per axis so top/bottom cannot animate apart and make
            # the debug overlay (and the crop) strobe.
            self._vertical = StableValue(**kwargs)
            self._horizontal = StableValue(**kwargs)
            self._filters = {
                "top": self._vertical,
                "bottom": self._vertical,
                "left": self._horizontal,
                "right": self._horizontal,
            }
        else:
            self._vertical = None
            self._horizontal = None
            self._filters = {edge: StableValue(**kwargs) for edge in EDGES}

    def reset(self) -> None:
        self._build_filters()
        self._raw = {edge: 0.0 for edge in EDGES}
        self._pixels = {edge: 0 for edge in EDGES}
        self._dark_frames = 0
        self._frame_wh = (16, 9)
        self._letterbox_locked = False
        self._pillarbox_locked = False
        self._vertical_misses = 0
        self._horizontal_misses = 0
        self._vertical_shrinks = 0
        self._horizontal_shrinks = 0

    def on_config_changed(self) -> None:
        # Keep the current crop so a slider nudge does not make the picture
        # jump; only the filter behaviour changes.  Pinning an axis (or an
        # aspect) does drop the disabled side immediately.
        previous_v = self._filters["top"].value
        previous_h = self._filters["left"].value
        previous = {edge: f.value for edge, f in self._filters.items()}
        self._build_filters()
        if self.config.symmetric and self._vertical and self._horizontal:
            self._vertical.force(previous_v)
            self._horizontal.force(previous_h)
        else:
            for edge, value in previous.items():
                self._filters[edge].force(value)
        detect_v, detect_h = detect_axes(self.config)
        if not detect_v:
            self._force_axis("vertical", 0.0)
        if not detect_h:
            self._force_axis("horizontal", 0.0)

    def status(self) -> dict[str, Any]:
        applied = self._applied_fractions()
        return {
            "enabled": self.enabled,
            "raw_percent": {k: round(v * 100, 2) for k, v in self._raw.items()},
            "applied_percent": {k: round(v * 100, 2) for k, v in applied.items()},
            "pixels": dict(self._pixels),
            "content_aspect": self._content_aspect(),
            "letterbox_locked": self._letterbox_locked,
            "direction": str(self.config.direction or "auto"),
            "target_aspect": parse_aspect_ratio(self.config.target_aspect),
        }

    def _applied_fractions(self) -> dict[str, float]:
        return {edge: float(self._filters[edge].value) for edge in EDGES}

    def _content_aspect(self) -> float | None:
        applied = self._applied_fractions()
        remaining_h = 1.0 - applied["top"] - applied["bottom"]
        remaining_w = 1.0 - applied["left"] - applied["right"]
        if remaining_h <= 0 or remaining_w <= 0:
            return None
        width, height = self._frame_wh
        panel = width / max(height, 1)
        return round(panel * (remaining_w / remaining_h), 3)

    def _force_axis(self, axis: str, value: float) -> None:
        clipped = float(max(0.0, value))
        if axis == "vertical":
            if self._vertical is not None:
                self._vertical.force(clipped)
            else:
                self._filters["top"].force(clipped)
                self._filters["bottom"].force(clipped)
            if clipped < 0.02:
                self._letterbox_locked = False
                self._vertical_misses = 0
                self._vertical_shrinks = 0
        else:
            if self._horizontal is not None:
                self._horizontal.force(clipped)
            else:
                self._filters["left"].force(clipped)
                self._filters["right"].force(clipped)
            if clipped < 0.02:
                self._pillarbox_locked = False
                self._horizontal_misses = 0
                self._horizontal_shrinks = 0

    # -- main --------------------------------------------------------------

    def _ddp_probe(self, ctx: FrameContext) -> np.ndarray | None:
        """Warp the (crop-inset) TV quad to a tiny rect for ``measure_bars``."""
        corners = self.state.corners
        if corners is None:
            return None
        crop = (ctx.meta.get("crop") or {}).get("fractions") or {}
        top = float(crop.get("top", 0.0))
        bottom = float(crop.get("bottom", 0.0))
        left = float(crop.get("left", 0.0))
        right = float(crop.get("right", 0.0))
        quad = (
            inset_quad(corners, left, top, right, bottom)
            if max(top, bottom, left, right) > 1e-6
            else corners
        )
        matrix = homography_to_rect(quad, DDP_PROBE_WIDTH, DDP_PROBE_HEIGHT)
        return cv2.warpPerspective(
            ctx.source,
            matrix,
            (DDP_PROBE_WIDTH, DDP_PROBE_HEIGHT),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )

    def process(self, ctx: FrameContext) -> None:
        ddp = self.state.led_path == "ddp"
        if ddp:
            image = self._ddp_probe(ctx)
            if image is None:
                ctx.skipped[self.name] = "no corners"
                return
            ctx.bar_probe = image
            if ctx.collect_debug:
                ctx.add_debug("blackbars_probe", image)
        else:
            image = ctx.image
        height, width = image.shape[:2]
        if height < 16 or width < 16:
            ctx.skipped[self.name] = "image too small"
            return
        self._frame_wh = (width, height)

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        bright = float(np.percentile(gray[::4, ::4], 99.0))
        detect_v, detect_h = detect_axes(self.config)
        target = parse_aspect_ratio(self.config.target_aspect)

        # Until the TV quad is trusted, the warp invents fake letterbox and
        # sticky filters freeze it.  Unit tests (no boundary stage) leave
        # corners unset -- allow measuring in that case.
        boundary_ready = self.state.corner_confidence >= 0.35 or (
            self.state.corners is None and self.state.corners_source == "none"
        )

        forced = None
        if target is not None:
            forced = crop_fractions_for_aspect(width, height, target)
            if not detect_v:
                forced = {**forced, "top": 0.0, "bottom": 0.0}
            if not detect_h:
                forced = {**forced, "left": 0.0, "right": 0.0}

        if not boundary_ready:
            measured = None
        elif forced is not None:
            # Pinned aspect: do not let DV fades or subtitle-on-bar rows
            # override the geometry.
            self._dark_frames = 0
            measured = self._postprocess(forced)
            self._raw = measured
        elif bright < self.config.dark_frame_luma:
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
                detect_v,
                detect_h,
            )
            measured = self._postprocess(measured)
            self._raw = measured

        v_limit, h_limit = self._crop_limits()
        if self.config.symmetric and self._vertical and self._horizontal:
            if forced is not None and measured is not None:
                v_sample = measured["top"] if detect_v else 0.0
                h_sample = measured["left"] if detect_h else 0.0
                self._force_axis("vertical", float(np.clip(v_sample, 0.0, v_limit)))
                self._force_axis("horizontal", float(np.clip(h_sample, 0.0, h_limit)))
                vertical = float(np.clip(self._vertical.value, 0.0, v_limit))
                horizontal = float(np.clip(self._horizontal.value, 0.0, h_limit))
            else:
                if not detect_v:
                    v_sample = 0.0
                    self._force_axis("vertical", 0.0)
                elif measured is None:
                    v_sample = self._vertical.committed
                else:
                    v_sample = self._sticky_sample(
                        measured["top"],
                        self._vertical,
                        locked=self._letterbox_locked,
                        misses_attr="_vertical_misses",
                        shrinks_attr="_vertical_shrinks",
                    )
                if not detect_h:
                    h_sample = 0.0
                    self._force_axis("horizontal", 0.0)
                elif measured is None:
                    h_sample = self._horizontal.committed
                else:
                    h_sample = self._sticky_sample(
                        measured["left"],
                        self._horizontal,
                        locked=self._pillarbox_locked,
                        misses_attr="_horizontal_misses",
                        shrinks_attr="_horizontal_shrinks",
                    )
                vertical = float(np.clip(self._vertical.update(v_sample), 0.0, v_limit))
                horizontal = float(np.clip(self._horizontal.update(h_sample), 0.0, h_limit))
            vertical, horizontal = self._exclusive_axis(vertical, horizontal)
            applied = {
                "top": vertical,
                "bottom": vertical,
                "left": horizontal,
                "right": horizontal,
            }
            # Lock from the committed target, not the rate-limited display
            # value, so a slow walk-down does not unlock early.
            self._letterbox_locked = self._vertical.committed >= 0.02
            self._pillarbox_locked = self._horizontal.committed >= 0.02
        else:
            applied = {}
            for edge in EDGES:
                sample = (
                    self._raw[edge] if measured is not None else self._filters[edge].committed
                )
                if edge in ("top", "bottom") and not detect_v:
                    sample = 0.0
                if edge in ("left", "right") and not detect_h:
                    sample = 0.0
                limit = v_limit if edge in ("top", "bottom") else h_limit
                if forced is not None:
                    self._filters[edge].force(float(np.clip(sample, 0.0, limit)))
                    applied[edge] = float(np.clip(self._filters[edge].value, 0.0, limit))
                else:
                    applied[edge] = float(np.clip(self._filters[edge].update(sample), 0.0, limit))
            # Same either/or rule when per-edge filters are used.
            v = max(applied["top"], applied["bottom"])
            h = max(applied["left"], applied["right"])
            if v >= 0.02 and h >= 0.02:
                if v >= h:
                    applied["left"] = applied["right"] = 0.0
                else:
                    applied["top"] = applied["bottom"] = 0.0

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

        if not ddp:
            ctx.set_image(image[y0:y1, x0:x1])
        ctx.record(
            self.name,
            pixels=dict(self._pixels),
            raw_percent={k: round(v * 100, 2) for k, v in self._raw.items()},
            applied_percent={k: round(v * 100, 2) for k, v in applied.items()},
            applied_fractions={k: round(v, 4) for k, v in applied.items()},
            content_aspect=self._content_aspect(),
            dark_frame=measured is None,
        )

    def _sticky_sample(
        self,
        sample: float,
        filt: StableValue,
        *,
        locked: bool,
        misses_attr: str,
        shrinks_attr: str,
    ) -> float:
        """While locked: hard to grow/release, easy to give picture back.

        Dark scenes briefly over-detect letterbox and eat the movie; that must
        shrink back within a fraction of a second.  Growing the crop or
        dropping to zero still needs a sustained signal so the overlay does
        not strobe.
        """
        if not locked:
            setattr(self, misses_attr, 0)
            setattr(self, shrinks_attr, 0)
            return sample

        committed = filt.committed
        if sample >= 0.02:
            setattr(self, misses_attr, 0)
            # Shrink (less crop → more picture): commit quickly.
            if sample < committed - 0.008:
                hits = int(getattr(self, shrinks_attr)) + 1
                setattr(self, shrinks_attr, hits)
                if hits >= 6:
                    filt.force(float(sample))
                    setattr(self, shrinks_attr, 0)
                return sample
            setattr(self, shrinks_attr, 0)
            # Grow (more crop → eat picture): only if clearly larger.
            if sample > committed + 0.045:
                return sample
            return committed

        setattr(self, shrinks_attr, 0)
        misses = int(getattr(self, misses_attr)) + 1
        setattr(self, misses_attr, misses)
        if misses < self._release_frames():
            return committed
        return sample

    def _crop_limits(self) -> tuple[float, float]:
        """Return (top/bottom limit, left/right limit) as fractions 0..1."""
        v = max(0.0, float(self.config.max_crop_top_bottom_percent)) / 100.0
        h = max(0.0, float(self.config.max_crop_left_right_percent)) / 100.0
        return v, h

    def _postprocess(self, measured: dict[str, float]) -> dict[str, float]:
        v_limit, h_limit = self._crop_limits()

        if self.config.symmetric:
            # Sticky floors: once locked on letterbox, allow more asymmetry so
            # noisy frames do not drop the crop to zero and strobe the overlay.
            vertical = _symmetric_pair(
                measured["top"],
                measured["bottom"],
                min_side=0.012 if self._letterbox_locked else 0.03,
            )
            horizontal = _symmetric_pair(
                measured["left"],
                measured["right"],
                min_side=0.012 if self._pillarbox_locked else 0.03,
            )
            measured = {
                "top": vertical,
                "bottom": vertical,
                "left": horizontal,
                "right": horizontal,
            }

        # Real content is letterboxed *or* pillarboxed, never a windowbox.
        # Prefer the larger pair so dark-scene false positives on the other
        # axis cannot shrink the picture from all four sides.
        if measured["top"] > 0.01 and measured["left"] > 0.01:
            if measured["top"] >= measured["left"]:
                measured["left"] = measured["right"] = 0.0
            else:
                measured["top"] = measured["bottom"] = 0.0

        limits = {
            "top": v_limit,
            "bottom": v_limit,
            "left": h_limit,
            "right": h_limit,
        }
        return {
            edge: float(np.clip(value, 0.0, limits[edge])) for edge, value in measured.items()
        }

    def _exclusive_axis(self, vertical: float, horizontal: float) -> tuple[float, float]:
        """Keep only one axis of crop; clear the sticky filter on the loser."""
        if vertical < 0.02 or horizontal < 0.02:
            return vertical, horizontal
        if vertical >= horizontal:
            if self._horizontal is not None and self._horizontal.committed > 0.0:
                self._horizontal.force(0.0)
            self._pillarbox_locked = False
            self._horizontal_misses = 0
            self._horizontal_shrinks = 0
            return vertical, 0.0
        if self._vertical is not None and self._vertical.committed > 0.0:
            self._vertical.force(0.0)
        self._letterbox_locked = False
        self._vertical_misses = 0
        self._vertical_shrinks = 0
        return 0.0, horizontal

    # -- debug -------------------------------------------------------------

    def debug_view(self, ctx: FrameContext) -> np.ndarray | None:
        base = ctx.debug_images.get("perspective")
        if base is None:
            base = ctx.bar_probe
        if base is None:
            return None
        canvas = base.copy()
        height, width = canvas.shape[:2]
        # ``_pixels`` is in probe space (160×90 in DDP). Draw from fractions
        # so a 320×180 wizard preview is not under-cropped by half.
        applied = self._applied_fractions()
        top = int(round(height * applied["top"]))
        bottom = int(round(height * applied["bottom"]))
        left = int(round(width * applied["left"]))
        right = int(round(width * applied["right"]))

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
