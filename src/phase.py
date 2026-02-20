"""Phase definitions for experiment workflow."""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


class PhaseStatus(Enum):
    PENDING = auto()
    ACTIVE = auto()
    COMPLETED = auto()
    SKIPPED = auto()


@dataclass
class PhaseConfig:
    id: str
    name: str
    duration_seconds: float
    capture_interval_ms: Optional[int]
    instructions: str
    record_video: bool = False
    record_audio: bool = False
    record_gopro: bool = False
    allow_pause: bool = False
    cameras: list[str] = None

    def __post_init__(self):
        if self.cameras is None:
            self.cameras = []


class Phase:
    def __init__(self, config: PhaseConfig):
        self.config = config
        self.status = PhaseStatus.PENDING
        self.elapsed_time: float = 0.0
        self.frame_count: int = 0

    def start(self):
        """Begin this phase."""
        self.status = PhaseStatus.ACTIVE
        self.elapsed_time = 0.0
        self.frame_count = 0
        print(f"\n--- Phase: {self.config.name} ---")
        print(f"Instructions: {self.config.instructions}")

    def update(self, delta_time: float) -> bool:
        """Update phase timer. Returns True if phase is complete."""
        if self.status != PhaseStatus.ACTIVE:
            return True

        self.elapsed_time += delta_time

        if self.config.duration_seconds > 0:
            if self.elapsed_time >= self.config.duration_seconds:
                self.complete()
                return True
        return False

    def complete(self):
        """Mark phase as completed."""
        self.status = PhaseStatus.COMPLETED
        print(f"Phase '{self.config.name}' completed. Frames: {self.frame_count}")

    def skip(self):
        """Skip this phase."""
        self.status = PhaseStatus.SKIPPED

    @property
    def progress(self) -> float:
        """Return progress as 0.0-1.0."""
        if self.config.duration_seconds <= 0:
            return 0.0
        return min(1.0, self.elapsed_time / self.config.duration_seconds)

    @property
    def should_capture(self) -> bool:
        """Check if capture is enabled for this phase."""
        return self.config.capture_interval_ms is not None
