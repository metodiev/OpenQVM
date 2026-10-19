"""GUI construction smoke tests (Qt offscreen platform)."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from openqvm.app.context import AppContext
from openqvm.core.models import NetworkMode, NicModel, NicSpec, PortForward, generate_mac
from openqvm.ui.pages.wizard import CreateVmWizard
from openqvm.ui.theme import apply_theme


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    for widget in list(app.topLevelWidgets()):
        widget.close()
        widget.deleteLater()
    app.processEvents()
    app.closeAllWindows()
    app.quit()
    app.processEvents()


@pytest.mark.gui
def test_main_window_and_wizard_construct(tmp_path, qapp):
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    context = AppContext.create(data_dir=tmp_path / "data")
    apply_theme(qapp, context.settings.theme)
    from openqvm.ui.main_window import MainWindow

    window = MainWindow(context)
    try:
        assert window.stack.count() == 4
        wizard = CreateVmWizard(context, window)
        try:
            name_page = wizard.page(0)
            name_page.name_edit.setText("Smoke Test VM")
            spec = wizard.build_spec()
            assert spec.name == "Smoke Test VM"
            assert spec.disks
            issues = context.manager.validate_spec(spec)
            assert not [issue for issue in issues if issue.is_error]
        finally:
            wizard.close()
            wizard.deleteLater()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
        context.close()


@pytest.mark.gui
def test_main_window_forwards_manager_events(tmp_path, qapp):
    from openqvm.core.events import TOPIC_VM_CREATED, VmEvent
    from openqvm.ui.main_window import MainWindow

    context = AppContext.create(data_dir=tmp_path / "data")
    window = MainWindow(context)
    try:
        received: list[tuple[str, object]] = []
        window.subscribe_to_events(lambda topic, payload: received.append((topic, payload)))
        event = VmEvent(vm_id="6f0c6b5a-5e80-4f88-9c0b-2a1b8f1e5c11", name="event-vm")
        context.manager.events.publish(TOPIC_VM_CREATED, event)
        for _ in range(5):
            qapp.processEvents()
        assert received == [(TOPIC_VM_CREATED, event)]
        assert window._refresh_pending or window._refresh_timer.isActive()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
        context.close()


@pytest.mark.gui
def test_nic_dialog_roundtrips_port_forwards(tmp_path, qapp):
    from openqvm.ui.pages.details import NicDialog

    context = AppContext.create(data_dir=tmp_path / "data")
    try:
        nic = NicSpec(
            id="6f0c6b5a-5e80-4f88-9c0b-2a1b8f1e5c11",
            mode=NetworkMode.NAT,
            model=NicModel.VIRTIO,
            mac=generate_mac(),
            port_forwards=(PortForward(host_port=2222, guest_port=22),),
        )
        dialog = NicDialog(context, nic=nic)
        try:
            result = dialog.nic()
            assert result.port_forwards == nic.port_forwards
            assert result.mac == nic.mac
            assert result.mode is NetworkMode.NAT
        finally:
            dialog.close()
            dialog.deleteLater()
            qapp.processEvents()
    finally:
        context.close()
