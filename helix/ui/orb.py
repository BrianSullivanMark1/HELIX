"""PresenceOrb — the living Presence that *is* HELIX.

Pure QPainter, single runtime, GPU-smooth. A breathing core of cyan: it brightens and ripples while
listening, spins a slow amber arc while thinking, and warms to gold while speaking — so it's obvious
HELIX has the floor (the mic is muted then). State changes ease in, so transitions feel alive.
"""
from __future__ import annotations

import math
from enum import Enum

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QRadialGradient
from PyQt6.QtWidgets import QSizePolicy, QWidget

from helix.ui.theme import AMBER, CYAN, CYAN_DIM

GOLD = "#ffc857"      # the warm "I'm speaking" tone (matches old HELIX's speaking colour)
GOLD_DIM = "#a87f2c"  # the dim edge of the gold core

# Chrome body — a brushed-silver sphere lit from the upper-left.
CHROME_HI = "#f4f8ff"    # specular highlight
CHROME_MID = "#9fb0bd"   # mid metal
CHROME_EDGE = "#1a2128"  # shadow edge


class OrbState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


# Per-state numeric targets the orb eases toward. `warm` blends the whole orb cyan→gold (speaking);
# `accent` drives the amber thinking arc.
_PARAMS: dict[OrbState, dict[str, float]] = {
    OrbState.IDLE: {"glow": 0.40, "amp": 0.045, "speed": 0.055, "accent": 0.0, "warm": 0.0},
    OrbState.LISTENING: {"glow": 0.72, "amp": 0.075, "speed": 0.10, "accent": 0.15, "warm": 0.0},
    OrbState.THINKING: {"glow": 0.58, "amp": 0.050, "speed": 0.14, "accent": 1.0, "warm": 0.0},
    OrbState.SPEAKING: {"glow": 0.86, "amp": 0.11, "speed": 0.18, "accent": 0.0, "warm": 1.0},
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
        cx, cy = w / 2, h * 0.43  # a touch above centre, so the conversation floats over the lower half
        center = QPointF(cx, cy)
        base = min(w, h) * 0.30  # ~50% larger than the old energy orb
        warm = self._p["warm"]   # 0 = cyan (idle/listening), 1 = gold (speaking)
        glow = min(1.0, self._p["glow"] + self._level * 0.4)  # mic level swells the aura while listening
        amp = self._p["amp"] + self._level * 0.04
        r = base * (1 + amp * math.sin(self._phase))

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)

        # State aura — a soft halo around the sphere, cyan warming to gold while speaking.
        aura = QRadialGradient(center, r * 1.9)
        aura.setColorAt(0.0, _col(CYAN, GOLD, warm, int(46 * glow)))
        aura.setColorAt(0.52, _col(CYAN, GOLD, warm, int(40 * glow)))  # ~the sphere's edge
        aura.setColorAt(1.0, _col(CYAN, GOLD, warm, 0))
        p.setBrush(aura)
        p.drawEllipse(center, r * 1.9, r * 1.9)

        # Chrome body — a shaded sphere, highlight toward the upper-left.
        hx, hy = cx - r * 0.34, cy - r * 0.38
        body = QRadialGradient(QPointF(hx, hy), r * 1.5)
        body.setColorAt(0.0, QColor(CHROME_HI))
        body.setColorAt(0.42, QColor(CHROME_MID))
        body.setColorAt(1.0, QColor(CHROME_EDGE))
        p.setBrush(body)
        p.drawEllipse(center, r, r)

        # Reflected rim light on the shadow side, tinted by state — clipped to the sphere.
        clip = QPainterPath()
        clip.addEllipse(center, r, r)
        p.save()
        p.setClipPath(clip)
        rim = QRadialGradient(QPointF(cx + r * 0.5, cy + r * 0.55), r)
        rim.setColorAt(0.0, _col(CYAN, GOLD, warm, int(150 * glow)))
        rim.setColorAt(0.6, _col(CYAN, GOLD, warm, 0))
        p.setBrush(rim)
        p.drawRect(QRectF(cx - r, cy - r, 2 * r, 2 * r))
        p.restore()

        # Specular highlight.
        spec = QRadialGradient(QPointF(hx, hy), r * 0.34)
        spec.setColorAt(0.0, QColor(255, 255, 255, 190))
        spec.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(spec)
        p.drawEllipse(QPointF(hx, hy), r * 0.34, r * 0.34)

        # Thinking — the amber arc still sweeps, now around the sphere.
        if self._state is OrbState.THINKING:
            rect = QRectF(cx - r * 1.34, cy - r * 1.34, r * 2.68, r * 2.68)
            amber = QColor(AMBER)
            amber.setAlpha(max(0, min(255, int(220 * self._p["accent"]))))
            pen = QPen(amber)
            pen.setWidthF(2.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(rect, int(self._spin * 16), 100 * 16)

        p.end()
