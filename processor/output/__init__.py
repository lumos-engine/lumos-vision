"""Output sinks.  The processor produces frames; sinks decide who sees them."""

from processor.output.base import Sink, SinkGroup
from processor.output.broker import FrameBroker
from processor.output.factory import create_sinks
from processor.output.file_sink import FileSink
from processor.output.mjpeg import MjpegSink, MjpegServer
from processor.output.v4l2 import V4L2Sink
from processor.output.ddp import DdpSink

__all__ = [
    "DdpSink",
    "FileSink",
    "FrameBroker",
    "MjpegServer",
    "MjpegSink",
    "Sink",
    "SinkGroup",
    "V4L2Sink",
    "create_sinks",
]
