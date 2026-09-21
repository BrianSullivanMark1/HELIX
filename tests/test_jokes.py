"""Tell a joke: the web first (two shapes), the dad-joke fallback, HELIX's own when offline; never raises."""
from __future__ import annotations

import json

from helix.api import joke_routes as jr


def test_jokeapi_single_and_twopart():
    single = lambda url, h: json.dumps({"error": False, "type": "single", "joke": "A joke.\n  With  spaces."})  # noqa: E731
    assert jr.fetch_joke(single) == {"joke": "A joke. With spaces.", "source": "jokeapi"}
    two = lambda url, h: json.dumps({"error": False, "type": "twopart", "setup": "Why?", "delivery": "Because."})  # noqa: E731
    assert jr.fetch_joke(two)["joke"] == "Why? ... Because."


def test_falls_back_to_dadjoke_then_helix():
    calls = []

    def get(url, h):
        calls.append(url)
        if "jokeapi" in url:
            raise OSError("down")
        return json.dumps({"joke": "Dad."})
    assert jr.fetch_joke(get) == {"joke": "Dad.", "source": "dadjoke"} and len(calls) == 2

    def dead(url, h):
        raise OSError("no internet")
    got = jr.fetch_joke(dead)
    assert got["source"] == "helix" and got["joke"] in jr.OFFLINE


def test_the_route_answers_and_the_blacklist_is_in_the_url():
    from fastapi import FastAPI
    from tests.test_fleet_routes import _call
    app = FastAPI(); jr.mount_jokes(app)
    st, doc = _call(app, "GET", "/api/joke")
    assert st == 200 and doc["joke"] and doc["source"] in ("jokeapi", "dadjoke", "helix")
    assert "blacklistFlags=racist,sexist,explicit,political" in jr.JOKEAPI
