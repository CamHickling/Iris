"""Video playback engine for reviewing recorded footage in the GUI."""

import threading
import time
from enum import Enum, auto
from typing import Callable, Optional

import cv2
import numpy as np


class PlayerState(Enum):
    STOPPED = auto()
    PLAYING = auto()
    PAUSED = auto()


class VideoPlayer:
    """Plays an MP4 file on a background thread with play/pause/stop/seek.

    Callbacks:
        on_frame(frame: np.ndarray, position_sec: float) -- called for each displayed frame
        on_state_change(state: PlayerState) -- called when state changes
        on_complete() -- called when video reaches the end
    """

    def __init__(self, video_path: str):
        self.video_path = video_path
        self._cap: Optional[cv2.VideoCapture] = None
        self._thread: Optional[threading.Thread] = None
        self._state = PlayerState.STOPPED
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # not paused initially

        # Video properties (populated after open)
        self.fps: float = 30.0
        self.total_frames: int = 0
        self.duration_sec: float = 0.0
        self.frame_size: tuple[int, int] = (0, 0)

        # Current position
        self._current_frame: int = 0
        self._position_lock = threading.Lock()

        # Callbacks
        self.on_frame: Optional[Callable[[np.ndarray, float], None]] = None
        self.on_state_change: Optional[Callable[[PlayerState], None]] = None
        self.on_complete: Optional[Callable[[], None]] = None

    def open(self) -> bool:
        """Open the video file and read its properties."""
        self._cap = cv2.VideoCapture(self.video_path)
        if not self._cap.isOpened():
            print(f"Failed to open video: {self.video_path}")
            return False

        self.fps = self._cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.total_frames = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.frame_size = (w, h)
        self.duration_sec = self.total_frames / self.fps if self.fps > 0 else 0.0
        print(f"Video opened: {self.video_path} ({w}x{h}, {self.fps:.1f} fps, {self.duration_sec:.1f}s)")
        return True

    def play(self):
        """Start or resume playback."""
        with self._state_lock:
            if self._state == PlayerState.PLAYING:
                return
            if self._state == PlayerState.PAUSED:
                self._pause_event.set()
                self._state = PlayerState.PLAYING
                self._notify_state_change()
                return

        # Start fresh playback
        self._stop_event.clear()
        self._pause_event.set()
        self._state = PlayerState.PLAYING
        self._notify_state_change()
        self._thread = threading.Thread(target=self._playback_loop, daemon=True)
        self._thread.start()

    def pause(self):
        """Pause playback."""
        with self._state_lock:
            if self._state != PlayerState.PLAYING:
                return
            self._pause_event.clear()
            self._state = PlayerState.PAUSED
            self._notify_state_change()

    def stop(self):
        """Stop playback and reset position."""
        self._stop_event.set()
        self._pause_event.set()  # unblock pause wait
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        with self._state_lock:
            self._state = PlayerState.STOPPED
            self._notify_state_change()
        with self._position_lock:
            self._current_frame = 0
        if self._cap is not None:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    def seek(self, position_sec: float):
        """Seek to a specific position in seconds."""
        if self._cap is None:
            return
        target_frame = int(position_sec * self.fps)
        target_frame = max(0, min(target_frame, self.total_frames - 1))
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
        with self._position_lock:
            self._current_frame = target_frame

    @property
    def position_sec(self) -> float:
        with self._position_lock:
            return self._current_frame / self.fps if self.fps > 0 else 0.0

    @property
    def progress(self) -> float:
        """Return playback progress as 0.0-1.0."""
        if self.total_frames <= 0:
            return 0.0
        with self._position_lock:
            return self._current_frame / self.total_frames

    @property
    def state(self) -> PlayerState:
        with self._state_lock:
            return self._state

    def close(self):
        """Release video resources."""
        self.stop()
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _playback_loop(self):
        """Background thread that reads and delivers frames at native FPS.

        Frames are read at the video's native FPS for correct timing,
        but on_frame callbacks are throttled to ~20fps to avoid overwhelming
        the GUI while still maintaining smooth playback perception.
        """
        interval = 1.0 / self.fps if self.fps > 0 else 1.0 / 30.0
        display_interval = 1.0 / 20.0  # cap display callbacks at 20fps
        last_display = 0.0

        while not self._stop_event.is_set():
            # Wait while paused
            self._pause_event.wait()
            if self._stop_event.is_set():
                break

            start = time.perf_counter()

            ret, frame = self._cap.read()
            if not ret or frame is None:
                # End of video
                with self._state_lock:
                    self._state = PlayerState.STOPPED
                    self._notify_state_change()
                if self.on_complete:
                    self.on_complete()
                break

            with self._position_lock:
                self._current_frame += 1
                pos = self._current_frame / self.fps if self.fps > 0 else 0.0

            # Only send frame to GUI at display rate (skip intermediate frames)
            now = time.perf_counter()
            if self.on_frame and (now - last_display) >= display_interval:
                self.on_frame(frame, pos)
                last_display = now

            elapsed = time.perf_counter() - start
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _notify_state_change(self):
        if self.on_state_change:
            self.on_state_change(self._state)
