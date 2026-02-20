"""Experiment state machine and main loop.

9-phase Taekwondo experiment flow with video recording, audio capture,
video playback, compositing, and multi-camera calibration.
"""

import json
import queue
import time
from pathlib import Path
from typing import Optional

import cv2

from .audio import AudioConfig, AudioRecorder
from .camera import CameraManager
from .compositing import (
    check_ffmpeg,
    create_hr_synced_video,
    create_review_composite,
    overlay_audio_on_video,
)
from .extrinsic_calibration import calibrate_all, save_calibration_log
from .gopro import GoProManager
from .heart_rate import PolarH10
from .lens_correct import calibrate_from_video, correct_video
from .phase import Phase, PhaseConfig, PhaseStatus
from .utils import timestamp_string
from .video_player import PlayerState, VideoPlayer
from .video_recorder import PausableVideoRecorder, VideoRecorder


class Experiment:
    def __init__(self, settings: dict, gui_event_queue: Optional[queue.Queue] = None,
                 user_action_queue: Optional[queue.Queue] = None,
                 gopro_mode: str = "auto"):
        self.settings = settings
        self.name = settings["experiment"]["name"]
        self.output_dir = Path(settings["experiment"]["output_dir"])
        self.gopro_mode = gopro_mode  # "auto" or "manual"

        self.camera_manager = CameraManager(settings["cameras"])
        # In manual mode, don't try to auto-control GoPros
        if gopro_mode == "manual":
            self.gopro_manager = GoProManager([])
            print("GoPro mode: MANUAL - experimenter controls GoPros")
        else:
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

        # Microphone setup
        mic_settings = settings.get("microphone", {})
        self.mic_enabled = mic_settings.get("enabled", False)
        self.mic_config = AudioConfig(
            device_name=mic_settings.get("device_name", "Tonor"),
            device_index=mic_settings.get("device_index"),
            sample_rate=mic_settings.get("sample_rate", 44100),
            channels=mic_settings.get("channels", 1),
            enabled=self.mic_enabled,
        )

        self._session_timestamp = timestamp_string()
        self._session_dir: Optional[Path] = None
        self._keepalive_interval = 2.5
        self._last_keepalive = 0.0

        # GUI communication queues
        self._gui_event_queue = gui_event_queue or queue.Queue()
        self._user_action_queue = user_action_queue or queue.Queue()

        # GoPro footage paths (uploaded by user in posthoc phase)
        self._gopro_footage: list[str] = []

    def _load_phases(self, phase_configs: list[dict]) -> list[Phase]:
        phases = []
        for cfg in phase_configs:
            config = PhaseConfig(
                id=cfg["id"],
                name=cfg["name"],
                duration_seconds=cfg["duration_seconds"],
                capture_interval_ms=cfg.get("capture_interval_ms"),
                instructions=cfg["instructions"],
                record_video=cfg.get("record_video", False),
                record_audio=cfg.get("record_audio", False),
                record_gopro=cfg.get("record_gopro", False),
                allow_pause=cfg.get("allow_pause", False),
                cameras=cfg.get("cameras", []),
            )
            phases.append(Phase(config))
        return phases

    @property
    def current_phase(self) -> Optional[Phase]:
        if 0 <= self.current_phase_index < len(self.phases):
            return self.phases[self.current_phase_index]
        return None

    def _send_gui_event(self, event_type: str, **data):
        """Send an event to the GUI."""
        self._gui_event_queue.put({"type": event_type, **data})

    def _wait_for_user_action(self, action_type: str, timeout: float = None) -> Optional[dict]:
        """Wait for a specific user action from the GUI."""
        deadline = time.time() + timeout if timeout else None
        while True:
            remaining = None
            if deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
            try:
                wait_time = min(remaining, 0.5) if remaining else 0.5
                action = self._user_action_queue.get(timeout=wait_time)
                if action.get("type") == action_type:
                    return action
                if action.get("type") == "stop":
                    raise KeyboardInterrupt("User stopped experiment")
            except queue.Empty:
                continue

    def _check_for_stop(self):
        """Non-blocking check if user requested stop."""
        try:
            action = self._user_action_queue.get_nowait()
            if action.get("type") == "stop":
                raise KeyboardInterrupt("User stopped experiment")
            # Put back if not a stop action
            self._user_action_queue.put(action)
        except queue.Empty:
            pass

    def _send_keepalive(self):
        """Send GoPro keep-alive if interval has elapsed."""
        now = time.time()
        if now - self._last_keepalive >= self._keepalive_interval:
            self.gopro_manager.keep_alive_all()
            self._last_keepalive = now

    def setup(self) -> bool:
        """Initialize experiment resources."""
        print(f"Setting up experiment: {self.name}")

        # Check ffmpeg
        if not check_ffmpeg():
            print("WARNING: ffmpeg not found on PATH. Compositing will be unavailable.")

        # Create session directory
        self._session_dir = self.output_dir / f"{self.name.replace(' ', '_')}_{self._session_timestamp}"
        self._session_dir.mkdir(parents=True, exist_ok=True)

        return True

    def teardown(self):
        """Clean up all resources."""
        print("\n--- Tearing Down Experiment ---")

        # Stop Polar H10 recording and save data
        if self.hr_monitor:
            self.hr_monitor.stop_recording()
            hr_dir = self._session_dir / "heart_rate"
            hr_dir.mkdir(parents=True, exist_ok=True)
            hr_path = hr_dir / "hr_full_session.csv"
            self.hr_monitor.save_to_csv(hr_path)
            ecg_path = hr_dir / "ecg_full_session.csv"
            self.hr_monitor.save_ecg_to_csv(ecg_path)
            summary = self.hr_monitor.get_summary()
            if summary:
                print("\nPolar H10 Summary by Phase:")
                for phase, stats in summary.items():
                    rr_info = ""
                    if "avg_rr_ms" in stats:
                        rr_info = f", avg_rr={stats['avg_rr_ms']}ms ({stats['rr_count']} beats)"
                    print(f"  {phase}: avg={stats['avg_bpm']} bpm, "
                          f"min={stats['min_bpm']}, max={stats['max_bpm']}, "
                          f"samples={stats['count']}{rr_info}")
            self.hr_monitor.disconnect()

        # Stop GoPro recording first, then disconnect
        if self.gopro_mode == "auto":
            print("Stopping all GoPro recordings...")
            self.gopro_manager.stop_recording_all()
            self.gopro_manager.disconnect_all()
        else:
            print("MANUAL MODE: Please stop GoPro recording and power off cameras.")

        # Close USB cameras
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
        # Notify GUI
        self._send_gui_event(
            "phase_change",
            phase_index=self.current_phase_index,
            phase_id=phase.config.id,
            phase_name=phase.config.name,
            total_phases=len(self.phases),
        )
        return True

    # ======================================================================
    #  Phase Handlers
    # ======================================================================

    def _run_setup(self, phase: Phase):
        """Phase 1: Create output dirs, display project name, auto-advance."""
        print(f"\nExperiment: {self.name}")
        print(f"Session directory: {self._session_dir}")

        # Create all subdirectories
        for subdir in ["performance", "review", "scoring", "composited",
                       "heart_rate", "gopro_footage", "calibration"]:
            (self._session_dir / subdir).mkdir(parents=True, exist_ok=True)

        self._send_gui_event("status", message=f"Experiment: {self.name}")
        phase.complete()

    def _run_heart_rate_start(self, phase: Phase):
        """Phase 2: Connect Polar H10, start recording, auto-advance."""
        if self.hr_monitor and self.hr_enabled:
            print("\n--- Connecting Polar H10 ---")
            if not self.hr_monitor.connect():
                print("WARNING: Polar H10 connection failed. Continuing without HR.")
                self.hr_enabled = False
            else:
                self.hr_monitor.start_recording(phase="heart_rate_start")
        else:
            print("Heart rate monitoring disabled.")

        phase.complete()

    def _run_warmup_calibration(self, phase: Phase):
        """Phase 3: Open cameras, test mic, connect GoPros, record calibration. Wait for user Continue."""
        # Open USB cameras
        print("\n--- Opening USB Cameras ---")
        if not self.camera_manager.open_all():
            print("WARNING: Some USB cameras failed to connect")

        # Test microphone
        if self.mic_enabled:
            print("\n--- Testing Microphone ---")
            from .audio import find_audio_device
            device_idx = self.mic_config.device_index
            if device_idx is None:
                device_idx = find_audio_device(self.mic_config.device_name)
            if device_idx is not None:
                print(f"Microphone found: device index {device_idx}")
            else:
                print(f"WARNING: Microphone '{self.mic_config.device_name}' not found")

        # Connect GoPros
        if self.gopro_mode == "auto":
            print("\n--- Connecting GoPro Cameras ---")
            if not self.gopro_manager.connect_all():
                print("WARNING: Some GoPro cameras failed to connect")

            # Start GoPro recording for calibration footage
            print("\n--- Starting GoPro Recording (Calibration) ---")
            self.gopro_manager.start_recording_all()
            self._send_gui_event("recording_status", recording=True, gopros=True)

            print("\nGoPros are recording. Please stand in T-pose facing the front camera.")
            print("Ensure the checkerboard is visible to all cameras if doing extrinsic calibration.")
            self._send_gui_event("wait_for_continue",
                                 message="GoPros recording. Press Continue when calibration is done.")
        else:
            print("\n--- GoPro Mode: MANUAL ---")
            print("Please start GoPro recording manually now.")
            print("Stand in T-pose facing the front camera for calibration.")
            self._send_gui_event("wait_for_continue",
                                 message="MANUAL MODE: Start GoPros yourself. Press Continue when done.")

        # Wait for user to press Continue, sending keep-alives while waiting
        print("Waiting for user to continue...")
        while True:
            self._send_keepalive()
            try:
                action = self._user_action_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if action.get("type") == "stop":
                if self.gopro_mode == "auto":
                    self.gopro_manager.stop_recording_all()
                self._send_gui_event("recording_status", recording=False)
                raise KeyboardInterrupt("User stopped experiment")
            if action.get("type") == "continue":
                break

        # Stop GoPro recording after calibration
        if self.gopro_mode == "auto":
            print("\n--- Stopping GoPro Recording (Calibration) ---")
            self.gopro_manager.stop_recording_all()
        else:
            print("\nPlease stop GoPro recording manually now.")
        self._send_gui_event("recording_status", recording=False)

        phase.complete()

    def _run_performance(self, phase: Phase):
        """Phase 4: Record overhead video + GoPros simultaneously."""
        if self.hr_monitor and self.hr_enabled:
            self.hr_monitor.set_phase("performance")

        overhead_cam = self.camera_manager.get_camera_by_role("overhead")
        if overhead_cam is None:
            print("WARNING: No overhead camera found, using first available")
            if self.camera_manager.cameras:
                overhead_cam = next(iter(self.camera_manager.cameras.values()))

        overhead_path = str(self._session_dir / "performance" / "overhead_camera.mp4")
        recorder = None

        if overhead_cam and overhead_cam.is_open:
            recorder = VideoRecorder(overhead_cam, overhead_path, fps=overhead_cam.config.fps)
            recorder.start()

        # Start GoPro recording
        if self.gopro_mode == "auto":
            self.gopro_manager.start_recording_all()
        else:
            print("MANUAL MODE: Ensure GoPros are recording.")

        self._send_gui_event("wait_for_continue", message="Recording in progress. Press Continue when done.")
        self._send_gui_event("recording_status", recording=True, cameras=["overhead"], gopros=True)

        # Wait for user to end performance
        print("Performance recording started. Waiting for user to continue...")
        while True:
            self._send_keepalive()
            action = None
            try:
                action = self._user_action_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if action and action.get("type") in ("continue", "stop"):
                if action.get("type") == "stop":
                    raise KeyboardInterrupt("User stopped experiment")
                break

        # Stop recording
        if recorder:
            recorder.stop()
        if self.gopro_mode == "auto":
            self.gopro_manager.stop_recording_all()
        else:
            print("MANUAL MODE: Please stop GoPro recording now.")

        self._send_gui_event("recording_status", recording=False)
        phase.complete()

    def _run_review(self, phase: Phase):
        """Phase 5: Play overhead video, record face cam + mic with pause/play."""
        if self.hr_monitor and self.hr_enabled:
            self.hr_monitor.set_phase("review")

        overhead_path = str(self._session_dir / "performance" / "overhead_camera.mp4")
        face_video_path = str(self._session_dir / "review" / "face_cam.mp4")
        audio_path = str(self._session_dir / "review" / "audio_commentary.wav")
        timestamps_path = str(self._session_dir / "review" / "review_timestamps.json")

        # Set up video player for overhead footage
        player = VideoPlayer(overhead_path)
        if not player.open():
            print("WARNING: Cannot open overhead video for review. Skipping review.")
            phase.complete()
            return

        # Set up face cam recorder (pausable)
        face_cam = self.camera_manager.get_camera_by_role("face")
        face_recorder = None
        if face_cam and face_cam.is_open:
            face_recorder = PausableVideoRecorder(face_cam, face_video_path, fps=face_cam.config.fps)
            face_recorder.start()

        # Set up audio recorder
        audio_recorder = None
        if self.mic_enabled:
            audio_recorder = AudioRecorder(self.mic_config)
            if audio_recorder.open(audio_path):
                audio_recorder.start_recording()
            else:
                audio_recorder = None

        # Timestamp log for pause/resume events
        timestamps = []

        # Send video frames to GUI via callback
        def on_frame(frame, position_sec):
            self._send_gui_event("video_frame", frame=frame, position_sec=position_sec,
                                 duration_sec=player.duration_sec)

        def on_state_change(state):
            self._send_gui_event("player_state", state=state.name)

        def on_complete():
            self._send_gui_event("video_complete")

        player.on_frame = on_frame
        player.on_state_change = on_state_change
        player.on_complete = on_complete

        self._send_gui_event("show_video_player", allow_pause=True,
                             message="Review overhead video. Use Pause/Play for commentary.")

        # Main review loop
        video_finished = False

        def mark_complete():
            nonlocal video_finished
            video_finished = True

        player.on_complete = mark_complete

        print("Review phase started. Waiting for user actions...")
        while not video_finished:
            try:
                action = self._user_action_queue.get(timeout=0.5)
            except queue.Empty:
                # Send current frame if playing
                if player.state == PlayerState.PLAYING:
                    self._send_gui_event("player_progress",
                                         position_sec=player.position_sec,
                                         duration_sec=player.duration_sec)
                continue

            action_type = action.get("type")
            if action_type == "stop":
                raise KeyboardInterrupt("User stopped experiment")
            elif action_type == "play":
                player.play()
                if face_recorder and face_recorder.is_paused:
                    face_recorder.resume()
                timestamps.append({
                    "type": "resume",
                    "wall_time": time.time(),
                    "video_position_sec": player.position_sec,
                })
            elif action_type == "pause":
                player.pause()
                if face_recorder:
                    face_recorder.pause()
                timestamps.append({
                    "type": "pause",
                    "wall_time": time.time(),
                    "video_position_sec": player.position_sec,
                })
            elif action_type == "continue":
                break

        # Stop everything
        player.stop()
        player.close()
        if face_recorder:
            face_recorder.stop()
        if audio_recorder:
            audio_recorder.stop_recording()
            audio_recorder.close()

        # Save timestamps
        with open(timestamps_path, "w") as f:
            json.dump(timestamps, f, indent=2)
        print(f"Review timestamps saved to {timestamps_path}")

        self._send_gui_event("hide_video_player")
        phase.complete()

    def _run_scoring(self, phase: Phase):
        """Phase 6: Play overhead video (no pause), record face cam."""
        if self.hr_monitor and self.hr_enabled:
            self.hr_monitor.set_phase("scoring")

        overhead_path = str(self._session_dir / "performance" / "overhead_camera.mp4")
        face_video_path = str(self._session_dir / "scoring" / "face_cam.mp4")

        # Set up video player
        player = VideoPlayer(overhead_path)
        if not player.open():
            print("WARNING: Cannot open overhead video for scoring. Skipping.")
            phase.complete()
            return

        # Set up face cam recorder (regular, not pausable)
        face_cam = self.camera_manager.get_camera_by_role("face")
        face_recorder = None
        if face_cam and face_cam.is_open:
            face_recorder = VideoRecorder(face_cam, face_video_path, fps=face_cam.config.fps)
            face_recorder.start()

        def on_frame(frame, position_sec):
            self._send_gui_event("video_frame", frame=frame, position_sec=position_sec,
                                 duration_sec=player.duration_sec)

        player.on_frame = on_frame

        self._send_gui_event("show_video_player", allow_pause=False,
                             message="Scoring: Press Start to begin. Face camera is recording.")

        video_finished = False

        def mark_complete():
            nonlocal video_finished
            video_finished = True

        player.on_complete = mark_complete

        # Wait for user to press Start (GUI handles countdown, then sends "play")
        print("Scoring phase: Waiting for user to start...")
        while True:
            try:
                action = self._user_action_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if action.get("type") == "stop":
                raise KeyboardInterrupt("User stopped experiment")
            if action.get("type") == "play":
                player.play()
                break

        print("Scoring video playing...")
        while not video_finished:
            try:
                action = self._user_action_queue.get(timeout=0.5)
                if action.get("type") == "stop":
                    raise KeyboardInterrupt("User stopped experiment")
                if action.get("type") == "continue":
                    break
            except queue.Empty:
                self._send_gui_event("player_progress",
                                     position_sec=player.position_sec,
                                     duration_sec=player.duration_sec)

        player.stop()
        player.close()
        if face_recorder:
            face_recorder.stop()

        self._send_gui_event("hide_video_player")
        phase.complete()

    def _run_finish(self, phase: Phase):
        """Phase 7: Stop HR. Run compositing."""
        if self.hr_monitor and self.hr_enabled:
            self.hr_monitor.set_phase("finish")

        print("\n--- Post-Processing ---")

        hr_csv = self._session_dir / "heart_rate" / "hr_full_session.csv"
        composited_dir = self._session_dir / "composited"

        # Composite: overlay commentary audio on expanded overhead video
        overhead_path = str(self._session_dir / "performance" / "overhead_camera.mp4")
        audio_path = str(self._session_dir / "review" / "audio_commentary.wav")
        timestamps_path = str(self._session_dir / "review" / "review_timestamps.json")
        composite_output = str(composited_dir / "overhead_with_commentary.mp4")

        if Path(audio_path).exists() and Path(timestamps_path).exists() and Path(overhead_path).exists():
            try:
                create_review_composite(overhead_path, audio_path, timestamps_path, composite_output)
            except Exception as e:
                print(f"WARNING: Review composite failed: {e}")
        else:
            print("Skipping review composite (missing files)")

        # HR synced face videos
        if hr_csv.exists():
            review_face = str(self._session_dir / "review" / "face_cam.mp4")
            if Path(review_face).exists():
                try:
                    create_hr_synced_video(
                        review_face, str(hr_csv),
                        str(composited_dir / "review_face_with_hr.mp4"),
                        phase="review",
                    )
                except Exception as e:
                    print(f"WARNING: HR overlay for review failed: {e}")

            scoring_face = str(self._session_dir / "scoring" / "face_cam.mp4")
            if Path(scoring_face).exists():
                try:
                    create_hr_synced_video(
                        scoring_face, str(hr_csv),
                        str(composited_dir / "scoring_face_with_hr.mp4"),
                        phase="scoring",
                    )
                except Exception as e:
                    print(f"WARNING: HR overlay for scoring failed: {e}")
        else:
            print("Skipping HR overlay (no HR data)")

        print("Post-processing complete.")
        phase.complete()

    def _run_posthoc_calibration(self, phase: Phase):
        """Phase 8: GoPro upload dialog, run calibration, save results."""
        self._send_gui_event("request_gopro_upload",
                             message="Upload GoPro footage for calibration.")

        # Wait for user to upload files or skip
        print("Waiting for GoPro footage upload...")
        while True:
            try:
                action = self._user_action_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if action.get("type") == "stop":
                raise KeyboardInterrupt("User stopped experiment")
            if action.get("type") == "gopro_files":
                self._gopro_footage = action.get("files", [])
                break
            if action.get("type") == "continue":
                break

        # Copy GoPro files to session dir
        gopro_dir = self._session_dir / "gopro_footage"
        import shutil
        video_paths = []
        for src_path in self._gopro_footage:
            dst = gopro_dir / Path(src_path).name
            if not dst.exists():
                shutil.copy2(src_path, dst)
            video_paths.append(str(dst))
            print(f"GoPro footage: {src_path} -> {dst}")

        cal_settings = self.settings.get("calibration", {})

        if video_paths and (cal_settings.get("run_intrinsic", True) or
                            cal_settings.get("run_extrinsic", True)):
            print("\n--- Running Calibration ---")
            cal_dir = self._session_dir / "calibration"

            if cal_settings.get("run_intrinsic", True):
                # Run intrinsic + correction per video
                for vpath in video_paths:
                    result = calibrate_from_video(vpath)
                    if result:
                        K, D, frame_size = result
                        corrected_path = correct_video(vpath, K, D, frame_size)
                        # Move corrected to calibration dir
                        corrected = Path(corrected_path)
                        dst = cal_dir / corrected.name
                        corrected.rename(dst)
                        print(f"Corrected video: {dst}")

            if cal_settings.get("run_extrinsic", True) and len(video_paths) >= 2:
                calibrations = calibrate_all(video_paths)
                save_calibration_log(calibrations, str(cal_dir / "calibration_log.json"))
        else:
            print("Skipping calibration (no footage or disabled in settings)")

        phase.complete()

    def _run_nlf_mocap(self, phase: Phase):
        """Phase 9: Placeholder for future NLF MoCap integration."""
        print("\n--- NLF Motion Capture ---")
        print("Coming Soon: Neural Lifting for Motion Capture")
        print("This phase will integrate markerless motion capture in a future update.")
        phase.complete()

    # ======================================================================
    #  Main Loop
    # ======================================================================

    def run(self):
        """Main experiment loop with 9 phases."""
        if not self.setup():
            print("Setup failed!")
            return

        try:
            print(f"\nStarting experiment with {len(self.phases)} phases")
            self.current_phase.start()
            if self.hr_monitor and self.hr_enabled:
                self.hr_monitor.set_phase(self.current_phase.config.id)

            self._send_gui_event(
                "phase_change",
                phase_index=0,
                phase_id=self.current_phase.config.id,
                phase_name=self.current_phase.config.name,
                total_phases=len(self.phases),
            )

            while self.current_phase:
                phase = self.current_phase
                phase_id = phase.config.id

                # Route to phase-specific handler
                handler = {
                    "setup": self._run_setup,
                    "heart_rate_start": self._run_heart_rate_start,
                    "warmup_calibration": self._run_warmup_calibration,
                    "performance": self._run_performance,
                    "review": self._run_review,
                    "scoring": self._run_scoring,
                    "finish": self._run_finish,
                    "posthoc_calibration": self._run_posthoc_calibration,
                    "nlf_mocap": self._run_nlf_mocap,
                }.get(phase_id)

                if handler:
                    handler(phase)
                else:
                    # Generic phase handler for unknown phases
                    print(f"Unknown phase '{phase_id}', waiting for user to continue...")
                    self._send_gui_event("wait_for_continue",
                                         message=f"Phase: {phase.config.name}. Press Continue.")
                    self._wait_for_user_action("continue")
                    phase.complete()

                if not self.next_phase():
                    break

            print("\nExperiment complete!")
            self._send_gui_event("experiment_complete")

        except KeyboardInterrupt:
            print("\nExperiment interrupted by user")
        finally:
            self.teardown()
