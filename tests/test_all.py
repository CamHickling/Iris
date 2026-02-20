"""Comprehensive unit tests for CaptureExpert.

All hardware dependencies (OpenCV, goprocam, bleak) are mocked.
"""

import csv
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import numpy as np
import pytest


# ============================================================
# Module: src/utils.py
# ============================================================

class TestTimestampString:
    def test_timestamp_string_format(self):
        from src.utils import timestamp_string
        ts = timestamp_string()
        assert re.match(r"\d{8}_\d{6}$", ts), f"Unexpected format: {ts}"

    def test_timestamp_string_length(self):
        from src.utils import timestamp_string
        ts = timestamp_string()
        assert len(ts) == 15  # YYYYMMDD_HHMMSS


class TestEnsureDir:
    def test_ensure_dir_creates_directory(self, tmp_path):
        from src.utils import ensure_dir
        target = tmp_path / "a" / "b" / "c"
        result = ensure_dir(target)
        assert target.exists()
        assert target.is_dir()
        assert result == target

    def test_ensure_dir_existing(self, tmp_path):
        from src.utils import ensure_dir
        target = tmp_path / "existing"
        target.mkdir()
        result = ensure_dir(target)
        assert result == target


class TestTimer:
    def test_timer_start_stop(self):
        from src.utils import Timer
        t = Timer()
        t.start()
        time.sleep(0.05)
        # Check elapsed while still running
        assert t.elapsed >= 0.04
        t.stop()
        # Note: Timer.stop() sets _running=False, so elapsed returns 0.0 after stop
        assert t.elapsed == 0.0

    def test_timer_elapsed_when_stopped(self):
        from src.utils import Timer
        t = Timer()
        assert t.elapsed == 0.0

    def test_timer_reset(self):
        from src.utils import Timer
        t = Timer()
        t.start()
        time.sleep(0.05)
        t.reset()
        assert t.elapsed < 0.05


# ============================================================
# Module: src/phase.py
# ============================================================

class TestPhase:
    def _make_phase(self, duration=10.0, capture_interval_ms=None):
        from src.phase import Phase, PhaseConfig
        config = PhaseConfig(
            id="test_phase",
            name="Test Phase",
            duration_seconds=duration,
            capture_interval_ms=capture_interval_ms,
            instructions="Do the thing",
        )
        return Phase(config)

    def test_phase_lifecycle(self):
        from src.phase import PhaseStatus
        phase = self._make_phase(duration=1.0)
        assert phase.status == PhaseStatus.PENDING
        phase.start()
        assert phase.status == PhaseStatus.ACTIVE
        phase.complete()
        assert phase.status == PhaseStatus.COMPLETED

    def test_phase_update_completes(self):
        from src.phase import PhaseStatus
        phase = self._make_phase(duration=1.0)
        phase.start()
        done = phase.update(0.5)
        assert not done
        assert phase.status == PhaseStatus.ACTIVE
        done = phase.update(0.6)
        assert done
        assert phase.status == PhaseStatus.COMPLETED

    def test_phase_zero_duration(self):
        from src.phase import PhaseStatus
        phase = self._make_phase(duration=0)
        phase.start()
        done = phase.update(10.0)
        assert not done
        assert phase.status == PhaseStatus.ACTIVE

    def test_phase_skip(self):
        from src.phase import PhaseStatus
        phase = self._make_phase()
        phase.skip()
        assert phase.status == PhaseStatus.SKIPPED

    def test_phase_progress(self):
        phase = self._make_phase(duration=10.0)
        phase.start()
        phase.update(5.0)
        assert phase.progress == pytest.approx(0.5)

    def test_phase_progress_zero_duration(self):
        phase = self._make_phase(duration=0)
        phase.start()
        assert phase.progress == 0.0

    def test_should_capture_true(self):
        phase = self._make_phase(capture_interval_ms=100)
        assert phase.should_capture is True

    def test_should_capture_false(self):
        phase = self._make_phase(capture_interval_ms=None)
        assert phase.should_capture is False


# ============================================================
# Module: src/camera.py
# ============================================================

