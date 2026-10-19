"""Sensible per-guest defaults derived from real host resources."""

from __future__ import annotations

from dataclasses import dataclass

from openqvm.core.models import DiskBus, Firmware, GuestOS, NicModel
from openqvm.core.validation import HostResources


@dataclass(frozen=True)
class GuestDefaults:
    vcpus: int
    memory_mib: int
    disk_bytes: int
    disk_bus: DiskBus
    machine: str
    firmware: Firmware
    nic_model: NicModel


def recommended_defaults(guest_os: GuestOS, host: HostResources) -> GuestDefaults:
    logical = max(1, host.cpu_count)
    is_windows = guest_os.is_windows

    if guest_os is GuestOS.WINDOWS_11:
        vcpus = min(4, max(2, logical // 2))
        memory = 4096
        disk = 64 * 1024**3
        bus = DiskBus.IDE
        machine = "q35"
        firmware = Firmware.UEFI
        nic = NicModel.E1000E
    elif guest_os is GuestOS.WINDOWS_10:
        vcpus = min(4, max(2, logical // 2))
        memory = 4096
        disk = 64 * 1024**3
        bus = DiskBus.IDE
        machine = "pc"
        firmware = Firmware.BIOS
        nic = NicModel.E1000E
    elif guest_os in (GuestOS.LINUX, GuestOS.UBUNTU, GuestOS.DEBIAN, GuestOS.FEDORA):
        vcpus = min(2, logical)
        memory = 2048
        disk = 20 * 1024**3
        bus = DiskBus.VIRTIO
        machine = "q35"
        firmware = Firmware.BIOS
        nic = NicModel.VIRTIO
    else:
        vcpus = min(2, logical)
        memory = 2048 if not is_windows else 4096
        disk = 20 * 1024**3
        bus = DiskBus.VIRTIO
        machine = "q35"
        firmware = Firmware.BIOS
        nic = NicModel.VIRTIO

    # Never propose more RAM than the host can comfortably provide.
    if host.memory_total_mib:
        memory = min(memory, max(512, int(host.memory_total_mib * 0.5)))
    vcpus = max(1, min(vcpus, max(1, logical * 2)))
    return GuestDefaults(
        vcpus=vcpus,
        memory_mib=memory,
        disk_bytes=disk,
        disk_bus=bus,
        machine=machine,
        firmware=firmware,
        nic_model=nic,
    )
