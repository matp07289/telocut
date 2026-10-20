"""ジョブで複数 PC に振り分ける（レンダリング・トランスコード）。

サーバー（アセットサーバー）がジョブを持ち、レンダーノード（各 PC の TeloCut）が 1 つずつ取りに来る。
- 書き出しジョブ: シーケンスを区間に分けて、各ノードが映像だけを書き出す → 最後に 1 台が つなぐ + 音声を付ける
  （区間が 1 つなら、そのまま音声付きで書き出す）。結果はサーバーの「アップロード（サーバー）/ 書き出し」に入る
- トランスコードジョブ: 1 本ずつ。サーバーの素材（ブラウザから）でも、各 PC のファイル（TeloCut から）でも
- 止まったノードの仕事は 3 分で他のノードに配り直す。3 回失敗したらジョブを「失敗」にする
- 優先度・中止・やり直し・ノードの一時停止（受け付ける仕事の種類も選べる）

サーバー側は FarmMixin（Store に混ぜる）と handle()（HTTP の振り分け）、ノード側は run_task()。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Dict, List, Optional

KINDS = ("render", "merge", "full", "transcode")
STALE = 180            # この秒数、進み具合の連絡が無いノードの仕事は配り直す
MAX_TRIES = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS farm_jobs(id TEXT PRIMARY KEY, kind TEXT, name TEXT, status TEXT, priority INT DEFAULT 0,
    created REAL, user TEXT DEFAULT '', client TEXT DEFAULT '', params TEXT DEFAULT '{}', project TEXT DEFAULT '',
    result_asset TEXT DEFAULT '', result_path TEXT DEFAULT '', error TEXT DEFAULT '', started REAL DEFAULT 0,
    finished REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS farm_tasks(job_id TEXT, idx INT, kind TEXT, a INT DEFAULT 0, b INT DEFAULT 0,
    status TEXT DEFAULT 'pending', node TEXT DEFAULT '', claimed REAL DEFAULT 0, updated REAL DEFAULT 0,
    progress REAL DEFAULT 0, error TEXT DEFAULT '', tries INT DEFAULT 0, result TEXT DEFAULT '',
    PRIMARY KEY(job_id, idx));
CREATE TABLE IF NOT EXISTS farm_nodes(name TEXT PRIMARY KEY, disabled INT DEFAULT 0, kinds TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS farm_auto(path TEXT PRIMARY KEY, job_id TEXT, added REAL);
"""


def _safe_sub(s: str) -> str:
    """保存先の中のフォルダ名（1 段・記号を除く）。"""
    s = "".join(ch for ch in str(s or "") if ch not in '\\/:*?"<>|\x00').replace("..", "").strip(" .")
    return s[:60]


def _new_id() -> str:
    return hashlib.sha1(f"{time.time()}{os.urandom(8)}".encode()).hexdigest()[:12]


