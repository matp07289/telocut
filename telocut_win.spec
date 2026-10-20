# -*- mode: python ; coding: utf-8 -*-
# TeloCut（動画編集ソフト）Windows PyInstaller spec  (PyInstaller 6.x)
#   ビルド: python -m PyInstaller telocut_win.spec --noconfirm
#   生成物: dist\TeloCut\ （フォルダ形式。起動が速い）
#     TeloCut.exe     動画編集（ウィンドウ版）
#     TeloCutCLI.exe  コンソール版（--mcp / --asset-server / --render-node 用）
#     DavinTelop.exe  テロップ編集（単体でも動き、TeloCut と連携する）
#
# 外部 ffmpeg は不要（PyAV 同梱の FFmpeg ライブラリで読み込み・音声解析・書き出しを行う）。
# NVENC 書き出しだけは、TeloCut.exe と同じフォルダか PATH に ffmpeg.exe があると選べる。

import os
import sys

sys.path.insert(0, os.path.abspath('.'))
from editor.assets import make_ico  # noqa: E402
from PyInstaller.utils.hooks import collect_submodules  # noqa: E402

ICON = os.path.abspath(os.path.join('editor', 'telocut.ico'))
make_ico(ICON)

VERSION = '0.9.0'
VERSION_FILE = os.path.abspath(os.path.join('editor', 'version_info.txt'))
_v = tuple(int(x) for x in VERSION.split('.')) + (0,)
with open(VERSION_FILE, 'w', encoding='utf-8') as f:
    f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={_v}, prodvers={_v}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041104B0', [
      StringStruct('CompanyName', 'DavinTelop'),
      StringStruct('FileDescription', 'TeloCut - テロップに強い動画編集ソフト'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'TeloCut'),
      StringStruct('OriginalFilename', 'TeloCut.exe'),
      StringStruct('ProductName', 'TeloCut'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [0x0411, 1200])])
  ]
)
""")

a = Analysis(
    ['telocut.py'],
    pathex=['.'],
    binaries=[],
    datas=[(ICON, '.'), (os.path.join('editor', 'assetweb.html'), 'editor')],
    hiddenimports=[
        *collect_submodules('editor'), 'core.telocut_link', 'core.voicevox', 'core.transcribe',
        'psd_tools', 'psd_tools.api', 'psd_tools.constants', 'editor', 'editor.mainwindow', 'editor.model', 'editor.media', 'editor.render',
        'editor.player', 'editor.monitor', 'editor.timeline', 'editor.bin_panel',
        'editor.inspector', 'editor.export', 'editor.theme', 'editor.assets',
        'editor.settings', 'editor.commands', 'editor.keyboard_dialog', 'editor.rendercache',
        'editor.projectio', 'editor.display', 'editor.prefs_dialog',
        'editor.project_window', 'editor.interchange', 'editor.analysis', 'editor.panels',
        'aaf2', 'aaf2.file', 'aaf2.mobs', 'aaf2.components', 'aaf2.essence', 'aaf2.misc',
        'aaf2.content', 'aaf2.mobslots', 'aaf2.dictionary', 'aaf2.metadict', 'aaf2.model',
        'core', 'core.models', 'core.timecode', 'core.renderer', 'core.preset',
        'core.exporter', 'core.textfmt',
        'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets',
        'av', 'numpy', 'PIL.Image', 'PIL.ImageDraw', 'PIL.ImageFont', 'PIL.ImageOps',
        'PIL.ImageEnhance', 'PIL.ImageFilter',
    ],
    excludes=[
        'tkinter', '_tkinter', 'sv_ttk', 'darkdetect',
        'PyQt5', 'PySide2', 'PySide6',
        'PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebEngineQuick',
        'PyQt6.QtQml', 'PyQt6.QtQuick', 'PyQt6.QtQuickWidgets', 'PyQt6.Qt3DCore',
        'PyQt6.QtPdf', 'PyQt6.QtPdfWidgets', 'PyQt6.QtDesigner', 'PyQt6.QtBluetooth',
        'PyQt6.QtNfc', 'PyQt6.QtSensors', 'PyQt6.QtSerialPort', 'PyQt6.QtPositioning',
        'PyQt6.QtSql', 'PyQt6.QtTest', 'PyQt6.QtTextToSpeech', 'PyQt6.QtRemoteObjects',
        'matplotlib', 'scipy', 'pandas', 'IPython', 'jupyter', 'notebook',
        'torch', 'faster_whisper', 'ctranslate2', 'onnxruntime', 'tokenizers',
        'huggingface_hub', 'sympy', 'cv2', 'mss', 'NDIlib',
        'pygrabber', 'comtypes', 'DaVinciResolveScript',
    ],
    noarchive=False,
)

# 使わない大きな DLL を除外（ソフトウェア OpenGL / Quick / QML / 3D / WebEngine 等）
_DROP = ('opengl32sw.dll', 'qt6quick', 'qt6qml', 'qt6pdf', 'qt6designer', 'qt6quick3d',
         'qt6webengine', 'qt63d', 'qt6shadertools', 'qt6virtualkeyboard', 'qt6charts',
         'qt6datavisualization', 'qt6graphs', 'qt6location', 'qt6sensors', 'qt6texttospeech',
         'qt6bluetooth', 'qt6nfc', 'qt6serialport', 'qt6positioning', 'qt6remoteobjects',
         'qt6sql', 'qt6test', 'qt6spatialaudio', 'qt6scxml', 'qt6statemachine')
a.binaries = [b for b in a.binaries
              if not any(k in os.path.basename(b[0]).lower() for k in _DROP)]
a.datas = [d for d in a.datas
           if not any(k in d[0].lower().replace('\\', '/') for k in
                      ('qt6/qml/', 'qt6/translations/qtwebengine', 'qt6/resources/qtwebengine'))]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TeloCut',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
    version=VERSION_FILE,
)

# コンソール版（MCP サーバー・アセットサーバー・レンダーノード用。中身は TeloCut.exe と同じ）
exe_cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TeloCutCLI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=ICON,
    version=VERSION_FILE,
)

# ── DavinTelop（tkinter のテロップ編集ソフト）─────────────
import sv_ttk  # noqa: E402
b = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[(os.path.dirname(sv_ttk.__file__), 'sv_ttk')],
    hiddenimports=[
        'PIL', 'PIL.Image', 'PIL.ImageDraw', 'PIL.ImageFont', 'PIL.ImageFilter', 'PIL.PngImagePlugin',
        'PIL.JpegImagePlugin', 'PIL.BmpImagePlugin', 'PIL.TiffImagePlugin', 'PIL._imaging',
        'tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox', 'tkinter.colorchooser',
        'tkinter.font', 'tkinter.simpledialog',
        'core', 'core.models', 'core.timecode', 'core.renderer', 'core.exporter', 'core.preset',
        'core.voicevox', 'core.lipsync', 'core.fusion', 'core.capture', 'core.transcribe',
        'core.telocut_link', 'core.textfmt',
        'csv', 'json', 'pathlib', 'dataclasses', 'importlib.util', 'xml.etree.ElementTree', 'faulthandler',
        'sv_ttk', 'darkdetect', 'psd_tools', 'psd_tools.api', 'psd_tools.constants', 'NDIlib',
        'aaf2', 'aaf2.file', 'aaf2.mobs', 'aaf2.components', 'aaf2.essence', 'aaf2.misc',
        'mss', 'mss.base', 'mss.windows', 'numpy', 'cv2', 'pygrabber', 'pygrabber.dshow_graph',
        'comtypes', 'comtypes.client',
    ],
    excludes=[
        'DaVinciResolveScript', 'matplotlib', 'scipy', 'pandas', 'IPython', 'jupyter', 'notebook',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'wx', 'gi', 'editor',
        'faster_whisper', 'ctranslate2', 'av', 'onnxruntime', 'tokenizers', 'huggingface_hub',
        'torch', 'sympy',
    ],
    noarchive=False,
)
pyz_b = PYZ(b.pure)
exe_dt = EXE(
    pyz_b,
    b.scripts,
    [],
    exclude_binaries=True,
    name='DavinTelop',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.abspath('DavinTelop.ico'),
)

coll = COLLECT(
    exe, exe_cli, a.binaries, a.datas,
    exe_dt, b.binaries, b.datas,
    strip=False,
    upx=False,
    name='TeloCut',
)
