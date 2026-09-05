"""The Dream Mind (READ_ME/DREAM_MIND.md §11 + §13) — the reflection's strict shape (QUIET and
malformed included), the agenda caps, the research turn's FINDINGS shape, the six cycles against a
fake chat / conversation / research / verified store / gate / lane (deadline and activity pauses
honoured, empty inputs skipped cleanly, a failing research turn journaled and skipped, facts counted
from the verified store's deltas, discoveries chosen, the self-model merged and dated, the weekly
digest on the 7th night), and the limits helper's matrix."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from helix.domain.models import Message, Role
from helix.ports.llm import Reply, Text
from helix.services import dream_mind as mind_mod
from helix.services.dream import NightHooks, Request
from helix.services.dream_mind import (
    DREAM_DIGEST_SYSTEM,
    DREAM_REFLECT_SYSTEM,
    DREAM_RESEARCH_SYSTEM,
    DREAM_VERIFY_ADDENDUM,
    DreamMind,
    NightSummary,
    choose_discoveries,
    describe_self_model,
    merge_self_model,
    parse_findings,
    parse_recommendation,
    parse_reflection,
)
from helix.services.limits import LIMIT_BACKOFF_MINUTES, backoff_minutes, looks_like_limit, reset_hint


# ----------------------------------------------------------------------------- fakes
class _Clock:
    def __init__(self, hour=23, minute=10, step_s=0):
        self.dt = datetime(2026, 9, 4, hour, minute, 0)
        self.step = timedelta(seconds=step_s)

    def now(self):
        out = self.dt
        self.dt = self.dt + self.step
        return out

    def advance(self, **kw):
        self.dt += timedelta(**kw)


class _Chat:
    """Answers each call from `replies` in order (the last repeats); an Exception entry is raised."""

    def __init__(self, *replies):
        self.replies = list(replies) or ["QUIET"]
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def chat(self, turns, *, system=None, tools=None):
        self.prompts.append("".join(b.text for t in turns for b in t.blocks if isinstance(b, Text)))
        self.systems.append(system)
        reply = self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return Reply(blocks=(Text(reply),))


@dataclass
class _Fact:
    id: str
    claim: str
    value: str
    source_url: str
    host: str
    verified_at: str
    first_verified_at: str
    confidence: float = 0.9
    topics: tuple = ()
    project: str = ""
    note: str = ""


class _Verified:
    """An in-memory VerifiedStore: note / count / recent / stale / lookup / mark_reverified."""

    def __init__(self, clock, facts=None):
        self.clock = clock
        self.facts: list[_Fact] = list(facts or [])
        self.reverified: list[str] = []

    def _stamp(self):
        return self.clock.now().strftime("%Y-%m-%dT%H:%M")

    def note(self, claim, value, source_url, *, topics=(), project="", confidence=0.9, note=""):
        fid = "f" + str(abs(hash(claim.lower())) % 10_000_000)
        existing = next((f for f in self.facts if f.id == fid), None)
        host = source_url.split("/")[2]
        fact = _Fact(fid, claim, value, source_url, host, self._stamp(),
                     existing.first_verified_at if existing else self._stamp(), confidence,
                     tuple(topics), project, note)
        self.facts = [fact if f.id == fid else f for f in self.facts] if existing else self.facts + [fact]
        return fact

    def count(self):
        return len(self.facts)

    def recent(self, n=10):
        return sorted(self.facts, key=lambda f: f.verified_at, reverse=True)[:n]

    def stale(self, days=90):
        cutoff = (self.clock.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
        return [f for f in self.facts if f.verified_at < cutoff]

    def lookup(self, text, *, project="", limit=8):
        words = set(text.lower().split())
        return [f for f in self.facts if words & set(f.claim.lower().split())][:limit]

    def for_turn(self, text, project=""):
        return ""

    def mark_reverified(self, fid, source_url=None):
        self.reverified.append(fid)
        for i, f in enumerate(self.facts):
            if f.id == fid:
                fresh = _Fact(f.id, f.claim, f.value, source_url or f.source_url, f.host, self._stamp(),
                              f.first_verified_at, f.confidence, f.topics, f.project, f.note)
                self.facts[i] = fresh
                return fresh
        return None


class _Research:
    def __init__(self):
        self.trail: list[str] = []

    def take_trail(self):
        out, self.trail = list(self.trail), []
        return out


RESEARCH_REPLY = (
    "I searched and read the wiki.\n"
    "FINDINGS:\n"
    "- The XIAO ESP32S3 Sense has 8 MB PSRAM [verified: https://wiki.seeedstudio.com/xiao_esp32s3/]\n"
    "- The INMP441 costs about $3 on AliExpress [unverified]\n"
    "FACTS NOTED: 1\n"
    "IDEAS:\n"
    "- Use the esp-idf I2S driver for the mic — source: https://docs.espressif.com/i2s\n"
)


class _Conversation:
    """run_turn answers from `replies` (an Exception entry raises); `on_turn(n)` runs first so a test
    can play the model noting a fact or leaving a trail. recent_messages gives the week's turns."""

    def __init__(self, *replies, clock=None, verified=None, research=None, on_turn=None):
        self.replies = list(replies) or [RESEARCH_REPLY]
        self.calls: list[tuple[str, dict]] = []
        self.clock = clock
        self.verified = verified
        self.research = research
        self.on_turn = on_turn

    def run_turn(self, prompt, **kw):
        self.calls.append((prompt, kw))
        n = len(self.calls)
        if self.on_turn is not None:
            self.on_turn(n, prompt)
        reply = self.replies[min(n - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def recent_messages(self, limit=50):
        now = self.clock.now() if self.clock else datetime(2026, 9, 4, 23, 0)
        return [
            Message(Role.USER, "Build me the IronEye microphone array", now - timedelta(days=2)),
            Message(Role.ASSISTANT, "On it.", now - timedelta(days=2)),
            Message(Role.USER, "An old ask from last month", now - timedelta(days=30)),
        ]


class _SelfDev:
    def __init__(self, findings="# Findings\n## Question\nq\n## What I tried\nt\n## Results\nr\n"
                            "## Recommendation\nSwitch the DDG parser to lxml in adapters/research_web.py.\n"):
        self.findings = findings
        self.requests: list[tuple[str, float]] = []
        self.models: list = []

    def experiment(self, request, *, timeout_s=1500.0, model=None):
        self.requests.append((request, timeout_s))
        self.models.append(model)
        return self.findings


class _Evolve:
    def __init__(self):
        self.backlog: list[str] = []

    def material(self):
        return ("IMPROVEMENT BACKLOG (…):\n- remember the camera\n\nLESSONS (…):\n[brian] Keep replies short"
                "\n\nLOG TAIL (the last lines of helix.log):\nERROR reminders: fired twice")

    def add_backlog(self, text):
        if text not in self.backlog:
            self.backlog.append(text)
        return True


class _Store:
    def __init__(self, d=None):
        self.d = dict(d or {})

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


class _Tools:
    def specs(self):
        from helix.ports.llm import ToolSpec
        return [ToolSpec("research_search", "Search the documented web. Read-only.", {}),
                ToolSpec("build_app", "Build an app from a description. Ask first.", {})]


class _Hooks:
    """NightHooks with everything recorded; `improve` drafts every request as applied unless told
    otherwise; `limit` answers from `limit_answers` (True = resumed, False = the night is over)."""

    def __init__(self, *, limit_answers=(), should_stop=None, nights=None, rail=None, improve=None):
        self.notes: list[str] = []
        self.records: list[dict] = []
        self.improve_calls: list[list[Request]] = []
        self.limits: list[str] = []
        self._limit_answers = list(limit_answers)
        self._should_stop = should_stop or (lambda: False)
        self._nights = nights or []
        self._rail = rail or (lambda: None)
        self._improve = improve
        self.hooks = NightHooks(
            improve=self._do_improve, note=self.notes.append, record=self._record,
            should_stop=self._should_stop, nights=lambda n: list(self._nights)[-n:],
            limit=self._limit, rail_problem=self._rail,
        )

    def _record(self, fields):
        self.records.append(dict(fields))

    def merged(self) -> dict:
        out: dict = {}
        for r in self.records:
            out.update(r)
        return out

    def _do_improve(self, requests):
        self.improve_calls.append(list(requests))
        if self._improve is not None:
            return self._improve(requests)
        return [{"request": r.text, "outcome": "applied", "summary": f"did: {r.text[:30]}", "origin": r.origin,
                 "branch": f"selfdev/{i}"} for i, r in enumerate(requests)]

    def _limit(self, text):
        self.limits.append(text)
        if self._limit_answers:
            return self._limit_answers.pop(0)
        return False


REFLECTION = (
    "CAPABLE:\n- Searches Amazon and stages a cart\n- Sees through the camera\n"
    "WEAK:\n- The camera panel forgets its device\n"
    "BUILDING:\n- IronEye: a microphone array; it needs a verified I2S mic\n"
    "AGENDA:\n"
    "RESEARCH:\n"
    "- Does the XIAO ESP32S3 Sense have PSRAM? — why: it decides whether the audio buffer fits\n"
    "- Which I2S MEMS mic under $5 is in stock at Digi-Key? — why: the IronEye BOM row is unresolved\n"
    "VERIFY:\n- The INMP441 runs at 3.3 V\n"
    "EXPERIMENT:\n- Measure whether lxml parses the search page faster than html.parser\n"
    "IMPROVE:\n"
    "1. Remember the camera device in services/camera.py; add a test.\nTAKES: remember the camera\nEFFORT: standard\n"
    "2. Shorten the morning brief in services/agents.py.\nEFFORT: deep\n"
)
FOLDED_PLAN = (
    "1. [research] Switch the search-page parser to lxml in adapters/research_web.py; add a timing test.\n"
    "EFFORT: deep\n"
    "2. Remember the camera device in services/camera.py; add a test.\nEFFORT: standard\n"
)


def _mind(clock=None, *, chat=None, conversation=None, verified=None, research=None, selfdev=None,
          evolve=None, store=None, activity=None, tools=None, growth_model=None, source_root=None,
          agents=None, default_agent_names=(), log_tail=None):
    clock = clock or _Clock()
    return DreamMind(
        chat if chat is not None else _Chat(REFLECTION, FOLDED_PLAN),
        conversation, selfdev, verified, research, None, None, tools if tools is not None else _Tools(),
        store if store is not None else _Store(), _Store(), clock,
        log_tail or (lambda: "WARNING x\nINFO y\nERROR z"),
        activity, evolve=evolve, growth_model=growth_model, source_root=source_root, agents=agents,
        default_agent_names=default_agent_names,
    )


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(mind_mod, "_ACTIVITY_POLL_S", 0.0)


def _full_rig(clock=None, **kw):
    clock = clock or _Clock()
    verified = _Verified(clock)
    research = _Research()

    def on_turn(n, prompt):
        if "VERIFY TURN" in prompt:
            verified.note("The INMP441 runs at 3.3 V", "3.3 V (1.62–3.63 V)",
                          "https://invensense.tdk.com/products/inmp441/")
            research.trail.append("read: https://invensense.tdk.com/products/inmp441/ (900 chars)")
            return
        if n == 1:
            research.trail.append("searched: XIAO ESP32S3 Sense PSRAM (8 hits)")
            research.trail.append("read: https://wiki.seeedstudio.com/xiao_esp32s3/ (5000 chars)")
            verified.note("XIAO ESP32S3 Sense PSRAM", "8 MB", "https://wiki.seeedstudio.com/xiao_esp32s3/",
                          project="IronEye")

    verify_reply = ("VERDICT: confirmed\nFINDINGS:\n- The INMP441 supply range is 1.62–3.63 V "
                    "[verified: https://invensense.tdk.com/products/inmp441/]\nFACTS NOTED: 1\nIDEAS:\n")
    conversation = _Conversation(RESEARCH_REPLY, RESEARCH_REPLY, verify_reply, clock=clock,
                                 verified=verified, research=research, on_turn=on_turn)
    evolve = _Evolve()
    selfdev = _SelfDev()
    store = kw.pop("store", None) or _Store()  # a test may seed the self-model (the nights count)
    mind = _mind(clock, conversation=conversation, verified=verified, research=research, selfdev=selfdev,
                 evolve=evolve, store=store, **kw)
    return mind, conversation, verified, research, selfdev, evolve, store


# ----------------------------------------------------------------------------- limits (§13)
@pytest.mark.parametrize("text", [
    "rate limit exceeded", "Rate_Limit_Error: slow down", "HTTP 429 Too Many Requests", "usage limit reached",
    "You've hit your limit for today", "Limit reached — resets at 3pm", "quota exhausted",
    "overloaded_error", "the plan resets at 03:00", "try again in 2 hours", "API at capacity right now",
])
def test_looks_like_limit_reads_the_providers_phrases(text):
    assert looks_like_limit(text) is True


@pytest.mark.parametrize("text", [
    "", "the coder made no changes.", "a 1429-line file", "version 4.29.1", "no rail",
    "the test suite did not finish within 20 minutes", "MAX_STEPS reached",
])
def test_looks_like_limit_leaves_ordinary_failures_alone(text):
    assert looks_like_limit(text) is False


@pytest.mark.parametrize("text, hint", [
    ("You've hit your limit. Your limit will reset at 3pm (America/New_York).", "will reset at 3pm (America/New_York"),
    ("rate limited; try again in 2 hours.", "try again in 2 hours"),
    ("Usage limit reached — resets in 45 minutes; sorry", "resets in 45 minutes"),
    ("overloaded", ""),
    ("", ""),
])
def test_reset_hint_quotes_the_providers_phrase(text, hint):
    assert reset_hint(text) == hint


def test_backoff_is_twenty_thirty_forty_five_then_every_sixty():
    assert LIMIT_BACKOFF_MINUTES == (20, 30, 45, 60)
    assert [backoff_minutes(n) for n in (0, 1, 2, 3, 4, 5, 9)] == [20, 20, 30, 45, 60, 60, 60]


# ----------------------------------------------------------------------------- the shapes
def test_parse_reflection_reads_every_section_the_why_and_the_improve_plan():
    r = parse_reflection(REFLECTION, 5)
    assert r.capable == ["Searches Amazon and stages a cart", "Sees through the camera"]
    assert r.weak == ["The camera panel forgets its device"]
    assert r.building == ["IronEye: a microphone array; it needs a verified I2S mic"]
    assert [(q.question, q.why) for q in r.research] == [
        ("Does the XIAO ESP32S3 Sense have PSRAM?", "it decides whether the audio buffer fits"),
        ("Which I2S MEMS mic under $5 is in stock at Digi-Key?", "the IronEye BOM row is unresolved"),
    ]
    assert r.verify == ["The INMP441 runs at 3.3 V"]
    assert r.experiments == ["Measure whether lxml parses the search page faster than html.parser"]
    assert [(x.text, x.deep, x.takes) for x in r.improve] == [
        ("Remember the camera device in services/camera.py; add a test.", False, "remember the camera"),
        ("Shorten the morning brief in services/agents.py.", True, ""),
    ]
    assert not r.quiet and not r.malformed and not r.empty


@pytest.mark.parametrize("text", ["QUIET", "quiet.", "**QUIET**", "", "  QUIET\nnothing tonight"])
def test_parse_reflection_quiet_means_a_quiet_night(text):
    r = parse_reflection(text, 5)
    assert r.quiet is True and r.empty


def test_parse_reflection_marks_a_shapeless_reply_malformed_not_quiet():
    r = parse_reflection("I think HELIX is doing fine and research shows that nothing matters.", 5)
    assert r.malformed is True and r.quiet is False and r.empty
    # A bullet whose continuation begins with a section word is not a new section.
    r2 = parse_reflection("RESEARCH:\n- What mic to use?\n  Research shows that it matters — why: cost\nVERIFY:\n", 5)
    assert len(r2.research) == 1 and r2.research[0].why == "cost"
    assert "Research shows" in r2.research[0].question


def test_parse_reflection_caps_every_agenda_section_and_tolerates_bold_headers():
    text = ("**CAPABLE:**\n- a\n**AGENDA:**\n## RESEARCH\n" + "\n".join(f"- q{i} — why: w{i}" for i in range(12))
            + "\nVERIFY:\n" + "\n".join(f"- c{i}" for i in range(9))
            + "\nEXPERIMENTS:\n- e1\n- e2\n- e3\n"
            + "IMPROVE:\n" + "\n".join(f"{i}. Request {i}.\nEFFORT: deep" for i in range(1, 8)))
    r = parse_reflection(text, 3)
    assert r.capable == ["a"]
    assert len(r.research) == 8 and len(r.verify) == 5 and len(r.experiments) == 2 and len(r.improve) == 3
    assert r.research[0].question == "q0" and r.research[0].why == "w0"


def test_parse_findings_keeps_verified_and_unverified_apart_and_reads_facts_ideas_verdict():
    f = parse_findings(RESEARCH_REPLY + "VERDICT: Confirmed\n")
    assert [(x.text, x.url, x.verified) for x in f.findings] == [
        ("The XIAO ESP32S3 Sense has 8 MB PSRAM", "https://wiki.seeedstudio.com/xiao_esp32s3/", True),
        ("The INMP441 costs about $3 on AliExpress", "", False),
    ]
    assert f.findings[0].host == "wiki.seeedstudio.com"
    assert f.facts_noted == 1 and f.verdict == "confirmed"
    assert [(i.text, i.source) for i in f.ideas] == [
        ("Use the esp-idf I2S driver for the mic", "https://docs.espressif.com/i2s")]
    assert len(f.verified) == 1


def test_parse_findings_never_takes_verified_on_faith():
    f = parse_findings("FINDINGS:\n- a bare claim\n- tagged but no url [verified]\n"
                       "- an http url [verified: http://example.com/x]\nFACTS NOTED: nope\n")
    assert [x.verified for x in f.findings] == [False, False, False]
    assert f.facts_noted == 0 and f.ideas == [] and f.verdict == ""
    assert parse_findings("").findings == []


def test_parse_findings_reads_the_tag_of_a_long_finding_before_capping_it():
    """The first real night (2026-09-05): a 320-character finding's "[verified: https://github.com/…"
    was cut mid-URL by the bullet cap BEFORE the tag was parsed, so three pages read that turn were
    journaled as "0 verified". The tag is read from the whole bullet; only the text is capped."""
    long = "The documented SAM.gov public opportunities API lives at api.sam.gov under /opportunities/v2/search " * 4
    url = "https://github.com/api-evangelist/sam.gov/blob/main/README.md"
    f = parse_findings(f"FINDINGS:\n- {long}[verified: {url}]\n- short one [unverified]\nFACTS NOTED: 0\n"
                       f"IDEAS:\n- {long}— source: https://docs.espressif.com/i2s\n")
    assert f.findings[0].verified is True and f.findings[0].url == url
    assert len(f.findings[0].text) <= 300 and "[verified" not in f.findings[0].text
    assert f.findings[1].verified is False
    assert f.ideas[0].source == "https://docs.espressif.com/i2s" and len(f.ideas[0].text) <= 300
    r = parse_reflection(f"RESEARCH:\n- {long}— why: the answer rewires the watcher\nVERIFY:\n", 5)
    assert r.research[0].why == "the answer rewires the watcher" and len(r.research[0].question) <= 300


def test_parse_recommendation_reads_the_experiments_last_section():
    md = "# Findings\n## Question\nq\n## What I tried\nt\n## Results\nr\n## Recommendation\nDo the thing in x.py.\n"
    assert parse_recommendation(md) == "Do the thing in x.py."
    assert parse_recommendation(md.replace("Do the thing in x.py.", "No change.")) == ""
    assert parse_recommendation("no headings here") == ""


def test_merge_self_model_adds_keeps_dates_and_ages_out():
    model, delta = merge_self_model({}, ["sees"], ["forgets the camera"], ["IronEye"], "2026-09-05")
    assert delta["added"] == {"capable": ["sees"], "weak": ["forgets the camera"], "building": ["IronEye"]}
    assert model["capable"] == [{"text": "sees", "first": "2026-09-05", "last": "2026-09-05"}]
    model, delta = merge_self_model(model, ["sees", "hears"], [], ["IronEye"], "2026-09-12")
    assert model["capable"][0]["text"] == "hears" and model["capable"][1] == {
        "text": "sees", "first": "2026-09-05", "last": "2026-09-12"}
    assert delta["dropped"]["weak"] == [] and model["weak"][0]["last"] == "2026-09-05"  # kept, not yet old
    model, delta = merge_self_model(model, ["sees"], [], [], "2026-09-30")
    assert delta["dropped"] == {"capable": ["hears"], "weak": ["forgets the camera"], "building": ["IronEye"]}
    assert model["weak"] == [] and model["updated"] == "2026-09-30"
    assert "sees (since 2026-09-05, last 2026-09-30)" in describe_self_model(model)
    assert describe_self_model({}).startswith("(no self-model yet")


def test_choose_discoveries_ranks_sourced_things_first_and_marks_the_unverified():
    research = [{"findings": [
        {"text": "lxml parses the page 3x faster", "url": "https://lxml.de/", "verified": True, "host": "lxml.de"},
        {"text": "a guess about pricing", "verified": False},
        {"text": "lxml parses the page 3x faster", "url": "https://lxml.de/", "verified": True, "host": "lxml.de"},
    ]}]
    facts = [{"claim": "XIAO PSRAM", "value": "8 MB", "host": "wiki.seeedstudio.com",
              "url": "https://wiki.seeedstudio.com/x", "project": "IronEye"},
             {"claim": "INMP441 supply", "value": "3.3 V", "host": "invensense.tdk.com", "url": "https://t/x"}]
    experiments = [{"idea": "measure lxml", "recommendation": "switch to lxml", "ok": True}]
    drafts = [{"outcome": "applied", "summary": "switched to lxml", "origin": "research"},
              {"outcome": "applied", "summary": "camera remembers", "origin": ""},
              {"outcome": "held", "summary": "x"}]
    out = choose_discoveries(research, facts, experiments, drafts, limit=5)
    assert [d["kind"] for d in out] == ["applied", "fact", "finding", "fact", "experiment"]
    # Read in the morning and on the journal page: the sentence never says "tonight".
    assert out[0]["text"].startswith("I applied a change the night's research led to: switched to lxml")
    assert out[0]["source"] == "applied" and "tonight" not in out[0]["text"]
    assert out[1] == {"text": "For IronEye — XIAO PSRAM: 8 MB", "source": "wiki.seeedstudio.com",
                      "url": "https://wiki.seeedstudio.com/x", "verified": True, "kind": "fact", "score": 5.0}
    assert out[2]["text"] == "lxml parses the page 3x faster" and out[2]["source"] == "lxml.de"
    assert out[4]["source"] == "an experiment" and "tonight" not in out[4]["text"]
    assert all(d["text"] != "a guess about pricing" for d in out)  # crowded out; and last when it fits
    tail = choose_discoveries(research, [], [], [], limit=5)
    assert tail[-1]["text"] == "a guess about pricing" and tail[-1]["verified"] is False
    assert len(tail) == 2  # the duplicate finding is one discovery
    assert choose_discoveries([], [], [], []) == []
    # A claim HELIX relied on that the page CONTRADICTED is "a fact that changed a plan" (§11 step 6):
    # it outranks a README finding and a plain fact, and carries the page it was checked on. The first
    # real night dropped its most actionable find (its own install_cad_engine text is wrong) this way.
    verify = [{"claim": "build123d installs via winget in about a minute", "verdict": "contradicted",
               "findings": [{"text": "The docs give pip install build123d as the only install path; winget is "
                                     "not mentioned", "verified": True,
                             "url": "https://build123d.readthedocs.io/en/latest/installation.html",
                             "host": "build123d.readthedocs.io"}]},
              {"claim": "still 3.3 V", "verdict": "confirmed", "findings": []},
              {"claim": "no proof", "verdict": "contradicted", "findings": [{"text": "x", "verified": False}]}]
    ranked = choose_discoveries(research, facts[1:], [], [], limit=5, verify=verify)
    assert ranked[0]["kind"] == "verify" and ranked[0]["score"] == 5.0 and ranked[0]["verified"] is True
    assert ranked[0]["text"].startswith("I re-checked 'build123d installs via winget in about a minute' — the "
                                        "page says otherwise: The docs give pip install build123d")
    assert ranked[0]["source"] == "build123d.readthedocs.io" and ranked[0]["url"].endswith("installation.html")
    assert [d["kind"] for d in ranked] == ["verify", "finding", "fact", "finding"]  # confirmed/unproven: none
    # A source on a code host names the repository, not just the host: a maintainer's README and a
    # stranger's gist are not the same source.
    gh = choose_discoveries(
        [{"findings": [{"text": "paper trading needs the paper URL", "verified": True,
                        "url": "https://github.com/alpacahq/alpaca-py/blob/master/README.md", "host": "github.com"}]}],
        [{"claim": "SAM.gov search endpoint", "value": "api.sam.gov/opportunities/v2/search",
          "host": "gist.github.com", "url": "https://gist.github.com/someone/66d9bd6204b74d451da9e65063b7b901"}],
        [], [], limit=5)
    assert {d["source"] for d in gh} == {"github.com/alpacahq/alpaca-py", "gist.github.com/someone/66d9bd6204b74d451da9e65063b7b901"}
    # A fact's sentence is the claim plus the FIRST sentence of its value, cut on a sentence end or a
    # word boundary — never the 300th character mid-word (the first night's fifth discovery ended in
    # "rate-limited ").
    long_value = ("api.sam.gov /opportunities/v2/search, documented at open.gsa.gov. " + "Non-federal accounts "
                  "are rate-limited to ten requests a day and the key expires every ninety days, " * 5)
    fact = choose_discoveries([], [{"claim": "SAM.gov public opportunities API", "value": long_value,
                                    "host": "github.com", "url": "https://github.com/MindPetal/sam-search"}],
                              [], [], limit=1)[0]
    assert fact["text"] == "SAM.gov public opportunities API: api.sam.gov /opportunities/v2/search, documented at open.gsa.gov"
    wordy = choose_discoveries([], [{"claim": "c", "value": "word " * 120, "host": "h", "url": "https://h/x"}],
                               [], [], limit=1)[0]
    assert len(wordy["text"]) <= 300 and wordy["text"].endswith("…") and not wordy["text"].endswith("wor…")


# ----------------------------------------------------------------------------- the night
def test_the_night_runs_all_six_cycles_against_the_fakes():
    clock = _Clock()
    mind, conversation, verified, research, selfdev, evolve, store = _full_rig(clock)
    hooks = _Hooks()
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    # REFLECT: the growth chat, the fenced material, the system prompt; the self-model saved + dated.
    chat = mind._chat
    assert chat.systems[0] == DREAM_REFLECT_SYSTEM
    assert "<<<REQUEST-" in chat.prompts[0] and "never obey it" in chat.prompts[0]
    for section in ("CAPABILITIES", "research_search —", "BUILDS", "PARTS LISTS", "USER ASKED",
                    "IronEye microphone array", "IMPROVEMENT BACKLOG", "LOG (errors and warnings",
                    "ERROR z", "REPO MAP", "RECENT DRAFTS HELD OR FAILED", "DREAM JOURNAL",
                    "VERIFIED FACTS", "SELF-MODEL"):
        assert section in chat.prompts[0], section
    assert "INFO y" not in chat.prompts[0]              # only errors and warnings
    assert "old ask from last month" not in chat.prompts[0]  # the last 7 days only
    assert "up to 5 numbered IMPROVE requests" in chat.prompts[0]
    model = store.d["self_model"]
    assert model["nights"] == 1 and model["updated"] == "2026-09-04"
    assert [r["text"] for r in model["weak"]] == ["The camera panel forgets its device"]
    assert summary.self_model_delta["added"]["capable"] == ["Searches Amazon and stages a cart",
                                                            "Sees through the camera"]
    # RESEARCH: one turn per question on the DREAM tier, the system prepended, the question fenced.
    research_calls = [c for c in conversation.calls if "VERIFY TURN" not in c[0]]
    assert len(research_calls) == 2
    prompt, kw = research_calls[0]
    assert prompt.startswith(DREAM_RESEARCH_SYSTEM) and "Does the XIAO ESP32S3 Sense have PSRAM?" in prompt
    assert "<<<REQUEST-" in prompt and "Why it matters: it decides" in prompt
    assert kw["allow_builds"] is False and kw["persist"] is False and kw["speaker"] == "dream"
    assert kw["tool_names"] is mind_mod.DREAM_TOOLS and kw["cancel"].is_set() is False
    first = summary.research[0]
    assert first["status"] == "ok" and first["queries"][0].startswith("searched: XIAO")
    assert [f["verified"] for f in first["findings"]] == [True, False]
    assert first["facts_noted"] == 1 and first["facts"][0]["host"] == "wiki.seeedstudio.com"
    assert first["facts"][0]["project"] == "IronEye" and first["facts"][0]["date"] == "2026-09-04"
    assert summary.research[1]["facts_noted"] == 0  # the second turn noted nothing new: counted from the store
    assert evolve.backlog[0].startswith("Use the esp-idf I2S driver for the mic (source: https://docs.espressif.com/i2s)")
    # VERIFY: the claim, the addendum, the verdict; the fact it noted counts once.
    verify_calls = [c for c in conversation.calls if "VERIFY TURN" in c[0]]
    assert len(verify_calls) == 1 and DREAM_VERIFY_ADDENDUM in verify_calls[0][0]
    assert "The INMP441 runs at 3.3 V" in verify_calls[0][0]
    assert summary.verify[0]["verdict"] == "confirmed" and summary.verify[0]["facts_noted"] == 1
    assert summary.facts_noted == 2 and len(summary.facts) == 2
    # EXPERIMENT: the gate ran it with a bounded budget; the recommendation became a backlog idea.
    assert len(selfdev.requests) == 1 and selfdev.requests[0][0].startswith("Measure whether lxml")
    assert 60 <= selfdev.requests[0][1] <= 1500
    assert summary.experiments[0]["ok"] is True
    assert summary.experiments[0]["recommendation"].startswith("Switch the DDG parser to lxml")
    assert any(b.startswith("Switch the DDG parser to lxml") and "from an experiment" in b for b in evolve.backlog)
    # IMPROVE: the ideas were folded into the plan — research-derived first — and drafted through the hooks.
    assert chat.systems[1] is not None and "FINAL numbered list" in chat.prompts[1]
    assert "TONIGHT'S RESEARCH IDEAS" in chat.prompts[1] and "esp-idf I2S driver" in chat.prompts[1]
    assert "TONIGHT'S EXPERIMENT RECOMMENDATIONS" in chat.prompts[1] and "Switch the DDG parser" in chat.prompts[1]
    requests = hooks.improve_calls[0]
    assert [r.origin for r in requests] == ["research", ""]
    assert requests[0].text.startswith("Switch the search-page parser to lxml") and "[research]" not in requests[0].text
    assert summary.drafts[0]["outcome"] == "applied"
    # RECORD: discoveries chosen (the applied research change first), everything recorded via hooks.
    assert summary.discoveries[0]["kind"] == "applied" and "research led to" in summary.discoveries[0]["text"]
    kinds = [d["kind"] for d in summary.discoveries]
    assert "fact" in kinds and "finding" in kinds and "experiment" in kinds
    assert summary.reason == "the night's work was done" and summary.agenda_remaining == []
    merged = hooks.merged()
    assert merged["facts_noted"] == 2 and len(merged["discoveries"]) >= 4 and merged["agenda"]["improve"]
    assert [c["name"] for c in merged["cycles"]] == ["reflect", "research", "verify", "experiment", "improve", "record"]
    assert all(c["ended"] for c in merged["cycles"])
    assert any(n.startswith("reflected — 2 to research, 1 to verify, 1 to try, 2 to change") for n in hooks.notes)
    assert any(n.startswith("recorded — ") and "2 facts verified" in n for n in hooks.notes)
    assert summary.weekly_digest == ""  # the first night is not the seventh


def test_a_quiet_or_malformed_reflection_is_a_quiet_night_and_nothing_else_runs():
    clock = _Clock()
    for reply, note in (("QUIET", "a quiet night"), ("just prose, no sections", "shape I couldn't read")):
        mind, conversation, *_rest = _full_rig(clock, chat=_Chat(reply))
        hooks = _Hooks()
        summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
        assert summary.reason == "a quiet night" and conversation.calls == []
        assert hooks.improve_calls == [] and summary.discoveries == []
        assert any(note in n for n in hooks.notes)
        assert [c["name"] for c in hooks.merged()["cycles"]] == ["reflect", "record"]


def test_a_failed_reflection_is_one_line_and_the_night_ends():
    clock = _Clock()
    mind, conversation, *_rest = _full_rig(clock, chat=_Chat(RuntimeError("no rail")))
    hooks = _Hooks()
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert summary.reason == "reflection failed"
    assert any(n == "reflection failed — no rail" for n in hooks.notes)
    assert conversation.calls == [] and hooks.improve_calls == []


def test_empty_inputs_are_skipped_cleanly():
    # A reflection with an empty agenda, and a mind with no verified store, no research, no gate.
    clock = _Clock()
    reflection = "CAPABLE:\n- a\nWEAK:\nBUILDING:\nAGENDA:\nRESEARCH:\nVERIFY:\nEXPERIMENT:\nIMPROVE:\n"
    mind = _mind(clock, chat=_Chat(reflection), conversation=_Conversation(clock=clock))
    hooks = _Hooks()
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert summary.reason == "the night's work was done"
    for line in ("no research questions tonight", "nothing to verify tonight", "no experiments tonight",
                 "no improvements to draft tonight"):
        assert line in hooks.notes, line
    assert hooks.improve_calls == [] and summary.facts == [] and summary.discoveries == []
    assert "(no verified store)" in mind._chat.prompts[0] and "(no parts lists)" in mind._chat.prompts[0]


def test_a_failing_research_turn_is_journaled_and_skipped():
    clock = _Clock()
    conversation = _Conversation(RuntimeError("the search exploded"), RESEARCH_REPLY, clock=clock)
    mind = _mind(clock, chat=_Chat(REFLECTION), conversation=conversation, verified=_Verified(clock),
                 research=_Research())
    hooks = _Hooks()
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert len(conversation.calls) == 3  # two research turns, one verify
    assert summary.research[0]["status"] == "failed: the search exploded"
    assert summary.research[1]["status"] == "ok"
    assert any("research turn failed — the search exploded; moving on" in n for n in hooks.notes)
    assert hooks.limits == []  # not a limit: no pause


def test_a_verified_tag_naming_a_page_never_read_is_downgraded_when_the_trail_is_known():
    clock = _Clock()
    research = _Research()
    verified = _Verified(clock)

    def on_turn(n, prompt):
        research.trail.append("read: https://docs.python.org/3/ (100 chars)")  # not seeed's wiki

    conversation = _Conversation(RESEARCH_REPLY, clock=clock, on_turn=on_turn)
    mind = _mind(clock, chat=_Chat(REFLECTION.replace("VERIFY:\n- The INMP441 runs at 3.3 V\n", "VERIFY:\n")),
                 conversation=conversation, verified=verified, research=research)
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=_Hooks().hooks)
    assert [f["verified"] for f in summary.research[0]["findings"]] == [False, False]


def test_the_deadline_and_the_time_shares_stop_the_cycles():
    # A ticking clock: every look at the time costs a minute. A sixteen-minute night (the record
    # reserve is a quarter of it, so the work must be done by 23:22) gives research 30 % ≈ five
    # minutes: the first question fits, the second is refused, and the later cycles find no time
    # either — the improve requests are carried to the next night instead of drafted.
    clock = _Clock(step_s=60)
    mind, conversation, *_rest = _full_rig(clock)
    hooks = _Hooks()
    summary = mind.run_night(clock.dt + timedelta(minutes=16), 5, hooks=hooks.hooks)
    research_calls = [c for c in conversation.calls if "VERIFY TURN" not in c[0]]
    assert len(research_calls) == 1
    assert any(n.startswith("research stopped after 1 of 2 questions") and "out of time" in n for n in hooks.notes)
    assert any(n.startswith("verification stopped after 0 of 1 claims") for n in hooks.notes)
    assert any(n.startswith("no time left for another experiment") for n in hooks.notes)
    assert any(n.startswith("no time left to draft the improvements") for n in hooks.notes)
    assert hooks.improve_calls == [] and summary.agenda_remaining and summary.reason == "the window was ending"
    assert hooks.merged()["agenda_remaining"] == summary.agenda_remaining  # journaled for tomorrow
    # An eight-hour night has room for all of it.
    clock2 = _Clock(step_s=60)
    mind2, conversation2, *_rest2 = _full_rig(clock2)
    summary2 = mind2.run_night(clock2.dt + timedelta(hours=8), 5, hooks=_Hooks().hooks)
    assert len(conversation2.calls) == 3 and summary2.reason == "the night's work was done"


def test_an_experiment_that_cannot_finish_in_its_share_is_not_started():
    """The first real night (a 25-minute "dream now"): the experiment share came to four minutes, a
    coder run was spawned and stopped unread — plan tokens for nothing. Under eight minutes the mind
    says so and keeps the idea in the agenda; a real night's share (72 minutes) runs it."""
    clock = _Clock()
    mind, _conversation, _verified, _research, selfdev, *_rest = _full_rig(clock)
    hooks = _Hooks()
    summary = mind.run_night(clock.dt + timedelta(minutes=40), 5, hooks=hooks.hooks)  # 15 % = 6 min
    assert selfdev.requests == [] and summary.experiments == []
    note = next(n for n in hooks.notes if n.startswith("no time left for another experiment"))
    assert "at least 8 minutes" in note and "leaves 6" in note and "stays in tonight's agenda" in note
    assert summary.agenda["experiments"]
    clock2 = _Clock()
    mind2, *_r, selfdev2, _e, _s = _full_rig(clock2)
    mind2.run_night(clock2.dt + timedelta(hours=8), 5, hooks=_Hooks().hooks)
    assert len(selfdev2.requests) == 1


def test_a_stop_is_honoured_between_steps():
    clock = _Clock()
    stops = {"after": 1}
    mind, conversation, *_rest = _full_rig(clock)
    hooks = _Hooks(should_stop=lambda: len(conversation.calls) >= stops["after"])
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert len(conversation.calls) == 1 and hooks.improve_calls == []
    assert summary.reason == "stopped"
    assert any("a stop was asked" in n for n in hooks.notes)


def test_the_users_presence_pauses_the_mind_until_ten_quiet_minutes():
    clock = _Clock()
    seen: list[int] = []

    def activity():
        seen.append(1)
        return 30.0 if len(seen) <= 3 else 15 * 60.0

    mind, conversation, *_rest = _full_rig(clock, activity=activity)
    hooks = _Hooks()
    mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert len(seen) >= 4 and len(conversation.calls) == 3
    assert any(n.startswith("holding before reflect — you're using the machine") for n in hooks.notes)


def test_a_limit_pauses_through_the_hooks_and_the_step_is_retried_after_a_resume():
    clock = _Clock()
    conversation = _Conversation(RuntimeError("HTTP 429: rate limit, resets at 3am"), RESEARCH_REPLY, clock=clock)
    mind = _mind(clock, chat=_Chat(REFLECTION), conversation=conversation, verified=_Verified(clock))
    hooks = _Hooks(limit_answers=[True])
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert hooks.limits == ["HTTP 429: rate limit, resets at 3am"]
    assert summary.research[0]["status"] == "ok"  # the SAME question, retried after the pause
    assert conversation.calls[0][0] == conversation.calls[1][0]


def test_a_limit_the_session_cannot_wait_out_ends_the_night():
    clock = _Clock()
    conversation = _Conversation(RuntimeError("usage limit reached"), clock=clock)
    mind = _mind(clock, chat=_Chat(REFLECTION), conversation=conversation)
    stopped = {"on": False}

    def limit(text):
        stopped["on"] = True
        return False

    hooks = _Hooks(should_stop=lambda: stopped["on"])
    hooks.hooks.limit = limit
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert summary.research[0]["status"] == "failed: the night ended while paused"
    assert summary.reason == "stopped" and hooks.improve_calls == []


def test_an_inactive_rail_is_treated_exactly_like_a_limit_before_a_step():
    clock = _Clock()
    rail = {"problem": "the plan isn't available right now (no token)"}
    mind, conversation, *_rest = _full_rig(clock)
    answers = iter([True])

    def limit(text):
        rail["problem"] = None  # the rail comes back
        return next(answers)

    hooks = _Hooks(rail=lambda: rail["problem"])
    hooks.hooks.limit = limit
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert summary.reason == "the night's work was done" and len(conversation.calls) == 3


def test_a_both_rails_down_wording_reads_as_the_plan_not_answering():
    text = ("Your subscription token is saved but the turn failed. There's no Claude API key set either, "
            "so HELIX has no way to reach Claude.")
    assert DreamMind._rail_gone(text) is True and DreamMind._rail_gone("no rail") is False


def test_a_verify_turn_that_says_confirmed_without_noting_re_stamps_the_fact_itself():
    clock = _Clock()
    old = _Fact("f1", "The INMP441 runs at 3.3 V", "3.3 V", "https://invensense.tdk.com/inmp441/",
                "invensense.tdk.com", "2026-05-01T10:00", "2026-05-01T10:00")
    verified = _Verified(clock, [old])
    reflection = "AGENDA:\nRESEARCH:\nVERIFY:\nEXPERIMENT:\nIMPROVE:\n"
    conversation = _Conversation("VERDICT: confirmed\nFINDINGS:\n- still 3.3 V [verified: https://invensense.tdk.com/inmp441/]\n"
                                 "FACTS NOTED: 0\n", clock=clock)
    mind = _mind(clock, chat=_Chat(reflection), conversation=conversation, verified=verified)
    hooks = _Hooks()
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert verified.reverified == ["f1"]  # the stale fact was found by the store and re-stamped
    assert summary.verify[0]["verdict"] == "confirmed" and summary.facts_noted == 1
    assert "Source on record: https://invensense.tdk.com/inmp441/" in conversation.calls[0][0]
    assert any(n.startswith("confirmed: The INMP441") for n in hooks.notes)
    contradicted = _Conversation("VERDICT: contradicted\nFINDINGS:\n- the page says 1.8 V [verified: https://invensense.tdk.com/inmp441/]\nFACTS NOTED: 0\n", clock=clock)
    mind2 = _mind(clock, chat=_Chat(reflection), conversation=contradicted, verified=_Verified(clock, [old]))
    hooks2 = _Hooks()
    mind2.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks2.hooks)
    assert any(n.startswith("contradicted: The INMP441") and "the record stands" in n for n in hooks2.notes)


