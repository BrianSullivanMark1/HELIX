"""Voice — hands-free wake word ("HELIX"), local transcription, and spoken replies.

Part of the immutable shell (helix/ui/). The low-level pieces are a straight, proven port:
  - VadSegmenter   : pure energy + trailing-silence segmentation (no Qt — unit-testable).
  - WakeWordListener / MicRecorder : QtMultimedia mic capture, guarded so a missing backend just
                     disables voice instead of breaking the app.
  - VoiceController: ties the listener to the injected SpeechIn (STT) and SpeechOut (TTS) ports and
                     runs the conversation-session state machine (say "HELIX" to engage, "goodbye" to
                     end the session). Transcription and speech run on QtWorkers so the UI never blocks.

Everything degrades silently: no mic, no faster-whisper, or no OS voice → the controller reports
unavailable and the Console stays a normal text app.
"""
from __future__ import annotations

import array
import json
import math
import os
import re
import tempfile
import wave
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from helix.logging_setup import get_logger
from helix.ports.speech import SpeechIn, SpeechOut
from helix.ports.stores import SettingsStore
from helix.ui.orb import OrbState
from helix.ui.workers import QtWorker

try:  # QtMultimedia ships with PyQt6 but needs platform plugins; degrade if it can't load.
    from PyQt6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices

    _MULTIMEDIA = True
except Exception:  # pragma: no cover - depends on the host's Qt plugins
    _MULTIMEDIA = False

_LOG = get_logger("voice")

VOICE_SETTING = "voice_input_on"  # hands-free mic on/off; persisted, default off

# --- Energy-based voice-activity detection (VAD). Threshold is ADAPTIVE — it tracks the ambient
# noise floor, so it works across mics instead of a single fixed level that mis-fires on one. ----
WAKE_RMS_FLOOR = 260.0       # absolute minimum speech threshold (int16 RMS)
WAKE_SPEECH_FACTOR = 3.2     # speech must be this many× the running ambient noise floor
WAKE_NOISE_INIT = 200.0      # starting noise-floor estimate
WAKE_END_SILENCE_S = 0.8     # trailing quiet that ends an utterance — short, so a turn starts fast
                             # (the single biggest latency win; was 3.0s and felt sluggish)
WAKE_MIN_SPEECH_S = 0.3      # ignore shorter blips (clicks, coughs)
WAKE_MAX_UTTER_S = 12.0      # hard cap per utterance
WAKE_PREROLL_S = 0.5         # keep this much pre-speech audio so the "H" onset of HELIX isn't clipped

SESSION_IDLE_MS = 5 * 60 * 1000  # end the conversation session after this much inactivity (5 min)
PTT_MAX_MS = 20 * 1000           # hard cap on one push-to-talk capture (safety if 'released' is missed)

# Accept the obvious mis-hearings of "HELIX" so a clear command still lands.
_WAKE_RE = re.compile(
    r"\b(?:hey\s+|ok\s+|okay\s+)?"
    r"(?:he+l+ix|helics?|helicks|heli[ckx]s?|healix|healex|hel[eu]x|heelux|hilux)\b[\s,.:;!?-]*",
    re.IGNORECASE,
)
# Phrases that close an active conversation session immediately (no wake word needed).
_DISMISSAL_RE = re.compile(
    r"\b(?:good\s*bye|bye(?:\s+now)?|be\s+right\s+back|brb|i'?ll\s+be\s+back|"
    r"that'?s\s+all|thank(?:s| you)\s*,?\s*he+lix)\b",
    re.IGNORECASE,
)
# An action verb means the utterance is a REQUEST, not a sign-off — so "build a goodbye card" or
# "that's all wrong, fix the layout" never end the session just because they contain 'goodbye'/'that's all'.
_ACTION_RE = re.compile(
    r"\b(?:build|make|create|add|fix|change|generate|design|open|show|set|turn|play|delete|remove|"
    r"update|put|write|draw|model|edit|rename|move|connect|install)\b",
    re.IGNORECASE,
)


