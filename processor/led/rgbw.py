"""RGB → RGBW for DDP strips that have a dedicated white LED.

The camera and colour stage are RGB. SK6812-style RGBW (warm white ~3000 K)
needs a fourth channel so the box can drive W instead of faking white with R+G+B.
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

    Tanner Helland's fit (the usual LED-firmware formula). 3000 K is a warm
    white close to typical RGBW phosphor channels.
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

    Pull as much of each pixel as possible onto the white LED of ``white_kelvin``
    so a 3000 K channel is not asked to render cool daylight by itself.
    """
    rgb = np.asarray(pixels_rgb, dtype=np.float32)
    if rgb.size == 0:
        return np.zeros((0, 4), dtype=np.uint8)
    rgb = rgb.reshape(-1, 3) / 255.0
    white = kelvin_to_srgb(white_kelvin)
    scale = np.full(rgb.shape[0], 1.0, dtype=np.float32)
    for channel in range(3):
        w = float(white[channel])
        if w > 1e-6:
            scale = np.minimum(scale, rgb[:, channel] / w)
    scale = np.clip(scale, 0.0, 1.0)
    remain = np.clip(rgb - scale[:, None] * white[None, :], 0.0, 1.0)
    out = np.concatenate([remain, scale[:, None]], axis=1)
    return np.clip(np.round(out * 255.0), 0, 255).astype(np.uint8)


def encode_led_pixels(
    pixels_rgb: np.ndarray, color_mode: Any, white_kelvin: float = 3000.0
) -> np.ndarray:
    """RGB samples → the byte layout DDP should send."""
    if normalize_color_mode(color_mode) == "rgbw":
        return rgb_to_rgbw(pixels_rgb, white_kelvin=white_kelvin)
    return np.ascontiguousarray(pixels_rgb, dtype=np.uint8)
