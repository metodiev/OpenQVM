"""Background task execution for the GUI.

VM operations can take seconds or minutes (disk creation, exports, snapshots).
They run on a Qt thread pool so the interface never blocks; results and errors
are delivered through signals on the GUI thread.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from openqvm.core.errors import OpenQVMError, error_from_exception
from openqvm.core.logging import get_logger

log = get_logger("ui.tasks")


class TaskSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(object)


class _Task(QRunnable):
    def __init__(self, function: Callable[[], Any], signals: TaskSignals) -> None:
        super().__init__()
        self.function = function
        self.signals = signals
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            result = self.function()
        except Exception as exc:
            error = error_from_exception(exc, default_message="The operation failed.")
            log.exception("Background operation failed: %s", error.message)
            self.signals.failed.emit(error)
            return
        self.signals.succeeded.emit(result)


class AsyncRunner(QObject):
    busy_changed = Signal(bool, str)

    def __init__(self, parent: QObject | None = None, *, max_threads: int = 4) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max_threads)
        self._tasks: list[_Task] = []
        self._active = 0

    def submit(
        self,
        description: str,
        function: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[OpenQVMError], None] | None = None,
    ) -> None:
        signals = TaskSignals(self)
        task = _Task(function, signals)
        self._tasks.append(task)
        self._active += 1
        self.busy_changed.emit(True, description)

        def cleanup() -> None:
            if task in self._tasks:
                self._tasks.remove(task)
            self._active = max(0, self._active - 1)
            if self._active == 0:
                self.busy_changed.emit(False, "")

        def succeeded(result: Any) -> None:
            cleanup()
            if on_success is not None:
                on_success(result)

        def failed(error: OpenQVMError) -> None:
            cleanup()
            if on_error is not None:
                on_error(error)

        signals.succeeded.connect(succeeded)
        signals.failed.connect(failed)
        self._pool.start(task)

    def wait_for_done(self, timeout_ms: int = 120000) -> bool:
        return self._pool.waitForDone(timeout_ms)
