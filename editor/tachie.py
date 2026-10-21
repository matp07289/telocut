"""立ち絵メーカー: ずんだもん などの立ち絵の部品（PSD・PSD を分けた PNG）から、目パチ・口パクの画像を作る。テンプレートに保存して使い回す。

読み込めるもの
  1. PsdSplitter（Resolve_Script）の JSON … 重なり順・最初の表示・位置がそろっている（いちばん確実）
  2. PSD … psd-tools で層を読む（下から順）
  3. フォルダ（同じ大きさの PNG）… 名前の決まり（!・*・-）とフォルダで組み立てる。重なり順は名前から推測（あとで並べ替えられる）
     ※ 切り抜かれた PNG（位置の情報が無い）は使えない → JSON か PSD を選ぶ

名前の決まり（PSDToolKit）
  !名前 … いつも表示（隠せない）
  *名前 … 同じ階層の中で 1 つだけ選ぶ（ファイル名では * が使えないので - になっていることが多い）
  名前の . や / … 階層（「!口.-あ.png」 = 「口」の中の「あ」）

作るもの（キャラクターの画像）: 口閉じ・半開き・開き・まばたき（閉じ / 半目）・あいうえおん。全部を同じ範囲で切り抜く
テンプレート: <設定フォルダ>/templates/tachie/<名前>/ に画像と設定（選んだ部品・目と口の割り当て・まばたきの間隔・口の感度）
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image

from .settings import BASE_DIR

TEMPLATE_DIR = BASE_DIR / "templates" / "tachie"
DEFAULT_PARAMS = {"blink_interval": 4.0, "blink_jitter": 1.5, "blink_frames": 3, "double_blink": 0.15,
                  "open_db": -24.0, "half_db": -36.0, "bounce": True, "vowel_sync": True}
VOWELS = [("a", "あ"), ("i", "い"), ("u", "う"), ("e", "え"), ("o", "お"), ("N", "ん")]

# 名前から役割を当てる（上にあるほど優先）
EYE_CLOSED = ["閉じ", "綴じ", "とじ", "つむ", "瞑", "UU", "__", "＿＿", "にっこり", "にこっ", "にこ", "close", "closed", "笑う", "なごみ"]
EYE_HALF = ["半目", "細め", "ジト", "じっとり", "half", "眠"]
EYE_OPEN = ["普通", "通常", "ノーマル", "開き", "目セット", "まる", "open", "normal"]
MOUTH = {
    "closed": ["ん", "むー", "んー", "－", "真一文字", "閉", "綴じ　－", "綴じ　にこ", "にこ", "むふ", "綴じ", "close"],
    "half": ["お", "ほー", "△", "口小", "はへえ", "ゆ", "う", "half"],
    "open": ["ほあー", "んあー", "あー", "わあ", "口大", "ほあ", "あ", "おほお", "open"],
    "a": ["あ", "ほあー", "ほあ", "んあー", "あー", "口大"], "i": ["い", "いー", "にし", "にかっ"], "u": ["う", "ゆ", "ほー", "3", "３"],
    "e": ["え", "へ", "んへー", "はへえ"], "o": ["お", "おほお", "△", "□"], "N": ["ん", "んー", "むー", "真一文字"],
}
# フォルダから読むときの重なり順（下 → 上）の目安
ORDER_HINTS = [("尻尾", 0), ("しっぽ", 0), ("うしろ", 1), ("後ろ", 1), ("後頭部", 1), ("後髪", 1), ("裏地", 2), ("基礎", 3), ("素体", 3),
               ("体", 4), ("服", 5), ("腕", 6), ("顔", 7), ("頬", 8), ("ほっぺ", 8), ("顔色", 8), ("口", 9), ("くち", 9), ("目", 10),
               ("眉", 11), ("前髪", 12), ("髪", 12), ("枝豆", 13), ("アクセサリー", 14), ("記号", 15), ("汗", 15), ("涙", 15)]


@dataclass
class Part:
    raw: str                     # 元の名前（!・*・- 付き）
    label: str                   # 表示する名前
    path: str                    # 階層の名前（例「口/ほあ」）
    radio: bool = False          # 同じ階層で 1 つだけ
    force: bool = False          # いつも表示
    visible: bool = True         # 最初の表示
    children: List["Part"] = field(default_factory=list)
    src: Optional[tuple] = None  # ("png", パス) / ("psd", 層) / None（グループ）
    pos: Tuple[int, int] = (0, 0)

    @property
    def is_group(self) -> bool:
        return self.src is None


def _flags(raw: str) -> Tuple[str, bool, bool]:
    name = raw.strip()
    radio = force = False
    while name and name[0] in "!*-":
        if name[0] == "!":
            force = True
        else:
            radio = True
        name = name[1:]
    name = re.sub(r"_$", "", name).strip()           # ファイルにするとき付いた「_」を外す
    return name or raw, radio, force


@dataclass
class Model:
    canvas: Tuple[int, int]
    root: Part
    source: str
    kind: str                     # json / psd / folder
    warnings: List[str] = field(default_factory=list)
    _cache: Dict[str, Image.Image] = field(default_factory=dict)

    def walk(self, node: Optional[Part] = None):
        node = node or self.root
        for c in node.children:
            yield c
            yield from self.walk(c)

    def find(self, path: str) -> Optional[Part]:
        return next((p for p in self.walk() if p.path == path), None)

    def groups(self) -> List[Part]:
        return [p for p in self.walk() if p.is_group and any(c.radio for c in p.children)]

    def image(self, part: Part) -> Optional[Image.Image]:
        if part.path in self._cache:
            return self._cache[part.path]
        im = None
        try:
            if part.src[0] == "png":
                im = Image.open(part.src[1]).convert("RGBA")
            elif part.src[0] == "psd":
                im = part.src[1].topil()
                im = im.convert("RGBA") if im is not None else None
        except Exception:
            im = None
        self._cache[part.path] = im
        return im

    # ── 表示の状態 ──
    def default_state(self) -> Dict[str, object]:
        """{部品の path: True/False（いつも表示でないもの）, グループの path: 選んだ子の path（1 つだけのもの）}"""
        st: Dict[str, object] = {}
        for p in [self.root] + list(self.walk()):
            radios = [c for c in p.children if c.radio]
            if radios:
                vis = [c for c in radios if c.visible]
                st["sel:" + p.path] = (vis[0] if vis else radios[0]).path
            for c in p.children:
                if not c.radio and not c.force:
                    st[c.path] = bool(c.visible)
        return st

    def visible_parts(self, state: Dict[str, object], override: Optional[Dict[str, str]] = None) -> List[Part]:
        """下から順に、表示する部品（画像のある層）。override = {グループの path: 子の path}（目・口の差し替え）。"""
        override = override or {}
        out: List[Part] = []

        def go(node: Part):
            sel = override.get(node.path, state.get("sel:" + node.path))
            for c in node.children:
                if c.radio:
                    on = c.path == sel
                elif c.force:
                    on = True
                else:
                    on = bool(state.get(c.path, c.visible))
                if not on:
                    continue
                if c.is_group:
                    go(c)
                else:
                    out.append(c)
        go(self.root)
        return out

    def compose(self, state: Dict[str, object], override: Optional[Dict[str, str]] = None) -> Image.Image:
        W, H = self.canvas
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        for p in self.visible_parts(state, override):
            src = self.image(p)
            if src is None:
                continue
            if src.size == (W, H) and p.pos == (0, 0):
                im.alpha_composite(src)
            else:
                layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                layer.paste(src, p.pos)
                im.alpha_composite(layer)
        return im


# ─────────────────────────── 読み込み ───────────────────────────
def load(path: str) -> Model:
    p = Path(path)
    if p.is_dir():
        js = [j for j in sorted(p.glob("*.json")) if _is_splitter_json(j)]
        if js:
            return load_json(str(js[0]))
        try:
            return load_folder(path)
        except ValueError:
            # 切り抜かれた PNG のフォルダ → 同じフォルダか 1 つ上の PSD を使う
            for d in (p, p.parent):
                psds = sorted(d.glob("*.psd"))
                if psds:
                    m = load_psd(str(psds[0]))
                    m.warnings.append(f"PNG が切り抜かれていたので、PSD を使いました: {psds[0].name}")
                    return m
            raise
    if p.suffix.lower() == ".json":
        return load_json(path)
    if p.suffix.lower() in (".psd", ".psb"):
        return load_psd(path)
    raise ValueError("JSON（PsdSplitter）・PSD・PNG のフォルダのどれかを選んでください")


def _is_splitter_json(p: Path) -> bool:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return isinstance(d, dict) and isinstance(d.get("data"), list) and "x" in d and "y" in d
    except (OSError, ValueError):
        return False


def load_json(path: str) -> Model:
    """PsdSplitter の JSON（data_window は 左, 下, 右, 上（下から数える）の範囲）。"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    W, H = int(d["x"]), int(d["y"])
    base_dir = Path(str(d.get("directory", "")))
    here = Path(path).parent
    warnings: List[str] = []

    def fix(p: str) -> str:
        if os.path.exists(p):
            return p
        try:                                                     # フォルダごと動かしたとき
            rel = Path(p).relative_to(base_dir)
            for cand in (here / base_dir.name / rel, here / rel):
                if cand.exists():
                    return str(cand)
        except ValueError:
            pass
        return p

    def node(n: dict, parent_path: str) -> Part:
        label, radio, force = _flags(n.get("name", ""))
        pp = f"{parent_path}/{label}" if parent_path else label
        part = Part(n.get("name", ""), label, pp, radio, force, bool(n.get("visible", True)))
        if isinstance(n.get("data"), list):
            part.children = [node(c, pp) for c in n["data"]]
        else:
            f = fix(str(n.get("data", "")))
            part.src = ("png", f)
            if not os.path.exists(f):
                warnings.append(f"画像が見つかりません: {f}")
            dw = n.get("data_window") or [0, 0, W, H]
            try:
                with Image.open(f) as im:
                    size = im.size
            except Exception:
                size = (W, H)
            part.pos = (0, 0) if size == (W, H) else (int(dw[0]), int(H - dw[3]))
        return part
    root = Part("Root", "", "", children=[node(c, "") for c in d["data"]])
    return Model((W, H), root, path, "json", warnings)


