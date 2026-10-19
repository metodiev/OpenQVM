"""Persistent storage for VM definitions, runtime state and snapshots.

Each VM owns a directory:

    <data>/vms/<uuid>/
        vm.json          configuration (atomically written, with .bak)
        runtime.json     transient lifecycle state
        snapshots.json   snapshot metadata
        disks/           qcow2/raw disk images
        firmware/        per-VM UEFI variable store
        qemu.log         QEMU stderr/stdout
        serial.log       guest serial console
        .lock            advisory operation lock
"""

from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openqvm.core.atomic import atomic_write_json, read_json
from openqvm.core.errors import CorruptDataError, NotFoundError, OperationFailedError, ValidationError
from openqvm.core.ids import is_uuid
from openqvm.core.locking import FileLock
from openqvm.core.models import (
    SCHEMA_VERSION,
    DiskSpec,
    SnapshotRecord,
    VMRuntime,
    VMSpec,
    VMStatus,
)
from openqvm.core.paths import AppPaths, resolve_within

log = logging.getLogger("openqvm.store")


@dataclass(frozen=True)
class StoreError:
    vm_id: str
    path: str
    message: str

    def as_text(self) -> str:
        return f"{self.vm_id}: {self.message}"


@dataclass
class StoreScan:
    specs: list[VMSpec] = field(default_factory=list)
    errors: list[StoreError] = field(default_factory=list)


class VmStore:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.paths.ensure()

    # -- scanning and loading -------------------------------------------------

    def scan(self) -> StoreScan:
        result = StoreScan()
        vms_dir = self.paths.vms_dir
        if not vms_dir.is_dir():
            return result
        for entry in sorted(vms_dir.iterdir(), key=lambda item: item.name.lower()):
            if not entry.is_dir() or not is_uuid(entry.name):
                continue
            try:
                result.specs.append(self.load_spec(entry.name))
            except (CorruptDataError, NotFoundError, ValidationError) as exc:
                result.errors.append(StoreError(entry.name, str(entry / "vm.json"), str(exc)))
            except OSError as exc:
                result.errors.append(StoreError(entry.name, str(entry), f"Could not read VM directory: {exc}"))
        result.specs.sort(key=lambda spec: spec.name.lower())
        return result

    def list_specs(self) -> list[VMSpec]:
        return self.scan().specs

    def load_spec(self, vm_id: str, *, recover: bool = True) -> VMSpec:
        spec_file = self.paths.vm_spec_file(vm_id)
        if not spec_file.is_file():
            raise NotFoundError(
                f"Virtual machine {vm_id} is not registered.",
                hint="Refresh the virtual machine list.",
                details={"vm_id": vm_id},
            )
        try:
            return VMSpec.from_dict(read_json(spec_file))
        except (CorruptDataError, ValidationError) as exc:
            if recover:
                backup = spec_file.with_suffix(".json.bak")
                if backup.is_file():
                    try:
                        spec = VMSpec.from_dict(read_json(backup))
                    except (CorruptDataError, ValidationError):
                        spec = None
                    if spec is not None:
                        log.warning("Recovered %s from its backup file", spec_file)
                        atomic_write_json(spec_file, spec.to_dict())
                        return spec
            if isinstance(exc, ValidationError):
                raise CorruptDataError(
                    f"The configuration of virtual machine {vm_id} is invalid: {exc}",
                    hint="Restore vm.json from a backup or remove the virtual machine.",
                    details={"vm_id": vm_id, "file": str(spec_file)},
                    cause=exc,
                ) from exc
            raise

    def save_spec(self, spec: VMSpec) -> None:
        vm_dir = self.paths.vm_dir(spec.id)
        vm_dir.mkdir(parents=True, exist_ok=True)
        spec_file = self.paths.vm_spec_file(spec.id)
        if spec_file.is_file():
            backup = spec_file.with_suffix(".json.bak")
            try:
                shutil.copy2(spec_file, backup)
            except OSError:
                log.exception("Could not create a configuration backup for %s", spec.id)
        atomic_write_json(spec_file, spec.to_dict())
        log.info("Saved VM configuration %s (%s)", spec.name, spec.id)

    def spec_exists(self, vm_id: str) -> bool:
        return self.paths.vm_spec_file(vm_id).is_file()

    def name_exists(self, name: str, *, exclude_vm_id: str | None = None) -> bool:
        normalized = name.strip().lower()
        for spec in self.list_specs():
            if spec.id != exclude_vm_id and spec.name.strip().lower() == normalized:
                return True
        return False

    # -- runtime --------------------------------------------------------------

    def load_runtime(self, vm_id: str) -> VMRuntime:
        runtime_file = self.paths.vm_runtime_file(vm_id)
        if not runtime_file.is_file():
            return VMRuntime(vm_id=vm_id)
        try:
            runtime = VMRuntime.from_dict(read_json(runtime_file))
        except (CorruptDataError, ValidationError) as exc:
            raise CorruptDataError(
                f"The runtime state of virtual machine {vm_id} is invalid.",
                hint="Stop the VM (if running) and remove runtime.json.",
                details={"vm_id": vm_id, "file": str(runtime_file)},
                cause=exc,
            ) from exc
        if runtime.vm_id != vm_id:
            raise CorruptDataError(
                "The runtime state belongs to a different virtual machine.",
                details={"expected": vm_id, "actual": runtime.vm_id},
            )
        return runtime

    def save_runtime(self, runtime: VMRuntime) -> None:
        vm_dir = self.paths.vm_dir(runtime.vm_id)
        if not vm_dir.is_dir():
            raise NotFoundError(f"Virtual machine {runtime.vm_id} is not registered.")
        atomic_write_json(self.paths.vm_runtime_file(runtime.vm_id), runtime.to_dict())
        runtime_file = self.paths.vm_runtime_file(runtime.vm_id)
        backup = runtime_file.with_suffix(".json.bak")
        try:
            shutil.copy2(runtime_file, backup)
        except OSError:
            pass

    # -- snapshots ------------------------------------------------------------

    def load_snapshots(self, vm_id: str) -> list[SnapshotRecord]:
        snapshot_file = self.paths.vm_snapshots_file(vm_id)
        if not snapshot_file.is_file():
            return []
        data = read_json(snapshot_file)
        if isinstance(data, dict):
            data = data.get("snapshots", [])
        if not isinstance(data, list):
            raise CorruptDataError(
                "The snapshot metadata is invalid.",
                details={"file": str(snapshot_file)},
            )
        try:
            return [SnapshotRecord.from_dict(item) for item in data]
        except ValidationError as exc:
            raise CorruptDataError(
                "The snapshot metadata is invalid.",
                details={"file": str(snapshot_file)},
                cause=exc,
            ) from exc

    def save_snapshots(self, vm_id: str, records: list[SnapshotRecord]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "snapshots": [record.to_dict() for record in records],
        }
        atomic_write_json(self.paths.vm_snapshots_file(vm_id), payload)

    # -- paths ----------------------------------------------------------------

    def disk_path(self, spec: VMSpec, disk: DiskSpec) -> Path:
        return resolve_within(self.paths.vm_disk_dir(spec.id), disk.filename)

    def primary_disk_path(self, spec: VMSpec) -> Path:
        disk = spec.system_disk
        if disk is None:
            raise ValidationError("The virtual machine has no system disk.")
        return self.disk_path(spec, disk)

    def serial_log_path(self, vm_id: str) -> Path:
        return self.paths.vm_serial_log(vm_id)

    def qemu_log_path(self, vm_id: str) -> Path:
        return self.paths.vm_log_file(vm_id)

    # -- lifecycle of the VM directory ---------------------------------------

    def create_vm_dir(self, vm_id: str) -> Path:
        if not is_uuid(vm_id):
            raise ValidationError("The virtual machine identifier is not valid.")
        if self.spec_exists(vm_id):
            raise ValidationError(f"Virtual machine {vm_id} already exists.")
        vm_dir = self.paths.vm_dir(vm_id)
        for directory in (vm_dir, self.paths.vm_disk_dir(vm_id), self.paths.vm_firmware_dir(vm_id)):
            directory.mkdir(parents=True, exist_ok=True)
        return vm_dir

    def lock(self, vm_id: str, *, timeout: float = 10.0) -> FileLock:
        return FileLock(self.paths.vm_lock_file(vm_id), timeout=timeout)

    def quarantine_vm(self, vm_id: str) -> Path:
        """Move a VM directory into the trash instead of deleting it immediately."""

        vm_dir = self.paths.require_vm_dir(vm_id)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = self.paths.trash_dir / f"{vm_id}-{stamp}"
        self.paths.trash_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(vm_dir), str(destination))
        except OSError as exc:
            raise OperationFailedError(
                "The virtual machine directory could not be moved to the trash.",
                hint="Close any program that has files open in the VM directory and try again.",
                details={"vm_id": vm_id},
                cause=exc,
            ) from exc
        log.info("Quarantined VM %s in %s", vm_id, destination)
        return destination

    def purge_trash(self) -> int:
        removed = 0
        if not self.paths.trash_dir.is_dir():
            return removed
        for entry in self.paths.trash_dir.iterdir():
            try:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed += 1
            except OSError:
                log.exception("Could not purge trash entry %s", entry)
        return removed

    def stopped_runtime(self, vm_id: str) -> VMRuntime:
        return VMRuntime(vm_id=vm_id, status=VMStatus.STOPPED, stopped_at=datetime.now(UTC).isoformat(timespec="seconds"))


