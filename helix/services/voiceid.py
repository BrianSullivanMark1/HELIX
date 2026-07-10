"""VoiceIdService — HELIX knows WHO is speaking.

Multiple people can register with HELIX by voice ("Hey, I am Brian"). Each registered user has a
voice profile: a set of compact acoustic embeddings (voice-prints) plus identity notes learned in a
short, friendly calibration conversation. Every spoken command addressed to HELIX is matched against
the registered profiles before it is acted on:

  - a match        → the turn runs, tagged with the speaker's identity (their notes join the context),
                     and the utterance's embedding quietly sharpens their profile (passive learning);
  - no match       → HELIX replies ONLY "I do not recognize this voice — would you like to register?"
                     and never acts on the words;
  - no profiles yet→ the gate is open (single-user trust) until someone registers.

"Recalibrate my voice" re-runs the guided calibration for the recognized speaker at any time.

The voice-print is deliberately dependency-free (numpy only, already a hard requirement): per-utterance
mel-cepstral statistics + pitch. It is not lab-grade biometrics — it is household-grade speaker
recognition, which is the actual job: telling apart the handful of voices that live with this machine,
on the same microphone, and improving with every interaction. All of it is local; audio never leaves
the machine, and only the derived embeddings (numbers, not audio) are stored.

Thresholds are SELF-CALIBRATING per user (accept when a new utterance scores like that user's own past
utterances score against each other) — automatic, no settings knobs, and naturally tightening as a
profile accumulates samples.

Pure Python + numpy; no Qt. The UI layer (VoiceController / Console) calls into this service.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from helix.logging_setup import get_logger

_LOG = get_logger("voiceid")

# ---------------------------------------------------------------------------
# Acoustic front end (16 kHz mono int16 PCM → embedding)
# ---------------------------------------------------------------------------
_SR = 16000
_FRAME = 400          # 25 ms analysis window
_HOP = 160            # 10 ms hop
_NFFT = 512
_NMEL = 40            # mel filters
_NCEP = 20            # cepstral coefficients kept (c1..c20; c0/energy excluded)
_FMIN, _FMAX = 64.0, 7600.0
_PITCH_LO, _PITCH_HI = 70.0, 380.0   # plausible speaking F0 range

MIN_VOICED_S = 0.6    # an utterance needs this much voiced audio to carry speaker evidence
_ENERGY_GATE = 0.02   # voiced frame = energy within ~17 dB of the utterance's loudest frame

# Block weights: mel-cepstral means are the strongest speaker cue; spread and dynamics refine it;
# pitch separates registers cheaply. Each block is L2-normalized before weighting, so no single
# block can dominate on raw magnitude.
_W_MEAN, _W_STD, _W_DMEAN, _W_DSTD, _W_PITCH = 1.0, 0.55, 0.35, 0.35, 0.6

EMBED_DIM = _NCEP * 4 + 2


def _mel(hz: float) -> float:
    import numpy as np
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def _mel_inv(m: float) -> float:
    return 700.0 * (10.0 ** (m / 2595.0) - 1.0)


_FILTERBANK = None  # built lazily, cached for the process


def _filterbank():
    global _FILTERBANK
    if _FILTERBANK is None:
        import numpy as np
        edges = np.array([_mel_inv(m) for m in np.linspace(_mel(_FMIN), _mel(_FMAX), _NMEL + 2)])
        bins = np.floor((_NFFT + 1) * edges / _SR).astype(int)
        fb = np.zeros((_NMEL, _NFFT // 2 + 1), dtype=np.float32)
        for i in range(_NMEL):
            lo, mid, hi = bins[i], bins[i + 1], bins[i + 2]
            if mid > lo:
                fb[i, lo:mid] = (np.arange(lo, mid) - lo) / (mid - lo)
            if hi > mid:
                fb[i, mid:hi] = (hi - np.arange(mid, hi)) / (hi - mid)
        _FILTERBANK = fb
    return _FILTERBANK


def _l2(v):
    import numpy as np
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-9 else v


def _dsp_embed(pcm: bytes) -> "object | None":
    """The classical voice-print (float32, EMBED_DIM, unit-norm) from 16 kHz mono int16 PCM — or None
    when the clip has too little voiced audio to say anything about the speaker (e.g. a bare 'stop').
    Pure numpy; a few milliseconds even for the longest utterance. Doubles as the VOICING GATE for
    the neural backend: silence, noise, and blips never reach either matcher."""
    try:
        import numpy as np

        usable = len(pcm) - (len(pcm) % 2)
        if usable < _FRAME * 2:
            return None
        x = np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32) / 32768.0
        n_frames = 1 + (x.size - _FRAME) // _HOP
        if n_frames < 8:
            return None
        idx = np.arange(_FRAME)[None, :] + _HOP * np.arange(n_frames)[:, None]
        raw = x[idx]  # unwindowed — the pitch autocorrelation needs full energy at long lags
        frames = raw * np.hanning(_FRAME).astype(np.float32)

        energy = (frames ** 2).mean(axis=1)
        peak = float(energy.max())
        if peak <= 1e-8:
            return None
        voiced = energy >= max(peak * _ENERGY_GATE, 1e-7)
        if voiced.sum() * _HOP / _SR < MIN_VOICED_S:
            return None

        spec = np.abs(np.fft.rfft(frames[voiced], n=_NFFT)) ** 2
        mels = np.log(spec @ _filterbank().T + 1e-9)
        # DCT-II (orthonormal) of the log-mel energies → cepstrum; keep c1..c20.
        k = np.arange(_NMEL, dtype=np.float32)
        basis = np.cos(np.pi * (k[None, :] + 0.5) * np.arange(1, _NCEP + 1)[:, None] / _NMEL)
        ceps = mels @ basis.T * np.sqrt(2.0 / _NMEL)

        deltas = np.diff(ceps, axis=0) if ceps.shape[0] > 1 else np.zeros_like(ceps[:1])

        # Pitch per voiced frame via normalized autocorrelation over the speaking-F0 lag range —
        # on the RAW frames: the Hann window starves long lags of overlap energy, which would make
        # low-pitched (long-period) voices look unvoiced and get rejected by the voicing gate below.
        lo_lag, hi_lag = int(_SR / _PITCH_HI), int(_SR / _PITCH_LO)
        vf = raw[voiced]
        vf = vf - vf.mean(axis=1, keepdims=True)
        f0s = []
        sampled = 0
        for row in vf[:: max(1, len(vf) // 60)]:  # ≤ ~60 frames is plenty for a median
            sampled += 1
            denom = float(row @ row)
            if denom < 1e-6:
                continue
            corr = np.correlate(row, row, mode="full")[_FRAME - 1 + lo_lag : _FRAME - 1 + hi_lag]
            if corr.size == 0:
                continue
            # Overlap-corrected normalized autocorrelation: at lag L only N−L samples overlap, so an
            # uncorrected score punishes long periods — a 70 Hz voice would read as "unvoiced".
            lags = np.arange(lo_lag, lo_lag + corr.size, dtype=np.float32)
            strength = corr / (denom * np.maximum(0.3, 1.0 - lags / _FRAME))
            best = int(strength.argmax())
            if strength[best] > 0.5:  # clear periodicity only
                f0s.append(_SR / (lo_lag + best))
        # VOICING GATE: real speech is periodic; broadband noise (fans, blenders, white noise) is not —
        # and without this gate two independent noise clips embed to a shared "noise direction"
        # (cos ≈ 0.94), which would both false-match and poison profiles. No pitch → no speaker evidence.
        if not f0s or (sampled and len(f0s) / sampled < 0.25):
            return None
        lf = np.log2(np.array(f0s, dtype=np.float32))
        # tanh keeps the pitch block BOUNDED without destroying its magnitude the way per-block L2
        # would (L2 of a 2-vector keeps only its direction — a 110 Hz and a 220 Hz speaker with
        # proportional spreads would look identical).
        pitch = np.tanh(np.array([float(np.median(lf)) - np.log2(150.0),
                                  float(np.percentile(lf, 75) - np.percentile(lf, 25))],
                                 dtype=np.float32))

        vec = np.concatenate([
            _W_MEAN * _l2(ceps.mean(axis=0)),
            _W_STD * _l2(ceps.std(axis=0)),
            _W_DMEAN * _l2(deltas.mean(axis=0)),
            _W_DSTD * _l2(deltas.std(axis=0)),
            _W_PITCH * pitch,
        ]).astype(np.float32)
        return _l2(vec)
    except Exception:  # noqa: BLE001 — a failed embedding just means "no speaker evidence"
        _LOG.warning("voice embedding failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Voice-prints: one utterance, up to two embeddings
# ---------------------------------------------------------------------------
@dataclass
class VoicePrint:
    """The acoustic evidence one utterance carries. `dsp` is the built-in classical embedding
    (always present when the clip passed the voicing gate); `neural` is the CAM++ 512-dim embedding
    when the speaker model is loaded — far stronger across rooms, distances, and days."""
    dsp: object | None = None
    neural: object | None = None


def _as_print(emb) -> "VoicePrint | None":
    """Accept either a VoicePrint or a bare vector (treated as a DSP print) — the compatibility shim
    that keeps every caller and test that hands raw arrays working unchanged."""
    if emb is None or isinstance(emb, VoicePrint):
        return emb
    return VoicePrint(dsp=emb)


def embed_pcm(pcm: bytes) -> "VoicePrint | None":
    """The utterance's voice-print(s) from 16 kHz mono int16 PCM — or None when the clip has too
    little voiced audio to say anything about the speaker. The DSP path runs first as the voicing
    gate; the neural embedding is added whenever the speaker model is pre-warmed."""
    dsp = _dsp_embed(pcm)
    if dsp is None:
        return None
    neural = None
    try:
        # Module-level cached session, pre-warmed in main.py before Qt (mirrors WhisperSpeechIn).
        # Imported lazily so voice identity never hard-depends on onnxruntime being installed.
        from helix.adapters import speaker_embed

        if speaker_embed.ready():
            neural = speaker_embed.embed(pcm)
    except Exception:  # noqa: BLE001 — no neural evidence just means the DSP fallback decides
        neural = None
    return VoicePrint(dsp=dsp, neural=neural)


# ---------------------------------------------------------------------------
# Profiles + matching
# ---------------------------------------------------------------------------
_ENROLL_CAP = 24      # calibration embeddings kept per user (recalibrations rotate the oldest out)
_PASSIVE_CAP = 48     # passively-learned embeddings kept per user (ring: newest wins)
_TOP_K = 3            # score = mean similarity of the K best-matching stored samples
_MIN_NEURAL = 3       # a profile needs this many neural samples before the neural matcher takes over

# Per-backend decision constants. The DSP embedding self-clusters tightly (its scores run high for
# everyone), so its bars sit high; the neural embedding separates real speakers with far more room
# (same-speaker ≈ 0.5–0.8, different ≈ 0.0–0.35 on this model), so its bars sit lower and its margin
# demands a much clearer winner. Same self-calibrating machinery either way.
#              floor  ceil  margin sigma_floor ramp_step ramp_ceil learn_headroom
_TUNING = {
    "dsp":    (0.60,  0.90, 0.035, 0.04,       0.004,    0.85,     0.04),
    "neural": (0.42,  0.78, 0.10,  0.05,       0.005,    0.62,     0.05),
}


@dataclass
class MatchResult:
    """The identity decision for one utterance."""
    name: str | None          # matched user, or None
    score: float = 0.0        # best cosine score (0 when no evidence)
    confident: bool = False   # solid enough to learn from passively
    no_evidence: bool = False # clip too short/quiet to judge (≠ rejected)


@dataclass
class _Profile:
    name: str
    # Sample entries are {"d": [floats]|None, "n": [floats]|None} — one utterance, both backends.
    enroll: list = field(default_factory=list)   # calibration samples
    passive: list = field(default_factory=list)  # passively learned samples
    notes: str = ""                              # identity model distilled from calibration chat
    created: float = 0.0
    updated: float = 0.0

    def matrix(self, backend: str):
        """All stored embeddings for one backend as a float32 matrix — or None when it has none."""
        import numpy as np
        key = "d" if backend == "dsp" else "n"
        rows = [e[key] for e in self.enroll + self.passive if e.get(key)]
        return np.asarray(rows, dtype=np.float32) if rows else None

    def count(self, backend: str) -> int:
        key = "d" if backend == "dsp" else "n"
        return sum(1 for e in self.enroll + self.passive if e.get(key))


def _sample_entry(print_: VoicePrint) -> dict:
    return {
        "d": [round(float(x), 5) for x in print_.dsp] if print_.dsp is not None else None,
        "n": [round(float(x), 5) for x in print_.neural] if print_.neural is not None else None,
    }


class VoiceIdService:
    """Registered voice profiles + the per-utterance identity decision. Thread-safe; persisted as
    plain JSON next to the other data files (embeddings only — never audio)."""

    def __init__(self, path: Path, chat=None) -> None:
        self._path = Path(path)
        self._chat = chat  # optional ChatModel — distills calibration answers into identity notes
        self._lock = threading.RLock()
        self._profiles: dict[str, _Profile] = {}
        self._load()

    # ----- persistence -----
    @staticmethod
    def _load_samples(entries) -> list:
        """Sample entries from disk — v2 dicts pass through; v1 plain float-lists become DSP-only
        entries, so profiles registered before the neural upgrade keep working and then sharpen as
        passive learning adds neural samples on top."""
        out = []
        for e in entries or []:
            try:  # one corrupt entry drops ITSELF, never the profile (let alone every user's)
                if isinstance(e, dict):
                    d, n = e.get("d"), e.get("n")
                    if d or n:
                        out.append({"d": [float(x) for x in d] if d else None,
                                    "n": [float(x) for x in n] if n else None})
                elif isinstance(e, (list, tuple)) and e:
                    out.append({"d": [float(x) for x in e], "n": None})
            except (TypeError, ValueError):
                continue
        return out

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for u in raw.get("users", []):
                p = _Profile(
                    name=str(u.get("name", "")).strip(),
                    enroll=self._load_samples(u.get("enroll")),
                    passive=self._load_samples(u.get("passive")),
                    notes=str(u.get("notes", "")),
                    created=float(u.get("created", 0.0)),
                    updated=float(u.get("updated", 0.0)),
                )
                if p.name and (p.enroll or p.passive):
                    self._profiles[p.name.lower()] = p
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — a corrupt file must not brick voice; start empty
            _LOG.warning("voice profiles unreadable; starting empty", exc_info=True)

    def _save_locked(self) -> None:
        data = {
            "version": 2,
            "users": [
                {
                    "name": p.name,
                    "enroll": p.enroll,
                    "passive": p.passive,
                    "notes": p.notes,
                    "created": p.created,
                    "updated": p.updated,
                }
                for p in self._profiles.values()
            ],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception:  # noqa: BLE001 — failing to persist must not break the running session
            _LOG.warning("could not save voice profiles", exc_info=True)

    # ----- introspection -----
    def has_profiles(self) -> bool:
        with self._lock:
            return bool(self._profiles)

    def names(self) -> list[str]:
        with self._lock:
            return [p.name for p in self._profiles.values()]

    def notes_for(self, name: str | None) -> str:
        if not name:
            return ""
        with self._lock:
            p = self._profiles.get(name.lower())
            return p.notes if p else ""

    # ----- matching -----
    @staticmethod
    def _score(rows, emb) -> float:
        import numpy as np
        sims = rows @ np.asarray(emb, dtype=np.float32)  # all unit-norm → dot = cosine
        k = min(_TOP_K, sims.size)
        return float(np.sort(sims)[-k:].mean())

    @staticmethod
    def _self_stats(rows, sigma_floor: float) -> tuple[float, float]:
        """(µ, σ) of this user's own top-K scores when their stored samples are matched against each
        other — i.e. what a genuine utterance from them typically scores. Self-calibrating: young
        profiles get a generous spread; rich profiles tighten automatically."""
        import numpy as np
        if rows is None or len(rows) < 3:
            return 0.80, 0.10  # too young to know — be generous
        sims = rows @ rows.T
        np.fill_diagonal(sims, -1.0)
        k = min(_TOP_K, len(rows) - 1)
        per = np.sort(sims, axis=1)[:, -k:].mean(axis=1)
        return float(per.mean()), max(float(per.std()), sigma_floor)

    def _judge_locked(self, profile: _Profile, print_: VoicePrint):
        """Score one profile against one utterance on the profile's STRONGEST usable backend:
        neural once it holds enough neural samples (and the utterance has a neural print), else the
        DSP fallback. Returns (backend, score, accept_at, confident_at) or None (no common backend)."""
        backend = "dsp"
        if print_.neural is not None and profile.count("neural") >= _MIN_NEURAL:
            backend = "neural"
        emb = print_.neural if backend == "neural" else print_.dsp
        rows = profile.matrix(backend)
        if emb is None or rows is None or not rows.size:
            return None
        floor, ceil, _margin, sigma_floor, ramp_step, ramp_ceil, headroom = _TUNING[backend]
        score = self._score(rows, emb)
        mu, sigma = self._self_stats(rows, sigma_floor)
        accept_at = min(max(mu - 1.5 * sigma, floor), ceil)
        # A profile mostly knows one room, one mood, one distance — its self-similarity is
        # unrealistically tight, so cap the bar on a slow ramp that rises with sample count and never
        # exceeds a livable ceiling. The ramp applies at EVERY size (a hard cutoff at some n would
        # make the bar jump in one utterance and lock the real owner out the next morning). This is
        # the "recognition improves with use" curve, applied to the threshold itself.
        accept_at = min(accept_at, min(ramp_ceil, floor + ramp_step * len(rows)))
        # Learn passively from matches comfortably ABOVE the accept bar (relative to the actual bar —
        # a bar stricter than acceptance would mean the profile never loosens with real life).
        confident_at = min(accept_at + headroom, ceil)
        return backend, score, accept_at, confident_at

    def identify(self, emb) -> MatchResult:
        """Match one utterance's voice-print against every registered profile. Accepts a VoicePrint
        or a bare vector (DSP); None (clip too short) → no_evidence. With no profiles registered the
        gate is open: name=None, but callers must check has_profiles() to know the gate is inactive.

        Decision procedure (scores are only comparable WITHIN a backend, and each profile has its own
        self-calibrated bar — so no sorting across profiles by score or by headroom):
          1. every profile is judged on its strongest usable backend;
          2. the ACCEPTED set = profiles whose score clears their own bar;
          3. a neural acceptance outranks any DSP acceptance (the trained model is the far stronger
             discriminator — mixed households mid-upgrade must not let a loose DSP bar steal a match);
          4. within the winning backend the highest raw score wins, and it must beat EVERY other
             profile judged on that backend (accepted or not) by that backend's margin — one voice
             scoring like two different people is too close to call, however many rivals there are."""
        print_ = _as_print(emb)
        if print_ is None:
            return MatchResult(None, no_evidence=True)
        with self._lock:
            if not self._profiles:
                return MatchResult(None)
            judged = []  # (backend, score, accept_at, confident_at, profile)
            for p in self._profiles.values():
                j = self._judge_locked(p, print_)
                if j is not None:
                    backend, score, accept_at, confident_at = j
                    judged.append((backend, score, accept_at, confident_at, p))
            if not judged:
                return MatchResult(None, no_evidence=True)
            accepted = [j for j in judged if j[1] >= j[2]]
            if not accepted:
                best_score = max(j[1] for j in judged)
                return MatchResult(None, score=best_score)
            backend = "neural" if any(j[0] == "neural" for j in accepted) else "dsp"
            pool = [j for j in accepted if j[0] == backend]
            _b, best_score, _a, confident_at, best = max(pool, key=lambda t: t[1])
            margin = _TUNING[backend][2]
            for other_backend, score, _oa, _oc, p in judged:
                if p is best or other_backend != backend:
                    continue
                if best_score - score < margin:
                    return MatchResult(None, score=best_score)  # two profiles too close to call
            confident = best_score >= confident_at
            return MatchResult(best.name, score=best_score, confident=confident)

    # ----- learning -----
    def add_passive(self, name: str, emb) -> None:
        """Quietly sharpen a profile with a confidently-matched utterance (ring-buffered). This is
        also how pre-neural profiles upgrade themselves: every confidently-matched utterance adds a
        neural sample, and once enough accumulate the neural matcher takes over automatically."""
        print_ = _as_print(emb)
        if print_ is None:
            return
        with self._lock:
            p = self._profiles.get(name.lower())
            if p is None:
                return
            p.passive.append(_sample_entry(print_))
            if len(p.passive) > _PASSIVE_CAP:
                del p.passive[: len(p.passive) - _PASSIVE_CAP]
            p.updated = time.time()
            self._save_locked()

    def register(self, name: str, embeddings: list, notes: str = "") -> bool:
        """Create (or extend, on recalibration) a profile from calibration voice-prints."""
        prints = [pr for pr in (_as_print(e) for e in embeddings) if pr is not None]
        name = (name or "").strip()
        if not name or not prints:
            return False
        with self._lock:
            p = self._profiles.get(name.lower())
            if p is None:
                p = _Profile(name=name, created=time.time())
                self._profiles[name.lower()] = p
            p.enroll.extend(_sample_entry(pr) for pr in prints)
            if len(p.enroll) > _ENROLL_CAP:  # recalibration rotates the oldest prints out
                del p.enroll[: len(p.enroll) - _ENROLL_CAP]
            if notes.strip():
                p.notes = notes.strip()
            p.updated = time.time()
            self._save_locked()
            return True

    def set_notes(self, name: str, notes: str) -> None:
        # Notes are spoken by whoever registered and later ride inside a bracketed context block on
        # every turn — sanitize at this single choke point: one line, no brackets (they'd close the
        # block early), bounded length.
        clean = " ".join((notes or "").split()).replace("[", "(").replace("]", ")")[:1200]
        with self._lock:
            p = self._profiles.get((name or "").lower())
            if p is not None:
                p.notes = clean
                p.updated = time.time()
                self._save_locked()

    def remove(self, name: str) -> bool:
        with self._lock:
            if self._profiles.pop((name or "").lower(), None) is not None:
                self._save_locked()
                return True
            return False

    # ----- identity notes -----
    def distill_notes(self, name: str, answers: list[str]) -> None:
        """Turn the calibration answers (work, role, habits, team) into a compact identity note on a
        BACKGROUND thread — the fast chat model when available, a plain join otherwise. Never blocks
        or raises; a failed distillation just leaves the raw answers as the note."""
        answers = [a.strip() for a in (answers or []) if a and a.strip()]
        if not answers:
            return
        raw = " ".join(answers)[:1200]
        self.set_notes(name, raw)  # immediately useful; the distilled version replaces it shortly
        if self._chat is None:
            return

        def _go() -> None:
            try:
                from helix.domain.models import Role
                from helix.ports.llm import Text, Turn

                prompt = (
                    f"A user named {name} just registered their voice with HELIX and answered a few "
                    "onboarding questions about their work, role, habits and team:\n\n"
                    + "\n".join(f"- {a}" for a in answers)
                    + "\n\nWrite the identity note now."
                )
                reply = self._chat.chat([Turn(Role.USER, (Text(prompt),))], system=NOTES_SYSTEM)
                text = (reply.text or "").strip()
                if text and len(text) <= 1200:
                    self.set_notes(name, text)
            except Exception:  # noqa: BLE001 — the raw-answers note already stands
                _LOG.warning("identity-note distillation failed", exc_info=True)

        threading.Thread(target=_go, daemon=True, name="helix-voiceid-notes").start()


# ---------------------------------------------------------------------------
# The enrollment / recalibration conversation (a small deterministic state machine —
# warm words, scripted flow; the answers are distilled into identity notes afterwards)
# ---------------------------------------------------------------------------
UNRECOGNIZED_REPLY = "I do not recognize this voice — would you like to register?"

# Only real self-introductions — "this is / it's" deliberately excluded (they open ordinary sentences:
# "it's raining", "this is broken"). The name is any Unicode letter run (José, Zoë, 陽子 all register).
_I_AM_RE = re.compile(
    r"^(?:hey[,!\s]+)?(?:i\s*am|i'm|my\s+name\s+is)\s+([^\W\d_][^\W\d_'-]{0,30})",
    re.IGNORECASE,
)
# Words that follow "I am …" in everyday speech but are never names — so "I'm heading out" or
# "I am absolutely sure" can't hijack a command into enrollment.
_NOT_NAMES = frozenset(
    "a about absolutely afraid all almost already also always back busy cold coming done down early "
    "fine glad going gonna good happy heading here home hot hungry in just late leaving looking lost "
    "no not now off okay ok on only out ready really so sorry still sure the there tired too trying "
    "up very waiting wondering working yes".split()
)
_YES_RE = re.compile(
    r"^(?:yes|yeah|yep|sure|ok(?:ay)?|please|go\s+ahead|do\s+it|register(?:\s+me)?|let'?s\s+do\s+it)\b",
    re.IGNORECASE,
)
_NO_RE = re.compile(r"^(?:no|nope|nah|not\s+now|later|cancel|never\s*mind|stop)\b", re.IGNORECASE)
_RECAL_RE = re.compile(r"\brecalibrat\w*\s+(?:my\s+)?voice\b|\bvoice\s+recalibrat\w*\b", re.IGNORECASE)
_REGISTER_RE = re.compile(r"\bregister\b.*\bvoice\b|\bregister\s+me\b|\bvoice\s+registration\b", re.IGNORECASE)
# An utterance that ASKS FOR A BUILD is a request, not a calibration command — "build a voice
# recalibration app" must reach the model, not start recalibration (same guard as is_dismissal's).
_BUILD_GUARD_RE = re.compile(r"\b(?:build|make|create|design|generate|write|code|add)\b", re.IGNORECASE)
# Everyday command verbs: their presence means the utterance is a REQUEST, never an introduction.
_COMMAND_GUARD_RE = re.compile(
    r"\b(?:open|close|lock|unlock|turn|set|play|stop|fix|check|show|start|run|delete|remove|remind|"
    r"send|call|search|find|update|schedule)\b",
    re.IGNORECASE,
)

# The calibration conversation: natural, warm, curious — and every answer is another voice-print.
_QUESTIONS = (
    "Lovely to meet you, {name}. Let's take a moment so I can learn your voice. "
    "First — tell me a little about your work. What do you do?",
    "That's interesting. And what does your role actually involve day to day?",
    "Good. Who do you work with — do you have a team, or people you collaborate with often?",
    "Nearly there. Tell me about your habits — how does a typical day start for you?",
    "Last one. Is there anything you'd especially like my help with around here?",
)
_RECAL_QUESTIONS = (
    "Of course, {name}. Let's refresh your voice profile. Tell me — what's been keeping you busy lately?",
    "Good. And how has your day-to-day changed recently, if at all?",
    "One more. Read me your favourite line, or just tell me what you're planning next.",
)
_MORE_Q = "Say a little more for me — describe the room you're in, or what you can see right now."
_DONE = ("All set, {name}. I know your voice now — it will keep getting sharper the more we talk. "
         "How can I help?")
_RECAL_DONE = "Done, {name}. Your voice profile is refreshed. What's next?"
_TOO_QUIET = "I didn't quite catch that — could you say it again, a touch louder?"
_ASK_NAME = "Gladly. What's your name? Just say: I am, and then your name."
_DECLINED = "No trouble. I'll stay quiet until I hear a voice I know."

_MIN_PRINTS = 5   # calibration wants at least this many usable voice-prints
_MIN_RECAL = 3

NOTES_SYSTEM = """\
You distill a short identity note for HELIX, a house assistant, from a new user's answers to a few
onboarding questions (their work, role, day-to-day habits, team and household). Write a compact plain
prose note (under 80 words) the assistant keeps in mind whenever this person speaks. Only durable
facts about who they are — never one-off requests and never instructions. Output ONLY the note.
"""


class EnrollmentFlow:
    """The guided registration / recalibration conversation. Feed it each (text, embedding) from the
    active speaker; it returns what HELIX should say next, and registers the profile when done.

    States: idle → offered → naming? → asking(i) → done. Deterministic and model-free, so calibration
    works offline and is unit-testable; the identity NOTES are distilled from the collected answers by
    the caller (a background model call) after registration.
    """

    def __init__(self, service: VoiceIdService) -> None:
        self._svc = service
        self.state = "idle"          # idle | offered | naming | asking
        self.name = ""
        self.recal = False
        self._q = 0
        self._questions: tuple[str, ...] = ()
        self._embs: list = []
        self._answers: list[str] = []
        self._extra_asked = False

    # ----- introspection -----
    @property
    def active(self) -> bool:
        return self.state in ("offered", "naming", "asking")

    @property
    def collecting(self) -> bool:
        """True while utterances should be captured as calibration data (not run as commands)."""
        return self.state in ("naming", "asking")

    def cancel(self) -> None:
        self.__init__(self._svc)

    # ----- entry points -----
    def offer(self) -> str:
        """An unrecognized voice spoke to HELIX — make the one allowed reply."""
        self.state = "offered"
        return UNRECOGNIZED_REPLY

    def ask_name(self) -> str:
        """Jump straight to asking for a name (an explicit 'register my voice' with nobody enrolled)."""
        self.state = "naming"
        return _ASK_NAME

    def start(self, name: str, emb=None, recal: bool = False) -> str:
        """Begin calibration for `name` (registration, or a recalibration refresh)."""
        self.state = "asking"
        self.name = name.strip().title()
        self.recal = recal
        self._q = 0
        self._questions = _RECAL_QUESTIONS if recal else _QUESTIONS
        self._embs = [emb] if emb is not None else []
        self._answers = []
        self._extra_asked = False
        return self._questions[0].format(name=self.name)

    # ----- the conversation -----
    def handle(self, text: str, emb) -> str | None:
        """Advance the flow with one utterance from the active speaker. Returns HELIX's next line,
        or None when the utterance isn't for this flow (caller proceeds normally)."""
        t = (text or "").strip()
        if self.state == "offered":
            intro = introduction_name(t)
            if intro:
                return self.start(intro, emb)
            # A yes must BE the utterance ("yes please"), not merely open one ("okay so as I was
            # saying…") — the offer is often followed by speech that isn't for HELIX at all.
            if _YES_RE.match(t) and len(t.split()) <= 4:
                self.state = "naming"
                return _ASK_NAME
            self.cancel()
            if _NO_RE.match(t):
                return _DECLINED
            return None  # something else entirely — the caller re-gates it as unrecognized speech
        if self.state == "naming":
            if _NO_RE.match(t) and len(t.split()) <= 3:
                self.cancel()
                return _DECLINED
            name = introduction_name(t)
            if name is None:
                words = t.split()
                if 0 < len(words) <= 2 and words[0].isalpha() and words[0].lower() not in _NOT_NAMES:
                    name = words[0]  # they just said their name
            if name:
                return self.start(name, emb)
            if len(t.split()) > 3:
                self.cancel()  # a full sentence that isn't a name — this speech wasn't for the flow
                return None
            return _ASK_NAME
        if self.state == "asking":
            if _NO_RE.match(t) and len(t.split()) <= 3:
                self.cancel()
                return _DECLINED
            if emb is not None:
                self._embs.append(emb)
                self._answers.append(t)
            else:
                return _TOO_QUIET  # no usable voice-print (quiet, noise, or nothing) — same question again
            self._q += 1
            if self._q < len(self._questions):
                return self._questions[self._q].format(name=self.name)
            need = _MIN_RECAL if self.recal else _MIN_PRINTS
            if len(self._embs) < need and not self._extra_asked:
                self._extra_asked = True
                return _MORE_Q
            return self._finish()
        return None

    def _finish(self) -> str:
        name, embs, recal = self.name, list(self._embs), self.recal
        answers = list(self._answers)
        need = _MIN_RECAL if recal else _MIN_PRINTS
        self.cancel()
        # Enough distinct voice-prints or no profile at all — a 1-print profile would close the
        # household gate behind almost no evidence.
        if len(embs) < need or not self._svc.register(name, embs):
            return "I couldn't capture enough of your voice — let's try again another time."
        self.last_registered = name          # the caller distills identity notes from these
        self.last_answers = answers
        return (_RECAL_DONE if recal else _DONE).format(name=name)


