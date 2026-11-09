"""Demo mode: simulated devices for walkthroughs and screen recordings.

Enabled only by ``python gui.py --demo``. Nothing in this module touches real
hardware:

- USB cameras play a test clip from ``demo_output/clips/<role>.mp4`` if one
  exists, otherwise a synthetic pattern (a stick figure for the overhead
  camera, a silhouette for the face camera).
- The Polar H10 produces plausible, phase-dependent heart rate and RR data.
- GoPros and the microphone report connected. The "microphone" writes faint
  generated room tone; no audio device is ever opened.

Settings come from ``demo_output/demo_settings.json`` (regenerated on every
launch), so ``settings.json`` is never read or written. Sessions are written
to ``demo_output/sessions/`` and backup is turned off.

The demo also runs a small control server on localhost so ``video/record.py``
can drive the GUI for screen recordings.
"""

import json
import math
import queue
import random
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .audio import AudioRecorder
from .calibrate import CalibrationTool
from .camera import Camera, CameraManager
from .heart_rate import HeartRateSample, PolarH10

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_ROOT = REPO_ROOT / "demo_output"
SESSIONS_DIR = DEMO_ROOT / "sessions"
CLIPS_DIR = DEMO_ROOT / "clips"
SETTINGS_PATH = DEMO_ROOT / "demo_settings.json"

DEFAULT_PORT = 47900
FRAME_SIZE = (1280, 720)

# Target heart rate per phase; the simulated signal drifts towards these
_HR_TARGETS = {
    "setup": 72,
    "heart_rate_start": 74,
    "warmup_calibration": 96,
    "performance": 158,
    "review": 104,
    "scoring": 92,
    "finish": 84,
}


# =============================================================================
#  Settings
# =============================================================================


def build_settings() -> dict:
    """Demo settings: every device enabled, output kept inside demo_output/."""
    with open(REPO_ROOT / "settings.example.json") as f:
        settings = json.load(f)

    settings["experiment"].update({
        "name": "Taekwondo Demo",
        "output_dir": str(SESSIONS_DIR),
        "backup_dir": None,
    })
    for cam in settings["cameras"]:
        cam["resolution"] = list(FRAME_SIZE)
        cam["enabled"] = True
    for gp in settings["gopros"]:
        gp["enabled"] = True
    settings["heart_rate"].update({"enabled": True, "device_address": "DEMO", "ecg_enabled": False})
    settings["microphone"].update({"enabled": True, "device_name": "Demo Mic", "device_index": 0})
    return settings


def prepare() -> Path:
    """Write fresh demo settings and return their path."""
    for d in (DEMO_ROOT, SESSIONS_DIR, CLIPS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(build_settings(), f, indent=2)
    return SETTINGS_PATH


# =============================================================================
#  Cameras
# =============================================================================


class DemoCamera(Camera):
    """Plays a test clip on loop, or draws a synthetic pattern."""

    def __init__(self, config):
        super().__init__(config)
        self._open = False
        self._clip: Optional[cv2.VideoCapture] = None
        self._background: Optional[np.ndarray] = None
        self._t0 = time.time()

    def open(self) -> bool:
        print(f"Opening camera: {self.config.name} (simulated)")
        clip_path = CLIPS_DIR / f"{self.config.role or self.config.id}.mp4"
        if clip_path.exists():
            self._clip = cv2.VideoCapture(str(clip_path))
            if not self._clip.isOpened():
                print(f"WARNING: could not read {clip_path}, using a synthetic pattern")
                self._clip = None
            else:
                print(f"  Playing test clip {clip_path.name}")
        self.config.actual_resolution = tuple(self.config.resolution)
        self.config.actual_fps = float(self.config.fps)
        self._t0 = time.time()
        self._open = True
        return True

    def close(self):
        print(f"Closing camera: {self.config.name} (simulated)")
        if self._clip is not None:
            self._clip.release()
            self._clip = None
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def read_frame(self) -> Optional[np.ndarray]:
        if not self._open:
            return None
        w, h = self.config.resolution
        if self._clip is not None:
            ok, frame = self._clip.read()
            if not ok:
                self._clip.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self._clip.read()
            if ok:
                if (frame.shape[1], frame.shape[0]) != (w, h):
                    frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
                return frame
        return self._synthetic_frame(w, h, time.time() - self._t0)

    # --- Synthetic patterns ---

    def _synthetic_frame(self, w, h, t):
        if self._background is None or self._background.shape[:2] != (h, w):
            self._background = _gradient(w, h, (44, 30, 20), (26, 16, 12))
        frame = self._background.copy()
        if self.config.role == "face" or self.config.id == "face_cam":
            _draw_face(frame, w, h, t)
            label = "SIMULATED FACE CAMERA"
        else:
            _draw_kicker(frame, w, h, t)
            label = "SIMULATED OVERHEAD CAMERA"
        # Labels sit at the bottom so they don't collide with Iris's HR overlay (top-left)
        cv2.putText(frame, label, (24, h - 44), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (249, 106, 123), 2, cv2.LINE_AA)
        cv2.putText(frame, f"{t:7.2f}s", (w - 170, h - 44), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (200, 200, 200), 2, cv2.LINE_AA)
        return frame


def _gradient(w, h, top, bottom):
    ramp = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None, None]
    top = np.array(top, dtype=np.float32)
    bottom = np.array(bottom, dtype=np.float32)
    img = top * (1 - ramp) + bottom * ramp
    return np.repeat(img, w, axis=1).astype(np.uint8)


