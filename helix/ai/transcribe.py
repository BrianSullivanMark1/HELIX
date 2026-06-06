from __future__ import annotations

import os

# Local, private speech-to-text via faster-whisper (CTranslate2 Whisper). Mirrors speech.py:
# an OPTIONAL dependency, lazy-imported, with a graceful failure if it is not installed.
# Audio never leaves the machine — no per-use cost, no API key, no voice sent to the cloud.
#
#   pip install faster-whisper
#
# "base.en" is a good CPU default: fast, English-only, decent accuracy for short commands.
DEFAULT_STT_MODEL = os.environ.get("HELIX_STT_MODEL", "base.en")

# The model is heavy to construct (and downloads weights on first use), so keep one instance
# alive across calls keyed by model size.
_MODELS: dict[str, object] = {}


class TranscribeError(RuntimeError):
    """Raised when local speech-to-text is unavailable or fails."""


def is_available() -> bool:
    """True if faster-whisper is importable (does NOT load a model — that is deferred)."""
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False
    return True


def _get_model(model_size: str):
    model = _MODELS.get(model_size)
    if model is not None:
        return model
    try:
        from faster_whisper import WhisperModel
    except Exception as error:  # not installed / import failure
        raise TranscribeError(
            "Local speech-to-text needs faster-whisper. Install it with:  pip install faster-whisper"
        ) from error
    # int8 on CPU is the lightest, broadly-compatible setting; first construction downloads weights.
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    _MODELS[model_size] = model
    return model


def transcribe(audio_path: str, model_size: str = DEFAULT_STT_MODEL, language: str = "en") -> str:
    """Transcribe a local audio file (e.g. WAV) to text. Runs on a worker thread (slow on CPU).

    Raises TranscribeError if faster-whisper is not installed or the file can't be read. The
    caller (the Xpert tab) surfaces that message and falls back gracefully, exactly like the
    edge-tts voice path does when its optional dependency is missing.
    """
    if not audio_path or not os.path.exists(audio_path):
        raise TranscribeError("No audio was captured.")
    model = _get_model(model_size)
    # beam_size=1 (greedy) is fastest and plenty for short spoken commands.
    segments, _info = model.transcribe(audio_path, language=language, beam_size=1)
    text = " ".join(segment.text for segment in segments).strip()
    return text
