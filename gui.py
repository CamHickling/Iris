#!/usr/bin/env python3
"""CaptureExpert - Desktop GUI Application.

Run directly:    python gui.py
From launcher:   double-click CaptureExpert.pyw
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

# --- Constants ----------------------------------------------------------------

APP_TITLE = "CaptureExpert"
DEFAULT_CONFIG = "settings.json"
WINDOW_SIZE = "1150x800"
MIN_SIZE = (950, 650)

FONT_HEADER = ("Segoe UI", 18, "bold")
FONT_SUB = ("Segoe UI", 14, "bold")
FONT_BODY = ("Segoe UI", 12)
FONT_SMALL = ("Segoe UI", 11)
FONT_MONO = ("Consolas", 11)

CLR_GREEN = "#2ecc71"
CLR_GREEN_H = "#27ae60"
CLR_BLUE = "#3498db"
CLR_BLUE_H = "#2980b9"
CLR_RED = "#e74c3c"
CLR_RED_H = "#c0392b"
CLR_ORANGE = "#e67e22"
CLR_ORANGE_H = "#d35400"

GOPRO_MODELS = ["hero7_silver", "hero5_session"]


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


class CaptureExpertApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title(APP_TITLE)
        self.geometry(WINDOW_SIZE)
        self.minsize(*MIN_SIZE)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # State
        self.settings = {}
        self.config_path = (
            Path(os.path.dirname(os.path.abspath(__file__))) / DEFAULT_CONFIG
        )
        self.output_queue = queue.Queue()
        self._worker_thread = None
        self._is_running = False

        # Widget references (populated in build methods)
        self._exp_w = {}
        self._cam_cards = []
        self._gp_cards = []
        self._hr_w = {}
        self._phase_cards = []
        self._undist_w = {}

        # Scrollable frame references (for rebuilding)
        self._cam_scroll = None
        self._gp_scroll = None
        self._phase_scroll = None

        self._load_settings()
        self._build_ui()
        self._populate_ui()
        self._poll_console()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
                    "name": "default_experiment",
                    "output_dir": "./output",
                    "save_format": "png",
                },
                "cameras": [],
                "gopros": [],
                "heart_rate": {
                    "enabled": False,
                    "device_address": None,
                    "ecg_enabled": False,
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
        self.settings["experiment"]["save_format"] = w["save_format"].get()

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

        # Phases
        phases = []
        for p in self._phase_cards:
            dur = p["duration"].get().strip()
            interval = p["interval"].get().strip()
            phases.append(
                {
                    "id": p["id"].get(),
                    "name": p["name"].get(),
                    "duration_seconds": int(dur) if dur else 0,
                    "capture_interval_ms": int(interval) if interval else None,
                    "instructions": p["instructions"].get("1.0", "end-1c"),
                }
            )
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
        self._build_undistort_tab(self.tabview.add("Undistort"))

        # Settings bar
        bar = ctk.CTkFrame(self, height=40)
        bar.grid(row=1, column=0, padx=10, pady=4, sticky="ew")

        ctk.CTkButton(
            bar, text="Save Settings", width=130, command=self._save_settings
        ).pack(side="left", padx=5, pady=5)
        ctk.CTkButton(
            bar, text="Load Settings", width=130, command=self._load_settings_dialog
        ).pack(side="left", padx=5, pady=5)

        self._status_label = ctk.CTkLabel(bar, text="Ready", font=FONT_BODY)
        self._status_label.pack(side="right", padx=15)

        # Console
        self._build_console()

    # --- Experiment Tab ---

    def _build_experiment_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_columnconfigure(1, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        # Left: settings
        left = ctk.CTkFrame(parent)
        left.grid(row=0, column=0, padx=(0, 5), pady=5, sticky="nsew")

        ctk.CTkLabel(left, text="Experiment Settings", font=FONT_SUB).pack(
            anchor="w", padx=15, pady=(15, 10)
        )

        self._exp_w["name"] = self._labeled_entry(left, "Name:", "experiment_name")
        self._exp_w["output_dir"] = self._labeled_entry(
            left, "Output Dir:", "./output", browse="dir"
        )

        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=15, pady=3)
        ctk.CTkLabel(row, text="Save Format:", width=100, anchor="w").pack(side="left")
        fmt = ctk.StringVar(value="png")
        ctk.CTkOptionMenu(row, variable=fmt, values=["png", "jpg", "bmp"], width=100).pack(
            side="left"
        )
        self._exp_w["save_format"] = fmt

        # Right: actions
        right = ctk.CTkFrame(parent)
        right.grid(row=0, column=1, padx=(5, 0), pady=5, sticky="nsew")

        ctk.CTkLabel(right, text="Actions", font=FONT_SUB).pack(
            anchor="w", padx=15, pady=(15, 5)
        )

        self._summary_label = ctk.CTkLabel(
            right, text="", font=FONT_SMALL, justify="left", anchor="w"
        )
        self._summary_label.pack(anchor="w", padx=15, pady=(0, 10))

        btn_f = ctk.CTkFrame(right, fg_color="transparent")
        btn_f.pack(fill="x", padx=15, pady=0)

        self._run_btn = ctk.CTkButton(
            btn_f,
            text="Run Experiment",
            font=FONT_BODY,
            fg_color=CLR_GREEN,
            hover_color=CLR_GREEN_H,
            height=45,
            command=self._run_experiment,
        )
        self._run_btn.pack(fill="x", pady=4)

        self._cal_btn = ctk.CTkButton(
            btn_f,
            text="Calibrate Devices",
            font=FONT_BODY,
            fg_color=CLR_BLUE,
            hover_color=CLR_BLUE_H,
            height=45,
            command=self._run_calibrate,
        )
        self._cal_btn.pack(fill="x", pady=4)

        self._stop_btn = ctk.CTkButton(
            btn_f,
            text="Stop",
            font=FONT_BODY,
            fg_color=CLR_RED,
            hover_color=CLR_RED_H,
            height=45,
            command=self._stop_experiment,
            state="disabled",
        )
        self._stop_btn.pack(fill="x", pady=4)

        # Progress section
        prog_f = ctk.CTkFrame(right, fg_color="transparent")
        prog_f.pack(fill="x", padx=15, pady=(15, 5))

        self._progress_label = ctk.CTkLabel(prog_f, text="", font=FONT_SMALL)
        self._progress_label.pack(anchor="w")

        self._progress_bar = ctk.CTkProgressBar(prog_f)
        self._progress_bar.pack(fill="x", pady=(5, 0))
        self._progress_bar.set(0)

    # --- Devices Tab ---

    def _build_devices_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_columnconfigure(1, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

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

        # Heart Rate (bottom, spans both columns)
        hr_frame = ctk.CTkFrame(parent)
        hr_frame.grid(
            row=1, column=0, columnspan=2, padx=0, pady=(3, 0), sticky="nsew"
        )

        ctk.CTkLabel(hr_frame, text="Heart Rate Monitor (Polar H10)", font=FONT_SUB).pack(
            anchor="w", padx=15, pady=(10, 5)
        )

        hr_row = ctk.CTkFrame(hr_frame, fg_color="transparent")
        hr_row.pack(fill="x", padx=15, pady=5)

        en_var = ctk.IntVar(value=0)
        ctk.CTkSwitch(hr_row, text="Enabled", variable=en_var).pack(
            side="left", padx=(0, 20)
        )
        self._hr_w["enabled"] = en_var

        ctk.CTkLabel(hr_row, text="Device Address:", anchor="w").pack(
            side="left", padx=(0, 5)
        )
        addr_e = ctk.CTkEntry(hr_row, width=200, placeholder_text="auto-scan if empty")
        addr_e.pack(side="left", padx=(0, 20))
        self._hr_w["device_address"] = addr_e

        ecg_var = ctk.IntVar(value=0)
        ctk.CTkSwitch(hr_row, text="ECG Recording", variable=ecg_var).pack(
            side="left"
        )
        self._hr_w["ecg_enabled"] = ecg_var

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
            text="Duration 0 = indefinite (wait for Stop).  Empty capture interval = no USB capture.",
            font=FONT_SMALL,
            text_color="gray",
        ).pack(side="left", padx=20)

        self._phase_scroll = ctk.CTkScrollableFrame(parent)
        self._phase_scroll.grid(row=1, column=0, padx=5, pady=5, sticky="nsew")

    # --- Undistort Tab ---

    def _build_undistort_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(parent, text="GoPro Lens Correction", font=FONT_SUB).pack(
            anchor="w", padx=15, pady=(15, 5)
        )
        ctk.CTkLabel(
            parent,
            text="Apply barrel distortion correction to GoPro video files.\n"
            "Select a single .mp4 file or a folder containing .mp4 files.",
            font=FONT_SMALL,
            text_color="gray",
            justify="left",
        ).pack(anchor="w", padx=15, pady=(0, 10))

        self._undist_w["input"] = self._labeled_entry(
            parent, "Input Path:", "", browse="file_or_dir"
        )

        btn_f = ctk.CTkFrame(parent, fg_color="transparent")
        btn_f.pack(fill="x", padx=15, pady=15)

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

    # --- Console ---

    def _build_console(self):
        frame = ctk.CTkFrame(self)
        frame.grid(row=2, column=0, padx=10, pady=(0, 10), sticky="nsew")

        hdr = ctk.CTkFrame(frame, fg_color="transparent", height=30)
        hdr.pack(fill="x", padx=5, pady=(5, 0))
        ctk.CTkLabel(hdr, text="Console Output", font=FONT_SMALL).pack(side="left")
        ctk.CTkButton(
            hdr, text="Clear", width=60, height=24, command=self._clear_console
        ).pack(side="right")

        self._console = ctk.CTkTextbox(frame, font=FONT_MONO, state="disabled")
        self._console.pack(fill="both", expand=True, padx=5, pady=5)

    # ==========================================================================
    #  Populate UI from Settings
    # ==========================================================================

    def _populate_ui(self):
        exp = self.settings.get("experiment", {})
        self._set_entry(self._exp_w["name"], exp.get("name", ""))
        self._set_entry(self._exp_w["output_dir"], exp.get("output_dir", "./output"))
        self._exp_w["save_format"].set(exp.get("save_format", "png"))

        # Heart Rate
        hr = self.settings.get("heart_rate", {})
        self._hr_w["enabled"].set(1 if hr.get("enabled") else 0)
        addr = hr.get("device_address") or ""
        self._set_entry(self._hr_w["device_address"], addr)
        self._hr_w["ecg_enabled"].set(1 if hr.get("ecg_enabled") else 0)

        self._rebuild_cameras()
        self._rebuild_gopros()
        self._rebuild_phases()
        self._update_summary()

    def _update_summary(self):
        s = self.settings
        cams = s.get("cameras", [])
        gps = s.get("gopros", [])
        hr = s.get("heart_rate", {})
        phases = s.get("phases", [])

        cam_en = sum(1 for c in cams if c.get("enabled", True))
        gp_en = sum(1 for g in gps if g.get("enabled", True))
        hr_status = "Enabled" if hr.get("enabled") else "Disabled"

        text = (
            f"Cameras:  {len(cams)} configured ({cam_en} enabled)\n"
            f"GoPros:   {len(gps)} configured ({gp_en} enabled)\n"
            f"Heart Rate: {hr_status}\n"
            f"Phases:   {len(phases)} configured"
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

        # Row 1: enabled + name
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

        # Row 2: id, index, resolution, fps
        r2 = ctk.CTkFrame(card, fg_color="transparent")
        r2.pack(fill="x", padx=8, pady=(0, 6))

        ctk.CTkLabel(r2, text="ID:", width=25, anchor="w").pack(side="left")
        refs["id"] = self._inline_entry(r2, cam.get("id", ""), width=80)

        ctk.CTkLabel(r2, text="Index:", width=45, anchor="w").pack(side="left", padx=(8, 0))
        refs["device_index"] = self._inline_entry(
            r2, str(cam.get("device_index", 0)), width=40
        )

        ctk.CTkLabel(r2, text="Res:", width=30, anchor="w").pack(side="left", padx=(8, 0))
        res = cam.get("resolution", [1920, 1080])
        refs["res_w"] = self._inline_entry(r2, str(res[0]), width=55)
        ctk.CTkLabel(r2, text="x", width=12).pack(side="left")
        refs["res_h"] = self._inline_entry(r2, str(res[1]), width=55)

        ctk.CTkLabel(r2, text="FPS:", width=35, anchor="w").pack(side="left", padx=(8, 0))
        refs["fps"] = self._inline_entry(r2, str(cam.get("fps", 30)), width=40)

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
        refs = {}

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
                "duration_seconds": 30,
                "capture_interval_ms": 100,
                "instructions": "",
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
    #  Actions (Run Experiment / Calibrate / Undistort)
    # ==========================================================================

    def _run_experiment(self):
        if self._is_running:
            return
        self._save_settings()
        self._clear_console()
        self._set_running(True)
        self._progress_label.configure(text="Starting experiment...")
        self._progress_bar.set(0)

        def worker():
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = OutputRedirector(self.output_queue, "stdout")
            sys.stderr = OutputRedirector(self.output_queue, "stderr")
            try:
                from src.experiment import Experiment

                exp = Experiment(self.settings)
                self._active_experiment = exp
                exp.run()
            except KeyboardInterrupt:
                pass
            except Exception as e:
                self.output_queue.put(("stderr", f"ERROR: {e}"))
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr
                self._active_experiment = None
                self.after(0, lambda: self._set_running(False))
                self.after(0, lambda: self._progress_label.configure(text="Experiment finished."))

        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    def _run_calibrate(self):
        if self._is_running:
            return
        self._save_settings()
        self._clear_console()
        self._set_running(True)
        self._progress_label.configure(text="Running device calibration...")

        def worker():
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = OutputRedirector(self.output_queue, "stdout")
            sys.stderr = OutputRedirector(self.output_queue, "stderr")
            try:
                from src.calibrate import CalibrationTool

                tool = CalibrationTool(self.settings)
                passed = tool.run()
                status = "All devices ready!" if passed else "Some devices failed."
                self.output_queue.put(("stdout", f"\nResult: {status}"))
            except Exception as e:
                self.output_queue.put(("stderr", f"ERROR: {e}"))
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr
                self.after(0, lambda: self._set_running(False))
                self.after(0, lambda: self._progress_label.configure(text="Calibration finished."))

        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    def _run_undistort(self):
        if self._is_running:
            return
        input_path = self._undist_w["input"].get().strip()
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

    def _stop_experiment(self):
        if not self._is_running or self._worker_thread is None:
            return
        self._log("Stopping... (sending interrupt)")
        self._progress_label.configure(text="Stopping...")
        try:
            thread_id = self._worker_thread.ident
            if thread_id is not None:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_ulong(thread_id), ctypes.py_object(KeyboardInterrupt)
                )
        except Exception as e:
            self._log(f"Stop error: {e}")

    def _set_running(self, running):
        self._is_running = running
        state_on = "normal"
        state_off = "disabled"
        if running:
            self._run_btn.configure(state=state_off)
            self._cal_btn.configure(state=state_off)
            self._undist_btn.configure(state=state_off)
            self._stop_btn.configure(state=state_on)
            self._status_label.configure(text="Running...", text_color=CLR_GREEN)
        else:
            self._run_btn.configure(state=state_on)
            self._cal_btn.configure(state=state_on)
            self._undist_btn.configure(state=state_on)
            self._stop_btn.configure(state=state_off)
            self._status_label.configure(text="Ready", text_color="white")

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

    # ==========================================================================
    #  Helpers
    # ==========================================================================

    def _labeled_entry(self, parent, label, default="", browse=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=15, pady=3)
        ctk.CTkLabel(row, text=label, width=100, anchor="w").pack(side="left")
        entry = ctk.CTkEntry(row)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        if default:
            entry.insert(0, default)

        if browse == "dir":
            ctk.CTkButton(
                row,
                text="Browse",
                width=70,
                command=lambda: self._browse_dir(entry),
            ).pack(side="left")
        elif browse == "file_or_dir":
            ctk.CTkButton(
                row,
                text="File",
                width=55,
                command=lambda: self._browse_file(entry),
            ).pack(side="left", padx=(0, 3))
            ctk.CTkButton(
                row,
                text="Folder",
                width=55,
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
    app = CaptureExpertApp()
    app.mainloop()
