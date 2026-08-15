"""v4l2loopback helper is a no-op off Linux and never unloads the module."""

from processor.config.schema import Config
from processor.utils.loopback import (
    OUTPUT_LABEL,
    ensure_loopback,
    ensure_processor_loopbacks,
    video_nr,
)


def test_video_nr_parses_dev_nodes():
    assert video_nr("/dev/video10") == 10
    assert video_nr("/dev/video11") == 11
    assert video_nr("not-a-device") is None


def test_ensure_loopback_is_noop_off_linux(monkeypatch):
    monkeypatch.setattr("processor.utils.loopback.sys.platform", "darwin")
    assert ensure_loopback("/dev/video10", label=OUTPUT_LABEL) is False


def test_lumos_does_not_request_a_scrcpy_loopback(monkeypatch):
    probed: list[tuple[str, str]] = []
    monkeypatch.setattr("processor.utils.loopback.sys.platform", "linux")
    monkeypatch.setattr("processor.utils.loopback.os.path.exists", lambda path: False)
    monkeypatch.setattr(
        "processor.utils.loopback._sudo_modprobe",
        lambda devices: probed.extend(devices) or False,
    )
    monkeypatch.setattr("processor.utils.loopback.ensure_loopback", lambda *a, **k: False)
    config = Config.from_dict(
        {
            "camera": {"source": "lumos"},
            "output": {"v4l2": {"enabled": True, "device": "/dev/video10"}},
        }
    )
    ensure_processor_loopbacks(config)
    assert probed == [("/dev/video10", OUTPUT_LABEL)]


def test_scrcpy_requests_android_cam_loopback(monkeypatch):
    probed: list[tuple[str, str]] = []
    monkeypatch.setattr("processor.utils.loopback.sys.platform", "linux")
    monkeypatch.setattr("processor.utils.loopback.os.path.exists", lambda path: False)
    monkeypatch.setattr(
        "processor.utils.loopback._sudo_modprobe",
        lambda devices: probed.extend(devices) or False,
    )
    monkeypatch.setattr("processor.utils.loopback.ensure_loopback", lambda *a, **k: False)
    config = Config.from_dict(
        {
            "camera": {"source": "scrcpy"},
            "output": {"v4l2": {"enabled": False}},
        }
    )
    ensure_processor_loopbacks(config)
    assert probed == [("/dev/video11", "Android Cam")]
