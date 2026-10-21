"""
DavinTelop - DaVinci Resolve テロップ作成ツール
CSV読み込み / PNG書き出し / SRT書き出し / FCPxml書き出し / Resolve API連携
カーニング・縁取り・背景・プリセット対応
"""
from __future__ import annotations
import os
import sys
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser, simpledialog
from pathlib import Path
from typing import Optional

try:
    import sv_ttk
    HAS_SV_TTK = True
except ImportError:
    HAS_SV_TTK = False

try:
    import darkdetect
    HAS_DARKDETECT = True
except ImportError:
    HAS_DARKDETECT = False

# コアモジュール
from core.models import DesignSettings, TelopEntry
from core.timecode import (parse_timecode, seconds_to_timecode, normalize_timecode,
                           shift_timecode, timecode_to_frames, seconds_to_frames,
                           _nominal_fps)
from core.preset import PresetManager
from dt_layout import TfxLayoutMixin
from core.exporter import (import_csv, export_png, export_srt, export_fcpxml,
                            export_resolve_script, export_csv, import_srt,
                            ResolveConnector)

try:
    from core.exporter import export_aaf, HAS_AAF2
except ImportError:
    HAS_AAF2 = False

try:
    from core.capture import ScreenCaptureThread, FileWatchThread, DirWatchThread
    HAS_CAPTURE = True
except ImportError:
    HAS_CAPTURE = False

try:
    from PIL import Image, ImageDraw, ImageTk
    PILLOW_OK = True
except ImportError:
    PILLOW_OK = False


def _make_checker_bg(w: int, h: int, sq: int = 16) -> "Image.Image":
    """透過確認用チェッカーボード背景を生成する (テーマ対応)。"""
    ts = sq * 2
    tile = Image.new('RGB', (ts, ts))
    td = ImageDraw.Draw(tile)
    c1 = _tc_rgb("checker_c1") if _current_theme_name() else (204, 204, 204)
    c2 = _tc_rgb("checker_c2") if _current_theme_name() else (153, 153, 153)
    td.rectangle([0,  0,  sq-1,  sq-1],  fill=c1)
    td.rectangle([sq, 0,  ts-1,  sq-1],  fill=c2)
    td.rectangle([0,  sq, sq-1,  ts-1],  fill=c2)
    td.rectangle([sq, sq, ts-1,  ts-1],  fill=c1)
    result = Image.new('RGB', (w, h))
    for y in range(0, h, ts):
        for x in range(0, w, ts):
            result.paste(tile, (x, y))
    return result


# ─────────────────────────────────────────────
#  テロップ編集ダイアログ
# ─────────────────────────────────────────────

class TelopEditDialog(tk.Toplevel):
    """テロップ1エントリの編集／新規作成ダイアログ（プレビュー付き・タイムコード連動）。

    新規作成時:
      - start_tc / end_tc で開始・終了TCを自動反映
      - submit_key（既定 Enter）で作成、newline_key（既定 Shift+Enter）で改行
      - on_submit(result) -> (next_start, next_end) を渡すと「連続入力」が使え、
        作成後もダイアログを閉じずに次のテロップを続けて入力できる
    """

    def __init__(self, parent, entry: Optional[TelopEntry] = None,
                 design: Optional["DesignSettings"] = None,
                 start_tc: str = "", end_tc: str = "",
                 submit_key: str = "Return", newline_key: str = "Shift-Return",
                 on_submit=None, continuous: bool = False,
                 get_playhead=None, chars_per_sec: float = 6.0,
                 min_duration: float = 1.0, title: str = ""):
        super().__init__(parent)
        self._is_new = entry is None
        self.title(title or ("新規テロップ" if self._is_new else "テロップ編集"))
        self.minsize(700, 400)
        self.resizable(True, True)
        self.transient(parent)
        self.result: Optional[dict] = None
        self.added_count = 0
        self._design = design
        self._preview_image_ref = None
        self._preview_pending = False
        self._updating_tc = False
        self._fps = design.frame_rate if design else 29.97
        self._on_submit = on_submit if self._is_new else None
        self._get_playhead = get_playhead
        self._cps = max(0.5, float(chars_per_sec or 6.0))
        self._min_dur = max(0.0, float(min_duration or 0.0))
        self._submit_key = submit_key or "Return"
        self._newline_key = newline_key if newline_key != self._submit_key else ""
        self.continuous_var = tk.BooleanVar(value=continuous)
        self._build(entry, start_tc, end_tc)
        self._center_on(parent, 920, 480)
        self.grab_set()
        self.wait_window()

    def _center_on(self, parent, w: int, h: int):
        try:
            self.update_idletasks()
            px = parent.winfo_rootx() + parent.winfo_width() // 2
            py = parent.winfo_rooty() + parent.winfo_height() // 2
            self.geometry(f"{w}x{h}+{max(0, px - w // 2)}+{max(0, py - h // 2)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    def _build(self, entry: Optional[TelopEntry], start_tc: str, end_tc: str):
        main = ttk.Frame(self, padding=8)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        # ── 左パネル（入力） ──────────────────────────
        left = ttk.Frame(main, padding=4)
        left.grid(row=0, column=0, sticky='nsew')
        left.columnconfigure(1, weight=1)
        left.rowconfigure(0, weight=1)

        pad = dict(padx=6, pady=4)

        ttk.Label(left, text="テキスト:").grid(row=0, column=0, sticky='ne', **pad)
        txt_frame = ttk.Frame(left)
        txt_frame.grid(row=0, column=1, columnspan=2, sticky='nsew', **pad)
        txt_frame.columnconfigure(0, weight=1)
        txt_frame.rowconfigure(0, weight=1)
        self._text = tk.Text(txt_frame, width=30, height=7, wrap='word', undo=True)
        self._text.grid(row=0, column=0, sticky='nsew')
        self._text.bind('<KeyRelease>', lambda e: self._on_text_change())
        self._count_var = tk.StringVar(value="0 文字 / 1 行")
        ttk.Label(txt_frame, textvariable=self._count_var, foreground='gray',
                  font=('', 8)).grid(row=1, column=0, sticky='e')

        ttk.Separator(left, orient='horizontal').grid(
            row=1, column=0, columnspan=3, sticky='ew', pady=6)

        # 開始TC
        ttk.Label(left, text="開始 TC:").grid(row=2, column=0, sticky='e', **pad)
        self._start_var = tk.StringVar()
        start_row = ttk.Frame(left)
        start_row.grid(row=2, column=1, columnspan=2, sticky='w', **pad)
        self._start_entry = ttk.Entry(start_row, textvariable=self._start_var, width=14)
        self._start_entry.pack(side='left')
        if self._get_playhead:
            ttk.Button(start_row, text="◉ 再生ヘッド", width=11,
                       command=self._use_playhead).pack(side='left', padx=(6, 0))
        ttk.Label(start_row, text="HH:MM:SS:FF", foreground='gray').pack(side='left', padx=6)
        self._start_var.trace_add('write', self._on_start_change)

        # 終了TC
        ttk.Label(left, text="終了 TC:").grid(row=3, column=0, sticky='e', **pad)
        self._end_var = tk.StringVar()
        self._end_entry = ttk.Entry(left, textvariable=self._end_var, width=14)
        self._end_entry.grid(row=3, column=1, sticky='w', **pad)
        self._end_var.trace_add('write', self._on_end_change)

        # 尺（秒）
        ttk.Label(left, text="尺 (秒):").grid(row=4, column=0, sticky='e', **pad)
        dur_frame = ttk.Frame(left)
        dur_frame.grid(row=4, column=1, columnspan=2, sticky='w', **pad)
        self._dur_var = tk.StringVar()
        ttk.Entry(dur_frame, textvariable=self._dur_var, width=8).pack(side='left')
        ttk.Label(dur_frame, text="秒", foreground='gray').pack(side='left', padx=(2, 6))
        for d in (2, 3, 5):
            ttk.Button(dur_frame, text=f"{d}s", width=3,
                       command=lambda d=d: self._dur_var.set(f"{d:.3f}")).pack(side='left', padx=1)
        ttk.Button(dur_frame, text="文字数で", width=7,
                   command=self._dur_from_text).pack(side='left', padx=(4, 0))
        self._dur_var.trace_add('write', self._on_dur_change)

        ttk.Label(left, text="TC欄: ↑↓ で ±1フレーム / Shift+↑↓ で ±1秒 / 数字のみ入力可 (例 1234)",
                  foreground='gray', font=('', 8)).grid(
                  row=5, column=0, columnspan=3, sticky='w', padx=6)

        ttk.Separator(left, orient='horizontal').grid(
            row=6, column=0, columnspan=3, sticky='ew', pady=6)

        # メモ
        ttk.Label(left, text="メモ:").grid(row=7, column=0, sticky='e', **pad)
        self._note = ttk.Entry(left, width=30)
        self._note.grid(row=7, column=1, columnspan=2, sticky='ew', **pad)

        # 有効 / 連続入力
        chk_row = ttk.Frame(left)
        chk_row.grid(row=8, column=1, columnspan=2, sticky='w', **pad)
        self._enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(chk_row, text="有効", variable=self._enabled_var).pack(side='left')
        if self._on_submit:
            ttk.Checkbutton(chk_row, text="連続入力（作成後も閉じない）",
                            variable=self.continuous_var).pack(side='left', padx=(12, 0))

        # ボタン
        btn_frame = ttk.Frame(left)
        btn_frame.grid(row=9, column=0, columnspan=3, pady=(8, 0))
        ok_label = "作成" if self._is_new else "OK"
        ok_label += f"  ({_binding_to_display(self._submit_key)})"
        ttk.Button(btn_frame, text=ok_label, width=16, command=self._ok).pack(side='left', padx=4)
        ttk.Button(btn_frame, text="閉じる (Esc)" if self._is_new else "キャンセル",
                   width=12, command=self.destroy).pack(side='left', padx=4)

        hint = f"{_binding_to_display(self._submit_key)}: {'作成' if self._is_new else '確定'}"
        if self._newline_key:
            hint += f"  /  {_binding_to_display(self._newline_key)}: 改行"
        hint += "  /  Esc: 閉じる"
        ttk.Label(left, text=hint, foreground='gray', font=('', 8)).grid(
            row=10, column=0, columnspan=3, pady=(4, 0))
        self._status_var = tk.StringVar(value="")
        ttk.Label(left, textvariable=self._status_var, foreground='#2E8B57').grid(
            row=11, column=0, columnspan=3, pady=(2, 0))

        # ── 右パネル（プレビュー） ────────────────────
        right = ttk.LabelFrame(main, text="プレビュー", padding=4)
        right.grid(row=0, column=1, sticky='nsew', padx=(10, 0))
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self._preview_canvas = tk.Canvas(right, bg='#888888', highlightthickness=0)
        self._preview_canvas.grid(row=0, column=0, sticky='nsew')
        self._preview_canvas.bind('<Configure>', lambda e: self._schedule_preview())

        # 既存エントリ / 新規の初期値を反映
        if entry:
            self._start_var.set(entry.start_time)
            self._end_var.set(entry.end_time)
            self._text.insert('1.0', entry.text)
            self._note.insert(0, entry.note)
            self._enabled_var.set(entry.enabled)
        else:
            self._start_var.set(start_tc)
            self._end_var.set(end_tc)
        self._update_dur_from_tc()
        self._update_count()

        # ── キーバインド ──
        try:
            self._text.bind(f"<{self._submit_key}>", self._on_submit_key)
            self.bind(f"<{self._submit_key}>", self._on_submit_key)
        except tk.TclError:
            self._text.bind("<Return>", self._on_submit_key)
            self.bind("<Return>", self._on_submit_key)
        if self._newline_key:
            try:
                self._text.bind(f"<{self._newline_key}>", self._on_newline_key)
            except tk.TclError:
                pass
        self.bind('<Escape>', lambda e: self.destroy())
        for ent, var in ((self._start_entry, self._start_var),
                         (self._end_entry, self._end_var)):
            ent.bind('<Up>',         lambda e, v=var: self._nudge(v, 1))
            ent.bind('<Down>',       lambda e, v=var: self._nudge(v, -1))
            ent.bind('<Shift-Up>',   lambda e, v=var: self._nudge(v, None, 1))
            ent.bind('<Shift-Down>', lambda e, v=var: self._nudge(v, None, -1))
            ent.bind('<FocusOut>',   lambda e, v=var: self._normalize(v))

        self._text.focus_set()
        self._text.mark_set('insert', 'end-1c')
        self._schedule_preview()

    # ── キー操作 ────────────────────────────────────

    def _on_submit_key(self, event=None):
        # Text 内と Toplevel の両方にバインドしているので二重実行を防ぐ
        if getattr(event, 'widget', None) is self and self.focus_get() is self._text:
            return "break"
        self._ok()
        return "break"

    def _on_newline_key(self, event=None):
        self._text.insert('insert', '\n')
        self._on_text_change()
        return "break"

    def _nudge(self, var: tk.StringVar, frames: Optional[int], secs: int = 0):
        from core.timecode import shift_timecode, _nominal_fps
        delta = frames if frames is not None else secs * _nominal_fps(self._fps)
        try:
            var.set(shift_timecode(var.get().strip() or "00:00:00:00", delta, self._fps))
        except ValueError:
            pass
        return "break"

    def _normalize(self, var: tk.StringVar):
        from core.timecode import normalize_timecode
        cur = var.get().strip()
        if not cur:
            return
        try:
            norm = normalize_timecode(cur, self._fps)
            if norm != cur:
                var.set(norm)
        except ValueError:
            pass

    def _use_playhead(self):
        tc = None
        try:
            tc = self._get_playhead()
        except Exception:
            tc = None
        if not tc:
            self._status_var.set("⚠ Resolve の再生ヘッドを取得できませんでした")
            return
        dur = self._dur_var.get()
        self._start_var.set(tc)
        self._dur_var.set(dur)   # 尺を保ったまま終了TCを再計算
        self._status_var.set(f"再生ヘッド {tc} を開始TCに設定しました")

    def _dur_from_text(self):
        """文字数から尺を自動計算（chars_per_sec 文字/秒、最小 min_duration 秒）。"""
        text = self._text.get('1.0', 'end-1c').replace('\n', '').strip()
        if not text:
            return
        dur = max(self._min_dur, len(text) / self._cps)
        self._dur_var.set(f"{dur:.3f}")

    def _on_text_change(self):
        self._update_count()
        self._schedule_preview()

    def _update_count(self):
        text = self._text.get('1.0', 'end-1c')
        lines = text.split('\n')
        chars = len(text.replace('\n', ''))
        longest = max((len(l) for l in lines), default=0)
        self._count_var.set(f"{chars} 文字 / {len(lines)} 行 (最長 {longest} 文字)")

    # ── タイムコード連動 ─────────────────────────────

    def _on_start_change(self, *_):
        if not self._updating_tc:
            self._updating_tc = True
            self._update_dur_from_tc()
            self._updating_tc = False

    def _on_end_change(self, *_):
        if not self._updating_tc:
            self._updating_tc = True
            self._update_dur_from_tc()
            self._updating_tc = False

    def _on_dur_change(self, *_):
        if not self._updating_tc:
            self._updating_tc = True
            self._update_end_from_dur()
            self._updating_tc = False

    def _update_dur_from_tc(self):
        """開始TC・終了TCから尺(秒)を計算して表示。"""
        try:
            start_s = parse_timecode(self._start_var.get().strip(), self._fps)
            end_s   = parse_timecode(self._end_var.get().strip(), self._fps)
            dur = max(0.0, end_s - start_s)
            self._dur_var.set(f"{dur:.3f}")
        except (ValueError, Exception):
            pass

    def _update_end_from_dur(self):
        """開始TC + 尺(秒) から終了TCを計算して表示。"""
        try:
            start_s = parse_timecode(self._start_var.get().strip(), self._fps)
            dur = float(self._dur_var.get().strip())
            end_s = start_s + max(0.0, dur)
            self._end_var.set(seconds_to_timecode(end_s, self._fps))
        except (ValueError, Exception):
            pass

    # ── リアルタイムプレビュー ───────────────────────

    def _schedule_preview(self):
        if not self._preview_pending:
            self._preview_pending = True
            self.after(150, self._update_preview)

    def _update_preview(self):
        self._preview_pending = False
        if not PILLOW_OK or self._design is None:
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return

        from core.renderer import render_telop_image

        text = self._text.get('1.0', 'end-1c').strip() or "プレビューテキスト"
        cw = self._preview_canvas.winfo_width()
        ch = self._preview_canvas.winfo_height()
        if cw < 10 or ch < 10:
            return

        try:
            img = render_telop_image(text, self._design)
            W, H = self._design.canvas_width, self._design.canvas_height
            bg = _make_checker_bg(W, H)
            bg.paste(img, (0, 0), img)
            scale = min(cw / W, ch / H)
            pw, ph = int(W * scale), int(H * scale)
            thumb = bg.resize((pw, ph), Image.LANCZOS)
            photo = ImageTk.PhotoImage(thumb)
            self._preview_image_ref = photo
            self._preview_canvas.delete('all')
            ox, oy = (cw - pw) // 2, (ch - ph) // 2
            self._preview_canvas.create_image(ox, oy, anchor='nw', image=photo)
            info = f"{W}×{H}  {self._fps}fps"
            self._preview_canvas.create_text(
                ox + 5, oy + 5, text=info,
                anchor='nw', fill='white', font=('', 9))
            self._preview_canvas.create_text(
                ox + 4, oy + 4, text=info,
                anchor='nw', fill='#333333', font=('', 9))
        except Exception as ex:
            self._preview_canvas.delete('all')
            self._preview_canvas.create_text(
                cw // 2, ch // 2, text=f"エラー:\n{ex}",
                fill='red', justify='center')

    def _ok(self):
        from core.timecode import normalize_timecode
        text  = self._text.get('1.0', 'end-1c').strip()
        if not text:
            messagebox.showwarning("入力エラー", "テキストを入力してください。", parent=self)
            self._text.focus_set()
            return
        try:
            start = normalize_timecode(self._start_var.get(), self._fps)
            end   = normalize_timecode(self._end_var.get(), self._fps)
        except ValueError as ex:
            messagebox.showwarning("入力エラー", f"タイムコードが不正です:\n{ex}", parent=self)
            return
        if parse_timecode(end, self._fps) <= parse_timecode(start, self._fps):
            messagebox.showwarning("入力エラー",
                                   "終了TCは開始TCより後にしてください。", parent=self)
            return
        result = {
            "start_time": start,
            "end_time":   end,
            "text":       text,
            "note":       self._note.get().strip(),
            "enabled":    self._enabled_var.get(),
        }
        if self._on_submit:
            nxt = self._on_submit(result)
            self.added_count += 1
            self.result = result
            if self.continuous_var.get() and nxt:
                # 次のテロップ入力へ（尺は維持）
                dur = self._dur_var.get()
                self._text.delete('1.0', 'end')
                self._start_var.set(nxt[0])
                self._end_var.set(nxt[1])
                if not nxt[1]:
                    self._dur_var.set(dur)
                self._note.delete(0, 'end')
                self._status_var.set(
                    f"✓ 追加しました ({self.added_count} 件): {text.splitlines()[0][:24]}")
                self._update_count()
                self._schedule_preview()
                self._text.focus_set()
                return
        self.result = result
        self.destroy()


# ─────────────────────────────────────────────
#  Resolve送信ダイアログ
# ─────────────────────────────────────────────

class ResolveDialog(tk.Toplevel):
    def __init__(self, parent, timeline_info: dict):
        super().__init__(parent)
        self.title("DaVinci Resolveへ送る")
        self.resizable(False, False)
        self.result: Optional[dict] = None
        self._build(timeline_info)
        self.grab_set()
        self.wait_window()

    def _build(self, info: dict):
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="接続中のタイムライン情報", font=('', 12, 'bold')).pack(anchor='w', pady=(0, 8))
        info_text = (
            f"名前: {info.get('name', '不明')}\n"
            f"FPS: {info.get('fps', '不明')}\n"
            f"解像度: {info.get('width', '?')} × {info.get('height', '?')}\n"
            f"ビデオトラック数: {info.get('track_count_v', '?')}"
        )
        ttk.Label(frame, text=info_text, justify='left').pack(anchor='w', pady=(0, 12))

        ttk.Separator(frame).pack(fill='x', pady=8)

        ttk.Label(frame, text="PNGを配置するトラック (V):").pack(anchor='w')
        self._track_var = tk.IntVar(value=2)
        ttk.Spinbox(frame, from_=1, to=20, textvariable=self._track_var, width=8).pack(anchor='w', pady=4)

        ttk.Label(frame, text="PNG書き出し先:").pack(anchor='w', pady=(8, 0))
        self._dir_var = tk.StringVar(value=str(Path.home() / "Desktop" / "davintelop_png"))
        dir_frame = ttk.Frame(frame)
        dir_frame.pack(fill='x', pady=4)
        ttk.Entry(dir_frame, textvariable=self._dir_var, width=38).pack(side='left')
        ttk.Button(dir_frame, text="...", width=3, command=self._pick_dir).pack(side='left', padx=4)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(pady=(16, 0))
        ttk.Button(btn_frame, text="実行", width=12, command=self._ok).pack(side='left', padx=4)
        ttk.Button(btn_frame, text="キャンセル", width=12, command=self.destroy).pack(side='left', padx=4)

    def _pick_dir(self):
        d = filedialog.askdirectory(parent=self)
        if d:
            self._dir_var.set(d)

    def _ok(self):
        self.result = {
            "track_index": self._track_var.get(),
            "png_dir": self._dir_var.get(),
        }
        self.destroy()


# ─────────────────────────────────────────────
#  キーボードショートカット ユーティリティ
# ─────────────────────────────────────────────

_IGNORE_KEYSYMS = frozenset({
    'Control_L', 'Control_R', 'Shift_L', 'Shift_R',
    'Alt_L', 'Alt_R', 'Meta_L', 'Meta_R',
    'Super_L', 'Super_R', 'Caps_Lock', 'Num_Lock', 'Scroll_Lock',
    'Win_L', 'Win_R', 'App',
})

def _capture_shortcut(event) -> str:
    """KeyPress イベントから Tkinter バインディング文字列を生成。"""
    key = event.keysym
    if not key or key in _IGNORE_KEYSYMS:
        return ""
    mods = []
    if event.state & 0x4:      mods.append("Control")
    if event.state & 0x1:      mods.append("Shift")
    if event.state & 0x20000:  mods.append("Alt")
    return "-".join(mods + [key])


def _binding_to_display(binding: str) -> str:
    """バインディング文字列を人間が読みやすい形式に変換 (例: Control-Return → Ctrl+Enter)。"""
    if not binding:
        return ""
    _MAP = {
        "Control": "Ctrl", "Return": "Enter", "Delete": "Del",
        "BackSpace": "BS",  "Escape": "Esc",  "Prior": "PageUp",
        "Next": "PageDown", "Insert": "Ins",
    }
    return "+".join(_MAP.get(p, p.upper() if len(p) == 1 else p)
                    for p in binding.split("-"))


class _ShortcutCapture(ttk.Frame):
    """ショートカットキーを1つキャプチャするウィジェット。"""

    def __init__(self, parent, var: tk.StringVar, **kw):
        super().__init__(parent, **kw)
        self._var = var
        self._recording = False

        self._btn = ttk.Button(self, text=self._display(), width=18,
                               command=self._toggle_record)
        self._btn.pack(side='left')
        ttk.Button(self, text="✕", width=3, command=self._clear).pack(side='left', padx=2)

        self._btn.bind('<KeyPress>',  self._on_key)
        self._btn.bind('<FocusOut>',  lambda e: self._stop_record(cancelled=True))
        self._var.trace_add('write',  lambda *_: self._refresh_display())

    def _display(self) -> str:
        d = _binding_to_display(self._var.get())
        return d if d else "(未設定)"

    def _refresh_display(self):
        if not self._recording:
            self._btn.configure(text=self._display())

    def _toggle_record(self):
        if self._recording:
            self._stop_record(cancelled=True)
        else:
            self._recording = True
            self._btn.configure(text="← キーを押す  [Esc: キャンセル]")
            self._btn.focus_set()

    def _on_key(self, event):
        if not self._recording:
            return
        if event.keysym == 'Escape':
            self._stop_record(cancelled=True)
            return "break"
        binding = _capture_shortcut(event)
        if binding:
            self._var.set(binding)
            self._stop_record(cancelled=False)
        return "break"

    def _stop_record(self, cancelled=False):
        self._recording = False
        self._btn.configure(text=self._display())

    def _clear(self):
        self._var.set("")


# ─────────────────────────────────────────────
#  ショートカット設定ダイアログ
# ─────────────────────────────────────────────

class ShortcutConfigDialog(tk.Toplevel):
    """キーボードショートカット設定ダイアログ。"""

    def __init__(self, parent, shortcuts: dict, defaults: dict, labels: dict):
        super().__init__(parent)
        self.title("キーボードショートカット設定")
        self.resizable(False, False)
        self.result: Optional[dict] = None
        self._shortcuts = dict(shortcuts)
        self._defaults  = defaults
        self._labels    = labels
        self._vars: dict = {}
        self._build()
        self.grab_set()
        self.wait_window()

    def _build(self):
        main = ttk.Frame(self, padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main,
                  text="ショートカットボタンをクリックしてキーを押してください\n"
                       "（Esc でキャンセル、✕ でショートカットを削除）",
                  foreground='gray', justify='left').pack(anchor='w', pady=(0, 10))

        grid = ttk.Frame(main)
        grid.pack(fill='x')

        # 項目が多いので2列に分けて表示
        items = list(self._labels.items())
        half = (len(items) + 1) // 2
        for c in (0, 2):
            ttk.Label(grid, text="アクション",      font=('', 9, 'bold')).grid(row=0, column=c, sticky='w', padx=(4, 16), pady=2)
            ttk.Label(grid, text="ショートカット",  font=('', 9, 'bold')).grid(row=0, column=c + 1, sticky='w', padx=(4, 20), pady=2)
        ttk.Separator(grid, orient='horizontal').grid(row=1, column=0, columnspan=4, sticky='ew', pady=4)

        for i, (action, label) in enumerate(items):
            r, c = 2 + i % half, (i // half) * 2
            ttk.Label(grid, text=label).grid(row=r, column=c, sticky='w', padx=(4, 16), pady=3)
            var = tk.StringVar(value=self._shortcuts.get(action, ""))
            self._vars[action] = var
            _ShortcutCapture(grid, var).grid(row=r, column=c + 1, sticky='w', padx=(4, 20))

        ttk.Separator(main).pack(fill='x', pady=12)

        btn_frame = ttk.Frame(main)
        btn_frame.pack()
        ttk.Button(btn_frame, text="OK",             width=10, command=self._ok).pack(side='left', padx=4)
        ttk.Button(btn_frame, text="キャンセル",     width=12, command=self.destroy).pack(side='left', padx=4)
        ttk.Button(btn_frame, text="デフォルトに戻す", command=self._reset_defaults).pack(side='left', padx=8)

    def _ok(self):
        result = {action: var.get() for action, var in self._vars.items()}
        # ダイアログ内専用キーはメイン画面のキーと重複してよい
        local = {"dlg_submit", "dlg_newline"}
        seen: dict = {}
        dups = []
        for action, key in result.items():
            if not key or action in local:
                continue
            if key in seen:
                dups.append(f"{_binding_to_display(key)}: {self._labels[seen[key]]} / {self._labels[action]}")
            seen[key] = action
        if result.get("dlg_submit") and result.get("dlg_submit") == result.get("dlg_newline"):
            dups.append("テロップ入力の作成キーと改行キーが同じです")
        if not result.get("dlg_submit"):
            result["dlg_submit"] = self._defaults.get("dlg_submit", "Return")
        if dups and not messagebox.askyesno(
                "ショートカットの重複",
                "次のショートカットが重複しています:\n\n" + "\n".join(dups) +
                "\n\nこのまま保存しますか？", parent=self):
            return
        self.result = result
        self.destroy()

    def _reset_defaults(self):
        for action, var in self._vars.items():
            var.set(self._defaults.get(action, ""))


# ─────────────────────────────────────────────
#  テロップ作成設定ダイアログ
# ─────────────────────────────────────────────

class NewTelopSettingsDialog(tk.Toplevel):
    """新規テロップ作成時の TC 自動反映・尺の既定値を設定する。"""

    _MODES = [
        ("auto",     "選択テロップの直後（未選択なら最後のテロップの直後）"),
        ("last",     "常に最後のテロップの直後"),
        ("playhead", "Resolve の再生ヘッド位置（取得失敗時は最後の直後）"),
        ("qa",       "「直接入力」欄の開始TC"),
    ]

    def __init__(self, parent, cfg: dict):
        super().__init__(parent)
        self.title("テロップ作成設定")
        self.resizable(False, False)
        self.transient(parent)
        self.result: Optional[dict] = None
        f = ttk.Frame(self, padding=16)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="新規テロップの開始TC", font=('', 10, 'bold')).grid(
            row=0, column=0, columnspan=3, sticky='w')
        self._mode = tk.StringVar(value=cfg.get("start_mode", "auto"))
        for i, (val, label) in enumerate(self._MODES, start=1):
            ttk.Radiobutton(f, text=label, value=val, variable=self._mode).grid(
                row=i, column=0, columnspan=3, sticky='w', padx=(12, 0), pady=1)

        r = len(self._MODES) + 1
        ttk.Separator(f).grid(row=r, column=0, columnspan=3, sticky='ew', pady=8)
        self._dur = tk.DoubleVar(value=cfg.get("default_dur", 3.0))
        self._gap = tk.IntVar(value=cfg.get("gap_frames", 0))
        self._cps = tk.DoubleVar(value=cfg.get("cps", 6.0))
        self._min = tk.DoubleVar(value=cfg.get("min_dur", 1.0))
        self._cont = tk.BooleanVar(value=cfg.get("continuous", False))
        rows = [
            ("既定の尺:", self._dur, 0.1, 600, 0.5, "秒"),
            ("前テロップとの間隔:", self._gap, 0, 600, 1, "フレーム"),
            ("文字数→尺 の速さ:", self._cps, 1, 30, 0.5, "文字/秒"),
            ("文字数→尺 の最小:", self._min, 0, 60, 0.5, "秒"),
        ]
        for i, (label, var, lo, hi, inc, unit) in enumerate(rows, start=r + 1):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky='e', pady=3)
            ttk.Spinbox(f, from_=lo, to=hi, increment=inc, textvariable=var,
                        width=8).grid(row=i, column=1, sticky='w', padx=6)
            ttk.Label(f, text=unit, foreground='gray').grid(row=i, column=2, sticky='w')
        r = r + 1 + len(rows)
        ttk.Checkbutton(f, text="連続入力モードを既定でオン（作成後もダイアログを閉じない）",
                        variable=self._cont).grid(row=r, column=0, columnspan=3,
                                                  sticky='w', pady=(8, 0))
        ttk.Label(f, text="作成/改行キーは［ファイル → キーボードショートカット設定］で変更できます。",
                  foreground='gray', font=('', 8)).grid(row=r + 1, column=0, columnspan=3,
                                                         sticky='w', pady=(6, 0))
        btns = ttk.Frame(f)
        btns.grid(row=r + 2, column=0, columnspan=3, pady=(12, 0))
        ttk.Button(btns, text="OK", width=10, command=self._ok).pack(side='left', padx=4)
        ttk.Button(btns, text="キャンセル", width=10, command=self.destroy).pack(side='left', padx=4)
        self.bind('<Escape>', lambda e: self.destroy())
        self.grab_set()
        self.wait_window()

    def _ok(self):
        try:
            self.result = {
                "start_mode": self._mode.get(),
                "default_dur": max(0.1, float(self._dur.get())),
                "gap_frames": max(0, int(self._gap.get())),
                "cps": max(0.5, float(self._cps.get())),
                "min_dur": max(0.0, float(self._min.get())),
                "continuous": bool(self._cont.get()),
            }
        except (tk.TclError, ValueError):
            messagebox.showwarning("入力エラー", "数値を正しく入力してください。", parent=self)
            return
        self.destroy()


# ─────────────────────────────────────────────
#  検索と置換ダイアログ
# ─────────────────────────────────────────────

class FindReplaceDialog(tk.Toplevel):
    """テロップテキストの検索・一括置換（モードレス）。"""

    def __init__(self, app):
        super().__init__(app)
        self._app = app
        self.title("検索と置換")
        self.resizable(False, False)
        self.transient(app)
        f = ttk.Frame(self, padding=12)
        f.pack(fill=tk.BOTH, expand=True)
        self._find = tk.StringVar()
        self._repl = tk.StringVar()
        ttk.Label(f, text="検索:").grid(row=0, column=0, sticky='e', pady=3)
        self._find_entry = ttk.Entry(f, textvariable=self._find, width=36)
        self._find_entry.grid(row=0, column=1, columnspan=3, sticky='ew', padx=6)
        ttk.Label(f, text="置換:").grid(row=1, column=0, sticky='e', pady=3)
        ttk.Entry(f, textvariable=self._repl, width=36).grid(
            row=1, column=1, columnspan=3, sticky='ew', padx=6)
        self._regex = tk.BooleanVar(value=False)
        self._case = tk.BooleanVar(value=False)
        self._sel_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="正規表現", variable=self._regex).grid(row=2, column=1, sticky='w')
        ttk.Checkbutton(f, text="大文字小文字を区別", variable=self._case).grid(row=2, column=2, sticky='w')
        ttk.Checkbutton(f, text="選択行のみ", variable=self._sel_only).grid(row=2, column=3, sticky='w')
        btns = ttk.Frame(f)
        btns.grid(row=3, column=0, columnspan=4, pady=(10, 0))
        ttk.Button(btns, text="該当を選択", command=self._select).pack(side='left', padx=3)
        ttk.Button(btns, text="すべて置換", command=self._replace).pack(side='left', padx=3)
        ttk.Button(btns, text="閉じる", command=self.destroy).pack(side='left', padx=3)
        self._msg = tk.StringVar(value="改行は \\n で指定できます")
        ttk.Label(f, textvariable=self._msg, foreground='gray').grid(
            row=4, column=0, columnspan=4, pady=(8, 0))
        self.bind('<Escape>', lambda e: self.destroy())
        self.bind('<Return>', lambda e: self._select())
        self._find_entry.focus_set()

    def _select(self):
        hits = self._app.find_matches(self._find.get(), self._regex.get(), self._case.get())
        self._app._filter_var.set("")
        self._app._tree.selection_set([])
        self._app._select_entries(hits)
        self._msg.set(f"{len(hits)} 件見つかりました")

    def _replace(self):
        n = self._app.find_replace_apply(self._find.get(), self._repl.get(),
                                         self._regex.get(), self._sel_only.get(),
                                         self._case.get())
        if n >= 0:
            self._msg.set(f"{n} 箇所を置換しました（Ctrl+Z で元に戻せます）")


# ─────────────────────────────────────────────
#  カラーボタン (クリックでカラーピッカー)
# ─────────────────────────────────────────────

class ColorButton(tk.Button):
    """クリックするとカラーピッカーを開くボタン。"""

    def __init__(self, parent, color: str = "#FFFFFF", command=None, **kwargs):
        super().__init__(parent, width=4, relief='solid', **kwargs)
        self._color = color
        self._ext_command = command
        self._apply_color(color)
        self.configure(command=self._pick)

    def _apply_color(self, color: str):
        self._color = color
        try:
            self.configure(bg=color, activebackground=color)
        except Exception:
            pass

    def _pick(self):
        result = colorchooser.askcolor(color=self._color, parent=self)
        if result and result[1]:
            self._apply_color(result[1])
            if self._ext_command:
                self._ext_command(result[1])

    def get(self) -> str:
        return self._color

    def set(self, color: str):
        self._apply_color(color)


# ─────────────────────────────────────────────
#  メインアプリケーション
# ─────────────────────────────────────────────
#  テーマ設定ユーティリティ
# ─────────────────────────────────────────────

# テーマカラー定義
_THEME: dict[str, dict] = {
    "dark": {
        "window_bg":    "#1c1c1c",
        "canvas_bg":    "#2a2a2a",
        "preview_bg":   "#1a1a1a",
        "text_bg":      "#2d2d2d",
        "text_fg":      "#f0f0f0",
        "listbox_bg":   "#2d2d2d",
        "listbox_fg":   "#f0f0f0",
        "listbox_sel":  "#0078D7",
        "checker_c1":   (60,  60,  60),
        "checker_c2":   (40,  40,  40),
        "panel_header": "#252535",
        "header_fg":    "#a0a8c0",
        "timeline_bg":       "#1e1f22",
        "timeline_fg":       "#9aa0aa",
        "timeline_ruler":    "#2a2c30",
        "timeline_grid":     "#33363b",
        "timeline_block":    "#2F6FB3",
        "timeline_styled":   "#7A4FB5",
        "timeline_disabled": "#555555",
        "timeline_sel":      "#FFD54A",
        "timeline_border":   "#16181b",
    },
    "light": {
        "window_bg":    "#f0f0f0",
        "canvas_bg":    "#888888",
        "preview_bg":   "#555555",
        "text_bg":      "#ffffff",
        "text_fg":      "#000000",
        "listbox_bg":   "#ffffff",
        "listbox_fg":   "#000000",
        "listbox_sel":  "#0078D7",
        "checker_c1":   (204, 204, 204),
        "checker_c2":   (153, 153, 153),
        "panel_header": "#e0e4f0",
        "header_fg":    "#3c4060",
        "timeline_bg":       "#f4f5f7",
        "timeline_fg":       "#555a66",
        "timeline_ruler":    "#e2e5ea",
        "timeline_grid":     "#d5d9e0",
        "timeline_block":    "#3D7FD0",
        "timeline_styled":   "#8E5CCB",
        "timeline_disabled": "#aaaaaa",
        "timeline_sel":      "#E8590C",
        "timeline_border":   "#2a3a50",
    },
}

def _current_theme_name() -> str:
    if HAS_SV_TTK:
        try:
            return sv_ttk.get_theme()   # "dark" or "light"
        except Exception:
            pass
    return "light"

def _tc(key: str) -> str:
    """現在のテーマから色を返す。"""
    return _THEME[_current_theme_name()].get(key, "")

def _tc_rgb(key: str) -> tuple:
    return _THEME[_current_theme_name()].get(key, (128, 128, 128))


# ─────────────────────────────────────────────
#  ツールチップ
# ─────────────────────────────────────────────

class _Tooltip:
    """ウィジェットにマウスを乗せると説明を表示する。"""

    def __init__(self, widget, text: str, delay: int = 500):
        self._w = widget
        self._text = text
        self._delay = delay
        self._job = None
        self._tip = None
        widget.bind('<Enter>', self._schedule, add='+')
        widget.bind('<Leave>', self._hide, add='+')
        widget.bind('<ButtonPress>', self._hide, add='+')

    def _schedule(self, _e=None):
        self._cancel()
        self._job = self._w.after(self._delay, self._show)

    def _cancel(self):
        if self._job:
            try:
                self._w.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _show(self):
        if self._tip or not self._text:
            return
        try:
            x = self._w.winfo_rootx() + 8
            y = self._w.winfo_rooty() + self._w.winfo_height() + 4
            self._tip = tw = tk.Toplevel(self._w)
            tw.wm_overrideredirect(True)
            tw.attributes('-topmost', True)
            tk.Label(tw, text=self._text, justify='left', bg='#ffffe1', fg='#000000',
                     relief='solid', borderwidth=1, font=('', 9), padx=6, pady=3).pack()
            tw.wm_geometry(f"+{x}+{y}")
        except tk.TclError:
            self._tip = None

    def _hide(self, _e=None):
        self._cancel()
        if self._tip:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class _PrefixSuffixDialog(tk.Toplevel):
    """先頭・末尾に追加する文字列を入力。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("先頭・末尾に文字を追加")
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        f = ttk.Frame(self, padding=14)
        f.pack(fill='both', expand=True)
        self._pre = tk.StringVar()
        self._suf = tk.StringVar()
        ttk.Label(f, text="先頭に追加:").grid(row=0, column=0, sticky='e', pady=3)
        e1 = ttk.Entry(f, textvariable=self._pre, width=24)
        e1.grid(row=0, column=1, padx=6)
        ttk.Label(f, text="末尾に追加:").grid(row=1, column=0, sticky='e', pady=3)
        ttk.Entry(f, textvariable=self._suf, width=24).grid(row=1, column=1, padx=6)
        ttk.Label(f, text="例: 先頭「♪」末尾「♪」、末尾「（笑）」など",
                  foreground='gray', font=('', 8)).grid(row=2, column=0, columnspan=2, pady=(4, 0))
        b = ttk.Frame(f)
        b.grid(row=3, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(b, text="OK", width=10, command=self._ok).pack(side='left', padx=4)
        ttk.Button(b, text="キャンセル", width=10, command=self.destroy).pack(side='left', padx=4)
        self.bind('<Return>', lambda e: self._ok())
        self.bind('<Escape>', lambda e: self.destroy())
        e1.focus_set()
        self.grab_set()
        self.wait_window()

    def _ok(self):
        if self._pre.get() or self._suf.get():
            self.result = (self._pre.get(), self._suf.get())
        self.destroy()


# ─────────────────────────────────────────────
#  タイムラインビュー（テロップの時間配置を視覚的に編集）
# ─────────────────────────────────────────────

class TimelineView(ttk.Frame):
    """テロップをブロックで表示するミニタイムライン。

    - クリックで選択（Ctrl/Shift で追加選択）、ダブルクリックで編集
    - ブロックをドラッグで移動（複数選択はまとめて移動）、左右端ドラッグで開始/終了を調整
    - ホイールで横スクロール、Ctrl+ホイールでズーム、Alt でスナップ無効
    """

    RULER_H = 18
    LANE_H = 26
    EDGE = 6

    def __init__(self, parent, app):
        super().__init__(parent)
        self._app = app
        self._pps = 40.0          # pixels per second
        self._t0 = 0.0            # 表示左端の秒
        self._drag = None
        self._items: dict = {}    # canvas item id -> entry
        self._layout: list = []   # (entry, s, e, lane)
        self._fit_pending = True

        bar = ttk.Frame(self)
        bar.pack(fill='x')
        ttk.Label(bar, text="タイムライン", font=('', 9, 'bold')).pack(side='left', padx=(4, 8))
        ttk.Button(bar, text="全体", width=5, command=self.fit).pack(side='left', padx=1)
        ttk.Button(bar, text="選択へ", width=6, command=self.show_selection).pack(side='left', padx=1)
        ttk.Button(bar, text="＋", width=3, command=lambda: self.zoom(1.5)).pack(side='left', padx=1)
        ttk.Button(bar, text="－", width=3, command=lambda: self.zoom(1 / 1.5)).pack(side='left', padx=1)
        self._snap_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="隣接スナップ", variable=self._snap_var).pack(side='left', padx=8)
        self._info_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._info_var, foreground='gray').pack(side='left', padx=8)
        ttk.Label(bar, text="ドラッグ: 移動 / 端: 尺調整 / Ctrl+ホイール: ズーム",
                  foreground='gray', font=('', 8)).pack(side='right', padx=4)

        self._canvas = tk.Canvas(self, height=self.RULER_H + self.LANE_H * 2 + 8,
                                 highlightthickness=0, bg=_tc("timeline_bg"))
        self._canvas.pack(fill='both', expand=True)
        self._hbar = ttk.Scrollbar(self, orient='horizontal', command=self._on_scrollbar)
        self._hbar.pack(fill='x')

        c = self._canvas
        c.bind('<Configure>', lambda e: self.redraw())
        c.bind('<ButtonPress-1>', self._on_press)
        c.bind('<B1-Motion>', self._on_motion)
        c.bind('<ButtonRelease-1>', self._on_release)
        c.bind('<Double-Button-1>', self._on_double)
        c.bind('<Motion>', self._on_hover)
        c.bind('<MouseWheel>', self._on_wheel)
        c.bind('<Shift-MouseWheel>', self._on_wheel)
        c.bind('<Control-MouseWheel>', self._on_ctrl_wheel)
        c.bind('<Button-4>', lambda e: self._scroll(-1))
        c.bind('<Button-5>', lambda e: self._scroll(1))
        c.bind('<Button-3>', self._on_right)

    # ── 座標変換 ─────────────────────────────
    def _fps(self) -> float:
        return self._app._current_fps()

    def _x(self, t: float) -> float:
        return (t - self._t0) * self._pps

    def _t(self, x: float) -> float:
        return self._t0 + x / self._pps

    def _span(self, e):
        fps = self._fps()
        try:
            s = parse_timecode(e.start_time, fps)
            t = parse_timecode(e.end_time, fps)
            return s, max(t, s)
        except ValueError:
            return None

    def _bounds(self):
        spans = [sp for sp in (self._span(e) for e in self._app.telop_list) if sp]
        if not spans:
            return 0.0, 10.0
        return min(s for s, _ in spans), max(t for _, t in spans)

    # ── 表示操作 ─────────────────────────────
    def fit(self):
        w = max(100, self._canvas.winfo_width())
        lo, hi = self._bounds()
        dur = max(5.0, hi - lo)
        self._pps = max(2.0, (w - 20) / dur)
        self._t0 = lo - 10 / self._pps
        self.redraw()

    def zoom(self, factor: float, x: Optional[float] = None):
        if x is None:
            x = self._canvas.winfo_width() / 2
        t = self._t(x)
        self._pps = min(2000.0, max(1.0, self._pps * factor))
        self._t0 = t - x / self._pps
        self.redraw()

    def show_selection(self):
        sel = self._app._get_selected_entries()
        spans = [sp for sp in (self._span(e) for e in sel) if sp]
        if not spans:
            return
        s = min(a for a, _ in spans)
        w = self._canvas.winfo_width()
        if not (self._t0 <= s <= self._t(w - 40)):
            self._t0 = s - 2.0
        self.redraw()

    def _scroll(self, units: int):
        self._t0 += units * 60 / self._pps
        self.redraw()

    def _on_wheel(self, e):
        self._scroll(-1 if e.delta > 0 else 1)
        return "break"

    def _on_ctrl_wheel(self, e):
        self.zoom(1.25 if e.delta > 0 else 0.8, e.x)
        return "break"

    def _on_scrollbar(self, *args):
        lo, hi = self._bounds()
        lo -= 5
        hi += 5
        total = max(1e-6, hi - lo)
        w = self._canvas.winfo_width() / self._pps
        if args[0] == 'moveto':
            self._t0 = lo + float(args[1]) * total
        elif args[0] == 'scroll':
            step = w * 0.9 if args[2] == 'pages' else 60 / self._pps
            self._t0 += int(args[1]) * step
        self.redraw()

    # ── 描画 ────────────────────────────────
    def _assign_lanes(self):
        items = []
        for e in self._app.telop_list:
            sp = self._span(e)
            if sp:
                items.append((sp[0], sp[1], e))
        items.sort(key=lambda x: (x[0], x[1]))
        lane_end: list = []
        layout = []
        for s, t, e in items:
            for i, le in enumerate(lane_end):
                if s >= le - 1e-6:
                    lane_end[i] = t
                    lane = i
                    break
            else:
                lane_end.append(t)
                lane = len(lane_end) - 1
            layout.append((e, s, t, lane))
        return layout, max(1, len(lane_end))

    def redraw(self):
        c = self._canvas
        try:
            if not c.winfo_exists():
                return
        except tk.TclError:
            return
        w, h = c.winfo_width(), c.winfo_height()
        if w < 20:
            return
        if self._fit_pending and self._app.telop_list:
            self._fit_pending = False
            self.fit()
            return
        c.delete('all')
        self._items.clear()
        c.configure(bg=_tc("timeline_bg"))
        fps = self._fps()
        layout, lanes = self._assign_lanes()
        self._layout = layout
        need_h = self.RULER_H + lanes * self.LANE_H + 8
        if int(c.cget('height')) != need_h and lanes <= 6:
            c.configure(height=need_h)

        # ルーラー
        fg = _tc("timeline_fg")
        c.create_rectangle(0, 0, w, self.RULER_H, fill=_tc("timeline_ruler"), width=0)
        steps = [1 / fps, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600]
        step = next((s for s in steps if s * self._pps >= 70), 3600)
        t = (self._t0 // step) * step
        t_end = self._t(w)
        while t <= t_end:
            x = self._x(t)
            c.create_line(x, self.RULER_H - 6, x, h, fill=_tc("timeline_grid"))
            c.create_text(x + 3, 2, text=seconds_to_timecode(max(0.0, t), fps),
                          anchor='nw', fill=fg, font=('', 7))
            t += step

        # ブロック
        issues = self._app._validate_entries()
        sel_ids = {e.id for e in self._app._get_selected_entries()}
        for e, s, t, lane in layout:
            x1, x2 = self._x(s), self._x(t)
            if x2 < -2 or x1 > w + 2:
                continue
            y1 = self.RULER_H + 4 + lane * self.LANE_H
            y2 = y1 + self.LANE_H - 4
            if not e.enabled:
                fill = _tc("timeline_disabled")
            elif issues.get(e.id) == 'error':
                fill = "#C0392B"
            elif issues.get(e.id) == 'overlap':
                fill = "#D68910"
            elif e.style:
                fill = _tc("timeline_styled")
            else:
                fill = _tc("timeline_block")
            outline = _tc("timeline_sel") if e.id in sel_ids else _tc("timeline_border")
            rid = c.create_rectangle(x1, y1, max(x1 + 2, x2), y2, fill=fill,
                                     outline=outline, width=2 if e.id in sel_ids else 1)
            self._items[rid] = e
            if x2 - x1 > 18:
                label = e.text.replace('\n', ' ')
                maxc = max(1, int((x2 - x1 - 6) / 7))
                tid = c.create_text(max(x1, 0) + 4, (y1 + y2) / 2, anchor='w',
                                    text=label[:maxc], fill='white', font=('', 8))
                self._items[tid] = e

        # スクロールバー
        lo, hi = self._bounds()
        lo -= 5
        hi += 5
        total = max(1e-6, hi - lo)
        a = (self._t0 - lo) / total
        b = (self._t(w) - lo) / total
        self._hbar.set(max(0.0, min(1.0, a)), max(0.0, min(1.0, b)))

    # ── マウス操作 ─────────────────────────────
    def _hit(self, x, y):
        """(entry, mode) mode = 'move' | 'start' | 'end'"""
        for e, s, t, lane in reversed(self._layout):
            y1 = self.RULER_H + 4 + lane * self.LANE_H
            y2 = y1 + self.LANE_H - 4
            x1, x2 = self._x(s), self._x(t)
            if y1 <= y <= y2 and x1 - 2 <= x <= x2 + 2:
                if x2 - x1 > self.EDGE * 3:
                    if x - x1 <= self.EDGE:
                        return e, 'start'
                    if x2 - x <= self.EDGE:
                        return e, 'end'
                return e, 'move'
        return None, None

    def _on_hover(self, ev):
        e, mode = self._hit(ev.x, ev.y)
        self._canvas.configure(cursor={'start': 'sb_h_double_arrow', 'end': 'sb_h_double_arrow',
                                       'move': 'fleur'}.get(mode, ''))
        if e:
            sp = self._span(e)
            dur = sp[1] - sp[0] if sp else 0
            self._info_var.set(f"No.{e.id}  {e.start_time} → {e.end_time}  ({dur:.2f}秒)")
        else:
            self._info_var.set(seconds_to_timecode(max(0.0, self._t(ev.x)), self._fps()))

    def _on_press(self, ev):
        self._canvas.focus_set()
        e, mode = self._hit(ev.x, ev.y)
        tree = self._app._tree
        if e is None:
            self._drag = None
            if not (ev.state & 0x5):
                tree.selection_set([])
            return
        iid = str(e.id)
        if ev.state & 0x4:            # Ctrl: トグル
            if iid in tree.selection():
                tree.selection_remove(iid)
            else:
                tree.selection_add(iid)
        elif ev.state & 0x1:          # Shift: 追加
            tree.selection_add(iid)
        elif iid not in tree.selection():
            tree.selection_set(iid)
        if tree.exists(iid):
            tree.see(iid)
        fps = self._fps()
        targets = self._app._get_selected_entries() if mode == 'move' else [e]
        orig = {}
        for t in targets:
            try:
                orig[t.id] = (timecode_to_frames(t.start_time, fps),
                              timecode_to_frames(t.end_time, fps))
            except ValueError:
                pass
        self._drag = dict(entry=e, mode=mode, x0=ev.x, targets=targets, orig=orig, moved=False)

    def _snap_frames(self, frame: int, exclude: set) -> int:
        """近くのテロップ端（±8px）に吸着。"""
        if not self._snap_var.get():
            return frame
        fps = self._fps()
        tol = 8 / self._pps * _nominal_fps(fps)
        best, best_d = frame, tol + 1
        for e, s, t, _lane in self._layout:
            if e.id in exclude:
                continue
            for edge in (s, t):
                f = seconds_to_frames(edge, fps)
                d = abs(f - frame)
                if d < best_d:
                    best, best_d = f, d
        return best if best_d <= tol else frame

    def _on_motion(self, ev):
        d = self._drag
        if not d or not d['orig']:
            return
        fps = self._fps()
        nom = _nominal_fps(fps)
        df = int(round((ev.x - d['x0']) / self._pps * nom))
        if df == 0 and not d['moved']:
            return
        d['moved'] = True
        from core.timecode import frames_to_timecode
        excl = {t.id for t in d['targets']}
        snap = not (ev.state & 0x20000)   # Alt でスナップ無効
        if d['mode'] == 'move':
            # 先頭ブロックの開始/終了をスナップ基準にする
            lead = d['entry']
            ls, le = d['orig'].get(lead.id, (0, 0))
            if snap:
                ns = self._snap_frames(ls + df, excl)
                if ns != ls + df:
                    df = ns - ls
                else:
                    ne = self._snap_frames(le + df, excl)
                    df = ne - le
            min_s = min(s for s, _ in d['orig'].values())
            df = max(df, -min_s)
            for t in d['targets']:
                if t.id in d['orig']:
                    s0, e0 = d['orig'][t.id]
                    t.start_time = frames_to_timecode(s0 + df, fps)
                    t.end_time = frames_to_timecode(e0 + df, fps)
        else:
            e = d['entry']
            s0, e0 = d['orig'][e.id]
            if d['mode'] == 'start':
                ns = s0 + df
                if snap:
                    ns = self._snap_frames(ns, excl)
                e.start_time = frames_to_timecode(max(0, min(ns, e0 - 1)), fps)
            else:
                ne = e0 + df
                if snap:
                    ne = self._snap_frames(ne, excl)
                e.end_time = frames_to_timecode(max(ne, s0 + 1), fps)
        lead = d['entry']
        self._info_var.set(f"No.{lead.id}  {lead.start_time} → {lead.end_time}")
        self.redraw()

    def _on_release(self, ev):
        d = self._drag
        self._drag = None
        if d and d['moved']:
            self._app._refresh_tree()
            self._app._on_select()

    def _on_double(self, ev):
        e, _ = self._hit(ev.x, ev.y)
        if e:
            self._app._tree.selection_set(str(e.id))
            self._app.edit_selected()
        else:
            # 空き部分のダブルクリック → その位置に新規テロップ
            fps = self._fps()
            tc = seconds_to_timecode(max(0.0, self._t(ev.x)), fps)
            self._app.add_entry_at(tc)

    def _on_right(self, ev):
        e, _ = self._hit(ev.x, ev.y)
        if e and str(e.id) not in self._app._tree.selection():
            self._app._tree.selection_set(str(e.id))
        if e:
            self._app._ctx_menu.tk_popup(ev.x_root, ev.y_root)


# ─────────────────────────────────────────────
#  切り離し可能 Notebook (TearableNotebook)
# ─────────────────────────────────────────────

class TearableNotebook(ttk.Notebook):
    """
    タブを右クリック → 「切り離す」で Toplevel として切り離せる Notebook。
    切り離したウィンドウを閉じると元のタブに戻る。
    Photoshop パネル風 UI 用。
    """

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self._floated: dict[int, tk.Toplevel] = {}  # tab_idx → Toplevel
        self._tab_frames: list[tk.Widget] = []
        self._tab_titles: list[str] = []
        self._tab_weights: list[int] = []

        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label="切り離す (Tear off)", command=self._tearoff_selected)
        self._menu.add_command(label="すべてドックに戻す",  command=self.dock_all)

        self.bind('<Button-3>',    self._show_menu)
        self.bind('<Double-Button-1>', self._on_double_click)

    # ── タブ追加 (通常の add をラップ) ─────────────────
    def add_tab(self, frame: tk.Widget, title: str, weight: int = 1):
        self._tab_frames.append(frame)
        self._tab_titles.append(title)
        self._tab_weights.append(weight)
        self.add(frame, text=f"  {title}  ")

    # ── イベント ──────────────────────────────────────
    def _show_menu(self, event):
        tab = self._tab_at(event.x, event.y)
        if tab is not None:
            self._pending_tearoff_idx = tab
            try:
                self._menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._menu.grab_release()

    def _on_double_click(self, event):
        tab = self._tab_at(event.x, event.y)
        if tab is not None:
            self._pending_tearoff_idx = tab
            self._tearoff_selected()

    def _tab_at(self, x, y) -> Optional[int]:
        try:
            idx = self.index(f"@{x},{y}")
            return idx
        except tk.TclError:
            return None

    # ── 切り離し ──────────────────────────────────────
    def _tearoff_selected(self):
        idx = getattr(self, '_pending_tearoff_idx', None)
        if idx is None:
            return
        if idx in self._floated:
            return  # already floated
        self._float_tab(idx)

    def _float_tab(self, idx: int):
        if idx >= len(self._tab_frames):
            return
        frame  = self._tab_frames[idx]
        title  = self._tab_titles[idx]

        # Notebook から除去
        try:
            self.forget(frame)
        except Exception:
            pass

        # Toplevel に移管
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("900x600")

        # ドックに戻すボタン
        bar = ttk.Frame(win)
        bar.pack(fill='x', side='bottom', pady=2)
        ttk.Button(bar, text="← ドックに戻す", width=14,
                   command=lambda i=idx: self._dock_tab(i)).pack(side='right', padx=8, pady=4)

        frame.tk.call('wm', 'manage', str(frame))
        try:
            frame.tk.call('wm', 'forget', str(frame))
        except Exception:
            pass
        frame.pack_configure(fill='both', expand=True)
        frame.pack(in_=win, fill='both', expand=True)

        # 閉じたらドックに戻す
        win.protocol("WM_DELETE_WINDOW", lambda i=idx: self._dock_tab(i))
        self._floated[idx] = win

    def _dock_tab(self, idx: int):
        if idx not in self._floated:
            return
        win   = self._floated.pop(idx)
        frame = self._tab_frames[idx]
        title = self._tab_titles[idx]

        # フレームを Toplevel から回収
        try:
            frame.pack_forget()
        except Exception:
            pass
        try:
            win.destroy()
        except Exception:
            pass

        # 元の位置に再挿入（タイトル順で挿入位置を決める）
        insert_pos = min(idx, len(self.tabs()))
        self.insert(insert_pos, frame, text=f"  {title}  ")
        self.select(frame)

    def dock_all(self):
        for idx in list(self._floated.keys()):
            self._dock_tab(idx)

    def is_floated(self, idx: int) -> bool:
        return idx in self._floated


# ─────────────────────────────────────────────
#  NDI Tools インストーラー ダイアログ
# ─────────────────────────────────────────────

class NDIInstallDialog(tk.Toplevel):
    """
    NDI Tools のインストール状態を表示し、
    自動ダウンロード＆インストールをサポートするダイアログ。
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.title("NDI Tools インストール")
        self.geometry("580x480")
        self.minsize(500, 400)
        self.resizable(True, True)
        self._dl_thread = None
        self._cancel_flag = False
        self._build()
        self._check_status()
        self.grab_set()

    # ── UI ──────────────────────────────────────────────

    def _build(self):
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill='both', expand=True)

        # ── ステータスバナー ──────────────────────────
        self._banner = ttk.Label(
            outer, text="NDI Tools の状態を確認中...",
            font=('', 11, 'bold'), wraplength=520, justify='left')
        self._banner.pack(anchor='w', pady=(0, 6))

        ttk.Separator(outer).pack(fill='x', pady=6)

        # ── 説明 ─────────────────────────────────────
        desc = (
            "NDI (Network Device Interface) は NewTek が開発した映像伝送プロトコルです。\n\n"
            "DaVinci Resolve や Premiere Pro が NDI 出力している映像を、\n"
            "DavinTelop がリアルタイムで受信してテロップと合成プレビューできます。\n\n"
            "利用するには NDI Tools (無料) のインストールが必要です。"
        )
        ttk.Label(outer, text=desc, justify='left', wraplength=520,
                  font=('', 9)).pack(anchor='w', pady=(0, 8))

        ttk.Separator(outer).pack(fill='x', pady=6)

        # ── バージョン選択 ────────────────────────────
        ttk.Label(outer, text="インストールするバージョン:",
                  font=('', 9, 'bold')).pack(anchor='w')

        from core.capture import NDI_DOWNLOAD_URLS, NDI_TOOLS_PAGE
        self._NDI_URLS = NDI_DOWNLOAD_URLS
        self._NDI_PAGE = NDI_TOOLS_PAGE

        self._ver_var = tk.StringVar(value=list(NDI_DOWNLOAD_URLS.keys())[0])
        for label in NDI_DOWNLOAD_URLS:
            size = "565 MB" if "6" in label else "364 MB"
            ttk.Radiobutton(outer, text=f"{label}  ({size})",
                            variable=self._ver_var, value=label).pack(anchor='w', padx=12)

        # ── ダウンロード先 ────────────────────────────
        ttk.Label(outer, text="保存先フォルダ:",
                  font=('', 9)).pack(anchor='w', pady=(8, 0))
        path_row = ttk.Frame(outer)
        path_row.pack(fill='x', pady=2)
        from pathlib import Path
        self._save_dir_var = tk.StringVar(value=str(Path.home() / "Downloads"))
        ttk.Entry(path_row, textvariable=self._save_dir_var).pack(
            side='left', fill='x', expand=True)
        ttk.Button(path_row, text="参照", width=5,
                   command=self._pick_dir).pack(side='left', padx=4)

        # ── プログレスバー ─────────────────────────────
        ttk.Separator(outer).pack(fill='x', pady=8)

        self._prog_label = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self._prog_label,
                  foreground='#0078D7', font=('', 9)).pack(anchor='w')
        self._prog_bar = ttk.Progressbar(outer, length=540, mode='determinate')
        self._prog_bar.pack(fill='x', pady=4)
        self._speed_label = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self._speed_label,
                  foreground='gray', font=('', 8)).pack(anchor='w')

        # ── ボタン ────────────────────────────────────
        ttk.Separator(outer).pack(fill='x', pady=8)
        btn_row = ttk.Frame(outer)
        btn_row.pack(fill='x')

        self._dl_btn = ttk.Button(
            btn_row, text="⬇ 自動ダウンロード＆インストール",
            command=self._start_download)
        self._dl_btn.pack(side='left', padx=(0, 6))

        ttk.Button(btn_row, text="🌐 ブラウザで開く",
                   command=self._open_browser).pack(side='left', padx=4)

        ttk.Button(btn_row, text="■ キャンセル",
                   command=self._cancel_download).pack(side='left', padx=4)

        ttk.Button(btn_row, text="閉じる",
                   command=self.destroy).pack(side='right')

    # ── ステータス確認 ─────────────────────────────────

    def _check_status(self):
        from core.capture import check_ndi_installation, HAS_NDI
        info = check_ndi_installation()
        if info["ndilib_ok"]:
            self._banner.configure(
                text="✅ NDI Tools は正常にインストールされています",
                foreground='#059669')
            self._prog_label.set("NDI は利用可能です。DavinTelop を再起動する必要はありません。")
        elif info["dll_found"]:
            self._banner.configure(
                text=f"⚠️ NDI DLL を検出しましたが初期化に失敗しました\n{info['dll_path']}",
                foreground='#D97706')
            self._prog_label.set("DavinTelop を再起動するか、NDI Tools を再インストールしてください。")
        else:
            self._banner.configure(
                text="❌ NDI Tools がインストールされていません",
                foreground='#DC2626')
            self._prog_label.set("下のボタンからインストールしてください。")

    # ── フォルダ選択 ───────────────────────────────────

    def _pick_dir(self):
        d = filedialog.askdirectory(parent=self, initialdir=self._save_dir_var.get())
        if d:
            self._save_dir_var.set(d)

    def _open_browser(self):
        import webbrowser
        webbrowser.open(self._NDI_PAGE)

    # ── ダウンロード ───────────────────────────────────

    def _start_download(self):
        import threading
        ver  = self._ver_var.get()
        url  = self._NDI_URLS[ver]
        save = self._save_dir_var.get().strip()
        if not save:
            messagebox.showwarning("保存先未設定", "保存先フォルダを指定してください。", parent=self)
            return

        self._cancel_flag = False
        self._dl_btn.configure(state='disabled')
        self._prog_bar.configure(mode='determinate', value=0)

        import urllib.request
        from pathlib import Path

        fname   = url.split("/")[-1].replace("%20", " ")
        outpath = str(Path(save) / fname)

        self._prog_label.set(f"ダウンロード中: {fname}")

        def _download():
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "DavinTelop/1.0"})
                res = urllib.request.urlopen(req, timeout=30)
                total = int(res.headers.get("Content-Length", 0))

                chunk     = 1024 * 256   # 256 KB
                received  = 0
                start_t   = __import__('time').time()

                with open(outpath, "wb") as f:
                    while True:
                        if self._cancel_flag:
                            self.after(0, lambda: self._prog_label.set("キャンセルしました"))
                            return
                        data = res.read(chunk)
                        if not data:
                            break
                        f.write(data)
                        received += len(data)

                        if total > 0:
                            pct = received / total * 100
                            self.after(0, lambda p=pct: self._prog_bar.configure(value=p))

                        elapsed = __import__('time').time() - start_t
                        if elapsed > 0:
                            speed_mb = received / elapsed / 1024 / 1024
                            recv_mb  = received / 1024 / 1024
                            total_mb = total    / 1024 / 1024 if total else 0
                            self.after(0, lambda s=speed_mb, r=recv_mb, t=total_mb:
                                self._speed_label.set(
                                    f"{r:.0f} MB / {t:.0f} MB  ({s:.1f} MB/s)"))

                self.after(0, lambda: self._on_download_complete(outpath))

            except Exception as ex:
                self.after(0, lambda e=str(ex): (
                    self._prog_label.set(f"ダウンロードエラー: {e}"),
                    self._dl_btn.configure(state='normal'),
                ))

        self._dl_thread = threading.Thread(target=_download, daemon=True)
        self._dl_thread.start()

    def _cancel_download(self):
        self._cancel_flag = True
        self._dl_btn.configure(state='normal')
        self._prog_label.set("キャンセルしました")

    def _on_download_complete(self, path: str):
        self._prog_bar.configure(value=100)
        self._prog_label.set(f"ダウンロード完了: {path}")
        self._speed_label.set("")
        self._dl_btn.configure(state='normal')

        if messagebox.askyesno(
            "インストール",
            f"NDI Tools のダウンロードが完了しました。\n\n"
            f"保存先: {path}\n\n"
            "今すぐインストーラーを実行しますか？\n"
            "（管理者権限が必要な場合があります）",
            parent=self,
        ):
            import subprocess
            try:
                subprocess.Popen([path], shell=True)
                messagebox.showinfo(
                    "インストール後の手順",
                    "インストーラーの指示に従ってインストールを完了してください。\n\n"
                    "インストール完了後、DavinTelop を再起動すると\n"
                    "NDI 機能が使えるようになります。",
                    parent=self,
                )
            except Exception as ex:
                messagebox.showerror("起動エラー", f"インストーラーの起動に失敗しました:\n{ex}", parent=self)


# ─────────────────────────────────────────────
#  画面領域セレクター (ドラッグ選択)
# ─────────────────────────────────────────────

class RegionSelectorWindow(tk.Toplevel):
    """フルスクリーン半透明オーバーレイでドラッグ範囲を選択する。"""

    def __init__(self, parent, callback):
        super().__init__(parent)
        self._callback  = callback
        self._sx = self._sy = 0
        self._rect_id   = None
        self._size_id   = None

        # 仮想デスクトップ全体（全モニター）をカバーする
        vs = {"left": 0, "top": 0,
              "width": self.winfo_screenwidth(),
              "height": self.winfo_screenheight()}
        if HAS_CAPTURE:
            try:
                from core.capture import get_virtual_screen
                vs = get_virtual_screen()
            except Exception:
                pass
        self._vx = vs["left"]
        self._vy = vs["top"]
        sw, sh   = vs["width"], vs["height"]

        # left/top が負になりうる（プライマリより左/上のモニター）
        self.geometry(f"{sw}x{sh}+{self._vx}+{self._vy}")
        self.overrideredirect(True)
        self.attributes('-topmost', True)
        self.attributes('-alpha', 0.35)
        self.configure(bg='black')

        self._cv = tk.Canvas(self, bg='black', highlightthickness=0,
                             cursor='crosshair')
        self._cv.pack(fill='both', expand=True)

        # ヒント（各モニター中央付近に表示）
        self._cv.create_text(
            sw // 2, 36, fill='white', font=('', 15, 'bold'),
            text="ドラッグして範囲を選択（全モニター対応）  /  ESC でキャンセル")

        self._cv.bind('<ButtonPress-1>',   self._on_press)
        self._cv.bind('<B1-Motion>',       self._on_drag)
        self._cv.bind('<ButtonRelease-1>', self._on_release)
        self.bind('<Escape>', lambda e: self.destroy())
        self.focus_set()

    def _on_press(self, e):
        self._sx, self._sy = e.x, e.y
        for iid in (self._rect_id, self._size_id):
            if iid:
                self._cv.delete(iid)

    def _on_drag(self, e):
        if self._rect_id:
            self._cv.delete(self._rect_id)
        if self._size_id:
            self._cv.delete(self._size_id)
        x1, y1 = min(self._sx, e.x), min(self._sy, e.y)
        x2, y2 = max(self._sx, e.x), max(self._sy, e.y)
        self._rect_id = self._cv.create_rectangle(
            x1, y1, x2, y2, outline='#00B4FF', width=2)
        self._size_id = self._cv.create_text(
            x1 + 4, y2 + 14, anchor='w', fill='white', font=('', 10),
            text=f"{x2 - x1} × {y2 - y1}")

    def _on_release(self, e):
        x1 = min(self._sx, e.x)
        y1 = min(self._sy, e.y)
        x2 = max(self._sx, e.x)
        y2 = max(self._sy, e.y)
        self.destroy()
        if x2 - x1 > 10 and y2 - y1 > 10:
            # キャンバス座標 → 絶対画面座標（仮想画面オフセットを加算）
            abs_x = self._vx + x1
            abs_y = self._vy + y1
            self._callback(abs_x, abs_y, x2 - x1, y2 - y1)


# ─────────────────────────────────────────────
#  Photoshop アクション設定ガイドダイアログ
# ─────────────────────────────────────────────

class LipSyncGuideDialog(tk.Toplevel):
    """口パクの作り方を説明するダイアログ。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("口パクの作り方ガイド")
        self.geometry("600x560")
        self.resizable(True, True)

        text = tk.Text(self, wrap='word', padx=12, pady=8,
                       font=('', 10), relief='flat')
        sb = ttk.Scrollbar(self, command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)

        guide = (
            "■ 口パク（リップシンク）の作り方\n\n"
            "DavinTelop の口パクは「立ち絵（ベース）」と「口形状の画像」を\n"
            "音声に合わせて切り替えるアニメーションです。\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "【手順1】キャラクター画像を用意する\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "必要な画像（すべて同じキャンバスサイズ・透過PNG推奨）:\n"
            "  ・ベース画像（口を除いた立ち絵、または口閉じ状態の全身）\n"
            "  ・口「あ」「い」「う」「え」「お」\n"
            "  ・口「ん」「閉じ」（任意）\n\n"
            "口パーツはベースと同じ位置に重なるように作ります。\n\n"
            "■ PSD から自動抽出する場合\n"
            "  ・口レイヤーの名前に「口」と母音を入れる\n"
            "    例: 口_あ / 口_い / 口_う / 口_え / 口_お / 口_閉じ\n"
            "  ・「キャラクター」タブで PSD を開き「レイヤー抽出」\n\n"
            "■ PNG を個別指定する場合\n"
            "  ・「キャラクター」タブでベースと各口形状の PNG を指定\n"
            "  ・口の位置がずれる場合は「口オフセット X/Y」で微調整\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "【手順2】タイミングを決める\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "「音声・タイミング」タブで口の動かし方を選びます。\n\n"
            "● VoiceVox 音素データ（高精度・推奨）\n"
            "  VoiceVox を起動し、話者とテロップを選択。\n"
            "  発音（あいうえお）に正確に合わせて口が動きます。\n\n"
            "● WAV 振幅解析（シンプル）\n"
            "  既存の WAV の音量に応じて「あ/閉じ」を切替。\n\n"
            "■ AnimType（口の滑らかさ）\n"
            "  aiueo   : 子音=閉じ。最も厳密\n"
            "  aiueo2  : 子音=次の母音。自然（推奨）\n"
            "  aiueo3  : 子音=前の形状維持。最も滑らか\n"
            "  open    : 発話中は常に「あ」。シンプル\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "【手順3】出力する\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "「出力設定」タブで形式を選んで出力します。\n\n"
            "■ 動画用ファイルとして出す（PNG連番 / GIF / APNG）\n"
            "  「▶ 生成開始」→ lipsync フォルダに出力。\n"
            "  Resolve/Premiere に読み込んで配置します。\n\n"
            "■ Resolve エフェクト（Fusion マクロ）として出す ★おすすめ\n"
            "  「🎬 Resolveエフェクト(.setting)を書き出し」をクリック。\n"
            "  ・立ち絵+口パクが Fusion マクロになります\n"
            "  ・Resolve 側で口形状・位置・サイズ・不透明度を調整可能\n"
            "  ・VoiceVox タイミングがあれば口パクのキーフレームを\n"
            "    自動で焼き込み（後から編集も可）\n\n"
            "  Resolve での使い方:\n"
            "  1. Fusion ページを開く\n"
            "  2. ノードエリアで右クリック → Macro → DavinTelop_LipSync\n"
            "  3. Inspector でパラメータを調整\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "【ヒント】音声と立ち絵をまとめて Resolve へ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "「🔊 VoiceVox」タブでは、音声生成と同時に\n"
            "立ち絵をビデオトラックへ自動配置できます\n"
            "（「立ち絵をビデオトラックに配置」をオン）。\n"
            "口パクのアニメは本ダイアログの Fusion マクロを併用すると\n"
            "セリフに合わせて口が動く立ち絵が完成します。\n"
        )
        text.insert('1.0', guide)
        text.configure(state='disabled')

        ttk.Button(self, text="閉じる", command=self.destroy).pack(pady=8)
        self.grab_set()


class RizoriputoGuideDialog(tk.Toplevel):
    """立ち絵リグは りぞりぷと(psd2tatie) で作る運用の案内ダイアログ。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("立ち絵リグの作り方（りぞりぷと連携）")
        self.geometry("640x620")
        self.resizable(True, True)

        text = tk.Text(self, wrap='word', padx=12, pady=8,
                       font=('', 10), relief='flat')
        sb = ttk.Scrollbar(self, command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)

        guide = (
            "■ 立ち絵（眉/目/口/腕を切替できるリグ）は りぞりぷと で作るのが確実です\n\n"
            "DaVinci Resolve の Fusion ジェネレータで、眉/目/口などを\n"
            "インスペクタから切り替えられる立ち絵リグは、無料ツール\n"
            "「りぞりぷと（Resolve_Script）」の psd2tatie が定番かつ確実です。\n"
            "（Fusion 上でノードをライブ生成して保存するため、確実に動作します）\n\n"
            "DavinTelop は『テロップ』『VoiceVox 音声』『口パク素材』など\n"
            "周辺を担当し、立ち絵リグ作成は りぞりぷと に任せる構成が安定です。\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "【1】りぞりぷとを導入する\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "・GitHub: nakano000 / Resolve_Script を入手\n"
            "  https://github.com/nakano000/Resolve_Script\n"
            "・解説動画（ずんだもん・めたん 目パチ口パク）:\n"
            "  YouTube『【DaVinci Resolve】ずんだもん・めたん…りぞりぷと』\n"
            "・同梱の psd2tatie / Tatie / TatieAnim を使います\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "【2】PSD から立ち絵リグを作る（psd2tatie）\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "・PSD はフォルダで分類（!口 / !目 / !眉 / !左腕 …、各オプションは *名前）\n"
            "・psd2tatie に PSD を読み込み、リグ（マクロ/ジェネレータ）を生成\n"
            "・Resolve のエフェクト → ジェネレーター に追加されます\n"
            "・インスペクタの「ポーズ」で 眉/目/口/腕 を選択\n"
            "・口パク/目パチは りぞりぷとの TatieAnim（Editページ）で付与\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "【3】DavinTelop と組み合わせる（連携）\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "● PSD分解PNG が必要なとき\n"
            "  「キャラクター」タブの PSD から本アプリでも全レイヤーを\n"
            "  個別PNGに分解できます（他用途や差し替えに）。\n\n"
            "● VoiceVox 音声\n"
            "  「🔊 VoiceVox」タブでセリフ音声を生成し、Resolve の\n"
            "  現在の再生ヘッド位置へ自動配置。りぞりぷとの口パクと\n"
            "  タイミングを合わせられます。\n\n"
            "● テロップ\n"
            "  通常どおり Text+ / 字幕として配置。立ち絵はりぞりぷと、\n"
            "  テロップは DavinTelop、と役割を分けると安定します。\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "【補足】DavinTelop 単体の立ち絵機能について\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "・「⚡ PSD分解→口パク立ち絵を自動配置（キーフレーム）」は\n"
            "  口パク付きの立ち絵を Fusion クリップとして配置します\n"
            "  （焼き込みキーフレーム方式・単体で完結）。\n"
            "・眉/目/口をインスペクタで自由に切り替える『リグ』が必要なら\n"
            "  りぞりぷと(psd2tatie) をご利用ください（本ガイドの方式）。\n"
        )
        text.insert('1.0', guide)
        text.configure(state='disabled')

        ttk.Button(self, text="閉じる", command=self.destroy).pack(pady=8)
        self.grab_set()


class PsGuideDialog(tk.Toplevel):
    """Photoshopファイル監視の設定方法を説明するダイアログ。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Photoshop アクション設定ガイド")
        self.geometry("560x500")
        self.resizable(True, True)

        text = tk.Text(self, wrap='word', padx=12, pady=8,
                       font=('', 10), relief='flat')
        sb = ttk.Scrollbar(self, command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)

        guide = (
            "■ Photoshop ファイル監視の設定方法\n\n"
            "【仕組み】\n"
            "Photoshop でテロップを編集するたびにショートカット 1 発で PNG を\n"
            "固定パスへ保存し、DavinTelop がそのファイルを監視して\n"
            "自動的に合成プレビューを更新します。\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "【手順】\n\n"
            "① 保存先ファイルを決める\n"
            "   例: C:\\Users\\ユーザー名\\Desktop\\ps_telop.png\n\n"
            "② Photoshop で「アクション」を作成\n"
            "   1. ウィンドウ → アクション でアクションパネルを開く\n"
            "   2. 新規アクション作成 → 名前を「テロップ保存」に設定\n"
            "   3. 記録開始後に下記操作を実行:\n"
            "      ・ファイル → 書き出し → 書き出し形式...\n"
            "      ・形式: PNG、透明部分: オン\n"
            "      ・上記で決めた固定パスへ保存\n"
            "   4. 記録停止\n\n"
            "③ ショートカットを割り当て\n"
            "   アクションパネル右上メニュー → アクションオプション\n"
            "   → ファンクションキー (例: F2) を設定\n\n"
            "④ DavinTelop で設定\n"
            "   合成プレビュー タブ → テロップソース\n"
            "   → 「Photoshop PNG」を選択\n"
            "   → 「参照」で上記 PNG パスを指定\n"
            "   → 「▶ 開始」をクリック\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "【テロップ作業フロー】\n\n"
            "  Photoshop でテロップを編集\n"
            "          ↓\n"
            "  F2 キーを押す (アクション実行 → PNG 保存)\n"
            "          ↓\n"
            "  DavinTelop が変化を検出 → 合成プレビュー更新\n"
            "          ↓\n"
            "  Premiere Pro / Resolve の映像に重なった\n"
            "  仕上がりをリアルタイムで確認\n"
        )
        text.insert('1.0', guide)
        text.configure(state='disabled')

        ttk.Button(self, text="閉じる", command=self.destroy).pack(pady=8)
        self.grab_set()


# ─────────────────────────────────────────────
#  VoiceVox 音声生成ダイアログ
# ─────────────────────────────────────────────

class TranscribeDialog(tk.Toplevel):
    """タイムライン/動画の音声を faster-whisper で自動字幕起こしするダイアログ。"""

    def __init__(self, parent, app):
        super().__init__(parent)
        self._app = app
        self.title("自動字幕起こし (Whisper)")
        self.geometry("560x520")
        self.resizable(True, True)
        self._running = False
        self._segments = []
        self._build()

    def _build(self):
        import core.transcribe as tr
        pad = dict(padx=8, pady=4)

        if not tr.HAS_FASTER_WHISPER:
            warn = ttk.LabelFrame(self, text="音声認識エンジンについて", padding=10)
            warn.pack(fill='x', padx=10, pady=10)
            ttk.Label(warn, foreground='orange', justify='left',
                text="このEXEには faster-whisper は同梱されていません（容量大のため）。\n"
                     "PC にインストール済みの faster-whisper を自動で呼び出して実行します。\n\n"
                     "未インストールの場合はコマンドプロンプトで:\n"
                     "    pip install faster-whisper\n"
                     "を実行してください（初回はモデルの自動DLあり）。"
                ).pack(anchor='w')

        # 音声ソース
        src_lf = ttk.LabelFrame(self, text="音声ソース", padding=8)
        src_lf.pack(fill='x', padx=10, pady=(10, 4))
        self._src_mode = tk.StringVar(value="timeline")
        ttk.Radiobutton(src_lf, text="現在のタイムラインの音声をレンダーして使う",
                        variable=self._src_mode, value="timeline",
                        command=self._on_src).pack(anchor='w')
        ttk.Radiobutton(src_lf, text="動画・音声ファイルを指定",
                        variable=self._src_mode, value="file",
                        command=self._on_src).pack(anchor='w')
        self._file_row = ttk.Frame(src_lf)
        self._file_row.pack(fill='x', pady=(2, 0))
        self._file_var = tk.StringVar()
        ttk.Entry(self._file_row, textvariable=self._file_var).pack(
            side='left', fill='x', expand=True, padx=(20, 4))
        ttk.Button(self._file_row, text="参照", width=6,
                   command=self._pick_file).pack(side='left')

        # 認識設定
        cfg = ttk.LabelFrame(self, text="認識設定", padding=8)
        cfg.pack(fill='x', padx=10, pady=4)
        ttk.Label(cfg, text="モデル:").grid(row=0, column=0, sticky='e', **pad)
        self._model_var = tk.StringVar(value="small")
        mc = ttk.Combobox(cfg, textvariable=self._model_var, state='readonly',
                          values=tr.MODELS, width=12)
        mc.grid(row=0, column=1, sticky='w', **pad)
        self._model_note = ttk.Label(cfg, text=tr.MODEL_NOTE.get("small", ""),
                                     foreground='gray', font=('', 8))
        self._model_note.grid(row=0, column=2, sticky='w', **pad)
        mc.bind('<<ComboboxSelected>>', lambda e: self._model_note.configure(
            text=tr.MODEL_NOTE.get(self._model_var.get(), "")))

        ttk.Label(cfg, text="言語:").grid(row=1, column=0, sticky='e', **pad)
        self._lang_var = tk.StringVar(value="ja")
        ttk.Combobox(cfg, textvariable=self._lang_var, state='readonly',
                     values=["ja", "auto", "en"], width=8
                     ).grid(row=1, column=1, sticky='w', **pad)
        self._gpu_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg, text="GPU(CUDA)を使う", variable=self._gpu_var
                        ).grid(row=1, column=2, sticky='w', **pad)

        # 出力先
        out_lf = ttk.LabelFrame(self, text="出力", padding=8)
        out_lf.pack(fill='x', padx=10, pady=4)
        self._o_telop = tk.BooleanVar(value=True)
        self._o_srt = tk.BooleanVar(value=True)
        self._o_resolve = tk.BooleanVar(value=True)
        ttk.Checkbutton(out_lf, text="DavinTelop のテロップ一覧に読み込む",
                        variable=self._o_telop).pack(anchor='w')
        ttk.Checkbutton(out_lf, text="SRT ファイルとして書き出し",
                        variable=self._o_srt).pack(anchor='w')
        ttk.Checkbutton(out_lf, text="Resolve のタイムラインに字幕(Text+)を配置",
                        variable=self._o_resolve).pack(anchor='w')
        orow = ttk.Frame(out_lf)
        orow.pack(fill='x', pady=(4, 0))
        ttk.Label(orow, text="出力先:").pack(side='left')
        self._out_dir_var = tk.StringVar(
            value=self._app._resolve_out_dir.get()
            if hasattr(self._app, "_resolve_out_dir") else "")
        ttk.Entry(orow, textvariable=self._out_dir_var).pack(
            side='left', fill='x', expand=True, padx=4)
        ttk.Button(orow, text="参照", width=6,
                   command=self._pick_out).pack(side='left')

        # 実行・進捗
        self._prog = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._prog, foreground='gray').pack(
            anchor='w', padx=12, pady=(6, 0))
        self._bar = ttk.Progressbar(self, mode='indeterminate')
        self._bar.pack(fill='x', padx=12, pady=4)

        # プレビュー
        prev = ttk.LabelFrame(self, text="結果プレビュー", padding=4)
        prev.pack(fill='both', expand=True, padx=10, pady=4)
        self._txt = tk.Text(prev, height=6, wrap='word')
        self._txt.pack(fill='both', expand=True)

        bot = ttk.Frame(self)
        bot.pack(fill='x', padx=10, pady=8)
        ttk.Button(bot, text="閉じる", command=self.destroy).pack(side='right', padx=4)
        self._run_btn = ttk.Button(bot, text="▶ 文字起こし開始",
                                   command=self._start)
        self._run_btn.pack(side='right', padx=4)

        self._on_src()

    def _on_src(self):
        state = 'normal' if self._src_mode.get() == "file" else 'disabled'
        for w in self._file_row.winfo_children():
            try: w.configure(state=state)
            except Exception: pass

    def _pick_file(self):
        p = filedialog.askopenfilename(
            title="動画/音声ファイル",
            filetypes=[("動画/音声", "*.mp4 *.mov *.mkv *.wav *.mp3 *.m4a *.aac *.flac"),
                       ("すべて", "*.*")], parent=self)
        if p:
            self._file_var.set(p)

    def _pick_out(self):
        p = filedialog.askdirectory(title="出力フォルダ", parent=self)
        if p:
            self._out_dir_var.set(p)

    def _start(self):
        if self._running:
            return
        out_dir = self._out_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("出力先未設定", "出力フォルダを指定してください。",
                                   parent=self)
            return
        self._running = True
        self._run_btn.configure(state='disabled')
        self._bar.start(12)
        self._prog.set("準備中...")
        self._txt.delete('1.0', 'end')
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        import os
        from pathlib import Path
        import core.transcribe as tr
        try:
            out_dir = self._out_dir_var.get().strip()
            os.makedirs(out_dir, exist_ok=True)
            fps = 30.0
            rc = None

            # ── 音声取得 ──
            if self._src_mode.get() == "timeline":
                self._set_prog("Resolve 接続中...")
                from core.exporter import ResolveConnector
                rc = ResolveConnector()
                if not rc.connect():
                    return self._fail(rc.error)
                try:
                    fps = float(rc.get_timeline_info().get("fps", 30.0)) or 30.0
                except Exception:
                    fps = 30.0
                self._set_prog("タイムライン音声をレンダリング中...（時間がかかります）")
                audio = rc.render_timeline_audio(out_dir)
                if not audio:
                    return self._fail(rc.error or "音声レンダリングに失敗しました。")
            else:
                audio = self._file_var.get().strip()
                if not audio or not Path(audio).exists():
                    return self._fail("音声/動画ファイルを指定してください。")

            # ── 文字起こし ──
            self._set_prog("文字起こし中...（モデル初回はDL待ちあり）")

            def _pcb(done, total, text):
                if total > 0:
                    self._set_prog(f"文字起こし中 {done:.0f}/{total:.0f}秒")
                self.after(0, lambda t=text: (self._txt.insert('end', t + "\n"),
                                              self._txt.see('end')))

            segs = tr.transcribe(
                audio,
                model_size=self._model_var.get(),
                language=self._lang_var.get(),
                use_gpu=self._gpu_var.get(),
                progress_cb=_pcb,
            )
            self._segments = segs
            if not segs:
                return self._fail("認識結果が空でした（音声が小さい/無音の可能性）。")

            # ── 出力 ──
            done_msgs = []
            base = Path(out_dir)

            if self._o_srt.get():
                srt = tr.segments_to_srt(segs)
                srt_path = base / "transcribe.srt"
                srt_path.write_text(srt, encoding="utf-8")
                done_msgs.append(f"SRT: {srt_path}")

            telops = tr.segments_to_telops(
                segs, fps, start_id=getattr(self._app, "_next_id", 1))

            if self._o_telop.get():
                def _add():
                    self._app.telop_list.extend(telops)
                    if telops:
                        self._app._next_id = max(
                            e.id for e in self._app.telop_list) + 1
                    self._app._refresh_tree()
                self.after(0, _add)
                done_msgs.append(f"テロップ一覧に {len(telops)} 件追加")

            if self._o_resolve.get():
                self._set_prog("Resolve に字幕(Text+)を配置中...")
                if rc is None:
                    from core.exporter import ResolveConnector
                    rc = ResolveConnector()
                    rc.connect()
                try:
                    okr = rc.create_text_plus_srt(telops, fps, video_track_index=1)
                    done_msgs.append("Resolveに字幕配置: "
                                     + ("OK" if okr else f"失敗({rc.error})"))
                except Exception as e:
                    done_msgs.append(f"Resolve配置エラー: {e}")

            self._finish("完了: " + " / ".join(done_msgs))
        except Exception as ex:
            self._fail(str(ex))

    def _set_prog(self, msg):
        self.after(0, lambda: self._prog.set(msg))

    def _fail(self, msg):
        def _f():
            self._running = False
            self._bar.stop()
            self._run_btn.configure(state='normal')
            self._prog.set("エラー")
            messagebox.showerror("自動字幕起こし", msg, parent=self)
        self.after(0, _f)

    def _finish(self, msg):
        def _f():
            self._running = False
            self._bar.stop()
            self._run_btn.configure(state='normal')
            self._prog.set(msg)
            messagebox.showinfo("自動字幕起こし完了",
                f"{len(self._segments)} 件の字幕を生成しました。\n\n{msg}",
                parent=self)
        self.after(0, _f)


class WavExportDialog(tk.Toplevel):
    """テロップの VoiceVox 音声を WAV ファイルとして一括書き出しするダイアログ。"""

    def __init__(self, parent, app, entries, title="VoiceVox WAV 書き出し"):
        super().__init__(parent)
        self._app = app
        self._entries = [e for e in entries if e.enabled] if entries else []
        self._client = None
        self._styles = []          # (表示名, style_id)
        self._running = False
        self.title(title)
        self.geometry("460x420")
        self.resizable(True, True)
        self._build()
        self._try_connect()

    def _build(self):
        pad = dict(padx=8, pady=4)
        info = ttk.Label(self,
            text=f"対象テロップ: {len(self._entries)} 件",
            font=('', 10, 'bold'))
        info.pack(anchor='w', padx=12, pady=(10, 2))

        conn = ttk.LabelFrame(self, text="VoiceVox 接続", padding=8)
        conn.pack(fill='x', padx=10, pady=4)
        ttk.Label(conn, text="ホスト:").grid(row=0, column=0, sticky='e', **pad)
        self._host = tk.StringVar(value="127.0.0.1")
        ttk.Entry(conn, textvariable=self._host, width=16).grid(row=0, column=1, sticky='w', **pad)
        ttk.Label(conn, text="ポート:").grid(row=0, column=2, sticky='e', **pad)
        self._port = tk.IntVar(value=50021)
        ttk.Spinbox(conn, from_=1024, to=65535, textvariable=self._port,
                    width=7).grid(row=0, column=3, sticky='w', **pad)
        ttk.Button(conn, text="接続", width=8,
                   command=self._try_connect).grid(row=0, column=4, padx=6)
        self._conn_lbl = tk.StringVar(value="未接続")
        ttk.Label(conn, textvariable=self._conn_lbl, foreground='gray'
                  ).grid(row=1, column=0, columnspan=5, sticky='w', padx=6)

        spk = ttk.LabelFrame(self, text="話者・調整", padding=8)
        spk.pack(fill='x', padx=10, pady=4)
        ttk.Label(spk, text="話者:").grid(row=0, column=0, sticky='e', **pad)
        self._spk = tk.StringVar()
        self._spk_combo = ttk.Combobox(spk, textvariable=self._spk,
                                       state='readonly', width=34)
        self._spk_combo.grid(row=0, column=1, columnspan=3, sticky='w', **pad)
        ttk.Label(spk, text="速度:").grid(row=1, column=0, sticky='e', **pad)
        self._speed = tk.DoubleVar(value=1.0)
        ttk.Spinbox(spk, from_=0.5, to=2.0, increment=0.05, width=6,
                    textvariable=self._speed).grid(row=1, column=1, sticky='w', **pad)
        ttk.Label(spk, text="音高:").grid(row=1, column=2, sticky='e', **pad)
        self._pitch = tk.DoubleVar(value=0.0)
        ttk.Spinbox(spk, from_=-0.15, to=0.15, increment=0.01, width=6,
                    textvariable=self._pitch).grid(row=1, column=3, sticky='w', **pad)

        out = ttk.LabelFrame(self, text="出力", padding=8)
        out.pack(fill='x', padx=10, pady=4)
        orow = ttk.Frame(out); orow.pack(fill='x')
        ttk.Label(orow, text="出力先:").pack(side='left')
        self._out = tk.StringVar(
            value=self._app._resolve_out_dir.get()
            if hasattr(self._app, "_resolve_out_dir") else "")
        ttk.Entry(orow, textvariable=self._out).pack(side='left', fill='x',
                                                     expand=True, padx=4)
        ttk.Button(orow, text="参照", width=6, command=self._pick_out).pack(side='left')
        self._meta = tk.BooleanVar(value=True)
        ttk.Checkbutton(out, text="メタCSV(voicevox_meta.csv)も書き出す",
                        variable=self._meta).pack(anchor='w', pady=(4, 0))
        self._fname_text = tk.BooleanVar(value=False)
        ttk.Checkbutton(out, text="ファイル名にテキストを含める",
                        variable=self._fname_text).pack(anchor='w')

        self._prog = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._prog, foreground='gray').pack(
            anchor='w', padx=12, pady=(6, 0))
        self._bar = ttk.Progressbar(self, mode='determinate')
        self._bar.pack(fill='x', padx=12, pady=4)

        bot = ttk.Frame(self); bot.pack(fill='x', padx=10, pady=8)
        ttk.Button(bot, text="閉じる", command=self.destroy).pack(side='right', padx=4)
        self._run_btn = ttk.Button(bot, text="▶ WAV 書き出し", command=self._start)
        self._run_btn.pack(side='right', padx=4)

    def _pick_out(self):
        p = filedialog.askdirectory(title="出力フォルダ", parent=self)
        if p:
            self._out.set(p)

    def _try_connect(self):
        from core.voicevox import VoiceVoxClient
        try:
            self._client = VoiceVoxClient(host=self._host.get().strip() or "127.0.0.1",
                                          port=int(self._port.get()))
            ver = self._client.get_version()
            if not ver:
                raise RuntimeError("応答なし")
            self._styles = self._client.flat_styles()
            self._spk_combo["values"] = [s[0] for s in self._styles]
            if self._styles and not self._spk.get():
                self._spk_combo.current(0)
            self._conn_lbl.set(f"接続OK (VoiceVox {ver}) / 話者 {len(self._styles)}")
        except Exception as ex:
            self._client = None
            self._conn_lbl.set(f"未接続: {ex}")

    def _speaker_id(self):
        name = self._spk.get()
        for n, sid in self._styles:
            if n == name:
                return sid
        return self._styles[0][1] if self._styles else 0

    def _start(self):
        if self._running:
            return
        if not self._entries:
            messagebox.showwarning("WAV書き出し", "対象テロップがありません。", parent=self)
            return
        if not self._client:
            self._try_connect()
        if not self._client:
            messagebox.showwarning("WAV書き出し", "VoiceVox に接続してください。", parent=self)
            return
        out_dir = self._out.get().strip()
        if not out_dir:
            messagebox.showwarning("WAV書き出し", "出力先を指定してください。", parent=self)
            return
        self._running = True
        self._run_btn.configure(state='disabled')
        self._bar.configure(maximum=len(self._entries), value=0)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        import csv as _csv
        from pathlib import Path
        try:
            out = Path(self._out.get().strip())
            out.mkdir(parents=True, exist_ok=True)
            sid = self._speaker_id()
            rows = []
            seq = 0
            for i, e in enumerate(self._entries):
                self.after(0, lambda i=i, t=e.text[:16]:
                           self._prog.set(f"生成中 {i+1}/{len(self._entries)}: {t}"))
                try:
                    wav, _q = self._client.synthesize(
                        e.text.replace('\n', '　'), sid,
                        speed=self._speed.get(), pitch=self._pitch.get())
                except Exception as ex:
                    self.after(0, lambda x=str(ex): self._prog.set(f"エラー: {x}"))
                    continue
                seq += 1
                if self._fname_text.get():
                    import re as _re
                    safe = _re.sub(r'[\\/:*?"<>|]', '_', e.text.replace('\n', ' '))[:20]
                    fname = f"telop_{seq:04d}_{safe}.wav"
                else:
                    fname = f"telop_{seq:04d}.wav"
                (out / fname).write_bytes(wav)
                rows.append({"filename": fname, "no": e.id,
                             "start_time": e.start_time, "end_time": e.end_time,
                             "text": e.text})
                self.after(0, lambda v=i+1: self._bar.configure(value=v))
            if self._meta.get() and rows:
                with open(out / "voicevox_meta.csv", 'w', encoding='utf-8-sig',
                          newline='') as f:
                    w = _csv.DictWriter(f, fieldnames=rows[0].keys())
                    w.writeheader(); w.writerows(rows)
            self.after(0, lambda n=len(rows): self._done(n, str(out)))
        except Exception as ex:
            self.after(0, lambda x=str(ex): self._fail(x))

    def _done(self, n, out):
        self._running = False
        self._run_btn.configure(state='normal')
        self._prog.set(f"完了: {n} 件の WAV を書き出しました")
        messagebox.showinfo("WAV書き出し完了",
                            f"{n} 件の WAV を書き出しました。\n{out}", parent=self)

    def _fail(self, msg):
        self._running = False
        self._run_btn.configure(state='normal')
        self._prog.set("エラー")
        messagebox.showerror("WAV書き出し", msg, parent=self)


class VoiceVoxPanel(ttk.Frame):
    """VoiceVox を使ってテロップの音声を一括生成するパネル（ウィンドウ埋め込み可）。"""

    def __init__(self, parent, app):
        super().__init__(parent)
        self._app       = app
        self._telops    = [e for e in app.telop_list if e.enabled]
        self._client    = None
        self._styles:   list[tuple[str, int]] = []   # (表示名, style_id)
        self._wav_refs: dict = {}   # iid -> wav bytes (preview用)
        self._running   = False
        self._stop_flag = False
        self._nb        = None
        self._list_tab  = None

        self._build()
        self._try_connect()
        self._schedule_auto_refresh()   # テロップ一覧を定期自動更新

    # ── UI 構築 ──────────────────────────────────

    def _build(self):
        default_out_dir = self._app._resolve_out_dir.get()
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=8, pady=6)
        self._nb = nb

        # ─ 設定タブ ─────────────────────────────
        cfg_tab = ttk.Frame(nb, padding=10)
        nb.add(cfg_tab, text=" 設定 ")

        pad = dict(padx=6, pady=4)

        # 接続
        conn_lf = ttk.LabelFrame(cfg_tab, text="エンジン接続", padding=8)
        conn_lf.pack(fill='x', pady=(0, 8))
        conn_lf.columnconfigure(1, weight=1)

        ttk.Label(conn_lf, text="エンジン:").grid(row=0, column=0, sticky='e', **pad)
        from core.voicevox import ENGINES
        self._engine_var = tk.StringVar(value="VoiceVox")
        eng_combo = ttk.Combobox(conn_lf, textvariable=self._engine_var,
                                  values=list(ENGINES.keys()), state='readonly', width=16)
        eng_combo.grid(row=0, column=1, sticky='w', **pad)
        eng_combo.bind('<<ComboboxSelected>>', lambda e: self._on_engine_change())

        ttk.Label(conn_lf, text="ホスト:").grid(row=1, column=0, sticky='e', **pad)
        self._host_var = tk.StringVar(value="127.0.0.1")
        ttk.Entry(conn_lf, textvariable=self._host_var, width=20
                  ).grid(row=1, column=1, sticky='w', **pad)

        ttk.Label(conn_lf, text="ポート:").grid(row=1, column=2, sticky='e', **pad)
        self._port_var = tk.IntVar(value=50021)
        ttk.Spinbox(conn_lf, from_=1024, to=65535, textvariable=self._port_var,
                    width=7).grid(row=1, column=3, sticky='w', **pad)

        ttk.Button(conn_lf, text="接続テスト", width=10,
                   command=self._try_connect
                   ).grid(row=2, column=0, columnspan=4, pady=(4, 0))

        self._conn_var = tk.StringVar(value="未接続")
        ttk.Label(conn_lf, textvariable=self._conn_var,
                  foreground='gray', font=('', 9)
                  ).grid(row=3, column=0, columnspan=4, sticky='w', padx=6)

        # 話者
        spk_lf = ttk.LabelFrame(cfg_tab, text="話者・スタイル", padding=8)
        spk_lf.pack(fill='x', pady=(0, 8))
        spk_lf.columnconfigure(1, weight=1)

        ttk.Label(spk_lf, text="話者:").grid(row=0, column=0, sticky='e', **pad)
        self._spk_var = tk.StringVar()
        self._spk_combo = ttk.Combobox(spk_lf, textvariable=self._spk_var,
                                        state='readonly', width=36)
        self._spk_combo.grid(row=0, column=1, sticky='ew', **pad)

        ttk.Button(spk_lf, text="▶ プレビュー", width=11,
                   command=self._preview_voice
                   ).grid(row=0, column=2, **pad)

        # 音声パラメータ
        voice_lf = ttk.LabelFrame(cfg_tab, text="音声パラメータ", padding=8)
        voice_lf.pack(fill='x', pady=(0, 8))
        voice_lf.columnconfigure(1, weight=1)
        voice_lf.columnconfigure(3, weight=1)

        def slider_row(parent, row, col, label, var, from_, to, res):
            ttk.Label(parent, text=label).grid(row=row, column=col,   sticky='e', **pad)
            f = ttk.Frame(parent)
            f.grid(row=row, column=col+1, sticky='ew', **pad)
            ttk.Scale(f, variable=var, from_=from_, to=to,
                      orient='horizontal', length=110).pack(side='left')
            ttk.Label(f, textvariable=var, width=5, font=('', 8)).pack(side='left', padx=2)

        self._speed_var      = tk.DoubleVar(value=1.0)
        self._pitch_var      = tk.DoubleVar(value=0.0)
        self._intonation_var = tk.DoubleVar(value=1.0)
        self._volume_var     = tk.DoubleVar(value=1.0)
        self._pre_sil_var    = tk.DoubleVar(value=0.1)
        self._post_sil_var   = tk.DoubleVar(value=0.1)

        slider_row(voice_lf, 0, 0, "話速:",   self._speed_var,      0.5, 2.0, 0.05)
        slider_row(voice_lf, 0, 2, "音高:",   self._pitch_var,     -0.15, 0.15, 0.01)
        slider_row(voice_lf, 1, 0, "抑揚:",   self._intonation_var, 0.0, 2.0, 0.05)
        slider_row(voice_lf, 1, 2, "音量:",   self._volume_var,     0.0, 2.0, 0.05)
        slider_row(voice_lf, 2, 0, "前無音:", self._pre_sil_var,    0.0, 1.5, 0.05)
        slider_row(voice_lf, 2, 2, "後無音:", self._post_sil_var,   0.0, 1.5, 0.05)

        # 出力先
        out_lf = ttk.LabelFrame(cfg_tab, text="書き出し設定", padding=8)
        out_lf.pack(fill='x', pady=(0, 4))
        out_lf.columnconfigure(1, weight=1)

        ttk.Label(out_lf, text="出力先:").grid(row=0, column=0, sticky='e', **pad)
        self._out_var = tk.StringVar(value=default_out_dir)
        ttk.Entry(out_lf, textvariable=self._out_var).grid(
            row=0, column=1, sticky='ew', **pad)
        ttk.Button(out_lf, text="参照", width=5,
                   command=self._pick_out_dir).grid(row=0, column=2, **pad)

        self._meta_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(out_lf, text="タイムコード付き CSV (voicevox_meta.csv) も書き出す",
                        variable=self._meta_var).grid(
            row=1, column=0, columnspan=3, sticky='w', padx=6)

        # Resolve 送信設定
        resolve_lf = ttk.LabelFrame(cfg_tab, text="DaVinci Resolve へ送信（オプション）", padding=8)
        resolve_lf.pack(fill='x', pady=(0, 4))
        resolve_lf.columnconfigure(1, weight=1)

        self._resolve_send_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(resolve_lf, text="生成後に WAV を Resolve オーディオトラックに配置",
                        variable=self._resolve_send_var).grid(
            row=0, column=0, columnspan=3, sticky='w', padx=6, pady=2)

        ttk.Label(resolve_lf, text="オーディオトラック:").grid(row=1, column=0, sticky='e', **pad)
        self._resolve_atrack_var = tk.IntVar(value=1)
        ttk.Spinbox(resolve_lf, from_=1, to=16, textvariable=self._resolve_atrack_var,
                    width=4).grid(row=1, column=1, sticky='w', **pad)
        ttk.Label(resolve_lf, text="A (1始まり)",
                  foreground='gray', font=('',8)).grid(row=1, column=2, sticky='w')

        self._resolve_playhead_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(resolve_lf,
                        text="再生ヘッド位置を基準に配置（先頭を再生ヘッドに合わせ相対配置）",
                        variable=self._resolve_playhead_var).grid(
            row=2, column=0, columnspan=3, sticky='w', padx=6, pady=2)

        # 立ち絵をビデオトラックに配置
        self._tachie_send_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(resolve_lf, text="立ち絵をビデオトラックに配置",
                        variable=self._tachie_send_var).grid(
            row=3, column=0, columnspan=3, sticky='w', padx=6, pady=(6, 2))

        ttk.Label(resolve_lf, text="立ち絵画像:").grid(row=4, column=0, sticky='e', **pad)
        tachie_row = ttk.Frame(resolve_lf)
        tachie_row.grid(row=4, column=1, columnspan=2, sticky='ew', **pad)
        self._tachie_path_var = tk.StringVar()
        ttk.Entry(tachie_row, textvariable=self._tachie_path_var, width=24).pack(
            side='left', fill='x', expand=True)
        ttk.Button(tachie_row, text="参照", width=5,
                   command=self._pick_tachie).pack(side='left', padx=4)

        ttk.Label(resolve_lf, text="ビデオトラック:").grid(row=5, column=0, sticky='e', **pad)
        vt_row = ttk.Frame(resolve_lf)
        vt_row.grid(row=5, column=1, columnspan=2, sticky='w', **pad)
        self._tachie_vtrack_var = tk.IntVar(value=3)
        ttk.Spinbox(vt_row, from_=1, to=20, textvariable=self._tachie_vtrack_var,
                    width=4).pack(side='left')
        ttk.Label(vt_row, text="V (1始まり)", foreground='gray',
                  font=('',8)).pack(side='left', padx=4)
        self._tachie_merge_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(vt_row, text="連続テロップを1クリップに結合",
                        variable=self._tachie_merge_var).pack(side='left', padx=8)

        # 字幕テロップPNGも配置
        self._sub_send_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(resolve_lf, text="字幕（テロップPNG）もビデオトラックに配置",
                        variable=self._sub_send_var).grid(
            row=6, column=0, columnspan=3, sticky='w', padx=6, pady=(6, 2))
        ttk.Label(resolve_lf, text="ビデオトラック:").grid(row=7, column=0, sticky='e', **pad)
        svt = ttk.Frame(resolve_lf)
        svt.grid(row=7, column=1, columnspan=2, sticky='w', **pad)
        self._sub_vtrack_var = tk.IntVar(value=2)
        ttk.Spinbox(svt, from_=1, to=20, textvariable=self._sub_vtrack_var,
                    width=4).pack(side='left')
        ttk.Label(svt, text="V（字幕PNG。音声・立ち絵と同時に配置）",
                  foreground='gray', font=('',8)).pack(side='left', padx=4)

        # ─ テロップ一覧タブ ─────────────────────
        list_tab = ttk.Frame(nb, padding=8)
        nb.add(list_tab, text=" テロップ ")
        self._list_tab = list_tab

        cols = ("sel", "no", "start", "end", "text")
        self._tree = ttk.Treeview(list_tab, columns=cols, show='headings',
                                   selectmode='none', height=12)
        self._tree.heading("sel",   text="✓")
        self._tree.heading("no",    text="No.")
        self._tree.heading("start", text="開始 TC")
        self._tree.heading("end",   text="終了 TC")
        self._tree.heading("text",  text="テキスト")
        self._tree.column("sel",   width=28,  anchor='center', stretch=False)
        self._tree.column("no",    width=40,  anchor='center', stretch=False)
        self._tree.column("start", width=110, anchor='center', stretch=False)
        self._tree.column("end",   width=110, anchor='center', stretch=False)
        self._tree.column("text",  width=300, anchor='w')

        vsb = ttk.Scrollbar(list_tab, orient='vertical', command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='left', fill='y')

        self._tree.bind('<Button-1>', self._on_tree_click)
        self._tree.tag_configure('checked',   foreground='black')
        self._tree.tag_configure('unchecked', foreground='#AAAAAA')
        self._checked: set = set()
        self.refresh_telops(force=True)

        # ── 下部コントロール ──────────────────────
        bot = ttk.Frame(self, padding=(8, 0, 8, 8))
        bot.pack(fill='x')

        self._prog_var = tk.StringVar(value="")
        ttk.Label(bot, textvariable=self._prog_var,
                  foreground='#0078D7', font=('', 9)).pack(side='left')

        self._prog_bar = ttk.Progressbar(bot, length=160, mode='determinate')
        self._prog_bar.pack(side='left', padx=8)

        ttk.Button(bot, text="🔄 テロップ更新", width=12,
                   command=lambda: self.refresh_telops(force=True)).pack(side='right', padx=4)
        self._gen_btn = ttk.Button(bot, text="▶ 全テロップを生成", width=16,
                                    command=self._start_generation)
        self._gen_btn.pack(side='right', padx=4)
        ttk.Button(bot, text="■ 中断", width=8,
                   command=self._stop_generation).pack(side='right', padx=2)
        ttk.Button(bot, text="🎙 立ち絵に口パク一括", width=16,
                   command=self._apply_lipsync_all).pack(side='right', padx=4)

    def refresh_telops(self, force: bool = False):
        """
        メインアプリの現在のテロップ一覧をツリーに反映する。
        - チェック状態は維持（既存ID）。新規IDは初期チェックON
        - 内容に変化が無ければ何もしない（定期呼び出し時のちらつき防止）
        - 生成中(force=False)は更新しない
        """
        if not hasattr(self, '_tree'):
            return
        if self._running and not force:
            return

        telops = [e for e in self._app.telop_list if e.enabled]
        # 変化検出用シグネチャ
        sig = tuple((e.id, e.start_time, e.end_time, e.text) for e in telops)
        if not force and sig == getattr(self, '_telop_sig', None):
            return
        self._telop_sig = sig
        self._telops = telops

        prev_checked = getattr(self, '_checked', set())
        prev_ids     = {self._tree.set(i, 'no') for i in self._tree.get_children()} \
                       if self._tree.get_children() else set()

        for iid in self._tree.get_children():
            self._tree.delete(iid)
        new_checked = set()
        for e in telops:
            iid = str(e.id)
            # 既存IDはチェック状態維持、新規IDはON
            is_checked = (iid in prev_checked) or (str(e.id) not in prev_ids)
            mark = '✓' if is_checked else '—'
            self._tree.insert('', 'end', iid=iid,
                values=(mark, e.id, e.start_time, e.end_time,
                        e.text.replace('\n', ' ')[:50]),
                tags=('checked' if is_checked else 'unchecked',))
            if is_checked:
                new_checked.add(iid)
        self._checked = new_checked

        if self._nb is not None and self._list_tab is not None:
            try:
                self._nb.tab(self._list_tab, text=f" テロップ ({len(telops)}) ")
            except Exception:
                pass

    def _schedule_auto_refresh(self):
        """テロップ一覧を定期的に自動更新する（手動「更新」不要にする）。"""
        try:
            self.refresh_telops()
        except Exception:
            pass
        # ウィジェットが生きている間のみ継続
        try:
            self.after(1500, self._schedule_auto_refresh)
        except Exception:
            pass

    # ── 接続テスト ────────────────────────────────

    def _on_engine_change(self):
        from core.voicevox import ENGINES
        engine = self._engine_var.get()
        port   = ENGINES.get(engine, 50021)
        self._port_var.set(port)

    def _try_connect(self):
        from core.voicevox import VoiceVoxClient
        engine = self._engine_var.get()
        host   = self._host_var.get().strip() or "127.0.0.1"
        port   = self._port_var.get()
        self._client = VoiceVoxClient(host=host, engine=engine, port=port)
        ver = self._client.get_version()
        if ver:
            self._conn_var.set(f"● 接続済み  {engine} v{ver}  ({host}:{port})")
            self._load_speakers()
        else:
            self._conn_var.set(f"✗ 接続失敗  — {engine} が起動しているか確認してください")
            self._client = None

    def _load_speakers(self):
        if not self._client:
            return
        self._styles = self._client.flat_styles()
        names = [s[0] for s in self._styles]
        self._spk_combo['values'] = names
        if names:
            self._spk_combo.current(0)

    # ── ユーティリティ ────────────────────────────

    def _get_speaker_id(self) -> Optional[int]:
        sel = self._spk_var.get()
        for name, sid in self._styles:
            if name == sel:
                return sid
        return self._styles[0][1] if self._styles else None

    def _pick_out_dir(self):
        d = filedialog.askdirectory(parent=self,
                                    initialdir=self._out_var.get())
        if d:
            self._out_var.set(d)

    def _pick_tachie(self):
        p = filedialog.askopenfilename(
            parent=self, title="立ち絵画像を選択",
            filetypes=[("画像", "*.png *.jpg *.jpeg *.tga *.tif *.tiff"),
                       ("すべて", "*")])
        if p:
            self._tachie_path_var.set(p)

    def _on_tree_click(self, event):
        region = self._tree.identify_region(event.x, event.y)
        col    = self._tree.identify_column(event.x)
        iid    = self._tree.identify_row(event.y)
        if not iid or col != '#1':
            return
        if iid in self._checked:
            self._checked.discard(iid)
            self._tree.set(iid, 'sel', '—')
            self._tree.item(iid, tags=('unchecked',))
        else:
            self._checked.add(iid)
            self._tree.set(iid, 'sel', '✓')
            self._tree.item(iid, tags=('checked',))

    # ── プレビュー再生 ────────────────────────────

    def _preview_voice(self):
        if not self._client:
            messagebox.showwarning("未接続", "VoiceVox に接続してください。", parent=self)
            return
        sid = self._get_speaker_id()
        if sid is None:
            return
        # プレビュー用テキスト（最初のチェック済みテロップか固定文）
        text = "テロップのプレビュー音声です。"
        for e in self._telops:
            if str(e.id) in self._checked:
                text = e.text.replace('\n', '　')
                break

        import threading, tempfile, os
        self._prog_var.set("プレビュー生成中...")

        def _run():
            try:
                wav, _aq = self._client.synthesize(  # synthesize() は (bytes, AudioQuery) を返す
                    text, sid,
                    speed=self._speed_var.get(),
                    pitch=self._pitch_var.get(),
                    intonation=self._intonation_var.get(),
                    volume=self._volume_var.get(),
                )
                tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
                tmp.write(wav)
                tmp.close()
                self.after(0, lambda p=tmp.name: self._play_wav(p))
            except Exception as ex:
                self.after(0, lambda ex=ex: self._prog_var.set(f"エラー: {ex}"))

        threading.Thread(target=_run, daemon=True).start()

    def _play_wav(self, path: str):
        self._prog_var.set("再生中...")
        try:
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            import subprocess, platform
            if platform.system() == "Windows":
                subprocess.Popen(["start", path], shell=True)

    # ── 一括生成 ──────────────────────────────────

    def _start_generation(self):
        if not self._client:
            messagebox.showwarning("未接続", "VoiceVox に接続してください。", parent=self)
            return
        out_dir = self._out_var.get().strip()
        if not out_dir:
            messagebox.showwarning("出力先未設定",
                                   "WAV の出力先フォルダを設定してください。", parent=self)
            return
        sid = self._get_speaker_id()
        if sid is None:
            messagebox.showwarning("話者未選択", "話者を選択してください。", parent=self)
            return

        targets = [e for e in self._telops if str(e.id) in self._checked]
        if not targets:
            messagebox.showwarning("対象なし", "生成するテロップがありません。", parent=self)
            return

        import threading
        self._stop_flag = False
        self._running   = True
        self._gen_btn.configure(state='disabled')
        self._prog_bar['maximum'] = len(targets)
        self._prog_bar['value']   = 0

        def _run():
            from pathlib import Path
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            meta_rows = []
            seq = 0

            for i, entry in enumerate(targets):
                if self._stop_flag:
                    self.after(0, lambda: self._prog_var.set("中断しました"))
                    break

                self.after(0, lambda i=i, t=entry.text[:20]:
                    self._prog_var.set(f"生成中 {i+1}/{len(targets)}: {t}"))

                try:
                    wav, _aq = self._client.synthesize(
                        entry.text.replace('\n', '　'),
                        sid,
                        speed=self._speed_var.get(),
                        pitch=self._pitch_var.get(),
                        intonation=self._intonation_var.get(),
                        volume=self._volume_var.get(),
                        pre_silence=self._pre_sil_var.get(),
                        post_silence=self._post_sil_var.get(),
                    )
                    seq += 1
                    fname = f"telop_{seq:04d}.wav"
                    wav_path = out / fname
                    wav_path.write_bytes(wav)
                    meta_rows.append({
                        "filename":   fname,
                        "no":         entry.id,
                        "start_time": entry.start_time,
                        "end_time":   entry.end_time,
                        "text":       entry.text,
                        "note":       entry.note,
                    })
                    self.after(0, lambda v=i+1: setattr(
                        self._prog_bar, '__setitem__', None) or
                        self._prog_bar.configure(value=v))

                except Exception as ex:
                    self.after(0, lambda e=str(ex):
                        self._prog_var.set(f"エラー: {e}"))

            # メタ CSV
            if self._meta_var.get() and meta_rows:
                import csv
                meta_path = out / "voicevox_meta.csv"
                with open(meta_path, 'w', encoding='utf-8-sig', newline='') as f:
                    writer = csv.DictWriter(
                        f, fieldnames=meta_rows[0].keys())
                    writer.writeheader()
                    writer.writerows(meta_rows)

            # Resolve 送信（オーディオ / 字幕PNG / 立ち絵）
            need_resolve = (not self._stop_flag and meta_rows and
                            (self._resolve_send_var.get()
                             or self._tachie_send_var.get()
                             or self._sub_send_var.get()))
            if need_resolve:
                self.after(0, lambda: self._prog_var.set("Resolve に送信中..."))
                try:
                    from core.exporter import ResolveConnector
                    rc = ResolveConnector()
                    if rc.connect():
                        fps_tl = rc.get_timeline_info().get("fps", 29.97)
                        sent_entries = [e for e in targets
                                        if str(e.id) in self._checked][:len(meta_rows)]
                        ph = self._resolve_playhead_var.get()

                        # オーディオ送信
                        if self._resolve_send_var.get():
                            wav_paths = [str(out / r["filename"]) for r in meta_rows]
                            atrack = self._resolve_atrack_var.get()
                            rc.import_wavs_to_timeline(
                                sent_entries, wav_paths, fps_tl,
                                audio_track_index=atrack,
                                playhead_relative=ph)

                        # 字幕（テロップPNG）をビデオトラックに配置
                        if self._sub_send_var.get():
                            try:
                                from core.exporter import export_png as _exp_png
                                sub_dir = out / "subtitles"
                                png_paths = _exp_png(
                                    sent_entries, self._app._collect_design(),
                                    str(sub_dir))
                                rc.import_pngs_to_timeline(
                                    sent_entries, png_paths, fps_tl,
                                    track_index=self._sub_vtrack_var.get(),
                                    playhead_relative=ph)
                            except Exception as se:
                                self.after(0, lambda e=str(se):
                                    self._prog_var.set(f"字幕配置: {e[:40]}"))

                        # 立ち絵をビデオトラックに配置
                        if self._tachie_send_var.get():
                            tachie = self._tachie_path_var.get().strip()
                            if tachie:
                                okt = rc.place_tachie_to_timeline(
                                    sent_entries, tachie, fps_tl,
                                    video_track_index=self._tachie_vtrack_var.get(),
                                    playhead_relative=ph,
                                    merge_adjacent=self._tachie_merge_var.get())
                                if not okt:
                                    self.after(0, lambda m=rc.error:
                                        self._prog_var.set(f"立ち絵配置: {m[:40]}"))
                            else:
                                self.after(0, lambda: messagebox.showwarning(
                                    "立ち絵", "立ち絵画像を選択してください。", parent=self))
                    else:
                        self.after(0, lambda m=rc.error:
                            messagebox.showwarning("Resolve送信", m, parent=self))
                except Exception as ex:
                    self.after(0, lambda e=str(ex):
                        messagebox.showwarning("Resolve送信", f"送信エラー:\n{e}", parent=self))

            def _done():
                self._running = False
                self._gen_btn.configure(state='normal')
                self._prog_var.set(
                    f"完了: {len(meta_rows)} 件の WAV を生成しました → {out}")
                # フォルダの自動オープンは行わない

            self.after(0, _done)

        threading.Thread(target=_run, daemon=True).start()

    def _stop_generation(self):
        self._stop_flag = True

    def _apply_lipsync_all(self):
        """全テロップのしゃべりに合わせて、選択中の りぞりぷと立ち絵の口を一括キーフレーム。"""
        if self._running:
            return
        targets = [e for e in self._telops if e.enabled]
        if not targets:
            messagebox.showwarning("口パク一括", "有効なテロップがありません。",
                                   parent=self)
            return
        if not self._client:
            self._try_connect()
        if not self._client:
            messagebox.showwarning("口パク一括",
                "VoiceVox に接続してください（設定タブ）。", parent=self)
            return
        sid = self._get_speaker_id()
        if sid is None:
            messagebox.showwarning("口パク一括", "話者を選択してください。", parent=self)
            return

        # Resolve 接続 & 立ち絵マクロの口入力を選択
        try:
            from core.exporter import ResolveConnector
        except Exception as ex:
            messagebox.showerror("口パク一括", str(ex), parent=self)
            return
        rc = ResolveConnector()
        if not rc.connect():
            messagebox.showwarning("口パク一括", rc.error, parent=self)
            return
        fps = float(rc.get_timeline_info().get("fps", 30.0)) or 30.0
        # 口入力は適用時に自動検出（MouthAnim）。無ければ後でピッカー。
        macro_name = None
        input_id = None

        self._running = True
        self._prog_var.set("口パク用タイミング解析中...")

        def _run():
            try:
                from core.lipsync import parse_mora_timeline
                from core.voicevox import AnimType
                from core.timecode import parse_timecode, seconds_to_frames
                combined = []
                for i, e in enumerate(targets):
                    if self._stop_flag:
                        break
                    self.after(0, lambda i=i: self._prog_var.set(
                        f"解析中 {i+1}/{len(targets)}"))
                    text = e.text.replace('\n', '　')
                    try:
                        aq = self._client.audio_query(text, sid)
                        shapes = parse_mora_timeline(aq, fps, AnimType.AIUEO2)
                    except Exception:
                        continue
                    try:
                        startf = seconds_to_frames(
                            parse_timecode(e.start_time, fps), fps)
                    except Exception:
                        startf = 0
                    endf = startf + len(shapes)
                    if len(combined) < endf:
                        combined += ["closed"] * (endf - len(combined))
                    for j, sh in enumerate(shapes):
                        combined[startf + j] = sh
                if not combined:
                    self.after(0, lambda: self._prog_var.set(
                        "口パク: タイミングを取得できませんでした"))
                    return
                self.after(0, lambda: self._prog_var.set("立ち絵に適用中..."))
                # まず最新方式(MouthAnim)を自動検出して適用
                ok = rc.apply_lipsync_to_tatie(combined, fps=fps)
                if not ok and getattr(rc, "_need_mouth_pick", False):
                    # 旧式立ち絵 → メインスレッドで口セレクタを選ばせる
                    box = {}
                    ev = threading.Event()
                    def _ask():
                        inputs = rc.get_tatie_inputs()
                        # _pick_mouth_param は LipSyncDialog のメソッド（親ウィンドウとして使うだけ）
                        box["sel"] = (LipSyncDialog._pick_mouth_param(self.winfo_toplevel(), inputs)
                                      if inputs else (None, None))
                        ev.set()
                    self.after(0, _ask)
                    ev.wait(120)
                    mn, iid = box.get("sel", (None, None))
                    if iid:
                        ok = rc.apply_lipsync_to_tatie(
                            combined, fps=fps, input_id=iid, macro_name=mn)
                if ok:
                    self.after(0, lambda: self._prog_var.set(
                        f"口パクを適用しました（{len(targets)}セリフ）"))
                else:
                    self.after(0, lambda m=rc.error: messagebox.showerror(
                        "口パク一括", m, parent=self))
            except Exception as ex:
                self.after(0, lambda e=str(ex): messagebox.showerror(
                    "口パク一括", e, parent=self))
            finally:
                self._running = False

        threading.Thread(target=_run, daemon=True).start()


# ─────────────────────────────────────────────
#  PSD レイヤー割り当てダイアログ
# ─────────────────────────────────────────────

class PsdLayerAssignDialog(tk.Toplevel):
    """
    PSD の全レイヤーをサムネイル付きで一覧し、各レイヤーに役割
    （ベース / あ / い / う / え / お / ん / 閉じ / 無視）を割り当てる。
    """

    ROLES = [("ignore", "（無視）"), ("base", "ベース"),
             ("a", "口 あ"), ("i", "口 い"), ("u", "口 う"),
             ("e", "口 え"), ("o", "口 お"), ("N", "口 ん"),
             ("closed", "口 閉じ")]

    def __init__(self, parent, layers: dict, shape_labels: dict):
        super().__init__(parent)
        self.title("PSD レイヤー割り当て")
        self.geometry("560x640")
        self.minsize(460, 480)
        self.result = None
        self._layers = layers           # {name: PIL.Image RGBA}
        self._role_vars: dict = {}      # name -> StringVar(role key)
        self._thumb_refs: list = []     # ImageTk 参照保持
        self._build()
        self.grab_set()
        self.wait_window()

    def _build(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill='x')
        ttk.Label(top,
            text="各レイヤーの役割を選んでください。\n"
                 "「ベース」は複数選択可（下から重ねて合成）。口形状は各1つ。",
            justify='left', font=('', 9)).pack(anchor='w')

        # スクロール領域
        mid = ttk.Frame(self)
        mid.pack(fill='both', expand=True, padx=8, pady=4)
        canvas = tk.Canvas(mid, highlightthickness=0)
        sb = ttk.Scrollbar(mid, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        inner = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>',
                   lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(win, width=e.width))
        # ホイールスクロール
        canvas.bind_all('<MouseWheel>',
                        lambda e: canvas.yview_scroll(int(-e.delta/120), 'units'))

        role_values = [r[1] for r in self.ROLES]
        role_by_label = {r[1]: r[0] for r in self.ROLES}
        self._role_by_label = role_by_label

        from PIL import Image as _PIL, ImageTk as _ITk
        for name, img in self._layers.items():
            row = ttk.Frame(inner, padding=(2, 3))
            row.pack(fill='x')

            # サムネイル（透過はチェッカー上）
            try:
                thumb = img.copy()
                thumb.thumbnail((56, 56), _PIL.LANCZOS)
                bg = _make_checker_bg(max(thumb.width,1), max(thumb.height,1), sq=6)
                bg = bg.convert("RGBA")
                bg.alpha_composite(thumb)
                photo = _ITk.PhotoImage(bg)
                self._thumb_refs.append(photo)
                ttk.Label(row, image=photo).pack(side='left', padx=(0, 6))
            except Exception:
                ttk.Label(row, text="[img]").pack(side='left', padx=(0, 6))

            ttk.Label(row, text=name[:30], width=26, anchor='w').pack(side='left')

            # 初期役割を名前から推測
            guess = self._guess_role(name)
            var = tk.StringVar(value=dict(self.ROLES)[guess])
            self._role_vars[name] = var
            ttk.Combobox(row, textvariable=var, values=role_values,
                         state='readonly', width=10).pack(side='left', padx=4)

        # ボタン
        bot = ttk.Frame(self, padding=8)
        bot.pack(fill='x')
        ttk.Button(bot, text="適用", width=10, command=self._apply).pack(side='right', padx=4)
        ttk.Button(bot, text="キャンセル", width=10, command=self.destroy).pack(side='right')

    def _guess_role(self, name: str) -> str:
        low = name.lower()
        is_mouth = any(k in low for k in ('口', 'mouth', 'lip', 'kuchi'))
        if is_mouth:
            for kw, shape in [('あ','a'),('a','a'),('い','i'),('i','i'),
                              ('う','u'),('u','u'),('え','e'),('e','e'),
                              ('お','o'),('o','o'),('ん','N'),('n','N'),
                              ('閉','closed'),('close','closed')]:
                if kw in low:
                    return shape
            return 'a'
        return 'base'

    def _apply(self):
        from PIL import Image as _PIL
        base_layers = []
        mouth_imgs = {}
        for name, var in self._role_vars.items():
            role = self._role_by_label.get(var.get(), 'ignore')
            img = self._layers.get(name)
            if img is None:
                continue
            if role == 'base':
                base_layers.append(img)
            elif role in ('a', 'i', 'u', 'e', 'o', 'N', 'closed'):
                mouth_imgs[role] = img   # 同役割が複数なら後勝ち

        base_img = None
        if base_layers:
            size = base_layers[0].size
            base_img = _PIL.new("RGBA", size, (0, 0, 0, 0))
            for im in base_layers:
                if im.size != size:
                    im = im.resize(size, _PIL.LANCZOS)
                base_img = _PIL.alpha_composite(base_img, im.convert("RGBA"))

        if base_img is None and not mouth_imgs:
            messagebox.showwarning("未割り当て",
                "ベースまたは口形状を1つ以上割り当ててください。", parent=self)
            return
        self.result = (base_img, mouth_imgs)
        self.destroy()


# ─────────────────────────────────────────────
#  口パク（Lip Sync）ダイアログ
# ─────────────────────────────────────────────

class LipSyncDialog(tk.Toplevel):
    """PSD 立ち絵 + VoiceVox / WAV 音声から口パクフレームを生成するダイアログ。"""

    SHAPE_LABELS = {
        'a': 'あ', 'i': 'い', 'u': 'う',
        'e': 'え', 'o': 'お', 'N': 'ん', 'closed': '閉じ',
    }

    def __init__(self, parent, telop_list: list, design: "DesignSettings",
                 default_out_dir: str = ""):
        super().__init__(parent)
        self.title("口パク生成 — Lip Sync")
        self.geometry("780x680")
        self.minsize(680, 560)
        self.resizable(True, True)

        self._telops   = [e for e in telop_list if e.enabled]
        self._design   = design
        self._out_dir  = default_out_dir
        self._base_img = None        # PIL.Image ベース画像
        self._mouth_imgs: dict = {}  # shape_key → PIL.Image
        self._mouth_vars: dict = {}  # shape_key → tk.StringVar (path)
        self._psd_layers: dict = {}  # レイヤー名 → PIL.Image (PSD読み込み後)
        self._preview_ref  = None
        self._stop_flag    = False

        self._build()
        self.grab_set()

    # ─── UI 構築 ────────────────────────────────────────────────────────────

    def _build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=8, pady=6)

        self._nb = nb
        self._build_char_tab(nb)
        self._build_audio_tab(nb)
        self._build_output_tab(nb)
        self._build_preview_tab(nb)

        # ─ 下部 ──────────────────────────────────
        bot = ttk.Frame(self, padding=(8, 0, 8, 8))
        bot.pack(fill='x')

        self._status_var = tk.StringVar(value="")
        ttk.Label(bot, textvariable=self._status_var,
                  foreground='#0078D7', font=('', 9)).pack(side='left')
        self._prog = ttk.Progressbar(bot, length=180, mode='determinate')
        self._prog.pack(side='left', padx=8)

        ttk.Button(bot, text="閉じる",    width=8, command=self.destroy).pack(side='right', padx=4)
        ttk.Button(bot, text="■ 中断",    width=7, command=self._stop).pack(side='right', padx=2)
        self._gen_btn = ttk.Button(bot, text="▶ 生成開始", width=12,
                                    command=self._start)
        self._gen_btn.pack(side='right', padx=4)

    # ─── Tab 1: キャラクター設定 ───────────────────────────────────────────

    def _build_char_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text=" キャラクター ")
        pad = dict(padx=4, pady=3)

        # PSD 自動抽出
        psd_lf = ttk.LabelFrame(tab, text="PSD から自動抽出", padding=8)
        psd_lf.pack(fill='x', pady=(0, 6))
        psd_row = ttk.Frame(psd_lf)
        psd_row.pack(fill='x')
        self._psd_path_var = tk.StringVar()
        ttk.Entry(psd_row, textvariable=self._psd_path_var, width=40).pack(
            side='left', fill='x', expand=True)
        ttk.Button(psd_row, text="PSD を開く", width=10,
                   command=self._open_psd).pack(side='left', padx=4)
        ttk.Button(psd_row, text="自動抽出", width=8,
                   command=self._extract_psd).pack(side='left', padx=2)
        ttk.Button(psd_row, text="レイヤー割り当て...", width=14,
                   command=self._assign_psd_layers).pack(side='left', padx=2)
        self._psd_info_var = tk.StringVar(
            value="自動抽出: レイヤー名(口_あ 等)で判定 / 割り当て: 各レイヤーを手動で指定")
        ttk.Label(psd_lf, textvariable=self._psd_info_var,
                  foreground='gray', font=('', 8)).pack(anchor='w', padx=4, pady=2)

        # 個別 PNG 指定
        png_lf = ttk.LabelFrame(tab, text="口形状 PNG を個別指定", padding=8)
        png_lf.pack(fill='x', pady=(0, 6))
        png_lf.columnconfigure(1, weight=1)

        ttk.Label(png_lf, text="ベース画像:").grid(row=0, column=0, sticky='e', **pad)
        self._base_var = tk.StringVar()
        ttk.Entry(png_lf, textvariable=self._base_var, width=36).grid(
            row=0, column=1, sticky='ew', **pad)
        ttk.Button(png_lf, text="参照", width=5,
                   command=lambda: self._pick_img(self._base_var, "ベース画像")
                   ).grid(row=0, column=2, **pad)

        for r, (key, label) in enumerate(self.SHAPE_LABELS.items(), start=1):
            ttk.Label(png_lf, text=f"口「{label}」:").grid(row=r, column=0, sticky='e', **pad)
            var = tk.StringVar()
            self._mouth_vars[key] = var
            ttk.Entry(png_lf, textvariable=var, width=36).grid(
                row=r, column=1, sticky='ew', **pad)
            ttk.Button(png_lf, text="参照", width=5,
                       command=lambda v=var, lbl=label: self._pick_img(v, f"口「{lbl}」")
                       ).grid(row=r, column=2, **pad)

        # 口の位置オフセット
        pos_lf = ttk.LabelFrame(tab, text="口レイヤーの合成オフセット（ベース画像に対するずれ）", padding=8)
        pos_lf.pack(fill='x')
        ttk.Label(pos_lf, text="X:").pack(side='left')
        self._mouth_x_var = tk.IntVar(value=0)
        ttk.Spinbox(pos_lf, from_=-2000, to=2000, textvariable=self._mouth_x_var,
                    width=7).pack(side='left', padx=4)
        ttk.Label(pos_lf, text="Y:").pack(side='left')
        self._mouth_y_var = tk.IntVar(value=0)
        ttk.Spinbox(pos_lf, from_=-2000, to=2000, textvariable=self._mouth_y_var,
                    width=7).pack(side='left', padx=4)
        ttk.Label(pos_lf, text="px  (通常はゼロ。口をPNG個別指定した場合に使用)",
                  foreground='gray', font=('', 8)).pack(side='left', padx=6)

    # ─── Tab 2: 音声・タイミング ───────────────────────────────────────────

    def _build_audio_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text=" 音声・タイミング ")
        pad = dict(padx=4, pady=4)

        # タイミングソース選択
        src_lf = ttk.LabelFrame(tab, text="タイミングソース", padding=8)
        src_lf.pack(fill='x', pady=(0, 8))

        self._timing_mode = tk.StringVar(value="voicevox")
        ttk.Radiobutton(src_lf, text="VoiceVox 音素データ（高精度・推奨）",
                        variable=self._timing_mode, value="voicevox",
                        command=self._on_timing_mode).pack(anchor='w')
        ttk.Radiobutton(src_lf, text="WAV 振幅解析（シンプル）",
                        variable=self._timing_mode, value="wav",
                        command=self._on_timing_mode).pack(anchor='w')

        # AnimType 選択 (Resolve_Script 参考)
        anim_lf = ttk.LabelFrame(tab, text="口パクアニメタイプ (AnimType)", padding=8)
        anim_lf.pack(fill='x', pady=(0, 8))
        from core.voicevox import AnimType
        self._anim_type_var = tk.StringVar(value=AnimType.AIUEO2)
        for atype, alabel in AnimType.LABEL.items():
            ttk.Radiobutton(anim_lf, text=alabel, variable=self._anim_type_var,
                            value=atype).pack(anchor='w', padx=4)
        ttk.Label(anim_lf,
                  text="aiueo=子音→閉じ  aiueo2=子音→次の母音（推奨）  aiueo3=子音→前の形状  open=常開き",
                  foreground='gray', font=('', 8)).pack(anchor='w', padx=6)

        # VoiceVox 設定
        self._vv_frame = ttk.LabelFrame(tab, text="VoiceVox 設定", padding=8)
        self._vv_frame.pack(fill='x', pady=(0, 8))
        self._vv_frame.columnconfigure(1, weight=1)

        ttk.Label(self._vv_frame, text="URL:").grid(row=0, column=0, sticky='e', **pad)
        self._vv_url_var = tk.StringVar(value="http://127.0.0.1:50021")
        ttk.Entry(self._vv_frame, textvariable=self._vv_url_var, width=32
                  ).grid(row=0, column=1, sticky='ew', **pad)
        ttk.Button(self._vv_frame, text="接続テスト", width=10,
                   command=self._test_vv).grid(row=0, column=2, **pad)
        self._vv_status_var = tk.StringVar(value="")
        ttk.Label(self._vv_frame, textvariable=self._vv_status_var,
                  foreground='gray', font=('', 8)
                  ).grid(row=1, column=0, columnspan=3, sticky='w', padx=6)

        ttk.Label(self._vv_frame, text="話者:").grid(row=2, column=0, sticky='e', **pad)
        self._vv_spk_var = tk.StringVar()
        self._vv_spk_combo = ttk.Combobox(
            self._vv_frame, textvariable=self._vv_spk_var, state='readonly', width=36)
        self._vv_spk_combo.grid(row=2, column=1, columnspan=2, sticky='ew', **pad)

        ttk.Label(self._vv_frame, text="テロップ:").grid(row=3, column=0, sticky='e', **pad)
        self._vv_telop_var = tk.StringVar()
        telop_names = [f"No.{e.id}  {e.text[:30].replace(chr(10),' ')}"
                       for e in self._telops] or ["(テロップなし)"]
        self._vv_telop_combo = ttk.Combobox(
            self._vv_frame, textvariable=self._vv_telop_var,
            values=telop_names, state='readonly', width=36)
        self._vv_telop_combo.grid(row=3, column=1, columnspan=2, sticky='ew', **pad)
        if telop_names:
            self._vv_telop_combo.current(0)

        # WAV ファイル設定
        self._wav_frame = ttk.LabelFrame(tab, text="WAV ファイル", padding=8)
        self._wav_frame.columnconfigure(1, weight=1)

        ttk.Label(self._wav_frame, text="WAV:").grid(row=0, column=0, sticky='e', **pad)
        self._wav_path_var = tk.StringVar()
        ttk.Entry(self._wav_frame, textvariable=self._wav_path_var, width=36
                  ).grid(row=0, column=1, sticky='ew', **pad)
        ttk.Button(self._wav_frame, text="参照", width=5,
                   command=self._pick_wav
                   ).grid(row=0, column=2, **pad)
        ttk.Label(self._wav_frame, text="振幅閾値:").grid(row=1, column=0, sticky='e', **pad)
        self._threshold_var = tk.DoubleVar(value=0.05)
        ttk.Spinbox(self._wav_frame, from_=0.01, to=0.5, increment=0.01,
                    textvariable=self._threshold_var, width=7, format="%.2f"
                    ).grid(row=1, column=1, sticky='w', **pad)

        self._on_timing_mode()

    # ─── Tab 3: 出力設定 ──────────────────────────────────────────────────

    def _build_output_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text=" 出力設定 ")
        pad = dict(padx=4, pady=4)

        out_lf = ttk.LabelFrame(tab, text="出力先・形式", padding=8)
        out_lf.pack(fill='x', pady=(0, 8))
        out_lf.columnconfigure(1, weight=1)

        ttk.Label(out_lf, text="出力フォルダ:").grid(row=0, column=0, sticky='e', **pad)
        self._out_dir_var = tk.StringVar(value=self._out_dir)
        ttk.Entry(out_lf, textvariable=self._out_dir_var, width=36
                  ).grid(row=0, column=1, sticky='ew', **pad)
        ttk.Button(out_lf, text="参照", width=5,
                   command=self._pick_out_dir).grid(row=0, column=2, **pad)

        ttk.Label(out_lf, text="FPS:").grid(row=1, column=0, sticky='e', **pad)
        self._fps_var = tk.DoubleVar(value=24.0)
        ttk.Combobox(out_lf, textvariable=self._fps_var,
                     values=[12.0, 15.0, 24.0, 30.0], width=8
                     ).grid(row=1, column=1, sticky='w', **pad)

        ttk.Label(out_lf, text="出力形式:").grid(row=2, column=0, sticky='e', **pad)
        fmt_frame = ttk.Frame(out_lf)
        fmt_frame.grid(row=2, column=1, sticky='w', **pad)
        self._fmt_png  = tk.BooleanVar(value=True)
        self._fmt_gif  = tk.BooleanVar(value=True)
        self._fmt_apng = tk.BooleanVar(value=False)
        ttk.Checkbutton(fmt_frame, text="PNG 連番", variable=self._fmt_png).pack(side='left', padx=4)
        ttk.Checkbutton(fmt_frame, text="GIF",      variable=self._fmt_gif).pack(side='left', padx=4)
        ttk.Checkbutton(fmt_frame, text="APNG（透過）", variable=self._fmt_apng).pack(side='left', padx=4)

        # サイズ
        sz_lf = ttk.LabelFrame(tab, text="出力サイズ", padding=8)
        sz_lf.pack(fill='x')
        self._resize_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sz_lf, text="リサイズする",
                        variable=self._resize_var).pack(side='left')
        self._resize_w = tk.IntVar(value=1920)
        self._resize_h = tk.IntVar(value=1080)
        ttk.Label(sz_lf, text="W:").pack(side='left', padx=(12, 2))
        ttk.Spinbox(sz_lf, from_=100, to=7680, textvariable=self._resize_w, width=6).pack(side='left')
        ttk.Label(sz_lf, text="H:").pack(side='left', padx=(8, 2))
        ttk.Spinbox(sz_lf, from_=100, to=4320, textvariable=self._resize_h, width=6).pack(side='left')

        # ── Resolve エフェクト (Fusion マクロ) ──
        fx_lf = ttk.LabelFrame(tab, text="Resolve エフェクト (Fusion マクロ)", padding=8)
        fx_lf.pack(fill='x', pady=(8, 0))
        ttk.Label(fx_lf,
            text="立ち絵+口パクを Fusion マクロ(.setting)として書き出します。\n"
                 "Resolve 側で口形状・位置・サイズ・不透明度を調整でき、\n"
                 "VoiceVox タイミングがあれば口パクのキーフレームを焼き込みます。",
            foreground='gray', font=('', 8), justify='left').pack(anchor='w')
        self._bake_keys_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(fx_lf, text="口パクのキーフレームを焼き込む（音声・タイミングタブの設定を使用）",
                        variable=self._bake_keys_var).pack(anchor='w', pady=(4, 2))
        # 目パチ（まばたき）
        blink_row = ttk.Frame(fx_lf)
        blink_row.pack(anchor='w', pady=(0, 4))
        self._blink_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(blink_row, text="目パチ（自動まばたき）",
                        variable=self._blink_var).pack(side='left')
        ttk.Label(blink_row, text="間隔:").pack(side='left', padx=(8, 2))
        self._blink_interval_var = tk.DoubleVar(value=4.0)
        ttk.Spinbox(blink_row, from_=1.0, to=15.0, increment=0.5,
                    textvariable=self._blink_interval_var, width=5,
                    format="%.1f").pack(side='left')
        ttk.Label(blink_row, text="秒", foreground='gray').pack(side='left', padx=(1, 0))
        fxbtn = ttk.Frame(fx_lf)
        fxbtn.pack(fill='x')
        ttk.Button(fxbtn, text="🎬 Resolveエフェクト(.setting)を書き出し",
                   command=self._export_fusion_macro).pack(side='left', padx=2)
        ttk.Button(fxbtn, text="❓ 口パクの作り方",
                   command=lambda: LipSyncGuideDialog(self)).pack(side='left', padx=8)

        fxbtn2 = ttk.Frame(fx_lf)
        fxbtn2.pack(fill='x', pady=(4, 0))
        ttk.Button(fxbtn2, text="⚡ PSD分解 → 口パク立ち絵を自動配置（キーフレーム）",
                   command=self._psd_to_fusion_clip).pack(side='left', padx=2)

        fxbtn2b = ttk.Frame(fx_lf)
        fxbtn2b.pack(fill='x', pady=(4, 0))
        ttk.Button(fxbtn2b, text="🎚 PSD分解 → 立ち絵を組む（全レイヤーON/OFF・口パク無し）",
                   command=lambda: self._psd_to_fusion_clip(toggle=True)).pack(side='left', padx=2)
        ttk.Label(fxbtn2b, text="各レイヤーをチェックで表示切替",
                  foreground='gray', font=('', 8)).pack(side='left', padx=4)

        fxbtn2c = ttk.Frame(fx_lf)
        fxbtn2c.pack(fill='x', pady=(4, 0))
        ttk.Button(fxbtn2c, text="🎭 立ち絵リグの作り方（りぞりぷと連携ガイド）",
                   command=lambda: RizoriputoGuideDialog(self)).pack(side='left', padx=2)
        ttk.Button(fxbtn2c, text="📂 PSDを全レイヤーPNGに分解",
                   command=self._psd_split_only).pack(side='left', padx=2)
        ttk.Label(fxbtn2c, text="眉/目/口を切替するリグは りぞりぷと(psd2tatie) で作成",
                  foreground='gray', font=('', 8)).pack(side='left', padx=4)

        fxbtn3 = ttk.Frame(fx_lf)
        fxbtn3.pack(fill='x', pady=(4, 0))
        ttk.Button(fxbtn3, text="🧩 PSD全レイヤー → Fusionコンポに展開（Fusionで編集）",
                   command=self._psd_layers_to_fusion).pack(side='left', padx=2)
        ttk.Label(fxbtn3, text="各レイヤーを個別ノードで展開。口割当・調整はFusionで",
                  foreground='gray', font=('', 8)).pack(side='left', padx=4)

        fxbtn4 = ttk.Frame(fx_lf)
        fxbtn4.pack(fill='x', pady=(8, 0))
        ttk.Button(fxbtn4, text="🎙 りぞりぷと立ち絵に口パクを適用（しゃべりに同期）",
                   command=self._apply_lipsync_to_rizoripute).pack(side='left', padx=2)
        ttk.Label(fxbtn4, text="現在のクリップの立ち絵マクロの口を、音声に合わせてキーフレーム",
                  foreground='gray', font=('', 8)).pack(side='left', padx=4)

    # ─── Tab 4: プレビュー ─────────────────────────────────────────────────

    def _build_preview_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text=" プレビュー ")

        ctrl = ttk.Frame(tab)
        ctrl.pack(fill='x', pady=(0, 4))
        ttk.Button(ctrl, text="プレビューフレームを生成",
                   command=self._preview_frame).pack(side='left', padx=4)
        ttk.Label(ctrl, text="形状:").pack(side='left', padx=(12, 2))
        self._pv_shape_var = tk.StringVar(value='a')
        ttk.Combobox(ctrl, textvariable=self._pv_shape_var,
                     values=list(self.SHAPE_LABELS.keys()), width=8,
                     state='readonly').pack(side='left')
        ttk.Button(ctrl, text="更新", command=self._preview_frame).pack(side='left', padx=4)

        self._pv_canvas = tk.Canvas(tab, bg='#222222')
        self._pv_canvas.pack(fill='both', expand=True)
        self._pv_canvas.bind('<Configure>', lambda e: self._preview_frame())

    # ─── ハンドラ ──────────────────────────────────────────────────────────

    def _on_timing_mode(self):
        mode = self._timing_mode.get()
        if mode == "voicevox":
            self._vv_frame.pack(fill='x', pady=(0, 8))
            self._wav_frame.pack_forget()
        else:
            self._vv_frame.pack_forget()
            self._wav_frame.pack(fill='x', pady=(0, 8))

    def _pick_img(self, var: tk.StringVar, label: str):
        p = filedialog.askopenfilename(
            parent=self, title=f"{label} を選択",
            filetypes=[("画像", "*.png *.jpg *.jpeg *.bmp"), ("すべて", "*")])
        if p:
            var.set(p)

    def _pick_wav(self):
        p = filedialog.askopenfilename(
            parent=self, title="WAV ファイルを選択",
            filetypes=[("WAV", "*.wav"), ("すべて", "*")])
        if p:
            self._wav_path_var.set(p)

    def _pick_out_dir(self):
        d = filedialog.askdirectory(parent=self, initialdir=self._out_dir_var.get())
        if d:
            self._out_dir_var.set(d)

    def _open_psd(self):
        p = filedialog.askopenfilename(
            parent=self, title="PSD ファイルを選択",
            filetypes=[("PSD", "*.psd *.psb"), ("すべて", "*")])
        if p:
            self._psd_path_var.set(p)

    def _extract_psd(self):
        from core.lipsync import HAS_PSD_TOOLS, extract_psd_layers, auto_map_psd_layers
        if not HAS_PSD_TOOLS:
            messagebox.showerror("エラー",
                "psd-tools が必要です。\npip install psd-tools",
                parent=self)
            return
        path = self._psd_path_var.get().strip()
        if not path or not Path(path).exists():
            messagebox.showwarning("PSD未選択", "PSD ファイルを選択してください。", parent=self)
            return
        try:
            self._status_var.set("PSD 読み込み中...")
            self.update()
            layers = extract_psd_layers(path)
            base, mouth = auto_map_psd_layers(layers)
            self._psd_layers = layers
            if base:
                self._base_img = base
            self._mouth_imgs.update(mouth)
            # GUI に反映
            detected = list(mouth.keys())
            self._psd_info_var.set(
                f"検出: {len(layers)} レイヤー  口形状: {[self.SHAPE_LABELS.get(k,k) for k in detected]}")
            self._status_var.set(f"PSD 抽出完了 ({len(detected)} 口形状検出)")
            self._preview_frame()
        except Exception as ex:
            messagebox.showerror("PSD エラー", str(ex), parent=self)
            self._status_var.set("")

    def _assign_psd_layers(self):
        """PSD の全レイヤーを一覧表示し、各レイヤーに役割を手動割り当てする。"""
        from core.lipsync import HAS_PSD_TOOLS, extract_psd_layers
        if not HAS_PSD_TOOLS:
            messagebox.showerror("エラー",
                "psd-tools が必要です。\npip install psd-tools", parent=self)
            return
        path = self._psd_path_var.get().strip()
        if not path or not Path(path).exists():
            messagebox.showwarning("PSD未選択", "PSD ファイルを選択してください。", parent=self)
            return
        try:
            self._status_var.set("PSD レイヤー読み込み中...")
            self.update()
            layers = extract_psd_layers(path)
        except Exception as ex:
            self._status_var.set("")
            messagebox.showerror("PSD エラー", str(ex), parent=self)
            return
        if not layers:
            messagebox.showwarning("レイヤーなし", "レイヤーを取得できませんでした。", parent=self)
            return

        dlg = PsdLayerAssignDialog(self, layers, self.SHAPE_LABELS)
        if dlg.result is None:
            self._status_var.set("")
            return
        base_img, mouth_imgs = dlg.result
        if base_img is not None:
            self._base_img = base_img
        if mouth_imgs:
            self._mouth_imgs.update(mouth_imgs)
        self._psd_layers = layers
        self._psd_info_var.set(
            f"割り当て完了  口形状: {[self.SHAPE_LABELS.get(k,k) for k in mouth_imgs]}")
        self._status_var.set("レイヤー割り当て完了")
        self._preview_frame()

    def _test_vv(self):
        from core.voicevox import VoiceVoxClient
        client = VoiceVoxClient(self._vv_url_var.get().strip())
        ver = client.get_version()
        if ver:
            self._vv_status_var.set(f"● 接続済み  v{ver}")
            styles = client.flat_styles()
            self._vv_spk_combo['values'] = [s[0] for s in styles]
            self._vv_styles = styles
            if styles:
                self._vv_spk_combo.current(0)
        else:
            self._vv_status_var.set("✗ 接続失敗")

    # ─── プレビュー ────────────────────────────────────────────────────────

    def _load_images(self) -> bool:
        """GUI から画像をロードして self._base_img / _mouth_imgs を設定。"""
        if PILLOW_OK:
            from PIL import Image as _PIL
            # ベース
            bp = self._base_var.get().strip()
            if bp and Path(bp).exists():
                self._base_img = _PIL.open(bp).convert("RGBA")
            # 口
            for key, var in self._mouth_vars.items():
                p = var.get().strip()
                if p and Path(p).exists():
                    self._mouth_imgs[key] = _PIL.open(p).convert("RGBA")
        return self._base_img is not None or bool(self._mouth_imgs)

    def _preview_frame(self):
        if not PILLOW_OK:
            return
        self._load_images()
        from core.lipsync import composite_frame
        from PIL import ImageTk

        shape = self._pv_shape_var.get()
        base = self._base_img
        if base is None:
            self._pv_canvas.delete('all')
            self._pv_canvas.create_text(
                self._pv_canvas.winfo_width() // 2,
                self._pv_canvas.winfo_height() // 2,
                text="ベース画像を設定してください",
                fill='white', font=('', 11))
            return
        try:
            offset = (self._mouth_x_var.get(), self._mouth_y_var.get())
            frame  = composite_frame(base, self._mouth_imgs, shape, offset)
            cw = self._pv_canvas.winfo_width()
            ch = self._pv_canvas.winfo_height()
            if cw < 10 or ch < 10:
                return
            scale = min(cw / frame.width, ch / frame.height)
            pw, ph = int(frame.width * scale), int(frame.height * scale)
            # 透過確認用チェッカー
            from PIL import Image as _PIL
            bg = _make_checker_bg(frame.width, frame.height)
            bg = bg.convert("RGBA")
            bg = _PIL.alpha_composite(bg, frame)
            thumb = bg.resize((pw, ph), _PIL.LANCZOS)
            photo = ImageTk.PhotoImage(thumb)
            self._preview_ref = photo
            self._pv_canvas.delete('all')
            ox = (cw - pw) // 2
            oy = (ch - ph) // 2
            self._pv_canvas.create_image(ox, oy, anchor='nw', image=photo)
            self._pv_canvas.create_text(ox + 4, oy + 4,
                text=f"{frame.width}×{frame.height}  [{self.SHAPE_LABELS.get(shape, shape)}]",
                anchor='nw', fill='white', font=('', 9))
        except Exception as ex:
            self._pv_canvas.delete('all')
            self._pv_canvas.create_text(
                self._pv_canvas.winfo_width() // 2,
                self._pv_canvas.winfo_height() // 2,
                text=f"エラー: {ex}", fill='red', justify='center')

    # ─── Resolve エフェクト (Fusion マクロ) 書き出し ───────────────────────

    def _pick_mouth_param(self, inputs):
        """立ち絵マクロの公開入力から口セレクタを選ばせる。
        inputs: [(macro_name, input_name, input_id), ...]
        返値: (macro_name, input_id) or (None, None)。"""
        if not inputs:
            return (None, None)
        # 候補（口/Mouth/N000等）を上に
        kws = ("口", "mouth", "Mouth", "くち", "クチ", "lip", "Lip",
               "MultiMerge", "N000")
        def score(item):
            blob = " ".join(item)
            return 0 if any(k in blob for k in kws) else 1
        ordered = sorted(inputs, key=score)
        labels = [f"{mn} ▸ {nm}    [{iid}]" for mn, nm, iid in ordered]

        dlg = tk.Toplevel(self)
        dlg.title("口セレクタを選択")
        dlg.geometry("560x230")
        dlg.transient(self)
        ttk.Label(dlg, text="口パクで動かす『口セレクタ』の入力を選んでください",
                  font=('', 10, 'bold')).pack(anchor='w', padx=12, pady=(12, 2))
        ttk.Label(dlg, text="（0=閉じ/1=あ/2=い/3=う/4=え/5=お/6=ん の順で切替されます）\n"
                            "口に関係する入力（口 / Mouth / N000 等）を選びます。",
                  foreground='gray').pack(anchor='w', padx=12)
        var = tk.StringVar(value=labels[0])
        cb = ttk.Combobox(dlg, textvariable=var, values=labels,
                          state='readonly', width=64)
        cb.pack(padx=12, pady=10)
        result = {"mn": None, "iid": None}

        def ok():
            i = labels.index(var.get()) if var.get() in labels else 0
            result["mn"] = ordered[i][0]
            result["iid"] = ordered[i][2]
            dlg.destroy()

        btn = ttk.Frame(dlg)
        btn.pack(pady=8)
        ttk.Button(btn, text="この入力に口パクを適用", command=ok).pack(side='left', padx=4)
        ttk.Button(btn, text="キャンセル", command=dlg.destroy).pack(side='left', padx=4)
        dlg.grab_set()
        self.wait_window(dlg)
        return (result["mn"], result["iid"])

    def _apply_lipsync_to_rizoripute(self):
        """りぞりぷと VoiceBin と同方式で、現在のクリップにある立ち絵マクロの
        口パラメータに、しゃべり（VoiceVox/WAV）の口形状キーフレームを適用する。"""
        from tkinter import simpledialog
        # ── 口パクタイムライン生成（VoiceVox or WAV） ──
        fps = self._fps_var.get()
        timeline = None
        try:
            self._status_var.set("タイミング解析中...")
            self.update()
            if self._timing_mode.get() == "voicevox":
                timeline = self._build_vv_timeline(fps)
            else:
                import core.lipsync as ls
                wav = self._wav_path_var.get().strip()
                from pathlib import Path as _P
                if wav and _P(wav).exists():
                    timeline = ls.analyze_wav_timeline(
                        wav, fps, self._threshold_var.get())
        except Exception as ex:
            self._status_var.set("")
            messagebox.showerror("タイミング解析エラー", str(ex), parent=self)
            return
        if not timeline:
            self._status_var.set("")
            messagebox.showwarning("タイミングなし",
                "「音声・タイミング」タブで VoiceVox かWAVを設定してください。",
                parent=self)
            return

        # ── Resolve 接続 ──
        try:
            from core.exporter import ResolveConnector
            rc = ResolveConnector()
            if not rc.connect():
                self._status_var.set("")
                messagebox.showwarning("Resolve未接続", rc.error, parent=self)
                return
        except Exception as ex:
            self._status_var.set("")
            messagebox.showerror("Resolve接続エラー", str(ex), parent=self)
            return

        # ── 適用（まず最新方式 MouthAnim を自動検出） ──
        self._status_var.set("立ち絵に口パクを適用中...")
        self.update()
        macro_name = None
        ok = rc.apply_lipsync_to_tatie(timeline, fps=fps)
        if not ok and getattr(rc, "_need_mouth_pick", False):
            # 旧式立ち絵 → 口セレクタを選ばせる
            inputs = rc.get_tatie_inputs()
            if not inputs:
                self._status_var.set("")
                messagebox.showerror("立ち絵マクロ未検出", rc.error, parent=self)
                return
            macro_name, input_id = self._pick_mouth_param(inputs)
            if not input_id:
                self._status_var.set("")
                return
            ok = rc.apply_lipsync_to_tatie(
                timeline, fps=fps, input_id=input_id, macro_name=macro_name)
        if ok:
            used = getattr(rc, "_mouth_param_used", "")
            self._status_var.set("口パク適用完了")
            messagebox.showinfo("完了",
                "現在のクリップの立ち絵マクロにしゃべりの口パクを\n"
                "適用しました（りぞりぷと VoiceBin と同方式）。\n\n"
                f"対象マクロ: {macro_name or '(先頭のマクロ)'}\n"
                f"口パラメータ: {used}\n"
                f"フレーム数: {len(timeline)}（{fps}fps）\n\n"
                "※ 口形状の対応 0=閉じ /1=あ/2=い/3=う/4=え/5=お/6=ん\n"
                "　立ち絵の口セレクタの並びがこの順なら自然に動きます。\n"
                "　ずれる場合は口セレクタの選択肢順を合わせてください。",
                parent=self)
        else:
            self._status_var.set("")
            messagebox.showerror("口パク適用エラー",
                rc.error + "\n\n"
                "・立ち絵（りぞりぷと製）クリップを選択 or 再生ヘッドを合わせる\n"
                "・そのクリップが Fusion マクロを含むことを確認\n"
                "・口パラメータ名が正しいか確認してください。",
                parent=self)

    def _export_fusion_macro(self):
        if not self._load_images():
            messagebox.showwarning("画像未設定",
                "ベース画像と口形状を設定してください。", parent=self)
            return
        if self._base_img is None:
            messagebox.showwarning("ベース未設定", "ベース画像を設定してください。", parent=self)
            return

        out_dir = self._out_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("出力先未設定", "出力フォルダを設定してください。", parent=self)
            return

        from pathlib import Path as _P
        out = _P(out_dir) / "lipsync_macro"
        out.mkdir(parents=True, exist_ok=True)

        # 画像をディスクに用意（PSD抽出のメモリ画像も書き出す）
        def _ensure_png(img, name, path_var=None):
            if path_var is not None:
                p = path_var.get().strip()
                if p and _P(p).exists():
                    return p
            if img is None:
                return None
            dest = str(out / name)
            img.save(dest, "PNG")
            return dest

        base_path = _ensure_png(self._base_img, "base.png", self._base_var)
        mouth_paths = {}
        for key, var in self._mouth_vars.items():
            img = self._mouth_imgs.get(key)
            mp = _ensure_png(img, f"mouth_{key}.png", var)
            if mp:
                mouth_paths[key] = mp

        if not mouth_paths:
            messagebox.showwarning("口画像なし",
                "口形状の画像が1つもありません。", parent=self)
            return

        # 口パクキーフレーム（任意）
        timeline = None
        fps = self._fps_var.get()
        if self._bake_keys_var.get():
            try:
                self._status_var.set("タイミング解析中...")
                self.update()
                if self._timing_mode.get() == "voicevox":
                    timeline = self._build_vv_timeline(fps)
                else:
                    import core.lipsync as ls
                    wav = self._wav_path_var.get().strip()
                    if wav and _P(wav).exists():
                        timeline = ls.analyze_wav_timeline(
                            wav, fps, self._threshold_var.get())
            except Exception as ex:
                if not messagebox.askyesno("タイミング取得失敗",
                    f"キーフレーム生成に失敗しました:\n{ex}\n\n"
                    "キーフレーム無し（手動調整用）で続行しますか？", parent=self):
                    self._status_var.set("")
                    return
                timeline = None

        # マクロ書き出し
        try:
            from core.fusion import generate_lipsync_macro, resolve_macros_dir
            macro_path = str(out / "DavinTelop_LipSync.setting")
            generate_lipsync_macro(
                macro_path, base_path, mouth_paths,
                timeline=timeline, fps=fps,
                macro_name="DavinTelop_LipSync")
        except Exception as ex:
            self._status_var.set("")
            messagebox.showerror("マクロ書き出しエラー", str(ex), parent=self)
            return

        # Resolve の Macros フォルダへコピー提案
        from core.fusion import resolve_macros_dir
        mdir = resolve_macros_dir()
        copied = ""
        if mdir and _P(mdir).exists() is False:
            try:
                _P(mdir).mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        if mdir and _P(mdir).exists():
            if messagebox.askyesno("Resolve へインストール",
                f"Fusion Macros フォルダにコピーしますか？\n{mdir}\n\n"
                "コピーすると Fusion ページの右クリック > Macro から使えます。",
                parent=self):
                try:
                    import shutil
                    shutil.copy(macro_path, str(_P(mdir) / "DavinTelop_LipSync.setting"))
                    copied = f"\n→ Macros にインストール済み"
                except Exception as ex:
                    copied = f"\n（コピー失敗: {ex}）"

        self._status_var.set(f"Fusionマクロを書き出しました → {out}")
        messagebox.showinfo("Resolveエフェクト書き出し完了",
            f"Fusionマクロを書き出しました:\n{macro_path}{copied}\n\n"
            "【使い方】\n"
            "1. Resolve の Fusion ページを開く\n"
            "2. ノードエリアで右クリック → Macro → DavinTelop_LipSync\n"
            "   （Macros にコピーしていない場合は、\n"
            "    .setting をノードエリアにドラッグ&ドロップ）\n"
            "3. Inspector で 口形状 / 位置 / サイズ / 不透明度 を調整\n"
            "   口パクキーフレームは焼き込み済み（編集可）",
            parent=self)

    def _psd_to_fusion_clip(self, toggle: bool = False):
        """PSD を全レイヤー分解 → 各レイヤーを個別ノードで Fusion に展開し配置。
        toggle=False: 口レイヤーは口パク（Blendキーフレーム）/ まばたき
        toggle=True : キーフレーム無し、各レイヤーをチェックでON/OFFして立ち絵を組む"""
        from pathlib import Path as _P
        out_dir = self._out_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("出力先未設定", "出力フォルダを設定してください。", parent=self)
            return
        out = _P(out_dir) / "lipsync_fusion"
        out.mkdir(parents=True, exist_ok=True)

        # ── 全レイヤーを書き出し、役割（口形状/通常）を分類 ──
        # layers_roles: [(name, png_path, role)]  role=口形状キー or None（下→上の順）
        layers_roles = []
        psd = self._psd_path_var.get().strip() if hasattr(self, '_psd_path_var') else ""
        try:
            if psd and _P(psd).exists():
                self._status_var.set("PSD を全レイヤー分解中...")
                self.update()
                from core.lipsync import (HAS_PSD_TOOLS, classify_layer_role,
                                          classify_eye_role)
                if not HAS_PSD_TOOLS:
                    messagebox.showerror("エラー",
                        "psd-tools が必要です。\npip install psd-tools", parent=self)
                    return
                from core.fusion import split_psd_all_layers
                all_layers = split_psd_all_layers(psd, str(out))   # [(name, path)]
                for name, path in all_layers:
                    role = classify_layer_role(name)        # 口形状 or None
                    if role is None:
                        role = classify_eye_role(name)      # 目 or None
                    layers_roles.append((name, path, role))
            else:
                # PSD未指定 → キャラクタータブの画像（base + 口）を使用
                if not self._load_images() or self._base_img is None:
                    messagebox.showwarning("画像未設定",
                        "PSD を指定するか、キャラクタータブで画像を設定してください。",
                        parent=self)
                    return
                base_path = str(out / "base.png")
                self._base_img.save(base_path, "PNG")
                layers_roles.append(("base", base_path, None))
                for key, var in self._mouth_vars.items():
                    img = self._mouth_imgs.get(key)
                    p = var.get().strip()
                    if p and _P(p).exists():
                        layers_roles.append((f"mouth_{key}", p, key))
                    elif img is not None:
                        dest = str(out / f"mouth_{key}.png")
                        img.save(dest, "PNG")
                        layers_roles.append((f"mouth_{key}", dest, key))
        except Exception as ex:
            self._status_var.set("")
            messagebox.showerror("PSD分解エラー", str(ex), parent=self)
            return

        if not layers_roles:
            messagebox.showwarning("分解結果なし", "レイヤーが取得できませんでした。", parent=self)
            return

        _MOUTH = {"a", "i", "u", "e", "o", "N", "closed"}
        mouth_roles = sorted({r for _, _, r in layers_roles if r in _MOUTH})
        eye_roles   = sorted({r for _, _, r in layers_roles
                              if r in ("eye_open", "eye_closed")})

        # ── 口パクタイムライン（任意） ──
        timeline = None
        fps = self._fps_var.get()
        if not toggle and self._bake_keys_var.get() and mouth_roles:
            try:
                self._status_var.set("タイミング解析中...")
                self.update()
                if self._timing_mode.get() == "voicevox":
                    timeline = self._build_vv_timeline(fps)
                else:
                    import core.lipsync as ls
                    wav = self._wav_path_var.get().strip()
                    if wav and _P(wav).exists():
                        timeline = ls.analyze_wav_timeline(wav, fps, self._threshold_var.get())
            except Exception:
                timeline = None

        # ── 目パチ（まばたき）タイムライン（任意） ──
        blink_frames = None
        if (not toggle and getattr(self, '_blink_var', None) and self._blink_var.get()
                and "eye_closed" in eye_roles):
            try:
                import core.lipsync as ls
                total = (len(timeline) if timeline
                         else int(fps * 10))   # タイミング無しは10秒分
                blink_frames = ls.build_blink_timeline(
                    total, fps,
                    interval_sec=self._blink_interval_var.get(),
                    blink_frames=max(2, int(fps * 0.1)))
            except Exception:
                blink_frames = None

        # ── Resolve に全レイヤー配置 ──
        self._status_var.set("Resolve に配置中...")
        self.update()
        try:
            from core.exporter import ResolveConnector
            rc = ResolveConnector()
            if not rc.connect():
                self._status_var.set("")
                messagebox.showwarning("Resolve未接続",
                    rc.error + f"\n\nレイヤーPNGは書き出し済み:\n{out}", parent=self)
                return
            ok = rc.place_tatie_full_via_fusion_api(
                layers_roles, timeline=timeline, fps=fps,
                blink_frames=blink_frames, toggle_mode=toggle)
            if ok:
                self._status_var.set(
                    f"全{len(layers_roles)}レイヤー配置")
                if toggle:
                    messagebox.showinfo("完了（立ち絵を組む）",
                        "PSDの全レイヤーを個別ノードで配置しました。\n"
                        "キーフレームは焼き込んでいません。\n\n"
                        f"レイヤー数: {len(layers_roles)}\n"
                        f"PNG: {out}\n\n"
                        "【表示の切り替え方】\n"
                        "・EDITページ: クリップ選択 → インスペクタの\n"
                        "  「レイヤー表示」ページで各チェックをON/OFF\n"
                        "・Fusionページ: 各 Mrg_◯◯ ノードの Blend を 0/1\n"
                        "好きなレイヤー構成の立ち絵を作れます。",
                        parent=self)
                else:
                    messagebox.showinfo("完了",
                        "PSDの全レイヤーを個別ノードで Fusion に展開し、\n"
                        "再生ヘッド位置に配置しました。\n\n"
                        f"レイヤー数: {len(layers_roles)}\n"
                        f"口形状: {mouth_roles}\n"
                        f"目: {eye_roles}"
                        + ("（自動まばたき）" if blink_frames else "") + "\n"
                        f"PNG: {out}\n\n"
                        "口レイヤーは口パク、閉じ目はまばたきのキーフレームを\n"
                        "焼き込み済みです。Fusionページで編集できます。",
                        parent=self)
            else:
                self._status_var.set("")
                messagebox.showwarning("配置失敗",
                    rc.error + f"\n\nレイヤーPNGは書き出し済み:\n{out}", parent=self)
        except Exception as ex:
            self._status_var.set("")
            messagebox.showerror("Resolve配置エラー", str(ex), parent=self)

    def _psd_split_only(self):
        """PSD を全レイヤー個別 PNG に分解してフォルダを開く。
        （立ち絵リグは りぞりぷと(psd2tatie) で作成する運用向け）"""
        from pathlib import Path as _P
        psd = self._psd_path_var.get().strip() if hasattr(self, '_psd_path_var') else ""
        if not psd or not _P(psd).exists():
            messagebox.showwarning("PSD未選択",
                "「キャラクター」タブで PSD を指定してください。", parent=self)
            return
        from core.lipsync import HAS_PSD_TOOLS
        if not HAS_PSD_TOOLS:
            messagebox.showerror("エラー", "psd-tools が必要です。\npip install psd-tools",
                                 parent=self)
            return
        out_dir = self._out_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("出力先未設定", "出力フォルダを設定してください。", parent=self)
            return
        out = _P(out_dir) / "psd_layers" / _P(psd).stem
        try:
            self._status_var.set("PSD を全レイヤーに分解中...")
            self.update()
            from core.fusion import split_psd_all_layers
            layers = split_psd_all_layers(psd, str(out))
        except Exception as ex:
            self._status_var.set("")
            messagebox.showerror("PSD分解エラー", str(ex), parent=self)
            return
        self._status_var.set(f"PSD分解: {len(layers)}レイヤー → {out}")
        try:
            import os
            os.startfile(str(out))   # フォルダを開く（Windows）
        except Exception:
            pass
        messagebox.showinfo("PSD分解完了",
            f"{len(layers)} レイヤーを個別PNGに分解しました:\n{out}\n\n"
            "眉/目/口を切り替えできる立ち絵リグは りぞりぷと(psd2tatie) で\n"
            "作成してください（「🎭 立ち絵リグの作り方」ボタン参照）。",
            parent=self)

    def _psd_to_generator(self):
        """PSD を全レイヤー分解し、りぞりぷと風の立ち絵ジェネレータ(.setting)を
        作成して Resolve の Generators テンプレートに追加する。
        眉/目/口/腕などはインスペクタのカテゴリ選択で切替できる。"""
        from pathlib import Path as _P
        psd = self._psd_path_var.get().strip() if hasattr(self, '_psd_path_var') else ""
        if not psd or not _P(psd).exists():
            messagebox.showwarning("PSD未選択",
                "「キャラクター」タブで PSD を指定してください。", parent=self)
            return
        from core.lipsync import HAS_PSD_TOOLS
        if not HAS_PSD_TOOLS:
            messagebox.showerror("エラー", "psd-tools が必要です。\npip install psd-tools",
                                 parent=self)
            return
        out_dir = self._out_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("出力先未設定", "出力フォルダを設定してください。", parent=self)
            return

        # ジェネレータ名（PSDファイル名から）
        gen_label = _P(psd).stem
        out = _P(out_dir) / "tatie_generator" / gen_label
        try:
            self._status_var.set("PSD を分解中...")
            self.update()
            from core.fusion import (split_psd_all_layers, generate_tatie_generator,
                                     resolve_generators_dir, _safe)
            layers = split_psd_all_layers(psd, str(out))
            if not layers:
                messagebox.showwarning("レイヤーなし", "レイヤーが取得できませんでした。",
                                       parent=self)
                self._status_var.set("")
                return
            gen_name = "Tatie_" + _safe(gen_label)
            setting_path = str(out / f"{gen_name}.setting")
            cats = generate_tatie_generator(setting_path, layers, gen_name=gen_name)
        except Exception as ex:
            self._status_var.set("")
            messagebox.showerror("ジェネレータ生成エラー", str(ex), parent=self)
            return

        # Generators テンプレート（サブフォルダ DavinTelop）と Macros にインストール
        installed = ""
        try:
            from core.fusion import resolve_generators_dir, resolve_macros_dir
            import shutil
            done = []
            gdir = resolve_generators_dir()
            if gdir:
                sub = _P(gdir) / "DavinTelop"
                sub.mkdir(parents=True, exist_ok=True)
                shutil.copy(setting_path, str(sub / f"{gen_name}.setting"))
                done.append("Generators › DavinTelop")
            mdir = resolve_macros_dir()
            if mdir:
                _P(mdir).mkdir(parents=True, exist_ok=True)
                shutil.copy(setting_path, str(_P(mdir) / f"{gen_name}.setting"))
                done.append("Macros")
            if done:
                installed = "\n→ 追加済み: " + " / ".join(done) + "（Resolve再起動で反映）"
        except Exception as ex:
            installed = f"\n（自動追加失敗: {ex}）"

        self._status_var.set(f"立ち絵ジェネレータ作成: {len(layers)}レイヤー")
        cat_txt = "\n".join(f"  ・{c}: {len(v)}択" for c, v in cats.items()) or "  （選択カテゴリなし）"
        messagebox.showinfo("立ち絵ジェネレータ作成完了",
            f"立ち絵ジェネレータを作成しました:\n{setting_path}{installed}\n\n"
            f"カテゴリ選択:\n{cat_txt}\n\n"
            "【使い方】\n"
            "1. Resolve を必ず再起動（テンプレートは起動時に読込）\n"
            "2. エフェクト → ジェネレーター → DavinTelop → "
            + gen_name + " をタイムラインへ\n"
            "   （または Fusionページで右クリック → Macro → " + gen_name + "）\n"
            "3. インスペクタの「ポーズ」ページで眉/目/口/腕などを選択\n"
            "   位置/サイズは「Settings」ページ",
            parent=self)

    def _psd_layers_to_fusion(self):
        """PSD の全レイヤーを個別PNGに分解し、Fusionコンポ(全ノード展開)を生成して
        Resolve に自動配置する。割り当てや口パクの設定は Fusion 側で行う。"""
        from pathlib import Path as _P
        from core.lipsync import HAS_PSD_TOOLS
        psd = self._psd_path_var.get().strip() if hasattr(self, '_psd_path_var') else ""
        if not psd or not _P(psd).exists():
            messagebox.showwarning("PSD未選択",
                "「キャラクター」タブで PSD を指定してください。", parent=self)
            return
        if not HAS_PSD_TOOLS:
            messagebox.showerror("エラー", "psd-tools が必要です。\npip install psd-tools",
                                 parent=self)
            return
        out_dir = self._out_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("出力先未設定", "出力フォルダを設定してください。", parent=self)
            return
        out = _P(out_dir) / "psd_layers"

        try:
            self._status_var.set("PSD を全レイヤーに分解中...")
            self.update()
            from core.fusion import split_psd_all_layers, generate_layered_comp
            layers = split_psd_all_layers(psd, str(out))
            if not layers:
                messagebox.showwarning("レイヤーなし", "レイヤーを取得できませんでした。",
                                       parent=self)
                self._status_var.set("")
                return
            comp_path = str(out / "DavinTelop_PSD_Layers.comp")
            generate_layered_comp(comp_path, layers)
        except Exception as ex:
            self._status_var.set("")
            messagebox.showerror("分解/comp生成エラー", str(ex), parent=self)
            return

        # Resolve へ自動配置（失敗時は書き出しのみ）
        self._status_var.set("Resolve に配置中...")
        self.update()
        try:
            from core.exporter import ResolveConnector
            rc = ResolveConnector()
            if rc.connect() and rc.place_fusion_comp_at_playhead(comp_path):
                self._status_var.set(f"全{len(layers)}レイヤーをFusionコンポに展開・配置しました")
                messagebox.showinfo("完了",
                    f"PSD の {len(layers)} レイヤーを個別ノードで Fusion コンポに展開し、\n"
                    f"再生ヘッド位置に配置しました。\n\n"
                    f"PNG: {out}\n\n"
                    "【Fusionでの編集】\n"
                    "・各レイヤーは Loader ノードとして展開済み（Comments にPSD名）\n"
                    "・口レイヤーを Merge の Blend やキーフレームで切替えて\n"
                    "  口パクを組めます\n"
                    "・位置/サイズは Transform を追加して調整", parent=self)
            else:
                self._status_var.set("")
                messagebox.showwarning("配置失敗（書き出しは完了）",
                    f"{rc.error}\n\n.comp は書き出し済み:\n{comp_path}\n\n"
                    "Fusionページにドラッグ&ドロップで読み込めます。", parent=self)
        except Exception as ex:
            self._status_var.set("")
            messagebox.showwarning("Resolve配置エラー（書き出しは完了）",
                f"{ex}\n\n.comp: {comp_path}", parent=self)

    # ─── 生成 ──────────────────────────────────────────────────────────────

    def _stop(self):
        self._stop_flag = True

    def _start(self):
        if not self._load_images():
            messagebox.showwarning("画像未設定",
                "ベース画像と口形状を設定してください。\n"
                "「キャラクター」タブで PNG を指定するか PSD を抽出してください。",
                parent=self)
            return
        out_dir = self._out_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("出力先未設定", "出力フォルダを設定してください。", parent=self)
            return

        self._stop_flag = False
        self._gen_btn.configure(state='disabled')
        import threading
        threading.Thread(target=self._run_generation, args=(out_dir,), daemon=True).start()

    def _run_generation(self, out_dir: str):
        from pathlib import Path as _Path
        import core.lipsync as ls

        # タイムライン生成
        self.after(0, lambda: self._status_var.set("タイムライン生成中..."))
        fps = self._fps_var.get()
        timeline: list[str] = []

        try:
            if self._timing_mode.get() == "voicevox":
                timeline = self._build_vv_timeline(fps)
            else:
                wav = self._wav_path_var.get().strip()
                if not wav or not _Path(wav).exists():
                    self.after(0, lambda: messagebox.showwarning(
                        "WAV未設定", "WAV ファイルを選択してください。", parent=self))
                    return
                timeline = ls.analyze_wav_timeline(
                    wav, fps, self._threshold_var.get())
        except Exception as ex:
            self.after(0, lambda e=str(ex): (
                messagebox.showerror("タイムラインエラー", e, parent=self),
                self._status_var.set("エラー"),
                self._gen_btn.configure(state='normal'),
            ))
            return

        if not timeline:
            self.after(0, lambda: (
                messagebox.showwarning("タイムライン空", "タイムラインが空です。", parent=self),
                self._gen_btn.configure(state='normal'),
            ))
            return

        # フレーム生成
        total_frames = len(timeline)
        self.after(0, lambda: (
            setattr(self._prog, 'maximum', total_frames) or
            self._prog.configure(maximum=total_frames, value=0)
        ))

        base   = self._base_img
        mouths = self._mouth_imgs
        offset = (self._mouth_x_var.get(), self._mouth_y_var.get())
        resize = (self._resize_w.get(), self._resize_h.get()) if self._resize_var.get() else None

        frames = []
        for i, shape in enumerate(timeline):
            if self._stop_flag:
                self.after(0, lambda: self._status_var.set("中断"))
                break
            from PIL import Image as _PIL
            frame = ls.composite_frame(base, mouths, shape, offset)
            if resize:
                frame = frame.resize(resize, _PIL.LANCZOS)
            frames.append(frame)
            self.after(0, lambda v=i+1: self._prog.configure(value=v))
            if i % 10 == 0:
                self.after(0, lambda i=i: self._status_var.set(
                    f"フレーム生成中... {i+1}/{total_frames}"))

        if not frames:
            self.after(0, lambda: self._gen_btn.configure(state='normal'))
            return

        # 書き出し
        out = _Path(out_dir) / "lipsync"
        out.mkdir(parents=True, exist_ok=True)

        if self._fmt_png.get():
            self.after(0, lambda: self._status_var.set("PNG 連番を書き出し中..."))
            ls.export_png_sequence(frames, str(out / "frames"))

        if self._fmt_gif.get():
            self.after(0, lambda: self._status_var.set("GIF を書き出し中..."))
            ls.export_gif(frames, str(out / "lipsync.gif"), fps)

        if self._fmt_apng.get():
            self.after(0, lambda: self._status_var.set("APNG を書き出し中..."))
            try:
                ls.export_apng(frames, str(out / "lipsync.png"), fps)
            except Exception:
                pass  # 古い Pillow では未対応

        def _done():
            self._gen_btn.configure(state='normal')
            self._status_var.set(f"完了: {len(frames)} フレーム → {out}")
            import subprocess, platform
            if platform.system() == "Windows":
                subprocess.Popen(["explorer", str(out)])
        self.after(0, _done)

    def _build_vv_timeline(self, fps: float) -> list[str]:
        """VoiceVox API でテロップを合成して AnimType 付きタイムラインを返す。"""
        from core.voicevox import VoiceVoxClient, AnimType
        from core.lipsync import parse_mora_timeline

        url  = self._vv_url_var.get().strip()
        host, _, port_str = url.replace("http://","").partition(":")
        try:
            port = int(port_str) if port_str else 50021
        except ValueError:
            port = 50021
        client = VoiceVoxClient(host=host or "127.0.0.1", port=port)
        if not client.is_available():
            raise RuntimeError("VoiceVox に接続できません。")

        spk_name = self._vv_spk_var.get()
        sid = 0
        if hasattr(self, '_vv_styles'):
            for name, style_id in self._vv_styles:
                if name == spk_name:
                    sid = style_id
                    break

        idx   = self._vv_telop_combo.current()
        entry = self._telops[idx] if self._telops and idx >= 0 else None
        text  = entry.text.replace('\n', '　') if entry else "テスト音声です。"

        anim_type  = self._anim_type_var.get() if hasattr(self, '_anim_type_var') else AnimType.AIUEO2
        audio_query = client.audio_query(text, sid)
        return parse_mora_timeline(audio_query, fps, anim_type)


# ─────────────────────────────────────────────

class DavinTelopApp(TfxLayoutMixin, tk.Tk):

    # ── デフォルトショートカット ─────────────────────
    _DEFAULT_SHORTCUTS: dict = {
        "send_to_resolve":       "Control-Return",
        "export_png":            "Control-Shift-P",
        "export_srt":            "Control-Shift-T",
        "export_fcpxml":         "Control-Shift-X",
        "export_resolve_script": "Control-Shift-R",
        "place_at_playhead":     "Control-Shift-Return",
        # テロップ操作
        "new_telop":             "Control-n",
        "new_telop_quick":       "Control-Alt-n",
        "undo":                  "Control-z",
        "redo":                  "Control-y",
        "find_replace":          "Control-Shift-F",
        "focus_search":          "Control-f",
        "jump_playhead":         "Control-j",
        # テロップ入力ダイアログ内
        "dlg_submit":            "Return",
        "dlg_newline":           "Shift-Return",
        "move_left":             "Alt-Left",
        "move_right":            "Alt-Right",
        "move_up":               "Alt-Up",
        "move_down":             "Alt-Down",
        # 位置スナップ
        "snap_topleft":          "",
        "snap_topcenter":        "",
        "snap_topright":         "",
        "snap_middleleft":       "",
        "snap_middleright":      "",
        "snap_bottomleft":       "",
        "snap_bottomcenter":     "",
        "snap_bottomright":      "",
        "snap_center":           "",
    }
    _SHORTCUT_LABELS: dict = {
        "send_to_resolve":       "⚡ Resolve API送信",
        "export_png":            "PNG 書き出し",
        "export_srt":            "SRT 書き出し",
        "export_fcpxml":         "FCPxml 書き出し",
        "export_resolve_script": "🐍 Resolveスクリプト生成",
        "place_at_playhead":     "📍 選択テロップを再生ヘッドに配置",
        # テロップ操作
        "new_telop":             "＋ 新規テロップ作成",
        "new_telop_quick":       "＋ 新規テロップ作成（サブキー）",
        "undo":                  "↶ 元に戻す",
        "redo":                  "↷ やり直し",
        "find_replace":          "🔍 検索と置換",
        "focus_search":          "🔎 リスト絞り込み欄へ移動",
        "jump_playhead":         "▶ Resolve再生ヘッドを選択テロップへ移動",
        "dlg_submit":            "テロップ入力: 作成/確定キー",
        "dlg_newline":           "テロップ入力: 改行キー",
        # テロップ移動
        "move_left":             "テロップ 左に移動（ステップ）",
        "move_right":            "テロップ 右に移動（ステップ）",
        "move_up":               "テロップ 上に移動（ステップ）",
        "move_down":             "テロップ 下に移動（ステップ）",
        # 位置スナップ
        "snap_topleft":          "↖ 位置: 左上",
        "snap_topcenter":        "↑ 位置: 上中央",
        "snap_topright":         "↗ 位置: 右上",
        "snap_middleleft":       "← 位置: 左中央",
        "snap_middleright":      "→ 位置: 右中央",
        "snap_bottomleft":       "↙ 位置: 左下",
        "snap_bottomcenter":     "↓ 位置: 下中央",
        "snap_bottomright":      "↘ 位置: 右下",
        "snap_center":           "⊕ 位置: 中央（中央中央）",
    }

    _NEW_TELOP_DEFAULTS: dict = {
        "default_dur": 3.0,      # 既定の尺（秒）
        "gap_frames": 0,         # 前テロップとの間隔（フレーム）
        "start_mode": "auto",    # auto / last / playhead / qa
        "cps": 6.0,              # 文字数→尺 換算（文字/秒）
        "min_dur": 1.0,          # 文字数→尺 の最小値（秒）
        "continuous": False,     # 連続入力モード
    }
    _UNDO_LIMIT = 100

    def __init__(self):
        super().__init__()
        self.title("DavinTelop  —  テロップ作成 for DaVinci Resolve")
        _geo = self._load_app_config().get("window_geometry", "")
        self.geometry(_geo if _geo else "1440x860")
        self.minsize(1100, 700)

        self.telop_list: list[TelopEntry] = []
        self.design = DesignSettings()
        self.preset_manager = PresetManager()
        self._next_id = 1
        self._preview_image_ref = None
        self._resolve = ResolveConnector()

        # Resolve出力設定 (config から復元)
        cfg = self._load_app_config()
        self._resolve_out_dir = tk.StringVar(
            value=cfg.get("resolve_out_dir", str(Path.home() / "Desktop" / "davintelop")))
        self._resolve_track   = tk.IntVar(value=cfg.get("resolve_track", 2))

        # ショートカット (デフォルト + 保存済みで上書き)
        self._shortcuts: dict = {**self._DEFAULT_SHORTCUTS, **cfg.get("shortcuts", {})}
        # 旧既定値 Ctrl+Shift+S は「名前を付けて保存」と衝突していたので移行
        if self._shortcuts.get("export_srt") == "Control-Shift-S":
            self._shortcuts["export_srt"] = self._DEFAULT_SHORTCUTS["export_srt"]
        if not self._shortcuts.get("dlg_submit"):
            self._shortcuts["dlg_submit"] = self._DEFAULT_SHORTCUTS["dlg_submit"]

        # 新規テロップ作成設定
        self._new_telop_cfg: dict = {**self._NEW_TELOP_DEFAULTS,
                                     **cfg.get("new_telop", {})}

        # 表示設定
        self._show_timeline = tk.BooleanVar(value=cfg.get("show_timeline", True))
        self._layout_mode = tk.StringVar(value=cfg.get("layout_mode", "tfx"))     # tfx = TFX-Artist 風 / classic = 従来
        self._show_quick_add = tk.BooleanVar(value=cfg.get("show_quick_add", False))
        self._style_clipboard: Optional[dict] = None
        self._pv_entry: Optional[TelopEntry] = None
        self._insp_text_job = None
        # CSV 読み込み設定（読込メニューから変更）
        self._encoding_var = tk.StringVar(value="auto")
        self._delim_var = tk.StringVar(value="auto")

        # Undo / Redo (テロップリストのスナップショット)
        self._undo_stack: list = []
        self._redo_stack: list = []
        self._last_snapshot = None
        self._bound_keys: list = []

        # PNG ファイル名オプション
        self._include_text_in_filename = tk.BooleanVar(
            value=cfg.get("include_text_in_filename", False))

        # 再生ヘッド配置時にテロップTCを書き戻すか
        self._playhead_writeback = tk.BooleanVar(
            value=cfg.get("playhead_writeback", True))

        # りぞりぷと(Resolve_Script) のパス（りぞりぷと.exe）
        self._rizoripute_path = tk.StringVar(value=cfg.get("rizoripute_path", ""))

        # プロジェクトファイル（.dtproj）の状態
        self._current_project_path: Optional[str] = None
        self._project_dirty = False
        self._suppress_dirty = True   # 起動・読込中は変更マークを抑制
        self.after(800, self._enable_dirty_tracking)
        self.after(1200, lambda: self._toggle_tc_sync() if getattr(self, "_tc_sync", None) is not None
                   and self._tc_sync.get() and getattr(self, "_tc_session", None) is None else None)
        # オートセーブ／復元
        self.after(900, self._check_autosave_restore)
        self.after(self._AUTOSAVE_INTERVAL_MS, self._autosave_tick)
        # 話者ルール（name -> {track, color}）
        self._speaker_rules: dict = cfg.get("speaker_rules", {})

        # 右パネル テロッププレビュー state
        self._telop_pv_entry: Optional[TelopEntry] = None
        self._telop_pv_pending = False
        self._template_pv_pending = False
        self._template_pv_photo_ref = None

        # キャンバスドラッグ/入力 state
        self._canvas_scale: float = 1.0
        self._canvas_ox: int = 0
        self._canvas_oy: int = 0
        self._drag_start_x: int = 0
        self._drag_start_y: int = 0
        self._drag_start_offset_x: int = 0
        self._drag_start_offset_y: int = 0

        # フローティングパネル state
        self._panel_float_state: dict = {}   # id(frame) -> bool
        self._panel_float_info: dict = {}    # id(frame) -> (paned, idx, weight)

        # 合成プレビュー state
        self._comp_bg_frame: Optional[object] = None   # PIL.Image (背景)
        self._comp_fg_frame: Optional[object] = None   # PIL.Image (テロップ)
        self._comp_photo_ref = None
        self._comp_bg_thread: Optional[object] = None
        self._comp_fg_thread: Optional[object] = None
        self._comp_bg_queue: queue.Queue = queue.Queue()
        self._comp_fg_queue: queue.Queue = queue.Queue()
        self._comp_bg_region: Optional[tuple] = None   # (x, y, w, h)
        self._comp_fg_region: Optional[tuple] = None

        # テーマ設定
        _auto_dark = False
        if HAS_DARKDETECT:
            try:
                _auto_dark = darkdetect.isDark() or False
            except Exception:
                pass
        self._is_dark = tk.BooleanVar(value=cfg.get("dark_mode", _auto_dark))

        # TearableNotebook 参照 (UI構築後に設定)
        self._main_nb: Optional[TearableNotebook] = None

        self._setup_style()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_menu()
        self._setup_ui()
        self._refresh_preset_menu()
        self._apply_shortcuts()
        self._reset_undo()
        self._schedule_preview_update()

    # ─── スタイル ───────────────────────────────

    def _setup_style(self):
        if HAS_SV_TTK:
            sv_ttk.set_theme("dark" if self._is_dark.get() else "light")
            # ウィンドウ背景を合わせる
            self.configure(bg=_tc("window_bg"))
        else:
            style = ttk.Style(self)
            import platform
            if platform.system() == "Windows":
                for theme in ('vista', 'winnative', 'clam'):
                    try:
                        style.theme_use(theme)
                        break
                    except Exception:
                        continue
            else:
                try:
                    style.theme_use('aqua')
                except Exception:
                    pass

        style = ttk.Style(self)
        style.configure('Progress.TLabel', foreground='#0078D7')

    def _toggle_theme(self):
        """ダーク / ライト テーマを切替える。"""
        self._is_dark.set(not self._is_dark.get())
        if HAS_SV_TTK:
            sv_ttk.set_theme("dark" if self._is_dark.get() else "light")
            self.configure(bg=_tc("window_bg"))
        self._refresh_theme_colors()
        self._save_app_config()
        self._schedule_preview_update()

    def _refresh_theme_colors(self):
        """テーマ変更後にキャンバス・テキスト等の色を更新する。"""
        canvas_bg  = _tc("canvas_bg")
        preview_bg = _tc("preview_bg")
        text_bg    = _tc("text_bg")
        text_fg    = _tc("text_fg")
        lb_bg      = _tc("listbox_bg")
        lb_fg      = _tc("listbox_fg")
        lb_sel     = _tc("listbox_sel")

        # プレビューキャンバス群
        for attr, bg in [
            ("_preview_canvas",      canvas_bg),
            ("_telop_pv_canvas",     preview_bg),
            ("_template_pv_canvas",  preview_bg),
            ("_comp_canvas",         "#111111"),
        ]:
            try:
                getattr(self, attr).configure(bg=bg)
            except (AttributeError, tk.TclError):
                pass

        try:
            self._insp_text.configure(bg=text_bg, fg=text_fg, insertbackground=text_fg)
            self._timeline.redraw()
        except (AttributeError, tk.TclError):
            pass

        # テロップリスト Treeview (dark時にTreeviewのBG設定)
        style = ttk.Style(self)
        if self._is_dark.get():
            style.configure("Treeview",
                background=lb_bg, foreground=lb_fg,
                fieldbackground=lb_bg,
                rowheight=22)
            style.map("Treeview",
                background=[("selected", lb_sel)],
                foreground=[("selected", "#ffffff")])
        else:
            style.configure("Treeview",
                background=lb_bg, foreground=lb_fg,
                fieldbackground=lb_bg,
                rowheight=22)
            style.map("Treeview",
                background=[("selected", lb_sel)],
                foreground=[("selected", "#ffffff")])

    @staticmethod
    def _set_children_state(widget, state: str):
        """フレーム内の子ウィジェットをまとめて有効/無効にする。"""
        for child in widget.winfo_children():
            try:
                child.configure(state=state)
            except tk.TclError:
                pass
            DavinTelopApp._set_children_state(child, state)

    # ─── メニューバー ────────────────────────────

    def _setup_menu(self):
        mb = tk.Menu(self)
        self.configure(menu=mb)

        # ファイルメニュー
        file_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="ファイル", menu=file_menu)
        file_menu.add_command(label="新規プロジェクト",
                              command=self.new_project)
        file_menu.add_command(label="プロジェクトを開く...", accelerator="Ctrl+O",
                              command=self.load_project)
        file_menu.add_command(label="上書き保存", accelerator="Ctrl+S",
                              command=self.save_project)
        file_menu.add_command(label="名前を付けて保存...", accelerator="Ctrl+Shift+S",
                              command=self.save_project_as)
        self._add_recent_menu(file_menu)
        file_menu.add_separator()
        file_menu.add_command(label="CSVをインポート...", command=self.import_csv)
        file_menu.add_command(label="SRTをインポート...", command=self.import_srt_file)
        file_menu.add_separator()
        file_menu.add_command(label="キーボードショートカット設定...", command=self._open_shortcut_settings)
        file_menu.add_command(label="テロップ作成設定...", command=self.open_new_telop_settings)
        file_menu.add_separator()
        file_menu.add_command(label="終了", accelerator="Ctrl+Q", command=self._on_close)
        # ショートカット（Win: Control / Mac: Command）
        for mod in ("Control", "Command"):
            self.bind(f"<{mod}-o>", lambda e: self.load_project())
            self.bind(f"<{mod}-s>", lambda e: self.save_project())
            self.bind(f"<{mod}-Shift-S>", lambda e: self.save_project_as())
            self.bind(f"<{mod}-q>", lambda e: self._on_close())

        # 編集メニュー
        edit_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="編集", menu=edit_menu)
        edit_menu.add_command(label="元に戻す", command=self.undo)
        edit_menu.add_command(label="やり直し", command=self.redo)
        edit_menu.add_separator()
        edit_menu.add_command(label="新規テロップを作成...", command=self.add_entry)
        edit_menu.add_command(label="選択の直後に挿入...", accelerator="Insert",
                              command=self.insert_after_selected)
        edit_menu.add_command(label="選択行をダイアログで編集", accelerator="Enter", command=self.edit_selected)
        edit_menu.add_command(label="詳細編集でテキストを編集", accelerator="F2",
                              command=self._insp_focus_text)
        edit_menu.add_command(label="選択行を複製", accelerator="Ctrl+D", command=self.duplicate_selected)
        edit_menu.add_command(label="選択行を削除", accelerator="Delete", command=self.delete_selected)
        edit_menu.add_separator()
        edit_menu.add_command(label="コピー（TSV）", accelerator="Ctrl+C", command=self.copy_selected)
        edit_menu.add_command(label="貼り付け（TSV / 台本テキスト）", accelerator="Ctrl+V",
                              command=self.paste_entries)
        edit_menu.add_separator()
        edit_menu.add_command(label="全選択", accelerator="Ctrl+A", command=self.select_all)
        edit_menu.add_command(label="有効/無効を切り替え", accelerator="Space", command=self.toggle_enabled)
        edit_menu.add_command(label="無効なテロップを削除", command=self.delete_disabled)
        edit_menu.add_separator()
        edit_menu.add_command(label="🔍 検索と置換...", accelerator="Ctrl+Shift+F",
                              command=self.open_find_replace)
        edit_menu.add_command(label="🔎 リストを絞り込み", accelerator="Ctrl+F",
                              command=lambda: self._filter_entry.focus_set())
        edit_menu.add_separator()
        edit_menu.add_command(label="🧹 字幕を自動整形（改行・フィラー除去）...",
                              command=self.open_autoformat)
        edit_menu.add_command(label="🎨 話者の色分け・トラック振り分け...",
                              command=self.open_speaker_rules)
        self.bind("<Command-n>", lambda e: self.add_entry())
        self.bind("<Command-z>", lambda e: self._guarded(self.undo))

        # 加工メニュー
        proc_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="加工", menu=proc_menu)
        self._build_process_menu(proc_menu)

        # 書き出しメニュー
        export_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="書き出し", menu=export_menu)
        export_menu.add_command(label="PNG を書き出し...", command=self.export_png)
        export_menu.add_command(label="SRT を書き出し...", command=self.export_srt)
        export_menu.add_command(label="FCPxml を書き出し...", command=self.export_fcpxml)
        export_menu.add_command(label="AAF を書き出し (Avid)...", command=self.export_aaf)
        export_menu.add_command(label="CSV を書き出し...", command=self.export_csv_file)
        export_menu.add_separator()
        export_menu.add_command(label="🔊 VoiceVox 音声生成...", command=self.export_voicevox)
        export_menu.add_command(label="🎵 全テロップを WAV 書き出し...",
                                command=self.export_wav_all)
        export_menu.add_command(label="🎵 選択テロップを WAV 書き出し...",
                                command=self.export_wav_selected)
        export_menu.add_command(label="🎭 口パク生成...",          command=self.export_lipsync)

        # Resolveメニュー
        resolve_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="Resolve", menu=resolve_menu)
        resolve_menu.add_command(label="⚡ Resolve API送信",
                                 command=self.send_to_resolve)
        resolve_menu.add_command(label="📍 選択テロップを再生ヘッドに配置",
                                 command=self.place_selected_at_playhead)
        resolve_menu.add_command(label="▶ 再生ヘッドを選択テロップへ移動",
                                 command=self.jump_to_selected)
        resolve_menu.add_checkbutton(
            label="配置時にテロップTCを再生ヘッドに更新",
            variable=self._playhead_writeback,
            command=self._save_app_config)
        resolve_menu.add_separator()
        resolve_menu.add_command(label="🎤 タイムライン音声を自動字幕起こし...",
                                 command=self.open_transcribe)
        resolve_menu.add_command(label="🐍 Resolveスクリプトを生成...",
                                 command=self.export_resolve_script)
        resolve_menu.add_separator()
        resolve_menu.add_command(label="🧩 りぞりぷとを起動",
                                 command=self.launch_rizoripute)
        resolve_menu.add_command(label="📁 りぞりぷとの場所を設定...",
                                 command=self.set_rizoripute_path)

        # TeloCut 連携メニュー（動画編集ソフト。起動中なら双方向にやり取りする）
        tc_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="TeloCut", menu=tc_menu)
        tc_menu.add_command(label="🎬 TeloCut（動画編集）を起動", command=self.launch_telocut)
        tc_menu.add_separator()
        cfg = self._load_app_config()
        self._tc_sync = tk.BooleanVar(value=bool(cfg.get("telocut_sync", False)) or "--telocut-sync" in sys.argv)
        tc_menu.add_checkbutton(label="🔗 TeloCut とリアルタイム連携（テロップ・デザイン・選択）",
                                variable=self._tc_sync, command=self._toggle_tc_sync)
        self._tc_sync_seek = tk.BooleanVar(value=bool(cfg.get("telocut_sync_seek", True)))
        tc_menu.add_checkbutton(label="　テロップを選んだら TeloCut の再生ヘッドも移動",
                                variable=self._tc_sync_seek, command=self._save_app_config)
        tc_menu.add_separator()
        tc_menu.add_command(label="⬇ TeloCut のテロップを取り込み", command=self.telocut_pull)
        tc_menu.add_command(label="⬆ テロップを TeloCut に反映（置き換え）", command=self.telocut_push)
        tc_menu.add_separator()
        tc_menu.add_command(label="▶ TeloCut の再生ヘッドを選択テロップへ", command=self.telocut_seek_selected)
        tc_menu.add_command(label="📍 選択テロップを TeloCut の再生ヘッドに配置",
                            command=self.telocut_place_selected)
        self._telocut_follow = tk.BooleanVar(value=False)
        tc_menu.add_checkbutton(label="TeloCut の再生位置のテロップを自動で選択",
                                variable=self._telocut_follow, command=self._toggle_telocut_follow)

        # プリセットメニュー
        self._preset_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="プリセット", menu=self._preset_menu)
        self._preset_menu.add_command(label="現在の設定を保存...", command=self.save_preset)
        self._preset_menu.add_separator()

        # ウィンドウメニュー
        win_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="ウィンドウ", menu=win_menu)
        win_menu.add_checkbutton(
            label="🌙 ダークモード",
            variable=self._is_dark,
            command=self._toggle_theme)
        win_menu.add_checkbutton(label="タイムラインを表示", variable=self._show_timeline,
                                 command=self._toggle_timeline)
        win_menu.add_checkbutton(label="直接入力エリアを表示", variable=self._show_quick_add,
                                 command=lambda: (self._show_quick_add.set(not self._show_quick_add.get()),
                                                  self._toggle_quick_add()))
        win_menu.add_command(label="パネル幅を初期状態に戻す", command=lambda: self._init_main_sashes())
        lay_m = tk.Menu(win_menu, tearoff=0)
        win_menu.add_cascade(label="レイアウト（次の起動から）", menu=lay_m)
        for val, lab in (("tfx", "TFX-Artist 風（中央に大きなプレビュー）"), ("classic", "従来（3 列）")):
            lay_m.add_radiobutton(label=lab, value=val, variable=self._layout_mode,
                                  command=lambda: (self._save_app_config(), messagebox.showinfo(
                                      "レイアウト", "次に DavinTelop を起動したときに切り替わります。", parent=self)))
        win_menu.add_separator()
        win_menu.add_command(label="🎬 テロッププレビュー ウィンドウ",
                             command=self._toggle_telop_preview_window)
        win_menu.add_command(label="📁 テンプレート ウィンドウ",
                             command=self._toggle_template_window)
        win_menu.add_separator()
        win_menu.add_command(label="テロップリストを切り離す",
                             command=lambda: self._tearoff_panel("left"))
        win_menu.add_command(label="デザイン設定を切り離す",
                             command=lambda: self._tearoff_panel("design"))
        win_menu.add_command(label="プレビューを切り離す",
                             command=lambda: self._tearoff_panel("preview"))
        win_menu.add_separator()
        win_menu.add_command(label="すべてのパネルをドックに戻す",
                             command=self._dock_all_panels)
        win_menu.add_separator()
        win_menu.add_command(
            label="ヒント: 右パネルのタブをダブルクリックで切り離し",
            state='disabled')

        # ヘルプメニュー
        help_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="ヘルプ", menu=help_menu)
        help_menu.add_command(label="📡 NDI Tools をインストール...",
                              command=lambda: NDIInstallDialog(self))
        help_menu.add_separator()
        help_menu.add_command(label="NDI Tools 公式サイト",
                              command=lambda: __import__('webbrowser').open(
                                  "https://ndi.video/tools/"))
        help_menu.add_command(label="VoiceVox 公式サイト",
                              command=lambda: __import__('webbrowser').open(
                                  "https://voicevox.hiroshiba.jp/"))
        help_menu.add_command(label="DaVinci Resolve 公式サイト",
                              command=lambda: __import__('webbrowser').open(
                                  "https://www.blackmagicdesign.com/jp/products/davinciresolve"))

    # ─── メインUI ────────────────────────────────

    def _setup_ui(self):
        self._create_extra_design_vars()
        if self._layout_mode.get() == "tfx":
            # TFX-Artist 風（中央に大きな Program・左にプロパティとレイヤー・右に色・下にスタイル）。dt_layout.py
            self._setup_ui_tfx()
            self._timeline = TimelineView(self._root_pane, self)
            if self._show_timeline.get():
                self._root_pane.add(self._timeline, weight=0)
            return
        # ツールバー（上）とステータスバー（下）を先に pack して、ウィンドウを
        # 縮めても見切れないようにする
        self._setup_toolbar()
        self._setup_bottom_bar()

        # 縦ペイン: 上=メイン3列 / 下=タイムライン
        self._root_pane = ttk.PanedWindow(self, orient=tk.VERTICAL)
        self._root_pane.pack(fill=tk.BOTH, expand=True, padx=6, pady=(2, 2))

        # メインペイン (左: リスト / 中央: デザイン+プレビュー / 右: 合成+VoiceVox)
        self._main_pane = ttk.PanedWindow(self._root_pane, orient=tk.HORIZONTAL)
        self._root_pane.add(self._main_pane, weight=1)

        self._left_frame = ttk.Frame(self._main_pane)
        self._main_pane.add(self._left_frame, weight=3)

        right_mid = ttk.Frame(self._main_pane)
        self._main_pane.add(right_mid, weight=4)

        self._right2_frame = ttk.Frame(self._main_pane)
        self._main_pane.add(self._right2_frame, weight=2)

        self._setup_left_panel(self._left_frame)
        self._setup_right_panel(right_mid)
        self._setup_right2_panel(self._right2_frame)

        self._timeline = TimelineView(self._root_pane, self)
        if self._show_timeline.get():
            self._root_pane.add(self._timeline, weight=0)
        self.after(250, self._init_main_sashes)

    def _init_main_sashes(self):
        """3列の初期幅: 左 36% / 中央 36% / 右 28%。リスト:詳細編集 = 6:4。"""
        try:
            self.update_idletasks()
            w = self._main_pane.winfo_width()
            if w > 600:
                self._main_pane.sashpos(0, int(w * 0.36))
                self._main_pane.sashpos(1, int(w * 0.72))
            h = self._list_pane.winfo_height()
            if h > 300:
                self._list_pane.sashpos(0, max(200, h - 250))
            self._right_pane.sashpos(0, 230)
        except tk.TclError:
            pass

    def _toggle_timeline(self):
        panes = [str(p) for p in self._root_pane.panes()]
        shown = str(self._timeline) in panes
        if self._show_timeline.get() and not shown:
            self._root_pane.add(self._timeline, weight=0)
            self.after(50, self._timeline.redraw)
        elif not self._show_timeline.get() and shown:
            self._root_pane.forget(self._timeline)
        self._save_app_config()

    # ─── ツールバー ─────────────────────────────

    def _setup_toolbar(self):
        tb = ttk.Frame(self, padding=(6, 4, 6, 0))
        tb.pack(fill='x', side='top')
        self._toolbar = tb

        def btn(text, cmd, tip=None, width=None):
            b = ttk.Button(tb, text=text, command=cmd)
            if width:
                b.configure(width=width)
            b.pack(side='left', padx=1)
            if tip:
                _Tooltip(b, tip)
            return b

        def sep():
            ttk.Separator(tb, orient='vertical').pack(side='left', fill='y', padx=6, pady=2)

        def menubtn(text, builder, tip=None):
            mb = ttk.Menubutton(tb, text=text)
            m = tk.Menu(mb, tearoff=0)
            builder(m)
            mb.configure(menu=m)
            mb.pack(side='left', padx=1)
            if tip:
                _Tooltip(mb, tip)
            return mb

        btn("📄", self.new_project, "新規プロジェクト", 3)
        btn("📂", self.load_project, "プロジェクトを開く (Ctrl+O)", 3)
        btn("💾", self.save_project, "上書き保存 (Ctrl+S)", 3)
        sep()
        btn("↶", self.undo, "元に戻す (Ctrl+Z)", 3)
        btn("↷", self.redo, "やり直し (Ctrl+Y)", 3)
        sep()
        btn("＋ テロップ", self.add_entry, "新規テロップ作成 (Ctrl+N)\nTCは自動入力・Enterで作成")
        menubtn("✏ 加工", self._build_process_menu,
                "選択テロップのテキスト・タイミング・スタイルを一括加工\n（未選択時は全テロップ）")
        btn("🔍", self.open_find_replace, "検索と置換 (Ctrl+Shift+F)", 3)
        sep()
        menubtn("📥 読込", self._build_import_menu, "CSV / SRT / クリップボードから読み込み")
        menubtn("📤 書出", self._build_export_menu, "PNG / SRT / FCPxml / AAF / CSV に書き出し")
        sep()
        btn("⚡ Resolve送信", self.send_to_resolve, "全テロップを Resolve のタイムラインに配置 (Ctrl+Enter)")
        btn("📍 再生ヘッド配置", self.place_selected_at_playhead,
            "選択テロップを Resolve の再生ヘッド位置に配置 (Ctrl+Shift+Enter)")
        btn("▶", self.jump_to_selected, "Resolve の再生ヘッドを選択テロップへ移動 (Ctrl+J)", 3)
        sep()
        btn("🔊", self.export_voicevox, "VoiceVox 音声生成", 3)
        btn("🎭", self.export_lipsync, "口パク生成", 3)

        self._theme_btn = ttk.Button(
            tb, text="🌙" if not self._is_dark.get() else "☀️",
            width=3, command=self._toggle_theme_btn)
        self._theme_btn.pack(side='right', padx=1)
        _Tooltip(self._theme_btn, "ダーク / ライト切替")
        b = ttk.Button(tb, text="⌨", width=3, command=self._open_shortcut_settings)
        b.pack(side='right', padx=1)
        _Tooltip(b, "キーボードショートカット設定")
        b = ttk.Button(tb, text="⚙", width=3, command=self.open_new_telop_settings)
        b.pack(side='right', padx=1)
        _Tooltip(b, "テロップ作成設定（既定の尺・TCの自動入力）")
        b = ttk.Button(tb, text="📁", width=3, command=self._toggle_template_window)
        b.pack(side='right', padx=1)
        _Tooltip(b, "テンプレート ウィンドウ")
        b = ttk.Button(tb, text="🎬", width=3, command=self._toggle_telop_preview_window)
        b.pack(side='right', padx=1)
        _Tooltip(b, "テロッププレビュー ウィンドウ")

    def _build_import_menu(self, m):
        m.add_command(label="CSV を読み込み...", command=self.import_csv)
        m.add_command(label="SRT を読み込み...", command=self.import_srt_file)
        m.add_separator()
        m.add_command(label="クリップボードから貼り付け（TSV / 台本）", command=self.paste_entries)
        m.add_separator()
        enc = tk.Menu(m, tearoff=0)
        for v in ("auto", "utf-8", "utf-8-sig", "cp932", "shift_jis", "euc_jp"):
            enc.add_radiobutton(label="自動判別" if v == "auto" else v, value=v,
                                variable=self._encoding_var)
        m.add_cascade(label="CSV 文字コード", menu=enc)
        dl = tk.Menu(m, tearoff=0)
        for v, lab in (("auto", "自動判別"), (",", "カンマ ,"), ("\t", "タブ"),
                       (";", "セミコロン ;"), ("|", "パイプ |")):
            dl.add_radiobutton(label=lab, value=v, variable=self._delim_var)
        m.add_cascade(label="CSV 区切り文字", menu=dl)

    def _build_export_menu(self, m):
        m.add_command(label="PNG を書き出し...", command=self.export_png)
        m.add_command(label="SRT を書き出し...", command=self.export_srt)
        m.add_command(label="FCPxml を書き出し...", command=self.export_fcpxml)
        m.add_command(label="AAF を書き出し (Avid)...", command=self.export_aaf)
        m.add_command(label="CSV を書き出し...", command=self.export_csv_file)
        m.add_separator()
        m.add_checkbutton(label="PNG ファイル名にテキストを含める",
                          variable=self._include_text_in_filename,
                          command=self._save_app_config)
        m.add_separator()
        m.add_command(label="🐍 Resolveスクリプトを生成...", command=self.export_resolve_script)
        m.add_command(label="🎵 全テロップを WAV 書き出し...", command=self.export_wav_all)
        m.add_command(label="🎵 選択テロップを WAV 書き出し...", command=self.export_wav_selected)

    def _build_process_menu(self, m):
        """加工メニュー（メニューバー・ツールバー・右クリックで共用）。"""
        from core import textfmt as tf
        T = self._apply_text_op

        txt = tk.Menu(m, tearoff=0)
        txt.add_command(label="2行に均等改行", command=lambda: T(lambda t: tf.balanced_break(t, 2), "2行に均等改行"))
        txt.add_command(label="3行に均等改行", command=lambda: T(lambda t: tf.balanced_break(t, 3), "3行に均等改行"))
        txt.add_command(label="N文字で自動改行...", command=self.wrap_by_chars)
        txt.add_command(label="改行を削除して1行に", command=lambda: T(tf.join_lines, "1行に"))
        txt.add_separator()
        txt.add_command(label="空白・空行を整理", command=lambda: T(tf.normalize_spaces, "空白整理"))
        txt.add_command(label="句読点を削除", command=lambda: T(tf.remove_punct, "句読点削除"))
        txt.add_command(label="句読点を全角スペースに", command=lambda: T(lambda t: tf.remove_punct(t, "　"), "句読点→空白"))
        txt.add_command(label="文末の「。」を削除", command=lambda: T(tf.remove_trailing_period, "文末の。削除"))
        txt.add_command(label="「...」「・・・」を「……」に統一", command=lambda: T(tf.normalize_ellipsis, "三点リーダー統一"))
        txt.add_command(label="フィラー（えー・あの 等）を除去", command=lambda: T(tf.remove_fillers, "フィラー除去"))
        txt.add_separator()
        txt.add_command(label="英数字・記号を半角に", command=lambda: T(tf.to_halfwidth_alnum, "半角化"))
        txt.add_command(label="英数字・記号を全角に", command=lambda: T(tf.to_fullwidth_alnum, "全角化"))
        txt.add_command(label="半角カナを全角に", command=lambda: T(tf.halfkana_to_fullkana, "全角カナ化"))
        txt.add_separator()
        br = tk.Menu(txt, tearoff=0)
        for l, r in (("「", "」"), ("『", "』"), ("（", "）"), ("【", "】"), ("“", "”"), ("＜", "＞")):
            br.add_command(label=f"{l} … {r}",
                           command=lambda l=l, r=r: T(lambda t: tf.add_brackets(t, l, r), f"{l}{r}で囲む"))
        br.add_separator()
        br.add_command(label="外側の括弧を外す", command=lambda: T(tf.strip_brackets, "括弧を外す"))
        txt.add_cascade(label="括弧で囲む", menu=br)
        txt.add_command(label="先頭・末尾に文字を追加...", command=self.add_prefix_suffix)
        txt.add_command(label="話者名を分離して話者欄へ（名前: 本文）", command=self.extract_speakers)
        m.add_cascade(label="テキスト", menu=txt)

        tim = tk.Menu(m, tearoff=0)
        tim.add_command(label="尺を一括設定...", command=self.set_duration_bulk)
        tim.add_command(label="最小の尺を確保（短すぎる尺を延長）...", command=self.ensure_min_duration)
        tim.add_command(label="尺を文字数から再計算", command=self.recalc_duration_by_chars)
        tim.add_separator()
        tim.add_command(label="隙間なく連続配置...", command=self.arrange_sequential)
        tim.add_command(label="隙間を埋める（終了を次の開始まで延長）", command=self.fill_gaps)
        tim.add_command(label="TC を一括シフト...", command=self.shift_tc_dialog)
        tim.add_separator()
        tim.add_command(label="開始を再生ヘッドに", accelerator="I", command=lambda: self.set_in_out('in'))
        tim.add_command(label="終了を再生ヘッドに", accelerator="O", command=lambda: self.set_in_out('out'))
        tim.add_command(label="1フレーム前へ", accelerator="[", command=lambda: self.nudge_selected(-1))
        tim.add_command(label="1フレーム後ろへ", accelerator="]", command=lambda: self.nudge_selected(1))
        tim.add_command(label="1秒前へ", accelerator="Shift+[", command=lambda: self.nudge_selected(None, -1))
        tim.add_command(label="1秒後ろへ", accelerator="Shift+]", command=lambda: self.nudge_selected(None, 1))
        m.add_cascade(label="タイミング", menu=tim)

        st = tk.Menu(m, tearoff=0)
        st.add_command(label="個別スタイルをコピー", command=self.copy_style)
        st.add_command(label="個別スタイルを貼り付け", command=self.paste_style)
        st.add_command(label="個別スタイルを解除（共通デザインに戻す）", command=self.clear_style)
        st.add_separator()
        st.add_command(label="話者を一括設定...", command=self.set_speaker_bulk)
        st.add_command(label="🎨 話者の色分け・トラック振り分け...", command=self.open_speaker_rules)
        m.add_cascade(label="スタイル・話者", menu=st)

        m.add_separator()
        m.add_command(label="🔗 選択テロップを結合", command=self.merge_selected)
        m.add_command(label="✂ 改行でテロップを分割", command=self.split_selected)
        m.add_command(label="⇅ 開始TC順に並べ替え", command=self.sort_by_tc)
        m.add_command(label="⚠ タイムコードをチェック", command=self.check_timecodes)
        m.add_separator()
        m.add_command(label="🧹 字幕を自動整形（改行・フィラー除去）...", command=self.open_autoformat)

    # ─── 左パネル ────────────────────────────────

    def _setup_left_panel(self, parent):
        hdr_left = ttk.Frame(parent)
        hdr_left.pack(fill='x', pady=(2, 0))
        ttk.Label(hdr_left, text="テロップ", font=('', 10, 'bold')).pack(side='left', padx=6)
        ttk.Button(hdr_left, text="↗ 分離", width=7,
                   command=lambda: self._toggle_float_panel(
                       self._left_frame, self._main_pane, 3, "テロップリスト")
                   ).pack(side='right', padx=4)
        self._qa_toggle_btn = ttk.Button(hdr_left, text="直接入力 ▾", width=10,
                                         command=self._toggle_quick_add)
        self._qa_toggle_btn.pack(side='right', padx=2)

        # ── 直接入力エリア（折りたたみ可） ──────────────
        qa_frame = ttk.LabelFrame(parent, text="直接入力", padding=6)
        self._qa_frame = qa_frame
        self._qa_anchor = ttk.Frame(parent)   # 再表示時の位置の目印
        self._qa_anchor.pack(fill='x')
        if self._show_quick_add.get():
            qa_frame.pack(fill='x', padx=4, pady=2, after=self._qa_anchor)
        else:
            self._qa_toggle_btn.configure(text="直接入力 ▸")

        tmpl_row = ttk.Frame(qa_frame)
        tmpl_row.pack(fill='x', pady=(0, 4))
        ttk.Label(tmpl_row, text="TC:").pack(side='left')
        for tc_label, tc_val in [("00:00", "00:00:00:00"), ("01:00", "01:00:00:00"),
                                  ("10:00", "10:00:00:00")]:
            ttk.Button(tmpl_row, text=tc_label, width=6,
                       command=lambda t=tc_val: self._qa_start_var.set(t)
                       ).pack(side='left', padx=1)
        ttk.Separator(tmpl_row, orient='vertical').pack(side='left', fill='y', padx=6)
        ttk.Label(tmpl_row, text="尺:").pack(side='left')
        for dur in [2, 3, 5, 10]:
            ttk.Button(tmpl_row, text=f"{dur}秒", width=4,
                       command=lambda d=dur: self._qa_dur_var.set(f"{d:.3f}")
                       ).pack(side='left', padx=1)

        tc_row = ttk.Frame(qa_frame)
        tc_row.pack(fill='x', pady=(0, 4))
        ttk.Label(tc_row, text="開始:").pack(side='left')
        self._qa_start_var = tk.StringVar(value="00:00:00:00")
        ttk.Entry(tc_row, textvariable=self._qa_start_var, width=12).pack(side='left', padx=(2, 6))
        ttk.Label(tc_row, text="終了:").pack(side='left')
        self._qa_end_var = tk.StringVar(value="00:00:05:00")
        ttk.Entry(tc_row, textvariable=self._qa_end_var, width=12).pack(side='left', padx=(2, 6))
        ttk.Label(tc_row, text="尺:").pack(side='left')
        self._qa_dur_var = tk.StringVar(value="5.000")
        ttk.Entry(tc_row, textvariable=self._qa_dur_var, width=7).pack(side='left', padx=(2, 2))
        ttk.Label(tc_row, text="秒").pack(side='left')

        self._qa_updating_tc = False
        self._qa_start_var.trace_add('write', self._qa_on_start_change)
        self._qa_end_var.trace_add('write', self._qa_on_end_change)
        self._qa_dur_var.trace_add('write', self._qa_on_dur_change)

        txt_row = ttk.Frame(qa_frame)
        txt_row.pack(fill='x')
        ttk.Label(txt_row, text="テキスト:").pack(side='left')
        self._qa_text_var = tk.StringVar()
        self._qa_text_entry = ttk.Entry(txt_row, textvariable=self._qa_text_var)
        self._qa_text_entry.pack(side='left', padx=(2, 6), fill='x', expand=True)
        self._qa_text_entry.bind('<Return>', lambda e: self._quick_add())
        ttk.Button(txt_row, text="追加", width=6, command=self._quick_add).pack(side='left')

        # ── リスト + 詳細編集（上下に分割） ──────────────
        list_pane = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        list_pane.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)
        self._list_pane = list_pane

        list_frame = ttk.Frame(list_pane, padding=(0, 2))
        list_pane.add(list_frame, weight=3)

        tb = ttk.Frame(list_frame)
        tb.pack(fill='x', pady=(0, 4))
        for label, cmd, tip in [
                ("＋", self.add_entry, "新規テロップ (Ctrl+N)"),
                ("⤓", self.insert_after_selected, "選択の直後に挿入 (Insert)"),
                ("✎", self.edit_selected, "ダイアログで編集 (Enter / F2)"),
                ("⧉", self.duplicate_selected, "複製 (Ctrl+D)"),
                ("✕", self.delete_selected, "削除 (Delete)"),
                ("↑", self.move_up, "上へ"), ("↓", self.move_down, "下へ"),
                ("⇅", self.sort_by_tc, "開始TC順に並べ替え"),
                ("🔗", self.merge_selected, "結合"),
                ("✂", self.split_selected, "改行で分割")]:
            b = ttk.Button(tb, text=label, width=3, command=cmd)
            b.pack(side='left', padx=1)
            _Tooltip(b, tip)

        self._filter_var = tk.StringVar()
        fe = ttk.Entry(tb, textvariable=self._filter_var, width=14)
        fe.pack(side='right', fill='x', expand=True, padx=(8, 0))
        _Tooltip(fe, "🔎 絞り込み (Ctrl+F)  テキスト・話者・メモ・TC で検索")
        ttk.Label(tb, text="🔎").pack(side='right')
        self._filter_entry = fe
        fe.bind('<Escape>', lambda e: self._filter_var.set(""))
        fe.bind('<Down>', lambda e: (self._tree.focus_set(), self._select_first_visible()))
        self._filter_var.trace_add('write', lambda *_: self._populate_tree())

        # ステータス（ツリーより先に bottom へ pack しないと横に回り込む）
        self._status_var = tk.StringVar(value="テロップ: 0 件")
        ttk.Label(list_frame, textvariable=self._status_var, anchor='w',
                  foreground='gray').pack(side='bottom', fill='x', pady=(4, 0))

        cols = ("id", "start", "end", "dur", "text", "spk", "chars", "enabled")
        self._tree = ttk.Treeview(list_frame, columns=cols, show='headings', selectmode='extended')
        for c, label, w, anchor, stretch in [
                ("id", "No.", 44, 'center', False), ("start", "開始", 92, 'center', False),
                ("end", "終了", 92, 'center', False), ("dur", "尺", 46, 'e', False),
                ("text", "テキスト", 160, 'w', True), ("spk", "話者", 60, 'w', False),
                ("chars", "字数", 38, 'e', False), ("enabled", "有効", 38, 'center', False)]:
            self._tree.heading(c, text=label)
            self._tree.column(c, width=w, anchor=anchor, stretch=stretch)
        self._tree.tag_configure('disabled', foreground='gray')
        self._tree.tag_configure('error',    foreground='#E5484D')
        self._tree.tag_configure('overlap',  foreground='#E8890C')

        scroll_y = ttk.Scrollbar(list_frame, orient='vertical', command=self._tree.yview)
        self._tree.configure(yscrollcommand=scroll_y.set)
        scroll_y.pack(side='right', fill='y')
        self._tree.pack(side='left', fill=tk.BOTH, expand=True)

        t = self._tree
        t.bind('<Double-1>', self._on_tree_double)
        t.bind('<Return>',   lambda e: self.edit_selected())
        t.bind('<Delete>',   lambda e: self.delete_selected())
        t.bind('<F2>',       lambda e: self._insp_focus_text())
        t.bind('<Insert>',   lambda e: self.insert_after_selected())
        t.bind('<space>',    lambda e: (self.toggle_enabled(), 'break')[1])
        t.bind('<Control-d>', lambda e: (self.duplicate_selected(), 'break')[1])
        t.bind('<Control-c>', lambda e: (self.copy_selected(), 'break')[1])
        t.bind('<Control-v>', lambda e: (self.paste_entries(), 'break')[1])
        t.bind('<Control-a>', lambda e: (self.select_all(), 'break')[1])
        t.bind('<Key-i>', lambda e: self.set_in_out('in'))
        t.bind('<Key-o>', lambda e: self.set_in_out('out'))
        t.bind('<bracketleft>',  lambda e: (self.nudge_selected(-1), 'break')[1])
        t.bind('<bracketright>', lambda e: (self.nudge_selected(1), 'break')[1])
        t.bind('<braceleft>',    lambda e: (self.nudge_selected(None, -1), 'break')[1])
        t.bind('<braceright>',   lambda e: (self.nudge_selected(None, 1), 'break')[1])
        t.bind('<Alt-Up>',   lambda e: (self.move_up(), 'break')[1])
        t.bind('<Alt-Down>', lambda e: (self.move_down(), 'break')[1])
        t.bind('<<TreeviewSelect>>', self._on_select)
        # ドラッグで並べ替え
        t.bind('<ButtonPress-1>', self._tree_drag_start, add='+')
        t.bind('<B1-Motion>', self._tree_drag_motion, add='+')
        t.bind('<ButtonRelease-1>', self._tree_drag_end, add='+')
        self._tree_drag = None

        # 右クリックメニュー
        self._ctx_menu = tk.Menu(self._tree, tearoff=0)
        cm = self._ctx_menu
        cm.add_command(label="ダイアログで編集", accelerator="Enter", command=self.edit_selected)
        cm.add_command(label="直後に新規挿入...", accelerator="Insert", command=self.insert_after_selected)
        cm.add_command(label="複製", accelerator="Ctrl+D", command=self.duplicate_selected)
        cm.add_separator()
        cm.add_command(label="コピー", accelerator="Ctrl+C", command=self.copy_selected)
        cm.add_command(label="貼り付け", accelerator="Ctrl+V", command=self.paste_entries)
        cm.add_separator()
        cm.add_command(label="有効/無効", accelerator="Space", command=self.toggle_enabled)
        proc = tk.Menu(cm, tearoff=0)
        self._build_process_menu(proc)
        cm.add_cascade(label="✏ 加工", menu=proc)
        cm.add_separator()
        cm.add_command(label="▶ Resolve再生ヘッドをここへ移動", command=self.jump_to_selected)
        cm.add_command(label="📍 再生ヘッドに配置 (Resolve)", command=self.place_selected_at_playhead)
        cm.add_separator()
        cm.add_command(label="削除", accelerator="Delete", command=self.delete_selected)
        t.bind('<Button-3>', self._show_context_menu)
        t.bind('<Button-2>', self._show_context_menu)
        if sys.platform == 'darwin':
            t.bind('<Control-Button-1>', self._show_context_menu)

        # 詳細編集パネル
        insp = ttk.LabelFrame(list_pane, text="詳細編集", padding=6)
        list_pane.add(insp, weight=1)
        self._build_inspector(insp)

    def _toggle_quick_add(self):
        show = not self._show_quick_add.get()
        self._show_quick_add.set(show)
        if show:
            self._qa_frame.pack(fill='x', padx=4, pady=2, after=self._qa_anchor)
            self._qa_toggle_btn.configure(text="直接入力 ▾")
            self._qa_text_entry.focus_set()
        else:
            self._qa_frame.pack_forget()
            self._qa_toggle_btn.configure(text="直接入力 ▸")
        self._save_app_config()

    def _select_first_visible(self):
        ch = self._tree.get_children()
        if ch and not self._tree.selection():
            self._tree.selection_set(ch[0])
            self._tree.focus(ch[0])

    def _on_tree_double(self, event):
        # 見出しのダブルクリックは無視
        if self._tree.identify_region(event.x, event.y) != 'cell':
            return
        col = self._tree.identify_column(event.x)
        if col == '#5':                    # テキスト列 → 詳細編集で直接編集
            self._insp_focus_text()
            return 'break'
        self.edit_selected()
        return 'break'

    # ─── ツリーのドラッグ並べ替え ─────────────────

    def _tree_drag_start(self, event):
        row = self._tree.identify_row(event.y)
        if not row or event.state & 0x5 or self._tree.identify_region(event.x, event.y) != 'cell':
            self._tree_drag = None
            return
        self._tree_drag = {"y": event.y, "row": row, "active": False}

    def _tree_drag_motion(self, event):
        d = self._tree_drag
        if not d or self._filter_var.get().strip():
            return
        if not d["active"] and abs(event.y - d["y"]) < 8:
            return
        d["active"] = True
        self._tree.configure(cursor='sb_v_double_arrow')
        return 'break'

    def _tree_drag_end(self, event):
        d = self._tree_drag
        self._tree_drag = None
        self._tree.configure(cursor='')
        if not d or not d["active"]:
            return
        target = self._tree.identify_row(event.y)
        sel = self._get_selected_entries()
        if not sel:
            return
        rest = [e for e in self.telop_list if e not in sel]
        if target:
            tid = int(target)
            tgt = next((e for e in self.telop_list if e.id == tid), None)
            if tgt in sel:
                return
            idx = rest.index(tgt)
            # 下方向へのドラッグは対象の後ろへ
            if self.telop_list.index(tgt) > self.telop_list.index(sel[0]):
                idx += 1
        else:
            idx = len(rest) if event.y > 10 else 0
        self.telop_list = rest[:idx] + sel + rest[idx:]
        self._refresh_tree()
        self._progress_var.set(f"{len(sel)} 件を移動しました")

    # ─── 詳細編集パネル（インスペクタ） ─────────────

    _STYLE_POS = [("", "既定"), ("top", "上"), ("middle", "中央"), ("bottom", "下")]

    def _build_inspector(self, f):
        self._insp_ids: tuple = ()
        self._insp_loading = False
        self._insp_text_job = None
        f.columnconfigure(1, weight=1)

        self._insp_title = tk.StringVar(value="テロップを選択してください")
        head = ttk.Frame(f)
        head.grid(row=0, column=0, columnspan=4, sticky='ew')
        ttk.Label(head, textvariable=self._insp_title, foreground='gray').pack(side='left')

        # テキスト + クイック加工
        tf_ = ttk.Frame(f)
        tf_.grid(row=1, column=0, columnspan=4, sticky='nsew', pady=(2, 4))
        tf_.columnconfigure(0, weight=1)
        tf_.rowconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        self._insp_text = tk.Text(tf_, height=2, wrap='word', undo=True,
                                  bg=_tc("text_bg"), fg=_tc("text_fg"),
                                  insertbackground=_tc("text_fg"), relief='flat',
                                  font=('', 11), padx=6, pady=4)
        self._insp_text.grid(row=0, column=0, sticky='nsew')
        self._insp_text.bind('<KeyRelease>', self._insp_on_text_key)
        self._insp_text.bind('<FocusOut>', lambda e: self._insp_commit_text())
        self._insp_text.bind('<Escape>', lambda e: (self._tree.focus_set(), 'break')[1])
        self._insp_text.bind('<Control-Return>', lambda e: (self._insp_commit_text(),
                                                            self._tree.focus_set(), 'break')[2])
        qb = head
        from core import textfmt as tfm
        for label, fn, tip in [
                ("整", tfm.normalize_spaces, "空白・空行を整理"),
                ("「」", lambda t: tfm.add_brackets(t, "「", "」"), "「」で囲む"),
                ("1行", tfm.join_lines, "改行を削除して1行に"),
                ("2行", lambda t: tfm.balanced_break(t, 2), "2行に均等改行")]:
            b = ttk.Button(qb, text=label, width=4, command=lambda fn=fn, tip=tip: self._apply_text_op(fn, tip))
            b.pack(side='right', padx=1)
            _Tooltip(b, tip)
        self._insp_count = tk.StringVar(value="")
        ttk.Label(f, textvariable=self._insp_count, foreground='gray',
                  font=('', 8)).grid(row=2, column=0, columnspan=4, sticky='e')

        # タイミング
        r = ttk.Frame(f)
        r.grid(row=3, column=0, columnspan=4, sticky='w', pady=2)
        ttk.Label(r, text="開始").pack(side='left')
        self._insp_start = tk.StringVar()
        self._insp_start_e = ttk.Entry(r, textvariable=self._insp_start, width=12)
        self._insp_start_e.pack(side='left', padx=(2, 0))
        b = ttk.Button(r, text="I", width=2, command=lambda: self.set_in_out('in'))
        b.pack(side='left', padx=(1, 6))
        _Tooltip(b, "開始を Resolve の再生ヘッドに (I)")
        ttk.Label(r, text="終了").pack(side='left')
        self._insp_end = tk.StringVar()
        self._insp_end_e = ttk.Entry(r, textvariable=self._insp_end, width=12)
        self._insp_end_e.pack(side='left', padx=(2, 0))
        b = ttk.Button(r, text="O", width=2, command=lambda: self.set_in_out('out'))
        b.pack(side='left', padx=(1, 6))
        _Tooltip(b, "終了を Resolve の再生ヘッドに (O)")
        ttk.Label(r, text="尺").pack(side='left')
        self._insp_dur = tk.StringVar()
        self._insp_dur_e = ttk.Entry(r, textvariable=self._insp_dur, width=6)
        self._insp_dur_e.pack(side='left', padx=(2, 0))
        ttk.Label(r, text="秒").pack(side='left', padx=(1, 6))
        for txt, fr, sec, tip in (("◀", -1, 0, "1フレーム前へ ["), ("▶", 1, 0, "1フレーム後ろへ ]")):
            b = ttk.Button(r, text=txt, width=2, command=lambda fr=fr: self.nudge_selected(fr))
            b.pack(side='left')
            _Tooltip(b, tip)
        for ent in (self._insp_start_e, self._insp_end_e):
            ent.bind('<Return>', lambda e: self._insp_commit_tc())
            ent.bind('<FocusOut>', lambda e: self._insp_commit_tc())
            ent.bind('<Up>',   lambda e, w=ent: self._insp_nudge_field(w, 1))
            ent.bind('<Down>', lambda e, w=ent: self._insp_nudge_field(w, -1))
        self._insp_dur_e.bind('<Return>', lambda e: self._insp_commit_dur())
        self._insp_dur_e.bind('<FocusOut>', lambda e: self._insp_commit_dur())

        # 話者・メモ・有効
        r2 = ttk.Frame(f)
        r2.grid(row=4, column=0, columnspan=4, sticky='ew', pady=2)
        ttk.Label(r2, text="話者").pack(side='left')
        self._insp_spk = tk.StringVar()
        self._insp_spk_cb = ttk.Combobox(r2, textvariable=self._insp_spk, width=10)
        self._insp_spk_cb.pack(side='left', padx=(2, 8))
        self._insp_spk_cb.bind('<<ComboboxSelected>>', lambda e: self._insp_commit_field('speaker'))
        self._insp_spk_cb.bind('<Return>', lambda e: self._insp_commit_field('speaker'))
        self._insp_spk_cb.bind('<FocusOut>', lambda e: self._insp_commit_field('speaker'))
        ttk.Label(r2, text="メモ").pack(side='left')
        self._insp_note = tk.StringVar()
        ne = ttk.Entry(r2, textvariable=self._insp_note, width=14)
        ne.pack(side='left', padx=(2, 8), fill='x', expand=True)
        ne.bind('<Return>', lambda e: self._insp_commit_field('note'))
        ne.bind('<FocusOut>', lambda e: self._insp_commit_field('note'))
        self._insp_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="有効", variable=self._insp_enabled,
                        command=lambda: self._insp_commit_field('enabled')).pack(side='left')

        # 個別スタイル
        sf = ttk.LabelFrame(f, text="個別スタイル（このテロップだけ変更）", padding=(6, 2))
        sf.grid(row=5, column=0, columnspan=4, sticky='ew', pady=(4, 0))
        self._insp_style_frame = sf
        s1 = ttk.Frame(sf)
        s1.pack(fill='x')
        ttk.Label(s1, text="文字色").pack(side='left')
        self._insp_fc = ColorButton(s1, color="#FFFFFF",
                                    command=lambda c: self._insp_set_style('font_color', c))
        self._insp_fc.pack(side='left', padx=(2, 6))
        ttk.Label(s1, text="縁色").pack(side='left')
        self._insp_sc = ColorButton(s1, color="#000000",
                                    command=lambda c: self._insp_set_style('stroke_color', c))
        self._insp_sc.pack(side='left', padx=(2, 6))
        ttk.Label(s1, text="サイズ").pack(side='left')
        self._insp_size = tk.StringVar()
        sp = ttk.Spinbox(s1, from_=8, to=300, textvariable=self._insp_size, width=5,
                         command=lambda: self._insp_set_style('font_size', self._insp_size.get()))
        sp.pack(side='left', padx=(2, 6))
        sp.bind('<Return>', lambda e: self._insp_set_style('font_size', self._insp_size.get()))
        ttk.Label(s1, text="位置").pack(side='left')
        self._insp_pos = tk.StringVar(value="既定")
        pc = ttk.Combobox(s1, textvariable=self._insp_pos, width=5, state='readonly',
                          values=[lab for _, lab in self._STYLE_POS])
        pc.pack(side='left', padx=(2, 6))
        pc.bind('<<ComboboxSelected>>', lambda e: self._insp_set_style(
            'position_v', dict((lab, v) for v, lab in self._STYLE_POS)[self._insp_pos.get()]))
        self._insp_style_info = tk.StringVar(value="")
        self._insp_style_info.trace_add('write', lambda *_: sf.configure(
            text="個別スタイル — " + (self._insp_style_info.get() or "このテロップだけ変更")))
        for txt, cmd, tip in (("✕", self.clear_style, "個別スタイルを解除（共通デザインに戻す）"),
                              ("📋", self.paste_style, "コピーしたスタイルを貼り付け"),
                              ("⧉", self.copy_style, "このテロップのスタイルをコピー")):
            b = ttk.Button(s1, text=txt, width=3, command=cmd)
            b.pack(side='right', padx=1)
            _Tooltip(b, tip)
        self._insp_set_enabled(False)

    def _insp_set_enabled(self, on: bool, multi: bool = False):
        state = 'normal' if on else 'disabled'
        try:
            self._insp_text.configure(state='normal' if (on and not multi) else 'disabled')
            for w in (self._insp_start_e, self._insp_end_e):
                w.configure(state='normal' if (on and not multi) else 'disabled')
            self._insp_dur_e.configure(state=state)
            self._set_children_state(self._insp_style_frame, state)
        except tk.TclError:
            pass

    def _insp_load(self, force: bool = False):
        """選択テロップを詳細編集パネルに表示（フォーカス中の欄は上書きしない）。"""
        sel = self._get_selected_entries()
        ids = tuple(e.id for e in sel)
        focus = self.focus_get()
        same = ids == self._insp_ids and not force
        self._insp_ids = ids
        self._insp_loading = True
        try:
            if not sel:
                self._insp_title.set("テロップを選択してください")
                self._insp_text.configure(state='normal')
                self._insp_text.delete('1.0', 'end')
                for v in (self._insp_start, self._insp_end, self._insp_dur,
                          self._insp_spk, self._insp_note, self._insp_size):
                    v.set("")
                self._insp_count.set("")
                self._insp_style_info.set("")
                self._insp_set_enabled(False)
                return
            e = sel[0]
            multi = len(sel) > 1
            self._insp_set_enabled(True, multi)
            self._insp_title.set(
                f"{len(sel)} 件を選択中 — 話者・尺・有効・スタイルをまとめて変更できます"
                if multi else f"No.{e.id}   ({self.telop_list.index(e) + 1} / {len(self.telop_list)})")
            if not (same and focus is self._insp_text):
                self._insp_text.configure(state='normal')
                self._insp_text.delete('1.0', 'end')
                self._insp_text.insert('1.0', "（複数選択中）" if multi else e.text)
                self._insp_text.edit_reset()
                if multi:
                    self._insp_text.configure(state='disabled')
            fps = self._current_fps()
            if focus is not self._insp_start_e:
                self._insp_start.set("" if multi else e.start_time)
            if focus is not self._insp_end_e:
                self._insp_end.set("" if multi else e.end_time)
            if focus is not self._insp_dur_e:
                try:
                    durs = {round(parse_timecode(x.end_time, fps) - parse_timecode(x.start_time, fps), 3)
                            for x in sel}
                    self._insp_dur.set(f"{durs.pop():.3f}" if len(durs) == 1 else "")
                except ValueError:
                    self._insp_dur.set("")
            if focus is not self._insp_spk_cb:
                spks = {x.speaker for x in sel}
                self._insp_spk.set(spks.pop() if len(spks) == 1 else "")
            self._insp_spk_cb.configure(values=sorted({x.speaker for x in self.telop_list if x.speaker}))
            self._insp_note.set("" if multi else e.note)
            self._insp_enabled.set(all(x.enabled for x in sel))
            self._insp_update_count()
            # スタイル
            d = self._collect_design()
            st = e.style or {}
            self._insp_fc.set(st.get('font_color', d.font_color))
            self._insp_sc.set(st.get('stroke_color', d.stroke_color))
            self._insp_size.set(str(st.get('font_size', d.font_size)))
            self._insp_pos.set(dict(self._STYLE_POS).get(st.get('position_v', ''), "既定"))
            names = {'font_color': '文字色', 'stroke_color': '縁色', 'font_size': 'サイズ',
                     'position_v': '位置', 'offset_y': 'Y位置'}
            self._insp_style_info.set(
                "変更中: " + "・".join(names.get(k, k) for k in st) if st else "共通デザインを使用中")
        finally:
            self._insp_loading = False

    def _insp_entries(self) -> list:
        ids = set(self._insp_ids)
        return [e for e in self.telop_list if e.id in ids]

    def _insp_focus_text(self):
        if len(self._get_selected_entries()) == 1:
            self._insp_text.focus_set()
            self._insp_text.mark_set('insert', 'end-1c')

    def _insp_update_count(self):
        t = self._insp_text.get('1.0', 'end-1c')
        if len(self._insp_ids) != 1:
            self._insp_count.set("")
            return
        lines = t.split('\n')
        self._insp_count.set(f"{len(t.replace(chr(10), ''))} 文字 / {len(lines)} 行"
                             f"（最長 {max((len(l) for l in lines), default=0)}）   "
                             "Ctrl+Enter: 確定 / Esc: リストへ")

    def _insp_on_text_key(self, event=None):
        if self._insp_loading:
            return
        self._insp_update_count()
        if self._insp_text_job:
            self.after_cancel(self._insp_text_job)
        self._insp_text_job = self.after(600, self._insp_commit_text)

    def _insp_commit_text(self):
        self._insp_text_job = None
        ents = self._insp_entries()
        if len(ents) != 1 or str(self._insp_text.cget('state')) == 'disabled':
            return
        new = self._insp_text.get('1.0', 'end-1c').strip()
        if new and new != ents[0].text:
            ents[0].text = new
            self._refresh_tree()
            self._preview_text_var.set(new)
            self.show_telop_preview(ents[0])

    def _insp_commit_tc(self):
        ents = self._insp_entries()
        if len(ents) != 1 or self._insp_loading:
            return
        e = ents[0]
        fps = self._current_fps()
        try:
            st = normalize_timecode(self._insp_start.get(), fps)
            en = normalize_timecode(self._insp_end.get(), fps)
        except ValueError:
            self._progress_var.set("⚠ タイムコードが不正です")
            return
        if parse_timecode(en, fps) <= parse_timecode(st, fps):
            self._progress_var.set("⚠ 終了TCは開始TCより後にしてください")
            return
        self._insp_start.set(st)
        self._insp_end.set(en)
        if (st, en) != (e.start_time, e.end_time):
            e.start_time, e.end_time = st, en
            self._refresh_tree()
            self._insp_load(force=True)

    def _insp_nudge_field(self, widget, frames: int):
        var = self._insp_start if widget is self._insp_start_e else self._insp_end
        try:
            var.set(shift_timecode(var.get() or "00:00:00:00", frames, self._current_fps()))
        except ValueError:
            pass
        return 'break'

    def _insp_commit_dur(self):
        if self._insp_loading or not self._insp_dur.get().strip():
            return
        try:
            dur = float(self._insp_dur.get())
        except ValueError:
            return
        if dur <= 0:
            return
        fps = self._current_fps()
        changed = False
        for e in self._insp_entries():
            try:
                st = parse_timecode(e.start_time, fps)
            except ValueError:
                continue
            new_end = seconds_to_timecode(st + dur, fps)
            if new_end != e.end_time:
                e.end_time = new_end
                changed = True
        if changed:
            self._refresh_tree()
            self._insp_load(force=True)

    def _insp_commit_field(self, field: str):
        if self._insp_loading:
            return
        ents = self._insp_entries()
        if not ents:
            return
        if field == 'speaker':
            val = self._insp_spk.get().strip()
            if len(ents) > 1 and not val:
                return
        elif field == 'note':
            if len(ents) != 1:
                return
            val = self._insp_note.get().strip()
        else:
            val = bool(self._insp_enabled.get())
        changed = False
        for e in ents:
            if getattr(e, field) != val:
                setattr(e, field, val)
                changed = True
        if changed:
            self._refresh_tree()

    def _insp_set_style(self, key: str, value):
        if self._insp_loading:
            return
        ents = self._insp_entries()
        if not ents:
            return
        d = self._collect_design()
        if key == 'font_size':
            try:
                value = int(float(value))
            except (TypeError, ValueError):
                return
        for e in ents:
            st = dict(e.style or {})
            if value in ("", None) or value == getattr(d, key):
                st.pop(key, None)           # 共通デザインと同じなら上書き不要
            else:
                st[key] = value
            e.style = st
        self._refresh_tree()
        self._insp_load(force=True)
        self._schedule_preview_update()

    # ─── ステータスバー（下部） ───────────────────

    def _setup_bottom_bar(self):
        bar = ttk.Frame(self, padding=(8, 3))
        bar.pack(fill='x', side='bottom')
        ttk.Separator(self, orient='horizontal').pack(fill='x', side='bottom')

        self._progress_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._progress_var, style='Progress.TLabel').pack(
            side='left', padx=4, fill='x', expand=True)

        # 右側: 保存先・トラック
        ttk.Button(bar, text="開く", width=4,
                   command=self._open_save_dir).pack(side='right', padx=(0, 2))
        ttk.Button(bar, text="📂", width=3,
                   command=self._pick_resolve_out_dir).pack(side='right', padx=(0, 2))
        save_entry = ttk.Entry(bar, textvariable=self._resolve_out_dir, width=34)
        save_entry.pack(side='right', padx=(2, 2))
        save_entry.bind('<FocusOut>', lambda e: self._save_app_config())
        save_entry.bind('<Return>',   lambda e: self._save_app_config())
        ttk.Label(bar, text="📁 PNG保存先:").pack(side='right')
        ttk.Separator(bar, orient='vertical').pack(side='right', fill='y', padx=8)
        ttk.Spinbox(bar, from_=1, to=20, textvariable=self._resolve_track,
                    width=3).pack(side='right')
        ttk.Label(bar, text="配置トラック V").pack(side='right', padx=(0, 2))

    def _setup_right_panel(self, parent):
        self._right_pane = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        self._right_pane.pack(fill=tk.BOTH, expand=True)

        self._design_outer = ttk.Frame(self._right_pane)
        self._right_pane.add(self._design_outer, weight=1)

        self._preview_outer = ttk.Frame(self._right_pane)
        self._right_pane.add(self._preview_outer, weight=4)   # プレビューを大きく

        self._setup_design_panel(self._design_outer)
        self._setup_preview_panel(self._preview_outer)
        # 初期サッシ位置: 描画後にデザインパネルを約220pxに固定
        self.after(150, self._init_preview_sash)

    def _init_preview_sash(self):
        try:
            self._right_pane.sashpos(0, 220)
        except Exception:
            pass

    def _setup_right2_panel(self, parent):
        """右2パネル: 合成プレビュー + VoiceVox をタブ表示。
        テロッププレビュー・テンプレートは独立ウィンドウとして呼び出す。"""
        nb = TearableNotebook(parent)
        nb.pack(fill='both', expand=True, padx=2, pady=2)
        self._main_nb = nb

        comp_frame = ttk.Frame(nb)
        nb.add_tab(comp_frame, "合成プレビュー", weight=2)
        self._build_composite_pane(comp_frame)

        # VoiceVox パネルをウィンドウ内に埋め込み
        vv_frame = ttk.Frame(nb)
        nb.add_tab(vv_frame, "🔊 VoiceVox", weight=2)
        self._voicevox_panel = VoiceVoxPanel(vv_frame, self)
        self._voicevox_panel.pack(fill='both', expand=True)

        # テロッププレビュー・テンプレートのウィンドウ参照
        self._telop_pv_win:   Optional[tk.Toplevel] = None
        self._template_win:   Optional[tk.Toplevel] = None

    def _build_telop_preview_pane(self, parent):
        lf = ttk.LabelFrame(parent, text="テロップ プレビュー", padding=4)
        lf.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        hdr = ttk.Frame(lf)
        hdr.pack(fill='x')
        nav = ttk.Frame(hdr)
        nav.pack(side='right')
        ttk.Button(nav, text="◀", width=3, command=self._telop_pv_prev).pack(side='left')
        ttk.Button(nav, text="▶", width=3, command=self._telop_pv_next).pack(side='left')
        self._telop_pv_info_var = tk.StringVar(value="テロップを選択してください")
        ttk.Label(hdr, textvariable=self._telop_pv_info_var, font=('', 9)).pack(side='left')

        self._telop_pv_canvas = tk.Canvas(lf, bg='#555555', takefocus=True)
        self._telop_pv_canvas.pack(fill=tk.BOTH, expand=True)
        self._telop_pv_canvas.bind('<Configure>', lambda e: self._schedule_telop_pv_update())
        self._telop_pv_canvas.bind('<Left>',  lambda e: self._telop_pv_prev())
        self._telop_pv_canvas.bind('<Right>', lambda e: self._telop_pv_next())

    def show_telop_preview(self, entry: TelopEntry):
        self._telop_pv_entry = entry
        # プレビューウィンドウは自動起動しない。
        # 既に開いている場合のみ内容を更新する。
        win = getattr(self, '_telop_pv_win', None)
        if win is not None and win.winfo_exists():
            self._schedule_telop_pv_update()

    def _schedule_telop_pv_update(self):
        # プレビューウィンドウ（とキャンバス）が無いときは何もしない
        win = getattr(self, '_telop_pv_win', None)
        if win is None or not win.winfo_exists():
            return
        if not hasattr(self, '_telop_pv_canvas'):
            return
        if not self._telop_pv_pending:
            self._telop_pv_pending = True
            self.after(120, self._do_telop_pv_update)

    def _do_telop_pv_update(self):
        self._telop_pv_pending = False
        if not PILLOW_OK:
            return
        # ウィンドウが閉じられている場合はスキップ
        win = getattr(self, '_telop_pv_win', None)
        if win is None or not win.winfo_exists() or not hasattr(self, '_telop_pv_canvas'):
            return
        try:
            if not self._telop_pv_canvas.winfo_exists():
                return
        except tk.TclError:
            return
        entry = self._telop_pv_entry
        canvas = self._telop_pv_canvas
        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        if cw < 10 or ch < 10:
            return

        if entry is None:
            canvas.delete('all')
            canvas.create_text(cw // 2, ch // 2,
                text="テロップを選択してください", fill='white', font=('', 12))
            return

        idx = self._telop_pv_current_idx()
        total = len(self.telop_list)
        info_text = (f"[{idx + 1}/{total}]  No.{entry.id}  "
                     f"{entry.start_time} → {entry.end_time}  "
                     f"| {entry.text.replace(chr(10), ' ')[:60]}")
        self._telop_pv_info_var.set(info_text)

        from core.renderer import render_telop_image
        try:
            design = self._collect_design()
            img    = render_telop_image(entry.text, design.with_overrides(entry.style))
            W, H   = design.canvas_width, design.canvas_height
            bg     = _make_checker_bg(W, H)
            bg.paste(img, (0, 0), img)
            scale = min(cw / W, ch / H)
            pw, ph = int(W * scale), int(H * scale)
            thumb  = bg.resize((pw, ph), Image.LANCZOS)
            photo  = ImageTk.PhotoImage(thumb)
            self._telop_pv_photo_ref = photo
            canvas.delete('all')
            ox, oy = (cw - pw) // 2, (ch - ph) // 2
            canvas.create_image(ox, oy, anchor='nw', image=photo)
            info = f"{W}×{H}  {design.frame_rate}fps"
            canvas.create_text(ox + 5, oy + 5, text=info,
                anchor='nw', fill='white', font=('', 9))
            canvas.create_text(ox + 4, oy + 4, text=info,
                anchor='nw', fill='#333333', font=('', 9))
        except Exception as ex:
            canvas.delete('all')
            canvas.create_text(cw // 2, ch // 2,
                text=f"レンダリングエラー:\n{ex}", fill='red', justify='center')

    def _telop_pv_current_idx(self) -> int:
        if self._telop_pv_entry is None:
            return -1
        try:
            return self.telop_list.index(self._telop_pv_entry)
        except ValueError:
            return -1

    def _telop_pv_go_to(self, idx: int):
        if not self.telop_list:
            return
        idx = max(0, min(idx, len(self.telop_list) - 1))
        entry = self.telop_list[idx]
        self.show_telop_preview(entry)
        self._tree.selection_set(str(entry.id))
        self._tree.see(str(entry.id))
        self._telop_pv_canvas.focus_set()

    def _telop_pv_prev(self):
        self._telop_pv_go_to(self._telop_pv_current_idx() - 1)

    def _telop_pv_next(self):
        self._telop_pv_go_to(self._telop_pv_current_idx() + 1)

    def _setup_design_panel(self, parent):
        hdr_design = ttk.Frame(parent)
        hdr_design.pack(fill='x', pady=(2, 0))
        ttk.Button(hdr_design, text="↗ 分離", width=7,
                   command=lambda: self._toggle_float_panel(
                       self._design_outer, self._right_pane, 1, "デザイン設定")
                   ).pack(side='right', padx=4)

        nb = ttk.Notebook(parent)
        nb.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        fonts = [name for name, _ in self._get_fonts_cached()]
        pad = dict(padx=4, pady=3)

        # ═══════════════════════════════════════
        # タブ1: テキスト
        # ═══════════════════════════════════════
        tab_text = ttk.Frame(nb, padding=10)
        nb.add(tab_text, text=" テキスト ")
        tab_text.columnconfigure(1, weight=1)

        ttk.Label(tab_text, text="フォント:").grid(row=0, column=0, sticky='e', **pad)
        self._font_var = tk.StringVar(value=self.design.font_path)
        self._font_combo = ttk.Combobox(tab_text, textvariable=self._font_var, values=fonts, width=28)
        self._font_combo.grid(row=0, column=1, sticky='ew', **pad)
        ttk.Button(tab_text, text="参照...", command=self._pick_font_file).grid(row=0, column=2, padx=4)
        self._font_var.trace_add('write', self._on_design_change)

        ttk.Label(tab_text, text="サイズ:").grid(row=1, column=0, sticky='e', **pad)
        self._font_size_var = tk.IntVar(value=self.design.font_size)
        sz_frame = ttk.Frame(tab_text)
        sz_frame.grid(row=1, column=1, columnspan=2, sticky='w', **pad)
        ttk.Spinbox(sz_frame, from_=8, to=300, textvariable=self._font_size_var, width=6,
                    command=self._on_design_change).pack(side='left')
        ttk.Scale(sz_frame, from_=8, to=200, variable=self._font_size_var, orient='horizontal',
                  length=160, command=lambda v: self._on_design_change()).pack(side='left', padx=6)
        ttk.Label(sz_frame, text="px").pack(side='left')
        self._font_size_var.trace_add('write', self._on_design_change)

        ttk.Label(tab_text, text="文字色:").grid(row=2, column=0, sticky='e', **pad)
        self._font_color_btn = ColorButton(tab_text, color=self.design.font_color,
                                            command=self._on_font_color_change)
        self._font_color_btn.grid(row=2, column=1, sticky='w', **pad)

        ttk.Label(tab_text, text="字間 (カーニング):").grid(row=3, column=0, sticky='e', **pad)
        self._kern_var = tk.IntVar(value=self.design.letter_spacing)
        kern_frame = ttk.Frame(tab_text)
        kern_frame.grid(row=3, column=1, columnspan=2, sticky='w', **pad)
        ttk.Spinbox(kern_frame, from_=-50, to=200, textvariable=self._kern_var, width=6,
                    command=self._on_design_change).pack(side='left')
        ttk.Scale(kern_frame, from_=-20, to=100, variable=self._kern_var, orient='horizontal',
                  length=160, command=lambda v: self._on_design_change()).pack(side='left', padx=6)
        ttk.Label(kern_frame, text="px").pack(side='left')
        self._kern_var.trace_add('write', self._on_design_change)

        ttk.Label(tab_text, text="行間:").grid(row=4, column=0, sticky='e', **pad)
        self._linespace_var = tk.DoubleVar(value=self.design.line_spacing)
        ls_frame = ttk.Frame(tab_text)
        ls_frame.grid(row=4, column=1, columnspan=2, sticky='w', **pad)
        ttk.Spinbox(ls_frame, from_=0.8, to=4.0, increment=0.1,
                    textvariable=self._linespace_var, width=6,
                    format='%.1f', command=self._on_design_change).pack(side='left')
        ttk.Label(ls_frame, text="倍", foreground='gray').pack(side='left', padx=4)
        self._linespace_var.trace_add('write', self._on_design_change)

        # ═══════════════════════════════════════
        # タブ2: エフェクト (縁取り / シャドウ / 背景)
        # ═══════════════════════════════════════
        tab_fx_outer = ttk.Frame(nb)
        nb.add(tab_fx_outer, text=" エフェクト ")

        # スクロール可能にする
        fx_canvas = tk.Canvas(tab_fx_outer, highlightthickness=0)
        fx_scroll = ttk.Scrollbar(tab_fx_outer, orient='vertical', command=fx_canvas.yview)
        fx_canvas.configure(yscrollcommand=fx_scroll.set)
        fx_scroll.pack(side='right', fill='y')
        fx_canvas.pack(side='left', fill='both', expand=True)
        tab_fx = ttk.Frame(fx_canvas, padding=(8, 6))
        fx_win = fx_canvas.create_window((0, 0), window=tab_fx, anchor='nw')

        def _fx_canvas_configure(e):
            fx_canvas.configure(scrollregion=fx_canvas.bbox('all'))
            fx_canvas.itemconfigure(fx_win, width=e.width)
        tab_fx.bind('<Configure>', _fx_canvas_configure)
        fx_canvas.bind('<Configure>', lambda e: fx_canvas.itemconfigure(fx_win, width=e.width))

        # ── 縁取りセクション ──
        stroke_lf = ttk.LabelFrame(tab_fx, text="縁取り", padding=8)
        stroke_lf.pack(fill='x', pady=(0, 8))

        self._stroke_enabled_var = tk.BooleanVar(value=self.design.stroke_enabled)
        ttk.Checkbutton(stroke_lf, text="有効にする",
                        variable=self._stroke_enabled_var,
                        command=lambda: self._on_fx_toggle('stroke')).pack(anchor='w', pady=(0, 6))

        self._stroke_detail = ttk.Frame(stroke_lf)
        self._stroke_detail.pack(fill='x')
        self._stroke_detail.columnconfigure(1, weight=1)

        ttk.Label(self._stroke_detail, text="色:").grid(row=0, column=0, sticky='e', **pad)
        self._stroke_color_btn = ColorButton(self._stroke_detail, color=self.design.stroke_color,
                                              command=self._on_stroke_color_change)
        self._stroke_color_btn.grid(row=0, column=1, sticky='w', **pad)

        ttk.Label(self._stroke_detail, text="幅:").grid(row=1, column=0, sticky='e', **pad)
        self._stroke_w_var = tk.IntVar(value=self.design.stroke_width)
        sw_frame = ttk.Frame(self._stroke_detail)
        sw_frame.grid(row=1, column=1, sticky='w', **pad)
        ttk.Spinbox(sw_frame, from_=0, to=30, textvariable=self._stroke_w_var, width=6,
                    command=self._on_design_change).pack(side='left')
        ttk.Scale(sw_frame, from_=0, to=20, variable=self._stroke_w_var, orient='horizontal',
                  length=140, command=lambda v: self._on_design_change()).pack(side='left', padx=6)
        ttk.Label(sw_frame, text="px").pack(side='left')
        self._stroke_w_var.trace_add('write', self._on_design_change)

        # ── ドロップシャドウセクション ──
        shadow_lf = ttk.LabelFrame(tab_fx, text="ドロップシャドウ", padding=8)
        shadow_lf.pack(fill='x', pady=(0, 8))

        self._shadow_enabled_var = tk.BooleanVar(value=self.design.shadow_enabled)
        ttk.Checkbutton(shadow_lf, text="有効にする",
                        variable=self._shadow_enabled_var,
                        command=lambda: self._on_fx_toggle('shadow')).pack(anchor='w', pady=(0, 6))

        self._shadow_detail = ttk.Frame(shadow_lf)
        self._shadow_detail.pack(fill='x')
        self._shadow_detail.columnconfigure(1, weight=1)

        ttk.Label(self._shadow_detail, text="色:").grid(row=0, column=0, sticky='e', **pad)
        self._shadow_color_btn = ColorButton(self._shadow_detail, color=self.design.shadow_color,
                                              command=self._on_shadow_color_change)
        self._shadow_color_btn.grid(row=0, column=1, sticky='w', **pad)

        ttk.Label(self._shadow_detail, text="不透明度:").grid(row=1, column=0, sticky='e', **pad)
        self._shadow_opacity_var = tk.IntVar(value=self.design.shadow_opacity)
        op_frame = ttk.Frame(self._shadow_detail)
        op_frame.grid(row=1, column=1, sticky='w', **pad)
        ttk.Scale(op_frame, from_=0, to=255, variable=self._shadow_opacity_var,
                  orient='horizontal', length=140,
                  command=lambda v: self._on_design_change()).pack(side='left')
        ttk.Label(op_frame, textvariable=self._shadow_opacity_var, width=4).pack(side='left', padx=4)
        self._shadow_opacity_var.trace_add('write', self._on_design_change)

        ttk.Label(self._shadow_detail, text="オフセット X:").grid(row=2, column=0, sticky='e', **pad)
        self._shadow_ox_var = tk.IntVar(value=self.design.shadow_offset_x)
        ttk.Spinbox(self._shadow_detail, from_=-50, to=50, textvariable=self._shadow_ox_var,
                    width=6, command=self._on_design_change).grid(row=2, column=1, sticky='w', **pad)
        self._shadow_ox_var.trace_add('write', self._on_design_change)

        ttk.Label(self._shadow_detail, text="オフセット Y:").grid(row=3, column=0, sticky='e', **pad)
        self._shadow_oy_var = tk.IntVar(value=self.design.shadow_offset_y)
        ttk.Spinbox(self._shadow_detail, from_=-50, to=50, textvariable=self._shadow_oy_var,
                    width=6, command=self._on_design_change).grid(row=3, column=1, sticky='w', **pad)
        self._shadow_oy_var.trace_add('write', self._on_design_change)

        # ── 背景セクション ──
        bg_lf = ttk.LabelFrame(tab_fx, text="背景", padding=8)
        bg_lf.pack(fill='x', pady=(0, 4))

        self._bg_enabled_var = tk.BooleanVar(value=self.design.bg_enabled)
        ttk.Checkbutton(bg_lf, text="有効にする",
                        variable=self._bg_enabled_var,
                        command=lambda: self._on_fx_toggle('bg')).pack(anchor='w', pady=(0, 6))

        self._bg_detail = ttk.Frame(bg_lf)
        self._bg_detail.pack(fill='x')
        self._bg_detail.columnconfigure(1, weight=1)

        ttk.Label(self._bg_detail, text="色:").grid(row=0, column=0, sticky='e', **pad)
        self._bg_color_btn = ColorButton(self._bg_detail, color=self.design.bg_color,
                                          command=self._on_bg_color_change)
        self._bg_color_btn.grid(row=0, column=1, sticky='w', **pad)

        ttk.Label(self._bg_detail, text="不透明度:").grid(row=1, column=0, sticky='e', **pad)
        self._bg_opacity_var = tk.IntVar(value=self.design.bg_opacity)
        bg_op_frame = ttk.Frame(self._bg_detail)
        bg_op_frame.grid(row=1, column=1, sticky='w', **pad)
        ttk.Scale(bg_op_frame, from_=0, to=255, variable=self._bg_opacity_var,
                  orient='horizontal', length=140,
                  command=lambda v: self._on_design_change()).pack(side='left')
        ttk.Label(bg_op_frame, textvariable=self._bg_opacity_var, width=4).pack(side='left', padx=4)
        self._bg_opacity_var.trace_add('write', self._on_design_change)

        ttk.Label(self._bg_detail, text="内側余白:").grid(row=2, column=0, sticky='e', **pad)
        self._bg_pad_var = tk.IntVar(value=self.design.bg_padding)
        bp_frame = ttk.Frame(self._bg_detail)
        bp_frame.grid(row=2, column=1, sticky='w', **pad)
        ttk.Spinbox(bp_frame, from_=0, to=100, textvariable=self._bg_pad_var,
                    width=6, command=self._on_design_change).pack(side='left')
        ttk.Label(bp_frame, text="px").pack(side='left', padx=4)
        self._bg_pad_var.trace_add('write', self._on_design_change)

        # 初期グレーアウト適用
        for key in ('stroke', 'shadow', 'bg'):
            self._on_fx_toggle(key, refresh_preview=False)

        # ═══════════════════════════════════════
        # タブ3: 配置 (位置 + キャンバス)
        # ═══════════════════════════════════════
        tab_layout = ttk.Frame(nb, padding=8)
        nb.add(tab_layout, text=" 配置 ")

        # ── テキスト配置セクション ──
        pos_lf = ttk.LabelFrame(tab_layout, text="テキスト配置", padding=8)
        pos_lf.pack(fill='x', pady=(0, 8))
        pos_lf.columnconfigure(1, weight=1)

        ttk.Label(pos_lf, text="横位置:").grid(row=0, column=0, sticky='e', **pad)
        self._pos_h_var = tk.StringVar(value=self.design.position_h)
        ph_frame = ttk.Frame(pos_lf)
        ph_frame.grid(row=0, column=1, sticky='w', **pad)
        for val, label in [("left", "左"), ("center", "中央"), ("right", "右")]:
            ttk.Radiobutton(ph_frame, text=label, variable=self._pos_h_var, value=val,
                            command=self._on_design_change).pack(side='left', padx=4)

        ttk.Label(pos_lf, text="縦位置:").grid(row=1, column=0, sticky='e', **pad)
        self._pos_v_var = tk.StringVar(value=self.design.position_v)
        pv_frame = ttk.Frame(pos_lf)
        pv_frame.grid(row=1, column=1, sticky='w', **pad)
        for val, label in [("top", "上"), ("middle", "中央"), ("bottom", "下")]:
            ttk.Radiobutton(pv_frame, text=label, variable=self._pos_v_var, value=val,
                            command=self._on_design_change).pack(side='left', padx=4)

        ttk.Label(pos_lf, text="テキスト揃え:").grid(row=2, column=0, sticky='e', **pad)
        self._align_var = tk.StringVar(value=self.design.text_align)
        al_frame = ttk.Frame(pos_lf)
        al_frame.grid(row=2, column=1, sticky='w', **pad)
        for val, label in [("left", "左"), ("center", "中央"), ("right", "右")]:
            ttk.Radiobutton(al_frame, text=label, variable=self._align_var, value=val,
                            command=self._on_design_change).pack(side='left', padx=4)

        ttk.Label(pos_lf, text="X オフセット:").grid(row=3, column=0, sticky='e', **pad)
        self._offset_x_var = tk.IntVar(value=self.design.offset_x)
        ox_frame = ttk.Frame(pos_lf)
        ox_frame.grid(row=3, column=1, sticky='w', **pad)
        ttk.Spinbox(ox_frame, from_=-2000, to=2000, textvariable=self._offset_x_var,
                    width=7, command=self._on_design_change).pack(side='left')
        ttk.Label(ox_frame, text="px").pack(side='left', padx=4)
        self._offset_x_var.trace_add('write', self._on_design_change)

        ttk.Label(pos_lf, text="Y オフセット:").grid(row=4, column=0, sticky='e', **pad)
        self._offset_y_var = tk.IntVar(value=self.design.offset_y)
        oy_frame = ttk.Frame(pos_lf)
        oy_frame.grid(row=4, column=1, sticky='w', **pad)
        ttk.Spinbox(oy_frame, from_=-2000, to=2000, textvariable=self._offset_y_var,
                    width=7, command=self._on_design_change).pack(side='left')
        ttk.Label(oy_frame, text="px").pack(side='left', padx=4)
        self._offset_y_var.trace_add('write', self._on_design_change)

        # ── キャンバスセクション ──
        canvas_lf = ttk.LabelFrame(tab_layout, text="キャンバス", padding=8)
        canvas_lf.pack(fill='x')
        canvas_lf.columnconfigure(1, weight=1)

        ttk.Label(canvas_lf, text="解像度:").grid(row=0, column=0, sticky='e', **pad)
        res_frame = ttk.Frame(canvas_lf)
        res_frame.grid(row=0, column=1, sticky='w', **pad)
        self._cw_var = tk.IntVar(value=self.design.canvas_width)
        self._ch_var = tk.IntVar(value=self.design.canvas_height)
        ttk.Spinbox(res_frame, from_=320, to=7680, textvariable=self._cw_var,
                    width=6, command=self._on_design_change).pack(side='left')
        ttk.Label(res_frame, text="×").pack(side='left', padx=4)
        ttk.Spinbox(res_frame, from_=240, to=4320, textvariable=self._ch_var,
                    width=6, command=self._on_design_change).pack(side='left')
        ttk.Label(res_frame, text="px").pack(side='left', padx=4)
        for w, h, label in [(1920, 1080, "FHD"), (3840, 2160, "4K"), (1280, 720, "HD")]:
            ttk.Button(res_frame, text=label, width=5,
                       command=lambda ww=w, hh=h: self._set_resolution(ww, hh)).pack(side='left', padx=2)
        self._cw_var.trace_add('write', self._on_design_change)
        self._ch_var.trace_add('write', self._on_design_change)

        ttk.Label(canvas_lf, text="フレームレート:").grid(row=1, column=0, sticky='e', **pad)
        self._fps_var = tk.DoubleVar(value=self.design.frame_rate)
        fps_combo = ttk.Combobox(canvas_lf, textvariable=self._fps_var, width=10,
                                  values=[23.976, 24.0, 25.0, 29.97, 30.0, 59.94, 60.0])
        fps_combo.grid(row=1, column=1, sticky='w', **pad)
        self._fps_var.trace_add('write', self._on_design_change)

    def _setup_preview_panel(self, parent):
        preview_frame = ttk.LabelFrame(parent, text="プレビュー", padding=4)
        preview_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # プレビューテキスト入力 + フローティングボタン
        pt_frame = ttk.Frame(preview_frame)
        pt_frame.pack(fill='x', pady=(0, 4))
        ttk.Button(pt_frame, text="↗ 分離", width=7,
                   command=lambda: self._toggle_float_panel(
                       self._preview_outer, self._right_pane, 2, "プレビュー")
                   ).pack(side='right', padx=4)
        ttk.Label(pt_frame, text="プレビューテキスト:").pack(side='left')
        self._preview_text_var = tk.StringVar(value="テロップのプレビューです\n2行目のテキスト")
        ttk.Entry(pt_frame, textvariable=self._preview_text_var, width=40).pack(
            side='left', padx=6, fill='x', expand=True)
        self._preview_text_var.trace_add('write', self._on_design_change)

        # 移動コントロール行
        ctrl_frame = ttk.Frame(preview_frame)
        ctrl_frame.pack(fill='x', pady=(0, 2))

        # ── 位置プリセット 3×3 グリッド ──────────────
        ttk.Label(ctrl_frame, text="位置:").pack(side='left', padx=(0, 2))
        grid_frame = ttk.Frame(ctrl_frame)
        grid_frame.pack(side='left')
        _snap_defs = [
            ("↖", "left",   "top",    0, 0), ("↑", "center", "top",    0, 0), ("↗", "right",  "top",    0, 0),
            ("←", "left",   "middle", 0, 0), ("⊕", "center", "middle", 0, 0), ("→", "right",  "middle", 0, 0),
            ("↙", "left",   "bottom", 0, 0), ("↓", "center", "bottom", 0, 0), ("↘", "right",  "bottom", 0, 0),
        ]
        for i, (sym, ph, pv, ox, oy) in enumerate(_snap_defs):
            ttk.Button(grid_frame, text=sym, width=3,
                       command=lambda _ph=ph, _pv=pv: self._snap_position(_ph, _pv)
                       ).grid(row=i // 3, column=i % 3, padx=1, pady=1)

        ttk.Separator(ctrl_frame, orient='vertical').pack(side='left', fill='y', padx=8)

        # ── ステップ移動 ──────────────────────────────
        ttk.Label(ctrl_frame, text="移動:").pack(side='left', padx=(0, 4))
        for symbol, dx, dy in [("←", -1, 0), ("↑", 0, -1), ("↓", 0, 1), ("→", 1, 0)]:
            ttk.Button(ctrl_frame, text=symbol, width=3,
                       command=lambda ddx=dx, ddy=dy: self._move_telop_step(ddx, ddy)
                       ).pack(side='left', padx=1)

        ttk.Separator(ctrl_frame, orient='vertical').pack(side='left', fill='y', padx=6)
        ttk.Label(ctrl_frame, text="移動量:").pack(side='left')
        self._move_step_var = tk.IntVar(value=10)
        ttk.Spinbox(ctrl_frame, from_=1, to=200, textvariable=self._move_step_var,
                    width=5).pack(side='left', padx=2)
        ttk.Label(ctrl_frame, text="px", foreground='gray').pack(side='left')

        # ヒントラベル
        hint = ttk.Label(preview_frame,
                         text="クリック→文字入力  ドラッグ→位置移動  矢印キー→移動  Shift+矢印→1px  Ctrl+矢印→50px",
                         foreground='gray', font=('', 8))
        hint.pack(anchor='e', padx=4)

        # キャンバス (実際のプレビュー) — takefocus でキー入力受付
        self._preview_canvas = tk.Canvas(
            preview_frame, bg='#888888', cursor='crosshair',
            takefocus=True, highlightthickness=2,
            highlightbackground='#666666', highlightcolor='#0078D7')
        self._preview_canvas.pack(fill=tk.BOTH, expand=True)
        self._preview_canvas.bind('<Configure>',    lambda e: self._schedule_preview_update())
        self._preview_canvas.bind('<ButtonPress-1>', self._canvas_press)
        self._preview_canvas.bind('<B1-Motion>',    self._canvas_drag_move)
        self._preview_canvas.bind('<Key>',          self._canvas_on_key)
        self._preview_canvas.bind('<BackSpace>',    self._canvas_on_backspace)
        self._preview_canvas.bind('<Return>',       self._canvas_on_return)
        for keysym in ('Left', 'Right', 'Up', 'Down'):
            self._preview_canvas.bind(f'<{keysym}>',         self._canvas_on_arrow)
            self._preview_canvas.bind(f'<Shift-{keysym}>',   self._canvas_on_arrow)
            self._preview_canvas.bind(f'<Control-{keysym}>', self._canvas_on_arrow)

    def _build_template_pane(self, parent):
        lf = ttk.LabelFrame(parent, text="テンプレート", padding=4)
        lf.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        btn_row = ttk.Frame(lf)
        btn_row.pack(fill='x', pady=(0, 4))
        ttk.Button(btn_row, text="保存", width=6,
                   command=self._template_save).pack(side='left', padx=2)
        ttk.Button(btn_row, text="読込", width=6,
                   command=self._template_load).pack(side='left', padx=2)
        ttk.Button(btn_row, text="削除", width=6,
                   command=self._template_delete).pack(side='left', padx=2)

        lb_frame = ttk.Frame(lf)
        lb_frame.pack(fill='x')
        sb = ttk.Scrollbar(lb_frame, orient='vertical')
        self._template_lb = tk.Listbox(lb_frame, height=5, yscrollcommand=sb.set,
                                        exportselection=False)
        sb.configure(command=self._template_lb.yview)
        self._template_lb.pack(side='left', fill='x', expand=True)
        sb.pack(side='left', fill='y')
        self._template_lb.bind('<<ListboxSelect>>', self._on_template_select)

        self._template_pv_canvas = tk.Canvas(lf, bg='#555555', height=120)
        self._template_pv_canvas.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self._template_pv_canvas.bind('<Configure>',
                                      lambda e: self._schedule_template_pv_update())

        self._refresh_template_list()

    # ─────────────────────────────────────────────
    #  合成プレビューパネル
    # ─────────────────────────────────────────────

    def _build_composite_pane(self, parent):
        """Premiere Pro / Resolve 映像 + Photoshop/DavinTelop テロップ合成プレビュー。"""
        outer = ttk.Frame(parent, padding=4)
        outer.pack(fill='both', expand=True)
        pad = dict(padx=4, pady=2)

        if not HAS_CAPTURE:
            ttk.Label(outer, text="core/capture.py が読み込めません",
                      foreground='red').pack()
            return

        # ── 背景ソース (NDI / スクリーンキャプチャ) ──────
        bg_lf = ttk.LabelFrame(outer, text="背景ソース (映像)", padding=6)
        bg_lf.pack(fill='x', pady=(0, 4))

        # 入力方式選択
        mode_row = ttk.Frame(bg_lf)
        mode_row.pack(fill='x')
        self._comp_bg_mode_var = tk.StringVar(value="ndi")
        ttk.Radiobutton(mode_row, text="NDI", variable=self._comp_bg_mode_var,
                        value="ndi", command=self._on_bg_mode_change).pack(side='left', padx=4)
        ttk.Radiobutton(mode_row, text="映像キャプチャ(Blackmagic)", variable=self._comp_bg_mode_var,
                        value="device", command=self._on_bg_mode_change).pack(side='left', padx=4)
        ttk.Radiobutton(mode_row, text="スクリーン", variable=self._comp_bg_mode_var,
                        value="screen", command=self._on_bg_mode_change).pack(side='left', padx=4)
        ttk.Radiobutton(mode_row, text="TeloCut", variable=self._comp_bg_mode_var,
                        value="telocut", command=self._on_bg_mode_change).pack(side='left', padx=4)

        # ── TeloCut（連携で今の絵をそのまま受け取る）──────
        self._bg_telocut_frame = ttk.Frame(bg_lf)
        tc_row = ttk.Frame(self._bg_telocut_frame)
        tc_row.pack(fill='x')
        ttk.Label(tc_row, text="大きさ:").pack(side='left')
        self._comp_tc_width_var = tk.StringVar(value="960")
        ttk.Combobox(tc_row, textvariable=self._comp_tc_width_var, values=["640", "960", "1280", "1920"],
                     state='readonly', width=6).pack(side='left', padx=4)
        ttk.Label(tc_row, text="FPS:").pack(side='left', padx=(8, 0))
        self._comp_tc_fps_var = tk.IntVar(value=12)
        ttk.Spinbox(tc_row, from_=1, to=30, textvariable=self._comp_tc_fps_var, width=4).pack(side='left', padx=4)
        self._comp_tc_telops_var = tk.BooleanVar(value=False)
        for _v in (self._comp_tc_telops_var, self._comp_tc_width_var, self._comp_tc_fps_var):
            _v.trace_add('write', self._on_tc_option_change)
        ttk.Checkbutton(self._bg_telocut_frame, text="TeloCut のテロップも入れる（外すと DavinTelop のテロップだけを重ねる）",
                        variable=self._comp_tc_telops_var).pack(anchor='w', pady=(2, 0))
        ttk.Label(self._bg_telocut_frame,
                  text="TeloCut のレコードモニター（再生ヘッドの絵）をそのまま背景にします。\n"
                       "画面キャプチャより正確で、TeloCut のウィンドウが隠れていても動きます。",
                  foreground='gray', font=('', 8), justify='left').pack(anchor='w', pady=(2, 0))

        # ── NDI 設定エリア ────────────────────────────
        self._bg_ndi_frame = ttk.Frame(bg_lf)
        self._bg_ndi_frame.pack(fill='x', **pad)

        ndi_src_row = ttk.Frame(self._bg_ndi_frame)
        ndi_src_row.pack(fill='x')
        ttk.Label(ndi_src_row, text="ソース:").pack(side='left')
        self._comp_ndi_src_var = tk.StringVar()
        self._comp_ndi_combo = ttk.Combobox(
            ndi_src_row, textvariable=self._comp_ndi_src_var,
            state='readonly', width=36)
        self._comp_ndi_combo.pack(side='left', padx=4, fill='x', expand=True)
        ttk.Button(ndi_src_row, text="🔍 検索", width=7,
                   command=self._scan_ndi_sources).pack(side='left', padx=2)

        ndi_hint = ttk.Label(self._bg_ndi_frame,
            text="Resolve/Premiere で NDI 出力を有効にして「🔍 検索」をクリック",
            foreground='gray', font=('', 8))
        ndi_hint.pack(anchor='w', pady=(2, 0))

        # ── 映像キャプチャデバイス設定エリア ───────────
        self._bg_device_frame = ttk.Frame(bg_lf)

        dev_row = ttk.Frame(self._bg_device_frame)
        dev_row.pack(fill='x')
        ttk.Label(dev_row, text="デバイス:").pack(side='left')
        self._comp_dev_var = tk.StringVar()
        self._comp_dev_combo = ttk.Combobox(
            dev_row, textvariable=self._comp_dev_var,
            state='readonly', width=32)
        self._comp_dev_combo.pack(side='left', padx=4, fill='x', expand=True)
        ttk.Button(dev_row, text="🔄", width=3,
                   command=self._refresh_capture_devices).pack(side='left', padx=2)

        ttk.Label(self._bg_device_frame,
            text="Blackmagic WDM Capture / DeckLink Video Capture を選択。\n"
                 "Resolve の出力を SDI/HDMI でループバック入力すると取り込めます。",
            foreground='gray', font=('', 8), justify='left').pack(anchor='w', pady=(2, 0))
        self._comp_devices: list = []

        # ── スクリーンキャプチャ設定エリア ───────────
        self._bg_screen_frame = ttk.Frame(bg_lf)

        app_row = ttk.Frame(self._bg_screen_frame)
        app_row.pack(fill='x')
        ttk.Label(app_row, text="アプリ:").pack(side='left')
        self._comp_bg_app_var = tk.StringVar(value="premiere")
        for val, lbl in [("premiere", "Premiere Pro"), ("resolve", "DaVinci Resolve")]:
            ttk.Radiobutton(app_row, text=lbl, variable=self._comp_bg_app_var,
                            value=val).pack(side='left', padx=6)

        # モニター選択（マルチディスプレイ対応・ドラッグ不要）
        mon_row = ttk.Frame(self._bg_screen_frame)
        mon_row.pack(fill='x', **pad)
        ttk.Label(mon_row, text="モニター:").pack(side='left')
        self._comp_bg_monitor_var = tk.StringVar()
        self._comp_bg_monitor_combo = ttk.Combobox(
            mon_row, textvariable=self._comp_bg_monitor_var,
            state='readonly', width=26)
        self._comp_bg_monitor_combo.pack(side='left', padx=4)
        ttk.Button(mon_row, text="🔄", width=3,
                   command=self._refresh_monitors).pack(side='left', padx=2)
        self._comp_bg_monitor_combo.bind(
            '<<ComboboxSelected>>', lambda e: self._on_monitor_selected())
        self._comp_monitors: list = []

        reg_row = ttk.Frame(self._bg_screen_frame)
        reg_row.pack(fill='x', **pad)
        ttk.Label(reg_row, text="または範囲:").pack(side='left')
        ttk.Button(reg_row, text="範囲選択", width=8,
                   command=self._select_bg_region).pack(side='left', padx=4)
        self._comp_bg_region_lbl = tk.StringVar(value="  未設定")
        ttk.Label(reg_row, textvariable=self._comp_bg_region_lbl,
                  foreground='gray', font=('', 8)).pack(side='left')

        # 区画ラベル作成後にモニター一覧を初期化（_on_monitor_selected が参照するため）
        self._refresh_monitors()
        self._refresh_capture_devices()

        fps_row = ttk.Frame(self._bg_screen_frame)
        fps_row.pack(fill='x', **pad)
        ttk.Label(fps_row, text="FPS:").pack(side='left')
        self._comp_bg_fps_var = tk.IntVar(value=15)
        ttk.Spinbox(fps_row, from_=1, to=30, textvariable=self._comp_bg_fps_var,
                    width=4).pack(side='left', padx=4)

        # ── 共通: 開始/停止/ステータス ───────────────
        ctrl_row = ttk.Frame(bg_lf)
        ctrl_row.pack(fill='x', **pad)
        ttk.Button(ctrl_row, text="▶ 開始", width=7,
                   command=self._start_bg_capture).pack(side='left', padx=(0, 2))
        ttk.Button(ctrl_row, text="■ 停止", width=7,
                   command=self._stop_bg_capture).pack(side='left', padx=2)
        self._comp_bg_status_var = tk.StringVar(value="停止中")
        ttk.Label(ctrl_row, textvariable=self._comp_bg_status_var,
                  foreground='gray', font=('', 8)).pack(side='left', padx=6)

        # 初期表示
        self._on_bg_mode_change()

        # ── テロップソース ────────────────────────────
        fg_lf = ttk.LabelFrame(outer, text="テロップソース", padding=6)
        fg_lf.pack(fill='x', pady=(0, 4))

        mode_row = ttk.Frame(fg_lf)
        mode_row.pack(fill='x')
        self._comp_fg_mode_var = tk.StringVar(value="davintelop")
        for val, lbl in [("davintelop", "DavinTelop 出力フォルダ"),
                         ("photoshop",  "Photoshop PNG"),
                         ("screen",     "スクリーンキャプチャ")]:
            ttk.Radiobutton(mode_row, text=lbl, variable=self._comp_fg_mode_var,
                            value=val, command=self._on_fg_mode_change
                            ).pack(side='left', padx=4)

        # パス行（フォルダ/ファイル/不要で切り替え）
        self._comp_fg_path_frame = ttk.Frame(fg_lf)
        self._comp_fg_path_frame.pack(fill='x', **pad)
        self._comp_fg_path_var = tk.StringVar(value=self._resolve_out_dir.get())
        self._comp_fg_path_entry = ttk.Entry(
            self._comp_fg_path_frame, textvariable=self._comp_fg_path_var)
        self._comp_fg_path_entry.pack(side='left', fill='x', expand=True)
        ttk.Button(self._comp_fg_path_frame, text="参照", width=5,
                   command=self._browse_fg_path).pack(side='left', padx=4)

        # スクリーン範囲行（screen モード時のみ表示）
        self._comp_fg_screen_frame = ttk.Frame(fg_lf)
        ttk.Button(self._comp_fg_screen_frame, text="範囲選択", width=8,
                   command=self._select_fg_region).pack(side='left')
        self._comp_fg_region_lbl = tk.StringVar(value="  未設定")
        ttk.Label(self._comp_fg_screen_frame, textvariable=self._comp_fg_region_lbl,
                  foreground='gray', font=('', 8)).pack(side='left')

        fg_ctrl = ttk.Frame(fg_lf)
        fg_ctrl.pack(fill='x', **pad)
        ttk.Button(fg_ctrl, text="▶ 開始", width=7,
                   command=self._start_fg_source).pack(side='left', padx=(0, 2))
        ttk.Button(fg_ctrl, text="■ 停止", width=7,
                   command=self._stop_fg_source).pack(side='left', padx=2)
        self._comp_fg_status_var = tk.StringVar(value="停止中")
        ttk.Label(fg_ctrl, textvariable=self._comp_fg_status_var,
                  foreground='gray', font=('', 8)).pack(side='left', padx=6)
        ttk.Button(fg_ctrl, text="📋 Photoshopガイド",
                   command=self._show_ps_guide).pack(side='right')

        # ── 合成設定 ──────────────────────────────
        comp_ctrl = ttk.Frame(outer)
        comp_ctrl.pack(fill='x', **pad)
        ttk.Label(comp_ctrl, text="テロップ不透明度:").pack(side='left')
        self._comp_opacity_var = tk.IntVar(value=100)
        ttk.Scale(comp_ctrl, from_=0, to=100, variable=self._comp_opacity_var,
                  orient='horizontal', length=120,
                  command=lambda v: self._redraw_composite()).pack(side='left', padx=4)
        ttk.Label(comp_ctrl, textvariable=self._comp_opacity_var, width=3).pack(side='left')
        ttk.Label(comp_ctrl, text="%").pack(side='left')

        # ── プレビューキャンバス ──────────────────────
        self._comp_canvas = tk.Canvas(outer, bg='#111111')
        self._comp_canvas.pack(fill='both', expand=True, pady=(2, 2))
        self._comp_canvas.bind('<Configure>', lambda e: self._redraw_composite())

        # ── NDI 出力 ──────────────────────────────
        ndiout_row = ttk.Frame(outer)
        ndiout_row.pack(fill='x', **pad)
        self._comp_ndiout_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ndiout_row, text="NDI出力", variable=self._comp_ndiout_var,
                        command=self._toggle_ndi_output).pack(side='left')
        ttk.Label(ndiout_row, text="名前:").pack(side='left', padx=(8, 2))
        self._comp_ndiout_name = tk.StringVar(value="DavinTelop")
        ttk.Entry(ndiout_row, textvariable=self._comp_ndiout_name, width=16).pack(side='left')
        self._comp_ndiout_status = tk.StringVar(value="")
        ttk.Label(ndiout_row, textvariable=self._comp_ndiout_status,
                  foreground='gray', font=('', 8)).pack(side='left', padx=6)
        self._ndi_sender = None

        # ── 下部ボタン ─────────────────────────────
        bot = ttk.Frame(outer)
        bot.pack(fill='x')
        ttk.Button(bot, text="💾 合成画像を保存...",
                   command=self._export_composite).pack(side='left', padx=2)

        # キューポーリング開始
        self._poll_composite_queues()
        # モードに合わせて初期表示を更新
        self._on_fg_mode_change()

    def _on_bg_mode_change(self):
        """背景ソースの NDI / 映像キャプチャ / スクリーンキャプチャ切り替え。"""
        mode = self._comp_bg_mode_var.get() if hasattr(self, '_comp_bg_mode_var') else "ndi"
        # まず全て隠す
        for fr in (getattr(self, '_bg_ndi_frame', None),
                   getattr(self, '_bg_device_frame', None),
                   getattr(self, '_bg_screen_frame', None),
                   getattr(self, '_bg_telocut_frame', None)):
            if fr is not None:
                fr.pack_forget()
        if mode == "ndi":
            self._bg_ndi_frame.pack(fill='x', padx=4, pady=2)
        elif mode == "device":
            self._bg_device_frame.pack(fill='x', padx=4, pady=2)
        elif mode == "telocut":
            self._bg_telocut_frame.pack(fill='x', padx=4, pady=2)
        else:
            self._bg_screen_frame.pack(fill='x', padx=4, pady=2)

    def _refresh_capture_devices(self):
        """映像キャプチャデバイス一覧をドロップダウンに反映する。"""
        if not HAS_CAPTURE:
            return
        try:
            from core.capture import list_capture_devices
            self._comp_devices = list_capture_devices()
        except Exception:
            self._comp_devices = []
        names = [f"[{i}] {n}" for i, n in enumerate(self._comp_devices)]
        self._comp_dev_combo['values'] = names
        if names and not self._comp_dev_var.get():
            # Blackmagic / DeckLink を優先選択
            pref = 0
            for i, n in enumerate(self._comp_devices):
                low = n.lower()
                if 'decklink' in low or 'blackmagic' in low:
                    pref = i
                    break
            self._comp_dev_combo.current(pref)

    def _scan_ndi_sources(self):
        """NDI ソースをスキャンしてコンボボックスに反映する。NDI 未インストール時はインストールダイアログを表示。"""
        from core.capture import HAS_NDI
        if not HAS_NDI:
            # NDI 未インストール → インストールダイアログを開く
            self._comp_bg_status_var.set("NDI Tools が必要です")
            self._open_ndi_install_dialog()
            return

        self._comp_bg_status_var.set("NDI ソース検索中...")
        self.update()
        import threading

        def _run():
            from core.capture import scan_ndi_sources
            sources = scan_ndi_sources(timeout_ms=3000)

            def _done():
                if sources:
                    self._comp_ndi_combo['values'] = sources
                    self._comp_ndi_combo.current(0)
                    self._comp_bg_status_var.set(f"NDI ソース {len(sources)} 件検出")
                else:
                    self._comp_ndi_combo['values'] = []
                    self._comp_bg_status_var.set(
                        "ソースなし — Resolve/Premiere の NDI 出力を有効化してください")
                    if messagebox.askyesno(
                        "NDI ソースが見つかりません",
                        "NDI ソースが検出されませんでした。\n\n"
                        "考えられる原因:\n"
                        "  ・Resolve/Premiere が起動していない\n"
                        "  ・NDI 出力が有効になっていない\n\n"
                        "NDI Tools のインストールを確認しますか？",
                        parent=self,
                    ):
                        self._open_ndi_install_dialog()
            self.after(0, _done)

        threading.Thread(target=_run, daemon=True).start()

    def _open_ndi_install_dialog(self):
        """NDI Tools インストールダイアログを開く。"""
        NDIInstallDialog(self)

    def _on_fg_mode_change(self):
        mode = self._comp_fg_mode_var.get() if hasattr(self, '_comp_fg_mode_var') else "davintelop"
        # path行とscreen行の切り替え
        if mode == "screen":
            self._comp_fg_path_frame.pack_forget()
            self._comp_fg_screen_frame.pack(fill='x', padx=4, pady=2,
                                            before=self._comp_fg_screen_frame.master.winfo_children()[-1]
                                            if self._comp_fg_screen_frame.master.winfo_children() else None)
            self._comp_fg_screen_frame.pack(fill='x', padx=4, pady=2)
        else:
            self._comp_fg_screen_frame.pack_forget()
            self._comp_fg_path_frame.pack(fill='x', padx=4, pady=2)
            # ラベルをモードに合わせて変更
            label = "出力フォルダ:" if mode == "davintelop" else "Photoshop PNG パス:"
            self._comp_fg_path_var.set(
                self._resolve_out_dir.get() if mode == "davintelop"
                else self._comp_fg_path_var.get())

    def _browse_fg_path(self):
        mode = self._comp_fg_mode_var.get()
        if mode == "davintelop":
            d = filedialog.askdirectory(title="DavinTelop 出力フォルダを選択",
                                        initialdir=self._comp_fg_path_var.get())
            if d:
                self._comp_fg_path_var.set(d)
        else:
            p = filedialog.askopenfilename(
                title="Photoshop 出力 PNG を選択",
                filetypes=[("PNG", "*.png"), ("すべて", "*")])
            if p:
                self._comp_fg_path_var.set(p)

    # ── 背景キャプチャ ──────────────────────────────

    def _refresh_monitors(self):
        """モニター一覧をドロップダウンに反映する。"""
        if not HAS_CAPTURE:
            return
        try:
            from core.capture import list_monitors
            self._comp_monitors = list_monitors()
        except Exception:
            self._comp_monitors = []
        names = [m["name"] for m in self._comp_monitors]
        self._comp_bg_monitor_combo['values'] = names
        if names and not self._comp_bg_monitor_var.get():
            # デフォルトは個別モニターがあれば最初の個別、なければ全体
            idx = 1 if len(self._comp_monitors) > 1 else 0
            self._comp_bg_monitor_combo.current(idx)
            self._on_monitor_selected()

    def _on_monitor_selected(self):
        """選択モニター全体を背景キャプチャ範囲に設定する。"""
        idx = self._comp_bg_monitor_combo.current()
        if idx < 0 or idx >= len(self._comp_monitors):
            return
        m = self._comp_monitors[idx]
        self._comp_bg_region = (m["left"], m["top"], m["width"], m["height"])
        if hasattr(self, '_comp_bg_region_lbl'):
            self._comp_bg_region_lbl.set(f"  {m['name']}")
        if self._comp_bg_thread and self._comp_bg_thread.is_alive():
            self._comp_bg_thread.set_region(*self._comp_bg_region)

    def _select_bg_region(self):
        self.withdraw()
        self.after(200, self._open_bg_selector)

    def _open_bg_selector(self):
        RegionSelectorWindow(self, self._on_bg_region_selected)

    def _on_bg_region_selected(self, x, y, w, h):
        self._comp_bg_region = (x, y, w, h)
        self._comp_bg_region_lbl.set(f"  ({x}, {y})  {w}×{h}")
        self.deiconify()
        if self._comp_bg_thread and self._comp_bg_thread.is_alive():
            self._comp_bg_thread.set_region(x, y, w, h)

    def _start_bg_capture(self):
        if not HAS_CAPTURE:
            messagebox.showerror("エラー", "capture モジュールが読み込めません。")
            return
        self._stop_bg_capture()
        self._comp_bg_queue = queue.Queue()
        mode = self._comp_bg_mode_var.get()

        if mode == "ndi":
            # ── NDI モード ────────────────────────────
            from core.capture import NDIReceiverThread, HAS_NDI
            if not HAS_NDI:
                messagebox.showerror(
                    "NDI 未対応",
                    "NDI Tools をインストールして DavinTelop を再起動してください。\n"
                    "https://ndi.video/tools/")
                return
            src_name = self._comp_ndi_src_var.get().strip()
            t = NDIReceiverThread(self._comp_bg_queue, source_name=src_name)
            t.start()
            self._comp_bg_thread = t
            self._comp_bg_status_var.set(
                f"NDI 接続中: {src_name or '(自動検出)'}")

        elif mode == "device":
            # ── 映像キャプチャデバイス (Blackmagic等) ────
            from core.capture import CaptureDeviceThread, HAS_CV2
            if not HAS_CV2:
                messagebox.showerror(
                    "OpenCV 未対応",
                    "映像キャプチャには OpenCV が必要です。\n"
                    "pip install opencv-python を実行してください。")
                return
            idx = self._comp_dev_combo.current()
            if idx < 0:
                messagebox.showwarning("デバイス未選択",
                    "キャプチャデバイスを選択してください。\n"
                    "（🔄 でデバイス一覧を更新できます）")
                return
            t = CaptureDeviceThread(self._comp_bg_queue, device_index=idx, fps=30.0)
            t.start()
            self._comp_bg_thread = t
            dev_name = self._comp_devices[idx] if idx < len(self._comp_devices) else f"#{idx}"
            self._comp_bg_status_var.set(f"キャプチャ開始: {dev_name}")

        elif mode == "telocut":
            # ── TeloCut（連携で今の絵を受け取る）─────────
            from core.capture import TeloCutFrameThread
            try:
                width = int(self._comp_tc_width_var.get() or 960)
            except ValueError:
                width = 960
            t = TeloCutFrameThread(self._comp_bg_queue, fps=self._comp_tc_fps_var.get(), width=width,
                                   telops=bool(self._comp_tc_telops_var.get()))
            t.start()
            self._comp_bg_thread = t
            self._comp_bg_status_var.set("TeloCut に接続中…")

        else:
            # ── スクリーンキャプチャモード ─────────────
            if not self._comp_bg_region:
                messagebox.showwarning("範囲未設定",
                    "「範囲選択」で映像エリアを指定してください。\n"
                    "(Premiere Pro または Resolve のプレビューウィンドウをドラッグ)")
                return
            t = ScreenCaptureThread(self._comp_bg_queue,
                                    fps=self._comp_bg_fps_var.get())
            t.set_region(*self._comp_bg_region)
            t.start()
            self._comp_bg_thread = t
            app = self._comp_bg_app_var.get()
            self._comp_bg_status_var.set(
                f"{'Premiere Pro' if app == 'premiere' else 'Resolve'} キャプチャ中...")

    def _stop_bg_capture(self):
        if self._comp_bg_thread:
            self._comp_bg_thread.stop()
            self._comp_bg_thread = None
        self._comp_bg_status_var.set("停止中")
        self._comp_bg_frame = None
        self._redraw_composite()
        self._schedule_program_redraw()

    def _on_tc_option_change(self, *_):
        """TeloCut の大きさ・FPS・テロップを入れるかを、動いている間にも反映する。"""
        from core.capture import TeloCutFrameThread
        t = self._comp_bg_thread
        if not isinstance(t, TeloCutFrameThread):
            return
        try:
            t.telops = bool(self._comp_tc_telops_var.get())
            t.width = int(self._comp_tc_width_var.get() or 960)
            t.fps = max(1.0, min(30.0, float(self._comp_tc_fps_var.get())))
        except (ValueError, tk.TclError):
            pass

    def _toggle_program_telocut(self):
        """ツールバーの「🎬 TeloCut を背景に」: TeloCut の今の絵（テロップ抜き）を Program に敷く / やめる。"""
        from core.capture import TeloCutFrameThread
        if self._pv_tc_var.get():
            self._comp_bg_mode_var.set("telocut")
            self._on_bg_mode_change()
            self._comp_tc_telops_var.set(False)   # DavinTelop のテロップと二重にしない
            self._pv_show_bg.set(True)
            self._start_bg_capture()
        elif isinstance(self._comp_bg_thread, TeloCutFrameThread):
            self._stop_bg_capture()

    # ── テロップソース ──────────────────────────────

    def _select_fg_region(self):
        self.withdraw()
        self.after(200, self._open_fg_selector)

    def _open_fg_selector(self):
        RegionSelectorWindow(self, self._on_fg_region_selected)

    def _on_fg_region_selected(self, x, y, w, h):
        self._comp_fg_region = (x, y, w, h)
        self._comp_fg_region_lbl.set(f"  ({x}, {y})  {w}×{h}")
        self.deiconify()
        if self._comp_fg_thread and self._comp_fg_thread.is_alive():
            self._comp_fg_thread.set_region(x, y, w, h)

    def _start_fg_source(self):
        if not HAS_CAPTURE:
            messagebox.showerror("エラー", "capture モジュールが読み込めません。")
            return
        self._stop_fg_source()
        mode = self._comp_fg_mode_var.get()
        self._comp_fg_queue = queue.Queue()

        if mode == "davintelop":
            path = self._comp_fg_path_var.get().strip()
            if not path or not Path(path).is_dir():
                messagebox.showwarning("フォルダ未設定",
                    "DavinTelop 出力フォルダを「参照」で選択してください。")
                return
            t = DirWatchThread(self._comp_fg_queue)
            t.set_dir(path)
            t.start()
            self._comp_fg_thread = t
            self._comp_fg_status_var.set(f"フォルダ監視中: {Path(path).name}")

        elif mode == "photoshop":
            path = self._comp_fg_path_var.get().strip()
            if not path:
                messagebox.showwarning("ファイル未設定",
                    "Photoshop の出力 PNG ファイルを「参照」で選択してください。")
                return
            t = FileWatchThread(self._comp_fg_queue)
            t.set_path(path)
            t.start()
            self._comp_fg_thread = t
            self._comp_fg_status_var.set(f"ファイル監視中: {Path(path).name}")

        else:  # screen
            if not self._comp_fg_region:
                messagebox.showwarning("範囲未設定",
                    "「範囲選択」でテロップ領域を指定してください。")
                return
            t = ScreenCaptureThread(self._comp_fg_queue, fps=15)
            t.set_region(*self._comp_fg_region)
            t.start()
            self._comp_fg_thread = t
            self._comp_fg_status_var.set("スクリーンキャプチャ中...")

    def _stop_fg_source(self):
        if self._comp_fg_thread:
            self._comp_fg_thread.stop()
            self._comp_fg_thread = None
        self._comp_fg_status_var.set("停止中")
        self._comp_fg_frame = None
        self._redraw_composite()

    # ── キューポーリング ────────────────────────────

    def _poll_composite_queues(self):
        """50ms ごとにキューを処理してプレビューを更新する。"""
        updated = False

        # 背景キュー
        try:
            while True:
                tag, data = self._comp_bg_queue.get_nowait()
                if tag == "frame":
                    self._comp_bg_frame = data
                    updated = True
                    self._schedule_program_redraw()
                elif tag == "error":
                    self._comp_bg_status_var.set(f"エラー: {data[:40]}")
                elif tag == "status":
                    self._comp_bg_status_var.set(data)
        except queue.Empty:
            pass

        # テロップキュー
        try:
            while True:
                tag, *rest = self._comp_fg_queue.get_nowait()
                if tag == "frame":
                    self._comp_fg_frame = rest[0]
                    updated = True
                elif tag == "dir_changed":
                    # DirWatchThread: 最初のPNGをロードしてプレビュー
                    paths = rest[0]
                    if paths:
                        try:
                            if PILLOW_OK:
                                from PIL import Image as _PIL_Image
                                self._comp_fg_frame = _PIL_Image.open(paths[0]).convert("RGBA")
                                updated = True
                                self._comp_fg_status_var.set(
                                    f"更新検出: {Path(paths[0]).name}")
                        except Exception:
                            pass
                elif tag == "error":
                    self._comp_fg_status_var.set(f"エラー: {rest[0][:40]}")
                elif tag == "status":
                    self._comp_fg_status_var.set(rest[0])
        except queue.Empty:
            pass

        if updated:
            self._redraw_composite()

        # 再スケジュール
        self.after(50, self._poll_composite_queues)

    # ── 合成描画 ──────────────────────────────────

    def _redraw_composite(self):
        if not PILLOW_OK:
            return
        if not hasattr(self, '_comp_canvas'):
            return

        canvas = self._comp_canvas
        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        if cw < 10 or ch < 10:
            return

        canvas.delete('all')

        bg = self._comp_bg_frame
        fg = self._comp_fg_frame

        if bg is None and fg is None:
            canvas.create_text(cw // 2, ch // 2, fill='#666666',
                font=('', 11), justify='center',
                text="背景・テロップソースを設定して\n「▶ 開始」をクリックしてください")
            return

        try:
            from PIL import Image as _PIL, ImageTk as _ITk

            # 背景が無い場合はチェッカーで代替
            if bg is None:
                if fg is not None:
                    W, H = fg.size
                    bg = _make_checker_bg(W, H)
                    bg = bg.convert("RGBA")
                else:
                    return
            else:
                bg = bg.copy().convert("RGBA")

            # テロップ合成
            if fg is not None:
                fg_work = fg.copy().convert("RGBA")
                # サイズが違う場合は bg に合わせてリサイズ
                if fg_work.size != bg.size:
                    fg_work = fg_work.resize(bg.size, _PIL.LANCZOS)
                # 不透明度適用
                opacity = self._comp_opacity_var.get() / 100.0
                if opacity < 1.0:
                    r, g, b, a = fg_work.split()
                    a = a.point(lambda p: int(p * opacity))
                    fg_work = _PIL.merge("RGBA", (r, g, b, a))
                bg = _PIL.alpha_composite(bg, fg_work)

            # NDI 出力（合成済みフル解像度を配信）
            sender = getattr(self, '_ndi_sender', None)
            if sender is not None and sender.active:
                sender.send_pil(bg)

            # キャンバスにフィット
            W, H = bg.size
            scale = min(cw / W, ch / H)
            pw, ph = int(W * scale), int(H * scale)
            thumb = bg.resize((pw, ph), _PIL.LANCZOS)
            photo = _ITk.PhotoImage(thumb)
            self._comp_photo_ref = photo
            ox, oy = (cw - pw) // 2, (ch - ph) // 2
            canvas.create_image(ox, oy, anchor='nw', image=photo)

            # 情報ラベル
            info = f"{W}×{H}"
            canvas.create_text(ox + 5, oy + 5, text=info,
                anchor='nw', fill='white', font=('', 9))
            canvas.create_text(ox + 4, oy + 4, text=info,
                anchor='nw', fill='#333', font=('', 9))

        except Exception as ex:
            canvas.create_text(cw // 2, ch // 2,
                text=f"合成エラー:\n{ex}", fill='red', justify='center')

    # ── NDI 出力 ──────────────────────────────────

    def _toggle_ndi_output(self):
        """合成映像の NDI 出力を開始/停止する。"""
        if self._comp_ndiout_var.get():
            from core.capture import NDISender, HAS_NDI
            if not HAS_NDI:
                self._comp_ndiout_var.set(False)
                self._open_ndi_install_dialog()
                return
            name = self._comp_ndiout_name.get().strip() or "DavinTelop"
            self._ndi_sender = NDISender(name)
            ok, err = self._ndi_sender.start()
            if ok:
                self._comp_ndiout_status.set(f"● 配信中: {name}")
                self._redraw_composite()   # 即時1フレーム送出
            else:
                self._ndi_sender = None
                self._comp_ndiout_var.set(False)
                self._comp_ndiout_status.set("✗ 開始失敗")
                messagebox.showerror("NDI出力", err, parent=self)
        else:
            if self._ndi_sender is not None:
                self._ndi_sender.stop()
                self._ndi_sender = None
            self._comp_ndiout_status.set("停止")

    # ── 合成画像の保存 ──────────────────────────────

    def _export_composite(self):
        if not PILLOW_OK:
            messagebox.showerror("エラー", "Pillow が必要です。")
            return
        bg = self._comp_bg_frame
        fg = self._comp_fg_frame
        if bg is None and fg is None:
            messagebox.showwarning("保存エラー", "プレビュー画像がありません。")
            return

        from PIL import Image as _PIL
        from datetime import datetime

        path = filedialog.asksaveasfilename(
            title="合成画像を保存",
            defaultextension=".png",
            initialfile=f"composite_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg")])
        if not path:
            return
        try:
            if bg is None and fg is not None:
                W, H = fg.size
                bg = _make_checker_bg(W, H).convert("RGBA")
            else:
                bg = bg.copy().convert("RGBA")

            if fg is not None:
                fg_work = fg.copy().convert("RGBA")
                if fg_work.size != bg.size:
                    fg_work = fg_work.resize(bg.size, _PIL.LANCZOS)
                opacity = self._comp_opacity_var.get() / 100.0
                if opacity < 1.0:
                    r, g, b, a = fg_work.split()
                    a = a.point(lambda p: int(p * opacity))
                    fg_work = _PIL.merge("RGBA", (r, g, b, a))
                bg = _PIL.alpha_composite(bg, fg_work)

            bg.save(path)
            self._progress_var.set(f"合成画像を保存: {Path(path).name}")
        except Exception as ex:
            messagebox.showerror("保存エラー", str(ex))

    def _show_ps_guide(self):
        PsGuideDialog(self)

    # ─────────────────────────────────────────────

    def _refresh_template_list(self):
        # テンプレートウィンドウが開いていないときは何もしない
        if not hasattr(self, '_template_lb'):
            return
        try:
            self._template_lb.delete(0, 'end')
        except tk.TclError:
            return
        for name in self.preset_manager.list_presets():
            self._template_lb.insert('end', name)

    def _on_template_select(self, event=None):
        self._schedule_template_pv_update()

    def _schedule_template_pv_update(self):
        win = getattr(self, '_template_win', None)
        if win is None or not win.winfo_exists():
            return
        if not hasattr(self, '_template_pv_canvas'):
            return
        if not self._template_pv_pending:
            self._template_pv_pending = True
            self.after(120, self._do_template_pv_update)

    def _do_template_pv_update(self):
        self._template_pv_pending = False
        if not PILLOW_OK:
            return
        win = getattr(self, '_template_win', None)
        if win is None or not win.winfo_exists() or not hasattr(self, '_template_pv_canvas'):
            return
        try:
            if not self._template_pv_canvas.winfo_exists():
                return
        except tk.TclError:
            return
        canvas = self._template_pv_canvas
        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        if cw < 10 or ch < 10:
            return

        sel = self._template_lb.curselection()
        if not sel:
            canvas.delete('all')
            canvas.create_text(cw // 2, ch // 2,
                text="テンプレートを選択してください", fill='white', font=('', 10))
            return

        name = self._template_lb.get(sel[0])
        try:
            design = self.preset_manager.load_preset(name)
        except Exception:
            return

        from core.renderer import render_telop_image
        try:
            img  = render_telop_image("サンプルテキスト\n2行目のテキスト", design)
            W, H = design.canvas_width, design.canvas_height
            bg   = _make_checker_bg(W, H)
            bg.paste(img, (0, 0), img)
            scale = min(cw / W, ch / H)
            pw, ph = int(W * scale), int(H * scale)
            thumb  = bg.resize((pw, ph), Image.LANCZOS)
            photo  = ImageTk.PhotoImage(thumb)
            self._template_pv_photo_ref = photo
            canvas.delete('all')
            ox, oy = (cw - pw) // 2, (ch - ph) // 2
            canvas.create_image(ox, oy, anchor='nw', image=photo)
        except Exception as ex:
            canvas.delete('all')
            canvas.create_text(cw // 2, ch // 2,
                text=f"エラー:\n{ex}", fill='red', justify='center')

    def _template_save(self):
        name = simpledialog.askstring("テンプレート保存", "テンプレート名を入力してください:", parent=self)
        if not name:
            return
        self.preset_manager.save_preset(name.strip(), self.design)
        self._refresh_preset_menu()
        self._refresh_template_list()
        self._progress_var.set(f"テンプレート「{name}」を保存しました")

    def _template_load(self):
        sel = self._template_lb.curselection()
        if not sel:
            messagebox.showwarning("テンプレート読込", "テンプレートを選択してください。", parent=self)
            return
        name = self._template_lb.get(sel[0])
        try:
            s = self.preset_manager.load_preset(name)
            self.design = s
            self._apply_design_to_ui(s)
            self._schedule_preview_update()
            self._progress_var.set(f"テンプレート「{name}」を読み込みました")
        except Exception as ex:
            messagebox.showerror("エラー", f"テンプレートの読み込みに失敗しました:\n{ex}", parent=self)

    def _template_delete(self):
        sel = self._template_lb.curselection()
        if not sel:
            messagebox.showwarning("テンプレート削除", "テンプレートを選択してください。", parent=self)
            return
        name = self._template_lb.get(sel[0])
        if not messagebox.askyesno("確認", f"テンプレート「{name}」を削除しますか？", parent=self):
            return
        try:
            self.preset_manager.delete_preset(name)
            self._refresh_preset_menu()
            self._refresh_template_list()
            self._progress_var.set(f"テンプレート「{name}」を削除しました")
        except Exception as ex:
            messagebox.showerror("エラー", f"削除に失敗しました:\n{ex}", parent=self)

    def _toggle_theme_btn(self):
        self._toggle_theme()
        self._theme_btn.configure(
            text="🌙" if not self._is_dark.get() else "☀️")

    # ─── CSV インポート ───────────────────────────

    def import_csv(self):
        path = filedialog.askopenfilename(
            title="CSVファイルを開く",
            filetypes=[("CSV ファイル", "*.csv"), ("テキストファイル", "*.txt"), ("すべて", "*")],
        )
        if not path:
            return

        encoding = self._encoding_var.get()
        delimiter = self._delim_var.get()
        if delimiter == "\\t":
            delimiter = "\t"

        try:
            if encoding == "auto":
                last_err = None
                for enc in ("utf-8-sig", "cp932", "euc_jp", "utf-16"):
                    try:
                        entries = import_csv(path, encoding=enc, delimiter=delimiter,
                                             start_id=self._next_id)
                        encoding = enc
                        break
                    except (UnicodeDecodeError, UnicodeError) as ex:
                        last_err = ex
                else:
                    raise last_err or UnicodeDecodeError
            else:
                entries = import_csv(path, encoding=encoding, delimiter=delimiter,
                                     start_id=self._next_id)
            if not entries:
                messagebox.showwarning("CSVインポート", "テロップデータが見つかりませんでした。\nヘッダ行と列構成を確認してください。")
                return

            self.telop_list.extend(entries)
            self._next_id = max(e.id for e in self.telop_list) + 1
            self._refresh_tree()
            self._progress_var.set(f"{len(entries)} 件のテロップを読み込みました")
        except UnicodeDecodeError:
            messagebox.showerror("文字コードエラー", f"文字コード「{encoding}」で読み込めませんでした。\n別の文字コードを試してください。")
        except Exception as ex:
            messagebox.showerror("エラー", f"CSVの読み込みに失敗しました:\n{ex}")

    # ─── 直接入力 ────────────────────────────────

    def _qa_on_start_change(self, *_):
        if not self._qa_updating_tc:
            self._qa_updating_tc = True
            self._qa_update_dur()
            self._qa_updating_tc = False

    def _qa_on_end_change(self, *_):
        if not self._qa_updating_tc:
            self._qa_updating_tc = True
            self._qa_update_dur()
            self._qa_updating_tc = False

    def _qa_on_dur_change(self, *_):
        if not self._qa_updating_tc:
            self._qa_updating_tc = True
            self._qa_update_end()
            self._qa_updating_tc = False

    def _qa_update_dur(self):
        try:
            fps = self._fps_var.get()
            start_s = parse_timecode(self._qa_start_var.get().strip(), fps)
            end_s   = parse_timecode(self._qa_end_var.get().strip(), fps)
            self._qa_dur_var.set(f"{max(0.0, end_s - start_s):.3f}")
        except Exception:
            pass

    def _qa_update_end(self):
        try:
            fps = self._fps_var.get()
            start_s = parse_timecode(self._qa_start_var.get().strip(), fps)
            dur = float(self._qa_dur_var.get().strip())
            self._qa_end_var.set(seconds_to_timecode(start_s + max(0.0, dur), fps))
        except Exception:
            pass

    def _quick_add(self):
        start = self._qa_start_var.get().strip()
        end   = self._qa_end_var.get().strip()
        text  = self._qa_text_var.get().strip()
        if not text:
            self._qa_text_entry.focus_set()
            return
        try:
            fps = self._fps_var.get()
            parse_timecode(start, fps)
            parse_timecode(end, fps)
        except ValueError as ex:
            messagebox.showwarning("入力エラー", f"タイムコードが不正です:\n{ex}")
            return

        entry = TelopEntry(id=self._next_id, start_time=start, end_time=end, text=text)
        self._next_id += 1
        self.telop_list.append(entry)
        self._refresh_tree()
        self._select_last()

        # 次の入力へ: 開始TCを今の終了TCに、テキストをクリア
        # ※ start を set すると trace で尺が 0 にリセットされるため、
        #   先に尺を保存しておき set 後に復元する。
        prev_dur = self._qa_dur_var.get()
        self._qa_start_var.set(end)   # この時点で end も end に揃い、尺=0 になる
        self._qa_dur_var.set(prev_dur)  # 尺を復元 → end TC が再計算される
        self._qa_text_var.set("")
        self._qa_text_entry.focus_set()

    def _quick_telop_popup(self):
        """ショートカットキーで呼び出す新規テロップ入力（add_entry と同じ）。"""
        self.add_entry()

    # ─── 新規テロップの TC 自動計算 ─────────────────

    def _current_fps(self) -> float:
        try:
            return float(self._fps_var.get()) or 29.97
        except (tk.TclError, ValueError, AttributeError):
            return 29.97

    def _get_playhead_tc(self) -> Optional[str]:
        """Resolve の再生ヘッド TC を返す（未接続なら接続を試みる）。失敗時 None。"""
        try:
            tc = self._resolve.get_current_timecode()
            if not tc and self._resolve.connect():
                tc = self._resolve.get_current_timecode()
            return tc or None
        except Exception:
            return None

    def _latest_end_entry(self) -> Optional[TelopEntry]:
        """終了TCが最も遅いテロップ。"""
        fps = self._current_fps()
        best, best_s = None, -1.0
        for e in self.telop_list:
            try:
                s = parse_timecode(e.end_time, fps)
            except ValueError:
                continue
            if s > best_s:
                best, best_s = e, s
        return best

    def _next_new_telop_tc(self, after: Optional[TelopEntry] = None) -> tuple[str, str]:
        """新規テロップの (開始TC, 終了TC) を設定に従って求める。"""
        fps = self._current_fps()
        cfg = self._new_telop_cfg
        gap = int(cfg.get("gap_frames", 0) or 0)
        mode = cfg.get("start_mode", "auto")
        start = None

        if mode == "playhead" and after is None:
            tc = self._get_playhead_tc()
            if tc:
                try:
                    start = normalize_timecode(tc, fps)
                except ValueError:
                    start = None
        if start is None and mode == "qa" and after is None:
            try:
                start = normalize_timecode(self._qa_start_var.get(), fps)
            except ValueError:
                start = None
        if start is None:
            base = None
            if after is not None:
                base = after.end_time
            elif mode == "auto" and self._get_selected_entries():
                base = self._get_selected_entries()[-1].end_time
            else:
                last = self._latest_end_entry()
                base = last.end_time if last else None
            try:
                if base:
                    start = shift_timecode(base, gap, fps)
                else:
                    start = normalize_timecode(self._qa_start_var.get() or "00:00:00:00", fps)
            except ValueError:
                start = "00:00:00:00"

        dur = float(cfg.get("default_dur", 3.0) or 3.0)
        end = seconds_to_timecode(parse_timecode(start, fps) + dur, fps)
        return start, end

    def _dialog_keys(self) -> tuple[str, str]:
        return (self._shortcuts.get("dlg_submit") or "Return",
                self._shortcuts.get("dlg_newline") or "")

    # ─── テロップリスト操作 ──────────────────────

    def add_entry(self, after: Optional[TelopEntry] = None, start_tc: Optional[str] = None):
        """新規テロップ作成ダイアログ。開始/終了TCは自動反映、Enterで作成。"""
        fps = self._current_fps()
        if after is None and self._new_telop_cfg.get("start_mode", "auto") == "auto":
            sel = self._get_selected_entries()
            after = sel[-1] if sel else None
        start, end = self._next_new_telop_tc(after)
        if start_tc:
            start = start_tc
            end = seconds_to_timecode(parse_timecode(start, fps)
                                      + float(self._new_telop_cfg.get("default_dur", 3.0)), fps)
            after = None
            # 開始TCが前後するテロップの間に挿入
            for i, e in enumerate(self.telop_list):
                try:
                    if parse_timecode(e.start_time, fps) <= parse_timecode(start, fps):
                        after = e
                except ValueError:
                    pass
        submit_key, newline_key = self._dialog_keys()
        state = {"anchor": after}

        def on_submit(result: dict):
            entry = TelopEntry(id=self._next_id, **result)
            self._next_id += 1
            anchor = state["anchor"]
            if anchor is not None and anchor in self.telop_list:
                self.telop_list.insert(self.telop_list.index(anchor) + 1, entry)
                state["anchor"] = entry
            else:
                self.telop_list.append(entry)
            self._refresh_tree()
            self._select_entries([entry])
            # 直接入力欄も次の入力位置に追従
            gap = int(self._new_telop_cfg.get("gap_frames", 0) or 0)
            nxt = shift_timecode(result["end_time"], gap, fps)
            prev_dur = self._qa_dur_var.get()
            self._qa_start_var.set(nxt)
            self._qa_dur_var.set(prev_dur)
            self._progress_var.set(f"テロップを追加しました: {entry.text[:30]}")
            return (nxt, "")

        dlg = TelopEditDialog(
            self, design=self._collect_design(), start_tc=start, end_tc=end,
            submit_key=submit_key, newline_key=newline_key,
            on_submit=on_submit, continuous=bool(self._new_telop_cfg.get("continuous")),
            get_playhead=self._get_playhead_tc,
            chars_per_sec=self._new_telop_cfg.get("cps", 6.0),
            min_duration=self._new_telop_cfg.get("min_dur", 1.0))
        cont = bool(dlg.continuous_var.get())
        if cont != bool(self._new_telop_cfg.get("continuous")):
            self._new_telop_cfg["continuous"] = cont
            self._save_app_config()

    def insert_after_selected(self):
        sel = self._get_selected_entries()
        self.add_entry(after=sel[-1] if sel else None)

    def edit_selected(self):
        sel = self._get_selected_entries()
        if not sel:
            return
        entry = sel[0]
        submit_key, newline_key = self._dialog_keys()
        dlg = TelopEditDialog(
            self, entry, design=self._collect_design().with_overrides(entry.style),
            submit_key=submit_key, newline_key=newline_key,
            get_playhead=self._get_playhead_tc,
            chars_per_sec=self._new_telop_cfg.get("cps", 6.0),
            min_duration=self._new_telop_cfg.get("min_dur", 1.0))
        if dlg.result:
            for k, v in dlg.result.items():
                setattr(entry, k, v)
            self._refresh_tree()
            self.show_telop_preview(entry)

    def duplicate_selected(self):
        import dataclasses
        new_entries = []
        for entry in self._get_selected_entries():
            new_entry = dataclasses.replace(entry, id=self._next_id, style=dict(entry.style))
            self._next_id += 1
            idx = self.telop_list.index(entry)
            self.telop_list.insert(idx + 1, new_entry)
            new_entries.append(new_entry)
        self._refresh_tree()
        self._select_entries(new_entries)

    def delete_selected(self):
        sel = self._get_selected_entries()
        if not sel:
            return
        if len(sel) > 1:
            if not messagebox.askyesno("確認", f"{len(sel)} 件のテロップを削除しますか？\n（Ctrl+Z で元に戻せます）"):
                return
        # 削除後は直後の行を選択して連続操作しやすくする
        idx = self.telop_list.index(sel[-1])
        for entry in sel:
            self.telop_list.remove(entry)
        self._refresh_tree()
        if self.telop_list:
            self._select_entries([self.telop_list[min(idx - len(sel) + 1, len(self.telop_list) - 1)]])

    def delete_disabled(self):
        dis = [e for e in self.telop_list if not e.enabled]
        if not dis:
            self._progress_var.set("無効なテロップはありません")
            return
        if not messagebox.askyesno("確認", f"無効なテロップ {len(dis)} 件を削除しますか？"):
            return
        self.telop_list = [e for e in self.telop_list if e.enabled]
        self._refresh_tree()

    def select_all(self):
        self._tree.selection_set(self._tree.get_children())

    def toggle_enabled(self):
        for entry in self._get_selected_entries():
            entry.enabled = not entry.enabled
        self._refresh_tree()

    def move_up(self):
        sel = self._get_selected_entries()
        for entry in sel:
            idx = self.telop_list.index(entry)
            if idx > 0 and self.telop_list[idx - 1] not in sel:
                self.telop_list[idx - 1], self.telop_list[idx] = \
                    self.telop_list[idx], self.telop_list[idx - 1]
        self._refresh_tree()

    def move_down(self):
        sel = self._get_selected_entries()
        for entry in reversed(sel):
            idx = self.telop_list.index(entry)
            if idx < len(self.telop_list) - 1 and self.telop_list[idx + 1] not in sel:
                self.telop_list[idx], self.telop_list[idx + 1] = \
                    self.telop_list[idx + 1], self.telop_list[idx]
        self._refresh_tree()

    def sort_by_tc(self):
        fps = self._current_fps()

        def key(e):
            try:
                return (0, parse_timecode(e.start_time, fps))
            except ValueError:
                return (1, 0.0)
        self.telop_list.sort(key=key)   # 安定ソート
        self._refresh_tree()
        self._progress_var.set("開始TC順に並べ替えました")

    # ─── 一括編集 ────────────────────────────────

    def _targets(self) -> list[TelopEntry]:
        """選択があれば選択、なければ全テロップ。"""
        return self._get_selected_entries() or list(self.telop_list)

    def shift_tc_dialog(self):
        targets = self._targets()
        if not targets:
            return
        scope = "選択中" if self._get_selected_entries() else "全"
        s = simpledialog.askstring(
            "TCを一括シフト",
            f"{scope} {len(targets)} 件の開始・終了TCをずらします。\n\n"
            "例:  +2 (秒)  /  -0.5 (秒)  /  +15f (フレーム)  /  -00:00:01:00",
            parent=self)
        if not s:
            return
        fps = self._current_fps()
        t = s.strip().replace(' ', '')
        sign = -1 if t.startswith('-') else 1
        body = t.lstrip('+-')
        try:
            if body.lower().endswith('f'):
                delta = sign * int(body[:-1])
            elif ':' in body:
                delta = sign * timecode_to_frames(body, fps)
            else:
                delta = sign * int(round(float(body) * fps))
        except ValueError:
            messagebox.showwarning("入力エラー", f"シフト量を解釈できません: {s}", parent=self)
            return
        clamped = 0
        for e in targets:
            try:
                if timecode_to_frames(e.start_time, fps) + delta < 0:
                    clamped += 1
                e.start_time = shift_timecode(e.start_time, delta, fps)
                e.end_time = shift_timecode(e.end_time, delta, fps)
            except ValueError:
                continue
        self._refresh_tree()
        msg = f"{len(targets)} 件を {delta:+d} フレームシフトしました"
        if clamped:
            msg += f"（{clamped} 件は 00:00:00:00 で頭打ち）"
        self._progress_var.set(msg)

    def merge_selected(self):
        sel = self._get_selected_entries()
        if len(sel) < 2:
            self._progress_var.set("結合するには 2 件以上選択してください")
            return
        fps = self._current_fps()
        try:
            starts = [parse_timecode(e.start_time, fps) for e in sel]
            ends = [parse_timecode(e.end_time, fps) for e in sel]
        except ValueError:
            messagebox.showwarning("結合", "不正なタイムコードを含むため結合できません。", parent=self)
            return
        first = sel[0]
        first.text = "\n".join(e.text for e in sel)
        first.start_time = seconds_to_timecode(min(starts), fps)
        first.end_time = seconds_to_timecode(max(ends), fps)
        for e in sel[1:]:
            self.telop_list.remove(e)
        self._refresh_tree()
        self._select_entries([first])
        self._progress_var.set(f"{len(sel)} 件を結合しました")

    def split_selected(self):
        """改行を含むテロップを行ごとに分割（尺は文字数比で按分）。"""
        fps = self._current_fps()
        made = 0
        for entry in list(self._get_selected_entries()):
            lines = [l for l in entry.text.split('\n') if l.strip()]
            if len(lines) < 2:
                continue
            try:
                sf = timecode_to_frames(entry.start_time, fps)
                ef = timecode_to_frames(entry.end_time, fps)
            except ValueError:
                continue
            total = max(len(lines), ef - sf)
            weights = [max(1, len(l)) for l in lines]
            wsum = sum(weights)
            idx = self.telop_list.index(entry)
            cur = sf
            acc = 0
            from core.timecode import frames_to_timecode
            for i, (line, w) in enumerate(zip(lines, weights)):
                acc += w
                nxt = ef if i == len(lines) - 1 else sf + round(total * acc / wsum)
                if i == 0:
                    entry.text, entry.start_time = line, frames_to_timecode(cur, fps)
                    entry.end_time = frames_to_timecode(nxt, fps)
                else:
                    import dataclasses
                    ne = dataclasses.replace(entry, id=self._next_id, text=line,
                                             style=dict(entry.style),
                                             start_time=frames_to_timecode(cur, fps),
                                             end_time=frames_to_timecode(nxt, fps))
                    self._next_id += 1
                    self.telop_list.insert(idx + i, ne)
                cur = nxt
            made += 1
        if not made:
            self._progress_var.set("改行を含むテロップを選択してください")
            return
        self._refresh_tree()
        self._progress_var.set(f"{made} 件のテロップを分割しました")

    def fill_gaps(self):
        """TC順で各テロップの終了を次テロップの開始(−間隔)まで延長する。"""
        fps = self._current_fps()
        gap = int(self._new_telop_cfg.get("gap_frames", 0) or 0)
        items = []
        for e in self._targets():
            try:
                items.append((timecode_to_frames(e.start_time, fps), e))
            except ValueError:
                continue
        items.sort(key=lambda t: t[0])
        changed = 0
        for (sf, e), (nsf, _ne) in zip(items, items[1:]):
            try:
                ef = timecode_to_frames(e.end_time, fps)
            except ValueError:
                continue
            target = nsf - gap
            if target > ef and target > sf:
                from core.timecode import frames_to_timecode
                e.end_time = frames_to_timecode(target, fps)
                changed += 1
        self._refresh_tree()
        self._progress_var.set(f"{changed} 件の終了TCを延長しました")

    def recalc_duration_by_chars(self):
        targets = self._targets()
        if not targets:
            return
        cps = float(self._new_telop_cfg.get("cps", 6.0) or 6.0)
        mn = float(self._new_telop_cfg.get("min_dur", 1.0) or 0.0)
        if not messagebox.askyesno(
                "尺を文字数から再計算",
                f"{len(targets)} 件の終了TCを「{cps:g} 文字/秒・最小 {mn:g} 秒」で再計算します。\n"
                "よろしいですか？（Ctrl+Z で元に戻せます）", parent=self):
            return
        fps = self._current_fps()
        for e in targets:
            try:
                st = parse_timecode(e.start_time, fps)
            except ValueError:
                continue
            n = len(e.text.replace('\n', '').strip())
            e.end_time = seconds_to_timecode(st + max(mn, n / cps), fps)
        self._refresh_tree()

    def open_find_replace(self):
        dlg = getattr(self, '_find_dlg', None)
        try:
            if dlg is not None and dlg.winfo_exists():
                dlg.lift()
                dlg._find_entry.focus_set()
                return
        except tk.TclError:
            pass
        self._find_dlg = FindReplaceDialog(self)

    def find_replace_apply(self, find: str, repl: str, use_regex: bool,
                           selected_only: bool, case: bool) -> int:
        import re as _re
        if not find:
            return 0
        targets = self._get_selected_entries() if selected_only else list(self.telop_list)
        flags = 0 if case else _re.IGNORECASE
        try:
            pat = _re.compile(find if use_regex else _re.escape(find), flags)
        except _re.error as ex:
            messagebox.showwarning("正規表現エラー", str(ex))
            return -1
        total = 0
        for e in targets:
            new, n = pat.subn(repl.replace('\\n', '\n') if not use_regex else repl, e.text)
            if n:
                e.text = new
                total += n
        if total:
            self._refresh_tree()
        return total

    def find_matches(self, find: str, use_regex: bool, case: bool) -> list[TelopEntry]:
        import re as _re
        if not find:
            return []
        try:
            pat = _re.compile(find if use_regex else _re.escape(find), 0 if case else _re.IGNORECASE)
        except _re.error:
            return []
        return [e for e in self.telop_list if pat.search(e.text)]

    # ─── クリップボード ─────────────────────────

    def copy_selected(self):
        import csv as _csv, io
        sel = self._get_selected_entries()
        if not sel:
            return
        buf = io.StringIO()
        w = _csv.writer(buf, delimiter='\t', lineterminator='\n')
        for e in sel:
            w.writerow([e.start_time, e.end_time, e.text])
        self.clipboard_clear()
        self.clipboard_append(buf.getvalue())
        self._progress_var.set(f"{len(sel)} 件をコピーしました（TSV）")

    def paste_entries(self):
        """TSV（開始/終了/テキスト）またはテキスト行を貼り付け。
        テキストのみの場合は1行=1テロップとして既定の尺で TC を自動割当。"""
        import csv as _csv, io
        try:
            data = self.clipboard_get()
        except tk.TclError:
            return
        data = data.replace('\r\n', '\n').strip('\n')
        if not data.strip():
            return
        fps = self._current_fps()
        new_entries: list[TelopEntry] = []
        rows = list(_csv.reader(io.StringIO(data), delimiter='\t'))
        is_tsv = rows and all(len(r) >= 3 for r in rows)
        if is_tsv:
            for r in rows:
                try:
                    st = normalize_timecode(r[0], fps)
                    en = normalize_timecode(r[1], fps)
                except ValueError:
                    continue
                new_entries.append(TelopEntry(id=0, start_time=st, end_time=en,
                                              text=r[2].replace('\\n', '\n')))
        if not new_entries:
            lines = [l.strip() for l in data.split('\n') if l.strip()]
            if len(lines) > 200 and not messagebox.askyesno(
                    "貼り付け", f"{len(lines)} 行を貼り付けます。よろしいですか？", parent=self):
                return
            sel = self._get_selected_entries()
            start, _ = self._next_new_telop_tc(sel[-1] if sel else None)
            gap = int(self._new_telop_cfg.get("gap_frames", 0) or 0)
            cps = float(self._new_telop_cfg.get("cps", 6.0) or 6.0)
            mn = float(self._new_telop_cfg.get("min_dur", 1.0) or 0.0)
            for line in lines:
                dur = max(mn, len(line) / cps)
                end = seconds_to_timecode(parse_timecode(start, fps) + dur, fps)
                new_entries.append(TelopEntry(id=0, start_time=start, end_time=end, text=line))
                start = shift_timecode(end, gap, fps)
        if not new_entries:
            return
        for e in new_entries:
            e.id = self._next_id
            self._next_id += 1
        sel = self._get_selected_entries()
        if sel:
            idx = self.telop_list.index(sel[-1]) + 1
            self.telop_list[idx:idx] = new_entries
        else:
            self.telop_list.extend(new_entries)
        self._refresh_tree()
        self._select_entries(new_entries)
        self._progress_var.set(f"{len(new_entries)} 件を貼り付けました")

    # ─── Resolve 再生ヘッド移動 ─────────────────

    def jump_to_selected(self):
        sel = self._get_selected_entries()
        if not sel:
            return
        if not self._resolve.get_current_timecode() and not self._resolve.connect():
            messagebox.showerror("接続エラー", self._resolve.error, parent=self)
            return
        if self._resolve.set_current_timecode(sel[0].start_time):
            self._progress_var.set(f"▶ 再生ヘッドを {sel[0].start_time} へ移動しました")
        else:
            self._progress_var.set("⚠ 再生ヘッドを移動できませんでした（Resolve 18 以降が必要）")

    # ─── タイムコードチェック ────────────────────

    def _validate_entries(self) -> dict:
        """id -> 'error' | 'overlap' の問題マップを返す。"""
        fps = self._current_fps()
        issues: dict = {}
        spans = []
        for e in self.telop_list:
            try:
                s = parse_timecode(e.start_time, fps)
                t = parse_timecode(e.end_time, fps)
            except ValueError:
                issues[e.id] = 'error'
                continue
            if t <= s:
                issues[e.id] = 'error'
                continue
            if e.enabled:
                spans.append((s, t, e))
        spans.sort(key=lambda x: x[0])
        max_end, max_e = -1.0, None
        for s, t, e in spans:
            # 同じトラック指定同士のみ重なり扱い（話者別トラックは除外）
            if max_e is not None and s < max_end - 1e-6 and \
                    (e.track or 0) == (max_e.track or 0):
                issues.setdefault(e.id, 'overlap')
                issues.setdefault(max_e.id, 'overlap')
            if t > max_end:
                max_end, max_e = t, e
        return issues

    def check_timecodes(self):
        issues = self._validate_entries()
        if not issues:
            messagebox.showinfo("タイムコードチェック",
                                "問題は見つかりませんでした。", parent=self)
            return
        errs = [e for e in self.telop_list if issues.get(e.id) == 'error']
        ovs = [e for e in self.telop_list if issues.get(e.id) == 'overlap']
        lines = []
        if errs:
            lines.append(f"■ 不正なTC / 終了≦開始: {len(errs)} 件")
            lines += [f"   No.{e.id}  {e.start_time} → {e.end_time}  {e.text[:16]}" for e in errs[:15]]
        if ovs:
            lines.append(f"■ 時間が重なっている: {len(ovs)} 件")
            lines += [f"   No.{e.id}  {e.start_time} → {e.end_time}  {e.text[:16]}" for e in ovs[:15]]
        self._filter_var.set("")
        self._select_entries(errs + ovs)
        messagebox.showwarning("タイムコードチェック",
                               "\n".join(lines) + "\n\n該当テロップを選択しました。", parent=self)

    # ─── ツリー表示 ──────────────────────────────

    def _snapshot(self) -> tuple:
        return (tuple(tuple(sorted(e.to_dict().items())) for e in self.telop_list),
                self._next_id)

    def _capture_undo(self):
        """前回スナップショットから変化していれば Undo スタックに積む。"""
        snap = self._snapshot()
        if self._last_snapshot is not None and snap != self._last_snapshot:
            self._undo_stack.append(self._last_snapshot)
            if len(self._undo_stack) > self._UNDO_LIMIT:
                del self._undo_stack[0]
            self._redo_stack.clear()
        self._last_snapshot = snap

    def _restore_snapshot(self, snap: tuple):
        rows, next_id = snap
        self.telop_list = [TelopEntry.from_dict(dict(r)) for r in rows]
        self._next_id = next_id
        self._last_snapshot = snap
        self._populate_tree()
        self._mark_dirty()

    def _reset_undo(self):
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._last_snapshot = self._snapshot()

    def _guarded(self, func):
        """テキスト入力中はアプリの Undo 等を発動させない。"""
        w = self.focus_get()
        if isinstance(w, (tk.Entry, tk.Text, ttk.Entry, tk.Spinbox, ttk.Spinbox, ttk.Combobox)):
            return
        func()
        return "break"

    def undo(self):
        if not self._undo_stack:
            self._progress_var.set("元に戻す操作はありません")
            return
        self._redo_stack.append(self._snapshot())
        self._restore_snapshot(self._undo_stack.pop())
        self._progress_var.set(f"元に戻しました（残り {len(self._undo_stack)}）")

    def redo(self):
        if not self._redo_stack:
            self._progress_var.set("やり直す操作はありません")
            return
        self._undo_stack.append(self._snapshot())
        self._restore_snapshot(self._redo_stack.pop())
        self._progress_var.set("やり直しました")

    def _refresh_tree(self):
        """テロップリスト変更後に呼ぶ: Undo 記録・再描画・変更マーク。"""
        self._capture_undo()
        self._populate_tree()
        self._mark_dirty()

    def _populate_tree(self):
        prev_sel = set(self._tree.selection())
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        fps = self._current_fps()
        query = self._filter_var.get().strip().lower() if hasattr(self, '_filter_var') else ""
        issues = self._validate_entries()
        total_s = 0.0
        shown = 0
        for entry in self.telop_list:
            try:
                dur_s = parse_timecode(entry.end_time, fps) - parse_timecode(entry.start_time, fps)
                dur = f"{dur_s:.1f}"
                if entry.enabled and dur_s > 0:
                    total_s += dur_s
            except ValueError:
                dur = "?"
            if query and query not in entry.text.lower() and \
                    query not in entry.note.lower() and query not in entry.speaker.lower() \
                    and query not in entry.start_time:
                continue
            check = "✓" if entry.enabled else "—"
            text_preview = entry.text.replace('\n', ' ⏎ ')[:60]
            chars = len(entry.text.replace('\n', ''))
            tags = []
            if not entry.enabled:
                tags.append('disabled')
            if entry.id in issues:
                tags.append(issues[entry.id])
            self._tree.insert('', 'end', iid=str(entry.id),
                              values=(("🎨" if entry.style else "") + str(entry.id),
                                      entry.start_time, entry.end_time, dur,
                                      text_preview, entry.speaker, chars, check),
                              tags=tuple(tags))
            shown += 1
        keep = [iid for iid in prev_sel if self._tree.exists(iid)]
        if keep:
            self._tree.selection_set(keep)
        count = len(self.telop_list)
        enabled = sum(1 for e in self.telop_list if e.enabled)
        m, sec = divmod(int(total_s), 60)
        status = f"テロップ: {count} 件  (有効: {enabled} 件)  合計尺 {m}:{sec:02d}"
        if query:
            status += f"  ／ 絞り込み: {shown} 件"
        n_err = sum(1 for v in issues.values() if v == 'error')
        n_ov = sum(1 for v in issues.values() if v == 'overlap')
        if n_err or n_ov:
            status += f"  ⚠ 不正 {n_err} / 重なり {n_ov}"
        self._status_var.set(status)
        tl = getattr(self, '_timeline', None)
        if tl is not None:
            tl.redraw()

    def _select_entries(self, entries: list):
        iids = [str(e.id) for e in entries if self._tree.exists(str(e.id))]
        if iids:
            self._tree.selection_set(iids)
            self._tree.focus(iids[-1])
            self._tree.see(iids[-1])

    def _select_last(self):
        children = self._tree.get_children()
        if children:
            self._tree.selection_set(children[-1])
            self._tree.see(children[-1])

    def _on_select(self, event=None):
        self._tc_send_selection()
        sel = self._get_selected_entries()
        self._pv_entry = sel[0] if sel else None
        if sel:
            self._preview_text_var.set(sel[0].text)
            self.show_telop_preview(sel[0])
        self._schedule_preview_update()
        if hasattr(self, '_insp_text'):
            self._insp_load()
        tl = getattr(self, '_timeline', None)
        if tl is not None:
            tl.redraw()

    def _get_selected_entries(self) -> list[TelopEntry]:
        selected_ids = {int(iid) for iid in self._tree.selection()}
        return [e for e in self.telop_list if e.id in selected_ids]

    def _show_context_menu(self, event):
        row = self._tree.identify_row(event.y)
        if row and row not in self._tree.selection():
            self._tree.selection_set(row)
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

    # ─── 加工: テキスト ──────────────────────────

    def _apply_text_op(self, func, label: str):
        """選択（未選択なら全件）のテキストに func を適用。"""
        # 詳細編集中の未確定テキストを先に反映
        if self._insp_text_job:
            self.after_cancel(self._insp_text_job)
            self._insp_commit_text()
        targets = self._targets()
        if not targets:
            return
        changed = 0
        for e in targets:
            try:
                new = func(e.text)
            except Exception:
                continue
            if new and new != e.text:
                e.text = new
                changed += 1
        if changed:
            self._refresh_tree()
            self._insp_load(force=True)
            self._on_select()
        scope = "選択" if self._get_selected_entries() else "全"
        self._progress_var.set(f"{label}: {scope} {len(targets)} 件中 {changed} 件を加工しました"
                               + ("（Ctrl+Z で元に戻せます）" if changed else ""))

    def wrap_by_chars(self):
        from core.textfmt import wrap_text
        n = simpledialog.askinteger("N文字で自動改行", "1行の最大文字数:", parent=self,
                                    initialvalue=self._new_telop_cfg.get("wrap_chars", 16),
                                    minvalue=2, maxvalue=200)
        if not n:
            return
        self._new_telop_cfg["wrap_chars"] = n
        self._apply_text_op(lambda t: wrap_text(t, max_chars=n, max_lines=0), f"{n}文字で改行")

    def add_prefix_suffix(self):
        dlg = _PrefixSuffixDialog(self)
        if dlg.result:
            pre, suf = dlg.result
            self._apply_text_op(lambda t: f"{pre}{t}{suf}", "先頭・末尾に追加")

    def extract_speakers(self):
        from core.textfmt import split_speaker
        n = 0
        for e in self._targets():
            spk, body = split_speaker(e.text)
            if spk and body:
                e.speaker, e.text = spk, body
                n += 1
        if n:
            self._refresh_tree()
            self._insp_load(force=True)
        self._progress_var.set(f"{n} 件の話者名を分離しました")

    # ─── 加工: タイミング ─────────────────────────

    def set_duration_bulk(self):
        targets = self._targets()
        if not targets:
            return
        dur = simpledialog.askfloat("尺を一括設定", f"{len(targets)} 件の尺（秒）:", parent=self,
                                    initialvalue=self._new_telop_cfg.get("default_dur", 3.0),
                                    minvalue=0.04, maxvalue=3600)
        if not dur:
            return
        fps = self._current_fps()
        for e in targets:
            try:
                e.end_time = seconds_to_timecode(parse_timecode(e.start_time, fps) + dur, fps)
            except ValueError:
                continue
        self._refresh_tree()
        self._insp_load(force=True)
        self._progress_var.set(f"{len(targets)} 件の尺を {dur:g} 秒にしました")

    def ensure_min_duration(self):
        targets = self._targets()
        mn = simpledialog.askfloat("最小の尺を確保", "これより短いテロップを延長（秒）:",
                                   parent=self, initialvalue=self._new_telop_cfg.get("min_dur", 1.0),
                                   minvalue=0.04, maxvalue=600)
        if not mn:
            return
        fps = self._current_fps()
        n = 0
        for e in targets:
            try:
                s = parse_timecode(e.start_time, fps)
                t = parse_timecode(e.end_time, fps)
            except ValueError:
                continue
            if t - s < mn:
                e.end_time = seconds_to_timecode(s + mn, fps)
                n += 1
        self._refresh_tree()
        self._progress_var.set(f"{n} 件の尺を {mn:g} 秒に延長しました")

    def arrange_sequential(self):
        """リスト順に、先頭の開始TCから尺を保ったまま隙間なく並べ直す。"""
        targets = self._targets()
        if len(targets) < 2:
            self._progress_var.set("2 件以上のテロップが必要です")
            return
        gap = simpledialog.askinteger(
            "隙間なく連続配置",
            f"{len(targets)} 件をリスト順に並べ直します（先頭の開始TCを基準・尺は維持）。\n\n"
            "テロップ間の間隔（フレーム）:", parent=self,
            initialvalue=int(self._new_telop_cfg.get("gap_frames", 0) or 0), minvalue=0, maxvalue=600)
        if gap is None:
            return
        fps = self._current_fps()
        from core.timecode import frames_to_timecode
        try:
            cur = timecode_to_frames(targets[0].start_time, fps)
        except ValueError:
            return
        for e in targets:
            try:
                dur = timecode_to_frames(e.end_time, fps) - timecode_to_frames(e.start_time, fps)
            except ValueError:
                continue
            dur = max(1, dur)
            e.start_time = frames_to_timecode(cur, fps)
            e.end_time = frames_to_timecode(cur + dur, fps)
            cur += dur + gap
        self._refresh_tree()
        self._insp_load(force=True)
        self._progress_var.set(f"{len(targets)} 件を連続配置しました")

    def set_in_out(self, which: str):
        """選択テロップの開始(I)/終了(O)を Resolve の再生ヘッドに合わせる。"""
        sel = self._get_selected_entries()
        if not sel:
            return
        tc = self._get_playhead_tc()
        if not tc:
            self._progress_var.set("⚠ Resolve の再生ヘッドを取得できませんでした")
            return
        fps = self._current_fps()
        try:
            ph = normalize_timecode(tc, fps)
        except ValueError:
            return
        e = sel[0]
        try:
            s = parse_timecode(e.start_time, fps)
            t = parse_timecode(e.end_time, fps)
        except ValueError:
            s = t = 0.0
        p = parse_timecode(ph, fps)
        if which == 'in':
            e.start_time = ph
            if t <= p:   # 尺を保って終了も移動
                e.end_time = seconds_to_timecode(p + max(t - s, 0.5), fps)
        else:
            if p <= s:
                self._progress_var.set("⚠ 再生ヘッドが開始TCより前です")
                return
            e.end_time = ph
        self._refresh_tree()
        self._insp_load(force=True)
        self._progress_var.set(f"{'開始' if which == 'in' else '終了'}を {ph} にしました")

    def nudge_selected(self, frames: Optional[int], secs: int = 0):
        sel = self._get_selected_entries()
        if not sel:
            return
        fps = self._current_fps()
        delta = frames if frames is not None else secs * _nominal_fps(fps)
        for e in sel:
            try:
                if timecode_to_frames(e.start_time, fps) + delta < 0:
                    return
            except ValueError:
                return
        for e in sel:
            e.start_time = shift_timecode(e.start_time, delta, fps)
            e.end_time = shift_timecode(e.end_time, delta, fps)
        self._refresh_tree()
        self._insp_load(force=True)

    # ─── 加工: スタイル・話者 ─────────────────────

    def copy_style(self):
        sel = self._get_selected_entries()
        if not sel:
            return
        self._style_clipboard = dict(sel[0].style or {})
        self._progress_var.set("個別スタイルをコピーしました" if self._style_clipboard
                               else "（共通デザイン）をコピーしました")

    def paste_style(self):
        clip = getattr(self, '_style_clipboard', None)
        sel = self._get_selected_entries()
        if clip is None or not sel:
            self._progress_var.set("先に個別スタイルをコピーしてください")
            return
        for e in sel:
            e.style = dict(clip)
        self._refresh_tree()
        self._insp_load(force=True)
        self._schedule_preview_update()
        self._progress_var.set(f"{len(sel)} 件にスタイルを貼り付けました")

    def clear_style(self):
        sel = self._targets()
        n = sum(1 for e in sel if e.style)
        for e in sel:
            e.style = {}
        self._refresh_tree()
        self._insp_load(force=True)
        self._schedule_preview_update()
        self._progress_var.set(f"{n} 件の個別スタイルを解除しました")

    def set_speaker_bulk(self):
        targets = self._targets()
        if not targets:
            return
        name = simpledialog.askstring("話者を一括設定",
                                      f"{len(targets)} 件の話者名（空欄で解除）:", parent=self)
        if name is None:
            return
        for e in targets:
            e.speaker = name.strip()
        self._refresh_tree()
        self._insp_load(force=True)

    def add_entry_at(self, tc: str):
        """指定TC（タイムラインのダブルクリック位置）に新規テロップ。"""
        self.add_entry(start_tc=tc)

    # ─── ファイル入出力（追加） ───────────────────

    def import_srt_file(self):
        path = filedialog.askopenfilename(
            title="SRTファイルを開く",
            filetypes=[("SRT 字幕", "*.srt"), ("すべて", "*")])
        if not path:
            return
        try:
            entries = import_srt(path, fps=self._current_fps(), start_id=self._next_id)
        except Exception as ex:
            messagebox.showerror("エラー", f"SRTの読み込みに失敗しました:\n{ex}")
            return
        if not entries:
            messagebox.showwarning("SRTインポート", "字幕が見つかりませんでした。")
            return
        self.telop_list.extend(entries)
        self._next_id = max(e.id for e in self.telop_list) + 1
        self._refresh_tree()
        self._progress_var.set(f"SRT から {len(entries)} 件を読み込みました")

    def export_csv_file(self):
        if not self.telop_list:
            messagebox.showwarning("警告", "テロップがありません。")
            return
        path = filedialog.asksaveasfilename(
            title="CSV を保存", defaultextension=".csv",
            initialfile="telops.csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            export_csv(self.telop_list, path)
            self._progress_var.set(f"CSV を書き出しました: {Path(path).name}")
        except Exception as ex:
            messagebox.showerror("エラー", f"CSV の書き出しに失敗しました:\n{ex}")

    # ─── テロップ作成設定 ───────────────────────

    def open_new_telop_settings(self):
        dlg = NewTelopSettingsDialog(self, self._new_telop_cfg)
        if dlg.result:
            self._new_telop_cfg.update(dlg.result)
            self._save_app_config()
            self._progress_var.set("テロップ作成設定を保存しました")

    # ─── デザイン設定 ─────────────────────────────

    def _collect_design(self) -> DesignSettings:
        """UI変数からDesignSettingsを生成。"""
        s = DesignSettings()

        # フォント (パスかフォント名かを判別)
        font_val = self._font_var.get()
        if font_val and Path(font_val).exists():
            s.font_path = font_val
        else:
            # フォント名からパスを逆引き
            for name, path in self._get_fonts_cached():
                if name == font_val:
                    s.font_path = path
                    break
            else:
                s.font_path = font_val

        try:
            s.font_size    = self._font_size_var.get()
            s.letter_spacing = self._kern_var.get()
            s.line_spacing = self._linespace_var.get()
        except tk.TclError:
            pass

        s.font_color      = self._font_color_btn.get()
        s.stroke_enabled  = self._stroke_enabled_var.get()
        s.stroke_color    = self._stroke_color_btn.get()
        try:
            s.stroke_width = self._stroke_w_var.get()
        except tk.TclError:
            pass

        s.shadow_enabled  = self._shadow_enabled_var.get()
        s.shadow_color    = self._shadow_color_btn.get()
        try:
            s.shadow_opacity  = self._shadow_opacity_var.get()
            s.shadow_offset_x = self._shadow_ox_var.get()
            s.shadow_offset_y = self._shadow_oy_var.get()
        except tk.TclError:
            pass

        s.bg_enabled = self._bg_enabled_var.get()
        s.bg_color   = self._bg_color_btn.get()
        try:
            s.bg_opacity = self._bg_opacity_var.get()
            s.bg_padding = self._bg_pad_var.get()
        except tk.TclError:
            pass

        s.position_h = self._pos_h_var.get()
        s.position_v = self._pos_v_var.get()
        s.text_align = self._align_var.get()
        try:
            s.offset_x = self._offset_x_var.get()
            s.offset_y = self._offset_y_var.get()
            s.canvas_width  = self._cw_var.get()
            s.canvas_height = self._ch_var.get()
            s.frame_rate    = self._fps_var.get()
        except tk.TclError:
            pass

        self._collect_extra_design(s)   # 装飾（グラデーション・二重縁・光彩・変形・縦書き）
        return s

    def _apply_design_to_ui(self, s: DesignSettings):
        """DesignSettingsをUIに反映。"""
        # フォント名 or パス
        font_display = s.font_path
        for name, path in self._get_fonts_cached():
            if path == s.font_path:
                font_display = name
                break
        self._font_var.set(font_display)

        self._font_size_var.set(s.font_size)
        self._kern_var.set(s.letter_spacing)
        self._linespace_var.set(s.line_spacing)
        self._font_color_btn.set(s.font_color)
        self._stroke_enabled_var.set(s.stroke_enabled)
        self._stroke_color_btn.set(s.stroke_color)
        self._stroke_w_var.set(s.stroke_width)
        self._shadow_enabled_var.set(s.shadow_enabled)
        self._shadow_color_btn.set(s.shadow_color)
        self._shadow_opacity_var.set(s.shadow_opacity)
        self._shadow_ox_var.set(s.shadow_offset_x)
        self._shadow_oy_var.set(s.shadow_offset_y)
        self._bg_enabled_var.set(s.bg_enabled)
        self._bg_color_btn.set(s.bg_color)
        self._bg_opacity_var.set(s.bg_opacity)
        self._bg_pad_var.set(s.bg_padding)
        self._pos_h_var.set(s.position_h)
        self._pos_v_var.set(s.position_v)
        self._align_var.set(s.text_align)
        self._offset_x_var.set(s.offset_x)
        self._offset_y_var.set(s.offset_y)
        self._cw_var.set(s.canvas_width)
        self._ch_var.set(s.canvas_height)
        self._fps_var.set(s.frame_rate)
        self._apply_extra_design(s)

    def _on_design_change(self, *_):
        self.design = self._collect_design()
        self._schedule_preview_update()

    def _on_fx_toggle(self, key: str, refresh_preview: bool = True):
        """縁取り/シャドウ/背景の有効チェックボックス変更時にグレーアウトを更新。"""
        mapping = {
            'stroke': (self._stroke_enabled_var,  self._stroke_detail),
            'shadow': (self._shadow_enabled_var,   self._shadow_detail),
            'bg':     (self._bg_enabled_var,        self._bg_detail),
        }
        var, detail_frame = mapping[key]
        state = 'normal' if var.get() else 'disabled'
        self._set_children_state(detail_frame, state)
        if refresh_preview:
            self._on_design_change()

    def _on_font_color_change(self, color: str):
        self.design.font_color = color
        self._schedule_preview_update()

    def _on_stroke_color_change(self, color: str):
        self.design.stroke_color = color
        self._schedule_preview_update()

    def _on_shadow_color_change(self, color: str):
        self.design.shadow_color = color
        self._schedule_preview_update()

    def _on_bg_color_change(self, color: str):
        self.design.bg_color = color
        self._schedule_preview_update()

    def _pick_font_file(self):
        path = filedialog.askopenfilename(
            title="フォントファイルを選択",
            filetypes=[("フォント", "*.ttf *.otf *.ttc"), ("すべて", "*")],
        )
        if path:
            self._font_var.set(path)

    def _set_resolution(self, w: int, h: int):
        self._cw_var.set(w)
        self._ch_var.set(h)
        self._on_design_change()

    # ─── プレビュー ──────────────────────────────

    _preview_pending = False

    def _schedule_preview_update(self):
        if not self._preview_pending:
            self._preview_pending = True
            self.after(120, self._update_preview)
        self._schedule_telop_pv_update()

    def _update_preview(self):
        self._preview_pending = False
        if not PILLOW_OK:
            self._preview_canvas.delete('all')
            self._preview_canvas.create_text(
                self._preview_canvas.winfo_width() // 2,
                self._preview_canvas.winfo_height() // 2,
                text="Pillowをインストールしてください\npip install Pillow",
                fill='red', justify='center')
            return

        from core.renderer import render_telop_image

        preview_text = self._preview_text_var.get()
        if not preview_text:
            preview_text = "プレビューテキスト"

        cw = self._preview_canvas.winfo_width()
        ch = self._preview_canvas.winfo_height()
        if cw < 10 or ch < 10:
            return

        try:
            s = self._collect_design()
            pv = getattr(self, '_pv_entry', None)
            if pv is not None and pv.text == preview_text and pv in self.telop_list:
                s = s.with_overrides(pv.style)
            key = (preview_text, repr(sorted(vars(s).items())))
            cache = getattr(self, '_pv_img_cache', None)
            if cache is not None and cache[0] == key:
                img = cache[1]
            else:
                img = render_telop_image(preview_text, s)
                self._pv_img_cache = (key, img)

            # 背景: 合成の背景ソース（TeloCut・NDI など）が動いていればその絵、無ければチェッカーボード（透過確認）
            frame = self._program_bg_frame()
            if frame is not None:
                bg = frame.convert("RGB").resize((s.canvas_width, s.canvas_height), Image.BILINEAR)
            else:
                bg = _make_checker_bg(s.canvas_width, s.canvas_height)
            bg.paste(img, (0, 0), img)

            # プレビューエリアに合わせてリサイズ
            scale = min(cw / s.canvas_width, ch / s.canvas_height)
            pw = int(s.canvas_width * scale)
            ph = int(s.canvas_height * scale)
            thumb = bg.resize((pw, ph), Image.LANCZOS)

            photo = ImageTk.PhotoImage(thumb)
            self._preview_image_ref = photo
            self._preview_canvas.delete('all')
            ox = (cw - pw) // 2
            oy = (ch - ph) // 2
            # ドラッグ計算用に保存
            self._canvas_scale = scale
            self._canvas_ox = ox
            self._canvas_oy = oy
            self._preview_canvas.create_image(ox, oy, anchor='nw', image=photo)
            sg = getattr(self, "_safe_guides", None)
            if sg is not None and sg.get():
                # TFX のようなセーフエリア（アクション 93% / タイトル 90%）の赤い点線
                for k in (0.93, 0.90):
                    mx, my = pw * (1 - k) / 2, ph * (1 - k) / 2
                    self._preview_canvas.create_rectangle(ox + mx, oy + my, ox + pw - mx, oy + ph - my,
                                                          outline='#C03030', dash=(4, 3))

            # 解像度表示 (チェッカー上で見やすい黒テキスト)
            info = f"{s.canvas_width}×{s.canvas_height}  {s.frame_rate}fps"
            self._preview_canvas.create_text(
                ox + 5, oy + 5, text=info,
                anchor='nw', fill='white', font=('', 10))
            self._preview_canvas.create_text(
                ox + 4, oy + 4, text=info,
                anchor='nw', fill='#333333', font=('', 10))
        except Exception as ex:
            self._preview_canvas.delete('all')
            self._preview_canvas.create_text(
                cw // 2, ch // 2, text=f"レンダリングエラー:\n{ex}",
                fill='red', justify='center')

    def _program_bg_frame(self):
        """Program に敷く背景の絵（「映像を背景に」が入っていて、背景ソースが動いているとき）。"""
        var = getattr(self, '_pv_show_bg', None)
        if var is not None and not var.get():
            return None
        if getattr(self, '_comp_bg_thread', None) is None:
            return None
        return getattr(self, '_comp_bg_frame', None)

    def _schedule_program_redraw(self):
        """背景の絵が届いたら Program を描き直す（テロップの描き直しはキャッシュで省く）。"""
        if self._program_bg_frame() is None and not getattr(self, '_pv_had_bg', False):
            return
        self._pv_had_bg = self._program_bg_frame() is not None
        if not self._preview_pending:
            self._preview_pending = True
            self.after(15, self._update_preview)

    # ─── キャンバス 直接入力 / ドラッグ移動 ──────────

    def _canvas_press(self, event):
        """クリックでフォーカスを取得し、ドラッグ開始位置を記録。"""
        self._preview_canvas.focus_set()
        self._drag_start_x = event.x
        self._drag_start_y = event.y
        try:
            self._drag_start_offset_x = self._offset_x_var.get()
            self._drag_start_offset_y = self._offset_y_var.get()
        except tk.TclError:
            self._drag_start_offset_x = 0
            self._drag_start_offset_y = 0

    def _canvas_drag_move(self, event):
        """ドラッグしてテロップ位置 (offset_x / offset_y) を変更。"""
        if self._canvas_scale <= 0:
            return
        dx = event.x - self._drag_start_x
        dy = event.y - self._drag_start_y
        # キャンバスピクセル差をイメージ空間に変換
        img_dx = int(dx / self._canvas_scale)
        img_dy = int(dy / self._canvas_scale)
        self._offset_x_var.set(self._drag_start_offset_x + img_dx)
        self._offset_y_var.set(self._drag_start_offset_y + img_dy)
        # trace で _on_design_change が呼ばれプレビューが更新される

    def _move_telop_step(self, dx: int, dy: int):
        """ボタン押下でテロップをステップ移動。"""
        try:
            step = self._move_step_var.get()
            self._offset_x_var.set(self._offset_x_var.get() + dx * step)
            self._offset_y_var.set(self._offset_y_var.get() + dy * step)
        except tk.TclError:
            pass

    def _center_telop(self):
        """テロップをキャンバス中央にリセット。"""
        self._snap_position('center', 'middle')

    def _snap_position(self, pos_h: str, pos_v: str):
        """テロップを指定の位置アンカーにスナップしオフセットをリセット。"""
        self._pos_h_var.set(pos_h)
        self._pos_v_var.set(pos_v)
        self._offset_x_var.set(0)
        self._offset_y_var.set(0)
        self._on_design_change()

    def _canvas_on_arrow(self, event):
        """矢印キーでテロップを移動。Shift=1px, Ctrl=50px, 通常=移動量設定値。"""
        if event.state & 0x1:       # Shift
            step = 1
        elif event.state & 0x4:     # Ctrl
            step = 50
        else:
            try:
                step = self._move_step_var.get()
            except tk.TclError:
                step = 10

        key = event.keysym
        if key == 'Left':
            self._offset_x_var.set(self._offset_x_var.get() - step)
        elif key == 'Right':
            self._offset_x_var.set(self._offset_x_var.get() + step)
        elif key == 'Up':
            self._offset_y_var.set(self._offset_y_var.get() - step)
        elif key == 'Down':
            self._offset_y_var.set(self._offset_y_var.get() + step)
        return 'break'

    def _canvas_on_key(self, event):
        """印字可能文字をプレビューテキストに追記。"""
        if event.char and event.char.isprintable():
            self._preview_text_var.set(self._preview_text_var.get() + event.char)

    def _canvas_on_backspace(self, event):
        """BackSpaceで末尾1文字を削除。"""
        cur = self._preview_text_var.get()
        if cur:
            self._preview_text_var.set(cur[:-1])

    def _canvas_on_return(self, event):
        """Enterで改行を追加。"""
        self._preview_text_var.set(self._preview_text_var.get() + '\n')

    # ─── フローティングパネル ──────────────────────

    def _toggle_float_panel(self, frame, paned, weight: int, title: str):
        """パネルをフローティングウィンドウにする / ドックに戻す。"""
        fid = id(frame)
        if self._panel_float_state.get(fid, False):
            self._dock_panel(frame, paned, weight)
        else:
            panes = list(paned.panes())
            try:
                idx = panes.index(str(frame))
            except ValueError:
                idx = len(panes)
            self._panel_float_info[fid] = (paned, idx, weight)

            paned.forget(frame)
            frame.tk.call('wm', 'manage', str(frame))
            frame.tk.call('wm', 'title',    str(frame), title)
            frame.tk.call('wm', 'geometry', str(frame), '900x520')

            # ダークモード時は浮いたウィンドウの背景も合わせる
            if HAS_SV_TTK and self._is_dark.get():
                try:
                    frame.configure(bg=_tc("window_bg"))
                except Exception:
                    pass

            cb = frame.register(lambda f=frame, p=paned, w=weight: self._dock_panel(f, p, w))
            frame.tk.call('wm', 'protocol', str(frame), 'WM_DELETE_WINDOW', cb)
            self._panel_float_state[fid] = True

    def _dock_panel(self, frame, paned, weight: int):
        """フローティングウィンドウをペインに戻す。"""
        fid = id(frame)
        try:
            frame.tk.call('wm', 'forget', str(frame))
        except Exception:
            pass
        info = self._panel_float_info.get(fid)
        if info:
            _, idx, w = info
            panes_count = len(list(paned.panes()))
            if 0 <= idx <= panes_count:
                paned.insert(idx, frame, weight=w)
            else:
                paned.add(frame, weight=w)
        else:
            paned.add(frame, weight=weight)
        self._panel_float_state[fid] = False

    def _tearoff_panel(self, which: str):
        """ウィンドウメニューからパネルを切り離す。"""
        if which == "left":
            self._toggle_float_panel(
                self._left_frame, self._main_pane, 1, "テロップリスト")
        elif which == "design":
            self._toggle_float_panel(
                self._design_outer, self._right_pane, 1, "デザイン設定")
        elif which == "preview":
            self._toggle_float_panel(
                self._preview_outer, self._right_pane, 2, "プレビュー")

    def _dock_all_panels(self):
        """すべてのフローティングパネルをドックに戻す。"""
        # PanedWindow 系フローティングを戻す
        for fid, is_floated in list(self._panel_float_state.items()):
            if is_floated:
                info = self._panel_float_info.get(fid)
                if info:
                    paned, _, weight = info
                    # frame を fid から逆引き (Python オブジェクト ID)
                    for frame in [self._left_frame, self._design_outer, self._preview_outer]:
                        if id(frame) == fid:
                            self._dock_panel(frame, paned, weight)
                            break
        # TearableNotebook 系も全部ドックに戻す
        if self._main_nb:
            self._main_nb.dock_all()

    # ─── テロッププレビュー・テンプレート独立ウィンドウ ──────

    def _toggle_telop_preview_window(self):
        """テロッププレビューを独立ウィンドウで開閉する。"""
        win = getattr(self, '_telop_pv_win', None)
        if win and win.winfo_exists():
            win.destroy()
            self._telop_pv_win = None
            return

        win = tk.Toplevel(self)
        win.title("テロッププレビュー")
        win.geometry("680x520")
        win.minsize(400, 300)
        self._telop_pv_win = win

        # ウィンドウ内にパネルを構築
        self._build_telop_preview_pane(win)

        win.protocol("WM_DELETE_WINDOW", lambda: self._close_telop_pv_win())

        # 選択中エントリがあればすぐに表示
        sel = self._get_selected_entries()
        if sel:
            self.show_telop_preview(sel[0])

    def _close_telop_pv_win(self):
        if hasattr(self, '_telop_pv_win') and self._telop_pv_win:
            self._telop_pv_win.destroy()
            self._telop_pv_win = None

    def _toggle_template_window(self):
        """テンプレートを独立ウィンドウで開閉する。"""
        win = getattr(self, '_template_win', None)
        if win and win.winfo_exists():
            win.destroy()
            self._template_win = None
            return

        win = tk.Toplevel(self)
        win.title("テンプレート")
        win.geometry("520x480")
        win.minsize(360, 300)
        self._template_win = win

        self._build_template_pane(win)

        win.protocol("WM_DELETE_WINDOW", lambda: self._close_template_win())
        self._refresh_template_list()

    def _close_template_win(self):
        if hasattr(self, '_template_win') and self._template_win:
            self._template_win.destroy()
            self._template_win = None

    # ─── キーボードショートカット ──────────────────

    def _apply_shortcuts(self):
        """_shortcuts 辞書の内容をウィンドウバインディングに反映。"""
        step = lambda: self._move_step_var.get() if hasattr(self, '_move_step_var') else 10
        actions = {
            "send_to_resolve":       self.send_to_resolve,
            "export_png":            self.export_png,
            "export_srt":            self.export_srt,
            "export_fcpxml":         self.export_fcpxml,
            "export_resolve_script": self.export_resolve_script,
            "place_at_playhead":     self.place_selected_at_playhead,
            "new_telop":             self.add_entry,
            "new_telop_quick":       self._quick_telop_popup,
            "undo":                  lambda: self._guarded(self.undo),
            "redo":                  lambda: self._guarded(self.redo),
            "find_replace":          self.open_find_replace,
            "focus_search":          lambda: (self._filter_entry.focus_set(),
                                              self._filter_entry.select_range(0, 'end')),
            "jump_playhead":         self.jump_to_selected,
            "move_left":             lambda: self._move_telop_step(-1, 0),
            "move_right":            lambda: self._move_telop_step(1,  0),
            "move_up":               lambda: self._move_telop_step(0, -1),
            "move_down":             lambda: self._move_telop_step(0,  1),
            "snap_topleft":          lambda: self._snap_position('left',   'top'),
            "snap_topcenter":        lambda: self._snap_position('center', 'top'),
            "snap_topright":         lambda: self._snap_position('right',  'top'),
            "snap_middleleft":       lambda: self._snap_position('left',   'middle'),
            "snap_middleright":      lambda: self._snap_position('right',  'middle'),
            "snap_bottomleft":       lambda: self._snap_position('left',   'bottom'),
            "snap_bottomcenter":     lambda: self._snap_position('center', 'bottom'),
            "snap_bottomright":      lambda: self._snap_position('right',  'bottom'),
            "snap_center":           lambda: self._snap_position('center', 'middle'),
        }
        # 旧バインディングをクリア
        for key in self._bound_keys:
            try:
                self.unbind(f"<{key}>")
            except Exception:
                pass
        self._bound_keys = []

        for action, binding in self._shortcuts.items():
            if binding and action in actions:
                try:
                    self.bind(f"<{binding}>",
                              lambda e, a=action: (actions[a](), "break")[1])
                    self._bound_keys.append(binding)
                except Exception:
                    pass

    def _open_shortcut_settings(self):
        dlg = ShortcutConfigDialog(
            self, self._shortcuts, self._DEFAULT_SHORTCUTS, self._SHORTCUT_LABELS)
        if dlg.result is not None:
            self._shortcuts = dlg.result
            self._save_app_config()
            self._apply_shortcuts()
            self._progress_var.set("ショートカットを更新しました")

    # ─── プリセット ──────────────────────────────

    def save_preset(self):
        name = simpledialog.askstring("プリセット保存", "プリセット名を入力してください:", parent=self)
        if not name:
            return
        self.preset_manager.save_preset(name.strip(), self.design)
        self._refresh_preset_menu()
        self._refresh_template_list()
        self._progress_var.set(f"プリセット「{name}」を保存しました")

    def load_preset(self, name: str):
        try:
            s = self.preset_manager.load_preset(name)
            self.design = s
            self._apply_design_to_ui(s)
            self._schedule_preview_update()
            self._progress_var.set(f"プリセット「{name}」を読み込みました")
        except Exception as ex:
            messagebox.showerror("エラー", f"プリセットの読み込みに失敗しました:\n{ex}")

    def _refresh_preset_menu(self):
        # 既存の動的エントリを削除 (2番目のセパレータ以降)
        end_idx = self._preset_menu.index('end')
        for i in range(end_idx, 1, -1):
            self._preset_menu.delete(i)

        for name in self.preset_manager.list_presets():
            self._preset_menu.add_command(
                label=name, command=lambda n=name: self.load_preset(n))

    # ─── PNG 書き出し ─────────────────────────────

    def export_png(self):
        if not self.telop_list:
            messagebox.showwarning("PNG書き出し", "テロップがありません。")
            return
        if not PILLOW_OK:
            messagebox.showerror("エラー", "Pillowをインストールしてください:\npip install Pillow")
            return

        # 保存先フィールドを直接使用（毎回ダイアログを出さない）
        output_dir = self._get_save_dir(prompt_if_empty=True)
        if not output_dir:
            return

        self._run_with_progress("PNG書き出し中...", lambda cb: self._do_export_png(output_dir, cb))

    def _get_save_dir(self, prompt_if_empty: bool = False) -> str:
        """テロップ保存先フォルダを返す。未設定時は (任意で) 一度だけ選択させる。"""
        d = self._resolve_out_dir.get().strip()
        if not d and prompt_if_empty:
            d = filedialog.askdirectory(title="テロップ保存先フォルダを選択")
            if d:
                self._resolve_out_dir.set(d)
                self._save_app_config()
        if d:
            try:
                Path(d).mkdir(parents=True, exist_ok=True)
            except Exception as ex:
                messagebox.showerror("保存先エラー",
                    f"保存先フォルダを作成できません:\n{d}\n{ex}")
                return ""
        return d

    def _do_export_png(self, output_dir: str, callback):
        s = self._collect_design()
        inc = self._include_text_in_filename.get()
        paths = export_png(self.telop_list, s, output_dir, callback=callback,
                           include_text_in_filename=inc)
        # 完了はステータスバーに表示（毎回モーダルを出さない）
        self.after(0, lambda: self._progress_var.set(
            f"✓ {len(paths)} 件のPNGを書き出しました → {output_dir}"))

    # ─── SRT 書き出し ─────────────────────────────

    def export_srt(self):
        if not self.telop_list:
            messagebox.showwarning("SRT書き出し", "テロップがありません。")
            return
        path = filedialog.asksaveasfilename(
            title="SRTファイルを保存",
            defaultextension=".srt",
            filetypes=[("SRT", "*.srt"), ("すべて", "*")],
        )
        if not path:
            return
        try:
            fps = self._fps_var.get()
            export_srt(self.telop_list, path, fps=fps)
            self._progress_var.set(f"SRTを書き出しました: {Path(path).name}")
            messagebox.showinfo("SRT書き出し完了", f"SRTを書き出しました:\n{path}")
        except Exception as ex:
            messagebox.showerror("エラー", f"SRTの書き出しに失敗しました:\n{ex}")

    # ─── FCPxml 書き出し ──────────────────────────

    def export_fcpxml(self):
        if not self.telop_list:
            messagebox.showwarning("FCPxml書き出し", "テロップがありません。")
            return

        if not messagebox.askyesno(
            "FCPxml書き出し",
            "FCPxmlにはPNG画像への参照が含まれます。\n"
            "先にPNGを書き出しておくことをお勧めします。\n\n"
            "続行しますか？"
        ):
            return

        png_dir = filedialog.askdirectory(title="PNGがある (または書き出す) フォルダを選択")
        if not png_dir:
            return

        # PNG を書き出し
        s = self._collect_design()
        try:
            png_paths = export_png(self.telop_list, s, png_dir)
        except Exception as ex:
            messagebox.showerror("PNG書き出しエラー", str(ex))
            return

        path = filedialog.asksaveasfilename(
            title="FCPxmlファイルを保存",
            defaultextension=".fcpxml",
            filetypes=[("FCPxml", "*.fcpxml"), ("XML", "*.xml")],
        )
        if not path:
            return

        try:
            export_fcpxml(
                [e for e in self.telop_list if e.enabled],
                png_paths, path, s)
            self._progress_var.set(f"FCPxmlを書き出しました: {Path(path).name}")
            messagebox.showinfo("FCPxml書き出し完了",
                                f"FCPxmlを書き出しました:\n{path}\n\n"
                                "DaVinci Resolve: タイムライン > インポート > タイムライン... から読み込めます。")
        except Exception as ex:
            messagebox.showerror("エラー", f"FCPxmlの書き出しに失敗しました:\n{ex}")

    # ─── AAF 書き出し (Avid Media Composer) ──────

    def export_aaf(self):
        if not self.telop_list:
            messagebox.showwarning("AAF書き出し", "テロップがありません。")
            return
        if not HAS_AAF2:
            messagebox.showerror(
                "AAF書き出し",
                "pyaaf2 がインストールされていません。\n"
                "pip install pyaaf2 を実行してください。")
            return
        if not PILLOW_OK:
            messagebox.showerror("エラー", "Pillowをインストールしてください:\npip install Pillow")
            return

        if not messagebox.askyesno(
            "AAF書き出し (Avid Media Composer)",
            "AAFにはPNG画像への参照が含まれます。\n"
            "PNGを同じフォルダに書き出してから AAF を生成します。\n\n"
            "Avid では「ファイル > 入力 > AAF」または\n"
            "ビンへのドラッグ&ドロップで取り込めます。\n\n"
            "続行しますか？"
        ):
            return

        png_dir = filedialog.askdirectory(title="PNGを書き出すフォルダを選択")
        if not png_dir:
            return

        path = filedialog.asksaveasfilename(
            title="AAFファイルを保存",
            defaultextension=".aaf",
            filetypes=[("AAF", "*.aaf")],
        )
        if not path:
            return

        self._run_with_progress(
            "AAF書き出し中...",
            lambda cb: self._do_export_aaf(png_dir, path, cb))

    def _do_export_aaf(self, png_dir: str, aaf_path: str, callback):
        s   = self._collect_design()
        inc = self._include_text_in_filename.get()
        # PNG を先に書き出し（AAF が参照する）
        png_paths = export_png(self.telop_list, s, png_dir, callback=callback,
                               include_text_in_filename=inc)
        export_aaf(
            [e for e in self.telop_list if e.enabled],
            png_paths, aaf_path, s, callback=callback)

        def _finish():
            self._progress_var.set(f"AAFを書き出しました: {Path(aaf_path).name}")
            messagebox.showinfo(
                "AAF書き出し完了",
                f"AAFを書き出しました:\n{aaf_path}\n\n"
                "【Avid Media Composer での取り込み】\n"
                "1. ファイル > 入力 > AAF を選択\n"
                "   （またはビンへドラッグ&ドロップ）\n"
                "2. 参照されたPNGをインポート/リンク\n"
                "3. シーケンスをタイムラインに配置")
        self.after(0, _finish)

    # ─── VoiceVox 音声生成 ──────────────────────

    def open_transcribe(self):
        """タイムライン/動画の音声を自動字幕起こしするダイアログを開く。"""
        TranscribeDialog(self, self)

    def export_wav_all(self):
        """全テロップの VoiceVox 音声を WAV 一括書き出し。"""
        entries = [e for e in self.telop_list if e.enabled]
        if not entries:
            messagebox.showinfo("WAV書き出し", "有効なテロップがありません。", parent=self)
            return
        WavExportDialog(self, self, entries,
                        title=f"VoiceVox WAV 書き出し（全{len(entries)}件）")

    def export_wav_selected(self):
        """選択テロップの VoiceVox 音声を WAV 書き出し。"""
        entries = self._get_selected_entries()
        if not entries:
            messagebox.showinfo("WAV書き出し", "テロップを選択してください。", parent=self)
            return
        WavExportDialog(self, self, entries,
                        title=f"VoiceVox WAV 書き出し（選択{len(entries)}件）")

    # ─── 字幕の自動整形 ───────────────────────────

    def open_autoformat(self):
        """字幕（テロップ）の自動整形ダイアログ。"""
        if not self.telop_list:
            messagebox.showinfo("自動整形", "テロップがありません。", parent=self)
            return
        dlg = tk.Toplevel(self)
        dlg.title("字幕の自動整形")
        dlg.geometry("420x340")
        dlg.transient(self)
        pad = dict(padx=10, pady=4)

        tgt = tk.StringVar(value="all")
        f0 = ttk.LabelFrame(dlg, text="対象", padding=8); f0.pack(fill='x', **pad)
        ttk.Radiobutton(f0, text="すべてのテロップ", variable=tgt, value="all").pack(anchor='w')
        ttk.Radiobutton(f0, text="選択中のテロップのみ", variable=tgt, value="sel").pack(anchor='w')

        f1 = ttk.LabelFrame(dlg, text="整形オプション", padding=8); f1.pack(fill='x', **pad)
        v_filler = tk.BooleanVar(value=True)
        v_wrap = tk.BooleanVar(value=True)
        v_strip = tk.BooleanVar(value=False)
        ttk.Checkbutton(f1, text="フィラー（えー/あの/まあ 等）を除去",
                        variable=v_filler).pack(anchor='w')
        ttk.Checkbutton(f1, text="話者名プレフィックス（「名前:」）を除去",
                        variable=v_strip).pack(anchor='w')
        wrow = ttk.Frame(f1); wrow.pack(fill='x', pady=(4, 0))
        ttk.Checkbutton(wrow, text="自動改行", variable=v_wrap).pack(side='left')
        ttk.Label(wrow, text="1行最大").pack(side='left', padx=(10, 2))
        v_max = tk.IntVar(value=20)
        ttk.Spinbox(wrow, from_=8, to=40, textvariable=v_max, width=4).pack(side='left')
        ttk.Label(wrow, text="文字 / 最大").pack(side='left', padx=2)
        v_lines = tk.IntVar(value=2)
        ttk.Spinbox(wrow, from_=1, to=4, textvariable=v_lines, width=3).pack(side='left')
        ttk.Label(wrow, text="行").pack(side='left', padx=2)

        def apply():
            import core.textfmt as tf
            targets = (self._get_selected_entries() if tgt.get() == "sel"
                       else self.telop_list)
            if not targets:
                messagebox.showinfo("自動整形", "対象がありません。", parent=dlg)
                return
            n = 0
            for e in targets:
                new = tf.format_text(
                    e.text, remove_filler=v_filler.get(), wrap=v_wrap.get(),
                    max_chars=v_max.get(), max_lines=v_lines.get(),
                    strip_speaker=v_strip.get())
                if new != e.text:
                    e.text = new
                    n += 1
            self._refresh_tree()
            self._schedule_preview_update()
            self._progress_var.set(f"自動整形: {n} 件を更新")
            dlg.destroy()

        bot = ttk.Frame(dlg); bot.pack(fill='x', pady=10)
        ttk.Button(bot, text="整形を適用", command=apply).pack(side='right', padx=8)
        ttk.Button(bot, text="キャンセル", command=dlg.destroy).pack(side='right')
        dlg.grab_set()

    # ─── 話者の色分け・トラック振り分け ───────────────

    _RESOLVE_COLORS = ["", "Orange", "Apricot", "Yellow", "Lime", "Olive",
                       "Green", "Teal", "Navy", "Blue", "Purple", "Violet",
                       "Pink", "Tan", "Beige", "Brown", "Chocolate"]

    def open_speaker_rules(self):
        """話者ごとの色・配置トラックを設定し、テロップに割り当てる。"""
        import core.textfmt as tf
        # 台本から話者を検出
        detected = {}
        for e in self.telop_list:
            spk, _ = tf.split_speaker(e.text)
            if spk:
                detected[spk] = detected.get(spk, 0) + 1
        speakers = list(detected.keys())
        for s in self._speaker_rules:
            if s not in speakers:
                speakers.append(s)
        if not speakers:
            messagebox.showinfo("話者の色分け",
                "話者が検出できませんでした。\n"
                "テロップを「名前: セリフ」の形式にすると検出できます。",
                parent=self)
            return

        dlg = tk.Toplevel(self)
        dlg.title("話者の色分け・トラック振り分け")
        dlg.geometry("480x420")
        dlg.transient(self)
        ttk.Label(dlg, text="話者ごとに 配置トラック と クリップ色 を設定",
                  font=('', 10, 'bold')).pack(anchor='w', padx=12, pady=(10, 2))
        ttk.Label(dlg, text="（トラック 0 = 既定トラック / 色は Resolve のクリップカラー）",
                  foreground='gray').pack(anchor='w', padx=12)

        body = ttk.Frame(dlg, padding=8); body.pack(fill='both', expand=True)
        ttk.Label(body, text="話者", width=16).grid(row=0, column=0, sticky='w')
        ttk.Label(body, text="トラック").grid(row=0, column=1, padx=6)
        ttk.Label(body, text="色").grid(row=0, column=2, padx=6)
        ttk.Label(body, text="件数").grid(row=0, column=3, padx=6)

        rows = {}
        for i, spk in enumerate(speakers, start=1):
            rule = self._speaker_rules.get(spk, {})
            ttk.Label(body, text=spk, width=16).grid(row=i, column=0, sticky='w', pady=2)
            tv = tk.IntVar(value=int(rule.get("track", 0)))
            ttk.Spinbox(body, from_=0, to=20, textvariable=tv, width=4
                        ).grid(row=i, column=1, padx=6)
            cv = tk.StringVar(value=rule.get("color", ""))
            ttk.Combobox(body, textvariable=cv, values=self._RESOLVE_COLORS,
                         state='readonly', width=12).grid(row=i, column=2, padx=6)
            ttk.Label(body, text=str(detected.get(spk, "-"))).grid(row=i, column=3, padx=6)
            rows[spk] = (tv, cv)

        v_strip = tk.BooleanVar(value=True)
        ttk.Checkbutton(dlg, text="割り当て時にテキストから話者名プレフィックスを除去",
                        variable=v_strip).pack(anchor='w', padx=12, pady=(4, 0))

        def apply():
            # ルール保存
            for spk, (tv, cv) in rows.items():
                self._speaker_rules[spk] = {"track": int(tv.get()),
                                            "color": cv.get()}
            self._save_app_config()
            # テロップへ割り当て
            n = 0
            for e in self.telop_list:
                spk, body_text = tf.split_speaker(e.text)
                if spk and spk in self._speaker_rules:
                    r = self._speaker_rules[spk]
                    e.speaker = spk
                    e.track = int(r.get("track", 0))
                    e.color = r.get("color", "")
                    if v_strip.get():
                        e.text = body_text
                    n += 1
            self._refresh_tree()
            self._schedule_preview_update()
            self._progress_var.set(f"話者割り当て: {n} 件")
            messagebox.showinfo("話者の色分け",
                f"{n} 件のテロップに話者設定を割り当てました。\n"
                "Resolve送信時に、指定トラックへ配置・クリップ色が付きます。",
                parent=self)
            dlg.destroy()

        bot = ttk.Frame(dlg); bot.pack(fill='x', pady=10)
        ttk.Button(bot, text="保存して割り当て", command=apply).pack(side='right', padx=8)
        ttk.Button(bot, text="キャンセル", command=dlg.destroy).pack(side='right')
        dlg.grab_set()

    # ─── りぞりぷと (Resolve_Script) 連携 ───────────────

    @staticmethod
    def _find_rizoripute_exe() -> str:
        """りぞりぷと.exe をよくある場所から自動探索。見つからなければ ""。"""
        import glob
        roots = []
        for drive in ("C:", "D:", "E:", "F:", "G:"):
            roots += [drive + "\\", drive + "\\tool", drive + "\\tools",
                      drive + "\\app", drive + "\\apps"]
        roots.append(str(Path.home()))
        roots.append(str(Path.home() / "Downloads"))
        roots.append(str(Path.home() / "Desktop"))
        for root in roots:
            try:
                for pat in ("りぞりぷと.exe", "Resolve_Script*/りぞりぷと.exe",
                            "*/りぞりぷと.exe", "*/*/りぞりぷと.exe"):
                    hits = glob.glob(str(Path(root) / pat))
                    if hits:
                        return hits[0]
            except Exception:
                continue
        return ""

    def set_rizoripute_path(self) -> str:
        """りぞりぷと.exe（またはランチャー）の場所を設定して返す。"""
        init = self._rizoripute_path.get().strip()
        p = filedialog.askopenfilename(
            title="りぞりぷと.exe を選択（Resolve_Script フォルダ内）",
            initialdir=str(Path(init).parent) if init else "",
            filetypes=[("りぞりぷと/実行ファイル", "*.exe"), ("すべて", "*.*")],
            parent=self)
        if p:
            self._rizoripute_path.set(p)
            self._save_app_config()
        return p

    # ─── TeloCut 連携 ─────────────────────────────

    def launch_telocut(self):
        import subprocess
        from core import telocut_link as TL
        if TL.is_running():
            self._progress_var.set("TeloCut は起動しています")
            return
        if getattr(sys, "frozen", False):
            exe = Path(sys.executable).with_name("TeloCut.exe")
            cmd = [str(exe)] if exe.exists() else None
        else:
            cmd = [sys.executable, str(Path(__file__).resolve().parent / "telocut.py")]
        if not cmd:
            messagebox.showerror("TeloCut", "TeloCut.exe が見つかりません（DavinTelop と同じフォルダに置いてください）。",
                                 parent=self)
            return
        subprocess.Popen(cmd)
        self._progress_var.set("TeloCut を起動しました（起動したら自動でリアルタイム連携します）")
        if not self._tc_sync.get():
            self._tc_sync.set(True)
            self._toggle_tc_sync()

    def _telocut_call(self, fn, *args):
        try:
            return fn(*args)
        except Exception as ex:
            messagebox.showerror("TeloCut 連携", str(ex), parent=self)
            return None

    def telocut_pull(self):
        from core import telocut_link as TL
        d = self._telocut_call(TL.get_telops)
        if d is None:
            return
        if self.telop_list and not messagebox.askyesno(
                "TeloCut から取り込み",
                f"TeloCut のシーケンス「{d.get('sequence', '')}」のテロップ {len(d['telops'])} 件で\n"
                "今のテロップ一覧を置き換えます。よろしいですか？（元に戻せます）", parent=self):
            return
        self.telop_list = []
        for t in d["telops"]:
            self.telop_list.append(TelopEntry(id=self._next_id, start_time=t["start_time"], end_time=t["end_time"],
                                              text=t.get("text", ""), style=dict(t.get("style") or {})))
            self._next_id += 1
        if d.get("design"):
            try:
                self.design = DesignSettings.from_dict(d["design"])
                self._apply_design_to_ui(self.design)
            except Exception:
                pass
        self._refresh_tree()
        self._mark_dirty()
        self._progress_var.set(f"TeloCut から {len(self.telop_list)} 件のテロップを取り込みました")

    def telocut_push(self):
        from core import telocut_link as TL
        n = len([e for e in self.telop_list if e.enabled])
        if not messagebox.askyesno("TeloCut に反映",
                                   f"TeloCut の開いているシーケンスのテロップを、この {n} 件で置き換えます。\n"
                                   "（TeloCut 側で 1 回の「元に戻す」で戻せます）\n\n"
                                   "テロップの共通デザインも送りますか？ → はい / いいえ どちらでも反映されます",
                                   parent=self):
            return
        r = self._telocut_call(TL.put_telops, self.telop_list, self._collect_design().to_dict())
        if r is not None:
            self._progress_var.set(f"TeloCut に {n} 件のテロップを反映しました")

    def telocut_seek_selected(self):
        from core import telocut_link as TL
        sel = self._get_selected_entries()
        if sel and self._telocut_call(TL.seek, sel[0].start_time) is not None:
            self._progress_var.set(f"TeloCut の再生ヘッドを {sel[0].start_time} へ移動しました")

    def telocut_place_selected(self):
        from core import telocut_link as TL
        sel = self._get_selected_entries()
        if not sel:
            return
        tc = self._telocut_call(TL.playhead)
        if not tc:
            return
        fps = self._current_fps()
        try:
            dur = parse_timecode(sel[0].end_time, fps) - parse_timecode(sel[0].start_time, fps)
        except ValueError:
            dur = 3.0
        sel[0].start_time = tc
        sel[0].end_time = seconds_to_timecode(parse_timecode(tc, fps) + max(dur, 1.0 / fps), fps)
        self._refresh_tree()
        self._mark_dirty()
        self._progress_var.set(f"No.{sel[0].id} を {tc} に配置しました")

    def _toggle_telocut_follow(self):
        from core import telocut_link as TL
        f = getattr(self, "_telocut_follower", None)
        if f is not None:
            f.stop()
            self._telocut_follower = None
        if self._telocut_follow.get() and not (getattr(self, "_tc_session", None) is not None):
            self._telocut_follower = TL.PlayheadFollower(lambda tc: self.after(0, self._on_telocut_tc, tc))
            self._progress_var.set("TeloCut の再生位置に追従します")

    def _on_telocut_tc(self, tc: str):
        fps = self._current_fps()
        try:
            t = parse_timecode(tc, fps)
        except ValueError:
            return
        for e in self.telop_list:
            try:
                if parse_timecode(e.start_time, fps) <= t < parse_timecode(e.end_time, fps):
                    if self._get_selected_entries()[:1] != [e]:
                        self._tc_remote_sel = {str(e.id)}
                        self._select_entries([e])
                    return
            except ValueError:
                continue

    # ─── TeloCut リアルタイム連携 ─────────────────────
    # TeloCut のブリッジにつなぎっぱなしにして、
    #   受信: sync（テロップ・デザインの全体）/ selection / playhead
    #   送信: 0.3 秒ごとに自分の状態のハッシュを見て、変わっていたら sync_put（0.25 秒落ち着いてから）
    # 反映した状態のハッシュを覚えておくので、同じ内容が行ったり来たりしない。

    def _toggle_tc_sync(self):
        from core import telocut_link as TL
        sess = getattr(self, "_tc_session", None)
        if sess is not None:
            sess.stop()
            self._tc_session = None
            self._tc_gen = getattr(self, "_tc_gen", 0) + 1
        self._save_app_config()
        if not self._tc_sync.get():
            self._tc_set_status(None)
            return
        self._tc_hash = None           # 最後に そろえた 状態のハッシュ
        self._tc_pending = False
        self._tc_changed_at = None
        self._tc_remote_sel = set()
        self._tc_ready = False
        f = getattr(self, "_telocut_follower", None)
        if f is not None:
            f.stop()
            self._telocut_follower = None
        import queue
        self._tc_q = queue.Queue()        # 受信スレッド → 画面のスレッド（Tk は別スレッドから触れない）
        # 古いセッション（切った直後のもの）から遅れて届く通知は無視する（世代番号で見分ける）
        self._tc_gen = getattr(self, "_tc_gen", 0) + 1
        gen, q = self._tc_gen, self._tc_q
        self._tc_session = TL.Session(
            lambda ev: q.put((lambda e=ev: self._tc_gen == gen and self._on_tc_event(e), ())),
            lambda up: q.put((lambda u=up: self._tc_gen == gen and self._on_tc_state(u), ())))
        self._tc_set_status(False)
        self._tc_tick = 0
        self.after(50, self._tc_poll)

    def _tc_set_status(self, connected):
        self._tc_title_suffix = "" if connected is None else (
            "  ［🔗 TeloCut と連携中］" if connected else "  ［TeloCut を待っています…］")
        self._update_title()

    def _on_tc_state(self, up: bool):
        self._tc_set_status(up)
        self._tc_ready = False
        if up:
            self._progress_var.set("TeloCut とつながりました。テロップをそろえています…")
            self._tc_async(lambda: __import__("core.telocut_link", fromlist=["x"]).sync_get(), self._tc_initial)
        else:
            self._progress_var.set("TeloCut との接続が切れました（起動すると自動でつながります）")

    def _tc_async(self, fn, done=None):
        """TeloCut への問い合わせを別スレッドで（画面を止めない）。"""
        import threading

        gen, q = getattr(self, "_tc_gen", 0), self._tc_q

        def work():
            try:
                r = fn()
            except Exception as ex:
                q.put((self._progress_var.set, (f"TeloCut 連携: {ex}",)))
                return
            if done:
                q.put((lambda: self._tc_gen == gen and done(r), ()))
        threading.Thread(target=work, daemon=True).start()

    def _tc_local(self):
        from core import telocut_link as TL
        items = [TL.entry_item(e) for e in self.telop_list]
        try:
            design = self._collect_design().to_dict()
        except Exception:
            design = self.design.to_dict()
        return items, design, TL.state_hash(items, design)

    def _tc_initial(self, remote: dict):
        from core import telocut_link as TL
        items, design, h = self._tc_local()
        if h == remote["hash"]:
            self._tc_hash = h
        elif not remote["items"] and self.telop_list:
            self._tc_push()                         # TeloCut が空 → こちらを送る
        elif not self.telop_list or not self._project_dirty and not self._current_project_path:
            self._tc_apply(remote)                  # こちらが空 → TeloCut に合わせる
        else:
            ans = messagebox.askyesnocancel(
                "TeloCut とリアルタイム連携",
                f"TeloCut（{remote.get('sequence', '')}: {len(remote['items'])} 件）と、"
                f"この DavinTelop（{len(self.telop_list)} 件）のテロップが違います。\n\n"
                "はい　　= TeloCut のテロップに合わせる\n"
                "いいえ　= この DavinTelop のテロップで TeloCut を上書き\n"
                "キャンセル = 連携をやめる", parent=self)
            if ans is None:
                self._tc_sync.set(False)
                self._toggle_tc_sync()
                return
            if ans:
                self._tc_apply(remote)
            else:
                self._tc_push()
        self._tc_ready = True
        self._progress_var.set("🔗 TeloCut とリアルタイム連携中（テロップ・デザイン・選択）")

    def _tc_apply(self, remote: dict):
        """TeloCut の状態を反映（同じテロップは uid で対応づけて、ID・メモ・色はそのまま）。"""
        from core import telocut_link as TL
        matched = TL.match_items(remote["items"], self.telop_list)
        new_list = []
        for it, e in zip(remote["items"], matched):
            if e is None:
                e = TelopEntry(id=self._next_id, start_time=it["start"], end_time=it["end"], text=it["text"])
                self._next_id += 1
            e.uid, e.start_time, e.end_time, e.text = it["uid"], it["start"], it["end"], it["text"]
            e.style, e.speaker, e.enabled, e.track = dict(it.get("style") or {}), it.get("speaker", ""), \
                bool(it.get("enabled", True)), int(it.get("track") or 0)
            new_list.append(e)
        sel = {str(e.id) for e in self._get_selected_entries()}
        self.telop_list = new_list
        # デザイン（キャンバスの大きさ・fps は TeloCut のシーケンスに合わせる）
        cur = self._tc_local()[1]
        want = {**cur, **TL.design_part(remote.get("design"))}
        want.update({"canvas_width": remote.get("width", cur.get("canvas_width")),
                     "canvas_height": remote.get("height", cur.get("canvas_height")),
                     "frame_rate": remote.get("fps", cur.get("frame_rate"))})
        if want != cur:
            self.design = DesignSettings.from_dict(want)
            try:
                self._apply_design_to_ui(self.design)
            except Exception:
                pass
        self._refresh_tree()
        keep = [e for e in self.telop_list if str(e.id) in sel]
        if keep:
            self._tc_remote_sel = {str(e.id) for e in keep}
            self._select_entries(keep)
        self._tc_hash = self._tc_local()[2]

    def _tc_push(self):
        from core import telocut_link as TL
        items, design, h = self._tc_local()
        self._tc_pending = True
        self._tc_changed_at = None

        def done(remote):
            self._tc_pending = False
            # 新しく足したテロップに TeloCut の uid を付ける（重複した uid も直る）
            for it, e in zip(remote["items"], TL.match_items(remote["items"], self.telop_list)):
                if e is not None:
                    e.uid = it["uid"]
            now = self._tc_local()[2]
            if now == remote["hash"]:
                self._tc_hash = now
            elif self._tc_local()[2] == TL.state_hash(items, design):
                # 送った後に手を加えていなければ、TeloCut 側の表記（TC の書き方など）に合わせる
                self._tc_apply(remote)
            else:
                self._tc_hash = h                      # 送った後にまた変わった → 次の確認で送る

        def fail():
            self._tc_pending = False

        import threading

        gen, q = self._tc_gen, self._tc_q

        def work():
            try:
                r = TL.sync_put(items, design)
            except Exception as ex:
                q.put((fail, ()))
                q.put((self._progress_var.set, (f"TeloCut に送れませんでした: {ex}",)))
                return
            q.put((lambda: done(r) if self._tc_gen == gen else fail(), ()))
        threading.Thread(target=work, daemon=True).start()

    def _tc_poll(self):
        if getattr(self, "_tc_session", None) is None or not self._tc_sync.get():
            return
        try:
            self._tc_poll_once()
        except Exception:
            import traceback
            traceback.print_exc()
            try:
                with open(_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write("\n===== TeloCut 連携 =====\n" + traceback.format_exc())
            except Exception:
                pass
        finally:
            self.after(50, self._tc_poll)      # 何があっても次の確認を予約する

    def _tc_poll_once(self):
        import queue
        import time as _t
        while True:                       # 受信したものを画面のスレッドで処理
            try:
                fn, args = self._tc_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:
                import traceback
                traceback.print_exc()
        self._tc_tick += 1
        if self._tc_tick % 6 == 0 and self._tc_session.connected and self._tc_ready and not self._tc_pending:
            h = self._tc_local()[2]
            if h != self._tc_hash:
                if self._tc_changed_at is None:
                    self._tc_changed_at = _t.time()
                elif _t.time() - self._tc_changed_at >= 0.25:     # 打ち込みが落ち着いてから送る
                    self._tc_push()
            else:
                self._tc_changed_at = None

    def _on_tc_event(self, ev: dict):
        kind = ev.get("event")
        if kind == "playhead":
            if self._telocut_follow.get():
                self._on_telocut_tc(ev.get("tc", ""))
            return
        if not self._tc_ready:
            return
        if kind == "sync":
            if self._tc_pending or ev.get("hash") == self._tc_hash:
                return
            if self._tc_changed_at is not None:
                return          # こちらで編集中 → こちらの変更を先に送る（後から TeloCut の結果が届く）
            self._tc_apply(ev)
        elif kind == "selection":
            uids = set(ev.get("uids") or [])
            ents = [e for e in self.telop_list if e.uid in uids]
            if ents and {str(e.id) for e in ents} != set(self._tree.selection()):
                self._tc_remote_sel = {str(e.id) for e in ents}
                self._select_entries(ents)

    def _tc_send_selection(self):
        if getattr(self, "_tc_session", None) is None or not self._tc_session.connected or not self._tc_ready:
            return
        cur = set(self._tree.selection())
        if cur == getattr(self, "_tc_remote_sel", set()):
            return              # TeloCut から来た選択（送り返さない）
        self._tc_remote_sel = set()
        uids = [e.uid for e in self._get_selected_entries() if e.uid]
        if not uids:
            return
        from core import telocut_link as TL
        seek = self._tc_sync_seek.get()
        self._tc_async(lambda: TL.select(uids, seek))

    def launch_rizoripute(self):
        """りぞりぷと(Resolve_Script) のランチャーを起動する。"""
        import subprocess
        path = self._rizoripute_path.get().strip()
        if not path or not Path(path).exists():
            # 自動探索 → だめなら手動選択
            auto = self._find_rizoripute_exe()
            if auto:
                path = auto
                self._rizoripute_path.set(path)
                self._save_app_config()
            else:
                if not messagebox.askyesno(
                    "りぞりぷとが見つかりません",
                    "りぞりぷと.exe の場所が未設定です。\n"
                    "今すぐ場所を指定しますか？\n\n"
                    "（GitHub: nakano000/Resolve_Script から入手し、\n"
                    "　半角英数のフォルダに解凍してください）",
                        parent=self):
                    return
                path = self.set_rizoripute_path()
                if not path:
                    return
        if not Path(path).exists():
            messagebox.showerror("起動エラー",
                f"ファイルが見つかりません:\n{path}", parent=self)
            return
        try:
            subprocess.Popen([path], cwd=str(Path(path).parent))
            self._progress_var.set(f"りぞりぷとを起動: {Path(path).name}")
        except Exception as ex:
            messagebox.showerror("起動エラー",
                f"りぞりぷとの起動に失敗しました:\n{ex}", parent=self)

    def export_lipsync(self):
        LipSyncDialog(
            self,
            telop_list=self.telop_list,
            design=self._collect_design(),
            default_out_dir=self._resolve_out_dir.get(),
        )

    def export_voicevox(self):
        # 埋め込み VoiceVox パネルのタブへ切替えてテロップ一覧を更新
        panel = getattr(self, '_voicevox_panel', None)
        nb    = getattr(self, '_main_nb', None)
        if panel is not None and nb is not None:
            panel.refresh_telops(force=True)
            try:
                nb.select(panel.master)   # VoiceVox タブを選択
            except Exception:
                pass
            # タブが切り離されている場合は前面に
            try:
                if hasattr(nb, '_floated'):
                    for idx, win in nb._floated.items():
                        if win.winfo_exists():
                            win.lift()
            except Exception:
                pass
        else:
            messagebox.showinfo("VoiceVox", "VoiceVox パネルが利用できません。")

    # ─── DaVinci Resolve 連携 ───────────────────

    def test_resolve_connection(self):
        if not self._resolve.connect():
            messagebox.showerror("接続失敗", self._resolve.error)
            return
        info = self._resolve.get_timeline_info()
        msg = (
            "DaVinci Resolveに正常に接続できました。\n\n"
            f"タイムライン: {info.get('name', '?')}\n"
            f"FPS: {info.get('fps', '?')}\n"
            f"解像度: {info.get('width', '?')} × {info.get('height', '?')}"
        )
        messagebox.showinfo("接続成功", msg)

    def _pick_resolve_out_dir(self):
        d = filedialog.askdirectory(title="テロップ保存先フォルダを選択",
                                    initialdir=self._resolve_out_dir.get())
        if d:
            self._resolve_out_dir.set(d)
            self._save_app_config()

    def _open_save_dir(self):
        """保存先フォルダをエクスプローラー/Finder で開く。"""
        d = self._resolve_out_dir.get().strip()
        if not d:
            messagebox.showinfo("保存先", "保存先フォルダが未設定です。\n「📂」で選択してください。")
            return
        p = Path(d)
        if not p.exists():
            if messagebox.askyesno("保存先",
                f"フォルダが存在しません:\n{d}\n\n作成して開きますか？"):
                try:
                    p.mkdir(parents=True, exist_ok=True)
                except Exception as ex:
                    messagebox.showerror("エラー", f"作成に失敗しました:\n{ex}")
                    return
            else:
                return
        import subprocess, platform
        try:
            if platform.system() == "Windows":
                subprocess.Popen(["explorer", str(p)])
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as ex:
            messagebox.showerror("エラー", f"フォルダを開けませんでした:\n{ex}")

    def export_resolve_script(self):
        """PNG + Resolveコンソール用スクリプトを生成してフォルダを開く。(ダイアログなし)"""
        if not self.telop_list:
            messagebox.showwarning("スクリプト生成", "テロップがありません。")
            return
        if not PILLOW_OK:
            messagebox.showerror("エラー", "Pillowをインストールしてください:\npip install Pillow")
            return

        output_dir = self._resolve_out_dir.get().strip()
        if not output_dir:
            messagebox.showwarning("出力先未設定", "出力先フォルダを設定してください。")
            return

        track_index = self._resolve_track.get()
        self._save_app_config()
        self._run_with_progress(
            "スクリプト生成中...",
            lambda cb: self._do_export_resolve_script(output_dir, track_index, cb)
        )

    def _do_export_resolve_script(self, output_dir: str, track_index: int, callback):
        s = self._collect_design()
        inc = self._include_text_in_filename.get()

        # PNG書き出し
        png_paths = export_png(self.telop_list, s, output_dir, callback=callback,
                               include_text_in_filename=inc)

        # スクリプト生成
        script_path = str(Path(output_dir) / "davintelop_resolve.py")
        export_resolve_script(
            self.telop_list, output_dir, script_path, s, track_index=track_index,
            include_text_in_filename=inc)

        def _finish():
            # フォルダをエクスプローラーで開く
            import subprocess, platform
            if platform.system() == "Windows":
                subprocess.Popen(["explorer", output_dir])
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", output_dir])

            messagebox.showinfo(
                "スクリプト生成完了",
                f"PNG {len(png_paths)} 件 + スクリプトを生成しました。\n\n"
                f"📂 {output_dir}\n\n"
                "【Resolveへの取り込み手順】\n"
                "1. DaVinci Resolve を起動し、\n"
                "   タイムラインを開いておく\n"
                "2. Workspace > Console を開く\n"
                "3. davintelop_resolve.py を\n"
                "   コンソールウィンドウへドラッグ&ドロップ\n"
                "4. Enter キーで実行"
            )

        self.after(0, _finish)

    def send_to_resolve(self):
        if not self.telop_list:
            messagebox.showwarning("Resolve連携", "テロップがありません。")
            return
        if not PILLOW_OK:
            messagebox.showerror("エラー", "Pillowをインストールしてください:\npip install Pillow")
            return

        output_dir = self._resolve_out_dir.get().strip()
        if not output_dir:
            messagebox.showwarning("出力先未設定", "出力先フォルダを設定してください。")
            return

        if not self._resolve.connect():
            messagebox.showerror("接続失敗", self._resolve.error)
            return

        info      = self._resolve.get_timeline_info()
        track_idx = self._resolve_track.get()
        fps       = info.get("fps", self.design.frame_rate)
        self._save_app_config()

        self._run_with_progress(
            "Resolve へ送信中...",
            lambda cb: self._do_send_to_resolve(output_dir, track_idx, fps, cb)
        )

    def _do_send_to_resolve(self, png_dir: str, track_idx: int, fps: float, callback):
        s = self._collect_design()
        inc = self._include_text_in_filename.get()
        try:
            png_paths = export_png(self.telop_list, s, png_dir, callback=callback,
                                   include_text_in_filename=inc)
        except Exception as ex:
            self.after(0, lambda ex=ex: messagebox.showerror("PNG書き出しエラー", str(ex)))
            return

        ok = self._resolve.import_pngs_to_timeline(
            self.telop_list, png_paths, fps, track_index=track_idx, callback=callback)

        if ok:
            # 成功時はポップアップなし — ステータスバーに表示
            self.after(0, lambda: self._progress_var.set(
                f"✓ Resolve V{track_idx} にテロップを配置しました"))
        else:
            err = self._resolve.error
            self.after(0, lambda: messagebox.showerror("エラー", err))

    # ─── 再生ヘッド位置に配置 ───────────────────

    def place_selected_at_playhead(self):
        """選択中のテロップ1件を Resolve の現在の再生ヘッド位置に配置する。"""
        sel = self._get_selected_entries()
        if not sel:
            messagebox.showwarning("再生ヘッド配置",
                "テロップリストで配置するテロップを1件選択してください。")
            return
        if len(sel) > 1:
            if not messagebox.askyesno("確認",
                f"{len(sel)} 件選択されています。\n"
                "先頭の1件のみを再生ヘッドに配置します。続行しますか？"):
                return
        entry = sel[0]

        if not PILLOW_OK:
            messagebox.showerror("エラー", "Pillowをインストールしてください:\npip install Pillow")
            return

        output_dir = self._resolve_out_dir.get().strip()
        if not output_dir:
            messagebox.showwarning("出力先未設定", "出力先フォルダを設定してください。")
            return

        if not self._resolve.connect():
            messagebox.showerror("接続失敗", self._resolve.error)
            return

        info      = self._resolve.get_timeline_info()
        track_idx = self._resolve_track.get()
        fps       = info.get("fps", self.design.frame_rate)
        self._save_app_config()

        self._run_with_progress(
            "再生ヘッドに配置中...",
            lambda cb: self._do_place_at_playhead(entry, output_dir, track_idx, fps, cb))

    def _do_place_at_playhead(self, entry, png_dir: str, track_idx: int,
                              fps: float, callback):
        from core.renderer import render_telop_image
        s = self._collect_design()

        # 尺（フレーム数）を計算
        try:
            start_s = parse_timecode(entry.start_time, fps)
            end_s   = parse_timecode(entry.end_time, fps)
        except ValueError:
            self.after(0, lambda: messagebox.showerror(
                "エラー", "テロップのタイムコードが不正です。"))
            return
        dur_frames = max(1, round((end_s - start_s) * fps))

        # 単一PNGを書き出し
        try:
            callback(1, 1, entry.text[:20])
            img = render_telop_image(entry.text, s.with_overrides(entry.style))
            out_dir = Path(png_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            png_path = str(out_dir / f"telop_playhead_{entry.id:04d}.png")
            img.save(png_path, "PNG")
        except Exception as ex:
            self.after(0, lambda e=str(ex): messagebox.showerror("PNG書き出しエラー", e))
            return

        # 再生ヘッド位置を取得（書き戻し・メッセージ用）
        tc = self._resolve.get_current_timecode() or ""

        ok = self._resolve.place_png_at_playhead(
            png_path, dur_frames, fps, track_index=track_idx)

        if ok:
            # テロップTCを再生ヘッド位置に書き戻す（オプション）
            wrote = False
            if self._playhead_writeback.get() and tc:
                try:
                    ph_s   = parse_timecode(tc, fps)
                    new_end = seconds_to_timecode(ph_s + (end_s - start_s), fps)
                    entry.start_time = tc
                    entry.end_time   = new_end
                    wrote = True
                    self.after(0, self._refresh_tree)
                except Exception:
                    pass
            msg = f"✓ 再生ヘッド {tc or '?'} に V{track_idx} へ配置"
            if wrote:
                msg += "（TC更新済み）"
            self.after(0, lambda m=msg: self._progress_var.set(m))
        else:
            err = self._resolve.error
            self.after(0, lambda: messagebox.showerror("エラー", err))

    # ─── プロジェクト保存/読み込み ────────────────

    # ─── プロジェクトファイル (.dtproj) ───────────────

    _PROJECT_EXT = ".dtproj"
    _PROJECT_FILETYPES = [("DavinTelop プロジェクト", "*.dtproj"),
                          ("旧形式 (JSON)", "*.json"), ("すべて", "*.*")]

    def _serialize_project(self) -> dict:
        """現在の編集状態を完全に保存するための辞書を返す。"""
        return {
            "app": "DavinTelop",
            "version": 2,
            "design": self._collect_design().to_dict(),
            "telops": [e.to_dict() for e in self.telop_list],
            "next_id": self._next_id,
            "settings": {
                "resolve_out_dir": self._resolve_out_dir.get(),
                "resolve_track": self._resolve_track.get(),
                "include_text_in_filename": self._include_text_in_filename.get(),
                "playhead_writeback": self._playhead_writeback.get(),
            },
        }

    def _apply_project_data(self, data: dict):
        """保存辞書から編集状態を復元する。"""
        s = DesignSettings.from_dict(data.get("design", {}))
        self.design = s
        self._apply_design_to_ui(s)
        self.telop_list = []
        for td in data.get("telops", []):
            try:
                self.telop_list.append(TelopEntry.from_dict(td))
            except Exception:
                pass
        self._next_id = data.get(
            "next_id", max((e.id for e in self.telop_list), default=0) + 1)
        st = data.get("settings", {})
        try:
            if "resolve_out_dir" in st:
                self._resolve_out_dir.set(st["resolve_out_dir"])
            if "resolve_track" in st:
                self._resolve_track.set(st["resolve_track"])
            if "include_text_in_filename" in st:
                self._include_text_in_filename.set(st["include_text_in_filename"])
            if "playhead_writeback" in st:
                self._playhead_writeback.set(st["playhead_writeback"])
        except Exception:
            pass
        self._populate_tree()
        self._reset_undo()
        self._mark_dirty()
        self._schedule_preview_update()

    def _write_project(self, path: str) -> bool:
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self._serialize_project(), f,
                          ensure_ascii=False, indent=2)
            self._current_project_path = path
            self._project_dirty = False
            self._add_recent_project(path)
            self._clear_autosave()
            self._update_title()
            self._progress_var.set(f"保存しました: {Path(path).name}")
            return True
        except Exception as ex:
            messagebox.showerror("保存エラー",
                                 f"プロジェクトの保存に失敗しました:\n{ex}", parent=self)
            return False

    def save_project(self):
        """上書き保存（パス未確定なら名前を付けて保存）。"""
        if self._current_project_path:
            self._write_project(self._current_project_path)
        else:
            self.save_project_as()

    def save_project_as(self):
        path = filedialog.asksaveasfilename(
            title="名前を付けて保存",
            defaultextension=self._PROJECT_EXT,
            initialfile=(Path(self._current_project_path).name
                         if self._current_project_path else "project.dtproj"),
            filetypes=self._PROJECT_FILETYPES,
        )
        if not path:
            return
        self._write_project(path)

    def load_project(self, path: str = None):
        if not path:
            if not self._confirm_discard():
                return
            path = filedialog.askopenfilename(
                title="プロジェクトを開く",
                filetypes=self._PROJECT_FILETYPES,
            )
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._apply_project_data(data)
            self._current_project_path = path
            self._project_dirty = False
            self._add_recent_project(path)
            self._update_title()
            self._progress_var.set(f"読み込み: {Path(path).name}")
        except Exception as ex:
            messagebox.showerror("エラー",
                                 f"プロジェクトの読み込みに失敗しました:\n{ex}", parent=self)

    def new_project(self):
        if not self._confirm_discard():
            return
        self.telop_list = []
        self._next_id = 1
        self._current_project_path = None
        self._populate_tree()
        self._reset_undo()
        self._schedule_preview_update()
        self._project_dirty = False
        self._update_title()
        self._progress_var.set("新規プロジェクト")

    def _confirm_discard(self) -> bool:
        """未保存変更があれば確認。続行可なら True。"""
        if not self._project_dirty:
            return True
        r = messagebox.askyesnocancel(
            "未保存の変更",
            "保存していない変更があります。保存しますか？", parent=self)
        if r is None:
            return False
        if r:
            self.save_project()
            return not self._project_dirty
        return True

    def _mark_dirty(self, *_):
        if getattr(self, "_suppress_dirty", False):
            return
        if not self._project_dirty:
            self._project_dirty = True
            self._update_title()

    def _enable_dirty_tracking(self):
        """起動直後の初期化が落ち着いたら変更追跡を有効化。"""
        self._suppress_dirty = False
        self._project_dirty = False
        self._update_title()

    def _update_title(self):
        name = (Path(self._current_project_path).name
                if self._current_project_path else "無題")
        mark = "*" if self._project_dirty else ""
        self.title(f"DavinTelop — {mark}{name}  (テロップ作成 for DaVinci Resolve)"
                   + getattr(self, "_tc_title_suffix", ""))

    # 最近使ったプロジェクト
    def _recent_projects(self) -> list:
        cfg = self._load_app_config()
        return [p for p in cfg.get("recent_projects", []) if Path(p).exists()]

    def _add_recent_project(self, path: str):
        try:
            cfg = self._load_app_config()
            lst = [p for p in cfg.get("recent_projects", []) if p != path]
            lst.insert(0, path)
            cfg["recent_projects"] = lst[:8]
            self._CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(self._CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            if getattr(self, "_recent_menu", None):
                self._rebuild_recent_menu()
        except Exception:
            pass

    def _add_recent_menu(self, parent_menu):
        self._recent_menu = tk.Menu(parent_menu, tearoff=0)
        parent_menu.add_cascade(label="最近使ったプロジェクト", menu=self._recent_menu)
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        m = self._recent_menu
        m.delete(0, 'end')
        recents = self._recent_projects()
        if not recents:
            m.add_command(label="(なし)", state='disabled')
            return
        for p in recents:
            m.add_command(label=Path(p).name,
                          command=lambda pp=p: self.load_project(pp))

    # ─── オートセーブ／クラッシュ復元 ───────────────

    _AUTOSAVE_PATH = Path.home() / ".davintelop" / "autosave.dtproj"
    _AUTOSAVE_INTERVAL_MS = 120000   # 2分

    def _autosave_tick(self):
        """一定間隔で、変更があれば自動保存（サイレント）。"""
        try:
            if getattr(self, "_project_dirty", False) and self.telop_list:
                self._AUTOSAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
                data = self._serialize_project()
                data["_autosave_of"] = self._current_project_path or ""
                with open(self._AUTOSAVE_PATH, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        finally:
            self.after(self._AUTOSAVE_INTERVAL_MS, self._autosave_tick)

    def _clear_autosave(self):
        try:
            if self._AUTOSAVE_PATH.exists():
                self._AUTOSAVE_PATH.unlink()
        except Exception:
            pass

    def _check_autosave_restore(self):
        """起動時、前回の自動保存があれば復元を提案。"""
        try:
            if not self._AUTOSAVE_PATH.exists():
                return
            if self.telop_list:   # 既に何か読み込まれていれば何もしない
                return
            with open(self._AUTOSAVE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            n = len(data.get("telops", []))
            if n == 0:
                self._clear_autosave()
                return
            orig = data.get("_autosave_of", "")
            extra = f"\n元ファイル: {Path(orig).name}" if orig else ""
            if messagebox.askyesno(
                "自動保存データの復元",
                f"前回終了時に保存されていない自動保存データがあります。\n"
                f"テロップ {n} 件{extra}\n\n復元しますか？", parent=self):
                self._suppress_dirty = True
                self._apply_project_data(data)
                self._current_project_path = orig or None
                self._suppress_dirty = False
                self._project_dirty = True   # 復元直後は未保存扱い
                self._update_title()
                self._progress_var.set("自動保存データを復元しました")
            else:
                self._clear_autosave()
        except Exception:
            pass

    # ─── ユーティリティ ───────────────────────────

    # ─── アプリ設定 保存/読み込み ─────────────────

    _CONFIG_PATH = Path.home() / ".davintelop" / "app_config.json"

    def _load_app_config(self) -> dict:
        try:
            if self._CONFIG_PATH.exists():
                with open(self._CONFIG_PATH, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_app_config(self):
        try:
            self._CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            cfg = self._load_app_config()   # 既存値(recent_projects等)を保持
            cfg.update({
                "resolve_out_dir":           self._resolve_out_dir.get(),
                "resolve_track":             self._resolve_track.get(),
                "shortcuts":                 self._shortcuts,
                "include_text_in_filename":  self._include_text_in_filename.get(),
                "dark_mode":                 self._is_dark.get(),
                "playhead_writeback":        self._playhead_writeback.get(),
                "rizoripute_path":           self._rizoripute_path.get(),
                "speaker_rules":             getattr(self, "_speaker_rules", {}),
                "new_telop":                 getattr(self, "_new_telop_cfg", {}),
                "show_timeline":             self._show_timeline.get(),
                "layout_mode":               self._layout_mode.get(),
                "show_quick_add":            self._show_quick_add.get(),
                "telocut_sync":              self._tc_sync.get() if hasattr(self, "_tc_sync") else False,
                "telocut_sync_seek":         self._tc_sync_seek.get() if hasattr(self, "_tc_sync_seek") else True,
            })
            with open(self._CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _run_with_progress(self, msg: str, task):
        """別スレッドで処理を実行してプログレス表示。"""
        import threading
        self._progress_var.set(msg)

        def callback(current, total, text):
            self.after(0, lambda: self._progress_var.set(f"{msg} {current}/{total}: {text}"))

        def run():
            try:
                task(callback)
            finally:
                self.after(0, lambda: self._progress_var.set(""))

        t = threading.Thread(target=run, daemon=True)
        t.start()

    _fonts_cache: Optional[list] = None

    def _get_fonts_cached(self) -> list[tuple[str, str]]:
        if self._fonts_cache is None:
            from core.renderer import get_system_fonts
            self._fonts_cache = get_system_fonts()
        return self._fonts_cache

    def _on_close(self):
        """ウィンドウを閉じる際にバックグラウンドスレッドを停止する。"""
        if not self._confirm_discard():
            return
        self._clear_autosave()   # 正常終了時は自動保存を消す
        try:
            if self.state() == 'normal':
                cfg = self._load_app_config()
                cfg["window_geometry"] = self.geometry()
                with open(self._CONFIG_PATH, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        self._stop_bg_capture()
        self._stop_fg_source()
        sender = getattr(self, '_ndi_sender', None)
        if sender is not None:
            try:
                sender.stop()
            except Exception:
                pass
        self.destroy()


# ─────────────────────────────────────────────
#  エントリーポイント
# ─────────────────────────────────────────────

def _enable_dpi_awareness():
    """Windows: Per-Monitor DPI 対応にして tkinter 座標と mss 物理ピクセルを一致させる。
    （マルチモニター・スクリーンキャプチャの座標ずれ防止）"""
    import platform
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        # Per-Monitor Aware V2 (Win10 1703+)
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
            return
        except Exception:
            pass
        # フォールバック: Per-Monitor Aware
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        # 最終フォールバック: System DPI Aware
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_LOG_DIR  = Path.home() / ".davintelop"
_LOG_PATH = _LOG_DIR / "crash.log"


def _setup_crash_logging():
    """例外と C 層のクラッシュ(segfault)をログファイルに記録する。
    windowed EXE では標準エラーが見えないため、原因特定に必須。"""
    import datetime, traceback, threading
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    def _write(header: str, text: str):
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"\n===== {ts}  {header} =====\n{text}\n")
        except Exception:
            pass

    # C 層クラッシュ(segfault 等)のネイティブスタックをダンプ
    try:
        import faulthandler
        fh = open(_LOG_PATH, "a", encoding="utf-8")
        faulthandler.enable(file=fh, all_threads=True)
        _write("session", "起動: faulthandler 有効")
    except Exception:
        pass

    # Python 未捕捉例外
    def _excepthook(exc_type, exc_value, exc_tb):
        _write("uncaught exception",
               "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    sys.excepthook = _excepthook

    # スレッド内例外 (Python 3.8+)
    try:
        def _threadhook(args):
            _write(f"thread exception ({args.thread.name})",
                   "".join(traceback.format_exception(
                       args.exc_type, args.exc_value, args.exc_traceback)))
        threading.excepthook = _threadhook
    except Exception:
        pass


if __name__ == "__main__":
    _setup_crash_logging()
    _enable_dpi_awareness()

    if not PILLOW_OK:
        print("警告: Pillow がインストールされていません。")
        print("  pip install Pillow  を実行してください。")
        print("  PNG書き出し・プレビュー機能が無効になります。\n")

    try:
        app = DavinTelopApp()
        # tkinter コールバック内例外もログに記録
        import traceback as _tb
        def _report_cb_exc(exc, val, tb):
            try:
                with open(_LOG_PATH, "a", encoding="utf-8") as f:
                    import datetime as _dt
                    f.write(f"\n===== {_dt.datetime.now():%Y-%m-%d %H:%M:%S}  tk callback =====\n")
                    f.write("".join(_tb.format_exception(exc, val, tb)) + "\n")
            except Exception:
                pass
        app.report_callback_exception = _report_cb_exc
        app.mainloop()
    except Exception:
        import traceback as _tb
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write("\n===== FATAL (mainloop) =====\n")
                f.write(_tb.format_exc() + "\n")
        except Exception:
            pass
        raise
