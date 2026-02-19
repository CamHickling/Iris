import cv2
import numpy as np
import os

# --- Checkerboard Configuration ---
BOARD_INNER_COLS = 10  # inner corners horizontally
BOARD_INNER_ROWS = 7   # inner corners vertically
SQUARE_SIZE_MM = 25.0  # side length of each square in mm

# How many frames to sample for calibration from each end of the video
CALIBRATION_SAMPLE_FRAMES = 60
# Fraction of video at each end to search for checkerboard (e.g. 0.15 = first/last 15%)
CALIBRATION_END_FRACTION = 0.15


def calibrate_from_video(input_file):
    """First pass: detect checkerboard corners and calibrate the camera."""
    cap = cv2.VideoCapture(input_file)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    board_size = (BOARD_INNER_COLS, BOARD_INNER_ROWS)

    # Prepare the object points grid: (0,0,0), (25,0,0), (50,0,0), ...
    objp = np.zeros((BOARD_INNER_COLS * BOARD_INNER_ROWS, 3), np.float32)
    objp[:, :2] = np.mgrid[0:BOARD_INNER_COLS, 0:BOARD_INNER_ROWS].T.reshape(-1, 2)
    objp *= SQUARE_SIZE_MM

    obj_points = []  # 3D points in real-world space
    img_points = []  # 2D points in image plane

    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
    min_detections = 10

    def build_end_samples(total, fraction, num_samples):
        """Build sample frame indices from first/last `fraction` of the video."""
        end_frames = int(total * fraction)
        half = max(1, num_samples // 2)
        interval = max(1, end_frames // half)
        end_start = total - end_frames
        frames = set()
        for i in range(0, end_frames, interval):
            frames.add(i)
        for i in range(end_start, total, interval):
            frames.add(i)
        return frames, end_frames, end_start

    def build_full_samples(total, num_samples):
        """Build sample frame indices evenly spaced across the full video."""
        interval = max(1, total // num_samples)
        return set(range(0, total, interval))

    def detect_in_frames(cap, sample_frames, label):
        """Seek through video and detect checkerboard in the given frame set."""
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        local_obj = []
        local_img = []
        frame_idx = 0
        max_frame = max(sample_frames)
        while frame_idx <= max_frame:
            ret, frame = cap.read()
            if not ret:
                break
            if frame_idx in sample_frames:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                found, corners = cv2.findChessboardCorners(
                    gray, board_size,
                    cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE + cv2.CALIB_CB_FAST_CHECK
                )
                if found:
                    corners_refined = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
                    local_obj.append(objp)
                    local_img.append(corners_refined)
                    print(f"  [{label}] Frame {frame_idx}: checkerboard detected ({len(local_obj)} total)")
            frame_idx += 1
        return local_obj, local_img

    # --- Progressive search: keep increasing density until we have enough ---
    checked_frames = set()

    # Pass 1: first/last portion of the video
    end_sample_frames, end_frames, end_start = build_end_samples(
        total_frames, CALIBRATION_END_FRACTION, CALIBRATION_SAMPLE_FRAMES
    )

    print(f"Calibrating from {input_file}")
    print(f"  Resolution: {w}x{h}, Total frames: {total_frames}")
    print(f"  Pass 1: Sampling {len(end_sample_frames)} frames from first/last "
          f"{CALIBRATION_END_FRACTION*100:.0f}% (frames 0-{end_frames} and {end_start}-{total_frames})...")

    obj_points, img_points = detect_in_frames(cap, end_sample_frames, "ends")
    checked_frames |= end_sample_frames

    # Pass 2+: progressively scan more of the video with increasing density
    # Each round doubles the number of sample frames across the full video
    pass_num = 2
    num_samples = CALIBRATION_SAMPLE_FRAMES
    while len(obj_points) < min_detections:
        num_samples *= 2
        if num_samples > total_frames:
            num_samples = total_frames

        new_frames = build_full_samples(total_frames, num_samples) - checked_frames
        if not new_frames:
            # We've checked every frame in the video, nothing left to try
            break

        print(f"\n  Only {len(obj_points)} detections so far — need {min_detections}.")
        print(f"  Pass {pass_num}: Sampling {len(new_frames)} additional frames "
              f"(~{num_samples} evenly spaced)...")

        extra_obj, extra_img = detect_in_frames(cap, new_frames, f"pass{pass_num}")
        obj_points.extend(extra_obj)
        img_points.extend(extra_img)
        checked_frames |= new_frames
        pass_num += 1

    cap.release()

    if len(obj_points) < 5:
        print(f"\nError: Only found the checkerboard in {len(obj_points)} frame(s) "
              f"after checking {len(checked_frames)}/{total_frames} frames. "
              f"Need at least 5 for a reliable calibration.")
        print("Make sure the full checkerboard is visible and well-lit in the video.")
        return None

    # Fix tangential distortion to zero (manufactured lenses don't need it).
    # Note: CALIB_RATIONAL_MODEL (k4-k6) is intentionally NOT used — with
    # limited calibration data its denominator can approach zero at certain
    # radii, producing a visible ring artifact in the corrected image.
    calib_flags = (
        cv2.CALIB_FIX_TANGENT_DIST
    )

    print(f"\nRunning initial calibration with {len(obj_points)} detections...")
    ret, K, D, rvecs, tvecs = cv2.calibrateCamera(
        obj_points, img_points, (w, h), None, None, flags=calib_flags
    )
    print(f"  Initial RMS reprojection error: {ret:.4f}")

    # --- Outlier filtering: remove images with high reprojection error ---
    per_image_errors = []
    for i in range(len(obj_points)):
        projected, _ = cv2.projectPoints(obj_points[i], rvecs[i], tvecs[i], K, D)
        err = cv2.norm(img_points[i], projected, cv2.NORM_L2) / len(projected)
        per_image_errors.append(err)

    errors = np.array(per_image_errors)
    median_err = np.median(errors)
    mad = np.median(np.abs(errors - median_err))
    threshold = median_err + 3.0 * max(mad, 0.1)

    kept_obj = []
    kept_img = []
    removed = 0
    for i, err in enumerate(per_image_errors):
        if err <= threshold:
            kept_obj.append(obj_points[i])
            kept_img.append(img_points[i])
        else:
            removed += 1
            print(f"  Removing detection {i} (error {err:.3f} > threshold {threshold:.3f})")

    if removed > 0 and len(kept_obj) >= 5:
        print(f"  Removed {removed} outlier(s), re-calibrating with {len(kept_obj)} detections...")
        ret, K, D, rvecs, tvecs = cv2.calibrateCamera(
            kept_obj, kept_img, (w, h), None, None, flags=calib_flags
        )
    elif len(kept_obj) < 5:
        print(f"  Warning: too few detections after filtering ({len(kept_obj)}), keeping all.")

    print(f"  Final RMS reprojection error: {ret:.4f}")
    print(f"  Camera matrix K:\n{K}")
    print(f"  Distortion coefficients D: {D.ravel()}")

    return K, D, (w, h)


def correct_video(input_file, K, D, frame_size):
    """Second pass: undistort every frame and write the corrected video."""
    w, h = frame_size
    cap = cv2.VideoCapture(input_file)
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # alpha=0: crop to valid pixels only (no black borders, no stretching)
    # alpha=1: keep all pixels (causes stretching at edges)
    # alpha=0.3: compromise — slight crop, mostly clean
    alpha = 0
    new_K, roi = cv2.getOptimalNewCameraMatrix(K, D, (w, h), alpha, (w, h))
    x, y, w_roi, h_roi = roi

    # Pre-compute the undistortion maps for speed
    map1, map2 = cv2.initUndistortRectifyMap(K, D, None, new_K, (w, h), cv2.CV_16SC2)

    dir_name = os.path.dirname(input_file)
    base_name = os.path.basename(input_file)
    output_file = os.path.join(dir_name, "corrected_" + base_name)
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_file, fourcc, fps, (w, h))

    print(f"\nCorrecting video -> {output_file}")
    print(f"  Crop ROI: x={x}, y={y}, w={w_roi}, h={h_roi} "
          f"({w_roi*100/w:.0f}% x {h_roi*100/h:.0f}% of original)")

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        dst = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)
        # Crop to valid region then resize back to original dimensions
        dst = dst[y:y+h_roi, x:x+w_roi]
        dst = cv2.resize(dst, (w, h))
        out.write(dst)

        frame_idx += 1
        if frame_idx % 100 == 0:
            print(f"  {frame_idx}/{total_frames} frames processed")

    cap.release()
    out.release()
    print(f"  Done. {frame_idx} frames written.")
    return output_file


def create_comparison_video(input_file, K, D, frame_size):
    """Third pass: side-by-side original vs corrected with spatial alignment."""
    w, h = frame_size
    cap = cv2.VideoCapture(input_file)
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # Use the ORIGINAL K as the new camera matrix so that pixel positions
    # correspond to the same spatial locations as the original frame.
    # This keeps the center aligned and shows black borders only where
    # the barrel distortion had pulled in extra field of view.
    map1, map2 = cv2.initUndistortRectifyMap(K, D, None, K, (w, h), cv2.CV_16SC2)

    dir_name = os.path.dirname(input_file)
    base_name = os.path.basename(input_file)
    output_file = os.path.join(dir_name, "comparison_" + base_name)
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_file, fourcc, fps, (w * 2, h))

    print(f"\nCreating comparison video -> {output_file}")

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        corrected = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)

        # Add labels
        cv2.putText(frame, "Original", (20, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 255, 0), 3)
        cv2.putText(corrected, "Corrected", (20, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 255), 3)

        side_by_side = np.hstack((frame, corrected))
        out.write(side_by_side)

        frame_idx += 1
        if frame_idx % 100 == 0:
            print(f"  {frame_idx}/{total_frames} frames processed")

    cap.release()
    out.release()
    print(f"  Done. {frame_idx} frames written.")
    return output_file


