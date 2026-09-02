"""Speech adapter tests — the neural-TTS fallback must NOT fire when playback is stopped on purpose
(closing the app or saying 'stop'), or a desktop voice would keep talking after the window is gone."""
from __future__ import annotations

import os
import tempfile
import time

import pytest

from helix.adapters import speech
from helix.adapters.speech import EdgeSpeechOut, _rate_string


class _Fallback:
    def __init__(self) -> None:
        self.spoke: list[str] = []

    def available(self) -> bool:
        return True

    def speak(self, text: str) -> None:
        self.spoke.append(text)

    def stop(self) -> None:
        pass


def _edge(fb: _Fallback) -> EdgeSpeechOut:
    return EdgeSpeechOut(lambda: "en-GB-RyanNeural", lambda: 1.0, fallback=fb)


def test_rate_string():
    assert _rate_string(1.0) == "+0%"
    assert _rate_string(1.5) == "+50%"
    assert _rate_string(0.8) == "-20%"


def test_stopped_during_synth_neither_plays_nor_falls_back():
    fb = _Fallback()
    e = _edge(fb)
    played: list[str] = []
    e._synthesize = lambda text, gen=None: (setattr(e, "_stopped_gen", e._gen), "x.mp3")[1]  # type: ignore[method-assign]
    e._play = lambda p, gen=None: played.append(p)  # type: ignore[method-assign]
    e.speak("hello")
    assert played == [] and fb.spoke == []


def test_killed_playback_does_not_fall_back_to_os_voice():
    fb = _Fallback()
    e = _edge(fb)

    def play(_p, gen=None):  # simulate stop() killing the player mid-play
        e._stopped_gen = e._gen
        raise RuntimeError("killed")

    e._synthesize = lambda text, gen=None: "x.mp3"  # type: ignore[method-assign]
    e._play = play  # type: ignore[method-assign]
    e.speak("hello")
    assert fb.spoke == []  # the stop was intentional — no desktop voice


def test_real_playback_failure_still_falls_back():
    fb = _Fallback()
    e = _edge(fb)

    def boom(_p, gen=None):
        raise RuntimeError("real failure")

    e._synthesize = lambda text, gen=None: "x.mp3"  # type: ignore[method-assign]
    e._play = boom  # type: ignore[method-assign]
    e.speak("hello")
    assert fb.spoke == ["hello"]  # genuine failure → OS voice, as intended


def test_narration_never_switches_to_the_os_voice():
    # Progress narration passes allow_fallback=False: a transient neural-TTS failure must SKIP the note,
    # not speak it in the desktop voice — otherwise consecutive notes flip between voices mid-build.
    fb = _Fallback()
    e = _edge(fb)

    def boom(_p, gen=None):
        raise RuntimeError("transient blip")

    e._synthesize = lambda text, gen=None: "x.mp3"  # type: ignore[method-assign]
    e._play = boom  # type: ignore[method-assign]
    e.speak("shaping the body", allow_fallback=False)
    assert fb.spoke == []  # stayed in one voice (skipped), never the OS voice


def test_speak_chunks_plays_every_sentence_in_order():
    fb = _Fallback()
    e = _edge(fb)
    played: list[str] = []
    e._synthesize = lambda text, gen=None: f"{text}.mp3"  # type: ignore[method-assign]
    e._play = lambda p, gen=None: played.append(p)  # type: ignore[method-assign]
    e.speak_chunks(["one.", "two.", "three."])
    assert played == ["one..mp3", "two..mp3", "three..mp3"]  # concurrent render, in-order playback
    assert fb.spoke == []


def test_speak_chunks_single_delegates_to_one_shot():
    fb = _Fallback()
    e = _edge(fb)
    played: list[str] = []
    e._synthesize = lambda text, gen=None: f"{text}.mp3"  # type: ignore[method-assign]
    e._play = lambda p, gen=None: played.append(p)  # type: ignore[method-assign]
    e.speak_chunks(["just one sentence."])
    assert played == ["just one sentence..mp3"]


def test_speak_chunks_stops_after_a_stop():
    fb = _Fallback()
    e = _edge(fb)
    played: list[str] = []

    def play(p, gen=None):
        played.append(p)
        e._stopped_gen = e._gen  # a 'stop' lands after the first sentence plays

    e._synthesize = lambda text, gen=None: f"{text}.mp3"  # type: ignore[method-assign]
    e._play = play  # type: ignore[method-assign]
    e.speak_chunks(["first.", "second.", "third."])
    assert played == ["first..mp3"]  # the rest were dropped
    assert fb.spoke == []            # a deliberate stop never falls back to the OS voice


