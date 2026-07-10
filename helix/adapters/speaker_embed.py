"""Neural speaker embeddings — the acoustic backbone of voice identity. Optional, like all of voice.

A WeSpeaker CAM++ model (VoxCeleb-trained, Apache-2.0, ~28 MB ONNX) turns an utterance into a
512-dim voice-print via onnxruntime, which the --with-voice build already ships for faster-whisper's
VAD. This replaces hand-rolled cepstral statistics as the discriminator: a trained network separates
real voices across rooms, distances, and days in a way classical DSP cannot.

Mirrors helix/adapters/speech.py exactly:
  - a MODULE-level session cache, pre-warmed from main.py BEFORE Qt starts;
  - prewarm() never raises — no model (offline first run, no onnxruntime) just means the DSP
    fallback in helix/services/voiceid.py keeps working;
  - the model file downloads once (sha256-pinned) into data/models/, like whisper's weights.

The fbank frontend reimplements kaldi-native-fbank's defaults in numpy (25 ms/10 ms, dither 0,
per-frame DC removal, pre-emphasis 0.97, povey window, 80 mels 20–8000 Hz, log, NO cepstral or
utterance-level mean normalization, int16-scale samples per the model's normalize_samples=0
metadata) — validated at 0.97+ cosine agreement against the sherpa-onnx reference extractor.
DO NOT "fix" these choices to look more standard (e.g. adding CMN drops agreement to 0.19):
changing the frontend silently invalidates every neural voice-print already stored in profiles.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from helix.logging_setup import get_logger

_LOG = get_logger("speaker_embed")

MODEL_FILE = "speaker_campplus.onnx"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/wespeaker_en_voxceleb_CAM%2B%2B.onnx"  # tag typo is upstream's
)
MODEL_SHA256 = "c46fad10b5f81e1aa4a60c162714208577093655076c5450f8c469e522ec54ef"
EMBED_DIM = 512

_SR = 16000
_FRAME, _HOP, _NFFT, _NMEL = 400, 160, 512, 80
_FMIN, _FMAX = 20.0, 8000.0  # knf defaults: low 20, high 0 → Nyquist
_PREEMPH = 0.97

# Module-level cache (survives into the container, same as speech._MODELS).
_SESSION = None
_MODEL_PATH: Path | None = None


def importable() -> bool:
    """True if onnxruntime is installed (the engine could be used)."""
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def ready() -> bool:
    """True if the model is already loaded in-process, so embed() will not build anything now."""
    return _SESSION is not None


def _verify(path: Path) -> bool:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == MODEL_SHA256
    except Exception:
        return False


_MAX_DOWNLOAD = 64 * 1024 * 1024  # the model is ~28 MB; anything past this is not our file
_TOTAL_DEADLINE_S = 300.0         # hard wall-clock cap — a trickling server must not hold launch


def _download(path: Path) -> bool:
    """Fetch the pinned model once (first voice-enabled launch). Best-effort; never raises. Bounded
    in size AND total time (this runs synchronously before the window appears), and always cleans up
    its .part file — a failed attempt leaves nothing behind."""
    tmp = path.with_suffix(".onnx.part")
    try:
        import time

        import httpx

        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _TOTAL_DEADLINE_S
        written = 0
        timeout = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0)
        with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes():
                    written += len(chunk)
                    if written > _MAX_DOWNLOAD or time.monotonic() > deadline:
                        raise RuntimeError("speaker model download exceeded its size/time budget")
                    f.write(chunk)
        if not _verify(tmp):  # a tampered/truncated download never becomes the model
            tmp.unlink(missing_ok=True)
            _LOG.warning("speaker model download failed integrity check; discarded")
            return False
        os.replace(tmp, path)
        return True
    except Exception as exc:
        _LOG.warning("speaker model download failed: %s", exc)
        try:
            tmp.unlink(missing_ok=True)  # never leave a 28 MB .part corpse behind
        except OSError:
            pass
        return False


def prewarm(models_dir: Path) -> bool:
    """Load (downloading once if needed) the speaker model now. Never raises; False = voice identity
    stays on the DSP fallback. Call BEFORE Qt for symmetry with the whisper prewarm — onnxruntime is
    better behaved than ctranslate2, but one native-init path at startup is one class of crash."""
    global _SESSION, _MODEL_PATH
    if _SESSION is not None:
        return True
    if not importable():
        return False
    path = Path(models_dir) / MODEL_FILE
    if not path.exists() or not _verify(path):
        if path.exists():
            _LOG.warning("speaker model on disk failed integrity check; re-downloading")
        if not _download(path):
            return False
    try:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 2  # an utterance embeds in well under a second on CPU
        _SESSION = ort.InferenceSession(str(path), sess_options=opts,
                                        providers=["CPUExecutionProvider"])
        _MODEL_PATH = path
        return True
    except Exception as exc:
        _LOG.warning("speaker model failed to load: %s", exc)
        _SESSION = None
        return False


# ---------------------------------------------------------------------------
# kaldi-native-fbank–compatible 80-dim log-mel frontend (numpy)
# ---------------------------------------------------------------------------
_FILTERBANK = None
_WINDOW = None


def _mel(hz):
    import numpy as np
    return 1127.0 * np.log(1.0 + hz / 700.0)


def _fbank_matrix():
    global _FILTERBANK
    if _FILTERBANK is None:
        import numpy as np

        # Kaldi-style triangles: for each mel bin, weight each FFT bin by its position between the
        # neighboring band centers (equivalent to standard triangular filters in mel space).
        fft_hz = np.arange(_NFFT // 2 + 1) * (_SR / _NFFT)
        fft_mel = _mel(fft_hz)
        centers = np.linspace(_mel(_FMIN), _mel(_FMAX), _NMEL + 2)
        fb = np.zeros((_NMEL, _NFFT // 2 + 1), dtype=np.float32)
        for i in range(_NMEL):
            left, center, right = centers[i], centers[i + 1], centers[i + 2]
            up = (fft_mel - left) / (center - left)
            down = (right - fft_mel) / (right - center)
            fb[i] = np.maximum(0.0, np.minimum(up, down)).astype(np.float32)
        _FILTERBANK = fb
    return _FILTERBANK


def _povey_window():
    global _WINDOW
    if _WINDOW is None:
        import numpy as np
        n = np.arange(_FRAME)
        _WINDOW = ((0.5 - 0.5 * np.cos(2 * np.pi * n / (_FRAME - 1))) ** 0.85).astype(np.float32)
    return _WINDOW


def fbank80(pcm: bytes):
    """80-dim log-mel frames (float32 [T, 80]) from 16 kHz mono int16 PCM — matched bit-for-bit to
    kaldi-native-fbank as sherpa-onnx feeds this model: int16-scale samples, NO mean normalization
    (verified against the reference extractor — adding CMN sends the embedding somewhere else
    entirely, cosine 0.19 vs 0.97)."""
    import numpy as np

    usable = len(pcm) - (len(pcm) % 2)
    if usable < _FRAME * 2:
        return None
    x = np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32)  # int16 scale, per model metadata
    n_frames = 1 + (x.size - _FRAME) // _HOP  # snip_edges
    if n_frames < 10:
        return None
    idx = np.arange(_FRAME)[None, :] + _HOP * np.arange(n_frames)[:, None]
    frames = x[idx]
    frames = frames - frames.mean(axis=1, keepdims=True)              # remove_dc_offset
    pre = np.empty_like(frames)                                       # pre-emphasis 0.97
    pre[:, 1:] = frames[:, 1:] - _PREEMPH * frames[:, :-1]
    pre[:, 0] = frames[:, 0] - _PREEMPH * frames[:, 0]
    spec = np.abs(np.fft.rfft(pre * _povey_window(), n=_NFFT)) ** 2
    return np.log(np.maximum(spec @ _fbank_matrix().T, 1e-10)).astype(np.float32)


def embed(pcm: bytes):
    """A unit-norm 512-dim neural voice-print for the utterance — or None when the model isn't
    loaded or the clip is too short. Callers gate voicing/noise BEFORE this (see voiceid.embed_pcm);
    this is pure feature extraction + inference."""
    if _SESSION is None:
        return None
    try:
        import numpy as np

        feats = fbank80(pcm)
        if feats is None:
            return None
        out = _SESSION.run(None, {"feats": feats[None, ...]})[0][0]
        vec = np.asarray(out, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 1e-6 else None
    except Exception:  # noqa: BLE001 — a failed embedding just means "no neural evidence"
        _LOG.warning("neural speaker embedding failed", exc_info=True)
        return None
