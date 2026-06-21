from __future__ import annotations

import array
import logging
import math
import os
import random
import re
import sys
import tempfile
import wave

from PyQt6.QtCore import Qt, QPointF, QRectF, QTimer, QObject, QRunnable, QThreadPool, QUrl, pyqtSignal
from PyQt6.QtTextToSpeech import QTextToSpeech, QVoice
from PyQt6.QtMultimedia import QAudioFormat, QAudioOutput, QAudioSource, QMediaDevices, QMediaPlayer
from PyQt6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from helix.ai.claude import (
    CLAUDE_API_KEY_SETTING,
    ClaudeClient,
    ClaudeConfig,
    DEFAULT_RESEARCH_MODEL,
    estimate_cost,
)
from helix.ai.speech import DEFAULT_VOICE, VOICE_CHOICES, synthesize_speech
from helix.ai.transcribe import is_available as stt_available, is_ready as stt_ready, transcribe
from helix.ai.actions import (
    ActionContext,
    ActionRouter,
    is_affirmative,
    is_negative,
    run_chat_turn,
)
from helix.ai.research import build_jarvis_chat_system
from helix.core.config import load_config
from helix.core.conversation import ConversationStore
from helix.core.memory import SQLiteMemory
from helix.core.settings import AppSettings
from helix.core.reliability import LOGGER_NAME, install_crash_guard, setup_logging
from helix.selfdev import builds as selfdev_builds, constitution as selfdev_constitution, mailer as selfdev_mailer, restart as selfdev_restart, triggers as selfdev_triggers, versioning as selfdev_versioning
from helix.tasks import registry as tasks_registry
from helix.agents import registry as agents_registry


_LOG = logging.getLogger(LOGGER_NAME)

class NoScrollComboBox(QComboBox):
    """A combo box that ignores the scroll wheel, so scrolling the page never changes its value.

    Critical for the fake/real money toggle: an accidental wheel scroll must never flip Practice ->
    Real. Ignoring the event lets it bubble up to the scroll area, which scrolls the page instead.
    """

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt override)
        event.ignore()






class WorkerSignals(QObject):
    finished = pyqtSignal(object)
    error = pyqtSignal(object)


class Worker(QRunnable):
    """Runs a no-arg callable on a background thread and reports back via signals."""

    def __init__(self, fn) -> None:
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # surfaced to the UI through the error signal
            self.signals.error.emit(exc)
        else:
            self.signals.finished.emit(result)


def spawn_worker(registry: set, work, done) -> None:
    """Run `work` off-thread; call `done(ok, payload)` on the UI thread when it finishes.

    `registry` keeps the worker referenced until completion so its signals are delivered.
    """
    worker = Worker(work)
    worker.setAutoDelete(False)
    registry.add(worker)
    worker.signals.finished.connect(lambda result: (registry.discard(worker), done(True, result)))
    worker.signals.error.connect(lambda exc: (registry.discard(worker), done(False, exc)))
    QThreadPool.globalInstance().start(worker)


def run_qt_app(memory: SQLiteMemory) -> int:
    log = setup_logging()
    install_crash_guard(log)  # an unhandled slot error logs + keeps the app alive, not abort (§39)
    log.info("HELIX desktop starting")
    # Speech-to-text is pre-warmed in main.py BEFORE PyQt6 is imported — building the ctranslate2 model
    # after Qt's native libs are loaded segfaults the process, and a bare `import PyQt6` is enough to
    # trip it (§23). By the time we get here PyQt6 is already imported, so we must NOT load the model
    # now — we only report readiness. If the pre-warm was skipped (e.g. under a debugger that imports Qt
    # before main.py), is_ready() is False and the voice paths (push-to-talk + hands-free) disable
    # themselves rather than attempt a crashing post-Qt load.
    log.info("speech-to-text %s", "ready" if stt_ready() else "unavailable (voice disabled this run)")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("HELIX")
    apply_hud_style(app)

    window = HelixMainWindow(memory)
    window.resize(980, 560)
    window.setMinimumSize(720, 480)
    window.show()
    exit_code = app.exec()
    QThreadPool.globalInstance().waitForDone(3000)
    log.info("HELIX desktop exited (code %s)", exit_code)
    # Qt + native multimedia/camera objects can fault during interpreter teardown on Windows, which
    # would make the process exit with a CRASH code (0xC0000409) even on a clean close — and the §39
    # supervisor would then relaunch on every normal exit. Flush, then exit HARD with the intended code,
    # skipping the crashy finalization: 0 stops the supervisor, RESTART_EXIT_CODE (42) relaunches it.
    import logging
    logging.shutdown()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(exit_code)
    return exit_code  # unreachable; kept for type sanity


_PRESENCE_TEXT = {
    "idle": "Standing by, sir.",
    "listening": "Listening…",
    "transcribing": "Catching that…",
    "thinking": "Thinking…",
    "acting": "On it…",
    "speaking": "Speaking.",
}


class PresenceOrb(QWidget):
    """HELIX's living presence — an animated orb that breathes when idle and reacts to its state
    (listening / thinking / acting / speaking). Left-click toggles the conversation. The JARVIS heart."""

    clicked = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(64, 64)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._state = "idle"
        self._level = 0.0
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)  # ~30 fps

    def set_state(self, state: str) -> None:
        self._state = state or "idle"

    def set_level(self, level: float) -> None:
        try:
            self._level = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            self._level = 0.0

    def _tick(self) -> None:
        self._phase += 0.09
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        base = min(self.width(), self.height()) * 0.22
        state = self._state
        color = QColor(255, 200, 87) if state == "speaking" else QColor(29, 216, 255)
        wobble = math.sin(self._phase)
        if state == "listening":
            amp = 0.12 + 0.55 * self._level
        elif state in ("thinking", "acting", "transcribing"):
            amp = 0.16
        elif state == "speaking":
            amp = 0.18
        else:
            amp = 0.06
        radius = base * (1.0 + amp * (0.5 + 0.5 * wobble))

        # soft outer glow
        glow = QRadialGradient(QPointF(cx, cy), radius * 2.6)
        inner = QColor(color); inner.setAlpha(60); glow.setColorAt(0.0, inner)
        outer = QColor(color); outer.setAlpha(0); glow.setColorAt(1.0, outer)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QPointF(cx, cy), radius * 2.6, radius * 2.6)

        # concentric rings
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for index, alpha in enumerate((150, 90, 45)):
            ring = QColor(color); ring.setAlpha(alpha)
            pen = QPen(ring); pen.setWidthF(1.6)
            painter.setPen(pen)
            rr = radius * (1.0 + 0.32 * index)
            painter.drawEllipse(QPointF(cx, cy), rr, rr)

        # rotating HUD reticle ticks
        tick_r = radius * 1.95
        tcol = QColor(color); tcol.setAlpha(120)
        pen = QPen(tcol); pen.setWidthF(2.0); pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        ticks = 12
        for k in range(ticks):
            ang = self._phase * 0.3 + (2 * math.pi * k / ticks)
            long = (k % 3 == 0)
            r2 = tick_r + (radius * 0.16 if long else radius * 0.07)
            painter.drawLine(
                QPointF(cx + math.cos(ang) * tick_r, cy + math.sin(ang) * tick_r),
                QPointF(cx + math.cos(ang) * r2, cy + math.sin(ang) * r2),
            )

        # orbiting particles
        painter.setPen(Qt.PenStyle.NoPen)
        for j, (rr_mult, speed, size) in enumerate(((1.42, 0.7, 3.0), (1.7, -0.5, 2.3), (2.0, 0.95, 2.6))):
            ang = self._phase * speed + j * 2.1
            pcol = QColor(color); pcol.setAlpha(220)
            painter.setBrush(pcol)
            painter.drawEllipse(
                QPointF(cx + math.cos(ang) * radius * rr_mult, cy + math.sin(ang) * radius * rr_mult), size, size
            )

        # rotating arc while working
        if state in ("thinking", "acting", "transcribing"):
            pen = QPen(QColor(color)); pen.setWidthF(3.0); pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen); painter.setBrush(Qt.BrushStyle.NoBrush)
            span = QRectF(cx - radius * 1.25, cy - radius * 1.25, radius * 2.5, radius * 2.5)
            painter.drawArc(span, int((self._phase * 70) % 360) * 16, 100 * 16)

        # white-hot core
        core = QRadialGradient(QPointF(cx, cy), radius)
        hot = QColor(255, 255, 255); hot.setAlpha(205); core.setColorAt(0.0, hot)
        mid = QColor(color); mid.setAlpha(230); core.setColorAt(0.4, mid)
        edge = QColor(color); edge.setAlpha(80); core.setColorAt(1.0, edge)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(core)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
        painter.end()


class AmbientTile(QFrame):
    """A small glanceable card on the Console — a label, a value, and a hint. Click to open the deep
    view. Awareness, not a menu."""

    def __init__(self, title: str, on_click=None, parent=None) -> None:
        super().__init__(parent)
        self._on_click = on_click
        self.setObjectName("ambientTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QFrame#ambientTile{border:1px solid #1b3a44;border-radius:14px;"
            "background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            "stop:0 rgba(20,44,53,0.55),stop:1 rgba(11,26,32,0.55));}"
            "QFrame#ambientTile:hover{border-color:#1dd8ff;"
            "background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            "stop:0 rgba(26,58,70,0.7),stop:1 rgba(13,32,40,0.7));}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 13, 16, 13)
        lay.setSpacing(3)
        cap = QLabel(title.upper())
        cap.setStyleSheet(
            "color:#6fb3c0;border:none;font-size:10pt;font-weight:800;letter-spacing:1px;"
        )
        self._value = QLabel("…")
        self._value.setStyleSheet("color:#eaffff;font-weight:800;font-size:17px;border:none;")
        self._hint = QLabel("")
        self._hint.setStyleSheet("color:#7faebb;border:none;font-size:11pt;")
        lay.addWidget(cap)
        lay.addWidget(self._value)
        lay.addWidget(self._hint)

    def set_value(self, value: str, hint: str = "") -> None:
        self._value.setText(value)
        self._hint.setText(hint)

    def mousePressEvent(self, _event) -> None:
        if self._on_click:
            try:
                self._on_click()
            except Exception:
                pass


class ConsoleView(QWidget):
    """The HELIX home — a big animated Presence orb (the app's face) with the conversation beneath it.
    Nothing else shows until you ask: tables and panels pop up on request (by voice, or the one launcher
    menu). No tabs, no tiles."""

    def __init__(self, xpert: "XpertTab", memory: SQLiteMemory, open_view, show_launcher, show_tasks=None, show_agents=None, parent=None) -> None:
        super().__init__(parent)
        self._xpert = xpert
        self.memory = memory
        self.settings = AppSettings()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 16, 26, 18)
        layout.setSpacing(10)

        # top bar — brand + live presence, and the single launcher (the only manual navigation)
        topbar = QHBoxLayout()
        brand = QVBoxLayout()
        brand.setSpacing(2)
        name = QLabel("HELIX")
        name.setObjectName("consoleBrand")
        self.presence = QLabel(_PRESENCE_TEXT["idle"])
        self.presence.setObjectName("consolePresence")
        brand.addWidget(name)
        brand.addWidget(self.presence)
        menu_button = QPushButton("☰  Apps")
        menu_button.setObjectName("ghostButton")
        menu_button.setCursor(Qt.CursorShape.PointingHandCursor)
        menu_button.setToolTip("Your apps — and ‘New app’ to build one")
        menu_button.clicked.connect(show_launcher)
        tasks_button = QPushButton("⚡  Tasks")
        tasks_button.setObjectName("ghostButton")
        tasks_button.setCursor(Qt.CursorShape.PointingHandCursor)
        tasks_button.setToolTip("Run a task")
        tasks_button.clicked.connect(show_tasks or show_launcher)
        agents_button = QPushButton("🤖  Agents")
        agents_button.setObjectName("ghostButton")
        agents_button.setCursor(Qt.CursorShape.PointingHandCursor)
        agents_button.setToolTip("Goal-driven automations — and ‘New Agent’")
        agents_button.clicked.connect(show_agents or show_launcher)
        # Archive lives here as a standalone, always-visible button (not buried in the menu) — it's the
        # recovery lifeline the Commandments require to always exist and function.
        archive_button = QPushButton("🗂  Archive")
        archive_button.setObjectName("ghostButton")
        archive_button.setCursor(Qt.CursorShape.PointingHandCursor)
        archive_button.setToolTip("Versions · restore · safety")
        archive_button.clicked.connect(lambda: open_view("archive"))
        # A play/stop toggle for the voice input channel, directly below Archive: a stop icon while the
        # mic is live (listening for “HELIX”), a play icon while muted. Its state persists across
        # sessions (XpertTab.set_voice_input writes the setting).
        self.voice_toggle = QPushButton()
        self.voice_toggle.setObjectName("ghostButton")
        self.voice_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.voice_toggle.clicked.connect(self._toggle_voice_input)
        self._sync_voice_toggle()
        # The manual-navigation buttons, stacked vertically: Menu → Tasks → Agents → Archive → Voice.
        nav = QVBoxLayout()
        nav.setSpacing(8)
        nav.addWidget(menu_button)
        nav.addWidget(tasks_button)
        nav.addWidget(agents_button)
        nav.addWidget(archive_button)
        nav.addWidget(self.voice_toggle)
        topbar.addLayout(brand)
        topbar.addStretch(1)
        topbar.addLayout(nav)
        layout.addLayout(topbar)

        # First-run gate: until a Claude key is saved, HELIX can't build anything — say so plainly
        # and point to Settings, rather than failing silently.
        self._key_gate = QFrame()
        self._key_gate.setObjectName("keyGate")
        gate_row = QHBoxLayout(self._key_gate)
        gate_row.setContentsMargins(14, 10, 14, 10)
        gate_msg = QLabel("Add your Claude API key to start building apps.")
        gate_msg.setObjectName("xpertHint")
        gate_msg.setWordWrap(True)
        gate_button = QPushButton("Open Settings")
        gate_button.setObjectName("primaryButton")
        gate_button.setCursor(Qt.CursorShape.PointingHandCursor)
        gate_button.clicked.connect(lambda: open_view("settings"))
        gate_row.addWidget(gate_msg, 1)
        gate_row.addWidget(gate_button, 0)
        layout.addWidget(self._key_gate)
        self.refresh_key_gate()

        # the orb — large, centered, the face of the app
        self.orb = PresenceOrb()
        self.orb.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.orb, 6)

        # the conversation box — slimmed, and HIDDEN until you tap the orb (left-click)
        try:
            xpert.compact()
        except Exception:
            pass
        xpert.setVisible(True)  # the conversation is the front door — show it so the user can just type
        layout.addWidget(xpert, 5)
        self.orb.clicked.connect(self._toggle_conversation)

        self._orb_timer = QTimer(self)
        self._orb_timer.timeout.connect(self._sync_presence)
        self._orb_timer.start(70)

    def _sync_presence(self) -> None:
        state = getattr(self._xpert, "_convo_state", "idle")
        self.orb.set_state(state)
        try:
            self.orb.set_level(self._xpert.level_bar.value() / 100.0)
        except Exception:
            pass
        self.presence.setText(_PRESENCE_TEXT.get(state, _PRESENCE_TEXT["idle"]))

    def refresh_key_gate(self) -> None:
        """Show the 'add your key' banner only while no Claude key is configured."""
        try:
            configured = ClaudeClient().is_configured()
        except Exception:
            configured = True
        self._key_gate.setVisible(not configured)

    def _toggle_conversation(self) -> None:
        """Left-click on the orb reveals or hides the conversation box."""
        self._xpert.setVisible(not self._xpert.isVisible())

    def _toggle_voice_input(self) -> None:
        """Flip the hands-free mic channel on/off via the XpertTab (which persists the choice)."""
        try:
            self._xpert.set_voice_input(not self._xpert.voice_input_on())
        except Exception:
            pass
        self._sync_voice_toggle()

    def _sync_voice_toggle(self) -> None:
        """Reflect the current voice-input state on the toggle: stop icon when live, play when muted."""
        try:
            on = self._xpert.voice_input_on()
        except Exception:
            on = True
        if on:
            self.voice_toggle.setText("⏹  Voice")
            self.voice_toggle.setToolTip("Voice input is on — click to mute the mic")
        else:
            self.voice_toggle.setText("▶  Voice")
            self.voice_toggle.setToolTip("Voice input is muted — click to listen")


