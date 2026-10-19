"""Atomic file writes.

Configuration and metadata are written to a temporary file in the destination
directory, flushed to disk and then atomically replaced.  A crash therefore never
leaves a half-written JSON document behind.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from openqvm.core.errors import CorruptDataError, OperationFailedError


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except Exception as exc:  # pragma: no cover - exercised through failure injection
        temp_path.unlink(missing_ok=True)
        raise OperationFailedError(
            f"Could not write {path.name}.",
            hint="Check that the data directory is writable and has free space.",
            cause=exc,
        ) from exc


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2) -> None:
    try:
        text = json.dumps(payload, indent=indent, ensure_ascii=False, sort_keys=False)
    except (TypeError, ValueError) as exc:
        raise OperationFailedError("Could not serialize data to JSON.", cause=exc) from exc
    atomic_write_text(path, text + "\n")


def read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise OperationFailedError(f"Could not read {path.name}.", cause=exc) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CorruptDataError(
            f"{path.name} contains invalid JSON.",
            hint="Restore the file from a backup or remove the affected virtual machine.",
            details={"file": str(path), "line": exc.lineno, "column": exc.colno},
            cause=exc,
        ) from exc


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync so the rename is durable on POSIX."""

    if os.name == "nt":  # directory handles cannot be fsynced portably on Windows
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
