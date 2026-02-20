# Iris - Experiment Flow

This document describes the complete lifecycle of a Iris experiment, from launch to data output.

---

## Entry Point

```
python main.py --config settings.json
```

`main.py` supports three mutually exclusive modes:

| Flag | Purpose |
|---|---|
| *(none)* | Run a full experiment |
| `--calibrate` | Pre-flight hardware check |
| `--undistort PATH` | Post-process GoPro videos (lens correction) |

When no special flag is given, `main.py` loads the config file, creates an `Experiment` instance, and calls `experiment.run()`.

---

## Hardware Overview

A typical setup configured in `settings.json`:

- **2 USB Logitech webcams** (front + side views at 1080p/30fps, device indices 0 and 1)
- **2 GoPro cameras** controlled over WiFi (2x Hero 7 Silver), each on a separate WiFi adapter (`en1`-`en2`)
- **1 Polar H10** heart rate monitor connected over Bluetooth Low Energy

All GoPros share the same default IP (`10.5.5.9`) but are reached through different network interfaces.

---

## Experiment Lifecycle

### 1. Initialization

`Experiment.__init__()` reads the config and creates:

- `CameraManager` for USB cameras
- `GoProManager` for GoPros
- `PolarH10` monitor (if `heart_rate.enabled` is true)
- A list of `Phase` objects from the phases config
- A session timestamp (e.g. `20260219_143022`) used to name output files

### 2. Setup

`Experiment.setup()` brings hardware online:

1. **Create output directory** (`output_dir` from config)
2. **Connect Polar H10** via BLE scan (up to 25s). If it fails, HR is disabled and the experiment continues.
3. **Open USB cameras**. A warning is printed if any fail; the experiment continues.

GoPros are *not* connected here -- they connect during the calibration phase.

### 3. Phase Execution

The experiment runs through phases in order. Each phase has:

| Field | Meaning |
|---|---|
| `id` | Identifier used to route to a handler and label data |
| `name` | Display name |
| `duration_seconds` | How long to run (0 = wait for Ctrl+C) |
| `capture_interval_ms` | IP camera frame capture interval (`null` = no capture) |
| `instructions` | Printed for the operator |

The default config defines three phases:

#### Phase 1: Calibration (10 seconds)

Handler: `_run_calibration()`

1. **Start HR recording** with phase label `"calibration"`
2. **Connect all 2 GoPros** in parallel (threaded)
3. **Enter timing loop:**
   - Every 100ms: capture frames from USB cameras, save to `output/frames/calibration/`
   - Every 2.5s: send GoPro keep-alive signals to prevent WiFi timeout
4. Phase auto-completes after 10 seconds

#### Phase 2: Recording (60 seconds)

Handler: `_run_recording()`

1. **Update HR phase** to `"recording"`
2. **Start all GoPro recordings** simultaneously (threaded shutter trigger)
3. **Enter timing loop:**
   - Every 33ms (~30fps): capture USB camera frames, save to `output/frames/recording/`
   - Every 2.5s: GoPro keep-alive
4. Phase auto-completes after 60 seconds
5. **Stop all GoPro recordings**

#### Phase 3: Post-Recording (indefinite)

Handler: `_run_post_recording()`

1. **Update HR phase** to `"post_recording"`
2. **Print GoPro battery status** for each camera
3. **Wait for Ctrl+C** (duration is 0, so the loop runs indefinitely)
4. GoPro keep-alive signals continue every 2.5s

> HR recording runs *continuously* from calibration start through post-recording, stopping only at teardown. This captures the full physiological timeline.

#### Phase Advancement

Between phases, `next_phase()` advances the index and updates the HR phase label. The first phase is started explicitly before the main loop begins.

#### Generic Phases

Any phase whose `id` doesn't match `"calibration"`, `"recording"`, or `"post_recording"` runs through a generic handler that captures USB camera frames on the configured interval.

### 4. Teardown

`Experiment.teardown()` runs in a `finally` block, so it executes even if the user presses Ctrl+C:

