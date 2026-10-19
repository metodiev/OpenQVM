"""Main application window and navigation."""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from openqvm import PRODUCT_NAME, PRODUCT_VERSION
from openqvm.app.context import AppContext
from openqvm.core.cancellation import CancellationToken
from openqvm.core.errors import ErrorCode, OpenQVMError
from openqvm.core.events import (
    ALL_TOPICS,
    TOPIC_OPERATION_PROGRESS,
    TOPIC_SNAPSHOTS_CHANGED,
    OperationProgress,
)
from openqvm.core.logging import get_logger
from openqvm.core.models import VMStatus
from openqvm.storage.transfer import suggested_export_name
from openqvm.ui.async_ops import AsyncRunner
from openqvm.ui.icons import app_icon
from openqvm.ui.pages.console import ConsoleWindow
from openqvm.ui.pages.dashboard import DashboardPage
from openqvm.ui.pages.details import VmDetailsPage
from openqvm.ui.pages.settings import SettingsPage
from openqvm.ui.pages.vm_list import VmListPage
from openqvm.ui.pages.wizard import CreateVmWizard
from openqvm.ui.theme import apply_theme
from openqvm.ui.widgets.common import confirm, show_error

log = get_logger("ui.main")

PAGE_DASHBOARD = 0
PAGE_VMS = 1
PAGE_DETAILS = 2
PAGE_SETTINGS = 3


