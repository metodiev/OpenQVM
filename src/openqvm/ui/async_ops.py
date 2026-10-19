"""Background task execution for the GUI.

VM operations can take seconds or minutes (disk creation, exports, snapshots).
They run on a Qt thread pool so the interface never blocks; results and errors
are delivered through signals on the GUI thread.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from openqvm.core.cancellation import CancellationToken
from openqvm.core.errors import CancelledError, ErrorCode, OpenQVMError, error_from_exception
from openqvm.core.logging import get_logger

log = get_logger("ui.tasks")


class _TaskSignals(QObject):
    """Shared signal carrier: the runner receives one queued call per finished task."""

    finished = Signal(int, object)
    failed = Signal(int, object)


class _Task(QRunnable):
    def __init__(
        self,
        job_id: int,
        function: Callable[[CancellationToken], Any],
        signals: _TaskSignals,
        token: CancellationToken,
    ) -> None:
        super().__init__()
        self.job_id = job_id
        self.function = function
        self.signals = signals
        self.token = token
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            result = self.function(self.token)
        except Exception as exc:
            error = error_from_exception(exc, default_message="The operation failed.")
            if isinstance(exc, CancelledError) or error.code == ErrorCode.CANCELLED:
                log.info("Background operation cancelled: %s", error.message)
            else:
                log.exception("Background operation failed: %s", error.message)
            self.signals.failed.emit(self.job_id, error)
            return
        self.signals.finished.emit(self.job_id, result)


@dataclass
class _Job:
    token: CancellationToken
    description: str
    on_success: Callable[[Any], None] | None
    on_error: Callable[[OpenQVMError], None] | None


class AsyncRunner(QObject):
    busy_changed = Signal(bool, str)

    def __init__(self, parent: QObject | None = None, *, max_threads: int = 4) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max_threads)
        self._jobs: dict[int, _Job] = {}
        self._next_job_id = 1
        self._signals = _TaskSignals(self)
        self._signals.finished.connect(self._on_finished)
        self._signals.failed.connect(self._on_failed)

    @property
    def active_count(self) -> int:
        return len(self._jobs)

    @property
    def active_descriptions(self) -> list[str]:
        return [job.description for job in self._jobs.values()]

    def submit(
        self,
        description: str,
        function: Callable[[CancellationToken], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[OpenQVMError], None] | None = None,
    ) -> CancellationToken:
        """Run *function* on the pool and return the token that can cancel it."""

        job_id = self._next_job_id
        self._next_job_id += 1
        token = CancellationToken()
        self._jobs[job_id] = _Job(token, description, on_success, on_error)
        self._emit_busy()
        self._pool.start(_Task(job_id, function, self._signals, token))
        return token

    def cancel_active(self, reason: str = "cancelled by the user") -> int:
        """Ask every running task to stop; returns how many were still cancellable."""

        requested = 0
        for job in self._jobs.values():
            if not job.token.is_cancelled:
                job.token.cancel(reason)
                requested += 1
        return requested

    @Slot(int, object)
    def _on_finished(self, job_id: int, result: Any) -> None:
        job = self._jobs.pop(job_id, None)
        if job is None:
            return
        self._emit_busy()
        if job.on_success is not None:
            job.on_success(result)

    @Slot(int, object)
    def _on_failed(self, job_id: int, error: OpenQVMError) -> None:
        job = self._jobs.pop(job_id, None)
        if job is None:
            return
        self._emit_busy()
        if job.on_error is not None:
            job.on_error(error)

    def _emit_busy(self) -> None:
        if not self._jobs:
            self.busy_changed.emit(False, "")
        else:
            self.busy_changed.emit(True, next(reversed(self._jobs.values())).description)

    def wait_for_done(self, timeout_ms: int = 120000) -> bool:
        return self._pool.waitForDone(timeout_ms)
