"""The Dream journal's face: GET /api/dream/journal and the shell accessor behind it
(READ_ME/DREAM_MIND.md §11 — "Face + routes").

The engine's `journal_entries()` (services/dream.py) owns the shape of a night; the shell hands it
through untouched, adds what an EMPTY journal needs to be honest (is dreaming on, when does the
window open), and tolerates every way the engine can be missing. The route is a thin, token-guarded
call, driven as plain ASGI like test_api_lifecycle (no TestClient — its import once took an unrelated
UI test down with it).
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from helix.adapters.signal_bus import SignalBus
from helix.api.shell import ShellSession


# ----------------------------------------------------------------- the rig (what the shell touches)
class _Settings:
    def __init__(self, **kv):
        self._d = dict(kv)

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class _Conversation:
    def recent_messages(self, n):
        return ["x"]  # not a fresh install — no greeting

    def run_turn(self, text, **kw):
        return "Done."


class _Queue:
    def active_names(self):
        return []

    def cancel_active(self):
        pass

    def clear_queued(self):
        return []


class _Dream:
    """Phase 1's engine surface, as the shell sees it — no journal view."""

    def __init__(self):
        self.activity = None
        self.running = False

    def tick(self):
        pass

    def status(self):
        return "Dreaming nightly from 23:00 for 8 hours."

    def morning_report(self):
        return None

    def pending_report(self):
        return None


class _DreamContainer:
    def __init__(self, dream=None):
        self.settings = _Settings(claude_api_key="sk-test")
        self.bus = SignalBus()
        self.conversation = _Conversation()
        self.build_queue = _Queue()
        self.voice_id = None
        self.dream = dream if dream is not None else _Dream()
        self.paths = SimpleNamespace(source_root="C:/repo", builds="x")
        self.growth_model = SimpleNamespace(resolve=lambda: "claude-fable-5")


class _NoDreamContainer(_DreamContainer):
    def __init__(self):
        super().__init__()
        self.dream = None  # an older container: no engine at all


# ----------------------------------------------------------------- what a night looks like
def _night(**over) -> dict:
    """One night as DreamService.journal_entries() shapes it — the page's contract."""
    night = {
        "id": "2026-09-04-night", "day": "2026-09-04", "kind": "night",
        "started": "2026-09-04T23:00:12", "ended": "2026-09-05T07:00:03", "window": "23:00–07:00",
        "stopped_reason": "the window ended", "theme": "the camera panel", "model": "Fable 5",
        "discoveries": [
            {"text": "The XIAO ESP32S3 Sense's camera connector is a 24-pin FPC at 0.5 mm pitch",
             "source": "wiki.seeedstudio.com", "url": "https://wiki.seeedstudio.com/xiao_esp32s3_camera_usage/",
             "verified": True, "kind": "fact"},
            {"text": "An INMP441 breakout may need a pull-down on L/R", "source": "", "url": "",
             "verified": False, "kind": "finding"},
        ],
        "facts": [
            {"id": "f1", "claim": "XIAO ESP32S3 Sense camera connector", "value": "24-pin FPC, 0.5 mm pitch",
             "host": "wiki.seeedstudio.com", "url": "https://wiki.seeedstudio.com/xiao_esp32s3_camera_usage/",
             "date": "2026-09-05", "project": "hat cam", "topics": ["camera", "esp32s3"]},
        ],
        "facts_noted": 1,
        "experiments": [{"idea": "measure the tracker's frame cost", "ok": True, "findings": "…",
                         "recommendation": "cap the tracker at 15 fps", "summary": ""}],
        "research": [], "verify": [], "agenda": {}, "agenda_remaining": [], "self_model_delta": {},
        "drafts": [
            {"outcome": "applied", "held_for": "", "summary": "The camera panel remembers its last device",
             "request": "…", "branch": "selfdev/camera-device", "reason": "", "origin": "research"},
            {"outcome": "drafted", "held_for": "", "summary": "Sleep phrases cover 'nap time'",
             "request": "…", "branch": "selfdev/nap-time", "reason": "", "origin": "plan"},
        ],
        "applied": [{"branch": "selfdev/camera-device", "summary": "The camera panel remembers its last device"}],
        "counts": {"tried": 2, "landed": 2, "applied": 1, "held": 0, "waiting": 1, "failed": 0,
                   "stopped": 0, "limited": 0, "facts": 1, "discoveries": 2, "experiments": 1},
        "rebuild": {"ok": True, "restored": False, "message": "rebuilt and relaunched", "at": "2026-09-05T06:41:00"},
        "restart_needed": 0,
        "report": "Last night I verified the XIAO's camera connector on Seeed's wiki and applied one change.",
        "report_delivered": True, "limit": "", "limit_log": [], "weekly_digest": "", "in_progress": False,
    }
    night.update(over)
    return night


