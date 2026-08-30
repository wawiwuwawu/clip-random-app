# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

fw_hiddenimports = (
    ['faster_whisper', 'ctranslate2', 'tokenizers', 'huggingface_hub', 'certifi']
    + collect_submodules('faster_whisper')
)
fw_datas = (
    collect_data_files('faster_whisper')
    + collect_data_files('tokenizers')
    + collect_data_files('huggingface_hub')
    + collect_data_files('certifi')
)


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ffmpeg-2026-07-02-git-95a888b9ca-full_build\\bin\\ffmpeg.exe', '.'),
        ('ffmpeg-2026-07-02-git-95a888b9ca-full_build\\bin\\ffprobe.exe', '.'),
        ('assets\\models\\mp.rnnn', 'assets/models'),
        ('assets\\models\\faster-whisper-tiny', 'assets/models/faster-whisper-tiny'),
    ] + fw_datas,
    hiddenimports=fw_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PyQt5', 'PyQt6', 'PySide2', 'shiboken2', 'qtpy',
        'gmpy2', 'tkinter', 'matplotlib', 'zmq',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SmartVideoCompiler',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon-SmartVideoCompiler.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='SmartVideoCompiler',
)
