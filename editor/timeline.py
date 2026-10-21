"""タイムライン（Avid 風の見た目と操作 / Premiere 風のツールも併用可）。"""
from __future__ import annotations

import copy
import json
from typing import List, Optional, Tuple

from PyQt6.QtCore import Qt, QRect, QRectF, QPoint, QPointF, pyqtSignal, QMimeData, QTimer
from PyQt6.QtGui import (QPainter, QColor, QPen, QFont, QFontMetrics, QPolygonF, QBrush,
                         QCursor, QAction, QLinearGradient)
from PyQt6.QtWidgets import (QWidget, QScrollBar, QVBoxLayout, QHBoxLayout, QToolButton,
                             QLabel, QMenu, QSlider, QInputDialog, QButtonGroup, QSizePolicy)

from core.timecode import frames_to_timecode
from . import media as MD
from . import model as M
from .theme import C

MIME_MEDIA = "application/x-davinedit-media"
MIME_GEN = "application/x-davinedit-gen"
MIME_FX = "application/x-davinedit-fx"

HW = 206          # トラックヘッダー幅（ソーストラック / レコードトラック / L M S ⇅）
RH = 30           # ルーラー高さ
EDGE = 7          # トリム判定幅

TOOLS = [
    ("select", "▲", "赤セグメント / 選択ツール\nドラッグで上書き移動（元は隙間）、端をドラッグでトリム、\nAlt+ドラッグで複製、Ctrl+ドロップで挿入"),
    ("yellow", "▲", "黄セグメント（抽出 / スプライスイン）\nドラッグで元を詰めて挿入、端をドラッグでリップルトリム"),
    ("ripple", "⇤", "リップル編集ツール (B)\n端をトリムすると後ろが追従"),
    ("roll", "⇹", "ローリング編集ツール (N)\n隣り合う2クリップの編集点を移動"),
    ("slip", "↹", "スリップツール (Y)\n位置と長さはそのまま、使う素材範囲をずらす"),
    ("slide", "⇆", "スライドツール (U)\nクリップを動かし、前後のクリップで吸収"),
    ("razor", "✂", "レーザーツール (C)\nクリックで分割（Shift で全トラック）"),
    ("hand", "✋", "手のひらツール (H)"),
    ("zoom", "⊕", "ズームツール (Z)\nクリックで拡大、Alt+クリックで縮小"),
]


