"""Generate sample media so the pipeline can be exercised without a camera.

Produces a set of stills covering the awkward cases (each aspect ratio, a dark
scene, a bumped camera, a heavy reflection) plus a video whose content aspect
ratio changes part-way through, which is what the black-bar stabiliser is
really being tested against.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2

from processor.testing.scene import SceneParams, SyntheticScene, tv_quad
from processor.utils.logging import get_logger

log = get_logger(__name__)

#: Camera poses used by the samples and by the detection tests.  All of them
#: are genuine projections of a 16:9 panel, so ground truth is exact.
POSES: dict[str, list[list[float]]] = {
    "front": tv_quad(),
    "steep": tv_quad(yaw=-22, pitch=8, distance=2.7, offset=(0.30, -0.10)),
    #: Shelf-beside-sofa, nearly side-on — left edge much taller than right.
    "side_on": tv_quad(yaw=-45, pitch=10, distance=2.3, offset=(0.38, -0.06), fov=48),
    "offcentre": tv_quad(yaw=14, pitch=-5, distance=2.7, offset=(-0.26, 0.08)),
    "small": tv_quad(distance=4.2, diagonal=1.0, yaw=5, pitch=-3),
}

STILLS: dict[str, dict] = {
    "01-cinemascope-2.39": {"content_aspect": 2.39},
    "02-scope-2.35": {"content_aspect": 2.35},
    "03-widescreen-1.85": {"content_aspect": 1.85},
    "04-hd-16x9": {"content_aspect": 16 / 9},
    "05-academy-4x3": {"content_aspect": 4 / 3},
    "06-strong-reflection": {"content_aspect": 2.39, "reflection_strength": 0.55},
    "07-dark-scene": {"content_aspect": 2.39, "exposure": 0.35, "reflection_strength": 0.05},
    "08-steep-angle": {"content_aspect": 2.39, "quad": POSES["steep"]},
    "09-small-tv": {"content_aspect": 16 / 9, "quad": POSES["small"]},
    "10-noisy-camera": {"content_aspect": 2.39, "noise_sigma": 9.0},
    "11-off-centre": {"content_aspect": 1.85, "quad": POSES["offcentre"]},
    "12-bright-room": {"content_aspect": 2.39, "exposure": 1.4},
    "13-side-on": {"content_aspect": 16 / 9, "quad": POSES["side_on"]},
    "14-side-on-dim": {
        "content_aspect": 16 / 9,
        "quad": POSES["side_on"],
        "bezel_px": 3,
        "exposure": 0.55,
        "reflection_strength": 0.08,
    },
}


def generate_stills(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    truth: dict[str, dict] = {}

    for name, overrides in STILLS.items():
        params = SceneParams(shake_px=0.0, **overrides)
        scene = SyntheticScene(params)
        image = scene.frame(3.0)
        path = out_dir / f"{name}.png"
        cv2.imwrite(str(path), image)
        written.append(path)

        quad = scene.true_quad
        truth[path.name] = {
            "quad_px": [[float(x), float(y)] for x, y in quad],
            "quad_normalised": [
                [float(x) / params.width, float(y) / params.height] for x, y in quad
            ],
            "content_aspect": float(params.content_aspect),
        }

    # Ground truth alongside the images, so accuracy can be measured rather
    # than eyeballed.
    truth_path = out_dir / "ground-truth.json"
    truth_path.write_text(json.dumps(truth, indent=2), encoding="utf-8")
    written.append(truth_path)
    return written


def generate_video(out_dir: Path, seconds: float = 20.0, fps: float = 15.0) -> Path:
    """A clip that changes aspect ratio and gets bumped, in one take."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "livingroom.mp4"

    params = SceneParams(content_aspect=2.39, bump_at=seconds * 0.7)
    scene = SyntheticScene(params)
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (params.width, params.height)
    )
    if not writer.isOpened():
        raise RuntimeError(f"could not open {path} for writing")

    total = int(seconds * fps)
    try:
        for i in range(total):
            t = i / fps
            # Switch to 16:9 a third of the way in, and to 1.85:1 later, so the
            # crop has to travel both directions during one clip.
            if t < seconds * 0.35:
                scene.params.content_aspect = 2.39
            elif t < seconds * 0.65:
                scene.params.content_aspect = 16 / 9
            else:
                scene.params.content_aspect = 1.85
            writer.write(scene.frame(t))
    finally:
        writer.release()

    log.info("Wrote %d frames to %s", total, path)
    return path


def generate_samples(out_dir: Path, seconds: float = 20.0, fps: float = 15.0) -> list[Path]:
    written = generate_stills(out_dir / "stills")
    written.append(generate_video(out_dir, seconds=seconds, fps=fps))
    return written
