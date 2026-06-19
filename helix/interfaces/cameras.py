"""Cameras screen — a horizontal carousel "wheel" of the registered house cameras.

Self-contained PyQt6 widget. The active camera is front-and-centre and large, running its live
OpenCV feed at full frame rate; cameras to either side recede in size + opacity along the wheel
curve (a ferris-wheel-lying-flat perspective). Arrow keys / on-screen buttons spin the wheel.

Only the centre camera runs at full rate; its immediate neighbours run at a reduced rate (5fps) and
the rest are paused, to keep things light. Offline cameras draw a styled placeholder instead of
crashing. OpenCV is an optional, guarded dependency (mirrors `helix/vision/camera.py`)."""
from __future__ import annotations

import math
from typing import Any

from PyQt6.QtCore import Qt, QRectF, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from helix.core.settings import AppSettings
from helix.vision import camera as vision_camera

# HUD palette (matches qt_app.apply_hud_style).
_BG = QColor("#061013")
_TILE_BG = QColor("#071417")
_BORDER = QColor("#286979")
_BORDER_HOT = QColor("#1dd8ff")
_TEXT = QColor("#eaffff")
_MUTED = QColor("#6fb3c0")
_OFFLINE = QColor("#ff6b6b")

_FULL_FPS = 24.0      # the centre camera
_NEIGHBOUR_FPS = 5.0  # the two cameras either side of centre
_STEP_DEG = 34.0      # angular spacing between cameras on the wheel
_VISIBLE_COS = 0.12   # only draw cameras on the front-facing half of the wheel


class CameraStream(QThread):
    """Reads frames from one camera (USB index or stream URL) on a background thread.

    `set_fps(0)` pauses reading (the capture stays open, last frame retained); `set_fps(n)` resumes
    at ~n frames/sec. Emits `frame_ready(QImage)` on each grab and `went_offline()` if the source
    can't be opened or read."""

    frame_ready = pyqtSignal(QImage)
    went_offline = pyqtSignal()

    def __init__(self, source: Any, parent=None) -> None:
        super().__init__(parent)
        self._source = source
        self._fps = 0.0
        self._running = True

    def set_fps(self, fps: float) -> None:
        self._fps = max(0.0, float(fps))

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:  # noqa: C901 - linear capture loop
        try:
            import cv2
        except Exception:
            self.went_offline.emit()
            return
        src = vision_camera.resolve_source(self._source)
        cap = None
        try:
            if isinstance(src, int) and hasattr(cv2, "CAP_DSHOW"):
                cap = cv2.VideoCapture(src, cv2.CAP_DSHOW)  # reliable USB backend on Windows
            else:
                cap = cv2.VideoCapture(src)
            if cap is None or not cap.isOpened():
                self.went_offline.emit()
                return
            while self._running:
                if self._fps <= 0.0:
                    self.msleep(120)  # paused — keep the capture warm, don't grab
                    continue
                ok, frame = cap.read()
                if not ok or frame is None:
                    self.went_offline.emit()
                    self.msleep(1000)  # back off, then retry (a stream may recover)
                    continue
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb.shape
                # .copy() detaches the QImage from the soon-to-be-reused numpy buffer.
                image = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()
                self.frame_ready.emit(image)
                self.msleep(max(1, int(1000.0 / self._fps)))
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass


class _Cam:
    """One camera tile's live state."""

    def __init__(self, name: str, source: Any) -> None:
        self.name = name
        self.source = source
        self.image: QImage | None = None
        self.offline = False
        self.stream: CameraStream | None = None


