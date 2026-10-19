"""Manager orchestration using a fake backend and fake disks (no QEMU needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from openqvm.core.cancellation import CancellationToken, cancelled_token
from openqvm.core.errors import CancelledError, ConflictError, InvalidStateError, ValidationError
from openqvm.core.factory import create_vm_spec
from openqvm.core.ids import new_id
from openqvm.core.models import DiskSpec, GuestOS, VMStatus
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
        self.check_failures: set[str] = set()
        self.on_create = None  # callable(path, disk, cancel) invoked before each disk is written

    def create(self, path, disk, *, overwrite: bool = False, cancel=None):
        if cancel is not None:
            cancel.raise_if_cancelled("Disk creation was cancelled.")
        if self.on_create is not None:
            self.on_create(path, disk, cancel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake qcow2")
        self.files[str(path)] = b"fake qcow2"
        return DiskInfo(path, disk.format.value, disk.size_bytes, 1024, None)

    def info(self, path):
        return DiskInfo(path, "qcow2", 20 * 1024**3, 1024, None)

    def resize(self, path, disk, new_size, *, cancel=None):
        return DiskInfo(path, "qcow2", new_size, 2048, None)

    def convert(self, source, destination, *, target_format=None, preallocated=False, cancel=None):
        if cancel is not None:
            cancel.raise_if_cancelled("The disk conversion was cancelled.")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(Path(source).read_bytes())
        return DiskInfo(destination, (target_format.value if target_format else "qcow2"), 20 * 1024**3, 1024, None)

    def delete(self, path):
        Path(path).unlink(missing_ok=True)

    def check(self, path, *, timeout: float = 1800.0, cancel=None):
        from openqvm.storage.disks import DiskCheckResult

        if cancel is not None:
            cancel.raise_if_cancelled("The disk check was cancelled.")
        if path.name in self.check_failures:
            return DiskCheckResult(path=path, ok=False, output="ERROR: image is corrupt")
        return DiskCheckResult(path=path, ok=True, output="No errors were found.")

    def require_qemu_img(self):
        raise RuntimeError("qemu-img is not available in this fake")


class FakeBackend:
    name = "fake"
    capabilities = BackendCapabilities("fake", True, True, True, True, True)

    def __init__(self, store: VmStore) -> None:
        self.store = store
        self.medium_changes: list[tuple[str | None, bool]] = []
        self.live_resizes: list[tuple[int, int]] = []

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

    def change_medium(self, spec, runtime, iso_path, *, force=False):
        self.medium_changes.append((iso_path, force))

    def resize_disk_live(self, spec, runtime, disk_index, new_size_bytes):
        self.live_resizes.append((disk_index, new_size_bytes))

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


def test_events_report_lifecycle_changes(manager):
    from openqvm.core.events import (
        TOPIC_OPERATION_FINISHED,
        TOPIC_VM_CREATED,
        TOPIC_VM_DELETED,
        TOPIC_VM_STATE_CHANGED,
        VmEvent,
        VmStateEvent,
    )

    received: dict[str, list] = {}
    for topic in (TOPIC_VM_CREATED, TOPIC_VM_STATE_CHANGED, TOPIC_VM_DELETED, TOPIC_OPERATION_FINISHED):
        received[topic] = []
        manager.events.subscribe(topic, lambda payload, bucket=received[topic]: bucket.append(payload))

    spec = manager.create_vm(_spec())
    assert isinstance(received[TOPIC_VM_CREATED][0], VmEvent)
    assert received[TOPIC_VM_CREATED][0].vm_id == spec.id

    manager.start_vm(spec.id)
    started = received[TOPIC_VM_STATE_CHANGED][-1]
    assert isinstance(started, VmStateEvent)
    assert started.previous is VMStatus.STOPPED
    assert started.current is VMStatus.RUNNING

    manager.pause_vm(spec.id)
    assert received[TOPIC_VM_STATE_CHANGED][-1].current is VMStatus.PAUSED

    manager.resume_vm(spec.id)
    assert received[TOPIC_VM_STATE_CHANGED][-1].current is VMStatus.RUNNING

    manager.stop_vm(spec.id, force=True)
    assert received[TOPIC_VM_STATE_CHANGED][-1].current is VMStatus.STOPPED

    manager.delete_vm(spec.id)
    assert received[TOPIC_VM_DELETED][-1].vm_id == spec.id
    actions = [record.action for record in received[TOPIC_OPERATION_FINISHED]]
    assert "Start VM" in actions
    assert "Delete VM" in actions
    assert all(record.status.value == "succeeded" for record in received[TOPIC_OPERATION_FINISHED])


def test_check_disks_reports_per_disk_status(manager):
    spec = manager.create_vm(_spec())
    summary = manager.check_disks(spec.id)
    assert summary.ok
    assert len(summary.results) == 1
    assert summary.results[0].ok

    manager.disks.check_failures.add(spec.disks[0].filename)
    failing = manager.check_disks(spec.id)
    assert not failing.ok
    assert "corrupt" in failing.results[0].output
    assert failing.to_dict()["ok"] is False


def test_check_disks_refuses_running_vm(manager):
    spec = manager.create_vm(_spec())
    manager.start_vm(spec.id)
    with pytest.raises(InvalidStateError, match="stopped"):
        manager.check_disks(spec.id)
    manager.stop_vm(spec.id, force=True)


def test_live_resize_uses_backend_and_persists_size(manager):
    spec = manager.create_vm(_spec())
    manager.start_vm(spec.id)
    new_size = spec.disks[0].size_bytes + 1024**3
    with pytest.raises(InvalidStateError, match="live resize"):
        manager.resize_disk(spec.id, spec.disks[0].id, new_size)
    updated = manager.resize_disk(spec.id, spec.disks[0].id, new_size, live=True)
    assert updated.disks[0].size_bytes == new_size
    assert manager.backend.live_resizes == [(0, new_size)]
    reloaded = manager.get_spec(spec.id)
    assert reloaded.disks[0].size_bytes == new_size
    manager.stop_vm(spec.id, force=True)


def test_live_resize_rejects_shrink(manager):
    spec = manager.create_vm(_spec())
    manager.start_vm(spec.id)
    with pytest.raises(ValidationError) as excinfo:
        manager.resize_disk(spec.id, spec.disks[0].id, spec.disks[0].size_bytes - 1024**2, live=True)
    assert any("enlarged" in issue.message for issue in excinfo.value.issues)
    assert manager.backend.live_resizes == []
    manager.stop_vm(spec.id, force=True)


def test_live_iso_change_uses_backend(manager, tmp_path):
    iso = tmp_path / "installer.iso"
    iso.write_bytes(b"x" * 40000)
    spec = manager.create_vm(_spec())
    manager.start_vm(spec.id)

    with pytest.raises(InvalidStateError, match="live change"):
        manager.attach_iso(spec.id, str(iso))

    updated = manager.attach_iso(spec.id, str(iso), live=True)
    assert updated.cdrom_iso == str(iso)
    assert manager.backend.medium_changes[-1] == (str(iso), False)

    ejected = manager.eject_iso(spec.id, live=True, force=True)
    assert ejected.cdrom_iso is None
    assert manager.backend.medium_changes[-1] == (None, True)
    manager.stop_vm(spec.id, force=True)


def test_offline_iso_change_still_works(manager, tmp_path):
    iso = tmp_path / "installer.iso"
    iso.write_bytes(b"x" * 40000)
    spec = manager.create_vm(_spec())
    updated = manager.attach_iso(spec.id, str(iso), live=True)
    assert updated.cdrom_iso == str(iso)
    assert manager.backend.medium_changes == []
    assert updated.boot_order[0].value == "disk"
    assert updated.boot_order[-1].value == "cdrom"
    ejected = manager.eject_iso(spec.id, live=True)
    assert ejected.cdrom_iso is None
    assert all(device.value != "cdrom" for device in ejected.boot_order)
    assert manager.backend.medium_changes == []

# -- cooperative cancellation ------------------------------------------------


class _CancelAfter(CancellationToken):
    """Token that cancels itself on the n-th check so operations stop mid-flight."""

    def __init__(self, checks: int) -> None:
        super().__init__()
        self.remaining = checks

    def raise_if_cancelled(self, message: str = "The operation was cancelled.") -> None:
        self.remaining -= 1
        if self.remaining <= 0:
            self.cancel("cancelled by the test")
        super().raise_if_cancelled(message)


def _spec_with_data_disk():
    spec = _spec()
    disk_id = new_id()
    spec.disks.append(
        DiskSpec(id=disk_id, filename=f"data-{disk_id[:8]}.qcow2", size_bytes=1024**3, role="data")
    )
    return spec


def test_create_stops_between_disks_and_removes_the_vm_directory(manager):
    spec = _spec_with_data_disk()
    token = CancellationToken()

    def cancel_after_first(path, disk, cancel):
        token.cancel("cancelled by the test")

    manager.disks.on_create = cancel_after_first
    with pytest.raises(CancelledError):
        manager.create_vm(spec, cancel=token)

    assert not manager.paths.vm_dir(spec.id).exists()
    summaries, _errors = manager.list_summaries(refresh=True)
    assert summaries == []
    record = manager.recent_operations(1)[0]
    assert (record.action, record.status.value) == ("Create VM", "cancelled")


def test_create_with_a_pre_cancelled_token_does_nothing(manager):
    spec = _spec()
    manager.disks.on_create = lambda *_: pytest.fail("no disk should be created")
    with pytest.raises(CancelledError):
        manager.create_vm(spec, cancel=cancelled_token())
    assert not manager.paths.vm_dir(spec.id).exists()


def test_export_cancelled_midway_leaves_no_package(manager, tmp_path):
    spec = manager.create_vm(_spec())
    output = tmp_path / "package.oqv"

    with pytest.raises(CancelledError):
        manager.export_vm(spec.id, output, cancel=_CancelAfter(2))

    assert not output.exists()
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob("*.oqv")) == []


def test_export_cancelled_during_verification_keeps_the_previous_package(manager, tmp_path):
    spec = manager.create_vm(_spec())
    output = tmp_path / "package.oqv"
    output.write_bytes(b"previous package")

    # The archive is complete and only the hash pass is cancelled.
    with pytest.raises(CancelledError):
        manager.export_vm(spec.id, output, overwrite=True, cancel=_CancelAfter(4))

    assert output.read_bytes() == b"previous package"


def test_import_cancelled_at_any_point_keeps_no_vm_and_no_staging(manager, tmp_path):
    source = manager.create_vm(_spec())
    package = tmp_path / "package.oqv"
    manager.export_vm(source.id, package)

    for checks in range(1, 7):
        with pytest.raises(CancelledError):
            manager.import_vm(package, cancel=_CancelAfter(checks))
        summaries, _errors = manager.list_summaries(refresh=True)
        assert [summary.spec.id for summary in summaries] == [source.id]
        assert list(manager.paths.vms_dir.glob(".import-*")) == []


def test_check_disks_propagates_cancellation_instead_of_reporting_a_failure(manager):
    spec = manager.create_vm(_spec())
    with pytest.raises(CancelledError):
        manager.check_disks(spec.id, cancel=cancelled_token())
    summaries, _errors = manager.list_summaries(refresh=True)
    assert len(summaries) == 1

# -- operation lifecycle and progress events ---------------------------------


def _collect(manager, topic):
    received: list = []
    manager.events.subscribe(topic, received.append)
    return received


def test_create_publishes_started_progress_and_finished(manager):
    from openqvm.core.events import (
        TOPIC_OPERATION_FINISHED,
        TOPIC_OPERATION_PROGRESS,
        TOPIC_OPERATION_STARTED,
    )

    started = _collect(manager, TOPIC_OPERATION_STARTED)
    finished = _collect(manager, TOPIC_OPERATION_FINISHED)
    progress = _collect(manager, TOPIC_OPERATION_PROGRESS)

    manager.create_vm(_spec())

    assert [record.status.value for record in started] == ["running"]
    assert [record.status.value for record in finished] == ["succeeded"]
    assert [event.fraction for event in progress] == [1.0]
    assert progress[0].action == "Create VM"


def test_failed_create_still_publishes_a_finished_event(manager):
    from openqvm.core.events import TOPIC_OPERATION_FINISHED

    finished = _collect(manager, TOPIC_OPERATION_FINISHED)
    manager.create_vm(_spec())
    with pytest.raises(ValidationError):
        manager.create_vm(_spec())
    assert [record.status.value for record in finished] == ["succeeded", "failed"]
    assert finished[-1].error_code == "validation_failed"


def test_export_reports_monotonic_progress(manager, tmp_path):
    from openqvm.core.events import TOPIC_OPERATION_PROGRESS

    spec = manager.create_vm(_spec())
    events = _collect(manager, TOPIC_OPERATION_PROGRESS)
    manager.export_vm(spec.id, tmp_path / "package.oqv")

    assert events[0].fraction is None, "the first report is the indeterminate inspection phase"
    fractions = [event.fraction for event in events if event.fraction is not None]
    assert fractions == sorted(fractions)
    assert fractions[-1] == 1.0
    assert any(event.message.startswith("Writing") for event in events)
    assert events[-1].message == "Package verified"


def test_import_reports_progress_until_registration(manager, tmp_path):
    from openqvm.core.events import TOPIC_OPERATION_PROGRESS

    spec = manager.create_vm(_spec())
    package = tmp_path / "package.oqv"
    manager.export_vm(spec.id, package)

    events = _collect(manager, TOPIC_OPERATION_PROGRESS)
    manager.import_vm(package)

    fractions = [event.fraction for event in events if event.fraction is not None]
    assert fractions == sorted(fractions)
    assert fractions[-1] == 1.0
    assert events[-1].message == "Registering the virtual machine"


def test_clone_reports_per_disk_progress(manager):
    from openqvm.core.events import TOPIC_OPERATION_PROGRESS

    spec = manager.create_vm(_spec())
    events = _collect(manager, TOPIC_OPERATION_PROGRESS)
    manager.clone_vm(spec.id, "Managed VM clone")

    assert [event.fraction for event in events] == [0.0, 1.0]
    assert events[-1].message.startswith("Copied")


def test_caller_progress_callbacks_still_receive_updates(manager):
    spec = manager.create_vm(_spec())
    seen: list[float | None] = []
    manager.clone_vm(
        spec.id, "Managed VM clone", progress=lambda fraction, _message: seen.append(fraction)
    )
    assert seen == [0.0, 1.0]

# -- backups -----------------------------------------------------------------


def _backup_policy(tmp_path, **overrides):
    from openqvm.services.backups import BackupPolicy

    values = {"directory": tmp_path / "backups", "enabled": True, "interval_hours": 24, "keep": 3}
    values.update(overrides)
    return BackupPolicy(**values)


def test_backup_creates_a_package_and_index_entry(manager, tmp_path):
    from openqvm.core.events import TOPIC_BACKUPS_CHANGED

    spec = manager.create_vm(_spec())
    events = _collect(manager, TOPIC_BACKUPS_CHANGED)

    result = manager.backup_vm(spec.id, destination=tmp_path / "backups", keep=3)

    assert result.path.is_file()
    assert result.path.parent == tmp_path / "backups"
    entries = manager.list_backups(spec.id)
    assert [entry.path for entry in entries] == [str(result.path)]
    assert entries[0].vm_id == spec.id
    assert entries[0].size_bytes > 0
    assert entries[0].reason == "manual"
    assert len(entries[0].sha256) == 64
    assert events[-1].reason == "created"
    assert events[-1].path == str(result.path)


def test_backup_retention_removes_old_packages(manager, tmp_path):
    manager.disks.on_create = None
    spec = manager.create_vm(_spec())
    backups = tmp_path / "backups"

    first = manager.backup_vm(spec.id, destination=backups, keep=2)
    second = manager.backup_vm(spec.id, destination=backups, keep=2)
    third = manager.backup_vm(spec.id, destination=backups, keep=2)

    assert not first.path.exists()
    assert second.path.exists() and third.path.exists()
    assert third.pruned == [str(first.path)]
    assert [entry.path for entry in manager.list_backups(spec.id)] == [
        str(third.path),
        str(second.path),
    ]


def test_backup_refuses_a_running_vm(manager, tmp_path):
    spec = manager.create_vm(_spec())
    manager.start_vm(spec.id)
    with pytest.raises(InvalidStateError, match="running"):
        manager.backup_vm(spec.id, destination=tmp_path / "backups")
    assert manager.list_backups(spec.id) == []
    manager.stop_vm(spec.id, force=True)


def test_cancelled_backup_leaves_no_package_or_entry(manager, tmp_path):
    spec = manager.create_vm(_spec())
    backups = tmp_path / "backups"
    with pytest.raises(CancelledError):
        manager.backup_vm(spec.id, destination=backups, cancel=cancelled_token())
    assert list(backups.glob("*.oqv")) == []
    assert manager.list_backups(spec.id) == []


def test_scheduled_backups_only_create_due_ones(manager, tmp_path):
    from datetime import UTC, datetime, timedelta

    spec = manager.create_vm(_spec())
    policy = _backup_policy(tmp_path)
    now = datetime.now(UTC)

    first = manager.run_scheduled_backups(policy, now=now)
    assert first.created == 1
    assert first.errors == []
    assert first.results[0].entry.reason == "scheduled"
    assert first.results[0].path.is_file()

    again = manager.run_scheduled_backups(policy, now=now + timedelta(hours=1))
    assert again.created == 0
    assert again.errors == []

    later = manager.run_scheduled_backups(policy, now=now + timedelta(hours=25))
    assert later.created == 1
    assert len(manager.list_backups(spec.id)) == 2


def test_scheduled_backups_skip_running_vms_and_disabled_policies(manager, tmp_path):
    spec = manager.create_vm(_spec())
    manager.start_vm(spec.id)
    run = manager.run_scheduled_backups(_backup_policy(tmp_path))
    assert run.created == 0
    assert run.skipped == [spec.name]
    manager.stop_vm(spec.id, force=True)

    disabled = _backup_policy(tmp_path, enabled=False)
    assert manager.run_scheduled_backups(disabled).created == 0
    assert manager.list_backups(spec.id) == []


def test_backup_operations_publish_lifecycle_events(manager, tmp_path):
    from openqvm.core.events import TOPIC_OPERATION_FINISHED, TOPIC_OPERATION_STARTED

    spec = manager.create_vm(_spec())
    started = _collect(manager, TOPIC_OPERATION_STARTED)
    finished = _collect(manager, TOPIC_OPERATION_FINISHED)
    manager.backup_vm(spec.id, destination=tmp_path / "backups")

    assert started[-1].action == "Back up VM"
    assert finished[-1].action == "Back up VM"
    assert finished[-1].status.value == "succeeded"


def test_corrupt_backup_index_is_reported(manager):
    from openqvm.core.errors import CorruptDataError

    spec = manager.create_vm(_spec())
    manager.paths.vm_backups_file(spec.id).write_text("{not json", encoding="utf-8")
    with pytest.raises(CorruptDataError):
        manager.list_backups(spec.id)
