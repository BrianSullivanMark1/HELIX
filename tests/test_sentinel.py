"""The sentinel — default watchers, quiet-unless-notable reporting, and SAM.gov query auth.

Invariants: the five default watchers seed exactly once (a deleted watcher never resurrects, a
user's same-named agent is never clobbered); every watcher goal carries the QUIET convention and a
real schedule; scheduled reports that say QUIET (or nothing) are never announced while real findings
are; failures are logged, not spoken; SAM.gov's api_key is attached server-side as a query parameter
(replacing any model-guessed value) and the key value never leaks back to the model.
"""
from __future__ import annotations

import pytest

from helix.app.container import _DEFAULT_WATCHERS, _WATCHERS_SEED_VERSION, _seed_watchers
from helix.domain.connections import KNOWN_SERVICES, service_for_url
from helix.services.agents import AgentService
from helix.services.connections import ConnectionsService


class _Store:
    def __init__(self):
        self._d = {}

    def get(self, k, default=None):
        return self._d.get(k, default)

    def set(self, k, v):
        self._d[k] = v


# ----- seeding -----

def test_watchers_seed_once_with_schedules():
    store = _Store()
    agents = AgentService(store, None)
    _seed_watchers(store, agents)
    names = [a.name for a in agents.list()]
    assert names[0] == "Morning Brief"  # first in heartbeat order at 8:00
    assert len(names) == len(_DEFAULT_WATCHERS) == 5
    for a in agents.list():
        assert a.schedule is not None, f"{a.name} has no schedule"
        assert a.last_run, f"{a.name} would fire immediately at first boot"
    brief = next(a for a in agents.list() if a.name == "Morning Brief")
    assert brief.schedule == {"kind": "daily", "at": "08:00"}
    assert store.get("watchers_seed_version") == _WATCHERS_SEED_VERSION


def test_deleted_watcher_never_resurrects():
    store = _Store()
    agents = AgentService(store, None)
    _seed_watchers(store, agents)
    agents.remove("Slack Watcher")
    _seed_watchers(store, agents)  # next launch
    assert "Slack Watcher" not in [a.name for a in agents.list()]


def test_seeding_never_clobbers_a_users_agent():
    store = _Store()
    agents = AgentService(store, None)
    agents.add("Morning Brief", "my own custom brief goal")
    _seed_watchers(store, agents)
    brief = next(a for a in agents.list() if a.name == "Morning Brief")
    assert brief.goal == "my own custom brief goal"


def test_watcher_goals_carry_the_quiet_convention():
    for name, goal, _hint in _DEFAULT_WATCHERS:
        if name != "Morning Brief":  # the brief always speaks — that's its job
            assert "QUIET" in goal, f"{name} lacks the quiet convention"
        assert "call_api" in goal
        assert "six tool calls" in goal, f"{name} lacks the tool-budget rule"


def test_retune_by_any_case_replaces_never_duplicates():
    store = _Store()
    agents = AgentService(store, None)
    _seed_watchers(store, agents)
    agents.add("github watcher", "new goal for the watcher, checked every 2 hours")
    matches = [a for a in agents.list() if a.name.strip().lower() == "github watcher"]
    assert len(matches) == 1
    assert matches[0].goal.startswith("new goal")


def test_retune_preserves_a_paused_watchers_pause():
    store = _Store()
    agents = AgentService(store, None)
    _seed_watchers(store, agents)
    agents.set_enabled("Slack Watcher", False)
    agents.add("Slack Watcher", "retuned goal, every 30 minutes")
    slack = next(a for a in agents.list() if a.name.strip().lower() == "slack watcher")
    assert slack.enabled is False  # a retune must never silently resume a paused watcher


# ----- the quiet filter (shell seam) -----

