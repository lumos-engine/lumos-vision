"""Enumerate capture cameras with stable Linux device paths.

``/dev/videoN`` numbers move when USB ports are replugged.  Symlinks under
``/dev/v4l/by-id/`` (and ``by-path/``) stay pointed at the same physical cam.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from processor.camera.controls import v4l2_ctl_available
from processor.utils.logging import get_logger

log = get_logger(__name__)

BY_ID_DIR = Path("/dev/v4l/by-id")
BY_PATH_DIR = Path("/dev/v4l/by-path")

#: Our HyperHDR-facing sink only -- other v4l2loopback cards (e.g. scrcpy
#: "Android Cam") are valid capture inputs and must stay listed.
_OUTPUT_LOOPBACK_MARKERS = ("screen sight",)
_INFO_CARD = re.compile(r"Card type\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
_INFO_BUS = re.compile(r"Bus info\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
_INFO_DRIVER = re.compile(r"Driver name\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE)


def resolve_device_path(device: str) -> str:
    """Return a filesystem path for ``device`` (index → ``/dev/videoN``)."""
    text = (device or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return f"/dev/video{text}"
    return text


def real_video_node(path: str | Path) -> str | None:
    """Resolve symlinks to a canonical ``/dev/videoN`` node, if any."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return None
    name = resolved.name
    if not name.startswith("video") or not name[5:].isdigit():
        return None
    return str(resolved)


