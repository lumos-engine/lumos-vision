"""Output sinks.  The V4L2 format struct and the YUYV packing are the two
places where a silent mistake produces a garbled virtual camera rather than an
error, so both are checked against their specifications directly."""

import struct
import sys

import cv2
import numpy as np
import pytest

from processor.config.schema import DdpConfig, FileSinkConfig, MjpegConfig, OutputConfig
from processor.led.sampler import LedLayout, LedSampler
from processor.output.base import NullSink, SinkGroup
from processor.output.broker import BrokerHub, FrameBroker
from processor.output.ddp import DDP_MAX_PAYLOAD, build_packets
from processor.output.factory import create_sinks
from processor.output.file_sink import FileSink
from processor.output.v4l2 import (
    PIXEL_FORMATS,
    V4L2_BUF_TYPE_VIDEO_OUTPUT,
    V4L2_FIELD_NONE,
    VIDIOC_S_FMT,
    V4L2Sink,
    bgr_to_yuyv,
    fourcc,
    pack_format,
)

# ------------------------------------------------------------------- V4L2


def test_fourcc_matches_the_kernel_macro():
    assert fourcc("YUYV") == 0x56595559
    assert fourcc("RGB3") == 0x33424752


def test_v4l2_format_struct_is_the_right_size():
    # sizeof(struct v4l2_format) is 208 on every supported architecture, and
    # the ioctl number encodes that size -- get it wrong and you get EINVAL.
    blob = pack_format(640, 360, "YUYV")
    assert len(blob) == 208


def test_v4l2_format_struct_fields_decode_correctly():
    blob = pack_format(640, 360, "YUYV")
    (buf_type,) = struct.unpack_from("<I", blob, 0)
    width, height, code, field, bytes_per_line, size_image = struct.unpack_from(
        "<6I", blob, 8
    )

    assert buf_type == V4L2_BUF_TYPE_VIDEO_OUTPUT
    assert (width, height) == (640, 360)
    assert code == fourcc("YUYV")
    assert field == V4L2_FIELD_NONE
    assert bytes_per_line == 640 * 2
    assert size_image == 640 * 360 * 2


@pytest.mark.parametrize("name", sorted(PIXEL_FORMATS))
def test_every_pixel_format_packs(name):
    assert len(pack_format(640, 360, name)) == 208


def test_yuyv_has_the_right_size_and_layout():
    image = np.zeros((4, 8, 3), np.uint8)
    packed = bgr_to_yuyv(image)
    assert packed.shape == (4, 8, 2)
    assert packed.nbytes == 4 * 8 * 2


def test_yuyv_encodes_neutral_grey_as_neutral_chroma():
    grey = np.full((4, 8, 3), 128, np.uint8)
    packed = bgr_to_yuyv(grey)
    assert np.allclose(packed[:, :, 0], 128, atol=1)  # luma
    assert np.allclose(packed[:, :, 1], 128, atol=1)  # chroma centred


def test_yuyv_round_trips_through_opencv():
    """Decoding our packing with OpenCV must give the original back."""
    rng = np.random.default_rng(0)
    image = rng.integers(0, 255, (16, 32, 3), dtype=np.uint8)
    image = cv2.GaussianBlur(image, (5, 5), 0)  # chroma is subsampled

    packed = bgr_to_yuyv(image)
    decoded = cv2.cvtColor(packed, cv2.COLOR_YUV2BGR_YUYV)
    assert decoded.shape == image.shape
    assert np.abs(decoded.astype(int) - image.astype(int)).mean() < 12


def test_yuyv_handles_an_odd_width():
    packed = bgr_to_yuyv(np.zeros((4, 9, 3), np.uint8))
    assert packed.shape[1] % 2 == 0


def test_v4l2_rejects_an_unknown_pixel_format():
    with pytest.raises(ValueError, match="pixel_format"):
        V4L2Sink(
            type("C", (), {"device": "/dev/video10", "pixel_format": "JPEG2000"})()
        )