def _point(origin, length, angle_deg):
    """End point of a limb hanging from origin; 0 deg points straight down."""
    a = math.radians(angle_deg)
    return (int(origin[0] + length * math.sin(a)), int(origin[1] + length * math.cos(a)))


def _draw_kicker(frame, w, h, t):
    """Side-on stick figure throwing a kick every 2.4 s."""
    s = h / 720
    floor_y = int(h * 0.80)
    cv2.line(frame, (0, floor_y), (w, floor_y), (90, 70, 60), max(2, int(3 * s)))

    p = (t % 2.4) / 2.4
    kick = math.sin(math.pi * p) ** 2                    # 0 -> 1 -> 0
    extend = min(1.0, max(0.0, (kick - 0.45) / 0.55))    # snap out at the top
    lean = -14 * kick

    thigh, shin, torso, upper_arm, forearm = 120 * s, 115 * s, 150 * s, 70 * s, 65 * s
    hip = (int(w * 0.48), int(floor_y - thigh - shin))
    neck = _point(hip, torso, 180 + lean)
    head = _point(hip, torso + 42 * s, 180 + lean)

    # Standing leg
    knee_s = _point(hip, thigh, -8)
    foot_s = (knee_s[0] + int(6 * s), floor_y)
    # Kicking leg: chamber the knee, then extend
    thigh_angle = 15 + 95 * kick
    knee_k = _point(hip, thigh, thigh_angle)
    shin_angle = thigh_angle - 110 * (1 - extend)
    foot_k = _point(knee_k, shin, shin_angle)
    # Guard
    elbow_l = _point(neck, upper_arm, 35 + lean)
    hand_l = _point(elbow_l, forearm, 160 + lean)
    elbow_r = _point(neck, upper_arm, -30 + lean)
    hand_r = _point(elbow_r, forearm, -150 + lean)

    colour, thick = (235, 235, 235), max(3, int(10 * s))
    for a, b in [(hip, neck), (hip, knee_s), (knee_s, foot_s), (hip, knee_k), (knee_k, foot_k),
                 (neck, elbow_l), (elbow_l, hand_l), (neck, elbow_r), (elbow_r, hand_r)]:
        cv2.line(frame, a, b, colour, thick, cv2.LINE_AA)
    cv2.circle(frame, head, int(30 * s), colour, thick, cv2.LINE_AA)
    cv2.circle(frame, foot_k, int(9 * s), (249, 106, 123), -1, cv2.LINE_AA)


def _draw_face(frame, w, h, t):
    """Abstract head-and-shoulders silhouette with a slight sway."""
    s = h / 720
    cx = int(w / 2 + 12 * s * math.sin(t * 0.9))
    cy = int(h * 0.42 + 5 * s * math.sin(t * 1.7))
    body = (96, 78, 70)
    cv2.ellipse(frame, (cx, int(h * 1.02)), (int(250 * s), int(190 * s)), 0, 180, 360, body, -1, cv2.LINE_AA)
    cv2.rectangle(frame, (cx - int(45 * s), cy + int(90 * s)), (cx + int(45 * s), int(h * 0.86)), body, -1)
    cv2.ellipse(frame, (cx, cy), (int(105 * s), int(130 * s)), 0, 0, 360, (130, 110, 100), -1, cv2.LINE_AA)


