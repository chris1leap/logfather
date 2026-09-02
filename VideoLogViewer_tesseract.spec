# -*- mode: python ; coding: utf-8 -*-
# Open-source references used in this build spec:
# - PyInstaller: https://www.pyinstaller.org/
# - PySide6 (Qt for Python): https://doc.qt.io/qtforpython/
# - Tesseract OCR: https://github.com/tesseract-ocr/tesseract
# - pytesseract: https://github.com/madmaze/pytesseract
from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

tmp_ret = collect_all("PySide6")
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

tmp_ret = collect_all("cv2")
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

version_file = Path("version.json")
if version_file.exists():
    datas.append((str(version_file), "."))
readme_file = Path("README.md")
if readme_file.exists():
    datas.append((str(readme_file), "."))
for _asset_name in (
    "logfather.png",
    "Logfather animated splash screen Argus II.mp4",
    "Logfather Argus II.jpg",
    "Logfather.ico",
    "logfather_architecture.svg",
):
    _asset_file = Path("assets") / _asset_name
    if _asset_file.exists():
        datas.append((str(_asset_file), "."))

# Set TESSERACT_ROOT to the directory containing tesseract.exe and tessdata.
_tesseract_root = os.environ.get("TESSERACT_ROOT")
if _tesseract_root:
    tesseract_root = Path(_tesseract_root)
else:
    tesseract_root = Path.cwd() / "vendor" / "tesseract"

tesseract_exe = tesseract_root / "tesseract.exe"
tessdata_dir = tesseract_root / "tessdata"
if tesseract_exe.exists():
    binaries.append((str(tesseract_exe), "tesseract"))
    for dll in tesseract_root.glob("*.dll"):
        binaries.append((str(dll), "tesseract"))
if tessdata_dir.exists():
    datas.append((str(tessdata_dir), "tesseract/tessdata"))

a = Analysis(
    ["src/Main_Window.py"],
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
    name="The Logfather",
    icon="assets/Logfather.ico",
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
    name="The Logfather",
)
