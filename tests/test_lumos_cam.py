"""Lumos Cam sidecar manager and processor hooks."""

from __future__ import annotations

import os
import threading
import time

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


def _use_synthetic_capture(app, monkeypatch) -> None:
    from processor.camera.synthetic import SyntheticSource
    from processor.config.schema import CameraConfig

    monkeypatch.setattr(
        app,
        "_make_source_unlocked",
        lambda: SyntheticSource(CameraConfig(source="synthetic", replay_fps=60)),
    )


class _AliveEmptyFfmpeg:
    """ffmpeg still running; stdout never yields a full BGR frame."""

    def __init__(self, pid: int = 5151) -> None:
        self.pid = pid
        self.returncode = None
        self._r, self._w = os.pipe()
        os.set_blocking(self._r, False)
        self.stdout = os.fdopen(self._r, "rb", buffering=0)
        self.stderr = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0
        self._close_write()

    def kill(self):
        self.returncode = -9
        self._close_write()

    def wait(self, timeout=None):
        self.returncode = self.returncode if self.returncode is not None else 0
        self._close_write()
        return self.returncode

    def communicate(self, timeout=None):
        self._close_write()
        return ("", None)

    def _close_write(self) -> None:
        w, self._w = self._w, None
        if w is not None:
            try:
                os.close(w)
            except OSError:
                pass


def test_resolve_adb_serial_follows_wireless_port(monkeypatch):
    from processor.utils import lumos_cam as mod

    monkeypatch.setattr(
        mod,
        "list_adb_serials",
        lambda adb="adb": ["192.168.1.243:37847"],
    )
    cfg = LumosCamConfig(serial="192.168.1.243:40189")
    assert mod.resolve_adb_serial(cfg) == "192.168.1.243:37847"


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
    cfg = LumosCamConfig(ffmpeg="/usr/bin/ffmpeg")
    cmd = build_ffmpeg_command(cfg, binary="/usr/bin/ffmpeg", rotation=90)
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("transpose=1,scale=")
    from processor.utils.lumos_cam import output_frame_size

    assert output_frame_size(cfg, 90) == (720, 1280)
    assert output_frame_size(cfg, 0) == (1280, 720)
    assert output_frame_size(cfg, 0, max_edge=0) == (1920, 1080)


def test_build_ffmpeg_command_h264():
    cfg = LumosCamConfig(
        codec="h264",
        video_host_port=18766,
        ffmpeg="/usr/bin/ffmpeg",
        startup_timeout_sec=15.0,
    )
    cmd = build_ffmpeg_command(cfg, binary="/usr/bin/ffmpeg")
    assert cmd[0] == "/usr/bin/ffmpeg"
    assert "-f" in cmd and "h264" in cmd
    assert "tcp://127.0.0.1:18766" in cmd
    assert "mjpeg" in cmd
    assert "pipe:1" in cmd
    assert "-flush_packets" in cmd
    assert "nobuffer" not in cmd
    assert "/dev/video11" not in cmd
    assert "-analyzeduration" in cmd
    assert "-probesize" in cmd
    assert "-timeout" not in cmd
    assert "-use_wallclock_as_timestamps" in cmd
    assert "-framerate" in cmd and "30" in cmd
    assert "-fps_mode" in cmd and "passthrough" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("scale=")
    assert "force_original_aspect_ratio=decrease" in vf


def test_build_ffmpeg_command_mjpeg():
    cfg = LumosCamConfig(codec="mjpeg", ffmpeg="/usr/bin/ffmpeg")
    cmd = build_ffmpeg_command(cfg, binary="/usr/bin/ffmpeg")
    assert "mjpeg" in cmd


def test_build_ffmpeg_command_flips():
    cfg = LumosCamConfig(ffmpeg="/usr/bin/ffmpeg")
    cmd = build_ffmpeg_command(cfg, binary="/usr/bin/ffmpeg", flip_h=True, flip_v=True)
    vf = cmd[cmd.index("-vf") + 1]
    assert "hflip" in vf
    assert "vflip" in vf


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
            self.pid = 2_147_483_647
            self.returncode = None
            self.stdout = None
            self.stderr = None

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
        startup_timeout_sec=1.0,
        ffmpeg="/usr/bin/ffmpeg",
    )
    result = mgr.start(cfg)
    assert result["ok"] is True
    assert result["running"] is True
    assert "-f" in spawned["cmd"]
    assert mgr.stop()["running"] is False


