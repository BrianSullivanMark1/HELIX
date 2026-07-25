"""The V3 camera faculty — view_camera parks the turn on a CameraRequest, the GUI window settles it
with ONE ephemeral frame, and the tool stays human-driven (fenced) and never hangs the worker."""
from __future__ import annotations

import base64
import io
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from helix.domain.events import CameraRequested  # noqa: E402
from helix.domain.vocabulary import friendly_tool_label  # noqa: E402
from helix.ports.llm import ToolOutput  # noqa: E402
from helix.services import camera, images  # noqa: E402
from helix.services.camera import CameraRequest  # noqa: E402
from helix.services.conversation import BUILD_TOOLS, SIGHT_TOOLS  # noqa: E402
from helix.services.prompts import CONSOLE_SYSTEM  # noqa: E402
from helix.services.tools import ToolRegistry  # noqa: E402


class _Bus:
    def __init__(self):
        self.published = []

    def publish(self, ev):
        self.published.append(ev)


class _AnsweringBus(_Bus):
    """A bus with a camera window on the other side: the publish handler claims the request and
    hands back the given picture bytes at once (SignalBus handlers run on the publisher's thread,
    so this mirrors the real synchronous seam)."""

    def __init__(self, data: bytes):
        super().__init__()
        self._data = data

    def publish(self, ev):
        super().publish(ev)
        assert ev.request.claim()
        ev.request.fulfil(self._data)


def _registry(bus=None) -> ToolRegistry:
    return ToolRegistry(forge=None, builds=None, bus=bus)


