---
name: color-profiles
description: >-
  Add or extend Screen Sight environment colour-calibration profiles
  (dimensions, options, slot-key migration). Use when the user wants a new
  profile parameter (time of day, lighting, curtains, season, …), a new option
  on an existing parameter, day/night or room-light combos, or asks how
  uncalibrated profile fallback works.
---

# Colour environment profiles

Live `color.*` (matrix, manual WB, gamma, black level) is a **view** of
`color.profiles.selection` + `color.profiles.slots`. The colour stage does not
know about profiles.

Uncalibrated combo → **passthrough** (`white_balance: off`, no matrix, gamma 1).
Do not invent a fallback matrix from a neighbouring combo.

Each slot also stores phone 3A (`camera`: ISO, exposure_ns, focus, awb_gains).
Profile switch restores those via `POST /locks`. While locked, 3A must not hunt;
unlock is manual only. Uncalibrated combos set AF/AE/AWB auto.

## Source of truth

Edit **`processor/utils/color_profiles.py`** `DEFAULT_DIMENSION_SPECS` and
`DEFAULT_SELECTION`. Schema defaults import those factories — do not duplicate
the option list in `schema.py`.

Wizard UI is data-driven from `/api/color/profile` (`dimensions` / `combos`).
Do **not** hardcode day/night or lighting in `app.js`.

## Ids

- `[a-z][a-z0-9_]*` only
- **Never** `on`, `off`, `yes`, `no`, `true`, `false` (YAML booleans). Use
  `lights_off`, not `off`.
- No `.`, `|`, or `=` in ids (those are key separators)

Slot key = schema order: `time_of_day=night|lighting=lights_off`

## Add an option (e.g. `lighting: desk`)

1. Append `{"id": "desk", "label": "Desk lamp"}` to that dimension in
   `DEFAULT_DIMENSION_SPECS`.
2. Existing slot keys are unchanged. New combos start uncalibrated.
3. Add a test that `all_combos` count increased (currently 2×3 = 6).

No key migration. No JS changes.

## Add a dimension (e.g. `curtains`: open / closed)

1. Append a spec to `DEFAULT_DIMENSION_SPECS`.
2. Put a default in `DEFAULT_SELECTION` (the value old calibrations were
   taken under).
3. **Rewrite stored keys** so old slots still match. In a one-off snippet or
   test, use:

```python
from processor.utils.color_profiles import migrate_slots_add_dimension

order = [d["id"] for d in DEFAULT_DIMENSION_SPECS]  # new dim last
migrated = migrate_slots_add_dimension(
    slots,
    dimension_id="curtains",
    default_option="open",
    order=order,
)
```

4. If `config.yaml` already has slots, rewrite those keys the same way
   (`oldkey|curtains=open`) or the user will see passthrough until they
   re-calibrate.
5. Update combo-count tests (N options multiply).

The wizard grows a new `<select>` from the API. Do not special-case the new
axis in the colour stage.

## Do not

- Change `ColorStage` to read profiles
- Auto-fill an uncalibrated combo from another slot
- Commit unless the user asked

## Files

| Change | Path |
|---|---|
| Dimensions / default selection | `processor/utils/color_profiles.py` |
| Slot helpers / bind | same |
| Dataclasses | `processor/config/schema.py` (`ColorProfilesConfig`) |
| Apply / switch | `processor/app.py` `set_color_profile`, `apply_color_calibration` |
| HTTP | `processor/web/server.py` `GET/POST /api/color/profile` |
| Tests | `tests/test_color_profiles.py` |

## Calibrate a combo

Room matching the combo → AE/AWB lock on mid-grey → wizard profile selects →
Colour calibrate → Apply & Save. Repeat for other combos.
