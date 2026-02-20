"""Multi-camera extrinsic calibration using shared checkerboard views.

Reuses checkerboard parameters from lens_correct.py (10x7, 25mm squares).
"""

import json
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .lens_correct import (
    BOARD_INNER_COLS,
    BOARD_INNER_ROWS,
    SQUARE_SIZE_MM,
    calibrate_from_video,
)


def calibrate_intrinsic(video_path: str) -> Optional[tuple]:
    """Run intrinsic calibration on a single video.

    Returns (K, D, frame_size) or None on failure.
    Delegates to lens_correct.calibrate_from_video().
    """
    print(f"\n--- Intrinsic calibration: {video_path} ---")
    return calibrate_from_video(video_path)


def _find_shared_checkerboard_frames(
    video_paths: list[str],
    max_frames: int = 200,
) -> dict[int, dict[str, np.ndarray]]:
    """Find frames where the checkerboard is visible in all videos simultaneously.

    Returns a dict: {frame_index: {video_path: corners_array, ...}}
    Only frames where ALL videos have a detection are included.
    """
    board_size = (BOARD_INNER_COLS, BOARD_INNER_ROWS)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)

    caps = {}
    total_frames = float("inf")
    for path in video_paths:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            print(f"Warning: Cannot open {path}")
            continue
        caps[path] = cap
        total_frames = min(total_frames, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))

    if len(caps) < 2:
        print("Need at least 2 video files for extrinsic calibration")
        for cap in caps.values():
            cap.release()
        return {}

    total_frames = int(total_frames)
    sample_interval = max(1, total_frames // max_frames)
    sample_indices = set(range(0, total_frames, sample_interval))

    # Detect checkerboard per video per frame
    detections: dict[str, dict[int, np.ndarray]] = {p: {} for p in caps}

    for path, cap in caps.items():
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        frame_idx = 0
        max_frame = max(sample_indices)
        while frame_idx <= max_frame:
            ret, frame = cap.read()
            if not ret:
                break
            if frame_idx in sample_indices:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                found, corners = cv2.findChessboardCorners(
                    gray, board_size,
                    cv2.CALIB_CB_ADAPTIVE_THRESH
                    + cv2.CALIB_CB_NORMALIZE_IMAGE
                    + cv2.CALIB_CB_FAST_CHECK,
                )
                if found:
                    corners_refined = cv2.cornerSubPix(
                        gray, corners, (11, 11), (-1, -1), criteria
                    )
                    detections[path][frame_idx] = corners_refined
            frame_idx += 1

    for cap in caps.values():
        cap.release()

    # Find frames where ALL videos have a detection
    shared = {}
    all_paths = list(caps.keys())
    if not all_paths:
        return {}

    common_frames = set(detections[all_paths[0]].keys())
    for path in all_paths[1:]:
        common_frames &= set(detections[path].keys())

    for fidx in sorted(common_frames):
        shared[fidx] = {path: detections[path][fidx] for path in all_paths}

    print(f"Found {len(shared)} shared checkerboard frames across {len(all_paths)} cameras")
    return shared


def calibrate_extrinsic_pair(
    K_a: np.ndarray, D_a: np.ndarray,
    K_b: np.ndarray, D_b: np.ndarray,
    img_points_a: list[np.ndarray],
    img_points_b: list[np.ndarray],
    frame_size_a: tuple[int, int],
    frame_size_b: tuple[int, int],
) -> Optional[dict]:
    """Compute extrinsic calibration (R, T) between two cameras.

    Uses cv2.stereoCalibrate with CALIB_FIX_INTRINSIC so K/D are fixed.
    """
    board_size = (BOARD_INNER_COLS, BOARD_INNER_ROWS)
    objp = np.zeros((BOARD_INNER_COLS * BOARD_INNER_ROWS, 3), np.float32)
    objp[:, :2] = np.mgrid[0:BOARD_INNER_COLS, 0:BOARD_INNER_ROWS].T.reshape(-1, 2)
    objp *= SQUARE_SIZE_MM

    obj_points = [objp for _ in img_points_a]

    try:
        ret, _, _, _, _, R, T, E, F = cv2.stereoCalibrate(
            obj_points,
            img_points_a,
            img_points_b,
            K_a, D_a,
            K_b, D_b,
            frame_size_a,
            flags=cv2.CALIB_FIX_INTRINSIC,
        )
        print(f"  Stereo RMS error: {ret:.4f}")
        return {"R": R, "T": T, "E": E, "F": F, "rms": ret}
    except cv2.error as e:
        print(f"  Stereo calibration failed: {e}")
        return None


def calibrate_all(
    video_paths: list[str],
    reference_camera: int = 0,
) -> dict:
    """Full calibration pipeline: intrinsic per camera + extrinsic pairs.

    Args:
        video_paths: List of video file paths (one per camera).
        reference_camera: Index of the reference camera (default: first).

    Returns:
        Dict with per-camera calibrations and pairwise extrinsics.
    """
    print(f"\n{'=' * 60}")
    print("  MULTI-CAMERA CALIBRATION")
    print(f"{'=' * 60}")

    # Step 1: Intrinsic calibration per camera
    intrinsics = {}
    for i, path in enumerate(video_paths):
        result = calibrate_intrinsic(path)
        if result is None:
            print(f"FAILED: Intrinsic calibration for {path}")
            continue
        K, D, frame_size = result
        intrinsics[path] = {"K": K, "D": D, "frame_size": frame_size, "index": i}

    if len(intrinsics) < 2:
        print("Need at least 2 successful intrinsic calibrations for extrinsic")
        return {"intrinsics": intrinsics, "extrinsics": {}}

    # Step 2: Find shared checkerboard frames
    calibrated_paths = list(intrinsics.keys())
    shared_frames = _find_shared_checkerboard_frames(calibrated_paths)

    if len(shared_frames) < 5:
        print(f"Only {len(shared_frames)} shared frames found, need at least 5")
        return {"intrinsics": intrinsics, "extrinsics": {}}

    # Step 3: Compute extrinsic pairs relative to reference
    ref_path = video_paths[reference_camera]
    if ref_path not in intrinsics:
        ref_path = calibrated_paths[0]
        print(f"Reference camera not calibrated, using {ref_path}")

    extrinsics = {}
    ref_cal = intrinsics[ref_path]

    for path in calibrated_paths:
        if path == ref_path:
            continue

        cam_cal = intrinsics[path]

        # Collect shared image points
        img_pts_ref = []
        img_pts_cam = []
        for fidx in sorted(shared_frames.keys()):
            frame_data = shared_frames[fidx]
            if ref_path in frame_data and path in frame_data:
                img_pts_ref.append(frame_data[ref_path])
                img_pts_cam.append(frame_data[path])

        if len(img_pts_ref) < 5:
            print(f"  Not enough shared frames between {ref_path} and {path}")
            continue

        print(f"\n--- Extrinsic: {Path(ref_path).name} <-> {Path(path).name} ---")
        print(f"  Using {len(img_pts_ref)} shared frames")

        result = calibrate_extrinsic_pair(
            ref_cal["K"], ref_cal["D"],
            cam_cal["K"], cam_cal["D"],
            img_pts_ref, img_pts_cam,
            ref_cal["frame_size"], cam_cal["frame_size"],
        )
        if result:
            extrinsics[path] = result

    return {"intrinsics": intrinsics, "extrinsics": extrinsics, "reference": ref_path}


def save_calibration_log(calibrations: dict, output_path: str):
    """Save calibration results to JSON."""

    def ndarray_to_list(obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, dict):
            return {k: ndarray_to_list(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [ndarray_to_list(v) for v in obj]
        return obj

    serializable = ndarray_to_list(calibrations)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(serializable, f, indent=2)
    print(f"Calibration log saved to {output_path}")
