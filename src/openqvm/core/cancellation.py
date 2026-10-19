"""Cooperative cancellation for long-running operations.

A token is passed into storage and transfer work.  Operations check it between
chunks and before destructive steps; cancellation therefore never leaves a
half-written package or a partially registered VM, because each layer cleans up
its own temporary state when a :class:`CancelledError` propagates.
"""

from __future__ import annotations

import threading
import time

from openqvm.core.errors import CancelledError


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = ""
        self._lock = threading.Lock()

    def cancel(self, reason: str = "cancelled by the user") -> None:
        with self._lock:
            self._reason = reason or "cancelled"
            self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def raise_if_cancelled(self, message: str = "The operation was cancelled.") -> None:
        if self._event.is_set():
            raise CancelledError(
                message,
                hint="No partial result was kept." if not self._reason else self._reason,
            )

    def sleep(self, seconds: float) -> bool:
        """Sleep in small slices; returns False as soon as cancellation is requested."""

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._event.is_set():
                return False
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return not self._event.is_set()


def cancelled_token(reason: str = "cancelled before it started") -> CancellationToken:
    token = CancellationToken()
    token.cancel(reason)
    return token
