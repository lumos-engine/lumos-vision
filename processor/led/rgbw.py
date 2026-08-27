"""RGB / RGBW packing for Direct DDP (Lumos OS ``lumos_vision`` plugin).

Direct always sends 4 bytes/LED (``R,G,B,W``). ``rgb`` leaves W at 0 so an
RGBW strip behaves like RGB. ``rgbw`` drives the white diode (set
``white_kelvin`` to the phosphor: 3000 K today, 6500 K later). HyperHDR stays
3-byte RGB; the box converts there.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

_RGBW_ALIASES = frozenset({"rgbw", "sk6812", "rgbw3000", "rgbw6500"})
_RGB_ALIASES = frozenset({"rgb", "rgb_only", "rgbw_off"})
#: How much of the gray component rides the warm W diode vs RGB fill.
DEFAULT_WHITE_GAIN = 0.35


def normalize_color_mode(value: Any) -> str:
    """``rgb`` = W off (4-byte RGB0). ``rgbw`` = use the white diode."""
    text = str(value or "rgbw").strip().lower()
    if text in _RGB_ALIASES:
        return "rgb"
    if text in _RGBW_ALIASES:
        return "rgbw"
    return "rgbw"


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
    """RGB samples → 4-byte DDP layout (Lumos OS Direct never uses 3-byte RGB)."""
    if normalize_color_mode(color_mode) == "rgbw":
        return rgb_to_rgbw(
            pixels_rgb, white_kelvin=white_kelvin, white_gain=white_gain
        )
    return rgb_as_rgbw(pixels_rgb)
