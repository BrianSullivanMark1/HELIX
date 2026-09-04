"""The camera PANEL — the persistent live camera beside the conversation, and everything the model
can do through it: instant looks, asked looks, clips, the reference grid, AR callouts, hologram
projection, and the panel commands.

Three layers, each driven without a browser or hardware:
  - the holders (CameraRequest with frames/hold/grid; CameraCommand's settle/wait),
  - the tools (view_camera's richer args; annotate_camera / project_hologram / camera_panel relay
    the panel's one-line answers and are fenced from autonomous runs),
  - the web shell's session model (live panel → capture event with an id; hold → ask banner and
    voice grammar; no panel → raise one; frames settle looks or become turns; the watchdog; the
    AR command replies; served transcript pictures), plus the HTTP routes as plain ASGI.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from helix.adapters.signal_bus import SignalBus
from helix.api.server import EventHub, build_app
from helix.api.shell import ShellSession
from helix.domain.events import CameraCommandRequested, CameraRequested
from helix.domain.models import BuildKind
from helix.domain.vocabulary import friendly_tool_label
from helix.ports.llm import ToolOutput
from helix.services import camera, images
from helix.services.camera import CameraCommand, CameraRequest
from helix.services.conversation import BUILD_TOOLS, SIGHT_TOOLS
from helix.services.prompts import CONSOLE_SYSTEM
from helix.services.tools import ToolRegistry

PIL = pytest.importorskip("PIL.Image")


def _png(size=(640, 480), color=(30, 60, 90)) -> bytes:
    im = PIL.new("RGB", size, color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# ---- the holders ----------------------------------------------------------------------------

def test_a_prompt_means_wait_unless_the_model_says_otherwise():
    assert CameraRequest("Turn it over").hold is True       # asked to present something → wait
    assert CameraRequest("").hold is False                  # a bare look grabs what's there
    assert CameraRequest("Turn it over", hold=False).hold is False
    assert CameraRequest("", hold=True).hold is True


def test_clip_sizes_are_clamped_and_a_still_has_no_span():
    r = CameraRequest(frames=50, seconds=99)
    assert r.frames == camera.MAX_FRAMES and r.seconds == camera.MAX_CLIP_SECONDS
    assert CameraRequest(frames=4).seconds == 2.0            # half a second a frame by default
    assert CameraRequest(frames="nope").frames == 1
    assert CameraRequest(frames=1, seconds=10).seconds == 0.0


def test_a_clip_fulfils_all_its_frames_in_order_and_wait_returns_the_first():
    r = CameraRequest(frames=3)
    r.claim()
    r.fulfil_frames([b"one", b"", b"two", b"three"])       # the empty one drops out
    assert r.wait(claim_timeout=0.1, timeout=0.5) == b"one"
    assert r.frames_data == (b"one", b"two", b"three")
    assert r.settled


def test_a_command_settles_once_and_silence_reads_as_none():
    cmd = CameraCommand("overlay", {"items": []})
    assert cmd.wait(timeout=0.05) is None
    cmd.settle("Drawn 2 callouts.")
    cmd.settle("ignored")
    assert cmd.wait(timeout=0.05) == "Drawn 2 callouts." and cmd.settled


# ---- the reference grid ---------------------------------------------------------------------

def test_the_grid_is_burned_onto_the_picture_and_the_legend_matches_it():
    plain = _png((800, 600))
    gridded = images.with_grid(plain)
    a = PIL.open(io.BytesIO(plain)).convert("RGB")
    b = PIL.open(io.BytesIO(gridded)).convert("RGB")
    assert b.size == a.size
    # A grid line sits at x = 80 (one tenth of 800): that column changed; the cell body didn't
    # (y = 330 sits inside row 6, clear of the horizontal line at 300).
    assert b.getpixel((80, 330)) != a.getpixel((80, 330))
    assert b.getpixel((40, 330)) == a.getpixel((40, 330))
    assert "A–J" in images.GRID_LEGEND and "1–10" in images.GRID_LEGEND
    assert "C4 spans x 0.20–0.30, y 0.30–0.40" in images.GRID_LEGEND


def test_the_grid_never_breaks_a_look():
    assert images.with_grid(b"not a picture") == b"not a picture"
    tiny = _png((20, 20))
    assert images.with_grid(tiny) == tiny  # too small to label — handed back untouched


def test_encode_frames_caps_grids_and_drops_garbage():
    blocks = images.encode_frames([_png(), b"junk", _png()], grid=True)
    assert len(blocks) == 2
    assert all(b.media_type in ("image/png", "image/jpeg") for b in blocks)


# ---- the tools ------------------------------------------------------------------------------

class _Bus:
    def __init__(self):
        self.published = []

    def publish(self, ev):
        self.published.append(ev)


class _ClipBus(_Bus):
    """A panel on the other side that answers a look with a three-frame clip."""

    def publish(self, ev):
        super().publish(ev)
        if isinstance(ev, CameraRequested):
            assert ev.request.claim()
            ev.request.fulfil_frames([_png(color=(10, 10, 10)), _png(color=(90, 90, 90)),
                                      _png(color=(200, 200, 200))])


class _AnsweringBus(_Bus):
    """A panel that answers AR commands with a line."""

    def __init__(self, reply="Drawn 2 callouts on the camera view."):
        super().__init__()
        self.reply = reply

    def publish(self, ev):
        super().publish(ev)
        if isinstance(ev, CameraCommandRequested):
            ev.request.settle(self.reply)


def _registry(bus=None, builds=None) -> ToolRegistry:
    return ToolRegistry(forge=None, builds=builds, bus=bus)


def test_view_camera_passes_the_richer_args_into_the_request():
    bus = _ClipBus()
    out = _registry(bus).dispatch("view_camera", {"prompt": "Show me every side", "wait": False,
                                                   "frames": 3, "seconds": 2, "grid": True})
    req = bus.published[0].request
    assert req.prompt == "Show me every side" and req.hold is False
    assert req.frames == 3 and req.seconds == 2.0 and req.grid is True
    assert isinstance(out, ToolOutput) and len(out.images) == 3
    assert "3 frames" in out.text and "time order" in out.text
    assert images.GRID_LEGEND in out.text           # the model is told how to read the grid
    decoded = PIL.open(io.BytesIO(base64.b64decode(out.images[0].data)))
    assert max(decoded.size) <= images.MAX_EDGE


def test_a_still_look_says_so_without_a_legend():
    class _StillBus(_Bus):
        def publish(self, ev):
            super().publish(ev)
            assert ev.request.claim()
            ev.request.fulfil(_png())

    out = _registry(_StillBus()).dispatch("view_camera", {})
    assert isinstance(out, ToolOutput) and len(out.images) == 1
    assert "camera sees" in out.text.lower() and "grid" not in out.text.lower()


def test_annotate_camera_sends_the_callouts_and_relays_the_panels_answer():
    bus = _AnsweringBus()
    out = _registry(bus).dispatch("annotate_camera", {
        "title": "Wiring", "items": [{"kind": "pin", "x": 0.2, "y": 0.3, "text": "GPIO4"},
                                     {"kind": "arrow", "x": 0.1, "y": 0.1, "x2": 0.5, "y2": 0.5}],
    })
    assert out == "Drawn 2 callouts on the camera view."
    cmd = bus.published[0].request
    assert cmd.command == "overlay" and cmd.payload["title"] == "Wiring"
    assert [i["kind"] for i in cmd.payload["items"]] == ["pin", "arrow"] and cmd.payload["clear"]


def test_annotate_camera_refuses_an_empty_drawing_without_bothering_the_panel():
    bus = _AnsweringBus()
    out = _registry(bus).dispatch("annotate_camera", {"items": []})
    assert "at least one" in out and not bus.published


def test_an_unanswered_command_reads_as_no_panel(monkeypatch):
    monkeypatch.setattr(camera, "COMMAND_TIMEOUT_S", 0.05)
    out = _registry(_Bus()).dispatch("camera_panel", {"action": "open"})
    assert "isn't available" in out


def test_project_hologram_resolves_a_model_build_by_name():
    class _Builds:
        def list(self):
            return [SimpleNamespace(slug="iron-eye", name="Iron Eye", build_kind=BuildKind.MODEL),
                    SimpleNamespace(slug="tip-calc", name="Tip Calc", build_kind=BuildKind.APP)]

    bus = _AnsweringBus("Projected Iron Eye onto the camera view.")
    out = _registry(bus, builds=_Builds()).dispatch("project_hologram", {"name": "iron eye"})
    assert out.startswith("Projected")
    assert bus.published[0].request.payload == {"slug": "iron-eye", "name": "Iron Eye"}
    # an app is not a hologram; a partial name still finds the hologram
    miss = _registry(_AnsweringBus(), builds=_Builds()).dispatch("project_hologram", {"name": "tip calc"})
    assert "don't have a hologram" in miss and "Iron Eye" in miss
    part = _AnsweringBus("ok")
    _registry(part, builds=_Builds()).dispatch("project_hologram", {"name": "eye"})
    assert part.published[0].request.payload["slug"] == "iron-eye"


def test_camera_panel_validates_its_action():
    bus = _AnsweringBus("Closed the camera panel.")
    assert _registry(bus).dispatch("camera_panel", {"action": "close"}) == "Closed the camera panel."
    assert "one of" in _registry(bus).dispatch("camera_panel", {"action": "explode"})


def test_the_ar_tools_ride_with_the_camera_advertised_labelled_and_fenced():
    names = {s.name for s in _registry(_Bus()).specs()}
    assert {"view_camera", "annotate_camera", "project_hologram", "camera_panel"} <= names
    headless = {s.name for s in _registry(bus=None).specs()}
    assert not ({"annotate_camera", "project_hologram", "camera_panel"} & headless)
    for name in ("annotate_camera", "project_hologram", "camera_panel"):
        assert name in BUILD_TOOLS                    # never from an unattended watcher
        assert "_" not in friendly_tool_label(name)
    assert "view_camera" in SIGHT_TOOLS
    for word in ("annotate_camera", "project_hologram", "camera_panel", "grid=true", "CLIP"):
        assert word in CONSOLE_SYSTEM                 # the persona teaches the whole faculty


# ---- the shell's session model --------------------------------------------------------------

class _Settings:
    def __init__(self, **kv):
        self._d = dict(kv)

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class _Conversation:
    def __init__(self):
        self.turns = []

    def recent_messages(self, n):
        return ["x"]

    def run_turn(self, text, **kw):
        self.turns.append((text, kw))
        return "Done."


class _Voice:
    """Just the camera seam of WebVoice: whether the grammar is armed, and the two callbacks."""

    def __init__(self):
        self.session = None

    def enabled(self):
        return True

    def can_listen(self):
        return True

    def is_muted(self):
        return False

    def camera_ears_live(self):
        return True

    def set_camera_session(self, on_capture, on_cancel):
        self.session = (on_capture, on_cancel)

    def clear_camera_session(self):
        self.session = None

    def narrate(self, *a, **k):
        pass

    def speak(self, *a, **k):
        pass

    def shutdown(self):
        pass

    # the rest of what the shell touches on a voice, harmlessly
    def __getattr__(self, name):
        return lambda *a, **k: None


class _Builds:
    def __init__(self, root: Path):
        self.root = root

    def workspace(self, slug):
        return self.root / slug

    def list(self):
        return []


class _Container:
    def __init__(self, root: Path):
        self.settings = _Settings(claude_api_key="sk-test", wake_word="Helix")
        self.bus = SignalBus()
        self.conversation = _Conversation()
        self.voice_id = None
        self.builds = _Builds(root)

    class _Stub:
        def __getattr__(self, name):
            return lambda *a, **k: None

    def __getattr__(self, name):
        return _Container._Stub()


@pytest.fixture
def rig(tmp_path):
    container = _Container(tmp_path)
    events: list[dict] = []
    sh = ShellSession(container, events.append, voice=None)
    yield container, events, sh
    sh.shutdown()


@pytest.fixture
def voiced(tmp_path):
    container = _Container(tmp_path)
    events: list[dict] = []
    voice = _Voice()
    sh = ShellSession(container, events.append, voice=voice)
    yield container, events, sh, voice
    sh.shutdown()


def _ev(events, t):
    return [e for e in events if e.get("t") == t]


def _look(sh, **kw) -> CameraRequest:
    """Publish a look the way view_camera does (claim happens in the shell's handler)."""
    req = CameraRequest(**kw)
    sh.c.bus.publish(CameraRequested(request=req))
    return req


def test_a_live_panel_answers_a_bare_look_instantly_and_stays_open(rig):
    _c, events, sh = rig
    cid = sh.camera_open()["id"]
    assert sh.camera_live(cid, True, "USB Camera") is True
    req = _look(sh)                                   # no prompt → no wait
    caps = _ev(events, "camera.capture")
    assert len(caps) == 1 and caps[0]["id"] == cid and caps[0]["rid"] == sh._camera["rid"]
    assert caps[0]["frames"] == 1
    assert not _ev(events, "camera.ask")             # nobody was asked to do anything
    assert sh.camera_frames(cid, [_png()], rid=caps[0]["rid"], frame_id="f-1") is True
    assert req.settled and req.frames_data and req.wait(timeout=0.1) is not None
    assert sh._camera is not None and sh._camera["request"] is None  # panel stays, look cleared
    assert sh._last_frame_id == "f-1"                 # callouts will anchor here
    assert _ev(events, "camera.ask.clear")
    trace = [e for e in _ev(events, "msg") if e["role"] == "system"]
    assert trace and "Looked through the camera" in trace[-1]["text"]
    assert trace[-1]["images"] and trace[-1]["images"][0].startswith("/api/images/")


def test_a_clip_look_asks_the_panel_for_that_many_frames(rig):
    _c, events, sh = rig
    cid = sh.camera_open()["id"]
    sh.camera_live(cid, True)
    req = _look(sh, frames=4, seconds=3)
    cap = _ev(events, "camera.capture")[-1]
    assert cap["frames"] == 4 and cap["seconds"] == 3.0
    sh.camera_frames(cid, [_png()] * 4, rid=cap["rid"])
    assert len(req.frames_data) == 4
    trace = [e for e in _ev(events, "msg") if e["role"] == "system"]
    assert "a clip" in trace[-1]["text"]


def test_a_held_look_on_a_live_panel_arms_the_grammar_and_waits(voiced):
    _c, events, sh, voice = voiced
    cid = sh.camera_open()["id"]
    assert voice.session is None                      # a manual panel leaves the mic to the model
    sh.camera_live(cid, True)
    req = _look(sh, prompt="Turn it over")           # a prompt → wait for their word
    assert not _ev(events, "camera.capture")
    ask = _ev(events, "camera.ask")[-1]
    assert ask["prompt"] == "Turn it over" and ask["hold"] is True and ask["ears"] is True
    assert voice.session is not None
    voice.session[0]()                                # "take the picture"
    cap = _ev(events, "camera.capture")[-1]
    assert cap["rid"] == ask["rid"]
    sh.camera_frames(cid, [_png()], rid=cap["rid"])
    assert req.settled and voice.session is None     # grammar let go once the look settled
    assert sh._camera is not None                     # …and the panel is still there


def test_a_spoken_cancel_on_a_user_opened_panel_settles_the_look_but_keeps_the_panel(voiced):
    _c, events, sh, voice = voiced
    cid = sh.camera_open()["id"]
    sh.camera_live(cid, True)
    req = _look(sh, prompt="Show me the back")
    voice.session[1]()                                # "cancel"
    assert req.settled and "cancelled" in req.error
    assert sh._camera is not None and sh._camera["id"] == cid
    assert not _ev(events, "camera.close")


def test_a_spoken_cancel_folds_a_panel_that_was_raised_for_the_look(voiced):
    _c, events, sh, voice = voiced
    req = _look(sh, prompt="Hold it up")             # no panel → one is raised for this look
    cam = _ev(events, "camera")[-1]
    assert cam["manual"] is False and cam["ask"]["prompt"] == "Hold it up" and cam["ask"]["hold"]
    assert cam["wake"] == "Helix"
    voice.session[1]()
    assert req.settled and sh._camera is None and _ev(events, "camera.close")


def test_a_look_with_no_panel_raises_one_that_grabs_on_its_own(rig):
    _c, events, sh = rig
    req = _look(sh)                                   # bare look, nothing open
    cam = _ev(events, "camera")[-1]
    assert cam["ask"]["hold"] is False and cam["ask"]["rid"]
    # the panel comes up, streams, and posts what it sees — no click needed
    sh.camera_live(cam["id"], True)
    assert sh.camera_frames(cam["id"], [_png()], rid=cam["ask"]["rid"]) is True
    assert req.settled and sh._camera is not None


def test_a_newer_look_supersedes_one_still_parked(rig):
    _c, events, sh = rig
    cid = sh.camera_open()["id"]
    sh.camera_live(cid, True)
    first = _look(sh, prompt="Show me the top")
    second = _look(sh, prompt="Show me the bottom")
    assert first.settled and "newer" in first.error
    assert sh._camera["request"] is second


def test_the_watchdog_raises_a_fresh_panel_when_a_live_one_never_answers(rig, monkeypatch):
    _c, events, sh = rig
    monkeypatch.setattr(ShellSession, "_CAPTURE_WATCHDOG_S", 0.05)
    cid = sh.camera_open()["id"]
    sh.camera_live(cid, True)                         # …but the page is actually gone
    req = _look(sh)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and len(_ev(events, "camera")) < 2:
        time.sleep(0.01)
    cams = _ev(events, "camera")
    assert len(cams) == 2 and cams[-1]["id"] != cid  # a NEW panel, for the same look
    assert sh._camera["request"] is req and not req.settled
    # the same bare look rides on the new panel: once that panel streams it grabs on its own
    assert cams[-1]["ask"]["rid"] == sh._camera["rid"] and cams[-1]["ask"]["hold"] is False


def test_a_stale_capture_id_is_dropped_not_turned(rig, monkeypatch):
    _c, _events, sh = rig
    monkeypatch.setattr(sh, "_start_turn", lambda *a, **k: pytest.fail("a stale capture became a turn"))
    cid = sh.camera_open()["id"]
    sh.camera_live(cid, True)
    assert sh.camera_frames(cid, [_png()], rid="long-gone") is False


def test_a_shot_with_a_question_asks_that_question(rig, monkeypatch):
    _c, events, sh = rig
    started: list = []
    monkeypatch.setattr(sh, "_start_turn", lambda *a, **k: started.append(a))
    cid = sh.camera_open()["id"]
    assert sh.camera_frames(cid, [_png()], caption="  which pin is   3V3? ", frame_id="f-9")
    prompt = started[0][0]
    assert prompt.startswith("which pin is 3V3?") and "Context:" in prompt
    assert "annotate_camera" in prompt                # the model is reminded it can draw the answer
    assert sh._last_frame_id == "f-9"
    user = [e for e in _ev(events, "msg") if e["role"] == "user"][-1]
    assert user["text"] == "which pin is 3V3?" and user["images"][0].startswith("/api/images/")


def test_a_recorded_clip_gets_the_clip_brief_with_its_count_and_span(rig, monkeypatch):
    _c, events, sh = rig
    started: list = []
    monkeypatch.setattr(sh, "_start_turn", lambda *a, **k: started.append(a))
    cid = sh.camera_open()["id"]
    sh.camera_frames(cid, [_png()] * 5, mode="clip", seconds=4.0)
    prompt, _voice, paths, _spk = started[0]
    assert "5 frames" in prompt and "about 4 seconds" in prompt and len(paths) == 5
    user = [e for e in _ev(events, "msg") if e["role"] == "user"][-1]
    assert user["text"].startswith("🎞") and len(user["images"]) == 4  # thumbnails capped at four


def test_esc_on_the_banner_settles_the_look_and_keeps_the_panel(rig):
    _c, events, sh = rig
    cid = sh.camera_open()["id"]
    sh.camera_live(cid, True)
    req = _look(sh, prompt="Hold it still")
    sh.camera_cancel(cid, keep_open=True)
    assert req.settled and sh._camera is not None and not _ev(events, "camera.close")
    sh.camera_cancel(cid)
    assert sh._camera is None and _ev(events, "camera.close")


def _command(sh, command, payload) -> str | None:
    cmd = CameraCommand(command, payload)
    sh.c.bus.publish(CameraCommandRequested(request=cmd))
    return cmd.wait(timeout=0.2)


def test_callouts_reach_the_panel_cleaned_and_anchored_to_the_last_frame(rig):
    _c, events, sh = rig
    assert "isn't open" in _command(sh, "overlay", {"items": [{"kind": "box"}]})
    cid = sh.camera_open()["id"]
    sh.camera_live(cid, True)
    sh._last_frame_id = "f-42"
    reply = _command(sh, "overlay", {"title": "Plan", "clear": False, "items": [
        {"kind": "box", "x": 0.1, "y": "0.2", "w": 9, "h": 0.1, "text": "  ESP32  chip ", "color": "amber"},
        {"kind": "wire", "points": [[0, 0], [0.5, 0.5], ["x", 1]]},
        {"kind": "nope", "x": 0.5},
        "garbage",
    ]})
    assert reply == "Drawn 2 callouts on the camera view; they track the object as it moves."
    ov = _ev(events, "camera.overlay")[-1]
    assert ov["frame"] == "f-42" and ov["title"] == "Plan" and ov["clear"] is False
    box, wire = ov["items"]
    assert box == {"kind": "box", "x": 0.1, "y": 0.2, "w": 1.5, "h": 0.1, "text": "ESP32 chip",
                   "color": "amber"}
    assert wire == {"kind": "wire", "points": [[0, 0], [0.5, 0.5]]}
    assert "drawable" in _command(sh, "overlay", {"items": [{"kind": "nope"}]})


def test_a_hologram_projects_only_once_it_has_a_mesh(rig, tmp_path):
    _c, events, sh = rig
    cid = sh.camera_open()["id"]
    reply = _command(sh, "hologram", {"slug": "iron-eye", "name": "Iron Eye"})
    assert "no mesh" in reply and not _ev(events, "camera.hologram")
    mesh = tmp_path / "iron-eye" / "assets" / "model.stl"
    mesh.parent.mkdir(parents=True)
    mesh.write_bytes(b"solid x\nendsolid x\n")
    reply = _command(sh, "hologram", {"slug": "iron-eye", "name": "Iron Eye"})
    assert reply.startswith("Projected Iron Eye")
    ev = _ev(events, "camera.hologram")[-1]
    assert ev == {"t": "camera.hologram", "id": cid, "slug": "iron-eye", "name": "Iron Eye",
                  "stl": "/builds/iron-eye/assets/model.stl"}
    assert "off" in _command(sh, "hologram", {"remove": True})
    assert _ev(events, "camera.hologram")[-1]["remove"] is True


def test_panel_commands_open_close_layout_and_clear(rig):
    _c, events, sh = rig
    assert _command(sh, "panel", {"action": "close"}) == "The camera panel isn't open."
    assert _command(sh, "panel", {"action": "open"}).startswith("Camera panel's open")
    assert sh._camera is not None and _ev(events, "camera")
    assert _command(sh, "panel", {"action": "open"}) == "The camera panel is already open."
    assert "full-screen" in _command(sh, "panel", {"action": "expand"})
    assert _ev(events, "camera.layout")[-1]["layout"] == "full"
    assert "docked" in _command(sh, "panel", {"action": "dock"})
    assert "Cleared" in _command(sh, "panel", {"action": "clear"})
    assert _ev(events, "camera.overlay")[-1]["clear"] is True and _ev(events, "camera.overlay")[-1]["items"] == []
    assert _command(sh, "panel", {"action": "close"}) == "Closed the camera panel."
    assert sh._camera is None and _ev(events, "camera.close")


def test_the_snapshot_re_raises_an_open_panel_for_a_reloaded_page(rig):
    _c, _events, sh = rig
    assert sh.snapshot()["camera"] is None
    cid = sh.camera_open()["id"]
    sh.camera_live(cid, True)
    req = _look(sh, prompt="Show me the label")
    snap = sh.snapshot()["camera"]
    assert snap["id"] == cid and snap["ask"]["prompt"] == "Show me the label"
    assert snap["ask"]["rid"] == sh._camera["rid"] and not req.settled


def test_transcript_pictures_are_served_by_id_and_retire_with_a_cap(rig, tmp_path):
    _c, events, sh = rig
    pic = tmp_path / "helix-cam-test.png"
    pic.write_bytes(_png())
    sh._bubble("user", "📷 (photo)", images=[str(pic)])
    url = _ev(events, "msg")[-1]["images"][0]
    iid = url.rsplit("/", 1)[-1]
    assert sh.served_image(iid) == pic
    assert sh.served_image("nope") is None
    assert sh._serve_image(url) == url                # already a URL: passes through
    for _ in range(ShellSession._IMAGES_KEPT + 5):    # the oldest retire — and their temp file goes
        sh._serve_image(str(tmp_path / "helix-cam-other.png"))
    assert sh.served_image(iid) is None and not pic.exists()


def test_an_attached_live_view_anchors_the_next_callouts(rig, monkeypatch):
    _c, _events, sh = rig
    monkeypatch.setattr(sh, "_start_turn", lambda *a, **k: None)
    row = sh.add_attachment("view.png", _png(), frame_id="f-77")
    sh.submit("what's this pin?", attachment_ids=[row["id"]])
    assert sh._last_frame_id == "f-77" and not sh._attachment_frames


# ---- the routes, as plain ASGI --------------------------------------------------------------

class _RouteShell:
    def __init__(self):
        self.calls = []
        self.images = {}

    def snapshot(self):
        return {"t": "snapshot"}

    def camera_open(self):
        return {"ok": True, "id": "cam1"}

    def camera_live(self, cam_id, on, label=""):
        self.calls.append(("live", cam_id, on, label))
        return True

    def camera_frames(self, cam_id, frames, **kw):
        self.calls.append(("frames", cam_id, frames, kw))
        return True

    def camera_frame(self, cam_id, png):
        self.calls.append(("frame", cam_id, png))
        return True

    def camera_cancel(self, cam_id, reason="", *, keep_open=False):
        self.calls.append(("cancel", cam_id, keep_open))

    def add_attachment(self, filename, data, *, frame_id=""):
        self.calls.append(("attach", filename, data, frame_id))
        return {"id": "a1", "name": filename, "image": True}

    def served_image(self, iid):
        return self.images.get(iid)


def _app(shell, root: Path | None = None):
    class _Builds:
        def __init__(self):
            self.rows = []

        def list(self):
            return self.rows

        def workspace(self, slug):
            return (root or Path("nowhere")) / slug

    builds = _Builds()
    container = SimpleNamespace(settings=_Settings(web_token="tok-test"),
                                paths=SimpleNamespace(builds="does-not-exist"), builds=builds)
    return build_app(container, shell, EventHub(), None), builds


def _call(app, method, path, body: bytes = b"", content_type: str | None = None):
    headers = [(b"host", b"127.0.0.1:8737"), (b"x-helix-token", b"tok-test")]
    if content_type:
        headers.append((b"content-type", content_type.encode()))
        headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
        "method": method, "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8737),
    }
    out = {"status": 0, "body": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = int(message["status"])
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], out["body"]


def _multipart(fields: dict, files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "helixboundary"
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for field, name, data in files:
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                      f'filename="{name}"\r\nContent-Type: image/png\r\n\r\n').encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def test_the_frames_route_carries_a_clip_with_its_metadata():
    shell = _RouteShell()
    app, _b = _app(shell)
    body, ctype = _multipart({"rid": "r1", "caption": "which pin?", "mode": "clip",
                              "seconds": "3.5", "frame": "f-1"},
                             [("frames", "a.png", b"AAA"), ("frames", "b.png", b"BBB")])
    status, raw = _call(app, "POST", "/api/camera/cam1/frames", body, ctype)
    assert status == 200 and json.loads(raw) == {"ok": True}
    kind, cam_id, frames, kw = shell.calls[-1]
    assert (kind, cam_id, frames) == ("frames", "cam1", [b"AAA", b"BBB"])
    assert kw == {"rid": "r1", "caption": "which pin?", "mode": "clip", "seconds": 3.5,
                  "frame_id": "f-1"}
    # an empty rid arrives as None — the user's own shot, not an answer to a look
    body, ctype = _multipart({}, [("frames", "a.png", b"AAA")])
    _call(app, "POST", "/api/camera/cam1/frames", body, ctype)
    assert shell.calls[-1][3]["rid"] is None


