"""Command line entry point."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import Any

from processor import __version__
from processor.config.loader import config_to_dict, find_config, load_config, save_config
from processor.config.schema import Config, ConfigError
from processor.utils.logging import get_logger, setup_logging

log = get_logger(__name__)

EPILOG = """\
examples:
  # live camera to the virtual webcam HyperHDR will read
  screensight run --rtsp-url rtsp://user:pass@192.168.1.93:5543/live/channel10

  # same, with the calibration wizard on http://localhost:7660
  screensight run --rtsp-url rtsp://... --web

  # USB webcam (Logitech etc.) on the same machine
  screensight run --source v4l2 --camera-device /dev/video2 --web

  # develop with no camera at all
  screensight run --source synthetic --no-v4l2 --mjpeg --debug

  # replay a recording through the pipeline
  screensight run --source file --input samples/livingroom.mp4 --no-v4l2 --debug
"""


# ---------------------------------------------------------------- arguments


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="screensight",
        description="Turn an RTSP camera pointed at a TV into a rectified, "
        "cropped, low-latency virtual webcam.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="run the processor (default)", epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_run_arguments(run)

    record = subparsers.add_parser("record", help="record the raw camera stream to a file")
    record.add_argument("-c", "--config", help="YAML configuration file")
    record.add_argument("--rtsp-url", help="RTSP URL to record")
    record.add_argument("-o", "--output", default="recordings/capture.mp4", help="output file")
    record.add_argument("-d", "--duration", type=float, default=30.0, help="seconds to record")
    record.add_argument("--fps", type=float, default=15.0, help="recording frame rate")
    record.add_argument("--process-width", type=int, help="downscale width (0 keeps native)")
    record.add_argument("--log-level", default="INFO")

    samples = subparsers.add_parser("samples", help="generate synthetic sample media")
    samples.add_argument("-o", "--out", default="samples/generated", help="output directory")
    samples.add_argument("--seconds", type=float, default=20.0, help="video length")
    samples.add_argument("--fps", type=float, default=15.0)
    samples.add_argument("--log-level", default="INFO")

    config_cmd = subparsers.add_parser("config", help="inspect or write the configuration")
    config_cmd.add_argument("-c", "--config", help="YAML configuration file to read")
    config_cmd.add_argument("-w", "--write", help="write the fully expanded config here")
    config_cmd.add_argument("--log-level", default="WARNING")

    subparsers.add_parser("stages", help="list the available pipeline stages")

    return parser


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    source = parser.add_argument_group("input")
    source.add_argument("-c", "--config", help="YAML configuration file")
    source.add_argument(
        "--rtsp-url",
        help="RTSP URL of the camera, e.g. rtsp://user:pass@192.168.1.93:5543/live/channel10",
    )
    source.add_argument(
        "--source",
        choices=("rtsp", "v4l2", "usb", "file", "image", "synthetic"),
        help="input type",
    )
    source.add_argument(
        "--camera-device",
        help="USB / V4L2 capture device, e.g. /dev/video2 (implies --source v4l2)",
    )
    source.add_argument("--input", help="video file, image, or directory of images")
    source.add_argument("--transport", choices=("tcp", "udp"), help="RTSP transport")
    source.add_argument(
        "--process-width", type=int, help="downscale the camera feed to this width (0 = native)"
    )
    source.add_argument("--replay-fps", type=float, help="playback rate for file/synthetic input")
    source.add_argument("--capture-width", type=int, help="request this width from a USB camera")
    source.add_argument("--capture-height", type=int, help="request this height from a USB camera")

    output = parser.add_argument_group("output")
    output.add_argument("--width", type=int, help="output width (default 1280)")
    output.add_argument("--height", type=int, help="output height (default 720)")
    output.add_argument("--fps", type=float, help="target output frame rate (default 20)")
    output.add_argument("--device", help="V4L2 loopback *output* device (default /dev/video10)")
    output.add_argument("--pixel-format", choices=("YUYV", "RGB24", "BGR24"))
    output.add_argument("--no-v4l2", action="store_true", help="disable the virtual camera output")
    output.add_argument("--mjpeg", action="store_true", help="serve the output as MJPEG over HTTP")
    output.add_argument("--mjpeg-port", type=int, help="MJPEG port (default 7661)")
    output.add_argument("--record", metavar="PATH", help="also write the output to a video file")
    output.add_argument("--ddp-host", help="send LED colours straight to WLED at this address")

    ui = parser.add_argument_group("interface")
    ui.add_argument("--debug", action="store_true", help="open the debug window")
    ui.add_argument("--view", help="initial debug view (source, boundary, output, grid, ...)")
    ui.add_argument("--web", action="store_true", help="serve the calibration wizard")
    ui.add_argument("--web-port", type=int, help="wizard port (default 7660)")
    ui.add_argument("--web-host", help="wizard bind address (default 127.0.0.1)")

    tuning = parser.add_argument_group("pipeline")
    tuning.add_argument("--stages", help="comma separated stage list, in order")
    tuning.add_argument("--inset", type=float, metavar="PERCENT", help="crop inset per edge")
    tuning.add_argument("--no-blackbars", action="store_true", help="disable letterbox removal")
    tuning.add_argument("--no-color", action="store_true", help="disable colour normalisation")
    tuning.add_argument("--gamma", type=float)
    tuning.add_argument("--saturation", type=float)
    tuning.add_argument(
        "--boundary-mode", choices=("auto", "manual", "hybrid"), help="how the TV is located"
    )

    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--save-config", metavar="PATH", help="write the effective config and exit")


def overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Translate CLI flags into dotted config paths."""
    updates: dict[str, Any] = {}

    def put(path: str, value: Any) -> None:
        if value is not None:
            updates[path] = value

    if args.rtsp_url:
        updates["camera.rtsp_url"] = args.rtsp_url
        updates.setdefault("camera.source", "rtsp")
    put("camera.source", args.source)
    if getattr(args, "camera_device", None):
        updates["camera.device"] = args.camera_device
        updates.setdefault("camera.source", "v4l2")
    if args.input:
        updates["camera.path"] = args.input
        updates.setdefault("camera.source", "file")
    put("camera.transport", args.transport)
    put("camera.process_width", args.process_width)
    put("camera.replay_fps", args.replay_fps)
    put("camera.capture_width", getattr(args, "capture_width", None))
    put("camera.capture_height", getattr(args, "capture_height", None))

    put("output.width", args.width)
    put("output.height", args.height)
    put("output.fps", args.fps)
    put("output.v4l2.device", args.device)
    put("output.v4l2.pixel_format", args.pixel_format)
    if args.no_v4l2:
        updates["output.v4l2.enabled"] = False
    if args.mjpeg or args.mjpeg_port:
        updates["output.mjpeg.enabled"] = True
        put("output.mjpeg.port", args.mjpeg_port)
    if args.record:
        updates["output.file.enabled"] = True
        updates["output.file.path"] = args.record
    if args.ddp_host:
        updates["output.ddp.enabled"] = True
        updates["output.ddp.host"] = args.ddp_host

    if args.debug:
        updates["debug.enabled"] = True
    put("debug.view", args.view)
    if args.web or args.web_port or args.web_host:
        updates["web.enabled"] = True
        put("web.port", args.web_port)
        put("web.host", args.web_host)

    if args.stages:
        updates["pipeline.stages"] = [s.strip() for s in args.stages.split(",") if s.strip()]
    put("crop.inset_percent", args.inset)
    if args.no_blackbars:
        updates["blackbars.enabled"] = False
    if args.no_color:
        updates["color.enabled"] = False
    put("color.gamma", args.gamma)
    put("color.saturation", args.saturation)
    put("boundary.mode", args.boundary_mode)
    put("logging.level", args.log_level)

    return updates


