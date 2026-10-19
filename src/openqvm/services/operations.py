"""Operation history used by the dashboard and CLI diagnostics."""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from openqvm.core.errors import ErrorCode, OpenQVMError
from openqvm.core.ids import new_id
from openqvm.core.logging import get_logger
from openqvm.core.models import OperationRecord, OperationStatus, utc_now_iso

log = get_logger("operations")


class OperationLog:
    """Thread-safe in-memory ring buffer backed by an append-only JSONL file."""

    def __init__(self, path: Path, *, max_entries: int = 200) -> None:
        self.path = Path(path)
        self.max_entries = max_entries
        self._records: deque[OperationRecord] = deque(maxlen=max_entries)
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-self.max_entries * 4 :]
        except OSError:
            return
        latest: dict[str, OperationRecord] = {}
        order: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = OperationRecord.from_dict(json.loads(line))
            except Exception:
                continue
            if record.id not in latest:
                order.append(record.id)
            latest[record.id] = record
        for record_id in order[-self.max_entries :]:
            record = latest[record_id]
            if record.status is OperationStatus.RUNNING:
                record.status = OperationStatus.FAILED
                record.message = record.message or "OpenQVM exited before the operation completed."
                record.finished_at = record.finished_at or utc_now_iso()
            self._records.append(record)

    def start(self, action: str, *, vm_id: str | None = None, vm_name: str = "") -> OperationRecord:
        record = OperationRecord(id=new_id(), action=action, vm_id=vm_id, vm_name=vm_name)
        with self._lock:
            self._records.append(record)
            self._append(record)
        return record

    def finish(
        self,
        record: OperationRecord,
        *,
        status: OperationStatus,
        message: str = "",
        error: OpenQVMError | None = None,
    ) -> OperationRecord:
        record.status = status
        record.finished_at = utc_now_iso()
        record.message = message or (error.message if error else "")
        record.error_code = error.code.value if error else None
        with self._lock:
            self._append(record)
        return record

    def succeed(self, record: OperationRecord, message: str = "") -> OperationRecord:
        return self.finish(record, status=OperationStatus.SUCCEEDED, message=message)

    def fail(self, record: OperationRecord, error: OpenQVMError) -> OperationRecord:
        status = (
            OperationStatus.CANCELLED if error.code == ErrorCode.CANCELLED else OperationStatus.FAILED
        )
        return self.finish(record, status=status, error=error)

    def recent(self, limit: int = 20) -> list[OperationRecord]:
        with self._lock:
            return list(reversed(list(self._records)[-limit:]))

    def _append(self, record: OperationRecord) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            log.exception("Could not persist operation record %s", record.id)


def relative_time(iso_timestamp: str | None) -> str:
    """Human friendly relative time for UI labels."""

    if not iso_timestamp:
        return "unknown"
    try:
        moment = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return iso_timestamp
    now = datetime.now(UTC)
    delta = (now - moment).total_seconds()
    if delta < 5:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"
