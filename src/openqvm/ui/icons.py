"""Application icon drawn programmatically (no binary assets required)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPixmap


def app_icon() -> QIcon:
    pixmap = QPixmap(128, 128)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    gradient = QLinearGradient(QPointF(0, 0), QPointF(128, 128))
    gradient.setColorAt(0.0, QColor("#60a5fa"))
    gradient.setColorAt(1.0, QColor("#2563eb"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(QRectF(6, 6, 116, 116), 26, 26)
    painter.setBrush(QColor("#ffffff"))
    for offset, opacity in ((30, 0.55), (46, 0.78), (62, 1.0)):
        painter.setOpacity(opacity)
        painter.drawRoundedRect(QRectF(30, offset, 68, 22), 7, 7)
    painter.setOpacity(1.0)
    painter.setBrush(QColor("#0b1220"))
    painter.drawEllipse(QRectF(58, 38, 12, 6))
    painter.end()
    return QIcon(pixmap)
