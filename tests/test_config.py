import pytest
import yaml

from processor.config.loader import (
    apply_updates,
    config_to_dict,
    deep_merge,
    dotted_get,
    dotted_set,
    load_config,
    save_config,
)
from processor.config.schema import Config, ConfigError


def test_defaults_match_the_documented_targets():
    config = Config()
    assert (config.output.width, config.output.height) == (1280, 720)
    assert config.output.fps == 20.0
    assert config.camera.process_width == 0
    assert (config.perspective.width, config.perspective.height) == (1280, 720)
    assert config.output.v4l2.device == "/dev/video10"
    assert config.camera.transport == "tcp"
    assert config.blackbars.enabled is True


def test_partial_config_keeps_defaults():
    config = Config.from_dict({"output": {"fps": 24}})
    assert config.output.fps == 24
    assert config.output.width == 1280
    assert config.crop.inset_percent == 2.0


def test_unknown_keys_are_rejected():
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict({"output": {"widht": 640}})
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict({"nonsense": {}})


def test_type_errors_are_reported_with_a_path():
    with pytest.raises(ConfigError, match="color.gamma"):
        Config.from_dict({"color": {"gamma": {}}})


def test_ints_accept_whole_floats_only():
    assert Config.from_dict({"output": {"width": 800.0}}).output.width == 800
    with pytest.raises(ConfigError):
        Config.from_dict({"output": {"width": 800.5}})


def test_booleans_accept_yaml_style_strings():
    assert Config.from_dict({"blackbars": {"enabled": "no"}}).blackbars.enabled is False
    assert Config.from_dict({"blackbars": {"enabled": "on"}}).blackbars.enabled is True


def test_optional_corners_round_trip():
    corners = [[0.1, 0.2], [0.9, 0.2], [0.9, 0.8], [0.1, 0.8]]
    config = Config.from_dict({"boundary": {"corners": corners}})
    assert config.boundary.corners == corners
    assert Config.from_dict({"boundary": {"corners": None}}).boundary.corners is None


def test_dotted_helpers():
    data = {"a": {"b": 1}}
    assert dotted_get(data, "a.b") == 1
    assert dotted_get(data, "a.missing", "fallback") == "fallback"
    dotted_set(data, "a.c.d", 5)
    assert data["a"]["c"]["d"] == 5


def test_deep_merge_does_not_mutate_the_base():
    base = {"a": {"b": 1, "c": 2}}
    merged = deep_merge(base, {"a": {"c": 3}})
    assert merged == {"a": {"b": 1, "c": 3}}
    assert base["a"]["c"] == 2


def test_apply_updates_validates_before_returning():
    config = Config()
    updated = apply_updates(config, {"color.gamma": 1.8, "output.fps": 20})
    assert updated.color.gamma == 1.8
    assert updated.output.fps == 20
    assert config.color.gamma == 1.0  # the original is untouched

    with pytest.raises(ConfigError):
        apply_updates(config, {"output.width": "wide"})


def test_yaml_round_trip(tmp_path):
    original = Config.from_dict(
        {"color": {"gamma": 1.25, "saturation": 1.4}, "crop": {"inset_percent": 3.0}}
    )
    path = save_config(original, tmp_path / "nested" / "config.yaml")
    assert path.exists()

    reloaded = load_config(path)
    assert reloaded == original


def test_saved_yaml_is_readable_and_complete(tmp_path):
    path = save_config(Config(), tmp_path / "config.yaml")
    data = yaml.safe_load(path.read_text())
    assert set(data) == set(config_to_dict(Config()))
    assert data["output"]["v4l2"]["device"] == "/dev/video10"


def test_overrides_are_merged_over_the_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("output:\n  fps: 30\n  width: 800\n")
    config = load_config(path, {"output": {"fps": 12}})
    assert config.output.fps == 12
    assert config.output.width == 800


def test_loader_migrates_old_lumos_bind_yaml():
    config = Config.from_dict(
        {
            "camera": {"source": "v4l2", "device": "/dev/video11"},
            "lumos_cam": {
                "enabled": True,
                "bind_camera": True,
                "v4l2_sink": "/dev/video11",
                "prefer_over_scrcpy": True,
            },
        }
    )
    assert config.camera.source == "lumos"
    assert config.camera.device == ""
    assert config.lumos_cam.enabled is True
    dumped = config_to_dict(config)
    assert "bind_camera" not in dumped["lumos_cam"]
    assert "v4l2_sink" not in dumped["lumos_cam"]
    assert "prefer_over_scrcpy" not in dumped["lumos_cam"]


def test_loader_does_not_steal_a_real_usb_device():
    config = Config.from_dict(
        {
            "camera": {"source": "v4l2", "device": "/dev/video4"},
            "lumos_cam": {"enabled": True},
        }
    )
    assert config.camera.source == "v4l2"
    assert config.camera.device == "/dev/video4"
    assert config.lumos_cam.enabled is False


def test_camera_source_lumos_mirrors_enabled_flag():
    config = Config.from_dict({"camera": {"source": "lumos"}})
    assert config.lumos_cam.enabled is True
    usb = Config.from_dict({"camera": {"source": "v4l2", "device": "/dev/video2"}})
    assert usb.lumos_cam.enabled is False


def test_explicit_scrcpy_source_stays_scrcpy_when_lumos_is_off():
    config = Config.from_dict({"camera": {"source": "scrcpy"}})
    assert config.camera.source == "scrcpy"
    assert config.scrcpy.enabled is True
    assert config.lumos_cam.enabled is False


def test_loader_prefers_lumos_over_stale_scrcpy_source():
    config = Config.from_dict(
        {
            "camera": {"source": "scrcpy", "device": "/dev/video11"},
            "lumos_cam": {"enabled": True},
            "scrcpy": {"enabled": True, "auto_restart": True},
        }
    )
    assert config.camera.source == "lumos"
    assert config.camera.device == ""
    assert config.lumos_cam.enabled is True
    assert config.scrcpy.enabled is False
