"""QEMU process supervision.

QEMU runs as a detached process so virtual machines survive closing the OpenQVM
window.  Every operation re-validates the process identity (PID *and* creation
time) before signalling it, which avoids acting on a recycled PID.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from openqvm.core.errors import BackendError, OperationFailedError
from openqvm.core.logging import get_logger

log = get_logger("qemu.process")

_CREATE_FLAGS = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    create_time: float


def launch_qemu(argv: list[str], *, log_path: Path, cwd: Path | None = None) -> ProcessInfo:
    """Start QEMU detached and return its verified process identity."""

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = log_path.open("ab")
    except OSError as exc:
        raise OperationFailedError("Could not open the QEMU log file.", cause=exc) from exc
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            cwd=str(cwd) if cwd else None,
            close_fds=True,
            start_new_session=os.name != "nt",
            creationflags=_CREATE_FLAGS,
        )
    except OSError as exc:
        raise BackendError(
            "QEMU could not be started.",
            hint="Verify the QEMU installation and that the accelerator is available.",
            cause=exc,
        ) from exc
    finally:
        handle.close()
    try:
        info = ProcessInfo(pid=process.pid, create_time=psutil.Process(process.pid).create_time())
    except psutil.Error as exc:
        raise BackendError("QEMU started but its process could not be inspected.", cause=exc) from exc
    log.info("Started QEMU pid=%s create_time=%.3f", info.pid, info.create_time)
    return info


def process_matches(pid: int | None, create_time: float | None) -> bool:
    if not pid:
        return False
    try:
        process = psutil.Process(pid)
        if not process.is_running():
            return False
        if create_time is not None and abs(process.create_time() - create_time) > 2.0:
            return False
        return True
    except psutil.Error:
        return False


def wait_for_exit(pid: int | None, create_time: float | None, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_matches(pid, create_time):
            return True
        time.sleep(0.1)
    return not process_matches(pid, create_time)


def terminate_process(pid: int | None, create_time: float | None, *, timeout: float = 10.0) -> bool:
    """Terminate a verified QEMU process (and its children)."""

    if not process_matches(pid, create_time):
        return True
    try:
        process = psutil.Process(pid)  # type: ignore[arg-type]
        children = process.children(recursive=True)
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        for child in children:
            try:
                if child.is_running():
                    child.kill()
            except psutil.Error:
                pass
        return True
    except psutil.Error as exc:
        log.warning("Could not terminate QEMU pid=%s: %s", pid, exc)
        return not process_matches(pid, create_time)


def read_log_tail(path: Path, *, max_chars: int = 4000) -> str:
    try:
        with Path(path).open("rb") as handle:
            try:
                handle.seek(-max_chars, os.SEEK_END)
            except OSError:
                handle.seek(0)
            data = handle.read(max_chars)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").strip()
