"""PresenceOrb — the living Presence that *is* HELIX.

Pure QPainter, single runtime, GPU-smooth. A breathing core of cyan: it brightens and ripples while
listening, spins a slow amber arc while thinking, and warms to gold while speaking — so it's obvious
HELIX has the floor (the mic is muted then). State changes ease in, so transitions feel alive.
"""
from __future__ import annotations

import math
from enum import Enum

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import QSizePolicy, QWidget

from helix.ui.theme import AMBER, CYAN, CYAN_DIM

GOLD = "#ffc857"      # the warm "I'm speaking" tone (matches old HELIX's speaking colour)
GOLD_DIM = "#a87f2c"  # the dim edge of the gold core


class OrbState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


# Per-state numeric targets the orb eases toward. `warm` blends the whole orb cyan→gold (speaking);
# `accent` drives the amber thinking arc.
_PARAMS: dict[OrbState, dict[str, float]] = {
    OrbState.IDLE: {"glow": 0.45, "amp": 0.045, "speed": 0.055, "accent": 0.0, "warm": 0.0},
    OrbState.LISTENING: {"glow": 0.85, "amp": 0.075, "speed": 0.10, "accent": 0.15, "warm": 0.0},
    OrbState.THINKING: {"glow": 0.70, "amp": 0.050, "speed": 0.14, "accent": 1.0, "warm": 0.0},
    OrbState.SPEAKING: {"glow": 1.0, "amp": 0.11, "speed": 0.18, "accent": 0.0, "warm": 1.0},
}

_KEYS = ("glow", "amp", "speed", "accent", "warm")


def _mix(c1: str, c2: str, t: float) -> QColor:
    a, b = QColor(c1), QColor(c2)
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )


def _col(c1: str, c2: str, t: float, alpha: int) -> QColor:
    """Blend c1→c2 by t, at the given alpha. (t is the eased cyan→gold 'warm' amount.)"""
    c = _mix(c1, c2, t)
    c.setAlpha(max(0, min(255, alpha)))
    return c


class PresenceOrb(QWidget):
    """The Presence. As a full-window background it's clickable (tap to talk) and pulses to the live
    mic level while listening, so it reads as a single living thing you converse with."""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(240, 240)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setStyleSheet("background: transparent;")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._state = OrbState.IDLE
        self._p = dict(_PARAMS[OrbState.IDLE])
        self._phase = 0.0
        self._spin = 0.0
        self._t = 0.0
        self._level = 0.0          # eased live mic level
        self._level_target = 0.0   # latest mic level, decays so the pulse falls when you stop talking
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)  # ~30 fps

    def set_state(self, state: OrbState) -> None:
        self._state = state

    def set_level(self, level: float) -> None:
        """Feed the live mic level (0..1) while listening — the orb swells in time with your voice."""
        self._level_target = max(0.0, min(1.0, float(level)))

    def mousePressEvent(self, _event) -> None:
        self.clicked.emit()

    def _tick(self) -> None:
        target = _PARAMS[self._state]
        for k in _KEYS:
            self._p[k] += (target[k] - self._p[k]) * 0.08
        self._level += (self._level_target - self._level) * 0.35
        self._level_target *= 0.82  # decay so the swell relaxes when the room goes quiet
        self._phase += self._p["speed"]
        self._spin = (self._spin + 3.0) % 360.0
        self._t += 1.0
        self.update()

    def paintEvent(self, _event) -> None:
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        center = QPointF(cx, cy)
        base = min(w, h) * 0.22
        glow = min(1.0, self._p["glow"] + self._level * 0.5)  # the mic level brightens the orb
        warm = self._p["warm"]
        amp = self._p["amp"] + self._level * 0.05
        r = base * (1 + amp * math.sin(self._phase))

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Outer glow
        g = QRadialGradient(center, r * 2.8)
        g.setColorAt(0.0, _col(CYAN, GOLD, warm, int(70 * glow)))
        g.setColorAt(0.5, _col(CYAN, GOLD, warm, int(22 * glow)))
        g.setColorAt(1.0, _col(CYAN, GOLD, warm, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(center, r * 2.8, r * 2.8)

        # Expanding ripples while listening / speaking
        if self._state in (OrbState.LISTENING, OrbState.SPEAKING):
            p.setBrush(Qt.BrushStyle.NoBrush)
            for k in range(3):
                frac = ((self._t * 0.012) + k / 3.0) % 1.0
                rr = r * (1.1 + frac * 1.9)
                pen = QPen(_col(CYAN, GOLD, warm, int(120 * glow * (1 - frac))))
                pen.setWidthF(1.6)
                p.setPen(pen)
                p.drawEllipse(center, rr, rr)

        # Inner halo
        halo = QRadialGradient(center, r * 1.5)
        halo.setColorAt(0.0, _col(CYAN, GOLD, warm, int(60 * glow)))
        halo.setColorAt(1.0, _col(CYAN, GOLD, warm, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(center, r * 1.5, r * 1.5)

        # Core — white-hot centre fading to cyan (or gold while speaking)
        core = QRadialGradient(center, r)
        core.setColorAt(0.0, _col("#eaffff", "#fff7e6", warm, 255))
        core.setColorAt(0.35, _col(CYAN, GOLD, warm, 235))
        core.setColorAt(0.80, _col(CYAN_DIM, GOLD_DIM, warm, 205))
        core.setColorAt(1.0, _col(CYAN_DIM, GOLD_DIM, warm, 0))
        p.setBrush(core)
        p.drawEllipse(center, r, r)

        # Rotating amber arc while thinking
        if self._state is OrbState.THINKING:
            rect = QRectF(cx - r * 1.28, cy - r * 1.28, r * 2.56, r * 2.56)
            amber = QColor(AMBER)
            amber.setAlpha(max(0, min(255, int(220 * self._p["accent"]))))
            pen = QPen(amber)
            pen.setWidthF(2.4)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(rect, int(self._spin * 16), 100 * 16)

        p.end()
