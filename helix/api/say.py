"""The test line - HELIX says a typed sentence out loud, no model involved.

  POST /api/say  {"text": "..."}

The line lands in the transcript as a HELIX bubble (so the face gets the words to act out),
then the voice speaks it through the real speak path - the orb goes 'speaking' and back to
'idle' exactly as it does for a model reply. Without a voice (or a voice whose TTS is not
available) the orb states are pushed by a timer instead, at reading pace, so the face still
performs the line. Human-only by construction: an HTTP route on the local face, never a tool.
Mounted by server.build_app with `mount_say(app, shell)`.
"""
from __future__ import annotations

import re
import threading
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MAX_CHARS = 600
WORDS_PER_SECOND = 2.6   # the pace the face reads at when nothing is actually speaking
MIN_SECONDS = 1.2


def seconds_for(text: str) -> float:
    words = len(re.findall(r"\S+", text))
    return max(MIN_SECONDS, words / WORDS_PER_SECOND + 0.6)


def say(shell, text: str) -> dict:
    """Bubble the line, then speak it (or mime it). Returns how it was delivered."""
    text = (text or "").strip()[:MAX_CHARS]
    if not text:
        return {"ok": False, "error": "Nothing to say."}
    shell.push({"t": "msg", "id": uuid.uuid4().hex[:10], "role": "helix", "text": text,
                "visuals": [], "sources": [], "actions": [], "images": []})
    voice = getattr(shell, "voice", None)
    if voice is not None:
        try:
            voice.speak(text)                      # sets 'speaking', ends in 'idle' by itself
            if voice.state() == "speaking":
                return {"ok": True, "spoken": True}
        except Exception:  # noqa: BLE001 - fall through to the mime
            pass
    secs = seconds_for(text)
    shell.push({"t": "orb", "state": "speaking"})
    threading.Timer(secs, lambda: shell.push({"t": "orb", "state": "idle"})).start()
    return {"ok": True, "spoken": False, "seconds": round(secs, 1)}


def mount_say(app: FastAPI, shell) -> None:
    @app.post("/api/say")
    async def say_route(request: Request):
        body = await request.json()
        result = say(shell, str(body.get("text") or ""))
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result