def print_fov_analysis(K, D, frame_size):
    """Compute and print field-of-view before and after correction."""
    w, h = frame_size
    fx, fy = K[0, 0], K[1, 1]

    # --- Original (distorted) FOV ---
    # Undistort edge-center and corner points to find the true ray angles.
    # cv2.undistortPoints with no P returns normalized camera coords (x/z, y/z),
    # so the angle from the optical axis = atan(coord).
    edge_pts = np.array([
        [[0.0, h / 2.0]],          # left edge center
        [[float(w - 1), h / 2.0]], # right edge center
        [[w / 2.0, 0.0]],          # top edge center
        [[w / 2.0, float(h - 1)]], # bottom edge center
    ], dtype=np.float32)

    corner_pts = np.array([
        [[0.0, 0.0]],                       # top-left
        [[float(w - 1), float(h - 1)]],     # bottom-right
    ], dtype=np.float32)

    edges_norm = cv2.undistortPoints(edge_pts, K, D)
    corners_norm = cv2.undistortPoints(corner_pts, K, D)

    orig_fov_h = np.degrees(
        np.arctan(edges_norm[1, 0, 0]) - np.arctan(edges_norm[0, 0, 0])
    )
    orig_fov_v = np.degrees(
        np.arctan(edges_norm[3, 0, 1]) - np.arctan(edges_norm[2, 0, 1])
    )

    # Diagonal: angle between top-left and bottom-right ray vectors
    v_tl = np.array([corners_norm[0, 0, 0], corners_norm[0, 0, 1], 1.0])
    v_br = np.array([corners_norm[1, 0, 0], corners_norm[1, 0, 1], 1.0])
    cos_diag = np.dot(v_tl, v_br) / (np.linalg.norm(v_tl) * np.linalg.norm(v_br))
    orig_fov_d = np.degrees(np.arccos(np.clip(cos_diag, -1, 1)))

    # --- Corrected (cropped) FOV ---
    alpha = 0
    new_K, roi = cv2.getOptimalNewCameraMatrix(K, D, (w, h), alpha, (w, h))
    x, y, w_roi, h_roi = roi
    nfx, nfy = new_K[0, 0], new_K[1, 1]
    ncx, ncy = new_K[0, 2], new_K[1, 2]

    # After undistortion the image is rectilinear, so FOV is simple geometry
    crop_fov_h = np.degrees(
        np.arctan((x + w_roi - ncx) / nfx) - np.arctan((x - ncx) / nfx)
    )
    crop_fov_v = np.degrees(
        np.arctan((y + h_roi - ncy) / nfy) - np.arctan((y - ncy) / nfy)
    )

    v_crop_tl = np.array([(x - ncx) / nfx, (y - ncy) / nfy, 1.0])
    v_crop_br = np.array([(x + w_roi - ncx) / nfx, (y + h_roi - ncy) / nfy, 1.0])
    cos_diag_c = np.dot(v_crop_tl, v_crop_br) / (np.linalg.norm(v_crop_tl) * np.linalg.norm(v_crop_br))
    crop_fov_d = np.degrees(np.arccos(np.clip(cos_diag_c, -1, 1)))

    # --- Print results ---
    print("\n========== Field of View Analysis ==========")
    print(f"\nCalibrated focal length: fx={fx:.1f}px  fy={fy:.1f}px")
    print(f"Corrected focal length:  fx={nfx:.1f}px  fy={nfy:.1f}px")

    print(f"\nOriginal (distorted) FOV:")
    print(f"  Horizontal: {orig_fov_h:.1f}\u00b0")
    print(f"  Vertical:   {orig_fov_v:.1f}\u00b0")
    print(f"  Diagonal:   {orig_fov_d:.1f}\u00b0")

    print(f"\nCorrected (cropped) FOV:")
    print(f"  Horizontal: {crop_fov_h:.1f}\u00b0")
    print(f"  Vertical:   {crop_fov_v:.1f}\u00b0")
    print(f"  Diagonal:   {crop_fov_d:.1f}\u00b0")

    print(f"\nFOV reduction:")
    print(f"  Horizontal: {orig_fov_h - crop_fov_h:.1f}\u00b0 lost  ({(1 - crop_fov_h / orig_fov_h) * 100:.1f}%)")
    print(f"  Vertical:   {orig_fov_v - crop_fov_v:.1f}\u00b0 lost  ({(1 - crop_fov_v / orig_fov_v) * 100:.1f}%)")
    print(f"  Diagonal:   {orig_fov_d - crop_fov_d:.1f}\u00b0 lost  ({(1 - crop_fov_d / orig_fov_d) * 100:.1f}%)")

    print(f"\nCrop region:")
    print(f"  Original resolution: {w} x {h}")
    print(f"  Valid ROI:           {w_roi} x {h_roi}  (offset {x}, {y})")
    print(f"  Pixels kept:         {w_roi * 100 / w:.1f}% x {h_roi * 100 / h:.1f}%")
    print(f"  Area kept:           {w_roi * h_roi * 100 / (w * h):.1f}%")

    print(f"\nDistortion coefficients [k1, k2, p1, p2, k3]:")
    print(f"  {D.ravel()}")
    print("=============================================")


