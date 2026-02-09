"""GoPro camera manager using goprocam for wireless control.

Manages 2x GoPro Hero 7 Silver and 2x GoPro Hero 5 Session cameras.
Each camera requires its own WiFi adapter connected to the camera's WiFi network.
"""

import threading
import time
from dataclasses import dataclass
from typing import Optional

from goprocam import GoProCamera, constants


@dataclass
class GoProConfig:
    id: str
    name: str
    model: str  # "hero7_silver" or "hero5_session"
    wifi_interface: str  # Network interface connected to this camera's WiFi
    ip_address: str = "10.5.5.9"  # Default GoPro WiFi IP
    enabled: bool = True


class GoProCam:
    """Wrapper for a single GoPro camera."""

    def __init__(self, config: GoProConfig):
        self.config = config
        self._camera: Optional[GoProCamera.GoPro] = None
        self._connected = False

    def connect(self) -> bool:
        """Connect to the GoPro camera via its WiFi network."""
        print(f"Connecting to GoPro: {self.config.name} ({self.config.model}) "
              f"on interface {self.config.wifi_interface}...")
        try:
            self._camera = GoProCamera.GoPro(
                ip_address=self.config.ip_address,
                camera=constants.gpcontrol,
                api_type=constants.ApiServerType.SMARTY,
            )
            # Verify connection by getting camera info
            model_name = self._camera.infoCamera(constants.Camera.Name)
            print(f"Connected to GoPro: {self.config.name} - Model: {model_name}")
            self._connected = True
            return True
        except Exception as e:
            print(f"ERROR: Failed to connect to GoPro {self.config.name}: {e}")
            self._connected = False
            return False

    def set_video_mode(self):
        """Set the camera to video recording mode."""
        if not self._connected or self._camera is None:
            return
        try:
            self._camera.mode(constants.Mode.VideoMode, constants.Mode.SubMode.Video.Video)
            print(f"GoPro {self.config.name}: Set to video mode")
        except Exception as e:
            print(f"ERROR: Failed to set video mode on {self.config.name}: {e}")

    def start_recording(self):
        """Start video recording."""
        if not self._connected or self._camera is None:
            return
        try:
            self._camera.shutter(constants.start)
            print(f"GoPro {self.config.name}: Recording started")
        except Exception as e:
            print(f"ERROR: Failed to start recording on {self.config.name}: {e}")

    def stop_recording(self):
        """Stop video recording."""
        if not self._connected or self._camera is None:
            return
        try:
            self._camera.shutter(constants.stop)
            print(f"GoPro {self.config.name}: Recording stopped")
        except Exception as e:
            print(f"ERROR: Failed to stop recording on {self.config.name}: {e}")

    def is_recording(self) -> bool:
        """Check if camera is currently recording."""
        if not self._connected or self._camera is None:
            return False
        try:
            return self._camera.IsRecording() == 1
        except Exception:
            return False

    def get_battery(self) -> Optional[str]:
        """Get battery percentage."""
        if not self._connected or self._camera is None:
            return None
        try:
            return self._camera.getStatus(
                constants.Status.Status, constants.Status.STATUS.BattPercent
            )
        except Exception:
            return None

    def keep_alive(self):
        """Send keep-alive signal to prevent WiFi disconnect."""
        if not self._connected or self._camera is None:
            return
        try:
            self._camera.KeepAlive()
        except Exception:
            pass

    def disconnect(self):
        """Power off and disconnect from the camera."""
        if self._camera is not None:
            try:
                if self.is_recording():
                    self.stop_recording()
            except Exception:
                pass
            self._connected = False
            self._camera = None
            print(f"GoPro {self.config.name}: Disconnected")


class GoProManager:
    """Manages multiple GoPro cameras with threaded control.

    Each camera requires a separate WiFi adapter connected to that camera's
    WiFi network. All cameras share the default GoPro IP (10.5.5.9) but are
    accessed through different network interfaces.
    """

    def __init__(self, gopro_configs: list[dict]):
        self.cameras: dict[str, GoProCam] = {}
        for cfg in gopro_configs:
            if cfg.get("enabled", True):
                config = GoProConfig(
                    id=cfg["id"],
                    name=cfg["name"],
                    model=cfg["model"],
                    wifi_interface=cfg["wifi_interface"],
                    ip_address=cfg.get("ip_address", "10.5.5.9"),
                    enabled=cfg.get("enabled", True),
                )
                self.cameras[config.id] = GoProCam(config)

    def connect_all(self) -> bool:
        """Connect to all GoPro cameras.

        Uses threading to connect to multiple cameras in parallel since each
        camera is on a different WiFi interface.
        """
        if not self.cameras:
            print("No GoPro cameras configured")
            return True

        print(f"\nConnecting to {len(self.cameras)} GoPro cameras...")
        results = {}
        threads = []

        def connect_cam(cam_id, cam):
            results[cam_id] = cam.connect()

        for cam_id, cam in self.cameras.items():
            t = threading.Thread(target=connect_cam, args=(cam_id, cam))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=30.0)

        # Set all cameras to video mode after connecting
        for cam_id, cam in self.cameras.items():
            if results.get(cam_id, False):
                cam.set_video_mode()

        connected = sum(1 for v in results.values() if v)
        total = len(self.cameras)
        print(f"GoPro cameras connected: {connected}/{total}")

        return connected == total

    def start_recording_all(self):
        """Trigger all GoPro cameras to start recording simultaneously.

        Uses threading to minimize the delay between camera triggers.
        """
        print("\nTriggering all GoPro cameras to start recording...")
        threads = []
        for cam in self.cameras.values():
            t = threading.Thread(target=cam.start_recording)
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=10.0)

        print("All GoPro cameras triggered to record")

    def stop_recording_all(self):
        """Stop recording on all GoPro cameras simultaneously."""
        print("\nStopping all GoPro cameras...")
        threads = []
        for cam in self.cameras.values():
            t = threading.Thread(target=cam.stop_recording)
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=10.0)

        print("All GoPro cameras stopped")

    def keep_alive_all(self):
        """Send keep-alive to all cameras."""
        for cam in self.cameras.values():
            cam.keep_alive()

    def disconnect_all(self):
        """Disconnect from all GoPro cameras."""
        for cam in self.cameras.values():
            cam.disconnect()
        print("All GoPro cameras disconnected")

    def get_status_all(self) -> dict:
        """Get status of all cameras."""
        status = {}
        for cam_id, cam in self.cameras.items():
            status[cam_id] = {
                "name": cam.config.name,
                "model": cam.config.model,
                "connected": cam._connected,
                "recording": cam.is_recording(),
                "battery": cam.get_battery(),
            }
        return status
