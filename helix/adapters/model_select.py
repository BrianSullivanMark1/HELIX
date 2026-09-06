"""Model resolver — HELIX grows on the strongest brain Anthropic offers (READ_ME/BRAIN.md, Growth).

Everyday conversation runs on a fast model. But GROWTH reasoning — the deep reasoner (think_harder)
and the nightly dream session, where HELIX rewrites itself — must use the most capable model available.
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never auto-follow a redirect on the authenticated model-list request — urllib would otherwise
    re-send the x-api-key header to the redirect target. Matches connections._OPENER: a 3xx is
    surfaced as an error rather than followed, so the key can never leak to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

# The pinned floor for REASONING — the strongest generally-available model as of this build. Used
# verbatim when the live list can't be reached, and as the minimum the resolver will return.
PREFERRED_GROWTH_MODEL = "claude-fable-5"

# The floor for WORK (drafting a self-change). The Fable-5 proposal sizes the coder model to the task
# but may never pick below this — even a trivial mechanical change is drafted on at least Opus 4.8.
WORK_FLOOR_MODEL = "claude-opus-4-8"

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
        self._refreshing = False

    def _now(self) -> float:
        if self._clock is not None:
            try:
                return self._clock.now().timestamp()
            except Exception:  # noqa: BLE001
                pass
        import time
        return time.monotonic()

    def work_model(self, deep: bool) -> str:
        """The model the self-dev CODER should draft with — the Fable-5 proposal picks the tier:
        deep=True  → the strongest available (resolve(): Fable 5, auto-upscaling), for a subtle,
                     cross-cutting, or architectural change;
        deep=False → the WORK FLOOR (Opus 4.8), for a small, localized, mechanical change.
        Never below the floor: resolve() is always >= Fable 5, which outranks Opus 4.8."""
        return self.resolve() if deep else WORK_FLOOR_MODEL

    def resolve(self) -> str:
        """The growth model id — returns IMMEDIATELY (never blocks a caller, never blocks startup):
        the cached value, or the pinned Fable 5 floor if nothing is cached yet. When the cache is
        stale or empty, a background thread refreshes it from the live model list, so a stronger model
        is adopted on the next call once the fetch lands — a slow or hung network can never freeze the
        app. Never raises."""
        with self._lock:
            fresh = self._cached is not None and (self._now() - self._fetched_at) < _CACHE_TTL_S
            if fresh:
                return self._cached
            current = self._cached or PREFERRED_GROWTH_MODEL
            if not self._refreshing:
                self._refreshing = True
                threading.Thread(target=self._refresh, daemon=True, name="helix-model-select").start()
        return current

    def _refresh(self) -> None:
        try:
            model = self._fetch_best()
        finally:
            with self._lock:
                self._refreshing = False
        with self._lock:
            self._cached = model
            self._fetched_at = self._now()

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
            with _OPENER.open(req, timeout=15) as r:  # no-redirect opener — the key can't leak via a 3xx
                payload = json.loads(r.read(2_000_000).decode("utf-8", "replace"))
            ids = [m.get("id", "") for m in (payload.get("data") or []) if isinstance(m, dict)]
            best = best_growth_model(ids)
            _LOG.info("growth model resolved to %s (from %d available)", best, len(ids))
            return best
        except Exception as exc:  # noqa: BLE001 — offline / bad key / API hiccup → the pinned floor
            _LOG.info("model list unavailable (%s); growth stays on %s", exc, PREFERRED_GROWTH_MODEL)
            return PREFERRED_GROWTH_MODEL
