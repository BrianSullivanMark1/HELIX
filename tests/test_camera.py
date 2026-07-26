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


def test_the_capture_ceiling_reaps_a_forgotten_window():
    # With no countdown, this worker-side ceiling is the ONLY thing bounding a window someone
    # walked away from: the wait abandons, the window's next tick folds it, HELIX says no picture.
    req = CameraRequest()
    assert req.claim()
    start = time.monotonic()
    assert req.wait(claim_timeout=1.0, timeout=0.2) is None
    assert time.monotonic() - start < 2.0
    assert req.abandoned and "left open" in req.error


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
    monkeypatch.setattr(CameraPanel, "_start_camera", lambda self, device=None: True)
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


def test_a_live_frame_waits_for_the_users_word_and_voice_capture_returns_it(monkeypatch, _app):
    # No countdown, no auto-capture: frames flow, the window WAITS, and 'take the picture'
    # (via the voice hook) is what takes the shot.
    req = CameraRequest()
    assert req.claim()
    panel = _panel(monkeypatch, req, _app)

    class _F:  # quacks like a QVideoFrame
        def toImage(self):
            return _frame()

    panel._on_frame(_F())
    assert panel._latest is not None
    panel._tick()
    assert not panel._settled  # frames alone never capture — the user's word does
    panel.voice_capture()
    assert req.wait(claim_timeout=1.0, timeout=1.0) is not None


def test_the_word_before_the_first_frame_waits_and_captures_on_arrival(monkeypatch, _app):
    # 'Take the picture' during sensor warm-up (or right after a picker switch) must not fail the
    # whole look — it waits, and the first frame that lands IS the picture.
    req = CameraRequest()
    assert req.claim()
    panel = _panel(monkeypatch, req, _app)
    panel.voice_capture()
    assert not panel._settled and panel._pending_capture

    class _F:
        def toImage(self):
            return _frame()

    panel._on_frame(_F())
    assert req.wait(claim_timeout=1.0, timeout=1.0) is not None


def test_the_hint_only_promises_listening_when_voice_is_ready(monkeypatch, _app):
    from PyQt6.QtWidgets import QLabel

    monkeypatch.setattr(CameraPanel, "_start_camera", lambda self, device=None: True)
    ready_req = CameraRequest()
    assert ready_req.claim()
    ready = CameraPanel(None, ready_req, voice_ready=True)
    assert any("I'm listening" in w.text() for w in ready.findChildren(QLabel))
    quiet_req = CameraRequest()
    assert quiet_req.claim()
    quiet = CameraPanel(None, quiet_req, voice_ready=False)
    assert not any("listening" in w.text() for w in quiet.findChildren(QLabel))


def test_voice_cancel_closes_without_a_picture(monkeypatch, _app):
    req = CameraRequest()
    assert req.claim()
    panel = _panel(monkeypatch, req, _app)
    panel._latest = _frame()
    panel.voice_cancel()
    assert req.wait(claim_timeout=1.0, timeout=1.0) is None
    assert "closed the camera window" in req.error


class _Dev:
    def __init__(self, name: str, raw: bytes):
        self._name, self._raw = name, raw

    def description(self) -> str:
        return self._name

    def id(self) -> bytes:
        return self._raw


class _CamSettings:
    def __init__(self):
        self.d = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


def test_the_picker_lists_cameras_switches_live_and_remembers(monkeypatch, _app):
    devs = [_Dev("Integrated Camera", b"cam-a"), _Dev("USB Camera", b"cam-b")]
    monkeypatch.setattr(camera_view, "_video_inputs", lambda: devs)
    started = []

    def _fake_start(self, device=None):
        device = device or devs[0]
        started.append(device)
        self._active_device_id = camera_view._device_id(device)
        return True

    monkeypatch.setattr(CameraPanel, "_start_camera", _fake_start)
    settings = _CamSettings()
    req = CameraRequest()
    assert req.claim()
    panel = CameraPanel(None, req, settings=settings)
    names = [panel._combo.itemText(i) for i in range(panel._combo.count())]
    assert names == ["Integrated Camera", "USB Camera"]
    assert panel._combo.currentIndex() == 0
    panel._combo.setCurrentIndex(1)  # the user picks the hooked-up camera
    assert started[-1].description() == "USB Camera"
    # The choice is REMEMBERED — next window (and _resolve_camera) starts on this camera.
    assert settings.d[camera_view.CAMERA_DEVICE_SETTING] == camera_view._device_id(devs[1])
    assert not panel._settled  # switching never settles the request


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


