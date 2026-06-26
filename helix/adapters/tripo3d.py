"""Tripo3D adapter — hosted text/image-to-3D. Turns a prompt into a real, textured GLB (bytes).

This is the neural "turbo" backend behind ModelBaker: for an object/character subject (where stacking
primitives can't compete), HELIX POSTs a prompt to Tripo, polls the task to completion, and downloads the
finished GLB — which drops straight into the same baked-mesh viewer as the local path.

Egress note: the prompt (and any reference image) leaves the machine for Tripo's cloud. This faculty is
OPT-IN — it activates only when a TRIPO_API_KEY is present — so HELIX stays local-first by default.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import httpx

API_BASE = "https://api.tripo3d.ai/v2/openapi"

# Status strings Tripo returns (lower-cased before compare). Everything not success/pending is terminal.
_PENDING = {"queued", "running", "processing", "submitted", "pending"}
_SUCCESS = {"success", "succeeded"}


class TripoError(Exception):
    """The Tripo API rejected the request, timed out, or returned no usable model."""


class Tripo3D:
    """A thin, synchronous client. `generate(prompt, image)` returns GLB bytes or raises TripoError."""

    def __init__(
        self,
        api_key: "str | Callable[[], str | None]",
        *,
        model_version: str | None = None,  # omit → Tripo uses the account's latest (dated strings vary)
        face_limit: int = 100000,          # cap polys so the in-app viewer stays smooth and files stay small
        texture_quality: str = "standard", # "standard" keeps GLBs a few MB; "detailed" = 4K (heavy)
        timeout_s: float = 300.0,
        poll_s: float = 3.0,
        client: httpx.Client | None = None,
    ) -> None:
        # api_key may be a literal or a getter (so the container can read os.environ/settings lazily).
        self._key_src = api_key
        self._model_version = model_version
        self._face_limit = face_limit
        self._texture_quality = texture_quality
        self._timeout_s = timeout_s
        self._poll_s = poll_s
        self._client = client  # injectable for tests; otherwise made per-call

    # ----- the ModelBaker NeuralBackend interface: (prompt, image|None) -> glb bytes -----
    def generate(self, prompt: str, image: Path | None = None) -> bytes:
        key = self._key()
        if not key:
            raise TripoError("no Tripo API key set (TRIPO_API_KEY).")
        prompt = (prompt or "").strip()
        if not prompt:
            raise TripoError("an empty prompt can't be modeled.")
        owns = self._client is None
        client = self._client or httpx.Client(timeout=30.0)
        try:
            headers = {"Authorization": f"Bearer {key}"}
            task_id = self._create(client, headers, prompt)
            url = self._await_model(client, headers, task_id)
            return self._download(client, url)
        finally:
            if owns:
                client.close()

    # ----- steps -----
    def _create(self, client: httpx.Client, headers: dict, prompt: str) -> str:
        body: dict[str, Any] = {
            "type": "text_to_model",
            "prompt": prompt[:1024],
            "texture": True,
            "pbr": True,
            "texture_quality": self._texture_quality,
        }
        if self._face_limit:
            body["face_limit"] = int(self._face_limit)
        if self._model_version:
            body["model_version"] = self._model_version
        data = self._json(client.post(f"{API_BASE}/task", headers=headers, json=body))
        task_id = (data.get("data") or {}).get("task_id")
        if not task_id:
            raise TripoError("Tripo accepted no task (no task_id returned).")
        return str(task_id)

    def _await_model(self, client: httpx.Client, headers: dict, task_id: str) -> str:
        deadline = self._timeout_s
        waited = 0.0
        while True:
            data = self._json(client.get(f"{API_BASE}/task/{task_id}", headers=headers))
            d = data.get("data") or {}
            status = str(d.get("status", "")).lower()
            if status in _SUCCESS:
                url = _output_url(d.get("output") or {})
                if not url:
                    raise TripoError("Tripo finished but returned no model URL.")
                return url
            if status not in _PENDING:
                raise TripoError(f"Tripo generation {status or 'failed'}.")
            if waited >= deadline:
                raise TripoError(f"Tripo timed out after {int(deadline)}s (status: {status}).")
            time.sleep(self._poll_s)
            waited += self._poll_s

    def _download(self, client: httpx.Client, url: str) -> bytes:
        resp = client.get(url, timeout=120.0)
        if resp.status_code != 200 or not resp.content:
            raise TripoError(f"couldn't download the model ({resp.status_code}).")
        return resp.content

    # ----- helpers -----
    def _key(self) -> str | None:
        src = self._key_src
        value = src() if callable(src) else src
        return (value or "").strip() or None

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError:
            raise TripoError(f"Tripo returned non-JSON ({resp.status_code}).")
        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("code") not in (0, None)):
            msg = ""
            if isinstance(data, dict):
                msg = str(data.get("message") or data.get("suggestion") or data.get("code") or "")
            raise TripoError(f"Tripo error {resp.status_code}: {msg or 'request rejected'}.")
        return data if isinstance(data, dict) else {}


def _output_url(output: dict) -> str | None:
    """Pull the best download URL from a task's output. Prefer the textured PBR model; accept either a
    plain URL string or an object with a 'url' field (Tripo has used both shapes)."""
    for key in ("pbr_model", "model", "base_model", "rendered_image"):
        val = output.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
        if isinstance(val, dict):
            url = val.get("url")
            if isinstance(url, str) and url.startswith("http"):
                return url
    return None
