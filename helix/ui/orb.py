"""PresenceOrb — the living Presence that *is* HELIX.

An electronic 3-D core: a dark sphere with a dense circuit-city etched under a glassy surface, a Fresnel
rim, and radiant energy-smoke streaming off it. Impulses fire down the bus traces. Everything shifts
cyan (idle/listening) → gold (speaking); a sweeping amber arc marks thinking. State eases in, the core
breathes, and the live mic level swells it. As a full-window background it's clickable (tap to talk).

The drawing lives in module-level `paint_orb()` so the app icon (scripts/make_icon.py) renders the exact
same orb.
"""
from __future__ import annotations

import math
import random
from enum import Enum

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QRadialGradient
from PyQt6.QtWidgets import QSizePolicy, QWidget

from helix.ui.theme import AMBER, CYAN, GOLD

# Dark electronic body, so the circuitry glows against it.
BODY_HI = "#2b3b4b"
BODY_MID = "#14202b"
BODY_EDGE = "#05090d"


class OrbState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


# `warm` blends cyan→gold (speaking); `accent` drives the amber thinking arc; `glow` is overall energy.
_PARAMS: dict[OrbState, dict[str, float]] = {
    OrbState.IDLE: {"glow": 0.42, "amp": 0.045, "speed": 0.055, "accent": 0.0, "warm": 0.0},
    OrbState.LISTENING: {"glow": 0.78, "amp": 0.075, "speed": 0.10, "accent": 0.15, "warm": 0.0},
    OrbState.THINKING: {"glow": 0.66, "amp": 0.050, "speed": 0.14, "accent": 1.0, "warm": 0.0},
    OrbState.SPEAKING: {"glow": 0.95, "amp": 0.10, "speed": 0.16, "accent": 0.0, "warm": 1.0},
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
    c = _mix(c1, c2, t)
    c.setAlpha(max(0, min(255, alpha)))
    return c


def _along(pts: list[tuple[float, float]], prog: float) -> tuple[float, float]:
    """Point at fractional arc-length `prog` along a polyline (unit coords)."""
    if len(pts) < 2:
        return pts[0]
    segs = [math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) for i in range(len(pts) - 1)]
    total = sum(segs) or 1.0
    target, acc = prog * total, 0.0
    for i, d in enumerate(segs):
        if acc + d >= target and d > 0:
            f = (target - acc) / d
            return (pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f)
        acc += d
    return pts[-1]


def _build_circuits(seed: int = 0x4845) -> tuple[list, list, list]:
    """A dense 'circuit-city' in unit-circle coords: concentric ring buses, many Manhattan-routed
    traces (a third of them flagged as buses that fire impulses), and junction pads."""
    rng = random.Random(seed)

    def clamp(x: float, y: float, m: float = 0.93) -> tuple[float, float]:
        d = math.hypot(x, y)
        return (x * m / d, y * m / d) if d > m else (x, y)

    rings: list[tuple[float, float, float]] = []
    for rad in (0.30, 0.46, 0.62, 0.78, 0.90):
        a = rng.uniform(0, 360)
        for _ in range(rng.randint(1, 2)):
            span = rng.uniform(28, 85)
            rings.append((rad, a, span))
            a += span + rng.uniform(40, 110)

    traces: list[tuple[list[tuple[float, float]], bool, float]] = []  # (polyline, is_bus, phase)
    nodes: list[tuple[float, float]] = []
    for i in range(36):
        ang = rng.uniform(0, 2 * math.pi)
        r0 = rng.uniform(0.0, 0.5)
        x, y = clamp(r0 * math.cos(ang), r0 * math.sin(ang))
        pts = [(x, y)]
        horiz = rng.random() < 0.5
        for _ in range(rng.randint(2, 5)):  # right-angle (Manhattan) routing
            step = rng.uniform(0.12, 0.32) * rng.choice((-1, 1))
            x, y = clamp(*((x + step, y) if horiz else (x, y + step)))
            if (x, y) != pts[-1]:
                pts.append((x, y))
            horiz = not horiz
        if len(pts) >= 2:
            traces.append((pts, i % 3 == 0, rng.uniform(0, 1)))
            nodes.append(pts[-1])
    for _ in range(12):  # extra scattered pads
        rr, a = rng.uniform(0.15, 0.85), rng.uniform(0, 2 * math.pi)
        nodes.append((rr * math.cos(a), rr * math.sin(a)))
    return rings, traces, nodes


