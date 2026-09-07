"""Model resolver — HELIX grows on the strongest brain Anthropic offers (READ_ME/BRAIN.md, Growth).

Everyday conversation runs on a fast model. But GROWTH reasoning — the deep reasoner (think_harder)
and the nightly dream session, where HELIX rewrites itself — must use the most capable model available.
That is Fable today; when a stronger model in the same line ships (a future Fable 6, or a higher
Opus), HELIX should adopt it AUTOMATICALLY, no code change.

FABLE, ELSE OPUS. Fable is not on every plan, and a floor that names a model the plan cannot call is
worse than no floor at all — every growth call then fails on an id that does not resolve. So the step
down is explicit and NAMED: when the live list carries no Fable, or a caller reports that the resolved
model came back unavailable, growth runs on `FALLBACK_GROWTH_MODEL` (Opus) and says so. It never drops
below Opus-class — falling to Sonnet is the silent downgrade this rule exists to prevent
(READ_ME/DREAM_MIND.md §13 rule 1).

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

# The pin for REASONING — the strongest generally-available model as of this build. Used verbatim
# only when NOTHING was learned about the plan (no key, offline, an API hiccup): a live list that
# carries no Fable is authoritative and yields the fallback below instead.
PREFERRED_GROWTH_MODEL = "claude-fable-5-1"

# What growth runs on when Fable ISN'T there — the plan doesn't carry it, or a call came back saying
# so. Opus is the sanctioned step down; below it is the downgrade §13 forbids.
FALLBACK_GROWTH_MODEL = "claude-opus-5"

# The floor for WORK (drafting a self-change). The proposal sizes the coder model to the task but may
# never pick below this — even a trivial mechanical change is drafted on at least Opus.
WORK_FLOOR_MODEL = "claude-opus-5"

# How long a model a caller reported unavailable stays demoted. Long enough that one refusal doesn't
# make every step of a night retry a model the plan doesn't carry, short enough that a plan upgrade
# is picked up the same day.
_DEMOTE_TTL_S = 43_200.0  # half a day

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


def is_fable_class(model_id: str) -> bool:
    """True for the TOP tier — Fable, or the Mythos line above it. What the dream reports on when it
    says which brain the night ran on."""
    parsed = _parse(model_id)
    return parsed is not None and _FAMILY_RANK.get(parsed[0], 0) >= _FAMILY_RANK["fable"]


def best_growth_model(candidate_ids: list[str]) -> str:
    """The strongest id among `candidate_ids`.

    A LIVE list is authoritative: a plan carrying no Fable resolves to its strongest OPUS — the named
    fallback — rather than the pinned Fable id, which is what the old unconditional floor returned and
    which nothing on that plan can call. Growth still never drops below Opus-class: a Sonnet-only list
    yields the Opus fallback id (the caller's own gate then reports the tier honestly) instead of
    quietly growing on Sonnet. Only an EMPTY or unrecognizable list — no key, offline, an API hiccup,
    so nothing was learned about the plan — keeps the pinned floor. Adopts a future Fable 6 / higher
    Opus automatically because its (family, version) sorts above the current top."""
    best, best_key = "", (0, ())
    for mid in candidate_ids or []:
        key = _rank(mid)
        if key > best_key:
            best, best_key = mid, key
    if not best:
        return PREFERRED_GROWTH_MODEL
    if best_key[0] < _FAMILY_RANK["opus"]:
        return FALLBACK_GROWTH_MODEL
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
        self._demoted_until = 0.0   # while in the future, growth is on the Opus fallback

    def _now(self) -> float:
        if self._clock is not None:
            try:
                return self._clock.now().timestamp()
            except Exception:  # noqa: BLE001
                pass
        import time
        return time.monotonic()

    def work_model(self, deep: bool) -> str:
        """The model the self-dev CODER should draft with — the proposal picks the tier:
        deep=True  → the strongest available (resolve(): Fable, else the Opus fallback);
        deep=False → the WORK FLOOR (Opus), for a small, localized, mechanical change.
        Never below the floor: resolve() is always Opus-class or better."""
        return self.resolve() if deep else WORK_FLOOR_MODEL

    def note_unavailable(self, model_id: str) -> str:
        """A CALLER reporting that the resolved model can't actually be used right now — the plan
        doesn't carry it, or a call came back saying so. Demotes growth to the Opus fallback for
        `_DEMOTE_TTL_S` and returns what growth now runs on, so one refusal doesn't make every step
        of a night retry a model that isn't there.

        On a subscription-only install (the API key cleared, everything billed to the plan) this is
        the ONLY availability signal there is: `_fetch_best` never consults the live list without a
        key, so the pin would otherwise stand forever. Reporting a model at or below the fallback
        changes nothing — there is nothing left to step down to."""
        mid = (model_id or "").strip()
        if not mid or not is_fable_class(mid):
            return self.resolve()
        with self._lock:
            self._demoted_until = self._now() + _DEMOTE_TTL_S
        _LOG.info("growth model %s is unavailable — growth falls back to %s", mid, FALLBACK_GROWTH_MODEL)
        return FALLBACK_GROWTH_MODEL

    def fallback_active(self) -> bool:
        """True while growth is on the Opus fallback because a caller reported Fable unavailable."""
        with self._lock:
            return self._now() < self._demoted_until

    def resolve(self) -> str:
        """The growth model id — returns IMMEDIATELY (never blocks a caller, never blocks startup):
        the cached value, or the pinned Fable floor if nothing is cached yet. When the cache is
        stale or empty, a background thread refreshes it from the live model list, so a stronger model
        is adopted on the next call once the fetch lands — a slow or hung network can never freeze the
        app. While a caller's `note_unavailable` demotion stands, this is the Opus fallback whatever
        the list says. Never raises."""
        with self._lock:
            if self._now() < self._demoted_until:
                return FALLBACK_GROWTH_MODEL
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
