"""Embedded guest display using the RFB (VNC) client."""

from __future__ import annotations

import threading

from PySide6.QtCore import QPoint, QRect, Qt, QThread, Signal
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QWheelEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

from openqvm.core.logging import get_logger
from openqvm.vm.rfb import SPECIAL_KEYSYMS, RfbClient, RfbError, keysym_for_char, type_text

log = get_logger("ui.vnc")

_KEY_TO_SPECIAL = {
    Qt.Key.Key_Escape: "escape",
    Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Backtab: "tab",
    Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Return: "return",
    Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Insert: "insert",
    Qt.Key.Key_Delete: "delete",
    Qt.Key.Key_Home: "home",
    Qt.Key.Key_End: "end",
    Qt.Key.Key_PageUp: "pageup",
    Qt.Key.Key_PageDown: "pagedown",
    Qt.Key.Key_Left: "left",
    Qt.Key.Key_Up: "up",
    Qt.Key.Key_Right: "right",
    Qt.Key.Key_Down: "down",
    Qt.Key.Key_Shift: "shift",
    Qt.Key.Key_Control: "control",
    Qt.Key.Key_Alt: "alt",
    Qt.Key.Key_Meta: "meta",
    Qt.Key.Key_CapsLock: "capslock",
    Qt.Key.Key_Pause: "pause",
    Qt.Key.Key_Print: "print",
}
for _index in range(1, 13):
    _KEY_TO_SPECIAL[getattr(Qt.Key, f"Key_F{_index}")] = f"f{_index}"

_PUNCTUATION = {
    Qt.Key.Key_Space: " ",
    Qt.Key.Key_Comma: ",",
    Qt.Key.Key_Period: ".",
    Qt.Key.Key_Slash: "/",
    Qt.Key.Key_Semicolon: ";",
    Qt.Key.Key_Apostrophe: "'",
    Qt.Key.Key_BracketLeft: "[",
    Qt.Key.Key_BracketRight: "]",
    Qt.Key.Key_Backslash: "\\",
    Qt.Key.Key_Minus: "-",
    Qt.Key.Key_Equal: "=",
    Qt.Key.Key_QuoteLeft: "`",
}


def key_to_keysym(event: QKeyEvent) -> int | None:
    special = _KEY_TO_SPECIAL.get(event.key())
    if special:
        return SPECIAL_KEYSYMS[special]
    char = event.text()
    if char:
        symbol = keysym_for_char(char)
        if symbol is not None:
            return symbol
    key = event.key()
    if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
        return ord(chr(key).lower())
    if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
        return ord(chr(key))
    if key in _PUNCTUATION:
        return keysym_for_char(_PUNCTUATION[key])
    return None


