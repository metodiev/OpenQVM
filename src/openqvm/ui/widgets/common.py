"""Shared widgets and helpers for the OpenQVM user interface."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from openqvm.core.errors import OpenQVMError
from openqvm.core.models import VMStatus
from openqvm.services.metrics import human_bytes
from openqvm.ui.theme import STATUS_COLOURS, rgba


class StatusPill(QLabel):
    def __init__(self, status: VMStatus | str = VMStatus.UNKNOWN, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("StatusPill")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_status(status)

    def set_status(self, status: VMStatus | str) -> None:
        value = status.value if isinstance(status, VMStatus) else str(status)
        colour = STATUS_COLOURS.get(value, STATUS_COLOURS["unknown"])
        label = value.replace("_", " ").title()
        self.setText(label)
        self.setStyleSheet(
            f"background-color: {colour}; color: #0b1220; border-radius: 9px; "
            f"padding: 3px 10px; font-size: 11px; font-weight: 650;"
        )
        self.setToolTip(f"VM status: {label}")


class StatusDelegate(QStyledItemDelegate):
    """Draws a rounded status pill inside a table cell without child widgets."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:  # type: ignore[no-untyped-def]
        value = str(index.data(Qt.ItemDataRole.UserRole) or "unknown")
        colour = STATUS_COLOURS.get(value, STATUS_COLOURS["unknown"])
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(option.rect.adjusted(7, 5, -7, -5))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colour))
        painter.drawRoundedRect(rect, 9, 9)
        painter.setPen(QColor("#0b1220"))
        font = painter.font()
        font.setPointSizeF(max(8.0, font.pointSizeF() - 1.0))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, value.replace("_", " ").title())
        painter.restore()

    def sizeHint(self, option, index):  # type: ignore[no-untyped-def] # noqa: N802
        hint = super().sizeHint(option, index)
        hint.setWidth(max(hint.width(), 88))
        return hint


class Card(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setFrameShape(QFrame.Shape.NoFrame)

    def body(self) -> QVBoxLayout:
        layout = self.layout()
        if layout is None:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(10)
        return layout  # type: ignore[return-value]


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        text_column = QVBoxLayout()
        text_column.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setProperty("role", "h1")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setProperty("role", "muted")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setVisible(bool(subtitle))
        text_column.addWidget(self.title_label)
        text_column.addWidget(self.subtitle_label)
        layout.addLayout(text_column, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        layout.addLayout(self.actions)

    def add_action(self, widget: QWidget) -> None:
        self.actions.addWidget(widget)

    def set_subtitle(self, text: str) -> None:
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))


class Banner(QFrame):
    def __init__(self, severity: str = "warning", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Banner")
        self._severity = severity
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(12, 10, 12, 10)
        self._layout.setSpacing(10)
        self.icon = QLabel()
        self.icon.setFixedWidth(18)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.action = QPushButton("Action")
        self.action.setProperty("variant", "ghost")
        self.action.setVisible(False)
        self._action_connected = False
        self._layout.addWidget(self.icon)
        self._layout.addWidget(self.message, 1)
        self._layout.addWidget(self.action)
        self.setVisible(False)
        self.set_severity(severity)

    def set_severity(self, severity: str) -> None:
        self._severity = severity
        colours = {
            "warning": ("#f59e0b", "!", 0.14),
            "error": ("#ef4444", "!", 0.14),
            "info": ("#38bdf8", "i", 0.14),
            "success": ("#22c55e", "✓", 0.14),
        }
        border, icon, alpha = colours.get(severity, colours["info"])
        background = rgba(border, alpha)
        self.setStyleSheet(
            f"QFrame#Banner {{ background-color: {background}; border: 1px solid {border}; "
            f"border-radius: 9px; }} QLabel {{ background: transparent; }}"
        )
        self.icon.setText(icon)
        self.icon.setStyleSheet(f"color: {border}; font-weight: 800;")

    def show_message(
        self,
        message: str,
        *,
        severity: str = "warning",
        action_text: str | None = None,
        action: Callable[[], None] | None = None,
    ) -> None:
        self.set_severity(severity)
        self.message.setText(message)
        if self._action_connected:
            try:
                self.action.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
            self._action_connected = False
        if action_text and action:
            self.action.setText(action_text)
            self.action.setVisible(True)
            self.action.clicked.connect(action)
            self._action_connected = True
        else:
            self.action.setVisible(False)
        self.setVisible(True)

    def clear(self) -> None:
        self.setVisible(False)


class MetricCard(Card):
    def __init__(self, title: str, value: str = "—", subtitle: str = "", parent=None) -> None:
        super().__init__(parent)
        layout = self.body()
        self.title = QLabel(title)
        self.title.setProperty("role", "muted")
        self.value = QLabel(value)
        self.value.setProperty("role", "metric")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setProperty("role", "muted")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.subtitle)

    def set(self, value: str, subtitle: str | None = None) -> None:
        self.value.setText(value)
        if subtitle is not None:
            self.subtitle.setText(subtitle)


class ScrollPage(QWidget):
    """A vertically scrollable page with a consistent content column."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.container = QWidget()
        self.content = QVBoxLayout(self.container)
        self.content.setContentsMargins(28, 22, 28, 28)
        self.content.setSpacing(18)
        self.scroll.setWidget(self.container)
        outer.addWidget(self.scroll)


class LogTail:
    """Incremental text tail reader that survives file truncation."""

    def __init__(self, path: Path, max_bytes: int = 2 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.position = 0

    def read(self) -> str:
        try:
            size = self.path.stat().st_size
        except OSError:
            return ""
        if size < self.position:
            self.position = 0
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.position)
                data = handle.read(self.max_bytes)
                self.position = handle.tell()
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace")

    def reset(self) -> None:
        self.position = 0


def show_error(parent, error: OpenQVMError) -> None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle("OpenQVM error")
    box.setText(error.message)
    if error.hint:
        box.setInformativeText(error.hint)
    details = {"code": error.code.value}
    if error.details:
        details.update(error.details)
    try:
        box.setDetailedText(json.dumps(details, indent=2, default=str))
    except TypeError:
        box.setDetailedText(str(details))
    box.exec()


def confirm(
    parent,
    title: str,
    text: str,
    *,
    informative: str = "",
    confirm_text: str = "Continue",
    dangerous: bool = False,
) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning if dangerous else QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    accept = box.addButton(confirm_text, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.exec()
    return box.clickedButton() is accept


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def copy_to_clipboard(text: str) -> None:
    QGuiApplication.clipboard().setText(text)


def format_datetime(value: str | None) -> str:
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError):
        return value
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def format_optional(value) -> str:  # type: ignore[no-untyped-def]
    return "—" if value is None else str(value)


def format_bytes(value: int | None) -> str:
    return human_bytes(value)


class ClickableCard(Card):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[no-untyped-def] # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def apply_button_style(button: QPushButton, variant: str) -> None:
    button.setProperty("variant", variant)
    button.style().unpolish(button)
    button.style().polish(button)
