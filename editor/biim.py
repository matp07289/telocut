"""biim 式（ゆっくり解説の RTA 動画）などの「素材の配置テンプレート」。

biim 式の画面: ゲーム画面 ＋ 右上の RTA タイマー（区間名・区間タイム）＋ 下の解説字幕（話者ごとの色）＋ 立ち絵 ＋ 倍速表示。
「区間: 名前」のマーカーを打つと、タイマーの下に今の区間と、その区間に入ったときのタイムが出る。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from . import model as M

LAYOUTS = [
    ("biim_full", "biim 式（ゲーム全画面＋タイマー＋字幕＋立ち絵）"),
    ("biim_side", "biim 式（ゲームを左に寄せて右に立ち絵）"),
    ("biim_43", "biim 式（4:3 のゲーム・左右に立ち絵）"),
    ("game_wipe", "ゲーム実況（右下に顔のワイプ）"),
    ("compare", "比較（2 つの映像を左右に・タイマー付き）"),
]
SPLIT_PREFIX = ("区間:", "区間：", "split:", "Split:")


def timer_text(sec: float, ms: bool = True) -> str:
    neg = sec < 0
    sec = abs(sec)
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    cs = int((sec - int(sec)) * 100)
    core = (f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}") + (f".{cs:02d}" if ms else "")
    return ("-" if neg else "") + core


def splits(seq: M.Sequence) -> List[Tuple[int, str]]:
    out = []
    for mk in seq.markers:
        for p in SPLIT_PREFIX:
            if mk.name.startswith(p):
                out.append((mk.frame, mk.name[len(p):].strip()))
    return sorted(out)


def timer_state(seq: M.Sequence, clip: M.Clip, cf: int) -> Tuple[str, str, str]:
    """(タイム, 区間名, 区間に入ったときのタイム)"""
    ms = clip.props.get("timer_ms", True)
    el = run_time(seq, clip, cf)
    cur, at = "", ""
    for f, name in splits(seq):
        if f <= cf:
            cur, at = name, timer_text(run_time(seq, clip, f), ms)
    return timer_text(el, ms), cur, at


def run_time(seq: M.Sequence, clip: M.Clip, cf: int) -> float:
    """タイマーの時間（秒）。ゲームのクリップを覚えていれば、倍速・スロー込みの「本当のプレイ時間」で数える。"""
    ids = clip.props.get("timer_clips") or []
    rate = float(clip.props.get("timer_rate", 1.0) or 1.0)
    if ids:
        from .render import src_frame
        total = 0.0
        for cid in ids:
            c = seq.find(cid)[1]
            if c is None or c.start > cf:
                continue
            f = min(cf, c.end)
            total += (src_frame(c, f) - src_frame(c, c.start)) / seq.fps
        return total * rate
    start = int(clip.props.get("timer_start", clip.start))
    return (cf - start) / seq.fps * rate


def draw_timer(d, seq, clip, cf, box, W, H):
    """RTA タイマーを描く（render._shape_image から）。"""
    from PIL import ImageFont
    t, seg, at = timer_state(seq, clip, cf)
    bh = box[3] - box[1]
    fs = max(10, int(bh * 0.62))
    f = small = None
    for fp in ("consolab.ttf", "C:/Windows/Fonts/consolab.ttf", "DejaVuSansMono-Bold.ttf",
               "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf", "arialbd.ttf"):
        try:
            f = ImageFont.truetype(fp, fs)
            break
        except Exception:
            continue
    from .render import _offline_font
    small = _offline_font(max(9, int(fs * 0.42)))
    tw = d.textlength(t, font=f) if f else len(t) * fs * 0.6
    x1 = box[2]
    x0 = x1 - tw - fs * 0.6
    d.rounded_rectangle([x0, box[1], x1, box[3]], radius=max(3, int(bh * 0.15)), fill=(10, 10, 16, 200),
                        outline=(255, 255, 255, 90), width=max(1, H // 540))
    col = clip.props.get("color", "#7CFF7C")
    from PIL import ImageColor
    try:
        rgb = ImageColor.getrgb(col)[:3]
    except ValueError:
        rgb = (124, 255, 124)
    d.text((x1 - tw - fs * 0.3, box[1] + (bh - fs) / 2 - fs * 0.08), t, font=f, fill=rgb + (255,),
           stroke_width=max(1, fs // 18), stroke_fill=(0, 0, 0, 255))
    if seg:
        line = f"{seg}  {at}" if at else seg
        sw = d.textlength(line, font=small) if small else len(line) * 7
        d.rounded_rectangle([x1 - sw - fs * 0.5, box[3] + bh * 0.08, x1, box[3] + bh * 0.08 + fs * 0.62],
                            radius=4, fill=(10, 10, 16, 180))
        d.text((x1 - sw - fs * 0.25, box[3] + bh * 0.1), line, font=small, fill=(255, 255, 255, 255))


def _track(seq: M.Sequence, name: str) -> M.Track:
    t = next((t for t in seq.video_tracks if t.name == name), None)
    if t is None:
        t = M.Track("video", name, selected=False)
        seq.video_tracks.append(t)
    return t


def _game_clips(seq: M.Sequence, sel: List[M.Clip]) -> List[M.Clip]:
    g = [c for c in sel if c.kind in ("video", "image", "nest", "color")]
    if g:
        return g
    for t in seq.video_tracks:
        g = [c for c in t.clips if c.kind == "video"]
        if g:
            return g
    return []


def apply_layout(project: M.Project, seq: M.Sequence, kind: str, selected: List[M.Clip],
                 playhead: int = 0) -> Dict[str, object]:
    """配置テンプレートを当てる。戻り値: 作った物の情報。"""
    W, H = seq.width, seq.height
    games = _game_clips(seq, selected)
    if not games:
        raise ValueError("ゲーム（映像）のクリップを選んでください")
    a, b = min(c.start for c in games), max(c.end for c in games)
    made: Dict[str, object] = {"range": (a, b)}
    cmp_pair = kind == "compare" and len(games) >= 2

    def fit(c):
        m = project.media_by_id(c.media_id) if c.media_id else None
        mw_, mh_ = (m.width, m.height) if m and m.width else (W, H)
        f = min(W / mw_, H / mh_)
        return mw_ * f, mh_ * f
    for i, c in enumerate(games):
        fw, fh = fit(c)
        if kind == "biim_side":
            s = 0.78
            c.props.update({"scale": s, "pos_x": round(-(W - fw * s) / 2 + W * 0.02, 1), "pos_y": round(-(H - fh * s) / 2 + H * 0.03, 1)})
        elif kind == "biim_43":
            s = min(1.0, (H * 4 / 3) / max(1, fw))
            c.props.update({"scale": round(s, 4), "pos_x": 0.0, "pos_y": 0.0})
        elif kind == "compare":
            s = min((W / 2) / fw, H / fh) * 0.98
            c.props.update({"scale": round(s, 4), "pos_x": round((-W / 4) if i % 2 == 0 else (W / 4), 1), "pos_y": 0.0})
        elif kind == "game_wipe":
            c.props.update({"scale": 1.0, "pos_x": 0.0, "pos_y": 0.0})
        else:
            c.props.update({"scale": 1.0, "pos_x": 0.0, "pos_y": 0.0})
    if kind in ("biim_full", "biim_side", "biim_43", "compare"):
        tt = _track(seq, "タイマー")
        tt.clips = [x for x in tt.clips if not (x.kind == "shape" and x.props.get("shape") == "rta_timer")]
        rect = [70, 3, 27, 7] if kind != "compare" else [36.5, 3, 27, 7]
        timer = M.Clip(M.new_id(), "shape", a, b - a, name="RTA タイマー",
                       props={"shape": "rta_timer", "rect": rect, "color": "#7CFF7C", "fill_opacity": 0.0,
                              "timer_start": a, "timer_ms": True,
                              "timer_clips": [c.id for c in sorted(games, key=lambda c: c.start)
                                              if c.kind in ("video", "nest", "color", "image")][: (1 if kind == "compare" else 999)]})
        tt.clips.append(timer)
        tt.sort()
        made["timer"] = timer
        # 倍速表示（速さを変えたクリップ）
        sp = _track(seq, "区間")
        sp.clips = [x for x in sp.clips if x.name != "倍速表示"]
        for c in games:
            rate = float(c.props.get("speed", 1.0) or 1.0)
            if c.props.get("speed_keys"):
                rate = max(k[1] for k in c.props["speed_keys"])
            if abs(rate - 1.0) > 1e-3:
                sp.clips.append(M.Clip(M.new_id(), "text", c.start, c.duration, name="倍速表示",
                                       props={"text": f"×{rate:g}", "style": {"font_size": 64, "font_color": "#ffffff",
                                              "stroke_enabled": True, "stroke_width": 6, "bold": True,
                                              "position_h": "left", "position_v": "top", "offset_x": 40, "offset_y": 30}}))
        sp.sort()
    if kind in ("biim_full", "biim_side", "biim_43"):
        # 立ち絵（登録したキャラクターがあれば 2 人まで）
        tr = _track(seq, "立ち絵")
        tr.clips = [x for x in tr.clips if x.kind != "character" or x.start >= b or x.end <= a]
        chars = list(project.characters or [])[:2]
        spots = {"biim_full": [(W * 0.36, H * 0.16), (-W * 0.36, H * 0.16)],
                 "biim_side": [(W * 0.39, H * 0.12), (W * 0.39, -H * 0.18)],
                 "biim_43": [(W * 0.41, H * 0.1), (-W * 0.41, H * 0.1)]}[kind]
        for i, ch in enumerate(chars):
            x, y = spots[i]
            tr.clips.append(M.Clip(M.new_id(), "character", a, b - a, name=ch.get("name", "キャラ"),
                                   props={"char_id": ch.get("id", ""), "audio_track": i, "scale": 0.45, "pos_x": round(x, 1),
                                          "pos_y": round(y, 1), "flip": x > 0}))
        tr.sort()
        made["characters"] = len(chars)
        # 解説字幕: 今あるテロップを biim の字幕の見た目に（話者ごとの色）
        n = 0
        colors = {"霊夢": "#ff5a6e", "魔理沙": "#ffd23f"}
        for sp_ in project.speakers or []:
            if sp_.get("name") and sp_.get("color"):
                colors[sp_["name"]] = sp_["color"]
        for t in seq.video_tracks:
            for c in t.clips:
                if c.kind == "text" and a <= c.start < b and c.name != "倍速表示":
                    col = colors.get(str(c.props.get("speaker", "")), "#ffffff")
                    c.props["style"] = {**(c.props.get("style") or {}), "font_size": 52, "font_color": col,
                                        "stroke_enabled": True, "stroke_color": "#202020", "stroke_width": 7,
                                        "position_v": "bottom", "offset_y": 40,
                                        "position_h": "left" if kind == "biim_side" else "center",
                                        "offset_x": 60 if kind == "biim_side" else 0}
                    n += 1
        made["subtitles"] = n
    if kind == "game_wipe":
        others = [c for c in selected if c not in games[:1] and c.kind == "video"]
        if others:
            cam = others[0]
            fw, fh = fit(cam)
            s = 0.28
            cam.props.update({"scale": s, "pos_x": round(W / 2 - W * 0.03 - fw * s / 2, 1),
                              "pos_y": round(H / 2 - H * 0.04 - fh * s / 2, 1),
                              "mask": {"shape": "rect", "rect": [0, 0, 100, 100], "radius": 6}})
            made["wipe"] = cam
    _ = (cmp_pair, playhead, math)
    return made


def add_split(seq: M.Sequence, frame: int, name: str) -> M.Marker:
    mk = M.Marker(frame, f"区間: {name}", "#7CFF7C")
    seq.markers.append(mk)
    seq.markers.sort(key=lambda m: m.frame)
    return mk
