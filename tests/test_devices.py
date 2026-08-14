"""Stable V4L2 device enumeration and path helpers."""

from pathlib import Path

from processor.camera.devices import (
    is_v4l2loopback,
    list_capture_devices,
    prefer_stable_device,
    real_video_node,
    resolve_device_path,
)
from processor.camera.v4l2 import open_device_candidates, resolve_device


def test_is_v4l2loopback_uses_driver_name(monkeypatch):
    monkeypatch.setattr(
        "processor.camera.devices._read_v4l2_info",
        lambda device: (
            {"driver": "v4l2loopback", "card": "Android Cam"}
            if "video11" in device
            else {"driver": "uvcvideo", "card": "USB Cam"}
        ),
    )
    assert is_v4l2loopback("/dev/video11") is True
    assert is_v4l2loopback("/dev/video2") is False

    monkeypatch.setattr(
        "processor.camera.devices._read_v4l2_info",
        lambda device: {"driver": "v4l2 loopback", "card": "Dummy video device"},
    )
    assert is_v4l2loopback("/dev/video11") is True


def test_resolve_device_path_accepts_index_and_paths():
    assert resolve_device_path("2") == "/dev/video2"
    assert resolve_device_path("/dev/video4") == "/dev/video4"
    assert resolve_device_path("/dev/v4l/by-id/usb-cam-video-index0").endswith(
        "usb-cam-video-index0"
    )


def test_resolve_device_passes_by_id_strings():
    path = "/dev/v4l/by-id/usb-046d_0809-video-index0"
    assert resolve_device(path) == path
    assert resolve_device("4") == 4


def test_open_device_candidates_include_resolved_node(tmp_path):
    node = tmp_path / "video4"
    node.touch()
    by_id = tmp_path / "by-id" / "usb-cam-video-index0"
    by_id.parent.mkdir()
    by_id.symlink_to(node)

    candidates = open_device_candidates(str(by_id))
    assert candidates[0] == str(by_id)
    assert str(node.resolve()) in {str(c) for c in candidates if not isinstance(c, int)}
    assert 4 in candidates


def test_list_capture_devices_prefers_by_id_and_filters(tmp_path, monkeypatch):
    video_root = tmp_path / "dev"
    video_root.mkdir()
    for name in ("video0", "video1", "video10"):
        (video_root / name).touch()

    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "usb-046d_0809-video-index0").symlink_to(video_root / "video0")
    (by_id / "usb-046d_0809-video-index1").symlink_to(video_root / "video1")
    (by_id / "platform-v4l2loopback-00-video-index0").symlink_to(video_root / "video10")

    monkeypatch.setattr(
        "processor.camera.devices._read_v4l2_info",
        lambda device: {
            "card": "Fake Cam",
            "bus_info": "usb-1",
            "driver": "uvcvideo",
        }
        if "loopback" not in str(device)
        else {"card": "Screen Sight", "driver": "v4l2loopback"},
    )
    monkeypatch.setattr(
        "processor.camera.devices._is_capture_capable",
        lambda device: "index1" not in Path(device).name,
    )

    devices = list_capture_devices(
        selected=str(by_id / "usb-046d_0809-video-index0"),
        by_id_dir=by_id,
        by_path_dir=tmp_path / "by-path-missing",
        include_bare_video=False,
    )

    assert len(devices) == 1
    entry = devices[0]
    assert entry["id_path"].endswith("usb-046d_0809-video-index0")
    assert entry["video_path"].endswith("video0")
    assert entry["name"] == "Fake Cam"
    assert entry["selected"] is True


