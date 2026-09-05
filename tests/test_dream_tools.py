"""The dream tools — HELIX's face for the nightly self-improvement session (READ_ME/DREAM.md §7),
driven through the ToolRegistry against a fake DreamService.

The engine (services/dream.py) is built by the other workstream; these pin the contract THIS side
keeps: the four tools appear only once the engine is attached (late-bound, like Evolve), each one
routes to the engine's method with the contract's arguments (partial schedules, coerced numbers),
the three controls are fenced from autonomous runs, and the one readable tool never hands a
watcher the name of a fenced tool — not even when the engine's own status text does.
"""
from __future__ import annotations

from helix.domain.vocabulary import friendly_tool_label
from helix.services.conversation import BUILD_TOOLS
from helix.services.tools import ToolRegistry

DREAM_TOOLS = ("dream_schedule", "dream_now", "stop_dreaming", "dream_status")
FENCED = ("dream_schedule", "dream_now", "stop_dreaming")


class _Dream:
    """The DreamService surface the registry touches (DREAM.md §4), recording every call."""

    def __init__(self, status: str = ("Dreaming nightly from 23:00 for 8 hours. Next session "
                                      "tonight at 23:00. Plans and drafts on claude-fable-5.")):
        self.calls: list[tuple] = []
        self.status_text = status
        self.running = False
        self.schedule_error: str | None = None

    def schedule(self, *, start=None, hours=None, enabled=None):
        self.calls.append(("schedule", start, hours, enabled))
        if self.schedule_error:
            raise ValueError(self.schedule_error)
        return "Dreaming nightly from 23:00 for 8 hours."

    def dream_now(self, minutes=30):
        self.calls.append(("dream_now", minutes))
        self.running = True
        return f"Dreaming for {minutes:g} minutes — I'll report when I wind down."

    def stop(self, reason="the user asked"):
        self.calls.append(("stop", reason))
        self.running = False
        return "Stopped dreaming; the session's summary is written."

    def status(self):
        return self.status_text


def _registry(dream=None) -> ToolRegistry:
    reg = ToolRegistry(None, None)
    if dream is not None:
        reg.attach_dream(dream)
    return reg


# ----- offered late-bound, like Evolve -----

def test_the_dream_tools_appear_only_once_the_engine_is_attached():
    bare = _registry()
    assert not set(DREAM_TOOLS) & {t.name for t in bare.specs()}
    assert _registry().dispatch("dream_status", {}).startswith("Unknown tool")
    reg = _registry(_Dream())
    assert set(DREAM_TOOLS) <= {t.name for t in reg.specs()}


# ----- dream_schedule -----

def test_dream_schedule_routes_the_contracts_keywords_and_coerces_the_models_text():
    dream = _Dream()
    out = _registry(dream).dispatch(
        "dream_schedule", {"start": "23:00", "hours": "8", "enabled": "true"}
    )
    assert dream.calls == [("schedule", "23:00", 8.0, True)]
    assert out == "Dreaming nightly from 23:00 for 8 hours."


def test_a_partial_schedule_leaves_the_unnamed_fields_alone():
    # "no dreaming tonight" must not reset the saved start time or hours: those arrive as None and
    # the engine keeps what it has. Same for "dream for six hours" — the start time stays.
    dream = _Dream()
    _registry(dream).dispatch("dream_schedule", {"enabled": False})
    assert dream.calls == [("schedule", None, None, False)]
    dream.calls.clear()
    _registry(dream).dispatch("dream_schedule", {"hours": 6})
    assert dream.calls == [("schedule", None, 6.0, None)]


def test_an_empty_schedule_call_asks_instead_of_touching_the_engine():
    dream = _Dream()
    out = _registry(dream).dispatch("dream_schedule", {})
    assert dream.calls == [] and "Nothing to change" in out


def test_garbage_arguments_read_as_absent_not_as_a_tool_error():
    # A model that writes "eight" or "maybe" gets asked again; the engine is never called with junk.
    dream = _Dream()
    out = _registry(dream).dispatch(
        "dream_schedule", {"hours": "eight", "enabled": "maybe", "start": "   "}
    )
    assert dream.calls == [] and "Nothing to change" in out


def test_the_engines_own_validation_is_said_plainly():
    dream = _Dream()
    dream.schedule_error = "hours must be between 1 and 12"
    out = _registry(dream).dispatch("dream_schedule", {"hours": 40})
    assert out.startswith("I couldn't set that schedule") and "1 and 12" in out


# ----- dream_now / stop_dreaming -----

def test_dream_now_defaults_to_half_an_hour_and_takes_the_users_minutes():
    dream = _Dream()
    reg = _registry(dream)
    reg.dispatch("dream_now", {})
    reg.dispatch("dream_now", {"minutes": "60"})
    reg.dispatch("dream_now", {"minutes": "soon"})
    assert [c[1] for c in dream.calls] == [30.0, 60.0, 30.0]
    assert dream.running


def test_stop_dreaming_stops_with_the_users_reason():
    dream = _Dream()
    dream.running = True
    out = _registry(dream).dispatch("stop_dreaming", {})
    assert dream.calls == [("stop", "the user asked")]
    assert "Stopped" in out and not dream.running


# ----- dream_status (the read) -----

def test_dream_status_reads_the_engine_and_names_the_model():
    out = _registry(_Dream()).dispatch("dream_status", {})
    assert "23:00" in out and "claude-fable-5" in out


def test_a_clean_status_passes_through_untouched():
    text = "Dreaming nightly from 23:00 for 8 hours. Last night: 3 drafted, 2 applied."
    assert _registry(_Dream(status=text)).dispatch("dream_status", {}) == text


def test_dream_status_never_hands_a_watcher_a_fenced_tools_name():
    # A watcher may read dream_status; its text must not coach it toward dream_now / stop_dreaming
    # / dream_schedule. The engine's status() is written that way, and the wrapper enforces it
    # even if a future status line slips — same rule search_amazon and show_parts keep.
    dream = _Dream(status="Not dreaming. Use dream_now to start one, stop_dreaming to end it, "
                          "dream_schedule to book a night.")
    out = _registry(dream).dispatch("dream_status", {})
    for name in FENCED:
        assert name not in out, out
    assert "Not dreaming" in out  # the substance survives the scrub


# ----- the fence and the voice -----

def test_the_controls_are_fenced_and_the_read_is_not():
    for name in FENCED:
        assert name in BUILD_TOOLS, f"{name} must be in BUILD_TOOLS — it books/cuts unattended self-editing"
    assert "dream_status" not in BUILD_TOOLS


def test_every_dream_tool_has_a_spoken_phrase():
    for name in DREAM_TOOLS:
        label = friendly_tool_label(name)
        assert label != "Working…" and "_" not in label, name


def test_the_specs_teach_the_shapes_and_advertise_the_read_as_read_only():
    specs = {t.name: t for t in _registry(_Dream()).specs()}
    assert "READ-ONLY" in specs["dream_status"].description
    assert "how did you sleep" in specs["dream_status"].description
    assert "minutes" in specs["dream_now"].input_schema["properties"]
    for field in ("start", "hours", "enabled"):
        assert field in specs["dream_schedule"].input_schema["properties"]
    assert "no dreaming tonight" in specs["dream_schedule"].description
    assert specs["stop_dreaming"].input_schema["properties"] == {}
    for name in DREAM_TOOLS:
        assert specs[name].input_schema["additionalProperties"] is False
