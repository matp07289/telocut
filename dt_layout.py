"""DavinTelop の TFX-Artist 風レイアウト（テロップ専用ソフトの画面の並び）。

  ┌ メニュー ─────────────────────────────────────────────┐
  │ フォント ▾ | 太さ | サイズ | 縁・影・背景 | 揃え | セーフエリア | ＋テロップ | 書き出し │
  ├───────────┬─────────────────────────────┬─────────────┤
  │ プロパティ │ Program（大きなプレビュー・セーフエリア）    │ シェーダー  │
  │（テキスト・ │                                             │（色: 文字 / │
  │ エフェクト・│                                             │ 縁 / 影 /   │
  │ 配置）     ├──────────┬──────────────┬──────┤ 背景）      │
  ├───────────┤ スタイル  │ プリセットスタイル S1… │ 記号  ├─────────────┤
  │ レイヤー   │ プレビュー │                          │      │ ブラウザ    │
  │（テロップ） │  あア     │  あ あ あ あ …            │ ○△□ │（合成・声）  │
  ├───────────┴──────────┴──────────────┴──────┴─────────────┤
  │ メッセージ … │ Screen w:1920 h:1080 │ テロップ 3/12 │ x:0 y:80 │ 揃え │
  └──────────────────────────────────────────────────────────────┘
従来のレイアウトには「ウィンドウ → レイアウト」で戻せる（次の起動から）。
"""
from __future__ import annotations

import colorsys
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Dict, List, Optional, Tuple

# TFX のスタイル見本に近い組み込みスタイル（S1…）: (名前, 文字色, 縁, 縁の色, 縁の幅, 影, 背景, 背景色)
BUILTIN_STYLES = [
    ("S1 白・黒縁", "#FFFFFF", True, "#000000", 6, False, False, "#000000"),
    ("S2 白・赤縁", "#FFFFFF", True, "#D81E1E", 8, False, False, "#000000"),
    ("S3 白・青縁", "#FFFFFF", True, "#2850D8", 8, True, False, "#000000"),
    ("S4 緑", "#3CFF6E", True, "#0A3A12", 6, False, False, "#000000"),
    ("S5 水色", "#9FE8FF", True, "#0B2B44", 6, True, False, "#000000"),
    ("S6 金", "#FFD84A", True, "#4A3000", 8, True, False, "#000000"),
    ("S7 紫", "#F3C8FF", True, "#6A1E9C", 8, False, False, "#000000"),
    ("S8 白・紺縁", "#FFFFFF", True, "#14205A", 10, True, False, "#000000"),
    ("S9 黒・白縁", "#111111", True, "#FFFFFF", 8, False, False, "#000000"),
    ("S10 黄・黒縁", "#FFE600", True, "#000000", 8, True, False, "#000000"),
    ("S11 帯・白", "#FFFFFF", False, "#000000", 0, False, True, "#000000"),
    ("S12 帯・黄", "#FFE14D", False, "#000000", 0, False, True, "#1E2A6E"),
]
SWATCHES = ["#FFFFFF", "#000000", "#FF0000", "#00FF00", "#0000FF", "#00FFFF", "#FF00FF", "#FFFF00",
            "#FF8000", "#8000FF", "#00A0FF", "#FF5090", "#808080", "#C0C0C0", "#7F0000", "#004C00",
            "#00007F", "#FFE14D", "#3CFF6E", "#9FE8FF", "#F3C8FF", "#D81E1E", "#14205A", "#4A3000"]
SYMBOLS = list("○●◎△▲□■◇◆☆★♡♥♪♫※！？→←↑↓⇒〜…「」『』【】（）・♂♀✕✔☀☁☂☃☎✉♨")
COLOR_TARGETS = [("font", "文字"), ("stroke", "縁取り"), ("shadow", "影"), ("bg", "背景")]


def _hex(rgb: Tuple[float, float, float]) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(c * 255)))) for c in rgb)


