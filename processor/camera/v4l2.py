"""USB / V4L2 webcam input.

Same drop-old rule as the RTSP source: a reader thread keeps one frame, the
pipeline always gets the newest, and nothing queues up behind a slow stage.

v4l2loopback (ffmpeg / scrcpy → ``/dev/video11``) is a special case: the
writer uses ``write()``, while OpenCV's V4L2 backend uses mmap + ``select()``.
Those two I/O paths often never meet, which shows up as
``VIDEOIO(V4L2): select() timeout`` with a live producer.  Loopback nodes are
read with a plain ``read()`` instead — the same syscall we use to *write*
``/dev/video10``.
"""

from __future__ import annotations

import fcntl
import os
import select
import struct
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from processor.camera.base import Frame, FrameSource
from processor.camera.controls import preferred_controls, set_controls
from processor.camera.devices import is_v4l2loopback, real_video_node, resolve_device_path
from processor.config.schema import CameraConfig
from processor.utils.logging import get_logger

log = get_logger(__name__)

V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
VIDIOC_G_FMT = 0xC0D05604
_V4L2_FORMAT_SIZE = 208


def resolve_device(device: str) -> str | int:
    """Accept ``/dev/video2``, ``/dev/v4l/by-id/…``, or a bare index ``2``.

    Stable by-id / by-path symlinks are preferred over bare ``/dev/videoN``
    because USB enumeration order can change after a reboot or replug.
    """
    text = (device or "").strip()
    if not text:
        raise ValueError("camera.device is required for the v4l2 source")
    if text.isdigit():
        return int(text)
    return text


def open_device_candidates(device: str | int) -> list[str | int]:
    """Ordered open attempts for OpenCV (path first, then resolved node / index)."""
    if isinstance(device, int):
        return [device, f"/dev/video{device}"]

    path = str(device)
    candidates: list[str | int] = [path]
    real = real_video_node(path)
    if real and real not in candidates:
        candidates.append(real)
        try:
            candidates.append(int(Path(real).name.replace("video", "", 1)))
        except ValueError:
            pass
    elif path.startswith("/dev/video"):
        try:
            candidates.append(int(path.rsplit("video", 1)[1]))
        except ValueError:
            pass
    return candidates


def _fourcc_string(code: int) -> str:
    return "".join(chr((int(code) >> (8 * i)) & 0xFF) for i in range(4))


def yuyv_to_bgr(data: bytes | memoryview | np.ndarray, width: int, height: int) -> np.ndarray:
    """Unpack packed YUYV 4:2:2 into BGR8."""
    expected = int(width) * int(height) * 2
    raw = np.frombuffer(data, dtype=np.uint8, count=expected)
    yuyv = raw.reshape((int(height), int(width), 2))
    return cv2.cvtColor(yuyv, cv2.COLOR_YUV2BGR_YUYV)


