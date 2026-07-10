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
import threading
import wave
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from helix.logging_setup import get_logger
from helix.ports.speech import SpeechIn, SpeechOut
from helix.ports.stores import SettingsStore
from helix.services import voiceid
from helix.ui.orb import OrbState
from helix.ui.workers import QtWorker

try:  # QtMultimedia ships with PyQt6 but needs platform plugins; degrade if it can't load.
    from PyQt6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices

    _MULTIMEDIA = True
except Exception:  # pragma: no cover - depends on the host's Qt plugins
    _MULTIMEDIA = False

_LOG = get_logger("voice")

VOICE_SETTING = "voice_input_on"  # hands-free mic on/off; persisted, default off
AUDIO_INPUT_SETTING = "audio_input_id"    # chosen mic (QAudioDevice id); "" = system default
AUDIO_OUTPUT_SETTING = "audio_output_id"  # chosen speaker, for the Settings 'Test' button only — HELIX's
                                          # own voice always follows the Windows DEFAULT output device

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


# Grammar words carry no evidence about WHO is speaking — the echo test scores content words only.
_ECHO_STOPWORDS = frozenset(
    "a an and are be but can could did do for i in is it its just of on or so that the this to was "
    "we what will with you your".split()
)


def _content_words(text: str) -> list[str]:
    words = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split()
    return [w for w in words if w not in _ECHO_STOPWORDS]


def is_echo(heard: str, spoken: str) -> bool:
    """Is this transcript, overheard WHILE HELIX speaks, just its own voice coming back through the mic?

    The name is the primary discriminator: a reply that never says 'HELIX' cannot put the name in the
    mic — so a wake-word match against such a reply is always the user (this is what makes barge-in
    safe in a noisy room). Only when the reply itself contains the name do we fall back to content-word
    overlap: mostly-contained words = an echo; fresh words = a real command cutting in.

    The wake word is stripped from BOTH sides before scoring — it appears in every barge utterance (the
    user said it) AND, on this branch, in the reply, so counting it as overlap would be a guaranteed
    free hit that wrongly flags a genuine short command like 'HELIX, make it red' as an echo."""
    if not (spoken or "").strip():
        return False
    if not _WAKE_RE.search(spoken):
        return False  # the reply never says the name; hearing it means the user said it
    heard_nowake = _WAKE_RE.sub(" ", heard or "")
    spoken_nowake = _WAKE_RE.sub(" ", spoken)
    content = _content_words(heard_nowake)
    if not content:
        return True  # nothing beyond the name and filler, and the reply itself says the name
    spoken_words = set(_content_words(spoken_nowake))
    hits = sum(1 for w in content if w in spoken_words)
    return hits / len(content) >= 0.6


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
# SLEEP / WAKE — pause/resume the user's mic WITHOUT stopping a build (the opposite of a stop command).
# "sleep" rests the mic; "wake" — or just saying the wake word "HELIX" — brings it back. Whole-utterance
# matches only, so "build a sleep-timer app" or "wake me at seven" don't fire. Legacy mute/unmute
# phrasings stay accepted (old habits keep working), but the spoken + on-screen language is sleep/wake.
_SLEEP_FORMS = re.compile(
    r"^(?:"
    r"(?:go(?:ing)?\s+(?:to\s+|2\s+)?)?sleep(?:\s+(?:now|mode|please))?|goto\s+sleep|take\s+a\s+nap|"
    r"mute(?:\s+(?:yourself|me|the\s+mic|my\s+mic|mic))?|stop\s+listening|pause\s+listening|"
    r"stop\s+the\s+mic|mic\s+off"
    r")$",
    re.IGNORECASE,
)
_WAKE_FORMS = re.compile(
    r"^(?:"
    r"wake(?:\s*up)?(?:\s+now|\s+please)?|wakey(?:\s+wakey)?|(?:are\s+you\s+|you\s+)?awake|"
    r"un\s*mute(?:d)?(?:\s+(?:yourself|me|the\s+mic|mic))?|start\s+listening(?:\s+again)?|"
    r"resume\s+listening|listen(?:ing)?\s+again|mic\s+on|you\s+can\s+listen(?:\s+again)?"
    r")$",
    re.IGNORECASE,
)
# Permissive wake hint — used ONLY while asleep, where the only possible outcomes are wake or stop, so a
# greedy match is free: any whiff of wake/listen brings HELIX back rather than stranding you muted.
_WAKE_HINT = re.compile(r"\b(?:wake|awake|wakey|woke|listen(?:ing)?|un\s*mute[d]?|mic\s*on)\b", re.IGNORECASE)

