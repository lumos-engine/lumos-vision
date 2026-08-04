"""Minimal HyperHDR JSON API: enable/disable the LED device (0W when off)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse

from processor.utils.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT_SEC = 1.5


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
    url = _json_rpc_url(base_url)
    if not url:
        return {"ok": True, "skipped": True, "enabled": bool(enabled)}

    payload = {
        "command": "componentstate",
        "componentstate": {"component": "LEDDEVICE", "state": bool(enabled)},
    }
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
        log.warning("HyperHDR LEDDEVICE %s failed: %s", "on" if enabled else "off", exc)
        return {"ok": False, "error": str(exc), "enabled": bool(enabled)}

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
        log.warning(
            "HyperHDR LEDDEVICE %s rejected: %s",
            "on" if enabled else "off",
            parsed.get("error") or parsed,
        )
        return {"ok": False, "error": parsed.get("error") or f"HTTP {status}", "response": parsed}

    log.info("HyperHDR LEDDEVICE %s", "enabled" if enabled else "disabled")
    return {"ok": True, "enabled": bool(enabled), "response": parsed}
