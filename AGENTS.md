# Screen Sight — Agent Guidelines

Python app that turns an RTSP camera pointed at a TV into a rectified, cropped,
low-latency virtual webcam for ambient lighting (HyperHDR today; DDP/WLED later).
Product name: **Screen Sight**. Package directory stays `processor/`. CLI:
`screensight` or `python -m processor`.

The processor must **not** depend on HyperHDR. It produces frames; sinks are
pluggable.

## Git — never commit unless asked

**Do not ever create commits, amend commits, or push on the user's behalf.**

- Never run `git commit`, `git commit --amend`, or `git push` unless the user
  explicitly asks for that action in the current message.
- Finishing a feature, syncing to another machine, or “the natural next step”
  is **not** permission to commit or push. Leave changes uncommitted and tell
  the user what to run if they want to publish.
- Staging (`git add`) for a commit the user did not request is also forbidden.

## Layout

```
processor/
  camera/       # RTSP, V4L2/USB (drop-old), file, image, synthetic
  pipeline/     # Stage ABC, FrameContext, PipelineState, registry
  stages/       # movement → boundary → perspective → crop → blackbars → …
  output/       # V4L2, MJPEG, file, null, DDP
  config/       # typed YAML schema + loader (dotted live updates)
  web/          # calibration wizard (localhost:7660)
  debug/        # OpenCV viewer
  testing/      # synthetic living-room scene + sample generator
  cli.py, app.py
tests/          # pytest; prefer synthetic sources, no hardware
tools/          # detection bench, etc.
packaging/      # systemd unit (screen-sight.service)
config.example.yaml
```

## Architecture rules

- **Stages are independent.** They only share data via `PipelineState` /
  `FrameContext.meta`. Never call another stage from a stage.
- **One pipeline thread.** Mutations from the web UI / CLI go through the
  command queue in `processor/app.py` and run between frames. Do not touch
  stages from other threads. Commands only execute while `_loop_active` is
  true (loop draining the queue) — not merely after `start()`.
- **New stage = class + `register_stage`.** Wire it in
  `processor/pipeline/registry.py`; then it is usable from `pipeline.stages`
  in YAML. Nothing else should need hardcoding.
- **Outputs are sinks.** Prefer adding a sink over teaching the pipeline about
  a consumer.

## Hard-won algorithm decisions (do not quietly reverse)

These were measured and rejected/chosen for concrete reasons. The module
docstrings (especially `movement.py`, `detection.py`, `blackbars.py`) are the
source of truth — read them before "improving" the algorithm.

| Area           | Keep                                                                    | Do not reintroduce                                                           |
| -------------- | ----------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Camera bump    | Masked **NCC** of the static region outside the TV                      | Phase correlation (content pans look like motion); ORB (no texture on walls) |
| TV find        | **Activity mask** (TRIANGLE threshold) chooses; edges/brightness refine | Locking onto letterboxed picture without growing to a 16:9 panel             |
| Aspect scoring | Recover true rectangle aspect (Zhang & He)                              | Scoring on-screen quad aspect (perspective lies)                             |
| Black bars     | Compare-and-sum + median → hysteresis → rate limit                      | Per-row percentile (same math, ~20× slower); raw jittery crops               |
| Synthetic TV   | Real pinhole projection (`tv_quad`)                                     | Fake axis-aligned "TV" rectangles for detection tests                        |

Temporal filters for anything that drives LEDs: median window → hysteresis →
rate limit. Ambient lighting amplifies 1–2 px wobble.

## Config & secrets

- Schema: `processor/config/schema.py`. Loader search order and
  `SCREENSIGHT_CONFIG` are in `processor/config/loader.py`.
- YAML: bare `off` becomes boolean `False`. Quote modes/strings that must stay
  strings (e.g. `"off"`).
- Never hardcode RTSP credentials. Never log full RTSP URLs; redact passwords
  everywhere the web UI or logs can see them.
- CP PLUS RTSP paths (`/live/channel10`, `/live/channel1`) are stable; **encoder
  resolution is not**. Always verify with `ffprobe` before calling a channel
  “main/high-res”. Both have been seen at 640×360; channel10 was previously
  also 2304×1296. Do not bake a fixed resolution into docs or code.
- Paths/branding: `screen-sight`, env `SCREENSIGHT_CONFIG`, V4L2
  `card_label="Screen Sight"`, service `packaging/screen-sight.service`.

## Dev commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[dev]"

# no camera / no v4l2loopback
python -m processor run --source synthetic --no-v4l2 --mjpeg

# USB webcam (not the loopback output device)
python -m processor run --source v4l2 --camera-device /dev/video2 --web --mjpeg
# Prefer stable paths: ls -l /dev/v4l/by-id/  →  camera.device: /dev/v4l/by-id/...
# Wizard "Source" panel lists USB cameras and can switch RTSP/file/synthetic live
# (POST /api/camera/source). Remount note for loopback output is unchanged.
# Hardware exposure/gain/WB: wizard "Camera hardware" section, or
#   v4l2-ctl -d /dev/video2 --list-ctrls
# Software Colour sliders only post-process; use UVC controls for real exposure.
# Defaults are 1280x720 with process_width=0 (no early downscale). Downscale in
# HyperHDR if needed; only lower Screen Sight res on a slow host.

# calibration wizard
python -m processor run --source synthetic --no-v4l2 --web

# tests (default; run after pipeline/detection/config changes)
pytest -q

# detection accuracy vs synthetic ground truth
python tools/bench_detection.py
```

V4L2 loopback is **Linux-only**. On macOS, develop with `--no-v4l2` and
synthetic/file sources; do not assume ioctl paths were exercised end-to-end.

## Coding conventions

- Match existing style: dataclasses for config, ABC stages, small focused
  modules, module docstrings that record _why_.
- Prefer clear names over comments. When a comment is needed, explain a
  non-obvious constraint or a rejected alternative — not what the next line
  does.
- Keep stages optional and cheap when idle. Prefer frame-based throttles for
  detection/movement checks over per-frame heavy work.
- Tests: use `SyntheticScene` / fixtures in `tests/conftest.py`. Avoid network
  and `/dev/video*` unless gated and clearly optional.
- Do not expand scope into HyperHDR integration, ESP32 firmware, or unrelated
  features beyond the minimal `power.hyperhdr_url` LEDDEVICE on/off used for
  TV-presence idle (see `processor/utils/hyperhdr_leds.py`).
  refactors.

## When changing detection or movement

1. Read the existing stage docstring and related tests.
2. Reproduce with `SyntheticScene` / `tools/bench_detection.py` before claiming
   improvement.
3. Keep ground-truth corners honest (pinhole projection).
4. Update tests and the bench expectation narrative in README only if behaviour
   intentionally changes.

## Branding checklist

If renaming or adding user-visible strings: README, `pyproject.toml`, CLI
`prog`, web `index.html`, MJPEG/V4L2 labels, systemd unit, config search paths,
and env var — stay consistent with **Screen Sight** / `screensight` /
`screen-sight`.
