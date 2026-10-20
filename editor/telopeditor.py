"""テロップエディター（DavinTelop を TeloCut に統合した画面）。

DavinTelop の主な機能を Qt で作り直し、タイムラインのテキストクリップを直接編集する:
- テロップ一覧（No・開始・終了・長さ・話者・テキスト。表の中で直接編集。重なり・速すぎを色で警告）
- 直接入力（開始 TC は再生ヘッド / 前のテロップの後ろを自動入力、Enter で追加して次へ）
- デザイン（フォント・色・縁取り・影・背景・位置・字間 / 行間）。共通デザイン or 選択テロップだけ（個別スタイル）
- プリセット（DavinTelop と同じ保存場所 ~/.davintelop/presets を共有）
- テキスト整形（フィラー除去・自動改行・全角 / 半角・句読点・括弧）、検索と置換、分割・結合・詰める
- 読み込み（.dtproj / SRT / CSV）・書き出し（SRT / CSV / PNG 連番 / FCPXML）、文字起こし
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import List, Optional

from PIL import Image
from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtGui import QColor, QImage, QPixmap, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QTableWidget, QTableWidgetItem,
                             QHeaderView, QAbstractItemView, QPushButton, QToolButton, QMenu, QComboBox,
                             QLabel, QLineEdit, QPlainTextEdit, QDoubleSpinBox, QSpinBox, QCheckBox,
                             QSplitter, QScrollArea, QGroupBox, QInputDialog, QMessageBox, QFileDialog,
                             QDialog, QDialogButtonBox, QColorDialog)

from core.models import DesignSettings, TelopEntry
from core.timecode import frames_to_timecode, normalize_timecode, timecode_to_frames
from . import model as M

COLS = ["No", "開始", "終了", "長さ", "話者", "テキスト"]


class _Color(QPushButton):
    def __init__(self, cb):
        super().__init__()
        self._c = "#FFFFFF"
        self._cb = cb
        self.setFixedWidth(56)
        self.clicked.connect(self._pick)

    def set(self, c):
        self._c = c or "#FFFFFF"
        self.setStyleSheet(f"background: {self._c}; border: 1px solid #555;")

    def value(self):
        return self._c

    def _pick(self):
        c = QColorDialog.getColor(QColor(self._c), self, "色を選択")
        if c.isValid():
            self.set(c.name())
            self._cb()


class TelopEditor(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self._loading = False
        self._dirty = True
        self._editing = False
        self._commit = QTimer(self)
        self._commit.setSingleShot(True)
        self._commit.setInterval(700)
        self._commit.timeout.connect(self._commit_edit)
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.addLayout(self._toolbar())
        split = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(split, 1)
        # 左: 一覧 + 直接入力
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("🔎 テロップを絞り込み")
        self.filter.textChanged.connect(lambda _t: self.refresh(force=True))
        lv.addWidget(self.filter)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        hh = self.table.horizontalHeader()
        for i in range(5):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(True)
        self.table.itemChanged.connect(self._cell_edited)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.cellDoubleClicked.connect(self._jump)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(lambda pos: self._menu().exec(self.table.mapToGlobal(pos)))
        lv.addWidget(self.table, 1)
        lv.addWidget(self._quick_input())
        self.stat = QLabel("")
        self.stat.setObjectName("info")
        lv.addWidget(self.stat)
        split.addWidget(left)
        # 右: プレビュー + デザイン
        right = QScrollArea()
        right.setWidgetResizable(True)
        rw = QWidget()
        self.rv = QVBoxLayout(rw)
        self.rv.setContentsMargins(4, 0, 4, 0)
        self.preview = QLabel()
        self.preview.setMinimumSize(320, 180)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background: #111; border: 1px solid #333;")
        self.rv.addWidget(self.preview)
        self._design_panel()
        right.setWidget(rw)
        split.addWidget(right)
        split.setSizes([620, 380])
        QShortcut(QKeySequence("Delete"), self.table, activated=self.delete_selected,
                  context=Qt.ShortcutContext.WidgetShortcut)

    # ─────────────────────────── 上のボタン ───────────────────────────
    def _toolbar(self):
        h = QHBoxLayout()
        h.addWidget(QLabel("トラック"))
        self.track = QComboBox()
        self.track.setToolTip("テロップを置くビデオトラック（自動 = テキストが一番多いトラック）")
        self.track.currentIndexChanged.connect(lambda _i: self.refresh(force=True))
        h.addWidget(self.track)
        for text, tip, fn in (("＋ 再生ヘッドに追加", "再生ヘッドの位置にテロップを追加", self.add_at_playhead),
                              ("✂ 分割", "選択テロップを文の切れ目で 2 つに分ける", self.split_selected),
                              ("⧉ 結合", "選択テロップを 1 つにまとめる", self.merge_selected),
                              ("⇤ 開始を再生ヘッドへ", "選択テロップの開始を再生ヘッドに（長さはそのまま）", self.place_at_playhead),
                              ("削除", "選択テロップを削除", self.delete_selected)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            h.addWidget(b)
        more = QToolButton()
        more.setText("整形・ファイル ▾")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more.setMenu(self._menu())
        h.addWidget(more)
        h.addStretch(1)
        return h

    def _menu(self) -> QMenu:
        m = QMenu(self)
        f = m.addMenu("テキスト整形（選択 / 無選択ならすべて）")
        from core import textfmt as TF
        for lab, fn in (("フィラー（えー・あのー）を除去", TF.remove_fillers),
                        ("2 行にバランスよく改行", lambda t: TF.balanced_break(t.replace("\n", ""), 2) if len(t) > 14 else t),
                        ("改行を取って 1 行に", TF.join_lines),
                        ("英数字を半角に", TF.to_halfwidth_alnum), ("英数字を全角に", TF.to_fullwidth_alnum),
                        ("半角カナを全角に", TF.halfkana_to_fullkana), ("空白を整える", TF.normalize_spaces),
                        ("句読点を削除", TF.remove_punct), ("文末の「。」を削除", TF.remove_trailing_period),
                        ("「…」をそろえる", TF.normalize_ellipsis),
                        ("「」で囲む", lambda t: TF.add_brackets(t, "「", "」")), ("括弧を外す", TF.strip_brackets)):
            f.addAction(lab, lambda fn=fn, lab=lab: self.transform(fn, lab))
        m.addAction("検索と置換...", self.find_replace)
        m.addSeparator()
        m.addAction("隙間を詰める（次のテロップの開始まで伸ばす）", self.close_gaps)
        m.addAction("重なりを直す（前のテロップを次の開始で切る）", self.fix_overlaps)
        m.addAction("読みやすさチェック（速すぎるテロップを選択）", self.select_fast)
        m.addSeparator()
        m.addAction("テロップを読み込み（.dtproj / SRT / CSV）...", self.mw.import_telops)
        ex = m.addMenu("書き出し")
        ex.addAction("SRT...", lambda: self.export("srt"))
        ex.addAction("CSV...", lambda: self.export("csv"))
        ex.addAction("PNG 連番（透過テロップ画像）...", lambda: self.export("png"))
        ex.addAction("FCPXML（Resolve / Final Cut）...", lambda: self.export("fcpxml"))
        m.addAction("文字起こしでテロップを作る...", self.mw.transcribe_subtitles)
        m.addSeparator()
        m.addAction("DavinTelop（単体版）で開く", self.mw.open_davintelop)
        return m

    # ─────────────────────────── 直接入力 ───────────────────────────
    def _quick_input(self) -> QWidget:
        box = QGroupBox("直接入力（Enter で追加・Shift+Enter で改行）")
        g = QVBoxLayout(box)
        r = QHBoxLayout()
        self.q_start = QLineEdit()
        self.q_start.setPlaceholderText("開始 TC")
        self.q_start.setFixedWidth(110)
        self.q_dur = QDoubleSpinBox()
        self.q_dur.setRange(0.2, 60)
        self.q_dur.setValue(3.0)
        self.q_dur.setSuffix(" 秒")
        self.q_gap = QSpinBox()
        self.q_gap.setRange(0, 120)
        self.q_gap.setSuffix(" f")
        self.q_gap.setToolTip("次のテロップとの間")
        self.q_follow = QCheckBox("再生ヘッドに合わせる")
        self.q_follow.setChecked(True)
        self.q_follow.setToolTip("オン: 開始 TC = 再生ヘッド / オフ: 前に追加したテロップの後ろ")
        ph = QPushButton("⟲")
        ph.setFixedWidth(28)
        ph.setToolTip("開始 TC を再生ヘッドにする")
        ph.clicked.connect(self._fill_start)
        for w in (QLabel("開始"), self.q_start, ph, QLabel("長さ"), self.q_dur, QLabel("間"), self.q_gap, self.q_follow):
            r.addWidget(w)
        r.addStretch(1)
        g.addLayout(r)
        self.q_text = QPlainTextEdit()
        self.q_text.setFixedHeight(54)
        self.q_text.setPlaceholderText("テロップの文字を入力して Enter")
        self.q_text.installEventFilter(self)
        g.addWidget(self.q_text)
        return box

    def eventFilter(self, obj, e):
        if obj is self.q_text and e.type() == e.Type.KeyPress and e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and not e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.quick_add()
            return True
        if obj is self.q_text and e.type() == e.Type.FocusIn and self.q_follow.isChecked():
            self._fill_start()
        return super().eventFilter(obj, e)

    def _fill_start(self):
        self.q_start.setText(self._tc(self.mw.record_player.position))

    def quick_add(self):
        text = self.q_text.toPlainText().strip()
        if not text:
            return
        seq = self.mw.project.sequence
        if self.q_follow.isChecked() or not self.q_start.text().strip():
            a = self.mw.record_player.position
        else:
            a = self._frames(self.q_start.text())
            if a is None:
                QMessageBox.warning(self, "テロップ", "開始 TC が読めません（例 01:00:05:10）")
                return
        b = a + max(1, int(round(self.q_dur.value() * seq.fps)))
        c = self._add(a, b, text, "テロップを追加")
        nxt = b + self.q_gap.value()
        self.q_text.clear()
        self.q_start.setText(self._tc(nxt))
        if self.q_follow.isChecked():
            self.mw.seek_record(nxt)
        self.select_clips([c.id])

    # ─────────────────────────── データ ───────────────────────────
    def _tc(self, f: int) -> str:
        seq = self.mw.project.sequence
        return frames_to_timecode(f + self.mw.tc_offset(seq), seq.fps)

    def _frames(self, s: str) -> Optional[int]:
        seq = self.mw.project.sequence
        try:
            return timecode_to_frames(normalize_timecode(s.strip(), seq.fps), seq.fps) - self.mw.tc_offset(seq)
        except (ValueError, AttributeError):
            return None

    def target_track(self) -> M.Track:
        seq = self.mw.project.sequence
        i = self.track.currentData()
        if isinstance(i, int) and 0 <= i < len(seq.video_tracks):
            return seq.video_tracks[i]
        best = max(seq.video_tracks, key=lambda t: sum(1 for c in t.clips if c.kind == "text"))
        if not any(c.kind == "text" for c in best.clips):
            # テキストが無い → 一番上の空きトラック、無ければ追加
            best = next((t for t in reversed(seq.video_tracks) if not t.clips), None)
            if best is None:
                best = M.Track("video", f"V{len(seq.video_tracks) + 1}")
                seq.video_tracks.append(best)
        return best

    def telop_clips(self) -> List[M.Clip]:
        return sorted((c for c in self.target_track().clips if c.kind == "text"), key=lambda c: c.start)

    def _add(self, a: int, b: int, text: str, label: str, style: Optional[dict] = None) -> M.Clip:
        self.mw.begin_edit()
        t = self.target_track()
        c = M.Clip(M.new_id(), "text", a, max(1, b - a), name="テロップ",
                   props={"text": text, "style": dict(style or {})})
        M.place_clips(self.mw.project.sequence, [(t, c)])
        self.mw.end_edit(label)
        return c

    def selected_clips(self) -> List[M.Clip]:
        seq = self.mw.project.sequence
        ids = []
        for r in sorted({i.row() for i in self.table.selectedIndexes()}):
            it = self.table.item(r, 0)
            if it is not None:
                ids.append(it.data(Qt.ItemDataRole.UserRole))
        out = []
        for cid in ids:
            _t, c = seq.find(cid)
            if c is not None:
                out.append(c)
        return out

    def select_clips(self, ids):
        from PyQt6.QtCore import QItemSelectionModel
        ids = set(ids)
        self.table.clearSelection()
        sm = self.table.selectionModel()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) in ids:
                sm.select(self.table.model().index(r, 0), flags)      # selectRow だと前の選択が消える
                self.table.scrollToItem(self.table.item(r, 0))

    # ─────────────────────────── 一覧 ───────────────────────────
    def showEvent(self, e):
        super().showEvent(e)
        if self._dirty:
            self.refresh(force=True)

    def refresh(self, force: bool = False):
        if not force and not self.isVisible():
            self._dirty = True
            return
        if self._editing:
            return
        self._dirty = False
        self._loading = True
        try:
            seq = self.mw.project.sequence
            cur = self.track.currentData()
            self.track.blockSignals(True)
            self.track.clear()
            self.track.addItem("自動", -1)
            for i, t in enumerate(seq.video_tracks):
                n = sum(1 for c in t.clips if c.kind == "text")
                self.track.addItem(f"{t.name}（{n}）", i)
            self.track.setCurrentIndex(max(0, self.track.findData(cur)))
            self.track.blockSignals(False)
            sel = {c.id for c in self.selected_clips()}
            clips = self.telop_clips()
            q = self.filter.text().strip().lower()
            if q:
                clips = [c for c in clips if q in str(c.prop("text", "")).lower()]
            self.table.setRowCount(len(clips))
            fps = seq.fps
            prev_end = None
            fast = overlap = 0
            for r, c in enumerate(clips):
                txt = str(c.prop("text", ""))
                cps = len(txt.replace("\n", "")) / max(1e-6, c.duration / fps)
                cells = [str(r + 1), self._tc(c.start), self._tc(c.end), f"{c.duration / fps:.1f}",
                         str(c.prop("speaker", "") or ""), txt]
                for k, v in enumerate(cells):
                    it = QTableWidgetItem(v)
                    if k in (0, 3):
                        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if k == 0:
                        it.setData(Qt.ItemDataRole.UserRole, c.id)
                    self.table.setItem(r, k, it)
                warn = None
                if prev_end is not None and c.start < prev_end:
                    warn, overlap = QColor(130, 40, 40), overlap + 1
                elif cps > 8:
                    warn, fast = QColor(120, 85, 20), fast + 1
                if warn is not None:
                    for k in range(len(COLS)):
                        self.table.item(r, k).setBackground(warn)
                prev_end = max(prev_end or 0, c.end)
            self.table.resizeRowsToContents()
            if sel:
                self.select_clips(sel)
            msg = f"{len(clips)} 件"
            if overlap:
                msg += f"　重なり {overlap}（赤）"
            if fast:
                msg += f"　速すぎ {fast}（橙・1 秒 8 文字超）"
            self.stat.setText(msg)
        finally:
            self._loading = False
        self._load_design()

    def _cell_edited(self, it: QTableWidgetItem):
        if self._loading:
            return
        r, k = it.row(), it.column()
        cid = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
        seq = self.mw.project.sequence
        t, c = seq.find(cid)
        if c is None:
            return
        val = it.text()
        self.mw.begin_edit()
        if k in (1, 2):
            f = self._frames(val)
            if f is None:
                self.mw.end_edit(None)
                QMessageBox.warning(self, "テロップ", f"タイムコードが読めません: {val}")
                self.refresh(force=True)
                return
            a, b = (f, c.end) if k == 1 else (c.start, f)
            if b <= a:
                b = a + 1 if k == 1 else b
                if k == 2:
                    self.mw.end_edit(None)
                    self.refresh(force=True)
                    return
            c.start, c.duration = a, b - a
            t.sort()
        elif k == 4:
            c.props["speaker"] = val.strip()
        elif k == 5:
            c.props["text"] = val
        self.mw.end_edit("テロップを編集")

    def _selection_changed(self):
        if self._loading:
            return
        clips = self.selected_clips()
        if clips:
            self.mw.canvas.selected = {c.id for c in clips}
            self.mw.canvas.update()
        self._load_design()

    def _jump(self, r, _c):
        it = self.table.item(r, 0)
        _t, c = self.mw.project.sequence.find(it.data(Qt.ItemDataRole.UserRole))
        if c is not None:
            self.mw.seek_record(c.start)

    # ─────────────────────────── 編集 ───────────────────────────
    def add_at_playhead(self):
        a = self.mw.record_player.position
        c = self._add(a, a + int(round(self.q_dur.value() * self.mw.project.sequence.fps)), "テロップ", "テロップを追加")
        self.refresh(force=True)
        self.select_clips([c.id])
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == c.id:
                self.table.editItem(self.table.item(r, 5))

    def delete_selected(self):
        clips = self.selected_clips()
        if not clips:
            return
        self.mw.begin_edit()
        M.delete_clips(self.mw.project.sequence, {c.id for c in clips}, ripple=False)
        self.mw.end_edit(f"テロップを削除（{len(clips)}）")

    def split_selected(self):
        from core.textfmt import balanced_break
        clips = self.selected_clips()
        if not clips:
            return
        seq = self.mw.project.sequence
        self.mw.begin_edit()
        for c in clips:
            txt = str(c.prop("text", ""))
            parts = [p for p in txt.split("\n") if p.strip()]
            if len(parts) < 2:
                parts = [p for p in balanced_break(txt, 2).split("\n") if p.strip()]
            if len(parts) < 2 or c.duration < 2:
                continue
            head = "\n".join(parts[:len(parts) // 2])
            tail = "\n".join(parts[len(parts) // 2:])
            cut = c.start + max(1, min(c.duration - 1, int(c.duration * len(head) / max(1, len(head) + len(tail)))))
            t, _ = seq.find(c.id)
            new = c.copy(id=M.new_id(), start=cut, duration=c.end - cut)
            new.props["text"] = tail
            c.duration = cut - c.start
            c.props["text"] = head
            t.clips.append(new)
            t.sort()
        self.mw.end_edit("テロップを分割")

    def merge_selected(self):
        clips = sorted(self.selected_clips(), key=lambda c: c.start)
        if len(clips) < 2:
            return
        seq = self.mw.project.sequence
        self.mw.begin_edit()
        first = clips[0]
        end = max(c.end for c in clips)
        first.props["text"] = "\n".join(str(c.prop("text", "")) for c in clips)
        first.duration = end - first.start
        M.delete_clips(seq, {c.id for c in clips[1:]}, ripple=False)
        self.mw.end_edit("テロップを結合")
        self.select_clips([first.id])

    def place_at_playhead(self):
        clips = sorted(self.selected_clips(), key=lambda c: c.start)
        if not clips:
            return
        f = self.mw.record_player.position
        d = f - clips[0].start
        self.mw.begin_edit()
        for c in clips:
            c.start = max(0, c.start + d)
        self.target_track().sort()
        self.mw.end_edit("テロップを再生ヘッドへ")

    def transform(self, fn, label: str):
        clips = self.selected_clips() or self.telop_clips()
        self.mw.begin_edit()
        n = 0
        for c in clips:
            old = str(c.prop("text", ""))
            new = fn(old)
            if new != old:
                c.props["text"] = new
                n += 1
        self.mw.end_edit(f"{label}（{n} 件）")
        self.stat.setText(f"{label}: {n} 件を変更")

    def find_replace(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("検索と置換")
        f = QFormLayout(dlg)
        a, b = QLineEdit(), QLineEdit()
        only = QCheckBox("選択したテロップだけ")
        f.addRow("検索", a)
        f.addRow("置換", b)
        f.addRow(only)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if not dlg.exec() or not a.text():
            return
        src, dst = a.text(), b.text()
        clips = self.selected_clips() if only.isChecked() else self.telop_clips()
        self.mw.begin_edit()
        n = 0
        for c in clips:
            t = str(c.prop("text", ""))
            if src in t:
                n += t.count(src)
                c.props["text"] = t.replace(src, dst)
        self.mw.end_edit(f"置換「{src}」→「{dst}」（{n} か所）")
        self.stat.setText(f"{n} か所を置換しました")

    def close_gaps(self):
        clips = self.telop_clips()
        self.mw.begin_edit()
        for c0, c1 in zip(clips, clips[1:]):
            if c0.end < c1.start:
                c0.duration = c1.start - c0.start
        self.mw.end_edit("テロップの隙間を詰める")

    def fix_overlaps(self):
        clips = self.telop_clips()
        self.mw.begin_edit()
        for c0, c1 in zip(clips, clips[1:]):
            if c0.end > c1.start:
                c0.duration = max(1, c1.start - c0.start)
        self.mw.end_edit("テロップの重なりを直す")

    def select_fast(self):
        fps = self.mw.project.sequence.fps
        ids = [c.id for c in self.telop_clips()
               if len(str(c.prop("text", "")).replace("\n", "")) / max(1e-6, c.duration / fps) > 8]
        self.select_clips(ids)
        self.stat.setText(f"速すぎるテロップ {len(ids)} 件を選択しました（1 秒 8 文字超）")

    # ─────────────────────────── 書き出し ───────────────────────────
    def entries(self) -> List[TelopEntry]:
        return [TelopEntry(i + 1, self._tc(c.start), self._tc(c.end), str(c.prop("text", "")),
                           speaker=str(c.prop("speaker", "") or ""), style=dict(c.prop("style") or {}))
                for i, c in enumerate(self.telop_clips()) if c.enabled]

    def design(self) -> DesignSettings:
        d = DesignSettings.from_dict(self.mw.project.design or {})
        seq = self.mw.project.sequence
        d.canvas_width, d.canvas_height, d.frame_rate = seq.width, seq.height, seq.fps
        return d

    def export(self, kind: str):
        from core import exporter as EX
        ents = self.entries()
        if not ents:
            QMessageBox.information(self, "書き出し", "テロップがありません。")
            return
        seq = self.mw.project.sequence
        try:
            if kind == "png":
                d = QFileDialog.getExistingDirectory(self, "PNG の書き出し先フォルダ")
                if not d:
                    return
                paths = EX.export_png(ents, self.design(), d)
                msg = f"PNG を {len(paths)} 枚書き出しました"
            else:
                ext = {"srt": "SRT (*.srt)", "csv": "CSV (*.csv)", "fcpxml": "FCPXML (*.fcpxml)"}[kind]
                p, _ = QFileDialog.getSaveFileName(self, "書き出し", f"{seq.name}.{kind}", ext)
                if not p:
                    return
                if kind == "srt":
                    EX.export_srt(ents, p, seq.fps)
                elif kind == "csv":
                    EX.export_csv(ents, p)
                else:
                    # FCPXML は透過 PNG を参照するので、先に「<名前>_png」フォルダへ書き出す
                    png_dir = str(Path(p).with_name(Path(p).stem + "_png"))
                    pngs = EX.export_png(ents, self.design(), png_dir)
                    EX.export_fcpxml(ents, pngs, p, self.design())
                msg = f"書き出しました: {p}"
        except Exception as ex:
            QMessageBox.critical(self, "書き出し", str(ex))
            return
        self.stat.setText(msg)

    # ─────────────────────────── デザイン ───────────────────────────
    def _design_panel(self):
        top = QHBoxLayout()
        self.scope = QComboBox()
        self.scope.addItem("共通デザイン（すべてのテロップ）", "common")
        self.scope.addItem("選択したテロップだけ（個別スタイル）", "selected")
        self.scope.currentIndexChanged.connect(lambda _i: self._load_design())
        top.addWidget(self.scope, 1)
        clear = QPushButton("個別スタイル解除")
        clear.clicked.connect(self.clear_individual)
        top.addWidget(clear)
        self.rv.addLayout(top)
        pr = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.setToolTip("DavinTelop と共通のプリセット")
        ap = QPushButton("適用")
        ap.clicked.connect(self.apply_preset)
        sv = QPushButton("保存...")
        sv.clicked.connect(self.save_preset)
        pr.addWidget(QLabel("プリセット"))
        pr.addWidget(self.preset, 1)
        pr.addWidget(ap)
        pr.addWidget(sv)
        self.rv.addLayout(pr)
        self._reload_presets()
        self.w = {}
        from core.renderer import get_system_fonts
        self._fonts = get_system_fonts()

        def grp(title):
            g = QGroupBox(title)
            f = QFormLayout(g)
            self.rv.addWidget(g)
            return f

        def spin(key, lo, hi, dec=0, step=1.0):
            s = QDoubleSpinBox() if dec else QSpinBox()
            s.setRange(lo, hi)
            if dec:
                s.setDecimals(dec)
                s.setSingleStep(step)
            s.setKeyboardTracking(False)
            s.valueChanged.connect(lambda _v, k=key: self._changed(k))
            self.w[key] = s
            return s

        def check(key, lab):
            c = QCheckBox(lab)
            c.toggled.connect(lambda _v, k=key: self._changed(k))
            self.w[key] = c
            return c

        def color(key):
            c = _Color(lambda k=key: self._changed(k))
            self.w[key] = c
            return c

        def combo(key, items):
            c = QComboBox()
            for lab, val in items:
                c.addItem(lab, val)
            c.currentIndexChanged.connect(lambda _i, k=key: self._changed(k))
            self.w[key] = c
            return c

        f = grp("文字")
        fc = QComboBox()
        fc.addItem("（既定の日本語フォント）", "")
        for name, path in self._fonts:
            fc.addItem(name, path)
        fc.currentIndexChanged.connect(lambda _i: self._changed("font_path"))
        self.w["font_path"] = fc
        f.addRow("フォント", fc)
        r = QHBoxLayout()
        r.addWidget(spin("font_size", 8, 400))
        r.addWidget(color("font_color"))
        r.addWidget(check("bold", "太字"))
        f.addRow("大きさ / 色", r)
        r = QHBoxLayout()
        r.addWidget(QLabel("字間"))
        r.addWidget(spin("letter_spacing", -50, 200))
        r.addWidget(QLabel("行間"))
        r.addWidget(spin("line_spacing", 0.5, 3.0, 2, 0.05))
        f.addRow(r)
        f = grp("縁取り・影・背景")
        r = QHBoxLayout()
        r.addWidget(check("stroke_enabled", "縁取り"))
        r.addWidget(color("stroke_color"))
        r.addWidget(spin("stroke_width", 0, 60))
        f.addRow(r)
        r = QHBoxLayout()
        r.addWidget(check("shadow_enabled", "影"))
        r.addWidget(color("shadow_color"))
        r.addWidget(QLabel("濃さ"))
        r.addWidget(spin("shadow_opacity", 0, 255))
        f.addRow(r)
        r = QHBoxLayout()
        r.addWidget(QLabel("影の位置 X"))
        r.addWidget(spin("shadow_offset_x", -100, 100))
        r.addWidget(QLabel("Y"))
        r.addWidget(spin("shadow_offset_y", -100, 100))
        f.addRow(r)
        r = QHBoxLayout()
        r.addWidget(check("bg_enabled", "背景"))
        r.addWidget(color("bg_color"))
        r.addWidget(QLabel("濃さ"))
        r.addWidget(spin("bg_opacity", 0, 255))
        r.addWidget(QLabel("余白"))
        r.addWidget(spin("bg_padding", 0, 200))
        f.addRow(r)
        f = grp("位置")
        r = QHBoxLayout()
        r.addWidget(combo("position_v", [("下", "bottom"), ("中央", "middle"), ("上", "top")]))
        r.addWidget(combo("position_h", [("中央", "center"), ("左", "left"), ("右", "right")]))
        r.addWidget(QLabel("揃え"))
        r.addWidget(combo("text_align", [("中央", "center"), ("左", "left"), ("右", "right")]))
        f.addRow("配置", r)
        r = QHBoxLayout()
        r.addWidget(QLabel("X"))
        r.addWidget(spin("offset_x", -2000, 2000))
        r.addWidget(QLabel("Y（端から）"))
        r.addWidget(spin("offset_y", -2000, 2000))
        f.addRow(r)
        self.rv.addStretch(1)

    def _reload_presets(self):
        from core.preset import PresetManager
        self.preset.clear()
        try:
            self.preset.addItems(PresetManager().list_presets())
        except Exception:
            pass

    def _effective(self) -> DesignSettings:
        d = DesignSettings.from_dict(self.mw.project.design or {})
        if self.scope.currentData() == "selected":
            sel = self.selected_clips()
            if sel:
                d = d.with_overrides(sel[0].prop("style") or {})
        return d

    def _load_design(self):
        if not hasattr(self, "w"):
            return
        d = self._effective()
        self._loading = True
        try:
            for k, wdg in self.w.items():
                v = getattr(d, k)
                if k == "font_path":
                    i = wdg.findData(v)
                    if i < 0 and v:
                        wdg.addItem(Path(v).stem, v)
                        i = wdg.count() - 1
                    wdg.setCurrentIndex(max(0, i))
                elif isinstance(wdg, QCheckBox):
                    wdg.setChecked(bool(v))
                elif isinstance(wdg, _Color):
                    wdg.set(v)
                elif isinstance(wdg, QComboBox):
                    wdg.setCurrentIndex(max(0, wdg.findData(v)))
                else:
                    wdg.setValue(v)
        finally:
            self._loading = False
        self._update_preview()

    def _value(self, k):
        wdg = self.w[k]
        if isinstance(wdg, QCheckBox):
            return wdg.isChecked()
        if isinstance(wdg, _Color):
            return wdg.value()
        if isinstance(wdg, QComboBox):
            return wdg.currentData()
        return wdg.value()

    def _changed(self, key: str):
        if self._loading:
            return
        v = self._value(key)
        if not self._editing:
            self.mw.begin_edit()
            self._editing = True
        if self.scope.currentData() == "selected":
            for c in self.selected_clips():
                c.props["style"] = {**(c.prop("style") or {}), key: v}
        else:
            d = dict(self.mw.project.design or {})
            d[key] = v
            self.mw.project.design = d
        self.mw.preview_changed()
        self._update_preview()
        self._commit.start()

    def _commit_edit(self):
        if self._editing:
            self._editing = False
            self.mw.end_edit("テロップのデザインを変更", reload_inspector=False)

    def clear_individual(self):
        clips = self.selected_clips()
        if not clips:
            return
        self.mw.begin_edit()
        for c in clips:
            c.props["style"] = {}
        self.mw.end_edit("個別スタイルを解除")

    def apply_preset(self):
        from core.preset import PresetManager
        name = self.preset.currentText()
        if not name:
            return
        try:
            d = PresetManager().load_preset(name).to_dict()
        except Exception as ex:
            QMessageBox.warning(self, "プリセット", str(ex))
            return
        for k in ("canvas_width", "canvas_height", "frame_rate"):
            d.pop(k, None)
        self.mw.begin_edit()
        if self.scope.currentData() == "selected":
            for c in self.selected_clips():
                c.props["style"] = dict(d)
        else:
            self.mw.project.design = {**(self.mw.project.design or {}), **d}
        self.mw.end_edit(f"プリセット「{name}」を適用")

    def save_preset(self):
        from core.preset import PresetManager
        name, ok = QInputDialog.getText(self, "プリセットを保存", "名前（DavinTelop でも使えます）:")
        if not ok or not name.strip():
            return
        PresetManager().save_preset(name.strip(), self._effective())
        self._reload_presets()
        self.preset.setCurrentText(name.strip())
        self.mw.effects.reload() if hasattr(self.mw, "effects") else None

    def _update_preview(self):
        from core.renderer import render_telop_image
        sel = self.selected_clips()
        txt = str(sel[0].prop("text", "")) if sel else (self.q_text.toPlainText() or "テロップのプレビュー")
        d = DesignSettings.from_dict(self.mw.project.design or {})
        if sel:
            d = d.with_overrides(sel[0].prop("style") or {})
        seq = self.mw.project.sequence
        d.canvas_width, d.canvas_height = seq.width, seq.height
        try:
            im = render_telop_image(txt or " ", d)
        except Exception:
            return
        bg = Image.new("RGBA", im.size, (40, 44, 52, 255))
        tile = max(8, im.width // 48)
        from PIL import ImageDraw
        dr = ImageDraw.Draw(bg)
        for y in range(0, im.height, tile):
            for x in range((y // tile) % 2 * tile, im.width, tile * 2):
                dr.rectangle([x, y, x + tile - 1, y + tile - 1], fill=(52, 56, 64, 255))
        bg.alpha_composite(im)
        w = max(160, self.preview.width() - 4)
        h = int(w * bg.height / bg.width)
        small = bg.convert("RGB").resize((w, h), Image.BILINEAR)
        data = small.tobytes("raw", "RGB")
        self.preview.setPixmap(QPixmap.fromImage(QImage(data, w, h, w * 3, QImage.Format.Format_RGB888).copy()))
