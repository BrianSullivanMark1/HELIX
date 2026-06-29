"""HelixMainWindow — the shell: the Presence orb as the window background, nav + pages floating on top."""
from __future__ import annotations

import socket

from PyQt6.QtCore import QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QInputDialog,
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
    BuildStarted,
    SelfChangeFinished,
    SelfChangeProgress,
)
from helix.domain.models import AppKind, BuildKind
from helix.logging_setup import get_logger
from helix.services.prompts import build_3d_model_prompt, build_task_prompt
from helix.ui.build_status import BuildStatusBoard
from helix.ui.commands_view import CommandsDialog
from helix.ui.connections_dialog import ConnectionsDialog
from helix.ui.console_view import ConsoleView
from helix.ui.knowledge_view import KnowledgeView
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
    _buildStartedSignal = pyqtSignal(object)
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
        # The shared status board behind the menu-tile borders, the Console legend, and the orb hue.
        self._board = BuildStatusBoard()

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
        self.launcher = LauncherView(
            container.builds, container.agents, container.tasks, container.knowledge
        )
        self.settings = SettingsView(container.settings, container.connections)
        self._stack.addWidget(self.console)  # 0
        self._stack.addWidget(self.launcher)  # 1
        self._stack.addWidget(self.settings)  # 2
        # In-app viewer for built HTML apps and 3D models — renders inside HELIX instead of the browser,
        # and is reused so tabs never pile up. None if PyQt6-WebEngine isn't available (browser fallback).
        self._viewer = AppViewer() if AppViewer is not None else None
        self._viewer_target: object | None = None  # the file/URL currently shown, for "open in browser"
        self._viewer_slug: str | None = None  # the build currently open in the viewer (for live reload)
        self._app_ports: dict[str, int] = {}  # slug -> port of a backend app's local server, shown in-app
        if self._viewer is not None:
            self._viewer.closeRequested.connect(self._close_viewer)
            self._viewer.openExternallyRequested.connect(self._open_current_externally)
            self._viewer.editRequested.connect(self._on_viewer_edit)  # live "Edit with AI" bar
            self._stack.addWidget(self._viewer)  # 3
        # The Knowledge manager — a native widget (no WebEngine), reused for whichever base is opened. Its
        # stack index is captured (not a fixed constant) since the optional viewer above may or may not
        # have been added before it.
        self._knowledge_view = KnowledgeView(container.knowledge)
        self._knowledge_slug: str | None = None  # the base currently open in the knowledge manager
        self._knowledge_view.closeRequested.connect(self._close_knowledge_view)
        self._knowledge_view_index = self._stack.addWidget(self._knowledge_view)
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
        self.console.openBuildRequested.connect(self._open_app)  # a legend chip → open that build
        self.settings.saved.connect(self._on_settings_saved)
        self.launcher.newAppRequested.connect(lambda: self._go(_CONSOLE))
        self.launcher.openSettingsRequested.connect(lambda: self._go(_SETTINGS))
        self.launcher.openAppRequested.connect(self._open_app)
        self.launcher.buildSeen.connect(self._on_build_seen)         # opened/ran → clear done/error status
        self.launcher.editBuildRequested.connect(self._on_edit_build)  # "Edit with AI" on a card
        self.launcher.connectBuildRequested.connect(self._on_connect_build)  # 🔑 API-key Connect panel
        self.launcher.set_status_provider(self._board.status_of)     # colour the tiles from the board
        self.launcher.set_connections_service(container.connections)  # show Connect on builds that need keys

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
        # Background-build lifecycle → the status board (tiles/legend/orb) + the Console (status line /
        # spoken announcement). Started fires the instant a build begins so the UI shows it at once.
        container.bus.subscribe(BuildStarted, self._buildStartedSignal.emit)
        container.bus.subscribe(BuildProgress, self._buildProgressSignal.emit)
        container.bus.subscribe(BuildFinished, self._buildFinishedSignal.emit)
        self._buildStartedSignal.connect(self._on_build_started)
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
        for label, idx in (("◉ Console", _CONSOLE), ("☰ Menu", _MENU)):
            btn = QPushButton(label)
            btn.setObjectName("Nav")
            btn.clicked.connect(lambda _checked=False, i=idx: self._nav(i))
            row.addWidget(btn)
        # Commands reference — a pop-up of the keywords + controls (part of the Forge shell).
        commands_btn = QPushButton("❔ Commands")
        commands_btn.setObjectName("Nav")
        commands_btn.clicked.connect(self._show_commands)
        row.addWidget(commands_btn)
        settings_btn = QPushButton("⚙ Settings")
        settings_btn.setObjectName("Nav")
        settings_btn.clicked.connect(lambda _checked=False: self._nav(_SETTINGS))
        row.addWidget(settings_btn)
        return bar

    def _show_commands(self) -> None:
        CommandsDialog(self).exec()

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

    def _refresh_build_ui(self) -> None:
        """Re-render the surfaces the status board drives: the menu tiles and the Console legend."""
        self.launcher.refresh()
        self.console.update_legend(self._board.legend())

    def _on_build(self, event: object) -> None:
        # Keep the status board's keys honest: a deleted build drops off; a renamed build's old-slug entry
        # is dropped (its transient green/red status doesn't survive the move — it just resets to blue).
        if isinstance(event, BuildDeleted):
            self._board.remove(event.slug)
        elif isinstance(event, BuildRenamed):
            old = getattr(event, "old_slug", None)
            if old:
                self._board.remove(old)
        self._refresh_build_ui()
        # Keep an open Knowledge base in sync when it changes underneath the manager (the orb saved a note
        # into it, it was renamed, or it was deleted from elsewhere).
        self._sync_knowledge_view(event)
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
            elif slug in self._app_ports:
                self._restart_app_server(slug)  # backend app edited — serve the new code, then re-show
            else:
                self._reload_viewer()

    def _on_build_started(self, ev: object) -> None:
        self._board.mark_building(ev.slug, ev.name)
        self.console.on_build_started(ev.name)
        self._refresh_build_ui()

    def _on_build_seen(self, slug: str) -> None:
        """A build was opened or run — clear its done/error status (back to blue) and refresh."""
        if self._board.mark_seen(slug):
            self._refresh_build_ui()

    def _on_edit_build(self, slug: str, name: str) -> None:
        """The 'Edit with AI' action on a menu card — describe a change and HELIX iterates this exact
        build live. Routes only through the build queue (a sandboxed data build), never the shell."""
        app = next((a for a in self._c.builds.list() if a.slug == slug), None)
        if app is None:
            return
        change, ok = QInputDialog.getMultiLineText(
            self, f"Edit {name}", f"Describe the change to “{name}” — HELIX updates it live:", ""
        )
        change = (change or "").strip()
        if ok and change:
            self._enqueue_edit(app, change)

    def _on_connect_build(self, slug: str, name: str) -> None:
        """Open the auto-generated 'paste your API keys' panel for a build that declared it needs them."""
        conns = self._c.connections.declared(slug)
        if not conns:
            return
        dlg = ConnectionsDialog(
            self, f"Connect — {name}", conns,
            self._c.connections.value, self._c.connections.set_value,
        )
        if dlg.exec():
            self._refresh_build_ui()  # repaint the Connect button (set vs. still-missing)

    def _on_viewer_edit(self, change: str) -> None:
        """The viewer's live edit bar — iterate the build that's currently open in place."""
        slug = self._viewer_slug
        change = (change or "").strip()
        if not slug or not change:
            return
        app = next((a for a in self._c.builds.list() if a.slug == slug), None)
        if app is None:
            return
        self._enqueue_edit(app, change)
        if self._viewer is not None:
            self._viewer.set_edit_status("Updating live…")

    def _enqueue_edit(self, app, change: str) -> None:
        """Queue an in-place iterate of an EXISTING build. Passing its exact name resolves to the same
        slug, so the Forge edits in place and never forks a near-duplicate. Shared by the menu card edit
        and the viewer's live edit box."""
        q = self._c.build_queue
        if app.build_kind == BuildKind.MODEL:
            q.enqueue(app.name, change, kind=BuildKind.MODEL, prompt=build_3d_model_prompt(app.name, change))
        elif app.build_kind == BuildKind.TASK:
            q.enqueue(app.name, change, kind=BuildKind.TASK, prompt=build_task_prompt(app.name, change))
        else:
            q.enqueue(app.name, change, kind=BuildKind.APP)
        self.console.status.setText(f"Updating {app.name}…")

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
        slug = getattr(ev, "slug", "") or ""
        # A failure BEFORE the build starts (e.g. a name/kind collision) has no slug and never fired a
        # BuildStarted, so there's no tile/legend entry to update — touching the board with an empty slug
        # would leak a permanent, un-clearable chip. Only update the board for a real, keyed build.
        if slug:
            if ev.stopped:
                self._board.remove(slug)         # stopped: clear the yellow (cleanup offered separately)
            elif ev.ok:
                self._board.mark_done(slug, ev.name)   # green until reopened
            else:
                self._board.mark_error(slug, ev.name)  # red until reopened
        self.console.on_build_finished(ev.name, ev.ok, ev.error, ev.stopped, ev.handle, ev.iterating)
        # If the finished build is the one open in the viewer, reflect it on the live edit bar (the page
        # itself reloads via the BuildIterated handler on success).
        if self._viewer is not None and slug and slug == self._viewer_slug:
            self._viewer.set_edit_status(
                "Updated." if ev.ok else ("Stopped." if ev.stopped else "That change didn't go through.")
            )
        self._refresh_build_ui()

    def closeEvent(self, event) -> None:
        # If real work is in flight, give the user a decision point rather than silently abandoning it.
        active, pending = self._c.build_queue.snapshot()  # both are lists of build names
        if active or pending or self.console.is_busy() or self._c.selfdev_lane.busy():
            busy = ", ".join(active) or ("a self-change" if self._c.selfdev_lane.busy() else "your request")
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
            self._stop_app_servers,  # kill any backend-app servers so they don't outlive HELIX / hold a port
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
        self._on_build_seen(slug)  # opening acknowledges a done/error result → tile/legend back to blue
        if app.build_kind == BuildKind.KNOWLEDGE:
            # A knowledge base opens in its native manager (add notes/files, search, remove), not a webview.
            self._knowledge_slug = slug
            self._knowledge_view.open_base(slug, app.name)
            self._go(self._knowledge_view_index)
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
        if app.kind == AppKind.PYTHON and app.entry_point:
            # An app with a backend (main.py) RUNS its local server and is shown INSIDE HELIX — no browser.
            self._open_server_app(slug, app)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ws)))  # other build: open its folder

    # ----- backend (server) apps: run main.py on a private port, show its page in the in-app viewer -----
    def _open_server_app(self, slug: str, app) -> None:
        if self._viewer is None:  # no in-app web view available — fall back to a console launch
            self._c.tasks.run(slug)
            self.console.status.setText(f"Started “{app.name}” (no in-app viewer available).")
            return
        if not self._c.tasks.is_running(slug):  # not already running — start it headless on a free port
            port = self._free_port()
            if not self._c.tasks.run(slug, port=port, headless=True):
                self.console.status.setText(f"Couldn't start “{app.name}”. Make sure Python is installed.")
                return
            self._app_ports[slug] = port
        port = self._app_ports.get(slug)
        if port is None:
            return
        url = f"http://127.0.0.1:{port}"
        self._viewer_slug = slug
        self._viewer_target = url
        self._viewer.show_starting(app.name)
        self._go(_VIEWER)
        self._await_server(slug, app.name, port, url, 0)

    @staticmethod
    def _free_port() -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()

    @staticmethod
    def _port_open(port: int) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.25)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    def _await_server(self, slug: str, name: str, port: int, url: str, tries: int) -> None:
        """Poll until the build's local server is accepting, then show its page in the viewer."""
        if self._viewer is None or self._viewer_slug != slug:
            return  # user navigated away / opened something else
        if not self._c.tasks.is_running(slug):  # the server process died on startup
            self._viewer.show_notice(name, "It didn’t start. Check its server.log in the build folder.")
            self.console.status.setText(f"“{name}” didn’t start — see its server.log.")
            return
        if self._port_open(port):
            self._viewer.load_url(url, name)
            return
        if tries >= 40:  # ~6s and still not up
            self._viewer.show_notice(name, "It’s taking too long to start — try Reload.")
            return
        QTimer.singleShot(150, lambda: self._await_server(slug, name, port, url, tries + 1))

    def _restart_app_server(self, slug: str) -> None:
        """A backend app was edited — restart its server so it serves the new code, then re-show it."""
        app = next((a for a in self._c.builds.list() if a.slug == slug), None)
        if app is None:
            return
        self._c.tasks.stop(slug)
        self._app_ports.pop(slug, None)
        self._open_server_app(slug, app)

    def _stop_app_servers(self) -> None:
        for slug in list(self._app_ports):
            self._c.tasks.stop(slug)
        self._app_ports.clear()

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
        slug = self._viewer_slug
        if self._viewer is not None:
            self._viewer.clear()  # stop the page (animation/audio) and free the GL surface
        self._viewer_slug = None
        if slug and slug in self._app_ports:  # leaving a backend app — stop its server, free the port
            self._c.tasks.stop(slug)
            self._app_ports.pop(slug, None)
        self._go(_MENU)

    def _close_knowledge_view(self) -> None:
        self._knowledge_slug = None
        self._go(_MENU)

    def _sync_knowledge_view(self, event: object) -> None:
        """Reflect a change to the OPEN knowledge base in the manager: re-point + retitle on a rename,
        close on a delete, reload its contents on any other change (e.g. the orb remembered a note)."""
        if self._knowledge_slug is None:
            return
        if isinstance(event, BuildRenamed):
            if getattr(event, "old_slug", None) == self._knowledge_slug:
                self._knowledge_slug = event.app.slug
                self._knowledge_view.open_base(event.app.slug, event.app.name)
            return
        if isinstance(event, BuildDeleted):
            if event.slug == self._knowledge_slug:
                self._close_knowledge_view()
            return
        slug = getattr(getattr(event, "app", None), "slug", None)
        if slug and slug == self._knowledge_slug:
            self._knowledge_view.reload()

    def _open_current_externally(self) -> None:
        t = self._viewer_target
        if t is None:
            return
        if isinstance(t, str) and t.startswith("http"):
            QDesktopServices.openUrl(QUrl(t))  # a backend app: open its local server in the real browser
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(t)))