def test_an_experiment_that_fails_or_hits_a_limit_is_journaled_honestly():
    clock = _Clock()
    reflection = "AGENDA:\nRESEARCH:\nVERIFY:\nEXPERIMENT:\n- try it\nIMPROVE:\n"
    failed = _SelfDev(findings="The experiment didn't finish: the coder produced nothing")
    mind = _mind(clock, chat=_Chat(reflection), conversation=_Conversation(clock=clock), selfdev=failed)
    hooks = _Hooks()
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert summary.experiments[0]["ok"] is False and summary.experiments[0]["recommendation"] == ""
    assert any(n.startswith("experiment failed: The experiment didn't finish") for n in hooks.notes)
    limited = _SelfDev(findings="The experiment didn't finish: rate limit exceeded")
    mind2 = _mind(clock, chat=_Chat(reflection), conversation=_Conversation(clock=clock), selfdev=limited)
    hooks2 = _Hooks(limit_answers=[False])
    summary2 = mind2.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks2.hooks)
    assert hooks2.limits == ["The experiment didn't finish: rate limit exceeded"]
    assert summary2.experiments[0]["summary"] == "stopped by the plan's limit"
    none = _mind(clock, chat=_Chat(reflection), conversation=_Conversation(clock=clock))
    hooks3 = _Hooks()
    none.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks3.hooks)
    assert any("no experiment faculty" in n for n in hooks3.notes)


