"""Environment colour profiles: keys, fallback, legacy absorb, switching."""

from __future__ import annotations

from processor.config.loader import apply_updates, config_to_dict, load_config, save_config
from processor.config.schema import Config, ConfigError
from processor.utils.color_calibrate import IDENTITY_MATRIX_FLAT
from processor.utils.color_profiles import (
    all_combos,
    bind_config,
    migrate_slots_add_dimension,
    profile_status,
    resolve_selection,
    slot_is_calibrated,
    slot_key,
    validate_profile_id,
)
import pytest


def test_default_profiles_are_day_night_times_three_lights_times_two_brightness():
    cfg = Config()
    combos = all_combos(cfg.color.profiles)
    assert len(combos) == 12
    assert cfg.color.profiles.selection == {
        "time_of_day": "night",
        "lighting": "lights_off",
        "brightness": "full",
    }
    assert slot_key(cfg.color.profiles) == (
        "time_of_day=night|lighting=lights_off|brightness=full"
    )


def test_uncalibrated_combo_is_passthrough():
    cfg = Config.from_dict({"color": {"gamma": 1.25, "saturation": 1.4}})
    assert cfg.color.matrix_enabled is False
    assert cfg.color.white_balance == "off"
    assert cfg.color.gamma == 1.25
    assert cfg.color.saturation == 1.4
    status = profile_status(cfg.color)
    assert status["mode"] == "none"
    assert status["calibrated"] is False
    assert status["calibrated_count"] == 0


def test_unknown_option_is_rejected():
    with pytest.raises(ConfigError, match="unknown option"):
        resolve_selection(
            Config().color.profiles,
            {"lighting": "disco"},
        )


def test_yaml_boolean_word_rejected_as_id():
    with pytest.raises(ConfigError, match="YAML boolean"):
        validate_profile_id("off", path="lighting")


def test_legacy_live_matrix_is_absorbed_into_active_slot():
    cfg = Config.from_dict(
        {
            "color": {
                "white_balance": "manual",
                "matrix_enabled": True,
                "matrix": [1.1, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.9],
                "gamma": 1.15,
                "calibration": {"calibrated_at": "2026-08-01T00:00:00Z"},
            }
        }
    )
    key = slot_key(cfg.color.profiles)
    slot = cfg.color.profiles.slots[key]
    assert slot_is_calibrated(slot)
    assert cfg.color.matrix_enabled is True
    assert cfg.color.gamma == pytest.approx(1.15)
    assert profile_status(cfg.color)["calibrated"] is True


def test_switching_to_empty_combo_clears_the_matrix():
    cfg = Config.from_dict(
        {
            "color": {
                "white_balance": "manual",
                "matrix_enabled": True,
                "matrix": [1.2, 0, 0, 0, 1, 0, 0, 0, 0.8],
                "calibration": {"calibrated_at": "2026-08-01T00:00:00Z"},
            }
        }
    )
    cfg = apply_updates(
        cfg, {"color.profiles.selection.time_of_day": "day"}
    )
    cfg = bind_config(cfg)
    assert cfg.color.profiles.selection["time_of_day"] == "day"
    assert cfg.color.matrix_enabled is False
    assert cfg.color.white_balance == "off"
    assert cfg.color.gamma == pytest.approx(1.0)
    assert cfg.color.matrix == IDENTITY_MATRIX_FLAT


def test_calibrated_slot_is_restored_when_switching_back():
    cfg = Config.from_dict(
        {
            "color": {
                "profiles": {
                    "selection": {"time_of_day": "night", "lighting": "bed"},
                    "slots": {
                        "time_of_day=night|lighting=bed": {
                            "calibrated_at": "2026-08-15T12:00:00Z",
                            "white_balance": "manual",
                            "matrix_enabled": True,
                            "matrix": [1.05, 0, 0, 0, 1, 0, 0, 0, 0.95],
                            "gamma": 1.08,
                            "saturation": 1.0,
                        }
                    },
                }
            }
        }
    )
    assert cfg.color.matrix_enabled is True
    assert cfg.color.gamma == pytest.approx(1.08)
    cfg = apply_updates(cfg, {"color.profiles.selection.lighting": "large"})
    cfg = bind_config(cfg)
    assert cfg.color.matrix_enabled is False
    cfg = apply_updates(cfg, {"color.profiles.selection.lighting": "bed"})
    cfg = bind_config(cfg)
    assert cfg.color.matrix_enabled is True
    assert cfg.color.gamma == pytest.approx(1.08)


