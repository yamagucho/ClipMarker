"""
ClipMarker  ─  ゲームクリップ自動マーカーツール
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import os
import cv2
import numpy as np
from pathlib import Path

from detector import SplatoonDetector, MarkerEvent, analyse_video
from exporter import save_csv, save_json, embed_chapters

try:
    from PIL import Image, ImageTk
    _PIL = True
except ImportError:
    _PIL = False

# ── テーマカラー ──────────────────────────────────────────────────────────────
BG       = "#1e1e24"   # ウィンドウ背景
PANEL    = "#28282f"   # パネル背景
CARD     = "#32323c"   # カード・入力欄背景
ACCENT   = "#50c896"   # グリーンアクセント（キル）
ACCENT2  = "#e05a3a"   # レッドアクセント（デス）
ACCENT3  = "#7b72e0"   # パープル（手動）
FG       = "#e8e8ee"   # メインテキスト
FG2      = "#9090a0"   # サブテキスト
BORDER   = "#44444e"   # ボーダー

GAME_PROFILES = {
    "Splatoon 2 / 3": SplatoonDetector,
}

EVENT_META = {
    "kill":   {"color": ACCENT,  "icon": "🎯", "label": "キル"},
    "death":  {"color": ACCENT2, "icon": "💀", "label": "デス"},
    "special":{"color": "#f0c040","icon": "⭐", "label": "スペシャル"},
    "manual": {"color": ACCENT3, "icon": "📌", "label": "手動"},
}

def _style_ttk():
    s = ttk.Style()
    s.theme_use("clam")
    s.configure("TCombobox",
                fieldbackground=CARD, background=CARD,
                foreground=FG, bordercolor=BORDER,
                arrowcolor=FG2, selectbackground=CARD,
                selectforeground=FG)
    s.configure("Treeview",
                background=PANEL, fieldbackground=PANEL,
                foreground=FG, rowheight=26, borderwidth=0)
    s.configure("Treeview.Heading",
                background=CARD, foreground=FG2,
                font=("Segoe UI", 9), relief="flat", borderwidth=0)
    s.map("Treeview", background=[("selected", CARD)],
          foreground=[("selected", FG)])
    s.configure("TScrollbar", background=CARD, troughcolor=PANEL,
                bordercolor=PANEL, arrowcolor=FG2)
    s.configure("TScale", background=PANEL, troughcolor=CARD,
                sliderrelief="flat")
    s.configure("TProgressbar", background=ACCENT, troughcolor=CARD,
                bordercolor=PANEL)
    s.configure("TRadiobutton", background=PANEL, foreground=FG2,
                focuscolor=PANEL, font=("Segoe UI", 9))
    s.map("TRadiobutton",
          foreground=[("active", FG), ("selected", ACCENT)])
    s.configure("TSeparator", background=BORDER)


def _btn(parent, text, command, color=CARD, fg=FG, font_size=9, **kw):
    return tk.Button(parent, text=text, command=command,
                     bg=color, fg=fg, activebackground=BORDER,
                     activeforeground=FG, relief="flat",
                     font=("Segoe UI", font_size),
                     cursor="hand2", bd=0, **kw)


class ClipMarkerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ClipMarker")
        self.geometry("980x660")
        self.minsize(720, 500)
        self.configure(bg=BG)

        _style_ttk()

        # アイコン設定（Windowsのみ有効）
        try:
            icon_path = Path(__file__).parent / "icon.ico"
            if icon_path.exists():
                self.iconbitmap(str(icon_path))
        except Exception:
            pass

        self._events: list[MarkerEvent] = []
        self._video_path: str = ""
        self._analysing = False

        self._build_ui()

    # ─── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # タイトルバー
        bar = tk.Frame(self, bg=PANEL, height=48)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        tk.Label(bar, text="● ClipMarker", bg=PANEL, fg=ACCENT,
                 font=("Segoe UI", 13, "bold"), padx=16).pack(side="left", pady=10)

        tk.Label(bar, text="ゲーム:", bg=PANEL, fg=FG2,
                 font=("Segoe UI", 9)).pack(side="left", padx=(12, 4))
        self._game_var = tk.StringVar(value=list(GAME_PROFILES)[0])
        cb = ttk.Combobox(bar, textvariable=self._game_var,
                          values=list(GAME_PROFILES), width=16, state="readonly")
        cb.pack(side="left", pady=12)

        # メイン2カラム
        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True, padx=10, pady=(6, 0))

        left  = tk.Frame(main, bg=BG, width=280)
        right = tk.Frame(main, bg=BG)
        left.pack(side="left", fill="y", padx=(0, 6))
        left.pack_propagate(False)
        right.pack(side="left", fill="both", expand=True)

        self._build_left(left)
        self._build_right(right)

        # ステータスバー
        self._status_var = tk.StringVar(value="動画ファイルを選択してください")
        sb = tk.Frame(self, bg=CARD, height=26)
        sb.pack(fill="x", side="bottom")
        sb.pack_propagate(False)
        tk.Label(sb, textvariable=self._status_var, bg=CARD, fg=FG2,
                 font=("Segoe UI", 9), anchor="w", padx=12).pack(fill="both")

    def _section(self, parent, title):
        tk.Label(parent, text=title.upper(), bg=BG, fg=FG2,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(10,2))
        card = tk.Frame(parent, bg=PANEL, padx=12, pady=10)
        card.pack(fill="x")
        return card

    def _build_left(self, parent):
        # ── ファイル ──────────────────────────────────────────────────────────
        f = self._section(parent, "動画ファイル")

        self._file_var = tk.StringVar(value="未選択")
        tk.Label(f, textvariable=self._file_var, bg=PANEL, fg=FG2,
                 font=("Segoe UI", 9), anchor="w", wraplength=240).pack(fill="x")

        _btn(f, "  📂  ファイルを開く  ", self._pick_file,
             color=CARD, padx=8, pady=5).pack(anchor="w", pady=(8,0))

        # サムネイル
        if _PIL:
            self._thumb_frame = tk.Frame(parent, bg="#111", height=90)
            self._thumb_frame.pack(fill="x", pady=4)
            self._thumb_frame.pack_propagate(False)
            self._thumb_label = tk.Label(self._thumb_frame, bg="#111")
            self._thumb_label.pack(fill="both", expand=True)

        # ── 検出設定 ──────────────────────────────────────────────────────────
        s = self._section(parent, "検出設定")
        self._kill_dark = self._slider(s, "バナー暗部比率 (%)",  30, 90, 55)
        self._kill_diff = self._slider(s, "出現差分閾値",        3,  40, 12)
        self._death_thr = self._slider(s, "デス感度",            5,  60, 30)
        self._cooldown  = self._slider(s, "クールダウン (秒)",   1,  10,  3)
        self._smpl_fps  = self._slider(s, "サンプルFPS",         1,  30, 10)

        # ── 解析ボタン ────────────────────────────────────────────────────────
        a = self._section(parent, "解析")
        self._analyse_btn = _btn(a, "▶  解析開始", self._start_analysis,
                                 color=ACCENT, fg="#fff",
                                 font_size=11, padx=0, pady=8)
        self._analyse_btn.pack(fill="x", pady=(0,6))
        self._analyse_btn.config(state="disabled",
                                 disabledforeground="#aaa",
                                 bg="#2a5a3a")

        self._progress = ttk.Progressbar(a, mode="determinate", maximum=100,
                                         style="TProgressbar")
        self._progress.pack(fill="x")

        # ── エクスポート ──────────────────────────────────────────────────────
        e = self._section(parent, "エクスポート")
        row = tk.Frame(e, bg=PANEL)
        row.pack(fill="x")
        for label, cmd in [("CSV", self._export_csv),
                           ("JSON", self._export_json),
                           ("動画+チャプター", self._export_video)]:
            _btn(row, label, cmd, color=CARD, padx=8, pady=4).pack(
                side="left", padx=(0,4))

    def _slider(self, parent, label, lo, hi, default):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, bg=PANEL, fg=FG2,
                 font=("Segoe UI", 9), width=20, anchor="w").pack(side="left")
        var = tk.DoubleVar(value=default)
        lbl = tk.Label(row, textvariable=var, bg=PANEL, fg=FG,
                       font=("Segoe UI", 9, "bold"), width=5)
        lbl.pack(side="right")
        sl = ttk.Scale(row, from_=lo, to=hi, variable=var,
                       orient="horizontal",
                       command=lambda v, vv=var: vv.set(round(float(v), 1)))
        sl.pack(side="left", fill="x", expand=True, padx=4)
        return var

    def _build_right(self, parent):
        # ヘッダ
        hdr = tk.Frame(parent, bg=BG)
        hdr.pack(fill="x", pady=(2,0))
        tk.Label(hdr, text="検出イベント", bg=BG, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        self._event_count = tk.Label(hdr, text="", bg=BG, fg=FG2,
                                     font=("Segoe UI", 9))
        self._event_count.pack(side="left", padx=8)
        _btn(hdr, "＋ 手動追加", self._manual_add,
             color=CARD, padx=8, pady=3).pack(side="right")

        # フィルタ
        filt = tk.Frame(parent, bg=BG)
        filt.pack(fill="x", pady=(4,0))
        self._filter_var = tk.StringVar(value="all")
        for val, label in [("all","すべて"),("kill","キル"),("death","デス"),("manual","手動")]:
            ttk.Radiobutton(filt, text=label, variable=self._filter_var,
                            value=val, command=self._refresh_list).pack(
                            side="left", padx=(0,10))

        # ツリービュー
        lf = tk.Frame(parent, bg=PANEL, padx=1, pady=1)
        lf.pack(fill="both", expand=True, pady=6)

        cols = ("time", "type", "conf")
        self._tree = ttk.Treeview(lf, columns=cols, show="headings",
                                  selectmode="browse")
        self._tree.heading("time",  text="タイムコード")
        self._tree.heading("type",  text="イベント")
        self._tree.heading("conf",  text="信頼度")
        self._tree.column("time",  width=115, anchor="center", minwidth=90)
        self._tree.column("type",  width=130, anchor="w",      minwidth=90)
        self._tree.column("conf",  width=75,  anchor="center", minwidth=60)

        sb = ttk.Scrollbar(lf, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._tree.bind("<Delete>", self._delete_selected)
        self._tree.bind("<Double-1>", self._edit_selected)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)

        for etype, meta in EVENT_META.items():
            self._tree.tag_configure(etype, foreground=meta["color"])

        # 詳細
        self._detail_var = tk.StringVar(value="イベントを選択すると詳細が表示されます")
        tk.Label(parent, textvariable=self._detail_var, bg=CARD, fg=FG2,
                 font=("Segoe UI", 9), anchor="w", padx=10, pady=6,
                 wraplength=400).pack(fill="x")

    # ─── ファイル選択 ─────────────────────────────────────────────────────────

    def _pick_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("動画", "*.mp4 *.mov *.mkv *.avi *.ts"), ("すべて", "*.*")])
        if not path:
            return
        self._video_path = path
        name = Path(path).name
        self._file_var.set(name)
        self._analyse_btn.config(state="normal", bg=ACCENT)
        self._status_var.set(f"準備完了: {name}")
        if _PIL:
            self._load_thumb(path)

    def _load_thumb(self, path):
        cap = cv2.VideoCapture(path)
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        img.thumbnail((280, 90))
        tki = ImageTk.PhotoImage(img)
        self._thumb_label.configure(image=tki)
        self._thumb_label._img = tki

    # ─── 解析 ─────────────────────────────────────────────────────────────────

    def _start_analysis(self):
        if not self._video_path or self._analysing:
            return
        self._analysing = True
        self._events.clear()
        self._tree.delete(*self._tree.get_children())
        self._analyse_btn.config(state="disabled", bg="#2a5a3a")
        self._progress["value"] = 0
        self._status_var.set("解析中...")

        def run():
            det = SplatoonDetector(
                kill_dark_ratio=self._kill_dark.get() / 100.0,
                kill_diff_threshold=self._kill_diff.get(),
                death_dark_threshold=self._death_thr.get(),
                cooldown_sec=self._cooldown.get(),
            )
            try:
                evs = analyse_video(
                    self._video_path, det,
                    sample_fps=self._smpl_fps.get(),
                    progress_cb=lambda f: (
                        setattr(self._progress, "__value__", f),
                        self._set_progress(f * 100)
                    ),
                )
                self._events.extend(evs)
                self.after(0, self._on_done)
            except Exception as ex:
                self.after(0, lambda: messagebox.showerror("エラー", str(ex)))
                self.after(0, self._reset)

        threading.Thread(target=run, daemon=True).start()

    def _set_progress(self, val):
        try:
            self._progress["value"] = val
            self.update_idletasks()
        except Exception:
            pass

    def _on_done(self):
        self._refresh_list()
        kills  = sum(1 for e in self._events if e.event_type == "kill")
        deaths = sum(1 for e in self._events if e.event_type == "death")
        self._status_var.set(
            f"完了 — {len(self._events)} 件検出  (キル: {kills} / デス: {deaths})")
        self._reset()

    def _reset(self):
        self._analysing = False
        self._analyse_btn.config(state="normal", bg=ACCENT)
        self._progress["value"] = 100

    # ─── イベントリスト ───────────────────────────────────────────────────────

    def _refresh_list(self):
        self._tree.delete(*self._tree.get_children())
        filt = self._filter_var.get()
        shown = [e for e in self._events
                 if filt == "all" or e.event_type == filt]
        for e in sorted(shown, key=lambda x: x.time_sec):
            h = int(e.time_sec // 3600)
            m = int((e.time_sec % 3600) // 60)
            s = e.time_sec % 60
            tc = f"{h:02d}:{m:02d}:{s:05.2f}"
            meta = EVENT_META.get(e.event_type, EVENT_META["manual"])
            self._tree.insert("", "end",
                values=(tc, f"{meta['icon']}  {e.label}", f"{e.confidence:.0%}"),
                tags=(e.event_type,), iid=str(id(e)))
        self._event_count.config(text=f"{len(shown)} 件")

    def _on_select(self, _=None):
        sel = self._tree.selection()
        if not sel:
            return
        ev = next((e for e in self._events if str(id(e)) == sel[0]), None)
        if ev:
            self._detail_var.set(
                f"種別: {ev.event_type}  |  時刻: {ev.time_sec:.3f} s  "
                f"|  信頼度: {ev.confidence:.1%}  |  ラベル: {ev.label}")

    def _delete_selected(self, _=None):
        sel = self._tree.selection()
        if sel:
            self._events = [e for e in self._events if str(id(e)) != sel[0]]
            self._refresh_list()

    def _edit_selected(self, _=None):
        sel = self._tree.selection()
        if not sel:
            return
        ev = next((e for e in self._events if str(id(e)) == sel[0]), None)
        if not ev:
            return
        dlg = tk.Toplevel(self)
        dlg.title("ラベル編集")
        dlg.geometry("320x130")
        dlg.resizable(False, False)
        dlg.configure(bg=PANEL)
        tk.Label(dlg, text="ラベル:", bg=PANEL, fg=FG2,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(16,4))
        var = tk.StringVar(value=ev.label)
        e = tk.Entry(dlg, textvariable=var, bg=CARD, fg=FG,
                     insertbackground=FG, relief="flat", font=("Segoe UI", 10))
        e.pack(fill="x", padx=16)
        e.focus()
        def save():
            ev.label = var.get()
            self._refresh_list()
            dlg.destroy()
        _btn(dlg, "保存", save, color=ACCENT, fg="#fff",
             padx=16, pady=5).pack(pady=12)
        dlg.bind("<Return>", lambda _: save())

    def _manual_add(self):
        dlg = tk.Toplevel(self)
        dlg.title("手動マーカー追加")
        dlg.geometry("340x220")
        dlg.resizable(False, False)
        dlg.configure(bg=PANEL)

        def row(label):
            tk.Label(dlg, text=label, bg=PANEL, fg=FG2,
                     font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(10,2))

        row("タイムコード  (HH:MM:SS.mm)")
        tc_var = tk.StringVar(value="00:00:00.00")
        tk.Entry(dlg, textvariable=tc_var, bg=CARD, fg=FG,
                 insertbackground=FG, relief="flat", width=18,
                 font=("Segoe UI", 10)).pack(anchor="w", padx=16)

        row("種別")
        type_var = tk.StringVar(value="manual")
        ttk.Combobox(dlg, textvariable=type_var,
                     values=list(EVENT_META.keys()),
                     state="readonly", width=14).pack(anchor="w", padx=16)

        row("ラベル（省略可）")
        lbl_var = tk.StringVar()
        tk.Entry(dlg, textvariable=lbl_var, bg=CARD, fg=FG,
                 insertbackground=FG, relief="flat", width=24,
                 font=("Segoe UI", 10)).pack(anchor="w", padx=16)

        def add():
            try:
                parts = tc_var.get().replace(",",".").split(":")
                sec = int(parts[0])*3600 + int(parts[1])*60 + float(parts[2])
            except Exception:
                messagebox.showerror("入力エラー", "HH:MM:SS.mm 形式で入力してください",
                                     parent=dlg)
                return
            et = type_var.get()
            ev = MarkerEvent(sec, et, 1.0, lbl_var.get() or et)
            self._events.append(ev)
            self._refresh_list()
            dlg.destroy()

        _btn(dlg, "追加", add, color=ACCENT3, fg="#fff",
             padx=16, pady=5).pack(pady=10)

    # ─── エクスポート ─────────────────────────────────────────────────────────

    def _export_csv(self):
        if not self._events:
            messagebox.showinfo("情報", "イベントがありません"); return
        p = filedialog.asksaveasfilename(defaultextension=".csv",
                                         filetypes=[("CSV","*.csv")])
        if p:
            save_csv(self._events, p)
            self._status_var.set(f"CSV保存: {Path(p).name}")

    def _export_json(self):
        if not self._events:
            messagebox.showinfo("情報", "イベントがありません"); return
        p = filedialog.asksaveasfilename(defaultextension=".json",
                                          filetypes=[("JSON","*.json")])
        if p:
            save_json(self._events, p)
            self._status_var.set(f"JSON保存: {Path(p).name}")

    def _export_video(self):
        if not self._events or not self._video_path:
            return
        p = filedialog.asksaveasfilename(defaultextension=".mp4",
                                          filetypes=[("MP4","*.mp4")])
        if not p:
            return
        self._status_var.set("動画変換中...")
        def run():
            try:
                embed_chapters(self._video_path, self._events, p)
                self.after(0, lambda: self._status_var.set(
                    f"動画保存完了: {Path(p).name}"))
            except FileNotFoundError:
                self.after(0, lambda: messagebox.showerror(
                    "ffmpegエラー",
                    "ffmpeg.exe が見つかりません。\nhttps://ffmpeg.org からインストールしてください"))
            except Exception as ex:
                self.after(0, lambda: messagebox.showerror("エラー", str(ex)))
        threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    app = ClipMarkerApp()
    app.mainloop()
