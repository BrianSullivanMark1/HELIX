"""The test line - HELIX says a typed sentence out loud, no model involved - and the voice's timing.

  POST /api/say            {"text": "..."}  ->  {"ok", "id", "url", "words": [{"t","d","w"}], "seconds"}
  GET  /api/say/audio/{id} the mp3 the page plays

The page plays the audio itself and drives the lips from the WORD BOUNDARIES the synthesizer
reports (edge-tts: one event per word with its offset and duration), so the mouth moves with the
sound, not with an estimate - and starts when the audio starts, not when the request does.
Without edge-tts (offline, or a voice it cannot make) the line falls back to the speak path
HELIX already has: the OS voice, the orb states pushed by the voice loop, the face on its own
clock. The transcript gets the line as a HELIX bubble either way.

Human-only by construction: an HTTP route on the local face, never a tool the model can call.
Mounted by server.build_app with `mount_say(app, shell, settings)`.
"""
from __future__ import annotations

import asyncio
import re
import threading
import uuid
from collections import OrderedDict
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

MAX_CHARS = 600
WORDS_PER_SECOND = 2.6   # the pace the face reads at when nothing is actually speaking
MIN_SECONDS = 1.2
DEFAULT_VOICE = "en-GB-RyanNeural"
KEPT = 12                # synthesized lines kept in memory for the page to fetch

Synth = Callable[[str, str, float], tuple[bytes, list[dict]]]


def seconds_for(text: str) -> float:
    words = len(re.findall(r"\S+", text))
    return max(MIN_SECONDS, words / WORDS_PER_SECOND + 0.6)


def rate_string(rate: float) -> str:
    """HELIX's tts_rate (1.0 = normal) as edge-tts's '+10%' form."""
    pct = int(round((float(rate or 1.0) - 1.0) * 100))
    pct = max(-50, min(100, pct))
    return f"{'+' if pct >= 0 else ''}{pct}%"


def edge_synth(text: str, voice: str, rate: float) -> tuple[bytes, list[dict]]:
    """mp3 bytes + [{t, d, w}] in seconds, from edge-tts's stream (WordBoundary events)."""
    import edge_tts  # local import: optional dependency, and slow to import

    async def go():
        audio = bytearray()
        words: list[dict] = []
        com = edge_tts.Communicate(text, voice, rate=rate_string(rate))
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append({"t": round(chunk["offset"] / 1e7, 3), "d": round(chunk["duration"] / 1e7, 3),
                              "w": str(chunk.get("text") or "")})
        return bytes(audio), words

    return asyncio.run(go())


class Lines:
    """The last few synthesized lines, by id, for GET /api/say/audio/{id}."""

    def __init__(self) -> None:
        self._d: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, audio: bytes) -> str:
        sid = uuid.uuid4().hex[:12]
        with self._lock:
            self._d[sid] = audio
            while len(self._d) > KEPT:
                self._d.popitem(last=False)
        return sid

    def get(self, sid: str) -> bytes | None:
        with self._lock:
            return self._d.get(sid)


def google_synth_for(tts, settings, over: dict | None = None) -> Synth:
    """THE VOICE (2026-09-21): Gemini-TTS through the person's gcloud, styled in plain words. No
    word boundaries come back - the page drives the lips from the audio's own amplitude, which
    was the plan. `over` (a Settings preview) may name a voice and a style for this one line."""
    from helix.adapters.google_tts import style_prompt
    over = over or {}

    def synth(text: str, _voice: str, _rate: float) -> tuple[bytes, list[dict]]:
        get = (lambda k: settings.get(k)) if settings is not None else (lambda k: None)
        voice = str(over.get("voice") or get("tts_google_voice") or "")
        style = style_prompt(str(over.get("style") or get("tts_style") or ""),
                             str(over.get("style_custom") if over.get("style_custom") is not None else (get("tts_style_custom") or "")))
        return tts.synthesize(text, voice, style), []

    return synth