def _rgb(hexs: str) -> Tuple[float, float, float]:
    h = (hexs or "#FFFFFF").lstrip("#")
    try:
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return (1.0, 1.0, 1.0)


class _Panel(ttk.Frame):
    """タイトルの帯付きのパネル（TFX のように中央にタイトル）。"""

    def __init__(self, parent, title: str):
        super().__init__(parent)
        bar = tk.Frame(self, height=20, bg="#2b2b30")
        bar.pack(fill="x")
        tk.Label(bar, text=title, bg="#2b2b30", fg="#a8a8b0", font=("", 9)).pack(pady=1)
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)


class ColorPanel(ttk.Frame):
    """シェーダー（色を選ぶ）: 色相と明るさの四角・色相のバー・16 進・見本の色・最近の色。どれに色を付けるかを選ぶ。"""

    def __init__(self, parent, app):
        super().__init__(parent, padding=4)
        self.app = app
        self.target = tk.StringVar(value="font")
        self.hex = tk.StringVar(value="#FFFFFF")
        self.h, self.s, self.v = 0.0, 0.0, 1.0
        self.recent: List[str] = []
        row = ttk.Frame(self)
        row.pack(fill="x")
        for key, lab in COLOR_TARGETS:
            ttk.Radiobutton(row, text=lab, value=key, variable=self.target, command=self.load_target).pack(side="left", padx=2)
        mid = ttk.Frame(self)
        mid.pack(fill="x", pady=4)
        self.sv = tk.Canvas(mid, width=180, height=140, highlightthickness=1, highlightbackground="#555", cursor="crosshair")
        self.sv.pack(side="left")
        self.hue = tk.Canvas(mid, width=18, height=140, highlightthickness=1, highlightbackground="#555", cursor="sb_v_double_arrow")
        self.hue.pack(side="left", padx=4)
        self.cur = tk.Canvas(mid, width=44, height=44, highlightthickness=1, highlightbackground="#888")
        self.cur.pack(side="left", anchor="n", padx=4)
        for c, fn in ((self.sv, self._sv_click), (self.hue, self._hue_click)):
            c.bind("<Button-1>", fn)
            c.bind("<B1-Motion>", fn)
        hx = ttk.Frame(self)
        hx.pack(fill="x")
        ttk.Label(hx, text="16 進").pack(side="left")
        e = ttk.Entry(hx, textvariable=self.hex, width=9)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _e: self.set_color(self.hex.get(), apply=True))
        ttk.Button(hx, text="スポイト…", command=self._pick_dialog).pack(side="left")
        ttk.Label(self, text="見本の色").pack(anchor="w", pady=(6, 0))
        self.sw = ttk.Frame(self)
        self.sw.pack(fill="x")
        for i, col in enumerate(SWATCHES):
            b = tk.Button(self.sw, bg=col, activebackground=col, width=2, height=1, relief="flat", bd=1,
                          command=lambda c=col: self.set_color(c, apply=True))
            b.grid(row=i // 8, column=i % 8, padx=1, pady=1)
        ttk.Label(self, text="最近の色").pack(anchor="w", pady=(6, 0))
        self.rec = ttk.Frame(self)
        self.rec.pack(fill="x")
        self._sv_img = None
        self._hue_img = None
        self._draw_hue()
        self.after(50, self.load_target)

    # ── 描く ──
    def _draw_hue(self):
        self.hue.delete("all")
        h = 140
        for y in range(h):
            self.hue.create_line(0, y, 18, y, fill=_hex(colorsys.hsv_to_rgb(y / h, 1, 1)))
        self._hue_mark = self.hue.create_rectangle(0, 0, 18, 2, outline="white")

    def _draw_sv(self):
        try:
            from PIL import Image, ImageTk
        except ImportError:
            return
        import numpy as np
        w, h = 180, 140
        s = np.linspace(0, 1, w)[None, :]
        v = np.linspace(1, 0, h)[:, None]
        r, g, b = colorsys.hsv_to_rgb(self.h, 1, 1)
        base = np.array([r, g, b])
        rgb = (v[..., None] * ((1 - s)[..., None] * np.ones(3) + s[..., None] * base)) * 255
        img = Image.fromarray(rgb.clip(0, 255).astype("uint8"), "RGB")
        self._sv_img = ImageTk.PhotoImage(img)
        self.sv.delete("all")
        self.sv.create_image(0, 0, anchor="nw", image=self._sv_img)
        x, y = self.s * w, (1 - self.v) * h
        self.sv.create_oval(x - 5, y - 5, x + 5, y + 5, outline="white", width=2)
        self.hue.coords(self._hue_mark, 0, self.h * 140 - 1, 18, self.h * 140 + 1)
        col = _hex(colorsys.hsv_to_rgb(self.h, self.s, self.v))
        self.cur.configure(bg=col)

    def _sv_click(self, e):
        self.s = max(0.0, min(1.0, e.x / 180))
        self.v = max(0.0, min(1.0, 1 - e.y / 140))
        self._from_hsv()

    def _hue_click(self, e):
        self.h = max(0.0, min(0.999, e.y / 140))
        self._from_hsv()

    def _from_hsv(self):
        col = _hex(colorsys.hsv_to_rgb(self.h, self.s, self.v))
        self.hex.set(col)
        self._draw_sv()
        self.apply(col)

    def _pick_dialog(self):
        from tkinter import colorchooser
        r = colorchooser.askcolor(color=self.hex.get(), parent=self)
        if r and r[1]:
            self.set_color(r[1], apply=True)

    # ── 色の読み書き ──
    def _button(self):
        return {"font": "_font_color_btn", "stroke": "_stroke_color_btn", "shadow": "_shadow_color_btn",
                "bg": "_bg_color_btn"}[self.target.get()]

    def load_target(self):
        btn = getattr(self.app, self._button(), None)
        if btn is not None:
            self.set_color(btn.get(), apply=False)

    def set_color(self, col: str, apply: bool = True):
        col = col if col.startswith("#") else "#" + col
        self.hex.set(col.upper())
        self.h, self.s, self.v = colorsys.rgb_to_hsv(*_rgb(col))
        self._draw_sv()
        if apply:
            self.apply(col)

    def apply(self, col: str):
        """選んだ対象（文字 / 縁取り / 影 / 背景）に色を付ける。縁・影・背景はオンにする。"""
        app = self.app
        t = self.target.get()
        btn = getattr(app, self._button(), None)
        if btn is None:
            return
        btn.set(col)
        if t != "font":
            var = getattr(app, {"stroke": "_stroke_enabled_var", "shadow": "_shadow_enabled_var", "bg": "_bg_enabled_var"}[t], None)
            if var is not None and not var.get():
                var.set(True)
                app._on_fx_toggle(t, refresh_preview=False)
        cb = {"font": "_on_font_color_change", "stroke": "_on_stroke_color_change",
              "shadow": "_on_shadow_color_change", "bg": "_on_bg_color_change"}[t]
        getattr(app, cb)(col)
        app._on_design_change()
        if col not in self.recent:
            self.recent = ([col] + self.recent)[:16]
            for w in self.rec.winfo_children():
                w.destroy()
            for i, c in enumerate(self.recent):
                tk.Button(self.rec, bg=c, activebackground=c, width=2, relief="flat", bd=1,
                          command=lambda c=c: self.set_color(c, apply=True)).grid(row=i // 8, column=i % 8, padx=1, pady=1)


class TfxLayoutMixin:
    """DavinTelopApp に TFX 風のレイアウトを足す（_setup_ui から呼ぶ）。"""

    def _setup_ui_tfx(self):
        self._setup_toolbar()
        self._setup_bottom_bar()
        self._root_pane = ttk.PanedWindow(self, orient=tk.VERTICAL)
        self._root_pane.pack(fill=tk.BOTH, expand=True, padx=2, pady=(2, 2))
        self._main_pane = ttk.PanedWindow(self._root_pane, orient=tk.HORIZONTAL)
        self._root_pane.add(self._main_pane, weight=1)

        # 左: プロパティ（上）・レイヤー = テロップ（下）
        left = ttk.PanedWindow(self._main_pane, orient=tk.VERTICAL)
        self._main_pane.add(left, weight=2)
        self._tfx_left = left
        p_prop = _Panel(left, "プロパティ（テキスト・エフェクト・配置）")
        left.add(p_prop, weight=2)
        self._design_outer = p_prop.body
        self._setup_design_panel(p_prop.body)
        p_layer = _Panel(left, "レイヤー（テロップ）")
        left.add(p_layer, weight=3)
        self._left_frame = p_layer.body
        self._setup_left_panel(p_layer.body)

        # 中央: Program（上）・スタイルプレビュー / プリセットスタイル / 記号（下）
        center = ttk.PanedWindow(self._main_pane, orient=tk.VERTICAL)
        self._main_pane.add(center, weight=6)
        self._right_pane = center
        p_prog = _Panel(center, "Program")
        center.add(p_prog, weight=5)
        self._preview_outer = p_prog.body
        self._setup_preview_panel(p_prog.body)
        strip = ttk.PanedWindow(center, orient=tk.HORIZONTAL)
        center.add(strip, weight=2)
        self._tfx_strip = strip
        p_sp = _Panel(strip, "スタイルプレビュー")
        strip.add(p_sp, weight=2)
        self._style_pv = tk.Canvas(p_sp.body, bg="#2a2a33", highlightthickness=0, height=150)
        self._style_pv.pack(fill="both", expand=True)
        self._style_pv.bind("<Configure>", lambda _e: self._tfx_style_preview())
        p_ps = _Panel(strip, "プリセットスタイル（クリックで使う）")
        strip.add(p_ps, weight=6)
        self._build_preset_grid(p_ps.body)
        p_sym = _Panel(strip, "記号")
        strip.add(p_sym, weight=2)
        self._build_symbols(p_sym.body)

        # 右: シェーダー（色）・ブラウザ（合成 / VoiceVox）
        right = ttk.PanedWindow(self._main_pane, orient=tk.VERTICAL)
        self._main_pane.add(right, weight=2)
        self._tfx_right = right
        p_sh = _Panel(right, "シェーダー（色）")
        right.add(p_sh, weight=3)
        self._color_panel = ColorPanel(p_sh.body, self)
        self._color_panel.pack(fill="both", expand=True)
        p_br = _Panel(right, "ブラウザ（合成・VoiceVox）")
        right.add(p_br, weight=3)
        self._right2_frame = p_br.body
        self._setup_right2_panel(p_br.body)

        self._build_tfx_toolbar()
        self._build_tfx_status()
        self._hide_float_buttons()
        self._init_main_sashes = self._init_tfx_sashes           # 「パネル幅を初期状態に戻す」
        self.after(300, self._init_tfx_sashes)
        self._tfx_last_design = None
        self.after(600, self._tfx_tick)

    # ── 初期の幅 ──
    def _init_tfx_sashes(self):
        try:
            self.update_idletasks()
            w = self._main_pane.winfo_width()
            if w > 900:
                self._main_pane.sashpos(0, int(w * 0.20))
                self._main_pane.sashpos(1, int(w * 0.79))
            h = self._right_pane.winfo_height()
            if h > 400:
                self._right_pane.sashpos(0, int(h * 0.70))
            h2 = self._tfx_left.winfo_height()
            if h2 > 400:
                self._tfx_left.sashpos(0, int(h2 * 0.42))
            h3 = self._tfx_right.winfo_height()
            if h3 > 400:
                self._tfx_right.sashpos(0, int(h3 * 0.50))
            ws = self._tfx_strip.winfo_width()
            if ws > 400:
                self._tfx_strip.sashpos(0, int(ws * 0.22))
                self._tfx_strip.sashpos(1, int(ws * 0.80))
        except tk.TclError:
            pass

    def _hide_float_buttons(self):
        """パネルを切り離すボタン（従来のレイアウト用）を隠す。"""
        def walk(w):
            for c in w.winfo_children():
                try:
                    if isinstance(c, ttk.Button) and str(c.cget("text")).startswith("↗ 分離"):
                        c.pack_forget()
                except tk.TclError:
                    pass
                walk(c)
        walk(self)
        self._tearoff_panel = lambda *_a: messagebox.showinfo(
            "TFX 風レイアウト", "このレイアウトではパネルは固定です（ウィンドウ → レイアウト で従来に戻せます）", parent=self)

    # ── 上のツールバー（フォント・サイズ・縁 / 影 / 背景・揃え）──
    def _build_tfx_toolbar(self):
        tb = ttk.Frame(self._toolbar)
        tb.pack(fill="x", side="top", pady=(0, 2))
        self._tfx_tb = tb
        fonts = [name for name, _ in self._get_fonts_cached()]
        fc = ttk.Combobox(tb, textvariable=self._font_var, values=fonts, width=28)
        fc.pack(side="left", padx=(0, 4))
        fc.bind("<<ComboboxSelected>>", lambda _e: self._on_design_change())
        fc.bind("<Return>", lambda _e: self._on_design_change())
        ttk.Spinbox(tb, from_=8, to=400, textvariable=self._font_size_var, width=5,
                    command=self._on_design_change).pack(side="left")
        ttk.Label(tb, text="px").pack(side="left", padx=(1, 8))
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=4)
        for lab, var, key in (("縁", self._stroke_enabled_var, "stroke"), ("影", self._shadow_enabled_var, "shadow"),
                              ("背景", self._bg_enabled_var, "bg")):
            ttk.Checkbutton(tb, text=lab, variable=var, command=lambda k=key: self._on_fx_toggle(k)).pack(side="left", padx=2)
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=6)
        for sym, val in (("⯇ 左", "left"), ("中央", "center"), ("右 ⯈", "right")):
            ttk.Radiobutton(tb, text=sym, value=val, variable=self._align_var,
                            command=self._on_design_change).pack(side="left", padx=1)
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=6)
        self._safe_guides = tk.BooleanVar(value=True)
        ttk.Checkbutton(tb, text="▦ セーフエリア", variable=self._safe_guides,
                        command=self._schedule_preview_update).pack(side="left", padx=2)
        self._pv_tc_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tb, text="🎬 TeloCut を背景に", variable=self._pv_tc_var,
                        command=self._toggle_program_telocut).pack(side="left", padx=2)
        self._pv_show_bg = tk.BooleanVar(value=True)
        ttk.Checkbutton(tb, text="映像を表示", variable=self._pv_show_bg,
                        command=self._schedule_program_redraw_force).pack(side="left", padx=2)
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(tb, text="＋ テロップ", command=self.add_entry).pack(side="left", padx=2)
        ttk.Button(tb, text="💾 プリセットに保存", command=self._tfx_save_preset).pack(side="left", padx=2)
        ttk.Button(tb, text="⇩ PNG 書き出し", command=self.export_png).pack(side="left", padx=2)

    def _schedule_program_redraw_force(self):
        self._pv_had_bg = True
        self._schedule_program_redraw()

    # ── 下のステータスバー（画面の大きさ・テロップ・位置）──
    def _build_tfx_status(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", side="bottom", before=self._root_pane)
        self._tfx_status = {k: tk.StringVar() for k in ("screen", "telop", "pos", "align")}
        for k in ("align", "pos", "telop", "screen"):
            ttk.Label(bar, textvariable=self._tfx_status[k], foreground="gray").pack(side="right", padx=8)
            ttk.Separator(bar, orient="vertical").pack(side="right", fill="y")

    def _tfx_tick(self):
        """0.5 秒ごと: ステータスバーとスタイルプレビュー（デザインが変わったら）を更新。"""
        try:
            st = self._tfx_status
            st["screen"].set(f"Screen w:{self._cw_var.get()}, h:{self._ch_var.get()}")
            # 合成タブで止めた / 別のソースにしたら「TeloCut を背景に」も外す
            from core.capture import TeloCutFrameThread
            running = isinstance(getattr(self, "_comp_bg_thread", None), TeloCutFrameThread)
            if self._pv_tc_var.get() != running:
                self._pv_tc_var.set(running)
            sel = self._tree.selection() if hasattr(self, "_tree") else ()
            n = len(self.telop_list)
            idx = ""
            if sel:
                ids = [e.id for e in self.telop_list]
                try:
                    idx = str(ids.index(int(sel[0])) + 1)
                except (ValueError, TypeError):
                    idx = "?"
            st["telop"].set(f"テロップ {idx or '-'}/{n}" + (f"（{len(sel)} 個選択）" if len(sel) > 1 else ""))
            st["pos"].set(f"x:{self._offset_x_var.get()}  y:{self._offset_y_var.get()}")
            st["align"].set(f"{self._pos_h_var.get()} / {self._pos_v_var.get()}")
            key = repr(vars(self.design))
            if key != self._tfx_last_design:
                self._tfx_last_design = key
                self._tfx_style_preview()
        except Exception:
            pass
        self.after(500, self._tfx_tick)

    # ── スタイルプレビュー ──
    def _tfx_style_preview(self):
        c = getattr(self, "_style_pv", None)
        if c is None:
            return
        try:
            from PIL import Image, ImageTk
            from core.renderer import render_telop_image
            import copy
            d = copy.copy(self.design)
            d.canvas_width, d.canvas_height = 900, 300
            d.position_h, d.position_v, d.offset_x, d.offset_y = "center", "middle", 0, 0
            d.font_size = 150
            img = render_telop_image("あア", d)
            box = img.getbbox() or (0, 0, 1, 1)
            img = img.crop(box)
            cw, ch = max(10, c.winfo_width()), max(10, c.winfo_height())
            k = min((cw - 16) / max(1, img.width), (ch - 16) / max(1, img.height), 1.0)
            img = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), Image.LANCZOS)
            self._style_pv_img = ImageTk.PhotoImage(img)
            c.delete("all")
            c.create_image(cw // 2, ch // 2, image=self._style_pv_img)
        except Exception as ex:
            c.delete("all")
            c.create_text(10, 10, anchor="nw", text=str(ex)[:60], fill="#c66")

    # ── プリセットスタイルの一覧（組み込み S1… ＋ 保存したプリセット）──
    def _build_preset_grid(self, parent):
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True)
        cv = tk.Canvas(outer, bg="#1e1e24", highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        self._preset_frame = tk.Frame(cv, bg="#1e1e24")
        cv.create_window(0, 0, anchor="nw", window=self._preset_frame)
        self._preset_frame.bind("<Configure>", lambda _e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda _e: self.after(50, self._fill_preset_grid))
        self._preset_canvas = cv
        self._preset_imgs: List[object] = []
        self._preset_cols = 0

    def _style_design(self, spec) -> object:
        import copy
        name, fc, se, sc, sw, sh, bg, bgc = spec
        d = copy.copy(self.design)
        d.font_color, d.stroke_enabled, d.stroke_color, d.stroke_width = fc, se, sc, sw
        d.shadow_enabled, d.bg_enabled, d.bg_color = sh, bg, bgc
        return d

    def _preset_items(self) -> List[Tuple[str, Callable[[], object], bool]]:
        out = [(spec[0], (lambda s=spec: self._style_design(s)), True) for spec in BUILTIN_STYLES]
        for name in self.preset_manager.list_presets():
            out.append((name, (lambda n=name: self.preset_manager.load_preset(n)), False))
        return out

    def _fill_preset_grid(self, force: bool = False):
        cv = getattr(self, "_preset_canvas", None)
        if cv is None:
            return
        cols = max(1, (cv.winfo_width() - 4) // 86)
        items = self._preset_items()
        sig = (cols, tuple(n for n, _f, _b in items))
        if sig == getattr(self, "_preset_sig", None) and not force:
            return
        self._preset_sig = sig
        for w in self._preset_frame.winfo_children():
            w.destroy()
        self._preset_imgs = []
        try:
            from PIL import Image, ImageTk
            from core.renderer import render_telop_image
        except ImportError:
            return
        import copy
        for i, (name, get, builtin) in enumerate(items):
            try:
                d = copy.copy(get())
                d.canvas_width, d.canvas_height = 260, 220
                d.position_h, d.position_v, d.offset_x, d.offset_y = "center", "middle", 0, 0
                d.font_size = 150
                img = render_telop_image("あ", d)
                box = img.getbbox() or (0, 0, 1, 1)
                img = img.crop(box)
                k = min(70 / max(1, img.width), 58 / max(1, img.height))
                img = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), Image.LANCZOS)
                tile = Image.new("RGBA", (80, 64), (12, 12, 16, 255))
                tile.alpha_composite(img, ((80 - img.width) // 2, (64 - img.height) // 2))
                ph = ImageTk.PhotoImage(tile)
            except Exception:
                ph = None
            self._preset_imgs.append(ph)
            cell = tk.Frame(self._preset_frame, bg="#1e1e24", bd=1, relief="flat")
            cell.grid(row=i // cols, column=i % cols, padx=2, pady=2)
            b = tk.Button(cell, image=ph, bg="#0c0c10", activebackground="#2a5bd7", relief="flat", bd=1,
                          command=lambda n=name, g=get, bi=builtin: self._use_preset_item(n, g, bi))
            b.pack()
            tk.Label(cell, text=name[:10], bg="#1e1e24", fg="#bbbbc4", font=("", 8)).pack()

    def _use_preset_item(self, name: str, get, builtin: bool):
        if builtin:
            d = get()
            self.design = d
            self._apply_design_to_ui(d)
            self._on_design_change()
            self._progress_var.set(f"スタイル「{name}」を使いました")
        else:
            self.load_preset(name)
        cp = getattr(self, "_color_panel", None)
        if cp is not None:
            cp.load_target()

    def _tfx_save_preset(self):
        from tkinter import simpledialog
        name = simpledialog.askstring("プリセットに保存", "名前:", parent=self)
        if name and name.strip():
            self.preset_manager.save_preset(name.strip(), self.design)
            try:
                self._refresh_preset_menu()
            except Exception:
                pass
            self._fill_preset_grid(force=True)

    # ── 記号（TFX の SVG セレクタの代わり）──
    def _build_symbols(self, parent):
        f = ttk.Frame(parent, padding=2)
        f.pack(fill="both", expand=True)
        for i, s in enumerate(SYMBOLS):
            tk.Button(f, text=s, width=2, relief="flat", bd=1, font=("", 12),
                      command=lambda s=s: self.insert_symbol(s)).grid(row=i // 6, column=i % 6, padx=1, pady=1)

    def insert_symbol(self, s: str) -> str:
        """記号を入れる: 文字を打っている欄があればカーソルの所に・無ければ選んだテロップの最後に。"""
        w = self.focus_get()
        try:
            if isinstance(w, (tk.Entry, ttk.Entry)):
                w.insert("insert", s)
                return "entry"
            if isinstance(w, tk.Text):
                w.insert("insert", s)
                return "text"
        except tk.TclError:
            pass
        t = getattr(self, "_insp_text", None)
        if t is not None and str(t.cget("state")) != "disabled":
            t.insert("end-1c", s)
            try:
                self._insp_commit_text()
            except Exception:
                pass
            return "inspector"
        return ""
