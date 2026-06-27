"""ConsoleView — the conversation, floating over the Presence orb (the window's living background).

The orb itself is owned by the main window and sits behind every screen. Here we drive its state and
mic-level pulse, and let the conversation float over its lower glow. Voice is optional: with no mic /
no faster-whisper the voice controls stay hidden and it's a normal text app.
"""
from __future__ import annotations

import re
from html import escape

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QShortcut,
)
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

from typing import TYPE_CHECKING

from helix.ports.speech import SpeechIn, SpeechOut
from helix.ports.stores import SettingsStore
from helix.services.cancel import CancelToken
from helix.services.conversation import ConversationService
from helix.ui.orb import OrbState, PresenceOrb
from helix.ui.theme import CYAN, CYAN_DIM, LINE, MUTED, TEXT
from helix.ui.voice import VoiceController, is_stop, split_visuals
from helix.ui.workers import QtWorker

if TYPE_CHECKING:
    from helix.services.cancel import BuildHandle
    from helix.services.forge import ForgeService

# Recognize a yes / no when HELIX asks whether to remove half-built work after a stop.
_YES = re.compile(r"\b(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please|do\s+it|go\s+ahead|remove|delete|"
                  r"discard|get\s+rid|trash|scrap)\b", re.IGNORECASE)
_NO = re.compile(r"\b(?:no|nope|nah|keep|leave\s+it|don'?t|cancel|never\s*mind)\b", re.IGNORECASE)
_NEG = re.compile(r"\b(?:not|never)\b|n'?t\b", re.IGNORECASE)  # a negation is never a 'remove'


def _cleanup_answer(text: str) -> str:
    """Classify a reply to 'remove the half-built X?' as 'yes' / 'no' / 'neither'. Safe by default: a
    negation ("not sure", "don't") or anything unclear NEVER means remove — only a clean yes does."""
    text = text or ""
    if _NEG.search(text):
        return "no" if _NO.search(text) else "neither"
    if _YES.search(text) and not _NO.search(text):
        return "yes"
    if _NO.search(text):
        return "no"
    return "neither"


_VLABEL = int(Qt.AlignmentFlag.AlignVCenter)
_RLABEL = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
_HCENTER = int(Qt.AlignmentFlag.AlignHCenter)
# A small HUD palette for multi-segment charts (pie/donut), cycled in order.
_SEGMENTS = (
    (63, 224, 224), (245, 166, 35), (120, 200, 255),
    (46, 196, 150), (200, 120, 255), (255, 120, 150), (150, 220, 120),
)


