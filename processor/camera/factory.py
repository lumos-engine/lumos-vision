"""Build the configured frame source."""

from __future__ import annotations

from pathlib import Path

from processor.camera.base import FrameSource
from processor.camera.file_source import IMAGE_SUFFIXES, FileSource, ImageSource
from processor.camera.rtsp import RtspSource
from processor.camera.synthetic import SyntheticSource
from processor.config.schema import CameraConfig


def create_source(config: CameraConfig) -> FrameSource:
    source = (config.source or "rtsp").strip().lower()

    if source == "rtsp":
        return RtspSource(config)
    if source == "synthetic":
        return SyntheticSource(config)
    if source == "image":
        return ImageSource(config)
    if source == "file":
        # Pointing --source file at a PNG is an easy mistake; just do the
        # right thing instead of failing with a codec error.
        path = Path(config.path).expanduser()
        if path.is_dir() or path.suffix.lower() in IMAGE_SUFFIXES:
            return ImageSource(config)
        return FileSource(config)

    raise ValueError(
        f"unknown camera.source {config.source!r} (expected rtsp, file, image or synthetic)"
    )