class TestCameraConfig:
    def test_device_index_stored(self):
        from src.camera import CameraConfig
        cfg = CameraConfig(
            id="cam1", name="Cam 1", device_index=2,
            resolution=(1920, 1080), fps=30, enabled=True,
        )
        assert cfg.device_index == 2


class TestCamera:
    def _make_camera(self):
        from src.camera import Camera, CameraConfig
        cfg = CameraConfig(
            id="cam1", name="Cam 1", device_index=0,
            resolution=(1920, 1080), fps=30, enabled=True,
        )
        return Camera(cfg)

    @patch("src.camera.cv2")
    def test_camera_open_close(self, mock_cv2):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cv2.VideoCapture.return_value = mock_cap

        camera = self._make_camera()
        assert camera.open() is True
        if sys.platform == "win32":
            mock_cv2.VideoCapture.assert_called_with(0, mock_cv2.CAP_DSHOW)
        else:
            mock_cv2.VideoCapture.assert_called_with(0)
        assert camera.is_open is True

        camera.close()
        mock_cap.release.assert_called_once()

    @patch("src.camera.cv2")
    def test_camera_open_failure(self, mock_cv2):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = False
        mock_cv2.VideoCapture.return_value = mock_cap

        camera = self._make_camera()
        assert camera.open() is False

    @patch("src.camera.cv2")
    def test_camera_read_frame_success(self, mock_cv2):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        fake_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        mock_cap.read.return_value = (True, fake_frame)
        mock_cv2.VideoCapture.return_value = mock_cap

        camera = self._make_camera()
        camera.open()
        frame = camera.read_frame()
        assert frame is not None
        assert frame.shape == (1080, 1920, 3)

    @patch("src.camera.cv2")
    def test_camera_read_frame_failure(self, mock_cv2):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.read.return_value = (False, None)
        mock_cv2.VideoCapture.return_value = mock_cap

        camera = self._make_camera()
        camera.open()
        frame = camera.read_frame()
        assert frame is None

    def test_camera_read_frame_not_opened(self):
        camera = self._make_camera()
        assert camera.read_frame() is None


class TestCameraManager:
    @patch("src.camera.cv2")
    def test_camera_manager_open_all(self, mock_cv2):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cv2.VideoCapture.return_value = mock_cap

        from src.camera import CameraManager
        configs = [
            {"id": "c1", "name": "Cam1", "device_index": 0,
             "resolution": [1920, 1080], "fps": 30, "enabled": True},
            {"id": "c2", "name": "Cam2", "device_index": 1,
             "resolution": [1920, 1080], "fps": 30, "enabled": True},
        ]
        mgr = CameraManager(configs)
        assert mgr.open_all() is True
        assert len(mgr.cameras) == 2

    @patch("src.camera.cv2")
    def test_camera_manager_capture_all(self, mock_cv2):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        fake_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        mock_cap.read.return_value = (True, fake_frame)
        mock_cv2.VideoCapture.return_value = mock_cap

        from src.camera import CameraManager
        configs = [
            {"id": "c1", "name": "Cam1", "device_index": 0,
             "resolution": [640, 480], "fps": 30, "enabled": True},
        ]
        mgr = CameraManager(configs)
        mgr.open_all()
        frames = mgr.capture_all()
        assert "c1" in frames
        assert frames["c1"].shape == (480, 640, 3)


# ============================================================
# Module: src/gopro.py
# ============================================================

class TestGoProConfig:
    def test_gopro_config_defaults(self):
        from src.gopro import GoProConfig
        cfg = GoProConfig(
            id="gp1", name="GoPro Front", model="hero7_silver",
            wifi_interface="wlan1",
        )
        assert cfg.ip_address == "10.5.5.9"
        assert cfg.enabled is True


