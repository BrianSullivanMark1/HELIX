"""Tripo3D adapter tests — request shape, polling, download, and error handling, all against a fake HTTP
client (no network). The live happy-path was verified by hand against the real API; these lock the wiring
so a regression in request/response handling is caught."""
from __future__ import annotations

import pytest

from helix.adapters.tripo3d import Tripo3D, TripoError, _output_url


class _Resp:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Client:
    """Returns a scripted create response, a queue of poll responses, and one download response."""

    def __init__(self, create=None, polls=None, download=None):
        self._create = create
        self._polls = list(polls or [])
        self._download = download
        self.posts: list = []
        self.gets: list = []

    def post(self, url, headers=None, json=None):
        self.posts.append((url, headers, json))
        return self._create

    def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        if "/task/" in url:
            return self._polls.pop(0)
        return self._download  # a model download URL


def _client(**kw):
    return _Client(**kw)


def test_happy_path_returns_glb_bytes():
    c = _client(
        create=_Resp(200, {"code": 0, "data": {"task_id": "t1"}}),
        polls=[
            _Resp(200, {"code": 0, "data": {"status": "running", "progress": 40}}),
            _Resp(200, {"code": 0, "data": {"status": "success",
                                            "output": {"pbr_model": "https://dl/model.glb"}}}),
        ],
        download=_Resp(200, content=b"GLB-BYTES"),
    )
    out = Tripo3D("key", poll_s=0, timeout_s=10, client=c).generate("a wooden chair")
    assert out == b"GLB-BYTES"
    # request was shaped correctly
    url, headers, body = c.posts[0]
    assert url.endswith("/v2/openapi/task")
    assert headers["Authorization"] == "Bearer key"
    assert body["type"] == "text_to_model" and body["prompt"] == "a wooden chair"
    assert "https://dl/model.glb" in c.gets  # downloaded the pbr model


def test_missing_key_raises():
    with pytest.raises(TripoError):
        Tripo3D(lambda: None, client=_client()).generate("x")


def test_empty_prompt_raises():
    with pytest.raises(TripoError):
        Tripo3D("key", client=_client()).generate("   ")


def test_insufficient_credit_surfaces_message():
    c = _client(create=_Resp(403, {"code": 2010, "message": "You don't have enough credit"}))
    with pytest.raises(TripoError) as e:
        Tripo3D("key", client=c).generate("a chair")
    assert "credit" in str(e.value).lower()


def test_failed_status_raises():
    c = _client(
        create=_Resp(200, {"code": 0, "data": {"task_id": "t1"}}),
        polls=[_Resp(200, {"code": 0, "data": {"status": "failed"}})],
    )
    with pytest.raises(TripoError):
        Tripo3D("key", poll_s=0, timeout_s=10, client=c).generate("a chair")


def test_timeout_raises():
    c = _client(
        create=_Resp(200, {"code": 0, "data": {"task_id": "t1"}}),
        polls=[_Resp(200, {"code": 0, "data": {"status": "running"}})],
    )
    with pytest.raises(TripoError):
        Tripo3D("key", poll_s=0, timeout_s=0, client=c).generate("a chair")


def test_no_task_id_raises():
    c = _client(create=_Resp(200, {"code": 0, "data": {}}))
    with pytest.raises(TripoError):
        Tripo3D("key", client=c).generate("a chair")


def _success_client():
    return _client(
        create=_Resp(200, {"code": 0, "data": {"task_id": "t"}}),
        polls=[_Resp(200, {"code": 0, "data": {"status": "success",
                                               "output": {"pbr_model": "https://d/m.glb"}}})],
        download=_Resp(200, content=b"GLB"),
    )


def test_high_detail_omits_face_limit_and_uses_detailed_textures():
    c = _success_client()
    Tripo3D("k", face_limit=0, texture_quality="detailed", poll_s=0, timeout_s=5, client=c).generate("a dragon")
    body = c.posts[0][2]
    assert "face_limit" not in body                 # native (high) polygon count
    assert body["texture_quality"] == "detailed"


def test_balanced_caps_faces():
    c = _success_client()
    Tripo3D("k", face_limit=100000, texture_quality="standard", poll_s=0, timeout_s=5, client=c).generate("a dragon")
    body = c.posts[0][2]
    assert body["face_limit"] == 100000 and body["texture_quality"] == "standard"


def test_output_url_handles_string_and_object_shapes():
    assert _output_url({"pbr_model": "https://x/m.glb"}) == "https://x/m.glb"
    assert _output_url({"model": {"url": "https://x/n.glb", "type": "glb"}}) == "https://x/n.glb"
    assert _output_url({"pbr_model": "https://p.glb", "model": "https://m.glb"}) == "https://p.glb"
    assert _output_url({}) is None
