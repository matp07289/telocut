"""PNG / SRT / FCPxml 書き出し & DaVinci Resolve API連携"""
from __future__ import annotations
import csv
import os
import re
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

def _sanitize_for_filename(text: str, max_len: int = 20) -> str:
    """ファイル名に使えない文字を除去し最大長に切り詰める。"""
    text = text.replace('\n', ' ').replace('\r', '')
    text = re.sub(r'[\\/:*?"<>|\t]', '', text)
    return text.strip()[:max_len].strip()


def export_png(
    entries: list[TelopEntry],
    settings: DesignSettings,
    output_dir: str,
    callback: Optional[Callable[[int, int, str], None]] = None,
    include_text_in_filename: bool = False,
) -> list[str]:
    """
    テロップをPNG画像として書き出す。
    include_text_in_filename=True のとき、ファイル名にテロップテキストを含める。
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
        if include_text_in_filename:
            sanitized = _sanitize_for_filename(entry.text)
            filename = f"telop_{i+1:04d}_{sanitized}.png" if sanitized else f"telop_{i+1:04d}.png"
        else:
            filename = f"telop_{i+1:04d}.png"
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
#  AAF 書き出し (Avid Media Composer 対応)
# ─────────────────────────────────────────────

try:
    import aaf2 as _aaf2
    HAS_AAF2 = True
except ImportError:
    _aaf2 = None
    HAS_AAF2 = False


def _aaf_edit_rate(fps: float):
    """fps を AAF の編集レート（分数）に変換する。"""
    from fractions import Fraction
    if abs(fps - 23.976) < 0.01:
        return Fraction(24000, 1001)
    if abs(fps - 29.97) < 0.01:
        return Fraction(30000, 1001)
    if abs(fps - 59.94) < 0.01:
        return Fraction(60000, 1001)
    # 整数 fps はそのまま
    if abs(fps - round(fps)) < 0.01:
        return int(round(fps))
    return Fraction(round(fps * 1000), 1000)


def export_aaf(
    entries: list[TelopEntry],
    png_paths: list[str],
    output_path: str,
    settings: DesignSettings,
    callback: Optional[Callable[[int, int, str], None]] = None,
) -> None:
    """
    PNG 参照付き AAF を書き出す（Avid Media Composer 対応）。

    各テロップを ImportDescriptor + NetworkLocator で PNG ファイルへ
    リンクした SourceMob として作成し、CompositionMob のビデオトラックに
    タイムコード順で配置する。

    Avid Media Composer での取り込み:
      ファイル > 入力 > AAF/OMF を選択、もしくはビンへドラッグ&ドロップ。
      リンク先 PNG はインポート（取り込み）またはリンクで取り込まれる。
    """
    if not HAS_AAF2:
        raise RuntimeError(
            "pyaaf2 がインストールされていません。\n"
            "pip install pyaaf2 を実行してください。")

    fps        = settings.frame_rate
    edit_rate  = _aaf_edit_rate(fps)
    W, H       = settings.canvas_width, settings.canvas_height

    enabled = [e for e in entries if e.enabled]

    # (entry, png_path, start_frame, length_frame) を収集
    items: list[tuple] = []
    for entry, png_path in zip(enabled, png_paths):
        try:
            start_s = parse_timecode(entry.start_time, fps)
            end_s   = parse_timecode(entry.end_time, fps)
        except ValueError:
            continue
        start_frame  = seconds_to_frames(start_s, fps)
        length_frame = seconds_to_frames(end_s, fps) - start_frame
        if length_frame <= 0:
            continue
        items.append((entry, png_path, start_frame, length_frame))

    if not items:
        raise ValueError("配置できるテロップがありません。")

    with _aaf2.open(output_path, "w") as f:
        comp = f.create.CompositionMob("DavinTelop_Telops")
        f.content.mobs.append(comp)

        # タイムコードスロット（タイムラインの開始TCを 0 に）
        try:
            tc_slot = comp.create_timecode_slot(edit_rate)
        except Exception:
            tc_slot = None

        # ビデオトラック
        pic_slot = comp.create_picture_slot(edit_rate)
        seq = f.create.Sequence(media_kind="picture")
        pic_slot["Segment"].value = seq

        prev_end = 0
        total = len(items)
        for i, (entry, png_path, start_frame, length_frame) in enumerate(items):
            if callback:
                callback(i + 1, total, entry.text[:20])

            # ギャップ（Filler）
            gap = start_frame - prev_end
            if gap > 0:
                seq.components.append(
                    f.create.Filler(media_kind="picture", length=gap))

            name = Path(png_path).stem

            # ── SourceMob: 外部 PNG への参照 ──
            src = f.create.SourceMob(name)
            f.content.mobs.append(src)
            desc = f.create.ImportDescriptor()
            loc  = f.create.NetworkLocator()
            loc["URLString"].value = Path(png_path).absolute().as_uri()
            desc["Locator"].append(loc)
            src.descriptor = desc
            s_slot = src.create_picture_slot(edit_rate)
            s_slot["Segment"].value = f.create.SourceClip(
                media_kind="picture", length=length_frame)

            # ── MasterMob: SourceMob を参照 ──
            master = f.create.MasterMob(name)
            f.content.mobs.append(master)
            m_slot = master.create_picture_slot(edit_rate)
            m_clip = f.create.SourceClip(media_kind="picture", length=length_frame)
            m_clip.mob = src
            m_slot["Segment"].value = m_clip

            # ── Composition に配置 ──
            clip = f.create.SourceClip(media_kind="picture", length=length_frame)
            clip.mob = master
            seq.components.append(clip)

            prev_end = start_frame + length_frame


# ─────────────────────────────────────────────
#  Resolve コンソール用スクリプト生成
# ─────────────────────────────────────────────

def export_resolve_script(
    entries: list[TelopEntry],
    png_dir: str,
    output_path: str,
    settings: DesignSettings,
    track_index: int = 2,
    include_text_in_filename: bool = False,
) -> None:
    """
    DaVinci Resolve のスクリプトコンソールで実行するPythonスクリプトを生成する。

    使い方:
      Workspace > Console を開き、生成した .py ファイルを
      コンソールウィンドウにドラッグ&ドロップして実行する。
    """
    fps = settings.frame_rate
    enabled = [e for e in entries if e.enabled]

    telop_data: list[dict] = []
    seq = 0
    for entry in enabled:
        try:
            start_s = parse_timecode(entry.start_time, fps)
            end_s   = parse_timecode(entry.end_time, fps)
        except ValueError:
            continue
        start_frame = seconds_to_frames(start_s, fps)
        dur_frames  = seconds_to_frames(end_s, fps) - start_frame
        if dur_frames <= 0:
            continue
        seq += 1
        if include_text_in_filename:
            sanitized = _sanitize_for_filename(entry.text)
            fname = f"telop_{seq:04d}_{sanitized}.png" if sanitized else f"telop_{seq:04d}.png"
        else:
            fname = f"telop_{seq:04d}.png"
        png_path = str(Path(png_dir) / fname)
        telop_data.append({
            "png":         png_path,
            "start_frame": start_frame,
            "dur_frames":  dur_frames,
            "text":        entry.text[:40],
        })

    lines = [
        "# -*- coding: utf-8 -*-",
        '"""',
        "DavinTelop - DaVinci Resolve テロップ配置スクリプト",
        "",
        "【使い方】",
        "  1. DaVinci Resolve を起動し、タイムラインを開いておく",
        "  2. Workspace > Console を開く",
        "  3. このファイルをコンソールウィンドウへドラッグ&ドロップ",
        "  4. Enter で実行",
        '"""',
        "",
        "import os",
        "",
        "# ── Resolve 接続 ────────────────────────────────────",
        'resolve    = bmd.scriptapp("Resolve")  # noqa',
        "pm         = resolve.GetProjectManager()",
        "project    = pm.GetCurrentProject()",
        "media_pool = project.GetMediaPool()",
        "",
        "# タイムライン取得 (未選択の場合は最初のタイムラインを使用)",
        "timeline = project.GetCurrentTimeline()",
        "if timeline is None:",
        "    tl_count = project.GetTimelineCount()",
        "    if tl_count > 0:",
        "        timeline = project.GetTimelineByIndex(1)",
        "        project.SetCurrentTimeline(timeline)",
        "        print(f'タイムライン「{timeline.GetName()}」を選択しました')",
        "    else:",
        "        print('エラー: タイムラインが存在しません。先にタイムラインを作成してください。')",
        "        raise SystemExit(1)",
        "",
        f"TRACK_INDEX = {track_index}  # 配置するビデオトラック番号",
        "",
        "# ── テロップデータ (DavinTelopが自動生成) ──────────────",
        "telop_data = [",
    ]

    for item in telop_data:
        lines.append(
            f'    {{"png": r"{item["png"]}", '
            f'"start_frame": {item["start_frame"]}, '
            f'"dur_frames": {item["dur_frames"]}, '
            f'"text": {repr(item["text"])}}},  '
            f'# {item["text"][:20]}'
        )

    lines += [
        "]",
        "",
        "# ── トラック確保 ─────────────────────────────────────",
        'v_count = timeline.GetTrackCount("video")',
        "while v_count < TRACK_INDEX:",
        '    timeline.AddTrack("video")',
        "    v_count += 1",
        "",
        "# ── 有効なテロップだけ抽出 ───────────────────────────",
        "valid_items = [item for item in telop_data if os.path.exists(item['png'])]",
        "skipped = len(telop_data) - len(valid_items)",
        "if skipped:",
        "    print(f'警告: {skipped} 件のPNGが見つかりません')",
        "",
        "# ── メディアインポート (pool 重複対応: 1ファイルずつ) ──────",
        "def _find_pool_item(media_pool, file_path):",
        "    from pathlib import Path",
        "    target = Path(file_path)",
        "    try:",
        "        root = media_pool.GetRootFolder()",
        "        for mpi in (root.GetClipList() or []):",
        "            try:",
        "                fp = mpi.GetClipProperty('File Path')",
        "                if fp and Path(fp) == target:",
        "                    return mpi",
        "            except Exception:",
        "                continue",
        "    except Exception:",
        "        pass",
        "    return None",
        "",
        "imported = []",
        "for item in valid_items:",
        "    result = media_pool.ImportMedia([item['png']])",
        "    if result:",
        "        imported.append(result[0])",
        "    else:",
        "        existing = _find_pool_item(media_pool, item['png'])",
        "        if existing:",
        "            imported.append(existing)",
        "        else:",
        "            print(f'エラー: インポート失敗 - {item[\"png\"]}')",
        "            raise SystemExit(1)",
        "print(f'{len(imported)} 件のPNGをインポートしました')",
        "",
        "# ── タイムライン開始フレームオフセット取得 ────────",
        "tl_start = 0",
        "try:",
        "    tl_start = int(timeline.GetStartFrame())",
        "    print(f'タイムライン開始フレーム: {tl_start}')",
        "except Exception as e:",
        "    print(f'GetStartFrame失敗 (offset=0 で続行): {e}')",
        "",
        "# ── 1クリップずつ配置 (一括だと2本目以降が置かれない Resolve バグ回避) ──",
        "print(f'配置開始: {len(valid_items)} 件, track={TRACK_INDEX}, offset={tl_start}')",
        "placed = 0",
        "for item, mpi in zip(valid_items, imported):",
        "    clip_info = [{",
        "        'mediaPoolItem': mpi,",
        "        'startFrame':    0,",
        "        'endFrame':      item['dur_frames'] - 1,",
        "        'recordFrame':   item['start_frame'] + tl_start,",
        "        'mediaType':     1,",
        "        'trackIndex':    TRACK_INDEX,",
        "    }]",
        "    r = media_pool.AppendToTimeline(clip_info)",
        "    if r:",
        "        placed += 1",
        "        print(f'  配置: {item[\"text\"][:20]}')",
        "    else:",
        "        print(f'  警告: 配置失敗 - {item[\"text\"][:20]}')",
        "print(f'\\n完了: {placed}/{len(valid_items)} 件のテロップを配置しました')",
    ]

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


