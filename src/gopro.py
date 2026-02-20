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


def ensure_interface_ip(interface_name: str, static_ip: str,
                        max_wait: float = 10.0) -> Optional[str]:
    """Wait for DHCP to assign a 10.5.5.x IP, or set a static IP as fallback.

    Args:
        interface_name: Windows WiFi interface name (e.g. "WiFi 4")
        static_ip: Static IP to assign if DHCP fails (e.g. "10.5.5.101")
        max_wait: Seconds to wait for DHCP before falling back to static

    Returns:
        The 10.5.5.x IP address, or None if all attempts fail.
    """
    # First check if we already have a valid IP
    ip = get_interface_ip(interface_name)
    if ip:
        return ip

    # Wait for DHCP
    print(f"  Waiting for DHCP on {interface_name}...")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(1.0)
        ip = get_interface_ip(interface_name)
        if ip:
            print(f"  DHCP assigned {ip} to {interface_name}")
            return ip

    # DHCP failed — assign static IP
    print(f"  DHCP failed on {interface_name}, assigning static IP {static_ip}...")
    try:
        # Use elevated powershell to set the static IP
        subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                f'Start-Process netsh -ArgumentList '
                f'"interface ip set address `"{interface_name}`" '
                f'static {static_ip} 255.255.255.0 10.5.5.9" '
                f'-Verb RunAs -Wait',
            ],
            capture_output=True, text=True, timeout=15,
        )
        time.sleep(2.0)
        ip = get_interface_ip(interface_name)
        if ip:
            print(f"  Static IP {ip} set on {interface_name}")
            return ip
    except Exception as e:
        print(f"  WARNING: Failed to set static IP: {e}")

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

    # Track assigned static IPs to avoid conflicts
    _used_static_ips: list[str] = []

    def __init__(self, config: GoProConfig):
        self.config = config
        self._camera: Optional[GoProCamera.GoPro] = None
        self._connected = False
        self._opener: Optional[urllib.request.OpenerDirector] = None
        self._source_ip: Optional[str] = None

    @property
    def is_connected(self) -> bool:
        """Whether the camera is currently connected."""
        return self._connected

    def _install_opener(self):
        """Install this camera's bound urllib opener. Must hold _api_lock."""
        if self._opener is not None:
            urllib.request.install_opener(self._opener)

    def _next_static_ip(self) -> str:
        """Pick the next available static IP in 10.5.5.0/24."""
        for last_octet in range(100, 200):
            candidate = f"10.5.5.{last_octet}"
            if candidate not in GoProCam._used_static_ips:
                GoProCam._used_static_ips.append(candidate)
                return candidate
        return "10.5.5.199"

    def connect(self) -> bool:
        """Connect to the GoPro camera via its WiFi network."""
        print(f"Connecting to GoPro: {self.config.name} ({self.config.model}) "
              f"on interface {self.config.wifi_interface}...")

        # Ensure the WiFi adapter has a valid IP in the GoPro subnet
        static_fallback = self._next_static_ip()
        source_ip = ensure_interface_ip(
            self.config.wifi_interface, static_fallback, max_wait=10.0,
        )

        if source_ip:
            print(f"  Interface {self.config.wifi_interface} -> local IP {source_ip}")
            self._source_ip = source_ip
            handler = _BoundHTTPHandler((source_ip, 0))
            self._opener = urllib.request.build_opener(handler)
        else:
            print(f"  WARNING: Could not get IP for {self.config.wifi_interface}, "
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
        """Send keep-alive signal via a source-bound UDP socket.

        The goprocam library's KeepAlive() uses an unbound socket, which would
        route to whichever GoPro the OS picks. We send our own bound UDP packet
        to ensure it reaches the correct camera.
        """
        if not self._connected:
            return
        try:
            payload = "_GPHD_:0:0:2:0.000000\n".encode()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                if self._source_ip:
                    sock.bind((self._source_ip, 0))
                sock.sendto(payload, (self.config.ip_address, 8554))
            finally:
                sock.close()
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
        # Reset static IP tracker for fresh connections
        GoProCam._used_static_ips.clear()
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
