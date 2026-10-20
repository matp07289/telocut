"""TeloCut のデータモデルと編集操作。

時間はすべてシーケンスのフレーム数 (int) で扱う。
Clip.src_in はメディア先頭からのオフセット（シーケンスfps換算のフレーム）。
"""
from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ─────────────────────────────────────────────
#  データ
# ─────────────────────────────────────────────

@dataclass
class MediaItem:
    id: str
    path: str
    name: str
    kind: str                 # video / audio / image
    duration_s: float = 0.0   # 画像は 0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    has_video: bool = False
    has_audio: bool = False
    audio_channels: int = 0
    # ソースモニターのマーク（シーケンスfps換算フレーム、-1=未設定）
    mark_in: int = -1
    mark_out: int = -1
    file_size: int = 0
    rel_path: str = ""          # プロジェクトファイルからの相対パス（保存時に更新）
    original_path: str = ""     # ローカルコピーしている場合の元（NAS）のパス
    credit: str = ""            # 素材のクレジット（概要欄に出す。例: 画像の作者とライセンス）
    mtime: float = 0.0          # ファイルの更新日時（自動同期・キャッシュの判定）
    pending: str = ""           # 外部アプリの書き出し待ち（このパターンのファイルができたら自動でつなぐ）
    tags: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "MediaItem":
        keys = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in keys})


def default_props(kind: str) -> dict:
    p = {"opacity": 1.0, "scale": 1.0, "pos_x": 0.0, "pos_y": 0.0, "rotation": 0.0,
         "fade_in": 0, "fade_out": 0, "volume_db": 0.0, "effects": [],
         "speed": 1.0,            # 再生速度（負=逆再生、0=フリーズフレーム）
         "crop": [0, 0, 0, 0],    # 左・上・右・下（%）
         "chroma": None,          # {"color": "#00ff00", "tol": 0.25, "soft": 0.1}
         "anim": {},              # {"scale": [開始, 終了], "pos_x": [...], "pos_y": [...], "opacity": [...]}
         "pan": 0.0,              # -1（左）〜 1（右）
         "dissolve": 0,           # このクリップの頭に付くクロスディゾルブの長さ（フレーム）
         "label": ""}             # クリップカラー
    if kind == "text":
        p.update({"text": "テロップ", "style": {}})
    if kind == "color":
        p.update({"color": "#000000"})
    if kind == "character":
        p.update({"char_id": "", "audio_track": 0, "blink": True, "bounce": True, "flip": False})
    if kind == "shape":
        p.update({"shape": "band", "color": "#000000", "rect": [0, 78, 100, 22], "radius": 0, "fill_opacity": 0.6})
    return p


@dataclass
class Clip:
    id: str
    kind: str                 # video / audio / image / text / color
    start: int                # タイムライン上の開始フレーム
    duration: int
    src_in: int = 0
    media_id: str = ""
    name: str = ""
    link: str = ""            # 映像と音声のリンクID
    enabled: bool = True
    props: dict = field(default_factory=dict)

    @property
    def end(self) -> int:
        return self.start + self.duration

    def copy(self, **kw) -> "Clip":
        c = copy.deepcopy(self)
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def prop(self, key, default=None):
        if key in self.props:
            return self.props[key]
        return default_props(self.kind).get(key, default)

    @classmethod
    def from_dict(cls, d: dict) -> "Clip":
        keys = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in keys})


@dataclass
class Track:
    kind: str                 # video / audio
    name: str
    clips: List[Clip] = field(default_factory=list)
    locked: bool = False
    muted: bool = False       # 映像トラックでは「非表示」
    solo: bool = False
    height: int = 0
    selected: bool = True     # Avid のレコードトラックセレクター
    volume_db: float = 0.0    # トラック音量（ミキサー）
    pan: float = 0.0
    sync_lock: bool = True    # 挿入・抽出で同期を保つ（Avid のシンクロック）
    duck: bool = False        # BGM トラック: 他のトラックで音が鳴っている間は音量を下げる
    duck_db: float = -12.0

    def sort(self):
        self.clips.sort(key=lambda c: c.start)

    def clip_at(self, frame: int) -> Optional[Clip]:
        for c in self.clips:
            if c.start <= frame < c.end:
                return c
        return None

    def clips_in(self, a: int, b: int) -> List[Clip]:
        return [c for c in self.clips if c.end > a and c.start < b]

    def is_empty(self, a: int, b: int, ignore=()) -> bool:
        return not [c for c in self.clips_in(a, b) if c.id not in ignore]

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        t = cls(kind=d["kind"], name=d["name"],
                clips=[Clip.from_dict(c) for c in d.get("clips", [])],
                locked=d.get("locked", False), muted=d.get("muted", False),
                solo=d.get("solo", False), height=d.get("height", 0),
                selected=d.get("selected", True), volume_db=d.get("volume_db", 0.0),
                pan=d.get("pan", 0.0), sync_lock=d.get("sync_lock", True),
                duck=d.get("duck", False), duck_db=d.get("duck_db", -12.0))
        return t


