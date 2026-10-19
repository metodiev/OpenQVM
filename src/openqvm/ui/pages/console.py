"""Console window: embedded VNC display, serial output and QEMU log."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QImage
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from openqvm.app.context import AppContext
from openqvm.core.errors import OpenQVMError
from openqvm.core.events import TOPIC_VM_DELETED, TOPIC_VM_STATE_CHANGED, TOPIC_VM_UPDATED, VmEvent, VmStateEvent
from openqvm.core.logging import get_logger
from openqvm.core.models import VMStatus
from openqvm.ui.vnc import VncWidget
from openqvm.ui.widgets.common import LogTail, open_folder

log = get_logger("ui.console")


class ConsoleWindow(QMainWindow):
    closed = Signal(str)
    vm_event = Signal(object)

    def __init__(self, context: AppContext, vm_id: str, parent=None) -> None:
        super().__init__(parent)
        self.context = context
        self.vm_id = vm_id
        self._vnc: VncWidget | None = None
        self._unsubscribers: list = []
        self._qemu_tail = LogTail(context.manager.qemu_log_path(vm_id))
        self._serial_tail = LogTail(context.manager.serial_log_path(vm_id))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setWindowTitle("OpenQVM console")
        self.resize(1000, 720)
        try:
            self.summary = context.manager.get_summary(vm_id)
        except OpenQVMError as exc:
            QMessageBox.critical(self, "Console unavailable", exc.message)
            raise
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(1200)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        self.vm_event.connect(self._on_vm_event, Qt.ConnectionType.QueuedConnection)
        for topic in (TOPIC_VM_STATE_CHANGED, TOPIC_VM_UPDATED, TOPIC_VM_DELETED):
            self._unsubscribers.append(
                context.manager.events.subscribe(topic, self._forward_vm_event)
            )
        if self.summary.runtime.status in (VMStatus.RUNNING, VMStatus.PAUSED):
            self._connect_display()
        self._refresh_status()

    def _forward_vm_event(self, payload) -> None:  # type: ignore[no-untyped-def]
        """Runs on the publishing thread; only emit a queued signal."""

        if getattr(payload, "vm_id", None) == self.vm_id:
            self.vm_event.emit(payload)

    def _on_vm_event(self, payload) -> None:  # type: ignore[no-untyped-def]
        if isinstance(payload, VmEvent):
            self.close()
            return
        if isinstance(payload, VmStateEvent):
            self._refresh_status()
            if payload.current not in (VMStatus.RUNNING, VMStatus.PAUSED) and self._vnc is not None:
                self._dispose_vnc()
                self.display_placeholder.setText("The virtual machine is not running.")
                self.display_placeholder.setVisible(True)

    def _build(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        self.name_label = QLabel(self.summary.spec.name)
        self.name_label.setProperty("role", "h2")
        toolbar.addWidget(self.name_label)
        self.status_label = QLabel()
        self.status_label.setProperty("role", "muted")
        toolbar.addWidget(self.status_label)
        toolbar.addStretch(1)
        self.reconnect_button = QPushButton("Reconnect display")
        self.reconnect_button.clicked.connect(self._connect_display)
        self.fit_button = QPushButton("Fit to window")
        self.fit_button.setCheckable(True)
        self.fit_button.setChecked(self.context.settings.console_fit)
        self.fit_button.toggled.connect(self._set_fit)
        self.cad_button = QPushButton("Ctrl+Alt+Del")
        self.cad_button.setToolTip("Send Ctrl+Alt+Del to the guest (Windows login and lock screen)")
        self.cad_button.clicked.connect(self._send_ctrl_alt_del)
        self.paste_button = QPushButton("Type text…")
        self.paste_button.setToolTip("Type clipboard text into the guest using a US keyboard layout")
        self.paste_button.clicked.connect(self._type_text)
        self.screenshot_button = QPushButton("Save screenshot…")
        self.screenshot_button.clicked.connect(self._save_screenshot)
        folder_button = QPushButton("Open VM folder")
        folder_button.clicked.connect(lambda: open_folder(self.context.paths.vm_dir(self.vm_id)))
        for button in (
            self.reconnect_button,
            self.fit_button,
            self.cad_button,
            self.paste_button,
            self.screenshot_button,
            folder_button,
        ):
            toolbar.addWidget(button)
        layout.addLayout(toolbar)

        self.tabs = QTabWidget()
        self.display_container = QWidget()
        display_layout = QVBoxLayout(self.display_container)
        display_layout.setContentsMargins(0, 0, 0, 0)
        self.display_placeholder = QLabel(
            "The display is not connected. Start the VM and use Reconnect display."
        )
        self.display_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.display_placeholder.setProperty("role", "muted")
        display_layout.addWidget(self.display_placeholder)
        self.tabs.addTab(self.display_container, "Display")

        self.serial_view = QPlainTextEdit()
        self.serial_view.setReadOnly(True)
        self.serial_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.serial_view.setPlaceholderText("Guest serial console output appears here.")
        self.tabs.addTab(self.serial_view, "Serial console")

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.tabs.addTab(self.log_view, "QEMU log")
        self.tabs.currentChanged.connect(lambda _: self._poll())
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage(
            "Closing this window leaves the virtual machine running. Guest display is loopback-only VNC."
        )

    # -- display --------------------------------------------------------------

    def _connect_display(self) -> None:
        endpoint = self.context.manager.vnc_endpoint(self.vm_id)
        if endpoint is None:
            self.display_placeholder.setText(
                "No VNC endpoint is available. The VM may be stopped or the display may be disabled."
            )
            self.display_placeholder.setVisible(True)
            if self._vnc is not None:
                self._vnc.hide()
            return
        self._dispose_vnc()
        host, port = endpoint
        self._vnc = VncWidget(host, port, self.display_container)
        self._vnc.set_fit(self.fit_button.isChecked())
        self._vnc.status_changed.connect(self._display_status)
        self.display_container.layout().addWidget(self._vnc)
        self.display_placeholder.setVisible(False)
        self._vnc.show()
        self._vnc.setFocus()
        self._vnc.start()

    def _dispose_vnc(self) -> None:
        if self._vnc is not None:
            self._vnc.stop()
            self._vnc.deleteLater()
            self._vnc = None

    def _display_status(self, message: str) -> None:
        self.statusBar().showMessage(message, 4000)

    def _set_fit(self, fit: bool) -> None:
        if self._vnc is not None:
            self._vnc.set_fit(fit)
        self.context.update_settings(console_fit=fit)

    def _send_ctrl_alt_del(self) -> None:
        if self._vnc is None or not self._vnc.send_ctrl_alt_del():
            self.statusBar().showMessage("Connect the display before sending Ctrl+Alt+Del.", 4000)
            return
        self.statusBar().showMessage("Sent Ctrl+Alt+Del to the guest.", 3000)

    def _type_text(self) -> None:
        if self._vnc is None:
            self.statusBar().showMessage("Connect the display before typing text.", 4000)
            return
        text, ok = QInputDialog.getMultiLineText(
            self,
            "Type text into the guest",
            "Text is typed as key events using a US keyboard layout:",
        )
        if not ok or not text:
            return
        typed, unsupported = self._vnc.send_text(text)
        message = f"Typed {typed} character(s)."
        if unsupported:
            message += f" {unsupported} character(s) are not supported by the US layout map."
        self.statusBar().showMessage(message, 6000)

    def _save_screenshot(self) -> None:
        image: QImage | None = self._vnc.screenshot() if self._vnc else None
        if image is None:
            QMessageBox.information(self, "No frame", "The guest display has not produced a frame yet.")
            return
        default = str(self.context.paths.exports_dir / f"{self.summary.spec.name}-console.png")
        path, _ = QFileDialog.getSaveFileName(self, "Save console screenshot", default, "PNG image (*.png)")
        if not path:
            return
        if image.save(path, "PNG"):
            QMessageBox.information(self, "Screenshot saved", path)
        else:
            QMessageBox.warning(self, "Screenshot failed", "The image could not be saved.")

    # -- logs and status ------------------------------------------------------

    def _poll(self) -> None:
        qemu_chunk = self._qemu_tail.read()
        if qemu_chunk and self.tabs.currentIndex() == 2:
            self.log_view.appendPlainText(qemu_chunk.rstrip("\n"))
        serial_chunk = self._serial_tail.read()
        if serial_chunk and self.tabs.currentIndex() == 1:
            self.serial_view.appendPlainText(serial_chunk.rstrip("\n"))
        self._refresh_status()

    def _refresh_status(self) -> None:
        try:
            summary = self.context.manager.get_summary(self.vm_id, refresh=True)
        except OpenQVMError:
            return
        self.summary = summary
        self.setWindowTitle(f"OpenQVM console — {summary.spec.name}")
        self.name_label.setText(summary.spec.name)
        self.status_label.setText(
            f"{summary.runtime.status.value} · accelerator {summary.runtime.accelerator or '—'}"
        )
        if summary.runtime.status not in (VMStatus.RUNNING, VMStatus.PAUSED):
            if self._vnc is not None:
                self._dispose_vnc()
                self.display_placeholder.setText("The virtual machine is not running.")
                self.display_placeholder.setVisible(True)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._timer.stop()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        self._dispose_vnc()
        self.closed.emit(self.vm_id)
        super().closeEvent(event)
