"""Colour normalisation.

Cameras lie: cheap sensors have a colour cast, auto-exposure drifts, and the
result is LEDs that are noticeably warmer or duller than the picture they are
supposed to be echoing.

An optional 3×3 BGR matrix (from the solid-patch wizard) runs first.  Then
everything that can be expressed per channel -- white balance gains, exposure
gain, contrast, brightness and gamma -- is folded into one 256-entry lookup
table per channel and applied in a single ``cv2.LUT`` call.  Saturation needs
cross-channel information, so it gets one extra pass.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from processor.config.schema import ColorConfig
from processor.pipeline.context import FrameContext, PipelineState
from processor.pipeline.stage import Stage
from processor.utils.color_calibrate import (
    apply_matrix_bgr,
    is_identity_matrix,
    matrix_from_flat,
)
from processor.utils.smoothing import EMA

#: BGR order, matching OpenCV's channel layout.
_LUMA_WEIGHTS = np.array([0.114, 0.587, 0.299], dtype=np.float32)
_IDENTITY_FLAT = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


def build_lut(
    gains: tuple[float, float, float],
    exposure: float = 1.0,
    contrast: float = 1.0,
    brightness: float = 1.0,
    gamma: float = 1.0,
) -> np.ndarray:
    """A ``(1, 256, 3)`` BGR lookup table combining every per-channel term.

    ``gamma`` is applied as ``v ** (1 / gamma)``, so values above 1 brighten
    the midtones -- the direction most people expect from a "gamma" slider.
    """
    x = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    table = np.empty((1, 256, 3), dtype=np.uint8)

    gamma = float(gamma) if gamma and gamma > 0 else 1.0
    inv_gamma = 1.0 / gamma

    for channel, gain in enumerate(gains):
        v = x * float(gain) * float(exposure)
        if contrast != 1.0:
            v = (v - 0.5) * float(contrast) + 0.5
        if brightness != 1.0:
            v = v * float(brightness)
        v = np.clip(v, 0.0, 1.0)
        if gamma != 1.0:
            v = np.power(v, inv_gamma)
        table[0, :, channel] = np.clip(v * 255.0 + 0.5, 0, 255).astype(np.uint8)

    return table


class ColorStage(Stage):
    name = "color"

    def __init__(self, config: ColorConfig, state: PipelineState):
        super().__init__(config, state)
        self.config: ColorConfig = config
        self._wb = EMA(alpha=max(config.wb_smoothing, 1e-3), initial=np.ones(3))
        self._exposure = EMA(alpha=max(config.exposure.smoothing, 1e-3), initial=1.0)
        self._lut: np.ndarray | None = None
        self._lut_key: tuple | None = None
        self._matrix: np.ndarray | None = None
        self._matrix_key: tuple | None = None
        self._measured = {"gains": [1.0, 1.0, 1.0], "exposure": 1.0, "luma": 0.0}

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        self._wb.reset(np.ones(3))
        self._exposure.reset(1.0)
        self._lut = None
        self._lut_key = None
        self._matrix = None
        self._matrix_key = None

    def on_config_changed(self) -> None:
        self._wb.alpha = max(self.config.wb_smoothing, 1e-3)
        self._exposure.alpha = max(self.config.exposure.smoothing, 1e-3)
        self._lut = None
        self._lut_key = None
        self._matrix = None
        self._matrix_key = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "white_balance": self.config.white_balance,
            "matrix_enabled": bool(self.config.matrix_enabled),
            "gains": [round(g, 3) for g in self._measured["gains"]],
            "exposure_gain": round(float(self._measured["exposure"]), 3),
            "mean_luma": round(float(self._measured["luma"]), 1),
        }

    # -- main --------------------------------------------------------------

    def process(self, ctx: FrameContext) -> None:
        image = ctx.image
        if image.ndim != 3 or image.shape[2] != 3:
            ctx.skipped[self.name] = "not a colour image"
            return

        matrix = self._resolve_matrix()
        if matrix is not None:
            image = apply_matrix_bgr(image, matrix)

        gains = self._resolve_gains(image)
        exposure = self._resolve_exposure(image)

        key = (
            round(gains[0], 3),
            round(gains[1], 3),
            round(gains[2], 3),
            round(exposure, 3),
            round(self.config.contrast, 3),
            round(self.config.brightness, 3),
            round(self.config.gamma, 3),
        )
        if key != self._lut_key or self._lut is None:
            self._lut = build_lut(
                gains,
                exposure=exposure,
                contrast=self.config.contrast,
                brightness=self.config.brightness,
                gamma=self.config.gamma,
            )
            self._lut_key = key

        identity = key == (1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
        if not identity:
            image = cv2.LUT(np.ascontiguousarray(image), self._lut)

        saturation = float(self.config.saturation)
        if abs(saturation - 1.0) > 1e-3:
            grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            grey3 = cv2.cvtColor(grey, cv2.COLOR_GRAY2BGR)
            image = cv2.addWeighted(image, saturation, grey3, 1.0 - saturation, 0.0)

        ctx.set_image(image)
        ctx.record(
            self.name,
            gains=[round(g, 3) for g in gains],
            exposure=round(exposure, 3),
            saturation=saturation,
            gamma=self.config.gamma,
            matrix=bool(matrix is not None),
        )

    # -- measurement -------------------------------------------------------

    def _sample(self, image: np.ndarray) -> np.ndarray:
        """Every 4th pixel; plenty for a global statistic and 16x cheaper."""
        return image[::4, ::4].reshape(-1, 3).astype(np.float32)

    def _resolve_matrix(self) -> np.ndarray | None:
        if not self.config.matrix_enabled:
            return None
        values = list(self.config.matrix or _IDENTITY_FLAT)
        if len(values) != 9:
            return None
        key = (True, *tuple(round(float(v), 5) for v in values))
        if key != self._matrix_key or self._matrix is None:
            mat = matrix_from_flat(values)
            self._matrix = None if is_identity_matrix(mat) else mat.astype(np.float32)
            self._matrix_key = key
        return self._matrix

    def _resolve_gains(self, image: np.ndarray) -> tuple[float, float, float]:
        mode = (self.config.white_balance or "off").lower()

        if mode == "manual":
            g = self.config.gains
            gains = np.array([g.b, g.g, g.r], dtype=np.float64)
        elif mode == "auto":
            means = self._sample(image).mean(axis=0)
            means = np.maximum(means, 1.0)
            # Grey world: assume the average of a whole TV frame is neutral.
            target = float(means.mean())
            raw = target / means
            strength = float(np.clip(self.config.wb_strength, 0.0, 1.0))
            raw = 1.0 + (raw - 1.0) * strength
            # Renormalise so white balance only shifts hue, never overall level
            # -- exposure is a separate control and should stay separate.
            raw = raw / float(np.mean(raw))
            gains = np.asarray(self._wb.update(np.clip(raw, 0.5, 2.0)), dtype=np.float64)
        else:
            gains = np.ones(3, dtype=np.float64)

        self._measured["gains"] = [float(g) for g in gains]
        return (float(gains[0]), float(gains[1]), float(gains[2]))

    def _resolve_exposure(self, image: np.ndarray) -> float:
        cfg = self.config.exposure
        samples = self._sample(image)
        luma = float(np.dot(samples.mean(axis=0), _LUMA_WEIGHTS))
        self._measured["luma"] = luma

        if not cfg.enabled:
            self._exposure.reset(1.0)
            self._measured["exposure"] = 1.0
            return 1.0

        if luma < 1.0:
            # Black screen: holding the last gain avoids a violent pump when
            # the picture comes back.
            gain = float(self._exposure.value or 1.0)
        else:
            gain = float(np.clip(cfg.target_luma / luma, cfg.min_gain, cfg.max_gain))

        smoothed = float(self._exposure.update(gain))
        self._measured["exposure"] = smoothed
        return smoothed