class VncWorker(QThread):
    frame_ready = Signal(QImage)
    connected = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, host: str, port: int, parent=None) -> None:
        super().__init__(parent)
        self.host = host
        self.port = port
        self._client: RfbClient | None = None
        self._client_lock = threading.Lock()

    def run(self) -> None:  # noqa: D401 - Qt entry point
        client = RfbClient(self.host, self.port, timeout=60)
        with self._client_lock:
            self._client = client
        try:
            client.connect()
            self.connected.emit(client.desktop_name or f"{self.host}:{self.port}")
            first = True
            while not self.isInterruptionRequested():
                rectangles = client.framebuffer_update(incremental=not first)
                first = False
                if rectangles:
                    image = QImage(
                        client.canvas,
                        client.width,
                        client.height,
                        client.width * 3,
                        QImage.Format.Format_RGB888,
                    ).copy()
                    self.frame_ready.emit(image)
        except RfbError as exc:
            if not self.isInterruptionRequested():
                self.error_occurred.emit(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("VNC worker failed")
            if not self.isInterruptionRequested():
                self.error_occurred.emit(f"Unexpected console error: {exc}")
        finally:
            client.close()
            with self._client_lock:
                self._client = None

    def stop(self) -> None:
        self.requestInterruption()
        with self._client_lock:
            client = self._client
        if client is not None:
            client.close()

    def send_pointer(self, buttons: int, x: int, y: int) -> None:
        with self._client_lock:
            client = self._client
        if client is not None:
            try:
                client.send_pointer(buttons, x, y)
            except RfbError:
                pass

    def send_key(self, keysym: int, down: bool) -> None:
        with self._client_lock:
            client = self._client
        if client is not None:
            try:
                client.send_key(keysym, down)
            except RfbError:
                pass


class VncWidget(QWidget):
    """Scaled, focusable view of a QEMU VNC display."""

    status_changed = Signal(str)

    def __init__(self, host: str, port: int, parent=None) -> None:
        super().__init__(parent)
        self.host = host
        self.port = port
        self._image: QImage | None = None
        self._worker: VncWorker | None = None
        self._buttons = 0
        self._fit = True
        self._status = "Not connected"
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(480, 300)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    @property
    def status(self) -> str:
        return self._status

    def start(self) -> None:
        self.stop()
        self._image = None
        self._set_status(f"Connecting to {self.host}:{self.port}…")
        worker = VncWorker(self.host, self.port, self)
        worker.frame_ready.connect(self._on_frame)
        worker.connected.connect(lambda name: self._set_status(f"Connected — {name}"))
        worker.error_occurred.connect(self._on_error)
        self._worker = worker
        worker.start()

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(3000)
            self._worker = None

    def reconnect(self) -> None:
        self.start()

    def send_ctrl_alt_del(self) -> bool:
        """Send Ctrl+Alt+Del to the guest.

        Windows reserves this combination locally, so the console exposes it as
        an explicit action.  Returns False when no display is connected.
        """

        worker = self._worker
        if worker is None:
            return False
        keys = [SPECIAL_KEYSYMS["control"], SPECIAL_KEYSYMS["alt"], SPECIAL_KEYSYMS["delete"]]
        for keysym in keys:
            worker.send_key(keysym, True)
        for keysym in reversed(keys):
            worker.send_key(keysym, False)
        return True

    def send_text(self, text: str) -> tuple[int, int]:
        """Type *text* into the guest using a US keyboard mapping.

        Returns ``(typed, unsupported)`` counts.  This works without a guest
        agent, unlike VNC clipboard transfer, because every character is sent as
        ordinary key events.
        """

        worker = self._worker
        if worker is None:
            return 0, len(text)
        return type_text(worker, text)

    def set_fit(self, fit: bool) -> None:
        self._fit = fit
        self.update()

    @property
    def has_frame(self) -> bool:
        return self._image is not None

    def screenshot(self) -> QImage | None:
        return self._image

    def _set_status(self, text: str) -> None:
        self._status = text
        self.status_changed.emit(text)
        self.update()

    def _on_frame(self, image: QImage) -> None:
        self._image = image
        self.update()

    def _on_error(self, message: str) -> None:
        self._set_status(message)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        if self._image is None:
            painter.setPen(QColor("#93a4bf"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._status)
            painter.end()
            return
        target = self._image_rect()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(target, self._image)
        painter.end()

    def _image_rect(self) -> QRect:
        assert self._image is not None
        image_width = self._image.width()
        image_height = self._image.height()
        if image_width <= 0 or image_height <= 0:
            return QRect()
        if self._fit:
            scale = min(self.width() / image_width, self.height() / image_height)
        else:
            scale = 1.0
        width = max(1, int(image_width * scale))
        height = max(1, int(image_height * scale))
        x = (self.width() - width) // 2
        y = (self.height() - height) // 2
        return QRect(x, y, width, height)

    def _to_framebuffer(self, position: QPoint) -> tuple[int, int] | None:
        if self._image is None:
            return None
        rect = self._image_rect()
        if not rect.contains(position):
            return None
        x = int((position.x() - rect.x()) * self._image.width() / max(1, rect.width()))
        y = int((position.y() - rect.y()) * self._image.height() / max(1, rect.height()))
        return max(0, min(x, self._image.width() - 1)), max(0, min(y, self._image.height() - 1))

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        button = {
            Qt.MouseButton.LeftButton: 1,
            Qt.MouseButton.MiddleButton: 2,
            Qt.MouseButton.RightButton: 4,
        }.get(event.button())
        if button is None:
            return
        self._buttons |= button
        self._send_pointer(event.position().toPoint())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        button = {
            Qt.MouseButton.LeftButton: 1,
            Qt.MouseButton.MiddleButton: 2,
            Qt.MouseButton.RightButton: 4,
        }.get(event.button())
        if button is None:
            return
        self._buttons &= ~button
        self._send_pointer(event.position().toPoint())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._send_pointer(event.position().toPoint())

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if self._image is None or self._worker is None:
            return
        coordinates = self._to_framebuffer(event.position().toPoint())
        if coordinates is None:
            return
        x, y = coordinates
        button = 8 if event.angleDelta().y() > 0 else 16
        self._worker.send_pointer(self._buttons | button, x, y)
        self._worker.send_pointer(self._buttons, x, y)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.isAutoRepeat():
            return
        keysym = key_to_keysym(event)
        if keysym is not None and self._worker is not None:
            self._worker.send_key(keysym, True)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.isAutoRepeat():
            return
        keysym = key_to_keysym(event)
        if keysym is not None and self._worker is not None:
            self._worker.send_key(keysym, False)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _send_pointer(self, position: QPoint) -> None:
        if self._worker is None:
            return
        coordinates = self._to_framebuffer(position)
        if coordinates is None:
            return
        self._worker.send_pointer(self._buttons, coordinates[0], coordinates[1])
