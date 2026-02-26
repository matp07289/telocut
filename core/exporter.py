"""PNG / SRT / FCPxml 書き出し & DaVinci Resolve API連携"""
import csv
import os
import sys
import json
import math
from pathlib import Path
from typing import Callable, Optional

from .models import DesignSettings, TelopEntry
from .timecode import (
    parse_timecode, seconds_to_timecode, seconds_to_srt_time, seconds_to_frames
)


# ─────────────────────────────────────────────
#  CSV インポート
# ─────────────────────────────────────────────

def import_csv(
    path: str,
    encoding: str = "utf-8",
    delimiter: str = "auto",
    start_id: int = 1,
) -> list[TelopEntry]:
    """
    CSVをテロップエントリのリストに変換。

    必須カラム: start, end, text  (順不同・ヘッダ行あり)
    代替カラム名:
      start: start_time / in / tc_in / 開始
      end:   end_time  / out / tc_out / 終了
      text:  content   / caption / テキスト / テロップ / subtitle
    """
    entries: list[TelopEntry] = []

    with open(path, 'r', encoding=encoding, newline='') as f:
        sample = f.read(2048)
        f.seek(0)

        if delimiter == "auto":
            tab_count = sample.count('\t')
            comma_count = sample.count(',')
            delim = '\t' if tab_count > comma_count else ','
        else:
            delim = delimiter

        reader = csv.DictReader(f, delimiter=delim)
        if reader.fieldnames is None:
            return entries

        # カラム名をマッピング
        headers = [h.strip().lower() if h else '' for h in reader.fieldnames]

        def find_col(candidates: list[str]) -> Optional[str]:
            for cand in candidates:
                for orig_h, norm_h in zip(reader.fieldnames, headers):
                    if norm_h == cand.lower():
                        return orig_h
            return None

        col_start = find_col(['start', 'start_time', 'in', 'tc_in', '開始', 'begin'])
        col_end   = find_col(['end', 'end_time', 'out', 'tc_out', '終了', 'finish'])
        col_text  = find_col(['text', 'content', 'caption', 'テキスト', 'テロップ', 'subtitle', 'body'])
        col_note  = find_col(['note', 'memo', 'comment', 'notes', 'メモ', '備考'])

        if not col_start or not col_end or not col_text:
            # ヘッダなしとして 1列目=start, 2列目=end, 3列目=text と仮定
            f.seek(0)
            reader2 = csv.reader(f, delimiter=delim)
            for idx, row in enumerate(reader2):
                if len(row) >= 3:
                    try:
                        entries.append(TelopEntry(
                            id=start_id + idx,
                            start_time=row[0].strip(),
                            end_time=row[1].strip(),
                            text=row[2].strip().replace('\\n', '\n'),
                        ))
                    except Exception:
                        continue
            return entries

        for idx, row in enumerate(reader):
            try:
                text = row.get(col_text, '').strip().replace('\\n', '\n')
                if not text:
                    continue
                entry = TelopEntry(
                    id=start_id + idx,
                    start_time=row.get(col_start, '').strip(),
                    end_time=row.get(col_end, '').strip(),
                    text=text,
                    note=row.get(col_note, '').strip() if col_note else '',
                )
                entries.append(entry)
            except Exception:
                continue

    return entries


# ─────────────────────────────────────────────
#  PNG 書き出し
# ─────────────────────────────────────────────

def export_png(
    entries: list[TelopEntry],
    settings: DesignSettings,
    output_dir: str,
    callback: Optional[Callable[[int, int, str], None]] = None,
) -> list[str]:
    """
    テロップをPNG画像として書き出す。
    Returns: 書き出したファイルパスのリスト
    """
    from .renderer import render_telop_image

    output_dir_path = Path(output_dir)
    output_dir_path.mkdir(parents=True, exist_ok=True)

    enabled = [e for e in entries if e.enabled]
    paths: list[str] = []

    for i, entry in enumerate(enabled):
        if callback:
            callback(i + 1, len(enabled), entry.text[:20])

        img = render_telop_image(entry.text, settings)
        filename = f"telop_{entry.id:04d}.png"
        out_path = output_dir_path / filename
        img.save(str(out_path), 'PNG')
        paths.append(str(out_path))

    return paths


# ─────────────────────────────────────────────
#  SRT 書き出し
# ─────────────────────────────────────────────

