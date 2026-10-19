"""End-to-end boot test against a real QEMU installation.

The test is skipped when QEMU or an Alpine ISO is not available.  It exercises
the same manager, backend, QMP control and RFB console code used by the GUI.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from openqvm.app.context import AppContext
from openqvm.core.factory import create_vm_spec
from openqvm.core.models import GuestOS, VMStatus
from openqvm.core.validation import HostResources
from openqvm.vm.rfb import RfbClient


def _find_iso() -> Path | None:
    override = os.environ.get("OPENQVM_TEST_ISO")
    if override and Path(override).is_file():
        return Path(override)
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.extend(sorted((Path(local) / "OpenQVM" / "iso").glob("alpine-virt-*.iso")))
    candidates.extend(sorted(Path("/usr/share/openqvm/iso").glob("alpine-virt-*.iso")))
    candidates.extend(sorted(Path.home().glob("*.iso")))
    return candidates[-1] if candidates else None


def _wait_for_login(context: AppContext, vm_id: str, timeout: float = 120.0) -> str:
    serial = context.manager.serial_log_path(vm_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = serial.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if "login:" in text:
            return text
        time.sleep(0.5)
    raise AssertionError(f"Guest did not reach a login prompt. Serial log:\n{text[-2000:]}")


@pytest.mark.e2e
@pytest.mark.timeout(360)
def test_real_qemu_boot_snapshot_export(tmp_path):
    iso = _find_iso()
    if iso is None:
        pytest.skip("No Alpine ISO available; set OPENQVM_TEST_ISO to run the e2e test.")
    context = AppContext.create(data_dir=tmp_path / "data")
    if not context.capabilities.backend_available:
        pytest.skip("QEMU is not installed on this host.")

    host = HostResources.detect()
    spec = create_vm_spec(
        name="E2E Alpine",
        guest_os=GuestOS.LINUX,
        host=host,
        vcpus=2,
        memory_mib=512,
        disk_bytes=1 * 1024**3,
        cdrom_iso=str(iso),
    )
    manager = context.manager
    vm_id = manager.create_vm(spec).id
    running = False
    try:
        runtime = manager.start_vm(vm_id)
        running = True
        assert runtime.status is VMStatus.RUNNING
        assert runtime.accelerator
        serial_text = _wait_for_login(context, vm_id)
        assert "Alpine Linux" in serial_text

        endpoint = manager.vnc_endpoint(vm_id)
        assert endpoint is not None
        client = RfbClient(*endpoint, timeout=30)
        try:
            client.connect()
            frame = client.grab_frame()
            assert frame.width >= 640 and frame.height >= 400
            non_black = sum(1 for index in range(0, len(frame.rgb), 3) if frame.rgb[index : index + 3] != b"\x00\x00\x00")
            assert non_black > (frame.width * frame.height) // 100
        finally:
            client.close()

        assert manager.pause_vm(vm_id).status is VMStatus.PAUSED
        assert manager.resume_vm(vm_id).status is VMStatus.RUNNING

        snapshot = manager.create_snapshot(vm_id, "e2e-snap", "created by e2e test")
        assert snapshot.name == "e2e-snap"
        assert any(view.name == "e2e-snap" for view in manager.list_snapshots(vm_id))

        assert manager.stop_vm(vm_id, force=True).status is VMStatus.STOPPED
        running = False
        manager.restore_snapshot(vm_id, "e2e-snap")
        manager.start_vm(vm_id)
        running = True
        _wait_for_login(context, vm_id, timeout=120)
        manager.stop_vm(vm_id, force=True)
        running = False

        export_path = tmp_path / "e2e-export.oqv"
        exported = manager.export_vm(vm_id, export_path)
        assert exported.path.is_file() and exported.size_bytes > 0
        imported = manager.import_vm(export_path)
        assert imported.spec.id != vm_id
        assert manager.store.disk_path(imported.spec, imported.spec.disks[0]).is_file()

        clone = manager.clone_vm(vm_id, "E2E Clone")
        assert clone.spec.id != vm_id
        assert manager.store.disk_path(clone.spec, clone.spec.disks[0]).is_file()

        manager.delete_vm(imported.spec.id)
        manager.delete_vm(clone.spec.id)
        manager.delete_vm(vm_id)
    finally:
        if running:
            try:
                manager.stop_vm(vm_id, force=True, timeout=10)
            except Exception:
                pass
        context.close()
