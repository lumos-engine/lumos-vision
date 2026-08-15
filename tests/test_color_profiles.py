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


def test_default_profiles_are_day_night_times_three_lights():
    cfg = Config()
    combos = all_combos(cfg.color.profiles)
    assert len(combos) == 6
    assert cfg.color.profiles.selection == {
        "time_of_day": "night",
        "lighting": "lights_off",
    }
    assert slot_key(cfg.color.profiles) == "time_of_day=night|lighting=lights_off"


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
    assert "time_of_day=day|lighting=large" in data["color"]["profiles"]["slots"]


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
