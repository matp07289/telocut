"""MMD の立ち絵・OBS 連携・Blackmagic（Desktop Video）・ペン（板タブ）（MainWindow のミックスイン）。"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTextEdit,
                             QVBoxLayout, QWidget)

from . import model as M
from .settings import SETTINGS


class _Bus(QObject):
    """別スレッド → 画面のスレッド。"""
    call = pyqtSignal(object)


class Features14Mixin:
    def _feature14_commands(self) -> dict:
        return {"mmd_tachie": self.mmd_tachie_dialog, "obs_panel": self.obs_panel,
                "obs_process_file": self.obs_process_file_dialog, "bmd_info": self.bmd_info,
                "bmd_output": self.bmd_toggle_output, "pen_mode": self.pen_mode_dialog,
                "pen_toolbar": self.pen_toggle_toolbar}

    def _build_feature14_menus(self):
        self._bus14 = _Bus()
        self._bus14.call.connect(lambda fn: fn())
        m = self._menu_named("3D / MMD") or self._menu_named("YouTube")
        if m is not None:
            m.addSeparator()
            self._a(m, "mmd_tachie")
        m = self._menu_named("連携・管理") or self._menu_named("AI / 連携")
        if m is not None:
            m.addSeparator()
            for c in ("obs_panel", "obs_process_file", None, "bmd_info", "bmd_output"):
                m.addSeparator() if c is None else self._a(m, c)
        m = self._menu_named("設定") or self._menu_named("ウィンドウ")
        if m is not None:
            m.addSeparator()
            for c in ("pen_mode", "pen_toolbar"):
                self._a(m, c)
        self._init_pen()
        self._init_bmd_watch()
        if (getattr(SETTINGS, "obs", {}) or {}).get("auto_connect"):
            QTimer.singleShot(1500, lambda: self._obs_connect(quiet=True))

    def _ui(self, fn):
        """別スレッドから画面の処理を頼む。"""
        self._bus14.call.emit(fn)

    # ═════════════════════════════════════════
    #  MMD のモデル → 立ち絵（目パチ・口パク）
    # ═════════════════════════════════════════
    def mmd_tachie_dialog(self, path: str = ""):
        from . import mmdtachie as MT
        exe = MT.find_blender()
        if not exe:
            QMessageBox.warning(self, "MMD の立ち絵", "Blender が見つかりません。Blender（4.2 など）と MMD Tools を入れてください。")
            return None
        dlg = QDialog(self)
        dlg.setWindowTitle("MMD のモデルから立ち絵を作る（Blender）")
        dlg.resize(760, 640)
        lay = QHBoxLayout(dlg)
        f = QFormLayout()
        left = QWidget()
        left.setLayout(f)
        lay.addWidget(left, 3)
        st = {"info": None, "blend": "", "dlg": dlg}
        dlg._st = st

        src = QLineEdit(path)
        b = QPushButton("選ぶ…")
        row = QHBoxLayout()
        row.addWidget(src, 1)
        row.addWidget(b)
        f.addRow("モデル（PMX / .blend）", row)
        info = QLabel("モデルを選ぶと、表情（モーフ）を調べます")
        info.setWordWrap(True)
        info.setStyleSheet("color: gray;")
        f.addRow(info)
        name = QLineEdit()
        f.addRow("名前", name)
        expr = QComboBox()
        expr.addItems(list(MT.EXPRESSIONS))
        f.addRow("表情", expr)
        from PyQt6.QtWidgets import QListWidget, QListWidgetItem
        vlist = QListWidget()
        vlist.setMaximumHeight(96)
        vlist.setToolTip("チェックした表情もまとめて作ります（Blender は 1 回だけ）。\n"
                         "「キャラクター・声 → 立ち絵の表情を切り替える」で、セリフごとに表情を変えられます。")

        def fill_vlist(names):
            vlist.clear()
            for t in names:
                it = QListWidgetItem(t)
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(Qt.CheckState.Unchecked)
                vlist.addItem(it)
        fill_vlist(list(MT.EXPRESSIONS))
        f.addRow("表情差分もまとめて作る", vlist)
        extra = QLineEdit()
        extra.setPlaceholderText("例: 照れ１=1, 頬染め=0.5（モーフを直接）")
        f.addRow("モーフを足す", extra)
        pose = QComboBox()
        pose.addItems(list(MT.POSES))
        pose.setCurrentIndex(1)
        f.addRow("ポーズ", pose)
        arm = QSpinBox()
        arm.setRange(0, 80)
        arm.setValue(38)
        arm.setSuffix("°")
        tilt = QSpinBox()
        tilt.setRange(-30, 30)
        tilt.setValue(8)
        tilt.setSuffix("°")
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("腕を下ろす"))
        r2.addWidget(arm)
        r2.addWidget(QLabel("首のかしげ"))
        r2.addWidget(tilt)
        f.addRow("角度", r2)
        vpd = QLineEdit()
        vpd.setPlaceholderText("MMD のポーズファイル（.vpd）・空 = 使わない")
        bv = QPushButton("…")
        r3 = QHBoxLayout()
        r3.addWidget(vpd, 1)
        r3.addWidget(bv)
        f.addRow("ポーズファイル", r3)
        frame = QComboBox()
        frame.addItems(MT.FRAMINGS)
        frame.setCurrentText("腰から上")
        f.addRow("構図", frame)
        yaw = QSpinBox()
        yaw.setRange(-60, 60)
        yaw.setSuffix("°（＋ = 右を向く）")
        f.addRow("向き", yaw)
        height = QSpinBox()
        height.setRange(400, 4000)
        height.setSingleStep(200)
        height.setValue(1600)
        height.setSuffix(" px")
        f.addRow("高さ", height)
        outline = QCheckBox("輪郭線（アニメ調）")
        f.addRow(outline)
        tmpl = QCheckBox("立ち絵テンプレートにも保存（次から「立ち絵テンプレートから置く」で使える）")
        tmpl.setChecked(True)
        f.addRow(tmpl)
        place = QCheckBox("再生ヘッドに置く（口パクは選んでいる音声トラック）")
        place.setChecked(True)
        f.addRow(place)
        blend_lab = QLabel("")
        blend_lab.setStyleSheet("color: #8cf;")
        f.addRow(blend_lab)
        br = QHBoxLayout()
        b_bl = QPushButton("Blender で開いてポーズを直す")
        b_bl.setToolTip("モデルを読み込んで今のポーズを付けた状態で Blender を開きます。\n"
                        "ポーズを直して保存（Ctrl+S）したら「作る」を押すと、そのポーズで作ります。")
        b_go = QPushButton("作る")
        b_go.setDefault(True)
        br.addWidget(b_bl)
        br.addStretch(1)
        br.addWidget(b_go)
        f.addRow(br)
        pv = QLabel("プレビュー")
        pv.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pv.setMinimumWidth(240)
        pv.setStyleSheet("background: #202024; color: gray;")
        lay.addWidget(pv, 2)

        def pick():
            p, _ = QFileDialog.getOpenFileName(dlg, "MMD のモデル", str(Path(src.text()).parent) if src.text() else "",
                                               "MMD モデル (*.pmx *.pmd);;Blender (*.blend)")
            if p:
                src.setText(p)
                do_probe()

        def pick_vpd():
            p, _ = QFileDialog.getOpenFileName(dlg, "ポーズファイル", "", "VPD (*.vpd)")
            if p:
                vpd.setText(p)

        def do_probe():
            p = src.text().strip()
            if not os.path.exists(p):
                return
            info.setText("Blender でモデルを調べています…")
            blend = p if p.lower().endswith(".blend") else ""

            def work(task):
                return MT.probe(exe, "" if blend else p, blend)

            def done(r):
                st["info"] = r
                st["blend"] = blend
                roles = MT.roles_for(r["morphs"])
                ok_ex = [k for k in MT.EXPRESSIONS if k == "通常" or MT.expression_morphs(k, r["morphs"])]
                expr.clear()
                expr.addItems(ok_ex)
                fill_vlist(ok_ex)
                name.setText(name.text() or r.get("name") or Path(p).stem)
                miss = [k for k, v in roles.items() if not v]
                info.setText(f"{r.get('name')}: モーフ {len(r['morphs'])} 個・ボーン {len(r['bones'])} 本。"
                             f"目 = {roles['eye'] or '（なし）'}・口 = {roles['a'] or '（なし）'}"
                             + (f"（無いもの: {', '.join(miss)}）" if miss else ""))
            self._run_task("モデルを調べる", work, on_done=done)

        def params(out_dir: str, save_blend: str = "") -> dict:
            r = st["info"] or {"morphs": []}
            ex = {}
            for part in extra.text().replace("、", ",").split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    try:
                        ex[k.strip()] = float(v)
                    except ValueError:
                        pass
            p = src.text().strip()
            blend = st["blend"]
            return MT.render_params("" if blend else p, r["morphs"], expr.currentText(), pose.currentText(),
                                    arm.value(), tilt.value(), frame.currentText(), yaw.value(), height.value(),
                                    outline.isChecked(), vpd.text().strip(), blend, ex, out_dir, save_blend)

        def open_blender():
            if st["info"] is None:
                QMessageBox.information(dlg, "MMD の立ち絵", "先にモデルを選んでください。")
                return
            blend = os.path.join(MT._work_dir(), f"pose_{Path(src.text()).stem}.blend")
            P = params(MT._work_dir(), blend)

            def work(task):
                return MT.open_in_blender(exe, P)

            def done(path):
                st["blend"] = path
                blend_lab.setText(f"Blender のポーズを使います: {Path(path).name}（保存してから「作る」）")
            self._run_task("Blender を開く", work, on_done=done)

        def go():
            if st["info"] is None:
                QMessageBox.information(dlg, "MMD の立ち絵", "先にモデルを選んでください。")
                return
            nm = name.text().strip() or "MMD 立ち絵"
            out_dir = os.path.join(MT._work_dir(), "render_" + time.strftime("%Y%m%d_%H%M%S"))
            P = params(out_dir)
            more = [vlist.item(i).text() for i in range(vlist.count())
                    if vlist.item(i).checkState() == Qt.CheckState.Checked and vlist.item(i).text() != expr.currentText()]
            if more:
                # 表情差分をまとめて（1 枚目 = 選んだ表情）
                PV = MT.variants_params(P, (st["info"] or {"morphs": []})["morphs"], [expr.currentText()] + more)

                def work_v(task):
                    n = sum(len(v) for v in PV["variants"].values())
                    done_n = [0]

                    def prog(key):
                        done_n[0] += 1
                        task.progress.emit(done_n[0], n, f"{key} をレンダリング")
                    return MT.render_variants(exe, PV, progress=prog, cancel=lambda: task.cancelled)

                def done_v(by_expr):
                    chars = self.mmd_tachie_finish_variants(nm, by_expr, template=tmpl.isChecked(), place=place.isChecked(),
                                                            source=src.text().strip())
                    first = next(iter(by_expr.values()))
                    im = QImage(first.get("closed", ""))
                    if not im.isNull():
                        pv.setPixmap(QPixmap.fromImage(im).scaled(pv.width(), pv.height(), Qt.AspectRatioMode.KeepAspectRatio,
                                                                  Qt.TransformationMode.SmoothTransformation))
                    self.status.showMessage(f"立ち絵を {len(chars)} 表情 作りました（{nm}）", 8000)
                self._run_task("MMD の立ち絵（表情差分）を作る", work_v, on_done=done_v)
                return

            def work(task):
                n = len(P["states"])
                done_n = [0]

                def prog(key):
                    done_n[0] += 1
                    task.progress.emit(done_n[0], n, f"{key} をレンダリング")
                return MT.render(exe, P, progress=prog, cancel=lambda: task.cancelled)

            def done(paths):
                ch = self.mmd_tachie_finish(nm, paths, template=tmpl.isChecked(), place=place.isChecked(),
                                            source=src.text().strip())
                im = QImage(paths.get("closed", ""))
                if not im.isNull():
                    pv.setPixmap(QPixmap.fromImage(im).scaled(pv.width(), pv.height(), Qt.AspectRatioMode.KeepAspectRatio,
                                                              Qt.TransformationMode.SmoothTransformation))
                self.status.showMessage(f"立ち絵「{ch['name']}」を作りました（{len(paths)} 枚）", 8000)
            self._run_task("MMD の立ち絵を作る", work, on_done=done)

        b.clicked.connect(pick)
        bv.clicked.connect(pick_vpd)
        b_bl.clicked.connect(open_blender)
        b_go.clicked.connect(go)
        src.editingFinished.connect(do_probe)
        dlg.show()
        if path:
            do_probe()
        self._mmd_dlg = dlg
        return dlg

    def mmd_tachie_finish(self, name: str, paths: dict, template: bool = True, place: bool = True, source: str = "") -> dict:
        """レンダリングした絵 → キャラクター（目パチ・口パク）。テンプレートにも保存。"""
        from . import mmdtachie as MT
        from . import tachie as T
        imgs = MT.load_images(paths)
        params = {"bounce": False}
        ch = T.save_character(name, imgs, params, source=source)
        ch["family"] = name
        if template:
            T.save_template(name, None, {}, {}, params, imgs)
        self.add_character(ch, place)
        return ch

    def mmd_tachie_finish_variants(self, name: str, by_expr: dict, template: bool = True, place: bool = True,
                                   source: str = "") -> list:
        """表情差分 → キャラクター（「名前（表情）」・同じ family）。1 つ目だけ置く。"""
        from . import mmdtachie as MT
        from . import tachie as T
        out = []
        # 表情ごとに大きさがそろうよう、全部が入る範囲で切り抜く
        allp = {f"{i}:{k}": p for i, d in enumerate(by_expr.values()) for k, p in d.items()}
        imgs_all = MT.load_images(allp)
        for i, ex in enumerate(by_expr):
            imgs = {k.split(":", 1)[1]: im for k, im in imgs_all.items() if k.startswith(f"{i}:")}
            nm = name if ex == "通常" else f"{name}（{ex}）"
            params = {"bounce": False}
            ch = T.save_character(nm, imgs, params, source=source)
            ch["family"] = name
            ch["expression"] = ex
            if template:
                T.save_template(nm, None, {}, {}, params, imgs)
            self.add_character(ch, place and i == 0)
            out.append(ch)
        return out

    # ═════════════════════════════════════════
    #  OBS 連携
    # ═════════════════════════════════════════
    def _obs_cfg(self) -> dict:
        d = {"host": "127.0.0.1", "port": 4455, "password": "", "watch": "obs", "folder": "", "convert": "remux",
             "cut": True, "threshold": -40.0, "min_silence": 0.8, "padding": 0.15, "voice_track": 1,
             "open_sequence": True, "auto_connect": False, "proxy": False}
        d.update(getattr(SETTINGS, "obs", {}) or {})
        return d

    def _obs_save(self, d: dict):
        SETTINGS.obs = d
        try:
            SETTINGS.save()
        except Exception:
            pass

    def obs_panel(self):
        cfg = self._obs_cfg()
        dlg = QDialog(self)
        dlg.setWindowTitle("OBS 連携（録画 → 自動で取り込み・変換・カット）")
        v = QVBoxLayout(dlg)
        g1 = QGroupBox("OBS につなぐ（OBS: ツール → WebSocket サーバー設定 で「有効」に）")
        f1 = QFormLayout(g1)
        host = QLineEdit(cfg["host"])
        port = QSpinBox()
        port.setRange(1, 65535)
        port.setValue(int(cfg["port"]))
        pw = QLineEdit(cfg["password"])
        pw.setEchoMode(QLineEdit.EchoMode.Password)
        f1.addRow("アドレス", host)
        f1.addRow("ポート", port)
        f1.addRow("パスワード", pw)
        auto = QCheckBox("TeloCut を起動したら自動でつなぐ")
        auto.setChecked(bool(cfg["auto_connect"]))
        f1.addRow(auto)
        status = QLabel(self._obs_status_text())
        status.setWordWrap(True)
        f1.addRow(status)
        r = QHBoxLayout()
        b_con = QPushButton("つなぐ")
        b_dis = QPushButton("切る")
        b_rec = QPushButton("● 録画開始")
        b_stop = QPushButton("■ 録画停止")
        for w in (b_con, b_dis, b_rec, b_stop):
            r.addWidget(w)
        f1.addRow(r)
        v.addWidget(g1)

        g2 = QGroupBox("録画が終わったら")
        f2 = QFormLayout(g2)
        watch = QComboBox()
        watch.addItem("OBS の通知で（WebSocket）", "obs")
        watch.addItem("録画フォルダを見張る（OBS につながなくてよい）", "folder")
        watch.addItem("自動で処理しない", "off")
        watch.setCurrentIndex(max(0, watch.findData(cfg["watch"])))
        f2.addRow("きっかけ", watch)
        folder = QLineEdit(cfg["folder"])
        bf = QPushButton("…")
        rf = QHBoxLayout()
        rf.addWidget(folder, 1)
        rf.addWidget(bf)
        f2.addRow("録画フォルダ", rf)
        conv = QComboBox()
        conv.addItem("MP4 に入れ替え（MKV など・再エンコードなし・速い）", "remux")
        conv.addItem("編集向けに変換（NVIDIA なら NVENC）", "edit")
        conv.addItem("そのまま使う", "none")
        conv.setCurrentIndex(max(0, conv.findData(cfg["convert"])))
        f2.addRow("変換", conv)
        cut = QCheckBox("無音の所を自動でカット（話している所だけのシーケンス）")
        cut.setChecked(bool(cfg["cut"]))
        f2.addRow(cut)
        th = QDoubleSpinBox()
        th.setRange(-80, -10)
        th.setValue(float(cfg["threshold"]))
        th.setSuffix(" dB 以下を無音")
        ms = QDoubleSpinBox()
        ms.setRange(0.2, 10)
        ms.setSingleStep(0.1)
        ms.setValue(float(cfg["min_silence"]))
        ms.setSuffix(" 秒以上")
        pad = QDoubleSpinBox()
        pad.setRange(0, 2)
        pad.setSingleStep(0.05)
        pad.setValue(float(cfg["padding"]))
        pad.setSuffix(" 秒の余白")
        rr = QHBoxLayout()
        for w in (th, ms, pad):
            rr.addWidget(w)
        f2.addRow("無音", rr)
        vt = QSpinBox()
        vt.setRange(1, 6)
        vt.setValue(int(cfg["voice_track"]))
        vt.setToolTip("OBS の音声トラックの番号（1 = 全部の音 / 2 以降 = マイクだけ などを OBS の設定で分けている場合）")
        f2.addRow("声を見る音声トラック", vt)
        opn = QCheckBox("できたシーケンスを開く")
        opn.setChecked(bool(cfg["open_sequence"]))
        f2.addRow(opn)
        prox = QCheckBox("プロキシも作る（NVIDIA なら速い）")
        prox.setChecked(bool(cfg["proxy"]))
        f2.addRow(prox)
        v.addWidget(g2)
        log = QTextEdit()
        log.setReadOnly(True)
        log.setMaximumHeight(120)
        log.setPlainText("\n".join(getattr(self, "_obs_log", [])[-30:]))
        v.addWidget(log)
        bb = QDialogButtonBox()
        b_file = bb.addButton("ファイルを選んで処理…", QDialogButtonBox.ButtonRole.ActionRole)
        bb.addButton("閉じる", QDialogButtonBox.ButtonRole.RejectRole)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)

        def collect():
            d = {"host": host.text().strip() or "127.0.0.1", "port": port.value(), "password": pw.text(),
                 "watch": watch.currentData(), "folder": folder.text().strip(), "convert": conv.currentData(),
                 "cut": cut.isChecked(), "threshold": th.value(), "min_silence": ms.value(), "padding": pad.value(),
                 "voice_track": vt.value(), "open_sequence": opn.isChecked(), "auto_connect": auto.isChecked(),
                 "proxy": prox.isChecked()}
            self._obs_save(d)
            return d

        def refresh():
            status.setText(self._obs_status_text())
            log.setPlainText("\n".join(getattr(self, "_obs_log", [])[-30:]))
            if not folder.text() and getattr(self, "_obs_record_dir", ""):
                folder.setText(self._obs_record_dir)

        def con():
            collect()
            self._obs_connect()
            refresh()

        def dis():
            self._obs_disconnect()
            refresh()

        def rec(start: bool):
            c = getattr(self, "_obs", None)
            if c is None:
                QMessageBox.information(dlg, "OBS", "先に OBS につないでください。")
                return
            try:
                c.start_record() if start else c.stop_record()
            except Exception as ex:
                QMessageBox.warning(dlg, "OBS", str(ex))
            refresh()

        def pick_folder():
            p = QFileDialog.getExistingDirectory(dlg, "OBS の録画フォルダ", folder.text())
            if p:
                folder.setText(p)
        b_con.clicked.connect(con)
        b_dis.clicked.connect(dis)
        b_rec.clicked.connect(lambda: rec(True))
        b_stop.clicked.connect(lambda: rec(False))
        bf.clicked.connect(pick_folder)
        b_file.clicked.connect(lambda: (collect(), self.obs_process_file_dialog()))
        t = QTimer(dlg)
        t.timeout.connect(refresh)
        t.start(1000)
        dlg.finished.connect(lambda _r: (collect(), self._obs_apply_watch()))
        dlg.show()
        self._obs_dlg = dlg
        return dlg

    def _obs_status_text(self) -> str:
        c = getattr(self, "_obs", None)
        w = getattr(self, "_obs_watcher", None)
        if c is not None and c.ws is not None:
            v = c.version.get("obsVersion", "?")
            return f"● OBS {v} につながっています" + ("（録画中）" if c.recording else "") + \
                (f"／録画フォルダ: {self._obs_record_dir}" if getattr(self, "_obs_record_dir", "") else "")
        if w is not None:
            return f"録画フォルダを見張っています: {w.folder}"
        return "つながっていません"

    def _obs_logline(self, s: str):
        self._obs_log = (getattr(self, "_obs_log", []) + [time.strftime("%H:%M:%S ") + s])[-200:]
        self.status.showMessage("OBS: " + s, 6000)

    def _obs_connect(self, quiet: bool = False):
        from . import obslink as OL
        cfg = self._obs_cfg()
        self._obs_disconnect()
        c = OL.ObsClient(cfg["host"], cfg["port"], cfg["password"],
                         on_recorded=lambda p: self._ui(lambda: self._obs_on_recorded(p, "obs")),
                         on_event=lambda t, d: self._ui(lambda: self._obs_event(t, d)))
        try:
            v = c.connect()
        except Exception as ex:
            if not quiet:
                QMessageBox.warning(self, "OBS 連携", f"OBS につながりません: {ex}\n\n"
                                                    "OBS の「ツール → WebSocket サーバー設定」で有効にし、ポートとパスワードを確かめてください。")
            self._obs_logline(f"つながりません: {ex}")
            return False
        self._obs = c
        try:
            self._obs_record_dir = c.record_directory()
        except Exception:
            self._obs_record_dir = ""
        self._obs_logline(f"OBS {v.get('obsVersion', '')} につながりました")
        self._obs_apply_watch()
        return True

    def _obs_disconnect(self):
        c = getattr(self, "_obs", None)
        if c is not None:
            c.close()
        self._obs = None

    def _obs_event(self, et: str, d: dict):
        if et == "_closed":
            if getattr(self, "_obs", None) is not None:
                self._obs_logline("OBS との接続が切れました")
                self._obs = None
        elif et == "RecordStateChanged":
            st = d.get("outputState", "")
            if st == "OBS_WEBSOCKET_OUTPUT_STARTED":
                self._obs_logline("録画が始まりました")

    def _obs_apply_watch(self):
        """きっかけの設定（OBS の通知 / フォルダの見張り）を反映。"""
        from . import obslink as OL
        cfg = self._obs_cfg()
        w = getattr(self, "_obs_watcher", None)
        if w is not None:
            w.stop()
            self._obs_watcher = None
        if cfg["watch"] == "folder":
            folder = cfg["folder"] or getattr(self, "_obs_record_dir", "")
            if folder and os.path.isdir(folder):
                self._obs_watcher = OL.FolderWatcher(folder, lambda p: self._ui(lambda: self._obs_on_recorded(p, "folder")))
                self._obs_watcher.start()
                self._obs_logline(f"録画フォルダを見張ります: {folder}")

    def _obs_on_recorded(self, path: str, via: str):
        cfg = self._obs_cfg()
        if cfg["watch"] == "off" or (via == "obs" and cfg["watch"] != "obs"):
            self._obs_logline(f"録画ができました: {Path(path).name}（自動の処理はしない設定）")
            return
        self._obs_logline(f"録画ができました: {Path(path).name} → 取り込みます")
        self.obs_process(path)

    def obs_process_file_dialog(self):
        p, _ = QFileDialog.getOpenFileName(self, "OBS の録画を選ぶ", self._obs_cfg().get("folder", ""),
                                           "動画 (*.mkv *.mp4 *.mov *.flv *.ts)")
        if p:
            self.obs_process(p)

    def obs_process(self, path: str, cfg: Optional[dict] = None, on_done=None):
        """録画 1 本を: 変換 → 取り込み → 無音カットのシーケンス（別スレッド → 画面のスレッドで並べる）。"""
        from . import media as MD
        from . import obslink as OL
        cfg = {**self._obs_cfg(), **(cfg or {})}
        fps = self.project.sequence.fps
        out_dir = os.path.join(str(Path(self.project_path).parent) if getattr(self, "project_path", None)
                               else SETTINGS.local_media_dir, "OBS")

        def work(task, path):
            # 書き終わるまで待つ（OBS の通知の直後はまだ閉じていないことがある）
            for _ in range(100):
                if not OL._locked(path):
                    break
                time.sleep(0.3)
            task.progress.emit(0, 0, "変換中…")
            main, extras = OL.prepare(path, out_dir, cfg["convert"], cancel=lambda: task.cancelled)
            task.progress.emit(0, 0, "音声を読み込み中…")
            items = [MD.probe(main)] + [MD.probe(x) for x in extras]
            for it in items:
                if it.has_audio or it.kind == "audio":
                    MD.build_audio_cache(it.path)
                    MD.build_peaks(it.path)
            v = items[0]
            total = max(1, int(round((v.duration_s or 0) * fps)))
            ranges = [(0, total)]
            if cfg["cut"]:
                task.progress.emit(0, 0, "無音の所を探しています…")
                vi = int(cfg["voice_track"]) - 1
                voice = items[vi].path if 0 < vi < len(items) else v.path
                ranges = OL.speech_ranges(voice, total, fps, cfg["threshold"], cfg["min_silence"], cfg["padding"]) or [(0, total)]
            return items, ranges

        def done(res):
            items, ranges = res
            self.obs_place(path, items, ranges, cfg)
            if on_done:
                on_done(items, ranges)
        return self._run_task("OBS の録画を取り込み", work, path, on_done=done)

    def obs_place(self, path: str, items: list, ranges: list, cfg: dict):
        from . import obslink as OL
        seq0 = self.project.sequence
        v = items[0]
        self.begin_edit()
        b = next((x for x in self.project.bins if x.name == "OBS 録画"), None)
        if b is None:
            b = M.Bin(M.new_id(), "OBS 録画")
            self.project.bins.append(b)
        for it in items:
            self.project.media.append(it)
            b.add("media", it.id)
        name = "OBS " + Path(path).stem
        seq = OL.build_sequence(self.project, name, v, items[1:], ranges, seq0.fps, v.width or seq0.width,
                                v.height or seq0.height, seq0.start_tc)
        self.project.sequences.append(seq)
        b.add("sequence", seq.id)
        cut = sum(e - s for s, e in ranges)
        total = max(1, int(round((v.duration_s or 0) * seq0.fps)))
        self.end_edit(f"OBS の録画を取り込み（{len(ranges)} カット）")
        self._obs_logline(f"{Path(path).name}: {len(ranges)} 個に分けて {cut / seq0.fps:.0f} 秒"
                          f"（元 {total / seq0.fps:.0f} 秒・{100 - 100 * cut // total}% 詰めた）")
        if cfg.get("open_sequence", True):
            self.load_sequence(seq.id)
        if cfg.get("proxy"):
            try:
                self.make_proxies([v])
            except Exception:
                pass
        try:
            self.bin.refresh()
        except Exception:
            pass
        return seq

    # ═════════════════════════════════════════
    #  Blackmagic（Desktop Video）
    # ═════════════════════════════════════════
    def bmd_info(self):
        from . import decklink as DK
        if not DK.installed():
            QMessageBox.information(self, "Blackmagic", "Blackmagic Desktop Video（ドライバ）が入っていません。\n"
                                                        "Blackmagic Design のサイトから Desktop Video を入れてください。")
            return
        devs = DK.devices()
        dlg = QDialog(self)
        dlg.setWindowTitle("Blackmagic（Desktop Video）")
        v = QVBoxLayout(dlg)
        lines = [f"Desktop Video（DeckLink API）{DK.api_version()}", ""]
        if not devs:
            lines.append("機器が見つかりません（つないで電源を入れてください）")
        for d in devs:
            modes = DK.display_modes(d["index"]) if d["output"] else []
            seq = self.project.sequence
            best = DK.best_mode(modes, seq.width, seq.height, seq.fps) if modes else None
            lines.append(f"● {d['name']}（{d['model']}）　入力 {'○' if d['input'] else '×'}・出力 {'○' if d['output'] else '×'}")
            if best:
                lines.append(f"　　このシーケンス（{seq.width}×{seq.height} {seq.fps:g}）の出力: {best['name']}")
            if modes:
                lines.append("　　出力できる形式: " + "、".join(m["name"] for m in modes[:14]) + (" …" if len(modes) > 14 else ""))
        t = QTextEdit("\n".join(lines))
        t.setReadOnly(True)
        v.addWidget(t)
        r = QHBoxLayout()
        b1 = QPushButton("Desktop Video Setup を開く")
        b1.clicked.connect(lambda: os.startfile(DK.SETUP_EXE) if os.path.exists(DK.SETUP_EXE) else None)
        b2 = QPushButton("Media Express を開く")
        b2.setEnabled(os.path.exists(DK.MEDIA_EXPRESS))
        b2.clicked.connect(lambda: os.startfile(DK.MEDIA_EXPRESS))
        b3 = QPushButton("カラーバーを出す（3 秒）")
        b3.setEnabled(any(d["output"] for d in devs))
        b3.clicked.connect(self.bmd_test_bars)
        for b in (b1, b2, b3):
            r.addWidget(b)
        v.addLayout(r)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        dlg.resize(640, 360)
        dlg.show()
        self._bmd_dlg = dlg
        return lines

    def bmd_test_bars(self):
        from . import decklink as DK
        seq = self.project.sequence

        def run():
            th = DK.OutputThread(int(getattr(SETTINGS, "bmd_device", 0)), seq.width, seq.height, seq.fps)
            th.start()
            th.ready.wait(10)
            bars = DK.test_pattern(1920, 1080)
            t = time.time()
            while time.time() - t < 3 and not th.error:
                th.push(bars)
                time.sleep(1 / 30)
            th.stop()
        threading.Thread(target=run, daemon=True).start()

    def bmd_toggle_output(self, on: Optional[bool] = None, auto: bool = False):
        """レコードモニターの絵を Blackmagic の機器に出す / やめる（auto = 機器の抜き差しによるもの）。"""
        from . import decklink as DK
        cur = getattr(self, "_bmd_out", None)
        on = (cur is None) if on is None else on
        if not auto:
            self._bmd_want = bool(on)          # 機器を外してつなぎ直したら、また出す
        if cur is not None:
            cur.stop()
            self._bmd_out = None
            try:
                self.record_player.image_changed.disconnect(self._bmd_on_image)
            except (TypeError, RuntimeError):
                pass
            self.status.showMessage("Blackmagic への出力をやめました", 4000)
        if not on:
            return None
        if not DK.installed():
            QMessageBox.information(self, "Blackmagic", "Blackmagic Desktop Video（ドライバ）が入っていません。")
            return None
        seq = self.project.sequence
        th = DK.OutputThread(int(getattr(SETTINGS, "bmd_device", 0)), seq.width, seq.height, seq.fps,
                             on_status=lambda s: self._ui(lambda: self.status.showMessage(s, 8000)))
        th.start()
        self._bmd_out = th
        self.record_player.image_changed.connect(self._bmd_on_image)
        img = getattr(self.record_mon.view, "_img", None)
        if isinstance(img, QImage) and not img.isNull():
            self._bmd_on_image(img)
        return th

    def _bmd_on_image(self, img: QImage):
        th = getattr(self, "_bmd_out", None)
        if th is None or img is None or img.isNull():
            return
        im = img.convertToFormat(QImage.Format.Format_RGB888)
        w, h, bpl = im.width(), im.height(), im.bytesPerLine()
        ptr = im.constBits()
        ptr.setsize(h * bpl)
        arr = np.frombuffer(ptr, np.uint8).reshape(h, bpl)[:, :w * 3].reshape(h, w, 3).copy()
        th.push(arr)

    def _init_bmd_watch(self):
        """Blackmagic の機器をつないだ / 外したら知らせる（5 秒ごとに裏で確かめる）。"""
        from . import decklink as DK
        if not DK.installed():
            return
        self._bmd_names = None

        def check():
            def work():
                try:
                    names = [d["name"] for d in DK.devices()]
                except Exception:
                    return
                self._ui(lambda: self._bmd_changed(names))
            threading.Thread(target=work, daemon=True).start()
        self._bmd_timer = QTimer(self)
        self._bmd_timer.timeout.connect(check)
        self._bmd_timer.start(5000)
        check()

    def _bmd_changed(self, names: list):
        old = self._bmd_names
        self._bmd_names = names
        if old is None or old == names:
            return
        add = [n for n in names if n not in old]
        gone = [n for n in old if n not in names]
        if add:
            self.status.showMessage(f"Blackmagic の機器をつなぎました: {', '.join(add)}", 8000)
            if getattr(self, "_bmd_want", False) and getattr(self, "_bmd_out", None) is None:
                self.bmd_toggle_output(True, auto=True)
        if gone:
            self.status.showMessage(f"Blackmagic の機器が外れました: {', '.join(gone)}", 8000)
            if getattr(self, "_bmd_out", None) is not None:
                self.bmd_toggle_output(False, auto=True)

    # ═════════════════════════════════════════
    #  ペン（板タブ・液タブ）
    # ═════════════════════════════════════════
    def _init_pen(self):
        from PyQt6.QtWidgets import QApplication
        from . import penmode as PM
        self._pen = PM.PenState()
        self._pen_filter = PM.PenFilter(lambda: self._ui(self._pen_detected))
        QApplication.instance().installEventFilter(self._pen_filter)
        self._pen_tb = None
        if PM.mode() == "on":
            self.pen_set(True)

    def _pen_detected(self):
        from . import penmode as PM
        if PM.mode() == "auto" and not self._pen.on:
            self.pen_set(True)
            self.status.showMessage("ペン（板タブ）を検出しました: ペンモードにしました（設定 → ペン（板タブ）で変えられます）", 8000)

    def pen_set(self, on: bool, toolbar: Optional[bool] = None):
        from . import penmode as PM
        self._pen.apply(on)
        show = on if toolbar is None else toolbar
        if show and self._pen_tb is None:
            self._pen_tb = PM.make_toolbar(self)
            self.addToolBar(Qt.ToolBarArea.BottomToolBarArea, self._pen_tb)
        if self._pen_tb is not None:
            self._pen_tb.setVisible(bool(show))

    def pen_toggle_toolbar(self):
        vis = self._pen_tb is not None and self._pen_tb.isVisible()
        self.pen_set(self._pen.on, toolbar=not vis)

    def pen_mode_dialog(self):
        from . import penmode as PM
        items = [("auto", "自動（ペンを使ったら切り替える）"), ("on", "いつもペンモード"), ("off", "使わない")]
        dlg = QDialog(self)
        dlg.setWindowTitle("ペン（板タブ・液タブ）")
        f = QFormLayout(dlg)
        cb = QComboBox()
        for k, lab in items:
            cb.addItem(lab, k)
        cb.setCurrentIndex(max(0, cb.findData(PM.mode())))
        f.addRow("ペンモード", cb)
        f.addRow(QLabel("ペンモードでは:\n・押したときのぶれでドラッグにならないよう、動かし始めの距離を広げます\n"
                        "・ダブルクリック（ダブルタップ）の間隔を長く、クリップの端（トリム）をつかみやすくします\n"
                        "・大きなボタンのペン用ツールバー（再生・JKL・コマ送り・分割・リップル削除・ズーム・元に戻す）を出します\n"
                        "・右クリックはペンのサイドボタン、または長押しで"))
        tb = QCheckBox("ペン用ツールバーを出す")
        tb.setChecked(self._pen_tb is not None and self._pen_tb.isVisible())
        f.addRow(tb)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if not dlg.exec():
            return
        SETTINGS.pen_mode = cb.currentData()
        try:
            SETTINGS.save()
        except Exception:
            pass
        on = cb.currentData() == "on" or (cb.currentData() == "auto" and self._pen_filter.seen)
        self.pen_set(on, toolbar=tb.isChecked())