def test_add_dimension_rewrites_slot_keys():
    from processor.config.schema import ColorProfileSlot

    original = ColorProfileSlot(calibrated_at="2026-08-15T00:00:00Z", matrix_enabled=True)
    old_key = "time_of_day=night|lighting=lights_off"
    migrated = migrate_slots_add_dimension(
        {old_key: original},
        dimension_id="curtains",
        default_option="open",
        order=["time_of_day", "lighting", "curtains"],
    )
    new_key = "time_of_day=night|lighting=lights_off|curtains=open"
    assert new_key in migrated
    assert migrated[new_key].calibrated_at == "2026-08-15T00:00:00Z"


def test_old_slot_keys_gain_brightness_full():
    cfg = Config.from_dict(
        {
            "color": {
                "profiles": {
                    "slots": {
                        "time_of_day=night|lighting=lights_off": {
                            "calibrated_at": "2026-08-15T00:00:00Z",
                            "matrix_enabled": True,
                        }
                    }
                }
            }
        }
    )
    key = "time_of_day=night|lighting=lights_off|brightness=full"
    assert key in cfg.color.profiles.slots
    assert slot_is_calibrated(cfg.color.profiles.slots[key])
    assert cfg.color.profiles.selection["brightness"] == "full"


def test_profile_round_trip_yaml(tmp_path):
    cfg = Config.from_dict(
        {
            "color": {
                "profiles": {
                    "selection": {"time_of_day": "day", "lighting": "large"},
                    "slots": {
                        "time_of_day=day|lighting=large": {
                            "calibrated_at": "2026-08-15T00:00:00Z",
                            "white_balance": "manual",
                            "matrix_enabled": True,
                            "gamma": 1.12,
                        }
                    },
                }
            }
        }
    )
    path = save_config(cfg, tmp_path / "config.yaml")
    reloaded = load_config(path)
    assert reloaded.color.profiles.selection["time_of_day"] == "day"
    assert reloaded.color.matrix_enabled is True
    assert reloaded.color.gamma == pytest.approx(1.12)
    data = config_to_dict(reloaded)
    assert "time_of_day=day|lighting=large|brightness=full" in data["color"]["profiles"]["slots"]


def test_profile_switch_restores_and_clears_phone_3a():
    cfg = Config.from_dict(
        {
            "color": {
                "profiles": {
                    "selection": {"time_of_day": "night", "lighting": "bed"},
                    "slots": {
                        "time_of_day=night|lighting=bed": {
                            "calibrated_at": "2026-08-15T12:00:00Z",
                            "white_balance": "manual",
                            "matrix_enabled": True,
                            "gamma": 1.08,
                            "camera": {
                                "af": "locked",
                                "ae": "locked",
                                "awb": "locked",
                                "iso": 250,
                                "exposure_ns": 12500000,
                                "focus_distance": 0.18,
                                "awb_gains": [1.7, 1.0, 1.0, 1.4],
                            },
                        }
                    },
                }
            }
        }
    )
    assert cfg.lumos_cam.ae == "locked"
    assert cfg.lumos_cam.iso == 250
    assert cfg.lumos_cam.exposure_ns == 12500000
    assert cfg.color.exposure.enabled is False

    cfg = apply_updates(cfg, {"color.profiles.selection.lighting": "large"})
    cfg = bind_config(cfg)
    assert cfg.lumos_cam.ae == "auto"
    assert cfg.lumos_cam.iso == 0
    assert cfg.color.matrix_enabled is False

    cfg = apply_updates(cfg, {"color.profiles.selection.lighting": "bed"})
    cfg = bind_config(cfg)
    assert cfg.lumos_cam.ae == "locked"
    assert cfg.lumos_cam.iso == 250
    assert cfg.lumos_cam.awb_gains[0] == pytest.approx(1.7)


def test_legacy_ae_lock_is_absorbed_into_the_active_slot():
    cfg = Config.from_dict(
        {
            "lumos_cam": {
                "ae": "locked",
                "iso": 400,
                "exposure_ns": 8000000,
            }
        }
    )
    slot = cfg.color.profiles.slots[slot_key(cfg.color.profiles)]
    assert slot.camera.ae == "locked"
    assert slot.camera.iso == 400
    assert cfg.lumos_cam.ae == "locked"


