"""Live medium and disk operations driven through a fake QMP connection."""

from __future__ import annotations

import copy

import pytest

from openqvm.core.errors import OperationFailedError
from openqvm.core.factory import create_vm_spec
from openqvm.core.models import GuestOS, VMRuntime, VMSpec, VMStatus
from openqvm.core.paths import AppPaths
from openqvm.core.validation import HostResources
from openqvm.platform.capabilities import PlatformCapabilities
from openqvm.store import VmStore
from openqvm.vm.qemu.backend import QemuBackend
from openqvm.vm.qemu.medium import find_cdrom_device

ISO = "C:/images/installer.iso"


def _blocks(inserted: str | None = ISO, locked: bool = False) -> list[dict]:
    entry: dict = {
        "device": "drive0",
        "removable": False,
        "inserted": {"image": {"filename": "C:/vm/disk.qcow2", "virtual-size": 1024}},
    }
    cdrom: dict = {
        "device": "cdrom0",
        "removable": True,
        "tray_open": False,
        "locked": locked,
        "qdev": "/machine/peripheral-anon/device[0]",
    }
    if inserted is not None:
        cdrom["inserted"] = {"image": {"filename": inserted, "format": "raw"}}
    return [entry, cdrom]


class FakeQmp:
    def __init__(self, blocks: list[dict]) -> None:
        self.blocks = copy.deepcopy(blocks)
        self.commands: list[tuple[str, dict]] = []
        self.closed = False

    def execute(self, command, arguments=None):  # type: ignore[no-untyped-def]
        arguments = dict(arguments or {})
        self.commands.append((command, arguments))
        entry = find_cdrom_device(self.blocks)
        if command == "query-block":
            return copy.deepcopy(self.blocks)
        if command == "blockdev-open-tray":
            assert entry is not None
            if entry.get("locked") and not arguments.get("force"):
                return {}
            entry["tray_open"] = True
            return {}
        if command == "blockdev-close-tray":
            assert entry is not None
            entry["tray_open"] = False
            return {}
        if command == "blockdev-remove-medium":
            assert entry is not None
            if not entry.get("tray_open"):
                from openqvm.vm.qemu.qmp import QmpCommandError

                raise QmpCommandError(command, "GenericError", "Tray of device is not open")
            entry.pop("inserted", None)
            return {}
        if command == "blockdev-change-medium":
            assert entry is not None
            if entry.get("locked") and not arguments.get("force"):
                from openqvm.vm.qemu.qmp import QmpCommandError

                raise QmpCommandError(command, "GenericError", "Device is locked and force was not specified")
            entry["inserted"] = {"image": {"filename": arguments["filename"], "format": "raw"}}
            entry["tray_open"] = False
            return {}
        if command == "block_resize":
            blocks = [item for item in self.blocks if item.get("device") == "drive0"]
            blocks[0]["inserted"]["image"]["virtual-size"] = arguments["size"]
            return {}
        return {}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def backend(tmp_path) -> QemuBackend:
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    capabilities = PlatformCapabilities(os_family="linux", architecture="x86_64", qemu=None)
    return QemuBackend(paths, VmStore(paths), capabilities)


def _runtime(vm_id: str) -> VMRuntime:
    return VMRuntime(vm_id=vm_id, status=VMStatus.RUNNING, pid=1234, qmp_port=4444)


def _spec() -> VMSpec:
    return create_vm_spec(
        name="Media VM",
        guest_os=GuestOS.LINUX,
        host=HostResources(cpu_count=4, memory_total_mib=8192),
        cdrom_iso=ISO,
    )


def test_eject_runs_open_remove_close(backend, monkeypatch):
    fake = FakeQmp(_blocks())
    monkeypatch.setattr(backend, "qmp_client", lambda runtime, timeout=10.0: fake)
    spec = _spec()
    backend.change_medium(spec, _runtime(spec.id), None)
    commands = [command for command, _ in fake.commands]
    assert commands == [
        "query-block",
        "blockdev-open-tray",
        "query-block",
        "blockdev-remove-medium",
        "blockdev-close-tray",
        "query-block",
    ]
    assert fake.closed


def test_locked_medium_requires_force(backend, monkeypatch):
    fake = FakeQmp(_blocks(locked=True))
    monkeypatch.setattr(backend, "qmp_client", lambda runtime, timeout=10.0: fake)
    spec = _spec()
    with pytest.raises(OperationFailedError) as excinfo:
        backend.change_medium(spec, _runtime(spec.id), None)
    assert "force" in (excinfo.value.hint or "")
    assert [command for command, _ in fake.commands] == ["query-block"]

    forced = FakeQmp(_blocks(locked=True))
    monkeypatch.setattr(backend, "qmp_client", lambda runtime, timeout=10.0: forced)
    backend.change_medium(spec, _runtime(spec.id), None, force=True)
    assert any(command == "blockdev-remove-medium" for command, _ in forced.commands)


def test_insert_medium_uses_filename_and_format(backend, monkeypatch):
    fake = FakeQmp(_blocks(inserted=None))
    monkeypatch.setattr(backend, "qmp_client", lambda runtime, timeout=10.0: fake)
    spec = _spec()
    backend.change_medium(spec, _runtime(spec.id), "C:/images/other.iso")
    command, arguments = next(item for item in fake.commands if item[0] == "blockdev-change-medium")
    assert arguments["filename"] == "C:/images/other.iso"
    assert arguments["format"] == "raw"
    assert "force" not in arguments


def test_insert_medium_forwards_force(backend, monkeypatch):
    fake = FakeQmp(_blocks(locked=True))
    monkeypatch.setattr(backend, "qmp_client", lambda runtime, timeout=10.0: fake)
    spec = _spec()
    backend.change_medium(spec, _runtime(spec.id), "C:/images/other.iso", force=True)
    command, arguments = next(item for item in fake.commands if item[0] == "blockdev-change-medium")
    assert arguments["force"] is True


def test_resize_disk_live_verifies_reported_size(backend, monkeypatch):
    fake = FakeQmp(_blocks())
    monkeypatch.setattr(backend, "qmp_client", lambda runtime, timeout=10.0: fake)
    spec = _spec()
    backend.resize_disk_live(spec, _runtime(spec.id), 0, 8 * 1024**3)
    command, arguments = next(item for item in fake.commands if item[0] == "block_resize")
    assert arguments == {"device": "drive0", "size": 8 * 1024**3}
    assert fake.closed


def test_resize_disk_live_rejects_mismatched_result(backend, monkeypatch):
    class StubbornQmp(FakeQmp):
        def execute(self, command, arguments=None):  # type: ignore[no-untyped-def]
            self.commands.append((command, dict(arguments or {})))
            if command == "query-block":
                return copy.deepcopy(self.blocks)
            return {}

    fake = StubbornQmp(_blocks())
    monkeypatch.setattr(backend, "qmp_client", lambda runtime, timeout=10.0: fake)
    spec = _spec()
    with pytest.raises(OperationFailedError, match="did not apply"):
        backend.resize_disk_live(spec, _runtime(spec.id), 0, 8 * 1024**3)
