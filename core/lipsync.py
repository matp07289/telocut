"""
口パク (Lip Sync) コアモジュール
  - PSD / PNG レイヤー管理
  - VoiceVox AudioQuery タイムライン (AnimType 対応)
  - WAV 振幅タイムライン
  - フレーム生成 / GIF / APNG 書き出し
  Resolve_Script (nakano000) の設計を参考に改良
"""
from __future__ import annotations
import os
import wave
from pathlib import Path
from typing import Optional

# AnimType と build_mora_timeline は voicevox モジュールから再エクスポート
try:
    from .voicevox import AnimType, build_mora_timeline, AudioQuery
    HAS_VV_TIMELINE = True
except ImportError:
    HAS_VV_TIMELINE = False

try:
    from PIL import Image, ImageChops
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

try:
    import numpy as _np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    from psd_tools import PSDImage
    HAS_PSD_TOOLS = True
except ImportError:
    HAS_PSD_TOOLS = False


# ── 口形状キー ──────────────────────────────────────────────────────────────
SHAPE_KEYS  = ['a', 'i', 'u', 'e', 'o', 'N', 'closed']
VOWEL_MAP   = {
    'a': 'a', 'i': 'i', 'u': 'u', 'e': 'e', 'o': 'o',
    'N': 'N', 'cl': 'closed', 'pau': 'closed',
}
SHAPE_LABEL = {
    'a': 'あ', 'i': 'い', 'u': 'う',
    'e': 'え', 'o': 'お', 'N': 'ん', 'closed': '閉じ',
}
# PSD レイヤー名キーワード → shape key
PSD_NAME_MAP = {
    'あ': 'a', 'a':   'a',
    'い': 'i', 'i':   'i',
    'う': 'u', 'u':   'u',
    'え': 'e', 'e':   'e',
    'お': 'o', 'o':   'o',
    'ん': 'N', 'n':   'N',
    '閉': 'closed', 'close': 'closed', 'shut': 'closed',
}

_MOUTH_KEYWORDS = ('口', 'mouth', 'lip', 'kuchi')
_EYE_KEYWORDS   = ('目', 'eye', '瞳', 'まぶた', 'eyelid', 'me_')


def classify_eye_role(name: str):
    """
    目レイヤーを 'eye_open' / 'eye_closed' に分類。目でなければ None。
    """
    low = name.lower()
    if not any(k in low for k in _EYE_KEYWORDS):
        return None
    if (any(k in name for k in ('閉', 'つぶ', '瞑'))
            or any(k in low for k in ('close', 'blink', 'shut'))):
        return 'eye_closed'
    return 'eye_open'


def build_blink_timeline(total_frames: int, fps: float,
                         interval_sec: float = 4.0,
                         blink_frames: int = 3) -> set:
    """
    まばたきで「目を閉じる」フレーム集合を返す。
    interval_sec ごと（±ジッタ）に blink_frames 分だけ目を閉じる。
    """
    import random
    closed: set = set()
    if total_frames <= 0:
        return closed
    t = interval_sec
    while int(t * fps) < total_frames:
        start = int(t * fps)
        for f in range(start, min(start + max(1, blink_frames), total_frames)):
            closed.add(f)
        t += interval_sec * (0.7 + random.random() * 0.6)
    return closed


def classify_layer_role(name: str):
    """
    レイヤー名から口形状キー (a/i/u/e/o/N/closed) を返す。
    口レイヤーでなければ None（=通常レイヤー、常時表示）。

    注意: 'mouth' や 'kuchi' という語自体に母音字が含まれるため、
    口キーワードを除去してから母音をトークン単位で判定する。
    """
    low = name.lower()
    if not any(k in low for k in _MOUTH_KEYWORDS):
        return None
    # 日本語かな（最も確実）
    for kana, shape in (('あ', 'a'), ('い', 'i'), ('う', 'u'),
                        ('え', 'e'), ('お', 'o'), ('ん', 'N'), ('閉', 'closed')):
        if kana in name:
            return shape
    if 'close' in low or 'shut' in low:
        return 'closed'
    # ローマ字: 口キーワードを除去してから孤立した母音トークンを探す
    import re
    rem = low
    for k in _MOUTH_KEYWORDS:
        rem = rem.replace(k, ' ')
    for t in re.split(r'[^a-z]+', rem):
        if t in ('a', 'i', 'u', 'e', 'o'):
            return t
        if t == 'n':
            return 'N'
    return 'a'   # 口レイヤーだが母音不明 → あ扱い


# ── PSD からレイヤーを抽出 ──────────────────────────────────────────────────