def _pcm_rms(pcm: bytes) -> float:
    """RMS level of 16-bit little-endian mono PCM (stdlib only)."""
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _pcm_bands(pcm: bytes, n: int = 16) -> list[float]:
    """`n` log-spaced frequency-band energies (each ~0..1) from 16-bit LE mono PCM, via an rFFT — feeds
    the orb's spectral ring. Degrades to zeros (no ring) if numpy is unavailable, so it's purely additive."""
    try:
        import numpy as np

        usable = len(pcm) - (len(pcm) % 2)
        if usable < 64:
            return [0.0] * n
        x = np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32)
        x *= np.hanning(x.size)
        spec = np.abs(np.fft.rfft(x))
        edges = np.unique(
            np.clip(np.logspace(np.log10(2), np.log10(spec.size), n + 1).astype(int), 1, spec.size)
        )
        if edges.size < 2:
            return [0.0] * n
        out = [float(spec[a:max(a + 1, b)].mean()) for a, b in zip(edges[:-1], edges[1:])]
        out += [0.0] * (n - len(out))
        return [min(1.0, (v / 90000.0) ** 0.6) for v in out[:n]]
    except Exception:
        return [0.0] * n


def split_wake(text: str) -> tuple[bool, str]:
    """(matched, command): if 'HELIX' is in `text`, return True + the words after it, else (False, '')."""
    match = _WAKE_RE.search(text or "")
    if not match:
        return False, ""
    return True, (text[match.end():] or "").strip()


def is_dismissal(text: str) -> bool:
    """True if `text` closes an active conversation session (e.g. 'goodbye', 'that's all') — but NOT when
    it's actually a request that merely contains such a word ('build a goodbye card', 'that's all wrong,
    fix it'), which must reach the model instead of silently ending the session."""
    text = text or ""
    return bool(_DISMISSAL_RE.search(text)) and not _ACTION_RE.search(text)


_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")  # [label](url) -> label
_MD_SYMS = re.compile(r"[*_`#~>|]+")              # markdown emphasis / headings / code / quotes / tables
_BULLET = re.compile(r"(?m)^\s*[-•·]\s+")         # list bullets at line start
_WS = re.compile(r"\s+")
# A ```viz block carries a table/chart spec the orb DISPLAYS but must never read aloud.
_VIZ_RE = re.compile(r"```viz\s*\n?(.*?)```", re.DOTALL)


def split_visuals(text: str) -> tuple[str, list[dict]]:
    """Split a reply into its spoken prose and any ```viz table/chart specs. The visuals are shown in the
    transcript; only the prose is spoken — so the data itself is seen, never read aloud."""
    specs: list[dict] = []

    def _take(match: "re.Match[str]") -> str:
        try:
            spec = json.loads(match.group(1).strip())
        except Exception:
            return ""  # malformed block — drop it from speech and from display
        if isinstance(spec, dict) and spec.get("type") in ("table", "chart"):
            specs.append(spec)
        return ""

    spoken = _VIZ_RE.sub(_take, text or "").strip()
    return spoken, specs


def speakable(text: str) -> str:
    """Strip viz blocks, markdown, and symbols so the voice never reads a table/chart or punctuation as
    words (e.g. '*' → 'asterisk'). A safety net behind the system prompt's plain-spoken instruction."""
    t = _VIZ_RE.sub("", text or "")  # never read a table/chart block aloud
    t = _MD_LINK.sub(r"\1", t)
    t = _BULLET.sub("", t)
    t = _MD_SYMS.sub("", t)
    return _WS.sub(" ", t).strip()