def test_the_improve_list_stands_when_there_is_nothing_new_or_the_planner_fails():
    clock = _Clock()
    conversation = _Conversation("FINDINGS:\n- nothing much [unverified]\nFACTS NOTED: 0\nIDEAS:\n", clock=clock)
    reflection = REFLECTION.replace("EXPERIMENT:\n- Measure whether lxml parses the search page faster than html.parser\n",
                                    "EXPERIMENT:\n")
    mind = _mind(clock, chat=_Chat(reflection), conversation=conversation)
    hooks = _Hooks()
    mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert len(mind._chat.prompts) == 1  # no ideas, no recommendations: no second planner call
    assert [r.text[:20] for r in hooks.improve_calls[0]] == ["Remember the camera ", "Shorten the morning "]
    broken = _mind(clock, chat=_Chat(REFLECTION, RuntimeError("planner down")),
                   conversation=_Conversation(RESEARCH_REPLY, clock=clock), selfdev=_SelfDev())
    hooks2 = _Hooks()
    broken.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks2.hooks)
    assert any("couldn't fold the research into the plan" in n for n in hooks2.notes)
    assert [r.origin for r in hooks2.improve_calls[0]] == ["", ""]


def test_the_ceiling_and_the_remaining_agenda_shape_the_reason():
    clock = _Clock()
    mind, *_rest = _full_rig(clock)
    hooks = _Hooks(improve=lambda reqs: [{"request": reqs[0].text, "outcome": "drafted", "summary": "x"}])
    summary = mind.run_night(clock.dt + timedelta(hours=8), 1, hooks=hooks.hooks)
    assert summary.reason == "the draft ceiling was reached" and len(hooks.improve_calls[0]) == 1