# What HELIX SAYS OUT LOUD when it sleeps / wakes, so you know the command landed even away from the
# screen. The sleep line deliberately carries no wake trigger words ("HELIX"/"wake"/"listen") — the open
# mic would otherwise hear the confirmation and instantly wake itself.
_SLEEP_CONFIRM = "Going to sleep."
_WAKE_CONFIRM = "Awake and listening."


def _clean_command(text: str) -> str:
    # Hyphens/underscores become spaces too, so a mis-transcribed "un-mute" / "wake-up" still matches.
    t = re.sub(r"[.!,?\-_]+", " ", (text or "").lower())
    t = _STOP_FILLERS.sub(" ", t)
    return " ".join(t.split())


def is_stop(text: str) -> bool:
    """True only when the WHOLE short utterance is a stop/cancel/hush command (fillers ignored)."""
    return bool(_STOP_FORMS.match(_clean_command(text)))


def is_sleep(text: str) -> bool:
    """True when the whole utterance asks HELIX to sleep (rest the mic), not stop a build."""
    return bool(_SLEEP_FORMS.match(_clean_command(text)))


def is_wake(text: str) -> bool:
    """True when the whole utterance asks HELIX to wake (resume listening)."""
    return bool(_WAKE_FORMS.match(_clean_command(text)))


def _wants_wake(text: str) -> bool:
    """Permissive wake test used ONLY while asleep, where the only outcomes are wake or stop — so a greedy
    match is free. True if HELIX is named (the wake word wakes it — 'sleep until you hear your name'), the
    utterance is a wake phrase, or a SHORT utterance carries a wake hint. Gating the fuzzy hint to short
    utterances keeps ambient chatter that merely contains 'listen'/'wake' ('did you listen to the game')
    from waking it, while 'wake' / 'wake up now' / 'mic on' still do."""
    if split_wake(text)[0]:
        return True
    if is_wake(text):
        return True
    cleaned = _clean_command(text)
    return len(cleaned.split()) <= 3 and bool(_WAKE_HINT.search(cleaned))


def _normalize16(pcm: bytes, target_peak: float = 0.9, max_gain: float = 8.0) -> bytes:
    """Boost a quiet utterance toward full scale before transcription — soft speech or a distant/quiet mic
    (earphones across the room) transcribes far better when it isn't near-silent. Peak-normalizes with a
    CAPPED gain, so real speech is lifted without a near-silent clip being blown up into loud hiss. No-op
    if the clip is already loud, empty, or numpy is unavailable."""
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return pcm
    try:
        import numpy as np

        x = np.frombuffer(pcm[:usable], dtype="<i2")
        if x.size == 0:
            return pcm
        peak = int(np.abs(x).max())
        if peak <= 0:
            return pcm
        gain = min(max_gain, target_peak * 32767.0 / peak)
        if gain <= 1.05:  # already close enough to full scale — leave it be
            return pcm
        y = np.clip(np.rint(x.astype(np.float32) * gain), -32768, 32767).astype("<i2")
        return y.tobytes()
    except Exception:
        return pcm


def _write_wav16(data: bytes, path: str) -> None:
    data = _normalize16(data)  # lift quiet speech so the transcriber hears it clearly
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


def device_id_str(device) -> str:
    """A stable, round-trippable string id for a QAudioDevice — latin-1 preserves every raw byte, so the
    saved id matches exactly on the next run. '' for a null/absent device."""
    try:
        return bytes(device.id()).decode("latin-1")
    except Exception:
        return ""


