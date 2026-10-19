"""Command-line interface for OpenQVM.

The CLI uses the exact same manager, backend and persistence layer as the GUI,
which makes every operation scriptable and testable without a display server.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

from openqvm import PRODUCT_NAME, PRODUCT_VERSION
from openqvm.app.context import AppContext
from openqvm.core.errors import ErrorCode, OpenQVMError, ValidationError
from openqvm.core.events import TOPIC_OPERATION_PROGRESS, OperationProgress
from openqvm.core.factory import create_vm_spec
from openqvm.core.models import (
    DiskBus,
    DiskFormat,
    Firmware,
    GuestOS,
    NetworkMode,
    PortForward,
)
from openqvm.core.validation import HostResources
from openqvm.platform.capabilities import PlatformCapabilities
from openqvm.services.metrics import human_bytes
from openqvm.storage.iso import inspect_iso
from openqvm.vm.manager import VmManager

EXIT_OK = 0
EXIT_OPERATION_FAILED = 1
EXIT_USAGE = 2
EXIT_BACKEND = 3
EXIT_NOT_FOUND = 4
EXIT_CONFLICT = 5
EXIT_INVALID_STATE = 6

_EXIT_CODES = {
    ErrorCode.VALIDATION_FAILED: EXIT_USAGE,
    ErrorCode.SECURITY: EXIT_USAGE,
    ErrorCode.UNSUPPORTED: EXIT_BACKEND,
    ErrorCode.BACKEND_UNAVAILABLE: EXIT_BACKEND,
    ErrorCode.NOT_FOUND: EXIT_NOT_FOUND,
    ErrorCode.CONFLICT: EXIT_CONFLICT,
    ErrorCode.INVALID_STATE: EXIT_INVALID_STATE,
}


class _ProgressLine:
    """Render ``operation.progress`` events on stderr for interactive terminals."""

    def __init__(self, manager: VmManager, *, enabled: bool) -> None:
        self._unsubscribers: list[Callable[[], None]] = []
        self._width = 0
        if not enabled or not sys.stderr.isatty():
            return
        self._unsubscribers.append(manager.events.subscribe(TOPIC_OPERATION_PROGRESS, self._render))

    def _render(self, event: OperationProgress) -> None:
        text = event.describe()
        self._width = max(self._width, len(text))
        print(f"\r{text:<{self._width}}", end="", file=sys.stderr, flush=True)

    def close(self) -> None:
        if not self._unsubscribers:
            return
        print("\r" + " " * self._width + "\r", end="", file=sys.stderr, flush=True)
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openqvm-cli",
        description="Manage OpenQVM virtual machines from the command line.",
    )
    parser.add_argument("--version", action="version", version=f"{PRODUCT_NAME} {PRODUCT_VERSION}")
    parser.add_argument("--data-dir", type=Path, help="Use an alternative OpenQVM data directory.")
    parser.add_argument("--qemu-dir", help="Directory containing qemu-system-x86_64 and qemu-img.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--verbose", action="store_true", help="Print diagnostic messages.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Report platform, QEMU and backend capabilities.")
    doctor.add_argument("--probe", action="store_true", help="Start QEMU briefly to verify the accelerator.")
    doctor.set_defaults(func=_cmd_doctor)

    list_parser = subparsers.add_parser("list", help="List virtual machines.")
    list_parser.add_argument("--no-refresh", action="store_true", help="Do not query running VMs.")
    list_parser.set_defaults(func=_cmd_list)

    create = subparsers.add_parser("create", help="Create a virtual machine.")
    create.add_argument("--name", required=True)
    create.add_argument("--os", default=GuestOS.LINUX.value, choices=[item.value for item in GuestOS])
    create.add_argument("--cpus", type=int, help="Virtual CPUs (default depends on the guest).")
    create.add_argument("--memory", type=int, help="Memory in MiB.")
    create.add_argument("--disk", type=int, help="Disk size in GiB.")
    create.add_argument("--disk-format", default=DiskFormat.QCOW2.value, choices=[item.value for item in DiskFormat])
    create.add_argument("--disk-bus", choices=[item.value for item in DiskBus])
    create.add_argument("--firmware", choices=[item.value for item in Firmware])
    create.add_argument("--machine", choices=["q35", "pc"])
    create.add_argument("--iso", type=Path, help="Installation image (.iso).")
    create.add_argument("--network", default=NetworkMode.NAT.value, choices=["nat", "bridged", "none"])
    create.add_argument("--bridge", help="Host bridge name for bridged networking.")
    create.add_argument(
        "--hostfwd",
        action="append",
        default=[],
        metavar="[tcp:|udp:]HOSTPORT:GUESTPORT",
        help="NAT port forward (repeatable).",
    )
    create.add_argument("--description", default="")
    create.add_argument("--accelerator", help="Override the accelerator (kvm, whpx, hvf, tcg).")
    create.add_argument("--start", action="store_true", help="Start the VM after creating it.")
    create.set_defaults(func=_cmd_create)

    for name, help_text in (
        ("start", "Start a virtual machine."),
        ("pause", "Pause a virtual machine."),
        ("resume", "Resume a paused virtual machine."),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("vm")
        command.set_defaults(func=_cmd_simple_lifecycle, action=name)

    stop = subparsers.add_parser("stop", help="Stop a virtual machine gracefully.")
    stop.add_argument("vm")
    stop.add_argument("--force", action="store_true", help="Terminate QEMU immediately.")
    stop.add_argument("--timeout", type=float, default=30.0, help="Seconds to wait for ACPI shutdown.")
    stop.set_defaults(func=_cmd_stop)

    status = subparsers.add_parser("status", help="Show the status of a virtual machine.")
    status.add_argument("vm")
    status.set_defaults(func=_cmd_status)

    delete = subparsers.add_parser("delete", help="Move a virtual machine to the trash.")
    delete.add_argument("vm")
    delete.add_argument("--yes", action="store_true", help="Confirm the destructive operation.")
    delete.set_defaults(func=_cmd_delete)

    snapshot = subparsers.add_parser("snapshot", help="Manage snapshots.")
    snapshot_sub = snapshot.add_subparsers(dest="snapshot_command", required=True)
    snapshot_list = snapshot_sub.add_parser("list", help="List snapshots.")
    snapshot_list.add_argument("vm")
    snapshot_list.set_defaults(func=_cmd_snapshot_list)
    snapshot_create = snapshot_sub.add_parser("create", help="Create a snapshot.")
    snapshot_create.add_argument("vm")
    snapshot_create.add_argument("name")
    snapshot_create.add_argument("--description", default="")
    snapshot_create.set_defaults(func=_cmd_snapshot_create)
    snapshot_restore = snapshot_sub.add_parser("restore", help="Restore a snapshot.")
    snapshot_restore.add_argument("vm")
    snapshot_restore.add_argument("name")
    snapshot_restore.set_defaults(func=_cmd_snapshot_restore)
    snapshot_delete = snapshot_sub.add_parser("delete", help="Delete a snapshot.")
    snapshot_delete.add_argument("vm")
    snapshot_delete.add_argument("name")
    snapshot_delete.add_argument("--yes", action="store_true")
    snapshot_delete.set_defaults(func=_cmd_snapshot_delete)

    clone = subparsers.add_parser("clone", help="Clone a stopped virtual machine.")
    clone.add_argument("vm")
    clone.add_argument("--name", required=True)
    clone.set_defaults(func=_cmd_clone)

    export = subparsers.add_parser("export", help="Export a stopped VM to a .oqv package.")
    export.add_argument("vm")
    export.add_argument("output", type=Path)
    export.add_argument("--overwrite", action="store_true")
    export.set_defaults(func=_cmd_export)

    import_parser = subparsers.add_parser("import", help="Import a .oqv package.")
    import_parser.add_argument("package", type=Path)
    import_parser.add_argument("--name", help="Name for the imported VM.")
    import_parser.set_defaults(func=_cmd_import)

    resize = subparsers.add_parser("resize", help="Enlarge a disk (offline by default).")
    resize.add_argument("vm")
    resize.add_argument("disk_id")
    resize.add_argument("size_gib", type=int)
    resize.add_argument("--live", action="store_true", help="Grow the disk while the VM is running (QEMU block_resize).")
    resize.set_defaults(func=_cmd_resize)

    attach = subparsers.add_parser("attach-iso", help="Attach an ISO to a VM.")
    attach.add_argument("vm")
    attach.add_argument("iso", type=Path)
    attach.add_argument("--live", action="store_true", help="Change the medium while the VM is running.")
    attach.add_argument("--force", action="store_true", help="Override QEMU's media lock (may disrupt the guest).")
    attach.set_defaults(func=_cmd_attach_iso)

    eject = subparsers.add_parser("eject-iso", help="Eject the ISO from a VM.")
    eject.add_argument("vm")
    eject.add_argument("--live", action="store_true", help="Eject the medium while the VM is running.")
    eject.add_argument("--force", action="store_true", help="Override QEMU's media lock (may disrupt the guest).")
    eject.set_defaults(func=_cmd_eject_iso)

    logs = subparsers.add_parser("logs", help="Show QEMU and serial log tails.")
    logs.add_argument("vm")
    logs.add_argument("--lines", type=int, default=80)
    logs.set_defaults(func=_cmd_logs)

    check = subparsers.add_parser("check", help="Run qemu-img check on the VM disks (VM must be stopped).")
    check.add_argument("vm")
    check.add_argument("--timeout", type=float, default=1800.0, help="Seconds allowed per disk.")
    check.set_defaults(func=_cmd_check)

    backup = subparsers.add_parser("backup", help="Back up a stopped VM, or every VM that is due.")
    backup.add_argument("vm", nargs="?", help="VM name or id; omit together with --due.")
    backup.add_argument("--due", action="store_true", help="Use the saved schedule and back up every due VM.")
    backup.add_argument("--keep", type=int, default=None, help="Packages to keep per VM.")
    backup.add_argument("--destination", type=Path, default=None, help="Folder for the packages.")
    backup.set_defaults(func=_cmd_backup)

    backups = subparsers.add_parser("backups", help="List the backups of a VM.")
    backups.add_argument("vm")
    backups.set_defaults(func=_cmd_backups)

    gui = subparsers.add_parser("gui", help="Start the desktop application.")
    gui.set_defaults(func=_cmd_gui)

    return parser


def _manager(args: argparse.Namespace) -> AppContext:
    return AppContext.create(data_dir=args.data_dir, qemu_dir=args.qemu_dir)


def _resolve_vm(manager: VmManager, reference: str):  # type: ignore[no-untyped-def]
    from openqvm.core.ids import is_uuid

    if is_uuid(reference):
        return manager.get_spec(reference)
    specs = manager.store.list_specs()
    matches = [
        spec
        for spec in specs
        if spec.name.lower() == reference.lower() or spec.id.startswith(reference)
    ]
    if not matches:
        raise OpenQVMError(
            f"No virtual machine matches '{reference}'.",
            code=ErrorCode.NOT_FOUND,
            hint="Run 'openqvm-cli list' to see available VMs.",
        )
    if len(matches) > 1:
        raise OpenQVMError(
            f"'{reference}' matches more than one virtual machine.",
            code=ErrorCode.CONFLICT,
            hint="Use the full VM identifier.",
        )
    return matches[0]


def _print(args: argparse.Namespace, payload, *, text: str | None = None) -> None:
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    elif text is not None:
        print(text)


def _status_text(manager: VmManager, spec) -> str:  # type: ignore[no-untyped-def]
    try:
        runtime = manager.get_summary(spec.id).runtime
        return runtime.status.value
    except OpenQVMError:
        return "error"


def _cmd_doctor(args: argparse.Namespace) -> int:
    context = _manager(args)
    capabilities = context.capabilities
    payload = {
        "product": f"{PRODUCT_NAME} {PRODUCT_VERSION}",
        "data_dir": str(context.paths.root),
        "capabilities": capabilities.to_dict(),
        "counts": context.manager.counts(),
    }
    if args.probe:
        payload["probe"] = _probe_accelerator(capabilities, context.paths)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK
    print(f"{PRODUCT_NAME} {PRODUCT_VERSION}")
    print(f"Data directory : {context.paths.root}")
    print(f"Platform       : {capabilities.os_family}/{capabilities.architecture}")
    if capabilities.qemu:
        print(f"QEMU           : {capabilities.qemu.version} ({capabilities.qemu.system_path})")
        print(f"qemu-img       : {capabilities.qemu.image_path}")
        print(f"Accelerator    : {capabilities.default_accelerator or 'none'}")
        print(f"Accelerators   : {', '.join(capabilities.accelerator_names) or 'none'}")
    else:
        print("QEMU           : NOT FOUND")
    print(f"UEFI firmware  : {capabilities.uefi.code_path if capabilities.uefi else 'not found'}")
    print(f"Bridged net    : {'yes' if capabilities.bridged_supported else 'no'} - {capabilities.bridged_detail}")
    print(f"Host-only net  : {'yes' if capabilities.host_only_supported else 'no'} - {capabilities.host_only_detail}")
    print(f"Shared folders : {'yes' if capabilities.shared_folders_supported else 'no'} - {capabilities.shared_folders_detail}")
    print(f"TPM            : {'yes' if capabilities.tpm_supported else 'no'} - {capabilities.tpm_detail}")
    print("Diagnostics:")
    for line in capabilities.diagnostics:
        print(f"  - {line}")
    if args.probe:
        for accelerator, result in payload["probe"].items():
            print(f"Probe {accelerator:<5}: {result['status']} {result.get('detail', '')}")
    counts = payload["counts"]
    print(f"VMs            : {counts['total']} total, {counts['running']} running, {counts['paused']} paused")
    return EXIT_OK


def _probe_accelerator(capabilities: PlatformCapabilities, paths) -> dict[str, dict[str, str]]:  # type: ignore[no-untyped-def]
    from openqvm.vm.qemu.process import launch_qemu, process_matches, terminate_process

    results: dict[str, dict[str, str]] = {}
    if capabilities.qemu is None:
        return results
    for name in capabilities.accelerator_names:
        if name == "tcg":
            results[name] = {"status": "supported", "detail": "software emulation"}
            continue
        probe_dir = paths.logs_dir / "probe"
        probe_dir.mkdir(parents=True, exist_ok=True)
        log_path = probe_dir / f"{name}.log"
        pid_file = probe_dir / f"{name}.pid"
        argv = [
            str(capabilities.qemu.system_path),
            "-machine",
            f"q35,accel={name}",
            "-m",
            "128",
            "-display",
            "none",
            "-nodefaults",
            "-S",
            "-pidfile",
            str(pid_file),
        ]
        try:
            info = launch_qemu(argv, log_path=log_path, cwd=probe_dir)
        except Exception as exc:
            results[name] = {"status": "failed", "detail": str(exc)}
            continue
        time.sleep(1.5)
        alive = process_matches(info.pid, info.create_time)
        terminate_process(info.pid, info.create_time, timeout=5)
        if alive:
            results[name] = {"status": "available", "detail": "QEMU stayed running"}
        else:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-800:].strip()
            results[name] = {"status": "failed", "detail": detail or "QEMU exited"}
    return results


def _cmd_list(args: argparse.Namespace) -> int:
    context = _manager(args)
    summaries, errors = context.manager.list_summaries(refresh=not args.no_refresh)
    rows = []
    for summary in summaries:
        spec = summary.spec
        runtime = summary.runtime
        rows.append(
            {
                "id": spec.id,
                "name": spec.name,
                "os": spec.guest_os.value,
                "status": runtime.status.value,
                "vcpus": spec.cpu.vcpus,
                "memory_mib": spec.memory_mib,
                "disk_gib": round(spec.total_disk_bytes / 1024**3),
                "accelerator": runtime.accelerator,
                "pid": runtime.pid,
                "last_error": runtime.last_error,
            }
        )
    if args.json:
        print(json.dumps({"vms": rows, "errors": errors}, indent=2))
        return EXIT_OK
    if not rows:
        print("No virtual machines. Create one with 'openqvm-cli create'.")
    else:
        header = f"{'ID':8}  {'NAME':28} {'STATUS':9} {'vCPU':>4} {'RAM':>7} {'DISK':>7}  OS"
        print(header)
        print("-" * len(header))
        for row in rows:
            print(
                f"{row['id'][:8]:8}  {row['name'][:28]:28} {row['status']:9} "
                f"{row['vcpus']:>4} {row['memory_mib']:>5}Mi {row['disk_gib']:>5}Gi  {row['os']}"
            )
    for error in errors:
        print(f"warning: {error}", file=sys.stderr)
    return EXIT_OK


def _parse_hostfwd(values: list[str]) -> list[PortForward]:
    forwards: list[PortForward] = []
    for value in values:
        protocol = "tcp"
        spec = value
        if ":" in value and value.split(":", 1)[0].lower() in {"tcp", "udp"}:
            protocol, spec = value.split(":", 1)
        try:
            host_part, guest_part = spec.split(":", 1)
            host_port = int(host_part)
            guest_port = int(guest_part)
        except ValueError as exc:
            raise ValidationError(
                f"Invalid port forward '{value}'. Use [tcp:|udp:]HOSTPORT:GUESTPORT."
            ) from exc
        forwards.append(PortForward(protocol=protocol, host_port=host_port, guest_port=guest_port))
    return forwards


def _cmd_create(args: argparse.Namespace) -> int:
    context = _manager(args)
    host = HostResources.detect()
    guest_os = GuestOS(args.os)
    iso = str(args.iso.expanduser().resolve()) if args.iso else None
    if iso:
        inspect_iso(iso)
    mode = None if args.network == "none" else NetworkMode(args.network)
    if mode is NetworkMode.BRIDGED and not args.bridge:
        raise ValidationError("Bridged networking requires --bridge.")
    spec = create_vm_spec(
        name=args.name,
        guest_os=guest_os,
        host=host,
        vcpus=args.cpus,
        memory_mib=args.memory,
        disk_bytes=args.disk * 1024**3 if args.disk else None,
        disk_format=DiskFormat(args.disk_format),
        disk_bus=DiskBus(args.disk_bus) if args.disk_bus else None,
        firmware=Firmware(args.firmware) if args.firmware else None,
        machine=args.machine,
        cdrom_iso=iso,
        network_mode=mode,
        bridge=args.bridge,
        port_forwards=_parse_hostfwd(args.hostfwd),
        description=args.description,
        accelerator=args.accelerator,
    )
    progress = _ProgressLine(context.manager, enabled=not args.json)
    try:
        created = context.manager.create_vm(spec)
        payload = {"vm": created.to_dict()}
        if args.start:
            runtime = context.manager.start_vm(created.id)
            payload["runtime"] = runtime.to_dict()
    finally:
        progress.close()
    _print(
        args,
        payload,
        text=f"Created '{created.name}' ({created.id})" + (" and started it." if args.start else "."),
    )
    return EXIT_OK


def _cmd_simple_lifecycle(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    action = args.action
    if action == "start":
        runtime = context.manager.start_vm(spec.id)
    elif action == "pause":
        runtime = context.manager.pause_vm(spec.id)
    else:
        runtime = context.manager.resume_vm(spec.id)
    _print(args, {"runtime": runtime.to_dict()}, text=f"{spec.name}: {runtime.status.value}")
    return EXIT_OK


def _cmd_stop(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    runtime = context.manager.stop_vm(spec.id, force=args.force, timeout=args.timeout)
    _print(args, {"runtime": runtime.to_dict()}, text=f"{spec.name}: {runtime.status.value}")
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    summary = context.manager.get_summary(spec.id)
    payload = summary.to_dict()
    metrics = context.metrics.snapshot(
        [(spec.id, summary.runtime.pid, summary.runtime.process_create_time)]
    )
    vm_metrics = metrics.per_vm.get(spec.id)
    payload["metrics"] = {
        "cpu_percent": vm_metrics.cpu_percent if vm_metrics else None,
        "memory_rss_bytes": vm_metrics.memory_rss_bytes if vm_metrics else None,
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK
    print(f"Name       : {spec.name}")
    print(f"ID         : {spec.id}")
    print(f"Status     : {summary.runtime.status.value}")
    print(f"vCPU       : {spec.cpu.vcpus}")
    print(f"Memory     : {spec.memory_mib} MiB")
    print(f"Disks      : {len(spec.disks)} ({human_bytes(spec.total_disk_bytes)} virtual)")
    print(f"Firmware   : {spec.firmware.value} / {spec.machine}")
    print(f"ISO        : {spec.cdrom_iso or 'none'}")
    print(f"Accelerator: {summary.runtime.accelerator or context.capabilities.default_accelerator or 'none'}")
    if summary.runtime.vnc_port:
        print(f"VNC        : 127.0.0.1:{summary.runtime.vnc_port}")
    if vm_metrics and vm_metrics.cpu_percent is not None:
        print(f"CPU usage  : {vm_metrics.cpu_percent:.1f}%")
    if vm_metrics and vm_metrics.memory_rss_bytes:
        print(f"RSS        : {human_bytes(vm_metrics.memory_rss_bytes)}")
    if summary.runtime.last_error:
        print(f"Last error : {summary.runtime.last_error}")
    return EXIT_OK


def _cmd_delete(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    if not args.yes:
        raise ValidationError(
            "Refusing to delete without confirmation.",
            hint="Re-run with --yes to move the VM to the trash.",
        )
    result = context.manager.delete_vm(spec.id)
    _print(
        args,
        {"vm_id": result.vm_id, "quarantine_path": str(result.quarantine_path)},
        text=f"Moved '{spec.name}' to the trash: {result.quarantine_path}",
    )
    return EXIT_OK


def _cmd_snapshot_list(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    views = context.manager.list_snapshots(spec.id)
    rows = [view.to_dict() for view in views]
    if args.json:
        print(json.dumps(rows, indent=2))
        return EXIT_OK
    if not rows:
        print("No snapshots.")
    for row in rows:
        memory = "memory+disk" if row["includes_memory"] else "disk only"
        missing = "" if row["present"] else " [MISSING ON DISK]"
        print(f"{row['name']:<24} {memory:<12} {row['created_at']}{missing}")
    return EXIT_OK


def _cmd_snapshot_create(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    view = context.manager.create_snapshot(spec.id, args.name, args.description)
    _print(args, view.to_dict(), text=f"Created snapshot '{view.name}' ({'memory+disk' if view.includes_memory else 'disk only'}).")
    return EXIT_OK


def _cmd_snapshot_restore(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    runtime = context.manager.restore_snapshot(spec.id, args.name)
    _print(args, {"runtime": runtime.to_dict()}, text=f"Restored snapshot '{args.name}'. VM status: {runtime.status.value}")
    return EXIT_OK


def _cmd_snapshot_delete(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    if not args.yes:
        raise ValidationError("Refusing to delete a snapshot without --yes.")
    context.manager.delete_snapshot(spec.id, args.name)
    _print(args, {"deleted": args.name}, text=f"Deleted snapshot '{args.name}'.")
    return EXIT_OK


def _cmd_clone(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    progress = _ProgressLine(context.manager, enabled=not args.json)
    try:
        result = context.manager.clone_vm(spec.id, args.name)
    finally:
        progress.close()
    payload = {"vm": result.spec.to_dict(), "warnings": result.warnings}
    _print(args, payload, text=f"Cloned '{spec.name}' as '{result.spec.name}' ({result.spec.id}).")
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return EXIT_OK


def _cmd_export(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    progress = _ProgressLine(context.manager, enabled=not args.json)
    try:
        result = context.manager.export_vm(
            spec.id, args.output.expanduser().resolve(), overwrite=args.overwrite
        )
    finally:
        progress.close()
    payload = {"path": str(result.path), "size_bytes": result.size_bytes, "sha256": result.sha256, "warnings": result.warnings}
    _print(args, payload, text=f"Exported to {result.path} ({human_bytes(result.size_bytes)}).")
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return EXIT_OK


def _cmd_import(args: argparse.Namespace) -> int:
    context = _manager(args)
    progress = _ProgressLine(context.manager, enabled=not args.json)
    try:
        result = context.manager.import_vm(args.package.expanduser().resolve(), new_name=args.name)
    finally:
        progress.close()
    payload = {"vm": result.spec.to_dict(), "warnings": result.warnings}
    _print(args, payload, text=f"Imported '{result.spec.name}' ({result.spec.id}).")
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return EXIT_OK


def _cmd_resize(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    updated = context.manager.resize_disk(spec.id, args.disk_id, args.size_gib * 1024**3, live=args.live)
    mode = "live" if args.live else "offline"
    message = (
        f"Resized disk ({mode}) to {args.size_gib} GiB. "
        "The guest may need to rescan the block device and grow its file system."
    )
    _print(args, {"vm": updated.to_dict(), "live": args.live}, text=message)
    return EXIT_OK


def _cmd_attach_iso(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    updated = context.manager.attach_iso(
        spec.id, str(args.iso.expanduser().resolve()), live=args.live, force=args.force
    )
    _print(
        args,
        {"vm": updated.to_dict(), "live": args.live},
        text=f"Attached {updated.cdrom_iso}" + (" (live)." if args.live else "."),
    )
    return EXIT_OK


def _cmd_eject_iso(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    updated = context.manager.eject_iso(spec.id, live=args.live, force=args.force)
    _print(
        args,
        {"vm": updated.to_dict(), "live": args.live},
        text="Ejected the installation image" + (" (live)." if args.live else "."),
    )
    return EXIT_OK


def _cmd_logs(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    qemu_log = context.manager.qemu_log_path(spec.id)
    serial_log = context.manager.serial_log_path(spec.id)
    payload = {"qemu_log": _tail(qemu_log, args.lines), "serial_log": _tail(serial_log, args.lines)}
    if args.json:
        print(json.dumps(payload, indent=2))
        return EXIT_OK
    print(f"== QEMU log ({qemu_log}) ==")
    print(payload["qemu_log"] or "(empty)")
    print(f"\n== Serial console ({serial_log}) ==")
    print(payload["serial_log"] or "(empty)")
    return EXIT_OK


def _cmd_check(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    summary = context.manager.check_disks(spec.id, timeout=args.timeout)
    if args.json:
        print(json.dumps({"vm": spec.name, **summary.to_dict()}, indent=2))
        return EXIT_OK if summary.ok else EXIT_OPERATION_FAILED
    print(f"Disk check for '{spec.name}':")
    for result in summary.results:
        state = "OK" if result.ok else "PROBLEMS"
        print(f"  {result.path.name:<40} {state}")
        if not result.ok and result.output.strip():
            for line in result.output.strip().splitlines()[:20]:
                print(f"    {line}")
    return EXIT_OK if summary.ok else EXIT_OPERATION_FAILED


def _cmd_backup(args: argparse.Namespace) -> int:
    from openqvm.services.backups import BackupPolicy

    context = _manager(args)
    saved = context.backup_policy()
    keep = args.keep if args.keep is not None else saved.keep
    destination = args.destination.expanduser().resolve() if args.destination else saved.directory
    progress = _ProgressLine(context.manager, enabled=not args.json)
    try:
        if args.due or args.vm is None:
            policy = BackupPolicy(
                directory=destination,
                enabled=True,
                interval_hours=saved.interval_hours,
                keep=keep,
            )
            run = context.manager.run_scheduled_backups(policy)
        else:
            spec = _resolve_vm(context.manager, args.vm)
            result = context.manager.backup_vm(spec.id, keep=keep, destination=destination)
    finally:
        progress.close()

    if args.due or args.vm is None:
        if args.json:
            print(json.dumps(run.to_dict(), indent=2, default=str))
        else:
            print(f"Created {run.created} backup(s).")
            for created in run.results:
                print(f"  {created.path} ({human_bytes(created.entry.size_bytes)})")
            for name in run.skipped:
                print(f"  skipped '{name}' (running)")
        for error in run.errors:
            print(f"error: {error}", file=sys.stderr)
        return EXIT_OK if not run.errors else EXIT_OPERATION_FAILED

    payload = {"vm": spec.name, "backup": result.entry.to_dict(), "pruned": list(result.pruned)}
    text = (
        f"Backed up '{spec.name}' to {result.path} ({human_bytes(result.entry.size_bytes)})."
        + (f"\nRemoved {len(result.pruned)} old backup(s)." if result.pruned else "")
    )
    _print(args, payload, text=text)
    return EXIT_OK


def _cmd_backups(args: argparse.Namespace) -> int:
    context = _manager(args)
    spec = _resolve_vm(context.manager, args.vm)
    entries = context.manager.list_backups(spec.id)
    payload = {"vm": spec.name, "backups": [entry.to_dict() for entry in entries]}
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK
    if not entries:
        print(f"No backups for '{spec.name}' (folder: {context.backup_policy().directory}).")
        return EXIT_OK
    for entry in entries:
        print(f"{entry.created_at}  {human_bytes(entry.size_bytes):>10}  {entry.reason:<9}  {entry.path}")
    return EXIT_OK


def _tail(path: Path, lines: int) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-max(1, lines) :])


def _cmd_gui(args: argparse.Namespace) -> int:
    from openqvm.ui.app import run_gui

    return run_gui(data_dir=args.data_dir, qemu_dir=args.qemu_dir)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except OpenQVMError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": exc.to_dict()}, indent=2), file=sys.stderr)
        else:
            print(f"error[{exc.code.value}]: {exc.message}", file=sys.stderr)
            if exc.hint:
                print(f"hint: {exc.hint}", file=sys.stderr)
        return _EXIT_CODES.get(exc.code, EXIT_OPERATION_FAILED)
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