class TestGoProCam:
    def _make_cam(self):
        from src.gopro import GoProCam, GoProConfig
        cfg = GoProConfig(
            id="gp1", name="GoPro Test", model="hero7_silver",
            wifi_interface="wlan1",
        )
        return GoProCam(cfg)

    @patch("src.gopro.GoProCamera")
    @patch("src.gopro.constants")
    def test_gopro_connect_disconnect(self, mock_constants, mock_gpc):
        mock_camera = MagicMock()
        mock_camera.infoCamera.return_value = "HERO7 Silver"
        mock_gpc.GoPro.return_value = mock_camera

        cam = self._make_cam()
        assert cam.connect() is True
        assert cam.is_connected is True

        cam.disconnect()
        assert cam.is_connected is False

    @patch("src.gopro.GoProCamera")
    @patch("src.gopro.constants")
    def test_gopro_recording(self, mock_constants, mock_gpc):
        mock_camera = MagicMock()
        mock_camera.infoCamera.return_value = "HERO7 Silver"
        mock_gpc.GoPro.return_value = mock_camera

        cam = self._make_cam()
        cam.connect()
        cam.start_recording()
        mock_camera.shutter.assert_called_once_with(mock_constants.start)

        cam.stop_recording()
        assert mock_camera.shutter.call_count == 2

    def test_gopro_is_connected_property_default(self):
        cam = self._make_cam()
        assert cam.is_connected is False


class TestGoProManager:
    @patch("src.gopro.GoProCamera")
    @patch("src.gopro.constants")
    def test_gopro_manager_connect_all(self, mock_constants, mock_gpc):
        mock_camera = MagicMock()
        mock_camera.infoCamera.return_value = "HERO7 Silver"
        mock_gpc.GoPro.return_value = mock_camera

        from src.gopro import GoProManager
        configs = [
            {"id": "gp1", "name": "GP1", "model": "hero7_silver",
             "wifi_interface": "wlan1", "enabled": True},
            {"id": "gp2", "name": "GP2", "model": "hero5_session",
             "wifi_interface": "wlan2", "enabled": True},
        ]
        mgr = GoProManager(configs)
        result = mgr.connect_all()
        assert result is True
        assert all(c.is_connected for c in mgr.cameras.values())

    @patch("src.gopro.GoProCamera")
    @patch("src.gopro.constants")
    def test_keep_alive_all_threaded(self, mock_constants, mock_gpc):
        mock_camera = MagicMock()
        mock_camera.infoCamera.return_value = "HERO7 Silver"
        mock_gpc.GoPro.return_value = mock_camera

        from src.gopro import GoProManager
        configs = [
            {"id": "gp1", "name": "GP1", "model": "hero7_silver",
             "wifi_interface": "wlan1", "enabled": True},
        ]
        mgr = GoProManager(configs)
        mgr.connect_all()
        mgr.keep_alive_all()
        mock_camera.KeepAlive.assert_called()


# ============================================================
# Module: src/heart_rate.py
# ============================================================

class TestParseHrMeasurement:
    def test_uint8_bpm_no_rr(self):
        from src.heart_rate import PolarH10
        # flags=0x00: uint8 BPM, no contact, no energy, no RR
        data = bytearray([0x00, 72])
        result = PolarH10._parse_hr_measurement(data)
        assert result["bpm"] == 72
        assert result["rr_intervals_ms"] == []
        assert result["sensor_contact"] is None

    def test_uint16_bpm(self):
        from src.heart_rate import PolarH10
        # flags=0x01: uint16 BPM
        data = bytearray([0x01, 0xC8, 0x00])  # 200 BPM
        result = PolarH10._parse_hr_measurement(data)
        assert result["bpm"] == 200

    def test_with_rr_intervals(self):
        from src.heart_rate import PolarH10
        # flags=0x10: uint8 BPM + RR present
        rr_raw = int(800 / 1000.0 * 1024)  # ~819
        data = bytearray([0x10, 65]) + rr_raw.to_bytes(2, "little")
        result = PolarH10._parse_hr_measurement(data)
        assert result["bpm"] == 65
        assert len(result["rr_intervals_ms"]) == 1
        assert abs(result["rr_intervals_ms"][0] - 800.0) < 1.0

    def test_sensor_contact_detected(self):
        from src.heart_rate import PolarH10
        # flags=0x06: contact supported + detected (bits 1-2 = 0b11)
        data = bytearray([0x06, 70])
        result = PolarH10._parse_hr_measurement(data)
        assert result["sensor_contact"] is True

    def test_sensor_contact_not_detected(self):
        from src.heart_rate import PolarH10
        # flags=0x04: contact supported but NOT detected (bits 1-2 = 0b10)
        data = bytearray([0x04, 70])
        result = PolarH10._parse_hr_measurement(data)
        assert result["sensor_contact"] is False

    def test_with_energy_expended(self):
        from src.heart_rate import PolarH10
        # flags=0x08: energy expended present (uint8 BPM)
        data = bytearray([0x08, 80, 0x00, 0x00])  # 2 bytes energy
        result = PolarH10._parse_hr_measurement(data)
        assert result["bpm"] == 80


