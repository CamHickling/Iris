"""Camera interface for IP cameras connected via network switch."""

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np


@dataclass
class CameraConfig:
    id: str
    name: str
    ip_address: str
    port: int
    stream_path: str
    resolution: tuple[int, int]
    fps: int
    enabled: bool
    username: Optional[str] = None
    password: Optional[str] = None

    @property
    def rtsp_url(self) -> str:
        """Build RTSP URL for the camera."""
        if self.username and self.password:
            return f"rtsp://{self.username}:{self.password}@{self.ip_address}:{self.port}/{self.stream_path}"
        return f"rtsp://{self.ip_address}:{self.port}/{self.stream_path}"


class Camera:
    def __init__(self, config: CameraConfig):
        self.config = config
        self._capture: Optional[cv2.VideoCapture] = None

    def open(self) -> bool:
        """Initialize network camera connection via RTSP."""
        url = self.config.rtsp_url
        print(f"Connecting to camera: {self.config.name} at {self.config.ip_address}:{self.config.port}")

        self._capture = cv2.VideoCapture(url)

        if not self._capture.isOpened():
            print(f"Failed to connect to camera: {self.config.name}")
            self._capture = None
            return False

        # Set camera properties
        self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.resolution[0])
        self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.resolution[1])
        self._capture.set(cv2.CAP_PROP_FPS, self.config.fps)

        # Set buffer size to minimize latency
        self._capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        print(f"Connected to camera: {self.config.name}")
        return True

    def close(self):
        """Release camera resources."""
        print(f"Closing camera: {self.config.name}")
        if self._capture:
            self._capture.release()
            self._capture = None

    def read_frame(self) -> Optional[np.ndarray]:
        """Capture a single frame from the network camera."""
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
                    ip_address=cfg["ip_address"],
                    port=cfg.get("port", 554),
                    stream_path=cfg.get("stream_path", "stream"),
                    resolution=tuple(cfg["resolution"]),
                    fps=cfg["fps"],
                    enabled=cfg["enabled"],
                    username=cfg.get("username"),
                    password=cfg.get("password"),
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
