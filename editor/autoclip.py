"""自動切り抜き（配信アーカイブ → 見どころ探し・無音カット・字幕・OP/ED・音量・書き出し）の中身。Qt を使わない。

Stretheus のような「自動で 9 割作って、残りだけ手で直す」流れ:
  1. 音の大きさを測る → 無音とみなすしきい値（dB）を提案
  2. 見どころの点数: 音の大きさ・急な盛り上がり（笑い・驚き）・無音の少なさ・チャットの量と急増・配信マーカー
  3. 候補を点数の高い順に（マーカーの周りは必ず候補に）
  4. 選んだ区間ごとに: 無音を詰める（つなぎ目に短いフェード）→ OP / ED を前後に → 縦なら切り出し → 字幕 → 音量をそろえる
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

LAUGH = re.compile(r"(ｗ|w{2,}|草|笑|lol|LUL|KEKW|ww|ﾜﾛ|ワロ|爆笑|ｗｗ|OMEGALUL|www)", re.I)
HYPE = re.compile(r"(！{2,}|!{2,}|すげ|すご|うま|ナイス|nice|GG|pog|おお|まじ|マジ|神|やば|ヤバ|きた|キタ|888|８８８|パチパチ)", re.I)

ROLES = [("streamer", "配信者"), ("sub", "サブ"), ("comment", "コメント")]
ROLE_STYLE = {
    "streamer": {"font_color": "#FFFFFF", "stroke_color": "#000000", "stroke_width": 6},
    "sub": {"font_color": "#7FE3FF", "stroke_color": "#003040", "stroke_width": 6},
    "comment": {"font_color": "#FFFFFF", "stroke_enabled": False, "bg_enabled": True, "bg_color": "#1E1E2A",
                "bg_opacity": 210, "bg_padding": 18, "position_h": "right", "position_v": "top", "offset_y": 60},
}


# ─────────────────────────── 音の大きさ ───────────────────────────
@dataclass
class Loudness:
    hop: float                      # 1 つの値の長さ（秒）
    db: np.ndarray                  # 区間ごとの大きさ（dBFS）
    duration: float

    def suggest_threshold(self) -> float:
        """無音とみなすしきい値の提案: 静かな所（下から 15%）より 8 dB 上。−60〜−25 dB の間。"""
        if self.db.size == 0:
            return -40.0
        floor = float(np.percentile(self.db, 15))
        speech = float(np.percentile(self.db, 70))
        th = min(floor + 8, (floor + speech) / 2)
        return round(max(-60.0, min(-25.0, th)), 1)

    def remaining(self, threshold: float, min_silence: float = 0.5, pad: float = 0.1) -> float:
        """無音カットしたあとの長さ（秒）の見込み。"""
        keep = keep_ranges(self, threshold, min_silence, pad, 0, self.duration)
        return sum(b - a for a, b in keep)


def measure(path: str, hop: float = 0.1) -> Loudness:
    """素材の音を hop 秒ごとの dB に（音声キャッシュがあれば使う・無ければ PyAV で読む）。"""
    from .media import AudioSource, SAMPLE_RATE
    src = AudioSource(path)
    if src.data is not None:
        x = np.asarray(src.data, dtype=np.float32)
        x = np.abs(x).mean(axis=1) if x.ndim == 2 else np.abs(x)
        rate = SAMPLE_RATE
    else:
        import av
        rate = 16000
        chunks = []
        with av.open(path) as c:
            if not c.streams.audio:
                return Loudness(hop, np.zeros(0), 0.0)
            rs = av.AudioResampler(format="flt", layout="mono", rate=rate)
            for fr in c.decode(c.streams.audio[0]):
                for r in rs.resample(fr):
                    chunks.append(np.abs(r.to_ndarray().reshape(-1)))
        x = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    k = max(1, int(rate * hop))
    n = len(x) // k
    if n == 0:
        return Loudness(hop, np.zeros(0), len(x) / rate)
    blocks = x[:n * k].reshape(n, k)
    rms = np.sqrt((blocks.astype(np.float64) ** 2).mean(axis=1)) * math.sqrt(2)
    return Loudness(hop, 20 * np.log10(np.maximum(rms, 1e-6)), len(x) / rate)


def keep_ranges(ld: Loudness, threshold: float, min_silence: float = 0.5, pad: float = 0.1,
                a: float = 0.0, b: Optional[float] = None) -> List[Tuple[float, float]]:
    """[a, b) の中で残す（声がある）区間。無音が min_silence 秒以上続いた所を、前後 pad 秒残して詰める。"""
    b = ld.duration if b is None else b
    i0, i1 = int(a / ld.hop), min(len(ld.db), int(math.ceil(b / ld.hop)))
    quiet = ld.db[i0:i1] < threshold
    out, cur = [], a
    i = 0
    n = len(quiet)
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            s, e = a + i * ld.hop, a + j * ld.hop
            if e - s >= min_silence:
                cut_a = s + (pad if i > 0 else 0)
                cut_b = e - (pad if j < n else 0)
                if cut_a > cur:
                    out.append((cur, cut_a))
                cur = max(cur, cut_b)
            i = j
        else:
            i += 1
    if b > cur:
        out.append((cur, b))
    return [(round(x, 3), round(y, 3)) for x, y in out if y - x >= 0.05]


# ─────────────────────────── チャット・マーカー ───────────────────────────
def _tc(s: str) -> Optional[float]:
    m = re.match(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d+))?\s*$", s)
    if not m:
        try:
            return float(s)
        except ValueError:
            return None
    h, mi, se, fr = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(se) + (float("0." + fr) if fr else 0.0)


def load_chat(path: str) -> List[Tuple[float, str]]:
    """チャットのログ（TwitchDownloader の JSON・chat-downloader の JSON・yt-dlp の live_chat・
    「[0:12:34] 名前: 本文」の文字・CSV）を [(秒, 本文)] に。"""
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    out: List[Tuple[float, str]] = []
    st = text.lstrip()
    if st.startswith("{") or st.startswith("["):
        try:
            d = json.loads(text)
            items = d.get("comments") if isinstance(d, dict) else d
            for c in items or []:
                if not isinstance(c, dict):
                    continue
                t = c.get("content_offset_seconds", c.get("time_in_seconds", c.get("offset")))
                msg = c.get("message")
                body = msg.get("body", "") if isinstance(msg, dict) else (msg or c.get("text", ""))
                if t is not None:
                    out.append((float(t), str(body)))
            if out:
                return sorted(out)
        except ValueError:
            pass
        for line in text.splitlines():                  # yt-dlp の live_chat（1 行 1 JSON）
            try:
                d = json.loads(line)
            except ValueError:
                continue
            act = (d.get("replayChatItemAction") or {})
            ms = act.get("videoOffsetTimeMsec")
            body = ""
            for a in act.get("actions") or []:
                r = ((a.get("addChatItemAction") or {}).get("item") or {}).get("liveChatTextMessageRenderer") or {}
                body = "".join(x.get("text", "") for x in (r.get("message") or {}).get("runs") or [])
            if ms is not None:
                out.append((int(ms) / 1000.0, body))
        if out:
            return sorted(out)
    if "," in text.splitlines()[0] if text.splitlines() else False:
        try:
            rows = list(csv.reader(io.StringIO(text)))
            for r in rows:
                for k, cell in enumerate(r):
                    t = _tc(cell)
                    if t is not None:
                        out.append((t, " ".join(r[k + 1:])))
                        break
            if out:
                return sorted(out)
        except csv.Error:
            pass
    for line in text.splitlines():                      # [0:12:34] 名前: 本文 / 0:12:34 本文
        m = re.match(r"^\s*\[?(\d+:\d{2}(?::\d{2})?(?:[.,]\d+)?)\]?\s*(.*)$", line)
        if m:
            t = _tc(m.group(1))
            if t is not None:
                out.append((t, m.group(2)))
    return sorted(out)


def load_markers(path: str) -> List[Tuple[float, str]]:
    """配信マーカー（Twitch のマーカー書き出し CSV や「0:12:34 説明」の文字）を [(秒, 説明)] に。"""
    return [(t, s.strip(" ,:")) for t, s in load_chat(path)]


# ─────────────────────────── 見どころの点数 ───────────────────────────
@dataclass
class Candidate:
    start: float
    end: float
    score: float
    reasons: List[str] = field(default_factory=list)
    marker: str = ""

    @property
    def length(self) -> float:
        return self.end - self.start


def _smooth(x: np.ndarray, r: int) -> np.ndarray:
    if r <= 0 or x.size == 0:
        return x
    k = np.ones(2 * r + 1) / (2 * r + 1)
    return np.convolve(np.pad(x, r, mode="edge"), k, mode="valid")


def score_series(ld: Loudness, chat: Sequence[Tuple[float, str]] = (), step: float = 1.0) -> Dict[str, np.ndarray]:
    """1 秒ごとの点数（0〜100）と、その内訳。"""
    n = max(1, int(math.ceil(ld.duration / step)))
    per = max(1, int(round(step / ld.hop)))
    db = ld.db[:n * per]
    if db.size < n * per:
        db = np.pad(db, (0, n * per - db.size), constant_values=-90)
    blk = db.reshape(n, per)
    loud = blk.mean(axis=1)
    peak = blk.max(axis=1)
    base = _smooth(loud, 30)                                       # 1 分くらいの平均からの差 = 盛り上がり
    excite = np.clip((loud - base) / 6.0, 0, 3)
    burst = np.clip((peak - np.roll(peak, 2)) / 10.0, 0, 2)       # 急に大きくなった（笑い・驚き）
    talk = (blk > np.percentile(db, 40)).mean(axis=1)              # 声のある割合（無音が少ない）
    cnt = np.zeros(n)
    laugh = np.zeros(n)
    for t, msg in chat:
        i = int(t / step)
        if 0 <= i < n:
            cnt[i] += 1
            if LAUGH.search(msg or "") or HYPE.search(msg or ""):
                laugh[i] += 1
    dens = _smooth(cnt, 5)
    cbase = _smooth(cnt, 60) + 0.2
    spike = np.clip(dens / cbase - 1.0, 0, 4)
    react = np.clip(_smooth(laugh, 5) / (cbase * 0.3 + 0.5), 0, 3)
    raw = 1.4 * _smooth(excite, 3) + 0.8 * _smooth(burst, 2) + 0.6 * _smooth(talk, 5)
    if chat:
        raw = raw + 1.2 * spike + 1.0 * react
    s = _smooth(raw, 4)
    hi = float(np.percentile(s, 99.5)) or 1.0
    score = np.clip(s / hi * 100, 0, 100)
    return {"score": score, "excite": excite, "burst": burst, "talk": talk, "chat": dens, "spike": spike, "react": react,
            "step": np.array([step])}


def candidates(series: Dict[str, np.ndarray], markers: Sequence[Tuple[float, str]] = (), count: int = 10,
               min_len: float = 20.0, max_len: float = 90.0, gap: float = 2.0) -> List[Candidate]:
    """点数の山から候補を作る（山の前後を、点数が下がるまで広げる）。マーカーの周りは必ず候補に。"""
    s = series["score"].copy()
    step = float(series["step"][0])
    n = len(s)
    used = np.zeros(n, bool)
    out: List[Candidate] = []
    for t, desc in markers:                               # 配信マーカー: 少し前から 1 分くらい
        a = max(0.0, t - 10)
        b = min(n * step, a + max(min_len, 60.0))
        out.append(Candidate(a, b, float(s[int(a / step):int(b / step) + 1].mean()) if n else 0, ["配信マーカー"], desc or "マーカー"))
        used[int(a / step):int(b / step)] = True
    order = np.argsort(-s)
    for i in order:
        if len(out) >= count + len(markers):
            break
        if used[i] or s[i] < 20:
            continue
        thr = s[i] * 0.45
        a = i
        while a > 0 and s[a - 1] >= thr and (i - a) * step < max_len * 0.6:
            a -= 1
        b = i
        while b < n - 1 and s[b + 1] >= thr and (b - a) * step < max_len:
            b += 1
        a = max(0, a - int(5 / step))                     # 盛り上がる前のフリから
        b = min(n - 1, b + int(3 / step))
        while (b - a) * step < min_len and (a > 0 or b < n - 1):
            a, b = max(0, a - 1), min(n - 1, b + 1)
        # ほかの候補（マーカーなど）と重ならないように、山 i から見ていちばん近い使用済みの手前まで
        g = int(gap / step)
        left = np.nonzero(used[:i])[0]
        right = np.nonzero(used[i + 1:])[0]
        if left.size:
            a = max(a, int(left[-1]) + 1 + g)
        if right.size:
            b = min(b, i + int(right[0]) - g)
        if a > i or b < i:
            continue
        if (b - a + 1) * step < min_len * 0.5:
            continue
        used[a:b + 1] = True
        why = []
        for k, lab in (("excite", "声が大きい"), ("burst", "笑い・驚き"), ("spike", "チャットが急増"), ("react", "「草」「すごい」が多い")):
            if k in series and series[k][a:b + 1].max() > (1.0 if k != "burst" else 0.6):
                why.append(lab)
        out.append(Candidate(round(a * step, 2), round((b + 1) * step, 2), round(float(s[a:b + 1].mean()), 1), why or ["盛り上がり"]))
    out.sort(key=lambda c: -c.score)
    return out


# ─────────────────────────── 組み立て ───────────────────────────
def build_sequence(project, media, ranges: List[Tuple[float, float]], name: str, fps: float, width: int, height: int,
                   opener=None, ending=None, fade_frames: int = 2, vertical: bool = False, crop_x: float = 0.5):
    """素材 media の ranges（秒）をつないだシーケンスを作る（OP / ED を前後に、つなぎ目に短い音のフェード）。
    vertical=True なら 1080×1920 にして、横長の映像は crop_x（0=左端・0.5=中央・1=右端）の所を切り出す。"""
    from . import model as M
    W, H = (1080, 1920) if vertical else (width, height)
    seq = M.Sequence.create(name=name, fps=fps, width=W, height=H)
    vt, at = seq.video_tracks[0], seq.audio_tracks[0]
    f = 0

    def put(m, a_s, b_s, fade=0, main=True):
        nonlocal f
        a, b = int(round(a_s * fps)), int(round(b_s * fps))
        d = b - a
        if d <= 0:
            return
        link = M.new_id() if (m.has_video and m.has_audio) else ""
        props = {}
        if vertical and m.has_video and m.width and m.height and m.width > m.height:
            # 縦の画面いっぱいに（高さを合わせて横を切り出す）
            fit = min(W / m.width, H / m.height)
            fill = H / (m.height * fit)
            props = {"scale": round(fill, 4), "pos_x": round((0.5 - crop_x) * (m.width * fit * fill - W), 1)}
        if m.has_video or m.kind == "image":
            vt.clips.append(M.Clip(M.new_id(), "video" if m.kind == "video" else "image", f, d, a, m.id, m.name,
                                   link=link, props=dict(props)))
        if m.has_audio:
            ap = {"fade_in": fade, "fade_out": fade} if fade else {}
            at.clips.append(M.Clip(M.new_id(), "audio", f, d, a, m.id, m.name, link=link, props=ap))
        f += d
    if opener is not None:
        put(opener, 0, opener.duration_s or 3, 0, False)
    main_start = f
    for a, b in ranges:
        put(media, a, b, fade_frames if len(ranges) > 1 else 0)
    main_end = f
    if ending is not None:
        put(ending, 0, ending.duration_s or 3, 0, False)
    project.sequences.append(seq)
    return seq, main_start, main_end


def map_time(ranges: List[Tuple[float, float]], t: float, offset: float = 0.0) -> Optional[float]:
    """元の動画の時刻 t（秒）が、詰めたあとのどこになるか（無音で消えた所なら None）。"""
    acc = 0.0
    for a, b in ranges:
        if a <= t < b:
            return offset + acc + (t - a)
        acc += b - a
    return None


def role_style(role: str, base: Optional[dict] = None, vertical: bool = False) -> dict:
    st = dict(ROLE_STYLE.get(role, {}))
    st.update(base or {})
    if vertical:
        st.setdefault("font_size", 72)
        st.setdefault("offset_y", 360 if role != "comment" else 220)
    return st