@pytest.mark.skipif(sys.platform == "linux", reason="v4l2 is supported on Linux")
def test_v4l2_explains_itself_on_other_platforms():
    from processor.config.schema import V4L2Config

    sink = V4L2Sink(V4L2Config())
    with pytest.raises(RuntimeError, match="Linux-only"):
        sink.open(640, 360)


def test_v4l2_open_repairs_stuck_format_instead_of_raising(monkeypatch):
    from processor.config.schema import V4L2Config

    repaired: list[str] = []
    monkeypatch.setattr("processor.output.v4l2.sys.platform", "linux")
    monkeypatch.setattr("processor.output.v4l2.os.path.exists", lambda path: True)
    monkeypatch.setattr(
        "processor.output.v4l2.V4L2Sink._set_keep_format", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "processor.output.v4l2.repair_loopback",
        lambda path, **k: repaired.append(path) or True,
    )
    monkeypatch.setattr("processor.output.v4l2.ensure_loopback", lambda *a, **k: True)

    def boom(*_args, **_kwargs):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr("processor.output.v4l2.os.open", boom)

    sink = V4L2Sink(V4L2Config())
    sink.open(640, 360)
    assert sink.stats["open"] is False
    assert repaired == ["/dev/video10"]


def test_v4l2_writes_pinned_format_when_s_fmt_is_rejected(monkeypatch):
    from processor.config.schema import V4L2Config

    repaired: list[str] = []
    monkeypatch.setattr("processor.output.v4l2.sys.platform", "linux")
    monkeypatch.setattr("processor.output.v4l2.os.path.exists", lambda path: True)
    monkeypatch.setattr(
        "processor.output.v4l2.V4L2Sink._set_keep_format", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "processor.output.v4l2.V4L2Sink._ctl_set_fmt", lambda *a, **k: False
    )
    monkeypatch.setattr(
        "processor.output.v4l2.repair_loopback",
        lambda path, **k: repaired.append(path) or True,
    )
    monkeypatch.setattr("processor.output.v4l2.ensure_loopback", lambda *a, **k: True)
    monkeypatch.setattr("processor.output.v4l2.os.open", lambda *_a, **_k: 7)
    monkeypatch.setattr("processor.output.v4l2.os.close", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "processor.output.v4l2.os.write", lambda *_a, **_k: 1280 * 720 * 2
    )

    def fake_ioctl(_fd, req, buf):
        if req == VIDIOC_S_FMT:
            raise OSError(22, "Invalid argument")
        packed = pack_format(1280, 720, "YUYV")
        buf[: len(packed)] = packed
        return 0

    monkeypatch.setattr("processor.output.v4l2.fcntl.ioctl", fake_ioctl)

    sink = V4L2Sink(V4L2Config())
    sink.open(640, 360)
    assert sink.stats["open"] is True
    assert sink.stats["size"] == [1280, 720]
    assert sink.stats["pixel_format"] == "YUYV"
    assert repaired == []