def process_videos(input_files):
    # --- Phase 1: Calibrate all videos first ---
    print("=" * 60)
    print("PHASE 1: Calibrating all videos")
    print("=" * 60)

    calibrations = {}
    for input_file in input_files:
        print()
        if not os.path.exists(input_file):
            print(f"Error: {input_file} not found — skipping.")
            continue

        result = calibrate_from_video(input_file)
        if result is None:
            print(f"FAILED: {input_file} — not enough checkerboard detections.")
        else:
            calibrations[input_file] = result

    # --- Check all passed before doing any correction ---
    failed = [f for f in input_files if f not in calibrations and os.path.exists(f)]
    if failed:
        print("\n" + "=" * 60)
        print("ABORTING: The following videos failed calibration:")
        for f in failed:
            print(f"  - {f}")
        print("Fix these videos and re-run. No corrections were applied.")
        print("=" * 60)
        return

    if not calibrations:
        print("\nNo videos to process.")
        return

    # --- Phase 2: Apply corrections ---
    print("\n" + "=" * 60)
    print("PHASE 2: Applying corrections to all videos")
    print("=" * 60)

    for input_file, (K, D, frame_size) in calibrations.items():
        print()
        print_fov_analysis(K, D, frame_size)
        output_file = correct_video(input_file, K, D, frame_size)
        comparison_file = create_comparison_video(input_file, K, D, frame_size)
        print(f"\nProcessing complete:")
        print(f"  Corrected: {output_file}")
        print(f"  Comparison: {comparison_file}")


# Usage
process_videos([
    '/Users/camhickling/Desktop/FrontCamera.mov',
    '/Users/camhickling/Desktop/BackCamera.mov',
])