class FarmMixin:
    """Store に混ぜて使う（self.db / self.lock / self.data / self.get / self.index / self.add_activity がある前提）。"""

    def _farm_init(self, local_worker: bool = True):
        with self.lock:
            self.db.executescript(SCHEMA)
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(farm_jobs)").fetchall()]
            if "skip" not in cols:                      # 素材が見えなかったノード（そのジョブは配らない）
                self.db.execute("ALTER TABLE farm_jobs ADD COLUMN skip TEXT DEFAULT ''")
            self.db.commit()
        (self.data / "farm").mkdir(exist_ok=True)
        self._farm_wake = threading.Event()
        if local_worker:
            threading.Thread(target=self._farm_local_loop, daemon=True).start()
            threading.Thread(target=self._farm_auto_loop, daemon=True).start()

    # ── 受付 ──
    def farm_add_export(self, d: dict) -> dict:
        from .mediacodecs import BY_ID
        pid = d.get("preset", "h264")
        if pid not in BY_ID or not BY_ID[pid].vcodec:
            raise ValueError("形式が正しくありません")
        a, b = int(d.get("a", 0)), int(d.get("b", 0))
        if b <= a:
            raise ValueError("書き出す範囲が空です")
        if not d.get("project"):
            raise ValueError("プロジェクトがありません")
        n = max(1, min(64, int(d.get("chunks", 1) or 1), (b - a) // 15 or 1))
        jid = _new_id()
        params = {"a": a, "b": b, "preset": pid, "alpha": bool(d.get("alpha")), "quality": int(d.get("quality", 18)),
                  "chunks": n, "ext": BY_ID[pid].ext, "fps": float(d.get("fps") or 0), "dest": d.get("dest", ""),
                  "sub": _safe_sub(d.get("sub", ""))}
        with self.lock:
            self.db.execute("INSERT INTO farm_jobs(id,kind,name,status,priority,created,user,client,params,project) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (jid, "export", str(d.get("name") or "書き出し")[:200], "waiting", int(d.get("priority", 0)),
                             time.time(), str(d.get("user", ""))[:40], str(d.get("client", ""))[:80],
                             json.dumps(params), d["project"]))
            if n == 1:
                self.db.execute("INSERT INTO farm_tasks(job_id,idx,kind,a,b) VALUES(?,?,?,?,?)", (jid, 0, "full", a, b))
            else:
                cuts = [a + (b - a) * k // n for k in range(n + 1)]
                for k in range(n):
                    self.db.execute("INSERT INTO farm_tasks(job_id,idx,kind,a,b) VALUES(?,?,?,?,?)",
                                    (jid, k, "render", cuts[k], cuts[k + 1]))
                self.db.execute("INSERT INTO farm_tasks(job_id,idx,kind,a,b) VALUES(?,?,?,?,?)", (jid, n, "merge", a, b))
            self.db.commit()
        self.add_activity(d.get("user", ""), "", "", "farm", jid, f"書き出しジョブ「{d.get('name') or ''}」（{n} 分割・{BY_ID[pid].label}）")
        self._farm_wake.set()
        return self.farm_job(jid)

    def farm_add_transcode(self, items: List[dict], preset: str, size: str = "", user: str = "", priority: int = 0,
                           dest: str = "", sub: str = "") -> list:
        """items: [{"asset_id": サーバーの素材} または {"src_path": 各 PC のパス, "name": 名前, "dst_dir": 保存先}]"""
        from .mediacodecs import BY_ID
        if preset not in BY_ID:
            raise ValueError("形式が正しくありません")
        out = []
        for it in list(items or [])[:500]:
            name = it.get("name") or ""
            if it.get("asset_id"):
                a = self.get(it["asset_id"])
                if not a or a["kind"] not in ("video", "audio"):
                    continue
                name = a["name"]
            elif not it.get("src_path"):
                continue
            jid = _new_id()
            params = {"preset": preset, "size": size or "", "asset_id": it.get("asset_id", ""),
                      "src_path": it.get("src_path", ""), "dst_dir": it.get("dst_dir", ""),
                      "ext": BY_ID[preset].ext, "dest": dest or "", "sub": _safe_sub(sub)}
            with self.lock:
                self.db.execute("INSERT INTO farm_jobs(id,kind,name,status,priority,created,user,params) VALUES(?,?,?,?,?,?,?,?)",
                                (jid, "transcode", (name or Path(it.get("src_path", "")).name)[:200], "waiting",
                                 int(priority), time.time(), (user or "")[:40], json.dumps(params)))
                self.db.execute("INSERT INTO farm_tasks(job_id,idx,kind) VALUES(?,?,?)", (jid, 0, "transcode"))
                self.db.commit()
            out.append(self.farm_job(jid))
        if out:
            self.add_activity(user, "", "", "transcode", preset, f"{len(out)} 件の変換を受け付けました（{BY_ID[preset].label}）")
            self._farm_wake.set()
        return out

    # ── 一覧 ──
    def farm_job(self, jid: str, with_tasks: bool = True) -> Optional[dict]:
        with self.lock:
            r = self.db.execute("SELECT * FROM farm_jobs WHERE id=?", (jid,)).fetchone()
            if r is None:
                return None
            j = dict(r)
            tasks = [dict(t) for t in self.db.execute("SELECT * FROM farm_tasks WHERE job_id=? ORDER BY idx", (jid,))]
        j.pop("project", None)
        j["params"] = json.loads(j.get("params") or "{}")
        work = [t for t in tasks if t["kind"] != "merge"] or tasks
        weight = {t["idx"]: (t["b"] - t["a"]) if t["kind"] == "render" else 1 for t in tasks}
        tot = sum(weight[t["idx"]] for t in work) or 1
        prog = sum(weight[t["idx"]] * (1.0 if t["status"] == "done" else t["progress"]) for t in work) / tot
        merge = next((t for t in tasks if t["kind"] == "merge"), None)
        if merge is not None:
            prog = prog * 0.9 + 0.1 * (1.0 if merge["status"] == "done" else merge["progress"])
        j["progress"] = round(prog, 4)
        j["nodes"] = sorted({t["node"] for t in tasks if t["node"]})
        j["done"] = sum(1 for t in tasks if t["status"] == "done")
        j["total"] = len(tasks)
        if j["status"] == "running" and j["started"] and 0.02 < prog < 1:
            el = time.time() - j["started"]
            j["eta"] = round(el / prog - el)
        if with_tasks:
            j["tasks"] = tasks
        return j

    def farm_jobs(self, limit: int = 100, kind: str = "") -> list:
        with self.lock:
            ids = [r[0] for r in self.db.execute(
                "SELECT id FROM farm_jobs" + (" WHERE kind=?" if kind else "") + " ORDER BY created DESC LIMIT ?",
                ((kind,) if kind else ()) + (max(1, min(500, int(limit))),))]
        return [self.farm_job(i) for i in ids]

    def farm_project(self, jid: str) -> str:
        with self.lock:
            r = self.db.execute("SELECT project FROM farm_jobs WHERE id=?", (jid,)).fetchone()
        return r[0] if r else ""

    # ── ノード ──
    def farm_node_set(self, name: str, d: dict) -> dict:
        with self.lock:
            cur = self.db.execute("SELECT * FROM farm_nodes WHERE name=?", (name,)).fetchone()
            dis = int(d.get("disabled", cur["disabled"] if cur else 0))
            kinds = ",".join(k for k in (d.get("kinds") if d.get("kinds") is not None else
                                         (cur["kinds"].split(",") if cur and cur["kinds"] else [])) if k in KINDS)
            self.db.execute("INSERT OR REPLACE INTO farm_nodes VALUES(?,?,?)", (name, dis, kinds))
            self.db.commit()
        return {"name": name, "disabled": bool(dis), "kinds": kinds.split(",") if kinds else []}

    def farm_nodes_info(self) -> Dict[str, dict]:
        with self.lock:
            flags = {r["name"]: {"disabled": bool(r["disabled"]), "kinds": r["kinds"].split(",") if r["kinds"] else []}
                     for r in self.db.execute("SELECT * FROM farm_nodes")}
            busy = {}
            for r in self.db.execute("SELECT t.node, t.kind, t.progress, j.name FROM farm_tasks t JOIN farm_jobs j "
                                     "ON j.id=t.job_id WHERE t.status='claimed'"):
                busy[r["node"]] = {"task": r["kind"], "job": r["name"], "progress": r["progress"]}
        return {"flags": flags, "busy": busy}

    # ── 配る ──
    def farm_claim(self, node: str, kinds=None, local: bool = False) -> Optional[dict]:
        kinds = [k for k in (kinds or KINDS) if k in KINDS]
        with self.lock:
            now = time.time()
            fl = self.db.execute("SELECT * FROM farm_nodes WHERE name=?", (node,)).fetchone()
            if fl is not None:
                if fl["disabled"]:
                    return None
                if fl["kinds"]:
                    kinds = [k for k in kinds if k in fl["kinds"].split(",")]
            # 止まったノードの仕事を配り直す
            self.db.execute("UPDATE farm_tasks SET status='pending', node='', tries=tries+1, error='応答が無くなったので配り直し' "
                            "WHERE status='claimed' AND updated<?", (now - STALE,))
            for r in self.db.execute("SELECT job_id, idx FROM farm_tasks WHERE status='pending' AND tries>=?", (MAX_TRIES,)).fetchall():
                self._farm_fail_job(r["job_id"], "何度やっても失敗しました")
            if not kinds:
                return None
            q = ("SELECT t.*, j.kind AS job_kind, j.params, j.name AS job_name FROM farm_tasks t JOIN farm_jobs j ON j.id=t.job_id "
                 "WHERE t.status='pending' AND j.status IN ('waiting','running') AND t.kind IN (%s) "
                 "AND (t.kind<>'merge' OR NOT EXISTS (SELECT 1 FROM farm_tasks x WHERE x.job_id=t.job_id AND x.kind='render' "
                 "AND x.status<>'done')) ORDER BY j.priority DESC, j.created, t.idx" % ",".join("?" * len(kinds)))
            skips = {r["id"]: set(filter(None, (r["skip"] or "").split("\n")))
                     for r in self.db.execute("SELECT id, skip FROM farm_jobs WHERE status IN ('waiting','running')")}
            for r in self.db.execute(q, kinds).fetchall():
                if node in skips.get(r["job_id"], ()):
                    continue                           # このノードからは素材が見えなかった
                p = json.loads(r["params"] or "{}")
                if local and r["kind"] == "transcode" and not p.get("asset_id"):
                    continue                       # サーバーからは各 PC のファイルは見えない
                if local and r["kind"] != "transcode":
                    continue
                self.db.execute("UPDATE farm_tasks SET status='claimed', node=?, claimed=?, updated=?, progress=0, error='' "
                                "WHERE job_id=? AND idx=?", (node, now, now, r["job_id"], r["idx"]))
                self.db.execute("UPDATE farm_jobs SET status='running', started=CASE WHEN started=0 THEN ? ELSE started END "
                                "WHERE id=?", (now, r["job_id"]))
                self.db.commit()
                t = dict(r)
                t["params"] = p
                if p.get("asset_id"):
                    a = self.get(p["asset_id"])
                    t["asset"] = {"id": a["id"], "path": a["path"], "name": a["name"]} if a else None
                if r["kind"] == "merge":
                    t["chunks"] = [x["idx"] for x in self.db.execute(
                        "SELECT idx FROM farm_tasks WHERE job_id=? AND kind='render' ORDER BY idx", (r["job_id"],))]
                return t
            self.db.commit()
        return None

    def farm_progress(self, jid: str, idx: int, node: str, progress: float) -> dict:
        with self.lock:
            j = self.db.execute("SELECT status FROM farm_jobs WHERE id=?", (jid,)).fetchone()
            t = self.db.execute("SELECT status, node FROM farm_tasks WHERE job_id=? AND idx=?", (jid, idx)).fetchone()
            cancel = j is None or j["status"] in ("cancelled", "error") or t is None or t["status"] != "claimed" \
                or t["node"] != node
            if not cancel:
                self.db.execute("UPDATE farm_tasks SET progress=?, updated=? WHERE job_id=? AND idx=?",
                                (max(0.0, min(1.0, float(progress))), time.time(), jid, idx))
                self.db.commit()
        return {"cancel": cancel}

    def farm_fail(self, jid: str, idx: int, node: str, error: str, skip: bool = False) -> dict:
        """失敗。skip=True は「このノードからは素材が見えない」→ 回数に数えず、ほかのノードに回す。"""
        if skip:
            with self.lock:
                r = self.db.execute("SELECT skip FROM farm_jobs WHERE id=?", (jid,)).fetchone()
                sk = set(filter(None, ((r["skip"] if r else "") or "").split("\n"))) | {node}
                self.db.execute("UPDATE farm_jobs SET skip=?, error=? WHERE id=?",
                                ("\n".join(sorted(sk)), f"{', '.join(sorted(sk))} からは素材が見えません（パス置換ルールを確認）"[:500], jid))
                self.db.execute("UPDATE farm_tasks SET status='pending', node='', error=? WHERE job_id=? AND idx=? "
                                "AND status='claimed'", (f"{node}: {error}"[:500], jid, idx))
                self.db.commit()
            self._farm_wake.set()
            return {"ok": True}
        with self.lock:
            self.db.execute("UPDATE farm_tasks SET status='pending', node='', tries=tries+1, error=? WHERE job_id=? AND idx=? "
                            "AND status='claimed'", (f"{node}: {error}"[:500], jid, idx))
            r = self.db.execute("SELECT tries FROM farm_tasks WHERE job_id=? AND idx=?", (jid, idx)).fetchone()
            if r and r["tries"] >= MAX_TRIES:
                self._farm_fail_job(jid, error)
            self.db.commit()
        self._farm_wake.set()
        return {"ok": True}

    def _farm_fail_job(self, jid: str, error: str):
        self.db.execute("UPDATE farm_jobs SET status='error', error=?, finished=? WHERE id=? AND status IN ('waiting','running')",
                        (str(error)[:500], time.time(), jid))
        self.db.execute("UPDATE farm_tasks SET status='error' WHERE job_id=? AND status IN ('pending','claimed')", (jid,))

    def farm_result_path(self, jid: str, idx: int, ext: str) -> Path:
        d = self.data / "farm" / jid
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{idx}{ext}"

    def farm_done(self, jid: str, idx: int, node: str, file: Optional[Path] = None, result_path: str = "") -> dict:
        """1 つの仕事が終わった。最後の仕事（つなぐ / 全体 / 変換）なら結果を「アップロード（サーバー）」に入れる。"""
        with self.lock:
            t = self.db.execute("SELECT * FROM farm_tasks WHERE job_id=? AND idx=?", (jid, idx)).fetchone()
            j = self.db.execute("SELECT * FROM farm_jobs WHERE id=?", (jid,)).fetchone()
            if t is None or j is None:
                raise ValueError("ジョブがありません")
            if j["status"] in ("cancelled", "error"):
                return {"ok": False, "cancelled": True}
            self.db.execute("UPDATE farm_tasks SET status='done', progress=1, updated=?, node=?, result=? WHERE job_id=? AND idx=?",
                            (time.time(), node, result_path or (str(file) if file else ""), jid, idx))
            self.db.commit()
        if t["kind"] in ("merge", "full", "transcode"):
            asset = ""
            if file is not None:
                params = json.loads(j["params"] or "{}")
                sub = "書き出し" if j["kind"] == "export" else "変換"
                if params.get("sub"):
                    sub = f"{sub}/{params['sub']}"
                try:
                    d = self.result_dir(params.get("dest", ""), sub)
                except OSError:                                # NAS に書けない → アップロードに
                    d = self.result_dir("uploads", sub)
                stem = "".join(ch for ch in j["name"] if ch not in '\\/:*?"<>|').strip() or jid
                if j["kind"] == "transcode":
                    from .mediacodecs import default_name
                    stem = Path(default_name(j["name"], params["preset"], params.get("size", ""))).stem
                dst = d / f"{stem}{params.get('ext', file.suffix)}"
                k = 1
                while dst.exists():
                    dst = d / f"{stem}_{k}{params.get('ext', file.suffix)}"
                    k += 1
                shutil.move(str(file), str(dst))
                a = self.index(str(dst))
                asset = a["id"] if a else ""
                result_path = result_path or str(dst)
            with self.lock:
                self.db.execute("UPDATE farm_jobs SET status='done', result_asset=?, result_path=?, finished=? WHERE id=?",
                                (asset, result_path, time.time(), jid))
                self.db.commit()
            shutil.rmtree(self.data / "farm" / jid, ignore_errors=True)
            self.add_activity(j["user"], node, "", "farm", jid, f"「{j['name']}」が完了（{node}）")
        self._farm_wake.set()
        return {"ok": True}

    def farm_cancel(self, jid: str) -> dict:
        with self.lock:
            self.db.execute("UPDATE farm_jobs SET status='cancelled', finished=? WHERE id=? AND status IN ('waiting','running')",
                            (time.time(), jid))
            self.db.execute("UPDATE farm_tasks SET status='cancelled' WHERE job_id=? AND status IN ('pending','claimed')", (jid,))
            self.db.commit()
        shutil.rmtree(self.data / "farm" / jid, ignore_errors=True)
        return {"ok": True}

    def farm_retry(self, jid: str) -> dict:
        with self.lock:
            self.db.execute("UPDATE farm_jobs SET status='waiting', error='', finished=0 WHERE id=? AND status IN ('error','cancelled')",
                            (jid,))
            self.db.execute("UPDATE farm_tasks SET status='pending', tries=0, node='', progress=0, error='' "
                            "WHERE job_id=? AND status<>'done'", (jid,))
            self.db.execute("UPDATE farm_jobs SET skip='' WHERE id=?", (jid,))
            self.db.commit()
        self._farm_wake.set()
        return self.farm_job(jid)

    def farm_update(self, jid: str, d: dict) -> dict:
        if "priority" in d:
            with self.lock:
                self.db.execute("UPDATE farm_jobs SET priority=? WHERE id=?", (int(d["priority"]), jid))
                self.db.commit()
        return self.farm_job(jid)

    # ── サーバー自身も変換を手伝う（サーバーの素材だけ）──
    def _farm_local_loop(self):
        from .mediacodecs import default_name, transcode
        time.sleep(1)
        while True:
            t = None
            try:
                t = self.farm_claim("サーバー", ["transcode"], local=True)
            except Exception:
                pass
            if t is None:
                self._farm_wake.wait(5)
                self._farm_wake.clear()
                continue
            p = t["params"]
            cancel = threading.Event()
            last = [0.0]

            def prog(x, t=t):
                if time.time() - last[0] > 1 or x >= 1:
                    last[0] = time.time()
                    if self.farm_progress(t["job_id"], t["idx"], "サーバー", x)["cancel"]:
                        cancel.set()
            try:
                try:
                    os.nice(10)
                except (AttributeError, OSError):
                    pass
                a = t.get("asset")
                if not a or not os.path.exists(a["path"]):
                    raise FileNotFoundError("元の素材が見つかりません")
                out = self.farm_result_path(t["job_id"], 0, p["ext"])
                transcode(a["path"], str(out), p["preset"], p.get("size", ""), prog, cancel)
                self.farm_done(t["job_id"], 0, "サーバー", out)
            except InterruptedError:
                pass
            except Exception as ex:
                self.farm_fail(t["job_id"], 0, "サーバー", str(ex))

    # ── 自動変換フォルダ: アップロード（サーバー）/ 自動変換 / <形式の id> / に置いたら変換 ──
    def _farm_auto_loop(self):
        from .mediacodecs import BY_ID
        from .assetserver import KIND
        base = self.data / "uploads" / "自動変換"
        try:
            base.mkdir(parents=True, exist_ok=True)
            for pid in ("dnxhr_hq", "prores_hq", "h264"):
                (base / pid).mkdir(exist_ok=True)
        except OSError:
            pass
        sizes: Dict[str, int] = {}
        while True:
            time.sleep(10)
            try:
                for d in base.iterdir():
                    if not d.is_dir() or d.name not in BY_ID:
                        continue
                    for f in d.iterdir():
                        if not f.is_file() or KIND.get(f.suffix.lower()) not in ("video", "audio") or ".part" in f.name:
                            continue
                        key = str(f)
                        with self.lock:
                            if self.db.execute("SELECT 1 FROM farm_auto WHERE path=?", (key,)).fetchone():
                                continue
                        sz = f.stat().st_size
                        if sizes.get(key) != sz:                 # 書き込みが終わるまで待つ
                            sizes[key] = sz
                            continue
                        a = self.index(key)
                        if not a:
                            continue
                        jobs = self.farm_add_transcode([{"asset_id": a["id"]}], d.name, "", "自動変換")
                        with self.lock:
                            self.db.execute("INSERT OR REPLACE INTO farm_auto VALUES(?,?,?)",
                                            (key, jobs[0]["id"] if jobs else "", time.time()))
                            self.db.commit()
            except Exception:
                pass


# ─────────────────────────── HTTP（assetserver の Handler から呼ぶ）───────────────────────────
def handle(h, method: str, parts: List[str], qs: dict) -> bool:
    """/api/farm/... を処理したら True。"""
    if parts[:2] != ["api", "farm"]:
        return False
    st = h.store
    rest = parts[2:]
    if method == "GET":
        if rest == ["jobs"]:
            h._json(st.farm_jobs(int(qs.get("limit", 100)), qs.get("kind", "")))
        elif len(rest) == 2 and rest[0] == "jobs":
            j = st.farm_job(rest[1])
            h._json(j if j else {"error": "not found"}, 200 if j else 404)
        elif len(rest) == 3 and rest[0] == "jobs" and rest[2] == "project":
            h._bytes(st.farm_project(rest[1]).encode("utf-8"), "application/json; charset=utf-8")
        elif len(rest) == 4 and rest[0] == "tasks" and rest[3] == "result":
            j = st.farm_job(rest[1], with_tasks=False)
            p = st.farm_result_path(rest[1], int(rest[2]), j["params"].get("ext", ".bin")) if j else None
            if p is None or not p.exists():
                h._json({"error": "まだありません"}, 404)
            else:
                h._send_file(str(p))
        elif rest == ["nodes"]:
            nodes = st.nodes()
            info = st.farm_nodes_info()
            for n in nodes:
                n.update(info["flags"].get(n["name"], {"disabled": False, "kinds": []}))
                n["busy"] = info["busy"].get(n["name"])
            h._json(nodes)
        else:
            h._json({"error": "not found"}, 404)
        return True
    if method == "PUT" and len(rest) == 4 and rest[0] == "tasks" and rest[3] == "result":
        jid, idx = rest[1], int(rest[2])
        j = st.farm_job(jid, with_tasks=False)
        if not j:
            h._json({"error": "ジョブがありません"}, 404)
            return True
        p = st.farm_result_path(jid, idx, j["params"].get("ext", ".bin"))
        h._save_upload(p)
        task = next((t for t in st.farm_job(jid)["tasks"] if t["idx"] == idx), None)
        if task and task["kind"] == "render":
            h._json(st.farm_done(jid, idx, qs.get("node", "?"), None, str(p)))
        else:
            h._json(st.farm_done(jid, idx, qs.get("node", "?"), p))
        return True
    if method == "POST":
        body = json.loads(h._body() or b"{}")
        if rest == ["export"]:
            h._json(st.farm_add_export(body))
        elif rest == ["transcode"]:
            h._json(st.farm_add_transcode(body.get("items", []), body.get("preset", ""), body.get("size", ""),
                                          body.get("user", ""), int(body.get("priority", 0))))
        elif rest == ["claim"]:
            h._json(st.farm_claim(body.get("node", "?"), body.get("kinds")))
        elif len(rest) == 4 and rest[0] == "tasks" and rest[3] == "progress":
            h._json(st.farm_progress(rest[1], int(rest[2]), body.get("node", "?"), body.get("progress", 0)))
        elif len(rest) == 4 and rest[0] == "tasks" and rest[3] == "fail":
            h._json(st.farm_fail(rest[1], int(rest[2]), body.get("node", "?"), body.get("error", ""), bool(body.get("skip"))))
        elif len(rest) == 4 and rest[0] == "tasks" and rest[3] == "done":       # 結果を NAS などに直接書いたとき
            h._json(st.farm_done(rest[1], int(rest[2]), body.get("node", "?"), None, body.get("path", "")))
        elif len(rest) == 3 and rest[0] == "jobs" and rest[2] == "cancel":
            h._json(st.farm_cancel(rest[1]))
        elif len(rest) == 3 and rest[0] == "jobs" and rest[2] == "retry":
            h._json(st.farm_retry(rest[1]))
        elif len(rest) == 2 and rest[0] == "nodes":
            h._json(st.farm_node_set(urllib.parse.unquote(rest[1]), body))
        else:
            h._json({"error": "not found"}, 404)
        return True
    if method == "PATCH" and len(rest) == 2 and rest[0] == "jobs":
        h._json(st.farm_update(rest[1], json.loads(h._body() or b"{}")))
        return True
    return False


# ─────────────────────────── ノード側（レンダーノード・TeloCut）───────────────────────────
def _req(url, data=None, method=None, timeout=60, raw=False):
    from .rendercluster import _req as r
    return r(url, data, method=method, timeout=timeout, raw=raw)


def _put_file(url: str, path: str, timeout: int = 3600):
    """大きなファイルを少しずつ送る（メモリに全部読まない）。"""
    import http.client
    u = urllib.parse.urlparse(url)
    size = os.path.getsize(path)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    try:
        conn.putrequest("PUT", u.path + ("?" + u.query if u.query else ""))
        conn.putheader("Content-Length", str(size))
        conn.putheader("X-TeloCut", "1")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.endheaders()
        with open(path, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                conn.send(b)
        r = conn.getresponse()
        body = r.read()
        if r.status >= 400:
            raise RuntimeError(f"送れませんでした（{r.status}）: {body[:200]!r}")
        return json.loads(body or b"null")
    finally:
        conn.close()


def _download(url: str, path: str, timeout: int = 3600):
    import urllib.request
    req = urllib.request.Request(url, headers={"X-TeloCut": "1"})
    tmp = path + ".dl"
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    os.replace(tmp, path)
    return path


class _Reporter:
    """進み具合をサーバーへ（2 秒ごと）。中止されたら cancel を立てる。"""

    def __init__(self, server: str, t: dict, node: str):
        self.server, self.t, self.node = server, t, node
        self.cancel = threading.Event()
        self._last = 0.0

    def __call__(self, x: float):
        if time.time() - self._last < 2 and x < 1:
            return
        self._last = time.time()
        try:
            r = _req(f"{self.server}/api/farm/tasks/{self.t['job_id']}/{self.t['idx']}/progress",
                     {"node": self.node, "progress": x}, timeout=15)
            if r and r.get("cancel"):
                self.cancel.set()
        except Exception:
            pass


def _local(path: str) -> str:
    """この PC で見えるパスにする（パス置換ルール）。"""
    from .settings import SETTINGS
    for c in (path, SETTINGS.map_path(path)):
        if c and os.path.exists(c):
            return c
    return ""


_projects: Dict[str, object] = {}


def run_task(server: str, node: str, t: dict, log: Callable[[str], None] = print) -> bool:
    """配られた仕事を 1 つ行う。成功したら True。"""
    from . import exportcore as EC
    from .mediacodecs import transcode
    server = server.rstrip("/")
    rep = _Reporter(server, t, node)
    p = t["params"]
    tmpdir = tempfile.mkdtemp(prefix="telocut_farm_")
    try:
        if t["kind"] == "transcode":
            src = ""
            if p.get("src_path"):
                src = _local(p["src_path"])
                if not src:
                    raise FileNotFoundError(f"この PC から見えません: {p['src_path']}")
            elif t.get("asset"):
                src = _local(t["asset"]["path"])
                if not src:                                  # 見えなければサーバーからもらう
                    src = os.path.join(tmpdir, "src" + Path(t["asset"]["name"]).suffix)
                    log(f"素材をダウンロード: {t['asset']['name']}")
                    _download(f"{server}/api/assets/{t['asset']['id']}/file", src)
            out = os.path.join(tmpdir, "out" + p["ext"])
            transcode(src, out, p["preset"], p.get("size", ""), rep, rep.cancel)
            dst_dir = _local(p.get("dst_dir", "")) if p.get("dst_dir") else ""
            if dst_dir and os.access(dst_dir, os.W_OK):      # 保存先が見えるなら直接置く
                from .mediacodecs import default_name
                dst = os.path.join(dst_dir, default_name(p.get("src_path") or t["job_name"], p["preset"], p.get("size", "")))
                k = 1
                while os.path.exists(dst):
                    dst = os.path.join(dst_dir, f"{Path(default_name(p.get('src_path') or t['job_name'], p['preset'], p.get('size', ''))).stem}_{k}{p['ext']}")
                    k += 1
                shutil.copy2(out, dst)
                _req(f"{server}/api/farm/tasks/{t['job_id']}/{t['idx']}/done", {"node": node, "path": dst})
            else:
                _put_file(f"{server}/api/farm/tasks/{t['job_id']}/{t['idx']}/result?node={urllib.parse.quote(node)}", out)
            log(f"変換 完了: {t['job_name']}")
            return True
        # 書き出し（区間 / 全体 / つなぐ）
        proj = _projects.get(t["job_id"])
        if proj is None:
            from .model import Project
            from .rendercluster import _resolve_media
            proj = Project.from_json(_req(f"{server}/api/farm/jobs/{t['job_id']}/project", raw=True).decode("utf-8"))
            off = _resolve_media(proj)
            used = {c.media_id for _, c in proj.sequence.all_clips() if c.media_id}
            missing = [m.name for m in off if m.id in used]
            if missing:
                raise FileNotFoundError("この PC から見えない素材: " + ", ".join(missing[:3]) + "（パス置換ルールを確認）")
            _projects.clear()
            _projects[t["job_id"]] = proj
        out = os.path.join(tmpdir, f"{t['idx']}{p['ext']}")
        if t["kind"] in ("render", "full"):
            n = max(1, t["b"] - t["a"])
            EC.render_to_file(proj, t["a"], t["b"], out, p["preset"], p.get("quality", 18), p.get("alpha", False),
                              audio=(t["kind"] == "full"), progress=lambda i, _n: rep(i / n), cancel=rep.cancel)
        else:
            chunks = []
            for k in t["chunks"]:
                cp = os.path.join(tmpdir, f"c{k}{p['ext']}")
                _download(f"{server}/api/farm/tasks/{t['job_id']}/{k}/result", cp)
                chunks.append(cp)
                rep(0.5 * len(chunks) / len(t["chunks"]))
            EC.merge_chunks(chunks, out, p["preset"], proj, p["a"], p["b"], cancel=rep.cancel)
        if rep.cancel.is_set():
            raise InterruptedError("中止しました")
        _put_file(f"{server}/api/farm/tasks/{t['job_id']}/{t['idx']}/result?node={urllib.parse.quote(node)}", out)
        log(f"{ {'render': '区間', 'full': '書き出し', 'merge': 'つなぐ'}[t['kind']] } 完了: {t['job_name']} {t['a']}-{t['b']}")
        return True
    except InterruptedError:
        log("中止されました")
        return False
    except Exception as ex:
        log(f"失敗: {ex}")
        try:
            _req(f"{server}/api/farm/tasks/{t['job_id']}/{t['idx']}/fail",
                 {"node": node, "error": str(ex)[:400], "skip": isinstance(ex, FileNotFoundError)})
        except Exception:
            pass
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def claim(server: str, node: str, kinds=None) -> Optional[dict]:
    return _req(server.rstrip("/") + "/api/farm/claim", {"node": node, "kinds": list(kinds or KINDS)}, timeout=20)