def test_scheduled_quiet_reports_are_never_spoken():
    os_qt = pytest.importorskip("PyQt6.QtWidgets")  # noqa: F841 — shell-level check needs Qt types
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])

    from helix.ui.main_window import HelixMainWindow

    class _Console:
        def __init__(self):
            self.announced = []
            self.status = type("S", (), {"setText": lambda _s, t: None})()

        def announce_agent_report(self, name, report):
            self.announced.append((name, report))

    w = HelixMainWindow.__new__(HelixMainWindow)  # the seam only needs .console
    w.console = _Console()
    w._on_scheduled_report("Slack Watcher", "QUIET")
    w._on_scheduled_report("Slack Watcher", "quiet.")
    w._on_scheduled_report("Slack Watcher", "")
    w._on_scheduled_report("Slack Watcher", None)
    # near-compliance garnish still counts as quiet — the model won't always obey 'exactly'
    w._on_scheduled_report("Slack Watcher", "QUIET — nothing new matched.")
    w._on_scheduled_report("Slack Watcher", "**QUIET**")
    # known non-reports (renamed agent mid-flight, tool-budget stall) are never spoken either
    w._on_scheduled_report("Slack Watcher", "No agent named 'Slack Watcher'.")
    w._on_scheduled_report("Slack Watcher", "I got stuck — could you rephrase?")
    assert w.console.announced == []
    w._on_scheduled_report("Slack Watcher", "Kate flagged the BRMS deploy as blocked.")
    assert w.console.announced == [("Slack Watcher", "Kate flagged the BRMS deploy as blocked.")]
    # a real finding mentioning quiet mid-sentence still speaks
    w._on_scheduled_report("GitHub Watcher", "Thoa has gone quiet on BRMS for two days.")
    assert len(w.console.announced) == 2
    w._on_scheduled_failure("Slack Watcher", "boom")  # logged + status only, never announced
    assert len(w.console.announced) == 2


# ----- SAM.gov: query auth + scrubbing -----

class _Secrets(_Store):
    pass


def test_sam_is_a_known_service():
    svc = service_for_url("https://api.sam.gov/opportunities/v2/search?limit=5")
    assert svc is not None and svc.id == "sam"
    assert svc.query and svc.query[0][0] == "api_key"
    assert not svc.auth  # query-param auth only


def test_sam_key_is_attached_server_side_and_model_value_is_replaced(monkeypatch):
    svc = ConnectionsService(None, _Secrets())
    svc.set_value("SAM_API_KEY", "sekrit-123")
    captured = {}

    class _Resp:
        def read(self, _n):
            return b'{"ok": true, "echo": "sekrit-123"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=0):
        captured["url"] = req.full_url
        return _Resp()

    import helix.services.connections as mod

    monkeypatch.setattr(mod._OPENER, "open", fake_open)
    out = svc.call_api("https://api.sam.gov/opportunities/v2/search?limit=5&api_key=model-guess")
    assert "api_key=sekrit-123" in captured["url"]      # stored key attached server-side
    assert "model-guess" not in captured["url"]         # a guessed param never survives
    assert "limit=5" in captured["url"]                 # the real query params do
    assert "sekrit-123" not in out                      # an echoing body is scrubbed
    assert "•••" in out


def test_unconnected_sam_returns_friendly_string():
    svc = ConnectionsService(None, _Secrets())
    out = svc.call_api("https://api.sam.gov/opportunities/v2/search?limit=5")
    assert "isn't connected yet" in out and "SAM.gov" in out


def test_scrub_covers_plus_and_percent_encodings():
    svc = ConnectionsService(None, _Secrets())
    svc.set_value("SAM_API_KEY", "ab cd/ef")
    sam = service_for_url("https://api.sam.gov/x")
    out = svc._scrub(sam, "raw=ab cd/ef plus=ab+cd%2Fef pct=ab%20cd%2Fef")
    assert "ab cd/ef" not in out and "ab+cd%2Fef" not in out and "ab%20cd%2Fef" not in out


def test_failure_log_line_is_scrubbed(monkeypatch, caplog):
    # The _LOG.warning on a failed call embeds str(e), which can carry the full request URL — the
    # scrub-before-log is the only thing keeping the SAM key out of helix.log. Pin it.
    import logging

    import helix.services.connections as mod

    svc = ConnectionsService(None, _Secrets())
    svc.set_value("SAM_API_KEY", "sekrit-789")

    def exploding_open(req, timeout=0):
        raise RuntimeError(f"boom at {req.full_url}")

    monkeypatch.setattr(mod._OPENER, "open", exploding_open)
    with caplog.at_level(logging.WARNING, logger="helix.connections"):
        out = svc.call_api("https://api.sam.gov/opportunities/v2/search?limit=5")
    assert "sekrit-789" not in out
    assert "sekrit-789" not in caplog.text
