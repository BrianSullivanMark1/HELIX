"""LocationService — named places, current selection, per-speaker isolation, and the injected block."""
from __future__ import annotations

from helix.services.location import LocationService


class _Store:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


def _svc():
    return LocationService(_Store())


def test_set_and_current_and_context():
    s = _svc()
    s.set_place("123 Main St, Springfield IL", "home")
    assert s.current() == ("home", "123 Main St, Springfield IL")
    ctx = s.context()
    assert "123 Main St" in ctx and "home" in ctx
    assert "web" in ctx.lower() and "never invent" in ctx.lower()


def test_empty_context_when_nothing_set():
    assert _svc().context() == ""


def test_multiple_places_and_switching_current():
    s = _svc()
    s.set_place("1 Home Rd", "home")
    s.set_place("9 Shop Ave", "shop")   # adding a place makes it current
    assert s.current() == ("shop", "9 Shop Ave")
    assert s.set_current("home").lower().startswith("okay")
    assert s.current() == ("home", "1 Home Rd")
    ctx = s.context()
    assert "Other saved places" in ctx and "shop" in ctx


def test_per_speaker_isolation():
    s = _svc()
    s.set_place("Brian's place", "home", user="brian")
    assert s.current(user="brian") == ("home", "Brian's place")
    assert s.current(user="sarah") is None          # Sarah has her own (empty) store
    assert s.context(user="sarah") == ""
    s.set_place("Sarah's place", "home", user="sarah")
    assert s.current(user="sarah") == ("home", "Sarah's place")
    assert s.current(user="brian") == ("home", "Brian's place")  # unaffected


def test_remove_place():
    s = _svc()
    s.set_place("1 Home Rd", "home")
    s.set_place("9 Shop Ave", "shop")
    assert s.remove("shop")
    assert "shop" not in s.places()
    assert not s.remove("nope")


def test_single_place_is_treated_as_current_without_explicit_selection():
    s = _svc()
    s.set_place("Only Place", "cabin")
    # even if the 'current' pointer were lost, a lone place is used
    assert s.current() == ("cabin", "Only Place")
