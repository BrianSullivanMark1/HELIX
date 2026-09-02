"""LauncherView — the Menu: five tabs over what you've made.

  • Apps      — built apps that open a screen (Open / Rename / Remove).
  • Protocols — built scripts that *do a thing* when run (Run / Rename / Remove).
  • Agents    — saved goals HELIX runs on demand (Run / Rename / Remove).
  • Holograms — 3D models the user designs by talking (drafted in OpenSCAD, shown as an engineering-style
                drawing; build_3d_model) (Open / Rename / Remove).
  • Vault     — searchable collections of the user's own notes/documents (Open / Rename / Remove).

Builds are data, not shell: they're freely removable. The tabs and New app are the immutable shell
(Settings lives in the top-right of the window, so the menu doesn't repeat it).
"""
from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from helix.services.agents import AgentService
from helix.services.builds import BuildService
from helix.services.tasks import TaskService
from helix.ui.build_status import BuildStatus
from helix.ui.console_view import chip_strip  # shared: a chip row that can't widen the window
from helix.ui.theme import CYAN, LINE, MUTED, PANEL, STATUS_DONE, STATUS_ERROR, STATUS_WORKING
from helix.ui.workers import QtWorker

_APPS, _MODELS, _AGENTS, _TASKS, _KNOWLEDGE = 0, 1, 2, 3, 4

# Tile border colour per build status (None = the default look). One glance at the menu shows what's
# building (yellow), freshly done (green), or broke (red); blue is the resting state.
_STATUS_BORDER = {
    BuildStatus.BUILDING: STATUS_WORKING,
    BuildStatus.DONE: STATUS_DONE,
    BuildStatus.ERROR: STATUS_ERROR,
}


class _Card(QFrame):
    """A panel with a title, a muted subtitle, and an optional action button row."""

    def __init__(self, title: str, subtitle: str) -> None:
        super().__init__()
        self.setObjectName("Card")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(16, 14, 16, 14)
        self._lay.setSpacing(6)
        name = QLabel(title)
        # PlainText: a build name / agent goal is user- (and possibly attacker-, via a replayed app
        # description) controlled — never let it render as live Qt rich text (e.g. a remote-image beacon).
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setStyleSheet(f"font-size:15px;font-weight:600;color:{CYAN};")
        desc = QLabel(subtitle)
        desc.setWordWrap(True)
        desc.setTextFormat(Qt.TextFormat.PlainText)
        desc.setStyleSheet(f"color:{MUTED};")
        self._lay.addWidget(name)
        self._lay.addWidget(desc)

    def apply_status(self, status: "BuildStatus | None") -> None:
        """Colour the card's border to its build status. None keeps the default (global stylesheet)."""
        color = _STATUS_BORDER.get(status) if status is not None else None
        if color is None:
            self.setStyleSheet("")  # fall back to the global QFrame#Card rule
            return
        # A 2px coloured border; restate background + radius since an instance stylesheet replaces the rule.
        self.setStyleSheet(
            f"QFrame#Card{{background:{PANEL};border:2px solid {color};border-radius:14px;}}"
        )

    def add_actions(self, *buttons: QPushButton) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 4, 0, 0)
        for b in buttons:
            row.addWidget(b)
        row.addStretch(1)
        self._lay.addLayout(row)


