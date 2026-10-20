"""ComfyUI 連携: AI 画像を生成して、そのままビン（AI生成）に取り込む。

- ComfyUI の HTTP API（/prompt, /history, /view, /upload/image, /object_info）を使う
- プリセット: SDXL / FLUX（txt2img）と、現在のフレームを元にした img2img
- 自作ワークフロー: ComfyUI の「API 形式で保存」した JSON に {{prompt}} {{negative}} {{seed}}
  {{width}} {{height}} {{steps}} {{image}} {{denoise}} を書いておくと置き換えて実行する
- 生成した画像は <プロジェクト>/AI生成 に保存し、プロンプトをタグ・クレジットに残す
"""
from __future__ import annotations

import json
import random
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, List, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit,
                             QPlainTextEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QPushButton, QLabel,
                             QListWidget, QListWidgetItem, QProgressBar, QFileDialog)

from .settings import SETTINGS

SIZES = [
    ("横 1920×1080（16:9）", 1344, 768, 1920, 1080),
    ("サムネイル 1280×720", 1344, 768, 1280, 720),
    ("縦 1080×1920（ショート）", 768, 1344, 1080, 1920),
    ("正方形 1024", 1024, 1024, 0, 0),
    ("立ち絵 832×1216", 832, 1216, 0, 0),
]

PRESETS = {
    "FLUX（高品質）": "flux",
    "SDXL（速い）": "sdxl",
    "自作ワークフロー（JSON）": "custom",
}


class ComfyError(RuntimeError):
    pass


class ComfyClient:
    def __init__(self, url: str = ""):
        self.url = (url or SETTINGS.comfy_url).rstrip("/")
        self.client_id = uuid.uuid4().hex

    def _get(self, path: str, timeout=10):
        with urllib.request.urlopen(self.url + path, timeout=timeout) as r:
            return r.read()

    def _json(self, path: str, data=None, timeout=10):
        req = urllib.request.Request(self.url + path,
                                     data=None if data is None else json.dumps(data).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8")
        except urllib.error.HTTPError as ex:
            raise ComfyError(ex.read().decode("utf-8", "replace")[:800]) from ex
        return json.loads(body) if body else None

    def ok(self) -> bool:
        try:
            self._json("/system_stats", timeout=3)
            return True
        except Exception:
            return False

    def checkpoints(self) -> List[str]:
        try:
            d = self._json("/object_info/CheckpointLoaderSimple")
            return [c for c in d["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
                    if not c.startswith("._")]
        except Exception:
            return []

    def upload_image(self, path: str) -> str:
        boundary = uuid.uuid4().hex
        data = Path(path).read_bytes()
        name = Path(path).name
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
                f"Content-Type: image/png\r\n\r\n").encode() + data + \
               f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.url + "/upload/image", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8"))
        return (d.get("subfolder") + "/" if d.get("subfolder") else "") + d["name"]

    def queue(self, workflow: dict) -> str:
        d = self._json("/prompt", {"prompt": workflow, "client_id": self.client_id}, timeout=30)
        if d.get("node_errors"):
            raise ComfyError(json.dumps(d["node_errors"], ensure_ascii=False)[:800])
        return d["prompt_id"]

    def interrupt(self):
        try:
            self._json("/interrupt", {}, timeout=5)
        except Exception:
            pass

    def wait(self, prompt_id: str, cancel: Optional[Callable[[], bool]] = None,
             tick: Optional[Callable[[str], None]] = None, timeout: float = 1800) -> List[dict]:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if cancel and cancel():
                self.interrupt()
                raise InterruptedError
            h = self._json(f"/history/{prompt_id}")
            if h and prompt_id in h:
                rec = h[prompt_id]
                st = rec.get("status", {})
                if st.get("status_str") == "error":
                    msg = next((m[1].get("exception_message", "") for m in st.get("messages", [])
                                if m[0] == "execution_error"), "生成に失敗しました")
                    raise ComfyError(msg)
                imgs = [im for out in rec.get("outputs", {}).values() for im in out.get("images", [])
                        if im.get("type") == "output"]
                if imgs or st.get("completed"):
                    return imgs
            if tick:
                q = self._json("/queue")
                n = len(q.get("queue_pending", []))
                tick(f"生成中… {int(time.time() - t0)} 秒" + (f"（待ち {n} 件）" if n else ""))
            time.sleep(1.0)
        raise ComfyError("タイムアウトしました")

    def download(self, img: dict, out_dir: Path, stem: str) -> Path:
        q = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                                    "type": img.get("type", "output")})
        out_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(img["filename"]).suffix or ".png"
        p = out_dir / f"{stem}{ext}"
        i = 1
        while p.exists():
            p = out_dir / f"{stem}_{i}{ext}"
            i += 1
        p.write_bytes(self._get("/view?" + q, timeout=120))
        return p


