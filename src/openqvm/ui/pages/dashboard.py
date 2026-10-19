"""Dashboard page: real inventory and host resource overview."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from openqvm.app.context import AppContext
from openqvm.core.models import VMStatus
from openqvm.services.metrics import human_bytes
from openqvm.services.operations import relative_time
from openqvm.ui.theme import colours
from openqvm.ui.widgets.common import (
    Banner,
    Card,
    MetricCard,
    PageHeader,
    ScrollPage,
    StatusPill,
)
from openqvm.vm.manager import VmSummary


class DashboardPage(ScrollPage):
    create_requested = Signal()
    show_vms_requested = Signal()
    open_console_requested = Signal(str)
    open_vm_requested = Signal(str)
    settings_requested = Signal()

    def __init__(self, context: AppContext, parent=None) -> None:
        super().__init__(parent)
        self.context = context
        self._active_rows = QVBoxLayout()
        self._build()

    def _build(self) -> None:
        header = PageHeader("Dashboard", "A live view of your virtual machines and host resources.")
        new_button = QPushButton("New virtual machine")
        new_button.setProperty("variant", "primary")
        new_button.setShortcut("Ctrl+N")
        new_button.clicked.connect(self.create_requested)
        header.add_action(new_button)
        self.content.addWidget(header)

        self.banner = Banner()
        self.content.addWidget(self.banner)

        counts = QHBoxLayout()
        counts.setSpacing(14)
        self.total_card = MetricCard("Virtual machines", "0", "configured")
        self.running_card = MetricCard("Running", "0", "active guests")
        self.paused_card = MetricCard("Paused", "0", "suspended")
        self.stopped_card = MetricCard("Stopped", "0", "ready to start")
        for card in (self.total_card, self.running_card, self.paused_card, self.stopped_card):
            counts.addWidget(card, 1)
        self.content.addLayout(counts)

        columns = QHBoxLayout()
        columns.setSpacing(16)

        resources = Card()
        resource_layout = resources.body()
        title = QLabel("Host resources")
        title.setProperty("role", "h3")
        resource_layout.addWidget(title)
        self.cpu_row = _ResourceRow("Host CPU", "Unavailable")
        self.memory_row = _ResourceRow("Host memory", "Unavailable")
        self.disk_row = _ResourceRow("OpenQVM data storage", "Unavailable")
        self.qemu_row = _ResourceRow("QEMU processes", "0")
        for row in (self.cpu_row, self.memory_row, self.disk_row, self.qemu_row):
            resource_layout.addWidget(row)
        note = QLabel("Metrics are measured from the host through psutil. Unavailable values are never simulated.")
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        resource_layout.addWidget(note)
        resource_layout.addStretch(1)
        columns.addWidget(resources, 3)

        right = QVBoxLayout()
        right.setSpacing(16)

        quick = Card()
        quick_layout = quick.body()
        quick_title = QLabel("Quick actions")
        quick_title.setProperty("role", "h3")
        quick_layout.addWidget(quick_title)
        show_vms = QPushButton("Browse virtual machines")
        show_vms.clicked.connect(self.show_vms_requested)
        quick_layout.addWidget(show_vms)
        settings = QPushButton("Backend and settings")
        settings.setProperty("variant", "ghost")
        settings.clicked.connect(self.settings_requested)
        quick_layout.addWidget(settings)
        self.backend_label = QLabel("Backend not detected")
        self.backend_label.setProperty("role", "muted")
        self.backend_label.setWordWrap(True)
        quick_layout.addWidget(self.backend_label)
        quick_layout.addStretch(1)
        right.addWidget(quick)

        active = Card()
        active_layout = active.body()
        active_title = QLabel("Running now")
        active_title.setProperty("role", "h3")
        active_layout.addWidget(active_title)
        self._active_rows.setSpacing(6)
        active_layout.addLayout(self._active_rows)
        self.active_empty = QLabel("No virtual machines are running.")
        self.active_empty.setProperty("role", "muted")
        active_layout.addWidget(self.active_empty)
        active_layout.addStretch(1)
        right.addWidget(active, 1)
        columns.addLayout(right, 2)
        self.content.addLayout(columns)

        recent = Card()
        recent_layout = recent.body()
        recent_title = QLabel("Recent operations")
        recent_title.setProperty("role", "h3")
        recent_layout.addWidget(recent_title)
        self.recent_table = QTableWidget(0, 4)
        self.recent_table.setHorizontalHeaderLabels(["When", "Action", "VM", "Result"])
        self.recent_table.verticalHeader().setVisible(False)
        self.recent_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.recent_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.recent_table.setAlternatingRowColors(True)
        header_view = self.recent_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.recent_table.setMinimumHeight(180)
        recent_layout.addWidget(self.recent_table)
        self.content.addWidget(recent)
        self.content.addStretch(1)

    def refresh(self, summaries: list[VmSummary], errors: list[str]) -> None:
        counts = {"total": len(summaries), "running": 0, "paused": 0, "stopped": 0, "error": 0}
        for summary in summaries:
            if summary.status is VMStatus.RUNNING:
                counts["running"] += 1
            elif summary.status is VMStatus.PAUSED:
                counts["paused"] += 1
            elif summary.status is VMStatus.STOPPED:
                counts["stopped"] += 1
            elif summary.status is VMStatus.ERROR:
                counts["error"] += 1
        self.total_card.set(str(counts["total"]), f"{counts['error']} in error state" if counts["error"] else "configured")
        self.running_card.set(str(counts["running"]), "active guests")
        self.paused_card.set(str(counts["paused"]), "suspended")
        self.stopped_card.set(str(counts["stopped"]), "ready to start")

        capabilities = self.context.capabilities
        if not capabilities.backend_available:
            self.banner.show_message(
                "QEMU is not available. Virtual machines cannot be started until a backend is installed.",
                severity="error",
                action_text="Open settings",
                action=self.settings_requested.emit,
            )
        elif errors:
            self.banner.show_message("; ".join(errors[:3]), severity="warning")
        elif not capabilities.bridged_supported and any(nic.mode.value == "bridged" for s in summaries for nic in s.spec.nics):
            self.banner.show_message(
                "One or more VMs request bridged networking, which is not available on this host.",
                severity="warning",
                action_text="Review VMs",
                action=self.show_vms_requested.emit,
            )
        else:
            self.banner.clear()

        if capabilities.qemu:
            self.backend_label.setText(
                f"Backend: QEMU {capabilities.qemu.version}\n"
                f"Accelerator: {capabilities.default_accelerator or 'none'}\n"
                f"Platform: {capabilities.os_family}/{capabilities.architecture}"
            )
        else:
            self.backend_label.setText("Backend: QEMU not found")

        metrics = self.context.metrics.snapshot(
            [(summary.spec.id, summary.runtime.pid, summary.runtime.process_create_time) for summary in summaries]
        )
        if metrics.cpu_percent is not None:
            self.cpu_row.set(f"{metrics.cpu_percent:.1f}%", metrics.cpu_percent / 100.0)
        else:
            self.cpu_row.set("Unavailable", None)
        if metrics.memory_used_bytes and metrics.memory_total_bytes:
            percentage = metrics.memory_used_bytes / metrics.memory_total_bytes
            self.memory_row.set(
                f"{human_bytes(metrics.memory_used_bytes)} / {human_bytes(metrics.memory_total_bytes)}",
                percentage,
            )
        else:
            self.memory_row.set("Unavailable", None)
        if metrics.disk_used_bytes and metrics.disk_total_bytes:
            self.disk_row.set(
                f"{human_bytes(metrics.disk_used_bytes)} / {human_bytes(metrics.disk_total_bytes)}",
                metrics.disk_used_bytes / metrics.disk_total_bytes,
            )
        else:
            self.disk_row.set("Unavailable", None)
        qemu_count = metrics.qemu_process_count
        self.qemu_row.set(str(qemu_count) if qemu_count is not None else "Unavailable", None)

        self._refresh_active(summaries)
        self._refresh_operations()

    def _refresh_active(self, summaries: list[VmSummary]) -> None:
        while self._active_rows.count():
            item = self._active_rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        active = [summary for summary in summaries if summary.status in (VMStatus.RUNNING, VMStatus.PAUSED)]
        self.active_empty.setVisible(not active)
        for summary in active[:5]:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            name = QLabel(summary.spec.name)
            name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            pill = StatusPill(summary.status)
            console = QPushButton("Console")
            console.setProperty("variant", "ghost")
            console.clicked.connect(lambda _=False, vm_id=summary.spec.id: self.open_console_requested.emit(vm_id))
            details = QPushButton("Details")
            details.setProperty("variant", "ghost")
            details.clicked.connect(lambda _=False, vm_id=summary.spec.id: self.open_vm_requested.emit(vm_id))
            layout.addWidget(name)
            layout.addWidget(pill)
            layout.addWidget(console)
            layout.addWidget(details)
            self._active_rows.addWidget(row)

    def _refresh_operations(self) -> None:
        operations = self.context.manager.recent_operations(8)
        theme = colours(self.context.settings.theme)
        self.recent_table.setRowCount(len(operations))
        for row, record in enumerate(operations):
            values = [
                relative_time(record.finished_at or record.started_at),
                record.action,
                record.vm_name or "—",
                record.message or record.status.value,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 3 and record.status.value == "failed":
                    item.setForeground(QColor(theme.danger))
                elif column == 3 and record.status.value == "succeeded":
                    item.setForeground(QColor(theme.success))
                self.recent_table.setItem(row, column, item)
        self.recent_table.setVisible(bool(operations))


class _ResourceRow(QWidget):
    def __init__(self, label: str, value: str, parent=None) -> None:
        super().__init__(parent)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(5)
        self.label = QLabel(label)
        self.label.setProperty("role", "muted")
        self.value = QLabel(value)
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        layout.addWidget(self.label, 0, 0)
        layout.addWidget(self.value, 0, 1)
        layout.addWidget(self.bar, 1, 0, 1, 2)

    def set(self, text: str, fraction: float | None) -> None:
        self.value.setText(text)
        if fraction is None:
            self.bar.setVisible(False)
        else:
            self.bar.setVisible(True)
            self.bar.setValue(max(0, min(1000, int(fraction * 1000))))
