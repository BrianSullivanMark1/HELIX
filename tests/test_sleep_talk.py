"""The whisper: EdgeSpeechOut.murmur is the same voice quieter, slower and lower, once, never the OS
voice; WebVoice.murmur speaks it only while the voice is idle and unmuted."""
from __future__ import annotations

import threading
import time

from helix.adapters import speech
from helix.api.voice_loop import WebVoice


# ----- EdgeSpeechOut.murmur -----
def _edge(monkeypatch, played: list, comms: list, *, rate="1.0"):
    import edge_tts

    class _Comm:
        def __init__(self, text, voice, **tuning) -> None:
            comms.append((text, voice, dict(tuning)))

        async def save(self, path) -> None:
            with open(path, "wb") as fh:
                fh.write(b"mp3")

    monkeypatch.setattr(edge_tts, "Communicate", _Comm)
    e = speech.EdgeSpeechOut(lambda: "en-GB-RyanNeural", lambda: rate, fallback=_Fallback())
    e._play = lambda path, gen=None: played.append(path)  # type: ignore[method-assign]
    return e


class _Fallback:
    def __init__(self):
        self.spoken: list[str] = []

    def available(self):
        return True

    def speak(self, text, allow_fallback=True):
        self.spoken.append(text)

    def stop(self):
        pass


def test_a_murmur_is_the_users_voice_scaled_down_and_played_once(monkeypatch):
    played: list = []
    comms: list = []
    e = _edge(monkeypatch, played, comms, rate="1.25")
    e.murmur("pages turning by themselves…")
    assert len(comms) == 1 and len(played) == 1
    text, voice, tuning = comms[0]
    assert text == "pages turning by themselves…" and voice == "en-GB-RyanNeural"
    # A fifth slower than the user's own 1.25× (→ 1.025 ≈ +2%), well under half the volume, a touch lower.
    assert tuning["rate"] == "+2%" and tuning["volume"] == "-45%" and tuning["pitch"] == "-8Hz"


def test_a_plain_reply_carries_no_murmur_tuning(monkeypatch):
    played: list = []
    comms: list = []
    e = _edge(monkeypatch, played, comms, rate="1.0")
    e.speak("Done.")
    assert comms[0][2] == {"rate": "+0%"}


def test_an_unreadable_rate_setting_is_natural_speed(monkeypatch):
    played: list = []
    comms: list = []
    e = _edge(monkeypatch, played, comms, rate="fast")
    e.murmur("mm…")
    assert comms[0][2]["rate"] == "-18%"
    assert speech._rate_multiplier(None) == 1.0 and speech._rate_multiplier(-2) == 1.0 and speech._rate_multiplier("0.5") == 0.5


def test_a_murmur_never_falls_back_to_the_os_voice(monkeypatch):
    import edge_tts

    class _Boom:
        def __init__(self, *a, **k) -> None:
            pass

        async def save(self, path) -> None:
            raise RuntimeError("offline")

    monkeypatch.setattr(edge_tts, "Communicate", _Boom)
    fallback = _Fallback()
    e = speech.EdgeSpeechOut(lambda: None, lambda: None, fallback=fallback)
    e.murmur("quiet now…")  # silence, not a second voice in the night
    assert fallback.spoken == []
    e.murmur("")  # nothing to say is nothing done
    assert fallback.spoken == []


def test_a_stop_during_synthesis_leaves_the_murmur_unplayed(monkeypatch):
    played: list = []
    comms: list = []
    e = _edge(monkeypatch, played, comms)
    real = e._synthesize

    def stopping(text, gen=None, **kw):
        path = real(text, gen, **kw)
        e.stop()
        return path

    e._synthesize = stopping  # type: ignore[method-assign]
    e.murmur("footsteps… I'll wait…")
    assert played == []


# ----- WebVoice.murmur -----
class _Settings:
    """Hands-free OFF at construction — a WebVoice built with it on opens a REAL microphone stream
    (PortAudio) inside the test process, which crashes the interpreter at teardown. The tests turn
    `enabled` on afterwards, as test_webvoice_sleep does."""

    def __init__(self, on=False):
        self._d = {"voice_input_on": on}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class _Stt:
    def available(self):
        return True

    def ready(self):
        return True


class _Tts:
    def __init__(self):
        self.murmured: list[str] = []
        self.spoken: list[str] = []
        self.done = threading.Event()

    def available(self):
        return True

    def speak(self, text, allow_fallback=True):
        self.spoken.append(text)

    def stop(self):
        pass

    def murmur(self, text):
        self.murmured.append(text)
        self.done.set()


def _voice(tts=None) -> tuple[WebVoice, _Tts]:
    tts = tts or _Tts()
    v = WebVoice(_Settings(), _Stt(), tts)
    v.can_listen = lambda: True  # type: ignore[method-assign]
    v.enabled = lambda: True  # type: ignore[method-assign]
    return v, tts


def test_a_murmur_is_whispered_only_while_idle_and_unmuted_and_never_over_a_note():
    v, tts = _voice()
    v.murmur("pages turning… **by themselves**…")
    assert tts.done.wait(2.0) and tts.murmured == ["pages turning… by themselves…"]  # markdown stripped like any speech
    assert v._speaking_text == "pages turning… by themselves…"  # the echo shield knows the words
    # Listening, thinking or speaking: silence — a murmur never talks over the user or a reply.
    for state in ("listening", "thinking", "speaking"):
        v._state = state
        v.murmur("mm…")
    v._state = "idle"
    # Muted means the user asked for quiet.
    v._muted = True
    v.murmur("mm…")
    v._muted = False
    # A progress note still playing: this one is skipped rather than stacked.
    v._narrating = True
    v.murmur("mm…")
    v._narrating = False
    time.sleep(0.05)
    assert tts.murmured == ["pages turning… by themselves…"]


def test_a_voice_backend_without_murmur_stays_silent():
    class _Plain:
        def available(self):
            return True

        def speak(self, text, allow_fallback=True):
            raise AssertionError("a murmur must never be spoken at reply volume")

        def stop(self):
            pass

    v = WebVoice(_Settings(), _Stt(), _Plain())
    v.enabled = lambda: True  # type: ignore[method-assign]
    v.murmur("mm…")  # nothing happens, nothing raises
    assert v._narrating is False


def test_the_mic_is_closed_while_a_murmur_plays_and_reopens_after():
    """The live app woke its sleeping orb every twenty seconds: the wake-word VAD heard the whisper
    and flipped the state to 'listening'. A murmur now closes the listen gate for its length."""
    class _SlowTts(_Tts):
        def __init__(self):
            super().__init__()
            self.release = threading.Event()

        def murmur(self, text):
            self.murmured.append(text)
            self.done.set()
            self.release.wait(2.0)

    v, tts = _voice(_SlowTts())
    v._apply_listen_gate()
    assert v._listening is True  # idle, hands-free on: the wake word is being listened for
    v.murmur("footsteps… I'll wait…")
    assert tts.done.wait(2.0)
    assert v._listening is False and v._murmuring is True  # deaf to its own whisper
    tts.release.set()
    deadline = time.monotonic() + 2.0
    while v._murmuring and time.monotonic() < deadline:
        time.sleep(0.01)
    assert v._murmuring is False and v._listening is True  # …and listening again once it ends