class DemoCameraManager(CameraManager):
    def __init__(self, camera_configs: list[dict]):
        super().__init__(camera_configs)
        self.cameras = {cid: DemoCamera(cam.config) for cid, cam in self.cameras.items()}


# =============================================================================
#  GoPros
# =============================================================================


class DemoGoProManager:
    """Same interface as GoProManager; no network traffic."""

    def __init__(self, gopro_configs: list[dict]):
        self.names = [c.get("name", c.get("id", "GoPro")) for c in gopro_configs
                      if c.get("enabled", True)]
        self._recording = False

    def connect_all(self) -> bool:
        for name in self.names:
            print(f"{name}: connected (simulated)")
        return True

    def start_recording_all(self):
        self._recording = True
        for name in self.names:
            print(f"{name}: recording started (simulated)")

    def stop_recording_all(self):
        if self._recording:
            for name in self.names:
                print(f"{name}: recording stopped (simulated)")
        self._recording = False

    def keep_alive_all(self):
        pass

    def disconnect_all(self):
        self._recording = False

    def get_status_all(self) -> dict:
        return {name: {"connected": True, "recording": self._recording} for name in self.names}


# =============================================================================
#  Heart rate
# =============================================================================


class DemoPolarH10(PolarH10):
    """Generates ~1 Hz heart rate and RR samples that follow the phase."""

    def connect(self) -> bool:
        print("Connecting to Polar H10 (simulated)...")
        time.sleep(1.0)
        self._battery_level = 87
        self._connection_ok = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._generate, daemon=True)
        self._thread.start()
        print("Polar H10 connected (simulated, battery 87%)")
        return True

    def _generate(self):
        rng = random.Random()
        bpm = 72.0
        while not self._stop_event.wait(1.0):
            target = _HR_TARGETS.get(self._current_phase, 78)
            bpm += (target - bpm) * 0.12 + rng.gauss(0, 1.2)
            if not self._recording:
                continue
            rr = [round(60000 / bpm + rng.gauss(0, 12), 1) for _ in range(rng.choice((1, 1, 2)))]
            sample = HeartRateSample(time.time(), int(round(bpm)), rr, True, self._current_phase)
            with self._samples_lock:
                self._hr_samples.append(sample)


# =============================================================================
#  Microphone
# =============================================================================


class DemoAudioRecorder(AudioRecorder):
    """Writes faint generated room tone in real time. Never opens a device."""

    def open(self, output_path: str) -> bool:
        import soundfile as sf

        self._sndfile = sf.SoundFile(output_path, mode="w", samplerate=self.config.sample_rate,
                                     channels=self.config.channels, subtype="PCM_16")
        self._stop = threading.Event()
        self._writer = None
        print("Audio device opened: Demo Mic (simulated)")
        return True

    def start_recording(self):
        self._recording = True
        self._stop.clear()
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()
        print("Audio recording started")

    def _write_loop(self):
        block = 1024
        rate = self.config.sample_rate
        rng = np.random.default_rng()
        next_t = time.perf_counter()
        while not self._stop.is_set():
            data = (rng.standard_normal((block, self.config.channels)) * 0.002).astype(np.float32)
            with self._lock:
                if self._sndfile is not None and self._recording:
                    self._sndfile.write(data)
            next_t += block / rate
            time.sleep(max(0.0, next_t - time.perf_counter()))

    def stop_recording(self):
        self._recording = False
        self._stop.set()
        if self._writer is not None:
            self._writer.join(timeout=1.0)
        print("Audio recording stopped")

    def close(self):
        self.stop_recording()
        with self._lock:
            if self._sndfile is not None:
                self._sndfile.close()
                self._sndfile = None
        print("Audio device closed")


# =============================================================================
#  Pre-flight check
# =============================================================================


