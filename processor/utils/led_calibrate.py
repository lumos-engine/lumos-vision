"""LED-strip colour sync for Direct DDP.

Camera colour cal maps TV → samples. This maps samples → the physical strip:
a 3×3 on logical RGB, plus a separate R/G wire-order. Not an environment
profile — one correction on ``output.ddp``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from processor.led.rgbw import apply_led_matrix
from processor.utils.color_calibrate import iso_now, matrix_from_flat
from processor.utils.logging import get_logger

log = get_logger(__name__)

#: Primaries + secondaries + neutrals. Skin is a camera problem, not diodes.
LED_PATCHES: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("black", (0, 0, 0)),
    ("white", (255, 255, 255)),
    ("grey", (128, 128, 128)),
    ("red", (255, 0, 0)),
    ("green", (0, 255, 0)),
    ("blue", (0, 0, 255)),
    ("cyan", (0, 255, 255)),
    ("magenta", (255, 0, 255)),
    ("yellow", (255, 255, 0)),
)

_REQUIRED = frozenset({"red", "green", "blue", "white"})
_IDENTITY = np.eye(3, dtype=np.float64)


def _clip_rgb(values: np.ndarray | tuple[float, ...] | list[float]) -> tuple[int, int, int]:
    arr = np.clip(np.round(np.asarray(values, dtype=np.float64).reshape(3)), 0, 255)
    return (int(arr[0]), int(arr[1]), int(arr[2]))


def drive_through_matrix(
    intended_rgb: tuple[int, int, int], matrix: np.ndarray | list[float]
) -> tuple[int, int, int]:
    rgb = np.asarray(intended_rgb, dtype=np.float32).reshape(1, 3)
    out = apply_led_matrix(rgb, matrix)
    return (int(out[0, 0]), int(out[0, 1]), int(out[0, 2]))


def solve_led_matrix(
    pairs: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]],
    *,
    ridge: float = 8.0,
) -> tuple[np.ndarray, list[str]]:
    """Fit ``intended @ M ≈ driven`` from named (intended, driven) RGB pairs."""
    notes: list[str] = []
    rows_i: list[np.ndarray] = []
    rows_d: list[np.ndarray] = []
    for name, (intended, driven) in pairs.items():
        if name == "black":
            continue
        intended_arr = np.asarray(intended, dtype=np.float64).reshape(3)
        driven_arr = np.asarray(driven, dtype=np.float64).reshape(3)
        if float(intended_arr.mean()) < 2.0:
            continue
        rows_i.append(intended_arr)
        rows_d.append(driven_arr)

    missing = sorted(_REQUIRED - set(pairs))
    if missing:
        raise ValueError(
            "need red, green, blue, and white before solving "
            f"(missing {', '.join(missing)})"
        )
    if len(rows_i) < 3:
        raise ValueError("need at least 3 usable patches to fit a LED matrix")

    i_mat = np.vstack(rows_i)
    d_mat = np.vstack(rows_d)
    gram = i_mat.T @ i_mat + ridge * np.eye(3)
    matrix = np.zeros((3, 3), dtype=np.float64)
    for col in range(3):
        rhs = i_mat.T @ d_mat[:, col] + ridge * _IDENTITY[:, col]
        matrix[:, col] = np.linalg.solve(gram, rhs)
    matrix = np.clip(matrix, -4.0, 4.0)
    for col in range(3):
        if float(np.linalg.norm(matrix[:, col])) < 1e-3:
            matrix[:, col] = _IDENTITY[:, col]
            notes.append(f"matrix column {col} collapsed — reset to identity")
    if np.allclose(matrix, _IDENTITY, atol=1e-3):
        notes.append("identity — every recorded patch matched the swatch")
    else:
        notes.append("3×3 LED output matrix (logical RGB)")
    return matrix, notes


@dataclass
class LedCalibrationSession:
    """Match/adjust loop that floods the strip with solid patches."""

    patches: tuple[tuple[str, tuple[int, int, int]], ...] = LED_PATCHES
    state: str = "idle"  # idle | running | ready | aborted
    phase: str = "idle"  # waiting | adjusting
    index: int = 0
    error: str = ""
    matrix: np.ndarray = field(default_factory=lambda: _IDENTITY.copy())
    #: patch name → (intended RGB, driven RGB)
    records: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = field(
        default_factory=dict
    )
    adjust_rgb: tuple[int, int, int] | None = None
    solution: list[float] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def patch(self) -> tuple[str, tuple[int, int, int]]:
        return self.patches[min(self.index, len(self.patches) - 1)]

    def intended_rgb(self) -> tuple[int, int, int]:
        return self.patch[1]

    def drive_rgb(self) -> tuple[int, int, int]:
        if self.phase == "adjusting" and self.adjust_rgb is not None:
            return self.adjust_rgb
        return drive_through_matrix(self.intended_rgb(), self.matrix)

    def start(self, matrix: list[float] | np.ndarray | None = None) -> dict[str, Any]:
        self.matrix = (
            _IDENTITY.copy()
            if matrix is None
            else matrix_from_flat(matrix)
        )
        self.state = "running"
        self.phase = "waiting"
        self.index = 0
        self.error = ""
        self.records = {}
        self.adjust_rgb = None
        self.solution = None
        self.notes = []
        return self.status()

    def abort(self) -> dict[str, Any]:
        self.state = "aborted"
        self.phase = "idle"
        self.adjust_rgb = None
        self.error = ""
        return self.status()

    def match(self) -> dict[str, Any]:
        self._require_running()
        intended = self.intended_rgb()
        self.records[self.patch[0]] = (intended, self.drive_rgb())
        self.phase = "waiting"
        self.adjust_rgb = None
        self.solution = None
        self._advance_if_possible()
        return self.status()

    def begin_adjust(self) -> dict[str, Any]:
        self._require_running()
        self.phase = "adjusting"
        self.adjust_rgb = self.drive_rgb()
        self.solution = None
        return self.status()

    def set_drive(self, r: int, g: int, b: int) -> dict[str, Any]:
        self._require_running()
        if self.phase != "adjusting":
            self.phase = "adjusting"
        self.adjust_rgb = _clip_rgb((r, g, b))
        return self.status()

    def commit_adjust(self) -> dict[str, Any]:
        self._require_running()
        if self.phase != "adjusting" or self.adjust_rgb is None:
            raise ValueError("start adjusting this patch before saving the drive")
        intended = self.intended_rgb()
        self.records[self.patch[0]] = (intended, self.adjust_rgb)
        self.phase = "waiting"
        self.adjust_rgb = None
        self.solution = None
        self._advance_if_possible()
        return self.status()

    def goto(self, *, index: int | None = None, patch: str | None = None) -> dict[str, Any]:
        self._require_running()
        if patch is not None:
            names = [name for name, _rgb in self.patches]
            if patch not in names:
                raise ValueError(f"unknown patch {patch!r}")
            index = names.index(patch)
        if index is None:
            raise ValueError("index or patch is required")
        index = int(index)
        if index < 0 or index >= len(self.patches):
            raise ValueError(f"index out of range (0..{len(self.patches) - 1})")
        self.index = index
        self.phase = "waiting"
        self.adjust_rgb = None
        if self.state == "ready" and self.solution is None:
            self.state = "running"
        return self.status()

    def next_patch(self) -> dict[str, Any]:
        return self.goto(index=min(self.index + 1, len(self.patches) - 1))

    def prev_patch(self) -> dict[str, Any]:
        return self.goto(index=max(self.index - 1, 0))

    def solve(self) -> dict[str, Any]:
        self._require_running()
        try:
            matrix, notes = solve_led_matrix(self.records)
        except ValueError as exc:
            self.error = str(exc)
            self.state = "running"
            raise
        self.matrix = matrix
        self.solution = [float(v) for v in matrix.reshape(9)]
        self.notes = notes
        self.state = "ready"
        self.phase = "waiting"
        self.adjust_rgb = None
        self.error = ""
        return self.status()

    def _advance_if_possible(self) -> None:
        if self.index < len(self.patches) - 1:
            self.index += 1

    def _require_running(self) -> None:
        if self.state not in {"running", "ready"}:
            raise ValueError("start a LED colour sync session first")

    def status(self) -> dict[str, Any]:
        name, intended = self.patch
        recorded = [
            {
                "name": patch_name,
                "intended": list(pair[0]),
                "driven": list(pair[1]),
            }
            for patch_name, pair in self.records.items()
        ]
        return {
            "state": self.state,
            "phase": self.phase,
            "index": self.index,
            "total": len(self.patches),
            "recorded": len(self.records),
            "progress": (len(self.records) / float(len(self.patches)))
            if self.patches
            else 0.0,
            "patch": name if self.state not in {"idle", "aborted"} else "idle",
            "intended": list(intended),
            "drive": list(self.drive_rgb())
            if self.state in {"running", "ready"}
            else [0, 0, 0],
            "error": self.error,
            "notes": list(self.notes),
            "matrix": [float(v) for v in np.asarray(self.matrix).reshape(9)],
            "solution": self.solution,
            "records": recorded,
            "patches": [p[0] for p in self.patches],
            "can_solve": _REQUIRED.issubset(self.records)
            and self.state in {"running", "ready"},
            "calibrated_at": iso_now() if self.state == "ready" else "",
        }


__all__ = [
    "LED_PATCHES",
    "LedCalibrationSession",
    "drive_through_matrix",
    "solve_led_matrix",
]
