"""Finding the TV in a camera frame.

Three independent cues are combined, because no single one is reliable in a
living room:

* **Activity** -- TV content changes, walls do not.  Accumulating peak frame
  difference over a couple of seconds paints a bright blob exactly where the
  screen is, and it does not care about reflections, bezels or furniture.
* **Edges** -- the bezel/panel border is usually the strongest straight
  rectangle in the frame.
* **Brightness** -- a lit screen is brighter than the wall it hangs on.

Each cue proposes candidate quadrilaterals; every candidate is scored against
all the cues plus geometric priors (convex, roughly 16:9, sensibly sized) and
the best one wins.  A cue that fails silently costs accuracy, not detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from processor.config.schema import BoundaryConfig
from processor.utils.geometry import (
    inset_quad,
    is_convex,
    rectangle_aspect_ratio,
    order_corners,
    quad_area,
    quad_aspect_ratio,
    quad_mask,
    quad_side_lengths,
)


#: A candidate must contain at least this fraction of the moving pixels to be
#: considered the TV at all.
MIN_ACTIVITY_RECALL = 0.85

#: ...and must not be more than this much larger than the moving region.
MAX_ACTIVITY_BLOAT = 1.75


@dataclass
class Detection:
    """A candidate TV boundary, in detection-scale pixel coordinates."""

    quad: np.ndarray
    confidence: float
    origin: str = "unknown"
    parts: dict[str, float] = field(default_factory=dict)


def auto_canny(gray: np.ndarray, low: int = 0, high: int = 0) -> np.ndarray:
    """Canny with thresholds derived from the image, unless overridden."""
    if low > 0 and high > 0:
        return cv2.Canny(gray, low, high)
    median = float(np.median(gray))
    lower = int(max(0, 0.66 * median))
    upper = int(min(255, 1.33 * median))
    if upper <= lower:
        lower, upper = 50, 150
    return cv2.Canny(gray, lower, upper)


def quads_from_contour(contour: np.ndarray) -> list[np.ndarray]:
    """Several plausible 4-point fits for one contour.

    ``approxPolyDP`` is fussy about its epsilon, so instead of guessing one
    value we sweep a range and let the scorer decide.  Two geometric fallbacks
    cover the cases where no epsilon yields exactly four vertices.
    """
    candidates: list[np.ndarray] = []
    hull = cv2.convexHull(contour)
    if len(hull) < 4:
        return candidates

    perimeter = cv2.arcLength(hull, True)
    if perimeter <= 0:
        return candidates

    for eps in (0.008, 0.012, 0.018, 0.025, 0.035, 0.05, 0.07):
        approx = cv2.approxPolyDP(hull, eps * perimeter, True)
        if len(approx) == 4:
            candidates.append(approx.reshape(4, 2).astype(np.float32))

    # Extreme-corner heuristic: works whenever the TV is roughly upright,
    # which it is unless the camera is mounted sideways.
    points = hull.reshape(-1, 2).astype(np.float32)
    if len(points) >= 4:
        total = points.sum(axis=1)
        diff = points[:, 0] - points[:, 1]
        extremes = np.array(
            [
                points[int(np.argmin(total))],
                points[int(np.argmax(diff))],
                points[int(np.argmax(total))],
                points[int(np.argmin(diff))],
            ],
            dtype=np.float32,
        )
        if len(np.unique(extremes, axis=0)) == 4:
            candidates.append(extremes)

    box = cv2.boxPoints(cv2.minAreaRect(contour)).astype(np.float32)
    candidates.append(box)
    return candidates


def complete_to_aspect(
    quad: np.ndarray, target_aspect: float, image_size: tuple[int, int]
) -> np.ndarray | None:
    """Grow a picture-area quad outwards until it has the panel's aspect ratio.

    The activity cue finds what is *moving*, which for a letterboxed film is
    the picture and not the screen.  Since the panel is 16:9 and the picture
    sits centred inside it, the missing bars can be added back geometrically
    instead of hunting for their outer edge in a noisy edge map -- an edge
    which, for a black bar against a black bezel, may not exist at all.

    The padding is derived from the *recovered* rectangle aspect, not the
    on-screen one, because the outset itself happens in rectified space.
    """
    if target_aspect <= 0:
        return None
    aspect = rectangle_aspect_ratio(quad, image_size)
    if aspect is None:
        aspect = quad_aspect_ratio(quad)
    if aspect <= 0:
        return None

    if aspect > target_aspect:  # letterboxed: grow vertically
        pad = (aspect / target_aspect - 1.0) / 2.0
        if pad < 0.02 or pad > 0.6:
            return None
        return inset_quad(quad, left=0.0, right=0.0, top=-pad, bottom=-pad)

    pad = (target_aspect / aspect - 1.0) / 2.0  # pillarboxed: grow horizontally
    if pad < 0.02 or pad > 0.6:
        return None
    return inset_quad(quad, left=-pad, right=-pad, top=0.0, bottom=0.0)


def _line_from_points(points: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """Robust line fit, returned as (point on line, unit direction)."""
    if len(points) < 3:
        return None
    vx, vy, x0, y0 = cv2.fitLine(points.astype(np.float32), cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
    return np.array([x0, y0], dtype=np.float64), np.array([vx, vy], dtype=np.float64)


def _intersect(a, b) -> np.ndarray | None:
    """Intersection of two lines given as (point, direction)."""
    (p, u), (q, v) = a, b
    denominator = u[0] * v[1] - u[1] * v[0]
    if abs(denominator) < 1e-9:  # parallel
        return None
    w = q - p
    t = (w[0] * v[1] - w[1] * v[0]) / denominator
    return p + t * u


def refine_quad(
    quad: np.ndarray,
    gradient: np.ndarray,
    max_shift: float,
    samples: int = 17,
) -> np.ndarray | None:
    """Snap each side of a quad onto the strongest nearby image edge.

    Contour fitting gets the TV roughly right but rarely lands on the panel
    border: contours merge with a lamp or a picture frame, and the geometric
    "grow to 16:9" step inherits whatever error the picture area had.  Fitting
    each side independently to the gradient ridge beside it fixes both,
    because the search is local -- clutter more than ``max_shift`` away cannot
    pull the edge towards it.
    """
    quad = np.asarray(quad, dtype=np.float64).reshape(4, 2)
    centre = quad.mean(axis=0)
    height, width = gradient.shape[:2]
    offsets = np.arange(-max_shift, max_shift + 1.0, 1.0, dtype=np.float32)

    # Some sides legitimately have no edge to find: the top of a letterboxed
    # picture is black against a black bezel.  A ridge therefore has to clear
    # an absolute bar derived from the image as a whole, not just be the
    # largest value in its own neighbourhood -- otherwise those sides snap
    # onto whatever noise happens to be nearby.
    evidence_floor = 0.25 * float(np.percentile(gradient, 97))

    lines = []
    refined_sides = 0
    for i in range(4):
        start, end = quad[i], quad[(i + 1) % 4]
        edge = end - start
        length = float(np.linalg.norm(edge))
        if length < 8:
            return None

        normal = np.array([-edge[1], edge[0]]) / length
        if np.dot(normal, start + 0.5 * edge - centre) < 0:
            normal = -normal  # point outwards

        t = np.linspace(0.12, 0.88, samples)[:, None]
        bases = start + t * edge  # (samples, 2)

        # (samples, len(offsets)) grid of probe coordinates along each normal.
        xs = (bases[:, 0][:, None] + offsets[None, :] * normal[0]).astype(np.float32)
        ys = (bases[:, 1][:, None] + offsets[None, :] * normal[1]).astype(np.float32)
        inside = (xs >= 0) & (xs < width - 1) & (ys >= 0) & (ys < height - 1)
        profile = cv2.remap(
            gradient, xs, ys, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0.0
        )
        profile[~inside] = 0.0

        peaks = profile.max(axis=1)
        best = offsets[profile.argmax(axis=1)]
        keep = peaks > max(evidence_floor, 0.35 * float(np.median(peaks)))

        line = None
        if int(np.count_nonzero(keep)) >= max(5, samples // 2):
            points = bases[keep] + best[keep][:, None] * normal
            line = _line_from_points(points)

        if line is None:
            # Not enough evidence: keep this side exactly where it was.
            direction = edge / length
            lines.append((start.copy(), direction))
        else:
            lines.append(line)
            refined_sides += 1

    if refined_sides == 0:
        return None

    corners = []
    for i in range(4):
        point = _intersect(lines[i - 1], lines[i])
        if point is None:
            return None
        corners.append(point)

    refined = np.array(corners, dtype=np.float32)
    # Reject a refinement that ran away; the original is then the safer answer.
    if np.max(np.linalg.norm(refined - quad, axis=1)) > max_shift * 2.5:
        return None
    return order_corners(refined)


def largest_contours(mask: np.ndarray, count: int = 3) -> list[np.ndarray]:
    found = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = found[0] if len(found) == 2 else found[1]
    if not contours:
        return []
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    return list(contours[:count])


class TvQuadDetector:
    """Stateful detector: accumulates activity, fits a quad on demand."""

    def __init__(self, config: BoundaryConfig):
        self.config = config
        self._activity: np.ndarray | None = None
        self._previous_gray: np.ndarray | None = None
        self._observations = 0
        self.last_debug: dict[str, np.ndarray] = {}

    # -- per-frame bookkeeping --------------------------------------------

    @property
    def ready(self) -> bool:
        """True once there is enough motion history to attempt a detection."""
        if not self.config.use_activity:
            return True
        return self._observations >= max(2, self.config.activity_frames // 4)

    @property
    def mature(self) -> bool:
        """True once the activity map has seen a full window of frames.

        Detections made before this are usable -- and worth showing, so the
        picture appears quickly -- but they are made from a partial map and
        should not be treated as final.
        """
        if not self.config.use_activity:
            return True
        return self._observations >= self.config.activity_frames

    @property
    def observations(self) -> int:
        return self._observations

    def reset(self) -> None:
        self._activity = None
        self._previous_gray = None
        self._observations = 0
        self.last_debug.clear()

    def observe(self, frame_bgr: np.ndarray) -> None:
        """Feed a frame into the activity accumulator.  Cheap; call always."""
        if not self.config.use_activity:
            return
        gray = self._to_detect_gray(frame_bgr)

        if self._previous_gray is None or self._previous_gray.shape != gray.shape:
            self._previous_gray = gray
            self._activity = np.zeros(gray.shape, dtype=np.float32)
            self._observations = 0
            return

        diff = cv2.absdiff(gray, self._previous_gray).astype(np.float32)
        self._previous_gray = gray

        # Peak-with-decay rather than a running mean: a TV that briefly shows a
        # static frame should not fade out of the activity map, but a one-off
        # event (someone walking past) should decay away within a few seconds.
        assert self._activity is not None
        np.maximum(self._activity * 0.96, diff, out=self._activity)
        self._observations += 1

    # -- detection ---------------------------------------------------------

    def detect(self, frame_bgr: np.ndarray, collect_debug: bool = False) -> Detection | None:
        """Best-scoring TV quad, in *source frame* coordinates."""
        gray = self._to_detect_gray(frame_bgr)
        height, width = gray.shape[:2]
        source_h, source_w = frame_bgr.shape[:2]
        upscale = source_w / float(width) if width else 1.0

        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = auto_canny(blurred, self.config.canny_low, self.config.canny_high)
        edges_thick = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

        activity_mask = self._activity_mask(gray.shape)
        candidates = self._collect_candidates(blurred, edges_thick, activity_mask)

        if collect_debug:
            self.last_debug = {
                "edges": edges_thick,
                "activity": (
                    np.zeros(gray.shape, np.uint8) if activity_mask is None else activity_mask
                ),
            }

        def best_of(subset) -> Detection | None:
            winner: Detection | None = None
            for quad, origin in subset:
                scored = self._score(quad, (height, width), activity_mask, edges_thick, origin)
                if scored is None:
                    continue
                if winner is None or scored.confidence > winner.confidence:
                    winner = scored
            return winner

        # Activity is the only cue that is specific to a *television*: it is
        # the one thing in the room that moves.  Edges and brightness match
        # picture frames, windows and doorways just as happily, so when there
        # is activity to go on, the answer is chosen from among the shapes it
        # implies and the other cues are demoted to refining it.
        best = None
        if activity_mask is not None:
            best = best_of([c for c in candidates if c[1].startswith("activity")])
        if best is None or best.confidence < self.config.min_confidence:
            fallback = best_of(candidates)
            if fallback is not None and (best is None or fallback.confidence > best.confidence):
                best = fallback

        if best is None:
            return None

        best = self._refine(best, blurred, (height, width), activity_mask, edges_thick)
        quad = order_corners(best.quad * upscale)
        quad[:, 0] = np.clip(quad[:, 0], 0, source_w - 1)
        quad[:, 1] = np.clip(quad[:, 1], 0, source_h - 1)
        return Detection(quad, best.confidence, best.origin, best.parts)

    # -- internals ---------------------------------------------------------

    def _to_detect_gray(self, frame_bgr: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        target = max(120, int(self.config.detect_width))
        if gray.shape[1] > target:
            scale = target / float(gray.shape[1])
            gray = cv2.resize(
                gray,
                (target, max(1, int(round(gray.shape[0] * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        return gray

    def _activity_mask(self, shape: tuple[int, ...]) -> np.ndarray | None:
        """Binary mask of "things that move", which is the TV and nothing else.

        Thresholded with the triangle method, which is built for exactly this
        histogram shape: one tall peak (a room that does not move) and a long
        tail (a screen that does), and it places the cut just above the peak.

        Otsu, the obvious first choice, fails badly here.  It maximises
        between-class variance, and the split it finds is "fast-moving
        highlights" versus "everything else", which throws away the slowly
        changing majority of the picture and keeps barely a third of the
        screen.  Scaling from the bright tail fails for the same reason, and
        estimating a noise floor from the median only works while the room
        occupies most of the frame -- which it does not, since the whole point
        is to aim the camera at the TV.

        Measured over the sample scenes, this recovers ~100 % of the picture
        area with under 3 % spill onto the room.
        """
        if not self.config.use_activity or self._activity is None:
            return None
        if self._activity.shape != tuple(shape[:2]):
            return None
        if self._observations < 2:
            return None

        activity = np.clip(self._activity, 0, 255).astype(np.uint8)
        activity = cv2.GaussianBlur(activity, (5, 5), 0)

        if float(np.percentile(activity, 99)) < 12.0:
            return None  # nothing on screen is moving; no usable signal

        _, mask = cv2.threshold(activity, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_TRIANGLE)
        covered = float(np.count_nonzero(mask)) / mask.size
        if covered < 0.005 or covered > 0.75:
            return None

        # Close hard: a letterbox bar, a static logo and a dark scene all leave
        # holes that belong to the same screen.
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8), iterations=3)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        return mask

    def _refine(
        self,
        best: Detection,
        blurred: np.ndarray,
        shape: tuple[int, int],
        activity_mask: np.ndarray | None,
        edges: np.ndarray,
    ) -> Detection:
        """Snap the winning candidate onto real edges, keeping it only if it
        scores better than what we started with."""
        gradient = cv2.magnitude(
            cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3),
            cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3),
        )
        top, right, bottom, left = quad_side_lengths(best.quad)
        span = min((top + bottom) / 2.0, (left + right) / 2.0)
        max_shift = float(np.clip(0.14 * span, 4.0, 0.06 * shape[1] + 12.0))

        current = best
        # Two passes: the first closes most of the gap, the second lands on the
        # ridge now that the search window is centred near it.
        for _ in range(2):
            snapped = refine_quad(current.quad, gradient, max_shift)
            if snapped is None:
                break
            scored = self._score(snapped, shape, activity_mask, edges, f"{current.origin}~")
            if scored is None or scored.confidence <= current.confidence:
                break
            current = scored
            max_shift = max(4.0, max_shift * 0.5)
        return current

    def _collect_candidates(
        self,
        blurred: np.ndarray,
        edges: np.ndarray,
        activity_mask: np.ndarray | None,
    ) -> list[tuple[np.ndarray, str]]:
        raw: list[tuple[np.ndarray, str]] = []

        if activity_mask is not None:
            for contour in largest_contours(activity_mask, 2):
                raw += [(q, "activity") for q in quads_from_contour(contour)]

        for contour in largest_contours(edges, 4):
            raw += [(q, "edges") for q in quads_from_contour(contour)]

        _, bright = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        for contour in largest_contours(bright, 2):
            raw += [(q, "bright") for q in quads_from_contour(contour)]

        # Every cue tends to find the *picture*, not the panel: activity stops
        # at the letterbox bars and the strongest edges are the bar/content
        # boundaries.  So for each candidate also offer the panel it would
        # imply, grown out to 16:9.
        candidates = list(raw)
        size = (blurred.shape[1], blurred.shape[0])
        for quad, origin in raw:
            completed = complete_to_aspect(quad, self.config.target_aspect, size)
            if completed is not None:
                candidates.append((completed, f"{origin}+bars"))

        return candidates

    def _score(
        self,
        quad: np.ndarray,
        shape: tuple[int, int],
        activity_mask: np.ndarray | None,
        edges: np.ndarray,
        origin: str,
    ) -> Detection | None:
        cfg = self.config
        height, width = shape
        frame_area = float(height * width)

        try:
            quad = order_corners(quad)
        except ValueError:
            return None

        area_frac = quad_area(quad) / frame_area
        if not (cfg.min_area_frac <= area_frac <= cfg.max_area_frac):
            return None
        if not is_convex(quad):
            return None

        # Judge the shape of the *rectangle*, not of its projection.  A 16:9
        # panel seen from the sofa can measure anywhere from 1.4 to 2.2 on
        # screen, so scoring the on-screen ratio systematically prefers
        # candidates that are wrong in a compensating way.
        aspect = rectangle_aspect_ratio(quad, (width, height))
        if aspect is None:
            aspect = quad_aspect_ratio(quad)
        if aspect <= 0:
            return None
        aspect_error = abs(aspect - cfg.target_aspect) / cfg.target_aspect
        if aspect_error > cfg.aspect_tolerance:
            return None
        aspect_score = 1.0 - (aspect_error / cfg.aspect_tolerance)

        mask = quad_mask(quad, width, height)
        inside = mask > 0
        inside_count = int(np.count_nonzero(inside))
        if inside_count < 50:
            return None

        recall = 1.0
        bloat_score = 1.0
        if activity_mask is not None:
            total_activity = int(np.count_nonzero(activity_mask))
            captured = int(np.count_nonzero(activity_mask[inside]))
            recall = captured / total_activity if total_activity else 0.0

            # Nothing outside the TV moves, so a candidate that fails to
            # contain the moving pixels is not the TV.  This one filter throws
            # out picture frames, doorways and half-screen contour fits.
            if recall < MIN_ACTIVITY_RECALL:
                return None

            # Containing the activity is necessary but not sufficient: a quad
            # covering the whole wall also contains it.  A 16:9 panel showing
            # 2.39:1 content -- about the widest thing anyone releases -- is
            # 1.34x the moving area, so anything much beyond that has swallowed
            # a picture frame or a doorway along with the TV.
            ratio = inside_count / max(total_activity, 1)
            if ratio > MAX_ACTIVITY_BLOAT:
                return None
            bloat_score = 1.0 if ratio <= 1.4 else float(np.clip(1.4 / ratio, 0.0, 1.0))

        activity_score = 0.6 * recall + 0.4 * bloat_score

        outline = np.zeros((height, width), dtype=np.uint8)
        cv2.polylines(outline, [quad.astype(np.int32)], True, 255, 3)
        outline_pixels = outline > 0
        edge_score = (
            float(np.count_nonzero(edges[outline_pixels])) / float(np.count_nonzero(outline_pixels))
            if np.any(outline_pixels)
            else 0.0
        )

        # A TV that fills a third of the frame is the common case; below that
        # we are probably looking at a picture frame or a reflection.
        area_score = float(min(1.0, area_frac / 0.35))

        # Aspect carries the most weight because "a TV is 16:9" is the most
        # reliable thing we know, and because edge support is *biased*: on
        # letterboxed content the bar/picture boundary is a far stronger edge
        # than the panel border, so leaning on edges picks the wrong rectangle.
        confidence = (
            0.38 * aspect_score + 0.28 * activity_score + 0.22 * edge_score + 0.12 * area_score
        )
        return Detection(
            quad=quad,
            confidence=float(np.clip(confidence, 0.0, 1.0)),
            origin=origin,
            parts={
                "aspect": round(aspect, 3),
                "aspect_score": round(aspect_score, 3),
                "activity_recall": round(recall, 3),
                "activity_score": round(activity_score, 3),
                "edge_score": round(edge_score, 3),
                "area_frac": round(area_frac, 3),
            },
        )
