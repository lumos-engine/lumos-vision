"""Build the configured set of output sinks."""

from __future__ import annotations

import sys

from processor.config.schema import OutputConfig
from processor.output.base import NullSink, Sink
from processor.output.ddp import DdpSink
from processor.output.file_sink import FileSink
from processor.output.mjpeg import MjpegSink
from processor.output.v4l2 import V4L2Sink
from processor.utils.logging import get_logger

log = get_logger(__name__)


def create_sinks(config: OutputConfig) -> list[Sink]:
    sinks: list[Sink] = []

    if config.v4l2.enabled:
        if sys.platform == "linux":
            sinks.append(V4L2Sink(config.v4l2))
        else:
            log.warning(
                "output.v4l2 is enabled but this is not Linux; skipping it. "
                "Enable output.mjpeg to preview the stream instead."
            )

    if config.mjpeg.enabled:
        sinks.append(MjpegSink(config.mjpeg, fps=config.fps))

    if config.file.enabled:
        sinks.append(FileSink(config.file, fps=config.fps))

    if config.ddp.enabled:
        try:
            sinks.append(DdpSink(config.ddp))
        except ValueError as exc:
            log.warning("Skipping DDP output: %s", exc)

    if not sinks:
        log.warning("No output sinks are enabled; frames will be discarded")
        sinks.append(NullSink())

    return sinks
