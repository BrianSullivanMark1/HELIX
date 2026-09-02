"""Voice grammar + audio math — the PURE core of HELIX's voice, shared by every shell.

Extracted verbatim from helix/ui/voice.py so the web backend's voice loop and the Qt shell run the
SAME brainstem: wake-word matching (with the curated HELIX mis-hearings), the sleep/wake/stop
whole-utterance grammars, the camera window's tiny grammar, echo detection, the say-as map and
sentence chunking for streamed TTS, VAD segmentation, and the PCM helpers. No Qt, no I/O beyond
writing a WAV the transcriber reads; unit-testable end to end (tests/test_voice.py pins it through
the ui re-exports).
"""
from __future__ import annotations

import array
import json
import math
import re
import wave

from helix.domain.brain import is_wake_utterance

VOICE_SETTING = "voice_input_on"  # hands-free mic on/off; persisted, default off
WAKE_WORD_SETTING = "wake_word"   # the spoken name that engages HELIX; "" / "HELIX" = the default name.
                                  # A household with a baby who says "HELIX/stop/goodbye" all day can pick
                                  # a baby-rare word (e.g. "Athena", "Friday") so the mic stops false-waking.
AUDIO_INPUT_SETTING = "audio_input_id"    # chosen mic (QAudioDevice id); "" = system default
STT_PREWARM_ERROR_SETTING = "stt_prewarm_error"  # WRITTEN by the launcher (main.STT_PREWARM_ERROR),
                                  # read here: why the speech model failed to load at launch, "" when
                                  # it loaded fine. The key is spelled out rather than imported
                                  # because main.py is the frozen build's entry SCRIPT — it is not an
                                  # importable module there — and a test pins the two spellings equal.
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

SESSION_IDLE_MS = 45 * 1000  # end the conversation session after this much inactivity. SHORT on purpose:
                             # inside a session the wake word isn't required, so a long window turns
                             # overheard family/TV speech into billed turns. 45s covers a natural
                             # back-and-forth without leaving the mic wide open for minutes.
PTT_MAX_MS = 20 * 1000           # hard cap on one push-to-talk capture (safety if 'released' is missed)
_RECENT_SPEAKER_S = 600          # how long a confidently-recognized speaker stays "still here" for short,
                                 # evidence-less follow-ups after a session lapses (10 min)

# Accept the obvious mis-hearings of "HELIX" so a clear command still lands.
_WAKE_RE = re.compile(
    r"\b(?:hey\s+|ok\s+|okay\s+)?"
    r"(?:he+l+ix|helics?|helicks|heli[ckx]s?|healix|healex|hel[eu]x|heelux|hilux)\b[\s,.:;!?-]*",
    re.IGNORECASE,
)
DEFAULT_WAKE_WORD = "HELIX"