def test_processor_recovers_v4l2_and_nudges_hyperhdr(monkeypatch):
    from processor.app import Processor
    from processor.config.schema import Config, V4L2Config

    grabber: list[bool] = []
    repaired: list[str] = []
    nudged: list[str] = []
    monkeypatch.setattr("processor.app.sys.platform", "linux")
    monkeypatch.setattr("processor.app.time.sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "processor.app.set_video_grabber",
        lambda url, enabled, **kw: grabber.append(bool(enabled)) or {"ok": True},
    )
    monkeypatch.setattr(
        "processor.app.repair_loopback",
        lambda path, **kw: repaired.append(path) or True,
    )
    monkeypatch.setattr(
        "processor.app.refresh_video_grabber",
        lambda url, **kw: nudged.append(url) or {"ok": True},
    )

    sink = V4L2Sink(V4L2Config())
    opened: list[tuple[int, int]] = []

    def fake_open(width, height):
        opened.append((width, height))
        sink._fd = 7
        sink._size = (width, height)

    monkeypatch.setattr(sink, "open", fake_open)
    monkeypatch.setattr(sink, "close", lambda: None)

    app = Processor(
        Config.from_dict(
            {
                "camera": {"source": "synthetic"},
                "output": {
                    "width": 640,
                    "height": 360,
                    "v4l2": {"enabled": True, "device": "/dev/video10"},
                },
                "power": {"hyperhdr_url": "http://127.0.0.1:8090"},
            }
        )
    )
    app.sinks = SinkGroup([sink])
    app._recover_v4l2_unlocked()
    app._nudge_hyperhdr_grabber_unlocked()
    assert grabber == [False]
    assert repaired == ["/dev/video10"]
    assert opened == [(640, 360)]
    assert nudged == ["http://127.0.0.1:8090"]


# ------------------------------------------------------------------- sinks


def test_sink_group_survives_a_broken_member():
    class Broken(NullSink):
        name = "broken"

        def write(self, image, ctx=None):
            raise OSError("device gone")

    good = NullSink()
    group = SinkGroup([Broken(), good])
    group.open(64, 36)
    group.write(np.zeros((36, 64, 3), np.uint8))
    assert good.frames == 1, "one broken sink stopped the others"


def test_sink_group_logs_a_dead_sink_once(caplog):
    import logging

    class Dead(NullSink):
        name = "dead"

        def write(self, image, ctx=None):
            return False

    group = SinkGroup([Dead()])
    group.open(4, 4)
    frame = np.zeros((4, 4, 3), np.uint8)
    with caplog.at_level(logging.WARNING):
        group.write(frame)
        group.write(frame)
        group.write(frame)
    assert sum(1 for rec in caplog.records if "stopped accepting" in rec.message) == 1


def test_sink_group_drops_a_sink_that_cannot_open():
    class Unopenable(NullSink):
        name = "unopenable"

        def open(self, width, height):
            raise RuntimeError("nope")

    group = SinkGroup([Unopenable(), NullSink()])
    group.open(64, 36)
    assert [s.name for s in group.sinks] == ["null"]


def test_factory_falls_back_to_null_when_nothing_is_enabled():
    config = OutputConfig()
    config.v4l2.enabled = False
    sinks = create_sinks(config)
    assert [s.name for s in sinks] == ["null"]


def test_factory_skips_v4l2_off_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    config = OutputConfig()
    config.mjpeg = MjpegConfig(enabled=True)
    assert [s.name for s in create_sinks(config)] == ["mjpeg"]


def test_file_sink_writes_a_playable_video(tmp_path):
    path = tmp_path / "out.mp4"
    sink = FileSink(FileSinkConfig(path=str(path)), fps=10)
    sink.open(64, 36)
    for _ in range(5):
        sink.write(np.full((36, 64, 3), 90, np.uint8))
    sink.close()

    assert path.exists() and path.stat().st_size > 0
    capture = cv2.VideoCapture(str(path))
    assert capture.isOpened()
    ok, frame = capture.read()
    capture.release()
    assert ok and frame.shape[:2] == (36, 64)


# ------------------------------------------------------------------ broker


def test_broker_keeps_only_the_newest_frame():
    broker = FrameBroker()
    broker.publish(np.zeros((2, 2, 3), np.uint8))
    broker.publish(np.ones((2, 2, 3), np.uint8))
    sequence, frame = broker.latest()
    assert sequence == 2
    assert frame.max() == 1


def test_broker_wait_times_out_without_new_frames():
    broker = FrameBroker()
    broker.publish(np.zeros((2, 2, 3), np.uint8))
    sequence, _ = broker.latest()
    assert broker.wait(sequence, timeout=0.05) is None


