"""Experiment state machine and main loop."""

import time
from pathlib import Path
from typing import Optional

import cv2

from .camera import CameraManager
from .gopro import GoProManager
from .heart_rate import PolarH10
from .phase import Phase, PhaseConfig, PhaseStatus
from .utils import timestamp_string


class Experiment:
    def __init__(self, settings: dict):
        self.settings = settings
        self.name = settings["experiment"]["name"]
        self.output_dir = Path(settings["experiment"]["output_dir"])

        self.camera_manager = CameraManager(settings["cameras"])
        self.gopro_manager = GoProManager(settings.get("gopros", []))
        self.phases = self._load_phases(settings["phases"])
        self.current_phase_index = 0

        # Polar H10 heart rate monitor setup
        hr_settings = settings.get("heart_rate", {})
        self.hr_enabled = hr_settings.get("enabled", False)
        self.hr_monitor: Optional[PolarH10] = None
        if self.hr_enabled:
            self.hr_monitor = PolarH10(
                device_address=hr_settings.get("device_address"),
                ecg_enabled=hr_settings.get("ecg_enabled", False),
            )

        self._session_timestamp = timestamp_string()
        self._keepalive_interval = 2.5  # seconds between GoPro keep-alive signals
        self._last_keepalive = 0.0
        self._frame_dir = self.output_dir / "frames"

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

    def _save_frames(self, frames: dict, phase: Phase):
        """Save captured frames to the output directory, organized by phase."""
        phase_dir = self._frame_dir / phase.config.id
        phase_dir.mkdir(parents=True, exist_ok=True)
        for cam_id, frame in frames.items():
            filename = f"{cam_id}_{phase.frame_count:06d}.jpg"
            cv2.imwrite(str(phase_dir / filename), frame)

    @property
    def current_phase(self) -> Optional[Phase]:
        if 0 <= self.current_phase_index < len(self.phases):
            return self.phases[self.current_phase_index]
        return None

    def setup(self) -> bool:
        """Initialize experiment resources."""
        print(f"Setting up experiment: {self.name}")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Connect Polar H10 first (before calibration starts)
        if self.hr_monitor:
            print("\n--- Connecting Polar H10 ---")
            if not self.hr_monitor.connect():
                print("WARNING: Polar H10 connection failed. Continuing without HR.")
                self.hr_enabled = False

        # Open IP cameras
        if not self.camera_manager.open_all():
            print("WARNING: Some IP cameras failed to connect")

        return True

    def teardown(self):
        """Clean up all resources. HR recording stops here at the very end."""
        print("\n--- Tearing Down Experiment ---")

        # Stop Polar H10 recording and save data LAST
        if self.hr_monitor:
            self.hr_monitor.stop_recording()
            hr_path = self.output_dir / f"heart_rate_{self._session_timestamp}.csv"
            self.hr_monitor.save_to_csv(hr_path)
            ecg_path = self.output_dir / f"ecg_{self._session_timestamp}.csv"
            self.hr_monitor.save_ecg_to_csv(ecg_path)
            summary = self.hr_monitor.get_summary()
            print("\nPolar H10 Summary by Phase:")
            for phase, stats in summary.items():
                rr_info = ""
                if "avg_rr_ms" in stats:
                    rr_info = f", avg_rr={stats['avg_rr_ms']}ms ({stats['rr_count']} beats)"
                print(f"  {phase}: avg={stats['avg_bpm']} bpm, "
                      f"min={stats['min_bpm']}, max={stats['max_bpm']}, "
                      f"samples={stats['count']}{rr_info}")
            self.hr_monitor.disconnect()

        # Disconnect GoPro cameras
        self.gopro_manager.disconnect_all()

        # Close IP cameras
        self.camera_manager.close_all()

        print("Teardown complete")

    def next_phase(self) -> bool:
        """Advance to next phase. Returns False if no more phases."""
        self.current_phase_index += 1
        if self.current_phase_index >= len(self.phases):
            return False
        phase = self.current_phase
        phase.start()
        # Update heart rate phase label
        if self.hr_monitor and self.hr_enabled:
            self.hr_monitor.set_phase(phase.config.id)
        return True

    def _run_calibration(self, phase: Phase):
        """Calibration phase: connect GoPros and start HR recording."""
        # Start heart rate recording at the very beginning of calibration
        if self.hr_monitor and self.hr_enabled:
            self.hr_monitor.start_recording(phase="calibration")

        # Connect to all 4 GoPro cameras during calibration
        print("\n--- Connecting GoPro Cameras ---")
        if not self.gopro_manager.connect_all():
            print("WARNING: Some GoPro cameras failed to connect")

        # Run calibration phase timing loop
        while phase.status == PhaseStatus.ACTIVE:
            time.sleep(0.1)
            phase_done = phase.update(0.1)

            if phase.should_capture and phase.status == PhaseStatus.ACTIVE:
                frames = self.camera_manager.capture_all()
                phase.frame_count += 1
                self._save_frames(frames, phase)

            # Send GoPro keep-alive
            now = time.time()
            if now - self._last_keepalive >= self._keepalive_interval:
                self.gopro_manager.keep_alive_all()
                self._last_keepalive = now

            if phase_done:
                break

    def _run_recording(self, phase: Phase):
        """Recording phase: trigger all GoPro cameras to record."""
        # Update HR phase
        if self.hr_monitor and self.hr_enabled:
            self.hr_monitor.set_phase("recording")

        # Trigger all GoPro cameras to start recording
        self.gopro_manager.start_recording_all()

        # Run recording phase timing loop
        while phase.status == PhaseStatus.ACTIVE:
            time.sleep(0.1)
            phase_done = phase.update(0.1)

            if phase.should_capture and phase.status == PhaseStatus.ACTIVE:
                frames = self.camera_manager.capture_all()
                phase.frame_count += 1
                self._save_frames(frames, phase)

            # Send GoPro keep-alive
            now = time.time()
            if now - self._last_keepalive >= self._keepalive_interval:
                self.gopro_manager.keep_alive_all()
                self._last_keepalive = now

            if phase_done:
                break

        # Stop GoPro recording when phase ends
        self.gopro_manager.stop_recording_all()

    def _run_post_recording(self, phase: Phase):
        """Post-recording phase: review data, HR still recording."""
        if self.hr_monitor and self.hr_enabled:
            self.hr_monitor.set_phase("post_recording")

        # Print GoPro status summary
        status = self.gopro_manager.get_status_all()
        if status:
            print("\nGoPro Camera Status:")
            for cam_id, info in status.items():
                print(f"  {info['name']}: battery={info['battery']}%")

        # Post-recording has duration 0, so it waits for user to press Ctrl+C
        # The phase.update() will immediately complete it if duration is 0
        # Instead, we loop until keyboard interrupt
        if phase.config.duration_seconds <= 0:
            print("\nPost-recording phase active. Press Ctrl+C to end experiment.")
            while True:
                time.sleep(1.0)
                # Send GoPro keep-alive
                now = time.time()
                if now - self._last_keepalive >= self._keepalive_interval:
                    self.gopro_manager.keep_alive_all()
                    self._last_keepalive = now
        else:
            while phase.status == PhaseStatus.ACTIVE:
                time.sleep(0.1)
                phase_done = phase.update(0.1)
                if phase_done:
                    break

    def run(self):
        """Main experiment loop."""
        if not self.setup():
            print("Setup failed!")
            return

        try:
            print(f"\nStarting experiment with {len(self.phases)} phases")
            self.current_phase.start()

            while self.current_phase:
                phase = self.current_phase
                phase_id = phase.config.id

                # Route to phase-specific handler
                if phase_id == "calibration":
                    self._run_calibration(phase)
                elif phase_id == "recording":
                    self._run_recording(phase)
                elif phase_id == "post_recording":
                    self._run_post_recording(phase)
                else:
                    # Generic phase handler for any other phases
                    while phase.status == PhaseStatus.ACTIVE:
                        time.sleep(0.1)
                        phase_done = phase.update(0.1)
                        if phase.should_capture and phase.status == PhaseStatus.ACTIVE:
                            frames = self.camera_manager.capture_all()
                            phase.frame_count += 1
                            self._save_frames(frames, phase)
                        if phase_done:
                            break

                if not self.next_phase():
                    break

            print("\nExperiment complete!")

        except KeyboardInterrupt:
            print("\nExperiment interrupted by user")
        finally:
            # Heart rate recording stops here - at the very end
            self.teardown()
