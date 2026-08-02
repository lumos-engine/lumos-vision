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
    width, height, code, field, bytes_per_line, size_image = struct.unpack_from("<6I", blob, 8)

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
        V4L2Sink(type("C", (), {"device": "/dev/video10", "pixel_format": "JPEG2000"})())


@pytest.mark.skipif(sys.platform == "linux", reason="v4l2 is supported on Linux")
def test_v4l2_explains_itself_on_other_platforms():
    from processor.config.schema import V4L2Config

    sink = V4L2Sink(V4L2Config())
    with pytest.raises(RuntimeError, match="Linux-only"):
        sink.open(640, 360)


# ------------------------------------------------------------------- sinks


def test_sink_group_survives_a_broken_member():
    class Broken(NullSink):
        name = "broken"

        def write(self, image):
            raise OSError("device gone")

    good = NullSink()
    group = SinkGroup([Broken(), good])
    group.open(64, 36)
    group.write(np.zeros((36, 64, 3), np.uint8))
    assert good.frames == 1, "one broken sink stopped the others"


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
    assert LedSampler(LedLayout()).sample(np.zeros((10, 10, 3), np.uint8)).shape == (0, 3)


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
