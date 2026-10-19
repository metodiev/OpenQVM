"""Qt application bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from openqvm import PRODUCT_NAME, PRODUCT_VERSION
from openqvm.app.context import AppContext
from openqvm.core.errors import OpenQVMError
from openqvm.core.logging import get_logger
from openqvm.ui.icons import app_icon
from openqvm.ui.main_window import MainWindow
from openqvm.ui.theme import apply_theme

log = get_logger("ui.app")


def run_gui(*, data_dir: Path | None = None, qemu_dir: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(PRODUCT_NAME)
    app.setApplicationDisplayName(PRODUCT_NAME)
    app.setOrganizationName("OpenQVM")
    app.setApplicationVersion(PRODUCT_VERSION)
    app.setWindowIcon(app_icon())

    try:
        context = AppContext.create(data_dir=data_dir, qemu_dir=qemu_dir)
    except OpenQVMError as exc:
        QMessageBox.critical(None, f"{PRODUCT_NAME} could not start", exc.format_for_user(include_details=True))
        return 1

    apply_theme(app, context.settings.theme)

    def exception_hook(exc_type, exc_value, exc_tb) -> None:  # type: ignore[no-untyped-def]
        log.exception("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
        QMessageBox.critical(
            None,
            "Unexpected error",
            f"{exc_value}\n\nThe error was written to {context.paths.app_log}.",
        )

    sys.excepthook = exception_hook
    window = MainWindow(context)
    window.show()
    try:
        return app.exec()
    finally:
        context.close()