# ─────────────────────────────────────────────
#  ワークフロー
# ─────────────────────────────────────────────
def workflow(kind: str, prompt: str, negative: str = "", width: int = 1024, height: int = 1024,
             steps: int = 0, seed: int = -1, ckpt: str = "", image: str = "", denoise: float = 0.6,
             custom: str = "") -> dict:
    seed = random.randint(0, 2 ** 48) if seed < 0 else seed
    if kind == "custom":
        s = custom
        # 文字列の中の値（"{{prompt}}"）と、数値の位置（{{seed}}）の両方を置き換える
        for k, v in {"prompt": prompt, "negative": negative, "image": image}.items():
            s = s.replace("{{" + k + "}}", json.dumps(v, ensure_ascii=False)[1:-1])
        for k, v in {"seed": seed, "width": width, "height": height, "steps": steps or 20, "denoise": denoise}.items():
            s = s.replace('"{{' + k + '}}"', str(v)).replace("{{" + k + "}}", str(v))
        try:
            wf = json.loads(s)
        except json.JSONDecodeError as ex:
            raise ComfyError(f"ワークフロー JSON が読めません: {ex}")
        if "nodes" in wf and "links" in wf:
            raise ComfyError("ComfyUI の「API 形式で保存（Export (API)）」で保存した JSON を使ってください")
        return wf
    flux = kind == "flux"
    ckpt = ckpt or ("flux1-dev-fp8.safetensors" if flux else "sd_xl_base_1.0.safetensors")
    steps = steps or (20 if flux else 25)
    wf = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative if not flux else "", "clip": ["1", 1]}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0],
            "seed": seed, "steps": steps, "cfg": 1.0 if flux else 7.0,
            "sampler_name": "euler" if flux else "dpmpp_2m", "scheduler": "simple" if flux else "karras",
            "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "TeloCut"}},
    }
    if flux:
        wf["8"] = {"class_type": "FluxGuidance", "inputs": {"conditioning": ["2", 0], "guidance": 3.5}}
        wf["5"]["inputs"]["positive"] = ["8", 0]
    if image:
        wf["9"] = {"class_type": "LoadImage", "inputs": {"image": image}}
        wf["4"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 0], "vae": ["1", 2]}}
        wf["5"]["inputs"]["denoise"] = float(denoise)
    else:
        wf["4"] = {"class_type": "EmptySD3LatentImage" if flux else "EmptyLatentImage",
                   "inputs": {"width": width, "height": height, "batch_size": 1}}
    return wf


def fit_to(path: Path, w: int, h: int):
    """生成サイズ（モデル向き）から書き出しサイズへ。中央で切り抜いて拡大する。"""
    if not w or not h:
        return
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        out = ImageOps.fit(im.convert("RGB"), (w, h), Image.Resampling.LANCZOS)
    out.save(path)


class GenerateWorker(QThread):
    progress = pyqtSignal(str)
    image_ready = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, url: str, jobs: List[dict], out_dir: Path, init_image: str = ""):
        super().__init__()
        self.url, self.jobs, self.out_dir, self.init_image = url, jobs, out_dir, init_image
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        c = ComfyClient(self.url)
        try:
            image = c.upload_image(self.init_image) if self.init_image else ""
            for i, job in enumerate(self.jobs):
                if self._cancel:
                    return
                self.progress.emit(f"{i + 1}/{len(self.jobs)} 送信中…")
                fit = job.pop("_fit", (0, 0))
                stem = job.pop("_stem", "ai")
                pid = c.queue(workflow(image=image, **job))
                imgs = c.wait(pid, cancel=lambda: self._cancel,
                              tick=lambda m, i=i: self.progress.emit(f"{i + 1}/{len(self.jobs)} {m}"))
                for im in imgs:
                    p = c.download(im, self.out_dir, stem)
                    fit_to(p, *fit)
                    self.image_ready.emit(str(p))
            self.progress.emit("完了")
        except InterruptedError:
            self.progress.emit("中止しました")
        except Exception as ex:
            self.failed.emit(str(ex))


