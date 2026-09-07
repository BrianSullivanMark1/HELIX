"""The neural voice-print backend — dual-backend matching, migration, and the real model.

Invariants: a profile with enough neural samples is judged by the neural matcher (whose thresholds
suit its wider score range); DSP-only (pre-upgrade) profiles keep matching and quietly accumulate
neural samples through passive learning until the neural matcher takes over; v1 profile files load
as DSP-only; and when the CAM++ model file is present on this machine, the real chain (kaldi fbank →
onnx) produces unit-norm 512-dim embeddings that agree with themselves across clips of one voice.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from helix.services.voiceid import (  # noqa: E402
    MatchResult,
    VoicePrint,
    VoiceIdService,
)


def _unit(seed: int, dim: int) -> "np.ndarray":
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _near(base, seed: int, wobble: float = 0.08):
    v = base + wobble * _unit(5000 + seed, base.size)
    return (v / np.linalg.norm(v)).astype(np.float32)


_A_DSP, _A_NEU = _unit(1, 82), _unit(11, 512)
_B_DSP, _B_NEU = _unit(2, 82), _unit(12, 512)


def _print(dsp_base, neu_base, seed):
    return VoicePrint(dsp=_near(dsp_base, seed), neural=_near(neu_base, seed))


def test_neural_matcher_takes_over_with_enough_samples(tmp_path):
    svc = VoiceIdService(tmp_path / "v.json")
    svc.register("Alice", [_print(_A_DSP, _A_NEU, s) for s in range(6)])
    # Same speaker, neural print present → matched via neural (weaker DSP evidence can't block it):
    # give the utterance a DSP print near Alice but a neural print near her too — matched.
    r = svc.identify(_print(_A_DSP, _A_NEU, 99))
    assert r.name == "Alice"
    # An utterance whose NEURAL print is a stranger's must be rejected even if its DSP print is
    # close to Alice's — the neural matcher (not DSP) is judging this profile now.
    r = svc.identify(VoicePrint(dsp=_near(_A_DSP, 50), neural=_near(_B_NEU, 50)))
    assert r.name is None


def test_dsp_only_profile_still_matches_without_neural(tmp_path):
    # A pre-upgrade profile (no neural samples) judged by DSP even when the utterance has neural.
    svc = VoiceIdService(tmp_path / "v.json")
    svc.register("Alice", [VoicePrint(dsp=_near(_A_DSP, s)) for s in range(6)])
    r = svc.identify(_print(_A_DSP, _A_NEU, 99))
    assert r.name == "Alice"


def test_passive_learning_upgrades_a_dsp_profile_to_neural(tmp_path):
    svc = VoiceIdService(tmp_path / "v.json")
    svc.register("Alice", [VoicePrint(dsp=_near(_A_DSP, s)) for s in range(6)])
    assert svc._profiles["alice"].count("neural") == 0
    for s in range(4):  # confident matches carry both prints — neural samples accumulate
        svc.add_passive("Alice", _print(_A_DSP, _A_NEU, 100 + s))
    assert svc._profiles["alice"].count("neural") >= 3
    # …and now a stranger's neural print is judged by the neural matcher and rejected.
    r = svc.identify(VoicePrint(dsp=_near(_A_DSP, 60), neural=_near(_B_NEU, 60)))
    assert r.name is None


def test_v1_profile_file_loads_as_dsp_only(tmp_path):
    p = tmp_path / "v.json"
    v1 = {
        "version": 1,
        "users": [{
            "name": "Brian",
            "enroll": [[round(float(x), 5) for x in _near(_A_DSP, s)] for s in range(6)],
            "passive": [],
            "notes": "old profile", "created": 1.0, "updated": 1.0,
        }],
    }
    p.write_text(json.dumps(v1), encoding="utf-8")
    svc = VoiceIdService(p)
    assert svc.names() == ["Brian"]
    assert svc._profiles["brian"].count("dsp") == 6
    assert svc._profiles["brian"].count("neural") == 0
    assert svc.identify(VoicePrint(dsp=_near(_A_DSP, 40))).name == "Brian"


def test_two_neural_profiles_separate(tmp_path):
    svc = VoiceIdService(tmp_path / "v.json")
    svc.register("Alice", [_print(_A_DSP, _A_NEU, s) for s in range(6)])
    svc.register("Bob", [_print(_B_DSP, _B_NEU, s) for s in range(6)])
    assert svc.identify(_print(_A_DSP, _A_NEU, 70)).name == "Alice"
    assert svc.identify(_print(_B_DSP, _B_NEU, 70)).name == "Bob"


def test_mature_owner_beats_young_lax_profile(tmp_path):
    # Winner is chosen by RAW SCORE within a backend, never by headroom-above-bar: a mature profile
    # (high self-calibrated bar, small lead) must still beat a young profile with a laxer bar that
    # the utterance under-scores.
    svc = VoiceIdService(tmp_path / "v.json")
    svc.register("Owner", [VoicePrint(dsp=_near(_A_DSP, s, wobble=0.05)) for s in range(20)])
    # A young stranger-ish profile in a direction moderately similar to the utterance
    mid = (_A_DSP * 0.75 + _B_DSP * 0.65)
    mid = mid / np.linalg.norm(mid)
    svc.register("Newbie", [VoicePrint(dsp=_near(mid, s)) for s in range(6)])
    r = svc.identify(VoicePrint(dsp=_near(_A_DSP, 90, wobble=0.05)))
    assert r.name == "Owner"


def test_any_near_tie_rival_forces_refusal_not_just_the_second(tmp_path):
    # The too-close-to-call margin must consider EVERY same-backend rival: an unrelated third
    # profile must never mask a near-tie between the top two.
    svc = VoiceIdService(tmp_path / "v.json")
    base = _near(_A_DSP, 0, wobble=0.02)
    svc.register("Twin1", [VoicePrint(dsp=_near(base, s, wobble=0.03)) for s in range(6)])
    svc.register("Twin2", [VoicePrint(dsp=_near(base, 100 + s, wobble=0.03)) for s in range(6)])
    svc.register("Other", [VoicePrint(dsp=_near(_B_DSP, s)) for s in range(6)])
    r = svc.identify(VoicePrint(dsp=_near(base, 500, wobble=0.03)))
    assert r.name is None  # twins are indistinguishable — refuse rather than guess


def test_neural_acceptance_outranks_dsp_in_mixed_households(tmp_path):
    # Upgrade-transition: Alice has a neural-rich profile; Bob is still DSP-only and his DSP samples
    # sit close to Alice's voice. Alice speaking must resolve to Alice via the neural match, even if
    # Bob's loose DSP bar also accepts her utterance.
    svc = VoiceIdService(tmp_path / "v.json")
    svc.register("Alice", [_print(_A_DSP, _A_NEU, s) for s in range(8)])
    svc.register("Bob", [VoicePrint(dsp=_near(_A_DSP, 300 + s, wobble=0.12)) for s in range(6)])
    r = svc.identify(_print(_A_DSP, _A_NEU, 99))
    assert r.name == "Alice"


def test_bare_vectors_still_accepted_everywhere(tmp_path):
    # The compatibility shim: raw arrays are DSP prints (older tests + controller seams rely on it).
    svc = VoiceIdService(tmp_path / "v.json")
    svc.register("Alice", [_near(_A_DSP, s) for s in range(6)])
    r = svc.identify(_near(_A_DSP, 90))
    assert isinstance(r, MatchResult) and r.name == "Alice"


# ----- the real model, when its file is present on this machine -----
_MODEL = Path(__file__).resolve().parent.parent / "data" / "models" / "speaker_campplus.onnx"

_SUBPROC_CHECK = r"""
import math, sys
from pathlib import Path
import numpy as np
from helix.adapters import speaker_embed