class DemoCalibrationTool(CalibrationTool):
    """Reports every device as connected, one at a time."""

    # Set by install(): pushes each result to the GUI as soon as it is ready
    on_result = None
    step_delay = 0.3

    def run(self) -> bool:
        print("=" * 60)
        print("  CALIBRATION TOOL - Device Connectivity Check (demo)")
        print("=" * 60)
        s = self.settings
        checks = [(c["name"], "USB Camera") for c in s.get("cameras", []) if c.get("enabled", True)]
        checks += [(g["name"], "GoPro") for g in s.get("gopros", []) if g.get("enabled", True)]
        if s.get("heart_rate", {}).get("enabled"):
            checks.append(("Polar H10", "Heart Rate"))
        if s.get("microphone", {}).get("enabled"):
            checks.append((f"Microphone ({s['microphone'].get('device_name', 'Mic')})", "Audio"))

        time.sleep(0.4)
        for device, dev_type in checks:
            time.sleep(self.step_delay)
            result = {"device": device, "type": dev_type, "status": "PASS",
                      "detail": "connected (simulated)"}
            self._results.append(result)
            print(f"  {device}: PASS - connected (simulated)")
            if DemoCalibrationTool.on_result:
                DemoCalibrationTool.on_result(result)
        self._print_summary()
        return True


# =============================================================================
#  Install
# =============================================================================


def install(app):
    """Swap in the simulated devices and start the control server.

    Called once by the GUI when launched with --demo. The swaps are made on
    the modules the GUI and experiment import from, so they only exist inside
    this demo process.
    """
    import customtkinter as ctk

    from . import audio, calibrate, experiment

    experiment.CameraManager = DemoCameraManager
    experiment.GoProManager = DemoGoProManager
    experiment.PolarH10 = DemoPolarH10
    experiment.AudioRecorder = DemoAudioRecorder
    audio.AudioRecorder = DemoAudioRecorder
    audio.find_audio_device = lambda name_substring: 0
    calibrate.CalibrationTool = DemoCalibrationTool
    DemoCalibrationTool.on_result = (
        lambda r: app.after(0, lambda: app._apply_calibration_results([r]))
    )

    app.title("Iris (demo mode)")
    ctk.CTkLabel(
        app._status_label.master, text="  DEMO MODE · simulated devices  ",
        font=("Segoe UI", 14, "bold"), text_color="#0E0E2C", fg_color="#7B6AF9",
        corner_radius=6,
    ).pack(side="right", padx=10)

    controller = DemoController(app, app._demo_port)
    controller.start()
    app._demo_controller = controller
    print(f"Demo mode: output in {SESSIONS_DIR}")


# =============================================================================
#  Control server (used by video/record.py)
# =============================================================================


