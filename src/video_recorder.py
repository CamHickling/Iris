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

        # Use actual resolution from camera (not configured), since the camera
        # may deliver frames at a different size than requested
        w, h = self.camera.config.actual_resolution or self.camera.config.resolution
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._writer = cv2.VideoWriter(self.output_path, fourcc, self.fps, (w, h))
        if not self._writer.isOpened():
            print(f"Failed to open video writer: {self.output_path}")
            return False

        self._stop_event.clear()
        self._frame_count = 0
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()
        print(f"Video recording started: {self.output_path} ({w}x{h} @ {self.fps}fps)")
        return True

    def _record_loop(self):
        """Main recording loop running on background thread.

        Writes enough frames each iteration to keep the video file duration
        in sync with wall-clock time.  If the camera delivers frames slower
        than the configured FPS (common with USB webcams), the latest frame
        is duplicated so the resulting MP4 plays back at true 1× speed.
        """
        interval = 1.0 / self.fps
        expected_size = self.camera.config.actual_resolution or self.camera.config.resolution
        size_warned = False
        start_time = time.perf_counter()

        while not self._stop_event.is_set():
            loop_start = time.perf_counter()
            frame = self.camera.read_frame()
            if frame is not None:
                # Ensure frame matches writer dimensions
                fh, fw = frame.shape[:2]
                if (fw, fh) != expected_size:
                    if not size_warned:
                        print(f"WARNING: Frame size {fw}x{fh} != writer size "
                              f"{expected_size[0]}x{expected_size[1]}, resizing")
                        size_warned = True
                    frame = cv2.resize(frame, expected_size)
                with self._lock:
                    self._last_frame = frame.copy()

            # Write enough frames to stay in sync with wall-clock time
            write_frame = frame
            if write_frame is None:
                with self._lock:
                    write_frame = self._last_frame
            if write_frame is not None:
                expected_frames = int((time.perf_counter() - start_time) * self.fps)
                while self._frame_count < expected_frames:
                    self._writer.write(write_frame)
                    self._frame_count += 1

            elapsed = time.perf_counter() - loop_start
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
        """Recording loop that writes frozen frames when paused.

        Like the base class, this keeps the video file in sync with
        wall-clock time by duplicating frames when the camera is slow.
        """
        interval = 1.0 / self.fps
        expected_size = self.camera.config.actual_resolution or self.camera.config.resolution
        size_warned = False
        start_time = time.perf_counter()

        while not self._stop_event.is_set():
            loop_start = time.perf_counter()

            with self._pause_lock:
                paused = self._paused

            if not paused:
                frame = self.camera.read_frame()
                if frame is not None:
                    fh, fw = frame.shape[:2]
                    if (fw, fh) != expected_size:
                        if not size_warned:
                            print(f"WARNING: Frame size {fw}x{fh} != writer size "
                                  f"{expected_size[0]}x{expected_size[1]}, resizing")
                            size_warned = True
                        frame = cv2.resize(frame, expected_size)
                    with self._lock:
                        self._last_frame = frame.copy()

            # Write enough frames to stay in sync with wall-clock time
            with self._lock:
                write_frame = self._last_frame
            if write_frame is not None:
                expected_frames = int((time.perf_counter() - start_time) * self.fps)
                while self._frame_count < expected_frames:
                    self._writer.write(write_frame)
                    self._frame_count += 1

            elapsed = time.perf_counter() - loop_start
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