class _JournalDream(_Dream):
    """The engine with the Phase 2 journal view."""

    def __init__(self, nights=None, **kw):
        super().__init__(**kw)
        self.nights = list(nights or [])
        self.asked: list[int] = []

    def journal_entries(self, nights=30):
        self.asked.append(nights)
        return self.nights[:nights]


class _BrokenJournalDream(_JournalDream):
    def journal_entries(self, nights=30):
        raise OSError("the journal file is locked")


def _shell(container):
    events: list[dict] = []
    sh = ShellSession(container, events.append, voice=None)
    return sh, events


# ----------------------------------------------------------------- the shell accessor
def test_the_journal_hands_the_engines_nights_through_with_the_empty_state_facts():
    """The nights come from the engine untouched (discoveries first, each fact with host and
    date, applied changes with their one-line summaries); beside them, whether dreaming is on and
    when the window opens — what the page's empty state needs to say the truth."""
    dream = _JournalDream(nights=[_night(), _night(id="2026-09-03-night", day="2026-09-03")])
    container = _DreamContainer(dream)
    container.settings.set("dream_enabled", True)
    container.settings.set("dream_start", "23:30")
    sh, _events = _shell(container)
    try:
        body = sh.dream_journal()
    finally:
        sh.shutdown()
    assert body["available"] is True and body["enabled"] is True and body["start"] == "23:30"
    assert body["running"] is False
    assert dream.asked == [30]                               # the default: the last 30 nights
    assert [n["day"] for n in body["nights"]] == ["2026-09-04", "2026-09-03"]
    night = body["nights"][0]
    assert night["discoveries"][0]["verified"] is True
    assert night["discoveries"][0]["source"] == "wiki.seeedstudio.com"
    assert night["discoveries"][1]["verified"] is False       # the unverified stays marked
    fact = night["facts"][0]
    assert fact["host"] == "wiki.seeedstudio.com" and fact["date"] == "2026-09-05"
    assert night["applied"] == [{"branch": "selfdev/camera-device",
                                 "summary": "The camera panel remembers its last device"}]
    assert night["counts"]["applied"] == 1 and night["counts"]["waiting"] == 1
    assert night["rebuild"]["ok"] is True
    assert night["report"].startswith("Last night I verified")


def test_the_nights_count_is_bounded_and_unreadable_counts_read_as_the_default():
    dream = _JournalDream(nights=[_night()])
    sh, _events = _shell(_DreamContainer(dream))
    try:
        sh.dream_journal(7)
        sh.dream_journal("lots")
        sh.dream_journal(0)
        sh.dream_journal(10_000)
    finally:
        sh.shutdown()
    assert dream.asked == [7, 30, 1, 90]


def test_no_engine_and_an_older_engine_and_a_locked_journal_all_read_as_no_nights():
    """Dreaming is an addition to the shell, never a dependency: no engine (an older container),
    an engine without the Phase 2 view, or a journal that cannot be read each answer an honest
    empty page — never a 500."""
    sh, _events = _shell(_NoDreamContainer())
    try:
        body = sh.dream_journal()
    finally:
        sh.shutdown()
    assert body == {"available": False, "enabled": False, "start": "23:00", "running": False, "nights": []}

    sh, _events = _shell(_DreamContainer(_Dream()))  # Phase 1's engine: no journal_entries
    try:
        body = sh.dream_journal()
    finally:
        sh.shutdown()
    assert body["available"] is False and body["nights"] == []

    dream = _BrokenJournalDream()
    dream.running = True
    sh, _events = _shell(_DreamContainer(dream))
    try:
        body = sh.dream_journal()
    finally:
        sh.shutdown()
    assert body["available"] is True and body["nights"] == [] and body["running"] is True


