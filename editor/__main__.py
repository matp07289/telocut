"""python -m editor で TeloCut を起動。

  --selftest <結果ファイル>  合成・テロップ描画・書き出しを試して結果を書き出し終了（ビルド確認用）
"""
import os
import sys
import traceback
from pathlib import Path

# リポジトリ直下を import パスに（core/ を共有するため）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

LOG_DIR = Path.home() / ".davintelop"


def _install_crash_log():
    """ウィンドウ版 EXE は標準エラーが無いので、未処理例外をログに残す。"""
    log = LOG_DIR / "telocut_crash.log"

    def hook(t, v, tb):
        text = "".join(traceback.format_exception(t, v, tb))
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                import datetime
                f.write(f"\n==== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ====\n{text}")
        except OSError:
            pass
        if sys.stderr:
            sys.stderr.write(text)
    sys.excepthook = hook


def selftest(result_path: str) -> int:
    """GUI を出さずに主要機能を確認する。"""
    import tempfile
    import time
    lines = []
    code = 0
    try:
        from PyQt6.QtWidgets import QApplication
        from editor import model as M
        from editor import media as MD, render
        from editor.export import ExportWorker
        app = QApplication.instance() or QApplication(sys.argv)
        lines.append("Qt OK")
        import av
        lines.append(f"PyAV {av.__version__}: libx264={'libx264' in av.codecs_available} "
                     f"aac={'aac' in av.codecs_available} prores={'prores_ks' in av.codecs_available}")
        proj = M.Project(sequences=[M.Sequence.create(width=640, height=360, fps=30.0)])
        from core.models import DesignSettings
        proj.design = DesignSettings().to_dict()
        seq = proj.sequence
        seq.video_tracks[0].clips.append(M.Clip(M.new_id(), "color", 0, 30, props={"color": "#203080"}))
        seq.video_tracks[1].clips.append(M.Clip(M.new_id(), "text", 5, 20, props={"text": "テロップ テスト"}))
        im = render.render_frame(seq, 10, proj, MD.DecoderPool(), 1.0)
        lines.append(f"render OK {im.size} center={im.getpixel((320, 30))}")
        out = os.path.join(tempfile.gettempdir(), "telocut_selftest.mp4")
        w = ExportWorker(proj, 0, 30, out, "libx264", 28)
        res = {}
        w.finished_ok.connect(lambda p: res.setdefault("ok", p))
        w.failed.connect(lambda e: res.setdefault("err", e))
        w.start()
        t0 = time.time()
        while not res and time.time() - t0 < 60:
            app.processEvents()
            time.sleep(0.02)
        w.wait(5000)
        if "ok" in res:
            item = MD.probe(out)
            lines.append(f"export OK {os.path.getsize(out)} bytes, {item.duration_s:.2f}s audio={item.has_audio}")
            dec = MD.VideoDecoder(out)
            fr = dec.get(0.5, (320, 180))
            dec.close()
            lines.append(f"video decode {'OK' if fr is not None else 'NG'}")
            if fr is None:
                code = 1
            if MD.build_audio_cache(out) is None:
                lines.append("audio cache NG")
                code = 1
            else:
                lines.append("audio cache OK")
        else:
            lines.append(f"export NG {res.get('err')}")
            code = 1
        # AAF / FCP XML の書き出しと読み戻し
        from editor import interchange as IX
        seq.video_tracks[0].clips[0].media_id = ""
        x = os.path.join(tempfile.gettempdir(), "telocut_selftest.xml")
        a_ = os.path.join(tempfile.gettempdir(), "telocut_selftest.aaf")
        item.id = "m1"
        proj.media = [item]
        seq.video_tracks[0].clips.append(M.Clip(M.new_id(), "video", 30, 20, 0, "m1", "v"))
        IX.export_fcpxml(proj, seq, x)
        IX.export_aaf(proj, seq, a_)
        nx = len(IX.import_fcpxml(x)[0].clips)
        na = len(IX.import_aaf(a_)[0].clips)
        lines.append(f"interchange XML={nx} AAF={na}")
        if nx < 1 or na < 1:
            code = 1
        from editor.mainwindow import MainWindow
        os.environ["TELOCUT_NO_BRIDGE"] = "1"     # 確認用なので連携の接続は開かない
        mw = MainWindow()
        lines.append(f"MainWindow OK: {mw.windowTitle()}")
        mw.dirty = False
        mw.close()
    except Exception:
        lines.append(traceback.format_exc())
        code = 1
    lines.append("RESULT " + ("PASS" if code == 0 else "FAIL"))
    Path(result_path).write_text("\n".join(lines), encoding="utf-8")
    return code


def _arg(name: str, default: str = "") -> str:
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main():
    import multiprocessing
    multiprocessing.freeze_support()      # 並列書き出し（spawn）を EXE でも動かす
    _install_crash_log()
    if "--mcp" in sys.argv:
        # Claude Code 用の MCP サーバー（標準入出力）。TeloCutCLI.exe --mcp
        from editor import mcp_server
        mcp_server.main()
        return
    if "--asset-server" in sys.argv:
        # 自宅アセットサーバー / レンダーファームの受付。TeloCutCLI.exe --asset-server --root D:\素材
        from editor import assetserver
        assetserver.main([a for a in sys.argv[1:] if a != "--asset-server"])
        return
    if "--render-node" in sys.argv:
        # レンダリング用 PC。TeloCutCLI.exe --render-node --server http://192.168.0.10:8770
        from editor.rendercluster import run_node
        server = _arg("--server")
        if not server:
            print("使い方: --render-node --server http://<アセットサーバー>:8770 [--name 名前]")
            sys.exit(2)
        try:
            run_node(server, _arg("--name") or None)
        except KeyboardInterrupt:
            pass
        return
    if "--telop" in sys.argv:
        # DavinTelop（テロップ編集）をソースから起動
        import runpy
        runpy.run_path(os.path.join(_ROOT, "main.py"), run_name="__main__")
        return
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        res = sys.argv[i + 1] if i + 1 < len(sys.argv) else str(LOG_DIR / "telocut_selftest.txt")
        sys.exit(selftest(res))

    from PyQt6.QtWidgets import QApplication
    from editor.theme import QSS
    from editor.mainwindow import MainWindow
    from editor.assets import app_icon

    if sys.platform == "win32":
        try:   # タスクバーで Python ではなく TeloCut のアイコンを出す
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DavinTelop.TeloCut")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("TeloCut")
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    app.setWindowIcon(app_icon())
    w = MainWindow()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    projects = [a for a in args if a.lower().endswith((".tcut", ".dvep"))]
    if projects:
        w.open_project(projects[0])
    elif "--no-select" not in sys.argv:
        # Avid と同じく、起動時に「プロジェクトの選択」を表示
        if not w.run_select_project():
            sys.exit(0)
    w.show()
    media = [a for a in args if a not in projects and os.path.exists(a)]
    if media:
        w.import_files(media)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
