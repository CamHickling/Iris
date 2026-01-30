#!/usr/bin/env python3
"""CaptureExpert - Multi-camera experiment capture tool."""

import argparse
import json
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
    args = parser.parse_args()

    settings = load_settings(args.config)
    print(f"Loaded settings: {settings['experiment']['name']}")

    experiment = Experiment(settings)
    experiment.run()


if __name__ == "__main__":
    main()
