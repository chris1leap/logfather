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
for asset_name in (
    'logfather.png',
    'Logfather Argus II.jpg',
    'Logfather animated splash screen Argus II.mp4',
    'Logfather.ico',
    'logfather_architecture.svg',
):
    asset_file = Path('assets') / asset_name
    if asset_file.exists():
        datas.append((str(asset_file), '.'))


a = Analysis(
    ['src/Main_Window.py'],
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
    icon='assets/Logfather.ico',
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