def say(shell, text: str, *, settings=None, synth: Synth | None = edge_synth, lines: Lines | None = None,
        google=None, over: dict | None = None, bubble: bool = True) -> dict:
    """Bubble the line, then synthesize it for the page - or fall back to the voice loop.
    `google` is the GoogleTts synthesizer (or None); the engine setting (or `over["engine"]`)
    decides which synth speaks: google -> Gemini-TTS, then edge-tts if Google refuses."""
    text = (text or "").strip()[:MAX_CHARS]
    if not text:
        return {"ok": False, "error": "Nothing to say."}
    if bubble:
        shell.push({"t": "msg", "id": uuid.uuid4().hex[:10], "role": "helix", "text": text,
                    "visuals": [], "sources": [], "actions": [], "images": []})
    over = over or {}
    engine = str(over.get("engine") or (settings.get("tts_engine") if settings else None) or "google").lower()
    synths: list[tuple[str, Synth]] = []
    if engine == "google" and google is not None and google.available():
        synths.append(("google", google_synth_for(google, settings, over)))
    if synth is not None and engine != "os":
        synths.append(("edge", synth))
    if lines is not None:
        voice = str(over.get("edge_voice") or (settings.get("tts_voice") if settings else None) or DEFAULT_VOICE)
        rate = float((settings.get("tts_rate") if settings else None) or 1.0)
        why = None
        for name, fn in synths:
            try:
                audio, words = fn(text, voice, rate)
                if audio:
                    sid = lines.put(audio)
                    seconds = (words[-1]["t"] + words[-1]["d"]) if words else seconds_for(text)
                    return {"ok": True, "id": sid, "url": f"/api/say/audio/{sid}", "words": words,
                            "seconds": round(seconds, 2), "spoken": "page", "engine": name,
                            **({"fell_back": why} if why else {})}
            except Exception as exc:  # noqa: BLE001 - offline, or a voice the service refused: the next path
                why = f"{name}: {exc}"
        if over.get("preview"):
            return {"ok": False, "error": why or "No voice could speak this line."}
    voice_loop = getattr(shell, "voice", None)
    if voice_loop is not None:
        try:
            voice_loop.speak(text)                 # sets 'speaking', ends in 'idle' by itself
            if voice_loop.state() == "speaking":
                return {"ok": True, "spoken": True}
        except Exception:  # noqa: BLE001 - fall through to the mime
            pass
    secs = seconds_for(text)
    shell.push({"t": "orb", "state": "speaking"})
    threading.Timer(secs, lambda: shell.push({"t": "orb", "state": "idle"})).start()
    return {"ok": True, "spoken": False, "seconds": round(secs, 1)}


def mount_say(app: FastAPI, shell, settings=None, *, synth: Synth | None = edge_synth, google=None) -> None:
    lines = Lines()

    @app.post("/api/say")
    async def say_route(request: Request):
        body = await request.json()
        text = str(body.get("text") or "")
        # a Settings preview: this voice, this style, this engine, for this one line - no bubble
        over = {k: body[k] for k in ("voice", "style", "style_custom", "engine", "edge_voice") if k in body}
        preview = bool(body.get("preview"))
        if preview:
            over["preview"] = True
        # synthesis is a network call to the voice service: off the loop
        result = await asyncio.to_thread(say, shell, text, settings=settings, synth=synth, lines=lines,
                                         google=google, over=over, bubble=not preview)
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    @app.get("/api/say/voices")
    def say_voices():
        """The catalog Settings shows: Google's voices and HELIX's styles, and the last error."""
        from helix.adapters.google_tts import catalog
        cat = catalog()
        cat["google_available"] = bool(google is not None and google.available())
        cat["last_error"] = getattr(google, "last_error", None)
        return cat

    @app.get("/api/say/audio/{sid}")
    def say_audio(sid: str):
        audio = lines.get(sid)
        if audio is None:
            return JSONResponse({"error": "That line is gone."}, status_code=404)
        return Response(content=audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
