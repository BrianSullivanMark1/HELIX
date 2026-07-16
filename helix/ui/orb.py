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

from helix.ui.theme import AMBER, GOLD, STATUS_DONE, STATUS_ERROR, STATUS_WORKING

# The V3 idle hue: deep electric blue (the theme CYAN stays for the rest of the UI; the Presence
# itself runs bluer — the same palette as the GPU orb, so both layers read as ONE presence).
ELECTRIC = "#2a8cff"

# Dark electronic body, so the circuitry glows against it. V3: darker still — near-black glass;
# only the energy glows.
BODY_HI = "#16202c"
BODY_MID = "#0a1118"
BODY_EDGE = "#030609"


class OrbState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"  # heard you, turning speech → text (a fast cyan shimmer)
    THINKING = "thinking"
    SPEAKING = "speaking"


class OrbStatus(Enum):
    """The orb's BUILD status, layered on top of the conversational state. It owns the orb's HUE — the
    whole Presence shifts to one colour so a glance tells you what's happening with your builds:
      NONE    → normal cyan↔gold conversational colouring (idle blue, gold while speaking).
      WORKING → yellow: a build is in progress.
      DONE    → green: a build just finished (a brief flash, then back to NONE).
      ERROR   → red: a build errored.
    State (listening/thinking/speaking) still drives the orb's energy/animation; status drives colour."""

    NONE = "none"
    WORKING = "working"
    DONE = "done"
    ERROR = "error"


# Per-status base colours. When a status is active the orb mixes toward ONE colour (cold == warm), so the
# whole Presence reads as that hue regardless of the conversational warm-blend.
_STATUS_COLORS: dict[OrbStatus, tuple[str, str]] = {
    OrbStatus.WORKING: (STATUS_WORKING, STATUS_WORKING),
    OrbStatus.DONE: (STATUS_DONE, STATUS_DONE),
    OrbStatus.ERROR: (STATUS_ERROR, STATUS_ERROR),
}


# `warm` blends cyan→gold (speaking); `accent` drives the amber thinking arc; `glow` is overall energy.
_PARAMS: dict[OrbState, dict[str, float]] = {
    OrbState.IDLE: {"glow": 0.50, "amp": 0.050, "speed": 0.065, "accent": 0.0, "warm": 0.0},
    OrbState.LISTENING: {"glow": 0.78, "amp": 0.075, "speed": 0.10, "accent": 0.15, "warm": 0.0},
    OrbState.TRANSCRIBING: {"glow": 0.88, "amp": 0.060, "speed": 0.24, "accent": 0.0, "warm": 0.0},
    OrbState.THINKING: {"glow": 0.66, "amp": 0.050, "speed": 0.14, "accent": 1.0, "warm": 0.0},
    OrbState.SPEAKING: {"glow": 0.95, "amp": 0.10, "speed": 0.16, "accent": 0.0, "warm": 1.0},
}
_KEYS = ("glow", "amp", "speed", "accent", "warm")
_N_BANDS = 16  # FFT bands the live orb listens to for its spectral ring


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
            traces.append((pts, i % 2 == 0, rng.uniform(0, 1)))  # half are buses → more impulses firing
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
        for _ in range(18)
    ]


# The V3 gyroscope: three tilted energy rings orbiting the sphere, each carrying bright charge
# packets. (radius_factor, tilt_degrees, flatten, speed, packets, phase) — pure data, so the live
# widget and the app-icon renderer draw the same rings.
_GYRO: tuple[tuple[float, float, float, float, int, float], ...] = (
    (1.34, -16.0, 0.34, 1.00, 3, 0.00),
    (1.52, 32.0, 0.42, -0.70, 2, 0.37),
    (1.68, 76.0, 0.30, 0.50, 2, 0.71),
)


