"""Environment colour profiles (day/night × room lights, extensible).

Live ``color.*`` correction is a *view* of the active slot. The source of
truth is ``color.profiles.selection`` plus ``color.profiles.slots``. A combo
with no calibrated slot is passthrough (no matrix, no manual WB, gamma 1).

Slot keys are ``dim=option|dim=option`` in schema order so a future dimension
can be appended without renaming the separators. Ids must be ``[a-z][a-z0-9_]*``
and must not be YAML booleans (``on`` / ``off`` / ``yes`` / ``no`` / ``true`` /
``false``).
"""

from __future__ import annotations

import re
from dataclasses import asdict
from itertools import product
from typing import Any, Iterable, Mapping, Sequence

from processor.config.schema import (
    BlackLevelConfig,
    ColorConfig,
    ColorProfileSlot,
    ColorProfilesConfig,
    Config,
    ConfigError,
    GainsConfig,
    ProfileDimension,
    ProfileOption,
)
from processor.utils.color_calibrate import IDENTITY_MATRIX_FLAT, iso_now

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_YAML_BOOLEAN_WORDS = frozenset({"on", "off", "yes", "no", "true", "false"})
_KEY_JOIN = "|"
_KV_JOIN = "="

DEFAULT_DIMENSION_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "time_of_day",
        "label": "Day / night",
        "options": (
            {"id": "day", "label": "Day"},
            {"id": "night", "label": "Night"},
        ),
    },
    {
        "id": "lighting",
        "label": "Room lights",
        "options": (
            {"id": "large", "label": "Large light on"},
            {"id": "bed", "label": "Bed light on"},
            {"id": "lights_off", "label": "Lights off"},
        ),
    },
)

DEFAULT_SELECTION: dict[str, str] = {
    "time_of_day": "night",
    "lighting": "lights_off",
}


def default_profile_dimensions() -> list[ProfileDimension]:
    return [
        ProfileDimension(
            id=str(spec["id"]),
            label=str(spec["label"]),
            options=[
                ProfileOption(id=str(opt["id"]), label=str(opt["label"]))
                for opt in spec["options"]
            ],
        )
        for spec in DEFAULT_DIMENSION_SPECS
    ]


def default_profile_selection() -> dict[str, str]:
    return dict(DEFAULT_SELECTION)


def validate_profile_id(value: str, *, path: str) -> str:
    text = str(value or "").strip()
    if not _ID_RE.match(text):
        raise ConfigError(
            f"{path}: id must match [a-z][a-z0-9_]* (got {value!r})"
        )
    if text in _YAML_BOOLEAN_WORDS:
        raise ConfigError(
            f"{path}: {text!r} is a YAML boolean word — use lights_off, not off"
        )
    return text


def dimension_options(dim: ProfileDimension) -> list[ProfileOption]:
    return list(dim.options or [])


def option_ids(dim: ProfileDimension) -> list[str]:
    return [opt.id for opt in dimension_options(dim)]


