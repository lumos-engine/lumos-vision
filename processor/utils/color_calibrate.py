"""Solid-patch colour calibration for the Screen Sight colour stage.

Shows known RGB patches on the TV (via the calibrate-display page), samples the
centre of the perspective-corrected frame, and solves manual BGR gains (+ a
light gamma tweak) that fit the existing :class:`ColorStage` LUT.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

#: Patches shown on the TV during an occasional manual run (sRGB 0–255).
#: Extra greys improve midtone / gamma fit; skip a full hue chart (needs 3×3).
DEFAULT_PATCHES: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("black", (0, 0, 0)),
    ("white", (255, 255, 255)),
    ("grey_dark", (64, 64, 64)),
    ("grey", (128, 128, 128)),
    ("grey_light", (192, 192, 192)),
    ("red", (255, 0, 0)),
    ("green", (0, 255, 0)),
    ("blue", (0, 0, 255)),
)

#: Neutral patches used for gain blend / gamma (name → nominal sRGB level).
_GREY_LEVELS: dict[str, float] = {
    "grey_dark": 64.0,
    "grey": 128.0,
    "grey_light": 192.0,
}

_LUMA_BGR = np.array([0.114, 0.587, 0.299], dtype=np.float64)


@dataclass(frozen=True)
class Patch:
    name: str
    #: sRGB 0–255 as (R, G, B) — display / browser order.
    rgb: tuple[int, int, int]

    @property
    def bgr(self) -> tuple[int, int, int]:
        r, g, b = self.rgb
        return (b, g, r)


@dataclass
class CalibrationSolution:
    #: OpenCV BGR channel gains for ``color.gains`` (stored as r/g/b in config).
    gains_bgr: tuple[float, float, float]
    gamma: float
    patch_means_bgr: dict[str, list[float]]
    notes: list[str] = field(default_factory=list)

    def gains_rgb(self) -> tuple[float, float, float]:
        b, g, r = self.gains_bgr
        return (r, g, b)

    def as_dict(self) -> dict[str, Any]:
        r, g, b = self.gains_rgb()
        return {
            "gains": {"r": round(r, 4), "g": round(g, 4), "b": round(b, 4)},
            "gamma": round(float(self.gamma), 4),
            "patch_means_bgr": {
                name: [round(v, 2) for v in mean]
                for name, mean in self.patch_means_bgr.items()
            },
            "notes": list(self.notes),
        }


def sample_center_roi(
    image: np.ndarray, *, fraction: float = 0.25
) -> np.ndarray:
    """Mean BGR of the centre ``fraction`` box (each side)."""
    if image is None or image.size == 0:
        raise ValueError("empty image")
    if image.ndim != 3 or image.shape[2] < 3:
        raise ValueError("expected a colour image")
    height, width = image.shape[:2]
    frac = float(np.clip(fraction, 0.05, 0.9))
    rw = max(2, int(round(width * frac)))
    rh = max(2, int(round(height * frac)))
    x0 = max(0, (width - rw) // 2)
    y0 = max(0, (height - rh) // 2)
    roi = image[y0 : y0 + rh, x0 : x0 + rw, :3]
    return roi.reshape(-1, 3).astype(np.float64).mean(axis=0)


def _neutral_gains(mean_bgr: np.ndarray) -> np.ndarray:
    safe = np.maximum(mean_bgr, 1.0)
    gains = float(safe.mean()) / safe
    return gains / float(np.mean(gains))


def solve_gains(
    means_bgr: dict[str, np.ndarray | list[float]],
    *,
    gain_min: float = 0.5,
    gain_max: float = 2.0,
) -> CalibrationSolution:
    """Solve manual channel gains (+ gamma) from measured patch means.

    White anchors balance; dark/mid/light greys refine midtone neutrality and
    gamma. Primaries are sanity checks only (crushed / leaky channels).
    """
    notes: list[str] = []
    cleaned: dict[str, np.ndarray] = {
        name: np.asarray(mean, dtype=np.float64).reshape(3)
        for name, mean in means_bgr.items()
    }

    white = cleaned.get("white")
    if white is None:
        raise ValueError("white patch measurement is required")

    white_luma = float(np.dot(white, _LUMA_BGR))
    if white_luma < 8.0:
        raise ValueError(
            f"white patch too dark (luma {white_luma:.1f}) — is the TV patch page fullscreen on HDMI?"
        )

    # Weighted blend: white strongest, then lighter greys, then dark grey.
    weighted: list[tuple[float, np.ndarray]] = [(1.0, _neutral_gains(white))]
    grey_weights = {
        "grey_light": 0.35,
        "grey": 0.45,
        "grey_dark": 0.25,
    }
    for name, weight in grey_weights.items():
        sample = cleaned.get(name)
        if sample is None:
            # Backward compatible: accept legacy single "grey" only.
            continue
        if float(sample.mean()) < 4.0:
            notes.append(f"{name} too dark to use for balance")
            continue
        weighted.append((weight, _neutral_gains(sample)))

    # Legacy alias: older runs may only have "grey".
    if "grey" not in cleaned and "grey_mid" in cleaned:
        weighted.append((0.45, _neutral_gains(cleaned["grey_mid"])))

    total_w = sum(w for w, _ in weighted)
    gains = sum(w * g for w, g in weighted) / total_w
    gains = gains / float(np.mean(gains))
    gains = np.clip(gains, gain_min, gain_max)

    # Gamma: average estimates from each grey vs white after linear gains.
    corr_white = white * gains
    w_luma = float(np.dot(corr_white, _LUMA_BGR))
    gamma_votes: list[float] = []
    for name, level in _GREY_LEVELS.items():
        sample = cleaned.get(name)
        if sample is None or w_luma <= 8.0:
            continue
        g_luma = float(np.dot(sample * gains, _LUMA_BGR))
        if g_luma <= 1.0:
            continue
        ratio = float(np.clip(g_luma / w_luma, 1e-3, 0.999))
        expected = float(np.clip(level / 255.0, 1e-3, 0.999))
        # ratio ** gamma ≈ expected
        est = float(np.log(expected) / np.log(ratio))
        if 0.5 <= est <= 2.0:
            gamma_votes.append(est)

    gamma = 1.0
    if gamma_votes:
        gamma = float(np.median(gamma_votes))
        gamma = float(np.clip(gamma, 0.6, 1.8))
        if abs(gamma - 1.0) < 0.05:
            gamma = 1.0
        if len(gamma_votes) >= 2:
            notes.append(f"gamma from {len(gamma_votes)} grey levels")

    for name, channel in (("red", 2), ("green", 1), ("blue", 0)):
        patch = cleaned.get(name)
        if patch is None:
            continue
        peak = float(patch[channel])
        others = float(np.mean([patch[i] for i in range(3) if i != channel]))
        if peak < 20:
            notes.append(f"{name} patch looks crushed (peak {peak:.0f})")
        elif others > peak * 0.85:
            notes.append(f"{name} patch is desaturated (leak {others:.0f} vs {peak:.0f})")

    black = cleaned.get("black")
    if black is not None and float(black.mean()) > 40:
        notes.append(
            f"black patch is bright (mean {float(black.mean()):.0f}) — check ambient light / AE"
        )

    return CalibrationSolution(
        gains_bgr=(float(gains[0]), float(gains[1]), float(gains[2])),
        gamma=gamma,
        patch_means_bgr={
            name: [float(v) for v in mean] for name, mean in cleaned.items()
        },
        notes=notes,
    )


@dataclass
class ColorCalibrationSession:
    """Automated patch sequence driven from the pipeline thread."""

    settle_sec: float = 1.8
    sample_frames: int = 8
    roi_fraction: float = 0.25
    patches: tuple[Patch, ...] = field(
        default_factory=lambda: tuple(
            Patch(name, rgb) for name, rgb in DEFAULT_PATCHES
        )
    )

    state: str = "idle"  # idle | running | ready | error | aborted
    index: int = 0
    phase: str = "idle"  # settle | sampling | done
    error: str = ""
    patch_started_at: float = 0.0
    samples: list[np.ndarray] = field(default_factory=list)
    measurements: dict[str, np.ndarray] = field(default_factory=dict)
    solution: CalibrationSolution | None = None
    preview_mode: bool = False
    #: Bump when display colour changes so the TV page can react.
    display_seq: int = 0

    def display_rgb(self) -> tuple[int, int, int]:
        if self.state == "ready" and self.preview_mode and self.solution is not None:
            # After solve, cycle preview patches from the list.
            patch = self.patches[min(self.index, len(self.patches) - 1)]
            return patch.rgb
        if self.state not in {"running", "ready"} or not self.patches:
            return (0, 0, 0)
        return self.patches[min(self.index, len(self.patches) - 1)].rgb

    def display_name(self) -> str:
        if self.state == "idle":
            return "idle"
        if not self.patches:
            return ""
        return self.patches[min(self.index, len(self.patches) - 1)].name

    def start(self) -> dict[str, Any]:
        self.state = "running"
        self.index = 0
        self.phase = "settle"
        self.error = ""
        self.samples = []
        self.measurements = {}
        self.solution = None
        self.preview_mode = False
        self.patch_started_at = time.monotonic()
        self.display_seq += 1
        return self.status()

    def abort(self) -> dict[str, Any]:
        self.state = "aborted"
        self.phase = "idle"
        self.preview_mode = False
        self.display_seq += 1
        return self.status()

    def tick(self, image: np.ndarray | None) -> None:
        """Advance settle → sample → next patch using one perspective frame."""
        if self.state != "running":
            return
        if image is None:
            return
        now = time.monotonic()
        patch = self.patches[self.index]

        if self.phase == "settle":
            if now - self.patch_started_at < self.settle_sec:
                return
            self.phase = "sampling"
            self.samples = []

        if self.phase == "sampling":
            try:
                mean = sample_center_roi(image, fraction=self.roi_fraction)
            except ValueError as exc:
                self.state = "error"
                self.error = str(exc)
                self.phase = "idle"
                return
            self.samples.append(mean)
            if len(self.samples) < self.sample_frames:
                return
            self.measurements[patch.name] = np.mean(self.samples, axis=0)
            self.samples = []
            if self.index + 1 >= len(self.patches):
                try:
                    self.solution = solve_gains(self.measurements)
                except ValueError as exc:
                    self.state = "error"
                    self.error = str(exc)
                    self.phase = "idle"
                    self.display_seq += 1
                    return
                self.state = "ready"
                self.phase = "done"
                self.preview_mode = True
                self.index = 0
                self.display_seq += 1
                return
            self.index += 1
            self.phase = "settle"
            self.patch_started_at = now
            self.display_seq += 1

    def status(self) -> dict[str, Any]:
        total = len(self.patches)
        rgb = self.display_rgb()
        return {
            "state": self.state,
            "phase": self.phase,
            "error": self.error,
            "index": self.index,
            "total": total,
            "patch": self.display_name(),
            "display_rgb": list(rgb),
            "display_seq": self.display_seq,
            "progress": round(
                (self.index + (0.5 if self.phase == "sampling" else 0.0))
                / max(total, 1),
                3,
            )
            if self.state == "running"
            else (1.0 if self.state == "ready" else 0.0),
            "measurements": {
                name: [round(float(v), 2) for v in mean]
                for name, mean in self.measurements.items()
            },
            "solution": None if self.solution is None else self.solution.as_dict(),
            "preview_mode": self.preview_mode,
            "settle_sec": self.settle_sec,
            "sample_frames": self.sample_frames,
        }


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "DEFAULT_PATCHES",
    "CalibrationSolution",
    "ColorCalibrationSession",
    "Patch",
    "iso_now",
    "sample_center_roi",
    "solve_gains",
]
