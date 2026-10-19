"""Cross-platform advisory file locking.

A per-VM lock serializes lifecycle and storage operations and prevents two
OpenQVM processes from starting the same VM concurrently.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType

from openqvm.core.errors import ConflictError


class FileLock:
    """Advisory exclusive lock backed by a lock file.

    On Windows ``msvcrt.locking`` is used; on POSIX ``fcntl.flock``.  The lock is
    released automatically when the process exits, which avoids stale locks after
    a crash.
    """

    def __init__(self, path: Path, *, timeout: float = 10.0, poll_interval: float = 0.1) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._handle: object | None = None
        self._acquired = False

    def acquire(self) -> None:
        if self._acquired:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 - lifetime is managed by this object
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                _try_lock(handle)
                self._handle = handle
                self._acquired = True
                return
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise ConflictError(
                        "Another OpenQVM operation is already using this virtual machine.",
                        hint="Wait for the running operation to finish and try again.",
                        details={"lock_file": str(self.path)},
                    ) from None
                time.sleep(self.poll_interval)

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        self._acquired = False
        if handle is None:
            return
        try:
            _unlock(handle)  # type: ignore[arg-type]
        finally:
            handle.close()  # type: ignore[attr-defined]

    @property
    def acquired(self) -> bool:
        return self._acquired

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


if os.name == "nt":  # pragma: no branch - platform specific
    import msvcrt

    def _try_lock(handle) -> None:  # type: ignore[no-untyped-def]
        handle.seek(0)
        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:  # type: ignore[no-untyped-def]
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - exercised on POSIX CI
    import fcntl

    def _try_lock(handle) -> None:  # type: ignore[no-untyped-def]
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:  # type: ignore[no-untyped-def]
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
