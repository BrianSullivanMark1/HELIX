"""MemoryDialog — browse and edit HELIX's long-term memory (the durable facts it keeps about you).

A simple list of facts with a delete on each, an add box, and — in a household — a picker for whose
memory to view. Backed by MemoryService (per-speaker). Part of the shell; opened from Settings.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from helix.ui.theme import CYAN, LINE, MUTED, TEXT


class MemoryDialog(QDialog):
    def __init__(self, memory, parent=None) -> None:
        super().__init__(parent)
        self._memory = memory
        self._user = ""  # which person's memory is shown ("" = you / default)
        self.setWindowTitle("HELIX — Long-term memory")
        self.setMinimumSize(560, 560)
        self.setStyleSheet(
            f"QDialog{{background:#080b0f;}} QLabel{{color:{TEXT};}}"
            f"QScrollArea{{border:none;background:transparent;}}"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)

        title = QLabel("What HELIX remembers about you")
        title.setStyleSheet(f"color:{CYAN};font-size:18px;font-weight:600;")
        root.addWidget(title)
        sub = QLabel("Durable facts it keeps in mind every conversation. Add or remove any of them.")
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{MUTED};font-size:12px;")
        root.addWidget(sub)

        # Household: a picker for whose memory, shown only when more than one person has any.
        users = [u for u in self._memory.users()]
        if len([u for u in users if u]) >= 1 and users != [""]:
            picker = QComboBox()
            picker.addItem("You / default", "")
            for u in users:
                if u:
                    picker.addItem(u.title(), u)
            picker.currentIndexChanged.connect(lambda _i: self._on_user(picker.currentData()))
            row = QHBoxLayout()
            row.addWidget(QLabel("Whose memory:"))
            row.addWidget(picker)
            row.addStretch(1)
            root.addLayout(row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._host = QWidget()
        self._host.setStyleSheet("background:transparent;")
        self._list = QVBoxLayout(self._host)
        self._list.setContentsMargins(0, 4, 8, 4)
        self._list.setSpacing(8)
        scroll.setWidget(self._host)
        root.addWidget(scroll, stretch=1)

        add_row = QHBoxLayout()
        self._add_input = QLineEdit()
        self._add_input.setPlaceholderText("Add a fact — e.g. “My daughter's name is Ada”")
        self._add_input.returnPressed.connect(self._add)
        add_btn = QPushButton("＋ Add")
        add_btn.setObjectName("Primary")
        add_btn.clicked.connect(self._add)
        add_row.addWidget(self._add_input, stretch=1)
        add_row.addWidget(add_btn)
        root.addLayout(add_row)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        brow = QHBoxLayout()
        brow.addStretch(1)
        brow.addWidget(close)
        root.addLayout(brow)

        self._reload()

    def _on_user(self, user) -> None:
        self._user = user or ""
        self._reload()

    def _reload(self) -> None:
        while self._list.count():
            item = self._list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        facts = self._memory.facts(user=self._user)
        if not facts:
            empty = QLabel("Nothing yet. Tell HELIX something lasting about you, or add it here.")
            empty.setStyleSheet(f"color:{MUTED};")
            empty.setWordWrap(True)
            self._list.addWidget(empty)
        for fact in facts:
            self._list.addWidget(self._fact_row(fact))
        self._list.addStretch(1)

    def _fact_row(self, fact: str) -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            f"QFrame{{background:rgba(13,20,27,0.6);border:1px solid {LINE};border-radius:10px;}}"
        )
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 8, 8, 8)
        label = QLabel(fact)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(label, stretch=1)
        x = QToolButton()
        x.setText("✕")
        x.setToolTip("Forget this")
        x.setCursor(Qt.CursorShape.PointingHandCursor)
        x.setStyleSheet("QToolButton{color:#9fb3ba;border:none;background:transparent;}"
                        "QToolButton:hover{color:#e0663f;}")
        x.clicked.connect(lambda _c=False, f=fact: self._delete(f))
        row.addWidget(x)
        return card

    def _add(self) -> None:
        text = self._add_input.text().strip()
        if not text:
            return
        self._add_input.clear()
        self._memory.add(text, user=self._user)
        self._reload()

    def _delete(self, fact: str) -> None:
        keep = [f for f in self._memory.facts(user=self._user) if f != fact]
        self._memory.set_facts(keep, user=self._user)
        self._reload()
