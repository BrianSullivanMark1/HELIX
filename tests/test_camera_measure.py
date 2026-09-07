"""AR measure & true scale — the backend half (READ_ME/MAKER_FLOW.md §5, §6).

What the panel's ruler sends and what HELIX hears, driven without a browser:
  - the holder: a "measure" command waits long and cancel-aware, and ABANDONS on the way out so a
    late Send is never settled into a void;
  - the plain 'Measured: …' line (one decimal, three-figure scale, garbage dropped);
  - the shell: the measure command opens/raises the panel and parks; Send settles the parked
    command or becomes a turn with a context bracket; ✕ answers with the cancel line; closing the
    panel answers plainly; a reloaded page re-raises the ask from the snapshot;
  - the layout beside a mesh rides the hologram event and the holograms route (or is null);
  - the routes, as plain ASGI.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from helix.adapters.signal_bus import SignalBus
from helix.api.server import EventHub, build_app
from helix.api.shell import ShellSession
from helix.domain.events import CameraCommandRequested
from helix.domain.models import BuildKind
from helix.services import camera
from helix.services.camera import CameraCommand, measure_line, read_layout
from helix.services.cancel import CancelToken

ROOT = Path(__file__).resolve().parent.parent

LAYOUT = {
    "units": "mm", "name": "IronEye", "outer": [115.0, 48.0, 35.0], "inner": [110.0, 43.0, 30.0],
    "wall": 2.5, "floor": 2.0, "lid": "screw",
    "components": [{"key": "xiao_esp32s3_sense", "label": "CAM", "x": 84.0, "y": 12.0, "w": 22.5,
                    "h": 18.5, "rot": 0, "face": "front", "mount": "pocket", "on_lid": False,
                    "z_top": 9.0,
                    "apertures": [{"kind": "lens", "x": 93.0, "y": 21.0, "d": 8.0, "face": "front"}]}],
    "apertures": [{"face": "left", "kind": "usb_c", "x": 24.0, "z": 8.0, "w": 10.0, "h": 4.0, "for": "CHG"}],
    "screws": [{"x": 5.0, "y": 5.0, "size": "M2", "insert": 3.2}],
    "problems": [],
}

SPEC_BODY = {
    "mm_per_px": 0.19, "reference": "card long edge",
    "items": [{"kind": "box", "label": "XIAO", "w_mm": 21.1, "h_mm": 17.6},
              {"kind": "distance", "label": "hole pitch", "mm": 15.2}],
}
SPEC_LINE = "Measured: XIAO 21.1 × 17.6 mm; hole pitch 15.2 mm (0.19 mm/px, card long edge)"


# ---- the holder ---------------------------------------------------------------------------------

def test_a_measure_command_waits_long_and_an_instant_command_waits_briefly():
    assert camera.MEASURE_TIMEOUT_S == 300.0 and camera.MEASURE_TIMEOUT_S < 600  # under the turn budget
    assert camera.COMMAND_TIMEOUT_S < camera.MEASURE_TIMEOUT_S
    cmd = CameraCommand("measure", {"prompt": "the XIAO"})
    assert cmd.waiting and not cmd.abandoned
    cmd.settle("Measured: XIAO 21.1 × 17.6 mm")
    assert cmd.wait(timeout=0.05) == "Measured: XIAO 21.1 × 17.6 mm" and not cmd.waiting


def test_the_wait_breaks_on_the_turns_stop_and_abandons():
    cmd = CameraCommand("measure", {"prompt": "the XIAO"})
    token = CancelToken()
    token.cancel()
    t0 = time.monotonic()
    assert cmd.wait(timeout=5.0, cancel=token) is None
    assert time.monotonic() - t0 < 1.0
    assert cmd.abandoned and not cmd.waiting
    # a late settle still records the line (harmless) but the holder says nobody is waiting
    cmd.settle("late")
    assert cmd.reply == "late" and cmd.settled and cmd.abandoned


def test_a_timed_out_wait_abandons_too():
    cmd = CameraCommand("overlay", {"items": []})
    assert cmd.wait(timeout=0.02) is None and cmd.abandoned


# ---- the line ------------------------------------------------------------------------------------

def test_the_measured_line_reads_exactly_as_the_spec_shows():
    assert measure_line(SPEC_BODY) == SPEC_LINE


def test_the_line_rounds_to_one_decimal_keeps_a_close_up_scale_and_drops_garbage():
    line = measure_line({
        "mm_per_px": 0.052349, "reference": "  US   quarter ",
        "items": [
            {"kind": "box", "label": "", "w_mm": 21.06, "h_mm": "17.649"},   # label defaults, strings parse
            {"kind": "distance", "label": "pitch", "mm": 0},                # a zero length isn't a length
            {"kind": "distance", "label": "pitch", "mm": "nope"},
            {"kind": "circle", "label": "ring", "mm": 4.0},                  # unknown kind
            "garbage",
            {"kind": "distance", "label": "x" * 80, "mm": float("nan")},
            {"kind": "distance", "label": "hole pitch", "mm": 15.25},
        ],
    })
    assert line == "Measured: part 1 21.1 × 17.6 mm; hole pitch 15.2 mm (0.0523 mm/px, US quarter)"
    assert measure_line({"mm_per_px": 0.19, "items": [{"kind": "box", "w_mm": 1, "h_mm": 2}]}) \
        == "Measured: part 1 1.0 × 2.0 mm (0.19 mm/px)"
    assert measure_line({"items": [{"kind": "distance", "label": "gap", "mm": 3.0}]}) \
        == "Measured: gap 3.0 mm"
    assert measure_line({"mm_per_px": 0.19, "items": []}) == ""
    assert measure_line({"items": [{"kind": "box", "w_mm": -1, "h_mm": 2}]}) == ""
    assert measure_line("not a dict") == ""
    assert "px" not in measure_line({"items": [{"kind": "distance", "mm": 2}]})  # never pixels


def test_the_calibration_references_agree_between_the_shell_and_the_panel():
    """The panel's presets (web/src/lib/measure.ts) state the same millimetres the backend lists —
    the card is ISO/IEC 7810 ID-1, the marker is the printable HELIX plate's outer square."""
    src = (ROOT / "web" / "src" / "lib" / "measure.ts").read_text(encoding="utf-8")
    assert camera.CALIBRATION_REFERENCES["card long edge"] == 85.60
    assert camera.CALIBRATION_REFERENCES["HELIX marker"] == 80.0
    for name, mm in camera.CALIBRATION_REFERENCES.items():
        assert re.search(rf"\b{mm:g}\b", src), f"{name} ({mm} mm) is missing from measure.ts"