def _read_v4l2_info(device: str) -> dict[str, str]:
    if not device or not v4l2_ctl_available():
        return {}
    try:
        result = subprocess.run(
            ["v4l2-ctl", "-d", device, "--info"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("v4l2-ctl --info failed for %s: %s", device, exc)
        return {}
    text = result.stdout or ""
    info: dict[str, str] = {}
    for pattern, key in (
        (_INFO_CARD, "card"),
        (_INFO_BUS, "bus_info"),
        (_INFO_DRIVER, "driver"),
    ):
        match = pattern.search(text)
        if match:
            info[key] = match.group(1).strip()
    return info


def _is_output_loopback(info: dict[str, str], name: str) -> bool:
    """True for Screen Sight's own loopback sink (never a capture source)."""
    blob = " ".join((info.get("card", ""), name)).lower()
    return any(marker in blob for marker in _OUTPUT_LOOPBACK_MARKERS)


def _is_loopback_driver(info: dict[str, str]) -> bool:
    """True for v4l2loopback. Driver name is often ``v4l2 loopback`` (space)."""
    blob = " ".join(
        (info.get("driver") or "", info.get("card") or "", info.get("bus_info") or "")
    ).lower()
    compact = "".join(ch for ch in blob if ch.isalnum())
    return "v4l2loopback" in compact or "androidcam" in compact


def is_v4l2loopback(device: str) -> bool:
    """True when ``v4l2-ctl --info`` reports the v4l2loopback driver."""
    path = resolve_device_path(device)
    if not path:
        return False
    return _is_loopback_driver(_read_v4l2_info(path))


def _is_capture_capable(device: str) -> bool:
    """True when the node advertises Video Capture (not metadata-only)."""
    if not v4l2_ctl_available():
        # Without v4l2-ctl, keep index0-style by-id names and bare video nodes.
        base = Path(device).name
        if "metadata" in base.lower():
            return False
        if re.search(r"index([1-9]|1[0-9])$", base):
            return False
        return True
    try:
        result = subprocess.run(
            ["v4l2-ctl", "-d", device, "--all"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = (result.stdout or "").lower()
    if "video capture" not in text:
        return False
    # Metadata nodes often list only Metadata Capture under Device Caps.
    caps_idx = text.find("device caps")
    if caps_idx >= 0:
        window = text[caps_idx : caps_idx + 400]
        if "video capture" not in window and "metadata capture" in window:
            return False
    return True


def _friendly_name(symlink_name: str, info: dict[str, str]) -> str:
    card = info.get("card") or ""
    if card:
        return card
    # usb-046d_0809-video-index0 → 046d:0809
    cleaned = symlink_name
    for prefix in ("usb-", "pci-"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    cleaned = re.sub(r"-video-index\d+$", "", cleaned)
    cleaned = cleaned.replace("_", ":")
    return cleaned or symlink_name


def _collect_from_dir(directory: Path, *, kind: str) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    found: list[dict[str, Any]] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        log.debug("cannot list %s: %s", directory, exc)
        return []

    for entry in entries:
        if not entry.is_symlink() and not entry.is_char_device():
            continue
        id_path = str(entry)
        video_path = real_video_node(entry)
        if video_path is None:
            continue
        info = _read_v4l2_info(id_path)
        if not info:
            info = _read_v4l2_info(video_path)
        if _is_output_loopback(info, entry.name):
            continue
        # UVC metadata nodes are usually *-video-index1+.  Check the symlink
        # name first so a bare /dev/video1 fallback cannot resurrect them.
        base = entry.name.lower()
        if "metadata" in base or re.search(r"-index([1-9]\d*)$", base):
            continue
        if not _is_capture_capable(id_path):
            # Some hosts only answer ioctl queries on the resolved node.
            if id_path == video_path or not _is_capture_capable(video_path):
                # exclusive_caps loopbacks only advertise Capture once a
                # producer (scrcpy) is attached -- still list them as inputs.
                if not _is_loopback_driver(info):
                    continue
        found.append(
            {
                "id_path": id_path,
                "video_path": video_path,
                "name": _friendly_name(entry.name, info),
                "bus_info": info.get("bus_info", ""),
                "driver": info.get("driver", ""),
                "stable_kind": kind,
            }
        )
    return found


def _collect_bare_video_nodes(seen_video: set[str]) -> list[dict[str, Any]]:
    """Fallback when by-id is empty (unusual on modern Linux)."""
    found: list[dict[str, Any]] = []
    for index in range(0, 64):
        path = f"/dev/video{index}"
        if not os.path.exists(path) or path in seen_video:
            continue
        info = _read_v4l2_info(path)
        if _is_output_loopback(info, path):
            continue
        if not _is_capture_capable(path) and not _is_loopback_driver(info):
            continue
        found.append(
            {
                "id_path": path,
                "video_path": path,
                "name": info.get("card") or path,
                "bus_info": info.get("bus_info", ""),
                "driver": info.get("driver", ""),
                "stable_kind": "video",
            }
        )
    return found


def list_capture_devices(
    selected: str | None = None,
    *,
    by_id_dir: Path | str | None = None,
    by_path_dir: Path | str | None = None,
    include_bare_video: bool = True,
) -> list[dict[str, Any]]:
    """Return capture cameras, preferring stable ``by-id`` paths.

    Each entry::

        {
          "id_path": "/dev/v4l/by-id/usb-…-video-index0",
          "video_path": "/dev/video4",
          "name": "Vimicro USB Camera …",
          "bus_info": "usb-…",
          "driver": "uvcvideo",
          "stable_kind": "by-id",
          "selected": bool,
        }
    """
    id_root = Path(by_id_dir) if by_id_dir is not None else BY_ID_DIR
    path_root = Path(by_path_dir) if by_path_dir is not None else BY_PATH_DIR

    devices = _collect_from_dir(id_root, kind="by-id")
    seen_video = {d["video_path"] for d in devices}

    # by-path only for nodes not already covered by by-id
    for entry in _collect_from_dir(path_root, kind="by-path"):
        if entry["video_path"] in seen_video:
            continue
        devices.append(entry)
        seen_video.add(entry["video_path"])

    # Platform loopbacks (scrcpy → Android Cam) rarely appear under by-id.
    # Always merge unseen /dev/videoN nodes so they show next to USB cams.
    if include_bare_video:
        for entry in _collect_bare_video_nodes(seen_video):
            devices.append(entry)
            seen_video.add(entry["video_path"])

    selected_path = resolve_device_path(selected or "")
    selected_real = real_video_node(selected_path) if selected_path else None
    for entry in devices:
        entry["selected"] = bool(
            selected_path
            and (
                entry["id_path"] == selected_path
                or entry["video_path"] == selected_path
                or (selected_real and entry["video_path"] == selected_real)
            )
        )
    return devices


def prefer_stable_device(device: str) -> str:
    """If ``device`` is a bare ``/dev/videoN``, return a matching by-id path."""
    text = (device or "").strip()
    if not text:
        return text
    real = real_video_node(resolve_device_path(text))
    if real is None:
        return text
    for entry in list_capture_devices(selected=text, include_bare_video=False):
        if entry["video_path"] == real:
            return entry["id_path"]
    return text
