"""HelixMainWindow — the shell: the Presence orb as the window background, nav + pages floating on top."""
from __future__ import annotations

from PyQt6.QtCore import QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from helix.domain.events import (
    AgentsChanged,
    BuildCreated,
    BuildDeleted,
    BuildDeleteRequested,
    BuildFinished,
    BuildIterated,
    BuildProgress,
    BuildRenamed,
    SelfChangeFinished,
    SelfChangeProgress,
)
from helix.domain.models import AppKind
from helix.logging_setup import get_logger
from helix.ui.console_view import ConsoleView
from helix.ui.launcher_view import LauncherView
from helix.ui.orb import PresenceOrb
from helix.ui.settings_view import SettingsView
from helix.ui.shader_orb import ShaderOrb
from helix.ui.theme import CYAN, LINE

try:  # the in-app web view needs PyQt6-WebEngine; without it, apps open in the system browser
    from helix.ui.app_viewer import AppViewer
except Exception:  # pragma: no cover - depends on the optional WebEngine dependency
    AppViewer = None

_LOG = get_logger("ui")
_CONSOLE, _MENU, _SETTINGS, _VIEWER = 0, 1, 2, 3


class HelixMainWindow(QMainWindow):
    # Bridges bus events (published on a worker/queue thread) onto the UI thread via queued signals.
    _buildSignal = pyqtSignal(object)
    _buildProgressSignal = pyqtSignal(object)
    _buildFinishedSignal = pyqtSignal(object)
    _deleteRequestSignal = pyqtSignal(object)
    _selfChangeProgressSignal = pyqtSignal(object)
    _selfChangeFinishedSignal = pyqtSignal(object)

    def __init__(self, container) -> None:
        super().__init__()
        self._c = container
        self.setWindowTitle("HELIX")
        self.resize(980, 740)

        # The orb is the living background of the whole window. Default is the dark QPainter PresenceOrb
        # (audio-reactive, on-brand). The GPU-shader ShaderOrb is OPT-IN (shader_orb=true) because a
        # transparent QWebEngine background isn't reliable across GPUs/the frozen build — when it fails it
        # paints an opaque (white) rectangle as the window background, which the transparent overlays then
        # show through, washing the whole app out. Keep it behind the flag until its transparency is
        # verified live on this machine.
        use_shader = bool(container.settings.get("shader_orb", False))
        self.orb = ShaderOrb() if use_shader else PresenceOrb()

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
            forge=container.forge, build_queue=container.build_queue,
            selfdev_lane=container.selfdev_lane,
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
        self._viewer_slug: str | None = None  # the build currently open in the viewer (for live reload)
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
        self.console.restartRequested.connect(self._on_restart_requested)
        self.settings.saved.connect(self._on_settings_saved)
        self.launcher.newAppRequested.connect(lambda: self._go(_CONSOLE))
        self.launcher.openSettingsRequested.connect(lambda: self._go(_SETTINGS))
        self.launcher.openAppRequested.connect(self._open_app)

        # Bus → UI bridge (refresh the menu when a build lands)
        container.bus.subscribe(BuildCreated, self._buildSignal.emit)
        container.bus.subscribe(BuildIterated, self._buildSignal.emit)
        container.bus.subscribe(BuildRenamed, self._buildSignal.emit)  # orb-driven rename refreshes menu
        container.bus.subscribe(BuildDeleted, self._buildSignal.emit)  # cleanup after a stopped build
        container.bus.subscribe(AgentsChanged, self._buildSignal.emit)  # orb-driven agent change refreshes
        self._buildSignal.connect(self._on_build)
        # A delete the model proposed → one real human confirmation in the Console before anything is removed.
        container.bus.subscribe(BuildDeleteRequested, self._deleteRequestSignal.emit)
        self._deleteRequestSignal.connect(self._on_delete_requested)
        # Background-build commentary + completion → the Console (status line / spoken announcement).
        container.bus.subscribe(BuildProgress, self._buildProgressSignal.emit)
        container.bus.subscribe(BuildFinished, self._buildFinishedSignal.emit)
        self._buildProgressSignal.connect(self._on_build_progress)
        self._buildFinishedSignal.connect(self._on_build_finished)
        # Background self-change drafting → live narration + an apply/discard prompt when ready.
        container.bus.subscribe(SelfChangeProgress, self._selfChangeProgressSignal.emit)
        container.bus.subscribe(SelfChangeFinished, self._selfChangeFinishedSignal.emit)
        self._selfChangeProgressSignal.connect(self._on_self_change_progress)
        self._selfChangeFinishedSignal.connect(self._on_self_change_finished)

    def _build_nav(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet(f"border-bottom:1px solid {LINE};")
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 12, 16, 12)
        brand = QLabel("◉  HELIX")
        brand.setObjectName("Title")
        brand.setStyleSheet(f"color:{CYAN};")
        glow = QGraphicsDropShadowEffect(brand)  # a soft cyan halo on the wordmark
        glow.setColor(QColor(CYAN))
        glow.setBlurRadius(20)
        glow.setOffset(0, 0)
        brand.setGraphicsEffect(glow)
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

    def _on_restart_requested(self) -> None:
        # Spawn the fresh process, THEN quit this one — otherwise two HELIX processes fight over the mic
        # and the SQLite store. quit() does NOT deliver closeEvent, so the teardown (queue reap, mic/worker
        # release, DB close) runs via the app.aboutToQuit hooks wired in bootstrap — covering this path
        # and OS logoff alike.
        self._c.restart()
        QApplication.instance().quit()

    def _on_build(self, event: object) -> None:
        self.launcher.refresh()
        # A rename can move the open build to a NEW slug — re-point the viewer so it isn't pinned to the
        # moved-on-disk workspace (and later iterate events keep reaching it).
        if isinstance(event, BuildRenamed):
            old = getattr(event, "old_slug", None)
            if old is not None and old == self._viewer_slug:
                self._viewer_slug = event.app.slug
                self._reload_viewer()
            return
        # Keep the in-app viewer honest when the build it's showing changes underneath it: reload on an
        # iterate (so "make it taller" visibly updates), close on a delete (no dead page on a gone folder).
        slug = getattr(event, "slug", None) or getattr(getattr(event, "app", None), "slug", None)
        if slug and slug == self._viewer_slug:
            if isinstance(event, BuildDeleted):
                self._close_viewer()
            else:
                self._reload_viewer()

    def _on_delete_requested(self, ev: object) -> None:
        # The model asked to delete ev.name; get one real human click, then perform it via the registry
        # (which removes the build or agent and publishes the refresh/viewer events).
        self.console.offer_delete(ev.name, lambda: self._c.tools.confirm_delete(ev.name))

    def _on_self_change_progress(self, ev: object) -> None:
        self.console.on_build_progress("self-change", ev.line)

    def _on_self_change_finished(self, ev: object) -> None:
        self.console.on_self_change_finished(ev.ok, ev.summary, ev.branch, ev.error, ev.stopped)

    def _on_build_progress(self, ev: object) -> None:
        self.console.on_build_progress(ev.name, ev.line)

    def _on_build_finished(self, ev: object) -> None:
        self.console.on_build_finished(ev.name, ev.ok, ev.error, ev.stopped, ev.handle, ev.iterating)

    def closeEvent(self, event) -> None:
        # If real work is in flight, give the user a decision point rather than silently abandoning it.
        active, pending = self._c.build_queue.snapshot()
        if active or pending or self.console.is_busy() or self._c.selfdev_lane.busy():
            busy = active or ("a self-change" if self._c.selfdev_lane.busy() else "your request")
            extra = f" (+{len(pending)} queued)" if pending else ""
            confirm = QMessageBox.question(
                self,
                "HELIX is still working",
                f"Still working on “{busy}”{extra}. Close anyway? Unfinished work will be stopped.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        # Reap the background build queue FIRST: kill the coder subprocess before anything else tears
        # down, so closing mid-build never orphans claude.exe (it would keep billing + lock the workspace).
        for teardown in (
            self._c.build_queue.shutdown,
            self._c.selfdev_lane.shutdown,
            (self._viewer.clear if self._viewer is not None else lambda: None),
            self.console.shutdown,
            self.launcher.shutdown,
            self._c.store.close,
        ):
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
                self._viewer_slug = slug
                self._viewer.load(target, app.name)
                self._go(_VIEWER)
                return
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))  # fallback: no WebEngine
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ws)))  # non-HTML build: open its folder

    def _reload_viewer(self) -> None:
        """Re-load the open build's page so a background iterate (a rewritten GLB/HTML) shows at once."""
        if self._viewer is None or self._viewer_slug is None:
            return
        app = next((a for a in self._c.builds.list() if a.slug == self._viewer_slug), None)
        if app is None or app.kind != AppKind.HTML or not app.entry_point:
            return
        target = self._c.builds.workspace(app.slug) / app.entry_point
        self._viewer_target = target
        self._viewer.load(target, app.name)

    def _close_viewer(self) -> None:
        if self._viewer is not None:
            self._viewer.clear()  # stop the page (animation/audio) and free the GL surface
        self._viewer_slug = None
        self._go(_MENU)

    def _open_current_externally(self) -> None:
        if self._viewer_target is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._viewer_target)))
