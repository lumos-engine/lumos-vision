"""Processing stages.  Each one is independent and individually optional."""

from processor.stages.blackbars import BlackBarStage
from processor.stages.boundary import BoundaryStage
from processor.stages.color import ColorStage
from processor.stages.crop import CropStage
from processor.stages.movement import MovementStage
from processor.stages.perspective import PerspectiveStage
from processor.stages.reflection import ReflectionStage
from processor.stages.resize import ResizeStage

__all__ = [
    "BlackBarStage",
    "BoundaryStage",
    "ColorStage",
    "CropStage",
    "MovementStage",
    "PerspectiveStage",
    "ReflectionStage",
    "ResizeStage",
]
