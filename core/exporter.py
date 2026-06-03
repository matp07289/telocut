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
    ) -> bool:
        """
        WAV ファイルをメディアプールに取り込み、タイムラインのオーディオトラックに配置。
        Resolve_Script の voice_dropper を参考に実装。
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
                    "recordFrame":  start_frame + tl_start,
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
