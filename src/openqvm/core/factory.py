"""Factory for new VM definitions with sensible, host-aware defaults."""

from __future__ import annotations

from openqvm.core.defaults import recommended_defaults
from openqvm.core.ids import new_id
from openqvm.core.models import (
    BootDevice,
    CpuSpec,
    DiskBus,
    DiskFormat,
    DiskSpec,
    DisplaySpec,
    Firmware,
    GuestOS,
    NetworkMode,
    NicModel,
    NicSpec,
    PortForward,
    VMSpec,
    generate_mac,
    utc_now_iso,
)
from openqvm.core.validation import HostResources


def create_vm_spec(
    *,
    name: str,
    guest_os: GuestOS,
    host: HostResources,
    vcpus: int | None = None,
    memory_mib: int | None = None,
    disk_bytes: int | None = None,
    disk_format: DiskFormat = DiskFormat.QCOW2,
    disk_bus: DiskBus | None = None,
    preallocated: bool = False,
    firmware: Firmware | None = None,
    machine: str | None = None,
    cdrom_iso: str | None = None,
    network_mode: NetworkMode | None = NetworkMode.NAT,
    nic_model: NicModel | None = None,
    bridge: str | None = None,
    port_forwards: list[PortForward] | None = None,
    description: str = "",
    accelerator: str | None = None,
) -> VMSpec:
    defaults = recommended_defaults(guest_os, host)
    disk_id = new_id()
    disks = [
        DiskSpec(
            id=disk_id,
            filename=f"system-{disk_id[:8]}.{disk_format.value}",
            format=disk_format,
            bus=disk_bus or defaults.disk_bus,
            size_bytes=disk_bytes or defaults.disk_bytes,
            preallocated=preallocated,
            role="system",
        )
    ]
    nics = []
    if network_mode is not None:
        nics.append(
            NicSpec(
                id=new_id(),
                mode=network_mode,
                model=nic_model or defaults.nic_model,
                mac=generate_mac(),
                bridge=bridge,
                port_forwards=tuple(port_forwards or ()),
            )
        )
    boot_order = [BootDevice.DISK]
    if cdrom_iso:
        boot_order = [BootDevice.CDROM, BootDevice.DISK]
    now = utc_now_iso()
    return VMSpec(
        id=new_id(),
        name=name,
        guest_os=guest_os,
        description=description,
        cpu=CpuSpec(cores=vcpus or defaults.vcpus),
        memory_mib=memory_mib or defaults.memory_mib,
        firmware=firmware or defaults.firmware,
        machine=machine or defaults.machine,
        accelerator=accelerator,
        disks=disks,
        cdrom_iso=cdrom_iso,
        boot_order=boot_order,
        nics=nics,
        display=DisplaySpec(),
        shared_folders=[],
        created_at=now,
        updated_at=now,
        revision=1,
    )