def resolve_selection(
    profiles: ColorProfilesConfig,
    selection: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a complete selection, filling missing dims with their first option."""
    raw = dict(profiles.selection)
    if selection:
        raw.update({str(k): str(v) for k, v in selection.items()})
    resolved: dict[str, str] = {}
    for dim in profiles.dimensions:
        dim_id = validate_profile_id(dim.id, path="color.profiles.dimensions.id")
        ids = option_ids(dim)
        if not ids:
            raise ConfigError(f"color.profiles.dimensions.{dim_id}: needs at least one option")
        for opt in dim.options:
            validate_profile_id(opt.id, path=f"color.profiles.dimensions.{dim_id}.options")
        chosen = str(raw.get(dim_id) or "").strip()
        if not chosen:
            chosen = ids[0]
        if chosen not in ids:
            raise ConfigError(
                f"color.profiles.selection.{dim_id}: unknown option {chosen!r} "
                f"(want one of {', '.join(ids)})"
            )
        resolved[dim_id] = chosen
    return resolved


def slot_key(
    profiles: ColorProfilesConfig,
    selection: Mapping[str, str] | None = None,
) -> str:
    resolved = resolve_selection(profiles, selection)
    return _KEY_JOIN.join(
        f"{dim.id}{_KV_JOIN}{resolved[dim.id]}" for dim in profiles.dimensions
    )


def parse_slot_key(key: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    text = str(key or "").strip()
    if not text:
        return parts
    for chunk in text.split(_KEY_JOIN):
        if _KV_JOIN not in chunk:
            continue
        dim, option = chunk.split(_KV_JOIN, 1)
        parts[dim.strip()] = option.strip()
    return parts


def rewrite_slot_key(key: str, extra: Mapping[str, str], *, order: Sequence[str]) -> str:
    """Append/overwrite dimensions and emit a key in ``order``."""
    parsed = parse_slot_key(key)
    parsed.update({str(k): str(v) for k, v in extra.items()})
    return _KEY_JOIN.join(f"{dim}{_KV_JOIN}{parsed[dim]}" for dim in order if dim in parsed)


def selection_label(
    profiles: ColorProfilesConfig,
    selection: Mapping[str, str] | None = None,
) -> str:
    resolved = resolve_selection(profiles, selection)
    labels: list[str] = []
    for dim in profiles.dimensions:
        chosen = resolved[dim.id]
        opt = next((o for o in dim.options if o.id == chosen), None)
        labels.append(opt.label if opt and opt.label else chosen)
    return " · ".join(labels)


def slot_is_calibrated(slot: ColorProfileSlot | None) -> bool:
    if slot is None:
        return False
    if str(slot.calibrated_at or "").strip():
        return True
    return bool(slot.matrix_enabled)


def lookup_slot(
    profiles: ColorProfilesConfig,
    selection: Mapping[str, str] | None = None,
) -> ColorProfileSlot | None:
    key = slot_key(profiles, selection)
    return profiles.slots.get(key)


def empty_slot() -> ColorProfileSlot:
    return ColorProfileSlot()


def slot_from_live(color: ColorConfig, *, calibrated_at: str | None = None) -> ColorProfileSlot:
    stamp = calibrated_at
    if stamp is None:
        stamp = str(color.calibration.calibrated_at or "").strip() or iso_now()
    return ColorProfileSlot(
        calibrated_at=stamp,
        white_balance=str(color.white_balance or "off"),
        gains=GainsConfig(r=color.gains.r, g=color.gains.g, b=color.gains.b),
        matrix_enabled=bool(color.matrix_enabled),
        matrix=list(color.matrix or IDENTITY_MATRIX_FLAT),
        black_level_enabled=bool(color.black_level_enabled),
        black_level=BlackLevelConfig(
            r=color.black_level.r, g=color.black_level.g, b=color.black_level.b
        ),
        gamma=float(color.gamma),
        saturation=float(color.saturation),
        notes=list(color.calibration.notes),
        patch_means_bgr={
            name: [float(v) for v in mean]
            for name, mean in (color.calibration.patch_means_bgr or {}).items()
        },
    )


def slot_from_solution(solution: Any) -> ColorProfileSlot:
    payload = solution.as_dict()
    gains = payload.get("gains") or {}
    black = payload.get("black_level") or {}
    return ColorProfileSlot(
        calibrated_at=iso_now(),
        white_balance="manual",
        gains=GainsConfig(
            r=float(gains.get("r", 1.0)),
            g=float(gains.get("g", 1.0)),
            b=float(gains.get("b", 1.0)),
        ),
        matrix_enabled=True,
        matrix=[float(v) for v in payload.get("matrix") or IDENTITY_MATRIX_FLAT],
        black_level_enabled=bool(payload.get("black_level_enabled")),
        black_level=BlackLevelConfig(
            r=float(black.get("r", 0.0)),
            g=float(black.get("g", 0.0)),
            b=float(black.get("b", 0.0)),
        ),
        gamma=float(payload.get("gamma", 1.0)),
        saturation=1.0,
        notes=list(payload.get("notes") or []),
        patch_means_bgr={
            name: [float(v) for v in mean]
            for name, mean in (payload.get("patch_means_bgr") or {}).items()
        },
    )


def slot_as_dict(slot: ColorProfileSlot) -> dict[str, Any]:
    return asdict(slot)


def bypass_live_updates() -> dict[str, Any]:
    """Passthrough: no matrix, no manual WB, gamma 1. Software AE off."""
    return {
        "color.white_balance": "off",
        "color.gains.r": 1.0,
        "color.gains.g": 1.0,
        "color.gains.b": 1.0,
        "color.matrix_enabled": False,
        "color.matrix": list(IDENTITY_MATRIX_FLAT),
        "color.black_level_enabled": False,
        "color.black_level.r": 0.0,
        "color.black_level.g": 0.0,
        "color.black_level.b": 0.0,
        "color.gamma": 1.0,
        "color.exposure.enabled": False,
        "color.calibration.calibrated_at": "",
        "color.calibration.patch_means_bgr": {},
        "color.calibration.matrix": list(IDENTITY_MATRIX_FLAT),
        "color.calibration.black_level.r": 0.0,
        "color.calibration.black_level.g": 0.0,
        "color.calibration.black_level.b": 0.0,
        "color.calibration.notes": [],
    }


def live_updates_from_slot(slot: ColorProfileSlot | None) -> dict[str, Any]:
    if not slot_is_calibrated(slot) or slot is None:
        return bypass_live_updates()
    return {
        "color.white_balance": slot.white_balance or "manual",
        "color.gains.r": float(slot.gains.r),
        "color.gains.g": float(slot.gains.g),
        "color.gains.b": float(slot.gains.b),
        "color.matrix_enabled": bool(slot.matrix_enabled),
        "color.matrix": list(slot.matrix or IDENTITY_MATRIX_FLAT),
        "color.black_level_enabled": bool(slot.black_level_enabled),
        "color.black_level.r": float(slot.black_level.r),
        "color.black_level.g": float(slot.black_level.g),
        "color.black_level.b": float(slot.black_level.b),
        "color.gamma": float(slot.gamma),
        "color.saturation": float(slot.saturation),
        "color.exposure.enabled": False,
        "color.calibration.calibrated_at": str(slot.calibrated_at or ""),
        "color.calibration.patch_means_bgr": {
            name: [float(v) for v in mean]
            for name, mean in (slot.patch_means_bgr or {}).items()
        },
        "color.calibration.matrix": list(slot.matrix or IDENTITY_MATRIX_FLAT),
        "color.calibration.black_level.r": float(slot.black_level.r),
        "color.calibration.black_level.g": float(slot.black_level.g),
        "color.calibration.black_level.b": float(slot.black_level.b),
        "color.calibration.notes": list(slot.notes),
    }


def live_looks_calibrated(color: ColorConfig) -> bool:
    if color.matrix_enabled:
        return True
    if str(color.white_balance or "off").lower() == "manual":
        return True
    return bool(str(color.calibration.calibrated_at or "").strip())


def absorb_legacy_calibration(config: Config) -> Config:
    """Copy a pre-profile live matrix into the current slot so upgrades keep it."""
    from processor.config.loader import apply_updates

    color = config.color
    if any(slot_is_calibrated(slot) for slot in color.profiles.slots.values()):
        return config
    if not live_looks_calibrated(color):
        return config
    key = slot_key(color.profiles)
    slot = slot_from_live(color)
    return apply_updates(config, {f"color.profiles.slots.{key}": slot_as_dict(slot)})


def bind_config(config: Config) -> Config:
    """Normalize selection, absorb a legacy matrix, then apply the active slot.

    Uncalibrated combos stay passthrough. Taste sliders (gamma without a
    matrix) are left alone when nothing is calibrated yet.
    """
    from processor.config.loader import apply_updates

    color = config.color
    resolved = resolve_selection(color.profiles)
    if dict(color.profiles.selection) != resolved:
        config = apply_updates(config, {"color.profiles.selection": resolved})
    config = absorb_legacy_calibration(config)
    slot = lookup_slot(config.color.profiles)
    if slot_is_calibrated(slot):
        return apply_updates(config, live_updates_from_slot(slot))
    if live_looks_calibrated(config.color):
        return apply_updates(config, bypass_live_updates())
    return config


def all_combos(profiles: ColorProfilesConfig) -> list[dict[str, str]]:
    dims = list(profiles.dimensions)
    if not dims:
        return [{}]
    id_lists = [option_ids(dim) for dim in dims]
    combos: list[dict[str, str]] = []
    for values in product(*id_lists):
        combos.append({dim.id: value for dim, value in zip(dims, values)})
    return combos


def profile_status(color: ColorConfig) -> dict[str, Any]:
    profiles = color.profiles
    resolved = resolve_selection(profiles)
    key = slot_key(profiles, resolved)
    slot = lookup_slot(profiles, resolved)
    calibrated = slot_is_calibrated(slot)
    combos = []
    for combo in all_combos(profiles):
        combo_key = slot_key(profiles, combo)
        combos.append(
            {
                "key": combo_key,
                "selection": combo,
                "label": selection_label(profiles, combo),
                "calibrated": slot_is_calibrated(profiles.slots.get(combo_key)),
                "active": combo_key == key,
            }
        )
    return {
        "key": key,
        "label": selection_label(profiles, resolved),
        "selection": resolved,
        "calibrated": calibrated,
        "mode": "calibrated" if calibrated else "none",
        "dimensions": [
            {
                "id": dim.id,
                "label": dim.label,
                "options": [{"id": opt.id, "label": opt.label} for opt in dim.options],
            }
            for dim in profiles.dimensions
        ],
        "combos": combos,
        "calibrated_count": sum(1 for item in combos if item["calibrated"]),
        "combo_count": len(combos),
    }


def selection_updates(selection: Mapping[str, str]) -> dict[str, Any]:
    return {f"color.profiles.selection.{key}": str(value) for key, value in selection.items()}


def store_slot_updates(key: str, slot: ColorProfileSlot) -> dict[str, Any]:
    return {f"color.profiles.slots.{key}": slot_as_dict(slot)}


def profiles_touch_selection(update_keys: Iterable[str]) -> bool:
    for key in update_keys:
        if key == "color.profiles.selection" or key.startswith("color.profiles.selection."):
            return True
        if key == "color.profiles.slots" or key.startswith("color.profiles.slots."):
            return True
        if key == "color.profiles.dimensions" or key.startswith("color.profiles.dimensions"):
            return True
    return False


def migrate_slots_add_dimension(
    slots: Mapping[str, ColorProfileSlot],
    *,
    dimension_id: str,
    default_option: str,
    order: Sequence[str],
) -> dict[str, ColorProfileSlot]:
    """Rewrite keys after appending a dimension (old combos get ``default_option``)."""
    extra = {dimension_id: default_option}
    migrated: dict[str, ColorProfileSlot] = {}
    for key, slot in slots.items():
        migrated[rewrite_slot_key(key, extra, order=order)] = slot
    return migrated


def migrate_slots_add_option(slots: Mapping[str, ColorProfileSlot]) -> dict[str, ColorProfileSlot]:
    """Adding an option does not rewrite keys; existing slots stay put."""
    return dict(slots)
