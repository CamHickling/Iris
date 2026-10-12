# Contributing to Iris

Thanks for your interest in improving Iris! Bug reports, hardware compatibility notes, and pull requests are all welcome.

## Reporting bugs

Open an issue and include:

- What you were doing (which phase, which devices were connected)
- What you expected vs. what happened
- The console output from the GUI (toggle the console panel) or the terminal
- Your OS, Python version, and device models (camera, GoPro, HR monitor, microphone)

If a session was affected, the `sync_manifest.json` from that session is often the fastest way to diagnose timing problems. Remove any participant-identifying information before attaching it.

## Development setup

```bash
git clone https://github.com/CamHickling/Iris.git
cd Iris
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements-dev.txt
copy settings.example.json settings.json
```

## Running tests

```bash
python -m pytest
```

The suite mocks hardware libraries (`bleak`, `goprocam`, `sounddevice`, …) in `tests/conftest.py`, so it runs without any devices attached.

## Pull requests

1. Fork the repo and create a branch from `main`.
2. Keep changes focused — one fix or feature per PR.
3. Add or update tests in `tests/` for behaviour you change.
4. Match the style of the surrounding code.
5. Update the README or `docs/` if you change configuration, outputs, or the phase workflow.
6. Make sure `python -m pytest` passes before opening the PR.

## Adding a new device

Device modules live in `src/` and follow a common pattern: a `*Config` dataclass built from `settings.json`, a device class with `open`/`start`/`stop` methods, and an optional `*Manager` for multiple instances. Log start/stop events to the sync manifest so the new stream can be aligned in post-processing (see [docs/SYNCHRONIZATION.md](docs/SYNCHRONIZATION.md)).