def test_manager_start_fails_when_loopback_has_no_producer(monkeypatch, tmp_path):
    sink = tmp_path / "video11"
    sink.write_text("")

    def fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "package:/data/app/dev.lumos.cam.apk\n"
            stderr = ""

        text = " ".join(str(c) for c in cmd)
        if "devices" in text:
            Result.stdout = "List of devices attached\nphone\tdevice\n"
        return Result()

    monkeypatch.setattr(
        "processor.utils.lumos_cam.subprocess.Popen", lambda *a, **k: _AliveEmptyFfmpeg()
    )
    monkeypatch.setattr("processor.utils.lumos_cam.subprocess.run", fake_run)
    monkeypatch.setattr("processor.utils.lumos_cam.os.killpg", lambda *a, **k: None)
    monkeypatch.setattr("processor.utils.lumos_cam.adb_device_ready", lambda *a, **k: True)
    monkeypatch.setattr("processor.utils.lumos_cam.package_installed", lambda cfg: True)

    mgr = LumosCamManager()

    def fake_status():
        return {
            "ok": True,
            "protocol": 1,
            "app_version": "0.1.0",
            "streaming": True,
            "video_clients": 0,
            "bytes_sent": 0,
        }

    mgr.client.status = fake_status
    mgr.client.set_camera = lambda camera_id: fake_status()
    mgr.client.set_zoom = lambda ratio: fake_status()
    mgr.client.set_pan = lambda x, y: fake_status()
    mgr.client.set_locks = lambda **k: fake_status()
    mgr.client.set_stream = lambda cfg, enabled=True: fake_status()

    result = mgr.start(
        LumosCamConfig(
            enabled=True,
            startup_timeout_sec=0.6,
            ffmpeg="/usr/bin/ffmpeg",
        )
    )
    assert result["ok"] is True
    assert result["running"] is True
    assert result["ready"] is False
    assert "no decoded frames" in (result.get("error") or "")


def test_manager_start_fails_when_phone_sends_no_bytes(monkeypatch, tmp_path):
    sink = tmp_path / "video11"
    sink.write_text("")

    def fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "package:/data/app/dev.lumos.cam.apk\n"
            stderr = ""

        text = " ".join(str(c) for c in cmd)
        if "devices" in text:
            Result.stdout = "List of devices attached\nphone\tdevice\n"
        return Result()

    monkeypatch.setattr(
        "processor.utils.lumos_cam.subprocess.Popen",
        lambda *a, **k: _AliveEmptyFfmpeg(pid=2_147_483_646),
    )
    monkeypatch.setattr("processor.utils.lumos_cam.subprocess.run", fake_run)
    monkeypatch.setattr("processor.utils.lumos_cam.os.killpg", lambda *a, **k: None)
    monkeypatch.setattr("processor.utils.lumos_cam.adb_device_ready", lambda *a, **k: True)
    monkeypatch.setattr("processor.utils.lumos_cam.package_installed", lambda cfg: True)

    mgr = LumosCamManager()

    def fake_status():
        return {
            "ok": True,
            "protocol": 1,
            "app_version": "0.1.10",
            "streaming": True,
            "video_clients": 1,
            "bytes_sent": 0,
            "encoder_attached": False,
        }

    mgr.client.status = fake_status
    mgr.client.set_camera = lambda camera_id: fake_status()
    mgr.client.set_zoom = lambda ratio: fake_status()
    mgr.client.set_pan = lambda x, y: fake_status()
    mgr.client.set_locks = lambda **k: fake_status()
    mgr.client.set_stream = lambda cfg, enabled=True: fake_status()

    result = mgr.start(
        LumosCamConfig(
            enabled=True,
            startup_timeout_sec=0.6,
            ffmpeg="/usr/bin/ffmpeg",
        )
    )
    assert result["ok"] is True
    assert result["running"] is True
    assert result["ready"] is False
    assert "bytes_sent=0" in (result.get("error") or "")


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
            "camera": {"source": "lumos"},
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
            "lumos_cam": {"camera_zoom": 1.0},
        }
    )
    app = Processor(config)
    app._lumos = FakeMgr()
    monkeypatch.setattr(app, "_recreate_source_unlocked", lambda: {"ok": True})
    _use_synthetic_capture(app, monkeypatch)
    app.start()
    try:
        result = app.apply_lumos_cam(action="zoom_in")
        assert result["ok"] is True
        assert ("live", round(ZOOM_STEP, 4)) in live_calls
        assert not any(c[0] == "restart" for c in live_calls)
        assert abs(app.config.lumos_cam.camera_zoom - ZOOM_STEP) < 1e-6
    finally:
        app.shutdown()


