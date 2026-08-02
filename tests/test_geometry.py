import numpy as np
import pytest

from processor.utils.geometry import (
    clip_quad,
    full_frame_quad,
    homography_to_rect,
    inset_quad,
    is_convex,
    max_corner_shift,
    order_corners,
    quad_area,
    quad_aspect_ratio,
    quad_from_normalised,
    quad_to_normalised,
)

SQUARE = [[10, 10], [110, 10], [110, 60], [10, 60]]


def test_order_corners_is_independent_of_input_order():
    expected = order_corners(SQUARE)
    for shift in range(4):
        rotated = np.roll(np.array(SQUARE), shift, axis=0)
        assert np.allclose(order_corners(rotated), expected)
    assert np.allclose(order_corners(list(reversed(SQUARE))), expected)


def test_order_corners_labels_are_tl_tr_br_bl():
    ordered = order_corners(SQUARE)
    assert tuple(ordered[0]) == (10, 10)
    assert tuple(ordered[1]) == (110, 10)
    assert tuple(ordered[2]) == (110, 60)
    assert tuple(ordered[3]) == (10, 60)


def test_order_corners_handles_strong_perspective():
    # A quad where "smallest x+y" and "smallest x-y" would pick the same point.
    skewed = [[0, 40], [90, 0], [140, 70], [30, 95]]
    ordered = order_corners(skewed)
    assert len(np.unique(ordered, axis=0)) == 4
    assert is_convex(ordered)


def test_order_corners_rejects_wrong_count():
    with pytest.raises(ValueError):
        order_corners([[0, 0], [1, 1], [2, 2]])


def test_quad_area_and_aspect():
    assert quad_area(SQUARE) == pytest.approx(100 * 50)
    assert quad_aspect_ratio(SQUARE) == pytest.approx(2.0)


def test_is_convex_rejects_bowtie_and_dent():
    assert is_convex(SQUARE)
    assert not is_convex([[0, 0], [100, 0], [0, 50], [100, 50]])  # bow-tie
    assert not is_convex([[0, 0], [100, 0], [50, 25], [0, 50]])  # reflex corner


def test_inset_quad_shrinks_symmetrically():
    inner = inset_quad(SQUARE, left=0.1, top=0.1, right=0.1, bottom=0.1)
    assert quad_area(inner) == pytest.approx(quad_area(SQUARE) * 0.8 * 0.8, rel=1e-3)
    # Still inside the original.
    assert inner[:, 0].min() > 10 and inner[:, 0].max() < 110


def test_inset_quad_respects_perspective():
    """The inset is a fraction of the *panel*, not of the on-screen box.

    For a trapezoid the two are different: half of the physical screen width
    is not half of the pixel width, and the panel is what we care about.
    """
    import cv2

    trapezoid = [[0, 0], [100, 20], [100, 80], [0, 120]]
    inner = inset_quad(trapezoid, left=0.1, top=0.2, right=0.3, bottom=0.05)

    # Rectify both quads with the same homography; in that space the inset
    # must land on exactly the requested fractions.
    matrix = homography_to_rect(trapezoid, 1000, 1000)
    mapped = cv2.perspectiveTransform(inner.reshape(1, 4, 2), matrix).reshape(4, 2) / 999.0
    assert np.allclose(mapped[:, 0], [0.1, 0.7, 0.7, 0.1], atol=1e-3)
    assert np.allclose(mapped[:, 1], [0.2, 0.2, 0.95, 0.95], atol=1e-3)

    # And the naive bounding-box answer is genuinely different, so the test
    # would fail if the implementation stopped accounting for perspective.
    assert inner[1][0] != pytest.approx(70.0, abs=1.0)


def test_inset_quad_clamps_absurd_values():
    inner = inset_quad(SQUARE, left=5.0, top=5.0, right=5.0, bottom=5.0)
    assert quad_area(inner) >= 0.0


def test_normalised_roundtrip():
    normalised = quad_to_normalised(SQUARE, 200, 100)
    assert np.allclose(quad_from_normalised(normalised, 200, 100), np.array(SQUARE, np.float32))


def test_homography_maps_quad_onto_rectangle():
    import cv2

    quad = np.array([[12, 20], [180, 8], [190, 110], [4, 96]], dtype=np.float32)
    matrix = homography_to_rect(quad, 64, 36)
    mapped = cv2.perspectiveTransform(order_corners(quad).reshape(1, 4, 2), matrix).reshape(4, 2)
    assert np.allclose(mapped, [[0, 0], [63, 0], [63, 35], [0, 35]], atol=1e-3)


def test_max_corner_shift_and_clip():
    shifted = np.array(SQUARE, dtype=np.float32) + np.array([3, 4], dtype=np.float32)
    assert max_corner_shift(SQUARE, shifted) == pytest.approx(5.0)
    clipped = clip_quad([[-10, -10], [500, 0], [500, 500], [0, 500]], 100, 50)
    assert clipped.min() == 0
    assert clipped[:, 0].max() == 99
    assert clipped[:, 1].max() == 49


def test_full_frame_quad():
    quad = full_frame_quad(640, 360)
    assert quad_aspect_ratio(quad) == pytest.approx(639 / 359, rel=1e-3)
