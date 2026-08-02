"""Quadrilateral helpers.

Throughout the code base a "quad" is a ``(4, 2)`` float32 array of pixel
coordinates ordered **top-left, top-right, bottom-right, bottom-left**.
Normalised quads use the same order but store coordinates in ``0..1`` so a
calibration stays valid if the capture resolution changes.
"""

from __future__ import annotations

import cv2
import numpy as np

UNIT_SQUARE = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]], dtype=np.float32)


def order_corners(points) -> np.ndarray:
    """Return the four points ordered TL, TR, BR, BL.

    Sorting by angle around the centroid keeps the winding consistent even for
    strongly skewed quads, where the usual "smallest x+y is top-left, smallest
    x-y is top-right" trick picks the same point twice.
    """
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
    if pts.shape[0] != 4:
        raise ValueError(f"expected 4 points, got {pts.shape[0]}")

    centre = pts.mean(axis=0)
    # Image coordinates have y pointing down, so increasing atan2 walks
    # clockwise on screen, which is the winding we want.
    angles = np.arctan2(pts[:, 1] - centre[1], pts[:, 0] - centre[0])
    pts = pts[np.argsort(angles)]

    start = int(np.argmin(pts[:, 0] + pts[:, 1]))
    return np.roll(pts, -start, axis=0).astype(np.float32)


def quad_area(quad) -> float:
    """Shoelace area of a quad in pixels."""
    q = np.asarray(quad, dtype=np.float64).reshape(-1, 2)
    x, y = q[:, 0], q[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0)


def quad_side_lengths(quad) -> tuple[float, float, float, float]:
    """Lengths of the top, right, bottom and left edges."""
    q = np.asarray(quad, dtype=np.float64).reshape(4, 2)
    return tuple(float(np.linalg.norm(q[(i + 1) % 4] - q[i])) for i in range(4))  # type: ignore[return-value]


def quad_aspect_ratio(quad) -> float:
    """Approximate width / height of a quad, averaging opposite edges."""
    top, right, bottom, left = quad_side_lengths(quad)
    width = (top + bottom) / 2.0
    height = (left + right) / 2.0
    if height <= 1e-6:
        return 0.0
    return width / height


def rectangle_aspect_ratio(quad, image_size: tuple[int, int]) -> float | None:
    """Recover the true width/height of a rectangle from its projection.

    The on-screen aspect ratio of a quad is *not* the aspect ratio of the
    rectangle it depicts -- viewed off-axis, a 16:9 TV can measure anything
    from 1.6 to 2.2.  Scoring candidates on their on-screen shape therefore
    rewards exactly the wrong ones, so we recover the real ratio instead.

    Uses the standard closed form (Zhang & He, *Whiteboard scanning and image
    enhancement*, 2006), which also solves for the unknown focal length.
    Returns ``None`` for a near-affine view, where the focal length is not
    recoverable and the naive ratio is already a good answer.

    ``image_size`` is ``(width, height)``; the principal point is assumed to
    be at the centre of the frame.
    """
    q = order_corners(quad).astype(np.float64)
    width, height = image_size
    centre = np.array([width / 2.0, height / 2.0], dtype=np.float64)

    # m1 top-left, m2 top-right, m3 bottom-left, m4 bottom-right.
    m1 = np.append(q[0] - centre, 1.0)
    m2 = np.append(q[1] - centre, 1.0)
    m3 = np.append(q[3] - centre, 1.0)
    m4 = np.append(q[2] - centre, 1.0)

    denominator_2 = np.dot(np.cross(m2, m4), m3)
    denominator_3 = np.dot(np.cross(m3, m4), m2)
    if abs(denominator_2) < 1e-9 or abs(denominator_3) < 1e-9:
        return None

    k2 = np.dot(np.cross(m1, m4), m3) / denominator_2
    k3 = np.dot(np.cross(m1, m4), m2) / denominator_3

    n2 = k2 * m2 - m1
    n3 = k3 * m3 - m1

    if abs(n2[2]) < 1e-9 or abs(n3[2]) < 1e-9:
        # Both vanishing points at infinity: the view is affine, so the
        # on-screen ratio is already correct.
        return quad_aspect_ratio(q)

    f_squared = -(n2[0] * n3[0] + n2[1] * n3[1]) / (n2[2] * n3[2])
    if f_squared <= 1e-6:
        return None
    f_squared = float(f_squared)

    # w/h = sqrt( n2^T W n2 / n3^T W n3 ) with W = (A A^T)^-1, A = diag(f, f, 1)
    def norm(n):
        return (n[0] ** 2 + n[1] ** 2) / f_squared + n[2] ** 2

    bottom = norm(n3)
    if bottom <= 1e-12:
        return None
    ratio = float(np.sqrt(norm(n2) / bottom))
    if not np.isfinite(ratio) or ratio <= 0:
        return None
    return ratio


