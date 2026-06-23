"""Bootstrap — build the container, create the window, run the Qt event loop."""
from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from helix.app.container import Container
from helix.ui.main_window import HelixMainWindow
from helix.ui.theme import apply_theme


def run_app(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("HELIX")
    apply_theme(app)

    container = Container()
    window = HelixMainWindow(container)
    window.show()
    return app.exec()
