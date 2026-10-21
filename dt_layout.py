"""DavinTelop のテロップ専用機風レイアウト（TFX-Artist / Nixia / Neo-n の画面の並びを参考に、DaVinci・TeloCut 向けに）。

  ┌ メニュー ──────────────────────────────────────────────────────┐
  │ いつものツールバー                                                    │
  │ フォント ▾ | サイズ | 縁・影・背景 | 揃え | セーフエリア | TeloCut を背景に … │
  │ 出力: [HD][4K][縦][正方形][TeloCut に合わせる]    [⚡Resolve][📍][⬆TeloCut][⬇][🔗] │
  ├─┬───────────┬──────────────────────────┬─────────────┤
  │ツ│ プロパティ │ Program（大きなプレビュー・セーフエリア）  │ シェーダー  │
  │ー│（レイヤー・ │  TeloCut の絵を敷いて、その上で作れる     │ カラー /    │
  │ル│ テキスト・ │                                          │ グラデーション│
  │ │ エフェクト・├────────┬─────────────┬───────┤             │
  │ │ 装飾・配置）│ スタイル │ プリセットスタイル S1… │ パーツ │─────────────┤
  │ ├───────────┤ プレビュー│  あ あ あ あ …           │ 記号  │ ブラウザ    │
  │ │ レイヤー   │  あア    │                          │ 見出し│（合成・声）  │
  │ │（サムネイル │          │                          │ …    │             │
  │ │ / 一覧）    │          │                          │      │             │
  ├─┴───────────┴────────┴─────────────┴───────┴─────────────┤
  │ メッセージ … │ Screen w:1920 h:1080 │ Layer │ テロップ 3/12 │ x:0 y:80 │ 変形 │
  └──────────────────────────────────────────────────────────────────┘
従来のレイアウトには「ウィンドウ → レイアウト」で戻せる（次の起動から）。
"""
from __future__ import annotations

import colorsys
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, List, Tuple

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
# 装飾つきの組み込みスタイル（テレビ・YouTube の定番）: (名前, {DesignSettings の上書き})
FANCY_STYLES = [
    ("T1 二重縁", {"font_color": "#FFFFFF", "stroke_enabled": True, "stroke_color": "#D81E1E", "stroke_width": 6,
                  "stroke2_enabled": True, "stroke2_color": "#FFFFFF", "stroke2_width": 6, "shadow_enabled": True}),
    ("T2 金グラデ", {"font_color": "#FFF6B0", "grad_enabled": True, "grad_color": "#C88A00", "grad_angle": 0,
                    "stroke_enabled": True, "stroke_color": "#3A2000", "stroke_width": 6,
                    "stroke2_enabled": True, "stroke2_color": "#FFFFFF", "stroke2_width": 4}),
    ("T3 光彩", {"font_color": "#FFFFFF", "stroke_enabled": True, "stroke_color": "#0A6A50", "stroke_width": 4,
                "glow_enabled": True, "glow_color": "#66FFCC", "glow_size": 14}),
    ("T4 ニュース帯", {"font_color": "#FFFFFF", "stroke_enabled": False, "bg_enabled": True, "bg_color": "#0A2A6E",
                     "bg_opacity": 220, "bg_shape": "band"}),
    ("T5 角丸", {"font_color": "#222222", "stroke_enabled": False, "bg_enabled": True, "bg_color": "#FFFFFF",
                "bg_opacity": 235, "bg_shape": "round"}),
    ("T6 炎", {"font_color": "#FFE14D", "grad_enabled": True, "grad_color": "#FF3000", "grad_angle": 0,
              "stroke_enabled": True, "stroke_color": "#FFFFFF", "stroke_width": 6,
              "stroke2_enabled": True, "stroke2_color": "#000000", "stroke2_width": 4}),
    ("T7 ネオン", {"font_color": "#FFFFFF", "stroke_enabled": True, "stroke_color": "#FF3FA8", "stroke_width": 3,
                  "glow_enabled": True, "glow_color": "#FF3FA8", "glow_size": 16}),
    ("T8 斜体", {"font_color": "#FFFFFF", "stroke_enabled": True, "stroke_color": "#000000", "stroke_width": 8,
                "skew": 12.0}),
]
SWATCHES = ["#FFFFFF", "#000000", "#FF0000", "#00FF00", "#0000FF", "#00FFFF", "#FF00FF", "#FFFF00",
            "#FF8000", "#8000FF", "#00A0FF", "#FF5090", "#808080", "#C0C0C0", "#7F0000", "#004C00",
            "#00007F", "#FFE14D", "#3CFF6E", "#9FE8FF", "#F3C8FF", "#D81E1E", "#14205A", "#4A3000"]
SYMBOLS = list("○●◎△▲□■◇◆☆★♡♥♪♫※！？→←↑↓⇒〜…「」『』【】（）・♂♀✕✔☀☁☂☃☎✉♨")
# パーツ（Neo-n のパーツキャビネットのように種類ごと）
PARTS = {
    "記号": SYMBOLS,
    "矢印": list("→←↑↓↗↘↙↖⇒⇐⇔⇧⇩➡⬅⬆⬇▶◀▲▼▷◁△▽"),
    "数字": list("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"),
    "見出し": ["【速報】", "【LIVE】", "【悲報】", "【朗報】", "【衝撃】", "【検証】", "【解説】", "【注意】",
             "【PR】", "【Q】", "【A】", "【結論】", "【まとめ】", "【神回】", "【ネタバレ】", "※個人の感想です",
             "※イメージです", "つづく", "CMのあと", "このあとすぐ"],
    "ネット": ["ｗ", "ｗｗｗ", "（笑）", "！？", "!!", "（汗）", "（白目）", "ﾄﾞﾔｧ", "(´・ω・｀)", "m(_ _)m",
             "( ﾟДﾟ)", "(*´▽｀*)", "♪", "♡", "…"],
    "単位": ["℃", "%", "㎏", "㎞", "㎝", "㎜", "円", "＄", "€", "￥", "秒", "分", "時間", "万", "億", "兆", "No.", "♯"],
}
COLOR_TARGETS = [("font", "文字"), ("stroke", "縁取り"), ("shadow", "影"), ("bg", "背景"),
                 ("grad", "グラデ"), ("stroke2", "二重縁"), ("glow", "光彩")]