def _png_bytes(size=(1800, 900), color=(30, 60, 90)) -> bytes:
    PIL = pytest.importorskip("PIL.Image")
    im = PIL.new("RGB", size, color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# ---- the CameraRequest hand-off (pure, no Qt) ----------------------------------------------


def test_a_fulfilled_request_hands_the_bytes_to_the_waiting_worker():
    req = CameraRequest(prompt="hold it up")

    def _window():
        assert req.claim()
        time.sleep(0.05)
        req.fulfil(b"picture")

    threading.Thread(target=_window).start()
    assert req.wait(timeout=5.0) == b"picture"
    assert req.error == ""


def test_a_failed_request_returns_none_with_the_plain_reason():
    req = CameraRequest()
    assert req.claim()
    req.fail("The user closed the camera window before a picture was taken.")
    assert req.wait(timeout=1.0) is None
    assert "closed the camera window" in req.error


def test_an_unclaimed_request_gives_up_fast_instead_of_waiting_out_the_capture_window():
    # No UI on the bus (headless) — the worker must not sit for the full 90s capture budget.
    req = CameraRequest()
    start = time.monotonic()
    assert req.wait(claim_timeout=0.2, timeout=30.0) is None
    assert time.monotonic() - start < 2.0
    assert "no camera window" in req.error.lower()


def test_a_fail_without_a_claim_still_settles_the_wait_immediately():
    # camera_view fails the request without claiming when QtMultimedia is missing.
    req = CameraRequest()
    req.fail("Camera support isn't available on this machine.")
    start = time.monotonic()
    assert req.wait(claim_timeout=5.0, timeout=5.0) is None
    assert time.monotonic() - start < 1.0
    assert "isn't available" in req.error


def test_the_turns_stop_token_breaks_the_wait():
    class _Cancel:
        def is_set(self):
            return True

    req = CameraRequest()
    assert req.claim()
    start = time.monotonic()
    assert req.wait(cancel=_Cancel(), timeout=30.0) is None
    assert time.monotonic() - start < 1.0
    assert req.abandoned  # the window notices on its next tick and folds


def test_a_frame_arriving_after_abandonment_is_dropped_not_resurrected():
    req = CameraRequest()
    req.abandon()
    req.fulfil(b"too late")
    req.fail("also too late")
    assert req.wait(claim_timeout=0.05, timeout=0.05) is None
    assert not req.claim()  # the UI is told not to open a window for a dead request


# ---- the view_camera tool ------------------------------------------------------------------


def test_view_camera_dispatch_hands_the_model_the_frame():
    PIL = pytest.importorskip("PIL.Image")
    bus = _AnsweringBus(_png_bytes())
    out = _registry(bus).dispatch("view_camera", {"prompt": "Hold the label up close"})
    assert isinstance(out, ToolOutput) and len(out.images) == 1
    assert "showing me" in out.text.lower()
    assert [type(e) for e in bus.published] == [CameraRequested]
    assert bus.published[0].request.prompt == "Hold the label up close"
    # The frame rode the SAME pipeline as every other image: long edge capped, sane media type.
    decoded = PIL.open(io.BytesIO(base64.b64decode(out.images[0].data)))
    assert max(decoded.size) <= images.MAX_EDGE
    assert out.images[0].media_type in ("image/png", "image/jpeg")


def test_view_camera_reports_plainly_when_no_window_answers(monkeypatch):
    monkeypatch.setattr(camera, "CLAIM_TIMEOUT_S", 0.05)
    bus = _Bus()  # publishes land nowhere — no UI claimed the request
    out = _registry(bus).dispatch("view_camera", {})
    assert isinstance(out, str) and "camera" in out.lower()


def test_dispatch_threads_the_turns_stop_token_into_the_wait():
    # Esc / spoken 'stop' sets the turn CancelToken — dispatch must hand it to the wait so an open
    # camera window can never hold a stopped turn hostage (and the window folds via abandoned).
    class _ClaimOnlyBus(_Bus):
        def publish(self, ev):
            super().publish(ev)
            assert ev.request.claim()  # a window opened… and then nobody captures

    class _Cancel:
        def is_set(self):
            return True

    bus = _ClaimOnlyBus()
    start = time.monotonic()
    out = _registry(bus).dispatch("view_camera", {}, cancel=_Cancel())
    assert isinstance(out, str) and "stopped" in out.lower()
    assert time.monotonic() - start < 2.0  # broke out of the 90s capture budget at once
    assert bus.published[0].request.abandoned


def test_the_models_window_prompt_is_collapsed_capped_and_never_markup():
    # The 'prompt' arg is MODEL-written text shown in a real window: whitespace collapses to one
    # line, length is capped, and (pinned in the panel test below) it renders as plain text only.
    bus = _AnsweringBus(_png_bytes(size=(20, 10)))
    noisy = "  Hold\nthe   <b>label</b>\tup  " + "x" * 300
    _registry(bus).dispatch("view_camera", {"prompt": noisy})
    sent = bus.published[0].request.prompt
    assert "\n" not in sent and "\t" not in sent and "  " not in sent
    assert len(sent) <= 120
    assert sent.startswith("Hold the <b>label</b> up")  # collapsed, not interpreted


def test_view_camera_reports_when_the_window_hands_back_garbage():
    bus = _AnsweringBus(b"not an image at all")
    out = _registry(bus).dispatch("view_camera", {})
    assert isinstance(out, str) and "camera" in out.lower()


def test_view_camera_is_advertised_only_when_a_ui_bus_exists():
    assert "view_camera" in {s.name for s in _registry(_Bus()).specs()}
    assert "view_camera" not in {s.name for s in _registry(bus=None).specs()}


def test_camera_sight_is_fenced_and_feeds_visual_memory():
    # An unattended watcher must never open the webcam and photograph the room behind the machine.
    assert "view_camera" in BUILD_TOOLS
    # A camera turn SAW pixels — the subscription rail checks membership by name to distill
    # visual memory, so forgetting this entry would silently disable it on Brian's live rail.
    assert "view_camera" in SIGHT_TOOLS


def test_the_persona_teaches_the_camera_and_the_voice_can_say_it():
    assert "view_camera" in CONSOLE_SYSTEM  # the model learns WHEN to call it from the persona
    label = friendly_tool_label("view_camera")
    assert label == "Looking through the camera" and "_" not in label


# ---- the camera window (offscreen Qt; hardware paths stay stubbed) -------------------------

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from helix.ui import camera_view  # noqa: E402
from helix.ui.camera_view import CameraPanel, show_camera_panel  # noqa: E402


@pytest.fixture(scope="module")
def _app():
    return QApplication.instance() or QApplication([])


def _panel(monkeypatch, req, _app) -> CameraPanel:
    monkeypatch.setattr(CameraPanel, "_start_camera", lambda self: True)
    return CameraPanel(None, req)


def _frame(w=64, h=48, color="#20a0c0") -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    return img


def test_missing_qt_camera_support_settles_the_request_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(camera_view, "_CAMERA", False)
    req = CameraRequest()
    assert show_camera_panel(None, req) is None
    assert req.wait(claim_timeout=0.05, timeout=0.05) is None
    assert "isn't available" in req.error


def test_the_panel_never_opens_for_a_request_the_worker_already_abandoned(monkeypatch, _app):
    # _CAMERA is forced True so this pins the CLAIM refusal, not the missing-backend early-return.
    monkeypatch.setattr(camera_view, "_CAMERA", True)
    monkeypatch.setattr(CameraPanel, "_start_camera", lambda self: True)
    req = CameraRequest()
    req.abandon()  # the user said stop before the event crossed to the GUI thread
    assert show_camera_panel(None, req) is None


def test_a_panel_that_fails_to_construct_still_settles_the_claimed_request(monkeypatch, _app):
    # A claimed-but-unsettled request would park the worker for the full capture budget.
    monkeypatch.setattr(camera_view, "_CAMERA", True)

    def _boom(self):
        raise RuntimeError("device enumeration exploded")

    monkeypatch.setattr(CameraPanel, "_start_camera", _boom)
    req = CameraRequest()
    assert show_camera_panel(None, req) is None
    assert req.wait(claim_timeout=1.0, timeout=1.0) is None
    assert "couldn't open" in req.error


def test_capture_encodes_the_latest_frame_and_fulfils_the_request(monkeypatch, _app):
    PIL = pytest.importorskip("PIL.Image")
    req = CameraRequest()
    assert req.claim()
    panel = _panel(monkeypatch, req, _app)
    panel._latest = _frame()
    panel._do_capture()
    data = req.wait(claim_timeout=1.0, timeout=1.0)
    assert data is not None
    decoded = PIL.open(io.BytesIO(data))  # real PNG bytes, straight from memory — no temp file
    assert decoded.size == (64, 48)


def test_closing_the_window_without_a_picture_fails_the_request(monkeypatch, _app):
    req = CameraRequest()
    assert req.claim()
    panel = _panel(monkeypatch, req, _app)
    panel.close()
    assert req.wait(claim_timeout=1.0, timeout=1.0) is None
    assert "closed the camera window" in req.error


def test_the_window_folds_itself_once_the_worker_abandons_the_request(monkeypatch, _app):
    req = CameraRequest()
    assert req.claim()
    panel = _panel(monkeypatch, req, _app)
    panel.show()
    req.abandon()
    panel._tick()
    assert panel._settled and not panel.isVisible()


def test_a_camera_that_never_delivers_a_frame_fails_after_the_grace_period(monkeypatch, _app):
    req = CameraRequest()
    assert req.claim()
    panel = _panel(monkeypatch, req, _app)
    panel._started_at = time.monotonic() - (camera_view.STARTUP_GRACE_S + 1.0)
    panel._tick()
    assert req.wait(claim_timeout=1.0, timeout=1.0) is None
    assert "never delivered" in req.error


def test_the_first_live_frame_arms_the_countdown_and_the_deadline_captures(monkeypatch, _app):
    req = CameraRequest()
    assert req.claim()
    panel = _panel(monkeypatch, req, _app)

    class _F:  # quacks like a QVideoFrame
        def toImage(self):
            return _frame()

    panel._on_frame(_F())
    assert panel._deadline is not None and panel._latest is not None
    panel._deadline = time.monotonic() - 0.1  # countdown ran out
    panel._tick()
    assert req.wait(claim_timeout=1.0, timeout=1.0) is not None


def test_more_time_rearms_the_countdown_instead_of_capturing(monkeypatch, _app):
    # Hands full, object not positioned yet: More time resets to a FRESH window (never stacks).
    req = CameraRequest()
    assert req.claim()
    panel = _panel(monkeypatch, req, _app)
    panel._latest = _frame()
    panel._deadline = time.monotonic() + 0.5  # nearly out
    panel._more_time()
    remaining = panel._remaining()
    assert remaining is not None and remaining > 5
    panel._tick()
    assert not panel._settled  # still previewing, nothing captured


def test_cancel_and_esc_route_through_the_close_funnel_that_stops_the_camera(monkeypatch, _app):
    # QDialog.done() with WA_DeleteOnClose deletes WITHOUT a QCloseEvent on this PyQt6 build, so
    # reject() must route through close() — otherwise the webcam is left racing its destructor.
    req = CameraRequest()
    assert req.claim()
    panel = _panel(monkeypatch, req, _app)
    panel.reject()
    assert not panel._timer.isActive()  # closeEvent ran: timer stopped, camera stopped
    assert req.wait(claim_timeout=1.0, timeout=1.0) is None
    assert "closed the camera window" in req.error


def test_the_models_window_line_renders_as_plain_text_never_markup(monkeypatch, _app):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QLabel

    req = CameraRequest(prompt="Hold the <b>label</b> up close")
    assert req.claim()
    panel = _panel(monkeypatch, req, _app)
    labels = [w for w in panel.findChildren(QLabel)
              if w.text() == "Hold the <b>label</b> up close"]
    assert labels and all(w.textFormat() == Qt.TextFormat.PlainText for w in labels)
