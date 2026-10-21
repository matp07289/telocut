"""TeloCut メインウィンドウ。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QSettings, QTimer, QByteArray
from PyQt6.QtGui import QAction, QKeySequence, QPixmap, QImage, QActionGroup
from PyQt6.QtWidgets import (QMainWindow, QDockWidget, QSplitter, QWidget, QFileDialog,
                             QMessageBox, QInputDialog, QLabel, QDialog, QFormLayout,
                             QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QDialogButtonBox,
                             QTextBrowser, QVBoxLayout, QApplication)

from core.timecode import (frames_to_timecode, timecode_to_frames, normalize_timecode,
                           parse_timecode)
from . import model as M
from .features import FeaturesMixin
from .features3d import Features3DMixin
from .features4 import Features4Mixin
from .features5 import Features5Mixin
from .production import ProductionMixin
from .features6 import Features6Mixin
from .features7 import Features7Mixin
from .farmpanel import FarmMixin as FarmUIMixin
from .autoclippanel import AutoClipMixin
from .settingsui import StudioMixin
from .features8 import Features8Mixin
from .features9 import Features9Mixin
from .features10 import Features10Mixin
from .features11 import Features11Mixin
from .features12 import Features12Mixin
from .features13 import Features13Mixin
from .features14 import Features14Mixin
from .features15 import Features15Mixin
from .dualdisplay import DualDisplayMixin
from . import media as MD
from . import render as R
from .bin_panel import BinContainer, EffectPalette, ImportWorker
from .project_window import ProjectPanel, SelectProjectDialog
from .panels import MarkerPanel, TelopListPanel, HistoryPanel, ScopesPanel, MixerPanel
from .commands import COMMAND_INFO, CATEGORIES, PRESETS, preset_keymap, conflicts
from .display import FullscreenOutput, screens_sorted, screen_names, ensure_docks_on_screen
from .export import ExportDialog
from .inspector import Inspector
from .keyboard_dialog import KeyboardDialog
from .monitor import MonitorPanel
from .player import Player
from .prefs_dialog import PreferencesDialog
from .projectio import (ProjectLock, RelinkDialog, atomic_write, attic_backup, describe_lock,
                        list_attic, resolve_media, update_rel_paths)
from .rendercache import CACHE, BackgroundRenderer, ProxyWorker, CopyWorker, segments
from .settings import SETTINGS
from .timeline import TimelinePanel
from .theme import C

APP_NAME = "TeloCut"
PROJECT_EXT = ".tcut"
PROJECT_FILTER = "TeloCut プロジェクト (*.tcut *.dvep)"
LAYOUT_VERSION = "5"

class SequenceDialog(QDialog):
    RES = [("1920×1080 (FHD)", 1920, 1080), ("3840×2160 (4K UHD)", 3840, 2160),
           ("1280×720 (HD)", 1280, 720), ("1080×1920 (縦型)", 1080, 1920),
           ("1080×1080 (正方形)", 1080, 1080)]
    FPS = [23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0]

    def __init__(self, parent, seq: M.Sequence):
        super().__init__(parent)
        self.setWindowTitle("シーケンス設定")
        f = QFormLayout(self)
        self.name = QLineEdit(seq.name)
        f.addRow("名前", self.name)
        self.res = QComboBox()
        for lab, w, h in self.RES:
            self.res.addItem(lab, (w, h))
        idx = next((i for i, r in enumerate(self.RES) if (r[1], r[2]) == (seq.width, seq.height)), -1)
        if idx < 0:
            self.res.addItem(f"{seq.width}×{seq.height}", (seq.width, seq.height))
            idx = self.res.count() - 1
        self.res.setCurrentIndex(idx)
        f.addRow("解像度", self.res)
        self.fps = QComboBox()
        for v in self.FPS:
            self.fps.addItem(f"{v:g} fps", v)
        self.fps.setCurrentIndex(min(range(len(self.FPS)), key=lambda i: abs(self.FPS[i] - seq.fps)))
        f.addRow("フレームレート", self.fps)
        self.start_tc = QLineEdit(seq.start_tc)
        f.addRow("開始タイムコード", self.start_tc)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


class MainWindow(FeaturesMixin, Features3DMixin, Features4Mixin, Features5Mixin, ProductionMixin, Features6Mixin,
                 Features7Mixin, FarmUIMixin, AutoClipMixin, StudioMixin, Features11Mixin, Features12Mixin, Features13Mixin, Features14Mixin, Features15Mixin, Features8Mixin, Features9Mixin, Features10Mixin, DualDisplayMixin,
                 QMainWindow):
    def __init__(self):
        super().__init__()
        SETTINGS.ensure_dirs()
        self.project = M.Project()
        self.project.sequences = [M.Sequence.create()]
        self.project_path: Optional[str] = None
        self.dirty = False
        self.read_only = False
        self.lock = ProjectLock()
        self._undo: list = []
        self._redo: list = []
        self._edit_depth = 0
        self._edit_snap = None
        self._clipboard = None
        self._peaks: dict = {}
        self._thumbs: dict = {}
        self._workers: list = []
        self.source_media_id: Optional[str] = None
        self.source_seq: Optional[M.Sequence] = None
        self.active = "record"
        self.trim = None                 # トリムモードの状態
        self.patch = {"V1": 0, "A1": 0}  # ソーストラック → レコードトラック（Avid のパッチ）
        self.src_enabled = {"V1": True, "A1": True}
        self._seek_from_timeline = False
        self.render_status: list = []    # [(a, b, "red"/"yellow"/"done")]
        self.renderer = None
        self.fullscreen_win = None
        self._focus_prev = None
        self.settings = QSettings("DavinTelop", APP_NAME)
        from .assets import app_icon
        self.setWindowIcon(app_icon())
        self._load_default_design()

        self.setWindowTitle(APP_NAME)
        self.resize(1600, 960)
        self.setDockNestingEnabled(True)
        self.setDockOptions(QMainWindow.DockOption.AnimatedDocks | QMainWindow.DockOption.AllowNestedDocks |
                            QMainWindow.DockOption.AllowTabbedDocks)

        # プレイヤー
        self.source_player = Player(lambda: self.project, lambda: self.source_seq, self)
        self.record_player = Player(lambda: self.project, lambda: self.project.sequence, self)
        self.record_player.allow_beyond = True

        # コンポーザー（ソース / レコード）— 独立パネルなので別の画面へ移動できる
        self.source_mon = MonitorPanel("ソース", "source", self.source_player)
        self.record_mon = MonitorPanel("レコード", "record", self.record_player)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.source_mon)
        split.addWidget(self.record_mon)
        split.setSizes([1, 1])
        self.composer = split
        for mon in (self.source_mon, self.record_mon):
            mon.activated.connect(lambda k=mon.kind: self.set_active_monitor(k))
            mon.action.connect(lambda a, k=mon.kind: self._monitor_action(k, a))
        self.source_mon.view.clear("ビンの素材をダブルクリックで表示")
        self.record_player.position_changed.connect(self._on_record_position)

        # パネル
        self.bin = BinContainer(self)
        self.bin.open_in_source.connect(self.load_source)
        self.bin.import_requested.connect(self.import_dialog)
        self.effects = EffectPalette()
        self.effects.apply.connect(self.apply_effect)
        self.project_panel = ProjectPanel(self, self.effects)
        self.inspector = Inspector(self)
        self.inspector.setMinimumWidth(260)
        self.timeline = TimelinePanel(self)
        self.canvas = self.timeline.canvas
        self.canvas.selection_changed.connect(self._on_selection)

        # Avid と同じ構成: 左=プロジェクトウィンドウ + ビンコンテナ / 上=コンポーザー / 下=タイムライン
        # コンポーザーは中央（余った幅を使う）。2画面目へ移す時は「別ウィンドウにする」
        from PyQt6.QtWidgets import QVBoxLayout as _VB
        self.composer_host = QWidget()
        _hl = _VB(self.composer_host)
        _hl.setContentsMargins(0, 0, 0, 0)
        _hl.addWidget(self.composer)
        self.setCentralWidget(self.composer_host)
        self.composer_window = None
        self.dock_project = self._dock("プロジェクト", self.project_panel, Qt.DockWidgetArea.LeftDockWidgetArea, "project")
        self.dock_bin = self._dock("ビン", self.bin, Qt.DockWidgetArea.LeftDockWidgetArea, "bin")
        self.splitDockWidget(self.dock_project, self.dock_bin, Qt.Orientation.Vertical)
        self.dock_insp = self._dock("エフェクトエディター", self.inspector, Qt.DockWidgetArea.RightDockWidgetArea, "insp")
        self.dock_tl = self._dock("タイムライン", self.timeline, Qt.DockWidgetArea.BottomDockWidgetArea, "tl")
        # 追加パネル（右側にタブで重ねる。スコープ・ミキサー・履歴は ウィンドウ メニューから表示）
        self.marker_panel = MarkerPanel(self)
        self.telop_panel = TelopListPanel(self)
        self.history_panel = HistoryPanel(self)
        self.scopes_panel = ScopesPanel(self)
        self.mixer_panel = MixerPanel(self)
        self.dock_telops = self._dock("テロップ一覧", self.telop_panel, Qt.DockWidgetArea.RightDockWidgetArea, "telops")
        self.dock_markers = self._dock("マーカー", self.marker_panel, Qt.DockWidgetArea.RightDockWidgetArea, "markers")
        self.dock_history = self._dock("履歴", self.history_panel, Qt.DockWidgetArea.RightDockWidgetArea, "history")
        self.dock_scopes = self._dock("ビデオスコープ", self.scopes_panel, Qt.DockWidgetArea.RightDockWidgetArea, "scopes")
        self.dock_mixer = self._dock("オーディオミキサー", self.mixer_panel, Qt.DockWidgetArea.RightDockWidgetArea, "mixer")
        for d in (self.dock_telops, self.dock_markers, self.dock_history, self.dock_scopes, self.dock_mixer):
            self.tabifyDockWidget(self.dock_insp, d)
        for d in (self.dock_history, self.dock_scopes, self.dock_mixer):
            d.hide()
        self.dock_insp.raise_()
        self.docks = [self.dock_project, self.dock_bin, self.dock_insp, self.dock_tl, self.dock_telops,
                      self.dock_markers, self.dock_history, self.dock_scopes, self.dock_mixer]
        self.dock_fx = self.dock_project
        self.setCorner(Qt.Corner.TopLeftCorner, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.setCorner(Qt.Corner.TopRightCorner, Qt.DockWidgetArea.RightDockWidgetArea)
        self.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.BottomDockWidgetArea)
        self.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.BottomDockWidgetArea)
        self.resizeDocks([self.dock_bin, self.dock_insp], [440, 280], Qt.Orientation.Horizontal)
        self.resizeDocks([self.dock_project, self.dock_bin], [260, 600], Qt.Orientation.Vertical)
        self.resizeDocks([self.dock_tl], [400], Qt.Orientation.Vertical)
        self._default_state = self.saveState()

        self.status = self.statusBar()
        self.sel_label = QLabel("")
        self.sel_label.setObjectName("info")
        self.status.addPermanentWidget(self.sel_label)
        self.proxy_label = QLabel("")
        self.status.addPermanentWidget(self.proxy_label)
        self.render_label = QLabel("")
        self.render_label.setObjectName("info")
        self.status.addPermanentWidget(self.render_label)
        self.cache_label = QLabel("")
        self.cache_label.setObjectName("info")
        self.status.addPermanentWidget(self.cache_label)

        self._init_features()
        self._init_features3d()
        self._init_features4()
        self._init_features5()
        self._init_production()
        self._init_farmui()
        self._init_studio()
        self._init_dual()
        self._init_features10()
        self._build_commands()
        self._build_menus()
        self._build_feature_menus()
        self._build_feature3d_menus()
        self._build_feature4_menus()
        self._build_feature5_menus()
        self._build_production_menus()
        self._build_feature6_menus()
        self._build_feature7_menus()
        self._build_feature8_menus()
        self._build_feature9_menus()
        self._build_feature10_menus()
        self._build_feature11_menus()
        self._build_feature12_menus()
        self._build_feature13_menus()
        self._build_feature14_menus()
        self._build_feature15_menus()          # 最後にメニューを仕事の流れで並べ直す（menulayout.py）
        self._build_toolbar()
        from .commands import add_new_keys
        self.apply_keymap(add_new_keys(dict(SETTINGS.keymap)) if SETTINGS.keymap else preset_keymap(SETTINGS.keyboard_preset))
        self._init_feature9_ui()
        self._init_feature10_ui()
        self._init_feature11()

        st = self.settings.value("window_state")
        geo = self.settings.value("window_geometry")
        if self.settings.value("layout_version") == LAYOUT_VERSION:
            if geo is not None:
                self.restoreGeometry(geo)
            if st is not None:
                self.restoreState(st)
            ensure_docks_on_screen(self, self.docks)
        else:
            self._need_default_sizes = True
        self.project.ensure_bins()
        self.bin.reset()
        self.set_active_monitor("record")
        self.refresh_all()
        QTimer.singleShot(200, self.canvas.zoom_fit)
        self._update_title()

        # タイマー: 自動保存 / ロックのハートビート / レンダー状態 / アイドル時レンダリング
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self.autosave)
        self._restart_autosave()
        self._lock_timer = QTimer(self)
        self._lock_timer.setInterval(60_000)
        self._lock_timer.timeout.connect(self.lock.heartbeat)
        self._lock_timer.start()
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(300)
        self._status_timer.timeout.connect(self.update_render_status)
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(4000)
        self._idle_timer.timeout.connect(self._idle_render)

    def _apply_default_sizes(self):
        w = max(1200, self.width())
        self.resizeDocks([self.dock_bin], [int(w * 0.26)], Qt.Orientation.Horizontal)
        self.resizeDocks([self.dock_insp], [int(w * 0.17)], Qt.Orientation.Horizontal)
        self.resizeDocks([self.dock_project, self.dock_bin], [250, 600], Qt.Orientation.Vertical)
        self.resizeDocks([self.dock_tl], [int(self.height() * 0.42)], Qt.Orientation.Vertical)

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_dual_checked", False):
            self._dual_checked = True
            QTimer.singleShot(500, self.auto_dual_at_start)
        if getattr(self, "_need_default_sizes", False):
            self._need_default_sizes = False
            QTimer.singleShot(0, self._apply_default_sizes)
            QTimer.singleShot(300, self._apply_default_sizes)

    def _dock(self, title, widget, area, name):
        d = QDockWidget(title, self)
        d.setObjectName(name)
        d.setWidget(widget)
        d.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable |
                      QDockWidget.DockWidgetFeature.DockWidgetFloatable |
                      QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.addDockWidget(area, d)
        return d

    def _load_default_design(self):
        from core.models import DesignSettings
        self.project.design = DesignSettings().to_dict()

    # ── コマンド / メニュー / キーボード ─────────
    def _build_commands(self):
        """コマンド id → 処理。メニューとキーボード設定はこの表を共有する。"""
        P = lambda: self.active_player()
        self.cmd_fns = {
            "play_stop": self.play_stop, "shuttle_j": lambda: self.shuttle("J"),
            "shuttle_k": lambda: self.shuttle("K"), "shuttle_l": lambda: self.shuttle("L"),
            "play_in_out": self.play_in_out,
            "step_back_1": lambda: self.step(-1), "step_fwd_1": lambda: self.step(1),
            "step_back_10": lambda: self.step(-10), "step_fwd_10": lambda: self.step(10),
            "step_back_5": lambda: self.step(-5), "step_fwd_5": lambda: self.step(5),
            "prev_edit": lambda: self.goto_edit(-1), "next_edit": lambda: self.goto_edit(1),
            "goto_start": lambda: P().seek(0), "goto_end": lambda: P().seek(P().end_frame),
            "goto_in": lambda: self.goto_mark("in"), "goto_out": lambda: self.goto_mark("out"),
            "goto_tc": self.goto_tc_dialog, "toggle_monitor": self.toggle_monitor,
            "fullscreen": lambda: self.toggle_fullscreen(None),
            "loop_toggle": self.toggle_loop,
            "mark_in": lambda: self.mark("in"), "mark_out": lambda: self.mark("out"),
            "mark_clip": self.mark_clip, "clear_in": lambda: self.mark("clear_in"),
            "clear_out": lambda: self.mark("clear_out"), "clear_both": lambda: self.mark("clear"),
            "add_marker": self.add_marker,
            "splice_in": lambda: self.three_point_edit("insert"),
            "overwrite": lambda: self.three_point_edit("overwrite"),
            "lift": lambda: self.lift_extract(False), "extract": lambda: self.lift_extract(True),
            "copy_clipboard": self.copy_clips, "paste": lambda: self.paste_clips(), "cut": self.cut_clips,
            "add_edit": lambda: self.add_edit(False), "add_edit_all": lambda: self.add_edit(True),
            "delete": self.delete_key, "ripple_delete": self.ripple_delete_selected,
            "ripple_trim_prev": lambda: self.ripple_trim_to_playhead("prev"),
            "ripple_trim_next": lambda: self.ripple_trim_to_playhead("next"),
            "match_frame": self.match_frame, "toggle_enabled": self.toggle_clips_enabled,
            "toggle_link": self.toggle_link, "add_fades": self.add_default_fades,
            "add_telop": lambda: self.add_telop(), "undo": self.undo, "redo": self.redo,
            "select_all_clips": self.select_all, "deselect_all": self.deselect_all,
            "select_all_tracks": lambda: self.select_tracks(True),
            "deselect_all_tracks": lambda: self.select_tracks(False),
            "trim_mode": self.toggle_trim,
            "trim_m10": lambda: self.trim_by(-10), "trim_m1": lambda: self.trim_by(-1),
            "trim_p1": lambda: self.trim_by(1), "trim_p10": lambda: self.trim_by(10),
            "trim_side_a": lambda: self.trim_side("a"), "trim_side_ab": lambda: self.trim_side("ab"),
            "trim_side_b": lambda: self.trim_side("b"),
            "tool_select": lambda: self.timeline.set_tool("select"),
            "tool_yellow": lambda: self.timeline.set_tool("yellow"),
            "tool_ripple": lambda: self.timeline.set_tool("ripple"),
            "tool_roll": lambda: self.timeline.set_tool("roll"),
            "tool_slip": lambda: self.timeline.set_tool("slip"),
            "tool_slide": lambda: self.timeline.set_tool("slide"),
            "tool_razor": lambda: self.timeline.set_tool("razor"),
            "tool_hand": lambda: self.timeline.set_tool("hand"),
            "tool_zoom": lambda: self.timeline.set_tool("zoom"),
            "snap_toggle": self.timeline.toggle_snap,
            "zoom_in": lambda: self.canvas.zoom_at(1.5), "zoom_out": lambda: self.canvas.zoom_at(0.66),
            "zoom_fit": self.canvas.zoom_fit, "focus": self.toggle_focus,
            "render_in_out": lambda: self.start_render(True), "render_all": lambda: self.start_render(False),
            "render_cancel": self.cancel_render, "proxy_selected": self.make_proxies_for_selection,
            "toggle_proxy": self.toggle_proxy,
            "new_project": self.new_project, "open_project": lambda: self.open_project(),
            "new_bin": lambda: self.new_bin(), "new_sequence": lambda: self.new_sequence(),
            "close_project": self.close_project,
            "save_project": self.save_project, "save_as": self.save_project_as,
            "import": self.import_dialog, "export": self.export_media,
            "keyboard_settings": self.keyboard_settings, "preferences": self.preferences,
            "workspace_single": self.workspace_single, "workspace_dual": self.workspace_dual,
            "shortcuts_help": self.show_shortcuts,
            "add_dissolve": lambda: self.add_dissolve(1.0), "speed": self.speed_dialog,
            "freeze_frame": self.freeze_frame, "paste_attributes": self.paste_attributes,
            "replace_edit": self.replace_edit, "close_gaps": self.close_gaps,
            "select_forward": self.select_forward, "subclip": self.make_subclip,
            "find_bin": self.find_bin, "project_find": self.project_find,
            "normalize": self.normalize, "audio_crossfades": self.audio_crossfades,
            "silence_cut": self.silence_cut, "scene_detect": self.scene_detect,
            "import_interchange": lambda: self.import_interchange(), "export_aaf": self.export_aaf_file,
            "export_xml": self.export_xml_file, "export_edl": self.export_edl_file,
            "export_frame": self.export_frame, "export_wav": self.export_wav,
            "export_chapters": self.export_chapters, "export_queue": self.export_queue,
            "tc_window": self.toggle_tc_window,
        }
        self.cmd_fns.update(self._feature_commands())
        self.cmd_fns.update(self._feature3d_commands())
        self.cmd_fns.update(self._feature4_commands())
        self.cmd_fns.update(self._feature5_commands())
        self.cmd_fns.update(self._production_commands())
        self.cmd_fns.update(self._feature6_commands())
        self.cmd_fns.update(self._feature7_commands())
        self.cmd_fns.update(self._farmui_commands())
        self.cmd_fns.update(self._autoclip_commands())
        self.cmd_fns.update(self._studio_commands())
        self.cmd_fns.update(self._feature8_commands())
        self.cmd_fns.update(self._feature9_commands())
        self.cmd_fns.update(self._feature10_commands())
        self.cmd_fns.update(self._feature11_commands())
        self.cmd_fns.update(self._feature12_commands())
        self.cmd_fns.update(self._feature13_commands())
        self.cmd_fns.update(self._feature14_commands())
        self.cmd_fns.update(self._feature15_commands())
        # エディション（無料版 / Pro / Studio）で使えない機能は、どこから呼んでも案内を出す
        from . import license as LIC
        for cid, feat in LIC.GATED_COMMANDS.items():
            fn = self.cmd_fns.get(cid)
            if fn is not None:
                self.cmd_fns[cid] = (lambda fn=fn, feat=feat: fn() if LIC.require(self, feat) else None)
        for i in range(4):
            self.cmd_fns[f"toggle_v{i + 1}"] = lambda i=i: self.toggle_track("video", i)
            self.cmd_fns[f"toggle_a{i + 1}"] = lambda i=i: self.toggle_track("audio", i)
        self.actions = {}
        for cid, info in COMMAND_INFO.items():
            fn = self.cmd_fns.get(cid)
            if fn is None:
                continue
            a = QAction(info[1], self)
            a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            a.triggered.connect(lambda _=False, f=fn: f())
            self.addAction(a)
            self.actions[cid] = a

    def _build_toolbar(self):
        from PyQt6.QtWidgets import QToolBar, QToolButton
        tb = QToolBar("ツールバー", self)
        tb.setObjectName("main_toolbar")
        tb.setMovable(True)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        self.toolbar = tb
        self._tb_buttons = []

        def add(cid, text, style=None):
            a = self.actions[cid]
            btn = QToolButton()
            btn.setText(text)
            btn.clicked.connect(a.trigger)
            btn.setToolTip(a.toolTip())
            self._tb_buttons.append((btn, a))
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if style:
                btn.setStyleSheet(style)
            tb.addWidget(btn)
            return btn
        for item in (("save_project", "💾 保存"), ("undo", "↶"), ("redo", "↷"), None,
                     ("import", "📥 読み込み"), ("import_interchange", "⇄ AAF / Premiere"), ("export", "📤 書き出し"), None,
                     ("splice_in", "⤵ 挿入", f"QToolButton {{ background: {C['splice']}; color: #1a1a1a; font-weight: bold; }}"),
                     ("overwrite", "⤓ 上書き", f"QToolButton {{ background: {C['overwrite']}; color: white; font-weight: bold; }}"),
                     ("lift", "リフト"), ("extract", "抽出"), ("add_edit", "✂ アドエディット"), ("trim_mode", "トリム"), None,
                     ("add_dissolve", "⨯ ディゾルブ"), ("add_telop", "T テロップ"), ("add_marker", "◆ マーカー"), None,
                     ("render_in_out", "⚙ レンダリング"), ("toggle_proxy", "Px"), ("fullscreen", "⛶")):
            if item is None:
                tb.addSeparator()
            else:
                add(*item)

    def apply_keymap(self, keymap):
        self.keymap = {k: list(v) for k, v in keymap.items()}
        for cid, a in self.actions.items():
            keys = [QKeySequence(k) for k in self.keymap.get(cid, [])]
            a.setShortcuts(keys)
            ks = " / ".join(k.toString(QKeySequence.SequenceFormat.NativeText) for k in keys)
            a.setToolTip(f"{COMMAND_INFO[cid][1]}" + (f"  [{ks}]" if ks else ""))
            a.setStatusTip(a.toolTip())
        for btn, a in getattr(self, "_tb_buttons", []):
            btn.setToolTip(a.toolTip())
        # フルスクリーン窓でもショートカットを効かせる
        if self.fullscreen_win is not None:
            for a in self.actions.values():
                self.fullscreen_win.addAction(a)

    def _a(self, menu, cid):
        menu.addAction(self.actions[cid])

    def _build_menus(self):
        mb = self.menuBar()
        f = mb.addMenu("ファイル")
        for c in ("new_bin", "new_sequence", None, "close_project", "new_project", "open_project"):
            f.addSeparator() if c is None else self._a(f, c)
        self.recent_menu = f.addMenu("最近使ったプロジェクト")
        self._rebuild_recent()
        for c in ("save_project", "save_as"):
            self._a(f, c)
        f.addAction("Attic（バックアップ）から復元...", self.restore_from_attic)
        f.addSeparator()
        self._a(f, "import")
        self._a(f, "import_interchange")
        f.addAction("テロップを読み込み（DavinTelop / SRT / CSV）...", self.import_telops)
        f.addAction("テロップ共通デザインを DavinTelop プリセットから設定...", self.set_design_from_preset)
        f.addSeparator()
        self._a(f, "export")
        ex = f.addMenu("他の形式で書き出し")
        for c in ("export_aaf", "export_xml", "export_edl", None, "export_frame", "export_wav",
                  "export_chapters", None, "export_queue"):
            ex.addSeparator() if c is None else self._a(ex, c)
        f.addAction("テロップを SRT で書き出し...", self.export_srt)
        f.addSeparator()
        mm = f.addMenu("メディア管理（NAS）")
        mm.addAction("オフライン素材を再リンク...", self.relink_media)
        mm.addAction("選択素材をローカルにコピー（バックグラウンド）", lambda: self.copy_local(self.bin.selected_ids()))
        mm.addAction("選択素材を元の場所（NAS）に戻す", lambda: self.restore_original(self.bin.selected_ids()))
        mm.addAction("使用中の素材をフォルダに収集...", self.consolidate_media)
        f.addSeparator()
        self._a(f, "keyboard_settings")
        self._a(f, "preferences")
        sm = self.menuBar().addMenu("設定")
        for c in ("user_settings", "project_settings", "preferences", None, "license"):
            sm.addSeparator() if c is None else self._a(sm, c)
        f.addSeparator()
        f.addAction("終了", QKeySequence("Ctrl+Q"), self.close)

        e = mb.addMenu("編集")
        for c in ("undo", "redo", None, "project_find", None, "cut", "copy_clipboard", "paste", "delete", "ripple_delete", None,
                  "select_all_clips", "deselect_all", "select_all_tracks", "deselect_all_tracks"):
            e.addSeparator() if c is None else self._a(e, c)

        c_ = mb.addMenu("クリップ")
        for c in ("splice_in", "overwrite", "replace_edit", "lift", "extract", None, "add_edit", "add_edit_all",
                  "ripple_trim_prev", "ripple_trim_next", "close_gaps", None, "add_dissolve", "add_fades",
                  "speed", "freeze_frame", "paste_attributes", None, "toggle_enabled", "toggle_link",
                  "match_frame", "find_bin", "subclip", "select_forward", None, "scene_detect", None, "add_telop"):
            c_.addSeparator() if c is None else self._a(c_, c)
        a_ = mb.addMenu("オーディオ")
        for c in ("normalize", "audio_crossfades", "silence_cut"):
            self._a(a_, c)
        a_.addSeparator()
        a_.addAction("オーディオミキサーを表示", lambda: (self.dock_mixer.show(), self.dock_mixer.raise_()))

        s = mb.addMenu("シーケンス")
        for c in ("mark_in", "mark_out", "mark_clip", "clear_in", "clear_out", "clear_both", None,
                  "add_marker"):
            s.addSeparator() if c is None else self._a(s, c)
        s.addAction("すべてのマーカーを削除", self.clear_markers)
        s.addSeparator()
        s.addAction("映像トラックを追加", lambda: self.add_track("video"))
        s.addAction("音声トラックを追加", lambda: self.add_track("audio"))
        s.addAction("シーケンス設定...", self.sequence_settings)

        tr = mb.addMenu("トリム")
        for c in ("trim_mode", None, "trim_m10", "trim_m1", "trim_p1", "trim_p10", None,
                  "trim_side_a", "trim_side_ab", "trim_side_b"):
            tr.addSeparator() if c is None else self._a(tr, c)

        pl = mb.addMenu("再生")
        for c in ("play_stop", "shuttle_j", "shuttle_k", "shuttle_l", "play_in_out", None,
                  "step_back_1", "step_fwd_1", "step_back_10", "step_fwd_10", "prev_edit", "next_edit",
                  "goto_start", "goto_end", "goto_in", "goto_out", "goto_tc", None, "toggle_monitor",
                  "loop_toggle", "fullscreen"):
            pl.addSeparator() if c is None else self._a(pl, c)
        pl.addSeparator()
        q = pl.addMenu("プレビュー画質")
        grp = QActionGroup(self)
        for lab, sc in (("フル", 1.0), ("1/2（標準）", 0.5), ("1/4（軽量）", 0.25)):
            a = q.addAction(lab, lambda sc=sc: self.set_preview_scale(sc))
            a.setCheckable(True)
            a.setChecked(sc == 0.5)
            grp.addAction(a)
        a = pl.addAction("セーフエリアを表示", self.toggle_safe_area)
        a.setCheckable(True)

        r = mb.addMenu("レンダリング")
        for c in ("render_in_out", "render_all", "render_cancel", None, "toggle_proxy", "proxy_selected"):
            r.addSeparator() if c is None else self._a(r, c)
        r.addAction("すべての重い素材（4K 等）のプロキシを作成", self.make_proxies_auto)
        r.addSeparator()
        r.addAction("レンダーキャッシュを削除", self.clear_render_cache)

        t = mb.addMenu("ツール")
        for c in ("tool_select", "tool_yellow", "tool_ripple", "tool_roll", "tool_slip", "tool_slide",
                  "tool_razor", "tool_hand", "tool_zoom", None, "snap_toggle", "zoom_in", "zoom_out",
                  "zoom_fit", "focus"):
            t.addSeparator() if c is None else self._a(t, c)

        w = mb.addMenu("ウィンドウ")
        for d in self.docks:
            w.addAction(d.toggleViewAction())
        self._a(w, "tc_window")
        self.composer_act = w.addAction("コンポーザーを別ウィンドウにする（2画面目へ）", self.toggle_composer_window)
        self.composer_act.setCheckable(True)
        w.addSeparator()
        self._a(w, "workspace_single")
        self._a(w, "workspace_dual")
        dm = w.addMenu("マルチディスプレイ（2 画面以上）")
        self.dual_act = dm.addAction("マルチディスプレイ用レイアウト", self.toggle_dual)
        self.dual_act.setCheckable(True)
        dm.addAction("推奨の配置に戻す", lambda: self.enter_dual(recommended=True))
        dm.addAction("画面の割り当て（編集 / パネル / 仕上げ / クライアントモニター）...", self.screen_roles_dialog)
        dm.addAction("編集画面とパネル画面を入れ替える", self.swap_dual_screens)
        auto = dm.addAction("ディスプレイをつないだら自動でレイアウトを切り替える", lambda: self.set_auto_dual(auto.isChecked()))
        auto.setCheckable(True)
        auto.setChecked(SETTINGS.auto_dual)
        self.ws_menu = w.addMenu("保存したワークスペース")
        self._rebuild_ws_menu()
        w.addAction("現在の配置をワークスペースとして保存...", self.save_workspace)
        w.addSeparator()
        self.fs_menu = w.addMenu("フルスクリーン出力先")
        self.fs_menu.aboutToShow.connect(self._rebuild_fs_menu)

        h = mb.addMenu("ヘルプ")
        self._a(h, "guidebook")
        self._a(h, "shortcuts_help")

    # ── 状態・共通 ───────────────────────────
    def active_player(self) -> Player:
        return self.source_player if self.active == "source" else self.record_player

    def set_active_monitor(self, kind: str):
        self.active = kind
        self.source_mon.set_active(kind == "source")
        self.record_mon.set_active(kind == "record")

    def tc_offset(self, seq: M.Sequence) -> int:
        try:
            return timecode_to_frames(seq.start_tc, seq.fps)
        except ValueError:
            return 0

    def media_frames(self, m: M.MediaItem) -> int:
        fps = self.project.sequence.fps
        if m.kind in ("video", "audio") and m.duration_s > 0:
            return max(1, int(m.duration_s * fps + 1e-6))
        return int(round(fps * 5))

    def peaks(self, m: M.MediaItem):
        if m.id in self._peaks:
            return self._peaks[m.id]
        p = MD.peaks_cache_path(m.path)
        arr = None
        if p.exists():
            try:
                import numpy as np
                arr = np.load(str(p))
            except Exception:
                arr = None
        if arr is not None:
            self._peaks[m.id] = arr
        return arr

    def thumb_pixmap(self, m: M.MediaItem):
        img = self._thumbs.get(m.id)
        if img is None:
            return None
        if isinstance(img, QImage):
            img = QPixmap.fromImage(img)
            self._thumbs[m.id] = img
        return img

    def text_design(self, clip: M.Clip):
        from .render import design_for
        return design_for(self.project, clip)

    def show_inspector(self):
        self.dock_insp.show()
        self.dock_insp.raise_()

    # ── Undo ───────────────────────────────
    def begin_edit(self):
        if self._edit_depth == 0:
            self._edit_snap = self.project.to_json()
        self._edit_depth += 1

    def end_edit(self, label: Optional[str], reload_inspector: bool = True):
        self._edit_depth = max(0, self._edit_depth - 1)
        if self._edit_depth == 0 and self._edit_snap is not None:
            now = self.project.to_json()
            if now != self._edit_snap:
                self._undo.append((label or "編集", self._edit_snap))
                if hasattr(self, "note_edit_position"):
                    self.note_edit_position()
                if len(self._undo) > 200:
                    del self._undo[0]
                self._redo.clear()
                self._set_dirty()
                if label:
                    self.status.showMessage(label, 2500)
            self._edit_snap = None
        self.refresh_all(reload_inspector)

    def _restore(self, snap: str):
        sel = set(self.canvas.selected)
        pos = self.record_player.position
        self.project = M.Project.from_json(snap)
        ids = {c.id for _, c in self.project.sequence.all_clips()}
        self.canvas.selected = sel & ids
        self.bin.refresh()
        self.refresh_all()
        self.record_player.seek(pos)

    def undo(self):
        if not self._undo:
            self.status.showMessage("元に戻す操作はありません", 2000)
            return
        label, snap = self._undo.pop()
        self._redo.append((label, self.project.to_json()))
        self._restore(snap)
        self._set_dirty()
        self.status.showMessage(f"元に戻す: {label}", 2500)

    def redo(self):
        if not self._redo:
            return
        label, snap = self._redo.pop()
        self._undo.append((label, self.project.to_json()))
        self._restore(snap)
        self._set_dirty()
        self.status.showMessage(f"やり直し: {label}", 2500)

    def refresh_all(self, reload_inspector: bool = True):
        seq = self.project.sequence
        self.timeline.seq_label.setText(f"{seq.name}   {seq.width}×{seq.height}  {seq.fps:g}fps")
        self.record_mon.tc_offset = self.tc_offset(seq)
        self.record_mon.set_info(seq.name, seq.fps, seq.duration, seq.width / max(1, seq.height))
        self.record_mon.set_marks(seq.mark_in, seq.mark_out, [m.frame for m in seq.markers])
        self.record_player.refresh()
        self.canvas.update()
        self.timeline.sync_scrollbars()
        if reload_inspector:
            self.inspector.load([c for _, c in self.canvas.selected_clips()])
        if hasattr(self, "_status_timer"):
            self._status_timer.start()
            self._idle_timer.start()
        if hasattr(self, "project_panel"):
            self.bin.refresh()
            self.project_panel.refresh()
        if hasattr(self, "marker_panel"):
            self.marker_panel.refresh()
            self.telop_panel.refresh()
            self.history_panel.refresh()
            self.mixer_panel.refresh()
            if hasattr(self, "telop_editor"):
                self.telop_editor.refresh()
            self.proxy_label.setText("PROXY" if SETTINGS.use_proxy else "")
            self.proxy_label.setStyleSheet(f"color: {C['splice']}; font-weight: bold; background: transparent;")

    def preview_changed(self):
        """インスペクタの変更を即プレビューに反映（Undo 確定は別）。"""
        self.record_player.refresh()
        self.canvas.update()

    def _set_dirty(self):
        self.dirty = True
        self._update_title()

    def _update_title(self):
        name = Path(self.project_path).name if self.project_path else "無題"
        from .version import VERSION
        from . import license as LIC
        lic = LIC.current()
        ed = "" if lic.source == "dev" else f" {lic.label}"
        self.setWindowTitle(f"{'*' if self.dirty else ''}{name} — {APP_NAME} {VERSION}{ed}")

    def _on_selection(self):
        self.inspector.load([c for _, c in self.canvas.selected_clips()])
        self._update_selection_status()

    def _on_record_position(self, f: int):
        self.canvas.update()
        if self.record_player.playing or not self._seek_from_timeline:
            self.canvas.ensure_visible(f)

    def seek_record(self, frame: int, quiet: bool = False, from_timeline: bool = False):
        self._seek_from_timeline = from_timeline or quiet
        self.record_player.seek(max(0, frame))
        self._seek_from_timeline = False
        if not quiet:
            self.set_active_monitor("record")

    # ── 再生・マーク ─────────────────────────
    def step(self, n: int):
        p = self.active_player()
        if p.playing:
            p.stop()
        p.step(n)

    def set_preview_scale(self, s: float):
        for p in (self.source_player, self.record_player):
            p.set_scale(s)
            p.refresh()

    def toggle_safe_area(self):
        for mon in (self.source_mon, self.record_mon):
            mon.view.safe_area = not mon.view.safe_area
            mon.view.update()

    def goto_edit(self, direction: int):
        p = self.active_player()
        if self.active == "record":
            seq = self.project.sequence
            pts = set(seq.edit_points()) | {m.frame for m in seq.markers}
        else:
            pts = {0, p.end_frame}
            m = self.project.media_by_id(self.source_media_id) if self.source_media_id else None
            if m:
                pts |= {x for x in (m.mark_in, m.mark_out) if x >= 0}
        pts = sorted(pts)
        cur = p.position
        if direction < 0:
            cand = [x for x in pts if x < cur]
            target = cand[-1] if cand else 0
        else:
            cand = [x for x in pts if x > cur]
            target = cand[0] if cand else cur
        p.seek(target)

    def mark(self, which: str):
        p = self.active_player()
        f = p.position
        if self.active == "record":
            seq = self.project.sequence
            self.begin_edit()
            if which == "in":
                seq.mark_in = f
                if seq.mark_out >= 0 and seq.mark_out <= f:
                    seq.mark_out = -1
            elif which == "out":
                seq.mark_out = f
                if seq.mark_in >= 0 and seq.mark_in >= f:
                    seq.mark_in = -1
            elif which == "clear_in":
                seq.mark_in = -1
            elif which == "clear_out":
                seq.mark_out = -1
            else:
                seq.mark_in = seq.mark_out = -1
            self.end_edit(None)
        else:
            m = self.project.media_by_id(self.source_media_id) if self.source_media_id else None
            if m is None:
                return
            if which == "in":
                m.mark_in = f
                if m.mark_out >= 0 and m.mark_out <= f:
                    m.mark_out = -1
            elif which == "out":
                m.mark_out = f
                if m.mark_in >= 0 and m.mark_in >= f:
                    m.mark_in = -1
            elif which == "clear_in":
                m.mark_in = -1
            elif which == "clear_out":
                m.mark_out = -1
            else:
                m.mark_in = m.mark_out = -1
            self.source_mon.set_marks(m.mark_in, m.mark_out)

    def goto_mark(self, which: str):
        if self.active == "record":
            seq = self.project.sequence
            f = seq.mark_in if which == "in" else seq.mark_out
        else:
            m = self.project.media_by_id(self.source_media_id) if self.source_media_id else None
            f = (m.mark_in if which == "in" else m.mark_out) if m else -1
        if f >= 0:
            self.active_player().seek(f)

    def goto_tc_dialog(self):
        seq = self.project.sequence
        off = self.tc_offset(seq) if self.active == "record" else 0
        cur = frames_to_timecode(self.active_player().position + off, seq.fps)
        tc, ok = QInputDialog.getText(self, "タイムコードへ移動", "タイムコード（数字のみも可: 1000 → 00:00:10:00）:", text=cur)
        if not ok:
            return
        try:
            f = timecode_to_frames(normalize_timecode(tc, seq.fps), seq.fps) - off
        except ValueError:
            return
        self.active_player().seek(max(0, f))

    def _monitor_action(self, kind: str, act: str):
        self.set_active_monitor(kind)
        p = self.active_player()
        acts = {
            "mark_in": lambda: self.mark("in"), "mark_out": lambda: self.mark("out"),
            "clear_marks": lambda: self.mark("clear"),
            "prev_edit": lambda: self.goto_edit(-1), "next_edit": lambda: self.goto_edit(1),
            "step_back": lambda: self.step(-1), "step_fwd": lambda: self.step(1),
            "play": p.toggle, "goto_in": lambda: self.goto_mark("in"),
            "goto_out": lambda: self.goto_mark("out"),
            "loop": lambda: (setattr(p, "loop", not p.loop),
                             self.status.showMessage("ループ再生: " + ("オン" if p.loop else "オフ"), 2000)),
            "insert": lambda: self.three_point_edit("insert"),
            "overwrite": lambda: self.three_point_edit("overwrite"),
            "lift": lambda: self.lift_extract(False), "extract": lambda: self.lift_extract(True),
            "add_telop": lambda: self.add_telop(), "goto_tc": self.goto_tc_dialog,
            "name_menu": lambda: self._name_menu(kind),
        }
        fn = acts.get(act)
        if fn:
            fn()

    # ── Avid コマンド ───────────────────────
    def play_stop(self):
        if self.trim:
            self._play_trim_loop()
            return
        self.active_player().toggle()

    def shuttle(self, key: str):
        if self.trim:
            self.exit_trim()
        self.active_player().shuttle(key)

    def play_in_out(self):
        """IN から OUT を再生（トリムモード中の P は「A 側」）。"""
        if self.trim:
            self.trim_side("a")
            return
        p = self.active_player()
        if self.active == "record":
            seq = self.project.sequence
            a, b = seq.mark_in, seq.mark_out
        else:
            m = self.project.media_by_id(self.source_media_id) if self.source_media_id else None
            a, b = (m.mark_in, m.mark_out) if m else (-1, -1)
        a = max(0, a)
        if b <= a:
            b = p.end_frame
        p.seek(a)
        p.play(1)
        self._stop_at = (p, b)
        if not hasattr(self, "_stop_timer"):
            self._stop_timer = QTimer(self)
            self._stop_timer.setInterval(15)
            self._stop_timer.timeout.connect(self._check_stop_at)
        self._stop_timer.start()

    def _check_stop_at(self):
        p, b = getattr(self, "_stop_at", (None, 0))
        if p is None or not p.playing:
            self._stop_timer.stop()
            return
        if p.position >= b:
            p.stop()
            p.seek(b)
            self._stop_timer.stop()

    def toggle_monitor(self):
        self.set_active_monitor("source" if self.active == "record" else "record")

    def toggle_loop(self):
        p = self.active_player()
        p.loop = not p.loop
        self.status.showMessage("ループ再生: " + ("オン" if p.loop else "オフ"), 2000)

    def mark_clip(self):
        """再生ヘッド位置のクリップ（または隙間）に IN/OUT を付ける（Avid の Mark Clip）。"""
        seq = self.project.sequence
        f = self.record_player.position
        tracks = [t for t in seq.tracks() if t.selected] or seq.tracks()
        for t in tracks:
            c = t.clip_at(f)
            if c is not None:
                a, b = c.start, c.end
                break
        else:
            ends = [c.end for t in tracks for c in t.clips if c.end <= f]
            starts = [c.start for t in tracks for c in t.clips if c.start > f]
            if not starts:
                return
            a, b = max(ends, default=0), min(starts)
        self.begin_edit()
        seq.mark_in, seq.mark_out = a, b
        self.end_edit(None)
        self.set_active_monitor("record")

    def delete_key(self):
        """Delete: 黄セグメントなら抽出（詰める）、それ以外はリフト（隙間を残す）。"""
        if self.canvas.tool == "yellow":
            self.ripple_delete_selected()
        elif self.canvas.selected:
            self.delete_selected()
        else:
            seq = self.project.sequence
            if seq.mark_in >= 0 and seq.mark_out > seq.mark_in:
                self.lift_extract(False)

    def select_tracks(self, on: bool):
        for t in self.project.sequence.tracks():
            t.selected = on
        self.set_dirty_light()
        self.canvas.update()

    def toggle_track(self, kind: str, idx: int):
        seq = self.project.sequence
        tracks = seq.video_tracks if kind == "video" else seq.audio_tracks
        if idx < len(tracks):
            tracks[idx].selected = not tracks[idx].selected
            self.set_dirty_light()
            self.canvas.update()

    def set_dirty_light(self):
        """Undo に積まない軽い変更（トラック選択など）。"""
        self._set_dirty()

    def toggle_focus(self):
        """Focus: 再生ヘッド周辺（約±3秒）を拡大 / 元の表示に戻す。"""
        c = self.canvas
        if self._focus_prev is not None:
            c.ppf, c.x0 = self._focus_prev
            self._focus_prev = None
        else:
            self._focus_prev = (c.ppf, c.x0)
            fps = self.project.sequence.fps
            c.ppf = max(1.0, (c.width() - 150) / (fps * 6))
            c.x0 = max(0.0, self.record_player.position - fps * 3)
        c.zoom_changed.emit()
        c.update()

    # ── トリムモード（Avid）──────────────────
    def toggle_trim(self):
        if self.trim:
            self.exit_trim()
        else:
            self.enter_trim()

    def enter_trim(self):
        seq = self.project.sequence
        f = self.record_player.position
        tracks = [t for t in seq.tracks() if t.selected and not t.locked and t.clips] or \
            [t for t in seq.tracks() if not t.locked and t.clips]
        pts = sorted({x for t in tracks for c in t.clips for x in (c.start, c.end)} - {0})
        if not pts:
            self.status.showMessage("トリムする編集点がありません", 3000)
            return
        e = min(pts, key=lambda p: abs(p - f))
        rollers = []
        for t in tracks:
            left = next((c for c in t.clips if c.end == e), None)
            right = next((c for c in t.clips if c.start == e), None)
            if left or right:
                rollers.append((t, left.id if left else None, right.id if right else None))
        if not rollers:
            self.status.showMessage("選択トラックに編集点がありません", 3000)
            return
        self.record_player.stop()
        self.trim = {"edit": e, "side": "ab", "rollers": rollers}
        self.source_player.set_sequence_provider(lambda: self.project.sequence)
        self.source_mon.set_overlay_title("A 側（出）")
        self.record_mon.set_overlay_title("B 側（入）")
        self._show_trim_frames()
        self.status.showMessage("トリムモード: M , . / でトリム、P [ ] で A側 / 両側 / B側、U で終了", 5000)

    def exit_trim(self):
        if not self.trim:
            return
        self.trim = None
        self.source_player.set_sequence_provider(lambda: self.source_seq)
        self.source_mon.set_overlay_title("ソース")
        self.record_mon.set_overlay_title("レコード")
        if self.source_media_id:
            self.load_source(self.source_media_id, self.source_player.position, activate=False)
        else:
            self.source_mon.view.clear("ビンの素材をダブルクリックで表示")
        self.canvas.update()

    def _show_trim_frames(self):
        e = self.trim["edit"]
        self.source_player.refresh()
        self.source_player.seek(max(0, e - 1))
        self.record_player.seek(e)
        self.canvas.ensure_visible(e)
        self.canvas.update()

    def trim_side(self, side: str):
        if not self.trim:
            self.status.showMessage("トリムモードではありません（U で開始）", 2500)
            return
        self.trim["side"] = side
        self.canvas.update()

    def trim_by(self, n: int):
        if not self.trim:
            self.status.showMessage("トリムモードではありません（U で開始）", 2500)
            return
        seq = self.project.sequence
        proj = self.project
        side = self.trim["side"]
        self.begin_edit()
        done_any = None
        first = True
        for t, lid, rid in self.trim["rollers"]:
            track, left = seq.find(lid) if lid else (t, None)
            track2, right = seq.find(rid) if rid else (t, None)
            tr = track or track2 or t
            mode = "ripple" if first else "ripple_linked"
            if side == "ab":
                if left is not None and right is not None:
                    d = M.roll_edit(seq, tr, left, right, n, proj)
                elif left is not None:
                    d = M.trim_edge(seq, tr, left, "out", n, proj, "normal")
                else:
                    d = M.trim_edge(seq, tr, right, "in", n, proj, "normal")
            elif side == "a" and left is not None:
                d = M.trim_edge(seq, tr, left, "out", n, proj, mode)
                first = False
            elif side == "b" and right is not None:
                d = M.trim_edge(seq, tr, right, "in", n, proj, mode)
                first = False
            else:
                continue
            if done_any is None:
                done_any = d
        if done_any and side in ("ab", "a"):
            self.trim["edit"] += done_any
        self.end_edit(f"トリム {n:+d}")
        if done_any is not None and done_any != n:
            self.status.showMessage(f"素材の長さ・隣のクリップで制限されました（{done_any:+d} フレーム）", 2500)
        self._show_trim_frames()

    def _play_trim_loop(self):
        """トリムモード中の再生: 編集点の前後 2 秒をループ再生。"""
        fps = self.project.sequence.fps
        e = self.trim["edit"]
        p = self.record_player
        if p.playing:
            p.stop()
            return
        a = max(0, e - int(fps * 2))
        p.seek(a)
        p.play(1)
        self._stop_at = (p, e + int(fps * 2))
        if not hasattr(self, "_stop_timer"):
            self._stop_timer = QTimer(self)
            self._stop_timer.setInterval(15)
            self._stop_timer.timeout.connect(self._check_stop_at)
        self._stop_timer.start()

    # ── レンダリング / プロキシ ───────────────
    def update_render_status(self):
        seq = self.project.sequence
        scale = self.record_player._worker.scale
        use_proxy = SETTINGS.use_proxy
        out = []
        for a, b in segments(seq):
            mid = (a + b) // 2
            cx = R.complexity(seq, mid, self.project)
            if cx == 0:
                continue
            samples = sorted({a, mid, b - 1, a + (b - a) // 4, a + (b - a) * 3 // 4})
            done = all(CACHE.on_disk(R.frame_signature(seq, f, self.project, scale, use_proxy))
                       for f in samples)
            out.append((a, b, "done" if done else ("red" if cx == 2 else "yellow")))
        self.render_status = out
        self.canvas.update()

    def start_render(self, in_out: bool, silent: bool = False):
        if self.renderer is not None:
            if not silent:
                self.status.showMessage("レンダリング中です", 2000)
            return
        seq = self.project.sequence
        if in_out and seq.mark_in >= 0 and seq.mark_out > seq.mark_in:
            a, b = seq.mark_in, seq.mark_out
        else:
            a, b = 0, seq.duration
        if b <= a:
            return
        r = BackgroundRenderer(self.project, a, b, self.record_player._worker.scale, only_heavy=True)
        self.renderer = r
        r.progress.connect(lambda d, n: self.render_label.setText(f"レンダリング中 {d}/{n}"))

        def done(n, cancelled):
            self.renderer = None
            self.render_label.setText("")
            if not silent or n:
                self.status.showMessage(("レンダリングを中止しました" if cancelled else
                                         f"レンダリング完了（{n} フレーム）"), 4000)
            self.update_render_status()
            self.record_player.refresh()
        r.finished_render.connect(done)
        r.start()
        if not silent:
            self.render_label.setText("レンダリング準備中...")

    def cancel_render(self):
        if self.renderer is not None:
            self.renderer.cancel.set()

    def _idle_render(self):
        if SETTINGS.idle_render and not self.record_player.playing and self.renderer is None \
                and any(st == "red" for _, _, st in self.render_status):
            self.start_render(False, silent=True)

    def clear_render_cache(self):
        if QMessageBox.question(self, "レンダーキャッシュ", "レンダー済みのキャッシュをすべて削除しますか？") \
                != QMessageBox.StandardButton.Yes:
            return
        self.cancel_render()
        CACHE.clear_disk()
        self.update_render_status()
        self.record_player.refresh()

    def make_proxies(self, items):
        items = [m for m in items if m.kind == "video" and os.path.exists(m.path) and not MD.has_proxy(m.path)]
        if not items:
            self.status.showMessage("プロキシを作成する素材はありません（作成済み・映像以外）", 3000)
            return
        w = ProxyWorker(items)
        w.progress.connect(lambda name, i, n: self.cache_label.setText(
            f"プロキシ作成中: {name} {int(100 * i / max(1, n))}%"))

        def item_done(mid, ok):
            m = self.project.media_by_id(mid)
            self.status.showMessage(f"プロキシ{'作成' if ok else '作成に失敗'}: {m.name if m else ''}", 3000)
            self.bin.refresh()
            self.record_player.refresh()
            self._status_timer.start()
        w.item_done.connect(item_done)
        w.finished.connect(lambda: (self.cache_label.setText(""),
                                    self._workers.remove(w) if w in self._workers else None))
        self._workers.append(w)
        w.start()

    def make_proxies_for_selection(self):
        ids = self.bin.selected_ids()
        if not ids:
            ids = list({c.media_id for _, c in self.canvas.selected_clips() if c.media_id})
        self.make_proxies([m for m in self.project.media if m.id in ids])

    def make_proxies_auto(self):
        self.make_proxies([m for m in self.project.media if (m.width or 0) > 1920])

    def toggle_proxy(self):
        SETTINGS.use_proxy = not SETTINGS.use_proxy
        SETTINGS.save()
        for p in (self.source_player, self.record_player):
            p.set_use_proxy(SETTINGS.use_proxy)
        self.status.showMessage("プロキシ再生: " + ("オン" if SETTINGS.use_proxy else "オフ（元素材）"), 3000)
        self._status_timer.start()

    # ── フルスクリーン / デュアルディスプレイ ────
    def _fullscreen_screen(self):
        scr = screens_sorted()
        idx = SETTINGS.fullscreen_screen
        if 0 <= idx < len(scr):
            return scr[idx]
        if self.dual_mode:
            # マルチディスプレイ中: クライアントモニター → 使っていない画面 → 仕上げ画面 の順に
            for role in ("client", "none", "finish"):
                hit = next((s for s, r in self.dual_roles if r == role), None)
                if hit is not None:
                    return hit
        here = self.screen()
        others = [s for s in scr if s is not here]
        return others[0] if others else here

    def toggle_fullscreen(self, on=None):
        """フルスクリーン再生（Avid: Ctrl+Shift+F）。2画面目があればそちらに出す。"""
        showing = self.fullscreen_win is not None and self.fullscreen_win.isVisible()
        if on is None:
            on = not showing
        if not on:
            if self.fullscreen_win is not None:
                for p in (self.source_player, self.record_player):
                    try:
                        p.image_changed.disconnect(self.fullscreen_win.set_image)
                    except TypeError:
                        pass
                self.fullscreen_win.close()
                self.fullscreen_win = None
            self.activateWindow()
            return
        win = FullscreenOutput(self)
        self.fullscreen_win = win
        mon = self.source_mon if self.active == "source" else self.record_mon
        player = self.source_player if self.active == "source" else self.record_player
        seq = self.project.sequence if self.active == "record" else (self.source_seq or self.project.sequence)
        win.aspect = seq.width / max(1, seq.height)
        if mon.view._img is not None:
            win.set_image(mon.view._img)
        player.image_changed.connect(win.set_image)
        for a in self.actions.values():
            win.addAction(a)
        win.show_on(self._fullscreen_screen())

    def _rebuild_fs_menu(self):
        self.fs_menu.clear()
        grp = QActionGroup(self.fs_menu)
        a = self.fs_menu.addAction("自動（メインウィンドウ以外の画面）",
                                   lambda: self._set_fs_screen(-1))
        a.setCheckable(True)
        a.setChecked(SETTINGS.fullscreen_screen < 0)
        grp.addAction(a)
        for i, name in enumerate(screen_names()):
            a = self.fs_menu.addAction(name, lambda i=i: self._set_fs_screen(i))
            a.setCheckable(True)
            a.setChecked(SETTINGS.fullscreen_screen == i)
            grp.addAction(a)

    def _set_fs_screen(self, i):
        SETTINGS.fullscreen_screen = i
        SETTINGS.save()

    def toggle_composer_window(self, on=None):
        """コンポーザーを独立ウィンドウにして別の画面へ置けるようにする / 中央に戻す。"""
        detached = self.composer_window is not None
        if on is None:
            on = not detached
        if on and not detached:
            win = QWidget(None, Qt.WindowType.Window)
            win.setWindowTitle("TeloCut — コンポーザー")
            win.setWindowIcon(self.windowIcon())
            from PyQt6.QtWidgets import QVBoxLayout as _VB
            lay = _VB(win)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(self.composer)
            for a in self.actions.values():
                win.addAction(a)
            win.closeEvent = lambda e: (e.ignore(), self.toggle_composer_window(False))
            win.resize(1100, 520)
            self.composer_window = win
            self.composer_host.hide()
            win.show()
            self.composer_act.setChecked(True)
        elif not on and detached:
            self.composer_host.layout().addWidget(self.composer)
            self.composer_host.show()
            w = self.composer_window
            self.composer_window = None
            w.closeEvent = lambda e: e.accept()
            w.close()
            self.composer_act.setChecked(False)

    def workspace_single(self):
        if self.dual_mode:
            self.leave_dual()
        self.toggle_composer_window(False)
        for d in self.docks:
            d.setFloating(False)
            d.show()
        self.restoreState(self._default_state)
        self.showMaximized()
        QTimer.singleShot(50, self._apply_default_sizes)

    def workspace_dual(self):
        """デュアルディスプレイ用の推奨レイアウト（dualdisplay.py）。"""
        self.enter_dual(recommended=True)

    def save_workspace(self):
        name, ok = QInputDialog.getText(self, "ワークスペースを保存", "名前:")
        if not ok or not name.strip():
            return
        self.settings.setValue(f"workspaces/{name.strip()}/state", self.saveState())
        self.settings.setValue(f"workspaces/{name.strip()}/geometry", self.saveGeometry())
        self._rebuild_ws_menu()

    def _rebuild_ws_menu(self):
        self.ws_menu.clear()
        self.settings.beginGroup("workspaces")
        names = self.settings.childGroups()
        self.settings.endGroup()
        if not names:
            a = self.ws_menu.addAction("（なし）")
            a.setEnabled(False)
        for n in names:
            self.ws_menu.addAction(n, lambda n=n: self.load_workspace(n))

    def load_workspace(self, name):
        geo = self.settings.value(f"workspaces/{name}/geometry")
        st = self.settings.value(f"workspaces/{name}/state")
        if geo is not None:
            self.restoreGeometry(geo)
        if st is not None:
            self.restoreState(st)
        ensure_docks_on_screen(self, self.docks)

    # ── 設定 ────────────────────────────────
    def keyboard_settings(self):
        dlg = KeyboardDialog(self, self.keymap, SETTINGS.keyboard_preset, SETTINGS.keyboard_layout)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        km = dlg.result_keymap()
        SETTINGS.keymap = km
        SETTINGS.keyboard_preset = dlg.preset
        SETTINGS.keyboard_layout = dlg.layout_name
        SETTINGS.save()
        self.apply_keymap(km)
        dup = conflicts(km)
        self.status.showMessage("キーボード設定を保存しました" + (f"（重複 {len(dup)} 件あり）" if dup else ""), 4000)

    def preferences(self):
        dlg = PreferencesDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dlg.apply()
        SETTINGS.ensure_dirs()
        self._restart_autosave()
        for p in (self.source_player, self.record_player):
            p.set_use_proxy(SETTINGS.use_proxy)
        self._status_timer.start()

    def _restart_autosave(self):
        self._autosave_timer.stop()
        if SETTINGS.autosave_minutes > 0:
            self._autosave_timer.start(SETTINGS.autosave_minutes * 60_000)

    def autosave(self):
        """自動保存（Avid の Auto-Save）。保存済みプロジェクトは上書き保存、無題は Attic へ。"""
        if not self.dirty:
            return
        if self.project_path and not self.read_only:
            if self.save_project(quiet=True):
                self.status.showMessage("自動保存しました", 2000)
        else:
            attic_backup(self.project_path, self.project.to_json())
            self.status.showMessage("自動保存（Attic）しました", 2000)

    def restore_from_attic(self):
        files = list_attic(self.project_path)
        if not files:
            QMessageBox.information(self, "Attic", "このプロジェクトのバックアップはまだありません。")
            return
        labels = [f"{p.stem[-15:-7]} {p.stem[-6:-4]}:{p.stem[-4:-2]}:{p.stem[-2:]}  ({p.name})" for p in files]
        lab, ok = QInputDialog.getItem(self, "Attic から復元", "復元するバックアップ:", labels, 0, False)
        if not ok or not self._confirm_discard():
            return
        p = files[labels.index(lab)]
        try:
            proj = M.Project.from_json(p.read_text(encoding="utf-8"))
        except Exception as ex:
            QMessageBox.critical(self, "Attic", str(ex))
            return
        keep_path = self.project_path
        self._load_project(proj, keep_path)
        self._set_dirty()
        self.status.showMessage(f"Attic から復元しました: {p.name}（保存すると上書きされます）", 5000)

    # ── メディア管理（NAS）──────────────────
    def relink_media(self):
        offline = resolve_media(self.project, self.project_path)
        if not offline:
            QMessageBox.information(self, "再リンク", "オフラインの素材はありません。")
            return
        RelinkDialog(self, self.project, offline).exec()
        MD._KEY_MEMO.clear()
        self._after_relink()

    def _after_relink(self):
        self._set_dirty()
        self.bin.refresh()
        self._rebuild_caches([m for m in self.project.media if os.path.exists(m.path)])
        self.record_player.refresh()
        self.source_player.refresh()

    def copy_local(self, ids):
        jobs = []
        local = Path(SETTINGS.local_media_dir)
        for m in self.project.media:
            if m.id not in ids or not os.path.exists(m.path):
                continue
            if Path(m.path).resolve().as_posix().startswith(local.resolve().as_posix()):
                continue
            jobs.append((m.id, m.path, str(local / f"{MD._cache_key(m.path)}_{m.name}")))
        if not jobs:
            self.status.showMessage("コピーする素材がありません（ビンで素材を選択）", 3000)
            return
        w = CopyWorker(jobs)
        w.progress.connect(lambda n, d, t: self.cache_label.setText(
            f"ローカルにコピー中: {n} {int(100 * d / max(1, t))}%"))

        def item_done(mid, dst, ok):
            m = self.project.media_by_id(mid)
            if m is None:
                return
            if ok:
                m.original_path = m.original_path or m.path
                m.path = dst
                self._set_dirty()
                self.record_player.refresh()
                self.source_player.refresh()
            self.bin.refresh()
            self.status.showMessage(f"{'ローカルにコピーしました' if ok else 'コピーに失敗'}: {m.name}", 3000)
        w.item_done.connect(item_done)
        w.finished.connect(lambda: (self.cache_label.setText(""),
                                    self._workers.remove(w) if w in self._workers else None))
        self._workers.append(w)
        w.start()
        self.status.showMessage("コピー中も元の場所の素材で編集を続けられます", 4000)

    def restore_original(self, ids):
        n = 0
        for m in self.project.media:
            if m.id in ids and m.original_path:
                orig = m.original_path if os.path.exists(m.original_path) else SETTINGS.map_path(m.original_path)
                if os.path.exists(orig):
                    m.path, m.original_path = orig, ""
                    n += 1
        if n:
            self._after_relink()
        self.status.showMessage(f"{n} 個の素材を元の場所に戻しました", 3000)

    def consolidate_media(self):
        used = {c.media_id for s in self.project.sequences for _, c in s.all_clips()}
        items = [m for m in self.project.media if m.id in used and os.path.exists(m.path)]
        if not items:
            QMessageBox.information(self, "メディア収集", "収集する素材がありません。")
            return
        d = QFileDialog.getExistingDirectory(self, "素材を集めるフォルダ（例: NAS のプロジェクトフォルダ）")
        if not d:
            return
        names = set()
        jobs = []
        for m in items:
            name = m.name
            k = 1
            while name.lower() in names:
                stem, ext = os.path.splitext(m.name)
                name = f"{stem}_{k}{ext}"
                k += 1
            names.add(name.lower())
            jobs.append((m.id, m.path, os.path.join(d, name)))
        w = CopyWorker(jobs)
        w.progress.connect(lambda n, dd, t: self.cache_label.setText(f"収集中: {n} {int(100 * dd / max(1, t))}%"))

        def item_done(mid, dst, ok):
            m = self.project.media_by_id(mid)
            if ok and m is not None:
                m.path, m.original_path = dst, ""
                self._set_dirty()
        w.item_done.connect(item_done)
        w.finished.connect(lambda: (self.cache_label.setText(""), self.bin.refresh(),
                                    self.status.showMessage("メディア収集が完了しました。プロジェクトを保存してください。", 5000),
                                    self._workers.remove(w) if w in self._workers else None))
        self._workers.append(w)
        w.start()

    # ── ビン / シーケンス（Avid）──────────────
    def _name_menu(self, kind: str):
        from PyQt6.QtGui import QCursor
        from PyQt6.QtWidgets import QMenu
        m = QMenu(self)
        if kind == "record":
            for sq in self.project.sequences:
                a = m.addAction(sq.name, lambda sid=sq.id: self.load_sequence(sid))
                a.setCheckable(True)
                a.setChecked(sq is self.project.sequence)
            m.addSeparator()
            m.addAction("新規シーケンス", lambda: self.new_sequence())
        else:
            for b in self.project.bins:
                sub = m.addMenu(b.name)
                for mid in b.ids("media"):
                    md = self.project.media_by_id(mid)
                    if md:
                        sub.addAction(md.name, lambda i=mid: self.load_source(i))
        m.exec(QCursor.pos())

    def load_sequence(self, sid: str):
        """ビンのシーケンスをレコードモニター / タイムラインに表示（Avid: ダブルクリック）。"""
        idx = next((i for i, s in enumerate(self.project.sequences) if s.id == sid), None)
        if idx is None or idx == self.project.current:
            return
        self.exit_trim()
        self.record_player.stop()
        self.project.sequence.playhead = self.record_player.position
        self.project.current = idx
        self.canvas.selected = set()
        self.patch = {"V1": 0, "A1": 0}
        self.refresh_all()
        self.record_player.seek(self.project.sequence.playhead)
        self.canvas.zoom_fit()
        self.set_active_monitor("record")
        self._set_dirty()

    def new_sequence(self, bin_id: Optional[str] = None):
        cur = self.project.sequence
        name, ok = QInputDialog.getText(self, "新規シーケンス", "シーケンス名:",
                                        text=f"シーケンス {len(self.project.sequences) + 1:02d}")
        if not ok:
            return
        self.begin_edit()
        fmt = self.project.settings.get("format") or {}         # プロジェクト設定の形式（無ければ今のシーケンス）
        sq = M.Sequence.create(name=name.strip() or "シーケンス", fps=float(fmt.get("fps", cur.fps)),
                               width=int(fmt.get("width", cur.width)), height=int(fmt.get("height", cur.height)),
                               start_tc=fmt.get("start_tc", cur.start_tc))
        self.project.sequences.append(sq)
        b = self.project.bin_by_id(bin_id or self.bin.current_bin_id()) or self.project.bins[0]
        b.add("sequence", sq.id)
        self.end_edit("新規シーケンス")
        self.load_sequence(sq.id)

    def duplicate_sequence(self, sid: str):
        src = self.project.sequence_by_id(sid)
        if src is None:
            return
        self.begin_edit()
        sq = M.Sequence.from_dict(copy.deepcopy(__import__("dataclasses").asdict(src)))
        sq.id = M.new_id()
        sq.name = src.name + " のコピー"
        for _, c in sq.all_clips():
            c.id = M.new_id()
        self.project.sequences.append(sq)
        (self.project.bin_of(sid) or self.project.bins[0]).add("sequence", sq.id)
        self.end_edit("シーケンスを複製")

    def rename_sequence(self, sid: str):
        sq = self.project.sequence_by_id(sid)
        if sq is None:
            return
        name, ok = QInputDialog.getText(self, "シーケンス名", "名前:", text=sq.name)
        if ok and name.strip():
            self.begin_edit()
            sq.name = name.strip()
            self.end_edit("シーケンス名を変更")

    def new_bin(self, name: Optional[str] = None):
        if name is None:
            name, ok = QInputDialog.getText(self, "新規ビン", "ビン名:", text=f"ビン {len(self.project.bins) + 1}")
            if not ok or not name.strip():
                return
        self.begin_edit()
        b = M.Bin(M.new_id(), name.strip())
        self.project.bins.append(b)
        self.end_edit("新規ビン")
        self.bin.open_bin(b.id)

    def delete_bin(self, bid: str):
        b = self.project.bin_by_id(bid)
        if b is None:
            return
        if len(self.project.bins) <= 1:
            QMessageBox.information(self, "ビン", "最後のビンは削除できません。")
            return
        if self.project.sequence.id in b.ids("sequence"):
            QMessageBox.information(self, "ビン", "表示中のシーケンスが入っているビンは削除できません。")
            return
        if b.items and QMessageBox.question(
                self, "ビンの削除", f"「{b.name}」の中身（{len(b.items)} 個）も削除しますか？\n"
                "（シーケンスで使われている素材は残します）") != QMessageBox.StandardButton.Yes:
            return
        used = {c.media_id for sq in self.project.sequences for _, c in sq.all_clips()}
        self.begin_edit()
        keep = M.Bin(M.new_id(), "削除したビンの使用中素材")
        for it in b.items:
            if it["type"] == "sequence":
                self.project.sequences = [sq for sq in self.project.sequences if sq.id != it["id"]]
            elif it["id"] in used:
                keep.add("media", it["id"])
            else:
                self.project.media = [m for m in self.project.media if m.id != it["id"]]
        self.project.bins.remove(b)
        if keep.items:
            self.project.bins.append(keep)
        self.project.current = next((i for i, sq in enumerate(self.project.sequences)
                                     if sq.id == self.project.sequence.id), 0)
        self.end_edit("ビンを削除")

    def move_to_bin(self, ids, bid: str):
        dst = self.project.bin_by_id(bid)
        if dst is None:
            return
        self.begin_edit()
        for i in ids:
            src = self.project.bin_of(i)
            typ = "sequence" if self.project.sequence_by_id(i) else "media"
            if src is not None:
                src.remove(i)
            dst.add(typ, i)
        self.end_edit("ビンへ移動")

    def delete_bin_items(self, sel):
        mids = [i for t, i in sel if t == "media"]
        sids = [i for t, i in sel if t == "sequence"]
        if self.project.sequence.id in sids:
            QMessageBox.information(self, "削除", "表示中のシーケンスは削除できません。別のシーケンスを表示してから削除してください。")
            sids.remove(self.project.sequence.id)
        if sids:
            if QMessageBox.question(self, "シーケンスの削除", f"{len(sids)} 個のシーケンスを削除しますか？") \
                    == QMessageBox.StandardButton.Yes:
                cur_id = self.project.sequence.id
                self.begin_edit()
                self.project.sequences = [sq for sq in self.project.sequences if sq.id not in sids]
                for b in self.project.bins:
                    for i in sids:
                        b.remove(i)
                self.project.current = next(i for i, sq in enumerate(self.project.sequences) if sq.id == cur_id)
                self.end_edit("シーケンスを削除")
        if mids:
            self.remove_media(mids)

    def create_project(self, path: str, name: str, w: int, h: int, fps: float, tc: str, template: dict = None) -> bool:
        """プロジェクトの選択で「新規」: フォルダとプロジェクトファイルを作って開く（テンプレートからも）。"""
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        except OSError as ex:
            QMessageBox.critical(self, "新規プロジェクト", str(ex))
            return False
        if template and template.get("builtin") != "empty":
            from . import templates as TP
            proj = TP.project_from_template(template, name, w, h, fps, tc)
        else:
            proj = M.Project(name=name)
            proj.sequences = [M.Sequence.create(name="シーケンス 01", fps=fps, width=w, height=h, start_tc=tc)]
            proj.bins = [M.Bin(M.new_id(), f"{name} ビン")]
            proj.settings["format"] = {"width": w, "height": h, "fps": fps, "start_tc": tc}
            proj.ensure_bins()
        self.lock.release()
        self.read_only = False
        self._load_project(proj, path)
        if not self.save_project():
            return False
        self._add_recent(path)
        return True

    def run_select_project(self) -> bool:
        """プロジェクトの選択画面。開いたら True、終了を選んだら False。"""
        while True:
            dlg = SelectProjectDialog(self if self.isVisible() else None)
            if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_action:
                return False
            act = dlg.result_action
            if act[0] == "open":
                self.dirty = False
                self.open_project(act[1])
                if self.project_path:
                    return True
            elif act[0] == "new":
                if self.create_project(*act[1:]):
                    self.maybe_start_panel()
                    return True

    def close_project(self):
        """プロジェクトを閉じてプロジェクトの選択へ（Avid と同じ流れ）。"""
        if not self._confirm_discard():
            return
        self.lock.release()
        self.read_only = False
        self.dirty = False
        self.hide()
        self.toggle_fullscreen(False)
        if self.run_select_project():
            self.show()
        else:
            self.close()

    # ── 他ソフトとの読み込み / 書き出し ─────────
    def import_interchange(self, path: Optional[str] = None):
        """AAF / Premiere プロジェクト / Final Cut Pro XML / EDL を読み込んで新しいビンに入れる。"""
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "他の編集ソフトから読み込み", self.settings.value("last_dir", ""),
                "対応ファイル (*.aaf *.prproj *.xml *.edl);;AAF (*.aaf);;Premiere Pro プロジェクト (*.prproj);;"
                "Final Cut Pro XML (*.xml);;EDL (*.edl)")
        if not path:
            return
        from . import interchange as IX
        from PyQt6.QtWidgets import QProgressDialog
        prog = QProgressDialog("読み込み中...", None, 0, 0, self)
        prog.setWindowTitle("読み込み")
        prog.setMinimumDuration(300)
        prog.show()
        QApplication.processEvents()
        try:
            imported = IX.detect_and_import(path, self.project, self.project.sequence.fps)
            if not imported:
                prog.close()
                QMessageBox.information(self, "読み込み", "シーケンスが見つかりませんでした。")
                return
            self.begin_edit()
            seqs, warns = IX.build_into_project(self.project, imported, f"{Path(path).stem}（{Path(path).suffix[1:].upper()}）")
            self.end_edit(f"{Path(path).name} を読み込み")
        except Exception as ex:
            prog.close()
            QMessageBox.critical(self, "読み込み", f"読み込みに失敗しました:\n{ex}")
            return
        prog.close()
        self.bin.open_bin(self.project.bins[-1].id)
        if seqs:
            self.load_sequence(seqs[0].id)
        self._rebuild_caches([m for m in self.project.media if os.path.exists(m.path)])
        msg = f"{len(seqs)} 個のシーケンスを読み込みました。"
        uniq = list(dict.fromkeys(warns))
        if uniq:
            msg += "\n\n注意:\n" + "\n".join(uniq[:15]) + ("\n…" if len(uniq) > 15 else "")
            if any(w.startswith("オフライン") for w in uniq):
                msg += "\n\nオフライン素材は ファイル → メディア管理 → 再リンク で場所を指定できます。"
        QMessageBox.information(self, "読み込み", msg)

    def _export_with(self, title, filt, ext, fn):
        seq = self.project.sequence
        path, _ = QFileDialog.getSaveFileName(self, title, f"{seq.name}{ext}", filt)
        if not path:
            return
        try:
            fn(path)
        except Exception as ex:
            QMessageBox.critical(self, title, f"書き出しに失敗しました:\n{ex}")
            return
        self.status.showMessage(f"書き出しました: {path}", 5000)

    def export_aaf_file(self):
        from . import interchange as IX
        self._export_with("AAF を書き出し", "AAF (*.aaf)", ".aaf",
                          lambda p: IX.export_aaf(self.project, self.project.sequence, p))

    def export_xml_file(self):
        from . import interchange as IX
        self._export_with("Final Cut Pro XML を書き出し", "XML (*.xml)", ".xml",
                          lambda p: IX.export_fcpxml(self.project, self.project.sequence, p))

    def export_edl_file(self):
        from . import interchange as IX
        seq = self.project.sequence
        vt = next((i for i, t in enumerate(seq.video_tracks) if t.selected), 0)
        self._export_with("EDL を書き出し", "EDL (*.edl)", ".edl",
                          lambda p: IX.export_edl(self.project, seq, p, vt))

    def export_frame(self):
        seq = self.project.sequence
        f = self.record_player.position
        path, _ = QFileDialog.getSaveFileName(self, "フレームを書き出し",
                                              f"{seq.name}_{frames_to_timecode(f + self.tc_offset(seq), seq.fps).replace(':', '')}.png",
                                              "PNG (*.png);;JPEG (*.jpg)")
        if not path:
            return
        pool = MD.DecoderPool()
        try:
            R.render_frame(seq, f, self.project, pool, 1.0).save(path)
        finally:
            pool.close()
        self.status.showMessage(f"フレームを書き出しました: {path}", 4000)

    def export_wav(self):
        import wave
        seq = self.project.sequence
        a, b = (seq.mark_in, seq.mark_out) if seq.mark_in >= 0 and seq.mark_out > seq.mark_in else (0, seq.duration)
        if b <= a:
            return
        path, _ = QFileDialog.getSaveFileName(self, "音声を書き出し", f"{seq.name}.wav", "WAV (*.wav)")
        if not path:
            return
        pool = MD.DecoderPool()
        s0, s1 = int(round(a * MD.SAMPLE_RATE / seq.fps)), int(round(b * MD.SAMPLE_RATE / seq.fps))
        with wave.open(path, "wb") as w:
            w.setnchannels(MD.CHANNELS)
            w.setsampwidth(2)
            w.setframerate(MD.SAMPLE_RATE)
            for s in range(s0, s1, MD.SAMPLE_RATE * 10):
                w.writeframes(R.to_int16(R.mix_audio(seq, self.project, pool, s, min(MD.SAMPLE_RATE * 10, s1 - s))))
        self.status.showMessage(f"音声を書き出しました: {path}", 4000)

    def export_chapters(self):
        seq = self.project.sequence
        if not seq.markers:
            QMessageBox.information(self, "チャプター", "マーカーがありません（M / Shift+M で追加）。")
            return

        def fmt(f):
            s = int(f / seq.fps)
            h, m, sec = s // 3600, (s // 60) % 60, s % 60
            return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
        marks = sorted(seq.markers, key=lambda m: m.frame)
        lines = []
        if marks[0].frame > 0:
            lines.append("00:00 オープニング")
        for i, mk in enumerate(marks):
            lines.append(f"{fmt(mk.frame)} {mk.name or f'チャプター {i + 1}'}")
        text = "\n".join(lines)
        QApplication.clipboard().setText(text)
        path, _ = QFileDialog.getSaveFileName(self, "チャプターを保存", f"{seq.name}_chapters.txt", "テキスト (*.txt)")
        if path:
            Path(path).write_text(text + "\n", encoding="utf-8")
        self.status.showMessage("YouTube チャプターをクリップボードにコピーしました", 4000)

    def export_queue(self):
        from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QProgressDialog
        from .export import ExportWorker, ENCODERS
        dlg = QDialog(self)
        dlg.setWindowTitle("書き出しキュー")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("書き出すシーケンスにチェック:"))
        lst = QListWidget()
        for sq in self.project.sequences:
            it = QListWidgetItem(f"{sq.name}   ({frames_to_timecode(sq.duration, sq.fps)})")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if sq.duration > 0 else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, sq.id)
            lst.addItem(it)
        v.addWidget(lst)
        f = QFormLayout()
        enc = QComboBox()
        for label, codec, ext in ENCODERS:
            if codec != "h264_nvenc":
                enc.addItem(label, (codec, ext))
        f.addRow("形式", enc)
        folder = QLineEdit(os.path.join(os.path.expanduser("~"), "Desktop"))
        f.addRow("出力フォルダ", folder)
        from PyQt6.QtWidgets import QCheckBox
        to_farm = QCheckBox("ファームに送る（複数 PC で同時に書き出し・この PC は空く）")
        to_farm.setEnabled(bool(SETTINGS.asset_server))
        f.addRow("", to_farm)
        v.addLayout(f)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        ids = [lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lst.count())
               if lst.item(i).checkState() == Qt.CheckState.Checked]
        codec, ext = enc.currentData()
        if to_farm.isChecked():
            cur = self.project.current
            n = 0
            try:
                for sid in ids:
                    self.project.current = next(k for k, s in enumerate(self.project.sequences) if s.id == sid)
                    sq = self.project.sequence
                    if sq.duration > 0 and self.farm_submit_export(0, sq.duration, codec,
                                                                   os.path.join(folder.text(), f"{sq.name}{ext}")):
                        n += 1
            except Exception as ex:
                QMessageBox.warning(self, "ファーム", f"送れませんでした: {ex}")
            finally:
                self.project.current = cur
            self.show_dock(self.dock_farm)
            self.status.showMessage(f"{n} 本をファームに送りました", 6000)
            return
        prog = QProgressDialog("書き出し中...", "中止", 0, len(ids), self)
        prog.setWindowTitle("書き出しキュー")
        prog.show()
        done = 0
        for i, sid in enumerate(ids):
            if prog.wasCanceled():
                break
            idx = next(k for k, s in enumerate(self.project.sequences) if s.id == sid)
            proj = copy.deepcopy(self.project)
            proj.current = idx
            sq = proj.sequence
            out = os.path.join(folder.text(), f"{sq.name}{ext}")
            prog.setLabelText(f"{i + 1}/{len(ids)}: {sq.name}")
            w = ExportWorker(proj, 0, sq.duration, out, codec, 20)
            res = {}
            w.finished_ok.connect(lambda p: res.setdefault("ok", p))
            w.failed.connect(lambda e: res.setdefault("err", e))
            w.start()
            while not res and not prog.wasCanceled():
                QApplication.processEvents()
                w.wait(50)
            if prog.wasCanceled():
                w.cancel.set()
                w.wait(3000)
                break
            w.wait(3000)
            done += 1 if "ok" in res else 0
            prog.setValue(i + 1)
        prog.close()
        QMessageBox.information(self, "書き出しキュー", f"{done} / {len(ids)} 個のシーケンスを書き出しました。")

    # ── 編集の新機能 ─────────────────────────
    def add_dissolve(self, sec: float = 1.0):
        """クロスディゾルブ: 選択クリップの頭、または再生ヘッドに最も近い編集点（選択トラック）。"""
        seq = self.project.sequence
        n = int(round(sec * seq.fps))
        targets = [c for t, c in self.canvas.selected_clips() if t.kind == "video"]
        if not targets:
            f = self.record_player.position
            vts = [t for t in seq.video_tracks if t.selected and not t.locked] or seq.video_tracks
            best = None
            for t in vts:
                for c in t.clips:
                    if c.start > 0 and (best is None or abs(c.start - f) < abs(best.start - f)):
                        best = c
            targets = [best] if best else []
        if not targets:
            self.status.showMessage("ディゾルブを付ける編集点がありません", 3000)
            return
        self.begin_edit()
        for c in targets:
            c.props["dissolve"] = max(0, min(n, c.duration))
        self.end_edit("クロスディゾルブ" if n else "ディゾルブを解除")

    def speed_dialog(self):
        sel = [(t, c) for t, c in self.canvas.selected_clips() if c.kind in ("video", "audio")]
        if not sel:
            self.status.showMessage("速度を変える映像・音声クリップを選択してください", 3000)
            return
        cur = float(sel[0][1].prop("speed", 1.0) or 1.0) * 100
        v, ok = QInputDialog.getDouble(self, "速度", "再生速度（%）　負の値で逆再生:", cur, -1000, 1000, 1)
        if not ok or v == 0:
            return
        keep = QMessageBox.question(self, "速度", "クリップの長さも速度に合わせて変えますか？\n"
                                    "（いいえ = 長さはそのまま、使う素材の範囲が変わります）")
        self.begin_edit()
        proj = self.project
        for t, c in sel:
            old = abs(float(c.prop("speed", 1.0) or 1.0))
            c.props["speed"] = v / 100
            if keep == QMessageBox.StandardButton.Yes:
                new_dur = max(1, int(round(c.duration * old / abs(v / 100))))
                M.trim_edge(proj.sequence, t, c, "out", new_dur - c.duration, proj, "normal")
        self.end_edit(f"速度 {v:g}%")

    def freeze_frame(self):
        """再生ヘッドの位置に 2 秒のフリーズフレームを挿入（後ろは押し出す）。"""
        seq = self.project.sequence
        f = self.record_player.position
        tracks = [t for t in reversed(seq.video_tracks) if not t.locked]
        for t in tracks:
            c = t.clip_at(f)
            if c is not None and c.kind == "video":
                break
        else:
            self.status.showMessage("再生ヘッドの位置に映像クリップがありません", 3000)
            return
        dur = int(round(seq.fps * 2))
        fr = c.copy(id=M.new_id(), start=f, duration=dur, link="",
                    src_in=int(R.src_frame(c, f)))
        fr.props["speed"] = 0.0
        fr.props["dissolve"] = 0
        fr.name = (c.name or "") + "（静止）"
        self.begin_edit()
        M.place_clips(seq, [(t, fr)], "insert")
        self.end_edit("フリーズフレーム")
        self.canvas.selected = {fr.id}
        self.canvas.selection_changed.emit()

    def normalize(self):
        from . import analysis as AN
        sel = [c for _, c in self.canvas.selected_clips() if c.kind == "audio"]
        if not sel:
            self.status.showMessage("ノーマライズする音声クリップを選択してください", 3000)
            return
        self.begin_edit()
        n = 0
        for c in sel:
            m = self.project.media_by_id(c.media_id)
            pk = AN.clip_peak_db(m, c, self.project.sequence.fps) if m else None
            if pk is not None:
                c.props["volume_db"] = round(-1.0 - pk, 1)
                n += 1
        self.end_edit(f"ノーマライズ（{n} クリップ）")

    def audio_crossfades(self):
        seq = self.project.sequence
        n, ok = QInputDialog.getInt(self, "オーディオクロスフェード", "長さ（フレーム）:", 6, 1, 300)
        if not ok:
            return
        self.begin_edit()
        cnt = 0
        for t in seq.audio_tracks:
            if t.locked:
                continue
            for a, b in zip(t.clips, t.clips[1:]):
                if a.end == b.start:
                    a.props["fade_out"] = min(n, a.duration // 2)
                    b.props["fade_in"] = min(n, b.duration // 2)
                    cnt += 1
        self.end_edit(f"オーディオクロスフェード（{cnt} 箇所）")

    def silence_cut(self):
        """無音カット: 選択した音声クリップ（映像を選んだらリンクした音声）の無音部分を詰める。"""
        from . import analysis as AN
        seq = self.project.sequence
        sel = self.canvas.selected_clips()
        audio = [(t, c) for t, c in sel if c.kind == "audio"]
        if not audio:
            for t, c in sel:
                audio += [(lt, lc) for lt, lc in seq.linked(c) if lc.kind == "audio"]
        if not audio:
            self.status.showMessage("無音カットする音声クリップ（または音声付きの映像）を選択してください", 3500)
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("無音カット")
        fl = QFormLayout(dlg)
        th = QDoubleSpinBox()
        th.setRange(-80, -10)
        th.setValue(-40)
        th.setSuffix(" dB 以下を無音とみなす")
        mn = QDoubleSpinBox()
        mn.setRange(0.1, 10)
        mn.setValue(0.5)
        mn.setSuffix(" 秒以上続いたら")
        pad = QDoubleSpinBox()
        pad.setRange(0, 2)
        pad.setValue(0.1)
        pad.setSingleStep(0.05)
        pad.setSuffix(" 秒（前後に残す余白）")
        fl.addRow("しきい値", th)
        fl.addRow("最短の無音", mn)
        fl.addRow("余白", pad)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        fl.addRow(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        ranges = []
        for t, c in audio:
            m = self.project.media_by_id(c.media_id)
            if m is None:
                continue
            for a, b in AN.silent_ranges(m, c, seq.fps, th.value(), mn.value(), pad.value()):
                tracks = {id(t): t}
                for lt, _lc in seq.linked(c):
                    tracks[id(lt)] = lt
                ranges.append((a, b, list(tracks.values())))
        if not ranges:
            self.status.showMessage("無音区間は見つかりませんでした", 3000)
            return
        self.begin_edit()
        total = 0
        for a, b, tracks in sorted(ranges, key=lambda r: -r[0]):
            # 同期を保つためシンクロックの空きトラックも詰める
            extra = [t for t in seq.tracks() if t.sync_lock and not t.locked and t not in tracks
                     and t.is_empty(a, b)]
            M.extract(seq, a, b, tracks + extra)
            total += b - a
        self.end_edit(f"無音カット（{len(ranges)} 箇所）")
        self.status.showMessage(f"無音カット: {len(ranges)} 箇所、{frames_to_timecode(total, seq.fps)} 短くなりました", 6000)

    def scene_detect(self):
        from . import analysis as AN
        from PyQt6.QtWidgets import QProgressDialog
        import threading
        seq = self.project.sequence
        sel = [(t, c) for t, c in self.canvas.selected_clips() if c.kind == "video"]
        if not sel:
            self.status.showMessage("シーンチェンジを検出する映像クリップを選択してください", 3000)
            return
        sens, ok = QInputDialog.getInt(self, "シーンチェンジ検出", "感度（1〜10、大きいほど細かく検出）:", 5, 1, 10)
        if not ok:
            return
        cancel = threading.Event()
        prog = QProgressDialog("解析中...", "中止", 0, 100, self)
        prog.setWindowTitle("シーンチェンジ検出")
        prog.canceled.connect(cancel.set)
        prog.show()
        found = []
        for t, c in sel:
            m = self.project.media_by_id(c.media_id)
            if m is None:
                continue
            cuts = AN.scene_cuts(m, c, seq.fps, sens / 10,
                                 lambda i, n: (prog.setValue(int(100 * i / max(1, n))), QApplication.processEvents()),
                                 cancel)
            found += [(t, c, f) for f in cuts]
        prog.close()
        if not found:
            QMessageBox.information(self, "シーンチェンジ検出", "シーンチェンジは見つかりませんでした。")
            return
        box = QMessageBox(self)
        box.setWindowTitle("シーンチェンジ検出")
        box.setText(f"{len(found)} 箇所のシーンチェンジを見つけました。")
        b_mark = box.addButton("マーカーを追加", QMessageBox.ButtonRole.AcceptRole)
        b_split = box.addButton("クリップを分割", QMessageBox.ButtonRole.ActionRole)
        box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        self.begin_edit()
        if box.clickedButton() is b_mark:
            for _t, _c, f in found:
                seq.markers.append(M.Marker(f, "シーン", "#4F8FE0"))
            seq.markers.sort(key=lambda m: m.frame)
            label = "シーンチェンジにマーカー"
        elif box.clickedButton() is b_split:
            for t, c, f in found:
                tracks = [t] + [lt for lt, _ in seq.linked(c)]
                M.razor(seq, f, tracks)
            label = "シーンチェンジで分割"
        else:
            label = None
        self.end_edit(label)

    def paste_attributes(self):
        if not self._clipboard:
            self.status.showMessage("先に属性をコピーするクリップを Ctrl+C でコピーしてください", 3500)
            return
        src = self._clipboard[0][3]
        targets = self.canvas.selected_clips()
        if not targets:
            return
        groups = [("モーション（位置・スケール・回転）", ("pos_x", "pos_y", "scale", "rotation")),
                  ("不透明度・フェード", ("opacity", "fade_in", "fade_out")),
                  ("エフェクト・クロップ・クロマキー", ("effects", "crop", "chroma")),
                  ("アニメーション", ("anim",)), ("速度", ("speed",)),
                  ("音量・パン", ("volume_db", "pan")), ("テロップのスタイル", ("style",)),
                  ("クリップカラー", ("label",))]
        from PyQt6.QtWidgets import QCheckBox
        dlg = QDialog(self)
        dlg.setWindowTitle("属性をペースト")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"「{src.name or src.prop('text', '')}」の属性を {len(targets)} 個のクリップへ"))
        boxes = []
        for lab, keys in groups:
            cb = QCheckBox(lab)
            cb.setChecked(lab != "速度")
            v.addWidget(cb)
            boxes.append((cb, keys))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.begin_edit()
        for _t, c in targets:
            for cb, keys in boxes:
                if not cb.isChecked():
                    continue
                for k in keys:
                    if k in src.props:
                        if k == "style" and c.kind != "text":
                            continue
                        c.props[k] = copy.deepcopy(src.props[k])
                    else:
                        c.props.pop(k, None)
        self.end_edit("属性をペースト")

    def replace_edit(self):
        """置き換え編集: クリップの中身をソース素材で差し替え（ソースとレコードの再生ヘッド位置を合わせる）。"""
        m = self.project.media_by_id(self.source_media_id) if self.source_media_id else None
        if m is None:
            self.status.showMessage("ソースモニターに差し替える素材を開いてください", 3000)
            return
        seq = self.project.sequence
        f = self.record_player.position
        sel = [(t, c) for t, c in self.canvas.selected_clips() if c.kind in ("video", "image", "audio")]
        if not sel:
            tv, _ta = self._target_tracks()
            c = tv.clip_at(f) if tv else None
            sel = [(tv, c)] + [(lt, lc) for lt, lc in seq.linked(c)] if c else []
        if not sel:
            self.status.showMessage("差し替えるクリップを選択してください", 3000)
            return
        anchor = sel[0][1]
        src_pos = self.source_player.position - (f - anchor.start if anchor.start <= f < anchor.end else 0)
        self.begin_edit()
        n = 0
        for t, c in sel:
            if (c.kind == "audio" and not m.has_audio) or (c.kind != "audio" and not (m.has_video or m.kind == "image")):
                continue
            c.media_id = m.id
            c.name = m.name
            c.src_in = max(0, src_pos)
            if c.kind != "audio":
                c.kind = "image" if m.kind == "image" else "video"
            n += 1
        self.end_edit(f"置き換え編集（{n} クリップ）")

    def close_gaps(self):
        """選択トラックすべてで空いている区間を詰める。"""
        seq = self.project.sequence
        tracks = self._selected_tracks()
        end = max((c.end for t in tracks for c in t.clips), default=0)
        busy = [False] * end
        for t in tracks:
            for c in t.clips:
                for i in range(max(0, c.start), min(end, c.end)):
                    busy[i] = True
        gaps = []
        i = 0
        while i < end:
            if not busy[i]:
                j = i
                while j < end and not busy[j]:
                    j += 1
                gaps.append((i, j))
                i = j
            else:
                i += 1
        if not gaps:
            self.status.showMessage("詰める隙間はありません", 2500)
            return
        self.begin_edit()
        for a, b in reversed(gaps):
            M.extract(seq, a, b, tracks)
        self.end_edit(f"隙間を詰める（{len(gaps)} 箇所）")

    def select_forward(self):
        seq = self.project.sequence
        f = self.record_player.position
        tracks = self._selected_tracks()
        self.canvas.selected = {c.id for t in tracks for c in t.clips if c.start >= f}
        self.canvas.selection_changed.emit()
        self.canvas.update()

    def apply_label(self, color: str):
        sel = self.canvas.selected_clips()
        if not sel:
            return
        self.begin_edit()
        for _t, c in sel:
            if color:
                c.props["label"] = color
            else:
                c.props.pop("label", None)
        self.end_edit("クリップカラー")

    def make_subclip(self):
        m = self.project.media_by_id(self.source_media_id) if self.source_media_id else None
        if m is None or m.mark_in < 0 or m.mark_out <= m.mark_in:
            self.status.showMessage("ソースモニターで IN と OUT を付けてください", 3000)
            return
        self.begin_edit()
        sub = copy.deepcopy(m)
        sub.id = M.new_id()
        n = sum(1 for x in self.project.media if x.name.startswith(m.name + ".Sub")) + 1
        sub.name = f"{m.name}.Sub.{n:02d}"
        self.project.media.append(sub)
        b = self.project.bin_by_id(self.bin.current_bin_id()) or self.project.bins[0]
        b.add("media", sub.id)
        self._thumbs[sub.id] = self._thumbs.get(m.id)
        if m.id in self.bin._icons:
            self.bin._icons[sub.id] = self.bin._icons[m.id]
        self.end_edit("サブクリップを作成")

    def find_bin(self):
        sel = self.canvas.selected_clips()
        mid = next((c.media_id for _, c in sel if c.media_id), None)
        if mid is None:
            f = self.record_player.position
            for t in reversed(self.project.sequence.tracks()):
                c = t.clip_at(f)
                if c and c.media_id:
                    mid = c.media_id
                    break
        b = self.project.bin_of(mid) if mid else None
        if b is None:
            self.status.showMessage("ビンに見つかりません", 2500)
            return
        self.dock_bin.show()
        self.dock_bin.raise_()
        self.bin.open_bin(b.id)
        v = self.bin.current_view()
        v.set_view("text")
        for r in range(v.table.rowCount()):
            it = v.table.item(r, 0)
            if it and it.data(Qt.ItemDataRole.UserRole) == ("media", mid):
                v.table.selectRow(r)
                v.table.scrollToItem(it)
                break

    def project_find(self):
        from PyQt6.QtWidgets import QListWidget, QListWidgetItem
        dlg = QDialog(self)
        dlg.setWindowTitle("プロジェクト内を検索")
        dlg.resize(560, 420)
        v = QVBoxLayout(dlg)
        q = QLineEdit()
        q.setPlaceholderText("クリップ名・テロップの文字・マーカー名")
        v.addWidget(q)
        res = QListWidget()
        v.addWidget(res, 1)

        def search():
            res.clear()
            t = q.text().strip().lower()
            if not t:
                return
            for sq in self.project.sequences:
                off = self.tc_offset(sq)
                for tr, c in sq.all_clips():
                    hay = (c.name + " " + c.prop("text", "")).lower()
                    if t in hay:
                        it = QListWidgetItem(f"{sq.name} / {tr.name}  {frames_to_timecode(c.start + off, sq.fps)}  "
                                             f"{(c.prop('text', '') or c.name).replace(chr(10), ' ')[:40]}")
                        it.setData(Qt.ItemDataRole.UserRole, (sq.id, c.id, c.start))
                        res.addItem(it)
                for mk in sq.markers:
                    if t in (mk.name + " " + mk.comment).lower():
                        it = QListWidgetItem(f"{sq.name} / マーカー  {frames_to_timecode(mk.frame + off, sq.fps)}  {mk.name}")
                        it.setData(Qt.ItemDataRole.UserRole, (sq.id, None, mk.frame))
                        res.addItem(it)
            for m in self.project.media:
                if t in m.name.lower():
                    it = QListWidgetItem(f"素材  {m.name}")
                    it.setData(Qt.ItemDataRole.UserRole, ("media", m.id, 0))
                    res.addItem(it)

        def go(it):
            sid, cid, f = it.data(Qt.ItemDataRole.UserRole)
            if sid == "media":
                self.load_source(cid)
                return
            self.load_sequence(sid)
            if cid:
                self.canvas.selected = {cid}
                self.canvas.selection_changed.emit()
            self.seek_record(f)
        q.textChanged.connect(lambda _t: search())
        res.itemDoubleClicked.connect(go)
        dlg.show()
        self._find_dlg = dlg

    # ── パネル・表示 ─────────────────────────
    def mixer_changed(self):
        """ミキサーの変更を、再生を止めずに反映する。"""
        snap = self.record_player._snapshot
        seq = self.project.sequence
        if snap is not None and len(snap.audio_tracks) == len(seq.audio_tracks):
            for a, b in zip(snap.audio_tracks, seq.audio_tracks):
                a.volume_db, a.pan, a.muted, a.solo = b.volume_db, b.pan, b.muted, b.solo
        self._set_dirty()

    def toggle_tc_window(self):
        from .panels import TimecodeWindow
        w = getattr(self, "_tc_win", None)
        if w is not None and w.isVisible():
            w.close()
            return
        self._tc_win = TimecodeWindow(self)
        self._tc_win.show()

    def _update_selection_status(self):
        sel = self.canvas.selected_clips()
        if not sel:
            self.sel_label.setText("")
            return
        seq = self.project.sequence
        a = min(c.start for _, c in sel)
        b = max(c.end for _, c in sel)
        self.sel_label.setText(f"選択 {len(sel)} クリップ  {frames_to_timecode(b - a, seq.fps)}")

    def keyPressEvent(self, e):
        """Avid: テンキーで数字を打つとタイムコード入力（+ / − で相対移動）。"""
        if e.modifiers() & Qt.KeyboardModifier.KeypadModifier and e.text() and e.text() in "0123456789+-":
            self._tc_entry(e.text())
            return
        super().keyPressEvent(e)

    def _tc_entry(self, first: str):
        seq = self.project.sequence
        txt, ok = QInputDialog.getText(self, "タイムコード入力",
                                       "数字で TC（例 1000 = 00:00:10:00）、+ / − で相対（例 +30 = 30 フレーム後ろ）:",
                                       text=first)
        if not ok or not txt.strip():
            return
        t = txt.strip()
        p = self.active_player()
        off = self.tc_offset(seq) if self.active == "record" else 0
        try:
            if t[0] in "+-":
                body = t[1:]
                if body.isdigit() and len(body) <= 2:
                    d = int(body)
                else:
                    d = timecode_to_frames(normalize_timecode(body, seq.fps), seq.fps)
                p.seek(max(0, p.position + (d if t[0] == "+" else -d)))
            else:
                p.seek(max(0, timecode_to_frames(normalize_timecode(t, seq.fps), seq.fps) - off))
        except ValueError:
            self.status.showMessage("タイムコードを読み取れません", 2500)

    # ── ソースモニター ───────────────────────
    def load_source(self, media_id: str, frame: int = 0, activate: bool = True):
        m = self.project.media_by_id(media_id)
        if m is None:
            return
        seq = self.project.sequence
        dur = self.media_frames(m)
        s = M.Sequence(name=m.name, fps=seq.fps,
                       width=m.width or seq.width, height=m.height or seq.height)
        s.video_tracks = [M.Track("video", "V1")]
        s.audio_tracks = [M.Track("audio", "A1")]
        if m.has_video or m.kind == "image":
            s.video_tracks[0].clips.append(M.Clip(M.new_id(), "image" if m.kind == "image" else "video",
                                                  0, dur, 0, m.id, m.name))
        if m.has_audio:
            s.audio_tracks[0].clips.append(M.Clip(M.new_id(), "audio", 0, dur, 0, m.id, m.name))
        self.source_media_id = media_id
        self.source_seq = s
        self.source_mon.set_info(m.name, seq.fps, dur, (m.width or 16) / max(1, m.height or 9))
        self.source_mon.set_marks(m.mark_in, m.mark_out)
        if m.kind == "audio":
            self.source_mon.view.clear("♪ " + m.name)
        self.source_player.refresh()
        self.source_player.seek(frame)
        if activate:
            self.set_active_monitor("source")

    def match_frame(self):
        seq = self.project.sequence
        f = self.record_player.position
        for t in reversed(seq.video_tracks):
            c = t.clip_at(f)
            if c and c.media_id:
                self.load_source(c.media_id, c.src_in + f - c.start)
                return
        for t in seq.audio_tracks:
            c = t.clip_at(f)
            if c and c.media_id:
                self.load_source(c.media_id, c.src_in + f - c.start)
                return

    # ── 編集操作 ─────────────────────────────
    def _target_tracks(self):
        """Avid のパッチ: ソーストラック V1 / A1 がつながっているレコードトラック（切なら None）。"""
        seq = self.project.sequence
        vi = min(self.patch.get("V1", 0), len(seq.video_tracks) - 1)
        ai = min(self.patch.get("A1", 0), len(seq.audio_tracks) - 1)
        tv = seq.video_tracks[vi] if self.src_enabled.get("V1", True) else None
        ta = seq.audio_tracks[ai] if self.src_enabled.get("A1", True) else None
        return tv, ta

    def source_track_keys(self):
        """ソースモニターの素材が持つトラック（パッチボタンを表示する）。"""
        m = self.project.media_by_id(self.source_media_id) if self.source_media_id else None
        if m is None:
            return ["V1", "A1"]
        keys = []
        if m.has_video or m.kind == "image":
            keys.append("V1")
        if m.has_audio:
            keys.append("A1")
        return keys

    def set_patch(self, key: str, idx: int):
        self.patch[key] = idx
        seq = self.project.sequence
        tracks = seq.video_tracks if key.startswith("V") else seq.audio_tracks
        if idx < len(tracks):
            tracks[idx].selected = True      # Avid: パッチ先のレコードトラックは選択される
        self.status.showMessage(f"ソース {key} → {tracks[idx].name} にパッチ", 2500)
        self.canvas.update()

    def toggle_source_track(self, key: str):
        self.src_enabled[key] = not self.src_enabled.get(key, True)
        self.status.showMessage(f"ソース {key}: {'入' if self.src_enabled[key] else '切（このトラックは配置しない）'}", 2500)
        self.canvas.update()

    def _selected_tracks(self):
        seq = self.project.sequence
        return [t for t in seq.tracks() if t.selected and not t.locked] or \
            [t for t in seq.tracks() if not t.locked]

    def _clips_for_media(self, m: M.MediaItem, start: int, src_in: int, dur: int, tv, ta):
        link = M.new_id() if (m.has_video and m.has_audio) else ""
        out = []
        if (m.has_video or m.kind == "image") and tv is not None:
            out.append((tv, M.Clip(M.new_id(), "image" if m.kind == "image" else "video",
                                   start, dur, src_in, m.id, m.name, link)))
        if m.has_audio and ta is not None:
            out.append((ta, M.Clip(M.new_id(), "audio", start, dur, src_in, m.id, m.name, link)))
        return out

    def three_point_edit(self, mode: str):
        """ソースの IN-OUT をシーケンスの IN（なければ再生ヘッド）に挿入/上書き。"""
        m = self.project.media_by_id(self.source_media_id) if self.source_media_id else None
        if m is None:
            self.status.showMessage("ソースモニターに素材を開いてください（ビンの素材をダブルクリック）", 4000)
            return
        seq = self.project.sequence
        total = self.media_frames(m)
        a = m.mark_in if m.mark_in >= 0 else 0
        b = m.mark_out if m.mark_out > a else total
        rec = seq.mark_in if seq.mark_in >= 0 else self.record_player.position
        dur = b - a
        if seq.mark_in >= 0 and seq.mark_out > seq.mark_in:
            dur = seq.mark_out - seq.mark_in
            if m.kind in ("video", "audio"):
                dur = min(dur, total - a)
        tv, ta = self._target_tracks()
        self.begin_edit()
        placements = self._clips_for_media(m, rec, a, dur, tv, ta)
        M.place_clips(seq, placements, mode)
        seq.mark_in = seq.mark_out = -1
        self.end_edit("挿入" if mode == "insert" else "上書き")
        self.canvas.selected = {c.id for _, c in placements}
        self.record_player.seek(rec + dur)
        self.canvas.ensure_visible(rec + dur)

    def place_media_at(self, ids: List[str], row, frame, insert: bool):
        seq = self.project.sequence
        tv, ta = self._target_tracks()
        if row is not None:
            t, kind, idx = row[0], row[1], row[2]
            if kind == "video":
                tv = t
                ta = seq.audio_tracks[idx] if idx < len(seq.audio_tracks) else ta
            else:
                ta = t
                tv = seq.video_tracks[idx] if idx < len(seq.video_tracks) else tv
        f = self.record_player.position if frame is None else frame
        self.begin_edit()
        placed = []
        for mid in ids:
            m = self.project.media_by_id(mid)
            if m is None:
                continue
            a = max(0, m.mark_in)
            total = self.media_frames(m)
            b = m.mark_out if m.mark_out > a else total
            pl = self._clips_for_media(m, f, a, b - a, tv, ta)
            M.place_clips(seq, pl, "insert" if insert else "overwrite")
            placed += pl
            f += b - a
        self.end_edit("クリップを配置")
        self.canvas.selected = {c.id for _, c in placed}
        self.canvas.selection_changed.emit()
        self.canvas.update()

    def place_generator(self, gen: dict, row, frame: int):
        seq = self.project.sequence
        kind = gen.get("kind", "text")
        dur = int(round(seq.fps * (3 if kind == "text" else 5)))
        track = row[0] if row is not None and row[1] == "video" else None
        if kind == "text":
            self.add_telop(frame, track)
            return
        if kind == "adjust":
            self.seek_record(frame)
            self.add_adjustment_layer()
            return
        if kind in ("shape", "null", "camera"):
            self.seek_record(frame)
            {"shape": lambda: self.add_shape_layer(gen.get("shape", "ellipse")), "null": self.add_null,
             "camera": self.add_camera}[kind]()
            return
        if track is None:
            track = self._target_tracks()[0]
        self.begin_edit()
        c = M.Clip(M.new_id(), "color", frame, dur, name="カラーマット",
                   props={"color": gen.get("color", "#000000")})
        M.place_clips(seq, [(track, c)])
        self.end_edit("カラーマットを配置")

    def add_telop(self, frame: Optional[int] = None, track: Optional[M.Track] = None):
        """再生ヘッドにテロップ（テキストクリップ）を追加してインスペクタで編集。"""
        seq = self.project.sequence
        f = self.record_player.position if frame is None else frame
        tmpl = self.default_telop_template() if track is None and hasattr(self, "default_telop_template") else None
        if tmpl is not None:
            # テロップのテンプレート（プロジェクト設定 / ユーザー設定）で置く
            from . import templates as TP
            self.begin_edit()
            made = TP.apply_telop(seq, tmpl, f, {p: "テロップ" for p in TP.placeholders(tmpl)})
            self.end_edit(f"テロップを追加（{tmpl['name']}）")
            txt = [c for c in made if c.kind == "text"]
            self.canvas.selected = {(txt or made)[0].id} if made else set()
            self.canvas.selection_changed.emit()
            self.show_inspector()
            return
        dur = int(round(seq.fps * 3))
        if track is None:
            # 一番上の映像トラックから、空いているトラックを探す
            for t in reversed(seq.video_tracks):
                if not t.locked and t.is_empty(f, f + dur):
                    track = t
                    break
        self.begin_edit()
        if track is None:
            track = M.Track("video", f"V{len(seq.video_tracks) + 1}")
            seq.video_tracks.append(track)
        c = M.Clip(M.new_id(), "text", f, dur, name="テロップ", props={"text": "テロップ", "style": {}})
        M.place_clips(seq, [(track, c)])
        self.end_edit("テロップを追加")
        self.canvas.selected = {c.id}
        self.canvas.selection_changed.emit()
        self.show_inspector()
        self.inspector.text.setFocus()
        self.inspector.text.selectAll()

    def lift_extract(self, extract: bool):
        seq = self.project.sequence
        if seq.mark_in < 0 or seq.mark_out <= seq.mark_in:
            # IN-OUT が無ければ選択クリップを対象に
            if self.canvas.selected:
                (self.ripple_delete_selected if extract else self.delete_selected)()
            else:
                self.status.showMessage("シーケンスに IN/OUT を設定するか、クリップを選択してください", 3000)
            return
        a, b = seq.mark_in, seq.mark_out
        tracks = self._selected_tracks()
        self.begin_edit()
        if extract:
            M.extract(seq, a, b, tracks)
        else:
            M.lift(seq, a, b, tracks)
        seq.mark_in = seq.mark_out = -1
        self.end_edit("抽出" if extract else "リフト")
        self.record_player.seek(a)

    def add_edit(self, all_tracks: bool):
        seq = self.project.sequence
        f = self.record_player.position
        tracks = None
        if not all_tracks:
            sel_tracks = {id(t): t for t, _ in self.canvas.selected_clips()}
            tracks = list(sel_tracks.values()) if sel_tracks else self._selected_tracks()
        self.begin_edit()
        n = M.razor(seq, f, tracks)
        self.end_edit(f"分割（{n} トラック）")

    def ripple_trim_to_playhead(self, which: str):
        seq = self.project.sequence
        f = self.record_player.position
        pts = seq.edit_points()
        if which == "prev":
            cand = [p for p in pts if p < f]
            if not cand:
                return
            a, b = cand[-1], f
        else:
            cand = [p for p in pts if p > f]
            if not cand:
                return
            a, b = f, cand[0]
        self.begin_edit()
        M.extract(seq, a, b)
        self.end_edit("リップルトリム")
        self.record_player.seek(a)

    def delete_selected(self):
        if not self.canvas.selected:
            return
        self.begin_edit()
        M.delete_clips(self.project.sequence, set(self.canvas.selected), ripple=False)
        self.canvas.selected.clear()
        self.end_edit("削除")

    def ripple_delete_selected(self):
        if not self.canvas.selected:
            return
        self.begin_edit()
        M.delete_clips(self.project.sequence, set(self.canvas.selected), ripple=True)
        self.canvas.selected.clear()
        self.end_edit("リップル削除")

    def ripple_delete_gap(self, track, f):
        self.begin_edit()
        ok = M.ripple_delete_gap(self.project.sequence, track, f)
        self.end_edit("隙間を詰める" if ok else None)
        if not ok:
            self.status.showMessage("他のトラックにクリップがあるため詰められません", 3000)

    def select_all(self):
        self.canvas.selected = {c.id for _, c in self.project.sequence.all_clips()}
        self.canvas.selection_changed.emit()
        self.canvas.update()

    def deselect_all(self):
        self.canvas.selected = set()
        self.canvas.selection_changed.emit()
        self.canvas.update()

    def toggle_clips_enabled(self):
        sel = self.canvas.selected_clips()
        if not sel:
            return
        self.begin_edit()
        on = not all(c.enabled for _, c in sel)
        for _, c in sel:
            c.enabled = on
        self.end_edit("有効/無効")

    def toggle_link(self):
        sel = self.canvas.selected_clips()
        if not sel:
            return
        self.begin_edit()
        if any(c.link for _, c in sel):
            for _, c in sel:
                c.link = ""
            label = "リンク解除"
        else:
            lk = M.new_id()
            for _, c in sel:
                c.link = lk
            label = "リンク"
        self.end_edit(label)

    def add_default_fades(self):
        self.apply_effect({"type": "fade", "in": 0.5, "out": 0.5})

    def copy_clips(self):
        sel = self.canvas.selected_clips()
        if not sel:
            return
        seq = self.project.sequence
        base = min(c.start for _, c in sel)
        items = []
        for t, c in sel:
            kind = t.kind
            idx = (seq.video_tracks if kind == "video" else seq.audio_tracks).index(t)
            items.append((kind, idx, c.start - base, copy.deepcopy(c)))
        self._clipboard = items
        self.status.showMessage(f"{len(items)} 個のクリップをコピー", 2000)

    def cut_clips(self):
        self.copy_clips()
        self.delete_selected()

    def paste_clips(self, frame: Optional[int] = None, track=None):
        if not self._clipboard:
            return
        seq = self.project.sequence
        f = self.record_player.position if frame is None else frame
        link_map = {}
        placements = []
        self.begin_edit()
        for kind, idx, off, c in self._clipboard:
            tracks = seq.video_tracks if kind == "video" else seq.audio_tracks
            while idx >= len(tracks):
                tracks.append(M.Track(kind, f"{'V' if kind == 'video' else 'A'}{len(tracks) + 1}"))
            nc = c.copy(id=M.new_id(), start=f + off)
            if c.link:
                nc.link = link_map.setdefault(c.link, M.new_id())
            placements.append((tracks[idx], nc))
        M.place_clips(seq, placements)
        self.end_edit("貼り付け")
        self.canvas.selected = {c.id for _, c in placements}
        self.canvas.selection_changed.emit()
        end = max(c.end for _, c in placements)
        self.record_player.seek(end)

    def apply_effect(self, fx: dict):
        typ = fx.get("type")
        if typ == "gen":
            self.place_generator(fx, None, self.record_player.position)
            return
        if typ in (None, "none"):
            return
        if typ == "dissolve":
            self.add_dissolve(float(fx.get("sec", 1.0)))
            return
        sel = self.canvas.selected_clips()
        if not sel:
            self.status.showMessage("先にタイムラインでクリップを選択してください", 3000)
            return
        seq = self.project.sequence
        fps = seq.fps
        self.begin_edit()
        n = 0
        for _, c in sel:
            visual = c.kind in ("video", "image", "text", "color", "character", "shape")
            if typ == "fade":
                if "in" in fx:
                    c.props["fade_in"] = min(c.duration // 2, int(round(fx["in"] * fps)))
                if "out" in fx:
                    c.props["fade_out"] = min(c.duration // 2, int(round(fx["out"] * fps)))
                n += 1
            elif typ == "effect" and visual:
                eff = list(c.prop("effects") or [])
                e = {"name": fx["name"]}
                if "value" in fx:
                    e["value"] = fx["value"]
                eff.append(e)
                c.props["effects"] = eff
                n += 1
            elif typ == "speed" and c.kind in ("video", "audio"):
                c.props["speed"] = float(fx.get("value", 1.0))
                n += 1
            elif typ == "anim" and visual:
                c.props["anim"] = dict(fx.get("anim", {}))
                n += 1
            elif typ == "plugin" and (visual or c.kind == "adjust"):
                from . import fxlib, nodes as ND
                e = fxlib.get(fx["name"])
                if e is not None:
                    new_fx = {"name": e.id, "params": e.defaults(), "on": True}
                    if c.prop("fx_mode") != "nodes" and not c.prop("effects") and SETTINGS.fx_mode_default == "nodes":
                        c.props["fx_mode"] = "nodes"
                        c.props["fx_graph"] = ND.new_graph()
                    if c.prop("fx_mode") == "nodes":
                        g = __import__("copy").deepcopy(c.prop("fx_graph") or ND.from_stack(c.prop("effects") or []))
                        ND.append_effect(g, new_fx)        # ノード管理: 出力の直前にノードを足す
                        c.props["fx_graph"] = g
                    else:
                        c.props["effects"] = list(c.prop("effects") or []) + [new_fx]
                    n += 1
            elif typ == "effect_clear" and visual:
                c.props["effects"] = []
                n += 1
            elif typ == "motion" and visual:
                for k in ("scale", "pos_x", "pos_y", "rotation"):
                    if k in fx:
                        c.props[k] = fx[k]
                n += 1
            elif typ == "volume" and c.kind == "audio":
                cur = float(c.prop("volume_db", 0.0))
                c.props["volume_db"] = fx["set"] if "set" in fx else cur + fx.get("delta", 0)
                n += 1
            elif typ == "telop_preset" and c.kind == "text":
                try:
                    from core.preset import PresetManager
                    d = PresetManager().load_preset(fx["name"]).to_dict()
                except Exception as ex:
                    QMessageBox.warning(self, "プリセット", str(ex))
                    break
                for k in ("canvas_width", "canvas_height", "frame_rate"):
                    d.pop(k, None)
                c.props["style"] = d
                n += 1
        self.end_edit(f"エフェクトを適用（{n} クリップ）")

    def clip_double_clicked(self, clip: M.Clip):
        if clip.kind == "text":
            self.show_inspector()
            self.inspector.text.setFocus()
            self.inspector.text.selectAll()
        elif clip.media_id:
            self.load_source(clip.media_id, clip.src_in)

    # ── トラック / マーカー ─────────────────
    def add_track(self, kind: str):
        seq = self.project.sequence
        self.begin_edit()
        tracks = seq.video_tracks if kind == "video" else seq.audio_tracks
        tracks.append(M.Track(kind, f"{'V' if kind == 'video' else 'A'}{len(tracks) + 1}"))
        self.end_edit("トラックを追加")

    def delete_track(self, track: M.Track):
        seq = self.project.sequence
        tracks = seq.video_tracks if track.kind == "video" else seq.audio_tracks
        if len(tracks) <= 1:
            return
        if track.clips and QMessageBox.question(
                self, "トラック削除", f"{track.name} には {len(track.clips)} 個のクリップがあります。削除しますか？") \
                != QMessageBox.StandardButton.Yes:
            return
        self.begin_edit()
        tracks.remove(track)
        seq.target_video = min(seq.target_video, len(seq.video_tracks) - 1)
        seq.target_audio = min(seq.target_audio, len(seq.audio_tracks) - 1)
        self.end_edit("トラックを削除")

    def add_marker(self):
        seq = self.project.sequence
        f = self.record_player.position
        self.begin_edit()
        seq.markers = [m for m in seq.markers if m.frame != f]
        seq.markers.append(M.Marker(f, ""))
        seq.markers.sort(key=lambda m: m.frame)
        self.end_edit("マーカーを追加")

    def clear_markers(self):
        self.begin_edit()
        self.project.sequence.markers = []
        self.end_edit("マーカーを削除")

    def sequence_settings(self):
        seq = self.project.sequence
        dlg = SequenceDialog(self, seq)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.begin_edit()
        old_fps = seq.fps
        seq.name = dlg.name.text().strip() or seq.name
        seq.width, seq.height = dlg.res.currentData()
        seq.fps = dlg.fps.currentData()
        try:
            seq.start_tc = normalize_timecode(dlg.start_tc.text(), seq.fps)
        except ValueError:
            pass
        if abs(old_fps - seq.fps) > 1e-6:
            r = seq.fps / old_fps
            for _, c in seq.all_clips():
                c.start = int(round(c.start * r))
                c.duration = max(1, int(round(c.duration * r)))
                c.src_in = int(round(c.src_in * r))
            for m in seq.markers:
                m.frame = int(round(m.frame * r))
        self.end_edit("シーケンス設定")
        self.canvas.zoom_fit()

    # ── 読み込み ─────────────────────────────
    def import_dialog(self):
        exts = " ".join(f"*{e}" for e in sorted(MD.VIDEO_EXT | MD.AUDIO_EXT | MD.IMAGE_EXT))
        paths, _ = QFileDialog.getOpenFileNames(self, "素材を読み込み", self.settings.value("last_dir", ""),
                                                f"メディア ({exts});;すべて (*)")
        if paths:
            self.settings.setValue("last_dir", os.path.dirname(paths[0]))
            self.import_files(paths)

    def named_bin(self, name: str) -> str:
        """名前でビンを探し、無ければ作って ID を返す（AI生成 / 監視フォルダ / お気に入り など）。"""
        self.project.ensure_bins()
        b = next((b for b in self.project.bins if b.name == name), None)
        if b is None:
            b = M.Bin(M.new_id(), name)
            self.project.bins.append(b)
            self.bin.refresh()
            self._set_dirty()
        return b.id

    def import_files(self, paths: List[str], place=None, bin_id: Optional[str] = None, after=None):
        exts = MD.VIDEO_EXT | MD.AUDIO_EXT | MD.IMAGE_EXT
        files = []
        for p in paths:
            if os.path.isdir(p):
                for root, _, fs in os.walk(p):
                    files += [os.path.join(root, f) for f in fs if Path(f).suffix.lower() in exts]
            elif Path(p).suffix.lower() in exts:
                files.append(p)
        if not files:
            return
        w = ImportWorker(files)
        placed_ids: list = []

        target_bin = bin_id or self.bin.current_bin_id()

        def on_item(item, qimg):
            self.project.media.append(item)
            self.project.ensure_bins()
            b = self.project.bin_by_id(target_bin) or self.project.bins[0]
            for bb in self.project.bins:
                bb.remove(item.id)
            b.add("media", item.id)
            if qimg is not None:
                self._thumbs[item.id] = qimg
                self.bin.set_icon(item.id, qimg)
            self.bin.refresh()
            self._set_dirty()
            placed_ids.append(item.id)

        def on_audio(mid):
            self._peaks.pop(mid, None)
            self.canvas.update()

        def on_done():
            self.cache_label.setText("")
            if SETTINGS.auto_proxy_over > 0:
                heavy = [self.project.media_by_id(i) for i in placed_ids]
                heavy = [m for m in heavy if m and m.kind == "video" and (m.width or 0) > SETTINGS.auto_proxy_over]
                if heavy:
                    self.make_proxies(heavy)
            if after is not None:
                after([m for m in (self.project.media_by_id(i) for i in placed_ids) if m])
            if place is not None and placed_ids:
                row, f, ins = place
                self.place_media_at(placed_ids, row, f, ins)
            if w in self._workers:
                self._workers.remove(w)

        w.item_ready.connect(on_item)
        w.audio_ready.connect(on_audio)
        w.item_progress.connect(self.cache_label.setText)
        w.failed.connect(lambda p, e: self.status.showMessage(f"読み込み失敗: {Path(p).name}: {e}", 6000))
        w.finished.connect(on_done)
        self._workers.append(w)
        w.start()

    def remove_media(self, ids: List[str]):
        used = {c.media_id for s in self.project.sequences for _, c in s.all_clips()}
        if set(ids) & used and QMessageBox.question(
                self, "素材の削除", "シーケンスで使われている素材があります。クリップも削除しますか？") \
                != QMessageBox.StandardButton.Yes:
            return
        self.begin_edit()
        for s in self.project.sequences:
            for t in s.tracks():
                t.clips = [c for c in t.clips if c.media_id not in ids]
        self.project.media = [m for m in self.project.media if m.id not in ids]
        for b in self.project.bins:
            for i in ids:
                b.remove(i)
        if self.source_media_id in ids:
            self.source_media_id = None
            self.source_seq = None
            self.source_player.refresh()
            self.source_mon.view.clear("ビンの素材をダブルクリックで表示")
        self.end_edit("素材を削除")
        self.bin.refresh()

    def import_telops(self):
        """DavinTelop のプロジェクト / SRT / CSV からテロップクリップを作る。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "テロップを読み込み", self.settings.value("last_dir", ""),
            "テロップ (*.dtproj *.srt *.csv);;DavinTelop プロジェクト (*.dtproj);;SRT (*.srt);;CSV (*.csv)")
        if not path:
            return
        seq = self.project.sequence
        from core.models import TelopEntry
        entries = []
        design = None
        try:
            ext = Path(path).suffix.lower()
            if ext == ".dtproj":
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                entries = [TelopEntry.from_dict(d) for d in data.get("telops", [])]
                design = data.get("design")
            elif ext == ".srt":
                from core.exporter import import_srt
                entries = import_srt(path, fps=seq.fps)
            else:
                from core.exporter import import_csv
                for enc in ("utf-8-sig", "cp932"):
                    try:
                        entries = import_csv(path, encoding=enc)
                        break
                    except UnicodeDecodeError:
                        continue
        except Exception as ex:
            QMessageBox.critical(self, "テロップの読み込み", str(ex))
            return
        entries = [e for e in entries if e.enabled and e.text.strip()]
        if not entries:
            QMessageBox.information(self, "テロップの読み込み", "テロップが見つかりませんでした。")
            return
        off = self.tc_offset(seq)
        # 全テロップの TC がシーケンス開始より前ならオフセットなしで配置
        try:
            first = min(timecode_to_frames(e.start_time, seq.fps) for e in entries)
        except ValueError:
            first = 0
        if first < off:
            off = 0
        self.begin_edit()
        if design:
            if QMessageBox.question(self, "テロップデザイン",
                                    "DavinTelop プロジェクトのデザインを共通テロップデザインに使いますか？") \
                    == QMessageBox.StandardButton.Yes:
                self.project.design = design
        track = M.Track("video", f"V{len(seq.video_tracks) + 1}")
        seq.video_tracks.append(track)
        n = 0
        for e in entries:
            try:
                a = timecode_to_frames(e.start_time, seq.fps) - off
                b = timecode_to_frames(e.end_time, seq.fps) - off
            except ValueError:
                continue
            if b <= a or a < 0:
                continue
            props = {"text": e.text, "style": dict(getattr(e, "style", {}) or {})}
            c = M.Clip(M.new_id(), "text", a, b - a, name="テロップ", props=props)
            M.clear_range(track, a, b)
            track.clips.append(c)
            track.sort()
            n += 1
        self.end_edit(f"テロップを読み込み（{n} 件）")
        self.canvas.zoom_fit()

    def set_design_from_preset(self):
        try:
            from core.preset import PresetManager
            pm = PresetManager()
            names = pm.list_presets()
        except Exception:
            names = []
        if not names:
            QMessageBox.information(self, "テロップデザイン", "DavinTelop のプリセットがありません。")
            return
        name, ok = QInputDialog.getItem(self, "テロップ共通デザイン", "プリセット:", names, 0, False)
        if not ok:
            return
        self.begin_edit()
        self.project.design = pm.load_preset(name).to_dict()
        self.end_edit("テロップ共通デザインを変更")

    def export_srt(self):
        seq = self.project.sequence
        texts = [c for t in seq.video_tracks for c in t.clips if c.kind == "text" and c.enabled]
        if not texts:
            QMessageBox.information(self, "SRT", "テロップ（テキストクリップ）がありません。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "SRT を保存", f"{seq.name}.srt", "SRT (*.srt)")
        if not path:
            return
        from core.models import TelopEntry
        from core.exporter import export_srt
        off = self.tc_offset(seq)
        entries = [TelopEntry(i + 1, frames_to_timecode(c.start + off, seq.fps),
                              frames_to_timecode(c.end + off, seq.fps), c.prop("text", ""))
                   for i, c in enumerate(sorted(texts, key=lambda c: c.start))]
        export_srt(entries, path, seq.fps)
        self.status.showMessage(f"SRT を書き出しました: {path}", 4000)

    def export_media(self):
        if self.project.sequence.duration <= 0:
            QMessageBox.information(self, "書き出し", "シーケンスが空です。")
            return
        self.record_player.stop()
        ExportDialog(self).exec()

    # ── プロジェクト ─────────────────────────
    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        r = QMessageBox.question(self, APP_NAME, "変更を保存しますか？",
                                 QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
                                 QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Cancel:
            return False
        if r == QMessageBox.StandardButton.Save:
            return self.save_project()
        return True

    def new_project(self):
        if not self._confirm_discard():
            return
        self.lock.release()
        self.read_only = False
        self._load_project(M.Project(sequences=[M.Sequence.create()]), None)

    def _load_project(self, proj: M.Project, path: Optional[str]):
        self.exit_trim()
        self.cancel_render()
        self.record_player.stop()
        self.source_player.stop()
        self.project = proj
        if not proj.design:
            self._load_default_design()
        self.project_path = path
        self._undo.clear()
        self._redo.clear()
        self.canvas.selected = set()
        self.source_media_id = None
        self.source_seq = None
        self.source_player.refresh()
        self.source_mon.view.clear("ビンの素材をダブルクリックで表示")
        self.dirty = False
        self._thumbs.clear()
        self._peaks.clear()
        MD._KEY_MEMO.clear()
        proj.ensure_bins()
        self.bin.reset()
        self.refresh_all()
        self.record_player.seek(proj.sequence.playhead)
        self.canvas.zoom_fit()
        self._update_title()
        self._rebuild_caches([m for m in proj.media if os.path.exists(m.path)])

    def _rebuild_caches(self, items):
        if not items:
            return
        from PyQt6.QtCore import QThread, pyqtSignal

        class W(QThread):
            thumb = pyqtSignal(str, object)
            audio = pyqtSignal(str)

            def run(self_):
                for m in items:
                    th = MD.thumbnail(m)
                    if th is not None:
                        data = th.tobytes("raw", "RGB")
                        q = QImage(data, th.width, th.height, th.width * 3, QImage.Format.Format_RGB888).copy()
                        self_.thumb.emit(m.id, q)
                    if m.has_audio and MD.build_audio_cache(m.path):
                        MD.build_peaks(m.path)
                        self_.audio.emit(m.id)

        w = W()
        w.thumb.connect(lambda mid, q: (self._thumbs.__setitem__(mid, q), self.bin.set_icon(mid, q),
                                        self.bin.refresh(), self.canvas.update()))
        w.audio.connect(lambda mid: (self._peaks.pop(mid, None), self.canvas.update(),
                                     self.record_player.refresh()))
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def open_project(self, path: Optional[str] = None):
        if not self._confirm_discard():
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "プロジェクトを開く",
                                                  self.settings.value("last_dir", ""), PROJECT_FILTER)
        if not path:
            return
        # 共有プロジェクトのロック確認
        read_only = False
        info = ProjectLock.read(path)
        if info and not ProjectLock.is_ours(info):
            if ProjectLock.is_stale(info):
                QMessageBox.information(self, "プロジェクトのロック",
                                        f"古いロックが残っています: {describe_lock(info)}\nロックを引き継いで開きます。")
            else:
                box = QMessageBox(self)
                box.setWindowTitle("使用中のプロジェクト")
                box.setText(f"このプロジェクトは {describe_lock(info)} が開いています。")
                b_ro = box.addButton("読み取り専用で開く", QMessageBox.ButtonRole.AcceptRole)
                b_force = box.addButton("ロックを解除して開く", QMessageBox.ButtonRole.DestructiveRole)
                box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
                box.exec()
                if box.clickedButton() is b_ro:
                    read_only = True
                elif box.clickedButton() is not b_force:
                    return
        try:
            proj = M.Project.from_json(Path(path).read_text(encoding="utf-8"))
        except Exception as ex:
            QMessageBox.critical(self, "開く", f"読み込みに失敗しました:\n{ex}")
            return
        offline = resolve_media(proj, path)
        if offline:
            RelinkDialog(self, proj, offline).exec()
        self.lock.release()
        self.read_only = read_only
        if not read_only:
            self.lock.acquire(path)
        self._load_project(proj, path)
        self.settings.setValue("last_dir", os.path.dirname(path))
        self._add_recent(path)

    def save_project(self, quiet: bool = False) -> bool:
        if hasattr(self, "_flush_work_time") and self.project_path and not self.read_only:
            self._flush_work_time()
        if not self.project_path or self.read_only:
            if quiet:
                return False
            if self.read_only:
                QMessageBox.information(self, "読み取り専用",
                                        "読み取り専用で開いています。別の名前で保存してください。")
            return self.save_project_as()
        info = ProjectLock.read(self.project_path)
        if info and not ProjectLock.is_ours(info) and not ProjectLock.is_stale(info):
            if quiet:
                return False
            QMessageBox.warning(self, "保存できません",
                                f"{describe_lock(info)} がこのプロジェクトを開いています。別の名前で保存してください。")
            return self.save_project_as()
        try:
            self.project.sequence.playhead = self.record_player.position
            update_rel_paths(self.project, self.project_path)
            text = self.project.to_json()
            atomic_write(self.project_path, text)
            attic_backup(self.project_path, text)
            self.lock.acquire(self.project_path)
        except Exception as ex:
            if not quiet:
                QMessageBox.critical(self, "保存", f"保存に失敗しました:\n{ex}\n\nNAS の接続を確認してください。"
                                                 "（Attic にバックアップを残しました）")
            attic_backup(self.project_path, self.project.to_json())
            return False
        self.dirty = False
        self._update_title()
        if hasattr(self, "after_save"):
            self.after_save()
        if not quiet:
            self._add_recent(self.project_path)
            self.status.showMessage(f"保存しました: {self.project_path}", 3000)
        return True

    def save_project_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, "名前を付けて保存",
                                              self.project_path or "project" + PROJECT_EXT, PROJECT_FILTER)
        if not path:
            return False
        if not path.lower().endswith((PROJECT_EXT, ".dvep")):
            path += PROJECT_EXT
        info = ProjectLock.read(path)
        if info and not ProjectLock.is_ours(info) and not ProjectLock.is_stale(info):
            QMessageBox.warning(self, "保存できません", f"{describe_lock(info)} がこのファイルを開いています。")
            return False
        self.lock.release()
        self.project_path = path
        self.read_only = False
        return self.save_project()

    def _add_recent(self, path):
        lst = [p for p in (self.settings.value("recent", []) or []) if p != path]
        lst.insert(0, path)
        self.settings.setValue("recent", lst[:10])
        self._rebuild_recent()

    def _rebuild_recent(self):
        self.recent_menu.clear()
        lst = [p for p in (self.settings.value("recent", []) or []) if os.path.exists(p)]
        if not lst:
            a = self.recent_menu.addAction("（なし）")
            a.setEnabled(False)
        for p in lst:
            self.recent_menu.addAction(Path(p).name, lambda p=p: self.open_project(p))

    def reset_workspace(self):
        self.workspace_single()

    def show_shortcuts(self):
        """現在のキー割り当てから一覧を作る。"""
        rows = []
        for cat in CATEGORIES:
            items = [(info[1], self.keymap.get(cid, [])) for cid, info in COMMAND_INFO.items()
                     if info[2] == cat and self.keymap.get(cid)]
            if not items:
                continue
            rows.append(f"<h3>{cat}</h3><table cellpadding=3>")
            for name, keys in items:
                ks = " / ".join(QKeySequence(k).toString(QKeySequence.SequenceFormat.NativeText) for k in keys)
                rows.append(f"<tr><td width=170><b>{ks}</b></td><td>{name}</td></tr>")
            rows.append("</table>")
        preset = PRESETS.get(SETTINGS.keyboard_preset, "カスタム")
        html = (f"<p>現在の配列: <b>{preset}</b>（ファイル → キーボード設定 で変更）<br>"
                "モニターやタイムラインをクリックすると、そのパネルが操作対象（紫の枠）になります。</p>"
                + "".join(rows))
        dlg = QDialog(self)
        dlg.setWindowTitle("キーボードショートカット")
        dlg.resize(600, 700)
        lay = QVBoxLayout(dlg)
        tb = QTextBrowser()
        tb.setHtml(html)
        lay.addWidget(tb)
        dlg.exec()

    def closeEvent(self, e):
        if not self._confirm_discard():
            e.ignore()
            return
        self.toggle_fullscreen(False)
        self.cancel_render()
        self.lock.release()
        if getattr(self, "bridge", None) is not None:
            self.bridge.stop()
        from .voiceengine import stop_launched
        stop_launched()      # TeloCut がエンジンだけ起動した音声合成ソフトを終了
        if getattr(self, "_node_stop", None) is not None:
            self._node_stop.set()
        if getattr(self, "_asset_httpd", None) is not None:
            self._asset_httpd.shutdown()
        if self.dual_mode:
            # デュアルの配置は別に保存し、1 画面用の配置は入る前のものを残す
            self.close_multi_windows()
            self.settings.setValue("window_state", self._single_state or self._default_state)
        else:
            self.settings.setValue("window_state", self.saveState())
        self.settings.setValue("window_geometry", self.saveGeometry())
        self.settings.setValue("layout_version", LAYOUT_VERSION)
        for w in list(self._workers):
            try:
                if hasattr(w, "cancel"):
                    w.cancel.set()
                w.wait(2000)
            except Exception:
                pass
        if self.renderer is not None:
            self.renderer.wait(3000)
        self.source_player.shutdown()
        self.record_player.shutdown()
        CACHE.prune()
        super().closeEvent(e)