# ---- the layout file -----------------------------------------------------------------------------

def test_read_layout_returns_the_dict_or_none_never_raises(tmp_path):
    ws = tmp_path / "iron-eye"
    assert read_layout(ws) is None                                   # no workspace at all
    (ws / "assets").mkdir(parents=True)
    assert read_layout(ws) is None                                   # no file
    f = ws / "assets" / "layout.json"
    f.write_text("{not json", encoding="utf-8")
    assert read_layout(ws) is None
    f.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert read_layout(ws) is None                                   # not the §6 shape
    f.write_text(json.dumps({"outer": [1, 2, 3]}), encoding="utf-8")
    assert read_layout(ws) is None                                   # no components list
    f.write_text(json.dumps(LAYOUT), encoding="utf-8")
    assert read_layout(ws) == LAYOUT
    assert read_layout(str(ws)) == LAYOUT                            # a string path works too


# ---- the shell -----------------------------------------------------------------------------------

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


def _ev(events, t):
    return [e for e in events if e.get("t") == t]


def _measure(sh, prompt="Measure the XIAO's outline") -> CameraCommand:
    """Publish a measure ask the way the camera_measure tool does (it then waits on the holder)."""
    cmd = CameraCommand("measure", {"prompt": prompt})
    sh.c.bus.publish(CameraCommandRequested(request=cmd))
    return cmd


def _no_turn(sh, monkeypatch):
    monkeypatch.setattr(sh, "_start_turn", lambda *a, **k: pytest.fail("a turn started"))


