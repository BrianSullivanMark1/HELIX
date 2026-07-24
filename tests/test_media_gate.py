"""The playback gate: sound the MACHINE itself is playing (YouTube, music) is never its user.

While the render meter reads hot, an utterance acts only when genuinely ADDRESSED (name leading — the
thalamic cocktail-party rule) or when its voice-print confidently matches a registered speaker. These
lock: lyrics with a wake-ish token can't start turns (the STT is hotword-biased toward the name, so it
WILL fish it out of music); the in-session no-wake-word privilege is suspended; a video's 'goodbye'
can't end the session; a bare fished-out name opens nothing; a recognized voice keeps every privilege
over the music — and with no media playing, behavior is byte-for-byte the old behavior. MediaSense
itself degrades to 'not playing' on any failure, so voice without a working meter is the old voice.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from helix.ui import mediasense  # noqa: E402
from helix.ui.mediasense import MediaSense  # noqa: E402
from helix.ui.voice import VoiceController  # noqa: E402

_app = QApplication.instance() or QApplication([])


class _Stt:
    def available(self):
        return True

    def ready(self):
        return True

    def transcribe(self, _path):
        return ""


class _Tts:
    def __init__(self):
        self.spoke = []
        self.stops = 0

    def available(self):
        return True

    def speak(self, text, allow_fallback=True):
        self.spoke.append(text)

    def stop(self):
        self.stops += 1


class _Settings:
    def __init__(self):
        self._d = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


def _controller(voice_id=None):
    tts = _Tts()
    vc = VoiceController(_Stt(), tts, _Settings(), voice_id=voice_id)
    vc._run = lambda fn, on_done: on_done(fn(lambda _s: None))
    heard, stops = [], []
    vc.recognized.connect(heard.append)
    vc.stopRequested.connect(lambda: stops.append(1))
    return vc, tts, heard, stops


# ---------- the gate itself (no voice-id: addressing is the only pass) ----------

def test_lyrics_with_a_wake_sound_do_not_start_a_turn_during_playback():
    vc, tts, heard, _stops = _controller()
    # The name mid-lyric would match the fuzzy wake regex — but it isn't ADDRESSED, so during
    # playback it must be dropped: no turn, no session, no spoken reply.
    vc._on_wake_text("rolling in my helix down the boulevard tonight", media=True)
    assert heard == []
    assert not vc._session
    assert tts.spoke == []


def test_same_lyric_without_playback_keeps_the_old_behavior():
    # No media → the gate is not consulted at all; the wake match acts exactly as before.
    vc, _tts, heard, _stops = _controller()
    vc._on_wake_text("rolling in my helix down the boulevard tonight", media=False)
    assert heard == ["down the boulevard tonight"]


def test_addressed_command_lands_over_music():
    vc, _tts, heard, _stops = _controller()
    vc._on_wake_text("hey helix, turn the volume down", media=True)
    assert heard == ["turn the volume down"]


def test_session_freepass_is_suspended_while_the_machine_plays():
    vc, _tts, heard, _stops = _controller()
    vc._on_wake_text("hey helix, hello there", media=False)  # opens the 45s session
    assert vc._session and heard == ["hello there"]
    vc._on_wake_text("we found love in a hopeless place", media=True)  # lyrics, no name
    assert heard == ["hello there"]  # dropped — the free-pass needs a quiet machine
    vc._on_wake_text("what's on my calendar", media=False)  # music paused → free-pass is back
    assert heard == ["hello there", "what's on my calendar"]


def test_bare_name_fished_out_of_playback_opens_nothing():
    vc, tts, heard, _stops = _controller()
    vc._on_wake_text("helix", media=True)
    assert heard == [] and not vc._session
    assert tts.spoke == []  # no "Yes?" acknowledgement to a lyric


def test_a_videos_goodbye_cannot_end_the_session():
    vc, tts, heard, _stops = _controller()
    vc._on_wake_text("hey helix, hello", media=False)
    assert vc._session
    vc._on_wake_text("goodbye", media=True)
    assert vc._session  # still live — an unaddressed farewell during playback is the video's, not yours
    assert all("Until next time" not in s for s in tts.spoke)


def test_an_addressed_dismissal_still_lands_over_music():
    vc, tts, _heard, _stops = _controller()
    vc._on_wake_text("hey helix, hello", media=False)
    assert vc._session
    vc._on_wake_text("thank you helix", media=True)  # addressed (the name is spoken) — honored
    assert not vc._session
    assert any("Until next time" in s for s in tts.spoke)


def test_addressed_stop_still_works_over_music():
    vc, _tts, heard, stops = _controller()
    vc._on_wake_text("helix stop", media=True)
    assert stops == [1]
    assert heard == []  # a stop is a reflex, never a model turn


def test_short_lyric_fragment_with_the_name_is_not_a_turn_during_playback():
    # The STT is hotword-biased toward the name, and VAD admits short vocal fragments — "my helix
    # baby" must not become a turn while music plays (the strict address test withholds the
    # short-utterance benefit of the doubt).
    vc, tts, heard, _stops = _controller()
    vc._on_wake_text("my helix baby", media=True)
    assert heard == [] and not vc._session and tts.spoke == []


# ---------- the asleep path: playback can't wake a slept mic ----------

def test_asleep_playback_cannot_wake_helix():
    vc, _tts, _heard, _stops = _controller()
    vc.can_listen = lambda: True
    vc.set_muted(True, announce=False)
    assert vc.is_muted()
    vc._on_muted_text("helix", media=True)  # a bare name fished out of a lyric
    assert vc.is_muted()
    vc._on_muted_text("wake up", media=True)  # a song's own "wake up!"
    assert vc.is_muted()
    vc._on_muted_text("helix wake up", media=True)  # the name LEADING with the ask — a person
    assert not vc.is_muted()


def test_asleep_wake_without_playback_is_unchanged():
    vc, _tts, _heard, _stops = _controller()
    vc.can_listen = lambda: True
    vc.set_muted(True, announce=False)
    vc._on_muted_text("wake up")  # machine quiet → the old contract exactly
    assert not vc.is_muted()


def test_registered_voice_wakes_over_music(tmp_path):
    svc, alice_emb, _stranger = _voiceid(tmp_path)
    vc, _tts, _heard, _stops = _controller(voice_id=svc)
    vc.can_listen = lambda: True
    vc.set_muted(True, announce=False)
    vc._pending_emb = alice_emb
    vc._on_muted_text("wake up", media=True)  # no name needed — it's a registered voice
    assert not vc.is_muted()


# ---------- an open calibration flow is not fed music walls ----------

def test_music_wall_never_feeds_an_open_calibration_flow():
    vc, _tts, _heard, _stops = _controller()
    fed = []
    vc._flow = SimpleNamespace(
        active=True, last_registered=None, cancel=lambda: None,
        handle=lambda t, emb: (fed.append(t), "and how should I greet you?")[1],
    )
    lyric = "we found love in a hopeless place where shadows run and rivers turn to gold tonight"
    vc._on_wake_text(lyric, media=True)  # a long unaddressed capture — never a calibration answer
    assert fed == []
    vc._on_wake_text("Brian", media=True)  # a short real answer still reaches the flow
    assert fed == ["Brian"]


# ---------- a recognized voice keeps its privileges over the music ----------

def _voiceid(tmp_path):
    np = pytest.importorskip("numpy")
    from helix.services.voiceid import VoiceIdService

    def unit(seed, dim=82):
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(dim).astype(np.float32)
        return v / np.linalg.norm(v)

    def near(base, seed, wobble=0.05):
        v = base + wobble * unit(1000 + seed)
        return (v / np.linalg.norm(v)).astype(np.float32)

    alice = unit(1)
    svc = VoiceIdService(tmp_path / "voices.json")
    svc.register("Alice", [near(alice, s) for s in range(6)])
    return svc, near(alice, 50, 0.02), unit(2)  # service, a Alice-like print, a stranger print


def test_recognized_voice_keeps_the_session_freepass_over_music(tmp_path):
    svc, alice_emb, _stranger = _voiceid(tmp_path)
    assert svc.identify(alice_emb).confident  # precondition: this print matches confidently
    vc, _tts, heard, _stops = _controller(voice_id=svc)
    vc._pending_emb = alice_emb
    vc._on_wake_text("hey helix, hello", media=False)
    assert vc._session and heard == ["hello"]
    vc._pending_emb = alice_emb
    vc._on_wake_text("turn the volume down a bit", media=True)  # no name — but it's Alice's voice
    assert heard == ["hello", "turn the volume down a bit"]


def test_unmatched_voice_during_music_is_dropped_before_the_identity_gate(tmp_path):
    from helix.services.voiceid import UNRECOGNIZED_REPLY

    svc, alice_emb, stranger = _voiceid(tmp_path)
    vc, tts, heard, _stops = _controller(voice_id=svc)
    vc._pending_emb = alice_emb
    vc._on_wake_text("hey helix, hello", media=False)
    assert vc._session
    vc._pending_emb = stranger  # music vocals match nobody
    vc._on_wake_text("baby don't hurt me no more", media=True)
    assert heard == ["hello"]
    # Crucially the drop happens BEFORE the identity gate: playback never earns a spoken refusal.
    assert UNRECOGNIZED_REPLY not in tts.spoke


# ---------- the utterance path carries the playback flag ----------

def test_utterance_captured_during_playback_is_gated(tmp_path):
    vc, _tts, heard, _stops = _controller()
    vc._stt.transcribe = lambda _p: "my helix dream tonight yeah"
    vc._media = SimpleNamespace(playing=lambda: True, tick=lambda: None)
    vc._on_utterance(b"\x20\x10" * 8000)
    assert heard == []  # tainted + unaddressed → dropped

    vc._set_state("idle")
    vc._media = SimpleNamespace(playing=lambda: False, tick=lambda: None)
    vc._on_utterance(b"\x20\x10" * 8000)
    assert heard == ["dream tonight yeah"]  # machine quiet → the old behavior exactly


def test_addressed_utterance_lands_even_when_flagged(tmp_path):
    vc, _tts, heard, _stops = _controller()
    vc._stt.transcribe = lambda _p: "hey helix what time is it"
    vc._media = SimpleNamespace(playing=lambda: True, tick=lambda: None)
    vc._on_utterance(b"\x20\x10" * 8000)
    assert heard == ["what time is it"]


# ---------- MediaSense: honest when it works, silent when it can't ----------

def test_media_sense_reports_playing_and_respects_the_hot_window(monkeypatch):
    level = {"v": 0.8}
    ms = MediaSense(peak_fn=lambda: level["v"])
    assert ms.playing()  # hot now
    level["v"] = 0.0
    assert ms.playing()  # still inside the recently-hot window (bridges in-song dips)
    monkeypatch.setattr(mediasense, "HOT_WINDOW_S", -1.0)  # collapse the window
    assert not ms.playing()


def test_media_sense_below_floor_is_not_playing():
    ms = MediaSense(peak_fn=lambda: 0.001)  # idle hiss, far under PEAK_FLOOR
    assert not ms.playing()


def test_media_sense_never_raises_and_backs_off_on_failure(monkeypatch):
    ms = MediaSense(peak_fn=lambda: (_ for _ in ()).throw(RuntimeError("meter broke")))
    assert not ms.playing()  # a broken meter reads as silence, never an exception

    attempts = []

    def _boom():
        attempts.append(1)
        raise OSError("no COM")

    monkeypatch.setattr(mediasense.platform, "system", lambda: "Windows")
    monkeypatch.setattr(mediasense, "_RenderMeter", _boom)
    ms2 = MediaSense()
    assert not ms2.playing()
    assert not ms2.playing()
    assert len(attempts) <= 1  # a failed build backs off (RETRY_S) — never one COM call per mic chunk


def test_meter_rebinds_and_rebuild_failures_are_not_fatal(monkeypatch):
    monkeypatch.setattr(mediasense.platform, "system", lambda: "Windows")
    built, fail = [], {"on": False}

    class _Fake:
        def __init__(self):
            built.append(1)
            if fail["on"]:
                raise OSError("no endpoint mid device-transition")

        def peak(self):
            return 0.5

        def close(self):
            pass

    monkeypatch.setattr(mediasense, "_RenderMeter", _Fake)
    monkeypatch.setattr(mediasense, "REBIND_S", -1.0)  # rebind on every sample
    ms = MediaSense()
    assert ms.playing() and len(built) == 1
    assert ms.playing() and len(built) == 2  # periodic rebind — a changed default output is picked up
    fail["on"] = True
    assert ms.playing()   # the recently-hot window bridges the failed rebuild
    assert not ms._dead   # a failed REBUILD is never fatal...
    fail["on"] = False
    ms._next_retry = float("-inf")
    assert ms.playing()   # ...and the next retry re-arms it


def test_media_sense_smoke_on_this_host():
    # Whatever this machine is (Windows with real COM, or anything else degrading to dark),
    # constructing and asking must return a bool and never raise.
    assert isinstance(MediaSense().playing(), bool)
