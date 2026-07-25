"""The camera window — HELIX's eye on the physical world.

The model calls view_camera; the tool's worker thread publishes CameraRequested and parks on the
request holder; HelixMainWindow opens THIS window on the GUI thread. It shows a live MIRRORED
preview (natural for positioning, like a mirror), counts down, then captures ONE UN-mirrored frame
— so printed markings on the object read correctly — and hands PNG bytes back through the holder.

Design constraints it honors:
- Preview via QVideoSink frames painted into a QLabel — no QtMultimediaWidgets dependency.
- Completely SILENT (no shutter, no beep): any sound would heat the render-endpoint meter and the
  playback gate would start demanding direct address for the user's next words.
- Every exit path settles the request (frame, fail, or noticing abandonment) — the parked tool
  thread never hangs on a window that closed.
- Frames live in memory only; nothing is written to disk (vision stays ephemeral).
"""
from __future__ import annotations

import time

from PyQt6.QtCore import QBuffer, QIODevice, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from helix.logging_setup import get_logger

try:  # QtMultimedia ships with PyQt6 but needs platform plugins; degrade if it can't load.
    from PyQt6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices, QVideoSink

    _CAMERA = True
except Exception:  # pragma: no cover - depends on the host's Qt plugins
    _CAMERA = False

_LOG = get_logger("camera")

CAMERA_DEVICE_SETTING = "camera_device_id"  # chosen webcam (QCameraDevice id); "" = system default

COUNTDOWN_S = 5        # seconds from first live frame to auto-capture — hands stay on the object
MORE_TIME_S = 10.0     # what the "More time" button re-arms the countdown to (fresh, not stacked)
STARTUP_GRACE_S = 10.0  # no frame within this after start = camera busy/broken; fail instead of sit
_TICK_MS = 250          # countdown/abandon poll cadence
_PREVIEW_W, _PREVIEW_H = 520, 390


def _device_id(device) -> str:
    """A stable, round-trippable string id for a QCameraDevice — latin-1 preserves every raw byte
    (same trick as voice.device_id_str for audio). '' for a null/absent device."""
    try:
        return bytes(device.id()).decode("latin-1")
    except Exception:
        return ""


def _resolve_camera(settings):
    """The QCameraDevice for the user's chosen webcam — or the system default, or the first one
    present. None when QtMultimedia is unavailable or the machine has no camera."""
    if not _CAMERA:
        return None
    devices = QMediaDevices.videoInputs()
    if not devices:
        return None
    want = (settings.get(CAMERA_DEVICE_SETTING, "") if settings is not None else "") or ""
    if want:
        for dev in devices:
            if _device_id(dev) == want:
                return dev
    default = QMediaDevices.defaultVideoInput()
    if default is not None and not default.isNull():
        return default
    return devices[0]


