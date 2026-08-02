"""Shared fixtures."""

from __future__ import annotations

import numpy as np
import pytest

from processor.config.schema import Config
from processor.testing.scene import SceneParams, SyntheticScene


@pytest.fixture
def scene() -> SyntheticScene:
    """A still (no shake) synthetic living room with 2.39:1 content."""
    return SyntheticScene(SceneParams(shake_px=0.0, content_aspect=2.39))


@pytest.fixture
def frame(scene: SyntheticScene) -> np.ndarray:
    return scene.frame(3.0)


@pytest.fixture
def config() -> Config:
    """Defaults, minus everything that touches hardware or the network."""
    return Config.from_dict(
        {
            "camera": {"source": "synthetic", "replay_fps": 30},
            "output": {"width": 640, "height": 360, "fps": 15, "v4l2": {"enabled": False}},
            "boundary": {"mode": "auto"},
            "logging": {"stats_interval": 0},
        }
    )