def _resolve_input_device(settings: "SettingsStore | None"):
    """The QAudioDevice for the user's chosen mic — or the system default, which is also the fallback when
    the saved device isn't currently present (e.g. earphones that are unplugged, so voice keeps working
    on the built-in mic). None if QtMultimedia isn't available."""
    if not _MULTIMEDIA:
        return None
    want = (settings.get(AUDIO_INPUT_SETTING, "") if settings is not None else "") or ""
    if want:
        for dev in QMediaDevices.audioInputs():
            if device_id_str(dev) == want:
                return dev
    return QMediaDevices.defaultAudioInput()


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

    def __init__(self, parent=None, settings: "SettingsStore | None" = None) -> None:
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
            device = _resolve_input_device(settings)
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

    def __init__(self, parent=None, settings: "SettingsStore | None" = None) -> None:
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
            device = _resolve_input_device(settings)
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
    mutedChanged = pyqtSignal(bool)    # the mic slept/woke — the Console updates its control
    identityLine = pyqtSignal(str, str)  # (what was heard, HELIX's spoken line) from the voice-identity
                                         # gate / calibration chat — shown in the transcript, never a turn

    def __init__(
        self,
        speech_in: SpeechIn,
        speech_out: SpeechOut,
        settings: SettingsStore,
        parent: QObject | None = None,
        voice_id: "voiceid.VoiceIdService | None" = None,
    ) -> None:
        super().__init__(parent)
        self._stt = speech_in
        self._tts = speech_out
        self._settings = settings
        # Voice identity: who is speaking. Optional — without the service the gate is open and
        # behavior is exactly the single-user HELIX of before.
        self._voice_id = voice_id
        self._flow = voiceid.EnrollmentFlow(voice_id) if voice_id is not None else None
        self._pending_emb = None           # the current utterance's voice-print (set on the worker)
        self.current_speaker: str | None = None   # who spoke the command recognized() is emitting
        self._session_speaker: str | None = None  # who opened the live session (sticky for short follow-ups)
        self._workers: set[QtWorker] = set()
        self._listener: WakeWordListener | None = None
        self._recorder: MicRecorder | None = None
        self._state = "idle"
        self._session = False
        self._ptt = False
        self._barge_busy = False          # one in-flight barge transcription at a time while speaking
        self._narrating = False           # a progress note is being spoken (skip new ones until it ends)
        self._muted = False               # user paused the mic: ignore all speech except unmute/stop
        self._speaking_text = ""          # what TTS is saying right now — the echo check compares to it
        self._speak_gen = 0               # a preempted utterance must not knock a newer turn to idle
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
            self._mic_ok = _MULTIMEDIA and WakeWordListener(settings=self._settings).is_available()
        return self._mic_ok

    def reload_audio_input(self) -> None:
        """Re-open the mic on the currently-selected input device (Settings just changed it) so a new mic
        takes effect without a restart. The device is read fresh on every (re)arm, so a later re-arm also
        picks it up; this just makes the switch immediate when hands-free is already listening."""
        self._mic_ok = None  # re-probe: the chosen device may differ in availability
        if self._listener is None:
            return
        try:
            self._stop_wake()
            self._start_wake()
        except Exception:
            pass

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
        self._listener = WakeWordListener(self, self._settings)
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
        self._session_speaker = None
        # A calibration chat abandoned mid-way (the user walked off) lapses with the session, so the
        # flow can never keep swallowing speech forever.
        if self._flow is not None and self._flow.active:
            self._flow.cancel()

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

    # ----- sleep / wake (rest the mic without stopping a build) -----
    def is_muted(self) -> bool:
        return self._muted

    def set_muted(self, on: bool, announce: bool = True) -> None:
        """Put the mic to SLEEP / WAKE it. Sleeping does NOT end the session or stop a build — it only
        changes what HELIX acts on; the listener stays live (when enabled) so 'wake', the wake word
        'HELIX', or a 'stop' still work by voice. Sleep is refused when nothing is actually listening (so
        we never advertise a slept mic that wasn't live, leaving an escape-less state); WAKE is always
        honored, so recovery is never blocked. When `announce`, HELIX speaks a one-line confirmation so
        you know the state changed even away from the screen."""
        on = bool(on)
        if on and not self.can_listen():
            return
        if on == self._muted:
            return
        self._muted = on
        if on:  # going quiet — drop the narration flag so a slept mic never speaks progress notes
            self._narrating = False
        self.mutedChanged.emit(on)
        if announce and self.enabled() and self._tts.available():
            # speak() preempts any in-flight narration AND drives state speaking -> idle (re-arming the
            # listener), so 'wake' / the wake word can still be heard right after the confirmation.
            self.speak(_SLEEP_CONFIRM if on else _WAKE_CONFIRM)
        else:
            if on:  # no spoken confirmation — hush any in-flight narration so sleep still feels immediate
                try:
                    self._tts.stop()
                except Exception:
                    pass
            self._set_state("idle")

    def toggle_muted(self) -> None:
        self.set_muted(not self._muted)

    def _on_muted_text(self, text: str) -> None:
        # Asleep: the ONLY outcomes are WAKE or STOP, so wake-matching is greedy (a false positive is
        # free) — any whiff of 'wake'/'listen'/the wake word brings HELIX back instead of stranding you.
        # Everything else is dropped: a slept mic never starts a turn or a build from your speech.
        self._barge_busy = False
        t = (text or "").strip()
        if t and _wants_wake(t):
            self.set_muted(False)  # speaks the wake confirmation and re-arms the listener (owns state)
            return
        if t and is_stop(t):
            self._hush()
            self.stopRequested.emit()
        self._set_state("idle")  # dropped or stopped — re-arm the wake listener

    def _on_utterance(self, pcm: bytes) -> None:
        # Asleep: route to the sleep handler — only a wake/stop phrase acts; all other speech is dropped.
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
            self._transcribe(path, self._on_barge_text, pcm)
            return
        if self._state != "idle":  # transcribing / thinking — ignore
            return
        path = self._pcm_to_wav(pcm)
        if path is None:
            return
        self._set_state("transcribing")
        self._transcribe(path, self._on_wake_text, pcm)

    def _hush(self) -> None:
        """Silence any in-flight speech/narration immediately."""
        try:
            self._tts.stop()
        except Exception:
            pass
        self._narrating = False

    # ----- voice identity (who is speaking) -----
    def _take_emb(self):
        """The current utterance's voice-print, computed on the transcription worker. Read-once."""
        emb, self._pending_emb = self._pending_emb, None
        return emb

    def _say_identity(self, heard: str, reply: str) -> None:
        """Speak (and surface in the transcript) a line from the identity gate / calibration chat.
        These lines never touch the conversation store — a stranger's words are not history."""
        self.identityLine.emit(heard, reply)
        self.speak(reply)

    def _after_flow(self, heard: str, reply: str | None) -> None:
        """Deliver a calibration-flow line; on completion, adopt the new speaker and distill their
        identity notes in the background."""
        flow = self._flow
        if reply:
            self._start_session()  # keep the calibration chat alive across its questions
            self._say_identity(heard, reply)
        else:
            self._set_state("idle")
        name = getattr(flow, "last_registered", None)
        if name and not flow.active:
            flow.last_registered = None
            self.current_speaker = name
            self._session_speaker = name
            if self._voice_id is not None:
                self._voice_id.distill_notes(name, getattr(flow, "last_answers", []))

    def _flow_intercept(self, text: str) -> bool:
        """While a registration/recalibration chat is open, route speech to it instead of the model.
        Returns True when the utterance was consumed."""
        flow = self._flow
        if flow is None or not flow.active or not text:
            return False
        if is_stop(text) or is_dismissal(text):
            flow.cancel()
            self.interrupt()
            return True
        matched, after = split_wake(text)
        # PEEK at the voice-print — only consume it if the flow accepts the utterance. On a lapse the
        # caller re-gates this same utterance, and destroying its print here would blind the gate
        # (identify(None) = no-evidence, which a live session would then mis-attribute).
        reply = flow.handle(after if matched and after else text, self._pending_emb)
        if reply is None:
            return False  # not for the flow (it lapsed) — the caller re-gates the utterance
        self._pending_emb = None
        self._after_flow(text, reply)
        return True

    def _gate(self, command: str) -> bool:
        """The voice-identity gate, run just before a captured command becomes a model turn.
        True → proceed (current_speaker is set); False → the gate consumed the utterance (it spoke
        the registration offer, started calibration, etc.) and the command must NOT run."""
        svc, flow = self._voice_id, self._flow
        self.current_speaker = None
        if svc is None:
            return True
        emb = self._take_emb()
        has = svc.has_profiles()
        res = svc.identify(emb)
        name = res.name
        intro = voiceid.introduction_name(command)
        if voiceid.wants_recalibration(command):
            if name:
                self._say_identity(command, flow.start(name, emb, recal=True))
            elif not has:
                self._say_identity(command, flow.ask_name())
            else:
                self._say_identity(command, flow.offer())  # a stranger can't refresh anyone's profile
            self._start_session()
            return False
        if intro:
            if name and res.confident and intro.lower() == name.lower():
                self._session_speaker = name
                self._start_session()
                self._say_identity(command, f"I know your voice, {name}. What can I do for you?")
                return False
            if name and res.confident:
                # A confidently-matched voice claiming a different name never re-enrolls under it —
                # that would graft this voice onto a second identity.
                self._say_identity(
                    command,
                    f"You sound like {name} to me. If someone new wants to register, "
                    "they should say it themselves — I am, then their name.",
                )
                return False
            self._start_session()
            self._say_identity(command, flow.start(intro, emb))
            return False
        if voiceid.wants_registration(command):
            if name:
                self._say_identity(
                    command, f"Your voice is already registered, {name}. "
                    "Say: recalibrate my voice, to refresh it.")
            else:
                self._start_session()
                self._say_identity(command, flow.ask_name())
            return False
        if not has:
            return True  # nobody registered yet — single-user trust, exactly as before
        if name:
            self.current_speaker = name
            self._session_speaker = name
            if res.confident:  # quietly sharpen the profile with this utterance
                threading.Thread(
                    target=svc.add_passive, args=(name, emb), daemon=True, name="helix-voiceid"
                ).start()
            return True
        if res.no_evidence and self._session and self._session_speaker:
            # Too short to judge, but it's a follow-up inside a session a recognized speaker opened.
            self.current_speaker = self._session_speaker
            return True
        self._say_identity(command, flow.offer())  # the ONE reply an unrecognized voice gets
        return False

    def _on_barge_text(self, text: str) -> None:
        # While HELIX is busy (speaking a reply OR building), two things cut through: a SHORT stop
        # phrase, and the NAME — "HELIX, actually make it blue" lands mid-sentence. Everything else
        # (family chatter, the TV, HELIX's own reply leaking into the mic) is ignored, so a noisy
        # house can't hijack a turn — only the wake word interrupts.
        self._barge_busy = False
        t = (text or "").strip()
        if not t:
            return
        if len(t.split()) <= 4 and is_stop(t):
            self._hush()
            # Don't force idle here: the Console cancels the running turn/build and drives the orb state
            # when the worker actually unwinds (stopping TTS ends a speaking turn on its own).
            self.stopRequested.emit()
            return
        matched, after = split_wake(t)
        if not matched or is_echo(t, self._speaking_text):
            return  # not addressed to HELIX — or HELIX hearing itself say its own name
        command = after.strip()
        if self._state == "thinking" and not self._narrating:
            # A model turn already in flight can't be redirected — only a stop phrase acts (above).
            return
        if self._flow is not None and self._flow.active:
            # Mid-calibration barge (answering over a spoken question): the open flow owns it — never
            # let it fall into the gate, which would clobber the flow state or start a turn mid-chat.
            self._hush()
            if self._flow_intercept(t):
                return
        self._hush()  # the name cuts the voice off mid-sentence
        if is_stop(command):
            self.stopRequested.emit()
            self._set_state("idle")
            return
        if is_sleep(command):  # 'sleep' rests the mic; set_muted speaks the confirmation and re-arms
            self.set_muted(True)
            return
        if not command:
            self._start_session()
            self._say("Yes?")  # called by name mid-speech — acknowledge and listen
            return
        if not self._gate(command):
            return  # gate consumed it — and no session opens for a refused voice
        self._start_session()
        self._set_state("thinking")
        self.recognized.emit(command)

    def _on_wake_text(self, text: str) -> None:
        text = (text or "").strip()
        if self._muted:  # a sleep landed WHILE this transcribed — the sleep handler owns wake/stop + state
            self._on_muted_text(text)
            return
        if self._flow_intercept(text):  # an open registration/recalibration chat owns the mic
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
        if is_sleep(command):  # "sleep / stop listening" — rest the mic; never a turn, never a build-stop
            self.set_muted(True)  # set_muted speaks the confirmation and re-arms the listener
            return
        if is_wake(command):  # already awake — consume it so 'wake' never becomes a model turn
            self._set_state("idle")
            return
        if is_stop(command):  # "stop / be quiet / never mind" — hush and keep listening, no new turn
            self.interrupt()
            self.stopRequested.emit()
            return
        if not command:
            self._start_session()
            self._say("Yes?")  # bare "HELIX" — acknowledge and wait for the command
            return
        if not self._gate(command):
            # The gate consumed it — and CRUCIALLY no session opens for a refused voice. A session
            # waives the wake-word requirement for the whole room, so opening one on a stranger's
            # utterance would turn every later overheard sentence into a spoken refusal loop.
            return
        self._start_session()
        self._set_state("thinking")
        self.recognized.emit(command)

    # ----- push-to-talk (manual capture; works whenever voice is ready) -----
    def ptt_start(self) -> bool:
        if self._state != "idle" or not self.can_listen():
            return False
        self._stop_wake()  # release the device so the recorder can take it
        self._recorder = MicRecorder(self, self._settings)
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
        # pcm rides along so push-to-talk gets a voice-print like every other path — without it the
        # gate would judge PTT on no evidence and refuse the machine's own registered owner.
        self._transcribe(path, self._on_ptt_text, data)

    def _on_ptt_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            self._set_state("idle")
            return
        if self._muted:  # asleep (incl. a sleep that landed mid-capture): only wake/stop act, never a turn
            self._on_muted_text(text)
            return
        if self._flow_intercept(text):  # an open registration/recalibration chat owns the mic
            return
        if is_sleep(text):  # push-to-talk "sleep" rests the mic instead of starting a turn
            self.set_muted(True)
            return
        if is_wake(text):  # already awake — don't send 'wake' to the model
            self._set_state("idle")
            return
        if not self._gate(text):
            return  # gate consumed it — and no session opens for a refused voice
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
        self._speaking_text = text  # the echo check compares overheard speech to this
        self._set_state("speaking")
        self._speak_gen += 1
        gen = self._speak_gen
        self._run(lambda _emit: self._tts.speak(text), lambda *_: self._speak_done(gen))

    def _speak_done(self, gen: int) -> None:
        """Settle to idle when an utterance finishes — unless it was preempted (a newer speak took
        over) or a barge-in already moved the turn on (state left 'speaking'). Without this guard the
        killed utterance's completion used to reset WHATEVER state came after it back to idle."""
        if gen == self._speak_gen and self._state == "speaking":
            self._set_state("idle")

    def narrate(self, text: str) -> None:
        """Speak a short progress note as HELIX works, WITHOUT changing the turn state (the mic stays
        gated, the orb keeps 'thinking'). Skips while a previous note is still speaking, so notes pace
        themselves to speech and never stack up — turning a stream of steps into spoken milestones."""
        if self._narrating or self._muted or not self.enabled():
            return  # muted means quiet: don't speak progress notes (HELIX would also hear itself)
        if self._state == "speaking":
            # A real reply is audibly playing right now. Overlaying a progress note here would (a) queue
            # its audio behind the reply on the one warm player and (b) overwrite _speaking_text, so the
            # echo shield would compare an overheard reply against the NOTE — and, since the note lacks
            # the wake word, wrongly treat HELIX's own reply as a user command. Skip the note instead.
            return
        text = speakable(text)
        if not text or not self._tts.available():
            return
        self._speaking_text = text  # narration echoes are filtered the same way as reply echoes
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
    def _transcribe(self, path: str, on_text: Callable[[str], None], pcm: bytes | None = None) -> None:
        def work(_emit: Callable[[str], None]):
            emb = None
            try:
                if pcm is not None and self._voice_id is not None:
                    try:
                        emb = voiceid.embed_pcm(pcm)
                    except Exception:
                        emb = None
                return (self._stt.transcribe(Path(path)), emb)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass

        def done(result) -> None:
            # The (text, voice-print) pair travels TOGETHER through the worker result, and the print
            # is published on the UI thread immediately before its own handler runs. Two overlapping
            # transcriptions (a barge racing a fresh wake capture) therefore can never cross-pair a
            # command with the other speaker's voice — a shared slot written on the worker thread did.
            text, emb = result if isinstance(result, tuple) else (result, None)
            self._pending_emb = emb
            on_text(text)

        self._run(work, done)

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
