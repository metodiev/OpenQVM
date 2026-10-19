"""Virtual CD-ROM discovery from QMP query-block output."""

from __future__ import annotations

from openqvm.vm.qemu.medium import CDROM_DEVICE_ID, find_cdrom_device, medium_filename

MODERN = [
    {"device": "drive0", "removable": False, "inserted": {"image": {"filename": "C:/vm/disk.qcow2"}}},
    {
        "device": "cdrom0",
        "removable": True,
        "tray_open": False,
        "qdev": "/machine/peripheral-anon/device[0]",
        "inserted": {
            "image": {"filename": "C:/iso/installer.iso", "format": "raw", "virtual-size": 1024}
        },
    },
]

LEGACY = [
    {"device": "drive0", "removable": False},
    {
        "device": "ide2-cd0",
        "removable": True,
        "qdev": "/machine/unattached/device[16]",
        "inserted": {"image": {"filename": "D:/iso/old.iso"}},
    },
]


def test_prefers_named_cdrom_device():
    entry = find_cdrom_device(MODERN)
    assert entry is not None
    assert entry["device"] == CDROM_DEVICE_ID
    assert medium_filename(entry) == "C:/iso/installer.iso"


def test_falls_back_to_legacy_cd_device():
    entry = find_cdrom_device(LEGACY)
    assert entry is not None
    assert entry["device"] == "ide2-cd0"
    assert medium_filename(entry) == "D:/iso/old.iso"


def test_empty_tray_has_no_medium():
    entry = find_cdrom_device([{"device": "cdrom0", "removable": True}])
    assert entry is not None
    assert medium_filename(entry) is None


def test_unrelated_removable_device_is_ignored():
    assert find_cdrom_device([{"device": "floppy0", "removable": True}]) is None
    assert find_cdrom_device([]) is None


def test_unattached_drive_is_not_treated_as_cdrom():
    blocks = [{"device": "drive0", "removable": True, "inserted": {"image": {"filename": "d.qcow2"}}}]
    assert find_cdrom_device(blocks) is None
