"""Probe webcam capabilities: test resolutions and FPS with MJPG vs default codec."""

import sys
import time
import cv2


def probe_camera(device_index=0):
    resolutions = [
        (2592, 1944),  # Full sensor 4:3
        (2048, 1536),  # 4:3
        (1920, 1080),  # 16:9
        (1280, 960),   # 4:3
        (1280, 720),   # 16:9
        (640, 480),    # 4:3
    ]

    for codec_name, fourcc in [("MJPG", cv2.VideoWriter_fourcc(*"MJPG")), ("default", None)]:
        print(f"\n{'='*60}")
        print(f"  Codec: {codec_name}  (device {device_index})")
        print(f"{'='*60}")

        for w, h in resolutions:
            cap = cv2.VideoCapture(device_index, cv2.CAP_DSHOW)
            if not cap.isOpened():
                print(f"  Cannot open device {device_index}")
                break

            if fourcc is not None:
                cap.set(cv2.CAP_PROP_FOURCC, fourcc)

            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            reported_fps = cap.get(cv2.CAP_PROP_FPS)
            actual_fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
            fourcc_str = "".join(chr((actual_fourcc >> (8 * i)) & 0xFF) for i in range(4))

            # Measure real FPS by capturing frames
            num_frames = 30
            start = time.perf_counter()
            ok_count = 0
            frame_shape = None
            for _ in range(num_frames):
                ret, frame = cap.read()
                if ret and frame is not None:
                    ok_count += 1
                    if frame_shape is None:
                        frame_shape = frame.shape
            elapsed = time.perf_counter() - start
            real_fps = ok_count / elapsed if elapsed > 0 else 0

            cap.release()

            match = "OK" if (actual_w == w and actual_h == h) else "ADJUSTED"
            print(f"  {w}x{h} -> {actual_w}x{actual_h} [{match}]  "
                  f"codec={fourcc_str}  reported_fps={reported_fps:.0f}  "
                  f"real_fps={real_fps:.1f}  frames={ok_count}/{num_frames}")


if __name__ == "__main__":
    devices = [0, 1] if len(sys.argv) < 2 else [int(x) for x in sys.argv[1:]]
    for idx in devices:
        print(f"\n\n*** PROBING CAMERA DEVICE {idx} ***")
        probe_camera(idx)
