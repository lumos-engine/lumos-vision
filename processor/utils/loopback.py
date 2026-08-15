"""Ensure v4l2loopback nodes for Screen Sight output and optional scrcpy.

Lumos Cam does **not** use a loopback. USB / RTSP / file / synthetic do not
either. Never ``modprobe -r`` — that would yank ``/dev/video10`` from HyperHDR.
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


def ensure_processor_loopbacks(config: Any) -> None:
    """Create output ``/dev/video10`` and scrcpy sink if those features are on."""
    if sys.platform != "linux":
        return
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
