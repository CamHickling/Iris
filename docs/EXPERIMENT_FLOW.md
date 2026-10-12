# Iris - Experiment Flow

This document describes the lifecycle of an Iris session, from launch to the files left on disk. The orchestration lives in `src/experiment.py` (`Experiment`).

---

## Entry Points

| Command | Purpose |
|---|---|
| `python gui.py` (or double-click `Iris.pyw`) | Full-screen GUI. The normal way to run a session. |
| `python main.py` | Run an experiment from the terminal with `settings.json` |
| `python main.py --config FILE` | Same, with a different config file |
| `python main.py --calibrate` | Pre-flight hardware check |
| `python main.py --undistort PATH` | GoPro lens correction on a file or folder |

The GUI runs the `Experiment` in a worker thread and talks to it through two queues:

- **GUI event queue** (experiment → GUI): `phase_change`, `status`, `wait_for_continue`, `recording_status`, `camera_selection`, `show_video_player`, `video_frame`, `player_progress`, `experiment_complete`, …
- **User action queue** (GUI → experiment): `continue`, `redo`, `stop`, `play`, `pause`, `camera_role_selection`

---

## Lifecycle

### 1. Initialization

`Experiment.__init__()` reads the settings and creates:

- `CameraManager` for the USB webcams
- `GoProManager` for the GoPros. In **manual GoPro mode** (chosen in the GUI), no GoPros are controlled and the experimenter is prompted to start/stop them by hand.
- `PolarH10` monitor, if `heart_rate.enabled` is true
- `AudioConfig` for the microphone
- A `Phase` for each entry in `phases`
- A session timestamp used to name the session directory

### 2. Setup

`Experiment.setup()` checks that ffmpeg is on `PATH` (compositing is skipped without it) and creates the session directory:

```
<output_dir>/<Experiment_Name>_<YYYYMMDD_HHMMSS>/
```

### 3. Phases

Phases run in order. Each phase `id` routes to a handler:

| `id` | Handler |
|---|---|
| `setup` | `_run_setup` |
| `heart_rate_start` | `_run_heart_rate_start` |
| `warmup_calibration` | `_run_warmup_calibration` |
| `performance` | `_run_performance` |
| `review` | `_run_review` |
| `scoring` | `_run_scoring` |
| `finish` | `_run_finish` |

A phase with any other `id` runs a generic handler that shows its name and waits for **Continue**.

At every phase change the HR monitor's phase label is updated (so every HR/ECG sample is tagged), and a `phase_start` event is written to the sync log.

Some phases have a **checklist** in the GUI (`_PHASE_CHECKLISTS` in `gui.py`). Continue stays disabled until every item is ticked.

#### Phase 1: Setup

Creates the session subdirectories (`performance/`, `review/`, `scoring/`, `composited/`, …) and shows the experiment name.

#### Phase 2: Heart Rate Start

Connects to the Polar H10 over BLE, retrying every 3 seconds until it succeeds. The session can't move on without a streaming HR monitor, but Stop still works. Once connected, HR recording starts and **runs for the rest of the session**. If HR is disabled in settings, this phase is a no-op.

#### Phase 3: Warmup & Calibration

1. **Open USB cameras.** Failures are logged, but the session continues.
2. **Assign camera roles.** If exactly two cameras are open, a preview frame from each is shown, and the experimenter picks which is `overhead` and which is `face`.
3. **Test the microphone** (if enabled) by looking up the configured device.
4. **GoPros:**
   - *Auto mode:* connect all GoPros and start recording.
   - *Manual mode:* prompt the experimenter to start them.
5. **T-pose.** The participant stands in a T-pose for motion-capture calibration, and checkerboard calibration footage is captured.
6. **Clap sync.** The participant claps once on camera, giving a shared audio/visual event to align recordings later (`clap_sync_requested` / `clap_sync_done` in the manifest).
7. **Stop GoPro recording.**

Checklist: GoPros recording, HR monitor on, cameras positioned, microphone on, intrinsic and extrinsic checkerboarding complete, T-pose, clap.

#### Phase 4: Performance

1. Start recording the **overhead** camera to `performance/overhead_camera.mp4`.
2. Start the GoPros (auto mode) or prompt the experimenter (manual mode).
3. Wait for **Continue** when the participant finishes.
4. Stop the overhead recorder and the GoPros.

Checklist: participant has completed all movements, GoPro recording stopped.

#### Phase 5: Narrating Review

The overhead video plays in the GUI's video player while the participant narrates.

- The **face camera** records to `review/face_cam.mp4` using a pausable recorder. It pauses and resumes along with the video.
- The **microphone** records continuously to `review/audio_commentary.wav`, so commentary made while the video is paused is kept.
- Every pause and resume is written to `review/review_timestamps.json` with its wall-clock time and video position.

#### Phase 6: Self-Scoring

The overhead video plays again, this time **without pause**. A countdown runs after the participant presses Start. The face camera and microphone record to `scoring/face_cam.mp4` and `scoring/audio_scoring.wav`.

#### Phase 7: Finish

1. Wait for the experimenter to confirm stopping HR (checklist item).
2. Stop HR recording and save `heart_rate/hr_full_session.csv` (and `ecg_full_session.csv` if ECG is enabled). Print a per-phase HR summary.
3. **Post-processing** (requires ffmpeg):
   - `composited/overhead_with_commentary.mp4`: the overhead video with frozen frames inserted at each pause, so the narration lines up with what the participant was watching, muxed with the commentary audio.
   - `composited/review_face_with_hr.mp4` and `composited/scoring_face_with_hr.mp4`: the face videos with a live HR overlay.

