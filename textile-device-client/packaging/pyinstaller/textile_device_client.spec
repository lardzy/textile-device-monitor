# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path


project_root = Path(SPECPATH).resolve().parents[1]
scripts_root = project_root / "scripts"
if str(scripts_root) not in sys.path:
    sys.path.insert(0, str(scripts_root))

from build_support import collect_hidden_imports


entry_script = project_root / "main.py"
app_icon = project_root / "resources" / "icon.ico"
console_mode = os.environ.get("TDC_PYINSTALLER_CONSOLE", "0") == "1"
bootloader_debug = os.environ.get("TDC_PYINSTALLER_BOOTLOADER_DEBUG", "0") == "1"


a = Analysis(
    [str(entry_script)],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
    hiddenimports=collect_hidden_imports(),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "IPython", "jupyter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="textile-device-client",
    debug=bootloader_debug,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=console_mode,
    icon=str(app_icon),
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
    name="TextileDeviceClient",
)