def build_wake_re(word: str | None):
    """The wake-word matcher for the configured name. For the default HELIX we keep the curated
    fuzzy alternation (its many STT mis-hearings). For a custom word we build a simple boundary match
    with the optional "hey/ok/okay" carrier — so a household can pick a baby-rare word that a busy room
    isn't constantly saying."""
    w = (word or "").strip()
    if not w or w.lower() == "helix":
        return _WAKE_RE
    return re.compile(
        r"\b(?:hey\s+|ok\s+|okay\s+)?" + re.escape(w) + r"\b[\s,.:;!?-]*",
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


def split_wake(text: str, wake_re=None) -> tuple[bool, str]:
    """(matched, command): if the wake word is in `text`, return True + the words after it, else
    (False, ''). `wake_re` overrides the default HELIX matcher (the controller passes the configured
    word's regex)."""
    match = (wake_re or _WAKE_RE).search(text or "")
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


def is_echo(heard: str, spoken: str, wake_re=None) -> bool:
    """Is this transcript, overheard WHILE HELIX speaks, just its own voice coming back through the mic?

    The name is the primary discriminator: a reply that never says 'HELIX' cannot put the name in the
    mic — so a wake-word match against such a reply is always the user (this is what makes barge-in
    safe in a noisy room). Only when the reply itself contains the name do we fall back to content-word
    overlap: mostly-contained words = an echo; fresh words = a real command cutting in.

    The wake word is stripped from BOTH sides before scoring — it appears in every barge utterance (the
    user said it) AND, on this branch, in the reply, so counting it as overlap would be a guaranteed
    free hit that wrongly flags a genuine short command like 'HELIX, make it red' as an echo."""
    wake_re = wake_re or _WAKE_RE
    if not (spoken or "").strip():
        return False
    if not wake_re.search(spoken):
        return False  # the reply never says the name; hearing it means the user said it
    heard_nowake = wake_re.sub(" ", heard or "")
    spoken_nowake = wake_re.sub(" ", spoken)
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
_MD_SYMS = re.compile(r"[*`#~>|]+")               # markdown emphasis / headings / code / quotes / tables
_BULLET = re.compile(r"(?m)^\s*[-•·]\s+")         # list bullets at line start
_WS = re.compile(r"\s+")
# Underscores are NOT deleted — deleting glued a snake_case token into one unsayable word ("call_api" →
# "callapi" → the voice said "calawpee"). Turn each into a space so the parts are spoken as words.
_UNDERSCORE = re.compile(r"_+")
# Tiny say-as map: short tech tokens the neural voice would otherwise slur. Whole-word, case-insensitive;
# expanded to spaced letters so they're read as initialisms. Kept deliberately small and safe.
_SAY_AS = {
    "api": "A P I", "apis": "A P Is", "url": "U R L", "urls": "U R Ls",
    "sam.gov": "Sam dot gov", "sam gov": "Sam dot gov",
    "http": "H T T P", "https": "H T T P S", "json": "Jason", "pdf": "P D F",
}
_SAY_AS_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_SAY_AS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
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


# Split a reply into sentence-ish chunks so speech can START on the first sentence while the rest are
# still being synthesized — the first word lands much sooner on a multi-sentence reply. Splits after
# ., !, or ? (not after a common abbreviation like "Dr." / "e.g."), and merges tiny fragments so a lone
# "Yes." isn't its own choppy chunk.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
# Abbreviations (letters only, dots stripped) that end in a period but don't end a sentence.
_ABBREV = frozenset("mr mrs ms dr st vs etc no fig eg ie am pm us prof jr sr".split())
_MIN_CHUNK = 12  # a chunk shorter than this is a fragment — glue it to a neighbour, not its own utterance


def _last_word_letters(chunk: str) -> str:
    return re.sub(r"[^a-z]", "", chunk.rsplit(" ", 1)[-1].lower())


def split_sentences(text: str) -> list[str]:
    """`text` (already speakable) → sentence chunks for streamed playback. One chunk for a short reply
    (no behavior change); several for a long one, so the first plays while the rest synthesize. Doesn't
    split after an abbreviation ("Dr. Smith"), and glues tiny fragments to a neighbour so a lone "Yes."
    isn't its own choppy utterance."""
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    # Pass 1: undo splits right after an abbreviation.
    joined: list[str] = []
    for p in parts:
        if joined and _last_word_letters(joined[-1]) in _ABBREV:
            joined[-1] = f"{joined[-1]} {p}"
        else:
            joined.append(p)
    # Pass 2: absorb a too-short fragment into a neighbour (the previous chunk if it was itself tiny,
    # else merge this tiny one back).
    chunks: list[str] = []
    for p in joined:
        if chunks and (len(chunks[-1]) < _MIN_CHUNK or len(p) < _MIN_CHUNK):
            chunks[-1] = f"{chunks[-1]} {p}"
        else:
            chunks.append(p)
    return chunks or [text]


def speakable(text: str) -> str:
    """Strip viz blocks, markdown, and symbols so the voice never reads a table/chart or punctuation as
    words (e.g. '*' → 'asterisk'). A safety net behind the system prompt's plain-spoken instruction.
    Also un-glues snake_case (underscore → space) and expands a few tech initialisms, so a stray tool
    name like 'call_api' is spoken as "call A P I", never the mangled "calawpee"."""
    t = _VIZ_RE.sub("", text or "")  # never read a table/chart block aloud
    t = _MD_LINK.sub(r"\1", t)
    t = _BULLET.sub("", t)
    t = _MD_SYMS.sub("", t)
    t = _UNDERSCORE.sub(" ", t)      # snake_case → separate words (never a deleted underscore)
    t = _SAY_AS_RE.sub(lambda m: _SAY_AS[m.group(1).lower()], t)
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
# "sleep" rests the mic; an EXPLICIT wake brings it back. Whole-utterance matches only, so "build a
# sleep-timer app", "wake me at seven", or EXPLAINING the commands to someone ("the command word is
# sleep") never fire — mentioning a command is not giving it. Legacy mute/unmute phrasings stay
# accepted (old habits keep working), but the spoken + on-screen language is sleep/wake. A genuine
# sleep request embedded in longer natural speech is handled by the model instead (the go_to_sleep
# tool), which judges how the words were meant.
_SLEEP_FORMS = re.compile(
    r"^(?:(?:can|could|would)\s+you\s+)?"          # polite lead-in ("could you go to sleep")
    r"(?:"
    r"(?:go(?:ing)?\s+(?:to\s+|2\s+)?)?sleep(?:\s+(?:now|mode|please))?|goto\s+sleep|take\s+a\s+nap|"
    r"good\s*night|nighty?\s+night|bed\s*time|(?:it'?s\s+)?time\s+(?:to\s+sleep|for\s+bed)|"
    r"go\s+to\s+bed|(?:go\s+)?rest|"  # 'rest now' cleans to bare 'rest' (fillers strip 'now')
    r"mute(?:\s+(?:yourself|me|the\s+mic|my\s+mic|mic))?|stop\s+listening|pause\s+listening|"
    r"stop\s+the\s+mic|mic\s+off"
    r")"
    r"(?:\s+for\s+a\s+(?:bit|while|minute|moment))?$",  # "go to sleep for a bit"
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
# V3: sleep means SLEEP. There is deliberately no fuzzy wake hint any more — while asleep, only an
# EXPLICIT wake brings HELIX back: a whole-utterance wake phrase ("wake up", "mic on"), or its name
# LEADING a short address ("HELIX", "hey HELIX, wake up"). Its name merely mentioned mid-sentence
# ("...the wake word is HELIX") is someone talking ABOUT it, and it stays asleep.

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
    """True when the whole utterance asks HELIX to sleep (rest the mic), not stop a build. This is the
    built-in brainstem grammar; VoiceController._is_sleep_reflex also consults learned reflexes."""
    return bool(_SLEEP_FORMS.match(_clean_command(text)))


def is_wake(text: str) -> bool:
    """True when the whole utterance asks HELIX to wake (resume listening)."""
    return bool(_WAKE_FORMS.match(_clean_command(text)))


# --- the camera window's tiny voice grammar ("take picture" / "cancel") -----------------------
# Deliberately TIGHT, whole-utterance only: explicit picture words act, everything else is ignored,
# so room chatter near an open camera window can never snap a frame ("we should take it easy" and a
# sentence merely CONTAINING 'capture' do nothing). Fillers (okay/please/now/hey/name) are stripped
# by _clean_command, so "okay HELIX, take the picture now" lands as "take the picture".
_CAMERA_TAKE = frozenset({
    "take picture", "take a picture", "take the picture", "take my picture",
    "take photo", "take a photo", "take the photo",
    "take pic", "take a pic", "take the pic",
    "take the shot", "take a shot", "take one",
    "snap it", "snap the picture", "snap a picture", "snap the photo", "snap a photo",
    "capture", "capture it", "capture the picture", "capture the photo",
    "picture", "cheese",
})
_CAMERA_CANCEL = frozenset({
    "cancel", "cancel it", "cancel that", "never mind", "nevermind", "forget it",
    "close it", "close the camera", "no picture", "stop", "stop it",
})


def camera_command(text: str, wake_re=None) -> str | None:
    """Classify an utterance heard while the CAMERA WINDOW is open: 'capture', 'cancel', or None
    (ignored — the room keeps talking, the window keeps waiting for its button or its words). The
    wake word is optional here — the open window IS the addressing context — and the name may sit
    ANYWHERE ("HELIX, take the picture" / "take the picture, HELIX"): the whole utterance minus the
    name must BE a grammar phrase. Deliberately not split_wake: keeping only the text AFTER the
    name would drop trailing-name commands and let a long mention-sentence ("I told HELIX take the
    picture yesterday") false-fire on its tail."""
    raw = text or ""
    stripped = (wake_re or _WAKE_RE).sub(" ", raw, count=1)  # the name, wherever it sits
    for candidate in (raw, stripped):  # raw covers the default name (a _clean_command filler)
        t = _clean_command(candidate)
        if t in _CAMERA_TAKE:
            return "capture"
        if t in _CAMERA_CANCEL:
            return "cancel"
    return None


def _wants_wake(text: str, wake_re=None) -> bool:
    """Wake test used while asleep — the THALAMIC GATE (domain/brain.py). Sleep means sleep: HELIX
    wakes only for an explicit wake PHRASE ("wake up", "mic on") or an utterance genuinely ADDRESSED
    to it — the name leading the utterance, even inside a natural greeting ("good morning HELIX, how
    you doing"), or a short utterance that IS the address. The name merely MENTIONED mid-sentence
    ("the wake word is HELIX", explaining the commands to a friend) is speech about HELIX, not to it,
    and leaves it asleep."""
    return is_wake_utterance(text, wake_re or _WAKE_RE, is_wake)


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
