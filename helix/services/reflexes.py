"""ReflexService — the growth layer's consolidation store (READ_ME/BRAIN.md).

A judgment the cortex (the model) makes repeatedly should migrate into a fast brainstem reflex —
"neurons that fire together wire together," then the pathway myelinates and runs without the cortex.
Here: when the model judges a spoken phrase to be a genuine sleep request (the go_to_sleep tool), that
phrase is CONSOLIDATED into a learned reflex. Next time HELIX hears it (addressed to it), the voice
layer fires instantly — no model call. The cortex taught the brainstem.

Developmental plasticity: reflexes are capped and prunable. New ones over the cap evict the
least-recently-fired (unused connections are pruned), so the reflex set stays lean and
experience-fitted rather than growing without bound. Whole-utterance + addressed-only by construction
(the voice layer only tests the command portion), so a consolidated reflex can never fire from ambient
speech.

Pure-ish: one small JSON store (guard-safe like reminders/agents), no Qt, no model.
"""
from __future__ import annotations

import re
import threading

from helix.logging_setup import get_logger

_LOG = get_logger("reflexes")

_MAX_PER_KIND = 40      # cap per reflex kind; over this, the least-recently-fired is pruned
_PHRASE_CAP = 80        # chars — a learned phrase is a short command, never a paragraph
_MIN_WORDS = 1
_MAX_WORDS = 8          # a genuine spoken command is short; longer "sleep" mentions never consolidate


def _norm(text: str) -> str:
    """Normalize a phrase to its comparable core: lowercased, punctuation→space, wake-word-ish and
    filler openers dropped, collapsed. Kept deliberately close to voice._clean_command so a learned
    reflex matches the same way the model's trigger utterance was heard."""
    t = re.sub(r"[.!,?\-_]+", " ", (text or "").lower())
    t = re.sub(r"\b(?:um+|uh+|okay|ok|please|yeah|yep|hey|helix|now|just|like|so|well)\b", " ", t)
    return " ".join(t.split())


class ReflexService:
    def __init__(self, store) -> None:
        self._store = store       # JsonSettings: {kind: [{"phrase":…, "count":int, "last":int}]}
        self._lock = threading.Lock()
        self._clock = 0           # a monotonic "recency" tick; no wall-clock dependency

    # ----- read side: the brainstem check (called by the voice layer, pre-model) -----
    def matches(self, text: str, kind: str = "sleep") -> bool:
        """True if `text` is a learned reflex of this kind — a whole-utterance match against a
        consolidated phrase. Bumps its recency so frequently-fired reflexes survive pruning."""
        core = _norm(text)
        if not core:
            return False
        with self._lock:
            reflexes = self._read(kind)
            for r in reflexes:
                if r.get("phrase") == core:
                    self._clock += 1
                    r["count"] = int(r.get("count", 0)) + 1
                    r["last"] = self._clock
                    self._write(kind, reflexes)
                    return True
        return False

    # ----- write side: consolidation (called by the conversation layer after a cortical judgment) -----
    def learn(self, text: str, kind: str = "sleep") -> bool:
        """Consolidate `text` into a fast reflex of this kind. No-op for anything not command-shaped
        (empty, too long, too many words — those were never a crisp command). Returns True if a NEW
        reflex was formed."""
        core = _norm(text)
        if not core:
            return False
        n = len(core.split())
        if not (_MIN_WORDS <= n <= _MAX_WORDS) or len(core) > _PHRASE_CAP:
            return False
        with self._lock:
            reflexes = self._read(kind)
            for r in reflexes:  # already known — just strengthen it
                if r.get("phrase") == core:
                    self._clock += 1
                    r["count"] = int(r.get("count", 0)) + 1
                    r["last"] = self._clock
                    self._write(kind, reflexes)
                    return False
            self._clock += 1
            reflexes.append({"phrase": core, "count": 1, "last": self._clock})
            # Developmental pruning: over the cap, drop the least-recently-fired (unused connection).
            if len(reflexes) > _MAX_PER_KIND:
                reflexes.sort(key=lambda r: int(r.get("last", 0)))
                reflexes = reflexes[-_MAX_PER_KIND:]
            self._write(kind, reflexes)
            _LOG.info("consolidated a %s reflex (%d total)", kind, len(reflexes))
            return True

    def phrases(self, kind: str = "sleep") -> list[str]:
        with self._lock:
            return [r.get("phrase", "") for r in self._read(kind) if r.get("phrase")]

    # ----- store plumbing -----
    def _read(self, kind: str) -> list[dict]:
        try:
            data = self._store.get("reflexes") or {}
            got = data.get(kind) or []
            return [dict(r) for r in got if isinstance(r, dict) and r.get("phrase")]
        except Exception:  # noqa: BLE001
            return []

    def _write(self, kind: str, reflexes: list[dict]) -> None:
        try:
            data = dict(self._store.get("reflexes") or {})
            data[kind] = reflexes
            self._store.set("reflexes", data)
        except Exception:  # noqa: BLE001
            _LOG.warning("could not save reflexes", exc_info=True)