def export_srt(
    entries: list[TelopEntry],
    output_path: str,
    fps: float = 29.97,
) -> None:
    """テロップをSRT字幕ファイルとして書き出す。"""
    enabled = [e for e in entries if e.enabled]

    with open(output_path, 'w', encoding='utf-8-sig') as f:
        for i, entry in enumerate(enabled):
            try:
                start_sec = parse_timecode(entry.start_time, fps)
                end_sec   = parse_timecode(entry.end_time, fps)
            except ValueError:
                continue

            f.write(f"{i + 1}\n")
            f.write(f"{seconds_to_srt_time(start_sec)} --> {seconds_to_srt_time(end_sec)}\n")
            f.write(f"{entry.text}\n\n")


# ─────────────────────────────────────────────
#  FCPxml 書き出し (DaVinci Resolve / Final Cut Pro 対応)
# ─────────────────────────────────────────────

def export_fcpxml(
    entries: list[TelopEntry],
    png_paths: list[str],
    output_path: str,
    settings: DesignSettings,
) -> None:
    """
    PNG付きFCPxmlを書き出す。
    DaVinci Resolveで「タイムライン > インポート > タイムライン...」から読み込み可能。
    """
    fps = settings.frame_rate
    W = settings.canvas_width
    H = settings.canvas_height

    # FPSから timebase と frameDuration を計算
    if abs(fps - 23.976) < 0.01:
        timebase = "24000/1001s"
        frame_dur = "1001/24000s"
    elif abs(fps - 24.0) < 0.01:
        timebase = "1/24s"
        frame_dur = "1/24s"
    elif abs(fps - 25.0) < 0.01:
        timebase = "1/25s"
        frame_dur = "1/25s"
    elif abs(fps - 29.97) < 0.01:
        timebase = "30000/1001s"
        frame_dur = "1001/30000s"
    elif abs(fps - 30.0) < 0.01:
        timebase = "1/30s"
        frame_dur = "1/30s"
    elif abs(fps - 59.94) < 0.01:
        timebase = "60000/1001s"
        frame_dur = "1001/60000s"
    elif abs(fps - 60.0) < 0.01:
        timebase = "1/60s"
        frame_dur = "1/60s"
    else:
        # 汎用
        num = round(fps * 1000)
        timebase = f"{num}/1000s"
        frame_dur = f"1000/{num}s"

    def secs_to_fcptime(s: float) -> str:
        frames = round(s * fps)
        # FCPxmlは分数で時間を表現
        if fps == round(fps):
            return f"{frames}/{int(fps)}s"
        else:
            # 29.97 → 30000/1001
            if abs(fps - 29.97) < 0.01:
                return f"{frames * 1001}/30000s"
            elif abs(fps - 23.976) < 0.01:
                return f"{frames * 1001}/24000s"
            elif abs(fps - 59.94) < 0.01:
                return f"{frames * 1001}/60000s"
            else:
                return f"{frames}/{int(fps)}s"

    enabled = [e for e in entries if e.enabled]

    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<!DOCTYPE fcpxml>')
    lines.append('<fcpxml version="1.9">')
    lines.append('  <resources>')
    lines.append(f'    <format id="r0" name="FFVideoFormat{H}p{fps}" frameDuration="{frame_dur}" width="{W}" height="{H}"/>')

    for i, (entry, png_path) in enumerate(zip(enabled, png_paths)):
        try:
            start_s = parse_timecode(entry.start_time, fps)
            end_s   = parse_timecode(entry.end_time, fps)
        except ValueError:
            continue
        dur = end_s - start_s
        dur_str = secs_to_fcptime(dur)
        src_uri = Path(png_path).as_uri()
        lines.append(f'    <asset id="r{i+1}" name="telop_{entry.id:04d}" src="{src_uri}" start="0s" duration="{dur_str}" hasVideo="1" format="r0"/>')

    lines.append('  </resources>')
    lines.append('  <library>')
    lines.append('    <event name="DavinTelop">')
    lines.append('      <project name="テロップ">')

    # タイムライン全体の長さ
    max_end = 0.0
    for entry in enabled:
        try:
            max_end = max(max_end, parse_timecode(entry.end_time, fps))
        except ValueError:
            pass
    total_dur = secs_to_fcptime(max_end)

    lines.append(f'        <sequence duration="{total_dur}" format="r0" tcStart="0s" tcFormat="NDF">')
    lines.append('          <spine>')

    for i, (entry, png_path) in enumerate(zip(enabled, png_paths)):
        try:
            start_s = parse_timecode(entry.start_time, fps)
            end_s   = parse_timecode(entry.end_time, fps)
        except ValueError:
            continue
        dur = end_s - start_s
        off_str  = secs_to_fcptime(start_s)
        dur_str  = secs_to_fcptime(dur)
        lines.append(f'            <clip name="telop_{entry.id:04d}" offset="{off_str}" duration="{dur_str}" start="0s" format="r0">')
        lines.append(f'              <asset-clip ref="r{i+1}" offset="0s" duration="{dur_str}"/>')
        lines.append(f'            </clip>')

    lines.append('          </spine>')
    lines.append('        </sequence>')
    lines.append('      </project>')
    lines.append('    </event>')
    lines.append('  </library>')
    lines.append('</fcpxml>')

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


