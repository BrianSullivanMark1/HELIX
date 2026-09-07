"""RecommendService — the usage ledger and the Suggested strip's ranking."""
from __future__ import annotations

from datetime import datetime, timedelta

from helix.services.recommend import RecommendService


class _Store:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


class _Clock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t


class _App:
    def __init__(self, slug, name):
        self.slug = slug
        self.name = name


def _apps(*slugs):
    return [_App(s, s.title()) for s in slugs]


def test_records_and_suggests_most_used():
    s = RecommendService(_Store(), _Clock(datetime(2026, 7, 14).astimezone()))
    for _ in range(3):
        s.record_open("timer")
    s.record_run("cleanup")
    sugg = s.suggestions(_apps("timer", "cleanup"), limit=2)
    slugs = [a.slug for a, _ in sugg]
    assert slugs[0] == "timer"  # most-used first
    assert "used 3×" in dict((a.slug, r) for a, r in sugg)["timer"]


def test_neglected_build_is_resurfaced():
    clk = _Clock(datetime(2026, 1, 1).astimezone())
    s = RecommendService(_Store(), clk)
    s.record_open("old")                       # last used Jan 1
    clk.t = datetime(2026, 1, 1).astimezone() + timedelta(days=31)
    s.record_open("fresh")
    s.record_open("fresh")                     # more recent + more used
    sugg = dict((a.slug, r) for a, r in s.suggestions(_apps("old", "fresh"), limit=2))
    assert "fresh" in sugg
    assert "old" in sugg and "days" in sugg["old"]  # surfaced as neglected


def test_deleted_builds_drop_out():
    s = RecommendService(_Store(), _Clock(datetime(2026, 7, 14).astimezone()))
    s.record_open("gone")
    s.record_open("here")
    sugg = [a.slug for a, _ in s.suggestions(_apps("here"))]  # 'gone' no longer in the build list
    assert sugg == ["here"]


def test_no_usage_means_no_suggestions():
    s = RecommendService(_Store(), _Clock(datetime(2026, 7, 14).astimezone()))
    assert s.suggestions(_apps("a", "b")) == []
