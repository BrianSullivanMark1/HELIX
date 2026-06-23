"""LauncherView — the menu of apps. Cards are the apps you've built; New app returns to the orb."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from helix.services.builds import BuildService
from helix.ui.theme import CYAN, MUTED


class _Card(QFrame):
    def __init__(self, name: str, request: str, slug: str, on_open) -> None:
        super().__init__()
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._slug = slug
        self._on_open = on_open
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(6)
        title = QLabel(name)
        title.setStyleSheet(f"font-size:15px;font-weight:600;color:{CYAN};")
        desc = QLabel(request)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{MUTED};")
        lay.addWidget(title)
        lay.addWidget(desc)

    def mousePressEvent(self, _e) -> None:
        self._on_open(self._slug)


class LauncherView(QWidget):
    newAppRequested = pyqtSignal()
    openSettingsRequested = pyqtSignal()
    openAppRequested = pyqtSignal(str)

    def __init__(self, builds: BuildService) -> None:
        super().__init__()
        self.setObjectName("Panel")
        self._builds = builds

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(14)

        header = QHBoxLayout()
        title = QLabel("Your apps")
        title.setObjectName("Title")
        new_btn = QPushButton("＋ New app")
        new_btn.setObjectName("Primary")
        new_btn.clicked.connect(self.newAppRequested.emit)
        settings_btn = QPushButton("⚙ Settings")
        settings_btn.clicked.connect(self.openSettingsRequested.emit)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(new_btn)
        header.addWidget(settings_btn)
        root.addLayout(header)

        self._empty = QLabel("No apps yet — go to the orb and describe one.")
        self._empty.setObjectName("Status")
        root.addWidget(self._empty)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(12)
        scroll.setWidget(self._grid_host)
        root.addWidget(scroll, stretch=1)

        self.refresh()

    def refresh(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        apps = self._builds.list()
        self._empty.setVisible(not apps)
        for i, app in enumerate(apps):
            card = _Card(app.name, app.request, app.slug, self.openAppRequested.emit)
            self._grid.addWidget(card, i // 2, i % 2)
