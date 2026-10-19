"""Entry point for the graphical application."""

from __future__ import annotations

import sys
from pathlib import Path


def _self_test() -> int:
    """Construct the main window with Qt's offscreen platform and exit.

    Used to verify a packaged binary: Qt plugins, PySide6, resources and the
    composition root all have to work before the window can be built.
    """

    import os
    import tempfile
    from pathlib import Path

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from openqvm import PRODUCT_NAME
    from openqvm.app.context import AppContext
    from openqvm.ui.main_window import MainWindow
    from openqvm.ui.theme import apply_theme

    data_dir = Path(tempfile.mkdtemp(prefix="openqvm-self-test-"))
    app = QApplication.instance() or QApplication([])
    context = AppContext.create(data_dir=data_dir)
    try:
        apply_theme(app, context.settings.theme)
        window = MainWindow(context)
        window.show()
        app.processEvents()
        window.close()
        app.processEvents()
    finally:
        context.close()
    print(f"{PRODUCT_NAME} self-test OK")
    return 0


def _gui_arguments(argv: list[str]) -> tuple[Path | None, str | None]:
    """Pick ``--data-dir``/``--qemu-dir`` out of the GUI command line."""

    data_dir: Path | None = None
    qemu_dir: str | None = None
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--data-dir" and index + 1 < len(argv):
            data_dir = Path(argv[index + 1])
            index += 2
            continue
        if argument == "--qemu-dir" and index + 1 < len(argv):
            qemu_dir = argv[index + 1]
            index += 2
            continue
        index += 1
    return data_dir, qemu_dir


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in args:
        return _self_test()
    if "--version" in args:
        from openqvm import PRODUCT_NAME, PRODUCT_VERSION

        print(f"{PRODUCT_NAME} {PRODUCT_VERSION}")
        return 0

    from openqvm.ui.app import run_gui

    data_dir, qemu_dir = _gui_arguments(args)
    return run_gui(data_dir=data_dir, qemu_dir=qemu_dir)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
