"""Experiment state machine and main loop."""

import time
from pathlib import Path
from typing import Optional

from .camera import CameraManager
from .phase import Phase, PhaseConfig, PhaseStatus


class Experiment:
    def __init__(self, settings: dict):
        self.settings = settings
        self.name = settings["experiment"]["name"]
        self.output_dir = Path(settings["experiment"]["output_dir"])

        self.camera_manager = CameraManager(settings["cameras"])
        self.phases = self._load_phases(settings["phases"])
        self.current_phase_index = 0

    def _load_phases(self, phase_configs: list[dict]) -> list[Phase]:
        phases = []
        for cfg in phase_configs:
            config = PhaseConfig(
                id=cfg["id"],
                name=cfg["name"],
                duration_seconds=cfg["duration_seconds"],
                capture_interval_ms=cfg.get("capture_interval_ms"),
                instructions=cfg["instructions"],
            )
            phases.append(Phase(config))
        return phases

    @property
    def current_phase(self) -> Optional[Phase]:
        if 0 <= self.current_phase_index < len(self.phases):
            return self.phases[self.current_phase_index]
        return None

    def setup(self) -> bool:
        """Initialize experiment resources."""
        print(f"Setting up experiment: {self.name}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self.camera_manager.open_all()

    def teardown(self):
        """Clean up resources."""
        print("Tearing down experiment")
        self.camera_manager.close_all()

    def next_phase(self) -> bool:
        """Advance to next phase. Returns False if no more phases."""
        self.current_phase_index += 1
        if self.current_phase_index >= len(self.phases):
            return False
        self.current_phase.start()
        return True

    def run(self):
        """Main experiment loop."""
        if not self.setup():
            print("Setup failed!")
            return

        try:
            print(f"\nStarting experiment with {len(self.phases)} phases")
            self.current_phase.start()

            # Dummy loop - replace with actual timing/capture logic
            while self.current_phase:
                phase = self.current_phase

                # Simulate time passing
                time.sleep(0.1)
                phase_done = phase.update(0.1)

                # Capture if needed
                if phase.should_capture and phase.status == PhaseStatus.ACTIVE:
                    frames = self.camera_manager.capture_all()
                    phase.frame_count += 1
                    # TODO: Save frames

                if phase_done:
                    if not self.next_phase():
                        break

            print("\nExperiment complete!")

        except KeyboardInterrupt:
            print("\nExperiment interrupted by user")
        finally:
            self.teardown()
