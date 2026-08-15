"""v4l2loopback helper is a no-op off Linux and never unloads on a healthy start."""

from processor.config.schema import Config
from processor.utils.loopback import (
    OUTPUT_LABEL,
    ensure_loopback,
    ensure_processor_loopbacks,
    needed_loopbacks,
    repair_loopback,
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


def test_needed_loopbacks_includes_output_and_scrcpy():
    config = Config.from_dict(
        {
            "camera": {"source": "scrcpy"},
            "output": {"v4l2": {"enabled": True, "device": "/dev/video10"}},
            "scrcpy": {"v4l2_sink": "/dev/video11"},
        }
    )
    assert needed_loopbacks(config) == [
        ("/dev/video10", OUTPUT_LABEL),
        ("/dev/video11", "Android Cam"),
    ]


def test_ensure_does_not_reload_existing_nodes(monkeypatch):
    monkeypatch.setattr("processor.utils.loopback.sys.platform", "linux")
    monkeypatch.setattr("processor.utils.loopback.os.path.exists", lambda path: True)
    monkeypatch.setattr("processor.utils.loopback._path_ready", lambda path: True)
    reloaded: list[str] = []
    monkeypatch.setattr(
        "processor.utils.loopback._sudo_reload",
        lambda devices: reloaded.append("reload") or False,
    )
    monkeypatch.setattr(
        "processor.utils.loopback.repair_loopback",
        lambda *a, **k: reloaded.append("repair"),
    )
    config = Config.from_dict(
        {"output": {"v4l2": {"enabled": True, "device": "/dev/video10"}}}
    )
    ensure_processor_loopbacks(config)
    assert reloaded == []


def test_repair_loopback_deletes_then_adds(monkeypatch):
    monkeypatch.setattr("processor.utils.loopback.sys.platform", "linux")
    exists = {"/dev/video10": True}

    monkeypatch.setattr(
        "processor.utils.loopback.os.path.exists", lambda path: exists.get(path, False)
    )
    monkeypatch.setattr("processor.utils.loopback.is_v4l2loopback", lambda path: True)

    calls: list[tuple] = []

    def delete(path):
        calls.append(("delete", path))
        exists[path] = False
        return True

    def add(path, label):
        calls.append(("add", path, label))
        exists[path] = True
        return True

    monkeypatch.setattr("processor.utils.loopback._ctl_delete", delete)
    monkeypatch.setattr("processor.utils.loopback._ctl_add", add)
    monkeypatch.setattr("processor.utils.loopback._sudo_reload", lambda devices: False)

    assert repair_loopback("/dev/video10", label=OUTPUT_LABEL) is True
    assert calls[0] == ("delete", "/dev/video10")
    assert calls[1] == ("add", "/dev/video10", OUTPUT_LABEL)
