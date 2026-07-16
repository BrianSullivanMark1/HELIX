"""BlockadeSkybox — text → a 360° panorama: the start/poll/download flow, style fallback, and errors."""
from __future__ import annotations

import json
import urllib.error

import pytest

from helix.adapters import blockade_skybox as mod
from helix.adapters.blockade_skybox import BlockadeError, BlockadeSkybox


class _Resp:
    def __init__(self, body):
        self._b = body if isinstance(body, bytes) else body.encode("utf-8")

    def read(self, n=None):
        return self._b if n is None else self._b[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    url = req.full_url
    if url.endswith("/skybox") and req.get_method() == "POST":
        return _Resp(json.dumps({"id": 42, "status": "pending"}))
    if "/imagine/requests/42" in url:
        return _Resp(json.dumps({"request": {"status": "complete", "file_url": "https://cdn.example/x.jpg"}}))
    if url == "https://cdn.example/x.jpg":
        return _Resp(b"PANORAMA-BYTES")
    if "/skybox/styles" in url:
        return _Resp(json.dumps([{"id": 5, "name": "Realistic", "premium": False}]))
    return _Resp(json.dumps({}))


def test_generate_start_poll_download(monkeypatch):
    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen)
    s = BlockadeSkybox(lambda: "key", style_provider=lambda: 5)
    assert s.generate("a backyard at dusk") == b"PANORAMA-BYTES"


def test_generate_fetches_a_default_style_when_none_set(monkeypatch):
    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen)
    s = BlockadeSkybox(lambda: "key")  # no style provider → fetches the styles list, picks one
    assert s.generate("a forest clearing") == b"PANORAMA-BYTES"


def test_no_key_is_friendly():
    with pytest.raises(BlockadeError) as e:
        BlockadeSkybox(lambda: "").generate("anything")
    assert "Blockade Labs API key" in str(e.value)


def test_empty_prompt_is_rejected():
    with pytest.raises(BlockadeError):
        BlockadeSkybox(lambda: "key", style_provider=lambda: 5).generate("   ")


def test_bad_key_is_friendly(monkeypatch):
    def unauth(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)
    monkeypatch.setattr(mod.urllib.request, "urlopen", unauth)
    with pytest.raises(BlockadeError) as e:
        BlockadeSkybox(lambda: "key", style_provider=lambda: 5).generate("x")
    assert "key" in str(e.value).lower()


def test_generation_error_status_is_surfaced(monkeypatch):
    def flow(req, timeout=None):
        url = req.full_url
        if url.endswith("/skybox"):
            return _Resp(json.dumps({"id": 7}))
        if "/imagine/requests/7" in url:
            return _Resp(json.dumps({"request": {"status": "error", "error_message": "bad prompt"}}))
        return _Resp(json.dumps({}))
    monkeypatch.setattr(mod.urllib.request, "urlopen", flow)
    with pytest.raises(BlockadeError) as e:
        BlockadeSkybox(lambda: "key", style_provider=lambda: 5).generate("x")
    assert "bad prompt" in str(e.value)
