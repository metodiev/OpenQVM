# PyInstaller specification (experimental, not covered by automated tests).
#
# Build from the repository root after installing openqvm and PyInstaller:
#   pyinstaller packaging/openqvm.spec
#
# The result is a one-directory bundle under dist/OpenQVM. Validate it on each
# target platform and include the Qt/PySide6 license files before distributing.

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = collect_submodules("openqvm") + [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]
datas = collect_data_files("openqvm")

a = Analysis(
    ["src/openqvm/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OpenQVM",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="OpenQVM",
)
