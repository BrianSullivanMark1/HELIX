"""BlockadeSkybox — text → a 360° equirectangular panorama, via Blockade Labs' Skybox AI.

HELIX's ENVIRONMENT/scene channel: where Tripo makes a single object, this makes a whole immersive
PLACE ("a cozy backyard at dusk", "a forest clearing", "a mid-century living room") the user can look
around inside — rendered as a skybox in the 3D viewer. Stdlib-only HTTP (like the calendar/connections
adapters). Opt-in: no key → the baker shows a friendly banner and the environment isn't generated.

Flow (Blockade Labs v1): POST /skybox to start a generation, poll /imagine/requests/{id} until it's
complete, then download the equirectangular file. All failures raise BlockadeError with a plain message.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

from helix.logging_setup import get_logger

_LOG = get_logger("blockade")

_BASE = "https://backend.blockadelabs.com/api/v1"
_POLL_EVERY = 3.0
_POLL_MAX_S = 150.0     # a skybox generation typically completes in ~15-60s
_HTTP_TIMEOUT = 30.0


class BlockadeError(RuntimeError):
    pass


class BlockadeSkybox:
    def __init__(
        self, key_provider: Callable[[], str | None], style_provider: Callable[[], object] | None = None
    ) -> None:
        self._key = key_provider
        self._style = style_provider or (lambda: None)
        self._default_style_id: int | None = None  # cached from the styles list

    # ----- public -----
    def generate(self, prompt: str) -> bytes:
        """Generate a 360° panorama for `prompt` and return the image bytes. Blocking (polls to done)."""
        key = (self._key() or "").strip()
        if not key:
            raise BlockadeError("Add a Blockade Labs API key in Settings → Connections to make 360° scenes.")
        prompt = " ".join((prompt or "").split())[:600]
        if not prompt:
            raise BlockadeError("An environment needs a description of the scene.")
        style_id = self._resolve_style(key)
        req_id = self._start(key, prompt, style_id)
        file_url = self._await_complete(key, req_id)
        return self._download(file_url)

    # ----- steps -----
    def _start(self, key: str, prompt: str, style_id: int | None) -> str:
        body: dict = {"prompt": prompt}
        if style_id is not None:
            body["skybox_style_id"] = style_id
        data = self._post(f"{_BASE}/skybox", key, body)
        rid = data.get("id") or data.get("obfuscated_id") or (data.get("request") or {}).get("id")
        if rid is None:
            raise BlockadeError(f"Blockade Labs didn't accept the request: {self._msg(data)}")
        return str(rid)

    def _await_complete(self, key: str, req_id: str) -> str:
        deadline = None  # set from a monotonic-free loop using elapsed polls (no time.monotonic in tests)
        waited = 0.0
        while waited <= _POLL_MAX_S:
            data = self._get(f"{_BASE}/imagine/requests/{req_id}", key)
            req = data.get("request") or data
            status = str(req.get("status") or "").lower()
            if status in ("complete", "completed"):
                url = req.get("file_url") or req.get("fileUrl") or ""
                if not url:
                    raise BlockadeError("Blockade Labs finished but returned no image.")
                return url
            if status in ("error", "failed", "abort", "aborted"):
                raise BlockadeError(f"The scene generation failed: {self._msg(req)}")
            time.sleep(_POLL_EVERY)
            waited += _POLL_EVERY
        raise BlockadeError("The scene took too long to generate — try again.")

    def _resolve_style(self, key: str) -> int | None:
        """The configured style id, else a cached default from the styles list (first non-premium one)."""
        configured = self._style()
        if configured not in (None, ""):
            try:
                return int(configured)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
        if self._default_style_id is not None:
            return self._default_style_id
        try:
            styles = self._get(f"{_BASE}/skybox/styles", key)
            items = styles if isinstance(styles, list) else styles.get("styles") or []
            free = [s for s in items if isinstance(s, dict) and not s.get("premium")]
            pick = (free or [s for s in items if isinstance(s, dict)])
            if pick:
                self._default_style_id = int(pick[0].get("id"))
                return self._default_style_id
        except Exception:  # noqa: BLE001 — a styles-fetch hiccup: let the API use its own default
            pass
        return None

    # ----- HTTP (stdlib) -----
    @staticmethod
    def _headers(key: str) -> dict:
        return {"x-api-key": key, "Accept": "application/json", "User-Agent": "HELIX"}

    def _post(self, url: str, key: str, body: dict) -> dict:
        payload = json.dumps(body).encode("utf-8")
        headers = {**self._headers(key), "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        return self._read_json(req)

    def _get(self, url: str, key: str) -> dict:
        req = urllib.request.Request(url, headers=self._headers(key), method="GET")
        return self._read_json(req)

    @staticmethod
    def _read_json(req) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
                raw = r.read(2_000_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read(500).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            if e.code in (401, 403):
                raise BlockadeError("Blockade Labs rejected the API key — check it in Settings.") from e
            raise BlockadeError(f"Blockade Labs returned HTTP {e.code}. {detail}".strip()) from e
        except urllib.error.URLError as e:
            raise BlockadeError(f"Couldn't reach Blockade Labs: {e.reason}") from e
        try:
            data = json.loads(raw or "{}")
        except ValueError as e:
            raise BlockadeError("Blockade Labs returned an unexpected response.") from e
        return data if isinstance(data, dict) else {"list": data}

    @staticmethod
    def _download(url: str) -> bytes:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "HELIX"})
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
                data = r.read(30_000_000)
        except Exception as e:  # noqa: BLE001
            raise BlockadeError(f"Couldn't download the generated scene: {e}") from e
        if not data:
            raise BlockadeError("The generated scene was empty.")
        return data

    @staticmethod
    def _msg(obj: dict) -> str:
        for k in ("error_message", "error", "message", "detail"):
            v = obj.get(k) if isinstance(obj, dict) else None
            if v:
                return str(v)[:200]
        return "no detail"