class PanelHost(QWidget):
    """Hosts ONE deep view at a time, summoned on request — a title + a back-to-HELIX button, no tab bar.
    Tables and panels live here and pop up only when the user (or HELIX) opens them."""

    def __init__(self, views: dict, on_home, settings: AppSettings | None = None, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings or AppSettings()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(10)
        header = QHBoxLayout()
        home_button = QPushButton("‹  HELIX")
        home_button.setObjectName("ghostButton")
        home_button.setCursor(Qt.CursorShape.PointingHandCursor)
        home_button.clicked.connect(on_home)
        self.title = QLabel("")
        self.title.setObjectName("panelTitle")
        header.addWidget(home_button)
        header.addSpacing(14)
        header.addWidget(self.title)
        header.addStretch(1)
        layout.addLayout(header)
        self._stack = QStackedWidget()
        self._index: dict = {}
        for position, (key, (label, widget)) in enumerate(views.items()):
            self._stack.addWidget(widget)
            self._index[key] = (position, label)
        layout.addWidget(self._stack, 1)

    def add_view(self, key: str, label: str, widget) -> None:
        """Register a panel at runtime (e.g. a freshly-built app), so it can be summoned immediately
        without a restart."""
        if key in self._index:
            return
        position = self._stack.addWidget(widget)
        self._index[key] = (position, label)

    def show_view(self, key: str) -> bool:
        entry = self._index.get(key)
        if entry is None:
            return False
        position, label = entry
        self._stack.setCurrentIndex(position)
        self.title.setText(custom_menu_labels(self.settings).get(key, label))
        return True


LAUNCHER_HIDDEN_SETTING = "launcher_hidden_items"
LAUNCHER_LABELS_SETTING = "launcher_custom_labels"
TASKS_HIDDEN_SETTING = "tasks_hidden_items"  # task keys removed from the Tasks launcher via the ✕ badge


def custom_menu_labels(settings: AppSettings) -> dict[str, str]:
    """User-chosen display names for menu items, keyed by view key (e.g. {"home": "House"}).
    Stored in settings so a rename persists across restarts; tolerant of a missing/corrupt value."""
    raw = settings.get(LAUNCHER_LABELS_SETTING, {})
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if str(v).strip()}


