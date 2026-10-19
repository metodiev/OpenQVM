"""Virtual machine creation wizard."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from openqvm.app.context import AppContext
from openqvm.core.errors import OpenQVMError, ValidationError, ValidationIssue
from openqvm.core.factory import create_vm_spec
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
    PortForward,
    SharedFolderSpec,
    VMSpec,
)
from openqvm.core.validation import HostResources
from openqvm.services.metrics import human_bytes
from openqvm.storage.iso import IsoInfo, inspect_iso
from openqvm.ui.widgets.common import Banner
from openqvm.vm.manager import VmManager


class _PageId:
    NAME = 0
    RESOURCES = 1
    STORAGE = 2
    NETWORK = 3
    SUMMARY = 4


class CreateVmWizard(QWizard):
    create_requested = Signal(object)
    cancel_requested = Signal()

    def __init__(self, context: AppContext, parent=None) -> None:
        super().__init__(parent)
        self.context = context
        self.manager: VmManager = context.manager
        self.host = HostResources.detect()
        self._submitted = False
        self.setWindowTitle("Create a virtual machine")
        self.setWizardStyle(QWizard.WizardStyle.ClassicStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setButtonText(QWizard.WizardButton.FinishButton, "Create virtual machine")
        self.setMinimumSize(820, 680)
        self.setPage(_PageId.NAME, NamePage(self))
        self.setPage(_PageId.RESOURCES, ResourcesPage(self))
        self.setPage(_PageId.STORAGE, StoragePage(self))
        self.setPage(_PageId.NETWORK, NetworkPage(self))
        self.setPage(_PageId.SUMMARY, SummaryPage(self))
        self.setStartId(_PageId.NAME)
        self.apply_defaults()

    def build_spec(self) -> VMSpec:
        name_page: NamePage = self.page(_PageId.NAME)  # type: ignore[assignment]
        resources: ResourcesPage = self.page(_PageId.RESOURCES)  # type: ignore[assignment]
        storage: StoragePage = self.page(_PageId.STORAGE)  # type: ignore[assignment]
        network: NetworkPage = self.page(_PageId.NETWORK)  # type: ignore[assignment]

        guest_os = GuestOS(name_page.os_combo.currentData())
        bus = DiskBus(storage.bus_combo.currentData())
        firmware = Firmware(resources.firmware_combo.currentData())
        machine = "pc" if bus is DiskBus.IDE else "q35"
        if firmware is Firmware.UEFI and machine == "pc":
            machine = "q35"
            bus = DiskBus.VIRTIO
        mode = NetworkMode(network.mode_combo.currentData()) if network.enable_network.isChecked() else None
        forwards = network.port_forwards() if mode is NetworkMode.NAT else []
        spec = create_vm_spec(
            name=name_page.name_edit.text().strip(),
            guest_os=guest_os,
            host=self.host,
            vcpus=resources.cpu_spin.value(),
            memory_mib=resources.memory_spin.value(),
            disk_bytes=storage.disk_size_spin.value() * 1024**3,
            disk_format=DiskFormat(storage.format_combo.currentData()),
            disk_bus=bus,
            preallocated=storage.preallocate.isChecked(),
            firmware=firmware,
            machine=machine,
            cdrom_iso=storage.iso_path or None,
            network_mode=mode,
            nic_model=NicModel(network.model_combo.currentData()),
            bridge=network.bridge_edit.text().strip() or None,
            port_forwards=forwards,
            description=name_page.description_edit.toPlainText().strip(),
        )
        spec.cpu = CpuSpec(cores=resources.cpu_spin.value())
        spec.display = DisplaySpec(adapter=GraphicsAdapter(resources.graphics_combo.currentData()))
        spec.shared_folders = storage.shared_folders()
        return spec

    def validate_spec(self) -> tuple[VMSpec | None, list[ValidationIssue]]:
        try:
            spec = self.build_spec()
        except OpenQVMError as exc:
            return None, [ValidationIssue("configuration", exc.format_for_user())]
        return spec, self.manager.validate_spec(spec)

    def accept(self) -> None:  # noqa: D401 - Qt override
        if self._submitted:
            return
        spec, issues = self.validate_spec()
        errors = [issue for issue in issues if issue.is_error]
        if spec is None or errors:
            summary: SummaryPage = self.page(_PageId.SUMMARY)  # type: ignore[assignment]
            summary.show_validation(spec, issues)
            self.setCurrentId(_PageId.SUMMARY)
            return
        self._submitted = True
        self.button(QWizard.WizardButton.FinishButton).setEnabled(False)
        self.button(QWizard.WizardButton.BackButton).setEnabled(False)
        summary: SummaryPage = self.page(_PageId.SUMMARY)  # type: ignore[assignment]
        summary.show_message("Creating the virtual machine… choose Cancel to stop.", severity="info")
        self.create_requested.emit(spec)

    def reject(self) -> None:  # noqa: N802 - Qt override
        """Cancel closes the wizard; while a creation is running it cancels the work instead."""

        if self._submitted:
            self.cancel_requested.emit()
            return
        super().reject()

    def creation_succeeded(self) -> None:
        super().accept()

    def creation_cancelling(self) -> None:
        summary: SummaryPage = self.page(_PageId.SUMMARY)  # type: ignore[assignment]
        summary.show_message("Cancelling creation…", severity="info")

    def creation_failed(self, error: OpenQVMError) -> None:
        self._submitted = False
        self.button(QWizard.WizardButton.FinishButton).setEnabled(True)
        self.button(QWizard.WizardButton.BackButton).setEnabled(True)
        summary: SummaryPage = self.page(_PageId.SUMMARY)  # type: ignore[assignment]
        summary.show_error(error)

    def defaults(self):
        from openqvm.core.defaults import GuestDefaults, recommended_defaults

        guest = GuestOS(self.page(_PageId.NAME).os_combo.currentData())  # type: ignore[attr-defined]
        defaults: GuestDefaults = recommended_defaults(guest, self.host)
        return defaults

    def apply_defaults(self) -> None:
        resources: ResourcesPage = self.page(_PageId.RESOURCES)  # type: ignore[assignment]
        storage: StoragePage = self.page(_PageId.STORAGE)  # type: ignore[assignment]
        defaults = self.defaults()
        resources.cpu_spin.setValue(defaults.vcpus)
        resources.memory_spin.setValue(defaults.memory_mib)
        index = resources.firmware_combo.findData(defaults.firmware.value)
        resources.firmware_combo.setCurrentIndex(max(0, index))
        storage.disk_size_spin.setValue(max(1, int(defaults.disk_bytes // 1024**3)))
        index = storage.bus_combo.findData(defaults.disk_bus.value)
        storage.bus_combo.setCurrentIndex(max(0, index))


class _WizardPage(QWizardPage):
    """A wizard page with OpenQVM-styled header labels.

    Qt's built-in wizard header paints an unthemed light band on Windows, so the
    native title area is suppressed and equivalent labels are rendered in the
    page itself.
    """

    def __init__(self) -> None:
        super().__init__()
        self._title_text = ("", "")
        self._content_layout: QVBoxLayout | None = None
        self._title_label: QLabel | None = None
        self._subtitle_label: QLabel | None = None

    def wizard_ref(self) -> CreateVmWizard:
        return self.wizard()  # type: ignore[return-value]

    def set_title(self, title: str, subtitle: str) -> None:
        self._title_text = (title, subtitle)
        QWizardPage.setTitle(self, "")
        QWizardPage.setSubTitle(self, "")
        if self._title_label is not None:
            self._title_label.setText(title)
        if self._subtitle_label is not None:
            self._subtitle_label.setText(subtitle)

    def content_layout(self) -> QVBoxLayout:
        if self._content_layout is None:
            outer = QVBoxLayout(self)
            outer.setContentsMargins(24, 18, 24, 12)
            outer.setSpacing(6)
            self._title_label = QLabel(self._title_text[0])
            self._title_label.setProperty("role", "h2")
            self._title_label.setWordWrap(True)
            self._subtitle_label = QLabel(self._title_text[1])
            self._subtitle_label.setProperty("role", "muted")
            self._subtitle_label.setWordWrap(True)
            outer.addWidget(self._title_label)
            outer.addWidget(self._subtitle_label)
            outer.addSpacing(8)
            self._content_layout = QVBoxLayout()
            self._content_layout.setSpacing(12)
            outer.addLayout(self._content_layout, 1)
        return self._content_layout


class NamePage(_WizardPage):
    def __init__(self, wizard: CreateVmWizard) -> None:
        super().__init__()
        self.set_title("Name and guest operating system", "Choose a descriptive name and the OS you plan to install.")
        layout = self.content_layout()
        form = QFormLayout()
        form.setSpacing(12)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Ubuntu Workstation")
        self.name_edit.setMaxLength(64)
        self.name_edit.textChanged.connect(self.completeChanged)
        form.addRow("Name", self.name_edit)
        self.os_combo = QComboBox()
        for guest in GuestOS:
            self.os_combo.addItem(guest.display_name, guest.value)
        self.os_combo.currentIndexChanged.connect(self._os_changed)
        form.addRow("Guest operating system", self.os_combo)
        layout.addLayout(form)
        self.description_edit = QPlainTextEdit()
        self.description_edit.setPlaceholderText("Optional notes about this virtual machine")
        self.description_edit.setMaximumHeight(110)
        layout.addWidget(QLabel("Description"))
        layout.addWidget(self.description_edit)
        self.note = QLabel(
            "OpenQVM does not bundle operating system images. Provide your own legally obtained ISO "
            "in the storage step."
        )
        self.note.setProperty("role", "muted")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        layout.addStretch(1)

    def _os_changed(self) -> None:
        self.wizard_ref().apply_defaults()

    def isComplete(self) -> bool:  # noqa: N802
        return bool(self.name_edit.text().strip())

    def validatePage(self) -> bool:  # noqa: N802
        from openqvm.core.validation import validate_vm_name

        issues = validate_vm_name(self.name_edit.text().strip())
        if issues:
            self.wizard().setSubTitle(" ".join(issue.message for issue in issues))
            return False
        return True


class ResourcesPage(_WizardPage):
    def __init__(self, wizard: CreateVmWizard) -> None:
        super().__init__()
        self.set_title("Compute resources", "Defaults are derived from this host and can be adjusted.")
        layout = self.content_layout()
        host = wizard.host
        host_label = QLabel(
            f"Host: {host.cpu_count} logical processors, "
            f"{human_bytes(host.memory_total_mib * 1024**2) if host.memory_total_mib else 'memory unknown'}."
        )
        host_label.setProperty("role", "muted")
        layout.addWidget(host_label)

        form = QFormLayout()
        form.setSpacing(12)
        self.cpu_spin = QSpinBox()
        self.cpu_spin.setRange(1, max(2, host.cpu_count * 2))
        self.cpu_spin.setSuffix(" vCPU")
        form.addRow("Processors", self.cpu_spin)
        self.memory_spin = QSpinBox()
        maximum = max(512, int((host.memory_total_mib or 4096) * 0.9))
        self.memory_spin.setRange(128, maximum)
        self.memory_spin.setSingleStep(256)
        self.memory_spin.setSuffix(" MiB")
        form.addRow("Memory", self.memory_spin)
        self.firmware_combo = QComboBox()
        self.firmware_combo.addItem("BIOS (legacy)", Firmware.BIOS.value)
        if wizard.context.capabilities.uefi is not None:
            self.firmware_combo.addItem("UEFI (edk2/OVMF)", Firmware.UEFI.value)
        form.addRow("Firmware", self.firmware_combo)
        self.graphics_combo = QComboBox()
        self.graphics_combo.addItem("Standard VGA", GraphicsAdapter.STD.value)
        self.graphics_combo.addItem("VirtIO GPU (requires guest drivers)", GraphicsAdapter.VIRTIO.value)
        form.addRow("Display adapter", self.graphics_combo)
        layout.addLayout(form)

        warning = QLabel(
            "Windows 11 requires UEFI and a TPM 2.0 device. This build does not provide a TPM, so the "
            "Windows 11 installer may refuse to continue."
        )
        warning.setWordWrap(True)
        warning.setProperty("role", "warning")
        layout.addWidget(warning)
        layout.addStretch(1)


class StoragePage(_WizardPage):
    def __init__(self, wizard: CreateVmWizard) -> None:
        super().__init__()
        self.set_title("Storage and installation image", "Create the system disk and attach an installer if needed.")
        self._iso_info: IsoInfo | None = None
        self.iso_path: str | None = None
        layout = self.content_layout()
        layout.setSpacing(12)

        disk_group = QGroupBox("System disk")
        disk_form = QFormLayout(disk_group)
        self.disk_size_spin = QSpinBox()
        self.disk_size_spin.setRange(1, 16384)
        self.disk_size_spin.setSuffix(" GiB")
        disk_form.addRow("Size", self.disk_size_spin)
        self.format_combo = QComboBox()
        self.format_combo.addItem("qcow2 (snapshots supported)", DiskFormat.QCOW2.value)
        self.format_combo.addItem("raw (no snapshots)", DiskFormat.RAW.value)
        self.format_combo.currentIndexChanged.connect(self._format_changed)
        disk_form.addRow("Format", self.format_combo)
        self.bus_combo = QComboBox()
        self.bus_combo.addItem("VirtIO (fast, requires guest drivers)", DiskBus.VIRTIO.value)
        self.bus_combo.addItem("IDE (compatible, slower)", DiskBus.IDE.value)
        disk_form.addRow("Bus", self.bus_combo)
        self.preallocate = QCheckBox("Allocate the full disk size now (slower, uses more host space)")
        disk_form.addRow("", self.preallocate)
        layout.addWidget(disk_group)

        iso_group = QGroupBox("Installation image (optional)")
        iso_layout = QVBoxLayout(iso_group)
        row = QHBoxLayout()
        self.iso_edit = QLineEdit()
        self.iso_edit.setReadOnly(True)
        self.iso_edit.setPlaceholderText("No image attached")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear)
        row.addWidget(self.iso_edit, 1)
        row.addWidget(browse)
        row.addWidget(clear)
        iso_layout.addLayout(row)
        self.iso_message = QLabel("An ISO is not required now; you can attach one later while the VM is stopped.")
        self.iso_message.setWordWrap(True)
        self.iso_message.setProperty("role", "muted")
        iso_layout.addWidget(self.iso_message)
        layout.addWidget(iso_group)

        shared_group = QGroupBox("Shared folders")
        shared_layout = QVBoxLayout(shared_group)
        capabilities = wizard.context.capabilities
        self.shared_table = QTableWidget(0, 3)
        self.shared_table.setHorizontalHeaderLabels(["Tag", "Host path", "Read-only"])
        self.shared_table.verticalHeader().setVisible(False)
        self.shared_table.horizontalHeader().setStretchLastSection(True)
        self.shared_table.setMinimumHeight(110)
        shared_layout.addWidget(self.shared_table)
        shared_buttons = QHBoxLayout()
        add_shared = QPushButton("Add folder…")
        add_shared.setEnabled(capabilities.shared_folders_supported)
        add_shared.clicked.connect(self._add_shared)
        remove_shared = QPushButton("Remove")
        remove_shared.setEnabled(capabilities.shared_folders_supported)
        remove_shared.clicked.connect(self._remove_shared)
        shared_buttons.addWidget(add_shared)
        shared_buttons.addWidget(remove_shared)
        shared_buttons.addStretch(1)
        shared_layout.addLayout(shared_buttons)
        note = QLabel(capabilities.shared_folders_detail)
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        shared_layout.addWidget(note)
        layout.addWidget(shared_group)
        layout.addStretch(1)

    def _format_changed(self) -> None:
        if self.format_combo.currentData() == DiskFormat.RAW.value:
            self.iso_message.setToolTip("RAW disks do not support snapshots.")
        self.completeChanged.emit()

    def _browse(self) -> None:
        start = str(self.wizard_ref().context.paths.isos_dir)
        path, _ = QFileDialog.getOpenFileName(self, "Select installation image", start, "Disk images (*.iso *.img)")
        if not path:
            return
        try:
            info = inspect_iso(path)
        except ValidationError as exc:
            self.iso_path = None
            self.iso_edit.clear()
            self.iso_message.setText(str(exc))
            self.iso_message.setProperty("role", "error")
            self.iso_message.style().unpolish(self.iso_message)
            self.iso_message.style().polish(self.iso_message)
            return
        self.iso_path = str(info.path)
        self.iso_edit.setText(self.iso_path)
        self._iso_info = info
        if info.is_iso9660:
            self.iso_message.setText(
                f"ISO-9660 image, volume '{info.volume_id or 'unnamed'}', {human_bytes(info.size_bytes)}."
            )
            self.iso_message.setProperty("role", "muted")
        else:
            self.iso_message.setText(
                "The file does not contain an ISO-9660 volume descriptor. It may still be bootable; verify the image."
            )
            self.iso_message.setProperty("role", "warning")
        self.iso_message.style().unpolish(self.iso_message)
        self.iso_message.style().polish(self.iso_message)
        self.completeChanged.emit()

    def _clear(self) -> None:
        self.iso_path = None
        self._iso_info = None
        self.iso_edit.clear()
        self.iso_message.setText("No image attached. You can attach one later while the VM is stopped.")
        self.iso_message.setProperty("role", "muted")
        self.iso_message.style().unpolish(self.iso_message)
        self.iso_message.style().polish(self.iso_message)

    def _add_shared(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a folder to share")
        if not path:
            return
        row = self.shared_table.rowCount()
        self.shared_table.insertRow(row)
        tag = f"share{row + 1}"
        self.shared_table.setItem(row, 0, QTableWidgetItem(tag))
        self.shared_table.setItem(row, 1, QTableWidgetItem(path))
        readonly = QCheckBox()
        readonly.setChecked(True)
        self.shared_table.setCellWidget(row, 2, readonly)

    def _remove_shared(self) -> None:
        row = self.shared_table.currentRow()
        if row >= 0:
            self.shared_table.removeRow(row)

    def shared_folders(self) -> list[SharedFolderSpec]:
        folders: list[SharedFolderSpec] = []
        for row in range(self.shared_table.rowCount()):
            tag_item = self.shared_table.item(row, 0)
            path_item = self.shared_table.item(row, 1)
            if tag_item is None or path_item is None:
                continue
            widget = self.shared_table.cellWidget(row, 2)
            readonly = widget.isChecked() if isinstance(widget, QCheckBox) else True
            folders.append(SharedFolderSpec(path=path_item.text(), tag=tag_item.text(), readonly=readonly))
        return folders


class NetworkPage(_WizardPage):
    def __init__(self, wizard: CreateVmWizard) -> None:
        super().__init__()
        self.set_title("Networking", "NAT keeps the guest isolated while allowing explicit port forwards.")
        capabilities = wizard.context.capabilities
        layout = self.content_layout()
        layout.setSpacing(12)
        self.enable_network = QCheckBox("Attach a virtual network adapter")
        self.enable_network.setChecked(True)
        self.enable_network.toggled.connect(self._sync_enabled)
        layout.addWidget(self.enable_network)

        form = QFormLayout()
        form.setSpacing(10)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("NAT (user-mode)", NetworkMode.NAT.value)
        if capabilities.bridged_supported:
            self.mode_combo.addItem("Bridged", NetworkMode.BRIDGED.value)
        if capabilities.host_only_supported:
            self.mode_combo.addItem("Host-only", NetworkMode.HOST_ONLY.value)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        form.addRow("Mode", self.mode_combo)
        self.model_combo = QComboBox()
        self.model_combo.addItem("VirtIO (recommended for Linux)", NicModel.VIRTIO.value)
        self.model_combo.addItem("Intel e1000e (recommended for Windows)", NicModel.E1000E.value)
        form.addRow("Adapter model", self.model_combo)
        self.mac_edit = QLineEdit()
        self.mac_edit.setPlaceholderText("Generated automatically")
        generate = QPushButton("Generate")
        generate.clicked.connect(self._generate_mac)
        mac_row = QHBoxLayout()
        mac_row.addWidget(self.mac_edit, 1)
        mac_row.addWidget(generate)
        form.addRow("MAC address", mac_row)
        self.bridge_edit = QLineEdit()
        self.bridge_edit.setPlaceholderText("Host bridge name, e.g. br0")
        form.addRow("Host bridge", self.bridge_edit)
        layout.addLayout(form)

        self.security_note = QLabel("")
        self.security_note.setWordWrap(True)
        self.security_note.setProperty("role", "muted")
        layout.addWidget(self.security_note)

        self.forwards_group = QGroupBox("Port forwards")
        forwards_layout = QVBoxLayout(self.forwards_group)
        self.forwards_table = QTableWidget(0, 3)
        self.forwards_table.setHorizontalHeaderLabels(["Protocol", "Host port", "Guest port"])
        self.forwards_table.verticalHeader().setVisible(False)
        self.forwards_table.horizontalHeader().setStretchLastSection(True)
        self.forwards_table.setMinimumHeight(110)
        forwards_layout.addWidget(self.forwards_table)
        buttons = QHBoxLayout()
        add = QPushButton("Add forward")
        add.clicked.connect(self._add_forward)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_forward)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        forwards_layout.addLayout(buttons)
        layout.addWidget(self.forwards_group)
        layout.addStretch(1)
        if not capabilities.bridged_supported:
            note = QLabel(f"Bridged networking unavailable: {capabilities.bridged_detail}")
            note.setWordWrap(True)
            note.setProperty("role", "muted")
            layout.addWidget(note)
        self._add_forward()
        self._mode_changed()

    def _generate_mac(self) -> None:
        from openqvm.core.models import generate_mac

        self.mac_edit.setText(generate_mac())

    def _sync_enabled(self) -> None:
        enabled = self.enable_network.isChecked()
        for widget in (self.mode_combo, self.model_combo, self.mac_edit, self.forwards_group):
            widget.setEnabled(enabled)
        self._mode_changed()

    def _mode_changed(self) -> None:
        mode = NetworkMode(self.mode_combo.currentData())
        self.bridge_edit.setEnabled(mode is NetworkMode.BRIDGED)
        self.forwards_group.setVisible(mode is NetworkMode.NAT)
        if mode is NetworkMode.NAT:
            self.security_note.setText(
                "NAT isolates the guest behind QEMU's user-mode network. Only the host ports you forward are reachable."
            )
        elif mode is NetworkMode.BRIDGED:
            self.security_note.setText(
                "Bridged mode places the guest on your physical network. It can reach other hosts and be reached by them."
            )
        else:
            self.security_note.setText("Host-only networking is not implemented yet.")

    def _add_forward(self) -> None:
        row = self.forwards_table.rowCount()
        self.forwards_table.insertRow(row)
        protocol = QComboBox()
        protocol.addItems(["tcp", "udp"])
        self.forwards_table.setCellWidget(row, 0, protocol)
        host_port = QSpinBox()
        host_port.setRange(1, 65535)
        host_port.setValue(2222 + row)
        self.forwards_table.setCellWidget(row, 1, host_port)
        guest_port = QSpinBox()
        guest_port.setRange(1, 65535)
        guest_port.setValue(22)
        self.forwards_table.setCellWidget(row, 2, guest_port)

    def _remove_forward(self) -> None:
        row = self.forwards_table.currentRow()
        if row >= 0:
            self.forwards_table.removeRow(row)

    def port_forwards(self) -> list[PortForward]:
        forwards: list[PortForward] = []
        for row in range(self.forwards_table.rowCount()):
            protocol_widget = self.forwards_table.cellWidget(row, 0)
            host_widget = self.forwards_table.cellWidget(row, 1)
            guest_widget = self.forwards_table.cellWidget(row, 2)
            if not all(isinstance(widget, (QComboBox, QSpinBox)) for widget in (protocol_widget, host_widget, guest_widget)):
                continue
            forwards.append(
                PortForward(
                    protocol=protocol_widget.currentText(),
                    host_port=host_widget.value(),
                    guest_port=guest_widget.value(),
                )
            )
        return forwards


class SummaryPage(_WizardPage):
    def __init__(self, wizard: CreateVmWizard) -> None:
        super().__init__()
        self.setTitle("Review and create")
        self.setSubTitle("Verify the configuration. Errors must be fixed before creation.")
        self._spec: VMSpec | None = None
        layout = self.content_layout()
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.details)
        self.banner = Banner()
        layout.addWidget(self.banner)
        self.issues = QLabel()
        self.issues.setWordWrap(True)
        self.issues.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.issues)
        layout.addStretch(1)

    def initializePage(self) -> None:  # noqa: N802
        self.refresh()

    def refresh(self) -> None:
        spec, issues = self.wizard_ref().validate_spec()
        self._spec = spec
        self.show_validation(spec, issues)

    def show_validation(self, spec: VMSpec | None, issues: list) -> None:
        if spec is not None:
            disk = spec.system_disk
            forwards = [
                f"{forward.protocol} {forward.host_address}:{forward.host_port} → guest {forward.guest_port}"
                for nic in spec.nics
                for forward in nic.port_forwards
            ]
            network = spec.nics[0].mode.display_name if spec.nics else "None"
            if spec.nics:
                network += f" ({spec.nics[0].model.display_name}, MAC {spec.nics[0].mac})"
            self.details.setText(
                "<table cellspacing='6'>"
                f"<tr><td><b>Name</b></td><td>{_escape(spec.name)}</td></tr>"
                f"<tr><td><b>Guest OS</b></td><td>{_escape(spec.guest_os.display_name)}</td></tr>"
                f"<tr><td><b>vCPU / memory</b></td><td>{spec.cpu.vcpus} vCPU, {spec.memory_mib} MiB</td></tr>"
                f"<tr><td><b>Firmware</b></td><td>{spec.firmware.display_name} / {_escape(spec.machine)}</td></tr>"
                f"<tr><td><b>Disk</b></td><td>{human_bytes(disk.size_bytes) if disk else '—'}, "
                f"{disk.format.value if disk else ''}, {disk.bus.display_name if disk else ''}</td></tr>"
                f"<tr><td><b>ISO</b></td><td>{_escape(spec.cdrom_iso or 'None')}</td></tr>"
                f"<tr><td><b>Network</b></td><td>{_escape(network)}</td></tr>"
                f"<tr><td><b>Port forwards</b></td><td>{_escape(', '.join(forwards) or 'None')}</td></tr>"
                f"<tr><td><b>Shared folders</b></td><td>{len(spec.shared_folders)}</td></tr>"
                "</table>"
            )
        else:
            self.details.setText("The configuration is incomplete.")
        errors = [issue for issue in issues if issue.is_error]
        warnings = [issue for issue in issues if not issue.is_error]
        if errors:
            self.banner.show_message(
                "<br>".join(_escape(issue.as_text()) for issue in errors),
                severity="error",
            )
        elif warnings:
            self.banner.show_message(
                "<br>".join(_escape(issue.as_text()) for issue in warnings),
                severity="warning",
            )
        else:
            self.banner.clear()
        self.issues.setText("")

    def show_error(self, error: OpenQVMError) -> None:
        self.banner.show_message(_escape(error.message), severity="error")

    def show_message(self, message: str, *, severity: str = "info") -> None:
        self.banner.show_message(_escape(message), severity=severity)

    def isComplete(self) -> bool:  # noqa: N802
        if self._spec is None:
            self.refresh()
        if self._spec is None:
            return False
        errors = [issue for issue in self.wizard_ref().manager.validate_spec(self._spec) if issue.is_error]
        return not errors


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )
