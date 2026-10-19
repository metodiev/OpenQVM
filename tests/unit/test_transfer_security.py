"""Import hardening: traversal, unexpected entries and checksums."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from openqvm.core.factory import create_vm_spec
from openqvm.core.models import GuestOS
from openqvm.core.paths import AppPaths
from openqvm.core.validation import HostResources
from openqvm.storage.disks import DiskManager
from openqvm.storage.transfer import EXPORT_FORMAT, EXPORT_VERSION, VmTransferService
from openqvm.store import VmStore

HOST = HostResources(cpu_count=4, memory_total_mib=8192)


def _service(tmp_path: Path) -> tuple[VmTransferService, object]:
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    store = VmStore(paths)
    return VmTransferService(paths, store, DiskManager(None)), store


def _manifest(spec, disk_payload: bytes, sha: str | None = None) -> dict:
    return {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_VERSION,
        "product": "OpenQVM",
        "product_version": "0.1.0",
        "created_at": "2026-01-01T00:00:00+00:00",
        "vm": spec.to_dict(),
        "disks": [
            {
                "filename": spec.disks[0].filename,
                "format": spec.disks[0].format.value,
                "size_bytes": spec.disks[0].size_bytes,
                "sha256": sha or hashlib.sha256(disk_payload).hexdigest(),
                "stored_bytes": len(disk_payload),
            }
        ],
        "firmware": [],
        "snapshots": [],
    }


def test_archive_path_traversal_is_rejected(tmp_path):
    spec = create_vm_spec(name="Evil", guest_os=GuestOS.LINUX, host=HOST)
    service, _ = _service(tmp_path)
    package = tmp_path / "evil.oqv"
    payload = b"disk"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.json", json.dumps(_manifest(spec, payload)))
        archive.writestr("disks/" + spec.disks[0].filename, payload)
        archive.writestr("../escaped.txt", b"owned")
    with pytest.raises(Exception, match="unexpected file entry"):
        service.import_vm(package)


def test_checksum_mismatch_is_rejected(tmp_path):
    spec = create_vm_spec(name="Bad hash", guest_os=GuestOS.LINUX, host=HOST)
    service, _ = _service(tmp_path)
    package = tmp_path / "badhash.oqv"
    payload = b"disk"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.json", json.dumps(_manifest(spec, payload, sha="0" * 64)))
        archive.writestr("disks/" + spec.disks[0].filename, payload)
    with pytest.raises(Exception, match="checksum"):
        service.import_vm(package)


def test_missing_manifest_is_rejected(tmp_path):
    service, _ = _service(tmp_path)
    package = tmp_path / "empty.oqv"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("disks/foo.qcow2", b"x")
    with pytest.raises(Exception, match="manifest"):
        service.import_vm(package)


def test_plain_zip_is_rejected(tmp_path):
    service, _ = _service(tmp_path)
    package = tmp_path / "notopenqvm.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": "something-else"}))
    with pytest.raises(Exception, match="not an OpenQVM export"):
        service.import_vm(package)
