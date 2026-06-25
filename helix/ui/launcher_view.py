"""LauncherView — the Menu: three tabs over what you've made.

  • Apps   — built apps that open a screen (click a card to launch; Open / Rename / Remove).
  • Agents — saved goals HELIX runs on demand (Run / Add / Remove).
  • Tasks  — built scripts that *do a thing* when run (Run / Rename / Remove).

Apps and Agents/Tasks are data, not shell: they're freely removable. The tabs, New app, and Settings
are the immutable shell.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from helix.domain.models import AppKind
from helix.services.agents import AgentService
from helix.services.builds import BuildService
from helix.services.tasks import TaskService
from helix.ui.theme import CYAN, MUTED
from helix.ui.workers import QtWorker

_APPS, _AGENTS, _TASKS = 0, 1, 2


class _Card(QFrame):
    """A panel with a title, a muted subtitle, and an optional action button row."""

    def __init__(self, title: str, subtitle: str) -> None:
        super().__init__()
        self.setObjectName("Card")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(16, 14, 16, 14)
        self._lay.setSpacing(6)
        name = QLabel(title)
        name.setStyleSheet(f"font-size:15px;font-weight:600;color:{CYAN};")
        desc = QLabel(subtitle)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{MUTED};")
        self._lay.addWidget(name)
        self._lay.addWidget(desc)

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

    def __init__(self, builds: BuildService, agents: AgentService, tasks: TaskService) -> None:
        super().__init__()
        self.setObjectName("Panel")
        self._builds = builds
        self._agents = agents
        self._tasks = tasks
        self._workers: set[QtWorker] = set()

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(14)

        # Header: tabs on the left, New app + Settings on the right.
        header = QHBoxLayout()
        self._tabs: dict[int, QPushButton] = {}
        for idx, label in ((_APPS, "Apps"), (_AGENTS, "Agents"), (_TASKS, "Tasks")):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _c=False, i=idx: self._show_tab(i))
            self._tabs[idx] = btn
            header.addWidget(btn)
        header.addStretch(1)
        new_btn = QPushButton("＋ New app")
        new_btn.setObjectName("Primary")
        new_btn.clicked.connect(self.newAppRequested.emit)
        settings_btn = QPushButton("⚙ Settings")
        settings_btn.clicked.connect(self.openSettingsRequested.emit)
        header.addWidget(new_btn)
        header.addWidget(settings_btn)
        root.addLayout(header)

        self._stack = QStackedWidget()
        self._apps_grid, apps_page, self._apps_empty = self._grid_page(
            "No apps yet — go to the orb and describe one."
        )
        self._tasks_grid, tasks_page, self._tasks_empty = self._grid_page(
            "No tasks yet. Ask the orb to build a script that does something, and it'll show here."
        )
        self._stack.addWidget(apps_page)            # 0
        self._stack.addWidget(self._agents_page())  # 1
        self._stack.addWidget(tasks_page)           # 2
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
        apps = [a for a in self._builds.list() if a.kind != AppKind.PYTHON]
        app_cards = []
        for app in apps:
            card = _Card(app.name, app.request)
            open_btn = QPushButton("Open")
            open_btn.clicked.connect(lambda _c=False, s=app.slug: self.openAppRequested.emit(s))
            rename = QPushButton("✎ Rename")
            rename.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self._rename_build(s, n))
            remove = QPushButton("✕ Remove")
            remove.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self._remove_build(s, n))
            card.add_actions(open_btn, rename, remove)
            app_cards.append(card)
        self._fill_grid(self._apps_grid, self._apps_empty, app_cards)

        runnable = self._tasks.runnable()
        task_cards = []
        for app in runnable:
            card = _Card(app.name, app.request)
            run = QPushButton("▶ Run")
            run.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self._run_task(s, n))
            rename = QPushButton("✎ Rename")
            rename.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self._rename_build(s, n))
            remove = QPushButton("✕ Remove")
            remove.clicked.connect(lambda _c=False, s=app.slug, n=app.name: self._remove_build(s, n))
            card.add_actions(run, rename, remove)
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
        self._agents.add(name, goal)
        self._agent_name.clear()
        self._agent_goal.clear()
        self._agent_status.setText(f"Saved agent “{name}”.")
        self.refresh()

    def _remove_agent(self, name: str) -> None:
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
            self._builds.delete(slug)
            self.refresh()

    def _run_task(self, slug: str, name: str) -> None:
        ok = self._tasks.run(slug)
        # Tasks open their own console; surface the outcome where the user can see it.
        self._agent_status.setText(f"Launched “{name}”." if ok else f"Couldn’t launch “{name}”.")

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
