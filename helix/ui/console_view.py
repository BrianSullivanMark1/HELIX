"""ConsoleView — the orb home + the conversation. The default screen."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from helix.ports.stores import SettingsStore
from helix.services.conversation import ConversationService
from helix.ui.orb import OrbState, PresenceOrb
from helix.ui.theme import CYAN, LINE, PANEL, PANEL_HI, TEXT
from helix.ui.workers import QtWorker


class ConsoleView(QWidget):
    openSettingsRequested = pyqtSignal()

    def __init__(self, conversation: ConversationService, settings: SettingsStore) -> None:
        super().__init__()
        self.setObjectName("Console")
        self._conversation = conversation
        self._settings = settings
        self._workers: set[QtWorker] = set()
        self._busy = False

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 18, 28, 22)
        root.setSpacing(12)

        # Key banner (only until a key is set)
        self._banner = QFrame()
        self._banner.setObjectName("Card")
        brow = QHBoxLayout(self._banner)
        brow.setContentsMargins(16, 10, 12, 10)
        msg = QLabel("Add your Claude API key to start building apps.")
        msg.setObjectName("Banner")
        open_btn = QPushButton("Open Settings")
        open_btn.setObjectName("Primary")
        open_btn.clicked.connect(self.openSettingsRequested.emit)
        brow.addWidget(msg)
        brow.addStretch(1)
        brow.addWidget(open_btn)
        root.addWidget(self._banner)

        # Orb
        self.orb = PresenceOrb()
        root.addWidget(self.orb, stretch=2)
        self.status = QLabel("Ready when you are.")
        self.status.setObjectName("Status")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status)

        # Transcript
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._transcript = QWidget()
        self._tlayout = QVBoxLayout(self._transcript)
        self._tlayout.setContentsMargins(0, 6, 0, 6)
        self._tlayout.setSpacing(8)
        self._tlayout.addStretch(1)
        self._scroll.setWidget(self._transcript)
        root.addWidget(self._scroll, stretch=3)

        # Input row
        row = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText("Tell HELIX what to build…")
        self._input.returnPressed.connect(self._send)
        send = QPushButton("Send")
        send.setObjectName("Primary")
        send.clicked.connect(self._send)
        row.addWidget(self._input)
        row.addWidget(send)
        root.addLayout(row)

        self.refresh_key_state()

    # ----- public -----
    def refresh_key_state(self) -> None:
        has_key = bool((self._settings.get("claude_api_key") or "").strip())
        self._banner.setVisible(not has_key)

    # ----- conversation -----
    def _send(self) -> None:
        text = self._input.text().strip()
        if not text or self._busy:
            return
        self._input.clear()
        self._add_bubble("you", text)
        self._busy = True
        self._input.setEnabled(False)
        self.orb.set_state(OrbState.THINKING)
        self.status.setText("Thinking…")

        worker = QtWorker(lambda emit: self._conversation.run_turn(text, on_progress=emit))
        # Keep a strong reference until the QThread *actually* finishes. Dropping it inside the
        # finished_ok/failed slot (which fires while run() is still unwinding) would let the GC
        # destroy a live QThread and crash the process. Lifetime is gated on the built-in
        # `finished` signal, which is delivered only after the thread has truly stopped.
        self._workers.add(worker)
        worker.progress.connect(self.status.setText)
        worker.finished_ok.connect(self._on_reply)
        worker.failed.connect(self._on_fail)
        worker.finished.connect(lambda w=worker: self._retire(w))
        worker.start()

    def _on_reply(self, text: object) -> None:
        self._add_bubble("helix", str(text))
        self._idle()

    def _on_fail(self, err: str) -> None:
        self._add_bubble("helix", f"⚠  {err}")
        self._idle()

    def _idle(self) -> None:
        self.orb.set_state(OrbState.IDLE)
        self.status.setText("Ready when you are.")

    def _retire(self, worker: QtWorker) -> None:
        # On QThread.finished — the thread has stopped, so it is safe to drop the reference.
        self._workers.discard(worker)
        worker.deleteLater()
        self._busy = False
        self._input.setEnabled(True)
        self._input.setFocus()

    def shutdown(self) -> None:
        """Wait briefly for any in-flight worker so we never destroy a running QThread on close."""
        for worker in list(self._workers):
            worker.wait(3000)

    # ----- transcript rendering -----
    def _add_bubble(self, who: str, text: str) -> None:
        bubble = QLabel(text)
        bubble.setWordWrap(True)
        bubble.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bubble.setMaximumWidth(560)
        is_user = who == "you"
        bg = PANEL_HI if is_user else PANEL
        edge = LINE if is_user else CYAN
        bubble.setStyleSheet(
            f"QLabel{{background:{bg};color:{TEXT};border:1px solid {edge};"
            f"border-radius:12px;padding:10px 14px;}}"
        )
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if is_user:
            row.addStretch(1)
            row.addWidget(bubble)
        else:
            row.addWidget(bubble)
            row.addStretch(1)
        # insert before the trailing stretch
        self._tlayout.insertLayout(self._tlayout.count() - 1, row)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