def load_psd(path: str) -> Model:
    from psd_tools import PSDImage
    psd = PSDImage.open(path)
    W, H = psd.size

    def node(layer, parent_path: str) -> Part:
        label, radio, force = _flags(layer.name)
        pp = f"{parent_path}/{label}" if parent_path else label
        part = Part(layer.name, label, pp, radio, force, bool(layer.visible))
        if layer.is_group():
            part.children = [node(c, pp) for c in layer]
        else:
            part.src = ("psd", layer)
            part.pos = (int(layer.left), int(layer.top))
        return part
    root = Part("Root", "", "", children=[node(l, "") for l in psd])
    return Model((W, H), root, path, "psd")


def _order_key(names: List[str]) -> Tuple[int, str]:
    joined = "/".join(names)
    best = 6
    for word, rank in ORDER_HINTS:
        if word in names[0]:
            best = rank
            break
    return (best, joined)


def load_folder(path: str) -> Model:
    """同じ大きさの PNG のフォルダ（名前とフォルダで階層を作る。重なり順は名前から推測）。"""
    base = Path(path)
    files = [f for f in base.rglob("*.png") if not f.name.startswith(".")]
    if not files:
        raise ValueError("PNG がありません")
    sizes = {}
    for f in files:
        try:
            with Image.open(f) as im:
                sizes[f] = im.size
        except Exception:
            pass
    from collections import Counter
    canvas = Counter(sizes.values()).most_common(1)[0][0]
    full = [f for f, s in sizes.items() if s == canvas]
    warnings = []
    if len(full) < len(files) * 0.6:
        raise ValueError("PNG が切り抜かれていて、位置が分かりません。同じフォルダの JSON（PsdSplitter）か PSD を選んでください")
    if len(full) < len(files):
        warnings.append(f"大きさの違う {len(files) - len(full)} 枚は使いません（全身の画像・ロゴなど）")
    root = Part("Root", "", "")
    index: Dict[str, Part] = {"": root}
    entries = []
    for f in full:
        rel = f.relative_to(base).with_suffix("")
        segs = []
        for comp in rel.parts:
            segs += [s for s in comp.split(".") if s]
        entries.append((segs, f))
    entries.sort(key=lambda e: _order_key([_flags(s)[0] for s in e[0]]))
    for segs, f in entries:
        parent = root
        for i, raw in enumerate(segs):
            label, radio, force = _flags(raw)
            pp = f"{parent.path}/{label}" if parent.path else label
            node = index.get(pp)
            if node is None:
                node = Part(raw, label, pp, radio, force, visible=not radio)
                index[pp] = node
                parent.children.append(node)
            if i == len(segs) - 1:
                node.src = ("png", str(f))
            parent = node
    # 1 つだけ選ぶグループは、最初の子を表示に
    for p in index.values():
        radios = [c for c in p.children if c.radio]
        if radios and not any(c.visible for c in radios):
            radios[0].visible = True
    return Model(canvas, root, path, "folder", warnings)


