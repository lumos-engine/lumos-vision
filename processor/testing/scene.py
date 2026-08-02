"""A fake "camera pointed at a TV" scene.

Everything the real pipeline has to cope with is reproduced here so the stages
can be developed and regression tested without hardware: perspective, a bezel,
wall reflections, sensor noise, a colour cast, letterboxed content of varying
aspect ratio, a static channel logo, subtitles, and an optional camera bump.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from processor.utils.geometry import order_corners

def tv_quad(
    yaw: float = 0.0,
    pitch: float = 0.0,
    roll: float = 0.0,
    distance: float = 2.3,
    offset: tuple[float, float] = (0.0, 0.0),
    diagonal: float = 1.4,
    #: Horizontal field of view.  Narrower than a typical IP camera's, because
    #: anyone using this points the camera *at* the TV rather than at the room.
    fov: float = 40.0,
    aspect: float = 16.0 / 9.0,
) -> list[list[float]]:
    """Project a real 16:9 panel through a pinhole camera.

    Hand-picking four corners produces a quadrilateral that is *not* the image
    of a 16:9 rectangle, which quietly invalidates anything that reasons about
    the TV's true shape.  Deriving the corners from a camera pose keeps the
    test bench self-consistent: the detector's 16:9 prior is then being tested
    against a scene where it is actually true.

    Angles are degrees, ``distance`` and ``diagonal`` are metres, and
    ``offset`` shifts the camera sideways and vertically relative to the screen
    centre.  Returns normalised (0..1) corners in TL, TR, BR, BL order.
    """
    height = diagonal / math.hypot(aspect, 1.0)
    width = height * aspect
    corners = np.array(
        [
            [-width / 2, -height / 2, 0.0],
            [+width / 2, -height / 2, 0.0],
            [+width / 2, +height / 2, 0.0],
            [-width / 2, +height / 2, 0.0],
        ],
        dtype=np.float64,
    )

    ry, rx, rz = (math.radians(a) for a in (yaw, pitch, roll))
    rot_y = np.array([[math.cos(ry), 0, math.sin(ry)], [0, 1, 0], [-math.sin(ry), 0, math.cos(ry)]])
    rot_x = np.array([[1, 0, 0], [0, math.cos(rx), -math.sin(rx)], [0, math.sin(rx), math.cos(rx)]])
    rot_z = np.array([[math.cos(rz), -math.sin(rz), 0], [math.sin(rz), math.cos(rz), 0], [0, 0, 1]])

    points = corners @ (rot_z @ rot_x @ rot_y).T
    points[:, 0] += offset[0]
    points[:, 1] += offset[1]
    points[:, 2] += distance

    # Normalised image coordinates; f is expressed in units of image width.
    f = 0.5 / math.tan(math.radians(fov) / 2.0)
    u = f * points[:, 0] / points[:, 2] + 0.5
    # Scale y by the 16:9 frame's own aspect so the projection stays square.
    v = f * (16.0 / 9.0) * points[:, 1] / points[:, 2] + 0.5
    return [[float(x), float(y)] for x, y in zip(u, v)]


#: A camera sitting on a shelf beside the sofa: slightly to one side of the
#: screen, a little below it, and turned a few degrees off-axis.  Framed to
#: leave a margin all round, so tests can nudge the camera without pushing the
#: TV out of shot.
DEFAULT_QUAD = tv_quad(yaw=-9.0, pitch=5.0, roll=1.5, distance=2.6, offset=(-0.02, -0.06))


@dataclass
class SceneParams:
    width: int = 960
    height: int = 540
    quad: list[list[float]] = field(default_factory=lambda: [list(p) for p in DEFAULT_QUAD])
    #: Aspect ratio of the *content*; anything above 16:9 produces black bars.
    content_aspect: float = 2.39
    #: Panel resolution used to render the TV picture before warping.
    panel_size: tuple[int, int] = (640, 360)
    bezel_px: int = 7
    reflection_strength: float = 0.22
    noise_sigma: float = 2.5
    #: Multiplicative BGR cast, simulating an uncalibrated camera sensor.
    color_cast: tuple[float, float, float] = (0.92, 1.0, 1.06)
    exposure: float = 0.92
    #: Sub-pixel handheld-style jitter, in pixels.
    shake_px: float = 0.35
    #: Seconds after which the camera is "bumped"; None disables it.
    bump_at: float | None = None
    bump_offset: tuple[float, float] = (0.035, 0.02)
    show_logo: bool = True
    show_subtitles: bool = True
    seed: int = 7


def render_panel(
    t: float,
    size: tuple[int, int] = (640, 360),
    content_aspect: float = 2.39,
    show_logo: bool = True,
    show_subtitles: bool = True,
) -> np.ndarray:
    """Render what the TV is displaying, including its letterbox bars."""
    width, height = size
    panel = np.zeros((height, width, 3), dtype=np.uint8)

    panel_aspect = width / height
    if content_aspect > panel_aspect:  # wider than the panel -> bars top/bottom
        content_h = max(2, int(round(width / content_aspect)))
        content_w = width
        y0 = (height - content_h) // 2
        x0 = 0
    else:  # taller than the panel -> pillarbox
        content_w = max(2, int(round(height * content_aspect)))
        content_h = height
        x0 = (width - content_w) // 2
        y0 = 0

    content = np.empty((content_h, content_w, 3), dtype=np.uint8)

    # A slow colour sweep gives every row and column something to detect, and a
    # background that is never uniformly black.  Each channel varies along a
    # single axis, so these are 1-D sines broadcast across the frame rather
    # than three full-resolution meshgrid evaluations.
    xs = np.linspace(0, 1, content_w, dtype=np.float32)
    ys = np.linspace(0, 1, content_h, dtype=np.float32)
    phase = t * 0.35
    sweep_b = (110 + 90 * np.sin(2 * math.pi * (xs + phase))).clip(0, 255)
    sweep_g = (100 + 80 * np.sin(2 * math.pi * (ys * 0.7 + phase * 1.3))).clip(0, 255)
    sweep_r = (120 + 90 * np.sin(2 * math.pi * (xs * 0.5 - phase * 0.8))).clip(0, 255)
    content[:, :, 0] = sweep_b[None, :]
    content[:, :, 1] = sweep_g[:, None]
    content[:, :, 2] = sweep_r[None, :]

    # Moving highlights, so frame differencing sees genuine motion.
    for i in range(3):
        angle = t * (0.6 + 0.25 * i) + i * 2.1
        cx = int((0.5 + 0.34 * math.cos(angle)) * content_w)
        cy = int((0.5 + 0.30 * math.sin(angle * 1.4)) * content_h)
        radius = int(0.09 * content_w * (1.0 + 0.25 * math.sin(t + i)))
        colour = (
            int(200 + 55 * math.sin(t + i)),
            int(160 + 90 * math.cos(t * 1.2 + i)),
            int(220 - 60 * math.sin(t * 0.8 + i)),
        )
        cv2.circle(content, (cx, cy), max(4, radius), colour, -1, lineType=cv2.LINE_AA)

    if show_subtitles and int(t) % 6 < 3:
        text_y = int(content_h * 0.88)
        cv2.putText(
            content,
            "SUBTITLE LINE EXAMPLE",
            (int(content_w * 0.18), text_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            content_w / 900.0,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

    if show_logo:
        cv2.rectangle(
            content,
            (int(content_w * 0.86), int(content_h * 0.06)),
            (int(content_w * 0.96), int(content_h * 0.16)),
            (235, 235, 245),
            -1,
        )

    panel[y0 : y0 + content_h, x0 : x0 + content_w] = content
    return panel


def _render_room(width: int, height: int, rng: np.random.Generator) -> np.ndarray:
    """Static background: a lit wall with the usual living-room clutter.

    The clutter is not decoration.  Camera-movement detection works by
    noticing that fixed scenery shifted, so a perfectly bare wall would make
    the test bench easier than reality rather than harder.
    """
    ys = np.linspace(0, 1, height, dtype=np.float32)[:, None]
    xs = np.linspace(0, 1, width, dtype=np.float32)[None, :]

    wall = 48 + 26 * (1.0 - ys) + 14 * np.exp(-(((xs - 0.12) / 0.35) ** 2))
    room = np.repeat(wall[:, :, None], 3, axis=2)
    room[:, :, 0] *= 1.06  # cool-ish wall paint
    room[:, :, 2] *= 0.94

    texture = rng.normal(0.0, 4.0, size=(height, width, 1)).astype(np.float32)
    room += cv2.GaussianBlur(texture, (0, 0), 2.0)[:, :, None]
    room = room.astype(np.uint8)

    def rect(x0, y0, x1, y1, colour, thickness=-1):
        cv2.rectangle(
            room,
            (int(x0 * width), int(y0 * height)),
            (int(x1 * width), int(y1 * height)),
            colour,
            thickness,
        )

    # Sideboard the TV stands near, with a lit top edge.
    rect(0.0, 0.88, 1.0, 1.0, (38, 46, 58))
    rect(0.0, 0.88, 1.0, 0.895, (58, 70, 86))
    rect(0.16, 0.90, 0.30, 0.97, (28, 34, 44))
    rect(0.32, 0.90, 0.46, 0.97, (28, 34, 44))

    # Framed picture on the left.
    rect(0.03, 0.16, 0.13, 0.44, (30, 38, 52))
    rect(0.04, 0.18, 0.12, 0.42, (86, 96, 112))
    rect(0.055, 0.21, 0.105, 0.39, (52, 74, 96))

    # Door frame on the right.
    rect(0.92, 0.02, 1.0, 0.89, (26, 32, 42))
    rect(0.935, 0.05, 1.0, 0.89, (70, 80, 96))

    # A floor lamp: pole plus shade.
    rect(0.885, 0.34, 0.897, 0.88, (60, 66, 76))
    cv2.ellipse(
        room,
        (int(0.891 * width), int(0.30 * height)),
        (int(0.035 * width), int(0.05 * height)),
        0, 0, 360, (150, 152, 138), -1,
    )

    # Plant silhouette in the corner.
    for angle in (-55, -25, 0, 30, 60):
        tip = (
            int((0.055 + 0.045 * math.sin(math.radians(angle))) * width),
            int((0.86 - 0.11 * math.cos(math.radians(angle))) * height),
        )
        cv2.line(room, (int(0.055 * width), int(0.87 * height)), tip, (34, 62, 40), 3, cv2.LINE_AA)

    return room


class SyntheticScene:
    """Deterministic, time-parameterised fake camera view."""

    def __init__(self, params: SceneParams | None = None):
        self.params = params or SceneParams()
        self._rng = np.random.default_rng(self.params.seed)
        self._room = _render_room(self.params.width, self.params.height, self._rng)
        self._reflection = self._build_reflection()

        p = self.params
        ys = np.linspace(-1, 1, p.height, dtype=np.float32)[:, None]
        xs = np.linspace(-1, 1, p.width, dtype=np.float32)[None, :]
        vignette = (1.0 - 0.22 * (xs**2 + ys**2))[:, :, None]
        # Fold the constant per-pixel gains into one array so the per-frame
        # camera simulation is a single multiply.
        self._gain = (vignette * np.array(p.color_cast, dtype=np.float32) * p.exposure).astype(
            np.float32
        )
        # Sampling 1.5M gaussians per frame dominated the render.  One pool,
        # rolled by a random offset each frame, is visually equivalent and
        # about a hundred times cheaper.
        self._noise_pool = (
            self._rng.normal(0.0, 1.0, size=(p.height, p.width, 3)).astype(np.float32)
            if p.noise_sigma > 0
            else None
        )

    # -- geometry ---------------------------------------------------------

    def quad_at(self, t: float) -> np.ndarray:
        """TV corners in pixels at time ``t``, including shake and bumps."""
        p = self.params
        quad = np.asarray(p.quad, dtype=np.float32).reshape(4, 2).copy()

        if p.bump_at is not None and t >= p.bump_at:
            quad[:, 0] += p.bump_offset[0]
            quad[:, 1] += p.bump_offset[1]

        quad[:, 0] *= p.width
        quad[:, 1] *= p.height

        if p.shake_px > 0:
            # Deterministic in t so the same timestamp always renders the same
            # frame -- tests depend on that.
            for i in range(4):
                quad[i, 0] += p.shake_px * math.sin(t * 5.3 + i * 1.7)
                quad[i, 1] += p.shake_px * math.cos(t * 4.1 + i * 2.3)
        return order_corners(quad)

    @property
    def true_quad(self) -> np.ndarray:
        """The un-shaken boundary of the visible picture, in pixels.

        This is the ground truth the pipeline should converge on: the edge of
        the panel's image area, with the bezel outside it.
        """
        p = self.params
        quad = np.asarray(p.quad, dtype=np.float32).reshape(4, 2).copy()
        quad[:, 0] *= p.width
        quad[:, 1] *= p.height
        return order_corners(quad)

    # -- rendering --------------------------------------------------------

    def _build_reflection(self) -> np.ndarray:
        """Soft glare blobs in panel space: a lamp and a window, roughly.

        Deliberately kept near the edges and away from the centre, because
        that is where real reflections land and where they do the most damage
        to an ambient light system.
        """
        w, h = self.params.panel_size
        blob = np.zeros((h, w), dtype=np.float32)
        cv2.ellipse(
            blob, (int(w * 0.15), int(h * 0.46)), (int(w * 0.12), int(h * 0.17)), 25, 0, 360, 1.0, -1
        )
        cv2.ellipse(
            blob, (int(w * 0.88), int(h * 0.66)), (int(w * 0.07), int(h * 0.09)), -15, 0, 360, 0.7, -1
        )
        return cv2.GaussianBlur(blob, (0, 0), max(w, h) * 0.04)

    def frame(self, t: float) -> np.ndarray:
        p = self.params
        panel = render_panel(t, p.panel_size, p.content_aspect, p.show_logo, p.show_subtitles)

        if p.reflection_strength > 0:
            glare = (self._reflection * (255.0 * p.reflection_strength))[:, :, None]
            panel = np.clip(panel.astype(np.float32) + glare, 0, 255).astype(np.uint8)

        quad = self.quad_at(t)
        scene = self._room.copy()

        panel_h, panel_w = panel.shape[:2]
        src = np.array(
            [[0, 0], [panel_w - 1, 0], [panel_w - 1, panel_h - 1], [0, panel_h - 1]],
            dtype=np.float32,
        )
        matrix = cv2.getPerspectiveTransform(src, quad)
        warped = cv2.warpPerspective(panel, matrix, (p.width, p.height), flags=cv2.INTER_LINEAR)

        mask = np.zeros((p.height, p.width), dtype=np.uint8)
        cv2.fillConvexPoly(mask, quad.astype(np.int32), 255)

        if p.bezel_px > 0:
            # Draw the bezel straddling the quad, then paint the picture over
            # its inner half.  That leaves the bezel entirely *outside* the
            # quad, so `quad` is exactly the visible picture boundary -- which
            # is what the pipeline is supposed to find and what ground-truth
            # comparisons should be measured against.
            cv2.polylines(
                scene,
                [quad.astype(np.int32)],
                True,
                (18, 18, 20),
                p.bezel_px * 2,
                lineType=cv2.LINE_AA,
            )

        scene[mask > 0] = warped[mask > 0]

        # A TV lights up the wall around it.  Blurring at quarter resolution is
        # indistinguishable at this sigma and vastly cheaper.
        lit = cv2.bitwise_and(warped, warped, mask=mask)
        small = cv2.resize(lit, (p.width // 4, p.height // 4), interpolation=cv2.INTER_AREA)
        glow = cv2.resize(
            cv2.GaussianBlur(small, (0, 0), 7), (p.width, p.height), interpolation=cv2.INTER_LINEAR
        )
        scene = cv2.addWeighted(scene, 1.0, glow, 0.22, 0)

        return self._apply_camera(scene)

    def _apply_camera(self, image: np.ndarray) -> np.ndarray:
        """Sensor-side degradation: exposure, colour cast, vignette, noise."""
        p = self.params
        out = image.astype(np.float32) * self._gain

        if self._noise_pool is not None:
            shift = (
                int(self._rng.integers(0, p.height)),
                int(self._rng.integers(0, p.width)),
            )
            out += np.roll(self._noise_pool, shift, axis=(0, 1)) * p.noise_sigma

        return np.clip(out, 0, 255).astype(np.uint8)
