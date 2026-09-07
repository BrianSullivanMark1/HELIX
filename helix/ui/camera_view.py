"""The camera window — HELIX's eye on the physical world.

The model calls view_camera; the tool's worker thread publishes CameraRequested and parks on the
request holder; HelixMainWindow opens THIS window on the GUI thread. It shows a live MIRRORED
preview (natural for positioning, like a mirror) and waits — no countdown, no time pressure — until
the user takes the picture BY VOICE ("take the picture" — the voice layer's camera session routes
the tiny grammar to voice_capture/voice_cancel) or with the buttons. The ONE captured frame is
UN-mirrored — so printed markings on the object read correctly — and goes back through the holder.
A camera picker in the window switches between attached webcams and remembers the choice.

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
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from helix.logging_setup import get_logger

try:  # QtMultimedia ships with PyQt6 but needs platform plugins; degrade if it can't load.
    from PyQt6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices, QVideoSink

    _CAMERA = True
except Exception:  # pragma: no cover - depends on the host's Qt plugins
    _CAMERA = False

_LOG = get_logger("camera")

CAMERA_DEVICE_SETTING = "camera_device_id"  # chosen webcam (QCameraDevice id); "" = system default

STARTUP_GRACE_S = 10.0  # no frame within this after start = camera busy/broken; fail instead of sit
_TICK_MS = 250          # abandon/no-frame poll cadence
_PREVIEW_W, _PREVIEW_H = 520, 390


def _device_id(device) -> str:
    """A stable, round-trippable string id for a QCameraDevice — latin-1 preserves every raw byte
    (same trick as voice.device_id_str for audio). '' for a null/absent device."""
    try:
        return bytes(device.id()).decode("latin-1")
    except Exception:
        return ""


def _video_inputs() -> list:
    """Every attached camera, [] when QtMultimedia is unavailable. A seam tests can stub."""
    if not _CAMERA:
        return []
    try:
        return list(QMediaDevices.videoInputs())
    except Exception:
        return []


def _resolve_camera(settings):
    """The QCameraDevice for the user's chosen webcam — or the system default, or the first one
    present. None when QtMultimedia is unavailable or the machine has no camera."""
    devices = _video_inputs()
    if not devices:
        return None
    want = (settings.get(CAMERA_DEVICE_SETTING, "") if settings is not None else "") or ""
    if want:
        for dev in devices:
            if _device_id(dev) == want:
                return dev
    if _CAMERA:
        default = QMediaDevices.defaultVideoInput()
        if default is not None and not default.isNull():
            return default
    return devices[0]


class CameraPanel(QDialog):
    """One camera look: live mirrored preview, a camera picker, and capture on the user's word
    (voice or button) — then it settles the CameraRequest and closes. Non-modal, so the rest of
    HELIX stays usable while it's up."""

    def __init__(self, parent: QWidget | None, request, *, settings=None,
                 voice_ready: bool = False) -> None:
        super().__init__(parent)
        self._request = request
        self._settings = settings
        self._latest: QImage | None = None
        self._settled = False
        self._pending_capture = False  # the word came before the first frame: capture on arrival
        self._started_at = time.monotonic()
        self._active_device_id = ""
        self._pending_device_id = ""  # a picked camera not yet PROVEN by a frame — see _on_frame
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

        line = request.prompt or "Hold it up to the camera — take your time."
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

        picker = QHBoxLayout()
        pick_label = QLabel("Camera")
        pick_label.setObjectName("Status")
        self._combo = QComboBox()
        self._combo.currentIndexChanged.connect(self._on_camera_pick)
        picker.addWidget(pick_label)
        picker.addWidget(self._combo, 1)
        root.addLayout(picker)

        # The hint tells the truth about the ears: voice_ready is the shell's word that the camera
        # voice session is actually live (mic on, model warm, not asleep) — never promise
        # listening that isn't happening.
        hint = QLabel(
            "No rush — I'm listening. Say 'take the picture' when you're ready, or 'cancel' to "
            "close without one. The buttons work too."
            if voice_ready else
            "No rush — take the picture with the button when you're ready; Cancel (or Esc) "
            "closes without one."
        )
        hint.setObjectName("Status")
        hint.setWordWrap(True)
        root.addWidget(hint)

        row = QHBoxLayout()
        capture = QPushButton("Take the picture")
        capture.setObjectName("Primary")
        capture.clicked.connect(self._do_capture)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(capture)
        row.addWidget(cancel)
        row.addStretch(1)
        root.addLayout(row)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(_TICK_MS)

        if not self._start_camera():
            self._settle_fail("I couldn't reach a camera on this machine.")
            QTimer.singleShot(0, self.close)
            return
        self._populate_cameras()

    # ---- camera plumbing -------------------------------------------------------------------

    def _start_camera(self, device=None) -> bool:
        """Bring a webcam up into a QVideoSink — the given device, or the saved/default one.
        False = no camera to start."""
        if device is None:
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
            self._active_device_id = _device_id(device)
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

    def _populate_cameras(self) -> None:
        """Fill the picker with every attached camera, current one selected. One camera still
        shows — it names what HELIX is looking through."""
        devices = _video_inputs()
        self._combo.blockSignals(True)
        self._combo.clear()
        for dev in devices:
            try:
                label = dev.description() or "Camera"
            except Exception:
                label = "Camera"
            self._combo.addItem(label, _device_id(dev))
        idx = self._combo.findData(self._active_device_id)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)
        self._combo.blockSignals(False)

    def _on_camera_pick(self, index: int) -> None:
        """The user picked a different camera: switch live, and remember the choice once the new
        device has actually delivered a picture (the saved id is what _resolve_camera prefers next
        time, so it must only ever name a camera that demonstrably works)."""
        if self._settled:
            return
        want = self._combo.itemData(index)
        if not want or want == self._active_device_id:
            return
        device = next((d for d in _video_inputs() if _device_id(d) == want), None)
        if device is None:
            return
        self._stop_camera()
        self._latest = None
        self._started_at = time.monotonic()  # the no-frame grace restarts for the new device
        # "Take the picture" said while camera A sat dark described CAMERA A. Carrying the latch over
        # spends camera B's very first frame — a black, still-auto-exposing warm-up shot taken before
        # the user can see what B is even pointed at — and the look is gone. Switching cameras is
        # exactly what someone does BECAUSE the word didn't land, so the word has to come again.
        self._pending_capture = False
        self._preview.setPixmap(QPixmap())
        self._preview.setText("Switching camera…")
        if not self._start_camera(device):
            self._settle_fail("I couldn't switch to that camera.")
            self.close()
            return
        # Deliberately NOT saved yet. start() is asynchronous: it returns True for a camera that
        # enumerates and opens and then never delivers a single frame (held by Zoom, a stalled
        # virtual cam). Persisting here burns that id into every FUTURE look — _resolve_camera
        # prefers the saved id over the system default, and nothing else in the app can clear it —
        # so each look would open the dead camera and die on the startup grace. Both ways a started
        # camera can still fail (the _tick no-frame reap and the async _on_camera_error) land after
        # this line, which is why only a delivered frame is allowed to commit the choice.
        self._pending_device_id = want

    def _remember_camera(self, device_id: str) -> None:
        """Persist the picked webcam. Called from _on_frame only — a frame is the one piece of
        evidence that the device actually delivers pictures."""
        self._pending_device_id = ""
        if self._settings is None:
            return
        try:
            self._settings.set(CAMERA_DEVICE_SETTING, device_id)
        except Exception:  # remembering the choice must never break the live preview
            _LOG.exception("couldn't save the camera choice")

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
        if self._pending_device_id:  # this camera just proved itself — now the choice is worth saving
            self._remember_camera(self._pending_device_id)
        if self._pending_capture:  # they said the word while the sensor was still waking
            self._pending_capture = False
            self._do_capture()
            return
        self._paint_preview()

    # ---- capture ---------------------------------------------------------------------------

    def _tick(self) -> None:
        if self._settled:
            return
        # The worker gave up (user said stop / the capture ceiling ran out) — fold quietly.
        if self._request.abandoned:
            self.close()
            return
        if self._latest is None and time.monotonic() - self._started_at > STARTUP_GRACE_S:
            self._settle_fail(
                "The camera never delivered a picture — another app may be holding it."
            )
            self.close()

    def _do_capture(self) -> None:
        if self._settled:
            return
        if self._latest is None:
            # The word came before the sensor's first frame (warm-up, or right after a picker
            # switch). Don't fail the whole look — capture the moment a frame lands; the startup
            # grace in _tick still reaps a camera that never delivers at all.
            self._pending_capture = True
            self._preview.setText("One moment — the camera is still waking…")
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

    # ---- voice hooks (called on the UI thread by the voice layer's camera session) -----------

    def voice_capture(self) -> None:
        """'Take the picture' by voice — same funnel as the button."""
        self._do_capture()

    def voice_cancel(self) -> None:
        """'Cancel' by voice — same funnel as the Cancel button."""
        self.reject()

    # ---- painting --------------------------------------------------------------------------

    def _paint_preview(self) -> None:
        if self._latest is None:
            return
        shown = self._latest.mirrored(True, False).scaled(
            self._preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._preview.setPixmap(QPixmap.fromImage(shown))

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


def show_camera_panel(parent: QWidget | None, request, *, settings=None,
                      voice_ready: bool = False) -> CameraPanel | None:
    """Open the non-modal camera window for ONE CameraRequest on the GUI thread. Settles the
    request immediately (fail) when camera support is missing, and skips a request whose worker
    already gave up. Returns the panel so the caller can track/close it, or None."""
    if not _CAMERA:
        request.fail("Camera support isn't available on this machine.")
        return None
    if not request.claim():
        return None  # abandoned before the UI got here (stop was faster than the event loop)
    try:
        panel = CameraPanel(parent, request, settings=settings, voice_ready=voice_ready)
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
