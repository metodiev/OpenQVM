"""Safe subprocess execution for QEMU command-line tools.

All arguments are passed as an argv list; ``shell=True`` is never used.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from openqvm.core.cancellation import CancellationToken
from openqvm.core.errors import BackendUnavailableError, CancelledError, OperationFailedError, TimeoutError_
from openqvm.platform.capabilities import qemu_environment

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass(frozen=True)
class ToolResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output_tail(self) -> str:
        combined = "\n".join(part for part in (self.stdout.strip(), self.stderr.strip()) if part)
        return combined[-4000:]


def run_tool(
    argv: list[str],
    *,
    timeout: float = 300.0,
    cwd: Path | None = None,
    tool_name: str | None = None,
    cancel: CancellationToken | None = None,
) -> ToolResult:
    if not argv or not all(isinstance(part, str) and part for part in argv):
        raise OperationFailedError("An invalid command was constructed.")
    executable = Path(argv[0])
    if not executable.is_file():
        raise BackendUnavailableError(
            f"{tool_name or executable.name} was not found.",
            hint="Install QEMU or configure the QEMU directory in Settings.",
            details={"executable": str(executable)},
        )
    name = tool_name or executable.name
    if cancel is not None:
        cancel.raise_if_cancelled(f"{name} was cancelled before it started.")

    common = {
        "stdin": subprocess.DEVNULL,
        "text": True,
        "errors": "replace",
        "cwd": str(cwd) if cwd else None,
        "env": qemu_environment(),
        "creationflags": _CREATE_NO_WINDOW,
    }
    if cancel is None:
        try:
            completed = subprocess.run(argv, timeout=timeout, capture_output=True, check=False, **common)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError_(
                f"{name} did not finish within {timeout:.0f} seconds.",
                hint="The operation may still be running; check the VM state before retrying.",
                cause=exc,
            ) from exc
        except OSError as exc:
            raise OperationFailedError(
                f"{name} could not be executed.",
                hint="Verify the QEMU installation and file permissions.",
                details={"executable": str(executable)},
                cause=exc,
            ) from exc
        return ToolResult(argv=list(argv), returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)

    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **common
        )
    except OSError as exc:
        raise OperationFailedError(
            f"{name} could not be executed.",
            hint="Verify the QEMU installation and file permissions.",
            details={"executable": str(executable)},
            cause=exc,
        ) from exc

    deadline = time.monotonic() + timeout
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if cancel.is_cancelled:
                _terminate(process)
                raise CancelledError(
                    f"{name} was cancelled.",
                    hint="No partial result was kept.",
                    details={"executable": str(executable)},
                ) from None
            if time.monotonic() >= deadline:
                _terminate(process)
                raise TimeoutError_(
                    f"{name} did not finish within {timeout:.0f} seconds.",
                    hint="The operation may still be running; check the VM state before retrying.",
                ) from None
    return ToolResult(argv=list(argv), returncode=process.returncode or 0, stdout=stdout or "", stderr=stderr or "")


def _terminate(process: subprocess.Popen[str]) -> None:
    try:
        process.terminate()
        process.communicate(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        try:
            process.kill()
            process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