class _ChartWidget(QWidget):
    """An inline HUD chart the orb SHOWS (never speaks): bar (default), line, area, or pie/donut —
    painted in QPainter with a cyan glow and an eased grow-in. The kind comes from the optional
    spec["kind"]; anything unknown (or a painter error) falls back to bars, so the channel never breaks.
    """

    _KINDS = {"bar", "column", "line", "area", "pie", "donut"}

    def __init__(self, spec: dict) -> None:
        super().__init__()
        self._title = str(spec.get("title") or "")
        self._unit = str(spec.get("unit") or "")
        kind = str(spec.get("kind") or "bar").lower()
        self._kind = kind if kind in self._KINDS else "bar"
        items: list[tuple[str, float]] = []
        for d in spec.get("data") or []:
            if isinstance(d, dict):
                try:
                    items.append((str(d.get("label", "")), float(d.get("value", 0) or 0)))
                except (TypeError, ValueError):
                    pass
        self._items = items[:24]
        self.setStyleSheet("background: transparent;")
        self._row_h = 28
        self._head = 28 if self._title else 6
        n = max(1, len(self._items))
        if self._kind in ("pie", "donut"):
            self.setMinimumHeight(self._head + 206)
            self.setMinimumWidth(400)
        elif self._kind in ("line", "area"):
            self.setMinimumHeight(self._head + 172)
            self.setMinimumWidth(440)
        else:
            self.setMinimumHeight(self._head + self._row_h * n + 8)
            self.setMinimumWidth(400)
        # Eased grow-in: values rise from zero the first time the card is shown.
        self._t = 0.0
        self._started = False
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(640)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_t)

    def _set_t(self, v: object) -> None:
        self._t = float(v)  # type: ignore[arg-type]
        self.update()

    def showEvent(self, event) -> None:
        if not self._started:
            self._started = True
            self._anim.start()
        super().showEvent(event)

    def render_now(self) -> None:
        """Skip the animation and paint at full value — used by the offscreen render test."""
        self._started = True
        self._t = 1.0
        self.update()

    # ----- painting -----
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = self._draw_title(p)
        if self._items:
            area = QRectF(0.0, float(top), float(self.width()), float(self.height() - top))
            try:
                if self._kind in ("pie", "donut"):
                    self._paint_pie(p, area, donut=self._kind == "donut")
                elif self._kind in ("line", "area"):
                    self._paint_line(p, area, fill=self._kind == "area")
                else:
                    self._paint_bars(p, area)
            except Exception:  # a chart must never crash the transcript — fall back to bars
                self._paint_bars(p, area)
        p.end()

    def _draw_title(self, p: QPainter) -> int:
        if not self._title:
            return 4
        p.setPen(QColor(CYAN))
        f = p.font(); f.setBold(True); p.setFont(f)
        p.drawText(2, 2, self.width() - 4, 22, _VLABEL, self._title)
        f.setBold(False); p.setFont(f)
        return self._head

    def _paint_bars(self, p: QPainter, area: QRectF) -> None:
        maxv = max((v for _, v in self._items), default=0.0) or 1.0
        x0, w = int(area.left()), self.width()
        label_w, gap = 108, 8
        bar_x = x0 + label_w + gap
        bar_max = max(40, w - bar_x - 66)
        y = int(area.top())
        rh = self._row_h
        for label, val in self._items:
            p.setPen(QColor(MUTED))
            p.drawText(x0, y, label_w, rh, _RLABEL, label)
            full = bar_max * (val / maxv)
            bw = max(2.0, full * self._t)
            rect = QRectF(float(bar_x), y + 6.0, bw, rh - 12.0)
            grad = QLinearGradient(rect.topLeft(), rect.topRight())
            grad.setColorAt(0.0, QColor(CYAN_DIM))
            grad.setColorAt(1.0, QColor(CYAN))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(63, 224, 224, 38))  # soft outer glow
            p.drawRoundedRect(rect.adjusted(-1.5, -1.5, 1.5, 1.5), 4.0, 4.0)
            p.setBrush(grad)
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor("#d4ecec"))
            p.drawText(int(bar_x + bw) + 8, y, 64, rh, _VLABEL, f"{self._unit}{val:g}")
            y += rh

    def _paint_line(self, p: QPainter, area: QRectF, fill: bool) -> None:
        vals = [v for _, v in self._items]
        maxv = max(vals, default=0.0)
        minv = min(vals + [0.0])
        span = (maxv - minv) or 1.0
        pl, pr, pt_, pb = 10.0, 14.0, 8.0, 22.0
        plot = QRectF(area.left() + pl, area.top() + pt_,
                      area.width() - pl - pr, area.height() - pt_ - pb)
        p.setPen(QPen(QColor(LINE), 1))  # gridlines
        for i in range(4):
            gy = plot.top() + plot.height() * i / 3.0
            p.drawLine(QPointF(plot.left(), gy), QPointF(plot.right(), gy))
        n = len(self._items)
        pts = []
        for i, (_label, val) in enumerate(self._items):
            x = plot.left() + (plot.width() * i / (n - 1) if n > 1 else plot.width() / 2)
            frac = (val - minv) / span
            y = plot.bottom() - plot.height() * frac * self._t
            pts.append(QPointF(x, y))
        if fill:
            path = QPainterPath(QPointF(pts[0].x(), plot.bottom()))
            for q in pts:
                path.lineTo(q)
            path.lineTo(pts[-1].x(), plot.bottom())
            path.closeSubpath()
            g = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            g.setColorAt(0.0, QColor(63, 224, 224, 110))
            g.setColorAt(1.0, QColor(63, 224, 224, 0))
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(g); p.drawPath(path)
        poly = QPolygonF(pts)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(63, 224, 224, 70), 5)); p.drawPolyline(poly)  # glow
        p.setPen(QPen(QColor(CYAN), 2)); p.drawPolyline(poly)  # crisp line
        p.setPen(Qt.PenStyle.NoPen)
        for i, (label, _v) in enumerate(self._items):
            p.setBrush(QColor(CYAN)); p.drawEllipse(pts[i], 2.6, 2.6)
            if n <= 8 or i % 2 == 0:
                p.setPen(QColor(MUTED))
                p.drawText(int(pts[i].x()) - 30, int(plot.bottom()) + 3, 60, 18, _HCENTER, label)
                p.setPen(Qt.PenStyle.NoPen)

    def _paint_pie(self, p: QPainter, area: QRectF, donut: bool) -> None:
        total = sum(max(0.0, v) for _, v in self._items) or 1.0
        size = max(120.0, min(area.width() - 150, area.height() - 12))
        cx = area.left() + size / 2 + 6
        cy = area.top() + area.height() / 2
        rect = QRectF(cx - size / 2, cy - size / 2, size, size)
        sweep_total = 360.0 * self._t
        p.setPen(QPen(QColor("#080b0f"), 1))  # thin dark separators between slices
        start, acc = 90.0, 0.0  # start at 12 o'clock
        for i, (_label, val) in enumerate(self._items):
            span = 360.0 * (max(0.0, val) / total)
            draw = max(0.0, min(span, sweep_total - acc))
            if draw > 0:
                p.setBrush(QColor(*_SEGMENTS[i % len(_SEGMENTS)]))
                p.drawPie(rect, int(start * 16), int(-draw * 16))
            start -= span
            acc += span
        if donut:
            hole = size * 0.52
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor("#0d141b"))
            p.drawEllipse(QRectF(cx - hole / 2, cy - hole / 2, hole, hole))
        lx, ly = int(rect.right()) + 14, int(area.top()) + 6  # legend
        for i, (label, val) in enumerate(self._items[:8]):
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(*_SEGMENTS[i % len(_SEGMENTS)]))
            p.drawRoundedRect(QRectF(lx, ly + 3, 10, 10), 2, 2)
            p.setPen(QColor(TEXT))
            pct = 100.0 * max(0.0, val) / total
            p.drawText(lx + 16, ly, self.width() - lx - 16, 16, _VLABEL, f"{label}  {pct:.0f}%")
            ly += 20
        p.end()


