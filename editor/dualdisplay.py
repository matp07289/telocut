"""マルチディスプレイ（2 画面・3 画面以上）用のレイアウト（推奨デザイン）と、画面の接続 / 切断に合わせた自動切り替え。

画面ごとに「役割」を割り当てる:
  edit   編集画面   … コンポーザー（ソース / レコード）を大きく、タイムラインを下に広く、右にエフェクトエディター
  panel  パネル画面 … 左: プロジェクト / ビン（アセット・AI 画像）　中央: テロップ一覧（マーカー・履歴）
                      （仕上げ画面が無いときは右に プリセット / SE / AI アシスト と スコープ / ミキサー も）
  finish 仕上げ画面 … プリセット / SE パッド / AI アシスト と、ビデオスコープ・オーディオミキサーを大きく
  client クライアントモニター … レコード（完成映像）を全画面で表示。テロップの読みやすさを実寸で確認できる
  none   使わない

推奨の割り当て（メインディスプレイ = 編集画面。ほかは編集画面に近い順に）
  2 画面: 編集 + パネル
  3 画面: 編集 + パネル + クライアントモニター
  4 画面: 編集 + パネル + 仕上げ + クライアントモニター（5 枚目以降は使わない）
ウィンドウ → マルチディスプレイ → 画面の割り当て で自由に変えられる（画面の組み合わせごとに保存）。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PyQt6.QtCore import Qt, QTimer, QRect
from PyQt6.QtGui import QGuiApplication, QScreen
from PyQt6.QtWidgets import (QMainWindow, QMessageBox, QDialog, QFormLayout, QComboBox, QLabel,
                             QDialogButtonBox, QPushButton)

from . import display as DSP
from .display import FullscreenOutput
from .settings import SETTINGS

ROLES = [("edit", "編集画面（モニター・タイムライン）"), ("panel", "パネル画面（素材・テロップ）"),
         ("finish", "仕上げ画面（プリセット・スコープ・ミキサー）"), ("client", "クライアントモニター（完成映像を全画面）"),
         ("none", "使わない")]
ROLE_NAMES = dict(ROLES)


def recommended_roles(n_extra: int) -> List[str]:
    """編集画面以外の画面（編集画面に近い順）に割り当てる役割。"""
    if n_extra <= 0:
        return []
    if n_extra == 1:
        return ["panel"]
    if n_extra == 2:
        return ["panel", "client"]
    return ["panel", "finish", "client"] + ["none"] * (n_extra - 3)


def _center(s):
    g = s.geometry()
    return g.x() + g.width() / 2, g.y() + g.height() / 2


class PanelWindow(QMainWindow):
    """2 画面目以降に置くパネル用ウィンドウ（ドックを並べるだけで中央ウィジェットは持たない）。"""

    def __init__(self, mw, name: str, title: str):
        super().__init__(None)
        self.mw = mw
        self.setObjectName(name)
        self.setWindowTitle(title)
        self.setWindowIcon(mw.windowIcon())
        self.setDockNestingEnabled(True)
        self.setDockOptions(QMainWindow.DockOption.AnimatedDocks | QMainWindow.DockOption.AllowNestedDocks |
                            QMainWindow.DockOption.AllowTabbedDocks)
        self.setStyleSheet(mw.styleSheet())
        for a in mw.actions.values():          # どの画面にいてもショートカットが効くように
            self.addAction(a)
        self._closing_by_app = False

    def close_by_app(self):
        self._closing_by_app = True
        self.close()
        self._closing_by_app = False

    def closeEvent(self, e):
        if self._closing_by_app:
            e.accept()
            return
        e.ignore()                             # ユーザーが閉じた → 1 画面レイアウトに戻す
        QTimer.singleShot(0, lambda: self.mw.leave_dual())


class ClientMonitor(FullscreenOutput):
    """クライアントモニター（レコードの映像を全画面表示し続ける）。Esc / ダブルクリックでは閉じない。"""

    def __init__(self, mw):
        super().__init__(mw)
        self.setWindowTitle("TeloCut — クライアントモニター")
        for a in mw.actions.values():
            self.addAction(a)

    def place(self, screen):
        g = screen.geometry()
        if isinstance(screen, QScreen):
            self.show_on(screen)
        else:
            self.setGeometry(g)
            self.show()

    def keyPressEvent(self, e):
        self.mw.keyPressEvent(e)

    def mouseDoubleClickEvent(self, _e):
        pass


class DualDisplayMixin:
    _MATERIAL_DOCKS = ("dock_project", "dock_bin", "dock_assets", "dock_comfy", "dock_telop_ed", "dock_telops",
                       "dock_markers", "dock_history", "dock_voice", "dock_updates", "dock_prod")
    _FINISH_DOCKS = ("dock_presets", "dock_se", "dock_assist", "dock_scopes", "dock_mixer", "dock_motion", "dock_farm")

    def _init_dual(self):
        self.panel_win: Optional[PanelWindow] = None
        self.finish_win: Optional[PanelWindow] = None
        self.client_win: Optional[ClientMonitor] = None
        self.dual_mode = False
        self.dual_roles: List[Tuple[object, str]] = []
        self._single_state = None
        app = QGuiApplication.instance()
        app.screenAdded.connect(lambda _s: QTimer.singleShot(1000, self._screens_changed))
        app.screenRemoved.connect(lambda _s: QTimer.singleShot(1000, self._screens_changed))

    def _docks_named(self, names):
        return [getattr(self, n) for n in names if hasattr(self, n)]

    def _panel_docks(self):
        return self._docks_named(self._MATERIAL_DOCKS + self._FINISH_DOCKS)

    # ── 役割の割り当て ─────────────────────
    def assign_roles(self, screens=None) -> List[Tuple[object, str]]:
        """[(画面, 役割)]。保存した割り当てが今の画面の組み合わせと一致すればそれを使い、無ければ推奨。"""
        scr = list(screens) if screens is not None else DSP.screens_sorted()
        if len(scr) < 2:
            return [(s, "edit") for s in scr]
        saved = SETTINGS.screen_roles.get(self._screen_key(scr))
        if saved and len(saved) == len(scr) and list(saved).count("edit") == 1:
            return list(zip(scr, saved))
        prim = QGuiApplication.primaryScreen()
        edit = prim if prim in scr else scr[0]
        ex, ey = _center(edit)
        others = sorted((s for s in scr if s is not edit),
                        key=lambda s: (_center(s)[0] - ex) ** 2 + (_center(s)[1] - ey) ** 2)
        if SETTINGS.dual_swap and others:      # 編集画面とパネル画面を入れ替え
            edit, others[0] = others[0], edit
        roles = {id(edit): "edit"}
        for s, r in zip(others, recommended_roles(len(others))):
            roles[id(s)] = r
        return [(s, roles[id(s)]) for s in scr]

    @staticmethod
    def _screen_key(scr) -> str:
        return "|".join(f"{s.name()}@{s.geometry().x()},{s.geometry().y()}" for s in scr)

    def _layout_key(self) -> str:
        return "multi/" + "-".join(r for _, r in self.dual_roles)

    def _screens_changed(self):
        scr = DSP.screens_sorted()
        if len(scr) >= 2 and SETTINGS.auto_dual:
            if self.dual_mode and [s for s, _ in self.dual_roles] == scr:
                return
            self.enter_dual()
            self.status.showMessage(f"ディスプレイ {len(scr)} 台用のレイアウトにしました", 6000)
        elif len(scr) < 2 and self.dual_mode:
            self.leave_dual()
            self.status.showMessage("ディスプレイが 1 台になったので、1 画面のレイアウトに戻しました", 6000)

    def auto_dual_at_start(self):
        if SETTINGS.auto_dual and len(DSP.screens_sorted()) >= 2 and not self.dual_mode:
            self.enter_dual()

    # ── 入る / 出る ─────────────────────────
    def enter_dual(self, recommended: bool = False, screens=None, roles=None):
        """マルチディスプレイ用レイアウトにする。recommended=True なら保存した配置を使わず推奨配置に。
        screens: 画面のリスト（テスト用）、roles: [(画面, 役割)] を直接指定。"""
        assign = roles or self.assign_roles(screens)
        if len(assign) < 2 or sum(1 for _, r in assign if r != "none") < 2:
            QMessageBox.information(self, "マルチディスプレイ",
                                    "画面が 1 つしか見つかりません（または使う画面が 1 つだけです）。"
                                    "2 台目のディスプレイを接続してから選んでください。")
            return False
        if self.dual_mode:
            self._teardown_multi(save=True)
        else:
            self.toggle_composer_window(False)
            self.toggle_fullscreen(False)
            self._single_state = self.saveState()
        self.dual_roles = assign
        self.dual_mode = True
        self._place_multi(recommended)
        if hasattr(self, "dual_act"):
            self.dual_act.setChecked(True)
        return True

    def _place_multi(self, recommended: bool):
        roles = {r: s for s, r in self.dual_roles if r != "none"}
        edit_scr = roles.get("edit") or self.dual_roles[0][0]
        L, R, B = (Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea,
                   Qt.DockWidgetArea.BottomDockWidgetArea)
        for d in self._panel_docks():
            d.setFloating(False)
            self.removeDockWidget(d)
        has_panel, has_finish = "panel" in roles, "finish" in roles

        # ── パネル画面: 素材 | テロップ（| 仕上げ）──
        if has_panel:
            pw = self.panel_win = self.panel_win or PanelWindow(self, "telocut_panel_window",
                                                                "TeloCut — パネル（素材・テロップ）")
            pw.addDockWidget(L, self.dock_project)
            pw.splitDockWidget(self.dock_project, self.dock_telops, Qt.Orientation.Horizontal)
            pw.splitDockWidget(self.dock_project, self.dock_bin, Qt.Orientation.Vertical)
            for d in (self.dock_assets, self.dock_comfy, self.dock_voice):
                pw.tabifyDockWidget(self.dock_bin, d)
            for d in (self.dock_telop_ed, self.dock_markers, self.dock_history):
                pw.tabifyDockWidget(self.dock_telops, d)
            if not has_finish:
                pw.splitDockWidget(self.dock_telops, self.dock_presets, Qt.Orientation.Horizontal)
                pw.splitDockWidget(self.dock_presets, self.dock_scopes, Qt.Orientation.Vertical)
                for d in (self.dock_se, self.dock_assist):
                    pw.tabifyDockWidget(self.dock_presets, d)
                pw.tabifyDockWidget(self.dock_scopes, self.dock_mixer)
        # ── 仕上げ画面: プリセット・SE・AI | スコープ / ミキサー ──
        if has_finish:
            fw = self.finish_win = self.finish_win or PanelWindow(self, "telocut_finish_window",
                                                                  "TeloCut — 仕上げ（プリセット・スコープ・ミキサー）")
            fw.addDockWidget(L, self.dock_presets)
            fw.splitDockWidget(self.dock_presets, self.dock_scopes, Qt.Orientation.Horizontal)
            fw.splitDockWidget(self.dock_scopes, self.dock_mixer, Qt.Orientation.Vertical)
            for d in (self.dock_se, self.dock_assist):
                fw.tabifyDockWidget(self.dock_presets, d)
        # 置き場所の無いパネルは編集画面の右にタブで
        homeless = []
        if not has_panel:
            homeless += self._docks_named(self._MATERIAL_DOCKS)
        if not has_panel and not has_finish:
            homeless += self._docks_named(self._FINISH_DOCKS)
        self.addDockWidget(R, self.dock_insp)
        self.addDockWidget(B, self.dock_tl)
        for d in homeless:
            self.addDockWidget(R, d)
            self.tabifyDockWidget(self.dock_insp, d)
        for d in self._panel_docks() + [self.dock_insp, self.dock_tl]:
            d.show()
        for d in (self.dock_bin, self.dock_telop_ed, self.dock_presets, self.dock_scopes, self.dock_insp):
            d.raise_()

        # ── ウィンドウを各画面へ ──
        def put(win, scr):
            win.showNormal()
            win.setGeometry(scr.availableGeometry())
            win.show()
            win.showMaximized()
        put(self, edit_scr)
        if has_panel:
            put(self.panel_win, roles["panel"])
        if has_finish:
            put(self.finish_win, roles["finish"])
        if "client" in roles:
            self.client_win = self.client_win or ClientMonitor(self)
            seq = self.project.sequence
            self.client_win.aspect = seq.width / max(1, seq.height)
            if self.record_mon.view._img is not None:
                self.client_win.set_image(self.record_mon.view._img)
            try:
                self.record_player.image_changed.disconnect(self.client_win.set_image)
            except TypeError:
                pass
            self.record_player.image_changed.connect(self.client_win.set_image)
            self.client_win.place(roles["client"])

        key = self._layout_key()
        saved = {n: self.settings.value(f"{key}/{n}") for n in ("main", "panel", "finish")}
        if not recommended and saved["main"] is not None:
            if has_panel and saved["panel"] is not None:
                self.panel_win.restoreState(saved["panel"])
            if has_finish and saved["finish"] is not None:
                self.finish_win.restoreState(saved["finish"])
            self.restoreState(saved["main"])
        else:
            QTimer.singleShot(0, self._multi_sizes)
            QTimer.singleShot(300, self._multi_sizes)
        desc = "、".join(f"{i + 1}: {ROLE_NAMES[r].split('（')[0]}" for i, (_, r) in enumerate(self.dual_roles))
        self.status.showMessage(f"マルチディスプレイ（{desc}）", 6000)

    def _multi_sizes(self, sizes=None):
        """推奨のパネルの大きさ。sizes: {役割: QRect}（テスト用。省略時は各画面の大きさ）。"""
        if not self.dual_mode:
            return
        geo = sizes or {r: s.availableGeometry() for s, r in self.dual_roles}
        H, V = Qt.Orientation.Horizontal, Qt.Orientation.Vertical
        has_finish = "finish" in geo
        if "panel" in geo and self.panel_win is not None:
            g, pw = geo["panel"], self.panel_win
            if has_finish:
                pw.resizeDocks([self.dock_project, self.dock_telops], [int(g.width() * 0.38), int(g.width() * 0.62)], H)
            else:
                pw.resizeDocks([self.dock_project, self.dock_telops, self.dock_presets],
                               [int(g.width() * 0.28), int(g.width() * 0.42), int(g.width() * 0.30)], H)
                pw.resizeDocks([self.dock_presets, self.dock_scopes], [int(g.height() * 0.58), int(g.height() * 0.42)], V)
            pw.resizeDocks([self.dock_project, self.dock_bin], [int(g.height() * 0.22), int(g.height() * 0.78)], V)
        if has_finish and self.finish_win is not None:
            g, fw = geo["finish"], self.finish_win
            fw.resizeDocks([self.dock_presets, self.dock_scopes], [int(g.width() * 0.4), int(g.width() * 0.6)], H)
            fw.resizeDocks([self.dock_scopes, self.dock_mixer], [int(g.height() * 0.6), int(g.height() * 0.4)], V)
        e = geo.get("edit")
        if e is not None:
            self.resizeDocks([self.dock_insp], [int(e.width() * 0.2)], H)
            self.resizeDocks([self.dock_tl], [int(e.height() * 0.45)], V)

    def save_dual_state(self):
        if not self.dual_mode:
            return
        key = self._layout_key()
        self.settings.setValue(f"{key}/main", self.saveState())
        for n, w in (("panel", self.panel_win), ("finish", self.finish_win)):
            if w is not None and w.isVisible():
                self.settings.setValue(f"{key}/{n}", w.saveState())

    def _teardown_multi(self, save: bool):
        """パネルをすべて編集画面のウィンドウに戻し、サブのウィンドウを閉じる。"""
        if save:
            self.save_dual_state()
        R = Qt.DockWidgetArea.RightDockWidgetArea
        for w in (self.panel_win, self.finish_win):
            if w is not None:
                for d in self._panel_docks():
                    w.removeDockWidget(d)
        for d in self._panel_docks():
            self.addDockWidget(R, d)
        for w in (self.panel_win, self.finish_win):
            if w is not None:
                w.close_by_app()
        if self.client_win is not None:
            try:
                self.record_player.image_changed.disconnect(self.client_win.set_image)
            except TypeError:
                pass
            self.client_win.hide()

    def leave_dual(self):
        """1 画面のレイアウトに戻す（マルチの配置は保存しておき、次回復元）。"""
        if not self.dual_mode:
            return
        self._teardown_multi(save=True)
        self.dual_mode = False
        self.dual_roles = []
        self.restoreState(self._single_state or self._default_state)
        for d in self._panel_docks():
            d.setFloating(False)
        DSP.ensure_docks_on_screen(self, self.docks)
        self.showNormal()
        self.setGeometry((self.screen() or QGuiApplication.primaryScreen()).availableGeometry())
        self.showMaximized()
        if hasattr(self, "dual_act"):
            self.dual_act.setChecked(False)

    def close_multi_windows(self):
        """アプリ終了時: 配置を保存してサブのウィンドウを閉じる。"""
        if self.dual_mode:
            self.save_dual_state()
        for w in (self.panel_win, self.finish_win):
            if w is not None:
                w.close_by_app()
        if self.client_win is not None:
            self.client_win.close()

    # ── メニュー用 ─────────────────────────
    def toggle_dual(self):
        if self.dual_mode:
            self.leave_dual()
        else:
            self.enter_dual()

    def swap_dual_screens(self):
        SETTINGS.dual_swap = not SETTINGS.dual_swap
        scr = DSP.screens_sorted()
        SETTINGS.screen_roles.pop(self._screen_key(scr), None)     # 推奨の割り当てでやり直す
        SETTINGS.save()
        if self.dual_mode:
            self.enter_dual(recommended=True)

    def set_auto_dual(self, on: bool):
        SETTINGS.auto_dual = bool(on)
        SETTINGS.save()

    def screen_roles_dialog(self, screens=None):
        """画面ごとの役割を選ぶ。"""
        scr = list(screens) if screens is not None else DSP.screens_sorted()
        if len(scr) < 2:
            QMessageBox.information(self, "画面の割り当て", "ディスプレイが 1 台だけです。")
            return
        cur = dict((id(s), r) for s, r in (self.dual_roles if self.dual_mode else self.assign_roles(scr)))
        dlg = QDialog(self)
        dlg.setWindowTitle("画面の割り当て（マルチディスプレイ）")
        f = QFormLayout(dlg)
        f.addRow(QLabel("画面ごとに役割を選びます（左から順）。編集画面は 1 つだけ選んでください。"))
        combos = []
        for i, s in enumerate(scr):
            g = s.geometry()
            cb = QComboBox()
            for k, lab in ROLES:
                cb.addItem(lab, k)
            cb.setCurrentIndex(max(0, cb.findData(cur.get(id(s), "none"))))
            combos.append(cb)
            prim = "（メイン）" if s is QGuiApplication.primaryScreen() else ""
            f.addRow(f"画面 {i + 1}{prim}: {s.name()} {g.width()}×{g.height()}", cb)
        rec = QPushButton("推奨の割り当てに戻す")

        def reset():
            SETTINGS.screen_roles.pop(self._screen_key(scr), None)
            for cb, (_, r) in zip(combos, self.assign_roles(scr)):
                cb.setCurrentIndex(cb.findData(r))
        rec.clicked.connect(reset)
        f.addRow(rec)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if not dlg.exec():
            return
        roles = [cb.currentData() for cb in combos]
        if roles.count("edit") != 1:
            QMessageBox.warning(self, "画面の割り当て", "編集画面はちょうど 1 つ選んでください。")
            return
        if len(set(r for r in roles if r not in ("none",))) != len([r for r in roles if r != "none"]):
            QMessageBox.warning(self, "画面の割り当て", "同じ役割（使わない以外）は 1 つの画面にだけ割り当ててください。")
            return
        SETTINGS.screen_roles[self._screen_key(scr)] = roles
        SETTINGS.save()
        self.enter_dual(recommended=True, roles=list(zip(scr, roles)))
