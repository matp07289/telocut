# -*- mode: python ; coding: utf-8 -*-
# DavinTelop Windows PyInstaller spec (PyInstaller 6.x 対応)
# 生成物: dist\DavinTelop.exe (onefile)

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        # sv_ttk テーマファイル (TCL/TK)
        (r'C:\Users\mizuk\AppData\Roaming\Python\Python38\site-packages\sv_ttk',
         'sv_ttk'),
    ],
    hiddenimports=[
        # Pillow
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL.ImageFont',
        'PIL.ImageFilter',
        'PIL.PngImagePlugin',
        'PIL.JpegImagePlugin',
        'PIL.BmpImagePlugin',
        'PIL.TiffImagePlugin',
        'PIL._imaging',
        # tkinter
        'tkinter',
        'tkinter.ttk',
        'tkinter.filedialog',
        'tkinter.messagebox',
        'tkinter.colorchooser',
        'tkinter.font',
        'tkinter.simpledialog',
        # core
        'core',
        'core.models',
        'core.timecode',
        'core.renderer',
        'core.exporter',
        'core.preset',
        # 標準ライブラリ
        'csv',
        'json',
        'pathlib',
        'dataclasses',
        'importlib.util',
        'xml.etree.ElementTree',
        'faulthandler',
        # テーマ
        'sv_ttk',
        'darkdetect',
        # VoiceVox / LipSync
        'core.voicevox',
        'core.lipsync',
        'psd_tools',
        'psd_tools.api',
        'psd_tools.constants',
        # NDI
        'NDIlib',
        # AAF (Avid Media Composer)
        'aaf2',
        'aaf2.file',
        'aaf2.mobs',
        'aaf2.components',
        'aaf2.essence',
        'aaf2.misc',
        # 合成プレビュー
        'core.capture',
        'mss',
        'mss.base',
        'mss.windows',
        'numpy',
    ],
    excludes=[
        'DaVinciResolveScript',
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        'IPython',
        'jupyter',
        'notebook',
        'PyQt5',
        'PyQt6',
        'PySide2',
        'PySide6',
        'wx',
        'gi',
        'cv2',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='DavinTelop',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='DavinTelop.ico',
)