@dataclass
class Marker:
    frame: int
    name: str = ""
    color: str = "#E0B000"
    comment: str = ""


@dataclass
class Sequence:
    name: str = "シーケンス 01"
    id: str = ""
    fps: float = 29.97
    width: int = 1920
    height: int = 1080
    start_tc: str = "00:00:00:00"
    video_tracks: List[Track] = field(default_factory=list)   # [0] = V1（最下層）
    audio_tracks: List[Track] = field(default_factory=list)
    markers: List[Marker] = field(default_factory=list)
    mark_in: int = -1
    mark_out: int = -1
    playhead: int = 0
    target_video: int = 0
    target_audio: int = 0
    master_db: float = 0.0

    @classmethod
    def create(cls, **kw) -> "Sequence":
        s = cls(**kw)
        if not s.id:
            s.id = new_id()
        if not s.video_tracks:
            s.video_tracks = [Track("video", f"V{i + 1}", selected=(i == 0)) for i in range(3)]
        if not s.audio_tracks:
            s.audio_tracks = [Track("audio", f"A{i + 1}", selected=(i < 2)) for i in range(3)]
        return s

    # ── 参照 ──
    def tracks(self) -> List[Track]:
        return self.video_tracks + self.audio_tracks

    def all_clips(self):
        for t in self.tracks():
            for c in t.clips:
                yield t, c

    def find(self, clip_id: str) -> Tuple[Optional[Track], Optional[Clip]]:
        for t, c in self.all_clips():
            if c.id == clip_id:
                return t, c
        return None, None

    def linked(self, clip: Clip) -> List[Tuple[Track, Clip]]:
        if not clip.link:
            return []
        return [(t, c) for t, c in self.all_clips() if c.link == clip.link and c.id != clip.id]

    @property
    def duration(self) -> int:
        return max((c.end for _, c in self.all_clips()), default=0)

    def edit_points(self) -> List[int]:
        pts = {0}
        for _, c in self.all_clips():
            pts.add(c.start)
            pts.add(c.end)
        return sorted(pts)

    def nominal_fps(self) -> int:
        return max(1, int(round(self.fps)))

    @classmethod
    def from_dict(cls, d: dict) -> "Sequence":
        s = cls(**{k: v for k, v in d.items()
                   if k in cls.__dataclass_fields__ and k not in
                   ("video_tracks", "audio_tracks", "markers")})
        s.video_tracks = [Track.from_dict(t) for t in d.get("video_tracks", [])]
        s.audio_tracks = [Track.from_dict(t) for t in d.get("audio_tracks", [])]
        s.markers = [Marker(**m) for m in d.get("markers", [])]
        return s


@dataclass
@dataclass
class Bin:
    """Avid のビン。素材（media）とシーケンスを入れる入れ物。"""
    id: str
    name: str
    items: List[dict] = field(default_factory=list)    # {"type": "media"/"sequence", "id": ...}
    view: str = "text"                                  # text / frame

    def ids(self, typ: str) -> List[str]:
        return [it["id"] for it in self.items if it.get("type") == typ]

    def add(self, typ: str, item_id: str):
        if not any(it.get("type") == typ and it.get("id") == item_id for it in self.items):
            self.items.append({"type": typ, "id": item_id})

    def remove(self, item_id: str):
        self.items = [it for it in self.items if it.get("id") != item_id]


