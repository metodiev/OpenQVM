"""Discovery of a usable QEMU installation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from openqvm.core.logging import get_logger

log = get_logger("qemu.discovery")

_SYSTEM_BINARY = "qemu-system-x86_64"
_IMAGE_BINARY = "qemu-img"
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass(frozen=True)
class QemuInstall:
    system_path: Path
    image_path: Path
    version: str
    accelerators: tuple[str, ...]
    share_dir: Path | None
    source: str

    def as_dict(self) -> dict[str, object]:
        return {
            "system_path": str(self.system_path),
            "image_path": str(self.image_path),
            "version": self.version,
            "accelerators": list(self.accelerators),
            "share_dir": str(self.share_dir) if self.share_dir else None,
            "source": self.source,
        }


def _exe_names(base: str) -> list[str]:
    if os.name == "nt":
        return [f"{base}.exe", f"{base}.cmd", f"{base}.bat"]
    return [base]


def candidate_directories(explicit: str | Path | None = None) -> list[Path]:
    candidates: list[Path] = []

    def add(value: str | Path | None) -> None:
        if not value:
            return
        path = Path(value).expanduser()
        if path not in candidates:
            candidates.append(path)

    add(explicit)
    add(os.environ.get("OPENQVM_QEMU_DIR"))
    found_system = shutil.which(_SYSTEM_BINARY)
    if found_system:
        add(Path(found_system).parent)
    if sys.platform == "win32":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        add(Path(program_files) / "qemu")
        add(Path(program_files) / "QEMU")
        if local_app_data:
            add(Path(local_app_data) / "Programs" / "qemu")
            add(Path(local_app_data) / "OpenQVM" / "qemu")
    elif sys.platform == "darwin":
        add("/opt/homebrew/bin")
        add("/usr/local/bin")
        add("/Applications/QEMU.app/Contents/MacOS")
    else:
        add("/usr/bin")
        add("/usr/local/bin")
        add("/opt/qemu/bin")
    return candidates


def _run(argv: list[str], *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )


def _first_executable(directory: Path, base: str) -> Path | None:
    for name in _exe_names(base):
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _probe_version(executable: Path, *, is_system: bool) -> str | None:
    try:
        result = _run([str(executable), "--version"], timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("Could not run %s: %s", executable, exc)
        return None
    output = f"{result.stdout}\n{result.stderr}".strip()
    match = re.search(r"QEMU (?:emulator|PC emulator) version ([0-9][^\s]*)", output)
    if match:
        return match.group(1)
    match = re.search(r"qemu-img\s+([0-9][^\s]*)", output)
    if match:
        return match.group(1)
    if result.returncode == 0:
        return output.splitlines()[0] if output else "unknown"
    log.debug("%s --version failed with %s: %s", executable, result.returncode, output)
    return None


def _probe_accelerators(executable: Path) -> tuple[str, ...]:
    try:
        result = _run([str(executable), "-accel", "help"], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ()
    accels: list[str] = []
    for line in f"{result.stdout}\n{result.stderr}".splitlines():
        name = line.strip()
        if not name or name.endswith(":") or " " in name:
            continue
        accels.append(name)
    return tuple(accels)


def _share_directory(qemu_dir: Path) -> Path | None:
    candidates = [
        qemu_dir / "share",
        qemu_dir.parent / "share" / "qemu",
        qemu_dir / ".." / "share" / "qemu",
    ]
    if sys.platform != "win32":
        candidates.extend([Path("/usr/share/qemu"), Path("/usr/local/share/qemu"), Path("/opt/homebrew/share/qemu")])
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate.resolve()
        except OSError:
            continue
    return None


def discover_qemu(explicit_dir: str | Path | None = None) -> QemuInstall | None:
    """Find a working ``qemu-system-x86_64`` and ``qemu-img`` pair.

    The search is deterministic: an explicit directory wins, then
    ``OPENQVM_QEMU_DIR``, then the system PATH, then well-known install
    locations.  A candidate is accepted only when both binaries execute
    successfully.
    """

    seen: set[Path] = set()
    for directory in candidate_directories(explicit_dir):
        try:
            directory = directory.resolve()
        except OSError:
            continue
        if directory in seen or not directory.is_dir():
            continue
        seen.add(directory)
        system_path = _first_executable(directory, _SYSTEM_BINARY)
        image_path = _first_executable(directory, _IMAGE_BINARY)
        if system_path is None or image_path is None:
            continue
        version = _probe_version(system_path, is_system=True)
        image_version = _probe_version(image_path, is_system=False)
        if version is None or image_version is None:
            continue
        accelerators = _probe_accelerators(system_path)
        install = QemuInstall(
            system_path=system_path,
            image_path=image_path,
            version=version,
            accelerators=accelerators,
            share_dir=_share_directory(directory),
            source=str(directory),
        )
        log.info("Using QEMU %s from %s (accelerators: %s)", version, directory, ", ".join(accelerators) or "none")
        return install
    log.debug("No QEMU installation discovered (searched %s directories)", len(seen))
    return None
