#!/usr/bin/env python3
"""Iris - Desktop GUI Application.

Run directly:    python gui.py
From launcher:   double-click Iris.pyw
"""

import ctypes
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
except ImportError:
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "Missing Dependency",
        "customtkinter is required.\n\nInstall with:\n  pip install customtkinter",
    )
    sys.exit(1)

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

# --- Constants ----------------------------------------------------------------

APP_TITLE = "Iris"
DEFAULT_CONFIG = "settings.json"
WINDOW_SIZE = "1400x950"
MIN_SIZE = (1200, 800)

FONT_HEADER = ("Segoe UI", 26, "bold")
FONT_SUB = ("Segoe UI", 20, "bold")
FONT_BODY = ("Segoe UI", 16)
FONT_SMALL = ("Segoe UI", 14)
FONT_BTN = ("Segoe UI", 20, "bold")
FONT_MONO = ("Consolas", 18)

CLR_GREEN = "#2ecc71"
CLR_GREEN_H = "#27ae60"
CLR_BLUE = "#3498db"
CLR_BLUE_H = "#2980b9"
CLR_RED = "#e74c3c"
CLR_RED_H = "#c0392b"
CLR_ORANGE = "#e67e22"
CLR_ORANGE_H = "#d35400"
CLR_PURPLE = "#9b59b6"
CLR_PURPLE_H = "#8e44ad"

GOPRO_MODELS = ["hero7_silver", "hero5_session"]
CAMERA_ROLES = ["overhead", "face", ""]

FONT_COUNTDOWN = ("Segoe UI", 120, "bold")

_PHASE_CHECKLISTS = {
    "warmup_calibration": [
        "GoPros are recording",
        "Participant wearing HR monitor",
        "Overhead camera positioned",
        "Face camera positioned",
        "Microphone is on",
        "Intrinsic calibration checkerboarding complete",
        "Extrinsic calibration checkerboarding complete",
        "Participant T-pose in the middle of the mat",
    ],
    "performance": [
        "Participant has completed all movements",
        "GoPro recording stopped",
    ],
    "finish": [
        "I want to stop recording participant heart rate",
    ],
}


# --- Stdout Redirector --------------------------------------------------------


class OutputRedirector:
    """Thread-safe redirector that sends print output to a queue."""

    def __init__(self, out_queue, tag="stdout"):
        self.queue = out_queue
        self.tag = tag

    def write(self, text):
        if text and text.strip():
            self.queue.put((self.tag, text))

    def flush(self):
        pass


# --- Application --------------------------------------------------------------


class IrisApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title(APP_TITLE)
        self.geometry(WINDOW_SIZE)
        self.minsize(*MIN_SIZE)
        self.attributes("-fullscreen", True)  # true fullscreen (no title bar/taskbar)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # Window icon
        logo_path = Path(os.path.dirname(os.path.abspath(__file__))) / "logo.png"
        if logo_path.exists() and PILImage:
            from PIL import ImageTk
            icon_img = PILImage.open(logo_path)
            self._icon_photo = ImageTk.PhotoImage(icon_img)
            self.iconphoto(True, self._icon_photo)

        # State
        self.settings = {}
        self.config_path = (
            Path(os.path.dirname(os.path.abspath(__file__))) / DEFAULT_CONFIG
        )
        self.output_queue = queue.Queue()
        self._worker_thread = None
        self._is_running = False
        self._active_experiment = None

        # Event queues for experiment <-> GUI communication
        self._gui_event_queue = queue.Queue()
        self._user_action_queue = queue.Queue()

        # Widget references (populated in build methods)
        self._exp_w = {}
        self._cam_cards = []
        self._gp_cards = []
        self._hr_w = {}
        self._mic_w = {}
        self._phase_cards = []
        self._cal_w = {}
        self._device_status_rows = []  # device status panel entries

        # Scrollable frame references (for rebuilding)
        self._cam_scroll = None
        self._gp_scroll = None
        self._phase_scroll = None

        # Video player state
        self._video_player_visible = False
        self._video_allow_pause = False
        self._video_first_play = True
        self._video_playing = False
        self._countdown_active = False

        # Console state
        self._console_visible = True
        self._console_frame = None

        # Experiment tab reference (for row weight reconfiguration)
        self._experiment_tab = None

        # Calibration / GoPro mode state
        self._calibration_done = False
        self._gopro_mode = "manual"

        # Phase tracking
        self._current_phase_index = 0
        self._experiment_layout_active = False

        # Recording indicator state
        self._rec_animating = False
        self._rec_dot_step = 0
        self._rec_start_time = 0.0
        self._rec_timer_id = None

        self._load_settings()
        self._build_ui()
        self._populate_ui()
        self._poll_console()
        self._poll_gui_events()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))

    # ==========================================================================
    #  Settings I/O
    # ==========================================================================

    def _load_settings(self, path=None):
        if path:
            self.config_path = Path(path)
        try:
            with open(self.config_path) as f:
                self.settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.settings = {
                "experiment": {
                    "name": "Taekwondo Experiment",
                    "output_dir": "C:/Users/BarlabPRIME/desktop/Iris_Recorded_Taekwondo_Data",
                    "recording_format": "mp4",
                },
                "cameras": [],
                "gopros": [],
                "heart_rate": {
                    "enabled": False,
                    "device_address": None,
                    "ecg_enabled": False,
                },
                "microphone": {
                    "enabled": False,
                    "device_name": "Tonor",
                    "device_index": None,
                    "sample_rate": 44100,
                    "channels": 1,
                },
                "calibration": {
                    "checkerboard_cols": 10,
                    "checkerboard_rows": 7,
                    "square_size_mm": 25.0,
                    "run_intrinsic": True,
                    "run_extrinsic": True,
                },
                "phases": [],
            }

    def _save_settings(self):
        self._collect_from_ui()
        with open(self.config_path, "w") as f:
            json.dump(self.settings, f, indent=2)
        self._log(f"Settings saved to {self.config_path}")

    def _load_settings_dialog(self):
        path = filedialog.askopenfilename(
            title="Load Configuration",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialdir=str(self.config_path.parent),
        )
        if path:
            self._load_settings(path)
            self._populate_ui()
            self._log(f"Loaded settings from {path}")

    def _collect_from_ui(self):
        """Read current widget values back into self.settings dict."""
        w = self._exp_w
        self.settings["experiment"]["name"] = w["name"].get()
        self.settings["experiment"]["output_dir"] = w["output_dir"].get()
        self.settings["experiment"]["recording_format"] = w["recording_format"].get()

        # Cameras
        cameras = []
        for c in self._cam_cards:
            try:
                cameras.append(
                    {
                        "id": c["id"].get(),
                        "name": c["name"].get(),
                        "device_index": int(c["device_index"].get() or 0),
                        "resolution": [
                            int(c["res_w"].get() or 1920),
                            int(c["res_h"].get() or 1080),
                        ],
                        "fps": int(c["fps"].get() or 30),
                        "enabled": bool(c["enabled"].get()),
                        "role": c["role"].get() or None,
                    }
                )
            except ValueError:
                pass
        self.settings["cameras"] = cameras

        # GoPros
        gopros = []
        for g in self._gp_cards:
            gopros.append(
                {
                    "id": g["id"].get(),
                    "name": g["name"].get(),
                    "model": g["model"].get(),
                    "wifi_interface": g["wifi_interface"].get(),
                    "ip_address": g["ip_address"].get(),
                    "enabled": bool(g["enabled"].get()),
                }
            )
        self.settings["gopros"] = gopros

        # Heart Rate
        hw = self._hr_w
        self.settings["heart_rate"]["enabled"] = bool(hw["enabled"].get())
        addr = hw["device_address"].get().strip()
        self.settings["heart_rate"]["device_address"] = addr if addr else None
        self.settings["heart_rate"]["ecg_enabled"] = bool(hw["ecg_enabled"].get())

        # Microphone
        mw = self._mic_w
        self.settings.setdefault("microphone", {})
        self.settings["microphone"]["enabled"] = bool(mw["enabled"].get())
        self.settings["microphone"]["device_name"] = mw["device_name"].get().strip()
        dev_idx = mw["device_index"].get().strip()
        self.settings["microphone"]["device_index"] = int(dev_idx) if dev_idx else None
        sr = mw["sample_rate"].get().strip()
        self.settings["microphone"]["sample_rate"] = int(sr) if sr else 44100
        ch = mw["channels"].get().strip()
        self.settings["microphone"]["channels"] = int(ch) if ch else 1

        # Calibration
        cw = self._cal_w
        self.settings.setdefault("calibration", {})
        self.settings["calibration"]["run_intrinsic"] = bool(cw["run_intrinsic"].get())
        self.settings["calibration"]["run_extrinsic"] = bool(cw["run_extrinsic"].get())

        # Phases
        phases = []
        for p in self._phase_cards:
            dur = p["duration"].get().strip()
            interval = p["interval"].get().strip()
            phase_data = {
                "id": p["id"].get(),
                "name": p["name"].get(),
                "duration_seconds": int(dur) if dur else 0,
                "capture_interval_ms": int(interval) if interval else None,
                "instructions": p["instructions"].get("1.0", "end-1c"),
            }
            # Preserve extended fields from existing settings
            existing = self.settings.get("phases", [])
            if p.get("_index") is not None and p["_index"] < len(existing):
                for key in ("record_video", "record_audio", "record_gopro",
                            "allow_pause", "cameras"):
                    if key in existing[p["_index"]]:
                        phase_data[key] = existing[p["_index"]][key]
            phases.append(phase_data)
        self.settings["phases"] = phases

    # ==========================================================================
    #  UI Construction
    # ==========================================================================

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=3)
        self.grid_rowconfigure(1, weight=0)
        self.grid_rowconfigure(2, weight=2)
        self.grid_columnconfigure(0, weight=1)

        # Tabs
        self.tabview = ctk.CTkTabview(self, anchor="w")
        self.tabview.grid(row=0, column=0, padx=10, pady=(10, 0), sticky="nsew")

        self._build_experiment_tab(self.tabview.add("Experiment"))
        self._build_devices_tab(self.tabview.add("Devices"))
        self._build_phases_tab(self.tabview.add("Phases"))
        self._build_calibration_tab(self.tabview.add("Calibration"))

        # Settings bar
        bar = ctk.CTkFrame(self, height=50)
        bar.grid(row=1, column=0, padx=10, pady=6, sticky="ew")

        ctk.CTkButton(
            bar, text="Save Settings", width=150, height=38,
            font=FONT_BODY, command=self._save_settings
        ).pack(side="left", padx=8, pady=8)
        ctk.CTkButton(
            bar, text="Load Settings", width=150, height=38,
            font=FONT_BODY, command=self._load_settings_dialog
        ).pack(side="left", padx=8, pady=8)

        self._console_toggle_btn = ctk.CTkButton(
            bar, text="Hide Console", width=150, height=38,
            font=FONT_BODY, command=self._toggle_console
        )
        self._console_toggle_btn.pack(side="left", padx=8, pady=8)

        ctk.CTkButton(
            bar, text="Exit", width=100, height=38,
            font=FONT_BODY, fg_color=CLR_RED, hover_color=CLR_RED_H,
            command=self._on_close,
        ).pack(side="right", padx=8, pady=8)

        self._status_label = ctk.CTkLabel(bar, text="Ready", font=FONT_BODY)
        self._status_label.pack(side="right", padx=20)

        # GoPro mode indicator (visible on all pages)
        self._mode_indicator = ctk.CTkLabel(
            bar, text="  NOT CALIBRATED  ", font=("Segoe UI", 14, "bold"),
            text_color="gray", corner_radius=6,
        )
        self._mode_indicator.pack(side="right", padx=10)

        # Console
        self._build_console()

    # --- Experiment Tab ---

    def _build_experiment_tab(self, parent):
        self._experiment_tab = parent
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_columnconfigure(1, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=0)

        # Left: settings + device status
        self._exp_left = ctk.CTkFrame(parent)
        left = self._exp_left
        left.grid(row=0, column=0, padx=(0, 8), pady=8, sticky="nsew")

        # -- Experiment Settings (hidden once experiment starts) --
        self._exp_settings_frame = ctk.CTkFrame(left, fg_color="transparent")
        self._exp_settings_frame.pack(fill="x")

        ctk.CTkLabel(self._exp_settings_frame, text="Experiment Settings", font=FONT_HEADER).pack(
            anchor="w", padx=25, pady=(25, 20)
        )

        self._exp_w["name"] = self._labeled_entry(self._exp_settings_frame, "Name:", "Taekwondo Experiment")
        self._exp_w["output_dir"] = self._labeled_entry(
            self._exp_settings_frame, "Output Dir:", "./output", browse="dir"
        )

        row = ctk.CTkFrame(self._exp_settings_frame, fg_color="transparent")
        row.pack(fill="x", padx=25, pady=8)
        ctk.CTkLabel(row, text="Rec. Format:", width=140, anchor="w", font=FONT_BODY).pack(side="left")
        fmt = ctk.StringVar(value="mp4")
        ctk.CTkOptionMenu(row, variable=fmt, values=["mp4"], width=120,
                          font=FONT_BODY, height=36).pack(side="left")
        self._exp_w["recording_format"] = fmt

        # --- Device Status Section ---
        self._device_status_separator = ctk.CTkFrame(left, fg_color="gray30", height=2)
        self._device_status_separator.pack(fill="x", padx=25, pady=(20, 0))

        self._device_status_header = ctk.CTkLabel(left, text="Device Status", font=FONT_HEADER)
        self._device_status_header.pack(anchor="w", padx=25, pady=(14, 10))

        self._device_status_frame = ctk.CTkFrame(left, fg_color="transparent")
        self._device_status_frame.pack(fill="both", expand=True, padx=25, pady=(0, 15))

        # --- Advancement Checklist Section (hidden until experiment runs) ---
        self._checklist_separator = ctk.CTkFrame(left, fg_color="gray30", height=2)
        self._checklist_header = ctk.CTkLabel(
            left, text="Advancement Requirements", font=FONT_SUB,
        )
        self._checklist_frame = ctk.CTkFrame(left, fg_color="transparent")
        self._checklist_vars = []
        # Not packed initially — shown during experiment via _update_checklist()

        # Right: actions
        self._exp_right = ctk.CTkFrame(parent)
        right = self._exp_right
        right.grid(row=0, column=1, padx=(8, 0), pady=8, sticky="nsew")

        ctk.CTkLabel(right, text="Actions", font=FONT_HEADER).pack(
            anchor="w", padx=25, pady=(25, 10)
        )

        self._summary_label = ctk.CTkLabel(
            right, text="", font=FONT_BODY, justify="left", anchor="w"
        )
        self._summary_label.pack(anchor="w", padx=25, pady=(0, 15))

        btn_f = ctk.CTkFrame(right, fg_color="transparent")
        btn_f.pack(fill="x", padx=25, pady=0)

        self._run_btn = ctk.CTkButton(
            btn_f,
            text="Run Experiment (MUST CALIBRATE FIRST)",
            font=FONT_BTN,
            text_color="white",
            fg_color=CLR_GREEN,
            hover_color=CLR_GREEN_H,
            height=60,
            command=self._run_experiment,
            state="disabled",
        )
        self._run_btn.pack(fill="x", pady=6)

        self._cal_btn = ctk.CTkButton(
            btn_f,
            text="Calibrate Devices",
            font=FONT_BTN,
            text_color="white",
            fg_color=CLR_BLUE,
            hover_color=CLR_BLUE_H,
            height=60,
            command=self._run_calibrate,
        )
        self._cal_btn.pack(fill="x", pady=6)

        self._redo_btn = ctk.CTkButton(
            btn_f,
            text="Redo Last Phase",
            font=FONT_BTN,
            text_color="white",
            fg_color=CLR_ORANGE,
            hover_color=CLR_ORANGE_H,
            height=60,
            command=self._redo_last_phase,
            state="disabled",
        )
        self._redo_btn.pack(fill="x", pady=6)

        # Progress section
        prog_f = ctk.CTkFrame(right, fg_color="transparent")
        prog_f.pack(fill="x", padx=25, pady=(20, 10))

        self._phase_indicator = ctk.CTkLabel(prog_f, text="", font=FONT_BODY)
        self._phase_indicator.pack(anchor="w", pady=(0, 4))

        self._progress_label = ctk.CTkLabel(prog_f, text="", font=FONT_BODY)
        self._progress_label.pack(anchor="w", pady=(0, 4))

        self._progress_bar = ctk.CTkProgressBar(prog_f, height=18)
        self._progress_bar.pack(fill="x", pady=(8, 0))
        self._progress_bar.set(0)

        # Continue button (hidden by default, shown when experiment waits for user)
        self._continue_btn = ctk.CTkButton(
            prog_f,
            text="Continue",
            font=FONT_BTN,
            fg_color=CLR_BLUE,
            hover_color=CLR_BLUE_H,
            height=55,
            command=self._on_continue,
        )
        # Not packed initially — shown/hidden dynamically

        # ---- Center Phase Display (hidden by default, shown during experiment) ----
        self._phase_display = ctk.CTkFrame(parent, fg_color="transparent")
        # Not placed in grid initially

        self._phase_display_name = ctk.CTkLabel(
            self._phase_display, text="", font=("Segoe UI", 72, "bold"),
            anchor="center", justify="center",
        )
        self._phase_display_name.pack(expand=True, fill="both", pady=(40, 5))

        self._phase_display_desc = ctk.CTkLabel(
            self._phase_display, text="", font=("Segoe UI", 28),
            anchor="center", justify="center", text_color="gray70",
            wraplength=800,
        )
        self._phase_display_desc.pack(fill="x", pady=(0, 10))

        # --- Phase status indicator (IN PROGRESS) ---
        self._phase_status_label = ctk.CTkLabel(
            self._phase_display, text="", font=("Segoe UI", 22, "bold"),
            text_color=CLR_ORANGE, anchor="center",
        )
        self._phase_status_label.pack(fill="x", pady=(0, 6))

        # --- Recording indicator (REC ● with animated dots + timer) ---
        self._rec_frame = ctk.CTkFrame(self._phase_display, fg_color="transparent")
        # Not packed initially — shown/hidden dynamically

        self._rec_dot_label = ctk.CTkLabel(
            self._rec_frame, text="\u25cf", font=("Segoe UI", 48),
            text_color=CLR_RED, width=50,
        )
        self._rec_dot_label.pack(side="left", padx=(0, 4))

        self._rec_text_label = ctk.CTkLabel(
            self._rec_frame, text="REC", font=("Segoe UI", 38, "bold"),
            text_color=CLR_RED,
        )
        self._rec_text_label.pack(side="left")

        self._rec_dots_label = ctk.CTkLabel(
            self._rec_frame, text="", font=("Segoe UI", 38, "bold"),
            text_color=CLR_RED, width=60, anchor="w",
        )
        self._rec_dots_label.pack(side="left")

        self._rec_timer_label = ctk.CTkLabel(
            self._rec_frame, text="00:00", font=("Consolas", 30),
            text_color=CLR_RED,
        )
        self._rec_timer_label.pack(side="left", padx=(20, 0))

        self._phase_display_progress = ctk.CTkProgressBar(self._phase_display, height=14)
        self._phase_display_progress.pack(fill="x", padx=60, pady=(0, 8))
        self._phase_display_progress.set(0)

        # Bottom button area — continue (dynamic) then stop (always visible)
        pd_btn_frame = ctk.CTkFrame(self._phase_display, fg_color="transparent")
        pd_btn_frame.pack(fill="x", padx=60, pady=(5, 20))

        self._phase_display_continue = ctk.CTkButton(
            pd_btn_frame,
            text="Continue",
            font=FONT_BTN,
            fg_color=CLR_GREEN,
            hover_color=CLR_GREEN_H,
            height=65,
            command=self._on_continue,
        )
        # Not packed initially — shown/hidden dynamically

        self._phase_display_redo = ctk.CTkButton(
            pd_btn_frame,
            text="Redo Last Phase",
            font=FONT_BODY,
            fg_color=CLR_ORANGE,
            hover_color=CLR_ORANGE_H,
            height=45,
            command=self._redo_last_phase,
        )
        self._phase_display_redo.pack(fill="x", pady=(5, 0))

        # Done-choice buttons (shown after finish phase)
        self._done_end_btn = ctk.CTkButton(
            pd_btn_frame,
            text="End Experiment",
            font=FONT_BTN,
            fg_color=CLR_GREEN,
            hover_color=CLR_GREEN_H,
            height=65,
            command=self._on_end_experiment,
        )
        self._done_posthoc_btn = ctk.CTkButton(
            pd_btn_frame,
            text="Post-hoc Calibration",
            font=FONT_BTN,
            fg_color=CLR_BLUE,
            hover_color=CLR_BLUE_H,
            height=65,
            command=self._on_continue_posthoc,
        )
        # Not packed initially

        # Camera selection panel (hidden by default, spans both columns)
        self._build_camera_selection_panel(parent)

        # Video player panel (hidden by default, spans both columns)
        self._build_video_player_panel(parent)

    def _build_camera_selection_panel(self, parent):
        """Build the camera selection panel (hidden by default)."""
        self._cs_frame = ctk.CTkFrame(parent)
        # Not placed in grid initially

        ctk.CTkLabel(
            self._cs_frame, text="Assign Camera Roles", font=FONT_HEADER,
        ).pack(pady=(20, 5))
        ctk.CTkLabel(
            self._cs_frame,
            text="Identify which camera is OVERHEAD (pointing down at the mat) "
                 "and which is FACE (pointing at the participant).",
            font=FONT_BODY, text_color="gray70", wraplength=900,
        ).pack(pady=(0, 15))

        # Container for the two side-by-side previews
        self._cs_previews_frame = ctk.CTkFrame(self._cs_frame, fg_color="transparent")
        self._cs_previews_frame.pack(fill="both", expand=True, padx=20, pady=(0, 15))
        self._cs_previews_frame.grid_columnconfigure(0, weight=1)
        self._cs_previews_frame.grid_columnconfigure(1, weight=1)
        self._cs_previews_frame.grid_rowconfigure(0, weight=1)

        # Build two camera cards (will be populated when shown)
        self._cs_cards = []
        for col in range(2):
            card = ctk.CTkFrame(self._cs_previews_frame)
            card.grid(row=0, column=col, padx=10, pady=5, sticky="nsew")

            lbl = ctk.CTkLabel(card, text="", font=FONT_SUB)
            lbl.pack(pady=(10, 2))

            dev_lbl = ctk.CTkLabel(card, text="", font=FONT_SMALL, text_color="gray60")
            dev_lbl.pack(pady=(0, 5))

            img_lbl = ctk.CTkLabel(card, text="No preview", fg_color="#1a1a1a")
            img_lbl.pack(fill="both", expand=True, padx=10, pady=5)

            btn_row = ctk.CTkFrame(card, fg_color="transparent")
            btn_row.pack(fill="x", padx=10, pady=(5, 10))

            overhead_btn = ctk.CTkButton(
                btn_row, text="This is OVERHEAD", font=FONT_BTN, height=50,
                fg_color=CLR_BLUE, hover_color=CLR_BLUE_H,
            )
            overhead_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))

            face_btn = ctk.CTkButton(
                btn_row, text="This is FACE", font=FONT_BTN, height=50,
                fg_color=CLR_PURPLE, hover_color=CLR_PURPLE_H,
            )
            face_btn.pack(side="left", fill="x", expand=True, padx=(5, 0))

            self._cs_cards.append({
                "card": card, "name_label": lbl, "dev_label": dev_lbl,
                "img_label": img_lbl, "overhead_btn": overhead_btn,
                "face_btn": face_btn, "camera_id": None,
            })

    def _build_video_player_panel(self, parent):
        """Build the video player panel (hidden by default)."""
        self._vp_frame = ctk.CTkFrame(parent)
        # Not placed in grid initially - will be shown/hidden dynamically

        # Header row with title, message, and device indicators
        hdr = ctk.CTkFrame(self._vp_frame, fg_color="transparent")
        hdr.pack(fill="x", padx=10, pady=(5, 0))
        self._vp_title_label = ctk.CTkLabel(hdr, text="Video Player", font=FONT_SUB)
        self._vp_title_label.pack(side="left")
        self._vp_message = ctk.CTkLabel(
            hdr, text="", font=FONT_SMALL, text_color="gray"
        )
        self._vp_message.pack(side="left", padx=15)

        # Device indicators (right side of header)
        ind_frame = ctk.CTkFrame(hdr, fg_color="transparent")
        ind_frame.pack(side="right")

        self._vp_ind_face = self._build_vp_indicator(ind_frame, "Face Cam")
        self._vp_ind_hr = self._build_vp_indicator(ind_frame, "Heart Rate")
        self._vp_ind_mic = self._build_vp_indicator(ind_frame, "Microphone")

        # Video display area - expands to fill available space
        self._vp_canvas = ctk.CTkLabel(
            self._vp_frame, text="No video", fg_color="#1a1a1a"
        )
        self._vp_canvas.pack(fill="both", expand=True, padx=5, pady=5)

        # Controls row
        ctrl = ctk.CTkFrame(self._vp_frame, fg_color="transparent")
        ctrl.pack(fill="x", padx=10, pady=(0, 5))

        # Recording indicator for video player
        self._vp_rec_frame = ctk.CTkFrame(ctrl, fg_color="transparent")
        self._vp_rec_frame.pack(side="left", padx=10)

        self._vp_rec_dot = ctk.CTkLabel(
            self._vp_rec_frame, text="\u25cf", font=("Segoe UI", 28),
            text_color=CLR_RED, width=24,
        )
        self._vp_rec_dot.pack(side="left", padx=(0, 2))

        self._vp_recording_label = ctk.CTkLabel(
            self._vp_rec_frame, text="", font=("Segoe UI", 20, "bold"),
            text_color=CLR_RED,
        )
        self._vp_recording_label.pack(side="left")

        self._vp_rec_dots = ctk.CTkLabel(
            self._vp_rec_frame, text="", font=("Segoe UI", 20, "bold"),
            text_color=CLR_RED, width=36, anchor="w",
        )
        self._vp_rec_dots.pack(side="left")

        self._vp_rec_timer = ctk.CTkLabel(
            self._vp_rec_frame, text="", font=("Consolas", 18),
            text_color=CLR_RED,
        )
        self._vp_rec_timer.pack(side="left", padx=(10, 0))

        # Hide all VP rec widgets initially
        self._vp_rec_frame.pack_forget()

        self._vp_continue_btn = ctk.CTkButton(
            ctrl, text="Continue", width=100, height=32,
            fg_color=CLR_BLUE, hover_color=CLR_BLUE_H,
            command=self._vp_continue,
        )
        self._vp_continue_btn.pack(side="right", padx=(0, 5))

        self._vp_time_label = ctk.CTkLabel(ctrl, text="0:00 / 0:00", font=FONT_SMALL)
        self._vp_time_label.pack(side="right", padx=10)

        # Centered play/pause toggle button
        self._vp_playpause_btn = ctk.CTkButton(
            ctrl, text="\u25B6  Play", width=220, height=50,
            font=FONT_BTN,
            fg_color=CLR_GREEN, hover_color=CLR_GREEN_H,
            command=self._vp_toggle_playpause,
        )
        self._vp_playpause_btn.pack(anchor="center", pady=(0, 2))

        self._vp_spacebar_hint = ctk.CTkLabel(
            ctrl, text="or press Spacebar", font=FONT_SMALL, text_color="gray50",
        )
        self._vp_spacebar_hint.pack(anchor="center")

    def _build_vp_indicator(self, parent, label):
        """Build a single device indicator (dot + label) for the video player."""
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(side="left", padx=(12, 0))
        dot = ctk.CTkLabel(frame, text="\u25cf", font=("Segoe UI", 16),
                           text_color="gray50", width=18)
        dot.pack(side="left")
        lbl = ctk.CTkLabel(frame, text=label, font=("Segoe UI", 13, "bold"),
                           text_color="gray50")
        lbl.pack(side="left", padx=(3, 0))
        return {"dot": dot, "label": lbl}

    def _set_vp_indicator(self, indicator, active):
        """Set a video player device indicator to active (green) or inactive (gray)."""
        color = CLR_GREEN if active else "gray50"
        indicator["dot"].configure(text_color=color)
        indicator["label"].configure(text_color=color)

    # --- Recording Indicator Animation ---

    def _start_rec_animation(self):
        """Start the recording indicator animation (dots + timer) on both phase display and video player."""
        self._rec_animating = True
        self._rec_dot_step = 0
        self._rec_start_time = time.time()
        # Show phase display REC frame (before the progress bar)
        self._rec_frame.pack(pady=(4, 8), before=self._phase_display_progress)
        # Show VP REC frame
        self._vp_rec_frame.pack(side="left", padx=10)
        self._vp_recording_label.configure(text="REC")
        self._vp_rec_timer.configure(text="00:00")
        self._rec_timer_label.configure(text="00:00")
        self._rec_animate_tick()

    def _stop_rec_animation(self):
        """Stop the recording animation and hide indicators."""
        self._rec_animating = False
        if self._rec_timer_id is not None:
            self.after_cancel(self._rec_timer_id)
            self._rec_timer_id = None
        # Hide phase display REC
        self._rec_frame.pack_forget()
        # Hide VP REC
        self._vp_rec_frame.pack_forget()
        self._vp_recording_label.configure(text="")
        self._vp_rec_dots.configure(text="")
        self._vp_rec_timer.configure(text="")

    def _rec_animate_tick(self):
        """Animate the recording dots and update the timer."""
        if not self._rec_animating:
            return
        # Cycle dots: ., .., ...
        self._rec_dot_step = (self._rec_dot_step + 1) % 4
        dots = "." * self._rec_dot_step if self._rec_dot_step > 0 else ""
        self._rec_dots_label.configure(text=dots)
        self._vp_rec_dots.configure(text=dots)

        # Pulse the red dot visibility (blink every other tick)
        if self._rec_dot_step % 2 == 0:
            self._rec_dot_label.configure(text_color=CLR_RED)
            self._vp_rec_dot.configure(text_color=CLR_RED)
        else:
            self._rec_dot_label.configure(text_color="#aa1111")
            self._vp_rec_dot.configure(text_color="#aa1111")

        # Update timer
        elapsed = time.time() - self._rec_start_time
        mins, secs = divmod(int(elapsed), 60)
        timer_text = f"{mins:02d}:{secs:02d}"
        self._rec_timer_label.configure(text=timer_text)
        self._vp_rec_timer.configure(text=timer_text)

        self._rec_timer_id = self.after(500, self._rec_animate_tick)

    # --- Devices Tab ---

    def _build_devices_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_columnconfigure(1, weight=1)
        parent.grid_rowconfigure(0, weight=2)
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_rowconfigure(2, weight=1)

        # USB Cameras (top-left)
        cam_frame = ctk.CTkFrame(parent)
        cam_frame.grid(row=0, column=0, padx=(0, 3), pady=(0, 3), sticky="nsew")

        hdr = ctk.CTkFrame(cam_frame, fg_color="transparent")
        hdr.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(hdr, text="USB Cameras", font=FONT_SUB).pack(side="left")
        ctk.CTkButton(
            hdr, text="+ Add", width=70, height=28, command=self._add_camera
        ).pack(side="right")

        self._cam_scroll = ctk.CTkScrollableFrame(cam_frame)
        self._cam_scroll.pack(fill="both", expand=True, padx=5, pady=5)

        # GoPros (top-right)
        gp_frame = ctk.CTkFrame(parent)
        gp_frame.grid(row=0, column=1, padx=(3, 0), pady=(0, 3), sticky="nsew")

        hdr = ctk.CTkFrame(gp_frame, fg_color="transparent")
        hdr.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(hdr, text="GoPro Cameras", font=FONT_SUB).pack(side="left")
        ctk.CTkButton(
            hdr, text="+ Add", width=70, height=28, command=self._add_gopro
        ).pack(side="right")

        self._gp_scroll = ctk.CTkScrollableFrame(gp_frame)
        self._gp_scroll.pack(fill="both", expand=True, padx=5, pady=5)

        # Heart Rate (bottom-left)
        hr_frame = ctk.CTkFrame(parent)
        hr_frame.grid(row=1, column=0, padx=(0, 3), pady=(3, 0), sticky="nsew")

        ctk.CTkLabel(hr_frame, text="Heart Rate (Polar H10)", font=FONT_SUB).pack(
            anchor="w", padx=15, pady=(10, 5)
        )

        hr_row = ctk.CTkFrame(hr_frame, fg_color="transparent")
        hr_row.pack(fill="x", padx=15, pady=5)

        en_var = ctk.IntVar(value=0)
        ctk.CTkSwitch(hr_row, text="Enabled", variable=en_var).pack(
            side="left", padx=(0, 20)
        )
        self._hr_w["enabled"] = en_var

        ctk.CTkLabel(hr_row, text="Address:", anchor="w").pack(
            side="left", padx=(0, 5)
        )
        addr_e = ctk.CTkEntry(hr_row, width=180, placeholder_text="auto-scan if empty")
        addr_e.pack(side="left", padx=(0, 15))
        self._hr_w["device_address"] = addr_e

        ecg_var = ctk.IntVar(value=0)
        ctk.CTkSwitch(hr_row, text="ECG", variable=ecg_var).pack(
            side="left"
        )
        self._hr_w["ecg_enabled"] = ecg_var

        # Microphone (bottom-right)
        mic_frame = ctk.CTkFrame(parent)
        mic_frame.grid(row=1, column=1, padx=(3, 0), pady=(3, 0), sticky="nsew")

        ctk.CTkLabel(mic_frame, text="Microphone", font=FONT_SUB).pack(
            anchor="w", padx=15, pady=(10, 5)
        )

        mic_r1 = ctk.CTkFrame(mic_frame, fg_color="transparent")
        mic_r1.pack(fill="x", padx=15, pady=3)

        mic_en = ctk.IntVar(value=0)
        ctk.CTkSwitch(mic_r1, text="Enabled", variable=mic_en).pack(
            side="left", padx=(0, 15)
        )
        self._mic_w["enabled"] = mic_en

        ctk.CTkLabel(mic_r1, text="Device:", anchor="w").pack(side="left", padx=(0, 5))
        self._mic_w["device_name"] = self._inline_entry(mic_r1, "Tonor", width=120)

        ctk.CTkLabel(mic_r1, text="Index:", anchor="w").pack(side="left", padx=(10, 5))
        self._mic_w["device_index"] = self._inline_entry(mic_r1, "", width=40)

        mic_r2 = ctk.CTkFrame(mic_frame, fg_color="transparent")
        mic_r2.pack(fill="x", padx=15, pady=3)

        ctk.CTkLabel(mic_r2, text="Sample Rate:", anchor="w").pack(side="left", padx=(0, 5))
        self._mic_w["sample_rate"] = self._inline_entry(mic_r2, "44100", width=70)

        ctk.CTkLabel(mic_r2, text="Channels:", anchor="w").pack(side="left", padx=(10, 5))
        self._mic_w["channels"] = self._inline_entry(mic_r2, "1", width=40)

        ctk.CTkButton(
            mic_r2, text="Test Mic", width=80, height=28,
            fg_color=CLR_PURPLE, hover_color=CLR_PURPLE_H,
            command=self._test_microphone,
        ).pack(side="right")

    # --- Phases Tab ---

    def _build_phases_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        hdr = ctk.CTkFrame(parent, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=10, pady=(8, 0), sticky="ew")
        ctk.CTkLabel(hdr, text="Experiment Phases", font=FONT_SUB).pack(side="left")
        ctk.CTkButton(
            hdr, text="+ Add Phase", width=100, height=28, command=self._add_phase
        ).pack(side="right")

        ctk.CTkLabel(
            hdr,
            text="Duration 0 = wait for user Continue. Phases are controlled by experiment handlers.",
            font=FONT_SMALL,
            text_color="gray",
        ).pack(side="left", padx=20)

        self._phase_scroll = ctk.CTkScrollableFrame(parent)
        self._phase_scroll.grid(row=1, column=0, padx=5, pady=5, sticky="nsew")

    # --- Calibration Tab (replaces Undistort) ---

    def _build_calibration_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_columnconfigure(1, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        # Left: Lens Correction
        left = ctk.CTkFrame(parent)
        left.grid(row=0, column=0, padx=(0, 5), pady=5, sticky="nsew")

        ctk.CTkLabel(left, text="GoPro Lens Correction", font=FONT_SUB).pack(
            anchor="w", padx=15, pady=(15, 5)
        )
        ctk.CTkLabel(
            left,
            text="Apply barrel distortion correction to GoPro video files.\n"
            "Select a single .mp4 file or a folder containing .mp4 files.",
            font=FONT_SMALL,
            text_color="gray",
            justify="left",
        ).pack(anchor="w", padx=15, pady=(0, 10))

        self._cal_w["input"] = self._labeled_entry(
            left, "Input Path:", "", browse="file_or_dir"
        )

        btn_f = ctk.CTkFrame(left, fg_color="transparent")
        btn_f.pack(fill="x", padx=15, pady=10)

        self._undist_btn = ctk.CTkButton(
            btn_f,
            text="Process Videos",
            font=FONT_BODY,
            fg_color=CLR_ORANGE,
            hover_color=CLR_ORANGE_H,
            height=42,
            width=200,
            command=self._run_undistort,
        )
        self._undist_btn.pack(side="left")

        # Right: Multi-Camera Calibration
        right = ctk.CTkFrame(parent)
        right.grid(row=0, column=1, padx=(5, 0), pady=5, sticky="nsew")

        ctk.CTkLabel(right, text="Multi-Camera Calibration", font=FONT_SUB).pack(
            anchor="w", padx=15, pady=(15, 5)
        )
        ctk.CTkLabel(
            right,
            text="Upload GoPro footage and run intrinsic + extrinsic\n"
            "calibration across multiple cameras.",
            font=FONT_SMALL,
            text_color="gray",
            justify="left",
        ).pack(anchor="w", padx=15, pady=(0, 10))

        # GoPro file browser entries
        self._cal_w["gopro_files"] = []
        for i in range(2):
            row = ctk.CTkFrame(right, fg_color="transparent")
            row.pack(fill="x", padx=15, pady=2)
            ctk.CTkLabel(row, text=f"GoPro {i+1}:", width=70, anchor="w").pack(side="left")
            entry = ctk.CTkEntry(row)
            entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
            ctk.CTkButton(
                row, text="Browse", width=70,
                command=lambda e=entry: self._browse_file(e),
            ).pack(side="left")
            self._cal_w["gopro_files"].append(entry)

        # Calibration options
        opt_f = ctk.CTkFrame(right, fg_color="transparent")
        opt_f.pack(fill="x", padx=15, pady=(10, 5))

        intr_var = ctk.IntVar(value=1)
        ctk.CTkSwitch(opt_f, text="Intrinsic Cal", variable=intr_var).pack(
            side="left", padx=(0, 20)
        )
        self._cal_w["run_intrinsic"] = intr_var

        extr_var = ctk.IntVar(value=1)
        ctk.CTkSwitch(opt_f, text="Extrinsic Cal", variable=extr_var).pack(
            side="left"
        )
        self._cal_w["run_extrinsic"] = extr_var

        cal_btn_f = ctk.CTkFrame(right, fg_color="transparent")
        cal_btn_f.pack(fill="x", padx=15, pady=10)

        self._run_cal_btn = ctk.CTkButton(
            cal_btn_f,
            text="Run Calibration",
            font=FONT_BODY,
            fg_color=CLR_PURPLE,
            hover_color=CLR_PURPLE_H,
            height=42,
            width=200,
            command=self._run_multicam_calibration,
        )
        self._run_cal_btn.pack(side="left")

    # --- Console ---

    def _build_console(self):
        self._console_frame = ctk.CTkFrame(self)
        self._console_frame.grid(row=2, column=0, padx=10, pady=(0, 10), sticky="nsew")

        hdr = ctk.CTkFrame(self._console_frame, fg_color="transparent", height=36)
        hdr.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkLabel(hdr, text="Console Output", font=FONT_BODY).pack(side="left")
        ctk.CTkButton(
            hdr, text="Clear", width=80, height=30, font=FONT_SMALL,
            command=self._clear_console
        ).pack(side="right")

        self._console = ctk.CTkTextbox(self._console_frame, font=FONT_MONO, state="disabled")
        self._console.pack(fill="both", expand=True, padx=8, pady=8)

    def _toggle_console(self):
        """Show or hide the console panel."""
        if self._console_visible:
            self._console_frame.grid_forget()
            self._console_visible = False
            self._console_toggle_btn.configure(text="Show Console")
        else:
            self._console_frame.grid(row=2, column=0, padx=10, pady=(0, 10), sticky="nsew")
            self._console_visible = True
            self._console_toggle_btn.configure(text="Hide Console")

    # ==========================================================================
    #  Populate UI from Settings
    # ==========================================================================

    def _populate_ui(self):
        exp = self.settings.get("experiment", {})
        self._set_entry(self._exp_w["name"], exp.get("name", ""))
        self._set_entry(self._exp_w["output_dir"], exp.get("output_dir", "C:/Users/BarlabPRIME/desktop/Iris_Recorded_Taekwondo_Data"))
        self._exp_w["recording_format"].set(exp.get("recording_format", "mp4"))

        # Heart Rate
        hr = self.settings.get("heart_rate", {})
        self._hr_w["enabled"].set(1 if hr.get("enabled") else 0)
        addr = hr.get("device_address") or ""
        self._set_entry(self._hr_w["device_address"], addr)
        self._hr_w["ecg_enabled"].set(1 if hr.get("ecg_enabled") else 0)

        # Microphone
        mic = self.settings.get("microphone", {})
        self._mic_w["enabled"].set(1 if mic.get("enabled") else 0)
        self._set_entry(self._mic_w["device_name"], mic.get("device_name", "Tonor"))
        dev_idx = mic.get("device_index")
        self._set_entry(self._mic_w["device_index"], str(dev_idx) if dev_idx is not None else "")
        self._set_entry(self._mic_w["sample_rate"], str(mic.get("sample_rate", 44100)))
        self._set_entry(self._mic_w["channels"], str(mic.get("channels", 1)))

        # Calibration
        cal = self.settings.get("calibration", {})
        self._cal_w["run_intrinsic"].set(1 if cal.get("run_intrinsic", True) else 0)
        self._cal_w["run_extrinsic"].set(1 if cal.get("run_extrinsic", True) else 0)

        self._rebuild_cameras()
        self._rebuild_gopros()
        self._rebuild_phases()
        self._update_summary()
        self._rebuild_device_status()

    def _update_summary(self):
        s = self.settings
        cams = s.get("cameras", [])
        gps = s.get("gopros", [])
        hr = s.get("heart_rate", {})
        mic = s.get("microphone", {})
        phases = s.get("phases", [])

        cam_en = sum(1 for c in cams if c.get("enabled", True))
        gp_en = sum(1 for g in gps if g.get("enabled", True))
        hr_status = "Enabled" if hr.get("enabled") else "Disabled"
        mic_status = "Enabled" if mic.get("enabled") else "Disabled"

        text = (
            f"Cameras:    {len(cams)} configured ({cam_en} enabled)\n"
            f"GoPros:     {len(gps)} configured ({gp_en} enabled)\n"
            f"Heart Rate: {hr_status}\n"
            f"Microphone: {mic_status}\n"
            f"Phases:     {len(phases)} configured"
        )
        self._summary_label.configure(text=text)

    # --- Rebuild Device / Phase Cards ---

    def _rebuild_cameras(self):
        self._cam_cards.clear()
        for w in self._cam_scroll.winfo_children():
            w.destroy()

        for i, cam in enumerate(self.settings.get("cameras", [])):
            self._create_camera_card(self._cam_scroll, cam, i)

    def _create_camera_card(self, parent, cam, index):
        card = ctk.CTkFrame(parent)
        card.pack(fill="x", padx=2, pady=3)
        refs = {}

        # Row 1: enabled + name + role
        r1 = ctk.CTkFrame(card, fg_color="transparent")
        r1.pack(fill="x", padx=8, pady=(6, 2))

        en = ctk.IntVar(value=1 if cam.get("enabled", True) else 0)
        ctk.CTkSwitch(r1, text="", variable=en, width=40).pack(side="left")
        refs["enabled"] = en

        ctk.CTkLabel(r1, text="Name:", width=45, anchor="w").pack(side="left", padx=(5, 0))
        refs["name"] = self._inline_entry(r1, cam.get("name", ""), expand=True)

        ctk.CTkButton(
            r1,
            text="Remove",
            width=65,
            height=24,
            fg_color=CLR_RED,
            hover_color=CLR_RED_H,
            command=lambda idx=index: self._remove_camera(idx),
        ).pack(side="right", padx=(5, 0))

        # Row 2: id, index, resolution, fps, role
        r2 = ctk.CTkFrame(card, fg_color="transparent")
        r2.pack(fill="x", padx=8, pady=(0, 6))

        ctk.CTkLabel(r2, text="ID:", width=25, anchor="w").pack(side="left")
        refs["id"] = self._inline_entry(r2, cam.get("id", ""), width=80)

        ctk.CTkLabel(r2, text="Idx:", width=30, anchor="w").pack(side="left", padx=(8, 0))
        refs["device_index"] = self._inline_entry(
            r2, str(cam.get("device_index", 0)), width=35
        )

        ctk.CTkLabel(r2, text="Res:", width=30, anchor="w").pack(side="left", padx=(8, 0))
        res = cam.get("resolution", [1920, 1080])
        refs["res_w"] = self._inline_entry(r2, str(res[0]), width=50)
        ctk.CTkLabel(r2, text="x", width=12).pack(side="left")
        refs["res_h"] = self._inline_entry(r2, str(res[1]), width=50)

        ctk.CTkLabel(r2, text="FPS:", width=30, anchor="w").pack(side="left", padx=(8, 0))
        refs["fps"] = self._inline_entry(r2, str(cam.get("fps", 30)), width=35)

        ctk.CTkLabel(r2, text="Role:", width=35, anchor="w").pack(side="left", padx=(8, 0))
        role_var = ctk.StringVar(value=cam.get("role", "") or "")
        ctk.CTkOptionMenu(r2, variable=role_var, values=CAMERA_ROLES, width=90).pack(
            side="left"
        )
        refs["role"] = role_var

        self._cam_cards.append(refs)

    def _rebuild_gopros(self):
        self._gp_cards.clear()
        for w in self._gp_scroll.winfo_children():
            w.destroy()

        for i, gp in enumerate(self.settings.get("gopros", [])):
            self._create_gopro_card(self._gp_scroll, gp, i)

    def _create_gopro_card(self, parent, gp, index):
        card = ctk.CTkFrame(parent)
        card.pack(fill="x", padx=2, pady=3)
        refs = {}

        # Row 1: enabled + name
        r1 = ctk.CTkFrame(card, fg_color="transparent")
        r1.pack(fill="x", padx=8, pady=(6, 2))

        en = ctk.IntVar(value=1 if gp.get("enabled", True) else 0)
        ctk.CTkSwitch(r1, text="", variable=en, width=40).pack(side="left")
        refs["enabled"] = en

        ctk.CTkLabel(r1, text="Name:", width=45, anchor="w").pack(side="left", padx=(5, 0))
        refs["name"] = self._inline_entry(r1, gp.get("name", ""), expand=True)

        ctk.CTkButton(
            r1,
            text="Remove",
            width=65,
            height=24,
            fg_color=CLR_RED,
            hover_color=CLR_RED_H,
            command=lambda idx=index: self._remove_gopro(idx),
        ).pack(side="right", padx=(5, 0))

        # Row 2: id, model
        r2 = ctk.CTkFrame(card, fg_color="transparent")
        r2.pack(fill="x", padx=8, pady=(0, 2))

        ctk.CTkLabel(r2, text="ID:", width=25, anchor="w").pack(side="left")
        refs["id"] = self._inline_entry(r2, gp.get("id", ""), width=140)

        ctk.CTkLabel(r2, text="Model:", width=50, anchor="w").pack(
            side="left", padx=(10, 0)
        )
        model_var = ctk.StringVar(value=gp.get("model", GOPRO_MODELS[0]))
        ctk.CTkOptionMenu(r2, variable=model_var, values=GOPRO_MODELS, width=140).pack(
            side="left"
        )
        refs["model"] = model_var

        # Row 3: wifi, ip
        r3 = ctk.CTkFrame(card, fg_color="transparent")
        r3.pack(fill="x", padx=8, pady=(0, 6))

        ctk.CTkLabel(r3, text="WiFi:", width=38, anchor="w").pack(side="left")
        refs["wifi_interface"] = self._inline_entry(
            r3, gp.get("wifi_interface", ""), width=120
        )

        ctk.CTkLabel(r3, text="IP:", width=25, anchor="w").pack(
            side="left", padx=(10, 0)
        )
        refs["ip_address"] = self._inline_entry(
            r3, gp.get("ip_address", "10.5.5.9"), width=120
        )

        self._gp_cards.append(refs)

    def _rebuild_phases(self):
        self._phase_cards.clear()
        for w in self._phase_scroll.winfo_children():
            w.destroy()

        for i, phase in enumerate(self.settings.get("phases", [])):
            self._create_phase_card(self._phase_scroll, phase, i)

    def _create_phase_card(self, parent, phase, index):
        card = ctk.CTkFrame(parent)
        card.pack(fill="x", padx=2, pady=3)
        refs = {"_index": index}

        # Row 1: phase number, name, remove
        r1 = ctk.CTkFrame(card, fg_color="transparent")
        r1.pack(fill="x", padx=8, pady=(6, 2))

        ctk.CTkLabel(r1, text=f"Phase {index + 1}", font=FONT_BODY, width=65).pack(
            side="left"
        )
        ctk.CTkLabel(r1, text="Name:", width=45, anchor="w").pack(side="left")
        refs["name"] = self._inline_entry(r1, phase.get("name", ""), expand=True)

        ctk.CTkButton(
            r1,
            text="Remove",
            width=65,
            height=24,
            fg_color=CLR_RED,
            hover_color=CLR_RED_H,
            command=lambda idx=index: self._remove_phase(idx),
        ).pack(side="right", padx=(5, 0))

        # Row 2: id, duration, capture interval
        r2 = ctk.CTkFrame(card, fg_color="transparent")
        r2.pack(fill="x", padx=8, pady=(0, 2))

        ctk.CTkLabel(r2, text="ID:", width=25, anchor="w").pack(side="left")
        refs["id"] = self._inline_entry(r2, phase.get("id", ""), width=130)

        ctk.CTkLabel(r2, text="Duration (s):", width=90, anchor="w").pack(
            side="left", padx=(10, 0)
        )
        refs["duration"] = self._inline_entry(
            r2, str(phase.get("duration_seconds", 0)), width=60
        )

        ctk.CTkLabel(r2, text="Capture (ms):", width=95, anchor="w").pack(
            side="left", padx=(10, 0)
        )
        interval = phase.get("capture_interval_ms")
        refs["interval"] = self._inline_entry(
            r2, str(interval) if interval is not None else "", width=60
        )

        # Row 3: instructions
        r3 = ctk.CTkFrame(card, fg_color="transparent")
        r3.pack(fill="x", padx=8, pady=(0, 6))

        ctk.CTkLabel(r3, text="Instructions:", anchor="w").pack(anchor="w")
        instr = ctk.CTkTextbox(r3, height=50, font=FONT_SMALL)
        instr.pack(fill="x", pady=(2, 0))
        instr.insert("1.0", phase.get("instructions", ""))
        refs["instructions"] = instr

        self._phase_cards.append(refs)

    # --- Add / Remove Devices & Phases ---

    def _add_camera(self):
        self._collect_from_ui()
        n = len(self.settings["cameras"])
        self.settings["cameras"].append(
            {
                "id": f"usb_{n}",
                "name": f"Camera {n + 1}",
                "device_index": n,
                "resolution": [1920, 1080],
                "fps": 30,
                "enabled": True,
                "role": "",
            }
        )
        self._rebuild_cameras()
        self._update_summary()

    def _remove_camera(self, index):
        self._collect_from_ui()
        if 0 <= index < len(self.settings["cameras"]):
            self.settings["cameras"].pop(index)
        self._rebuild_cameras()
        self._update_summary()

    def _add_gopro(self):
        self._collect_from_ui()
        n = len(self.settings["gopros"])
        self.settings["gopros"].append(
            {
                "id": f"gopro_{n + 1}",
                "name": f"GoPro {n + 1}",
                "model": "hero7_silver",
                "wifi_interface": f"Wi-Fi {n + 2}",
                "ip_address": "10.5.5.9",
                "enabled": True,
            }
        )
        self._rebuild_gopros()
        self._update_summary()

    def _remove_gopro(self, index):
        self._collect_from_ui()
        if 0 <= index < len(self.settings["gopros"]):
            self.settings["gopros"].pop(index)
        self._rebuild_gopros()
        self._update_summary()

    def _add_phase(self):
        self._collect_from_ui()
        n = len(self.settings["phases"])
        self.settings["phases"].append(
            {
                "id": f"phase_{n + 1}",
                "name": f"Phase {n + 1}",
                "duration_seconds": 0,
                "capture_interval_ms": None,
                "instructions": "",
                "record_video": False,
                "record_audio": False,
                "record_gopro": False,
                "allow_pause": False,
                "cameras": [],
            }
        )
        self._rebuild_phases()
        self._update_summary()

    def _remove_phase(self, index):
        self._collect_from_ui()
        if 0 <= index < len(self.settings["phases"]):
            self.settings["phases"].pop(index)
        self._rebuild_phases()
        self._update_summary()

    # ==========================================================================
    #  Video Player Controls
    # ==========================================================================

    def _show_video_player(self, allow_pause=True, message="", title="Video Player"):
        """Show the video player panel in the experiment tab."""
        self._video_player_visible = True
        self._video_allow_pause = allow_pause
        self._video_first_play = True
        self._video_playing = False
        self._countdown_active = False
        self._vp_phase_title = title

        # Switch to Experiment tab and hide all other panels
        self.tabview.set("Experiment")
        self._exp_left.grid_forget()
        self._exp_right.grid_forget()
        self._phase_display.grid_forget()
        self._experiment_tab.grid_rowconfigure(0, weight=1)
        self._experiment_tab.grid_rowconfigure(1, weight=0)

        # Hide console to give video maximum space
        self._console_was_visible_before_video = self._console_visible
        if self._console_visible:
            self._toggle_console()

        self._vp_frame.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky="nsew")
        self._vp_title_label.configure(text=title)
        self._vp_message.configure(text=message)
        # Clear any leftover video frame and show phase-specific instructional text
        self._vp_canvas.configure(image=None)
        if title == "Narrating Review":
            self._vp_canvas.configure(
                text="You will now narrate\nyour own performance",
                font=("Segoe UI", 60, "bold"),
            )
        elif title == "Self-Scoring":
            self._vp_canvas.configure(
                text="You will now score\nyour own performance",
                font=("Segoe UI", 60, "bold"),
            )
        else:
            self._vp_canvas.configure(text="Press Play to begin", font=FONT_BODY)
        # Configure play/pause button
        start_label = "\u25B6  Start" if not allow_pause else "\u25B6  Play"
        self._vp_playpause_btn.configure(
            text=start_label, state="normal",
            fg_color=CLR_GREEN, hover_color=CLR_GREEN_H,
        )
        self._vp_rec_frame.pack_forget()  # Hide recording indicator initially

        # Update device indicators based on current settings
        s = self.settings
        face_active = any(
            c.get("enabled", True) and c.get("role") == "face"
            for c in s.get("cameras", [])
        )
        hr_active = s.get("heart_rate", {}).get("enabled", False)
        mic_active = s.get("microphone", {}).get("enabled", False)
        self._set_vp_indicator(self._vp_ind_face, face_active)
        self._set_vp_indicator(self._vp_ind_hr, hr_active)
        self._set_vp_indicator(self._vp_ind_mic, mic_active)

        # Bind spacebar for play/pause toggle
        self.bind("<space>", self._vp_space_handler)

    def _hide_video_player(self):
        """Hide the video player panel and restore the appropriate layout."""
        self._video_player_visible = False
        self._video_first_play = True
        self._video_playing = False
        self._countdown_active = False
        self._vp_frame.grid_forget()
        self._vp_canvas.configure(text="No video", image=None, font=FONT_BODY)
        self.unbind("<space>")

        # Restore console if it was visible before video
        if getattr(self, '_console_was_visible_before_video', False) and not self._console_visible:
            self._toggle_console()

        # Restore the correct layout depending on whether experiment is running
        if self._experiment_layout_active:
            self._show_experiment_layout()
        else:
            self._experiment_tab.grid_rowconfigure(0, weight=1)
            self._experiment_tab.grid_rowconfigure(1, weight=0)
            self._exp_left.grid(row=0, column=0, padx=(0, 8), pady=8, sticky="nsew")
            self._exp_right.grid(row=0, column=1, padx=(8, 0), pady=8, sticky="nsew")

    def _show_camera_selection(self, cameras, frames):
        """Show the camera selection panel with preview images."""
        import cv2

        self.tabview.set("Experiment")
        self._phase_display.grid_forget()
        self._exp_left.grid_forget()
        self._exp_right.grid_forget()

        self._experiment_tab.grid_columnconfigure(0, weight=1)
        self._experiment_tab.grid_columnconfigure(1, weight=0)
        self._experiment_tab.grid_rowconfigure(0, weight=1)

        self._cs_frame.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky="nsew")

        for i, cam_info in enumerate(cameras):
            if i >= 2:
                break
            card = self._cs_cards[i]
            cam_id = cam_info["id"]
            card["camera_id"] = cam_id
            card["name_label"].configure(text=cam_info["name"])
            card["dev_label"].configure(text=f"Device index: {cam_info['device_index']}")

            # Render preview frame
            frame = frames.get(cam_id)
            if frame is not None and PILImage is not None:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = PILImage.fromarray(rgb)
                # Scale to ~480px wide
                w, h = img.size
                scale = 480 / w if w > 0 else 1
                new_w, new_h = int(w * scale), int(h * scale)
                img = img.resize((new_w, new_h), PILImage.BILINEAR)
                ctk_img = ctk.CTkImage(light_image=img, dark_image=img,
                                       size=(new_w, new_h))
                card["img_label"].configure(image=ctk_img, text="")
                card["img_label"]._ctk_image = ctk_img  # prevent GC

            # Wire buttons — single click assigns BOTH roles
            card["overhead_btn"].configure(
                command=lambda cid=cam_id: self._on_camera_role_select(cid, "overhead")
            )
            card["face_btn"].configure(
                command=lambda cid=cam_id: self._on_camera_role_select(cid, "face")
            )

        # Store camera list for role resolution
        self._cs_camera_ids = [c["id"] for c in cameras[:2]]

    def _hide_camera_selection(self):
        """Hide the camera selection panel and restore experiment layout."""
        self._cs_frame.grid_forget()
        # Clear preview images
        for card in self._cs_cards:
            card["img_label"].configure(image=None, text="No preview")
            card["camera_id"] = None

        if self._experiment_layout_active:
            self._show_experiment_layout()

    def _on_camera_role_select(self, clicked_cam_id, selected_role):
        """Handle camera role button click. Assigns both roles from single click."""
        other_role = "face" if selected_role == "overhead" else "overhead"
        other_cam_id = None
        for cid in self._cs_camera_ids:
            if cid != clicked_cam_id:
                other_cam_id = cid
                break

        role_map = {clicked_cam_id: selected_role}
        if other_cam_id:
            role_map[other_cam_id] = other_role

        self._user_action_queue.put({
            "type": "camera_role_selection",
            "role_map": role_map,
        })

    def _show_experiment_layout(self):
        """Switch to running-experiment layout: device status on left, phase display center."""
        self._experiment_layout_active = True

        # Hide settings fields and separator, show only device status
        self._exp_settings_frame.pack_forget()
        self._device_status_separator.pack_forget()

        # Reconfigure grid: narrow left column for devices, wide center for phase
        self._experiment_tab.grid_columnconfigure(0, weight=0, minsize=350)
        self._experiment_tab.grid_columnconfigure(1, weight=1)

        # Hide the actions panel
        self._exp_right.grid_forget()

        # Show left panel (device status only) and center phase display
        self._exp_left.grid(row=0, column=0, padx=(0, 8), pady=8, sticky="nsew")
        self._phase_display.grid(row=0, column=1, padx=(8, 0), pady=8, sticky="nsew")

    def _restore_default_layout(self):
        """Restore the default pre-experiment layout with settings and actions."""
        self._experiment_layout_active = False

        # Stop any recording animation
        self._stop_rec_animation()
        self._phase_status_label.configure(text="")

        # Clean up any choice buttons that may still be visible
        self._hide_done_choice_buttons()
        self._phase_display_redo.pack_forget()
        self._phase_display_redo.pack(fill="x", pady=(5, 0))

        # Rebuild left panel pack order exactly as built
        self._exp_settings_frame.pack_forget()
        self._device_status_separator.pack_forget()
        self._device_status_header.pack_forget()
        self._device_status_frame.pack_forget()
        self._checklist_separator.pack_forget()
        self._checklist_header.pack_forget()
        self._checklist_frame.pack_forget()

        self._exp_settings_frame.pack(fill="x")
        self._device_status_separator.pack(fill="x", padx=25, pady=(20, 0))
        self._device_status_header.pack(anchor="w", padx=25, pady=(14, 10))
        self._device_status_frame.pack(fill="both", expand=True, padx=25, pady=(0, 15))

        # Restore grid weights
        self._experiment_tab.grid_columnconfigure(0, weight=1, minsize=0)
        self._experiment_tab.grid_columnconfigure(1, weight=1)

        # Hide phase display, show both panels
        self._phase_display.grid_forget()
        self._exp_left.grid(row=0, column=0, padx=(0, 8), pady=8, sticky="nsew")
        self._exp_right.grid(row=0, column=1, padx=(8, 0), pady=8, sticky="nsew")

    def _vp_toggle_playpause(self):
        """Single button toggles between play and pause."""
        if self._countdown_active:
            return
        if self._video_first_play:
            # First press: run 3-2-1 countdown before starting
            self._video_first_play = False
            self._countdown_active = True
            self._vp_playpause_btn.configure(state="disabled")
            self._run_countdown(3)
        elif self._video_playing:
            # Pause
            if self._video_allow_pause:
                self._user_action_queue.put({"type": "pause"})
                self._video_playing = False
                self._vp_playpause_btn.configure(
                    text="\u25B6  Play",
                    fg_color=CLR_GREEN, hover_color=CLR_GREEN_H,
                )
        else:
            # Resume
            self._user_action_queue.put({"type": "play"})
            self._video_playing = True
            self._vp_playpause_btn.configure(
                text="\u23F8  Pause",
                fg_color=CLR_ORANGE, hover_color=CLR_ORANGE_H,
            )

    def _vp_space_handler(self, event=None):
        """Spacebar toggles play/pause when video player is visible."""
        if not self._video_player_visible or self._countdown_active:
            return
        focused = self.focus_get()
        if isinstance(focused, (ctk.CTkEntry, ctk.CTkTextbox)):
            return
        self._vp_toggle_playpause()

    def _run_countdown(self, count):
        """Show 3-2-1 countdown on the video canvas, then start playback."""
        if count > 0:
            self._vp_canvas.configure(
                text=str(count), image=None, font=FONT_COUNTDOWN,
            )
            self.after(1000, lambda: self._run_countdown(count - 1))
        else:
            # Countdown finished — start video
            self._vp_canvas.configure(text="", font=FONT_BODY)
            self._countdown_active = False
            self._video_playing = True
            self._user_action_queue.put({"type": "play"})
            if self._video_allow_pause:
                self._vp_playpause_btn.configure(
                    text="\u23F8  Pause", state="normal",
                    fg_color=CLR_ORANGE, hover_color=CLR_ORANGE_H,
                )
            else:
                # Scoring mode: no controls once started
                self._vp_playpause_btn.configure(state="disabled")

    def _vp_continue(self):
        """User clicked Continue in video player (advance to next phase)."""
        self._user_action_queue.put({"type": "continue"})
        self._hide_continue_btn()

    def _on_continue(self):
        """User clicked the main Continue button (advance to next phase)."""
        self._user_action_queue.put({"type": "continue"})
        self._hide_continue_btn()

    def _on_end_experiment(self):
        """User chose to end the experiment (skip post-hoc calibration)."""
        self._hide_done_choice_buttons()
        self._user_action_queue.put({"type": "end_experiment"})

    def _on_continue_posthoc(self):
        """User chose to go to Calibration tab after ending experiment."""
        self._hide_done_choice_buttons()
        self._user_action_queue.put({"type": "continue_posthoc"})
        # Switch to Calibration tab after experiment ends
        self.after(1000, lambda: self.tabview.set("Calibration"))

    def _show_done_choice_buttons(self):
        """Show the end/posthoc choice buttons on the phase display."""
        # If checklist items exist and not all checked, show buttons disabled
        if self._checklist_vars and not all(v.get() for v in self._checklist_vars):
            self._done_end_btn.configure(state="disabled")
            self._done_posthoc_btn.configure(state="disabled")
        else:
            self._done_end_btn.configure(state="normal")
            self._done_posthoc_btn.configure(state="normal")
        self._done_end_btn.pack(fill="x", pady=(5, 5))
        self._done_posthoc_btn.pack(fill="x", pady=(5, 0))

    def _hide_done_choice_buttons(self):
        """Hide the end/posthoc choice buttons."""
        self._done_end_btn.pack_forget()
        self._done_posthoc_btn.pack_forget()

    def _update_checklist(self, phase_id):
        """Populate checklist for the given phase, or hide if none needed."""
        # Clear old checkboxes
        for w in self._checklist_frame.winfo_children():
            w.destroy()
        self._checklist_vars = []

        items = _PHASE_CHECKLISTS.get(phase_id, [])
        if not items:
            self._checklist_separator.pack_forget()
            self._checklist_header.pack_forget()
            self._checklist_frame.pack_forget()
            return

        # Show checklist section
        self._checklist_separator.pack(fill="x", padx=25, pady=(10, 0))
        self._checklist_header.pack(anchor="w", padx=25, pady=(8, 4))
        self._checklist_frame.pack(fill="x", padx=25, pady=(0, 10))

        for text in items:
            var = ctk.IntVar(value=0)
            cb = ctk.CTkCheckBox(
                self._checklist_frame,
                text=text,
                font=FONT_BODY,
                variable=var,
                command=self._on_checklist_toggle,
            )
            cb.pack(anchor="w", pady=2)
            self._checklist_vars.append(var)

    def _on_checklist_toggle(self):
        """Re-evaluate whether all checklist items are checked and update gated buttons."""
        all_checked = all(v.get() for v in self._checklist_vars)
        state = "normal" if all_checked else "disabled"
        self._continue_btn.configure(state=state)
        self._phase_display_continue.configure(state=state)
        self._done_end_btn.configure(state=state)
        self._done_posthoc_btn.configure(state=state)

    def _show_continue_btn(self, label=None):
        """Show Continue buttons with optional label (both phase display and actions panel)."""
        if label:
            self._continue_btn.configure(text=label)
            self._phase_display_continue.configure(text=label)
        # If checklist items exist and not all checked, show button disabled
        if self._checklist_vars and not all(v.get() for v in self._checklist_vars):
            self._continue_btn.configure(state="disabled")
            self._phase_display_continue.configure(state="disabled")
        else:
            self._continue_btn.configure(state="normal")
            self._phase_display_continue.configure(state="normal")
        self._continue_btn.pack(fill="x", pady=(10, 0))
        # Pack continue above stop in the phase display button frame
        self._phase_display_continue.pack(fill="x", pady=(0, 5), before=self._phase_display_redo)

    def _hide_continue_btn(self):
        """Hide Continue buttons."""
        self._continue_btn.pack_forget()
        self._phase_display_continue.pack_forget()

    def _update_video_frame(self, frame):
        """Display a video frame in the player canvas, scaled to fit.

        Frames are pre-downscaled by the experiment thread, so only a
        lightweight resize to match canvas dimensions is needed here.
        """
        if PILImage is None:
            return
        try:
            import cv2
            # BGR -> RGB
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = PILImage.fromarray(rgb)

            # Get actual canvas dimensions
            disp_w = self._vp_canvas.winfo_width()
            disp_h = self._vp_canvas.winfo_height()
            if disp_w < 50 or disp_h < 50:
                disp_w, disp_h = 960, 540

            # Maintain aspect ratio
            src_h, src_w = frame.shape[:2]
            scale = min(disp_w / src_w, disp_h / src_h)
            new_w = max(1, int(src_w * scale))
            new_h = max(1, int(src_h * scale))

            # Only resize if dimensions differ meaningfully (>5px)
            if abs(new_w - src_w) > 5 or abs(new_h - src_h) > 5:
                img = img.resize((new_w, new_h), PILImage.BILINEAR)

            ctk_img = ctk.CTkImage(light_image=img, dark_image=img,
                                   size=(new_w, new_h))
            self._vp_canvas.configure(image=ctk_img, text="")
            self._vp_canvas._ctk_image = ctk_img  # prevent GC
        except Exception as e:
            print(f"Video frame render error: {e}")

    def _update_video_time(self, position_sec, duration_sec):
        """Update the video time display."""
        pos_m, pos_s = divmod(int(position_sec), 60)
        dur_m, dur_s = divmod(int(duration_sec), 60)
        self._vp_time_label.configure(text=f"{pos_m}:{pos_s:02d} / {dur_m}:{dur_s:02d}")

    # ==========================================================================
    #  Actions (Run Experiment / Calibrate / Undistort / Mic Test)
    # ==========================================================================

    def _run_experiment(self):
        if self._is_running:
            return
        if not self._calibration_done:
            messagebox.showwarning("Calibrate First",
                                   "Please run Calibrate Devices before starting the experiment.")
            return
        self._save_settings()
        self._clear_console()
        self._set_running(True)
        self._progress_label.configure(text="Starting experiment...")
        self._progress_bar.set(0)

        # Switch to running layout: device status left, phase display center
        self._show_experiment_layout()

        # Clear event queues
        while not self._gui_event_queue.empty():
            try:
                self._gui_event_queue.get_nowait()
            except queue.Empty:
                break
        while not self._user_action_queue.empty():
            try:
                self._user_action_queue.get_nowait()
            except queue.Empty:
                break

        def worker():
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = OutputRedirector(self.output_queue, "stdout")
            sys.stderr = OutputRedirector(self.output_queue, "stderr")
            try:
                from src.experiment import Experiment

                exp = Experiment(
                    self.settings,
                    gui_event_queue=self._gui_event_queue,
                    user_action_queue=self._user_action_queue,
                    gopro_mode=self._gopro_mode,
                )
                self._active_experiment = exp
                exp.run()
            except KeyboardInterrupt:
                pass
            except Exception as e:
                self.output_queue.put(("stderr", f"ERROR: {e}"))
                import traceback
                self.output_queue.put(("stderr", traceback.format_exc()))
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr
                self._active_experiment = None
                self.after(0, lambda: self._set_running(False))
                self.after(0, lambda: self._progress_label.configure(text="Experiment finished."))
                self.after(0, self._hide_video_player)
                self.after(0, self._restore_default_layout)
                self.after(500, self._save_console_log)

        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    def _run_calibrate(self):
        if self._is_running:
            return
        self._save_settings()
        self._clear_console()
        self._set_running(True)
        self._progress_label.configure(text="Running device calibration...")

        # Refresh device list and set all to "checking"
        self._rebuild_device_status()
        self._set_all_devices_checking()

        def worker():
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = OutputRedirector(self.output_queue, "stdout")
            sys.stderr = OutputRedirector(self.output_queue, "stderr")
            try:
                from src.calibrate import CalibrationTool

                tool = CalibrationTool(self.settings)
                passed = tool.run()

                # Push calibration results to the device status panel
                cal_results = list(tool._results)
                self.after(0, lambda: self._apply_calibration_results(cal_results))

                # GoPros always run in manual mode
                has_gopros = any(g.get("enabled", True)
                                for g in self.settings.get("gopros", []))
                if has_gopros:
                    self.after(0, lambda: self._set_gopro_mode("manual"))
                    self.after(0, lambda: self._update_gopro_status("manual"))

                status = "All devices ready!" if passed else "Calibration complete (some devices failed)."
                self.output_queue.put(("stdout", f"\nResult: {status}"))

                # Enable Run Experiment
                self.after(0, self._on_calibration_done)

            except Exception as e:
                self.output_queue.put(("stderr", f"ERROR: {e}"))
                import traceback
                self.output_queue.put(("stderr", traceback.format_exc()))
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr
                self.after(0, lambda: self._set_running(False))
                self.after(0, lambda: self._progress_label.configure(text="Calibration finished."))

        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    def _set_gopro_mode(self, mode):
        """Update the GoPro mode and refresh the indicator on the settings bar."""
        self._gopro_mode = mode
        self._mode_indicator.configure(
            text="  GoPro: MANUAL  ", text_color=CLR_ORANGE,
        )

    def _on_calibration_done(self):
        """Enable Run Experiment after calibration has been run."""
        self._calibration_done = True
        self._run_btn.configure(text="Run Experiment", state="normal")

    def _run_undistort(self):
        if self._is_running:
            return
        input_path = self._cal_w["input"].get().strip()
        if not input_path:
            messagebox.showwarning("No Input", "Please select a video file or folder.")
            return

        target = Path(input_path)
        if not target.exists():
            messagebox.showerror("Not Found", f"Path does not exist:\n{input_path}")
            return

        self._clear_console()
        self._set_running(True)
        self._progress_label.configure(text="Processing videos...")

        def worker():
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = OutputRedirector(self.output_queue, "stdout")
            sys.stderr = OutputRedirector(self.output_queue, "stderr")
            try:
                from src.len_correction import process_directory, undistort_video

                if target.is_dir():
                    process_directory(str(target))
                elif target.is_file():
                    out = target.parent / f"{target.stem}_undistorted{target.suffix}"
                    undistort_video(str(target), str(out))
                else:
                    self.output_queue.put(("stderr", f"Invalid path: {input_path}"))
            except Exception as e:
                self.output_queue.put(("stderr", f"ERROR: {e}"))
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr
                self.after(0, lambda: self._set_running(False))
                self.after(0, lambda: self._progress_label.configure(text="Processing complete."))

        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    def _run_multicam_calibration(self):
        """Run multi-camera calibration on uploaded GoPro files."""
        if self._is_running:
            return

        files = []
        for entry in self._cal_w["gopro_files"]:
            path = entry.get().strip()
            if path and Path(path).exists():
                files.append(path)

        if len(files) < 1:
            messagebox.showwarning("No Files", "Please select at least one GoPro video file.")
            return

        self._clear_console()
        self._set_running(True)
        self._progress_label.configure(text="Running multi-camera calibration...")

        def worker():
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = OutputRedirector(self.output_queue, "stdout")
            sys.stderr = OutputRedirector(self.output_queue, "stderr")
            try:
                from src.extrinsic_calibration import calibrate_all, save_calibration_log

                result = calibrate_all(files)
                output_dir = Path(files[0]).parent
                save_calibration_log(result, str(output_dir / "calibration_log.json"))
                self.output_queue.put(("stdout", "\nCalibration complete!"))
            except Exception as e:
                self.output_queue.put(("stderr", f"ERROR: {e}"))
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr
                self.after(0, lambda: self._set_running(False))
                self.after(0, lambda: self._progress_label.configure(text="Calibration complete."))

        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    def _test_microphone(self):
        """Quick microphone test: record 1 second and check for signal."""
        if self._is_running:
            return

        self._clear_console()
        self._log("Testing microphone...")

        def worker():
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = OutputRedirector(self.output_queue, "stdout")
            sys.stderr = OutputRedirector(self.output_queue, "stderr")
            try:
                from src.audio import AudioConfig, AudioRecorder, find_audio_device
                import tempfile

                name = self._mic_w["device_name"].get().strip() or "Tonor"
                idx_str = self._mic_w["device_index"].get().strip()
                device_idx = int(idx_str) if idx_str else None

                if device_idx is None:
                    device_idx = find_audio_device(name)
                    if device_idx is None:
                        print(f"Microphone '{name}' not found!")
                        return
                    print(f"Found device: index {device_idx}")

                config = AudioConfig(
                    device_name=name,
                    device_index=device_idx,
                    sample_rate=44100,
                    channels=1,
                )
                recorder = AudioRecorder(config)
                tmp = tempfile.mktemp(suffix=".wav")
                if recorder.open(tmp):
                    recorder.start_recording()
                    time.sleep(1.0)
                    recorder.stop_recording()
                    recorder.close()

                    import os
                    size = os.path.getsize(tmp)
                    if size > 1000:
                        print(f"Microphone test PASSED ({size} bytes recorded)")
                    else:
                        print(f"Microphone test FAILED (only {size} bytes)")
                    os.unlink(tmp)
                else:
                    print("Failed to open microphone")
            except Exception as e:
                self.output_queue.put(("stderr", f"Mic test error: {e}"))
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr

        threading.Thread(target=worker, daemon=True).start()

    def _redo_last_phase(self):
        """User clicked Redo Last Phase — go back and re-run the previous phase."""
        if not self._is_running:
            return
        self._log("Redo last phase requested")
        self._progress_label.configure(text="Restarting previous phase...")
        self._user_action_queue.put({"type": "redo"})

    def _stop_experiment(self):
        if not self._is_running:
            return
        self._log("Stopping... (sending stop signal)")
        self._progress_label.configure(text="Stopping...")

        # Send stop via action queue first
        self._user_action_queue.put({"type": "stop"})

        # Fallback: async exception after a short delay
        def force_stop():
            if self._is_running and self._worker_thread is not None:
                try:
                    thread_id = self._worker_thread.ident
                    if thread_id is not None:
                        ctypes.pythonapi.PyThreadState_SetAsyncExc(
                            ctypes.c_ulong(thread_id), ctypes.py_object(KeyboardInterrupt)
                        )
                except Exception as e:
                    self._log(f"Stop error: {e}")

        self.after(2000, force_stop)

    def _set_running(self, running):
        self._is_running = running
        state_on = "normal"
        state_off = "disabled"
        if running:
            self._run_btn.configure(state=state_off)
            self._cal_btn.configure(state=state_off)
            self._undist_btn.configure(state=state_off)
            self._run_cal_btn.configure(state=state_off)
            self._redo_btn.configure(state=state_on)
            self._status_label.configure(text="Running...", text_color=CLR_GREEN)
        else:
            # Only re-enable Run Experiment if calibration has been done
            run_state = state_on if self._calibration_done else state_off
            self._run_btn.configure(state=run_state)
            self._cal_btn.configure(state=state_on)
            self._undist_btn.configure(state=state_on)
            self._run_cal_btn.configure(state=state_on)
            self._redo_btn.configure(state=state_off)
            self._status_label.configure(text="Ready", text_color="white")
            self._hide_continue_btn()

    # ==========================================================================
    #  Device Status
    # ==========================================================================

    def _rebuild_device_status(self):
        """Rebuild the device status rows from current settings."""
        for w in self._device_status_frame.winfo_children():
            w.destroy()
        self._device_status_rows = []

        s = self.settings

        for cam in s.get("cameras", []):
            enabled = cam.get("enabled", True)
            self._add_device_row(
                cam.get("name", "Camera"), "camera", cam,
                "disabled" if not enabled else "unknown",
            )

        for gp in s.get("gopros", []):
            enabled = gp.get("enabled", True)
            self._add_device_row(
                gp.get("name", "GoPro"), "gopro", gp,
                "disabled" if not enabled else "unknown",
            )

        hr = s.get("heart_rate", {})
        self._add_device_row(
            "Polar H10", "heart_rate", hr,
            "disabled" if not hr.get("enabled") else "unknown",
        )

        mic = s.get("microphone", {})
        self._add_device_row(
            mic.get("device_name", "Microphone"), "microphone", mic,
            "disabled" if not mic.get("enabled") else "unknown",
        )

    def _add_device_row(self, name, dev_type, config, initial_status):
        """Add a single device status row to the panel."""
        row = ctk.CTkFrame(self._device_status_frame, fg_color="gray20", corner_radius=8)
        row.pack(fill="x", pady=4, ipady=6)

        dot = ctk.CTkLabel(row, text="\u25cf", font=("Segoe UI", 22), width=30)
        dot.pack(side="left", padx=(12, 0))

        # Device type tag
        type_labels = {
            "camera": "CAM",
            "gopro": "GP",
            "heart_rate": "HR",
            "microphone": "MIC",
        }
        tag = type_labels.get(dev_type, "")
        tag_lbl = ctk.CTkLabel(
            row, text=tag, font=("Segoe UI", 13, "bold"), width=42,
            text_color="gray55", anchor="w",
        )
        tag_lbl.pack(side="left", padx=(6, 4))

        name_lbl = ctk.CTkLabel(row, text=name, font=FONT_BODY, anchor="w")
        name_lbl.pack(side="left", fill="x", expand=True)

        status_lbl = ctk.CTkLabel(row, text="", font=("Segoe UI", 16, "bold"), anchor="e")
        status_lbl.pack(side="right", padx=(0, 14))

        entry = {
            "dot": dot,
            "status_label": status_lbl,
            "type": dev_type,
            "config": config,
        }
        self._device_status_rows.append(entry)
        self._apply_single_status(entry, initial_status)

    def _apply_single_status(self, entry, status):
        """Set the visual status of a single device row."""
        status_map = {
            "pass": (CLR_GREEN, "Connected"),
            "fail": (CLR_RED, "Connection Failed"),
            "manual": (CLR_ORANGE, "Manual Mode"),
            "disabled": ("gray50", "Disabled"),
            "unknown": ("gray60", "\u2014"),
            "checking": (CLR_ORANGE, "Checking\u2026"),
        }
        color, text = status_map.get(status, ("gray60", status))
        entry["dot"].configure(text_color=color)
        entry["status_label"].configure(text=text, text_color=color)

    def _set_all_devices_checking(self):
        """Set all enabled device rows to 'checking' state."""
        for entry in self._device_status_rows:
            cfg = entry["config"]
            if entry["type"] == "heart_rate":
                if cfg.get("enabled"):
                    self._apply_single_status(entry, "checking")
            elif cfg.get("enabled", True):
                self._apply_single_status(entry, "checking")

    def _apply_calibration_results(self, cal_results):
        """Update device status panel from CalibrationTool._results.

        Each result dict has: device, type, status (PASS/FAIL/SKIP), detail.
        """
        # Map calibration type names to our internal types
        type_map = {
            "USB Camera": "camera",
            "GoPro": "gopro",
            "Heart Rate": "heart_rate",
            "Audio": "microphone",
        }

        # Track which rows we've matched so unmatched ones stay as-is
        matched = set()

        for r in cal_results:
            dev_type = type_map.get(r["type"])
            if dev_type is None:
                continue

            # Find the matching row by type and device name
            for i, entry in enumerate(self._device_status_rows):
                if i in matched:
                    continue
                if entry["type"] != dev_type:
                    continue
                # Match by name: the calibration result "device" should
                # be contained in or match the config name
                cfg_name = entry["config"].get("name",
                           entry["config"].get("device_name", ""))
                if (cfg_name and cfg_name in r["device"]) or \
                   r["device"] in cfg_name or \
                   entry["type"] == "heart_rate" or \
                   entry["type"] == "microphone":
                    matched.add(i)
                    self._apply_cal_result_to_row(entry, r)
                    break

    def _apply_cal_result_to_row(self, entry, result):
        """Apply a single calibration result to a device row."""
        status = result["status"]
        if status == "PASS":
            self._apply_single_status(entry, "pass")
        elif status == "FAIL":
            self._apply_single_status(entry, "fail")
        elif status == "SKIP":
            # GoPros in manual mode get orange "Manual Mode", others get gray "Disabled"
            if result.get("detail") == "manual mode":
                self._apply_single_status(entry, "manual")
            else:
                self._apply_single_status(entry, "disabled")

    def _update_gopro_status(self, gopro_status):
        """Update GoPro rows after a retry attempt.

        Args:
            gopro_status: "pass" or "manual"
        """
        for entry in self._device_status_rows:
            if entry["type"] == "gopro":
                cfg = entry["config"]
                if cfg.get("enabled", True):
                    self._apply_single_status(entry, gopro_status)

    # ==========================================================================
    #  GUI Event Polling (experiment -> GUI)
    # ==========================================================================

    def _poll_gui_events(self):
        """Process events from the experiment thread.

        Video frames are deduplicated: only the latest frame is rendered
        to prevent queue backup when the GUI can't keep up with FPS.
        """
        latest_video_frame = None
        try:
            while True:
                event = self._gui_event_queue.get_nowait()
                if event.get("type") == "video_frame":
                    # Keep only the latest video frame, skip stale ones
                    latest_video_frame = event
                else:
                    self._handle_gui_event(event)
        except queue.Empty:
            pass
        # Render only the most recent video frame
        if latest_video_frame is not None:
            self._handle_gui_event(latest_video_frame)
        self.after(33, self._poll_gui_events)

    def _handle_gui_event(self, event):
        """Handle a single event from the experiment."""
        etype = event.get("type")

        if etype == "phase_change":
            idx = event.get("phase_index", 0)
            total = event.get("total_phases", 1)
            name = event.get("phase_name", "")
            phase_id = event.get("phase_id", "")
            self._current_phase_index = idx
            self._phase_indicator.configure(
                text=f"Phase {idx + 1}/{total}: {name}"
            )
            progress = (idx + 1) / total if total > 0 else 0
            self._progress_bar.set(progress)
            self._phase_display_progress.set(progress)
            self._hide_continue_btn()
            self._update_checklist(phase_id)

            # Stop any running recording animation from previous phase
            self._stop_rec_animation()

            # Update center phase display
            phases = self.settings.get("phases", [])
            instructions = ""
            if idx < len(phases):
                instructions = phases[idx].get("instructions", "")
            self._phase_display_name.configure(text=name)
            self._phase_display_desc.configure(text=instructions)

            # Show prominent "IN PROGRESS" status
            self._phase_status_label.configure(
                text=f"\u25b6  PHASE IN PROGRESS  \u25b6",
                text_color=CLR_ORANGE,
            )

            # Pre-switch to video player layout for review/scoring phases
            # so the window is already resized before show_video_player fires
            if phase_id in ("review", "scoring"):
                self._exp_left.grid_forget()
                self._phase_display.grid_forget()
                self._vp_canvas.configure(image=None, text="", font=FONT_BODY)
                self._vp_frame.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky="nsew")
            else:
                # Restore normal experiment layout if coming from a video phase
                self._vp_frame.grid_forget()
                if self._experiment_layout_active:
                    self._exp_left.grid(row=0, column=0, padx=(0, 8), pady=8, sticky="nsew")
                    self._phase_display.grid(row=0, column=1, padx=(8, 0), pady=8, sticky="nsew")

        elif etype == "camera_selection":
            self._show_camera_selection(
                event.get("cameras", []),
                event.get("frames", {}),
            )

        elif etype == "hide_camera_selection":
            self._hide_camera_selection()

        elif etype == "show_video_player":
            self._show_video_player(
                allow_pause=event.get("allow_pause", True),
                message=event.get("message", ""),
                title=event.get("title", "Video Player"),
            )

        elif etype == "hide_video_player":
            self._hide_video_player()

        elif etype == "video_frame":
            # Discard frames while waiting for first play or during countdown
            # so the instructional text stays visible
            if self._video_first_play or self._countdown_active:
                return
            frame = event.get("frame")
            if frame is not None:
                self._update_video_frame(frame)
            pos = event.get("position_sec", 0)
            dur = event.get("duration_sec", 0)
            self._update_video_time(pos, dur)

        elif etype == "player_progress":
            pos = event.get("position_sec", 0)
            dur = event.get("duration_sec", 0)
            self._update_video_time(pos, dur)

        elif etype == "video_complete":
            self._vp_canvas.configure(text="Video complete", font=FONT_BODY)
            self._video_playing = False
            self._vp_playpause_btn.configure(state="disabled")

        elif etype == "wait_for_continue":
            msg = event.get("message", "Press Continue to proceed.")
            self._progress_label.configure(text=msg)
            # Update phase status to show waiting
            self._phase_status_label.configure(
                text="\u23f8  WAITING FOR CONTINUE  \u23f8",
                text_color=CLR_GREEN,
            )
            self._phase_display_desc.configure(text=msg)
            # Build "Continue to <next phase>" label
            phases = self.settings.get("phases", [])
            idx = getattr(self, "_current_phase_index", 0)
            next_idx = idx + 1
            if next_idx < len(phases):
                next_name = phases[next_idx].get("name", "Next Phase")
                btn_label = f"Continue to {next_name}"
            else:
                btn_label = "Finish Experiment"
            self._show_continue_btn(label=btn_label)

        elif etype == "recording_status":
            if event.get("recording"):
                self._start_rec_animation()
            else:
                self._stop_rec_animation()

        elif etype == "status":
            msg = event.get("message", "")
            self._progress_label.configure(text=msg)

        elif etype == "experiment_done_choice":
            self._hide_continue_btn()
            self._hide_video_player()
            self._stop_rec_animation()
            self._phase_status_label.configure(text="\u2705  COMPLETE", text_color=CLR_GREEN)
            # Show completion screen with two choices
            self._phase_display_name.configure(text="Experiment Complete")
            self._phase_display_desc.configure(
                text="Recording and post-processing finished.\nChoose what to do next."
            )
            self._phase_display_progress.set(1.0)
            self._phase_display_redo.pack_forget()
            self._show_done_choice_buttons()

        elif etype == "experiment_complete":
            self._progress_label.configure(text="Experiment complete!")
            self._phase_display_name.configure(text="Experiment Complete")
            self._phase_display_desc.configure(text="All phases finished successfully.")
            self._phase_display_progress.set(1.0)
            self._phase_status_label.configure(text="\u2705  COMPLETE", text_color=CLR_GREEN)
            self._stop_rec_animation()
            self._hide_continue_btn()
            self._hide_done_choice_buttons()
            self._hide_video_player()
            self._save_console_log()

    # ==========================================================================
    #  Console
    # ==========================================================================

    def _log(self, text):
        self._console.configure(state="normal")
        self._console.insert("end", text + "\n")
        self._console.see("end")
        self._console.configure(state="disabled")

    def _poll_console(self):
        try:
            while True:
                tag, text = self.output_queue.get_nowait()
                self._console.configure(state="normal")
                self._console.insert("end", text + "\n")
                self._console.see("end")
                self._console.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_console)

    def _clear_console(self):
        self._console.configure(state="normal")
        self._console.delete("1.0", "end")
        self._console.configure(state="disabled")

    def _save_console_log(self):
        """Save the console output to a log file in the session output directory."""
        text = self._console.get("1.0", "end").strip()
        if not text:
            return
        # Try to find the session directory from settings
        output_dir = self.settings.get("experiment", {}).get("output_dir", "C:/Users/BarlabPRIME/desktop/Iris_Recorded_Taekwondo_Data")
        output_path = Path(output_dir)
        if not output_path.is_absolute():
            output_path = Path(os.path.dirname(os.path.abspath(__file__))) / output_path
        # Find the most recent session directory (most recently modified subfolder)
        if output_path.exists():
            subdirs = [d for d in output_path.iterdir() if d.is_dir()]
            if subdirs:
                session_dir = max(subdirs, key=lambda d: d.stat().st_mtime)
            else:
                session_dir = output_path
        else:
            output_path.mkdir(parents=True, exist_ok=True)
            session_dir = output_path
        log_path = session_dir / "console_log.txt"
        try:
            log_path.write_text(text, encoding="utf-8")
            self._log(f"Console log saved to: {log_path}")
        except Exception as e:
            self._log(f"WARNING: Failed to save console log: {e}")

    # ==========================================================================
    #  Helpers
    # ==========================================================================

    def _labeled_entry(self, parent, label, default="", browse=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=25, pady=8)
        ctk.CTkLabel(row, text=label, width=140, anchor="w", font=FONT_BODY).pack(side="left")
        entry = ctk.CTkEntry(row, font=FONT_BODY, height=36)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        if default:
            entry.insert(0, default)

        if browse == "dir":
            ctk.CTkButton(
                row,
                text="Browse",
                width=90,
                height=36,
                font=FONT_SMALL,
                command=lambda: self._browse_dir(entry),
            ).pack(side="left")
        elif browse == "file_or_dir":
            ctk.CTkButton(
                row,
                text="File",
                width=70,
                height=36,
                font=FONT_SMALL,
                command=lambda: self._browse_file(entry),
            ).pack(side="left", padx=(0, 4))
            ctk.CTkButton(
                row,
                text="Folder",
                width=70,
                height=36,
                font=FONT_SMALL,
                command=lambda: self._browse_dir(entry),
            ).pack(side="left")

        return entry

    def _inline_entry(self, parent, default="", width=None, expand=False):
        entry = ctk.CTkEntry(parent, width=width) if width else ctk.CTkEntry(parent)
        if default:
            entry.insert(0, default)
        entry.pack(side="left", fill="x" if expand else "none", expand=expand)
        return entry

    @staticmethod
    def _set_entry(entry, value):
        entry.delete(0, "end")
        if value:
            entry.insert(0, str(value))

    def _browse_dir(self, entry):
        path = filedialog.askdirectory()
        if path:
            self._set_entry(entry, path)

    def _browse_file(self, entry):
        path = filedialog.askopenfilename(
            filetypes=[("MP4 Video", "*.mp4"), ("All files", "*.*")]
        )
        if path:
            self._set_entry(entry, path)

    def _on_close(self):
        if self._is_running:
            if not messagebox.askyesno(
                "Confirm Exit",
                "An operation is running. Stop and exit?",
            ):
                return
            self._stop_experiment()
            time.sleep(0.5)
        self.destroy()


# --- Entry Point --------------------------------------------------------------

if __name__ == "__main__":
    app = IrisApp()
    app.mainloop()