class DemoController:
    """Line-delimited JSON commands over localhost TCP.

    Each request is executed on the Tk thread and answered with one JSON line:
    ``{"ok": true, "result": ...}`` or ``{"ok": false, "error": "..."}``.
    """

    def __init__(self, app, port: int):
        self.app = app
        self.port = port
        self._requests: "queue.Queue[tuple[dict, queue.Queue]]" = queue.Queue()
        self._server = None

    def start(self):
        controller = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    try:
                        cmd = json.loads(line)
                    except json.JSONDecodeError as e:
                        self._reply({"ok": False, "error": f"bad json: {e}"})
                        continue
                    reply: queue.Queue = queue.Queue()
                    controller._requests.put((cmd, reply))
                    try:
                        self._reply(reply.get(timeout=10))
                    except queue.Empty:
                        self._reply({"ok": False, "error": "GUI did not respond"})

            def _reply(self, obj):
                self.wfile.write((json.dumps(obj, default=str) + "\n").encode())
                self.wfile.flush()

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.app.after(50, self._poll)
        print(f"Demo control server listening on 127.0.0.1:{self.port}")

    def _poll(self):
        try:
            while True:
                cmd, reply = self._requests.get_nowait()
                try:
                    reply.put({"ok": True, "result": self._execute(cmd)})
                except Exception as e:  # report, don't crash the GUI
                    reply.put({"ok": False, "error": f"{type(e).__name__}: {e}"})
        except queue.Empty:
            pass
        self.app.after(30, self._poll)

    # --- Commands ---

    def _execute(self, cmd: dict):
        app = self.app
        name = cmd.get("cmd")

        if name == "state":
            return self._state()
        if name == "tab":
            app.tabview.set(cmd["name"])
        elif name == "console":
            if bool(cmd.get("show")) != app._console_visible:
                app._toggle_console()
        elif name == "calibrate":
            app._run_calibrate()
        elif name == "run":
            app._run_experiment()
        elif name == "select_roles":
            app._on_camera_role_select(cmd.get("overhead", "overhead_camera"), "overhead")
        elif name == "tick":
            idx = cmd.get("index")
            targets = app._checklist_vars if idx is None else [app._checklist_vars[idx]]
            for var in targets:
                var.set(1)
            app._on_checklist_toggle()
        elif name == "continue":
            if app._video_player_visible:
                app._vp_continue()
            elif str(app._phase_display_continue.cget("state")) == "normal":
                app._on_continue()
            else:
                raise RuntimeError("Continue is disabled")
        elif name == "playpause":
            app._vp_toggle_playpause()
        elif name == "scroll":
            self._animate_scroll(cmd.get("target", "phases"), float(cmd.get("to", 1.0)),
                                 float(cmd.get("duration", 2.0)))
        elif name == "window":
            # Keep Iris in front while recording; release it for other windows
            if cmd.get("topmost", True):
                app.deiconify()
                app.attributes("-topmost", True)
                app.lift()
                app.focus_force()
            else:
                app.attributes("-topmost", False)
                if cmd.get("minimize"):
                    app.iconify()
        elif name == "geometry":
            return {n: self._geometry(n) for n in cmd.get("widgets", [])}
        elif name == "quit":
            app.after(100, app.destroy)
        else:
            raise ValueError(f"unknown command: {name}")
        return None

    def _state(self) -> dict:
        app = self.app
        phases = app.settings.get("phases", [])
        idx = app._current_phase_index
        sessions = sorted((d for d in SESSIONS_DIR.glob("*") if d.is_dir()),
                          key=lambda d: d.stat().st_mtime)
        return {
            "running": app._is_running,
            "calibration_done": app._calibration_done,
            "phase_index": idx,
            "phase_id": phases[idx]["id"] if app._is_running and idx < len(phases) else None,
            "continue_visible": bool(app._phase_display_continue.winfo_ismapped()),
            "continue_enabled": str(app._phase_display_continue.cget("state")) == "normal",
            "checklist_len": len(app._checklist_vars),
            "camera_selection": bool(app._cs_frame.winfo_ismapped()),
            "video_player": app._video_player_visible,
            "video_playing": app._video_playing,
            "countdown": app._countdown_active,
            "recording": app._rec_animating,
            "status": app._progress_label.cget("text"),
            "devices": [e["status_label"].cget("text") for e in app._device_status_rows],
            "session_dir": str(sessions[-1]) if sessions else None,
        }

    _WIDGETS = {
        "device_status": "_device_status_frame",
        "checklist": "_checklist_frame",
        "continue": "_phase_display_continue",
        "phase_name": "_phase_display_name",
        "phase_status": "_phase_status_label",
        "rec": "_rec_frame",
        "video": "_vp_canvas",
        "playpause": "_vp_playpause_btn",
        "camera_selection": "_cs_frame",
        "phase_display": "_phase_display",
        "tabview": "tabview",
    }

    def _geometry(self, name: str):
        widget = getattr(self.app, self._WIDGETS.get(name, name))
        if not widget.winfo_ismapped():
            return None
        return [widget.winfo_rootx(), widget.winfo_rooty(),
                widget.winfo_width(), widget.winfo_height()]

    def _animate_scroll(self, target: str, to: float, duration: float):
        frame = {"phases": self.app._phase_scroll, "cameras": self.app._cam_scroll,
                 "gopros": self.app._gp_scroll}[target]
        canvas = frame._parent_canvas
        start = canvas.yview()[0]
        steps = max(1, int(duration * 30))

        def step(i=1):
            p = i / steps
            eased = p * p * (3 - 2 * p)
            canvas.yview_moveto(start + (to - start) * eased)
            if i < steps:
                self.app.after(int(1000 * duration / steps), lambda: step(i + 1))

        step()


def send(command: dict, port: int = DEFAULT_PORT, timeout: float = 15.0):
    """Send one command to a running demo GUI and return its result."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall((json.dumps(command) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    reply = json.loads(data)
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error"))
    return reply.get("result")