def _build_smoke(seed: int = 0x536D) -> list[tuple[float, float, float, float]]:
    """Energy-smoke seeds: (emit_angle, phase, speed, size)."""
    rng = random.Random(seed)
    return [
        (rng.uniform(0, 2 * math.pi), rng.uniform(0, 1), rng.uniform(0.4, 0.9), rng.uniform(0.5, 1.1))
        for _ in range(14)
    ]


def paint_orb(
    p: QPainter,
    cx: float,
    cy: float,
    r: float,
    *,
    warm: float,
    glow: float,
    t: float,
    rings: list,
    traces: list,
    nodes: list,
    smoke: list,
    thinking: bool = False,
    accent: float = 0.0,
    spin: float = 0.0,
) -> None:
    """Draw the electronic Presence orb (sphere radius r, centred at cx,cy). Shared by the live widget
    and the app-icon renderer so they're identical. The caller owns the QPainter (antialiasing/no-pen
    set, and end())."""
    center = QPointF(cx, cy)

    # 1. Energy smoke streaming off the orb (drifts outward + up, fades).
    for theta, phase, speed, sz in smoke:
        prog = (t * speed * 0.01 + phase) % 1.0
        sx, sy = cx + math.cos(theta) * r * 0.95, cy + math.sin(theta) * r * 0.95
        px = sx + math.cos(theta) * r * prog
        py = sy + math.sin(theta) * r * prog - r * 0.85 * prog
        rad = r * (0.2 + prog * 0.5) * sz
        a = int(52 * glow * math.sin(prog * math.pi))
        if a <= 1:
            continue
        blob = QRadialGradient(QPointF(px, py), rad)
        blob.setColorAt(0.0, _col(CYAN, GOLD, warm, a))
        blob.setColorAt(1.0, _col(CYAN, GOLD, warm, 0))
        p.setBrush(blob)
        p.drawEllipse(QPointF(px, py), rad, rad)

    # 2. Aura.
    aura = QRadialGradient(center, r * 1.7)
    aura.setColorAt(0.0, _col(CYAN, GOLD, warm, int(34 * glow)))
    aura.setColorAt(0.55, _col(CYAN, GOLD, warm, int(30 * glow)))
    aura.setColorAt(1.0, _col(CYAN, GOLD, warm, 0))
    p.setBrush(aura)
    p.drawEllipse(center, r * 1.7, r * 1.7)

    # 3. Dark electronic body.
    hx, hy = cx - r * 0.3, cy - r * 0.34
    body = QRadialGradient(QPointF(hx, hy), r * 1.5)
    body.setColorAt(0.0, QColor(BODY_HI))
    body.setColorAt(0.5, QColor(BODY_MID))
    body.setColorAt(1.0, QColor(BODY_EDGE))
    p.setBrush(body)
    p.drawEllipse(center, r, r)

    # 4. Circuit-city, clipped to the sphere.
    clip = QPainterPath()
    clip.addEllipse(center, r, r)
    p.save()
    p.setClipPath(clip)
    core = QRadialGradient(center, r * 0.5)
    core.setColorAt(0.0, _col(CYAN, GOLD, warm, int(90 * glow)))
    core.setColorAt(1.0, _col(CYAN, GOLD, warm, 0))
    p.setBrush(core)
    p.drawEllipse(center, r * 0.5, r * 0.5)
    lw = max(1.0, r * 0.009)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for rad, a0, span in rings:  # concentric ring buses
        pen = QPen(_col(CYAN, GOLD, warm, int(80 * glow)))
        pen.setWidthF(lw)
        p.setPen(pen)
        p.drawArc(QRectF(cx - r * rad, cy - r * rad, 2 * r * rad, 2 * r * rad), int(a0 * 16), int(span * 16))
    for pts, is_bus, _ph in traces:  # the streets / wiring
        pen = QPen(_col(CYAN, GOLD, warm, int((125 if is_bus else 66) * glow)))
        pen.setWidthF(lw)
        p.setPen(pen)
        path = QPainterPath()
        path.moveTo(cx + pts[0][0] * r, cy + pts[0][1] * r)
        for ux, uy in pts[1:]:
            path.lineTo(cx + ux * r, cy + uy * r)
        p.drawPath(path)
    p.setPen(Qt.PenStyle.NoPen)
    for ux, uy in nodes:  # junction pads
        nx, ny = cx + ux * r, cy + uy * r
        pad = QRadialGradient(QPointF(nx, ny), r * 0.026)
        pad.setColorAt(0.0, _col(CYAN, GOLD, warm, int(160 * glow)))
        pad.setColorAt(1.0, _col(CYAN, GOLD, warm, 0))
        p.setBrush(pad)
        p.drawEllipse(QPointF(nx, ny), r * 0.026, r * 0.026)
    for pts, is_bus, ph in traces:  # impulses fire down the bus traces (a comet head + fading trail)
        if not is_bus:
            continue
        head = (t * 0.03 + ph) % 1.0
        for j in range(6):
            prog = head - 0.05 * j
            if prog <= 0.02 or prog >= 1.0:
                continue
            ux, uy = _along(pts, prog)
            nx, ny = cx + ux * r, cy + uy * r
            f = 1.0 - j / 6.0
            rad = r * (0.01 + 0.03 * f)
            a = int(245 * glow * f * f)
            if a <= 1:
                continue
            comet = QRadialGradient(QPointF(nx, ny), rad)
            comet.setColorAt(
                0.0, _col("#ffffff", GOLD, warm * 0.5, a) if j == 0 else _col(CYAN, GOLD, warm, a)
            )
            comet.setColorAt(1.0, _col(CYAN, GOLD, warm, 0))
            p.setBrush(comet)
            p.drawEllipse(QPointF(nx, ny), rad, rad)
    p.restore()

    # 5. Glass overlay — shades the flat circuitry into a 3-D sphere (highlight + darkened rim).
    p.save()
    p.setClipPath(clip)
    glass = QRadialGradient(QPointF(hx, hy), r * 1.6)
    glass.setColorAt(0.0, QColor(255, 255, 255, 55))
    glass.setColorAt(0.35, QColor(255, 255, 255, 0))
    glass.setColorAt(0.82, QColor(0, 0, 0, 0))
    glass.setColorAt(1.0, QColor(0, 0, 0, 150))
    p.setBrush(glass)
    p.drawEllipse(center, r, r)
    rim = QRadialGradient(center, r)
    rim.setColorAt(0.80, _col(CYAN, GOLD, warm, 0))
    rim.setColorAt(0.97, _col(CYAN, GOLD, warm, int(160 * glow)))
    rim.setColorAt(1.0, _col(CYAN, GOLD, warm, 0))
    p.setBrush(rim)
    p.drawEllipse(center, r, r)
    p.restore()

    # 6. Specular glint.
    spec = QRadialGradient(QPointF(hx, hy), r * 0.26)
    spec.setColorAt(0.0, QColor(255, 255, 255, 130))
    spec.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(spec)
    p.drawEllipse(QPointF(hx, hy), r * 0.26, r * 0.26)

    # 7. Thinking — the amber arc sweeps around the sphere.
    if thinking:
        rect = QRectF(cx - r * 1.34, cy - r * 1.34, r * 2.68, r * 2.68)
        amber = QColor(AMBER)
        amber.setAlpha(max(0, min(255, int(220 * accent))))
        pen = QPen(amber)
        pen.setWidthF(2.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(rect, int(spin * 16), 100 * 16)
        p.setPen(Qt.PenStyle.NoPen)


class PresenceOrb(QWidget):
    """The Presence. Clickable (tap to talk) and reacts to the live mic level while listening."""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(240, 240)
        self.setStyleSheet("background: transparent;")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._state = OrbState.IDLE
        self._p = dict(_PARAMS[OrbState.IDLE])
        self._phase = 0.0
        self._spin = 0.0
        self._t = 0.0
        self._level = 0.0
        self._level_target = 0.0
        self._rings, self._traces, self._nodes = _build_circuits()
        self._smoke = _build_smoke()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)  # ~30 fps

    def set_state(self, state: OrbState) -> None:
        self._state = state

    def set_level(self, level: float) -> None:
        self._level_target = max(0.0, min(1.0, float(level)))

    def mousePressEvent(self, _event) -> None:
        self.clicked.emit()

    def _tick(self) -> None:
        target = _PARAMS[self._state]
        for k in _KEYS:
            self._p[k] += (target[k] - self._p[k]) * 0.08
        self._level += (self._level_target - self._level) * 0.35
        self._level_target *= 0.82
        self._phase += self._p["speed"]
        self._spin = (self._spin + 3.0) % 360.0
        self._t += 1.0
        self.update()

    def paintEvent(self, _event) -> None:
        w, h = self.width(), self.height()
        cx, cy = w / 2, h * 0.43
        base = min(w, h) * 0.30
        glow = min(1.0, self._p["glow"] + self._level * 0.4)
        amp = self._p["amp"] + self._level * 0.04
        r = base * (1 + amp * math.sin(self._phase))

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        paint_orb(
            p, cx, cy, r,
            warm=self._p["warm"], glow=glow, t=self._t,
            rings=self._rings, traces=self._traces, nodes=self._nodes, smoke=self._smoke,
            thinking=(self._state is OrbState.THINKING), accent=self._p["accent"], spin=self._spin,
        )
        p.end()
