"""High-level VM orchestration.

``VmManager`` implements the operations exposed by the GUI and CLI by combining
the backend, persistent store, disk manager, snapshot service and transfer
service.  Every mutation is serialized per VM with an advisory lock.
"""

from __future__ import annotations

import functools
import shutil
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from openqvm.core.errors import (
    ConflictError,
    InvalidStateError,
    OpenQVMError,
    UnsupportedFeatureError,
    ValidationError,
    ValidationIssue,
    error_from_exception,
    raise_if_errors,
)
from openqvm.core.events import (
    TOPIC_OPERATION_FINISHED,
    TOPIC_SNAPSHOTS_CHANGED,
    TOPIC_VM_CREATED,
    TOPIC_VM_DELETED,
    TOPIC_VM_STATE_CHANGED,
    TOPIC_VM_UPDATED,
    EventBus,
    SnapshotEvent,
    VmEvent,
    VmStateEvent,
)
from openqvm.core.logging import get_logger
from openqvm.core.models import (
    DiskBus,
    DiskFormat,
    DiskSpec,
    PortForward,
    SnapshotRecord,
    VMRuntime,
    VMSpec,
    VMStatus,
)
from openqvm.core.paths import AppPaths
from openqvm.core.validation import (
    HostResources,
    validate_disk_resize,
    validate_snapshot_name,
    validate_vm_name,
    validate_vm_spec,
)
from openqvm.networking import security_note
from openqvm.platform.capabilities import PlatformCapabilities
from openqvm.platform.compatibility import check_compatibility
from openqvm.services.operations import OperationLog
from openqvm.snapshots import SnapshotService, SnapshotTagInfo
from openqvm.storage.disks import DiskCheckResult, DiskManager
from openqvm.storage.iso import inspect_iso
from openqvm.storage.transfer import ExportResult, ImportResult, VmTransferService
from openqvm.store import VmStore
from openqvm.vm.qemu.backend import QemuBackend

log = get_logger("vm.manager")
T = TypeVar("T")


@dataclass(frozen=True)
class VmSummary:
    spec: VMSpec
    runtime: VMRuntime

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def status(self) -> VMStatus:
        return self.runtime.status

    def to_dict(self) -> dict[str, Any]:
        return {"spec": self.spec.to_dict(), "runtime": self.runtime.to_dict()}


@dataclass(frozen=True)
class SnapshotView:
    name: str
    description: str
    created_at: str
    includes_memory: bool
    vm_state: str
    present: bool

    @property
    def restore_requires_stop(self) -> bool:
        return (not self.includes_memory) and self.vm_state in {"running", "paused"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "includes_memory": self.includes_memory,
            "vm_state": self.vm_state,
            "present": self.present,
            "restore_requires_stop": self.restore_requires_stop,
        }


@dataclass(frozen=True)
class CloneResult:
    spec: VMSpec
    warnings: list[str]


@dataclass(frozen=True)
class DeleteResult:
    vm_id: str
    quarantine_path: Path


@dataclass(frozen=True)
class DiskCheckSummary:
    vm_id: str
    results: list[DiskCheckResult]

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "vm_id": self.vm_id,
            "ok": self.ok,
            "disks": [result.to_dict() for result in self.results],
        }


