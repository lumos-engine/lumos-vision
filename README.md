# Screen Sight

Takes a camera pointed at a television (USB webcam or RTSP IP cam) and turns
it into a clean, rectified, cropped, low-latency virtual webcam for ambient
lighting.

```
USB or RTSP camera ─▶ Screen Sight ─▶ /dev/video10 ─▶ HyperHDR ─▶ ESP32 + WLED
```

The processor knows nothing about HyperHDR. It produces frames and hands them
to pluggable output sinks; HyperHDR is simply the one reading the virtual
camera today. A DDP sink that drives WLED directly is already in the box, so
HyperHDR can be removed from the chain later without touching the pipeline.

**What it actually does:** finds the TV in the camera's view, removes the
perspective so the screen becomes a true rectangle, trims the bezel, detects
and removes letterbox bars as the aspect ratio changes mid-film, ignores
reflections near the edges, and normalises the colour — by default at
1280×720 so cinema letterboxing stays sharp for HyperHDR (downscale there if
the host needs it).

---

## Quick start (USB webcam)

Recommended path on Ubuntu: a UVC webcam (e.g. Logitech) on the same machine.

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip v4l2loopback-dkms v4l-utils

git clone <this repo> screen-sight && cd screen-sight
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .

# Virtual camera HyperHDR will read (once per boot unless you make it persistent)
sudo modprobe v4l2loopback video_nr=10 card_label="Screen Sight" exclusive_caps=1

# Find the Logitech node — do NOT use /dev/video10 (that is the loopback output)
v4l2-ctl --list-devices
# Prefer stable IDs (videoN numbers move when USB devices are replugged):
ls -l /dev/v4l/by-id/
```

Run (one line — avoid spaces after `\` if you split lines):

```bash
python -m processor run --source v4l2 --camera-device /dev/video2 --capture-width 1280 --capture-height 720 --web --web-host 0.0.0.0 --mjpeg
```

In `config.yaml`, prefer a by-id path, for example
`device: /dev/v4l/by-id/usb-…-video-index0`.

Defaults keep full detail: no `process_width` downscale, 1280×720 working size
and virtual-cam output. On a slow machine, pass `--width 640 --height 360`
and/or `--process-width 960`.

Then open:

- Calibration wizard: <http://localhost:7660> (or `http://PC_IP:7660` from another machine)
- MJPEG preview: <http://localhost:7661>

In the wizard, use the **Source** panel to pick USB / RTSP / file / synthetic
(Apply switches live; Apply & Save writes `config.yaml`). For USB, choose a
camera from the by-id list. Then use **Camera hardware** (exposure / gain /
white balance on the UVC sensor). The **Colour (software)** sliders only
post-process frames after capture.

For an **Android phone camera over USB**, prefer **Lumos Cam** (sibling repo
`lumos-cam`, needs app ≥ 0.1). In the wizard set **Input type** to Lumos Cam,
or `camera.source: lumos` in YAML. Screen Sight launches the APK over adb,
forwards control/video ports, and decodes H.264 through an ffmpeg pipe (no
`/dev/video11`). Zoom, pan, and AF/AE/AWB locks are live (no process restart).
Colour-cal **Start** turns on cal mode; **Abort** / **Apply** restore auto.
Scrcpy remains a fallback (`camera.source: scrcpy`) if the APK is not
installed; that path still needs a v4l2loopback node (see
`config.example.yaml`) and the protocol in
[docs/lumos-cam-api.md](docs/lumos-cam-api.md).

For **cinema / letterboxed** content: enable **Black bars**, raise **Darkness
threshold** to ~50–70 until **Result → output** has no bars, then point
HyperHDR at that clean frame.

**Colour calibrate** (occasional, HDMI TV): mark corners first, open
`/calibrate/display` fullscreen on the HDMI output, keep the wizard on your
other display, then **Colour calibrate → Start**. Screen Sight cycles black,
white, three greys, and R/G/B, measures the panel centre, and proposes manual
RGB gains + gamma. Use **Apply & Save** to keep them in `config.yaml`.

If ports are busy from a previous run:

```bash
pkill -f 'python -m processor'
ss -tlnp | grep -E '7660|7661' || echo "ports free"
```

### Idle when the TV is off (optional)

Set `power.tv_host` (e.g. `192.168.1.244`) in `config.yaml`. By default the
TV is probed every 15s and idle starts after 5 consecutive failed pings
(~75s offline). Idle releases the USB camera, skips the pipeline, keeps
writing black frames to `/dev/video10` (so HyperHDR does not need a restart),
and disables HyperHDR’s `LEDDEVICE` via `power.hyperhdr_url` for true LED
power-off. Resume needs `success_pings` successes (default 1). Leave
`tv_host` empty to disable. Leave `hyperhdr_url` empty for camera-only idle
without touching LEDs.

