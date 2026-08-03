# Screen Sight

Takes an RTSP stream from a camera pointed at a television and turns it into a
clean, rectified, cropped, low-latency video stream, published as a virtual
V4L2 webcam.

```
RTSP camera ─▶ decode ─▶ Screen Sight ─▶ /dev/video10 ─▶ HyperHDR ─▶ ESP32 + WLED
```

The processor knows nothing about HyperHDR. It produces frames and hands them
to pluggable output sinks; HyperHDR is simply the one reading the virtual
camera today. A DDP sink that drives WLED directly is already in the box, so
HyperHDR can be removed from the chain later without touching the pipeline.

**What it actually does:** finds the TV in the camera's view, removes the
perspective so the screen becomes a true rectangle, trims the bezel, detects
and removes letterbox bars as the aspect ratio changes mid-film, ignores
reflections near the edges, and normalises the colour — all at 640x360 and
15 fps for a couple of milliseconds of CPU per frame.

---

## Quick start

Try it with no camera and no kernel modules:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# A synthetic living room, streamed to http://localhost:7661
python -m processor run --source synthetic --no-v4l2 --mjpeg
```

Against a real camera, with the calibration wizard on
<http://localhost:7660>:

```bash
python -m processor run \
  --rtsp-url rtsp://admin:PASSWORD@192.168.1.93:5543/live/channel10 \
  --web
```

Or a USB webcam plugged into the same machine (`v4l2-ctl --list-devices` to
find the node — do not use the Screen Sight loopback `/dev/video10`):

```bash
python -m processor run \
  --source v4l2 --camera-device /dev/video2 \
  --capture-width 1280 --capture-height 720 \
  --web --mjpeg
```

The RTSP URL is never hardcoded and never logged in full — credentials are
redacted from logs and from everything the web UI can see.

**Verify the encoder size before assuming which channel is “main”.** On CP PLUS
cameras the path (`/live/channel10`, `/live/channel1`, …) is stable, but the
resolution behind it can change (ezyKam+ quality, ONVIF, NVR, reboot). Check
with:

```bash
ffprobe -rtsp_transport tcp \
  "rtsp://admin:PASSWORD@192.168.1.93:5543/live/channel10"
```

Use whichever URL currently delivers the resolution you want. Do not treat
`channel10` as permanently 2304×1296 — it has also been observed at 640×360.

---

## Installing on Ubuntu 24.04

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip v4l2loopback-dkms v4l-utils

git clone <this repo> screen-sight && cd screen-sight
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .            # optional: provides the `screensight` command
```

### The virtual camera

```bash
sudo modprobe v4l2loopback video_nr=10 card_label="Screen Sight" exclusive_caps=1
```

`exclusive_caps=1` matters. Without it the device advertises both capture and
output capabilities, and many consumers — HyperHDR included — refuse to open
it.

To load it at every boot:

```bash
echo v4l2loopback | sudo tee /etc/modules-load.d/v4l2loopback.conf
printf 'options v4l2loopback video_nr=10 card_label="Screen Sight" exclusive_caps=1\n' \
  | sudo tee /etc/modprobe.d/v4l2loopback.conf
```

Check it is there, then run the processor and confirm the format:

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video10 --all
```

### Pointing HyperHDR at it

In HyperHDR, add a **USB capture** device, select `Screen Sight` (`/dev/video10`),
set the resolution to 640x360 and the format to YUYV. Leave HyperHDR's own
cropping and signal detection off — this processor has already done that work,
and layering two croppers on top of each other only makes both wrong.

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

1. **Mark the TV corners.** Press *Auto-detect* first; it is usually right. If
   not, click the four corners of the screen clockwise from the top-left and
   drag to fine-tune — a magnifying loupe follows the cursor. Press *Apply*.
2. **Watch the result.** The right-hand panel shows exactly what the virtual
   camera is emitting, plus any single pipeline stage you want to inspect.
3. **Tune.** Every slider applies live: crop inset, black-bar sensitivity and
   how fast the crop is allowed to move, gamma, saturation, white balance.
4. **Save to YAML** when you are happy.

![the calibration wizard](docs/wizard.png)

Automatic detection needs a second or two of moving picture to work — it finds
the TV by noticing that it is the only thing in the room that changes. Have
something playing.

### Boundary modes

| Mode | Behaviour |
| --- | --- |
| `auto` | Detect continuously; ignore any saved corners. |
| `hybrid` | Use saved corners, but fall back to detection if the camera is bumped. **Default.** |
| `manual` | Always use saved corners, whatever happens. |

---

## Configuration

Copy `config.example.yaml` to `config.yaml`; it is picked up automatically from
the working directory, `~/.config/screen-sight/`, or
`/etc/screen-sight/`. Anything you leave out keeps its default, so a
useful config can be four lines:

```yaml
camera:
  rtsp_url: "rtsp://admin:PASSWORD@192.168.1.93:5543/live/channel10"
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

