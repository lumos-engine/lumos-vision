"""Stage interface.

A stage is an independent, optional transformation.  It reads
``ctx.image``, optionally replaces it, and records structured results in
``ctx.meta``.  Stages never call each other; anything shared goes through
:class:`~processor.pipeline.context.PipelineState`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from processor.pipeline.context import FrameContext, PipelineState


class Stage(ABC):
    #: Unique name; also the config section and the debug view key.
    name: str = "stage"

    def __init__(self, config: Any, state: PipelineState):
        self.config = config
        self.state = state
        self._enabled = bool(getattr(config, "enabled", True))

    # -- enable / disable --------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        value = bool(value)
        if value != self._enabled:
            self._enabled = value
            self.on_toggle(value)

    def toggle(self) -> bool:
        self.enabled = not self._enabled
        return self._enabled

    def on_toggle(self, enabled: bool) -> None:
        """Hook for stages that must drop temporal state when switched off."""
        self.reset()

    # -- main entry point --------------------------------------------------

    @abstractmethod
    def process(self, ctx: FrameContext) -> None:
        """Transform ``ctx`` in place."""

    def reset(self) -> None:
        """Drop all temporal state (called on recalibration and reconfig)."""

    def apply_config(self, config: Any) -> None:
        """Swap in a new config section while running."""
        self.config = config
        self._enabled = bool(getattr(config, "enabled", True))
        self.on_config_changed()

    def on_config_changed(self) -> None:
        """Hook for stages that cache derived values (LUTs, kernels...)."""

    # -- introspection -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Small JSON-friendly summary for the web UI."""
        return {"enabled": self._enabled}

    def debug_view(self, ctx: FrameContext) -> np.ndarray | None:
        """An annotated image explaining what this stage did, if useful."""
        return ctx.debug_images.get(self.name)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} enabled={self._enabled}>"
