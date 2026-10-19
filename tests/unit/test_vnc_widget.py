"""Console input helpers: typing text and sending Ctrl+Alt+Del."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from openqvm.ui.vnc import VncWidget
from openqvm.vm.rfb import SPECIAL_KEYSYMS


class FakeWorker:
    def __init__(self) -> None:
        self.keys: list[tuple[int, bool]] = []

    def send_key(self, keysym: int, down: bool) -> None:
        self.keys.append((keysym, down))

    def send_pointer(self, buttons: int, x: int, y: int) -> None:  # pragma: no cover - unused
        pass


def _qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_typing_text_sends_shifted_events():
    _qapp()
    widget = VncWidget("127.0.0.1", 1)
    worker = FakeWorker()
    widget._worker = worker  # type: ignore[assignment]
    try:
        typed, unsupported = widget.send_text("Hi!")
        assert (typed, unsupported) == (3, 0)
        shift = SPECIAL_KEYSYMS["shift"]
        assert worker.keys == [
            (ord("H"), True),
            (ord("H"), False),
            (ord("i"), True),
            (ord("i"), False),
            (shift, True),
            (ord("1"), True),
            (ord("1"), False),
            (shift, False),
        ]
    finally:
        widget.deleteLater()


def test_typing_reports_unsupported_characters():
    _qapp()
    widget = VncWidget("127.0.0.1", 1)
    worker = FakeWorker()
    widget._worker = worker  # type: ignore[assignment]
    try:
        typed, unsupported = widget.send_text("ok 😀")
        assert typed == 3  # 'o', 'k', ' '
        assert unsupported == 1
    finally:
        widget.deleteLater()


def test_typing_without_connection_is_a_noop():
    _qapp()
    widget = VncWidget("127.0.0.1", 1)
    try:
        assert widget.send_text("abc") == (0, 3)
        assert widget.send_ctrl_alt_del() is False
    finally:
        widget.deleteLater()


def test_ctrl_alt_del_presses_and_releases_in_order():
    _qapp()
    widget = VncWidget("127.0.0.1", 1)
    worker = FakeWorker()
    widget._worker = worker  # type: ignore[assignment]
    try:
        assert widget.send_ctrl_alt_del() is True
        control = SPECIAL_KEYSYMS["control"]
        alt = SPECIAL_KEYSYMS["alt"]
        delete = SPECIAL_KEYSYMS["delete"]
        assert worker.keys == [
            (control, True),
            (alt, True),
            (delete, True),
            (delete, False),
            (alt, False),
            (control, False),
        ]
    finally:
        widget.deleteLater()
