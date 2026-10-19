"""QEMU command-line construction.

The builder is pure and fully testable.  It never uses a shell and every value
that becomes part of a QEMU option is validated or escaped.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path

from openqvm.core.errors import BackendUnavailableError, ValidationError
from openqvm.core.models import BootDevice, DiskBus, GraphicsAdapter, VMSpec
from openqvm.core.paths import AppPaths
from openqvm.networking import build_netdev_arguments
from openqvm.platform.capabilities import PlatformCapabilities
from openqvm.vm.qemu.medium import CDROM_DEVICE_ID

VNC_HOST = "127.0.0.1"
QMP_HOST = "127.0.0.1"


@dataclass(frozen=True)
class LaunchPlan:
    argv: list[str]
    accelerator: str
    qmp_port: int
    vnc_port: int | None
    vnc_display: int | None
    serial_log: Path
    qemu_log: Path
    pid_file: Path
    firmware_vars: Path | None


def escape_option(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValidationError("A command-line value contains control characters.")
    return value.replace(",", ",,")


def allocate_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((QMP_HOST, 0))
        return int(sock.getsockname()[1])


def allocate_vnc_display() -> tuple[int, int]:
    """Find a free VNC display (10-99) and return ``(display, port)``."""

    for display in range(10, 100):
        port = 5900 + display
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((VNC_HOST, port))
            except OSError:
                continue
            return display, port
    raise BackendUnavailableError(
        "No free VNC display is available.",
        hint="Close unused virtual machines or console viewers and try again.",
    )


def _boot_order(spec: VMSpec) -> str:
    mapping = {BootDevice.DISK: "c", BootDevice.CDROM: "d", BootDevice.NETWORK: "n"}
    return "".join(mapping[device] for device in spec.boot_order) or "c"


def _disk_arguments(spec: VMSpec, paths: AppPaths) -> list[str]:
    arguments: list[str] = []
    for index, disk in enumerate(spec.disks):
        disk_path = paths.vm_disk_dir(spec.id) / disk.filename
        common = f"file={escape_option(str(disk_path))},format={disk.format.value},cache=writeback"
        if disk.bus is DiskBus.VIRTIO:
            arguments.extend(
                [
                    "-drive",
                    f"{common},if=none,id=drive{index},discard=unmap",
                    "-device",
                    f"virtio-blk-pci,drive=drive{index}",
                ]
            )
        else:
            arguments.extend(["-drive", f"{common},if=ide,index={index},id=drive{index}"])
    return arguments


def _input_arguments(spec: VMSpec) -> list[str]:
    """Attach an absolute pointing device so VNC mouse input does not drift.

    q35 does not create a USB bus implicitly, so an xHCI controller is added.
    The legacy ``pc`` machine only needs ``-usb`` to enable its UHCI controller.
    """

    if spec.machine == "q35":
        return ["-device", "qemu-xhci", "-device", "usb-tablet"]
    return ["-usb", "-device", "usb-tablet"]


def _cdrom_arguments(spec: VMSpec) -> list[str]:
    """Always provide an IDE CD-ROM so media can be changed while running.

    The drive is empty when no image is attached; QEMU then skips it during boot
    and the tray can be loaded later over QMP.
    """

    options = ["if=none", f"id={CDROM_DEVICE_ID}", "media=cdrom", "readonly=on"]
    if spec.cdrom_iso:
        iso = Path(spec.cdrom_iso)
        if not iso.is_file():
            raise ValidationError(
                "The attached installation image is missing.",
                hint="Attach an existing ISO file or eject the current one.",
            )
        options.extend([f"file={escape_option(str(iso))}", "format=raw"])
    return ["-drive", ",".join(options), "-device", f"ide-cd,drive={CDROM_DEVICE_ID}"]


def _firmware_arguments(spec: VMSpec, capabilities: PlatformCapabilities, variables: Path | None) -> list[str]:
    if spec.firmware.value != "uefi":
        return []
    if capabilities.uefi is None or variables is None:
        raise BackendUnavailableError(
            "UEFI firmware is not available.",
            hint="Install edk2/OVMF firmware or change the VM to BIOS boot.",
        )
    return [
        "-drive",
        f"if=pflash,format=raw,readonly=on,file={escape_option(str(capabilities.uefi.code_path))}",
        "-drive",
        f"if=pflash,format=raw,file={escape_option(str(variables))}",
    ]


def build_launch_plan(
    spec: VMSpec,
    paths: AppPaths,
    capabilities: PlatformCapabilities,
    *,
    qmp_port: int,
    vnc_display: int | None,
    vnc_port: int | None,
    firmware_vars: Path | None = None,
    tpm_port: int | None = None,
) -> LaunchPlan:
    if capabilities.qemu is None:
        raise BackendUnavailableError(
            "QEMU is not available.",
            hint="Install QEMU and restart OpenQVM, or configure the QEMU directory in Settings.",
        )
    accelerator = spec.accelerator or capabilities.default_accelerator
    if not accelerator:
        raise BackendUnavailableError(
            "No QEMU accelerator is available.",
            hint="Install and enable KVM, WHPX or HVF, or allow software emulation (TCG).",
        )
    reported = capabilities.qemu.accelerators
    if reported and accelerator not in reported and accelerator != "tcg":
        raise BackendUnavailableError(
            f"The accelerator '{accelerator}' is not supported by this QEMU build.",
            hint=f"Available accelerators: {', '.join(reported)}.",
        )

    serial_log = paths.vm_serial_log(spec.id)
    qemu_log = paths.vm_log_file(spec.id)
    pid_file = paths.vm_dir(spec.id) / "qemu.pid"

    argv: list[str] = [
        str(capabilities.qemu.system_path),
        "-name",
        escape_option(spec.name),
        "-machine",
        f"{spec.machine},accel={accelerator}",
        "-m",
        str(spec.memory_mib),
        "-smp",
        f"cpus={spec.cpu.vcpus},sockets={spec.cpu.sockets},cores={spec.cpu.cores},threads={spec.cpu.threads}",
        "-uuid",
        spec.id,
        # Windows guests keep the RTC in local time by default; Linux guests expect UTC.
        "-rtc",
        f"base={'localtime' if spec.guest_os.is_windows else 'utc'}",
        "-boot",
        f"order={_boot_order(spec)},menu=off",
        "-no-user-config",
        "-monitor",
        "none",
        "-qmp",
        f"tcp:{QMP_HOST}:{qmp_port},server=on,wait=off",
        "-pidfile",
        escape_option(str(pid_file)),
    ]

    argv.extend(_firmware_arguments(spec, capabilities, firmware_vars))
    argv.extend(_disk_arguments(spec, paths))
    argv.extend(_cdrom_arguments(spec))

    for index, nic in enumerate(spec.nics):
        netdev, device = build_netdev_arguments(nic, index)
        argv.extend(["-netdev", netdev, "-device", device])

    for folder in spec.shared_folders:
        options = (
            f"local,path={escape_option(folder.path)},mount_tag={escape_option(folder.tag)},"
            f"security_model=mapped-xattr,readonly={'on' if folder.readonly else 'off'}"
        )
        argv.extend(["-virtfs", options])

    if spec.tpm:
        if tpm_port is None:
            raise BackendUnavailableError(
                "TPM 2.0 was requested but the TPM emulator is not running.",
                hint="Start the virtual machine again so OpenQVM can launch swtpm.",
            )
        argv.extend(
            [
                "-chardev",
                f"socket,id=chrtpm,host=127.0.0.1,port={tpm_port}",
                "-tpmdev",
                "emulator,id=tpm0,chardev=chrtpm",
                "-device",
                "tpm-tis,tpmdev=tpm0",
            ]
        )

    if spec.display.vnc_enabled and vnc_display is not None:
        argv.extend(["-display", f"vnc={VNC_HOST}:{vnc_display}"])
        argv.extend(["-vga", "std" if spec.display.adapter is GraphicsAdapter.STD else "virtio"])
    else:
        argv.extend(["-display", "none"])

    argv.extend(_input_arguments(spec))

    argv.extend(
        [
            "-chardev",
            f"file,id=serial0,path={escape_option(str(serial_log))}",
            "-serial",
            "chardev:serial0",
        ]
    )

    return LaunchPlan(
        argv=argv,
        accelerator=accelerator,
        qmp_port=qmp_port,
        vnc_port=vnc_port,
        vnc_display=vnc_display,
        serial_log=serial_log,
        qemu_log=qemu_log,
        pid_file=pid_file,
        firmware_vars=firmware_vars,
    )