# 追加の装飾の色: 対象 → (色の項目, オンにする項目)
XD_TARGET = {"grad": ("grad_color", "grad_enabled"), "stroke2": ("stroke2_color", "stroke2_enabled"),
             "glow": ("glow_color", "glow_enabled")}
# 装飾の項目（DesignSettings に足したもの）
XD_FIELDS = [("grad_enabled", bool), ("grad_color", str), ("grad_angle", int),
             ("stroke2_enabled", bool), ("stroke2_color", str), ("stroke2_width", int),
             ("glow_enabled", bool), ("glow_color", str), ("glow_size", int), ("bg_shape", str),
             ("scale_x", int), ("scale_y", int), ("rotation", float), ("skew", float), ("vertical", bool)]
GRAD_PRESETS = [("金", "#FFF6B0", "#C88A00"), ("銀", "#FFFFFF", "#8A9AA8"), ("炎", "#FFE14D", "#FF3000"),
                ("海", "#B8F4FF", "#0060D0"), ("桜", "#FFFFFF", "#FF6FA8"), ("若葉", "#EFFFB0", "#2DB84A"),
                ("夕焼け", "#FFC24D", "#B0207A"), ("夜", "#D8D8FF", "#3A2A8C")]
# 出力形式（Nixia の HD / 4K… のボタン）: (名前, 幅, 高さ)
FORMATS = [("HD", 1920, 1080), ("4K", 3840, 2160), ("縦 9:16", 1080, 1920), ("正方形", 1080, 1080)]
ACCENT = "#E8731A"      # Nixia のようなオレンジ
PANEL_BG = "#1f1f24"


def _hex(rgb: Tuple[float, float, float]) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(c * 255)))) for c in rgb)


def _rgb(hexs: str) -> Tuple[float, float, float]:
    h = (hexs or "#FFFFFF").lstrip("#")
    try:
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return (1.0, 1.0, 1.0)


