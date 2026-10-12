# Iris — Synchronization Reference

This document describes how every data stream in an Iris session relates to a single master timeline, and how to align them in post-processing.

---

## 1. The Master Clock

Every sync-critical moment is recorded as a **Unix timestamp** (`time.time()` — seconds since 1970-01-01 UTC, float with microsecond precision). This is the single source of truth for aligning all components.

The file **`sync_manifest.json`**, saved at the root of every session directory, contains a chronological list of these timestamped events.

---

## 2. Session Directory Layout

```
Session_Name_20260222_143012/
├── sync_manifest.json              ← MASTER SYNC FILE
├── performance/
│   └── overhead_camera.mp4         ← Overhead USB camera (warmup through performance)
├── review/
│   ├── face_cam.mp4                ← Face USB camera (pausable)
│   ├── audio_commentary.wav        ← Microphone (with pauses)
│   └── review_timestamps.json      ← Pause/resume wall-clock + video-position pairs
├── scoring/
│   ├── face_cam.mp4                ← Face USB camera (continuous)
│   └── audio_scoring.wav           ← Microphone (continuous)
├── heart_rate/
│   ├── hr_full_session.csv         ← HR + RR intervals, timestamped per sample
│   └── ecg_full_session.csv        ← Raw ECG waveform, timestamped per batch
├── gopro_footage/                  ← Manually copied GoPro files
├── composited/                     ← Post-processed outputs
│   ├── overhead_with_commentary.mp4
│   ├── review_face_with_hr.mp4
│   └── scoring_face_with_hr.mp4
└── calibration/
```

---

## 3. sync_manifest.json — Format

```json
{
  "session": "Taekwondo_Experiment_-_P01_20260222_143012",
  "experiment_name": "Taekwondo Experiment - P01",
  "gopro_mode": "manual",
  "hr_enabled": true,
  "mic_enabled": true,
  "events": [
    {"event": "session_created",          "wall_time": 1740234612.123, "session_dir": "..."},
    {"event": "experiment_start",         "wall_time": 1740234612.456, "total_phases": 7},
    {"event": "phase_start",              "wall_time": 1740234612.500, "phase_id": "setup", ...},
    {"event": "hr_connect_attempt",       "wall_time": 1740234614.100},
    {"event": "hr_connect_success",       "wall_time": 1740234618.200},
    {"event": "hr_recording_start",       "wall_time": 1740234618.300, "phase": "heart_rate_start"},
    {"event": "overhead_recorder_start",  "wall_time": 1740234625.000, "file": "performance/overhead_camera.mp4", "fps": 30},
    {"event": "gopro_manual_start_prompted", "wall_time": 1740234626.000, "purpose": "calibration"},
    {"event": "gopro_manual_stop_prompted",  "wall_time": 1740234680.000, "purpose": "calibration"},
    {"event": "gopro_manual_start_prompted", "wall_time": 1740234685.000, "purpose": "performance"},
    {"event": "overhead_recorder_stop",   "wall_time": 1740234750.000, "file": "performance/overhead_camera.mp4"},
    {"event": "gopro_manual_stop_prompted", "wall_time": 1740234750.500, "purpose": "performance"},
    {"event": "face_recorder_start",      "wall_time": 1740234755.000, "file": "review/face_cam.mp4", "fps": 30, "phase": "review"},
    {"event": "audio_recorder_start",     "wall_time": 1740234755.100, "file": "review/audio_commentary.wav", "sample_rate": 44100, "phase": "review"},
    {"event": "review_video_player_shown","wall_time": 1740234755.200, "overhead_video": "performance/overhead_camera.mp4", "duration_sec": 125.0},
    {"event": "review_playback_stop",     "wall_time": 1740234900.000},
    {"event": "face_recorder_stop",       "wall_time": 1740234900.100, "file": "review/face_cam.mp4", "phase": "review"},
    {"event": "audio_recorder_stop",      "wall_time": 1740234900.200, "file": "review/audio_commentary.wav", "phase": "review"},
    {"event": "face_recorder_start",      "wall_time": 1740234910.000, "file": "scoring/face_cam.mp4", "fps": 30, "phase": "scoring"},
    {"event": "audio_recorder_start",     "wall_time": 1740234910.100, "file": "scoring/audio_scoring.wav", "sample_rate": 44100, "phase": "scoring"},
    {"event": "scoring_video_player_shown","wall_time": 1740234910.200, ...},
    {"event": "scoring_playback_start",   "wall_time": 1740234915.000},
    {"event": "scoring_playback_stop",    "wall_time": 1740235040.000},
    {"event": "face_recorder_stop",       "wall_time": 1740235040.100, "file": "scoring/face_cam.mp4", "phase": "scoring"},
    {"event": "audio_recorder_stop",      "wall_time": 1740235040.200, "file": "scoring/audio_scoring.wav", "phase": "scoring"},
    {"event": "hr_stop_recording",        "wall_time": 1740235100.000},
    {"event": "teardown_complete",        "wall_time": 1740235102.000}
  ]
}
```