def _recorded(action: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def decorator(function: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(function)
        def wrapper(self: VmManager, vm_id: str, *args: Any, **kwargs: Any) -> T:
            record = self.operations.start(action, vm_id=vm_id, vm_name=self._vm_name_safe(vm_id))
            try:
                result = function(self, vm_id, *args, **kwargs)
            except OpenQVMError as exc:
                failed = self.operations.fail(record, exc)
                self.events.publish(TOPIC_OPERATION_FINISHED, failed)
                raise
            except Exception as exc:  # pragma: no cover - defensive
                normalized = error_from_exception(exc, default_message=f"{action} failed.")
                failed = self.operations.fail(record, normalized)
                self.events.publish(TOPIC_OPERATION_FINISHED, failed)
                raise normalized from exc
            finished = self.operations.succeed(record)
            self.events.publish(TOPIC_OPERATION_FINISHED, finished)
            return result

        return wrapper

    return decorator


class VmManager:
    def __init__(
        self,
        paths: AppPaths,
        capabilities: PlatformCapabilities,
        *,
        store: VmStore | None = None,
        backend: QemuBackend | None = None,
        disks: DiskManager | None = None,
        operations: OperationLog | None = None,
        host: HostResources | None = None,
        events: EventBus | None = None,
    ) -> None:
        self.paths = paths
        self.capabilities = capabilities
        self.store = store or VmStore(paths)
        self.operations = operations or OperationLog(paths.operations_log)
        self.events = events or EventBus()
        self.host = host or HostResources.detect()
        self._create_lock = threading.Lock()
        self._configure_components(disks=disks, backend=backend)

    def _publish_state(
        self, vm_id: str, name: str, previous: VMStatus, runtime: VMRuntime
    ) -> None:
        if previous is runtime.status:
            return
        self.events.publish(
            TOPIC_VM_STATE_CHANGED,
            VmStateEvent(
                vm_id=vm_id,
                name=name,
                previous=previous,
                current=runtime.status,
                runtime=runtime,
            ),
        )

    def _publish_vm(self, topic: str, vm_id: str, *, name: str = "", reason: str = "") -> None:
        self.events.publish(topic, VmEvent(vm_id=vm_id, name=name, reason=reason))

    def _publish_snapshots(self, vm_id: str, *, name: str = "", reason: str = "") -> None:
        self.events.publish(TOPIC_SNAPSHOTS_CHANGED, SnapshotEvent(vm_id=vm_id, name=name, reason=reason))

    @staticmethod
    def _vm_busy(runtime: VMRuntime) -> bool:
        """True when a QEMU process may still be using this VM's disks."""

        return runtime.status.is_active or (runtime.pid is not None and runtime.status is not VMStatus.STOPPED)

    def _configure_components(self, *, disks: DiskManager | None = None, backend: QemuBackend | None = None) -> None:
        self.disks = disks or DiskManager(self.capabilities.qemu.image_path if self.capabilities.qemu else None)
        self.backend = backend or QemuBackend(self.paths, self.store, self.capabilities)
        self.snapshots = SnapshotService(self.store, self.disks)
        self.transfer = VmTransferService(self.paths, self.store, self.disks)

    def reconfigure(self, capabilities: PlatformCapabilities) -> None:
        """Apply a new platform detection result (for example after changing QEMU settings)."""

        self.capabilities = capabilities
        self._configure_components()

    # -- validation -----------------------------------------------------------

    def validate_spec(self, spec: VMSpec, *, exclude_vm_id: str | None = None) -> list[ValidationIssue]:
        issues = validate_vm_spec(
            spec,
            host=self.host,
            existing_specs=self.store.list_specs(),
            exclude_vm_id=exclude_vm_id,
        )
        issues.extend(check_compatibility(spec, self.capabilities))
        if spec.cdrom_iso:
            try:
                info = inspect_iso(spec.cdrom_iso)
            except ValidationError as exc:
                issues.append(ValidationIssue("cdrom_iso", str(exc)))
            else:
                if not info.is_iso9660:
                    issues.append(
                        ValidationIssue(
                            "cdrom_iso",
                            "The image does not contain an ISO-9660 volume descriptor. "
                            "It may still boot, but verify that it is a bootable image.",
                            severity="warning",
                        )
                    )
        return issues

    def _raise_for_issues(self, spec: VMSpec, *, exclude_vm_id: str | None = None) -> None:
        raise_if_errors(
            self.validate_spec(spec, exclude_vm_id=exclude_vm_id),
            message="The virtual machine configuration is not valid.",
        )

    # -- lookup ---------------------------------------------------------------

    def _require_spec(self, vm_id: str) -> VMSpec:
        return self.store.load_spec(vm_id)

    def _vm_name_safe(self, vm_id: str) -> str:
        try:
            return self.store.load_spec(vm_id).name
        except Exception:
            return vm_id

    def get_spec(self, vm_id: str) -> VMSpec:
        return self.store.load_spec(vm_id)

    def get_runtime(self, vm_id: str) -> VMRuntime:
        return self.store.load_runtime(vm_id)

    def get_summary(self, vm_id: str, *, refresh: bool = True) -> VmSummary:
        spec = self.store.load_spec(vm_id)
        runtime = self.store.load_runtime(vm_id)
        if refresh and (runtime.status.is_active or runtime.pid is not None):
            with suppress(ConflictError):
                with self.store.lock(vm_id, timeout=0.2):
                    runtime = self.store.load_runtime(vm_id)
                    runtime = self.backend.refresh(spec, runtime)
        return VmSummary(spec=spec, runtime=runtime)

    def list_summaries(self, *, refresh: bool = True) -> tuple[list[VmSummary], list[str]]:
        scan = self.store.scan()
        errors = [error.as_text() for error in scan.errors]
        summaries: list[VmSummary] = []
        for spec in scan.specs:
            try:
                runtime = self.store.load_runtime(spec.id)
                if refresh and (runtime.status.is_active or runtime.pid is not None):
                    with suppress(ConflictError):
                        with self.store.lock(spec.id, timeout=0.2):
                            runtime = self.store.load_runtime(spec.id)
                            runtime = self.backend.refresh(spec, runtime)
                summaries.append(VmSummary(spec=spec, runtime=runtime))
            except OpenQVMError as exc:
                errors.append(f"{spec.name}: {exc.message}")
        return summaries, errors

    def reconcile(self) -> list[str]:
        """Synchronize persisted runtime state with real QEMU processes at startup."""

        errors: list[str] = []
        for spec in self.store.scan().specs:
            try:
                runtime = self.store.load_runtime(spec.id)
                if runtime.status.is_active or runtime.pid is not None:
                    with self.store.lock(spec.id, timeout=2.0):
                        runtime = self.store.load_runtime(spec.id)
                        self.backend.refresh(spec, runtime)
            except OpenQVMError as exc:
                errors.append(f"{spec.name}: {exc.message}")
        return errors

    # -- create / update / delete --------------------------------------------

    def create_vm(self, spec: VMSpec) -> VMSpec:
        record = self.operations.start("Create VM", vm_id=spec.id, vm_name=spec.name)
        try:
            with self._create_lock:
                self._raise_for_issues(spec)
                vm_dir = self.store.create_vm_dir(spec.id)
                try:
                    for disk in spec.disks:
                        self.disks.create(self.store.disk_path(spec, disk), disk)
                    self.store.save_spec(spec)
                    self.store.save_runtime(VMRuntime(vm_id=spec.id))
                except Exception:
                    shutil.rmtree(vm_dir, ignore_errors=True)
                    raise
            self.operations.succeed(record)
            log.info("Created VM '%s' (%s)", spec.name, spec.id)
            self._publish_vm(TOPIC_VM_CREATED, spec.id, name=spec.name, reason="created")
            return spec
        except OpenQVMError as exc:
            self.operations.fail(record, exc)
            raise
        except Exception as exc:  # pragma: no cover - defensive
            normalized = error_from_exception(exc, default_message="The virtual machine could not be created.")
            self.operations.fail(record, normalized)
            raise normalized from exc

    def update_vm(self, spec: VMSpec, *, expected_revision: int | None = None) -> VMSpec:
        with self.store.lock(spec.id, timeout=30):
            existing = self.store.load_spec(spec.id)
            if expected_revision is not None and existing.revision != expected_revision:
                raise ConflictError(
                    "The virtual machine was modified by another OpenQVM window.",
                    hint="Reload the configuration and try again.",
                    details={"expected": expected_revision, "actual": existing.revision},
                )
            runtime = self.store.load_runtime(spec.id)
            runtime = self.backend.refresh(existing, runtime)
            if self._vm_busy(runtime):
                hardware_changed = (
                    existing.cpu != spec.cpu
                    or existing.memory_mib != spec.memory_mib
                    or existing.disks != spec.disks
                    or existing.nics != spec.nics
                    or existing.firmware != spec.firmware
                    or existing.machine != spec.machine
                    or existing.cdrom_iso != spec.cdrom_iso
                    or existing.shared_folders != spec.shared_folders
                    or existing.display != spec.display
                )
                if hardware_changed:
                    raise InvalidStateError(
                        "Hardware settings cannot be changed while the virtual machine is running.",
                        hint="Shut down the VM, apply the changes, then start it again.",
                    )
            self._raise_for_issues(spec, exclude_vm_id=spec.id)
            spec.touch()
            self.store.save_spec(spec)
            log.info("Updated VM '%s' (%s)", spec.name, spec.id)
            self._publish_vm(TOPIC_VM_UPDATED, spec.id, name=spec.name, reason="updated")
            return spec

    def rename_vm(self, vm_id: str, new_name: str) -> VMSpec:
        issues = validate_vm_name(new_name)
        raise_if_errors(issues, message="The new name is not valid.")
        with self.store.lock(vm_id, timeout=30):
            spec = self.store.load_spec(vm_id)
            spec.name = new_name
            self._raise_for_issues(spec, exclude_vm_id=vm_id)
            spec.touch()
            self.store.save_spec(spec)
            self._publish_vm(TOPIC_VM_UPDATED, spec.id, name=spec.name, reason="renamed")
            return spec

    @_recorded("Delete VM")
    def delete_vm(self, vm_id: str) -> DeleteResult:
        with self.store.lock(vm_id, timeout=30):
            spec = self.store.load_spec(vm_id)
            runtime = self.store.load_runtime(vm_id)
            if runtime.status.is_active or runtime.pid is not None:
                runtime = self.backend.refresh(spec, runtime)
                if self._vm_busy(runtime):
                    raise InvalidStateError(
                        "A running virtual machine cannot be deleted.",
                        hint="Stop the VM before deleting it.",
                    )
            quarantine = self.store.quarantine_vm(vm_id)
        with suppress(OSError):
            self.paths.vm_lock_file(vm_id).unlink()
        self._publish_vm(TOPIC_VM_DELETED, vm_id, name=spec.name, reason="deleted")
        return DeleteResult(vm_id=vm_id, quarantine_path=quarantine)

    def purge_trash(self) -> int:
        return self.store.purge_trash()

    # -- lifecycle ------------------------------------------------------------

    @_recorded("Start VM")
    def start_vm(self, vm_id: str) -> VMRuntime:
        with self.store.lock(vm_id, timeout=30):
            spec = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(spec, self.store.load_runtime(vm_id))
            if self._vm_busy(runtime):
                raise ConflictError(
                    "The virtual machine appears to be running.",
                    hint="If it is unresponsive, use force stop before starting it again.",
                )
            self._raise_for_issues(spec, exclude_vm_id=vm_id)
            previous = runtime.status
            try:
                started = self.backend.start(spec, runtime)
            except OpenQVMError:
                with suppress(OpenQVMError):
                    self._publish_state(vm_id, spec.name, previous, self.store.load_runtime(vm_id))
                raise
            self._publish_state(vm_id, spec.name, previous, started)
            return started

    @_recorded("Stop VM")
    def stop_vm(self, vm_id: str, *, force: bool = False, timeout: float = 30.0) -> VMRuntime:
        with self.store.lock(vm_id, timeout=30):
            spec = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(spec, self.store.load_runtime(vm_id))
            if runtime.status is VMStatus.STOPPED and runtime.pid is None:
                return runtime
            previous = runtime.status
            stopped = self.backend.stop(spec, runtime, force=force, timeout=timeout)
            self._publish_state(vm_id, spec.name, previous, stopped)
            return stopped

    @_recorded("Pause VM")
    def pause_vm(self, vm_id: str) -> VMRuntime:
        with self.store.lock(vm_id, timeout=30):
            spec = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(spec, self.store.load_runtime(vm_id))
            previous = runtime.status
            paused = self.backend.pause(spec, runtime)
            self._publish_state(vm_id, spec.name, previous, paused)
            return paused

    @_recorded("Resume VM")
    def resume_vm(self, vm_id: str) -> VMRuntime:
        with self.store.lock(vm_id, timeout=30):
            spec = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(spec, self.store.load_runtime(vm_id))
            previous = runtime.status
            resumed = self.backend.resume(spec, runtime)
            self._publish_state(vm_id, spec.name, previous, resumed)
            return resumed

    def refresh_status(self, vm_id: str) -> VMRuntime:
        with self.store.lock(vm_id, timeout=10):
            spec = self.store.load_spec(vm_id)
            runtime = self.store.load_runtime(vm_id)
            previous = runtime.status
            refreshed = self.backend.refresh(spec, runtime)
        self._publish_state(vm_id, spec.name, previous, refreshed)
        return refreshed

    # -- storage --------------------------------------------------------------

    @_recorded("Resize disk")
    def resize_disk(self, vm_id: str, disk_id: str, new_size_bytes: int, *, live: bool = False) -> VMSpec:
        with self.store.lock(vm_id, timeout=60):
            spec = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(spec, self.store.load_runtime(vm_id))
            disk = spec.disk_by_id(disk_id)
            if disk is None:
                raise ValidationError("The selected disk was not found.")
            issues = validate_disk_resize(disk, new_size_bytes)
            if issues:
                raise ValidationError("The new disk size is not valid.", issues=issues)
            busy = self._vm_busy(runtime)
            if busy and not live:
                raise InvalidStateError(
                    "A disk attached to a running virtual machine can only be grown with a live resize.",
                    hint="Shut down the VM, retry with live resize, or use 'openqvm-cli resize --live'.",
                )
            if busy:
                if not self.backend.capabilities.supports_online_disk_resize:
                    raise UnsupportedFeatureError(
                        "This backend cannot resize disks while the VM is running.",
                        hint="Shut down the VM and resize the disk offline.",
                    )
                index = next(
                    position for position, item in enumerate(spec.disks) if item.id == disk_id
                )
                self.backend.resize_disk_live(spec, runtime, index, new_size_bytes)
                reason = "disk_resized_live"
            else:
                self.disks.resize(self.store.disk_path(spec, disk), disk, new_size_bytes)
                reason = "disk_resized"
            updated = replace(disk, size_bytes=new_size_bytes)
            spec.disks = [updated if item.id == disk_id else item for item in spec.disks]
            spec.touch()
            try:
                self.store.save_spec(spec)
            except Exception:
                log.exception("Disk %s was resized but the VM configuration could not be saved", disk.filename)
                raise
            self._publish_vm(TOPIC_VM_UPDATED, spec.id, name=spec.name, reason=reason)
            return spec

    @_recorded("Add disk")
    def add_disk(
        self,
        vm_id: str,
        *,
        size_bytes: int,
        disk_format: DiskFormat = DiskFormat.QCOW2,
        bus: DiskBus = DiskBus.VIRTIO,
        preallocated: bool = False,
    ) -> VMSpec:
        with self.store.lock(vm_id, timeout=30):
            spec = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(spec, self.store.load_runtime(vm_id))
            if self._vm_busy(runtime):
                raise InvalidStateError("Disks cannot be added to a running virtual machine.")
            from openqvm.core.ids import new_id

            disk_id = new_id()
            new_disk = DiskSpec(
                id=disk_id,
                filename=f"data-{disk_id[:8]}.{disk_format.value}",
                format=disk_format,
                bus=bus,
                size_bytes=size_bytes,
                preallocated=preallocated,
                role="data",
            )
            candidate = replace(spec, disks=[*spec.disks, new_disk])
            self._raise_for_issues(candidate, exclude_vm_id=vm_id)
            path = self.store.disk_path(spec, new_disk)
            self.disks.create(path, new_disk)
            try:
                spec.disks.append(new_disk)
                spec.touch()
                self.store.save_spec(spec)
            except Exception:
                self.disks.delete(path)
                raise
            self._publish_vm(TOPIC_VM_UPDATED, spec.id, name=spec.name, reason="disk_added")
            return spec

    @_recorded("Remove disk")
    def remove_disk(self, vm_id: str, disk_id: str) -> VMSpec:
        with self.store.lock(vm_id, timeout=30):
            spec = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(spec, self.store.load_runtime(vm_id))
            if self._vm_busy(runtime):
                raise InvalidStateError("Disks cannot be removed from a running virtual machine.")
            disk = spec.disk_by_id(disk_id)
            if disk is None:
                raise ValidationError("The selected disk was not found.")
            if disk.role == "system":
                raise ValidationError(
                    "The system disk cannot be removed.",
                    hint="Delete the virtual machine instead, or attach a data disk.",
                )
            path = self.store.disk_path(spec, disk)
            quarantine = self.paths.vm_disk_dir(vm_id) / "deleted"
            quarantine.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            moved_to: Path | None = None
            if path.is_file():
                moved_to = quarantine / f"{stamp}-{disk.filename}"
                shutil.move(str(path), str(moved_to))
            try:
                spec.disks = [item for item in spec.disks if item.id != disk_id]
                spec.touch()
                self.store.save_spec(spec)
            except Exception:
                if moved_to is not None and moved_to.is_file():
                    shutil.move(str(moved_to), str(path))
                raise
            self._publish_vm(TOPIC_VM_UPDATED, spec.id, name=spec.name, reason="disk_removed")
            return spec

    @_recorded("Attach ISO")
    def attach_iso(self, vm_id: str, iso_path: str, *, live: bool = False, force: bool = False) -> VMSpec:
        with self.store.lock(vm_id, timeout=60):
            spec = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(spec, self.store.load_runtime(vm_id))
            info = inspect_iso(iso_path)
            from openqvm.core.models import BootDevice

            boot_order = list(spec.boot_order)
            if BootDevice.CDROM not in boot_order:
                # Append rather than prepend: an ISO attached to an existing VM
                # should not silently become the first boot device.  An empty or
                # unbootable disk still falls through to the CD-ROM.
                boot_order = [*boot_order, BootDevice.CDROM]
            candidate = replace(spec, cdrom_iso=str(info.path), boot_order=boot_order)
            self._raise_for_issues(candidate, exclude_vm_id=vm_id)
            busy = self._vm_busy(runtime)
            if busy and not live:
                raise InvalidStateError(
                    "Installation images can only be attached to a stopped VM without live change.",
                    hint="Shut down the VM, or retry with live change.",
                )
            if busy:
                if not self.backend.capabilities.supports_live_iso_change:
                    raise UnsupportedFeatureError(
                        "This backend cannot change the installation medium while the VM is running.",
                        hint="Shut down the VM and attach the ISO.",
                    )
                self.backend.change_medium(spec, runtime, str(info.path), force=force)
                reason = "iso_attached_live"
            else:
                reason = "iso_attached"
            spec.cdrom_iso = str(info.path)
            spec.boot_order = boot_order
            spec.touch()
            self.store.save_spec(spec)
            self._publish_vm(TOPIC_VM_UPDATED, spec.id, name=spec.name, reason=reason)
            return spec

    @_recorded("Eject ISO")
    def eject_iso(self, vm_id: str, *, live: bool = False, force: bool = False) -> VMSpec:
        with self.store.lock(vm_id, timeout=60):
            spec = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(spec, self.store.load_runtime(vm_id))
            busy = self._vm_busy(runtime)
            if busy and not live:
                raise InvalidStateError(
                    "The installation image can only be ejected from a stopped VM without live change.",
                    hint="Shut down the VM, or retry with live change.",
                )
            if busy:
                if not self.backend.capabilities.supports_live_iso_change:
                    raise UnsupportedFeatureError(
                        "This backend cannot change the installation medium while the VM is running.",
                        hint="Shut down the VM and eject the ISO.",
                    )
                self.backend.change_medium(spec, runtime, None, force=force)
                reason = "iso_ejected_live"
            else:
                reason = "iso_ejected"
            from openqvm.core.models import BootDevice

            spec.cdrom_iso = None
            spec.boot_order = [device for device in spec.boot_order if device is not BootDevice.CDROM]
            spec.touch()
            self.store.save_spec(spec)
            self._publish_vm(TOPIC_VM_UPDATED, spec.id, name=spec.name, reason=reason)
            return spec

    def disk_usage(self, spec: VMSpec) -> tuple[int, int]:
        """Return ``(virtual_bytes, allocated_bytes)`` measured with qemu-img."""

        virtual = 0
        allocated = 0
        for disk in spec.disks:
            try:
                info = self.disks.info(self.store.disk_path(spec, disk))
            except OpenQVMError:
                virtual += disk.size_bytes
                continue
            virtual += info.virtual_size
            allocated += info.actual_size
        return virtual, allocated

    @_recorded("Check disks")
    def check_disks(self, vm_id: str, *, timeout: float = 1800.0) -> DiskCheckSummary:
        """Run ``qemu-img check`` on every disk of a stopped VM.

        Individual disk failures are reported in the result rather than raised so
        that one damaged image does not hide the condition of the others.  Running
        the check against a live VM is refused because ``qemu-img`` must not touch
        disks that QEMU is writing.
        """

        with self.store.lock(vm_id, timeout=60):
            spec = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(spec, self.store.load_runtime(vm_id))
            if self._vm_busy(runtime):
                raise InvalidStateError(
                    "Disk consistency checks require the virtual machine to be stopped.",
                    hint="Shut down the VM first so qemu-img can read the disks safely.",
                )
            results: list[DiskCheckResult] = []
            for disk in spec.disks:
                path = self.store.disk_path(spec, disk)
                try:
                    results.append(self.disks.check(path, timeout=timeout))
                except OpenQVMError as exc:
                    results.append(
                        DiskCheckResult(path=path, ok=False, output=exc.message, error=exc.code.value)
                    )
            summary = DiskCheckSummary(vm_id=vm_id, results=results)
        if not summary.ok:
            log.warning("Disk check reported problems for VM '%s'", spec.name)
        return summary

    # -- snapshots ------------------------------------------------------------

    def _snapshot_tags(self, spec: VMSpec, runtime: VMRuntime) -> list[SnapshotTagInfo]:
        if runtime.status.is_active:
            client = self.backend.qmp_client(runtime)
            try:
                return self.snapshots.list_online(client, spec)
            finally:
                client.close()
        self._require_offline_safe(runtime)
        return self.snapshots.list_offline(spec)

    @staticmethod
    def _require_offline_safe(runtime: VMRuntime) -> None:
        """Refuse disk-level snapshot access while a QEMU process may still be writing."""

        if runtime.pid is not None and runtime.status is not VMStatus.STOPPED:
            raise InvalidStateError(
                "The QEMU control interface is unavailable, so the disks cannot be accessed safely.",
                hint="Restore control of the VM, or force-stop it, before managing snapshots.",
                details={"status": runtime.status.value, "pid": runtime.pid},
            )

    def list_snapshots(self, vm_id: str) -> list[SnapshotView]:
        spec = self.store.load_spec(vm_id)
        runtime = self.store.load_runtime(vm_id)
        if runtime.status.is_active or runtime.pid is not None:
            runtime = self.backend.refresh(spec, runtime)
        metadata = {record.name: record for record in self.store.load_snapshots(vm_id)}
        views: list[SnapshotView] = []
        tags = self._snapshot_tags(spec, runtime)
        present = {tag.name for tag in tags}
        for record in metadata.values():
            views.append(
                SnapshotView(
                    name=record.name,
                    description=record.description,
                    created_at=record.created_at,
                    includes_memory=record.includes_memory,
                    vm_state=record.vm_state,
                    present=record.name in present,
                )
            )
        for tag in tags:
            if tag.name in metadata:
                continue
            views.append(
                SnapshotView(
                    name=tag.name,
                    description="",
                    created_at=tag.date or "",
                    includes_memory=tag.vm_size not in {"0 B", "0", ""},
                    vm_state="unknown",
                    present=True,
                )
            )
        views.sort(key=lambda view: (view.created_at, view.name), reverse=True)
        return views

    @_recorded("Create snapshot")
    def create_snapshot(self, vm_id: str, name: str, description: str = "") -> SnapshotView:
        name = name.strip()
        raise_if_errors(validate_snapshot_name(name), message="The snapshot name is not valid.")
        with self.store.lock(vm_id, timeout=60):
            spec = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(spec, self.store.load_runtime(vm_id))
            metadata = self.store.load_snapshots(vm_id)
            if any(record.name.lower() == name.lower() for record in metadata):
                raise ConflictError(f"A snapshot named '{name}' already exists.")
            live_names = {tag.name.lower() for tag in self._snapshot_tags(spec, runtime)}
            if name.lower() in live_names:
                raise ConflictError(f"A snapshot named '{name}' already exists.")
            record: SnapshotRecord
            if runtime.status.is_active:
                client = self.backend.qmp_client(runtime)
                try:
                    record = self.snapshots.create_online(
                        client, spec, name, description, vm_state=runtime.status.value
                    )
                finally:
                    client.close()
            else:
                self._require_offline_safe(runtime)
                record = self.snapshots.create_offline(spec, name, description)
            metadata.append(record)
            try:
                self.store.save_snapshots(vm_id, metadata)
            except Exception:
                with suppress(Exception):
                    if runtime.status.is_active:
                        client = self.backend.qmp_client(runtime)
                        try:
                            self.snapshots.delete_online(client, name)
                        finally:
                            client.close()
                    else:
                        self.snapshots.delete_offline(spec, name)
                raise
            view = SnapshotView(
                name=record.name,
                description=record.description,
                created_at=record.created_at,
                includes_memory=record.includes_memory,
                vm_state=record.vm_state,
                present=True,
            )
        self._publish_snapshots(vm_id, name=record.name, reason="created")
        return view

    @_recorded("Restore snapshot")
    def restore_snapshot(self, vm_id: str, name: str) -> VMRuntime:
        with self.store.lock(vm_id, timeout=120):
            spec = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(spec, self.store.load_runtime(vm_id))
            tags = {tag.name for tag in self._snapshot_tags(spec, runtime)}
            if name not in tags:
                raise ValidationError(f"Snapshot '{name}' was not found on the VM disks.")
            record = next((item for item in self.store.load_snapshots(vm_id) if item.name == name), None)
            if runtime.status.is_active:
                if record is None or not record.includes_memory:
                    raise InvalidStateError(
                        "This disk-only snapshot can only be restored while the virtual machine is stopped.",
                        hint="Shut down the VM, then restore the snapshot.",
                    )
                client = self.backend.qmp_client(runtime)
                try:
                    self.snapshots.restore_online(client, name, includes_memory=True)
                finally:
                    client.close()
                self._publish_snapshots(vm_id, name=name, reason="restored")
                return self.backend.refresh(spec, runtime)
            if record is not None and record.includes_memory:
                log.warning(
                    "Restoring memory snapshot %s offline for %s: guest memory state is not restored",
                    name,
                    spec.name,
                )
            self._require_offline_safe(runtime)
            self.snapshots.restore_offline(spec, name)
        self._publish_snapshots(vm_id, name=name, reason="restored")
        return runtime

    @_recorded("Delete snapshot")
    def delete_snapshot(self, vm_id: str, name: str) -> None:
        with self.store.lock(vm_id, timeout=120):
            spec = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(spec, self.store.load_runtime(vm_id))
            if runtime.status.is_active:
                client = self.backend.qmp_client(runtime)
                try:
                    self.snapshots.delete_online(client, name)
                finally:
                    client.close()
            else:
                self._require_offline_safe(runtime)
                self.snapshots.delete_offline(spec, name)
            metadata = [record for record in self.store.load_snapshots(vm_id) if record.name != name]
            self.store.save_snapshots(vm_id, metadata)
        self._publish_snapshots(vm_id, name=name, reason="deleted")

    # -- clone / export / import ---------------------------------------------

    @_recorded("Clone VM")
    def clone_vm(self, vm_id: str, new_name: str) -> CloneResult:
        raise_if_errors(validate_vm_name(new_name), message="The clone name is not valid.")
        with self.store.lock(vm_id, timeout=120):
            source = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(source, self.store.load_runtime(vm_id))
            if self._vm_busy(runtime):
                raise InvalidStateError(
                    "A running virtual machine cannot be cloned.",
                    hint="Shut down the VM before cloning it.",
                )
            if self.store.name_exists(new_name):
                raise ConflictError(f"Another virtual machine is already named '{new_name}'.")
            clone = source.clone_with_identity(new_name)
            self._raise_for_issues(clone)
            vm_dir = self.store.create_vm_dir(clone.id)
            try:
                for source_disk, target_disk in zip(source.disks, clone.disks, strict=True):
                    self.disks.convert(
                        self.store.disk_path(source, source_disk),
                        self.store.disk_path(clone, target_disk),
                        target_format=target_disk.format,
                        preallocated=target_disk.preallocated,
                    )
                self.store.save_spec(clone)
                self.store.save_runtime(VMRuntime(vm_id=clone.id))
            except Exception:
                shutil.rmtree(vm_dir, ignore_errors=True)
                raise
        snapshots = self.store.load_snapshots(vm_id)
        warnings: list[str] = []
        if snapshots:
            warnings.append("Snapshot history is not copied by cloning.")
        if any(nic.port_forwards for nic in source.nics):
            warnings.append("NAT port forwards are not copied to the clone.")
        return CloneResult(spec=clone, warnings=warnings)

    @_recorded("Export VM")
    def export_vm(self, vm_id: str, output_path: Path, *, overwrite: bool = False) -> ExportResult:
        with self.store.lock(vm_id, timeout=120):
            spec = self.store.load_spec(vm_id)
            runtime = self.backend.refresh(spec, self.store.load_runtime(vm_id))
            if self._vm_busy(runtime):
                raise InvalidStateError(
                    "A running virtual machine cannot be exported.",
                    hint="Shut down the VM so the exported disks are consistent.",
                )
            return self.transfer.export_vm(spec, output_path, overwrite=overwrite)

    def import_vm(self, package: Path, *, new_name: str | None = None) -> ImportResult:
        record = self.operations.start("Import VM", vm_name=new_name or package.name)
        try:
            with self._create_lock:
                result = self.transfer.import_vm(package, new_name=new_name)
            self.operations.succeed(record, message=f"Imported '{result.spec.name}'.")
            return result
        except OpenQVMError as exc:
            self.operations.fail(record, exc)
            raise
        except Exception as exc:  # pragma: no cover - defensive
            normalized = error_from_exception(exc, default_message="The virtual machine could not be imported.")
            self.operations.fail(record, normalized)
            raise normalized from exc

    # -- diagnostics ----------------------------------------------------------

    def recent_operations(self, limit: int = 20):
        return self.operations.recent(limit)

    def network_security_note(self, spec: VMSpec) -> str:
        return " ".join(security_note(nic.mode) for nic in spec.nics) or "No network adapters are attached."

    def counts(self) -> dict[str, int]:
        counts = {"total": 0, "running": 0, "paused": 0, "stopped": 0, "error": 0, "unknown": 0}
        for spec in self.store.list_specs():
            counts["total"] += 1
            try:
                status = self.store.load_runtime(spec.id).status
            except OpenQVMError:
                counts["error"] += 1
                continue
            key = status.value if status.value in counts else "unknown"
            counts[key] += 1
        return counts

    def vnc_endpoint(self, vm_id: str) -> tuple[str, int] | None:
        runtime = self.store.load_runtime(vm_id)
        if not runtime.vnc_port or runtime.status not in (VMStatus.RUNNING, VMStatus.PAUSED):
            return None
        return "127.0.0.1", runtime.vnc_port

    def serial_log_path(self, vm_id: str) -> Path:
        return self.paths.vm_serial_log(vm_id)

    def qemu_log_path(self, vm_id: str) -> Path:
        return self.paths.vm_log_file(vm_id)

    def last_error(self, vm_id: str) -> str | None:
        try:
            return self.store.load_runtime(vm_id).last_error
        except OpenQVMError:
            return None

    def make_port_forward(self, host_port: int = 2222, guest_port: int = 22, protocol: str = "tcp") -> PortForward:
        return PortForward(protocol=protocol, host_port=host_port, guest_port=guest_port)