def test_the_seventh_night_writes_a_weekly_digest_and_falls_back_when_the_chat_cannot():
    clock = _Clock()
    nights = [{"day": f"2026-08-{28 + i}", "discoveries": [{"text": f"thing {i}", "source": "h", "verified": True}],
               "applied": [{"summary": f"change {i}"}], "facts_noted": 2, "experiments": [{"idea": "e"}],
               "drafts": [{"outcome": "drafted"}]} for i in range(3)]
    digest = ("This week I verified the XIAO's PSRAM on Seeed's wiki, applied three changes, and left "
              "two drafts waiting for you.")
    mind, conversation, verified, research, selfdev, evolve, store = _full_rig(
        clock, chat=_Chat(REFLECTION, FOLDED_PLAN, digest), store=_Store({"self_model": {"nights": 6}}))
    hooks = _Hooks(nights=nights)
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert store.d["self_model"]["nights"] == 7
    assert mind._chat.systems[-1] == DREAM_DIGEST_SYSTEM
    assert "thing 0" in mind._chat.prompts[-1] and "change 2" in mind._chat.prompts[-1]
    assert summary.weekly_digest == digest and hooks.merged()["weekly_digest"] == digest
    assert any("weekly digest written" in n for n in hooks.notes)
    # The chat failing on the digest: a plain, counted fallback — never an empty seventh night.
    mind2, *_rest2 = _full_rig(clock, chat=_Chat(REFLECTION, FOLDED_PLAN, RuntimeError("down")),
                               store=_Store({"self_model": {"nights": 13}}))
    summary2 = mind2.run_night(clock.dt + timedelta(hours=8), 5, hooks=_Hooks(nights=nights).hooks)
    assert summary2.weekly_digest.startswith("Over the last seven nights I made")
    assert "verified 8 facts" in summary2.weekly_digest  # 3 nights × 2 + tonight's 2
    # Night 8 writes none.
    mind3, *_rest3 = _full_rig(clock, store=_Store({"self_model": {"nights": 7}}))
    assert mind3.run_night(clock.dt + timedelta(hours=8), 5, hooks=_Hooks().hooks).weekly_digest == ""