class TimelineCanvas(QWidget):
    selection_changed = pyqtSignal()
    zoom_changed = pyqtSignal()

    def __init__(self, mw, parent=None):
        super().__init__(parent)
        self.mw = mw
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(120)
        self.ppf = 2.0             # pixels per frame
        self.x0 = 0.0              # 左端のフレーム
        self.y0 = 0                # 縦スクロール(px)
        self.tool = "select"
        self.snap = True
        self.linked_selection = True
        self.selected: set = set()
        self._drag = None
        self._snap_x = None
        self._ghost = []           # ドラッグ中の配置予定 [(row_index, start, dur, color)]
        self._rows = []
        self._hover = None
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(30)
        self._scroll_timer.timeout.connect(self._auto_scroll)

    # ── 便利 ─────────────────────────────────
    @property
    def seq(self) -> M.Sequence:
        return self.mw.project.sequence

    def fx(self, frame: float) -> float:
        return HW + (frame - self.x0) * self.ppf

    def frame_at(self, x: float) -> int:
        return int(round(self.x0 + (x - HW) / self.ppf))

    def _layout(self):
        rows = []
        y = RH - self.y0
        seq = self.seq
        for i in range(len(seq.video_tracks) - 1, -1, -1):
            t = seq.video_tracks[i]
            h = t.height or 46
            rows.append((t, "video", i, y, h))
            y += h
        y += 6      # 映像と音声の区切り
        for i, t in enumerate(seq.audio_tracks):
            h = t.height or 40
            rows.append((t, "audio", i, y, h))
            y += h
        self._rows = rows
        self._content_h = y + self.y0 - RH
        return rows

    def row_at(self, y: float):
        for r in self._rows:
            if r[3] <= y < r[3] + r[4]:
                return r
        return None

    def row_index(self, track) -> int:
        for i, r in enumerate(self._rows):
            if r[0] is track:
                return i
        return -1

    def clip_rect(self, row, c: M.Clip) -> QRectF:
        _, _, _, y, h = row
        return QRectF(self.fx(c.start), y + 1, max(1.0, c.duration * self.ppf), h - 2)

    def hit(self, pos) -> Tuple[Optional[tuple], Optional[M.Clip], str]:
        """(row, clip, part) part = body / in / out / ''"""
        row = self.row_at(pos.y())
        if row is None or pos.x() < HW:
            return row, None, ""
        f = self.x0 + (pos.x() - HW) / self.ppf
        t = row[0]
        for c in t.clips:
            r = self.clip_rect(row, c)
            if r.left() - 2 <= pos.x() <= r.right() + 2:
                if r.width() > EDGE * 2.5:
                    if pos.x() - r.left() <= EDGE:
                        return row, c, "in"
                    if r.right() - pos.x() <= EDGE:
                        return row, c, "out"
                if c.start <= f < c.end or r.width() < 4:
                    return row, c, "body"
        return row, None, ""

    def select_clip(self, c: M.Clip, add: bool = False, toggle: bool = False, linked: bool = True):
        ids = {c.id}
        if linked and self.linked_selection:
            ids |= {x.id for _, x in self.seq.linked(c)}
        if toggle:
            if c.id in self.selected:
                self.selected -= ids
            else:
                self.selected |= ids
        elif add:
            self.selected |= ids
        else:
            self.selected = ids
        self.selection_changed.emit()
        self.update()

    def selected_clips(self) -> List[Tuple[M.Track, M.Clip]]:
        return [(t, c) for t, c in self.seq.all_clips() if c.id in self.selected]

    def snap_points(self, exclude=(), playhead: bool = True) -> List[int]:
        pts = {self.mw.record_player.position} if playhead else set()
        seq = self.seq
        for t, c in seq.all_clips():
            if c.id in exclude:
                continue
            pts.add(c.start)
            pts.add(c.end)
        for m in seq.markers:
            pts.add(m.frame)
        if seq.mark_in >= 0:
            pts.add(seq.mark_in)
        if seq.mark_out >= 0:
            pts.add(seq.mark_out)
        return sorted(pts)

    def do_snap(self, frame: int, exclude=(), shift=False, force=False, playhead=True) -> int:
        """近くの編集点に吸着（Shift で一時的に反転、force=True なら常に吸着）。"""
        if not force and self.snap == bool(shift):
            self._snap_x = None
            return frame
        tol = 10 / self.ppf
        best, bd = frame, tol + 1
        for p in self.snap_points(exclude, playhead):
            d = abs(p - frame)
            if d < bd:
                best, bd = p, d
        self._snap_x = self.fx(best) if bd <= tol else None
        return best if bd <= tol else frame

    # ── ズーム / スクロール ─────────────────
    def zoom_at(self, factor: float, x: Optional[float] = None):
        if x is None:
            x = self.fx(self.mw.record_player.position)
            if not (HW <= x <= self.width()):
                x = HW + (self.width() - HW) / 2
        f = self.x0 + (x - HW) / self.ppf
        self.ppf = max(0.01, min(60.0, self.ppf * factor))
        self.x0 = max(0.0, f - (x - HW) / self.ppf)
        self.zoom_changed.emit()
        self.update()

    def zoom_fit(self):
        d = max(self.seq.duration, int(self.seq.fps * 10))
        self.ppf = max(0.01, (self.width() - HW - 30) / d)
        self.x0 = 0
        self.zoom_changed.emit()
        self.update()

    def ensure_visible(self, frame: int):
        x = self.fx(frame)
        if x < HW + 20 or x > self.width() - 20:
            self.x0 = max(0.0, frame - (self.width() - HW) * 0.3 / self.ppf)
            self.zoom_changed.emit()
            self.update()

    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        mods = e.modifiers()
        if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            self.zoom_at(1.25 if dy > 0 else 0.8, e.position().x())
        elif mods & Qt.KeyboardModifier.ShiftModifier or e.angleDelta().x():
            d = dy or e.angleDelta().x()
            self.x0 = max(0.0, self.x0 - d / self.ppf * 0.5)
            self.zoom_changed.emit()
            self.update()
        else:
            self.y0 = max(0, min(max(0, self._content_h - (self.height() - RH)), self.y0 - dy // 2))
            self.zoom_changed.emit()
            self.update()

    # ── 描画 ────────────────────────────────
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), QColor(C["bg"]))
        rows = self._layout()
        seq = self.seq
        fps = seq.fps
        small = QFont(self.font())
        small.setPointSizeF(8)
        p.setFont(small)
        fm = QFontMetrics(small)

        # IN-OUT 範囲
        if seq.mark_in >= 0 or seq.mark_out >= 0:
            a = self.fx(max(0, seq.mark_in))
            b = self.fx(seq.mark_out) if seq.mark_out >= 0 else W
            p.fillRect(QRectF(max(HW, a), RH, max(0, b - max(HW, a)), H - RH),
                       QColor(60, 75, 130, 70))

        # トラック背景・クリップ
        sel_pen = QPen(QColor(C["selected"]), 2)
        for row in rows:
            t, kind, idx, y, h = row
            if y + h < RH or y > H:
                continue
            bg = QColor(C["track_v"] if kind == "video" else C["track_a"])
            if t.locked:
                bg = bg.darker(140)
            p.fillRect(QRect(HW, y, W - HW, h - 1), bg)
            p.fillRect(QRect(HW, y + h - 1, W - HW, 1), QColor(C["bg"]))
            f0 = self.frame_at(HW)
            f1 = self.frame_at(W)
            for c in t.clips:
                if c.end < f0 or c.start > f1:
                    continue
                self._paint_clip(p, row, c, c.id in self.selected, fm, sel_pen)
            if t.locked:
                p.fillRect(QRect(HW, y, W - HW, h - 1), QBrush(QColor(0, 0, 0, 60), Qt.BrushStyle.BDiagPattern))

        # ゴースト（移動・ドロップの予定位置）
        for ri, st, du, col in self._ghost:
            if 0 <= ri < len(rows):
                _, _, _, y, h = rows[ri]
                r = QRectF(self.fx(st), y + 1, du * self.ppf, h - 2)
                p.fillRect(r, QColor(255, 255, 255, 40))
                p.setPen(QPen(QColor(col), 2, Qt.PenStyle.DashLine))
                p.drawRect(r)

        # ルーラー
        p.fillRect(QRect(0, 0, W, RH), QColor(C["panel2"]))
        p.setPen(QColor(C["border"]))
        p.drawLine(0, RH - 1, W, RH - 1)
        steps = [1, 2, 5, 10, 15, int(round(fps)), int(round(fps)) * 2, int(round(fps)) * 5,
                 int(round(fps)) * 10, int(round(fps)) * 30, int(round(fps)) * 60,
                 int(round(fps)) * 300, int(round(fps)) * 600]
        step = next((s for s in steps if s * self.ppf >= 90), steps[-1])
        sub = max(1, step // 5)
        f = int(self.x0 // sub) * sub
        offset = self.mw.tc_offset(seq)
        while self.fx(f) <= W:
            x = self.fx(f)
            if x >= HW:
                if f % step == 0:
                    p.setPen(QColor(C["dim"]))
                    p.drawLine(int(x), RH - 12, int(x), RH - 1)
                    p.setPen(QColor(C["text"]))
                    p.drawText(int(x) + 3, 12, frames_to_timecode(f + offset, fps))
                else:
                    p.setPen(QColor(C["border"]))
                    p.drawLine(int(x), RH - 5, int(x), RH - 1)
            f += sub
        # レンダーバー（赤=要レンダリング / 黄=リアルタイム可 / 緑=レンダリング済み）
        colors = {"red": QColor("#d8463c"), "yellow": QColor("#d6b23a"), "done": QColor("#3fae5a")}
        for a, b, st in getattr(self.mw, "render_status", []):
            x0, x1 = self.fx(a), self.fx(b)
            if x1 < HW or x0 > W:
                continue
            p.fillRect(QRectF(max(HW, x0), RH - 5, max(1.0, min(W, x1) - max(HW, x0)), 4), colors[st])
        # マーカー
        for m in seq.markers:
            x = self.fx(m.frame)
            if x >= HW:
                p.setBrush(QColor(m.color))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawPolygon(QPolygonF([QPointF(x - 5, RH - 12), QPointF(x + 5, RH - 12),
                                         QPointF(x, RH - 4)]))
                if m.name:
                    p.setPen(QColor(m.color))
                    p.drawText(int(x) + 6, RH - 4, m.name)
        # IN/OUT 記号
        for fr, ch in ((seq.mark_in, "{"), (seq.mark_out, "}")):
            if fr >= 0 and self.fx(fr) >= HW:
                p.setPen(QColor(C["accent"]))
                p.drawText(int(self.fx(fr)) - 3, RH - 14, ch)

        # トラックヘッダー
        for row in rows:
            self._paint_header(p, row)
        p.fillRect(QRect(0, 0, HW, RH), QColor(C["panel3"]))
        p.setPen(QColor(C["text"]))
        p.drawText(QRect(8, 0, HW - 8, RH), Qt.AlignmentFlag.AlignVCenter,
                   frames_to_timecode(self.mw.record_player.position + offset, fps))

        # トリムモードのローラー（赤=両側 / 黄=片側リップル）
        trim = getattr(self.mw, "trim", None)
        if trim:
            x = self.fx(trim["edit"])
            col = QColor(C["overwrite"] if trim["side"] == "ab" else C["splice"])
            for track, _lid, _rid in trim["rollers"]:
                ri = self.row_index(track)
                if ri < 0:
                    continue
                _, _, _, y, h = rows[ri]
                p.setPen(QPen(col, 3))
                if trim["side"] in ("ab", "a"):
                    p.drawLine(int(x) - 3, y + 3, int(x) - 3, y + h - 4)
                if trim["side"] in ("ab", "b"):
                    p.drawLine(int(x) + 3, y + 3, int(x) + 3, y + h - 4)
                p.setPen(QPen(col, 1))
                p.drawLine(int(x), y, int(x), y + h)
            p.setPen(QColor(C["splice"]))
            p.drawText(HW + 8, RH + 14, "トリムモード  M , . / でトリム   P [ ] で A側 / 両側 / B側   U で終了")

        # スナップ線（ヘッダーの上には描かない）
        if self._snap_x is not None and self._drag and self._snap_x >= HW:
            p.setPen(QPen(QColor("#ffffff"), 1, Qt.PenStyle.DashLine))
            p.drawLine(int(self._snap_x), RH, int(self._snap_x), H)

        # 再生ヘッド
        x = self.fx(self.mw.record_player.position)
        if x >= HW:
            p.setPen(QPen(QColor(C["playhead"]), 1))
            p.drawLine(int(x), RH - 2, int(x), H)
            p.setBrush(QColor(C["playhead"]))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(QPolygonF([QPointF(x - 6, RH - 14), QPointF(x + 6, RH - 14),
                                     QPointF(x + 6, RH - 6), QPointF(x, RH - 1),
                                     QPointF(x - 6, RH - 6)]))

        # ラバーバンド
        if self._drag and self._drag.get("mode") == "marquee":
            r = QRect(self._drag["p0"], self._drag["p1"]).normalized()
            p.setPen(QPen(QColor(C["accent"]), 1))
            p.setBrush(QColor(124, 108, 242, 40))
            p.drawRect(r)
        p.end()

    def _paint_clip(self, p: QPainter, row, c: M.Clip, sel: bool, fm, sel_pen):
        r = self.clip_rect(row, c)
        col = QColor(c.props.get("label") or C.get("clip_" + c.kind, C["clip_video"]))
        if not c.enabled:
            col = QColor("#555558")
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0, col.lighter(125 if sel else 112))
        g.setColorAt(1, col.darker(110) if not sel else col)
        p.fillRect(r, g)
        m = self.mw.project.media_by_id(c.media_id) if c.media_id else None
        offline = m is not None and not getattr(m, "pending", "") and MD.is_offline(m.path)
        if c.kind == "nest" and self.mw.project.sequence_by_id(c.props.get("seq_id", "")) is None:
            offline = True
        if offline:
            # Premiere のように、オフラインのクリップは赤い斜線
            p.fillRect(r, QColor(150, 20, 24))
            p.save()
            p.setClipRect(r)
            p.setPen(QPen(QColor(200, 50, 50), 3))
            step = 10
            for x0 in range(int(r.left()) - int(r.height()), int(r.right()), step):
                p.drawLine(QPointF(x0, r.bottom()), QPointF(x0 + r.height(), r.top()))
            p.restore()

        # 音声波形
        if c.kind == "audio" and m is not None and r.width() > 4:
            peaks = self.mw.peaks(m)
            if peaks is not None and len(peaks):
                p.setPen(QColor(200, 255, 230, 150))
                mid = r.center().y()
                amp = (r.height() - 14) / 2
                pps = 100.0 / self.seq.fps          # peaks per frame
                gain = 10 ** (float(c.prop("volume_db", 0.0)) / 20.0)
                x0 = max(r.left(), HW)
                x1 = min(r.right(), self.width())
                step = 1 if r.width() < 4000 else 2
                xi = int(x0)
                while xi < x1:
                    fr = c.src_in + (xi - r.left()) / self.ppf
                    i = int(fr * pps)
                    j = int((fr + step / self.ppf) * pps) + 1
                    if 0 <= i < len(peaks):
                        v = float(peaks[i:max(i + 1, min(j, len(peaks)))].max()) * gain
                        v = min(1.0, v) * amp
                        p.drawLine(xi, int(mid - v) + 6, xi, int(mid + v) + 7)
                    xi += step

        # サムネイル（先頭と末尾）
        if c.kind in ("video", "image") and m is not None and r.width() > 50 and r.height() > 30:
            pm = self.mw.thumb_pixmap(m)
            if pm is not None:
                th = int(r.height() - 16)
                tw = int(th * pm.width() / max(1, pm.height()))
                pr = QRectF(r.left() + 1, r.top() + 14, tw, th)
                if pr.right() < r.right():
                    p.drawPixmap(pr.toRect(), pm)
                if r.width() > tw * 2 + 20:
                    p.drawPixmap(QRectF(r.right() - tw - 1, r.top() + 14, tw, th).toRect(), pm)

        # クロスディゾルブ（頭の編集点をまたぐ X 印）
        dis = int(c.props.get("dissolve", 0) or 0)
        if dis > 0:
            x0 = self.fx(c.start - dis // 2)
            x1 = self.fx(c.start - dis // 2 + dis)
            rr = QRectF(x0, r.top() + 2, max(4.0, x1 - x0), r.height() - 4)
            p.fillRect(rr, QColor(255, 255, 255, 50))
            p.setPen(QPen(QColor(255, 255, 255, 200), 1))
            p.drawLine(rr.topLeft(), rr.bottomRight())
            p.drawLine(rr.bottomLeft(), rr.topRight())
        # 速度変更の表示
        sp = float(c.props.get("speed", 1.0) if c.props.get("speed") is not None else 1.0)
        if sp != 1.0 and r.width() > 40:
            p.setPen(QColor("#ffe08a"))
            p.drawText(QRectF(r.right() - 60, r.bottom() - 14, 56, 12),
                       Qt.AlignmentFlag.AlignRight, "停止" if sp == 0 else f"{sp * 100:.0f}%")
        # フェード表示
        p.setPen(QPen(QColor(255, 255, 255, 160), 1))
        fi, fo = int(c.prop("fade_in", 0) or 0), int(c.prop("fade_out", 0) or 0)
        if fi > 0:
            p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.left() + fi * self.ppf, r.top()))
        if fo > 0:
            p.drawLine(QPointF(r.right() - fo * self.ppf, r.top()), QPointF(r.right(), r.bottom()))

        # 名前
        if r.width() > 18:
            label = c.prop("text", "").replace("\n", " ") if c.kind == "text" else (c.name or c.kind)
            if c.prop("effects"):
                label = "ƒx " + label
            # 目印: オフライン・ネスト・スピードランプ・テイク・Dynamic Link・登場 / 退場アニメ
            marks = []
            if offline:
                marks.append("⚠オフライン")
            if c.kind == "nest":
                marks.append("⧉")
            if c.props.get("speed_keys"):
                marks.append("⤳")
            takes = c.props.get("takes") or []
            if len(takes) > 1:
                marks.append(f"T{takes.index(c.media_id) + 1 if c.media_id in takes else '?'}/{len(takes)}")
            if m is not None and getattr(m, "link_src", ""):
                st = getattr(self.mw, "dynlink", None)
                stt = st.state.get(m.id, {}).get("status") if st else ""
                marks.append({"rendering": "DL⟳", "error": "DL⚠", "waiting": "DL…"}.get(stt, "DL"))
            if c.props.get("clip_in") or c.props.get("clip_out"):
                marks.append("◇")
            if marks:
                label = " ".join(marks) + " " + label
            if c.kind in ("video", "audio") and c.props.get("volume_db"):
                label += f"  {float(c.props['volume_db']):+.1f}dB"
            tx = max(r.left(), HW) + 4
            avail = int(r.right() - tx - 3)
            if avail > 8:
                p.setPen(QColor("#ffffff") if sel else QColor("#f0f0f4"))
                p.drawText(QRectF(tx, r.top() + 1, avail, 13), Qt.AlignmentFlag.AlignLeft,
                           fm.elidedText(label, Qt.TextElideMode.ElideRight, avail))
        # 枠
        if sel:
            p.setPen(sel_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r.adjusted(1, 1, -1, -1))
        else:
            p.setPen(QPen(QColor(0, 0, 0, 120), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
        # ホバー中の端
        if self._hover and self._hover[0] == c.id:
            p.setPen(QPen(QColor(C["splice"] if self.tool in ("ripple", "yellow") else C["overwrite"]
                                 if self.tool == "roll" else "#ffffff"), 3))
            xe = r.left() + 1 if self._hover[1] == "in" else r.right() - 1
            p.drawLine(QPointF(xe, r.top()), QPointF(xe, r.bottom()))

    def _paint_header(self, p: QPainter, row):
        t, kind, idx, y, h = row
        if y + h < RH or y > self.height():
            return
        seq = self.seq
        p.fillRect(QRect(0, y, HW, h - 1), QColor(C["panel2"]))
        p.fillRect(QRect(0, y + h - 1, HW, 1), QColor(C["bg"]))
        target = t.selected
        # ソーストラック（パッチ）ボタン
        for key, rect, enabled in self._source_buttons(row):
            hl = self._drag and self._drag.get("mode") == "patch" and self._drag.get("key") == key
            p.setPen(QPen(QColor(C["selected"] if hl else C["border"]), 1))
            p.setBrush(QColor("#5f8fd6") if enabled else QColor(C["panel3"]))
            p.drawRoundedRect(rect, 3, 3)
            p.setPen(QColor("#ffffff") if enabled else QColor(C["dim"]))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, key)
        d = self._drag
        if d and d.get("mode") == "patch" and d.get("hover") is row and row[1] == d.get("kind"):
            p.setPen(QPen(QColor(C["selected"]), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRect(3, y + (h - 22) // 2, 30, 22), 3, 3)
        # レコードトラック（トラックセレクター）ボタン
        nr = QRect(38, y + (h - 22) // 2, 44, 22)
        p.setPen(QPen(QColor(C["accent"] if target else C["border"]), 1))
        p.setBrush(QColor(C["accent"]) if target else QColor(C["panel3"]))
        p.drawRoundedRect(nr, 3, 3)
        p.setPen(QColor("#ffffff") if target else QColor(C["text"]))
        p.drawText(nr, Qt.AlignmentFlag.AlignCenter, t.name)
        # ボタン群
        for key, rect, on, label in self._header_buttons(row):
            p.setPen(QPen(QColor(C["border"]), 1))
            colr = {"lock": "#9a6a2a", "mute": "#b0453c", "solo": "#c9a227", "sync": "#3f7f9a"}[key]
            p.setBrush(QColor(colr) if on else QColor(C["panel3"]))
            p.drawRoundedRect(rect, 2, 2)
            p.setPen(QColor("#ffffff") if on else QColor(C["dim"]))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
        # 名前の横にクリップ数
        p.setPen(QColor(C["dim"]))
        p.drawRect(QRect(HW - 1, y, 1, h - 1))

    def _header_buttons(self, row):
        t, kind, idx, y, h = row
        cy = y + (h - 18) // 2
        out = [("lock", QRect(90, cy, 24, 18), t.locked, "🔒" if t.locked else "L")]
        out.append(("mute", QRect(118, cy, 24, 18), t.muted, "👁" if kind == "video" else "M"))
        if kind == "audio":
            out.append(("solo", QRect(146, cy, 24, 18), t.solo, "S"))
        out.append(("sync", QRect(174, cy, 24, 18), t.sync_lock, "⇅"))
        return out

    def _source_buttons(self, row):
        """この行にパッチされているソーストラックのボタン [(key, rect, enabled)]。"""
        t, kind, idx, y, h = row
        out = []
        for key in self.mw.source_track_keys():
            if (kind == "video") == key.startswith("V") and self.mw.patch.get(key) == idx:
                out.append((key, QRect(4, y + (h - 20) // 2, 28, 20), self.mw.src_enabled.get(key, True)))
        return out

    # ── マウス ───────────────────────────────
    def mousePressEvent(self, e):
        self.setFocus()
        self.mw.set_active_monitor("record")
        pos = e.position().toPoint()
        mods = e.modifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)

        if e.button() == Qt.MouseButton.RightButton:
            self._context_menu(e)
            return
        if e.button() == Qt.MouseButton.MiddleButton or self.tool == "hand":
            self._drag = {"mode": "hand", "x": pos.x(), "y": pos.y(), "x0": self.x0, "y0": self.y0}
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        # ルーラー → 再生ヘッド（Ctrl / Shift を押している間だけ編集点に吸着）
        if pos.y() < RH and pos.x() >= HW:
            if getattr(self.mw, "trim", None):
                self.mw.exit_trim()
            self._drag = {"mode": "playhead"}
            self._move_playhead(pos.x(), ctrl or shift)
            return
        # ヘッダー
        if pos.x() < HW:
            row = self.row_at(pos.y())
            if row:
                self._header_click(row, pos)
            return
        if self.tool == "zoom":
            self.zoom_at(0.5 if alt else 2.0, pos.x())
            return

        row, clip, part = self.hit(pos)
        if self.tool == "razor":
            if row is not None:
                f = self.do_snap(self.frame_at(pos.x()), shift=False)
                tracks = None if shift else [row[0]]
                if not shift and clip is not None and self.linked_selection:
                    tracks = [row[0]] + [t for t, _ in self.seq.linked(clip)]
                self.mw.begin_edit()
                M.razor(self.seq, f, tracks)
                self.mw.end_edit("分割")
            return
        if clip is None:
            # 何もない所: クリックなら再生ヘッドを移動（Avid）、ドラッグなら範囲選択
            if not (shift or ctrl):
                self.selected.clear()
                self.selection_changed.emit()
            self._drag = {"mode": "marquee", "p0": pos, "p1": pos, "base": set(self.selected),
                          "moved": False}
            self.update()
            return

        # クリップ上
        track = row[0]
        if track.locked:
            return
        if ctrl:
            self.select_clip(clip, toggle=True, linked=not alt)
        elif shift:
            self.select_clip(clip, add=True, linked=not alt)
        elif clip.id not in self.selected:
            self.select_clip(clip, linked=not alt)

        mode = None
        if self.tool in ("select", "ripple", "yellow") and part in ("in", "out"):
            mode = "trim"
        elif self.tool == "roll" and part in ("in", "out"):
            mode = "roll"
        elif self.tool == "roll":
            mode = "trim"
        elif self.tool == "slip":
            mode = "slip"
        elif self.tool == "slide":
            mode = "slide"
        elif self.tool in ("select", "ripple", "yellow"):
            mode = "move"
        if mode is None:
            return
        # 位置をロックしたクリップは動かさない（クリップ → ロック）
        locked = [x for x in (self.seq.find(i)[1] for i in self.selected) if x is not None and x.props.get("pos_locked")]
        if locked:
            self.mw.status.showMessage(f"🔒 ロックしたクリップがあるので動かせません（{locked[0].name or locked[0].kind}）", 4000)
            return
        self.mw.begin_edit()
        self._drag = {"mode": mode, "x": pos.x(), "y": pos.y(), "clip": clip.id,
                      "f0": self.x0 + (pos.x() - HW) / self.ppf,
                      "track": track, "row": row, "part": part or "out", "alt": alt,
                      "backup": copy.deepcopy((self.seq.video_tracks, self.seq.audio_tracks)),
                      "moved": False}

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        mods = e.modifiers()
        d = self._drag
        if d is None:
            self._update_hover(pos)
            return
        self._last_mouse = (QPoint(pos), mods)
        self._drag_update(pos, mods)
        # 左右の端に来たら自動スクロール
        if d["mode"] in ("playhead", "move", "trim", "roll", "slide", "marquee") and \
                (pos.x() < HW + 12 or pos.x() > self.width() - 12):
            if not self._scroll_timer.isActive():
                self._scroll_timer.start()
        else:
            self._scroll_timer.stop()

    def _auto_scroll(self):
        if self._drag is None or not hasattr(self, "_last_mouse"):
            self._scroll_timer.stop()
            return
        pos, mods = self._last_mouse
        if pos.x() < HW + 12:
            step = -max(1.0, (HW + 12 - pos.x()) / 3) / self.ppf
        elif pos.x() > self.width() - 12:
            step = max(1.0, (pos.x() - self.width() + 12) / 3) / self.ppf
        else:
            self._scroll_timer.stop()
            return
        self.x0 = max(0.0, self.x0 + step * 4)
        self.zoom_changed.emit()
        self._drag_update(pos, mods)

    def _drag_update(self, pos, mods):
        d = self._drag
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        mode = d["mode"]
        if mode == "hand":
            self.x0 = max(0.0, d["x0"] - (pos.x() - d["x"]) / self.ppf)
            self.y0 = max(0, d["y0"] - (pos.y() - d["y"]))
            self.zoom_changed.emit()
            self.update()
            return
        if mode == "playhead":
            self._move_playhead(pos.x(), ctrl or shift)
            return
        if mode == "patch":
            d["moved"] = d["moved"] or abs(pos.y() - d["y"]) > 4
            d["hover"] = self.row_at(pos.y())
            self.update()
            return
        if mode == "marquee":
            if not d["moved"] and abs(pos.x() - d["p0"].x()) < 4 and abs(pos.y() - d["p0"].y()) < 4:
                return
            d["moved"] = True
            d["p1"] = pos
            rect = QRect(d["p0"], pos).normalized()
            sel = set(d["base"])
            for row in self._rows:
                for c in row[0].clips:
                    if self.clip_rect(row, c).intersects(QRectF(rect)):
                        sel.add(c.id)
                        if self.linked_selection:
                            sel |= {x.id for _, x in self.seq.linked(c)}
            self.selected = sel
            self.selection_changed.emit()
            self.update()
            return
        if not d["moved"] and abs(pos.x() - d["x"]) < 3 and abs(pos.y() - d["y"]) < 3:
            return
        d["moved"] = True
        # スクロールしても正しく動くよう、フレーム位置の差で計算
        df = int(round(self.x0 + (pos.x() - HW) / self.ppf - d["f0"]))
        if mode == "move":
            self._preview_move(d, pos, df, shift)
        else:
            self._live_edit(d, df, shift)
        self.update()

    def mouseReleaseEvent(self, e):
        d = self._drag
        self._drag = None
        self._snap_x = None
        self._scroll_timer.stop()
        self.unsetCursor()
        if d is None:
            return
        mode = d["mode"]
        if mode == "marquee":
            if not d.get("moved"):
                # クリックだけなら再生ヘッドをその位置へ（Avid の動作）
                if getattr(self.mw, "trim", None):
                    self.mw.exit_trim()
                self._move_playhead(d["p0"].x(), bool(e.modifiers() & (
                    Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)))
            self.update()
            return
        if mode == "patch":
            row = self.row_at(e.position().y())
            if d["moved"] and row is not None and row[1] == d["kind"]:
                self.mw.set_patch(d["key"], row[2])
            elif not d["moved"]:
                self.mw.toggle_source_track(d["key"])
            self.update()
            return
        if mode in ("hand", "playhead"):
            return
        if not d.get("moved"):
            self.mw.end_edit(None)
            self._ghost = []
            self.update()
            return
        if mode == "move":
            ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
            self._commit_move(d, ctrl)
            self._ghost = []
            self.mw.end_edit("クリップを複製" if d["alt"] else "クリップを移動")
        else:
            label = {"trim": "トリム", "roll": "ローリング編集", "slip": "スリップ",
                     "slide": "スライド"}[mode]
            self.mw.end_edit(label)
        self.update()

    def mouseDoubleClickEvent(self, e):
        pos = e.position().toPoint()
        if pos.y() < RH or pos.x() < HW:
            return
        row, clip, _ = self.hit(pos)
        if clip is not None:
            self.mw.clip_double_clicked(clip)

    def leaveEvent(self, _e):
        self._hover = None
        self.update()

    def _update_hover(self, pos):
        row, clip, part = self.hit(pos)
        cur = Qt.CursorShape.ArrowCursor
        hover = None
        if pos.x() < HW or pos.y() < RH:
            cur = Qt.CursorShape.ArrowCursor
        elif self.tool == "razor":
            cur = Qt.CursorShape.CrossCursor
        elif self.tool == "hand":
            cur = Qt.CursorShape.OpenHandCursor
        elif self.tool == "zoom":
            cur = Qt.CursorShape.PointingHandCursor
        elif clip is not None and part in ("in", "out") and self.tool in ("select", "ripple", "roll", "yellow"):
            cur = Qt.CursorShape.SizeHorCursor
            hover = (clip.id, part)
        elif clip is not None and self.tool in ("slip", "slide"):
            cur = Qt.CursorShape.SizeHorCursor
        self.setCursor(cur)
        if hover != self._hover:
            self._hover = hover
            self.update()
        if clip is not None:
            self.setToolTip(f"{clip.name or clip.prop('text', '')}\n"
                            f"{frames_to_timecode(clip.start + self.mw.tc_offset(self.seq), self.seq.fps)}"
                            f" - 長さ {frames_to_timecode(clip.duration, self.seq.fps)}")
        else:
            self.setToolTip("")

    def _move_playhead(self, x, snap: bool):
        """再生ヘッドをマウス位置へ。トラック領域の外に出ないよう制限（端では自動スクロール）。"""
        x = max(HW, min(self.width() - 1, x))
        f = max(0, self.frame_at(x))
        if snap:
            f = self.do_snap(f, force=True, playhead=False)
        else:
            self._snap_x = None
        self.mw.seek_record(f, from_timeline=True)
        self.update()

    # ── ドラッグ処理 ─────────────────────────
    def _row_shift(self, d, pos) -> int:
        """押した行からの行移動数（同じ種類のトラック内）。"""
        row0 = d["row"]
        row1 = self.row_at(pos.y())
        if row1 is None or row1[1] != row0[1]:
            return 0
        return self._rows.index(row1) - self._rows.index(row0)

    def _move_targets(self, d, pos):
        """移動対象 [(clip, src_track, dst_track)] とゴースト用行番号。"""
        seq = self.seq
        drow = self._row_shift(d, pos)
        out = []
        kind0 = d["row"][1]
        for t, c in self.selected_clips():
            if t.locked:
                continue
            ri = self.row_index(t)
            dst = t
            if t.kind == kind0 and drow:
                ni = ri + drow
                if 0 <= ni < len(self._rows) and self._rows[ni][1] == t.kind:
                    dst = self._rows[ni][0]
                else:
                    return None
            out.append((c, t, dst))
        return out

    def _preview_move(self, d, pos, df, shift):
        targets = self._move_targets(d, pos)
        if not targets:
            self._ghost = []
            return
        lead = next((c for c, _, _ in targets if c.id == d["clip"]), targets[0][0])
        ids = {c.id for c, _, _ in targets}
        min_start = min(c.start for c, _, _ in targets)
        df = max(df, -min_start)
        # 先頭クリップの開始/終了を基準にスナップ
        ns = self.do_snap(lead.start + df, exclude=ids, shift=shift)
        if ns != lead.start + df:
            df = ns - lead.start
        else:
            ne = self.do_snap(lead.end + df, exclude=ids, shift=shift)
            df = ne - lead.end
        d["df"] = df
        d["targets"] = targets
        col = C["splice"] if (d["alt"] or self.tool == "yellow") else C["overwrite"] if self.tool == "select" \
            else C["selected"]
        self._ghost = [(self.row_index(dst), c.start + df, c.duration, col) for c, _, dst in targets]

    def _commit_move(self, d, insert: bool):
        targets = d.get("targets")
        if not targets:
            return
        df = d.get("df", 0)
        seq = self.seq
        if d["alt"]:
            # 複製: 新しい ID とリンクで配置
            link_map = {}
            placements = []
            for c, _, dst in targets:
                nc = c.copy(id=M.new_id(), start=c.start + df)
                if c.link:
                    nc.link = link_map.setdefault(c.link, M.new_id())
                placements.append((dst, nc))
            M.place_clips(seq, placements, "insert" if insert else "overwrite")
            self.selected = {c.id for _, c in placements}
        elif self.tool == "yellow":
            # 黄セグメント: 元の場所を詰め（抽出）、移動先に挿入（スプライスイン）
            ids = {c.id for c, _, _ in targets}
            a0 = min(c.start for c, _, _ in targets)
            b0 = max(c.end for c, _, _ in targets)
            dest = a0 + df
            moved = [(c.copy(), dst) for c, _, dst in targets]
            M.delete_clips(seq, ids, ripple=True)
            if dest > a0:
                dest = max(a0, dest - (b0 - a0))
            placements = []
            for c, dst in moved:
                c.start = dest + (c.start - a0)
                placements.append((dst, c))
            M.place_clips(seq, placements, "insert")
            self.selected = {c.id for _, c in placements}
        else:
            M.move_clips(seq, targets, df, "insert" if insert else "overwrite")
        self.selection_changed.emit()

    def _live_edit(self, d, df, shift):
        """トリム系は元の状態から毎回やり直して即時反映する。"""
        seq = self.seq
        seq.video_tracks, seq.audio_tracks = copy.deepcopy(d["backup"])
        track, clip = seq.find(d["clip"])
        if clip is None:
            return
        proj = self.mw.project
        mode = d["mode"]
        part = d["part"]
        if mode in ("trim", "roll"):
            edge_frame = clip.start + df if part == "in" else clip.end + df
            snapped = self.do_snap(edge_frame, exclude={clip.id}, shift=shift)
            df += snapped - edge_frame
        if mode == "trim":
            tmode = "ripple" if self.tool in ("ripple", "yellow") else "normal"
            done = M.trim_edge(seq, track, clip, part, df, proj, tmode)
            if self.linked_selection and not d["alt"]:
                for lt, lc in seq.linked(clip):
                    if not lt.locked:
                        M.trim_edge(seq, lt, lc, part, done, proj, "normal" if tmode == "normal" else "ripple_linked")
            self.mw.seek_record(clip.start if part == "in" else clip.end - 1, quiet=True)
        elif mode == "roll":
            if part == "out":
                left, right = clip, next((c for c in track.clips if c.start == clip.end), None)
            else:
                right, left = clip, next((c for c in track.clips if c.end == clip.start), None)
            if left is None or right is None:
                M.trim_edge(seq, track, clip, part, df, proj, "normal")
            else:
                done = M.roll_edit(seq, track, left, right, df, proj)
                if self.linked_selection:
                    for lt, lc in seq.linked(left):
                        rr = next((c for c in lt.clips if c.start == lc.end), None)
                        if rr is not None:
                            M.roll_edit(seq, lt, lc, rr, done, proj)
                self.mw.seek_record(left.end, quiet=True)
        elif mode == "slip":
            done = M.slip(seq, clip, df, proj)
            for lt, lc in seq.linked(clip):
                M.slip(seq, lc, done, proj)
            self.mw.seek_record(clip.start, quiet=True)
        elif mode == "slide":
            done = M.slide(seq, track, clip, df, proj)
            for lt, lc in seq.linked(clip):
                M.slide(seq, lt, lc, done, proj)
            self.mw.seek_record(clip.start, quiet=True)

    # ── ヘッダー ─────────────────────────────
    def _header_click(self, row, pos):
        t, kind, idx, y, h = row
        for key, rect, _en in self._source_buttons(row):
            if rect.contains(pos):
                # クリック: ソーストラックの入/切、ドラッグ: 別のトラックへパッチ
                self._drag = {"mode": "patch", "key": key, "kind": kind, "y": pos.y(), "moved": False,
                              "hover": row}
                return
        for key, rect, on, _ in self._header_buttons(row):
            if rect.contains(pos):
                self.mw.begin_edit()
                setattr(t, {"lock": "locked", "mute": "muted", "solo": "solo", "sync": "sync_lock"}[key], not on)
                self.mw.end_edit(None)
                return
        # Avid のレコードトラックセレクター（複数選択可）
        t.selected = not t.selected
        self.mw.set_dirty_light()
        self.update()

    # ── コンテキストメニュー ─────────────────
    def _context_menu(self, e):
        pos = e.position().toPoint()
        gpos = e.globalPosition().toPoint()
        mw = self.mw
        m = QMenu(self)
        if pos.x() < HW:
            row = self.row_at(pos.y())
            if row is None:
                return
            t, kind, idx, _, _ = row
            m.addAction("トラック名を変更...", lambda: self._rename_track(t))
            m.addAction("映像トラックを追加" if kind == "video" else "音声トラックを追加",
                        lambda: mw.add_track(kind))
            act = m.addAction("このトラックを削除", lambda: mw.delete_track(t))
            act.setEnabled(len(self.seq.video_tracks if kind == "video" else self.seq.audio_tracks) > 1)
            hm = m.addMenu("トラックの高さ")
            for lab, hh in (("小", 30), ("標準", 46 if kind == "video" else 40), ("大", 80), ("特大", 120)):
                hm.addAction(lab, lambda hh=hh: self._set_height(t, hh))
            m.exec(gpos)
            return
        row, clip, _ = self.hit(pos)
        if clip is not None:
            if clip.id not in self.selected:
                self.select_clip(clip)
            m.addAction("切り取り\tCtrl+X", mw.cut_clips)
            m.addAction("コピー\tCtrl+C", mw.copy_clips)
            m.addAction("削除（隙間を残す）\tDelete", mw.delete_selected)
            m.addAction("リップル削除\tShift+Delete", mw.ripple_delete_selected)
            m.addSeparator()
            m.addAction("有効 / 無効\tShift+E", mw.toggle_clips_enabled)
            m.addAction("リンク / リンク解除\tCtrl+L", mw.toggle_link)
            m.addAction("フェードイン/アウトを追加", mw.add_default_fades)
            m.addAction("クロスディゾルブ（頭）", lambda: mw.add_dissolve(1.0))
            m.addAction("速度を変更...", mw.speed_dialog)
            if clip.kind == "video":
                m.addAction("フリーズフレームを挿入", mw.freeze_frame)
            if clip.kind == "audio":
                m.addAction("ノーマライズ", mw.normalize)
            m.addAction("無音カット...", mw.silence_cut)
            m.addAction("属性をペースト...", mw.paste_attributes)
            cm = m.addMenu("クリップカラー")
            for name, col in (("既定", ""), ("赤", "#d8463c"), ("橙", "#e5902a"), ("黄", "#e5c02a"),
                              ("緑", "#3fae5a"), ("水色", "#2fa3b5"), ("青", "#4f6fe0"), ("紫", "#9a6ae0")):
                cm.addAction(name, lambda col=col: mw.apply_label(col))
            if clip.media_id:
                m.addAction("ビンで表示（Find Bin）", mw.find_bin)
            m.addSeparator()
            if clip.kind in ("video", "audio", "image") and clip.media_id:
                m.addAction("ソースモニターで開く", lambda: mw.load_source(clip.media_id))
            if clip.kind == "text":
                m.addAction("テキストを編集", lambda: mw.clip_double_clicked(clip))
            m.addAction("インスペクタを表示", mw.show_inspector)
            if hasattr(mw, "clip_context_actions"):
                mw.clip_context_actions(m, clip)
        else:
            f = self.frame_at(pos.x())
            if row is not None:
                m.addAction("ここに貼り付け\tCtrl+V", lambda: mw.paste_clips(f, row[0]))
                m.addAction("この隙間を詰める（リップル削除）",
                            lambda: mw.ripple_delete_gap(row[0], f))
                if row[1] == "video":
                    m.addAction("ここにテロップを追加\tT", lambda: mw.add_telop(f, row[0]))
            m.addSeparator()
            m.addAction("映像トラックを追加", lambda: mw.add_track("video"))
            m.addAction("音声トラックを追加", lambda: mw.add_track("audio"))
            if hasattr(mw, "empty_context_actions"):
                mw.empty_context_actions(m, f)
        m.exec(gpos)

    def _rename_track(self, t):
        name, ok = QInputDialog.getText(self, "トラック名", "名前:", text=t.name)
        if ok and name.strip():
            self.mw.begin_edit()
            t.name = name.strip()[:6]
            self.mw.end_edit("トラック名変更")

    def _set_height(self, t, h):
        t.height = h
        self.update()

    # ── ドラッグ＆ドロップ ───────────────────
    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasFormat(MIME_MEDIA) or md.hasFormat(MIME_GEN) or md.hasFormat(MIME_FX) or md.hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        md = e.mimeData()
        pos = e.position().toPoint()
        row = self.row_at(pos.y())
        f = max(0, self.frame_at(pos.x()))
        self._ghost = []
        if md.hasFormat(MIME_FX):
            row, clip, _ = self.hit(pos)
            self._hover = (clip.id, "body") if clip else None
            e.acceptProposedAction()
            self.update()
            return
        if row is not None and pos.x() >= HW:
            f = self.do_snap(f)
            dur = int(self.seq.fps * 5)
            if md.hasFormat(MIME_MEDIA):
                ids = bytes(md.data(MIME_MEDIA)).decode().split(",")
                m = self.mw.project.media_by_id(ids[0])
                if m is not None:
                    dur = self.mw.media_frames(m)
            self._ghost = [(self._rows.index(row), f, dur, C["splice"] if
                            e.modifiers() & Qt.KeyboardModifier.ControlModifier else C["overwrite"])]
        e.acceptProposedAction()
        self.update()

    def dragLeaveEvent(self, _e):
        self._ghost = []
        self._snap_x = None
        self.update()

    def dropEvent(self, e):
        md = e.mimeData()
        pos = e.position().toPoint()
        self._ghost = []
        self._snap_x = None
        insert = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        row = self.row_at(pos.y())
        f = max(0, self.do_snap(self.frame_at(max(HW, pos.x()))))
        self._snap_x = None
        if md.hasFormat(MIME_FX):
            _, clip, _ = self.hit(pos)
            fx = json.loads(bytes(md.data(MIME_FX)).decode())
            if clip is not None:
                if clip.id not in self.selected:
                    self.select_clip(clip)
                self.mw.apply_effect(fx)
        elif md.hasFormat(MIME_MEDIA):
            ids = bytes(md.data(MIME_MEDIA)).decode().split(",")
            self.mw.place_media_at(ids, row, f, insert)
        elif md.hasFormat(MIME_GEN):
            gen = json.loads(bytes(md.data(MIME_GEN)).decode())
            self.mw.place_generator(gen, row, f)
        elif md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            self.mw.import_files(paths, place=(row, f, insert))
        e.acceptProposedAction()
        self._hover = None
        self.update()


class TimelinePanel(QWidget):
    """ツールバー + キャンバス + スクロールバー。"""

    def __init__(self, mw, parent=None):
        super().__init__(parent)
        self.mw = mw
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QWidget()
        bar.setStyleSheet(f"background: {C['panel2']};")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(6, 3, 6, 3)
        bl.setSpacing(2)
        self.seq_label = QLabel("")
        self.seq_label.setStyleSheet("font-weight: bold; background: transparent;")
        bl.addWidget(self.seq_label)
        bl.addSpacing(12)
        self.tool_group = QButtonGroup(self)
        self.tool_buttons = {}
        for key, icon, tip in TOOLS:
            b = QToolButton()
            b.setText(icon)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setFixedSize(32, 24)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, k=key: self.set_tool(k))
            if key == "select":
                b.setStyleSheet(f"QToolButton {{ color: {C['overwrite']}; font-weight: bold; }}")
            elif key == "yellow":
                b.setStyleSheet(f"QToolButton {{ color: {C['splice']}; font-weight: bold; }}")
            self.tool_group.addButton(b)
            self.tool_buttons[key] = b
            bl.addWidget(b)
        bl.addSpacing(12)
        self.snap_btn = QToolButton()
        self.snap_btn.setText("🧲 スナップ")
        self.snap_btn.setCheckable(True)
        self.snap_btn.setChecked(True)
        self.snap_btn.setToolTip("スナップ (S)  ドラッグ中 Shift で一時反転")
        self.snap_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bl.addWidget(self.snap_btn)
        self.link_btn = QToolButton()
        self.link_btn.setText("🔗 リンク選択")
        self.link_btn.setCheckable(True)
        self.link_btn.setChecked(True)
        self.link_btn.setToolTip("リンクされた映像と音声を一緒に選択・編集（Alt で一時解除）")
        self.link_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bl.addWidget(self.link_btn)
        bl.addStretch(1)
        self.info = QLabel("")
        self.info.setObjectName("info")
        bl.addWidget(self.info)
        bl.addSpacing(8)
        for txt, fac, tip in (("－", 0.66, "縮小 (-)"), ("＋", 1.5, "拡大 (=)")):
            b = QToolButton()
            b.setText(txt)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, f=fac: self.canvas.zoom_at(f))
            bl.addWidget(b)
        b = QToolButton()
        b.setText("全体")
        b.setToolTip("シーケンス全体を表示 (\\)")
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(lambda: self.canvas.zoom_fit())
        bl.addWidget(b)
        lay.addWidget(bar)

        mid = QHBoxLayout()
        mid.setSpacing(0)
        self.canvas = TimelineCanvas(mw)
        self.vbar = QScrollBar(Qt.Orientation.Vertical)
        mid.addWidget(self.canvas, 1)
        mid.addWidget(self.vbar)
        lay.addLayout(mid, 1)
        self.hbar = QScrollBar(Qt.Orientation.Horizontal)
        lay.addWidget(self.hbar)

        self.snap_btn.toggled.connect(lambda on: setattr(self.canvas, "snap", on))
        self.link_btn.toggled.connect(lambda on: setattr(self.canvas, "linked_selection", on))
        self.canvas.zoom_changed.connect(self.sync_scrollbars)
        self.hbar.valueChanged.connect(self._on_hbar)
        self.vbar.valueChanged.connect(self._on_vbar)
        self._sync = False
        self.set_tool("select")

    def set_tool(self, key: str):
        self.canvas.tool = key
        self.tool_buttons[key].setChecked(True)
        names = {k: tip.split("\n")[0] for k, _, tip in TOOLS}
        self.info.setText(names[key])

    def toggle_snap(self):
        self.snap_btn.setChecked(not self.snap_btn.isChecked())

    def sync_scrollbars(self):
        self._sync = True
        c = self.canvas
        total = max(c.seq.duration + int(c.seq.fps * 30), int((c.width() - HW) / c.ppf))
        page = int((c.width() - HW) / c.ppf)
        self.hbar.setRange(0, max(0, total - page))
        self.hbar.setPageStep(max(1, page))
        self.hbar.setValue(int(c.x0))
        c._layout()
        vis = c.height() - RH
        self.vbar.setRange(0, max(0, c._content_h - vis))
        self.vbar.setPageStep(max(1, vis))
        self.vbar.setValue(c.y0)
        self._sync = False

    def _on_hbar(self, v):
        if not self._sync:
            self.canvas.x0 = v
            self.canvas.update()

    def _on_vbar(self, v):
        if not self._sync:
            self.canvas.y0 = v
            self.canvas.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.sync_scrollbars()