# ---- the camera voice grammar + the voice layer's camera session ---------------------------

from helix.ui.voice import VoiceController, camera_command  # noqa: E402


def test_camera_grammar_hears_the_take_picture_variants():
    for say in ("take the picture", "take a picture", "take picture", "take the photo",
                "okay take the picture now", "hey helix, take the picture", "snap it",
                "capture", "Take the photo, please.", "cheese"):
        assert camera_command(say) == "capture", say


def test_camera_grammar_hears_the_cancel_variants():
    for say in ("cancel", "cancel that", "never mind", "close the camera", "stop",
                "no picture", "forget it"):
        assert camera_command(say) == "cancel", say


def test_camera_grammar_ignores_room_chatter():
    # Whole-utterance matches only: a sentence merely CONTAINING a capture word does nothing,
    # so conversation near an open camera window can never snap a frame.
    for say in ("", "that's a nice camera", "we should take it easy",
                "I captured the flag in that game yesterday",
                "picture this, we're on a beach", "can you take the trash out",
                "let me hold it a little closer"):
        assert camera_command(say) is None, say


def test_camera_grammar_takes_the_name_anywhere_but_never_a_mention():
    from helix.ui.voice import build_wake_re

    assert camera_command("take the picture, helix") == "capture"  # trailing name works too
    custom = build_wake_re("friday")
    assert camera_command("friday, take the picture", custom) == "capture"
    assert camera_command("take the picture, friday", custom) == "capture"
    # A sentence MENTIONING the name plus a grammar phrase is talk ABOUT it, not a command.
    assert camera_command("I told helix take the picture yesterday") is None
    assert camera_command("I told friday take the picture yesterday", custom) is None


def test_the_spoken_camera_lines_never_match_the_grammar():
    # The camera ears are LIVE while these play (narrate doesn't gate the mic) — a spoken cue or
    # tool label that matched the grammar would capture itself.
    assert camera_command("Camera's open — ready when you are.") is None
    assert camera_command(friendly_tool_label("view_camera")) is None


class _VStt:
    def available(self):
        return True

    def ready(self):
        return True

    def transcribe(self, _path):
        return ""


class _VTts:
    def available(self):
        return True

    def speak(self, text, allow_fallback=True):
        pass

    def stop(self):
        pass


class _VSettings:
    def __init__(self):
        self.d = {"voice_input_on": True}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


class _Listener:
    def __init__(self):
        self.active = None

    def set_active(self, on: bool) -> None:
        self.active = bool(on)


def _voice() -> VoiceController:
    return VoiceController(_VStt(), _VTts(), _VSettings())


def test_camera_session_routes_the_grammar_to_the_window_and_nothing_else(_app):
    vc = _voice()
    hits: list[str] = []
    vc.set_camera_session(lambda: hits.append("capture"), lambda: hits.append("cancel"))
    vc._on_camera_text("take the picture", media=False)
    vc._on_camera_text("what a lovely day", media=False)  # chatter — ignored, no turn, no session
    vc._on_camera_text("cancel", media=False)
    assert hits == ["capture", "cancel"]
    assert not vc._session  # camera words never open a conversation session
    vc.clear_camera_session()
    vc._on_camera_text("take the picture", media=False)  # window gone — dropped
    assert hits == ["capture", "cancel"]


