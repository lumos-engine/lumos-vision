"""Solid-patch colour calibration for the Screen Sight colour stage.

Shows known RGB patches on the TV (via the calibrate-display page), samples the
centre of the perspective-corrected frame, and solves a 3×3 BGR correction
matrix (+ gamma) applied by :class:`~processor.stages.color.ColorStage`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

#: Patches for an occasional manual run (sRGB 0–255, display RGB order).
DEFAULT_PATCHES: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("black", (0, 0, 0)),
    ("white", (255, 255, 255)),
    ("grey_dark", (64, 64, 64)),
    ("grey", (128, 128, 128)),
    ("grey_light", (192, 192, 192)),
    ("red", (255, 0, 0)),
    ("green", (0, 255, 0)),
    ("blue", (0, 0, 255)),
    ("cyan", (0, 255, 255)),
    ("magenta", (255, 0, 255)),
    ("yellow", (255, 255, 0)),
    ("red_mid", (128, 0, 0)),
    ("green_mid", (0, 128, 0)),
    ("blue_mid", (0, 0, 128)),
    ("yellow_mid", (128, 128, 0)),
    ("skin_light", (225, 185, 155)),
    ("skin_medium", (190, 140, 105)),
    ("skin_deep", (145, 95, 65)),
)

#: Neutral patches used for gamma (name → nominal sRGB level).
_GREY_LEVELS: dict[str, float] = {
    "grey_dark": 64.0,
    "grey": 128.0,
    "grey_light": 192.0,
}

#: Per-patch weights for the matrix least-squares fit.
_PATCH_WEIGHTS: dict[str, float] = {
    "white": 2.0,
    "grey_light": 1.6,
    "grey": 1.8,
    "grey_dark": 1.2,
    "red": 1.0,
    "green": 1.0,
    "blue": 1.0,
    "cyan": 1.1,
    "magenta": 1.1,
    "yellow": 1.2,
    "red_mid": 0.9,
    "green_mid": 0.9,
    "blue_mid": 0.9,
    "yellow_mid": 1.0,
    "skin_light": 2.2,
    "skin_medium": 2.4,
    "skin_deep": 2.2,
}

_IDENTITY_3 = np.eye(3, dtype=np.float64)
_LUMA_BGR = np.array([0.114, 0.587, 0.299], dtype=np.float64)
IDENTITY_MATRIX_FLAT: list[float] = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


def patch_targets_bgr() -> dict[str, np.ndarray]:
    """Name → target BGR (0–255) for every default patch."""
    out: dict[str, np.ndarray] = {}
    for name, rgb in DEFAULT_PATCHES:
        r, g, b = rgb
        out[name] = np.array([b, g, r], dtype=np.float64)
    return out


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
    #: OpenCV BGR channel gains (kept ~1 when a matrix carries the cast).
    gains_bgr: tuple[float, float, float]
    gamma: float
    #: 3×3 row-major BGR matrix: ``corrected = measured @ matrix``.
    matrix_bgr: np.ndarray
    patch_means_bgr: dict[str, list[float]]
    notes: list[str] = field(default_factory=list)

    def gains_rgb(self) -> tuple[float, float, float]:
        b, g, r = self.gains_bgr
        return (r, g, b)

    def matrix_flat(self) -> list[float]:
        return [float(v) for v in np.asarray(self.matrix_bgr).reshape(9)]

    def as_dict(self) -> dict[str, Any]:
        r, g, b = self.gains_rgb()
        return {
            "gains": {"r": round(r, 4), "g": round(g, 4), "b": round(b, 4)},
            "gamma": round(float(self.gamma), 4),
            "matrix": [round(v, 5) for v in self.matrix_flat()],
            "matrix_enabled": True,
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


def flatten_matrix(matrix: np.ndarray | list[float]) -> list[float]:
    arr = np.asarray(matrix, dtype=np.float64).reshape(9)
    return [float(v) for v in arr]


def matrix_from_flat(values: list[float] | tuple[float, ...] | np.ndarray) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float64).reshape(3, 3)
    return arr


def is_identity_matrix(matrix: np.ndarray | list[float], *, atol: float = 1e-4) -> bool:
    return bool(np.allclose(matrix_from_flat(matrix), _IDENTITY_3, atol=atol))


def apply_matrix_bgr(image: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Apply ``corrected = bgr @ matrix`` per pixel; clip to uint8."""
    mat = np.asarray(matrix, dtype=np.float32).reshape(3, 3)
    flat = image.reshape(-1, 3).astype(np.float32)
    out = flat @ mat
    out = np.clip(out, 0.0, 255.0)
    return out.reshape(image.shape).astype(np.uint8)


