"""In-process publish/subscribe bus used by the service and UI layers.

The bus is synchronous and thread-safe: publishing from a worker thread calls each
subscriber on that thread, so subscribers that touch widgets must bridge to the GUI
thread (the Qt signal connection does this automatically).  Subscriber failures are
logged and never interrupt other subscribers or the operation that published.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from openqvm.core.models import VMRuntime, VMStatus

log = logging.getLogger("openqvm.events")

TOPIC_VM_CREATED = "vm.created"
TOPIC_VM_UPDATED = "vm.updated"
TOPIC_VM_DELETED = "vm.deleted"
TOPIC_VM_STATE_CHANGED = "vm.state_changed"
TOPIC_SNAPSHOTS_CHANGED = "snapshots.changed"
TOPIC_BACKUPS_CHANGED = "backups.changed"
TOPIC_OPERATION_STARTED = "operation.started"
TOPIC_OPERATION_PROGRESS = "operation.progress"
TOPIC_OPERATION_FINISHED = "operation.finished"
TOPIC_ANY = "*"

ALL_TOPICS = (
    TOPIC_VM_CREATED,
    TOPIC_VM_UPDATED,
    TOPIC_VM_DELETED,
    TOPIC_VM_STATE_CHANGED,
    TOPIC_SNAPSHOTS_CHANGED,
    TOPIC_BACKUPS_CHANGED,
    TOPIC_OPERATION_STARTED,
    TOPIC_OPERATION_PROGRESS,
    TOPIC_OPERATION_FINISHED,
)


@dataclass(frozen=True)
class VmEvent:
    """Payload for VM create/update/delete notifications."""

    vm_id: str
    name: str = ""
    reason: str = ""


@dataclass(frozen=True)
class VmStateEvent:
    """Payload for lifecycle transitions; both states are reported truthfully."""

    vm_id: str
    name: str
    previous: VMStatus
    current: VMStatus
    runtime: VMRuntime

    @property
    def changed(self) -> bool:
        return self.previous is not self.current


@dataclass(frozen=True)
class SnapshotEvent:
    vm_id: str
    name: str = ""
    reason: str = ""


@dataclass(frozen=True)
class BackupEvent:
    vm_id: str
    name: str = ""
    reason: str = ""
    path: str = ""


@dataclass(frozen=True)
class OperationProgress:
    """Payload for ``operation.progress``.

    ``fraction`` is ``None`` when the operation cannot estimate the remaining work
    (for example a single ``qemu-img`` call), which the UI renders as a busy
    indicator instead of a percentage.
    """

    action: str
    fraction: float | None = None
    message: str = ""
    vm_id: str | None = None
    vm_name: str = ""

    @property
    def percent(self) -> int | None:
        return None if self.fraction is None else round(self.fraction * 100)

    def describe(self) -> str:
        label = self.message or self.action
        return label if self.percent is None else f"{label} — {self.percent} %"


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[Any], None]]] = {}
        self._lock = threading.RLock()

    def subscribe(self, topic: str, callback: Callable[[Any], None]) -> Callable[[], None]:
        """Register *callback*; returns an idempotent unsubscribe function."""

        with self._lock:
            self._subscribers.setdefault(topic, []).append(callback)

        def unsubscribe() -> None:
            with self._lock:
                callbacks = self._subscribers.get(topic)
                if not callbacks:
                    return
                try:
                    callbacks.remove(callback)
                except ValueError:
                    return
                if not callbacks:
                    self._subscribers.pop(topic, None)

        return unsubscribe

    def publish(self, topic: str, payload: Any = None) -> int:
        """Notify subscribers of *topic* and the wildcard topic.

        Returns the number of subscribers that were invoked successfully.
        """

        with self._lock:
            callbacks = list(self._subscribers.get(topic, ()))
            if topic != TOPIC_ANY:
                callbacks += list(self._subscribers.get(TOPIC_ANY, ()))
        delivered = 0
        for callback in callbacks:
            try:
                callback(payload)
            except Exception:  # pragma: no cover - subscriber isolation
                log.exception("Subscriber for %s failed", topic)
            else:
                delivered += 1
        return delivered

    def subscriber_count(self, topic: str | None = None) -> int:
        with self._lock:
            if topic is None:
                return sum(len(callbacks) for callbacks in self._subscribers.values())
            return len(self._subscribers.get(topic, ()))

    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()
