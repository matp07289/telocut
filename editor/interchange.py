"""他の編集ソフトとのやり取り（読み込み / 書き出し）。

対応形式
- AAF（Avid Media Composer / Premiere Pro）… 読み込み・書き出し（素材は外部ファイルへのリンク）
- Premiere Pro プロジェクト（.prproj）… 読み込み（変換）。gzip 圧縮 XML を解析
- Final Cut Pro 7 XML（Premiere の「Final Cut Pro XML」書き出し / 読み込み）… 読み込み・書き出し
- EDL（CMX3600）… 読み込み・書き出し

読み込みはすべて ImportedSequence（共通の中間データ）に変換してから TeloCut のプロジェクトに組み込む。
"""
from __future__ import annotations

import gzip
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.timecode import frames_to_timecode, timecode_to_frames

from .model import Clip, MediaItem, Project, Sequence, Track, Marker, Bin, new_id


# ─────────────────────────────────────────────
#  中間データ
# ─────────────────────────────────────────────

@dataclass
class ImportedClip:
    kind: str                  # video / audio
    track: int                 # 0 始まり
    start: int                 # シーケンスのフレーム
    duration: int
    src_in: int                # 素材先頭からのフレーム（シーケンス fps）
    path: str = ""
    name: str = ""
    link: str = ""
    media_duration: int = 0    # 素材の長さ（フレーム、分かる場合）
    props: dict = field(default_factory=dict)


@dataclass
class ImportedSequence:
    name: str
    fps: float = 29.97
    width: int = 1920
    height: int = 1080
    start_tc: str = "00:00:00:00"
    clips: List[ImportedClip] = field(default_factory=list)
    markers: List[Tuple[int, str, str]] = field(default_factory=list)   # (frame, name, comment)
    warnings: List[str] = field(default_factory=list)


def _rate_to_fps(timebase: float, ntsc: bool) -> float:
    if ntsc:
        return round(timebase * 1000 / 1001, 3)
    return float(timebase)


def _fps_to_rate(fps: float) -> Tuple[int, bool]:
    """(timebase, ntsc)"""
    nominal = int(round(fps))
    ntsc = abs(fps - nominal) > 0.001
    return nominal, ntsc


