"""QEMU command-line construction."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from openqvm.core.errors import BackendUnavailableError, ValidationError
from openqvm.core.factory import create_vm_spec
from openqvm.core.models import (
    DiskBus,
    Firmware,
    GuestOS,
    PortForward,
    SharedFolderSpec,
)
from openqvm.core.paths import AppPaths
from openqvm.core.validation import HostResources
from openqvm.platform.capabilities import AcceleratorInfo, PlatformCapabilities, UefiFirmware
from openqvm.platform.qemu_discovery import QemuInstall
from openqvm.vm.qemu.command import allocate_vnc_display, build_launch_plan, escape_option

HOST = HostResources(cpu_count=8, memory_total_mib=16384)


def capabilities(tmp_path: Path, *, uefi: bool = False, accelerators=("tcg", "whpx")) -> PlatformCapabilities:
    qemu_dir = tmp_path / "qemu"
    qemu_dir.mkdir(exist_ok=True)
    system = qemu_dir / "qemu-system-x86_64.exe"
    image = qemu_dir / "qemu-img.exe"
    system.write_bytes(b"fake")
    image.write_bytes(b"fake")
    install = QemuInstall(
        system_path=system,
        image_path=image,
        version="11.0.0",
        accelerators=tuple(accelerators),
        share_dir=qemu_dir,
        source=str(qemu_dir),
    )
    uefi_info = None
    if uefi:
        code = qemu_dir / "edk2-x86_64-code.fd"
        variables = qemu_dir / "edk2-i386-vars.fd"
        code.write_bytes(b"code")
        variables.write_bytes(b"vars")
        uefi_info = UefiFirmware(code_path=code, vars_template=variables)
    return PlatformCapabilities(
        os_family="windows",
        architecture="AMD64",
        qemu=install,
        accelerators=[AcceleratorInfo(name, True) for name in accelerators],
        default_accelerator=accelerators[0],
        uefi=uefi_info,
        bridged_supported=True,
        host_only_supported=False,
        shared_folders_supported=True,
    )


def make_plan(tmp_path, spec, capabilities_=None):
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    paths.vm_disk_dir(spec.id).mkdir(parents=True, exist_ok=True)
    for disk in spec.disks:
        paths.vm_disk_dir(spec.id).joinpath(disk.filename).write_bytes(b"disk")
    return build_launch_plan(
        spec,
        paths,
        capabilities_ or capabilities(tmp_path),
        qmp_port=4444,
        vnc_display=10,
        vnc_port=5910,
        firmware_vars=None,
    )


def argv_value(argv: list[str], option: str) -> str:
    return argv[argv.index(option) + 1]


def test_basic_plan_builds_expected_options(tmp_path):
    spec = create_vm_spec(name="Plan VM", guest_os=GuestOS.LINUX, host=HOST, vcpus=2, memory_mib=1024)
    plan = make_plan(tmp_path, spec)
    argv = plan.argv
    assert argv_value(argv, "-machine") == "q35,accel=tcg"
    assert argv_value(argv, "-m") == "1024"
    assert argv_value(argv, "-smp") == "cpus=2,sockets=1,cores=2,threads=1"
    assert argv_value(argv, "-uuid") == spec.id
    assert argv_value(argv, "-qmp").startswith("tcp:127.0.0.1:4444")
    assert argv_value(argv, "-display") == "vnc=127.0.0.1:10"
    assert argv_value(argv, "-vga") == "std"
    assert argv_value(argv, "-boot").startswith("order=c")
    assert plan.accelerator == "tcg"
    assert "-cdrom" not in argv


def test_iso_and_boot_order(tmp_path):
    iso = tmp_path / "installer.iso"
    iso.write_bytes(b"x" * 40000)
    spec = create_vm_spec(
        name="ISO VM", guest_os=GuestOS.LINUX, host=HOST, cdrom_iso=str(iso)
    )
    plan = make_plan(tmp_path, spec)
    drive_values = [plan.argv[i + 1] for i, arg in enumerate(plan.argv) if arg == "-drive"]
    cdrom = next(value for value in drive_values if "id=cdrom0" in value)
    assert f"file={iso}" in cdrom
    assert "format=raw" in cdrom
    assert "readonly=on" in cdrom
    device_values = [plan.argv[i + 1] for i, arg in enumerate(plan.argv) if arg == "-device"]
    assert "ide-cd,drive=cdrom0" in device_values
    assert argv_value(plan.argv, "-boot") == "order=dc,menu=off"


def test_cdrom_drive_is_always_present(tmp_path):
    spec = create_vm_spec(name="No ISO VM", guest_os=GuestOS.LINUX, host=HOST)
    plan = make_plan(tmp_path, spec)
    drive_values = [plan.argv[i + 1] for i, arg in enumerate(plan.argv) if arg == "-drive"]
    cdrom = next(value for value in drive_values if "id=cdrom0" in value)
    assert "media=cdrom" in cdrom
    assert "file=" not in cdrom
    device_values = [plan.argv[i + 1] for i, arg in enumerate(plan.argv) if arg == "-device"]
    assert "ide-cd,drive=cdrom0" in device_values
    assert "-cdrom" not in plan.argv


def test_ide_disk_uses_ide_and_has_block_id(tmp_path):
    spec = create_vm_spec(name="IDE VM", guest_os=GuestOS.WINDOWS_10, host=HOST, disk_bus=DiskBus.IDE, machine="pc")
    plan = make_plan(tmp_path, spec)
    drive_args = [plan.argv[i + 1] for i, arg in enumerate(plan.argv) if arg == "-drive"]
    assert any("if=ide,index=0,id=drive0" in value for value in drive_args)
    assert argv_value(plan.argv, "-machine") == "pc,accel=tcg"


def test_nat_port_forward(tmp_path):
    spec = create_vm_spec(
        name="NAT VM",
        guest_os=GuestOS.LINUX,
        host=HOST,
        port_forwards=[PortForward(protocol="tcp", host_port=2222, guest_port=22)],
    )
    plan = make_plan(tmp_path, spec)
    netdev = argv_value(plan.argv, "-netdev")
    assert "hostfwd=tcp:127.0.0.1:2222-:22" in netdev
    devices = [plan.argv[index + 1] for index, arg in enumerate(plan.argv) if arg == "-device"]
    device = next(value for value in devices if value.startswith("virtio-net-pci"))
    assert device.startswith("virtio-net-pci,netdev=net0")
    assert f"mac={spec.nics[0].mac}" in device


def test_uefi_requires_firmware(tmp_path):
    spec = create_vm_spec(name="UEFI VM", guest_os=GuestOS.LINUX, host=HOST, firmware=Firmware.UEFI)
    with pytest.raises(BackendUnavailableError):
        make_plan(tmp_path, spec, capabilities(tmp_path, uefi=False))


def test_uefi_adds_pflash(tmp_path):
    spec = create_vm_spec(name="UEFI VM", guest_os=GuestOS.LINUX, host=HOST, firmware=Firmware.UEFI)
    variables = tmp_path / "OVMF_VARS.fd"
    variables.write_bytes(b"vars")
    caps = capabilities(tmp_path, uefi=True)
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    paths.vm_disk_dir(spec.id).mkdir(parents=True)
    for disk in spec.disks:
        paths.vm_disk_dir(spec.id).joinpath(disk.filename).write_bytes(b"disk")
    plan = build_launch_plan(
        spec,
        paths,
        caps,
        qmp_port=4444,
        vnc_display=10,
        vnc_port=5910,
        firmware_vars=variables,
    )
    drive_args = [plan.argv[i + 1] for i, arg in enumerate(plan.argv) if arg == "-drive"]
    assert any("if=pflash" in value and "readonly=on" in value for value in drive_args)
    assert any("if=pflash" in value and str(variables) in value for value in drive_args)


def test_unsupported_accelerator_override(tmp_path):
    spec = create_vm_spec(name="Accel VM", guest_os=GuestOS.LINUX, host=HOST, accelerator="kvm")
    with pytest.raises(BackendUnavailableError):
        make_plan(tmp_path, spec)


def test_vnc_display_allocation():
    display, port = allocate_vnc_display()
    assert 10 <= display <= 99
    assert port == 5900 + display
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def test_escape_option_rejects_control_characters():
    assert escape_option("a,b") == "a,,b"
    with pytest.raises(ValidationError):
        escape_option("bad\nvalue")


def test_shared_folder_arguments(tmp_path):
    folder = tmp_path / "shared"
    folder.mkdir()
    spec = create_vm_spec(name="Share VM", guest_os=GuestOS.LINUX, host=HOST)
    spec.shared_folders = [SharedFolderSpec(path=str(folder), tag="share", readonly=True)]
    plan = make_plan(tmp_path, spec)
    virtfs = argv_value(plan.argv, "-virtfs")
    assert "mount_tag=share" in virtfs
    assert "readonly=on" in virtfs


def test_absolute_pointer_for_q35(tmp_path):
    spec = create_vm_spec(name="Pointer VM", guest_os=GuestOS.LINUX, host=HOST)
    assert spec.machine == "q35"
    plan = make_plan(tmp_path, spec)
    devices = [plan.argv[i + 1] for i, arg in enumerate(plan.argv) if arg == "-device"]
    assert "qemu-xhci" in devices
    assert "usb-tablet" in devices
    assert "-usb" not in plan.argv


def test_absolute_pointer_for_legacy_pc(tmp_path):
    spec = create_vm_spec(name="Pointer PC", guest_os=GuestOS.WINDOWS_10, host=HOST)
    assert spec.machine == "pc"
    plan = make_plan(tmp_path, spec)
    devices = [plan.argv[i + 1] for i, arg in enumerate(plan.argv) if arg == "-device"]
    assert "usb-tablet" in devices
    assert "-usb" in plan.argv


def test_rtc_matches_guest_expectations(tmp_path):
    linux = create_vm_spec(name="Linux RTC", guest_os=GuestOS.LINUX, host=HOST)
    assert argv_value(make_plan(tmp_path, linux).argv, "-rtc") == "base=utc"
    windows = create_vm_spec(name="Windows RTC", guest_os=GuestOS.WINDOWS_10, host=HOST)
    assert argv_value(make_plan(tmp_path, windows).argv, "-rtc") == "base=localtime"
