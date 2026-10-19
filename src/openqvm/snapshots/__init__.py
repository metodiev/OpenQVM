"""QEMU snapshot support.

Two mechanisms are used and their guarantees are reported honestly:

* Offline snapshots (VM stopped) use ``qemu-img snapshot`` and capture disk state
  only.  They are crash-consistent at best and contain no memory.
* Online snapshots use QEMU's monitor (``savevm``) and include device and RAM
  state.  They require every attached disk to be qcow2.

A snapshot is only reported as including memory when it was created with the VM
running or paused, and the metadata reflects that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from openqvm.core.cancellation import CancellationToken
from openqvm.core.errors import (
    InvalidStateError,
    OperationFailedError,
    UnsupportedFeatureError,
    ValidationError,
)
from openqvm.core.logging import get_logger
from openqvm.core.models import DiskFormat, SnapshotRecord, VMSpec, utc_now_iso
from openqvm.storage.disks import DiskManager
from openqvm.storage.tools import run_tool
from openqvm.store import VmStore

log = get_logger("snapshots")


@dataclass(frozen=True)
class SnapshotTagInfo:
    name: str
    id: str
    vm_size: str
    date: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "id": self.id, "vm_size": self.vm_size, "date": self.date}


def _snapshot_disks(spec: VMSpec) -> list:
    unsupported = [disk for disk in spec.disks if disk.format is not DiskFormat.QCOW2]
    if unsupported:
        names = ", ".join(disk.filename for disk in unsupported)
        raise UnsupportedFeatureError(
            "Snapshots require qcow2 disks.",
            hint=f"Convert the following disks to qcow2 first: {names}",
            details={"containers": names},
        )
    return list(spec.disks)


class SnapshotService:
    def __init__(self, store: VmStore, disks: DiskManager) -> None:
        self.store = store
        self.disks = disks

    # -- offline --------------------------------------------------------------

    def list_offline(self, spec: VMSpec) -> list[SnapshotTagInfo]:
        common: dict[str, SnapshotTagInfo] | None = None
        for disk in _snapshot_disks(spec):
            disk_tags = {
                info.name: info for info in self._list_disk_snapshots(self.store.disk_path(spec, disk))
            }
            if common is None:
                common = disk_tags
            else:
                common = {name: info for name, info in common.items() if name in disk_tags}
        return sorted((common or {}).values(), key=lambda item: item.name.lower())

    def create_offline(
        self, spec: VMSpec, name: str, description: str = "", cancel: CancellationToken | None = None
    ) -> SnapshotRecord:
        if cancel is not None:
            cancel.raise_if_cancelled("Snapshot creation was cancelled.")
        disks = _snapshot_disks(spec)
        for disk in disks:
            existing_tags = {info.name for info in self._list_disk_snapshots(self.store.disk_path(spec, disk))}
            if name in existing_tags:
                raise ValidationError(f"A snapshot named '{name}' already exists.")
        created: list[Path] = []
        try:
            for disk in disks:
                if cancel is not None:
                    cancel.raise_if_cancelled("Snapshot creation was cancelled.")
                path = self.store.disk_path(spec, disk)
                self._run_img(["snapshot", "-c", name, str(path)], operation="create snapshot", cancel=cancel)
                created.append(path)
        except Exception:
            for path in created:
                try:
                    self._run_img(["snapshot", "-d", name, str(path)], operation="rollback snapshot")
                except Exception:
                    log.exception("Could not roll back snapshot %s on %s", name, path)
            raise

        for disk in disks:
            tags = {info.name for info in self._list_disk_snapshots(self.store.disk_path(spec, disk))}
            if name not in tags:
                raise OperationFailedError(
                    "The snapshot was not created on every disk.",
                    hint="Run a disk consistency check.",
                    details={"disk": disk.filename, "snapshot": name},
                )
        return SnapshotRecord(
            name=name,
            description=description,
            created_at=utc_now_iso(),
            includes_memory=False,
            vm_state="stopped",
            disks=[disk.filename for disk in disks],
        )

    def restore_offline(
        self, spec: VMSpec, name: str, cancel: CancellationToken | None = None
    ) -> None:
        for disk in _snapshot_disks(spec):
            if cancel is not None:
                cancel.raise_if_cancelled("Snapshot restore was cancelled.")
            self._run_img(
                ["snapshot", "-a", name, str(self.store.disk_path(spec, disk))],
                operation="restore",
                cancel=cancel,
            )
        log.info("Restored disk-only snapshot %s for %s", name, spec.name)

    def delete_offline(
        self, spec: VMSpec, name: str, cancel: CancellationToken | None = None
    ) -> None:
        for disk in _snapshot_disks(spec):
            if cancel is not None:
                cancel.raise_if_cancelled("Snapshot deletion was cancelled.")
            self._run_img(
                ["snapshot", "-d", name, str(self.store.disk_path(spec, disk))],
                operation="delete",
                cancel=cancel,
            )

    def _list_disk_snapshots(self, path: Path) -> list[SnapshotTagInfo]:
        result = run_tool(
            [str(self.disks.require_qemu_img()), "snapshot", "-l", str(path)],
            timeout=120,
            tool_name="qemu-img",
        )
        if not result.ok:
            raise OperationFailedError(
                "The snapshot list could not be read.",
                details={"disk": path.name, "output": result.output_tail},
            )
        return parse_snapshot_list(result.stdout)

    def _run_img(
        self, args: list[str], *, operation: str, cancel: CancellationToken | None = None
    ) -> None:
        result = run_tool(
            [str(self.disks.require_qemu_img()), *args],
            timeout=3600,
            tool_name="qemu-img",
            cancel=cancel,
        )
        if not result.ok:
            raise OperationFailedError(
                f"Could not {operation}.",
                hint="Verify that the VM is stopped and the disk is not in use.",
                details={"exit_code": result.returncode, "output": result.output_tail},
            )

    # -- online (QMP human monitor) ------------------------------------------

    def create_online(
        self, qmp, spec: VMSpec, name: str, description: str = "", vm_state: str = "running"
    ) -> SnapshotRecord:
        """Create an online snapshot, preferring a memory-inclusive snapshot.

        Some accelerators (notably WHPX) cannot save guest memory.  In that case
        QEMU refuses ``savevm``; OpenQVM then falls back to per-disk internal
        block snapshots and records that the snapshot is disk-only.  The
        limitation is surfaced to the user rather than hidden.
        """

        disks = _snapshot_disks(spec)
        memory_error: str | None = None
        response = qmp.human_monitor(f"savevm {name}")
        if not response.strip():
            tags = {info.name for info in self.list_online(qmp, spec)}
            if name in tags:
                return SnapshotRecord(
                    name=name,
                    description=description,
                    created_at=utc_now_iso(),
                    includes_memory=vm_state in {"running", "paused"},
                    vm_state=vm_state,
                    disks=[disk.filename for disk in disks],
                )
            memory_error = "QEMU reported success but the snapshot was not found."
        else:
            memory_error = response.strip()

        log.info("Memory snapshot failed for %s; falling back to disk-only: %s", spec.name, memory_error)
        created_devices: list[str] = []
        try:
            for index, _disk in enumerate(disks):
                device = f"drive{index}"
                qmp.execute("blockdev-snapshot-internal-sync", {"device": device, "name": name})
                created_devices.append(device)
        except Exception as exc:
            for device in created_devices:
                try:
                    qmp.execute(
                        "blockdev-snapshot-delete-internal-sync", {"device": device, "name": name}
                    )
                except Exception:
                    log.exception("Could not roll back disk snapshot %s on %s", name, device)
            raise OperationFailedError(
                "QEMU could not create the snapshot.",
                hint="Check the QEMU log. Some accelerators cannot snapshot guest memory; "
                "disk-only snapshots require every disk to be qcow2.",
                details={"memory_snapshot_error": memory_error, "disk_snapshot_error": str(exc)},
                cause=exc,
            ) from exc

        tags = {info.name for info in self.list_online(qmp, spec)}
        if name not in tags:
            raise OperationFailedError(
                "QEMU reported success but the snapshot was not found.",
                hint="Check the QEMU log and disk consistency.",
            )
        return SnapshotRecord(
            name=name,
            description=description,
            created_at=utc_now_iso(),
            includes_memory=False,
            vm_state=vm_state,
            disks=[disk.filename for disk in disks],
        )

    def restore_online(self, qmp, name: str, *, includes_memory: bool) -> None:
        if not includes_memory:
            raise InvalidStateError(
                "This snapshot contains disk state only and cannot be restored while QEMU is running.",
                hint="Shut down the virtual machine, then restore the snapshot offline.",
            )
        response = qmp.human_monitor(f"loadvm {name}")
        if response.strip():
            raise OperationFailedError(
                "QEMU could not load the snapshot.",
                details={"output": response.strip()[-2000:]},
            )

    def delete_online(self, qmp, name: str) -> None:
        response = qmp.human_monitor(f"delvm {name}")
        if response.strip():
            raise OperationFailedError(
                "QEMU could not delete the snapshot.",
                details={"output": response.strip()[-2000:]},
            )

    def list_online(self, qmp, spec: VMSpec) -> list[SnapshotTagInfo]:
        _snapshot_disks(spec)
        output = qmp.human_monitor("info snapshots")
        return parse_snapshot_list(output)


_SNAPSHOT_LINE_RE = re.compile(r"^\s*(--|\d+)\s+(\S+)\s+(.*)$")
_SNAPSHOT_DETAILS_RE = re.compile(
    r"^(?P<size>\d+(?:\.\d+)?\s*[KMGTP]?i?B)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r"(?:\s+(?P<clock>\d+:\d{2}:\d{2}(?:\.\d+)?))?"
)


def parse_snapshot_list(output: str) -> list[SnapshotTagInfo]:
    """Parse the human-readable snapshot table produced by QEMU.

    Handles both ``qemu-img snapshot -l`` (numeric IDs) and the monitor's
    ``info snapshots`` output (IDs are ``--`` for disk-only snapshots).  Unknown
    lines are ignored; malformed tables yield an empty list rather than
    fabricated entries.
    """

    entries: list[SnapshotTagInfo] = []
    seen: set[str] = set()
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("list of snapshots"):
            continue
        if "TAG" in stripped.upper() and "VM_SIZE" in stripped.upper().replace(" ", "_"):
            continue
        match = _SNAPSHOT_LINE_RE.match(line)
        if not match:
            continue
        snapshot_id, name, rest = match.groups()
        if name.upper() in {"TAG", "ID"} or name in seen:
            continue
        seen.add(name)
        details = _SNAPSHOT_DETAILS_RE.match(rest.strip())
        if details:
            vm_size = details.group("size").strip()
            date = details.group("date")
        else:
            vm_size = "unknown"
            date = ""
        entries.append(SnapshotTagInfo(name=name, id=snapshot_id, vm_size=vm_size, date=date))
    return entries


def require_snapshot(records: list[SnapshotRecord], name: str) -> SnapshotRecord:
    for record in records:
        if record.name == name:
            return record
    raise InvalidStateError(
        f"Snapshot '{name}' was not found.",
        hint="Refresh the snapshot list.",
    )