def test_speak_chunks_falls_back_per_failed_sentence():
    fb = _Fallback()
    e = _edge(fb)
    played: list[str] = []
    # the middle sentence fails to synthesize; the others render fine
    e._synthesize = lambda text, gen=None: (_ for _ in ()).throw(RuntimeError("blip")) if "two" in text else f"{text}.mp3"  # type: ignore[method-assign]
    e._play = lambda p, gen=None: played.append(p)  # type: ignore[method-assign]
    e.speak_chunks(["one.", "two.", "three."])
    assert played == ["one..mp3", "three..mp3"]  # the good ones still play, in order
    assert fb.spoke == ["two."]                  # the failed one is spoken in the OS voice


def _track_temps(monkeypatch) -> list[str]:
    """Record every temp file _synthesize creates, so a stranded one is provable."""
    made: list[str] = []
    real = tempfile.mkstemp

    def spy(*args, **kwargs):
        handle, path = real(*args, **kwargs)
        made.append(path)
        return handle, path

    monkeypatch.setattr(speech.tempfile, "mkstemp", spy)
    return made


def _fake_edge(monkeypatch, *, audio: bytes = b"", boom: Exception | None = None) -> None:
    """Stand in for edge-tts at the seam — no network, no real voice service."""
    import edge_tts

    class _Comm:
        def __init__(self, text, voice, rate=None) -> None:
            pass

        async def save(self, path) -> None:
            if boom is not None:
                raise boom
            with open(path, "wb") as fh:
                fh.write(audio)

    monkeypatch.setattr(edge_tts, "Communicate", _Comm)


def _gone(path: str, timeout: float = 5.0) -> bool:
    # Cleanup runs on a daemon thread (os.remove of an mp3 can block ~0.2s on Windows), so poll.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and os.path.exists(path):
        time.sleep(0.02)
    return not os.path.exists(path)


def test_a_failed_synthesis_strands_no_temp_mp3(monkeypatch):
    # An offline reply retries three times and then raises; the mp3 it opened is never handed out, so
    # only _synthesize itself can delete it — otherwise a long offline session fills %TEMP%.
    # gen=1 matters: _stopped_gen starts at 0 and _is_stopped is `_stopped_gen >= gen`, so the default
    # gen=0 short-circuits on the STOPPED branch at the top of the retry loop and this test would never
    # reach the offline path it claims to cover.
    made = _track_temps(monkeypatch)
    _fake_edge(monkeypatch, boom=RuntimeError("offline"))
    e = _edge(_Fallback())
    with pytest.raises(RuntimeError, match="offline"):
        e._synthesize("hello", 1)
    assert made and all(_gone(p) for p in made)


def test_a_silent_render_strands_no_temp_mp3(monkeypatch):
    # edge-tts answering with zero bytes fails the same way — the empty file must still be reaped.
    # gen=1 for the same reason as above: otherwise this exits via the stopped branch.
    made = _track_temps(monkeypatch)
    _fake_edge(monkeypatch, audio=b"")
    e = _edge(_Fallback())
    with pytest.raises(RuntimeError, match="no audio"):
        e._synthesize("hello", 1)
    assert made and all(_gone(p) for p in made)


def test_a_stopped_synthesis_strands_no_temp_mp3(monkeypatch):
    # Saying 'stop' partway through a multi-sentence reply aborts the remaining renders before their
    # first attempt: the file exists (mkstemp made it) but no audio was ever written.
    made = _track_temps(monkeypatch)
    _fake_edge(monkeypatch, audio=b"ID3 audio")  # would have succeeded — the stop lands first
    e = _edge(_Fallback())
    e._gen = 3
    e._stopped_gen = 3
    with pytest.raises(RuntimeError):
        e._synthesize("hello", 3)
    assert made and all(_gone(p) for p in made)


def test_speak_keeps_the_mp3_alive_for_playback_then_reaps_it(monkeypatch):
    # The success path is unchanged: the file must still be on disk when the player reads it.
    made = _track_temps(monkeypatch)
    _fake_edge(monkeypatch, audio=b"ID3 audio")
    e = _edge(_Fallback())
    alive: list[bool] = []
    e._play = lambda p, gen=None: alive.append(os.path.exists(p))  # type: ignore[method-assign]
    e.speak("hello")
    assert alive == [True]
    assert made and all(_gone(p) for p in made)