def test_enable_lumos_reopens_bound_camera(monkeypatch):
    from processor.app import Processor

    recreates = []

    class FakeMgr:
        def __init__(self):
            self._running = False

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
                app_version="0.1.0",
                protocol=1,
                package_installed=True,
                last_error="",
                command=[],
            )

        def stop(self):
            self._running = False
            return {"ok": True, "running": False}

        def restart(self, cfg):
            self._running = True
            return {"ok": True, "running": True, "pid": 1}

        def ensure_running(self, cfg):
            self._running = True
            return {"ok": True, "running": True, "pid": 1}

        def apply_live(self, cfg):
            return {"ok": True, "running": True, "live": True}

    config = Config.from_dict(
        {
            "camera": {"source": "synthetic", "replay_fps": 60},
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
            "lumos_cam": {"enabled": False},
        }
    )
    app = Processor(config)
    app._lumos = FakeMgr()
    monkeypatch.setattr(
        app,
        "_recreate_source_unlocked",
        lambda: recreates.append(1) or {"ok": True},
    )
    app.start()
    try:
        result = app.apply_lumos_cam({"enabled": True}, action="apply")
        assert result["ok"] is True
        assert recreates == [1]
        assert app.config.camera.device != "/dev/video11"
    finally:
        app.shutdown()


def test_apply_lumos_skips_ffmpeg_restart_when_stream_unchanged(monkeypatch):
    from processor.app import Processor

    recreates = []

    class FakeMgr:
        def __init__(self):
            self._running = True
            self.restarts = 0
            self.live_calls = 0

        @property
        def running(self):
            return self._running

        def status(self, cfg):
            from processor.utils.lumos_cam import LumosCamStatus

            return LumosCamStatus(
                enabled=cfg.enabled,
                running=self._running,
                pid=1,
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
                app_version="0.1.0",
                protocol=1,
                package_installed=True,
                last_error="",
                command=[],
            )

        def stop(self):
            self._running = False
            return {"ok": True, "running": False}

        def restart(self, cfg):
            self.restarts += 1
            self._running = True
            return {"ok": True, "running": True, "pid": 1, "ready": True}

        def ensure_running(self, cfg):
            return {"ok": True, "running": True, "pid": 1, "ready": True}

        def apply_live(self, cfg):
            self.live_calls += 1
            return {"ok": True, "running": True, "live": True}

    config = Config.from_dict(
        {
            "camera": {"source": "lumos"},
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
            "lumos_cam": {
                "serial": "phone",
                "camera_id": "0",
                "camera_zoom": 1.0,
            },
        }
    )
    app = Processor(config)
    mgr = FakeMgr()
    app._lumos = mgr
    monkeypatch.setattr(
        app,
        "_recreate_source_unlocked",
        lambda: recreates.append(1) or {"ok": True},
    )
    _use_synthetic_capture(app, monkeypatch)
    app.start()
    try:
        result = app.apply_lumos_cam(
            {
                "enabled": True,
                "serial": "phone",
                "camera_id": "0",
                "camera_zoom": 1.5,
                "af": "locked",
            },
            action="apply",
        )
        assert result["ok"] is True
        assert mgr.restarts == 0
        assert mgr.live_calls == 1
        assert recreates == []
        assert app.config.lumos_cam.camera_zoom == 1.5
        assert app.config.lumos_cam.af == "locked"
    finally:
        app.shutdown()


