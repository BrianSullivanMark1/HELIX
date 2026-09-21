"""Tell a joke - the easy way to hear the voice and see the face act (Brian, 2026-09-22).

  GET /api/joke            -> {"joke": "...", "source": "jokeapi" | "dadjoke" | "helix"}

Pulled from JokeAPI (light adult humour allowed, the filthy and the hateful filtered out with its
own blacklist flags), then icanhazdadjoke, then a handful HELIX keeps for when the internet is
out. Never raises; never blocks the loop (the fetch runs on a thread with a short timeout).
"""
from __future__ import annotations

import asyncio
import json
import random
import urllib.request

from fastapi import FastAPI

JOKEAPI = "https://v2.jokeapi.dev/joke/Any?blacklistFlags=racist,sexist,explicit,political&type=single,twopart"
DADJOKE = "https://icanhazdadjoke.com/"
UA = "HELIX (Mark 1; a desktop assistant that tells its team jokes)"

OFFLINE = [
    "I told the fleet a joke about UDP. I am not sure it got it.",
    "Why did the deploy go to therapy? Too many unresolved dependencies.",
    "I would tell you a joke about a rollback, but you have probably heard it before. Twice.",
    "My database and I have a healthy relationship. We both know it is read-only in prod.",
    "A SQL query walks into a bar, sees two tables, and asks: may I join you?",
    "I asked the QA environment how it was doing. It said: not deployed, but emotionally available.",
]


def _get(url: str, headers: dict, timeout: float = 6.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_joke(get=_get) -> dict:
    """One joke as {joke, source}. Tries the web, then HELIX's own few."""
    try:
        doc = json.loads(get(JOKEAPI, {"Accept": "application/json"}))
        if not doc.get("error"):
            if doc.get("type") == "twopart":
                text = f"{doc.get('setup', '').strip()} ... {doc.get('delivery', '').strip()}"
            else:
                text = str(doc.get("joke", "")).strip()
            if text:
                return {"joke": " ".join(text.split()), "source": "jokeapi"}
    except Exception:  # noqa: BLE001
        pass
    try:
        doc = json.loads(get(DADJOKE, {"Accept": "application/json"}))
        text = str(doc.get("joke", "")).strip()
        if text:
            return {"joke": " ".join(text.split()), "source": "dadjoke"}
    except Exception:  # noqa: BLE001
        pass
    return {"joke": random.choice(OFFLINE), "source": "helix"}


def mount_jokes(app: FastAPI) -> None:
    @app.get("/api/joke")
    async def joke():
        return await asyncio.to_thread(fetch_joke)
