"""Read / write UVC camera controls via ``v4l2-ctl``.

OpenCV can set a few CAP_PROP_* knobs, but the reliable interface on Linux for
exposure, white balance, gain, etc. is the V4L2 control API.  We shell out to
``v4l2-ctl`` so we do not need extra native bindings, and so controls still
work while OpenCV holds the device open.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

from processor.utils.logging import get_logger

log = get_logger(__name__)

#: Controls worth surfacing in the wizard (order = UI order).
PREFERRED_CONTROLS = (
    "exposure_auto",
    "auto_exposure",
    "exposure_absolute",
    "exposure_time_absolute",
    "exposure_auto_priority",
    "gain",
    "brightness",
    "contrast",
    "saturation",
    "sharpness",
    "gamma",
    "white_balance_temperature_auto",
    "white_balance_automatic",
    "white_balance_temperature",
    "backlight_compensation",
    "power_line_frequency",
    "hue",
)

_CTRL_LINE = re.compile(
    r"^\s*([A-Za-z0-9_]+)\s+0x[0-9a-fA-F]+\s+\((\w+)\)\s*:\s*(.*)$"
)
_KV = re.compile(r"(\w+)=(-?\d+)")
_MENU_ITEM = re.compile(r"^\s*(\d+):\s*(.+?)\s*$")


@dataclass
class CameraControl:
    name: str
    type: str  # int | bool | menu | …
    min: int = 0
    max: int = 0
    step: int = 1
    default: int = 0
    value: int = 0
    menu: dict[int, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "min": self.min,
            "max": self.max,
            "step": self.step,
            "default": self.default,
            "value": self.value,
        }
        if self.menu:
            data["menu"] = {str(k): v for k, v in sorted(self.menu.items())}
        return data


def v4l2_ctl_available() -> bool:
    return shutil.which("v4l2-ctl") is not None


def list_controls(device: str) -> list[CameraControl]:
    """Parse ``v4l2-ctl -d DEVICE --list-ctrls`` into structured controls."""
    if not device:
        return []
    if not v4l2_ctl_available():
        log.warning("v4l2-ctl not found; install v4l-utils for camera hardware controls")
        return []

    result = subprocess.run(
        ["v4l2-ctl", "-d", device, "--list-ctrls"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if result.returncode != 0:
        log.warning(
            "v4l2-ctl --list-ctrls failed for %s: %s",
            device,
            (result.stderr or result.stdout or "").strip(),
        )
        return []

    controls: list[CameraControl] = []
    current: CameraControl | None = None
    for line in (result.stdout or "").splitlines():
        match = _CTRL_LINE.match(line)
        if match:
            name, ctype, rest = match.group(1), match.group(2), match.group(3)
            kv = {k: int(v) for k, v in _KV.findall(rest)}
            current = CameraControl(
                name=name,
                type=ctype,
                min=kv.get("min", 0),
                max=kv.get("max", 0),
                step=max(1, kv.get("step", 1)),
                default=kv.get("default", 0),
                value=kv.get("value", kv.get("default", 0)),
            )
            controls.append(current)
            continue
        menu = _MENU_ITEM.match(line)
        if menu and current is not None and current.type == "menu":
            current.menu[int(menu.group(1))] = menu.group(2).strip()
    return controls


def preferred_controls(device: str) -> list[CameraControl]:
    """Subset of :func:`list_controls` in a sensible UI order."""
    available = {c.name: c for c in list_controls(device)}
    ordered = [available[name] for name in PREFERRED_CONTROLS if name in available]
    # Include any remaining numeric/menu controls the user might still want.
    seen = {c.name for c in ordered}
    for ctrl in available.values():
        if ctrl.name not in seen and ctrl.type in ("int", "bool", "menu"):
            ordered.append(ctrl)
            seen.add(ctrl.name)
    return ordered


def set_controls(device: str, values: dict[str, int]) -> dict[str, Any]:
    """Apply ``name=value`` pairs.  Returns ``{ok, applied, errors}``."""
    if not device:
        return {"ok": False, "error": "no camera device", "applied": {}, "errors": {}}
    if not values:
        return {"ok": True, "applied": {}, "errors": {}}
    if not v4l2_ctl_available():
        return {
            "ok": False,
            "error": "v4l2-ctl not installed (apt install v4l-utils)",
            "applied": {},
            "errors": {},
        }

    applied: dict[str, int] = {}
    errors: dict[str, str] = {}
    # One invocation is faster and atomic enough for slider drags.
    payload = ",".join(f"{name}={int(value)}" for name, value in values.items())
    result = subprocess.run(
        ["v4l2-ctl", "-d", device, "-c", payload],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if result.returncode == 0:
        applied = {name: int(value) for name, value in values.items()}
    else:
        # Fall back to one-by-one so a single bad key does not block the rest.
        for name, value in values.items():
            one = subprocess.run(
                ["v4l2-ctl", "-d", device, "-c", f"{name}={int(value)}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if one.returncode == 0:
                applied[name] = int(value)
            else:
                errors[name] = (one.stderr or one.stdout or "failed").strip()

    ok = bool(applied) and not errors
    if errors:
        log.warning("Some V4L2 controls failed on %s: %s", device, errors)
    return {"ok": ok or bool(applied), "applied": applied, "errors": errors}


def get_control_values(device: str, names: list[str] | None = None) -> dict[str, int]:
    controls = list_controls(device)
    if names is not None:
        wanted = set(names)
        controls = [c for c in controls if c.name in wanted]
    return {c.name: c.value for c in controls}