| Stage | What it does |
| --- | --- |
| `movement` | Notices that the camera itself was moved, and asks for a recalibration. |
| `boundary` | Locates the TV and publishes its four corners. |
| `perspective` | Warps that quadrilateral into a true rectangle. |
| `crop` | Trims a fixed inset — bezel, and the glowing rim of the panel. |
| `blackbars` | Finds and removes letterbox/pillarbox bars, with heavy anti-flicker. |
| `reflection` | Ignores a margin at each edge; optionally blanks static logos. |
| `color` | White balance, exposure, gamma, contrast, brightness, saturation. |
| `resize` | Scales to the output resolution. |

Adding a stage means writing the class and calling `register_stage`; nothing
else changes, and the new name becomes usable in `pipeline.stages`
immediately. The future modules named in the original brief — subtitle
masking, adaptive edge weighting — fit this shape.

### How the TV is found

Three cues, combined, because none is reliable alone in a living room:

- **Activity.** A TV is the only thing in the room that moves. Accumulating
  peak frame difference over a couple of seconds paints a mask over exactly
  the screen and nothing else — measured at ~100 % of the picture area with
  under 3 % spill onto the room. This is the only cue that is *specific* to a
  television, so it chooses the answer; the others only refine it.
- **Edges.** The panel border, found by contour fitting.
- **Brightness.** A lit screen against a darker wall.

Two details do most of the work:

*A letterboxed film only moves in the middle of the screen*, so the activity
mask describes the picture, not the panel. Each candidate therefore also gets
offered as the 16:9 panel it would imply, grown outwards. Without this the
detector locks onto the 2.39:1 picture and is then wrong for the next
programme.

*The on-screen aspect ratio of a quad is not the aspect ratio of the rectangle
it depicts.* Seen from the sofa, a 16:9 TV measures anywhere from 1.4 to 2.2.
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
frame, a much cheaper question gets asked twice a second: *is the calibration
still valid?* The TV is masked out and the remaining scenery — wall, picture
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

| Key | |
| --- | --- |
| `0`–`9` | select a view |
| `[` `]` | previous / next view |
| `g` | grid of every view at once |
| `o` / `h` | toggle the overlay / this help |
| `space` | pause the redraw |
| `r` | force a TV recalibration |
| `s` / `w` | save a snapshot / write the config |
| `m b p c` | toggle movement / boundary / perspective / crop |
| `k f l z` | toggle blackbars / reflection / color / resize |
| `q` | quit |

### Tests

```bash
pip install -r requirements-dev.txt
pytest                              # 191 tests, no hardware needed
python tools/bench_detection.py     # detection accuracy against ground truth
```

---

## Performance

Measured on the bundled scenes at 640x360 output; per-frame pipeline cost:

| Stage | ms |
| --- | --- |
| movement | 0.05 |
| boundary | 0.60 |
| perspective | 0.40 |
| blackbars | 0.75 |
| color | 0.24 |
| resize | 0.19 |
| crop + reflection | 0.02 |
| **total** | **~2.3** |

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

| Sink | Notes |
| --- | --- |
| `v4l2` | The virtual webcam. Linux only; skipped with a warning elsewhere. |
| `mjpeg` | HTTP stream, for checking the result from another machine. |
| `file` | Records the processed output to a video file. |
| `ddp` | Samples LED colours and sends them straight to WLED over UDP. |

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
[The virtual camera](#the-virtual-camera).

**HyperHDR cannot open the device** — reload v4l2loopback with
`exclusive_caps=1`, and make sure the processor is actually running and
writing to it. `v4l2-ctl -d /dev/video10 --all` should show a 640x360 YUYV
format.

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

## Licence

MIT.
