"""THE VOICE - Gemini-TTS through Google Cloud Text-to-Speech, with the person's own gcloud token.

Decided 2026-09-22 (built 2026-09-21): paid quality, one API, styled in plain words. The voice has
two knobs a person understands:
  * the VOICE - one of Google's thirty named timbres (Charon, Puck, Kore ...), and
  * the STYLE - a sentence telling the voice how to carry itself ("a calm, dry British man") -
    which is also how an accent is chosen: Gemini-TTS takes accent, pace and mood from the prompt.
Each person runs HELIX on their own PC with their own settings, so "one voice per person" is what
the settings already are.

Same shape as the vault and the fleet: `gcloud auth print-access-token` once (cached ~50 min), then
plain HTTPS to Google with `x-goog-user-project`, never a key on disk. Playback on Windows through
the warm MediaPlayer that edge-tts already uses. If Google refuses (offline, no role, a voice it
does not know) the line falls back to edge-tts, then the OS voice - a reply is always spoken.

A small cache keeps the last lines by (text, voice, style): think-out-loud lines, chimes and task
endings repeat, and a repeated line costs nothing the second time.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from typing import Callable

from helix.adapters import pulse as _pulse
from helix.adapters.gcp_secret_manager import Http, Ran, Runner, _real_http, _real_runner
from helix.adapters.speech import _WarmMediaPlayer, mp3_duration_ms
from helix.logging_setup import get_logger

_LOG = get_logger("google_tts")

API = "https://texttospeech.googleapis.com/v1/text:synthesize"
TOKEN_TTL_S = 50 * 60
DEFAULT_MODEL = "gemini-2.5-flash-tts"
DEFAULT_VOICE = "Charon"
DEFAULT_STYLE = "butler"
MAX_CHARS = 3800          # the API's 4,000-byte ceiling per field, with room for UTF-8
CACHE = 64

NOT_SIGNED_IN = "No gcloud account is signed in on this PC, so HELIX cannot reach Google's voice as you."
NO_RIGHTS = "{who} may not use Cloud Text-to-Speech in {project} yet (the Service Usage Consumer role, once, in IAM)."
API_OFF = "Cloud Text-to-Speech is not enabled in {project}: gcloud services enable texttospeech.googleapis.com"

# Google publishes the names and genders only; the feel of each is HELIX's own ear, so a person can
# pick without auditioning all thirty. `pick` marks the eight worth hearing first.
VOICES: tuple[dict, ...] = (
    {"name": "Charon", "gender": "male", "feel": "deep, even, informative", "pick": True},
    {"name": "Fenrir", "gender": "male", "feel": "bright, excitable", "pick": True},
    {"name": "Puck", "gender": "male", "feel": "upbeat, quick", "pick": True},
    {"name": "Enceladus", "gender": "male", "feel": "soft, breathy", "pick": False},
    {"name": "Iapetus", "gender": "male", "feel": "clear, plain", "pick": False},
    {"name": "Algieba", "gender": "male", "feel": "smooth, low", "pick": True},
    {"name": "Orus", "gender": "male", "feel": "firm, steady", "pick": False},
    {"name": "Schedar", "gender": "male", "feel": "even, unhurried", "pick": False},
    {"name": "Sadaltager", "gender": "male", "feel": "knowing, measured", "pick": False},
    {"name": "Zubenelgenubi", "gender": "male", "feel": "casual, easy", "pick": False},
    {"name": "Umbriel", "gender": "male", "feel": "easy-going, warm", "pick": False},
    {"name": "Rasalgethi", "gender": "male", "feel": "informative, crisp", "pick": False},
    {"name": "Alnilam", "gender": "male", "feel": "firm, forward", "pick": False},
    {"name": "Achird", "gender": "male", "feel": "friendly, open", "pick": False},
    {"name": "Algenib", "gender": "male", "feel": "gravelly, low", "pick": True},
    {"name": "Sadachbia", "gender": "male", "feel": "lively, light", "pick": False},
    {"name": "Kore", "gender": "female", "feel": "firm, clear", "pick": True},
    {"name": "Zephyr", "gender": "female", "feel": "bright, warm", "pick": True},
    {"name": "Aoede", "gender": "female", "feel": "breezy, light", "pick": False},
    {"name": "Leda", "gender": "female", "feel": "youthful, quick", "pick": False},
    {"name": "Sulafat", "gender": "female", "feel": "warm, rounded", "pick": True},
    {"name": "Despina", "gender": "female", "feel": "smooth, calm", "pick": False},
    {"name": "Erinome", "gender": "female", "feel": "clear, steady", "pick": False},
    {"name": "Laomedeia", "gender": "female", "feel": "upbeat, open", "pick": False},
    {"name": "Achernar", "gender": "female", "feel": "soft, gentle", "pick": False},
    {"name": "Autonoe", "gender": "female", "feel": "bright, even", "pick": False},
    {"name": "Callirrhoe", "gender": "female", "feel": "easy-going, low", "pick": False},
    {"name": "Gacrux", "gender": "female", "feel": "mature, measured", "pick": False},
    {"name": "Pulcherrima", "gender": "female", "feel": "forward, sure", "pick": False},
    {"name": "Vindemiatrix", "gender": "female", "feel": "gentle, unhurried", "pick": False},
)
VOICE_NAMES = frozenset(v["name"] for v in VOICES)

# The STYLE is a sentence the voice is handed before every line. Accent lives here.
STYLES: tuple[dict, ...] = (
    {"key": "butler", "label": "The Butler", "flag": "GB",
     "prompt": "Speak as a calm, dry, well-spoken British man - unhurried, precise, with a hint of wit. Natural conversational pace."},
    {"key": "dubliner", "label": "The Dubliner", "flag": "IE",
     "prompt": "Speak with a warm, natural Irish accent - easy, friendly, a little musical, never hurried."},
    {"key": "control", "label": "Mission Control", "flag": "US",
     "prompt": "Speak like a calm mission-control voice: clear, steady, confident, every word deliberate."},
    {"key": "scientist", "label": "The Scientist", "flag": "US",
     "prompt": "Speak like a curious scientist thinking aloud - warm, precise, quietly excited by the problem."},
    {"key": "highlander", "label": "The Highlander", "flag": "SCO",
     "prompt": "Speak with a soft Scottish accent - grounded, warm, a little wry."},
    {"key": "plain", "label": "Plain", "flag": "", "prompt": ""},
    {"key": "custom", "label": "Your own words", "flag": "", "prompt": None},
)
STYLE_BY_KEY = {s["key"]: s for s in STYLES}


def style_prompt(key: str | None, custom: str | None = None) -> str:
    """The sentence for a style key; `custom` wins for the custom style."""
    k = (key or DEFAULT_STYLE).strip()
    if k == "custom":
        return (custom or "").strip()[:MAX_CHARS]
    s = STYLE_BY_KEY.get(k) or STYLE_BY_KEY[DEFAULT_STYLE]
    return s["prompt"] or ""


def normalize_voice(name: str | None) -> str:
    n = (name or "").strip()
    return n if n in VOICE_NAMES else DEFAULT_VOICE


class TtsError(RuntimeError):
    pass


class GoogleTts:
    """The synthesizer alone: text -> MP3 bytes. No playback, no fallback. Shared by the voice
    loop (GoogleSpeechOut) and the page (say.py), so both cache the same lines."""

    def __init__(self, project: str, *, runner: Runner | None = None, http: Http | None = None,
                 gcloud: str = "gcloud", identity: Callable[[], str | None] | None = None,
                 model: Callable[[], str | None] | None = None) -> None:
        self._project = project
        self._run = runner or _real_runner
        self._http = http or _real_http
        self._gcloud = shutil.which(gcloud) or gcloud
        self._identity = identity or (lambda: None)
        self._model = model or (lambda: DEFAULT_MODEL)
        self._lock = threading.Lock()
        self._token: tuple[str, float] | None = None
        self._cache: OrderedDict[tuple[str, str, str, str], bytes] = OrderedDict()
        self.last_error: str | None = None

    def available(self) -> bool:
        return self._run is not _real_runner or shutil.which(self._gcloud) is not None

    def token(self, *, fresh: bool = False) -> str:
        with self._lock:
            if not fresh and self._token and time.time() < self._token[1]:
                return self._token[0]
        ran: Ran = self._run([self._gcloud, "auth", "print-access-token"], 30.0)
        tok = (ran.out or "").strip().splitlines()[0].strip() if ran.rc == 0 and (ran.out or "").strip() else ""
        if not tok:
            raise TtsError(NOT_SIGNED_IN)
        with self._lock:
            self._token = (tok, time.time() + TOKEN_TTL_S)
        return tok

    def synthesize(self, text: str, voice: str, style: str, *, whisper: bool = False) -> bytes:
        text = (text or "").strip()[:MAX_CHARS]
        if not text:
            return b""
        voice = normalize_voice(voice)
        model = (self._model() or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        prompt = (style or "").strip()
        if whisper:
            prompt = (prompt + " " if prompt else "") + "Whisper this, quietly and slowly."
        key = (text, voice, prompt, model)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        body = {"input": ({"prompt": prompt, "text": text} if prompt else {"text": text}),
                "voice": {"languageCode": "en-US", "name": voice, "model_name": model},
                "audioConfig": {"audioEncoding": "MP3"}}
        status, out = self._post(body)
        if status == 401:
            status, out = self._post(body, fresh=True)
        if status == 403:
            low = (out or "").lower()
            if "not been used" in low or "is disabled" in low or "not enabled" in low:
                raise TtsError(API_OFF.format(project=self._project))
            raise TtsError(NO_RIGHTS.format(who=self._identity() or "This account", project=self._project))
        if status == 0:
            raise TtsError(f"Google's voice could not be reached: {out[:160]}")
        if status >= 400:
            try:
                msg = json.loads(out)["error"]["message"]
            except Exception:  # noqa: BLE001
                msg = out[:200]
            raise TtsError(f"Google's voice answered {status}: {msg}")
        try:
            audio = base64.b64decode(json.loads(out)["audioContent"])
        except Exception as exc:  # noqa: BLE001
            raise TtsError("Google's voice answered without audio.") from exc
        with self._lock:
            self._cache[key] = audio
            while len(self._cache) > CACHE:
                self._cache.popitem(last=False)
        return audio

    def _post(self, body: dict, *, fresh: bool = False) -> tuple[int, str]:
        tok = self.token(fresh=fresh)
        # x-goog-user-project: the person's own token, the project's quota and bill. The shared
        # _real_http sends Authorization only, so the real transport is a request of our own; an
        # injected http (tests) gets the plain call.
        if self._http is _real_http:
            return _post_with_project(API, tok, self._project, body)
        return self._http("POST", API, tok, body, 30.0)


def _post_with_project(url: str, token: str, project: str, body: dict) -> tuple[int, str]:
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, method="POST", data=data, headers={
        "Authorization": f"Bearer {token}", "x-goog-user-project": project,
        "Content-Type": "application/json; charset=utf-8", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30.0) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return e.code, ""
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


class GoogleSpeechOut:
    """SpeechOut (ports/speech.py) on Google's voice, falling back to whatever it is given
    (edge-tts, which itself falls back to the OS voice). `engine` is read per line: when the
    setting says edge or os, every call goes straight to the fallback - one object, live switch."""

    def __init__(self, tts: GoogleTts, *, engine: Callable[[], str | None], voice: Callable[[], str | None],
                 style: Callable[[], str], fallback) -> None:
        self._tts = tts
        self._engine = engine
        self._voice = voice
        self._style = style
        self._fallback = fallback
        self._player = _WarmMediaPlayer()
        self._lock = threading.Lock()
        self._gen = 0
        self._stopped_gen = 0

    def _google(self) -> bool:
        return (self._engine() or "google").strip().lower() == "google" and self._tts.available()

    def available(self) -> bool:
        return self._google() or self._fallback.available()

    def _is_stopped(self, gen: int) -> bool:
        return self._stopped_gen >= gen

    def _say(self, text: str, gen: int, *, whisper: bool = False) -> bool:
        """Synthesize + play one line through Google. False = did not speak (caller decides)."""
        audio = self._tts.synthesize(text, self._voice() or DEFAULT_VOICE, self._style(), whisper=whisper)
        if not audio or self._is_stopped(gen):
            return bool(audio)
        handle, path = tempfile.mkstemp(suffix=".mp3", prefix="helix_gtts_")
        os.close(handle)
        try:
            with open(path, "wb") as f:
                f.write(audio)
            if not self._player.play(path, mp3_duration_ms(path)) and not self._is_stopped(gen):
                raise TtsError("playback failed")
            return True
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def speak(self, text: str, allow_fallback: bool = True) -> None:
        text = (text or "").strip()
        if not text:
            return
        if not self._google():
            self._fallback.speak(text, allow_fallback)
            return
        with self._lock:
            self._gen += 1
            gen = self._gen
        try:
            self._say(text, gen)
        except Exception as exc:  # noqa: BLE001
            if self._is_stopped(gen):
                return
            self._tts.last_error = str(exc)
            _LOG.warning("Google voice failed (%s); %s", exc, "falling back" if allow_fallback else "skipping the note")
            if allow_fallback:
                self._fallback.speak(text, allow_fallback)

    def speak_chunks(self, chunks: list[str], allow_fallback: bool = True) -> None:
        chunks = [c for c in (str(x).strip() for x in chunks) if c]
        if not chunks:
            return
        if not self._google():
            fn = getattr(self._fallback, "speak_chunks", None)
            if callable(fn):
                fn(chunks, allow_fallback)
            else:
                for c in chunks:
                    self._fallback.speak(c, allow_fallback)
            return
        # One request for the whole reply reads better than sentence by sentence (the prompt
        # shapes a paragraph better than a clause) and is one round trip instead of many.
        self.speak(" ".join(chunks), allow_fallback)

    def murmur(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if not self._google():
            fn = getattr(self._fallback, "murmur", None)
            if callable(fn):
                fn(text)
            return
        with self._lock:
            self._gen += 1
            gen = self._gen
        try:
            self._say(text, gen, whisper=True)
        except Exception as exc:  # noqa: BLE001 - a murmur is optional; silence beats a second voice
            if not self._is_stopped(gen):
                _LOG.info("murmur not spoken (%s)", exc)

    def stop(self) -> None:
        with self._lock:
            self._stopped_gen = self._gen
        self._player.stop()
        try:
            self._fallback.stop()
        except Exception:  # noqa: BLE001
            pass


def catalog() -> dict:
    """What Settings shows: the voices, the styles, the defaults."""
    return {"voices": [dict(v) for v in VOICES], "styles": [dict(s) for s in STYLES],
            "default_voice": DEFAULT_VOICE, "default_style": DEFAULT_STYLE, "model": DEFAULT_MODEL}
