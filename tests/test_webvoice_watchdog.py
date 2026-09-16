"""WebVoice's watchdog — the ears stay alive for the life of the process.

THE BUG: HELIX heard fine at boot and was deaf an hour later, with nothing in helix.log. The mic
was opened once through PortAudio; a laptop sleep ended that stream silently and nothing reopened
it — and a listen gate that shut for a turn, a transcription or a reply that never came back stayed
shut for good. Driven here as pure logic against a stand-in sounddevice (no real device is ever
opened in a test process — that crashes the interpreter at teardown) with the watchdog thread
halted, its ticks called by hand.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from types import SimpleNamespace

from helix.api import voice_loop
from helix.api.voice_loop import WebVoice


class _Settings:
    def __init__(self):
        self._d = {"voice_input_on": False}  # OFF at construction: the constructor opens no stream

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class _Stt:
    def available(self):
        return True

    def ready(self):
        return True

    def transcribe(self, path):
        return "hello"


class _Tts:
    def __init__(self):
        self.stops = 0

    def available(self):
        return False  # confirmations fall to the quiet branch — no speech thread in tests

    def stop(self):
        self.stops += 1


class _Stream:
    """One opened input stream: alive until PortAudio (the test) says otherwise."""

    def __init__(self):
        self.active = True
        self.aborted = self.stopped = self.closed = False

    def start(self):
        pass

    def abort(self):
        self.aborted, self.active = True, False

    def stop(self):
        self.stopped, self.active = True, False

    def close(self):
        self.closed = True


class _Sd:
    """The sounddevice module's seam: streams that open (or refuse to, `fail_first` times), and
    the PortAudio re-init pair the watchdog uses to refresh the device list."""

    def __init__(self, fail_first=0):
        self.streams: list[_Stream] = []
        self.fail_left = fail_first
        self.terminated = self.initialized = 0

    def RawInputStream(self, **kw):  # noqa: N802 — sounddevice's name
        if self.fail_left:
            self.fail_left -= 1
            raise OSError("no input device")
        stream = _Stream()
        self.streams.append(stream)
        return stream

    def _terminate(self):
        self.terminated += 1

    def _initialize(self):
        self.initialized += 1

    def query_devices(self, kind=None):
        return {"name": "Fake Mic"}


def _voice(monkeypatch, sd=None, tts=None) -> WebVoice:
    sd = sd if sd is not None else _Sd()
    monkeypatch.setattr(voice_loop, "_sounddevice", lambda: sd)
    v = WebVoice(_Settings(), _Stt(), tts or _Tts())
    v._halt.set()  # the thread retires; the tests tick the watchdog by hand
    v._media = SimpleNamespace(tick=lambda: None, playing=lambda: False)  # no WASAPI in a test
    v.can_listen = lambda: True  # type: ignore[method-assign]
    v.enabled = lambda: True  # type: ignore[method-assign]
    return v


_LIVE = b"\x01\x00" * 1600  # 100 ms of not-quite-silence — a real mic's hiss


# ----- the callback -----

def test_a_faulting_callback_never_escapes_to_portaudio(monkeypatch):
    """sounddevice ABORTS a stream whose callback raises — permanently. One bad chunk is dropped."""
    v = _voice(monkeypatch)
    v._listening = True
    monkeypatch.setattr(v._vad, "push", lambda chunk: (_ for _ in ()).throw(RuntimeError("boom")))
    v._on_audio(_LIVE, 1600, None, None)  # must not raise
    assert v._audio_faults == 1
    assert v._last_audio_ts > 0  # and the chunk still counted as the stream being alive


# ----- a stream that died under us -----

def test_a_stalled_stream_is_reopened_with_a_fresh_device_list(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="helix")
    sd = _Sd()
    v = _voice(monkeypatch, sd)
    v._start_stream()
    first = v._stream
    assert first is sd.streams[0] and v._stream_wanted
    now = time.monotonic()
    v._check_stream(now)  # fresh audio: nothing to do
    assert v._stream is first
    v._last_audio_ts = now - 10
    v._check_stream(now)  # one silent tick is suspicion only (the GIL may have starved it)
    assert v._stream is first
    v._check_stream(now + 2)  # two in a row: dead
    assert first.aborted and first.closed  # a dead stream is aborted, never waited on
    assert (sd.terminated, sd.initialized) == (1, 1)  # PortAudio asked for the devices again
    assert v._stream is sd.streams[1] and v._stream_wanted
    assert "no audio for" in caplog.text and "microphone open: Fake Mic" in caplog.text


def test_a_stream_portaudio_stopped_is_reopened_at_once(monkeypatch):
    sd = _Sd()
    v = _voice(monkeypatch, sd)
    v._start_stream()
    sd.streams[0].active = False  # the device vanished; PortAudio's thread gave up
    v._check_stream(time.monotonic())
    assert len(sd.streams) == 2 and v._stream is sd.streams[1]


def test_digital_silence_is_a_dead_device_with_a_growing_window(monkeypatch):
    sd = _Sd()
    v = _voice(monkeypatch, sd)
    v._start_stream()
    v._on_audio(bytes(3200), 1600, None, None)  # exact zero — no mic delivers that for long
    assert v._zero_since > 0
    now = v._zero_since + voice_loop._ZERO_WINDOW_S
    v._last_audio_ts = now  # the callback IS alive; only what it carries is dead
    v._check_stream(now)
    assert len(sd.streams) == 2 and v._zero_reopens == 1
    assert v._zero_window == voice_loop._ZERO_WINDOW_S * 2  # an OS-muted mic costs less each time
    v._on_audio(_LIVE, 1600, None, None)  # real signal again: the window resets
    assert v._zero_since == 0 and v._zero_reopens == 0
    assert v._zero_window == voice_loop._ZERO_WINDOW_S


def test_a_failed_open_is_retried_with_backoff(monkeypatch):
    sd = _Sd(fail_first=2)
    v = _voice(monkeypatch, sd)
    v._start_stream()  # the audio service isn't up yet
    assert v._stream is None and v._stream_wanted and v._reopen_fails == 1
    v._check_stream(time.monotonic())  # inside the backoff: no attempt
    assert v._reopen_fails == 1
    v._check_stream(v._reopen_next)  # due: the second attempt fails too, and waits longer
    assert v._reopen_fails == 2 and v._reopen_next > time.monotonic() + 4
    v._check_stream(v._reopen_next)  # third time: the mic is there
    assert v._stream is sd.streams[0] and v._reopen_fails == 0


def test_the_watchdog_only_keeps_a_stream_hands_free_asked_for(monkeypatch):
    sd = _Sd()
    v = _voice(monkeypatch, sd)
    v._check_stream(time.monotonic())  # never opened (a test rig, PTT-only): never opens one
    assert sd.streams == []
    v._start_stream()
    v.set_enabled(False)  # the user turned voice off: the stream goes and stays gone
    assert v._stream is None and not v._stream_wanted
    v._check_stream(time.monotonic() + 100)
    assert len(sd.streams) == 1


# ----- a gate that shut and never reopened -----

def test_a_voice_stuck_thinking_with_no_turn_in_flight_is_idled(monkeypatch):
    v = _voice(monkeypatch)
    busy = {"on": False}
    v.busy_probe = lambda: busy["on"]
    v._set_state("thinking")
    v._state_since -= 20
    now = time.monotonic()
    ticks = voice_loop._STUCK_THINKING_TICKS
    for _ in range(ticks - 1):
        v._check_stuck(now)
    assert v.state() == "thinking"  # not on a hunch: a run of ticks agreeing
    v._check_stuck(now)
    assert v.state() == "idle"
    # A turn really running is left alone however long it takes.
    busy["on"] = True
    v._set_state("thinking")
    v._state_since -= 600
    for _ in range(ticks * 3):
        v._check_stuck(now)
    assert v.state() == "thinking"
    # And with no shell to ask (no probe) the watchdog never judges 'thinking' at all.
    v.busy_probe = None
    for _ in range(ticks * 3):
        v._check_stuck(now)
    assert v.state() == "thinking"


def test_a_transcription_that_never_returns_is_abandoned(monkeypatch):
    v = _voice(monkeypatch)
    v._set_state("transcribing")
    v._barge_busy = v._camera_stt_busy = True
    v._stt_since = time.monotonic() - voice_loop._STUCK_TRANSCRIBING_S - 1
    v._check_stuck(time.monotonic())
    assert v.state() == "idle" and not v._barge_busy and not v._camera_stt_busy
    assert v._stt_since == 0  # the next utterance gets through


def test_a_reply_that_never_stops_playing_is_hushed(monkeypatch):
    tts = _Tts()
    v = _voice(monkeypatch, tts=tts)
    v._set_state("speaking")
    v._state_since -= voice_loop._STUCK_SPEAKING_S + 1
    v._check_stuck(time.monotonic())
    assert v.state() == "idle" and tts.stops == 1


def test_a_crashing_utterance_handler_returns_the_voice_to_idle(monkeypatch):
    v = _voice(monkeypatch)
    v._set_state("transcribing")
    handle, path = tempfile.mkstemp(suffix=".wav")
    os.close(handle)
    crashed = threading.Event()

    def boom(text):
        crashed.set()
        raise RuntimeError("handler crashed")

    v._transcribe(path, boom)
    assert crashed.wait(2.0)
    deadline = time.monotonic() + 2.0
    while v.state() != "idle" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert v.state() == "idle"
    assert not os.path.exists(path)


def test_shutdown_halts_the_watchdog(monkeypatch):
    sd = _Sd()
    v = WebVoice(_Settings(), _Stt(), _Tts())
    monkeypatch.setattr(voice_loop, "_sounddevice", lambda: sd)
    assert not v._halt.is_set()
    v.shutdown()
    assert v._halt.is_set()
    v._tick(time.monotonic())  # a closed voice's tick is a no-op
    assert sd.streams == []
