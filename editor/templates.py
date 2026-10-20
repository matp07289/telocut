"""テンプレート（テロップ・Blender の 3D テロップ・プロジェクト）。Qt を使わない。

保存場所: <設定フォルダ>/templates/<種類>/<名前>.json（ユーザーが作った物）。組み込みのテンプレートはこのファイルの中。
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from . import model as M
from .settings import BASE_DIR

TEMPLATE_DIR = BASE_DIR / "templates"
PH = re.compile(r"\{([^{}]{1,20})\}")       # 文字の中の差し込み {名前}


def _dir(kind: str) -> Path:
    d = TEMPLATE_DIR / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(name: str) -> str:
    return "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or "テンプレート"


def user_templates(kind: str) -> List[dict]:
    out = []
    for p in sorted(_dir(kind).glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            d["_user"] = True
            d["_path"] = str(p)
            out.append(d)
        except (OSError, ValueError):
            pass
    return out


def save_template(kind: str, t: dict) -> str:
    d = {k: v for k, v in t.items() if not k.startswith("_")}
    p = _dir(kind) / f"{_safe(d.get('name', 'テンプレート'))}.json"
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(p)


def delete_template(kind: str, name: str) -> bool:
    p = _dir(kind) / f"{_safe(name)}.json"
    try:
        p.unlink()
        return True
    except OSError:
        return False


# ─────────────────────────── テロップのテンプレート ───────────────────────────
def _t(text, style=None, offset=0.0, dur=-1.0, track=0, **props):
    return {"kind": "text", "offset": offset, "dur": dur, "track": track,
            "props": {"text": text, "style": style or {}, **props}}


def _s(shape="band", rect=(0, 78, 100, 22), color="#000000", fill=0.6, offset=0.0, dur=-1.0, track=0, **props):
    return {"kind": "shape", "offset": offset, "dur": dur, "track": track,
            "props": {"shape": shape, "rect": list(rect), "color": color, "fill_opacity": fill, **props}}


STROKE = {"stroke_enabled": True, "stroke_color": "#000000", "stroke_width": 6}

BUILTIN_TELOPS: List[dict] = [
    {"name": "字幕（標準・下）", "category": "字幕", "duration": 3.0, "items": [
        _t("{本文}", {**STROKE, "font_size": 60, "position_v": "bottom", "offset_y": 60})]},
    {"name": "ロワーサード（名前と肩書き）", "category": "人物", "duration": 5.0, "items": [
        _s("rect", (4, 72, 44, 16), "#101018", 0.75, clip_in="slide_left", clip_out="fade"),
        _t("{名前}", {**STROKE, "font_size": 64, "position_h": "left", "position_v": "bottom", "offset_x": 110,
                      "offset_y": 165, "bold": True}, track=1, text_anim="slide", text_anim_out="fade"),
        _t("{肩書き}", {"font_size": 34, "font_color": "#9fd0ff", "position_h": "left", "position_v": "bottom",
                       "offset_x": 112, "offset_y": 120, "stroke_enabled": False}, offset=0.2, track=2,
           text_anim="fade", text_anim_out="fade")]},
    {"name": "見出し（章タイトル）", "category": "見出し", "duration": 3.0, "items": [
        _s("rect", (25, 58, 50, 1.2), "#ffd257", 1.0, clip_in="zoom_in"),
        _t("{見出し}", {**STROKE, "font_size": 110, "bold": True, "position_v": "middle", "offset_y": 0},
           track=1, text_anim="pop", text_anim_out="fade")]},
    {"name": "テロップ帯（黒帯＋白文字）", "category": "字幕", "duration": 4.0, "items": [
        _s("band", (0, 82, 100, 14), "#000000", 0.65),
        _t("{本文}", {"font_size": 54, "position_v": "bottom", "offset_y": 70, "stroke_enabled": False}, track=1)]},
    {"name": "ツッコミ（右上・赤）", "category": "演出", "duration": 1.5, "items": [
        _t("{ツッコミ}", {"font_size": 84, "font_color": "#ff3b3b", "stroke_enabled": True, "stroke_color": "#ffffff",
                       "stroke_width": 8, "bold": True, "position_h": "right", "position_v": "top", "offset_x": 60,
                       "offset_y": 60}, text_anim="pop")]},
    {"name": "強調（黄色・大きく）", "category": "演出", "duration": 1.5, "items": [
        _t("{強調}", {**STROKE, "font_size": 130, "font_color": "#ffe14d", "stroke_width": 10, "bold": True,
                    "position_v": "middle", "offset_y": 0}, text_anim="zoom", text_anim_out="fade")]},
    {"name": "吹き出し（コメント風）", "category": "演出", "duration": 3.0, "items": [
        _t("💬 {コメント}", {"font_size": 44, "font_color": "#ffffff", "stroke_enabled": False, "bg_enabled": True,
                            "bg_color": "#1e1e2a", "bg_opacity": 215, "bg_padding": 18, "position_h": "right",
                            "position_v": "top", "offset_x": 50, "offset_y": 60}, text_anim="drop", text_anim_out="fade")]},
    {"name": "速報（上の帯）", "category": "見出し", "duration": 5.0, "items": [
        _s("rect", (0, 0, 100, 11), "#c81e1e", 0.95, clip_in="slide_left"),
        _t("{速報}", {"font_size": 52, "bold": True, "position_v": "top", "offset_y": 22, "stroke_enabled": False},
           track=1, text_anim="wipe")]},
    {"name": "場所・日時（左上）", "category": "人物", "duration": 4.0, "items": [
        _s("rect", (3, 4, 28, 8), "#000000", 0.55, clip_in="fade", clip_out="fade"),
        _t("{場所}", {"font_size": 38, "position_h": "left", "position_v": "top", "offset_x": 80, "offset_y": 52,
                    "stroke_enabled": False}, track=1, text_anim="fade", text_anim_out="fade")]},
    {"name": "ゆっくり霊夢（赤い字幕）", "category": "ゆっくり", "duration": 3.0, "items": [
        _t("{セリフ}", {"font_size": 58, "font_color": "#ff5a6e", "stroke_enabled": True, "stroke_color": "#ffffff",
                      "stroke_width": 7, "position_v": "bottom", "offset_y": 70}, speaker="霊夢")]},
    {"name": "ゆっくり魔理沙（黄色い字幕）", "category": "ゆっくり", "duration": 3.0, "items": [
        _t("{セリフ}", {"font_size": 58, "font_color": "#ffd23f", "stroke_enabled": True, "stroke_color": "#2b2b2b",
                      "stroke_width": 7, "position_v": "bottom", "offset_y": 70}, speaker="魔理沙")]},
    {"name": "引用（「」）", "category": "字幕", "duration": 4.0, "items": [
        _t("「{引用}」", {"font_size": 56, "font_color": "#f2efe6", "stroke_enabled": False, "shadow_enabled": True,
                       "position_v": "middle", "offset_y": 0}, text_anim="fade", text_anim_out="fade")]},
    {"name": "エンドクレジット（1 行）", "category": "見出し", "duration": 4.0, "items": [
        _t("{クレジット}", {"font_size": 40, "font_color": "#dddddd", "stroke_enabled": False, "position_v": "bottom",
                         "offset_y": 50}, text_anim="fade", text_anim_out="fade")]},
    {"name": "カウントダウン（3・2・1）", "category": "演出", "duration": 3.0, "items": [
        _s("countdown", (40, 35, 20, 30), "#ffffff", 0.0)]},
]


def telop_templates() -> List[dict]:
    return [dict(t, _user=False) for t in BUILTIN_TELOPS] + user_templates("telop")


def placeholders(t: dict) -> List[str]:
    out: List[str] = []
    for it in t.get("items", []):
        for m in PH.findall(str(it.get("props", {}).get("text", ""))):
            if m not in out:
                out.append(m)
    return out


def apply_telop(seq: M.Sequence, t: dict, frame: int, values: Optional[Dict[str, str]] = None,
                duration_s: Optional[float] = None) -> List[M.Clip]:
    """テンプレートを frame に置く（必要なら上にトラックを足す）。{名前} は values で置き換える。"""
    fps = seq.fps
    total = int(round((duration_s or t.get("duration") or 3.0) * fps))
    values = values or {}
    items = t.get("items", [])
    ntr = max([it.get("track", 0) for it in items] + [0]) + 1
    # 空いているトラックの並びを探す（無ければ足す）
    base = None
    for i in range(len(seq.video_tracks)):
        if all(i + k < len(seq.video_tracks) and seq.video_tracks[i + k].is_empty(frame, frame + total)
               and not seq.video_tracks[i + k].locked for k in range(ntr)) and i > 0:
            base = i
            break
    if base is None:
        base = len(seq.video_tracks)
    while len(seq.video_tracks) < base + ntr:
        seq.video_tracks.append(M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False))
    made = []
    for it in items:
        off = int(round(float(it.get("offset", 0)) * fps))
        d = total - off if float(it.get("dur", -1)) < 0 else int(round(float(it["dur"]) * fps))
        if d <= 0:
            continue
        props = copy.deepcopy(it.get("props", {}))
        if "text" in props:
            props["text"] = PH.sub(lambda m: values.get(m.group(1), m.group(1)), str(props["text"]))
        c = M.Clip(M.new_id(), it["kind"], frame + off, d, name=t.get("name", "テンプレート"), props=props)
        tr = seq.video_tracks[base + int(it.get("track", 0))]
        tr.clips.append(c)
        tr.sort()
        made.append(c)
    return made


def telop_from_clips(seq: M.Sequence, name: str, clips_with_tracks, category: str = "マイテンプレート") -> dict:
    """選んだテキスト・図形のクリップからテンプレートを作る（位置・時間・重なりの関係ごと）。"""
    cl = [(t, c) for t, c in clips_with_tracks if c.kind in ("text", "shape")]
    if not cl:
        raise ValueError("テロップか図形のクリップを選んでください")
    fps = seq.fps
    a = min(c.start for _, c in cl)
    b = max(c.end for _, c in cl)
    idx = {id(t): i for i, t in enumerate(seq.video_tracks)}
    used = sorted({idx.get(id(t), 0) for t, _ in cl})
    rel = {v: k for k, v in enumerate(used)}
    items = []
    for t, c in sorted(cl, key=lambda tc: (tc[1].start, idx.get(id(tc[0]), 0))):
        items.append({"kind": c.kind, "offset": round((c.start - a) / fps, 3),
                      "dur": -1 if c.start == a and c.end == b else round(c.duration / fps, 3),
                      "track": rel[idx.get(id(t), 0)], "props": copy.deepcopy(c.props)})
    return {"name": name, "category": category, "duration": round((b - a) / fps, 3), "items": items}


# ─────────────────────────── Blender の 3D テロップ ───────────────────────────
BUILTIN_3D: List[dict] = [
    {"name": "金の見出し（回転）", "text": "{見出し}", "depth": 0.25, "bevel": 0.04, "material": "金", "anim": "回転（Y 軸）",
     "duration": 4.0},
    {"name": "銀のロゴ文字（ポップ）", "text": "{文字}", "depth": 0.2, "bevel": 0.03, "material": "銀", "anim": "ポップイン",
     "duration": 3.0},
    {"name": "クロームのタイトル（ゆれ）", "text": "{タイトル}", "depth": 0.3, "bevel": 0.05, "material": "クローム",
     "anim": "ゆれ", "duration": 5.0},
    {"name": "ネオン（光る）", "text": "{文字}", "depth": 0.08, "bevel": 0.01, "material": "ネオン（光る）", "anim": "ポップイン",
     "duration": 3.0},
    {"name": "赤いポップ文字", "text": "{文字}", "depth": 0.35, "bevel": 0.06, "material": "赤いプラスチック",
     "anim": "ポップイン", "duration": 2.5},
]


def text3d_templates() -> List[dict]:
    return [dict(t, _user=False) for t in BUILTIN_3D] + user_templates("blender3d")


# ─────────────────────────── プロジェクトのテンプレート ───────────────────────────
def project_templates() -> List[dict]:
    return [dict(t, _user=False) for t in BUILTIN_PROJECTS] + user_templates("project")


BUILTIN_PROJECTS: List[dict] = [
    {"name": "空のプロジェクト", "builtin": "empty"},
    {"name": "YouTube 解説（ゆっくり・立ち絵・字幕）", "builtin": "yukkuri"},
    {"name": "biim 式 RTA 解説", "builtin": "biim"},
    {"name": "縦型ショート（1080×1920）", "builtin": "shorts"},
    {"name": "インタビュー（2 カメラ・ロワーサード）", "builtin": "interview"},
]


def project_from_template(t: dict, name: str, w: int, h: int, fps: float, tc: str) -> M.Project:
    """テンプレートから新しいプロジェクトを作る（トラック構成・ビン・テロップのデザイン・プロジェクト設定）。"""
    proj = M.Project(name=name)
    kind = t.get("builtin", "")
    if kind == "shorts":
        w, h = 1080, 1920
    seq = M.Sequence.create(name="シーケンス 01", fps=fps, width=w, height=h, start_tc=tc)
    proj.sequences = [seq]
    bins = ["素材"]
    if kind == "yukkuri":
        seq.video_tracks = [M.Track("video", n, selected=(i == 0)) for i, n in enumerate(("背景", "映像", "立ち絵", "字幕"))]
        seq.audio_tracks = [M.Track("audio", n, selected=(i < 2)) for i, n in enumerate(("セリフ", "BGM", "SE"))]
        seq.audio_tracks[1].duck = True
        bins = ["素材", "立ち絵", "BGM・SE", "テロップ"]
        proj.settings["default_telop_template"] = "ゆっくり霊夢（赤い字幕）"
    elif kind == "biim":
        seq.video_tracks = [M.Track("video", n, selected=(i == 0)) for i, n in
                            enumerate(("ゲーム", "タイマー", "区間", "字幕", "立ち絵"))]
        seq.audio_tracks = [M.Track("audio", n, selected=(i < 2)) for i, n in enumerate(("ゲーム音", "解説", "BGM"))]
        seq.audio_tracks[2].duck = True
        bins = ["ゲーム映像", "立ち絵", "BGM・SE"]
        proj.settings["layout_template"] = "biim"
    elif kind == "interview":
        seq.video_tracks = [M.Track("video", n, selected=(i == 0)) for i, n in enumerate(("A カメラ", "B カメラ", "テロップ"))]
        seq.audio_tracks = [M.Track("audio", n, selected=(i < 2)) for i, n in enumerate(("ピンマイク", "カメラ音", "BGM"))]
        bins = ["A カメラ", "B カメラ", "音声"]
        proj.settings["default_telop_template"] = "ロワーサード（名前と肩書き）"
    elif kind == "shorts":
        bins = ["素材", "BGM"]
        proj.settings["default_telop_template"] = "強調（黄色・大きく）"
    elif not kind and t.get("project"):
        src = M.Project.from_json(t["project"])
        proj.design, proj.speakers, proj.characters = src.design, src.speakers, src.characters
        proj.settings = dict(src.settings)
        if src.sequences:
            s0 = src.sequences[0]
            seq.video_tracks = [M.Track("video", tr.name, selected=tr.selected, height=tr.height) for tr in s0.video_tracks]
            seq.audio_tracks = [M.Track("audio", tr.name, selected=tr.selected, volume_db=tr.volume_db, duck=tr.duck)
                                for tr in s0.audio_tracks]
            # テロップ・図形・カラーなど素材の要らないクリップはひな形として残す
            for st, dt in zip(s0.video_tracks, seq.video_tracks):
                dt.clips = [c.copy(id=M.new_id()) for c in st.clips if c.kind in ("text", "shape", "color", "null", "camera")]
        bins = [b.name for b in src.bins] or bins
    proj.bins = [M.Bin(M.new_id(), b) for b in bins]
    proj.settings.setdefault("format", {"width": seq.width, "height": seq.height, "fps": fps, "start_tc": tc})
    proj.settings["template"] = t.get("name", "")
    proj.ensure_bins()
    return proj


def project_template_from(project: M.Project, name: str) -> dict:
    """今のプロジェクトをテンプレートに（素材は入れない・トラック構成 / ビン / デザイン / 設定 / テロップのひな形）。"""
    p = M.Project.from_json(project.to_json())
    p.media = []
    for s in p.sequences:
        for t in s.tracks():
            t.clips = [c for c in t.clips if c.kind in ("text", "shape", "color", "null", "camera")]
    for b in p.bins:
        b.items = [it for it in b.items if it.get("type") != "media"]
    p.settings.pop("work_seconds", None)
    p.settings.pop("notes", None)
    return {"name": name, "project": p.to_json()}