class MainWindow(QMainWindow):
    event_received = Signal(str, object)

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
        self.runner = AsyncRunner(self)
        self.summaries: list = []
        self.errors: list[str] = []
        self._refreshing = False
        self._refresh_pending = False
        self._consoles: dict[str, ConsoleWindow] = {}
        self._wizard: CreateVmWizard | None = None
        self._unsubscribers: list = []
        self._settings_store = QSettings("OpenQVM", "OpenQVM")
        self.setWindowTitle(f"{PRODUCT_NAME} {PRODUCT_VERSION}")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(1120, 720)
        self.resize(1280, 820)
        self._build()
        self._connect()
        self._restore_state()
        self.refresh_all()
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.refresh_all)
        self.timer.start()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(120)
        self._refresh_timer.timeout.connect(self._apply_pending_refresh)
        self._subscribe_events()

    def _subscribe_events(self) -> None:
        """Forward manager events onto the GUI thread.

        Manager operations run on worker threads, so publisher callbacks must not
        touch widgets.  Emitting a Qt signal delivers them to the GUI thread.
        """

        self.event_received.connect(self._on_event, Qt.ConnectionType.QueuedConnection)
        for topic in ALL_TOPICS:
            self._unsubscribers.append(
                self.context.manager.events.subscribe(
                    topic, functools.partial(self.event_received.emit, topic)
                )
            )

    def _on_event(self, topic: str, payload) -> None:  # type: ignore[no-untyped-def]
        if topic == TOPIC_OPERATION_PROGRESS:
            self._show_progress(payload)
            return
        self._refresh_pending = True
        self._refresh_timer.start()
        if topic == TOPIC_SNAPSHOTS_CHANGED and self.stack.currentIndex() == PAGE_DETAILS:
            if getattr(payload, "vm_id", None) == self.details.vm_id:
                self.details.reload_snapshots()

    def _show_progress(self, event: OperationProgress) -> None:
        """Show a determinate bar when the operation reports a fraction."""

        if self.runner.active_count == 0:
            return
        self.status_message.setText(event.describe())
        if event.percent is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(event.percent)

    def _apply_pending_refresh(self) -> None:
        if not self._refresh_pending:
            return
        self._refresh_pending = False
        if not self._refreshing:
            self.refresh_all()

    # -- construction ---------------------------------------------------------

    def _build(self) -> None:
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(236)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(16, 22, 16, 16)
        sidebar_layout.setSpacing(8)
        title = QLabel(PRODUCT_NAME)
        title.setProperty("role", "h2")
        tagline = QLabel("Desktop virtualization")
        tagline.setProperty("role", "muted")
        sidebar_layout.addWidget(title)
        sidebar_layout.addWidget(tagline)
        sidebar_layout.addSpacing(18)

        self.nav_dashboard = self._nav_button("Dashboard")
        self.nav_vms = self._nav_button("Virtual machines")
        self.nav_settings = self._nav_button("Settings")
        self.nav_dashboard.clicked.connect(self.show_dashboard)
        self.nav_vms.clicked.connect(self.show_vm_list)
        self.nav_settings.clicked.connect(self.show_settings)
        sidebar_layout.addWidget(self.nav_dashboard)
        sidebar_layout.addWidget(self.nav_vms)
        sidebar_layout.addWidget(self.nav_settings)
        sidebar_layout.addStretch(1)

        self.backend_chip = QLabel("Backend: detecting…")
        self.backend_chip.setWordWrap(True)
        self.backend_chip.setProperty("role", "muted")
        sidebar_layout.addWidget(self.backend_chip)
        self.theme_button = QPushButton()
        self.theme_button.setProperty("variant", "ghost")
        self.theme_button.clicked.connect(self._toggle_theme)
        sidebar_layout.addWidget(self.theme_button)
        version = QLabel(f"v{PRODUCT_VERSION}")
        version.setProperty("role", "muted")
        sidebar_layout.addWidget(version)
        layout.addWidget(sidebar)

        self.stack = QStackedWidget()
        self.dashboard = DashboardPage(self.context)
        self.vm_list = VmListPage(self.context)
        self.details = VmDetailsPage(self.context, self.runner)
        self.settings_page = SettingsPage(self.context)
        for page in (self.dashboard, self.vm_list, self.details, self.settings_page):
            self.stack.addWidget(page)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        status = QStatusBar()
        self.status_message = QLabel("Ready")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(120)
        self.progress.setVisible(False)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip("Ask the running operation to stop")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.cancel_operation)
        status.addWidget(self.status_message, 1)
        status.addPermanentWidget(self.cancel_button)
        status.addPermanentWidget(self.progress)
        self.setStatusBar(status)

    def _nav_button(self, text: str) -> QPushButton:
        button = QPushButton(text)
        button.setProperty("variant", "nav")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAccessibleName(text)
        return button

    def _connect(self) -> None:
        self.dashboard.create_requested.connect(self.create_vm)
        self.dashboard.show_vms_requested.connect(self.show_vm_list)
        self.dashboard.open_console_requested.connect(self.open_console)
        self.dashboard.open_vm_requested.connect(self.open_details)
        self.dashboard.settings_requested.connect(self.show_settings)

        self.vm_list.create_requested.connect(self.create_vm)
        self.vm_list.import_requested.connect(self.import_vm)
        self.vm_list.details_requested.connect(self.open_details)
        self.vm_list.console_requested.connect(self.open_console)
        self.vm_list.start_requested.connect(self.start_vm)
        self.vm_list.pause_requested.connect(self.pause_vm)
        self.vm_list.resume_requested.connect(self.resume_vm)
        self.vm_list.stop_requested.connect(self.stop_vm)
        self.vm_list.force_stop_requested.connect(self.force_stop_vm)
        self.vm_list.delete_requested.connect(self.delete_vm)
        self.vm_list.clone_requested.connect(self.clone_vm)
        self.vm_list.export_requested.connect(self.export_vm)
        self.vm_list.refresh_requested.connect(self.refresh_all)

        self.details.back_requested.connect(self.show_vm_list)
        self.details.console_requested.connect(self.open_console)
        self.details.changed.connect(self.refresh_all)
        self.details.deleted.connect(self._on_vm_deleted)

        self.settings_page.theme_changed.connect(self.set_theme)
        self.settings_page.capabilities_changed.connect(self._backend_changed)
        self.runner.busy_changed.connect(self._on_busy)

        QShortcut(QKeySequence.StandardKey.New, self, activated=self.create_vm)
        QShortcut(QKeySequence("F5"), self, activated=self.refresh_all)
        QShortcut(QKeySequence("Ctrl+1"), self, activated=self.show_dashboard)
        QShortcut(QKeySequence("Ctrl+2"), self, activated=self.show_vm_list)
        QShortcut(QKeySequence("Ctrl+3"), self, activated=self.show_settings)
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close)
        QShortcut(QKeySequence("Alt+Left"), self, activated=self.show_vm_list)
        self.theme_button.setText(self._theme_button_text())

    def subscribe_to_events(self, callback) -> None:  # type: ignore[no-untyped-def]
        """Register an additional GUI-thread callback for manager events.

        Used by tests and future pages; the callback receives ``(topic, payload)``.
        """

        self.event_received.connect(callback, Qt.ConnectionType.QueuedConnection)

    def _restore_state(self) -> None:
        geometry = self._settings_store.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        page = self.context.settings.last_page
        if page == "vms":
            self.show_vm_list()
        elif page == "settings":
            self.show_settings()
        else:
            self.show_dashboard()

    # -- navigation -----------------------------------------------------------

    def _set_page(self, index: int, active: QPushButton) -> None:
        self.stack.setCurrentIndex(index)
        for button in (self.nav_dashboard, self.nav_vms, self.nav_settings):
            button.setProperty("active", button is active)
            button.style().unpolish(button)
            button.style().polish(button)

    def show_dashboard(self) -> None:
        self._set_page(PAGE_DASHBOARD, self.nav_dashboard)
        self.context.update_settings(last_page="dashboard")
        self.refresh_all()

    def show_vm_list(self) -> None:
        self._set_page(PAGE_VMS, self.nav_vms)
        self.context.update_settings(last_page="vms")
        self.refresh_all()

    def show_settings(self) -> None:
        self._set_page(PAGE_SETTINGS, self.nav_settings)
        self.context.update_settings(last_page="settings")
        self.settings_page.refresh()

    def open_details(self, vm_id: str) -> None:
        self._set_page(PAGE_DETAILS, self.nav_vms)
        self.details.set_vm(vm_id)
        self.vm_list.select_vm(vm_id)

    # -- refresh --------------------------------------------------------------

    def refresh_all(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            summaries, errors = self.context.manager.list_summaries(refresh=True)
        except OpenQVMError as exc:
            show_error(self, exc)
            return
        finally:
            self._refreshing = False
        self.summaries = summaries
        self.errors = errors
        self.dashboard.refresh(summaries, errors)
        self.vm_list.refresh(summaries, errors)
        if self.stack.currentIndex() == PAGE_DETAILS:
            self.details.refresh_runtime()
        self._update_backend_chip()

    def _update_backend_chip(self) -> None:
        capabilities = self.context.capabilities
        if capabilities.qemu:
            self.backend_chip.setText(
                f"QEMU {capabilities.qemu.version}\n{capabilities.default_accelerator or 'no accelerator'} · "
                f"{capabilities.os_family}"
            )
            self.backend_chip.setToolTip("\n".join(capabilities.diagnostics))
        else:
            self.backend_chip.setText("QEMU not found")
            self.backend_chip.setToolTip("\n".join(capabilities.diagnostics))

    def _backend_changed(self) -> None:
        self.refresh_all()
        self._update_backend_chip()

    def _on_busy(self, busy: bool, description: str) -> None:
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        self.progress.setVisible(busy)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setVisible(busy)
        self.status_message.setText(description if busy else "Ready")

    def cancel_operation(self) -> None:
        """Request a cooperative stop of every running background operation."""

        requested = self.runner.cancel_active()
        if requested:
            self.status_message.setText("Cancelling…")
            self.cancel_button.setEnabled(False)

    # -- VM operations --------------------------------------------------------

    def _summary(self, vm_id: str):
        return next((summary for summary in self.summaries if summary.spec.id == vm_id), None)

    def _run(
        self,
        description: str,
        function: Callable[[CancellationToken], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[OpenQVMError], None] | None = None,
    ) -> CancellationToken:
        return self.runner.submit(
            description,
            function,
            on_success=on_success,
            on_error=on_error or (lambda error: self._report_error(error)),
        )

    def _report_error(self, error: OpenQVMError) -> None:
        if error.code == ErrorCode.CANCELLED:
            self.status_message.setText(error.message)
            return
        show_error(self, error)

    def _after_change(self, message: str = "") -> None:
        self.refresh_all()
        if message:
            self.status_message.setText(message)

    def create_vm(self) -> None:
        wizard = CreateVmWizard(self.context, self)
        self._wizard = wizard
        self._creation_token: CancellationToken | None = None

        def submitted(spec) -> None:  # type: ignore[no-untyped-def]
            def success(created) -> None:  # type: ignore[no-untyped-def]
                self._creation_token = None
                wizard.creation_succeeded()
                self.refresh_all()
                self.open_details(created.id)

            def failure(error: OpenQVMError) -> None:
                self._creation_token = None
                wizard.creation_failed(error)

            self._creation_token = self._run(
                "Creating virtual machine",
                lambda token: self.context.manager.create_vm(spec, cancel=token),
                on_success=success,
                on_error=failure,
            )

        def cancelled() -> None:
            token = self._creation_token
            if token is None or token.is_cancelled:
                return
            token.cancel("Creation was cancelled by the user.")
            wizard.creation_cancelling()

        wizard.create_requested.connect(submitted)
        wizard.cancel_requested.connect(cancelled)
        wizard.finished.connect(lambda _: setattr(self, "_wizard", None))
        wizard.exec()

    def start_vm(self, vm_id: str) -> None:
        def success(_runtime) -> None:  # type: ignore[no-untyped-def]
            self.refresh_all()
            self.open_console(vm_id)

        self._run("Starting virtual machine", lambda _token: self.context.manager.start_vm(vm_id), on_success=success)

    def pause_vm(self, vm_id: str) -> None:
        self._run(
            "Pausing virtual machine",
            lambda _token: self.context.manager.pause_vm(vm_id),
            on_success=lambda _: self._after_change(),
        )

    def resume_vm(self, vm_id: str) -> None:
        self._run(
            "Resuming virtual machine",
            lambda _token: self.context.manager.resume_vm(vm_id),
            on_success=lambda _: self._after_change(),
        )

    def stop_vm(self, vm_id: str) -> None:
        self._run(
            "Shutting down virtual machine",
            lambda _token: self.context.manager.stop_vm(vm_id, force=False, timeout=30),
            on_success=lambda _: self._after_change("Virtual machine stopped."),
        )

    def force_stop_vm(self, vm_id: str) -> None:
        summary = self._summary(vm_id)
        name = summary.spec.name if summary else vm_id
        if not confirm(
            self,
            "Force stop",
            f"Force stop '{name}'?",
            informative="QEMU will be terminated immediately. Unsaved guest data may be lost.",
            confirm_text="Force stop",
            dangerous=True,
        ):
            return
        self._run(
            "Force stopping virtual machine",
            lambda _token: self.context.manager.stop_vm(vm_id, force=True),
            on_success=lambda _: self._after_change("Virtual machine force-stopped."),
        )

    def delete_vm(self, vm_id: str) -> None:
        summary = self._summary(vm_id)
        name = summary.spec.name if summary else vm_id
        if not confirm(
            self,
            "Delete virtual machine",
            f"Move '{name}' and all of its disks to the trash?",
            informative="The VM disappears from OpenQVM. Data remains in the trash until it is purged.",
            confirm_text="Delete VM",
            dangerous=True,
        ):
            return
        self._run(
            "Deleting virtual machine",
            lambda _token: self.context.manager.delete_vm(vm_id),
            on_success=lambda _: self._after_change("Virtual machine moved to the trash."),
        )

    def clone_vm(self, vm_id: str) -> None:
        summary = self._summary(vm_id)
        base = summary.spec.name if summary else "VM"
        name, ok = QInputDialog.getText(self, "Clone virtual machine", "Name for the clone:", text=f"{base} Clone")
        if not ok or not name.strip():
            return
        self._run(
            "Cloning virtual machine",
            lambda token: self.context.manager.clone_vm(vm_id, name.strip(), cancel=token),
            on_success=lambda result: self._clone_done(result),
        )

    def _clone_done(self, result) -> None:  # type: ignore[no-untyped-def]
        self.refresh_all()
        self.vm_list.select_vm(result.spec.id)
        self.status_message.setText(f"Cloned as '{result.spec.name}'.")

    def export_vm(self, vm_id: str) -> None:
        summary = self._summary(vm_id)
        if summary is None:
            return
        suggested = self.context.paths.exports_dir / suggested_export_name(summary.spec)
        path, _ = QFileDialog.getSaveFileName(self, "Export virtual machine", str(suggested), "OpenQVM package (*.oqv)")
        if not path:
            return
        overwrite = Path(path).exists()
        if overwrite and not confirm(self, "Replace package", f"{path} already exists. Replace it?", confirm_text="Replace"):
            return
        self._run(
            "Exporting virtual machine",
            lambda token: self.context.manager.export_vm(vm_id, Path(path), overwrite=overwrite, cancel=token),
            on_success=self._export_done,
        )

    def _export_done(self, result) -> None:  # type: ignore[no-untyped-def]
        self.refresh_all()
        QMessageBox.information(
            self,
            "Export complete",
            f"Package written to:\n{result.path}\n\nSHA-256:\n{result.sha256}",
        )

    def import_vm(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import virtual machine", str(self.context.paths.exports_dir), "OpenQVM package (*.oqv)"
        )
        if not path:
            return

        def success(result) -> None:  # type: ignore[no-untyped-def]
            self.refresh_all()
            self.open_details(result.spec.id)
            QMessageBox.information(
                self,
                "Import complete",
                f"Imported '{result.spec.name}'.\n" + ("\n".join(result.warnings) if result.warnings else ""),
            )

        self._run(
            "Importing virtual machine",
            lambda token: self.context.manager.import_vm(Path(path), cancel=token),
            on_success=success,
        )

    def open_console(self, vm_id: str) -> None:
        existing = self._consoles.get(vm_id)
        if existing is not None:
            existing.showNormal()
            existing.raise_()
            existing.activateWindow()
            return
        summary = self._summary(vm_id)
        status = summary.status if summary else None
        if status not in (VMStatus.RUNNING, VMStatus.PAUSED):
            QMessageBox.information(self, "Console unavailable", "Start the virtual machine to open its console.")
            return
        try:
            console = ConsoleWindow(self.context, vm_id, self)
        except OpenQVMError as exc:
            show_error(self, exc)
            return
        console.closed.connect(self._console_closed)
        console.setWindowIcon(app_icon())
        self._consoles[vm_id] = console
        console.show()
        console.raise_()

    def _console_closed(self, vm_id: str) -> None:
        console = self._consoles.pop(vm_id, None)
        if console is not None:
            console.deleteLater()

    def _on_vm_deleted(self, vm_id: str) -> None:
        console = self._consoles.pop(vm_id, None)
        if console is not None:
            console.close()
        self.show_vm_list()

    # -- theme ----------------------------------------------------------------

    def _theme_button_text(self) -> str:
        return "Switch to light theme" if self.context.settings.theme == "dark" else "Switch to dark theme"

    def _toggle_theme(self) -> None:
        self.set_theme("light" if self.context.settings.theme == "dark" else "dark")

    def set_theme(self, theme: str) -> None:
        if theme not in {"dark", "light"}:
            theme = "dark"
        apply_theme(QApplication.instance(), theme)
        self.context.update_settings(theme=theme)
        self.settings_page.theme_combo.blockSignals(True)
        index = self.settings_page.theme_combo.findData(theme)
        self.settings_page.theme_combo.setCurrentIndex(max(0, index))
        self.settings_page.theme_combo.blockSignals(False)
        self.theme_button.setText(self._theme_button_text())
        self.refresh_all()

    # -- window ---------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._settings_store.setValue("window/geometry", self.saveGeometry())
        timer = getattr(self, "timer", None)
        if timer is not None:
            timer.stop()
        refresh_timer = getattr(self, "_refresh_timer", None)
        if refresh_timer is not None:
            refresh_timer.stop()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        for console in list(getattr(self, "_consoles", {}).values()):
            console.close()
        self.runner.cancel_active("OpenQVM is closing")
        self.runner.wait_for_done(5000)
        super().closeEvent(event)