class SettingsStore:
    """Small, forward-compatible application settings store."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> AppSettings:
        if not self.path.is_file():
            return AppSettings()
        try:
            data = read_json(self.path)
            return AppSettings.from_dict(data)
        except (CorruptDataError, ValidationError, OSError) as exc:
            log.warning("Ignoring invalid settings file %s: %s", self.path, exc)
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        with self._lock:
            atomic_write_json(self.path, settings.to_dict())


@dataclass
class AppSettings:
    theme: str = "dark"
    qemu_dir: str | None = None
    console_fit: bool = True
    last_page: str = "dashboard"
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.theme not in {"dark", "light"}:
            raise ValidationError("Theme must be dark or light.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "theme": self.theme,
            "qemu_dir": self.qemu_dir,
            "console_fit": self.console_fit,
            "last_page": self.last_page,
        }

    @classmethod
    def from_dict(cls, data: Any) -> AppSettings:
        if not isinstance(data, dict):
            raise ValidationError("Settings must be an object.")
        theme = data.get("theme", "dark")
        if not isinstance(theme, str):
            raise ValidationError("Theme must be a string.")
        qemu_dir = data.get("qemu_dir")
        if qemu_dir is not None and not isinstance(qemu_dir, str):
            raise ValidationError("qemu_dir must be a string or null.")
        return cls(
            theme=theme,
            qemu_dir=qemu_dir or None,
            console_fit=bool(data.get("console_fit", True)),
            last_page=str(data.get("last_page", "dashboard")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )
