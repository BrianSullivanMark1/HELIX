"""Speech adapters — optional. Voice is purely additive; null fallbacks keep text-only fully working.

SpeechIn  : local, private STT via faster-whisper (audio never leaves the machine).
SpeechOut : the OS voice (Windows System.Speech / macOS `say`) — local, no extra dependency.
Both degrade to a null implementation when unavailable, so nothing here is ever a hard requirement.

The STT model is cached at MODULE level (not per-instance) so it can be pre-warmed before Qt starts:
on Windows, building faster-whisper's native ctranslate2 runtime AFTER QApplication has initialized
triggers a native access-violation crash. `prewarm()` is therefore called from main.py before any Qt
import; the container's WhisperSpeechIn then reuses the already-loaded model.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import platform
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

from helix.logging_setup import get_logger

_LOG = get_logger("speech")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_STT_MODEL = "small.en"   # accuracy-first default: far better on the wake word + short commands
                                 # than base.en, still CPU-friendly (~1s/utterance). Weights live in the
                                 # user's HF cache and are pre-warmed before Qt.
_FALLBACK_STT_MODEL = "base.en"  # if the preferred model can't load (offline / not yet downloaded), fall
                                 # back so hands-free voice still works instead of going dark.

# One model instance per size, shared across every WhisperSpeechIn. Heavy to build (and downloads
# weights on first use), so we keep it alive once loaded.
_MODELS: dict[str, object] = {}
_ACTIVE_MODEL: str | None = None  # the size prewarm actually loaded (preferred, or the fallback)


def stt_importable() -> bool:
    """True if faster-whisper is installed (does NOT load a model — that is deferred to prewarm)."""
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _build_model(model_size: str, device: str = "cpu"):
    from faster_whisper import WhisperModel
    # int8 on CPU is the lightest, broadly-compatible setting; first construction downloads weights.
    return WhisperModel(model_size, device=device, compute_type="int8")


def prewarm(model_size: str = DEFAULT_STT_MODEL, device: str = "cpu") -> bool:
    """Load + cache the STT model now and report whether it is ready. Never raises.

    MUST be called from the desktop entry point BEFORE constructing QApplication (see module docstring):
    building ctranslate2 after Qt is up crashes the process on Windows. Best-effort — returns False if
    faster-whisper isn't installed or the model can't be built, so the caller can keep voice disabled."""
    global _ACTIVE_MODEL
    if not stt_importable():
        return False
    # Try the preferred model, then the lighter fallback — so a machine that can't fetch/build small.en
    # still gets working voice on base.en rather than none. Record what actually loaded (active_model()).
    for size in (model_size, _FALLBACK_STT_MODEL):
        try:
            if size not in _MODELS:
                _MODELS[size] = _build_model(size, device)
            _ACTIVE_MODEL = size
            return True
        except Exception:
            continue
    return False


def active_model() -> str:
    """The STT model size prewarm actually loaded (the preferred one, or the fallback if that failed). The
    container builds WhisperSpeechIn with this, so transcription always uses what's really in memory."""
    return _ACTIVE_MODEL or DEFAULT_STT_MODEL


def stt_ready(model_size: str = DEFAULT_STT_MODEL) -> bool:
    """True if the model is already loaded in-process, so transcribe() will NOT construct it now."""
    return model_size in _MODELS


