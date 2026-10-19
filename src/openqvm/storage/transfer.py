"""Export and import of self-contained OpenQVM packages (``.oqv``).

A package is a ZIP archive containing a manifest, the VM configuration and the
disk images (byte-for-byte, so qcow2 internal snapshots survive).  Import treats
the archive as untrusted: paths, sizes, compression ratios, media types and
checksums are all validated before anything is registered.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from openqvm import PRODUCT_NAME, PRODUCT_VERSION
from openqvm.core.cancellation import CancellationToken
from openqvm.core.errors import (
    ConflictError,
    CorruptDataError,
    OperationFailedError,
    SecurityError,
    ValidationError,
)
from openqvm.core.ids import new_id
from openqvm.core.logging import get_logger
from openqvm.core.models import SnapshotRecord, VMSpec, generate_mac, utc_now_iso
from openqvm.core.paths import AppPaths
from openqvm.core.progress import ByteProgress, ProgressCallback, report_progress
from openqvm.core.validation import validate_vm_name
from openqvm.storage.disks import DiskManager
from openqvm.store import VmStore

log = get_logger("storage.transfer")

EXPORT_FORMAT = "openqvm-export"
EXPORT_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_ALLOWED_ENTRY_RE = re.compile(r"^(manifest\.json|disks/[A-Za-z0-9][A-Za-z0-9._-]{0,254}|firmware/[A-Za-z0-9][A-Za-z0-9._-]{0,254})$")
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 16 * 1024**4
_CHUNK = 4 * 1024 * 1024


@dataclass(frozen=True)
class HashResult:
    sha256: str
    size: int


@dataclass(frozen=True)
class ExportResult:
    path: Path
    size_bytes: int
    sha256: str
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ImportResult:
    spec: VMSpec
    vm_dir: Path
    warnings: list[str] = field(default_factory=list)


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")
    return cleaned[:48] or "vm"


def suggested_export_name(spec: VMSpec) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{_slug(spec.name)}-{stamp}.oqv"


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _sha256_file(
    path: Path,
    cancel: CancellationToken | None = None,
    progress: ByteProgress | None = None,
    message: str = "Verifying the package",
) -> HashResult:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            if cancel is not None:
                cancel.raise_if_cancelled("The export was cancelled while verifying it.")
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if progress is not None:
                progress.advance(len(chunk), message)
    return HashResult(digest.hexdigest(), size)


def _write_entry(
    zf: zipfile.ZipFile,
    arcname: str,
    source: Path,
    cancel: CancellationToken | None = None,
    progress: ByteProgress | None = None,
    message: str = "",
) -> HashResult:
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as src, zf.open(arcname, "w", force_zip64=True) as dst:
        while True:
            if cancel is not None:
                cancel.raise_if_cancelled("The export was cancelled.")
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            dst.write(chunk)
            if progress is not None:
                progress.advance(len(chunk), message or f"Writing {arcname}")
    return HashResult(digest.hexdigest(), size)


class VmTransferService:
    def __init__(self, paths: AppPaths, store: VmStore, disks: DiskManager) -> None:
        self.paths = paths
        self.store = store
        self.disks = disks

    def export_vm(
        self,
        spec: VMSpec,
        output_path: Path,
        *,
        overwrite: bool = False,
        cancel: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
    ) -> ExportResult:
        if cancel is not None:
            cancel.raise_if_cancelled("The export was cancelled before it started.")
        report_progress(progress, None, "Inspecting the virtual machine")
        output_path = Path(output_path).expanduser()
        if output_path.suffix.lower() != ".oqv":
            output_path = output_path.with_suffix(".oqv")
        if not output_path.is_absolute():
            raise ValidationError("The export destination must be an absolute path.")
        if output_path.exists() and not overwrite:
            raise ConflictError(
                f"The export file already exists: {output_path.name}",
                hint="Choose another file name or allow overwrite.",
            )
        vm_dir = self.paths.require_vm_dir(spec.id)
        try:
            if vm_dir.resolve() in output_path.resolve().parents:
                raise ValidationError(
                    "The export file must not be written inside the virtual machine directory.",
                    hint="Choose a destination outside the OpenQVM VM folder.",
                )
        except OSError:
            pass

        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.parent / f".{output_path.name}.{new_id()[:8]}.tmp"
        warnings: list[str] = []
        disk_entries: list[dict[str, object]] = []
        firmware_entries: list[dict[str, object]] = []
        firmware_vars = self.paths.vm_firmware_dir(spec.id) / "OVMF_VARS.fd"
        payload_bytes = sum(_file_size(self.store.disk_path(spec, disk)) for disk in spec.disks)
        payload_bytes += _file_size(firmware_vars)
        # The package is written once and then hashed, so the work is two passes.
        tracker = ByteProgress(progress, payload_bytes * 2) if progress is not None else None
        try:
            with zipfile.ZipFile(temp_path, "w", allowZip64=True, compression=zipfile.ZIP_STORED) as archive:
                for disk in spec.disks:
                    disk_path = self.store.disk_path(spec, disk)
                    if not disk_path.is_file():
                        raise OperationFailedError(
                            f"The disk file is missing: {disk.filename}",
                            hint="Run a disk consistency check or restore the VM from a backup.",
                        )
                    result = _write_entry(
                        archive,
                        f"disks/{disk.filename}",
                        disk_path,
                        cancel,
                        tracker,
                        f"Writing {disk.filename}",
                    )
                    disk_entries.append(
                        {
                            "filename": disk.filename,
                            "format": disk.format.value,
                            "size_bytes": disk.size_bytes,
                            "sha256": result.sha256,
                            "stored_bytes": result.size,
                        }
                    )

                firmware_vars = self.paths.vm_firmware_dir(spec.id) / "OVMF_VARS.fd"
                if firmware_vars.is_file():
                    result = _write_entry(
                        archive,
                        "firmware/OVMF_VARS.fd",
                        firmware_vars,
                        cancel,
                        tracker,
                        "Writing the firmware variables",
                    )
                    firmware_entries.append(
                        {"filename": "OVMF_VARS.fd", "sha256": result.sha256, "stored_bytes": result.size}
                    )

                manifest = {
                    "format": EXPORT_FORMAT,
                    "format_version": EXPORT_VERSION,
                    "product": PRODUCT_NAME,
                    "product_version": PRODUCT_VERSION,
                    "created_at": utc_now_iso(),
                    "vm": spec.to_dict(),
                    "disks": disk_entries,
                    "firmware": firmware_entries,
                    "snapshots": [
                        record.to_dict() for record in self.store.load_snapshots(spec.id)
                    ],
                }
                archive.writestr(
                    _MANIFEST_NAME,
                    json.dumps(manifest, indent=2, ensure_ascii=False),
                    compress_type=zipfile.ZIP_STORED,
                )
            with temp_path.open("rb+") as handle:
                os.fsync(handle.fileno())
            hash_result = _sha256_file(temp_path, cancel, tracker)
            if output_path.exists():
                output_path.unlink()
            os.replace(temp_path, output_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        if tracker is not None:
            tracker.finish("Package verified")
        if spec.cdrom_iso:
            warnings.append("The attached installation ISO is not included in the package.")
        log.info("Exported VM %s to %s (%s bytes)", spec.name, output_path, hash_result.size)
        return ExportResult(path=output_path, size_bytes=hash_result.size, sha256=hash_result.sha256, warnings=warnings)

    def import_vm(
        self,
        package: Path,
        *,
        new_name: str | None = None,
        cancel: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
    ) -> ImportResult:
        if cancel is not None:
            cancel.raise_if_cancelled("The import was cancelled before it started.")
        report_progress(progress, None, "Validating the package")
        package = Path(package).expanduser()
        if not package.is_absolute():
            raise ValidationError("The import package must be an absolute path.")
        if not package.is_file():
            raise ValidationError(f"The import package was not found: {package}")
        if not zipfile.is_zipfile(package):
            raise ValidationError("The selected file is not a valid OpenQVM package (ZIP archive).")
        warnings: list[str] = []
        try:
            with zipfile.ZipFile(package) as archive:
                manifest = self._read_manifest(archive)
                spec, records, disk_manifest, firmware_manifest = self._parse_manifest(manifest, archive)
                total_bytes = sum(int(entry.get("stored_bytes") or 0) for entry in disk_manifest)
                total_bytes += sum(int(entry.get("stored_bytes") or 0) for entry in firmware_manifest)
                tracker = ByteProgress(progress, total_bytes) if progress is not None else None
                self._precheck_archive(archive, spec, disk_manifest, firmware_manifest, cancel)
                if cancel is not None:
                    cancel.raise_if_cancelled("The import was cancelled.")

                existing = self.store.list_specs()
                target_name = self._unique_name(new_name or spec.name, existing)
                if target_name != spec.name:
                    warnings.append(f"The VM was imported as '{target_name}' to avoid a name collision.")

                spec.id = new_id()
                spec.name = target_name
                spec.created_at = utc_now_iso()
                spec.updated_at = spec.created_at
                spec.revision = 1
                used_macs = {nic.mac for other in existing for nic in other.nics}
                new_nics = []
                for nic in spec.nics:
                    mac = generate_mac()
                    while mac in used_macs:
                        mac = generate_mac()
                    used_macs.add(mac)
                    new_nics.append(replace(nic, mac=mac))
                spec.nics = new_nics
                if spec.cdrom_iso and not Path(spec.cdrom_iso).exists():
                    warnings.append("The attached installation ISO does not exist on this host; it was cleared.")
                    spec.cdrom_iso = None
                if spec.shared_folders:
                    importable = [folder for folder in spec.shared_folders if Path(folder.path).is_dir()]
                    if len(importable) != len(spec.shared_folders):
                        warnings.append("Shared folders that do not exist on this host were removed.")
                    spec.shared_folders = importable

                staging = self.paths.vms_dir / f".import-{spec.id}"
                final_dir = self.paths.vm_dir(spec.id)
                if staging.exists():
                    shutil.rmtree(staging)
                staging.mkdir(parents=True)
                try:
                    (staging / "disks").mkdir()
                    for entry in disk_manifest:
                        self._extract_entry(
                            archive,
                            entry,
                            staging / "disks" / entry["filename"],
                            f"disks/{entry['filename']}",
                            cancel,
                            tracker,
                            f"Extracting {entry['filename']}",
                        )
                        disk_path = staging / "disks" / entry["filename"]
                        info = self.disks.info(disk_path)
                        expected_format = next(
                            disk.format.value for disk in spec.disks if disk.filename == entry["filename"]
                        )
                        if info.format != expected_format:
                            raise CorruptDataError(
                                f"The imported disk {entry['filename']} has an unexpected format.",
                                details={"expected": expected_format, "actual": info.format},
                            )
                        if info.virtual_size != next(
                            disk.size_bytes for disk in spec.disks if disk.filename == entry["filename"]
                        ):
                            warnings.append(
                                f"Disk {entry['filename']} has virtual size {info.virtual_size} bytes, "
                                "which differs from the configuration."
                            )
                    if firmware_manifest:
                        (staging / "firmware").mkdir()
                        for entry in firmware_manifest:
                            self._extract_entry(
                                archive,
                                entry,
                                staging / "firmware" / entry["filename"],
                                f"firmware/{entry['filename']}",
                                cancel,
                                tracker,
                                "Extracting the firmware variables",
                            )
                    for directory in ("logs",):
                        (staging / directory).mkdir(exist_ok=True)
                    if cancel is not None:
                        cancel.raise_if_cancelled("The import was cancelled before it was registered.")
                    os.replace(staging, final_dir)
                except Exception:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise

                if tracker is not None:
                    tracker.finish("Registering the virtual machine")

                try:
                    self.store.save_snapshots(spec.id, records)
                    self.store.save_spec(spec)
                except Exception:
                    shutil.rmtree(final_dir, ignore_errors=True)
                    raise
        except zipfile.BadZipFile as exc:
            raise CorruptDataError("The package is corrupted and cannot be read.", cause=exc) from exc

        log.info("Imported VM '%s' from %s", spec.name, package)
        return ImportResult(spec=spec, vm_dir=final_dir, warnings=warnings)

    # -- import helpers -------------------------------------------------------

    def _read_manifest(self, archive: zipfile.ZipFile) -> dict:
        try:
            info = archive.getinfo(_MANIFEST_NAME)
        except KeyError as exc:
            raise ValidationError("The package does not contain a manifest.") from exc
        if info.file_size > _MAX_MANIFEST_BYTES:
            raise SecurityError("The package manifest is unexpectedly large.")
        try:
            raw = archive.read(_MANIFEST_NAME)
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorruptDataError("The package manifest is not valid JSON.", cause=exc) from exc
        if not isinstance(data, dict):
            raise CorruptDataError("The package manifest is not an object.")
        if data.get("format") != EXPORT_FORMAT:
            raise ValidationError("The selected file is not an OpenQVM export.")
        version = data.get("format_version")
        if version != EXPORT_VERSION:
            raise ValidationError(
                f"Unsupported package format version: {version!r}.",
                hint="Upgrade OpenQVM to import this package.",
            )
        return data

    def _parse_manifest(
        self, manifest: dict, archive: zipfile.ZipFile
    ) -> tuple[VMSpec, list[SnapshotRecord], list[dict], list[dict]]:
        try:
            spec = VMSpec.from_dict(manifest.get("vm"))
        except ValidationError as exc:
            raise CorruptDataError("The package contains an invalid VM configuration.", cause=exc) from exc
        records_raw = manifest.get("snapshots", [])
        if not isinstance(records_raw, list):
            raise CorruptDataError("The package snapshot metadata is invalid.")
        try:
            records = [SnapshotRecord.from_dict(item) for item in records_raw]
        except ValidationError as exc:
            raise CorruptDataError("The package snapshot metadata is invalid.", cause=exc) from exc

        disk_manifest = manifest.get("disks", [])
        if not isinstance(disk_manifest, list):
            raise CorruptDataError("The package disk manifest is invalid.")
        expected = {disk.filename: disk for disk in spec.disks}
        seen: set[str] = set()
        normalized: list[dict] = []
        for entry in disk_manifest:
            if not isinstance(entry, dict):
                raise CorruptDataError("The package disk manifest is invalid.")
            filename = entry.get("filename")
            if not isinstance(filename, str) or filename not in expected:
                raise SecurityError("The package references a disk that is not part of the VM configuration.")
            if filename in seen:
                raise CorruptDataError("The package contains duplicate disk entries.")
            seen.add(filename)
            sha = entry.get("sha256")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise CorruptDataError("The package contains an invalid disk checksum.")
            normalized.append(entry)
        missing = set(expected) - seen
        if missing:
            raise CorruptDataError(
                "The package is incomplete.",
                details={"missing_disks": sorted(missing)},
            )

        firmware_manifest = manifest.get("firmware", [])
        if not isinstance(firmware_manifest, list):
            raise CorruptDataError("The package firmware manifest is invalid.")
        firmware_normalized: list[dict] = []
        for entry in firmware_manifest:
            if not isinstance(entry, dict) or entry.get("filename") != "OVMF_VARS.fd":
                raise SecurityError("The package references an unsupported firmware file.")
            sha = entry.get("sha256")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise CorruptDataError("The package contains an invalid firmware checksum.")
            firmware_normalized.append(entry)
        return spec, records, normalized, firmware_normalized

    def _precheck_archive(
        self,
        archive: zipfile.ZipFile,
        spec: VMSpec,
        disks: list[dict],
        firmware: list[dict],
        cancel: CancellationToken | None = None,
    ) -> None:
        expected_names = {_MANIFEST_NAME}
        expected_names.update(f"disks/{entry['filename']}" for entry in disks)
        expected_names.update(f"firmware/{entry['filename']}" for entry in firmware)

        uncompressed_total = 0
        seen_names: set[str] = set()
        for info in archive.infolist():
            if cancel is not None:
                cancel.raise_if_cancelled("The import was cancelled.")
            name = info.filename
            if name.endswith("/"):
                continue
            if name in seen_names:
                raise SecurityError("The package contains duplicate entries.", details={"entry": name})
            seen_names.add(name)
            if not _ALLOWED_ENTRY_RE.match(name) or name not in expected_names:
                raise SecurityError(
                    "The package contains an unexpected file entry.",
                    details={"entry": name},
                )
            if info.flag_bits & 0x1:
                raise SecurityError("Encrypted package entries are not supported.")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise SecurityError("The package contains a symbolic link, which is not allowed.")
            if info.file_size < 0:
                raise SecurityError("The package contains an invalid entry size.")
            uncompressed_total += info.file_size
            if uncompressed_total > _MAX_UNCOMPRESSED_BYTES:
                raise SecurityError("The package exceeds the maximum supported size.")
        compressed_total = max(1, sum(max(0, info.compress_size) for info in archive.infolist()))
        if uncompressed_total > max(compressed_total * 16, 512 * 1024**2):
            raise SecurityError("The package has an unsafe compression ratio.")
        for entry in disks:
            name = f"disks/{entry['filename']}"
            if name not in archive.namelist():
                raise CorruptDataError(f"The package is missing {name}.")
        for entry in firmware:
            name = f"firmware/{entry['filename']}"
            if name not in archive.namelist():
                raise CorruptDataError(f"The package is missing {name}.")

    def _extract_entry(
        self,
        archive: zipfile.ZipFile,
        entry: dict,
        destination: Path,
        archive_name: str,
        cancel: CancellationToken | None = None,
        progress: ByteProgress | None = None,
        message: str = "",
    ) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        written = 0
        with archive.open(archive_name) as src, destination.open("wb") as dst:
            while True:
                if cancel is not None:
                    cancel.raise_if_cancelled("The import was cancelled.")
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_UNCOMPRESSED_BYTES:
                    raise SecurityError("The package entry is too large.")
                digest.update(chunk)
                dst.write(chunk)
                if progress is not None:
                    progress.advance(len(chunk), message or f"Extracting {archive_name}")
            dst.flush()
            os.fsync(dst.fileno())
        if not hmac.compare_digest(digest.hexdigest(), entry["sha256"]):
            destination.unlink(missing_ok=True)
            raise CorruptDataError(
                f"The checksum of {entry['filename']} does not match the manifest.",
                hint="The package is corrupted; obtain a new export.",
            )
        expected_size = entry.get("stored_bytes")
        if isinstance(expected_size, int) and expected_size != written:
            destination.unlink(missing_ok=True)
            raise CorruptDataError(f"The size of {entry['filename']} does not match the manifest.")

    def _unique_name(self, name: str, existing: list[VMSpec]) -> str:
        base = name.strip() or "Imported VM"
        if any(issue.is_error for issue in validate_vm_name(base)):
            base = _slug(base)
        base = base[:45].rstrip(" ._-") or "Imported VM"
        if any(issue.is_error for issue in validate_vm_name(base)):
            base = "Imported VM"
        used = {spec.name.strip().lower() for spec in existing}
        if base.lower() not in used:
            return base
        candidate = f"{base} (imported)"
        counter = 2
        while candidate.lower() in used:
            candidate = f"{base} (imported {counter})"
            counter += 1
        return candidate[:64].rstrip(" ._-)") or "Imported VM"
