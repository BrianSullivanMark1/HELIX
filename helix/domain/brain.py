"""The HELIX brain — the biomimetic cognitive stack, as pure data + logic (see READ_ME/BRAIN.md).

This module owns two things, both dependency-free so any layer may import them:
  1. THE LAYERS — the named cognitive stack (brainstem → thalamus → limbic → cortex → growth), so the
     rest of the code (and HELIX itself, reasoning about its own mind) has one vocabulary for it.
  2. THE THALAMIC GATE — is a heard utterance ADDRESSED to HELIX, or is HELIX merely being talked
     ABOUT? This is the cocktail-party / own-name-detection principle: the wake word LEADING an
     utterance (a salutation + name up front) breaks through as "directed at me" even in a long
     sentence ("good morning HELIX, how you doing"), while the name buried mid-sentence as a topic
     ("the wake word is HELIX") is ambient and does not address it.

Pure: no I/O, no Qt, no model. The voice layer and the conversation layer both consume it.
"""
from __future__ import annotations

import re
from enum import Enum


class Layer(Enum):
    """The cognitive stack, fast/fixed low → slow/flexible high (READ_ME/BRAIN.md)."""

    BRAINSTEM = "brainstem"   # reflexes + arousal (sleep/wake) — instant, no model
    THALAMUS = "thalamus"     # attention gating — is this addressed to me?
    LIMBIC = "limbic"         # salience + self-state (interoception) — per-turn context
    CORTEX = "cortex"         # reasoning + language + judgment — the model turn
    GROWTH = "growth"         # consolidation + plasticity — nightly, strongest model


# Openers that, at the START of an utterance, mark it as SPOKEN TO HELIX — a greeting or a direct
# address. "good morning HELIX", "hey HELIX", "how you doing HELIX". These are the high-salience
# addressing cues (the own-name / cocktail-party breakthrough). The key is they LEAD the utterance:
# a greeting up front + the name soon after = talking to it; the same name buried after ordinary
# words ("the wake word is HELIX") is talking about it.
_OPENER = re.compile(
    r"^(?:good\s*(?:morning|afternoon|evening|day)|morning|afternoon|evening|"
    r"hey|hi|hello|yo|greetings|are\s+you\s+(?:there|awake|up|around)|you\s+there|"
    r"how\s+(?:are\s+you|you\s+doing|goes\s+it|is\s+it\s+going)|what'?s\s+up|wake\s+up)\b",
    re.IGNORECASE,
)
# A couple of discourse fillers that may sit before a genuine greeting ("well hello there HELIX") and
# should be skipped — but NOT greetings themselves, so "so anyway" (pure filler, no greeting) still
# fails to open.
_LEAD_FILLER = re.compile(r"^(?:well|oh|um+|uh+|okay|ok|so)\s+", re.IGNORECASE)

# When an opener leads, the name must still arrive reasonably soon — a greeting followed much later by
# the name in a different clause ("how are you going to explain HELIX to them") is not an address.
_OPENER_NAME_WINDOW = 5


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def is_addressed(text: str, wake_re) -> bool:
    """Thalamic gate: True when `text` is spoken TO HELIX (should reach the cortex / wake it), False
    when HELIX is merely mentioned. `wake_re` is the configured wake-word matcher (voice.build_wake_re).

    Addressed when ANY holds:
      - the utterance is short (≤3 words) and contains the name — it IS the address ("HELIX?", "hey HELIX");
      - the name LEADS the utterance (it is the first word) — "HELIX, build me a timer";
      - the utterance OPENS with a greeting/address and the name arrives soon after — "good morning
        HELIX, how you doing", "well hello there HELIX".
    NOT addressed when the name only appears after ordinary words with no opening greeting — "the wake
    word is HELIX", "so anyway HELIX can build apps", "we should tell them HELIX exists" — speech ABOUT
    HELIX, not TO it.
    """
    if wake_re is None or not (text or "").strip():
        return False
    words = _words(text)
    if not words:
        return False
    name_positions = [i for i, w in enumerate(words) if wake_re.search(w)]
    if not name_positions:
        return False
    if len(words) <= 3:
        return True
    if name_positions[0] == 0:  # the name is the very first word — a bare address
        return True
    head = _LEAD_FILLER.sub("", (text or "").strip().lower(), count=1)
    return bool(_OPENER.match(head)) and name_positions[0] <= _OPENER_NAME_WINDOW


def is_wake_utterance(text: str, wake_re, wake_phrase_fn) -> bool:
    """The full wake test used while HELIX is asleep: an explicit wake PHRASE ("wake up", "mic on" —
    supplied by the voice layer via `wake_phrase_fn`) OR the utterance being ADDRESSED to HELIX. Sleep
    means sleep: a mentioned name never wakes. `wake_phrase_fn` is voice.is_wake (kept in the voice
    layer with the rest of the phrase grammar); this composes it with the thalamic gate."""
    if wake_phrase_fn is not None and wake_phrase_fn(text):
        return True
    return is_addressed(text, wake_re)
