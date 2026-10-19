"""QEMU backend implementation."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from openqvm.core.errors import (
    BackendUnavailableError,
    ConflictError,
    InvalidStateError,
    OperationFailedError,
    SecurityError,
    UnsupportedFeatureError,
    ValidationError,
)
from openqvm.core.logging import get_logger
from openqvm.core.models import Firmware, VMRuntime, VMSpec, VMStatus
from openqvm.core.paths import AppPaths
from openqvm.platform.capabilities import PlatformCapabilities
from openqvm.store import VmStore
from openqvm.vm.backend import BackendCapabilities
from openqvm.vm.qemu.command import (
    allocate_tcp_port,
    allocate_vnc_display,
    build_launch_plan,
)
from openqvm.vm.qemu.medium import find_cdrom_device, medium_filename
from openqvm.vm.qemu.process import (
    ProcessInfo,
    launch_qemu,
    process_matches,
    read_log_tail,
    terminate_process,
    wait_for_exit,
)
from openqvm.vm.qemu.qmp import QmpClient, QmpCommandError

log = get_logger("qemu.backend")


class QemuBackend:
    name = "qemu"
    capabilities = BackendCapabilities(
        name="qemu",
        supports_pause=True,
        supports_live_snapshots=True,
        supports_memory_snapshots=True,
        supports_live_iso_change=True,
        supports_online_disk_resize=True,
    )

    def __init__(self, paths: AppPaths, store: VmStore, platform: PlatformCapabilities) -> None:
        self.paths = paths
        self.store = store
        self.platform = platform

    # -- helpers --------------------------------------------------------------

    def _require_backend(self) -> None:
        if not self.platform.backend_available:
            details = " ".join(self.platform.diagnostics)
            raise BackendUnavailableError(
                "The QEMU virtualization backend is not available.",
                hint=(
                    "Install QEMU 8 or newer and make sure qemu-system-x86_64 and qemu-img are available. "
                    "OpenQVM never falls back to a mock backend for real VMs."
                ),
                details={"diagnostics": details},
            )

    def _ensure_firmware_vars(self, spec: VMSpec) -> Path | None:
        if spec.firmware is not Firmware.UEFI:
            return None
        if self.platform.uefi is None:
            raise ValidationError(
                "UEFI firmware files were not found on this host.",
                hint="Install the edk2/OVMF firmware package or switch the VM to BIOS.",
            )
        firmware_dir = self.paths.vm_firmware_dir(spec.id)
        firmware_dir.mkdir(parents=True, exist_ok=True)
        variables = firmware_dir / "OVMF_VARS.fd"
        if not variables.is_file():
            try:
                shutil.copyfile(self.platform.uefi.vars_template, variables)
            except OSError as exc:
                raise OperationFailedError("Could not create the UEFI variable store.", cause=exc) from exc
        return variables

    def _check_disks(self, spec: VMSpec) -> None:
        for disk in spec.disks:
            path = self.store.disk_path(spec, disk)
            if path.is_symlink():
                raise SecurityError(
                    "A virtual disk is a symbolic link, which is not allowed.",
                    hint="Replace the link with a regular disk file.",
                    details={"disk": disk.filename},
                )
            if not path.is_file():
                raise ValidationError(
                    f"The disk file is missing: {disk.filename}",
                    hint="Restore the disk from a backup or recreate the virtual machine.",
                )

    def _plan(self, spec: VMSpec):  # type: ignore[no-untyped-def]
        qmp_port = allocate_tcp_port()
        if spec.display.vnc_enabled:
            display, vnc_port = allocate_vnc_display()
        else:
            display, vnc_port = None, None
        firmware_vars = self._ensure_firmware_vars(spec)
        return build_launch_plan(
            spec,
            self.paths,
            self.platform,
            qmp_port=qmp_port,
            vnc_display=display,
            vnc_port=vnc_port,
            firmware_vars=firmware_vars,
        )

    def qmp_client(self, runtime: VMRuntime, *, timeout: float = 10.0) -> QmpClient:
        if not runtime.qmp_port:
            raise InvalidStateError("The virtual machine is not exposing a QEMU control port.")
        client = QmpClient("127.0.0.1", runtime.qmp_port, timeout=timeout)
        client.connect()
        return client

    # -- live storage operations ---------------------------------------------

    def _cdrom_device(self, client: QmpClient) -> dict:
        blocks = client.execute("query-block")
        if not isinstance(blocks, list):
            raise OperationFailedError("QEMU returned an invalid block device list.")
        entry = find_cdrom_device(blocks)
        if entry is None:
            raise UnsupportedFeatureError(
                "This virtual machine has no CD-ROM drive.",
                hint="Restart the VM so OpenQVM can add one, then try again.",
            )
        if not entry.get("qdev"):
            raise OperationFailedError(
                "QEMU did not report the CD-ROM device path.",
                hint="Check the QEMU version and the VM log.",
            )
        return entry

    @staticmethod
    def _medium_error(error: QmpCommandError) -> OperationFailedError:
        locked = "locked" in error.message.lower()
        hint = (
            "The guest is currently using the media. Stop the VM, or retry with force enabled "
            "(openqvm-cli --force), which can disrupt the guest."
            if locked
            else "Check the image path and the QEMU log."
        )
        return OperationFailedError(
            "The installation medium could not be changed.",
            hint=hint,
            details={"qmp_error": error.message},
            cause=error,
        )

    def change_medium(
        self, spec: VMSpec, runtime: VMRuntime, iso_path: str | None, *, force: bool = False
    ) -> None:
        """Insert, replace or eject the CD-ROM medium while the VM is running."""

        client = self.qmp_client(runtime, timeout=20.0)
        try:
            entry = self._cdrom_device(client)
            qdev = str(entry["qdev"])
            inserted = bool(entry.get("inserted"))
            if entry.get("locked") and not force and (inserted or iso_path is not None):
                raise OperationFailedError(
                    "The guest is currently using the CD-ROM medium.",
                    hint="Stop the VM, or retry with force enabled (openqvm-cli --force), "
                    "which can disrupt the guest.",
                    details={"device": entry.get("device"), "medium": medium_filename(entry)},
                )
            try:
                if iso_path is not None:
                    arguments: dict[str, object] = {
                        "id": qdev,
                        "filename": iso_path,
                        "format": "raw",
                    }
                    if force:
                        arguments["force"] = True
                    client.execute("blockdev-change-medium", arguments)
                else:
                    if inserted and not entry.get("tray_open"):
                        open_arguments: dict[str, object] = {"id": qdev}
                        if force:
                            open_arguments["force"] = True
                        client.execute("blockdev-open-tray", open_arguments)
                        entry = self._cdrom_device(client)
                        if not entry.get("tray_open"):
                            raise OperationFailedError(
                                "The guest did not release the CD-ROM tray.",
                                hint="Stop the VM, or retry with force enabled "
                                "(openqvm-cli --force), which can disrupt the guest.",
                            )
                    if inserted:
                        client.execute("blockdev-remove-medium", {"id": qdev})
                    client.execute("blockdev-close-tray", {"id": qdev})
            except QmpCommandError as exc:
                raise self._medium_error(exc) from exc
            entry = self._cdrom_device(client)
        finally:
            client.close()
        current = medium_filename(entry)
        if iso_path is None:
            if current is not None:
                raise OperationFailedError(
                    "QEMU reported that the medium is still present after ejecting it.",
                    details={"medium": current},
                )
        elif current is None or Path(current).resolve() != Path(iso_path).resolve():
            raise OperationFailedError(
                "QEMU did not report the requested medium after changing it.",
                details={"expected": iso_path, "actual": current},
            )
        log.info("Changed CD-ROM medium for %s to %s", spec.name, iso_path or "(empty)")

    def resize_disk_live(
        self, spec: VMSpec, runtime: VMRuntime, disk_index: int, new_size_bytes: int
    ) -> None:
        """Grow a disk through QEMU while the VM is running."""

        device = f"drive{disk_index}"
        client = self.qmp_client(runtime, timeout=20.0)
        try:
            try:
                client.execute("block_resize", {"device": device, "size": new_size_bytes})
            except QmpCommandError as exc:
                raise OperationFailedError(
                    "QEMU could not resize the running disk.",
                    hint="Verify that the disk is attached with an id QEMU can resize and that it is a qcow2 image.",
                    details={"qmp_error": exc.message, "device": device},
                    cause=exc,
                ) from exc
            blocks = client.execute("query-block")
            entry = next((item for item in blocks if item.get("device") == device), None)
            reported = None
            if isinstance(entry, dict):
                reported = ((entry.get("inserted") or {}).get("image") or {}).get("virtual-size")
            if reported != new_size_bytes:
                raise OperationFailedError(
                    "QEMU did not apply the requested disk size.",
                    details={"expected": new_size_bytes, "actual": reported, "device": device},
                )
        finally:
            client.close()
        log.info("Live-resized %s on %s to %s bytes", device, spec.name, new_size_bytes)

    # -- lifecycle ------------------------------------------------------------

    def start(self, spec: VMSpec, runtime: VMRuntime) -> VMRuntime:
        self._require_backend()
        self._check_disks(spec)
        if process_matches(runtime.pid, runtime.process_create_time):
            raise ConflictError("The virtual machine is already running.")

        plan = self._plan(spec)
        serial_log = plan.serial_log
        serial_log.parent.mkdir(parents=True, exist_ok=True)
        serial_log.write_bytes(b"")
        plan.qemu_log.parent.mkdir(parents=True, exist_ok=True)
        plan.qemu_log.write_bytes(b"")
        plan.pid_file.unlink(missing_ok=True)

        runtime.status = VMStatus.STARTING
        runtime.started_at = None
        runtime.stopped_at = None
        runtime.last_error = None
        runtime.last_error_code = None
        runtime.accelerator = plan.accelerator
        runtime.qmp_port = plan.qmp_port
        runtime.vnc_port = plan.vnc_port
        self.store.save_runtime(runtime)

        try:
            info: ProcessInfo = launch_qemu(plan.argv, log_path=plan.qemu_log, cwd=self.paths.vm_dir(spec.id))
        except Exception as exc:
            runtime.status = VMStatus.ERROR
            runtime.last_error = str(exc)
            error_code = getattr(exc, "code", None)
            runtime.last_error_code = getattr(error_code, "value", error_code)
            self.store.save_runtime(runtime)
            raise
        runtime.pid = info.pid
        runtime.process_create_time = info.create_time
        self.store.save_runtime(runtime)

        try:
            client = QmpClient("127.0.0.1", plan.qmp_port, timeout=10.0)
            client.connect(timeout=30.0)
            try:
                status = client.query_status()
            finally:
                client.close()
            if not process_matches(info.pid, info.create_time):
                raise OperationFailedError("QEMU exited during startup.", details={"log": read_log_tail(plan.qemu_log)})
            runtime.status = _map_qmp_status(status)
            runtime.started_at = datetime.now(UTC).isoformat(timespec="seconds")
            self.store.save_runtime(runtime)
            log.info("VM %s started (pid=%s, accel=%s)", spec.name, info.pid, plan.accelerator)
            return runtime
        except Exception as exc:
            terminate_process(info.pid, info.create_time, timeout=5)
            log_tail = read_log_tail(plan.qemu_log)
            runtime.status = VMStatus.ERROR
            runtime.last_error = str(exc)
            runtime.last_error_code = getattr(getattr(exc, "code", None), "value", None)
            self.store.save_runtime(runtime)
            message = "The virtual machine failed to start."
            if log_tail:
                message += f" QEMU reported: {log_tail[-1200:]}"
            raise OperationFailedError(
                message,
                hint="Verify the accelerator, firmware, disk images and installed QEMU version.",
                details={"log": log_tail[-4000:]},
                cause=exc,
            ) from exc

    def stop(self, spec: VMSpec, runtime: VMRuntime, *, force: bool = False, timeout: float = 30.0) -> VMRuntime:
        if not process_matches(runtime.pid, runtime.process_create_time):
            return self._mark_stopped(runtime)

        if force:
            self._try_quit(runtime)
            if not wait_for_exit(runtime.pid, runtime.process_create_time, timeout=5):
                terminate_process(runtime.pid, runtime.process_create_time, timeout=10)
            return self._mark_stopped(runtime)

        client: QmpClient | None = None
        try:
            client = self.qmp_client(runtime, timeout=5.0)
            if runtime.status is VMStatus.PAUSED:
                try:
                    client.cont()
                except Exception:
                    pass
            client.system_powerdown()
        except Exception as exc:
            if not process_matches(runtime.pid, runtime.process_create_time):
                return self._mark_stopped(runtime)
            log.warning("Graceful shutdown failed for %s: %s", spec.name, exc)
            raise OperationFailedError(
                "The guest could not be asked to shut down.",
                hint="Use 'Force stop' to terminate the VM, which may lose unsaved guest data.",
                details={"state": runtime.status.value},
                cause=exc,
            ) from exc
        finally:
            if client is not None:
                client.close()

        if wait_for_exit(runtime.pid, runtime.process_create_time, timeout=timeout):
            return self._mark_stopped(runtime)

        raise OperationFailedError(
            "The guest did not shut down in time.",
            hint="The guest may ignore ACPI power requests. Use 'Force stop' to terminate it.",
            details={"timeout_seconds": timeout},
        )

    def pause(self, spec: VMSpec, runtime: VMRuntime) -> VMRuntime:
        if runtime.status is not VMStatus.RUNNING:
            raise InvalidStateError("Only a running virtual machine can be paused.")
        client = self.qmp_client(runtime)
        try:
            client.stop()
        finally:
            client.close()
        runtime.status = VMStatus.PAUSED
        self.store.save_runtime(runtime)
        return runtime

    def resume(self, spec: VMSpec, runtime: VMRuntime) -> VMRuntime:
        if runtime.status is not VMStatus.PAUSED:
            raise InvalidStateError("Only a paused virtual machine can be resumed.")
        client = self.qmp_client(runtime)
        try:
            client.cont()
        finally:
            client.close()
        runtime.status = VMStatus.RUNNING
        self.store.save_runtime(runtime)
        return runtime

    def refresh(self, spec: VMSpec, runtime: VMRuntime) -> VMRuntime:
        if not process_matches(runtime.pid, runtime.process_create_time):
            if runtime.status.is_active:
                log.info("QEMU for %s is no longer running", spec.name)
                return self._mark_stopped(runtime)
            if runtime.status is VMStatus.STOPPED and runtime.pid is not None:
                return self._mark_stopped(runtime)
            return runtime

        try:
            client = self.qmp_client(runtime, timeout=3.0)
            try:
                status = client.query_status()
            finally:
                client.close()
        except Exception as exc:
            if runtime.status is not VMStatus.UNKNOWN:
                runtime.status = VMStatus.UNKNOWN
                runtime.last_error = f"QEMU control interface unavailable: {exc}"
                self.store.save_runtime(runtime)
            return runtime

        mapped = _map_qmp_status(status)
        changed = mapped is not runtime.status
        if changed:
            runtime.status = mapped
            if mapped is VMStatus.ERROR:
                runtime.last_error = f"QEMU reported status: {status}"
            self.store.save_runtime(runtime)
        return runtime

    def _try_quit(self, runtime: VMRuntime) -> None:
        try:
            client = self.qmp_client(runtime, timeout=3.0)
            try:
                client.quit()
            finally:
                client.close()
        except Exception:
            pass

    def _mark_stopped(self, runtime: VMRuntime) -> VMRuntime:
        runtime.status = VMStatus.STOPPED
        runtime.pid = None
        runtime.process_create_time = None
        runtime.qmp_port = None
        runtime.vnc_port = None
        runtime.stopped_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.store.save_runtime(runtime)
        return runtime


def _map_qmp_status(status: str) -> VMStatus:
    if status == "running":
        return VMStatus.RUNNING
    if status in {"paused", "suspended"}:
        return VMStatus.PAUSED
    if status in {"shutdown", "shutting-down"}:
        return VMStatus.STOPPING
    if status in {"guest-panicked", "internal-error", "io-error"}:
        return VMStatus.ERROR
    return VMStatus.UNKNOWN
