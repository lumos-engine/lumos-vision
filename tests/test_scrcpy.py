"""scrcpy sidecar manager and processor hooks."""

from __future__ import annotations

from processor.config.loader import apply_updates, config_to_dict
from processor.config.schema import Config, ScrcpyConfig
from processor.utils.scrcpy import (
    MIN_VIEW_ZOOM_FOR_PAN,
    ZOOM_STEP,
    ScrcpyManager,
    adb_device_ready,
    build_crop_arg,
    build_scrcpy_command,
    clamp_zoom,
    step_pan,
    step_zoom,
)


def _use_synthetic_capture(app, monkeypatch) -> None:
    from processor.camera.synthetic import SyntheticSource
    from processor.config.schema import CameraConfig

    monkeypatch.setattr(
        app,
        "_make_source_unlocked",
        lambda: SyntheticSource(CameraConfig(source="synthetic", replay_fps=60)),
    )


def test_sink_has_capture_requires_device_caps_not_idle_output(monkeypatch, tmp_path):
    from processor.utils.scrcpy import sink_has_capture

    sink = tmp_path / "video11"
    sink.write_text("")
    stdout = {"text": ""}

    class Result:
        returncode = 0
        stderr = ""

        @property
        def stdout(self):
            return stdout["text"]

    monkeypatch.setattr(
        "processor.utils.scrcpy.subprocess.run",
        lambda *a, **k: Result(),
    )
    stdout["text"] = (
        "Capabilities     : 0x85200003\n"
        "\tVideo Capture\n"
        "\tVideo Output\n"
        "\tDevice Capabilities\n"
        "Device Caps      : 0x05200002\n"
        "\tVideo Output\n"
        "\tStreaming\n"
    )
    assert sink_has_capture(str(sink)) is False
    stdout["text"] = (
        "Capabilities     : 0x85200001\n"
        "\tVideo Capture\n"
        "\tDevice Capabilities\n"
        "Device Caps      : 0x05200001\n"
        "\tVideo Capture\n"
        "\tStreaming\n"
    )
    assert sink_has_capture(str(sink)) is True


def test_adb_device_ready_parses_devices(monkeypatch):
    class Result:
        stdout = "List of devices attached\n452ee42b0506\tdevice\n"

    monkeypatch.setattr(
        "processor.utils.scrcpy.subprocess.run",
        lambda *a, **k: Result(),
    )
    assert adb_device_ready() is True
    assert adb_device_ready("452ee42b0506") is True
    assert adb_device_ready("other") is False


def test_build_scrcpy_command_includes_camera_and_sink():
    cfg = ScrcpyConfig(
        enabled=True,
        binary="/opt/scrcpy/scrcpy",
        camera_id="0",
        camera_size="1920x1080",
        camera_fps=30,
        camera_zoom=2.0,
        v4l2_sink="/dev/video11",
        serial="452ee42b0506",
        extra_args=["-V", "info"],
    )
    cmd = build_scrcpy_command(cfg, binary="/opt/scrcpy/scrcpy")
    assert cmd[0] == "/opt/scrcpy/scrcpy"
    assert "--video-source=camera" in cmd
    assert "--camera-id=0" in cmd
    assert "--camera-size=1920x1080" in cmd
    assert "--camera-zoom=2" in cmd
    assert "--v4l2-sink=/dev/video11" in cmd
    assert "--no-audio" in cmd
    assert "--no-playback" in cmd
    assert "--no-window" in cmd
    assert cmd[cmd.index("-s") + 1] == "452ee42b0506"
    assert "-V" in cmd and "info" in cmd
    windowed = build_scrcpy_command(
        ScrcpyConfig(enabled=True, no_playback=False, v4l2_sink="/dev/video11"),
        binary="/opt/scrcpy/scrcpy",
    )
    assert "--no-window" not in windowed


def test_step_zoom_matches_scrcpy_factor():
    cfg = ScrcpyConfig(zoom_min=1.0, zoom_max=10.0, camera_zoom=1.0)
    z = step_zoom(1.0, inward=True, cfg=cfg)
    assert abs(z - ZOOM_STEP) < 1e-6
    assert abs(step_zoom(z, inward=False, cfg=cfg) - 1.0) < 1e-6
    assert clamp_zoom(99.0, cfg) == 10.0


def test_crop_arg_pans_within_camera_size():
    cfg = ScrcpyConfig(
        camera_size="1920x1080",
        view_zoom=2.0,
        pan_x=-1.0,
        pan_y=0.0,
    )
    assert build_crop_arg(cfg) == "960:540:0:270"
    cfg.pan_x = 1.0
    assert build_crop_arg(cfg) == "960:540:960:270"
    assert build_crop_arg(ScrcpyConfig(camera_size="1920x1080")) == ""


def test_step_pan_left_raises_view_zoom():
    cfg = ScrcpyConfig(view_zoom=1.0, pan_x=0.0, pan_y=0.0)
    pan_x, pan_y, view_zoom = step_pan(cfg, direction="left")
    assert pan_x < 0
    assert pan_y == 0.0
    assert view_zoom >= MIN_VIEW_ZOOM_FOR_PAN


def test_config_round_trip_includes_scrcpy(tmp_path):
    config = Config.from_dict(
        {
            "scrcpy": {
                "enabled": True,
                "binary": "/opt/scrcpy/scrcpy",
                "camera_zoom": 1.5,
                "v4l2_sink": "/dev/video11",
            }
        }
    )
    assert config.scrcpy.enabled is True
    assert config.scrcpy.camera_zoom == 1.5
    data = config_to_dict(config)
    assert data["scrcpy"]["binary"] == "/opt/scrcpy/scrcpy"
    again = Config.from_dict(data)
    assert again.scrcpy.v4l2_sink == "/dev/video11"


