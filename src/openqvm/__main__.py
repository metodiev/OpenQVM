"""Entry point for the graphical application."""

from __future__ import annotations


def main() -> int:
    from openqvm.ui.app import run_gui

    return run_gui()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