# ----- speech-in (STT) -----
class WhisperSpeechIn:
    """Local STT via faster-whisper, using the module-level (pre-warmed) model cache."""

    def __init__(
        self, model_size: str = DEFAULT_STT_MODEL, device: str = "cpu", wake_word: Callable[[], str] | None = None
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._wake_word = wake_word  # optional provider of the user's chosen wake word, biased in the decoder

    def available(self) -> bool:
        """True if faster-whisper is importable (the engine could be used)."""
        return stt_importable()

    def ready(self) -> bool:
        """True if the model is already loaded (pre-warmed), so transcribe won't build it after Qt."""
        return stt_ready(self._model_size)

    def transcribe(self, wav_path: Path) -> str:
        # A single failure returns "" and is forgotten — one bad clip must NOT disable voice for the
        # session (the controller re-checks availability on every settle to decide whether to re-arm).
        try:
            model = _MODELS.get(self._model_size)
            if model is None:
                model = _build_model(self._model_size, self._device)
                _MODELS[self._model_size] = model
            # beam_size=5 (beam search) is markedly more accurate than greedy on short spoken commands —
            # worth the ~1s. `hotwords` biases the decoder toward the wake word AND the control phrases
            # that MOST need to land ("HELIX", "go to sleep", "wake up", "stop"), so an uncommon word like
            # HELIX is mis-heard far less. condition_on_previous_text=False keeps each clip independent, so
            # a previous utterance never drags the next transcription off course.
            hotwords = "HELIX, hey HELIX, wake up, go to sleep, stop, never mind, goodbye"
            try:  # bias the decoder toward the user's chosen wake word too, so a custom name lands
                custom = (self._wake_word() or "").strip() if self._wake_word is not None else ""
                if custom and custom.lower() != "helix":
                    hotwords = f"{custom}, hey {custom}, {hotwords}"
            except Exception:  # noqa: BLE001 — a bad provider never breaks transcription
                pass
            segments, _info = model.transcribe(
                str(wav_path),
                language="en",
                beam_size=5,
                condition_on_previous_text=False,
                hotwords=hotwords,
            )
            return " ".join(seg.text for seg in segments).strip()
        except Exception as exc:
            _LOG.warning("transcription failed: %s", exc)
            return ""


# ----- speech-out (TTS) -----
class OsSpeechOut:
    """The built-in OS voice. Local, no network, no extra dependency. Text is piped via stdin.

    speak() BLOCKS until the utterance finishes, so a caller running it on a worker thread learns
    exactly when speech ends (the voice controller uses this to re-arm the mic only once HELIX has
    stopped talking, so it never transcribes its own reply). stop() — called from any thread — kills
    the process, which unblocks an in-flight speak() for instant barge-in.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()  # speak() runs on a worker; stop() on the UI thread

    def available(self) -> bool:
        return platform.system() in ("Windows", "Darwin")

    def speak(self, text: str, allow_fallback: bool = True) -> None:
        text = (text or "").strip()
        if not text:
            self.stop()
            return
        system = platform.system()
        proc: subprocess.Popen | None = None
        try:
            with self._lock:  # kill any prior utterance and publish the new handle atomically
                self._kill_locked()
                if system == "Windows":
                    script = (
                        # Read stdin as UTF-8 — without this the fallback voice mangles em-dashes and
                        # accented names (the text is piped in as UTF-8 below).
                        "[Console]::InputEncoding=[System.Text.Encoding]::UTF8;"
                        "Add-Type -AssemblyName System.Speech;"
                        "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                        ".Speak([Console]::In.ReadToEnd())"
                    )
                    proc = subprocess.Popen(
                        ["powershell", "-NoProfile", "-Command", script],
                        stdin=subprocess.PIPE, text=True, encoding="utf-8", creationflags=_NO_WINDOW,
                    )
                    self._proc = proc
                elif system == "Darwin":
                    proc = subprocess.Popen(["say", text])
                    self._proc = proc
            if proc is not None and system == "Windows" and proc.stdin is not None:
                proc.stdin.write(text)
                proc.stdin.close()
            if proc is not None:
                proc.wait()  # block until the utterance completes (or stop() kills it)
        except Exception as exc:
            _LOG.warning("TTS failed: %s", exc)

    def speak_chunks(self, chunks: list[str], allow_fallback: bool = True) -> None:
        """The OS voice synthesizes locally (no network), so a whole reply reads seamlessly as one
        utterance — no per-sentence gap. Just join and speak."""
        text = " ".join(c for c in (str(x).strip() for x in chunks) if c)
        if text:
            self.speak(text)

    def stop(self) -> None:
        with self._lock:
            self._kill_locked()

    def _kill_locked(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None


# ----- neural speech-out (edge-tts) -----
DEFAULT_TTS_VOICE = "en-GB-RyanNeural"  # British male — the J.A.R.V.I.S. default, as in HELIX v1

# Curated neural voices: (label, edge-tts id). British first.
TTS_VOICES: tuple[tuple[str, str], ...] = (
    ("British — Ryan (male)", "en-GB-RyanNeural"),
    ("British — Sonia (female)", "en-GB-SoniaNeural"),
    ("British — Thomas (male)", "en-GB-ThomasNeural"),
    ("US — Guy (male)", "en-US-GuyNeural"),
    ("US — Aria (female)", "en-US-AriaNeural"),
    ("US — Jenny (female)", "en-US-JennyNeural"),
    ("Australian — William (male)", "en-AU-WilliamNeural"),
    ("Australian — Natasha (female)", "en-AU-NatashaNeural"),
    ("Irish — Connor (male)", "en-IE-ConnorNeural"),
    ("Canadian — Liam (male)", "en-CA-LiamNeural"),
)


def edge_available() -> bool:
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return False


def _rate_string(multiplier: object) -> str:
    """A speed multiplier (1.0 = natural) → edge-tts rate, e.g. 1.5 → '+50%', 0.8 → '-20%'."""
    try:
        m = float(multiplier)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        m = 1.0
    pct = round((max(0.5, min(2.0, m)) - 1.0) * 100)
    return f"+{pct}%" if pct >= 0 else f"{pct}%"


# MPEG-audio frame tables — used to compute an MP3's exact play length in pure Python, so the warm
# player can block for precisely the audio duration instead of polling MediaPlayer.Position. In a
# pump-less console process Position never advances, so the old completion logic waited out its whole
# grace/timeout — multiple seconds of dead air after every spoken sentence.
_MP3_BR_V1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)  # MPEG-1 Layer III
_MP3_BR_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)       # MPEG-2/2.5 L3
_MP3_SR = {3: (44100, 48000, 32000, 0), 2: (22050, 24000, 16000, 0), 0: (11025, 12000, 8000, 0)}


def mp3_duration_ms(path: str) -> int:
    """Exact duration (ms) of an MP3, by summing its frame durations. Dependency-free and validated to
    match the media engine's own NaturalDuration to the millisecond. Returns 0 if it can't be parsed
    (a non-MP3 or truncated clip) — the player then falls back to detecting the length itself."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return 0
    i, n, total = 0, len(data), 0.0
    if data[:3] == b"ID3" and n >= 10:  # skip an ID3v2 tag if one is present (syncsafe size)
        i = 10 + ((data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9])
    while i + 4 <= n:
        if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:  # frame sync
            i += 1
            continue
        ver = (data[i + 1] >> 3) & 3
        if ((data[i + 1] >> 1) & 3) != 1:  # Layer III only (edge-tts is L3)
            i += 1
            continue
        bri = (data[i + 2] >> 4) & 0xF
        sri = (data[i + 2] >> 2) & 3
        pad = (data[i + 2] >> 1) & 1
        if sri == 3 or bri in (0, 15):  # reserved sample-rate / free-or-bad bitrate
            i += 1
            continue
        sr = _MP3_SR[ver][sri]
        if ver == 3:  # MPEG-1
            flen = (144000 * _MP3_BR_V1[bri]) // sr + pad
            spf = 1152
        else:         # MPEG-2 / 2.5 (edge-tts synthesizes 24 kHz → MPEG-2)
            flen = (72000 * _MP3_BR_V2[bri]) // sr + pad
            spf = 576
        if flen <= 0:
            i += 1
            continue
        total += spf / sr
        i += flen
    return int(total * 1000)


class _WarmMediaPlayer:
    """One persistent STA PowerShell MediaPlayer, fed MP3 paths over stdin.

    The old design spawned a fresh powershell per utterance — 0.5-1s of dead air before every spoken
    line, plus a blind 3-second sleep when the duration never resolved. Keeping one warm player makes
    consecutive lines start instantly. Each request is 'path|milliseconds': the caller computes the
    clip's EXACT length (mp3_duration_ms) and the player blocks for just that long — Position never
    advances in this pump-less process, so the previous Position/grace logic sat as multi-second dead
    air after every sentence. The player also Close()s the media so the caller can delete the temp
    file immediately (an un-closed handle made os.remove() block ~4s — the same dead air, relocated).
    stop() kills the process (instant hush); the next utterance warms a fresh one.
    """

    _TAIL_MS = 150  # a short tail after the exact audio length, covering Play-start latency so the
    #                 final syllable never clips even though we sleep a precise, computed duration.
    _SCRIPT = (
        "Add-Type -AssemblyName PresentationCore;"
        "$mp=New-Object System.Windows.Media.MediaPlayer;"
        "$mp.Volume=1.0;"
        "while($true){"
        "$line=[Console]::In.ReadLine();"
        "if($line -eq $null -or $line -eq ''){break};"
        # Split 'path|ms' on the LAST '|' (a Windows path can't contain one) → the exact sleep length.
        "$ix=$line.LastIndexOf('|');"
        "$path=$line.Substring(0,$ix);"
        "$ms=[int]$line.Substring($ix+1);"
        "$ok='DONE';"
        "try{"
        "$mp.Open([uri]$path);"
        # A brief bounded wait so the media is buffered before Play (otherwise a fixed sleep can start
        # before playback does and clip the first syllable). Returns as soon as the length is known.
        "for($i=0;$i -lt 40;$i++){if($mp.NaturalDuration.HasTimeSpan){break};Start-Sleep -Milliseconds 15};"
        "$mp.Play();"
        # Block for the caller's exact duration; only when it's unknown fall back to the engine's own.
        "if($ms -gt 0){Start-Sleep -Milliseconds $ms}"
        "elseif($mp.NaturalDuration.HasTimeSpan){Start-Sleep -Milliseconds ([int]$mp.NaturalDuration.TimeSpan.TotalMilliseconds + 250)}"
        "else{Start-Sleep -Seconds 3};"
        # Stop AND Close — Close frees the file handle at once so the caller's os.remove doesn't block.
        "$mp.Stop();"
        "$mp.Close()"
        "}catch{$ok='FAIL'};"
        "[Console]::Out.WriteLine($ok)"
        "}"
    )

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def _ensure_locked(self) -> subprocess.Popen:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-STA", "-Command", self._SCRIPT],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1, creationflags=_NO_WINDOW,
            )
        return self._proc

    def play(self, path: str, dur_ms: int = 0) -> bool:
        """Play one MP3, blocking for its known duration `dur_ms` (+ a short tail) rather than polling
        for completion. False = failed OR stopped (the caller's generation guard tells those apart — a
        killed player just makes readline return ''). dur_ms<=0 → the player detects the length itself."""
        sleep_ms = dur_ms + self._TAIL_MS if dur_ms > 0 else 0
        with self._lock:
            try:
                proc = self._ensure_locked()
                proc.stdin.write(f"{path}|{sleep_ms}\n")
                proc.stdin.flush()
            except Exception:
                self._kill_locked()
                return False
        try:
            line = proc.stdout.readline()  # blocks for the utterance's real duration
        except Exception:
            return False
        return line.strip() == "DONE"

    def stop(self) -> None:
        with self._lock:
            self._kill_locked()

    def _kill_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None


