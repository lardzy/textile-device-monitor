# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=['PyQt6', 'PyQt6.QtWidgets', 'PyQt6.QtCore', 'pystray', 'PIL', 'psutil', 'requests', 'openpyxl', 'formulas', 'formulas', 'formulas._version', 'formulas.builder', 'formulas.cell', 'formulas.errors', 'formulas.excel', 'formulas.excel.cycle', 'formulas.excel.ods_reader', 'formulas.excel.xlreader', 'formulas.functions', 'formulas.functions.comp', 'formulas.functions.date', 'formulas.functions.eng', 'formulas.functions.financial', 'formulas.functions.google', 'formulas.functions.info', 'formulas.functions.logic', 'formulas.functions.look', 'formulas.functions.math', 'formulas.functions.operators', 'formulas.functions.stat', 'formulas.functions.text', 'formulas.parser', 'formulas.ranges', 'formulas.tokens', 'formulas.tokens.function', 'formulas.tokens.operand', 'formulas.tokens.operator', 'formulas.tokens.parenthesis'],
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
    [],
    exclude_binaries=True,
    name='textile-device-client',
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
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='textile-device-client',
)
