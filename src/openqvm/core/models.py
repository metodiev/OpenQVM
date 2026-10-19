"""Domain model for OpenQVM.

The model is deliberately explicit (no untyped dictionaries at the boundary) so
that persisted configuration is validated before any backend operation runs.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from openqvm.core.errors import ValidationError
from openqvm.core.ids import is_uuid, new_id

SCHEMA_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def parse_timestamp(value: str, *, field_name: str) -> str:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValidationError(f"{field_name} is not a valid timestamp.") from exc
    return value


class GuestOS(StrEnum):
    GENERIC = "generic"
    LINUX = "linux"
    UBUNTU = "ubuntu"
    DEBIAN = "debian"
    FEDORA = "fedora"
    WINDOWS_10 = "windows10"
    WINDOWS_11 = "windows11"
    OTHER = "other"

    @property
    def display_name(self) -> str:
        return {
            GuestOS.GENERIC: "Generic",
            GuestOS.LINUX: "Linux",
            GuestOS.UBUNTU: "Ubuntu",
            GuestOS.DEBIAN: "Debian",
            GuestOS.FEDORA: "Fedora",
            GuestOS.WINDOWS_10: "Windows 10",
            GuestOS.WINDOWS_11: "Windows 11",
            GuestOS.OTHER: "Other",
        }[self]

    @property
    def is_windows(self) -> bool:
        return self in (GuestOS.WINDOWS_10, GuestOS.WINDOWS_11)


class Firmware(StrEnum):
    BIOS = "bios"
    UEFI = "uefi"

    @property
    def display_name(self) -> str:
        return "BIOS" if self is Firmware.BIOS else "UEFI (edk2)"


class DiskFormat(StrEnum):
    QCOW2 = "qcow2"
    RAW = "raw"

    @property
    def supports_snapshots(self) -> bool:
        return self is DiskFormat.QCOW2


class DiskBus(StrEnum):
    VIRTIO = "virtio"
    IDE = "ide"

    @property
    def display_name(self) -> str:
        return "VirtIO" if self is DiskBus.VIRTIO else "IDE"


class NicModel(StrEnum):
    VIRTIO = "virtio-net-pci"
    E1000E = "e1000e"

    @property
    def display_name(self) -> str:
        return "VirtIO" if self is NicModel.VIRTIO else "Intel e1000e"


class NetworkMode(StrEnum):
    NAT = "nat"
    BRIDGED = "bridged"
    HOST_ONLY = "host_only"

    @property
    def display_name(self) -> str:
        return {
            NetworkMode.NAT: "NAT (user-mode)",
            NetworkMode.BRIDGED: "Bridged",
            NetworkMode.HOST_ONLY: "Host-only",
        }[self]


class GraphicsAdapter(StrEnum):
    STD = "std"
    VIRTIO = "virtio"


class BootDevice(StrEnum):
    DISK = "disk"
    CDROM = "cdrom"
    NETWORK = "network"


class VMStatus(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    ERROR = "error"
    UNKNOWN = "unknown"

    @property
    def is_active(self) -> bool:
        return self in (VMStatus.STARTING, VMStatus.RUNNING, VMStatus.PAUSED, VMStatus.STOPPING)

    @property
    def display_name(self) -> str:
        return {
            VMStatus.STOPPED: "Stopped",
            VMStatus.STARTING: "Starting",
            VMStatus.RUNNING: "Running",
            VMStatus.PAUSED: "Paused",
            VMStatus.STOPPING: "Stopping",
            VMStatus.ERROR: "Error",
            VMStatus.UNKNOWN: "Unknown",
        }[self]


class OperationStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# parsing helpers


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{field_name} must be an object.")
    return value


def _list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{field_name} must be a list.")
    return value


def _check_keys(data: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValidationError(
            f"{context} contains unsupported fields: {', '.join(unknown)}.",
            hint="Remove the unknown fields or upgrade OpenQVM.",
        )


def _str(data: dict[str, Any], key: str, *, context: str, allow_empty: bool = False, max_length: int = 4096) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValidationError(f"{context}.{key} must be a string.")
    if not allow_empty and not value.strip():
        raise ValidationError(f"{context}.{key} must not be empty.")
    if len(value) > max_length:
        raise ValidationError(f"{context}.{key} is too long (maximum {max_length} characters).")
    return value


def _opt_str(data: dict[str, Any], key: str, *, context: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{context}.{key} must be a string or null.")
    return value


def _int(
    data: dict[str, Any], key: str, *, context: str, minimum: int | None = None, maximum: int | None = None
) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{context}.{key} must be an integer.")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{context}.{key} must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise ValidationError(f"{context}.{key} must be at most {maximum}.")
    return value


def _bool(data: dict[str, Any], key: str, *, context: str, default: bool = False) -> bool:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, bool):
        raise ValidationError(f"{context}.{key} must be true or false.")
    return value


def _enum(enum_type, value: Any, *, context: str):  # type: ignore[no-untyped-def]
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise ValidationError(f"{context} must be one of: {allowed}.") from exc


# ---------------------------------------------------------------------------
# building blocks


@dataclass(frozen=True)
class CpuSpec:
    sockets: int = 1
    cores: int = 2
    threads: int = 1

    def __post_init__(self) -> None:
        for name in ("sockets", "cores", "threads"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 128:
                raise ValidationError(f"cpu.{name} must be an integer between 1 and 128.")

    @property
    def vcpus(self) -> int:
        return self.sockets * self.cores * self.threads

    def to_dict(self) -> dict[str, int]:
        return {"sockets": self.sockets, "cores": self.cores, "threads": self.threads}

    @classmethod
    def from_dict(cls, data: Any) -> CpuSpec:
        mapping = _mapping(data, "cpu")
        _check_keys(mapping, {"sockets", "cores", "threads"}, "cpu")
        try:
            return cls(
                sockets=_int(mapping, "sockets", context="cpu", minimum=1, maximum=128),
                cores=_int(mapping, "cores", context="cpu", minimum=1, maximum=128),
                threads=_int(mapping, "threads", context="cpu", minimum=1, maximum=128),
            )
        except ValidationError:
            raise
        except KeyError as exc:  # pragma: no cover - _int reports missing as type error
            raise ValidationError(f"cpu.{exc.args[0]} is required.") from exc


@dataclass(frozen=True)
class PortForward:
    protocol: str = "tcp"
    host_port: int = 2222
    guest_port: int = 22
    host_address: str = "127.0.0.1"

    def __post_init__(self) -> None:
        if self.protocol not in {"tcp", "udp"}:
            raise ValidationError("Port forwarding protocol must be 'tcp' or 'udp'.")
        for name in ("host_port", "guest_port"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
                raise ValidationError(f"{name} must be between 1 and 65535.")
        if not self.host_address:
            raise ValidationError("The host address for a port forward must not be empty.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "host_port": self.host_port,
            "guest_port": self.guest_port,
            "host_address": self.host_address,
        }

    @classmethod
    def from_dict(cls, data: Any, *, context: str = "port_forward") -> PortForward:
        mapping = _mapping(data, context)
        _check_keys(mapping, {"protocol", "host_port", "guest_port", "host_address"}, context)
        return cls(
            protocol=_str(mapping, "protocol", context=context),
            host_port=_int(mapping, "host_port", context=context, minimum=1, maximum=65535),
            guest_port=_int(mapping, "guest_port", context=context, minimum=1, maximum=65535),
            host_address=_str(mapping, "host_address", context=context),
        )


@dataclass(frozen=True)
class NicSpec:
    id: str
    mode: NetworkMode = NetworkMode.NAT
    model: NicModel = NicModel.VIRTIO
    mac: str = ""
    bridge: str | None = None
    port_forwards: tuple[PortForward, ...] = ()

    def __post_init__(self) -> None:
        if not is_uuid(self.id):
            raise ValidationError("Network adapter id must be a UUID.")
        if not self.mac:
            object.__setattr__(self, "mac", generate_mac())
        object.__setattr__(self, "mac", normalize_mac(self.mac))
        if self.mode is NetworkMode.BRIDGED and not (self.bridge or "").strip():
            raise ValidationError("Bridged networking requires a host bridge name.")
        if self.mode is not NetworkMode.NAT and self.port_forwards:
            raise ValidationError("Port forwarding is only available with NAT networking.")
        if not isinstance(self.mode, NetworkMode) or not isinstance(self.model, NicModel):
            raise ValidationError("Network adapter mode or model is not valid.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode.value,
            "model": self.model.value,
            "mac": self.mac,
            "bridge": self.bridge,
            "port_forwards": [forward.to_dict() for forward in self.port_forwards],
        }

    @classmethod
    def from_dict(cls, data: Any) -> NicSpec:
        mapping = _mapping(data, "network.adapter")
        _check_keys(mapping, {"id", "mode", "model", "mac", "bridge", "port_forwards"}, "network.adapter")
        adapter_id = _str(mapping, "id", context="network.adapter")
        if not is_uuid(adapter_id):
            raise ValidationError("network.adapter.id must be a UUID.")
        forwards_raw = _list(mapping.get("port_forwards", []), "network.adapter.port_forwards")
        return cls(
            id=adapter_id,
            mode=_enum(NetworkMode, mapping.get("mode", NetworkMode.NAT.value), context="network.adapter.mode"),
            model=_enum(NicModel, mapping.get("model", NicModel.VIRTIO.value), context="network.adapter.model"),
            mac=_str(mapping, "mac", context="network.adapter"),
            bridge=_opt_str(mapping, "bridge", context="network.adapter"),
            port_forwards=tuple(PortForward.from_dict(item) for item in forwards_raw),
        )


@dataclass(frozen=True)
class DiskSpec:
    id: str
    filename: str
    format: DiskFormat = DiskFormat.QCOW2
    bus: DiskBus = DiskBus.VIRTIO
    size_bytes: int = 20 * 1024**3
    preallocated: bool = False
    role: str = "system"

    def __post_init__(self) -> None:
        if not is_uuid(self.id):
            raise ValidationError("Disk id must be a UUID.")
        if not isinstance(self.format, DiskFormat) or not isinstance(self.bus, DiskBus):
            raise ValidationError("Disk format or bus is not valid.")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise ValidationError("Disk size must be an integer number of bytes.")
        if not 16 * 1024**2 <= self.size_bytes <= 16 * 1024**4:
            raise ValidationError("Disk size must be between 16 MiB and 16 TiB.")
        expected_suffix = f".{self.format.value}"
        if (
            not self.filename
            or "/" in self.filename
            or "\\" in self.filename
            or self.filename in {".", ".."}
            or not self.filename.lower().endswith(expected_suffix)
        ):
            raise ValidationError(f"Disk file name must be a plain file name ending in {expected_suffix}.")
        if self.role not in {"system", "data"}:
            raise ValidationError("Disk role must be 'system' or 'data'.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "format": self.format.value,
            "bus": self.bus.value,
            "size_bytes": self.size_bytes,
            "preallocated": self.preallocated,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, data: Any, *, context: str = "disk") -> DiskSpec:
        mapping = _mapping(data, context)
        _check_keys(
            mapping, {"id", "filename", "format", "bus", "size_bytes", "preallocated", "role"}, context
        )
        disk_id = _str(mapping, "id", context=context)
        if not is_uuid(disk_id):
            raise ValidationError(f"{context}.id must be a UUID.")
        return cls(
            id=disk_id,
            filename=_str(mapping, "filename", context=context, max_length=255),
            format=_enum(DiskFormat, mapping.get("format", DiskFormat.QCOW2.value), context=f"{context}.format"),
            bus=_enum(DiskBus, mapping.get("bus", DiskBus.VIRTIO.value), context=f"{context}.bus"),
            size_bytes=_int(mapping, "size_bytes", context=context, minimum=16 * 1024**2),
            preallocated=_bool(mapping, "preallocated", context=context),
            role=_str(mapping, "role", context=context),
        )


@dataclass(frozen=True)
class DisplaySpec:
    adapter: GraphicsAdapter = GraphicsAdapter.STD
    vnc_enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"adapter": self.adapter.value, "vnc_enabled": self.vnc_enabled}

    @classmethod
    def from_dict(cls, data: Any) -> DisplaySpec:
        mapping = _mapping(data, "display")
        _check_keys(mapping, {"adapter", "vnc_enabled"}, "display")
        return cls(
            adapter=_enum(GraphicsAdapter, mapping.get("adapter", GraphicsAdapter.STD.value), context="display.adapter"),
            vnc_enabled=_bool(mapping, "vnc_enabled", context="display", default=True),
        )


@dataclass(frozen=True)
class SharedFolderSpec:
    path: str
    tag: str
    readonly: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "tag": self.tag, "readonly": self.readonly}

    @classmethod
    def from_dict(cls, data: Any) -> SharedFolderSpec:
        mapping = _mapping(data, "shared_folder")
        _check_keys(mapping, {"path", "tag", "readonly"}, "shared_folder")
        return cls(
            path=_str(mapping, "path", context="shared_folder"),
            tag=_str(mapping, "tag", context="shared_folder", max_length=32),
            readonly=_bool(mapping, "readonly", context="shared_folder", default=True),
        )


@dataclass
class VMSpec:
    id: str
    name: str
    guest_os: GuestOS = GuestOS.GENERIC
    description: str = ""
    cpu: CpuSpec = field(default_factory=CpuSpec)
    memory_mib: int = 2048
    firmware: Firmware = Firmware.BIOS
    machine: str = "q35"
    accelerator: str | None = None
    disks: list[DiskSpec] = field(default_factory=list)
    cdrom_iso: str | None = None
    boot_order: list[BootDevice] = field(default_factory=lambda: [BootDevice.DISK])
    nics: list[NicSpec] = field(default_factory=list)
    display: DisplaySpec = field(default_factory=DisplaySpec)
    shared_folders: list[SharedFolderSpec] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    revision: int = 1
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not is_uuid(self.id):
            raise ValidationError("Virtual machine id must be a UUID.")
        if isinstance(self.memory_mib, bool) or not isinstance(self.memory_mib, int) or self.memory_mib <= 0:
            raise ValidationError("Memory must be a positive number of MiB.")
        if not isinstance(self.guest_os, GuestOS) or not isinstance(self.firmware, Firmware):
            raise ValidationError("Guest operating system or firmware is not valid.")
        if not self.machine:
            raise ValidationError("Machine type must not be empty.")

    @property
    def vcpus(self) -> int:
        return self.cpu.vcpus

    @property
    def total_disk_bytes(self) -> int:
        return sum(disk.size_bytes for disk in self.disks)

    @property
    def system_disk(self) -> DiskSpec | None:
        for disk in self.disks:
            if disk.role == "system":
                return disk
        return self.disks[0] if self.disks else None

    def disk_by_id(self, disk_id: str) -> DiskSpec | None:
        return next((disk for disk in self.disks if disk.id == disk_id), None)

    def touch(self) -> None:
        self.updated_at = utc_now_iso()
        self.revision += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "guest_os": self.guest_os.value,
            "cpu": self.cpu.to_dict(),
            "memory_mib": self.memory_mib,
            "firmware": self.firmware.value,
            "machine": self.machine,
            "accelerator": self.accelerator,
            "disks": [disk.to_dict() for disk in self.disks],
            "cdrom_iso": self.cdrom_iso,
            "boot_order": [device.value for device in self.boot_order],
            "nics": [nic.to_dict() for nic in self.nics],
            "display": self.display.to_dict(),
            "shared_folders": [folder.to_dict() for folder in self.shared_folders],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: Any) -> VMSpec:
        mapping = _mapping(data, "vm")
        _check_keys(
            mapping,
            {
                "schema_version",
                "id",
                "name",
                "description",
                "guest_os",
                "cpu",
                "memory_mib",
                "firmware",
                "machine",
                "accelerator",
                "disks",
                "cdrom_iso",
                "boot_order",
                "nics",
                "display",
                "shared_folders",
                "created_at",
                "updated_at",
                "revision",
            },
            "vm",
        )
        schema_version = _int(mapping, "schema_version", context="vm", minimum=1)
        if schema_version > SCHEMA_VERSION:
            raise ValidationError(
                f"This virtual machine uses a newer configuration format (version {schema_version}).",
                hint="Upgrade OpenQVM to open it.",
            )
        vm_id = _str(mapping, "id", context="vm")
        if not is_uuid(vm_id):
            raise ValidationError("vm.id must be a UUID.")
        disks_raw = _list(mapping.get("disks", []), "vm.disks")
        boot_raw = _list(mapping.get("boot_order", [BootDevice.DISK.value]), "vm.boot_order")
        nics_raw = _list(mapping.get("nics", []), "vm.nics")
        folders_raw = _list(mapping.get("shared_folders", []), "vm.shared_folders")
        return cls(
            schema_version=schema_version,
            id=vm_id,
            name=_str(mapping, "name", context="vm", max_length=64),
            description=_opt_str(mapping, "description", context="vm") or "",
            guest_os=_enum(GuestOS, mapping.get("guest_os", GuestOS.GENERIC.value), context="vm.guest_os"),
            cpu=CpuSpec.from_dict(mapping.get("cpu", {})),
            memory_mib=_int(mapping, "memory_mib", context="vm", minimum=1),
            firmware=_enum(Firmware, mapping.get("firmware", Firmware.BIOS.value), context="vm.firmware"),
            machine=_str(mapping, "machine", context="vm", max_length=32),
            accelerator=_opt_str(mapping, "accelerator", context="vm"),
            disks=[DiskSpec.from_dict(item, context=f"vm.disks[{index}]") for index, item in enumerate(disks_raw)],
            cdrom_iso=_opt_str(mapping, "cdrom_iso", context="vm") or None,
            boot_order=[_enum(BootDevice, item, context="vm.boot_order") for item in boot_raw],
            nics=[NicSpec.from_dict(item) for item in nics_raw],
            display=DisplaySpec.from_dict(mapping.get("display", {})),
            shared_folders=[SharedFolderSpec.from_dict(item) for item in folders_raw],
            created_at=parse_timestamp(_str(mapping, "created_at", context="vm"), field_name="vm.created_at"),
            updated_at=parse_timestamp(_str(mapping, "updated_at", context="vm"), field_name="vm.updated_at"),
            revision=_int(mapping, "revision", context="vm", minimum=1),
        )

    def clone_with_identity(self, new_name: str) -> VMSpec:
        """Return an independent copy with new identifiers, MACs and disk names."""

        disks = [
            DiskSpec(
                id=new_id(),
                filename=f"{disk.role}-{new_id()[:8]}.{disk.format.value}",
                format=disk.format,
                bus=disk.bus,
                size_bytes=disk.size_bytes,
                preallocated=disk.preallocated,
                role=disk.role,
            )
            for disk in self.disks
        ]
        # Port forwards bind host ports and are therefore not copied: a clone would
        # collide with its source.  The caller can add forwards to the clone later.
        nics = [
            NicSpec(
                id=new_id(),
                mode=nic.mode,
                model=nic.model,
                mac=generate_mac(),
                bridge=nic.bridge,
                port_forwards=(),
            )
            for nic in self.nics
        ]
        now = utc_now_iso()
        return VMSpec(
            id=new_id(),
            name=new_name,
            guest_os=self.guest_os,
            description=self.description,
            cpu=self.cpu,
            memory_mib=self.memory_mib,
            firmware=self.firmware,
            machine=self.machine,
            accelerator=self.accelerator,
            disks=disks,
            cdrom_iso=self.cdrom_iso,
            boot_order=list(self.boot_order),
            nics=nics,
            display=self.display,
            shared_folders=list(self.shared_folders),
            created_at=now,
            updated_at=now,
            revision=1,
        )


@dataclass
class VMRuntime:
    vm_id: str
    status: VMStatus = VMStatus.STOPPED
    pid: int | None = None
    process_create_time: float | None = None
    qmp_port: int | None = None
    vnc_port: int | None = None
    accelerator: str | None = None
    started_at: str | None = None
    stopped_at: str | None = None
    last_error: str | None = None
    last_error_code: str | None = None
    schema_version: int = SCHEMA_VERSION

    @property
    def is_active(self) -> bool:
        return self.status.is_active

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "vm_id": self.vm_id,
            "status": self.status.value,
            "pid": self.pid,
            "process_create_time": self.process_create_time,
            "qmp_port": self.qmp_port,
            "vnc_port": self.vnc_port,
            "accelerator": self.accelerator,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "last_error": self.last_error,
            "last_error_code": self.last_error_code,
        }

    @classmethod
    def from_dict(cls, data: Any) -> VMRuntime:
        mapping = _mapping(data, "runtime")
        return cls(
            vm_id=_str(mapping, "vm_id", context="runtime"),
            status=_enum(VMStatus, mapping.get("status", VMStatus.STOPPED.value), context="runtime.status"),
            pid=mapping.get("pid"),
            process_create_time=mapping.get("process_create_time"),
            qmp_port=mapping.get("qmp_port"),
            vnc_port=mapping.get("vnc_port"),
            accelerator=mapping.get("accelerator"),
            started_at=mapping.get("started_at"),
            stopped_at=mapping.get("stopped_at"),
            last_error=mapping.get("last_error"),
            last_error_code=mapping.get("last_error_code"),
            schema_version=_int(mapping, "schema_version", context="runtime", minimum=1),
        )


@dataclass
class SnapshotRecord:
    name: str
    description: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    includes_memory: bool = False
    vm_state: str = "stopped"
    disks: list[str] = field(default_factory=list)

    @property
    def tag(self) -> str:
        return self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "includes_memory": self.includes_memory,
            "vm_state": self.vm_state,
            "disks": list(self.disks),
        }

    @classmethod
    def from_dict(cls, data: Any) -> SnapshotRecord:
        mapping = _mapping(data, "snapshot")
        _check_keys(mapping, {"name", "description", "created_at", "includes_memory", "vm_state", "disks"}, "snapshot")
        return cls(
            name=_str(mapping, "name", context="snapshot"),
            description=_opt_str(mapping, "description", context="snapshot") or "",
            created_at=parse_timestamp(
                mapping.get("created_at") or utc_now_iso(), field_name="snapshot.created_at"
            ),
            includes_memory=_bool(mapping, "includes_memory", context="snapshot"),
            vm_state=_str(mapping, "vm_state", context="snapshot"),
            disks=[str(item) for item in _list(mapping.get("disks", []), "snapshot.disks")],
        )


@dataclass
class OperationRecord:
    id: str
    action: str
    status: OperationStatus = OperationStatus.RUNNING
    vm_id: str | None = None
    vm_name: str = ""
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None
    message: str = ""
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "status": self.status.value,
            "vm_id": self.vm_id,
            "vm_name": self.vm_name,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "message": self.message,
            "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, data: Any) -> OperationRecord:
        mapping = _mapping(data, "operation")
        return cls(
            id=_str(mapping, "id", context="operation"),
            action=_str(mapping, "action", context="operation"),
            status=_enum(OperationStatus, mapping.get("status", "running"), context="operation.status"),
            vm_id=mapping.get("vm_id"),
            vm_name=mapping.get("vm_name") or "",
            started_at=mapping.get("started_at") or utc_now_iso(),
            finished_at=mapping.get("finished_at"),
            message=mapping.get("message") or "",
            error_code=mapping.get("error_code"),
        )


_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def normalize_mac(mac: str) -> str:
    value = mac.strip().lower().replace("-", ":")
    if not _MAC_RE.match(value):
        raise ValidationError("The MAC address must use the form aa:bb:cc:dd:ee:ff.")
    octets = [int(part, 16) for part in value.split(":")]
    if octets[0] & 0x01:
        raise ValidationError("The MAC address must be a unicast address.")
    return value


def generate_mac() -> str:
    random_source = random.SystemRandom()
    # 52:54:00 is QEMU's OUI; the local bit is set so the address is valid on a LAN.
    return f"52:54:00:{random_source.randrange(256):02x}:{random_source.randrange(256):02x}:{random_source.randrange(256):02x}"
