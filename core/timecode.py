"""タイムコード変換ユーティリティ"""
import re


def parse_timecode(tc: str, fps: float = 29.97) -> float:
    """
    タイムコード文字列を秒数に変換。

    対応フォーマット:
      HH:MM:SS:FF  (ノンドロップフレーム)
      HH:MM:SS;FF  (ドロップフレーム)
      HH:MM:SS.mmm (ミリ秒)
      HH:MM:SS,mmm (SRT形式)
      HH:MM:SS
      MM:SS
      数値文字列    (秒)
    """
    tc = tc.strip()

    # HH:MM:SS:FF or HH:MM:SS;FF
    m = re.match(r'^(\d{1,2}):(\d{2}):(\d{2})[:;](\d{2})$', tc)
    if m:
        h, mn, s, f = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        nom = max(1, int(round(fps)))
        s += f // nom          # FF が公称fps以上なら秒へ繰り上げ
        f %= nom
        return h * 3600 + mn * 60 + s + f / fps

    # HH:MM:SS.mmm or HH:MM:SS,mmm
    m = re.match(r'^(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})$', tc)
    if m:
        h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ms_str = m.group(4).ljust(3, '0')
        return h * 3600 + mn * 60 + s + int(ms_str) / 1000

    # HH:MM:SS
    m = re.match(r'^(\d{1,2}):(\d{2}):(\d{2})$', tc)
    if m:
        h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return h * 3600 + mn * 60 + s

    # MM:SS
    m = re.match(r'^(\d{1,2}):(\d{2})$', tc)
    if m:
        mn, s = int(m.group(1)), int(m.group(2))
        return mn * 60 + s

    # Plain number (秒として扱う)
    try:
        return float(tc)
    except ValueError:
        pass

    raise ValueError(f"タイムコードの解析に失敗しました: {tc!r}")


def _nominal_fps(fps: float) -> int:
    """タイムコード上の1秒あたりフレーム数 (29.97→30, 23.976→24)。"""
    return max(1, int(round(fps)))


def seconds_to_timecode(seconds: float, fps: float = 29.97) -> str:
    """秒数をタイムコード (HH:MM:SS:FF) に変換。parse_timecode の逆変換。"""
    return frames_to_timecode(seconds_to_frames(max(0.0, seconds), fps), fps)


def frames_to_timecode(frames: int, fps: float = 29.97) -> str:
    """フレーム数 (タイムコード基準) を HH:MM:SS:FF に変換。"""
    nom = _nominal_fps(fps)
    frames = max(0, int(frames))
    ff = frames % nom
    total_secs = frames // nom
    ss = total_secs % 60
    total_mins = total_secs // 60
    mm = total_mins % 60
    hh = total_mins // 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def normalize_timecode(text: str, fps: float = 29.97) -> str:
    """入力ゆれを HH:MM:SS:FF に整形する。

    数字のみ ("1234" → 00:00:12:34, "10000" → 00:01:00:00) や
    "1:02:03.5"・"12.5"(秒) など parse_timecode が読める形式を受け付ける。
    解析できない場合は ValueError。
    """
    t = text.strip()
    if not t:
        raise ValueError("タイムコードが空です")
    if t.isdigit() and len(t) <= 8 and len(t) > 2:
        t = t.zfill(8)
        t = f"{t[0:2]}:{t[2:4]}:{t[4:6]}:{t[6:8]}"   # 60秒/30F 超えは繰り上げ
    return seconds_to_timecode(parse_timecode(t, fps), fps)


def shift_timecode(tc: str, delta_frames: int, fps: float = 29.97) -> str:
    """タイムコードを delta_frames だけずらす (0未満は 0 にクランプ)。"""
    return frames_to_timecode(timecode_to_frames(tc, fps) + delta_frames, fps)


def seconds_to_srt_time(seconds: float) -> str:
    """秒数をSRT形式タイムコード (HH:MM:SS,mmm) に変換。"""
    ms = round((seconds % 1) * 1000)
    if ms >= 1000:
        seconds += 1
        ms -= 1000
    total_secs = int(seconds)
    ss = total_secs % 60
    total_mins = total_secs // 60
    mm = total_mins % 60
    hh = total_mins // 60
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def seconds_to_frames(seconds: float, fps: float = 29.97) -> int:
    """秒数をフレーム数 (タイムコード基準) に変換。

    parse_timecode は HH:MM:SS 部分を整数秒、FF を FF/fps 秒として返すため、
    整数秒部分は公称 fps (29.97→30) で数えないと 01:00:00:00 等がずれる。
    """
    if seconds < 0:
        return -seconds_to_frames(-seconds, fps)
    whole = int(seconds + 1e-9)
    return whole * _nominal_fps(fps) + int(round((seconds - whole) * fps))


def timecode_to_frames(tc: str, fps: float = 29.97) -> int:
    """タイムコード文字列をフレーム数に変換。"""
    return seconds_to_frames(parse_timecode(tc, fps), fps)
