"""Lumos OS REST client (LED box that runs HyperHDR as a plugin).

Not HyperHDR JSON-RPC (``power.hyperhdr_url``). Brightness is only pushed
when HyperHDR is the active plugin so we do not fight a fallback or a
local UI override.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

from processor.utils.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT_SEC = 1.5
LED_BRIGHTNESS_MIN = 0
LED_BRIGHTNESS_MAX = 255
DEFAULT_LED_BRIGHTNESS = 128


def clamp_led_brightness(value: Any) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        number = DEFAULT_LED_BRIGHTNESS
    return max(LED_BRIGHTNESS_MIN, min(LED_BRIGHTNESS_MAX, number))


def normalize_base_url(base: str) -> str:
    """``192.168.1.230`` or ``http://host/api/v1`` → ``http://host``."""
    text = (base or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def hyperhdr_in_use(status: Mapping[str, Any] | None) -> bool:
    """True when Lumos OS is actually showing HyperHDR.

    Prefer ``hyperhdr_active`` (needs a firmware OTA). Until that field
    exists, ``active_plugin == "hyperhdr"`` is the same check. Fallback
    and local override mean HyperHDR is not driving the LEDs.
    """
    data = dict(status or {})
    if bool(data.get("in_fallback")):
        return False
    if bool(data.get("local_override")):
        return False
    if "hyperhdr_active" in data and data.get("hyperhdr_active") is not None:
        return bool(data.get("hyperhdr_active"))
    return str(data.get("active_plugin") or "").strip().lower() == "hyperhdr"


def _open_json(
    url: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    opener: Any = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=float(timeout_sec)) as response:
            raw = response.read()
            status = getattr(response, "status", None) or response.getcode()
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read() or b""
        except Exception:
            raw = b""
        status = int(getattr(exc, "code", 0) or 0)
        parsed = _parse_json_body(raw)
        error = ""
        if isinstance(parsed, dict):
            error = str(
                parsed.get("error") or parsed.get("message") or parsed.get("code") or ""
            )
        return {
            "ok": False,
            "error": error or f"HTTP {status}",
            "status": status,
            "response": parsed,
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": str(exc)}

    parsed = _parse_json_body(raw)

    if not (200 <= int(status) < 300):
        error = ""
        if isinstance(parsed, dict):
            error = str(parsed.get("error") or parsed.get("message") or "")
        return {
            "ok": False,
            "error": error or f"HTTP {status}",
            "status": status,
            "response": parsed,
        }
    return {"ok": True, "status": status, "response": parsed}


def _parse_json_body(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {"raw": raw.decode("utf-8", errors="replace")[:200]}


def normalize_vision_output_mode(value: Any) -> str | None:
    """Map Screen Sight ``led_path`` / aliases onto the Lumos OS vision mode."""
    text = str(value or "").strip().lower()
    if text in {"ddp", "direct", "wled", "lumos_vision"}:
        return "ddp"
    if text == "hyperhdr":
        return "hyperhdr"
    return None


def set_vision_output(
    base_url: str,
    mode: Any,
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    opener: Any = None,
) -> dict[str, Any]:
    """POST ``/api/v1/vision/output`` so the box plugin matches Screen Sight.

    Body is only ``{"mode": "ddp"|"hyperhdr"}`` — never ``color_mode``. Direct
    DDP colour (``rgb`` / ``rgbw_off`` / ``rgbw``) stays in Screen Sight config.

    Allowed on the box only when HyperHDR or Lumos Vision is selected (or the
    stream timed out). A 409 ``plugin_not_selected`` is logged, not raised.
    """
    host = normalize_base_url(base_url)
    resolved = normalize_vision_output_mode(mode)
    if not host:
        return {"ok": True, "skipped": True, "reason": "no_url"}
    if resolved is None:
        return {"ok": False, "error": f"invalid vision output mode {mode!r}"}

    result = _open_json(
        urljoin(host + "/", "api/v1/vision/output"),
        method="POST",
        payload={"mode": resolved},
        timeout_sec=timeout_sec,
        opener=opener,
    )
    result["mode"] = resolved
    if result.get("ok"):
        log.info("Lumos OS vision output %s", resolved)
    elif int(result.get("status") or 0) == 409:
        log.warning(
            "Lumos OS refused vision output %s (%s). "
            "Select HyperHDR or Lumos Vision on the box first "
            "(POST /api/v1/plugin/hyperhdr or /api/v1/plugin/lumos_vision).",
            resolved,
            result.get("error") or "plugin_not_selected",
        )
    else:
        log.warning(
            "Lumos OS vision output %s failed: %s",
            resolved,
            result.get("error"),
        )
    return result


def fetch_status(
    base_url: str,
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    opener: Any = None,
) -> dict[str, Any]:
    host = normalize_base_url(base_url)
    if not host:
        return {"ok": True, "skipped": True, "reason": "no_url"}
    result = _open_json(
        urljoin(host + "/", "api/v1/status"),
        method="GET",
        timeout_sec=timeout_sec,
        opener=opener,
    )
    payload = result.get("response")
    if result.get("ok") and isinstance(payload, dict):
        result["status_payload"] = payload
    return result


def set_brightness(
    base_url: str,
    brightness: int,
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    opener: Any = None,
) -> dict[str, Any]:
    host = normalize_base_url(base_url)
    value = clamp_led_brightness(brightness)
    if not host:
        return {"ok": True, "skipped": True, "reason": "no_url", "brightness": value}
    result = _open_json(
        urljoin(host + "/", "api/v1/brightness"),
        method="POST",
        payload={"brightness": value},
        timeout_sec=timeout_sec,
        opener=opener,
    )
    result["brightness"] = value
    return result


def apply_led_brightness(
    base_url: str,
    brightness: int,
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    opener: Any = None,
) -> dict[str, Any]:
    """GET status, POST brightness only if HyperHDR is in use."""
    host = normalize_base_url(base_url)
    value = clamp_led_brightness(brightness)
    if not host:
        return {"ok": True, "skipped": True, "reason": "no_url", "brightness": value}

    status_result = fetch_status(host, timeout_sec=timeout_sec, opener=opener)
    if status_result.get("skipped"):
        return {**status_result, "brightness": value}
    if not status_result.get("ok"):
        log.warning(
            "Lumos OS status failed (%s): %s",
            host,
            status_result.get("error"),
        )
        return {
            "ok": False,
            "error": status_result.get("error") or "status failed",
            "brightness": value,
            "status": status_result,
        }

    payload = status_result.get("status_payload")
    if not isinstance(payload, dict):
        payload = {}
    if not hyperhdr_in_use(payload):
        reason = "hyperhdr_inactive"
        log.debug(
            "Skipping Lumos OS brightness %s: HyperHDR is not active (plugin=%s)",
            value,
            payload.get("active_plugin"),
        )
        return {
            "ok": True,
            "skipped": True,
            "reason": reason,
            "brightness": value,
            "hyperhdr_active": False,
            "status_payload": payload,
        }

    result = set_brightness(
        host, value, timeout_sec=timeout_sec, opener=opener
    )
    if result.get("ok"):
        log.info("Lumos OS LED brightness %s", value)
    else:
        log.warning(
            "Lumos OS brightness %s failed: %s",
            value,
            result.get("error"),
        )
    result["hyperhdr_active"] = True
    result["status_payload"] = payload
    return result
