"""Utility functions."""

import time
from datetime import datetime
from pathlib import Path


def timestamp_string() -> str:
    """Return current timestamp as string for filenames."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists."""
    path.mkdir(parents=True, exist_ok=True)
    return path


class Timer:
    """Simple timer for tracking elapsed time."""

    def __init__(self):
        self._start_time: float = 0
        self._running = False

    def start(self):
        self._start_time = time.perf_counter()
        self._running = True

    def stop(self) -> float:
        self._running = False
        return self.elapsed

    @property
    def elapsed(self) -> float:
        if not self._running:
            return 0.0
        return time.perf_counter() - self._start_time

    def reset(self):
        self._start_time = time.perf_counter()
