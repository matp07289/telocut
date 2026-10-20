"""エフェクトプラグインの仕組み（AviUtl のスクリプト / プラグイン配布のように追加できる）。

エフェクト = 「クリップの画像（RGBA）→ 画像」の関数 + パラメーター定義。
- 組み込み: fx_builtin.py（グロー・縁取り・集中線・VHS・グリッチ など）
- 追加プラグイン: <設定フォルダ>/plugins/*.py（環境設定の plugin_dir でも可）。書き方:

    from editor.fxlib import effect, P

    @effect("my_red", "赤くする", "カラー", [P("amount", "強さ", 0.5, 0, 1)])
    def my_red(im, p, ctx):            # im: PIL RGBA, p: パラメーター dict, ctx: Ctx
        r, g, b, a = im.split()
        ...
        return im

クリップの props["effects"] には {"name": id, "params": {...}, "on": True} の形で保存する。
"""
from __future__ import annotations

import importlib.util
import os
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional


@dataclass
class P:
    """パラメーター。kind: float / int / color / choice / bool / file（ファイルのパス）"""
    key: str
    label: str
    default: object = 0.0
    min: float = 0.0
    max: float = 1.0
    step: float = 0.0
    kind: str = ""
    choices: List[str] = field(default_factory=list)
    suffix: str = ""

    def __post_init__(self):
        if not self.kind:
            if isinstance(self.default, bool):
                self.kind = "bool"
            elif isinstance(self.default, str) and self.default.startswith("#"):
                self.kind = "color"
            elif self.choices:
                self.kind = "choice"
            elif isinstance(self.default, int):
                self.kind = "int"
            else:
                self.kind = "float"
        if not self.step:
            self.step = 1 if self.kind == "int" else max(0.01, (self.max - self.min) / 100)


@dataclass
class Ctx:
    """エフェクトに渡す情報。"""
    rel: int = 0             # クリップ先頭からのフレーム
    fps: float = 30.0
    duration: int = 1        # クリップの長さ（フレーム）
    scale: float = 1.0       # プレビューの縮小率（px のパラメーターに掛ける）
    seed: int = 0            # クリップごとに違う乱数の種

    @property
    def t(self) -> float:    # 秒
        return self.rel / max(1e-6, self.fps)


@dataclass
class Effect:
    id: str
    name: str
    category: str
    params: List[P]
    fn: Callable
    animated: bool = False   # フレームごとに変わる（キャッシュのキーに時刻を入れる）
    expands: bool = False    # 画像が大きくなる（縁取り・影など）
    description: str = ""
    source: str = "組み込み"

    def defaults(self) -> dict:
        return {p.key: p.default for p in self.params}


REGISTRY: Dict[str, Effect] = {}
_loaded = False
LOAD_ERRORS: List[str] = []


def effect(id: str, name: str, category: str, params: Optional[List[P]] = None, animated: bool = False,
           expands: bool = False, description: str = ""):
    """エフェクトを登録するデコレーター。"""
    def deco(fn):
        REGISTRY[id] = Effect(id, name, category, list(params or []), fn, animated, expands, description,
                              _current_source[0])
        return fn
    return deco


_current_source = ["組み込み"]


def plugin_dirs() -> List[Path]:
    from .settings import BASE_DIR, SETTINGS
    dirs = [BASE_DIR / "plugins"]
    extra = getattr(SETTINGS, "plugin_dir", "")
    if extra:
        dirs.append(Path(extra))
    return dirs


def load(force: bool = False):
    """組み込みと追加プラグインを読み込む。"""
    global _loaded
    if _loaded and not force:
        return
    from . import fx_builtin  # noqa: F401  （import で登録される）
    if force:
        for k in [k for k, e in REGISTRY.items() if e.source != "組み込み"]:
            REGISTRY.pop(k)
    LOAD_ERRORS.clear()
    for d in plugin_dirs():
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.py")):
            _current_source[0] = f.name
            try:
                spec = importlib.util.spec_from_file_location(f"telocut_plugin_{f.stem}", f)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            except Exception:
                LOAD_ERRORS.append(f"{f.name}: {traceback.format_exc(limit=2)}")
            finally:
                _current_source[0] = "組み込み"
    _loaded = True


def get(id: str) -> Optional[Effect]:
    load()
    return REGISTRY.get(id)


def categories() -> Dict[str, List[Effect]]:
    load()
    out: Dict[str, List[Effect]] = {}
    for e in REGISTRY.values():
        out.setdefault(e.category, []).append(e)
    return out


def params_of(fx: dict) -> dict:
    e = get(fx.get("name", ""))
    p = e.defaults() if e else {}
    p.update(fx.get("params") or {})
    return p


def is_animated(effects: list) -> bool:
    for fx in effects or []:
        e = get(fx.get("name", ""))
        if e is not None and fx.get("on", True) and (e.animated or fx.get("keys")):
            return True
    return False


def params_at(fx: dict, ctx: "Ctx") -> dict:
    """キーフレーム（fx["keys"] = {パラメーター: [[フレーム, 値, イージング], ...]}）を反映した値。"""
    p = params_of(fx)
    keys = fx.get("keys") or {}
    if keys:
        from .render import key_value
        for k, ks in keys.items():
            if ks:
                v = key_value(ks, ctx.rel)
                if v is not None:
                    p[k] = v
    return p


def apply(im, fx: dict, ctx: Ctx):
    """1 つのエフェクトを掛ける。登録されていなければ None（呼び出し側が旧来の処理をする）。"""
    e = get(fx.get("name", ""))
    if e is None:
        return None
    if not fx.get("on", True):
        return im
    try:
        out = e.fn(im if im.mode == "RGBA" else im.convert("RGBA"), params_at(fx, ctx), ctx)
        return out if out is not None else im
    except Exception:
        return im


def write_sample_plugin() -> Path:
    """プラグインフォルダとサンプルを作る（メニューから）。"""
    d = plugin_dirs()[0]
    d.mkdir(parents=True, exist_ok=True)
    sample = d / "sample_plugin.py"
    if not sample.exists():
        sample.write_text('''"""TeloCut エフェクトプラグインのサンプル。このフォルダに .py を置くと エフェクトパレットに出ます。"""
import numpy as np
from PIL import Image
from editor.fxlib import effect, P


@effect("sample_tint", "色を乗せる（サンプル）", "プラグイン", [
    P("color", "色", "#FF66AA"),
    P("amount", "強さ", 0.4, 0.0, 1.0),
])
def sample_tint(im, p, ctx):
    a = np.asarray(im).astype(np.float32)
    c = np.array([int(p["color"][i:i + 2], 16) for i in (1, 3, 5)], np.float32)
    k = float(p["amount"])
    a[..., :3] = a[..., :3] * (1 - k) + c * k
    return Image.fromarray(a.clip(0, 255).astype(np.uint8), "RGBA")


@effect("sample_pulse", "どくん（サンプル・アニメ）", "プラグイン", [
    P("period", "周期（秒）", 1.0, 0.2, 5.0),
], animated=True)
def sample_pulse(im, p, ctx):
    import math
    k = 1 + 0.06 * max(0.0, math.sin(ctx.t * 2 * math.pi / float(p["period"])))
    w, h = im.size
    big = im.resize((int(w * k), int(h * k)), Image.BILINEAR)
    return big.crop(((big.width - w) // 2, (big.height - h) // 2, (big.width - w) // 2 + w, (big.height - h) // 2 + h))
''', encoding="utf-8")
    return d
