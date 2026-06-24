"""ConsoleView — the orb home + the conversation. The default screen.

Voice is layered on but optional: a single Voice toggle arms the hands-free wake word ("HELIX"), each
reply is spoken back, and a hold-to-talk button gives a manual capture. With no mic / no faster-whisper
the voice controls simply stay hidden and the Console is a normal text app.
"""
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

from helix.ports.speech import SpeechIn, SpeechOut
from helix.ports.stores import SettingsStore
from helix.services.conversation import ConversationService
from helix.ui.orb import OrbState, PresenceOrb
from helix.ui.theme import CYAN, LINE, PANEL, PANEL_HI, TEXT
from helix.ui.voice import VoiceController
from helix.ui.workers import QtWorker


class ConsoleView(QWidget):
    openSettingsRequested = pyqtSignal()

    def __init__(
        self,
        conversation: ConversationService,
        settings: SettingsStore,
        speech_in: SpeechIn | None = None,
        speech_out: SpeechOut | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("Console")
        self._conversation = conversation
        self._settings = settings
        self._workers: set[QtWorker] = set()
        self._busy = False

        # Voice is created only when both speech ports are present; it self-reports what it can do.
        self._voice: VoiceController | None = None
        if speech_in is not None and speech_out is not None:
            self._voice = VoiceController(speech_in, speech_out, settings, self)

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

        # Orb — the Presence leads the screen, so give it the most room.
        self.orb = PresenceOrb()
        root.addWidget(self.orb, stretch=3)
        self.status = QLabel("Ready when you are.")
        self.status.setObjectName("Status")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status)

        # Voice toggle (centered under the orb) — also the in-app instructions for how voice works.
        self._voice_btn = QPushButton("🔊 Voice")
        self._voice_btn.clicked.connect(self._toggle_voice)
        vrow = QHBoxLayout()
        vrow.addStretch(1)
        vrow.addWidget(self._voice_btn)
        vrow.addStretch(1)
        root.addLayout(vrow)

        # Transcript
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._transcript = QWidget()
        self._tlayout = QVBoxLayout(self._transcript)
        self._tlayout.setContentsMargins(0, 6, 0, 6)
        self._tlayout.setSpacing(8)
        self._tlayout.addStretch(1)
        self._scroll.setWidget(self._transcript)
        root.addWidget(self._scroll, stretch=2)

        # Input row: hold-to-talk · text · send
        row = QHBoxLayout()
        self._talk = QPushButton("🎤 Hold to Talk")
        self._talk.pressed.connect(self._talk_start)
        self._talk.released.connect(self._talk_stop)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Tell HELIX what to build…")
        self._input.returnPressed.connect(self._send)
        send = QPushButton("Send")
        send.setObjectName("Primary")
        send.clicked.connect(self._send)
        row.addWidget(self._talk)
        row.addWidget(self._input)
        row.addWidget(send)
        root.addLayout(row)

        # Voice signals drive the orb, the status line, and the live mic level.
        if self._voice is not None:
            self._voice.recognized.connect(self._on_recognized)
            self._voice.stateChanged.connect(self._on_voice_state)
            self._voice.start_if_enabled()

        self.refresh_key_state()
        self._refresh_voice_ui()

    # ----- public -----
    def refresh_key_state(self) -> None:
        has_key = bool((self._settings.get("claude_api_key") or "").strip())
        self._banner.setVisible(not has_key)

    # ----- voice controls -----
    def _refresh_voice_ui(self) -> None:
        """Show/enable the voice controls according to what the host actually supports."""
        voice = self._voice
        if voice is None or not voice.supported():
            self._voice_btn.setVisible(False)
            self._talk.setVisible(False)
            return
        on = voice.enabled()
        self._voice_btn.setVisible(True)
        self._voice_btn.setObjectName("Primary" if on else "")
        # Re-polish so the dynamic objectName restyles the button.
        self._voice_btn.style().unpolish(self._voice_btn)
        self._voice_btn.style().polish(self._voice_btn)
        self._voice_btn.setText("🔊 Voice on — say “HELIX”" if on else "🔇 Voice off")
        self._voice_btn.setToolTip(
            "Listening for “HELIX”. Say “goodbye” to end the conversation, or click to mute."
            if on
            else "Turn on hands-free voice — then just say “HELIX”."
        )
        self._talk.setVisible(True)
        # Enable on capability only — never disable mid-hold, or a held button won't emit 'released'.
        self._talk.setEnabled(voice.can_listen())

    def _toggle_voice(self) -> None:
        voice = self._voice
        if voice is None:
            return
        target = not voice.enabled()
        if target and not voice.supported():
            self.status.setText("Voice needs a microphone and faster-whisper installed.")
            return
        started = voice.set_enabled(target)
        self._refresh_voice_ui()
        if not target:
            self.status.setText("Voice off.")
        elif started:
            self.status.setText("Listening — say “HELIX”.")
        elif voice.restart_required():
            self.status.setText("Voice saved — restart HELIX to start hands-free listening.")
        else:
            self.status.setText("Voice unavailable on this machine.")

    def _on_voice_state(self, state: object) -> None:
        self.orb.set_state(state if isinstance(state, OrbState) else OrbState.IDLE)
        if state == OrbState.LISTENING:
            self.status.setText("Listening…")
        elif state == OrbState.THINKING:
            self.status.setText("Thinking…")
        elif state == OrbState.SPEAKING:
            self.status.setText("Speaking…")
        else:  # IDLE
            self.status.setText(
                "Listening for “HELIX”…" if self._voice and self._voice.enabled()
                else "Ready when you are."
            )

    def _talk_start(self) -> None:
        if self._voice is not None and not self._busy:
            self._voice.ptt_start()

    def _talk_stop(self) -> None:
        if self._voice is not None:
            self._voice.ptt_stop()

    def _on_recognized(self, text: str) -> None:
        # A command captured by voice — show it and run it through the same path as a typed message.
        self._submit(str(text), from_voice=True)

    # ----- conversation -----
    def _send(self) -> None:
        text = self._input.text().strip()
        self._input.clear()
        self._submit(text, from_voice=False)

    def _submit(self, text: str, *, from_voice: bool) -> None:
        text = (text or "").strip()
        if not text or self._busy:
            return
        self._add_bubble("you", text)
        self._busy = True
        self._input.setEnabled(False)
        self._talk.setEnabled(False)
        if self._voice is not None:
            if not from_voice:
                self._voice.begin_turn()  # voice path already went quiet when it captured the command
        else:
            self.orb.set_state(OrbState.THINKING)
            self.status.setText("Thinking…")

        worker = QtWorker(lambda emit: self._conversation.run_turn(text, on_progress=emit))
        # Keep a strong reference until the QThread *actually* finishes (see _retire) so the GC can't
        # destroy a live QThread and crash the process.
        self._workers.add(worker)
        worker.progress.connect(self.status.setText)
        worker.finished_ok.connect(self._on_reply)
        worker.failed.connect(self._on_fail)
        worker.finished.connect(lambda w=worker: self._retire(w))
        worker.start()

    def _on_reply(self, text: object) -> None:
        self._add_bubble("helix", str(text))
        if self._voice is not None and self._voice.enabled():
            self._voice.speak(str(text))  # speak the reply, then re-arm the mic
        elif self._voice is not None:
            self._voice.idle()
            self._idle_status()
        else:
            self._idle()

    def _on_fail(self, err: str) -> None:
        self._add_bubble("helix", f"⚠  {err}")
        if self._voice is not None:
            self._voice.idle()
            self._idle_status()
        else:
            self._idle()

    def _idle(self) -> None:
        self.orb.set_state(OrbState.IDLE)
        self._idle_status()

    def _idle_status(self) -> None:
        self.status.setText(
            "Listening for “HELIX”…" if self._voice and self._voice.enabled()
            else "Ready when you are."
        )

    def _retire(self, worker: QtWorker) -> None:
        # On QThread.finished — the thread has stopped, so it is safe to drop the reference.
        self._workers.discard(worker)
        worker.deleteLater()
        self._busy = False
        self._input.setEnabled(True)
        self._input.setFocus()
        self._refresh_voice_ui()

    def shutdown(self) -> None:
        """Wait briefly for any in-flight worker so we never destroy a running QThread on close."""
        if self._voice is not None:
            self._voice.shutdown()
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