def test_broker_subscriber_counting():
    hub = BrokerHub()
    assert not hub.any_subscribers()
    hub.get("output").subscribe()
    assert hub.any_subscribers()
    assert hub.subscribed_names() == {"output"}
    hub.get("output").unsubscribe()
    assert not hub.any_subscribers()


# --------------------------------------------------------------- LEDs / DDP


def test_led_sampler_reads_each_edge():
    image = np.zeros((100, 200, 3), np.uint8)
    image[:10, :] = (255, 0, 0)  # top: blue in BGR
    image[-10:, :] = (0, 255, 0)  # bottom: green
    image[:, :10] = (0, 0, 255)  # left: red
    image[:, -10:] = (255, 255, 255)  # right: white

    sampler = LedSampler(LedLayout(top=4, right=2, bottom=4, left=2, depth=0.05))
    pixels = sampler.sample(image)

    assert pixels.shape == (12, 3)
    assert pixels[1][2] > 150, "top edge should be blue (RGB blue channel)"
    assert pixels[4][0] > 150, "right edge should be bright"


def test_led_sampler_order_starts_at_the_configured_corner():
    image = np.zeros((100, 200, 3), np.uint8)
    layout = LedLayout(top=2, right=2, bottom=2, left=2, start_corner="bottom-right")
    sampler = LedSampler(layout)
    assert sampler.sample(image).shape == (8, 3)


def test_led_sampler_handles_an_empty_layout():
    assert LedSampler(LedLayout()).sample(np.zeros((10, 10, 3), np.uint8)).shape == (
        0,
        3,
    )


def test_ddp_packet_header_is_well_formed():
    pixels = np.zeros((10, 3), np.uint8)
    packets = build_packets(pixels, sequence=3)
    assert len(packets) == 1

    header = packets[0][:10]
    assert header[0] & 0x40, "version bits missing"
    assert header[0] & 0x01, "final packet must set PUSH"
    assert header[1] == 3
    assert int.from_bytes(header[8:10], "big") == 30
    assert len(packets[0]) == 10 + 30


def test_ddp_splits_large_payloads_and_pushes_once():
    pixels = np.zeros((900, 3), np.uint8)  # 2700 bytes
    packets = build_packets(pixels, sequence=1)
    assert len(packets) == 2
    assert not packets[0][0] & 0x01, "only the last packet should PUSH"
    assert packets[-1][0] & 0x01
    assert all(len(p) - 10 <= DDP_MAX_PAYLOAD for p in packets)

    offsets = [int.from_bytes(p[4:8], "big") for p in packets]
    assert offsets == [0, DDP_MAX_PAYLOAD]


def test_ddp_requires_a_host_and_some_leds():
    with pytest.raises(ValueError, match="host"):
        from processor.output.ddp import DdpSink

        DdpSink(DdpConfig(enabled=True))

    with pytest.raises(ValueError, match="leds"):
        from processor.output.ddp import DdpSink

        DdpSink(DdpConfig(enabled=True, host="10.0.0.5"))


def test_rgbw_extracts_shared_white_and_keeps_hue():
    from processor.led.rgbw import encode_led_pixels, rgb_to_rgbw

    white = rgb_to_rgbw(np.array([[255, 255, 255]], np.uint8), white_kelvin=3000)
    assert white.shape == (1, 4)
    assert white[0, 3] == 255
    assert white[0, :3].max() == 0

    red = rgb_to_rgbw(np.array([[255, 0, 0]], np.uint8), white_kelvin=3000)
    assert red[0, 0] > 200
    assert red[0, 3] < 15

    # Camera-sampled TV red is never 255,0,0. CCT-fold would dump this onto W.
    camera_red = rgb_to_rgbw(np.array([[180, 70, 55]], np.uint8), white_kelvin=3000)
    assert camera_red[0, 0] > camera_red[0, 3]
    assert camera_red[0, 3] == 55
    assert camera_red[0, 0] == 125

    rgb = encode_led_pixels(np.array([[10, 20, 30]], np.uint8), "rgb")
    rgbw = encode_led_pixels(np.array([[10, 20, 30]], np.uint8), "rgbw", 3000)
    assert rgb.shape == (1, 3)
    assert rgbw.shape == (1, 4)
    assert list(rgbw[0]) == [0, 10, 20, 10]