class EdgeSpeechOut:
    """Neural TTS via edge-tts (Microsoft's online voices), so HELIX can speak with a chosen accent.

    edge-tts synthesizes an MP3 from the reply text over the network; playback goes through one warm
    STA MediaPlayer process (no Qt event loop needed — speak() runs on a worker thread and returns
    when playback ends, so the mic re-arms only after HELIX finishes). If synthesis or playback
    fails — offline, no WMP — it falls back to the local OS voice, so a reply is always spoken.
    """

    def __init__(
        self,
        voice_provider: "Callable[[], str | None]",
        rate_provider: "Callable[[], object]",
        fallback: object | None = None,
    ) -> None:
        self._voice = voice_provider
        self._rate = rate_provider
        self._fallback = fallback if fallback is not None else OsSpeechOut()
        self._lock = threading.Lock()
        self._player = _WarmMediaPlayer()
        # Per-utterance generation guard: each speak() gets a fresh gen; stop() records the gen it
        # stopped. A KILLED playback is identified by gen (not a shared bool), so a CONCURRENT speak()
        # can't reset the flag and make the killed utterance fall back to the OS voice (two voices).
        self._gen = 0
        self._stopped_gen = 0

    def available(self) -> bool:
        return edge_available() or self._fallback.available()

    def _is_stopped(self, gen: int) -> bool:
        return self._stopped_gen >= gen

    def speak(self, text: str, allow_fallback: bool = True) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._gen += 1
            gen = self._gen
        path = None
        try:
            path = self._synthesize(text, gen)
            if self._is_stopped(gen):  # stopped during synthesis — don't start playing
                return
            self._play(path, gen)
        except Exception as exc:  # offline, WMP missing, etc.
            if self._is_stopped(gen):  # we were told to stop; a killed proc is NOT a failure — never fall back
                return
            if not allow_fallback:
                # Progress narration: stay in ONE voice. Skip this note rather than speak it in the OS
                # voice, which would make consecutive notes flip between the neural and desktop voices.
                _LOG.warning("neural TTS failed (%s); skipping this narration note", exc)
                return
            _LOG.warning("neural TTS failed (%s); falling back to the OS voice", exc)
            self._fallback.speak(text)
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def speak_chunks(self, chunks: list[str], allow_fallback: bool = True) -> None:
        """Speak a reply as several sentence chunks with NO audible gap between them: every chunk is
        synthesized CONCURRENTLY up front, then played in order. The first (short) chunk is ready fast
        (low first-word latency), and each later chunk is already rendered by the time we reach it — so
        the pause the naive 'synthesize-then-play each' had is gone. Preemption/stop still works via the
        per-utterance gen guard.

        Temp files are reaped AFTER the whole reply (not between sentences): on Windows os.remove of a
        just-played mp3 costs ~0.2s (handle/AV latency), which stacked into an audible gap between every
        sentence — so it must never sit on the playback path."""
        chunks = [c for c in (str(x).strip() for x in chunks) if c]
        if not chunks:
            return
        if len(chunks) == 1:
            self.speak(chunks[0], allow_fallback)
            return
        with self._lock:
            self._gen += 1
            gen = self._gen
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(chunks)),
                                                    thread_name_prefix="helix-tts")
        futures = [ex.submit(self._render_quiet, c, gen) for c in chunks]
        rendered: list[str] = []  # every temp mp3 produced — deleted in one pass off the playback loop
        try:
            for i, fut in enumerate(futures):
                if self._is_stopped(gen):
                    break
                try:
                    path = fut.result()
                except Exception:  # noqa: BLE001
                    path = None
                if path:
                    rendered.append(path)
                if self._is_stopped(gen):
                    break
                if path is None:  # this sentence failed to synthesize — speak just it in the OS voice
                    if allow_fallback and not self._is_stopped(gen):
                        self._fallback.speak(chunks[i])
                    continue
                try:
                    self._play(path, gen)
                except Exception:  # noqa: BLE001
                    if not self._is_stopped(gen) and allow_fallback:
                        self._fallback.speak(chunks[i])
        finally:
            # Gather any rendered-but-unplayed chunk (reachable on a stop), then delete them all off the
            # playback path so no os.remove ever sat in the gap between two spoken sentences.
            for f in futures:
                if f.done() and not f.cancelled():
                    try:
                        leftover = f.result(timeout=0)
                    except Exception:  # noqa: BLE001
                        leftover = None
                    if leftover and leftover not in rendered:
                        rendered.append(leftover)
            self._reap(rendered)
            ex.shutdown(wait=False)

    @staticmethod
    def _reap(paths: list[str]) -> None:
        """Delete temp mp3s on a background thread. os.remove of a just-played file can block ~0.2s on
        Windows even after the player closes its handle (AV / filesystem latency), so this must never
        run inline between two spoken sentences — hence one daemon thread, after playback."""
        cleanup = [p for p in paths if p]
        if not cleanup:
            return

        def _go() -> None:
            for p in cleanup:
                try:
                    os.remove(p)
                except OSError:
                    pass

        threading.Thread(target=_go, daemon=True, name="helix-tts-reap").start()

    def _render_quiet(self, text: str, gen: int) -> str | None:
        try:
            if self._is_stopped(gen):
                return None
            return self._synthesize(text, gen)
        except Exception:  # noqa: BLE001
            return None

    def _synthesize(self, text: str, gen: int | None = None) -> str:
        import edge_tts

        gen = self._gen if gen is None else gen
        voice = (self._voice() or "").strip() or DEFAULT_TTS_VOICE
        rate = _rate_string(self._rate())
        handle, path = tempfile.mkstemp(suffix=".mp3", prefix="helix_tts_")
        os.close(handle)

        async def _go() -> None:
            await edge_tts.Communicate(text, voice, rate=rate).save(path)

        # Retry transient network blips so a single failed request doesn't drop us to the OS voice — the
        # main cause of consecutive lines coming out in different voices during a build.
        last_exc: Exception | None = None
        for attempt in range(3):
            if self._is_stopped(gen):
                raise RuntimeError("stopped")
            try:
                asyncio.run(_go())
                if os.path.getsize(path) > 0:
                    return path
                last_exc = RuntimeError("edge-tts produced no audio")
            except Exception as exc:
                last_exc = exc
        raise last_exc or RuntimeError("edge-tts produced no audio")

    def _play(self, path: str, gen: int | None = None) -> None:
        gen = self._gen if gen is None else gen
        if platform.system() != "Windows":
            raise RuntimeError("MP3 playback here is Windows-only")
        if self._is_stopped(gen):
            return
        # Pass the clip's exact length so the player blocks for precisely that (deterministic; no
        # Position polling and no multi-second grace between sentences). The warm player blocks until
        # playback really ends. A failure propagates so speak() falls back to the OS voice — unless
        # this utterance was stopped on purpose (the kill makes play() return False).
        if not self._player.play(path, mp3_duration_ms(path)) and not self._is_stopped(gen):
            raise RuntimeError("playback failed")

    def stop(self) -> None:
        # Mark the current (and any earlier) utterance stopped, so its in-flight speak() treats the kill
        # as intentional — and a LATER speak() (higher gen) is unaffected, so it can't be un-stopped.
        with self._lock:
            self._stopped_gen = self._gen
        self._player.stop()
        self._fallback.stop()