class CameraCarousel(QWidget):
    """A horizontal wheel of camera tiles. Spin with the arrow keys or the on-screen buttons."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.settings = AppSettings()
        self._cams: list[_Cam] = []
        self._rotation = 0.0   # continuous "current centre index" (animates toward _target)
        self._target = 0       # the camera index we're spinning toward
        self.setMinimumHeight(420)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        outer.addStretch(1)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 10)
        self._left = QPushButton("‹")
        self._right = QPushButton("›")
        for btn in (self._left, self._right):
            btn.setObjectName("ghostButton")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedSize(46, 38)
            btn.setFont(QFont("", 18))
        self._left.clicked.connect(lambda: self.spin(-1))
        self._right.clicked.connect(lambda: self.spin(1))
        self._caption = QLabel("")
        self._caption.setObjectName("panelTitle")
        self._caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        controls.addStretch(1)
        controls.addWidget(self._left)
        controls.addSpacing(10)
        controls.addWidget(self._caption, 0, Qt.AlignmentFlag.AlignCenter)
        controls.addSpacing(10)
        controls.addWidget(self._right)
        controls.addStretch(1)
        outer.addLayout(controls)

        # ~60fps repaint timer drives both the spin easing and live-frame display; running only while
        # the screen is visible (started in showEvent, stopped in hideEvent).
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # -- camera registry / lifecycle ---------------------------------------- #

    def reload(self) -> None:
        """Re-read the registered cameras and (re)start the streams for what's visible."""
        self._stop_streams()
        cams = vision_camera.list_cameras(self.settings)
        self._cams = [_Cam(str(c.get("name", "?")), c.get("source", vision_camera.DEFAULT_CAMERA_INDEX)) for c in cams]
        self._target = min(self._target, max(0, len(self._cams) - 1))
        self._rotation = float(self._target)
        for cam in self._cams:
            stream = CameraStream(cam.source, self)
            stream.frame_ready.connect(lambda img, c=cam: self._on_frame(c, img))
            stream.went_offline.connect(lambda c=cam: self._on_offline(c))
            cam.stream = stream
            stream.start()
        self._apply_rates()
        self._update_caption()
        self.update()

    def _stop_streams(self) -> None:
        for cam in self._cams:
            if cam.stream is not None:
                cam.stream.stop()
                cam.stream.wait(800)
                cam.stream = None

    def _apply_rates(self) -> None:
        """Centre camera = full rate; immediate neighbours = reduced; everything else paused."""
        centre = self._target
        for i, cam in enumerate(self._cams):
            if cam.stream is None:
                continue
            dist = abs(i - centre)
            if dist == 0:
                cam.stream.set_fps(_FULL_FPS)
            elif dist == 1:
                cam.stream.set_fps(_NEIGHBOUR_FPS)
            else:
                cam.stream.set_fps(0.0)  # paused

    def _on_frame(self, cam: _Cam, image: QImage) -> None:
        cam.image = image
        cam.offline = False

    def _on_offline(self, cam: _Cam) -> None:
        cam.offline = True

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self.reload()
        self._timer.start(16)
        self.setFocus()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._timer.stop()
        self._stop_streams()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._stop_streams()
        super().closeEvent(event)

    # -- interaction --------------------------------------------------------- #

    def spin(self, direction: int) -> None:
        if not self._cams:
            return
        self._target = max(0, min(len(self._cams) - 1, self._target + (1 if direction > 0 else -1)))
        self._apply_rates()
        self._update_caption()
        self.setFocus()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Left:
            self.spin(-1)
        elif event.key() == Qt.Key.Key_Right:
            self.spin(1)
        else:
            super().keyPressEvent(event)

    def _update_caption(self) -> None:
        if self._cams:
            self._caption.setText(f"{self._cams[self._target].name}   ·   {self._target + 1} / {len(self._cams)}")
        else:
            self._caption.setText("")

    def _tick(self) -> None:
        # Ease the wheel toward the target index, then repaint (also refreshes the live frames).
        self._rotation += (self._target - self._rotation) * 0.22
        if abs(self._rotation - self._target) < 0.001:
            self._rotation = float(self._target)
        self.update()

    # -- rendering ----------------------------------------------------------- #

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), _BG)

        if not self._cams:
            painter.setPen(_MUTED)
            painter.setFont(QFont("", 12))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "No cameras registered yet.\nAsk HELIX to “add a camera”, then say “show cameras”.",
            )
            painter.end()
            return

        cx = self.width() / 2.0
        cy = self.height() / 2.0 - 14
        radius = self.width() * 0.40
        base_h = min(self.height() * 0.56, self.width() * 0.34)
        base_w = base_h * 4.0 / 3.0

        # Build the draw list, then paint far → near so the centre tile lands on top.
        tiles = []
        for i, cam in enumerate(self._cams):
            theta = math.radians((i - self._rotation) * _STEP_DEG)
            depth = math.cos(theta)
            if depth <= _VISIBLE_COS:
                continue
            tiles.append((depth, cam, math.sin(theta)))
        tiles.sort(key=lambda t: t[0])  # ascending depth → nearest (largest cos) drawn last

        for depth, cam, sin_t in tiles:
            scale = 0.45 + 0.55 * depth          # recede in size
            opacity = 0.30 + 0.70 * depth         # …and in opacity
            w = base_w * scale
            h = base_h * scale
            x = cx + sin_t * radius
            rect = QRectF(x - w / 2.0, cy - h / 2.0, w, h)
            painter.setOpacity(opacity)
            self._draw_tile(painter, rect, cam, focused=depth > 0.985)

        painter.setOpacity(1.0)
        painter.end()

    def _draw_tile(self, painter: QPainter, rect: QRectF, cam: _Cam, *, focused: bool) -> None:
        radius = 12.0
        painter.setBrush(QBrush(_TILE_BG))
        painter.setPen(QPen(_BORDER_HOT if focused else _BORDER, 2.0 if focused else 1.0))
        painter.drawRoundedRect(rect, radius, radius)

        inner = rect.adjusted(3, 3, -3, -3)
        if cam.offline or cam.image is None:
            painter.setPen(_OFFLINE if cam.offline else _MUTED)
            painter.setFont(QFont("", max(8, int(rect.height() * 0.06))))
            painter.drawText(
                inner,
                Qt.AlignmentFlag.AlignCenter,
                "● OFFLINE" if cam.offline else "connecting…",
            )
        else:
            scaled = cam.image.scaled(
                int(inner.width()),
                int(inner.height()),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            # Centre-crop the expanded frame into the tile.
            sx = max(0, (scaled.width() - int(inner.width())) // 2)
            sy = max(0, (scaled.height() - int(inner.height())) // 2)
            cropped = scaled.copy(sx, sy, int(inner.width()), int(inner.height()))
            painter.save()
            painter.setClipRect(inner)
            painter.drawImage(inner.topLeft(), cropped)
            painter.restore()

        # Camera name label beneath the tile.
        label_rect = QRectF(rect.left(), rect.bottom() + 4, rect.width(), 22)
        painter.setPen(_TEXT if focused else _MUTED)
        painter.setFont(QFont("", max(8, int(10 if focused else 8)), QFont.Weight.DemiBold if focused else QFont.Weight.Normal))
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, cam.name)
