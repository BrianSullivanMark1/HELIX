"""SettingsView — the one ⚙. For now: your Claude API key. Everything else is a smart default."""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from helix.ports.stores import SettingsStore
from helix.ui.theme import MUTED


class SettingsView(QWidget):
    saved = pyqtSignal()

    def __init__(self, settings: SettingsStore) -> None:
        super().__init__()
        self.setObjectName("Panel")
        self._settings = settings

        root = QVBoxLayout(self)
        root.setContentsMargins(36, 28, 36, 28)
        root.setSpacing(14)

        title = QLabel("Settings")
        title.setObjectName("Title")
        root.addWidget(title)

        hint = QLabel(
            "Your Claude API key stays on this machine. The only thing it's ever used for is the "
            "Claude calls you trigger."
        )
        hint.setObjectName("Status")
        hint.setWordWrap(True)
        root.addWidget(hint)

        root.addSpacing(8)
        root.addWidget(QLabel("Claude API key"))
        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText("sk-ant-…")
        self._key.setText(self._settings.get("claude_api_key", "") or "")
        root.addWidget(self._key)

        row = QHBoxLayout()
        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.clicked.connect(self._save)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{MUTED};")
        row.addWidget(save)
        row.addWidget(self._status)
        row.addStretch(1)
        root.addLayout(row)
        root.addStretch(1)

    def reload(self) -> None:
        self._key.setText(self._settings.get("claude_api_key", "") or "")

    def _save(self) -> None:
        self._settings.set("claude_api_key", self._key.text().strip())
        self._status.setText("Saved.")
        self.saved.emit()
