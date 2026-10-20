"""TeloCut アセットサーバー（自宅用アセット管理 + レンダーファーム）。

Avid Interplay Production のように、素材を 1 か所で管理してチームで共有する小さなサーバー。
VM / 常時起動の PC / NAS 上の Docker などで動かす想定。Qt は不要（Python 標準ライブラリ + Pillow / PyAV）。

  python -m editor.assetserver --root D:\\Assets --data D:\\TeloCutServer --port 8770
  TeloCut.exe --asset-server --root D:\\Assets ...

機能
- 指定フォルダ（NAS の共有フォルダ等）の素材を登録（定期的に再スキャン）・サムネイル作成
- 検索（名前・タグ・種類）、タグ・評価・コメント、ユーザーごとのお気に入り
- アップロード（各 PC から素材を送る）、ダウンロード（範囲指定にも対応）
- プロジェクト（.tcut）の一覧と、使用中かどうか（ロック）の表示
- レンダーファーム: 区間レンダリングのジョブを受け付けて、レンダーノード（各 PC の TeloCut）に配る
- ブラウザ用の画面（http://サーバー:8770/）
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import mimetypes
import os
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

VERSION = "1.0"
MEDIA_EXT = {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".m4v", ".webm", ".wmv", ".mts", ".m2ts",
             ".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".aif", ".aiff",
             ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".psd", ".psb", ".exr",
             ".tcut", ".aaf", ".xml", ".edl", ".blend", ".aep", ".prproj"}
KIND = {**{e: "video" for e in (".mp4", ".mov", ".mkv", ".avi", ".mxf", ".m4v", ".webm", ".wmv", ".mts", ".m2ts")},
        **{e: "audio" for e in (".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".aif", ".aiff")},
        **{e: "image" for e in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".psd", ".psb",
                                ".exr")}}


def _like(prefix: str) -> str:
    """LIKE の「prefix/ で始まる」用（% と _ をそのままの文字として扱う）。"""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"


def psd_embedded_thumbnail(path: str):
    """PSD / PSB の「画像リソース」から JPEG のサムネイル（ID 1036・1033）を取り出す。無ければ None。
    ファイルの先頭部分しか読まないので、何 GB の PSD でもメモリをほとんど使わない。"""
    import struct
    from PIL import Image
    try:
        with open(path, "rb") as f:
            head = f.read(26)
            if len(head) < 26 or head[:4] != b"8BPS":
                return None
            n = struct.unpack(">I", f.read(4))[0]          # カラーモードデータ
            f.seek(n, 1)
            res_len = struct.unpack(">I", f.read(4))[0]
            end = f.tell() + res_len
            best = None
            while f.tell() + 12 <= end:
                if f.read(4) != b"8BIM":
                    break
                rid = struct.unpack(">H", f.read(2))[0]
                nlen = f.read(1)[0]
                f.seek(nlen + (1 if (nlen + 1) % 2 else 0), 1)   # パスカル文字列（偶数に揃える）
                size = struct.unpack(">I", f.read(4))[0]
                if rid in (1036, 1033) and size > 28 and size < 20 * 1024 * 1024:
                    data = f.read(size)
                    best = (rid, data[28:])
                    if rid == 1036:
                        break
                else:
                    f.seek(size, 1)
                if size % 2:
                    f.seek(1, 1)
        if best is None:
            return None
        im = Image.open(io.BytesIO(best[1]))
        im.load()
        if best[0] == 1033:                     # 古い形式は BGR
            r, g, b = im.convert("RGB").split()
            im = Image.merge("RGB", (b, g, r))
        return im
    except Exception:
        return None


class Store:
    def __init__(self, data_dir: str, roots):
        self.data = Path(data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "thumbs").mkdir(exist_ok=True)
        (self.data / "uploads").mkdir(exist_ok=True)
        (self.data / "segments").mkdir(exist_ok=True)
        (self.data / "proxies").mkdir(exist_ok=True)
        self.roots = [str(Path(r)) for r in roots] + [str(self.data / "uploads")]
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.data / "assets.db"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, path TEXT, name TEXT, kind TEXT, size INT,
                mtime REAL, duration REAL, width INT, height INT, tags TEXT DEFAULT '[]', rating INT DEFAULT 0,
                comment TEXT DEFAULT '', added REAL, credit TEXT DEFAULT '', missing INT DEFAULT 0);
            CREATE TABLE IF NOT EXISTS favorites(user TEXT, asset_id TEXT, PRIMARY KEY(user, asset_id));
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, created REAL, codec TEXT, crf INT,
                project TEXT, status TEXT, client TEXT);
            CREATE TABLE IF NOT EXISTS tasks(job_id TEXT, key TEXT, a INT, b INT, status TEXT, node TEXT,
                claimed REAL, error TEXT, PRIMARY KEY(job_id, key));
            CREATE TABLE IF NOT EXISTS nodes(name TEXT PRIMARY KEY, last_seen REAL, info TEXT);
            CREATE TABLE IF NOT EXISTS vfolders(id TEXT PRIMARY KEY, parent TEXT DEFAULT '', name TEXT,
                created REAL, sort INT DEFAULT 0);
            CREATE TABLE IF NOT EXISTS vfolder_items(folder_id TEXT, asset_id TEXT, added REAL,
                PRIMARY KEY(folder_id, asset_id));
            CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY, asset_id TEXT, t REAL, user TEXT, text TEXT,
                created REAL);
            CREATE INDEX IF NOT EXISTS notes_asset ON notes(asset_id);
            CREATE TABLE IF NOT EXISTS shots(id TEXT PRIMARY KEY, project TEXT DEFAULT '', name TEXT, seq TEXT DEFAULT '',
                tc_in TEXT DEFAULT '', tc_out TEXT DEFAULT '', status TEXT DEFAULT 'todo', assignee TEXT DEFAULT '',
                due TEXT DEFAULT '', priority INT DEFAULT 0, note TEXT DEFAULT '', asset_id TEXT DEFAULT '',
                sort INT DEFAULT 0, created REAL, updated REAL, updated_by TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, user TEXT, host TEXT,
                project TEXT DEFAULT '', kind TEXT, target TEXT, text TEXT);
            """)
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(assets)").fetchall()]
            if "label" not in cols:
                self.db.execute("ALTER TABLE assets ADD COLUMN label TEXT DEFAULT ''")
            if "dir" not in cols:
                # 以前のデータベース: フォルダ列を足して、今ある素材の分を埋める
                self.db.execute("ALTER TABLE assets ADD COLUMN dir TEXT")
                rows = self.db.execute("SELECT id, path FROM assets").fetchall()
                self.db.executemany("UPDATE assets SET dir=? WHERE id=?",
                                    [(os.path.dirname(r["path"]).replace("\\", "/"), r["id"]) for r in rows])
            self.db.execute("CREATE INDEX IF NOT EXISTS assets_dir ON assets(dir)")
            self.db.commit()
        self.scanning = False
        self.last_scan = 0.0
        self._indexing: set = set()
        self._proxy_sem = threading.Semaphore(1)        # ブラウザ用プロキシは 1 本ずつ（VM の CPU・メモリを守る）
        self._proxy_state: dict = {}                    # id → {"status": making/ready/error, "progress": 0〜1}

    # ── 素材 ─────────────────────────────────
    @staticmethod
    def aid(path: str) -> str:
        return hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:16]

    def scan(self):
        if self.scanning:
            return
        self.scanning = True
        try:
            os.nice(10)        # Linux: 走査はこのスレッドだけ優先度を下げ、検索や取り込みの応答を優先
        except (AttributeError, OSError):
            pass
        try:
            seen = set()
            for root in self.roots:
                for dp, _dirs, files in os.walk(root):
                    for f in files:
                        ext = Path(f).suffix.lower()
                        if ext not in MEDIA_EXT or f.startswith("."):
                            continue
                        p = os.path.join(dp, f)
                        seen.add(self.aid(p))
                        try:
                            self.index(p)
                        except Exception:
                            continue
            with self.lock:
                rows = self.db.execute("SELECT id FROM assets").fetchall()
                for r in rows:
                    self.db.execute("UPDATE assets SET missing=? WHERE id=?", (0 if r["id"] in seen else 1, r["id"]))
                self.db.commit()
        finally:
            self.scanning = False
            self.last_scan = time.time()

    def index(self, path: str) -> dict:
        st = os.stat(path)
        i = self.aid(path)
        with self.lock:
            row = self.db.execute("SELECT * FROM assets WHERE id=?", (i,)).fetchone()
        if row and row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) < 1:
            return dict(row)
        ext = Path(path).suffix.lower()
        kind = KIND.get(ext, "other" if ext not in (".tcut",) else "project")
        dur, w, h = 0.0, 0, 0
        try:
            if kind in ("video", "audio"):
                import av
                with av.open(path) as c:
                    dur = (c.duration or 0) / 1e6
                    if c.streams.video:
                        w, h = c.streams.video[0].codec_context.width, c.streams.video[0].codec_context.height
            elif kind == "image" and ext in (".psd", ".psb"):
                import struct
                with open(path, "rb") as f:
                    head = f.read(26)
                if head[:4] == b"8BPS":
                    h, w = struct.unpack(">II", head[14:22])      # PSD のヘッダー: 高さ・幅
            elif kind == "image":
                from PIL import Image
                with Image.open(path) as im:
                    w, h = im.size
        except Exception:
            pass
        with self.lock:
            if row:
                self.db.execute("UPDATE assets SET size=?, mtime=?, duration=?, width=?, height=?, missing=0, dir=? "
                                "WHERE id=?", (st.st_size, st.st_mtime, dur, w, h, os.path.dirname(path).replace("\\", "/"), i))
            else:
                self.db.execute("INSERT INTO assets(id,path,name,kind,size,mtime,duration,width,height,added,dir) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (i, path, Path(path).name, kind, st.st_size, st.st_mtime, dur, w, h, time.time(),
                                 os.path.dirname(path).replace("\\", "/")))
            self.db.commit()
        thumb = self.data / "thumbs" / f"{i}.jpg"
        if thumb.exists():
            thumb.unlink()
        return self.get(i)

    # サムネイルを同時に作る数（多いと NAS 越しのデコードで VM のメモリ・CPU が尽きて応答しなくなる）
    _thumb_sem = threading.Semaphore(2)

    def thumb(self, i: str, t: Optional[float] = None, size: int = 320) -> Optional[bytes]:
        """サムネイル（size=320）/ プレビュー（size=960 など）。動画は t 秒の位置のコマ。"""
        size = max(64, min(1920, int(size)))
        tag = ("" if t is None else f"_t{int(round(t * 10))}") + ("" if size == 320 else f"_s{size}")
        p = self.data / "thumbs" / f"{i}{tag}.jpg"
        if p.exists():
            return p.read_bytes()
        a = self.get(i)
        if not a or not os.path.exists(a["path"]):
            return None
        if not self._thumb_sem.acquire(timeout=20):
            return None                        # 混んでいる → 今回はサムネイルなし（次の表示で作る）
        try:
            if p.exists():
                return p.read_bytes()
            return self._make_thumb(a, p, t, size)
        finally:
            self._thumb_sem.release()

    def _make_thumb(self, a: dict, p: Path, t: Optional[float] = None, size: int = 320) -> Optional[bytes]:
        try:
            from PIL import Image
            im = None
            if a["kind"] == "image":
                if a["path"].lower().endswith((".psd", ".psb")):
                    # Photoshop が保存している小さな JPEG プレビューを、ヘッダーだけ読んで取り出す（レイヤーは読まない）
                    im = psd_embedded_thumbnail(a["path"]) if size <= 320 else None
                    if im is None:
                        if (a.get("size") or 0) > 100 * 1024 * 1024:
                            return None        # プレビューの無い巨大な PSD は合成しない（メモリを使い切るため）
                        from psd_tools import PSDImage
                        im = PSDImage.open(a["path"]).composite()
                else:
                    im = Image.open(a["path"])
                    if im.format == "JPEG":
                        im.draft("RGB", (640, 360))        # JPEG は縮小しながら読む
                    elif im.width * im.height > 60_000_000:
                        return None            # 巨大な PNG / TIFF は読み込むと数 GB になるので作らない
            elif a["kind"] == "video":
                import av
                with av.open(a["path"]) as c:
                    s = c.streams.video[0]
                    pos = min(1.0, (a["duration"] or 0) / 3) if t is None else max(0.0, min(t, (a["duration"] or 0) - 0.05))
                    c.seek(int(pos / float(s.time_base)), stream=s)
                    for f in c.decode(s):
                        im = f.to_image()
                        break
            if im is None:
                return None
            im = im.convert("RGB")
            im.thumbnail((size, size * 9 // 16 if size > 320 else 180))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            p.write_bytes(buf.getvalue())
            return buf.getvalue()
        except Exception:
            return None

    def get(self, i: str) -> Optional[dict]:
        with self.lock:
            r = self.db.execute("SELECT * FROM assets WHERE id=?", (i,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["tags"] = json.loads(d.get("tags") or "[]")
        return d

    # ── 実際のフォルダ（マウントしている構成そのまま）──
    def _inside_roots(self, path: str) -> Optional[str]:
        """素材フォルダの中のパスなら正規化して返す（外側を見せない）。"""
        rp = os.path.realpath(path)
        for r in self.roots:
            rr = os.path.realpath(r)
            if rp == rr or rp.startswith(rr.rstrip(os.sep) + os.sep):
                return rp
        return None

    def browse(self, path: str, all_files: bool = False) -> dict:
        """フォルダの中身をそのまま返す（空のフォルダ・素材以外のファイルも）。素材は登録済みの情報と合わせる。
        まだ登録していない素材は裏で登録し、pending で知らせる（少し後にもう一度読むと情報が付く）。"""
        if not path:
            return {"path": "", "dirs": [{"name": Path(r).name or r, "path": r.replace("\\", "/")} for r in self.roots],
                    "files": [], "pending": 0}
        p = self._inside_roots(path)
        if p is None or not os.path.isdir(p):
            raise ValueError("素材フォルダの外か、存在しないフォルダです")
        dirs, files = [], []
        with os.scandir(p) as it:
            for e in it:
                if e.name.startswith(".") or e.name in ("TeloCutServer", "@eaDir", "#recycle", "$RECYCLE.BIN"):
                    continue
                try:
                    if e.is_dir(follow_symlinks=True):
                        dirs.append({"name": e.name, "path": e.path.replace("\\", "/")})
                    elif e.is_file(follow_symlinks=True):
                        ext = Path(e.name).suffix.lower()
                        if not all_files and ext not in MEDIA_EXT:
                            continue
                        st = e.stat()
                        files.append({"name": e.name, "path": e.path.replace("\\", "/"), "size": st.st_size,
                                      "mtime": st.st_mtime, "kind": KIND.get(ext, "other"), "media": ext in MEDIA_EXT})
                except OSError:
                    continue
        with self.lock:
            rows = {r["path"].replace("\\", "/"): dict(r) for r in
                    self.db.execute("SELECT * FROM assets WHERE dir=?", (p.replace("\\", "/"),)).fetchall()}
        todo = []
        for f in files:
            r = rows.get(f["path"])
            if r is not None:
                r["tags"] = json.loads(r.get("tags") or "[]")
                f.update({k: v for k, v in r.items() if k not in ("path", "name")})
            elif f["media"]:
                todo.append(f["path"])
        if todo:
            self._index_later(todo)
        dirs.sort(key=lambda d: d["name"].lower())
        files.sort(key=lambda f: f["name"].lower())
        return {"path": p.replace("\\", "/"), "dirs": dirs, "files": files, "pending": len(todo)}

    def _index_later(self, paths):
        new = [x for x in paths if x not in self._indexing]
        if not new:
            return
        self._indexing.update(new)

        def work():
            for x in new:
                try:
                    self.index(x)
                except Exception:
                    pass
                finally:
                    self._indexing.discard(x)
        threading.Thread(target=work, daemon=True).start()

    def rescan_dir(self, path: str, recursive: bool = False) -> dict:
        p = self._inside_roots(path)
        if p is None:
            raise ValueError("素材フォルダの外です")
        found = []
        walker = os.walk(p) if recursive else [(p, [], os.listdir(p))]
        for dp, _d, fs in walker:
            for f in fs:
                if Path(f).suffix.lower() in MEDIA_EXT and not f.startswith("."):
                    found.append(os.path.join(dp, f))
        self._index_later(found)
        return {"queued": len(found)}

    def meta(self, i: str) -> dict:
        """コーデック・fps・ビットレート・音声などの詳しい情報。"""
        a = self.get(i)
        if not a:
            return {}
        out = {"path": a["path"], "size": a.get("size")}
        if a["kind"] in ("video", "audio"):
            import av
            with av.open(a["path"]) as c:
                out["format"] = c.format.long_name if c.format else ""
                out["bitrate"] = c.bit_rate or 0
                out["duration"] = (c.duration or 0) / 1e6
                for s_ in c.streams:
                    cc = s_.codec_context
                    if s_.type == "video":
                        out["video"] = {"codec": cc.name, "width": cc.width, "height": cc.height,
                                        "fps": float(s_.average_rate or s_.guessed_rate or 0), "pix_fmt": str(cc.pix_fmt)}
                    elif s_.type == "audio" and "audio" not in out:
                        out["audio"] = {"codec": cc.name, "rate": cc.sample_rate, "channels": cc.channels}
        elif a["kind"] == "image" and not a["path"].lower().endswith((".psd", ".psb")):
            from PIL import Image
            with Image.open(a["path"]) as im:
                out["image"] = {"format": im.format, "mode": im.mode, "width": im.width, "height": im.height}
        return out

    def waveform(self, i: str, bins: int = 600) -> list:
        """音声の波形（0〜1 の山の高さ。最初の 30 分まで）。"""
        bins = max(50, min(2000, int(bins)))
        cp = self.data / "thumbs" / f"{i}_wave{bins}.json"
        if cp.exists():
            return json.loads(cp.read_text())
        a = self.get(i)
        if not a or a["kind"] not in ("audio", "video"):
            return []
        import av
        import numpy as np
        with self._thumb_sem:
            peaks = []
            with av.open(a["path"]) as c:
                if not c.streams.audio:
                    return []
                st = c.streams.audio[0]
                rs = av.AudioResampler(format="s16", layout="mono", rate=4000)
                limit = 30 * 60 * 4000
                n = 0
                for fr in c.decode(st):
                    for r in rs.resample(fr):
                        x = np.abs(r.to_ndarray().reshape(-1).astype(np.int32))
                        peaks.append(x)
                        n += x.size
                    if n >= limit:
                        break
            if not peaks:
                return []
            x = np.concatenate(peaks)[:limit]
            k = max(1, x.size // bins)
            y = x[:k * bins].reshape(-1, k).max(axis=1) / 32768.0
            out = [round(float(v), 3) for v in y]
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(out))
        return out

    def duplicates(self, limit: int = 200) -> list:
        """名前と大きさが同じファイル（コピーの重複の候補）。"""
        with self.lock:
            rows = self.db.execute("SELECT name, size, COUNT(*) AS n FROM assets WHERE missing=0 AND size>0 "
                                   "GROUP BY name, size HAVING n>1 ORDER BY size DESC LIMIT ?", (int(limit),)).fetchall()
            out = []
            for r in rows:
                items = self.db.execute("SELECT * FROM assets WHERE missing=0 AND name=? AND size=?",
                                        (r["name"], r["size"])).fetchall()
                out.append({"name": r["name"], "size": r["size"], "items": [dict(x) for x in items]})
        return out

    def stats(self) -> dict:
        with self.lock:
            kinds = {r["kind"]: (r["n"], r["sz"] or 0) for r in self.db.execute(
                "SELECT kind, COUNT(*) AS n, SUM(size) AS sz FROM assets WHERE missing=0 GROUP BY kind").fetchall()}
        return {"kinds": {k: {"count": v[0], "size": v[1]} for k, v in kinds.items()},
                "total": sum(v[0] for v in kinds.values()), "total_size": sum(v[1] for v in kinds.values()),
                "scanning": self.scanning, "last_scan": self.last_scan, "indexing": len(self._indexing),
                "roots": self.roots}

    # ── 片付け（プロキシの容量・消えた素材のサムネイル / プロキシ）──
    def housekeeping(self, proxy_gb: float = 20.0) -> dict:
        """ブラウザ用プロキシを proxy_gb 以下に（古く使っていない物から消す）、消えた素材のサムネイル等を消す。"""
        with self.lock:
            alive = {r[0] for r in self.db.execute("SELECT id FROM assets WHERE missing=0")}
        removed = freed = 0
        for d in ("thumbs", "proxies"):
            for f in (self.data / d).iterdir() if (self.data / d).is_dir() else []:
                aid = f.name.split("_", 1)[0].split(".", 1)[0]
                if aid not in alive or f.name.endswith(".part.mp4") and time.time() - f.stat().st_mtime > 3600:
                    try:
                        freed += f.stat().st_size
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
        px = sorted(((f.stat().st_atime, f.stat().st_size, f) for f in (self.data / "proxies").glob("*")
                     if f.is_file()), key=lambda x: x[0])
        total = sum(x[1] for x in px)
        limit = proxy_gb * 1024 ** 3
        for _t, size, f in px:
            if total <= limit:
                break
            try:
                f.unlink()
                total -= size
                freed += size
                removed += 1
            except OSError:
                pass
        return {"removed": removed, "freed": freed, "proxies": total}

    # ── 素材フォルダの情報（Windows のドライブ・ネットワークパス）──
    def roots_info(self) -> list:
        """素材フォルダごとの {path, name, unc, drive}。
        unc は /proc/mounts の接続元から（CIFS: //NAS/共有 → \\\\NAS\\共有、NFS: NAS:/…/共有 → \\\\NAS\\共有）。
        drive は TeloCut（各 PC）が教えてくれたドライブ文字（roots.json）。"""
        mounts = {}
        try:
            for line in open("/proc/mounts", encoding="utf-8", errors="replace"):
                f = line.split()
                if len(f) >= 3:
                    mounts[f[1].replace("\\040", " ")] = (f[0].replace("\\040", " "), f[2])
        except OSError:
            pass
        saved = self._roots_saved()
        out = []
        for r in self.roots:
            rr = r.replace("\\", "/")
            unc = ""
            src = mounts.get(rr)
            if src:
                dev, typ = src
                if typ.startswith("cifs") or typ.startswith("smb"):
                    unc = "\\\\" + dev.lstrip("/").replace("/", "\\")
                elif typ.startswith("nfs") and ":" in dev:
                    host, path = dev.split(":", 1)
                    unc = f"\\\\{host}\\{path.rstrip('/').rsplit('/', 1)[-1]}"
            sv = saved.get(rr, {})
            is_up = rr == str(self.data / "uploads").replace("\\", "/")
            out.append({"path": rr, "name": "アップロード（サーバー）" if is_up else (Path(r).name or r),
                        "unc": sv.get("unc") or unc, "drive": sv.get("drive", ""), "uploads": is_up})
        return out

    def _roots_saved(self) -> dict:
        try:
            return json.loads((self.data / "roots.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def set_roots_info(self, d: dict) -> list:
        """{サーバーのパス: {"drive": "Z:", "unc": "\\\\NAS\\共有"}}（TeloCut が分かったときに送ってくる）"""
        saved = self._roots_saved()
        known = {r.replace("\\", "/") for r in self.roots}
        for k, v in (d or {}).items():
            k = k.replace("\\", "/")
            if k in known and isinstance(v, dict):
                cur = saved.setdefault(k, {})
                for f in ("drive", "unc"):
                    if v.get(f):
                        cur[f] = str(v[f])[:200]
        (self.data / "roots.json").write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
        return self.roots_info()

    # ── タイムコード付きコメント（レビュー用）──
    def notes(self, asset_id: str) -> list:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM notes WHERE asset_id=? ORDER BY t, created",
                                                     (asset_id,)).fetchall()]

    def add_note(self, asset_id: str, t: float, user: str, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("コメントが空です")
        n = {"id": hashlib.md5(f"{asset_id}{time.time()}{text}".encode()).hexdigest()[:12], "asset_id": asset_id,
             "t": max(0.0, float(t or 0)), "user": (user or "")[:40], "text": text[:2000], "created": time.time()}
        with self.lock:
            self.db.execute("INSERT INTO notes VALUES(:id,:asset_id,:t,:user,:text,:created)", n)
            self.db.commit()
        a = self.get(asset_id) or {}
        self.add_activity(n["user"], "", "", "note", asset_id, f"{a.get('name', '素材')} にコメント: {n['text'][:80]}")
        return n

    def delete_note(self, nid: str) -> dict:
        with self.lock:
            self.db.execute("DELETE FROM notes WHERE id=?", (nid,))
            self.db.commit()
        return {"ok": True}

    def note_counts(self, ids) -> dict:
        ids = list(ids)
        if not ids:
            return {}
        with self.lock:
            q = ",".join("?" * len(ids))
            return {r[0]: r[1] for r in self.db.execute(
                f"SELECT asset_id, COUNT(*) FROM notes WHERE asset_id IN ({q}) GROUP BY asset_id", ids)}

    # ── まとめて変更 ──
    def bulk_update(self, ids, d: dict) -> dict:
        """複数の素材の評価・ラベル・タグ（追加 / 削除）をまとめて変える。"""
        n = 0
        for i in ids or []:
            a = self.get(i)
            if not a:
                continue
            ch = {k: d[k] for k in ("rating", "label") if k in d}
            if d.get("tags_add") or d.get("tags_remove"):
                tags = [t for t in a.get("tags") or [] if t not in (d.get("tags_remove") or [])]
                for t in d.get("tags_add") or []:
                    if t and t not in tags:
                        tags.append(t)
                ch["tags"] = tags
            if ch:
                self.update(i, ch)
                n += 1
        return {"updated": n}

    # ── ブラウザで再生できない素材のプロキシ（H.264 / AAC の MP4）──
    BROWSER_VIDEO = {"h264", "vp8", "vp9", "av1"}
    BROWSER_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".webm"}

    def playable(self, a: dict, meta: Optional[dict] = None) -> bool:
        ext = Path(a["path"]).suffix.lower()
        if a["kind"] == "audio":
            return ext in self.BROWSER_AUDIO_EXT
        if a["kind"] != "video":
            return False
        if ext not in (".mp4", ".m4v", ".mov", ".webm", ".mkv"):
            return False
        m = meta if meta is not None else self.meta(a["id"])
        return (m.get("video") or {}).get("codec") in self.BROWSER_VIDEO and \
            (m.get("video") or {}).get("pix_fmt", "yuv420p") in ("yuv420p", "yuvj420p", "nv12")

    def proxy_path(self, i: str, audio_only: bool) -> Path:
        return self.data / "proxies" / f"{i}.{'m4a' if audio_only else 'mp4'}"

    def proxy(self, i: str, start: bool = True) -> dict:
        """プロキシの状態。無ければ作り始める（start=True）。"""
        a = self.get(i)
        if not a or a["kind"] not in ("video", "audio"):
            return {"status": "none"}
        p = self.proxy_path(i, a["kind"] == "audio")
        if p.exists() and p.stat().st_mtime >= (a.get("mtime") or 0):
            return {"status": "ready", "url": f"/api/assets/{i}/proxyfile"}
        st = self._proxy_state.get(i)
        if st and st["status"] in ("making", "waiting"):
            return st
        if st and st["status"] == "error" and not start:
            return st
        if not start:
            return {"status": "none"}
        self._proxy_state[i] = {"status": "waiting", "progress": 0.0}
        threading.Thread(target=self._make_proxy, args=(a, p), daemon=True).start()
        return self._proxy_state[i]

    def _make_proxy(self, a: dict, p: Path):
        import av
        i = a["id"]
        tmp = p.with_suffix(".part" + p.suffix)
        with self._proxy_sem:
            self._proxy_state[i] = {"status": "making", "progress": 0.0}
            try:
                try:
                    os.nice(5)
                except (AttributeError, OSError):
                    pass
                dur = float(a.get("duration") or 0) or 1.0
                with av.open(a["path"]) as src, av.open(str(tmp), "w", format="mp4",
                                                         options={"movflags": "+faststart"}) as dst:
                    vin = src.streams.video[0] if src.streams.video and a["kind"] == "video" else None
                    ain = src.streams.audio[0] if src.streams.audio else None
                    vout = aout = None
                    if vin is not None:
                        w0 = vin.codec_context.width or 1280
                        h0 = vin.codec_context.height or 720
                        w = min(960, w0) // 2 * 2
                        h = max(2, int(round(h0 * w / max(1, w0))) // 2 * 2)
                        rate = vin.average_rate or vin.guessed_rate or 30
                        vout = dst.add_stream("libx264", rate=rate)
                        vout.width, vout.height, vout.pix_fmt = w, h, "yuv420p"
                        vout.options = {"preset": "veryfast", "crf": "26"}
                        vin.thread_type = "AUTO"
                    if ain is not None:
                        aout = dst.add_stream("aac", rate=48000)
                        aout.layout = "stereo"
                        res = av.AudioResampler(format="fltp", layout="stereo", rate=48000)
                    streams = [s_ for s_ in (vin, ain) if s_ is not None]
                    last = 0.0
                    for pkt in src.demux(*streams):
                        for fr in pkt.decode():
                            if pkt.stream is vin:
                                fr = fr.reformat(width=vout.width, height=vout.height, format="yuv420p")
                                fr.pts = None
                                for op in vout.encode(fr):
                                    dst.mux(op)
                            else:
                                for rf in res.resample(fr):
                                    rf.pts = None
                                    for op in aout.encode(rf):
                                        dst.mux(op)
                            if fr.time is not None and fr.time - last > 1:
                                last = fr.time
                                self._proxy_state[i]["progress"] = min(0.99, fr.time / dur)
                    for o in (vout, aout):
                        if o is not None:
                            for op in o.encode(None):
                                dst.mux(op)
                os.replace(tmp, p)
                self._proxy_state[i] = {"status": "ready", "url": f"/api/assets/{i}/proxyfile", "progress": 1.0}
            except Exception as ex:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                self._proxy_state[i] = {"status": "error", "error": str(ex)[:300]}

    # ── 制作管理（ショット・活動）──
    SHOT_STATUS = ("todo", "wip", "review", "fix", "done")
    SHOT_FIELDS = ("project", "name", "seq", "tc_in", "tc_out", "status", "assignee", "due", "priority", "note",
                   "asset_id", "sort")

    def shots(self, project: str = "") -> list:
        with self.lock:
            q = "SELECT * FROM shots" + (" WHERE project=?" if project else "") + " ORDER BY sort, created"
            return [dict(r) for r in self.db.execute(q, (project,) if project else ())]

    def shot_create(self, d: dict, user: str = "") -> dict:
        import uuid
        now = time.time()
        sh = {k: d.get(k, "") for k in self.SHOT_FIELDS}
        sh["priority"] = int(d.get("priority") or 0)
        sh["sort"] = int(d.get("sort") or 0)
        sh["status"] = d.get("status") if d.get("status") in self.SHOT_STATUS else "todo"
        sh["name"] = (sh["name"] or "ショット").strip()[:200]
        sh.update(id=uuid.uuid4().hex[:12], created=now, updated=now, updated_by=user[:40])
        with self.lock:
            self.db.execute(f"INSERT INTO shots({','.join(sh)}) VALUES({','.join('?' * len(sh))})", list(sh.values()))
            self.db.commit()
        self.add_activity(user, "", sh["project"], "shot", sh["id"], f"ショット「{sh['name']}」を追加")
        return sh

    def shot_update(self, i: str, d: dict, user: str = "") -> dict:
        with self.lock:
            old = self.db.execute("SELECT * FROM shots WHERE id=?", (i,)).fetchone()
            if old is None:
                raise ValueError("ショットがありません")
            old = dict(old)
            ch = {k: d[k] for k in self.SHOT_FIELDS if k in d}
            if "status" in ch and ch["status"] not in self.SHOT_STATUS:
                raise ValueError("状態が正しくありません")
            if not ch:
                return old
            ch.update(updated=time.time(), updated_by=user[:40])
            self.db.execute(f"UPDATE shots SET {','.join(k + '=?' for k in ch)} WHERE id=?", list(ch.values()) + [i])
            self.db.commit()
            new = dict(self.db.execute("SELECT * FROM shots WHERE id=?", (i,)).fetchone())
        jp = {"todo": "未着手", "wip": "作業中", "review": "レビュー待ち", "fix": "修正", "done": "OK"}
        if "status" in d and d["status"] != old["status"]:
            self.add_activity(user, "", new["project"], "status", i,
                              f"「{new['name']}」{jp.get(old['status'], old['status'])} → {jp.get(new['status'], new['status'])}")
        elif "assignee" in d and d["assignee"] != old["assignee"]:
            self.add_activity(user, "", new["project"], "assign", i, f"「{new['name']}」の担当を {new['assignee'] or 'なし'} に")
        return new

    def shot_delete(self, i: str, user: str = "") -> dict:
        with self.lock:
            r = self.db.execute("SELECT name, project FROM shots WHERE id=?", (i,)).fetchone()
            self.db.execute("DELETE FROM shots WHERE id=?", (i,))
            self.db.commit()
        if r:
            self.add_activity(user, "", r["project"], "shot", i, f"ショット「{r['name']}」を削除")
        return {"ok": True}

    def add_activity(self, user: str, host: str, project: str, kind: str, target: str, text: str) -> dict:
        ev = {"time": time.time(), "user": (user or "")[:40], "host": (host or "")[:60], "project": (project or "")[:200],
              "kind": (kind or "")[:20], "target": (target or "")[:200], "text": (text or "")[:1000]}
        with self.lock:
            cur = self.db.execute("INSERT INTO activity(time,user,host,project,kind,target,text) VALUES(?,?,?,?,?,?,?)",
                                  list(ev.values()))
            self.db.execute("DELETE FROM activity WHERE id < ?", (cur.lastrowid - 5000,))
            self.db.commit()
        ev["id"] = cur.lastrowid
        return ev

    def activity(self, limit: int = 100, project: str = "", since_id: int = 0) -> list:
        with self.lock:
            q = "SELECT * FROM activity WHERE id>?" + (" AND project=?" if project else "") + " ORDER BY id DESC LIMIT ?"
            args = [int(since_id)] + ([project] if project else []) + [max(1, min(1000, int(limit)))]
            return [dict(r) for r in self.db.execute(q, args)]

    def dashboard(self, project: str = "") -> dict:
        sh = self.shots(project)
        today = time.strftime("%Y-%m-%d")
        by_status = {k: 0 for k in self.SHOT_STATUS}
        by_user: dict = {}
        overdue = []
        for x in sh:
            by_status[x["status"]] = by_status.get(x["status"], 0) + 1
            u = by_user.setdefault(x["assignee"] or "（未割り当て）", {k: 0 for k in self.SHOT_STATUS})
            u[x["status"]] = u.get(x["status"], 0) + 1
            if x["due"] and x["due"] < today and x["status"] != "done":
                overdue.append(x)
        with self.lock:
            projects = [r[0] for r in self.db.execute("SELECT DISTINCT project FROM shots WHERE project<>'' ORDER BY project")]
        nodes = self.nodes()
        jobs = self.jobs(20)
        return {"total": len(sh), "by_status": by_status, "by_user": by_user, "overdue": overdue,
                "done_ratio": (by_status.get("done", 0) / len(sh)) if sh else 0.0, "projects": projects,
                "nodes_online": sum(1 for n in nodes if n.get("online")), "nodes": len(nodes),
                "jobs_running": sum(1 for j in jobs if j.get("status") not in ("done", "failed", "cancelled")),
                "activity": self.activity(30, project)}

    # ── フォルダ（NAS と同じ構成）──
    def folders(self, parent: str = "") -> dict:
        """parent の直下のフォルダ（中の素材数つき・下のフォルダも含めた数）と、直下の素材数。parent="" なら素材フォルダの一覧。"""
        parent = parent.replace("\\", "/")
        if not parent:
            out = []
            for r in [x.replace("\\", "/").rstrip("/") for x in self.roots]:
                with self.lock:
                    n = self.db.execute("SELECT COUNT(*) FROM assets WHERE missing=0 AND (dir=? OR dir LIKE ? ESCAPE '\\')",
                                        (r, _like(r))).fetchone()[0]
                up = str(self.data / "uploads").replace("\\", "/").rstrip("/")
                out.append({"path": r, "name": "アップロード（サーバー）" if r == up else (Path(r).name or r),
                            "count": n, "has_children": True})
            return {"path": "", "folders": out, "files": 0}
        parent = parent.rstrip("/")
        with self.lock:
            rows = self.db.execute("SELECT dir, COUNT(*) AS n FROM assets WHERE missing=0 AND (dir=? OR dir LIKE ? ESCAPE '\\') "
                                   "GROUP BY dir", (parent, _like(parent))).fetchall()
        here = 0
        kids: dict = {}
        deeper: set = set()
        for r in rows:
            if r["dir"] == parent:
                here += r["n"]
                continue
            rest = r["dir"][len(parent) + 1:]
            name = rest.split("/", 1)[0]
            kids[name] = kids.get(name, 0) + r["n"]
            if "/" in rest:
                deeper.add(name)
        return {"path": parent, "files": here,
                "folders": [{"path": f"{parent}/{k}", "name": k, "count": v, "has_children": k in deeper}
                            for k, v in sorted(kids.items(), key=lambda x: x[0].lower())]}

    # ── 独自フォルダ（みんなで共有）──
    def vfolders(self) -> list:
        with self.lock:
            rows = self.db.execute("SELECT v.*, (SELECT COUNT(*) FROM vfolder_items i JOIN assets a ON a.id=i.asset_id "
                                   "AND a.missing=0 WHERE i.folder_id=v.id) AS count FROM vfolders v "
                                   "ORDER BY v.sort, v.name").fetchall()
        return [dict(r) for r in rows]

    def vfolder_create(self, name: str, parent: str = "") -> dict:
        import uuid
        i = uuid.uuid4().hex[:12]
        with self.lock:
            self.db.execute("INSERT INTO vfolders(id, parent, name, created) VALUES(?,?,?,?)",
                            (i, parent or "", name.strip() or "新しいフォルダ", time.time()))
            self.db.commit()
        return {"id": i, "parent": parent or "", "name": name}

    def vfolder_update(self, i: str, d: dict) -> dict:
        with self.lock:
            if "name" in d:
                self.db.execute("UPDATE vfolders SET name=? WHERE id=?", (str(d["name"]).strip(), i))
            if "parent" in d:
                newp = d["parent"] or ""
                # 自分の下に動かすと輪になるので禁止
                cur = newp
                while cur:
                    if cur == i:
                        raise ValueError("フォルダを自分の中には移動できません")
                    r = self.db.execute("SELECT parent FROM vfolders WHERE id=?", (cur,)).fetchone()
                    cur = r["parent"] if r else ""
                self.db.execute("UPDATE vfolders SET parent=? WHERE id=?", (newp, i))
            self.db.commit()
        return {"ok": True}

    def vfolder_delete(self, i: str):
        with self.lock:
            ids, todo = [], [i]
            while todo:
                x = todo.pop()
                ids.append(x)
                todo += [r["id"] for r in self.db.execute("SELECT id FROM vfolders WHERE parent=?", (x,)).fetchall()]
            for x in ids:
                self.db.execute("DELETE FROM vfolder_items WHERE folder_id=?", (x,))
                self.db.execute("DELETE FROM vfolders WHERE id=?", (x,))
            self.db.commit()
        return {"deleted": len(ids)}

    def vfolder_items(self, i: str, asset_ids: list, on: bool = True):
        with self.lock:
            for a in asset_ids:
                if on:
                    self.db.execute("INSERT OR IGNORE INTO vfolder_items(folder_id, asset_id, added) VALUES(?,?,?)",
                                    (i, a, time.time()))
                else:
                    self.db.execute("DELETE FROM vfolder_items WHERE folder_id=? AND asset_id=?", (i, a))
            self.db.commit()
        return {"ok": True}

    def search(self, q="", kind="", tag="", fav_user="", limit=200, offset=0, dir="", under="", vfolder="",
               order="added", rating_min=0, label="", since=0.0):
        sql = "SELECT a.* FROM assets a"
        args = []
        where = ["a.missing=0"]
        if vfolder:
            sql += " JOIN vfolder_items vi ON vi.asset_id=a.id AND vi.folder_id=?"
            args.append(vfolder)
        dir, under = dir.replace("\\", "/"), under.replace("\\", "/")
        if dir:
            where.append("a.dir=?")
            args.append(dir.rstrip("/"))
        if under:
            where.append("(a.dir=? OR a.dir LIKE ? ESCAPE '\\')")
            args += [under.rstrip("/"), _like(under.rstrip("/"))]
        if fav_user:
            sql += " JOIN favorites f ON f.asset_id=a.id AND f.user=?"
            args.append(fav_user)
        if q:
            for word in q.split():
                where.append("(a.name LIKE ? OR a.tags LIKE ? OR a.comment LIKE ? OR a.path LIKE ?)")
                args += [f"%{word}%"] * 4
        if kind:
            where.append("a.kind=?")
            args.append(kind)
        if int(rating_min or 0) > 0:
            where.append("a.rating>=?")
            args.append(int(rating_min))
        if label:
            where.append("a.label=?")
            args.append(label)
        if float(since or 0) > 0:
            where.append("a.added>=?")
            args.append(time.time() - float(since))
        if tag:
            where.append("a.tags LIKE ?")
            args.append(f'%"{tag}"%')
        orders = {"added": "a.added DESC", "name": "a.name COLLATE NOCASE", "mtime": "a.mtime DESC",
                  "size": "a.size DESC", "kind": "a.kind, a.name COLLATE NOCASE"}
        sql += " WHERE " + " AND ".join(where) + f" ORDER BY {orders.get(order, orders['added'])} LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d.get("tags") or "[]")
            out.append(d)
        return out

    def update(self, i: str, d: dict):
        with self.lock:
            for k in ("rating", "comment", "credit", "label"):
                if k in d:
                    self.db.execute(f"UPDATE assets SET {k}=? WHERE id=?", (d[k], i))
            if "tags" in d:
                self.db.execute("UPDATE assets SET tags=? WHERE id=?", (json.dumps(list(d["tags"]), ensure_ascii=False), i))
            self.db.commit()
        return self.get(i)

    def favorite(self, user: str, i: str, on: bool):
        with self.lock:
            if on:
                self.db.execute("INSERT OR IGNORE INTO favorites VALUES(?,?)", (user, i))
            else:
                self.db.execute("DELETE FROM favorites WHERE user=? AND asset_id=?", (user, i))
            self.db.commit()

    def favorites(self, user: str):
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT asset_id FROM favorites WHERE user=?", (user,))]

    def projects(self):
        out = []
        for root in self.roots:
            for p in Path(root).rglob("*.tcut"):
                lock = None
                lp = Path(str(p) + ".lock")
                if lp.exists():
                    try:
                        lock = json.loads(lp.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        lock = {"user": "?"}
                out.append({"path": str(p), "name": p.stem, "mtime": p.stat().st_mtime, "lock": lock})
        return out

    # ── レンダーファーム ─────────────────────
    def add_job(self, d: dict) -> str:
        jid = hashlib.sha1(f"{time.time()}{os.urandom(8)}".encode()).hexdigest()[:12]
        with self.lock:
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                            (jid, time.time(), d.get("codec", "libx264"), int(d.get("crf", 18)),
                             d["project"], "running", d.get("client", "")))
            for t in d["tasks"]:
                seg = self.data / "segments" / f"{t['key']}.bin"
                status = "done" if seg.exists() else "pending"
                self.db.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?,?,?,?)",
                                (jid, t["key"], int(t["a"]), int(t["b"]), status, "", 0, ""))
            self.db.commit()
        return jid

    def claim(self, node: str, job: str = "") -> Optional[dict]:
        with self.lock:
            # 長時間戻らない（ノード停止）のタスクは配り直す
            self.db.execute("UPDATE tasks SET status='pending' WHERE status='claimed' AND claimed<?", (time.time() - 600,))
            q = "SELECT t.*, j.codec, j.crf, j.project FROM tasks t JOIN jobs j ON j.id=t.job_id " \
                "WHERE t.status='pending' AND j.status='running'"
            args = []
            if job:
                q += " AND t.job_id=?"
                args.append(job)
            r = self.db.execute(q + " ORDER BY j.created, t.a LIMIT 1", args).fetchone()
            if r is None:
                return None
            self.db.execute("UPDATE tasks SET status='claimed', node=?, claimed=? WHERE job_id=? AND key=?",
                            (node, time.time(), r["job_id"], r["key"]))
            self.db.commit()
            return dict(r)

    def put_segment(self, key: str, data: bytes, node: str):
        p = self.data / "segments" / f"{key}.bin"
        tmp = p.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        with self.lock:
            self.db.execute("UPDATE tasks SET status='done', node=? WHERE key=?", (node, key))
            self.db.commit()

    def fail(self, job: str, key: str, error: str):
        with self.lock:
            self.db.execute("UPDATE tasks SET status='pending', error=? WHERE job_id=? AND key=?", (error[:500], job, key))
            self.db.commit()

    def job_status(self, jid: str) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT key, status, node, error FROM tasks WHERE job_id=?", (jid,)).fetchall()
        tasks = [dict(r) for r in rows]
        done = sum(1 for t in tasks if t["status"] == "done")
        if tasks and done == len(tasks):
            with self.lock:
                self.db.execute("UPDATE jobs SET status='done' WHERE id=?", (jid,))
                self.db.commit()
        return {"id": jid, "total": len(tasks), "done": done, "tasks": tasks}

    def heartbeat(self, name: str, info: dict):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO nodes VALUES(?,?,?)", (name, time.time(), json.dumps(info)))
            self.db.commit()

    def nodes(self):
        with self.lock:
            rows = self.db.execute("SELECT * FROM nodes ORDER BY name").fetchall()
        now = time.time()
        return [{"name": r["name"], "online": now - r["last_seen"] < 30, "last_seen": r["last_seen"],
                 "info": json.loads(r["info"] or "{}")} for r in rows]

    def jobs(self, limit=20):
        with self.lock:
            rows = self.db.execute("SELECT id, created, codec, status, client FROM jobs ORDER BY created DESC LIMIT ?",
                                   (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            st = self.job_status(r["id"])
            d.update(total=st["total"], done=st["done"])
            out.append(d)
        return out


# assetweb.html が見つからないとき（配布物が壊れている）だけ使う
WEB = """<!doctype html><meta charset="utf-8"><title>TeloCut アセットサーバー</title>
<body style="font-family:sans-serif;background:#1b1b1d;color:#ddd;padding:24px">
<h2>TeloCut アセットサーバー</h2><p>画面のファイル（editor/assetweb.html）が見つかりません。サーバーを入れ直してください。</p>
<p>API は動いています: <a style="color:#a99cff" href="/api/info">/api/info</a></p></body>"""


class Handler(BaseHTTPRequestHandler):
    store: Store = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, data: bytes, ctype="application/octet-stream", code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    MAX_JSON = 8 * 1024 * 1024

    def _length(self) -> int:
        try:
            return max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            return 0

    def _body(self) -> bytes:
        n = self._length()
        if n > self.MAX_JSON:
            raise ValueError("データが大きすぎます")
        return self.rfile.read(n) if n else b""

    def _same_site(self) -> bool:
        """ほかの Web サイトから勝手に送られた要求（CSRF）を断る。
        JSON（Content-Type: application/json）か X-TeloCut ヘッダーが付いた要求だけ受け付ける
        （どちらもブラウザはほかのサイトからは事前確認なしに送れない）。"""
        ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return ct == "application/json" or self.headers.get("X-TeloCut") == "1"

    def _save_upload(self, dst: Path):
        """アップロードを少しずつファイルに書く（大きな動画でもメモリを使わない）。"""
        n = self._length()
        tmp = dst.with_name(dst.name + ".part")
        try:
            with open(tmp, "wb") as f:
                left = n
                while left > 0:
                    buf = self.rfile.read(min(1 << 20, left))
                    if not buf:
                        raise ConnectionError("アップロードが途中で切れました")
                    f.write(buf)
                    left -= len(buf)
            os.replace(tmp, dst)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _send_file(self, path: str, download: bool = False):
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else 0
            end = int(b) if b else size - 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if download:
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(os.path.basename(path)))
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                buf = f.read(min(1 << 20, left))
                if not buf:
                    break
                self.wfile.write(buf)
                left -= len(buf)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        parts = [p for p in u.path.split("/") if p]
        st = self.store
        try:
            if not parts:
                web = Path(__file__).with_name("assetweb.html")
                html = web.read_bytes() if web.exists() else WEB.encode("utf-8")
                return self._bytes(html, "text/html; charset=utf-8", extra={"Cache-Control": "no-cache"})
            if parts[:2] == ["api", "info"]:
                return self._json({"app": "TeloCut Asset Server", "version": VERSION, "roots": st.roots,
                                   "scanning": st.scanning})
            if parts[:2] == ["api", "assets"] and len(parts) == 2:
                return self._json(st.search(qs.get("q", ""), qs.get("kind", ""), qs.get("tag", ""),
                                            qs.get("fav_user", ""), max(1, min(5000, int(qs.get("limit", 200)))),
                                            max(0, int(qs.get("offset", 0))),
                                            qs.get("dir", ""), qs.get("under", ""), qs.get("vfolder", ""),
                                            qs.get("order", "added"), int(qs.get("rating_min", 0) or 0),
                                            qs.get("label", ""), float(qs.get("since", 0) or 0)))
            if parts[:2] == ["api", "browse"]:
                return self._json(st.browse(qs.get("path", ""), qs.get("all", "") == "1"))
            if parts[:2] == ["api", "duplicates"]:
                return self._json(st.duplicates())
            if parts[:2] == ["api", "stats"]:
                return self._json(st.stats())
            if parts[:2] == ["api", "roots"]:
                return self._json(st.roots_info())
            if parts[:2] == ["api", "shots"]:
                return self._json(st.shots(qs.get("project", "")))
            if parts[:2] == ["api", "activity"]:
                return self._json(st.activity(int(qs.get("limit", 100)), qs.get("project", ""), int(qs.get("since", 0) or 0)))
            if parts[:2] == ["api", "dashboard"]:
                return self._json(st.dashboard(qs.get("project", "")))
            if parts[:2] == ["api", "folders"]:
                return self._json(st.folders(qs.get("parent", "")))
            if parts[:2] == ["api", "vfolders"]:
                return self._json(st.vfolders())
            if parts[:2] == ["api", "assets"] and len(parts) >= 3:
                a = st.get(parts[2])
                if a is None:
                    return self._json({"error": "not found"}, 404)
                if len(parts) == 3:
                    return self._json(a)
                if parts[3] in ("thumb", "preview"):
                    tt = float(qs["t"]) if "t" in qs else None
                    size = int(qs.get("size", 320 if parts[3] == "thumb" else 960))
                    t = st.thumb(parts[2], tt, size)
                    return self._bytes(t, "image/jpeg") if t else self._json({"error": "no thumb"}, 404)
                if parts[3] == "file":
                    return self._send_file(a["path"], download=qs.get("dl") == "1")
                if parts[3] == "proxy":
                    return self._json(st.proxy(parts[2], qs.get("start", "1") == "1"))
                if parts[3] == "proxyfile":
                    pp = st.proxy_path(parts[2], a["kind"] == "audio")
                    return self._send_file(str(pp)) if pp.exists() else self._json({"error": "not ready"}, 404)
                if parts[3] == "notes":
                    return self._json(st.notes(parts[2]))
                if parts[3] == "play":
                    # ブラウザでそのまま再生できるか（できなければプロキシを使う）
                    m = st.meta(parts[2])
                    return self._json({"direct": st.playable(a, m), "meta": m, "proxy": st.proxy(parts[2], False)})
                if parts[3] == "meta":
                    return self._json(st.meta(parts[2]))
                if parts[3] == "wave":
                    return self._json(st.waveform(parts[2], int(qs.get("bins", 600))))
            if parts[:2] == ["api", "favorites"]:
                return self._json(st.favorites(qs.get("user", "")))
            if parts[:2] == ["api", "projects"]:
                return self._json(st.projects())
            if parts[:2] == ["api", "render"]:
                if parts[2] == "jobs" and len(parts) == 4:
                    return self._json(st.job_status(parts[3]))
                if parts[2] == "jobs":
                    return self._json(st.jobs())
                if parts[2] == "nodes":
                    return self._json(st.nodes())
                if parts[2] == "segment":
                    p = st.data / "segments" / f"{parts[3]}.bin"
                    return self._send_file(str(p)) if p.exists() else self._json({"error": "not ready"}, 404)
            self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as ex:
            self._json({"error": str(ex)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        parts = [p for p in u.path.split("/") if p]
        st = self.store
        try:
            if not self._same_site():
                n = self._length()
                if n <= self.MAX_JSON:
                    self.rfile.read(n)                  # 読み捨ててから答える（読まずに切ると相手に届かない）
                self.close_connection = True
                return self._json({"error": "この要求は受け付けません（ほかのサイトからの送信）"}, 403)
            if parts[:2] == ["api", "housekeeping"]:
                return self._json(st.housekeeping(float(qs.get("proxy_gb", 20))))
            if parts[:2] == ["api", "scan"]:
                if qs.get("path"):
                    return self._json(st.rescan_dir(qs["path"], qs.get("recursive", "") == "1"))
                threading.Thread(target=st.scan, daemon=True).start()
                return self._json({"ok": True})
            if parts[:2] == ["api", "upload"]:
                import re as _re
                name = _re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", qs.get("name", "upload.bin")).strip(" .") or "upload.bin"
                # フォルダは「英数字・日本語などの名前」を / でつないだものだけ（.. や C: やルートは使えない）
                parts_ = [_re.sub(r'[\\:*?"<>|\x00-\x1f]', "_", x).strip(" .") for x in
                          qs.get("folder", "").replace("\\", "/").split("/")]
                parts_ = [x for x in parts_ if x and x not in (".", "..")][:8]
                up = (st.data / "uploads").resolve()
                d = up.joinpath(*parts_) if parts_ else up
                if up != d.resolve() and up not in d.resolve().parents:
                    raise ValueError("アップロード先が正しくありません")
                d.mkdir(parents=True, exist_ok=True)
                p = d / name
                k = 1
                while p.exists():
                    p = d / f"{Path(name).stem}_{k}{Path(name).suffix}"
                    k += 1
                self._save_upload(p)
                return self._json(st.index(str(p)))
            body = json.loads(self._body() or b"{}")
            if parts[:2] == ["api", "favorites"]:
                st.favorite(body["user"], body["asset_id"], bool(body.get("on", True)))
                return self._json({"ok": True})
            if parts[:2] == ["api", "roots"]:
                return self._json(st.set_roots_info(body))
            if parts[:2] == ["api", "shots"] and len(parts) == 2:
                if isinstance(body.get("items"), list):          # まとめて追加（マーカーから など）
                    return self._json([st.shot_create(x, body.get("user", "")) for x in body["items"][:500]])
                return self._json(st.shot_create(body, body.get("user", "")))
            if parts[:2] == ["api", "activity"]:
                return self._json(st.add_activity(body.get("user", ""), body.get("host", ""), body.get("project", ""),
                                                  body.get("kind", ""), body.get("target", ""), body.get("text", "")))
            if parts[:3] == ["api", "assets", "bulk"]:
                return self._json(st.bulk_update(body.get("ids", []), body.get("set", {})))
            if parts[:2] == ["api", "assets"] and len(parts) == 4 and parts[3] == "notes":
                return self._json(st.add_note(parts[2], body.get("t", 0), body.get("user", ""), body.get("text", "")))
            if parts[:2] == ["api", "vfolders"] and len(parts) == 2:
                return self._json(st.vfolder_create(body.get("name", ""), body.get("parent", "")))
            if parts[:2] == ["api", "vfolders"] and len(parts) == 4 and parts[3] == "items":
                return self._json(st.vfolder_items(parts[2], body.get("asset_ids", []), bool(body.get("on", True))))
            if parts[:2] == ["api", "render"]:
                if parts[2] == "jobs":
                    return self._json({"id": st.add_job(body)})
                if parts[2] == "claim":
                    return self._json(st.claim(body.get("node", "?"), body.get("job", "")))
                if parts[2] == "fail":
                    st.fail(body["job"], body["key"], body.get("error", ""))
                    return self._json({"ok": True})
                if parts[2] == "heartbeat":
                    st.heartbeat(body.get("node", "?"), body.get("info", {}))
                    return self._json({"ok": True})
            self._json({"error": "not found"}, 404)
        except Exception as ex:
            self._json({"error": str(ex)}, 500)

    def do_PATCH(self):
        parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        try:
            if parts[:2] == ["api", "assets"] and len(parts) == 3:
                return self._json(self.store.update(parts[2], json.loads(self._body() or b"{}")))
            if parts[:2] == ["api", "vfolders"] and len(parts) == 3:
                return self._json(self.store.vfolder_update(parts[2], json.loads(self._body() or b"{}")))
            if parts[:2] == ["api", "shots"] and len(parts) == 3:
                b = json.loads(self._body() or b"{}")
                return self._json(self.store.shot_update(parts[2], b, b.get("user", "")))
            self._json({"error": "not found"}, 404)
        except Exception as ex:
            self._json({"error": str(ex)}, 500)

    def do_DELETE(self):
        parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        self._body()                                   # 本文が付いていても読み捨てる（接続を使い回すため）
        try:
            if parts[:2] == ["api", "vfolders"] and len(parts) == 3:
                return self._json(self.store.vfolder_delete(parts[2]))
            if parts[:2] == ["api", "notes"] and len(parts) == 3:
                return self._json(self.store.delete_note(parts[2]))
            if parts[:2] == ["api", "shots"] and len(parts) == 3:
                qs = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
                return self._json(self.store.shot_delete(parts[2], qs.get("user", "")))
            self._json({"error": "not found"}, 404)
        except Exception as ex:
            self._json({"error": str(ex)}, 500)

    def do_PUT(self):
        u = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        parts = [p for p in u.path.split("/") if p]
        try:
            if parts[:3] == ["api", "render", "segment"] and len(parts) == 4:
                self.store.put_segment(parts[3], self._body(), qs.get("node", "?"))
                return self._json({"ok": True})
            self._json({"error": "not found"}, 404)
        except Exception as ex:
            self._json({"error": str(ex)}, 500)


def serve(roots, data_dir: str, port: int = 8770, rescan_min: int = 10, block: bool = True, proxy_gb: float = 20.0):
    store = Store(data_dir, roots)
    Handler.store = store
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.request_queue_size = 128      # 混んでいても接続を受け付ける
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)

    def rescan():
        last_clean = 0.0
        while True:
            store.scan()
            if time.time() - last_clean > 6 * 3600:          # 6 時間ごとに片付け
                try:
                    store.housekeeping(proxy_gb)
                except Exception:
                    pass
                last_clean = time.time()
            time.sleep(max(60, rescan_min * 60))
    threading.Thread(target=rescan, daemon=True).start()
    print(f"TeloCut アセットサーバー: http://localhost:{port}/  （素材フォルダ: {', '.join(store.roots)}）")
    if block:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        return None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, store


def main(argv=None):
    ap = argparse.ArgumentParser(description="TeloCut アセットサーバー")
    ap.add_argument("--root", action="append", default=[], help="素材フォルダ（複数指定可）")
    ap.add_argument("--data", default=str(Path.home() / "TeloCutServer"), help="データ（DB・サムネイル・アップロード）の保存先")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--rescan", type=int, default=10, help="再スキャン間隔（分）")
    ap.add_argument("--proxy-gb", type=float, default=20.0, help="ブラウザ用プロキシの容量の上限（GB）")
    a = ap.parse_args(argv)
    serve(a.root, a.data, a.port, a.rescan, proxy_gb=a.proxy_gb)


if __name__ == "__main__":
    main()
