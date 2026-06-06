"""
ClipMarker  -  Game clip auto-marker tool
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import time
import subprocess
import tempfile
import os
import cv2
import numpy as np
from pathlib import Path

from detector import SplatoonDetector, MarkerEvent, analyse_video, check_gpu, ocr_available
from exporter import save_csv, save_json, embed_chapters

try:
    from PIL import Image, ImageTk
    _PIL = True
except ImportError:
    _PIL = False

try:
    import pygame
    pygame.mixer.init(44100, -16, 2, 2048)
    _PYGAME = True
except Exception:
    _PYGAME = False

# Colors
BG      = "#1e1e24"
PANEL   = "#28282f"
CARD    = "#32323c"
ACCENT  = "#50c896"
ACCENT2 = "#e05a3a"
ACCENT3 = "#7b72e0"
FG      = "#e8e8ee"
FG2     = "#9090a0"
BORDER  = "#44444e"

GAME_PROFILES = {"Splatoon 2 / 3": SplatoonDetector}

EVENT_META = {
    "kill":    {"color": ACCENT,    "icon": "🎯", "label": "Kill"},
    "death":   {"color": ACCENT2,   "icon": "💀", "label": "Death"},
    "special": {"color": "#f0c040", "icon": "⭐", "label": "Special"},
    "manual":  {"color": ACCENT3,   "icon": "📌", "label": "Manual"},
}

# Seekbar geometry
SB_PAD     = 14
SB_HEIGHT  = 52
SB_TRACK_Y = 34
SB_TRACK_H = 8
SB_PIN_H   = 14
SB_HEAD_R  = 9


def _style_ttk():
    s = ttk.Style()
    s.theme_use("clam")
    s.configure("TCombobox", fieldbackground=CARD, background=CARD,
                foreground=FG, bordercolor=BORDER, arrowcolor=FG2,
                selectbackground=CARD, selectforeground=FG)
    s.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                foreground=FG, rowheight=26, borderwidth=0)
    s.configure("Treeview.Heading", background=CARD, foreground=FG2,
                font=("Segoe UI", 9), relief="flat", borderwidth=0)
    s.map("Treeview", background=[("selected", CARD)], foreground=[("selected", FG)])
    s.configure("TScrollbar", background=CARD, troughcolor=PANEL,
                bordercolor=PANEL, arrowcolor=FG2)
    s.configure("TScale", background=PANEL, troughcolor=CARD, sliderrelief="flat")
    s.configure("TProgressbar", background=ACCENT, troughcolor=CARD, bordercolor=PANEL)
    s.configure("TRadiobutton", background=PANEL, foreground=FG2,
                focuscolor=PANEL, font=("Segoe UI", 9))
    s.map("TRadiobutton", foreground=[("active", FG), ("selected", ACCENT)])


def _btn(parent, text, command, color=CARD, fg=FG, font_size=9, **kw):
    return tk.Button(parent, text=text, command=command, bg=color, fg=fg,
                     activebackground=BORDER, activeforeground=FG, relief="flat",
                     font=("Segoe UI", font_size), cursor="hand2", bd=0, **kw)


class ClipMarkerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ClipMarker")
        self.geometry("1240x740")
        self.minsize(900, 580)
        self.configure(bg=BG)
        _style_ttk()

        try:
            ico = Path(__file__).parent / "icon.ico"
            if ico.exists():
                self.iconbitmap(str(ico))
        except Exception:
            pass

        self._events = []
        self._video_path = ""
        self._analysing = False

        # player state
        self._cap = None
        self._playing = False
        self._cur_sec = 0.0
        self._video_duration = 0.0
        self._video_fps = 30.0
        self._speed = 1.0
        self._after_id = None
        self._photo_ref = None
        self._last_frame = None
        self._drag_seeking = False
        self._drag_was_playing = False
        self._hover_x = None

        # timing (wall-clock sync for accurate speed)
        self._play_start_wall = 0.0
        self._play_start_vid  = 0.0

        # audio
        self._audio_path = None

        self._build_ui()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Keys
    # ------------------------------------------------------------------

    def _bind_keys(self):
        self.bind("<space>",       lambda e: self._toggle_play())
        self.bind("<Left>",        lambda e: self._seek_relative(-5))
        self.bind("<Right>",       lambda e: self._seek_relative(5))
        self.bind("<Shift-Left>",  lambda e: self._seek_relative(-30))
        self.bind("<Shift-Right>", lambda e: self._seek_relative(30))
        self.bind("<Key-comma>",       lambda e: self._step_frame(-1))
        self.bind("<Key-period>",      lambda e: self._step_frame(1))

    def _seek_relative(self, delta):
        self._seek_to(self._cur_sec + delta)

    def _step_frame(self, direction):
        """Step one frame forward/backward (only when paused)."""
        if self._playing:
            return
        self._seek_to(self._cur_sec + direction / self._video_fps)

    # ------------------------------------------------------------------
    # UI build
    # ------------------------------------------------------------------

    def _build_ui(self):
        bar = tk.Frame(self, bg=PANEL, height=48)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Label(bar, text="● ClipMarker", bg=PANEL, fg=ACCENT,
                 font=("Segoe UI", 13, "bold"), padx=16).pack(side="left", pady=10)
        tk.Label(bar, text="Game:", bg=PANEL, fg=FG2,
                 font=("Segoe UI", 9)).pack(side="left", padx=(12, 4))
        self._game_var = tk.StringVar(value=list(GAME_PROFILES)[0])
        ttk.Combobox(bar, textvariable=self._game_var,
                     values=list(GAME_PROFILES), width=16,
                     state="readonly").pack(side="left", pady=12)

        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True, padx=10, pady=(6, 0))

        left = tk.Frame(main, bg=BG, width=275)
        right = tk.Frame(main, bg=BG)
        left.pack(side="left", fill="y", padx=(0, 6))
        left.pack_propagate(False)
        right.pack(side="left", fill="both", expand=True)

        self._build_left(left)
        self._build_right(right)

        self._status_var = tk.StringVar(value="Select a video file")
        sb = tk.Frame(self, bg=CARD, height=26)
        sb.pack(fill="x", side="bottom")
        sb.pack_propagate(False)
        tk.Label(sb, textvariable=self._status_var, bg=CARD, fg=FG2,
                 font=("Segoe UI", 9), anchor="w", padx=12).pack(fill="both")

    def _section(self, parent, title):
        tk.Label(parent, text=title.upper(), bg=BG, fg=FG2,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(10, 2))
        card = tk.Frame(parent, bg=PANEL, padx=12, pady=10)
        card.pack(fill="x")
        return card

    # ------------------------------------------------------------------
    # Left panel
    # ------------------------------------------------------------------

    def _build_left(self, parent):
        f = self._section(parent, "Video File")
        self._file_var = tk.StringVar(value="Not selected")
        tk.Label(f, textvariable=self._file_var, bg=PANEL, fg=FG2,
                 font=("Segoe UI", 9), anchor="w", wraplength=235).pack(fill="x")
        _btn(f, "  Open File  ", self._pick_file,
             color=CARD, padx=8, pady=5).pack(anchor="w", pady=(8, 0))

        s = self._section(parent, "Detection Settings")
        self._kill_dark   = self._slider(s, "Banner dark ratio (%)", 30, 90, 55)
        self._kill_bright = self._slider(s, "Text pixel ratio (%)",   1, 20,  3)
        self._cooldown    = self._slider(s, "Cooldown (sec)",          1, 10,  3)
        self._smpl_fps    = self._slider(s, "Sample FPS",              1, 30, 10)

        g = self._section(parent, "GPU Acceleration")
        self._gpu_var = tk.BooleanVar(value=False)
        row = tk.Frame(g, bg=PANEL)
        row.pack(fill="x")
        self._gpu_chk = tk.Checkbutton(
            row, text="Use GPU", variable=self._gpu_var,
            bg=PANEL, fg=FG, activebackground=PANEL, activeforeground=FG,
            selectcolor=CARD, font=("Segoe UI", 9))
        self._gpu_chk.pack(side="left")
        self._gpu_lbl = tk.StringVar(value="checking...")
        tk.Label(row, textvariable=self._gpu_lbl, bg=PANEL, fg=FG2,
                 font=("Segoe UI", 8)).pack(side="left", padx=6)
        self.after(300, self._probe_gpu)

        a = self._section(parent, "Analysis")
        self._analyse_btn = _btn(a, "▶  Start Analysis", self._start_analysis,
                                 color=ACCENT, fg="#fff", font_size=11, padx=0, pady=8)
        self._analyse_btn.pack(fill="x", pady=(0, 6))
        self._analyse_btn.config(state="disabled", disabledforeground="#aaa", bg="#2a5a3a")
        self._progress = ttk.Progressbar(a, mode="determinate", maximum=100)
        self._progress.pack(fill="x")

        # OCR status
        ocr_row = tk.Frame(a, bg=PANEL)
        ocr_row.pack(fill="x", pady=(4, 0))
        if ocr_available():
            ocr_txt = "OCR mode: EasyOCR  (model loads on first analysis)"
            ocr_col = ACCENT
        else:
            ocr_txt = "OCR: easyocr not installed  (pip install easyocr)"
            ocr_col = "#e0a030"
        tk.Label(ocr_row, text=ocr_txt, bg=PANEL, fg=ocr_col,
                 font=("Segoe UI", 8)).pack(anchor="w")

        e = self._section(parent, "Export")
        row2 = tk.Frame(e, bg=PANEL)
        row2.pack(fill="x")
        for lbl, cmd in [("CSV", self._export_csv),
                         ("JSON", self._export_json),
                         ("Video+Chapters", self._export_video)]:
            _btn(row2, lbl, cmd, color=CARD, padx=8, pady=4).pack(side="left", padx=(0, 4))

    def _slider(self, parent, label, lo, hi, default):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, bg=PANEL, fg=FG2,
                 font=("Segoe UI", 9), width=22, anchor="w").pack(side="left")
        var = tk.DoubleVar(value=default)
        tk.Label(row, textvariable=var, bg=PANEL, fg=FG,
                 font=("Segoe UI", 9, "bold"), width=4).pack(side="right")
        ttk.Scale(row, from_=lo, to=hi, variable=var, orient="horizontal",
                  command=lambda v, vv=var: vv.set(round(float(v), 1))
                  ).pack(side="left", fill="x", expand=True, padx=4)
        return var

    def _probe_gpu(self):
        def run():
            info = check_gpu()
            if info["cuda"] or info["opencl"]:
                self._gpu_lbl.set("({})".format(info["label"]))
            else:
                self._gpu_lbl.set("(no GPU found)")
                self.after(0, lambda: self._gpu_chk.config(state="disabled"))
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------
    # Right panel
    # ------------------------------------------------------------------

    def _build_right(self, parent):
        paned = ttk.PanedWindow(parent, orient="vertical")
        paned.pack(fill="both", expand=True)

        # --- top: video + controls + seekbar ---
        top = tk.Frame(paned, bg=BG)
        paned.add(top, weight=3)

        self._video_canvas = tk.Canvas(top, bg="#000", highlightthickness=0)
        self._video_canvas.pack(fill="both", expand=True)
        self._video_canvas.bind("<Configure>", self._on_canvas_resize)
        self._ph_id = self._video_canvas.create_text(
            400, 180, text="Select a video file to begin",
            fill=FG2, font=("Segoe UI", 13))

        # controls row
        ctrl = tk.Frame(top, bg=PANEL)
        ctrl.pack(fill="x")

        self._play_btn = _btn(ctrl, "▶", self._toggle_play,
                              color=CARD, font_size=15, padx=12, pady=5)
        self._play_btn.pack(side="left", padx=(6, 2), pady=4)
        self._play_btn.config(state="disabled")

        # volume
        if _PYGAME:
            tk.Label(ctrl, text="🔊", bg=PANEL, fg=FG2,
                     font=("Segoe UI", 10)).pack(side="left", padx=(6, 2))
            self._vol_var = tk.DoubleVar(value=1.0)
            vol_sl = ttk.Scale(ctrl, from_=0.0, to=1.0, variable=self._vol_var,
                               orient="horizontal", length=60,
                               command=lambda v: pygame.mixer.music.set_volume(float(v)))
            vol_sl.pack(side="left", padx=(0, 6), pady=4)

        self._time_var = tk.StringVar(value="--:-- / --:--")
        tk.Label(ctrl, textvariable=self._time_var, bg=PANEL, fg=FG,
                 font=("Courier New", 10)).pack(side="left", padx=8)

        # audio indicator
        audio_txt = "♪" if _PYGAME else "♪ (no pygame)"
        self._audio_lbl = tk.Label(ctrl, text=audio_txt, bg=PANEL, fg=FG2,
                                   font=("Segoe UI", 9))
        self._audio_lbl.pack(side="left", padx=4)

        # speed buttons (right)
        tk.Label(ctrl, text="Speed", bg=PANEL, fg=FG2,
                 font=("Segoe UI", 8)).pack(side="right", padx=(0, 8))
        for spd, lbl in [(2.0, "2×"), (1.5, "1.5×"), (1.0, "1×"), (0.5, "½×")]:
            _btn(ctrl, lbl, lambda s=spd: self._set_speed(s),
                 color=CARD, font_size=8, padx=6, pady=3
                 ).pack(side="right", padx=1, pady=4)

        # key hint
        tk.Label(ctrl, text="Space/←→/,.frame", bg=PANEL, fg="#555",
                 font=("Segoe UI", 7)).pack(side="right", padx=8)

        # seekbar
        sb_f = tk.Frame(top, bg=PANEL)
        sb_f.pack(fill="x")
        self._seekbar = tk.Canvas(sb_f, bg=PANEL, height=SB_HEIGHT,
                                  highlightthickness=0, cursor="hand2")
        self._seekbar.pack(fill="x", padx=4, pady=(0, 4))
        self._seekbar.bind("<Configure>",       lambda e: self._draw_seekbar())
        self._seekbar.bind("<Button-1>",        self._sb_press)
        self._seekbar.bind("<B1-Motion>",       self._sb_drag)
        self._seekbar.bind("<ButtonRelease-1>", self._sb_release)
        self._seekbar.bind("<Motion>",          self._sb_hover)
        self._seekbar.bind("<Leave>",           self._sb_leave)

        # --- bottom: events list ---
        bot = tk.Frame(paned, bg=BG)
        paned.add(bot, weight=2)

        hdr = tk.Frame(bot, bg=BG)
        hdr.pack(fill="x", pady=(6, 0))
        tk.Label(hdr, text="Detected Events", bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self._event_count = tk.Label(hdr, text="", bg=BG, fg=FG2,
                                     font=("Segoe UI", 9))
        self._event_count.pack(side="left", padx=8)
        _btn(hdr, "+ Add Manual", self._manual_add,
             color=CARD, padx=8, pady=3).pack(side="right")

        filt = tk.Frame(bot, bg=BG)
        filt.pack(fill="x", pady=(4, 0))
        self._filter_var = tk.StringVar(value="all")
        for val, lbl in [("all", "All"), ("kill", "Kill"),
                         ("death", "Death"), ("manual", "Manual")]:
            ttk.Radiobutton(filt, text=lbl, variable=self._filter_var,
                            value=val, command=self._refresh_list
                            ).pack(side="left", padx=(0, 10))

        lf = tk.Frame(bot, bg=PANEL, padx=1, pady=1)
        lf.pack(fill="both", expand=True, pady=4)
        cols = ("time", "type", "conf")
        self._tree = ttk.Treeview(lf, columns=cols, show="headings", selectmode="browse")
        self._tree.heading("time", text="Timecode")
        self._tree.heading("type", text="Event")
        self._tree.heading("conf", text="Confidence")
        self._tree.column("time", width=115, anchor="center", minwidth=90)
        self._tree.column("type", width=130, anchor="w",      minwidth=90)
        self._tree.column("conf", width=75,  anchor="center", minwidth=60)
        tv_sb = ttk.Scrollbar(lf, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=tv_sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        tv_sb.pack(side="right", fill="y")
        self._tree.bind("<Delete>",           self._delete_selected)
        self._tree.bind("<Double-1>",         self._edit_selected)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        for etype, meta in EVENT_META.items():
            self._tree.tag_configure(etype, foreground=meta["color"])

        self._detail_var = tk.StringVar(value="Select an event to see details")
        tk.Label(bot, textvariable=self._detail_var, bg=CARD, fg=FG2,
                 font=("Segoe UI", 9), anchor="w", padx=10, pady=5).pack(fill="x")

    # ------------------------------------------------------------------
    # Video player
    # ------------------------------------------------------------------

    def _open_video(self, path):
        self._player_stop()
        if self._cap:
            self._cap.release()
            self._cap = None
        self._cleanup_audio()

        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return
        self._cap = cap
        self._video_fps      = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total                = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        self._video_duration = total / self._video_fps if total > 0 else 0.0
        self._cur_sec        = 0.0

        ok, frame = cap.read()
        if ok:
            self._last_frame = frame
            self._show_frame(frame)
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

        self._play_btn.config(state="normal")
        self._update_time_label()
        self._draw_seekbar()

        # extract audio in background
        if _PYGAME:
            threading.Thread(target=self._extract_audio_bg,
                             args=(path,), daemon=True).start()

    def _extract_audio_bg(self, path):
        """Extract audio with ffmpeg to temp WAV (runs in thread)."""
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", path,
                 "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
                 tmp.name],
                capture_output=True, timeout=60)
            if result.returncode == 0 and os.path.getsize(tmp.name) > 0:
                pygame.mixer.music.load(tmp.name)
                self._audio_path = tmp.name
                self.after(0, lambda: self._audio_lbl.config(text="♪ ready", fg=ACCENT))
            else:
                os.unlink(tmp.name)
                self.after(0, lambda: self._audio_lbl.config(text="♪ no audio", fg=FG2))
        except FileNotFoundError:
            self.after(0, lambda: self._audio_lbl.config(text="♪ (ffmpeg needed)", fg=FG2))
        except Exception:
            self.after(0, lambda: self._audio_lbl.config(text="♪ error", fg=FG2))

    def _cleanup_audio(self):
        if _PYGAME:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
        if self._audio_path and os.path.exists(self._audio_path):
            try:
                os.unlink(self._audio_path)
            except Exception:
                pass
        self._audio_path = None

    def _toggle_play(self):
        if not self._cap:
            return
        if self._playing:
            self._player_pause()
        else:
            self._player_play()

    def _player_play(self):
        if not self._cap:
            return
        if self._video_duration > 0 and self._cur_sec >= self._video_duration - 0.05:
            self._seek_to(0.0)

        # start audio
        if _PYGAME and self._audio_path:
            try:
                pygame.mixer.music.play(start=self._cur_sec)
                pygame.mixer.music.set_volume(
                    self._vol_var.get() if hasattr(self, "_vol_var") else 1.0)
            except Exception:
                try:
                    pygame.mixer.music.play()
                    pygame.mixer.music.set_pos(self._cur_sec)
                except Exception:
                    pass

        # set timing reference
        self._play_start_wall = time.monotonic()
        self._play_start_vid  = self._cur_sec
        self._playing         = True
        self._play_btn.config(text="⏸")
        self._after_id = self.after(16, self._next_frame)

    def _player_pause(self):
        self._playing = False
        self._play_btn.config(text="▶")
        if self._after_id:
            self.after_cancel(self._after_id)
            self._after_id = None
        if _PYGAME and self._audio_path:
            try:
                pygame.mixer.music.pause()
            except Exception:
                pass

    def _player_stop(self):
        self._player_pause()
        self._cur_sec = 0.0

    def _next_frame(self):
        """Wall-clock-synced frame update: skips frames when behind, never slows down."""
        if not self._playing or not self._cap:
            return

        # current target time from wall clock
        elapsed    = (time.monotonic() - self._play_start_wall) * self._speed
        target_sec = self._play_start_vid + elapsed

        if target_sec >= self._video_duration > 0:
            self._player_pause()
            return

        # fast-grab to skip frames if we're behind
        frame_dur = 1.0 / self._video_fps
        pos = self._cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        while pos < target_sec - frame_dur:
            if not self._cap.grab():
                self._player_pause()
                return
            pos = self._cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0

        ok, frame = self._cap.read()
        if not ok:
            self._player_pause()
            return

        self._cur_sec    = self._cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        self._last_frame = frame
        self._show_frame(frame)
        self._update_time_label()
        self._draw_seekbar()

        self._after_id = self.after(16, self._next_frame)  # ~60 fps poll

    def _show_frame(self, frame):
        if not _PIL:
            return
        cw = self._video_canvas.winfo_width()
        ch = self._video_canvas.winfo_height()
        if cw < 2 or ch < 2:
            return
        fh, fw = frame.shape[:2]
        scale  = min(cw / fw, ch / fh)
        nw, nh = int(fw * scale), int(fh * scale)
        rgb    = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img    = Image.fromarray(rgb).resize((nw, nh), Image.BILINEAR)
        photo  = ImageTk.PhotoImage(img)
        self._video_canvas.itemconfigure(self._ph_id, state="hidden")
        self._video_canvas.delete("frame")
        self._video_canvas.create_image(cw // 2, ch // 2, image=photo,
                                        anchor="center", tags="frame")
        self._photo_ref = photo

    def _on_canvas_resize(self, _=None):
        if self._last_frame is not None:
            self._show_frame(self._last_frame)

    def _seek_to(self, sec):
        if not self._cap:
            return
        sec = max(0.0, min(sec, self._video_duration))
        self._cur_sec = sec
        self._cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000)

        if self._playing:
            # reset timing reference so playback continues from new position
            self._play_start_wall = time.monotonic()
            self._play_start_vid  = sec
            # restart audio from new position
            if _PYGAME and self._audio_path:
                try:
                    pygame.mixer.music.play(start=sec)
                    pygame.mixer.music.set_volume(
                        self._vol_var.get() if hasattr(self, "_vol_var") else 1.0)
                except Exception:
                    pass

        ok, frame = self._cap.read()
        if ok:
            self._last_frame = frame
            self._show_frame(frame)
            self._cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000)  # rewind after read

        self._update_time_label()
        self._draw_seekbar()

    def _set_speed(self, spd):
        self._speed = spd
        if self._playing:
            # re-anchor wall clock so speed change is seamless
            self._play_start_wall = time.monotonic()
            self._play_start_vid  = self._cur_sec
            # audio doesn't support speed change easily; just resync position
            if _PYGAME and self._audio_path:
                try:
                    pygame.mixer.music.play(start=self._cur_sec)
                except Exception:
                    pass

    def _update_time_label(self):
        def f(s):
            return "{:02d}:{:02d}".format(int(s // 60), int(s % 60))
        self._time_var.set("{} / {}".format(f(self._cur_sec), f(self._video_duration)))

    # ------------------------------------------------------------------
    # Seekbar
    # ------------------------------------------------------------------

    def _track_x(self):
        w = self._seekbar.winfo_width()
        return SB_PAD, w - SB_PAD

    def _sec_to_x(self, sec):
        x0, x1 = self._track_x()
        if self._video_duration <= 0:
            return x0
        return x0 + int(sec / self._video_duration * (x1 - x0))

    def _x_to_sec(self, x):
        x0, x1 = self._track_x()
        if x1 <= x0:
            return 0.0
        return max(0.0, min(1.0, (x - x0) / (x1 - x0))) * self._video_duration

    @staticmethod
    def _fmt_sec(s):
        return "{:02d}:{:05.2f}".format(int(s // 60), s % 60)

    def _draw_seekbar(self, hover_x=None):
        c  = self._seekbar
        c.delete("all")
        w  = c.winfo_width()
        if w < 4:
            return
        x0, x1 = self._track_x()
        ty = SB_TRACK_Y
        th = SB_TRACK_H

        # track background (rounded ends via two rects + ovals)
        r = th // 2
        c.create_rectangle(x0 + r, ty - r, x1 - r, ty + r, fill=CARD, outline="")
        c.create_oval(x0, ty - r, x0 + th, ty + r, fill=CARD, outline="")
        c.create_oval(x1 - th, ty - r, x1, ty + r, fill=CARD, outline="")

        # progress fill
        if self._video_duration > 0 and self._cur_sec > 0:
            px = self._sec_to_x(self._cur_sec)
            if px > x0 + r:
                c.create_rectangle(x0 + r, ty - r, px, ty + r, fill="#3a6e56", outline="")
                c.create_oval(x0, ty - r, x0 + th, ty + r, fill="#3a6e56", outline="")

        # hover region highlight (before cursor)
        if hover_x is not None and self._video_duration > 0:
            hx = max(x0, min(hover_x, x1))
            if hx > x0 + r:
                c.create_rectangle(x0 + r, ty - r, hx, ty + r, fill="#4a8a6a", outline="")
                c.create_oval(x0, ty - r, x0 + th, ty + r, fill="#4a8a6a", outline="")

        # event marker pins
        if self._video_duration > 0:
            for ev in self._events:
                mx    = self._sec_to_x(ev.time_sec)
                color = EVENT_META.get(ev.event_type, EVENT_META["manual"])["color"]
                c.create_line(mx, ty - r - SB_PIN_H, mx, ty + r + 2,
                              fill=color, width=2)
                pr = 5
                c.create_oval(mx - pr, ty - r - SB_PIN_H - pr,
                              mx + pr, ty - r - SB_PIN_H + pr,
                              fill=color, outline="")

        # playhead
        if self._video_duration > 0:
            hx = self._sec_to_x(self._cur_sec)
            hr = SB_HEAD_R
            # shadow
            c.create_oval(hx - hr + 1, ty - hr + 1, hx + hr + 1, ty + hr + 1,
                         fill="#000", outline="")
            c.create_oval(hx - hr, ty - hr, hx + hr, ty + hr,
                         fill=FG, outline=PANEL, width=2)

        # hover: cursor line + time label
        if hover_x is not None and self._video_duration > 0:
            hx = max(x0, min(hover_x, x1))
            sec_h = self._x_to_sec(hx)
            # marker tooltip check
            hit = None
            for ev in self._events:
                if abs(hover_x - self._sec_to_x(ev.time_sec)) < 8:
                    hit = ev
                    break
            # time label above cursor
            label = self._fmt_sec(sec_h)
            if hit:
                meta  = EVENT_META.get(hit.event_type, EVENT_META["manual"])
                label = "{} {}  {}".format(meta["icon"], hit.label, label)
            lx = max(x0 + 30, min(hx, x1 - 30))
            c.create_rectangle(lx - 36, 2, lx + 36, 18,
                               fill=CARD, outline=BORDER)
            c.create_text(lx, 10, text=label, fill=FG,
                          font=("Segoe UI", 8), anchor="center")

    def _sb_press(self, event):
        if not self._cap:
            return
        self._drag_seeking     = True
        self._drag_was_playing = self._playing
        if self._playing:
            # pause audio but keep playing flag for visual
            if _PYGAME and self._audio_path:
                try:
                    pygame.mixer.music.pause()
                except Exception:
                    pass
            self._playing = False
            if self._after_id:
                self.after_cancel(self._after_id)
                self._after_id = None
        self._seek_to(self._x_to_sec(event.x))

    def _sb_drag(self, event):
        if self._drag_seeking and self._cap:
            self._seek_to(self._x_to_sec(event.x))
            self._draw_seekbar(hover_x=event.x)

    def _sb_release(self, event):
        if self._drag_seeking:
            self._drag_seeking = False
            if self._drag_was_playing:
                self._player_play()

    def _sb_hover(self, event):
        self._hover_x = event.x
        self._draw_seekbar(hover_x=event.x)

    def _sb_leave(self, _=None):
        self._hover_x = None
        self._draw_seekbar()

    # ------------------------------------------------------------------
    # File pick
    # ------------------------------------------------------------------

    def _pick_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.ts"), ("All", "*.*")])
        if not path:
            return
        self._video_path = path
        self._file_var.set(Path(path).name)
        self._analyse_btn.config(state="normal", bg=ACCENT)
        self._status_var.set("Ready: " + Path(path).name)
        self._audio_lbl.config(text="♪ extracting...", fg=FG2)
        self._open_video(path)

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def _start_analysis(self):
        if not self._video_path or self._analysing:
            return
        self._analysing = True
        self._events.clear()
        self._tree.delete(*self._tree.get_children())
        self._analyse_btn.config(state="disabled", bg="#2a5a3a")
        self._progress["value"] = 0
        self._status_var.set("Analysing...")
        use_gpu = self._gpu_var.get()

        def run():
            det = SplatoonDetector(
                kill_dark_ratio=self._kill_dark.get() / 100.0,
                kill_bright_ratio=self._kill_bright.get() / 100.0,
                cooldown_sec=self._cooldown.get(),
                use_gpu=use_gpu,
            )
            try:
                evs = analyse_video(
                    self._video_path, det,
                    sample_fps=self._smpl_fps.get(),
                    progress_cb=lambda f: self._set_progress(f * 100),
                    use_gpu=use_gpu,
                )
                self._events.extend(evs)
                self.after(0, self._on_done)
            except Exception as ex:
                self.after(0, lambda: messagebox.showerror("Error", str(ex)))
                self.after(0, self._reset_analyse)

        threading.Thread(target=run, daemon=True).start()

    def _set_progress(self, val):
        try:
            self._progress["value"] = val
            self.update_idletasks()
        except Exception:
            pass

    def _on_done(self):
        self._refresh_list()
        self._draw_seekbar()
        kills  = sum(1 for e in self._events if e.event_type == "kill")
        deaths = sum(1 for e in self._events if e.event_type == "death")
        self._status_var.set(
            "Done  -  {} events  (kills: {}  deaths: {})".format(
                len(self._events), kills, deaths))
        self._reset_analyse()

    def _reset_analyse(self):
        self._analysing = False
        self._analyse_btn.config(state="normal", bg=ACCENT)
        self._progress["value"] = 100

    # ------------------------------------------------------------------
    # Event list
    # ------------------------------------------------------------------

    def _refresh_list(self):
        self._tree.delete(*self._tree.get_children())
        filt  = self._filter_var.get()
        shown = [e for e in self._events if filt == "all" or e.event_type == filt]
        for e in sorted(shown, key=lambda x: x.time_sec):
            h  = int(e.time_sec // 3600)
            m  = int((e.time_sec % 3600) // 60)
            s  = e.time_sec % 60
            tc = "{:02d}:{:02d}:{:05.2f}".format(h, m, s)
            meta = EVENT_META.get(e.event_type, EVENT_META["manual"])
            self._tree.insert("", "end",
                              values=(tc, "{}  {}".format(meta["icon"], e.label),
                                      "{:.0%}".format(e.confidence)),
                              tags=(e.event_type,), iid=str(id(e)))
        self._event_count.config(text="{} events".format(len(shown)))

    def _on_select(self, _=None):
        sel = self._tree.selection()
        if not sel:
            return
        ev = next((e for e in self._events if str(id(e)) == sel[0]), None)
        if not ev:
            return
        self._detail_var.set(
            "Type: {}  |  Time: {:.3f}s  |  Conf: {:.1%}  |  Label: {}".format(
                ev.event_type, ev.time_sec, ev.confidence, ev.label))
        self._seek_to(ev.time_sec)

    def _delete_selected(self, _=None):
        sel = self._tree.selection()
        if sel:
            self._events = [e for e in self._events if str(id(e)) != sel[0]]
            self._refresh_list()
            self._draw_seekbar()

    def _edit_selected(self, _=None):
        sel = self._tree.selection()
        if not sel:
            return
        ev = next((e for e in self._events if str(id(e)) == sel[0]), None)
        if not ev:
            return
        dlg = tk.Toplevel(self)
        dlg.title("Edit Label")
        dlg.geometry("320x130")
        dlg.resizable(False, False)
        dlg.configure(bg=PANEL)
        tk.Label(dlg, text="Label:", bg=PANEL, fg=FG2,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(16, 4))
        var = tk.StringVar(value=ev.label)
        ent = tk.Entry(dlg, textvariable=var, bg=CARD, fg=FG,
                       insertbackground=FG, relief="flat", font=("Segoe UI", 10))
        ent.pack(fill="x", padx=16)
        ent.focus()
        def save():
            ev.label = var.get()
            self._refresh_list()
            dlg.destroy()
        _btn(dlg, "Save", save, color=ACCENT, fg="#fff",
             padx=16, pady=5).pack(pady=12)
        dlg.bind("<Return>", lambda _: save())

    def _manual_add(self):
        dlg = tk.Toplevel(self)
        dlg.title("Add Manual Marker")
        dlg.geometry("340x220")
        dlg.resizable(False, False)
        dlg.configure(bg=PANEL)
        cur_tc = "00:00:00.00"
        if self._video_duration > 0:
            cur_tc = "00:{:02d}:{:05.2f}".format(int(self._cur_sec // 60),
                                                   self._cur_sec % 60)
        def row(lbl):
            tk.Label(dlg, text=lbl, bg=PANEL, fg=FG2,
                     font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(10, 2))
        row("Timecode  (HH:MM:SS.mm)")
        tc_var = tk.StringVar(value=cur_tc)
        tk.Entry(dlg, textvariable=tc_var, bg=CARD, fg=FG, insertbackground=FG,
                 relief="flat", width=18, font=("Segoe UI", 10)).pack(anchor="w", padx=16)
        row("Type")
        type_var = tk.StringVar(value="manual")
        ttk.Combobox(dlg, textvariable=type_var, values=list(EVENT_META.keys()),
                     state="readonly", width=14).pack(anchor="w", padx=16)
        row("Label (optional)")
        lbl_var = tk.StringVar()
        tk.Entry(dlg, textvariable=lbl_var, bg=CARD, fg=FG, insertbackground=FG,
                 relief="flat", width=24, font=("Segoe UI", 10)).pack(anchor="w", padx=16)
        def add():
            try:
                parts = tc_var.get().replace(",", ".").split(":")
                sec   = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            except Exception:
                messagebox.showerror("Error", "Use HH:MM:SS.mm format", parent=dlg)
                return
            et = type_var.get()
            ev = MarkerEvent(sec, et, 1.0, lbl_var.get() or et)
            self._events.append(ev)
            self._refresh_list()
            self._draw_seekbar()
            dlg.destroy()
        _btn(dlg, "Add", add, color=ACCENT3, fg="#fff", padx=16, pady=5).pack(pady=10)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _export_csv(self):
        if not self._events:
            messagebox.showinfo("Info", "No events")
            return
        p = filedialog.asksaveasfilename(defaultextension=".csv",
                                          filetypes=[("CSV", "*.csv")])
        if p:
            save_csv(self._events, p)
            self._status_var.set("CSV saved: " + Path(p).name)

    def _export_json(self):
        if not self._events:
            messagebox.showinfo("Info", "No events")
            return
        p = filedialog.asksaveasfilename(defaultextension=".json",
                                          filetypes=[("JSON", "*.json")])
        if p:
            save_json(self._events, p)
            self._status_var.set("JSON saved: " + Path(p).name)

    def _export_video(self):
        if not self._events or not self._video_path:
            return
        p = filedialog.asksaveasfilename(defaultextension=".mp4",
                                          filetypes=[("MP4", "*.mp4")])
        if not p:
            return
        self._status_var.set("Converting video...")
        def run():
            try:
                embed_chapters(self._video_path, self._events, p)
                self.after(0, lambda: self._status_var.set(
                    "Video saved: " + Path(p).name))
            except FileNotFoundError:
                self.after(0, lambda: messagebox.showerror(
                    "ffmpeg Error",
                    "ffmpeg.exe not found.\nInstall from https://ffmpeg.org"))
            except Exception as ex:
                self.after(0, lambda: messagebox.showerror("Error", str(ex)))
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _on_close(self):
        self._player_stop()
        if self._cap:
            self._cap.release()
        self._cleanup_audio()
        if _PYGAME:
            try:
                pygame.mixer.quit()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    app = ClipMarkerApp()
    app.mainloop()
