"""DDP output to Lumos OS (``lumos_vision`` plugin) on UDP :4048.

``rgb``: 3 bytes/LED. ``rgbw`` / ``rgbw_off``: 4 bytes/LED, layout R,G,B,W
(not GRBW). Data type 0 — stride is implied. Offsets in bytes, max 1440
bytes/datagram. One pixel per Lumos active LED, perimeter top-left clockwise;
OS maps logical → physical.

HyperHDR path does not use this sink.
"""

from __future__ import annotations

import socket
import time
from typing import Any

import numpy as np

from processor.config.schema import DdpConfig
from processor.led.rgbw import normalize_color_mode, wire_led_pixels
from processor.led.sampler import LedLayout, LedSampler, panel_insets_from_meta
from processor.output.base import Sink
from processor.utils.logging import get_logger

log = get_logger(__name__)

DDP_PORT = 4048
DDP_HEADER_LEN = 10
DDP_FLAGS1_VER1 = 0x40
DDP_FLAGS1_PUSH = 0x01
DDP_ID_DISPLAY = 1
#: Keep each datagram inside a typical 1500-byte MTU: 360 RGBW LEDs × 4 bytes.
DDP_MAX_PAYLOAD = 1440


def build_packets(
    pixels: np.ndarray, sequence: int, output_id: int = DDP_ID_DISPLAY
) -> list[bytes]:
    """Split an RGB array into DDP datagrams, PUSH set on the last one."""
    data = np.ascontiguousarray(pixels, dtype=np.uint8).tobytes()
    packets: list[bytes] = []
    total = len(data)
    offset = 0

    while offset < total:
        chunk = data[offset : offset + DDP_MAX_PAYLOAD]
        is_last = offset + len(chunk) >= total
        flags1 = DDP_FLAGS1_VER1 | (DDP_FLAGS1_PUSH if is_last else 0)
        header = bytes(
            [
                flags1,
                sequence & 0x0F,
                0,  # data type: 0 lets the receiver use its configured format
                output_id,
                (offset >> 24) & 0xFF,
                (offset >> 16) & 0xFF,
                (offset >> 8) & 0xFF,
                offset & 0xFF,
                (len(chunk) >> 8) & 0xFF,
                len(chunk) & 0xFF,
            ]
        )
        packets.append(header + chunk)
        offset += len(chunk)

    return packets