class ConsoleView(QWidget):
    openSettingsRequested = pyqtSignal()
    restartRequested = pyqtSignal()  # user asked to restart so voice can pre-warm and start listening

    def __init__(
        self,
        conversation: ConversationService,
        settings: SettingsStore,
        speech_in: SpeechIn | None = None,
        speech_out: SpeechOut | None = None,
        orb: PresenceOrb | None = None,
        forge: "ForgeService | None" = None,
    ) -> None:
        super().__init__()
        self.setObjectName("Console")
        self._conversation = conversation
        self._settings = settings
        self._forge = forge  # for removing/rolling back work a 'stop' interrupted
        self._workers: set[QtWorker] = set()
        self._busy = False
        self._cancelled = False  # set by a 'stop' — a pending reply is shown but not spoken
        self._cancel: CancelToken | None = None  # the running turn's stop signal
        self._pending_cleanup: "BuildHandle | None" = None  # awaiting yes/no to remove half-built work
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
        """Interrupt the current turn: hush speech now, and actually halt a running build."""
        if self._voice is not None:
            self._voice.interrupt()
        self._cancel_active()

    def _on_voice_stop(self) -> None:
        # The user said "stop" — the controller already hushed any speech; halt the running build too.
        self._cancel_active()

    def _cancel_active(self) -> None:
        """Signal the in-flight turn/build to stop. The build unwinds and (if one was in progress) the
        cleanup offer fires when the worker finishes."""
        if self._busy:
            self._cancelled = True
            if self._cancel is not None:
                self._cancel.cancel()  # kills the coder subprocess / breaks the build loop
        self.status.setText("Stopping…" if self._busy else "Stopped.")

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
            # Honest about the real state: it's saved on, but the speech model only pre-warms at launch,
            # so it isn't actually listening yet. Offer a one-click restart instead of a silent "on".
            self.status.setText("Voice needs a restart to start listening.")
            self._add_bubble("helix", "Voice is on, but I need a quick restart to start listening.")
            self._add_actions([("Restart now", self.restartRequested.emit), ("Later", lambda: None)])
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
        listening = on and voice.can_listen()          # actually hearing you right now
        needs_restart = on and not voice.can_listen()  # saved on, but not pre-warmed this run
        self._voice_btn.setVisible(True)
        # A near-solid dark pill so the label reads over the bright orb (cyan-on-cyan was invisible).
        edge = "#3fe0e0" if listening else ("#e0a13f" if needs_restart else "#26323b")
        txt = "#3fe0e0" if listening else ("#e0a13f" if needs_restart else "#aebcc3")
        self._voice_btn.setStyleSheet(
            f"QPushButton{{background:rgba(8,11,15,0.93);border:1px solid {edge};border-radius:14px;"
            f"color:{txt};padding:8px 18px;}} QPushButton:hover{{border-color:#3fe0e0;}}"
        )
        # Tell the truth: never say "say HELIX" when it isn't actually listening.
        self._voice_btn.setText(
            "🔊 Voice on — say “HELIX”" if listening
            else ("🔊 Voice on · restart to listen" if needs_restart else "🔇 Voice off")
        )
        self._voice_btn.setToolTip(
            "Listening for “HELIX”. Say “stop” to interrupt, “goodbye” to end; tap the orb to stop/mute."
            if listening else
            "Voice is on but needs a restart to start listening (the speech model loads at launch)."
            if needs_restart else
            "Turn on hands-free voice — then just say “HELIX” (or tap the orb)."
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
        if not text:
            return
        if self._pending_cleanup is not None:  # we asked "remove the half-built X?" — this is the answer
            self._answer_cleanup(text)
            return
        if self._busy:
            # Already working. If it's a short "stop", treat the typed/spoken word as a stop command.
            if is_stop(text):
                self._add_bubble("you", text)
                self._stop()
            return
        self._cancelled = False
        self._cancel = CancelToken()
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

        token = self._cancel
        worker = QtWorker(lambda emit: self._conversation.run_turn(text, on_progress=emit, cancel=token))
        # Strong ref until the QThread truly finishes (see _retire) so the GC can't kill a live thread.
        self._workers.add(worker)
        worker.progress.connect(self._on_progress)
        worker.finished_ok.connect(self._on_reply)
        worker.failed.connect(self._on_fail)
        worker.finished.connect(lambda w=worker: self._retire(w))
        worker.start()

    def _on_progress(self, line: str) -> None:
        # Live commentary as HELIX works: show every step on the status line, and (voice on) speak the
        # milestones — narrate() paces them to speech so it's a fluent "doing this, next that", not chatter.
        self.status.setText(line)
        if self._voice is not None:
            self._voice.narrate(line)

    def _on_reply(self, text: object) -> None:
        # Split the prose from any table/chart blocks: prose is shown AND spoken; visuals are only shown.
        spoken, visuals = split_visuals(str(text))
        if spoken or not visuals:
            self._add_bubble("helix", spoken or str(text))
        for spec in visuals:
            self._add_visual(spec)
        if self._cancelled:  # the user said stop while this was generating — show it, don't speak it
            self._cancelled = False
            handle = self._cancel.build if self._cancel is not None else None
            if self._voice is not None:
                self._voice.idle()
            self._idle_status()
            if handle is not None:  # a build was interrupted — offer to remove the half-finished work
                self._offer_cleanup(handle)
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

    # ----- cleanup after a stopped build -----
    def _offer_cleanup(self, handle: "BuildHandle") -> None:
        """A build was stopped mid-run — ask whether to remove (new) or roll back (iteration) the work."""
        if self._forge is None:
            return  # nothing we can do without the forge; leave the partial work in place
        self._pending_cleanup = handle
        verb = "roll back" if handle.iterating else "remove"
        q = f"I stopped. Want me to {verb} the half-built “{handle.name}”?"
        self._add_bubble("helix", q)
        self._add_actions([
            ("Roll back" if handle.iterating else "Remove", self._cleanup_remove),
            ("Keep it", self._cleanup_keep),
        ])
        if self._voice is not None and self._voice.enabled():
            self._voice.speak(q)  # spoken too, so the user can answer "yes"/"no" by voice

    def _answer_cleanup(self, text: str) -> None:
        answer = _cleanup_answer(text)
        if answer == "yes":
            self._cleanup_remove()
        elif answer == "no":
            self._cleanup_keep()
        else:  # neither a clear yes nor no — keep the work and treat the words as a fresh request
            self._pending_cleanup = None
            self._submit(text, from_voice=False)

    def _cleanup_remove(self) -> None:
        handle = self._pending_cleanup
        self._pending_cleanup = None
        if handle is None or self._forge is None:
            return
        try:
            self._forge.discard_build(handle)
            msg = f"{'Rolled back' if handle.iterating else 'Removed'} “{handle.name}”."
        except Exception as exc:
            msg = f"I couldn't remove it: {exc}"
        self._announce(msg)

    def _cleanup_keep(self) -> None:
        self._pending_cleanup = None
        self._announce("Okay, I kept it.")

    def _announce(self, msg: str) -> None:
        self._add_bubble("helix", msg)
        self.status.setText(msg)
        if self._voice is not None and self._voice.enabled():
            self._voice.speak(msg)

    def _add_actions(self, buttons: list[tuple[str, object]]) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        for label, cb in buttons:
            btn = QPushButton(label)
            btn.setObjectName("Nav")
            btn.clicked.connect(lambda _checked=False, f=cb: f())
            row.addWidget(btn)
        row.addStretch(1)
        self._tlayout.insertLayout(self._tlayout.count() - 1, row)
        QTimer.singleShot(0, self._scroll_to_bottom)

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
            lay.addWidget(_ChartWidget(spec))
            self._insert_visual(card)
        elif kind == "table":
            self._insert_visual(self._table_widget(spec))

    @staticmethod
    def _looks_numeric(s: str) -> bool:
        t = s.strip().lstrip("$€£+-").replace(",", "").rstrip("%")
        try:
            float(t)
            return True
        except ValueError:
            return False

    def _table_widget(self, spec: dict) -> QLabel:
        cols = spec.get("columns") or []
        rows = spec.get("rows") or []
        title = str(spec.get("title") or "")
        parts: list[str] = []
        if title:
            parts.append(
                f"<div style='color:{CYAN};font-weight:600;letter-spacing:.5px;"
                f"margin-bottom:8px'>{escape(title)}</div>"
            )
        parts.append("<table cellspacing='0' cellpadding='7' style='border-collapse:collapse'>")
        if cols:
            parts.append(
                "<tr>"
                + "".join(
                    f"<th style='color:{CYAN};text-align:left;padding:4px 12px;"
                    f"border-bottom:1px solid {CYAN}'>{escape(str(c))}</th>"
                    for c in cols
                )
                + "</tr>"
            )
        for ri, r in enumerate(rows):
            cells = r if isinstance(r, (list, tuple)) else [r]
            bg = "rgba(63,224,224,0.05)" if ri % 2 else "transparent"  # zebra striping
            tds = "".join(
                f"<td style='color:{TEXT};text-align:{'right' if self._looks_numeric(str(c)) else 'left'};"
                f"padding:4px 12px;border-bottom:1px solid #14202a'>{escape(str(c))}</td>"
                for c in cells
            )
            parts.append(f"<tr style='background:{bg}'>{tds}</tr>")
        parts.append("</table>")
        lbl = QLabel("".join(parts))
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setStyleSheet(
            f"QLabel{{background:rgba(13,20,27,0.86);border:1px solid {CYAN_DIM};"
            "border-radius:12px;padding:12px 16px;}"
        )
        lbl.setMaximumWidth(620)
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
