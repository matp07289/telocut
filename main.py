"""
DavinTelop - DaVinci Resolve テロップ作成ツール
CSV読み込み / PNG書き出し / SRT書き出し / FCPxml書き出し / Resolve API連携
カーニング・縁取り・背景・プリセット対応
"""
import os
import sys
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser, simpledialog
from pathlib import Path
from typing import Optional

# コアモジュール
from core.models import DesignSettings, TelopEntry
from core.timecode import parse_timecode, seconds_to_timecode
from core.preset import PresetManager
from core.exporter import import_csv, export_png, export_srt, export_fcpxml, ResolveConnector

try:
    from PIL import Image, ImageTk
    PILLOW_OK = True
except ImportError:
    PILLOW_OK = False


# ─────────────────────────────────────────────
#  テロップ編集ダイアログ
# ─────────────────────────────────────────────

class TelopEditDialog(tk.Toplevel):
    """テロップ1エントリの編集ダイアログ。"""

    def __init__(self, parent, entry: Optional[TelopEntry] = None):
        super().__init__(parent)
        self.title("テロップ編集")
        self.resizable(False, False)
        self.result: Optional[dict] = None
        self._build(entry)
        self.grab_set()
        self.wait_window()

    def _build(self, entry: Optional[TelopEntry]):
        pad = dict(padx=8, pady=4)
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="開始タイムコード:").grid(row=0, column=0, sticky='e', **pad)
        self._start = ttk.Entry(frame, width=18)
        self._start.grid(row=0, column=1, sticky='w', **pad)
        ttk.Label(frame, text="例: 00:00:01:00  00:00:01.000", foreground='gray').grid(row=0, column=2, sticky='w')

        ttk.Label(frame, text="終了タイムコード:").grid(row=1, column=0, sticky='e', **pad)
        self._end = ttk.Entry(frame, width=18)
        self._end.grid(row=1, column=1, sticky='w', **pad)

        ttk.Label(frame, text="テキスト:").grid(row=2, column=0, sticky='ne', **pad)
        self._text = tk.Text(frame, width=42, height=5, wrap='word')
        self._text.grid(row=2, column=1, columnspan=2, sticky='ew', **pad)

        ttk.Label(frame, text="メモ:").grid(row=3, column=0, sticky='e', **pad)
        self._note = ttk.Entry(frame, width=42)
        self._note.grid(row=3, column=1, columnspan=2, sticky='ew', **pad)

        self._enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="有効", variable=self._enabled_var).grid(
            row=4, column=1, sticky='w', **pad)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=5, column=0, columnspan=3, pady=(12, 0))
        ttk.Button(btn_frame, text="OK", width=10, command=self._ok).pack(side='left', padx=4)
        ttk.Button(btn_frame, text="キャンセル", width=10, command=self.destroy).pack(side='left', padx=4)

        if entry:
            self._start.insert(0, entry.start_time)
            self._end.insert(0, entry.end_time)
            self._text.insert('1.0', entry.text)
            self._note.insert(0, entry.note)
            self._enabled_var.set(entry.enabled)

        self._text.focus_set()

    def _ok(self):
        start = self._start.get().strip()
        end   = self._end.get().strip()
        text  = self._text.get('1.0', 'end-1c').strip()
        if not start or not end or not text:
            messagebox.showwarning("入力エラー", "開始・終了・テキストは必須です。", parent=self)
            return
        self.result = {
            "start_time": start,
            "end_time": end,
            "text": text,
            "note": self._note.get().strip(),
            "enabled": self._enabled_var.get(),
        }
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

class DavinTelopApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("DavinTelop  —  テロップ作成 for DaVinci Resolve")
        self.geometry("1440x860")
        self.minsize(1100, 700)

        self.telop_list: list[TelopEntry] = []
        self.design = DesignSettings()
        self.preset_manager = PresetManager()
        self._next_id = 1
        self._preview_image_ref = None
        self._resolve = ResolveConnector()

        self._setup_style()
        self._setup_menu()
        self._setup_ui()
        self._refresh_preset_menu()
        self._schedule_preview_update()

    # ─── スタイル ───────────────────────────────

    def _setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use('aqua')
        except Exception:
            pass

    # ─── メニューバー ────────────────────────────

    def _setup_menu(self):
        mb = tk.Menu(self)
        self.configure(menu=mb)

        # ファイルメニュー
        file_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="ファイル", menu=file_menu)
        file_menu.add_command(label="CSVを開く...", accelerator="⌘O", command=self.import_csv)
        file_menu.add_command(label="プロジェクトを保存...", accelerator="⌘S", command=self.save_project)
        file_menu.add_command(label="プロジェクトを開く...", command=self.load_project)
        file_menu.add_separator()
        file_menu.add_command(label="終了", accelerator="⌘Q", command=self.quit)
        self.bind("<Command-o>", lambda e: self.import_csv())
        self.bind("<Command-s>", lambda e: self.save_project())
        self.bind("<Command-q>", lambda e: self.quit())

        # 編集メニュー
        edit_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="編集", menu=edit_menu)
        edit_menu.add_command(label="行を追加", accelerator="⌘N", command=self.add_entry)
        edit_menu.add_command(label="選択行を編集", accelerator="Return", command=self.edit_selected)
        edit_menu.add_command(label="選択行を複製", command=self.duplicate_selected)
        edit_menu.add_command(label="選択行を削除", accelerator="⌫", command=self.delete_selected)
        edit_menu.add_separator()
        edit_menu.add_command(label="全選択", accelerator="⌘A", command=self.select_all)
        edit_menu.add_command(label="有効/無効を切り替え", command=self.toggle_enabled)
        self.bind("<Command-n>", lambda e: self.add_entry())

        # 書き出しメニュー
        export_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="書き出し", menu=export_menu)
        export_menu.add_command(label="PNG を書き出し...", command=self.export_png)
        export_menu.add_command(label="SRT を書き出し...", command=self.export_srt)
        export_menu.add_command(label="FCPxml を書き出し...", command=self.export_fcpxml)

        # Resolveメニュー
        resolve_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="Resolve", menu=resolve_menu)
        resolve_menu.add_command(label="DaVinci Resolveへ送る...", command=self.send_to_resolve)
        resolve_menu.add_command(label="接続テスト", command=self.test_resolve_connection)

        # プリセットメニュー
        self._preset_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="プリセット", menu=self._preset_menu)
        self._preset_menu.add_command(label="現在の設定を保存...", command=self.save_preset)
        self._preset_menu.add_separator()

    # ─── メインUI ────────────────────────────────

    def _setup_ui(self):
        # メインペイン (左: リスト / 右: デザイン+プレビュー)
        main_pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        left_frame = ttk.Frame(main_pane)
        main_pane.add(left_frame, weight=1)

        right_frame = ttk.Frame(main_pane)
        main_pane.add(right_frame, weight=2)

        self._setup_left_panel(left_frame)
        self._setup_right_panel(right_frame)
        self._setup_bottom_bar()

    def _setup_left_panel(self, parent):
        # CSV取り込みセクション
        csv_frame = ttk.LabelFrame(parent, text="CSVインポート", padding=6)
        csv_frame.pack(fill='x', padx=4, pady=(4, 2))

        ttk.Button(csv_frame, text="CSVを開く...", command=self.import_csv).grid(row=0, column=0, padx=4, pady=2)

        ttk.Label(csv_frame, text="文字コード:").grid(row=0, column=1, padx=(12, 2))
        self._encoding_var = tk.StringVar(value="utf-8")
        enc_combo = ttk.Combobox(csv_frame, textvariable=self._encoding_var, width=10,
                                  values=["utf-8", "utf-8-sig", "shift_jis", "cp932", "euc_jp"])
        enc_combo.grid(row=0, column=2, padx=2)

        ttk.Label(csv_frame, text="区切り:").grid(row=0, column=3, padx=(12, 2))
        self._delim_var = tk.StringVar(value="auto")
        ttk.Combobox(csv_frame, textvariable=self._delim_var, width=8,
                     values=["auto", ",", "\t", ";", "|"]).grid(row=0, column=4, padx=2)

        # テロップリスト
        list_frame = ttk.LabelFrame(parent, text="テロップリスト", padding=4)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)

        # ツールバー
        tb = ttk.Frame(list_frame)
        tb.pack(fill='x', pady=(0, 4))
        for label, cmd in [("＋ 追加", self.add_entry), ("✎ 編集", self.edit_selected),
                            ("⧉ 複製", self.duplicate_selected), ("✕ 削除", self.delete_selected),
                            ("↑", self.move_up), ("↓", self.move_down)]:
            ttk.Button(tb, text=label, width=6, command=cmd).pack(side='left', padx=1)

        # Treeview
        cols = ("id", "start", "end", "text", "enabled")
        self._tree = ttk.Treeview(list_frame, columns=cols, show='headings', selectmode='extended')
        self._tree.heading("id",      text="No.")
        self._tree.heading("start",   text="開始")
        self._tree.heading("end",     text="終了")
        self._tree.heading("text",    text="テキスト")
        self._tree.heading("enabled", text="有効")
        self._tree.column("id",      width=40,  anchor='center', stretch=False)
        self._tree.column("start",   width=110, anchor='center', stretch=False)
        self._tree.column("end",     width=110, anchor='center', stretch=False)
        self._tree.column("text",    width=200, anchor='w')
        self._tree.column("enabled", width=42,  anchor='center', stretch=False)

        scroll_y = ttk.Scrollbar(list_frame, orient='vertical', command=self._tree.yview)
        self._tree.configure(yscrollcommand=scroll_y.set)
        self._tree.pack(side='left', fill=tk.BOTH, expand=True)
        scroll_y.pack(side='right', fill='y')

        self._tree.bind('<Double-1>', lambda e: self.edit_selected())
        self._tree.bind('<Return>',   lambda e: self.edit_selected())
        self._tree.bind('<Delete>',   lambda e: self.delete_selected())
        self._tree.bind('<<TreeviewSelect>>', self._on_select)

        # 右クリックメニュー
        self._ctx_menu = tk.Menu(self._tree, tearoff=0)
        self._ctx_menu.add_command(label="編集", command=self.edit_selected)
        self._ctx_menu.add_command(label="複製", command=self.duplicate_selected)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="有効/無効", command=self.toggle_enabled)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="削除", command=self.delete_selected)
        self._tree.bind('<Button-2>', self._show_context_menu)
        self._tree.bind('<Control-Button-1>', self._show_context_menu)

        # ステータスラベル
        self._status_var = tk.StringVar(value="テロップ: 0 件")
        ttk.Label(list_frame, textvariable=self._status_var, anchor='w').pack(
            fill='x', pady=(4, 0))

    def _setup_right_panel(self, parent):
        right_pane = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        right_pane.pack(fill=tk.BOTH, expand=True)

        design_outer = ttk.Frame(right_pane)
        right_pane.add(design_outer, weight=1)

        preview_outer = ttk.Frame(right_pane)
        right_pane.add(preview_outer, weight=2)

        self._setup_design_panel(design_outer)
        self._setup_preview_panel(preview_outer)

    def _setup_design_panel(self, parent):
        nb = ttk.Notebook(parent)
        nb.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # ── テキストタブ ──────────────────────────
        tab_text = ttk.Frame(nb, padding=10)
        nb.add(tab_text, text="テキスト")

        fonts = [name for name, _ in self._get_fonts_cached()]

        ttk.Label(tab_text, text="フォント:").grid(row=0, column=0, sticky='e', padx=4, pady=3)
        self._font_var = tk.StringVar(value=self.design.font_path)
        self._font_combo = ttk.Combobox(tab_text, textvariable=self._font_var, values=fonts, width=28)
        self._font_combo.grid(row=0, column=1, sticky='w', padx=4, pady=3)
        ttk.Button(tab_text, text="ファイル...", command=self._pick_font_file).grid(row=0, column=2, padx=4)
        self._font_var.trace_add('write', self._on_design_change)

        ttk.Label(tab_text, text="サイズ:").grid(row=1, column=0, sticky='e', padx=4, pady=3)
        self._font_size_var = tk.IntVar(value=self.design.font_size)
        sz_frame = ttk.Frame(tab_text)
        sz_frame.grid(row=1, column=1, sticky='w', padx=4)
        ttk.Spinbox(sz_frame, from_=8, to=300, textvariable=self._font_size_var, width=6,
                    command=self._on_design_change).pack(side='left')
        ttk.Scale(sz_frame, from_=8, to=200, variable=self._font_size_var, orient='horizontal',
                  length=140, command=lambda v: self._on_design_change()).pack(side='left', padx=6)
        self._font_size_var.trace_add('write', self._on_design_change)

        ttk.Label(tab_text, text="文字色:").grid(row=2, column=0, sticky='e', padx=4, pady=3)
        self._font_color_btn = ColorButton(tab_text, color=self.design.font_color,
                                            command=self._on_font_color_change)
        self._font_color_btn.grid(row=2, column=1, sticky='w', padx=4)

        ttk.Label(tab_text, text="カーニング (字間):").grid(row=3, column=0, sticky='e', padx=4, pady=3)
        self._kern_var = tk.IntVar(value=self.design.letter_spacing)
        kern_frame = ttk.Frame(tab_text)
        kern_frame.grid(row=3, column=1, sticky='w', padx=4)
        ttk.Spinbox(kern_frame, from_=-50, to=200, textvariable=self._kern_var, width=6,
                    command=self._on_design_change).pack(side='left')
        ttk.Scale(kern_frame, from_=-20, to=100, variable=self._kern_var, orient='horizontal',
                  length=140, command=lambda v: self._on_design_change()).pack(side='left', padx=6)
        ttk.Label(kern_frame, text="px").pack(side='left')
        self._kern_var.trace_add('write', self._on_design_change)

        ttk.Label(tab_text, text="行間:").grid(row=4, column=0, sticky='e', padx=4, pady=3)
        self._linespace_var = tk.DoubleVar(value=self.design.line_spacing)
        ls_frame = ttk.Frame(tab_text)
        ls_frame.grid(row=4, column=1, sticky='w', padx=4)
        ttk.Spinbox(ls_frame, from_=0.8, to=4.0, increment=0.1,
                    textvariable=self._linespace_var, width=6,
                    format='%.1f', command=self._on_design_change).pack(side='left')
        self._linespace_var.trace_add('write', self._on_design_change)

        # ── 縁取りタブ ────────────────────────────
        tab_stroke = ttk.Frame(nb, padding=10)
        nb.add(tab_stroke, text="縁取り")

        self._stroke_enabled_var = tk.BooleanVar(value=self.design.stroke_enabled)
        ttk.Checkbutton(tab_stroke, text="縁取りを有効にする",
                        variable=self._stroke_enabled_var,
                        command=self._on_design_change).grid(row=0, column=0, columnspan=3,
                                                              sticky='w', pady=(0, 8))

        ttk.Label(tab_stroke, text="縁取り色:").grid(row=1, column=0, sticky='e', padx=4, pady=3)
        self._stroke_color_btn = ColorButton(tab_stroke, color=self.design.stroke_color,
                                              command=self._on_stroke_color_change)
        self._stroke_color_btn.grid(row=1, column=1, sticky='w', padx=4)

        ttk.Label(tab_stroke, text="縁取り幅:").grid(row=2, column=0, sticky='e', padx=4, pady=3)
        self._stroke_w_var = tk.IntVar(value=self.design.stroke_width)
        sw_frame = ttk.Frame(tab_stroke)
        sw_frame.grid(row=2, column=1, sticky='w', padx=4)
        ttk.Spinbox(sw_frame, from_=0, to=30, textvariable=self._stroke_w_var, width=6,
                    command=self._on_design_change).pack(side='left')
        ttk.Scale(sw_frame, from_=0, to=20, variable=self._stroke_w_var, orient='horizontal',
                  length=140, command=lambda v: self._on_design_change()).pack(side='left', padx=6)
        ttk.Label(sw_frame, text="px").pack(side='left')
        self._stroke_w_var.trace_add('write', self._on_design_change)

        ttk.Separator(tab_stroke, orient='horizontal').grid(row=3, column=0, columnspan=3,
                                                             sticky='ew', pady=8)

        self._shadow_enabled_var = tk.BooleanVar(value=self.design.shadow_enabled)
        ttk.Checkbutton(tab_stroke, text="ドロップシャドウ",
                        variable=self._shadow_enabled_var,
                        command=self._on_design_change).grid(row=4, column=0, columnspan=3,
                                                              sticky='w')

        ttk.Label(tab_stroke, text="シャドウ色:").grid(row=5, column=0, sticky='e', padx=4, pady=3)
        self._shadow_color_btn = ColorButton(tab_stroke, color=self.design.shadow_color,
                                              command=self._on_shadow_color_change)
        self._shadow_color_btn.grid(row=5, column=1, sticky='w', padx=4)

        ttk.Label(tab_stroke, text="不透明度:").grid(row=6, column=0, sticky='e', padx=4, pady=3)
        self._shadow_opacity_var = tk.IntVar(value=self.design.shadow_opacity)
        ttk.Scale(tab_stroke, from_=0, to=255, variable=self._shadow_opacity_var,
                  orient='horizontal', length=160,
                  command=lambda v: self._on_design_change()).grid(row=6, column=1, sticky='w', padx=4)
        self._shadow_opacity_var.trace_add('write', self._on_design_change)

        ttk.Label(tab_stroke, text="オフセット X:").grid(row=7, column=0, sticky='e', padx=4, pady=3)
        self._shadow_ox_var = tk.IntVar(value=self.design.shadow_offset_x)
        ttk.Spinbox(tab_stroke, from_=-50, to=50, textvariable=self._shadow_ox_var,
                    width=6, command=self._on_design_change).grid(row=7, column=1, sticky='w', padx=4)
        self._shadow_ox_var.trace_add('write', self._on_design_change)

        ttk.Label(tab_stroke, text="オフセット Y:").grid(row=8, column=0, sticky='e', padx=4, pady=3)
        self._shadow_oy_var = tk.IntVar(value=self.design.shadow_offset_y)
        ttk.Spinbox(tab_stroke, from_=-50, to=50, textvariable=self._shadow_oy_var,
                    width=6, command=self._on_design_change).grid(row=8, column=1, sticky='w', padx=4)
        self._shadow_oy_var.trace_add('write', self._on_design_change)

        # ── 背景タブ ──────────────────────────────
        tab_bg = ttk.Frame(nb, padding=10)
        nb.add(tab_bg, text="背景")

        self._bg_enabled_var = tk.BooleanVar(value=self.design.bg_enabled)
        ttk.Checkbutton(tab_bg, text="背景を有効にする",
                        variable=self._bg_enabled_var,
                        command=self._on_design_change).grid(row=0, column=0, columnspan=3,
                                                              sticky='w', pady=(0, 8))

        ttk.Label(tab_bg, text="背景色:").grid(row=1, column=0, sticky='e', padx=4, pady=3)
        self._bg_color_btn = ColorButton(tab_bg, color=self.design.bg_color,
                                          command=self._on_bg_color_change)
        self._bg_color_btn.grid(row=1, column=1, sticky='w', padx=4)

        ttk.Label(tab_bg, text="不透明度:").grid(row=2, column=0, sticky='e', padx=4, pady=3)
        self._bg_opacity_var = tk.IntVar(value=self.design.bg_opacity)
        bg_op_frame = ttk.Frame(tab_bg)
        bg_op_frame.grid(row=2, column=1, sticky='w', padx=4)
        ttk.Scale(bg_op_frame, from_=0, to=255, variable=self._bg_opacity_var,
                  orient='horizontal', length=160,
                  command=lambda v: self._on_design_change()).pack(side='left')
        ttk.Label(bg_op_frame, textvariable=self._bg_opacity_var, width=4).pack(side='left', padx=4)
        self._bg_opacity_var.trace_add('write', self._on_design_change)

        ttk.Label(tab_bg, text="内側余白:").grid(row=3, column=0, sticky='e', padx=4, pady=3)
        self._bg_pad_var = tk.IntVar(value=self.design.bg_padding)
        ttk.Spinbox(tab_bg, from_=0, to=100, textvariable=self._bg_pad_var,
                    width=6, command=self._on_design_change).grid(row=3, column=1, sticky='w', padx=4)
        ttk.Label(tab_bg, text="px").grid(row=3, column=2, sticky='w')
        self._bg_pad_var.trace_add('write', self._on_design_change)

        # ── 位置タブ ──────────────────────────────
        tab_pos = ttk.Frame(nb, padding=10)
        nb.add(tab_pos, text="位置")

        ttk.Label(tab_pos, text="横位置:").grid(row=0, column=0, sticky='e', padx=4, pady=3)
        self._pos_h_var = tk.StringVar(value=self.design.position_h)
        ph_frame = ttk.Frame(tab_pos)
        ph_frame.grid(row=0, column=1, sticky='w')
        for val, label in [("left", "左"), ("center", "中央"), ("right", "右")]:
            ttk.Radiobutton(ph_frame, text=label, variable=self._pos_h_var, value=val,
                            command=self._on_design_change).pack(side='left', padx=4)

        ttk.Label(tab_pos, text="縦位置:").grid(row=1, column=0, sticky='e', padx=4, pady=3)
        self._pos_v_var = tk.StringVar(value=self.design.position_v)
        pv_frame = ttk.Frame(tab_pos)
        pv_frame.grid(row=1, column=1, sticky='w')
        for val, label in [("top", "上"), ("middle", "中央"), ("bottom", "下")]:
            ttk.Radiobutton(pv_frame, text=label, variable=self._pos_v_var, value=val,
                            command=self._on_design_change).pack(side='left', padx=4)

        ttk.Label(tab_pos, text="X オフセット:").grid(row=2, column=0, sticky='e', padx=4, pady=3)
        self._offset_x_var = tk.IntVar(value=self.design.offset_x)
        ox_frame = ttk.Frame(tab_pos)
        ox_frame.grid(row=2, column=1, sticky='w', padx=4)
        ttk.Spinbox(ox_frame, from_=-2000, to=2000, textvariable=self._offset_x_var,
                    width=7, command=self._on_design_change).pack(side='left')
        ttk.Label(ox_frame, text="px").pack(side='left', padx=4)
        self._offset_x_var.trace_add('write', self._on_design_change)

        ttk.Label(tab_pos, text="Y オフセット:").grid(row=3, column=0, sticky='e', padx=4, pady=3)
        self._offset_y_var = tk.IntVar(value=self.design.offset_y)
        oy_frame = ttk.Frame(tab_pos)
        oy_frame.grid(row=3, column=1, sticky='w', padx=4)
        ttk.Spinbox(oy_frame, from_=-2000, to=2000, textvariable=self._offset_y_var,
                    width=7, command=self._on_design_change).pack(side='left')
        ttk.Label(oy_frame, text="px").pack(side='left', padx=4)
        self._offset_y_var.trace_add('write', self._on_design_change)

        ttk.Label(tab_pos, text="テキスト揃え:").grid(row=4, column=0, sticky='e', padx=4, pady=3)
        self._align_var = tk.StringVar(value=self.design.text_align)
        al_frame = ttk.Frame(tab_pos)
        al_frame.grid(row=4, column=1, sticky='w')
        for val, label in [("left", "左揃え"), ("center", "中央揃え"), ("right", "右揃え")]:
            ttk.Radiobutton(al_frame, text=label, variable=self._align_var, value=val,
                            command=self._on_design_change).pack(side='left', padx=4)

        # ── キャンバスタブ ────────────────────────
        tab_canvas = ttk.Frame(nb, padding=10)
        nb.add(tab_canvas, text="キャンバス")

        ttk.Label(tab_canvas, text="解像度:").grid(row=0, column=0, sticky='e', padx=4, pady=3)
        res_frame = ttk.Frame(tab_canvas)
        res_frame.grid(row=0, column=1, sticky='w', padx=4)
        self._cw_var = tk.IntVar(value=self.design.canvas_width)
        self._ch_var = tk.IntVar(value=self.design.canvas_height)
        ttk.Spinbox(res_frame, from_=320, to=7680, textvariable=self._cw_var,
                    width=6, command=self._on_design_change).pack(side='left')
        ttk.Label(res_frame, text="×").pack(side='left', padx=4)
        ttk.Spinbox(res_frame, from_=240, to=4320, textvariable=self._ch_var,
                    width=6, command=self._on_design_change).pack(side='left')
        ttk.Label(res_frame, text="px").pack(side='left', padx=4)
        # プリセット解像度ボタン
        for w, h, label in [(1920, 1080, "FHD"), (3840, 2160, "4K"), (1280, 720, "HD")]:
            ttk.Button(res_frame, text=label, width=5,
                       command=lambda ww=w, hh=h: self._set_resolution(ww, hh)).pack(side='left', padx=2)
        self._cw_var.trace_add('write', self._on_design_change)
        self._ch_var.trace_add('write', self._on_design_change)

        ttk.Label(tab_canvas, text="フレームレート:").grid(row=1, column=0, sticky='e', padx=4, pady=3)
        self._fps_var = tk.DoubleVar(value=self.design.frame_rate)
        fps_combo = ttk.Combobox(tab_canvas, textvariable=self._fps_var, width=10,
                                  values=[23.976, 24.0, 25.0, 29.97, 30.0, 59.94, 60.0])
        fps_combo.grid(row=1, column=1, sticky='w', padx=4)
        self._fps_var.trace_add('write', self._on_design_change)

    def _setup_preview_panel(self, parent):
        preview_frame = ttk.LabelFrame(parent, text="プレビュー", padding=4)
        preview_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # プレビューテキスト入力
        pt_frame = ttk.Frame(preview_frame)
        pt_frame.pack(fill='x', pady=(0, 4))
        ttk.Label(pt_frame, text="プレビューテキスト:").pack(side='left')
        self._preview_text_var = tk.StringVar(value="テロップのプレビューです\n2行目のテキスト")
        ttk.Entry(pt_frame, textvariable=self._preview_text_var, width=40).pack(
            side='left', padx=6, fill='x', expand=True)
        self._preview_text_var.trace_add('write', self._on_design_change)

        # キャンバス (実際のプレビュー)
        self._preview_canvas = tk.Canvas(preview_frame, bg='#1a1a1a', cursor='crosshair')
        self._preview_canvas.pack(fill=tk.BOTH, expand=True)
        self._preview_canvas.bind('<Configure>', lambda e: self._schedule_preview_update())

    def _setup_bottom_bar(self):
        bar = ttk.Frame(self, relief='groove', padding=(8, 6))
        bar.pack(fill='x', side='bottom', padx=6, pady=(0, 6))

        ttk.Button(bar, text="📁 PNG を書き出し", command=self.export_png).pack(side='left', padx=4)
        ttk.Button(bar, text="💬 SRT を書き出し", command=self.export_srt).pack(side='left', padx=4)
        ttk.Button(bar, text="🎬 FCPxml を書き出し", command=self.export_fcpxml).pack(side='left', padx=4)

        ttk.Separator(bar, orient='vertical').pack(side='left', fill='y', padx=8)

        ttk.Button(bar, text="🔗 DaVinci Resolve へ送る", command=self.send_to_resolve).pack(side='left', padx=4)

        ttk.Separator(bar, orient='vertical').pack(side='left', fill='y', padx=8)

        self._progress_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._progress_var, foreground='blue').pack(side='left', padx=4)

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

    # ─── テロップリスト操作 ──────────────────────

    def add_entry(self):
        dlg = TelopEditDialog(self)
        if dlg.result:
            entry = TelopEntry(
                id=self._next_id,
                **dlg.result,
            )
            self._next_id += 1
            self.telop_list.append(entry)
            self._refresh_tree()
            self._select_last()

    def edit_selected(self):
        sel = self._get_selected_entries()
        if not sel:
            return
        entry = sel[0]
        dlg = TelopEditDialog(self, entry)
        if dlg.result:
            idx = self.telop_list.index(entry)
            for k, v in dlg.result.items():
                setattr(self.telop_list[idx], k, v)
            self._refresh_tree()

    def duplicate_selected(self):
        for entry in self._get_selected_entries():
            new_entry = TelopEntry(
                id=self._next_id,
                start_time=entry.start_time,
                end_time=entry.end_time,
                text=entry.text,
                enabled=entry.enabled,
                note=entry.note,
            )
            self._next_id += 1
            idx = self.telop_list.index(entry)
            self.telop_list.insert(idx + 1, new_entry)
        self._refresh_tree()

    def delete_selected(self):
        sel = self._get_selected_entries()
        if not sel:
            return
        if len(sel) > 1:
            if not messagebox.askyesno("確認", f"{len(sel)} 件のテロップを削除しますか？"):
                return
        for entry in sel:
            self.telop_list.remove(entry)
        self._refresh_tree()

    def select_all(self):
        for iid in self._tree.get_children():
            self._tree.selection_add(iid)

    def toggle_enabled(self):
        for entry in self._get_selected_entries():
            entry.enabled = not entry.enabled
        self._refresh_tree()

    def move_up(self):
        sel = self._get_selected_entries()
        for entry in sel:
            idx = self.telop_list.index(entry)
            if idx > 0:
                self.telop_list[idx - 1], self.telop_list[idx] = \
                    self.telop_list[idx], self.telop_list[idx - 1]
        self._refresh_tree()

    def move_down(self):
        sel = list(reversed(self._get_selected_entries()))
        for entry in sel:
            idx = self.telop_list.index(entry)
            if idx < len(self.telop_list) - 1:
                self.telop_list[idx], self.telop_list[idx + 1] = \
                    self.telop_list[idx + 1], self.telop_list[idx]
        self._refresh_tree()

    def _refresh_tree(self):
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        for entry in self.telop_list:
            check = "✓" if entry.enabled else "—"
            text_preview = entry.text.replace('\n', ' ')[:40]
            self._tree.insert('', 'end', iid=str(entry.id),
                              values=(entry.id, entry.start_time, entry.end_time,
                                      text_preview, check))
            if not entry.enabled:
                self._tree.tag_configure('disabled', foreground='gray')
                self._tree.item(str(entry.id), tags=('disabled',))
        count = len(self.telop_list)
        enabled = sum(1 for e in self.telop_list if e.enabled)
        self._status_var.set(f"テロップ: {count} 件  (有効: {enabled} 件)")

    def _select_last(self):
        children = self._tree.get_children()
        if children:
            self._tree.selection_set(children[-1])
            self._tree.see(children[-1])

    def _on_select(self, event=None):
        sel = self._get_selected_entries()
        if sel:
            self._preview_text_var.set(sel[0].text)

    def _get_selected_entries(self) -> list[TelopEntry]:
        selected_ids = {int(iid) for iid in self._tree.selection()}
        return [e for e in self.telop_list if e.id in selected_ids]

    def _show_context_menu(self, event):
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

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

    def _on_design_change(self, *_):
        self.design = self._collect_design()
        self._schedule_preview_update()

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
            img = render_telop_image(preview_text, s)

            # 黒背景に合成してプレビュー
            bg = Image.new('RGB', (s.canvas_width, s.canvas_height), (20, 20, 20))
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
            self._preview_canvas.create_image(ox, oy, anchor='nw', image=photo)

            # 解像度表示
            self._preview_canvas.create_text(
                ox + 4, oy + 4,
                text=f"{s.canvas_width}×{s.canvas_height}  {s.frame_rate}fps",
                anchor='nw', fill='#888888', font=('', 10))
        except Exception as ex:
            self._preview_canvas.delete('all')
            self._preview_canvas.create_text(
                cw // 2, ch // 2, text=f"レンダリングエラー:\n{ex}",
                fill='red', justify='center')

    # ─── プリセット ──────────────────────────────

    def save_preset(self):
        name = simpledialog.askstring("プリセット保存", "プリセット名を入力してください:", parent=self)
        if not name:
            return
        self.preset_manager.save_preset(name.strip(), self.design)
        self._refresh_preset_menu()
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
        output_dir = filedialog.askdirectory(title="PNG書き出し先フォルダを選択")
        if not output_dir:
            return
        if not PILLOW_OK:
            messagebox.showerror("エラー", "Pillowをインストールしてください:\npip install Pillow")
            return
        self._run_with_progress("PNG書き出し中...", lambda cb: self._do_export_png(output_dir, cb))

    def _do_export_png(self, output_dir: str, callback):
        s = self._collect_design()
        paths = export_png(self.telop_list, s, output_dir, callback=callback)
        self.after(0, lambda: messagebox.showinfo(
            "PNG書き出し完了",
            f"{len(paths)} 件のPNGを書き出しました:\n{output_dir}"))

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

    def send_to_resolve(self):
        if not self.telop_list:
            messagebox.showwarning("Resolve連携", "テロップがありません。")
            return
        if not PILLOW_OK:
            messagebox.showerror("エラー", "Pillowをインストールしてください:\npip install Pillow")
            return

        if not self._resolve.connect():
            messagebox.showerror("接続失敗",
                                  self._resolve.error + "\n\n"
                                  "Resolve > Preferences > General >\n"
                                  "External Scripting Using: Local を有効にしてください。")
            return

        info = self._resolve.get_timeline_info()
        dlg = ResolveDialog(self, info)
        if not dlg.result:
            return

        png_dir   = dlg.result["png_dir"]
        track_idx = dlg.result["track_index"]
        fps       = info.get("fps", self.design.frame_rate)

        self._run_with_progress(
            "Resolve へ送信中...",
            lambda cb: self._do_send_to_resolve(png_dir, track_idx, fps, cb)
        )

    def _do_send_to_resolve(self, png_dir: str, track_idx: int, fps: float, callback):
        s = self._collect_design()
        try:
            png_paths = export_png(self.telop_list, s, png_dir, callback=callback)
        except Exception as ex:
            self.after(0, lambda: messagebox.showerror("PNG書き出しエラー", str(ex)))
            return

        ok = self._resolve.import_pngs_to_timeline(
            self.telop_list, png_paths, fps, track_index=track_idx, callback=callback)

        if ok:
            self.after(0, lambda: messagebox.showinfo(
                "完了", f"DaVinci Resolveのタイムライン (V{track_idx}) にテロップを配置しました。"))
        else:
            self.after(0, lambda: messagebox.showerror("エラー", self._resolve.error))

    # ─── プロジェクト保存/読み込み ────────────────

    def save_project(self):
        path = filedialog.asksaveasfilename(
            title="プロジェクトを保存",
            defaultextension=".json",
            filetypes=[("DavinTelop プロジェクト", "*.json")],
        )
        if not path:
            return
        data = {
            "version": 1,
            "design": self._collect_design().to_dict(),
            "telops": [e.to_dict() for e in self.telop_list],
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self._progress_var.set(f"保存: {Path(path).name}")

    def load_project(self):
        path = filedialog.askopenfilename(
            title="プロジェクトを開く",
            filetypes=[("DavinTelop プロジェクト", "*.json")],
        )
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            s = DesignSettings.from_dict(data.get("design", {}))
            self.design = s
            self._apply_design_to_ui(s)
            self.telop_list = []
            for td in data.get("telops", []):
                self.telop_list.append(TelopEntry(**td))
            self._next_id = max((e.id for e in self.telop_list), default=0) + 1
            self._refresh_tree()
            self._schedule_preview_update()
            self._progress_var.set(f"読み込み: {Path(path).name}")
        except Exception as ex:
            messagebox.showerror("エラー", f"プロジェクトの読み込みに失敗しました:\n{ex}")

    # ─── ユーティリティ ───────────────────────────

    def _run_with_progress(self, msg: str, task):
        """別スレッドで処理を実行してプログレス表示。"""
        import threading
        self._progress_var.set(msg)

        def callback(current, total, text):
            self._progress_var.set(f"{msg} {current}/{total}: {text}")

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


# ─────────────────────────────────────────────
#  エントリーポイント
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if not PILLOW_OK:
        print("警告: Pillow がインストールされていません。")
        print("  pip install Pillow  を実行してください。")
        print("  PNG書き出し・プレビュー機能が無効になります。\n")

    app = DavinTelopApp()
    app.mainloop()
