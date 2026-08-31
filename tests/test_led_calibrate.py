"""LED strip colour sync: wire order, 3×3 learn loop, DDP flood."""

from __future__ import annotations

import numpy as np

from processor.config.schema import Config, DdpConfig
from processor.led.rgbw import IDENTITY_RGB_FLAT, wire_led_pixels
from processor.utils.led_calibrate import LedCalibrationSession, solve_led_matrix


def test_config_defaults_include_led_sync():
    config = Config()
    assert config.output.ddp.rgb_order == "rgb"
    assert list(config.output.ddp.color_matrix) == IDENTITY_RGB_FLAT
    assert config.output.ddp.calibrated_at == ""


def test_grbw_alias_normalises_to_grb():
    config = Config.from_dict(
        {"output": {"led_path": "ddp", "ddp": {"host": "10.0.0.5", "rgb_order": "grbw"}}}
    )
    assert config.output.ddp.rgb_order == "grb"


def test_grb_permutation_puts_green_on_byte_zero():
    green = np.array([[0, 255, 0]], np.uint8)
    rgb = wire_led_pixels(green, "rgb", rgb_order="rgb", apply_matrix=False)
    assert list(rgb[0]) == [0, 255, 0]

    grb = wire_led_pixels(green, "rgbw_off", rgb_order="grb", apply_matrix=False)
    assert grb.shape == (1, 4)
    assert list(grb[0]) == [255, 0, 0, 0]
    assert grb[0, 3] == 0


def test_rgbw_off_stride_stays_four_bytes_after_matrix():
    pixels = np.array([[10, 20, 30], [40, 50, 60]], np.uint8)
    packed = wire_led_pixels(
        pixels,
        "rgbw_off",
        rgb_order="rgb",
        matrix=IDENTITY_RGB_FLAT,
    )
    assert packed.shape == (2, 4)
    assert packed[0, 3] == 0
    assert packed[1, 3] == 0
    assert list(packed[0, :3]) == [10, 20, 30]


def test_solve_identity_when_every_patch_matches():
    pairs = {
        "red": ((255, 0, 0), (255, 0, 0)),
        "green": ((0, 255, 0), (0, 255, 0)),
        "blue": ((0, 0, 255), (0, 0, 255)),
        "white": ((255, 255, 255), (255, 255, 255)),
    }
    matrix, notes = solve_led_matrix(pairs)
    assert np.allclose(matrix, np.eye(3), atol=0.05)
    assert any("identity" in note for note in notes)


def test_solve_swap_maps_green_to_red_drive():
    pairs = {
        "red": ((255, 0, 0), (0, 255, 0)),
        "green": ((0, 255, 0), (255, 0, 0)),
        "blue": ((0, 0, 255), (0, 0, 255)),
        "white": ((255, 255, 255), (255, 255, 255)),
    }
    matrix, _notes = solve_led_matrix(pairs)
    green = np.array([0.0, 255.0, 0.0])
    driven = green @ matrix
    assert driven[0] > 200
    assert driven[1] < 40


def test_session_match_all_then_solve_is_identity():
    session = LedCalibrationSession()
    session.start(IDENTITY_RGB_FLAT)
    for _ in session.patches:
        session.match()
    status = session.solve()
    assert status["state"] == "ready"
    assert np.allclose(np.array(status["solution"]).reshape(3, 3), np.eye(3), atol=0.05)


def test_session_adjust_records_drive():
    session = LedCalibrationSession()
    session.start()
    names = [name for name, _rgb in session.patches]
    session.goto(patch="green")
    session.begin_adjust()
    session.set_drive(255, 0, 0)
    session.commit_adjust()
    intended, driven = session.records["green"]
    assert intended == (0, 255, 0)
    assert driven == (255, 0, 0)
    assert session.patch[0] == names[names.index("green") + 1]


def test_ddp_sink_flood_and_hold_off_encode_after_matrix(monkeypatch):
    from processor.output.ddp import DdpSink

    sent: list[bytes] = []

    class _FakeSock:
        def setblocking(self, _flag):
            return None

        def sendto(self, packet, _addr):
            sent.append(packet)

        def close(self):
            return None

    monkeypatch.setattr(
        "processor.output.ddp.socket.socket", lambda *a, **k: _FakeSock()
    )
    sink = DdpSink(
        DdpConfig(
            enabled=True,
            host="10.0.0.5",
            leds_top=4,
            color_mode="rgbw_off",
            rgb_order="grb",
        )
    )
    sink.open(8, 8)
    assert sink.flood((0, 255, 0), apply_matrix=False) is True
    payload = sent[-1][10:]
    assert payload[0:4] == b"\xff\x00\x00\x00"
    assert payload[3::4] == b"\x00\x00\x00\x00"

    sent.clear()
    sink.clear_flood()
    assert sink.hold_off() is True
    black = sent[-1][10:]
    assert black == b"\x00" * 16


class _NullSock:
    def setblocking(self, _flag):
        return None

    def sendto(self, packet, _addr):
        return None

    def close(self):
        return None


def test_processor_led_sync_apply_does_not_touch_profiles(monkeypatch):
    from processor.app import Processor

    monkeypatch.setattr(
        "processor.output.ddp.socket.socket",
        lambda *a, **k: _NullSock(),
    )
    app = Processor(
        Config.from_dict(
            {
                "camera": {"source": "synthetic", "replay_fps": 60},
                "output": {
                    "width": 320,
                    "height": 180,
                    "fps": 30,
                    "led_path": "ddp",
                    "v4l2": {"enabled": False},
                    "ddp": {
                        "host": "10.0.0.5",
                        "leds_top": 4,
                        "color_mode": "rgbw_off",
                    },
                },
                "logging": {"stats_interval": 0},
            }
        )
    )
    app.start()
    try:
        result = app.apply_led_color({"action": "test", "channel": "g"})
        assert result["ok"] is True
        assert result["test"] == "g"
        started = app.apply_led_color({"action": "start"})
        assert started["ok"] is True
        assert started["state"] == "running"
        for _ in range(started["total"]):
            matched = app.apply_led_color({"action": "match"})
            assert matched["ok"] is True
        solved = app.apply_led_color({"action": "solve"})
        assert solved["ok"] is True
        applied = app.apply_led_color({"action": "apply"})
        assert applied["ok"] is True
        assert app.config.output.ddp.calibrated_at
        assert np.allclose(
            np.array(app.config.output.ddp.color_matrix).reshape(3, 3),
            np.eye(3),
            atol=0.05,
        )
        assert not app.config.color.profiles.slots
    finally:
        app.shutdown()


def test_led_sync_requires_direct_path():
    from processor.app import Processor

    app = Processor(
        Config.from_dict(
            {
                "camera": {"source": "synthetic"},
                "output": {"v4l2": {"enabled": False}},
                "logging": {"stats_interval": 0},
            }
        )
    )
    result = app.apply_led_color({"action": "start"})
    assert result["ok"] is False
    assert "Direct" in result["error"]
