"""V4L2 loopback output -- the virtual webcam HyperHDR captures.

Talks to ``/dev/videoN`` directly: one ``VIDIOC_S_FMT`` ioctl to declare the
format, then a plain ``write()`` per frame.  That keeps the dependency list at
zero and the per-frame cost at one colour conversion plus one syscall.

Requires the ``v4l2loopback`` kernel module (Linux only)::

    sudo apt install v4l2loopback-dkms v4l2loopback-utils
    sudo modprobe v4l2loopback video_nr=10 card_label="Screen Sight" exclusive_caps=1

``exclusive_caps=1`` matters: without it the device advertises both capture and
output capabilities and many consumers, HyperHDR included, refuse to open it.
"""

from __future__ import annotations

import fcntl
import os
import struct
import sys
import time
from typing import Any

import cv2
import numpy as np

from processor.config.schema import V4L2Config
from processor.output.base import Sink
from processor.utils.logging import get_logger

log = get_logger(__name__)

# include/uapi/linux/videodev2.h
V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
V4L2_FIELD_NONE = 1
V4L2_COLORSPACE_SRGB = 8
V4L2_QUANTIZATION_DEFAULT = 0
V4L2_QUANTIZATION_FULL_RANGE = 1
V4L2_QUANTIZATION_LIM_RANGE = 2

#: _IOWR('V', 5, struct v4l2_format), where sizeof(struct v4l2_format) == 208.
VIDIOC_S_FMT = 0xC0D05605
VIDIOC_G_FMT = 0xC0D05604
_V4L2_FORMAT_SIZE = 208


def fourcc(code: str) -> int:
    a, b, c, d = code.ljust(4)[:4].encode("ascii")
    return a | (b << 8) | (c << 16) | (d << 24)


PIXEL_FORMATS = {
    "YUYV": (fourcc("YUYV"), 2.0),
    "YUY2": (fourcc("YUYV"), 2.0),
    "RGB24": (fourcc("RGB3"), 3.0),
    "BGR24": (fourcc("BGR3"), 3.0),
}


def bgr_to_yuyv(image: np.ndarray) -> np.ndarray:
    """Pack BGR into full-range YUYV 4:2:2.

    ``COLOR_BGR2YCrCb`` is full-range BT.601 and runs in SIMD C++, so the only
    Python-side work is interleaving -- a few hundred microseconds at 640x360.
    """
    height, width = image.shape[:2]
    if width % 2:
        image = image[:, : width - 1]
        width -= 1

    ycrcb = cv2.cvtColor(image, cv2.COLOR_BGR2YCrCb)
    y = ycrcb[:, :, 0]
    cr = ycrcb[:, :, 1].astype(np.uint16)
    cb = ycrcb[:, :, 2].astype(np.uint16)

    # 4:2:2 keeps one chroma pair per two pixels; average rather than drop, so
    # a one-pixel-wide colour edge does not disappear entirely.
    u = ((cb[:, 0::2] + cb[:, 1::2] + 1) >> 1).astype(np.uint8)
    v = ((cr[:, 0::2] + cr[:, 1::2] + 1) >> 1).astype(np.uint8)

    out = np.empty((height, width, 2), dtype=np.uint8)
    out[:, :, 0] = y
    out[:, 0::2, 1] = u
    out[:, 1::2, 1] = v
    return out


def pack_format(
    width: int, height: int, pixel_format: str, full_range: bool = True
) -> bytes:
    """Build ``struct v4l2_format`` for a video-output device."""
    code, bytes_per_pixel = PIXEL_FORMATS[pixel_format]
    bytes_per_line = int(width * bytes_per_pixel)
    size_image = bytes_per_line * height
    quantization = V4L2_QUANTIZATION_FULL_RANGE if full_range else V4L2_QUANTIZATION_LIM_RANGE

    # struct v4l2_format { __u32 type; <4 bytes padding>; union { ... } fmt; }
    # The union is 8-byte aligned (it contains a pointer via v4l2_window) and
    # 200 bytes long, giving the well-known total of 208.
    header = struct.pack("<I4x", V4L2_BUF_TYPE_VIDEO_OUTPUT)
    pix = struct.pack(
        "<12I",
        width,
        height,
        code,
        V4L2_FIELD_NONE,
        bytes_per_line,
        size_image,
        V4L2_COLORSPACE_SRGB,
        0,  # priv
        0,  # flags
        0,  # ycbcr_enc (default)
        quantization,
        0,  # xfer_func (default)
    )
    blob = header + pix
    return blob + b"\x00" * (_V4L2_FORMAT_SIZE - len(blob))