1. **Stop Polar H10 recording**
2. **Save heart rate CSV** to `output/heart_rate_YYYYMMDD_HHMMSS.csv`
3. **Save ECG CSV** to `output/ecg_YYYYMMDD_HHMMSS.csv` (if ECG was enabled)
4. **Print HR summary** -- min/max/avg BPM and RR interval stats per phase
5. **Disconnect all GoPros**
6. **Close all USB cameras**

---

## Output Structure

```
output/
  frames/
    calibration/
      cam_0_000001.jpg
      cam_0_000002.jpg
      cam_1_000001.jpg
      cam_1_000002.jpg
      ...
    recording/
      cam_0_000001.jpg
      ...
  heart_rate_20260219_143022.csv
  ecg_20260219_143022.csv
```

**Heart rate CSV columns:** `timestamp, bpm, rr_intervals_ms, sensor_contact, phase`

**ECG CSV columns:** `timestamp, phase, ecg_values_uv`

GoPro footage is saved to the cameras' own SD cards and must be downloaded separately after the experiment.

---

## Calibration Mode

```
python main.py --calibrate
```

Runs `CalibrationTool` which performs a pre-flight check of all configured hardware:

1. **USB cameras** -- attempts connection and a single frame capture
2. **GoPros** -- connects, checks battery and model info
3. **Polar H10** -- connects, reads battery, records 3 seconds to verify signal

Prints a per-device pass/fail summary and exits with code 0 (all pass) or 1 (any fail).

---

## Lens Correction Mode (Post-Processing)

```
python main.py --undistort /path/to/videos/      # process all .mp4 files in directory
python main.py --undistort /path/to/clip.mp4      # process a single file
```

After downloading GoPro footage from SD cards, this mode applies barrel distortion correction using estimated calibration data for the GoPro Hero 7 Silver wide lens.

- Processes every frame through `cv2.undistort()` with pre-configured K (camera matrix) and D (distortion coefficients)
- Output files are saved alongside originals with an `_undistorted` suffix
- Runs headless (no GUI windows) with frame-count progress output
- Skips all experiment setup -- processes files and exits

For more precise correction, `src/lens_correct.py` provides an advanced pipeline that calibrates from a checkerboard video and produces side-by-side comparisons.

---

## Key Design Decisions

- **Continuous HR recording**: Heart rate data is captured from calibration start through post-recording end, stopped only at teardown. This ensures no gaps in physiological data.
- **Threaded GoPro operations**: connect, start/stop recording, and keep-alive are all threaded to minimize timing skew between cameras.
- **Keep-alive signals**: GoPros disconnect if they don't receive periodic WiFi pings, so the experiment loop sends keep-alive every 2.5 seconds during all phases.
- **Graceful degradation**: Failed hardware (cameras, GoPros, HR monitor) prints a warning but doesn't abort the experiment.
- **Phase-labeled data**: Every HR/ECG sample is tagged with the current phase ID, enabling per-phase analysis in post-processing.
- **Ctrl+C safety**: Teardown runs in a `finally` block, so interrupting any phase still saves data and disconnects hardware cleanly.

---

## Configuration Reference

See `settings.json` for the full config structure. Key sections:

| Section | Purpose |
|---|---|
| `experiment` | Name, output directory, save format |
| `cameras` | USB camera device index and settings |
| `gopros` | GoPro model, WiFi interface, IP |
| `heart_rate` | Polar H10 toggle, BLE address, ECG toggle |
| `phases` | Ordered list of experiment phases |

---

## Source Files

| File | Role |
|---|---|
| `main.py` | CLI entry point and argument routing |
| `src/experiment.py` | Experiment orchestrator and phase routing |
| `src/phase.py` | Phase state machine (pending/active/completed/skipped) |
| `src/camera.py` | USB camera capture via OpenCV |
| `src/gopro.py` | GoPro WiFi control via goprocam library |
| `src/heart_rate.py` | Polar H10 BLE heart rate and ECG via bleak |
| `src/calibrate.py` | Pre-flight hardware verification tool |
| `src/len_correction.py` | Headless GoPro video undistortion (batch or single file) |
| `src/lens_correct.py` | Advanced checkerboard-based calibration and correction |
| `src/utils.py` | Timestamp formatting, directory helpers, Timer class |
