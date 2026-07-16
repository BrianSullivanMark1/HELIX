"""The masked key panels — where a credential is pasted, and the only place it's ever typed.

Two panels share the pattern (one masked field per key, a friendly label + hint, saved through the
store — never a file, never chat): ConnectionsDialog collects what a BUILD declared it needs, and
ConnectPanel is the just-in-time panel the orb opens mid-conversation the moment a service key is
missing. The user never sees a token in plaintext; keys live on this machine only.
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
from helix.ports.stores import SettingsStore
from helix.services.connections import CONNECTABLE, ConnectionsService
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


class ConnectPanel(QDialog):
    """The just-in-time connect panel for ONE service: the reason it's needed, a masked field per
    credential, and a Connect button that writes straight to the right store (secrets or Settings).
    An empty field writes nothing, so reconnecting never clears a value the user didn't retype."""

    def __init__(
        self,
        parent: QWidget | None,
        label: str,
        reason: str,
        store: str,
        fields: tuple[tuple[str, str, str], ...],
        *,
        connections: ConnectionsService,
        settings: SettingsStore,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._connections = connections
        self._settings = settings
        self._fields: list[tuple[str, QLineEdit]] = []
        self.setWindowTitle(f"Connect {label}")
        self.setModal(True)
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(10)

        head = QLabel(f"Connect {label}")
        head.setObjectName("Title")
        root.addWidget(head)
        if reason:
            why = QLabel(reason)
            why.setObjectName("Status")
            why.setWordWrap(True)
            root.addWidget(why)
        note = QLabel(
            "Paste the key below. It's saved on this machine only — never shown in chat."
        )
        note.setObjectName("Status")
        note.setWordWrap(True)
        root.addWidget(note)
        root.addSpacing(4)

        for _key, field_label, hint in fields:
            root.addWidget(QLabel(field_label))
            field = QLineEdit()
            field.setEchoMode(QLineEdit.EchoMode.Password)
            if hint:
                field.setPlaceholderText(hint)
            root.addWidget(field)
            self._fields.append((_key, field))

        row = QHBoxLayout()
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setObjectName("Primary")
        self._connect_btn.clicked.connect(self._connect)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(self._connect_btn)
        row.addWidget(cancel)
        row.addStretch(1)
        root.addLayout(row)

    def _connect(self) -> None:
        # Each NON-EMPTY field lands in its store — the value is never logged, echoed, or kept here.
        for key, field in self._fields:
            value = field.text().strip()
            if not value:
                continue
            if self._store == "secrets":
                self._connections.set_value(key, value)
            else:
                self._settings.set(key, value)
        self.accept()


def show_connect_panel(
    parent: QWidget | None,
    service_id: str,
    reason: str,
    *,
    connections: ConnectionsService,
    settings: SettingsStore,
) -> None:
    """Open the modal just-in-time connect panel for a CONNECTABLE service. An unknown id shows
    nothing — the tool layer already told the model which services are connectable."""
    spec = CONNECTABLE.get((service_id or "").strip().lower())
    if spec is None:
        return
    label, store, fields = spec
    ConnectPanel(
        parent, label, reason, store, fields, connections=connections, settings=settings
    ).exec()