# ─────────────────────────── 目と口を当てる ───────────────────────────
def find_group(model: Model, words: List[str]) -> Optional[Part]:
    """名前に words のどれかを含む「1 つだけ選ぶ」グループ（浅いものを優先）。"""
    cands = [g for g in model.groups() if any(w in g.label for w in words)]
    cands.sort(key=lambda g: (g.path.count("/"), len(g.label)))
    return cands[0] if cands else None


def _guess(options: List[Part], words: List[str], exclude: Tuple[str, ...] = ()) -> Optional[str]:
    for w in words:
        for o in options:
            if o.path in exclude:
                continue
            if o.label == w:
                return o.path
    for w in words:
        for o in options:
            if o.path in exclude:
                continue
            if w in o.label:
                return o.path
    return None


def guess_roles(model: Model, state: Dict[str, object]) -> dict:
    """目のグループ・口のグループと、開き / 半目 / 閉じ・口の形を名前から当てる。"""
    eye = find_group(model, ["目", "eye"])
    mouth = find_group(model, ["口", "くち", "mouth"])
    out = {"eye_group": eye.path if eye else "", "mouth_group": mouth.path if mouth else "", "eye": {}, "mouth": {}}
    if eye:
        opts = [c for c in eye.children if c.radio]
        cur = state.get("sel:" + eye.path)
        closed = _guess(opts, EYE_CLOSED) or ""
        half = _guess(opts, EYE_HALF, (closed,)) or ""
        # 開いた目: 「普通」などの名前 → 今表示している目（閉じ・半目でなければ）→ 閉じ・半目でない最初の目
        open_ = (_guess(opts, EYE_OPEN, (closed, half)) or (cur if cur not in (closed, half) else "")
                 or next((o.path for o in opts if o.path not in (closed, half)), cur or ""))
        out["eye"] = {"open": open_, "closed": closed, "half": half}
    if mouth:
        opts = [c for c in mouth.children if c.radio]
        m = {}
        for key in ("closed", "open", "half"):
            m[key] = _guess(opts, MOUTH[key], tuple(v for v in m.values() if v)) or ""
        if not m["closed"]:
            m["closed"] = state.get("sel:" + mouth.path) or (opts[0].path if opts else "")
        if not m["open"]:
            m["open"] = next((o.path for o in opts if o.path != m["closed"]), m["closed"])
        for key, _lab in VOWELS:
            m[key] = _guess(opts, MOUTH[key]) or ""
        out["mouth"] = m
    return out


