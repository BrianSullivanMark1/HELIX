"""ConnectionsDialog — the auto-generated 'paste your API keys' panel for a build.

Given what a build declared it needs (its Connection list), it renders one masked field per key with a
friendly label + hint, pre-filled with any value already saved, and writes the values back through the
ConnectionsService. The user never edits a file or sees a token in plaintext; keys live on the machine,
never in the build's folder.
"""
from __future__ import annotations

from typing import Callable

from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from helix.domain.connections import Connection
from helix.ui.theme import MUTED


class ConnectionsDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        conns: list[Connection],
        get_value: Callable[[str], str],
        set_value: Callable[[str, str], None],
    ) -> None:
        super().__init__(parent)
        self._set_value = set_value
        self._fields: list[tuple[str, QLineEdit]] = []
        self.setWindowTitle(title)
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(10)

        head = QLabel("Connect this build")
        head.setObjectName("Title")
        root.addWidget(head)
        note = QLabel(
            "Paste each key below. They're saved on this machine only — never inside the build's files "
            "or sent anywhere except the service itself."
        )
        note.setObjectName("Status")
        note.setWordWrap(True)
        root.addWidget(note)
        root.addSpacing(4)

        for c in conns:
            label = QLabel(c.label + (f"  ({c.hint})" if c.hint else ""))
            root.addWidget(label)
            field = QLineEdit()
            field.setEchoMode(QLineEdit.EchoMode.Password)
            if c.hint:
                field.setPlaceholderText(c.hint)
            field.setText(get_value(c.key) or "")
            root.addWidget(field)
            self._fields.append((c.key, field))

        row = QHBoxLayout()
        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.clicked.connect(self._save)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{MUTED};")
        row.addWidget(save)
        row.addWidget(cancel)
        row.addWidget(self._status)
        row.addStretch(1)
        root.addLayout(row)

    def _save(self) -> None:
        for key, field in self._fields:
            self._set_value(key, field.text().strip())
        self.accept()