def _require_camera_identity(config: Config) -> None:
    """Fail fast when the configured source has no URL/device.

    Lumos Cam with ``bind_camera`` owns capture via the ffmpeg pipe, so
    ``camera.device`` must stay empty (it used to point at the unused loopback).
    """
    if config.lumos_cam.enabled and config.lumos_cam.bind_camera:
        return
    if config.camera.source == "rtsp" and not config.camera.rtsp_url:
        raise SystemExit(
            "no camera configured. Pass --rtsp-url, set camera.rtsp_url in the config, "
            "or try --source synthetic / --source v4l2 --camera-device /dev/video2."
        )
    if config.camera.source in ("v4l2", "usb") and not config.camera.device:
        raise SystemExit(
            "USB camera selected but no device given. Pass --camera-device /dev/video2 "
            "(check with: v4l2-ctl --list-devices)."
        )


def build_config(args: argparse.Namespace) -> tuple[Config, Path | None]:
    """Load YAML (explicit, discovered, or defaults) and apply CLI overrides."""
    path = Path(args.config).expanduser() if getattr(args, "config", None) else find_config()
    if path is not None and not path.exists():
        raise SystemExit(f"configuration file not found: {path}")

    nested: dict[str, Any] = {}
    for dotted, value in overrides_from_args(args).items():
        node = nested
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    config = load_config(path, nested)
    return config, path


# ------------------------------------------------------------------ commands