### No camera (dev / CI)

```bash
python -m processor run --source synthetic --no-v4l2 --mjpeg
```

### RTSP IP camera (optional)

```bash
python -m processor run --rtsp-url 'rtsp://admin:PASSWORD@192.168.1.93:5543/live/channel10' --web --web-host 0.0.0.0 --mjpeg
```

Credentials are never logged in full. On CP PLUS and similar cams, RTSP **paths**
are stable but **encoder resolution is not** — verify with `ffprobe` before
assuming a channel is high-res.

---

## Installing on Ubuntu 24.04

See [Quick start (USB webcam)](#quick-start-usb-webcam) for the full install.
`pip install -e .` also provides the `screensight` command.

X11 is more reliable than Wayland for some OpenCV / desktop capture paths; if
the machine misbehaves under Wayland, switch the session to Xorg.

### The virtual camera (HyperHDR output)

```bash
sudo modprobe v4l2loopback video_nr=10 card_label="Screen Sight" exclusive_caps=1
```

`exclusive_caps=1` matters. Without it the device advertises both capture and
output capabilities, and many consumers — HyperHDR included — refuse to open
it.

If HyperHDR shows a pink / doubled / wrong-aspect image while the Screen Sight
window and `:7661` look fine, the loopback format is stale. Reload it, start
Screen Sight first, then HyperHDR:

```bash
sudo pkill -f "python -m processor" || true
sudo systemctl stop hyperhdr@$USER 2>/dev/null || pkill hyperhdr || true
sudo modprobe -r v4l2loopback
sudo modprobe v4l2loopback video_nr=10 card_label="Screen Sight" exclusive_caps=1
python -m processor run
# then start HyperHDR; match width/height/pixel format to the log line
# "V4L2 output ready: /dev/video10 YUYV WxH"
v4l2-ctl -d /dev/video10 -c keep_format=1
```

To load it at every boot:

```bash
echo v4l2loopback | sudo tee /etc/modules-load.d/v4l2loopback.conf
printf 'options v4l2loopback video_nr=10 card_label="Screen Sight" exclusive_caps=1\n' \
  | sudo tee /etc/modprobe.d/v4l2loopback.conf
```

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video10 --all
```

### Pointing HyperHDR at it

1. Start Screen Sight so it is writing to `/dev/video10`
2. Restart HyperHDR so it rescans devices
3. Open **Video capturing** (not “add device” — there isn’t one)
4. Choose **Screen Sight** / `/dev/video10`
5. Resolution **1280×720** (match Screen Sight), format **YUYV**, ~20 fps  
   (use 640×360 only if you started Screen Sight at that size)
6. Turn off HyperHDR’s own crop / black-bar / signal detection — Screen Sight
   already did that work

### Running as a service

```bash
sudo cp packaging/screen-sight.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now screen-sight
journalctl -u screen-sight -f
```

Edit the unit first: it expects the checkout in `/opt/screen-sight` and
a config at `/etc/screen-sight/config.yaml`.

---

## Calibrating

Start with `--web` and open <http://localhost:7660>.

1. **Mark the TV corners.** Press _Auto-detect_ first; it is usually right. If
   not, click the four corners of the screen clockwise from the top-left and
   drag to fine-tune — a magnifying loupe follows the cursor. Press _Apply_.
2. **Watch the result.** The right-hand panel shows exactly what the virtual
   camera is emitting, plus any single pipeline stage you want to inspect.
3. **Tune.** Every slider applies live: crop inset, black-bar sensitivity and
   how fast the crop is allowed to move, gamma, saturation, white balance.
4. **Save to YAML** when you are happy.

![the calibration wizard](docs/wizard.png)

Automatic detection needs a second or two of moving picture to work — it finds
the TV by noticing that it is the only thing in the room that changes. Have
something playing. Extreme side-on angles (strong trapezoid) with a dark bezel
on a dark wall are the hardest case: click the four corners manually if
Auto-detect misses, then Save.

### Boundary modes

| Mode     | Behaviour                                                                           |
| -------- | ----------------------------------------------------------------------------------- |
| `auto`   | Detect continuously; ignore any saved corners.                                      |
| `hybrid` | Use saved corners, but fall back to detection if the camera is bumped. **Default.** |
| `manual` | Always use saved corners, whatever happens.                                         |

---

## Configuration

Copy `config.example.yaml` to `config.yaml`; it is picked up automatically from
the working directory, `~/.config/screen-sight/`, or
`/etc/screen-sight/`. Anything you leave out keeps its default, so a
useful config can be four lines:

```yaml
camera:
  rtsp_url: 'rtsp://admin:PASSWORD@192.168.1.93:5543/live/channel10'
output:
  fps: 15
```

`screensight config` prints the full expanded configuration, every key with its
default, which is the most reliable reference.

---

## The pipeline

Every stage is independent and individually optional. Remove a name from
`pipeline.stages` and it is gone; set `<stage>.enabled: false` and it stays
constructed but passive, so the debug UI can switch it back on at runtime.

| Stage         | What it does                                                            |
| ------------- | ----------------------------------------------------------------------- |
| `movement`    | Notices that the camera itself was moved, and asks for a recalibration. |
| `boundary`    | Locates the TV and publishes its four corners.                          |
| `perspective` | Warps that quadrilateral into a true rectangle.                         |
| `crop`        | Trims a fixed inset — bezel, and the glowing rim of the panel.          |
| `blackbars`   | Finds and removes letterbox/pillarbox bars, with heavy anti-flicker.    |
| `reflection`  | Ignores a margin at each edge; optionally blanks static logos.          |
| `color`       | White balance, exposure, gamma, contrast, brightness, saturation.       |
| `resize`      | Scales to the output resolution.                                        |

Adding a stage means writing the class and calling `register_stage`; nothing
else changes, and the new name becomes usable in `pipeline.stages`
immediately. The future modules named in the original brief — subtitle
masking, adaptive edge weighting — fit this shape.

### How the TV is found

Three cues, combined, because none is reliable alone in a living room:

- **Activity.** A TV is the only thing in the room that moves. Accumulating
  peak frame difference over a couple of seconds paints a mask over exactly
  the screen and nothing else — measured at ~100 % of the picture area with
  under 3 % spill onto the room. This is the only cue that is _specific_ to a
  television, so it chooses the answer; the others only refine it.
- **Edges.** The panel border, found by contour fitting.
- **Brightness.** A lit screen against a darker wall.

Two details do most of the work:

_A letterboxed film only moves in the middle of the screen_, so the activity
mask describes the picture, not the panel. Each candidate therefore also gets
offered as the 16:9 panel it would imply, grown outwards. Without this the
detector locks onto the 2.39:1 picture and is then wrong for the next
programme.

_The on-screen aspect ratio of a quad is not the aspect ratio of the rectangle
it depicts._ Seen from the sofa, a 16:9 TV measures anywhere from 1.4 to 2.2.
Scoring candidates on their on-screen shape systematically rewards the wrong
ones, so the true ratio is recovered from the projection first (Zhang & He's
closed form, which also solves for the unknown focal length).

Accuracy on the bundled scenes — every content aspect from 4:3 to 2.39:1,
steep angles, glare, darkness, sensor noise, thick and absent bezels — is
2–17 px on a 960 px frame:

```bash
python tools/bench_detection.py
```

### Why nothing flickers

Ambient lighting amplifies instability: a crop edge that wobbles by two pixels
is invisible on a monitor and obvious on a light strip. Every value that can
change over time passes through three filters, in order:

1. a median window, which removes single-frame outliers;
2. a hysteresis gate, which only commits a new value once it has held for most
   of a second;
3. a rate limiter, so even a committed change eases in over a second or two.

Detection of the bars themselves is a compare-and-sum rather than a per-row
percentile — the same statement mathematically, 20x cheaper, and at 15 fps
that difference is a tenth of the entire CPU budget.

### Recalibration

The camera is bolted to a shelf, so instead of re-detecting the TV every
frame, a much cheaper question gets asked twice a second: _is the calibration
still valid?_ The TV is masked out and the remaining scenery — wall, picture
frame, doorway — is compared with a reference by normalised cross-correlation.

Correlation rather than a brightness difference, because the two events that
must be told apart both change every pixel in the frame. Turning the room
lights on scales the image and moves nothing; nudging the camera preserves
brightness and moves everything. A difference metric ranks the lighting change
as the larger event by a factor of twenty, which is exactly backwards.

Phase correlation and ORB feature matching were both tried and rejected; the
reasons are recorded in `processor/stages/movement.py` so they do not get
quietly re-attempted.

Recovery after a real bump takes about four seconds: a second and a half to be
sure the camera moved rather than someone walking past, then a fresh window of
motion history to detect the TV in its new position. The crop is stale for
that period, which is the right trade for an event that happens twice a year.

---

## Developing without a camera

The synthetic scene renders a full living room — a 16:9 panel projected
through an actual pinhole camera model, with a bezel, wall reflections, sensor
noise, a colour cast, letterboxed content, subtitles, a static logo and an
optional camera bump.

```bash
# generate sample stills and a clip whose aspect ratio changes mid-way
python -m processor samples --out samples/generated

# replay it through the pipeline with the debug window
python -m processor run --source file --input samples/generated/livingroom.mp4 \
  --no-v4l2 --debug

# record from the real camera, then work offline
python -m processor record --rtsp-url rtsp://... -o recordings/sofa.mp4 -d 60
```

`samples/generated/stills/ground-truth.json` carries the exact TV corners for
each still, so detection accuracy can be measured rather than eyeballed.

### Debug window

`--debug` opens a window with per-stage views and live statistics.

| Key       |                                                 |
| --------- | ----------------------------------------------- |
| `0`–`9`   | select a view                                   |
| `[` `]`   | previous / next view                            |
| `g`       | grid of every view at once                      |
| `o` / `h` | toggle the overlay / this help                  |
| `space`   | pause the redraw                                |
| `r`       | force a TV recalibration                        |
| `s` / `w` | save a snapshot / write the config              |
| `m b p c` | toggle movement / boundary / perspective / crop |
| `k f l z` | toggle blackbars / reflection / color / resize  |
| `q`       | quit                                            |

### Tests

```bash
pip install -r requirements-dev.txt
pytest                              # 191 tests, no hardware needed
python tools/bench_detection.py     # detection accuracy against ground truth
```

---

## Performance

Measured on the bundled scenes at 640x360 output; per-frame pipeline cost:

| Stage             | ms       |
| ----------------- | -------- |
| movement          | 0.05     |
| boundary          | 0.60     |
| perspective       | 0.40     |
| blackbars         | 0.75     |
| color             | 0.24     |
| resize            | 0.19     |
| crop + reflection | 0.02     |
| **total**         | **~2.3** |

TV detection itself costs ~12 ms, but it runs only while searching for the
screen and stops entirely once locked; the per-frame cost after that is the
0.05 ms of accumulating the activity map.

On the target machine (i5-3230M) expect roughly 4x those figures, which is
under 10 ms against a 66 ms budget at 15 fps. Memory sits well under 300 MB.

If you need more headroom, `perspective.width`/`height` is the knob that
matters: every stage after the warp scales with it.

**Latency.** The camera decoder runs on its own thread and keeps exactly one
frame in hand. If the pipeline falls behind, intermediate frames are dropped
rather than queued, and frames arriving faster than `output.fps` are discarded
before any work is done on them. Latency therefore stays flat over hours
instead of creeping upwards; end-to-end is a few milliseconds of processing
plus whatever the camera and network contribute.

---

## Output sinks

| Sink    | Notes                                                             |
| ------- | ----------------------------------------------------------------- |
| `v4l2`  | The virtual webcam. Linux only; skipped with a warning elsewhere. |
| `mjpeg` | HTTP stream, for checking the result from another machine.        |
| `file`  | Records the processed output to a video file.                     |
| `ddp`   | Samples LED colours and sends them straight to WLED over UDP.     |

The DDP sink is the path that eventually removes HyperHDR from the chain. It
is off by default and needs the LED counts for each edge:

```yaml
output:
  ddp:
    enabled: true
    host: 192.168.1.50
    leds_top: 42
    leds_right: 24
    leds_bottom: 42
    leds_left: 24
    start_corner: top-left
```

---

## Troubleshooting

**`/dev/video10 does not exist`** — the loopback module is not loaded. See
[The virtual camera](#the-virtual-camera-hyperhdr-output).

**`Address already in use` (7660 / 7661)** — a previous Screen Sight is still
running. `pkill -f 'python -m processor'` then retry.

**`zsh: command not found: --source`** — a line-continuation `\` had a trailing
space (or the lines were pasted badly). Use the one-line command in
[Quick start](#quick-start-usb-webcam).

**HyperHDR cannot open the device** — reload v4l2loopback with
`exclusive_caps=1`, start Screen Sight **before** enabling capture, and pick
the device under **Video capturing**. `v4l2-ctl -d /dev/video10 --all` should
show a 1280x720 YUYV format (or whatever `--width/--height` you set).

**The TV is never detected** — the detector needs moving picture; a paused
frame or a static menu gives it nothing to work with. Play something, or mark
the corners by hand in the wizard. Check `boundary.confidence` in the wizard's
status panel; below ~0.3 it will not accept a fit.

**The crop keeps twitching** — raise `blackbars.window` and
`blackbars.hold_frames`, and lower `blackbars.max_step_percent`.

**Colours drift or pump** — that is `color.exposure.enabled` or
`color.white_balance: auto` reacting to content. Both are off by default;
turn them off again, or raise their smoothing.

**RTSP keeps reconnecting** — force `camera.transport: tcp` (the default), and
raise `camera.read_timeout` if the camera has long keyframe intervals.

---

## License

LumosOS — including **all past and present commits** in this repository — is
licensed under the [GNU General Public License v3.0](LICENSE), with
[Additional Terms](NOTICE) under GPL §7.

In short:

- Derivative works and redistributed copies must remain open source under GPL-3.0.
- Products built with LumosOS must give clear front-page credit that LumosOS was used to build them (see `NOTICE`).

```
Copyright (C) 2026 Shivansh Tyagi
```
