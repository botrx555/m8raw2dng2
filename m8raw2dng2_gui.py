#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
m8raw2dng2_gui - a friendly desktop front-end for m8raw2dng2.py

Pick your input and output folders, tick the options you want, and click Convert.
A live log shows progress. Nothing is encoded until you press the button.

Layout (top -> bottom):
  * Input / output & run control
  * Sensor darkfield (-sd)           -- one-time setup per body
  * FNumber calibration              -- one-time setup per body/lens
  * Image & DNG basics
  * FNumber estimate                 -- pick at most one (the rest grey out)
  * Preview detail                   -- the two children grey until --legacy-preview
  * Command (read-only readout + Copy)
  * Convert + Show log   (the Log is a separate, resizable window)

The database (lensdb.ini / sensdb.ini) is always read from the folder this tool
lives in, so there is no database picker.  Field values and flag states are saved
when you close the window and restored when you reopen it.

Requirements: Python with Tkinter (bundled with the python.org installer on Windows
and macOS; on Linux install the distro package, e.g. `sudo apt install python3-tk`).
Plus the converter's own deps: numpy, tifffile, optionally pillow.  Keep this file
next to m8raw2dng2.py.

Run:  python3 m8raw2dng2_gui.py
"""

import os
import sys
import queue
import threading
import logging
import json
import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

def _app_base_dir() -> str:
    # Folder that holds lensdb.ini / sensdb.ini and anchors relative paths.
    # Bundled (PyInstaller): the folder CONTAINING the .app / .exe (beside the app).
    # Loose script: the folder holding this .py.
    if getattr(sys, "frozen", False):
        d = os.path.dirname(os.path.abspath(sys.executable))
        for _ in range(3):
            if os.path.basename(d).endswith(".app"):
                return os.path.dirname(d)
            d = os.path.dirname(d)
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


TOOLDIR = _app_base_dir()
USERDIR = os.path.expanduser("~")

sys.path.insert(0, TOOLDIR)
try:
    import m8raw2dng2 as core
except Exception as e:  # pragma: no cover - only hit if the file is missing
    raise SystemExit(f"Could not import m8raw2dng2.py (keep it next to this GUI): {e}")

STATE_FILE = os.path.join(TOOLDIR, "m8raw2dng2_gui_state.json")
PRESET_DIR = os.path.join(TOOLDIR, "Presets")
CMD_DISPLAY_CHARS = 96


class QueueHandler(logging.Handler):
    """Funnels converter log records into a thread-safe queue for the UI."""
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(self.format(record))
        except Exception:
            pass


class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        self.master = master
        master.title(f"m8raw2dng2  {core.VERSION_DISPLAY}")

        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._canvas = tk.Canvas(self, highlightthickness=0)
        self._vsb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._vsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._vsb.grid(row=0, column=1, sticky="ns")

        self._body = ttk.Frame(self._canvas)
        self._win = self._canvas.create_window((0, 0), window=self._body, anchor="nw")
        self._body.columnconfigure(0, weight=1)
        self._body.bind("<Configure>",
                        lambda _e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>",
                          lambda e: self._canvas.itemconfigure(self._win, width=e.width))

        self._prog_total = 0
        self._prog_done = 0

        self.log_q = queue.Queue()
        self._log_path = None

        self._init_vars()

        self._build_io(self._body)
        self._build_darkfield(self._body)
        self._build_calibration(self._body)
        self._build_image(self._body)
        self._build_fnumber(self._body)
        self._build_preview(self._body)
        self._build_command(self._body)
        self._build_actions(self._body)
        self._build_log_window()

        self._ensure_default_folders()
        self._populate_lens_dropdowns()
        self.presets = self._load_presets()
        self._refresh_preset_box()
        self._load_state()

        self._wire_dynamics()
        self._sync_all()
        self._refresh_badges()
        self._refresh_cmd()
        self._bind_wheel()
        self._enable_drag_autoscroll()

        master.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_log)
        self.after(80, self._fit_window)

    def _init_vars(self):
        self.in_path = tk.StringVar(value="./Input")
        self.out_path = tk.StringVar(value="./Output")

        self.v_verbose = tk.BooleanVar(value=True)
        self.v_refresh = tk.BooleanVar(value=False)
        self.v_recursive = tk.BooleanVar(value=False)
        self.v_dryrun = tk.BooleanVar(value=False)
        self.v_probe = tk.BooleanVar(value=False)
        self.v_verify = tk.BooleanVar(value=False)
        self.v_jobs = tk.StringVar(value="1")
        self.v_log_on = tk.BooleanVar(value=False)
        self.v_log_path = tk.StringVar(value="")
        self.v_preset = tk.StringVar(value="")

        self.v_autolines = tk.BooleanVar(value=True)
        self.v_calib_lenscode = tk.StringVar(value="")
        self.v_calib_aps = tk.StringVar(value="")

        self.v_sensor = tk.BooleanVar(value=True)
        self.v_preview = tk.BooleanVar(value=True)
        self.v_nocrop = tk.BooleanVar(value=True)
        self.v_m9 = tk.BooleanVar(value=False)
        self.v_black_on = tk.BooleanVar(value=True)
        self.v_black_val = tk.StringVar(value=str(core.BLACK_DEFAULT))
        self.v_lens_on = tk.BooleanVar(value=False)
        self.v_lens_code = tk.StringVar(value="")
        self.v_cfa = tk.StringVar(value="RGGB")

        self.v_mimic = tk.BooleanVar(value=False)
        self.v_legacy_fnumber = tk.BooleanVar(value=False)
        self.v_selfcal = tk.BooleanVar(value=False)
        self.v_aperture_on = tk.BooleanVar(value=False)
        self.v_aperture_val = tk.StringVar(value="")

        self.v_legacy = tk.BooleanVar(value=False)
        self.v_preview_uncompressed = tk.BooleanVar(value=False)
        self.v_preview_size = tk.StringVar(value="1024")

    STATE_VARS = (
        "in_path", "out_path", "v_refresh", "v_recursive", "v_dryrun", "v_probe",
        "v_verify", "v_jobs", "v_log_on", "v_log_path",
        "v_autolines", "v_calib_lenscode", "v_calib_aps",
        "v_sensor", "v_preview", "v_nocrop", "v_m9", "v_black_on", "v_black_val",
        "v_lens_on", "v_lens_code", "v_cfa",
        "v_mimic", "v_legacy_fnumber", "v_selfcal", "v_aperture_on", "v_aperture_val",
        "v_legacy", "v_preview_uncompressed", "v_preview_size",
    )
    PRESET_VARS = tuple(v for v in STATE_VARS if v not in (
        "in_path", "out_path", "v_calib_lenscode", "v_calib_aps"))

    def _section(self, parent, title):
        """A LabelFrame whose title is drawn in the default body font.  The aqua
        theme otherwise renders group-box titles a notch smaller than the text
        inside the box; using a labelwidget with the default font (not a
        hard-coded size) makes the title match the body and stay theme-aware."""
        f = ttk.LabelFrame(parent, padding=6)
        lbl = ttk.Label(f, text=title, font="TkDefaultFont")
        f.configure(labelwidget=lbl)
        return f

    def _build_io(self, parent):
        f = self._section(parent, "Input / output & run control")
        f.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Input  (-i)").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.in_path).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="Folder\u2026", command=self._pick_in).grid(row=0, column=2)

        ttk.Label(f, text="Output  (-o)").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(f, textvariable=self.out_path).grid(row=1, column=1, sticky="ew", padx=6, pady=(4, 0))
        ttk.Button(f, text="Folder\u2026", command=self._pick_out).grid(row=1, column=2, pady=(4, 0))

        row = ttk.Frame(f); row.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.cb_verbose = ttk.Checkbutton(row, text="Verbose (-v)", variable=self.v_verbose)
        self.cb_verbose.grid(row=0, column=0, sticky="w", padx=(0, 16))
        ttk.Checkbutton(row, text="Overwrite DNGs (-r)", variable=self.v_refresh).grid(row=0, column=1, sticky="w", padx=(0, 16))
        ttk.Checkbutton(row, text="Recurse (-R)", variable=self.v_recursive).grid(row=0, column=2, sticky="w", padx=(0, 16))
        self.cb_dryrun = ttk.Checkbutton(row, text="Dry run (--dry-run)", variable=self.v_dryrun)
        self.cb_dryrun.grid(row=0, column=3, sticky="w")

        row2 = ttk.Frame(f); row2.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.cb_probe = ttk.Checkbutton(row2, text="Probe input (--probe)", variable=self.v_probe)
        self.cb_probe.grid(row=0, column=0, sticky="w", padx=(0, 16))
        ttk.Checkbutton(row2, text="Verify DNG (--verify)", variable=self.v_verify).grid(row=0, column=1, sticky="w", padx=(0, 24))
        ttk.Label(row2, text="Parallel jobs (-j):").grid(row=0, column=2, sticky="w")
        ttk.Entry(row2, textvariable=self.v_jobs, width=5).grid(row=0, column=3, sticky="w", padx=(4, 0))

        row3 = ttk.Frame(f); row3.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Checkbutton(row3, text="Write log (--log):", variable=self.v_log_on).grid(row=0, column=0, sticky="w")
        ttk.Entry(row3, textvariable=self.v_log_path, width=40).grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Label(row3, text="(blank = auto-name in output folder)").grid(row=0, column=2, sticky="w", padx=(6, 0))

        row4 = ttk.Frame(f); row4.grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(row4, text="Preset:").grid(row=0, column=0, sticky="w")
        self.preset_box = ttk.Combobox(row4, textvariable=self.v_preset, width=26, state="readonly")
        self.preset_box.grid(row=0, column=1, sticky="w", padx=(6, 6))
        self.preset_box.bind("<<ComboboxSelected>>", self._apply_preset)
        ttk.Button(row4, text="Save\u2026", command=self._save_preset).grid(row=0, column=2, padx=2)
        ttk.Button(row4, text="Delete", command=self._delete_preset).grid(row=0, column=3, padx=2)

    def _build_darkfield(self, parent):
        f = self._section(parent, "Sensor darkfield  (-sd)")
        f.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.btn_make_df = ttk.Button(f, text="Create / update darkfield\u2026", width=26,
                                      command=self._make_darkfield)
        self.btn_make_df.grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(f, text="auto-detect & record defect columns (--auto-lines)",
                        variable=self.v_autolines).grid(row=0, column=1, sticky="w", padx=(12, 0))
        b = ttk.Frame(f); b.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.df_dot = ttk.Label(b, text="\u25cb"); self.df_dot.grid(row=0, column=0)
        self.df_txt = ttk.Label(b, text="Not created"); self.df_txt.grid(row=0, column=1, padx=(6, 0))

    def _build_calibration(self, parent):
        f = self._section(parent, "FNumber calibration  (--calibrate-fnumber)")
        f.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        r = ttk.Frame(f); r.grid(row=0, column=0, sticky="w")
        self.btn_calib = ttk.Button(r, text="Calibrate aperture meter\u2026", width=26,
                                    command=self._calibrate_fnumber)
        self.btn_calib.grid(row=0, column=0, sticky="w")
        ttk.Label(r, text="lens code:").grid(row=0, column=1, sticky="w", padx=(14, 4))
        self.dd_calib = ttk.Combobox(r, textvariable=self.v_calib_lenscode, width=10, state="readonly")
        self.dd_calib.grid(row=0, column=2, sticky="w")
        self.dd_calib.bind("<<ComboboxSelected>>", self._calib_lens_chosen)
        ttk.Label(r, text="apertures (file order):").grid(row=0, column=3, sticky="w", padx=(14, 4))
        ttk.Entry(r, textvariable=self.v_calib_aps, width=20).grid(row=0, column=4, sticky="w")
        b = ttk.Frame(f); b.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.cal_dot = ttk.Label(b, text="\u25cb"); self.cal_dot.grid(row=0, column=0)
        self.cal_txt = ttk.Label(b, text="Not calibrated"); self.cal_txt.grid(row=0, column=1, padx=(6, 0))
        r2 = ttk.Frame(f); r2.grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(r2, text="Lens database:").grid(row=0, column=0, sticky="w")
        ttk.Button(r2, text="Edit lenses\u2026", width=16,
                   command=self._open_lens_editor).grid(row=0, column=1, sticky="w", padx=(8, 0))

    def _build_image(self, parent):
        f = self._section(parent, "Image & DNG basics")
        f.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        r0 = ttk.Frame(f); r0.grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(r0, text="Sensor fixes (-s)", variable=self.v_sensor).grid(row=0, column=0, sticky="w", padx=(0, 16))
        ttk.Checkbutton(r0, text="Embed preview (-p)", variable=self.v_preview).grid(row=0, column=1, sticky="w", padx=(0, 16))
        ttk.Checkbutton(r0, text="No crop (--no-crop)", variable=self.v_nocrop).grid(row=0, column=2, sticky="w", padx=(0, 16))
        ttk.Checkbutton(r0, text="M9 colour (-c)", variable=self.v_m9).grid(row=0, column=3, sticky="w")
        r1 = ttk.Frame(f); r1.grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Checkbutton(r1, text="Black level (-b):", variable=self.v_black_on).grid(row=0, column=0, sticky="w")
        ttk.Entry(r1, textvariable=self.v_black_val, width=6).grid(row=0, column=1, sticky="w", padx=(4, 18))
        ttk.Checkbutton(r1, text="Lens (-l):", variable=self.v_lens_on).grid(row=0, column=2, sticky="w")
        self.dd_lens = ttk.Combobox(r1, textvariable=self.v_lens_code, width=14, state="disabled")
        self.dd_lens.grid(row=0, column=3, sticky="w", padx=(4, 18))
        ttk.Label(r1, text="CFA (--cfa):").grid(row=0, column=4, sticky="w")
        ttk.Combobox(r1, textvariable=self.v_cfa, width=8, state="readonly",
                     values=list(core.CFA_PATTERNS.keys())).grid(row=0, column=5, sticky="w", padx=(4, 0))

    def _build_fnumber(self, parent):
        f = self._section(parent, "FNumber estimate")
        f.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        self.cb_mimic = ttk.Checkbutton(f, text="Mimic original (--mimic-fnumber)", variable=self.v_mimic)
        self.cb_mimic.grid(row=0, column=0, sticky="w", padx=(0, 36))
        self.cb_legacy_fn = ttk.Checkbutton(f, text="Ignore meters (--legacy-fnumber)", variable=self.v_legacy_fnumber)
        self.cb_legacy_fn.grid(row=0, column=1, sticky="w")
        self.cb_selfcal = ttk.Checkbutton(f, text="Batch self-cal (--selfcal)", variable=self.v_selfcal)
        self.cb_selfcal.grid(row=1, column=0, sticky="w", pady=(4, 0))
        af = ttk.Frame(f); af.grid(row=1, column=1, sticky="w", pady=(4, 0))
        self.cb_aperture = ttk.Checkbutton(af, text="Force aperture (-A):", variable=self.v_aperture_on)
        self.cb_aperture.grid(row=0, column=0, sticky="w")
        self.lbl_fslash = ttk.Label(af, text="f/")
        self.lbl_fslash.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.e_aperture = ttk.Entry(af, textvariable=self.v_aperture_val, width=7)
        self.e_aperture.grid(row=0, column=2, sticky="w", padx=(2, 0))

    def _build_preview(self, parent):
        f = self._section(parent, "Preview detail")
        f.grid(row=5, column=0, sticky="ew", pady=(0, 6))
        r = ttk.Frame(f); r.grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(r, text="Legacy preview (--legacy-preview)", variable=self.v_legacy).grid(row=0, column=0, sticky="w", padx=(0, 18))
        self.cb_preview_unc = ttk.Checkbutton(r, text="Uncompressed (--preview-uncompressed)", variable=self.v_preview_uncompressed)
        self.cb_preview_unc.grid(row=0, column=1, sticky="w", padx=(0, 18))
        self.lbl_psize = ttk.Label(r, text="Long edge (--preview-size):")
        self.lbl_psize.grid(row=0, column=2, sticky="w")
        self.e_preview_size = ttk.Entry(r, textvariable=self.v_preview_size, width=6)
        self.e_preview_size.grid(row=0, column=3, sticky="w", padx=(4, 0))

    def _build_command(self, parent):
        f = self._section(parent, "Command")
        f.grid(row=6, column=0, sticky="ew", pady=(0, 6))
        f.columnconfigure(0, weight=1)
        self._full_cmd = ""
        self.cmd_lbl = ttk.Label(f, text="", font="TkFixedFont")
        self.cmd_lbl.grid(row=0, column=0, sticky="w")
        ttk.Button(f, text="Copy", command=self._copy_command).grid(row=0, column=1, sticky="e", padx=(8, 0))

    def _build_actions(self, parent):
        f = ttk.Frame(parent, padding=(0, 0))
        f.grid(row=7, column=0, sticky="ew", pady=(2, 2))
        f.columnconfigure(0, weight=1)
        btns = ttk.Frame(f)
        btns.grid(row=0, column=0, sticky="w")
        self.btn_run = ttk.Button(btns, text="Convert", command=self._run)
        self.btn_run.grid(row=0, column=0, sticky="w")
        ttk.Button(btns, text="Show log", command=self._show_log).grid(row=0, column=1, sticky="w", padx=(10, 0))
        pf = ttk.Frame(f)
        pf.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        pf.columnconfigure(0, weight=1)
        self.prog = ttk.Progressbar(pf, mode="determinate")
        self.prog.grid(row=0, column=0, sticky="ew")
        self.prog_lbl = ttk.Label(pf, text="Idle")
        self.prog_lbl.grid(row=0, column=1, sticky="e", padx=(10, 0))

    def _build_log_window(self):
        """The Log lives in its own resizable window.  Keeping it out of the main
        panel means the panel stays short enough to fit the display (no scrolling),
        and the log text scrolls and selects natively here with nothing nested."""
        self._saved_logwin_geom = None
        self._logwin_placed = False
        self._logwin = tk.Toplevel(self.master)
        self._logwin.title("m8raw2dng2 \u2014 Log")
        self._logwin.columnconfigure(0, weight=1)
        self._logwin.rowconfigure(0, weight=1)
        self._logwin.protocol("WM_DELETE_WINDOW", self._hide_log)
        self.txt = tk.Text(self._logwin, height=22, width=92, wrap="word")
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(self._logwin, orient="vertical", command=self.txt.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.txt["yscrollcommand"] = sb.set
        bar = ttk.Frame(self._logwin, padding=(6, 4))
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Button(bar, text="Clear", command=lambda: self.txt.delete("1.0", "end")).grid(row=0, column=0, sticky="w")
        self._logwin.withdraw()

    def _show_log(self):
        try:
            if self._logwin is None or not self._logwin.winfo_exists():
                return
            if not self._logwin_placed:
                geom = getattr(self, "_saved_logwin_geom", None)
                if geom:
                    self._logwin.geometry(geom)
                else:
                    self.master.update_idletasks()
                    x = self.master.winfo_x() + self.master.winfo_width() + 12
                    y = self.master.winfo_y()
                    self._logwin.geometry(f"+{max(0, x)}+{max(0, y)}")
                self._logwin_placed = True
            self._logwin.deiconify()
            self._logwin.lift()
        except Exception:
            pass

    def _hide_log(self):
        try:
            self._logwin.withdraw()
        except Exception:
            pass

    def _pick_in(self):
        d = filedialog.askdirectory(title="Choose the input folder",
                                    initialdir=self._resolve(self.in_path.get()) or USERDIR)
        if d:
            self.in_path.set(d); self._refresh_cmd()

    def _pick_out(self):
        d = filedialog.askdirectory(title="Choose the output folder",
                                    initialdir=self._resolve(self.out_path.get()) or USERDIR)
        if d:
            self.out_path.set(d); self._refresh_cmd()

    def _ensure_default_folders(self):
        for name in ("Input", "Output"):
            try:
                os.makedirs(os.path.join(TOOLDIR, name), exist_ok=True)
            except Exception:
                pass

    def _fit_window(self):
        """Size the window to the content's real size so the one-line rows are not
        clipped on the right and the whole tool is reachable by the scrollbar (the
        native font is wider/taller than the design mockup, so a fixed size won't do).
        Height is capped to the screen; anything beyond that the scrollbar reveals."""
        try:
            self.update_idletasks()
            bw = self._body.winfo_reqwidth()
            bh = self._body.winfo_reqheight()
            if not bw or bw < 200:
                return
            self._canvas.configure(width=bw)
            try:
                sb = self._vsb.winfo_reqwidth()
            except Exception:
                sb = 16
            total_w = bw + sb + 24
            try:
                screen_h = self.master.winfo_screenheight()
            except Exception:
                screen_h = 1000
            total_h = min(bh + 28, max(480, screen_h - 120))
            self.master.geometry(f"{int(total_w)}x{int(total_h)}")
            self.master.minsize(int(total_w), 480)
        except Exception:
            pass

    @staticmethod
    def _wheel_delta(e):
        n = getattr(e, "num", 0)
        if n == 4:
            return -1
        if n == 5:
            return 1
        d = getattr(e, "delta", 0)
        if not d:
            return 0
        return -1 if d > 0 else 1

    def _bind_wheel(self):
        """Scroll the whole tool with the wheel from anywhere in the window.

        macOS Tk is fussy about wheel delivery over non-scrollable widgets, so cover
        every angle: bind the handler on the toplevel window, on the 'all' tag, and
        on every widget directly, AND re-assert the 'all' binding whenever the
        pointer enters any widget (the bind_all-on-Enter pattern is what makes the
        wheel fire over plain frames / checkbuttons on macOS).  'break' makes
        whichever path fires first the only one that acts (no double-scroll; a
        combobox can't cycle its value on wheel)."""
        def on_wheel(e):
            self._canvas.yview_scroll(self._wheel_delta(e), "units")
            return "break"
        seqs = ("<MouseWheel>", "<Button-4>", "<Button-5>")

        def bind_global():
            for s in seqs:
                try:
                    self._canvas.bind_all(s, on_wheel)
                except Exception:
                    pass

        for s in seqs:
            try:
                self.master.bind(s, on_wheel)
            except Exception:
                pass
        bind_global()

        def walk(w):
            for s in seqs:
                try:
                    w.bind(s, on_wheel)
                except Exception:
                    pass
            try:
                w.bind("<Enter>", lambda _e: bind_global(), add="+")
            except Exception:
                pass
            try:
                kids = list(w.winfo_children())
            except Exception:
                kids = []
            for c in kids:
                walk(c)
        try:
            walk(self)
        except Exception:
            pass

    def _enable_drag_autoscroll(self):
        """Make a drag-selection auto-scroll past the visible edge: entries scroll
        horizontally, the log scrolls vertically.  macOS Tk does not reliably start
        its own auto-scan when the pointer leaves the widget mid-drag, so the user
        can otherwise only select what is already on screen.  Added with add='+' so
        the native selection behaviour is preserved."""
        def x_scroll(e):
            w = e.widget
            try:
                if e.x < 0:
                    w.xview_scroll(-2, "units")
                elif e.x > w.winfo_width():
                    w.xview_scroll(2, "units")
            except Exception:
                pass
        def y_scroll(e):
            w = e.widget
            try:
                if e.y < 0:
                    w.yview_scroll(-1, "units")
                elif e.y > w.winfo_height():
                    w.yview_scroll(1, "units")
            except Exception:
                pass

        def walk(w):
            try:
                cls = w.winfo_class()
            except Exception:
                cls = ""
            if cls in ("TEntry", "Entry"):
                try:
                    w.bind("<B1-Motion>", x_scroll, add="+")
                except Exception:
                    pass
            try:
                kids = list(w.winfo_children())
            except Exception:
                kids = []
            for c in kids:
                walk(c)
        try:
            walk(self)
        except Exception:
            pass
        try:
            self.txt.bind("<B1-Motion>", y_scroll, add="+")
        except Exception:
            pass

    def _resolve(self, p):
        """Resolve a possibly-relative path against the tool's own folder."""
        p = (p or "").strip()
        if not p:
            return ""
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(TOOLDIR, p))

    def _lens_db(self):
        try:
            return core.parse_lensdb(os.path.join(TOOLDIR, "lensdb.ini"))
        except Exception:
            return {}

    def _lens_codes(self):
        return sorted(self._lens_db().keys())

    def _calib_lens_chosen(self, event=None):
        aps = self._lens_db().get(self.v_calib_lenscode.get().strip(), {}).get("Apertures") or []
        if aps:
            self.v_calib_aps.set(",".join(f"{a:g}" for a in aps))

    def _populate_lens_dropdowns(self):
        codes = self._lens_codes()
        self.dd_lens["values"] = codes
        self.dd_calib["values"] = codes

    def _lensdb_path(self):
        return os.path.join(TOOLDIR, "lensdb.ini")

    def _write_lens_db(self, db):
        # Canonical, atomic rewrite of lensdb.ini from
        # {code: {Maker, Model, SerialNo, FocalLength, Apertures[]}}.
        lines = []
        for code in sorted(db):
            e = db[code]
            lines.append(f"[{code}]")
            lines.append(f"Maker = {e.get('Maker', '') or ''}")
            lines.append(f"Model = {e.get('Model', '') or ''}")
            lines.append(f"SerialNo = {e.get('SerialNo', '') or ''}")
            fl = e.get("FocalLength")
            lines.append(f"FocalLength = {('%g' % fl) if fl is not None else ''}")
            for a in e.get("Apertures", []):
                lines.append(f"Aperture = {a:g}")
            lines.append("")
        text = "\n".join(lines).rstrip() + "\n"
        path = self._lensdb_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)

    def _open_lens_editor(self):
        win = tk.Toplevel(self)
        win.title("Lens database (lensdb.ini)")
        win.transient(self)
        win.resizable(False, False)

        db = self._lens_db()

        v_pick = tk.StringVar()
        v_code = tk.StringVar()
        v_maker = tk.StringVar()
        v_model = tk.StringVar()
        v_serial = tk.StringVar()
        v_focal = tk.StringVar()
        v_aps = tk.StringVar()

        frm = ttk.Frame(win, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")

        top = ttk.Frame(frm)
        top.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(top, text="Lens:").grid(row=0, column=0, sticky="w")
        dd = ttk.Combobox(top, textvariable=v_pick, width=12, state="readonly",
                          values=sorted(db.keys()))
        dd.grid(row=0, column=1, sticky="w", padx=(6, 0))

        def clear_form():
            v_pick.set("")
            for v in (v_code, v_maker, v_model, v_serial, v_focal, v_aps):
                v.set("")

        ttk.Button(top, text="New", width=8, command=clear_form).grid(row=0, column=2, padx=(12, 0))

        def field(r, label, var, width=26):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=4)
            ttk.Entry(frm, textvariable=var, width=width).grid(row=r, column=1, sticky="w", padx=8, pady=4)

        field(1, "6-bit code", v_code, 14)
        field(2, "Maker", v_maker)
        field(3, "Model", v_model)
        field(4, "SerialNo", v_serial)
        field(5, "FocalLength (mm)", v_focal, 10)
        field(6, "Apertures (comma-sep)", v_aps, 30)

        def load(code):
            e = db.get(code)
            if not e:
                return
            v_code.set(code)
            v_maker.set(e.get("Maker", "") or "")
            v_model.set(e.get("Model", "") or "")
            v_serial.set(e.get("SerialNo", "") or "")
            fl = e.get("FocalLength")
            v_focal.set(f"{fl:g}" if fl is not None else "")
            v_aps.set(",".join(f"{a:g}" for a in e.get("Apertures", [])))

        dd.bind("<<ComboboxSelected>>", lambda ev: load(v_pick.get()))

        def do_save():
            code = v_code.get().strip()
            if not code:
                messagebox.showerror("Lens database", "6-bit code is required.", parent=win)
                return
            if len(code) != 6 or any(c not in "01" for c in code):
                if not messagebox.askyesno(
                        "Lens database",
                        f"'{code}' is not six binary digits (000000-111111).\nSave it anyway?",
                        parent=win):
                    return
            fl_txt = v_focal.get().strip()
            fl = None
            if fl_txt:
                try:
                    fl = float(fl_txt)
                except ValueError:
                    messagebox.showerror("Lens database", f"FocalLength '{fl_txt}' is not a number.", parent=win)
                    return
            aps = []
            for tok in v_aps.get().replace(";", ",").split(","):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    aps.append(float(tok))
                except ValueError:
                    messagebox.showerror("Lens database", f"Aperture '{tok}' is not a number.", parent=win)
                    return
            if code in db and code != v_pick.get():
                if not messagebox.askyesno("Lens database",
                                           f"Lens '{code}' already exists.\nOverwrite it?", parent=win):
                    return
            db[code] = {"Maker": v_maker.get().strip(), "Model": v_model.get().strip(),
                        "SerialNo": v_serial.get().strip(), "FocalLength": fl, "Apertures": aps}
            try:
                self._write_lens_db(db)
            except Exception as exc:
                messagebox.showerror("Lens database", f"Could not write lensdb.ini:\n{exc}", parent=win)
                return
            dd["values"] = sorted(db.keys())
            v_pick.set(code)
            self._populate_lens_dropdowns()
            messagebox.showinfo("Lens database", f"Saved lens '{code}'.", parent=win)

        def do_delete():
            code = (v_pick.get() or v_code.get()).strip()
            if not code or code not in db:
                messagebox.showerror("Lens database", "Pick an existing lens to delete.", parent=win)
                return
            if not messagebox.askyesno("Lens database", f"Delete lens '{code}'?", parent=win):
                return
            db.pop(code, None)
            try:
                self._write_lens_db(db)
            except Exception as exc:
                messagebox.showerror("Lens database", f"Could not write lensdb.ini:\n{exc}", parent=win)
                return
            dd["values"] = sorted(db.keys())
            clear_form()
            self._populate_lens_dropdowns()

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Delete", command=do_delete).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(btns, text="Save / Update", command=do_save).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(btns, text="Close", command=win.destroy).grid(row=0, column=2)

        win.grab_set()
        win.wait_window()

    def _refresh_badges(self):
        try:
            sdb = core.parse_sensdb(os.path.join(TOOLDIR, "sensdb.ini"))
        except Exception:
            sdb = {}
        df = [(s, e) for s, e in sdb.items() if e.get("levels") is not None]
        if df:
            if len(df) == 1:
                s, e = df[0]
                self._set_badge(self.df_dot, self.df_txt, True,
                                f"On file  \u00b7  body {s}  \u00b7  {len(e.get('lines') or [])} defect columns")
            else:
                self._set_badge(self.df_dot, self.df_txt, True, f"On file  \u00b7  {len(df)} bodies")
        else:
            self._set_badge(self.df_dot, self.df_txt, False, "Not created")
        cal = [(s, lc, v) for s, e in sdb.items() for lc, v in (e.get("meter_offsets") or {}).items()]
        if cal:
            if len(cal) == 1:
                _s, lc, v = cal[0]
                self._set_badge(self.cal_dot, self.cal_txt, True,
                                f"Calibrated  \u00b7  lens {lc}  \u00b7  offset {v:.3f}")
            else:
                self._set_badge(self.cal_dot, self.cal_txt, True, f"Calibrated  \u00b7  {len(cal)} (body, lens) pairs")
        else:
            self._set_badge(self.cal_dot, self.cal_txt, False, "Not calibrated")

    @staticmethod
    def _set_badge(dot, txt, present, text):
        dot.config(text="\u2714" if present else "\u25cb")
        txt.config(text=text)
        state = ["!disabled"] if present else ["disabled"]
        try:
            dot.state(state); txt.state(state)
        except Exception:
            pass

    def _wire_dynamics(self):
        for v in (self.v_mimic, self.v_legacy_fnumber, self.v_selfcal, self.v_aperture_on):
            v.trace_add("write", self._sync_fnumber)
        self.v_legacy.trace_add("write", self._sync_preview)
        self.v_dryrun.trace_add("write", self._sync_dryprobe)
        self.v_probe.trace_add("write", self._sync_dryprobe)
        self.v_lens_on.trace_add("write", self._sync_lens)
        for name in self.STATE_VARS:
            getattr(self, name).trace_add("write", self._refresh_cmd)
        self.v_verbose.trace_add("write", self._refresh_cmd)

    def _sync_all(self):
        self._sync_fnumber(); self._sync_preview(); self._sync_dryprobe(); self._sync_lens()

    def _sync_fnumber(self, *_):
        flags = ((self.v_mimic, self.cb_mimic),
                 (self.v_legacy_fnumber, self.cb_legacy_fn),
                 (self.v_selfcal, self.cb_selfcal),
                 (self.v_aperture_on, self.cb_aperture))
        locked = any(v.get() for v, _ in flags)
        for v, cb in flags:
            cb.config(state=("normal" if (v.get() or not locked) else "disabled"))
        self.e_aperture.config(state=("normal" if self.v_aperture_on.get() else "disabled"))
        self.lbl_fslash.state(["!disabled"] if (self.v_aperture_on.get() or not locked) else ["disabled"])

    def _sync_preview(self, *_):
        on = self.v_legacy.get()
        st = "normal" if on else "disabled"
        self.cb_preview_unc.config(state=st)
        self.e_preview_size.config(state=st)
        self.lbl_psize.state(["!disabled"] if on else ["disabled"])

    def _sync_dryprobe(self, *_):
        self.cb_probe.config(state=("disabled" if self.v_dryrun.get() else "normal"))
        self.cb_dryrun.config(state=("disabled" if self.v_probe.get() else "normal"))

    def _sync_lens(self, *_):
        self.dd_lens.config(state=("readonly" if self.v_lens_on.get() else "disabled"))

    def _refresh_cmd(self, *_):
        o = self._collect()
        cmd = "m8raw2dng2 " + self._describe(o)
        ip = self._resolve(self.in_path.get())
        op = self._resolve(self.out_path.get())
        if ip:
            cmd += f" -i {self.in_path.get().strip()}"
        if op:
            cmd += f" -o {self.out_path.get().strip()}"
        self._full_cmd = cmd
        shown = cmd if len(cmd) <= CMD_DISPLAY_CHARS else cmd[:CMD_DISPLAY_CHARS - 1] + "\u2026"
        if hasattr(self, "cmd_lbl"):
            self.cmd_lbl.config(text=shown)

    def _copy_command(self):
        try:
            self.clipboard_clear(); self.clipboard_append(self._full_cmd)
            self._log("(command copied to clipboard)")
        except Exception:
            pass

    def _collect(self, *, for_darkfield=False):
        o = core.Options()
        ip = self._resolve(self.in_path.get())
        o.inputs = [ip] if ip else []
        o.out_dir = self._resolve(self.out_path.get()) or None
        o.db_dir = None
        o.verbose = self.v_verbose.get()
        o.refresh = self.v_refresh.get()
        o.preview = self.v_preview.get()
        o.color_m9 = self.v_m9.get()
        o.sensor = self.v_sensor.get()
        o.recursive = self.v_recursive.get()
        o.dry_run = self.v_dryrun.get()
        o.no_crop = self.v_nocrop.get()
        o.legacy_preview = self.v_legacy.get()
        o.verify = self.v_verify.get()
        try:
            o.preview_size = max(128, min(4096, int(self.v_preview_size.get())))
        except ValueError:
            o.preview_size = 1024
        o.preview_uncompressed = self.v_preview_uncompressed.get()
        o.cfa = self.v_cfa.get()
        try:
            o.jobs = max(1, int(self.v_jobs.get()))
        except ValueError:
            o.jobs = 1
        if self.v_black_on.get():
            o.set_black = True
            try:
                o.black = int(self.v_black_val.get())
            except ValueError:
                o.black = core.BLACK_DEFAULT
        if self.v_lens_on.get():
            o.lens = True
            o.lens_code = self.v_lens_code.get().strip() or None
        if self.v_aperture_on.get():
            try:
                o.aperture = float(self.v_aperture_val.get())
            except ValueError:
                o.aperture = None
        o.auto_lines = self.v_autolines.get()
        o.mimic_fnumber = self.v_mimic.get()
        o.legacy_fnumber = self.v_legacy_fnumber.get()
        o.selfcal = self.v_selfcal.get()
        if for_darkfield:
            o.sensor_darkfield_create = True
        return o

    def _describe(self, o):
        parts = []
        if o.verbose: parts.append("-v")
        if o.refresh: parts.append("-r")
        if o.preview: parts.append("-p")
        if o.color_m9: parts.append("-c")
        if o.set_black: parts.append(f"-b {o.black}")
        if o.sensor: parts.append("-s")
        if o.lens: parts.append(f"-l {o.lens_code or '(file code)'}")
        if o.no_crop: parts.append("--no-crop")
        if o.aperture: parts.append(f"-A {o.aperture:g}")
        if o.mimic_fnumber: parts.append("--mimic-fnumber")
        if o.legacy_fnumber: parts.append("--legacy-fnumber")
        if o.selfcal: parts.append("--selfcal")
        if o.legacy_preview:
            parts.append("--legacy-preview")
            if getattr(o, "preview_size", 1024) != 1024:
                parts.append(f"--preview-size {o.preview_size}")
            if getattr(o, "preview_uncompressed", False):
                parts.append("--preview-uncompressed")
        if o.recursive: parts.append("-R")
        if o.jobs > 1: parts.append(f"-j {o.jobs}")
        if o.verify: parts.append("--verify")
        parts.append(f"--cfa {o.cfa}")
        if o.dry_run: parts.append("--dry-run")
        if self.v_probe.get(): parts.append("--probe")
        if self.v_log_on.get():
            lp = self.v_log_path.get().strip()
            parts.append("--log " + (lp if lp else "(auto)"))
        return " ".join(parts)

    def _load_presets(self):
        """Each preset is its own file in the Presets/ folder:
        {"name": <display name>, "values": {...}}.  Returns {name: values}."""
        self._preset_files = {}
        presets = {}
        try:
            entries = sorted(os.listdir(PRESET_DIR))
        except Exception:
            return presets
        for fn in entries:
            if not fn.lower().endswith(".json"):
                continue
            path = os.path.join(PRESET_DIR, fn)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    obj = json.load(fh)
            except Exception:
                continue
            if isinstance(obj, dict) and "values" in obj:
                name = obj.get("name") or os.path.splitext(fn)[0]
                vals = obj.get("values") or {}
            elif isinstance(obj, dict):
                name, vals = os.path.splitext(fn)[0], obj
            else:
                continue
            presets[name] = vals
            self._preset_files[name] = path
        return presets

    @staticmethod
    def _safe_filename(name):
        keep = "".join(c if (c.isalnum() or c in " -_().") else "_" for c in name).strip()
        return (keep or "preset") + ".json"

    def _write_preset_file(self, name, vals):
        os.makedirs(PRESET_DIR, exist_ok=True)
        path = self._preset_files.get(name)
        if not path:
            base = self._safe_filename(name)
            stem, ext = os.path.splitext(base)
            path = os.path.join(PRESET_DIR, base)
            k = 2
            while os.path.exists(path):
                path = os.path.join(PRESET_DIR, f"{stem}_{k}{ext}"); k += 1
            self._preset_files[name] = path
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"name": name, "values": vals}, fh, indent=2)

    def _refresh_preset_box(self):
        names = sorted(self.presets.keys())
        self.preset_box["values"] = names
        if not names:
            self.v_preset.set("")
            try:
                self.preset_box.set("(no preset saved)")
            except Exception:
                pass

    def _capture_current(self):
        d = {}
        for name in self.PRESET_VARS:
            d[name] = getattr(self, name).get()
        return d

    def _apply_dict(self, d):
        for name, val in d.items():
            if hasattr(self, name):
                try:
                    getattr(self, name).set(val)
                except Exception:
                    pass

    def _apply_preset(self, *_):
        name = self.v_preset.get()
        if name in self.presets:
            self._apply_dict(self.presets[name])
            self._sync_all(); self._refresh_cmd()

    def _save_preset(self):
        name = simpledialog.askstring("Save preset", "Name this preset:", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        vals = self._capture_current()
        self.presets[name] = vals
        try:
            self._write_preset_file(name, vals)
        except Exception as e:
            messagebox.showwarning("Could not save preset", str(e))
            return
        self._refresh_preset_box()
        self.v_preset.set(name)

    def _delete_preset(self):
        name = self.v_preset.get()
        if name in self.presets and messagebox.askyesno("Delete preset", f"Delete preset '{name}'?"):
            del self.presets[name]
            path = self._preset_files.pop(name, None)
            if path:
                try:
                    os.remove(path)
                except Exception:
                    pass
            self._refresh_preset_box()

    def _load_state(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            return
        self._saved_logwin_geom = d.pop("_logwin_geometry", None)
        self._apply_dict(d)

    def _save_state(self):
        d = {name: getattr(self, name).get() for name in self.STATE_VARS}
        try:
            if self._logwin is not None and self._logwin.winfo_exists():
                d["_logwin_geometry"] = self._logwin.geometry()
        except Exception:
            pass
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump(d, fh, indent=2)
        except Exception:
            pass

    def _on_close(self):
        self._save_state()
        self.master.destroy()

    def _log(self, msg):
        self.txt.insert("end", msg + "\n"); self.txt.see("end")

    def _drain_log(self):
        try:
            while True:
                self._log(self.log_q.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._drain_log)

    def _prog_busy_start(self):
        """Indeterminate sweep for operations without a known item count."""
        try:
            self.prog.config(mode="indeterminate")
            self.prog.start(12)
            self.prog_lbl.config(text="Working\u2026")
        except Exception:
            pass

    def _prog_plan(self, total):
        """Switch to a determinate bar once the number of frames is known."""
        try:
            self.prog.stop()
            self._prog_total = int(total)
            self._prog_done = 0
            self.prog.config(mode="determinate", maximum=max(self._prog_total, 1), value=0)
            self.prog_lbl.config(text=f"Converting  \u00b7  0 / {self._prog_total}")
        except Exception:
            pass

    def _prog_step(self, n=1):
        try:
            self._prog_done += n
            self.prog.config(value=self._prog_done)
            pct = int(round(100 * self._prog_done / max(self._prog_total, 1)))
            done = (self._prog_done >= self._prog_total)
            head = "Done" if done else "Converting"
            self.prog_lbl.config(text=f"{head}  \u00b7  {self._prog_done} / {self._prog_total}  \u00b7  {pct}%")
        except Exception:
            pass

    def _prog_reset(self):
        try:
            self.prog.stop()
            self.prog.config(mode="determinate", maximum=1, value=0)
            self.prog_lbl.config(text="Idle")
        except Exception:
            pass

    def _busy(self, busy):
        state = "disabled" if busy else "normal"
        for b in (self.btn_run, self.btn_make_df, self.btn_calib):
            b.config(state=state)
        if busy:
            self._prog_busy_start()
        else:
            self._prog_reset()

    def _resolve_log_path(self):
        p = self.v_log_path.get().strip()
        if p:
            return p
        base = self._resolve(self.out_path.get()) or TOOLDIR
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            base = TOOLDIR
        return os.path.join(base, "m8raw2dng2_%s.log" %
                            datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))

    def _attach_logging(self):
        root = logging.getLogger(core.PROG)
        for h in list(root.handlers):
            root.removeHandler(h)
        qh = QueueHandler(self.log_q)
        qh.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(qh)
        self._log_path = None
        if self.v_log_on.get():
            try:
                self._log_path = self._resolve_log_path()
                fh = logging.FileHandler(self._log_path, encoding="utf-8")
                fh.setFormatter(logging.Formatter("%(message)s"))
                root.addHandler(fh)
            except Exception as e:
                self.log_q.put(f"(could not open log file: {e})")
                self._log_path = None
        root.setLevel(logging.DEBUG if self.v_verbose.get() else logging.INFO)
        root.propagate = False
        for noisy in ("PIL", "tifffile", "PIL.TiffImagePlugin"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        self._show_log()

    def _guard_inputs(self):
        if not self._resolve(self.in_path.get()):
            messagebox.showwarning("No input", "Choose an input folder first.")
            return False
        return True

    def _run(self):
        if self.v_probe.get():
            self._probe(); return
        if not self._guard_inputs():
            return
        o = self._collect()
        self._refresh_cmd()
        self._attach_logging()
        if self._log_path:
            self.log_q.put(f"Log file: {self._log_path}")
        self._busy(True)

        def work():
            try:
                lensdb = core.parse_lensdb(core._db_path(o, "lensdb.ini"))
                sensdb = core.parse_sensdb(core._db_path(o, "sensdb.ini"))
                jobs = core.discover_jobs(o.inputs, o.recursive)
                if not jobs:
                    self.log_q.put("No RAW/DNG files found.")
                    return
                self.log_q.put(f"{len(jobs)} image(s) to process.")
                if o.dry_run:
                    for raw, jpg, bia in jobs:
                        self.log_q.put(f"would convert {raw} (jpg={bool(jpg)} bia={bool(bia)})")
                    return
                o.meter_offset_map = core.resolve_meter_offsets(jobs, o, sensdb)
                self.after(0, lambda n=len(jobs): self._prog_plan(n))
                ok = 0
                for (raw, jpg, bia) in jobs:
                    try:
                        if core.process_one(raw, jpg, bia, o, lensdb, sensdb):
                            ok += 1
                    except Exception as e:
                        self.log_q.put(f"FAILED {raw}: {e}")
                    self.after(0, self._prog_step)
                self.log_q.put(f"Done: {ok}/{len(jobs)} converted.")
            except Exception as e:
                self.log_q.put(f"ERROR: {e}")
            finally:
                self.after(0, lambda: self._busy(False))

        threading.Thread(target=work, daemon=True).start()

    def _probe(self):
        if not self._guard_inputs():
            return
        o = self._collect()
        self._attach_logging()
        target = o.inputs[0]
        if os.path.isdir(target):
            jobs = core.discover_jobs([target], o.recursive)
            if not jobs:
                self._log("No RAW/DNG files found to probe.")
                return
            targets = [j[0] for j in jobs]
        else:
            targets = [target]

        def work():
            try:
                if len(targets) > 1:
                    core.log.info("Probing %d file(s):\n", len(targets))
                for k, t in enumerate(targets):
                    core.probe(t, o)
                    if k != len(targets) - 1:
                        core.log.info("")
            except Exception as e:
                self.log_q.put(f"Probe error: {e}")
            finally:
                self.after(0, lambda: self._busy(False))

        if self._log_path:
            self.log_q.put(f"Log file: {self._log_path}")
        self._busy(True)
        threading.Thread(target=work, daemon=True).start()

    def _make_darkfield(self):
        folder = filedialog.askdirectory(
            title="Choose the folder of dark frames (ISO 160 darks)", initialdir=USERDIR)
        if not folder:
            return
        o = self._collect(for_darkfield=True)
        jobs = core.discover_jobs([folder], self.v_recursive.get())
        jobs = [(r, j, b) for (r, j, b) in jobs
                if os.path.splitext(r)[1].lower() == ".raw"]
        if not jobs:
            messagebox.showwarning(
                "No dark frames",
                "No dark .RAW frames found in that folder (darkfield creation needs RAW files).")
            return
        serial = None
        for (_raw, jpg, _b) in jobs:
            if jpg:
                jm = core.read_jpeg_meta(jpg)
                if jm and jm.get("serial"):
                    serial = jm["serial"]; break
        if not serial:
            serial = simpledialog.askstring(
                "Camera serial",
                "No .JPG sidecar was found (needed for the camera serial).\nEnter the serial "
                "for this darkfield's sensdb.ini section:", parent=self)
            if not serial:
                return
        if not messagebox.askyesno(
                "Create darkfield",
                "Analyse %d dark frame(s), skipping long exposures, and write/replace the [%s] "
                "block (LevelCorrection%s) in sensdb.ini.\n\nProceed?"
                % (len(jobs), serial, " + Line entries" if self.v_autolines.get() else "")):
            return
        self._attach_logging()
        sensdb_path = core._db_path(o, "sensdb.ini")
        sensdb = core.parse_sensdb(sensdb_path)

        def work():
            try:
                core.create_darkfield_smart(jobs, o, sensdb_path, sensdb, serial=serial)
            except Exception as e:
                self.log_q.put(f"Darkfield error: {e}")
            finally:
                self.after(0, lambda: (self._busy(False), self._refresh_badges()))

        self._busy(True)
        threading.Thread(target=work, daemon=True).start()

    def _calibrate_fnumber(self):
        aps = self.v_calib_aps.get().strip()
        if not aps:
            messagebox.showwarning(
                "No apertures",
                "Enter the apertures of your calibration frames, comma-separated, in filename "
                "order (e.g. 2.8,4,5.6,8,11) before calibrating.")
            return
        code = self.v_calib_lenscode.get().strip()
        if not code:
            messagebox.showwarning(
                "No lens code",
                "Select the 6-bit lens code for the lens you are calibrating (the dropdown is "
                "fed from lensdb.ini), so the offset is stored against the right (body, lens).")
            return
        folder = filedialog.askdirectory(
            title="Choose the folder of known-aperture calibration frames", initialdir=USERDIR)
        if not folder:
            return
        o = self._collect()
        o.lens = True
        o.lens_code = code
        o.calibrate_fnumber = aps
        jobs = core.discover_jobs([folder], self.v_recursive.get())
        jobs = [(r, j, b) for (r, j, b) in jobs
                if os.path.splitext(r)[1].lower() == ".raw"]
        if not jobs:
            messagebox.showwarning(
                "No frames",
                "No calibration .RAW frames found in that folder.")
            return
        n_ap = len([x for x in aps.replace(";", ",").split(",") if x.strip()])
        if n_ap != len(jobs):
            messagebox.showwarning(
                "Count mismatch",
                "You entered %d apertures but %d .RAW frame(s) were found. They must match "
                "one-to-one, in filename order." % (n_ap, len(jobs)))
            return
        if not messagebox.askyesno(
                "Calibrate aperture meter",
                "Derive the aperture-meter offset for this body + lens %s from %d frame(s) and store "
                "it in sensdb.ini (MeterOffset/ImageCalib under the body's section).\n\nProceed?"
                % (code, len(jobs))):
            return
        self._attach_logging()
        sensdb_path = core._db_path(o, "sensdb.ini")

        def work():
            try:
                core.calibrate_fnumber_offset(jobs, o, sensdb_path)
            except Exception as e:
                self.log_q.put(f"Calibration error: {e}")
            finally:
                self.after(0, lambda: (self._busy(False), self._refresh_badges()))

        self._busy(True)
        threading.Thread(target=work, daemon=True).start()


def main():
    if not core.HAVE_TIFFFILE:
        root = tk.Tk(); root.withdraw()
        messagebox.showerror(
            "Missing dependency",
            "tifffile is required.\n\nInstall with:\n    pip install numpy tifffile pillow")
        return
    root = tk.Tk()
    style = ttk.Style()
    for theme in ("aqua", "vista", "clam"):
        if theme in style.theme_names():
            try:
                style.theme_use(theme); break
            except Exception:
                continue
    App(root)
    root.minsize(680, 480)
    root.mainloop()


if __name__ == "__main__":
    main()
