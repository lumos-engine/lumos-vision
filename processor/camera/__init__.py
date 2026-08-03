"""Frame sources.  Everything downstream only sees :class:`FrameSource`."""

from processor.camera.base import Frame, FrameSource
from processor.camera.factory import create_source
from processor.camera.file_source import FileSource, ImageSource
from processor.camera.rtsp import RtspSource
from processor.camera.synthetic import SyntheticSource
from processor.camera.v4l2 import V4l2Source

__all__ = [
    "Frame",
    "FrameSource",
    "FileSource",
    "ImageSource",
    "RtspSource",
    "SyntheticSource",
    "V4l2Source",
    "create_source",
]
