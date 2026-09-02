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

from helix.domain.connections import Connection, looks_like_mispaste
from helix.ports.stores import SettingsStore
from helix.services.connections import CONNECTABLE, ConnectionsService
from helix.ui.theme import MUTED


def _mispaste_warning(values: list[str], skip: list[bool] | None = None) -> str:
    """The first mis-paste warning among the non-empty values, or empty if they all look plausible.
    `skip[i]` exempts a field — one whose declared value IS a URL, or one the user didn't touch."""
    for i, v in enumerate(values):
        if v and not (skip and skip[i]):
            warn = looks_like_mispaste(v)
            if warn:
                return warn
    return ""


def _expects_url(*words: str) -> bool:
    """True if a connection field's own naming says its value is a URL (SUPABASE_URL, a webhook
    address) — warning 'that looks like a web address' on those would be actively wrong advice."""
    return any("url" in (w or "").lower() or "webhook" in (w or "").lower() for w in words)


class ConnectionsDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        conns: list[Connection],
        get_value: Callable[[str], str],
        set_value: Callable[[str, str], None],
        is_managed: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._set_value = set_value
        self._fields: list[tuple[str, QLineEdit]] = []
        self._warned: tuple[str, ...] | None = None  # values already warned about (mis-paste guard)
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

        self._exempt: list[bool] = []   # URL-typed fields never trigger the mis-paste guard
        self._initial: list[str] = []   # prefilled values — only what the user CHANGES is scanned
        for c in conns:
            # A key HELIX already holds centrally (ANTHROPIC_API_KEY is the common one — the coder is
            # told to declare it) is shown as ALREADY PROVIDED rather than as another blank to fill,
            # because the value in the box came from Settings, not from this panel.
            #
            # "Already connected" is a claim about POSSESSION, so it is gated on the value, never on
            # is_managed alone: that predicate only tests membership in the container's static managed
            # map, which registers ANTHROPIC/CLAUDE/TRIPO/VOYAGE/BLOCKADE unconditionally whether or
            # not any of them has ever been set. A subscription-only install has no claude_api_key, so
            # membership-only would tell that user "leave it as is" over an EMPTY box — while the
            # launcher card that opened this panel is lit amber "Connect" from the very same empty
            # value. An unset managed key keeps its own hint and reads as the blank it actually is.
            current = get_value(c.key) or ""
            managed = is_managed is not None and bool(is_managed(c.key)) and bool(current.strip())
            # (a distinct name — `note` above is the panel's intro QLabel, still alive in this scope)
            aside = "already connected in HELIX — leave it as is" if managed else c.hint
            label = QLabel(c.label + (f"  ({aside})" if aside else ""))
            root.addWidget(label)
            field = QLineEdit()
            field.setEchoMode(QLineEdit.EchoMode.Password)
            if c.hint:
                field.setPlaceholderText(c.hint)
            field.setText(current)
            root.addWidget(field)
            self._fields.append((c.key, field))
            self._exempt.append(_expects_url(c.key, c.label, c.hint))
            self._initial.append(current.strip())

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
        # Mis-paste guard: warn ONCE on an obviously-wrong value (a URL where a key belongs) and hold
        # the save; an unchanged second click means the user insists, so save it anyway. Only values
        # the user CHANGED this session are scanned (a stored value must never re-warn on every
        # open), and URL-typed fields are exempt (their correct value IS a web address).
        values = tuple(field.text().strip() for _key, field in self._fields)
        skip = [ex or v == init for ex, init, v in zip(self._exempt, self._initial, values)]
        warn = _mispaste_warning(list(values), skip)
        if warn and values != self._warned:
            self._warned = values
            self._status.setText(warn + " Save again to keep it anyway.")
            return
        # Only what the user actually CHANGED is written. A prefilled value can come from somewhere
        # other than the secrets store — a HELIX-managed key (the Claude key lives in Settings and is
        # served through value()) prefills this panel too, and writing it back would COPY it into the
        # secrets store, where it wins over Settings forever: rotating the key in Settings would then
        # never reach this build again, and it would keep injecting the frozen snapshot. Re-saving an
        # untouched field is a no-op for every other key anyway, so the guard costs nothing. Clearing
        # a field is still a change, so an override the user wants gone is written away as "".
        for (key, field), value, initial in zip(self._fields, values, self._initial):
            if value != initial:
                self._set_value(key, value)
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
        self._warned: tuple[str, ...] | None = None  # values already warned about (mis-paste guard)
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

        self._exempt: list[bool] = []  # URL-typed fields never trigger the mis-paste guard
        for _key, field_label, hint in fields:
            root.addWidget(QLabel(field_label))
            field = QLineEdit()
            field.setEchoMode(QLineEdit.EchoMode.Password)
            if hint:
                field.setPlaceholderText(hint)
            root.addWidget(field)
            self._fields.append((_key, field))
            self._exempt.append(_expects_url(_key, field_label, hint))

        self._status = QLabel("")
        self._status.setObjectName("Status")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{MUTED};")
        root.addWidget(self._status)

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
        # Mis-paste guard: an obviously-wrong value (a URL where a key belongs) warns ONCE and holds
        # the save; an unchanged second click means the user insists, so it saves anyway. URL-typed
        # fields are exempt — their correct value IS a web address.
        values = tuple(field.text().strip() for _key, field in self._fields)
        warn = _mispaste_warning(list(values), self._exempt)
        if warn and values != self._warned:
            self._warned = values
            self._status.setText(warn + " Connect again to save anyway.")
            return
        # Each NON-EMPTY field lands in its store — the value is never logged, echoed, or kept here.
        for (key, field), value in zip(self._fields, values):
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
