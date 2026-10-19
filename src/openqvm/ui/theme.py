"""Light and dark themes for the OpenQVM desktop application."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

STATUS_COLOURS = {
    "running": "#22c55e",
    "paused": "#f59e0b",
    "stopped": "#94a3b8",
    "starting": "#38bdf8",
    "stopping": "#f97316",
    "error": "#ef4444",
    "unknown": "#a78bfa",
}


@dataclass(frozen=True)
class ThemeColours:
    background: str
    surface: str
    surface_alt: str
    border: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    danger: str
    success: str
    warning: str


DARK = ThemeColours(
    background="#0b1220",
    surface="#131c2e",
    surface_alt="#1b2740",
    border="#27344d",
    text="#e5edf9",
    muted="#93a4bf",
    accent="#3b82f6",
    accent_hover="#2563eb",
    danger="#ef4444",
    success="#22c55e",
    warning="#f59e0b",
)

LIGHT = ThemeColours(
    background="#f4f6fb",
    surface="#ffffff",
    surface_alt="#eef2f9",
    border="#d7dfec",
    text="#0f172a",
    muted="#5b6b82",
    accent="#2563eb",
    accent_hover="#1d4ed8",
    danger="#dc2626",
    success="#15803d",
    warning="#b45309",
)


def colours(theme: str) -> ThemeColours:
    return LIGHT if theme == "light" else DARK


def rgba(hex_colour: str, alpha: float) -> str:
    value = hex_colour.lstrip("#")
    red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    return f"rgba({red}, {green}, {blue}, {max(0.0, min(1.0, alpha)):.3f})"


def stylesheet(theme: str) -> str:
    palette = colours(theme)
    accent_soft = rgba(palette.accent, 0.14)
    selection_soft = rgba(palette.accent, 0.22)
    return f"""
    QWidget {{
        background-color: {palette.background};
        color: {palette.text};
        font-family: "Segoe UI", "SF Pro Text", "Ubuntu", sans-serif;
        font-size: 13px;
    }}
    QMainWindow, QDialog, QWizard {{ background-color: {palette.background}; }}
    QFrame#Card, QFrame#Panel {{
        background-color: {palette.surface};
        border: 1px solid {palette.border};
        border-radius: 12px;
    }}
    QFrame#Sidebar {{
        background-color: {palette.surface};
        border-right: 1px solid {palette.border};
    }}
    QLabel {{ background: transparent; }}
    QLabel[role="h1"] {{ font-size: 24px; font-weight: 700; }}
    QLabel[role="h2"] {{ font-size: 18px; font-weight: 650; }}
    QLabel[role="h3"] {{ font-size: 15px; font-weight: 650; }}
    QLabel[role="muted"] {{ color: {palette.muted}; }}
    QLabel[role="metric"] {{ font-size: 26px; font-weight: 700; }}
    QLabel[role="error"] {{ color: {palette.danger}; }}
    QLabel[role="warning"] {{ color: {palette.warning}; }}
    QLabel[role="success"] {{ color: {palette.success}; }}
    QLabel#StatusPill {{
        border-radius: 9px;
        padding: 3px 10px;
        font-size: 11px;
        font-weight: 650;
        color: #0b1220;
    }}
    QPushButton {{
        background-color: {palette.surface_alt};
        color: {palette.text};
        border: 1px solid {palette.border};
        border-radius: 8px;
        padding: 7px 14px;
        min-height: 18px;
    }}
    QPushButton:hover {{ border-color: {palette.accent}; }}
    QPushButton:pressed {{ background-color: {palette.border}; }}
    QPushButton:disabled {{ color: {palette.muted}; border-color: {palette.border}; }}
    QPushButton[variant="primary"] {{
        background-color: {palette.accent};
        border-color: {palette.accent};
        color: #ffffff;
        font-weight: 650;
    }}
    QPushButton[variant="primary"]:hover {{ background-color: {palette.accent_hover}; }}
    QPushButton[variant="primary"]:disabled {{
        background-color: {palette.surface_alt};
        border-color: {palette.border};
        color: {palette.muted};
    }}
    QPushButton[variant="danger"] {{
        background-color: transparent;
        border-color: {palette.danger};
        color: {palette.danger};
    }}
    QPushButton[variant="danger"]:hover {{ background-color: {palette.danger}; color: #ffffff; }}
    QPushButton[variant="ghost"] {{
        background: transparent;
        border-color: transparent;
        color: {palette.muted};
        text-align: left;
        padding-left: 12px;
    }}
    QPushButton[variant="ghost"]:hover {{ color: {palette.text}; background-color: {palette.surface_alt}; }}
    QPushButton[variant="nav"] {{
        background: transparent;
        border: none;
        border-radius: 8px;
        padding: 10px 12px;
        text-align: left;
        color: {palette.muted};
        font-weight: 550;
    }}
    QPushButton[variant="nav"]:hover {{ background-color: {palette.surface_alt}; color: {palette.text}; }}
    QPushButton[variant="nav"][active="true"] {{
        background-color: {accent_soft};
        color: {palette.text};
        border-left: 3px solid {palette.accent};
    }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.border};
        border-radius: 7px;
        padding: 7px 9px;
        min-height: 20px;
        selection-background-color: {palette.accent};
        selection-color: #ffffff;
    }}
    QPlainTextEdit, QTextEdit {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.border};
        border-radius: 7px;
        padding: 7px 9px;
        selection-background-color: {palette.accent};
        selection-color: #ffffff;
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
        border-color: {palette.accent};
    }}
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
        color: {palette.muted};
        background-color: {palette.surface_alt};
        border: 1px solid {palette.border};
        border-radius: 7px;
        padding: 7px 9px;
        min-height: 20px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {palette.surface};
        border: 1px solid {palette.border};
        selection-background-color: {palette.accent};
        selection-color: #ffffff;
    }}
    QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
    QTableWidget, QTableView, QTreeWidget, QListWidget {{
        background-color: {palette.surface};
        alternate-background-color: {palette.surface_alt};
        border: 1px solid {palette.border};
        border-radius: 10px;
        gridline-color: {palette.border};
        selection-background-color: {selection_soft};
        selection-color: {palette.text};
    }}
    QHeaderView::section {{
        background-color: {palette.surface_alt};
        color: {palette.muted};
        border: none;
        border-bottom: 1px solid {palette.border};
        padding: 8px;
        font-weight: 650;
    }}
    QTableWidget::item, QTableView::item {{ color: {palette.text}; padding: 3px; }}
    QTabWidget::pane {{
        border: 1px solid {palette.border};
        border-radius: 10px;
        background-color: {palette.surface};
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {palette.muted};
        padding: 9px 16px;
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:selected {{ color: {palette.text}; border-bottom: 2px solid {palette.accent}; }}
    QProgressBar {{
        background-color: {palette.surface_alt};
        border: none;
        border-radius: 5px;
        height: 10px;
        text-align: center;
        font-size: 10px;
        color: transparent;
    }}
    QProgressBar::chunk {{ background-color: {palette.accent}; border-radius: 5px; }}
    QScrollArea {{ border: none; background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {palette.border}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {palette.muted}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {palette.border}; border-radius: 5px; min-width: 30px; }}
    QMenu {{
        background-color: {palette.surface};
        border: 1px solid {palette.border};
        padding: 5px;
    }}
    QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 5px; }}
    QMenu::item:selected {{ background-color: {palette.accent}; color: #ffffff; }}
    QStatusBar {{ background-color: {palette.surface}; border-top: 1px solid {palette.border}; }}
    QToolTip {{
        background-color: {palette.surface_alt};
        color: {palette.text};
        border: 1px solid {palette.border};
        padding: 5px;
    }}
    QWizard {{ background-color: {palette.background}; }}
    QWizardPage {{ background-color: transparent; }}
    QSplitter::handle {{ background-color: {palette.border}; }}
    QGroupBox {{
        border: 1px solid {palette.border};
        border-radius: 10px;
        margin-top: 12px;
        padding-top: 8px;
        font-weight: 650;
    }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; }}
    """


def apply_theme(app: QApplication, theme: str) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    colours_ = colours(theme)
    palette.setColor(QPalette.ColorRole.Window, QColor(colours_.background))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(colours_.text))
    palette.setColor(QPalette.ColorRole.Base, QColor(colours_.surface))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(colours_.surface_alt))
    palette.setColor(QPalette.ColorRole.Text, QColor(colours_.text))
    palette.setColor(QPalette.ColorRole.Button, QColor(colours_.surface_alt))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(colours_.text))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(colours_.accent))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(colours_.surface_alt))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(colours_.text))
    app.setPalette(palette)
    app.setStyleSheet(stylesheet(theme))


def repolish(widget) -> None:  # type: ignore[no-untyped-def]
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()
