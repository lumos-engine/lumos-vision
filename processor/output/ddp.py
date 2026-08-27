"""DDP output to Lumos OS (``lumos_vision`` plugin) on UDP :4048.

Direct path contract: Vision-converted RGBW, 4 bytes/LED, layout R,G,B,W
(not GRBW). Data type 0 — stride is implied, do not advertise RGBW in the
header. Offsets are in bytes, max 1440 bytes/datagram. One pixel per Lumos
active LED, perimeter top-left clockwise; OS maps logical → physical.

HyperHDR path does not use this sink (3-byte RGB stays on the virtual cam).
"""

from __future__ import annotations

import socket
import time
from typing import Any

import numpy as np

from processor.config.schema import DdpConfig
from processor.led.rgbw import encode_led_pixels
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


def build_packets(pixels: np.ndarray, sequence: int, output_id: int = DDP_ID_DISPLAY) -> list[bytes]:
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

    def open(self, width: int, height: int) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setblocking(False)
        log.info(
            "DDP output to %s:%d (%d LEDs, %s)",
            self.config.host,
            self.config.port,
            self.sampler.layout.count,
            "rgbw",
        )

    def write(self, image: np.ndarray, ctx: Any | None = None) -> bool:
        if self._socket is None:
            return False

        if self.config.fps > 0:
            now = time.monotonic()
            if now < self._next_send:
                return True
            self._next_send = now + 1.0 / self.config.fps

        pixels = encode_led_pixels(
            self._sample(image, ctx),
            "rgbw",
            white_kelvin=float(self.config.white_kelvin or 3000),
        )
        self._sequence = (self._sequence % 15) + 1
        address = (self.config.host, self.config.port)

        for packet in build_packets(pixels, self._sequence):
            try:
                self._socket.sendto(packet, address)
            except BlockingIOError:
                # The socket buffer is full; dropping this frame is strictly
                # better than blocking the pipeline for a light strip.
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
            "color_mode": "rgbw",
            "white_kelvin": int(self.config.white_kelvin or 3000),
            "frames": self._frames,
            "errors": self._errors,
        }


def _corners_from_ctx(ctx: Any | None) -> np.ndarray | None:
    if ctx is None:
        return None
    meta = getattr(ctx, "meta", None) or {}
    recorded = (meta.get("boundary") or {}).get("corners")
    if recorded:
        return np.asarray(recorded, dtype=np.float32).reshape(4, 2)
    return None