def is_convex(quad) -> bool:
    """True when the quad has no reflex corner (no bow-tie, no dent)."""
    q = np.asarray(quad, dtype=np.float64).reshape(4, 2)
    edges = np.roll(q, -1, axis=0) - q
    nxt = np.roll(edges, -1, axis=0)
    # 2-D cross product; numpy 2 no longer accepts 2-vectors in np.cross.
    crosses = edges[:, 0] * nxt[:, 1] - edges[:, 1] * nxt[:, 0]
    return bool(np.all(crosses > 0) or np.all(crosses < 0))


def quad_to_normalised(quad, width: int, height: int) -> list[list[float]]:
    """Convert a pixel-space quad into resolution independent coordinates."""
    q = np.asarray(quad, dtype=np.float64).reshape(4, 2)
    q = q / np.array([max(width, 1), max(height, 1)], dtype=np.float64)
    return [[float(x), float(y)] for x, y in q]


def quad_from_normalised(quad, width: int, height: int) -> np.ndarray:
    """Inverse of :func:`quad_to_normalised`."""
    q = np.asarray(quad, dtype=np.float32).reshape(4, 2)
    return (q * np.array([width, height], dtype=np.float32)).astype(np.float32)


def homography_to_rect(quad, width: int, height: int) -> np.ndarray:
    """Homography mapping ``quad`` onto a ``width x height`` axis-aligned rect."""
    src = order_corners(quad)
    dst = np.array(
        [[0.0, 0.0], [width - 1.0, 0.0], [width - 1.0, height - 1.0], [0.0, height - 1.0]],
        dtype=np.float32,
    )
    return cv2.getPerspectiveTransform(src, dst)


def inset_quad(quad, left: float, top: float, right: float, bottom: float) -> np.ndarray:
    """Shrink (or, with negative values, grow) a quad by fractions of its own
    perspective-corrected size.

    The insets are applied in the *rectified* space and mapped back through the
    quad's homography, so a 2 % inset removes 2 % of the TV panel rather than
    2 % of the on-screen bounding box.  That distinction matters as soon as the
    camera is off-axis.

    Each argument is a fraction in ``-1.0..0.49``; negative values push the
    edge outwards, which is how a detected picture area is grown back out to
    the full panel.
    """
    src = order_corners(quad)
    left = float(np.clip(left, -1.0, 0.49))
    right = float(np.clip(right, -1.0, 0.49))
    top = float(np.clip(top, -1.0, 0.49))
    bottom = float(np.clip(bottom, -1.0, 0.49))

    inner = np.array(
        [
            [left, top],
            [1.0 - right, top],
            [1.0 - right, 1.0 - bottom],
            [left, 1.0 - bottom],
        ],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(UNIT_SQUARE, src)
    warped = cv2.perspectiveTransform(inner.reshape(1, 4, 2), matrix)
    return warped.reshape(4, 2).astype(np.float32)


def max_corner_shift(a, b) -> float:
    """Largest per-corner distance between two quads, in pixels."""
    qa = np.asarray(a, dtype=np.float64).reshape(4, 2)
    qb = np.asarray(b, dtype=np.float64).reshape(4, 2)
    return float(np.max(np.linalg.norm(qa - qb, axis=1)))


def clip_quad(quad, width: int, height: int) -> np.ndarray:
    """Clamp corners into the frame."""
    q = np.asarray(quad, dtype=np.float32).reshape(4, 2).copy()
    q[:, 0] = np.clip(q[:, 0], 0, max(width - 1, 0))
    q[:, 1] = np.clip(q[:, 1], 0, max(height - 1, 0))
    return q


def quad_mask(quad, width: int, height: int) -> np.ndarray:
    """Filled uint8 mask (255 inside the quad)."""
    mask = np.zeros((height, width), dtype=np.uint8)
    pts = np.asarray(quad, dtype=np.int32).reshape(1, 4, 2)
    cv2.fillPoly(mask, pts, 255)
    return mask


def full_frame_quad(width: int, height: int) -> np.ndarray:
    """The quad covering the whole frame -- the identity fallback."""
    return np.array(
        [[0.0, 0.0], [width - 1.0, 0.0], [width - 1.0, height - 1.0], [0.0, height - 1.0]],
        dtype=np.float32,
    )