def test_a_later_utterance_cannot_unstop_an_earlier_killed_one():
    # The race #27 guards: while utterance A plays, B starts and stop()s A; A's killed playback must NOT
    # be mistaken for a failure and spoken in the OS voice just because B reset a shared flag.
    fb = _Fallback()
    e = _edge(fb)
    e._gen = 5  # pretend A is generation 5
    e._stopped_gen = 5  # stop() recorded A as stopped…
    e._gen = 6  # …and B (gen 6) then started
    assert e._is_stopped(5)  # A is still considered stopped
    assert not e._is_stopped(6)  # B is not


def test_a_failed_stt_prewarm_remembers_why_it_gave_up(monkeypatch):
    # prewarm swallowed its own exceptions, so a machine that can't build the whisper weights (no disk
    # space, a corrupt HF cache, a proxy blocking huggingface) left NO trace anywhere — while the UI
    # kept offering a restart that re-runs the identical failing load. Keep the reason.
    monkeypatch.setattr(speech, "_MODELS", {})
    monkeypatch.setattr(speech, "_ACTIVE_MODEL", None)
    monkeypatch.setattr(speech, "_LAST_PREWARM_ERROR", None)
    monkeypatch.setattr(speech, "stt_importable", lambda: True)

    def boom(size, device="cpu"):
        raise OSError(f"no space left on device while fetching {size}")

    monkeypatch.setattr(speech, "_build_model", boom)
    assert speech.prewarm() is False
    reason = speech.last_prewarm_error()
    assert reason and "no space left" in reason
    assert speech.DEFAULT_STT_MODEL in reason      # names the preferred size...
    assert "base.en" in reason                     # ...and the lighter fallback that also failed

    # and a later success clears it, so a fixed machine doesn't keep reporting an old failure
    monkeypatch.setattr(speech, "_build_model", lambda size, device="cpu": object())
    assert speech.prewarm() is True
    assert speech.last_prewarm_error() is None


def test_prewarm_without_faster_whisper_says_so_rather_than_nothing(monkeypatch):
    monkeypatch.setattr(speech, "_LAST_PREWARM_ERROR", None)
    monkeypatch.setattr(speech, "stt_importable", lambda: False)
    assert speech.prewarm() is False
    assert "faster-whisper" in (speech.last_prewarm_error() or "")


def test_the_launcher_writes_a_failed_prewarm_to_the_log_and_to_settings(monkeypatch, caplog, tmp_path):
    # The launcher pre-warms BEFORE setup_logging has attached a handler, so the reason has to be
    # recorded deliberately or it is lost: the log line is what a person reads, and the settings key is
    # what the UI needs to stop promising a restart that cannot help.
    import logging

    import helix.logging_setup as logging_setup
    import main as launcher

    configured: list = []
    monkeypatch.setattr(
        logging_setup, "setup_logging",
        lambda path, **kw: configured.append(path) or logging.getLogger("helix"),
    )

    class _Paths:
        log_file = tmp_path / "helix.log"

    class _Settings:
        def __init__(self):
            self.d = {}

        def get(self, k, default=None):
            return self.d.get(k, default)

        def set(self, k, v):
            self.d[k] = v

    settings = _Settings()
    paths = _Paths()
    caplog.set_level(logging.WARNING)
    launcher._record_stt_prewarm(paths, settings, "small.en: OSError: no space left on device")
    assert configured == [paths.log_file]  # logging is brought forward so the line actually lands
    assert "no space left on device" in caplog.text
    assert settings.d[launcher.STT_PREWARM_ERROR] == "small.en: OSError: no space left on device"

    # a later good launch clears the flag, and a good launch with nothing stored writes nothing at all
    launcher._record_stt_prewarm(paths, settings, "")
    assert settings.d[launcher.STT_PREWARM_ERROR] == ""
    fresh = _Settings()
    launcher._record_stt_prewarm(paths, fresh, "")
    assert fresh.d == {}