Missing inputs (e.g. no microphone) skip the step that needs them, with a warning.

#### Redo and Stop

- **Redo** steps back one phase and runs it again. Recorders started by the current phase are stopped first. A `phase_redo` event is logged.
- **Stop** ends the session immediately; teardown still runs.

### 4. Teardown

`Experiment.teardown()` runs in a `finally` block, so it executes even when a session is stopped early:

1. Stop the overhead recorder if it is still running.
2. Stop and save HR data if Finish didn't already, then disconnect the Polar H10.
3. Stop and disconnect GoPros (auto mode), or remind the experimenter to stop them (manual mode).
4. Close the USB cameras.
5. Write `sync_manifest.json`.
6. Copy the session directory to `experiment.backup_dir` (default `F:/Iris_Recorded_Taekwondo_Data`). This is skipped with a warning if the drive is not attached. Set `backup_dir` to `null` to turn backup off.

---

## Output Structure

```
<Experiment_Name>_<YYYYMMDD_HHMMSS>/
├── sync_manifest.json
├── performance/overhead_camera.mp4
├── review/
│   ├── face_cam.mp4
│   ├── audio_commentary.wav
│   └── review_timestamps.json
├── scoring/
│   ├── face_cam.mp4
│   └── audio_scoring.wav
├── heart_rate/
│   ├── hr_full_session.csv
│   └── ecg_full_session.csv
├── gopro_footage/
├── composited/
│   ├── overhead_with_commentary.mp4
│   ├── review_face_with_hr.mp4
│   └── scoring_face_with_hr.mp4
└── calibration/
```

**Heart rate CSV columns:** `timestamp, bpm, rr_intervals_ms, sensor_contact, phase`

**ECG CSV columns:** `timestamp, phase, ecg_values_uv`

GoPro footage stays on the cameras' SD cards. Copy it into `gopro_footage/` after the session; the GUI's Calibration tab can then run intrinsic/extrinsic calibration on it.

See [SYNCHRONIZATION.md](SYNCHRONIZATION.md) for the manifest format and how to align streams.

---

## Calibration Mode

```
python main.py --calibrate
```

`CalibrationTool` (`src/calibrate.py`) checks every configured device:

1. **USB cameras:** open and capture one frame
2. **GoPros:** connect, read model and battery
3. **Polar H10:** connect, read battery, record 3 seconds to confirm signal

It prints a pass/fail summary and exits with code 0 if everything passed, otherwise 1.

---

## Lens Correction

```
python main.py --undistort /path/to/videos/   # every .mp4 in the folder
python main.py --undistort /path/to/clip.mp4  # a single file
```

`src/gopro_undistort.py` removes barrel distortion using estimated Hero 7 Silver wide-lens parameters and writes `<name>_undistorted.mp4` next to each input.

For a measured correction, `src/intrinsic_calibration.py` calibrates from a checkerboard video (see `docs/checkerboard.pdf`), and `src/extrinsic_calibration.py` estimates the relative pose between cameras from shared checkerboard views.

---

## Key Design Decisions

- **Continuous HR recording.** HR runs from Phase 2 to Finish without gaps and is labeled by phase, rather than being started and stopped per phase.
- **One shared clock.** Every device event is logged with `time.time()` to the sync manifest, so streams can be aligned afterwards even though they're recorded by separate threads and devices.
- **Clap sync.** A physical clap gives a single event that is visible and audible in every stream, as a cross-check on the timestamps.
- **Pause-aware compositing.** The review composite inserts frozen frames during pauses, so commentary stays lined up with what was on screen.
- **Graceful degradation.** A missing device produces a warning and the session continues. Post-processing skips any step whose inputs are missing.
- **Threaded GoPro control.** Connect, start/stop, and keep-alive run in parallel to keep timing skew between cameras small. Keep-alive packets are sent every 2.5 s over a socket bound to each camera's WiFi adapter.
- **Safe teardown.** Teardown runs in `finally`, so stopping mid-session still saves data, writes the manifest, and releases hardware.

---

## Source Files

| File | Role |
|---|---|
| `gui.py` | CustomTkinter GUI, checklists, video player, settings editor |
| `main.py` | CLI entry point |
| `src/experiment.py` | Experiment orchestrator and phase handlers |
| `src/phase.py` | Phase model (pending/active/completed/skipped) |
| `src/camera.py` | USB cameras via OpenCV, role assignment |
| `src/video_recorder.py` | Threaded video recording, including pausable recording |
| `src/video_player.py` | Threaded playback engine for review and scoring |
| `src/audio.py` | Microphone recording |
| `src/gopro.py` | GoPro WiFi control with per-interface routing |
| `src/heart_rate.py` | Polar H10 HR, RR, and ECG over BLE |
| `src/compositing.py` | ffmpeg review composite and HR overlays |
| `src/calibrate.py` | Pre-flight hardware check |
| `src/intrinsic_calibration.py` | Checkerboard lens calibration |
| `src/extrinsic_calibration.py` | Multi-camera stereo calibration |
| `src/gopro_undistort.py` | GoPro barrel-distortion removal |
| `src/utils.py` | Timestamps, directory helpers, `Timer` |
