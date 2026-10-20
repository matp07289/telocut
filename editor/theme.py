"""Avid Media Composer 風のダークテーマ。"""

C = {
    "bg":        "#1b1b1d",
    "panel":     "#242427",
    "panel2":    "#2c2c30",
    "panel3":    "#35353a",
    "border":    "#3d3d43",
    "text":      "#d2d2d8",
    "dim":       "#8b8b94",
    "accent":    "#7c6cf2",     # Avid 系の紫
    "accent2":   "#5a8dee",
    "splice":    "#e5c02a",     # スプライスイン / 抽出（黄）
    "overwrite": "#d8463c",     # 上書き / リフト（赤）
    "playhead":  "#4fb3ff",
    "in_out":    "#3a4a7a",
    "clip_video": "#6c76b6",
    "clip_audio": "#4b9a84",
    "clip_image": "#8b6db8",
    "clip_text":  "#c08848",
    "clip_color": "#7a7a80",
    "clip_adjust": "#3f8f9f",
    "clip_character": "#b06a8a",
    "clip_shape": "#6f6f50",
    "track_v":   "#2e3140",
    "track_a":   "#2b3834",
    "selected":  "#ffd257",
    "marker":    "#e0b000",
}

QSS = f"""
* {{
    color: {C['text']};
    font-family: "Segoe UI", "Yu Gothic UI", "Meiryo UI", sans-serif;
    font-size: 9pt;
}}
QMainWindow, QDialog {{ background: {C['bg']}; }}
QWidget {{ background: {C['panel']}; }}
QMainWindow::separator {{ background: {C['bg']}; width: 4px; height: 4px; }}
QDockWidget {{ titlebar-close-icon: none; }}
QDockWidget::title {{
    background: {C['panel3']}; padding: 3px 8px; border-bottom: 1px solid {C['border']};
}}
QMenuBar {{ background: {C['panel2']}; border-bottom: 1px solid {C['border']}; }}
QMenuBar::item:selected {{ background: {C['panel3']}; }}
QMenu {{ background: {C['panel2']}; border: 1px solid {C['border']}; }}
QMenu::item {{ padding: 4px 22px 4px 18px; }}
QMenu::item:selected {{ background: {C['accent']}; color: white; }}
QMenu::separator {{ height: 1px; background: {C['border']}; margin: 3px 6px; }}
QToolTip {{ background: #fefbe3; color: #111; border: 1px solid #888; padding: 3px; }}
QPushButton, QToolButton {{
    background: {C['panel3']}; border: 1px solid {C['border']}; border-radius: 3px;
    padding: 3px 8px; min-height: 18px;
}}
QPushButton:hover, QToolButton:hover {{ background: #42424a; }}
QPushButton:pressed, QToolButton:pressed, QToolButton:checked {{
    background: {C['accent']}; color: white; border-color: {C['accent']};
}}
QPushButton:disabled {{ color: {C['dim']}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {C['bg']}; border: 1px solid {C['border']}; border-radius: 2px;
    padding: 2px 4px; selection-background-color: {C['accent']};
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {C['accent']};
}}
QComboBox QAbstractItemView {{ background: {C['panel2']}; selection-background-color: {C['accent']}; }}
QListWidget, QTreeWidget, QTableWidget {{
    background: {C['bg']}; border: 1px solid {C['border']}; alternate-background-color: #202023;
}}
QListWidget::item:selected, QTreeWidget::item:selected {{ background: {C['accent']}; color: white; }}
QHeaderView::section {{
    background: {C['panel3']}; border: none; border-right: 1px solid {C['border']}; padding: 3px 6px;
}}
QTabWidget::pane {{ border: 1px solid {C['border']}; }}
QTabBar::tab {{
    background: {C['panel2']}; padding: 4px 12px; border: 1px solid {C['border']};
    border-bottom: none; margin-right: 1px;
}}
QTabBar::tab:selected {{ background: {C['panel3']}; color: white; }}
QScrollBar:horizontal {{ background: {C['bg']}; height: 12px; }}
QScrollBar:vertical {{ background: {C['bg']}; width: 12px; }}
QScrollBar::handle {{ background: #4a4a52; border-radius: 4px; min-width: 24px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QGroupBox {{
    border: 1px solid {C['border']}; border-radius: 3px; margin-top: 10px; padding-top: 6px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {C['dim']}; }}
QSlider::groove:horizontal {{ height: 4px; background: {C['border']}; }}
QSlider::handle:horizontal {{ width: 10px; margin: -5px 0; background: {C['text']}; border-radius: 5px; }}
QStatusBar {{ background: {C['panel2']}; border-top: 1px solid {C['border']}; }}
QLabel#tc {{
    font-family: "Consolas", "Courier New", monospace; font-size: 13pt; color: #e8e8ee;
    background: #111113; border: 1px solid {C['border']}; padding: 1px 6px;
}}
QLabel#info {{ color: {C['dim']}; background: transparent; }}
QPushButton#splice {{ background: {C['splice']}; color: #1a1a1a; font-weight: bold; }}
QPushButton#overwrite {{ background: {C['overwrite']}; color: white; font-weight: bold; }}
QPushButton#splice:hover {{ background: #f2d34a; }}
QPushButton#overwrite:hover {{ background: #ea5b51; }}
QFrame#monitorFrame[active="true"] {{ border: 2px solid {C['accent']}; }}
QFrame#monitorFrame[active="false"] {{ border: 2px solid {C['bg']}; }}
"""