class TestParseEcgData:
    def test_valid_ecg_data(self):
        from src.heart_rate import PolarH10
        # type=0x00 (ECG), followed by 8 bytes timestamp, then 3-byte samples
        data = bytearray(10 + 6)  # header + 2 samples
        data[0] = 0x00
        # timestamp bytes 1-8 left as 0
        # sample 1 at index 10
        data[10] = 0x01
        data[11] = 0x00
        data[12] = 0x00
        # sample 2 at index 13
        data[13] = 0xFF
        data[14] = 0xFF
        data[15] = 0xFF  # -1 in signed 3-byte
        result = PolarH10._parse_ecg_data(data)
        assert result is not None
        assert len(result["samples_uv"]) == 2
        assert result["samples_uv"][0] == 1
        assert result["samples_uv"][1] == -1

    def test_invalid_ecg_type(self):
        from src.heart_rate import PolarH10
        data = bytearray([0x01] + [0] * 15)
        result = PolarH10._parse_ecg_data(data)
        assert result is None


class TestHrSampleRecording:
    def test_samples_stored_with_phase(self):
        from src.heart_rate import PolarH10, HeartRateSample
        monitor = PolarH10.__new__(PolarH10)
        monitor._hr_samples = []
        monitor._ecg_samples = []
        monitor._samples_lock = threading.Lock()
        monitor._recording = True
        monitor._current_phase = "calibration"

        # Simulate HR callback
        data = bytearray([0x00, 72])
        monitor._hr_callback(None, data)
        assert len(monitor._hr_samples) == 1
        assert monitor._hr_samples[0].phase == "calibration"
        assert monitor._hr_samples[0].bpm == 72

    def test_samples_not_recorded_when_stopped(self):
        from src.heart_rate import PolarH10
        monitor = PolarH10.__new__(PolarH10)
        monitor._hr_samples = []
        monitor._ecg_samples = []
        monitor._samples_lock = threading.Lock()
        monitor._recording = False
        monitor._current_phase = "calibration"

        data = bytearray([0x00, 72])
        monitor._hr_callback(None, data)
        assert len(monitor._hr_samples) == 0


class TestSaveToCsv:
    def test_save_to_csv_format(self, tmp_path):
        from src.heart_rate import PolarH10, HeartRateSample
        monitor = PolarH10.__new__(PolarH10)
        monitor._samples_lock = threading.Lock()
        monitor._hr_samples = [
            HeartRateSample(timestamp=1000.0, bpm=72, rr_intervals_ms=[800.0, 810.5],
                            sensor_contact=True, phase="recording"),
            HeartRateSample(timestamp=1001.0, bpm=75, rr_intervals_ms=[],
                            sensor_contact=None, phase="recording"),
        ]

        filepath = tmp_path / "hr.csv"
        monitor.save_to_csv(filepath)

        with open(filepath) as f:
            reader = csv.reader(f)
            rows = list(reader)

        assert rows[0] == ["timestamp", "bpm", "rr_intervals_ms", "sensor_contact", "phase"]
        assert rows[1][1] == "72"
        assert rows[1][2] == "800.0;810.5"
        assert rows[2][2] == ""


