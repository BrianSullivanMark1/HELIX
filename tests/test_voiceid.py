"""Voice identity — the invariants that make multi-user voice recognition trustworthy.

No Qt, no mic, no model: utterances are synthesized int16 PCM (harmonic "voices" with distinct
pitch and formants), profiles live in a tmp_path JSON file, and the enrollment conversation is a
pure state machine. The locked invariants: two different synthetic voices are separable and a
registered voice matches itself across fresh utterances; no registered profiles means an open
gate; too-short clips carry no speaker evidence; passive learning is ring-buffered; profiles
survive a save/load round trip; 'recalibrate my voice' triggers only as a command, never inside
a build request; and the calibration conversation registers a profile end-to-end.
"""
from __future__ import annotations

import json
import math

import pytest

np = pytest.importorskip("numpy")

from helix.services.voiceid import (  # noqa: E402
    EnrollmentFlow,
    MIN_VOICED_S,
    UNRECOGNIZED_REPLY,
    VoiceIdService,
    embed_pcm,
    introduction_name,
    wants_recalibration,
    wants_registration,
)

_SR = 16000


def _voice(f0: float, formants: tuple[float, ...], seconds: float = 2.0, seed: int = 0) -> bytes:
    """A synthetic 'speaker': a harmonic series shaped by formant resonances, with per-utterance
    jitter so two clips from the same voice are similar but not identical."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(_SR * seconds)) / _SR
    f0 = f0 * (1.0 + 0.03 * float(rng.standard_normal()))
    x = np.zeros_like(t)
    for k in range(1, int(7000 / f0)):
        freq = k * f0
        gain = sum(math.exp(-(((freq - fm) / 220.0) ** 2)) for fm in formants) + 0.02
        x += gain * np.sin(2 * np.pi * freq * t + float(rng.uniform(0, 2 * np.pi)))
    x += 0.01 * rng.standard_normal(t.size)
    x = x / np.abs(x).max() * 0.6
    return (x * 32767).astype("<i2").tobytes()


_ALICE = dict(f0=205.0, formants=(850.0, 2100.0, 3000.0))
_BOB = dict(f0=105.0, formants=(500.0, 1300.0, 2400.0))


def _alice(seed: int) -> bytes:
    return _voice(seed=seed, **_ALICE)


def _bob(seed: int) -> bytes:
    return _voice(seed=seed, **_BOB)


# ----- the acoustic front end -----

def test_embedding_separates_two_voices():
    a1, a2 = embed_pcm(_alice(1)), embed_pcm(_alice(2))
    b1 = embed_pcm(_bob(1))
    assert a1 is not None and a2 is not None and b1 is not None
    same = float(a1 @ a2)
    cross = float(a1 @ b1)
    assert same > cross + 0.1, f"same-voice {same:.3f} not clearly above cross-voice {cross:.3f}"


def test_embedding_is_unit_norm():
    e = embed_pcm(_alice(3))
    assert abs(float(np.linalg.norm(e)) - 1.0) < 1e-4


def test_short_clip_has_no_speaker_evidence():
    short = _voice(seed=4, seconds=MIN_VOICED_S * 0.5, **_ALICE)
    assert embed_pcm(short) is None


def test_silence_has_no_speaker_evidence():
    assert embed_pcm(b"\x00\x00" * _SR) is None
    assert embed_pcm(b"") is None


def test_broadband_noise_has_no_speaker_evidence():
    # White noise (fans, blenders) is loud but unvoiced — without the periodicity gate two independent
    # noise clips embed to a shared "noise direction" and false-match each other.
    rng = np.random.default_rng(7)
    noise = (rng.standard_normal(_SR * 2) * 12000).clip(-32767, 32767).astype("<i2").tobytes()
    assert embed_pcm(noise) is None


def test_pitch_magnitude_separates_proportional_speakers():
    # Two voices whose (offset, IQR) pitch stats are proportional must NOT collapse to the same pitch
    # feature — per-block L2 on the 2-dim block used to keep only the direction.
    low = embed_pcm(_voice(110.0, (500.0, 1300.0, 2400.0), seed=5))
    high = embed_pcm(_voice(220.0, (500.0, 1300.0, 2400.0), seed=5))
    assert low is not None and high is not None
    assert float(low @ high) < 0.98  # same formants, an octave apart — must not be identical


# ----- profiles + matching -----

def _enrolled_service(tmp_path, name="Alice", seeds=range(10, 16)) -> VoiceIdService:
    svc = VoiceIdService(tmp_path / "voices.json")
    svc.register(name, [embed_pcm(_alice(s)) for s in seeds])
    return svc


def test_no_profiles_means_open_gate(tmp_path):
    svc = VoiceIdService(tmp_path / "voices.json")
    assert not svc.has_profiles()
    r = svc.identify(embed_pcm(_alice(1)))
    assert r.name is None and not r.no_evidence


def test_registered_voice_is_recognized(tmp_path):
    svc = _enrolled_service(tmp_path)
    r = svc.identify(embed_pcm(_alice(99)))  # a fresh utterance, not an enrollment sample
    assert r.name == "Alice"


def test_unregistered_voice_is_rejected(tmp_path):
    svc = _enrolled_service(tmp_path)
    r = svc.identify(embed_pcm(_bob(99)))
    assert r.name is None


def test_no_evidence_clip_is_flagged_not_rejected(tmp_path):
    svc = _enrolled_service(tmp_path)
    r = svc.identify(None)
    assert r.no_evidence and r.name is None


def test_passive_learning_grows_and_ring_buffers(tmp_path):
    svc = _enrolled_service(tmp_path)
    for s in range(100, 160):  # far past the passive cap
        svc.add_passive("Alice", embed_pcm(_alice(s)))
    raw = json.loads((tmp_path / "voices.json").read_text())
    user = raw["users"][0]
    assert 0 < len(user["passive"]) <= 48
    # sharpened profile still recognizes a fresh utterance
    assert svc.identify(embed_pcm(_alice(200))).name == "Alice"


def test_profiles_survive_reload(tmp_path):
    _enrolled_service(tmp_path)
    svc2 = VoiceIdService(tmp_path / "voices.json")
    assert svc2.names() == ["Alice"]
    assert svc2.identify(embed_pcm(_alice(50))).name == "Alice"


def test_corrupt_profile_file_starts_empty(tmp_path):
    p = tmp_path / "voices.json"
    p.write_text("{not json", encoding="utf-8")
    svc = VoiceIdService(p)
    assert not svc.has_profiles()


def test_two_registered_voices_both_recognized(tmp_path):
    svc = _enrolled_service(tmp_path)
    svc.register("Bob", [embed_pcm(_bob(s)) for s in range(10, 16)])
    assert svc.identify(embed_pcm(_alice(77))).name == "Alice"
    assert svc.identify(embed_pcm(_bob(77))).name == "Bob"


def test_remove_profile(tmp_path):
    svc = _enrolled_service(tmp_path)
    assert svc.remove("alice")  # case-insensitive
    assert not svc.has_profiles()


def test_notes_round_trip(tmp_path):
    svc = _enrolled_service(tmp_path)
    svc.set_notes("Alice", "Runs the marketing team; mornings start with a run.")
    assert "marketing" in svc.notes_for("Alice")
    assert svc.notes_for(None) == ""
    assert svc.notes_for("Nobody") == ""


# ----- trigger phrases -----

def test_recalibration_phrases():
    assert wants_recalibration("recalibrate my voice")
    assert wants_recalibration("HELIX, please recalibrate my voice now")
    assert wants_recalibration("run a voice recalibration")
    assert not wants_recalibration("build a voice recalibration app")
    assert not wants_recalibration("make me a recalibrate my voice button")
    assert not wants_recalibration("what's the weather")
    assert not wants_recalibration("")


def test_registration_phrases():
    assert wants_registration("register my voice")
    assert wants_registration("can you register me")
    assert not wants_registration("build a voice registration screen")


def test_introduction_names():
    assert introduction_name("Hey, I am Brian") == "Brian"
    assert introduction_name("i'm Sarah, nice to meet you") == "Sarah"
    assert introduction_name("my name is Marcus") == "Marcus"
    assert introduction_name("I am José") == "José"  # names aren't ASCII-only
    # Everyday sentences must NEVER read as introductions — they are commands or chatter.
    assert introduction_name("I am absolutely sure the build failed") is None
    assert introduction_name("I'm heading out, lock the door") is None
    assert introduction_name("it's raining, close the windows") is None
    assert introduction_name("this is broken, fix the login page") is None
    assert introduction_name("I'm sorry about that") is None
    assert introduction_name("open the garage") is None
    assert introduction_name("") is None


# ----- the enrollment conversation -----

def _fake_emb(seed: int):
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(82).astype(np.float32)
    return v / np.linalg.norm(v)


def test_enrollment_full_flow_registers(tmp_path):
    svc = VoiceIdService(tmp_path / "voices.json")
    flow = EnrollmentFlow(svc)
    assert flow.offer() == UNRECOGNIZED_REPLY
    line = flow.handle("yes please", None)  # a short 'yes' carries no voice evidence — still accepted
    assert "name" in line.lower()
    line = flow.handle("I am Brian", _fake_emb(1))
    assert "Brian" in line and flow.collecting
    answers = [
        "I run a company called Mark One.", "Mostly strategy and hiring.",
        "A small team of eight.", "Coffee first, then email.", "Keep my calendar straight.",
    ]
    for i, ans in enumerate(answers):
        line = flow.handle(ans, _fake_emb(2 + i))
        assert line is not None
    assert not flow.active
    assert svc.names() == ["Brian"]
    assert flow.last_registered == "Brian"
    assert len(flow.last_answers) == 5


def test_enrollment_direct_introduction_skips_offer(tmp_path):
    svc = VoiceIdService(tmp_path / "voices.json")
    flow = EnrollmentFlow(svc)
    flow.offer()
    line = flow.handle("Hey, I am Kate", _fake_emb(1))
    assert "Kate" in line and flow.collecting


def test_enrollment_declined(tmp_path):
    svc = VoiceIdService(tmp_path / "voices.json")
    flow = EnrollmentFlow(svc)
    flow.offer()
    line = flow.handle("no thanks", None)
    assert not flow.active
    assert not svc.has_profiles()
    assert line  # a polite close, not silence


def test_enrollment_unrelated_speech_falls_through(tmp_path):
    svc = VoiceIdService(tmp_path / "voices.json")
    flow = EnrollmentFlow(svc)
    flow.offer()
    assert flow.handle("turn on the kitchen lights", _fake_emb(1)) is None
    assert not flow.active  # the offer lapsed; the caller re-gates the utterance


def test_enrollment_quiet_answer_asks_again(tmp_path):
    svc = VoiceIdService(tmp_path / "voices.json")
    flow = EnrollmentFlow(svc)
    flow.start("Brian", _fake_emb(1))
    line = flow.handle("mm", None)  # spoke, but no usable voice-print
    assert "again" in line.lower() or "catch" in line.lower()
    assert flow.collecting  # still on the same question


def test_enrollment_cannot_register_without_voice_prints(tmp_path):
    # Print-less utterances must never burn questions into a 1-print profile — the flow re-asks, and
    # even a directly-driven finish refuses to register below the minimum.
    svc = VoiceIdService(tmp_path / "voices.json")
    flow = EnrollmentFlow(svc)
    flow.start("Brian", _fake_emb(1))
    for _ in range(8):
        flow.handle("", None)  # transcription produced nothing usable, eight times
    assert flow.collecting  # still asking — questions were not silently burned
    assert not svc.has_profiles()


def test_offered_state_ignores_conversational_openers(tmp_path):
    # "okay so as I was saying…" opens with an affirmation word but is NOT a yes to the offer.
    svc = VoiceIdService(tmp_path / "voices.json")
    flow = EnrollmentFlow(svc)
    flow.offer()
    assert flow.handle("okay so as I was saying the numbers look fine", None) is None
    assert not flow.active


def test_naming_state_lapses_on_ordinary_speech(tmp_path):
    # The naming prompt must not trap the room: a full sentence that isn't a name lapses the flow
    # instead of enrolling its first word ("What?" → user 'What').
    svc = VoiceIdService(tmp_path / "voices.json")
    flow = EnrollmentFlow(svc)
    flow.ask_name()
    assert flow.handle("turn the kitchen lights off please", None) is None
    assert not flow.active
    assert not svc.has_profiles()


def test_recalibration_extends_existing_profile(tmp_path):
    svc = _enrolled_service(tmp_path, name="Brian", seeds=range(10, 16))
    flow = EnrollmentFlow(svc)
    flow.start("Brian", recal=True)
    for i in range(4):
        line = flow.handle(f"answer number {i}", _fake_emb(40 + i))
        if not flow.active:
            break
    assert not flow.active
    raw = json.loads((tmp_path / "voices.json").read_text())
    assert len(raw["users"][0]["enroll"]) > 6  # refreshed prints appended
