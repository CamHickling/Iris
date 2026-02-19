#!/usr/bin/env python3
"""CaptureExpert - Multi-camera experiment capture tool."""

import argparse
import json
import sys
from pathlib import Path

from src.experiment import Experiment


def load_settings(config_path: str) -> dict:
    with open(config_path, "r") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="CaptureExpert")
    parser.add_argument(
        "--config",
        default="settings.json",
        help="Path to configuration file",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Run device calibration check instead of experiment",
    )
    parser.add_argument(
        "--undistort",
        metavar="PATH",
        help="Run lens correction on GoPro videos in PATH (directory or single file)",
    )
    args = parser.parse_args()

    if args.undistort:
        from src.len_correction import undistort_video, process_directory

        target = Path(args.undistort)
        if target.is_dir():
            process_directory(str(target))
        elif target.is_file():
            name = target.stem
            output = target.parent / f"{name}_undistorted{target.suffix}"
            undistort_video(str(target), str(output))
        else:
            print(f"Error: {args.undistort} is not a valid file or directory.")
            sys.exit(1)
        sys.exit(0)

    settings = load_settings(args.config)

    if args.calibrate:
        from src.calibrate import CalibrationTool

        tool = CalibrationTool(settings)
        sys.exit(0 if tool.run() else 1)

    print(f"Loaded settings: {settings['experiment']['name']}")

    experiment = Experiment(settings)
    experiment.run()


if __name__ == "__main__":
    main()