class LauncherView(QWidget):
    newAppRequested = pyqtSignal()
    openSettingsRequested = pyqtSignal()
    openAppRequested = pyqtSignal(str)
    buildSeen = pyqtSignal(str)              # a build was opened/run — clears its done/error status
    editBuildRequested = pyqtSignal(str, str)  # (slug, name) — open the live "Edit with AI" prompt
    connectBuildRequested = pyqtSignal(str, str)  # (slug, name) — open the API-key Connect panel
    buildReverted = pyqtSignal(str)            # slug — a build was rolled back; reload its open viewer

    def __init__(
        self, builds: BuildService, agents: AgentService, tasks: TaskService, knowledge=None,
        recommend=None,
    ) -> None:
        super().__init__()
        self.setObjectName("Panel")
        self._builds = builds
        self._agents = agents
        self._tasks = tasks
        self._knowledge = knowledge  # KnowledgeService — drives the Vault tab's cards + doc counts
        self._recommend = recommend  # RecommendService — the "Suggested" strip of most-used/neglected builds
        self._workers: set[QtWorker] = set()
        # slug -> BuildStatus|None, supplied by the main window's status board; drives the tile borders.
        self._status_provider: Callable[[str], "BuildStatus | None"] = lambda _slug: None
        self._connections = None  # ConnectionsService (read-only here): whether a build needs/has its keys

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(14)

        # Header: tabs on the left, New app + Settings on the right.
        header = QHBoxLayout()
        self._tabs: dict[int, QPushButton] = {}
        for idx, label in (
            (_APPS, "Apps"), (_TASKS, "Protocols"), (_AGENTS, "Agents"), (_MODELS, "Holograms"),
            (_KNOWLEDGE, "Vault"),
        ):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _c=False, i=idx: self._show_tab(i))
            self._tabs[idx] = btn
            header.addWidget(btn)
        header.addStretch(1)
        new_btn = QPushButton("＋ New app")
        new_btn.setObjectName("Primary")
        new_btn.clicked.connect(self.newAppRequested.emit)
        header.addWidget(new_btn)  # Settings lives in the window's top-right nav — not repeated here
        root.addLayout(header)

        # "Suggested" strip — the builds you reach for most (and one you haven't opened in a while),
        # from the local usage ledger. Hidden until there's usage to suggest from. Clicking one opens it.
        self._suggest_row = QHBoxLayout()
        self._suggest_row.setContentsMargins(0, 0, 0, 0)
        self._suggest_row.setSpacing(8)
        self._suggest_host = QWidget()
        self._suggest_host.setLayout(self._suggest_row)
        self._suggest_host.setStyleSheet("background: transparent;")
        # Scrolled sideways so a run of suggestions can never widen the window past the screen edge.
        self._suggest_strip = chip_strip(self._suggest_host)
        self._suggest_strip.setVisible(False)
        root.addWidget(self._suggest_strip)

        self._stack = QStackedWidget()
        self._apps_grid, apps_page, self._apps_empty = self._grid_page(
            "No apps yet — go to the orb and describe one."
        )
        self._models_grid, models_page, self._models_empty = self._grid_page(
            "No holograms yet — ask the orb to show you something in 3D."
        )
        self._tasks_grid, tasks_page, self._tasks_empty = self._grid_page(
            "No protocols yet. Ask the orb to build a protocol — a script that does something when "
            "you run it."
        )
        # The Protocols page gets its OWN status line — a Run result used to land on the hidden
        # Agents-page label, so the user never saw it.
        self._tasks_status = QLabel("")
        self._tasks_status.setObjectName("Status")
        self._tasks_status.setTextFormat(Qt.TextFormat.PlainText)  # protocol run output — never rich text
        self._tasks_status.setWordWrap(True)
        tasks_page.layout().addWidget(self._tasks_status)

        self._knowledge_grid, knowledge_page, self._knowledge_empty = self._grid_page(
            "Nothing in the vault yet. Tell the orb to remember something, or open a vault here to "
            "add notes and files HELIX can search."
        )

        self._stack.addWidget(apps_page)            # 0
        self._stack.addWidget(models_page)          # 1
        self._stack.addWidget(self._agents_page())  # 2
        self._stack.addWidget(tasks_page)           # 3
        self._stack.addWidget(knowledge_page)       # 4
        root.addWidget(self._stack, stretch=1)

        self._show_tab(_APPS)
        self.refresh()

    # ----- page scaffolding -----
    def _grid_page(self, empty_text: str) -> tuple[QGridLayout, QWidget, QLabel]:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        empty = QLabel(empty_text)
        empty.setObjectName("Status")
        lay.addWidget(empty)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        scroll.setWidget(host)
        lay.addWidget(scroll, stretch=1)
        return grid, page, empty

    def _agents_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # Add-an-agent row
        add = QHBoxLayout()
        self._agent_name = QLineEdit()
        self._agent_name.setPlaceholderText("Agent name (e.g. Morning brief)")
        self._agent_name.setMaximumWidth(220)
        self._agent_goal = QLineEdit()
        self._agent_goal.setPlaceholderText("Goal — what should HELIX do when you run it?")
        add_btn = QPushButton("＋ Add agent")
        add_btn.setObjectName("Primary")
        add_btn.clicked.connect(self._add_agent)
        add.addWidget(self._agent_name)
        add.addWidget(self._agent_goal)
        add.addWidget(add_btn)
        lay.addLayout(add)

        self._agents_empty = QLabel("No agents yet. An agent is a saved goal HELIX runs on demand.")
        self._agents_empty.setObjectName("Status")
        lay.addWidget(self._agents_empty)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        self._agents_grid = QGridLayout(host)
        self._agents_grid.setContentsMargins(0, 0, 0, 0)
        self._agents_grid.setSpacing(12)
        scroll.setWidget(host)
        lay.addWidget(scroll, stretch=1)

        self._agent_status = QLabel("")
        self._agent_status.setObjectName("Status")
        self._agent_status.setTextFormat(Qt.TextFormat.PlainText)  # shows agent run output — never rich text
        self._agent_status.setWordWrap(True)
        lay.addWidget(self._agent_status)
        return page

    # ----- tabs -----
    def _show_tab(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        for i, btn in self._tabs.items():
            active = i == index
            btn.setChecked(active)
            btn.setObjectName("Primary" if active else "")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    # ----- refresh -----
    def refresh(self) -> None:
        # Classification lives in the service (BuildService.categorized), never here — the menu just
        # renders the pre-sorted buckets, so the tabs and the orb's list always agree.
        cat = self._builds.categorized()
        self._fill_grid(
            self._apps_grid, self._apps_empty, [self._openable_card(a) for a in cat["apps"]]
        )
        self._fill_grid(
            self._models_grid, self._models_empty, [self._openable_card(a) for a in cat["models"]]
        )
        self._fill_grid(
            self._knowledge_grid, self._knowledge_empty,
            [self._knowledge_card(a) for a in cat.get("knowledge", [])],
        )

        task_cards = []
        for app in cat["tasks"]:
            card = _Card(app.name, app.request)
            run = QPushButton("▶ Run")
            run.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self._run_task(s, n))
            edit = QPushButton("✨ Edit")
            edit.setToolTip("Describe a change and HELIX updates this protocol live")
            edit.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self.editBuildRequested.emit(s, n))
            rename = QPushButton("✎ Rename")
            rename.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self._rename_build(s, n))
            remove = QPushButton("✕ Remove")
            remove.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self._remove_build(s, n))
            actions = [run, edit]
            connect = self._connect_button(app)
            if connect is not None:
                actions.append(connect)
            actions += [self._history_button(app), rename, remove]
            card.add_actions(*actions)
            card.apply_status(self._status_provider(app.slug))
            task_cards.append(card)
        self._fill_grid(self._tasks_grid, self._tasks_empty, task_cards)

        agent_cards = []
        for agent in self._agents.list():
            card = _Card(agent.name, agent.goal or "—")
            run = QPushButton("▶ Run")
            run.clicked.connect(lambda _c=False, n=agent.name: self._run_agent(n))
            rename = QPushButton("✎ Rename")
            rename.clicked.connect(lambda _c=False, n=agent.name: self._rename_agent(n))
            remove = QPushButton("✕ Remove")
            remove.clicked.connect(lambda _c=False, n=agent.name: self._remove_agent(n))
            card.add_actions(run, rename, remove)
            agent_cards.append(card)
        self._fill_grid(self._agents_grid, self._agents_empty, agent_cards)
        self._refresh_suggestions()

    def _refresh_suggestions(self) -> None:
        """Rebuild the 'Suggested' strip from the usage ledger. Hidden when there's nothing to suggest."""
        while self._suggest_row.count():
            item = self._suggest_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        sugg = self._recommend.suggestions(self._builds.list()) if self._recommend is not None else []
        if not sugg:
            self._suggest_strip.setVisible(False)
            return
        label = QLabel("Suggested")
        label.setStyleSheet(f"color:{MUTED};font-size:12px;")
        self._suggest_row.addWidget(label)
        for app, reason in sugg:
            chip = QPushButton()
            # Bound each chip (elide, cap width) so several suggestions — this strip isn't scrolled —
            # can't stretch the row, and the window, past the screen edge.
            chip.setMaximumWidth(260)
            chip.setText(chip.fontMetrics().elidedText(
                f"{app.name}  ·  {reason}", Qt.TextElideMode.ElideRight, 232))
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolTip(f"Open {app.name}")
            chip.setStyleSheet(
                f"QPushButton{{background:rgba(13,20,27,0.7);color:{CYAN};border:1px solid {LINE};"
                "border-radius:12px;padding:4px 12px;font-size:12px;}"
                f"QPushButton:hover{{border-color:{CYAN};}}"
            )
            chip.clicked.connect(lambda _c=False, s=app.slug: self.openAppRequested.emit(s))
            self._suggest_row.addWidget(chip)
        self._suggest_row.addStretch(1)
        self._suggest_strip.setVisible(True)

    def set_status_provider(self, provider: "Callable[[str], BuildStatus | None]") -> None:
        """Wire the source of per-build status (the main window's board) that colours the tile borders."""
        self._status_provider = provider

    def set_connections_service(self, connections) -> None:
        """Wire the ConnectionsService so cards that need API keys show a Connect button."""
        self._connections = connections

    def _connect_button(self, app) -> "QPushButton | None":
        """A '🔑 Connect' button for a build that declared it needs API keys — amber until they're set."""
        if self._connections is None or not self._connections.needs_connection(app.slug):
            return None
        missing = self._connections.missing(app.slug)
        btn = QPushButton("🔑 Connect" if missing else "🔑 Keys set")
        color = STATUS_WORKING if missing else STATUS_DONE
        btn.setStyleSheet(f"QPushButton{{border:1px solid {color};color:{color};}}")
        btn.setToolTip(
            "Set the API keys this build needs" if missing else "API keys are set — click to edit"
        )
        btn.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self.connectBuildRequested.emit(s, n))
        return btn

    def _history_button(self, app) -> QPushButton:
        """A version dropdown — the last few saved versions (with dates); pick one to roll back to it.
        Every AI edit is a version (a git commit), so this is a clean undo for a bad prompt."""
        btn = QPushButton("🕘 Versions")
        btn.setToolTip("Roll this build back to an earlier version")
        menu = QMenu(btn)
        versions = self._builds.versions(app.slug, 5)
        if len(versions) <= 1:
            menu.addAction("No earlier versions yet").setEnabled(False)
        else:
            for i, c in enumerate(versions):
                when = c.at.strftime("%b %d · %I:%M %p")
                if i == 0:
                    menu.addAction(f"{when}  ·  current").setEnabled(False)
                else:
                    act = menu.addAction(f"Revert to {when}")
                    act.triggered.connect(
                        lambda _c=False, s=app.slug, sha=c.sha, w=when, n=app.name: self._revert(s, sha, w, n)
                    )
        btn.setMenu(menu)
        return btn

    def _revert(self, slug: str, sha: str, when: str, name: str) -> None:
        confirm = QMessageBox.question(
            self,
            "Revert",
            f"Revert “{name}” to the version from {when}? Your current version is kept in history, so you "
            "can always revert again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if self._builds.revert(slug, sha) is None:
            QMessageBox.warning(
                self,
                "Revert",
                f"Couldn’t revert “{name}” — it may be open or running right now. Close it and try again.",
            )
            return
        self.buildReverted.emit(slug)  # let the main window reload an open viewer
        self.refresh()

    def _openable_card(self, app) -> _Card:
        """A card for something that opens in the browser — an app or a 3D model
        (Open / Edit with AI / Rename / Remove)."""
        card = _Card(app.name, app.request)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(lambda _c=False, s=app.slug: self.openAppRequested.emit(s))
        edit = QPushButton("✨ Edit")
        edit.setToolTip("Describe a change and HELIX updates this build live")
        edit.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self.editBuildRequested.emit(s, n))
        rename = QPushButton("✎ Rename")
        rename.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self._rename_build(s, n))
        remove = QPushButton("✕ Remove")
        remove.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self._remove_build(s, n))
        actions = [open_btn, edit]
        connect = self._connect_button(app)
        if connect is not None:
            actions.append(connect)
        actions += [self._history_button(app), rename, remove]
        card.add_actions(*actions)
        card.apply_status(self._status_provider(app.slug))
        return card

    def _knowledge_card(self, app) -> _Card:
        """A card for a vault — Open (manage its docs) / Rename / Remove. The subtitle shows how much
        is in it rather than the build request, since a vault is a living collection, not a one-shot
        description."""
        count = self._knowledge.count(app.slug) if self._knowledge is not None else 0
        subtitle = f"{count} document{'s' if count != 1 else ''} · searchable by the orb"
        card = _Card(app.name, subtitle)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(lambda _c=False, s=app.slug: self.openAppRequested.emit(s))
        rename = QPushButton("✎ Rename")
        rename.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self._rename_build(s, n))
        remove = QPushButton("✕ Remove")
        remove.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self._remove_build(s, n))
        card.add_actions(open_btn, self._history_button(app), rename, remove)
        card.apply_status(self._status_provider(app.slug))
        return card

    def _fill_grid(self, grid: QGridLayout, empty: QLabel, cards: list[QWidget]) -> None:
        while grid.count():
            item = grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        empty.setVisible(not cards)
        for i, card in enumerate(cards):
            grid.addWidget(card, i // 2, i % 2)

    # ----- actions -----
    def _add_agent(self) -> None:
        name = self._agent_name.text().strip()
        goal = self._agent_goal.text().strip()
        if not name or not goal:
            self._agent_status.setText("Give the agent a name and a goal.")
            return
        replaced = self._agents.exists(name)
        if replaced:  # don't silently overwrite a hand-written goal
            confirm = QMessageBox.question(
                self,
                "Update agent",
                f"An agent named “{name}” already exists. Replace its goal?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        self._agents.add(name, goal)
        self._agent_name.clear()
        self._agent_goal.clear()
        self._agent_status.setText(f"{'Updated' if replaced else 'Saved'} agent “{name}”.")
        self.refresh()

    def _remove_agent(self, name: str) -> None:
        confirm = QMessageBox.question(
            self,
            "Remove",
            f"Remove the agent “{name}”? This can’t be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self._agents.remove(name)
            self.refresh()

    def _ask_new_name(self, current: str) -> str | None:
        """Prompt for a new name; return it only if the user confirmed a non-empty, changed value."""
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=current)
        if not ok:
            return None
        new_name = new_name.strip()
        return new_name if new_name and new_name != current else None

    def _rename_build(self, slug: str, current: str) -> None:
        new_name = self._ask_new_name(current)
        if new_name is None:
            return
        if self._builds.rename(slug, new_name) is None:
            QMessageBox.warning(
                self,
                "Rename",
                f"Couldn’t rename to “{new_name}”. That name may already be in use, or it’s open or "
                "building right now — close it (or wait a moment) and try again.",
            )
            return
        self.refresh()

    def _rename_agent(self, current: str) -> None:
        new_name = self._ask_new_name(current)
        if new_name is None:
            return
        if self._agents.rename(current, new_name) is None:
            QMessageBox.warning(
                self,
                "Rename",
                f"Couldn’t rename to “{new_name}” — that name may already be in use.",
            )
            return
        self.refresh()

    def _remove_build(self, slug: str, name: str) -> None:
        # Apps/tasks are real built artifacts (and cost Claude time to make), so confirm before deleting.
        confirm = QMessageBox.question(
            self,
            "Remove",
            f"Remove “{name}”? This permanently deletes its files and can’t be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            if not self._builds.delete(slug):
                QMessageBox.warning(
                    self,
                    "Remove",
                    f"Couldn’t remove “{name}” — it’s open or running right now. Close it (or wait a "
                    "moment) and try again.",
                )
            self.refresh()

    def _run_task(self, slug: str, name: str) -> None:
        self.buildSeen.emit(slug)  # running a task acknowledges its done/error status (back to blue)
        if self._recommend is not None:
            self._recommend.record_run(slug)  # feed the Suggested strip
        ok = self._tasks.run(slug)
        # Protocols open their own console; surface the outcome on THEIR page, where the user is looking.
        self._tasks_status.setText(
            f"Launched “{name}” in its own window."
            if ok else f"Couldn’t launch “{name}” — it may be missing a runnable main.py."
        )

    def _run_agent(self, name: str) -> None:
        self._show_tab(_AGENTS)
        self._agent_status.setText(f"Running “{name}”…")
        worker = QtWorker(lambda emit: self._agents.run(name, on_progress=emit))
        self._workers.add(worker)
        worker.progress.connect(self._agent_status.setText)
        worker.finished_ok.connect(lambda r: self._agent_status.setText(f"HELIX: {str(r)[:600]}"))
        worker.failed.connect(lambda e: self._agent_status.setText(f"⚠  {e}"))
        worker.finished.connect(lambda w=worker: self._retire(w))
        worker.start()

    def _retire(self, worker: QtWorker) -> None:
        self._workers.discard(worker)
        worker.deleteLater()

    def shutdown(self) -> None:
        for worker in list(self._workers):
            worker.wait(3000)
