"""Camera interface for USB webcams connected via USB."""

import sys
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np


@dataclass
class CameraConfig:
    id: str
    name: str
    device_index: int
    resolution: tuple[int, int]
    fps: int
    enabled: bool
    role: Optional[str] = None
    actual_resolution: Optional[tuple[int, int]] = None
    actual_fps: Optional[float] = None


class Camera:
    def __init__(self, config: CameraConfig):
        self.config = config
        self._capture: Optional[cv2.VideoCapture] = None

    def open(self) -> bool:
        """Initialize USB camera connection."""
        print(f"Opening camera: {self.config.name} (device {self.config.device_index})")

        # Use DirectShow backend on Windows for reliable USB camera access
        if sys.platform == "win32":
            self._capture = cv2.VideoCapture(self.config.device_index, cv2.CAP_DSHOW)
        else:
            self._capture = cv2.VideoCapture(self.config.device_index)

        if not self._capture.isOpened():
            print(f"Failed to open camera: {self.config.name}")
            self._capture = None
            return False

        # Set camera properties
        self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.resolution[0])
        self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.resolution[1])
        self._capture.set(cv2.CAP_PROP_FPS, self.config.fps)

        # Widest FOV: disable zoom and autofocus (which can digitally crop)
        self._capture.set(cv2.CAP_PROP_ZOOM, 0)
        self._capture.set(cv2.CAP_PROP_AUTOFOCUS, 0)

        # Set buffer size to minimize latency
        self._capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        # Store actual resolution (may differ from requested)
        actual_w = int(self._capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = self._capture.get(cv2.CAP_PROP_FPS)
        actual_zoom = self._capture.get(cv2.CAP_PROP_ZOOM)
        self.config.actual_resolution = (actual_w, actual_h)
        self.config.actual_fps = actual_fps

        if (actual_w, actual_h) != self.config.resolution:
            print(f"WARNING: {self.config.name} resolution mismatch! "
                  f"Requested {self.config.resolution[0]}x{self.config.resolution[1]}, "
                  f"got {actual_w}x{actual_h}. Using actual resolution for recording.")

        print(f"Opened camera: {self.config.name} "
              f"(requested {self.config.resolution[0]}x{self.config.resolution[1]}, "
              f"actual {actual_w}x{actual_h}, fps={actual_fps:.0f}, zoom={actual_zoom})")
        return True

    def close(self):
        """Release camera resources."""
        print(f"Closing camera: {self.config.name}")
        if self._capture:
            self._capture.release()
            self._capture = None

    def read_frame(self) -> Optional[np.ndarray]:
        """Capture a single frame from the USB camera."""
        if self._capture is None:
            return None

        ret, frame = self._capture.read()
        if not ret or frame is None:
            print(f"Warning: Failed to read frame from {self.config.name}")
            return None

        return frame

    @property
    def is_open(self) -> bool:
        return self._capture is not None and self._capture.isOpened()


class CameraManager:
    def __init__(self, camera_configs: list[dict]):
        self.cameras: dict[str, Camera] = {}
        for cfg in camera_configs:
            if cfg.get("enabled", True):
                config = CameraConfig(
                    id=cfg["id"],
                    name=cfg["name"],
                    device_index=cfg["device_index"],
                    resolution=tuple(cfg["resolution"]),
                    fps=cfg["fps"],
                    enabled=cfg["enabled"],
                    role=cfg.get("role"),
                )
                self.cameras[config.id] = Camera(config)

    def open_all(self) -> bool:
        """Open all cameras."""
        success = True
        for camera in self.cameras.values():
            if not camera.open():
                success = False
        return success

    def close_all(self):
        """Close all cameras."""
        for camera in self.cameras.values():
            camera.close()

    def capture_all(self) -> dict[str, np.ndarray]:
        """Capture frame from all cameras."""
        frames = {}
        for cam_id, camera in self.cameras.items():
            frame = camera.read_frame()
            if frame is not None:
                frames[cam_id] = frame
        return frames

    def assign_roles(self, role_map: dict[str, str]):
        """Update camera roles from a {camera_id: role} mapping."""
        for cam_id, role in role_map.items():
            if cam_id in self.cameras:
                self.cameras[cam_id].config.role = role
                print(f"Assigned role '{role}' to camera '{self.cameras[cam_id].config.name}' "
                      f"(device {self.cameras[cam_id].config.device_index})")

    def get_camera_by_role(self, role: str) -> Optional["Camera"]:
        """Find the first camera with the given role."""
        for camera in self.cameras.values():
            if camera.config.role == role:
                return camera
        return None
