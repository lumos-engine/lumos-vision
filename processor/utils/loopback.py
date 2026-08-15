"""Ensure v4l2loopback nodes for Screen Sight output and optional scrcpy.

Lumos Cam does **not** use a loopback. USB / RTSP / file / synthetic do not
either.

``ensure_*`` never ``modprobe -r`` — that would yank ``/dev/video10`` from
HyperHDR on a healthy start. ``repair_loopback`` is the exception: S_FMT
EINVAL means the node is stuck (stale ``keep_format``, a consumer holding
the old size), and delete+add of that node — or a full reload if ctl cannot
— is the only way HyperHDR will see a capture device again.

Linux-only; a no-op on macOS.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Any

from processor.camera.devices import is_v4l2loopback
from processor.utils.logging import get_logger

log = get_logger(__name__)

OUTPUT_LABEL = "Screen Sight"
SCRCPY_LABEL = "Android Cam"
_VIDEO_NR = re.compile(r"(?:^|/)video(\d+)$")


def video_nr(path: str) -> int | None:
    match = _VIDEO_NR.search((path or "").strip())
    if not match:
        return None
    return int(match.group(1))


def _run(cmd: list[str], *, timeout: float = 8.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _path_ready(path: str) -> bool:
    if not path or not os.path.exists(path):
        return False
    if is_v4l2loopback(path):
        return True
    log.warning("%s exists but is not v4l2loopback — leaving it alone", path)
    return True


def _ctl_add(path: str, label: str) -> bool:
    binary = shutil.which("v4l2loopback-ctl")
    if not binary:
        return False
    nr = video_nr(path)
    attempts = [
        [binary, "add", "--name", label, "--exclusive-caps", "1", path],
        [binary, "add", "-n", label, path],
    ]
    if nr is not None:
        attempts.append([binary, "add", "--name", label, str(nr)])
    for cmd in attempts:
        try:
            result = _run(cmd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.debug("v4l2loopback-ctl add failed: %s", exc)
            continue
        if os.path.exists(path):
            log.info("Created %s via %s", path, " ".join(cmd))
            return True
        if result.returncode != 0:
            log.debug(
                "v4l2loopback-ctl add: %s (%s)",
                (result.stderr or result.stdout or "").strip(),
                " ".join(cmd),
            )
    return os.path.exists(path)


def _ctl_delete(path: str) -> bool:
    binary = shutil.which("v4l2loopback-ctl")
    if not binary or not path:
        return False
    attempts = [
        [binary, "delete", path],
        [binary, "remove", path],
        [binary, "del", path],
    ]
    for cmd in attempts:
        try:
            _run(cmd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.debug("v4l2loopback-ctl delete failed: %s", exc)
            continue
        if not os.path.exists(path):
            log.info("Removed stuck loopback %s via %s", path, " ".join(cmd))
            return True
    return not os.path.exists(path)


def _modprobe_line(devices: list[tuple[str, str]]) -> list[str]:
    nrs: list[str] = []
    labels: list[str] = []
    for path, label in devices:
        nr = video_nr(path)
        if nr is None:
            continue
        nrs.append(str(nr))
        labels.append(label)
    if not nrs:
        return []
    return [
        "modprobe",
        "v4l2loopback",
        f"devices={len(nrs)}",
        f"video_nr={','.join(nrs)}",
        f"card_label={','.join(labels)}",
        "exclusive_caps=1",
    ]


def _sudo_modprobe(devices: list[tuple[str, str]]) -> bool:
    line = _modprobe_line(devices)
    if not line:
        return False
    helper = shutil.which("screen-sight-loopback")
    if helper:
        cmd = ["sudo", "-n", helper, *[p for p, _ in devices]]
        try:
            _run(cmd, timeout=15.0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.debug("screen-sight-loopback: %s", exc)
        if all(os.path.exists(p) for p, _ in devices):
            return True
    cmd = ["sudo", "-n", *line]
    try:
        result = _run(cmd, timeout=15.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("sudo modprobe v4l2loopback failed: %s", exc)
        return False
    if result.returncode != 0:
        log.debug(
            "sudo -n modprobe: %s",
            (result.stderr or result.stdout or "").strip(),
        )
        return False
    return all(os.path.exists(p) for p, _ in devices)


def _sudo_reload(devices: list[tuple[str, str]]) -> bool:
    """Unload+reload via the privileged helper. Never ``modprobe -r`` here."""
    if not devices:
        return False
    helper = shutil.which("screen-sight-loopback")
    if not helper:
        log.warning(
            "Cannot reload v4l2loopback: screen-sight-loopback is not on PATH"
        )
        return False
    cmd = ["sudo", "-n", helper, "--reload", *[p for p, _ in devices]]
    try:
        result = _run(cmd, timeout=20.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("screen-sight-loopback --reload failed: %s", exc)
        return False
    if result.returncode != 0:
        log.debug(
            "screen-sight-loopback --reload: %s",
            (result.stderr or result.stdout or "").strip(),
        )
    if all(os.path.exists(p) for p, _ in devices):
        log.info(
            "Reloaded v4l2loopback: %s",
            ", ".join(f"{p} ({label})" for p, label in devices),
        )
        return True
    return False


def _log_manual_command(devices: list[tuple[str, str]]) -> None:
    nrs = [str(video_nr(path)) for path, _ in devices if video_nr(path) is not None]
    labels = [label for path, label in devices if video_nr(path) is not None]
    if not nrs:
        return
    log.error(
        "v4l2loopback node missing — create it with: sudo modprobe v4l2loopback "
        'devices=%d video_nr=%s card_label="%s" exclusive_caps=1',
        len(nrs),
        ",".join(nrs),
        ",".join(labels),
    )


def _card_name(path: str) -> str:
    nr = video_nr(path)
    if nr is None:
        return ""
    sysfs = f"/sys/devices/virtual/video4linux/video{nr}/name"
    try:
        with open(sysfs, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def existing_loopback_devices() -> list[tuple[str, str]]:
    """``(/dev/videoN, card_label)`` for currently loaded loopback nodes."""
    root = "/sys/devices/virtual/video4linux"
    if not os.path.isdir(root):
        return []
    found: list[tuple[str, str]] = []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    for name in sorted(names):
        if not name.startswith("video") or not name[5:].isdigit():
            continue
        path = f"/dev/{name}"
        if not os.path.exists(path):
            continue
        label = _card_name(path) or ""
        if label in {OUTPUT_LABEL, SCRCPY_LABEL} or is_v4l2loopback(path):
            found.append((path, label or OUTPUT_LABEL))
    return found


def needed_loopbacks(config: Any) -> list[tuple[str, str]]:
    """Output ``/dev/video10`` and the scrcpy sink, when those features are on."""
    needed: list[tuple[str, str]] = []
    output = getattr(getattr(config, "output", None), "v4l2", None)
    if output is not None and getattr(output, "enabled", False):
        device = str(getattr(output, "device", "") or "").strip()
        if device:
            needed.append((device, OUTPUT_LABEL))
    camera = getattr(config, "camera", None)
    source = str(getattr(camera, "source", "") or "").strip().lower()
    if source == "scrcpy":
        sink = str(getattr(getattr(config, "scrcpy", None), "v4l2_sink", "") or "").strip()
        if sink:
            needed.append((sink, SCRCPY_LABEL))
    return needed


def ensure_loopback(path: str, *, label: str) -> bool:
    """Make sure ``path`` exists as a v4l2loopback node. Never unloads the module."""
    path = (path or "").strip()
    if not path:
        return False
    if sys.platform != "linux":
        return os.path.exists(path)
    if _path_ready(path):
        return True
    if _ctl_add(path, label):
        return True
    if _sudo_modprobe([(path, label)]) and os.path.exists(path):
        log.info("Loaded v4l2loopback for %s (%s)", path, label)
        return True
    _log_manual_command([(path, label)])
    return os.path.exists(path)


def repair_loopback(
    path: str,
    *,
    label: str,
    keep: list[tuple[str, str]] | None = None,
) -> bool:
    """Recreate a stuck loopback node after S_FMT EINVAL.

    Prefers ``v4l2loopback-ctl delete`` + add of this node only, so a scrcpy
    ``/dev/video11`` producer can keep running. Falls back to the privileged
    helper's ``--reload`` (module unload) if ctl cannot replace the node.
    """
    path = (path or "").strip()
    if not path or sys.platform != "linux":
        return False
    if os.path.exists(path) and not is_v4l2loopback(path):
        log.warning("%s is not v4l2loopback — not repairing it", path)
        return False

    if os.path.exists(path):
        _ctl_delete(path)
    if _ctl_add(path, label) and os.path.exists(path):
        log.info("Recreated %s (%s) after format failure", path, label)
        return True

    devices: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in list(keep or []) + existing_loopback_devices() + [(path, label)]:
        node, card = item
        if not node or node in seen:
            continue
        seen.add(node)
        devices.append((node, card))
    if _sudo_reload(devices) and os.path.exists(path):
        return True
    if _sudo_modprobe(devices) and os.path.exists(path):
        return True
    log.error(
        "Could not repair %s — HyperHDR will not see Screen Sight until "
        "the loopback node accepts %s",
        path,
        label,
    )
    return os.path.exists(path)


def ensure_processor_loopbacks(config: Any) -> None:
    """Create output ``/dev/video10`` and scrcpy sink if those features are on."""
    if sys.platform != "linux":
        return
    needed = needed_loopbacks(config)
    if not needed:
        return

    missing = [(path, label) for path, label in needed if not os.path.exists(path)]
    if not missing:
        for path, _label in needed:
            _path_ready(path)
        return

    if missing == needed:
        if _sudo_modprobe(needed) and all(os.path.exists(p) for p, _ in needed):
            log.info(
                "Loaded v4l2loopback: %s",
                ", ".join(f"{p} ({label})" for p, label in needed),
            )
            return

    for path, label in missing:
        ensure_loopback(path, label=label)