# ─────────────────────────── 画像を作る ───────────────────────────
def build_images(model: Model, state: Dict[str, object], roles: dict) -> Dict[str, Image.Image]:
    """口閉じ / 半開き / 開き / まばたき（閉じ・半目）/ あいうえおん を、同じ範囲で切り抜いて返す。"""
    eg, mg = roles.get("eye_group", ""), roles.get("mouth_group", "")
    eye, mouth = roles.get("eye", {}), roles.get("mouth", {})
    if not mg or not mouth.get("closed"):
        raise ValueError("口のグループと、口を閉じた形を選んでください")

    def comp(m: str, e: str = "") -> Image.Image:
        ov = {mg: m}
        if eg and (e or eye.get("open")):
            ov[eg] = e or eye["open"]
        return model.compose(state, ov)
    imgs = {"closed": comp(mouth["closed"]), "open": comp(mouth.get("open") or mouth["closed"])}
    if mouth.get("half"):
        imgs["half"] = comp(mouth["half"])
    if eg and eye.get("closed"):
        imgs["blink"] = comp(mouth["closed"], eye["closed"])
    if eg and eye.get("half"):
        imgs["blink_half"] = comp(mouth["closed"], eye["half"])
    for key, _lab in VOWELS:
        if mouth.get(key):
            imgs[key] = comp(mouth[key])
    box = None
    for im in imgs.values():
        b = im.getbbox()
        if b:
            box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
    if box:
        imgs = {k: v.crop(box) for k, v in imgs.items()}
    return imgs