def test_enable_lumos_starts_pipe_before_first_frame(monkeypatch):
    from processor.app import Processor

    recreates = []

    class FakeMgr:
        def __init__(self):
            self._running = False

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
                app_version="0.1.0",
                protocol=1,
                package_installed=True,
                last_error="",
                command=[],
            )

        def stop(self):
            self._running = False
            return {"ok": True, "running": False}

        def restart(self, cfg):
            self._running = True
            return {"ok": True, "running": True, "pid": 1, "ready": False}

        def ensure_running(self, cfg):
            return self.restart(cfg)

        def apply_live(self, cfg):
            return {"ok": True, "running": True, "live": True}

    config = Config.from_dict(
        {
            "camera": {"source": "synthetic", "replay_fps": 60},
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
            "lumos_cam": {"enabled": False},
        }
    )
    app = Processor(config)
    app._lumos = FakeMgr()
    monkeypatch.setattr(
        app,
        "_recreate_source_unlocked",
        lambda: recreates.append(1) or {"ok": True},
    )
    app.start()
    try:
        result = app.apply_lumos_cam({"enabled": True}, action="apply")
        assert result["ok"] is True
        assert recreates == [1]
        assert app.config.camera.device != "/dev/video11"
    finally:
        app.shutdown()


def test_make_source_uses_lumos_pipe_when_source_is_lumos():
    from processor.app import Processor
    from processor.camera.lumos import LumosPipeSource
    from processor.camera.v4l2 import V4l2Source

    class FakeMgr:
        running = False

        def read_bgr(self, timeout=1.0):
            return None

        def frame_size(self):
            return (1920, 1080)

        def stop(self):
            return {"ok": True, "running": False}

        def ensure_running(self, cfg):
            return {"ok": False, "running": False}

        def status(self, cfg):
            from processor.utils.lumos_cam import LumosCamStatus

            return LumosCamStatus(
                enabled=cfg.enabled,
                running=False,
                pid=None,
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
                app_version="0.1.0",
                protocol=1,
                package_installed=True,
                last_error="",
                command=[],
            )

    config = Config.from_dict(
        {
            "camera": {
                "source": "v4l2",
                "device": "/dev/video11",
                "capture_width": 640,
                "capture_height": 480,
            },
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
            "lumos_cam": {
                "enabled": True,
            },
        }
    )
    app = Processor(config)
    app._lumos = FakeMgr()
    assert config.camera.source == "lumos"
    assert config.camera.device == ""
    source = app._make_source_unlocked()
    assert isinstance(source, LumosPipeSource)
    assert not isinstance(source, V4l2Source)


def test_lumos_primary_skips_scrcpy(monkeypatch):
    from processor.app import Processor

    scrcpy_starts = []

    config = Config.from_dict(
        {
            "camera": {"source": "lumos"},
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
            "scrcpy": {"enabled": True},
        }
    )
    app = Processor(config)
    _use_synthetic_capture(app, monkeypatch)
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
            "camera": {"source": "lumos"},
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
            "boundary": {
                "mode": "manual",
                "corners": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
            },
        }
    )
    app = Processor(config, config_path=tmp_path / "config.yaml")
    app._lumos = FakeMgr()
    _use_synthetic_capture(app, monkeypatch)
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


