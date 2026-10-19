"""Settings, backend diagnostics and data management."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from openqvm import PRODUCT_NAME, PRODUCT_VERSION
from openqvm.app.context import AppContext
from openqvm.core.errors import OpenQVMError
from openqvm.ui.widgets.common import Banner, Card, PageHeader, ScrollPage, confirm, open_folder, show_error


class SettingsPage(ScrollPage):
    theme_changed = Signal(str)
    capabilities_changed = Signal()

    def __init__(self, context: AppContext, parent=None) -> None:
        super().__init__(parent)
        self.context = context
        self._build()

    def _build(self) -> None:
        header = PageHeader("Settings", "Backend configuration, appearance and data management.")
        self.content.addWidget(header)
        self.banner = Banner()
        self.content.addWidget(self.banner)

        backend = Card()
        backend_layout = QVBoxLayout(backend)
        backend_layout.setContentsMargins(16, 16, 16, 16)
        backend_title = QLabel("Virtualization backend")
        backend_title.setProperty("role", "h3")
        backend_layout.addWidget(backend_title)

        directory_row = QHBoxLayout()
        self.qemu_dir = QLineEdit()
        self.qemu_dir.setPlaceholderText("Auto-detect QEMU (PATH and standard install locations)")
        self.qemu_dir.setText(self.context.settings.qemu_dir or "")
        directory_row.addWidget(self.qemu_dir, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_qemu)
        directory_row.addWidget(browse)
        detect = QPushButton("Detect and apply")
        detect.setProperty("variant", "primary")
        detect.clicked.connect(self._redetect)
        directory_row.addWidget(detect)
        backend_layout.addWidget(QLabel("QEMU directory"))
        backend_layout.addLayout(directory_row)
        self.backend_summary = QLabel()
        self.backend_summary.setWordWrap(True)
        self.backend_summary.setTextInteractionFlags(self.backend_summary.textInteractionFlags())
        backend_layout.addWidget(self.backend_summary)
        self.diagnostics = QLabel()
        self.diagnostics.setWordWrap(True)
        self.diagnostics.setProperty("role", "muted")
        backend_layout.addWidget(self.diagnostics)
        self.content.addWidget(backend)

        appearance = Card()
        form = QFormLayout(appearance)
        form.setContentsMargins(16, 16, 16, 16)
        appearance_title = QLabel("Appearance")
        appearance_title.setProperty("role", "h3")
        form.addRow(appearance_title)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Dark", "dark")
        self.theme_combo.addItem("Light", "light")
        index = self.theme_combo.findData(self.context.settings.theme)
        self.theme_combo.setCurrentIndex(max(0, index))
        self.theme_combo.currentIndexChanged.connect(
            lambda _: self.theme_changed.emit(self.theme_combo.currentData())
        )
        form.addRow("Theme", self.theme_combo)
        self.content.addWidget(appearance)

        data_card = Card()
        data_layout = QVBoxLayout(data_card)
        data_layout.setContentsMargins(16, 16, 16, 16)
        data_title = QLabel("Data and recovery")
        data_title.setProperty("role", "h3")
        data_layout.addWidget(data_title)
        self.data_path = QLabel(str(self.context.paths.root))
        self.data_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.data_path.setWordWrap(True)
        data_layout.addWidget(self.data_path)
        data_buttons = QHBoxLayout()
        open_data = QPushButton("Open data folder")
        open_data.clicked.connect(lambda: open_folder(self.context.paths.root))
        open_trash = QPushButton("Open trash")
        open_trash.clicked.connect(lambda: open_folder(self.context.paths.trash_dir))
        purge = QPushButton("Purge trash…")
        purge.setProperty("variant", "danger")
        purge.clicked.connect(self._purge_trash)
        data_buttons.addWidget(open_data)
        data_buttons.addWidget(open_trash)
        data_buttons.addStretch(1)
        data_buttons.addWidget(purge)
        data_layout.addLayout(data_buttons)
        note = QLabel(
            "Deleted virtual machines are moved to the trash rather than erased. Deleting snapshots or "
            "quarantined disks is permanent and secure erase is not guaranteed on SSDs."
        )
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        data_layout.addWidget(note)
        self.content.addWidget(data_card)

        about = Card()
        about_layout = QVBoxLayout(about)
        about_layout.setContentsMargins(16, 16, 16, 16)
        about_title = QLabel("About")
        about_title.setProperty("role", "h3")
        about_layout.addWidget(about_title)
        about_text = QLabel(
            f"{PRODUCT_NAME} {PRODUCT_VERSION}\n"
            "Licensed under the Apache License 2.0. QEMU is a separate project with its own license.\n"
            "This build provides a QEMU backend; platform-specific accelerators are used where available."
        )
        about_text.setWordWrap(True)
        about_layout.addWidget(about_text)
        self.content.addWidget(about)
        self.content.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        capabilities = self.context.capabilities
        if capabilities.qemu:
            text = (
                f"QEMU {capabilities.qemu.version}\n"
                f"System: {capabilities.qemu.system_path}\n"
                f"qemu-img: {capabilities.qemu.image_path}\n"
                f"Accelerator: {capabilities.default_accelerator or 'none'} "
                f"(available: {', '.join(capabilities.accelerator_names) or 'none'})"
            )
        else:
            text = "QEMU was not found. Install it or choose the directory containing qemu-system-x86_64 and qemu-img."
        self.backend_summary.setText(text)
        self.diagnostics.setText("\n".join(f"• {line}" for line in capabilities.diagnostics))
        if not capabilities.backend_available:
            self.banner.show_message(text, severity="error")
        elif not capabilities.uefi:
            self.banner.show_message(
                "UEFI firmware was not found. BIOS servers remain available; install edk2/OVMF for UEFI guests.",
                severity="info",
            )
        else:
            self.banner.clear()

    def _browse_qemu(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose the QEMU directory", str(Path.home()))
        if directory:
            self.qemu_dir.setText(directory)

    def _redetect(self) -> None:
        directory = self.qemu_dir.text().strip() or None
        try:
            self.context.update_settings(qemu_dir=directory)
            self.context.redetect(qemu_dir=directory)
        except OpenQVMError as exc:
            show_error(self, exc)
            return
        self.refresh()
        self.capabilities_changed.emit()
        self.banner.show_message("Backend capabilities were re-detected.", severity="success")

    def _purge_trash(self) -> None:
        if not confirm(
            self,
            "Purge trash",
            "Permanently delete everything in the OpenQVM trash folder?",
            informative="Deleted VMs, disks and snapshots in the trash cannot be recovered after this operation.",
            confirm_text="Purge trash",
            dangerous=True,
        ):
            return
        removed = self.context.manager.purge_trash()
        self.banner.show_message(f"Purged {removed} trash entr{'y' if removed == 1 else 'ies'}.", severity="success")