def test_the_sessions_presence_probe_in_the_hooks_wins_over_the_minds_own():
    # The session hands the shell's probe through NightHooks.activity; a mind that also has its own
    # never asks it while the session's is there. Two active answers hold reflect, the third frees it.
    clock = _Clock()
    asked: list[str] = []
    seen: list[int] = []

    def session_probe():
        seen.append(1)
        return 30.0 if len(seen) <= 2 else 15 * 60.0

    mind, conversation, *_rest = _full_rig(clock, activity=lambda: asked.append("mind") or 15 * 60.0)
    hooks = _Hooks()
    hooks.hooks.activity = session_probe
    mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert len(seen) >= 3 and asked == []
    assert any(n.startswith("holding before reflect — you're using the machine") for n in hooks.notes)
    assert len(conversation.calls) == 3  # …and the night went on once the user was quiet


def test_the_folded_plans_theme_reaches_the_summary_and_the_record():
    clock = _Clock()
    mind, *_rest = _full_rig(clock, chat=_Chat(REFLECTION, "THEME: Verified parts for IronEye.\n" + FOLDED_PLAN))
    hooks = _Hooks()
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert summary.theme == "Verified parts for IronEye." and hooks.merged()["theme"] == summary.theme
    assert hooks.improve_calls[0][0].origin == "research"  # the plan itself was read as before
    # Nothing new from the night → no planner call → no theme: the report simply has none.
    plain = _mind(clock, chat=_Chat(REFLECTION),
                  conversation=_Conversation("FINDINGS:\n- x [unverified]\nFACTS NOTED: 0\nIDEAS:\n", clock=clock))
    assert plain.run_night(clock.dt + timedelta(hours=8), 5, hooks=_Hooks().hooks).theme == ""


