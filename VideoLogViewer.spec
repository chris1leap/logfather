# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('PySide6')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('cv2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

version_file = Path('version.json')
if version_file.exists():
    datas.append((str(version_file), '.'))
readme_file = Path('README.md')
if readme_file.exists():
    datas.append((str(readme_file), '.'))
logo_file = Path('logfather.png')
if logo_file.exists():
    datas.append((str(logo_file), '.'))
placeholder_file = Path('Logfather Argus II.jpg')
if placeholder_file.exists():
    datas.append((str(placeholder_file), '.'))
splash_file = Path('Logfather animated splash screen Argus II.mp4')
if splash_file.exists():
    datas.append((str(splash_file), '.'))
icon_file = Path('logfather.ico')
if icon_file.exists():
    datas.append((str(icon_file), '.'))


a = Analysis(
    ['Main_Window.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='The Logfather',
    icon='logfather.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
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
    name='The Logfather',
)