class TestGetSummary:
    def test_per_phase_stats(self):
        from src.heart_rate import PolarH10, HeartRateSample
        monitor = PolarH10.__new__(PolarH10)
        monitor._samples_lock = threading.Lock()
        monitor._hr_samples = [
            HeartRateSample(timestamp=1.0, bpm=60, rr_intervals_ms=[1000.0],
                            sensor_contact=True, phase="rest"),
            HeartRateSample(timestamp=2.0, bpm=80, rr_intervals_ms=[750.0],
                            sensor_contact=True, phase="rest"),
            HeartRateSample(timestamp=3.0, bpm=120, rr_intervals_ms=[500.0],
                            sensor_contact=True, phase="exercise"),
        ]

        summary = monitor.get_summary()
        assert "rest" in summary
        assert "exercise" in summary
        assert summary["rest"]["min_bpm"] == 60
        assert summary["rest"]["max_bpm"] == 80
        assert summary["rest"]["avg_bpm"] == 70.0
        assert summary["rest"]["count"] == 2
        assert summary["exercise"]["avg_bpm"] == 120.0


# ============================================================
# Module: src/len_correction.py
# ============================================================

class TestUndistortVideo:
    @patch("src.len_correction.cv2")
    def test_undistort_video_correct_property_access(self, mock_cv2):
        """Verify the bug fix: cap.get() is called directly with cv2 constants,
        NOT wrapped in cv2.get()."""
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = False  # exit immediately
        mock_cap.get.side_effect = lambda prop: {
            mock_cv2.CAP_PROP_FRAME_WIDTH: 1920.0,
            mock_cv2.CAP_PROP_FRAME_HEIGHT: 1080.0,
            mock_cv2.CAP_PROP_FPS: 30.0,
        }.get(prop, 0.0)
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.getOptimalNewCameraMatrix.return_value = (MagicMock(), (0, 0, 1920, 1080))
        mock_cv2.VideoWriter_fourcc.return_value = 0x7634706D
        mock_writer = MagicMock()
        mock_cv2.VideoWriter.return_value = mock_writer

        from src.len_correction import undistort_video
        undistort_video("in.mp4", "out.mp4")

        # Verify cap.get was called with the constants directly (not via cv2.get)
        mock_cap.get.assert_any_call(mock_cv2.CAP_PROP_FRAME_WIDTH)
        mock_cap.get.assert_any_call(mock_cv2.CAP_PROP_FRAME_HEIGHT)

        # Verify cv2.get was NOT called (the bug)
        assert not hasattr(mock_cv2, 'get') or not mock_cv2.get.called

    @patch("src.len_correction.cv2")
    def test_undistort_video_headless(self, mock_cv2):
        """Verify no imshow/waitKey/destroyAllWindows calls in the processing path."""
        mock_cap = MagicMock()
        fake_frame = MagicMock()
        # Return one frame then stop
        mock_cap.isOpened.side_effect = [True, True]
        mock_cap.read.side_effect = [(True, fake_frame), (False, None)]
        mock_cap.get.side_effect = lambda prop: {
            mock_cv2.CAP_PROP_FRAME_WIDTH: 1920.0,
            mock_cv2.CAP_PROP_FRAME_HEIGHT: 1080.0,
            mock_cv2.CAP_PROP_FPS: 30.0,
            mock_cv2.CAP_PROP_FRAME_COUNT: 1.0,
        }.get(prop, 0.0)
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.getOptimalNewCameraMatrix.return_value = (MagicMock(), (0, 0, 1920, 1080))
        mock_cv2.VideoWriter_fourcc.return_value = 0x7634706D
        mock_cv2.VideoWriter.return_value = MagicMock()

        from src.len_correction import undistort_video
        undistort_video("in.mp4", "out.mp4")

        mock_cv2.imshow.assert_not_called()
        mock_cv2.waitKey.assert_not_called()
        mock_cv2.destroyAllWindows.assert_not_called()

    @patch("src.len_correction.cv2")
    @patch("src.len_correction.os")
    def test_process_directory(self, mock_os, mock_cv2):
        """Verify process_directory discovers .mp4 files and processes each."""
        mock_os.listdir.return_value = ["clip1.mp4", "clip2.MP4", "photo.jpg"]
        mock_os.path.join = os.path.join
        mock_os.path.splitext = os.path.splitext

        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = False  # exit immediately
        mock_cap.get.return_value = 0.0
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.getOptimalNewCameraMatrix.return_value = (MagicMock(), (0, 0, 0, 0))
        mock_cv2.VideoWriter_fourcc.return_value = 0
        mock_cv2.VideoWriter.return_value = MagicMock()

        from src.len_correction import process_directory
        process_directory("/videos", "/output")

        mock_os.makedirs.assert_called_once_with("/output", exist_ok=True)

        # Should have opened exactly 2 videos (clip1.mp4 and clip2.MP4, not photo.jpg)
        assert mock_cv2.VideoCapture.call_count == 2
        call_args_list = [c[0][0] for c in mock_cv2.VideoCapture.call_args_list]
        # Normalize path separators for cross-platform compatibility
        call_args_normalized = [p.replace("\\", "/") for p in call_args_list]
        assert "/videos/clip1.mp4" in call_args_normalized
        assert "/videos/clip2.MP4" in call_args_normalized


