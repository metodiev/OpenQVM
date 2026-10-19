"""Validation rules and cross-VM constraints."""

from __future__ import annotations

from dataclasses import replace

from openqvm.core.factory import create_vm_spec
from openqvm.core.models import (
    BootDevice,
    Firmware,
    GuestOS,
    NetworkMode,
    NicSpec,
    PortForward,
)
from openqvm.core.validation import (
    HostResources,
    validate_disk_resize,
    validate_snapshot_name,
    validate_vm_name,
    validate_vm_spec,
)

HOST = HostResources(cpu_count=8, memory_total_mib=16384)


def _errors(issues):
    return [issue for issue in issues if issue.is_error]


def test_name_validation():
    assert not _errors(validate_vm_name("Ubuntu Workstation"))
    assert _errors(validate_vm_name(""))
    assert _errors(validate_vm_name(" leading"))
    assert _errors(validate_vm_name("bad/name"))
    assert _errors(validate_vm_name("CON"))
    assert _errors(validate_vm_name("x" * 65))


def test_resources_are_bounded():
    spec = create_vm_spec(name="Big VM", guest_os=GuestOS.LINUX, host=HOST, vcpus=64, memory_mib=20000)
    issues = validate_vm_spec(spec, host=HOST)
    fields = {issue.field for issue in _errors(issues)}
    assert "cpu" in fields
    assert "memory_mib" in fields


def test_duplicate_name_and_mac_detected():
    first = create_vm_spec(name="Dup", guest_os=GuestOS.LINUX, host=HOST)
    second = create_vm_spec(name="Dup", guest_os=GuestOS.LINUX, host=HOST)
    second.nics[0] = replace(second.nics[0], mac=first.nics[0].mac)
    issues = validate_vm_spec(second, host=HOST, existing_specs=[first])
    messages = " ".join(issue.message for issue in _errors(issues))
    assert "already named" in messages
    assert "MAC address" in messages


def test_duplicate_host_ports_detected():
    spec = create_vm_spec(
        name="Ports",
        guest_os=GuestOS.LINUX,
        host=HOST,
        port_forwards=[
            PortForward(host_port=2222, guest_port=22),
            PortForward(host_port=2222, guest_port=23),
        ],
    )
    issues = validate_vm_spec(spec, host=HOST)
    assert any("already forwarded" in issue.message for issue in _errors(issues))


def test_missing_iso_is_an_error(host=None):
    spec = create_vm_spec(name="ISO VM", guest_os=GuestOS.LINUX, host=HOST)
    spec.cdrom_iso = "C:/definitely-does-not-exist/installer.iso"
    issues = validate_vm_spec(spec, host=HOST)
    assert any(issue.field == "cdrom_iso" and issue.is_error for issue in issues)


def test_cdrom_boot_without_iso_is_warning():
    spec = create_vm_spec(name="Boot VM", guest_os=GuestOS.LINUX, host=HOST)
    spec.boot_order = [BootDevice.CDROM, BootDevice.DISK]
    issues = validate_vm_spec(spec, host=HOST)
    cdrom_issue = next(issue for issue in issues if issue.field == "boot_order")
    assert cdrom_issue.severity == "warning"


def test_uefi_requires_q35():
    spec = create_vm_spec(name="UEFI VM", guest_os=GuestOS.LINUX, host=HOST)
    spec.firmware = Firmware.UEFI
    spec.machine = "pc"
    issues = validate_vm_spec(spec, host=HOST)
    assert any(issue.field == "firmware" and issue.is_error for issue in issues)


def test_shared_folder_must_exist(tmp_path):
    from openqvm.core.models import SharedFolderSpec

    spec = create_vm_spec(name="Share VM", guest_os=GuestOS.LINUX, host=HOST)
    spec.shared_folders = [SharedFolderSpec(path=str(tmp_path / "missing"), tag="share")]
    issues = validate_vm_spec(spec, host=HOST)
    assert any("does not exist" in issue.message for issue in _errors(issues))


def test_resize_only_grows():
    spec = create_vm_spec(name="Resize VM", guest_os=GuestOS.LINUX, host=HOST)
    disk = spec.disks[0]
    assert _errors(validate_disk_resize(disk, disk.size_bytes - 1024**3))
    assert not _errors(validate_disk_resize(disk, disk.size_bytes + 1024**3))


def test_snapshot_names():
    assert not validate_snapshot_name("before-upgrade_1")
    assert validate_snapshot_name("with space")
    assert validate_snapshot_name("")


def test_bridged_requires_bridge():
    spec = create_vm_spec(name="Bridge VM", guest_os=GuestOS.LINUX, host=HOST, network_mode=NetworkMode.NAT)
    nic = spec.nics[0]
    spec.nics[0] = NicSpec(id=nic.id, mode=NetworkMode.BRIDGED, model=nic.model, mac=nic.mac, bridge="br0")
    object.__setattr__(spec.nics[0], "bridge", None)
    issues = validate_vm_spec(spec, host=HOST)
    assert any("bridge" in issue.field for issue in _errors(issues))

def test_tpm_requires_a_capable_host(tmp_path):
    from openqvm.platform.capabilities import PlatformCapabilities
    from openqvm.platform.compatibility import check_compatibility
    from openqvm.platform.qemu_discovery import QemuInstall

    qemu = QemuInstall(
        system_path=tmp_path / "qemu-system-x86_64.exe",
        image_path=tmp_path / "qemu-img.exe",
        version="11.0.0",
        accelerators=("tcg",),
        share_dir=None,
        source="test",
    )
    spec = create_vm_spec(name="TPM VM", guest_os=GuestOS.WINDOWS_10, host=HOST)
    spec.tpm = True
    unsupported = PlatformCapabilities(
        os_family="windows",
        architecture="AMD64",
        qemu=qemu,
        accelerators=[],
        default_accelerator="tcg",
        tpm_supported=False,
        tpm_detail="This QEMU build has no TPM support (the -tpmdev option is unavailable).",
    )
    issues = check_compatibility(spec, unsupported)
    assert [issue.field for issue in issues if issue.is_error] == ["tpm"]
    assert "no TPM support" in issues[0].message

    supported = PlatformCapabilities(
        os_family="linux",
        architecture="x86_64",
        qemu=qemu,
        accelerators=[],
        default_accelerator="tcg",
        tpm_supported=True,
        tpm_detail="swtpm found.",
    )
    assert not [issue for issue in check_compatibility(spec, supported) if issue.is_error]
