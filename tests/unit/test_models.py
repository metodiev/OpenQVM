"""Domain model serialization and invariants."""

from __future__ import annotations

import copy

import pytest

from openqvm.core.errors import ValidationError
from openqvm.core.factory import create_vm_spec
from openqvm.core.models import (
    SCHEMA_VERSION,
    DiskFormat,
    GuestOS,
    NicSpec,
    PortForward,
    VMRuntime,
    VMSpec,
    VMStatus,
    generate_mac,
    normalize_mac,
)


def test_spec_roundtrip(host):
    spec = create_vm_spec(
        name="Roundtrip VM",
        guest_os=GuestOS.UBUNTU,
        host=host,
        cdrom_iso=None,
        network_mode=None,
    )
    restored = VMSpec.from_dict(spec.to_dict())
    assert restored == spec
    assert restored.schema_version == SCHEMA_VERSION


def test_spec_rejects_unknown_fields(host):
    spec = create_vm_spec(name="Strict VM", guest_os=GuestOS.LINUX, host=host)
    data = spec.to_dict()
    data["surprise"] = True
    with pytest.raises(ValidationError, match="unsupported fields"):
        VMSpec.from_dict(data)


def test_spec_rejects_future_schema(host):
    spec = create_vm_spec(name="Future VM", guest_os=GuestOS.LINUX, host=host)
    data = spec.to_dict()
    data["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(ValidationError, match="newer configuration format"):
        VMSpec.from_dict(data)


def test_spec_requires_uuid(host):
    spec = create_vm_spec(name="Bad ID", guest_os=GuestOS.LINUX, host=host)
    data = spec.to_dict()
    data["id"] = "not-a-uuid"
    with pytest.raises(ValidationError):
        VMSpec.from_dict(data)


def test_clone_identity_is_fresh(host):
    spec = create_vm_spec(
        name="Original",
        guest_os=GuestOS.LINUX,
        host=host,
        port_forwards=[PortForward(host_port=2222, guest_port=22)],
    )
    clone = spec.clone_with_identity("Copy")
    assert clone.id != spec.id
    assert clone.name == "Copy"
    assert clone.disks[0].id != spec.disks[0].id
    assert clone.disks[0].filename != spec.disks[0].filename
    assert clone.nics[0].mac != spec.nics[0].mac
    assert clone.nics[0].port_forwards == ()


def test_mac_validation():
    assert normalize_mac("52-54-00-AB-CD-EF") == "52:54:00:ab:cd:ef"
    with pytest.raises(ValidationError):
        normalize_mac("not-a-mac")
    with pytest.raises(ValidationError):
        normalize_mac("53:54:00:ab:cd:ef")  # multicast bit


def test_generated_macs_are_local_unicast():
    for _ in range(20):
        mac = generate_mac()
        first = int(mac.split(":")[0], 16)
        assert first & 0x01 == 0
        assert first & 0x02 == 0x02


def test_nic_rejects_forward_on_bridged():
    with pytest.raises(ValidationError):
        NicSpec(id="6f0c6b5a-5e80-4f88-9c0b-2a1b8f1e5c11", mode="bridged", bridge="br0", mac=generate_mac())


def test_runtime_roundtrip():
    runtime = VMRuntime(vm_id="6f0c6b5a-5e80-4f88-9c0b-2a1b8f1e5c11", status=VMStatus.RUNNING, pid=1234, qmp_port=5000)
    assert VMRuntime.from_dict(runtime.to_dict()) == runtime


def test_disk_filename_traversal_rejected(host):
    spec = create_vm_spec(name="Disk VM", guest_os=GuestOS.LINUX, host=host)
    data = spec.to_dict()
    data["disks"][0]["filename"] = "../escape.qcow2"
    with pytest.raises(ValidationError):
        VMSpec.from_dict(data)


def test_raw_disk_format_roundtrip(host):
    spec = create_vm_spec(
        name="Raw VM",
        guest_os=GuestOS.LINUX,
        host=host,
        disk_format=DiskFormat.RAW,
    )
    restored = VMSpec.from_dict(copy.deepcopy(spec.to_dict()))
    assert restored.disks[0].format is DiskFormat.RAW

def test_vm_spec_carries_the_tpm_flag():
    from openqvm.core.factory import create_vm_spec
    from openqvm.core.models import GuestOS, VMSpec
    from openqvm.core.validation import HostResources

    spec = create_vm_spec(
        name="TPM VM", guest_os=GuestOS.LINUX, host=HostResources(cpu_count=4, memory_total_mib=8192)
    )
    assert VMSpec.from_dict(spec.to_dict()).tpm is False
    spec.tpm = True
    assert VMSpec.from_dict(spec.to_dict()).tpm is True
    clone = spec.clone_with_identity("TPM clone")
    assert clone.tpm is True
