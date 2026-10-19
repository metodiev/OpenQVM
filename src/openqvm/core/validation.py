"""Validation rules for virtual machine definitions.

Validation is intentionally independent from any platform backend: structural and
resource checks live here, while hardware/feature availability is checked in
:mod:`openqvm.platform.compatibility`.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from openqvm.core.errors import ValidationError, ValidationIssue, raise_if_errors
from openqvm.core.ids import is_uuid
from openqvm.core.models import (
    BootDevice,
    DiskSpec,
    Firmware,
    GuestOS,
    NetworkMode,
    NicModel,
    SnapshotRecord,
    VMSpec,
)

_MIN_MEMORY_MIB = 128
_MAX_VCPUS = 128
_MAX_DISK_BYTES = 16 * 1024**4
_MIN_DISK_BYTES = 16 * 1024**2

_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 ._()+-]{0,62}[A-Za-z0-9)])?$")
_SNAPSHOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHARED_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


@dataclass(frozen=True)
class HostResources:
    """Physical resources of the host, used to bound allocations."""

    cpu_count: int
    memory_total_mib: int  # 0 when the value could not be determined

    @classmethod
    def detect(cls) -> HostResources:
        cpu_count = os.cpu_count() or 1
        memory_mib = 0
        try:
            import psutil

            cpu_count = psutil.cpu_count(logical=True) or cpu_count
            memory_mib = int(psutil.virtual_memory().total // (1024 * 1024))
        except Exception:
            pass
        return cls(cpu_count=max(1, cpu_count), memory_total_mib=memory_mib)


def validate_vm_name(name: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not isinstance(name, str) or not name.strip():
        return [ValidationIssue("name", "A virtual machine name is required.")]
    if name != name.strip():
        issues.append(ValidationIssue("name", "The name must not start or end with whitespace."))
    if len(name) > 64:
        issues.append(ValidationIssue("name", "The name must be at most 64 characters long."))
    if not _NAME_RE.match(name):
        issues.append(
            ValidationIssue(
                "name",
                "Use letters, digits, spaces and the characters . _ ( ) + - only.",
            )
        )
    stem = name.split(".", 1)[0].strip().lower()
    if stem in _RESERVED_NAMES:
        issues.append(ValidationIssue("name", "This name is reserved by the operating system."))
    return issues


def validate_snapshot_name(name: str) -> list[ValidationIssue]:
    if not _SNAPSHOT_RE.match(name or ""):
        return [
            ValidationIssue(
                "snapshot",
                "Snapshot names must start with a letter or digit and contain only letters, "
                "digits, dots, dashes and underscores (maximum 64 characters).",
            )
        ]
    return []


def validate_disk_resize(disk: DiskSpec, new_size_bytes: int) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if isinstance(new_size_bytes, bool) or not isinstance(new_size_bytes, int):
        return [ValidationIssue("disk.size", "The new disk size must be a number.")]
    if new_size_bytes <= disk.size_bytes:
        issues.append(
            ValidationIssue(
                "disk.size",
                "Disks can only be enlarged. Shrinking is refused because it can corrupt the guest file system.",
            )
        )
    if new_size_bytes > _MAX_DISK_BYTES:
        issues.append(ValidationIssue("disk.size", "The disk size exceeds the 16 TiB limit."))
    if new_size_bytes < _MIN_DISK_BYTES:
        issues.append(ValidationIssue("disk.size", "The disk must be at least 16 MiB."))
    return issues


def validate_vm_spec(
    spec: VMSpec,
    *,
    host: HostResources,
    existing_specs: tuple[VMSpec, ...] | list[VMSpec] = (),
    exclude_vm_id: str | None = None,
) -> list[ValidationIssue]:
    """Return every issue found in *spec*.

    ``existing_specs`` is used for cross-VM checks such as duplicate names, MAC
    addresses and NAT host ports.  The VM with ``exclude_vm_id`` (the VM being
    edited) is ignored for those checks.
    """

    issues: list[ValidationIssue] = []
    others = [other for other in existing_specs if other.id != (exclude_vm_id or spec.id)]

    issues.extend(validate_vm_name(spec.name))

    normalized_name = spec.name.strip().lower()
    for other in others:
        if other.name.strip().lower() == normalized_name:
            issues.append(ValidationIssue("name", f"Another virtual machine is already named '{other.name}'."))
            break

    if not is_uuid(spec.id):
        issues.append(ValidationIssue("id", "The virtual machine identifier is not a UUID."))

    issues.extend(_validate_resources(spec, host))

    if spec.firmware is Firmware.UEFI and spec.machine == "pc":
        issues.append(
            ValidationIssue(
                "firmware",
                "UEFI firmware requires the q35 machine type (legacy 'pc' has no UEFI support).",
            )
        )

    if spec.machine not in {"q35", "pc"}:
        issues.append(ValidationIssue("machine", "Machine type must be 'q35' or 'pc'."))

    if not spec.disks:
        issues.append(ValidationIssue("disks", "At least one virtual disk is required."))
    system_disks = [disk for disk in spec.disks if disk.role == "system"]
    if spec.disks and len(system_disks) != 1:
        issues.append(ValidationIssue("disks", "Exactly one system disk is required."))

    seen_disk_ids: set[str] = set()
    seen_filenames: set[str] = set()
    for index, disk in enumerate(spec.disks):
        prefix = f"disks[{index}]"
        if disk.id in seen_disk_ids:
            issues.append(ValidationIssue(f"{prefix}.id", "Disk identifiers must be unique."))
        seen_disk_ids.add(disk.id)
        filename_key = disk.filename.lower()
        if filename_key in seen_filenames:
            issues.append(ValidationIssue(f"{prefix}.filename", "Disk file names must be unique."))
        seen_filenames.add(filename_key)
        if disk.bus.value == "ide" and spec.machine == "q35":
            issues.append(
                ValidationIssue(
                    f"{prefix}.bus",
                    "IDE disks are only supported with the 'pc' machine type. Use VirtIO for q35.",
                    severity="warning",
                )
            )
        if not 16 * 1024**2 <= disk.size_bytes <= _MAX_DISK_BYTES:
            issues.append(ValidationIssue(f"{prefix}.size_bytes", "Disk size is outside the supported range."))

    if not spec.boot_order:
        issues.append(ValidationIssue("boot_order", "At least one boot device is required."))
    if len(set(spec.boot_order)) != len(spec.boot_order):
        issues.append(ValidationIssue("boot_order", "Boot devices must not be repeated."))
    has_iso = bool(spec.cdrom_iso)
    if BootDevice.CDROM in spec.boot_order and not has_iso:
        issues.append(
            ValidationIssue(
                "boot_order",
                "The CD-ROM is selected as a boot device but no ISO image is attached.",
                severity="warning",
            )
        )
    if BootDevice.NETWORK in spec.boot_order:
        issues.append(
            ValidationIssue(
                "boot_order",
                "Network boot (PXE) is not supported by this version of OpenQVM.",
                severity="warning",
            )
        )

    if has_iso:
        issues.extend(_validate_iso_reference(spec.cdrom_iso or ""))

    issues.extend(_validate_network(spec, others))

    seen_tags: set[str] = set()
    for index, folder in enumerate(spec.shared_folders):
        prefix = f"shared_folders[{index}]"
        if not _SHARED_TAG_RE.match(folder.tag):
            issues.append(ValidationIssue(f"{prefix}.tag", "Shared folder tags may contain letters, digits, '_' and '-'."))
        if folder.tag.lower() in seen_tags:
            issues.append(ValidationIssue(f"{prefix}.tag", "Shared folder tags must be unique."))
        seen_tags.add(folder.tag.lower())
        path = Path(folder.path)
        if not path.is_absolute() or ".." in path.parts:
            issues.append(ValidationIssue(f"{prefix}.path", "Shared folder paths must be absolute and normalized."))
        elif not path.is_dir():
            issues.append(ValidationIssue(f"{prefix}.path", "The shared folder does not exist or is not a directory."))

    if spec.guest_os is GuestOS.WINDOWS_11:
        issues.append(
            ValidationIssue(
                "guest_os",
                "Windows 11 requires UEFI and a TPM 2.0 device. OpenQVM does not yet provide a TPM, "
                "so the Windows 11 installer may refuse to continue.",
                severity="warning",
            )
        )

    return issues


def _validate_resources(spec: VMSpec, host: HostResources) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    vcpus = spec.vcpus
    if vcpus < 1:
        issues.append(ValidationIssue("cpu", "At least one virtual CPU is required."))
    if vcpus > _MAX_VCPUS:
        issues.append(ValidationIssue("cpu", f"At most {_MAX_VCPUS} virtual CPUs are supported."))
    if host.cpu_count and vcpus > host.cpu_count * 2:
        issues.append(
            ValidationIssue(
                "cpu",
                f"{vcpus} virtual CPUs exceed twice the host core count ({host.cpu_count}).",
            )
        )
    elif host.cpu_count and vcpus > host.cpu_count:
        issues.append(
            ValidationIssue(
                "cpu",
                f"{vcpus} virtual CPUs exceed the host's {host.cpu_count} logical processors; "
                "overcommitting may reduce performance.",
                severity="warning",
            )
        )

    if spec.memory_mib < _MIN_MEMORY_MIB:
        issues.append(ValidationIssue("memory_mib", f"Memory must be at least {_MIN_MEMORY_MIB} MiB."))
    if spec.memory_mib > 1024 * 1024:
        issues.append(ValidationIssue("memory_mib", "Memory must not exceed 1 TiB."))
    if spec.memory_mib % 4:
        issues.append(ValidationIssue("memory_mib", "Memory should be a multiple of 4 MiB.", severity="warning"))
    if host.memory_total_mib:
        limit = int(host.memory_total_mib * 0.9)
        if spec.memory_mib > limit:
            issues.append(
                ValidationIssue(
                    "memory_mib",
                    f"Memory exceeds 90% of physical host memory ({host.memory_total_mib} MiB).",
                )
            )
    return issues


def _validate_iso_reference(path: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    iso = Path(path)
    if not iso.is_absolute() or ".." in iso.parts:
        issues.append(ValidationIssue("cdrom_iso", "The ISO path must be absolute and normalized."))
        return issues
    if iso.suffix.lower() not in {".iso", ".img"}:
        issues.append(ValidationIssue("cdrom_iso", "The installation image must be an .iso or .img file."))
    if not iso.exists():
        issues.append(ValidationIssue("cdrom_iso", "The selected installation image no longer exists."))
    elif not iso.is_file():
        issues.append(ValidationIssue("cdrom_iso", "The selected installation image is not a file."))
    return issues


def _validate_network(spec: VMSpec, others: list[VMSpec]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not spec.nics:
        return issues

    seen_macs: dict[str, str] = {}
    for other in others:
        for nic in other.nics:
            seen_macs[nic.mac] = other.name

    used_host_ports: set[tuple[str, int]] = set()
    for index, nic in enumerate(spec.nics):
        prefix = f"nics[{index}]"
        if nic.mac in seen_macs:
            issues.append(
                ValidationIssue(
                    f"{prefix}.mac",
                    f"The MAC address {nic.mac} is already used by '{seen_macs[nic.mac]}'.",
                )
            )
        seen_macs[nic.mac] = spec.name
        if nic.mode is NetworkMode.BRIDGED and not nic.bridge:
            issues.append(ValidationIssue(f"{prefix}.bridge", "A host bridge name is required for bridged networking."))
        if nic.mode is NetworkMode.NAT:
            for forward_index, forward in enumerate(nic.port_forwards):
                key = (forward.protocol, forward.host_port)
                if key in used_host_ports:
                    issues.append(
                        ValidationIssue(
                            f"{prefix}.port_forwards[{forward_index}].host_port",
                            f"Host port {forward.host_port}/{forward.protocol} is already forwarded in this VM.",
                        )
                    )
                used_host_ports.add(key)
        if nic.model is NicModel.E1000E and spec.guest_os not in (GuestOS.WINDOWS_10, GuestOS.WINDOWS_11):
            # e1000e works for Linux too, but VirtIO is faster; keep this advisory.
            pass
    return issues


def assert_valid_spec(
    spec: VMSpec,
    *,
    host: HostResources,
    existing_specs: tuple[VMSpec, ...] | list[VMSpec] = (),
    exclude_vm_id: str | None = None,
    extra_issues: tuple[ValidationIssue, ...] | list[ValidationIssue] = (),
) -> None:
    issues = list(validate_vm_spec(spec, host=host, existing_specs=existing_specs, exclude_vm_id=exclude_vm_id))
    issues.extend(extra_issues)
    raise_if_errors(issues, message="The virtual machine configuration is not valid.")


def validate_snapshot_unique(records: list[SnapshotRecord], name: str) -> list[ValidationIssue]:
    if any(record.name.lower() == name.lower() for record in records):
        return [ValidationIssue("snapshot", f"A snapshot named '{name}' already exists.")]
    return []


def ensure_writable_data_dir(path: Path) -> None:
    """Fail early with a clear message when the data directory is not writable."""

    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValidationError(
            f"The OpenQVM data directory is not writable: {path}",
            hint="Choose a writable data directory or fix its permissions.",
            cause=exc,
        ) from exc


def is_platform_windows() -> bool:
    return sys.platform == "win32"
