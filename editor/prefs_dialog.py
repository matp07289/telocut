"""環境設定ダイアログ。"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget, QFormLayout,
                             QSpinBox, QDoubleSpinBox, QCheckBox, QLineEdit, QPushButton, QLabel,
                             QFileDialog, QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
                             QDialogButtonBox, QMessageBox)

from .settings import SETTINGS


class PreferencesDialog(QDialog):
    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("環境設定")
        self.resize(720, 520)
        v = QVBoxLayout(self)
        tabs = QTabWidget()
        v.addWidget(tabs, 1)

        # 保存・キャッシュ
        t1 = QWidget()
        f = QFormLayout(t1)
        self.autosave = QSpinBox()
        self.autosave.setRange(0, 120)
        self.autosave.setSuffix(" 分ごと（0 = しない）")
        self.autosave.setValue(SETTINGS.autosave_minutes)
        f.addRow("自動保存", self.autosave)
        self.attic_keep = QSpinBox()
        self.attic_keep.setRange(1, 500)
        self.attic_keep.setSuffix(" 世代")
        self.attic_keep.setValue(SETTINGS.attic_keep)
        f.addRow("Attic（バックアップ）", self.attic_keep)
        self.dirs = {}
        for key, label in (("attic_dir", "Attic の場所"), ("cache_dir", "音声・波形キャッシュ"),
                           ("render_dir", "レンダーキャッシュ"), ("proxy_dir", "プロキシ"),
                           ("local_media_dir", "NAS 素材のローカルコピー先")):
            row = QHBoxLayout()
            ed = QLineEdit(getattr(SETTINGS, key))
            b = QPushButton("...")
            b.setFixedWidth(30)
            b.clicked.connect(lambda _=False, e=ed: self._browse(e))
            row.addWidget(ed, 1)
            row.addWidget(b)
            self.dirs[key] = ed
            f.addRow(label, row)
        self.render_gb = QDoubleSpinBox()
        self.render_gb.setRange(1, 2000)
        self.render_gb.setSuffix(" GB まで（超えたら古い順に削除）")
        self.render_gb.setValue(SETTINGS.render_cache_gb)
        f.addRow("レンダーキャッシュ容量", self.render_gb)
        self.ram = QSpinBox()
        self.ram.setRange(30, 5000)
        self.ram.setSuffix(" フレーム")
        self.ram.setValue(SETTINGS.ram_cache_frames)
        f.addRow("RAM キャッシュ", self.ram)
        note = QLabel("※ キャッシュ・プロキシ・Attic は各 PC のローカル SSD を推奨します（NAS に置くと遅くなります）。")
        note.setObjectName("info")
        note.setWordWrap(True)
        f.addRow(note)
        tabs.addTab(t1, "保存・キャッシュ")

        # 再生
        t2 = QWidget()
        f2 = QFormLayout(t2)
        self.use_proxy = QCheckBox("プロキシがあれば再生に使う（書き出しは常に元素材）")
        self.use_proxy.setChecked(SETTINGS.use_proxy)
        f2.addRow(self.use_proxy)
        self.proxy_w = QComboBox()
        for w in (640, 960, 1280):
            self.proxy_w.addItem(f"幅 {w}px", w)
        self.proxy_w.setCurrentIndex(max(0, [640, 960, 1280].index(SETTINGS.proxy_width)
                                         if SETTINGS.proxy_width in (640, 960, 1280) else 1))
        f2.addRow("プロキシの解像度", self.proxy_w)
        self.auto_proxy = QComboBox()
        for lab, v_ in (("自動作成しない", 0), ("幅 1920 超（2K 以上）", 1920), ("幅 2560 超（4K 等）", 2560),
                        ("幅 3840 超（6K 以上）", 3840)):
            self.auto_proxy.addItem(lab, v_)
        idx = [0, 1920, 2560, 3840].index(SETTINGS.auto_proxy_over) if SETTINGS.auto_proxy_over in (0, 1920, 2560, 3840) else 2
        self.auto_proxy.setCurrentIndex(idx)
        f2.addRow("読み込み時に自動でプロキシ", self.auto_proxy)
        self.idle = QCheckBox("停止中に重い部分を自動でバックグラウンドレンダリング")
        self.idle.setChecked(SETTINGS.idle_render)
        f2.addRow(self.idle)
        self.threads = QSpinBox()
        self.threads.setRange(1, 16)
        self.threads.setValue(SETTINGS.render_threads)
        f2.addRow("レンダリングのスレッド数", self.threads)
        self.fx_mode = QComboBox()
        self.fx_mode.addItem("レイヤー（上から順に掛ける・AviUtl / Premiere 風）", "stack")
        self.fx_mode.addItem("ノード（分岐・合成・Resolve Fusion / Blender 風）", "nodes")
        self.fx_mode.setCurrentIndex(max(0, self.fx_mode.findData(SETTINGS.fx_mode_default)))
        f2.addRow("エフェクトの管理方法（新しく付けるとき）", self.fx_mode)
        tabs.addTab(t2, "再生・レンダリング")

        # NAS・共有
        t3 = QWidget()
        v3 = QVBoxLayout(t3)
        fu = QFormLayout()
        self.user = QLineEdit(SETTINGS.user_name)
        fu.addRow("ユーザー名（ロック表示用）", self.user)
        v3.addLayout(fu)
        v3.addWidget(QLabel("パス置換ルール（PC によってドライブ文字や共有名が違う場合）"))
        self.maps = QTableWidget(0, 2)
        self.maps.setHorizontalHeaderLabels(["置換前（例: Z:\\  /  \\\\NAS\\video）", "置換後（この PC での場所）"])
        self.maps.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for src, dst in SETTINGS.path_maps:
            r = self.maps.rowCount()
            self.maps.insertRow(r)
            self.maps.setItem(r, 0, QTableWidgetItem(src))
            self.maps.setItem(r, 1, QTableWidgetItem(dst))
        v3.addWidget(self.maps, 1)
        h = QHBoxLayout()
        b = QPushButton("行を追加")
        b.clicked.connect(lambda: self.maps.insertRow(self.maps.rowCount()))
        h.addWidget(b)
        b = QPushButton("選択行を削除")
        b.clicked.connect(lambda: self.maps.removeRow(self.maps.currentRow()) if self.maps.currentRow() >= 0 else None)
        h.addWidget(b)
        h.addStretch(1)
        v3.addLayout(h)
        tip = QLabel("NAS 運用のポイント: プロジェクトと素材は NAS、キャッシュ類はローカル。"
                     "プロジェクトを開いている間は .lock ファイルで他の人の上書きを防ぎます。"
                     "素材は「プロジェクトからの相対パス」でも探すので、プロジェクトフォルダごと移動しても再リンク不要です。")
        tip.setObjectName("info")
        tip.setWordWrap(True)
        v3.addWidget(tip)
        tabs.addTab(t3, "NAS・共有")

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _browse(self, ed: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "フォルダを選択", ed.text())
        if d:
            ed.setText(d)

    def apply(self):
        SETTINGS.autosave_minutes = self.autosave.value()
        SETTINGS.attic_keep = self.attic_keep.value()
        for k, ed in self.dirs.items():
            if ed.text().strip():
                setattr(SETTINGS, k, ed.text().strip())
        SETTINGS.render_cache_gb = self.render_gb.value()
        SETTINGS.ram_cache_frames = self.ram.value()
        SETTINGS.use_proxy = self.use_proxy.isChecked()
        SETTINGS.proxy_width = self.proxy_w.currentData()
        SETTINGS.auto_proxy_over = self.auto_proxy.currentData()
        SETTINGS.idle_render = self.idle.isChecked()
        SETTINGS.render_threads = self.threads.value()
        SETTINGS.fx_mode_default = self.fx_mode.currentData()
        SETTINGS.user_name = self.user.text().strip() or SETTINGS.user_name
        maps = []
        for r in range(self.maps.rowCount()):
            a = self.maps.item(r, 0)
            b = self.maps.item(r, 1)
            if a and b and a.text().strip() and b.text().strip():
                maps.append([a.text().strip(), b.text().strip()])
        SETTINGS.path_maps = maps
        SETTINGS.save()
