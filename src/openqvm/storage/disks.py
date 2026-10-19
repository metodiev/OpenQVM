"""Virtual disk operations backed by ``qemu-img``."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from openqvm.core.errors import (
    BackendUnavailableError,
    ConflictError,
    CorruptDataError,
    OperationFailedError,
    ValidationError,
)
from openqvm.core.models import DiskFormat, DiskSpec
from openqvm.core.validation import validate_disk_resize
from openqvm.storage.tools import ToolResult, run_tool


@dataclass(frozen=True)
class DiskInfo:
    path: Path
    format: str
    virtual_size: int
    actual_size: int
    backing_file: str | None
    snapshots: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "format": self.format,
            "virtual_size": self.virtual_size,
            "actual_size": self.actual_size,
            "backing_file": self.backing_file,
            "snapshots": list(self.snapshots),
        }


@dataclass(frozen=True)
class DiskCheckResult:
    path: Path
    ok: bool
    output: str

    def to_dict(self) -> dict[str, object]:
        return {"path": str(self.path), "ok": self.ok, "output": self.output}


class DiskManager:
    """Creates and inspects virtual disks using the configured ``qemu-img``."""

    def __init__(self, qemu_img: Path | None) -> None:
        self.qemu_img = Path(qemu_img) if qemu_img else None

    def _require_tool(self) -> Path:
        if self.qemu_img is None:
            raise BackendUnavailableError(
                "qemu-img is not available.",
                hint="Install QEMU or configure its directory in Settings.",
            )
        return self.qemu_img

    def require_qemu_img(self) -> Path:
        """Public accessor used by the snapshot service."""

        return self._require_tool()

    def _run(self, args: list[str], *, timeout: float = 300.0, operation: str) -> ToolResult:
        result = run_tool([str(self._require_tool()), *args], timeout=timeout, tool_name="qemu-img")
        if not result.ok:
            raise OperationFailedError(
                f"The disk operation '{operation}' failed.",
                hint="Check that the disk file is not in use and that there is enough free space.",
                details={"exit_code": result.returncode, "output": result.output_tail},
            )
        return result

    def create(self, path: Path, disk: DiskSpec, *, overwrite: bool = False) -> DiskInfo:
        path = Path(path)
        if path.exists() and not overwrite:
            raise ConflictError(
                f"The disk file already exists: {path.name}",
                hint="Choose a different name or delete the existing disk explicitly.",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        args = ["create", "-f", disk.format.value]
        if disk.preallocated:
            args.extend(["-o", "preallocation=full"])
        args.extend([str(path), str(disk.size_bytes)])
        self._run(args, operation="create")
        info = self.info(path)
        if info.format != disk.format.value:
            raise OperationFailedError(
                "The disk was created with an unexpected format.",
                details={"expected": disk.format.value, "actual": info.format},
            )
        if info.virtual_size != disk.size_bytes:
            raise OperationFailedError(
                "The disk was created with an unexpected size.",
                details={"expected": disk.size_bytes, "actual": info.virtual_size},
            )
        return info

    def info(self, path: Path) -> DiskInfo:
        result = self._run(["info", "--output=json", str(path)], operation="info")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CorruptDataError(
                f"qemu-img returned unreadable metadata for {path.name}.",
                details={"output": result.output_tail},
                cause=exc,
            ) from exc
        snapshots = tuple(
            str(snapshot.get("name") or snapshot.get("id") or "") for snapshot in data.get("snapshots", []) or []
        )
        return DiskInfo(
            path=path,
            format=str(data.get("format", "")),
            virtual_size=int(data.get("virtual-size", 0)),
            actual_size=int(data.get("actual-size", 0)),
            backing_file=data.get("backing-filename"),
            snapshots=tuple(name for name in snapshots if name),
        )

    def check(self, path: Path, *, timeout: float = 1800.0) -> DiskCheckResult:
        result = run_tool([str(self._require_tool()), "check", str(path)], timeout=timeout, tool_name="qemu-img")
        output = result.output_tail
        # qemu-img check uses exit code 3 when corruption was found but repaired.
        if result.returncode not in (0, 3):
            raise OperationFailedError(
                "The disk consistency check could not be completed.",
                details={"exit_code": result.returncode, "output": output},
            )
        return DiskCheckResult(path=path, ok=result.returncode == 0, output=output)

    def resize(self, path: Path, disk: DiskSpec, new_size_bytes: int) -> DiskInfo:
        issues = validate_disk_resize(disk, new_size_bytes)
        if issues:
            raise ValidationError("The new disk size is not valid.", issues=issues)
        info = self.info(path)
        if info.backing_file:
            raise ValidationError(
                "Disks with a backing file cannot be resized.",
                hint="Remove the snapshot chain before resizing.",
            )
        self._run(["resize", str(path), str(new_size_bytes)], operation="resize")
        resized = self.info(path)
        if resized.virtual_size != new_size_bytes:
            raise OperationFailedError(
                "The disk was not resized to the requested size.",
                details={"expected": new_size_bytes, "actual": resized.virtual_size},
            )
        return resized

    def convert(
        self,
        source: Path,
        destination: Path,
        *,
        target_format: DiskFormat = DiskFormat.QCOW2,
        preallocated: bool = False,
    ) -> DiskInfo:
        if Path(source).resolve() == Path(destination).resolve():
            raise ValidationError("The source and destination disk must be different files.")
        if Path(destination).exists():
            raise ConflictError(f"The destination disk already exists: {Path(destination).name}")
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        args = ["convert", "-O", target_format.value]
        if preallocated and target_format is DiskFormat.QCOW2:
            args.extend(["-o", "preallocation=full"])
        args.extend([str(source), str(destination)])
        self._run(args, operation="convert", timeout=24 * 60 * 60)
        info = self.info(destination)
        if info.format != target_format.value:
            raise OperationFailedError(
                "The converted disk has an unexpected format.",
                details={"expected": target_format.value, "actual": info.format},
            )
        return info

    def copy(self, source: Path, destination: Path) -> None:
        """Byte-for-byte copy used for exports (preserves internal snapshots)."""

        if Path(destination).exists():
            raise ConflictError(f"The destination file already exists: {Path(destination).name}")
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, destination)
        except OSError as exc:
            raise OperationFailedError(
                "The disk could not be copied.",
                hint="Check free disk space and file permissions.",
                details={"source": str(source)},
                cause=exc,
            ) from exc

    def delete(self, path: Path) -> None:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise OperationFailedError(
                "The disk file could not be deleted.",
                hint="Close any program using the disk and try again.",
                cause=exc,
            ) from exc

    def ensure_same_filesystem(self, source: Path, destination: Path) -> None:
        try:
            if os.stat(source).st_dev != os.stat(destination.parent).st_dev:
                raise ValidationError(
                    "The destination is on a different file system.",
                    hint="Choose a destination on the same volume to allow atomic moves.",
                )
        except OSError:
            return
