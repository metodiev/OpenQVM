"""Virtual machine inventory page."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from openqvm.app.context import AppContext
from openqvm.core.models import VMStatus
from openqvm.services.metrics import human_bytes
from openqvm.ui.theme import colours
from openqvm.ui.widgets.common import Banner, PageHeader, StatusDelegate
from openqvm.vm.manager import VmSummary


def _text_item(text: str, colour) -> QTableWidgetItem:  # type: ignore[no-untyped-def]
    item = QTableWidgetItem(text)
    item.setForeground(colour)
    return item


class VmListPage(QWidget):
    create_requested = Signal()
    import_requested = Signal()
    details_requested = Signal(str)
    console_requested = Signal(str)
    start_requested = Signal(str)
    pause_requested = Signal(str)
    resume_requested = Signal(str)
    stop_requested = Signal(str)
    force_stop_requested = Signal(str)
    delete_requested = Signal(str)
    export_requested = Signal(str)
    clone_requested = Signal(str)
    refresh_requested = Signal()

    def __init__(self, context: AppContext, parent=None) -> None:
        super().__init__(parent)
        self.context = context
        self._summaries: list[VmSummary] = []
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 24)
        layout.setSpacing(14)

        header = PageHeader("Virtual machines", "Create, start, inspect and organize your guests.")
        new_button = QPushButton("New virtual machine")
        new_button.setProperty("variant", "primary")
        new_button.clicked.connect(self.create_requested)
        header.add_action(new_button)
        import_button = QPushButton("Import…")
        import_button.clicked.connect(self.import_requested)
        header.add_action(import_button)
        layout.addWidget(header)

        self.banner = Banner()
        layout.addWidget(self.banner)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by name…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _: self._rebuild_table())
        self.search.setMaximumWidth(320)
        toolbar.addWidget(self.search)
        self.filter = QComboBox()
        self.filter.addItem("All statuses", "all")
        self.filter.addItem("Running", "running")
        self.filter.addItem("Paused", "paused")
        self.filter.addItem("Stopped", "stopped")
        self.filter.addItem("Error", "error")
        self.filter.currentIndexChanged.connect(lambda _: self._rebuild_table())
        toolbar.addWidget(self.filter)
        toolbar.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_requested)
        toolbar.addWidget(refresh)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Name", "Operating system", "Status", "vCPU", "Memory", "Disk", "Accelerator"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.setItemDelegateForColumn(2, StatusDelegate(self.table))
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        for column in range(3, 7):
            header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)

        self.empty = QLabel("No virtual machines yet.\nCreate one to install an operating system from an ISO image.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setProperty("role", "muted")
        self.empty.setVisible(False)
        layout.addWidget(self.empty, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.start_button = QPushButton("Start")
        self.start_button.setProperty("variant", "primary")
        self.start_button.clicked.connect(lambda: self._emit_selected(self.start_requested))
        self.pause_button = QPushButton("Pause")
        self.pause_button.clicked.connect(lambda: self._emit_selected(self.pause_requested))
        self.resume_button = QPushButton("Resume")
        self.resume_button.clicked.connect(lambda: self._emit_selected(self.resume_requested))
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(lambda: self._emit_selected(self.stop_requested))
        self.console_button = QPushButton("Open console")
        self.console_button.clicked.connect(lambda: self._emit_selected(self.console_requested))
        self.details_button = QPushButton("Details")
        self.details_button.clicked.connect(lambda: self._emit_selected(self.details_requested))
        self.delete_button = QPushButton("Delete")
        self.delete_button.setProperty("variant", "danger")
        self.delete_button.clicked.connect(lambda: self._emit_selected(self.delete_requested))
        for button in (
            self.start_button,
            self.pause_button,
            self.resume_button,
            self.stop_button,
            self.console_button,
            self.details_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(self.delete_button)
        layout.addLayout(actions)

    # -- data -----------------------------------------------------------------

    def refresh(self, summaries: list[VmSummary], errors: list[str]) -> None:
        self._summaries = summaries
        if errors:
            self.banner.show_message("; ".join(errors[:2]), severity="warning")
        elif not self.context.capabilities.backend_available:
            self.banner.show_message(
                "QEMU is not available, so virtual machines cannot be started.",
                severity="error",
            )
        else:
            self.banner.clear()
        self._rebuild_table()

    def _selected_id(self) -> str | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def selected_summary(self) -> VmSummary | None:
        vm_id = self._selected_id()
        return next((summary for summary in self._summaries if summary.spec.id == vm_id), None)

    def select_vm(self, vm_id: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == vm_id:
                self.table.selectRow(row)
                return

    def _rebuild_table(self) -> None:
        selected = self._selected_id()
        query = self.search.text().strip().lower()
        status_filter = self.filter.currentData()
        visible: list[VmSummary] = []
        for summary in self._summaries:
            if query and query not in summary.spec.name.lower():
                continue
            if status_filter != "all" and summary.status.value != status_filter:
                continue
            visible.append(summary)

        self.table.setUpdatesEnabled(False)
        self.table.clearSelection()
        self.table.setRowCount(0)
        self.table.setRowCount(len(visible))
        theme = colours(self.context.settings.theme)
        text_colour = QColor(theme.text)
        for row, summary in enumerate(visible):
            spec = summary.spec
            name_item = QTableWidgetItem(spec.name)
            name_item.setData(Qt.ItemDataRole.UserRole, spec.id)
            name_item.setForeground(text_colour)
            name_item.setToolTip(f"{spec.name}\nID: {spec.id}\nGuest: {spec.guest_os.display_name}")
            font = name_item.font()
            font.setBold(True)
            name_item.setFont(font)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, _text_item(spec.guest_os.display_name, text_colour))
            status_item = QTableWidgetItem("")
            status_item.setData(Qt.ItemDataRole.UserRole, summary.status.value)
            status_item.setToolTip(f"VM status: {summary.status.display_name}")
            self.table.setItem(row, 2, status_item)
            self.table.setItem(row, 3, _text_item(str(spec.cpu.vcpus), text_colour))
            self.table.setItem(row, 4, _text_item(f"{spec.memory_mib} MiB", text_colour))
            self.table.setItem(row, 5, _text_item(human_bytes(spec.total_disk_bytes), text_colour))
            accelerator = summary.runtime.accelerator or self.context.capabilities.default_accelerator or "—"
            self.table.setItem(row, 6, _text_item(accelerator, text_colour))
        self.table.setUpdatesEnabled(True)
        self.empty.setVisible(not visible)
        self.table.setVisible(bool(visible))
        if selected:
            self.select_vm(selected)
        self._update_actions()

    def _update_actions(self) -> None:
        summary = self.selected_summary()
        has_selection = summary is not None
        status = summary.status if summary else VMStatus.STOPPED
        self.start_button.setEnabled(has_selection and status in (VMStatus.STOPPED, VMStatus.ERROR, VMStatus.UNKNOWN))
        self.pause_button.setEnabled(has_selection and status is VMStatus.RUNNING)
        self.resume_button.setEnabled(has_selection and status is VMStatus.PAUSED)
        self.stop_button.setEnabled(has_selection and status.is_active)
        self.console_button.setEnabled(has_selection and status in (VMStatus.RUNNING, VMStatus.PAUSED))
        self.details_button.setEnabled(has_selection)
        self.delete_button.setEnabled(has_selection and not status.is_active)

    def _emit_selected(self, signal) -> None:  # type: ignore[no-untyped-def]
        vm_id = self._selected_id()
        if vm_id:
            signal.emit(vm_id)

    def _on_double_click(self, index) -> None:  # type: ignore[no-untyped-def]
        vm_id = self._selected_id()
        if not vm_id:
            return
        summary = self.selected_summary()
        if summary and summary.status in (VMStatus.RUNNING, VMStatus.PAUSED):
            self.console_requested.emit(vm_id)
        else:
            self.details_requested.emit(vm_id)

    def _show_context_menu(self, position) -> None:  # type: ignore[no-untyped-def]
        summary = self.selected_summary()
        if summary is None:
            return
        menu = QMenu(self)
        menu.addAction("Open console", lambda: self.console_requested.emit(summary.spec.id)).setEnabled(
            summary.status in (VMStatus.RUNNING, VMStatus.PAUSED)
        )
        menu.addAction("Details", lambda: self.details_requested.emit(summary.spec.id))
        menu.addSeparator()
        if summary.status is VMStatus.RUNNING:
            menu.addAction("Pause", lambda: self.pause_requested.emit(summary.spec.id))
            menu.addAction("Stop (ACPI)", lambda: self.stop_requested.emit(summary.spec.id))
            menu.addAction("Force stop", lambda: self.force_stop_requested.emit(summary.spec.id))
        elif summary.status is VMStatus.PAUSED:
            menu.addAction("Resume", lambda: self.resume_requested.emit(summary.spec.id))
            menu.addAction("Force stop", lambda: self.force_stop_requested.emit(summary.spec.id))
        elif summary.status in (VMStatus.STOPPED, VMStatus.ERROR, VMStatus.UNKNOWN):
            menu.addAction("Start", lambda: self.start_requested.emit(summary.spec.id))
        menu.addSeparator()
        menu.addAction("Clone…", lambda: self.clone_requested.emit(summary.spec.id)).setEnabled(
            not summary.status.is_active
        )
        menu.addAction("Export…", lambda: self.export_requested.emit(summary.spec.id)).setEnabled(
            not summary.status.is_active
        )
        menu.addAction("Delete…", lambda: self.delete_requested.emit(summary.spec.id)).setEnabled(
            not summary.status.is_active
        )
        menu.exec(self.table.viewport().mapToGlobal(position))

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def] # noqa: N802
        if event.key() == Qt.Key.Key_Delete:
            vm_id = self._selected_id()
            summary = self.selected_summary()
            if vm_id and summary and not summary.status.is_active:
                self.delete_requested.emit(vm_id)
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            vm_id = self._selected_id()
            if vm_id:
                summary = self.selected_summary()
                if summary and summary.status in (VMStatus.RUNNING, VMStatus.PAUSED):
                    self.console_requested.emit(vm_id)
                else:
                    self.details_requested.emit(vm_id)
            return
        super().keyPressEvent(event)
