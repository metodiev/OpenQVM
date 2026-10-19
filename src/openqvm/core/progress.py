"""Progress reporting for long-running operations.

Operations accept a :data:`ProgressCallback` and report a fraction between 0 and 1
plus a short message, or ``None`` when the remaining work cannot be estimated.
The manager republishes every report on the event bus as ``operation.progress``,
so the GUI, the CLI and tests all observe the same numbers.

Byte-based work (exports, imports, copies) uses :class:`ByteProgress`, which turns
"bytes handled in the current phase" into a fraction of the whole operation.
"""

from __future__ import annotations

from collections.abc import Callable

ProgressCallback = Callable[[float | None, str], None]


def report_progress(
    progress: ProgressCallback | None, fraction: float | None, message: str = ""
) -> None:
    """Call *progress* with a clamped fraction; a no-op when nobody is listening."""

    if progress is None:
        return
    if fraction is None:
        progress(None, message)
        return
    progress(min(1.0, max(0.0, float(fraction))), message)


class ByteProgress:
    """Accumulate bytes into a fraction of a fixed total and forward it."""

    def __init__(
        self, progress: ProgressCallback | None, total_bytes: int, *, message: str = ""
    ) -> None:
        self._progress = progress
        self._total = max(1, int(total_bytes))
        self._done = 0
        self._message = message

    @property
    def fraction(self) -> float:
        return min(1.0, self._done / self._total)

    def advance(self, nbytes: int, message: str | None = None) -> None:
        self._done += max(0, int(nbytes))
        report_progress(self._progress, self.fraction, self._message if message is None else message)

    def finish(self, message: str | None = None) -> None:
        self._done = self._total
        report_progress(self._progress, 1.0, self._message if message is None else message)
