import os

import cv2
import numpy as np


def undistort_video(input_path, output_path):
    cap = cv2.VideoCapture(input_path)

    # Get video properties
    width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps    = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # --- GOPRO HERO 7 SILVER CALIBRATION DATA ---
    # Note: These are estimated for 4K/Wide. For perfect MoCap,
    # replace these with values from a checkerboard test.

    # Camera Matrix (K)
    # [fx, 0, cx], [0, fy, cy], [0, 0, 1]
    K = np.array([[width * 0.5, 0, width / 2],
                  [0, width * 0.5, height / 2],
                  [0, 0, 1]])

    # Distortion Coefficients (D)
    # [k1, k2, p1, p2, k3]
    D = np.array([-0.25, 0.08, 0, 0, -0.02])

    # Refine the camera matrix for the output
    new_camera_matrix, roi = cv2.getOptimalNewCameraMatrix(K, D, (width, height), 1, (width, height))

    # Setup Video Writer
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    print(f"Processing {input_path} ({total_frames} frames)...")

    frame_num = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        frame_num += 1

        # Apply undistortion
        undistorted_frame = cv2.undistort(frame, K, D, None, new_camera_matrix)

        # Optional: Crop the black edges (ROI)
        # x, y, w, h = roi
        # undistorted_frame = undistorted_frame[y:y+h, x:x+w]
        # (If you crop, you must resize or update 'out' dimensions)

        out.write(undistorted_frame)

        if frame_num % 100 == 0 or frame_num == total_frames:
            print(f"  Processing frame {frame_num}/{total_frames}...")

    cap.release()
    out.release()
    print(f"Done! Saved to: {output_path}")


def process_directory(input_dir, output_dir=None):
    if output_dir is None:
        output_dir = input_dir

    os.makedirs(output_dir, exist_ok=True)

    mp4_files = [
        f for f in os.listdir(input_dir)
        if f.lower().endswith('.mp4')
    ]

    if not mp4_files:
        print(f"No .mp4 files found in {input_dir}")
        return

    print(f"Found {len(mp4_files)} video(s) to process.")

    for filename in mp4_files:
        input_path = os.path.join(input_dir, filename)
        name, ext = os.path.splitext(filename)
        output_path = os.path.join(output_dir, f"{name}_undistorted{ext}")
        undistort_video(input_path, output_path)

    print(f"All {len(mp4_files)} video(s) processed.")


if __name__ == "__main__":
    undistort_video('input_gopro_video.mp4', 'output_rectilinear.mp4')