def test_manager_start_stop_with_fake_popen(monkeypatch, tmp_path):
    sink = tmp_path / "video11"
    sink.write_text("")

    class FakeProc:
        def __init__(self):
            self.pid = 4242
            self.returncode = None
            self.stdout = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            self.returncode = self.returncode if self.returncode is not None else 0
            return self.returncode

        def communicate(self, timeout=None):
            return ("", None)

    spawned = {}

    def fake_popen(cmd, **kwargs):
        spawned["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr("processor.utils.scrcpy.subprocess.Popen", fake_popen)
    monkeypatch.setattr("processor.utils.scrcpy.sink_has_capture", lambda device: True)
    monkeypatch.setattr("processor.utils.scrcpy.os.killpg", lambda *a, **k: None)

    mgr = ScrcpyManager()
    cfg = ScrcpyConfig(
        enabled=True,
        binary="/opt/scrcpy/scrcpy",
        v4l2_sink=str(sink),
        startup_timeout_sec=1.0,
    )
    result = mgr.start(cfg)
    assert result["ok"] is True
    assert result["running"] is True
    assert mgr.running is True
    assert "--v4l2-sink=" + str(sink) in spawned["cmd"]
    assert mgr.stop()["running"] is False


def test_apply_scrcpy_zoom_restarts(monkeypatch):
    from processor.app import Processor

    calls = []

    class FakeMgr:
        def __init__(self):
            self._running = False

        @property
        def running(self):
            return self._running

        def status(self, cfg):
            from processor.utils.scrcpy import ScrcpyStatus

            return ScrcpyStatus(
                enabled=cfg.enabled,
                running=self._running,
                pid=1 if self._running else None,
                zoom=cfg.camera_zoom,
                view_zoom=cfg.view_zoom,
                pan_x=cfg.pan_x,
                pan_y=cfg.pan_y,
                crop="",
                camera_id=cfg.camera_id,
                camera_size=cfg.camera_size,
                camera_fps=cfg.camera_fps,
                v4l2_sink=cfg.v4l2_sink,
                binary=cfg.binary,
                last_error="",
                command=[],
            )

        def stop(self):
            self._running = False
            calls.append("stop")
            return {"ok": True, "running": False}

        def ensure_running(self, cfg):
            self._running = True
            calls.append(("ensure", cfg.camera_zoom))
            return {"ok": True, "running": True, "pid": 1}

        def restart(self, cfg):
            self._running = True
            calls.append(("restart", round(cfg.camera_zoom, 4)))
            return {"ok": True, "running": True, "pid": 1}

    config = Config.from_dict(
        {
            "camera": {"source": "scrcpy"},
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
            "scrcpy": {"camera_zoom": 1.0},
        }
    )
    app = Processor(config)
    app._scrcpy = FakeMgr()
    monkeypatch.setattr(app, "_recreate_source_unlocked", lambda: {"ok": True})
    _use_synthetic_capture(app, monkeypatch)
    app.start()
    try:
        result = app.apply_scrcpy(action="zoom_in")
        assert result["ok"] is True
        assert ("restart", round(ZOOM_STEP, 4)) in calls
        assert abs(app.config.scrcpy.camera_zoom - ZOOM_STEP) < 1e-6
    finally:
        app.shutdown()


def test_apply_updates_can_set_scrcpy_zoom():
    config = Config()
    updated = apply_updates(config, {"camera.source": "scrcpy", "scrcpy.camera_zoom": 2.5})
    assert updated.scrcpy.enabled is True
    assert updated.camera.source == "scrcpy"
    assert updated.scrcpy.camera_zoom == 2.5


def test_apply_scrcpy_pan_left(monkeypatch):
    from processor.app import Processor

    class FakeMgr:
        def __init__(self):
            self._running = False

        @property
        def running(self):
            return self._running

        def status(self, cfg):
            from processor.utils.scrcpy import ScrcpyStatus

            return ScrcpyStatus(
                enabled=cfg.enabled,
                running=self._running,
                pid=1 if self._running else None,
                zoom=cfg.camera_zoom,
                view_zoom=cfg.view_zoom,
                pan_x=cfg.pan_x,
                pan_y=cfg.pan_y,
                crop="",
                camera_id=cfg.camera_id,
                camera_size=cfg.camera_size,
                camera_fps=cfg.camera_fps,
                v4l2_sink=cfg.v4l2_sink,
                binary=cfg.binary,
                last_error="",
                command=[],
            )

        def stop(self):
            self._running = False
            return {"ok": True, "running": False}

        def ensure_running(self, cfg):
            self._running = True
            return {"ok": True, "running": True, "pid": 1}

        def restart(self, cfg):
            self._running = True
            return {"ok": True, "running": True, "pid": 1, "pan_x": cfg.pan_x}

    config = Config.from_dict(
        {
            "camera": {"source": "scrcpy"},
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
        }
    )
    app = Processor(config)
    app._scrcpy = FakeMgr()
    monkeypatch.setattr(app, "_recreate_source_unlocked", lambda: {"ok": True})
    _use_synthetic_capture(app, monkeypatch)
    app.start()
    try:
        result = app.apply_scrcpy(action="pan_left")
        assert result["ok"] is True
        assert app.config.scrcpy.pan_x < 0
        assert app.config.scrcpy.view_zoom >= MIN_VIEW_ZOOM_FOR_PAN
    finally:
        app.shutdown()