def test_a_journal_with_stray_entries_keeps_only_the_records():
    dream = _JournalDream(nights=[_night(), "a stray line", None, 7])
    sh, _events = _shell(_DreamContainer(dream))
    try:
        body = sh.dream_journal()
    finally:
        sh.shutdown()
    assert len(body["nights"]) == 1 and body["nights"][0]["day"] == "2026-09-04"


def test_the_settings_default_when_never_set_and_a_bare_rig_without_settings_still_answers():
    sh, _events = _shell(_DreamContainer(_JournalDream()))
    try:
        body = sh.dream_journal()
    finally:
        sh.shutdown()
    assert body["enabled"] is False and body["start"] == "23:00"  # the contract's defaults

    container = _DreamContainer(_JournalDream(nights=[_night()]))
    container.settings = None  # a rig with no settings at all
    sh, _events = _shell(container)
    try:
        body = sh.dream_journal()
    finally:
        sh.shutdown()
    assert body["enabled"] is False and body["start"] == "23:00" and len(body["nights"]) == 1


# ----------------------------------------------------------------- the route, over plain ASGI
class _RouteShell:
    def __init__(self):
        self.voice = None
        self.asked: list = []

    def snapshot(self):
        return {"t": "snapshot"}

    def push(self, ev):
        pass

    def voice_state(self):
        return {"supported": False}

    def dream_journal(self, nights=30):
        self.asked.append(nights)
        return {"available": True, "enabled": True, "start": "23:00", "running": False,
                "nights": [_night()][:nights]}


def _route_app():
    from helix.api.server import EventHub, build_app

    container = SimpleNamespace(
        settings=_Settings(web_token="tok-test"),
        paths=SimpleNamespace(builds="does-not-exist"),  # mounted with check_dir=False
    )
    shell = _RouteShell()
    return build_app(container, shell, EventHub(), None), shell


