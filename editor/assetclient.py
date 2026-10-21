"""TeloCut Assets — アセットサーバーにつなぐだけの独立したアプリ（TeloCutAssets.exe）。

TeloCut（編集ソフト）を開かなくても、アセットサーバーの素材を探す・見る・聞く・アップロード・ダウンロードできる。
画面は TeloCut のアセットパネル（★ お気に入り / サーバー / Web 画像）と同じ。

「取り込む」を選んだときの動き（ツールバーで切り替え）:
  TeloCut に送る      … 起動中の TeloCut のビンに入れる（起動していなければダウンロード先にコピー）
  ダウンロード先にコピー … 決めたフォルダにコピー（NAS から直接見えないものはダウンロード）
  エクスプローラーで表示 … ファイルの場所を開く
エクスプローラーからファイル・フォルダをドラッグ＆ドロップするとアップロード。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import QSettings, Qt, QTimer
from PyQt6.QtGui import QAction, QActionGroup, QDesktopServices
from PyQt6.QtWidgets import (QApplication, QComboBox, QFileDialog, QLabel, QMainWindow, QMessageBox, QToolBar,
                             QToolButton, QWidget)

from . import model as M
from .assetclient2 import ClientFeatures
from .assetpanel import AssetPanel
from .settings import SETTINGS

APP_NAME = "TeloCut Assets"
ICON_NAME = "telocut_assets.ico"
MODES = [("telocut", "TeloCut に送る"), ("copy", "ダウンロード先にコピー"), ("reveal", "エクスプローラーで表示")]


# ─────────────────────────── アイコン ───────────────────────────
def draw_icon(size: int = 256):
    """青緑の角丸に、重なったサムネイルと虫めがね。"""
    from PIL import Image, ImageDraw
    s = size
    k = s / 256
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / max(1, s - 1)
        gd.line([(0, y), (s, y)], fill=(int(30 + 10 * t), int(170 - 70 * t), int(190 - 60 * t), 255))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([int(8 * k), int(8 * k), s - int(8 * k), s - int(8 * k)], radius=int(52 * k), fill=255)
    im.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(im)
    for i, col in enumerate(((255, 255, 255, 120), (255, 255, 255, 180), (255, 255, 255, 255))):
        o = int((2 - i) * 18 * k)
        d.rounded_rectangle([int(40 * k) + o, int(56 * k) - o, int(176 * k) + o, int(150 * k) - o], radius=int(10 * k), fill=col)
    # 一番手前のサムネイルの絵（山と太陽）
    d.polygon([(int(52 * k), int(140 * k)), (int(96 * k), int(92 * k)), (int(128 * k), int(124 * k)),
               (int(146 * k), int(106 * k)), (int(168 * k), int(140 * k))], fill=(40, 150, 170, 255))
    d.ellipse([int(136 * k), int(70 * k), int(158 * k), int(92 * k)], fill=(255, 200, 60, 255))
    # 虫めがね
    cx, cy, r = int(168 * k), int(172 * k), int(34 * k)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(20, 30, 40, 255), width=max(2, int(12 * k)), fill=(255, 255, 255, 230))
    d.line([cx + int(24 * k), cy + int(24 * k), cx + int(58 * k), cy + int(58 * k)], fill=(20, 30, 40, 255), width=max(3, int(16 * k)))
    return im


def make_ico(path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    draw_icon(256).save(str(path), format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return path


def app_icon():
    from PyQt6.QtGui import QIcon
    if getattr(sys, "frozen", False):
        p = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / ICON_NAME
    else:
        p = Path(__file__).with_name(ICON_NAME)
        if not p.exists():
            try:
                make_ico(p)
            except Exception:
                pass
    return QIcon(str(p)) if p.exists() else QIcon()


# ─────────────────────────── TeloCut の代わり ───────────────────────────
class _Bin:
    @staticmethod
    def selected_ids() -> list:
        return []


class _Voice:
    @staticmethod
    def refresh():
        pass


class ClientHost:
    """アセットパネルが使う「TeloCut の画面」の代わり（取り込み先を TeloCut / フォルダ / エクスプローラーにする）。"""

    def __init__(self, window: "AssetClientWindow"):
        self.window = window
        self.project = M.Project(sequences=[M.Sequence.create(name="アセット")])   # 取り込み範囲の fps 計算などに使う
        self.bin = _Bin()
        self.voice_panel = _Voice()

    @property
    def project_path(self) -> str:
        # integrations.work_dir が「プロジェクトのフォルダ / ServerAssets」にダウンロードするので、ダウンロード先を指す
        return str(Path(self.window.download_dir()) / "_assets.tcut")

    def import_files(self, paths: List[str], place=None, bin_id=None, after=None):
        self.window.handle_import(list(paths))

    def load_source(self, _mid):
        pass

    def named_bin(self, name: str) -> str:
        return name

    def begin_edit(self):
        pass

    def end_edit(self, _label=None, **_kw):
        pass


class ClientPanel(AssetPanel):
    """アセットパネル（アップロードはファイルを選ぶ・PSD の立ち絵登録は TeloCut で）。"""
    standalone = True

    def server_upload(self, paths: Optional[List[str]] = None):
        """ファイルを選んで（または渡して）アップロード。進み具合・中止・アップロード先は窓の機能で。"""
        if not self._server():
            self.msg.setText("アセットサーバーの URL を入れて「接続」してください")
            return None
        if paths is None:
            paths, _ = QFileDialog.getOpenFileNames(self, "サーバーに送るファイル")
        paths = [p for p in (paths or []) if os.path.isfile(p)]
        if not paths:
            return None
        return paths if self.mw.window.upload_files(paths) else None

    def extra_item_menu(self, menu, rows):
        self.mw.window.extra_item_menu(menu, rows)

    def register_psd_from_row(self, row):
        self.msg.setText("PSD の立ち絵登録は TeloCut のアセットパネルで行ってください（キャラクターはプロジェクトに入るため）")


# ─────────────────────────── 画面 ───────────────────────────
class AssetClientWindow(ClientFeatures, QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)
        self.qs = QSettings("DavinTelop", "TeloCutAssets")
        self.host = ClientHost(self)
        self.panel = ClientPanel(self.host)
        self.setCentralWidget(self.panel)
        self.browser = self.panel.browser
        self._build_toolbar()
        self._build_menus()
        self.telocut_label = QLabel("")
        self.telocut_label.setObjectName("info")
        self.statusBar().addPermanentWidget(self.telocut_label)
        self._telocut_timer = QTimer(self)
        self._telocut_timer.setInterval(4000)
        self._telocut_timer.timeout.connect(self.update_telocut_status)
        self._telocut_timer.start()
        self.update_telocut_status()
        geo = self.qs.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1280, 800)
        self.panel.tabs.setCurrentWidget(self.browser)
        if SETTINGS.asset_server:
            QTimer.singleShot(300, self.browser.connect_server)
        self._init_features()

    # ── 設定 ──
    def download_dir(self) -> str:
        d = str(self.qs.value("download_dir", "") or "") or str(Path.home() / "Downloads" / "TeloCut Assets")
        Path(d).mkdir(parents=True, exist_ok=True)
        return d

    def mode(self) -> str:
        return self.mode_cb.currentData()

    def _build_toolbar(self):
        tb = QToolBar("取り込み", self)
        tb.setObjectName("assets_toolbar")
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addWidget(QLabel(" 取り込むと: "))
        self.mode_cb = QComboBox()
        for k, lab in MODES:
            self.mode_cb.addItem(lab, k)
        self.mode_cb.setCurrentIndex(max(0, self.mode_cb.findData(self.qs.value("mode", "telocut"))))
        self.mode_cb.currentIndexChanged.connect(lambda _i: self.qs.setValue("mode", self.mode()))
        self.mode_cb.setToolTip("素材をダブルクリック・「取り込む」したときの動き")
        tb.addWidget(self.mode_cb)
        self.dir_btn = QToolButton()
        self.dir_btn.setToolTip("ダウンロード先を変える")
        self.dir_btn.clicked.connect(self.choose_download_dir)
        tb.addWidget(self.dir_btn)
        self._update_dir_btn()
        tb.addSeparator()
        up = QToolButton()
        up.setText("⬆ アップロード…")
        up.setToolTip("ファイルを選んでサーバーに送る（エクスプローラーからドラッグ＆ドロップでも）")
        up.clicked.connect(lambda: self.panel.server_upload())
        tb.addWidget(up)
        web = QToolButton()
        web.setText("🌐 ブラウザ版")
        web.setToolTip("アセットサーバーのブラウザ画面を開く（制作管理・変換・ファームも）")
        web.clicked.connect(self.open_web)
        tb.addWidget(web)

    def _update_dir_btn(self):
        d = self.download_dir()
        self.dir_btn.setText("📁 " + (d if len(d) < 42 else "…" + d[-40:]))

    def _build_menus(self):
        mb = self.menuBar()
        f = mb.addMenu("ファイル")
        f.addAction("アップロード…", lambda: self.panel.server_upload())
        f.addAction("ダウンロード先を変える…", self.choose_download_dir)
        f.addAction("ダウンロード先を開く", lambda: self._open_folder(self.download_dir()))
        f.addSeparator()
        f.addAction("終了", self.close)
        v = mb.addMenu("表示")
        th = v.addMenu("配色")
        from . import theme as TH
        grp = QActionGroup(self)
        for key, t in TH.THEMES.items():
            a = QAction(t.get("label", key), self, checkable=True)
            a.setChecked(key == (SETTINGS.theme or "avid"))
            a.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            grp.addAction(a)
            th.addAction(a)
        v.addAction("サーバーに接続し直す", self.browser.connect_server)
        h = mb.addMenu("ヘルプ")
        h.addAction("ブラウザ版を開く", self.open_web)
        h.addAction(f"{APP_NAME} について", self.about)

    def set_theme(self, key: str):
        from . import theme as TH
        SETTINGS.theme = key
        SETTINGS.save()
        TH.apply(QApplication.instance())

    def choose_download_dir(self):
        d = QFileDialog.getExistingDirectory(self, "ダウンロード先", self.download_dir())
        if d:
            self.qs.setValue("download_dir", d)
            self._update_dir_btn()

    def open_web(self):
        url = (SETTINGS.asset_server or "").rstrip("/")
        if not url:
            QMessageBox.information(self, APP_NAME, "アセットサーバーの URL を入れて「接続」してください。")
            return
        from PyQt6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(url + "/"))

    def about(self):
        from .version import VERSION
        QMessageBox.about(self, APP_NAME, f"<b>{APP_NAME}</b> {VERSION}<br>アセットサーバーの素材を探す・見る・送るためのアプリ。<br>"
                                          f"サーバー: {SETTINGS.asset_server or '（未設定）'}<br>ダウンロード先: {self.download_dir()}")

    # ── TeloCut ──
    def telocut_running(self) -> bool:
        from . import bridge
        if not bridge.find_running_port():
            return False
        try:
            bridge.call("ping", timeout=0.6)
            return True
        except Exception:
            return False

    def update_telocut_status(self) -> bool:
        on = self.telocut_running()
        self.telocut_label.setText("TeloCut: 起動中（取り込みはビンへ）" if on else "TeloCut: 起動していません")
        return on

    # ── 取り込み ──
    def handle_import(self, paths: List[str]) -> str:
        """取り込みの動き（TeloCut に送る / コピー / 場所を開く）。何をしたかを返す。"""
        if not paths:
            return ""
        mode = self.mode()
        if mode == "telocut":
            try:
                from . import bridge
                bridge.call("import_files", paths=paths, timeout=10)
                self.panel.msg.setText(f"{len(paths)} 個を TeloCut のビンに送りました")
                return "telocut"
            except Exception:
                self.panel.msg.setText("TeloCut が起動していないので、ダウンロード先にコピーします")
                mode = "copy"
        if mode == "reveal":
            self._reveal(paths[0])
            return "reveal"
        dst_dir = Path(self.download_dir())
        n = 0
        for p in paths:
            src = Path(p)
            try:
                if dst_dir in src.resolve().parents:            # もうダウンロード先にある（サーバーから落としたもの）
                    continue
                dst = dst_dir / src.name
                if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                    shutil.copy2(src, dst)
                    n += 1
            except OSError as ex:
                self.panel.msg.setText(f"コピーできませんでした: {src.name}（{ex}）")
        self.panel.msg.setText(f"ダウンロード先に {len(paths)} 個（コピー {n} 個）: {dst_dir}")
        return "copy"

    def _reveal(self, path: str):
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            self._open_folder(str(Path(path).parent))

    def _open_folder(self, d: str):
        from PyQt6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    # ── ドラッグ＆ドロップでアップロード ──
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.isLocalFile() for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = []
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if os.path.isdir(p):
                for dp, _ds, fs in os.walk(p):
                    files += [os.path.join(dp, f) for f in fs if not f.startswith(".")]
            elif os.path.isfile(p):
                files.append(p)
        if files:
            self.panel.server_upload(files[:500])

    def closeEvent(self, e):
        self.qs.setValue("geometry", self.saveGeometry())
        self.remember_place()
        if self.tray_close(e):
            return
        if self.tray is not None:
            self.tray.hide()
        super().closeEvent(e)


def selftest(out: str) -> int:
    """EXE の確認（画面を出さずに作って、部品がそろっているか）。"""
    lines = []
    try:
        app = QApplication.instance() or QApplication(sys.argv)
        from . import theme as TH
        TH.apply(app)
        w = AssetClientWindow()
        w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        w.show()
        app.processEvents()
        tabs = [w.panel.tabs.tabText(i) for i in range(w.panel.tabs.count())]
        lines.append(f"tabs: {tabs}")
        lines.append(f"modes: {[w.mode_cb.itemText(i) for i in range(w.mode_cb.count())]}")
        lines.append(f"icon: {not w.windowIcon().isNull()}")
        ok = "サーバー" in tabs and "変換ジョブ" in tabs and w.browser is not None and w.mode_cb.count() == 3
        lines.append("RESULT PASS" if ok else "RESULT FAIL")
        w.close()
    except Exception as ex:
        import traceback
        lines.append(traceback.format_exc())
        lines.append(f"RESULT FAIL {ex}")
        ok = False
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        return selftest(sys.argv[i + 1] if i + 1 < len(sys.argv) else "telocut_assets_selftest.txt")
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DavinTelop.TeloCutAssets")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    from . import theme as TH
    TH.apply(app)
    w = AssetClientWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