def command_run(args: argparse.Namespace) -> int:
    try:
        config, config_path = build_config(args)
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}") from exc

    setup_logging(config.logging.level)

    if args.save_config:
        target = save_config(config, args.save_config)
        print(f"wrote {target}")
        return 0

    _require_camera_identity(config)

    # Imported here so `screensight --help` and the offline subcommands stay fast and
    # do not need a GUI-capable OpenCV build.
    from processor.app import Processor

    processor = Processor(config, config_path)
    processor.start()

    web_server = None
    if config.web.enabled:
        from processor.web.server import start_web_server

        web_server, _ = start_web_server(processor)

    _install_signal_handlers(processor)

    try:
        if config.debug.enabled:
            from processor.debug.viewer import DebugViewer

            # The GUI must own the main thread (mandatory on macOS), so the
            # pipeline moves to a worker.
            processor.run_in_background()
            DebugViewer(processor).run()
            processor.stop()
        else:
            processor.run()
    except KeyboardInterrupt:
        log.info("Interrupted")
    finally:
        if web_server is not None:
            web_server.shutdown()
            web_server.server_close()
        processor.shutdown()

    log.info("Stopped after %d frames", processor.status()["frames_out"])
    return 0


def _install_signal_handlers(processor) -> None:
    def handler(signum, _frame):
        log.info("Received %s, shutting down", signal.Signals(signum).name)
        processor.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # not on the main thread, or unsupported platform


def command_record(args: argparse.Namespace) -> int:
    setup_logging(args.log_level)
    import cv2

    from processor.camera.factory import create_source
    from processor.config.schema import CameraConfig

    path = Path(args.config).expanduser() if args.config else find_config()
    base = load_config(path) if path else Config()
    camera: CameraConfig = base.camera
    if args.rtsp_url:
        camera.rtsp_url = args.rtsp_url
        camera.source = "rtsp"
    if args.process_width is not None:
        camera.process_width = args.process_width
    if not camera.rtsp_url and camera.source == "rtsp":
        raise SystemExit("pass --rtsp-url or set camera.rtsp_url in the config")

    target = Path(args.output).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)

    source = create_source(camera).start()
    writer = None
    frames = 0
    interval = 1.0 / args.fps if args.fps > 0 else 0.0
    deadline = time.monotonic() + args.duration
    next_due = time.monotonic()

    log.info("Recording %.0fs from %s to %s", args.duration, camera.source, target)
    try:
        while time.monotonic() < deadline:
            frame = source.read(timeout=2.0)
            if frame is None:
                continue
            now = time.monotonic()
            if interval and now < next_due:
                continue
            next_due = max(now, next_due + interval)

            if writer is None:
                writer = cv2.VideoWriter(
                    str(target),
                    cv2.VideoWriter_fourcc(*"mp4v"),
                    args.fps,
                    (frame.width, frame.height),
                )
                if not writer.isOpened():
                    raise SystemExit(f"could not open {target} for writing")
            writer.write(frame.image)
            frames += 1
            if frames % 30 == 0:
                remaining = max(0.0, deadline - time.monotonic())
                print(f"\r{frames} frames, {remaining:4.1f}s left", end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        print()
        source.stop()
        if writer is not None:
            writer.release()

    if frames == 0:
        log.error("No frames were captured")
        return 1
    log.info("Wrote %d frames to %s", frames, target)
    return 0


def command_samples(args: argparse.Namespace) -> int:
    setup_logging(args.log_level)
    from processor.testing.generate import generate_samples

    written = generate_samples(Path(args.out), seconds=args.seconds, fps=args.fps)
    for path in written:
        print(path)
    return 0


def command_config(args: argparse.Namespace) -> int:
    setup_logging(args.log_level)
    import yaml

    path = Path(args.config).expanduser() if args.config else find_config()
    config = load_config(path) if path else Config()

    if args.write:
        target = save_config(config, args.write)
        print(f"wrote {target}")
        return 0

    print(yaml.safe_dump(config_to_dict(config), sort_keys=False, default_flow_style=False, indent=2))
    return 0


def command_stages(_args: argparse.Namespace) -> int:
    from processor.pipeline.registry import describe_stages

    for spec in describe_stages():
        print(f"{spec['name']:<12} {spec['description']}")
    return 0


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `screensight --rtsp-url ...` should work without typing `run`.
    commands = {"run", "record", "samples", "config", "stages"}
    if not argv or (argv[0] not in commands and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "run")

    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "run": command_run,
        "record": command_record,
        "samples": command_samples,
        "config": command_config,
        "stages": command_stages,
    }
    handler = handlers.get(args.command or "run")
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
