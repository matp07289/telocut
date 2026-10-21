"""立ち絵メーカー（目パチ・口パク）とテンプレートから立ち絵を置く（MainWindow のミックスイン）。"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import QInputDialog, QMessageBox

from . import model as M


class Features13Mixin:
    def _feature13_commands(self) -> dict:
        return {"tachie_maker": self.tachie_maker, "tachie_template": self.tachie_from_template_dialog}

    def _build_feature13_menus(self):
        m = self._menu_named("YouTube")
        if m is not None:
            m.addSeparator()
            for c in ("tachie_maker", "tachie_template"):
                self._a(m, c)

    def tachie_maker(self, source: str = ""):
        """立ち絵の部品（PSD・PsdSplitter の JSON・PNG のフォルダ）から、目パチ・口パクの立ち絵を作る。"""
        from .tachie_ui import TachieMakerDialog
        dlg = TachieMakerDialog(self, source)
        self._tachie_dlg = dlg
        dlg.show()
        return dlg

    def add_character(self, ch: dict, place: bool = False, audio_track: Optional[int] = None) -> Optional[M.Clip]:
        """キャラクターをプロジェクトに入れる（place なら再生ヘッドからタイムラインに置く。口パクは選んでいる音声トラック）。"""
        self.begin_edit()
        self.project.characters = [c for c in self.project.characters if c.get("id") != ch["id"]] + [ch]
        clip = None
        if place:
            seq = self.project.sequence
            f = self.record_player.position
            ai = audio_track if audio_track is not None else next(
                (i for i, a in enumerate(seq.audio_tracks) if a.selected and a.clips),
                next((i for i, a in enumerate(seq.audio_tracks) if a.clips), 0))
            end = max((c.end for c in seq.audio_tracks[ai].clips), default=0) if seq.audio_tracks else 0
            dur = end - f if end > f + seq.fps else int(seq.fps * 10)
            p = ch.get("params") or {}
            t = M.Track("video", ch.get("name", "立ち絵")[:6], selected=False)
            seq.video_tracks.append(t)
            clip = M.Clip(M.new_id(), "character", f, max(1, dur), name=ch.get("name", "立ち絵"),
                          props={**M.default_props("character"), "char_id": ch["id"], "audio_track": ai,
                                 "scale": 0.55, "pos_x": round(seq.width * 0.3, 1), "pos_y": round(seq.height * 0.12, 1),
                                 "bounce": bool(p.get("bounce", True)), "vowel_sync": bool(p.get("vowel_sync", True))})
            t.clips.append(clip)
        self.end_edit(f"立ち絵「{ch.get('name', '')}」" + ("を置く" if place else "を追加"))
        if hasattr(self, "voice_panel"):
            try:
                self.voice_panel.refresh()
            except Exception:
                pass
        return clip

    def tachie_from_template_dialog(self):
        from . import tachie as T
        ts = T.templates()
        if not ts:
            QMessageBox.information(self, "立ち絵テンプレート", "テンプレートがありません。「立ち絵メーカー」で作って保存してください。")
            return self.tachie_maker()
        names = [t["name"] for t in ts]
        n, ok = QInputDialog.getItem(self, "立ち絵テンプレートから置く", "テンプレート:", names, 0, False)
        if ok:
            return self.tachie_from_template(ts[names.index(n)], place=True)
        return None

    def tachie_from_template(self, t: dict, place: bool = True) -> Optional[M.Clip]:
        """テンプレートの立ち絵をそのまま置く（目パチ・口パクの設定も）。"""
        from . import tachie as T
        ch = T.character_from_template(t)
        return self.add_character(ch, place)
