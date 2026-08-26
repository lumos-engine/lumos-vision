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
from pathlib import Path
from typing import Any

from processor.camera.devices import is_v4l2loopback
from processor.utils.logging import get_logger

log = get_logger(__name__)

OUTPUT_LABEL = "Screen Sight"
SCRCPY_LABEL = "Android Cam"
_VIDEO_NR = re.compile(r"(?:^|/)video(\d+)$")
_sudo_password_blocked = False


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


def _modprobe_bin() -> str:
    for candidate in ("/usr/sbin/modprobe", "/sbin/modprobe"):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("modprobe") or "modprobe"


def loopback_helper() -> str:
    """PATH, packaged install, or the checkout copy under ``packaging/``."""
    found = shutil.which("screen-sight-loopback")
    if found:
        return found
    here = Path(__file__).resolve()
    candidates = (
        Path("/usr/lib/screen-sight/screen-sight-loopback"),
        here.parents[2] / "packaging" / "screen-sight-loopback",
    )
    for path in candidates:
        try:
            if path.is_file():
                return str(path)
        except OSError:
            continue
    return ""


def _sudo(args: list[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    """Run ``args`` with ``sudo -n`` unless we are already root."""
    global _sudo_password_blocked
    if os.geteuid() == 0:
        cmd = list(args)
    else:
        sudo = shutil.which("sudo") or "sudo"
        cmd = [sudo, "-n", *args]
    if _sudo_password_blocked and os.geteuid() != 0:
        return subprocess.CompletedProcess(cmd, 1, "", "a password is required")
    try:
        result = _run(cmd, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("%s failed: %s", " ".join(cmd), exc)
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        if "password is required" in err.lower():
            _sudo_password_blocked = True
            log.error(
                "Passwordless sudo is not set; Screen Sight will use the "
                "format already on the loopback instead of reloading the module. "
                "One-time fix: sudo cp packaging/sudoers.d/screen-sight-loopback "
                "/etc/sudoers.d/ && sudo visudo -c -f /etc/sudoers.d/screen-sight-loopback"
            )
        else:
            log.warning("%s: %s", " ".join(cmd), err or f"exit {result.returncode}")
    return result


def _ctl_add(path: str, label: str) -> bool:
    binary = shutil.which("v4l2loopback-ctl")
    if not binary:
        return False
    existed = os.path.exists(path)
    nr = video_nr(path)
    attempts = [
        ["add", "--name", label, "--exclusive-caps", "1", path],
        ["add", "-n", label, path],
    ]
    if nr is not None:
        attempts.append(["add", "--name", label, str(nr)])
    for args in attempts:
        for prefix in ([binary], ["sudo", "-n", binary] if os.geteuid() != 0 else []):
            if not prefix:
                continue
            cmd = [*prefix, *args]
            try:
                result = _run(cmd)
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.debug("v4l2loopback-ctl add failed: %s", exc)
                continue
            created = os.path.exists(path) and not existed
            if created:
                log.info("Created %s via %s", path, " ".join(cmd))
                return True
            if result.returncode != 0:
                log.debug(
                    "v4l2loopback-ctl add: %s (%s)",
                    (result.stderr or result.stdout or "").strip(),
                    " ".join(cmd),
                )
    return os.path.exists(path) and not existed


def _ctl_delete(path: str) -> bool:
    binary = shutil.which("v4l2loopback-ctl")
    if not binary or not path:
        return False
    attempts = [
        ["delete", path],
        ["remove", path],
        ["del", path],
    ]
    prefixes = [[binary]]
    if os.geteuid() != 0:
        prefixes.append(["sudo", "-n", binary])
    for args in attempts:
        for prefix in prefixes:
            cmd = [*prefix, *args]
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
        _modprobe_bin(),
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
    helper = loopback_helper()
    if helper:
        _sudo([helper, *[p for p, _ in devices]], timeout=15.0)
        if all(os.path.exists(p) for p, _ in devices):
            return True
    result = _sudo(line, timeout=15.0)
    if result.returncode != 0:
        return False
    return all(os.path.exists(p) for p, _ in devices)


def _module_loaded() -> bool:
    try:
        with open("/proc/modules", encoding="utf-8") as handle:
            return "v4l2loopback" in handle.read()
    except OSError:
        return False


def _sudo_reload(devices: list[tuple[str, str]]) -> bool:
    """Unload and reload v4l2loopback. Used only when S_FMT is stuck."""
    if not devices:
        return False
    helper = loopback_helper()
    if helper:
        result = _sudo([helper, "--reload", *[p for p, _ in devices]], timeout=20.0)
        if result.returncode == 0 and all(os.path.exists(p) for p, _ in devices):
            log.info(
                "Reloaded v4l2loopback via %s: %s",
                helper,
                ", ".join(f"{p} ({label})" for p, label in devices),
            )
            return True

    unload = _sudo([_modprobe_bin(), "-r", "v4l2loopback"], timeout=15.0)
    if unload.returncode != 0 and _module_loaded():
        return False
    if not _sudo_modprobe(devices):
        return False
    log.info(
        "Reloaded v4l2loopback: %s",
        ", ".join(f"{p} ({label})" for p, label in devices),
    )
    return True


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
    log.error(
        "Could not repair %s — HyperHDR will not see Screen Sight until "
        "the loopback node accepts %s. If sudo asked for a password, install "
        "packaging/sudoers.d/screen-sight-loopback or run: sudo %s -r v4l2loopback",
        path,
        label,
        _modprobe_bin(),
    )
    return False


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
