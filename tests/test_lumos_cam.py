"""Lumos Cam sidecar manager and processor hooks."""

from __future__ import annotations

from processor.config.loader import apply_updates, config_to_dict
from processor.config.schema import Config, LumosCamConfig
from processor.utils.lumos_cam import (
    MIN_APP_VERSION,
    PROTOCOL_VERSION,
    LumosCamManager,
    build_ffmpeg_command,
    step_lumos_pan,
    step_lumos_zoom,
)
from processor.utils.scrcpy import ZOOM_STEP


def test_adb_launch_error_detects_miui_type3():
    from processor.utils.lumos_cam import _adb_launch_error

    class Result:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    assert (
        _adb_launch_error(
            Result(
                0,
                "Starting: Intent { cmp=dev.lumos.cam/.MainActivity }\n"
                "Error type 3\n"
                "Error: Activity class {dev.lumos.cam/dev.lumos.cam.MainActivity} does not exist.\n",
            )
        )
        is not None
    )
    assert (
        _adb_launch_error(
            Result(0, "Error: Activity not started, unable to resolve Intent\n")
        )
        is not None
    )
    assert _adb_launch_error(Result(0, "Events injected: 1\n")) is None


def test_build_ffmpeg_command_rotates():
    cfg = LumosCamConfig(ffmpeg="/usr/bin/ffmpeg", v4l2_sink="/dev/video11")
    cmd = build_ffmpeg_command(cfg, binary="/usr/bin/ffmpeg", rotation=90)
    assert "-vf" in cmd and "transpose=1" in cmd


def test_build_ffmpeg_command_h264():
    cfg = LumosCamConfig(
        codec="h264",
        video_host_port=18766,
        v4l2_sink="/dev/video11",
        ffmpeg="/usr/bin/ffmpeg",
    )
    cmd = build_ffmpeg_command(cfg, binary="/usr/bin/ffmpeg")
    assert cmd[0] == "/usr/bin/ffmpeg"
    assert "-f" in cmd and "h264" in cmd
    assert "tcp://127.0.0.1:18766" in cmd
    assert "/dev/video11" in cmd


def test_build_ffmpeg_command_mjpeg():
    cfg = LumosCamConfig(codec="mjpeg", ffmpeg="/usr/bin/ffmpeg")
    cmd = build_ffmpeg_command(cfg, binary="/usr/bin/ffmpeg")
    assert "mjpeg" in cmd


def test_step_lumos_zoom_and_pan():
    cfg = LumosCamConfig(zoom_min=1.0, zoom_max=10.0, camera_zoom=1.0)
    assert abs(step_lumos_zoom(1.0, inward=True, cfg=cfg) - ZOOM_STEP) < 1e-6
    pan_x, pan_y = step_lumos_pan(cfg, direction="left")
    assert pan_x < 0
    assert pan_y == 0.0
    pan_x, pan_y = step_lumos_pan(LumosCamConfig(pan_x=-0.5, pan_y=0.2), direction="center")
    assert pan_x == 0.0 and pan_y == 0.0


def test_config_round_trip_includes_lumos_cam():
    config = Config.from_dict(
        {
            "lumos_cam": {
                "enabled": True,
                "camera_zoom": 1.5,
                "af": "locked",
                "v4l2_sink": "/dev/video11",
            }
        }
    )
    assert config.lumos_cam.enabled is True
    assert config.lumos_cam.camera_zoom == 1.5
    assert config.lumos_cam.af == "locked"
    data = config_to_dict(config)
    assert data["lumos_cam"]["package"] == "dev.lumos.cam"
    again = Config.from_dict(data)
    assert again.lumos_cam.control_host_port == 18765
    assert MIN_APP_VERSION.startswith("0.")
    assert PROTOCOL_VERSION == 1


def test_manager_start_stop_with_fakes(monkeypatch, tmp_path):
    sink = tmp_path / "video11"
    sink.write_text("")

    class FakeProc:
        def __init__(self):
            self.pid = 5150
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

    def fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "package:/data/app/dev.lumos.cam.apk\n"
            stderr = ""

        text = " ".join(str(c) for c in cmd)
        if "devices" in text:
            Result.stdout = "List of devices attached\nphone\tdevice\n"
        return Result()

    monkeypatch.setattr("processor.utils.lumos_cam.subprocess.Popen", fake_popen)
    monkeypatch.setattr("processor.utils.lumos_cam.subprocess.run", fake_run)
    monkeypatch.setattr("processor.utils.lumos_cam.sink_has_capture", lambda device: True)
    monkeypatch.setattr("processor.utils.lumos_cam.os.killpg", lambda *a, **k: None)
    monkeypatch.setattr("processor.utils.lumos_cam.adb_device_ready", lambda *a, **k: True)
    monkeypatch.setattr("processor.utils.lumos_cam.package_installed", lambda cfg: True)

    mgr = LumosCamManager()

    def fake_status():
        return {
            "ok": True,
            "protocol": 1,
            "app_version": "0.1.0",
            "zoom": 1.0,
            "af": "auto",
            "ae": "auto",
            "awb": "auto",
        }

    mgr.client.status = fake_status
    mgr.client.set_camera = lambda camera_id: fake_status()
    mgr.client.set_zoom = lambda ratio: fake_status()
    mgr.client.set_pan = lambda x, y: fake_status()
    mgr.client.set_locks = lambda **k: fake_status()
    mgr.client.set_stream = lambda cfg, enabled=True: fake_status()

    cfg = LumosCamConfig(
        enabled=True,
        v4l2_sink=str(sink),
        startup_timeout_sec=1.0,
        ffmpeg="/usr/bin/ffmpeg",
    )
    result = mgr.start(cfg)
    assert result["ok"] is True
    assert result["running"] is True
    assert "-f" in spawned["cmd"]
    assert mgr.stop()["running"] is False