# ============================================================
# Module: src/experiment.py
# ============================================================

class TestExperiment:
    def _make_settings(self):
        return {
            "experiment": {
                "name": "Test Experiment",
                "output_dir": "/tmp/test_experiment",
            },
            "cameras": [],
            "gopros": [],
            "phases": [
                {
                    "id": "calibration",
                    "name": "Calibration",
                    "duration_seconds": 30,
                    "capture_interval_ms": 1000,
                    "instructions": "Stand still",
                },
                {
                    "id": "recording",
                    "name": "Recording",
                    "duration_seconds": 60,
                    "capture_interval_ms": 500,
                    "instructions": "Move around",
                },
            ],
            "heart_rate": {"enabled": False},
        }

    @patch("src.experiment.PolarH10")
    @patch("src.experiment.GoProManager")
    @patch("src.experiment.CameraManager")
    def test_experiment_setup(self, mock_cm, mock_gm, mock_hr):
        from src.experiment import Experiment
        settings = self._make_settings()
        exp = Experiment(settings)
        assert exp.name == "Test Experiment"
        assert len(exp.phases) == 2
        assert exp.current_phase_index == 0

    @patch("src.experiment.PolarH10")
    @patch("src.experiment.GoProManager")
    @patch("src.experiment.CameraManager")
    def test_experiment_phase_advancement(self, mock_cm, mock_gm, mock_hr):
        from src.experiment import Experiment
        settings = self._make_settings()
        exp = Experiment(settings)
        assert exp.current_phase.config.id == "calibration"
        result = exp.next_phase()
        assert result is True
        assert exp.current_phase.config.id == "recording"
        result = exp.next_phase()
        assert result is False

    @patch("src.experiment.PolarH10")
    @patch("src.experiment.GoProManager")
    @patch("src.experiment.CameraManager")
    def test_load_phases(self, mock_cm, mock_gm, mock_hr):
        from src.experiment import Experiment
        settings = self._make_settings()
        exp = Experiment(settings)
        assert exp.phases[0].config.id == "calibration"
        assert exp.phases[0].config.duration_seconds == 30
        assert exp.phases[1].config.capture_interval_ms == 500

    @patch("src.experiment.cv2")
    @patch("src.experiment.PolarH10")
    @patch("src.experiment.GoProManager")
    @patch("src.experiment.CameraManager")
    def test_save_frames(self, mock_cm, mock_gm, mock_hr, mock_cv2, tmp_path):
        from src.experiment import Experiment
        settings = self._make_settings()
        settings["experiment"]["output_dir"] = str(tmp_path / "output")
        exp = Experiment(settings)

        # Set up a phase
        phase = exp.phases[0]
        phase.start()
        phase.frame_count = 3

        frames = {"cam1": np.zeros((100, 100, 3), dtype=np.uint8)}
        exp._save_frames(frames, phase)

        mock_cv2.imwrite.assert_called_once()
        call_args = mock_cv2.imwrite.call_args
        assert "cam1_000003.jpg" in call_args[0][0]


# ============================================================
# Module: main.py
# ============================================================

class TestLoadSettings:
    def test_load_settings(self, tmp_path):
        from main import load_settings
        config = {
            "experiment": {"name": "Test", "output_dir": "/tmp/out"},
            "cameras": [],
            "gopros": [],
            "phases": [],
        }
        config_path = tmp_path / "settings.json"
        config_path.write_text(json.dumps(config))

        result = load_settings(str(config_path))
        assert result["experiment"]["name"] == "Test"
        assert result["cameras"] == []