def test_camera_words_over_machine_playback_need_direct_address(_app):
    # The playback gate, camera edition: a video saying 'take the picture' is never the user.
    vc = _voice()
    hits: list[str] = []
    vc.set_camera_session(lambda: hits.append("capture"), lambda: hits.append("cancel"))
    vc._on_camera_text("take the picture", media=True)
    assert hits == []
    vc._on_camera_text("helix, take the picture", media=True)  # the name leading = addressed
    assert hits == ["capture"]


def test_camera_session_opens_the_ears_and_only_the_session_closes_them(_app):
    vc = _voice()
    listener = _Listener()
    vc._listener = listener
    vc._state = "thinking"
    vc._apply_listen_gate()
    assert listener.active is False  # the normal focus shield: deaf while a turn runs
    vc.set_camera_session(lambda: None, lambda: None)
    assert listener.active is True   # the ONE exception: the camera window is up
    vc._state = "idle"               # an announcement clobbered the state mid-park —
    vc._apply_listen_gate()
    assert listener.active is True   # — the ears are keyed to the SESSION and survive it
    vc._state = "thinking"
    vc._muted = True
    vc._apply_listen_gate()
    assert listener.active is False  # sleep means sleep — even for the camera
    vc._muted = False
    vc._state = "speaking"
    vc._apply_listen_gate()
    assert listener.active is False  # never while HELIX is audibly speaking (echo)
    vc._state = "thinking"
    vc._working = True
    vc._apply_listen_gate()
    assert listener.active is False  # never while a background build has the focus shield up
    vc._working = False
    vc.clear_camera_session()
    vc._apply_listen_gate()
    assert listener.active is False  # window gone — the focus shield is whole again


class _ScriptStt:
    def __init__(self, text: str):
        self._text = text

    def available(self):
        return True

    def ready(self):
        return True

    def transcribe(self, _path):
        return self._text


def test_camera_words_survive_an_announcement_and_never_become_a_model_turn(_app):
    # A reminder spoken mid-park drives the state thinking→speaking→idle. With the window still
    # open, 'take the picture' must STILL land on the window — and must never leak into the normal
    # wake/session grammar as a queued follow-up turn (the focus-shield invariant).
    vc = VoiceController(_ScriptStt("take the picture"), _VTts(), _VSettings())
    vc._run = lambda fn, on_done: on_done(fn(lambda _s: None))  # synchronous worker, house style
    hits: list[str] = []
    heard: list[str] = []
    vc.recognized.connect(heard.append)
    vc.set_camera_session(lambda: hits.append("capture"), lambda: hits.append("cancel"))
    vc._state = "idle"     # what _speak_done leaves behind after the announcement
    vc._start_session()    # the 45s session from 'HELIX, what is this?' is still open
    vc._on_utterance(b"\x00\x00" * 800)
    assert hits == ["capture"]
    assert heard == []     # the words went to the window, not to a model turn
    vc._session_timer.stop()


def test_a_stale_transcription_from_a_replaced_window_acts_on_nobody(_app):
    # STT finishing after its window was replaced must not capture the NEW window.
    vc = _voice()
    old_hits: list[int] = []
    new_hits: list[int] = []
    vc.set_camera_session(lambda: old_hits.append(1), lambda: old_hits.append(-1))
    stale = vc._camera_session
    vc.set_camera_session(lambda: new_hits.append(1), lambda: new_hits.append(-1))
    vc._on_camera_text("take the picture", media=False, session=stale)
    assert old_hits == [] and new_hits == []
    vc._on_camera_text("take the picture", media=False, session=vc._camera_session)
    assert new_hits == [1]


def test_a_registered_voice_keeps_camera_words_over_machine_playback(_app):
    vc = _voice()
    hits: list[str] = []
    vc.set_camera_session(lambda: hits.append("capture"), lambda: hits.append("cancel"))
    vc._known_voice = lambda: True  # the voice-print matched a registered speaker
    vc._on_camera_text("take the picture", media=True)
    assert hits == ["capture"]
