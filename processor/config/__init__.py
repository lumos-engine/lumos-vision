"""Configuration schema and YAML loading."""

from processor.config.schema import (
    BlackBarsConfig,
    BoundaryConfig,
    CameraConfig,
    ColorConfig,
    Config,
    CropConfig,
    DebugConfig,
    MovementConfig,
    OutputConfig,
    PerspectiveConfig,
    PipelineConfig,
    ReflectionConfig,
    WebConfig,
)
from processor.config.loader import (
    DEFAULT_CONFIG_PATHS,
    config_to_dict,
    deep_merge,
    dotted_get,
    dotted_set,
    load_config,
    save_config,
)

__all__ = [
    "BlackBarsConfig",
    "BoundaryConfig",
    "CameraConfig",
    "ColorConfig",
    "Config",
    "CropConfig",
    "DebugConfig",
    "MovementConfig",
    "OutputConfig",
    "PerspectiveConfig",
    "PipelineConfig",
    "ReflectionConfig",
    "WebConfig",
    "DEFAULT_CONFIG_PATHS",
    "config_to_dict",
    "deep_merge",
    "dotted_get",
    "dotted_set",
    "load_config",
    "save_config",
]
