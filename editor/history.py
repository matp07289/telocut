"""更新履歴と版の保存（WatchTower 風の自動同期の強化）。

- 素材ファイルが他のアプリで保存されるたびに「いつ・どのファイルが・どのアプリで・どれだけ変わったか」を記録
- 保存される前の版をプロジェクトの .telocut_history/versions に残す（大きすぎるファイルは残さない・各 10 版まで）
- 履歴パネルから「この版を使う」（素材をその版に切り替える）/「元のファイルに戻す」ができる
- アセットサーバーにつないでいれば、チームの「制作管理」にも更新を送る
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import List, Optional

APPS = {".psd": "Photoshop", ".psb": "Photoshop", ".aep": "After Effects", ".blend": "Blender",
        ".png": "画像編集", ".jpg": "画像編集", ".jpeg": "画像編集", ".tif": "画像編集", ".tiff": "画像編集",
        ".webp": "画像編集", ".exr": "画像編集", ".wav": "音声編集", ".mp3": "音声編集", ".flac": "音声編集",
        ".aif": "音声編集", ".aiff": "音声編集", ".mov": "動画の書き出し", ".mp4": "動画の書き出し", ".mxf": "動画の書き出し"}
KEEP = 10


def guess_app(path: str, link_src: str = "") -> str:
    if link_src:
        return "Dynamic Link（" + APPS.get(Path(link_src).suffix.lower(), "外部アプリ") + "）"
    return APPS.get(Path(path).suffix.lower(), "外部アプリ")


class History:
    def __init__(self, base: Path, limit_mb: float = 300):
        self.base = Path(base)
        self.limit = limit_mb * 1024 * 1024
        self.lock = threading.Lock()
        self.listeners = []

    @property
    def events_path(self) -> Path:
        return self.base / "events.jsonl"

    def vdir(self, mid: str) -> Path:
        return self.base / "versions" / mid

    # ── 版 ──
    def versions(self, mid: str) -> List[dict]:
        d = self.vdir(mid)
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.iterdir(), reverse=True):
            if p.is_file() and not p.name.endswith(".part"):
                st = p.stat()
                out.append({"path": str(p), "name": p.name.split("_", 2)[-1], "time": st.st_mtime, "size": st.st_size,
                            "label": p.name[:15]})
        return out

    def snapshot(self, mid: str, path: str) -> Optional[str]:
        """今のファイルを版として残す（直前の版と同じなら何もしない）。"""
        try:
            st = os.stat(path)
        except OSError:
            return None
        if st.st_size > self.limit or st.st_size == 0:
            return None
        vs = self.versions(mid)
        if vs and vs[0]["size"] == st.st_size and abs(vs[0]["time"] - st.st_mtime) < 1:
            return None
        d = self.vdir(mid)
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(st.st_mtime))
        dst = d / f"{stamp}_{Path(path).name}"
        k = 1
        while dst.exists():
            dst = d / f"{stamp}{k}_{Path(path).name}"
            k += 1
        tmp = dst.with_name(dst.name + ".part")
        shutil.copy2(path, tmp)
        os.replace(tmp, dst)
        for old in self.versions(mid)[KEEP:]:
            try:
                os.remove(old["path"])
            except OSError:
                pass
        return str(dst)

    # ── 出来事 ──
    def log(self, kind: str, media_id: str, name: str, path: str, app: str = "", detail: str = "", **extra) -> dict:
        ev = {"time": time.time(), "kind": kind, "media_id": media_id, "name": name, "path": path, "app": app,
              "detail": detail, "user": os.environ.get("USERNAME") or os.environ.get("USER") or "",
              "host": os.environ.get("COMPUTERNAME") or "", **extra}
        with self.lock:
            self.base.mkdir(parents=True, exist_ok=True)
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:
                pass
        return ev

    def events(self, limit: int = 500, media_id: str = "") -> List[dict]:
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in reversed(lines):
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            if media_id and ev.get("media_id") != media_id:
                continue
            out.append(ev)
            if len(out) >= limit:
                break
        return out


def size_delta(a: int, b: int) -> str:
    d = (b or 0) - (a or 0)
    if not a:
        return ""
    sign = "+" if d >= 0 else "−"
    d = abs(d)
    for u in ("B", "KB", "MB", "GB"):
        if d < 1024 or u == "GB":
            return f"{sign}{d:.0f} {u}" if u in ("B", "KB") else f"{sign}{d:.1f} {u}"
        d /= 1024
    return ""