def solve_matrix(
    means_bgr: dict[str, np.ndarray | list[float]],
    *,
    ridge: float = 12.0,
    coeff_clip: float = 2.5,
) -> tuple[np.ndarray, list[str]]:
    """Weighted least-squares 3×3: ``measured @ A ≈ target``, ridge toward I."""
    notes: list[str] = []
    targets = patch_targets_bgr()
    rows_m: list[np.ndarray] = []
    rows_t: list[np.ndarray] = []
    weights: list[float] = []

    for name, mean in means_bgr.items():
        if name == "black":
            continue
        if name not in targets:
            continue
        meas = np.asarray(mean, dtype=np.float64).reshape(3)
        if float(meas.mean()) < 2.0 and name != "white":
            notes.append(f"skip {name}: too dark to fit")
            continue
        rows_m.append(meas)
        rows_t.append(targets[name])
        weights.append(float(_PATCH_WEIGHTS.get(name, 1.0)))

    if len(rows_m) < 3:
        raise ValueError("need at least 3 usable patches to fit a colour matrix")

    m_mat = np.vstack(rows_m)  # N×3
    t_mat = np.vstack(rows_t)
    w = np.asarray(weights, dtype=np.float64)
    sqrt_w = np.sqrt(w)

    # Solve each output channel: (M'WM + λI) a = M'W t + λ e_j
    a_mat = np.zeros((3, 3), dtype=np.float64)
    mw = m_mat * sqrt_w[:, None]
    gram = mw.T @ mw + ridge * np.eye(3)
    for col in range(3):
        rhs = mw.T @ (t_mat[:, col] * sqrt_w) + ridge * _IDENTITY_3[:, col]
        a_mat[:, col] = np.linalg.solve(gram, rhs)

    a_mat = np.clip(a_mat, -coeff_clip, coeff_clip)
    # Keep columns roughly sane: no all-zero column.
    for col in range(3):
        if float(np.linalg.norm(a_mat[:, col])) < 1e-3:
            a_mat[:, col] = _IDENTITY_3[:, col]
            notes.append(f"matrix column {col} collapsed — reset to identity")

    return a_mat, notes


def _estimate_gamma(
    cleaned: dict[str, np.ndarray], matrix: np.ndarray
) -> tuple[float, list[str]]:
    notes: list[str] = []
    white = cleaned.get("white")
    if white is None:
        return 1.0, notes
    corr_white = white @ matrix
    w_luma = float(np.dot(corr_white, _LUMA_BGR))
    gamma_votes: list[float] = []
    for name, level in _GREY_LEVELS.items():
        sample = cleaned.get(name)
        if sample is None or w_luma <= 8.0:
            continue
        g_luma = float(np.dot(sample @ matrix, _LUMA_BGR))
        if g_luma <= 1.0:
            continue
        ratio = float(np.clip(g_luma / w_luma, 1e-3, 0.999))
        expected = float(np.clip(level / 255.0, 1e-3, 0.999))
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
    return gamma, notes


def solve_calibration(
    means_bgr: dict[str, np.ndarray | list[float]],
) -> CalibrationSolution:
    """Fit 3×3 matrix + gamma from measured patch means."""
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

    matrix, notes = solve_matrix(cleaned)
    gamma, gamma_notes = _estimate_gamma(cleaned, matrix)
    notes.extend(gamma_notes)

    # Sanity on primaries / skin after correction.
    targets = patch_targets_bgr()
    for name in ("skin_medium", "yellow", "red"):
        if name not in cleaned or name not in targets:
            continue
        corr = cleaned[name] @ matrix
        err = float(np.linalg.norm(corr - targets[name]))
        if err > 80:
            notes.append(f"{name} residual high ({err:.0f})")

    black = cleaned.get("black")
    if black is not None and float(black.mean()) > 40:
        notes.append(
            f"black patch is bright (mean {float(black.mean()):.0f}) — check ambient light / AE"
        )

    notes.append("3x3 matrix + gamma")

    return CalibrationSolution(
        gains_bgr=(1.0, 1.0, 1.0),
        gamma=gamma,
        matrix_bgr=matrix,
        patch_means_bgr={
            name: [float(v) for v in mean] for name, mean in cleaned.items()
        },
        notes=notes,
    )


def solve_gains(
    means_bgr: dict[str, np.ndarray | list[float]],
    *,
    gain_min: float = 0.5,
    gain_max: float = 2.0,
) -> CalibrationSolution:
    """Backward-compatible name — now returns a full matrix calibration."""
    del gain_min, gain_max
    return solve_calibration(means_bgr)


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
                    self.solution = solve_calibration(self.measurements)
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
    "IDENTITY_MATRIX_FLAT",
    "CalibrationSolution",
    "ColorCalibrationSession",
    "Patch",
    "apply_matrix_bgr",
    "flatten_matrix",
    "is_identity_matrix",
    "iso_now",
    "matrix_from_flat",
    "patch_targets_bgr",
    "sample_center_roi",
    "solve_calibration",
    "solve_gains",
    "solve_matrix",
]