def _asgi(app, method: str, path: str, query: str = "", token: str | None = "tok-test"):
    """One request straight through the ASGI stack → (status, decoded JSON body or None)."""
    headers = [(b"host", b"127.0.0.1:8737")]
    if token is not None:
        headers.append((b"x-helix-token", token.encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
        "method": method, "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": query.encode(), "headers": headers,
        "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8737),
    }
    status: list[int] = []
    chunks: list[bytes] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status.append(int(message["status"]))
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    raw = b"".join(chunks)
    return status[0], (json.loads(raw) if raw else None)


def test_get_api_dream_journal_is_a_thin_token_guarded_call_into_the_shell():
    app, shell = _route_app()
    status, body = _asgi(app, "GET", "/api/dream/journal")
    assert status == 200
    assert body["available"] is True and body["enabled"] is True and body["start"] == "23:00"
    assert shell.asked == [30]                                   # the default: 30 nights
    night = body["nights"][0]
    assert night["day"] == "2026-09-04"
    assert night["discoveries"][0]["source"] == "wiki.seeedstudio.com"
    assert night["facts"][0]["host"] == "wiki.seeedstudio.com" and night["facts"][0]["date"] == "2026-09-05"
    assert night["applied"][0]["summary"] == "The camera panel remembers its last device"
    assert night["counts"]["applied"] == 1
    assert night["rebuild"]["ok"] is True and night["report"]

    status, _body = _asgi(app, "GET", "/api/dream/journal", query="nights=7")
    assert status == 200 and shell.asked[-1] == 7             # ?nights= reaches the shell

    assert _asgi(app, "GET", "/api/dream/journal", token="wrong")[0] == 401
    assert _asgi(app, "GET", "/api/dream/journal", token=None)[0] == 401  # the token is not optional


@pytest.mark.parametrize("query", ["nights=abc", "nights="])
def test_an_unreadable_nights_query_is_refused_not_crashed(query):
    app, _shell = _route_app()
    status, _body = _asgi(app, "GET", "/api/dream/journal", query=query)
    assert status == 422  # FastAPI's validation answer — never a 500, never a default of nothing


# ----------------------------------------------------------------- the page's honesty rules (source pins)
# The face has no test runner of its own; these pin the page's text the way test_camera_measure
# pins measure.ts, so a regression in wording is caught by the suite that runs every night.
ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "web" / "src" / "pages" / "Dream.tsx"


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_a_failed_experiment_shows_the_journals_own_reason_not_a_stock_phrase():
    """The first real night journaled 'The experiment ran past its 4-minute budget and was
    stopped.' as the experiment's summary; the page said 'could not run' and dropped it. The
    stock phrase is only the fallback when the journal recorded no reason."""
    src = _page()
    assert re.search(r"e\.ok === false\s*\?\s*\(e\.summary \|\| \"could not run\"\)", src)


def test_a_draft_line_leads_with_the_request_and_drops_a_chatter_summary():
    """A draft's `summary` is the coder's closing words; the night's drafts carried 'All done —
    everything is in place. Here's a summary of the change:' there. The request is the honest
    label, the summary sits muted beneath it, and sign-off chatter is dropped, never shown."""
    src = _page()
    assert "function cleanSummary(" in src
    assert "{d.request || summary || d.branch}" in src          # the request leads a draft line
    assert "const summary = cleanSummary(d.summary);" in src
    assert "const summary = cleanSummary(a.summary);" in src    # applied lines are filtered the same
    body = src[src.index("function cleanSummary("):src.index("function ExperimentRow(")]
    assert "[:：]$" in body                                      # ends with a colon → nothing
    assert "here(’|')?s (a |the )?summary" in body               # "here's a summary" → nothing
    assert "^all done" in body                                   # "All done — …" → nothing


def test_the_journal_is_read_once_on_open_and_again_when_a_session_flips():
    """One effect: it runs on mount and whenever dream.running changes — a second mount-only
    effect made the page fetch the journal twice on open."""
    src = _page()
    page = src[src.index("export default function Dream()"):]
    effects = re.findall(r"useEffect\(", page)
    assert len(effects) == 1, effects
    assert "useEffect(() => { load(); }, [liveDream?.running, load]);" in page
    assert "useEffect(load, [load])" not in page


def test_the_nights_model_is_named_as_what_the_drafts_ran_on():
    """The journal's `model` is the growth model the night planned and drafted on; the research
    and verify turns run through the conversation's own model (READ_ME/DREAM.md §11), so the
    header must not present the bare name as the whole night's model."""
    assert "` · drafted on ${night.model}`" in _page()


def test_the_docs_record_the_minds_wiring_and_the_rail_per_cycle():
    """READ_ME/DREAM.md and ARCHITECTURE.md must carry the engine facts that shipped with the face:
    the container order, the collaborators, the self-model file, the hooks, the rail check, and
    which model each cycle runs on — the face must not overclaim Fable for the research turns."""
    dream = (ROOT / "READ_ME" / "DREAM.md").read_text(encoding="utf-8")
    arch = (ROOT / "READ_ME" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for doc in (dream, arch):
        assert "DreamMind(" in doc
        assert "mind=" in doc and "subscription=" in doc
        assert "verified=self.verified" in doc
        assert "helix_self.json" in doc
        assert "NightHooks(improve, note, record, should_stop, nights, limit, rail_problem, activity)" in doc
        assert "rail_problem" in doc
        assert "conversation's own model" in doc.lower()  # research/verify: the orb rail, said plainly
    assert "## 11. What shipped — Phase 2, the mind" in dream
    assert "not every turn in it" in dream                 # bar 5 no longer overclaims