class _Panel(ttk.Frame):
    """タイトルの帯付きのパネル（左にアクセントの線・タイトル）。"""

    def __init__(self, parent, title: str):
        super().__init__(parent)
        bar = tk.Frame(self, height=20, bg=PANEL_BG)
        bar.pack(fill="x")
        tk.Frame(bar, width=3, bg=ACCENT).pack(side="left", fill="y")
        tk.Label(bar, text=title, bg=PANEL_BG, fg="#c8c8d0", font=("", 9, "bold")).pack(side="left", padx=6, pady=1)
        self.bar = bar
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
        for i, (key, lab) in enumerate(COLOR_TARGETS):
            ttk.Radiobutton(row, text=lab, value=key, variable=self.target,
                            command=self.load_target).grid(row=i // 4, column=i % 4, sticky="w", padx=2)
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
        ttk.Button(hx, text="選ぶ…", command=self._pick_dialog).pack(side="left")
        ttk.Button(hx, text="💧 Program から", command=lambda: app._tool_eyedrop()).pack(side="left", padx=2)
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
                "bg": "_bg_color_btn"}.get(self.target.get(), "")

    def load_target(self):
        t = self.target.get()
        if t in XD_TARGET:
            var = getattr(self.app, "_xd", {}).get(XD_TARGET[t][0])
            if var is not None:
                self.set_color(var.get(), apply=False)
            return
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
        """選んだ対象（文字 / 縁取り / 影 / 背景 / グラデ / 二重縁 / 光彩）に色を付ける。文字以外はオンにする。"""
        app = self.app
        t = self.target.get()
        if t in XD_TARGET:
            xd = getattr(app, "_xd", {})
            ck, fk = XD_TARGET[t]
            if ck in xd:
                xd[ck].set(col)
                xd[fk].set(True)
            self._remember(col)
            return
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
        self._remember(col)

    def _remember(self, col: str):
        if col in self.recent:
            return
        self.recent = ([col] + self.recent)[:16]
        for w in self.rec.winfo_children():
            w.destroy()
        for i, c in enumerate(self.recent):
            tk.Button(self.rec, bg=c, activebackground=c, width=2, relief="flat", bd=1,
                      command=lambda c=c: self.set_color(c, apply=True)).grid(row=i // 8, column=i % 8, padx=1, pady=1)


class LayerBrowser(ttk.Frame):
    """レイヤーブラウザ（TFX のように、テロップをサムネイルで並べる）。👁 で有効 / 無効、クリックで選ぶ。"""
    ROW = 50
    TW, TH = 112, 42

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.cv = tk.Canvas(self, bg="#16161b", highlightthickness=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview)
        self.cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.cv.pack(side="left", fill="both", expand=True)
        self.cv.bind("<Button-1>", self._click)
        self.cv.bind("<Double-Button-1>", self._dclick)
        self.cv.bind("<MouseWheel>", lambda e: self.cv.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.cv.bind("<Configure>", lambda _e: self.refresh(force=True))
        self.thumbs = {}            # (文字, スタイル, デザイン) → PhotoImage
        self._sig = None
        self._queue: List[tuple] = []
        self._rows: List[object] = []

    def _key(self, e):
        return (e.text, repr(sorted((e.style or {}).items())), repr(vars(self.app.design)))

    def refresh(self, force: bool = False):
        app = self.app
        sel = tuple(app._tree.selection()) if hasattr(app, "_tree") else ()
        sig = (tuple((e.id, e.text, e.enabled, e.start_time, repr(e.style)) for e in app.telop_list),
               repr(vars(app.design)), sel, self.cv.winfo_width())
        if sig == self._sig and not force:
            return
        self._sig = sig
        cv = self.cv
        cv.delete("all")
        w = max(200, cv.winfo_width())
        self._rows = list(app.telop_list)
        self._queue = []
        for i, e in enumerate(self._rows):
            y = i * self.ROW
            on = str(e.id) in sel
            cv.create_rectangle(0, y, w, y + self.ROW - 2, fill="#2a5bd7" if on else "#202027", outline="")
            cv.create_text(14, y + self.ROW // 2, text="👁" if e.enabled else "－",
                           fill="#e0e0e8" if e.enabled else "#666", font=("", 11))
            cv.create_rectangle(28, y + 3, 28 + self.TW, y + 3 + self.TH, fill="#0c0c10", outline="#3a3a44")
            k = self._key(e)
            ph = self.thumbs.get(k)
            if ph is not None:
                cv.create_image(28 + self.TW // 2, y + 3 + self.TH // 2, image=ph)
            else:
                self._queue.append((i, e, k))
            fg = "#ffffff" if e.enabled else "#777"
            cv.create_text(28 + self.TW + 8, y + 12, anchor="w", text=f"{i + 1}  {e.start_time}",
                           fill="#9aa0b0", font=("", 8))
            cv.create_text(28 + self.TW + 8, y + 31, anchor="w", text=e.text.replace("\n", " ⏎ ")[:28],
                           fill=fg, font=("", 9))
        cv.configure(scrollregion=(0, 0, w, max(1, len(self._rows)) * self.ROW))
        if self._queue:
            self.after(5, self._render_next)

    def _render_next(self):
        if not self._queue:
            return
        i, e, k = self._queue.pop(0)
        if k not in self.thumbs:
            self.thumbs[k] = self._thumb(e)
        ph = self.thumbs[k]
        if ph is not None and i < len(self._rows) and self._rows[i] is e:
            y = i * self.ROW
            self.cv.create_image(28 + self.TW // 2, y + 3 + self.TH // 2, image=ph)
        if self._queue:
            self.after(5, self._render_next)
        if len(self.thumbs) > 400:
            self.thumbs.clear()

    def _thumb(self, e):
        try:
            from PIL import Image, ImageTk
            from core.renderer import render_telop_image
            d = self.app.design.with_overrides(e.style)
            img = render_telop_image(e.text or " ", d)
            box = img.getbbox()
            if not box:
                return None
            img = img.crop(box)
            k = min((self.TW - 6) / img.width, (self.TH - 6) / img.height)
            img = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _row_at(self, ev):
        y = self.cv.canvasy(ev.y)
        i = int(y // self.ROW)
        return (i, self._rows[i]) if 0 <= i < len(self._rows) else (-1, None)

    def _click(self, ev):
        i, e = self._row_at(ev)
        if e is None:
            return
        app = self.app
        if ev.x < 28:
            e.enabled = not e.enabled
            app._refresh_tree()
        else:
            iid = str(e.id)
            if app._tree.exists(iid):
                app._tree.selection_set(iid)
                app._tree.see(iid)
        self.refresh(force=True)

    def _dclick(self, ev):
        i, e = self._row_at(ev)
        t = getattr(self.app, "_insp_text", None)
        if e is not None and t is not None:
            t.focus_set()


class TfxLayoutMixin:
    """DavinTelopApp にテロップ専用機風のレイアウトを足す（_setup_ui から呼ぶ）。"""

    # ── 装飾の項目（グラデーション・二重縁・光彩・変形・縦書き）──
    def _create_extra_design_vars(self):
        if getattr(self, "_xd", None):
            return
        mk = {bool: tk.BooleanVar, str: tk.StringVar, int: tk.IntVar, float: tk.DoubleVar}
        self._xd = {}
        for k, t in XD_FIELDS:
            v = mk[t](value=getattr(self.design, k, t()))
            v.trace_add("write", lambda *_a: self._xd_changed())
            self._xd[k] = v
        self._xd_lock = tk.BooleanVar(value=True)       # 縦横の拡大率をそろえる
        self._xd["scale_x"].trace_add("write", lambda *_a: self._xd_follow_scale())

    def _xd_changed(self):
        if hasattr(self, "_font_size_var"):
            self._on_design_change()

    def _xd_follow_scale(self):
        if self._xd_lock.get():
            try:
                v = self._xd["scale_x"].get()
            except (tk.TclError, ValueError):
                return
            if self._xd["scale_y"].get() != v:
                self._xd["scale_y"].set(v)

    def _collect_extra_design(self, s):
        for k, v in getattr(self, "_xd", {}).items():
            try:
                setattr(s, k, v.get())
            except (tk.TclError, ValueError):
                pass

    def _apply_extra_design(self, s):
        xd = getattr(self, "_xd", None)
        if not xd:
            return
        lock = self._xd_lock.get()
        self._xd_lock.set(False)
        for k, v in xd.items():
            v.set(getattr(s, k, v.get()))
        self._xd_lock.set(lock and s.scale_x == s.scale_y)

    def _setup_ui_tfx(self):
        self._setup_toolbar()
        self._setup_bottom_bar()
        self._build_tool_palette()
        self._root_pane = ttk.PanedWindow(self, orient=tk.VERTICAL)
        self._root_pane.pack(fill=tk.BOTH, expand=True, padx=2, pady=(2, 2))
        self._main_pane = ttk.PanedWindow(self._root_pane, orient=tk.HORIZONTAL)
        self._root_pane.add(self._main_pane, weight=1)

        # 左: プロパティ（上）・レイヤー = テロップ（下）
        left = ttk.PanedWindow(self._main_pane, orient=tk.VERTICAL)
        self._main_pane.add(left, weight=2)
        self._tfx_left = left
        p_prop = _Panel(left, "プロパティ（レイヤー・テキスト・エフェクト・装飾・配置）")
        left.add(p_prop, weight=2)
        self._design_outer = p_prop.body
        self._setup_design_panel(p_prop.body)
        self._add_property_tabs(p_prop.body)
        p_layer = _Panel(left, "レイヤー（テロップ）")
        left.add(p_layer, weight=3)
        lnb = ttk.Notebook(p_layer.body)
        lnb.pack(fill="both", expand=True)
        t_list = ttk.Frame(lnb)
        lnb.add(t_list, text=" 一覧 ")
        self._left_frame = t_list
        self._setup_left_panel(t_list)
        self._layer_browser = LayerBrowser(lnb, self)
        lnb.add(self._layer_browser, text=" サムネイル ")
        self._layer_nb = lnb

        # 中央: Program（上）・スタイルプレビュー / プリセットスタイル / パーツ（下）
        center = ttk.PanedWindow(self._main_pane, orient=tk.VERTICAL)
        self._main_pane.add(center, weight=6)
        self._right_pane = center
        p_prog = _Panel(center, "Program")
        center.add(p_prog, weight=5)
        self._preview_outer = p_prog.body
        self._setup_preview_panel(p_prog.body)
        self._preview_canvas.bind("<Button-1>", self._tfx_canvas_click, add="+")
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
        p_sym = _Panel(strip, "パーツ（クリックで入れる）")
        strip.add(p_sym, weight=2)
        self._build_symbols(p_sym.body)

        # 右: シェーダー（カラー / グラデーション）・ブラウザ（合成 / VoiceVox）
        right = ttk.PanedWindow(self._main_pane, orient=tk.VERTICAL)
        self._main_pane.add(right, weight=2)
        self._tfx_right = right
        p_sh = _Panel(right, "シェーダー・合成")
        right.add(p_sh, weight=3)
        snb = ttk.Notebook(p_sh.body)
        snb.pack(fill="both", expand=True)
        self._shader_nb = snb
        self._color_panel = ColorPanel(snb, self)
        snb.add(self._color_panel, text=" カラー ")
        snb.add(self._build_gradient_tab(snb), text=" グラデーション ")
        # 合成（背景ソース）と VoiceVox はタブに（合成の絵は Program に出るので、別の小さなプレビューは出さない）
        br = ttk.Frame(snb)
        snb.add(br, text=" 合成・声 ")
        self._right2_frame = br
        self._setup_right2_panel(br)
        cc = getattr(self, "_comp_canvas", None)
        if cc is not None:
            cc.pack_forget()
            ttk.Label(cc.master, text="合成の絵は Program（真ん中）に出ます。NDI 出力も Program と同じ背景＋テロップです。",
                      foreground="gray", wraplength=300, justify="left").pack(anchor="w", padx=6, pady=4,
                                                                             before=cc.master.winfo_children()[0])

        self._build_tfx_toolbar()
        self._build_output_bar()
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
                self._main_pane.sashpos(0, int(w * 0.21))
                self._main_pane.sashpos(1, int(w * 0.79))
            h = self._right_pane.winfo_height()
            if h > 400:
                self._right_pane.sashpos(0, int(h * 0.70))
            h2 = self._tfx_left.winfo_height()
            if h2 > 400:
                self._tfx_left.sashpos(0, int(h2 * 0.46))
            ws = self._tfx_strip.winfo_width()
            if ws > 400:
                self._tfx_strip.sashpos(0, int(ws * 0.20))
                self._tfx_strip.sashpos(1, int(ws * 0.74))
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
            "テロップ専用機風レイアウト", "このレイアウトではパネルは固定です（ウィンドウ → レイアウト で従来に戻せます）", parent=self)

    def _tip(self, w, text):
        tip = getattr(sys.modules.get(type(self).__module__), "_Tooltip", None)
        if tip is not None:
            tip(w, text)

    # ── 左端のツールパレット（Nixia のような縦の道具箱）──
    def _build_tool_palette(self):
        pal = tk.Frame(self, bg=PANEL_BG, width=34)
        pal.pack(side="left", fill="y", padx=(2, 0), pady=2)
        self._tool_palette = pal
        self._eyedrop = False
        tools = [
            ("↖", "移動: Program をドラッグ・矢印キーで動かす", lambda: self._preview_canvas.focus_set()),
            ("💧", "スポイト: Program（TeloCut の絵など）をクリックして色を取る（シェーダーで選んでいる所に）",
             self._tool_eyedrop),
            ("T", "＋ テロップ", self.add_entry),
            None,
            ("⊕", "まん中へ（位置をリセット）", lambda: self._tool_place("center", "middle")),
            ("⤒", "上に置く", lambda: self._tool_place(None, "top")),
            ("⤓", "下に置く", lambda: self._tool_place(None, "bottom")),
            None,
            ("縦", "縦書き の切り替え", lambda: self._xd["vertical"].set(not self._xd["vertical"].get())),
            ("⟲", "変形をリセット（拡大率 100%・回転 0・せん断 0）", self._tool_reset_transform),
            ("▦", "セーフエリアの表示", self._tool_toggle_safe),
            None,
            ("🎬", "TeloCut の絵を Program に敷く / やめる", self._tool_toggle_telocut),
            ("⚡", "テロップを作ったらすぐ再生ヘッド（TeloCut / Resolve）に配置する・しない", self._tool_toggle_place_now),
        ]
        self._tool_btns = {}
        for t in tools:
            if t is None:
                tk.Frame(pal, height=1, bg="#3a3a44").pack(fill="x", padx=4, pady=4)
                continue
            sym, tip, fn = t
            b = tk.Button(pal, text=sym, width=2, relief="flat", bd=0, bg=PANEL_BG, fg="#e0e0e8",
                          activebackground=ACCENT, activeforeground="white", font=("", 12), command=fn)
            b.pack(padx=2, pady=1)
            self._tip(b, tip)
            self._tool_btns[sym] = b

    def _tool_eyedrop(self):
        self._eyedrop = True
        try:
            self._preview_canvas.configure(cursor="crosshair")
            self._tool_btns["💧"].configure(bg=ACCENT)
        except (tk.TclError, KeyError, AttributeError):
            pass
        self._progress_var.set("スポイト: Program の中をクリックすると、その色を使います（シェーダーで選んでいる所に）")

    def _tfx_canvas_click(self, ev):
        if not getattr(self, "_eyedrop", False):
            return
        col = self._program_color_at(ev.x, ev.y)
        self._eyedrop = False
        self._preview_canvas.configure(cursor="")
        self._tool_btns["💧"].configure(bg=PANEL_BG)
        if col:
            self._color_panel.set_color(col, apply=True)
            self._progress_var.set(f"スポイト: {col} を {dict(COLOR_TARGETS)[self._color_panel.target.get()]} に使いました")
        return "break"

    def _program_color_at(self, x: int, y: int) -> str:
        """Program に描いている絵の (x, y) の色（"#RRGGBB"・外なら ""）。"""
        photo = getattr(self, "_preview_image_ref", None)
        if photo is None:
            return ""
        px, py = int(x - self._canvas_ox), int(y - self._canvas_oy)
        if not (0 <= px < photo.width() and 0 <= py < photo.height()):
            return ""
        v = self.tk.call(str(photo), "get", px, py)
        rgb = [int(c) for c in (v.split() if isinstance(v, str) else v)][:3]
        return "#%02X%02X%02X" % tuple(rgb)

    def _tool_place(self, h, v):
        if h:
            self._pos_h_var.set(h)
            self._offset_x_var.set(0)
        self._pos_v_var.set(v)
        self._offset_y_var.set(0 if v == "middle" else 60)
        self._on_design_change()

    def _tool_reset_transform(self):
        for k, val in (("scale_x", 100), ("scale_y", 100), ("rotation", 0.0), ("skew", 0.0)):
            self._xd[k].set(val)

    def _tool_toggle_safe(self):
        self._safe_guides.set(not self._safe_guides.get())
        self._schedule_preview_update()

    def _tool_toggle_place_now(self):
        self._place_now.set(not self._place_now.get())
        self._save_app_config()
        self._progress_var.set("テロップを作ったらすぐ再生ヘッドに配置: " + ("する" if self._place_now.get() else "しない"))

    def _tool_toggle_telocut(self):
        self._pv_tc_var.set(not self._pv_tc_var.get())
        self._toggle_program_telocut()

    # ── プロパティに「レイヤー」「装飾」のタブを足す ──
    def _add_property_tabs(self, parent):
        nb = next((w for w in parent.winfo_children() if isinstance(w, ttk.Notebook)), None)
        if nb is None:
            return
        self._prop_nb = nb
        xd = self._xd
        pad = dict(padx=4, pady=3)

        # レイヤー（TFX の Layer: 位置・拡大率・回転・せん断・縦書き）
        lay = ttk.Frame(nb, padding=8)
        nb.insert(0, lay, text=" レイヤー ")
        lay.columnconfigure(1, weight=1)

        def num(row, label, var, lo, hi, inc=1, unit=""):
            ttk.Label(lay, text=label).grid(row=row, column=0, sticky="e", **pad)
            f = ttk.Frame(lay)
            f.grid(row=row, column=1, sticky="ew", **pad)
            ttk.Spinbox(f, from_=lo, to=hi, increment=inc, textvariable=var, width=7).pack(side="left")
            ttk.Scale(f, from_=lo, to=hi, variable=var, orient="horizontal",
                      command=lambda _v, var=var, inc=inc: var.set(round(float(var.get()) / inc) * inc if inc < 1
                                                                   else int(float(var.get())))).pack(side="left", fill="x", expand=True, padx=6)
            ttk.Label(f, text=unit, foreground="gray").pack(side="left")

        num(0, "位置 X:", self._offset_x_var, -1920, 1920, 1, "px")
        num(1, "位置 Y:", self._offset_y_var, -1080, 1080, 1, "px")
        num(2, "拡大率 横:", xd["scale_x"], 10, 400, 1, "%")
        num(3, "拡大率 縦:", xd["scale_y"], 10, 400, 1, "%")
        ttk.Checkbutton(lay, text="🔗 縦横をそろえる", variable=self._xd_lock).grid(row=4, column=1, sticky="w", **pad)
        num(5, "回転:", xd["rotation"], -180, 180, 0.5, "°")
        num(6, "せん断:", xd["skew"], -45, 45, 0.5, "°")
        ttk.Checkbutton(lay, text="縦書き", variable=xd["vertical"]).grid(row=7, column=1, sticky="w", **pad)
        ttk.Button(lay, text="⟲ 変形をリセット", command=self._tool_reset_transform).grid(row=8, column=1, sticky="w", **pad)

        # 装飾（二重縁・光彩・背景の形）
        deco = ttk.Frame(nb, padding=8)
        nb.add(deco, text=" 装飾 ")
        deco.columnconfigure(1, weight=1)
        r = 0
        for title, flag, ck, wk, lo, hi in (("二重縁（縁取りの外側）", "stroke2_enabled", "stroke2_color", "stroke2_width", 1, 30),
                                           ("光彩（まわりを光らせる）", "glow_enabled", "glow_color", "glow_size", 2, 60)):
            ttk.Checkbutton(deco, text=title, variable=xd[flag]).grid(row=r, column=0, columnspan=2, sticky="w", pady=(6, 2))
            r += 1
            ttk.Label(deco, text="色:").grid(row=r, column=0, sticky="e", **pad)
            self._xd_color_button(deco, ck, flag).grid(row=r, column=1, sticky="w", **pad)
            r += 1
            ttk.Label(deco, text="幅:" if "stroke" in wk else "広さ:").grid(row=r, column=0, sticky="e", **pad)
            ttk.Spinbox(deco, from_=lo, to=hi, textvariable=xd[wk], width=6).grid(row=r, column=1, sticky="w", **pad)
            r += 1
        ttk.Label(deco, text="背景の形:").grid(row=r, column=0, sticky="e", pady=(8, 2))
        f = ttk.Frame(deco)
        f.grid(row=r, column=1, sticky="w", pady=(8, 2))
        for val, lab in (("rect", "四角"), ("round", "角丸"), ("band", "横いっぱいの帯")):
            ttk.Radiobutton(f, text=lab, value=val, variable=xd["bg_shape"]).pack(side="left", padx=2)
        r += 1
        ttk.Label(deco, text="（背景は「エフェクト → 背景」をオンにすると出ます）", foreground="gray").grid(
            row=r, column=0, columnspan=2, sticky="w")
        nb.select(lay)

    def _xd_color_button(self, parent, key: str, flag: str = ""):
        var = self._xd[key]
        b = tk.Button(parent, width=4, relief="flat", bd=1, bg=var.get(), activebackground=var.get())

        def pick():
            from tkinter import colorchooser
            r = colorchooser.askcolor(color=var.get(), parent=self)
            if r and r[1]:
                var.set(r[1].upper())
                if flag:
                    self._xd[flag].set(True)
        b.configure(command=pick)

        def show(*_a):
            try:
                b.configure(bg=var.get(), activebackground=var.get())
            except tk.TclError:
                pass
        var.trace_add("write", show)
        return b

    # ── シェーダー: グラデーション ──
    def _build_gradient_tab(self, parent):
        xd = self._xd
        f = ttk.Frame(parent, padding=6)
        ttk.Checkbutton(f, text="文字をグラデーションにする", variable=xd["grad_enabled"]).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="色 1（文字色）").pack(side="left")
        ttk.Button(row, text="選ぶ", width=5, command=lambda: self._shader_target("font")).pack(side="left", padx=4)
        ttk.Label(row, text="色 2").pack(side="left", padx=(8, 0))
        self._xd_color_button(row, "grad_color", "grad_enabled").pack(side="left", padx=4)
        self._grad_bar = tk.Canvas(f, height=26, highlightthickness=1, highlightbackground="#555")
        self._grad_bar.pack(fill="x", pady=4)
        self._grad_bar.bind("<Configure>", lambda _e: self._draw_grad_bar())
        ar = ttk.Frame(f)
        ar.pack(fill="x")
        ttk.Label(ar, text="向き:").pack(side="left")
        for sym, ang in (("↓", 0), ("↘", 45), ("→", 90), ("↗", 135)):
            ttk.Button(ar, text=sym, width=3, command=lambda a=ang: xd["grad_angle"].set(a)).pack(side="left", padx=1)
        ttk.Spinbox(ar, from_=0, to=359, textvariable=xd["grad_angle"], width=5).pack(side="left", padx=6)
        ttk.Label(ar, text="°", foreground="gray").pack(side="left")
        ttk.Label(f, text="グラデーションの見本（クリックで使う）").pack(anchor="w", pady=(8, 2))
        g = ttk.Frame(f)
        g.pack(fill="x")
        self._grad_imgs = []
        for i, (name, c1, c2) in enumerate(GRAD_PRESETS):
            cv = tk.Canvas(g, width=64, height=30, highlightthickness=0, cursor="hand2")
            cv.grid(row=i // 4, column=i % 4, padx=2, pady=2)
            for y in range(30):
                t = y / 29
                a, b = _rgb(c1), _rgb(c2)
                cv.create_line(0, y, 64, y, fill=_hex(tuple(a[j] * (1 - t) + b[j] * t for j in range(3))))
            cv.create_text(32, 15, text=name, fill="#000" if sum(_rgb(c1)) > 1.6 else "#fff", font=("", 9, "bold"))
            cv.bind("<Button-1>", lambda _e, a=c1, b=c2: self._use_gradient(a, b))
        for k in ("grad_color", "grad_angle", "grad_enabled"):
            xd[k].trace_add("write", lambda *_a: self._draw_grad_bar())
        return f

    def _shader_target(self, t: str):
        self._color_panel.target.set(t)
        self._color_panel.load_target()
        self._shader_nb.select(self._color_panel)

    def _use_gradient(self, c1: str, c2: str):
        self._font_color_btn.set(c1)
        self._on_font_color_change(c1)
        self._xd["grad_color"].set(c2)
        self._xd["grad_enabled"].set(True)
        self._draw_grad_bar()

    def _draw_grad_bar(self):
        c = getattr(self, "_grad_bar", None)
        if c is None:
            return
        w = max(10, c.winfo_width())
        c.delete("all")
        try:
            a, b = _rgb(self._font_color_btn.get()), _rgb(self._xd["grad_color"].get())
        except (tk.TclError, AttributeError):
            return
        for x in range(0, w, 2):
            t = x / max(1, w - 1)
            c.create_line(x, 0, x, 26, width=2, fill=_hex(tuple(a[j] * (1 - t) + b[j] * t for j in range(3))))

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
        for lab, key in (("二重縁", "stroke2_enabled"), ("光彩", "glow_enabled"), ("グラデ", "grad_enabled"), ("縦書き", "vertical")):
            ttk.Checkbutton(tb, text=lab, variable=self._xd[key]).pack(side="left", padx=2)
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
        ttk.Button(tb, text="💾 プリセットに保存", command=self._tfx_save_preset).pack(side="left", padx=2)

    def _schedule_program_redraw_force(self):
        self._pv_had_bg = True
        self._schedule_program_redraw()

    # ── 出力バー（Nixia の HD / 4K… と、DaVinci・TeloCut へ送るボタン）──
    def _build_output_bar(self):
        ob = tk.Frame(self._toolbar, bg=PANEL_BG)
        ob.pack(fill="x", side="top", pady=(0, 2))
        self._out_bar = ob
        tk.Label(ob, text=" 出力形式", bg=PANEL_BG, fg="#9aa0b0", font=("", 9)).pack(side="left", padx=(4, 6))
        self._fmt_btns = {}
        for name, w, h in FORMATS:
            b = tk.Button(ob, text=name, width=7, relief="flat", bd=0, bg="#33333b", fg="white",
                          activebackground=ACCENT, activeforeground="white", font=("", 9, "bold"),
                          command=lambda w=w, h=h: self._set_format(w, h))
            b.pack(side="left", padx=1, pady=3)
            self._tip(b, f"{name}: {w}×{h}")
            self._fmt_btns[(w, h)] = b
        b = tk.Button(ob, text="🎬 TeloCut に合わせる", relief="flat", bd=0, bg="#33333b", fg="white",
                      activebackground=ACCENT, font=("", 9), command=self._match_telocut_format)
        b.pack(side="left", padx=(6, 1), pady=3)
        self._tip(b, "起動中の TeloCut のシーケンスと同じ解像度・フレームレートにする")

        def act(text, fn, tip, color="#3a3a44"):
            bb = tk.Button(ob, text=text, relief="flat", bd=0, bg=color, fg="white", activebackground=ACCENT,
                           activeforeground="white", font=("", 9, "bold"), padx=8, command=fn)
            bb.pack(side="right", padx=1, pady=3)
            self._tip(bb, tip)
            return bb
        sync = getattr(self, "_tc_sync", None)
        if sync is not None:
            cb = tk.Checkbutton(ob, text="🔗 連携", variable=sync, command=self._toggle_tc_sync, bg=PANEL_BG,
                                fg="white", selectcolor="#2a5bd7", activebackground=PANEL_BG, activeforeground="white")
            cb.pack(side="right", padx=4)
            self._tip(cb, "TeloCut とリアルタイム連携（テロップ・デザイン・選択）")
        act("⬇ TeloCut から", self.telocut_pull, "TeloCut のテロップを取り込む")
        act("⬆ TeloCut へ", self.telocut_push, "テロップを TeloCut に反映（置き換え）", "#2a5bd7")
        tk.Frame(ob, width=10, bg=PANEL_BG).pack(side="right")
        act("⇩ PNG", self.export_png, "全テロップを PNG に書き出す")
        act("📍 再生ヘッドに", self.place_selected_at_playhead, "選んだテロップを Resolve の再生ヘッドに置く")
        act("⚡ Resolve へ送る", self.send_to_resolve, "全テロップを DaVinci Resolve のタイムラインに置く", ACCENT)

    def _set_format(self, w: int, h: int, fps=None):
        self._cw_var.set(w)
        self._ch_var.set(h)
        if fps:
            self._fps_var.set(fps)
        self._on_design_change()
        self._progress_var.set(f"出力形式: {w}×{h}" + (f"  {fps}fps" if fps else ""))

    def _match_telocut_format(self):
        def work():
            try:
                from core import telocut_link as L
                d = L.call("get_telops", timeout=5)
                w, h, fps = int(d["width"]), int(d["height"]), float(d.get("fps") or 0)
                self.after(0, lambda: self._set_format(w, h, round(fps, 3) if fps else None))
            except Exception as ex:
                msg = str(ex)
                self.after(0, lambda: messagebox.showwarning(
                    "TeloCut に合わせる", f"TeloCut から読み取れませんでした（起動していますか？）\n{msg}", parent=self))
        threading.Thread(target=work, daemon=True).start()

    # ── 下のステータスバー（画面の大きさ・テロップ・位置）──
    def _build_tfx_status(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", side="bottom", before=self._root_pane)
        self._tfx_status = {k: tk.StringVar() for k in ("screen", "layer", "telop", "pos", "xf", "align")}
        for k in ("align", "xf", "pos", "telop", "layer", "screen"):
            ttk.Label(bar, textvariable=self._tfx_status[k], foreground="gray").pack(side="right", padx=8)
            ttk.Separator(bar, orient="vertical").pack(side="right", fill="y")

    def _tfx_tick(self):
        """0.5 秒ごと: ステータスバー・スタイルプレビュー（デザインが変わったら）・サムネイル・出力形式のボタン。"""
        try:
            st = self._tfx_status
            cw, chh = self._cw_var.get(), self._ch_var.get()
            st["screen"].set(f"Screen w:{cw}, h:{chh}")
            for (w, h), b in self._fmt_btns.items():
                b.configure(bg=ACCENT if (w, h) == (cw, chh) else "#33333b")
            # 合成タブで止めた / 別のソースにしたら「TeloCut を背景に」も外す
            from core.capture import TeloCutFrameThread
            running = isinstance(getattr(self, "_comp_bg_thread", None), TeloCutFrameThread)
            if self._pv_tc_var.get() != running:
                self._pv_tc_var.set(running)
            self._tool_btns["🎬"].configure(bg=ACCENT if running else PANEL_BG)
            self._tool_btns["⚡"].configure(bg=ACCENT if self._place_now.get() else PANEL_BG)
            sel = self._tree.selection() if hasattr(self, "_tree") else ()
            n = len(self.telop_list)
            idx = ""
            name = "-"
            if sel:
                ids = [e.id for e in self.telop_list]
                try:
                    k = ids.index(int(sel[0]))
                    idx = str(k + 1)
                    name = self.telop_list[k].text.replace("\n", " ")[:16]
                except (ValueError, TypeError):
                    idx = "?"
            st["layer"].set(f"Layer: {name}")
            st["telop"].set(f"テロップ {idx or '-'}/{n}" + (f"（{len(sel)} 個選択）" if len(sel) > 1 else ""))
            st["pos"].set(f"x:{self._offset_x_var.get()}  y:{self._offset_y_var.get()}")
            d = self.design
            st["xf"].set(f"拡大 {d.scale_x}/{d.scale_y}%  回転 {d.rotation:g}°" + ("  縦書き" if d.vertical else ""))
            st["align"].set(f"{self._pos_h_var.get()} / {self._pos_v_var.get()}")
            key = repr(vars(self.design))
            if key != self._tfx_last_design:
                self._tfx_last_design = key
                self._tfx_style_preview()
                self._draw_grad_bar()
            lb = getattr(self, "_layer_browser", None)
            if lb is not None and lb.winfo_ismapped():
                lb.refresh()
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
            d.vertical = False
            d.rotation = 0.0
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

    # ── プリセットスタイルの一覧（組み込み S1… / T1… ＋ 保存したプリセット）──
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
        cv.bind("<MouseWheel>", lambda e: cv.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self._preset_canvas = cv
        self._preset_imgs: List[object] = []
        self._preset_cols = 0

    def _style_design(self, spec) -> object:
        import copy
        d = copy.copy(self.design)
        if isinstance(spec[1], dict):
            # 装飾つき: 装飾をいったん既定に戻してから上書き
            from core.models import DesignSettings
            base = DesignSettings()
            for k, _t in XD_FIELDS:
                setattr(d, k, getattr(base, k))
            d.shadow_enabled = d.bg_enabled = False
            for k, v in spec[1].items():
                setattr(d, k, v)
            return d
        name, fc, se, sc, sw, sh, bg, bgc = spec
        d.font_color, d.stroke_enabled, d.stroke_color, d.stroke_width = fc, se, sc, sw
        d.shadow_enabled, d.bg_enabled, d.bg_color = sh, bg, bgc
        d.grad_enabled = d.stroke2_enabled = d.glow_enabled = False
        d.bg_shape = "rect"
        return d

    def _preset_items(self) -> List[Tuple[str, Callable[[], object], bool]]:
        out = [(spec[0], (lambda s=spec: self._style_design(s)), True) for spec in BUILTIN_STYLES + FANCY_STYLES]
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
                d.vertical = False
                d.rotation = 0.0
                d.scale_x = d.scale_y = 100
                if getattr(d, "bg_shape", "rect") == "band":
                    d.bg_shape = "rect"
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
            b = tk.Button(cell, image=ph, bg="#0c0c10", activebackground=ACCENT, relief="flat", bd=1,
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

    # ── パーツ（Neo-n のパーツキャビネット: 記号・矢印・数字・見出し・ネット・単位）──
    def _build_symbols(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x")
        self._parts_cat = tk.StringVar(value="記号")
        cb = ttk.Combobox(top, textvariable=self._parts_cat, values=list(PARTS), state="readonly", width=8)
        cb.pack(side="left", padx=2, pady=2)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._fill_parts())
        self._parts_box = ttk.Frame(parent, padding=2)
        self._parts_box.pack(fill="both", expand=True)
        self._fill_parts()

    def _fill_parts(self):
        box = self._parts_box
        for w in box.winfo_children():
            w.destroy()
        items = PARTS.get(self._parts_cat.get(), SYMBOLS)
        wide = any(len(s) > 1 for s in items)
        per = 2 if wide else 6
        for i, s in enumerate(items):
            tk.Button(box, text=s, width=(10 if wide else 2), relief="flat", bd=1, font=("", 10 if wide else 12),
                      command=lambda s=s: self.insert_symbol(s)).grid(row=i // per, column=i % per, padx=1, pady=1, sticky="ew")

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
