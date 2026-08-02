#!/usr/bin/env python3
"""Measure TV detection accuracy against scenes with known ground truth.

Every scene here is a real projection of a 16:9 panel, so the true corners are
known exactly and the error is meaningful rather than eyeballed.  Run it after
touching anything in ``processor/stages/detection.py``::

    python tools/bench_detection.py
    python tools/bench_detection.py --debug 2.39   # dump a diagnostic image
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from processor.config.schema import BoundaryConfig  # noqa: E402
from processor.stages.detection import (  # noqa: E402
    TvQuadDetector,
    auto_canny,
    refine_quad,
)
from processor.testing.generate import POSES  # noqa: E402
from processor.testing.scene import SceneParams, SyntheticScene  # noqa: E402
from processor.utils.geometry import max_corner_shift, quad_side_lengths  # noqa: E402

CASES: list[tuple[str, dict]] = [
    ("2.39", dict(content_aspect=2.39)),
    ("2.35", dict(content_aspect=2.35)),
    ("1.85", dict(content_aspect=1.85)),
    ("16:9", dict(content_aspect=16 / 9)),
    ("4:3", dict(content_aspect=4 / 3)),
    ("steep", dict(quad=POSES["steep"])),
    ("small", dict(quad=POSES["small"])),
    ("offcentre", dict(quad=POSES["offcentre"])),
    ("noisy", dict(noise_sigma=9.0)),
    ("glare", dict(reflection_strength=0.55)),
    ("dark", dict(exposure=0.35, reflection_strength=0.05)),
    ("bright room", dict(exposure=1.4)),
    ("no bezel", dict(bezel_px=1)),
    ("thick bezel", dict(bezel_px=16)),
    ("no subtitles", dict(show_subtitles=False, show_logo=False)),
]

#: Accuracy target, as a fraction of the frame width.
TOLERANCE = 0.025


def build(name: str) -> SyntheticScene:
    overrides = dict(next(kw for n, kw in CASES if n == name))
    return SyntheticScene(SceneParams(shake_px=0.0, **overrides))


def warm(detector: TvQuadDetector, scene: SyntheticScene, frames: int = 30) -> None:
    for i in range(frames):
        detector.observe(scene.frame(i * 0.12))


def run_all() -> int:
    failures = 0
    print(f"{'':4}{'case':<14}{'error':>9}  {'conf':>5}  {'origin':<18}{'aspect':>7}{'edge':>7}")
    for name, _ in CASES:
        scene = build(name)
        detector = TvQuadDetector(BoundaryConfig())
        warm(detector, scene)
        result = detector.detect(scene.frame(4.0))

        if result is None:
            print(f"{'BAD':4}{name:<14}{'no detection':>9}")
            failures += 1
            continue

        error = max_corner_shift(result.quad, scene.true_quad)
        ok = error < TOLERANCE * scene.params.width
        failures += 0 if ok else 1
        print(
            f"{'OK' if ok else 'BAD':4}{name:<14}{error:8.1f}px  {result.confidence:5.3f}  "
            f"{result.origin:<18}{result.parts['aspect']:7.2f}{result.parts['edge_score']:7.2f}"
        )

    total = len(CASES)
    print(f"\n{total - failures}/{total} within {TOLERANCE:.1%} of frame width")
    return failures


def debug_one(name: str, out: Path) -> None:
    """Dump candidates, masks and refinement behaviour for one scene."""
    scene = build(name)
    detector = TvQuadDetector(BoundaryConfig())
    warm(detector, scene)

    frame = scene.frame(4.0)
    gray = detector._to_detect_gray(frame)
    scale = frame.shape[1] / gray.shape[1]
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.dilate(auto_canny(blurred), np.ones((3, 3), np.uint8))
    activity = detector._activity_mask(gray.shape)
    truth = scene.true_quad / scale

    candidates = detector._collect_candidates(blurred, edges, activity)
    scored = [
        s for s in (detector._score(q, gray.shape, activity, edges, o) for q, o in candidates) if s
    ]
    scored.sort(key=lambda s: -s.confidence)

    print(f"scene {name}: {len(candidates)} candidates, {len(scored)} passed the filters")
    for s in scored[:5]:
        print(
            f"  {s.confidence:.3f} {s.origin:<18} err={max_corner_shift(s.quad, truth) * scale:6.1f}px"
            f"  {s.parts}"
        )

    ground = detector._score(truth, gray.shape, activity, edges, "ground-truth")
    print(f"  ground truth scores: {None if ground is None else (round(ground.confidence, 3), ground.parts)}")

    if scored:
        best = scored[0]
        gradient = cv2.magnitude(
            cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3),
            cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3),
        )
        top, right, bottom, left = quad_side_lengths(best.quad)
        span = min((top + bottom) / 2, (left + right) / 2)
        print(f"  refinement (span={span:.0f}px at detect scale):")
        for shift in (6, 10, 14, 20, 28):
            snapped = refine_quad(best.quad, gradient, float(shift))
            if snapped is None:
                print(f"    max_shift={shift:3}  -> rejected")
                continue
            rescored = detector._score(snapped, gray.shape, activity, edges, "refined")
            error = max_corner_shift(snapped, truth) * scale
            conf = "filtered out" if rescored is None else f"{rescored.confidence:.3f}"
            print(f"    max_shift={shift:3}  err={error:6.1f}px  conf={conf}")

    vis = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if activity is not None:
        vis[activity > 0] = (0.6 * vis[activity > 0] + np.array([0, 80, 0])).astype(np.uint8)
    vis[edges > 0] = (70, 70, 255)
    cv2.polylines(vis, [truth.astype(np.int32)], True, (255, 255, 255), 1)
    final = detector.detect(frame)
    if final is not None:
        cv2.polylines(vis, [(final.quad / scale).astype(np.int32)], True, (0, 255, 255), 1)

    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), cv2.resize(vis, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST))
    print(f"  wrote {out}  (white = truth, yellow = detected, green = activity, red = edges)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug", metavar="CASE", help="dump diagnostics for one case")
    parser.add_argument("--out", default="snapshots/detect-debug.png")
    args = parser.parse_args()

    if args.debug:
        debug_one(args.debug, Path(args.out))
        return 0
    return 1 if run_all() else 0


if __name__ == "__main__":
    raise SystemExit(main())
