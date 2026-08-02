"""YAML loading / saving and dotted-path access used by the live tuner."""

from __future__ import annotations

import copy
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from processor.config.schema import Config, ConfigError

DEFAULT_CONFIG_PATHS = (
    Path("config.yaml"),
    Path("config/config.yaml"),
    Path.home() / ".config" / "tv-vision-processor" / "config.yaml",
    Path("/etc/tv-vision-processor/config.yaml"),
)


def find_config() -> Path | None:
    """First existing path from :data:`DEFAULT_CONFIG_PATHS`."""
    env = os.environ.get("TVVP_CONFIG")
    if env:
        candidate = Path(env).expanduser()
        return candidate if candidate.exists() else None
    for path in DEFAULT_CONFIG_PATHS:
        if path.exists():
            return path
    return None


def read_yaml(path: str | os.PathLike) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level of the config must be a mapping")
    return data


def load_config(path: str | os.PathLike | None = None, overrides: dict | None = None) -> Config:
    """Build a :class:`Config` from an optional YAML file plus overrides."""
    data: dict[str, Any] = {}
    if path is not None:
        data = read_yaml(path)
    if overrides:
        data = deep_merge(data, overrides)
    return Config.from_dict(data)


def config_to_dict(config: Config) -> dict[str, Any]:
    """Plain nested dict, ready for YAML or JSON."""
    return asdict(config)


def save_config(config: Config | dict, path: str | os.PathLike) -> Path:
    """Write the config to YAML, atomically, creating parents as needed."""
    data = config_to_dict(config) if isinstance(config, Config) else config
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(
            "# TV Vision Processor configuration\n"
            "# Written by the calibration wizard; hand edits are fine too.\n"
        )
        yaml.safe_dump(data, handle, sort_keys=False, default_flow_style=False, indent=2)
    os.replace(tmp, target)
    return target


def deep_merge(base: dict, updates: dict) -> dict:
    """Recursively merge ``updates`` into a copy of ``base``."""
    result = copy.deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def dotted_get(data: dict, path: str, default: Any = None) -> Any:
    """``dotted_get(cfg, "color.gamma")``."""
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def dotted_set(data: dict, path: str, value: Any) -> dict:
    """Set a nested key in place, creating intermediate dicts."""
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value
    return data


def apply_updates(config: Config, updates: dict[str, Any]) -> Config:
    """Return a new Config with dotted-path ``updates`` applied.

    Validation happens by rebuilding the whole dataclass tree, so a bad value
    from the web UI raises before it can reach the pipeline.
    """
    data = config_to_dict(config)
    for path, value in updates.items():
        dotted_set(data, path, value)
    return Config.from_dict(data)
