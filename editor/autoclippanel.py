"""自動切り抜きの画面（Stretheus 風）: 入力 → しきい値と見どころの確認 → 字幕の確認（字幕決定）→ 書き出し。"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QRadioButton,
                             QSlider, QSpinBox, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from . import autoclip as AC
from . import model as M
from .settings import SETTINGS

VIDEO_FILTER = "動画 (*.mp4 *.mov *.avi *.mkv *.flv *.wmv *.m4v *.webm *.mxf)"


class _Worker(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.cancelled = False

    def run(self):
        try:
            self.done.emit(self.fn(self))
        except Exception as ex:
            import traceback
            self.failed.emit(f"{ex}\n{traceback.format_exc()[-600:]}")


class _DropEdit(QLineEdit):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setPlaceholderText("ここに動画をドラッグ＆ドロップ（または「参照...」）")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.setText(urls[0])


class _Graph(QWidget):
    """音の大きさ（灰）としきい値（赤線）、見どころの点数（紫）と候補（帯）。クリックした位置を通知。"""
    clicked = pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(150)
        self.ld: Optional[AC.Loudness] = None
        self.score: Optional[np.ndarray] = None
        self.cands: List[AC.Candidate] = []
        self.threshold = -40.0
        self.checked: set = set()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#141418"))
        if self.ld is None or self.ld.db.size == 0:
            p.setPen(QColor("#888"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "解析すると、ここに音の大きさと見どころが出ます")
            return
        W, H = self.width(), self.height()
        dur = max(1e-6, self.ld.duration)
        lo, hi = -70.0, 0.0
        y = lambda v: H - (min(hi, max(lo, v)) - lo) / (hi - lo) * H
        for i, c in enumerate(self.cands):
            x0, x1 = c.start / dur * W, c.end / dur * W
            p.fillRect(QRectF(x0, 0, max(2, x1 - x0), H), QColor(124, 108, 242, 70 if i in self.checked else 30))
        db = self.ld.db
        step = max(1, len(db) // max(1, W))
        pts = db[:len(db) // step * step].reshape(-1, step).max(axis=1) if step > 1 else db
        p.setPen(QPen(QColor("#5a5a66"), 1))
        for i, v in enumerate(pts):
            x = i / len(pts) * W
            p.drawLine(QPointF(x, H), QPointF(x, y(v)))
        p.setPen(QPen(QColor("#e05050"), 1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(0, y(self.threshold)), QPointF(W, y(self.threshold)))
        p.drawText(4, int(y(self.threshold)) - 3, f"{self.threshold:.1f} dB")
        if self.score is not None and len(self.score):
            path = QPainterPath()
            for i, v in enumerate(self.score):
                pt = QPointF(i / len(self.score) * W, H - v / 100 * H * 0.95)
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            p.setPen(QPen(QColor("#a99cff"), 2))
            p.drawPath(path)

    def mousePressEvent(self, e):
        if self.ld is not None:
            self.clicked.emit(e.position().x() / max(1, self.width()) * self.ld.duration)


class AutoClipDialog(QDialog):
    """配信アーカイブ / 動画 → 自動で切り抜き（見どころ・無音カット・字幕・OP/ED・音量・書き出し）。"""

    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("自動切り抜き（見どころ・無音カット・字幕・書き出し）")
        self.resize(980, 720)
        self.cfg = dict(SETTINGS.autoclip or {})
        self.media: Optional[M.MediaItem] = None
        self.ld: Optional[AC.Loudness] = None
        self.series = None
        self.cands: List[AC.Candidate] = []
        self.made: List[dict] = []                 # [{"seq": Sequence, "ranges": [...], "main": (a, b)}]
        self.worker = None
        v = QVBoxLayout(self)
        self.steps = QLabel("")
        self.steps.setObjectName("info")
        v.addWidget(self.steps)
        self.stack = QStackedWidget()
        v.addWidget(self.stack, 1)
        self.stack.addWidget(self._page_input())
        self.stack.addWidget(self._page_check())
        self.stack.addWidget(self._page_subs())
        self.stack.addWidget(self._page_done())
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        v.addWidget(self.bar)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        v.addWidget(self.msg)
        self._go(0)

    # ─────────────── 1. 入力と設定 ───────────────
    def _page_input(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        f = QFormLayout()
        self.inp = _DropEdit()
        self.inp.setText(self.cfg.get("last_input", ""))
        b = QPushButton("参照...")
        b.clicked.connect(lambda: self._pick(self.inp, VIDEO_FILTER))
        f.addRow("入力動画", self._row(self.inp, b))
        self.chat = QLineEdit()
        self.chat.setPlaceholderText("任意: チャットのログ（TwitchDownloader / chat-downloader の JSON・yt-dlp の live_chat・テキスト）")
        b = QPushButton("参照...")
        b.clicked.connect(lambda: self._pick(self.chat, "チャット (*.json *.jsonl *.txt *.csv *.log)"))
        f.addRow("チャット", self._row(self.chat, b))
        self.mk = QLineEdit()
        self.mk.setPlaceholderText("任意: 配信マーカー（Twitch のマーカー CSV・「0:12:34 説明」の文字）")
        b = QPushButton("参照...")
        b.clicked.connect(lambda: self._pick(self.mk, "マーカー (*.csv *.txt)"))
        f.addRow("配信マーカー", self._row(self.mk, b))
        v.addLayout(f)
        g = QGroupBox("作るもの")
        gv = QVBoxLayout(g)
        self.mode = QButtonGroup(self)
        for i, (k, lab) in enumerate((("archive", "アーカイブから見どころを探して切り抜く（候補から選ぶ）"),
                                      ("whole", "動画全体を 1 本に（無音カット・字幕だけ）"),
                                      ("clip", "クリップ（短い動画）を縦動画に"))):
            r = QRadioButton(lab)
            r.setProperty("k", k)
            self.mode.addButton(r, i)
            gv.addWidget(r)
            if k == self.cfg.get("mode", "archive"):
                r.setChecked(True)
        v.addWidget(g)
        g = QGroupBox("設定（次回も同じ）")
        gf = QFormLayout(g)
        self.outdir = QLineEdit(self.cfg.get("outdir", ""))
        self.outdir.setPlaceholderText("空なら入力動画と同じフォルダ")
        b = QPushButton("参照...")
        b.clicked.connect(lambda: (lambda d: self.outdir.setText(d) if d else None)(QFileDialog.getExistingDirectory(self, "出力ディレクトリ")))
        gf.addRow("出力ディレクトリ", self._row(self.outdir, b))
        self.do_cut = QCheckBox("無音カットを有効にする")
        self.do_cut.setChecked(self.cfg.get("cut", True))
        self.do_sub = QCheckBox("テロップ生成を有効にする（音声認識）")
        self.do_sub.setChecked(self.cfg.get("subs", True))
        self.do_edit = QCheckBox("焼き込み前に字幕編集画面を開く（おすすめ）")
        self.do_edit.setChecked(self.cfg.get("edit", True))
        self.do_loud = QCheckBox("音量をそろえる（−14 LUFS・動画サイト向け）")
        self.do_loud.setChecked(self.cfg.get("loud", True))
        self.do_xml = QCheckBox("他の編集ソフト用に FCPXML と SRT も書き出す")
        self.do_xml.setChecked(self.cfg.get("xml", False))
        for c in (self.do_cut, self.do_sub, self.do_edit, self.do_loud, self.do_xml):
            gf.addRow(c)
        self.lang = QComboBox()
        self.lang.addItem("日本語", "ja")
        self.lang.addItem("英語", "en")
        self.lang.setCurrentIndex(max(0, self.lang.findData(self.cfg.get("lang", "ja"))))
        self.model_size = QComboBox()
        self.model_size.addItems(["small", "medium", "large-v3", "base"])
        self.model_size.setCurrentText(self.cfg.get("model", "small"))
        gf.addRow("字幕の言語 / モデル", self._row(self.lang, self.model_size))
        self.layout_ = QComboBox()
        self.layout_.addItem("横動画", "h")
        self.layout_.addItem("縦動画（ショート）", "v")
        self.layout_.setCurrentIndex(max(0, self.layout_.findData(self.cfg.get("layout", "h"))))
        self.crop = QSlider(Qt.Orientation.Horizontal)
        self.crop.setRange(0, 100)
        self.crop.setValue(int(self.cfg.get("crop", 50)))
        self.crop.setToolTip("縦動画にするとき、横長の映像のどこを切り出すか（左端〜右端）")
        gf.addRow("向き / 切り出し位置", self._row(self.layout_, QLabel("左"), self.crop, QLabel("右")))
        self.op = QLineEdit(self.cfg.get("opener", ""))
        self.ed = QLineEdit(self.cfg.get("ending", ""))
        for e, lab in ((self.op, "オープニング"), (self.ed, "エンディング")):
            e.setPlaceholderText("任意: 前 / 後ろに付ける動画")
            b = QPushButton("参照...")
            b.clicked.connect(lambda _=False, e=e: self._pick(e, VIDEO_FILTER))
            gf.addRow(lab, self._row(e, b))
        self.fade = QSpinBox()
        self.fade.setRange(0, 15)
        self.fade.setValue(int(self.cfg.get("fade", 2)))
        self.fade.setSuffix(" フレーム")
        gf.addRow("つなぎ目のフェード（音）", self.fade)
        from . import mediacodecs as MC
        self.fmt = QComboBox()
        for p in MC.PRESETS:
            if p.vcodec:
                self.fmt.addItem(f"{p.group}: {p.label}", p.id)
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(self.cfg.get("format", "h264"))))
        self.farm = QCheckBox("ファームで書き出す（ほかの PC に任せる）")
        self.farm.setChecked(self.cfg.get("farm", False) and bool(SETTINGS.asset_server))
        self.farm.setEnabled(bool(SETTINGS.asset_server))
        gf.addRow("書き出し形式", self._row(self.fmt, self.farm))
        v.addWidget(g, 1)
        h = QHBoxLayout()
        h.addStretch(1)
        go = QPushButton("実行")
        go.setDefault(True)
        go.clicked.connect(self.run_analysis)
        h.addWidget(go)
        v.addLayout(h)
        return w

    @staticmethod
    def _row(*ws):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for x in ws:
            h.addWidget(x, 1 if isinstance(x, (QLineEdit, QSlider)) else 0)
        return w

    def _pick(self, edit: QLineEdit, filt: str):
        p, _ = QFileDialog.getOpenFileName(self, "ファイルを選ぶ", os.path.dirname(edit.text() or ""), filt)
        if p:
            edit.setText(p)

    def mode_key(self) -> str:
        b = self.mode.checkedButton()
        return b.property("k") if b else "archive"

    def save_cfg(self):
        self.cfg.update({"last_input": self.inp.text().strip(), "mode": self.mode_key(), "outdir": self.outdir.text().strip(),
                         "cut": self.do_cut.isChecked(), "subs": self.do_sub.isChecked(), "edit": self.do_edit.isChecked(),
                         "loud": self.do_loud.isChecked(), "xml": self.do_xml.isChecked(), "lang": self.lang.currentData(),
                         "model": self.model_size.currentText(), "layout": self.layout_.currentData(),
                         "crop": self.crop.value(), "opener": self.op.text().strip(), "ending": self.ed.text().strip(),
                         "fade": self.fade.value(), "format": self.fmt.currentData(), "farm": self.farm.isChecked()})
        SETTINGS.autoclip = self.cfg
        SETTINGS.save()

    def _go(self, i: int):
        self.stack.setCurrentIndex(i)
        names = ["① 入力", "② しきい値・見どころの確認", "③ 字幕の確認", "④ 書き出し"]
        self.steps.setText("　→　".join(f"<b>{n}</b>" if k == i else n for k, n in enumerate(names)))

    def _start(self, title: str, fn, done):
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self.msg.setText(title)
        w = _Worker(fn)
        w.progress.connect(lambda i, n, t: (self.bar.setRange(0, max(1, n)), self.bar.setValue(i), self.msg.setText(f"{title} {t}")))
        w.done.connect(lambda r: (self.bar.setVisible(False), done(r)))
        w.failed.connect(lambda e: (self.bar.setVisible(False), self.msg.setText("失敗しました"),
                                    QMessageBox.warning(self, "自動切り抜き", e)))
        self.worker = w
        w.start()

    # ─────────────── 2. 解析 → 確認 ───────────────
    def run_analysis(self, wait: bool = False):
        path = self.inp.text().strip().strip('"')
        if not path or not os.path.isfile(path):
            QMessageBox.information(self, "自動切り抜き", "入力動画を指定してください（「参照...」かドラッグ＆ドロップ）。")
            return
        self.save_cfg()
        chat_p, mk_p = self.chat.text().strip(), self.mk.text().strip()

        def work(task):
            task.progress.emit(0, 0, "音の大きさを測っています…")
            ld = AC.measure(path)
            chat = AC.load_chat(chat_p) if chat_p and os.path.isfile(chat_p) else []
            mks = AC.load_markers(mk_p) if mk_p and os.path.isfile(mk_p) else []
            series = AC.score_series(ld, chat)
            return ld, chat, mks, series
        if wait:
            self._analysed(work(_DummyTask()))
        else:
            self._start("解析中…", work, self._analysed)

    def _analysed(self, res):
        self.ld, self.chat_rows, self.markers, self.series = res
        mode = self.mode_key()
        self.cands = AC.candidates(self.series, self.markers, count=12) if mode == "archive" else []
        self.graph.ld, self.graph.score = self.ld, self.series["score"]
        self.graph.cands = self.cands
        th = self.ld.suggest_threshold()
        self.th.blockSignals(True)
        self.th.setValue(th)
        self.th.blockSignals(False)
        self._th_changed()
        self.clist.clear()
        for i, c in enumerate(self.cands):
            it = QListWidgetItem(f"{_tc(c.start)}〜{_tc(c.end)}（{c.length:.0f} 秒）  点数 {c.score:.0f}  "
                                 + (f"◆{c.marker}  " if c.marker else "") + "・".join(c.reasons))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if (i < 3 or c.marker) else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, i)
            self.clist.addItem(it)
        self.clist.setVisible(mode == "archive")
        self.cand_label.setVisible(mode == "archive")
        self._sync_checked()
        self.msg.setText(f"長さ {_tc(self.ld.duration)}・チャット {len(self.chat_rows)} 件・マーカー {len(self.markers)} 個"
                         + (f"・候補 {len(self.cands)} 個" if mode == "archive" else ""))
        self._go(1)

    def _page_check(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.graph = _Graph()
        self.graph.clicked.connect(self._graph_click)
        v.addWidget(self.graph, 1)
        h = QHBoxLayout()
        h.addWidget(QLabel("カット閾値（これより小さい音を無音とみなす）"))
        self.th = QDoubleSpinBox()
        self.th.setRange(-80, -10)
        self.th.setDecimals(1)
        self.th.setSuffix(" dB")
        self.th.valueChanged.connect(self._th_changed)
        h.addWidget(self.th)
        self.minsil = QDoubleSpinBox()
        self.minsil.setRange(0.2, 5)
        self.minsil.setValue(float(self.cfg.get("min_silence", 0.6)))
        self.minsil.setSuffix(" 秒以上の無音を詰める")
        self.minsil.valueChanged.connect(self._th_changed)
        h.addWidget(self.minsil)
        self.remain = QLabel("")
        h.addWidget(self.remain, 1)
        v.addLayout(h)
        v.addWidget(QLabel("会話が切れるときは dB を小さく、無音が残るときは大きく"))
        self.cand_label = QLabel("見どころの候補（点数の高い順。チェックした区間を切り抜く・グラフをクリックでその位置を確認）")
        v.addWidget(self.cand_label)
        self.clist = QListWidget()
        self.clist.itemChanged.connect(lambda _it: self._sync_checked())
        self.clist.itemDoubleClicked.connect(lambda it: self._preview_at(self.cands[it.data(Qt.ItemDataRole.UserRole)].start))
        v.addWidget(self.clist, 1)
        h = QHBoxLayout()
        back = QPushButton("戻る")
        back.clicked.connect(lambda: self._go(0))
        h.addWidget(back)
        h.addStretch(1)
        ok = QPushButton("OK（作る）")
        ok.setDefault(True)
        ok.clicked.connect(lambda: self.make_clips())
        h.addWidget(ok)
        v.addLayout(h)
        return w

    def _sync_checked(self):
        self.graph.checked = {self.clist.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.clist.count())
                              if self.clist.item(i).checkState() == Qt.CheckState.Checked}
        self.graph.update()

    def _th_changed(self, *_a):
        if self.ld is None:
            return
        self.graph.threshold = self.th.value()
        self.graph.update()
        if self.do_cut.isChecked():
            r = self.ld.remaining(self.th.value(), self.minsil.value())
            self.remain.setText(f"全体 {_tc(self.ld.duration)} → 無音カット後 約 {_tc(r)}")
        else:
            self.remain.setText("無音カットはしません")

    def _graph_click(self, t: float):
        best = next((i for i, c in enumerate(self.cands) if c.start <= t < c.end), None)
        if best is not None:
            self.clist.setCurrentRow(best)
        self._preview_at(t)

    def _preview_at(self, t: float):
        """その位置をソースモニターで見る（取り込んでいなければ取り込む）。"""
        m = self._ensure_media(wait=False)
        if m is not None and hasattr(self.mw, "load_source"):
            self.mw.load_source(m.id)
            try:
                self.mw.source_player.seek(int(t * self.mw.project.sequence.fps))
            except Exception:
                pass

    def _ensure_media(self, wait: bool = True, path: str = "") -> Optional[M.MediaItem]:
        path = path or self.inp.text().strip().strip('"')
        hit = next((m for m in self.mw.project.media if os.path.normcase(m.path) == os.path.normcase(path)), None)
        if hit is not None:
            return hit
        from . import media as MD
        info = MD.probe(path)
        info.id = M.new_id()
        self.mw.begin_edit()
        self.mw.project.media.append(info)
        (self.mw.project.bin_by_id(self.mw.bin.current_bin_id()) or self.mw.project.bins[0]).add("media", info.id)
        self.mw.end_edit(f"素材を追加: {info.name}")
        if info.has_audio:
            threading.Thread(target=lambda: (MD.build_audio_cache(path), MD.build_peaks(path)), daemon=True).start()
        self.mw.bin.refresh()
        return info

    # ─────────────── 3. 作る（無音カット・OP/ED・縦・字幕・音量）───────────────
    def selected_candidates(self) -> List[AC.Candidate]:
        return [self.cands[self.clist.item(i).data(Qt.ItemDataRole.UserRole)] for i in range(self.clist.count())
                if self.clist.item(i).checkState() == Qt.CheckState.Checked]

    def make_clips(self, wait: bool = False):
        mode = self.mode_key()
        self.cfg["min_silence"] = self.minsil.value()
        self.save_cfg()
        src = self._ensure_media()
        op = self._ensure_media(path=self.op.text().strip()) if self.op.text().strip() and os.path.isfile(self.op.text().strip()) else None
        ed = self._ensure_media(path=self.ed.text().strip()) if self.ed.text().strip() and os.path.isfile(self.ed.text().strip()) else None
        if mode == "archive":
            spans = [(c.start, c.end, c.marker or (c.reasons[0] if c.reasons else "見どころ")) for c in self.selected_candidates()]
            if not spans:
                QMessageBox.information(self, "自動切り抜き", "切り抜く候補にチェックを入れてください。")
                return
        else:
            spans = [(0.0, self.ld.duration, "全体")]
        vertical = self.layout_.currentData() == "v" or mode == "clip"
        seq0 = self.mw.project.sequence
        fps = src.fps if src.fps and 10 < src.fps < 121 else seq0.fps
        stem = Path(src.path).stem
        self.mw.begin_edit()
        self.made = []
        for k, (a, b, why) in enumerate(spans):
            ranges = AC.keep_ranges(self.ld, self.th.value(), self.minsil.value(), 0.12, a, b) if self.do_cut.isChecked() \
                else [(a, b)]
            name = f"{stem}_{k + 1:02d}" if len(spans) > 1 else stem
            seq, ms, me = AC.build_sequence(self.mw.project, src, ranges, name, fps, src.width or seq0.width,
                                            src.height or seq0.height, op, ed, self.fade.value(), vertical,
                                            self.crop.value() / 100)
            (self.mw.project.bin_of(seq0.id) or self.mw.project.bins[0]).add("sequence", seq.id)
            self.made.append({"seq": seq, "ranges": ranges, "main": (ms, me), "why": why, "vertical": vertical})
        self.mw.end_edit(f"自動切り抜き（{len(self.made)} 本）")
        self.mw.bin.refresh()
        if self.do_sub.isChecked():
            self._transcribe(wait)
        else:
            self._after_subs(wait)

    def _transcribe(self, wait: bool = False):
        import copy
        proj = copy.deepcopy(self.mw.project)
        lang, model = self.lang.currentData(), self.model_size.currentText()
        jobs = [(m["seq"].id, m["main"]) for m in self.made]

        paths = self._audio_paths()

        def work(task):
            import tempfile
            from .smartrender import write_audio
            _ensure_audio(paths, task)
            out = {}
            for i, (sid, (a, b)) in enumerate(jobs):
                sq = proj.sequence_by_id(sid)
                fd, wav = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                try:
                    task.progress.emit(i, len(jobs), f"音声認識 {i + 1} / {len(jobs)}（時間がかかります）")
                    write_audio(proj, sq, a, b, wav)
                    out[sid] = TRANSCRIBE(wav, model, lang)
                finally:
                    try:
                        os.remove(wav)
                    except OSError:
                        pass
            return out
        if wait:
            self._got_subs(work(_DummyTask()), wait)
        else:
            self._start("テロップ生成中…", work, self._got_subs)

    def _got_subs(self, res: Dict[str, list], wait: bool = False):
        from .yukkuri import subtitle_chunks
        self.mw.begin_edit()
        n = 0
        for m in self.made:
            seq = m["seq"]
            a, _b = m["main"]
            t = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
            seq.video_tracks.append(t)
            for s in res.get(seq.id, []):
                s0, s1 = a + int(round(s.start * seq.fps)), a + int(round(s.end * seq.fps))
                chunks = subtitle_chunks(s.text, 16 if m["vertical"] else 22)
                tot = sum(len(c) for c in chunks) or 1
                pos = s0
                for i, ch in enumerate(chunks):
                    d = (s1 - pos) if i == len(chunks) - 1 else max(1, int((s1 - s0) * len(ch) / tot))
                    if d <= 0:
                        continue
                    t.clips.append(M.Clip(M.new_id(), "text", pos, d, name="字幕",
                                          props={"text": ch, "speaker": "streamer",
                                                 "style": AC.role_style("streamer", vertical=m["vertical"])}))
                    pos += d
                    n += 1
            t.sort()
            for c0, c1 in zip(t.clips, t.clips[1:]):
                if c0.end > c1.start:
                    c0.duration = max(1, c1.start - c0.start)
            m["sub_track"] = t
        self.mw.end_edit(f"字幕（{n} 枚）")
        self.msg.setText(f"字幕 {n} 枚")
        if self.do_edit.isChecked() and n:
            self._fill_subs()
            self._go(2)
            if wait:
                return
        else:
            self._after_subs(wait)

    # ─────────────── 3'. 字幕の確認（字幕決定）───────────────
    def _page_subs(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("誤字を直す・チェックを外して消す・話者（配信者 / サブ / コメント）を選ぶ。行を選ぶとプレビュー"))
        sp = QSplitter(Qt.Orientation.Horizontal)
        self.stable = QTableWidget(0, 4)
        self.stable.setHorizontalHeaderLabels(["表示", "時間", "テロップ", "話者"])
        self.stable.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.stable.verticalHeader().setVisible(False)
        self.stable.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.stable.currentCellChanged.connect(lambda r, *_: self._preview_sub(r))
        sp.addWidget(self.stable)
        self.pv = QLabel("")
        self.pv.setMinimumWidth(320)
        self.pv.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pv.setStyleSheet("background:#000")
        sp.addWidget(self.pv)
        sp.setSizes([600, 340])
        v.addWidget(sp, 1)
        h = QHBoxLayout()
        h.addStretch(1)
        ok = QPushButton("字幕決定")
        ok.setDefault(True)
        ok.clicked.connect(lambda: self.apply_subs())
        h.addWidget(ok)
        v.addLayout(h)
        return w

    def _sub_rows(self) -> List[Tuple[dict, M.Clip]]:
        rows = []
        for m in self.made:
            t = m.get("sub_track")
            if t is not None:
                rows += [(m, c) for c in sorted(t.clips, key=lambda c: c.start)]
        return rows

    def _fill_subs(self):
        self._rows = self._sub_rows()
        self.stable.setRowCount(len(self._rows))
        for r, (m, c) in enumerate(self._rows):
            chk = QTableWidgetItem("")
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if c.enabled else Qt.CheckState.Unchecked)
            self.stable.setItem(r, 0, chk)
            tm = QTableWidgetItem(f"{m['seq'].name}  {_tc(c.start / m['seq'].fps)}")
            tm.setFlags(tm.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.stable.setItem(r, 1, tm)
            self.stable.setItem(r, 2, QTableWidgetItem(str(c.prop("text", ""))))
            cb = QComboBox()
            for k, lab in AC.ROLES:
                cb.addItem(lab, k)
            cb.setCurrentIndex(max(0, cb.findData(c.props.get("speaker", "streamer"))))
            self.stable.setCellWidget(r, 3, cb)

    def _preview_sub(self, r: int):
        if not (0 <= r < len(getattr(self, "_rows", []))):
            return
        from .media import DecoderPool
        from .render import render_frame_np
        m, c = self._rows[r]
        seq = m["seq"]
        sc = min(1.0, 340 / seq.width, 420 / seq.height)
        arr = np.ascontiguousarray(render_frame_np(seq, c.start + c.duration // 2, self.mw.project, DecoderPool(), sc))
        img = QImage(arr.data, arr.shape[1], arr.shape[0], arr.shape[1] * 3, QImage.Format.Format_RGB888).copy()
        self.pv.setPixmap(QPixmap.fromImage(img))

    def apply_subs(self, wait: bool = False):
        self.mw.begin_edit()
        for r, (m, c) in enumerate(self._rows):
            show = self.stable.item(r, 0).checkState() == Qt.CheckState.Checked
            text = self.stable.item(r, 2).text()
            role = self.stable.cellWidget(r, 3).currentData()
            c.enabled = show and bool(text.strip())
            c.props["text"] = ("💬 " + text if role == "comment" and not text.startswith("💬") else text)
            c.props["speaker"] = role
            c.props["style"] = AC.role_style(role, vertical=m["vertical"])
        self.mw.end_edit("字幕決定")
        self._after_subs(wait)

    # ─────────────── 4. 音量・書き出し ───────────────
    def _after_subs(self, wait: bool = False):
        if self.do_loud.isChecked():
            import copy
            proj = copy.deepcopy(self.mw.project)
            ids = [m["seq"].id for m in self.made]

            paths = self._audio_paths()

            def work(task):
                from .youtube import measure_loudness
                _ensure_audio(paths, task)
                out = {}
                for i, sid in enumerate(ids):
                    task.progress.emit(i, len(ids), "音量を測っています")
                    out[sid] = measure_loudness(proj.sequence_by_id(sid), proj)
                return out
            if wait:
                self._loud_done(work(_DummyTask()), wait)
            else:
                self._start("音量をそろえています…", work, self._loud_done)
        else:
            self._export(wait)

    def _loud_done(self, res, wait=False):
        self.mw.begin_edit()
        for m in self.made:
            lufs = res.get(m["seq"].id)
            if lufs is not None:
                m["seq"].master_db = round(max(-30.0, min(20.0, -14.0 - lufs)), 1)
        self.mw.end_edit("音量をそろえる")
        self._export(wait)

    def out_dir(self) -> str:
        d = self.outdir.text().strip() or os.path.dirname(self.inp.text().strip().strip('"'))
        os.makedirs(d, exist_ok=True)
        return d

    def _export(self, wait: bool = False):
        from . import mediacodecs as MC
        pid = self.fmt.currentData()
        ext = MC.BY_ID[pid].ext
        d = self.out_dir()
        files = []
        for m in self.made:
            seq = m["seq"]
            stem = "".join(ch for ch in seq.name if ch not in '\\/:*?"<>|')
            m["out"] = os.path.join(d, f"{stem}{'_縦' if m['vertical'] else ''}{ext}")
            files.append(m["out"])
            if self.do_xml.isChecked():
                self._write_xml(seq, os.path.join(d, stem))
        if self.farm.isChecked():
            cur = self.mw.project.current
            try:
                for m in self.made:
                    self.mw.project.current = self.mw.project.sequences.index(m["seq"])
                    self.mw.farm_submit_export(0, m["seq"].duration, pid, m["out"], name=m["seq"].name)
            finally:
                self.mw.project.current = cur
            self._finish(files, "ファームに送りました（終わると上の場所に保存されます）")
            return
        import copy
        proj = copy.deepcopy(self.mw.project)
        jobs = [(m["seq"].id, m["out"]) for m in self.made]
        paths = self._audio_paths()

        def work(task):
            from . import exportcore as EC
            _ensure_audio(paths, task)
            for i, (sid, path) in enumerate(jobs):
                proj.current = next(k for k, s in enumerate(proj.sequences) if s.id == sid)
                sq = proj.sequence
                EC.render_to_file(proj, 0, sq.duration, path, pid,
                                  progress=lambda a, n, i=i: task.progress.emit(a, n, f"書き出し {i + 1} / {len(jobs)}"))
            return [p for _s, p in jobs]
        if wait:
            self._finish(work(_DummyTask()))
        else:
            self._start("書き出し中…", work, self._finish)

    def _audio_paths(self) -> List[str]:
        ids = {c.media_id for m in self.made for t in m["seq"].audio_tracks for c in t.clips if c.media_id}
        return [x.path for x in self.mw.project.media if x.id in ids]

    def _write_xml(self, seq, base: str):
        from . import interchange as IX
        try:
            IX.export_fcpxml(self.mw.project, seq, base + ".fcpxml")
        except Exception:
            pass
        try:
            rows = sorted((c for t in seq.video_tracks for c in t.clips if c.kind == "text" and c.enabled), key=lambda c: c.start)

            def srt(f):
                s = f / seq.fps
                return f"{int(s // 3600):02d}:{int(s // 60 % 60):02d}:{int(s % 60):02d},{int(round(s % 1 * 1000)) % 1000:03d}"
            with open(base + ".srt", "w", encoding="utf-8") as fp:
                for i, c in enumerate(rows, 1):
                    fp.write(f"{i}\n{srt(c.start)} --> {srt(c.end)}\n{str(c.prop('text', '')).replace('💬 ', '')}\n\n")
        except Exception:
            pass

    def _page_done(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.done_list = QListWidget()
        self.done_list.itemDoubleClicked.connect(lambda it: os.startfile(os.path.dirname(it.text())) if os.name == "nt" else None)
        v.addWidget(QLabel("できたもの（ダブルクリックでフォルダを開く）。シーケンスはビンにあるので、タイムラインで細かく直せます"))
        v.addWidget(self.done_list, 1)
        h = QHBoxLayout()
        again = QPushButton("別の動画で続ける")
        again.clicked.connect(lambda: self._go(0))
        close = QPushButton("閉じる")
        close.clicked.connect(self.accept)
        h.addStretch(1)
        h.addWidget(again)
        h.addWidget(close)
        v.addLayout(h)
        return w

    def _finish(self, files, note: str = "書き出しました"):
        self.done_list.clear()
        for f in files:
            self.done_list.addItem(f)
        self.msg.setText(f"{note}（{len(files)} 本）")
        if self.made:
            self.mw.load_sequence(self.made[0]["seq"].id)
        self._go(3)


def _ensure_audio(paths: List[str], task=None):
    """音声のキャッシュ（ミックス・音声認識・書き出しで使う）ができるまで待つ。読み込んだばかりの素材は裏で作っている途中のため。"""
    from . import media as MD
    for p in paths:
        if MD.AudioSource(p).data is None:
            if task is not None:
                task.progress.emit(0, 0, f"音声を準備しています: {Path(p).name}")
            MD.build_audio_cache(p)


class _DummyTask:
    cancelled = False

    class progress:
        @staticmethod
        def emit(*_a):
            pass


def _tc(sec: float) -> str:
    s = int(sec or 0)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def TRANSCRIBE(wav: str, model: str, lang: str) -> list:
    """音声認識（テストで差し替えられるように関数にしておく）。"""
    from core import transcribe as T
    return T.transcribe(wav, model, lang, False, True, None)


class AutoClipMixin:
    def _autoclip_commands(self) -> dict:
        return {"auto_clip": self.auto_clip_dialog}

    def auto_clip_dialog(self):
        dlg = AutoClipDialog(self)
        dlg.show()
        self._autoclip_dlg = dlg
        return dlg
