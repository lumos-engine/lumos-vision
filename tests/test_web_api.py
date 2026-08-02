"""Calibration wizard HTTP API, exercised against a real server."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from processor.app import Processor
from processor.camera.base import Frame
from processor.config.schema import Config
from processor.testing.scene import SceneParams, SyntheticScene
from processor.web.server import CalibrationServer, public_config


@pytest.fixture
def processor() -> Processor:
    config = Config.from_dict(
        {
            "camera": {
                "source": "synthetic",
                "replay_fps": 60,
                "rtsp_url": "rtsp://admin:hunter2@192.168.1.93:5543/live/channel10",
            },
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "boundary": {"mode": "auto"},
            "logging": {"stats_interval": 0},
        }
    )
    app = Processor(config)
    app.start()

    # Feed frames at explicit scene times rather than in real time: detection
    # needs a couple of seconds of *content* motion, and waiting for that in
    # wall-clock would make the suite crawl.  The processing loop is not
    # running, so `submit` executes inline on the request thread.
    scene = SyntheticScene(SceneParams(shake_px=0.0))
    for i in range(40):
        app.process_frame(Frame(image=scene.frame(i * 0.12), index=i))

    try:
        yield app
    finally:
        app.shutdown()


@pytest.fixture
def server(processor: Processor):
    httpd = CalibrationServer(("127.0.0.1", 0), processor)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        return response.status, response.read(), response.headers


def post(base, path, payload):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


# ------------------------------------------------------------------- pages


def test_serves_the_wizard_and_its_assets(server):
    for path in ("/", "/static/app.js", "/static/style.css"):
        status, body, _ = get(server, path)
        assert status == 200 and body


def test_unknown_paths_are_404(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(server, "/nope")
    assert excinfo.value.code == 404


def test_static_files_cannot_escape_the_static_directory(server):
    for path in ("/static/../server.py", "/static/..%2fserver.py"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            get(server, path)
        assert excinfo.value.code in (403, 404)


# -------------------------------------------------------------------- data


def test_status_reports_the_running_pipeline(server):
    status, body, _ = get(server, "/api/status")
    data = json.loads(body)
    assert status == 200
    assert data["frames_out"] > 0
    assert "boundary" in data["views"]
    assert {s["name"] for s in data["pipeline"]["stages"]} >= {"boundary", "color"}


def test_camera_credentials_are_never_sent_to_the_browser(server, processor):
    assert "hunter2" in processor.config.camera.rtsp_url
    for path in ("/api/config", "/api/status"):
        _, body, _ = get(server, path)
        assert b"hunter2" not in body
    assert "hunter2" not in json.dumps(public_config(processor))


def test_snapshot_returns_a_jpeg(server):
    status, body, headers = get(server, "/api/snapshot?view=boundary")
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert body[:2] == b"\xff\xd8"  # JPEG SOI


# ------------------------------------------------------------------ config


def test_config_updates_apply_to_the_live_pipeline(server, processor):
    status, body = post(server, "/api/config", {"updates": {"color.gamma": 1.45}})
    assert status == 200 and body["ok"]
    assert processor.config.color.gamma == 1.45
    assert processor.pipeline.get("color").config.gamma == 1.45


def test_invalid_config_values_are_rejected_with_a_reason(server, processor):
    before = processor.config.color.gamma
    status, body = post(server, "/api/config", {"updates": {"color.gamma": "banana"}})
    assert status == 400
    assert not body["ok"]
    assert "color.gamma" in body["error"]
    assert processor.config.color.gamma == before, "a bad value reached the pipeline"


def test_unknown_config_keys_are_rejected(server):
    status, body = post(server, "/api/config", {"updates": {"color.hue_rotate": 30}})
    assert status == 400 and not body["ok"]


def test_empty_update_is_rejected(server):
    status, _ = post(server, "/api/config", {"updates": {}})
    assert status == 400


def test_config_can_be_saved_to_yaml(server, tmp_path):
    target = tmp_path / "saved.yaml"
    status, body = post(server, "/api/config/save", {"path": str(target)})
    assert status == 200 and body["ok"]
    assert target.exists()

    from processor.config.loader import load_config

    assert load_config(target).output.width == 320


# ------------------------------------------------------------- calibration


def test_auto_detect_returns_normalised_corners(server):
    status, body = post(server, "/api/calibrate/auto", {})
    assert status == 200
    assert body["ok"], body.get("error")
    assert len(body["corners"]) == 4
    assert all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in body["corners"])
    assert body["confidence"] > 0


def test_manual_corners_switch_the_stage_to_manual(server, processor):
    corners = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
    status, body = post(server, "/api/calibrate/corners", {"corners": corners})
    assert status == 200 and body["ok"]
    assert processor.config.boundary.mode == "manual"
    assert processor.config.boundary.corners == corners


def test_malformed_corners_are_rejected(server):
    for payload in ([[0, 0], [1, 1]], [[0, 0, 0]] * 4, "nonsense"):
        status, body = post(server, "/api/calibrate/corners", {"corners": payload})
        assert status == 400, payload
        assert not body["ok"]


def test_clearing_corners_returns_to_auto(server, processor):
    post(server, "/api/calibrate/corners", {"corners": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]})
    status, body = post(server, "/api/calibrate/corners", {"corners": None})
    assert status == 200 and body["ok"]
    assert processor.config.boundary.mode == "auto"


def test_recalibrate_endpoint(server):
    status, body = post(server, "/api/recalibrate", {})
    assert status == 200 and body["ok"]


# ------------------------------------------------------------------ stages


def test_stage_can_be_toggled(server, processor):
    status, body = post(server, "/api/stage", {"name": "color", "enabled": False})
    assert status == 200 and body["enabled"] is False
    assert processor.pipeline.get("color").enabled is False

    status, body = post(server, "/api/stage", {"name": "color", "enabled": True})
    assert body["enabled"] is True


def test_toggling_an_unknown_stage_is_a_404(server):
    status, body = post(server, "/api/stage", {"name": "teleport", "enabled": False})
    assert status == 404
    assert not body["ok"]