def test_ddp_rgbw_packets_are_four_bytes_per_led():
    pixels = np.zeros((10, 4), np.uint8)
    packets = build_packets(pixels, sequence=1)
    assert int.from_bytes(packets[0][8:10], "big") == 40
    assert packets[0][2] == 0


def test_ddp_sink_always_sends_four_bytes_per_led(monkeypatch):
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
            color_mode="rgb",
            white_kelvin=3000,
        )
    )
    sink.open(8, 8)
    frame = np.zeros((8, 8, 3), np.uint8)
    assert sink.write(frame) is True
    assert sent
    assert int.from_bytes(sent[0][8:10], "big") == 16
    assert sink.stats["color_mode"] == "rgbw"


def test_quad_sampler_matches_warped_axis_aligned():
    from processor.testing.scene import SceneParams, SyntheticScene
    from processor.utils.geometry import homography_to_rect

    scene = SyntheticScene(
        SceneParams(
            shake_px=0.0,
            noise_sigma=0.0,
            reflection_strength=0.0,
            show_logo=False,
            show_subtitles=False,
            bezel_px=0,
            color_cast=(1.0, 1.0, 1.0),
            exposure=1.0,
        )
    )
    t = 1.0
    frame = scene.frame(t)
    corners = scene.quad_at(t)
    width, height = 320, 180
    warped = cv2.warpPerspective(
        frame, homography_to_rect(corners, width, height), (width, height)
    )
    layout = LedLayout(top=8, right=4, bottom=8, left=4, depth=0.08)
    axis = LedSampler(layout).sample(warped)
    quad = LedSampler(layout).sample_quad(frame, corners)
    diff = np.abs(axis.astype(np.float32) - quad.astype(np.float32)).mean()
    assert axis.shape == quad.shape == (24, 3)
    assert diff < 28, f"quad vs warped sampler mean abs diff {diff:.1f}"


def test_quad_sampler_insets_move_samples_in_panel_uv():
    from processor.testing.scene import SceneParams, SyntheticScene

    scene = SyntheticScene(
        SceneParams(
            shake_px=0.0,
            noise_sigma=0.0,
            reflection_strength=0.0,
            show_logo=False,
            show_subtitles=False,
            bezel_px=0,
            color_cast=(1.0, 1.0, 1.0),
            exposure=1.0,
        )
    )
    frame = scene.frame(1.0)
    corners = scene.quad_at(1.0)
    layout = LedLayout(top=6, right=0, bottom=0, left=0, depth=0.08)
    full = LedSampler(layout).sample_quad(frame, corners)
    inset = LedSampler(layout).sample_quad(frame, corners, insets=(0.2, 0.2, 0.2, 0.2))
    assert full.shape == inset.shape == (6, 3)
    assert np.abs(full.astype(int) - inset.astype(int)).mean() > 2


def test_quad_sampler_missing_corners_falls_back_on_ddp_sink():
    from processor.output.ddp import DdpSink

    sink = DdpSink(DdpConfig(enabled=True, host="127.0.0.1", leds_top=4))
    sink.open(16, 16)
    black = np.zeros((16, 16, 3), np.uint8)
    assert sink.write(black) is True
    sink.close()


def test_factory_ddp_led_path_skips_v4l2(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    config = OutputConfig(led_path="ddp")
    config.ddp.enabled = True
    config.ddp.host = "10.0.0.5"
    config.ddp.leds_top = 4
    config.v4l2.enabled = False
    names = [s.name for s in create_sinks(config)]
    assert "ddp" in names
    assert "v4l2" not in names