# Short phrases that interrupt HELIX — "stop" / "stop talking" / "be quiet" / "never mind". Matched only
# as a WHOLE short utterance (after stripping fillers), so a real instruction that merely contains the
# word — "stop the timer at zero", "add a stop button", "cancel that order screen" — is NOT swallowed.
_STOP_FILLERS = re.compile(r"\b(?:um+|uh+|okay|ok|please|yeah|yep|hey|helix|now|just|like)\b", re.IGNORECASE)
# Stop / cancel a build (or hush speech). Includes explicit "stop build" forms so a build is reliably
# halted by voice — distinct from MUTE below, which only pauses the mic and never touches a build.
_STOP_FORMS = re.compile(
    r"^(?:no\s+)*"  # 'no no stop'
    r"(?:stop(?:\s+stop)*(?:\s+(?:it|talking|build|building|the\s+build))?|"
    r"cancel(?:\s+(?:that|build|building|the\s+build))?|abort|halt|"
    r"be\s+quiet|never\s*mind|that'?s\s+enough|enough|shut\s*up|hush|shush|quiet)$",
    re.IGNORECASE,
)
# MUTE / UNMUTE — pause/resume the user's mic WITHOUT stopping a build (the opposite of a stop command).
# Whole-utterance matches only, so "mute the alarm app" or "stop listening to the radio build" don't fire.
_MUTE_FORMS = re.compile(
    r"^(?:mute(?:\s+(?:yourself|me|the\s+mic|my\s+mic|mic))?|stop\s+listening|pause\s+listening|"
    r"stop\s+the\s+mic|mic\s+off)$",
    re.IGNORECASE,
)
_UNMUTE_FORMS = re.compile(
    r"^(?:unmute(?:\s+(?:yourself|me|the\s+mic|mic))?|start\s+listening|resume\s+listening|"
    r"listen\s+again|mic\s+on|you\s+can\s+listen(?:\s+again)?)$",
    re.IGNORECASE,
)


def _clean_command(text: str) -> str:
    t = re.sub(r"[.!,?]+", " ", (text or "").lower())
    t = _STOP_FILLERS.sub(" ", t)
    return " ".join(t.split())


def is_stop(text: str) -> bool:
    """True only when the WHOLE short utterance is a stop/cancel/hush command (fillers ignored)."""
    return bool(_STOP_FORMS.match(_clean_command(text)))


def is_mute(text: str) -> bool:
    """True when the whole utterance asks to pause the mic (mute the user), not stop a build."""
    return bool(_MUTE_FORMS.match(_clean_command(text)))


def is_unmute(text: str) -> bool:
    """True when the whole utterance asks to resume listening (unmute the user)."""
    return bool(_UNMUTE_FORMS.match(_clean_command(text)))


def _write_wav16(data: bytes, path: str) -> None:
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)  # Int16
        handle.setframerate(16000)
        handle.writeframes(data)


def _mono16k_format():
    fmt = QAudioFormat()
    fmt.setSampleRate(16000)
    fmt.setChannelCount(1)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    return fmt


class VadSegmenter:
    """Turns a stream of PCM chunks into complete spoken utterances using energy + trailing silence.
    Pure (no Qt) so the segmentation is unit-testable; the listener feeds it live mic chunks."""

    def __init__(self, sample_rate: int = 16000) -> None:
        bytes_per_s = sample_rate * 2  # 16-bit mono
        self._end_silence = int(WAKE_END_SILENCE_S * bytes_per_s)
        self._min_speech = int(WAKE_MIN_SPEECH_S * bytes_per_s)
        self._max_bytes = int(WAKE_MAX_UTTER_S * bytes_per_s)
        self._preroll_cap = int(WAKE_PREROLL_S * bytes_per_s)
        self._noise = WAKE_NOISE_INIT  # adapts to ambient; persists across utterances
        self.reset()

    def reset(self) -> None:
        self._in_speech = False
        self._utter = bytearray()
        self._silence = 0
        self._preroll = bytearray()

    @property
    def threshold(self) -> float:
        return max(WAKE_RMS_FLOOR, self._noise * WAKE_SPEECH_FACTOR)

    def push(self, chunk: bytes) -> bytes | None:
        """Feed a chunk; return a completed utterance (bytes) when one ends, else None."""
        if not chunk:
            return None
        rms = _pcm_rms(chunk)
        loud = rms >= self.threshold
        if loud:
            if not self._in_speech:
                self._in_speech = True
                self._utter = bytearray(self._preroll)  # seed with pre-roll so the wake word survives
                self._preroll = bytearray()
            self._utter += chunk
            self._silence = 0
        elif self._in_speech:
            self._utter += chunk
            self._silence += len(chunk)
            if self._silence >= self._end_silence:
                return self._finish()
        else:
            self._noise = 0.95 * self._noise + 0.05 * rms  # track the ambient noise floor
            self._preroll += chunk
            if len(self._preroll) > self._preroll_cap:
                del self._preroll[: len(self._preroll) - self._preroll_cap]
        if self._in_speech and len(self._utter) >= self._max_bytes:
            return self._finish()
        return None

    def _finish(self) -> bytes | None:
        utter = bytes(self._utter)
        spoken = len(utter) - self._silence  # rough speech length, minus trailing quiet
        self.reset()
        return utter if spoken >= self._min_speech else None


