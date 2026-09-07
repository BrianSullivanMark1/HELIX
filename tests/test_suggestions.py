"""SuggestionService — the ANTICIPATE producer: one useful candidate, priority-ordered."""
from __future__ import annotations

from helix.services.suggestions import SuggestionService


class _App:
    def __init__(self, slug, name):
        self.slug, self.name = slug, name


class _Builds:
    def __init__(self, apps):
        self._apps = apps

    def list(self):
        return list(self._apps)


class _Recommend:
    def __init__(self, sugg):
        self._sugg = sugg

    def suggestions(self, apps, limit=3):
        return self._sugg


class _Pending:
    def __init__(self, id, summary):
        self.id, self.summary = id, summary


class _Selfdev:
    def __init__(self, pending):
        self._pending = pending

    def pending(self):
        return list(self._pending)


def test_pending_self_change_wins():
    app = _App("timer", "Timer")
    s = SuggestionService(
        recommend=_Recommend([(app, "not opened in 20 days")]),
        builds=_Builds([app]),
        selfdev=_Selfdev([_Pending("sd1", "make the orb bigger")]),
    )
    c = s.candidate()
    assert c is not None and c.id == "selfchange:sd1" and "apply it" in c.text


def test_neglected_build_when_no_pending_change():
    app = _App("timer", "Timer")
    s = SuggestionService(
        recommend=_Recommend([(app, "not opened in 20 days")]),
        builds=_Builds([app]),
        selfdev=_Selfdev([]),
    )
    c = s.candidate()
    assert c is not None and c.id == "neglected:timer" and c.open_slug == "timer"
    assert "Timer" in c.text


def test_nothing_to_suggest():
    app = _App("timer", "Timer")
    s = SuggestionService(
        recommend=_Recommend([(app, "used 5×")]),  # not neglected
        builds=_Builds([app]),
        selfdev=_Selfdev([]),
    )
    assert s.candidate() is None


def test_degrades_with_no_services():
    assert SuggestionService().candidate() is None