def test_a_launch_with_voice_off_forgets_a_failure_from_an_earlier_run(monkeypatch, tmp_path):
    """The record says "restarting cannot help", so the console hides the Restart button while it
    stands. If a run that never even ATTEMPTED the pre-warm left it standing, then a user who switched
    voice off after a failure and later switched it back on would be refused the one restart that now
    works — the mirror image of the loop this record exists to end. A run that did not try clears it."""
    import main as launcher

    class _Settings:
        def __init__(self, d):
            self.d = dict(d)

        def get(self, k, default=None):
            return self.d.get(k, default)

        def set(self, k, v):
            self.d[k] = v

    settings = _Settings({"voice_input_on": False,
                          launcher.STT_PREWARM_ERROR: "small.en: OSError: no space left on device"})

    class _Paths:
        log_file = tmp_path / "helix.log"
        settings_file = tmp_path / "settings.json"
        data = tmp_path

    # Both are imported INSIDE the function, so patch them where they are looked up, not on `launcher`.
    monkeypatch.setattr("helix.config.AppPaths.resolve", staticmethod(_Paths))
    monkeypatch.setattr("helix.adapters.json_settings.JsonSettings", lambda _p: settings)
    monkeypatch.setattr(launcher, "_record_stt_prewarm",
                        lambda paths, s, reason: s.set(launcher.STT_PREWARM_ERROR, reason))

    def _boom():  # voice is off, so the model must never be touched
        raise AssertionError("pre-warmed the speech model with voice switched off")

    monkeypatch.setattr("helix.adapters.speech.prewarm", _boom)

    launcher._prewarm_voice_if_enabled()
    assert settings.d[launcher.STT_PREWARM_ERROR] == "", (
        "a stale pre-warm failure survived a launch that never tried — the Restart button stays hidden"
    )


def test_a_launch_with_voice_on_records_the_prewarm_result_it_actually_got(monkeypatch, tmp_path):
    """The join between the two halves — speech.prewarm()/last_prewarm_error() and the settings record
    the Console reads. Both halves were pinned; the ONE line that connects them was not, so renaming or
    reordering it left every test green while the record silently stopped being written: prewarm_error()
    reads "" forever and the Console goes back to offering the Restart button that provably cannot help.
    The voice-OFF test above deliberately stubs _record_stt_prewarm out, so nothing else drives this."""
    import helix.logging_setup as logging_setup
    import main as launcher

    class _Settings:
        def __init__(self, d):
            self.d = dict(d)

        def get(self, k, default=None):
            return self.d.get(k, default)

        def set(self, k, v):
            self.d[k] = v

    class _Paths:
        log_file = tmp_path / "helix.log"
        settings_file = tmp_path / "settings.json"
        data = tmp_path

    # Imported inside _prewarm_voice_if_enabled, so patch them where they are looked up. setup_logging
    # is stubbed because the real one attaches handlers to the root logger for the rest of the session.
    monkeypatch.setattr("helix.config.AppPaths.resolve", staticmethod(_Paths))
    monkeypatch.setattr("helix.adapters.json_settings.JsonSettings", lambda _p: _Settings({}))
    monkeypatch.setattr(logging_setup, "setup_logging", lambda path, **kw: None)
    monkeypatch.setattr("helix.adapters.speaker_embed.prewarm", lambda _dir: True)

    def _run(settings) -> dict:
        monkeypatch.setattr("helix.adapters.json_settings.JsonSettings", lambda _p: settings)
        launcher._prewarm_voice_if_enabled()
        return settings.d

    # A pre-warm that failed leaves the reason the adapter gave, verbatim — that string is the whole
    # point of the record, and the Console shows it in place of the useless Restart offer.
    monkeypatch.setattr(speech, "prewarm", lambda: False)
    monkeypatch.setattr(speech, "last_prewarm_error", lambda: "small.en: OSError: no space left")
    d = _run(_Settings({"voice_input_on": True}))
    assert d[launcher.STT_PREWARM_ERROR] == "small.en: OSError: no space left"

    # A failure the adapter couldn't explain still has to be recorded — "" would read as "voice is fine".
    monkeypatch.setattr(speech, "last_prewarm_error", lambda: None)
    d = _run(_Settings({"voice_input_on": True}))
    assert d[launcher.STT_PREWARM_ERROR] == "the speech model could not be loaded"

    # And the mirror: a pre-warm that worked clears whatever the last bad run left behind.
    monkeypatch.setattr(speech, "prewarm", lambda: True)
    d = _run(_Settings({"voice_input_on": True,
                        launcher.STT_PREWARM_ERROR: "small.en: OSError: no space left"}))
    assert d[launcher.STT_PREWARM_ERROR] == ""