assert speaker_embed.prewarm(Path(sys.argv[1])), "prewarm failed"

def voice(f0, formants, seed):
    rng = np.random.default_rng(seed)
    t = np.arange(32000) / 16000.0
    x = np.zeros_like(t)
    for k in range(1, int(7000 / f0)):
        freq = k * f0
        gain = sum(math.exp(-(((freq - fm) / 220.0) ** 2)) for fm in formants) + 0.02
        x += gain * np.sin(2 * np.pi * freq * t + float(rng.uniform(0, 2 * np.pi)))
    x += 0.01 * rng.standard_normal(t.size)
    x = x / np.abs(x).max() * 0.6
    return (x * 32767).astype("<i2").tobytes()

a1 = speaker_embed.embed(voice(205.0, (850.0, 2100.0, 3000.0), 1))
a2 = speaker_embed.embed(voice(205.0, (850.0, 2100.0, 3000.0), 2))
b1 = speaker_embed.embed(voice(105.0, (500.0, 1300.0, 2400.0), 1))
assert a1 is not None and a2 is not None and b1 is not None, "embed returned None"
assert a1.shape == (512,), a1.shape
assert abs(float(np.linalg.norm(a1)) - 1.0) < 1e-4
same, cross = float(a1 @ a2), float(a1 @ b1)
assert same > 0.9, f"same-voice too low: {same}"
assert same > cross, f"same {same} not above cross {cross}"
print("REAL-MODEL-OK")
"""


@pytest.mark.skipif(not _MODEL.exists(), reason="CAM++ model not downloaded on this machine")
def test_real_model_chain_produces_consistent_embeddings():
    # In a CLEAN subprocess: onnxruntime's DLLs refuse to initialize once Qt is loaded, and pytest's
    # collection imports Qt-using test modules first. The app has the same constraint — that is why
    # main.py pre-warms the speaker model BEFORE any PyQt6 import (same rule as whisper/ctranslate2).
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", _SUBPROC_CHECK, str(_MODEL.parent)],
        capture_output=True, text=True, timeout=180,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "REAL-MODEL-OK" in proc.stdout
