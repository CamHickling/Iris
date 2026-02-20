"""GoPro camera manager using goprocam for wireless control.

Manages multiple GoPro cameras, each controlled over WiFi via a dedicated
USB WiFi adapter. Uses source-address binding to route HTTP traffic to the
correct GoPro when multiple cameras share the same IP (10.5.5.9).
"""

import http.client
import platform
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

import urllib.request

from goprocam import GoProCamera, constants


def get_interface_ip(interface_name: str) -> Optional[str]:
    """Get the IPv4 address of a WiFi interface connected to a GoPro network.

    Looks for an IP in the 10.5.5.x subnet assigned by the GoPro's DHCP.
    Windows only; returns None on other platforms.
    """
    if platform.system() != "Windows":
        return None
    try:
        result = subprocess.run(
            ["netsh", "interface", "ip", "show", "addresses", interface_name],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            stripped = line.strip()
            # Match lines like "IP Address:  10.5.5.103"
            if "IP Address" in stripped or "IP address" in stripped:
                ip = stripped.rsplit(":", 1)[-1].strip()
                if ip.startswith("10.5.5."):
                    return ip
    except Exception:
        pass
    # Fallback: parse ipconfig output
    try:
        result = subprocess.run(
            ["ipconfig"], capture_output=True, text=True, timeout=5,
        )
        found_interface = False
        for line in result.stdout.splitlines():
            if interface_name in line:
                found_interface = True
            elif found_interface and "IPv4" in line:
                ip = line.rsplit(":", 1)[-1].strip()
                if ip.startswith("10.5.5."):
                    return ip
            elif found_interface and line.strip() == "":
                found_interface = False
    except Exception:
        pass
    return None


class _BoundHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that binds to a specific local source address."""

    def __init__(self, host, source_address=None, **kwargs):
        self._bind_address = source_address
        super().__init__(host, **kwargs)

    def connect(self):
        self.sock = socket.create_connection(
            (self.host, self.port),
            self.timeout,
            self._bind_address,
        )


class _BoundHTTPHandler(urllib.request.HTTPHandler):
    """urllib HTTP handler that routes connections through a specific interface."""

    def __init__(self, source_address):
        super().__init__()
        self._source_address = source_address

    def http_open(self, req):
        return self.do_open(
            lambda host, **kw: _BoundHTTPConnection(
                host, source_address=self._source_address, **kw
            ),
            req,
        )


@dataclass
class GoProConfig:
    id: str
    name: str
    model: str  # "hero7_silver" or "hero5_session"
    wifi_interface: str  # Network interface connected to this camera's WiFi
    ip_address: str = "10.5.5.9"  # Default GoPro WiFi IP
    enabled: bool = True


class GoProCam:
    """Wrapper for a single GoPro camera with interface-bound networking.

    Uses a class-level lock and per-instance urllib openers to ensure HTTP
    requests reach the correct GoPro when multiple cameras share the same IP.
    """

    # Class-level lock serializes all goprocam HTTP calls so that the correct
    # urllib opener is active for each request.
    _api_lock = threading.Lock()

    def __init__(self, config: GoProConfig):
        self.config = config
        self._camera: Optional[GoProCamera.GoPro] = None
        self._connected = False
        self._opener: Optional[urllib.request.OpenerDirector] = None

    @property
    def is_connected(self) -> bool:
        """Whether the camera is currently connected."""
        return self._connected

    def _install_opener(self):
        """Install this camera's bound urllib opener. Must hold _api_lock."""
        if self._opener is not None:
            urllib.request.install_opener(self._opener)

    def connect(self) -> bool:
        """Connect to the GoPro camera via its WiFi network."""
        print(f"Connecting to GoPro: {self.config.name} ({self.config.model}) "
              f"on interface {self.config.wifi_interface}...")

        # Find the local IP of the WiFi adapter for source-address binding
        source_ip = get_interface_ip(self.config.wifi_interface)
        if source_ip:
            print(f"  Interface {self.config.wifi_interface} -> local IP {source_ip}")
            handler = _BoundHTTPHandler((source_ip, 0))
            self._opener = urllib.request.build_opener(handler)
        else:
            print(f"  WARNING: Could not find IP for {self.config.wifi_interface}, "
                  f"using default routing")

        try:
            with GoProCam._api_lock:
                self._install_opener()
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
            with GoProCam._api_lock:
                self._install_opener()
                self._camera.mode(constants.Mode.VideoMode, constants.Mode.SubMode.Video.Video)
            print(f"GoPro {self.config.name}: Set to video mode")
        except Exception as e:
            print(f"ERROR: Failed to set video mode on {self.config.name}: {e}")

    def start_recording(self):
        """Start video recording."""
        if not self._connected or self._camera is None:
            return
        try:
            with GoProCam._api_lock:
                self._install_opener()
                self._camera.shutter(constants.start)
            print(f"GoPro {self.config.name}: Recording started")
        except Exception as e:
            print(f"ERROR: Failed to start recording on {self.config.name}: {e}")

    def stop_recording(self):
        """Stop video recording."""
        if not self._connected or self._camera is None:
            return
        try:
            with GoProCam._api_lock:
                self._install_opener()
                self._camera.shutter(constants.stop)
            print(f"GoPro {self.config.name}: Recording stopped")
        except Exception as e:
            print(f"ERROR: Failed to stop recording on {self.config.name}: {e}")

    def is_recording(self) -> bool:
        """Check if camera is currently recording."""
        if not self._connected or self._camera is None:
            return False
        try:
            with GoProCam._api_lock:
                self._install_opener()
                return self._camera.IsRecording() == 1
        except Exception:
            return False

    def get_battery(self) -> Optional[str]:
        """Get battery percentage."""
        if not self._connected or self._camera is None:
            return None
        try:
            with GoProCam._api_lock:
                self._install_opener()
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
            with GoProCam._api_lock:
                self._install_opener()
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
    """Manages multiple GoPro cameras with interface-bound networking.

    Each camera requires a separate WiFi adapter connected to that camera's
    WiFi network. All cameras share the default GoPro IP (10.5.5.9) but are
    accessed through different network interfaces via source-address binding.
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
        """Connect to all GoPro cameras sequentially.

        Sequential connection is required because each camera installs its own
        urllib opener bound to its WiFi interface's local IP address.
        """
        if not self.cameras:
            print("No GoPro cameras configured")
            return True

        print(f"\nConnecting to {len(self.cameras)} GoPro cameras...")
        results = {}

        for cam_id, cam in self.cameras.items():
            results[cam_id] = cam.connect()

        # Set all cameras to video mode after connecting
        for cam_id, cam in self.cameras.items():
            if results.get(cam_id, False):
                cam.set_video_mode()

        connected = sum(1 for v in results.values() if v)
        total = len(self.cameras)
        print(f"GoPro cameras connected: {connected}/{total}")

        return connected == total

    def start_recording_all(self):
        """Trigger all GoPro cameras to start recording.

        Uses threading so both cameras start nearly simultaneously.
        The API lock inside each camera serializes the actual HTTP calls.
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
        """Stop recording on all GoPro cameras."""
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
        threads = []
        for cam in self.cameras.values():
            t = threading.Thread(target=cam.keep_alive)
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=5.0)

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
                "connected": cam.is_connected,
                "recording": cam.is_recording(),
                "battery": cam.get_battery(),
            }
        return status