def _path_from_url(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    if u.lower().startswith("file:"):
        u = re.sub(r"^file:(//localhost)?", "", u, flags=re.I)
        u = urllib.parse.unquote(u)
        if re.match(r"^/+[A-Za-z]:", u):          # /C:/... → C:/...
            u = u.lstrip("/")
        elif u.startswith("//"):                   # UNC
            u = "\\\\" + u.lstrip("/").replace("/", "\\")
        if re.match(r"^[A-Za-z]:", u):
            u = u.replace("/", "\\")
    return u


def _path_to_url(path: str) -> str:
    p = Path(path)
    try:
        return p.absolute().as_uri().replace("file:///", "file://localhost/")
    except ValueError:
        return "file://localhost/" + urllib.parse.quote(str(p).replace("\\", "/"))


# ─────────────────────────────────────────────
#  プロジェクトへの組み込み
# ─────────────────────────────────────────────

def build_into_project(project: Project, imported: List[ImportedSequence], bin_name: str,
                       probe_fn=None) -> Tuple[List[Sequence], List[str]]:
    """中間データから素材・シーケンスを作り、新しいビンに入れる。(作ったシーケンス, 警告) を返す。"""
    from .media import probe as _probe
    probe_fn = probe_fn or _probe
    by_path: Dict[str, MediaItem] = {os.path.normcase(m.path): m for m in project.media}
    b = Bin(new_id(), bin_name)
    project.bins.append(b)
    made, warns = [], []
    for imp in imported:
        warns += imp.warnings
        seq = Sequence.create(name=imp.name or "読み込んだシーケンス", fps=imp.fps, width=imp.width,
                              height=imp.height, start_tc=imp.start_tc)
        nv = max([c.track for c in imp.clips if c.kind == "video"], default=-1) + 1
        na = max([c.track for c in imp.clips if c.kind == "audio"], default=-1) + 1
        seq.video_tracks = [Track("video", f"V{i + 1}", selected=(i == 0)) for i in range(max(3, nv))]
        seq.audio_tracks = [Track("audio", f"A{i + 1}", selected=(i < 2)) for i in range(max(3, na))]
        links: Dict[str, str] = {}
        for ic in imp.clips:
            if ic.duration <= 0:
                continue
            m = None
            if ic.path:
                key = os.path.normcase(ic.path)
                m = by_path.get(key)
                if m is None:
                    if os.path.exists(ic.path):
                        try:
                            m = probe_fn(ic.path)
                        except Exception:
                            m = None
                    if m is None:
                        # オフライン素材として登録（後で再リンク）
                        kind = "video" if ic.kind == "video" else "audio"
                        if Path(ic.path).suffix.lower() in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
                            kind = "image"
                        m = MediaItem(id=new_id(), path=ic.path, name=ic.name or Path(ic.path).name,
                                      kind=kind, duration_s=(ic.media_duration or (ic.src_in + ic.duration)) / imp.fps,
                                      fps=imp.fps, width=imp.width, height=imp.height,
                                      has_video=kind != "audio", has_audio=kind != "image")
                        warns.append(f"オフライン: {ic.path}")
                    project.media.append(m)
                    by_path[key] = m
                    b.add("media", m.id)
            if m is None:
                warns.append(f"素材の場所が分からないクリップを飛ばしました: {ic.name}")
                continue
            kind = ic.kind
            if kind == "video" and m.kind == "image":
                kind = "image"
            link = links.setdefault(ic.link, new_id()) if ic.link else ""
            tracks = seq.video_tracks if ic.kind == "video" else seq.audio_tracks
            t = tracks[ic.track]
            c = Clip(new_id(), kind, ic.start, ic.duration, ic.src_in, m.id, ic.name or m.name, link,
                     props=dict(ic.props))
            t.clips.append(c)
        for t in seq.tracks():
            t.sort()
            # 重なりがあれば後ろ優先で詰めずに残す（警告のみ）
            for a, c2 in zip(t.clips, t.clips[1:]):
                if c2.start < a.end:
                    warns.append(f"{seq.name} {t.name}: クリップの重なりを検出（{a.name} / {c2.name}）")
                    break
        seq.markers = [Marker(f, n or "", comment=cm) for f, n, cm in imp.markers]
        project.sequences.append(seq)
        b.add("sequence", seq.id)
        made.append(seq)
    return made, warns


# ─────────────────────────────────────────────
#  AAF
# ─────────────────────────────────────────────

def _edit_rate(fps: float):
    for nominal, frac in ((23.976, Fraction(24000, 1001)), (29.97, Fraction(30000, 1001)),
                          (59.94, Fraction(60000, 1001))):
        if abs(fps - nominal) < 0.01:
            return frac
    if abs(fps - round(fps)) < 0.01:
        return int(round(fps))
    return Fraction(fps).limit_denominator(1001)


def export_aaf(project: Project, seq: Sequence, path: str):
    """シーケンスを AAF に書き出す（素材は外部ファイルへリンク）。Avid / Premiere で読み込めます。"""
    import aaf2
    rate = _edit_rate(seq.fps)
    used = {}
    for _, c in seq.all_clips():
        if c.media_id and c.kind in ("video", "audio", "image"):
            m = project.media_by_id(c.media_id)
            if m:
                used[m.id] = m
    with aaf2.open(path, "w") as f:
        masters = {}
        for m in used.values():
            total = max(1, int(round((m.duration_s or 5.0) * float(rate)))) if m.kind != "image" else 10 ** 6
            src = f.create.SourceMob(m.name)
            f.content.mobs.append(src)
            desc = f.create.ImportDescriptor()
            loc = f.create.NetworkLocator()
            loc["URLString"].value = Path(m.path).absolute().as_uri()
            desc["Locator"].append(loc)
            src.descriptor = desc
            master = f.create.MasterMob(m.name)
            f.content.mobs.append(master)
            slots = {}
            if m.has_video or m.kind == "image":
                s_slot = src.create_picture_slot(rate)
                s_slot["Segment"].value = f.create.SourceClip(media_kind="picture", length=total)
                m_slot = master.create_picture_slot(rate)
                mc = f.create.SourceClip(media_kind="picture", length=total)
                mc.mob = src
                mc.slot_id = s_slot.slot_id
                m_slot["Segment"].value = mc
                slots["picture"] = m_slot.slot_id
            if m.has_audio:
                s_slot = src.create_sound_slot(rate)
                s_slot["Segment"].value = f.create.SourceClip(media_kind="sound", length=total)
                m_slot = master.create_sound_slot(rate)
                mc = f.create.SourceClip(media_kind="sound", length=total)
                mc.mob = src
                mc.slot_id = s_slot.slot_id
                m_slot["Segment"].value = mc
                slots["sound"] = m_slot.slot_id
            masters[m.id] = (master, slots)

        comp = f.create.CompositionMob(seq.name)
        f.content.mobs.append(comp)
        # タイムコードトラック（開始タイムコード）
        tc_slot = comp.create_timeline_slot(rate)
        tc = f.create.Timecode(int(round(seq.fps)), drop=False)
        tc.start = timecode_to_frames(seq.start_tc, seq.fps)
        tc_slot.segment = tc

        def build(track: Track, media_kind: str, phys: int):
            slot = comp.create_picture_slot(rate) if media_kind == "picture" else comp.create_sound_slot(rate)
            slot["PhysicalTrackNumber"].value = phys
            try:
                slot.name = track.name
            except Exception:
                pass
            sq = f.create.Sequence(media_kind=media_kind)
            slot["Segment"].value = sq
            pos = 0
            for c in track.clips:
                if c.start > pos:
                    sq.components.append(f.create.Filler(media_kind=media_kind, length=c.start - pos))
                ms = masters.get(c.media_id)
                if ms and ((media_kind == "picture" and "picture" in ms[1]) or
                           (media_kind == "sound" and "sound" in ms[1])):
                    sc = f.create.SourceClip(media_kind=media_kind, length=c.duration)
                    sc.mob = ms[0]
                    sc.slot_id = ms[1][media_kind]
                    sc.start = c.src_in if c.kind != "image" else 0
                    sq.components.append(sc)
                else:   # テロップ・カラーマット等は空き（Filler）として出力
                    sq.components.append(f.create.Filler(media_kind=media_kind, length=c.duration))
                pos = c.end
            if pos == 0:
                sq.components.append(f.create.Filler(media_kind=media_kind, length=1))

        for i, t in enumerate(seq.video_tracks):
            build(t, "picture", i + 1)
        for i, t in enumerate(seq.audio_tracks):
            build(t, "sound", i + 1)


def import_aaf(path: str) -> List[ImportedSequence]:
    """AAF のシーケンス（トップレベルのコンポジション）を読み込む。"""
    import aaf2
    out = []
    with aaf2.open(path, "r") as f:
        comps = list(f.content.toplevel()) or list(f.content.compositionmobs())
        for comp in comps:
            imp = ImportedSequence(name=comp.name or Path(path).stem)
            rate = None
            v_i = a_i = 0
            start_tc = 0
            for slot in comp.slots:
                seg = slot.segment
                mk = getattr(seg, "media_kind", None) or ""
                er = slot.edit_rate
                if mk.lower() == "timecode":
                    try:
                        start_tc = int(seg["Start"].value)
                    except Exception:
                        pass
                    continue
                if mk.lower() not in ("picture", "sound"):
                    continue
                if rate is None:
                    rate = er
                kind = "video" if mk.lower() == "picture" else "audio"
                idx = v_i if kind == "video" else a_i
                _walk_aaf_segment(seg, kind, idx, 0, er, rate, imp, f"{kind}{idx}")
                if kind == "video":
                    v_i += 1
                else:
                    a_i += 1
            fps = float(rate) if rate else 29.97
            imp.fps = round(fps, 3)
            imp.start_tc = frames_to_timecode(start_tc, imp.fps)
            # 映像と音声で同じ素材・同じ位置のものをリンク
            keymap = {}
            for c in imp.clips:
                k = (c.path, c.start, c.duration, c.src_in)
                keymap.setdefault(k, []).append(c)
            for lst in keymap.values():
                kinds = {c.kind for c in lst}
                if len(kinds) == 2:
                    lk = new_id()
                    for c in lst:
                        c.link = lk
            if imp.clips:
                out.append(imp)
    return out


def _resolve_aaf_source(clip, depth=0):
    """SourceClip をたどって (ファイルパス, 名前, 開始オフセット, 素材の長さ) を返す。"""
    try:
        mob = clip.mob
    except Exception:
        mob = None
    if mob is None or depth > 6:
        return "", "", 0, 0
    name = mob.name or ""
    offset = int(clip.start or 0)
    desc = getattr(mob, "descriptor", None)
    if desc is not None:
        try:
            for loc in desc["Locator"].value or []:
                url = loc["URLString"].value
                if url:
                    length = 0
                    try:
                        length = int(desc["Length"].value)
                    except Exception:
                        pass
                    return _path_from_url(url), name, offset, length
        except Exception:
            pass
    # MasterMob など: 該当スロットの SourceClip をさらにたどる
    try:
        slot = mob.slot_at(clip.slot_id) if hasattr(mob, "slot_at") else None
    except Exception:
        slot = None
    if slot is None:
        for s in mob.slots:
            if s.slot_id == clip.slot_id:
                slot = s
                break
    if slot is None:
        return "", name, offset, 0
    seg = slot.segment
    if seg.__class__.__name__ == "Sequence":
        comps = [c for c in seg.components if c.__class__.__name__ == "SourceClip"]
        seg = comps[0] if comps else None
    if seg is not None and seg.__class__.__name__ == "SourceClip":
        p, n, off2, length = _resolve_aaf_source(seg, depth + 1)
        return p, n or name, offset + off2, length
    return "", name, offset, 0


def _walk_aaf_segment(seg, kind, track, pos, er, seq_rate, imp, linkbase) -> int:
    cls = seg.__class__.__name__
    ratio = float(seq_rate) / float(er) if er and seq_rate else 1.0
    if cls == "Sequence":
        for comp in seg.components:
            ccls = comp.__class__.__name__
            if ccls == "Transition":
                length = int(round((comp.length or 0) * ratio))
                pos -= length
                imp.warnings.append("トランジションはカットとして読み込みました")
                continue
            pos = _walk_aaf_segment(comp, kind, track, pos, er, seq_rate, imp, linkbase)
        return pos
    length = int(round((getattr(seg, "length", 0) or 0) * ratio))
    if cls == "SourceClip":
        path, name, off, mlen = _resolve_aaf_source(seg)
        imp.clips.append(ImportedClip(kind, track, pos, length, int(round(off * ratio)), path,
                                      name or Path(path).name, media_duration=int(mlen * ratio)))
    elif cls == "OperationGroup":
        try:
            segs = list(seg["InputSegments"].value or [])
        except Exception:
            segs = []
        if segs:
            _walk_aaf_segment(segs[0], kind, track, pos, er, seq_rate, imp, linkbase)
            imp.warnings.append("エフェクト（OperationGroup）は元の素材のみ読み込みました")
    elif cls == "Selector":
        try:
            _walk_aaf_segment(seg["Selected"].value, kind, track, pos, er, seq_rate, imp, linkbase)
        except Exception:
            pass
    return pos + length


# ─────────────────────────────────────────────
#  Final Cut Pro 7 XML（Premiere 互換）
# ─────────────────────────────────────────────

def _txt(el, path, default=""):
    x = el.find(path)
    return x.text.strip() if x is not None and x.text else default


def _rate_of(el, default=(30, True)):
    r = el.find("rate")
    if r is None:
        return default
    tb = int(float(_txt(r, "timebase", str(default[0]))))
    ntsc = _txt(r, "ntsc", "FALSE").upper() == "TRUE"
    return tb, ntsc


def import_fcpxml(path: str) -> List[ImportedSequence]:
    root = ET.parse(path).getroot()
    files: Dict[str, Tuple[str, str, int]] = {}

    def file_info(fel):
        fid = fel.get("id", "")
        if fel.find("pathurl") is not None or fel.find("name") is not None:
            p = _path_from_url(_txt(fel, "pathurl"))
            info = (p, _txt(fel, "name", Path(p).name), int(float(_txt(fel, "duration", "0") or 0)))
            if fid:
                files[fid] = info
            return info
        return files.get(fid, ("", "", 0))

    out = []
    for sq in root.iter("sequence"):
        tb, ntsc = _rate_of(sq)
        fps = _rate_to_fps(tb, ntsc)
        imp = ImportedSequence(name=_txt(sq, "name", "シーケンス"), fps=fps)
        tc = sq.find("timecode")
        if tc is not None:
            imp.start_tc = _txt(tc, "string", "00:00:00:00").replace(";", ":")
        sc = sq.find("media/video/format/samplecharacteristics")
        if sc is not None:
            imp.width = int(float(_txt(sc, "width", "1920")))
            imp.height = int(float(_txt(sc, "height", "1080")))
        for kind, tag in (("video", "media/video/track"), ("audio", "media/audio/track")):
            for ti, track in enumerate(sq.findall(tag)):
                for ci in track.findall("clipitem"):
                    if _txt(ci, "enabled", "TRUE").upper() == "FALSE":
                        continue
                    start = int(float(_txt(ci, "start", "-1")))
                    end = int(float(_txt(ci, "end", "-1")))
                    cin = int(float(_txt(ci, "in", "0")))
                    cout = int(float(_txt(ci, "out", "0")))
                    if start < 0 or end < 0:      # トランジションに掛かっている
                        dur = cout - cin
                        if start < 0 and end >= 0:
                            start = end - dur
                        elif end < 0 and start >= 0:
                            end = start + dur
                        else:
                            continue
                    fel = ci.find("file")
                    p, n, fdur = file_info(fel) if fel is not None else ("", "", 0)
                    link = ""
                    lk = ci.findall("link")
                    if lk:
                        ids = sorted(_txt(l, "linkclipref") for l in lk)
                        link = "|".join(ids)
                    props = {}
                    sp = None
                    for flt in ci.iter("effect"):
                        if _txt(flt, "effectid") == "timeremap":
                            for prm in flt.iter("parameter"):
                                if _txt(prm, "parameterid") == "speed":
                                    sp = float(_txt(prm, "value", "100")) / 100.0
                    if sp and sp != 1.0:
                        props["speed"] = sp
                    imp.clips.append(ImportedClip(kind, ti, start, end - start, cin, p,
                                                  _txt(ci, "name", n), link, fdur, props))
        for mk in sq.findall("marker"):
            imp.markers.append((int(float(_txt(mk, "in", "0"))), _txt(mk, "name"), _txt(mk, "comment")))
        out.append(imp)
    return out


def export_fcpxml(project: Project, seq: Sequence, path: str):
    """Final Cut Pro 7 XML（xmeml v4）で書き出す。Premiere Pro / DaVinci Resolve で読み込めます。"""
    tb, ntsc = _fps_to_rate(seq.fps)

    def rate(parent):
        r = ET.SubElement(parent, "rate")
        ET.SubElement(r, "timebase").text = str(tb)
        ET.SubElement(r, "ntsc").text = "TRUE" if ntsc else "FALSE"

    root = ET.Element("xmeml", version="4")
    sq = ET.SubElement(root, "sequence", id="sequence-1")
    ET.SubElement(sq, "name").text = seq.name
    ET.SubElement(sq, "duration").text = str(seq.duration)
    rate(sq)
    tcel = ET.SubElement(sq, "timecode")
    rate(tcel)
    ET.SubElement(tcel, "string").text = seq.start_tc
    ET.SubElement(tcel, "frame").text = str(timecode_to_frames(seq.start_tc, seq.fps))
    ET.SubElement(tcel, "displayformat").text = "NDF"
    media = ET.SubElement(sq, "media")
    video = ET.SubElement(media, "video")
    fmt = ET.SubElement(ET.SubElement(video, "format"), "samplecharacteristics")
    ET.SubElement(fmt, "width").text = str(seq.width)
    ET.SubElement(fmt, "height").text = str(seq.height)
    audio = ET.SubElement(media, "audio")
    written = set()
    clip_ids = {}
    n = 0
    for _, c in seq.all_clips():
        n += 1
        clip_ids[c.id] = f"clipitem-{n}"

    def add_clip(track_el, c: Clip, kind: str):
        m = project.media_by_id(c.media_id) if c.media_id else None
        if m is None:
            return
        ci = ET.SubElement(track_el, "clipitem", id=clip_ids[c.id])
        ET.SubElement(ci, "name").text = c.name or m.name
        ET.SubElement(ci, "enabled").text = "TRUE" if c.enabled else "FALSE"
        mdur = int(round(m.duration_s * seq.fps)) if m.duration_s else c.src_in + c.duration
        ET.SubElement(ci, "duration").text = str(mdur)
        rate(ci)
        ET.SubElement(ci, "start").text = str(c.start)
        ET.SubElement(ci, "end").text = str(c.end)
        ET.SubElement(ci, "in").text = str(c.src_in if c.kind != "image" else 0)
        ET.SubElement(ci, "out").text = str((c.src_in if c.kind != "image" else 0) + c.duration)
        fid = f"file-{m.id}"
        if fid in written:
            ET.SubElement(ci, "file", id=fid)
        else:
            written.add(fid)
            fe = ET.SubElement(ci, "file", id=fid)
            ET.SubElement(fe, "name").text = m.name
            ET.SubElement(fe, "pathurl").text = _path_to_url(m.path)
            rate(fe)
            ET.SubElement(fe, "duration").text = str(mdur)
            fm = ET.SubElement(fe, "media")
            if m.has_video or m.kind == "image":
                v = ET.SubElement(fm, "video")
                s2 = ET.SubElement(v, "samplecharacteristics")
                ET.SubElement(s2, "width").text = str(m.width or seq.width)
                ET.SubElement(s2, "height").text = str(m.height or seq.height)
            if m.has_audio:
                a = ET.SubElement(fm, "audio")
                ET.SubElement(a, "channelcount").text = str(m.audio_channels or 2)
        sp = float(c.prop("speed", 1.0) or 1.0)
        if sp != 1.0:
            fl = ET.SubElement(ET.SubElement(ci, "filter"), "effect")
            ET.SubElement(fl, "name").text = "Time Remap"
            ET.SubElement(fl, "effectid").text = "timeremap"
            prm = ET.SubElement(fl, "parameter")
            ET.SubElement(prm, "parameterid").text = "speed"
            ET.SubElement(prm, "value").text = str(sp * 100)
        if c.link:
            for t2, o in seq.all_clips():
                if o.link == c.link:
                    lk = ET.SubElement(ci, "link")
                    ET.SubElement(lk, "linkclipref").text = clip_ids[o.id]
                    ET.SubElement(lk, "mediatype").text = "video" if t2.kind == "video" else "audio"

    for t in seq.video_tracks:
        tr = ET.SubElement(video, "track")
        for c in t.clips:
            add_clip(tr, c, "video")
    for t in seq.audio_tracks:
        tr = ET.SubElement(audio, "track")
        for c in t.clips:
            add_clip(tr, c, "audio")
    for mk in seq.markers:
        me = ET.SubElement(sq, "marker")
        ET.SubElement(me, "name").text = mk.name
        ET.SubElement(me, "comment").text = getattr(mk, "comment", "") or ""
        ET.SubElement(me, "in").text = str(mk.frame)
        ET.SubElement(me, "out").text = "-1"
    tree = ET.ElementTree(root)
    try:
        ET.indent(tree)      # Python 3.9+
    except AttributeError:
        pass
    with open(path, "wb") as fo:
        fo.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        tree.write(fo, encoding="utf-8", xml_declaration=False)


# ─────────────────────────────────────────────
#  EDL（CMX3600）
# ─────────────────────────────────────────────

def _reel(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "", Path(name).stem.upper())
    return (s or "AX")[:8]


def export_edl(project: Project, seq: Sequence, path: str, video_track: int = 0):
    """CMX3600 EDL。映像トラック1本 + 同じ位置の音声（A1/A2）をイベントとして書き出す。"""
    fps = seq.fps
    off = timecode_to_frames(seq.start_tc, fps)
    tc = lambda f: frames_to_timecode(f, fps)
    lines = [f"TITLE: {seq.name}", "FCM: NON-DROP FRAME", ""]
    n = 0
    vt = seq.video_tracks[video_track] if video_track < len(seq.video_tracks) else None
    events = []
    if vt:
        for c in vt.clips:
            if c.media_id:
                events.append(("V", c))
    for ai, t in enumerate(seq.audio_tracks[:4]):
        for c in t.clips:
            if c.media_id and not any(e[1].link and e[1].link == c.link for e in events if e[0] == "V"):
                events.append((f"A{'' if ai == 0 else ai + 1}" if ai else "A", c))
    events.sort(key=lambda e: e[1].start)
    for ch, c in events:
        m = project.media_by_id(c.media_id)
        if m is None:
            continue
        n += 1
        linked_audio = any(o.link == c.link and t.kind == "audio" for t, o in seq.all_clips()) if c.link else False
        chan = "B" if ch == "V" and linked_audio else ch
        src_in = c.src_in if c.kind != "image" else 0
        lines.append(f"{n:03d}  {_reel(m.name):<8} {chan:<5} C        "
                     f"{tc(src_in)} {tc(src_in + c.duration)} {tc(c.start + off)} {tc(c.end + off)}")
        lines.append(f"* FROM CLIP NAME: {m.name}")
        lines.append(f"* SOURCE FILE: {m.path}")
        lines.append("")
    for mk in seq.markers:
        lines.append(f"* LOC: {tc(mk.frame + off)} YELLOW  {mk.name}")
    with open(path, "w", encoding="utf-8", newline="\r\n") as fo:
        fo.write("\n".join(lines) + "\n")


_EDL_RE = re.compile(r"^(\d{3,6})\s+(\S+)\s+(\S+)\s+(C|D|W\d*)\s+(?:(\d+)\s+)?"
                     r"(\d\d:\d\d:\d\d[:;]\d\d)\s+(\d\d:\d\d:\d\d[:;]\d\d)\s+"
                     r"(\d\d:\d\d:\d\d[:;]\d\d)\s+(\d\d:\d\d:\d\d[:;]\d\d)")


def import_edl(path: str, fps: float, project: Project) -> List[ImportedSequence]:
    """EDL を読み込む。素材は「FROM CLIP NAME / SOURCE FILE」コメント → ビン内の同名素材 の順で探す。"""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    imp = ImportedSequence(name=Path(path).stem, fps=fps)
    by_name = {m.name.lower(): m for m in project.media}
    events = []
    rec_min = None
    cur = None
    for line in text.splitlines():
        if line.startswith("TITLE:"):
            imp.name = line[6:].strip() or imp.name
            continue
        mm = _EDL_RE.match(line.strip())
        if mm:
            num, reel, chan, trans, _dur, si, so, ri, ro = mm.groups()
            cur = {"reel": reel, "chan": chan.upper(), "si": timecode_to_frames(si.replace(";", ":"), fps),
                   "so": timecode_to_frames(so.replace(";", ":"), fps),
                   "ri": timecode_to_frames(ri.replace(";", ":"), fps),
                   "ro": timecode_to_frames(ro.replace(";", ":"), fps), "name": "", "file": ""}
            events.append(cur)
            rec_min = cur["ri"] if rec_min is None else min(rec_min, cur["ri"])
            continue
        if cur is not None:
            if "FROM CLIP NAME:" in line:
                cur["name"] = line.split("FROM CLIP NAME:", 1)[1].strip()
            elif "SOURCE FILE:" in line:
                cur["file"] = line.split("SOURCE FILE:", 1)[1].strip()
    if rec_min is None:
        return []
    start_frame = (rec_min // (int(round(fps)) * 3600)) * int(round(fps)) * 3600  # 1時間単位に丸める
    imp.start_tc = frames_to_timecode(start_frame, fps)
    for i, e in enumerate(events):
        path_ = e["file"]
        if not path_:
            m = by_name.get(e["name"].lower()) if e["name"] else None
            path_ = m.path if m else ""
        lk = f"edl{i}"
        ch = e["chan"]
        start, dur = e["ri"] - start_frame, e["ro"] - e["ri"]
        if "V" in ch or ch == "B":
            imp.clips.append(ImportedClip("video", 0, start, dur, e["si"], path_, e["name"] or e["reel"], lk))
        # A → A1, A2 → A2 ...、AA → A1+A2、B → 映像 + A1
        if ch in ("B", "A", "A1") or ch.startswith("V") and "A" in ch:
            a_tracks = [0]
        elif ch == "AA":
            a_tracks = [0, 1]
        else:
            mnum = re.match(r"A(\d)", ch)
            a_tracks = [int(mnum.group(1)) - 1] if mnum else []
        for ai in a_tracks:
            imp.clips.append(ImportedClip("audio", ai, start, dur, e["si"], path_, e["name"] or e["reel"], lk))
        if not path_:
            imp.warnings.append(f"素材が分かりません（リール {e['reel']} / {e['name']}）")
    return [imp]


# ─────────────────────────────────────────────
#  Premiere Pro プロジェクト（.prproj）
# ─────────────────────────────────────────────

TICKS_PER_SEC = 254016000000


def _open_prproj(path: str) -> ET.Element:
    data = Path(path).read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return ET.fromstring(data)


def import_prproj(path: str) -> List[ImportedSequence]:
    """Premiere Pro プロジェクトを解析してシーケンスを取り出す（できる範囲で変換）。

    .prproj は ObjectID / ObjectUID で互いを参照し合う XML。参照をたどって
    シーケンス → トラック → クリップ → 素材ファイル を復元する。
    エフェクト・トランジション・ネスト・タイトル等は対象外（警告に出す）。
    """
    root = _open_prproj(path)
    by_id: Dict[str, ET.Element] = {}
    by_uid: Dict[str, ET.Element] = {}
    for el in root.iter():
        oid = el.get("ObjectID")
        if oid:
            by_id[oid] = el
        uid = el.get("ObjectUID")
        if uid:
            by_uid[uid] = el

    def deref(el):
        if el is None:
            return None
        r = el.get("ObjectRef")
        if r:
            return by_id.get(r)
        r = el.get("ObjectURef")
        if r:
            return by_uid.get(r)
        return el

    def first(el, tag):
        if el is None:
            return None
        for x in el.iter(tag):
            if x is not el:
                return x
        return None

    def text_of(el, tag, default=""):
        x = first(el, tag)
        return x.text.strip() if x is not None and x.text else default

    def media_path(media_el) -> Tuple[str, str]:
        if media_el is None:
            return "", ""
        p = text_of(media_el, "ActualMediaFilePath") or text_of(media_el, "FilePath")
        return p, text_of(media_el, "Title") or Path(p).name

    def find_media(clip_el):
        """(Video|Audio)Clip → Source → MediaSource → Media"""
        src = deref(first(clip_el, "Source"))
        if src is None:
            return None
        med = first(src, "Media")
        return deref(med)

    out = []
    for sq in [e for e in root.iter("Sequence") if e.get("ObjectUID")]:
        name = text_of(sq, "Name", "シーケンス")
        imp = ImportedSequence(name=name)
        fps_ticks = None
        groups = []
        for tg in sq.iter("TrackGroup"):
            second = first(tg, "Second")
            g = deref(second) if second is not None else None
            if g is not None and g.tag in ("VideoTrackGroup", "AudioTrackGroup"):
                groups.append(g)
        for g in groups:
            kind = "video" if g.tag == "VideoTrackGroup" else "audio"
            fr = text_of(g, "FrameRate")
            if kind == "video" and fr and fr.isdigit():
                fps_ticks = int(fr)
                w = text_of(g, "FrameRect")
                m = re.match(r"\s*0,\s*0,\s*(\d+),\s*(\d+)", w or "")
                if m:
                    imp.width, imp.height = int(m.group(1)), int(m.group(2))
            tracks = []
            for tr in g.iter("Track"):
                t = deref(tr)
                if t is not None and t.tag in ("VideoClipTrack", "AudioClipTrack") and t not in tracks:
                    tracks.append(t)
            for ti, t in enumerate(tracks):
                items = []
                for it in t.iter("TrackItem"):
                    x = deref(it)
                    if x is not None and x.tag in ("VideoClipTrackItem", "AudioClipTrackItem") and x not in items:
                        items.append(x)
                for x in items:
                    start = int(text_of(x, "Start", "0") or 0)
                    end = int(text_of(x, "End", "0") or 0)
                    sub = deref(first(x, "SubClip"))
                    clip_el = deref(first(sub, "Clip")) if sub is not None else None
                    in_ticks = int(text_of(clip_el, "InPoint", "0") or 0) if clip_el is not None else 0
                    media = find_media(clip_el) if clip_el is not None else None
                    p, n = media_path(media)
                    if not p:
                        imp.warnings.append(f"{name}: 素材ファイルの無いクリップ（タイトル・ネスト等）を飛ばしました")
                        continue
                    imp.clips.append(ImportedClip(kind, ti, start, end - start, in_ticks, p,
                                                  text_of(sub, "Name") or n))
        fps = TICKS_PER_SEC / fps_ticks if fps_ticks else 29.97
        imp.fps = round(fps, 3)
        tpf = TICKS_PER_SEC / fps
        for c in imp.clips:
            c.start = int(round(c.start / tpf))
            c.duration = max(1, int(round(c.duration / tpf)))
            c.src_in = int(round(c.src_in / tpf))
        # 映像と音声のリンク（同じ素材・同じ位置）
        groups_k = {}
        for c in imp.clips:
            groups_k.setdefault((c.path, c.start, c.duration), []).append(c)
        for lst in groups_k.values():
            if {c.kind for c in lst} == {"video", "audio"}:
                lk = new_id()
                for c in lst:
                    c.link = lk
        if imp.clips:
            out.append(imp)
    if not out:
        raise ValueError("シーケンスが見つかりませんでした（Premiere のバージョンによっては未対応です。"
                         "Premiere から「Final Cut Pro XML」または AAF で書き出したファイルも読み込めます）")
    return out


def detect_and_import(path: str, project: Project, fps: float) -> List[ImportedSequence]:
    ext = Path(path).suffix.lower()
    if ext == ".aaf":
        return import_aaf(path)
    if ext == ".prproj":
        return import_prproj(path)
    if ext == ".edl":
        return import_edl(path, fps, project)
    if ext in (".xml", ".fcpxml"):
        head = Path(path).read_bytes()[:4000].decode("utf-8", "replace")
        if "<xmeml" in head:
            return import_fcpxml(path)
        if "<PremiereData" in head:
            return import_prproj(path)
        raise ValueError("この XML 形式には対応していません（Final Cut Pro 7 XML / xmeml に対応）")
    raise ValueError(f"対応していない形式です: {ext}")
