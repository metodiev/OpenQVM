"""Event bus semantics: delivery, wildcard, unsubscribe and isolation."""

from __future__ import annotations

import threading

from openqvm.core.events import (
    ALL_TOPICS,
    TOPIC_ANY,
    TOPIC_VM_CREATED,
    TOPIC_VM_STATE_CHANGED,
    EventBus,
    VmEvent,
)


def test_publish_delivers_to_topic_subscribers():
    bus = EventBus()
    received: list[object] = []
    bus.subscribe(TOPIC_VM_CREATED, received.append)
    payload = VmEvent(vm_id="6f0c6b5a-5e80-4f88-9c0b-2a1b8f1e5c11", name="vm")
    assert bus.publish(TOPIC_VM_CREATED, payload) == 1
    assert received == [payload]


def test_wildcard_receives_every_topic_once():
    bus = EventBus()
    topics: list[str] = []
    bus.subscribe(TOPIC_ANY, lambda payload: topics.append(payload))
    bus.publish(TOPIC_VM_CREATED, "a")
    bus.publish(TOPIC_VM_STATE_CHANGED, "b")
    assert topics == ["a", "b"]


def test_publish_to_wildcard_topic_does_not_duplicate():
    bus = EventBus()
    calls: list[object] = []
    bus.subscribe(TOPIC_ANY, calls.append)
    bus.publish(TOPIC_ANY, "payload")
    assert calls == ["payload"]


def test_unsubscribe_is_idempotent():
    bus = EventBus()
    calls: list[object] = []
    unsubscribe = bus.subscribe(TOPIC_VM_CREATED, calls.append)
    assert bus.subscriber_count(TOPIC_VM_CREATED) == 1
    unsubscribe()
    unsubscribe()
    assert bus.subscriber_count(TOPIC_VM_CREATED) == 0
    bus.publish(TOPIC_VM_CREATED, "x")
    assert calls == []


def test_failing_subscriber_does_not_block_others():
    bus = EventBus()
    calls: list[str] = []

    def broken(payload) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("subscriber exploded")

    bus.subscribe(TOPIC_VM_CREATED, broken)
    bus.subscribe(TOPIC_VM_CREATED, calls.append)
    assert bus.publish(TOPIC_VM_CREATED, "ok") == 1
    assert calls == ["ok"]


def test_subscribers_can_unsubscribe_while_publishing():
    bus = EventBus()
    calls: list[object] = []

    def once(payload) -> None:  # type: ignore[no-untyped-def]
        calls.append(payload)
        unsubscribe()

    unsubscribe = bus.subscribe(TOPIC_VM_CREATED, once)
    bus.publish(TOPIC_VM_CREATED, 1)
    bus.publish(TOPIC_VM_CREATED, 2)
    assert calls == [1]


def test_publish_from_threads():
    bus = EventBus()
    calls: list[int] = []
    lock = threading.Lock()

    def record(value: int) -> None:
        with lock:
            calls.append(value)

    bus.subscribe(TOPIC_VM_STATE_CHANGED, record)
    threads = [threading.Thread(target=bus.publish, args=(TOPIC_VM_STATE_CHANGED, index)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(calls) == list(range(20))


def test_clear_removes_every_subscription():
    bus = EventBus()
    for topic in ALL_TOPICS:
        bus.subscribe(topic, lambda payload: None)
    assert bus.subscriber_count() == len(ALL_TOPICS)
    bus.clear()
    assert bus.subscriber_count() == 0
