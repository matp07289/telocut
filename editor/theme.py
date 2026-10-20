"""画面の配色（Avid Media Composer の「インターフェース」設定のように、配色パターン・明るさ・強調色・文字の大きさを選べる）。

C は各画面が `from .theme import C` で共有している色の辞書。配色を変えるときは中身を差し替えるので、
タイムラインなど描き直す部分はすぐ、ほかの部分は次に開いたときに反映される。
"""
from __future__ import annotations

import colorsys
from typing import Dict, Optional

# ── 配色パターン ─────────────────────────────
_BASE = {
    "splice": "#e5c02a", "overwrite": "#d8463c", "playhead": "#4fb3ff", "in_out": "#3a4a7a",
    "clip_video": "#6c76b6", "clip_audio": "#4b9a84", "clip_image": "#8b6db8", "clip_text": "#c08848",
    "clip_color": "#7a7a80", "clip_adjust": "#3f8f9f", "clip_character": "#b06a8a", "clip_shape": "#6f6f50",
    "clip_null": "#55555c", "clip_camera": "#5f7f9f", "clip_nest": "#7b8f5a",
    "selected": "#ffd257", "marker": "#e0b000",
}

THEMES: Dict[str, dict] = {
    "avid": {"label": "Avid グレー（標準）", **_BASE,
             "bg": "#1b1b1d", "panel": "#242427", "panel2": "#2c2c30", "panel3": "#35353a", "border": "#3d3d43",
             "text": "#d2d2d8", "dim": "#8b8b94", "accent": "#7c6cf2", "accent2": "#5a8dee",
             "track_v": "#2e3140", "track_a": "#2b3834", "alt": "#202023", "hover": "#42424a", "scroll": "#4a4a52"},
    "avid_blue": {"label": "Avid クラシック（青みグレー）", **_BASE,
                  "bg": "#20242b", "panel": "#2a2f37", "panel2": "#323843", "panel3": "#3b4250", "border": "#47505f",
                  "text": "#d5dbe5", "dim": "#8e97a6", "accent": "#4a86d8", "accent2": "#6fa8ff",
                  "track_v": "#2f3a52", "track_a": "#2d3f3c", "alt": "#252a32", "hover": "#465063", "scroll": "#4f596b"},
    "graphite": {"label": "グラファイト（明るめのグレー）", **_BASE,
                 "bg": "#2e2e30", "panel": "#3a3a3d", "panel2": "#434347", "panel3": "#4d4d52", "border": "#5a5a60",
                 "text": "#ececf0", "dim": "#a4a4ac", "accent": "#e08a2e", "accent2": "#f0a850",
                 "track_v": "#3d4150", "track_a": "#3a4a45", "alt": "#333336", "hover": "#57575d", "scroll": "#65656c"},
    "midnight": {"label": "ミッドナイト（濃紺）", **_BASE,
                 "bg": "#0f1320", "panel": "#161b2c", "panel2": "#1c2238", "panel3": "#242b45", "border": "#2f3858",
                 "text": "#d7ddf0", "dim": "#7f89a8", "accent": "#5b8cff", "accent2": "#8aa9ff",
                 "track_v": "#1f2a48", "track_a": "#1c3236", "alt": "#131828", "hover": "#2c3557", "scroll": "#36406a"},
    "forest": {"label": "フォレスト（落ち着いた緑）", **_BASE,
               "bg": "#181d1a", "panel": "#212823", "panel2": "#28302a", "panel3": "#303a33", "border": "#3b473e",
               "text": "#d6e2d8", "dim": "#8a998d", "accent": "#4caf72", "accent2": "#7bd197",
               "track_v": "#2b3540", "track_a": "#29392f", "alt": "#1c221e", "hover": "#3c4840", "scroll": "#46544a"},
    "warm": {"label": "ウォーム（ブラウン）", **_BASE,
             "bg": "#1f1a17", "panel": "#29221e", "panel2": "#312923", "panel3": "#3a302a", "border": "#4a3d35",
             "text": "#e6dbd2", "dim": "#a0928a", "accent": "#d9822b", "accent2": "#f0a35e",
             "track_v": "#33303c", "track_a": "#2f3a30", "alt": "#241e1a", "hover": "#4a3d35", "scroll": "#56473e"},
    "light": {"label": "ライト（明るい）", **_BASE,
              "bg": "#e6e6ea", "panel": "#f2f2f5", "panel2": "#e1e1e6", "panel3": "#d4d4db", "border": "#b9b9c2",
              "text": "#1d1d22", "dim": "#666670", "accent": "#5b4fd8", "accent2": "#3a73d6",
              "track_v": "#cfd3e6", "track_a": "#cde0d9", "alt": "#ebebef", "hover": "#c6c6cf", "scroll": "#a9a9b4",
              "in_out": "#b9c4ec", "selected": "#d99a00"},
    "contrast": {"label": "ハイコントラスト（見やすさ重視）", **_BASE,
                 "bg": "#000000", "panel": "#0b0b0b", "panel2": "#141414", "panel3": "#1f1f1f", "border": "#7a7a7a",
                 "text": "#ffffff", "dim": "#c8c8c8", "accent": "#ffd400", "accent2": "#00d0ff",
                 "track_v": "#1a1f33", "track_a": "#13291f", "alt": "#090909", "hover": "#333333", "scroll": "#9a9a9a",
                 "selected": "#00ff7f"},
}
NEUTRAL = ("bg", "panel", "panel2", "panel3", "border", "alt", "hover", "scroll", "track_v", "track_a")

