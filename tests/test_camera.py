import cv2
import numpy as np
import pytest

from processor.app import Processor
from processor.camera.base import Frame
from processor.camera.factory import create_source
from processor.camera.rtsp import _ffmpeg_capture_options, redact_url
from processor.config.loader import apply_updates
from processor.config.schema import CameraConfig, Config


def test_credentials_are_stripped_from_urls():
    url = "rtsp://admin:s3cret@192.168.1.93:5543/live/channel10"
    redacted = redact_url(url)
    assert "s3cret" not in redacted
    assert "admin" not in redacted
    assert "192.168.1.93:5543/live/channel10" in redacted


def test_redaction_leaves_credential_free_urls_alone():
    url = "rtsp://192.168.1.93:5543/live/channel10"
    assert redact_url(url) == url
    assert redact_url("") == ""


def test_ffmpeg_options_ask_for_low_latency_tcp():
    options = _ffmpeg_capture_options(CameraConfig(transport="tcp", read_timeout=5))
    assert "rtsp_transport;tcp" in options
    assert "fflags;nobuffer" in options
    assert "flags;low_delay" in options
    assert "stimeout;5000000" in options


def test_extra_ffmpeg_options_are_appended():
    options = _ffmpeg_capture_options(CameraConfig(ffmpeg_options="buffer_size;65536"))
    assert options.endswith("buffer_size;65536")


def test_rtsp_source_requires_a_url():
    with pytest.raises(ValueError, match="rtsp_url"):
        create_source(CameraConfig(source="rtsp"))


def test_yuyv_to_bgr_converts_packed_frame():
    from processor.camera.v4l2 import yuyv_to_bgr

    width, height = 8, 4
    packed = np.zeros((height, width, 2), np.uint8)
    packed[:, :, 0] = 128
    packed[:, 0::2, 1] = 128
    packed[:, 1::2, 1] = 128
    bgr = yuyv_to_bgr(packed.tobytes(), width, height)
    assert bgr.shape == (height, width, 3)
    assert bgr.dtype == np.uint8


def test_v4l2_source_requires_a_device():
    with pytest.raises(ValueError, match="device"):
        create_source(CameraConfig(source="v4l2"))


def test_usb_alias_uses_v4l2_source():
    from processor.camera.v4l2 import V4l2Source

    source = create_source(CameraConfig(source="usb", device="/dev/video2"))
    assert isinstance(source, V4l2Source)


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError, match="unknown camera.source"):
        create_source(CameraConfig(source="carrier pigeon"))


def test_synthetic_source_produces_frames():
    source = create_source(CameraConfig(source="synthetic", replay_fps=200)).start()
    try:
        frame = source.read(timeout=2.0)
        assert isinstance(frame, Frame)
        assert frame.image.dtype == np.uint8
        assert frame.image.ndim == 3
        assert frame.age < 1.0
    finally:
        source.stop()


def test_file_source_replays_and_loops(tmp_path):
    path = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 20, (64, 36))
    for i in range(6):
        writer.write(np.full((36, 64, 3), i * 30, np.uint8))
    writer.release()

    config = CameraConfig(source="file", path=str(path), loop=True, replay_fps=200)
    source = create_source(config).start()
    try:
        frames = [source.read(timeout=2.0) for _ in range(14)]
    finally:
        source.stop()

    assert all(f is not None for f in frames), "looping stopped early"
    assert source.stats["loops"] >= 1


def test_file_source_stops_at_the_end_when_not_looping(tmp_path):
    path = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 20, (64, 36))
    for _ in range(4):
        writer.write(np.zeros((36, 64, 3), np.uint8))
    writer.release()

    config = CameraConfig(source="file", path=str(path), loop=False, replay_fps=200)
    source = create_source(config).start()
    try:
        results = [source.read(timeout=1.0) for _ in range(10)]
    finally:
        source.stop()
    assert results[-1] is None


def test_file_source_reports_a_missing_file():
    with pytest.raises(FileNotFoundError):
        create_source(CameraConfig(source="file", path="/nonexistent/clip.mp4"))


def test_image_source_cycles_a_directory(tmp_path):
    for i in range(3):
        cv2.imwrite(str(tmp_path / f"{i}.png"), np.full((36, 64, 3), i * 80, np.uint8))

    source = create_source(
        CameraConfig(source="image", path=str(tmp_path), replay_fps=200)
    ).start()
    try:
        values = {int(source.read(timeout=2.0).image[0, 0, 0]) for _ in range(6)}
    finally:
        source.stop()
    assert len(values) == 3


def test_a_still_image_is_accepted_by_the_file_source(tmp_path):
    path = tmp_path / "shot.png"
    cv2.imwrite(str(path), np.zeros((36, 64, 3), np.uint8))
    source = create_source(CameraConfig(source="file", path=str(path)))
    assert source.name == "image"


def test_process_width_downscales_the_input():
    config = CameraConfig(source="synthetic", replay_fps=200, process_width=320)
    source = create_source(config).start()
    try:
        # The synthetic source renders at its own size; the file/rtsp paths are
        # the ones that downscale, so check the helper they share.
        from processor.camera.file_source import _downscale

        assert _downscale(np.zeros((540, 960, 3), np.uint8), 320).shape[:2] == (180, 320)
        assert _downscale(np.zeros((180, 320, 3), np.uint8), 960).shape[:2] == (180, 320)
    finally:
        source.stop()


def test_recreate_source_swaps_synthetic_without_raising():
    config = Config.from_dict(
        {
            "camera": {"source": "synthetic", "replay_fps": 60},
            "output": {"width": 320, "height": 180, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
        }
    )
    app = Processor(config)
    app.start()
    old = app.source
    try:
        app.config = apply_updates(app.config, {"camera.replay_fps": 120})
        result = app.recreate_source()
        assert result["ok"]
        assert app.source is not None
        assert app.source is not old
        assert app.config.camera.replay_fps == 120
        frame = app.source.read(timeout=2.0)
        assert isinstance(frame, Frame)
    finally:
        app.shutdown()


def test_update_config_recreates_when_source_fields_change():
    config = Config.from_dict(
        {
            "camera": {"source": "synthetic", "replay_fps": 40},
            "output": {"width": 320, "height": 180, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
        }
    )
    app = Processor(config)
    app.start()
    old = app.source
    try:
        app.update_config({"camera.replay_fps": 80})
        assert app.source is not old
        assert app.config.camera.replay_fps == 80
    finally:
        app.shutdown()
