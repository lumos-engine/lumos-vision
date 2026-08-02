"""Synthetic scene generation for development and tests.

Lives inside the package rather than in ``tests/`` so the ``synthetic`` camera
source and the sample-media tools can both use it.
"""

from processor.testing.scene import SceneParams, SyntheticScene, render_panel

__all__ = ["SceneParams", "SyntheticScene", "render_panel"]