C: Dict[str, str] = {}
QSS = ""


def _shift(hexs: str, amount: float) -> str:
    """明るさを amount（−1〜1）だけずらす。"""
    h = hexs.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hh, ll, ss = colorsys.rgb_to_hls(r, g, b)
    ll = max(0.0, min(1.0, ll + amount))
    r, g, b = colorsys.hls_to_rgb(hh, ll, ss)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def palette(name: str = "avid", brightness: int = 0, accent: str = "") -> Dict[str, str]:
    """配色パターン name に、明るさ（−50〜50。Avid の「明るさ」スライダー）と強調色を当てた色の辞書。"""
    base = dict(THEMES.get(name) or THEMES["avid"])
    base.pop("label", None)
    k = max(-50, min(50, int(brightness))) / 250.0
    if k:
        for key in NEUTRAL:
            base[key] = _shift(base[key], k)
        # 背景が明るくなりすぎたら文字を暗く
        lum = colorsys.rgb_to_hls(*(int(base["panel"].lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)))[1]
        if lum > 0.6 and name != "light":
            base["text"], base["dim"] = "#1d1d22", "#55555e"
    if accent:
        base["accent"] = accent
    return base


def build_qss(c: Dict[str, str], font_pt: float = 9.0) -> str:
    light = colorsys.rgb_to_hls(*(int(c["panel"].lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)))[1] > 0.6
    on_accent = "#111111" if colorsys.rgb_to_hls(*(int(c["accent"].lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)))[1] > 0.62 else "white"
    tc_fg, tc_bg = ("#1d1d22", "#ffffff") if light else ("#e8e8ee", "#111113")
    return f"""
* {{
    color: {c['text']};
    font-family: "Segoe UI", "Yu Gothic UI", "Meiryo UI", sans-serif;
    font-size: {font_pt:g}pt;
}}
QMainWindow, QDialog {{ background: {c['bg']}; }}
QWidget {{ background: {c['panel']}; }}
QMainWindow::separator {{ background: {c['bg']}; width: 4px; height: 4px; }}
QDockWidget {{ titlebar-close-icon: none; }}
QDockWidget::title {{
    background: {c['panel3']}; padding: 3px 8px; border-bottom: 1px solid {c['border']};
}}
QMenuBar {{ background: {c['panel2']}; border-bottom: 1px solid {c['border']}; }}
QMenuBar::item:selected {{ background: {c['panel3']}; }}
QMenu {{ background: {c['panel2']}; border: 1px solid {c['border']}; }}
QMenu::item {{ padding: 4px 22px 4px 18px; }}
QMenu::item:selected {{ background: {c['accent']}; color: {on_accent}; }}
QTabBar#page_bar::tab {{ padding: 4px 14px; margin: 0 1px; border-radius: 4px; background: transparent; }}
QTabBar#page_bar::tab:selected {{ background: {c['accent']}; color: {on_accent}; font-weight: bold; }}
QTabBar#page_bar::tab:hover:!selected {{ background: {c['hover']}; }}
QMenu::separator {{ height: 1px; background: {c['border']}; margin: 3px 6px; }}
QToolTip {{ background: #fefbe3; color: #111; border: 1px solid #888; padding: 3px; }}
QPushButton, QToolButton {{
    background: {c['panel3']}; border: 1px solid {c['border']}; border-radius: 3px;
    padding: 3px 8px; min-height: 18px;
}}
QPushButton:hover, QToolButton:hover {{ background: {c['hover']}; }}
QPushButton:pressed, QToolButton:pressed, QToolButton:checked {{
    background: {c['accent']}; color: {on_accent}; border-color: {c['accent']};
}}
QPushButton:disabled {{ color: {c['dim']}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {c['bg']}; border: 1px solid {c['border']}; border-radius: 2px;
    padding: 2px 4px; selection-background-color: {c['accent']};
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {c['accent']};
}}
QComboBox QAbstractItemView {{ background: {c['panel2']}; selection-background-color: {c['accent']}; }}
QListWidget, QTreeWidget, QTableWidget {{
    background: {c['bg']}; border: 1px solid {c['border']}; alternate-background-color: {c['alt']};
}}
QListWidget::item:selected, QTreeWidget::item:selected {{ background: {c['accent']}; color: {on_accent}; }}
QHeaderView::section {{
    background: {c['panel3']}; border: none; border-right: 1px solid {c['border']}; padding: 3px 6px;
}}
QTabWidget::pane {{ border: 1px solid {c['border']}; }}
QTabBar::tab {{
    background: {c['panel2']}; padding: 4px 12px; border: 1px solid {c['border']};
    border-bottom: none; margin-right: 1px;
}}
QTabBar::tab:selected {{ background: {c['panel3']}; color: {c['text']}; font-weight: bold; }}
QScrollBar:horizontal {{ background: {c['bg']}; height: 12px; }}
QScrollBar:vertical {{ background: {c['bg']}; width: 12px; }}
QScrollBar::handle {{ background: {c['scroll']}; border-radius: 4px; min-width: 24px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QGroupBox {{
    border: 1px solid {c['border']}; border-radius: 3px; margin-top: 10px; padding-top: 6px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {c['dim']}; }}
QSlider::groove:horizontal {{ height: 4px; background: {c['border']}; }}
QSlider::handle:horizontal {{ width: 10px; margin: -5px 0; background: {c['text']}; border-radius: 5px; }}
QStatusBar {{ background: {c['panel2']}; border-top: 1px solid {c['border']}; }}
QLabel#tc {{
    font-family: "Consolas", "Courier New", monospace; font-size: {font_pt + 4:g}pt; color: {tc_fg};
    background: {tc_bg}; border: 1px solid {c['border']}; padding: 1px 6px;
}}
QLabel#info {{ color: {c['dim']}; background: transparent; }}
QPushButton#splice {{ background: {c['splice']}; color: #1a1a1a; font-weight: bold; }}
QPushButton#overwrite {{ background: {c['overwrite']}; color: white; font-weight: bold; }}
QPushButton#splice:hover {{ background: #f2d34a; }}
QPushButton#overwrite:hover {{ background: #ea5b51; }}
QFrame#monitorFrame[active="true"] {{ border: 2px solid {c['accent']}; }}
QFrame#monitorFrame[active="false"] {{ border: 2px solid {c['bg']}; }}
"""


