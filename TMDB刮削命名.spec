# -*- mode: python ; coding: utf-8 -*-
# One-file windowed exe. The engine is compiled in via hiddenimports.
# Do not ship tmdb_format_rename.py as data: that extracts a .py the exe
# would still look like it depends on. No system Python and no sibling .py.

a = Analysis(
    ['tmdb_刮削命名.pyw'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['tmdb_format_rename'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TMDB刮削命名',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
