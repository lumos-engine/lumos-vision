"""V4L2 control parsing (no hardware required)."""

from processor.camera.controls import CameraControl, PREFERRED_CONTROLS
from processor.camera.controls import list_controls  # noqa: F401 — import smoke
from processor.config.loader import apply_updates, config_to_dict
from processor.config.schema import Config


def test_camera_controls_round_trip_in_config():
    config = Config.from_dict(
        {
            "camera": {
                "source": "v4l2",
                "device": "/dev/video2",
                "controls": {"brightness": 140, "exposure_auto": 1},
            }
        }
    )
    assert config.camera.controls["brightness"] == 140
    assert config.camera.controls["exposure_auto"] == 1

    updated = apply_updates(config, {"camera.controls.exposure_absolute": 250})
    assert updated.camera.controls["exposure_absolute"] == 250
    assert updated.camera.controls["brightness"] == 140

    data = config_to_dict(updated)
    assert data["camera"]["controls"]["exposure_absolute"] == 250


def test_preferred_control_names_cover_exposure():
    assert "exposure_auto" in PREFERRED_CONTROLS or "auto_exposure" in PREFERRED_CONTROLS
    assert any("exposure" in name for name in PREFERRED_CONTROLS)


def test_camera_control_to_dict_includes_menu():
    ctrl = CameraControl(
        name="exposure_auto",
        type="menu",
        min=0,
        max=3,
        value=1,
        menu={1: "Manual Mode", 3: "Aperture Priority Mode"},
    )
    data = ctrl.to_dict()
    assert data["menu"]["1"] == "Manual Mode"
    assert data["value"] == 1
