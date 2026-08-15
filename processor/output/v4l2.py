"""V4L2 loopback output -- the virtual webcam HyperHDR captures.

Talks to ``/dev/videoN`` directly: one ``VIDIOC_S_FMT`` ioctl to declare the
format, then a plain ``write()`` per frame.  That keeps the dependency list at
zero and the per-frame cost at one colour conversion plus one syscall.

Requires the ``v4l2loopback`` kernel module (Linux only). Screen Sight creates
and, when ``S_FMT`` fails, repairs the node; do not treat a missing or stuck
``/dev/video10`` as a manual ``modprobe`` step.

``exclusive_caps=1`` matters: without it the device advertises both capture and
output capabilities and many consumers, HyperHDR included, refuse to open it.
A producer must hold the node open (and write at least one frame) before
HyperHDR will list it as a capture device.
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
from processor.utils.loopback import OUTPUT_LABEL, ensure_loopback, repair_loopback

log = get_logger(__name__)

# include/uapi/linux/videodev2.h
V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
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

_CTL_PIX = {"YUYV": "YUYV", "YUY2": "YUYV", "RGB24": "RGB3", "BGR24": "BGR3"}


def format_name(code: int) -> str | None:
    if code == fourcc("YUYV"):
        return "YUYV"
    if code == fourcc("RGB3"):
        return "RGB24"
    if code == fourcc("BGR3"):
        return "BGR24"
    return None


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
    width: int,
    height: int,
    pixel_format: str,
    full_range: bool = True,
    buf_type: int = V4L2_BUF_TYPE_VIDEO_OUTPUT,
) -> bytes:
    """Build ``struct v4l2_format`` for a video-output (or capture) device."""
    code, bytes_per_pixel = PIXEL_FORMATS[pixel_format]
    bytes_per_line = int(max(width, 0) * bytes_per_pixel)
    size_image = bytes_per_line * max(height, 0)
    quantization = V4L2_QUANTIZATION_FULL_RANGE if full_range else V4L2_QUANTIZATION_LIM_RANGE

    # struct v4l2_format { __u32 type; <4 bytes padding>; union { ... } fmt; }
    # The union is 8-byte aligned (it contains a pointer via v4l2_window) and
    # 200 bytes long, giving the well-known total of 208.
    header = struct.pack("<I4x", int(buf_type))
    pix = struct.pack(
        "<12I",
        max(int(width), 0),
        max(int(height), 0),
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


def unpack_format(blob: bytes) -> tuple[int, int, int, int, int]:
    width, height, code, _field, bytes_per_line, size_image = struct.unpack_from(
        "<6I", blob, 8
    )
    return width, height, code, bytes_per_line, size_image


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
        self._next_repair = 0.0
        self._repair_cooldown = 15.0
        self._requested: tuple[int, int] = (0, 0)

    # -- lifecycle ---------------------------------------------------------

    def open(self, width: int, height: int) -> None:
        if sys.platform != "linux":
            raise RuntimeError(
                "the v4l2 sink is Linux-only; use output.mjpeg or output.file "
                "for development on this platform"
            )
        if width % 2 and self.pixel_format in {"YUYV", "YUY2"}:
            raise ValueError("YUYV output requires an even width")

        self._requested = (width, height)
        self._size = (width, height)
        if not self._open_device():
            log.error(
                "V4L2 sink deferred: will keep retrying %s %s %dx%d",
                self.device,
                self.pixel_format,
                width,
                height,
            )

    def _open_device(self) -> bool:
        width, height = self._requested or self._size
        if not os.path.exists(self.device):
            ensure_loopback(self.device, label=OUTPUT_LABEL)
        if not os.path.exists(self.device):
            self._last_error = f"{self.device} does not exist"
            log.error(
                "%s does not exist and Screen Sight could not create it",
                self.device,
            )
            return False

        self._set_keep_format(0)

        negotiated = self._negotiate(width, height, self.pixel_format)
        if negotiated is None and time.monotonic() >= self._next_repair:
            self._next_repair = time.monotonic() + self._repair_cooldown
            log.info("Repairing %s after S_FMT failure", self.device)
            repair_loopback(self.device, label=OUTPUT_LABEL)
            self._set_keep_format(0)
            negotiated = self._negotiate(width, height, self.pixel_format)

        if negotiated is None:
            self._last_error = (
                f"failed to set {self.pixel_format} {width}x{height} on {self.device}"
            )
            log.error("%s; HyperHDR will not see this device until it succeeds", self._last_error)
            return False

        fd, got_w, got_h, got_code, got_bpl, got_size = negotiated
        name = format_name(got_code) or self.pixel_format
        if (got_w, got_h, name) != (width, height, self.pixel_format):
            log.warning(
                "%s is pinned at %s %dx%d (wanted %s %dx%d) — writing that "
                "so HyperHDR still has a producer",
                self.device,
                name,
                got_w,
                got_h,
                self.pixel_format,
                width,
                height,
            )

        self._fd = fd
        self._size = (got_w, got_h)
        self.pixel_format = name
        self._announce_capture(fd, got_w, got_h)
        self._set_keep_format(1)
        self._last_error = None
        log.info(
            "V4L2 output ready: %s %s %dx%d (device bpl=%d size=%d)",
            self.device,
            self.pixel_format,
            got_w,
            got_h,
            got_bpl,
            got_size,
        )
        return True

    def _negotiate(
        self, width: int, height: int, pixel_format: str
    ) -> tuple[int, int, int, int, int, int] | None:
        """Open the node. Prefer the configured format; otherwise keep the pin."""
        last: OSError | None = None
        for flags in (os.O_RDWR, os.O_WRONLY):
            fd: int | None = None
            try:
                fd = os.open(self.device, flags)
                current = self._read_format(fd)
                if self._try_s_fmt(fd, width, height, pixel_format):
                    got = self._read_format(fd)
                    if got is None:
                        code, _bpp = PIXEL_FORMATS[pixel_format]
                        bpl = int(width * _bpp)
                        got = (width, height, code, bpl, bpl * height)
                    return (fd, *got)
                adopted = self._adoptable(current)
                if adopted is not None:
                    return (fd, *adopted)
                os.close(fd)
                fd = None
                if self._ctl_set_fmt(width, height, pixel_format):
                    fd = os.open(self.device, flags)
                    got = self._read_format(fd)
                    if got is not None and got[0] >= 2 and got[1] >= 1:
                        return (fd, *got)
                    os.close(fd)
                    fd = None
                if pixel_format in {"YUYV", "YUY2"}:
                    fd = os.open(self.device, flags)
                    if self._try_s_fmt(fd, width, height, "RGB24"):
                        got = self._read_format(fd)
                        if got is not None:
                            return (fd, *got)
                    os.close(fd)
                    fd = None
            except OSError as exc:
                last = exc
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    fd = None
                continue
        if last is not None:
            log.warning(
                "failed to set %s %dx%d on %s: %s",
                pixel_format,
                width,
                height,
                self.device,
                last,
            )
        return None

    def _read_format(self, fd: int) -> tuple[int, int, int, int, int] | None:
        for buf_type in (V4L2_BUF_TYPE_VIDEO_OUTPUT, V4L2_BUF_TYPE_VIDEO_CAPTURE):
            buf = bytearray(pack_format(0, 0, "YUYV", self.full_range, buf_type=buf_type))
            try:
                fcntl.ioctl(fd, VIDIOC_G_FMT, buf)
            except OSError:
                continue
            width, height, code, bpl, size = unpack_format(buf)
            if width >= 2 and height >= 1 and format_name(code):
                return width, height, code, bpl, size
        return None

    def _adoptable(
        self, current: tuple[int, int, int, int, int] | None
    ) -> tuple[int, int, int, int, int] | None:
        if current is None:
            return None
        width, height, code, bpl, size = current
        if format_name(code) and width >= 2 and height >= 1:
            return current
        return None

    def _try_s_fmt(self, fd: int, width: int, height: int, pixel_format: str) -> bool:
        request = bytearray(pack_format(width, height, pixel_format, self.full_range))
        try:
            fcntl.ioctl(fd, VIDIOC_S_FMT, request)
            return True
        except OSError as exc:
            log.warning(
                "failed to set %s %dx%d on %s: %s",
                pixel_format,
                width,
                height,
                self.device,
                exc,
            )
            return False

    def _ctl_set_fmt(self, width: int, height: int, pixel_format: str) -> bool:
        pix = _CTL_PIX.get(pixel_format)
        if not pix:
            return False
        try:
            import subprocess

            result = subprocess.run(
                [
                    "v4l2-ctl",
                    "-d",
                    self.device,
                    f"--set-fmt-video-out=width={width},height={height},pixelformat={pix}",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=3.0,
            )
        except Exception as exc:
            log.debug("v4l2-ctl S_FMT on %s failed: %s", self.device, exc)
            return False
        if result.returncode != 0:
            log.debug(
                "v4l2-ctl S_FMT on %s: %s",
                self.device,
                (result.stderr or result.stdout or "").strip(),
            )
            return False
        log.info(
            "Set %s %dx%d on %s via v4l2-ctl",
            pixel_format,
            width,
            height,
            self.device,
        )
        return True

    def _announce_capture(self, fd: int, width: int, height: int) -> None:
        """Write one black frame so exclusive_caps flips to Video Capture."""
        black = np.zeros((height, width, 3), dtype=np.uint8)
        payload = np.ascontiguousarray(self._convert(black))
        try:
            os.write(fd, payload.tobytes())
        except OSError as exc:
            log.debug("prime write on %s failed: %s", self.device, exc)

    def _set_keep_format(self, value: int) -> None:
        """v4l2loopback control: 1 locks format, 0 allows renegotiation."""
        try:
            import subprocess

            subprocess.run(
                ["v4l2-ctl", "-d", self.device, "-c", f"keep_format={int(value)}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except Exception as exc:
            log.debug("keep_format=%s on %s failed: %s", value, self.device, exc)

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
                if not self._open_device():
                    self._retry_at = time.monotonic() + 5.0
                    return False
            except Exception as exc:
                self._retry_at = time.monotonic() + 5.0
                self._last_error = str(exc)
                return False

        height, width = image.shape[:2]
        if (width, height) != self._size:
            image = cv2.resize(image, self._size, interpolation=cv2.INTER_AREA)

        flip_h = bool(getattr(self.config, "flip_horizontal", False))
        flip_v = bool(getattr(self.config, "flip_vertical", False))
        flip_code = None
        if flip_h and flip_v:
            flip_code = -1
        elif flip_v:
            flip_code = 0
        elif flip_h:
            flip_code = 1
        if flip_code is not None:
            image = cv2.flip(image, flip_code)

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