class DdpSink(Sink):
    name = "ddp"

    def __init__(self, config: DdpConfig):
        self.config = config
        if not config.host:
            raise ValueError("output.ddp.host is required when DDP output is enabled")

        self.sampler = LedSampler(
            LedLayout(
                top=config.leds_top,
                right=config.leds_right,
                bottom=config.leds_bottom,
                left=config.leds_left,
                depth=config.sample_depth,
                start_corner=config.start_corner,
                clockwise=config.clockwise,
            ),
            smoothing=config.smoothing,
        )
        if self.sampler.layout.count == 0:
            raise ValueError("output.ddp needs at least one non-zero leds_* count")

        self._socket: socket.socket | None = None
        self._sequence = 0
        self._frames = 0
        self._errors = 0
        self._next_send = 0.0
        #: After ``hold_off``, skip further sends until ``resume``.
        self._held_off = False
        #: Flood RGB (logical) while tests / LED cal pause camera sampling.
        self._override: np.ndarray | None = None
        self._override_apply_matrix = True
        self._override_white = False

    def open(self, width: int, height: int) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setblocking(False)
        log.info(
            "DDP output to %s:%d (%d LEDs, %s)",
            self.config.host,
            self.config.port,
            self.sampler.layout.count,
            normalize_color_mode(self.config.color_mode),
        )

    def write(self, image: np.ndarray, ctx: Any | None = None) -> bool:
        if self._socket is None:
            return False
        if self._override is not None:
            return self._send_override()
        if self._held_off:
            return True

        if self.config.fps > 0:
            now = time.monotonic()
            if now < self._next_send:
                return True
            self._next_send = now + 1.0 / self.config.fps

        return self._send_pixels(self._sample(image, ctx))

    def hold_off(self) -> bool:
        """Send one black frame and stop so a dead stream does not sit on colour."""
        if self._override is not None:
            return True
        if self._held_off:
            return True
        count = self.sampler.layout.count
        black = np.zeros((count, 3), dtype=np.uint8)
        ok = self._send_pixels(black)
        self._held_off = True
        return ok

    def resume(self) -> None:
        self._held_off = False

    def flood(
        self,
        rgb: tuple[int, int, int] | np.ndarray,
        *,
        apply_matrix: bool = True,
        white: bool = False,
    ) -> bool:
        """Bypass the camera and hold every LED at ``rgb`` (or W-only)."""
        self._override = np.asarray(rgb, dtype=np.uint8).reshape(3)
        self._override_apply_matrix = bool(apply_matrix)
        self._override_white = bool(white)
        return self._send_override()

    def clear_flood(self) -> None:
        self._override = None
        self._override_apply_matrix = True
        self._override_white = False

    def _send_override(self) -> bool:
        if self._override is None:
            return True
        count = self.sampler.layout.count
        rgb = np.broadcast_to(self._override.reshape(1, 3), (count, 3)).copy()
        return self._send_pixels(
            rgb,
            apply_matrix=self._override_apply_matrix,
            white_flood=self._override_white,
        )

    def _send_pixels(
        self,
        rgb: np.ndarray,
        *,
        apply_matrix: bool = True,
        white_flood: bool = False,
    ) -> bool:
        if self._socket is None:
            return False
        pixels = wire_led_pixels(
            rgb,
            self.config.color_mode,
            rgb_order=self.config.rgb_order,
            matrix=self.config.color_matrix,
            white_kelvin=float(self.config.white_kelvin or 3000),
            white_gain=float(self.config.white_gain),
            apply_matrix=apply_matrix,
            white_flood=white_flood,
        )
        self._sequence = (self._sequence % 15) + 1
        address = (self.config.host, self.config.port)
        for packet in build_packets(pixels, self._sequence):
            try:
                self._socket.sendto(packet, address)
            except BlockingIOError:
                self._errors += 1
                return True
            except OSError as exc:
                self._errors += 1
                if self._errors <= 3:
                    log.warning("DDP send failed: %s", exc)
                return False
        self._frames += 1
        return True

    def _sample(self, image: np.ndarray, ctx: Any | None) -> np.ndarray:
        corners = _corners_from_ctx(ctx)
        source = getattr(ctx, "source", None) if ctx is not None else None
        if corners is not None and source is not None:
            insets = panel_insets_from_meta(getattr(ctx, "meta", None))
            return self.sampler.sample_quad(
                source,
                corners,
                insets=insets,
                black_level=getattr(ctx, "color_black_level", None),
                matrix=getattr(ctx, "color_matrix", None),
                lut=getattr(ctx, "color_lut", None),
                saturation=float(getattr(ctx, "color_saturation", 1.0) or 1.0),
            )
        return self.sampler.sample(image)

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "target": f"{self.config.host}:{self.config.port}",
            "leds": self.sampler.layout.count,
            "color_mode": normalize_color_mode(self.config.color_mode),
            "rgb_order": self.config.rgb_order,
            "white_kelvin": int(self.config.white_kelvin or 3000),
            "white_gain": float(self.config.white_gain),
            "frames": self._frames,
            "errors": self._errors,
            "held_off": self._held_off,
            "flood": self._override is not None,
        }


def _corners_from_ctx(ctx: Any | None) -> np.ndarray | None:
    if ctx is None:
        return None
    meta = getattr(ctx, "meta", None) or {}
    recorded = (meta.get("boundary") or {}).get("corners")
    if recorded:
        return np.asarray(recorded, dtype=np.float32).reshape(4, 2)
    return None
