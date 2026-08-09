# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path


project_root = Path(SPECPATH).parent
src_path = project_root / "src"

block_cipher = None

a = Analysis(
    [str(project_root / "packaging" / "stockbot_service_entry.py")],
    pathex=[str(src_path)],
    binaries=[],
    datas=[
        (str(project_root / "data" / "symbols.csv"), "data"),
    ],
    hiddenimports=[
        "pywintypes",
        "servicemanager",
        "win32event",
        "win32service",
        "win32serviceutil",
        "win32timezone",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
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
    name="StockBotService",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="StockBotService",
)