Every `wall_time` value is a Unix timestamp (float). Convert to human-readable with:
```python
from datetime import datetime
datetime.fromtimestamp(1740234612.123)  # → 2025-02-22 14:30:12.123000
```

---

## 4. Component-by-Component Sync Details

### 4.1 Heart Rate (Polar H10) → Session Clock

**Already natively synced.** Each HR and ECG sample carries a `time.time()` Unix timestamp.

| File | Columns | Clock | Rate |
|------|---------|-------|------|
| `hr_full_session.csv` | `timestamp, bpm, rr_intervals_ms, sensor_contact, phase` | `time.time()` | ~1 Hz |
| `ecg_full_session.csv` | `timestamp, phase, ecg_values_uv` | `time.time()` | 130 Hz (batched) |

**To align HR with any other event:**
```python
# Find HR samples during performance phase
import csv
with open("heart_rate/hr_full_session.csv") as f:
    reader = csv.DictReader(f)
    for row in reader:
        if row["phase"] == "performance":
            t = float(row["timestamp"])  # Unix timestamp — directly comparable to sync_manifest
```

**Phase labels** (`phase` column) change at the exact `wall_time` of each `phase_start` event in the sync manifest. The phase label in the CSV tells you which experiment phase each sample belongs to.

**RR intervals** (`rr_intervals_ms` column) are semicolon-separated beat-to-beat intervals in milliseconds. Multiple RR intervals may arrive in a single sample notification.

---

### 4.2 Overhead Camera → Session Clock

**File:** `performance/overhead_camera.mp4`
**Frame rate:** Configured FPS (typically 30), recorded in the manifest as `fps` on the `overhead_recorder_start` event.

The overhead camera records continuously from warmup/calibration through the end of performance. It has **no embedded wall-clock timestamps** — time within the video is purely frame-based.

**Sync anchor:** The `overhead_recorder_start` event in the sync manifest gives the wall-clock moment the first frame was captured.

```
Video time (seconds) = frame_number / fps
Wall-clock time      = overhead_recorder_start.wall_time + (frame_number / fps)
```

**To convert a wall-clock time to a video position:**
```python
video_sec = target_wall_time - manifest_events["overhead_recorder_start"]["wall_time"]
frame_num = int(video_sec * fps)
```

---

### 4.3 Face Camera → Session Clock

**Files:**
- `review/face_cam.mp4` — pausable recording (frozen frames during pauses)
- `scoring/face_cam.mp4` — continuous recording

**Sync anchors:** `face_recorder_start` events in the manifest (one per phase) give the wall-clock start of each recording.

#### Review Phase (Pausable)
During pauses, the `PausableVideoRecorder` writes **frozen frames** (copies of the last real frame) to maintain a 1:1 relationship between frame count and elapsed wall-clock time. This means:

```
Wall-clock time = face_recorder_start.wall_time + (frame_number / fps)
```

The `review/review_timestamps.json` file provides exact pause/resume moments with both `wall_time` and `video_position_sec`, which can be used to identify which segments contain frozen vs. live frames.

#### Scoring Phase (Continuous)
No pauses. Simple frame-count relationship:

```
Wall-clock time = face_recorder_start.wall_time + (frame_number / fps)
```

---

### 4.4 Microphone → Session Clock

**Files:**
- `review/audio_commentary.wav` — with pause gaps (silence during pauses)
- `scoring/audio_scoring.wav` — continuous

**Format:** PCM 16-bit WAV, 44100 Hz mono (configurable; sample rate recorded in manifest).

**Sync anchor:** `audio_recorder_start` events in the manifest.

```
Wall-clock time = audio_recorder_start.wall_time + (sample_number / sample_rate)
```

