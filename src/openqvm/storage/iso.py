"""Installation image validation.

ISO images are untrusted input.  OpenQVM only passes them to QEMU as a read-only
CD-ROM; it never mounts or executes their contents on the host.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openqvm.core.errors import ValidationError

_ISO_PRIMARY_DESCRIPTOR_OFFSET = 0x8000
_ISO_MAGIC_OFFSET = 0x8001
_VOLUME_ID_OFFSET = 0x8028


@dataclass(frozen=True)
class IsoInfo:
    path: Path
    size_bytes: int
    is_iso9660: bool
    volume_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "is_iso9660": self.is_iso9660,
            "volume_id": self.volume_id,
        }


def inspect_iso(path: str | Path) -> IsoInfo:
    iso_path = Path(path)
    if not iso_path.is_absolute():
        raise ValidationError("The installation image path must be absolute.")
    if iso_path.suffix.lower() not in {".iso", ".img"}:
        raise ValidationError("The installation image must have an .iso or .img extension.")
    try:
        stat = iso_path.stat()
    except FileNotFoundError as exc:
        raise ValidationError(
            f"The installation image was not found: {iso_path}",
            hint="Choose an existing ISO file.",
        ) from exc
    except OSError as exc:
        raise ValidationError(f"The installation image could not be read: {iso_path}") from exc
    if not iso_path.is_file():
        raise ValidationError("The installation image is not a regular file.")
    if stat.st_size < 32 * 1024:
        raise ValidationError("The file is too small to be an installation image.")

    is_iso9660 = False
    volume_id = ""
    try:
        with iso_path.open("rb") as handle:
            handle.seek(_ISO_PRIMARY_DESCRIPTOR_OFFSET)
            descriptor = handle.read(2048)
        if len(descriptor) >= 2048 and descriptor[_ISO_MAGIC_OFFSET - _ISO_PRIMARY_DESCRIPTOR_OFFSET :][:5] == b"CD001":
            is_iso9660 = True
            raw_id = descriptor[_VOLUME_ID_OFFSET - _ISO_PRIMARY_DESCRIPTOR_OFFSET :][:32]
            volume_id = raw_id.decode("ascii", errors="replace").strip()
    except OSError as exc:
        raise ValidationError("The installation image could not be read.") from exc

    return IsoInfo(path=iso_path, size_bytes=stat.st_size, is_iso9660=is_iso9660, volume_id=volume_id)


def validate_iso(path: str | Path) -> IsoInfo:
    """Validate an ISO reference and return its metadata.

    Non-ISO9660 images are accepted with ``is_iso9660=False`` because hybrid or
    UDF-based installers exist; the UI displays this as an informational note.
    """

    info = inspect_iso(path)
    return info