def _build_sparks(seed: int = 0x5350) -> list[tuple[float, float, float, float, float, float]]:
    """Orbiting spark seeds: (angle0, radius_factor, tilt_rad, flatten, speed, size)."""
    rng = random.Random(seed)
    return [
        (
            rng.uniform(0, 2 * math.pi), rng.uniform(1.12, 1.85), rng.uniform(0, math.pi),
            rng.uniform(0.25, 0.6), rng.uniform(0.3, 1.0), rng.uniform(0.5, 1.0),
        )
        for _ in range(46)
    ]


_SPARKS = _build_sparks()


def _gyro_point(a: float, rf: float, tilt_rad: float, flat: float) -> tuple[float, float, float]:
    """A ring/orbit point in unit coords: (x, y, depth) — depth > 0 means in FRONT of the sphere,
    so the two halves can be drawn under and over the body for a cheap, convincing 3-D read."""
    ex, ey = math.cos(a), math.sin(a) * flat
    x = ex * math.cos(tilt_rad) - ey * math.sin(tilt_rad)
    y = ex * math.sin(tilt_rad) + ey * math.cos(tilt_rad)
    return x * rf, y * rf, math.sin(a)


def _paint_gyro(
    p: QPainter, cx: float, cy: float, r: float, *, t: float, warm: float, glow: float,
    cold, warm_color, front: bool,
) -> None:
    """One depth half of the gyroscope: thin in-hue ring arcs + bright charge packets. Called twice —
    behind the body first, then over the glass — so the rings genuinely orbit the sphere."""
    n = 72
    for rf, tilt_deg, flat, speed, packets, ph in _GYRO:
        tilt = math.radians(tilt_deg)
        pen = QPen(_col(cold, warm_color, warm, int(110 * glow)))
        pen.setWidthF(max(1.2, r * 0.012))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        prev = None
        for i in range(n + 1):
            a = 2 * math.pi * i / n
            ux, uy, depth = _gyro_point(a, rf, tilt, flat)
            pt = QPointF(cx + ux * r, cy + uy * r)
            if prev is not None and (depth > 0) == front and (prev[1] > 0) == front:
                p.drawLine(prev[0], pt)
            prev = (pt, depth)
        p.setPen(Qt.PenStyle.NoPen)
        for k in range(packets):  # the racing charges — an in-hue glow with a white-hot pin
            a = ((t * 0.016 * speed + ph + k / packets) % 1.0) * 2 * math.pi
            ux, uy, depth = _gyro_point(a, rf, tilt, flat)
            if (depth > 0) != front:
                continue
            nx, ny = cx + ux * r, cy + uy * r
            rad = r * 0.040
            comet = QRadialGradient(QPointF(nx, ny), rad)
            comet.setColorAt(0.0, _col("#ffffff", warm_color, warm * 0.5, int(200 * glow)))
            comet.setColorAt(0.35, _col(cold, warm_color, warm, int(190 * glow)))
            comet.setColorAt(1.0, _col(cold, warm_color, warm, 0))
            p.setBrush(comet)
            p.drawEllipse(QPointF(nx, ny), rad, rad)