def test_list_keeps_input_loopback_excludes_screen_sight(tmp_path, monkeypatch):
    video_root = tmp_path / "dev"
    video_root.mkdir()
    for name in ("video0", "video10", "video11"):
        (video_root / name).touch()

    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "usb-cam-video-index0").symlink_to(video_root / "video0")
    (by_id / "platform-v4l2loopback-0-video-index0").symlink_to(video_root / "video10")
    (by_id / "platform-v4l2loopback-1-video-index0").symlink_to(video_root / "video11")

    def fake_info(device: str) -> dict[str, str]:
        text = str(device)
        if text.endswith("video10") or "loopback-0" in text:
            return {"card": "Screen Sight", "driver": "v4l2loopback", "bus_info": "platform:0"}
        if text.endswith("video11") or "loopback-1" in text:
            return {"card": "Android Cam", "driver": "v4l2loopback", "bus_info": "platform:1"}
        return {"card": "USB Cam", "driver": "uvcvideo", "bus_info": "usb-1"}

    monkeypatch.setattr("processor.camera.devices._read_v4l2_info", fake_info)
    monkeypatch.setattr("processor.camera.devices._is_capture_capable", lambda device: True)

    devices = list_capture_devices(
        by_id_dir=by_id,
        by_path_dir=tmp_path / "missing",
        include_bare_video=False,
    )
    names = {entry["name"] for entry in devices}
    assert names == {"USB Cam", "Android Cam"}


def test_list_merges_bare_video_when_by_id_already_has_devices(tmp_path, monkeypatch):
    video_root = tmp_path / "dev"
    video_root.mkdir()
    (video_root / "video0").touch()
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "usb-cam-video-index0").symlink_to(video_root / "video0")

    monkeypatch.setattr(
        "processor.camera.devices._read_v4l2_info",
        lambda device: {"card": "USB Cam", "driver": "uvcvideo"},
    )
    monkeypatch.setattr("processor.camera.devices._is_capture_capable", lambda device: True)
    monkeypatch.setattr(
        "processor.camera.devices._collect_bare_video_nodes",
        lambda seen: [
            {
                "id_path": "/dev/video11",
                "video_path": "/dev/video11",
                "name": "Android Cam",
                "bus_info": "platform:v4l2loopback-001",
                "driver": "v4l2loopback",
                "stable_kind": "video",
            }
        ]
        if "/dev/video11" not in seen
        else [],
    )

    devices = list_capture_devices(
        by_id_dir=by_id,
        by_path_dir=tmp_path / "missing",
        include_bare_video=True,
    )
    assert [d["name"] for d in devices] == ["USB Cam", "Android Cam"]


def test_list_capture_devices_selected_matches_bare_video(tmp_path, monkeypatch):
    video_root = tmp_path / "dev"
    video_root.mkdir()
    (video_root / "video2").touch()
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    link = by_id / "usb-cam-video-index0"
    link.symlink_to(video_root / "video2")

    monkeypatch.setattr(
        "processor.camera.devices._read_v4l2_info",
        lambda device: {"card": "Cam", "driver": "uvcvideo"},
    )
    monkeypatch.setattr("processor.camera.devices._is_capture_capable", lambda device: True)

    devices = list_capture_devices(
        selected=str(video_root / "video2"),
        by_id_dir=by_id,
        by_path_dir=tmp_path / "missing",
        include_bare_video=False,
    )
    assert devices and devices[0]["selected"] is True


def test_prefer_stable_device_rewrites_bare_node(tmp_path, monkeypatch):
    video_root = tmp_path / "dev"
    video_root.mkdir()
    (video_root / "video3").touch()
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    link = by_id / "usb-stable-video-index0"
    link.symlink_to(video_root / "video3")

    monkeypatch.setattr(
        "processor.camera.devices._read_v4l2_info",
        lambda device: {"card": "Cam", "driver": "uvcvideo"},
    )
    monkeypatch.setattr("processor.camera.devices._is_capture_capable", lambda device: True)
    monkeypatch.setattr("processor.camera.devices.BY_ID_DIR", by_id)
    monkeypatch.setattr("processor.camera.devices.BY_PATH_DIR", tmp_path / "missing")

    assert prefer_stable_device(str(video_root / "video3")) == str(link)


def test_real_video_node_requires_videoN_name(tmp_path):
    other = tmp_path / "not-a-video"
    other.touch()
    assert real_video_node(other) is None
    node = tmp_path / "video7"
    node.touch()
    assert real_video_node(node).endswith("video7")