def test_the_measure_command_raises_the_panel_and_parks(rig):
    _c, events, sh = rig
    cmd = _measure(sh)
    cam = _ev(events, "camera")[-1]                       # opened like the manual open path…
    assert cam["manual"] is True and cam["ask"] is None and cam["measure"] is None
    ask = _ev(events, "camera.measure")[-1]               # …then the ask lands on that panel
    assert events.index(ask) > events.index(cam)
    assert ask["id"] == cam["id"] and ask["mid"] == sh._camera["mid"]
    assert ask["prompt"] == "Measure the XIAO's outline"
    assert not cmd.settled and cmd.waiting                # parked, not answered
    assert sh._camera["measure"] is cmd and sh._camera["mid"] == ask["mid"]
    assert "measure" in _ev(events, "status")[-1]["text"]


def test_a_measure_ask_on_an_open_panel_parks_without_reopening(rig):
    _c, events, sh = rig
    cid = sh.camera_open()["id"]
    sh.camera_live(cid, True)
    cmd = _measure(sh, "  measure   the speaker ")
    assert len(_ev(events, "camera")) == 1                # the same panel, raised in place
    ask = _ev(events, "camera.measure")[-1]
    assert ask["id"] == cid and ask["prompt"] == "measure the speaker"
    assert sh._camera["measure"] is cmd and not cmd.settled


def test_a_blank_prompt_gets_the_default_ask_and_a_long_one_is_capped(rig):
    _c, events, sh = rig
    _measure(sh, "")
    assert _ev(events, "camera.measure")[-1]["prompt"] == ShellSession._MEASURE_ASK
    _measure(sh, "x" * 500)
    assert len(_ev(events, "camera.measure")[-1]["prompt"]) == 160


def test_a_send_settles_the_parked_measure_with_the_line_and_starts_no_turn(rig, monkeypatch):
    _c, events, sh = rig
    _no_turn(sh, monkeypatch)
    cmd = _measure(sh)
    cid = sh._camera["id"]
    assert sh.camera_measured(cid, SPEC_BODY) == SPEC_LINE
    assert cmd.wait(timeout=0.1) == SPEC_LINE              # the tool relays exactly this
    assert sh._camera is not None and sh._camera["measure"] is None and sh._camera["mid"] is None
    assert _ev(events, "camera.measure.clear")[-1]["id"] == cid
    trace = [e for e in _ev(events, "msg") if e["role"] == "system"]
    assert trace[-1]["text"] == f"📐 {SPEC_LINE}"           # the transcript shows what was sent
    assert not [e for e in _ev(events, "msg") if e["role"] == "user"]


def test_a_send_with_nothing_parked_becomes_a_turn_with_the_context(rig, monkeypatch):
    _c, events, sh = rig
    started: list = []
    monkeypatch.setattr(sh, "_start_turn", lambda *a, **k: started.append(a))
    cid = sh.camera_open()["id"]
    assert sh.camera_measured(cid, SPEC_BODY) == SPEC_LINE
    assert len(started) == 1
    prompt, from_voice, paths, speaker = started[0]
    assert prompt.startswith(SPEC_LINE) and "[Context:" in prompt and "real millimetres" in prompt
    assert from_voice is False and paths == [] and speaker is None
    trace = [e for e in _ev(events, "msg") if e["role"] == "system"]
    assert trace[-1]["text"] == f"📐 {SPEC_LINE}"
    assert not [e for e in _ev(events, "msg") if e["role"] == "user"]   # no duplicate bubble
    assert not _ev(events, "camera.measure.clear")                        # nothing was parked


def test_a_send_while_busy_queues_behind_the_turn_in_flight(rig, monkeypatch):
    _c, _events, sh = rig
    _no_turn(sh, monkeypatch)
    cid = sh.camera_open()["id"]
    sh._busy = True
    assert sh.camera_measured(cid, SPEC_BODY) == SPEC_LINE
    assert len(sh._pending) == 1 and sh._pending[0][0].startswith(SPEC_LINE)


