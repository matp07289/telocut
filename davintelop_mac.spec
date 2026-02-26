# -*- mode: python ; coding: utf-8 -*-
# DavinTelop macOS PyInstaller spec
# 生成物: dist/DavinTelop.app  →  dist/DavinTelop-1.0.0.pkg

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
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
        'xml.etree.ElementTree',
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
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DavinTelop',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch='arm64',
    codesign_identity=None,
    entitlements_file=None,
    icon='DavinTelop.icns',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='DavinTelop',
)

app = BUNDLE(
    coll,
    name='DavinTelop.app',
    icon='DavinTelop.icns',
    bundle_identifier='com.davintelop.app',
    version='1.0.0',
    info_plist={
        'NSHighResolutionCapable': True,
        'NSPrincipalClass': 'NSApplication',
        'NSAppleScriptEnabled': False,
        'CFBundleDisplayName': 'DavinTelop',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'LSMinimumSystemVersion': '11.0',
        'LSUIElement': False,
        'CFBundleDocumentTypes': [],
    },
)
