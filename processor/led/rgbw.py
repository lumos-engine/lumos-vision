"""RGB / RGBW packing for Direct DDP (Lumos OS ``lumos_vision`` plugin).

``rgb`` is a 3-byte strip (WS2812 / WS2815). ``rgbw_off`` is an RGBW strip
with W held at 0 (same 4-byte stride, white diode unused). ``rgbw`` drives W
(set ``white_kelvin`` to the phosphor). HyperHDR stays 3-byte RGB; the box
converts there.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

_RGB_ALIASES = frozenset({"rgb", "rgb3", "ws2812", "ws2815"})
_RGBW_OFF_ALIASES = frozenset({"rgbw_off", "rgb_only", "rgb0"})
_RGBW_ALIASES = frozenset({"rgbw", "sk6812", "rgbw3000", "rgbw6500"})
_RGB_ORDER_GRB = frozenset({"grb", "grbw"})
DEFAULT_WHITE_GAIN = 0.35
IDENTITY_RGB_FLAT: list[float] = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


def normalize_color_mode(value: Any) -> str:
    """``rgb`` | ``rgbw_off`` | ``rgbw``."""
    text = str(value or "rgbw").strip().lower()
    if text in _RGB_ALIASES:
        return "rgb"
    if text in _RGBW_OFF_ALIASES:
        return "rgbw_off"
    if text in _RGBW_ALIASES:
        return "rgbw"
    return "rgbw"


def bytes_per_led(color_mode: Any) -> int:
    return 3 if normalize_color_mode(color_mode) == "rgb" else 4


def normalize_rgb_order(value: Any) -> str:
    """``rgb`` (and RGBW) or ``grb`` (and GRBW). Wire order of the first three diodes."""
    text = str(value or "rgb").strip().lower()
    if text in _RGB_ORDER_GRB:
        return "grb"
    return "rgb"


def apply_led_matrix(pixels_rgb: np.ndarray, matrix: Any) -> np.ndarray:
    """``(N, 3)`` RGB uint8 → same shape after ``rgb @ matrix``."""
    rgb = np.asarray(pixels_rgb, dtype=np.float32)
    if rgb.size == 0:
        return np.zeros((0, 3), dtype=np.uint8)
    rgb = rgb.reshape(-1, 3)
    if matrix is None:
        return np.clip(np.round(rgb), 0, 255).astype(np.uint8)
    mat = np.asarray(matrix, dtype=np.float32).reshape(3, 3)
    if np.allclose(mat, np.eye(3, dtype=np.float32), atol=1e-4):
        return np.clip(np.round(rgb), 0, 255).astype(np.uint8)
    out = rgb @ mat
    return np.clip(np.round(out), 0, 255).astype(np.uint8)


def kelvin_to_srgb(kelvin: float) -> np.ndarray:
    """Approximate D65-ish sRGB of a black-body, channels in 0..1.

    Tanner Helland's fit. Used to know what the W diode actually emits so
    leftover R/G/B can cancel the yellow.
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


def rgb_to_rgbw(
    pixels_rgb: np.ndarray,
    white_kelvin: float = 3000.0,
    white_gain: float = DEFAULT_WHITE_GAIN,
) -> np.ndarray:
    """``(N, 3)`` RGB uint8 → ``(N, 4)`` RGBW uint8.

    Chroma stays on R/G/B. Gray is a mix of the 3000 K W diode and a cool RGB
    fill so screen white does not become lamp yellow. ``white_gain`` 0 = RGB
    whites; 1 = maximum W plus complementary leftover.
    """
    gain = float(np.clip(white_gain, 0.0, 1.0))
    rgb = np.asarray(pixels_rgb, dtype=np.float32)
    if rgb.size == 0:
        return np.zeros((0, 4), dtype=np.uint8)
    rgb = rgb.reshape(-1, 3)
    minc = np.min(rgb, axis=1, keepdims=True)
    chroma = np.clip(rgb - minc, 0.0, 255.0)
    white = minc * gain
    led_srgb = kelvin_to_srgb(white_kelvin).reshape(1, 3)
    rgb_from_w = white * led_srgb
    fill = np.clip(minc - rgb_from_w, 0.0, 255.0)
    remain = np.clip(chroma + fill, 0.0, 255.0)
    out = np.concatenate([remain, white], axis=1)
    return np.clip(np.round(out), 0, 255).astype(np.uint8)


def rgb_as_rgbw(pixels_rgb: np.ndarray) -> np.ndarray:
    """``(N, 3)`` RGB → ``(N, 4)`` RGB0. Same stride as RGBW; W diode stays off."""
    rgb = np.ascontiguousarray(pixels_rgb, dtype=np.uint8).reshape(-1, 3)
    if rgb.size == 0:
        return np.zeros((0, 4), dtype=np.uint8)
    zeros = np.zeros((rgb.shape[0], 1), dtype=np.uint8)
    return np.concatenate([rgb, zeros], axis=1)


def encode_led_pixels(
    pixels_rgb: np.ndarray,
    color_mode: Any,
    white_kelvin: float = 3000.0,
    white_gain: float = DEFAULT_WHITE_GAIN,
) -> np.ndarray:
    """RGB samples → DDP byte layout for ``color_mode``."""
    mode = normalize_color_mode(color_mode)
    if mode == "rgbw":
        return rgb_to_rgbw(
            pixels_rgb, white_kelvin=white_kelvin, white_gain=white_gain
        )
    if mode == "rgbw_off":
        return rgb_as_rgbw(pixels_rgb)
    rgb = np.ascontiguousarray(pixels_rgb, dtype=np.uint8)
    if rgb.size == 0:
        return np.zeros((0, 3), dtype=np.uint8)
    return rgb.reshape(-1, 3)


def wire_led_pixels(
    pixels_rgb: np.ndarray,
    color_mode: Any,
    *,
    rgb_order: Any = "rgb",
    matrix: Any = None,
    white_kelvin: float = 3000.0,
    white_gain: float = DEFAULT_WHITE_GAIN,
    apply_matrix: bool = True,
    white_flood: bool = False,
) -> np.ndarray:
    """Logical RGB → optional 3×3 → pack → R/G swap for GRB(W) wire order.

    White extract for ``rgbw`` runs on logical RGB so hue is not scrambled
    before the W diode is filled. Test-W sets the W byte and leaves RGB at 0.
    """
    rgb = np.ascontiguousarray(pixels_rgb, dtype=np.uint8).reshape(-1, 3)
    if white_flood:
        rgb = np.zeros_like(rgb)
        packed = encode_led_pixels(
            rgb, color_mode, white_kelvin=white_kelvin, white_gain=white_gain
        )
        if packed.ndim == 2 and packed.shape[1] == 4:
            packed = packed.copy()
            packed[:, 3] = 255
        return packed
    if apply_matrix:
        rgb = apply_led_matrix(rgb, matrix)
    packed = encode_led_pixels(
        rgb, color_mode, white_kelvin=white_kelvin, white_gain=white_gain
    )
    if normalize_rgb_order(rgb_order) != "grb" or packed.size == 0:
        return packed
    packed = packed.copy()
    packed[:, [0, 1]] = packed[:, [1, 0]]
    return packed