def save_character(name: str, imgs: Dict[str, Image.Image], params: Optional[dict] = None, source: str = "") -> dict:
    """キャラクター（プロジェクトの立ち絵）として画像を保存。"""
    from .model import new_id
    from .yukkuri import char_dir
    cid = new_id()
    d = char_dir() / cid
    d.mkdir(parents=True, exist_ok=True)
    out = {}
    for k, im in imgs.items():
        p = d / f"{k}.png"
        im.save(p)
        out[k] = str(p)
    return {"id": cid, "name": name, "images": out, "source": source, "params": {**DEFAULT_PARAMS, **(params or {})}}


# ─────────────────────────── テンプレート ───────────────────────────
def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "立ち絵"


def save_template(name: str, model: Optional[Model], state: Dict[str, object], roles: dict, params: dict,
                  imgs: Dict[str, Image.Image]) -> Path:
    """テンプレート（画像・選んだ部品・目と口の割り当て・まばたき / 口パクの設定）を保存。元の素材が無くても使える。"""
    d = TEMPLATE_DIR / _safe(name)
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    for k, im in imgs.items():
        im.save(d / f"{k}.png")
    meta = {"name": name, "source": model.source if model else "", "kind": model.kind if model else "",
            "state": state, "roles": roles, "params": {**DEFAULT_PARAMS, **(params or {})}, "images": sorted(imgs)}
    (d / "template.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def templates() -> List[dict]:
    out = []
    if TEMPLATE_DIR.is_dir():
        for d in sorted(TEMPLATE_DIR.iterdir()):
            f = d / "template.json"
            if f.exists():
                try:
                    meta = json.loads(f.read_text(encoding="utf-8"))
                    meta["dir"] = str(d)
                    out.append(meta)
                except ValueError:
                    pass
    return out


def delete_template(name: str) -> bool:
    d = TEMPLATE_DIR / _safe(name)
    if d.exists():
        shutil.rmtree(d)
        return True
    return False


def character_from_template(t: dict, name: str = "") -> dict:
    """テンプレートの画像をコピーして、新しいキャラクターにする。"""
    d = Path(t["dir"])
    imgs = {k: Image.open(d / f"{k}.png").convert("RGBA") for k in t.get("images", []) if (d / f"{k}.png").exists()}
    return save_character(name or t["name"], imgs, t.get("params"), t.get("source", ""))


def export_template(name: str, zip_path: str) -> str:
    """テンプレートを zip に（ほかの PC へ）。"""
    d = TEMPLATE_DIR / _safe(name)
    base = zip_path[:-4] if zip_path.lower().endswith(".zip") else zip_path
    return shutil.make_archive(base, "zip", d.parent, d.name)


def import_template(zip_path: str) -> Optional[str]:
    import zipfile
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        top = {n.split("/")[0] for n in names}
        if len(top) != 1 or not any(n.endswith("template.json") for n in names):
            raise ValueError("立ち絵テンプレートの zip ではありません")
        for n in names:
            if ".." in n or n.startswith(("/", "\\")):
                raise ValueError("zip の中身が正しくありません")
        z.extractall(TEMPLATE_DIR)
    return next(iter(top))