class _LauncherCard(QPushButton):
    """A launcher destination card carrying a small ✕ badge. The badge is a child button pinned to the
    top-right corner; clicking it emits `hide_requested` and (because Qt routes the press to the child)
    does not also open the destination. For a self-added feature the ✕ removes its CODE (non-restorable);
    for a core pillar it just hides the card (restorable in Settings) — `removable` picks the tooltip."""

    hide_requested = pyqtSignal(str)
    rename_requested = pyqtSignal(str)

    def __init__(self, key: str, text: str, removable: bool = False, permanent: bool = False,
                 badge_tooltip: str | None = None, allow_rename: bool = True, parent=None) -> None:
        super().__init__(text, parent)
        self._key = key
        self.setObjectName("launcherCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        # NOTE (X-button audit, 2026-06-20): the task asked to resume any prior in-progress "X button"
        # fix. Audited the ✕ badge paths app-wide (this card badge + Launcher._on_badge/_hide, the
        # grocery/components/chip remove ✕, the version-Archive purge ✕) and found no incomplete or
        # broken work — all ✕ handlers are wired and functional. No git/TODO breadcrumbs of an unfinished
        # fix were found either. If a regression resurfaces, start here: the badge press is routed to the
        # child button so it must NOT also trigger the card's open action (resizeEvent re-pins it).
        self._badge = None
        if not permanent:  # permanent items (Settings, Archive) carry no ✕ — they can't be hidden or removed
            self._badge = QPushButton("✕", self)
            self._badge.setObjectName("launcherRemove" if removable else "launcherHide")
            self._badge.setCursor(Qt.CursorShape.PointingHandCursor)
            self._badge.setFixedSize(22, 22)
            self._badge.setToolTip(
                badge_tooltip if badge_tooltip is not None else (
                    "Remove this feature and delete its code (you approve the change first)"
                    if removable else "Hide from the menu (restore in Settings)"
                )
            )
            self._badge.clicked.connect(lambda: self.hide_requested.emit(self._key))
        if allow_rename:
            self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.customContextMenuRequested.connect(self._show_menu)

    def _show_menu(self, pos) -> None:
        """Right-click (or long-press, which Qt maps to a context-menu request) → a Rename option."""
        menu = QMenu(self)
        menu.addAction("Rename…", lambda: self.rename_requested.emit(self._key))
        menu.exec(self.mapToGlobal(pos))

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        if self._badge is not None:
            self._badge.move(self.width() - self._badge.width() - 8, 8)


class Launcher(QWidget):
    """The single manual navigation — a clean grid of destinations. Voice can open the same things.
    Each card has a small ✕ to hide it from the menu; hidden items persist in settings
    (`launcher_hidden_items`) and can be brought back from Settings → Restore hidden menu items."""

    def __init__(self, items: list, on_pick, on_home, settings: AppSettings | None = None,
                 removable_keys=None, on_remove_code=None, permanent_keys=None, parent=None) -> None:
        super().__init__(parent)
        self._items = list(items)
        self._on_pick = on_pick
        self.settings = settings or AppSettings()
        self._removable_keys = set(removable_keys or ())
        self._on_remove_code = on_remove_code
        self._permanent_keys = set(permanent_keys or ())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 34, 48, 40)
        outer.setSpacing(18)
        header = QHBoxLayout()
        title = QLabel("What can I open?")
        title.setObjectName("launcherTitle")
        close_button = QPushButton("✕")
        close_button.setObjectName("ghostButton")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(on_home)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(close_button)
        outer.addLayout(header)
        self._grid = QGridLayout()
        self._grid.setSpacing(18)
        outer.addLayout(self._grid)
        outer.addStretch(1)
        self._rebuild()

    def _hidden(self) -> set[str]:
        raw = self.settings.get(LAUNCHER_HIDDEN_SETTING, [])
        return set(raw) if isinstance(raw, list) else set()

    def _rebuild(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        hidden = self._hidden()
        labels = custom_menu_labels(self.settings)
        visible = [it for it in self._items if it[0] not in hidden or it[0] in self._permanent_keys]
        for n, (key, label, subtitle) in enumerate(visible):
            shown = labels.get(key, label)
            card = _LauncherCard(
                key, f"{shown}\n{subtitle}",
                removable=key in self._removable_keys,
                permanent=key in self._permanent_keys,
            )
            card.clicked.connect(lambda _=False, k=key: self._on_pick(k))
            card.hide_requested.connect(self._on_badge)
            card.rename_requested.connect(self._rename)
            self._grid.addWidget(card, n // 2, n % 2)  # 2-column grid — compact and readable

    def _hide(self, key: str) -> None:
        if key in self._permanent_keys:  # Settings/Archive can never be hidden (Commandments 8 & 12)
            return
        hidden = self._hidden()
        hidden.add(key)
        self.settings.set(LAUNCHER_HIDDEN_SETTING, sorted(hidden))
        self._rebuild()

    def _on_badge(self, key: str) -> None:
        """The card ✕: for a self-added feature, remove its CODE (with approval); otherwise just hide the
        card from the menu (restorable in Settings)."""
        if key in self._removable_keys and self._on_remove_code is not None:
            label = next((it[1] for it in self._items if it[0] == key), key)
            self._on_remove_code(label)
        else:
            self._hide(key)

    def _rename(self, key: str) -> None:
        """Prompt for a new display name for this menu item; save it (or clear back to the default if
        the box is emptied). Persists to settings so the rename survives a restart."""
        default = next((it[1] for it in self._items if it[0] == key), key)
        current = custom_menu_labels(self.settings).get(key, default)
        text, ok = QInputDialog.getText(
            self, "Rename menu item", f"Display name (leave blank to reset to “{default}”):", text=current
        )
        if not ok:
            return
        labels = custom_menu_labels(self.settings)
        new_name = text.strip()
        if new_name and new_name != default:
            labels[key] = new_name
        else:
            labels.pop(key, None)
        self.settings.set(LAUNCHER_LABELS_SETTING, labels)
        self._rebuild()

    def restore(self, key: str) -> None:
        hidden = self._hidden()
        hidden.discard(key)
        self.settings.set(LAUNCHER_HIDDEN_SETTING, sorted(hidden))
        self._rebuild()

    def hidden_items(self) -> list:
        """The (key, label, subtitle) tuples currently hidden — feeds the Settings restore list."""
        hidden = self._hidden()
        return [it for it in self._items if it[0] in hidden]


class TasksView(QWidget):
    """The Tasks launcher — a grid of runnable task "applications" (the action counterpart to the Menu's
    app shortcuts). Each card runs its task off-thread and shows the result below. Cards come from
    `helix.tasks.registry` (append to BUILTIN_TASKS or call register()), so this panel grows with no UI
    edits. Opened by the Tasks button in the Console top bar, mirroring how Menu opens the launcher."""

    def __init__(self, on_home, settings: AppSettings | None = None, on_pick=None, parent=None) -> None:
        super().__init__(parent)
        self._workers: set = set()
        self.settings = settings or AppSettings()
        self._on_pick = on_pick  # route the permanent New Task / Settings cards to the main window
        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 34, 48, 40)
        outer.setSpacing(18)
        header = QHBoxLayout()
        title = QLabel("Run a task")
        title.setObjectName("launcherTitle")
        close_button = QPushButton("✕")
        close_button.setObjectName("ghostButton")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(on_home)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(close_button)
        outer.addLayout(header)
        self._grid = QGridLayout()
        self._grid.setSpacing(18)
        outer.addLayout(self._grid)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Pick a task to run — its result appears here.")
        outer.addWidget(self.output, 1)
        self._rebuild()

    def _hidden(self) -> set[str]:
        raw = self.settings.get(TASKS_HIDDEN_SETTING, [])
        return set(raw) if isinstance(raw, list) else set()

    def _rebuild(self) -> None:
        """(Re)build the task grid. Two permanent cards lead — New Task and Settings, mirroring the Menu —
        then the registered tasks, each with a ✕ badge to remove it (removals persist in settings)."""
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        n = 0
        # Permanent cards (no ✕): New Task opens the Console; Settings opens TASK settings (the Menu's
        # Settings opens app settings — the two are deliberately separate).
        for key, label, subtitle in (
            ("newtask", "New Task", "design it with me, then build"),
            ("tasksettings", "Settings", "task options"),
        ):
            card = _LauncherCard(key, f"{label}\n{subtitle}", permanent=True, allow_rename=False)
            card.clicked.connect(lambda _=False, k=key: self._pick(k))
            self._grid.addWidget(card, n // 2, n % 2)
            n += 1
        # Registered task apps (removable).
        hidden = self._hidden()
        all_tasks = tasks_registry.all_tasks()
        visible = [t for t in all_tasks if t.key not in hidden]
        for task in visible:
            card = _LauncherCard(
                task.key, f"{task.label}\n{task.subtitle}",
                badge_tooltip="Remove this task from the list",
                allow_rename=False,
            )
            card.clicked.connect(lambda _=False, t=task: self._run(t))
            card.hide_requested.connect(self._remove)
            self._grid.addWidget(card, n // 2, n % 2)
            n += 1

    def _pick(self, key: str) -> None:
        """Route a permanent card (New Task / Settings) to the main window."""
        if self._on_pick is not None:
            self._on_pick(key)

    def _remove(self, key: str) -> None:
        """The task card ✕: drop the task from the launcher and remember it as hidden in settings.
        Refuses to hide the final card so the launcher always keeps at least one runnable task."""
        hidden = self._hidden()
        remaining = [t for t in tasks_registry.all_tasks() if t.key != key and t.key not in hidden]
        if not remaining:
            self.output.setPlainText("That's the last task, sir — I'll keep it on the launcher.")
            return
        hidden.add(key)
        self.settings.set(TASKS_HIDDEN_SETTING, sorted(hidden))
        self._rebuild()

    def _run(self, task) -> None:
        self.output.setPlainText(f"Running {task.label}…")
        spawn_worker(self._workers, task.run, lambda ok, payload, t=task: self._done(t, ok, payload))

    def _done(self, task, ok: bool, payload) -> None:
        self.output.setPlainText(str(payload) if ok else f"{task.label} failed: {payload}")


class AgentsView(QWidget):
    """The Agents screen — goal-driven automations. Leads with New Agent + Settings (mirroring Tasks),
    then a card per agent the user has created. Clicking an agent opens its AgentView panel."""

    def __init__(self, on_home, settings: AppSettings | None = None, on_pick=None, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings or AppSettings()
        self._on_pick = on_pick
        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 34, 48, 40)
        outer.setSpacing(16)
        header = QHBoxLayout()
        title = QLabel("Agents")
        title.setObjectName("launcherTitle")
        close_button = QPushButton("✕")
        close_button.setObjectName("ghostButton")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(on_home)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(close_button)
        outer.addLayout(header)
        hint = QLabel("Agents run on their own toward a goal. Design one with New Agent, then run it.")
        hint.setObjectName("xpertHint")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        self._grid = QGridLayout()
        self._grid.setSpacing(18)
        outer.addLayout(self._grid)
        outer.addStretch(1)
        self._rebuild()

    def _rebuild(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        n = 0
        for key, label, subtitle in (
            ("newagent", "New Agent", "design it with me, then run"),
            ("agentsettings", "Settings", "agent options"),
        ):
            card = _LauncherCard(key, f"{label}\n{subtitle}", permanent=True, allow_rename=False)
            card.clicked.connect(lambda _=False, k=key: self._pick(k))
            self._grid.addWidget(card, n // 2, n % 2)
            n += 1
        for agent in agents_registry.list_agents(self.settings):
            state = "on" if agent.get("enabled") else "off"
            card = _LauncherCard(
                agent["key"], f"{agent.get('name', 'Agent')}\n{(agent.get('goal', '') or '')[:46]}  ·  {state}",
                badge_tooltip="Delete this agent", allow_rename=False,
            )
            card.clicked.connect(lambda _=False, k=agent["key"]: self._pick(k))
            card.hide_requested.connect(self._remove)
            self._grid.addWidget(card, n // 2, n % 2)
            n += 1

    def _pick(self, key: str) -> None:
        if self._on_pick is not None:
            self._on_pick(key)

    def _remove(self, key: str) -> None:
        agent = agents_registry.get_agent(self.settings, key)
        name = (agent.get("name", key) if agent else key)
        confirm = QMessageBox.question(self, "Delete agent", f"Delete the agent “{name}”?")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        agents_registry.delete_agent(self.settings, key)
        self._rebuild()


class AgentView(QWidget):
    """One agent's panel: its goal, an on/off toggle, and Run now (executes the goal through the AI
    tool-loop). Anything that needs approval pauses and is reported rather than auto-run."""

    step_signal = pyqtSignal(str)

    def __init__(self, memory: SQLiteMemory, settings: AppSettings, agent_key: str, parent=None) -> None:
        super().__init__(parent)
        self.memory = memory
        self.settings = settings
        self.key = agent_key
        self._workers: set = set()
        agent = agents_registry.get_agent(settings, agent_key) or {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(12)
        goal = QLabel(agent.get("goal", "") or "(no goal set)")
        goal.setObjectName("xpertHint")
        goal.setWordWrap(True)
        layout.addWidget(goal)

        row = QHBoxLayout()
        self.run_button = QPushButton("▶  Run now")
        self.run_button.setObjectName("primaryButton")
        self.run_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_button.clicked.connect(self._run)
        row.addWidget(self.run_button)
        self.enabled_box = QCheckBox("Enabled")
        self.enabled_box.setChecked(bool(agent.get("enabled")))
        self.enabled_box.stateChanged.connect(self._toggle_enabled)
        row.addWidget(self.enabled_box)
        row.addStretch(1)
        layout.addLayout(row)

        trigger = QLabel("Trigger: manual (Run now). Scheduled triggers are coming.")
        trigger.setObjectName("xpertHint")
        layout.addWidget(trigger)

        self.log = QTextBrowser()
        self.log.setObjectName("designTranscript")
        self.log.setOpenExternalLinks(True)
        last = agent.get("last_result", "")
        if last:
            self.log.setMarkdown(f"**Last run** ({agent.get('last_run', '')})\n\n{last}")
        else:
            self.log.setPlaceholderText("Run the agent and its activity appears here.")
        layout.addWidget(self.log, 1)

        self.step_signal.connect(lambda m: self.log.setMarkdown(f"_{m}_"))

    def _toggle_enabled(self) -> None:
        agents_registry.update_agent(self.settings, self.key, enabled=self.enabled_box.isChecked())

    def _run(self) -> None:
        agent = agents_registry.get_agent(self.settings, self.key)
        if not agent:
            return
        self.run_button.setEnabled(False)
        self.log.setMarkdown("_Running…_")
        self.step_signal.emit("Running…")
        spawn_worker(
            self._workers,
            lambda: agents_registry.run_agent(self.settings, self.memory, agent,
                                              on_step=lambda s: self.step_signal.emit(s)),
            self._done,
        )

    def _done(self, ok: bool, payload) -> None:
        self.run_button.setEnabled(True)
        if not ok:
            self.log.setMarkdown(f"**Run failed:** {payload}")
            return
        reply = payload.get("reply", "")
        pending = payload.get("pending")
        text = f"**Last run** ({payload.get('ran_at', '')})\n\n{reply}"
        if pending:
            text += f"\n\n---\n\n⚠ The agent {pending}."
        self.log.setMarkdown(text)


class ArchiveTab(QWidget):
    """Archive / version history (§selfdev): a vertical list of saved app versions you can restore, with
    a user-set master DEFAULT, an immutable ROOT (blank-menu) factory reset, per-version purge, and
    manual GitHub backup. Versions are whole-app snapshots — restoring rolls the entire app back to that
    point as a new, non-destructive commit. git is the version store; this view reads the SQLite index."""

    restore_requested = pyqtSignal(int)
    setdefault_requested = pyqtSignal(int)
    purge_requested = pyqtSignal(int)
    reset_root_requested = pyqtSignal()
    push_requested = pyqtSignal()

    def __init__(self, memory, parent=None) -> None:
        super().__init__(parent)
        self.memory = memory
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 20)
        outer.setSpacing(12)
        intro = QLabel(
            "Every self-improvement is saved here as a version. Restore any of them to roll the whole app "
            "back — a new commit is made, so nothing is ever lost."
        )
        intro.setObjectName("archiveIntro")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        actions = QHBoxLayout()
        reset_btn = QPushButton("⟲  Reset to Default (Root) — blank menu")
        reset_btn.setObjectName("dangerButton")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setToolTip(
            "Factory reset: a clean app with only the core pillars and a blank menu — your lifeline if "
            "something breaks. Non-destructive (a new commit; history is kept)."
        )
        reset_btn.clicked.connect(self.reset_root_requested.emit)
        push_btn = QPushButton("⬆  Back up to GitHub")
        push_btn.setObjectName("ghostButton")
        push_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        push_btn.setToolTip("Push main to GitHub now (manual off-machine backup).")
        push_btn.clicked.connect(self.push_requested.emit)
        actions.addWidget(reset_btn)
        actions.addStretch(1)
        actions.addWidget(push_btn)
        outer.addLayout(actions)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._host = QWidget()
        self._list = QVBoxLayout(self._host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(10)
        scroll.setWidget(self._host)
        outer.addWidget(scroll, 1)
        self.refresh()

    def refresh(self) -> None:
        while self._list.count():
            item = self._list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        try:
            versions = self.memory.list_interface_versions()
        except Exception:
            versions = []
        if not versions:
            empty = QLabel("No versions yet. As HELIX builds features, each one is saved here.")
            empty.setObjectName("archiveMeta")
            self._list.addWidget(empty)
        for version in versions:
            self._list.addWidget(self._version_card(version))
        self._list.addStretch(1)

    def _version_card(self, v: dict) -> QFrame:
        is_root = bool(v.get("is_root"))
        is_default = bool(v.get("is_default"))
        protected = is_root or is_default
        vid = int(v.get("id"))
        card = QFrame()
        card.setObjectName("archiveCardRoot" if is_root else "archiveCard")
        col = QVBoxLayout(card)
        col.setSpacing(6)

        head = QHBoxLayout()
        name = QLabel(v.get("label") or (v.get("commit_sha") or "")[:10])
        name.setObjectName("archiveLabel")
        name.setWordWrap(True)
        head.addWidget(name, 1)
        if is_default:
            tag = QLabel("DEFAULT")
            tag.setObjectName("archiveTagDefault")
            head.addWidget(tag, 0, Qt.AlignmentFlag.AlignTop)
        if is_root:
            tag = QLabel("ROOT")
            tag.setObjectName("archiveTagRoot")
            head.addWidget(tag, 0, Qt.AlignmentFlag.AlignTop)
        col.addLayout(head)

        meta_bits = [(v.get("created_at") or "")[:16]]
        if not is_root:
            meta_bits.append((v.get("commit_sha") or "")[:10])
        meta = QLabel(" · ".join(b for b in meta_bits if b))
        meta.setObjectName("archiveMeta")
        col.addWidget(meta)

        prompt = (v.get("prompt") or "").strip()
        if prompt:
            preview = QLabel(prompt[:220] + ("…" if len(prompt) > 220 else ""))
            preview.setObjectName("archivePrompt")
            preview.setWordWrap(True)
            col.addWidget(preview)

        btns = QHBoxLayout()
        restore = QPushButton("Reset to this" if is_root else "Restore")
        restore.setObjectName("ghostButton")
        restore.setCursor(Qt.CursorShape.PointingHandCursor)
        restore.clicked.connect(
            lambda _=False, i=vid, r=is_root: (
                self.reset_root_requested.emit() if r else self.restore_requested.emit(i)
            )
        )
        btns.addWidget(restore)
        if not protected:
            setdef = QPushButton("Set as default")
            setdef.setObjectName("ghostButton")
            setdef.setCursor(Qt.CursorShape.PointingHandCursor)
            setdef.clicked.connect(lambda _=False, i=vid: self.setdefault_requested.emit(i))
            btns.addWidget(setdef)
        btns.addStretch(1)
        if not protected:
            purge = QPushButton("✕")
            purge.setObjectName("launcherRemove")
            purge.setFixedSize(26, 26)
            purge.setCursor(Qt.CursorShape.PointingHandCursor)
            purge.setToolTip("Permanently remove this version from the Archive")
            purge.clicked.connect(lambda _=False, i=vid: self.purge_requested.emit(i))
            btns.addWidget(purge)
        col.addLayout(btns)
        return card


class GuardrailsBox(QGroupBox):
    """Read-only display of the Twelve Commandments in Settings (§44). HELIX cannot change these: the
    coder may not edit the constitution (a protected path) and the approval gate auto-rejects any change
    that touches the guardrails. This panel just shows the law and its integrity status."""

    def __init__(self, parent=None) -> None:
        super().__init__("Guardrails — the Twelve Commandments", parent)
        self.setObjectName("guardrailsBox")
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        intact = selfdev_constitution.verify_integrity()
        status = QLabel(
            "🛡  Protected — HELIX cannot change these."
            if intact else "⚠  Integrity check FAILED — self-improvement is paused."
        )
        status.setObjectName("guardrailsStatus" if intact else "guardrailsStatusBad")
        status.setWordWrap(True)
        layout.addWidget(status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        for c in selfdev_constitution.commandments():
            line = QLabel(f"<b>{c.n}. {c.title}.</b> {c.text}")
            line.setObjectName("guardrailLine")
            line.setWordWrap(True)
            col.addWidget(line)
        col.addStretch(1)
        scroll.setWidget(host)
        scroll.setMinimumHeight(200)
        layout.addWidget(scroll)


class BuildView(QWidget):
    """A built app's home screen: what it is, a button to run or open it, and its README.

    Generated apps live in their own folder under data/builds/<slug>/. Running is best-effort: an HTML
    app opens in the browser; a Python app launches in a new console (needs Python on the machine);
    otherwise the folder is opened so the user can run it however its README says."""

    def __init__(self, build: dict, parent=None) -> None:
        super().__init__(parent)
        self._build = build
        self._path = build.get("path", "")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(12)

        req = (build.get("request") or "").strip()
        if req:
            blurb = QLabel(req)
            blurb.setObjectName("xpertHint")
            blurb.setWordWrap(True)
            layout.addWidget(blurb)

        row = QHBoxLayout()
        run_button = QPushButton("▶  Run")
        run_button.setObjectName("primaryButton")
        run_button.setCursor(Qt.CursorShape.PointingHandCursor)
        run_button.clicked.connect(self._run)
        folder_button = QPushButton("📂  Open folder")
        folder_button.setObjectName("ghostButton")
        folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        folder_button.clicked.connect(self._open_folder)
        row.addWidget(run_button)
        row.addWidget(folder_button)
        row.addStretch(1)
        layout.addLayout(row)

        readme = QTextEdit()
        readme.setReadOnly(True)
        readme.setPlainText(self._read_readme())
        layout.addWidget(readme, 1)

        self._status = QLabel("")
        self._status.setObjectName("xpertHint")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

    def _read_readme(self) -> str:
        from pathlib import Path
        base = Path(self._path)
        for name in ("README.md", "readme.md", "README.txt", "readme.txt"):
            p = base / name
            if p.exists():
                try:
                    return p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
        try:
            files = [f.name for f in base.iterdir() if f.is_file() and not f.name.startswith(".")]
            return "This app's files:\n  " + "\n  ".join(sorted(files))
        except OSError:
            return "(couldn't read the app folder)"

    def _open_folder(self) -> None:
        try:
            os.startfile(self._path)  # Windows file explorer
        except Exception as error:  # noqa: BLE001
            self._status.setText(f"Couldn't open the folder: {error}")

    def _run(self) -> None:
        import subprocess
        from helix.selfdev import builds as _builds
        entry = _builds.entry_point(self._path)
        try:
            if entry["kind"] == "html":
                os.startfile(entry["path"])
                self._status.setText("Opened in your browser.")
            elif entry["kind"] == "python":
                subprocess.Popen(
                    [sys.executable, entry["path"]], cwd=self._path,
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                )
                self._status.setText(f"Running {os.path.basename(entry['path'])} in a new window.")
            else:
                os.startfile(self._path)
                self._status.setText("No obvious entry point — opened the folder; see the README to run it.")
        except Exception as error:  # noqa: BLE001
            self._status.setText(f"Couldn't run it: {error}")


class DesignDialog(QDialog):
    """A working editor for designing an app (or task) with the AI before building it.

    A back-and-forth chat (Markdown, clickable links) to shape the idea, a name field, then one
    'Build it' button that submits the whole design for an uninterrupted build. The build engine writes
    the app start-to-finish with no further prompts; the result opens when it's done. Voice/orb are
    untouched — this is the focused, writing-first design surface."""

    step_signal = pyqtSignal(str)  # build progress, marshalled from the worker thread to the UI

    def __init__(self, memory: SQLiteMemory, kind: str = "app", on_built=None, parent=None) -> None:
        super().__init__(parent)
        self.memory = memory
        self.kind = kind if kind in ("task", "agent") else "app"
        self._on_built = on_built
        self._workers: set = set()
        self._messages: list = []
        self._md = ""
        self._busy = False
        self._client = ClaudeClient(ClaudeConfig(model=DEFAULT_RESEARCH_MODEL, timeout_seconds=90))

        noun = {"task": "task", "agent": "agent"}.get(self.kind, "app")
        self.setWindowTitle(f"Design your {noun}")
        self.setObjectName("designDialog")
        self.resize(720, 640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel(f"Design your {noun}")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        self.name_field = QLineEdit()
        self.name_field.setPlaceholderText(f"{noun.capitalize()} name (optional)")
        layout.addWidget(self.name_field)

        self.transcript = QTextBrowser()
        self.transcript.setOpenExternalLinks(True)  # links the AI gives are clickable
        self.transcript.setObjectName("designTranscript")
        layout.addWidget(self.transcript, 1)

        self.status = QLabel("")
        self.status.setObjectName("xpertHint")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        input_row = QHBoxLayout()
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText(f"Describe the {noun} you want — then refine it with me.")
        self.input.setFixedHeight(64)
        input_row.addWidget(self.input, 1)
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("ghostButton")
        self.send_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_button.clicked.connect(self._send)
        input_row.addWidget(self.send_button)
        layout.addLayout(input_row)

        button_row = QHBoxLayout()
        self.build_button = QPushButton("Create agent" if self.kind == "agent" else f"Build {noun}")
        self.build_button.setObjectName("primaryButton")
        self.build_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.build_button.setEnabled(False)  # enabled once the design has been discussed
        self.build_button.clicked.connect(self._build)
        close_button = QPushButton("Close")
        close_button.setObjectName("ghostButton")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(self.reject)
        button_row.addWidget(close_button)
        button_row.addStretch(1)
        button_row.addWidget(self.build_button)
        layout.addLayout(button_row)

        self.step_signal.connect(self.status.setText)

        if not self._client.is_configured():
            self.status.setText("Add your Claude API key in Settings to design and build.")
            self.send_button.setEnabled(False)
        else:
            self._render_intro(noun)

    def _render_intro(self, noun: str) -> None:
        self._md = (
            f"**HELIX** — Tell me what {noun} you'd like and I'll help you design it. "
            "When it's ready, hit the button below.\n\n---\n\n"
        )
        self.transcript.setMarkdown(self._md)

    def _append(self, who: str, text: str) -> None:
        self._md += f"**{who}**\n\n{text}\n\n---\n\n"
        self.transcript.setMarkdown(self._md)
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.send_button.setEnabled(not busy and self._client.is_configured())
        self.build_button.setEnabled(not busy and bool(self._messages))
        self.input.setReadOnly(busy)
        if message:
            self.status.setText(message)
        elif not busy:
            self.status.setText("")

    def _design_system(self) -> str:
        if self.kind == "agent":
            return (
                "You are HELIX's design partner, helping the user design an AGENT — a goal-driven "
                "automation that runs on its own through HELIX's tools. Have a SHARP, quick back-and-forth: "
                "nail down (1) the agent's GOAL, (2) its TRIGGER (manual for now; schedule/event later), "
                "(3) the ACTIONS it should take, and (4) GUARDRAILS — what it may do freely vs. what needs "
                "the user's approval (anything that spends money or reaches outward). You may use Markdown. "
                "Do NOT write code; this is design. Keep replies tight. When it's solid, give a short, "
                "clear statement of the agent's goal and how it should behave."
            )
        noun = "task" if self.kind == "task" else "app"
        return (
            f"You are HELIX's design partner, helping the user design a small {noun} before any code is "
            "written. Have a SHARP, quick back-and-forth: ask only the few questions you truly need, then "
            "propose a concrete design. You may use Markdown — headings, bold, bullet lists, tables, and "
            "links — to make the design clear and skimmable. Do NOT write the implementation code now; "
            "this is the design phase. Keep replies tight. When the design is solid, give a short, "
            f"build-ready spec of the {noun}. Prefer simple, self-contained {noun}s (a single HTML file "
            "is ideal)."
        )

    def _send(self) -> None:
        if self._busy:
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        self._append("You", text)
        self._messages.append({"role": "user", "content": text})
        self.input.clear()
        self._set_busy(True, "HELIX is thinking…")
        msgs = list(self._messages)
        system = self._design_system()
        spawn_worker(
            self._workers,
            lambda: self._client.chat(msgs, system=system, max_tokens=1500),
            self._reply,
        )

    def _reply(self, ok: bool, payload) -> None:
        self._set_busy(False)
        if not ok:
            self._append("HELIX", f"_(I hit a problem: {payload})_")
            return
        blocks = (payload or {}).get("content", []) or []
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text" and b.get("text")).strip()
        self._messages.append({"role": "assistant", "content": text or "…"})
        self._append("HELIX", text or "…")
        self.build_button.setEnabled(True)

    def _compose_request(self) -> str:
        lines = []
        for m in self._messages:
            who = "User" if m.get("role") == "user" else "HELIX (designer)"
            lines.append(f"{who}: {m.get('content', '')}")
        return "Build this from the agreed design.\n\nDesign discussion:\n" + "\n\n".join(lines)

    def _default_name(self) -> str:
        for m in self._messages:
            if m.get("role") == "user":
                return (m.get("content", "").strip().splitlines() or ["App"])[0][:40]
        return "App"

    def _build(self) -> None:
        if self._busy or not self._messages:
            return
        name = self.name_field.text().strip() or self._default_name()
        if self.kind == "agent":
            # An agent isn't a code build — save it as a goal-driven automation.
            record = agents_registry.add_agent(AppSettings(), name, goal=self._compose_request())
            if self._on_built is not None:
                try:
                    self._on_built(record["key"])
                except Exception:
                    pass
            self.accept()
            return
        noun = "task" if self.kind == "task" else "app"
        confirm = QMessageBox.question(
            self, f"Build {noun}",
            f"Build “{name}” now? This runs uninterrupted and can take a couple of minutes.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        request = self._compose_request()
        self._set_busy(True, "Building — this runs uninterrupted…")
        spawn_worker(
            self._workers,
            lambda: selfdev_builds.build_app(name, request, on_step=lambda s: self.step_signal.emit(s)),
            self._built,
        )

    def _built(self, ok: bool, payload) -> None:
        self._set_busy(False)
        if not ok:
            self.status.setText(f"Build failed: {payload}")
            return
        ws, result = payload
        if not getattr(result, "ok", False):
            self.status.setText(f"Build failed: {getattr(result, 'error', 'unknown error')}")
            return
        if self._on_built is not None:
            try:
                self._on_built(ws.name)
            except Exception:
                pass
        self.accept()


class HelixMainWindow(QMainWindow):
    def __init__(self, memory: SQLiteMemory) -> None:
        super().__init__()
        self.memory = memory
        self.setWindowTitle("HELIX")
        icon_path = load_config().root_dir / "assets" / "helix.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))  # taskbar / title-bar icon
        self.setStatusBar(QStatusBar())

        # The Console conversation is the front door; deep views are summoned on request (a sentence,
        # the menu, or a built app). The orb IS the home — no tabs.
        self.xpert_tab = XpertTab(memory)

        # Settings panel — voice speed + audio devices + keys, summonable like any other panel.
        # Reparents Xpert's secondary controls here (keeps their wiring).
        settings_panel = QWidget()
        settings_layout = QVBoxLayout(settings_panel)
        settings_layout.setContentsMargins(18, 14, 18, 16)
        settings_layout.setSpacing(14)
        settings_layout.addWidget(self._make_key_box())
        settings_layout.addWidget(self.xpert_tab.controls_box)
        new_chat_button = QPushButton("New chat")
        new_chat_button.setObjectName("ghostButton")
        new_chat_button.setCursor(Qt.CursorShape.PointingHandCursor)
        new_chat_button.clicked.connect(self.xpert_tab._new_chat)
        settings_layout.addWidget(new_chat_button, 0, Qt.AlignmentFlag.AlignLeft)
        restore_menu_button = QPushButton("Restore hidden menu items")
        restore_menu_button.setObjectName("ghostButton")
        restore_menu_button.setCursor(Qt.CursorShape.PointingHandCursor)
        restore_menu_button.clicked.connect(self._restore_menu_items)
        settings_layout.addWidget(restore_menu_button, 0, Qt.AlignmentFlag.AlignLeft)
        settings_layout.addWidget(GuardrailsBox())
        settings_layout.addStretch(1)

        self.archive_tab = ArchiveTab(memory)
        # The menu starts blank: the only built-in panels are Settings and Archive. Everything else
        # in the menu is an app the user has built (from selfdev_builds.list_builds()).
        self.panel_host = PanelHost(
            {
                "settings": ("App settings", settings_panel),
                "tasksettings": ("Task settings", self._make_task_settings_panel()),
                "agentsettings": ("Agent settings", self._make_agent_settings_panel()),
                "archive": ("Archive", self.archive_tab),
            },
            on_home=self._show_home,
            settings=AppSettings(),
        )
        # Register a panel for each app the user has already built, and build the menu from them.
        self._build_views: dict = {}
        for build in selfdev_builds.list_builds():
            self._register_build_view(build)
        # Register a panel for each existing agent.
        self._agent_views: dict = {}
        for agent in agents_registry.list_agents(AppSettings()):
            self._register_agent_view(agent)
        self.launcher = self._make_launcher()
        self.tasks_view = TasksView(on_home=self._show_home, settings=AppSettings(), on_pick=self.open_view)
        self.agents_view = AgentsView(on_home=self._show_home, settings=AppSettings(), on_pick=self.open_view)
        self.console = ConsoleView(
            self.xpert_tab, memory, self.open_view, self.show_launcher, self.show_tasks, self.show_agents
        )

        self.stack = QStackedWidget()
        self.stack.addWidget(self.console)      # 0 — the orb home (default)
        self.stack.addWidget(self.panel_host)   # 1 — a summoned panel
        self.stack.addWidget(self.launcher)     # 2 — the launcher menu
        self.stack.addWidget(self.tasks_view)   # 3 — the Tasks screen
        self.stack.addWidget(self.agents_view)  # 4 — the Agents screen
        self.setCentralWidget(self.stack)

        # The Console conversation can open screens and announce a freshly-built app.
        self.xpert_tab.request_show_screen.connect(self.open_view)
        self.xpert_tab.request_build_created.connect(self._on_build_created)
        self.archive_tab.restore_requested.connect(self._archive_restore)
        self.archive_tab.setdefault_requested.connect(self._archive_set_default)
        self.archive_tab.purge_requested.connect(self._archive_purge)
        self.archive_tab.reset_root_requested.connect(self._archive_reset_root)
        self.archive_tab.push_requested.connect(self._archive_push)

        self.refresh_all()

        # Self-improvement background beat (§selfdev): apply a pending restart on a safe tick.
        self.settings = AppSettings()
        selfdev_restart.clear_restart(self.settings)  # consume any flag from the session that just restarted
        self._selfdev_timer = QTimer(self)
        self._selfdev_timer.timeout.connect(self._selfdev_tick)
        self._selfdev_timer.start(60000)  # every 60s
        # Auto crash-fix (§selfdev): draft fixes for new logged crashes, off-thread, ~2 min after
        # launch and every 6 hours. Drafts only — never auto-merged (approval still required).
        self._sd_workers = set()
        self._crash_busy = False
        # Backfill the Archive from git history and clean up provenance for any removed feature, off-thread.
        QTimer.singleShot(8000, self._archive_startup_sync)
        QTimer.singleShot(120000, self._check_crashes)
        self._crash_timer = QTimer(self)
        self._crash_timer.timeout.connect(self._check_crashes)
        self._crash_timer.start(6 * 3600 * 1000)
        # Email approval (§selfdev): poll for Brian's Yes/No replies, off-thread, every 3 min.
        self._email_busy = False
        self._email_timer = QTimer(self)
        self._email_timer.timeout.connect(self._poll_email)
        self._email_timer.start(180000)
        # Guardrails tripwire (§44): if the constitution was tampered with, pause autonomous self-writing.
        if not selfdev_constitution.verify_integrity():
            self.statusBar().showMessage("⚠ Guardrails integrity check failed — self-improvement paused.", 0)

    def refresh_all(self) -> None:
        self.xpert_tab.refresh()
        self.statusBar().showMessage("HELIX memory synced", 3000)

    def _make_key_box(self) -> QGroupBox:
        """The Claude API key field for Settings — the one credential HELIX needs to build apps."""
        box = QGroupBox("Claude API key")
        layout = QVBoxLayout(box)
        hint = QLabel(
            "HELIX uses Claude to build your apps. Paste your key below — it's stored locally on this "
            "machine only. Get one at console.anthropic.com."
        )
        hint.setObjectName("xpertHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._key_field = QLineEdit()
        self._key_field.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_field.setPlaceholderText("sk-ant-…")
        existing = AppSettings().get(CLAUDE_API_KEY_SETTING, "") or ""
        if existing:
            self._key_field.setText(existing)
        layout.addWidget(self._key_field)
        save = QPushButton("Save key")
        save.setObjectName("ghostButton")
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.clicked.connect(self._save_key)
        layout.addWidget(save, 0, Qt.AlignmentFlag.AlignLeft)
        return box

    def _save_key(self) -> None:
        key = self._key_field.text().strip()
        AppSettings().set(CLAUDE_API_KEY_SETTING, key)
        self.statusBar().showMessage("Saved your Claude key." if key else "Cleared the Claude key.", 5000)
        try:
            self.console.refresh_key_gate()
        except Exception:
            pass

    def _make_task_settings_panel(self) -> QWidget:
        """Settings scoped to Tasks (opened by the Settings card on the Tasks screen) — kept separate
        from the app-wide settings the Menu opens."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(14)
        hint = QLabel(
            "Settings for your tasks. Tasks are quick actions you run from the Tasks screen — describe "
            "one with “New Task”, and remove one with its ✕."
        )
        hint.setObjectName("xpertHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        restore = QPushButton("Restore removed tasks")
        restore.setObjectName("ghostButton")
        restore.setCursor(Qt.CursorShape.PointingHandCursor)
        restore.clicked.connect(self._restore_tasks)
        layout.addWidget(restore, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return panel

    def _restore_tasks(self) -> None:
        AppSettings().set(TASKS_HIDDEN_SETTING, [])
        try:
            self.tasks_view._rebuild()
        except Exception:
            pass
        self.statusBar().showMessage("Restored removed tasks.", 5000)

    def _register_build_view(self, build: dict) -> None:
        slug = build.get("slug", "")
        if not slug or slug in self._build_views:
            return
        view = BuildView(build)
        self._build_views[slug] = view
        self.panel_host.add_view(slug, build.get("name", slug), view)

    def _make_launcher(self) -> "Launcher":
        """Build the menu: New app + Settings, then a card for each app the user has built."""
        builds_list = selfdev_builds.list_builds()
        core_menu = [
            ("newapp", "New app", "design it with me, then build"),
            ("settings", "Settings", "voice · keys · devices"),
        ]
        build_menu = [
            (b["slug"], b.get("name", b["slug"]), (b.get("request") or "")[:60])
            for b in builds_list
        ]
        return Launcher(
            core_menu + build_menu,
            on_pick=self.open_view,
            on_home=self._show_home,
            settings=AppSettings(),
            removable_keys={b["slug"] for b in builds_list},  # the ✕ on a built app deletes it
            on_remove_code=self._remove_build,
            permanent_keys=selfdev_constitution.PERMANENT_MENU_KEYS,  # Settings/Archive: no ✕, ever
        )

    def _rebuild_menu(self) -> None:
        """Recreate the launcher (after a build is added/removed) and swap it into the stack in place."""
        new_launcher = self._make_launcher()
        idx = self.stack.indexOf(self.launcher)
        if idx != -1:
            self.stack.insertWidget(idx, new_launcher)
            self.stack.removeWidget(self.launcher)
            self.launcher.deleteLater()
        else:
            self.stack.addWidget(new_launcher)
        self.launcher = new_launcher

    def _on_build_created(self, slug: str) -> None:
        """A new app was built via the Console — register its panel, refresh the menu, and open it."""
        build = next((b for b in selfdev_builds.list_builds() if b.get("slug") == slug), None)
        if build is None:
            self.statusBar().showMessage("Built your app — it's in your menu now.", 8000)
            return
        self._register_build_view(build)
        self._rebuild_menu()
        self.statusBar().showMessage(f"Built {build.get('name', slug)} — opening it.", 8000)
        self.open_view(slug)

    def _remove_build(self, slug: str) -> None:
        """The ✕ on a built-app card: delete its workspace (code + history) after confirmation."""
        build = next((b for b in selfdev_builds.list_builds() if b.get("slug") == slug), None)
        name = (build.get("name", slug) if build else slug)
        confirm = QMessageBox.question(
            self, "Delete app", f"Delete “{name}” and all its files?\nThis can't be undone.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        removed = selfdev_builds.delete_build(slug)
        if not removed:
            self.statusBar().showMessage(
                f"Couldn't delete {name} — it may be open in a browser or Explorer. Close it and try again.",
                9000,
            )
            return
        self._build_views.pop(slug, None)
        self._show_home()
        self._rebuild_menu()
        self.statusBar().showMessage(f"Deleted {name}.", 6000)

    # -- agents -------------------------------------------------------------- #

    def _register_agent_view(self, agent: dict) -> None:
        key = agent.get("key", "")
        if not key or key in self._agent_views:
            return
        view = AgentView(self.memory, AppSettings(), key)
        self._agent_views[key] = view
        self.panel_host.add_view(key, agent.get("name", key), view)

    def _make_agent_settings_panel(self) -> QWidget:
        """Settings scoped to Agents (opened by the Settings card on the Agents screen)."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(14)
        hint = QLabel(
            "Settings for your agents. An agent is a goal-driven automation — design one with “New "
            "Agent”, run it from its page, and toggle it on. Anything that spends money or reaches "
            "outward always asks for your approval first."
        )
        hint.setObjectName("xpertHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        return panel

    def _on_agent_created(self, key: str) -> None:
        """A new agent was created via the editor — register its panel, refresh the list, and open it."""
        agent = agents_registry.get_agent(AppSettings(), key)
        if agent is None:
            return
        self._register_agent_view(agent)
        self.agents_view._rebuild()
        self.statusBar().showMessage(f"Created agent {agent.get('name', key)} — opening it.", 8000)
        self.open_view(key)

    def show_agents(self) -> None:
        self.agents_view._rebuild()  # reflect any added/removed agents
        self.stack.setCurrentIndex(4)

    def open_view(self, name: str | None = None) -> None:
        """Open a screen by key. 'newapp'/'newtask'/'newagent' open the design editor; tasks/agents/
        archive are special; a built-app or agent key opens its panel. No/unknown name → the menu."""
        if name == "newapp":
            self._open_design("app")
        elif name == "newtask":
            self._open_design("task")
        elif name == "newagent":
            self._open_design("agent")
        elif name in ("new", None):
            self._show_home()
        elif name == "tasks":
            self.show_tasks()
        elif name == "agents":
            self.show_agents()
        elif name == "archive":
            self._open_archive()
        elif name and self.panel_host.show_view(name):
            self.stack.setCurrentIndex(1)
        else:
            self.show_launcher()

    def _open_design(self, kind: str) -> None:
        """Open the design editor for a new app/task/agent; on finish, register + open the result."""
        on_built = self._on_agent_created if kind == "agent" else self._on_build_created
        dialog = DesignDialog(self.memory, kind=kind, on_built=on_built, parent=self)
        dialog.exec()

    def show_launcher(self) -> None:
        self.stack.setCurrentIndex(2)

    def show_tasks(self) -> None:
        self.stack.setCurrentIndex(3)

    def _restore_menu_items(self) -> None:
        """A small dialog listing the menu items hidden via the ✕ badge, each with a Restore button."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Menu items")
        layout = QVBoxLayout(dialog)
        hidden = self.launcher.hidden_items()
        if not hidden:
            layout.addWidget(QLabel("No hidden menu items.\nTap the ✕ on a menu card to hide it."))
        else:
            layout.addWidget(QLabel("Hidden menu items — restore to show them in the menu again:"))
            for key, label, _subtitle in hidden:
                row = QHBoxLayout()
                row.addWidget(QLabel(label))
                row.addStretch(1)
                restore = QPushButton("Restore")
                restore.setObjectName("ghostButton")
                restore.setCursor(Qt.CursorShape.PointingHandCursor)

                def _do_restore(_=False, k=key, d=dialog) -> None:
                    self.launcher.restore(k)
                    d.accept()
                    self._restore_menu_items()  # reopen so multiple items can be restored in a row

                restore.clicked.connect(_do_restore)
                row.addWidget(restore)
                layout.addLayout(row)
        close = QPushButton("Close")
        close.setObjectName("ghostButton")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        dialog.exec()

    def _show_home(self) -> None:
        self.stack.setCurrentIndex(0)

    def _trade_cycle_active(self) -> bool:
        """No long-running foreground work to protect anymore, so a pending restart is always safe to
        apply on the next tick. (Kept as a hook in case a future Build wants to defer restarts.)"""
        return False

    def _selfdev_tick(self) -> None:
        """Background self-improvement beat. Applies a pending restart when it's safe (not mid-trade)."""
        self._apply_restart_if_safe()

    def _apply_restart_if_safe(self) -> None:
        """Perform a pending restart now if no trade cycle is mid-flight (otherwise the 60s tick gets it).
        Shared by the background beat and the Archive's restore/reset (which set the restart flag)."""
        try:
            if selfdev_restart.restart_pending(self.settings) and not self._trade_cycle_active():
                self.statusBar().showMessage("Restarting to apply the change…", 5000)
                selfdev_restart.perform_restart(self.settings)  # supervisor relaunches us, or we self-spawn
                QApplication.exit(selfdev_restart.RESTART_EXIT_CODE)
        except Exception:
            pass

    # -- Archive: versions, restore, root reset, purge, backup (§selfdev) ------ #

    def _archive_repo(self) -> str:
        return str(load_config().root_dir)

    def _open_archive(self) -> None:
        """Show the Archive, syncing git history → SQLite off-thread first so the list is current."""
        if self.panel_host.show_view("archive"):
            self.stack.setCurrentIndex(1)
        self.statusBar().showMessage("Loading versions…", 3000)
        spawn_worker(
            self._sd_workers,
            lambda: selfdev_versioning.sync(self.memory, self.settings, self._archive_repo()),
            self._archive_synced,
        )

    def _archive_synced(self, ok: bool, payload) -> None:
        self.archive_tab.refresh()
        if not ok:
            self.statusBar().showMessage(f"Couldn't refresh versions: {payload}", 6000)

    def _archive_restore(self, version_id: int) -> None:
        row = self.memory.get_interface_version(version_id)
        if not row:
            return
        confirm = QMessageBox.question(
            self,
            "Restore version",
            f"Roll the WHOLE app back to:\n\n{(row.get('label') or '')[:120]}\n\n"
            "A new restore-commit is made (nothing is lost), then HELIX restarts to load it.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._show_home()
        self.statusBar().showMessage("Restoring…", 8000)
        spawn_worker(
            self._sd_workers,
            lambda: selfdev_versioning.restore_version(
                self._archive_repo(), row["commit_sha"], label=(row.get("label") or "")[:40]
            ),
            self._restore_done,
        )

    def _archive_reset_root(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Reset to Default (Root)",
            "Reset HELIX to its ROOT baseline — a clean app with a BLANK menu (core pillars only).\n\n"
            "This is the factory reset / lifeline. A new commit is made (history is kept), then HELIX restarts.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._show_home()
        self.statusBar().showMessage("Resetting to root…", 8000)
        spawn_worker(
            self._sd_workers,
            lambda: selfdev_versioning.reset_to_root(self._archive_repo(), self.memory),
            self._restore_done,
        )

    def _restore_done(self, ok: bool, payload) -> None:
        if ok and payload:
            selfdev_restart.request_restart(self.settings)
            self.statusBar().showMessage("Restored — HELIX will restart to apply it.", 8000)
            self._apply_restart_if_safe()
        elif ok:
            self.statusBar().showMessage("Already at that version — nothing to restore.", 6000)
        else:
            self.statusBar().showMessage(f"Restore failed: {payload}", 9000)

    def _archive_set_default(self, version_id: int) -> None:
        if selfdev_versioning.set_default(self.memory, version_id):
            self.statusBar().showMessage("Master default set.", 5000)
        self.archive_tab.refresh()

    def _archive_purge(self, version_id: int) -> None:
        row = self.memory.get_interface_version(version_id)
        name = ((row.get("label") if row else "") or "this version")[:80]
        confirm = QMessageBox.question(
            self,
            "Remove version",
            f"Permanently remove “{name}” from the Archive?\nThis can't be undone.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        ok, message = selfdev_versioning.purge_version(self._archive_repo(), self.memory, version_id)
        self.statusBar().showMessage(message, 6000)
        self.archive_tab.refresh()

    def _archive_push(self) -> None:
        self.statusBar().showMessage("Backing up to GitHub…", 8000)
        spawn_worker(
            self._sd_workers,
            lambda: selfdev_versioning.push_to_github(self._archive_repo()),
            self._push_done,
        )

    def _push_done(self, ok: bool, payload) -> None:
        if ok and isinstance(payload, tuple):
            self.statusBar().showMessage(payload[1], 8000)
        else:
            self.statusBar().showMessage(f"Backup failed: {payload}", 8000)

    def _archive_startup_sync(self) -> None:
        """One-shot, ~8s after launch: reconcile git history into the Archive and prune provenance for
        features that have been removed — so SQLite cleanup happens even if the Archive is never opened."""
        spawn_worker(
            self._sd_workers,
            lambda: selfdev_versioning.sync(self.memory, self.settings, self._archive_repo()),
            lambda ok, payload: None,
        )

    def _check_crashes(self) -> None:
        """Off-thread: draft fixes for any new logged crash (recorded pending; never auto-merged)."""
        if not selfdev_constitution.verify_integrity():
            return  # guardrails compromised — pause autonomous self-modification (Commandment 7)
        if self._crash_busy:
            return
        self._crash_busy = True
        spawn_worker(
            self._sd_workers,
            lambda: selfdev_triggers.maybe_fix_crashes(self.settings),
            self._crashes_done,
        )

    def _crashes_done(self, ok: bool, payload) -> None:
        self._crash_busy = False
        if ok and payload:
            self.statusBar().showMessage(
                f"Drafted {len(payload)} crash fix(es) — ask Xpert to review and approve", 10000
            )

    def _poll_email(self) -> None:
        """Off-thread: apply any Yes/No email replies to pending self-improvements."""
        if self._email_busy or not selfdev_mailer.is_configured(self.settings):
            return
        self._email_busy = True
        spawn_worker(self._sd_workers, lambda: selfdev_mailer.poll_replies(self.settings), self._email_done)

    def _email_done(self, ok: bool, payload) -> None:
        self._email_busy = False
        if ok and payload:
            applied = ", ".join(f"{a['action']} {a['branch']}" for a in payload)
            self.statusBar().showMessage(f"Email approval applied: {applied}", 10000)


# --- Audio devices + hands-free wake-word ("HELIX") voice detection (§23) --------------------- #

XPERT_INPUT_DEVICE_SETTING = "xpert_input_device"    # preferred mic, by description
XPERT_OUTPUT_DEVICE_SETTING = "xpert_output_device"  # preferred speaker, by description
XPERT_VOICE_SPEED_SETTING = "xpert_voice_speed"      # HELIX's talking rate (×), default 1.5
XPERT_VOICE_SETTING = "xpert_voice"                  # HELIX's neural voice id (edge-tts)
XPERT_VOICE_INPUT_SETTING = "xpert_voice_input_on"   # hands-free mic channel on/off, default on

# Energy-based voice-activity detection (VAD). The speech threshold is ADAPTIVE — it tracks the
# ambient noise floor, so it works across mics (a quiet close-talk headset vs. a noisier array mic)
# instead of a single fixed level that mis-fires on one and goes deaf on the other.
WAKE_RMS_FLOOR = 260.0       # absolute minimum speech threshold (int16 RMS)
WAKE_SPEECH_FACTOR = 3.2     # speech must be this many× the running ambient noise floor
WAKE_NOISE_INIT = 200.0      # starting noise-floor estimate
WAKE_END_SILENCE_S = 3.0     # this much trailing quiet ends an utterance (allow natural pauses)
WAKE_MIN_SPEECH_S = 0.3      # ignore shorter blips (clicks, coughs)
WAKE_MAX_UTTER_S = 12.0      # hard cap per utterance
WAKE_PREROLL_S = 0.25        # keep this much pre-speech audio so the wake word isn't clipped
# Conversation session (§23): once HELIX answers a wake-word command, it stays in an active
# session and keeps listening without the wake word until the user dismisses it or it goes quiet
# for SESSION_IDLE_MS. A 1-second tick drives the on-screen countdown.
SESSION_IDLE_MS = 5 * 60 * 1000   # end the session after this much inactivity (5 minutes)
SESSION_TICK_MS = 1000            # how often the on-screen session countdown refreshes
# Accept the obvious mis-hearings of "HELIX" so a clear command still lands.
_WAKE_RE = re.compile(r"\b(?:hey\s+|ok\s+|okay\s+)?(?:he+lix|helics|healix|helex|heelux)\b[\s,.:;!?-]*", re.IGNORECASE)
# Phrases that close an active conversation session immediately (no wake word needed).
_DISMISSAL_RE = re.compile(
    r"\b(?:good\s*bye|bye(?:\s+now)?|be\s+right\s+back|brb|i'?ll\s+be\s+back|"
    r"that'?s\s+all|thank(?:s| you)\s*,?\s*he+lix)\b",
    re.IGNORECASE,
)


def _pcm_rms(pcm: bytes) -> float:
    """RMS level of 16-bit little-endian mono PCM (stdlib only — no numpy, no deprecated audioop)."""
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def _split_wake(text: str) -> tuple:
    """(matched, command): if 'HELIX' is in `text`, return True + the words after it, else (False, '')."""
    match = _WAKE_RE.search(text or "")
    if not match:
        return False, ""
    return True, (text[match.end():] or "").strip()


def _is_dismissal(text: str) -> bool:
    """True if `text` is a phrase that should close an active conversation session
    (e.g. 'goodbye', 'that's all', 'thanks HELIX'). Pure, so it's unit-testable."""
    return bool(_DISMISSAL_RE.search(text or ""))


def _write_wav16(data: bytes, path: str) -> None:
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)  # Int16
        handle.setframerate(16000)
        handle.writeframes(data)


def _find_audio_device(devices: list, description: str):
    """Return the QAudioDevice whose description matches `description`, else None."""
    for device in devices or []:
        try:
            if device.description() == description:
                return device
        except Exception:
            continue
    return None


def _mono16k_format() -> QAudioFormat:
    fmt = QAudioFormat()
    fmt.setSampleRate(16000)
    fmt.setChannelCount(1)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    return fmt


class VadSegmenter:
    """Turns a stream of PCM chunks into complete spoken utterances using energy + trailing silence.
    Pure (no Qt) so the segmentation is unit-testable; WakeWordListener feeds it live mic chunks."""

    def __init__(self, sample_rate: int = 16000) -> None:
        bytes_per_s = sample_rate * 2  # 16-bit mono
        self._end_silence = int(WAKE_END_SILENCE_S * bytes_per_s)
        self._min_speech = int(WAKE_MIN_SPEECH_S * bytes_per_s)
        self._max_bytes = int(WAKE_MAX_UTTER_S * bytes_per_s)
        self._preroll_cap = int(WAKE_PREROLL_S * bytes_per_s)
        self._noise = WAKE_NOISE_INIT  # adapts to ambient; persists across utterances
        self.reset()

    def reset(self) -> None:
        self._in_speech = False
        self._utter = bytearray()
        self._silence = 0
        self._preroll = bytearray()

    @property
    def threshold(self) -> float:
        return max(WAKE_RMS_FLOOR, self._noise * WAKE_SPEECH_FACTOR)

    def push(self, chunk: bytes):
        """Feed a chunk; return a completed utterance (bytes) when one ends, else None."""
        if not chunk:
            return None
        rms = _pcm_rms(chunk)
        loud = rms >= self.threshold
        if loud:
            if not self._in_speech:
                self._in_speech = True
                self._utter = bytearray(self._preroll)  # seed with pre-roll so the wake word survives
                self._preroll = bytearray()
            self._utter += chunk
            self._silence = 0
        elif self._in_speech:
            self._utter += chunk
            self._silence += len(chunk)
            if self._silence >= self._end_silence:
                return self._finish()
        else:
            self._noise = 0.95 * self._noise + 0.05 * rms  # track the ambient noise floor
            self._preroll += chunk
            if len(self._preroll) > self._preroll_cap:
                del self._preroll[: len(self._preroll) - self._preroll_cap]
        if self._in_speech and len(self._utter) >= self._max_bytes:
            return self._finish()
        return None

    def _finish(self):
        utter = bytes(self._utter)
        spoken = len(utter) - self._silence  # rough speech length, minus trailing quiet
        self.reset()
        return utter if spoken >= self._min_speech else None


class MicRecorder(QObject):
    """Push-to-talk mic capture via QtMultimedia's QAudioSource, written out as a 16 kHz mono WAV
    that faster-whisper transcribes. Optional/guarded: if the multimedia backend or an input device
    is missing, is_available() is False and the Xpert tab disables the Talk button gracefully
    (mirroring how edge-tts / faster-whisper degrade)."""

    def __init__(self, device=None, parent=None) -> None:
        super().__init__(parent)
        self._source = None
        self._io = None
        self._buffer = bytearray()
        self._device = None
        self._format = None
        self._available = False
        try:
            if device is None or device.isNull():
                device = QMediaDevices.defaultAudioInput()
            if device is None or device.isNull():
                return
            self._device = device
            self._format = _mono16k_format()
            self._available = True
        except Exception:
            self._available = False

    def is_available(self) -> bool:
        return self._available

    def start(self) -> bool:
        if not self._available:
            return False
        try:
            self._buffer = bytearray()
            self._source = QAudioSource(self._device, self._format, self)
            self._io = self._source.start()
            if self._io is None:
                self._source = None
                return False
            self._io.readyRead.connect(self._on_ready)
            return True
        except Exception:
            self._source = None
            self._io = None
            return False

    def _on_ready(self) -> None:
        if self._io is not None:
            self._buffer += bytes(self._io.readAll())

    def stop(self) -> bytes:
        if self._io is not None:
            try:
                self._buffer += bytes(self._io.readAll())
            except Exception:
                pass
        if self._source is not None:
            try:
                self._source.stop()
            except Exception:
                pass
        data = bytes(self._buffer)
        self._buffer = bytearray()
        self._source = None
        self._io = None
        return data

    def save_wav(self, data: bytes, path: str) -> None:
        _write_wav16(data, path)


class WakeWordListener(QObject):
    """Always-on, hands-free mic capture for the 'HELIX' wake word. Continuously reads the mic,
    segments speech with VadSegmenter, and emits each finished utterance for transcription.
    Processing is gated by set_active() so it goes quiet while HELIX is transcribing / thinking /
    speaking (it never transcribes its own reply). Optional/guarded like MicRecorder."""

    utterance = pyqtSignal(bytes)
    level = pyqtSignal(float)  # 0..1 mic level, for a live meter

    def __init__(self, device=None, parent=None) -> None:
        super().__init__(parent)
        self._source = None
        self._io = None
        self._device = None
        self._format = None
        self._available = False
        self._active = False
        self._seg = VadSegmenter()
        try:
            if device is None or device.isNull():
                device = QMediaDevices.defaultAudioInput()
            if device is None or device.isNull():
                return
            self._device = device
            self._format = _mono16k_format()
            self._available = True
        except Exception:
            self._available = False

    def is_available(self) -> bool:
        return self._available

    def start(self) -> bool:
        if not self._available:
            return False
        try:
            self._seg.reset()
            self._source = QAudioSource(self._device, self._format, self)
            self._io = self._source.start()
            if self._io is None:
                self._source = None
                return False
            self._io.readyRead.connect(self._on_ready)
            self._active = True
            return True
        except Exception:
            self._source = None
            self._io = None
            return False

    def set_active(self, on: bool) -> None:
        """Gate processing without tearing down the stream: while inactive, mic chunks are drained
        and discarded (VAD reset), so HELIX never hears / transcribes its own spoken replies."""
        if on and not self._active:
            self._seg.reset()
        self._active = bool(on)

    def stop(self) -> None:
        if self._source is not None:
            try:
                self._source.stop()
            except Exception:
                pass
        self._source = None
        self._io = None
        self._active = False
        self._seg.reset()

    def _on_ready(self) -> None:
        if self._io is None:
            return
        chunk = bytes(self._io.readAll())  # always drain so the device buffer can't back up
        if not chunk or not self._active:
            return
        self.level.emit(min(1.0, _pcm_rms(chunk) / 8000.0))
        utter = self._seg.push(chunk)
        if utter:
            self.utterance.emit(utter)


class ChatInput(QPlainTextEdit):
    """The ask box — multi-line: Enter sends, Shift+Enter makes a new line (for paragraphs). Grows with
    its content up to a few lines, like a modern chat input."""

    submitted = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("askBox")
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(
            "QPlainTextEdit#askBox{border:1px solid #2f6c80;border-radius:12px;"
            "background-color:rgba(13,30,37,0.75);color:#eaffff;padding:10px 14px;font-size:13pt;}"
            "QPlainTextEdit#askBox:focus{border:1px solid #1dd8ff;background-color:rgba(16,38,46,0.9);}"
        )
        self._min_h = 70
        self._max_h = 180
        self.setFixedHeight(self._min_h)
        self.document().contentsChanged.connect(self._adjust_height)

    def _adjust_height(self) -> None:
        height = int(self.document().size().height()) + 2 * int(self.frameWidth()) + 28
        self.setFixedHeight(max(self._min_h, min(self._max_h, height)))

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class XpertTab(QWidget):
    """The HELIX 'brain' - a two-way J.A.R.V.I.S.-style voice assistant that can act on every
    pillar, plus the one-way expert overview across all five pillars (H E L I X)."""

    # Worker -> main-thread signals (Qt queues these across threads, so tool side effects that
    # touch widgets are marshalled safely back to the UI thread).
    convo_step = pyqtSignal(str)            # live "what HELIX is doing now" status during a turn
    request_show_screen = pyqtSignal(str)   # ask the main window to open a screen (menu/tasks/archive/settings)
    request_build_created = pyqtSignal(str) # a new app was built — refresh the menu so its card appears

    def __init__(self, memory: SQLiteMemory) -> None:
        super().__init__()
        self.memory = memory
        self.settings = AppSettings()
        self._workers = set()
        self._state = None
        # SQLite-backed conversation persistence so HELIX retains context across restarts. Self-
        # contained (its own tables in the same data/helix.db), resumes the most recent session on
        # launch, and writes each turn immediately (a crash loses at most the in-flight turn).
        try:
            self._convo_store = ConversationStore(memory.db_path)
        except Exception:
            self._convo_store = None  # persistence is best-effort; never block the conversation
        # Conversation state for the voice assistant (§23).
        self._history = []            # full Messages-API history; persists across turns
        self._pending_action = None   # a money/outward action awaiting an explicit spoken "yes"
        self._convo_state = "idle"
        self._convo_context = ""      # HELIX live context, snapshotted at the start of each turn
        self._speak_done_cb = None    # called when the current spoken reply finishes
        self._pending_speech = ""
        # Hands-free wake-word ("HELIX") state. Always on: started right after launch (no toggle); it
        # degrades silently to push-to-talk + typing if the mic / voice model / Claude key isn't ready.
        self._handsfree = False
        self._wake_listener = None
        self._loading_devices = False
        # Conversation session: while active, utterances need no wake word (§23). The idle timer
        # ends the session after SESSION_IDLE_MS of quiet; the tick refreshes the on-screen countdown.
        self._session_active = False
        self._session_timer = QTimer(self)
        self._session_timer.setSingleShot(True)
        self._session_timer.timeout.connect(lambda: self._end_session(spoken=False))
        self._session_tick = QTimer(self)
        self._session_tick.setInterval(SESSION_TICK_MS)
        self._session_tick.timeout.connect(self._update_session_indicator)
        self._setup_tts()
        self._setup_mic()
        self._router = self._build_router()
        self.convo_step.connect(self._on_convo_step)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer_layout.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 0, 2, 2)
        layout.setSpacing(12)

        subtitle = QLabel(
            "Describe an app and I’ll build it. Say “HELIX”, hold the button to talk, or just type."
        )
        subtitle.setObjectName("xpertHint")
        subtitle.setWordWrap(True)
        self._subtitle = subtitle

        # --- Two-way voice conversation: the J.A.R.V.I.S. assistant (§23) ---
        self.convo_box = QGroupBox("Conversation")
        convo_layout = QVBoxLayout(self.convo_box)
        convo_layout.setSpacing(12)
        self.transcript = QTextEdit()
        self.transcript.setObjectName("briefingPanel")
        self.transcript.setReadOnly(True)
        self.transcript.setMinimumHeight(340)
        self.convo_progress = QProgressBar()
        self.convo_progress.setRange(0, 0)
        self.convo_progress.setTextVisible(False)
        self.convo_progress.setMaximumHeight(6)
        self.convo_progress.setVisible(False)
        self.convo_status = QLabel("")
        self.convo_status.setObjectName("convoStatus")
        self.convo_status.setWordWrap(True)

        self.talk_button = QPushButton("\U0001f3a4  Hold to Talk")
        self.talk_button.setMinimumHeight(48)
        self.talk_button.pressed.connect(self._on_talk_pressed)
        self.talk_button.released.connect(self._on_talk_released)
        self._new_chat_button = new_chat_button = QPushButton("New chat")
        new_chat_button.clicked.connect(self._new_chat)
        self.text_input = ChatInput()
        self.text_input.setPlaceholderText("Ask HELIX anything…  (Enter to send · Shift+Enter for a new line)")
        self.text_input.submitted.connect(self._on_send)
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._on_send)

        talk_row = QHBoxLayout()
        talk_row.setSpacing(10)
        talk_row.addWidget(self.talk_button, 1)
        talk_row.addWidget(new_chat_button)

        # Hands-free wake word ("HELIX") is always on (§23) — a live "listening" meter, no toggle.
        self.listen_label = QLabel("\U0001f3a4  Listening for “HELIX”")
        self.listen_label.setObjectName("listenHint")
        self.listen_label.setVisible(False)
        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setTextVisible(False)
        self.level_bar.setMaximumHeight(8)
        self.level_bar.setVisible(False)
        listen_row = QHBoxLayout()
        listen_row.setSpacing(10)
        listen_row.addWidget(self.listen_label)
        listen_row.addWidget(self.level_bar, 1)

        # Subtle "in conversation" indicator + countdown, shown only while a session is active (§23).
        self.session_label = QLabel("")
        self.session_label.setObjectName("sessionPill")
        self.session_label.setStyleSheet("color:#3ddc84;font-weight:600;")
        self.session_label.setVisible(False)

        # Voice — which neural voice HELIX speaks with (edge-tts), with a Preview button to hear it.
        self.voice_picker = NoScrollComboBox()
        for voice_id, label in VOICE_CHOICES:
            self.voice_picker.addItem(label, voice_id)
        saved_voice = self.settings.get(XPERT_VOICE_SETTING, "") or DEFAULT_VOICE
        voice_idx = self.voice_picker.findData(saved_voice)
        if voice_idx < 0:  # a saved voice outside the curated list — keep it selectable
            self.voice_picker.addItem(saved_voice, saved_voice)
            voice_idx = self.voice_picker.findData(saved_voice)
        self.voice_picker.setCurrentIndex(max(0, voice_idx))
        self.voice_picker.currentIndexChanged.connect(self._on_voice_changed)
        self.voice_preview_button = QPushButton("Preview")
        self.voice_preview_button.setObjectName("ghostButton")
        self.voice_preview_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.voice_preview_button.clicked.connect(self._preview_voice)
        voice_row = QHBoxLayout()
        voice_row.addWidget(QLabel("Voice"))
        voice_row.addWidget(self.voice_picker, 1)
        voice_row.addWidget(self.voice_preview_button)

        # Voice output speed — how fast HELIX talks (0.8×–2.0×).
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setMinimum(80)
        self.speed_slider.setMaximum(200)
        self.speed_slider.setSingleStep(5)
        self.speed_slider.setPageStep(10)
        try:
            saved_speed = float(self.settings.get(XPERT_VOICE_SPEED_SETTING, 1.5))
        except (TypeError, ValueError):
            saved_speed = 1.5
        self.speed_slider.setValue(int(max(0.8, min(2.0, saved_speed)) * 100))
        self.speed_value = QLabel()
        self.speed_slider.valueChanged.connect(self._on_speed_changed)
        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Voice speed"))
        speed_row.addWidget(self.speed_slider, 1)
        speed_row.addWidget(self.speed_value)

        # Mic / speaker pickers (default to the system default, e.g. your Bluetooth headset).
        self.mic_picker = NoScrollComboBox()
        self.speaker_picker = NoScrollComboBox()
        self._load_device_pickers()
        self.mic_picker.currentIndexChanged.connect(self._on_input_device_changed)
        self.speaker_picker.currentIndexChanged.connect(self._on_output_device_changed)
        dev_row = QHBoxLayout()
        dev_row.addWidget(QLabel("Mic"))
        dev_row.addWidget(self.mic_picker, 1)
        dev_row.addWidget(QLabel("Speaker"))
        dev_row.addWidget(self.speaker_picker, 1)

        type_row = QHBoxLayout()
        type_row.setSpacing(10)
        type_row.addWidget(self.text_input, 1)
        type_row.addWidget(self.send_button)

        convo_layout.addWidget(self.transcript, 1)
        convo_layout.addWidget(self.convo_progress)
        convo_layout.addWidget(self.session_label)
        convo_layout.addLayout(listen_row)
        convo_layout.addWidget(self.convo_status)
        convo_layout.addLayout(talk_row)

        # Secondary controls (voice speed + audio devices), de-emphasized below the conversation.
        self.controls_box = QWidget()
        self.controls_box.setObjectName("xpertControls")
        controls_layout = QVBoxLayout(self.controls_box)
        controls_layout.setContentsMargins(14, 10, 14, 10)
        controls_layout.setSpacing(8)
        controls_layout.addLayout(voice_row)
        controls_layout.addLayout(speed_row)
        controls_layout.addLayout(dev_row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#6fb3c0;font-size:12pt;")

        layout.addWidget(subtitle)
        layout.addWidget(self.text_input)  # the ask/question box, above the conversation
        layout.addWidget(self.convo_box, 1)
        layout.addWidget(self.controls_box)
        layout.addWidget(self.status)

        self._update_speed_label()
        self.refresh()
        self._restore_chat()  # resume the persisted conversation from the last run (or greet if none)
        # Hands-free is always on — start the wake listener once the event loop is running.
        QTimer.singleShot(0, self._enable_handsfree)

    def refresh(self) -> None:
        spawn_worker(self._workers, self._gather, self._gather_done)

    def _gather(self) -> dict:
        usage = self.memory.ai_usage_summary()
        try:
            from helix.selfdev import builds as _builds
            built = len(_builds.list_builds())
        except Exception:
            built = 0
        return {"calls": usage["calls"], "month_cost": usage["month_cost"], "built": built}

    def _gather_done(self, ok: bool, payload) -> None:
        if not ok:
            self.status.setText("Could not load status — you can still talk to HELIX.")
            return
        self._state = payload  # feeds the conversation's live context (see _context)
        built = payload.get("built", 0)
        prefix = f"{built} app(s) built  ·  " if built else ""
        self.status.setText(prefix + "Describe an app and I'll build it.")

    def _context(self) -> str:
        """A short, live snapshot of the user's workshop for the system prompt: what they've built
        and what's waiting to ship."""
        state = self._state or {}
        lines: list[str] = []
        try:
            from helix.selfdev import builds as _builds
            made = _builds.list_builds()
        except Exception:
            made = []
        if made:
            names = ", ".join(b["name"] for b in made[:12])
            lines.append(f"Apps the user has built ({len(made)}): {names}.")
        else:
            lines.append("The user hasn't built any apps yet.")
        try:
            from helix.selfdev import engine as _engine
            pending = _engine.list_pending(self.settings)
        except Exception:
            pending = []
        if pending:
            lines.append(f"{len(pending)} HELIX code change(s) drafted and waiting to ship.")
        lines.append(
            f"Claude usage: {state.get('calls', 0)} calls, ~${state.get('month_cost', 0):.4f} this month."
        )
        return "\n".join(lines)

    @staticmethod
    def _plain(text: str) -> str:
        for ch in ("#", "*", "`"):
            text = text.replace(ch, "")
        return text.strip()

    def _setup_tts(self) -> None:
        self._speaking = False
        # Preferred: a natural neural voice (edge-tts) played through a Qt media player.
        try:
            self.player = QMediaPlayer(self)
            self.audio_out = QAudioOutput(self)
            self.player.setAudioOutput(self.audio_out)
            self.audio_out.setVolume(1.0)
            out_device = self._saved_output_device()
            if out_device is not None:
                self.audio_out.setDevice(out_device)  # route HELIX's voice to the chosen speaker/headset
            self.player.mediaStatusChanged.connect(self._on_media_status)
        except Exception:
            self.player = None
        # Fallback: the built-in OS voice (robotic, but offline).
        try:
            self.tts = QTextToSpeech(self)
            for locale in self.tts.availableLocales():
                if locale.name() == "en_GB":
                    self.tts.setLocale(locale)
                    break
            for voice in self.tts.availableVoices():
                try:
                    if voice.gender() == QVoice.Gender.Male:
                        self.tts.setVoice(voice)
                        break
                except Exception:
                    break
            self.tts.setRate(self._tts_rate())  # match the chosen voice speed
            self.tts.setPitch(-0.1)
            self.tts.setVolume(1.0)
            self.tts.stateChanged.connect(self._on_tts_state)
        except Exception:
            self.tts = None

    # ---- voice selection -------------------------------------------------- #

    def _voice_name(self) -> str:
        """The edge-tts voice HELIX speaks with — the picker's choice, else the saved/default."""
        picker = getattr(self, "voice_picker", None)
        if picker is not None and picker.currentData():
            return picker.currentData()
        return self.settings.get(XPERT_VOICE_SETTING, "") or DEFAULT_VOICE

    def _on_voice_changed(self, _index: int) -> None:
        self.settings.set(XPERT_VOICE_SETTING, self._voice_name())

    def _preview_voice(self) -> None:
        """Speak a short sample so the user can hear the selected voice before committing."""
        self._speak_text("Hello, sir. This is how I'll sound from now on.")

    # ---- voice output speed ----------------------------------------------- #

    def _voice_speed(self) -> float:
        """Current voice speed multiplier from the slider (falls back to the saved setting)."""
        if getattr(self, "speed_slider", None) is not None:
            return self.speed_slider.value() / 100.0
        try:
            return float(self.settings.get(XPERT_VOICE_SPEED_SETTING, 1.5))
        except (TypeError, ValueError):
            return 1.5

    def _voice_rate_str(self) -> str:
        """edge-tts rate string from the speed, e.g. 1.5× -> '+50%', 0.8× -> '-20%'."""
        pct = round((self._voice_speed() - 1.0) * 100)
        return f"+{pct}%" if pct >= 0 else f"{pct}%"

    def _tts_rate(self) -> float:
        """QTextToSpeech rate (-1..1) for the offline fallback voice, from the speed multiplier."""
        return max(-1.0, min(1.0, self._voice_speed() - 1.0))

    def _update_speed_label(self) -> None:
        if getattr(self, "speed_value", None) is not None:
            self.speed_value.setText(f"{self._voice_speed():.2f}×")

    def _on_speed_changed(self, _value: int) -> None:
        self._update_speed_label()
        self.settings.set(XPERT_VOICE_SPEED_SETTING, self._voice_speed())
        if getattr(self, "tts", None) is not None:
            try:
                self.tts.setRate(self._tts_rate())
            except Exception:
                pass

    def _setup_mic(self) -> None:
        self.mic = MicRecorder(self._saved_input_device(), self)

    def _saved_input_device(self):
        desc = self.settings.get(XPERT_INPUT_DEVICE_SETTING, "")
        return _find_audio_device(QMediaDevices.audioInputs(), desc) if desc else None

    def _saved_output_device(self):
        desc = self.settings.get(XPERT_OUTPUT_DEVICE_SETTING, "")
        return _find_audio_device(QMediaDevices.audioOutputs(), desc) if desc else None

    # ---- action wiring (the "Act" layer) ---------------------------------- #

    def _build_router(self) -> ActionRouter:
        ctx = ActionContext(
            memory=self.memory,
            settings=self.settings,
            research_fn=self._research_fn,
            on_progress=lambda text: self.convo_step.emit(text),
            show_screen=lambda name: self.request_show_screen.emit(name),
            on_build_created=lambda name: self.request_build_created.emit(name),
        )
        return ActionRouter(ctx)

    def _on_convo_step(self, tool_name: str) -> None:
        friendly = {
            "build_app": "Building your app…",
            "list_builds": "Checking your apps…",
            "improve_helix": "Drafting your change…",
            "remove_feature": "Drafting the removal…",
            "audit_dead_code": "Auditing the code…",
            "approve_change": "Shipping it…",
            "reject_change": "Discarding the draft…",
            "list_pending_changes": "Checking what's pending…",
            "fix_recent_crashes": "Checking for crashes…",
            "show_screen": "Pulling that up…",
        }.get(tool_name)
        if friendly is None:
            # Not a known tool name → it's already a ready-made progress line (e.g. a coder streaming step).
            friendly = tool_name or "Working on it..."
        self._set_convo_state("acting", friendly)
        # Narrate the coding-agent milestones aloud so the user can follow a code change by ear.
        spoken = self._coding_narration(tool_name)
        if spoken:
            self._speak_text(spoken)

    @staticmethod
    def _coding_narration(step: str) -> str:
        """An in-character, spoken line for a code-change milestone (or "" to stay quiet).

        Deliberately NOT a readout of the status label: the label shows the literal actions ("Reading X",
        "Editing Y"); the voice gives JARVIS-style commentary at the few real milestones only, and varies
        each time so it never sounds like a recording. `step` is the tool name on the first call, then the
        coder's live progress strings from `selfdev.coder.run_coding_task`.
        """
        msg = (step or "").lower()
        if step == "improve_helix":
            pool = [
                "Right then, sir — let me see what I can do with the place.",
                "On it. Rolling up my sleeves, metaphorically speaking.",
                "Improving myself — a rare and faintly vain privilege.",
                "Very good. Drafting it now; do resist the urge to supervise.",
            ]
        elif msg.startswith("creating work branch"):
            pool = [
                "First, a safe little sandbox — I'd rather not rewire anything you're using.",
                "Cordoning off a branch, so nothing goes live until you say the word.",
                "Setting up a workspace away from the live wiring. Measure twice, and all that.",
            ]
        elif "is working on the change" in msg:
            pool = [
                "Writing the code now — the part where I look busy and happen to be.",
                "Hands deep in the source, sir. Give me a moment to be brilliant.",
                "Reworking myself as we speak. Mildly existential, but I manage.",
                "Composing the changes. Feel free to look impressed when it's done.",
            ]
        elif msg.startswith("committing the proposed change"):
            pool = [
                "Tidying up and saving it to the branch for your verdict.",
                "Bundling the work up — sealed, and waiting on your approval.",
                "Done writing. Committing it; the rest is your call, sir.",
            ]
        elif "captured for review" in msg:
            pool = [
                "Hit a snag, sir — but I kept the work so you can have a look. Even I have off days.",
                "Not quite to plan. I saved what I had; we'll call it a learning experience.",
            ]
        else:
            return ""
        return random.choice(pool)

    def _research_fn(self, prompt: str) -> str:
        """A general Claude call (used by tools that need a quick model answer), recording usage."""
        model = DEFAULT_RESEARCH_MODEL
        client = ClaudeClient(ClaudeConfig(model=model, timeout_seconds=300))
        text = client.complete(prompt, max_tokens=4000)
        usage = client.last_usage or {}
        in_tok = int(usage.get("input_tokens", 0) or 0)
        out_tok = int(usage.get("output_tokens", 0) or 0)
        self.memory.record_ai_usage(model, in_tok, out_tok, estimate_cost(model, in_tok, out_tok))
        return text

    # ---- hands-free wake word ("HELIX") + audio devices ------------------- #

    def _load_device_pickers(self) -> None:
        self._loading_devices = True
        for picker, devices, key in (
            (self.mic_picker, QMediaDevices.audioInputs(), XPERT_INPUT_DEVICE_SETTING),
            (self.speaker_picker, QMediaDevices.audioOutputs(), XPERT_OUTPUT_DEVICE_SETTING),
        ):
            picker.clear()
            picker.addItem("System default", "")
            for device in devices:
                try:
                    picker.addItem(device.description(), device.description())
                except Exception:
                    continue
            index = picker.findData(self.settings.get(key, ""))
            picker.setCurrentIndex(index if index >= 0 else 0)
        self._loading_devices = False

    def _on_input_device_changed(self, _index: int) -> None:
        if self._loading_devices:
            return
        self.settings.set(XPERT_INPUT_DEVICE_SETTING, self.mic_picker.currentData() or "")
        self._setup_mic()  # rebuild the push-to-talk recorder on the new mic
        if self._handsfree:  # restart the wake listener on the new mic
            self._start_wake()
            if self._wake_listener is not None:
                self._wake_listener.set_active(self._convo_state == "idle")

    def _on_output_device_changed(self, _index: int) -> None:
        if self._loading_devices:
            return
        self.settings.set(XPERT_OUTPUT_DEVICE_SETTING, self.speaker_picker.currentData() or "")
        device = self._saved_output_device() or QMediaDevices.defaultAudioOutput()
        if getattr(self, "audio_out", None) is not None:
            try:
                self.audio_out.setDevice(device)
            except Exception:
                pass

    def voice_input_on(self) -> bool:
        """Whether the hands-free mic channel is enabled. Persisted, defaults on; the Console's
        play/stop toggle flips it."""
        return bool(self.settings.get(XPERT_VOICE_INPUT_SETTING, True))

    def set_voice_input(self, on: bool) -> None:
        """Turn the hands-free voice input channel on (listen) or off (mute), and remember the choice
        across sessions. Off tears the wake listener down so the mic goes fully quiet."""
        self.settings.set(XPERT_VOICE_INPUT_SETTING, bool(on))
        if on:
            self._enable_handsfree()
        else:
            self._disable_handsfree()

    def _disable_handsfree(self) -> None:
        """Mute the voice input channel: stop the wake listener and hide its live meter. The
        push-to-talk button and typing stay available."""
        self._end_session()
        self._stop_wake()
        self._handsfree = False
        self.level_bar.setVisible(False)
        self.listen_label.setVisible(False)
        self._set_convo_state("idle")

    def _enable_handsfree(self) -> None:
        """Hands-free is on by default (§23): continuously listen for the wake word “HELIX”. Started once
        at launch; degrades silently to push-to-talk + typing if the mic, the local voice model, or a
        Claude key isn't ready yet (it'll come up on a later launch once those are in place). The
        stt_ready() guard matters — loading the voice model after Qt is up would crash the process."""
        if not self.voice_input_on():  # the user muted the mic via the Console toggle — stay quiet
            return
        if self._handsfree:
            return
        if not (self.mic.is_available() and stt_available() and stt_ready() and self._claude_ready()):
            return
        if not self._start_wake():
            return
        self._handsfree = True
        self._end_session()
        self.level_bar.setVisible(True)
        self.listen_label.setVisible(True)
        self._set_convo_state("idle")

    def _start_wake(self) -> bool:
        self._stop_wake()
        self._wake_listener = WakeWordListener(self._saved_input_device(), self)
        if not self._wake_listener.is_available():
            self._wake_listener = None
            return False
        self._wake_listener.utterance.connect(self._on_wake_utterance)
        self._wake_listener.level.connect(self._on_wake_level)
        return self._wake_listener.start()

    def _stop_wake(self) -> None:
        if self._wake_listener is not None:
            try:
                self._wake_listener.stop()
            except Exception:
                pass
            self._wake_listener = None
        if hasattr(self, "level_bar"):
            self.level_bar.setValue(0)

    def _start_session(self) -> None:
        """Begin (or refresh) an active conversation session: HELIX keeps listening and answering
        without the wake word until dismissed or quiet for SESSION_IDLE_MS (§23)."""
        self._session_active = True
        self._session_timer.start(SESSION_IDLE_MS)
        if not self._session_tick.isActive():
            self._session_tick.start()
        self._update_session_indicator()

    def _end_session(self, spoken: bool = False) -> None:
        """Close the conversation session and return to wake-word-only listening. With spoken=True
        HELIX briefly acknowledges (used when the user says a dismissal phrase)."""
        was_active = self._session_active
        self._session_active = False
        self._session_timer.stop()
        self._session_tick.stop()
        if hasattr(self, "session_label"):
            self.session_label.setVisible(False)
        if spoken and was_active:
            self._append_transcript("HELIX", "Of course, sir.")
            self._speak_reply("Of course, sir.")

    def _update_session_indicator(self) -> None:
        if not self._session_active or not hasattr(self, "session_label"):
            return
        remaining = max(0, self._session_timer.remainingTime()) // 1000
        self.session_label.setText(
            f"\U0001f7e2  In conversation · say “goodbye” to end · {remaining // 60}:{remaining % 60:02d}"
        )
        self.session_label.setVisible(True)

    def _on_wake_level(self, level: float) -> None:
        self.level_bar.setValue(int(level * 100))

    def _on_wake_utterance(self, pcm: bytes) -> None:
        if self._convo_state != "idle":  # already mid-turn; ignore
            return
        if self._wake_listener is not None:
            self._wake_listener.set_active(False)  # quiet while we transcribe / think / speak
        handle, path = tempfile.mkstemp(suffix=".wav", prefix="helix_wake_")
        os.close(handle)
        try:
            _write_wav16(pcm, path)
        except Exception:
            self._set_convo_state("idle")  # re-arms the listener via the gating in _set_convo_state
            return
        self._set_convo_state("transcribing")
        spawn_worker(
            self._workers, lambda: transcribe(path), lambda ok, p: self._wake_transcribed(ok, p, path)
        )

    def _wake_transcribed(self, ok: bool, payload, path: str = "") -> None:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        text = str(payload or "").strip() if ok else ""
        matched, after = _split_wake(text)
        # During an active session, a dismissal phrase ("goodbye", "that's all", "thanks HELIX")
        # ends it right away with a brief acknowledgement.
        if self._session_active and _is_dismissal(text):
            self._end_session(spoken=True)
            return
        if matched:
            command = after.strip()
        elif self._session_active and text:
            command = text  # within an active session, the wake word isn't required
        else:
            self._set_convo_state("idle")  # not addressed to HELIX - keep listening
            return
        # Engaged: open (or refresh) the conversation session so HELIX keeps the floor.
        self._start_session()
        if not command:
            # bare "HELIX" - acknowledge, then take the next utterance as the command
            self._append_transcript("You", "HELIX")
            self._speak_reply("Yes, sir?")
            return
        self._append_transcript("You", command)
        self._handle_user_text(command)

    # ---- conversation UI helpers ------------------------------------------ #

    def _new_chat(self) -> None:
        self._history = []
        self._pending_action = None
        self._end_session()
        # "New chat" closes the persisted session (with a one-line summary) and starts a fresh one,
        # so the next restart resumes THIS new conversation, not the one just cleared.
        if self._convo_store is not None:
            try:
                self._convo_store.new_session()
            except Exception:
                pass
        self.transcript.clear()
        self._append_transcript(
            "HELIX", "Standing by, sir. Hold the Talk button and speak, or type below."
        )
        self._set_convo_state("idle")

    def _restore_chat(self) -> None:
        """On startup, rebuild the in-memory conversation buffer from the persisted history so HELIX
        picks up where the last run left off. Falls back to the standard greeting if there's nothing
        saved (or persistence is unavailable)."""
        self._pending_action = None
        self._end_session()
        self.transcript.clear()
        loaded: list = []
        if self._convo_store is not None:
            try:
                loaded = self._convo_store.load_recent_messages(50)
            except Exception:
                loaded = []
        # Drop a trailing user turn whose assistant reply never landed (a crash mid-turn), so the
        # buffer doesn't open the next turn with two consecutive user messages (the API rejects that).
        while loaded and loaded[-1].get("role") == "user":
            loaded.pop()
        if loaded:
            # The trimmer guarantees the window opens on a plain user turn (Messages-API requirement).
            self._history = self._trim_history(loaded)
            for message in self._history:
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    self._append_transcript(
                        "You" if message.get("role") == "user" else "HELIX", content
                    )
        else:
            self._history = []
        # A short, warm greeting on launch — nothing more. No auto-read of tasks, investments, or
        # briefings; HELIX simply says hello and waits for the first request.
        greeting = self._startup_greeting()
        self._append_transcript("HELIX", greeting)
        self._speak_text(greeting)
        self._set_convo_state("idle")

    @staticmethod
    def _startup_greeting() -> str:
        """Pick a randomized welcome so the launch greeting varies each run and never feels canned."""
        return random.choice([
            "Hello, sir. How is your day?",
            "Good to see you, sir. What can I do for you?",
            "Welcome back, sir. How can I help?",
            "Good day, sir. What are we working on?",
            "Hello again, sir. How's your day going?",
            "At your service, sir. What's on your mind?",
        ])

    def _persist_turn(self, role: str, content: str) -> None:
        """Append one conversation turn to the SQLite history immediately (best-effort — persistence
        never breaks the live conversation)."""
        if self._convo_store is None:
            return
        try:
            self._convo_store.append_turn(role, content)
        except Exception:
            pass

    def _append_transcript(self, who: str, text: str) -> None:
        color = "#ffc857" if who == "HELIX" else "#1dd8ff"
        safe = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.transcript.append(f'<span style="color:{color};font-weight:700;">{who}:</span> {safe}')
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def announce(self, text: str) -> None:
        """Proactively say something (a door alert, a low-stock nudge) — but ONLY when idle, so HELIX
        never talks over a turn. Used by the Console's awareness loop."""
        if not text or self._convo_state != "idle":
            return
        self._append_transcript("HELIX", text)
        self._speak_reply(text)

    def compact(self) -> None:
        """Slim down for the orb home: hide the hint, the buttons, and the typing line — the orb + voice
        lead, and voice speed / devices live in the Settings panel. Leaves just the conversation log."""
        for widget in (
            getattr(self, "_subtitle", None),
            getattr(self, "talk_button", None),
            getattr(self, "_new_chat_button", None),
            getattr(self, "send_button", None),
            getattr(self, "listen_label", None),
            getattr(self, "level_bar", None),
        ):
            if widget is not None:
                try:
                    widget.setVisible(False)
                except Exception:
                    pass

    def _set_convo_state(self, state: str, detail: str = "") -> None:
        self._convo_state = state
        self.convo_progress.setVisible(state in ("transcribing", "thinking", "acting", "speaking"))
        labels = {
            "idle": "Ready.",
            "listening": "● Listening... release to send.",
            "transcribing": "Transcribing...",
            "thinking": "Thinking...",
            "acting": detail or "Working on it...",
            "speaking": "Speaking, sir.",
        }
        if state == "listening":
            base = "● Listening... release to send."
        elif self._handsfree:
            base = "Hands-free on — say “HELIX”." if state == "idle" else labels.get(state, "")
        else:
            base = labels.get(state, "")
        self.convo_status.setText(detail if (detail and state == "acting") else base)
        mic_ok = self.mic.is_available()
        if self._handsfree:
            self.talk_button.setText("\U0001f3a4  Hands-free on — say “HELIX”")
        elif state == "listening":
            self.talk_button.setText("● Listening... (release)")
        elif not mic_ok:
            self.talk_button.setText("\U0001f3a4  Mic unavailable - type below")
            self.talk_button.setToolTip("No microphone detected. Type to HELIX instead.")
        else:
            self.talk_button.setText("\U0001f3a4  Hold to Talk")
        # While hands-free is on, the listener owns the mic, so the manual press-to-talk is disabled.
        self.talk_button.setEnabled(mic_ok and not self._handsfree and state in ("idle", "listening"))
        self.text_input.setEnabled(state == "idle")
        self.send_button.setEnabled(state == "idle")
        # Gate the wake listener: it only processes audio while idle (silent during a turn / reply).
        if self._wake_listener is not None:
            self._wake_listener.set_active(self._handsfree and state == "idle")

    @staticmethod
    def _claude_ready() -> bool:
        return ClaudeClient().is_configured()

    def _begin_turn(self) -> None:
        """Capture the live HELIX context for the system prompt before the worker runs."""
        self._convo_context = self._context()

    # ---- push-to-talk ----------------------------------------------------- #

    def _on_talk_pressed(self) -> None:
        if self._convo_state != "idle":
            return
        if not self.mic.is_available():
            self._append_transcript("HELIX", "No microphone is available, sir - type to me instead.")
            return
        if not stt_available():
            self._append_transcript(
                "HELIX",
                "Voice needs faster-whisper, sir: pip install faster-whisper. You can type meanwhile.",
            )
            return
        if not stt_ready():  # model didn't pre-load before Qt — loading it now would crash (§23)
            self._append_transcript(
                "HELIX", "The voice model didn't load at startup, sir — restart HELIX, then talk. Type meanwhile."
            )
            return
        if not self.mic.start():
            self._append_transcript("HELIX", "I couldn't open the microphone, sir.")
            return
        self._set_convo_state("listening")

    def _on_talk_released(self) -> None:
        if self._convo_state != "listening":
            return
        data = self.mic.stop()
        if len(data) < 9600:  # < ~0.3s of 16 kHz 16-bit mono = a slip, not speech
            self._set_convo_state("idle", "Didn't catch that - hold a touch longer.")
            return
        handle, path = tempfile.mkstemp(suffix=".wav", prefix="helix_stt_")
        os.close(handle)
        try:
            self.mic.save_wav(data, path)
        except Exception:
            self._set_convo_state("idle", "Couldn't save the audio.")
            return
        self._set_convo_state("transcribing")
        spawn_worker(
            self._workers,
            lambda: transcribe(path),
            lambda ok, payload: self._transcribed(ok, payload, path),
        )

    def _transcribed(self, ok: bool, payload, path: str = "") -> None:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        if not ok:
            self._append_transcript("HELIX", f"Speech-to-text didn't work: {payload}")
            self._set_convo_state("idle")
            return
        text = str(payload or "").strip()
        if not text:
            self._set_convo_state("idle", "Didn't catch that, sir.")
            return
        self._append_transcript("You", text)
        self._handle_user_text(text)

    def _on_send(self) -> None:
        if self._convo_state != "idle":
            return
        text = self.text_input.toPlainText().strip()
        if not text:
            return
        self.text_input.clear()
        self._append_transcript("You", text)
        self._handle_user_text(text)

    # ---- the turn (Think + Act + Speak) ----------------------------------- #

    def _handle_user_text(self, text: str) -> None:
        # Persist the user turn the moment it arrives, so a crash/restart loses at most this in-flight
        # turn (covers all branches below — they each record this same text into the in-memory buffer).
        self._persist_turn("user", text)
        # Deterministic spoken-confirmation gate for any pending money/outward action: it fires
        # ONLY on the user's own affirmative words, never on the model's say-so.
        if self._pending_action is not None:
            pending = self._pending_action
            if is_affirmative(text):
                self._pending_action = None
                self._history.append({"role": "user", "content": text})
                self._set_convo_state("acting", "Confirming...")
                spawn_worker(
                    self._workers,
                    lambda: self._router.execute_confirmed(*pending),
                    self._confirmed_done,
                )
                return
            if is_negative(text):
                self._pending_action = None
                reply = "Cancelled, sir."
                self._history.append({"role": "user", "content": text})
                self._history.append({"role": "assistant", "content": reply})
                self._persist_turn("assistant", reply)
                self._append_transcript("HELIX", reply)
                self._speak_reply(reply)
                return
            # Ambiguous: abandon the gated action (no implicit yes) and treat as a fresh request.
            self._pending_action = None

        if not self._claude_ready():
            self._append_transcript(
                "HELIX", "I need a Claude API key to begin, sir — add one in Settings (the ⚙ menu)."
            )
            self._set_convo_state("idle")
            return
        self._begin_turn()
        self._history.append({"role": "user", "content": text})
        self._history = self._trim_history(self._history)
        snapshot = list(self._history)
        self._set_convo_state("thinking")
        spawn_worker(self._workers, lambda: self._think(snapshot), self._thought)

    @staticmethod
    def _trim_history(messages: list, limit: int = 24) -> list:
        """Keep the conversation tail bounded for cost/latency, but never start the window on a
        dangling tool_result / assistant turn (the Messages API would reject that)."""
        msgs = messages[-limit:]
        while msgs and not (msgs[0].get("role") == "user" and isinstance(msgs[0].get("content"), str)):
            msgs.pop(0)
        return msgs

    def _think(self, messages: list):
        model = DEFAULT_RESEARCH_MODEL
        client = ClaudeClient(ClaudeConfig(model=model))
        system = build_jarvis_chat_system(self._convo_context)
        result = run_chat_turn(
            client, model, system, messages, self._router,
            on_step=lambda name: self.convo_step.emit(name),
        )
        for usage in result.usages:
            in_tok = int(usage.get("input_tokens", 0) or 0)
            out_tok = int(usage.get("output_tokens", 0) or 0)
            if in_tok or out_tok:
                self.memory.record_ai_usage(model, in_tok, out_tok, estimate_cost(model, in_tok, out_tok))
        return result

    def _thought(self, ok: bool, payload) -> None:
        if not ok:
            reply = "I couldn't reach Claude just now, sir."
            self._append_transcript("HELIX", f"{reply} ({payload})")
            self._speak_reply(reply)
            return
        result = payload
        self._history = result.messages
        self._pending_action = result.pending
        self._persist_turn("assistant", result.reply)
        self._append_transcript("HELIX", result.reply)
        self._speak_reply(result.reply)

    def _confirmed_done(self, ok: bool, payload) -> None:
        reply = str(payload) if ok else f"That didn't go through, sir: {payload}"
        self._history.append({"role": "assistant", "content": reply})
        self._persist_turn("assistant", reply)
        self._append_transcript("HELIX", reply)
        self._speak_reply(reply)

    # ---- speaking --------------------------------------------------------- #

    def _speak_reply(self, text: str) -> None:
        self._set_convo_state("speaking")

        def done() -> None:
            self._set_convo_state("idle")
            if self._handsfree:
                if self._session_active:
                    self._start_session()  # reset the inactivity countdown from the end of the reply
                if self._wake_listener is not None:
                    # Brief guard so HELIX's own voice tail / room echo can't re-trigger the wake word.
                    self._wake_listener.set_active(False)
                    QTimer.singleShot(450, self._resume_wake)

        self._speak_text(self._plain(text), on_done=done)

    def _resume_wake(self) -> None:
        if self._handsfree and self._wake_listener is not None and self._convo_state == "idle":
            self._wake_listener.set_active(True)

    def _speak_text(self, text: str, on_done=None) -> None:
        text = (text or "").strip()
        self.stop_speaking()
        self._speak_done_cb = on_done
        if not text:
            self._finish_speaking()
            return
        self._pending_speech = text
        if self.player is not None and not self._speaking:
            self._speaking = True
            rate = self._voice_rate_str()  # honor the voice-speed slider
            voice = self._voice_name()     # honor the chosen voice
            spawn_worker(
                self._workers, lambda: synthesize_speech(text, voice=voice, rate=rate), self._speak_ready
            )
        else:
            self._fallback_say(text)
            self._guard_speaking(text)

    def _speak_ready(self, ok: bool, payload) -> None:
        self._speaking = False
        text = getattr(self, "_pending_speech", "")
        if ok and payload and self.player is not None:
            self.player.setSource(QUrl.fromLocalFile(payload))
            self.player.play()
            self._guard_speaking(text)
        else:
            self._fallback_say(text)
            self._guard_speaking(text)

    def _guard_speaking(self, text: str) -> None:
        """Safety net: re-enable the UI even if the end-of-speech signal never arrives."""
        ms = max(2500, min(60000, len(text or "") * 70))
        QTimer.singleShot(ms, self._finish_speaking)

    def _on_media_status(self, status) -> None:
        try:
            ended = status == QMediaPlayer.MediaStatus.EndOfMedia
        except Exception:
            ended = False
        if ended:
            self._finish_speaking()

    def _on_tts_state(self, state) -> None:
        try:
            ready = state == QTextToSpeech.State.Ready
        except Exception:
            ready = False
        if ready and self._speak_done_cb is not None:
            self._finish_speaking()

    def _finish_speaking(self) -> None:
        cb = self._speak_done_cb
        self._speak_done_cb = None
        if cb is not None:
            cb()

    def _fallback_say(self, text: str) -> None:
        if getattr(self, "tts", None) is not None and text and not text.startswith("Click 'Generate"):
            self.tts.stop()
            self.tts.say(text)

    def stop_speaking(self) -> None:
        if getattr(self, "player", None) is not None:
            self.player.stop()
        if getattr(self, "tts", None) is not None:
            self.tts.stop()










def apply_hud_style(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 12))
    app.setStyleSheet(
        """
        QMainWindow,
        QWidget {
            background-color: #061013;
            color: #e7fbff;
            font-size: 13pt;
        }

        QTabWidget::pane {
            background-color: #071417;
            border: 1px solid #1bbfe8;
            border-radius: 8px;
            top: -1px;
        }

        QTabBar::tab {
            background-color: #0b2026;
            color: #8eeaff;
            border: 1px solid #255b68;
            border-bottom-color: #1bbfe8;
            border-top-left-radius: 7px;
            border-top-right-radius: 7px;
            padding: 10px 18px;
            margin-right: 6px;
            font-weight: 700;
            min-width: 96px;
        }

        QTabBar::tab:selected {
            background-color: #102f38;
            color: #fff2c2;
            border-color: #ffbd3e;
        }

        QTabBar::tab:hover {
            background-color: #123a45;
            color: #ffffff;
        }

        QLabel {
            color: #dff9ff;
            font-size: 13pt;
        }

        QCheckBox {
            color: #eaffff;
            spacing: 8px;
            background: transparent;
        }

        /* Prominent, high-contrast toggles — the Hands-free wake word (Xpert), the Home auto-text
           reminder, and the Investment AI-research/cost toggle. Scoped by objectName so table
           task-checkboxes stay default. */
        QCheckBox#handsfreeToggle,
        QCheckBox#autoTextToggle,
        QCheckBox#aiResearchToggle {
            color: #ffc857;
            font-size: 14pt;
            font-weight: 800;
            padding: 4px 0;
        }

        QCheckBox#handsfreeToggle::indicator,
        QCheckBox#autoTextToggle::indicator,
        QCheckBox#aiResearchToggle::indicator {
            width: 22px;
            height: 22px;
            border: 2px solid #1dd8ff;
            border-radius: 5px;
            background-color: #081316;
        }

        QCheckBox#handsfreeToggle::indicator:hover,
        QCheckBox#autoTextToggle::indicator:hover,
        QCheckBox#aiResearchToggle::indicator:hover {
            border-color: #ffbd3e;
            background-color: #0b1d22;
        }

        QCheckBox#handsfreeToggle::indicator:checked,
        QCheckBox#autoTextToggle::indicator:checked,
        QCheckBox#aiResearchToggle::indicator:checked {
            background-color: #ffbd3e;
            border-color: #ffbd3e;
        }

        QCheckBox#handsfreeToggle::indicator:checked:hover,
        QCheckBox#autoTextToggle::indicator:checked:hover,
        QCheckBox#aiResearchToggle::indicator:checked:hover {
            background-color: #ffd06a;
        }

        QLabel#sectionHeader {
            color: #ffc857;
            font-size: 24pt;
            font-weight: 800;
            padding: 2px 0 10px 0;
            border-bottom: 2px solid #1dd8ff;
        }

        QGroupBox {
            background-color: #09181c;
            border: 1px solid #286979;
            border-radius: 8px;
            color: #ffc857;
            font-size: 14pt;
            font-weight: 800;
            margin-top: 14px;
            padding: 14px;
        }

        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 14px;
            padding: 0 8px;
            background-color: #061013;
        }

        QLineEdit,
        QTextEdit,
        QDoubleSpinBox,
        QSpinBox,
        QComboBox {
            background-color: #081316;
            color: #f3fdff;
            border: 1px solid #2c6574;
            border-radius: 6px;
            padding: 8px 10px;
            selection-background-color: #ffbd3e;
            selection-color: #081316;
            min-height: 34px;
        }

        QTextEdit {
            font-family: Consolas, "Segoe UI";
            font-size: 14pt;
        }

        QTextEdit#briefingPanel {
            background-color: #061013;
            border: 2px solid #1dd8ff;
            color: #eaffff;
            font-size: 15pt;
            padding: 12px;
        }

        QLineEdit:focus,
        QTextEdit:focus,
        QDoubleSpinBox:focus,
        QSpinBox:focus,
        QComboBox:focus {
            border: 2px solid #ffbd3e;
            background-color: #0b1d22;
        }

        QComboBox::drop-down {
            border-left: 1px solid #2c6574;
            width: 30px;
        }

        QPushButton {
            background-color: #11333c;
            color: #f1fcff;
            border: 1px solid #1dd8ff;
            border-radius: 6px;
            padding: 10px 20px;
            font-size: 13pt;
            font-weight: 800;
            min-height: 38px;
        }

        QPushButton:hover {
            background-color: #175161;
            border-color: #ffbd3e;
            color: #fff6d6;
        }

        QPushButton:pressed {
            background-color: #ffbd3e;
            color: #061013;
        }

        /* Compact buttons embedded in table rows (e.g. Assets → Details) — no tall min-height. */
        QPushButton#rowButton {
            min-height: 0;
            padding: 3px 14px;
            font-size: 11pt;
            font-weight: 700;
        }

        /* Grocery screen (GroceryTab). */
        QLabel#grocerySummary {
            color: #7faebb;
            font-size: 12pt;
        }
        QLabel#groceryEmpty {
            color: #6fb3c0;
            font-size: 13pt;
            padding: 18px 4px;
        }
        QFrame#groceryCard {
            border: 1px solid #1b3a44;
            border-radius: 12px;
            background-color: rgba(13, 32, 40, 0.55);
        }
        QLabel#groceryCategory {
            color: #ffc857;
            font-size: 13pt;
            font-weight: 800;
            letter-spacing: 1px;
        }
        QFrame#groceryItem {
            border: none;
            border-radius: 8px;
            background-color: #0b1d22;
        }
        QFrame#groceryItem:hover {
            background-color: #11333c;
        }
        QLabel#groceryItemName {
            color: #eaffff;
            font-size: 13pt;
            font-weight: 700;
            border: none;
        }
        QLabel#groceryQty {
            color: #1dd8ff;
            font-size: 12pt;
            font-weight: 800;
            border: none;
        }
        QPushButton#groceryRemove {
            min-height: 0;
            padding: 0;
            font-size: 12pt;
            font-weight: 800;
            color: #ff9e9e;
            background-color: transparent;
            border: 1px solid #4a2b2b;
            border-radius: 14px;
        }
        QPushButton#groceryRemove:hover {
            background-color: #ff6b6b;
            color: #061013;
            border-color: #ff6b6b;
        }
        QPushButton#orderButton {
            background-color: #ffc857;
            color: #061013;
            border: 1px solid #ffd06a;
            font-size: 14pt;
            font-weight: 800;
            padding: 12px 26px;
        }
        QPushButton#orderButton:hover {
            background-color: #ffd784;
            border-color: #fff6d6;
        }
        QPushButton#orderButton:disabled {
            background-color: #2a2f33;
            color: #6b7378;
            border-color: #2a2f33;
        }

        /* Components screen — structured parts browser (ComponentsTab). */
        QLabel#componentStep {
            color: #8eeaff;
            font-size: 11pt;
            font-weight: 800;
            letter-spacing: 1px;
            padding-top: 4px;
        }
        QFrame#categoryTile {
            border: 1px solid #1b3a44;
            border-radius: 10px;
            background-color: #0b1d22;
        }
        QFrame#categoryTile:hover {
            background-color: #11333c;
            border-color: #1dd8ff;
        }
        QFrame#categoryTile[selected="true"] {
            background-color: #102f38;
            border: 2px solid #ffbd3e;
        }
        QLabel#categoryIcon { font-size: 16pt; border: none; }
        QLabel#categoryName {
            color: #eaffff;
            font-size: 11pt;
            font-weight: 700;
            border: none;
        }
        QFrame#filterChip {
            border: 1px solid #2c6574;
            border-radius: 12px;
            background-color: #0b1d22;
        }
        QLabel#filterChipText {
            color: #8eeaff;
            font-size: 11pt;
            font-weight: 700;
            border: none;
        }
        QPushButton#chipRemove {
            min-height: 0;
            padding: 0;
            font-size: 10pt;
            font-weight: 800;
            color: #8eeaff;
            background-color: transparent;
            border: none;
        }
        QPushButton#chipRemove:hover { color: #ff6b6b; }
        QFrame#resultCard {
            border: 1px solid #1b3a44;
            border-radius: 10px;
            background-color: rgba(13, 32, 40, 0.55);
        }
        QFrame#resultCard:hover {
            background-color: #11333c;
            border-color: #1dd8ff;
        }
        QFrame#resultCard[selected="true"] {
            border: 2px solid #ffbd3e;
            background-color: #102f38;
        }
        QLabel#resultPart {
            color: #eaffff;
            font-size: 13pt;
            font-weight: 800;
            border: none;
        }
        QLabel#resultDesc { color: #aee3f0; font-size: 11pt; border: none; }
        QLabel#resultPrice {
            color: #ffc857;
            font-size: 12pt;
            font-weight: 800;
            border: none;
        }
        QLabel#vendorBadge {
            color: #061013;
            background-color: #1dd8ff;
            border-radius: 7px;
            padding: 2px 8px;
            font-size: 10pt;
            font-weight: 800;
        }
        QFrame#detailPanel {
            border: 1px solid #1dd8ff;
            border-radius: 12px;
            background-color: #08171b;
        }
        QLabel#detailName {
            color: #ffc857;
            font-size: 18pt;
            font-weight: 800;
        }
        QLabel#detailDesc { color: #dff9ff; font-size: 12pt; }
        QLabel#detailPrice {
            color: #ffc857;
            font-size: 20pt;
            font-weight: 800;
        }
        QLabel#detailStock { font-size: 12pt; font-weight: 800; }
        QLabel#detailSpecKey { color: #7faebb; font-size: 11pt; font-weight: 700; }
        QLabel#detailSpecValue { color: #eaffff; font-size: 11pt; font-weight: 700; }

        QTableWidget {
            background-color: #071417;
            alternate-background-color: #0c2026;
            border: 1px solid #286979;
            border-radius: 6px;
            color: #e7fbff;
            gridline-color: #214d58;
            font-size: 13pt;
            selection-background-color: #164653;
            selection-color: #ffffff;
        }

        QHeaderView::section {
            background-color: #102f38;
            color: #ffc857;
            border: 0;
            border-right: 1px solid #286979;
            border-bottom: 1px solid #286979;
            padding: 10px;
            font-size: 13pt;
            font-weight: 800;
        }

        QTableCornerButton::section {
            background-color: #102f38;
            border: 0;
        }

        QStatusBar {
            background-color: #061013;
            color: #8eeaff;
            border-top: 1px solid #286979;
            font-size: 12pt;
        }

        QScrollBar:vertical {
            background-color: #071417;
            width: 16px;
            margin: 0;
        }

        QScrollBar::handle:vertical {
            background-color: #1e7f94;
            border-radius: 6px;
            min-height: 36px;
        }

        QScrollBar::handle:vertical:hover {
            background-color: #1dd8ff;
        }

        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {
            height: 0;
        }

        QMessageBox {
            background-color: #061013;
        }

        /* --- Console (the JARVIS landing surface) + Xpert conversation --- */
        QLabel#consoleBrand {
            color: #eaffff;
            font-size: 30pt;
            font-weight: 900;
            letter-spacing: 6px;
        }

        QLabel#consolePresence {
            color: #6fb3c0;
            font-size: 12pt;
            font-weight: 600;
            letter-spacing: 1px;
        }

        QFrame#consoleDivider {
            border: none;
            background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(29,216,255,0), stop:0.5 rgba(29,216,255,0.55),
                stop:1 rgba(29,216,255,0));
        }

        QPushButton#ghostButton {
            background-color: rgba(17,51,60,0.4);
            color: #9fe9ff;
            border: 1px solid #1e5a68;
            border-radius: 16px;
            padding: 6px 18px;
            font-size: 12pt;
            font-weight: 700;
            min-height: 30px;
        }

        QPushButton#ghostButton:hover {
            border-color: #1dd8ff;
            color: #ffffff;
            background-color: rgba(23,81,97,0.5);
        }

        QPushButton#primaryButton {
            background-color: #0e8aa8;
            color: #eaffff;
            border: 1px solid #1dd8ff;
            border-radius: 16px;
            padding: 6px 20px;
            font-size: 12pt;
            font-weight: 700;
            min-height: 30px;
        }

        QPushButton#primaryButton:hover {
            background-color: #15a6c8;
            color: #ffffff;
        }

        QFrame#keyGate {
            background-color: rgba(255,200,87,0.10);
            border: 1px solid #ffc857;
            border-radius: 14px;
        }

        QDialog#designDialog {
            background-color: #061013;
        }

        QTextBrowser#designTranscript {
            background-color: rgba(10,28,33,0.6);
            color: #d6f6ff;
            border: 1px solid #1e5a68;
            border-radius: 12px;
            padding: 10px;
            font-size: 12pt;
        }

        QLabel#panelTitle {
            color: #eaffff;
            font-size: 16pt;
            font-weight: 800;
            letter-spacing: 2px;
        }

        QLabel#launcherTitle {
            color: #eaffff;
            font-size: 22pt;
            font-weight: 800;
            letter-spacing: 3px;
        }

        QPushButton#launcherCard {
            background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 rgba(20,44,53,0.55), stop:1 rgba(11,26,32,0.55));
            color: #eaffff;
            border: 1px solid #1b3a44;
            border-radius: 16px;
            padding: 16px 22px;
            font-size: 15pt;
            font-weight: 800;
            text-align: left;
        }

        QPushButton#launcherCard:hover {
            border-color: #1dd8ff;
            background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 rgba(26,58,70,0.7), stop:1 rgba(13,32,40,0.7));
        }

        QPushButton#launcherHide, QPushButton#launcherRemove {
            background-color: rgba(8,20,25,0.55);
            color: #6f93a0;
            border: none;
            border-radius: 11px;
            font-size: 10pt;
            font-weight: 800;
            padding: 0px;
        }

        /* Hide (core pillars, reversible): a neutral hover — no danger implied. */
        QPushButton#launcherHide:hover {
            background-color: rgba(40,70,82,0.9);
            color: #d6eef5;
        }

        /* Remove (self-added features): a destructive red hover — the ✕ deletes the feature's code. */
        QPushButton#launcherRemove:hover {
            background-color: rgba(230,60,60,0.95);
            color: #ffffff;
        }

        /* Archive (§selfdev versions & restore). */
        QLabel#archiveIntro {
            color: #8fc7d4;
            font-size: 11pt;
            padding: 0px 2px 6px 2px;
        }
        QFrame#archiveCard {
            background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 rgba(20,44,53,0.5), stop:1 rgba(11,26,32,0.5));
            border: 1px solid #1b3a44;
            border-radius: 12px;
            padding: 12px 14px;
        }
        QFrame#archiveCard:hover { border-color: #1dd8ff; }
        QFrame#archiveCardRoot {
            background-color: rgba(20,40,30,0.5);
            border: 1px solid #2c6e4a;
            border-radius: 12px;
            padding: 12px 14px;
        }
        QLabel#archiveLabel { color: #eaffff; font-size: 13pt; font-weight: 800; }
        QLabel#archiveMeta { color: #6f93a0; font-size: 9pt; }
        QLabel#archivePrompt { color: #b9d9e2; font-size: 10pt; }
        QLabel#archiveTagDefault {
            color: #07141a; background-color: #1dd8ff; border-radius: 8px;
            padding: 1px 8px; font-size: 9pt; font-weight: 800;
        }
        QLabel#archiveTagRoot {
            color: #ffffff; background-color: #2c8e5a; border-radius: 8px;
            padding: 1px 8px; font-size: 9pt; font-weight: 800;
        }
        QPushButton#dangerButton {
            background-color: rgba(120,30,30,0.55); color: #ffd9d9;
            border: 1px solid #6e2a2a; border-radius: 12px; padding: 10px 16px; font-weight: 800;
        }
        QPushButton#dangerButton:hover { background-color: rgba(200,55,55,0.9); color: #ffffff; }

        /* Guardrails — the Twelve Commandments (§44, read-only in Settings). */
        QGroupBox#guardrailsBox {
            border: 1px solid #2c6e4a; border-radius: 12px; margin-top: 10px;
            font-weight: 800; color: #cfeee0; padding: 12px 10px 10px 10px;
        }
        QGroupBox#guardrailsBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }
        QLabel#guardrailsStatus { color: #6ee0a6; font-weight: 800; }
        QLabel#guardrailsStatusBad { color: #ff6a6a; font-weight: 800; }
        QLabel#guardrailLine { color: #cfe6ec; font-size: 10pt; }

        QLabel#xpertHint {
            color: #8fc7d4;
            font-size: 12pt;
            padding: 2px 2px 4px 2px;
        }

        QLabel#listenHint {
            color: #1dd8ff;
            font-size: 11pt;
            font-weight: 700;
        }

        QLabel#convoStatus {
            color: #9fc8d2;
            font-size: 11pt;
        }

        QWidget#xpertControls {
            background-color: rgba(11,26,32,0.5);
            border: 1px solid #16323b;
            border-radius: 10px;
        }
        """
    )






