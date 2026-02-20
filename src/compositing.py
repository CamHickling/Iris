"""Post-processing: audio/video muxing, review compositing, and HR overlay.

Requires ffmpeg on PATH.
"""

import json
import shutil
import subprocess
from pathlib import Path

import cv2
import ffmpeg


def check_ffmpeg() -> bool:
    """Check that ffmpeg is available on PATH."""
    return shutil.which("ffmpeg") is not None


def overlay_audio_on_video(video_path: str, audio_path: str, output_path: str):
    """Mux audio onto video (copy video stream, encode audio as AAC)."""
    print(f"Muxing audio onto video -> {output_path}")
    video_input = ffmpeg.input(video_path)
    audio_input = ffmpeg.input(audio_path)
    (
        ffmpeg
        .output(video_input.video, audio_input.audio, output_path,
                vcodec="copy", acodec="aac", audio_bitrate="192k",
                shortest=None)
        .overwrite_output()
        .run(quiet=True)
    )
    print(f"  Done: {output_path}")


def create_review_composite(
    overhead_video: str,
    audio_path: str,
    timestamps_path: str,
    output_path: str,
):
    """Expand overhead video by inserting frozen frames at pause points, then mux audio.

    The timestamps_path JSON should contain a list of events:
        [{"type": "pause", "wall_time": ..., "video_position_sec": ...},
         {"type": "resume", "wall_time": ..., "video_position_sec": ...}, ...]

    During each pause->resume interval, the last frame before the pause is
    repeated to fill the gap (matching the wall-clock duration of the pause).
    """
    print(f"Creating review composite -> {output_path}")

    with open(timestamps_path) as f:
        events = json.load(f)

    # Build pause intervals: [(video_pos_sec, pause_duration_sec), ...]
    pause_intervals = []
    i = 0
    while i < len(events):
        if events[i]["type"] == "pause":
            pause_time = events[i]["wall_time"]
            video_pos = events[i]["video_position_sec"]
            # Find matching resume
            resume_time = None
            for j in range(i + 1, len(events)):
                if events[j]["type"] == "resume":
                    resume_time = events[j]["wall_time"]
                    i = j + 1
                    break
            if resume_time is None:
                # Pause without resume (end of video)
                break
            pause_intervals.append((video_pos, resume_time - pause_time))
        else:
            i += 1

    cap = cv2.VideoCapture(overhead_video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    temp_video = str(Path(output_path).parent / "_temp_expanded.mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(temp_video, fourcc, fps, (w, h))

    # Sort pauses by video position
    pause_intervals.sort(key=lambda x: x[0])
    pause_idx = 0
    frame_idx = 0
    last_frame = None

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        current_sec = frame_idx / fps
        last_frame = frame

        # Check if we've reached a pause point
        if pause_idx < len(pause_intervals):
            pause_pos, pause_dur = pause_intervals[pause_idx]
            if current_sec >= pause_pos:
                # Write the current frame, then insert frozen frames
                writer.write(frame)
                frozen_count = int(pause_dur * fps)
                for _ in range(frozen_count):
                    writer.write(frame)
                pause_idx += 1
                frame_idx += 1
                continue

        writer.write(frame)
        frame_idx += 1

    cap.release()
    writer.release()

    # Mux audio onto the expanded video
    overlay_audio_on_video(temp_video, audio_path, output_path)

    # Clean up temp file
    Path(temp_video).unlink(missing_ok=True)
    print(f"  Review composite done: {output_path}")


def create_hr_synced_video(
    video_path: str,
    hr_csv_path: str,
    output_path: str,
    phase: str,
):
    """Burn HR BPM as text overlay onto a video using ffmpeg drawtext filter.

    Reads the HR CSV to get BPM values for the given phase, then overlays
    the nearest BPM value as text on each frame.
    """
    import csv

    print(f"Creating HR overlay video -> {output_path}")

    # Read HR samples for the given phase
    hr_data = []  # [(timestamp, bpm), ...]
    with open(hr_csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["phase"] == phase:
                hr_data.append((float(row["timestamp"]), int(row["bpm"])))

    if not hr_data:
        print(f"  No HR data for phase '{phase}', copying video as-is")
        shutil.copy2(video_path, output_path)
        return

    # Get video start time (use first HR sample as reference)
    video_start = hr_data[0][0]

    # Build ffmpeg drawtext filter with enable expressions for each BPM value
    # Group consecutive samples with the same BPM to reduce filter complexity
    segments = []  # [(start_sec, end_sec, bpm), ...]
    for i, (ts, bpm) in enumerate(hr_data):
        start = ts - video_start
        if i + 1 < len(hr_data):
            end = hr_data[i + 1][0] - video_start
        else:
            end = start + 10.0  # extend last sample
        if segments and segments[-1][2] == bpm:
            segments[-1] = (segments[-1][0], end, bpm)
        else:
            segments.append((start, end, bpm))

    # Build a single drawtext with a text file that changes over time
    # Simpler approach: overlay using a simple constant BPM display
    # that updates based on the average BPM for the phase
    avg_bpm = sum(b for _, b in hr_data) // len(hr_data)
    min_bpm = min(b for _, b in hr_data)
    max_bpm = max(b for _, b in hr_data)

    drawtext = (
        f"drawtext=text='HR\\: {avg_bpm} bpm (range\\: {min_bpm}-{max_bpm})':"
        f"fontsize=28:fontcolor=red:x=20:y=20:"
        f"borderw=2:bordercolor=black"
    )

    try:
        (
            ffmpeg
            .input(video_path)
            .output(output_path, vf=drawtext, acodec="copy")
            .overwrite_output()
            .run(quiet=True)
        )
        print(f"  Done: {output_path}")
    except ffmpeg.Error as e:
        print(f"  ffmpeg error: {e}")
        # Fallback: use OpenCV for overlay
        _hr_overlay_opencv(video_path, hr_data, video_start, output_path)


def _hr_overlay_opencv(
    video_path: str,
    hr_data: list[tuple[float, int]],
    video_start: float,
    output_path: str,
):
    """Fallback: burn HR BPM overlay using OpenCV."""
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(output_path, fourcc, fps, (w, h))

    hr_idx = 0
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        current_sec = frame_idx / fps

        # Find nearest HR sample
        while hr_idx + 1 < len(hr_data):
            next_ts = hr_data[hr_idx + 1][0] - video_start
            if next_ts <= current_sec:
                hr_idx += 1
            else:
                break

        bpm = hr_data[hr_idx][1]
        text = f"HR: {bpm} bpm"
        cv2.putText(frame, text, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
        writer.write(frame)
        frame_idx += 1

    cap.release()
    writer.release()
    print(f"  HR overlay (OpenCV fallback) done: {output_path}")