def windows_prefers_light() -> Optional[bool]:
    """Windows の「アプリのモード」がライトなら True（分からなければ None）。"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        v, _t = winreg.QueryValueEx(k, "AppsUseLightTheme")
        return bool(v)
    except Exception:
        return None


def current_choice(settings=None) -> tuple:
    """設定から (配色名, 明るさ, 強調色, 文字の大きさ)。Windows に合わせる設定ならそれに。"""
    if settings is None:
        from .settings import SETTINGS as settings
    name = getattr(settings, "theme", "avid") or "avid"
    if getattr(settings, "theme_follow_os", False):
        light = windows_prefers_light()
        if light is not None:
            name = "light" if light else (name if name != "light" else "avid")
    return name, int(getattr(settings, "theme_brightness", 0) or 0), getattr(settings, "theme_accent", "") or "", \
        float(getattr(settings, "ui_font_pt", 9.0) or 9.0)


def apply(app=None, name: Optional[str] = None, brightness: Optional[int] = None, accent: Optional[str] = None,
          font_pt: Optional[float] = None) -> str:
    """配色を当てる（C を差し替え、アプリ全体のスタイルシートを作り直す）。"""
    global QSS
    n, b, a, f = current_choice()
    n = name if name is not None else n
    b = brightness if brightness is not None else b
    a = accent if accent is not None else a
    f = font_pt if font_pt is not None else f
    new = palette(n, b, a)
    C.clear()
    C.update(new)
    QSS = build_qss(C, f)
    if app is not None:
        app.setStyleSheet(QSS)
        for w in app.topLevelWidgets():
            w.update()
    return QSS


apply()
