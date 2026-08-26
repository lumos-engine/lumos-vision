"""Lumos OS REST: HyperHDR-gated LED brightness."""

from __future__ import annotations

import io
import json

from processor.utils.lumos_os import (
    apply_led_brightness,
    clamp_led_brightness,
    hyperhdr_in_use,
    normalize_base_url,
    set_brightness,
)


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_clamp_and_url_normalise():
    assert clamp_led_brightness(-4) == 0
    assert clamp_led_brightness(300) == 255
    assert clamp_led_brightness("128") == 128
    assert normalize_base_url("192.168.1.230") == "http://192.168.1.230"
    assert normalize_base_url("http://192.168.1.230/api/v1") == "http://192.168.1.230"
    assert normalize_base_url("") == ""


def test_hyperhdr_in_use_prefers_explicit_flag_and_falls_back_to_plugin():
    assert hyperhdr_in_use({"hyperhdr_active": True}) is True
    assert hyperhdr_in_use({"hyperhdr_active": False, "active_plugin": "hyperhdr"}) is False
    assert hyperhdr_in_use({"active_plugin": "hyperhdr"}) is True
    assert hyperhdr_in_use({"active_plugin": "clock"}) is False
    assert hyperhdr_in_use({"active_plugin": "hyperhdr", "in_fallback": True}) is False
    assert hyperhdr_in_use({"active_plugin": "hyperhdr", "local_override": True}) is False
    assert hyperhdr_in_use({}) is False


def test_apply_skips_empty_url():
    assert apply_led_brightness("", 80)["skipped"] is True
    assert apply_led_brightness("   ", 80)["skipped"] is True


def test_apply_posts_when_active_plugin_is_hyperhdr():
    seen = []

    def fake_open(request, timeout=1.5):
        seen.append((request.get_method(), request.full_url, request.data))
        if request.full_url.endswith("/status"):
            return _Resp(b'{"active_plugin":"hyperhdr"}')
        return _Resp(b'{"ok":true}')

    result = apply_led_brightness("192.168.1.230", 40, opener=fake_open)
    assert result["ok"] is True
    assert result.get("skipped") is not True
    assert result["brightness"] == 40
    methods = [row[0] for row in seen]
    assert methods == ["GET", "POST"]
    assert seen[0][1] == "http://192.168.1.230/api/v1/status"
    assert seen[1][1] == "http://192.168.1.230/api/v1/brightness"
    assert json.loads(seen[1][2].decode()) == {"brightness": 40}


def test_apply_posts_when_hyperhdr_active_flag_is_true():
    def fake_open(request, timeout=1.5):
        if request.full_url.endswith("/status"):
            return _Resp(b'{"hyperhdr_active":true,"active_plugin":"hyperhdr"}')
        return _Resp(b"{}")

    result = apply_led_brightness("http://192.168.1.230", 255, opener=fake_open)
    assert result["ok"] is True
    assert result["hyperhdr_active"] is True


def test_apply_skips_when_another_plugin_is_active():
    seen = []

    def fake_open(request, timeout=1.5):
        seen.append(request.full_url)
        return _Resp(b'{"active_plugin":"clock","hyperhdr_active":false}')

    result = apply_led_brightness("http://192.168.1.230", 90, opener=fake_open)
    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["reason"] == "hyperhdr_inactive"
    assert seen == ["http://192.168.1.230/api/v1/status"]


def test_apply_skips_fallback_and_local_override():
    def fake_open(request, timeout=1.5):
        return _Resp(b'{"active_plugin":"hyperhdr","in_fallback":true}')

    skipped = apply_led_brightness("http://192.168.1.230", 10, opener=fake_open)
    assert skipped["skipped"] is True

    def fake_override(request, timeout=1.5):
        return _Resp(b'{"active_plugin":"hyperhdr","local_override":true}')

    overridden = apply_led_brightness(
        "http://192.168.1.230", 10, opener=fake_override
    )
    assert overridden["skipped"] is True


def test_set_brightness_skips_empty_url():
    assert set_brightness("", 12)["skipped"] is True


def test_vision_output_mode_aliases():
    from processor.utils.lumos_os import normalize_vision_output_mode

    assert normalize_vision_output_mode("hyperhdr") == "hyperhdr"
    assert normalize_vision_output_mode("ddp") == "ddp"
    assert normalize_vision_output_mode("lumos_vision") == "ddp"
    assert normalize_vision_output_mode("clock") is None


def test_set_vision_output_skips_empty_url():
    from processor.utils.lumos_os import set_vision_output

    assert set_vision_output("", "ddp")["skipped"] is True


def test_set_vision_output_posts_mode():
    from processor.utils.lumos_os import set_vision_output

    seen = []

    def fake_open(request, timeout=1.5):
        seen.append((request.get_method(), request.full_url, request.data))
        return _Resp(b'{"ok":true}')

    result = set_vision_output("192.168.1.230", "ddp", opener=fake_open)
    assert result["ok"] is True
    assert result["mode"] == "ddp"
    assert seen[0][0] == "POST"
    assert seen[0][1] == "http://192.168.1.230/api/v1/vision/output"
    assert json.loads(seen[0][2].decode()) == {"mode": "ddp"}

    seen.clear()
    result = set_vision_output("http://192.168.1.230", "hyperhdr", opener=fake_open)
    assert json.loads(seen[0][2].decode()) == {"mode": "hyperhdr"}


def test_set_vision_output_409_is_not_raised():
    from processor.utils.lumos_os import set_vision_output

    class _Conflict(_Resp):
        status = 409

    def fake_open(request, timeout=1.5):
        return _Conflict(b'{"error":"plugin_not_selected"}')

    result = set_vision_output("http://192.168.1.230", "ddp", opener=fake_open)
    assert result["ok"] is False
    assert result["status"] == 409
    assert result["error"] == "plugin_not_selected"


def test_processor_led_path_switch_calls_vision_output(monkeypatch):
    from processor.app import Processor
    from processor.config.schema import Config

    seen = []
    monkeypatch.setattr(
        "processor.app.set_vision_output",
        lambda url, mode, **kwargs: seen.append((url, mode)) or {"ok": True, "mode": mode},
    )
    monkeypatch.setattr(
        "processor.app.apply_led_brightness",
        lambda url, value, **kwargs: {"ok": True, "skipped": True},
    )
    app = Processor(
        Config.from_dict(
            {
                "output": {"v4l2": {"enabled": False}},
                "lumos_os": {"url": "http://192.168.1.230"},
            }
        )
    )
    try:
        app.update_config({"output.led_path": "ddp"})
        assert app.config.output.led_path == "ddp"
        assert seen[-1] == ("http://192.168.1.230", "ddp")
        app.update_config({"output.led_path": "hyperhdr"})
        assert seen[-1] == ("http://192.168.1.230", "hyperhdr")
    finally:
        app.shutdown()
