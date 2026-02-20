"""Video recording from USB cameras to MP4 files."""

import threading
import time
from typing import Optional

import cv2
import numpy as np

from .camera import Camera


class VideoRecorder:
    """Records frames from a Camera to an MP4 file on a background thread."""

    def __init__(self, camera: Camera, output_path: str, fps: int = 30):
        self.camera = camera
        self.output_path = output_path
        self.fps = fps
        self._writer: Optional[cv2.VideoWriter] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._frame_count = 0
        self._last_frame: Optional[np.ndarray] = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Start recording on a background thread."""
        if not self.camera.is_open:
            print(f"Cannot record: camera '{self.camera.config.name}' not open")
            return False

        w, h = self.camera.config.resolution
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._writer = cv2.VideoWriter(self.output_path, fourcc, self.fps, (w, h))
        if not self._writer.isOpened():
            print(f"Failed to open video writer: {self.output_path}")
            return False

        self._stop_event.clear()
        self._frame_count = 0
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()
        print(f"Video recording started: {self.output_path}")
        return True

    def _record_loop(self):
        """Main recording loop running on background thread."""
        interval = 1.0 / self.fps
        while not self._stop_event.is_set():
            start = time.perf_counter()
            frame = self.camera.read_frame()
            if frame is not None:
                with self._lock:
                    self._last_frame = frame.copy()
                self._writer.write(frame)
                self._frame_count += 1
            elapsed = time.perf_counter() - start
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def stop(self):
        """Stop recording and release the video writer."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        print(f"Video recording stopped: {self._frame_count} frames written to {self.output_path}")

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def last_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._last_frame.copy() if self._last_frame is not None else None


class PausableVideoRecorder(VideoRecorder):
    """Video recorder that writes frozen frames during pause to stay time-synced.

    When paused, the last captured frame is repeatedly written at the configured
    FPS so that the output video duration matches wall-clock time.
    """

    def __init__(self, camera: Camera, output_path: str, fps: int = 30):
        super().__init__(camera, output_path, fps)
        self._paused = False
        self._pause_lock = threading.Lock()

    def pause(self):
        """Pause live capture; frozen frames will be written instead."""
        with self._pause_lock:
            self._paused = True
        print("Video recording paused (writing frozen frames)")

    def resume(self):
        """Resume live capture from the camera."""
        with self._pause_lock:
            self._paused = False
        print("Video recording resumed (live frames)")

    @property
    def is_paused(self) -> bool:
        with self._pause_lock:
            return self._paused

    def _record_loop(self):
        """Recording loop that writes frozen frames when paused."""
        interval = 1.0 / self.fps
        while not self._stop_event.is_set():
            start = time.perf_counter()

            with self._pause_lock:
                paused = self._paused

            if paused:
                # Write the last captured frame to keep time-sync
                with self._lock:
                    frame = self._last_frame
                if frame is not None:
                    self._writer.write(frame)
                    self._frame_count += 1
            else:
                frame = self.camera.read_frame()
                if frame is not None:
                    with self._lock:
                        self._last_frame = frame.copy()
                    self._writer.write(frame)
                    self._frame_count += 1

            elapsed = time.perf_counter() - start
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
