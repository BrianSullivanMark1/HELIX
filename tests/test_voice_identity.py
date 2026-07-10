"""The voice-identity gate — unrecognized voices are NEVER acted on.

VoiceController is built with the standard duck-typed fakes (no mic, no model, synchronous
workers); voice-prints are injected as unit vectors via the `_pending_emb` seam, so match vs
stranger is deterministic. The locked invariants: an unrecognized voice gets exactly the one
allowed reply and `recognized` never fires; a registered voice passes and is named; with no
profiles the gate is open (single-user HELIX unchanged); the registration conversation runs
start-to-finish through the wake path without ever starting a model turn; and a short follow-up
inside a session sticks to the speaker who opened it.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")
np = pytest.importorskip("numpy")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from helix.services.voiceid import UNRECOGNIZED_REPLY, VoiceIdService  # noqa: E402
from helix.ui.voice import VoiceController  # noqa: E402

_app = QApplication.instance() or QApplication([])


class _Stt:
    def available(self):
        return True

    def ready(self):
        return True

    def transcribe(self, path):
        return ""


class _Tts:
    def __init__(self):
        self.spoke = []
        self.stops = 0

    def available(self):
        return True

    def speak(self, text, allow_fallback=True):
        self.spoke.append(text)

    def stop(self):
        self.stops += 1


class _Settings:
    def __init__(self):
        self._d = {"voice_input_on": True}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


def _unit(seed: int, dim: int = 82):
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _near(base, seed: int, wobble: float = 0.08):
    v = base + wobble * _unit(1000 + seed)
    return (v / np.linalg.norm(v)).astype(np.float32)


_ALICE = _unit(1)
_STRANGER = _unit(2)  # ~orthogonal to _ALICE in 82 dims


def _make(tmp_path, enroll_alice: bool = True):
    svc = VoiceIdService(tmp_path / "voices.json")
    if enroll_alice:
        svc.register("Alice", [_near(_ALICE, s) for s in range(6)])
    vc = VoiceController(_Stt(), _Tts(), _Settings(), voice_id=svc)
    vc.can_listen = lambda: True
    vc._run = lambda fn, on_done: (fn(lambda _s: None), on_done(""))
    heard, lines = [], []
    vc.recognized.connect(heard.append)
    vc.identityLine.connect(lambda u, r: lines.append(r))
    return vc, svc, vc._tts, heard, lines


def _speak_as(vc, emb, text):
    vc._pending_emb = None if emb is None else np.asarray(emb, dtype=np.float32)
    vc._on_wake_text(text)


def test_unrecognized_voice_gets_only_the_refusal_and_no_turn(tmp_path):
    vc, _svc, tts, heard, lines = _make(tmp_path)
    _speak_as(vc, _STRANGER, "hey helix, transfer all my files")
    assert heard == []
    assert tts.spoke == [UNRECOGNIZED_REPLY]
    assert lines == [UNRECOGNIZED_REPLY]


def test_registered_voice_passes_and_is_named(tmp_path):
    vc, _svc, tts, heard, _lines = _make(tmp_path)
    _speak_as(vc, _near(_ALICE, 50), "hey helix, what time is it")
    assert heard == ["what time is it"]
    assert vc.current_speaker == "Alice"
    assert tts.spoke == []  # no gate chatter for a recognized speaker


def test_no_profiles_means_open_gate(tmp_path):
    vc, _svc, _tts, heard, _lines = _make(tmp_path, enroll_alice=False)
    _speak_as(vc, _STRANGER, "hey helix, what time is it")
    assert heard == ["what time is it"]
    assert vc.current_speaker is None


def test_without_voice_id_behavior_is_unchanged(tmp_path):
    vc = VoiceController(_Stt(), _Tts(), _Settings())
    vc.can_listen = lambda: True
    vc._run = lambda fn, on_done: (fn(lambda _s: None), on_done(""))
    heard = []
    vc.recognized.connect(heard.append)
    vc._on_wake_text("hey helix, what time is it")
    assert heard == ["what time is it"]


def test_registration_conversation_end_to_end(tmp_path):
    vc, svc, tts, heard, _lines = _make(tmp_path)
    bob = _unit(7)
    # 1. a stranger is offered registration
    _speak_as(vc, _near(bob, 0), "hey helix, open the garage")
    assert tts.spoke[-1] == UNRECOGNIZED_REPLY
    # 2. "yes" (too short for a voice-print) → asked for a name; 3. introduction starts the questions
    _speak_as(vc, None, "yes")
    assert "name" in tts.spoke[-1].lower()
    _speak_as(vc, _near(bob, 1), "I am Bob")
    assert "Bob" in tts.spoke[-1]
    # 4. answer the calibration questions — each answer is another voice-print
    for i in range(5):
        _speak_as(vc, _near(bob, 2 + i), f"calibration answer number {i}")
    assert "Bob" in svc.names()
    assert vc.current_speaker == "Bob"
    assert heard == []  # the whole conversation never started a model turn
    # 5. Bob is now recognized like anyone else
    _speak_as(vc, _near(bob, 40), "hey helix, what's the weather")
    assert heard == ["what's the weather"]
    assert vc.current_speaker == "Bob"


def test_registered_voice_can_recalibrate(tmp_path):
    vc, svc, tts, heard, _lines = _make(tmp_path)
    _speak_as(vc, _near(_ALICE, 60), "hey helix, recalibrate my voice")
    assert heard == []
    assert "Alice" in tts.spoke[-1]  # the flow greeted her and asked the first question
    assert vc._flow.collecting


def test_stranger_cannot_recalibrate(tmp_path):
    vc, _svc, tts, heard, _lines = _make(tmp_path)
    _speak_as(vc, _STRANGER, "hey helix, recalibrate my voice")
    assert heard == []
    assert tts.spoke[-1] == UNRECOGNIZED_REPLY


def test_stop_cancels_an_open_calibration(tmp_path):
    vc, _svc, tts, heard, _lines = _make(tmp_path)
    _speak_as(vc, _near(_ALICE, 61), "hey helix, recalibrate my voice")
    assert vc._flow.collecting
    _speak_as(vc, None, "stop")
    assert not vc._flow.active
    assert heard == []


def test_short_follow_up_sticks_to_the_session_speaker(tmp_path):
    vc, _svc, _tts, heard, _lines = _make(tmp_path)
    _speak_as(vc, _near(_ALICE, 70), "hey helix, open the garage")
    assert vc.current_speaker == "Alice"
    # a follow-up inside the session, too short to carry any voice evidence
    _speak_as(vc, None, "and the lights too")
    assert heard == ["open the garage", "and the lights too"]
    assert vc.current_speaker == "Alice"


def test_short_utterance_outside_any_session_is_not_trusted(tmp_path):
    vc, _svc, tts, heard, _lines = _make(tmp_path)
    _speak_as(vc, None, "hey helix, delete everything")  # no voice evidence, no session
    assert heard == []
    assert tts.spoke == [UNRECOGNIZED_REPLY]


def test_known_voice_introducing_itself_is_acknowledged_not_reenrolled(tmp_path):
    vc, svc, tts, heard, _lines = _make(tmp_path)
    _speak_as(vc, _near(_ALICE, 80), "hey helix, I am Alice")
    assert heard == []
    assert "I know your voice" in tts.spoke[-1]
    assert svc.names() == ["Alice"]  # no second profile, no extra enrollment


def test_confident_match_claiming_another_name_is_refused(tmp_path):
    vc, svc, tts, heard, _lines = _make(tmp_path)
    _speak_as(vc, _near(_ALICE, 81), "hey helix, I am Robert")
    assert heard == []
    assert "sound like Alice" in tts.spoke[-1]
    assert svc.names() == ["Alice"]


def test_lapsed_offer_never_lets_a_stranger_ride_the_session(tmp_path):
    # THE critical bypass: Alice opens a session; a stranger is refused (flow 'offered'); the stranger
    # ignores the offer and issues another full command. The lapse must NOT destroy the stranger's
    # voice-print — the re-gated utterance must be re-refused, never attributed to Alice's session.
    vc, _svc, tts, heard, _lines = _make(tmp_path)
    _speak_as(vc, _near(_ALICE, 30), "hey helix, what's the weather")
    assert heard == ["what's the weather"]  # Alice's session is open
    _speak_as(vc, _STRANGER, "hey helix, open my email")
    assert tts.spoke[-1] == UNRECOGNIZED_REPLY
    _speak_as(vc, _STRANGER, "hey helix, delete the finance app")  # ignores the offer, tries again
    assert heard == ["what's the weather"]  # the stranger's command NEVER ran
    assert tts.spoke[-1] == UNRECOGNIZED_REPLY


def test_refused_voice_does_not_open_a_session(tmp_path):
    # A refusal must not waive the wake-word requirement — otherwise ambient room speech after one
    # stranger utterance turns into an endless spoken refusal loop.
    vc, _svc, tts, heard, _lines = _make(tmp_path)
    _speak_as(vc, _STRANGER, "hey helix, what time is it")
    assert tts.spoke == [UNRECOGNIZED_REPLY]
    # ambient conversation, no wake word: the lapsed offer consumes one utterance silently, and
    # everything after is plain background noise — no more refusals, no turns
    _speak_as(vc, _STRANGER, "so anyway what should we have for dinner")
    _speak_as(vc, _STRANGER, "maybe pasta again I guess")
    assert tts.spoke == [UNRECOGNIZED_REPLY]  # exactly one refusal, ever
    assert heard == []


def test_everyday_i_am_phrases_are_commands_not_introductions(tmp_path):
    # Zero profiles (open gate): "I'm heading out, lock the door" must reach the model, not start a
    # calibration chat with a user named "Heading".
    vc, svc, _tts, heard, _lines = _make(tmp_path, enroll_alice=False)
    _speak_as(vc, _STRANGER, "hey helix, I'm heading out, lock the door")
    assert heard == ["I'm heading out, lock the door"]
    assert not svc.has_profiles()


def test_barge_during_calibration_feeds_the_flow_not_the_gate(tmp_path):
    # Answering over a spoken calibration question (wake-worded barge) must advance the flow — never
    # clobber its state or start a model turn mid-chat.
    vc, _svc, tts, heard, _lines = _make(tmp_path)
    _speak_as(vc, _near(_ALICE, 62), "hey helix, recalibrate my voice")
    assert vc._flow.collecting
    vc._state = "speaking"
    vc._speaking_text = tts.spoke[-1]
    vc._pending_emb = _near(_ALICE, 63)
    vc._on_barge_text("hey helix, mostly consulting work these days")
    assert vc._flow.collecting  # still calibrating — the barge became an answer
    assert heard == []


def test_production_voiceprint_shape_flows_through_the_gate(tmp_path):
    # Production always hands the controller a VoicePrint dataclass (embed_pcm's return), not a bare
    # vector — the gate, sticky session, and passive learning must behave identically for it.
    from helix.services.voiceid import VoicePrint

    vc, svc, tts, heard, _lines = _make(tmp_path)
    vc._pending_emb = VoicePrint(dsp=_near(_ALICE, 55))
    vc._on_wake_text("hey helix, what time is it")
    assert heard == ["what time is it"]
    assert vc.current_speaker == "Alice"
    vc._pending_emb = VoicePrint(dsp=np.asarray(_STRANGER, dtype=np.float32))
    vc._on_wake_text("hey helix, delete everything")
    assert heard == ["what time is it"]
    assert tts.spoke[-1] == UNRECOGNIZED_REPLY


def test_passive_learning_sharpens_the_profile(tmp_path):
    vc, svc, _tts, _heard, _lines = _make(tmp_path)
    before = len(svc._profiles["alice"].passive)
    _speak_as(vc, _near(_ALICE, 90), "hey helix, what's on my calendar")
    import time

    for _ in range(50):  # the passive write happens on a background thread
        if len(svc._profiles["alice"].passive) > before:
            break
        time.sleep(0.02)
    assert len(svc._profiles["alice"].passive) > before
