"""RGB → RGBW for Direct DDP (Lumos OS ``lumos_vision`` plugin).

Camera samples stay RGB. Direct sends 4-byte ``R,G,B,W``. HyperHDR stays
3-byte RGB; the box converts there.

Do **not** project samples onto the 3000 K white-LED chromaticity. Camera
pixels of a TV are weakly saturated, so that fold dumps almost every LED onto
W and the strip looks white with a hint of leftover blue. Extract the shared
component instead (``W = min(R,G,B)``): saturated red keeps W ≈ 0, screen white
rides W (the SK6812 diode is already 3000 K). ``white_kelvin`` is the strip spec,
not a second white extraction.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

_RGBW_ALIASES = frozenset({"rgbw", "sk6812", "rgbw3000"})


def normalize_color_mode(value: Any) -> str:
    text = str(value or "rgb").strip().lower()
    if text in _RGBW_ALIASES:
        return "rgbw"
    return "rgb"


def kelvin_to_srgb(kelvin: float) -> np.ndarray:
    """Approximate D65-ish sRGB of a black-body, channels in 0..1.

    Tanner Helland's fit. Kept for the strip-spec UI and tests; Direct DDP
    does not fold pixels onto this chromaticity.
    """
    k = float(np.clip(kelvin, 1000.0, 40000.0)) / 100.0
    if k <= 66.0:
        red = 255.0
        green = 99.4708025861 * math.log(k) - 161.1195681661
    else:
        red = 329.698727446 * ((k - 60.0) ** -0.1332047592)
        green = 288.1221695283 * ((k - 60.0) ** -0.0755148492)
    if k >= 66.0:
        blue = 255.0
    elif k <= 19.0:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(k - 10.0) - 305.0447927307
    return np.clip(np.array([red, green, blue], dtype=np.float32) / 255.0, 0.0, 1.0)


def rgb_to_rgbw(pixels_rgb: np.ndarray, white_kelvin: float = 3000.0) -> np.ndarray:
    """``(N, 3)`` RGB uint8 → ``(N, 4)`` RGBW uint8.

    ExtractMin: ``W = min(R, G, B)``, leftover on R/G/B. ``white_kelvin`` is
    unused in the extract (the W diode already is that CCT).
    """
    _ = white_kelvin
    rgb = np.asarray(pixels_rgb, dtype=np.float32)
    if rgb.size == 0:
        return np.zeros((0, 4), dtype=np.uint8)
    rgb = rgb.reshape(-1, 3)
    white = np.min(rgb, axis=1, keepdims=True)
    remain = np.clip(rgb - white, 0.0, 255.0)
    out = np.concatenate([remain, white], axis=1)
    return np.clip(np.round(out), 0, 255).astype(np.uint8)


def encode_led_pixels(
    pixels_rgb: np.ndarray, color_mode: Any, white_kelvin: float = 3000.0
) -> np.ndarray:
    """RGB samples → the byte layout DDP should send."""
    if normalize_color_mode(color_mode) == "rgbw":
        return rgb_to_rgbw(pixels_rgb, white_kelvin=white_kelvin)
    return np.ascontiguousarray(pixels_rgb, dtype=np.uint8)
