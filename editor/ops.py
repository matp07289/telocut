"""編集命令（JSON）の実行と、シーケンスの状態の要約。

AI アシスタント（Claude Code）・MCP サーバー・DavinTelop 連携が共通で使う。
命令はタイムコード文字列（シーケンスの開始タイムコード基準）またはフレーム数で位置を指定する。

対応する命令（op）:
  add_telop   {start, end, text, style?, track?}       テロップを追加
  edit_telop  {clip_id, text?, start?, end?, style?}   テロップを変更
  cut         {start, end}                             範囲を削除して詰める（全トラック）
  lift        {start, end}                             範囲を削除（隙間を残す）
  split       {at}                                     全トラックを分割
  delete_clip {clip_id, ripple?}                       クリップを削除
  move_clip   {clip_id, start}                         クリップを移動
  set_props   {clip_id, props}                         クリップのプロパティを変更（opacity, scale, volume_db など）
  add_marker  {at, name?, comment?, color?}            マーカーを追加
  add_se      {at, name, track?}                       効果音を配置（SE フォルダから名前で検索）
  place_media {media, at, track?}                      ビンの素材を配置（名前で指定）
  replace_telops {telops: [{start, end, text, style?}], track?}  テロップを一括で置き換え（DavinTelop 連携）
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from core.timecode import frames_to_timecode, normalize_timecode, timecode_to_frames
from . import model as M


def _frame(mw, v) -> int:
    seq = mw.project.sequence
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if s.lstrip("-").isdigit() and len(s) <= 6:
        return int(s)
    return timecode_to_frames(normalize_timecode(s, seq.fps), seq.fps) - mw.tc_offset(seq)


def describe_state(mw, max_clips: int = 600) -> dict:
    """AI / MCP に渡すシーケンスの要約。"""
    seq = mw.project.sequence
    off = mw.tc_offset(seq)
    tc = lambda f: frames_to_timecode(f + off, seq.fps)
    tracks = []
    n = 0
    for t in seq.video_tracks + seq.audio_tracks:
        items = []
        for c in t.clips:
            n += 1
            if n > max_clips:
                break
            m = mw.project.media_by_id(c.media_id) if c.media_id else None
            d = {"id": c.id, "kind": c.kind, "start": tc(c.start), "end": tc(c.end)}
            if c.kind == "text":
                d["text"] = c.prop("text", "")
            elif m is not None:
                d["media"] = m.name
                d["src_in"] = c.src_in
            if c.props.get("line_text"):
                d["speech"] = c.props["line_text"]
            items.append(d)
        tracks.append({"name": t.name, "kind": t.kind, "clips": items})
    return {
        "sequence": seq.name, "fps": seq.fps, "resolution": [seq.width, seq.height],
        "start_tc": seq.start_tc, "duration": tc(seq.duration), "playhead": tc(mw.record_player.position),
        "tracks": tracks,
        "markers": [{"at": tc(m.frame), "name": m.name, "comment": m.comment} for m in seq.markers],
        "media": [{"name": m.name, "kind": m.kind, "duration_s": round(m.duration_s, 2)} for m in mw.project.media[:200]],
    }


def _text_track(seq: M.Sequence, a: int, b: int, want: Optional[int] = None) -> M.Track:
    if want is not None and 0 <= want < len(seq.video_tracks):
        return seq.video_tracks[want]
    for t in reversed(seq.video_tracks):
        if not t.locked and t.is_empty(a, b):
            return t
    t = M.Track("video", f"V{len(seq.video_tracks) + 1}")
    seq.video_tracks.append(t)
    return t


def apply_ops(mw, ops: List[dict], label: str = "AI の編集") -> List[str]:
    """命令を順に実行（全体で 1 回の「元に戻す」）。結果メッセージのリストを返す。"""
    seq = mw.project.sequence
    log = []
    mw.begin_edit()
    try:
        for i, op in enumerate(ops):
            kind = op.get("op")
            try:
                if kind == "add_telop":
                    a, b = _frame(mw, op["start"]), _frame(mw, op["end"])
                    if b <= a:
                        raise ValueError("end が start より前")
                    t = _text_track(seq, a, b, op.get("track"))
                    c = M.Clip(M.new_id(), "text", a, b - a, name="テロップ",
                               props={"text": str(op.get("text", "")), "style": dict(op.get("style") or {})})
                    for k in ("text_anim", "karaoke"):
                        if op.get(k):
                            c.props[k] = op[k]
                    M.place_clips(seq, [(t, c)])
                    log.append(f"テロップ追加: {c.props['text'][:20]}")
                elif kind == "edit_telop":
                    t, c = seq.find(op["clip_id"])
                    if c is None or c.kind != "text":
                        raise ValueError("テロップが見つかりません")
                    if "text" in op:
                        c.props["text"] = str(op["text"])
                    if "style" in op:
                        c.props["style"] = {**(c.props.get("style") or {}), **op["style"]}
                    if "start" in op or "end" in op:
                        a = _frame(mw, op.get("start", c.start))
                        b = _frame(mw, op.get("end", c.end))
                        if b > a:
                            c.start, c.duration = a, b - a
                            t.sort()
                    log.append("テロップ変更")
                elif kind in ("cut", "lift"):
                    a, b = _frame(mw, op["start"]), _frame(mw, op["end"])
                    if b <= a:
                        raise ValueError("範囲が空です")
                    (M.extract if kind == "cut" else M.lift)(seq, a, b)
                    log.append(f"{'カット' if kind == 'cut' else 'リフト'}: {b - a} フレーム")
                elif kind == "split":
                    n = M.razor(seq, _frame(mw, op["at"]))
                    log.append(f"分割 {n} トラック")
                elif kind == "delete_clip":
                    M.delete_clips(seq, {op["clip_id"]}, ripple=bool(op.get("ripple")))
                    log.append("クリップ削除")
                elif kind == "move_clip":
                    t, c = seq.find(op["clip_id"])
                    if c is None:
                        raise ValueError("クリップが見つかりません")
                    M.move_clips(seq, [(c, t, t)], _frame(mw, op["start"]) - c.start)
                    log.append("クリップ移動")
                elif kind == "set_props":
                    t, c = seq.find(op["clip_id"])
                    if c is None:
                        raise ValueError("クリップが見つかりません")
                    c.props.update(op.get("props") or {})
                    log.append("プロパティ変更")
                elif kind == "add_marker":
                    seq.markers.append(M.Marker(_frame(mw, op["at"]), str(op.get("name", "")),
                                                op.get("color", "#E0B000"), str(op.get("comment", ""))))
                    seq.markers.sort(key=lambda m: m.frame)
                    log.append(f"マーカー: {op.get('name', '')}")
                elif kind == "add_se":
                    path = find_se(op.get("name", ""))
                    if not path:
                        raise ValueError(f"効果音が見つかりません: {op.get('name')}")
                    mw.place_se(path, int(op.get("track", -1)), _frame(mw, op["at"]), record_undo=False)
                    log.append(f"効果音: {Path(path).stem}")
                elif kind == "place_media":
                    name = str(op.get("media", "")).lower()
                    m = next((x for x in mw.project.media if x.name.lower() == name), None) or \
                        next((x for x in mw.project.media if name in x.name.lower()), None)
                    if m is None:
                        raise ValueError(f"素材が見つかりません: {op.get('media')}")
                    f = _frame(mw, op.get("at", mw.record_player.position))
                    tv, ta = mw._target_tracks()
                    a0 = max(0, m.mark_in)
                    b0 = m.mark_out if m.mark_out > a0 else mw.media_frames(m)
                    M.place_clips(seq, mw._clips_for_media(m, f, a0, b0 - a0, tv, ta))
                    log.append(f"素材配置: {m.name}")
                elif kind == "replace_telops":
                    tw = op.get("track")
                    tracks = [seq.video_tracks[tw]] if isinstance(tw, int) and tw < len(seq.video_tracks) else \
                        [t for t in seq.video_tracks if any(c.kind == "text" for c in t.clips)]
                    for t in tracks:
                        t.clips = [c for c in t.clips if c.kind != "text"]
                    target = tracks[0] if tracks else _text_track(seq, 0, 1)
                    for tl in op.get("telops", []):
                        a, b = _frame(mw, tl["start"]), _frame(mw, tl["end"])
                        if b > a:
                            M.clear_range(target, a, b)
                            target.clips.append(M.Clip(M.new_id(), "text", a, b - a, name="テロップ",
                                                       props={"text": tl.get("text", ""),
                                                              "style": dict(tl.get("style") or {})}))
                    target.sort()
                    log.append(f"テロップを置き換え（{len(op.get('telops', []))} 件）")
                else:
                    raise ValueError(f"未対応の命令: {kind}")
            except Exception as ex:
                log.append(f"⚠ {i + 1} 番目（{kind}）: {ex}")
    finally:
        mw.end_edit(label)
    return log


def find_se(name: str) -> str:
    from .settings import SETTINGS
    from .media import AUDIO_EXT
    d = getattr(SETTINGS, "se_dir", "")
    if not d or not os.path.isdir(d) or not name:
        return ""
    q = name.lower()
    best = ""
    for root, _, fs in os.walk(d):
        for f in fs:
            if Path(f).suffix.lower() in AUDIO_EXT:
                stem = Path(f).stem.lower()
                if stem == q:
                    return os.path.join(root, f)
                if q in stem and not best:
                    best = os.path.join(root, f)
    return best


def telops_payload(mw) -> dict:
    """DavinTelop へ渡すテロップ一覧（TelopEntry 互換）とデザイン。"""
    seq = mw.project.sequence
    off = mw.tc_offset(seq)
    items = []
    for t in seq.video_tracks:
        for c in t.clips:
            if c.kind == "text":
                items.append({"start_time": frames_to_timecode(c.start + off, seq.fps),
                              "end_time": frames_to_timecode(c.end + off, seq.fps),
                              "text": c.prop("text", ""), "style": c.prop("style") or {}, "clip_id": c.id})
    items.sort(key=lambda d: d["start_time"])
    return {"fps": seq.fps, "start_tc": seq.start_tc, "design": mw.project.design, "telops": items,
            "sequence": seq.name, "width": seq.width, "height": seq.height}


# ─────────────────────────────────────────────
#  DavinTelop とのリアルタイム連携
# ─────────────────────────────────────────────
SYNC_LABEL = "DavinTelop と同期"


def _telop_target(seq: M.Sequence) -> M.Track:
    best = max(seq.video_tracks, key=lambda t: sum(1 for c in t.clips if c.kind == "text"))
    if any(c.kind == "text" for c in best.clips):
        return best
    return _text_track(seq, 0, 1)


def sync_payload(mw) -> dict:
    """シーケンスのテロップ（全ビデオトラックのテキストクリップ）を連携用の形で。"""
    from core import telocut_link as TL
    seq = mw.project.sequence
    off = mw.tc_offset(seq)
    tc = lambda f: frames_to_timecode(f + off, seq.fps)
    items = []
    for vi, t in enumerate(seq.video_tracks):
        for c in t.clips:
            if c.kind == "text":
                items.append((c.start, vi, TL.item(c.id, tc(c.start), tc(c.end), str(c.prop("text", "")),
                                                    c.prop("style") or {}, str(c.prop("speaker", "") or ""),
                                                    c.enabled, vi + 1)))
    items = [it for _, _, it in sorted(items, key=lambda x: (x[0], x[1]))]
    design = dict(mw.project.design or {})
    return {"items": items, "design": TL.design_part(design), "hash": TL.state_hash(items, design),
            "fps": seq.fps, "width": seq.width, "height": seq.height, "start_tc": seq.start_tc,
            "sequence": seq.name}


def sync_apply(mw, items: List[dict], design: Optional[dict] = None) -> dict:
    """DavinTelop の状態を反映する。uid が同じクリップは中身だけ更新（エフェクト等はそのまま）、
    新しいものは追加、無くなったものは削除。続けて届いた変更は 1 回の「元に戻す」にまとめる。"""
    import time as _time
    seq = mw.project.sequence
    existing = {c.id: (t, c) for t in seq.video_tracks for c in t.clips if c.kind == "text"}
    n_undo = len(mw._undo)
    mw.begin_edit()
    keep = set()
    try:
        target = _telop_target(seq)
        for it in items:
            try:
                a, b = _frame(mw, it["start"]), _frame(mw, it["end"])
            except (KeyError, ValueError):
                continue
            if b <= a:
                b = a + 1
            tr = int(it.get("track") or 0)
            want_track = seq.video_tracks[tr - 1] if 0 < tr <= len(seq.video_tracks) else None
            uid = it.get("uid") or ""
            hit = existing.get(uid) if uid not in keep else None
            if hit is not None:
                t, c = hit
                if want_track is not None and want_track is not t:
                    t.clips.remove(c)
                    want_track.clips.append(c)
                    t = want_track
            else:
                t = want_track or target
                c = M.Clip(M.new_id(), "text", a, b - a, name="テロップ", props={})
                t.clips.append(c)
            c.start, c.duration = a, b - a
            c.props["text"] = str(it.get("text", ""))
            c.props["style"] = dict(it.get("style") or {})
            if it.get("speaker"):
                c.props["speaker"] = it["speaker"]
            else:
                c.props.pop("speaker", None)
            c.enabled = bool(it.get("enabled", True))
            keep.add(c.id)
        gone = {cid for cid in existing if cid not in keep}
        if gone:
            M.delete_clips(seq, gone, ripple=False)
        for t in seq.video_tracks:
            t.sort()
        if design is not None:
            d = dict(mw.project.design or {})
            d.update({k: v for k, v in design.items() if k not in ("canvas_width", "canvas_height", "frame_rate")})
            mw.project.design = d
    finally:
        mw.end_edit(SYNC_LABEL)
    # 3 秒以内に続いた同期は 1 つの取り消しにまとめる
    now = _time.time()
    if len(mw._undo) > n_undo and n_undo > 0 and mw._undo[n_undo - 1][0] == SYNC_LABEL \
            and now - getattr(mw, "_last_sync_t", 0) < 3:
        mw._undo.pop()
    mw._last_sync_t = now
    return sync_payload(mw)


def select_uids(mw, uids: List[str], seek: bool = True) -> int:
    seq = mw.project.sequence
    found = [c for t in seq.video_tracks for c in t.clips if c.id in set(uids)]
    if not found:
        return 0
    mw.canvas.selected = {c.id for c in found}
    mw.canvas.selection_changed.emit()
    mw.canvas.update()
    if seek:
        mw.seek_record(min(c.start for c in found))
    return len(found)