def test_camera_for_slot_follows_checkboxes_not_phone_overlay():
    from processor.config.schema import LumosCamConfig
    from processor.utils.color_profiles import camera_for_slot

    cam = camera_for_slot(
        {
            "af": "locked",
            "ae": "locked",
            "awb": "locked",
            "iso": 200,
            "exposure_ns": 1_000_000,
            "focus_distance": 0.2,
            "awb_gains": [1.5, 1.0, 1.0, 1.2],
        },
        LumosCamConfig(af="auto", ae="locked", awb="auto"),
    )
    assert cam.ae == "locked"
    assert cam.af == "auto"
    assert cam.awb == "auto"
    assert cam.iso == 200
    assert cam.exposure_ns == 1_000_000
    assert cam.focus_distance == -1.0
    assert cam.awb_gains == []


def test_processor_switches_between_slot_and_passthrough():
    from processor.app import Processor

    app = Processor(
        Config.from_dict(
            {
                "output": {"v4l2": {"enabled": False}},
                "color": {
                    "profiles": {
                        "selection": {"time_of_day": "night", "lighting": "bed"},
                        "slots": {
                            "time_of_day=night|lighting=bed": {
                                "calibrated_at": "2026-08-15T12:00:00Z",
                                "white_balance": "manual",
                                "matrix_enabled": True,
                                "gamma": 1.07,
                            }
                        },
                    }
                },
            }
        )
    )
    try:
        assert app.config.color.matrix_enabled is True
        off = app.set_color_profile({"lighting": "large"})
        assert off["ok"] is True
        assert off["calibrated"] is False
        assert app.config.color.matrix_enabled is False
        back = app.set_color_profile({"lighting": "bed"})
        assert back["calibrated"] is True
        assert app.config.color.gamma == pytest.approx(1.07)
    finally:
        app.shutdown()


def test_bind_config_restores_slot_led_brightness():
    cfg = Config.from_dict(
        {
            "lumos_os": {"url": "http://192.168.1.230", "led_brightness": 128},
            "color": {
                "profiles": {
                    "selection": {
                        "time_of_day": "night",
                        "lighting": "bed",
                        "brightness": "full",
                    },
                    "slots": {
                        "time_of_day=night|lighting=bed|brightness=full": {
                            "led_brightness": 40,
                        },
                        "time_of_day=night|lighting=large|brightness=full": {
                            "led_brightness": 200,
                        },
                    },
                }
            },
        }
    )
    assert cfg.lumos_os.led_brightness == 40
    assert profile_status(cfg.color)["led_brightness"] == 40
    cfg = apply_updates(cfg, {"color.profiles.selection.lighting": "large"})
    cfg = bind_config(cfg)
    assert cfg.lumos_os.led_brightness == 200
    assert profile_status(cfg.color)["led_brightness"] == 200


def test_processor_slider_stores_led_brightness_on_active_slot(monkeypatch):
    from processor.app import Processor

    seen = []
    monkeypatch.setattr(
        "processor.app.apply_led_brightness",
        lambda url, value, **kwargs: seen.append((url, value)) or {"ok": True},
    )
    app = Processor(
        Config.from_dict(
            {
                "output": {"v4l2": {"enabled": False}},
                "lumos_os": {"url": "http://192.168.1.230"},
                "color": {
                    "profiles": {
                        "selection": {"time_of_day": "night", "lighting": "bed"},
                    }
                },
            }
        )
    )
    try:
        app.update_config({"lumos_os.led_brightness": 77})
        key = slot_key(app.config.color.profiles)
        assert app.config.lumos_os.led_brightness == 77
        assert app.config.color.profiles.slots[key].led_brightness == 77
        assert seen[-1] == ("http://192.168.1.230", 77)

        app.set_color_profile({"lighting": "large"})
        assert app.config.lumos_os.led_brightness == 128
        app.set_color_profile({"lighting": "bed"})
        assert app.config.lumos_os.led_brightness == 77
        assert seen[-1] == ("http://192.168.1.230", 77)
    finally:
        app.shutdown()