def _paint_sparks(
    p: QPainter, cx: float, cy: float, r: float, *, t: float, warm: float, glow: float,
    cold, warm_color, front: bool,
) -> None:
    """One depth half of the orbiting spark field — tiny twinkling embers around the presence."""
    p.setPen(Qt.PenStyle.NoPen)
    for a0, rf, tilt, flat, speed, size in _SPARKS:
        a = a0 + t * 0.006 * speed
        ux, uy, depth = _gyro_point(a, rf, tilt, flat)
        if (depth > 0) != front:
            continue
        tw = 0.5 + 0.5 * math.sin(t * 0.05 * (0.6 + speed) + a0 * 7.0)
        alpha = int(95 * glow * tw)
        if alpha <= 3:
            continue
        nx, ny = cx + ux * r, cy + uy * r
        rad = r * 0.016 * size * (0.7 + 0.6 * tw)
        dot = QRadialGradient(QPointF(nx, ny), rad)
        dot.setColorAt(0.0, _col(cold, warm_color, warm, alpha))
        dot.setColorAt(1.0, _col(cold, warm_color, warm, 0))
        p.setBrush(dot)
        p.drawEllipse(QPointF(nx, ny), rad, rad)


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
    cold=ELECTRIC,
    warm_color=GOLD,
) -> None:
    """Draw the electronic Presence orb (sphere radius r, centred at cx,cy). Shared by the live widget
    and the app-icon renderer so they're identical. The caller owns the QPainter (antialiasing/no-pen
    set, and end()).

    `cold`/`warm_color` are the two base colours the orb blends between (default cyan→gold); the live
    widget overrides them to drive the build-status hue (yellow/green/red). Each accepts a colour string
    or a QColor, so the widget can pass smoothly-eased QColors."""
    center = QPointF(cx, cy)

    # 1. Energy smoke streaming off the orb (drifts outward + up, fades).
    for theta, phase, speed, sz in smoke:
        prog = (t * speed * 0.01 + phase) % 1.0
        sx, sy = cx + math.cos(theta) * r * 0.95, cy + math.sin(theta) * r * 0.95
        px = sx + math.cos(theta) * r * prog
        py = sy + math.sin(theta) * r * prog - r * 0.85 * prog
        rad = r * (0.2 + prog * 0.5) * sz
        a = int(64 * glow * math.sin(prog * math.pi))
        if a <= 1:
            continue
        blob = QRadialGradient(QPointF(px, py), rad)
        blob.setColorAt(0.0, _col(cold, warm_color, warm, a))
        blob.setColorAt(1.0, _col(cold, warm_color, warm, 0))
        p.setBrush(blob)
        p.drawEllipse(QPointF(px, py), rad, rad)

    # 2. Aura.
    aura = QRadialGradient(center, r * 1.8)
    aura.setColorAt(0.0, _col(cold, warm_color, warm, int(46 * glow)))
    aura.setColorAt(0.55, _col(cold, warm_color, warm, int(38 * glow)))
    aura.setColorAt(1.0, _col(cold, warm_color, warm, 0))
    p.setBrush(aura)
    p.drawEllipse(center, r * 1.8, r * 1.8)

    # 2b. The far half of the V3 gyroscope + spark field — BEHIND the body, so the rings orbit it.
    _paint_sparks(p, cx, cy, r, t=t, warm=warm, glow=glow, cold=cold, warm_color=warm_color,
                  front=False)
    _paint_gyro(p, cx, cy, r, t=t, warm=warm, glow=glow, cold=cold, warm_color=warm_color,
                front=False)

    # 3. Dark electronic body.
    hx, hy = cx - r * 0.3, cy - r * 0.34
    body = QRadialGradient(QPointF(hx, hy), r * 1.5)
    body.setColorAt(0.0, QColor(BODY_HI))
    body.setColorAt(0.5, QColor(BODY_MID))
    body.setColorAt(1.0, QColor(BODY_EDGE))
    p.setBrush(body)
    p.drawEllipse(center, r, r)

    # 4. Circuit-city, clipped to the sphere — a living lattice: a mind-core that breathes and emits energy
    #    pulses, an activation wavefront that lights the circuit up in sequence, synapses that flare
    #    white-hot, and impulses racing the buses. This is where the Presence reads as conscious.
    clip = QPainterPath()
    clip.addEllipse(center, r, r)
    p.save()
    p.setClipPath(clip)

    # The mind core: a central glow that BREATHES (a slow pulse), brightest at its white-hot centre.
    pulse = 0.72 + 0.28 * math.sin(t * 0.045)
    core = QRadialGradient(center, r * 0.58)
    core.setColorAt(0.0, _col("#ffffff", warm_color, warm * 0.6, int(72 * glow * pulse)))
    core.setColorAt(0.4, _col(cold, warm_color, warm, int(120 * glow * pulse)))
    core.setColorAt(1.0, _col(cold, warm_color, warm, 0))
    p.setBrush(core)
    p.drawEllipse(center, r * 0.58, r * 0.58)

    # Concentric energy pulses ripple outward from the core — the visible "thinking" heartbeat.
    p.setBrush(Qt.BrushStyle.NoBrush)
    for k in range(3):
        rp = (t * 0.011 + k / 3.0) % 1.0
        ring_r = r * (0.06 + rp * 0.96)
        a = int(120 * glow * math.sin(rp * math.pi))
        if a <= 2:
            continue
        pen = QPen(_col(cold, warm_color, warm, a))
        pen.setWidthF(max(1.0, r * 0.014))
        p.setPen(pen)
        p.drawEllipse(center, ring_r, ring_r)

    # An activation wavefront sweeps outward; everything near it brightens — a thought propagating.
    wf = (t * 0.010) % 1.45

    def wave(rr: float) -> float:
        d = rr - wf
        return 1.0 + 1.05 * math.exp(-(d * d) / 0.010)

    lw = max(1.0, r * 0.009)
    for rad, a0, span in rings:  # concentric ring buses
        pen = QPen(_col(cold, warm_color, warm, min(255, int(82 * glow * wave(rad)))))
        pen.setWidthF(lw)
        p.setPen(pen)
        p.drawArc(QRectF(cx - r * rad, cy - r * rad, 2 * r * rad, 2 * r * rad), int(a0 * 16), int(span * 16))
    for pts, is_bus, _ph in traces:  # the streets / wiring
        rr = math.hypot(*_along(pts, 0.5))  # midpoint radius drives the wave brightening
        a = min(255, int((132 if is_bus else 64) * glow * wave(rr)))
        pen = QPen(_col(cold, warm_color, warm, a))
        pen.setWidthF(lw * (1.25 if is_bus else 1.0))
        p.setPen(pen)
        path = QPainterPath()
        path.moveTo(cx + pts[0][0] * r, cy + pts[0][1] * r)
        for ux, uy in pts[1:]:
            path.lineTo(cx + ux * r, cy + uy * r)
        p.drawPath(path)
    p.setPen(Qt.PenStyle.NoPen)
    for ux, uy in nodes:  # junction pads = synapses: each twinkles on its own clock, some flare white-hot
        nx, ny = cx + ux * r, cy + uy * r
        rr = math.hypot(ux, uy)
        ph = (ux * 7.3 + uy * 5.1) % 1.0
        tw = 0.5 + 0.5 * math.sin((t * 0.05 + ph) * 2 * math.pi)
        bright = wave(rr) * (0.55 + 0.85 * tw)
        pr = r * 0.028 * (0.85 + 0.4 * tw)
        hot = tw > 0.86  # a firing synapse
        pad = QRadialGradient(QPointF(nx, ny), pr)
        pad.setColorAt(0.0, _col("#ffffff", warm_color, warm * 0.5, min(255, int(175 * glow * bright)))
                       if hot else _col(cold, warm_color, warm, min(255, int(150 * glow * bright))))
        pad.setColorAt(1.0, _col(cold, warm_color, warm, 0))
        p.setBrush(pad)
        p.drawEllipse(QPointF(nx, ny), pr, pr)
        if hot:  # a wider soft bloom when it fires
            bloom = QRadialGradient(QPointF(nx, ny), pr * 2.6)
            bloom.setColorAt(0.0, _col(cold, warm_color, warm, int(95 * glow)))
            bloom.setColorAt(1.0, _col(cold, warm_color, warm, 0))
            p.setBrush(bloom)
            p.drawEllipse(QPointF(nx, ny), pr * 2.6, pr * 2.6)
    for pts, is_bus, ph in traces:  # impulses race the buses — a white-hot comet head + a glowing trail
        if not is_bus:
            continue
        head = (t * 0.032 + ph) % 1.0
        for j in range(9):
            prog = head - 0.045 * j
            if prog <= 0.02 or prog >= 1.0:
                continue
            ux, uy = _along(pts, prog)
            nx, ny = cx + ux * r, cy + uy * r
            f = 1.0 - j / 9.0
            rad = r * (0.012 + 0.036 * f)
            a = int(255 * glow * f * f)
            if a <= 2:
                continue
            comet = QRadialGradient(QPointF(nx, ny), rad)
            comet.setColorAt(
                0.0, _col("#ffffff", warm_color, warm * 0.5, a) if j == 0 else _col(cold, warm_color, warm, a)
            )
            comet.setColorAt(1.0, _col(cold, warm_color, warm, 0))
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
    rim.setColorAt(0.80, _col(cold, warm_color, warm, 0))
    rim.setColorAt(0.97, _col(cold, warm_color, warm, int(205 * glow)))
    rim.setColorAt(1.0, _col(cold, warm_color, warm, 0))
    p.setBrush(rim)
    p.drawEllipse(center, r, r)
    p.restore()

    # 6. Specular glint — restrained: a hint of glass, never a fog.
    spec = QRadialGradient(QPointF(hx, hy), r * 0.26)
    spec.setColorAt(0.0, QColor(255, 255, 255, 70))
    spec.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(spec)
    p.drawEllipse(QPointF(hx, hy), r * 0.26, r * 0.26)

    # 6b. The near half of the gyroscope + sparks — OVER the glass, completing the 3-D orbit.
    _paint_gyro(p, cx, cy, r, t=t, warm=warm, glow=glow, cold=cold, warm_color=warm_color,
                front=True)
    _paint_sparks(p, cx, cy, r, t=t, warm=warm, glow=glow, cold=cold, warm_color=warm_color,
                  front=True)
    p.setPen(Qt.PenStyle.NoPen)

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
        self._status = OrbStatus.NONE
        self._p = dict(_PARAMS[OrbState.IDLE])
        # Eased base colours: the whole orb mixes between these two. Conversational colouring rides
        # electric-blue→gold; a build status overrides both toward one hue (yellow/green/red), eased
        # so it fades, not snaps.
        self._cold = QColor(ELECTRIC)
        self._warm = QColor(GOLD)
        self._phase = 0.0
        self._spin = 0.0
        self._t = 0.0
        self._level = 0.0
        self._level_target = 0.0
        self._bands = [0.0] * _N_BANDS          # eased FFT band energies (the spectral ring)
        self._bands_target = [0.0] * _N_BANDS
        self._rings, self._traces, self._nodes = _build_circuits()
        self._smoke = _build_smoke()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        # ~30 fps while anything is actually moving (listening/speaking/thinking, a build-status hue, or
        # live mic energy); ~15 fps for the slow idle breathing. Halves the repaint load on a
        # permanently-open app without freezing the ambient presence. _tick adjusts the interval live.
        self._active_ms, self._idle_ms = 33, 66
        self._timer.start(self._active_ms)

    def set_state(self, state: OrbState) -> None:
        self._state = state

    def set_status(self, status: OrbStatus) -> None:
        """Set the build-status hue (yellow/green/red), or NONE for normal conversational colour. Eased,
        so the orb glides between colours rather than snapping."""
        self._status = status if isinstance(status, OrbStatus) else OrbStatus.NONE

    def _target_colors(self) -> tuple[QColor, QColor]:
        """The (cold, warm) base colours this frame should ease toward. A build status owns the hue; with
        no status, the normal cyan→gold pair (the conversational warm-blend rides on top)."""
        pair = _STATUS_COLORS.get(self._status)
        if pair is not None:
            return QColor(pair[0]), QColor(pair[1])
        return QColor(ELECTRIC), QColor(GOLD)

    def set_level(self, level: float) -> None:
        self._level_target = max(0.0, min(1.0, float(level)))

    def set_bands(self, bands) -> None:
        """Live FFT band energies (each ~0..1) from the mic — drives the spectral ring while listening."""
        try:
            b = [max(0.0, min(1.0, float(x))) for x in list(bands)[:_N_BANDS]]
        except (TypeError, ValueError):
            return  # ignore malformed input; the ring just keeps its last values and decays
        b += [0.0] * (_N_BANDS - len(b))
        self._bands_target = b

    def mousePressEvent(self, _event) -> None:
        self.clicked.emit()

    @staticmethod
    def _ease_color(cur: QColor, target: QColor, f: float = 0.08) -> QColor:
        return QColor(
            int(cur.red() + (target.red() - cur.red()) * f),
            int(cur.green() + (target.green() - cur.green()) * f),
            int(cur.blue() + (target.blue() - cur.blue()) * f),
        )

    def _tick(self) -> None:
        target = _PARAMS[self._state]
        for k in _KEYS:
            self._p[k] += (target[k] - self._p[k]) * 0.08
        tcold, twarm = self._target_colors()
        self._cold = self._ease_color(self._cold, tcold)
        self._warm = self._ease_color(self._warm, twarm)
        self._level += (self._level_target - self._level) * 0.35
        self._level_target *= 0.82
        for i in range(_N_BANDS):
            self._bands[i] += (self._bands_target[i] - self._bands[i]) * 0.40
            self._bands_target[i] *= 0.80  # decay so the ring relaxes when the room goes quiet
        self._phase += self._p["speed"]
        self._spin = (self._spin + 3.0) % 360.0
        self._t += 1.0
        self.update()
        # Downshift to the idle cadence only when nothing is actively animating: resting state, no build
        # hue, and the mic level + spectral ring have decayed to ~zero. Any set_state/set_status/set_level
        # /set_bands lifts one of these and the very next tick restores the fast cadence.
        busy = (
            self._state != OrbState.IDLE or self._status != OrbStatus.NONE
            or self._level > 0.01 or self._level_target > 0.01
            or any(b > 0.01 for b in self._bands) or any(b > 0.01 for b in self._bands_target)
        )
        want = self._active_ms if busy else self._idle_ms
        if self._timer.interval() != want:
            self._timer.setInterval(want)

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
            cold=self._cold, warm_color=self._warm,
        )
        self._paint_spectrum(p, cx, cy, r, glow)
        p.end()

    def _paint_spectrum(self, p: QPainter, cx: float, cy: float, r: float, glow: float) -> None:
        """A ring of soft spikes around the orb that flick with the live voice spectrum. Drawn over
        paint_orb on the live widget only (the shared paint_orb + app icon stay unchanged). Silent rooms
        relax to nothing, so it only appears when you actually speak."""
        peak = max(self._bands) if self._bands else 0.0
        if peak + self._level < 0.025:
            return
        warm = self._p["warm"]
        cold, warm_color = self._cold, self._warm  # the eased base colours (status hue rides here too)
        n, half = 64, 32
        inner = r * 1.06
        for i in range(n):
            ang = (i / n) * 2 * math.pi - math.pi / 2
            bi = int(abs(i - half) / half * (_N_BANDS - 1))  # symmetric L/R, low freqs at the sides
            mag = max(self._bands[bi], self._level * 0.5)
            a = int(150 * glow * mag)
            if a < 6:
                continue
            length = r * (0.04 + 0.42 * mag)
            x0, y0 = cx + math.cos(ang) * inner, cy + math.sin(ang) * inner
            x1, y1 = cx + math.cos(ang) * (inner + length), cy + math.sin(ang) * (inner + length)
            pen = QPen(_col(cold, warm_color, warm, a), 2.2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(x0, y0), QPointF(x1, y1))
        p.setPen(Qt.PenStyle.NoPen)
