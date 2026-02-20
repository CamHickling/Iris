"""Pre-flight calibration tool to verify all devices are connected and working."""

import sys
import time
from typing import Optional

from .camera import Camera, CameraConfig
from .gopro import GoProCam, GoProConfig, GoProManager
from .heart_rate import PolarH10


class CalibrationTool:
    """Checks connectivity and status of all configured devices."""

    def __init__(self, settings: dict):
        self.settings = settings
        self._results: list[dict] = []

    def run(self) -> bool:
        """Run all device checks. Returns True if everything passes."""
        print("=" * 60)
        print("  CALIBRATION TOOL - Device Connectivity Check")
        print("=" * 60)

        self._check_cameras()
        self._check_gopros()
        self._check_heart_rate()
        self._check_microphone()
        self._print_summary()

        all_passed = all(r["status"] == "PASS" for r in self._results)
        return all_passed

    def _check_cameras(self):
        """Check each USB camera can connect and capture a frame."""
        cameras_cfg = self.settings.get("cameras", [])
        if not cameras_cfg:
            print("\nNo USB cameras configured.")
            return

        print(f"\n--- USB Cameras ({len(cameras_cfg)}) ---")

        for cfg in cameras_cfg:
            if not cfg.get("enabled", True):
                self._results.append({
                    "device": cfg["name"],
                    "type": "USB Camera",
                    "status": "SKIP",
                    "detail": "Disabled in config",
                })
                continue

            config = CameraConfig(
                id=cfg["id"],
                name=cfg["name"],
                device_index=cfg["device_index"],
                resolution=tuple(cfg["resolution"]),
                fps=cfg["fps"],
                enabled=cfg["enabled"],
            )
            camera = Camera(config)

            connected = camera.open()
            frame = None
            frame_shape = None

            if connected:
                frame = camera.read_frame()
                if frame is not None:
                    frame_shape = f"{frame.shape[1]}x{frame.shape[0]}"

            camera.close()

            passed = connected and frame is not None
            detail = []
            if connected:
                detail.append("connected")
            else:
                detail.append("connection failed")
            if frame is not None:
                detail.append(f"frame captured ({frame_shape})")
            elif connected:
                detail.append("frame capture failed")

            status = "PASS" if passed else "FAIL"
            print(f"  {cfg['name']}: {status} - {', '.join(detail)}")

            self._results.append({
                "device": cfg["name"],
                "type": "USB Camera",
                "status": status,
                "detail": ", ".join(detail),
            })

    def _check_gopros(self):
        """Check each GoPro camera can connect and report battery."""
        gopro_cfgs = self.settings.get("gopros", [])
        if not gopro_cfgs:
            print("\nNo GoPro cameras configured.")
            return

        enabled_cfgs = [c for c in gopro_cfgs if c.get("enabled", True)]
        print(f"\n--- GoPro Cameras ({len(enabled_cfgs)}) ---")

        # Use GoProManager for threaded parallel connection
        manager = GoProManager(gopro_cfgs)

        if not manager.cameras:
            print("  No enabled GoPro cameras.")
            return

        manager.connect_all()

        for cam_id, cam in manager.cameras.items():
            connected = cam.is_connected
            battery = cam.get_battery() if connected else None

            detail = []
            if connected:
                detail.append("connected")
                detail.append(f"model={cam.config.model}")
                if battery is not None:
                    detail.append(f"battery={battery}%")
                else:
                    detail.append("battery=unknown")
            else:
                detail.append("connection failed")

            status = "PASS" if connected else "FAIL"
            print(f"  {cam.config.name}: {status} - {', '.join(detail)}")

            self._results.append({
                "device": cam.config.name,
                "type": "GoPro",
                "status": status,
                "detail": ", ".join(detail),
            })

        manager.disconnect_all()

    def _check_heart_rate(self):
        """Check Polar H10 connectivity and signal."""
        hr_settings = self.settings.get("heart_rate", {})
        if not hr_settings.get("enabled", False):
            print("\nPolar H10: Disabled in config.")
            return

        print("\n--- Polar H10 Heart Rate Monitor ---")

        monitor = PolarH10(
            device_address=hr_settings.get("device_address"),
            ecg_enabled=False,  # Don't need ECG for calibration check
        )

        connected = monitor.connect()
        battery = monitor.battery_level if connected else None
        hr_signal = False

        if connected:
            # Start recording briefly to check for HR signal
            monitor.start_recording(phase="calibration_check")
            time.sleep(3.0)
            monitor.stop_recording()
            hr_signal = len(monitor.get_samples()) > 0

        detail = []
        if connected:
            detail.append("connected")
            if battery is not None:
                detail.append(f"battery={battery}%")
            detail.append("HR signal detected" if hr_signal else "no HR signal")
        else:
            detail.append("connection failed")

        status = "PASS" if connected and hr_signal else "FAIL"
        print(f"  Polar H10: {status} - {', '.join(detail)}")

        self._results.append({
            "device": "Polar H10",
            "type": "Heart Rate",
            "status": status,
            "detail": ", ".join(detail),
        })

        monitor.disconnect()

    def _check_microphone(self):
        """Check that the configured USB microphone can record audio."""
        mic_settings = self.settings.get("microphone", {})
        if not mic_settings.get("enabled", False):
            print("\nMicrophone: Disabled in config.")
            return

        print("\n--- Microphone ---")

        try:
            from .audio import AudioConfig, AudioRecorder, find_audio_device
            import tempfile
            import os

            device_name = mic_settings.get("device_name", "Tonor")
            device_index = mic_settings.get("device_index")

            if device_index is None:
                device_index = find_audio_device(device_name)

            if device_index is None:
                print(f"  Microphone '{device_name}': FAIL - device not found")
                self._results.append({
                    "device": f"Microphone ({device_name})",
                    "type": "Audio",
                    "status": "FAIL",
                    "detail": "device not found",
                })
                return

            config = AudioConfig(
                device_name=device_name,
                device_index=device_index,
                sample_rate=mic_settings.get("sample_rate", 44100),
                channels=mic_settings.get("channels", 1),
            )
            recorder = AudioRecorder(config)
            tmp_path = tempfile.mktemp(suffix=".wav")
            opened = recorder.open(tmp_path)
            has_signal = False

            if opened:
                recorder.start_recording()
                import time
                time.sleep(1.0)
                recorder.stop_recording()
                recorder.close()
                size = os.path.getsize(tmp_path)
                has_signal = size > 1000
                os.unlink(tmp_path)
            else:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

            detail = []
            if opened:
                detail.append(f"device {device_index}")
                detail.append("signal detected" if has_signal else "no signal")
            else:
                detail.append("failed to open")

            status = "PASS" if opened and has_signal else "FAIL"
            print(f"  Microphone ({device_name}): {status} - {', '.join(detail)}")

            self._results.append({
                "device": f"Microphone ({device_name})",
                "type": "Audio",
                "status": status,
                "detail": ", ".join(detail),
            })

        except ImportError:
            print("  Microphone: SKIP - sounddevice/soundfile not installed")
            self._results.append({
                "device": "Microphone",
                "type": "Audio",
                "status": "SKIP",
                "detail": "sounddevice/soundfile not installed",
            })
        except Exception as e:
            print(f"  Microphone: FAIL - {e}")
            self._results.append({
                "device": "Microphone",
                "type": "Audio",
                "status": "FAIL",
                "detail": str(e),
            })

    def _print_summary(self):
        """Print a formatted summary table."""
        print("\n" + "=" * 60)
        print("  CALIBRATION SUMMARY")
        print("=" * 60)

        if not self._results:
            print("  No devices configured.")
            return

        # Column widths
        name_w = max(len(r["device"]) for r in self._results) + 2
        type_w = max(len(r["type"]) for r in self._results) + 2

        header = f"  {'Device':<{name_w}} {'Type':<{type_w}} Status  Detail"
        print(header)
        print("  " + "-" * (len(header) - 2))

        for r in self._results:
            print(f"  {r['device']:<{name_w}} {r['type']:<{type_w}} {r['status']:<8}{r['detail']}")

        print()
        passed = sum(1 for r in self._results if r["status"] == "PASS")
        failed = sum(1 for r in self._results if r["status"] == "FAIL")
        skipped = sum(1 for r in self._results if r["status"] == "SKIP")
        total = len(self._results)

        print(f"  Results: {passed}/{total} passed", end="")
        if failed:
            print(f", {failed} failed", end="")
        if skipped:
            print(f", {skipped} skipped", end="")
        print()

        if failed == 0:
            print("  Overall: PASS - All devices ready")
        else:
            print("  Overall: FAIL - Some devices not available")

        print("=" * 60)
