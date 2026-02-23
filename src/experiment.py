"""Experiment state machine and main loop.

7-phase Taekwondo experiment flow with video recording, audio capture,
video playback, and compositing. Post-hoc calibration is available
separately from the Calibration tab.
"""

import json
import queue
import shutil
import time
from pathlib import Path
from typing import Optional

from .audio import AudioConfig, AudioRecorder
from .camera import CameraManager
from .compositing import (
    check_ffmpeg,
    create_hr_synced_video,
    create_review_composite,
)
from .gopro import GoProManager
from .heart_rate import PolarH10
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

        self._skip_remaining = False
        self._redo_requested = False
        self._hr_saved = False

        # Synchronization event log — timestamped record of every key moment
        self._sync_log = []

        # Persistent recorder for overhead camera (spans calibration -> performance)
        self._overhead_recorder: Optional["VideoRecorder"] = None

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
                if action.get("type") == "redo":
                    self._redo_requested = True
                    return None
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

    def _log_sync(self, event: str, **extra):
        """Append a timestamped event to the synchronization log."""
        entry = {"event": event, "wall_time": time.time()}
        entry.update(extra)
        self._sync_log.append(entry)

    def setup(self) -> bool:
        """Initialize experiment resources."""
        print(f"Setting up experiment: {self.name}")

        # Check ffmpeg
        if not check_ffmpeg():
            print("WARNING: ffmpeg not found on PATH. Compositing will be unavailable.")

        # Create session directory
        self._session_dir = self.output_dir / f"{self.name.replace(' ', '_')}_{self._session_timestamp}"
        self._session_dir.mkdir(parents=True, exist_ok=True)

        self._log_sync("session_created", session_dir=str(self._session_dir))
        return True

    def _backup_to_f_drive(self):
        """Copy the session folder to F:/Iris_Recorded_Taekwondo_Data/ as a backup."""
        if self._session_dir is None or not self._session_dir.exists():
            print("No session directory to back up.")
            return

        backup_root = Path("F:/Iris_Recorded_Taekwondo_Data")
        try:
            # Check if F: drive is available
            if not Path("F:/").exists():
                print("WARNING: F: drive not found. Skipping backup.")
                return

            backup_dest = backup_root / self._session_dir.name
            print(f"\n--- Backing Up to F: Drive ---")
            print(f"Source:      {self._session_dir}")
            print(f"Destination: {backup_dest}")

            backup_root.mkdir(parents=True, exist_ok=True)

            # Remove existing backup of this session if present
            if backup_dest.exists():
                shutil.rmtree(backup_dest)

            shutil.copytree(str(self._session_dir), str(backup_dest))
            print(f"Backup complete: {backup_dest}")

        except Exception as e:
            print(f"WARNING: Backup to F: drive failed: {e}")
            print("Primary data is still safe in the output directory.")

    def _save_sync_manifest(self):
        """Write the synchronization manifest to the session directory."""
        if not self._session_dir:
            return
        manifest = {
            "session": str(self._session_dir.name),
            "experiment_name": self.name,
            "gopro_mode": self.gopro_mode,
            "hr_enabled": self.hr_enabled,
            "mic_enabled": self.mic_enabled,
            "events": self._sync_log,
        }
        path = self._session_dir / "sync_manifest.json"
        with open(path, "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"Sync manifest saved to {path}")

    def teardown(self):
        """Clean up all resources."""
        print("\n--- Tearing Down Experiment ---")
        self._log_sync("teardown_start")

        # Stop overhead recorder if still active (safety net)
        if self._overhead_recorder:
            print("Stopping overhead recorder...")
            self._overhead_recorder.stop()
            self._log_sync("overhead_recorder_stop_safety")
            self._overhead_recorder = None

        # Stop Polar H10 recording and save data (skip if already done in _run_finish)
        if self.hr_monitor:
            if not self._hr_saved:
                self._log_sync("hr_stop_recording")
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

        self._log_sync("teardown_complete")
        self._save_sync_manifest()

        # Backup session data to F: drive
        self._backup_to_f_drive()

        print("Teardown complete")

    def next_phase(self) -> bool:
        """Advance to next phase. Returns False if no more phases."""
        self.current_phase_index += 1
        if self.current_phase_index >= len(self.phases):
            return False
        phase = self.current_phase
        phase.start()
        self._log_sync("phase_start",
                       phase_id=phase.config.id,
                       phase_name=phase.config.name,
                       phase_index=self.current_phase_index)
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
        """Phase 2: Connect Polar H10, start recording, auto-advance.

        If HR is enabled, retries connection until successful.  The experimenter
        cannot advance past this phase without a streaming HR monitor.
        """
        if self.hr_monitor and self.hr_enabled:
            connected = False
            attempt = 0
            while not connected:
                attempt += 1
                print(f"\n--- Connecting Polar H10 (attempt {attempt}) ---")
                self._send_gui_event("status",
                                     message=f"Connecting Polar H10 (attempt {attempt})...")
                self._log_sync("hr_connect_attempt", attempt=attempt)
                if self.hr_monitor.connect():
                    connected = True
                    self._log_sync("hr_connect_success")
                    self.hr_monitor.start_recording(phase="heart_rate_start")
                    self._log_sync("hr_recording_start", phase="heart_rate_start")
                    self._send_gui_event("status",
                                         message="Polar H10 connected and streaming.")
                    print("Polar H10 connected and streaming.")
                else:
                    self._log_sync("hr_connect_failed", attempt=attempt)
                    print(f"Polar H10 connection failed (attempt {attempt}). Retrying in 3s...")
                    self._send_gui_event("status",
                                         message=f"HR connection failed (attempt {attempt}). Retrying...")
                    # Check for stop during retry delay
                    try:
                        action = self._user_action_queue.get(timeout=3.0)
                        if action.get("type") == "stop":
                            raise KeyboardInterrupt("User stopped experiment")
                    except queue.Empty:
                        pass
        else:
            print("Heart rate monitoring disabled.")

        phase.complete()

    def _run_warmup_calibration(self, phase: Phase):
        """Phase 3: Open cameras, test mic, connect GoPros, record calibration. Wait for user Continue."""
        # Open USB cameras
        print("\n--- Opening USB Cameras ---")
        if not self.camera_manager.open_all():
            print("WARNING: Some USB cameras failed to connect")

        # Interactive camera role selection (when exactly 2 cameras are open)
        open_cameras = [c for c in self.camera_manager.cameras.values() if c.is_open]
        if len(open_cameras) == 2:
            print("\n--- Camera Role Selection ---")
            # Read 5 throwaway frames per camera for auto-exposure warmup
            for cam in open_cameras:
                for _ in range(5):
                    cam.read_frame()

            # Capture one preview frame from each camera
            preview_frames = {}
            camera_info = []
            for cam in open_cameras:
                frame = cam.read_frame()
                if frame is not None:
                    preview_frames[cam.config.id] = frame
                    camera_info.append({
                        "id": cam.config.id,
                        "name": cam.config.name,
                        "device_index": cam.config.device_index,
                    })

            if len(preview_frames) == 2:
                # Send previews to GUI and wait for user selection
                self._send_gui_event(
                    "camera_selection",
                    cameras=camera_info,
                    frames=preview_frames,
                )
                print("Waiting for user to assign camera roles...")
                action = self._wait_for_user_action("camera_role_selection", timeout=120)
                self._send_gui_event("hide_camera_selection")

                if action and "role_map" in action:
                    self.camera_manager.assign_roles(action["role_map"])
                    print("Camera roles assigned by user.")
                else:
                    print("WARNING: No camera role selection received, using defaults.")
            else:
                print("WARNING: Could not capture preview frames, using default roles.")
        else:
            print(f"Skipping camera selection ({len(open_cameras)} cameras open, need 2).")

        # Start overhead camera recording (continuous through performance)
        overhead_cam = self.camera_manager.get_camera_by_role("overhead")
        if overhead_cam is None:
            print("WARNING: No overhead camera found by role, using first available")
            if self.camera_manager.cameras:
                overhead_cam = next(iter(self.camera_manager.cameras.values()))

        overhead_path = str(self._session_dir / "performance" / "overhead_camera.mp4")
        if overhead_cam and overhead_cam.is_open:
            print(f"OVERHEAD CAM selected: '{overhead_cam.config.name}' "
                  f"(device_index={overhead_cam.config.device_index}, role={overhead_cam.config.role})")
            self._overhead_recorder = VideoRecorder(
                overhead_cam, overhead_path, fps=overhead_cam.config.fps
            )
            self._overhead_recorder.start()
            self._log_sync("overhead_recorder_start",
                           file="performance/overhead_camera.mp4",
                           fps=overhead_cam.config.fps)
            print(f"Overhead recording started (continuous through performance)")

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
            self._log_sync("gopro_recording_start", purpose="calibration")
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
                if self._overhead_recorder:
                    self._overhead_recorder.stop()
                    self._overhead_recorder = None
                self._send_gui_event("recording_status", recording=False)
                raise KeyboardInterrupt("User stopped experiment")
            if action.get("type") == "redo":
                if self.gopro_mode == "auto":
                    self.gopro_manager.stop_recording_all()
                if self._overhead_recorder:
                    self._overhead_recorder.stop()
                    self._overhead_recorder = None
                self._send_gui_event("recording_status", recording=False)
                self._redo_requested = True
                return
            if action.get("type") == "continue":
                break

        # Stop GoPro recording after calibration (overhead keeps recording)
        if self.gopro_mode == "auto":
            print("\n--- Stopping GoPro Recording (Calibration) ---")
            self.gopro_manager.stop_recording_all()
            self._log_sync("gopro_recording_stop", purpose="calibration")
        else:
            print("\nPlease stop GoPro recording manually now.")
            self._log_sync("gopro_manual_stop_prompted", purpose="calibration")
        self._send_gui_event("recording_status", recording=False)

        phase.complete()

    def _run_performance(self, phase: Phase):
        """Phase 4: Record overhead video + GoPros simultaneously.

        Overhead camera recording was started in warmup_calibration and continues here.
        """
        if self.hr_monitor and self.hr_enabled:
            self.hr_monitor.set_phase("performance")

        # Start GoPro recording
        if self.gopro_mode == "auto":
            self.gopro_manager.start_recording_all()
            self._log_sync("gopro_recording_start", purpose="performance")
        else:
            print("MANUAL MODE: Ensure GoPros are recording.")
            self._log_sync("gopro_manual_start_prompted", purpose="performance")

        recording_active = self._overhead_recorder is not None
        if recording_active:
            print("Overhead camera continuing to record (started in calibration)")
        else:
            print("WARNING: No overhead recorder active")

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
            if action and action.get("type") == "stop":
                raise KeyboardInterrupt("User stopped experiment")
            if action and action.get("type") == "redo":
                if self._overhead_recorder:
                    self._overhead_recorder.stop()
                    self._overhead_recorder = None
                if self.gopro_mode == "auto":
                    self.gopro_manager.stop_recording_all()
                self._send_gui_event("recording_status", recording=False)
                self._redo_requested = True
                return
            if action and action.get("type") == "continue":
                break

        # Stop overhead recording (was running since warmup_calibration)
        if self._overhead_recorder:
            self._log_sync("overhead_recorder_stop",
                           file="performance/overhead_camera.mp4")
            self._overhead_recorder.stop()
            self._overhead_recorder = None

        if self.gopro_mode == "auto":
            self.gopro_manager.stop_recording_all()
            self._log_sync("gopro_recording_stop", purpose="performance")
        else:
            print("MANUAL MODE: Please stop GoPro recording now.")
            self._log_sync("gopro_manual_stop_prompted", purpose="performance")

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
            self._send_gui_event("hide_video_player")
            phase.complete()
            return

        # Set up face cam recorder (pausable)
        face_cam = self.camera_manager.get_camera_by_role("face")
        face_recorder = None
        if face_cam and face_cam.is_open:
            print(f"REVIEW FACE CAM: '{face_cam.config.name}' "
                  f"(device_index={face_cam.config.device_index}, role={face_cam.config.role})")
            face_recorder = PausableVideoRecorder(face_cam, face_video_path, fps=face_cam.config.fps)
            face_recorder.start()
            self._log_sync("face_recorder_start",
                           file="review/face_cam.mp4",
                           fps=face_cam.config.fps,
                           phase="review")

        # Set up audio recorder
        audio_recorder = None
        if self.mic_enabled:
            audio_recorder = AudioRecorder(self.mic_config)
            if audio_recorder.open(audio_path):
                audio_recorder.start_recording()
                self._log_sync("audio_recorder_start",
                               file="review/audio_commentary.wav",
                               sample_rate=self.mic_config.sample_rate,
                               phase="review")
            else:
                audio_recorder = None

        # Timestamp log for pause/resume events
        timestamps = []

        # Send video frames to GUI via callback (pre-downscale for performance)
        import cv2 as _cv2

        def on_frame(frame, position_sec):
            # Downscale to 960px wide before sending to GUI to avoid lag
            h, w = frame.shape[:2]
            if w > 960:
                scale = 960 / w
                small = _cv2.resize(frame, (960, int(h * scale)), interpolation=_cv2.INTER_AREA)
            else:
                small = frame
            self._send_gui_event("video_frame", frame=small, position_sec=position_sec,
                                 duration_sec=player.duration_sec)

        def on_state_change(state):
            self._send_gui_event("player_state", state=state.name)

        def on_complete():
            self._send_gui_event("video_complete")

        player.on_frame = on_frame
        player.on_state_change = on_state_change
        player.on_complete = on_complete

        self._send_gui_event("show_video_player", allow_pause=True,
                             message="Review overhead video. Use Pause/Play for commentary.",
                             title="Narrating Review")
        self._log_sync("review_video_player_shown",
                       overhead_video="performance/overhead_camera.mp4",
                       duration_sec=player.duration_sec)
        self._send_gui_event("recording_status", recording=True, cameras=["face"])

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
            elif action_type == "redo":
                player.stop()
                player.close()
                if face_recorder:
                    face_recorder.stop()
                if audio_recorder:
                    audio_recorder.stop_recording()
                    audio_recorder.close()
                self._send_gui_event("recording_status", recording=False)
                self._send_gui_event("hide_video_player")
                self._redo_requested = True
                return
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
        self._log_sync("review_playback_stop")
        player.stop()
        player.close()
        if face_recorder:
            face_recorder.stop()
            self._log_sync("face_recorder_stop", file="review/face_cam.mp4", phase="review")
        if audio_recorder:
            audio_recorder.stop_recording()
            audio_recorder.close()
            self._log_sync("audio_recorder_stop", file="review/audio_commentary.wav", phase="review")

        self._send_gui_event("recording_status", recording=False)

        # Save timestamps
        with open(timestamps_path, "w") as f:
            json.dump(timestamps, f, indent=2)
        print(f"Review timestamps saved to {timestamps_path}")

        self._send_gui_event("hide_video_player")
        phase.complete()

    def _run_scoring(self, phase: Phase):
        """Phase 6: Play overhead video (no pause), record face cam + audio."""
        if self.hr_monitor and self.hr_enabled:
            self.hr_monitor.set_phase("scoring")

        overhead_path = str(self._session_dir / "performance" / "overhead_camera.mp4")
        face_video_path = str(self._session_dir / "scoring" / "face_cam.mp4")
        audio_path = str(self._session_dir / "scoring" / "audio_scoring.wav")

        # Set up video player
        player = VideoPlayer(overhead_path)
        if not player.open():
            print("WARNING: Cannot open overhead video for scoring. Skipping.")
            self._send_gui_event("hide_video_player")
            phase.complete()
            return

        # Set up face cam recorder (regular, not pausable)
        face_cam = self.camera_manager.get_camera_by_role("face")
        face_recorder = None
        if face_cam and face_cam.is_open:
            print(f"SCORING FACE CAM: '{face_cam.config.name}' "
                  f"(device_index={face_cam.config.device_index}, role={face_cam.config.role})")
            face_recorder = VideoRecorder(face_cam, face_video_path, fps=face_cam.config.fps)
            face_recorder.start()
            self._log_sync("face_recorder_start",
                           file="scoring/face_cam.mp4",
                           fps=face_cam.config.fps,
                           phase="scoring")

        # Set up audio recorder
        audio_recorder = None
        if self.mic_enabled:
            audio_recorder = AudioRecorder(self.mic_config)
            if audio_recorder.open(audio_path):
                audio_recorder.start_recording()
                self._log_sync("audio_recorder_start",
                               file="scoring/audio_scoring.wav",
                               sample_rate=self.mic_config.sample_rate,
                               phase="scoring")
            else:
                audio_recorder = None

        import cv2 as _cv2

        def on_frame(frame, position_sec):
            h, w = frame.shape[:2]
            if w > 960:
                scale = 960 / w
                small = _cv2.resize(frame, (960, int(h * scale)), interpolation=_cv2.INTER_AREA)
            else:
                small = frame
            self._send_gui_event("video_frame", frame=small, position_sec=position_sec,
                                 duration_sec=player.duration_sec)

        player.on_frame = on_frame

        self._send_gui_event("show_video_player", allow_pause=False,
                             message="Scoring: Press Start to begin. Face camera is recording.",
                             title="Self-Scoring")
        self._log_sync("scoring_video_player_shown",
                       overhead_video="performance/overhead_camera.mp4",
                       duration_sec=player.duration_sec)
        self._send_gui_event("recording_status", recording=True, cameras=["face"])

        video_finished = False

        def mark_complete():
            nonlocal video_finished
            video_finished = True

        player.on_complete = mark_complete

        def _cleanup_scoring():
            player.stop()
            player.close()
            if face_recorder:
                face_recorder.stop()
            if audio_recorder:
                audio_recorder.stop_recording()
                audio_recorder.close()
            self._send_gui_event("recording_status", recording=False)
            self._send_gui_event("hide_video_player")

        # Wait for user to press Start (GUI handles countdown, then sends "play")
        print("Scoring phase: Waiting for user to start...")
        while True:
            try:
                action = self._user_action_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if action.get("type") == "stop":
                raise KeyboardInterrupt("User stopped experiment")
            if action.get("type") == "redo":
                _cleanup_scoring()
                self._redo_requested = True
                return
            if action.get("type") == "play":
                player.play()
                self._log_sync("scoring_playback_start")
                break

        print("Scoring video playing...")
        while not video_finished:
            try:
                action = self._user_action_queue.get(timeout=0.5)
                if action.get("type") == "stop":
                    raise KeyboardInterrupt("User stopped experiment")
                if action.get("type") == "redo":
                    _cleanup_scoring()
                    self._redo_requested = True
                    return
                if action.get("type") == "continue":
                    break
            except queue.Empty:
                self._send_gui_event("player_progress",
                                     position_sec=player.position_sec,
                                     duration_sec=player.duration_sec)

        self._send_gui_event("recording_status", recording=False)

        self._log_sync("scoring_playback_stop")
        player.stop()
        player.close()
        if face_recorder:
            face_recorder.stop()
            self._log_sync("face_recorder_stop", file="scoring/face_cam.mp4", phase="scoring")
        if audio_recorder:
            audio_recorder.stop_recording()
            audio_recorder.close()
            self._log_sync("audio_recorder_stop", file="scoring/audio_scoring.wav", phase="scoring")

        self._send_gui_event("hide_video_player")
        phase.complete()

    def _run_finish(self, phase: Phase):
        """Phase 7: Wait for checklist, stop HR, save data, run compositing."""
        if self.hr_monitor and self.hr_enabled:
            self.hr_monitor.set_phase("finish")

        # Wait for user to confirm they want to stop HR (checklist gates Continue)
        self._send_gui_event("wait_for_continue",
                             message="Check the requirement below, then press Finish Experiment.")
        print("Waiting for user to confirm HR stop...")
        result = self._wait_for_user_action("continue")
        if self._redo_requested:
            return

        # --- Stop HR recording and save data ---
        self._log_sync("hr_stop_in_finish")
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
                for ph, stats in summary.items():
                    rr_info = ""
                    if "avg_rr_ms" in stats:
                        rr_info = f", avg_rr={stats['avg_rr_ms']}ms ({stats['rr_count']} beats)"
                    print(f"  {ph}: avg={stats['avg_bpm']} bpm, "
                          f"min={stats['min_bpm']}, max={stats['max_bpm']}, "
                          f"samples={stats['count']}{rr_info}")
            self._hr_saved = True

        # --- Post-Processing ---
        print("\n--- Post-Processing ---")
        self._send_gui_event("status", message="Running post-processing...")

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

    # ======================================================================
    #  Main Loop
    # ======================================================================

    def run(self):
        """Main experiment loop."""
        if not self.setup():
            print("Setup failed!")
            return

        try:
            print(f"\nStarting experiment with {len(self.phases)} phases")
            self._log_sync("experiment_start", total_phases=len(self.phases))
            self.current_phase.start()
            self._log_sync("phase_start",
                           phase_id=self.current_phase.config.id,
                           phase_name=self.current_phase.config.name,
                           phase_index=0)
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

                # Handle redo: go back one phase and re-run
                if self._redo_requested:
                    self._redo_requested = False
                    if self.current_phase_index > 0:
                        self.current_phase_index -= 1
                    prev = self.current_phase
                    prev.start()
                    self._log_sync("phase_redo",
                                   phase_id=prev.config.id,
                                   phase_name=prev.config.name,
                                   phase_index=self.current_phase_index)
                    if self.hr_monitor and self.hr_enabled:
                        self.hr_monitor.set_phase(prev.config.id)
                    self._send_gui_event(
                        "phase_change",
                        phase_index=self.current_phase_index,
                        phase_id=prev.config.id,
                        phase_name=prev.config.name,
                        total_phases=len(self.phases),
                    )
                    continue

                if self._skip_remaining:
                    break
                if not self.next_phase():
                    break

            print("\nExperiment complete!")
            self._send_gui_event("experiment_complete")

        except KeyboardInterrupt:
            print("\nExperiment interrupted by user")
        finally:
            self.teardown()
