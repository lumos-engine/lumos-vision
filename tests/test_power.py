"""TV presence idle and HyperHDR LEDDEVICE hard-off."""

from __future__ import annotations

import io
import json

import numpy as np
import pytest

from processor.app import Processor
from processor.config.schema import Config
from processor.utils.hyperhdr_leds import _json_rpc_url, set_led_device
from processor.utils.tv_presence import PresenceMonitor, ping_host


def test_empty_tv_host_ping_is_treated_as_online():
    assert ping_host("") is True
    assert ping_host("   ") is True


def test_presence_monitor_debounces_offline_and_online():
    monitor = PresenceMonitor(offline_checks=2, online_checks=2, online=True)
    assert monitor.update(False) is None
    assert monitor.online is True
    assert monitor.fail_streak == 1
    assert monitor.update(False) == "offline"
    assert monitor.online is False
    assert monitor.fail_streak == 2

    assert monitor.update(True) is None
    assert monitor.online is False
    assert monitor.ok_streak == 1
    assert monitor.update(True) == "online"
    assert monitor.online is True


def test_presence_monitor_single_check_can_trip_immediately():
    monitor = PresenceMonitor(offline_checks=1, online_checks=1, online=True)
    assert monitor.update(False) == "offline"
    assert monitor.update(True) == "online"


def test_json_rpc_url_normalises_base():
    assert _json_rpc_url("http://127.0.0.1:8090") == "http://127.0.0.1:8090/json-rpc"
    assert _json_rpc_url("http://127.0.0.1:8090/json-rpc") == "http://127.0.0.1:8090/json-rpc"
    assert _json_rpc_url("") == ""


def test_set_led_device_skips_empty_url():
    assert set_led_device("", False) == {"ok": True, "skipped": True, "enabled": False}


def test_set_led_device_posts_componentstate():
    seen = {}

    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_open(request, timeout=1.5):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode())
        seen["timeout"] = timeout
        return _Resp(b'{"success": true}')

    result = set_led_device("http://127.0.0.1:8090", False, opener=fake_open)
    assert result["ok"] is True
    assert seen["url"].endswith("/json-rpc")
    assert seen["body"] == {
        "command": "componentstate",
        "componentstate": {"component": "LEDDEVICE", "state": False},
    }


def _processor(**power) -> Processor:
    config = Config.from_dict(
        {
            "camera": {"source": "synthetic", "replay_fps": 60},
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
            "power": power,
        }
    )
    app = Processor(config)
    return app


def test_power_disabled_never_idles(monkeypatch):
    monkeypatch.setattr("processor.app.ping_host", lambda *a, **k: False)
    app = _processor(tv_host="")
    app.start()
    try:
        assert app.source is not None
        assert app._idle is False
        app._last_power_check = 0.0
        app._tick_power()
        assert app._idle is False
        assert app.status()["power"]["enabled"] is False
    finally:
        app.shutdown()


def test_enter_and_leave_idle_releases_camera_and_toggles_leds(monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(
        "processor.app.set_led_device",
        lambda url, enabled, **kw: calls.append(bool(enabled)) or {"ok": True},
    )
    monkeypatch.setattr("processor.app.ping_host", lambda *a, **k: True)

    app = _processor(
        tv_host="192.168.1.244",
        hyperhdr_url="http://127.0.0.1:8090",
        failed_pings=1,
        success_pings=1,
        check_interval_sec=1.0,
    )
    app.start()
    try:
        assert app.source is not None
        assert calls == [True]

        monkeypatch.setattr("processor.app.ping_host", lambda *a, **k: False)
        app._last_power_check = 0.0
        app._tick_power()
        assert app._idle is True
        assert app.source is None
        assert app._leds_off is True
        assert calls[-1] is False

        app._write_idle_frame()
        assert app._black_frame is not None
        assert app._black_frame.shape == (180, 320, 3)
        assert np.all(app._black_frame == 0)

        monkeypatch.setattr("processor.app.ping_host", lambda *a, **k: True)
        app._last_power_check = 0.0
        app._tick_power()
        assert app._idle is False
        assert app.source is not None
        assert app._leds_off is False
        assert calls[-1] is True

        status = app.status()["power"]
        assert status["enabled"] is True
        assert status["tv_host"] == "192.168.1.244"
        assert status["idle"] is False
        assert status["online"] is True
    finally:
        app.shutdown()


def test_starts_idle_when_tv_already_offline(monkeypatch):
    monkeypatch.setattr("processor.app.ping_host", lambda *a, **k: False)
    monkeypatch.setattr(
        "processor.app.set_led_device", lambda url, enabled, **kw: {"ok": True}
    )
    app = _processor(tv_host="192.168.1.244", hyperhdr_url="http://127.0.0.1:8090")
    app.start()
    try:
        assert app._idle is True
        assert app.source is None
        assert app.sinks is not None
        assert app.status()["power"]["idle"] is True
    finally:
        app.shutdown()


def test_config_round_trip_includes_power():
    config = Config.from_dict(
        {
            "power": {
                "tv_host": "192.168.1.244",
                "idle_fps": 3.0,
                "failed_pings": 5,
                "success_pings": 2,
            }
        }
    )
    assert config.power.tv_host == "192.168.1.244"
    assert config.power.idle_fps == 3.0
    assert config.power.hyperhdr_url == "http://127.0.0.1:8090"
    assert config.power.failed_pings == 5
    assert config.power.success_pings == 2


def test_failed_pings_from_config_drives_presence_monitor():
    app = _processor(tv_host="192.168.1.244", failed_pings=4, success_pings=3)
    assert app._presence.offline_checks == 4
    assert app._presence.online_checks == 3
