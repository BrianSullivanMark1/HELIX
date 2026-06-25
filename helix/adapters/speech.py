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

DEFAULT_STT_MODEL = "base.en"  # good CPU default: fast, English-only, decent on short spoken commands

# One model instance per size, shared across every WhisperSpeechIn. Heavy to build (and downloads
# weights on first use), so we keep it alive once loaded.
_MODELS: dict[str, object] = {}


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
    if not stt_importable():
        return False
    try:
        if model_size not in _MODELS:
            _MODELS[model_size] = _build_model(model_size, device)
        return True
    except Exception:
        return False


def stt_ready(model_size: str = DEFAULT_STT_MODEL) -> bool:
    """True if the model is already loaded in-process, so transcribe() will NOT construct it now."""
    return model_size in _MODELS


# ----- speech-in (STT) -----
class WhisperSpeechIn:
    """Local STT via faster-whisper, using the module-level (pre-warmed) model cache."""

    def __init__(self, model_size: str = DEFAULT_STT_MODEL, device: str = "cpu") -> None:
        self._model_size = model_size
        self._device = device

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
            # beam_size=1 (greedy) is fastest and plenty for short spoken commands.
            segments, _info = model.transcribe(str(wav_path), language="en", beam_size=1)
            return " ".join(seg.text for seg in segments).strip()
        except Exception as exc:
            _LOG.warning("transcription failed: %s", exc)
            return ""


class NullSpeechIn:
    def available(self) -> bool:
        return False

    def ready(self) -> bool:
        return False

    def transcribe(self, wav_path: Path) -> str:
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

    def speak(self, text: str) -> None:
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


class EdgeSpeechOut:
    """Neural TTS via edge-tts (Microsoft's online voices), so HELIX can speak with a chosen accent.

    edge-tts synthesizes an MP3 from the reply text over the network; we play it blocking through the
    Windows Media Player COM object (no Qt event loop needed — speak() runs on a worker thread and
    returns when playback ends, so the mic re-arms only after HELIX finishes). If synthesis or playback
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
        self._proc: subprocess.Popen | None = None
        self._stopped = False  # set by stop()/close so a KILLED playback isn't mistaken for a failure

    def available(self) -> bool:
        return edge_available() or self._fallback.available()

    def speak(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._stopped = False
        path = None
        try:
            path = self._synthesize(text)
            if self._stopped:  # stopped during synthesis — don't start playing
                return
            self._play(path)
        except Exception as exc:  # offline, WMP missing, etc. — still speak, via the OS voice
            if self._stopped:  # we were told to stop; a killed proc is NOT a failure — never fall back
                return
            _LOG.warning("neural TTS failed (%s); falling back to the OS voice", exc)
            self._fallback.speak(text)
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _synthesize(self, text: str) -> str:
        import edge_tts

        voice = (self._voice() or "").strip() or DEFAULT_TTS_VOICE
        rate = _rate_string(self._rate())
        handle, path = tempfile.mkstemp(suffix=".mp3", prefix="helix_tts_")
        os.close(handle)

        async def _go() -> None:
            await edge_tts.Communicate(text, voice, rate=rate).save(path)

        asyncio.run(_go())
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise RuntimeError("edge-tts produced no audio")
        return path

    def _play(self, path: str) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("MP3 playback here is Windows-only")
        if self._stopped:
            return
        # Play the MP3 via WPF's MediaPlayer (Media Foundation; present on every Windows) and block for
        # its natural duration. PowerShell runs STA, which MediaPlayer requires. A non-zero exit (e.g.
        # the file won't open) propagates so speak() falls back to the OS voice.
        script = (
            "Add-Type -AssemblyName PresentationCore;"
            "$mp=New-Object System.Windows.Media.MediaPlayer;"
            f"$mp.Open([uri]'{path}');"
            "$mp.Volume=1.0;"
            "Start-Sleep -Milliseconds 300;"
            "$mp.Play();"
            "$d=$null;"
            "for($i=0;$i -lt 50;$i++){if($mp.NaturalDuration.HasTimeSpan){$d=$mp.NaturalDuration.TimeSpan;break};Start-Sleep -Milliseconds 100};"
            "if($d){Start-Sleep -Milliseconds ([int]$d.TotalMilliseconds + 250)}else{Start-Sleep -Seconds 3};"
            "$mp.Stop();$mp.Close();"
        )
        with self._lock:
            self._proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-STA", "-Command", script],
                creationflags=_NO_WINDOW,
            )
            proc = self._proc
        proc.wait()
        with self._lock:
            self._proc = None
        if not self._stopped and proc.returncode not in (0, None):
            raise RuntimeError(f"playback exited {proc.returncode}")

    def stop(self) -> None:
        self._stopped = True  # so the in-flight speak() treats the kill as intentional, not a failure
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        self._fallback.stop()


class NullSpeechOut:
    def available(self) -> bool:
        return False

    def speak(self, text: str) -> None:
        pass

    def stop(self) -> None:
        pass