def extract_psd_layers(psd_path: str) -> dict[str, "Image.Image"]:
    """
    PSD を開き、全レイヤーを {レイヤー名: PIL.Image(RGBA)} で返す。
    psd-tools が必要。
    """
    if not HAS_PSD_TOOLS:
        raise RuntimeError("psd-tools が未インストールです: pip install psd-tools")
    if not HAS_PILLOW:
        raise RuntimeError("Pillow が未インストールです。")

    psd   = PSDImage.open(psd_path)
    result: dict[str, Image.Image] = {}

    def _collect(layer, prefix=""):
        name = f"{prefix}{layer.name}" if prefix else layer.name
        if layer.is_group():
            for child in layer:
                _collect(child, prefix=name + "/")
        else:
            try:
                img = layer.topil()
                if img is not None:
                    # キャンバスサイズに配置
                    canvas = Image.new("RGBA", (psd.width, psd.height), (0, 0, 0, 0))
                    canvas.paste(img.convert("RGBA"),
                                 (layer.left, layer.top), img.convert("RGBA"))
                    result[name] = canvas
            except Exception:
                pass

    for layer in psd:
        _collect(layer)
    return result


def auto_map_psd_layers(
        layers: dict[str, "Image.Image"]
) -> tuple[Optional["Image.Image"], dict[str, "Image.Image"]]:
    """
    レイヤー名から口形状を自動判定して (ベース画像, {shape_key: PIL.Image}) を返す。
    - 名前に「口」「mouth」「lips」が含まれるグループを口レイヤーとして扱う
    - 残りをベースとして合成
    """
    if not HAS_PILLOW:
        raise RuntimeError("Pillow が未インストールです。")

    mouth_layers:   dict[str, Image.Image] = {}
    base_layers:    list[Image.Image]      = []
    mouth_keywords = ['口', 'mouth', 'lips', 'kuchi']

    for name, img in layers.items():
        low  = name.lower()
        # 口レイヤー判定
        is_mouth = any(k in low for k in mouth_keywords)
        if is_mouth:
            for keyword, shape in PSD_NAME_MAP.items():
                if keyword in low:
                    mouth_layers[shape] = img
                    break
        else:
            base_layers.append(img)

    # ベース合成
    base: Optional[Image.Image] = None
    if base_layers:
        sizes = [img.size for img in base_layers]
        w = max(s[0] for s in sizes)
        h = max(s[1] for s in sizes)
        base = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        for img in base_layers:
            base = Image.alpha_composite(base, img.convert("RGBA"))

    return base, mouth_layers


# ── タイムライン生成 ────────────────────────────────────────────────────────

def parse_mora_timeline(
    audio_query_or_dict,
    fps: float,
    anim_type: str = "aiueo2",
) -> list[str]:
    """
    VoiceVox の audio_query から fps 単位の口形状キーリストを生成。
    audio_query_or_dict: AudioQuery オブジェクト または dict (後方互換)
    anim_type: "aiueo" / "aiueo2" / "aiueo3" / "open"
    返値: ['closed', 'a', 'a', 'i', ...] (フレーム数分)
    """
    # AudioQuery オブジェクトが来た場合 → voicevox の build_mora_timeline を使う
    if HAS_VV_TIMELINE and isinstance(audio_query_or_dict, AudioQuery):
        return build_mora_timeline(audio_query_or_dict, fps, anim_type)

    # HAS_VV_TIMELINE=False なのに AudioQuery が渡された場合: to_dict() で変換して処理
    if not HAS_VV_TIMELINE and hasattr(audio_query_or_dict, 'to_dict'):
        audio_query_or_dict = audio_query_or_dict.to_dict()

    # dict が来た場合 → 従来ロジック (後方互換)
    d = audio_query_or_dict if isinstance(audio_query_or_dict, dict) else {}
    _VOWEL_MAP = {"a":"a","i":"i","u":"u","e":"e","o":"o","N":"N","cl":"closed","pau":"closed"}
    events: list[tuple[float, float, str]] = []
    t = float(d.get("prePhonemeLength", 0.0))

    for phrase in d.get("accent_phrases", []):
        for mora in phrase.get("moras", []):
            c_len = float(mora.get("consonant_length") or 0.0)
            v_len = float(mora.get("vowel_length", 0.0))
            vowel = mora.get("vowel", "a")
            shape = _VOWEL_MAP.get(vowel, "a")
            if c_len > 0:
                c_shape = shape if anim_type == "aiueo2" else "closed"
                events.append((t, t + c_len, c_shape))
                t += c_len
            events.append((t, t + v_len, "a" if anim_type == "open" else shape))
            t += v_len
        pause = phrase.get("pause_mora")
        if pause:
            p_len = float(pause.get("vowel_length", 0.0))
            events.append((t, t + p_len, "closed"))
            t += p_len

    t += float(d.get("postPhonemeLength", 0.0))
    total = max(1, int(t * fps) + 1)
    timeline = ["closed"] * total
    for s, e, shape in events:
        for f in range(int(s * fps), min(int(e * fps) + 1, total)):
            timeline[f] = shape
    return timeline


