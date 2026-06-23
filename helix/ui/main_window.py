"""HelixMainWindow — the shell: nav + stacked pages, and the bus→UI bridge."""
from __future__ import annotations

from PyQt6.QtCore import QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from helix.domain.events import BuildCreated, BuildIterated
from helix.domain.models import AppKind
from helix.ui.console_view import ConsoleView
from helix.ui.launcher_view import LauncherView
from helix.ui.settings_view import SettingsView
from helix.ui.theme import CYAN, LINE

_CONSOLE, _MENU, _SETTINGS = 0, 1, 2


class HelixMainWindow(QMainWindow):
    # Bridges bus events (published on a worker thread) onto the UI thread via a queued signal.
    _buildSignal = pyqtSignal(object)

    def __init__(self, container) -> None:
        super().__init__()
        self._c = container
        self.setWindowTitle("HELIX")
        self.resize(980, 740)

        central = QWidget()
        central.setObjectName("Console")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_nav())

        self._stack = QStackedWidget()
        self.console = ConsoleView(container.conversation, container.settings)
        self.launcher = LauncherView(container.builds)
        self.settings = SettingsView(container.settings)
        self._stack.addWidget(self.console)  # 0
        self._stack.addWidget(self.launcher)  # 1
        self._stack.addWidget(self.settings)  # 2
        outer.addWidget(self._stack, stretch=1)
        self.setCentralWidget(central)

        # Navigation wiring
        self.console.openSettingsRequested.connect(lambda: self._go(_SETTINGS))
        self.settings.saved.connect(self._on_settings_saved)
        self.launcher.newAppRequested.connect(lambda: self._go(_CONSOLE))
        self.launcher.openSettingsRequested.connect(lambda: self._go(_SETTINGS))
        self.launcher.openAppRequested.connect(self._open_app)

        # Bus → UI bridge (refresh the menu when a build lands)
        container.bus.subscribe(BuildCreated, self._buildSignal.emit)
        container.bus.subscribe(BuildIterated, self._buildSignal.emit)
        self._buildSignal.connect(self._on_build)

    def _build_nav(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet(f"border-bottom:1px solid {LINE};")
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 12, 16, 12)
        brand = QLabel("◉  HELIX")
        brand.setObjectName("Title")
        brand.setStyleSheet(f"color:{CYAN};")
        row.addWidget(brand)
        row.addStretch(1)
        for label, idx in (("◉ Console", _CONSOLE), ("☰ Menu", _MENU), ("⚙ Settings", _SETTINGS)):
            btn = QPushButton(label)
            btn.setObjectName("Nav")
            btn.clicked.connect(lambda _checked=False, i=idx: self._nav(i))
            row.addWidget(btn)
        return bar

    # ----- navigation -----
    def _nav(self, index: int) -> None:
        if index == _MENU:
            self.launcher.refresh()
        self._go(index)

    def _go(self, index: int) -> None:
        self._stack.setCurrentIndex(index)

    def _on_settings_saved(self) -> None:
        self.console.refresh_key_state()
        self._go(_CONSOLE)

    def _on_build(self, _event: object) -> None:
        self.launcher.refresh()

    def _open_app(self, slug: str) -> None:
        app = next((a for a in self._c.builds.list() if a.slug == slug), None)
        if app is None:
            return
        ws = self._c.builds.workspace(slug)
        target = ws / app.entry_point if (app.kind == AppKind.HTML and app.entry_point) else ws
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