class WakeWordListener(QObject):
    """Always-on, hands-free mic capture. Continuously reads the mic, segments speech with
    VadSegmenter, and emits each finished utterance. Processing is gated by set_active() so it goes
    quiet while HELIX is transcribing / thinking / speaking (it never hears its own reply)."""

    utterance = pyqtSignal(bytes)
    level = pyqtSignal(float)  # 0..1 mic level, for a live meter
    bands = pyqtSignal(list)   # per-band FFT energies (0..1), for the orb's spectral ring

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._source = None
        self._io = None
        self._device = None
        self._format = None
        self._available = False
        self._active = False
        self._seg = VadSegmenter()
        if not _MULTIMEDIA:
            return
        try:
            device = QMediaDevices.defaultAudioInput()
            if device is None or device.isNull():
                return
            self._device = device
            self._format = _mono16k_format()
            self._available = True
        except Exception:
            self._available = False

    def is_available(self) -> bool:
        return self._available

    def start(self) -> bool:
        if not self._available:
            return False
        try:
            self._seg.reset()
            self._source = QAudioSource(self._device, self._format, self)
            self._io = self._source.start()
            if self._io is None:
                self._source = None
                return False
            self._io.readyRead.connect(self._on_ready)
            self._active = True
            return True
        except Exception:
            self._source = None
            self._io = None
            return False

    def set_active(self, on: bool) -> None:
        """Gate processing without tearing down the stream: while inactive, mic chunks are drained
        and discarded (VAD reset), so HELIX never transcribes its own spoken replies."""
        if on and not self._active:
            self._seg.reset()
        self._active = bool(on)

    def stop(self) -> None:
        if self._source is not None:
            try:
                self._source.stop()
            except Exception:
                pass
        self._source = None
        self._io = None
        self._active = False
        self._seg.reset()

    def _on_ready(self) -> None:
        if self._io is None:
            return
        chunk = bytes(self._io.readAll())  # always drain so the device buffer can't back up
        if not chunk or not self._active:
            return
        self.level.emit(min(1.0, _pcm_rms(chunk) / 8000.0))
        self.bands.emit(_pcm_bands(chunk))
        utter = self._seg.push(chunk)
        if utter:
            self.utterance.emit(utter)