class _LoopbackCapture:
    """``read()``-based capture for v4l2loopback writers (ffmpeg / scrcpy).

    Do not STREAMON: ffmpeg's v4l2 muxer ``write()``s frames. STREAMON switches
    the node to mmap and delivers a couple of leftover buffers, then stalls —
    the "connected for a second then dies" symptom.
    """

    def __init__(self, fd: int, path: str, width: int, height: int, size_image: int, fourcc: str):
        self._fd = fd
        self.path = path
        self.width = width
        self.height = height
        self.size_image = size_image
        self.fourcc = fourcc

    @classmethod
    def open(cls, path: str) -> "_LoopbackCapture | None":
        fd = -1
        last_exc: OSError | None = None
        for flags in (os.O_RDONLY, os.O_RDWR):
            try:
                fd = os.open(path, flags)
                last_exc = None
                break
            except OSError as exc:
                last_exc = exc
                fd = -1
        if fd < 0:
            log.warning("loopback open %s failed: %s", path, last_exc)
            return None
        buf = bytearray(_V4L2_FORMAT_SIZE)
        struct.pack_into("<I4x", buf, 0, V4L2_BUF_TYPE_VIDEO_CAPTURE)
        try:
            fcntl.ioctl(fd, VIDIOC_G_FMT, buf)
        except OSError as exc:
            os.close(fd)
            log.warning("loopback G_FMT %s failed: %s", path, exc)
            return None
        width, height, code, _field, bpl, size_image = struct.unpack_from("<6I", buf, 8)
        fourcc = _fourcc_string(code)
        if width <= 0 or height <= 0:
            os.close(fd)
            log.warning("loopback %s has no format yet (%dx%d)", path, width, height)
            return None
        if size_image <= 0:
            size_image = int(bpl) * height if bpl else width * height * 2
        log.info(
            "V4L2 loopback %s: read() %dx%d fourcc=%s (%d bytes)",
            path,
            width,
            height,
            fourcc.strip() or "?",
            size_image,
        )
        return cls(fd, path, width, height, size_image, fourcc)

    def isOpened(self) -> bool:
        return self._fd >= 0

    def read(self) -> tuple[bool, np.ndarray | None]:
        image = self._read_frame(block=True)
        if image is None:
            return False, None
        # Drop-old: ffmpeg will block in write() if we let the 2-deep
        # loopback queue fill, which stalls the phone TCP stream.
        while True:
            extra = self._read_frame(block=False)
            if extra is None:
                break
            image = extra
        return True, image

    def _read_frame(self, *, block: bool) -> np.ndarray | None:
        fd = self._fd
        if fd < 0:
            return None
        if not block:
            try:
                ready, _, _ = select.select([fd], [], [], 0)
            except OSError:
                self.release()
                return None
            if not ready:
                return None
        try:
            data = os.read(fd, self.size_image)
        except OSError:
            self.release()
            return None
        if not data:
            self.release()
            return None
        return self._decode(data)

    def _decode(self, data: bytes) -> np.ndarray | None:
        fourcc = (self.fourcc or "").upper().strip()
        width, height = self.width, self.height
        if fourcc in {"YUYV", "YUY2"}:
            row = width * 2
            if len(data) < row or len(data) % row:
                return None
            got_h = len(data) // row
            return yuyv_to_bgr(data, width, min(height, got_h))
        if fourcc in {"BGR3", "RGB3"}:
            row = width * 3
            if len(data) < row or len(data) % row:
                return None
            got_h = len(data) // row
            image = np.frombuffer(data, dtype=np.uint8, count=row * got_h)
            image = image.reshape((got_h, width, 3)).copy()
            if fourcc == "RGB3":
                return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            return image
        log.warning("loopback %s fourcc %s is not YUYV/BGR; dropping frame", self.path, fourcc)
        return None

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.width)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.height)
        if prop == cv2.CAP_PROP_FPS:
            return 0.0
        if prop == cv2.CAP_PROP_FOURCC:
            raw = (self.fourcc or "YUYV").ljust(4)[:4]
            return float(sum(ord(ch) << (8 * i) for i, ch in enumerate(raw)))
        return 0.0

    def set(self, _prop: int, _value: float) -> bool:
        return False

    def release(self) -> None:
        fd, self._fd = self._fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


