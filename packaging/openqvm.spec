# PyInstaller specification for the portable Windows bundle.
#
# Build from the repository root (see scripts/build.ps1 -Bundle):
#   python -m PyInstaller --noconfirm packaging/openqvm.spec
#
# The result is one directory, dist/OpenQVM, containing the windowed GUI
# (OpenQVM.exe) and the console CLI (openqvm-cli.exe). It is not code-signed; see
# docs/installation.md for the trust implications.

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")
GUI_ENTRY = os.path.join(SRC, "openqvm", "__main__.py")
CLI_ENTRY = os.path.join(SRC, "openqvm", "cli.py")

hiddenimports = collect_submodules("openqvm") + [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]
datas = collect_data_files("openqvm")

gui_analysis = Analysis(
    [GUI_ENTRY],
    pathex=[SRC],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc_data"],
    noarchive=False,
)
cli_analysis = Analysis(
    [CLI_ENTRY],
    pathex=[SRC],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("openqvm"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6", "tkinter", "unittest", "pydoc_data"],
    noarchive=False,
)

gui_pyz = PYZ(gui_analysis.pure)
cli_pyz = PYZ(cli_analysis.pure)

gui = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="OpenQVM",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
cli = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="openqvm-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    gui,
    gui_analysis.binaries,
    gui_analysis.datas,
    cli,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=False,
    name="OpenQVM",
)
