"""ConsoleView — the conversation, floating over the Presence orb (the window's living background).

The orb itself is owned by the main window and sits behind every screen. Here we drive its state and
mic-level pulse, and let the conversation float over its lower glow. Voice is optional: with no mic /
no faster-whisper the voice controls stay hidden and it's a normal text app.
"""
from __future__ import annotations

from html import escape

from PyQt6.QtCore import QEvent, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence, QPainter, QShortcut
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
from helix.ui.theme import CYAN, LINE, MUTED
from helix.ui.voice import VoiceController, split_visuals
from helix.ui.workers import QtWorker


class _BarChart(QWidget):
    """A small horizontal bar chart painted in the HELIX cyan, for a chart viz the orb shows inline."""

    def __init__(self, spec: dict) -> None:
        super().__init__()
        self._title = str(spec.get("title") or "")
        self._unit = str(spec.get("unit") or "")
        items: list[tuple[str, float]] = []
        for d in spec.get("data") or []:
            if isinstance(d, dict):
                try:
                    items.append((str(d.get("label", "")), float(d.get("value", 0) or 0)))
                except (TypeError, ValueError):
                    pass
        self._items = items[:20]
        self.setStyleSheet("background: transparent;")
        self._row_h = 26
        head = 26 if self._title else 4
        self.setMinimumHeight(head + self._row_h * max(1, len(self._items)) + 4)
        self.setMinimumWidth(380)  # else the left-aligned card collapses and the bars have no room

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        y = 2
        if self._title:
            p.setPen(QColor(CYAN))
            f = p.font(); f.setBold(True); p.setFont(f)
            p.drawText(2, y, w - 4, 22, Qt.AlignmentFlag.AlignVCenter, self._title)
            f.setBold(False); p.setFont(f)
            y += 24
        if not self._items:
            p.end()
            return
        maxv = max((v for _, v in self._items), default=0.0) or 1.0
        label_w = 104
        bar_x = label_w + 6
        bar_max = max(40, w - bar_x - 64)
        for label, val in self._items:
            p.setPen(QColor(MUTED))
            p.drawText(0, y, label_w, self._row_h,
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
            bw = max(2, int(bar_max * (val / maxv)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(63, 224, 224, 205))
            p.drawRoundedRect(QRectF(bar_x, y + 5.0, float(bw), self._row_h - 10.0), 3.0, 3.0)
            p.setPen(QColor("#d4ecec"))
            p.drawText(bar_x + bw + 6, y, 60, self._row_h,
                       Qt.AlignmentFlag.AlignVCenter, f"{self._unit}{val:g}")
            y += self._row_h
        p.end()


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
        self._cancelled = False  # set by a 'stop' — a pending reply is shown but not spoken
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

        # The conversation fills the full height — text scrolls all the way up — floating over the orb's
        # glow (bubbles are semi-opaque so they stay legible). The controls sit beneath it.
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
        # A tap on the empty conversation area is a tap on the orb behind it (tap to talk); clicks on a
        # bubble are consumed by it for text selection and never reach these filters.
        self._transcript.installEventFilter(self)
        self._scroll.viewport().installEventFilter(self)
        root.addWidget(self._scroll, stretch=1)

        # Status pill + voice toggle, beneath the conversation, over the orb's lower glow.
        self.status = QLabel("Ready when you are.")
        self.status.setObjectName("Status")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setStyleSheet(
            "QLabel#Status{background:rgba(8,11,15,0.92);color:#d4ecec;"
            "border:1px solid rgba(63,224,224,0.28);border-radius:11px;padding:5px 16px;}"
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
            self._voice.stopRequested.connect(self._on_voice_stop)
            if self.orb is not None:
                self._voice.level.connect(self.orb.set_level)
            self._voice.start_if_enabled()

        # Esc interrupts a reply, anywhere in the Console.
        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc.activated.connect(self._stop)

        self.refresh_key_state()
        self._refresh_voice_ui()

    # ----- public -----
    def refresh_key_state(self) -> None:
        has_key = bool((self._settings.get("claude_api_key") or "").strip())
        self._banner.setVisible(not has_key)

    def _on_tap(self) -> None:
        # A tap on empty space is a tap on the orb behind it: interrupt while HELIX is busy, else toggle
        # voice. Clicks on buttons/input/bubbles are consumed by those children and never get here.
        if self._voice is not None and self._voice.is_active():
            self._stop()
        else:
            self.toggle_voice()

    def mousePressEvent(self, _event) -> None:
        self._on_tap()

    def eventFilter(self, obj, event) -> bool:
        # The full-height transcript covers the orb, so route taps on its empty area to the orb too.
        if event.type() == QEvent.Type.MouseButtonPress and obj in (
            self._transcript, self._scroll.viewport()
        ):
            self._on_tap()
        return super().eventFilter(obj, event)

    def _stop(self) -> None:
        """Interrupt the current reply: stop speaking now, and don't speak one that's still pending."""
        if self._voice is not None:
            self._voice.interrupt()
        if self._busy:
            self._cancelled = True
        self.status.setText("Stopped.")

    def _on_voice_stop(self) -> None:
        # The user said "stop" — the controller already hushed any speech; cancel a pending reply too.
        if self._busy:
            self._cancelled = True
        self.status.setText("Stopped.")

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
        # A near-solid dark pill so the label reads over the bright orb (cyan-on-cyan was invisible).
        edge = "#3fe0e0" if on else "#26323b"
        txt = "#3fe0e0" if on else "#aebcc3"
        self._voice_btn.setStyleSheet(
            f"QPushButton{{background:rgba(8,11,15,0.93);border:1px solid {edge};border-radius:14px;"
            f"color:{txt};padding:8px 18px;}} QPushButton:hover{{border-color:#3fe0e0;}}"
        )
        self._voice_btn.setText("🔊 Voice on — say “HELIX”" if on else "🔇 Voice off")
        self._voice_btn.setToolTip(
            "Listening for “HELIX”. Say “stop” to interrupt, “goodbye” to end; tap the orb to stop/mute."
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
        self._cancelled = False
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
        # Split the prose from any table/chart blocks: prose is shown AND spoken; visuals are only shown.
        spoken, visuals = split_visuals(str(text))
        if spoken or not visuals:
            self._add_bubble("helix", spoken or str(text))
        for spec in visuals:
            self._add_visual(spec)
        if self._cancelled:  # the user said stop while this was generating — show it, don't speak it
            self._cancelled = False
            if self._voice is not None:
                self._voice.idle()
            self._idle_status()
            return
        if self._voice is not None and self._voice.enabled():
            self._voice.speak(spoken)  # speak the prose only — the table/chart is shown, never read
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

    def _add_visual(self, spec: dict) -> None:
        """Render a table or chart inline in the transcript (shown, never spoken)."""
        kind = spec.get("type")
        if kind == "chart":
            card = QFrame()
            card.setStyleSheet(
                f"QFrame{{background:rgba(13,20,27,0.86);border:1px solid {CYAN};border-radius:12px;}}"
            )
            lay = QVBoxLayout(card)
            lay.setContentsMargins(14, 12, 14, 12)
            lay.addWidget(_BarChart(spec))
            self._insert_visual(card)
        elif kind == "table":
            self._insert_visual(self._table_widget(spec))

    def _table_widget(self, spec: dict) -> QLabel:
        cols = spec.get("columns") or []
        rows = spec.get("rows") or []
        title = str(spec.get("title") or "")
        parts: list[str] = []
        if title:
            parts.append(
                f"<div style='color:{CYAN};font-weight:600;margin-bottom:6px'>{escape(title)}</div>"
            )
        parts.append("<table border='1' cellspacing='0' cellpadding='6' style='border-color:#26323b'>")
        if cols:
            parts.append(
                "<tr>"
                + "".join(f"<th style='color:{CYAN};text-align:left'>{escape(str(c))}</th>" for c in cols)
                + "</tr>"
            )
        for r in rows:
            cells = r if isinstance(r, (list, tuple)) else [r]
            parts.append(
                "<tr>" + "".join(f"<td style='color:#e2edf1'>{escape(str(c))}</td>" for c in cells) + "</tr>"
            )
        parts.append("</table>")
        lbl = QLabel("".join(parts))
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setStyleSheet(
            f"QLabel{{background:rgba(13,20,27,0.86);border:1px solid {CYAN};"
            "border-radius:12px;padding:10px 14px;}}"
        )
        lbl.setMaximumWidth(560)
        return lbl

    def _insert_visual(self, widget: QWidget) -> None:
        rowlay = QHBoxLayout()
        rowlay.setContentsMargins(0, 0, 0, 0)
        rowlay.addWidget(widget)
        rowlay.addStretch(1)
        self._tlayout.insertLayout(self._tlayout.count() - 1, rowlay)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