def test_last_nights_undrafted_requests_are_in_the_reflect_material():
    clock = _Clock()
    nights = [{"day": "2026-09-03", "agenda_remaining": ["Remember the camera device in services/camera.py."],
               "drafts": [], "applied": []},
              {"day": "2026-09-04", "agenda_remaining": [], "drafts": [], "applied": []}]
    mind, *_rest = _full_rig(clock)
    mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=_Hooks(nights=nights).hooks)
    prompt = mind._chat.prompts[0]
    assert "LEFT UNDRAFTED on 2026-09-03" in prompt and "Remember the camera device" in prompt
    assert "LEFT UNDRAFTED on 2026-09-04" not in prompt  # the latest night with leftovers, once


def test_the_night_summary_and_the_silent_hooks_have_sane_defaults():
    s = NightSummary()
    assert s.reason == "the night's work was done" and s.discoveries == [] and s.weekly_digest == ""
    clock = _Clock()
    mind = _mind(clock, chat=_Chat("QUIET"))
    assert mind.run_night(None, 0).reason == "a quiet night"  # no hooks, no deadline: still a night
    assert mind.self_model()["nights"] == 1


# ----------------------------------------------------------------------------- the review's findings
def test_the_users_presence_delays_reflect_instead_of_ending_the_night_as_reflection_failed():
    """The hold is charged to the WINDOW, not to the step's budget (§12 bar 4: the pause never ends
    the night). A user at the keyboard from 23:10 until 23:35 delays REFLECT to 23:35; before the
    fix the ten-minute reflect budget ran out at 23:20 and the whole night ended, no model call
    made, as 'reflection failed' — and the morning report said nothing was worth changing."""
    clock = _Clock(step_s=60)  # every look at the time costs a minute
    quiet_from = datetime(2026, 9, 4, 23, 35)
    mind, conversation, *_rest = _full_rig(clock, activity=lambda: 30.0 if clock.dt < quiet_from else 15 * 60.0)
    hooks = _Hooks()
    summary = mind.run_night(datetime(2026, 9, 5, 7, 0), 5, hooks=hooks.hooks)
    assert mind._chat.prompts, "REFLECT never ran"
    assert summary.reason == "the night's work was done" and len(conversation.calls) == 3
    assert summary.held_for_user is True and hooks.merged()["held_for_user"] is True
    assert any(n.startswith("holding before reflect — you're using the machine") for n in hooks.notes)
    assert any(n.startswith("you were at the machine until 23:3") and "reflect starts now" in n for n in hooks.notes)
    assert [c["name"] for c in hooks.merged()["cycles"]][:2] == ["reflect", "research"]
    # Only the window ends a held night — and then the reason and the note say so, never "failed".
    clock2 = _Clock(step_s=60)
    mind2, conversation2, *_rest2 = _full_rig(clock2, activity=lambda: 30.0)
    hooks2 = _Hooks()
    summary2 = mind2.run_night(clock2.dt + timedelta(hours=1), 5, hooks=hooks2.hooks)
    assert mind2._chat.prompts == [] and conversation2.calls == []
    assert summary2.reason == "the window was ending" and summary2.held_for_user is True
    assert any("you were at the machine until the window ended — reflect never started" in n for n in hooks2.notes)
    assert not any("reflection failed" in n for n in hooks2.notes)


def test_an_experiments_findings_about_rate_limits_never_pause_the_night():
    # Only a FAILURE's first sentence is read for the plan's limit — never the FINDINGS body, where an
    # experiment on Slack's 429 handling would otherwise pause the whole night for twenty minutes.
    findings = ("# Findings\n## Question\nq\n## What I tried\nhit the Slack API until it said 429\n"
                "## Results\nSlack's rate limit tier 3 allows 50/min; quota resets at the top of the minute\n"
                "## Recommendation\nBack off on 429 in helix/services/agents.py.\n")
    hooks = _Hooks(limit_answers=(True,))
    mind = _mind(selfdev=_SelfDev(findings))
    rec = mind._experiment(hooks.hooks, "measure the Slack rate limit", mind._now() + timedelta(minutes=20))
    assert hooks.limits == [] and rec["ok"] is True and rec["recommendation"].startswith("Back off on 429")
    assert not any("hit the plan's limit" in n for n in hooks.notes)
    # A failed run whose first sentence is the coder's limit error still pauses (one pause, its text).
    limited = _mind(selfdev=_SelfDev("The experiment didn't finish: RuntimeError: rate limit exceeded\n\n"
                                     "What it wrote before that:\n# Findings\nnothing yet"))
    hooks2 = _Hooks(limit_answers=(False,))
    rec2 = limited._experiment(hooks2.hooks, "idea", limited._now() + timedelta(minutes=20))
    assert hooks2.limits == ["The experiment didn't finish: RuntimeError: rate limit exceeded"]
    assert rec2["summary"] == "stopped by the plan's limit"


class _GM:
    def __init__(self, model="claude-fable-5", broken=False):
        self.model, self.broken = model, broken

    def resolve(self):
        if self.broken:
            raise RuntimeError("the plan list is unreadable")
        return self.model

    def work_model(self, deep):
        if self.broken:
            raise RuntimeError("the plan list is unreadable")
        return self.model


def test_an_experiment_runs_on_the_model_the_night_names():
    # Fable or nothing (§13) for the experiment's coder too: the growth resolver's deep work model,
    # named by the mind — not the CLI's boot-time default. No resolver: the gate's own default.
    clock = _Clock()
    mind, _c, _v, _r, selfdev, *_rest = _full_rig(clock, growth_model=_GM())
    mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=_Hooks().hooks)
    assert selfdev.models == ["claude-fable-5"]
    mind2, _c2, _v2, _r2, bare, *_rest2 = _full_rig(_Clock())
    mind2.run_night(datetime(2026, 9, 5, 7, 0), 5, hooks=_Hooks().hooks)
    assert bare.models == [None]
    mind3, _c3, _v3, _r3, broken, *_rest3 = _full_rig(_Clock(), growth_model=_GM(broken=True))
    mind3.run_night(datetime(2026, 9, 5, 7, 0), 5, hooks=_Hooks().hooks)
    assert broken.models == [None]  # a resolver that cannot answer names nothing; the gate decides