class CameraPanel(QDialog):
    """One camera look: live mirrored preview, a countdown, Capture/Cancel — then it settles the
    CameraRequest and closes. Non-modal, so the rest of HELIX stays usable while it's up."""

    def __init__(self, parent: QWidget | None, request, *, settings=None) -> None:
        super().__init__(parent)
        self._request = request
        self._settings = settings
        self._latest: QImage | None = None
        self._settled = False
        self._deadline: float | None = None  # countdown end, armed by the first live frame
        self._started_at = time.monotonic()
        self._camera = None
        self._session = None
        self._sink = None

        self.setWindowTitle("HELIX — Camera")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(_PREVIEW_W + 44)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(10)

        head = QLabel("Show me")
        head.setObjectName("Title")
        root.addWidget(head)

        line = request.prompt or "Hold it up to the camera — I'll take the picture."
        why = QLabel(line)
        why.setObjectName("Status")
        why.setWordWrap(True)
        why.setTextFormat(Qt.TextFormat.PlainText)  # the model wrote this line — text, never markup
        root.addWidget(why)

        self._preview = QLabel()
        self._preview.setFixedSize(_PREVIEW_W, _PREVIEW_H)
        self._preview.setStyleSheet("background: #05080b; border: 1px solid #1b2730;")
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setText("Waking the camera…")
        root.addWidget(self._preview, alignment=Qt.AlignmentFlag.AlignHCenter)

        hint = QLabel("Auto-capture counts down once the picture is live — Capture now to skip "
                      "ahead, More time to reset the count, Cancel (or Esc) to close without one.")
        hint.setObjectName("Status")
        hint.setWordWrap(True)
        root.addWidget(hint)

        row = QHBoxLayout()
        capture = QPushButton("Capture now")
        capture.setObjectName("Primary")
        capture.clicked.connect(self._do_capture)
        more = QPushButton("More time")
        more.clicked.connect(self._more_time)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(capture)
        row.addWidget(more)
        row.addWidget(cancel)
        row.addStretch(1)
        root.addLayout(row)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(_TICK_MS)

        if not self._start_camera():
            self._settle_fail("I couldn't reach a camera on this machine.")
            QTimer.singleShot(0, self.close)

    # ---- camera plumbing -------------------------------------------------------------------

    def _start_camera(self) -> bool:
        """Bring the webcam up into a QVideoSink. False = no camera to start."""
        device = _resolve_camera(self._settings)
        if device is None:
            return False
        try:
            self._camera = QCamera(device)
            self._session = QMediaCaptureSession()
            self._sink = QVideoSink()
            self._session.setCamera(self._camera)
            self._session.setVideoSink(self._sink)
            self._sink.videoFrameChanged.connect(self._on_frame)
            self._camera.errorOccurred.connect(self._on_camera_error)
            self._camera.start()
            return True
        except Exception:
            _LOG.exception("camera start failed")
            return False

    def _stop_camera(self) -> None:
        try:
            if self._sink is not None:
                self._sink.videoFrameChanged.disconnect(self._on_frame)
        except Exception:
            pass
        try:
            if self._camera is not None:
                self._camera.stop()
        except Exception:
            pass
        self._camera = None
        self._session = None
        self._sink = None

    def _on_camera_error(self, *args) -> None:
        # Transient errors after frames are flowing are ignored; a failure before ANY frame means
        # the camera never came up (in use elsewhere, blocked by Windows privacy) — say so and go.
        if self._latest is None and not self._settled:
            _LOG.warning("camera error before first frame: %s", args)
            self._settle_fail(
                "The camera wouldn't start — another app may be using it, or Windows camera "
                "access is off for desktop apps."
            )
            self.close()

    def _on_frame(self, frame) -> None:
        if self._settled:
            return
        try:
            img = frame.toImage()
        except Exception:
            return
        if img is None or img.isNull():
            return
        self._latest = img.copy()  # detach from the driver's frame buffer before it's recycled
        if self._deadline is None:
            self._deadline = time.monotonic() + COUNTDOWN_S  # first live frame arms the countdown
        self._paint_preview()

    # ---- countdown + capture ---------------------------------------------------------------

    def _remaining(self) -> int | None:
        if self._deadline is None:
            return None
        return max(0, int(self._deadline - time.monotonic() + 0.999))

    def _more_time(self) -> None:
        """Re-arm the countdown to a FRESH window (never stacked — the 90s capture budget stays the
        hard ceiling on the worker side). Before the first frame this is a no-op; the countdown
        only ever starts from a live picture."""
        if self._deadline is not None and not self._settled:
            self._deadline = time.monotonic() + MORE_TIME_S
            self._paint_preview()

    def _tick(self) -> None:
        if self._settled:
            return
        # The worker gave up (user said stop / timeout) — nothing to hand back; just fold quietly.
        if self._request.abandoned:
            self.close()
            return
        if self._latest is None and time.monotonic() - self._started_at > STARTUP_GRACE_S:
            self._settle_fail(
                "The camera never delivered a picture — another app may be holding it."
            )
            self.close()
            return
        remaining = self._remaining()
        if remaining is not None and remaining <= 0:
            self._do_capture()
        elif self._latest is not None:
            self._paint_preview()  # keep the countdown number fresh even between frames

    def _do_capture(self) -> None:
        if self._settled:
            return
        if self._latest is None:
            self._settle_fail("The camera never delivered a picture.")
            self.close()
            return
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        ok = self._latest.save(buf, "PNG")  # UN-mirrored: markings on the object read correctly
        buf.close()
        if not ok:
            self._settle_fail("The camera picture couldn't be encoded.")
            self.close()
            return
        self._settled = True
        self._request.fulfil(bytes(buf.data()))
        self.close()

    def _settle_fail(self, reason: str) -> None:
        if not self._settled:
            self._settled = True
            self._request.fail(reason)

    # ---- painting --------------------------------------------------------------------------

    def _paint_preview(self) -> None:
        if self._latest is None:
            return
        shown = self._latest.mirrored(True, False).scaled(
            self._preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        pm = QPixmap.fromImage(shown)
        remaining = self._remaining()
        if remaining is not None and remaining > 0:
            painter = QPainter(pm)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            font = QFont(self.font())
            font.setPointSize(44)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(0, 0, 0, 170))
            painter.drawText(pm.rect().adjusted(2, 2, 2, 2), Qt.AlignmentFlag.AlignCenter,
                             str(remaining))
            painter.setPen(QColor("#3fe0e0"))
            painter.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, str(remaining))
            painter.end()
        self._preview.setPixmap(pm)

    # ---- teardown --------------------------------------------------------------------------
    # Two Qt facts shape this section (both verified on this PyQt6 build): QDialog.done()/reject()
    # deletes a WA_DeleteOnClose dialog WITHOUT delivering a QCloseEvent, and QDialog.closeEvent
    # re-enters reject() and IGNORES the close if the window is still visible afterwards. So both
    # exits share one idempotent _teardown, and closeEvent accepts the event itself rather than
    # deferring to QDialog's re-entrant version.

    def _teardown(self) -> None:
        """Stop the tick and the webcam, and settle the request (a no-op when a capture already
        fulfilled it) — so the parked tool thread wakes no matter how the window went away."""
        self._timer.stop()
        self._stop_camera()
        self._settle_fail("The user closed the camera window before a picture was taken.")

    def reject(self) -> None:  # Cancel button / Esc — this path never sees a QCloseEvent
        self._teardown()
        super().reject()

    def closeEvent(self, event) -> None:  # X button / programmatic close()
        self._teardown()
        event.accept()


def show_camera_panel(parent: QWidget | None, request, *, settings=None) -> CameraPanel | None:
    """Open the non-modal camera window for ONE CameraRequest on the GUI thread. Settles the
    request immediately (fail) when camera support is missing, and skips a request whose worker
    already gave up. Returns the panel so the caller can track/close it, or None."""
    if not _CAMERA:
        request.fail("Camera support isn't available on this machine.")
        return None
    if not request.claim():
        return None  # abandoned before the UI got here (stop was faster than the event loop)
    try:
        panel = CameraPanel(parent, request, settings=settings)
    except Exception:
        # A claimed request MUST be settled by this side — an unsettled claim parks the worker
        # for the full capture timeout with no window to exit through.
        _LOG.exception("camera panel failed to open")
        request.fail("The camera window couldn't open.")
        return None
    if panel._settled:  # no camera to start — already failed; never show a dead window
        return None
    panel.show()
    panel.raise_()
    panel.activateWindow()
    return panel