def test_a_send_after_the_tool_gave_up_still_reaches_helix(rig, monkeypatch):
    _c, events, sh = rig
    started: list = []
    monkeypatch.setattr(sh, "_start_turn", lambda *a, **k: started.append(a))
    cmd = _measure(sh)
    cid = sh._camera["id"]
    assert cmd.wait(timeout=0.02) is None and cmd.abandoned   # stop / the 300 s ceiling
    assert sh.camera_measured(cid, SPEC_BODY) == SPEC_LINE
    assert len(started) == 1 and started[0][0].startswith(SPEC_LINE)
    assert sh._camera["measure"] is None                       # the stale ask is cleared…
    assert _ev(events, "camera.measure.clear")                 # …and its banner with it


def test_nothing_measurable_or_a_stale_panel_does_nothing(rig, monkeypatch):
    _c, events, sh = rig
    _no_turn(sh, monkeypatch)
    cmd = _measure(sh)
    cid = sh._camera["id"]
    assert sh.camera_measured("not-the-id", SPEC_BODY) == ""
    assert sh.camera_measured(cid, {"mm_per_px": 0.19, "items": []}) == ""
    assert sh.camera_measured(cid, {"items": [{"kind": "box", "w_mm": "x", "h_mm": 1}]}) == ""
    assert not cmd.settled and sh._camera["measure"] is cmd
    assert not [e for e in _ev(events, "msg") if e["role"] == "system"]


def test_cancel_answers_the_parked_measure_and_keeps_the_panel(rig):
    _c, events, sh = rig
    assert sh.camera_measure_cancel("nothing-open") is False
    cmd = _measure(sh)
    cid = sh._camera["id"]
    assert sh.camera_measure_cancel(cid) is True
    assert cmd.wait(timeout=0.1) == "The user cancelled the measurement."
    assert sh._camera is not None and sh._camera["id"] == cid and sh._camera["measure"] is None
    assert _ev(events, "camera.measure.clear")[-1]["id"] == cid and not _ev(events, "camera.close")
    assert sh.camera_measure_cancel(cid) is False             # nothing parked any more


def test_closing_the_panel_answers_a_parked_measure_plainly(rig):
    _c, events, sh = rig
    cmd = _measure(sh)
    cid = sh._camera["id"]
    sh.camera_cancel(cid)
    assert cmd.wait(timeout=0.1) == "The camera panel closed before a measurement was sent."
    assert sh._camera is None and _ev(events, "camera.close")


def test_a_newer_measure_ask_replaces_one_still_parked(rig):
    _c, events, sh = rig
    first = _measure(sh, "the XIAO")
    second = _measure(sh, "the speaker")
    assert first.wait(timeout=0.1) == "A newer measurement request replaced this one."
    assert sh._camera["measure"] is second and not second.settled
    assert _ev(events, "camera.measure")[-1]["prompt"] == "the speaker"


def test_a_look_and_a_measure_can_be_parked_together(rig, monkeypatch):
    """The ruler and a held look are independent asks on the same panel."""
    from helix.domain.events import CameraRequested
    from helix.services.camera import CameraRequest

    _c, events, sh = rig
    _no_turn(sh, monkeypatch)
    cid = sh.camera_open()["id"]
    sh.camera_live(cid, True)
    req = CameraRequest("Show me the back")
    sh.c.bus.publish(CameraRequested(request=req))
    cmd = _measure(sh)
    sh.camera_cancel(cid, keep_open=True)                     # Esc on the look banner
    assert req.settled and not cmd.settled and sh._camera["measure"] is cmd
    sh.camera_measured(cid, SPEC_BODY)
    assert cmd.settled and sh._camera is not None


def test_the_snapshot_re_raises_a_parked_measure_for_a_reloaded_page(rig):
    _c, _events, sh = rig
    assert sh.snapshot()["camera"] is None
    cid = sh.camera_open()["id"]
    assert sh.snapshot()["camera"]["measure"] is None
    cmd = _measure(sh, "the XIAO")
    snap = sh.snapshot()["camera"]
    assert snap["id"] == cid and snap["measure"] == {"mid": sh._camera["mid"], "prompt": "the XIAO"}
    assert not cmd.settled


