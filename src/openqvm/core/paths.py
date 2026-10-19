"""Application data locations and path-safety helpers."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from openqvm.core.errors import NotFoundError, SecurityError, ValidationError
from openqvm.core.ids import is_uuid

_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _default_data_root() -> Path:
    override = os.environ.get("OPENQVM_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "OpenQVM"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenQVM"
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "openqvm"
    return Path.home() / ".local" / "share" / "openqvm"


@dataclass(frozen=True)
class AppPaths:
    """Filesystem layout of an OpenQVM data directory."""

    root: Path

    @classmethod
    def default(cls) -> AppPaths:
        return cls(_default_data_root())

    def ensure(self) -> None:
        for directory in (
            self.root,
            self.vms_dir,
            self.logs_dir,
            self.isos_dir,
            self.trash_dir,
            self.exports_dir,
            self.backups_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def vms_dir(self) -> Path:
        return self.root / "vms"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def isos_dir(self) -> Path:
        return self.root / "iso"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def trash_dir(self) -> Path:
        return self.root / "trash"

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    @property
    def operations_log(self) -> Path:
        return self.root / "operations.jsonl"

    @property
    def app_log(self) -> Path:
        return self.logs_dir / "openqvm.log"

    def vm_dir(self, vm_id: str) -> Path:
        if not is_uuid(vm_id):
            raise ValidationError(
                "The virtual machine identifier is not valid.",
                hint="Use the identifier shown by 'openqvm-cli list'.",
                details={"vm_id": vm_id},
            )
        return self.vms_dir / vm_id

    def require_vm_dir(self, vm_id: str) -> Path:
        directory = self.vm_dir(vm_id)
        if not directory.is_dir():
            raise NotFoundError(
                f"Virtual machine {vm_id} was not found.",
                hint="Refresh the virtual machine list.",
                details={"vm_id": vm_id},
            )
        return directory

    def vm_spec_file(self, vm_id: str) -> Path:
        return self.vm_dir(vm_id) / "vm.json"

    def vm_runtime_file(self, vm_id: str) -> Path:
        return self.vm_dir(vm_id) / "runtime.json"

    def vm_snapshots_file(self, vm_id: str) -> Path:
        return self.vm_dir(vm_id) / "snapshots.json"

    def vm_backups_file(self, vm_id: str) -> Path:
        return self.vm_dir(vm_id) / "backups.json"

    def vm_lock_file(self, vm_id: str) -> Path:
        # Locks live outside the VM directory so that the directory can be moved
        # to the trash while the lock is still held (required on Windows).
        return self.vms_dir / ".locks" / f"{vm_id}.lock"

    def vm_log_file(self, vm_id: str) -> Path:
        return self.vm_dir(vm_id) / "qemu.log"

    def vm_serial_log(self, vm_id: str) -> Path:
        return self.vm_dir(vm_id) / "serial.log"

    def vm_disk_dir(self, vm_id: str) -> Path:
        return self.vm_dir(vm_id) / "disks"

    def vm_firmware_dir(self, vm_id: str) -> Path:
        return self.vm_dir(vm_id) / "firmware"

    def vm_tpm_dir(self, vm_id: str) -> Path:
        return self.vm_dir(vm_id) / "tpm"


def safe_component(value: str, *, what: str = "name") -> str:
    """Validate a single path component (no separators, no traversal)."""

    if not isinstance(value, str) or not _SAFE_COMPONENT_RE.match(value):
        raise ValidationError(
            f"The {what} contains characters that are not allowed.",
            hint="Use letters, digits, dots, underscores and dashes.",
            details={"value": value},
        )
    if value in {".", ".."}:
        raise ValidationError(f"The {what} is not valid.")
    return value


def resolve_within(base: Path, *parts: str) -> Path:
    """Resolve *parts* under *base*, rejecting traversal and symlink escapes."""

    base_resolved = Path(base).resolve()
    candidate = base_resolved.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise ValidationError("The file path could not be resolved.") from exc
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise SecurityError(
            "The requested path escapes the virtual machine directory.",
            hint="Use a file name without directory separators.",
            details={"base": str(base_resolved), "path": str(resolved)},
        )
    return resolved


def is_absolute_local_path(value: str) -> bool:
    path = Path(value)
    return path.is_absolute() and ".." not in path.parts


def is_within(path: Path, base: Path) -> bool:
    try:
        resolved = Path(path).resolve(strict=False)
        base_resolved = Path(base).resolve(strict=False)
    except OSError:
        return False
    return resolved == base_resolved or base_resolved in resolved.parents
