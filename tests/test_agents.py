"""AgentService.rename — renames in place, preserves goal + order, refuses collisions."""
from __future__ import annotations

from helix.services.agents import AgentService


class _FakeSettings:
    def __init__(self) -> None:
        self._d: dict = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value) -> None:
        self._d[key] = value


def _svc() -> AgentService:
    return AgentService(_FakeSettings(), None)  # conversation unused by rename


def test_agent_rename_keeps_goal_and_order():
    s = _svc()
    s.add("Morning brief", "summarize the news")
    s.add("Cleanup", "tidy the files")
    out = s.rename("Morning brief", "Daily brief")
    assert out is not None and out.name == "Daily brief" and out.goal == "summarize the news"
    assert [a.name for a in s.list()] == ["Daily brief", "Cleanup"]  # position preserved


def test_agent_rename_refuses_collision():
    s = _svc()
    s.add("A", "g1")
    s.add("B", "g2")
    assert s.rename("B", "A") is None
    assert {a.name for a in s.list()} == {"A", "B"}


def test_agent_rename_blank_or_missing_returns_none():
    s = _svc()
    s.add("A", "g")
    assert s.rename("A", "   ") is None
    assert s.rename("nope", "X") is None
    assert [a.name for a in s.list()] == ["A"]
