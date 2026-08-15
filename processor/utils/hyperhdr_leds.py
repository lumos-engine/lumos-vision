"""Minimal HyperHDR JSON API.

LEDDEVICE on/off is used for TV-presence idle (0W when the TV is gone).
VIDEOGRABBER on/off releases ``/dev/video10`` so Screen Sight can set the
loopback format, then makes HyperHDR rescan the capture device.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse

from processor.utils.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT_SEC = 1.5
GRABBER_TIMEOUT_SEC = 0.4
_VIDEO_GRABBER_COMPONENTS = ("VIDEOGRABBER", "V4L")


def _json_rpc_url(base: str) -> str:
    text = (base or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    if not parsed.scheme:
        text = "http://" + text
    if text.endswith("/json-rpc"):
        return text
    return urljoin(text + "/", "json-rpc")


def _post_json_rpc(
    base_url: str,
    payload: dict[str, Any],
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    opener: Any = None,
) -> dict[str, Any]:
    url = _json_rpc_url(base_url)
    if not url:
        return {"ok": True, "skipped": True}

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=float(timeout_sec)) as response:
            raw = response.read()
            status = getattr(response, "status", None) or response.getcode()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": str(exc)}

    parsed: dict[str, Any] = {}
    if raw:
        try:
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                parsed = data
        except json.JSONDecodeError:
            parsed = {"raw": raw.decode("utf-8", errors="replace")[:200]}

    success = bool(parsed.get("success", status == 200))
    if not success:
        return {
            "ok": False,
            "error": parsed.get("error") or f"HTTP {status}",
            "response": parsed,
        }
    return {"ok": True, "response": parsed}


def set_component(
    base_url: str,
    component: str,
    enabled: bool,
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    opener: Any = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Set a HyperHDR component (``LEDDEVICE``, ``VIDEOGRABBER``, …)."""
    url = _json_rpc_url(base_url)
    if not url:
        return {"ok": True, "skipped": True, "enabled": bool(enabled)}

    payload = {
        "command": "componentstate",
        "componentstate": {"component": str(component), "state": bool(enabled)},
    }
    result = _post_json_rpc(
        base_url, payload, timeout_sec=timeout_sec, opener=opener
    )
    result["enabled"] = bool(enabled)
    result["component"] = str(component)
    if result.get("skipped"):
        return result
    if not result.get("ok"):
        if not quiet:
            log.warning(
                "HyperHDR %s %s failed: %s",
                component,
                "on" if enabled else "off",
                result.get("error"),
            )
        return result
    log.info("HyperHDR %s %s", component, "enabled" if enabled else "disabled")
    return result


def set_led_device(
    base_url: str,
    enabled: bool,
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    opener: Any = None,
) -> dict[str, Any]:
    """Set HyperHDR ``LEDDEVICE`` component state.

    Returns ``{"ok": True/False, ...}``.  Empty ``base_url`` is a no-op success
    (caller skipped LED hard-off).
    """
    return set_component(
        base_url,
        "LEDDEVICE",
        enabled,
        timeout_sec=timeout_sec,
        opener=opener,
    )


def set_video_grabber(
    base_url: str,
    enabled: bool,
    *,
    timeout_sec: float = GRABBER_TIMEOUT_SEC,
    opener: Any = None,
) -> dict[str, Any]:
    """Set HyperHDR's USB/V4L grabber. Tries ``VIDEOGRABBER`` then ``V4L``."""
    url = _json_rpc_url(base_url)
    if not url:
        return {"ok": True, "skipped": True, "enabled": bool(enabled)}

    last: dict[str, Any] = {}
    for name in _VIDEO_GRABBER_COMPONENTS:
        last = set_component(
            base_url,
            name,
            enabled,
            timeout_sec=timeout_sec,
            opener=opener,
            quiet=True,
        )
        if last.get("ok") or last.get("skipped"):
            return last
        error = str(last.get("error") or "")
        if "Connection refused" in error or "Name or service not known" in error:
            break
    if last.get("error"):
        log.debug(
            "HyperHDR video grabber %s failed: %s",
            "on" if enabled else "off",
            last.get("error"),
        )
    return last


def refresh_video_grabber(
    base_url: str,
    *,
    timeout_sec: float = GRABBER_TIMEOUT_SEC,
    opener: Any = None,
) -> dict[str, Any]:
    """Drop and re-open the V4L grabber so it rescans ``/dev/video*``."""
    url = _json_rpc_url(base_url)
    if not url:
        return {"ok": True, "skipped": True}

    off = set_video_grabber(
        base_url, False, timeout_sec=timeout_sec, opener=opener
    )
    on = set_video_grabber(
        base_url, True, timeout_sec=timeout_sec, opener=opener
    )
    if on.get("skipped"):
        return on
    ok = bool(on.get("ok"))
    if ok:
        log.info("HyperHDR video grabber rescanned")
    return {
        "ok": ok,
        "off": off,
        "on": on,
        "error": None if ok else on.get("error") or off.get("error"),
    }
