"""HelixMainWindow — the shell: the Presence orb as the window background, nav + pages floating on top."""
from __future__ import annotations

from PyQt6.QtCore import QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from helix.domain.events import BuildCreated, BuildDeleted, BuildIterated
from helix.domain.models import AppKind
from helix.logging_setup import get_logger
from helix.ui.console_view import ConsoleView
from helix.ui.launcher_view import LauncherView
from helix.ui.orb import PresenceOrb
from helix.ui.settings_view import SettingsView
from helix.ui.theme import CYAN, LINE

try:  # the in-app web view needs PyQt6-WebEngine; without it, apps open in the system browser
    from helix.ui.app_viewer import AppViewer
except Exception:  # pragma: no cover - depends on the optional WebEngine dependency
    AppViewer = None

_LOG = get_logger("ui")
_CONSOLE, _MENU, _SETTINGS, _VIEWER = 0, 1, 2, 3


class HelixMainWindow(QMainWindow):
    # Bridges bus events (published on a worker thread) onto the UI thread via a queued signal.
    _buildSignal = pyqtSignal(object)

    def __init__(self, container) -> None:
        super().__init__()
        self._c = container
        self.setWindowTitle("HELIX")
        self.resize(980, 740)

        # The orb is the living background of the whole window.
        self.orb = PresenceOrb()

        # The foreground: nav + pages, all transparent so the orb shows through.
        overlay = QWidget()
        overlay.setObjectName("Overlay")
        ov = QVBoxLayout(overlay)
        ov.setContentsMargins(0, 0, 0, 0)
        ov.setSpacing(0)
        ov.addWidget(self._build_nav())

        self._stack = QStackedWidget()
        self.console = ConsoleView(
            container.conversation, container.settings,
            container.speech_in, container.speech_out, self.orb,
            forge=container.forge,
        )
        self.launcher = LauncherView(container.builds, container.agents, container.tasks)
        self.settings = SettingsView(container.settings)
        self._stack.addWidget(self.console)  # 0
        self._stack.addWidget(self.launcher)  # 1
        self._stack.addWidget(self.settings)  # 2
        # In-app viewer for built HTML apps and 3D models — renders inside HELIX instead of the browser,
        # and is reused so tabs never pile up. None if PyQt6-WebEngine isn't available (browser fallback).
        self._viewer = AppViewer() if AppViewer is not None else None
        self._viewer_target: object | None = None  # the file currently shown, for "open in browser"
        if self._viewer is not None:
            self._viewer.closeRequested.connect(self._close_viewer)
            self._viewer.openExternallyRequested.connect(self._open_current_externally)
            self._stack.addWidget(self._viewer)  # 3
        ov.addWidget(self._stack, stretch=1)

        # Tap-to-talk: the Console handles clicks on its own empty space (the orb glowing behind it),
        # so there's no fragile click-through. Menu/Settings don't, so their empty space is inert.

        # Layer the orb (bottom) under the overlay (top), both filling the window.
        central = QWidget()
        central.setObjectName("Root")
        layers = QStackedLayout(central)
        layers.setStackingMode(QStackedLayout.StackingMode.StackAll)
        layers.addWidget(self.orb)
        layers.addWidget(overlay)
        self.orb.lower()
        self.setCentralWidget(central)

        # Navigation wiring
        self.console.openSettingsRequested.connect(lambda: self._go(_SETTINGS))
        self.console.restartRequested.connect(lambda: self._c.restart())
        self.settings.saved.connect(self._on_settings_saved)
        self.launcher.newAppRequested.connect(lambda: self._go(_CONSOLE))
        self.launcher.openSettingsRequested.connect(lambda: self._go(_SETTINGS))
        self.launcher.openAppRequested.connect(self._open_app)

        # Bus → UI bridge (refresh the menu when a build lands)
        container.bus.subscribe(BuildCreated, self._buildSignal.emit)
        container.bus.subscribe(BuildIterated, self._buildSignal.emit)
        container.bus.subscribe(BuildDeleted, self._buildSignal.emit)  # cleanup after a stopped build
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

    def closeEvent(self, event) -> None:
        # Stop voice (TTS + mic) and any workers BEFORE the window goes, so nothing keeps running
        # after the close. Each step is guarded so a hiccup can't block the close.
        if self._viewer is not None:
            try:
                self._viewer.clear()  # blank the web view so its render process stops cleanly
            except Exception:
                _LOG.exception("viewer teardown failed during close")
        for teardown in (self.console.shutdown, self.launcher.shutdown):
            try:
                teardown()
            except Exception:
                _LOG.exception("shutdown step failed during close")
        super().closeEvent(event)

    def _open_app(self, slug: str) -> None:
        app = next((a for a in self._c.builds.list() if a.slug == slug), None)
        if app is None:
            return
        ws = self._c.builds.workspace(slug)
        if app.kind == AppKind.HTML and app.entry_point:
            target = ws / app.entry_point
            if self._viewer is not None:  # render inside HELIX — no browser tabs
                self._viewer_target = target
                self._viewer.load(target, app.name)
                self._go(_VIEWER)
                return
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))  # fallback: no WebEngine
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ws)))  # non-HTML build: open its folder

    def _close_viewer(self) -> None:
        if self._viewer is not None:
            self._viewer.clear()  # stop the page (animation/audio) and free the GL surface
        self._go(_MENU)

    def _open_current_externally(self) -> None:
        if self._viewer_target is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._viewer_target)))
