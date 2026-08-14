# Lumos Cam protocol v1

Wire contract between **Lumos Cam** (Android Camera2 app) and **Lumos Vision**
(Screen Sight host). Version this document with the app; Lumos Vision pins
`needs Lumos Cam ≥ 0.1`.

Header on every HTTP response: `Lumos-Cam-Protocol: 1`.

## Ports (device)

| Role    | Default | Transport |
|---------|---------|-----------|
| Control | `8765`  | HTTP/1.1 JSON |
| Video   | `8766`  | Raw elementary stream over TCP |

Host uses `adb forward`:

```
adb forward tcp:<control_host_port> tcp:8765
adb forward tcp:<video_host_port>   tcp:8766
```

Defaults on the host: control `18765`, video `18766`.

## Video

One TCP client at a time. After `POST /stream` with `enabled: true`, Lumos Cam
accepts a connection on the video port and writes:

| `codec` | Payload | Host ffmpeg |
|---------|---------|-------------|
| `h264` (default) | Annex-B H.264 (hardware encoder) | `-f h264 -i tcp://127.0.0.1:<video_host_port>` |
| `mjpeg` | Concatenated JPEG frames | `-f mjpeg -i tcp://127.0.0.1:<video_host_port>` |

No length prefix. ffmpeg writes YUYV to `/dev/video11` (same bind path as scrcpy).

## Control HTTP

All JSON bodies. Unknown fields ignored. Errors:

```json
{"ok": false, "error": "human-readable"}
```

### `GET /status`

```json
{
  "ok": true,
  "protocol": 1,
  "app_version": "0.1.0",
  "camera_id": "0",
  "size": "1920x1080",
  "fps": 30,
  "zoom": 1.0,
  "zoom_min": 1.0,
  "zoom_max": 10.0,
  "pan_x": 0.0,
  "pan_y": 0.0,
  "af": "auto",
  "ae": "auto",
  "awb": "auto",
  "cal_mode": false,
  "focus_distance": null,
  "iso": null,
  "exposure_ns": null,
  "streaming": false,
  "codec": "h264",
  "orientation": 90,
  "video_clients": 0,
  "bytes_sent": 0,
  "encoder_attached": false
}
```

Lock fields `af` / `ae` / `awb` are `"auto"` or `"locked"`.

### `GET /caps`

Camera2 capability dump (see Lumos Cam `docs/camera2-caps-21061119BI.json`). Used to
clamp zoom and decide whether manual ISO/focus are offered.

### `POST /locks`

```json
{"af": "locked", "ae": "auto", "awb": "locked"}
```

Omitted keys unchanged.

### `POST /zoom`

```json
{"ratio": 1.5}
```

Live `CONTROL_ZOOM_RATIO` (API 30+) or `SCALER_CROP_REGION`. No process restart.

### `POST /pan`

```json
{"x": 0.0, "y": 0.0}
```

Normalised `-1..1` (x: left→right, y: top→bottom). Combined with zoom via the
scaler crop. `{"x": 0, "y": 0}` recentres.

### `POST /focus`

```json
{"mode": "auto"}
```

or `{"mode": "manual", "distance": 2.5}` (dioptres, Camera2 `LENS_FOCUS_DISTANCE`).

### `POST /exposure`

```json
{"mode": "auto"}
```

or `{"mode": "manual", "iso": 100, "exposure_ns": 10000000}`.

### `POST /cal_mode`

```json
{"enabled": true}
```

Atomic preset:

- `enabled: true` — lock AF, AE, AWB; freeze current zoom/pan; remember prior
  auto/lock state.
- `enabled: false` — restore the remembered state.

### `POST /stream`

```json
{"enabled": true, "codec": "h264", "width": 1920, "height": 1080, "fps": 30}
```

Starts or stops the video listener. Size/fps/codec changes reopen the camera
session (preview stays up).

### `POST /camera`

```json
{"id": "0"}
```

Select Camera2 id (usually `"0"` = back).