def _stem(prompt: str) -> str:
    import re
    s = re.sub(r"[^\w\-]+", "_", prompt.strip())[:32].strip("_")
    return f"ai_{time.strftime('%m%d_%H%M%S')}_{s or 'image'}"


class ComfyPanel(QWidget):
    """AI 画像生成パネル（ドック）。"""

    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self.worker: Optional[GenerateWorker] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        self.url = QLineEdit(SETTINGS.comfy_url)
        self.url.setPlaceholderText("http://192.168.0.53:8188")
        self.state = QLabel("●")
        test = QPushButton("接続確認")
        test.clicked.connect(self.check)
        top.addWidget(QLabel("ComfyUI"))
        top.addWidget(self.url, 1)
        top.addWidget(self.state)
        top.addWidget(test)
        lay.addLayout(top)
        form = QFormLayout()
        self.preset = QComboBox()
        self.preset.addItems(list(PRESETS))
        self.preset.currentIndexChanged.connect(self._preset_changed)
        self.ckpt = QComboBox()
        self.ckpt.setEditable(True)
        self.custom_path = QLineEdit()
        self.custom_path.setPlaceholderText("API 形式のワークフロー JSON")
        pick = QPushButton("…")
        pick.setFixedWidth(28)
        pick.clicked.connect(self._pick_custom)
        crow = QHBoxLayout()
        crow.addWidget(self.custom_path, 1)
        crow.addWidget(pick)
        self.custom_row = QWidget()
        self.custom_row.setLayout(crow)
        crow.setContentsMargins(0, 0, 0, 0)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("英語推奨。例: anime style background, cozy cafe interior, warm light")
        self.prompt.setFixedHeight(70)
        self.negative = QLineEdit("lowres, blurry, text, watermark")
        self.size = QComboBox()
        self.size.addItems([s[0] for s in SIZES])
        self.steps = QSpinBox()
        self.steps.setRange(0, 150)
        self.steps.setSpecialValueText("自動")
        self.seed = QSpinBox()
        self.seed.setRange(-1, 2 ** 31 - 1)
        self.seed.setSpecialValueText("ランダム")
        self.seed.setValue(-1)
        self.count = QSpinBox()
        self.count.setRange(1, 16)
        self.from_frame = QCheckBox("現在のフレームを元にする（img2img）")
        self.denoise = QDoubleSpinBox()
        self.denoise.setRange(0.05, 1.0)
        self.denoise.setSingleStep(0.05)
        self.denoise.setValue(0.6)
        self.place = QCheckBox("生成したら再生ヘッド位置に配置")
        form.addRow("方式", self.preset)
        form.addRow("モデル", self.ckpt)
        form.addRow("JSON", self.custom_row)
        form.addRow("プロンプト", self.prompt)
        form.addRow("ネガティブ", self.negative)
        form.addRow("サイズ", self.size)
        srow = QHBoxLayout()
        for lab, w in (("ステップ", self.steps), ("シード", self.seed), ("枚数", self.count)):
            srow.addWidget(QLabel(lab))
            srow.addWidget(w)
        form.addRow(srow)
        drow = QHBoxLayout()
        drow.addWidget(self.from_frame)
        drow.addWidget(QLabel("変化量"))
        drow.addWidget(self.denoise)
        form.addRow(drow)
        form.addRow(self.place)
        lay.addLayout(form)
        brow = QHBoxLayout()
        self.go = QPushButton("生成")
        self.go.setDefault(True)
        self.go.clicked.connect(self.generate)
        self.stop = QPushButton("中止")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(lambda: self.worker and self.worker.cancel())
        brow.addWidget(self.go, 1)
        brow.addWidget(self.stop)
        lay.addLayout(brow)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.hide()
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        lay.addWidget(self.bar)
        lay.addWidget(self.msg)
        self.results = QListWidget()
        self.results.setViewMode(QListWidget.ViewMode.IconMode)
        self.results.setIconSize(QSize(120, 80))
        self.results.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.results.itemDoubleClicked.connect(lambda it: self.mw.load_source_media_path(it.data(Qt.ItemDataRole.UserRole))
                                               if hasattr(self.mw, "load_source_media_path") else None)
        lay.addWidget(self.results, 1)
        self._preset_changed()

    def _preset_changed(self):
        kind = PRESETS[self.preset.currentText()]
        self.custom_row.setEnabled(kind == "custom")
        self.ckpt.setEnabled(kind != "custom")
        self.negative.setEnabled(kind != "flux")
        if kind != "custom" and self.ckpt.count():
            want = "flux" if kind == "flux" else "sd_xl"
            i = next((i for i in range(self.ckpt.count()) if want in self.ckpt.itemText(i).lower()), -1)
            if i >= 0:
                self.ckpt.setCurrentIndex(i)

    def _pick_custom(self):
        p, _ = QFileDialog.getOpenFileName(self, "ワークフロー（API 形式）", "", "JSON (*.json)")
        if p:
            self.custom_path.setText(p)

    def _client(self) -> ComfyClient:
        url = self.url.text().strip() or SETTINGS.comfy_url
        if url != SETTINGS.comfy_url:
            SETTINGS.comfy_url = url
            SETTINGS.save()
        return ComfyClient(url)

    def check(self) -> bool:
        c = self._client()
        ok = c.ok()
        self.state.setStyleSheet(f"color: {'#4c4' if ok else '#c44'}")
        if ok:
            cur = self.ckpt.currentText()
            self.ckpt.clear()
            self.ckpt.addItems(c.checkpoints())
            if cur:
                self.ckpt.setCurrentText(cur)
            self._preset_changed()
            self.msg.setText("接続しました")
        else:
            self.msg.setText(f"{c.url} に接続できません（ComfyUI が起動しているか、--listen 付きか確認）")
        return ok

    def build_jobs(self) -> List[dict]:
        kind = PRESETS[self.preset.currentText()]
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            raise ComfyError("プロンプトを入力してください")
        _, gw, gh, ow, oh = SIZES[self.size.currentIndex()]
        custom = Path(self.custom_path.text()).read_text(encoding="utf-8") if kind == "custom" else ""
        seed = self.seed.value()
        jobs = []
        for i in range(self.count.value()):
            jobs.append({"kind": kind, "prompt": prompt, "negative": self.negative.text(),
                         "width": gw, "height": gh, "steps": self.steps.value(),
                         "seed": seed + i if seed >= 0 else -1, "ckpt": self.ckpt.currentText(),
                         "denoise": self.denoise.value(), "custom": custom,
                         "_fit": (ow, oh), "_stem": _stem(prompt)})
        return jobs

    def generate(self):
        if self.worker and self.worker.isRunning():
            return
        try:
            jobs = self.build_jobs()
        except (ComfyError, OSError) as ex:
            self.msg.setText(str(ex))
            return
        from .integrations import work_dir
        out = work_dir(self.mw, "AI生成")
        init = ""
        if self.from_frame.isChecked():
            from . import media as MD, render as R
            init = str(out / "_img2img_src.png")
            pool = MD.DecoderPool()
            try:
                seq = self.mw.project.sequence
                R.render_frame(seq, self.mw.record_player.position, self.mw.project, pool, 1.0) \
                    .convert("RGB").save(init)
            finally:
                pool.close()
        self._prompt_used = self.prompt.toPlainText().strip()
        self._model_used = self.ckpt.currentText() or self.preset.currentText()
        self._place_frame = self.mw.record_player.position if self.place.isChecked() else None
        self.worker = GenerateWorker(self._client().url, jobs, out, init)
        self.worker.progress.connect(self.msg.setText)
        self.worker.image_ready.connect(self._got)
        self.worker.failed.connect(lambda e: self.msg.setText("失敗: " + e))
        self.worker.finished.connect(self._done)
        self.go.setEnabled(False)
        self.stop.setEnabled(True)
        self.bar.show()
        self.worker.start()

    def _done(self):
        self.go.setEnabled(True)
        self.stop.setEnabled(False)
        self.bar.hide()

    def _got(self, path: str):
        it = QListWidgetItem(QIcon(QPixmap(path)), Path(path).stem[-16:])
        it.setData(Qt.ItemDataRole.UserRole, path)
        it.setToolTip(path)
        self.results.insertItem(0, it)
        import_generated(self.mw, [path], self._prompt_used, self._model_used, self._place_frame)


def import_generated(mw, paths: List[str], prompt: str, model: str, place_frame=None):
    """生成画像を「AI生成」ビンに取り込み、プロンプトをタグとクレジットに残す。"""
    bid = mw.named_bin("AI生成")

    def tag(items):
        for m in items:
            m.credit = f"AI 生成（ComfyUI / {model}）"
            m.tags = list(dict.fromkeys((m.tags or []) + ["AI生成"] + [w.strip() for w in prompt.split(",")[:4] if w.strip()]))
    mw.import_files(paths, place=None if place_frame is None else (None, place_frame, False), bin_id=bid, after=tag)
