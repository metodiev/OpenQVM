"""Helpers for locating the virtual CD-ROM device in QMP output."""

from __future__ import annotations

from typing import Any

CDROM_DEVICE_ID = "cdrom0"


def find_cdrom_device(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the ``query-block`` entry for the virtual CD-ROM.

    Current VMs name the drive ``cdrom0``.  VMs started by older builds used
    QEMU's implicit ``ideN-cd0`` name, so removable devices with ``cd`` in the
    name are accepted as a fallback.  Unrelated removable devices are never
    returned.
    """

    for entry in blocks:
        if entry.get("device") == CDROM_DEVICE_ID:
            return entry
    for entry in blocks:
        name = str(entry.get("device", "")).lower()
        if entry.get("removable") and "cd" in name:
            return entry
    return None


def medium_filename(entry: dict[str, Any]) -> str | None:
    """Return the file currently inserted in *entry*, if any."""

    inserted = entry.get("inserted")
    if not isinstance(inserted, dict):
        return None
    image = inserted.get("image")
    if isinstance(image, dict):
        filename = image.get("filename")
        return str(filename) if filename else None
    file_name = inserted.get("file")
    return str(file_name) if file_name else None
