"""Typed configuration schema.

Every knob lives here with a default, so a config file only needs to contain
what it wants to override.  The dataclasses double as documentation and are
what the web wizard edits (via ``dotted_set``) while the pipeline is running.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Union, get_args, get_origin, get_type_hints

# --------------------------------------------------------------------------
# Camera / input
# --------------------------------------------------------------------------


@dataclass
class CameraConfig:
    #: rtsp | v4l2 | usb | file | image | synthetic
    source: str = "rtsp"
    rtsp_url: str = ""
    #: USB webcam device for the ``v4l2`` / ``usb`` source (e.g. /dev/video2).
    device: str = ""
    #: Optional capture mode requests for V4L2 (0 = driver default).
    capture_width: int = 0
    capture_height: int = 0
    capture_fps: float = 0.0
    #: Hardware UVC controls applied via v4l2-ctl, e.g.
    #: ``{exposure_auto: 1, exposure_absolute: 300, brightness: 128}``.
    #: Empty means leave the driver defaults alone.
    controls: dict[str, int] = field(default_factory=dict)
    #: Video file or image path used by the ``file`` / ``image`` sources.
    path: str = ""
    #: Replay files in a loop (development convenience).
    loop: bool = True
    #: Replay speed for file sources; 0 means "as fast as the pipeline wants".
    replay_fps: float = 0.0
    #: tcp is far more reliable than udp over WiFi/Ethernet with H.264.
    transport: str = "tcp"
    #: Seconds without a decoded frame before the connection is torn down.
    read_timeout: float = 8.0
    reconnect_delay: float = 2.0
    max_reconnect_delay: float = 20.0
    #: Cap the width we feed the pipeline.  0 keeps the camera's native size
    #: (preferred -- let HyperHDR or a later sink downscale if it needs to).
    process_width: int = 0
    #: Extra FFmpeg options appended to OPENCV_FFMPEG_CAPTURE_OPTIONS.
    ffmpeg_options: str = ""


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


@dataclass
class BoundaryConfig:
    enabled: bool = True
    #: auto    -- detect continuously / on demand
    #: manual  -- always use ``corners``
    #: hybrid  -- use ``corners`` if present, otherwise detect
    mode: str = "hybrid"
    #: Calibrated corners in normalised (0..1) coordinates, TL, TR, BR, BL.
    corners: list[list[float]] | None = None
    #: Re-run detection when the movement stage reports the camera moved.
    auto_recalibrate: bool = True
    #: Also re-run detection every N seconds (0 disables the timer).
    recalibrate_interval: float = 0.0
    #: Detection runs on a downscaled copy; 480 px wide is plenty and cheap.
    detect_width: int = 480
    #: How many frames of TV "activity" to accumulate before the first fit.
    activity_frames: int = 40
    #: Use the fact that TV content moves and the wall does not.
    use_activity: bool = True
    #: Candidate filtering.
    min_area_frac: float = 0.03
    max_area_frac: float = 0.98
    target_aspect: float = 16.0 / 9.0
    aspect_tolerance: float = 0.55
    min_confidence: float = 0.30
    #: Corner smoothing (anti-jitter).
    smoothing_alpha: float = 0.25
    corner_deadband_px: float = 1.5
    corner_snap_px: float = 40.0
    #: Canny thresholds; 0 means "derive from the image median".
    canny_low: int = 0
    canny_high: int = 0


@dataclass
class MovementConfig:
    enabled: bool = True
    #: ncc  -- normalised cross correlation of the scene *outside* the TV.
    #: none -- never recalibrate automatically.
    #: (Phase correlation and ORB were tried and rejected; see the movement
    #: stage's docstring for why.)
    method: str = "ncc"
    #: Seconds between checks.  The camera is bolted to a shelf; twice a
    #: second is already generous.
    check_interval: float = 0.5
    #: `ncc`: percent decorrelation of the static scenery that counts as moved.
    #: Measured on the reference scene: TV content and lighting changes score
    #: below 0.1, a 34 px camera bump scores 8.8.
    ncc_threshold: float = 1.0
    #: How many consecutive checks must agree before we believe it.
    consecutive: int = 3
    #: Seconds to wait after a recalibration before checking again.
    settle_time: float = 2.0


@dataclass
class PerspectiveConfig:
    enabled: bool = True
    #: Rectified working resolution.  Match the output (or a little larger)
    #: so letterbox detection and colour sampling keep full detail.  Drop to
    #: 640x360 only on a very slow machine.
    width: int = 1280
    height: int = 720
    #: nearest | linear | cubic -- linear is the right latency/quality trade.
    interpolation: str = "linear"


@dataclass
class InsetConfig:
    top: float = -1.0
    bottom: float = -1.0
    left: float = -1.0
    right: float = -1.0


@dataclass
class CropConfig:
    enabled: bool = True
    #: Percent of the panel trimmed from every edge, to drop bezel bleed and
    #: the bright rim where wall reflections start.
    inset_percent: float = 2.0
    #: Per-edge overrides; negative means "use inset_percent".
    inset: InsetConfig = field(default_factory=InsetConfig)


@dataclass
class BlackBarsConfig:
    enabled: bool = True
    #: A row/column counts as "bar" when its bright tail is below this luma.
    #: Real USB cams rarely produce true 0-black letterbox; 40 catches the
    #: usual dark-gray bars without eating dim content.
    luma_threshold: int = 40
    #: Which percentile of the row is compared against the threshold.  Using
    #: a high percentile instead of the mean means a single bright subtitle
    #: pixel does not disqualify an otherwise black row -- but a real image
    #: does.
    percentile: float = 96.0
    detect_top_bottom: bool = True
    detect_left_right: bool = True
    #: Never crop away more than this fraction of a dimension per side.
    max_crop_percent: float = 40.0
    #: Force top == bottom and left == right (true for essentially all
    #: broadcast and film content, and it halves the flicker sources).
    symmetric: bool = True
    #: Temporal stabilisation, in frames / percent of the dimension.
    window: int = 15
    hold_frames: int = 8
    change_threshold_percent: float = 0.8
    max_step_percent: float = 1.0
    #: Ignore bar detection entirely while the whole frame is dark (fade to
    #: black, dark scene) -- otherwise the crop collapses to nothing.
    dark_frame_luma: float = 12.0


@dataclass
class ReflectionConfig:
    enabled: bool = True
    #: Second inset applied *after* black bars, in percent of the remaining
    #: image.  Kept separate from ``crop`` because it protects against a
    #: different thing: glare bleeding in from the room.
    margin_percent: float = 2.0
    #: Optional rectangular exclusions (normalised x, y, w, h) for a static
    #: channel logo or on-screen clock.  Excluded areas are replaced with the
    #: local average so they stop dragging the LED colour around.
    exclusions: list[list[float]] = field(default_factory=list)


@dataclass
class ExposureConfig:
    enabled: bool = False
    target_luma: float = 110.0
    min_gain: float = 0.6
    max_gain: float = 2.5
    smoothing: float = 0.08


@dataclass
class GainsConfig:
    r: float = 1.0
    g: float = 1.0
    b: float = 1.0


@dataclass
class ColorConfig:
    enabled: bool = True
    #: off | auto (grey-world) | manual (use ``gains``)
    white_balance: str = "off"
    #: 0..1 -- how far toward a fully neutral grey point to push.
    wb_strength: float = 0.6
    wb_smoothing: float = 0.05
    gains: GainsConfig = field(default_factory=GainsConfig)
    exposure: ExposureConfig = field(default_factory=ExposureConfig)
    gamma: float = 1.0
    brightness: float = 1.0
    contrast: float = 1.0
    saturation: float = 1.0


@dataclass
class ResizeConfig:
    enabled: bool = True
    #: stretch    -- fill the output, ignore aspect ratio
    #: letterbox  -- preserve aspect, pad with black
    #: crop       -- preserve aspect, trim overflow
    mode: str = "stretch"


# --------------------------------------------------------------------------
# Pipeline / output / UI
# --------------------------------------------------------------------------

DEFAULT_STAGE_ORDER = [
    "movement",
    "boundary",
    "perspective",
    "crop",
    "blackbars",
    "reflection",
    "color",
    "resize",
]


@dataclass
class PipelineConfig:
    #: Stage order.  Removing a name here removes the stage entirely; setting
    #: ``<stage>.enabled: false`` keeps it constructed but passive (so the
    #: debug UI can toggle it back on at runtime).
    stages: list[str] = field(default_factory=lambda: list(DEFAULT_STAGE_ORDER))
    #: Keep intermediate images for the debug UI / web preview.  Costs a few
    #: milliseconds of copying, so it is off unless something needs it.
    collect_debug: bool = False


@dataclass
class V4L2Config:
    enabled: bool = True
    device: str = "/dev/video10"
    #: YUYV | RGB24 | BGR24 -- YUYV is what most grabbers, HyperHDR included,
    #: are happiest with.
    pixel_format: str = "YUYV"


@dataclass
class MjpegConfig:
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 7661
    quality: int = 70


@dataclass
class FileSinkConfig:
    enabled: bool = False
    path: str = "recordings/output.mp4"
    fourcc: str = "mp4v"


@dataclass
class DdpConfig:
    """Direct DDP output to WLED -- the path that eventually replaces HyperHDR."""

    enabled: bool = False
    host: str = ""
    port: int = 4048
    #: LEDs per edge, clockwise from the top-left of the TV.
    leds_top: int = 0
    leds_right: int = 0
    leds_bottom: int = 0
    leds_left: int = 0
    #: Fraction of the image depth sampled inward from each edge.
    sample_depth: float = 0.08
    #: Start index and direction, to match however the strip is physically run.
    start_corner: str = "top-left"
    clockwise: bool = True
    smoothing: float = 0.35
    fps: float = 0.0  # 0 = follow the pipeline


@dataclass
class OutputConfig:
    #: Virtual-cam / MJPEG size.  1280x720 keeps cinema letterbox edges sharp
    #: for HyperHDR; downscale there if the host needs it.
    width: int = 1280
    height: int = 720
    fps: float = 20.0
    v4l2: V4L2Config = field(default_factory=V4L2Config)
    mjpeg: MjpegConfig = field(default_factory=MjpegConfig)
    file: FileSinkConfig = field(default_factory=FileSinkConfig)
    ddp: DdpConfig = field(default_factory=DdpConfig)


@dataclass
class DebugConfig:
    enabled: bool = False
    #: Which stage output to display; "grid" shows them all at once.
    view: str = "output"
    window_name: str = "Screen Sight"
    scale: float = 1.0
    overlay: bool = True
    snapshot_dir: str = "snapshots"


@dataclass
class WebConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 7660
    #: Preview stream rate; deliberately lower than the pipeline rate so the
    #: wizard does not steal CPU from the actual output.
    stream_fps: float = 10.0
    stream_quality: int = 65
    stream_max_width: int = 960


@dataclass
class LoggingConfig:
    level: str = "INFO"
    #: Seconds between throughput/latency log lines (0 disables).
    stats_interval: float = 30.0


@dataclass
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    boundary: BoundaryConfig = field(default_factory=BoundaryConfig)
    movement: MovementConfig = field(default_factory=MovementConfig)
    perspective: PerspectiveConfig = field(default_factory=PerspectiveConfig)
    crop: CropConfig = field(default_factory=CropConfig)
    blackbars: BlackBarsConfig = field(default_factory=BlackBarsConfig)
    reflection: ReflectionConfig = field(default_factory=ReflectionConfig)
    color: ColorConfig = field(default_factory=ColorConfig)
    resize: ResizeConfig = field(default_factory=ResizeConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
    web: WebConfig = field(default_factory=WebConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Config":
        return build_dataclass(cls, data or {})


# --------------------------------------------------------------------------
# dict -> dataclass conversion
# --------------------------------------------------------------------------


class ConfigError(ValueError):
    """Raised for structurally invalid configuration."""


def build_dataclass(cls, data: dict[str, Any], path: str = ""):
    """Recursively build ``cls`` from a plain dict, keeping defaults for gaps."""
    if not isinstance(data, dict):
        raise ConfigError(f"{path or 'config'}: expected a mapping, got {type(data).__name__}")

    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        where = path or "config"
        raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")

    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        child = f"{path}.{f.name}" if path else f.name
        kwargs[f.name] = _coerce(hints[f.name], data[f.name], child)
    return cls(**kwargs)


def _coerce(annotation, value, path: str):
    origin = get_origin(annotation)

    if origin is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if value is None:
            return None
        for candidate in args:
            try:
                return _coerce(candidate, value, path)
            except (ConfigError, TypeError, ValueError):
                continue
        raise ConfigError(f"{path}: cannot interpret {value!r}")

    if is_dataclass(annotation):
        return build_dataclass(annotation, value, path)

    if origin in (list, tuple):
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{path}: expected a list")
        args = get_args(annotation)
        if not args:
            return list(value)
        return [_coerce(args[0], v, f"{path}[{i}]") for i, v in enumerate(value)]

    if origin is dict or annotation is dict:
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: expected a mapping")
        key_type, val_type = str, int
        args = get_args(annotation) if origin is dict else ()
        if len(args) == 2:
            key_type, val_type = args
        return {
            _coerce(key_type, k, f"{path}.key"): _coerce(val_type, v, f"{path}.{k}")
            for k, v in value.items()
        }

    if annotation is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "yes", "on", "1"}:
                return True
            if lowered in {"false", "no", "off", "0"}:
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        raise ConfigError(f"{path}: expected a boolean, got {value!r}")

    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ConfigError(f"{path}: expected a number, got {value!r}")
        try:
            return float(value)
        except ValueError:
            raise ConfigError(f"{path}: expected a number, got {value!r}") from None

    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ConfigError(f"{path}: expected a number, got {value!r}")
        if isinstance(value, float) and not value.is_integer():
            raise ConfigError(f"{path}: expected a whole number, got {value!r}")
        try:
            return int(float(value)) if isinstance(value, str) else int(value)
        except ValueError:
            raise ConfigError(f"{path}: expected a whole number, got {value!r}") from None

    if annotation is str:
        if isinstance(value, bool):
            # YAML turns bare on/off/yes/no into booleans, which bites anyone
            # writing `white_balance: off`.  Say so instead of just refusing.
            word = "true" if value else "false"
            raise ConfigError(
                f"{path}: expected a string but YAML read this as the boolean "
                f"{word}; quote it, e.g. \"off\""
            )
        if not isinstance(value, str):
            raise ConfigError(f"{path}: expected a string, got {value!r}")
        return value

    return value
