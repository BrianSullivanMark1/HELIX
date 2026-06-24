"""ConsoleView — the conversation, floating over the Presence orb (the window's living background).

The orb itself is owned by the main window and sits behind every screen. Here we drive its state and
mic-level pulse, and let the conversation float over its lower glow. Voice is optional: with no mic /
no faster-whisper the voice controls stay hidden and it's a normal text app.
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
from helix.ui.theme import CYAN, LINE
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
        orb: PresenceOrb | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("Console")
        self._conversation = conversation
        self._settings = settings
        self._workers: set[QtWorker] = set()
        self._busy = False
        self.orb = orb  # shared with the whole window; owned by HelixMainWindow

        self._voice: VoiceController | None = None
        if speech_in is not None and speech_out is not None:
            self._voice = VoiceController(speech_in, speech_out, settings, self)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 14, 28, 18)
        root.setSpacing(10)

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

        # The orb's bright centre glows through this gap — and it's the clickable "tap to talk" zone.
        root.addStretch(3)

        # Status + voice toggle, centred over the orb. A translucent pill keeps the text legible
        # against the orb's glow, and the chip hugs the text rather than spanning the window.
        self.status = QLabel("Ready when you are.")
        self.status.setObjectName("Status")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setStyleSheet(
            "QLabel#Status{background:rgba(8,11,15,0.58);border-radius:11px;padding:4px 14px;}"
        )
        srow = QHBoxLayout()
        srow.addStretch(1)
        srow.addWidget(self.status)
        srow.addStretch(1)
        root.addLayout(srow)

        self._voice_btn = QPushButton("🔊 Voice")
        self._voice_btn.clicked.connect(self.toggle_voice)
        vrow = QHBoxLayout()
        vrow.addStretch(1)
        vrow.addWidget(self._voice_btn)
        vrow.addStretch(1)
        root.addLayout(vrow)

        root.addStretch(1)

        # Transcript — floats over the orb's lower glow (bubbles are semi-opaque so text stays legible).
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("background: transparent;")
        self._scroll.viewport().setStyleSheet("background: transparent;")
        self._transcript = QWidget()
        self._transcript.setStyleSheet("background: transparent;")
        self._tlayout = QVBoxLayout(self._transcript)
        self._tlayout.setContentsMargins(0, 6, 0, 6)
        self._tlayout.setSpacing(8)
        self._tlayout.addStretch(1)
        self._scroll.setWidget(self._transcript)
        root.addWidget(self._scroll, stretch=3)

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

        # Voice signals drive the orb (state + live mic level), the status line, and recognized commands.
        if self._voice is not None:
            self._voice.recognized.connect(self._on_recognized)
            self._voice.stateChanged.connect(self._on_voice_state)
            if self.orb is not None:
                self._voice.level.connect(self.orb.set_level)
            self._voice.start_if_enabled()

        self.refresh_key_state()
        self._refresh_voice_ui()

    # ----- public -----
    def refresh_key_state(self) -> None:
        has_key = bool((self._settings.get("claude_api_key") or "").strip())
        self._banner.setVisible(not has_key)

    def mousePressEvent(self, _event) -> None:
        # A tap on the Console's empty space is a tap on the orb glowing behind it — toggle voice.
        # Clicks on the buttons, input, or transcript are consumed by those children and never arrive here.
        self.toggle_voice()

    def toggle_voice(self) -> None:
        """Flip hands-free voice on/off. Wired to both the Voice button and a tap on the orb."""
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

    # ----- voice controls -----
    def _refresh_voice_ui(self) -> None:
        voice = self._voice
        if voice is None or not voice.supported():
            self._voice_btn.setVisible(False)
            self._talk.setVisible(False)
            return
        on = voice.enabled()
        self._voice_btn.setVisible(True)
        self._voice_btn.setObjectName("Primary" if on else "")
        self._voice_btn.style().unpolish(self._voice_btn)
        self._voice_btn.style().polish(self._voice_btn)
        self._voice_btn.setText("🔊 Voice on — say “HELIX”" if on else "🔇 Voice off")
        self._voice_btn.setToolTip(
            "Listening for “HELIX”. Say “goodbye” to end, click the orb or here to mute."
            if on
            else "Turn on hands-free voice — then just say “HELIX” (or tap the orb)."
        )
        self._talk.setVisible(True)
        self._talk.setEnabled(voice.can_listen())

    def _on_voice_state(self, state: object) -> None:
        if self.orb is not None:
            self.orb.set_state(state if isinstance(state, OrbState) else OrbState.IDLE)
        if state == OrbState.LISTENING:
            self.status.setText("Listening…")
        elif state == OrbState.THINKING:
            self.status.setText("Thinking…")
        elif state == OrbState.SPEAKING:
            self.status.setText("Speaking…")
        else:
            self._idle_status()

    def _talk_start(self) -> None:
        if self._voice is not None and not self._busy:
            self._voice.ptt_start()

    def _talk_stop(self) -> None:
        if self._voice is not None:
            self._voice.ptt_stop()

    def _on_recognized(self, text: str) -> None:
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
        elif self.orb is not None:
            self.orb.set_state(OrbState.THINKING)
            self.status.setText("Thinking…")

        worker = QtWorker(lambda emit: self._conversation.run_turn(text, on_progress=emit))
        # Strong ref until the QThread truly finishes (see _retire) so the GC can't kill a live thread.
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
        if self.orb is not None:
            self.orb.set_state(OrbState.IDLE)
        self._idle_status()

    def _idle_status(self) -> None:
        self.status.setText(
            "Listening for “HELIX”…" if self._voice and self._voice.enabled()
            else "Ready when you are."
        )

    def _retire(self, worker: QtWorker) -> None:
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
        # Semi-opaque so the orb glows through behind the words, but text stays readable.
        bg = "rgba(18,27,36,0.82)" if is_user else "rgba(13,20,27,0.82)"
        edge = LINE if is_user else CYAN
        bubble.setStyleSheet(
            f"QLabel{{background:{bg};color:#e2edf1;border:1px solid {edge};"
            f"border-radius:12px;padding:10px 14px;}}"
        )
        rowlay = QHBoxLayout()
        rowlay.setContentsMargins(0, 0, 0, 0)
        if is_user:
            rowlay.addStretch(1)
            rowlay.addWidget(bubble)
        else:
            rowlay.addWidget(bubble)
            rowlay.addStretch(1)
        self._tlayout.insertLayout(self._tlayout.count() - 1, rowlay)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