@dataclass
class Project:
    media: List[MediaItem] = field(default_factory=list)
    sequences: List[Sequence] = field(default_factory=list)
    current: int = 0
    design: dict = field(default_factory=dict)   # テロップ共通デザイン (core.models.DesignSettings)
    bins: List[Bin] = field(default_factory=list)
    name: str = ""
    characters: List[dict] = field(default_factory=list)   # 立ち絵（ゆっくり等）
    speakers: List[dict] = field(default_factory=list)     # 話者設定（声・字幕色・キャラ）
    watch_folders: List[str] = field(default_factory=list) # 監視フォルダ（新しいファイルを自動取り込み）

    @property
    def sequence(self) -> Sequence:
        if not self.sequences:
            self.sequences.append(Sequence.create())
        self.current = max(0, min(self.current, len(self.sequences) - 1))
        return self.sequences[self.current]

    def character(self, cid: str) -> Optional[dict]:
        return next((c for c in self.characters if c.get("id") == cid), None)

    def media_by_id(self, mid: str) -> Optional[MediaItem]:
        for m in self.media:
            if m.id == mid:
                return m
        return None

    def sequence_by_id(self, sid: str) -> Optional[Sequence]:
        return next((s for s in self.sequences if s.id == sid), None)

    def bin_by_id(self, bid: str) -> Optional[Bin]:
        return next((b for b in self.bins if b.id == bid), None)

    def bin_of(self, item_id: str) -> Optional[Bin]:
        return next((b for b in self.bins if any(it.get("id") == item_id for it in b.items)), None)

    def ensure_bins(self):
        """ビンが無い（旧形式）プロジェクトには既定のビンを作り、どのビンにも無い物を入れる。"""
        for sq in self.sequences:
            if not sq.id:
                sq.id = new_id()
        if not self.bins:
            self.bins.append(Bin(new_id(), f"{self.name or 'プロジェクト'} ビン"))
        placed = {it.get("id") for b in self.bins for it in b.items}
        for m in self.media:
            if m.id not in placed:
                self.bins[0].add("media", m.id)
        for sq in self.sequences:
            if sq.id not in placed:
                self.bins[0].add("sequence", sq.id)
        valid = {m.id for m in self.media} | {sq.id for sq in self.sequences}
        for b in self.bins:
            b.items = [it for it in b.items if it.get("id") in valid]

    def to_json(self) -> str:
        return json.dumps({"app": "TeloCut", "version": 2, **asdict(self)},
                          ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "Project":
        d = json.loads(text)
        p = cls(media=[MediaItem.from_dict(m) for m in d.get("media", [])],
                sequences=[Sequence.from_dict(s) for s in d.get("sequences", [])],
                current=d.get("current", 0), design=d.get("design", {}),
                bins=[Bin(**{k: v for k, v in b.items() if k in Bin.__dataclass_fields__})
                      for b in d.get("bins", [])],
                name=d.get("name", ""), characters=d.get("characters", []),
                speakers=d.get("speakers", []), watch_folders=d.get("watch_folders", []))
        p.ensure_bins()
        return p


# ─────────────────────────────────────────────
#  トラック単位の基本操作
# ─────────────────────────────────────────────

def split_clip(track: Track, frame: int) -> Optional[Tuple[Clip, Clip]]:
    """frame でクリップを2つに分割。分割したら (左, 右) を返す。"""
    c = track.clip_at(frame)
    if c is None or frame <= c.start or frame >= c.end:
        return None
    off = frame - c.start
    sp = float(c.props.get("speed", 1.0) or 0.0)
    if c.kind in ("video", "audio"):
        new_src = c.src_in + int(round(off * sp)) if sp >= 0 else c.src_in
    else:
        new_src = c.src_in
    right = c.copy(id=new_id(), start=frame, duration=c.duration - off, src_in=new_src)
    right.props["dissolve"] = 0
    if sp < 0:          # 逆再生: 左側の素材開始位置をずらす
        c.src_in = c.src_in + int(round((c.duration - off) * -sp))
    c.duration = off
    # フェードは端側だけ残す
    if c.props.get("fade_out"):
        right.props["fade_out"] = c.props["fade_out"]
        c.props["fade_out"] = 0
    if right.props.get("fade_in"):
        right.props["fade_in"] = 0
    track.clips.append(right)
    track.sort()
    return c, right


def clear_range(track: Track, a: int, b: int, ignore=()) -> None:
    """[a, b) を空ける（上書きの基本操作）。"""
    if b <= a:
        return
    split_clip(track, a)
    split_clip(track, b)
    track.clips = [c for c in track.clips
                   if c.id in ignore or not (c.start >= a and c.end <= b)]


def shift_after(track: Track, at: int, delta: int, ignore=()) -> None:
    """at 以降に始まるクリップを delta だけずらす。"""
    for c in track.clips:
        if c.start >= at and c.id not in ignore:
            c.start += delta
    track.sort()


def insert_gap(track: Track, at: int, length: int) -> None:
    split_clip(track, at)
    shift_after(track, at, length)


def remove_range_ripple(track: Track, a: int, b: int) -> None:
    clear_range(track, a, b)
    shift_after(track, b, -(b - a))


# ─────────────────────────────────────────────
#  シーケンス単位の編集（Avid / Premiere の基本編集）
# ─────────────────────────────────────────────

def _unlocked(seq: Sequence) -> List[Track]:
    return [t for t in seq.tracks() if not t.locked]


def place_clips(seq: Sequence, placements: List[Tuple[Track, Clip]], mode: str = "overwrite") -> None:
    """placements = [(track, clip)] を配置。mode: overwrite / insert。
    insert は全アンロックトラックに隙間を作って同期を保つ（スプライスイン）。"""
    if not placements:
        return
    a = min(c.start for _, c in placements)
    b = max(c.end for _, c in placements)
    if mode == "insert":
        targets = {id(t) for t, _ in placements}
        for t in _unlocked(seq):
            if id(t) in targets or t.sync_lock:
                insert_gap(t, a, b - a)
    for t, c in placements:
        if t.locked:
            continue
        clear_range(t, c.start, c.end)
        t.clips.append(c)
        t.sort()


def lift(seq: Sequence, a: int, b: int, tracks: Optional[List[Track]] = None) -> None:
    """範囲を削除して隙間を残す（リフト）。"""
    for t in (tracks if tracks is not None else _unlocked(seq)):
        if not t.locked:
            clear_range(t, a, b)


def extract(seq: Sequence, a: int, b: int, tracks: Optional[List[Track]] = None) -> None:
    """範囲を削除して詰める（抽出）。tracks 省略時は全アンロックトラックで同期を保つ。"""
    for t in (tracks if tracks is not None else _unlocked(seq)):
        if not t.locked:
            remove_range_ripple(t, a, b)


def razor(seq: Sequence, frame: int, tracks: Optional[List[Track]] = None) -> int:
    n = 0
    for t in (tracks if tracks is not None else _unlocked(seq)):
        if not t.locked and split_clip(t, frame):
            n += 1
    return n


def delete_clips(seq: Sequence, ids: set, ripple: bool = False) -> None:
    """クリップ削除。ripple=True なら空いた範囲を詰める（他トラックも空いている場合のみ同期して詰める）。"""
    ranges = []
    for t in seq.tracks():
        if t.locked:
            continue
        for c in list(t.clips):
            if c.id in ids:
                ranges.append((t, c.start, c.end))
                t.clips.remove(c)
    if not ripple:
        return
    for t, a, b in sorted(ranges, key=lambda r: -r[1]):
        others = [o for o in _unlocked(seq) if o is not t]
        if all(o.is_empty(a, b) for o in others):
            for o in _unlocked(seq):
                shift_after(o, b, -(b - a))
        else:
            shift_after(t, b, -(b - a))


def ripple_delete_gap(seq: Sequence, track: Track, frame: int) -> bool:
    """frame 位置の隙間を詰める（全トラックが空いている範囲だけ）。"""
    if track.clip_at(frame):
        return False
    prev_end = max((c.end for c in track.clips if c.end <= frame), default=0)
    nxt = min((c.start for c in track.clips if c.start > frame), default=None)
    if nxt is None:
        return False
    a, b = prev_end, nxt
    for t in _unlocked(seq):
        for c in t.clips_in(a, b):
            a = max(a, c.end) if c.start < frame else a
            b = min(b, c.start) if c.start >= frame else b
    if b <= a:
        return False
    for t in _unlocked(seq):
        shift_after(t, b, -(b - a))
    return True


def _advance(clip: Clip, frames: int):
    """タイムライン上で頭を frames だけ詰めたときの素材位置の移動（速度を考慮）。"""
    if clip.kind not in ("video", "audio"):
        return
    sp = float(clip.props.get("speed", 1.0) or 0.0)
    if sp > 0:
        clip.src_in += int(round(frames * sp))


def max_extend(seq: Sequence, clip: Clip, project: Project) -> Tuple[int, int]:
    """クリップを左右に伸ばせる上限 (左に伸ばせるフレーム, 右に伸ばせるフレーム)。素材長で制限。"""
    if clip.kind in ("video", "audio"):
        sp = abs(float(clip.props.get("speed", 1.0) or 0.0))
        if sp == 0:              # フリーズフレームはいくらでも伸ばせる
            return 10 ** 9, 10 ** 9
        m = project.media_by_id(clip.media_id)
        if m and m.duration_s > 0:
            total = int(m.duration_s * seq.fps + 1e-6)
            used = int(round(clip.duration * sp))
            return int(clip.src_in / sp), max(0, int((total - (clip.src_in + used)) / sp))
        return clip.src_in, 0
    return 10 ** 9, 10 ** 9


def trim_edge(seq: Sequence, track: Track, clip: Clip, edge: str, delta: int,
              project: Project, mode: str = "normal") -> int:
    """クリップの端をトリム。mode: normal / ripple / ripple_linked。実際に動かした量を返す。"""
    left_room, right_room = max_extend(seq, clip, project)
    others = [c for c in track.clips if c.id != clip.id]
    if edge == "in":
        lo = -min(left_room, clip.start)
        if mode == "normal":
            prev_end = max((c.end for c in others if c.end <= clip.start), default=0)
            lo = max(lo, prev_end - clip.start)
        delta = max(lo, min(delta, clip.duration - 1))
        if mode in ("ripple", "ripple_linked"):
            # 開始位置は固定し、素材側をずらして後続を詰める
            _advance(clip, delta)
            clip.duration -= delta
            if mode == "ripple":
                for t in _unlocked(seq):
                    shift_after(t, clip.end + delta, -delta, ignore={clip.id})
        else:
            clip.start += delta
            clip.duration -= delta
            if clip.kind in ("video", "audio"):
                _advance(clip, delta)
    else:
        hi = right_room
        if mode == "normal":
            nxt = min((c.start for c in others if c.start >= clip.end), default=None)
            if nxt is not None:
                hi = min(hi, nxt - clip.end)
        delta = max(-(clip.duration - 1), min(delta, hi))
        old_end = clip.end
        clip.duration += delta
        if mode == "ripple":
            for t in _unlocked(seq):
                shift_after(t, old_end, delta, ignore={clip.id})
        # ripple_linked: リンク先は本体のリップルで後続が既に動いているので長さだけ変える
    track.sort()
    return delta


def roll_edit(seq: Sequence, track: Track, left: Clip, right: Clip, delta: int, project: Project) -> int:
    """隣接2クリップの編集点を移動（ローリング）。"""
    _, l_room = max_extend(seq, left, project)
    r_room, _ = max_extend(seq, right, project)
    delta = max(delta, -(left.duration - 1), -r_room)
    delta = min(delta, right.duration - 1, l_room)
    left.duration += delta
    right.start += delta
    right.duration -= delta
    if right.kind in ("video", "audio"):
        _advance(right, delta)
    return delta


def slip(seq: Sequence, clip: Clip, delta: int, project: Project) -> int:
    """位置と長さはそのまま素材の使用範囲をずらす（スリップ）。"""
    if clip.kind not in ("video", "audio"):
        return 0
    left_room, right_room = max_extend(seq, clip, project)
    delta = max(-left_room, min(delta, right_room))
    clip.src_in += delta
    return delta


def slide(seq: Sequence, track: Track, clip: Clip, delta: int, project: Project) -> int:
    """クリップを移動し、前後のクリップの長さで吸収（スライド）。"""
    prev = next((c for c in track.clips if c.end == clip.start), None)
    nxt = next((c for c in track.clips if c.start == clip.end), None)
    if delta < 0:
        lim = prev.duration - 1 if prev else clip.start - max(
            (c.end for c in track.clips if c.end <= clip.start), default=0)
        delta = max(delta, -lim)
        if nxt:
            r_room, _ = max_extend(seq, nxt, project)
            delta = max(delta, -r_room)
    else:
        lim = nxt.duration - 1 if nxt else min(
            (c.start for c in track.clips if c.start >= clip.end), default=10 ** 9) - clip.end
        delta = min(delta, lim)
        if prev:
            _, l_room = max_extend(seq, prev, project)
            delta = min(delta, l_room)
    if prev:
        prev.duration += delta
    if nxt:
        nxt.start += delta
        nxt.duration -= delta
        if nxt.kind in ("video", "audio"):
            _advance(nxt, delta)
    clip.start += delta
    track.sort()
    return delta


def move_clips(seq: Sequence, moves: List[Tuple[Clip, Track, Track]], delta: int,
               mode: str = "overwrite") -> None:
    """moves = [(clip, 元トラック, 先トラック)] を delta だけ移動して配置。"""
    ids = {c.id for c, _, _ in moves}
    for c, src, _ in moves:
        src.clips = [x for x in src.clips if x.id != c.id]
    placements = []
    for c, _, dst in moves:
        c.start = max(0, c.start + delta)
        placements.append((dst, c))
    if mode == "insert":
        a = min(c.start for _, c in placements)
        b = max(c.end for _, c in placements)
        for t in _unlocked(seq):
            insert_gap(t, a, b - a)
        for t, c in placements:
            t.clips.append(c)
            t.sort()
    else:
        for t, c in placements:
            clear_range(t, c.start, c.end, ignore=ids)
            t.clips.append(c)
            t.sort()
