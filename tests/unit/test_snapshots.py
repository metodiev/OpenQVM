"""Snapshot table parsing."""

from __future__ import annotations

import pytest

from openqvm.core.errors import CancelledError
from openqvm.snapshots import parse_snapshot_list

QEMU_IMG_OUTPUT = """Snapshot list:
ID        TAG               VM SIZE                DATE       VM CLOCK
1         before-upgrade        0 B 2026-10-19 20:00:00   00:00:00.000
2         with-memory        512 MiB 2026-10-19 20:05:00   00:01:23.000
"""

MONITOR_OUTPUT = """List of snapshots present on all disks:
ID      TAG               VM_SIZE                DATE        VM_CLOCK     ICOUNT
--      live1                 0 B 2026-10-19 20:19:05  0000:00:47.480         --
"""


def test_parse_qemu_img_output():
    entries = parse_snapshot_list(QEMU_IMG_OUTPUT)
    assert [entry.name for entry in entries] == ["before-upgrade", "with-memory"]
    assert entries[0].vm_size == "0 B"
    assert entries[1].vm_size == "512 MiB"


def test_parse_monitor_disk_only_output():
    entries = parse_snapshot_list(MONITOR_OUTPUT)
    assert len(entries) == 1
    assert entries[0].name == "live1"
    assert entries[0].id == "--"
    assert entries[0].vm_size == "0 B"


def test_parse_empty_output():
    assert parse_snapshot_list("There is no snapshot available.\r\n") == []
    assert parse_snapshot_list("") == []


def test_parse_ignores_headers_and_duplicates():
    text = QEMU_IMG_OUTPUT + "3         before-upgrade        0 B 2026-10-19 20:06:00   00:00:00.000\n"
    entries = parse_snapshot_list(text)
    assert len(entries) == 2

# -- snapshot cancellation ---------------------------------------------------


class _StubDisks:
    """Disk manager placeholder: offline snapshots never reach qemu-img here."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def require_qemu_img(self):
        raise AssertionError("qemu-img must not be invoked when the token is cancelled")


def _snapshot_service(store):
    from openqvm.snapshots import SnapshotService

    return SnapshotService(store, _StubDisks())


def test_offline_snapshots_refuse_a_cancelled_token(store, linux_spec):
    from openqvm.core.cancellation import cancelled_token

    service = _snapshot_service(store)
    token = cancelled_token()
    with pytest.raises(CancelledError):
        service.create_offline(linux_spec, "before-upgrade", cancel=token)
    with pytest.raises(CancelledError):
        service.restore_offline(linux_spec, "before-upgrade", cancel=token)
    with pytest.raises(CancelledError):
        service.delete_offline(linux_spec, "before-upgrade", cancel=token)
