"""Camera interface for capture devices."""

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class CameraConfig:
    id: str
    name: str
    device_index: int
    resolution: tuple[int, int]
    fps: int
    enabled: bool


class Camera:
    def __init__(self, config: CameraConfig):
        self.config = config
        self._capture = None

    def open(self) -> bool:
        """Initialize camera connection."""
        # TODO: Initialize actual camera capture
        print(f"Opening camera: {self.config.name} (device {self.config.device_index})")
        return True

    def close(self):
        """Release camera resources."""
        print(f"Closing camera: {self.config.name}")
        if self._capture:
            self._capture = None

    def read_frame(self) -> Optional[np.ndarray]:
        """Capture a single frame."""
        # TODO: Return actual frame data
        # Dummy frame for now
        h, w = self.config.resolution[1], self.config.resolution[0]
        return np.zeros((h, w, 3), dtype=np.uint8)

    @property
    def is_open(self) -> bool:
        return self._capture is not None


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