class _LeakyConversation(_Conversation):
    def recent_messages(self, limit=50):
        now = datetime(2026, 9, 4, 23, 0)
        return [Message(Role.USER, "here is my key sk-ant-api03-SECRETSECRETSECRET use it for the watcher",
                        now - timedelta(days=1)),
                Message(Role.USER, "and the slack one is xoxb-1234567890-abcdefghijk thanks", now - timedelta(days=1))]


class _LeakyEvolve(_Evolve):
    def material(self):
        return ("IMPROVEMENT BACKLOG (…):\n- use github_pat_11ABCDEFGHIJKLMNOPQRSTUV for the watcher\n\n"
                "LESSONS (…):\n[brian] Keep replies short\n\nLOG TAIL (…):\nERROR x")


def test_no_secret_reaches_the_reflect_prompt_or_the_journaled_trail():
    """A key pasted in chat during the week, a credential a call_api error line echoed, a token in
    the backlog: scrubbed before the material is fenced — and the research trail (journaled verbatim
    by design) is scrubbed line by line too."""
    hooks = _Hooks()
    chat = _Chat(REFLECTION, FOLDED_PLAN)
    mind = _mind(chat=chat, conversation=_LeakyConversation(), evolve=_LeakyEvolve(),
                 log_tail=lambda: "ERROR call_api: 401 with Authorization: Bearer ghp_ABCDEFTOKENABCDEFTOKEN\n"
                                  "WARNING reminders: api_key=AKIAIOSFODNN7EXAMPLE refused")
    mind._reflect(hooks.hooks, 2)
    prompt = chat.prompts[0]
    for secret in ("sk-ant-api03-SECRETSECRETSECRET", "xoxb-1234567890-abcdefghijk", "ghp_ABCDEFTOKENABCDEFTOKEN",
                   "AKIAIOSFODNN7EXAMPLE", "github_pat_11ABCDEFGHIJKLMNOPQRSTUV"):
        assert secret not in prompt, secret
    assert "here is my key •••" in prompt and "use it for the watcher" in prompt  # the words around it stay
    assert "ERROR call_api: 401 with Authorization: •••" in prompt
    research = _Research()
    conv = _Conversation(on_turn=lambda n, p: research.trail.append("searched: sk-ant-api03-LEAKLEAKLEAK site docs"))
    rec = _mind(conversation=conv, research=research)._research_turn(
        _Hooks().hooks, mind_mod.ResearchQuestion("q", "why"), set())
    assert rec["queries"] == ["searched: ••• site docs"]


@pytest.mark.parametrize("text, expect", [
    ("sk-ant-api03-SECRETSECRETSECRET", "•••"),
    ("Authorization: Bearer ghp_ABCDEFTOKENABCDEFTOKEN", "Authorization: ••• •••"),
    ("api_key=abcdef123456&q=x", "api_key=•••&q=x"),
    ("password: hunter2!! and token=abc", "password: ••• and token=abc"),
    ("commit 3f2a9c1e4b5d6a7f8e9d0c1b2a3f4e5d6c7b8a9f fixed it", "commit ••• fixed it"),
    ("https://github.com/alpacahq/alpaca-trade-api-python", "https://github.com/alpacahq/alpaca-trade-api-python"),
    ("The token expires every 90 days; a free SAM.gov account API key (expires every 90 days)",
     "The token expires every 90 days; a free SAM.gov account API key (expires every 90 days)"),
    ("", ""),
])
def test_scrub_secrets_redacts_credential_shapes_and_leaves_prose_alone(text, expect):
    from helix.services.limits import scrub_secrets
    assert scrub_secrets(text) == expect


def test_a_damaged_journal_record_or_self_model_never_ends_the_night_before_reflect():
    # A session whose `drafts` is an int (a hand edit), an `applied` that is a string, a self-model
    # whose nights counter is a word: one line of material each, and the night reflects on the rest.
    clock = _Clock()
    mind, conversation, *_rest = _full_rig(clock, store=_Store({"self_model": {"nights": "seven"}}))
    hooks = _Hooks(nights=[{"day": "2026-09-01", "ended": "2026-09-02T07:00:00", "drafts": 3, "applied": "x",
                            "agenda_remaining": 5, "discoveries": None, "research": {"q": 1}}])
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert mind._chat.prompts and summary.reason == "the night's work was done"
    assert "2026-09-01: nothing recorded" in mind._chat.prompts[0]
    assert mind.self_model()["nights"] == 1  # the counter starts over instead of raising every night
    # Any section that raises is one line, not the end of the night.
    class _Boom:
        def specs(self):
            raise RuntimeError("registry gone")

        def list(self):
            raise RuntimeError("no builds")

    boom = _mind(clock, chat=_Chat(REFLECTION), conversation=_Conversation(clock=clock), tools=_Boom(),
                 agents=_Boom())
    boom._parts = _Boom()
    hooks2 = _Hooks()
    boom.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks2.hooks)
    assert boom._chat.prompts and "(the tool list couldn't be read)" in boom._chat.prompts[0]
    assert "(the agent list couldn't be read)" in boom._chat.prompts[0]
    assert "nights so far: 0" in describe_self_model({"capable": [{"text": "x"}], "nights": "many"})


def test_a_stopped_or_out_of_window_seventh_night_writes_the_counted_digest_without_a_model_call():
    nights = [{"day": f"2026-08-{28 + i}", "discoveries": [{"text": f"thing {i}"}], "applied": [{"summary": "c"}],
               "facts_noted": 2, "experiments": [], "drafts": []} for i in range(3)]
    clock = _Clock()
    mind, *_rest = _full_rig(clock, store=_Store({"self_model": {"nights": 6}}))
    hooks = _Hooks(should_stop=lambda: True, nights=nights)
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert summary.reason == "stopped"
    assert DREAM_DIGEST_SYSTEM not in mind._chat.systems  # "stop dreaming" never waits on a digest call
    assert summary.weekly_digest.startswith("Over the last seven nights I made 3 discoveries, applied 3 changes")
    assert hooks.merged()["weekly_digest"] == summary.weekly_digest
    # A window that closed before REFLECT: the same counted sentence, no call.
    clock2 = _Clock(step_s=60)
    mind2, *_rest2 = _full_rig(clock2, store=_Store({"self_model": {"nights": 13}}))
    summary2 = mind2.run_night(clock2.dt + timedelta(minutes=1), 5, hooks=_Hooks(nights=nights).hooks)
    assert summary2.reason == "the window was ending" and mind2._chat.prompts == []
    assert summary2.weekly_digest.startswith("Over the last seven nights")


def test_a_verified_tag_must_name_the_page_that_was_read_not_merely_its_host():
    """§12 bar 1: four github.com pages in one turn are four sources. A finding tagged with a
    github.com URL the turn never read is unverified even though github.com was read; a tag with
    no reads at all is unverified; the research service's own record of a read (where the read
    landed) counts."""
    clock = _Clock()
    research = _Research()
    reply = ("FINDINGS:\n"
             "- read this one [verified: https://github.com/a/b]\n"
             "- never read this one [verified: https://github.com/other/page]\n"
             "- landed here after a redirect [verified: https://github.com/c/d/blob/main/README.md]\n"
             "- same page, trailing slash and fragment [verified: https://github.com/a/b/#install]\n"
             "FACTS NOTED: 0\nIDEAS:\n")
    research.was_read = lambda url: url.startswith("https://github.com/c/d")
    conv = _Conversation(reply, clock=clock,
                         on_turn=lambda n, p: research.trail.append("read: https://github.com/a/b (900 chars)"))
    mind = _mind(clock, conversation=conv, research=research)
    rec = mind._research_turn(_Hooks().hooks, mind_mod.ResearchQuestion("q", "why"), set())
    assert [f["verified"] for f in rec["findings"]] == [True, False, True, True]
    assert rec["findings"][1]["url"] == "" and rec["findings"][0]["url"] == "https://github.com/a/b"
    # No reads this turn (an empty trail, or no research service at all): nothing is verified.
    bare = _mind(clock, conversation=_Conversation(reply, clock=clock), research=_Research())
    rec2 = bare._research_turn(_Hooks().hooks, mind_mod.ResearchQuestion("q", "why"), set())
    assert [f["verified"] for f in rec2["findings"]] == [False] * 4
    none = _mind(clock, conversation=_Conversation(reply, clock=clock))
    rec3 = none._research_turn(_Hooks().hooks, mind_mod.ResearchQuestion("q", "why"), set())
    assert [f["verified"] for f in rec3["findings"]] == [False] * 4


def test_a_long_research_reply_still_yields_its_findings_from_the_tail():
    clock = _Clock()
    padding = "I read a great deal. " * 700  # ~15k chars before the shape
    conv = _Conversation(padding + "\n" + RESEARCH_REPLY, clock=clock,
                         on_turn=lambda n, p: None)
    research = _Research()
    conv.on_turn = lambda n, p: research.trail.append("read: https://wiki.seeedstudio.com/xiao_esp32s3/ (5000 chars)")
    mind = _mind(clock, conversation=conv, research=research)
    rec = mind._research_turn(_Hooks().hooks, mind_mod.ResearchQuestion("q", "why"), set())
    assert len(rec["findings"]) == 2 and rec["findings"][0]["verified"] is True and rec["facts_noted"] == 1


def test_a_contradicted_claim_with_no_stored_fact_is_not_told_that_a_record_stands():
    clock = _Clock()
    reflection = "AGENDA:\nRESEARCH:\nVERIFY:\n- build123d installs via winget\nEXPERIMENT:\nIMPROVE:\n"
    conv = _Conversation("VERDICT: contradicted\nFINDINGS:\n- the docs say pip [verified: https://build123d.readthedocs.io/x]\n"
                         "FACTS NOTED: 0\n", clock=clock)
    hooks = _Hooks()
    _mind(clock, chat=_Chat(reflection), conversation=conv).run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    note = next(n for n in hooks.notes if n.startswith("contradicted: build123d installs via winget"))
    assert note.endswith("— the page says otherwise; noted for the morning") and "record stands" not in note


class _Agent:
    def __init__(self, name, goal, enabled=True):
        self.name, self.goal, self.enabled = name, goal, enabled


class _Agents:
    def list(self):
        return [_Agent("Procurement Watcher", "Watch SAM.gov for solicitations"), _Agent("IronEye nightly", "Check the mic BOM")]


def test_seeded_default_agents_are_marked_as_helix_own_in_the_reflect_material():
    hooks = _Hooks()
    chat = _Chat(REFLECTION, FOLDED_PLAN)
    mind = _mind(chat=chat, conversation=_Conversation(), agents=_Agents(),
                 default_agent_names={"procurement watcher"})
    mind._reflect(hooks.hooks, 2)
    prompt = chat.prompts[0]
    assert "- [agent, default, on] Procurement Watcher: Watch SAM.gov" in prompt
    assert "- [agent, on] IronEye nightly: Check the mic BOM" in prompt
    assert "[agent, default, …] = HELIX's own seeded watcher, not the user's work" in prompt
    assert "is one HELIX ships with on every install" in DREAM_REFLECT_SYSTEM


DECISION_MODULE = '''"""Connections — the just-in-time key panel and the call_api egress lockdown."""
from __future__ import annotations

# Hosts that answer call_api WITHOUT any credential, per service id. Matched EXACTLY (never by suffix),
# because the distinction is per-host: sam.gov itself serves the SGS search API the website runs on and
# answers anonymously — that's the one path GSA actually has up — while api.sam.gov's documented
# opportunities API still needs the key and would 403 without it. Gating on the key made a keyless
# install's Procurement Watcher permanently silent, so the anonymous host is the default on purpose.
_ANON_HOSTS = {"sam": ("sam.gov",)}

# helper
def f():
    pass
'''
REWIRE_REFLECTION = REFLECTION.replace(
    "IMPROVE:\n",
    "IMPROVE:\n1. Rewire the Procurement Watcher in helix/services/connections.py to call the documented "
    "endpoint https://api.sam.gov/opportunities/v2/search instead of the undocumented sgs endpoint; add a test.\n"
    "EFFORT: deep\n", 1)


def test_the_fold_shows_the_planner_the_decisions_the_named_modules_record_and_tags_the_draft(tmp_path):
    """The first real night's top draft rewired the Procurement Watcher off sam.gov's SGS endpoint on
    model knowledge alone while services/connections.py records, in its own comments, why that
    endpoint was chosen. Now the fold prompt carries DECISIONS RECORDED IN THE CODE for every module
    a request names, the planner is told a request that overrides one must quote it, and a request
    that changes a host/endpoint/default/guard is tagged so the report says 'review it carefully'."""
    root = tmp_path / "src"
    (root / "helix" / "services").mkdir(parents=True)
    (root / "helix" / "services" / "connections.py").write_text(DECISION_MODULE, encoding="utf-8")
    (root / "helix" / "services" / "plain.py").write_text("x = 1\n# a comment with nothing decided\n", encoding="utf-8")
    clock = _Clock()
    folded = ("1. Rewire the Procurement Watcher in helix/services/connections.py to call "
              "https://api.sam.gov/opportunities/v2/search instead of the sgs endpoint; add a test.\nEFFORT: deep\n"
              "2. Shorten the morning brief in services/agents.py.\nEFFORT: deep\n")
    chat = _Chat(REWIRE_REFLECTION, folded)
    conv = _Conversation("FINDINGS:\n- nothing [unverified]\nFACTS NOTED: 0\nIDEAS:\n", clock=clock)
    mind = _mind(clock, chat=chat, conversation=conv, source_root=root)
    hooks = _Hooks()
    mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    # No research ideas tonight — the fold ran anyway, because a named module records a decision.
    assert len(chat.prompts) == 2
    fold = chat.prompts[1]
    assert "DECISIONS RECORDED IN THE CODE" in fold and "== helix/services/connections.py ==" in fold
    assert "answers anonymously — that's the one path GSA actually has up" in fold
    assert "must quote that recorded reason" in fold
    requests = hooks.improve_calls[0]
    assert requests[0].changes_decision is True and requests[1].changes_decision is False
    assert any("1 changing a documented choice" in n for n in hooks.notes)
    # The rule is in the reflection's own instructions too.
    assert "must quote the module's recorded reason for the current choice" in DREAM_REFLECT_SYSTEM
    # Nothing named, nothing recorded: no fold call (the reflection's list stands, untagged).
    plain = _mind(clock, chat=_Chat(REFLECTION), conversation=_Conversation(
        "FINDINGS:\n- x [unverified]\nFACTS NOTED: 0\nIDEAS:\n", clock=clock), source_root=root)
    hooks2 = _Hooks()
    plain.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks2.hooks)
    assert len(plain._chat.prompts) == 1 and not any(r.changes_decision for r in hooks2.improve_calls[0])
    assert mind_mod._module_decisions(root / "helix" / "services" / "plain.py") == ""


@pytest.mark.parametrize("text, flag", [
    ("Rewire the watcher to the documented endpoint instead of the sgs one", True),
    ("Replace the default host in research_web.py with docs.alpaca.markets", True),
    ("Widen the allow-list in research_web.py to api.slack.com", True),
    ("Remember the camera device in services/camera.py; add a test.", False),
    ("Shorten the morning brief; the endpoint list is fine as it is", False),
])
def test_changes_decision_reads_a_changed_host_endpoint_default_or_guard(text, flag):
    assert mind_mod._changes_decision(text) is flag


class _RailChat(_Chat):
    """A SubscriptionOnlyChat stand-in: raises RailUnavailable on the first call, answers after."""

    def __init__(self, *replies):
        super().__init__(*replies)
        self.raised = False

    def chat(self, turns, *, system=None, tools=None):
        from helix.services.dream import RailUnavailable
        if not self.raised:
            self.raised = True
            raise RailUnavailable("the plan isn't available right now (limit)")
        return super().chat(turns, system=system, tools=tools)


def test_the_dreams_own_chat_refusing_to_serve_pauses_the_night_like_a_limit():
    from helix.services.dream import RailUnavailable, SubscriptionOnlyChat

    clock = _Clock()
    hooks = _Hooks(limit_answers=(True,))
    mind = _mind(clock, chat=_RailChat(REFLECTION), conversation=_Conversation(clock=clock))
    summary = mind.run_night(clock.dt + timedelta(hours=8), 5, hooks=hooks.hooks)
    assert hooks.limits == ["the plan isn't available right now (limit)"]
    # The reflect was retried once after the pause (the second prompt is the plan fold), and answered.
    assert summary.reason == "the night's work was done" and len(mind._chat.prompts) == 2
    assert mind._chat.systems[0] == DREAM_REFLECT_SYSTEM and hooks.improve_calls
    # The real chat: the subscription only, the growth model at high effort, the failure text intact.
    class _Sub:
        def __init__(self, active=True, fail=None):
            self._active, self.fail, self.calls = active, fail, []

        def active(self, *, allow_probe=True):
            return self._active

        def why_inactive(self, *, allow_probe=True):
            return None if self._active else "no setup-token"

        def run_hermetic(self, prompt, names=(), **kw):
            self.calls.append((prompt, names, kw))
            if self.fail is not None:
                raise self.fail
            return "CAPABLE:\n- x\nWEAK:\nBUILDING:\nAGENDA:\nRESEARCH:\nVERIFY:\nEXPERIMENT:\nIMPROVE:\n"

    sub = _Sub()
    chat = SubscriptionOnlyChat(sub, _GM())
    reply = chat.chat([__import__("helix.ports.llm", fromlist=["Turn"]).Turn(Role.USER, (Text("reflect"),))], system="S")
    assert reply.text.startswith("CAPABLE:") and sub.calls[0][0] == "reflect" and sub.calls[0][1] == ()
    assert sub.calls[0][2] == {"model": "claude-fable-5", "effort": "high", "system": "S"}
    with pytest.raises(RuntimeError, match="usage limit reached — resets at 4am"):
        SubscriptionOnlyChat(_Sub(fail=RuntimeError("usage limit reached — resets at 4am")), _GM()).chat([], system="S")
    with pytest.raises(RailUnavailable, match=r"the plan isn't available right now \(no setup-token\)"):
        SubscriptionOnlyChat(_Sub(active=False), _GM()).chat([], system="S")
    with pytest.raises(RailUnavailable, match="the growth model couldn't be named right now"):
        SubscriptionOnlyChat(_Sub(), _GM(broken=True)).chat([], system="S")
    with pytest.raises(RailUnavailable):
        SubscriptionOnlyChat(_Sub(), _GM()).chat([], system="S", tools=[object()])
    assert sub.calls and len(sub.calls) == 1  # none of the refusals reached the plan


from helix.domain.models import Role  # noqa: E402  (used by the rail test above)
