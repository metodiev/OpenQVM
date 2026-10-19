"""Manager orchestration using a fake backend and fake disks (no QEMU needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from openqvm.core.errors import ConflictError, InvalidStateError
from openqvm.core.factory import create_vm_spec
from openqvm.core.models import GuestOS, VMStatus
from openqvm.core.paths import AppPaths
from openqvm.core.validation import HostResources
from openqvm.platform.capabilities import AcceleratorInfo, PlatformCapabilities
from openqvm.platform.qemu_discovery import QemuInstall
from openqvm.services.operations import OperationLog
from openqvm.storage.disks import DiskInfo
from openqvm.store import VmStore
from openqvm.vm.backend import BackendCapabilities
from openqvm.vm.manager import VmManager


class FakeDisks:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def create(self, path, disk, *, overwrite: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake qcow2")
        self.files[str(path)] = b"fake qcow2"
        return DiskInfo(path, disk.format.value, disk.size_bytes, 1024, None)

    def info(self, path):
        return DiskInfo(path, "qcow2", 20 * 1024**3, 1024, None)

    def resize(self, path, disk, new_size):
        return DiskInfo(path, "qcow2", new_size, 2048, None)

    def delete(self, path):
        Path(path).unlink(missing_ok=True)

    def require_qemu_img(self):
        raise RuntimeError("qemu-img is not available in this fake")


class FakeBackend:
    name = "fake"
    capabilities = BackendCapabilities("fake", True, True, True, False, False)

    def __init__(self, store: VmStore) -> None:
        self.store = store

    def start(self, spec, runtime):
        runtime.status = VMStatus.RUNNING
        runtime.pid = 4242
        runtime.qmp_port = 5000
        runtime.vnc_port = 5910
        runtime.accelerator = "tcg"
        self.store.save_runtime(runtime)
        return runtime

    def stop(self, spec, runtime, *, force=False, timeout=30.0):
        runtime.status = VMStatus.STOPPED
        runtime.pid = None
        runtime.qmp_port = None
        runtime.vnc_port = None
        self.store.save_runtime(runtime)
        return runtime

    def pause(self, spec, runtime):
        runtime.status = VMStatus.PAUSED
        self.store.save_runtime(runtime)
        return runtime

    def resume(self, spec, runtime):
        runtime.status = VMStatus.RUNNING
        self.store.save_runtime(runtime)
        return runtime

    def refresh(self, spec, runtime):
        return runtime

    def qmp_client(self, runtime, timeout=10.0):
        raise RuntimeError("no QMP in fake backend")


@pytest.fixture
def manager(tmp_path):
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    qemu = tmp_path / "qemu"
    qemu.mkdir()
    system = qemu / "qemu-system-x86_64"
    image = qemu / "qemu-img"
    system.write_bytes(b"")
    image.write_bytes(b"")
    install = QemuInstall(system, image, "11.0.0", ("tcg",), qemu, str(qemu))
    capabilities = PlatformCapabilities(
        os_family="linux",
        architecture="x86_64",
        qemu=install,
        accelerators=[AcceleratorInfo("tcg", True)],
        default_accelerator="tcg",
        bridged_supported=False,
        host_only_supported=False,
        shared_folders_supported=False,
    )
    store = VmStore(paths)
    manager = VmManager(
        paths,
        capabilities,
        store=store,
        backend=FakeBackend(store),
        disks=FakeDisks(),
        operations=OperationLog(paths.operations_log),
        host=HostResources(cpu_count=8, memory_total_mib=16384),
    )
    yield manager
    # No real processes are started by the fake backend.


def _spec(host=None):
    return create_vm_spec(
        name="Managed VM",
        guest_os=GuestOS.LINUX,
        host=HostResources(cpu_count=8, memory_total_mib=16384),
        vcpus=2,
        memory_mib=1024,
        disk_bytes=1 * 1024**3,
    )


def test_create_start_pause_resume_stop(manager):
    spec = manager.create_vm(_spec())
    assert manager.store.disk_path(spec, spec.disks[0]).is_file()

    runtime = manager.start_vm(spec.id)
    assert runtime.status is VMStatus.RUNNING
    assert runtime.pid == 4242

    assert manager.pause_vm(spec.id).status is VMStatus.PAUSED
    assert manager.resume_vm(spec.id).status is VMStatus.RUNNING
    assert manager.stop_vm(spec.id, force=True).status is VMStatus.STOPPED


def test_duplicate_names_are_rejected(manager):
    manager.create_vm(_spec())
    duplicate = _spec()
    duplicate.name = "Managed VM"
    with pytest.raises(Exception, match="already named"):
        manager.create_vm(duplicate)


def test_hardware_changes_require_stop(manager):
    spec = manager.create_vm(_spec())
    manager.start_vm(spec.id)
    changed = manager.get_spec(spec.id)
    changed.memory_mib = 2048
    with pytest.raises(InvalidStateError, match="Hardware settings"):
        manager.update_vm(changed, expected_revision=changed.revision)
    manager.rename_vm(spec.id, "Renamed while running")
    assert manager.get_spec(spec.id).name == "Renamed while running"


def test_optimistic_concurrency_conflict(manager):
    spec = manager.create_vm(_spec())
    stale = manager.get_spec(spec.id)
    fresh = manager.get_spec(spec.id)
    fresh.name = "First rename"
    manager.update_vm(fresh, expected_revision=fresh.revision)
    stale.name = "Stale rename"
    with pytest.raises(ConflictError):
        manager.update_vm(stale, expected_revision=stale.revision)


def test_delete_requires_stopped_vm(manager):
    spec = manager.create_vm(_spec())
    manager.start_vm(spec.id)
    with pytest.raises(InvalidStateError):
        manager.delete_vm(spec.id)
    manager.stop_vm(spec.id, force=True)
    result = manager.delete_vm(spec.id)
    assert result.quarantine_path.is_dir()
    assert not manager.paths.vm_dir(spec.id).exists()


def test_invalid_resources_are_rejected_before_disk_creation(manager):
    spec = _spec()
    spec.memory_mib = 1024 * 1024 * 4
    with pytest.raises(Exception, match="not valid"):
        manager.create_vm(spec)
    assert not manager.paths.vm_dir(spec.id).exists()