def test_live_cancel_attachment_and_image_routes():
    shell = _RouteShell()
    app, _b = _app(shell)
    status, _ = _call(app, "POST", "/api/camera/cam1/live",
                      json.dumps({"on": True, "label": "USB cam"}).encode(), "application/json")
    assert status == 200 and shell.calls[-1] == ("live", "cam1", True, "USB cam")
    _call(app, "POST", "/api/camera/cam1/cancel", json.dumps({"keep_open": True}).encode(),
          "application/json")
    assert shell.calls[-1] == ("cancel", "cam1", True)
    _call(app, "POST", "/api/camera/cam1/cancel")          # a bare POST still closes
    assert shell.calls[-1] == ("cancel", "cam1", False)
    body, ctype = _multipart({"frame": "f-5"}, [("file", "view.png", b"PNGDATA")])
    status, raw = _call(app, "POST", "/api/attachments", body, ctype)
    assert status == 200 and shell.calls[-1] == ("attach", "view.png", b"PNGDATA", "f-5")
    status, _ = _call(app, "GET", "/api/images/nope")
    assert status == 404


def test_served_images_and_the_hologram_list(tmp_path):
    shell = _RouteShell()
    pic = tmp_path / "shot.png"
    pic.write_bytes(_png((8, 8)))
    shell.images["abc"] = pic
    app, builds = _app(shell, tmp_path)
    status, raw = _call(app, "GET", "/api/images/abc")
    assert status == 200 and raw == pic.read_bytes()
    builds.rows = [SimpleNamespace(slug="iron-eye", name="Iron Eye", build_kind=BuildKind.MODEL),
                   SimpleNamespace(slug="bare", name="Bare", build_kind=BuildKind.MODEL),
                   SimpleNamespace(slug="app", name="App", build_kind=BuildKind.APP)]
    mesh = tmp_path / "iron-eye" / "assets" / "model.stl"
    mesh.parent.mkdir(parents=True)
    mesh.write_bytes(b"solid\n")
    status, raw = _call(app, "GET", "/api/camera/holograms")
    assert status == 200
    assert json.loads(raw) == {"holograms": [{"slug": "iron-eye", "name": "Iron Eye",
                                              "stl": "/builds/iron-eye/assets/model.stl"}]}


def test_camera_preferences_are_settings_with_defaults():
    from helix.api.server import _SETTING_KEYS
    for key in ("camera_device", "camera_mirror", "camera_clip_seconds", "camera_attach_view"):
        assert key in _SETTING_KEYS
