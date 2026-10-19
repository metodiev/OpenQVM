"""CLI/GUI hand-off helpers that do not need a display."""

from __future__ import annotations

import argparse
import sys

from openqvm import cli


def _args(**kwargs) -> argparse.Namespace:
    values = {"data_dir": None, "qemu_dir": None, "json": False}
    values.update(kwargs)
    return argparse.Namespace(**values)


def test_gui_arguments_are_optional():
    from openqvm.__main__ import _gui_arguments

    assert _gui_arguments([]) == (None, None)
    data_dir, qemu_dir = _gui_arguments(
        ["ignored", "--data-dir", "data", "--qemu-dir", "C:/qemu", "--verbose"]
    )
    assert str(data_dir) == "data"
    assert qemu_dir == "C:/qemu"


def test_launch_sibling_gui_reports_a_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "openqvm-cli.exe"))
    assert cli._launch_sibling_gui(_args()) == cli.EXIT_BACKEND


def test_launch_sibling_gui_starts_the_desktop_binary(tmp_path, monkeypatch):
    cli_exe = tmp_path / "openqvm-cli.exe"
    cli_exe.write_bytes(b"")
    gui_exe = tmp_path / ("OpenQVM.exe" if sys.platform == "win32" else "OpenQVM")
    gui_exe.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(cli_exe))
    launched: list[list[str]] = []

    class FakePopen:
        def __init__(self, command, **kwargs) -> None:  # type: ignore[no-untyped-def]
            launched.append(list(command))

    monkeypatch.setattr("subprocess.Popen", FakePopen)
    result = cli._launch_sibling_gui(
        _args(data_dir=tmp_path / "data", qemu_dir="C:/qemu")
    )

    assert result == cli.EXIT_OK
    assert launched == [
        [str(gui_exe), "--data-dir", str(tmp_path / "data"), "--qemu-dir", "C:/qemu"]
    ]