def _jpeg_bytes(width: int, height: int, value: int) -> bytes:
    import cv2
    import numpy as np

    image = np.full((height, width, 3), int(value), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    assert ok
    return buf.tobytes()


def test_read_bgr_assembles_chunked_pipe():
    mgr = LumosCamManager()
    payload = _jpeg_bytes(64, 48, 40)
    r, w = os.pipe()
    os.set_blocking(r, False)

    class Proc:
        pid = 99
        returncode = None
        stdout = os.fdopen(r, "rb", buffering=0)
        stderr = None

        def poll(self):
            return None

    mgr._proc = Proc()
    mgr._frame_wh = (64, 48)

    def writer() -> None:
        view = memoryview(payload)
        off = 0
        while off < len(view):
            n = os.write(w, view[off : off + 64])
            off += n
            time.sleep(0.0005)
        os.close(w)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        image = mgr.read_bgr(timeout=2.0)
        assert image is not None
        assert image.shape[0] == 48
        assert image.shape[1] == 64
    finally:
        thread.join(timeout=2.0)
        try:
            mgr._proc.stdout.close()
        except OSError:
            pass


def test_read_bgr_keeps_newest_queued_frame():
    mgr = LumosCamManager()
    older = _jpeg_bytes(8, 4, 1)
    newer = _jpeg_bytes(8, 4, 200)
    r, w = os.pipe()
    os.set_blocking(r, False)
    os.write(w, older + newer)

    class Proc:
        pid = 100
        returncode = None
        stdout = os.fdopen(r, "rb", buffering=0)
        stderr = None

        def poll(self):
            return None

    mgr._proc = Proc()
    mgr._frame_wh = (8, 4)
    try:
        image = mgr.read_bgr(timeout=1.0)
        assert image is not None
        assert image.mean() > 100
    finally:
        os.close(w)
        try:
            mgr._proc.stdout.close()
        except OSError:
            pass


def test_cli_allows_empty_camera_device_when_lumos_owns_capture():
    from processor.cli import _require_camera_identity

    config = Config.from_dict(
        {
            "camera": {"source": "lumos", "device": ""},
        }
    )
    assert config.camera.source == "lumos"
    _require_camera_identity(config)

    migrated = Config.from_dict(
        {
            "camera": {"source": "v4l2", "device": ""},
            "lumos_cam": {"enabled": True, "bind_camera": True},
        }
    )
    assert migrated.camera.source == "lumos"
    _require_camera_identity(migrated)

    bare = Config.from_dict({"camera": {"source": "v4l2", "device": ""}})
    try:
        _require_camera_identity(bare)
    except SystemExit as exc:
        assert "USB camera" in str(exc)
    else:
        raise AssertionError("expected SystemExit when Lumos Cam is off")


def test_apply_camera_source_lumos_starts_sidecar_and_v4l2_stops_it(monkeypatch):
    from processor.app import Processor
    from processor.utils.lumos_cam import LumosCamStatus

    class FakeMgr:
        def __init__(self):
            self._running = False
            self.starts = 0

        @property
        def running(self):
            return self._running

        def status(self, cfg):
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
                app_version="0.1.0",
                protocol=1,
                package_installed=True,
                last_error="",
                command=[],
            )

        def stop(self):
            self._running = False
            return {"ok": True, "running": False}

        def restart(self, cfg):
            self._running = True
            self.starts += 1
            return {"ok": True, "running": True, "pid": 1, "ready": True}

        def ensure_running(self, cfg):
            return self.restart(cfg)

        def apply_live(self, cfg):
            return {"ok": True, "running": True, "live": True}

    config = Config.from_dict(
        {
            "camera": {"source": "synthetic", "replay_fps": 60},
            "output": {"width": 320, "height": 180, "fps": 30, "v4l2": {"enabled": False}},
            "logging": {"stats_interval": 0},
        }
    )
    app = Processor(config)
    app._lumos = FakeMgr()
    _use_synthetic_capture(app, monkeypatch)
    app.start()
    try:
        result = app.apply_camera_source({"source": "lumos", "serial": "phone"})
        assert result["ok"] is True
        assert app.config.camera.source == "lumos"
        assert app.config.lumos_cam.enabled is True
        assert app.config.lumos_cam.serial == "phone"
        assert app._lumos.starts >= 1
        assert app._lumos.running is True

        again = app.apply_camera_source({"source": "v4l2", "device": "/dev/video2"})
        assert again["ok"] is True
        assert app.config.camera.source == "v4l2"
        assert app.config.lumos_cam.enabled is False
        assert app._lumos.running is False
    finally:
        app.shutdown()

