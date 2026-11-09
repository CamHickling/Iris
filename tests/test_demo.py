"""Demo mode (python gui.py --demo) must stay isolated from real sessions."""

import time
from pathlib import Path

from src import demo
from src.camera import CameraConfig


class TestDemoSettings:
    def test_output_stays_in_demo_output(self):
        s = demo.build_settings()
        assert Path(s["experiment"]["output_dir"]) == demo.SESSIONS_DIR
        assert demo.DEMO_ROOT in demo.SESSIONS_DIR.parents
        assert s["experiment"]["backup_dir"] is None

    def test_settings_file_is_not_settings_json(self):
        assert demo.SETTINGS_PATH.name != "settings.json"
        assert demo.SETTINGS_PATH.parent == demo.DEMO_ROOT

    def test_every_device_enabled(self):
        s = demo.build_settings()
        assert all(c["enabled"] for c in s["cameras"])
        assert all(g["enabled"] for g in s["gopros"])
        assert s["heart_rate"]["enabled"] and s["microphone"]["enabled"]

    def test_demo_output_is_gitignored(self):
        gitignore = (demo.REPO_ROOT / ".gitignore").read_text()
        assert "demo_output/" in gitignore.splitlines()


class TestDemoCamera:
    def test_synthetic_frames(self):
        cam = demo.DemoCamera(CameraConfig(id="overhead_camera", name="Overhead", device_index=0,
                                           resolution=(320, 180), fps=30, enabled=True,
                                           role="overhead"))
        assert cam.read_frame() is None  # not open yet
        assert cam.open() and cam.is_open
        frame = cam.read_frame()
        assert frame.shape == (180, 320, 3)
        cam.close()
        assert not cam.is_open

    def test_manager_uses_demo_cameras(self):
        mgr = demo.DemoCameraManager(demo.build_settings()["cameras"])
        assert mgr.cameras and all(isinstance(c, demo.DemoCamera) for c in mgr.cameras.values())


class TestDemoDevices:
    def test_gopros_report_connected(self):
        mgr = demo.DemoGoProManager(demo.build_settings()["gopros"])
        assert mgr.connect_all()
        mgr.start_recording_all()
        assert all(st["recording"] for st in mgr.get_status_all().values())
        mgr.stop_recording_all()
        mgr.keep_alive_all()
        mgr.disconnect_all()

    def test_heart_rate_follows_phase(self, monkeypatch):
        monkeypatch.setattr(demo.time, "sleep", lambda s: None)
        hr = demo.DemoPolarH10()
        assert hr.connect()
        hr.start_recording(phase="performance")
        deadline = time.time() + 5
        while not hr.get_samples() and time.time() < deadline:
            time.sleep(0.1)
        hr.disconnect()
        samples = hr.get_samples()
        assert samples and samples[0].phase == "performance"
        assert 50 < samples[0].bpm < 200

    def test_calibration_passes_every_device(self, monkeypatch):
        monkeypatch.setattr(demo.time, "sleep", lambda s: None)
        tool = demo.DemoCalibrationTool(demo.build_settings())
        assert tool.run()
        assert len(tool._results) == 6
        assert all(r["status"] == "PASS" for r in tool._results)
