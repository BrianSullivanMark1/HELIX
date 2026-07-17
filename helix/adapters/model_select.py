"""Model resolver — HELIX grows on the strongest brain Anthropic offers (READ_ME/BRAIN.md, Growth).

Everyday conversation runs on a fast model. But GROWTH reasoning — the deep reasoner (think_harder)
and the nightly Evolve loop, where HELIX rewrites itself — must use the most capable model available.
That is Fable 5 today; when a stronger model in the same line ships (a future Fable 6, or a higher
Opus), HELIX should adopt it AUTOMATICALLY, no code change.

This resolver queries the live Models API (GET /v1/models) and ranks what it finds by family and
version, caching the answer for a day. If the list can't be reached, it falls back to the pinned
`PREFERRED_GROWTH_MODEL`. Pure HTTP via urllib (like connections.call_api) — no SDK dependency, one
FIXED host, GET only, so there is no user-controlled URL and no new egress surface.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.request

from helix.logging_setup import get_logger

_LOG = get_logger("model_select")

# The pinned floor — the strongest generally-available model as of this build. Used verbatim when the
# live list can't be reached, and as the minimum the resolver will return.
PREFERRED_GROWTH_MODEL = "claude-fable-5"

_MODELS_URL = "https://api.anthropic.com/v1/models?limit=100"
_ANTHROPIC_VERSION = "2023-06-01"
_CACHE_TTL_S = 86_400.0  # a day — model lineups change on the order of months, not minutes

# Family rank: which LINE is the strongest reasoning brain. Higher wins. Mythos/Fable are the top tier
# (above Opus); a future family name unknown here ranks 0 and never displaces a known top model, so an
# unrelated new id can't accidentally capture growth. Update this tuple when a new TOP tier appears.
_FAMILY_RANK: dict[str, int] = {
    "mythos": 5,
    "fable": 4,
    "opus": 3,
    "sonnet": 2,
    "haiku": 1,
}

# id → (family, version-tuple). "claude-fable-5" → ("fable", (5,)); "claude-opus-4-8" → ("opus",(4,8)).
_ID_RE = re.compile(r"^claude-([a-z]+)-(\d+(?:[.-]\d+)*)", re.IGNORECASE)


def _parse(model_id: str) -> tuple[str, tuple[int, ...]] | None:
    m = _ID_RE.match((model_id or "").strip().lower())
    if not m:
        return None
    family = m.group(1)
    version = tuple(int(p) for p in re.split(r"[.-]", m.group(2)) if p.isdigit())
    return family, version


def _rank(model_id: str) -> tuple[int, tuple[int, ...]]:
    """Sort key: (family rank, version). Bigger is stronger. An unknown family ranks 0 so it never
    outranks a known top-tier model."""
    parsed = _parse(model_id)
    if parsed is None:
        return (0, ())
    family, version = parsed
    return (_FAMILY_RANK.get(family, 0), version)


def best_growth_model(candidate_ids: list[str]) -> str:
    """The strongest id among `candidate_ids`, but never weaker than the pinned floor — so a fresh
    install or a stripped-down list still grows on at least Fable 5. Adopts a future Fable 6 / higher
    Opus automatically because its (family, version) sorts above the current top."""
    best = PREFERRED_GROWTH_MODEL
    best_key = _rank(best)
    for mid in candidate_ids or []:
        key = _rank(mid)
        if key > best_key:
            best, best_key = mid, key
    return best


class GrowthModelResolver:
    """Resolves the growth-reasoning model id, caching the live lookup. `key_fn` returns the current
    Anthropic API key (may be None); with no key the pinned floor is used."""

    def __init__(self, key_fn, clock=None) -> None:
        self._key_fn = key_fn
        self._clock = clock  # optional Clock (monotonic-ish via .now().timestamp()); None → time.monotonic
        self._lock = threading.Lock()
        self._cached: str | None = None
        self._fetched_at = 0.0

    def _now(self) -> float:
        if self._clock is not None:
            try:
                return self._clock.now().timestamp()
            except Exception:  # noqa: BLE001
                pass
        import time
        return time.monotonic()

    def resolve(self) -> str:
        """The growth model id — cached for a day, pinned floor on any failure. Never raises."""
        with self._lock:
            if self._cached is not None and (self._now() - self._fetched_at) < _CACHE_TTL_S:
                return self._cached
        model = self._fetch_best()
        with self._lock:
            self._cached = model
            self._fetched_at = self._now()
        return model

    def _fetch_best(self) -> str:
        key = ""
        try:
            key = (self._key_fn() or "").strip()
        except Exception:  # noqa: BLE001
            key = ""
        if not key:
            return PREFERRED_GROWTH_MODEL  # no key → the pinned floor (the subscription path uses this too)
        try:
            req = urllib.request.Request(
                _MODELS_URL,
                headers={
                    "x-api-key": key,
                    "anthropic-version": _ANTHROPIC_VERSION,
                    "User-Agent": "HELIX",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - fixed https host, GET only
                payload = json.loads(r.read(2_000_000).decode("utf-8", "replace"))
            ids = [m.get("id", "") for m in (payload.get("data") or []) if isinstance(m, dict)]
            best = best_growth_model(ids)
            _LOG.info("growth model resolved to %s (from %d available)", best, len(ids))
            return best
        except Exception as exc:  # noqa: BLE001 — offline / bad key / API hiccup → the pinned floor
            _LOG.info("model list unavailable (%s); growth stays on %s", exc, PREFERRED_GROWTH_MODEL)
            return PREFERRED_GROWTH_MODEL