def test_apply_lumos_zoom_is_live(monkeypatch):
    from processor.app import Processor

    live_calls = []

    class FakeMgr:
        def __init__(self):
            self._running = True

        @property
        def running(self):
            return self._running

        def status(self, cfg):
            from processor.utils.lumos_cam import LumosCamStatus

            return LumosCamStatus(
                enabled=cfg.enabled,
                running=self._running,
                pid=1 if self._running else None,
                zoom=cfg.camera_zoom,
                pan_x=cfg.pan_x,
                pan_y=cfg.pan_y,
                af=cfg.af,
                ae=cfg.ae,
                awb=cfg.awb,
                cal_mode=False,
                camera_id=cfg.camera_id,
                camera_size=cfg.camera_size,
                camera_fps=cfg.camera_fps,
                codec=cfg.codec,
                v4l2_sink=cfg.v4l2_sink,
                app_version="0.1.0",
                protocol=1,
                package_installed=True,
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
            live_calls.append(("restart", cfg.camera_zoom))
            return {"ok": True, "running": True, "pid": 1}

        def apply_live(self, cfg):
            live_calls.append(("live", round(cfg.camera_zoom, 4)))
            return {"ok": True, "running": True, "live": True}

        def set_cal_mode(self, enabled):
            live_calls.append(("cal", enabled))
            return {"ok": True, "cal_mode": enabled}

    config = Config.from_dict(
        {
            "camera": {"source": "synthetic", "replay_fps": 60},
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
            "lumos_cam": {"enabled": True, "bind_camera": False, "camera_zoom": 1.0},
        }
    )
    app = Processor(config)
    app._lumos = FakeMgr()
    monkeypatch.setattr(app, "_recreate_source_unlocked", lambda: {"ok": True})
    app.start()
    try:
        result = app.apply_lumos_cam(action="zoom_in")
        assert result["ok"] is True
        assert ("live", round(ZOOM_STEP, 4)) in live_calls
        assert not any(c[0] == "restart" for c in live_calls)
        assert abs(app.config.lumos_cam.camera_zoom - ZOOM_STEP) < 1e-6
    finally:
        app.shutdown()


def test_lumos_primary_skips_scrcpy(monkeypatch):
    from processor.app import Processor

    scrcpy_starts = []

    config = Config.from_dict(
        {
            "camera": {"source": "synthetic", "replay_fps": 60},
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
            "lumos_cam": {"enabled": True, "prefer_over_scrcpy": True, "bind_camera": False},
            "scrcpy": {"enabled": True, "bind_camera": False},
        }
    )
    app = Processor(config)
    monkeypatch.setattr(
        app,
        "_start_lumos_unlocked",
        lambda restart=False: {"ok": True, "running": True, "pid": 1},
    )
    monkeypatch.setattr(
        app,
        "_start_scrcpy_unlocked",
        lambda restart=False: scrcpy_starts.append(restart) or {"ok": True, "running": True},
    )
    app.start()
    try:
        assert scrcpy_starts == []
        assert app._lumos_is_primary() is True
    finally:
        app.shutdown()


def test_color_cal_toggles_lumos_cal_mode(monkeypatch, tmp_path):
    from processor.app import Processor

    calls = []

    class FakeMgr:
        running = True

        def status(self, cfg):
            from processor.utils.lumos_cam import LumosCamStatus

            return LumosCamStatus(
                enabled=True,
                running=True,
                pid=1,
                zoom=1.0,
                pan_x=0.0,
                pan_y=0.0,
                af="locked",
                ae="locked",
                awb="locked",
                cal_mode=bool(calls and calls[-1] is True),
                camera_id="0",
                camera_size="1920x1080",
                camera_fps=30,
                codec="h264",
                v4l2_sink="/dev/video11",
                app_version="0.1.0",
                protocol=1,
                package_installed=True,
                last_error="",
                command=[],
            )

        def stop(self):
            return {"ok": True, "running": False}

        def ensure_running(self, cfg):
            return {"ok": True, "running": True, "pid": 1}

        def restart(self, cfg):
            return {"ok": True, "running": True, "pid": 1}

        def set_cal_mode(self, enabled):
            calls.append(enabled)
            return {"ok": True, "cal_mode": enabled}

    config = Config.from_dict(
        {
            "camera": {"source": "synthetic", "replay_fps": 60},
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
            "boundary": {
                "mode": "manual",
                "corners": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
            },
            "lumos_cam": {"enabled": True, "bind_camera": False},
        }
    )
    app = Processor(config, config_path=tmp_path / "config.yaml")
    app._lumos = FakeMgr()
    app.start()
    try:
        import numpy as np

        app.state.set_corners(
            np.array([[32, 18], [288, 18], [288, 162], [32, 162]], dtype=np.float32),
            1.0,
            "manual",
        )
        started = app.start_color_calibration(mode="manual")
        assert started["ok"] is True
        assert started["lumos_cal_mode"] is True
        assert started["settle_sec"] == 0.5
        assert calls == [True]
        aborted = app.abort_color_calibration()
        assert aborted["ok"] is True
        assert calls == [True, False]
    finally:
        app.shutdown()
