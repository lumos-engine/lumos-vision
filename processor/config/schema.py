"""Typed configuration schema.

Every knob lives here with a default, so a config file only needs to contain
what it wants to override.  The dataclasses double as documentation and are
what the web wizard edits (via ``dotted_set``) while the pipeline is running.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Union, get_args, get_origin, get_type_hints

#: Leftover Lumos keys from the ffmpeg→v4l2loopback path. Dropped on load.
_DROPPED_LUMOS_KEYS = ("v4l2_sink", "bind_camera", "prefer_over_scrcpy")
_LEGACY_LOOPBACK_NODES = {"video10", "video11"}

# --------------------------------------------------------------------------
# Camera / input
# --------------------------------------------------------------------------


@dataclass
class CameraConfig:
    #: lumos | scrcpy | v4l2 | usb | rtsp | file | image | synthetic
    source: str = "rtsp"
    rtsp_url: str = ""
    #: USB webcam for ``v4l2`` / ``usb``. Prefer ``/dev/v4l/by-id/…`` (stable);
    #: bare ``/dev/videoN`` works but can renumber after replug.
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
    #: Absolute luma floor used alongside content-relative detection.
    #: Vimicro letterbox is blue-black, not 0: ~#051138 in low light (luma
    #: ~18) and ~#131437 / #201C58 in room light (luma ~24-36).  Keep this
    #: above the good-light bar luma with noise headroom.
    luma_threshold: int = 55
    #: Which percentile of the row is compared against the threshold.  Using
    #: a high percentile instead of the mean means a single bright subtitle
    #: pixel does not disqualify an otherwise black row -- but a real image
    #: does.
    percentile: float = 96.0
    detect_top_bottom: bool = True
    detect_left_right: bool = True
    #: Which axis to look at. ``auto`` measures both and keeps letterbox *or*
    #: pillarbox (never a windowbox). Pin ``top_bottom`` / ``left_right`` when
    #: Dolby Vision pumping or subtitles sitting on the bars fool auto.
    direction: str = "auto"
    #: Optional content aspect, e.g. ``"2.39"`` or ``"21:9"``. Empty = measure
    #: from the picture. Quote in YAML so ``21:9`` stays a string.
    target_aspect: str | float = ""
    #: Never crop away more than this fraction of height per side (letterbox).
    #: 16% covers 2.76:1 on a 16:9 panel; common 2.39:1 needs ~13%.
    max_crop_top_bottom_percent: float = 16.0
    #: Never crop away more than this fraction of width per side (pillarbox).
    #: 12.5% is exactly 4:3 on 16:9.
    max_crop_left_right_percent: float = 12.5
    #: Deprecated alias: if set in older configs, prefer the axis-specific
    #: fields above.  Kept so unknown-key validation does not reject YAML.
    max_crop_percent: float = 16.0
    #: Force top == bottom and left == right (true for essentially all
    #: broadcast and film content, and it halves the flicker sources).
    symmetric: bool = True
    #: Temporal stabilisation, in frames / percent of the dimension.
    #: Noisy USB cams need a wider median and longer hold or cinema bars
    #: strobe on/off in the blackbars debug view.
    window: int = 21
    hold_frames: int = 14
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
class BlackLevelConfig:
    """Per-channel black pedestal in display RGB order (0–255 scale)."""

    r: float = 0.0
    g: float = 0.0
    b: float = 0.0


@dataclass
class ColorCalibrationInfo:
    """Provenance from the solid-patch colour calibration wizard."""

    #: UTC ISO timestamp of the last successful apply, or empty.
    calibrated_at: str = ""
    #: Measured mean BGR per patch name, e.g. ``{"white": [b, g, r]}``.
    patch_means_bgr: dict[str, list[float]] = field(default_factory=dict)
    #: Fitted 3×3 BGR matrix (row-major, 9 floats) from the last apply.
    matrix: list[float] = field(
        default_factory=lambda: [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    )
    #: Black pedestal from the last apply (RGB channel fields).
    black_level: BlackLevelConfig = field(default_factory=BlackLevelConfig)
    notes: list[str] = field(default_factory=list)


@dataclass
class ProfileCameraState:
    """Phone 3A freeze for one environment combo.

    ``ae`` / ``af`` / ``awb`` are ``auto`` or ``locked``. Numeric fields are
    the Camera2 values to restore on profile switch (0 / -1 / empty = unknown).
    While locked, the phone must not hunt; unlock is manual only.
    """

    af: str = "auto"
    ae: str = "auto"
    awb: str = "auto"
    iso: int = 0
    exposure_ns: int = 0
    focus_distance: float = -1.0
    awb_gains: list[float] = field(default_factory=list)


@dataclass
class ProfileOption:
    id: str = ""
    label: str = ""


@dataclass
class ProfileDimension:
    """One axis of an environment profile (e.g. time of day, room lights)."""

    id: str = ""
    label: str = ""
    options: list[ProfileOption] = field(default_factory=list)


def _default_color_profile_dimensions() -> list[ProfileDimension]:
    from processor.utils.color_profiles import default_profile_dimensions

    return default_profile_dimensions()


def _default_color_profile_selection() -> dict[str, str]:
    from processor.utils.color_profiles import default_profile_selection

    return default_profile_selection()


@dataclass
class ColorProfileSlot:
    """Stored correction for one environment combo.

    Empty ``calibrated_at`` means this combo has not been measured yet; the
    pipeline then runs in no-calibration (passthrough) mode.
    """

    calibrated_at: str = ""
    white_balance: str = "off"
    gains: GainsConfig = field(default_factory=GainsConfig)
    matrix_enabled: bool = False
    matrix: list[float] = field(
        default_factory=lambda: [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    )
    black_level_enabled: bool = False
    black_level: BlackLevelConfig = field(default_factory=BlackLevelConfig)
    gamma: float = 1.0
    saturation: float = 1.0
    notes: list[str] = field(default_factory=list)
    patch_means_bgr: dict[str, list[float]] = field(default_factory=dict)
    camera: ProfileCameraState = field(default_factory=ProfileCameraState)
    #: Lumos OS LED strip brightness (0–255). Independent of colour cal.
    led_brightness: int = 128


@dataclass
class ColorProfilesConfig:
    """Cartesian environment profiles for colour calibration.

    Add a dimension (or an option on an existing one) without changing the
    live colour stage — see ``.agents/skills/color-profiles/SKILL.md``.
    """

    dimensions: list[ProfileDimension] = field(
        default_factory=_default_color_profile_dimensions
    )
    selection: dict[str, str] = field(default_factory=_default_color_profile_selection)
    #: Keyed ``dim=option|dim=option`` in ``dimensions`` order.
    slots: dict[str, ColorProfileSlot] = field(default_factory=dict)


@dataclass
class ColorConfig:
    enabled: bool = True
    #: off | auto (grey-world) | manual (use ``gains``)
    white_balance: str = "off"
    #: 0..1 -- how far toward a fully neutral grey point to push.
    wb_strength: float = 0.6
    wb_smoothing: float = 0.05
    gains: GainsConfig = field(default_factory=GainsConfig)
    #: When true, apply ``matrix`` (3×3 BGR) before the per-channel LUT.
    matrix_enabled: bool = False
    #: Row-major 3×3 BGR: ``corrected = measured @ matrix``. Identity = no-op.
    matrix: list[float] = field(
        default_factory=lambda: [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    )
    #: Subtract camera/TV black floor before matrix/LUT (backlight glow).
    black_level_enabled: bool = False
    black_level: BlackLevelConfig = field(default_factory=BlackLevelConfig)
    exposure: ExposureConfig = field(default_factory=ExposureConfig)
    gamma: float = 1.0
    brightness: float = 1.0
    contrast: float = 1.0
    saturation: float = 1.0
    calibration: ColorCalibrationInfo = field(default_factory=ColorCalibrationInfo)
    profiles: ColorProfilesConfig = field(default_factory=ColorProfilesConfig)


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
    #: HyperHDR has no reliable UI flip for v4l2loopback; use these when its
    #: preview is mirrored/upside-down while other viewers look correct.
    flip_horizontal: bool = False
    flip_vertical: bool = False


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
    #: ``rgb`` = 3 bytes/LED (WS2812/WS2815). ``rgbw_off`` = RGBW strip, W=0.
    #: ``rgbw`` = drive the white diode (set ``white_kelvin`` to the phosphor).
    color_mode: str = "rgbw"
    #: SK6812 W-phosphor CCT. Strip spec; not used to fold hue onto W.
    white_kelvin: int = 3000
    #: 0..1: how much of the gray component uses the warm W diode vs cool RGB
    #: fill. 0 = D65-ish mixed RGB whites; 1 = maximum 3000 K W.
    white_gain: float = 0.35
    #: Wire order of the first three diodes: ``rgb`` (RGBW) or ``grb`` (GRBW).
    rgb_order: str = "rgb"
    #: Row-major 3×3 on logical RGB: ``driven = intended @ matrix``. Identity = no-op.
    color_matrix: list[float] = field(
        default_factory=lambda: [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    )
    calibrated_at: str = ""


#: Aliases accepted in YAML / the wizard for the opt-in WLED path.
_LED_PATH_DDP = frozenset({"ddp", "direct", "wled"})


def normalize_led_path(value: Any) -> str:
    """``hyperhdr`` (default, virtual cam) or ``ddp`` (direct to WLED)."""
    text = str(value or "hyperhdr").strip().lower()
    return "ddp" if text in _LED_PATH_DDP else "hyperhdr"


def sync_led_path_sinks(output: OutputConfig, *, restore_v4l2: bool = False) -> None:
    """Keep V4L2 and DDP mutually exclusive for the live LED path.

    ``restore_v4l2`` is True when the user just switched *to* HyperHDR (wizard
    or live update) so the virtual camera comes back without a YAML edit.
    Loading a file with ``led_path: hyperhdr`` leaves ``v4l2.enabled`` alone
    so ``--no-v4l2`` still works.
    """
    path = normalize_led_path(output.led_path)
    output.led_path = path
    if path == "ddp":
        output.ddp.enabled = True
        output.v4l2.enabled = False
        # ``rgb`` is 3-byte; ``rgbw`` / ``rgbw_off`` are 4-byte.
        from processor.led.rgbw import normalize_color_mode

        output.ddp.color_mode = normalize_color_mode(output.ddp.color_mode)
        if int(output.ddp.white_kelvin or 0) <= 0:
            output.ddp.white_kelvin = 3000
        from processor.led.rgbw import normalize_rgb_order

        output.ddp.rgb_order = normalize_rgb_order(output.ddp.rgb_order)
    else:
        output.ddp.enabled = False
        if restore_v4l2:
            output.v4l2.enabled = True


@dataclass
class OutputConfig:
    #: Virtual-cam / MJPEG size.  1280x720 keeps cinema letterbox edges sharp
    #: for HyperHDR; downscale there if the host needs it.
    width: int = 1280
    height: int = 720
    fps: float = 20.0
    #: ``hyperhdr`` (default): warp → V4L2 → HyperHDR. ``ddp``: sample the
    #: camera quad and send DDP UDP to WLED. Mutually exclusive live paths.
    led_path: str = "hyperhdr"
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
class PowerConfig:
    """Idle the camera/pipeline when the TV is offline (optional).

    Empty ``tv_host`` disables the feature entirely.  When enabled, an offline
    TV releases the capture device, feeds black frames to the virtual webcam
    so HyperHDR's grabber stays alive, and disables HyperHDR's LEDDEVICE for
    true LED power-off.
    """

    #: TV IPv4/hostname to ping, e.g. ``192.168.1.244``.  Empty = never idle.
    tv_host: str = ""
    #: HyperHDR JSON API base, e.g. ``http://127.0.0.1:8090``.  Empty skips
    #: LED hard-off (camera idle only).
    hyperhdr_url: str = "http://127.0.0.1:8090"
    #: Seconds between presence probes.
    check_interval_sec: float = 15.0
    #: Consecutive failed pings before entering idle (TV treated as off).
    #: Default 5 × 15s ≈ 75s before idle.
    failed_pings: int = 5
    #: Consecutive successful pings before leaving idle (TV treated as on).
    success_pings: int = 1
    ping_timeout_sec: float = 1.0
    #: Black-frame rate written to sinks while idle (keeps /dev/videoN alive).
    idle_fps: float = 2.0


@dataclass
class LumosOsConfig:
    """Lumos OS LED box (HyperHDR plugin host).

    Distinct from ``power.hyperhdr_url`` (HyperHDR JSON-RPC on the PC).
    Empty ``url`` skips brightness API calls. LED brightness lives on the
    active colour-profile slot and is copied here as a live view.
    """

    #: Base URL or host, e.g. ``http://192.168.1.230``. Empty = skip.
    url: str = ""
    #: Live LED brightness 0–255 (view of the active profile slot).
    led_brightness: int = 128


@dataclass
class ScrcpyConfig:
    """Optional Android phone camera via scrcpy → v4l2loopback.

    When ``enabled``, Screen Sight starts/stops scrcpy itself.  Absolute phone
    zoom is applied with ``--camera-zoom`` (scrcpy has no external live zoom
    API), so zoom/size changes briefly restart the child while the pipeline
    reconnects.
    """

    enabled: bool = False
    #: ``scrcpy`` on PATH, or an absolute path (e.g. ``/opt/scrcpy/scrcpy``).
    binary: str = "scrcpy"
    #: Optional ``adb`` serial (``scrcpy -s …``).
    serial: str = ""
    camera_id: str = "0"
    camera_size: str = "1920x1080"
    camera_fps: int = 30
    #: Phone Camera2 zoom ratio (optical/hybrid until the device goes digital).
    camera_zoom: float = 1.0
    zoom_min: float = 1.0
    zoom_max: float = 10.0
    #: Extra digital framing zoom via scrcpy ``--crop`` (needed to pan).
    #: 1.0 = full frame (no pan room).  ~1.2–2.0 leaves room to shift.
    view_zoom: float = 1.0
    #: Horizontal pan in the crop window: -1 = leftmost, +1 = rightmost.
    pan_x: float = 0.0
    #: Vertical pan: -1 = top, +1 = bottom.
    pan_y: float = 0.0
    #: Loopback node scrcpy writes. Implied when ``camera.source`` is ``scrcpy``
    #: (``camera.device`` is set to this path; Screen Sight creates the node).
    v4l2_sink: str = "/dev/video11"
    no_playback: bool = True
    no_audio: bool = True
    #: How long to wait for the sink to advertise capture after spawn.
    startup_timeout_sec: float = 12.0
    #: If scrcpy dies (phone unplug), keep trying to restart when ADB returns.
    auto_restart: bool = True
    #: Minimum seconds between automatic restart attempts.
    restart_interval_sec: float = 5.0
    #: Extra CLI tokens appended verbatim.
    extra_args: list[str] = field(default_factory=list)


@dataclass
class LumosCamConfig:
    """Lumos Cam (Camera2 Android app) → ffmpeg pipe.

    Capture is selected with ``camera.source: lumos``. ``enabled`` is a mirror
    of that (kept so sidecar start/stop stays a boolean). Zoom, pan, and
    AF/AE/AWB locks are live HTTP; ffmpeg only restarts when codec/size/ports
    change. Needs Lumos Cam ≥ 0.1.0 (``Lumos-Cam-Protocol: 1``).
    """

    enabled: bool = False
    serial: str = ""
    adb: str = "adb"
    package: str = "dev.lumos.cam"
    activity: str = "dev.lumos.cam.MainActivity"
    camera_id: str = "0"
    #: 1080p Camera2 JPEGs on typical Xiaomi phones cannot hold 30 fps, so
    #: the TCP/adb queue fills and playback goes slow-motion. 1280x720 stays live.
    camera_size: str = "1280x720"
    camera_fps: int = 30
    codec: str = "mjpeg"
    camera_zoom: float = 1.0
    zoom_min: float = 1.0
    zoom_max: float = 10.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    af: str = "auto"
    ae: str = "auto"
    awb: str = "auto"
    #: 0 = not set (phone AE auto unless ``ae`` is locked without numbers).
    iso: int = 0
    exposure_ns: int = 0
    #: <0 = not set.
    focus_distance: float = -1.0
    #: Camera2 RGGB gains; empty = not set.
    awb_gains: list[float] = field(default_factory=list)
    control_device_port: int = 8765
    video_device_port: int = 8766
    control_host_port: int = 18765
    video_host_port: int = 18766
    ffmpeg: str = "ffmpeg"
    startup_timeout_sec: float = 15.0
    auto_restart: bool = True
    restart_interval_sec: float = 5.0
    #: Restart if frames stop while ffmpeg/TCP still looks alive (Doze, stall).
    #: 0 falls back to ``camera.read_timeout``.
    stall_timeout_sec: float = 8.0


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
    power: PowerConfig = field(default_factory=PowerConfig)
    lumos_os: LumosOsConfig = field(default_factory=LumosOsConfig)
    scrcpy: ScrcpyConfig = field(default_factory=ScrcpyConfig)
    lumos_cam: LumosCamConfig = field(default_factory=LumosCamConfig)

    @classmethod
    def from_dict(
        cls, data: dict[str, Any] | None, *, bind_profiles: bool = True
    ) -> "Config":
        raw = copy.deepcopy(data or {})
        normalize_capture_dict(raw)
        cfg = build_dataclass(cls, raw)
        sync_led_path_sinks(cfg.output, restore_v4l2=False)
        if bind_profiles:
            from processor.utils.color_profiles import bind_config

            return bind_config(cfg)
        return cfg


def _legacy_loopback_device(device: str) -> bool:
    name = (device or "").rstrip("/").rsplit("/", 1)[-1]
    return name in _LEGACY_LOOPBACK_NODES or "loopback" in (device or "").lower()


def _source_has_no_capture_identity(camera: dict[str, Any], source: str) -> bool:
    """True when YAML looks like leftover USB/RTSP with nothing actually set."""
    device = str(camera.get("device") or "").strip()
    if source in {"v4l2", "usb"}:
        return not device or _legacy_loopback_device(device)
    if source in {"rtsp", ""}:
        return not str(camera.get("rtsp_url") or "").strip() and (
            not device or _legacy_loopback_device(device)
        )
    return False


def normalize_capture_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate old Lumos/scrcpy YAML and keep sidecar ``enabled`` in sync.

    Mutates ``data`` in place. ``camera.source`` is the only capture switch;
    ``lumos_cam.enabled`` / ``scrcpy.enabled`` mirror it.
    """
    camera = data.get("camera")
    if not isinstance(camera, dict):
        camera = {}
        data["camera"] = camera

    lumos = data.get("lumos_cam")
    if isinstance(lumos, dict):
        for key in _DROPPED_LUMOS_KEYS:
            lumos.pop(key, None)
    else:
        lumos = None

    scrcpy = data.get("scrcpy")
    if isinstance(scrcpy, dict):
        scrcpy.pop("bind_camera", None)
    else:
        scrcpy = None

    source = str(camera.get("source") or "rtsp").strip().lower()
    if source == "usb":
        source = "v4l2"
    device = str(camera.get("device") or "").strip()
    lumos_on = bool(lumos and lumos.get("enabled"))
    scrcpy_on = bool(scrcpy and scrcpy.get("enabled"))

    if source == "lumos":
        pass
    elif source == "scrcpy":
        pass
    elif lumos_on and _source_has_no_capture_identity(camera, source):
        source = "lumos"
        if _legacy_loopback_device(device):
            device = ""
    elif (
        scrcpy_on
        and source not in {"file", "image", "synthetic", "lumos"}
        and (
            _source_has_no_capture_identity(camera, source)
            or (
                scrcpy is not None
                and device == str(scrcpy.get("v4l2_sink") or "").strip()
            )
        )
    ):
        source = "scrcpy"

    if source == "lumos" and _legacy_loopback_device(device):
        device = ""

    camera["source"] = source
    camera["device"] = device

    if lumos is not None or source == "lumos":
        if lumos is None:
            lumos = {}
            data["lumos_cam"] = lumos
        lumos["enabled"] = source == "lumos"

    if source == "scrcpy":
        if scrcpy is None:
            scrcpy = {}
            data["scrcpy"] = scrcpy
        scrcpy["enabled"] = True
        sink = str(scrcpy.get("v4l2_sink") or "/dev/video11").strip() or "/dev/video11"
        scrcpy["v4l2_sink"] = sink
        camera["device"] = sink
    elif scrcpy is not None:
        scrcpy["enabled"] = False

    return data


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
