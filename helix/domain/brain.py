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
# Discourse fillers that may sit before a genuine greeting ("well hello there HELIX") and should be
# skipped — but NOT greetings themselves, so "so anyway" (pure filler, no greeting) still fails to
# open. STT loves inserting commas after these, so trailing punctuation is absorbed with the filler.
_LEAD_FILLER = re.compile(r"^(?:well|oh|um+|uh+|okay|ok|so|please)(?:[,.!;:]+\s*|\s+)", re.IGNORECASE)

# CARRIER words that may precede the name and still leave it LEADING: the wake grammar's own spoken
# carriers (hey/ok/okay) plus the imperative marker "please" — "okay, HELIX, do it" and "please
# HELIX, turn it down" address exactly like "HELIX, ...". Deliberately NOT the narrative fillers
# (so/well/oh/um): "so HELIX built me an app" is a report ABOUT it, and song lyrics love leading
# vocals with "oh" — admitting those would wake HELIX on mentions and let music through the
# playback gate.
_CARRIERS = frozenset({"hey", "ok", "okay", "please"})

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
      - the name LEADS the utterance, allowing carrier words before it — "HELIX, build me a timer",
        "okay, HELIX, do it", "please HELIX, turn it down";
      - the utterance OPENS with a greeting/address and the name arrives soon after — "good morning
        HELIX, how you doing", "well hello there HELIX".
    NOT addressed when the name only appears after ordinary words with no opening greeting — "the wake
    word is HELIX", "so anyway HELIX can build apps", "so HELIX built me an app", "we should tell them
    HELIX exists" — speech ABOUT HELIX, not TO it. Narrative leads ("so/well/oh HELIX ...") are NOT
    carriers, exactly so mentions and sung "oh HELIX ..." lines stay unaddressed.
    """
    if wake_re is None or not (text or "").strip():
        return False
    words = _words(text)
    if not words:
        return False
    if len(words) <= 3 and wake_re.search(" ".join(words)):
        return True  # a short utterance that IS the address ("HELIX?", "hey HELIX")
    return is_directly_addressed(text, wake_re)


def is_directly_addressed(text: str, wake_re) -> bool:
    """The STRICT address test — is_addressed WITHOUT the short-utterance benefit of the doubt: the
    name LEADS the utterance (at most carrier words — hey/okay/please — before it), or a greeting
    opens it with the name close behind. Used where the benefit of the doubt is dangerous: judging
    speech heard while the machine itself is playing audio, where a hotword-biased STT can fish the
    name out of a short music fragment ("my HELIX baby") that must NOT count as an address.

    The name is matched on JOINED words (not per-token), so a multi-word custom wake word ("red
    queen") addresses too; and a carrier is never skipped when it IS the configured name (a wake word
    of "Okay" stays the lead)."""
    if wake_re is None:
        return False
    words = _words(text)
    if not words or not wake_re.search(" ".join(words)):
        return False
    lead = list(words)
    while len(lead) > 1 and lead[0] in _CARRIERS and not wake_re.search(lead[0]):
        lead.pop(0)
    if wake_re.match(" ".join(lead)):
        return True  # the name (with at most carriers before it) leads the utterance
    head = _LEAD_FILLER.sub("", (text or "").strip().lower(), count=1)
    if not _OPENER.match(head):
        return False
    # A greeting opens it — the name must still arrive within the first few words.
    return bool(wake_re.search(" ".join(_words(head)[: _OPENER_NAME_WINDOW + 1])))


def is_wake_utterance(text: str, wake_re, wake_phrase_fn) -> bool:
    """The full wake test used while HELIX is asleep: an explicit wake PHRASE ("wake up", "mic on" —
    supplied by the voice layer via `wake_phrase_fn`) OR the utterance being ADDRESSED to HELIX. Sleep
    means sleep: a mentioned name never wakes. `wake_phrase_fn` is voice.is_wake (kept in the voice
    layer with the rest of the phrase grammar); this composes it with the thalamic gate."""
    if wake_phrase_fn is not None and wake_phrase_fn(text):
        return True
    return is_addressed(text, wake_re)