# ─────────────────────────────────────────────
#  DaVinci Resolve API 連携
# ─────────────────────────────────────────────

import platform as _platform

def _get_resolve_api_paths() -> list[str]:
    """OS別のDaVinci Resolve スクリプトモジュールパス候補を返す。
    環境変数 RESOLVE_SCRIPT_API があれば最優先で使用する。"""
    import os
    paths: list[str] = []

    # Resolve自身がセットする環境変数（最優先）
    env_api = os.environ.get("RESOLVE_SCRIPT_API")
    if env_api:
        paths.append(os.path.join(env_api, "Modules"))
        paths.append(env_api)

    if _platform.system() == "Windows":
        programdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        paths += [
            os.path.join(programdata,
                         r"Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules"),
            r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
        ]
    else:
        paths += [
            "/Library/Application Support/Blackmagic Design/"
            "DaVinci Resolve/Developer/Scripting/Modules",
        ]

    return paths


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

    def _add_api_paths(self):
        """Resolve APIパスをsys.pathに追加し、DLLパスも設定する。"""
        import os

        # モジュールパスをsys.pathに追加
        for p in _get_resolve_api_paths():
            if p not in sys.path:
                sys.path.insert(0, p)

        # Windows: fusionscript.dll のあるディレクトリをDLL検索パスに追加
        if _platform.system() == "Windows":
            program_files = os.environ.get("PROGRAMFILES",
                                           r"C:\Program Files")
            resolve_dir = os.path.join(program_files,
                                       "Blackmagic Design", "DaVinci Resolve")
            # PATH環境変数に追加
            current_path = os.environ.get("PATH", "")
            if resolve_dir not in current_path:
                os.environ["PATH"] = resolve_dir + os.pathsep + current_path
            # Python 3.8以降はos.add_dll_directory()も使用
            if os.path.isdir(resolve_dir):
                try:
                    os.add_dll_directory(resolve_dir)
                except (AttributeError, OSError):
                    pass
            # RESOLVE_SCRIPT_LIB: DaVinciResolveScript.py が imp.load_dynamic() で
            # fusionscript.dll を探す際に参照する環境変数
            fusion_dll = os.path.join(resolve_dir, "fusionscript.dll")
            if "RESOLVE_SCRIPT_LIB" not in os.environ:
                os.environ["RESOLVE_SCRIPT_LIB"] = fusion_dll

    def _load_dvr(self):
        """
        DaVinci Resolve スクリプトモジュール (fusionscript) をロードして返す。

        試行順:
          1) 通常の import DaVinciResolveScript (regular Python 環境)
          2) importlib で DaVinciResolveScript.py を直接ロード (PyInstaller 対応)
          3) fusionscript.dll を imp.load_dynamic で直接ロード (最終フォールバック)
        """
        import imp
        import importlib.util

        # ── 1) 通常 import ──────────────────────────────────────
        try:
            import DaVinciResolveScript as dvr
            if hasattr(dvr, "scriptapp"):
                return dvr
        except ImportError:
            pass

        # ── 2) importlib で DaVinciResolveScript.py を直接ロード ──
        for p in _get_resolve_api_paths():
            script_path = os.path.join(p, "DaVinciResolveScript.py")
            if not os.path.exists(script_path):
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    "DaVinciResolveScript", script_path
                )
                if spec and spec.loader:
                    dvr = importlib.util.module_from_spec(spec)
                    sys.modules["DaVinciResolveScript"] = dvr
                    spec.loader.exec_module(dvr)
                    # DaVinciResolveScript.py は sys.modules[__name__] を
                    # fusionscript DLL に差し替えるため、両方確認する
                    result = sys.modules.get("DaVinciResolveScript", dvr)
                    if hasattr(result, "scriptapp"):
                        return result
                    # "fusionscript" キーに入っている場合も確認
                    fs = sys.modules.get("fusionscript")
                    if fs and hasattr(fs, "scriptapp"):
                        return fs
            except (ImportError, OSError, Exception):
                pass

        # ── 3) fusionscript.dll を直接ロード (PyInstaller EXE 用) ──
        if _platform.system() == "Windows":
            program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
            dll_path = os.path.join(
                program_files, "Blackmagic Design", "DaVinci Resolve", "fusionscript.dll"
            )
            if os.path.exists(dll_path):
                try:
                    module = imp.load_dynamic("fusionscript", dll_path)
                    if hasattr(module, "scriptapp"):
                        return module
                except (ImportError, OSError):
                    pass

        raise ImportError("DaVinci Resolve スクリプトモジュールが見つかりません")

    def is_available(self) -> bool:
        """Resolve API モジュールが利用可能かチェック。"""
        self._add_api_paths()
        try:
            self._load_dvr()
            return True
        except ImportError:
            return False

    def connect(self) -> bool:
        """Resolveに接続する。Resolveが起動している必要がある。"""
        self._add_api_paths()
        try:
            dvr = self._load_dvr()
            self._resolve = dvr.scriptapp("Resolve")
            if self._resolve is None:
                self._error = (
                    "DaVinci Resolveに接続できません。\n"
                    "Resolveが起動中か確認してください。\n\n"
                    "Preferences > General >\n"
                    "External Scripting Using: Local を有効にしてください。"
                )
                return False

            pm = self._resolve.GetProjectManager()
            if pm is None:
                self._error = (
                    "ProjectManager の取得に失敗しました。\n"
                    "Preferences > General >\n"
                    "External Scripting Using: Local を有効にして\n"
                    "DaVinci Resolve を再起動してください。"
                )
                return False

            self._project = pm.GetCurrentProject()
            if self._project is None:
                self._error = "プロジェクトが開かれていません。"
                return False

            self._timeline = self._project.GetCurrentTimeline()
            if self._timeline is None:
                # タイムライン未選択 → 最初のタイムラインを自動選択
                tl_count = self._project.GetTimelineCount()
                if tl_count > 0:
                    self._timeline = self._project.GetTimelineByIndex(1)
                    self._project.SetCurrentTimeline(self._timeline)
                else:
                    self._error = (
                        "タイムラインが存在しません。\n"
                        "DaVinci Resolve でタイムラインを作成してから再試行してください。"
                    )
                    return False

            return True
        except ImportError:
            checked = "\n".join(_get_resolve_api_paths())
            lib_val = os.environ.get("RESOLVE_SCRIPT_LIB", "（未設定）")
            self._error = (
                "DaVinci Resolve スクリプトモジュールが見つかりません。\n\n"
                f"RESOLVE_SCRIPT_LIB: {lib_val}\n\n"
                f"検索したパス:\n{checked}\n\n"
                "確認事項:\n"
                "・DaVinci Resolve が起動しているか\n"
                "・Preferences > General >\n"
                "  外部スクリプトに使用: ローカル  になっているか\n"
                "・設定変更後は Resolve を再起動してください"
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

    def _find_pool_item(self, media_pool, file_path: str):
        """メディアプールのルートフォルダからパス一致のアイテムを返す。見つからなければ None。"""
        target = Path(file_path)
        try:
            root = media_pool.GetRootFolder()
            if not root:
                return None
            for mpi in (root.GetClipList() or []):
                try:
                    fp = mpi.GetClipProperty("File Path")
                    if fp and Path(fp) == target:
                        return mpi
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def import_pngs_to_timeline(
        self,
        entries: list[TelopEntry],
        png_paths: list[str],
        fps: float,
        track_index: int = 2,
        callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> bool:
        """
        PNG画像をメディアプールに一括取り込みし、タイムラインに一括配置する。
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

            # ── 1) 有効なエントリとPNGを収集 ──
            valid_items: list[tuple] = []  # (entry, png_path, start_frame, dur_frames)
            for entry, png_path in zip(enabled, png_paths):
                if not os.path.exists(png_path):
                    continue
                try:
                    start_s = parse_timecode(entry.start_time, fps)
                    end_s   = parse_timecode(entry.end_time, fps)
                except ValueError:
                    continue
                start_frame = seconds_to_frames(start_s, fps)
                dur_frames  = seconds_to_frames(end_s, fps) - start_frame
                if dur_frames <= 0:
                    continue
                valid_items.append((entry, png_path, start_frame, dur_frames))

            if not valid_items:
                self._error = "配置できるテロップがありません。"
                return False

            # ── 2) PNG を1ファイルずつインポート (pool 重複対応) ──
            imported = []
            for _, png_path, _, _ in valid_items:
                result = media_pool.ImportMedia([png_path])
                if result:
                    imported.append(result[0])
                else:
                    # 既に pool にある場合は pool 内を検索
                    existing = self._find_pool_item(media_pool, png_path)
                    if existing:
                        imported.append(existing)
                    else:
                        self._error = (
                            f"メディアインポートに失敗しました: "
                            f"{Path(png_path).name}"
                        )
                        return False

            if callback:
                callback(0, len(valid_items), "インポート完了、配置中...")

            # タイムライン開始フレームオフセット (例: 01:00:00:00 開始 → 216000)
            tl_start = 0
            try:
                tl_start = int(self._timeline.GetStartFrame())
            except Exception:
                pass

            # ── 3) 1クリップずつ配置 (一括だと2本目以降が置かれない Resolve バグ回避) ──
            placed = 0
            for i, ((entry, png_path, start_frame, dur_frames), mpi) in enumerate(
                    zip(valid_items, imported)):
                clip_info = [{
                    "mediaPoolItem": mpi,
                    "startFrame":   0,
                    "endFrame":     dur_frames - 1,
                    "recordFrame":  start_frame + tl_start,
                    "mediaType":    1,
                    "trackIndex":   track_index,
                }]
                r = media_pool.AppendToTimeline(clip_info)
                if r:
                    placed += 1
                if callback:
                    callback(i + 1, len(valid_items), entry.text[:20])

            if placed == 0:
                self._error = "タイムラインへの配置に失敗しました。"
                return False

            if callback:
                callback(len(valid_items), len(valid_items), f"完了 ({placed}/{len(valid_items)}件)")

            return True
        except Exception as e:
            self._error = f"タイムライン配置エラー: {e}"
            return False

    def get_current_timecode(self) -> Optional[str]:
        """現在のタイムラインの再生ヘッド位置のタイムコードを返す。"""
        if not self._timeline:
            return None
        try:
            return self._timeline.GetCurrentTimecode()
        except Exception:
            return None

    def place_lipsync_via_fusion_api(
        self,
        base_path: str,
        mouth_paths: dict,
        timeline: Optional[list] = None,
        fps: float = 30.0,
    ) -> bool:
        """
        立ち絵+口パクを Fusion comp API で直接構築する（psd2tatie 方式）。
        .comp テキストの ImportFusionComp ではなく、comp.AddTool で
        Loader / Merge / Transform を生成して MediaOut1 に接続する。
        ノードを直接作るため確実に配置できる。
        """
        if not self._project or not self._timeline:
            self._error = "Resolveに接続されていません。"
            return False
        if not os.path.exists(base_path):
            self._error = f"ベース画像が見つかりません: {Path(base_path).name}"
            return False

        SHAPE_IDX = {"a": 1, "i": 2, "u": 3, "e": 4, "o": 5, "N": 6, "closed": 0}

        try:
            insert = getattr(self._timeline,
                             "InsertFusionCompositionIntoTimeline", None)
            if insert is None:
                self._error = ("InsertFusionCompositionIntoTimeline 非対応です。\n"
                               "Resolve 18 以降が必要です。")
                return False
            item = insert()
            if not item:
                self._error = "Fusionクリップの挿入に失敗しました。"
                return False
            getcomp = getattr(item, "GetFusionCompByIndex", None)
            comp = getcomp(1) if getcomp else None
            if comp is None:
                self._error = "Fusionコンポジションを取得できませんでした。"
                return False

            try:
                comp.Lock()
            except Exception:
                pass

            def _add_loader(path, x, y):
                ld = comp.AddTool("Loader", x, y)
                # ファイル名設定（複数の方法を試す）
                p = str(path)
                set_ok = False
                for setter in (
                    lambda: ld.Clip.__setitem__(1, p),   # ld.Clip[1] = p
                    lambda: setattr(ld, "Clip", p),
                    lambda: ld.SetInput("Clip", p),
                ):
                    try:
                        setter()
                        set_ok = True
                        break
                    except Exception:
                        continue
                # 静止画を全域に伸ばす
                for attr, val in (("Loop", 1),):
                    try:
                        getattr(ld, attr).__setitem__(1, val)
                    except Exception:
                        pass
                try:
                    ld.GlobalIn = 0
                    ld.GlobalOut = 100000
                except Exception:
                    pass
                return ld, set_ok

            # ベース
            base_ld, ok = _add_loader(base_path, 0, 0)
            if not ok:
                try: comp.Unlock()
                except Exception: pass
                self._error = "Loader のファイル名設定に失敗しました（API非対応）。"
                return False
            prev = base_ld

            # 口ローダー + Merge。各口の表示/非表示は Blend を直接キーフレーム
            # （式やユーザーコントロールに依存しないため確実に1フレーム1口になる）。
            avail = [s for s in ("closed", "a", "i", "u", "e", "o", "N")
                     if mouth_paths.get(s) and os.path.exists(mouth_paths[s])]

            def _bake_blend(tool, active_frames, total):
                """Blend を active_frames で 1、それ以外で 0 にステップ・キーフレーム。
                SetInput(name, value, time) を変化点で呼び、アニメ化する。"""
                if not active_frames:
                    try: tool.SetInput("Blend", 0.0)
                    except Exception: pass
                    return
                aset = set(active_frames)
                prev_v = None
                # フレーム0から total まで、状態変化点だけキーを打つ
                for f in range(0, total + 1):
                    v = 1.0 if f in aset else 0.0
                    if v != prev_v:
                        try:
                            tool.SetInput("Blend", v, float(f))
                        except Exception:
                            pass
                        prev_v = v

            total = len(timeline) - 1 if timeline else 0
            for i, s in enumerate(avail):
                m_ld, _ = _add_loader(mouth_paths[s], 1, i + 1)
                mg = comp.AddTool("Merge", 2, i + 1)
                try:
                    mg.ConnectInput("Background", prev)
                    mg.ConnectInput("Foreground", m_ld)
                except Exception:
                    pass

                # まず静的デフォルト（closed のみ表示）。キーフレームが効かない
                # 環境でも口が重ならないようにするための保険。
                try:
                    mg.SetInput("Blend", 1.0 if s == "closed" else 0.0)
                except Exception:
                    pass

                if timeline:
                    active = [f for f, sh in enumerate(timeline) if sh == s]
                    if s == "closed":
                        active += [f for f, sh in enumerate(timeline)
                                   if sh not in avail]
                    _bake_blend(mg, sorted(active), total)
                prev = mg

            # MediaOut1 に接続
            try:
                mo = comp.FindTool("MediaOut1")
                if mo is None:
                    # 名前違い対策
                    mo = comp.FindToolByID("MediaOut") if hasattr(comp, "FindToolByID") else None
                if mo is not None:
                    mo.ConnectInput("Input", prev)
            except Exception:
                pass

            try:
                comp.Unlock()
            except Exception:
                pass
            return True
        except Exception as e:
            self._error = f"Fusion API 配置エラー: {e}"
            return False

    def place_tatie_full_via_fusion_api(
        self,
        layers: list,                  # [(name, png_path, role)] 下→上の順
        timeline: Optional[list] = None,    # 口パク用 mora タイムライン
        fps: float = 30.0,
        blink_frames: Optional[set] = None,  # 目を閉じるフレーム集合（まばたき）
        toggle_mode: bool = False,      # True: キーフレーム焼かず各レイヤーをON/OFF制御
    ) -> bool:
        """
        PSD の全レイヤーを個別 Loader として Fusion comp に展開する。
        toggle_mode=True のとき:
          口パク/まばたきのキーフレームを焼かず、全レイヤーを表示(1)にし、
          各レイヤーの表示ON/OFFチェックボックスを MediaOut1 に公開する
          （Editページのインスペクタ/Fusionで好きな立ち絵を組める）。
        toggle_mode=False のとき（従来）role 別の挙動:
          口形状キー(a/i/u/e/o/N/closed): 口パク（Blendキーフレーム）
          'eye_closed' : まばたきで閉じるフレームのみ表示
          'eye_open'   : 常時表示（閉じ目の下）
          None         : 通常レイヤー、常時表示
        レイヤーは渡された順（下→上）に Merge で重ねる。
        """
        if not self._project or not self._timeline:
            self._error = "Resolveに接続されていません。"
            return False
        valid = [(n, p, r) for (n, p, r) in layers if p and os.path.exists(p)]
        if not valid:
            self._error = "配置できるレイヤーがありません。"
            return False

        try:
            insert = getattr(self._timeline,
                             "InsertFusionCompositionIntoTimeline", None)
            if insert is None:
                self._error = "Resolve 18 以降が必要です。"
                return False
            item = insert()
            if not item:
                self._error = "Fusionクリップの挿入に失敗しました。"
                return False
            comp = item.GetFusionCompByIndex(1)
            if comp is None:
                self._error = "Fusionコンポジションを取得できませんでした。"
                return False

            try:
                comp.Lock()
            except Exception:
                pass

            import re as _re
            _used_names: set = set()

            def _safe_name(n):
                b = _re.sub(r'[^0-9A-Za-z_]', '_', n).strip('_') or "Layer"
                if b[0].isdigit():
                    b = "L_" + b
                nm = b
                k = 1
                while nm in _used_names:
                    nm = f"{b}_{k}"; k += 1
                _used_names.add(nm)
                return nm

            def _set_name(tool, n):
                try:
                    tool.SetAttrs({"TOOLS_Name": _safe_name(n)})
                except Exception:
                    pass

            def _set_color(tool, role):
                # Resolve_Script 風に役割で色分け
                col = None
                if role in ("a", "i", "u", "e", "o", "N", "closed"):
                    col = {"R": 0.92, "G": 0.43, "B": 0.0}     # 口=オレンジ
                elif role in ("eye_open", "eye_closed"):
                    col = {"R": 0.26, "G": 0.53, "B": 0.96}    # 目=青
                if col:
                    try:
                        tool.TileColor = col
                    except Exception:
                        pass

            def _add_loader(path, x, y, name=None):
                ld = comp.AddTool("Loader", x, y)
                p = str(path)
                for setter in (lambda: ld.Clip.__setitem__(1, p),
                               lambda: setattr(ld, "Clip", p),
                               lambda: ld.SetInput("Clip", p)):
                    try:
                        setter(); break
                    except Exception:
                        continue
                try:
                    ld.Loop.__setitem__(1, 1)
                except Exception:
                    pass
                try:
                    ld.GlobalIn = 0
                    ld.GlobalOut = 100000
                except Exception:
                    pass
                if name:
                    _set_name(ld, name)
                # SetDomain で DoD を画像範囲に最適化（psd2tatie 同様）
                sd = None
                try:
                    sd = comp.AddTool("SetDomain", x, y)
                    sd.SetInput("Mode", 1)   # 1 = Set To Input (画像のDoD)
                    sd.ConnectInput("Input", ld)
                except Exception:
                    sd = None
                return sd if sd is not None else ld

            # キーフレーム焼き込みの総フレーム数（口/まばたきの最大）
            total = 0
            if timeline:
                total = max(total, len(timeline) - 1)
            if blink_frames:
                total = max(total, max(blink_frames))
            mouth_shapes = {"a", "i", "u", "e", "o", "N", "closed"}

            def _bake_blend(tool, active_set):
                prev_v = None
                for f in range(0, total + 1):
                    v = 1.0 if f in active_set else 0.0
                    if v != prev_v:
                        try:
                            tool.SetInput("Blend", v, float(f))
                        except Exception:
                            pass
                        prev_v = v

            prev = None
            placed = 0
            toggle_merges = []   # [(merge, ctrl_name, layer_name)]  toggle_mode 用
            for i, (name, path, role) in enumerate(valid):
                ld = _add_loader(path, 0, i, name=name)
                _set_color(ld, role)
                if prev is None:
                    # 最下層はそのまま土台に
                    prev = ld
                    placed += 1
                    continue
                mg = comp.AddTool("Merge", 2, i)
                _set_name(mg, "Mrg_" + name)
                _set_color(mg, role)
                try:
                    mg.ConnectInput("Background", prev)
                    mg.ConnectInput("Foreground", ld)
                except Exception:
                    pass

                if toggle_mode:                 # 全レイヤーON/OFF制御モード
                    try:
                        mg.SetInput("Blend", 1.0)   # 既定は表示
                    except Exception:
                        pass
                    toggle_merges.append((mg, f"Show{i}", name))
                    prev = mg
                    placed += 1
                    continue

                if role in mouth_shapes:        # 口レイヤー
                    try:
                        mg.SetInput("Blend", 1.0 if role == "closed" else 0.0)
                    except Exception:
                        pass
                    if timeline:
                        active = {f for f, sh in enumerate(timeline) if sh == role}
                        if role == "closed":
                            shapes_present = {r for _, _, r in valid
                                             if r in mouth_shapes}
                            active |= {f for f, sh in enumerate(timeline)
                                       if sh not in shapes_present}
                        _bake_blend(mg, active)
                elif role == "eye_closed":      # まばたき: 閉じ目
                    try:
                        mg.SetInput("Blend", 0.0)
                    except Exception:
                        pass
                    if blink_frames:
                        _bake_blend(mg, set(blink_frames))
                elif role == "eye_open":        # 開き目: 常時表示（閉じ目の下）
                    try:
                        mg.SetInput("Blend", 1.0)
                    except Exception:
                        pass
                else:                           # 通常レイヤー: 常時表示
                    try:
                        mg.SetInput("Blend", 1.0)
                    except Exception:
                        pass
                prev = mg
                placed += 1

            # ルート Transform（キャラ全体を1ノードで移動/拡縮/回転）
            try:
                root_xf = comp.AddTool("Transform", 4, 0)
                _set_name(root_xf, "CharaTransform")
                root_xf.ConnectInput("Input", prev)
                prev = root_xf
            except Exception:
                pass

            # MediaOut1 へ接続
            mo = None
            try:
                mo = comp.FindTool("MediaOut1")
                if mo is not None and prev is not None:
                    mo.ConnectInput("Input", prev)
            except Exception:
                pass

            # toggle_mode: 各レイヤーの表示ON/OFFチェックを MediaOut1 に公開
            if toggle_mode and mo is not None and toggle_merges:
                try:
                    uc = {}
                    for idx, (mg, cname, lname) in enumerate(toggle_merges):
                        uc[cname] = {
                            "LINKID_DataType": "Number",
                            "INPID_InputControl": "CheckboxControl",
                            "INP_Integer": True,
                            "INP_Default": 1,
                            "LINKS_Name": f"表示: {lname[:24]}",
                            "ICS_ControlPage": "レイヤー表示",
                            "ICD_Width": 1.0,
                        }
                    mo.UserControls = uc
                    mo = mo.Refresh()
                    mo_name = mo.Name
                    # 各 Merge の Blend をチェックボックスに連動（式）
                    def _expr(tool, inp, expr):
                        try:
                            o = getattr(tool, inp)
                            if hasattr(o, "SetExpression"):
                                o.SetExpression(expr); return True
                        except Exception:
                            pass
                        try:
                            if hasattr(tool, "SetExpression"):
                                tool.SetExpression(inp, expr); return True
                        except Exception:
                            pass
                        return False
                    for mg, cname, lname in toggle_merges:
                        _expr(mg, "Blend", f"{mo_name}.{cname}")
                    # 接続し直し（Refresh で参照が変わる場合に備える）
                    try:
                        if prev is not None:
                            mo.ConnectInput("Input", prev)
                    except Exception:
                        pass
                except Exception:
                    pass

            try:
                comp.Unlock()
            except Exception:
                pass
            return placed > 0
        except Exception as e:
            self._error = f"Fusion API 全レイヤー配置エラー: {e}"
            return False

    def place_fusion_comp_at_playhead(self, comp_path: str) -> bool:
        """
        Fusion コンポジション(.comp)を再生ヘッド位置に Fusion クリップとして配置する。
        InsertFusionCompositionIntoTimeline で空の Fusion クリップを挿入し、
        ImportFusionComp で立ち絵+口パクのコンポジションを読み込む。
        """
        if not self._project or not self._timeline:
            self._error = "Resolveに接続されていません。"
            return False
        if not os.path.exists(comp_path):
            self._error = f"コンポジションが見つかりません: {Path(comp_path).name}"
            return False
        try:
            insert = getattr(self._timeline,
                             "InsertFusionCompositionIntoTimeline", None)
            if insert is None:
                self._error = ("このResolveバージョンは "
                               "InsertFusionCompositionIntoTimeline 非対応です。\n"
                               "Resolve 18 以降が必要です。")
                return False
            item = insert()
            if not item:
                self._error = "Fusionクリップの挿入に失敗しました。"
                return False
            imp = getattr(item, "ImportFusionComp", None)
            if imp is None:
                self._error = "ImportFusionComp 非対応のResolveバージョンです。"
                return False
            # パス形式を複数試す（Resolve API はバックスラッシュ/スラッシュで
            # 挙動が異なることがある）
            candidates = []
            for cp in (os.path.normpath(comp_path),
                       comp_path.replace("\\", "/"),
                       os.path.abspath(comp_path)):
                if cp not in candidates:
                    candidates.append(cp)

            last = "不明"
            for cp in candidates:
                try:
                    result = imp(cp)
                except Exception as ie:
                    last = f"例外({cp}): {ie}"
                    continue
                if result:
                    return True
                last = f"戻り値なし: {cp}"

            self._error = ("コンポジションの読み込みに失敗しました。\n"
                           f"詳細: {last}")
            return False
        except Exception as e:
            self._error = f"Fusionクリップ配置エラー: {e}"
            return False

    def place_png_at_playhead(
        self,
        png_path: str,
        duration_frames: int,
        fps: float,
        track_index: int = 2,
    ) -> bool:
        """
        PNG 1 枚を現在の再生ヘッド位置に配置する。
        duration_frames: クリップの尺（フレーム数）
        track_index: 配置するビデオトラック (1始まり)
        """
        if not self._project or not self._timeline:
            self._error = "Resolveに接続されていません。"
            return False
        if not os.path.exists(png_path):
            self._error = f"PNGが見つかりません: {Path(png_path).name}"
            return False
        if duration_frames <= 0:
            self._error = "クリップの尺が不正です。"
            return False

        try:
            media_pool = self._project.GetMediaPool()

            # ビデオトラック確保
            v_count = self._timeline.GetTrackCount("video")
            while v_count < track_index:
                self._timeline.AddTrack("video")
                v_count += 1

            # 再生ヘッド位置のタイムコード → フレーム
            tc = self._timeline.GetCurrentTimecode()
            if not tc:
                self._error = "再生ヘッド位置を取得できませんでした。"
                return False
            playhead_frame = seconds_to_frames(parse_timecode(tc, fps), fps)

            # PNG をインポート
            result = media_pool.ImportMedia([png_path])
            if result:
                mpi = result[0]
            else:
                mpi = self._find_pool_item(media_pool, png_path)
                if not mpi:
                    self._error = f"メディアインポート失敗: {Path(png_path).name}"
                    return False

            clip_info = [{
                "mediaPoolItem": mpi,
                "startFrame":    0,
                "endFrame":      duration_frames - 1,
                "recordFrame":   playhead_frame,   # GetCurrentTimecode は絶対フレーム基準
                "mediaType":     1,
                "trackIndex":    track_index,
            }]
            r = media_pool.AppendToTimeline(clip_info)
            if not r:
                self._error = "再生ヘッド位置への配置に失敗しました。"
                return False
            return True
        except Exception as e:
            self._error = f"再生ヘッド配置エラー: {e}"
            return False

    def _get_or_create_folder(self, media_pool, folder_name: str):
        """メディアプールのルートに指定名フォルダを取得または作成する。"""
        root = media_pool.GetRootFolder()
        for folder in (root.GetSubFolderList() or []):
            if folder.GetName() == folder_name:
                return folder
        return media_pool.AddSubFolder(root, folder_name)

    def import_wavs_to_timeline(
        self,
        entries: list[TelopEntry],
        wav_paths: list[str],
        fps: float,
        audio_track_index: int = 1,
        callback: Optional[Callable[[int, int, str], None]] = None,
        playhead_relative: bool = False,
    ) -> bool:
        """
        WAV ファイルをメディアプールに取り込み、タイムラインのオーディオトラックに配置。
        Resolve_Script の voice_dropper を参考に実装。

        playhead_relative=False: 各テロップの絶対TCに配置
        playhead_relative=True : 現在の再生ヘッド位置を基準に、先頭テロップを
                                 再生ヘッドに合わせ、相対間隔を保って配置
        """
        if not self._project or not self._timeline:
            self._error = "Resolveに接続されていません。"
            return False

        try:
            media_pool = self._project.GetMediaPool()

            # "DavinTelop/Voice" フォルダを確保
            dt_folder = self._get_or_create_folder(media_pool, "DavinTelop")
            media_pool.SetCurrentFolder(dt_folder)

            enabled = [e for e in entries if e.enabled]

            # オーディオトラック数を確保
            a_count = self._timeline.GetTrackCount("audio")
            while a_count < audio_track_index:
                self._timeline.AddTrack("audio")
                a_count += 1

            # タイムライン開始フレームオフセット
            tl_start = 0
            try:
                tl_start = int(self._timeline.GetStartFrame())
            except Exception:
                pass

            valid_items = []
            for entry, wav_path in zip(enabled, wav_paths):
                if not os.path.exists(wav_path):
                    continue
                try:
                    start_s = parse_timecode(entry.start_time, fps)
                    end_s   = parse_timecode(entry.end_time, fps)
                except ValueError:
                    continue
                start_frame = seconds_to_frames(start_s, fps)
                dur_frames  = seconds_to_frames(end_s, fps) - start_frame
                if dur_frames <= 0:
                    continue
                valid_items.append((entry, wav_path, start_frame, dur_frames))

            if not valid_items:
                self._error = "配置できる音声がありません。"
                return False

            # 配置基準オフセットを決定
            if playhead_relative:
                tc = self._timeline.GetCurrentTimecode()
                if not tc:
                    self._error = "再生ヘッド位置を取得できませんでした。"
                    return False
                playhead_frame = seconds_to_frames(parse_timecode(tc, fps), fps)
                first_start    = min(it[2] for it in valid_items)
                base_offset    = playhead_frame - first_start   # 先頭を再生ヘッドへ
            else:
                base_offset    = tl_start

            # WAV をインポート
            imported = []
            for _, wav_path, _, _ in valid_items:
                result = media_pool.ImportMedia([wav_path])
                if result:
                    imported.append(result[0])
                else:
                    existing = self._find_pool_item(media_pool, wav_path)
                    if existing:
                        imported.append(existing)
                    else:
                        self._error = f"音声インポート失敗: {Path(wav_path).name}"
                        return False

            # 1クリップずつ配置
            placed = 0
            for i, ((entry, _, start_frame, dur_frames), mpi) in enumerate(
                    zip(valid_items, imported)):
                clip_info = [{
                    "mediaPoolItem": mpi,
                    "startFrame":   0,
                    "endFrame":     dur_frames - 1,
                    "recordFrame":  start_frame + base_offset,
                    "mediaType":    2,      # audio
                    "trackIndex":   audio_track_index,
                }]
                r = media_pool.AppendToTimeline(clip_info)
                if r:
                    placed += 1
                if callback:
                    callback(i + 1, len(valid_items), entry.text[:20])

            if placed == 0:
                self._error = "オーディオトラックへの配置に失敗しました。"
                return False

            return True
        except Exception as e:
            self._error = f"音声配置エラー: {e}"
            return False

    def place_tachie_to_timeline(
        self,
        entries: list[TelopEntry],
        tachie_path: str,
        fps: float,
        video_track_index: int = 3,
        callback: Optional[Callable[[int, int, str], None]] = None,
        playhead_relative: bool = False,
        merge_adjacent: bool = True,
    ) -> bool:
        """
        立ち絵（静止画 PNG）を各テロップの位置でビデオトラックに配置する。
        同一の立ち絵を 1 回だけインポートし、各テロップ尺ぶん並べる。

        merge_adjacent=True: 連続するテロップを1つの立ち絵クリップに結合
                             （喋っている間ずっと立ち絵を出す用途）
        playhead_relative=True: 再生ヘッド基準で相対配置
        """
        if not self._project or not self._timeline:
            self._error = "Resolveに接続されていません。"
            return False
        if not os.path.exists(tachie_path):
            self._error = f"立ち絵が見つかりません: {Path(tachie_path).name}"
            return False

        try:
            media_pool = self._project.GetMediaPool()
            dt_folder  = self._get_or_create_folder(media_pool, "DavinTelop")
            media_pool.SetCurrentFolder(dt_folder)

            enabled = [e for e in entries if e.enabled]

            # ビデオトラック確保
            v_count = self._timeline.GetTrackCount("video")
            while v_count < video_track_index:
                self._timeline.AddTrack("video")
                v_count += 1

            tl_start = 0
            try:
                tl_start = int(self._timeline.GetStartFrame())
            except Exception:
                pass

            # 各テロップの (start_frame, end_frame) を収集
            spans = []
            for entry in enabled:
                try:
                    s = parse_timecode(entry.start_time, fps)
                    e = parse_timecode(entry.end_time, fps)
                except ValueError:
                    continue
                sf = seconds_to_frames(s, fps)
                ef = seconds_to_frames(e, fps)
                if ef > sf:
                    spans.append([sf, ef])
            if not spans:
                self._error = "配置できるテロップがありません。"
                return False

            spans.sort()
            # 隣接/重複スパンを結合
            if merge_adjacent:
                merged = [spans[0]]
                for sf, ef in spans[1:]:
                    if sf <= merged[-1][1]:
                        merged[-1][1] = max(merged[-1][1], ef)
                    else:
                        merged.append([sf, ef])
                spans = merged

            # 配置基準オフセット
            if playhead_relative:
                tc = self._timeline.GetCurrentTimecode()
                if not tc:
                    self._error = "再生ヘッド位置を取得できませんでした。"
                    return False
                playhead_frame = seconds_to_frames(parse_timecode(tc, fps), fps)
                base_offset    = playhead_frame - spans[0][0]
            else:
                base_offset    = tl_start

            # 立ち絵を1回インポート
            result = media_pool.ImportMedia([tachie_path])
            if result:
                mpi = result[0]
            else:
                mpi = self._find_pool_item(media_pool, tachie_path)
                if not mpi:
                    self._error = f"立ち絵インポート失敗: {Path(tachie_path).name}"
                    return False

            placed = 0
            for i, (sf, ef) in enumerate(spans):
                dur = ef - sf
                clip_info = [{
                    "mediaPoolItem": mpi,
                    "startFrame":   0,
                    "endFrame":     dur - 1,
                    "recordFrame":  sf + base_offset,
                    "mediaType":    1,      # video
                    "trackIndex":   video_track_index,
                }]
                if media_pool.AppendToTimeline(clip_info):
                    placed += 1
                if callback:
                    callback(i + 1, len(spans), "立ち絵配置中")

            if placed == 0:
                self._error = "立ち絵のビデオトラック配置に失敗しました。"
                return False
            return True
        except Exception as e:
            self._error = f"立ち絵配置エラー: {e}"
            return False

    def create_text_plus_srt(
        self,
        entries: list[TelopEntry],
        fps: float,
        video_track_index: int = 1,
        callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> bool:
        """
        テロップを Resolve の Text+ クリップとしてビデオトラックに配置する。
        Resolve_Script の subtitle2text_plus を参考に実装。
        """
        if not self._project or not self._timeline:
            self._error = "Resolveに接続されていません。"
            return False

        try:
            media_pool = self._project.GetMediaPool()
            enabled    = [e for e in entries if e.enabled]

            # ビデオトラック数を確保
            v_count = self._timeline.GetTrackCount("video")
            while v_count < video_track_index:
                self._timeline.AddTrack("video")
                v_count += 1

            tl_start = 0
            try:
                tl_start = int(self._timeline.GetStartFrame())
            except Exception:
                pass

            placed = 0
            for i, entry in enumerate(enabled):
                try:
                    start_s = parse_timecode(entry.start_time, fps)
                    end_s   = parse_timecode(entry.end_time, fps)
                except ValueError:
                    continue
                start_frame = seconds_to_frames(start_s, fps)
                dur_frames  = seconds_to_frames(end_s, fps) - start_frame
                if dur_frames <= 0:
                    continue

                # Text+ クリップを生成してタイムラインに配置
                clip_info = [{
                    "mediaType":   1,
                    "trackIndex":  video_track_index,
                    "recordFrame": start_frame + tl_start,
                    "duration":    dur_frames,
                }]
                result = media_pool.AppendToTimeline(clip_info)

                # 配置後に Text+ ノードにテキストを設定
                if result:
                    try:
                        tl_item = result[0]
                        comp = tl_item.GetFusionCompByIndex(1)
                        if comp:
                            txt_node = comp.FindToolByID("TextPlus")
                            if txt_node:
                                txt_node.SetInput("StyledText", entry.text)
                        placed += 1
                    except Exception:
                        placed += 1  # 配置はできたがテキスト設定失敗

                if callback:
                    callback(i + 1, len(enabled), entry.text[:20])

            return placed > 0
        except Exception as e:
            self._error = f"Text+ 配置エラー: {e}"
            return False