class V4l2Source(FrameSource):
    name = "v4l2"

    def __init__(self, config: CameraConfig):
        self.config = config
        self.device = resolve_device(config.device)
        self._open_path = resolve_device_path(str(config.device))

        self._capture: cv2.VideoCapture | _LoopbackCapture | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frame_ready = threading.Event()
        self._lock = threading.Lock()

        self._latest: Frame | None = None
        self._latest_consumed = True
        self._connected = False

        self._frame_index = 0
        self._dropped = 0
        self._reconnects = 0
        self._last_frame_at = 0.0
        self._last_error: str | None = None

    def start(self) -> "V4l2Source":
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="v4l2-reader", daemon=True)
        self._thread.start()
        log.info("V4L2 source started: %s", self.device)
        return self

    def stop(self) -> None:
        self._stop.set()
        # Close the fd first so a blocking loopback read() unblocks.
        self._release()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)
        log.info("V4L2 source stopped")

    def read(self, timeout: float = 1.0) -> Frame | None:
        if not self._frame_ready.wait(timeout=timeout):
            return None
        with self._lock:
            frame = self._latest
            self._latest_consumed = True
            self._frame_ready.clear()
        return frame

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "frames": self._frame_index,
            "dropped": self._dropped,
            "reconnects": self._reconnects,
            "last_frame_age": (
                round(time.monotonic() - self._last_frame_at, 3) if self._last_frame_at else None
            ),
            "last_error": self._last_error,
            "device": str(self.device),
            "video_node": real_video_node(self._device_path()) or self._device_path(),
        }

    def _run(self) -> None:
        delay = self.config.reconnect_delay
        while not self._stop.is_set():
            if not self._open():
                self._last_error = "open failed"
                self._sleep(delay)
                delay = min(delay * 1.6, self.config.max_reconnect_delay)
                self._reconnects += 1
                continue

            delay = self.config.reconnect_delay
            self._connected = True
            log.info("V4L2 connected: %s", self.device)

            try:
                self._pump()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.warning("V4L2 reader error: %s", self._last_error)

            self._connected = False
            self._release()
            if not self._stop.is_set():
                self._reconnects += 1
                log.warning("V4L2 disconnected, retrying in %.1fs", delay)
                self._sleep(delay)
                delay = min(delay * 1.6, self.config.max_reconnect_delay)

    def _pump(self) -> None:
        capture = self._capture
        assert capture is not None
        last_ok = time.monotonic()

        while not self._stop.is_set():
            ok, image = capture.read()
            now = time.monotonic()

            if not ok or image is None:
                if not capture.isOpened() or self._stop.is_set():
                    return
                # Keep a loopback fd open: closing it makes ffmpeg's write()
                # fail, which kills the phone TCP stream a few seconds later.
                if isinstance(capture, _LoopbackCapture):
                    time.sleep(0.01)
                    continue
                if now - last_ok > self.config.read_timeout:
                    self._last_error = f"no frames for {self.config.read_timeout:.0f}s"
                    return
                time.sleep(0.005)
                continue

            last_ok = now
            self._last_frame_at = now
            self._publish(image, now)

    def _publish(self, image: np.ndarray, now: float) -> None:
        image = self._downscale(image)
        with self._lock:
            if not self._latest_consumed:
                self._dropped += 1
            self._frame_index += 1
            self._latest = Frame(image=image, index=self._frame_index, captured_at=now)
            self._latest_consumed = False
            self._frame_ready.set()

    def _downscale(self, image: np.ndarray) -> np.ndarray:
        target = self.config.process_width
        if target <= 0 or image.shape[1] <= target:
            return image
        scale = target / float(image.shape[1])
        size = (target, max(1, int(round(image.shape[0] * scale))))
        return cv2.resize(image, size, interpolation=cv2.INTER_AREA)

    def _open(self) -> bool:
        self._release()
        path = self._device_path()
        if is_v4l2loopback(path):
            capture = _LoopbackCapture.open(path)
            if capture is None:
                return False
            self._open_path = path
            self._capture = capture
            log.info("V4L2 opened %s (loopback read)", self.device)
            return True

        capture: cv2.VideoCapture | None = None
        opened_as: str | int | None = None
        for candidate in open_device_candidates(self.device):
            trial = cv2.VideoCapture(candidate, cv2.CAP_V4L2)
            if trial.isOpened():
                capture = trial
                opened_as = candidate
                break
            trial.release()

        if capture is None or not capture.isOpened():
            if capture is not None:
                capture.release()
            log.warning("Failed to open V4L2 device %s", self.device)
            return False

        self._open_path = (
            resolve_device_path(str(opened_as))
            if not isinstance(opened_as, int)
            else f"/dev/video{opened_as}"
        )
        real = real_video_node(self._open_path)
        log.info(
            "V4L2 opened %s%s",
            self.device,
            f" → {real}" if real and real != str(self.device) else "",
        )

        try:
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except cv2.error:
            pass

        # Prefer MJPEG when available: USB webcams usually deliver higher
        # resolution/fps that way than raw YUYV.
        try:
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        except cv2.error:
            pass

        width = int(self.config.capture_width or 0)
        height = int(self.config.capture_height or 0)
        if width > 0:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height > 0:
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if self.config.capture_fps > 0:
            capture.set(cv2.CAP_PROP_FPS, self.config.capture_fps)

        self._capture = capture
        got_w = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        got_h = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        got_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        fourcc = int(capture.get(cv2.CAP_PROP_FOURCC) or 0)
        log.info(
            "V4L2 mode: %dx%d @ %.1f fps fourcc=%s",
            got_w,
            got_h,
            got_fps,
            _fourcc_string(fourcc).strip() or "?",
        )
        if self.config.controls:
            self.apply_controls(dict(self.config.controls))
        return True

    def _device_path(self) -> str:
        """Path for v4l2-ctl: prefer the configured by-id symlink."""
        if isinstance(self.device, int):
            return f"/dev/video{self.device}"
        path = str(self.device)
        if Path(path).exists():
            return path
        if self._open_path and Path(self._open_path).exists():
            return self._open_path
        real = real_video_node(path)
        return real or path

    def apply_controls(self, values: dict[str, int]) -> dict:
        """Push hardware controls to the device (exposure, gain, …)."""
        return set_controls(self._device_path(), values)

    def list_controls(self) -> list[dict]:
        return [c.to_dict() for c in preferred_controls(self._device_path())]

    def _release(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(timeout=max(0.0, seconds))