The audio recorder uses a real-time callback stream — samples arrive continuously from the sound card at the configured sample rate. During review pauses, silence is captured (the recorder keeps running while the face camera writes frozen frames).

---

### 4.5 Face Camera + Overhead Playback + Microphone (Intra-Phase Sync)

During review and scoring, three streams run simultaneously:
1. **Overhead video playback** (being watched by participant)
2. **Face camera recording** (recording the participant)
3. **Microphone recording** (recording the participant's voice)

These three are started within milliseconds of each other. Their start times are all recorded individually in the sync manifest. The **precise offset** between any two is:

```python
offset = stream_B_start_wall_time - stream_A_start_wall_time
```

For example, to align face video with audio in the review phase:
```python
face_start = get_event("face_recorder_start", phase="review")["wall_time"]
audio_start = get_event("audio_recorder_start", phase="review")["wall_time"]
offset_sec = audio_start - face_start
# Positive offset means audio started later; pad audio with silence at the start
```

In practice the offset is typically < 100ms because both are started in immediate succession.

---

### 4.6 GoPro Cameras → Session Clock

GoPros are operated in **manual mode** — the experimenter starts and stops them by hand. The sync manifest records `gopro_manual_start_prompted` and `gopro_manual_stop_prompted` events, which log the wall-clock time when the **GUI displayed the prompt** to the experimenter. The actual button press on the GoPro happens some seconds after.

#### Sync Strategy for GoPros

**Option A — Clap/Visual Sync (Recommended)**

Use a clearly visible and audible event (sharp clap, clapperboard, or LED flash) that appears in both the GoPro footage and the overhead camera footage. Then:

1. Find the clap frame in the overhead video → convert to wall-clock time using the overhead sync anchor
2. Find the same clap frame in the GoPro video → note the GoPro-internal timestamp
3. `offset = wall_clock_of_clap - gopro_internal_time_of_clap`
4. Apply this offset to all GoPro timestamps

**Option B — GoPro File Metadata**

GoPro MP4 files contain creation timestamps in their metadata:
```bash
ffprobe -v quiet -print_format json -show_format gopro_video.mp4
# Look for: format.tags.creation_time
```

This gives the GoPro's internal clock time for the start of recording. If the GoPro's clock is reasonably accurate (often synced via the GoPro app), this can be compared directly to the sync manifest `wall_time` values.

```python
from datetime import datetime
# GoPro creation_time is typically ISO 8601 UTC
gopro_start = datetime.fromisoformat("2026-02-22T14:30:26+00:00").timestamp()
# Compare to manifest
overhead_start = manifest_events["overhead_recorder_start"]["wall_time"]
offset = gopro_start - overhead_start
```

**Option C — Prompt Timestamp + Estimated Delay**

The manifest records when the "start GoPros" prompt appeared. If you consistently press the GoPro button within a known window (e.g., 2-3 seconds), you can use:
```
gopro_actual_start ≈ gopro_manual_start_prompted.wall_time + estimated_delay
```

This is the least accurate method but requires no additional setup.

#### GoPro Calibration vs. Performance

GoPros record in two separate segments:
1. **Calibration** — from `gopro_*_start_prompted (calibration)` to `gopro_*_stop_prompted (calibration)`
2. **Performance** — from `gopro_*_start_prompted (performance)` to `gopro_*_stop_prompted (performance)`

Each segment produces a separate video file on the GoPro's SD card. The calibration footage typically shows a T-pose and/or checkerboard pattern. The performance footage captures the actual experiment.

---

## 5. Cross-Component Alignment Recipes

### 5.1 HR ↔ Overhead Video

```python
import csv, json

# Load sync manifest
with open("sync_manifest.json") as f:
    manifest = json.load(f)

# Find overhead start time
overhead_start = next(
    e["wall_time"] for e in manifest["events"]
    if e["event"] == "overhead_recorder_start"
)
fps = next(
    e["fps"] for e in manifest["events"]
    if e["event"] == "overhead_recorder_start"
)

# For each HR sample, compute the corresponding video frame
with open("heart_rate/hr_full_session.csv") as f:
    for row in csv.DictReader(f):
        hr_time = float(row["timestamp"])
        video_sec = hr_time - overhead_start
        if video_sec >= 0:
            frame = int(video_sec * fps)
            print(f"HR {row['bpm']} bpm → overhead frame {frame} ({video_sec:.2f}s)")
```

### 5.2 Face Camera ↔ Audio (Same Phase)

These start within milliseconds of each other. Use the manifest to get the precise offset:

```python
events = manifest["events"]
face_start = next(e["wall_time"] for e in events
                  if e["event"] == "face_recorder_start" and e["phase"] == "review")
audio_start = next(e["wall_time"] for e in events
                   if e["event"] == "audio_recorder_start" and e["phase"] == "review")

offset_ms = (audio_start - face_start) * 1000
print(f"Audio starts {offset_ms:.1f}ms after face camera")
```

### 5.3 All Streams on a Common Timeline

```python
# Build a unified timeline converter
class SessionTimeline:
    def __init__(self, manifest_path):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.events = {e["event"]: e for e in self.manifest["events"]}

    def overhead_frame_to_wall(self, frame_num):
        e = next(ev for ev in self.manifest["events"] if ev["event"] == "overhead_recorder_start")
        return e["wall_time"] + frame_num / e["fps"]

    def face_frame_to_wall(self, frame_num, phase):
        e = next(ev for ev in self.manifest["events"]
                 if ev["event"] == "face_recorder_start" and ev["phase"] == phase)
        return e["wall_time"] + frame_num / e["fps"]

    def audio_sample_to_wall(self, sample_num, phase):
        e = next(ev for ev in self.manifest["events"]
                 if ev["event"] == "audio_recorder_start" and ev["phase"] == phase)
        return e["wall_time"] + sample_num / e["sample_rate"]

    def wall_to_overhead_frame(self, wall_time):
        e = next(ev for ev in self.manifest["events"] if ev["event"] == "overhead_recorder_start")
        return int((wall_time - e["wall_time"]) * e["fps"])
```

---

## 6. Timing Precision

| Component | Clock Source | Precision | Notes |
|-----------|-------------|-----------|-------|
| Sync manifest events | `time.time()` | ~1 μs | Python wall-clock |
| HR samples | `time.time()` | ~1 μs | Per-notification timestamp |
| ECG samples | `time.time()` | ~1 μs | Per-batch timestamp (130 Hz batches) |
| Video frame timing | `time.perf_counter()` | ~100 ns | Used for FPS regulation within recorder |
| Audio samples | Hardware callback | ~23 μs at 44.1 kHz | Determined by audio buffer size |
| GoPro video | GoPro internal clock | ~1 s | Must sync via clap or metadata |

**Important:** `time.time()` (wall-clock) and `time.perf_counter()` (monotonic) are two different clocks. The sync manifest and HR data both use `time.time()`, ensuring they share the same time base. Video frame spacing uses `time.perf_counter()` for jitter-free playback, but the **start moment** of each recording is anchored to `time.time()` via the sync manifest.

---

## 7. Known Limitations

1. **GoPro sync is approximate** unless a visual/audio sync event (clap) is used. The manifest only records when the prompt was shown, not when the experimenter actually pressed the button.

2. **USB camera frame drops.** If the system is under load, the video recorder may drop frames. The recorder regulates timing with `time.perf_counter()` sleeps, but a dropped frame means a gap in the video. Frame count × (1/fps) assumes no drops.

3. **Audio callback jitter.** The audio stream runs in a real-time callback. Buffer underruns or system load can cause small timing gaps. In practice this is < 1ms on modern hardware.

4. **Review pause precision.** The `review_timestamps.json` records pause/resume at the moment the GUI processes the user's click, which includes a small variable delay from the event queue polling interval (~33ms).

5. **Clock drift.** Over a long session (> 1 hour), the system wall-clock and hardware clocks may drift relative to each other by tens of milliseconds. For most experimental purposes this is negligible.

---

## 8. Quick-Start Checklist for Post-Processing

1. Open `sync_manifest.json` — this is your master reference
2. Identify the phase you want to analyze
3. Find the relevant `*_start` events for each stream in that phase
4. Use the `wall_time` values as anchors:
   - **HR/ECG:** timestamps in CSV are already wall-clock — compare directly
   - **Video:** `wall_time = start_event.wall_time + frame / fps`
   - **Audio:** `wall_time = start_event.wall_time + sample / sample_rate`
   - **GoPro:** use clap-sync or file metadata to find the offset, then apply
5. To align two streams, subtract their start `wall_time` values to get the offset