class MicRecorder(QObject):
    """Push-to-talk capture via QAudioSource, written out as a 16 kHz mono WAV for transcription.
    Optional/guarded: if the backend or an input device is missing, is_available() is False."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._source = None
        self._io = None
        self._buffer = bytearray()
        self._device = None
        self._format = None
        self._available = False
        if not _MULTIMEDIA:
            return
        try:
            device = QMediaDevices.defaultAudioInput()
            if device is None or device.isNull():
                return
            self._device = device
            self._format = _mono16k_format()
            self._available = True
        except Exception:
            self._available = False

    def is_available(self) -> bool:
        return self._available

    def start(self) -> bool:
        if not self._available:
            return False
        try:
            self._buffer = bytearray()
            self._source = QAudioSource(self._device, self._format, self)
            self._io = self._source.start()
            if self._io is None:
                self._source = None
                return False
            self._io.readyRead.connect(self._on_ready)
            return True
        except Exception:
            self._source = None
            self._io = None
            return False

    def _on_ready(self) -> None:
        if self._io is not None:
            self._buffer += bytes(self._io.readAll())

    def stop(self) -> bytes:
        if self._io is not None:
            try:
                self._buffer += bytes(self._io.readAll())
            except Exception:
                pass
        if self._source is not None:
            try:
                self._source.stop()
            except Exception:
                pass
        data = bytes(self._buffer)
        self._buffer = bytearray()
        self._source = None
        self._io = None
        return data


# state -> the orb's visual state
_ORB = {
    "idle": OrbState.IDLE,
    "listening": OrbState.LISTENING,
    "transcribing": OrbState.TRANSCRIBING,
    "thinking": OrbState.THINKING,
    "speaking": OrbState.SPEAKING,
}


class VoiceController(QObject):
    """The voice brain: hands-free wake word → local STT → conversation session → spoken replies.

    It owns the listener and the worker threads, but NOT the conversation: when it captures a command
    it emits `recognized(text)` and the Console runs the turn, then calls `speak(reply)`. The mic is
    gated off the whole time HELIX is busy, so it never transcribes its own voice.
    """

    recognized = pyqtSignal(str)       # a user command captured by voice — the Console runs it
    stateChanged = pyqtSignal(object)  # an OrbState for the orb + status line
    level = pyqtSignal(float)          # 0..1 mic level while listening
    bands = pyqtSignal(list)           # per-band FFT energies for the orb's spectral ring
    stopRequested = pyqtSignal()       # the user said "stop" — the Console cancels any pending reply
    mutedChanged = pyqtSignal(bool)    # the mic was muted/unmuted — the Console updates its control

    def __init__(
        self,
        speech_in: SpeechIn,
        speech_out: SpeechOut,
        settings: SettingsStore,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._stt = speech_in
        self._tts = speech_out
        self._settings = settings
        self._workers: set[QtWorker] = set()
        self._listener: WakeWordListener | None = None
        self._recorder: MicRecorder | None = None
        self._state = "idle"
        self._session = False
        self._ptt = False
        self._barge_busy = False          # one in-flight 'stop?' transcription at a time while speaking
        self._narrating = False           # a progress note is being spoken (skip new ones until it ends)
        self._muted = False               # user paused the mic: ignore all speech except unmute/stop
        self._mic_ok: bool | None = None  # cache the device probe; don't reopen it on every settle
        self._session_timer = QTimer(self)
        self._session_timer.setSingleShot(True)
        self._session_timer.timeout.connect(self._end_session)
        self._ptt_timer = QTimer(self)
        self._ptt_timer.setSingleShot(True)
        self._ptt_timer.timeout.connect(self._ptt_watchdog)

    # ----- capability -----
    def mic_available(self) -> bool:
        if self._mic_ok is None:
            self._mic_ok = _MULTIMEDIA and WakeWordListener().is_available()
        return self._mic_ok

    def can_listen(self) -> bool:
        """Hands-free can run only if the mic works AND the STT model is loaded (pre-warmed). We never
        build the model after Qt is up — that crashes on Windows — so an un-prewarmed model means no."""
        return self.mic_available() and self._stt.available() and self._stt.ready()

    def supported(self) -> bool:
        """The host has a mic and faster-whisper — voice can work (perhaps after a restart to prewarm)."""
        return self.mic_available() and self._stt.available()

    def restart_required(self) -> bool:
        """Installed and mic-capable, but the model wasn't pre-warmed this run (voice was off at launch).
        Saving voice on + restarting loads it before Qt, after which hands-free works every launch."""
        return self.supported() and not self._stt.ready()

    def enabled(self) -> bool:
        return bool(self._settings.get(VOICE_SETTING, False))

    # ----- on/off -----
    def set_enabled(self, on: bool) -> bool:
        """Turn hands-free on/off and remember it. Returns True if it actually started listening."""
        self._settings.set(VOICE_SETTING, bool(on))
        if on:
            return self._start_wake()
        self.interrupt()  # stop any in-flight speech right away — toggling off goes quiet immediately
        self._muted = False  # turning voice off clears any mute state, so re-enabling starts listening
        self._stop_wake()
        self._end_session()
        self._set_state("idle")
        return False

    def start_if_enabled(self) -> None:
        """Called once at launch: begin listening if the user left voice on and it's ready."""
        if self.enabled():
            self._start_wake()

    def _start_wake(self) -> bool:
        self._stop_wake()
        if not self.can_listen():
            return False
        self._listener = WakeWordListener(self)
        if not self._listener.is_available():
            self._listener = None
            return False
        self._listener.utterance.connect(self._on_utterance)
        self._listener.level.connect(self.level)
        self._listener.bands.connect(self.bands)
        if not self._listener.start():
            self._listener = None
            return False
        self._set_state("idle")
        return True

    def _stop_wake(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    # ----- session -----
    def _start_session(self) -> None:
        self._session = True
        self._session_timer.start(SESSION_IDLE_MS)

    def _end_session(self) -> None:
        self._session = False
        self._session_timer.stop()

    # ----- state machine -----
    def _set_state(self, state: str) -> None:
        self._state = state
        if state == "idle":
            # Re-arm hands-free when we settle (also restores it after a push-to-talk cycle).
            if self.enabled() and self._listener is None and self.can_listen():
                self._start_wake()
                return  # _start_wake re-enters _set_state("idle")
        if self._listener is not None:
            # Listen while idle (full commands) AND while busy (speaking OR thinking/building) — during
            # 'busy' only a short "stop" acts (barge-in), so the user can halt a long build by voice.
            self._listener.set_active(state in ("idle", "speaking", "thinking") and self.enabled())
        self.stateChanged.emit(_ORB.get(state, OrbState.IDLE))

    # ----- wake-word flow -----
    @staticmethod
    def _pcm_to_wav(pcm: bytes) -> str | None:
        try:
            handle, path = tempfile.mkstemp(suffix=".wav", prefix="helix_wake_")
            os.close(handle)
            _write_wav16(pcm, path)
            return path
        except Exception:
            return None

    # ----- mute (pause the mic without stopping a build) -----
    def is_muted(self) -> bool:
        return self._muted

    def set_muted(self, on: bool) -> None:
        """Pause/resume listening. Muting does NOT end the session or stop a build — it only changes what
        HELIX acts on; the listener stays live (when enabled) so an 'unmute'/'stop' still works by voice.
        Mute is refused when nothing is actually listening (so we never advertise a muted mic that wasn't
        live, leaving an escape-less state); UNMUTE is always honored, so recovery is never blocked."""
        on = bool(on)
        if on and not self.can_listen():
            return
        if on == self._muted:
            return
        self._muted = on
        if on:  # going quiet — hush any in-flight narration so a mute feels immediate
            self._narrating = False
            try:
                self._tts.stop()
            except Exception:
                pass
        self.mutedChanged.emit(on)

    def toggle_muted(self) -> None:
        self.set_muted(not self._muted)

    def _on_muted_text(self, text: str) -> None:
        # While muted, ignore everything EXCEPT a short unmute or stop phrase — so you can always come back
        # (or halt a build) by voice, but HELIX never starts a turn or build from your muted speech.
        self._barge_busy = False
        t = (text or "").strip()
        if not t or len(t.split()) > 5:
            return
        if is_unmute(t):
            self.set_muted(False)
        elif is_stop(t):
            try:
                self._tts.stop()
            except Exception:
                pass
            self.stopRequested.emit()

    def _on_utterance(self, pcm: bytes) -> None:
        # Muted: route to the mute handler — only an unmute/stop phrase acts; all other speech is dropped.
        if self._muted:
            if self._barge_busy:
                return
            path = self._pcm_to_wav(pcm)
            if path is None:
                return
            self._barge_busy = True
            self._transcribe(path, self._on_muted_text)
            return
        # While speaking/thinking — OR while a background build is narrating (state is 'idle' then) — only
        # a short "stop" counts. Otherwise HELIX's own narration, picked up by the open mic, would be
        # transcribed as a brand-new (billed) command.
        if self._state in ("speaking", "thinking") or self._narrating:  # barge-in: only "stop" interrupts
            if self._barge_busy:
                return
            path = self._pcm_to_wav(pcm)
            if path is None:
                return
            self._barge_busy = True
            self._transcribe(path, self._on_barge_text)
            return
        if self._state != "idle":  # transcribing / thinking — ignore
            return
        path = self._pcm_to_wav(pcm)
        if path is None:
            return
        self._set_state("transcribing")
        self._transcribe(path, self._on_wake_text)

    def _on_barge_text(self, text: str) -> None:
        # While HELIX is busy (speaking a reply OR building), only a SHORT stop phrase counts — so its
        # own reply/narration audio, picked up by the mic, can't make it cut itself off.
        self._barge_busy = False
        t = (text or "").strip()
        if t and len(t.split()) <= 4 and is_stop(t):
            try:
                self._tts.stop()  # hush any speech/narration now
            except Exception:
                pass
            self._narrating = False
            # Don't force idle here: the Console cancels the running turn/build and drives the orb state
            # when the worker actually unwinds (stopping TTS ends a speaking turn on its own).
            self.stopRequested.emit()

    def _on_wake_text(self, text: str) -> None:
        text = (text or "").strip()
        if self._muted:  # a mute landed WHILE this was transcribing — honor only unmute/stop, drop the rest
            self._on_muted_text(text)
            self._set_state("idle")
            return
        matched, after = split_wake(text)
        if self._session and is_dismissal(text):
            self._end_session()
            self._say("Goodbye, sir.")  # acknowledge, then drop back to wake-word-only
            return
        if matched:
            command = after.strip()
        elif self._session and text:
            command = text  # inside an active session the wake word isn't required
        else:
            self._set_state("idle")  # not addressed to HELIX — keep listening
            return
        if is_mute(command):  # "mute / stop listening" — pause the mic; never a turn, never a build-stop
            self.set_muted(True)
            self._set_state("idle")
            return
        if is_stop(command):  # "stop / be quiet / never mind" — hush and keep listening, no new turn
            self.interrupt()
            self.stopRequested.emit()
            return
        self._start_session()
        if not command:
            self._say("Yes?")  # bare "HELIX" — acknowledge and wait for the command
            return
        self._set_state("thinking")
        self.recognized.emit(command)

    # ----- push-to-talk (manual capture; works whenever voice is ready) -----
    def ptt_start(self) -> bool:
        if self._state != "idle" or not self.can_listen():
            return False
        self._stop_wake()  # release the device so the recorder can take it
        self._recorder = MicRecorder(self)
        if not self._recorder.is_available() or not self._recorder.start():
            self._recorder = None
            self._set_state("idle")
            return False
        self._ptt = True
        self._ptt_timer.start(PTT_MAX_MS)
        self._set_state("listening")
        return True

    def _ptt_watchdog(self) -> None:
        """Failsafe: if the button's 'released' never arrived (focus loss, disabled mid-hold), end the
        capture anyway so the mic isn't held open and hands-free can re-arm."""
        if self._ptt:
            self.ptt_stop()

    def ptt_stop(self) -> None:
        if not self._ptt:
            return
        self._ptt = False
        self._ptt_timer.stop()
        data = self._recorder.stop() if self._recorder is not None else b""
        self._recorder = None
        if not data:
            self._set_state("idle")  # re-arms hands-free if enabled
            return
        self._set_state("transcribing")
        try:
            handle, path = tempfile.mkstemp(suffix=".wav", prefix="helix_ptt_")
            os.close(handle)
            _write_wav16(data, path)
        except Exception:
            self._set_state("idle")
            return
        self._transcribe(path, self._on_ptt_text)

    def _on_ptt_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            self._set_state("idle")
            return
        if self._muted:  # paused (incl. a mute that landed mid-capture): only unmute/stop, never a turn
            self._on_muted_text(text)
            self._set_state("idle")
            return
        if is_mute(text):  # push-to-talk "mute" pauses the mic instead of starting a turn
            self.set_muted(True)
            self._set_state("idle")
            return
        self._start_session()
        self._set_state("thinking")
        self.recognized.emit(text)

    # ----- speaking (called by the Console after a reply, and for internal acks) -----
    def begin_turn(self) -> None:
        """The Console is about to run a turn (typed or voice) — go quiet and show 'thinking'."""
        self._set_state("thinking")

    def speak(self, text: str) -> None:
        # Preempt ANY in-flight speech (a progress note, or an earlier reply still playing) so two
        # utterances can never overlap into two voices talking at once.
        self._narrating = False
        try:
            self._tts.stop()
        except Exception:
            pass
        text = speakable(text)  # strip markdown/symbols so they aren't read aloud as words
        if not text or not self._tts.available():
            self._set_state("idle")
            return
        self._set_state("speaking")
        self._run(lambda _emit: self._tts.speak(text), lambda *_: self._set_state("idle"))

    def narrate(self, text: str) -> None:
        """Speak a short progress note as HELIX works, WITHOUT changing the turn state (the mic stays
        gated, the orb keeps 'thinking'). Skips while a previous note is still speaking, so notes pace
        themselves to speech and never stack up — turning a stream of steps into spoken milestones."""
        if self._narrating or self._muted or not self.enabled():
            return  # muted means quiet: don't speak progress notes (HELIX would also hear itself)
        text = speakable(text)
        if not text or not self._tts.available():
            return
        self._narrating = True
        # allow_fallback=False: progress notes stay in ONE voice — a transient neural-TTS failure skips
        # the note instead of speaking it in the OS voice (which made consecutive notes flip voices).
        self._run(lambda _emit: self._tts.speak(text, allow_fallback=False),
                  lambda *_: self._clear_narrating())

    def _clear_narrating(self) -> None:
        self._narrating = False

    def idle(self) -> None:
        self._set_state("idle")

    def is_active(self) -> bool:
        """True while HELIX is busy (transcribing / thinking / speaking) — i.e. interruptible."""
        return self._state != "idle"

    def interrupt(self) -> None:
        """Stop talking right now and return to listening — the 'stop' action."""
        try:
            self._tts.stop()
        except Exception:
            pass
        self._narrating = False
        self._set_state("idle")

    def _say(self, text: str) -> None:
        """Speak an internal acknowledgement (not a model turn), then return to listening."""
        self.speak(text)

    # ----- workers -----
    def _transcribe(self, path: str, on_text: Callable[[str], None]) -> None:
        def work(_emit: Callable[[str], None]) -> str:
            try:
                return self._stt.transcribe(Path(path))
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass

        self._run(work, on_text)

    def _run(self, fn: Callable[[Callable[[str], None]], object], on_done: Callable[..., None]) -> None:
        worker = QtWorker(fn)
        self._workers.add(worker)
        worker.finished_ok.connect(on_done)
        worker.failed.connect(lambda _err: on_done(""))
        worker.finished.connect(lambda w=worker: self._retire(w))
        worker.start()

    def _retire(self, worker: QtWorker) -> None:
        self._workers.discard(worker)
        worker.deleteLater()

    def shutdown(self) -> None:
        self._stop_wake()
        try:
            self._tts.stop()
        except Exception:
            pass
        for worker in list(self._workers):
            worker.wait(2000)