class V4L2Sink(Sink):
    name = "v4l2"

    def __init__(self, config: V4L2Config, full_range: bool = True):
        self.config = config
        self.device = config.device
        self.pixel_format = (config.pixel_format or "YUYV").upper()
        if self.pixel_format not in PIXEL_FORMATS:
            raise ValueError(
                f"unsupported output.v4l2.pixel_format {config.pixel_format!r}; "
                f"expected one of {', '.join(sorted(PIXEL_FORMATS))}"
            )
        self.full_range = full_range

        self._fd: int | None = None
        self._size: tuple[int, int] = (0, 0)
        self._frames = 0
        self._errors = 0
        self._last_error: str | None = None
        self._retry_at = 0.0

    # -- lifecycle ---------------------------------------------------------

    def open(self, width: int, height: int) -> None:
        if sys.platform != "linux":
            raise RuntimeError(
                "the v4l2 sink is Linux-only; use output.mjpeg or output.file "
                "for development on this platform"
            )
        if width % 2 and self.pixel_format in {"YUYV", "YUY2"}:
            raise ValueError("YUYV output requires an even width")

        self._size = (width, height)
        self._open_device()

    def _open_device(self) -> None:
        width, height = self._size
        if not os.path.exists(self.device):
            raise FileNotFoundError(
                f"{self.device} does not exist. Load the loopback module first:\n"
                f"  sudo modprobe v4l2loopback video_nr={self._device_number()} "
                f'card_label="Screen Sight" exclusive_caps=1'
            )

        fd = os.open(self.device, os.O_WRONLY)
        try:
            request = pack_format(width, height, self.pixel_format, self.full_range)
            fcntl.ioctl(fd, VIDIOC_S_FMT, request)
            # v4l2loopback may accept S_FMT then keep a stale capture format from
            # HyperHDR; read back what the device actually has.
            got = bytearray(request)
            fcntl.ioctl(fd, VIDIOC_G_FMT, got)
            got_w, got_h, got_code, _, got_bpl, got_size = struct.unpack_from("<6I", got, 8)
        except OSError as exc:
            os.close(fd)
            raise OSError(
                f"failed to set {self.pixel_format} {width}x{height} on {self.device}: {exc}"
            ) from exc

        expect_code, bpp = PIXEL_FORMATS[self.pixel_format]
        if (got_w, got_h, got_code) != (width, height, expect_code):
            os.close(fd)
            raise OSError(
                f"{self.device} negotiated {got_w}x{got_h} fourcc=0x{got_code:08x} "
                f"instead of {width}x{height} {self.pixel_format}. "
                f"Reload the loopback (modprobe -r v4l2loopback) and start Screen "
                f"Sight *before* HyperHDR; set keep_format=1 on the module."
            )

        self._fd = fd
        self._size = (got_w, got_h)
        self._lock_loopback_format()
        log.info(
            "V4L2 output ready: %s %s %dx%d (bpl=%d size=%d)",
            self.device,
            self.pixel_format,
            got_w,
            got_h,
            got_bpl,
            got_size,
        )

    def _lock_loopback_format(self) -> None:
        """Stop HyperHDR from renegotiating a different size/fourcc on open."""
        try:
            import subprocess

            subprocess.run(
                ["v4l2-ctl", "-d", self.device, "-c", "keep_format=1"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except Exception as exc:
            log.debug("could not set keep_format on %s: %s", self.device, exc)

    def _device_number(self) -> str:
        digits = "".join(ch for ch in self.device if ch.isdigit())
        return digits or "10"

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    # -- frames ------------------------------------------------------------

    def _convert(self, image: np.ndarray) -> np.ndarray:
        if self.pixel_format in {"YUYV", "YUY2"}:
            return bgr_to_yuyv(image)
        if self.pixel_format == "RGB24":
            return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        return np.ascontiguousarray(image)

    def write(self, image: np.ndarray) -> bool:
        if self._fd is None:
            # The consumer may have unloaded the module; retry occasionally
            # rather than giving up for the rest of the process's life.
            if time.monotonic() < self._retry_at:
                return False
            try:
                self._open_device()
            except Exception as exc:
                self._retry_at = time.monotonic() + 5.0
                self._last_error = str(exc)
                return False

        height, width = image.shape[:2]
        if (width, height) != self._size:
            image = cv2.resize(image, self._size, interpolation=cv2.INTER_AREA)

        payload = np.ascontiguousarray(self._convert(image))
        try:
            os.write(self._fd, payload.tobytes())
        except OSError as exc:
            self._errors += 1
            self._last_error = str(exc)
            log.warning("V4L2 write failed (%s); will reopen %s", exc, self.device)
            self.close()
            self._retry_at = time.monotonic() + 2.0
            return False

        self._frames += 1
        return True

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "pixel_format": self.pixel_format,
            "size": list(self._size),
            "open": self._fd is not None,
            "frames": self._frames,
            "errors": self._errors,
            "last_error": self._last_error,
        }