def analyze_wav_timeline(wav_path: str, fps: float,
                          threshold: float = 0.05) -> list[str]:
    """
    WAV の RMS 振幅からシンプルな口形状タイムラインを生成（あ / 閉じ の 2値）。
    psd-tools 不要。numpy が使えると高速。
    """
    with wave.open(wav_path, 'r') as wf:
        n_frames   = wf.getnframes()
        framerate  = wf.getframerate()
        n_ch       = wf.getnchannels()
        sampwidth  = wf.getsampwidth()
        raw        = wf.readframes(n_frames)

    # dtype 判定
    if sampwidth == 2:
        import array
        data = array.array('h', raw)
        max_val = 32768.0
    else:
        data = [b for b in raw]
        max_val = 128.0

    if HAS_NUMPY:
        arr = _np.array(data, dtype=_np.float32) / max_val
        if n_ch > 1:
            arr = arr.reshape(-1, n_ch).mean(axis=1)
        spf   = max(1, int(framerate / fps))
        nfr   = len(arr) // spf + 1
        tl: list[str] = []
        for i in range(nfr):
            chunk = arr[i * spf:(i + 1) * spf]
            rms   = float(_np.sqrt(_np.mean(chunk ** 2))) if len(chunk) > 0 else 0.0
            tl.append("a" if rms > threshold else "closed")
        return tl
    else:
        # numpy なし: 非効率だが動作する
        norm = [x / max_val for x in data]
        if n_ch > 1:
            norm = [sum(norm[i:i+n_ch]) / n_ch for i in range(0, len(norm), n_ch)]
        spf   = max(1, int(framerate / fps))
        tl = []
        for i in range(0, len(norm), spf):
            chunk = norm[i:i + spf]
            rms   = (sum(x ** 2 for x in chunk) / len(chunk)) ** 0.5 if chunk else 0.0
            tl.append("a" if rms > threshold else "closed")
        return tl


# ── フレーム合成 ────────────────────────────────────────────────────────────

def composite_frame(
    base: "Image.Image",
    mouth_layers: dict[str, "Image.Image"],
    shape_key: str,
    mouth_offset: tuple[int, int] = (0, 0),
) -> "Image.Image":
    """ベース + 口レイヤーを合成して 1 フレームの RGBA Image を返す。"""
    result = base.copy().convert("RGBA")
    layer  = mouth_layers.get(shape_key) or mouth_layers.get("closed")
    if layer:
        ox, oy = mouth_offset
        if ox == 0 and oy == 0:
            result = Image.alpha_composite(result, layer.convert("RGBA"))
        else:
            overlay = Image.new("RGBA", result.size, (0, 0, 0, 0))
            overlay.paste(layer.convert("RGBA"), (ox, oy), layer.convert("RGBA"))
            result = Image.alpha_composite(result, overlay)
    return result


def generate_frames(
    base: "Image.Image",
    mouth_layers: dict[str, "Image.Image"],
    timeline: list[str],
    mouth_offset: tuple[int, int] = (0, 0),
    callback=None,
) -> list["Image.Image"]:
    """タイムラインに従ってフレームリストを生成。callback(i, total) で進捗通知。"""
    frames = []
    total  = len(timeline)
    for i, shape in enumerate(timeline):
        frames.append(composite_frame(base, mouth_layers, shape, mouth_offset))
        if callback:
            callback(i + 1, total)
    return frames


def export_png_sequence(
    frames: list["Image.Image"],
    out_dir: str,
    prefix: str = "frame",
    callback=None,
) -> list[str]:
    """PNG 連番として書き出す。"""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    paths = []
    for i, frame in enumerate(frames):
        p = str(Path(out_dir) / f"{prefix}_{i:04d}.png")
        frame.save(p, "PNG")
        paths.append(p)
        if callback:
            callback(i + 1, len(frames))
    return paths


def export_gif(
    frames: list["Image.Image"],
    out_path: str,
    fps: float = 24.0,
    loop: int = 0,
) -> None:
    """GIF アニメーションとして書き出す。"""
    if not frames:
        return
    duration_ms = int(1000 / fps)
    # RGBA → RGB + palette (GIF は 256色)
    converted = []
    for f in frames:
        bg = Image.new("RGB", f.size, (0, 0, 0))
        bg.paste(f, mask=f.getchannel("A") if "A" in f.getbands() else None)
        converted.append(bg.convert("P", palette=Image.ADAPTIVE, colors=256))
    converted[0].save(
        out_path, save_all=True, append_images=converted[1:],
        duration=duration_ms, loop=loop, optimize=True,
    )


def export_apng(
    frames: list["Image.Image"],
    out_path: str,
    fps: float = 24.0,
) -> None:
    """APNG (透過アニメーション PNG) として書き出す。Pillow 9.1+ 必要。"""
    if not frames:
        return
    duration_ms = int(1000 / fps)
    frames[0].save(
        out_path, save_all=True, append_images=frames[1:],
        duration=duration_ms, loop=0, format="PNG",
    )