def test_the_hologram_event_carries_the_layout_when_the_file_exists(rig, tmp_path):
    _c, events, sh = rig
    cid = sh.camera_open()["id"]
    ws = tmp_path / "iron-eye"
    (ws / "assets").mkdir(parents=True)
    (ws / "assets" / "model.stl").write_bytes(b"solid x\nendsolid x\n")

    def project():
        cmd = CameraCommand("hologram", {"slug": "iron-eye", "name": "Iron Eye"})
        sh.c.bus.publish(CameraCommandRequested(request=cmd))
        return cmd.wait(timeout=0.2)

    reply = project()
    assert reply.startswith("Projected Iron Eye") and "layout" not in reply
    assert _ev(events, "camera.hologram")[-1]["layout"] is None
    (ws / "assets" / "layout.json").write_text(json.dumps(LAYOUT), encoding="utf-8")
    reply = project()
    assert "(1 part)" in reply and "ghost" in reply
    ev = _ev(events, "camera.hologram")[-1]
    assert ev["id"] == cid and ev["layout"] == LAYOUT and ev["stl"] == "/builds/iron-eye/assets/model.stl"
    (ws / "assets" / "layout.json").write_text("{broken", encoding="utf-8")
    project()
    assert _ev(events, "camera.hologram")[-1]["layout"] is None   # a bad file never breaks projection


# ---- the routes, as plain ASGI -------------------------------------------------------------------

class _RouteShell:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"t": "snapshot"}

    def camera_measured(self, cam_id, payload):
        self.calls.append(("measured", cam_id, payload))
        return "Measured: XIAO 21.1 × 17.6 mm (0.19 mm/px, card long edge)"

    def camera_measure_cancel(self, cam_id):
        self.calls.append(("measure_cancel", cam_id))
        return True


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


def test_the_measure_route_sends_or_cancels():
    shell = _RouteShell()
    app, _b = _app(shell)
    status, raw = _call(app, "POST", "/api/camera/cam1/measure", json.dumps(SPEC_BODY).encode(),
                        "application/json")
    assert status == 200
    assert json.loads(raw) == {"ok": True,
                               "line": "Measured: XIAO 21.1 × 17.6 mm (0.19 mm/px, card long edge)"}
    assert shell.calls[-1] == ("measured", "cam1", SPEC_BODY)
    status, raw = _call(app, "POST", "/api/camera/cam1/measure", json.dumps({"cancel": True}).encode(),
                        "application/json")
    assert status == 200 and json.loads(raw) == {"ok": True, "line": ""}
    assert shell.calls[-1] == ("measure_cancel", "cam1")
    status, raw = _call(app, "POST", "/api/camera/cam1/measure")   # a bare POST measures nothing
    assert status == 200 and shell.calls[-1] == ("measured", "cam1", {})
    status, _ = _call(app, "POST", "/api/camera/cam1/measure", b"[1,2]", "application/json")
    assert status == 200 and shell.calls[-1] == ("measured", "cam1", {})


def test_the_holograms_route_carries_each_layout_or_null(tmp_path):
    shell = _RouteShell()
    app, builds = _app(shell, tmp_path)
    builds.rows = [SimpleNamespace(slug="iron-eye", name="Iron Eye", build_kind=BuildKind.MODEL),
                   SimpleNamespace(slug="bracket", name="Bracket", build_kind=BuildKind.MODEL)]
    for slug in ("iron-eye", "bracket"):
        (tmp_path / slug / "assets").mkdir(parents=True)
        (tmp_path / slug / "assets" / "model.stl").write_bytes(b"solid\n")
    (tmp_path / "iron-eye" / "assets" / "layout.json").write_text(json.dumps(LAYOUT), encoding="utf-8")
    status, raw = _call(app, "GET", "/api/camera/holograms")
    assert status == 200
    rows = json.loads(raw)["holograms"]
    assert [r["slug"] for r in rows] == ["iron-eye", "bracket"]
    assert rows[0]["layout"] == LAYOUT and rows[0]["stl"] == "/builds/iron-eye/assets/model.stl"
    assert rows[1]["layout"] is None
