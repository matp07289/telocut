"""コーデックのプリセットとトランスコード（Qt を使わないので、アプリとアセットサーバーの両方で使う）。

DNxHR（LB / SQ / HQ / HQX / 444）・DNxHD（1080p）・ProRes（Proxy / LT / 422 / HQ / 4444 / 4444 XQ）・
QuickTime Animation・PNG（MOV）・非圧縮 10bit（v210）・CineForm・H.264 / H.265・音声（WAV / AAC / FLAC）
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class Preset:
    id: str
    label: str
    ext: str
    vcodec: str = ""                 # 空 = 音声だけ
    pix_fmt: str = "yuv420p"
    options: Dict[str, str] = field(default_factory=dict)
    acodec: str = "pcm_s16le"
    alpha: bool = False              # 透明（アルファ）を残せる
    group: str = ""
    note: str = ""
    container: str = ""              # 空 = 拡張子から


PRESETS: List[Preset] = [
    # ── 配信・確認用
    Preset("h264", "H.264 / MP4（YouTube・確認用）", ".mp4", "libx264", "yuv420p", {"crf": "18", "preset": "medium"},
           "aac", group="配信"),
    Preset("h264_mov", "H.264 / MOV", ".mov", "libx264", "yuv420p", {"crf": "18", "preset": "medium"}, "pcm_s16le",
           group="配信"),
    Preset("h265", "H.265 / MP4", ".mp4", "libx265", "yuv420p", {"crf": "22", "preset": "medium", "tag": "hvc1"},
           "aac", group="配信"),
    # ── DNx（Avid）
    Preset("dnxhr_lb", "DNxHR LB（軽い・オフライン編集）/ MXF", ".mxf", "dnxhd", "yuv422p", {"profile": "dnxhr_lb"},
           "pcm_s16le", group="DNx（Avid）"),
    Preset("dnxhr_sq", "DNxHR SQ / MXF", ".mxf", "dnxhd", "yuv422p", {"profile": "dnxhr_sq"}, "pcm_s16le", group="DNx（Avid）"),
    Preset("dnxhr_hq", "DNxHR HQ（高画質）/ MXF", ".mxf", "dnxhd", "yuv422p", {"profile": "dnxhr_hq"}, "pcm_s16le",
           group="DNx（Avid）"),
    Preset("dnxhr_hqx", "DNxHR HQX（10bit）/ MXF", ".mxf", "dnxhd", "yuv422p10le", {"profile": "dnxhr_hqx"}, "pcm_s24le",
           group="DNx（Avid）"),
    Preset("dnxhr_444", "DNxHR 444（10bit 4:4:4）/ MXF", ".mxf", "dnxhd", "yuv444p10le", {"profile": "dnxhr_444"},
           "pcm_s24le", group="DNx（Avid）"),
    Preset("dnxhr_hq_mov", "DNxHR HQ / MOV", ".mov", "dnxhd", "yuv422p", {"profile": "dnxhr_hq"}, "pcm_s16le",
           group="DNx（Avid）"),
    Preset("dnxhd_hq", "DNxHD 1080p HQ（220 / 185 / 175 など自動）/ MXF", ".mxf", "dnxhd", "yuv422p", {"_dnxhd": "hq"},
           "pcm_s16le", group="DNx（Avid）", note="1920×1080 のみ"),
    Preset("dnxhd_hqx", "DNxHD 1080p HQX（10bit）/ MXF", ".mxf", "dnxhd", "yuv422p10le", {"_dnxhd": "hqx"},
           "pcm_s24le", group="DNx（Avid）", note="1920×1080 のみ"),
    Preset("dnxhd_lb", "DNxHD 1080p 36 / 45（軽い）/ MXF", ".mxf", "dnxhd", "yuv422p", {"_dnxhd": "lb"}, "pcm_s16le",
           group="DNx（Avid）", note="1920×1080 のみ"),
    # ── QuickTime
    Preset("prores_proxy", "ProRes 422 Proxy / MOV", ".mov", "prores_ks", "yuv422p10le", {"profile": "0"}, "pcm_s16le",
           group="QuickTime"),
    Preset("prores_lt", "ProRes 422 LT / MOV", ".mov", "prores_ks", "yuv422p10le", {"profile": "1"}, "pcm_s16le",
           group="QuickTime"),
    Preset("prores_422", "ProRes 422 / MOV", ".mov", "prores_ks", "yuv422p10le", {"profile": "2"}, "pcm_s16le",
           group="QuickTime"),
    Preset("prores_hq", "ProRes 422 HQ / MOV", ".mov", "prores_ks", "yuv422p10le", {"profile": "3"}, "pcm_s16le",
           group="QuickTime"),
    Preset("prores_4444", "ProRes 4444（透明あり）/ MOV", ".mov", "prores_ks", "yuva444p10le", {"profile": "4"},
           "pcm_s24le", alpha=True, group="QuickTime"),
    Preset("prores_4444xq", "ProRes 4444 XQ（透明あり）/ MOV", ".mov", "prores_ks", "yuva444p10le", {"profile": "5"},
           "pcm_s24le", alpha=True, group="QuickTime"),
    Preset("qtrle", "QuickTime Animation（透明あり・可逆）/ MOV", ".mov", "qtrle", "argb", {}, "pcm_s16le", alpha=True,
           group="QuickTime"),
    Preset("png_mov", "PNG（透明あり・可逆）/ MOV", ".mov", "png", "rgba", {}, "pcm_s16le", alpha=True, group="QuickTime"),
    Preset("v210", "非圧縮 10bit 4:2:2（v210）/ MOV", ".mov", "v210", "yuv422p10le", {}, "pcm_s24le", group="QuickTime"),
    Preset("cineform", "CineForm / MOV", ".mov", "cfhd", "yuv422p10le", {"quality": "film1"}, "pcm_s16le",
           group="QuickTime"),
    Preset("mjpeg", "Motion JPEG / MOV", ".mov", "mjpeg", "yuvj422p", {"_bitrate": "80000000"}, "pcm_s16le",
           group="QuickTime"),
    # ── 音声だけ
    Preset("wav", "音声だけ: WAV（24bit）", ".wav", "", acodec="pcm_s24le", group="音声"),
    Preset("m4a", "音声だけ: AAC / M4A", ".m4a", "", acodec="aac", group="音声"),
    Preset("flac", "音声だけ: FLAC（可逆）", ".flac", "", acodec="flac", group="音声"),
]
BY_ID: Dict[str, Preset] = {p.id: p for p in PRESETS}
SIZES = [("", "元の大きさ"), ("3840", "4K（幅 3840）"), ("1920", "1080p（幅 1920）"), ("1280", "720p（幅 1280）"),
         ("960", "540p（幅 960）"), ("half", "半分")]

# DNxHD 1080p の決まったビットレート（Mbps）: fps → (LB, SQ, HQ, HQX)
# HQX（10bit）は 29.97 / 30p が無い（Avid の 220x はインターレースだけ）→ DNxHR HQX を使う
_DNXHD = {23.976: (36, 115, 175, 175), 24: (36, 115, 175, 175), 25: (36, 120, 185, 185), 29.97: (45, 145, 220, None),
          30: (45, 145, 220, None), 50: (75, 240, 365, 365), 59.94: (90, 290, 440, 440), 60: (90, 290, 440, 440)}


def dnxhd_bitrate(kind: str, fps: float) -> int:
    k = min(_DNXHD, key=lambda f: abs(f - fps))
    if abs(k - fps) > 0.05:
        raise ValueError(f"DNxHD は {', '.join(str(x) for x in sorted(_DNXHD))} fps だけです。DNxHR を使ってください")
    v = _DNXHD[k][{"lb": 0, "sq": 1, "hq": 2, "hqx": 3}[kind]]
    if v is None:
        raise ValueError(f"DNxHD HQX（10bit）は {k} fps がありません。DNxHR HQX を使ってください")
    return v * 1_000_000


def fps_fraction(fps: float) -> Fraction:
    for nominal, frac in ((29.97, Fraction(30000, 1001)), (59.94, Fraction(60000, 1001)), (23.976, Fraction(24000, 1001)),
                          (47.952, Fraction(48000, 1001)), (119.88, Fraction(120000, 1001))):
        if abs(fps - nominal) < 0.01:
            return frac
    return Fraction(fps).limit_denominator(1001)


def out_size(w: int, h: int, size: str) -> Tuple[int, int]:
    """size: "" / "half" / 幅（数字）。偶数にそろえる（DNx・ProRes は偶数が必要）。"""
    if size == "half":
        w2, h2 = w // 2, h // 2
    elif size and size.isdigit() and int(size) < w:
        w2 = int(size)
        h2 = round(h * w2 / w)
    else:
        w2, h2 = w, h
    return max(2, w2 // 2 * 2), max(2, h2 // 2 * 2)


def check(preset: Preset, w: int, h: int, fps: float) -> str:
    """書き出せない組み合わせなら理由（日本語）、問題なければ ""。"""
    if preset.options.get("_dnxhd"):
        if (w, h) != (1920, 1080):
            return f"DNxHD は 1920×1080 だけです（今は {w}×{h}）。DNxHR を選んでください"
        try:
            dnxhd_bitrate(preset.options["_dnxhd"], fps)
        except ValueError as ex:
            return str(ex)
    if preset.vcodec == "dnxhd" and (w < 256 or h < 120):
        return "DNxHR は 256×120 以上が必要です"
    return ""


def add_video_stream(container, preset: Preset, w: int, h: int, fps: float):
    """プリセットどおりの映像ストリームを作る。"""
    vs = container.add_stream(preset.vcodec, rate=fps_fraction(fps))
    vs.width, vs.height = w, h
    vs.pix_fmt = preset.pix_fmt
    opts = {k: v for k, v in preset.options.items() if not k.startswith("_") and k != "tag"}
    if preset.options.get("_dnxhd"):
        vs.bit_rate = dnxhd_bitrate(preset.options["_dnxhd"], fps)
        opts["profile"] = "dnxhd"
    if preset.vcodec == "dnxhd" and preset.options.get("profile", "").startswith("dnxhr"):
        opts["profile"] = preset.options["profile"]
    if preset.options.get("_bitrate"):
        vs.bit_rate = int(preset.options["_bitrate"])
    if preset.options.get("tag"):
        vs.codec_context.codec_tag = preset.options["tag"]
    vs.options = opts
    return vs


def add_audio_stream(container, preset: Preset, rate: int = 48000, channels: int = 2):
    a = container.add_stream(preset.acodec, rate=rate)
    a.layout = "stereo" if channels >= 2 else "mono"
    if preset.acodec == "aac":
        a.bit_rate = 256000
    return a


def audio_format(preset: Preset) -> str:
    return {"pcm_s16le": "s16", "pcm_s24le": "s32", "aac": "fltp", "flac": "s16", "alac": "s16p"}.get(preset.acodec, "fltp")


def open_output(path: str, preset: Preset):
    import av
    fmt = preset.container or {".mxf": "mxf", ".mov": "mov", ".mp4": "mp4", ".m4a": "ipod", ".wav": "wav",
                               ".flac": "flac"}.get(Path(path).suffix.lower())
    opts = {"movflags": "+faststart"} if fmt in ("mp4", "mov", "ipod") else {}
    return av.open(path, "w", format=fmt, options=opts)


# ─────────────────────────── トランスコード（素材 → 別の形式）───────────────────────────
def transcode(src: str, dst: str, preset_id: str, size: str = "", progress: Optional[Callable[[float], None]] = None,
              cancel: Optional[threading.Event] = None, tc_start: Optional[float] = None,
              tc_end: Optional[float] = None) -> str:
    """src を preset で dst に変換（映像・音声・タイムコード付き素材にも対応）。途中でやめたら dst は残さない。"""
    import av
    preset = BY_ID[preset_id]
    tmp = str(Path(dst).with_name(Path(dst).stem + ".part" + Path(dst).suffix))
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    try:
        with av.open(src) as ic:
            vin = ic.streams.video[0] if ic.streams.video and preset.vcodec else None
            ain = ic.streams.audio[0] if ic.streams.audio else None
            if vin is None and ain is None:
                raise ValueError("変換できる映像も音声もありません")
            dur = float(ic.duration / 1e6) if ic.duration else 0.0
            if tc_end:
                dur = min(dur or tc_end, tc_end) - (tc_start or 0)
            oc = open_output(tmp, preset)
            try:
                vout = aout = None
                if vin is not None:
                    w0, h0 = vin.codec_context.width, vin.codec_context.height
                    w, h = out_size(w0, h0, size)
                    fps = float(vin.average_rate or vin.guessed_rate or 30)
                    err = check(preset, w, h, fps)
                    if err:
                        raise ValueError(err)
                    vout = add_video_stream(oc, preset, w, h, fps)
                    vin.thread_type = "AUTO"
                if ain is not None:
                    ch = 2 if (ain.codec_context.channels or 2) >= 2 else 1
                    aout = add_audio_stream(oc, preset, 48000, ch)
                    res = av.AudioResampler(format=audio_format(preset), layout="stereo" if ch == 2 else "mono", rate=48000)
                if tc_start:
                    ic.seek(int(tc_start * 1e6))
                streams = [s for s in (vin, ain) if s is not None]
                vi = 0
                last = -1.0
                for pkt in ic.demux(*streams):
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("中止しました")
                    for fr in pkt.decode():
                        t = float(fr.time) if fr.time is not None else 0.0
                        if tc_start and t < tc_start - 1e-3:
                            continue
                        if tc_end and t >= tc_end:
                            continue
                        if pkt.stream is vin:
                            fr = fr.reformat(width=vout.width, height=vout.height, format=preset.pix_fmt)
                            fr.pts = vi
                            fr.time_base = 1 / fps_fraction(fps)        # 1 フレーム = 1（MOV でも fps が正しく入る）
                            vi += 1
                            for op in vout.encode(fr):
                                oc.mux(op)
                        else:
                            for rf in res.resample(fr):
                                rf.pts = None
                                for op in aout.encode(rf):
                                    oc.mux(op)
                        if progress and dur and t - last > 0.5:
                            last = t
                            progress(min(0.99, (t - (tc_start or 0)) / dur))
                for o in (vout, aout):
                    if o is not None:
                        for op in o.encode(None):
                            oc.mux(op)
            finally:
                oc.close()
        os.replace(tmp, dst)
        if progress:
            progress(1.0)
        return dst
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def default_name(src: str, preset_id: str, size: str = "") -> str:
    p = BY_ID[preset_id]
    tag = preset_id + (f"_{size}" if size else "")
    return f"{Path(src).stem}_{tag}{p.ext}"
