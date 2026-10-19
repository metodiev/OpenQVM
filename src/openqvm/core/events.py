"""Small synchronous publish/subscribe bus used by the service layer."""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any

log = logging.getLogger("openqvm.events")


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, topic: str, callback: Callable[[Any], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers[topic].append(callback)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers[topic].remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def publish(self, topic: str, payload: Any = None) -> None:
        with self._lock:
            callbacks = list(self._subscribers.get(topic, ()))
        for callback in callbacks:
            try:
                callback(payload)
            except Exception:  # pragma: no cover - subscriber isolation
                log.exception("Subscriber for %s failed", topic)