# ─────────────────────────────────────────────
#  DaVinci Resolve API 連携
# ─────────────────────────────────────────────

_RESOLVE_API_PATH = (
    "/Library/Application Support/Blackmagic Design/"
    "DaVinci Resolve/Developer/Scripting/Modules"
)


class ResolveConnector:
    """DaVinci Resolve Python API のラッパー。"""

    def __init__(self):
        self._resolve = None
        self._project = None
        self._timeline = None
        self._error: str = ""

    @property
    def error(self) -> str:
        return self._error

    def is_available(self) -> bool:
        """Resolve API モジュールが利用可能かチェック。"""
        if _RESOLVE_API_PATH not in sys.path:
            sys.path.insert(0, _RESOLVE_API_PATH)
        try:
            import DaVinciResolveScript  # noqa: F401
            return True
        except ImportError:
            return False

    def connect(self) -> bool:
        """Resolveに接続する。Resolveが起動している必要がある。"""
        if _RESOLVE_API_PATH not in sys.path:
            sys.path.insert(0, _RESOLVE_API_PATH)
        try:
            import DaVinciResolveScript as dvr
            self._resolve = dvr.scriptapp("Resolve")
            if self._resolve is None:
                self._error = "DaVinci Resolveに接続できません。\nResolveが起動中か確認してください。\n\nPreferences > General > External Scripting を有効にしてください。"
                return False

            pm = self._resolve.GetProjectManager()
            self._project = pm.GetCurrentProject()
            if self._project is None:
                self._error = "プロジェクトが開かれていません。"
                return False

            self._timeline = self._project.GetCurrentTimeline()
            if self._timeline is None:
                self._error = "タイムラインが選択されていません。"
                return False

            return True
        except ImportError:
            self._error = (
                "DaVinci Resolve スクリプトモジュールが見つかりません。\n"
                f"パス: {_RESOLVE_API_PATH}\n"
                "Resolveのバージョン・インストール状態を確認してください。"
            )
            return False
        except Exception as e:
            self._error = f"接続エラー: {e}"
            return False

    def get_timeline_info(self) -> dict:
        """現在のタイムライン情報を取得。"""
        if not self._timeline:
            return {}
        try:
            return {
                "name": self._timeline.GetName(),
                "fps": float(self._timeline.GetSetting("timelineFrameRate")),
                "width": int(self._timeline.GetSetting("timelineResolutionWidth")),
                "height": int(self._timeline.GetSetting("timelineResolutionHeight")),
                "track_count_v": self._timeline.GetTrackCount("video"),
            }
        except Exception as e:
            self._error = str(e)
            return {}

    def import_pngs_to_timeline(
        self,
        entries: list[TelopEntry],
        png_paths: list[str],
        fps: float,
        track_index: int = 2,
        callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> bool:
        """
        PNG画像をメディアプールに取り込み、タイムラインに配置する。
        track_index: 配置するビデオトラック (1始まり、2=V2)
        """
        if not self._project or not self._timeline:
            self._error = "Resolveに接続されていません。"
            return False

        try:
            media_pool = self._project.GetMediaPool()

            # ビデオトラックが足りなければ追加
            v_count = self._timeline.GetTrackCount("video")
            while v_count < track_index:
                self._timeline.AddTrack("video")
                v_count += 1

            enabled = [e for e in entries if e.enabled]

            for i, (entry, png_path) in enumerate(zip(enabled, png_paths)):
                if callback:
                    callback(i + 1, len(enabled), entry.text[:20])

                try:
                    start_s = parse_timecode(entry.start_time, fps)
                    end_s   = parse_timecode(entry.end_time, fps)
                except ValueError:
                    continue

                start_frame = seconds_to_frames(start_s, fps)
                end_frame   = seconds_to_frames(end_s, fps)
                dur_frames  = end_frame - start_frame
                if dur_frames <= 0:
                    continue

                # メディアプールに取り込み
                items = media_pool.ImportMedia([png_path])
                if not items:
                    continue

                clip_info = {
                    "mediaPoolItem": items[0],
                    "startFrame": 0,
                    "endFrame": dur_frames - 1,
                    "recordFrame": start_frame,
                    "mediaType": 1,
                    "trackIndex": track_index,
                }
                media_pool.AppendToTimeline([clip_info])

            return True
        except Exception as e:
            self._error = f"タイムライン配置エラー: {e}"
            return False
