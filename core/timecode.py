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


def seconds_to_timecode(seconds: float, fps: float = 29.97) -> str:
    """秒数をタイムコード (HH:MM:SS:FF) に変換。"""
    total_frames = int(seconds * fps)
    ff = total_frames % round(fps)
    total_secs = total_frames // round(fps)
    ss = total_secs % 60
    total_mins = total_secs // 60
    mm = total_mins % 60
    hh = total_mins // 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


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
    """秒数をフレーム数に変換。"""
    return int(seconds * fps)


def timecode_to_frames(tc: str, fps: float = 29.97) -> int:
    """タイムコード文字列をフレーム数に変換。"""
    return seconds_to_frames(parse_timecode(tc, fps), fps)
