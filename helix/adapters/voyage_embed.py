"""VoyageEmbedder — text embeddings via the Voyage AI API, for optional semantic knowledge search.

Opt-in: active only when a Voyage key is connected (the JIT key panel writes the secrets store; a
legacy Settings key or the VOYAGE_API_KEY env var also counts). Plain urllib (like call_api), POSTing
to a FIXED host — no user-controlled URL, so no SSRF surface.
Returns None on any failure, so the knowledge layer always falls back to keyword retrieval. The key is
sent only to Voyage, never written into a build or returned to the model.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

from helix.logging_setup import get_logger

_LOG = get_logger("voyage")
_URL = "https://api.voyageai.com/v1/embeddings"
_MAX_BATCH = 96            # stay under Voyage's per-request input cap
_MAX_CHARS = 16_000        # clamp any single input so one huge chunk can't be rejected/over-billed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect on an authenticated request — the bearer key must only ever reach the
    fixed Voyage host, never a 3xx target (defense-in-depth, same posture as call_api)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class VoyageEmbedder:
    def __init__(
        self,
        key_getter: Callable[[], str | None],
        model: str = "voyage-3.5-lite",
        timeout: float = 30.0,
    ) -> None:
        self._key = key_getter
        self.model = model  # public: the knowledge cache keys vectors by this, so changing it invalidates
        self._timeout = timeout

    def available(self) -> bool:
        return bool((self._key() or "").strip())

    def embed(self, texts: list[str], *, input_type: str | None = None) -> list[list[float]] | None:
        key = (self._key() or "").strip()
        if not key or not texts:
            return None
        clamped = [t[:_MAX_CHARS] for t in texts]
        out: list[list[float]] = []
        for i in range(0, len(clamped), _MAX_BATCH):
            vecs = self._embed_batch(clamped[i : i + _MAX_BATCH], key, input_type)
            if vecs is None:
                return None
            out.extend(vecs)
        return out if len(out) == len(texts) else None

    def _embed_batch(
        self, batch: list[str], key: str, input_type: str | None
    ) -> list[list[float]] | None:
        body: dict = {"input": batch, "model": self.model}
        if input_type in ("query", "document"):
            body["input_type"] = input_type
        req = urllib.request.Request(
            _URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                     "User-Agent": "HELIX"},
        )
        try:
            with _OPENER.open(req, timeout=self._timeout) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            _LOG.warning("voyage embed HTTP %s", getattr(exc, "code", "?"))
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            _LOG.warning("voyage embed failed: %s", exc)
            return None
        try:
            items = payload["data"]
            vecs = [[float(x) for x in it["embedding"]] for it in items]
        except (KeyError, TypeError, ValueError):
            return None
        return vecs if len(vecs) == len(batch) else None
