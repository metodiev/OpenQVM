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

def _wait_until(qapp, predicate, timeout: float = 10.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.mark.gui
def test_async_runner_cancels_a_running_task(qapp):
    import threading

    from openqvm.core.errors import ErrorCode, OpenQVMError
    from openqvm.ui.async_ops import AsyncRunner

    runner = AsyncRunner()
    busy: list[tuple[bool, str]] = []
    runner.busy_changed.connect(lambda flag, text: busy.append((flag, text)))
    errors: list[OpenQVMError] = []
    started = threading.Event()

    def work(token):
        started.set()
        token.sleep(30)
        token.raise_if_cancelled()
        return "finished"

    runner.submit("Long task", work, on_error=errors.append)
    try:
        assert started.wait(5)
        assert runner.active_descriptions == ["Long task"]
        assert runner.cancel_active() == 1
        assert _wait_until(qapp, lambda: runner.active_count == 0)
        assert [error.code for error in errors] == [ErrorCode.CANCELLED]
        assert busy[-1] == (False, "")
        assert runner.cancel_active() == 0
    finally:
        runner.wait_for_done(5000)


@pytest.mark.gui
def test_main_window_cancel_button_stops_the_operation(tmp_path, qapp):
    import threading

    from openqvm.ui.main_window import MainWindow

    context = AppContext.create(data_dir=tmp_path / "data")
    window = MainWindow(context)
    started = threading.Event()

    def work(token):
        started.set()
        token.sleep(30)
        token.raise_if_cancelled()
        return "done"

    try:
        window._run("Long task", work)
        assert started.wait(5)
        assert _wait_until(qapp, lambda: not window.cancel_button.isHidden())
        assert window.status_message.text() == "Long task"

        window.cancel_button.click()
        assert window.status_message.text() == "Cancelling…"

        assert _wait_until(qapp, lambda: window.runner.active_count == 0)
        assert _wait_until(qapp, lambda: window.status_message.text() == "The operation was cancelled.")
        assert window.cancel_button.isHidden()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
        context.close()

@pytest.mark.gui
def test_wizard_cancel_stops_the_creation_instead_of_closing(tmp_path, qapp):
    from openqvm.core.errors import CancelledError
    from openqvm.ui.pages.wizard import CreateVmWizard

    context = AppContext.create(data_dir=tmp_path / "data")
    wizard = CreateVmWizard(context)
    try:
        wizard.page(0).name_edit.setText("Cancel Test VM")
        events: list[str] = []
        wizard.create_requested.connect(lambda _spec: events.append("create"))
        wizard.cancel_requested.connect(lambda: events.append("cancel"))

        wizard.accept()
        assert events == ["create"]
        assert wizard._submitted

        wizard.reject()
        assert events == ["create", "cancel"]
        assert wizard._submitted, "the dialog stays open until the cancellation is confirmed"

        wizard.creation_failed(CancelledError("The operation was cancelled."))
        assert not wizard._submitted
        wizard.reject()
    finally:
        wizard.close()
        wizard.deleteLater()
        qapp.processEvents()
        context.close()