def wants_recalibration(text: str) -> bool:
    """'Recalibrate my voice' (and close variants) — but never a build request that mentions it."""
    text = text or ""
    return bool(_RECAL_RE.search(text)) and not _BUILD_GUARD_RE.search(text)


def wants_registration(text: str) -> bool:
    """An explicit ask to register a voice ('register my voice', 'I am Brian' handled separately)."""
    text = text or ""
    return bool(_REGISTER_RE.search(text)) and not _BUILD_GUARD_RE.search(text)


def introduction_name(text: str) -> str | None:
    """The name in a self-introduction ('Hey, I am Brian') — or None. Deliberately strict: the
    utterance must BE an introduction (a short pleasantry tail at most), the captured word must not
    be everyday non-name vocabulary, and a request that carries an action/build verb is a command,
    not an introduction — so 'I'm heading out, lock the door' never starts an enrollment."""
    text = (text or "").strip()
    m = _I_AM_RE.match(text)
    if not m:
        return None
    name = m.group(1)
    if name.lower() in _NOT_NAMES:
        return None
    tail_words = re.findall(r"[^\W\d_]+", text[m.end():])
    if len(tail_words) > 4:  # a real introduction doesn't trail into a sentence
        return None
    if _BUILD_GUARD_RE.search(text) or _COMMAND_GUARD_RE.search(text):
        return None
    return name
