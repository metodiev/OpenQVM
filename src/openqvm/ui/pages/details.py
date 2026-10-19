"""Virtual machine details, storage, networking, snapshots and logs."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from openqvm.app.context import AppContext
from openqvm.core.errors import OpenQVMError
from openqvm.core.models import (
    CpuSpec,
    DiskBus,
    DiskFormat,
    DisplaySpec,
    Firmware,
    GraphicsAdapter,
    GuestOS,
    NetworkMode,
    NicModel,
    NicSpec,
    PortForward,
    VMSpec,
    VMStatus,
    generate_mac,
)
from openqvm.services.metrics import human_bytes
from openqvm.ui.async_ops import AsyncRunner
from openqvm.ui.widgets.common import (
    Banner,
    Card,
    LogTail,
    StatusPill,
    confirm,
    format_datetime,
    open_folder,
    show_error,
)
from openqvm.vm.manager import SnapshotView, VmSummary


class VmDetailsPage(QWidget):
    back_requested = Signal()
    console_requested = Signal(str)
    changed = Signal()
    deleted = Signal(str)

    def __init__(self, context: AppContext, runner: AsyncRunner, parent=None) -> None:
        super().__init__(parent)
        self.context = context
        self.runner = runner
        self.vm_id: str | None = None
        self.spec: VMSpec | None = None
        self.runtime = None
        self._snapshot_views: list[SnapshotView] = []
        self._qemu_tail = LogTail(Path())
        self._serial_tail = LogTail(Path())
        self._build()
        self.log_timer = QTimer(self)
        self.log_timer.setInterval(1500)
        self.log_timer.timeout.connect(self._refresh_logs)

    # -- construction ---------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 18, 28, 22)
        layout.setSpacing(12)

        header = QHBoxLayout()
        back = QPushButton("← Virtual machines")
        back.setProperty("variant", "ghost")
        back.clicked.connect(self.back_requested)
        header.addWidget(back)
        header.addSpacing(6)
        self.name_label = QLabel("Virtual machine")
        self.name_label.setProperty("role", "h1")
        header.addWidget(self.name_label)
        self.status_pill = StatusPill(VMStatus.UNKNOWN)
        header.addWidget(self.status_pill)
        header.addStretch(1)
        self.start_button = QPushButton("Start")
        self.start_button.setProperty("variant", "primary")
        self.start_button.clicked.connect(lambda: self._lifecycle("start"))
        self.pause_button = QPushButton("Pause")
        self.pause_button.clicked.connect(lambda: self._lifecycle("pause"))
        self.resume_button = QPushButton("Resume")
        self.resume_button.clicked.connect(lambda: self._lifecycle("resume"))
        self.stop_button = QPushButton("Stop")
        stop_menu = QMenu(self.stop_button)
        stop_menu.addAction("Shut down (ACPI)", lambda: self._lifecycle("stop"))
        stop_menu.addAction("Force stop", lambda: self._lifecycle("force_stop"))
        self.stop_button.setMenu(stop_menu)
        self.console_button = QPushButton("Open console")
        self.console_button.clicked.connect(self._open_console)
        for button in (self.start_button, self.pause_button, self.resume_button, self.stop_button, self.console_button):
            header.addWidget(button)
        layout.addLayout(header)

        self.banner = Banner()
        layout.addWidget(self.banner)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._wrap_scroll(self._build_overview()), "Overview")
        self.tabs.addTab(self._wrap_scroll(self._build_storage()), "Storage")
        self.tabs.addTab(self._wrap_scroll(self._build_network()), "Network")
        self.tabs.addTab(self._wrap_scroll(self._build_snapshots()), "Snapshots")
        self.tabs.addTab(self._build_logs(), "Logs")
        self.tabs.addTab(self._wrap_scroll(self._build_danger()), "Danger zone")
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs, 1)

    @staticmethod
    def _wrap_scroll(inner: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        return scroll

    def _build_overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)
        self.overview_banner = Banner()
        layout.addWidget(self.overview_banner)

        form_card = Card()
        form = QFormLayout(form_card)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)
        self.name_edit = QLineEdit()
        form.addRow("Name", self.name_edit)
        self.description_edit = QLineEdit()
        self.description_edit.setPlaceholderText("Optional description")
        form.addRow("Description", self.description_edit)
        self.os_combo = QComboBox()
        for guest in GuestOS:
            self.os_combo.addItem(guest.display_name, guest.value)
        form.addRow("Guest operating system", self.os_combo)
        self.cpu_spin = QSpinBox()
        self.cpu_spin.setRange(1, 128)
        self.cpu_spin.setSuffix(" vCPU")
        form.addRow("Processors", self.cpu_spin)
        self.memory_spin = QSpinBox()
        self.memory_spin.setRange(128, 1024 * 1024)
        self.memory_spin.setSingleStep(256)
        self.memory_spin.setSuffix(" MiB")
        form.addRow("Memory", self.memory_spin)
        self.firmware_combo = QComboBox()
        self.firmware_combo.addItem("BIOS (legacy)", Firmware.BIOS.value)
        self.firmware_combo.addItem("UEFI (edk2/OVMF)", Firmware.UEFI.value)
        form.addRow("Firmware", self.firmware_combo)
        self.machine_combo = QComboBox()
        self.machine_combo.addItem("q35 (modern)", "q35")
        self.machine_combo.addItem("pc (legacy i440fx)", "pc")
        form.addRow("Machine type", self.machine_combo)
        self.accelerator_combo = QComboBox()
        self.accelerator_combo.addItem("Automatic", "")
        for accelerator in self.context.capabilities.accelerator_names:
            self.accelerator_combo.addItem(accelerator, accelerator)
        form.addRow("Accelerator", self.accelerator_combo)
        self.graphics_combo = QComboBox()
        self.graphics_combo.addItem("Standard VGA", GraphicsAdapter.STD.value)
        self.graphics_combo.addItem("VirtIO GPU", GraphicsAdapter.VIRTIO.value)
        form.addRow("Display adapter", self.graphics_combo)
        self.save_button = QPushButton("Save changes")
        self.save_button.setProperty("variant", "primary")
        self.save_button.clicked.connect(self._save_overview)
        form.addRow("", self.save_button)
        layout.addWidget(form_card)

        runtime_card = Card()
        runtime_layout = QGridLayout(runtime_card)
        runtime_layout.setContentsMargins(16, 16, 16, 16)
        title = QLabel("Runtime information")
        title.setProperty("role", "h3")
        runtime_layout.addWidget(title, 0, 0, 1, 2)
        self.runtime_labels: dict[str, QLabel] = {}
        rows = [
            ("id", "Identifier"),
            ("pid", "QEMU process"),
            ("accelerator", "Accelerator"),
            ("vnc", "VNC endpoint"),
            ("started", "Started"),
            ("error", "Last error"),
        ]
        for row, (key, label) in enumerate(rows, start=1):
            caption = QLabel(label)
            caption.setProperty("role", "muted")
            value = QLabel("—")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setWordWrap(True)
            runtime_layout.addWidget(caption, row, 0)
            runtime_layout.addWidget(value, row, 1)
            self.runtime_labels[key] = value
        runtime_layout.setColumnStretch(1, 1)
        layout.addWidget(runtime_card)
        layout.addStretch(1)
        return page

    def _build_storage(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        self.storage_banner = Banner()
        layout.addWidget(self.storage_banner)

        disk_card = Card()
        disk_layout = QVBoxLayout(disk_card)
        disk_layout.setContentsMargins(16, 16, 16, 16)
        title = QLabel("Virtual disks")
        title.setProperty("role", "h3")
        disk_layout.addWidget(title)
        self.disk_table = QTableWidget(0, 6)
        self.disk_table.setHorizontalHeaderLabels(
            ["Role", "File", "Format", "Bus", "Virtual size", "Allocated"]
        )
        self.disk_table.verticalHeader().setVisible(False)
        self.disk_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.disk_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.disk_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        header = self.disk_table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.disk_table.setMinimumHeight(150)
        disk_layout.addWidget(self.disk_table)
        disk_actions = QHBoxLayout()
        self.add_disk_button = QPushButton("Add disk…")
        self.add_disk_button.clicked.connect(self._add_disk)
        self.resize_disk_button = QPushButton("Resize…")
        self.resize_disk_button.clicked.connect(self._resize_disk)
        self.check_disks_button = QPushButton("Check disks…")
        self.check_disks_button.setToolTip("Run qemu-img check on every disk (VM must be stopped)")
        self.check_disks_button.clicked.connect(self._check_disks)
        self.remove_disk_button = QPushButton("Remove…")
        self.remove_disk_button.setProperty("variant", "danger")
        self.remove_disk_button.clicked.connect(self._remove_disk)
        disk_open = QPushButton("Open disk folder")
        disk_open.setProperty("variant", "ghost")
        disk_open.clicked.connect(lambda: open_folder(self.context.paths.vm_disk_dir(self.vm_id or "")))
        disk_actions.addWidget(self.add_disk_button)
        disk_actions.addWidget(self.resize_disk_button)
        disk_actions.addWidget(self.check_disks_button)
        disk_actions.addWidget(self.remove_disk_button)
        disk_actions.addStretch(1)
        disk_actions.addWidget(disk_open)
        disk_layout.addLayout(disk_actions)
        layout.addWidget(disk_card)

        iso_card = Card()
        iso_layout = QVBoxLayout(iso_card)
        iso_layout.setContentsMargins(16, 16, 16, 16)
        iso_title = QLabel("Installation image")
        iso_title.setProperty("role", "h3")
        iso_layout.addWidget(iso_title)
        self.iso_label = QLabel("No image attached")
        self.iso_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.iso_label.setWordWrap(True)
        iso_layout.addWidget(self.iso_label)
        self.boot_label = QLabel()
        self.boot_label.setProperty("role", "muted")
        iso_layout.addWidget(self.boot_label)
        iso_actions = QHBoxLayout()
        self.attach_iso_button = QPushButton("Attach ISO…")
        self.attach_iso_button.clicked.connect(self._attach_iso)
        self.eject_iso_button = QPushButton("Eject")
        self.eject_iso_button.clicked.connect(self._eject_iso)
        iso_actions.addWidget(self.attach_iso_button)
        iso_actions.addWidget(self.eject_iso_button)
        iso_actions.addStretch(1)
        iso_layout.addLayout(iso_actions)
        layout.addWidget(iso_card)
        layout.addStretch(1)
        return page

    def _build_network(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        self.network_banner = Banner()
        layout.addWidget(self.network_banner)
        card = Card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        title = QLabel("Network adapters")
        title.setProperty("role", "h3")
        card_layout.addWidget(title)
        self.nic_table = QTableWidget(0, 5)
        self.nic_table.setHorizontalHeaderLabels(["Mode", "Model", "MAC address", "Bridge", "Port forwards"])
        self.nic_table.verticalHeader().setVisible(False)
        self.nic_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.nic_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.nic_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.nic_table.setMinimumHeight(140)
        card_layout.addWidget(self.nic_table)
        actions = QHBoxLayout()
        self.add_nic_button = QPushButton("Add adapter…")
        self.add_nic_button.clicked.connect(self._add_nic)
        self.edit_nic_button = QPushButton("Edit…")
        self.edit_nic_button.clicked.connect(self._edit_nic)
        self.remove_nic_button = QPushButton("Remove")
        self.remove_nic_button.setProperty("variant", "danger")
        self.remove_nic_button.clicked.connect(self._remove_nic)
        actions.addWidget(self.add_nic_button)
        actions.addWidget(self.edit_nic_button)
        actions.addWidget(self.remove_nic_button)
        actions.addStretch(1)
        card_layout.addLayout(actions)
        layout.addWidget(card)
        self.network_note = QLabel()
        self.network_note.setWordWrap(True)
        self.network_note.setProperty("role", "muted")
        layout.addWidget(self.network_note)
        layout.addStretch(1)
        return page

    def _build_snapshots(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        self.snapshot_banner = Banner()
        layout.addWidget(self.snapshot_banner)
        card = Card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        title = QLabel("Snapshots")
        title.setProperty("role", "h3")
        card_layout.addWidget(title)
        explanation = QLabel(
            "Snapshots created while the VM is running try to include memory first. Accelerators without "
            "memory-save support (for example WHPX) fall back to disk-only snapshots, which can only be "
            "restored while the VM is stopped. Offline snapshots always contain disk state only."
        )
        explanation.setWordWrap(True)
        explanation.setProperty("role", "muted")
        card_layout.addWidget(explanation)
        self.snapshot_table = QTableWidget(0, 5)
        self.snapshot_table.setHorizontalHeaderLabels(["Name", "Contents", "Created", "State", "Description"])
        self.snapshot_table.verticalHeader().setVisible(False)
        self.snapshot_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.snapshot_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        snapshot_header = self.snapshot_table.horizontalHeader()
        for column in (0, 1, 2, 3):
            snapshot_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        snapshot_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.snapshot_table.setMinimumHeight(180)
        card_layout.addWidget(self.snapshot_table)
        actions = QHBoxLayout()
        self.create_snapshot_button = QPushButton("Create snapshot…")
        self.create_snapshot_button.setProperty("variant", "primary")
        self.create_snapshot_button.clicked.connect(self._create_snapshot)
        self.restore_snapshot_button = QPushButton("Restore…")
        self.restore_snapshot_button.clicked.connect(self._restore_snapshot)
        self.delete_snapshot_button = QPushButton("Delete…")
        self.delete_snapshot_button.setProperty("variant", "danger")
        self.delete_snapshot_button.clicked.connect(self._delete_snapshot)
        self.refresh_snapshot_button = QPushButton("Refresh")
        self.refresh_snapshot_button.clicked.connect(self._load_snapshots)
        actions.addWidget(self.create_snapshot_button)
        actions.addWidget(self.restore_snapshot_button)
        actions.addWidget(self.delete_snapshot_button)
        actions.addStretch(1)
        actions.addWidget(self.refresh_snapshot_button)
        card_layout.addLayout(actions)
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _build_logs(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        controls = QHBoxLayout()
        self.log_path_label = QLabel()
        self.log_path_label.setProperty("role", "muted")
        controls.addWidget(self.log_path_label, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._load_logs)
        open_logs = QPushButton("Open log folder")
        open_logs.clicked.connect(lambda: open_folder(self.context.paths.vm_dir(self.vm_id or "")))
        controls.addWidget(refresh)
        controls.addWidget(open_logs)
        layout.addLayout(controls)
        splitter = QSplitter(Qt.Orientation.Vertical)
        qemu_box = QGroupBox("QEMU log")
        qemu_layout = QVBoxLayout(qemu_box)
        self.qemu_log = QPlainTextEdit()
        self.qemu_log.setReadOnly(True)
        self.qemu_log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        qemu_layout.addWidget(self.qemu_log)
        serial_box = QGroupBox("Serial console output")
        serial_layout = QVBoxLayout(serial_box)
        self.serial_log = QPlainTextEdit()
        self.serial_log.setReadOnly(True)
        self.serial_log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        serial_layout.addWidget(self.serial_log)
        splitter.addWidget(qemu_box)
        splitter.addWidget(serial_box)
        layout.addWidget(splitter, 1)
        return page

    def _build_danger(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        card = Card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        title = QLabel("Destructive actions")
        title.setProperty("role", "h3")
        card_layout.addWidget(title)
        explanation = QLabel(
            "Deleting a VM moves its entire directory (configuration, disks, snapshots and logs) to the "
            "OpenQVM trash folder. Nothing is securely erased and the data can still be recovered until the "
            "trash is purged."
        )
        explanation.setWordWrap(True)
        card_layout.addWidget(explanation)
        buttons = QHBoxLayout()
        self.force_stop_button = QPushButton("Force stop VM")
        self.force_stop_button.clicked.connect(lambda: self._lifecycle("force_stop"))
        self.delete_button = QPushButton("Delete virtual machine…")
        self.delete_button.setProperty("variant", "danger")
        self.delete_button.clicked.connect(self._delete_vm)
        buttons.addWidget(self.force_stop_button)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        card_layout.addLayout(buttons)
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    # -- loading --------------------------------------------------------------

    def set_vm(self, vm_id: str) -> None:
        self.vm_id = vm_id
        self.tabs.setCurrentIndex(0)
        self._qemu_tail = LogTail(self.context.manager.qemu_log_path(vm_id))
        self._serial_tail = LogTail(self.context.manager.serial_log_path(vm_id))
        self.qemu_log.clear()
        self.serial_log.clear()
        self.full_refresh()

    def full_refresh(self) -> None:
        if not self.vm_id:
            return
        try:
            summary = self.context.manager.get_summary(self.vm_id)
        except OpenQVMError as exc:
            show_error(self, exc)
            self.back_requested.emit()
            return
        self.spec = summary.spec
        self.runtime = summary.runtime
        self._populate_header()
        self._populate_overview(summary)
        self._populate_storage()
        self._populate_network()
        self._load_snapshots()
        self._load_logs()

    def refresh_runtime(self) -> None:
        if not self.vm_id or self.spec is None:
            return
        try:
            summary = self.context.manager.get_summary(self.vm_id)
        except OpenQVMError:
            return
        self.runtime = summary.runtime
        self.spec = summary.spec
        self._populate_header()
        self._populate_runtime(summary)
        self._update_editable_state()

    def _populate_header(self) -> None:
        assert self.spec is not None and self.runtime is not None
        self.name_label.setText(self.spec.name)
        self.status_pill.set_status(self.runtime.status)
        active = self._busy
        running = self.runtime.status is VMStatus.RUNNING
        paused = self.runtime.status is VMStatus.PAUSED
        self.start_button.setVisible(not active)
        self.start_button.setEnabled(not active)
        self.pause_button.setVisible(running)
        self.resume_button.setVisible(paused)
        self.stop_button.setVisible(active)
        self.console_button.setEnabled(running or paused)
        if self.runtime.last_error:
            self.banner.show_message(self.runtime.last_error, severity="error")
        else:
            self.banner.clear()

    @property
    def _busy(self) -> bool:
        if self.runtime is None:
            return False
        return bool(
            self.runtime.status.is_active
            or (self.runtime.pid is not None and self.runtime.status is not VMStatus.STOPPED)
        )

    def _populate_overview(self, summary: VmSummary) -> None:
        spec = summary.spec
        self.name_edit.setText(spec.name)
        self.description_edit.setText(spec.description)
        index = self.os_combo.findData(spec.guest_os.value)
        self.os_combo.setCurrentIndex(max(0, index))
        self.cpu_spin.setValue(spec.cpu.vcpus)
        self.memory_spin.setValue(spec.memory_mib)
        index = self.firmware_combo.findData(spec.firmware.value)
        self.firmware_combo.setCurrentIndex(max(0, index))
        index = self.machine_combo.findData(spec.machine)
        self.machine_combo.setCurrentIndex(max(0, index))
        index = self.accelerator_combo.findData(spec.accelerator or "")
        self.accelerator_combo.setCurrentIndex(max(0, index))
        index = self.graphics_combo.findData(spec.display.adapter.value)
        self.graphics_combo.setCurrentIndex(max(0, index))
        self._populate_runtime(summary)
        self._update_editable_state()

    def _populate_runtime(self, summary: VmSummary) -> None:
        spec = summary.spec
        runtime = summary.runtime
        self.runtime_labels["id"].setText(spec.id)
        self.runtime_labels["pid"].setText(str(runtime.pid) if runtime.pid else "—")
        self.runtime_labels["accelerator"].setText(
            runtime.accelerator or self.context.capabilities.default_accelerator or "—"
        )
        self.runtime_labels["vnc"].setText(
            f"127.0.0.1:{runtime.vnc_port}" if runtime.vnc_port else "—"
        )
        self.runtime_labels["started"].setText(format_datetime(runtime.started_at))
        self.runtime_labels["error"].setText(runtime.last_error or "—")

    def _update_editable_state(self) -> None:
        active = self._busy
        capabilities = self.context.manager.backend.capabilities
        for widget in (self.cpu_spin, self.memory_spin, self.firmware_combo, self.machine_combo,
                       self.accelerator_combo, self.graphics_combo, self.os_combo):
            widget.setEnabled(not active)
        self.save_button.setEnabled(True)
        if active:
            self.overview_banner.show_message(
                "Stop the virtual machine to change hardware settings. Name and description can still be edited.",
                severity="info",
            )
        else:
            self.overview_banner.clear()
        self.add_disk_button.setEnabled(not active)
        self.remove_disk_button.setEnabled(not active)
        self.check_disks_button.setEnabled(not active)
        self.resize_disk_button.setEnabled(not active or capabilities.supports_online_disk_resize)
        self.attach_iso_button.setEnabled(not active or capabilities.supports_live_iso_change)
        self.eject_iso_button.setEnabled(
            not active
            or (capabilities.supports_live_iso_change and bool(self.spec and self.spec.cdrom_iso))
        )
        for widget in (self.add_nic_button, self.edit_nic_button, self.remove_nic_button):
            widget.setEnabled(not active)

    def _populate_storage(self) -> None:
        if self.spec is None:
            return
        active = self._busy
        self.disk_table.setRowCount(len(self.spec.disks))
        for row, disk in enumerate(self.spec.disks):
            self.disk_table.setItem(row, 0, QTableWidgetItem(disk.role.title()))
            filename = QTableWidgetItem(disk.filename)
            filename.setData(Qt.ItemDataRole.UserRole, disk.id)
            self.disk_table.setItem(row, 1, filename)
            self.disk_table.setItem(row, 2, QTableWidgetItem(disk.format.value))
            self.disk_table.setItem(row, 3, QTableWidgetItem(disk.bus.display_name))
            self.disk_table.setItem(row, 4, QTableWidgetItem(human_bytes(disk.size_bytes)))
            allocated = "—"
            if not active:
                try:
                    info = self.context.manager.disks.info(self.context.manager.store.disk_path(self.spec, disk))
                    allocated = human_bytes(info.actual_size)
                except OpenQVMError:
                    allocated = "Unavailable"
            self.disk_table.setItem(row, 5, QTableWidgetItem(allocated))
        self.iso_label.setText(self.spec.cdrom_iso or "No image attached")
        self.boot_label.setText(
            "Boot order: " + " → ".join(device.value for device in self.spec.boot_order)
        )
        if active:
            capabilities = self.context.manager.backend.capabilities
            live_actions: list[str] = []
            if capabilities.supports_live_iso_change:
                live_actions.append("change the installation image")
            if capabilities.supports_online_disk_resize:
                live_actions.append("grow a disk")
            if live_actions:
                self.storage_banner.show_message(
                    "The VM is running. You can still "
                    + " and ".join(live_actions)
                    + ". The guest may need to rescan the block device before it sees a new disk size.",
                    severity="info",
                )
            else:
                self.storage_banner.show_message(
                    "Storage changes require the virtual machine to be stopped.", severity="info"
                )
        else:
            self.storage_banner.clear()

    def _populate_network(self) -> None:
        if self.spec is None:
            return
        self.nic_table.setRowCount(len(self.spec.nics))
        for row, nic in enumerate(self.spec.nics):
            self.nic_table.setItem(row, 0, QTableWidgetItem(nic.mode.display_name))
            self.nic_table.setItem(row, 1, QTableWidgetItem(nic.model.display_name))
            mac_item = QTableWidgetItem(nic.mac)
            mac_item.setData(Qt.ItemDataRole.UserRole, nic.id)
            self.nic_table.setItem(row, 2, mac_item)
            self.nic_table.setItem(row, 3, QTableWidgetItem(nic.bridge or "—"))
            forwards = ", ".join(
                f"{forward.protocol} {forward.host_address}:{forward.host_port}→{forward.guest_port}"
                for forward in nic.port_forwards
            )
            self.nic_table.setItem(row, 4, QTableWidgetItem(forwards or "—"))
        self.network_note.setText(self.context.manager.network_security_note(self.spec))
        capabilities = self.context.capabilities
        if any(nic.mode is NetworkMode.BRIDGED for nic in self.spec.nics) and not capabilities.bridged_supported:
            self.network_banner.show_message(capabilities.bridged_detail, severity="warning")
        elif any(nic.mode is NetworkMode.HOST_ONLY for nic in self.spec.nics) and not capabilities.host_only_supported:
            self.network_banner.show_message(capabilities.host_only_detail, severity="warning")
        else:
            self.network_banner.clear()

    def _load_snapshots(self) -> None:
        self.reload_snapshots()

    def reload_snapshots(self) -> None:
        if not self.vm_id:
            return
        try:
            self._snapshot_views = self.context.manager.list_snapshots(self.vm_id)
        except OpenQVMError as exc:
            self._snapshot_views = []
            self.snapshot_banner.show_message(exc.message, severity="warning")
        else:
            if self.the_snapshot_is_missing():
                self.snapshot_banner.show_message(
                    "Some snapshots exist only in metadata and are missing from the VM disks.", severity="warning"
                )
            else:
                self.snapshot_banner.clear()
        self.snapshot_table.setRowCount(len(self._snapshot_views))
        for row, view in enumerate(self._snapshot_views):
            name_item = QTableWidgetItem(view.name)
            name_item.setData(Qt.ItemDataRole.UserRole, view.name)
            self.snapshot_table.setItem(row, 0, name_item)
            contents = "Memory + disk" if view.includes_memory else "Disk only"
            if view.restore_requires_stop:
                contents += " (restore while stopped)"
            self.snapshot_table.setItem(row, 1, QTableWidgetItem(contents))
            self.snapshot_table.setItem(row, 2, QTableWidgetItem(format_datetime(view.created_at)))
            state = view.vm_state if view.present else "missing from disk"
            self.snapshot_table.setItem(row, 3, QTableWidgetItem(state))
            self.snapshot_table.setItem(row, 4, QTableWidgetItem(view.description or "—"))

    def the_snapshot_is_missing(self) -> bool:
        return any(not view.present for view in self._snapshot_views)

    def _load_logs(self) -> None:
        if not self.vm_id:
            return
        self._qemu_tail.reset()
        self._serial_tail.reset()
        self.qemu_log.clear()
        self.serial_log.clear()
        self.log_path_label.setText(str(self.context.paths.vm_dir(self.vm_id)))
        self._refresh_logs()

    def _refresh_logs(self) -> None:
        if not self.vm_id or self.tabs.currentIndex() != 4:
            return
        appended_qemu = self._qemu_tail.read()
        if appended_qemu:
            self.qemu_log.appendPlainText(appended_qemu.rstrip("\n"))
        appended_serial = self._serial_tail.read()
        if appended_serial:
            self.serial_log.appendPlainText(appended_serial.rstrip("\n"))

    def _tab_changed(self, index: int) -> None:
        if index == 4:
            self.log_timer.start()
            self._refresh_logs()
        else:
            self.log_timer.stop()
        if index == 3:
            self._load_snapshots()

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def] # noqa: N802
        super().showEvent(event)
        if self.tabs.currentIndex() == 4:
            self.log_timer.start()

    def hideEvent(self, event) -> None:  # type: ignore[no-untyped-def] # noqa: N802
        self.log_timer.stop()
        super().hideEvent(event)

    # -- operations -----------------------------------------------------------

    def _run(self, description: str, function, on_success=None, *, refresh: bool = True) -> None:  # type: ignore[no-untyped-def]
        self.runner.submit(
            description,
            function,
            on_success=lambda result: self._operation_done(result, on_success, refresh),
            on_error=lambda error: self._operation_failed(error),
        )

    def _operation_done(self, result, on_success, refresh: bool = True) -> None:  # type: ignore[no-untyped-def]
        if on_success is not None:
            on_success(result)
        if refresh:
            self.full_refresh()
        self.changed.emit()

    def _operation_failed(self, error: OpenQVMError) -> None:
        show_error(self, error)
        self.full_refresh()

    def _lifecycle(self, action: str) -> None:
        if not self.vm_id:
            return
        manager = self.context.manager
        vm_id = self.vm_id
        if action == "start":
            self._run("Starting VM", lambda: manager.start_vm(vm_id))
        elif action == "pause":
            self._run("Pausing VM", lambda: manager.pause_vm(vm_id))
        elif action == "resume":
            self._run("Resuming VM", lambda: manager.resume_vm(vm_id))
        elif action == "stop":
            self._run("Stopping VM", lambda: manager.stop_vm(vm_id, force=False, timeout=30))
        elif action == "force_stop":
            if not confirm(
                self,
                "Force stop",
                "Force stopping terminates QEMU immediately.",
                informative="The guest may lose unsaved data and the file system may become inconsistent.",
                confirm_text="Force stop",
                dangerous=True,
            ):
                return
            self._run("Force stopping VM", lambda: manager.stop_vm(vm_id, force=True))

    def _open_console(self) -> None:
        if self.vm_id and self.runtime and self.runtime.status in (VMStatus.RUNNING, VMStatus.PAUSED):
            self.console_requested.emit(self.vm_id)

    def _save_overview(self) -> None:
        if self.spec is None or self.runtime is None:
            return
        spec = self.spec
        active = self.runtime.status.is_active
        if not active:
            spec.cpu = CpuSpec(cores=self.cpu_spin.value())
            spec.memory_mib = self.memory_spin.value()
            spec.firmware = Firmware(self.firmware_combo.currentData())
            spec.machine = self.machine_combo.currentData()
            spec.guest_os = GuestOS(self.os_combo.currentData())
            spec.display = DisplaySpec(adapter=GraphicsAdapter(self.graphics_combo.currentData()))
        spec.accelerator = self.accelerator_combo.currentData() or None
        spec.name = self.name_edit.text().strip()
        spec.description = self.description_edit.text().strip()
        revision = spec.revision
        self._run(
            "Saving VM configuration",
            lambda: self.context.manager.update_vm(spec, expected_revision=revision),
        )

    def _disk_id(self) -> str | None:
        rows = self.disk_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.disk_table.item(rows[0].row(), 1)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _add_disk(self) -> None:
        if not self.vm_id:
            return
        dialog = AddDiskDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        self._run(
            "Adding disk",
            lambda: self.context.manager.add_disk(
                self.vm_id,
                size_bytes=values["size_gib"] * 1024**3,
                disk_format=values["format"],
                bus=values["bus"],
                preallocated=values["preallocated"],
            ),
        )

    def _resize_disk(self) -> None:
        disk_id = self._disk_id()
        if not disk_id or self.spec is None:
            return
        disk = self.spec.disk_by_id(disk_id)
        if disk is None:
            return
        current_gib = max(1, disk.size_bytes // 1024**3)
        size, ok = QInputDialog.getInt(
            self,
            "Resize disk",
            f"New size in GiB (current: {current_gib} GiB):",
            current_gib + 1,
            current_gib + 1,
            16384,
        )
        if not ok:
            return
        live = self._busy
        if live and not confirm(
            self,
            "Live disk resize",
            f"Grow the disk to {size} GiB while the VM is running?",
            informative=(
                "QEMU will change the disk size immediately. The guest may need to rescan the block "
                "device and grow its file system before it can use the additional space."
            ),
            confirm_text="Grow disk",
        ):
            return
        self._run(
            "Resizing disk",
            lambda: self.context.manager.resize_disk(
                self.vm_id, disk_id, size * 1024**3, live=live
            ),
        )

    def _check_disks(self) -> None:
        if not self.vm_id:
            return
        self.runner.submit(
            "Checking disks",
            lambda: self.context.manager.check_disks(self.vm_id),
            on_success=self._show_disk_check,
            on_error=self._operation_failed,
        )

    def _show_disk_check(self, summary) -> None:  # type: ignore[no-untyped-def]
        lines: list[str] = []
        details: list[str] = []
        for result in summary.results:
            state = "OK" if result.ok else "Problems found"
            lines.append(f"{result.path.name}: {state}")
            if result.output.strip():
                details.append(f"=== {result.path.name} ===\n{result.output.strip()}")
            if result.error:
                details.append(f"=== {result.path.name} ===\n{result.error}")
        if not lines:
            lines.append("The virtual machine has no disks to check.")
        message = "\n".join(lines)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information if summary.ok else QMessageBox.Icon.Warning)
        box.setWindowTitle("Disk consistency check")
        box.setText(message)
        if summary.ok:
            box.setInformativeText("No consistency problems were reported by qemu-img check.")
        else:
            box.setInformativeText(
                "At least one disk reported problems. Back up the VM before attempting repairs; "
                "qemu-img does not repair images by itself in this workflow."
            )
        box.setDetailedText("\n\n".join(details))
        box.exec()
        self.full_refresh()

    def _remove_disk(self) -> None:
        disk_id = self._disk_id()
        if not disk_id or self.spec is None:
            return
        disk = self.spec.disk_by_id(disk_id)
        if disk is None:
            return
        if disk.role == "system":
            QMessageBox.information(self, "System disk", "The system disk cannot be removed.")
            return
        if not confirm(
            self,
            "Remove disk",
            f"Detach and move {disk.filename} to the deleted-disks folder?",
            confirm_text="Remove disk",
            dangerous=True,
        ):
            return
        self._run(
            "Removing disk",
            lambda: self.context.manager.remove_disk(self.vm_id, disk_id),
        )

    def _attach_iso(self) -> None:
        if not self.vm_id:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Attach installation image", str(self.context.paths.isos_dir), "Disk images (*.iso *.img)"
        )
        if not path:
            return
        live = self._busy
        if live and not confirm(
            self,
            "Live image change",
            "Change the installation image while the VM is running?",
            informative=(
                "QEMU refuses the change while the guest is using the current medium. If that happens, "
                "stop the VM or use 'openqvm-cli attach-iso --live --force', which can disrupt the guest."
            ),
            confirm_text="Change image",
        ):
            return
        self._run(
            "Attaching ISO",
            lambda: self.context.manager.attach_iso(self.vm_id, path, live=live),
        )

    def _eject_iso(self) -> None:
        if not self.vm_id or not self.spec or not self.spec.cdrom_iso:
            return
        live = self._busy
        if not confirm(
            self,
            "Eject ISO",
            "Detach the installation image from this VM?",
            informative=(
                "The guest may still be using the media; QEMU then refuses the eject unless force is used."
                if live
                else ""
            ),
            confirm_text="Eject",
        ):
            return
        self._run(
            "Ejecting ISO",
            lambda: self.context.manager.eject_iso(self.vm_id, live=live),
        )

    def _selected_nic(self) -> NicSpec | None:
        rows = self.nic_table.selectionModel().selectedRows()
        if not rows or self.spec is None:
            return None
        item = self.nic_table.item(rows[0].row(), 2)
        nic_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((nic for nic in self.spec.nics if nic.id == nic_id), None)

    def _add_nic(self) -> None:
        if not self.vm_id or self.spec is None:
            return
        dialog = NicDialog(self.context, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        spec = self.spec
        spec.nics = [*spec.nics, dialog.nic()]
        self._save_network(spec)

    def _edit_nic(self) -> None:
        nic = self._selected_nic()
        if nic is None or self.spec is None:
            return
        dialog = NicDialog(self.context, self, nic=nic)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.nic()
        spec = self.spec
        spec.nics = [updated if item.id == nic.id else item for item in spec.nics]
        self._save_network(spec)

    def _remove_nic(self) -> None:
        nic = self._selected_nic()
        if nic is None or self.spec is None:
            return
        if not confirm(self, "Remove adapter", "Remove this network adapter from the VM?", confirm_text="Remove"):
            return
        spec = self.spec
        spec.nics = [item for item in spec.nics if item.id != nic.id]
        self._save_network(spec)

    def _save_network(self, spec: VMSpec) -> None:
        revision = spec.revision
        self._run(
            "Saving network configuration",
            lambda: self.context.manager.update_vm(spec, expected_revision=revision),
        )

    def _create_snapshot(self) -> None:
        if not self.vm_id:
            return
        dialog = SnapshotDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, description = dialog.values()
        self._run(
            "Creating snapshot",
            lambda: self.context.manager.create_snapshot(self.vm_id, name, description),
            on_success=lambda _: self._load_snapshots(),
        )

    def _selected_snapshot(self) -> SnapshotView | None:
        rows = self.snapshot_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.snapshot_table.item(rows[0].row(), 0)
        name = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((view for view in self._snapshot_views if view.name == name), None)

    def _restore_snapshot(self) -> None:
        view = self._selected_snapshot()
        if view is None or not self.vm_id:
            return
        if not view.present:
            QMessageBox.warning(self, "Snapshot missing", "This snapshot is not present on the VM disks.")
            return
        active = self._busy
        if active and view.restore_requires_stop:
            QMessageBox.information(
                self,
                "Stop the VM first",
                "This disk-only snapshot can only be restored while the virtual machine is stopped.\n\n"
                "Shut down the VM, then restore the snapshot.",
            )
            return
        if not confirm(
            self,
            "Restore snapshot",
            f"Restore snapshot '{view.name}'?",
            informative=(
                "The guest's current disk state will be replaced."
                + (" Memory state will be restored as well." if view.includes_memory else " Guest memory is not restored.")
            ),
            confirm_text="Restore",
            dangerous=True,
        ):
            return
        self._run(
            "Restoring snapshot",
            lambda: self.context.manager.restore_snapshot(self.vm_id, view.name),
            on_success=lambda _: self._load_snapshots(),
        )

    def _delete_snapshot(self) -> None:
        view = self._selected_snapshot()
        if view is None or not self.vm_id:
            return
        if not confirm(
            self,
            "Delete snapshot",
            f"Delete snapshot '{view.name}'?",
            informative="The snapshot data will be removed from all disks. This cannot be undone.",
            confirm_text="Delete snapshot",
            dangerous=True,
        ):
            return
        self._run(
            "Deleting snapshot",
            lambda: self.context.manager.delete_snapshot(self.vm_id, view.name),
            on_success=lambda _: self._load_snapshots(),
        )

    def _delete_vm(self) -> None:
        if not self.vm_id or self.spec is None:
            return
        if self._busy:
            QMessageBox.warning(self, "VM is running", "Stop the virtual machine before deleting it.")
            return
        if not confirm(
            self,
            "Delete virtual machine",
            f"Move '{self.spec.name}' and all of its disks to the trash?",
            informative="The VM will no longer appear in OpenQVM. Files remain recoverable in the trash folder.",
            confirm_text="Delete VM",
            dangerous=True,
        ):
            return
        vm_id = self.vm_id
        self._run(
            "Deleting VM",
            lambda: self.context.manager.delete_vm(vm_id),
            on_success=lambda _: self.deleted.emit(vm_id),
            refresh=False,
        )


class AddDiskDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add virtual disk")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.size = QSpinBox()
        self.size.setRange(1, 16384)
        self.size.setValue(20)
        self.size.setSuffix(" GiB")
        form.addRow("Size", self.size)
        self.format = QComboBox()
        self.format.addItem("qcow2", DiskFormat.QCOW2.value)
        self.format.addItem("raw", DiskFormat.RAW.value)
        form.addRow("Format", self.format)
        self.bus = QComboBox()
        self.bus.addItem("VirtIO", DiskBus.VIRTIO.value)
        self.bus.addItem("IDE", DiskBus.IDE.value)
        form.addRow("Bus", self.bus)
        self.preallocate = QCheckBox("Preallocate the full size")
        form.addRow("", self.preallocate)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict:
        return {
            "size_gib": self.size.value(),
            "format": DiskFormat(self.format.currentData()),
            "bus": DiskBus(self.bus.currentData()),
            "preallocated": self.preallocate.isChecked(),
        }


class NicDialog(QDialog):
    def __init__(self, context: AppContext, parent=None, *, nic: NicSpec | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self._nic_id = nic.id if nic else None
        self.setWindowTitle("Network adapter")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem("NAT", NetworkMode.NAT.value)
        if context.capabilities.bridged_supported:
            self.mode.addItem("Bridged", NetworkMode.BRIDGED.value)
        if context.capabilities.host_only_supported:
            self.mode.addItem("Host-only", NetworkMode.HOST_ONLY.value)
        if nic is not None and self.mode.findData(nic.mode.value) < 0:
            self.mode.addItem(nic.mode.display_name, nic.mode.value)
        form.addRow("Mode", self.mode)
        self.model = QComboBox()
        self.model.addItem("VirtIO", NicModel.VIRTIO.value)
        self.model.addItem("Intel e1000e", NicModel.E1000E.value)
        form.addRow("Model", self.model)
        mac_row = QHBoxLayout()
        self.mac = QLineEdit()
        self.mac.setPlaceholderText("aa:bb:cc:dd:ee:ff")
        generate = QPushButton("Generate")
        generate.clicked.connect(lambda: self.mac.setText(generate_mac()))
        mac_row.addWidget(self.mac, 1)
        mac_row.addWidget(generate)
        form.addRow("MAC address", mac_row)
        self.bridge = QLineEdit()
        form.addRow("Host bridge", self.bridge)
        layout.addLayout(form)
        self.forwards_group = QGroupBox("Port forwards")
        forwards_layout = QVBoxLayout(self.forwards_group)
        self.forwards_table = QTableWidget(0, 3)
        self.forwards_table.setHorizontalHeaderLabels(["Protocol", "Host port", "Guest port"])
        self.forwards_table.verticalHeader().setVisible(False)
        self.forwards_table.horizontalHeader().setStretchLastSection(True)
        self.forwards_table.setMinimumHeight(110)
        forwards_layout.addWidget(self.forwards_table)
        forward_buttons = QHBoxLayout()
        add_forward = QPushButton("Add forward")
        add_forward.clicked.connect(self._add_forward)
        remove_forward = QPushButton("Remove")
        remove_forward.clicked.connect(self._remove_forward)
        forward_buttons.addWidget(add_forward)
        forward_buttons.addWidget(remove_forward)
        forward_buttons.addStretch(1)
        forwards_layout.addLayout(forward_buttons)
        layout.addWidget(self.forwards_group)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setProperty("role", "muted")
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if nic is not None:
            self.mode.setCurrentIndex(max(0, self.mode.findData(nic.mode.value)))
            self.model.setCurrentIndex(max(0, self.model.findData(nic.model.value)))
            self.mac.setText(nic.mac)
            self.bridge.setText(nic.bridge or "")
            for forward in nic.port_forwards:
                self._add_forward(forward.protocol, forward.host_port, forward.guest_port)
        else:
            self.mac.setText(generate_mac())
        if self.forwards_table.rowCount() == 0:
            self._add_forward("tcp", 2222, 22)
        self.mode.currentIndexChanged.connect(self._update_note)
        self._update_note()

    def _add_forward(self, protocol: str = "tcp", host_port: int = 2222, guest_port: int = 22) -> None:
        row = self.forwards_table.rowCount()
        self.forwards_table.insertRow(row)
        protocol_widget = QComboBox()
        protocol_widget.addItems(["tcp", "udp"])
        protocol_widget.setCurrentText(protocol)
        self.forwards_table.setCellWidget(row, 0, protocol_widget)
        host_widget = QSpinBox()
        host_widget.setRange(1, 65535)
        host_widget.setValue(host_port)
        self.forwards_table.setCellWidget(row, 1, host_widget)
        guest_widget = QSpinBox()
        guest_widget.setRange(1, 65535)
        guest_widget.setValue(guest_port)
        self.forwards_table.setCellWidget(row, 2, guest_widget)

    def _remove_forward(self) -> None:
        row = self.forwards_table.currentRow()
        if row >= 0:
            self.forwards_table.removeRow(row)

    def _port_forwards(self) -> tuple[PortForward, ...]:
        forwards: list[PortForward] = []
        for row in range(self.forwards_table.rowCount()):
            protocol = self.forwards_table.cellWidget(row, 0)
            host = self.forwards_table.cellWidget(row, 1)
            guest = self.forwards_table.cellWidget(row, 2)
            if not isinstance(protocol, QComboBox) or not isinstance(host, QSpinBox) or not isinstance(guest, QSpinBox):
                continue
            forwards.append(
                PortForward(
                    protocol=protocol.currentText(),
                    host_port=host.value(),
                    guest_port=guest.value(),
                )
            )
        return tuple(forwards)

    def _update_note(self) -> None:
        mode = NetworkMode(self.mode.currentData())
        self.bridge.setEnabled(mode is NetworkMode.BRIDGED)
        self.forwards_group.setVisible(mode is NetworkMode.NAT)
        if mode is NetworkMode.NAT:
            self.note.setText("Only forwarded host ports are reachable from outside the guest.")
        elif mode is NetworkMode.BRIDGED:
            self.note.setText("The guest becomes directly reachable on the physical network.")
        else:
            self.note.setText("Host-only networking is not implemented yet.")

    def _accept(self) -> None:
        try:
            self.nic()
        except OpenQVMError as exc:
            QMessageBox.warning(self, "Invalid adapter", exc.message)
            return
        self.accept()

    def nic(self) -> NicSpec:
        from openqvm.core.ids import new_id

        mode = NetworkMode(self.mode.currentData())
        return NicSpec(
            id=self._nic_id or new_id(),
            mode=mode,
            model=NicModel(self.model.currentData()),
            mac=self.mac.text().strip(),
            bridge=self.bridge.text().strip() or None,
            port_forwards=self._port_forwards() if mode is NetworkMode.NAT else (),
        )


class SnapshotDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create snapshot")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. before-upgrade")
        form.addRow("Name", self.name)
        self.description = QLineEdit()
        form.addRow("Description", self.description)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        from openqvm.core.validation import validate_snapshot_name

        issues = validate_snapshot_name(self.name.text().strip())
        if issues:
            QMessageBox.warning(self, "Invalid name", issues[0].message)
            return
        self.accept()

    def values(self) -> tuple[str, str]:
        return self.name.text().strip(), self.description.text().strip()
