"""ConnectionsService — the Forge's API-key capability: reading a build's declared keys, storing/injecting
values, the read-only call_api guard rails, and the 'a backend build runs' classification fix."""
from __future__ import annotations

import json
from pathlib import Path

from helix.domain.connections import KNOWN_SERVICES, service_for_url
from helix.domain.models import AppKind
from helix.services.builds import BuildService
from helix.services.connections import ConnectionsService


class _Builds:
    def __init__(self, root: Path) -> None:
        self._root = root

    def workspace(self, slug: str) -> Path:
        return self._root / slug


class _Secrets:
    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


def _svc(tmp_path: Path) -> ConnectionsService:
    return ConnectionsService(_Builds(tmp_path), _Secrets())


def _declare(tmp_path: Path, slug: str, entries: list) -> None:
    ws = tmp_path / slug
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "connections.json").write_text(json.dumps(entries), encoding="utf-8")


def test_declared_parses_and_ignores_junk(tmp_path):
    s = _svc(tmp_path)
    _declare(tmp_path, "foo", [
        {"key": "SLACK_TOKEN", "label": "Slack token", "hint": "xoxp-"},
        {"nope": 1},            # no key — ignored
        {"key": "   "},         # blank key — ignored
        {"key": "GITHUB_TOKEN"},  # label/hint default
    ])
    conns = s.declared("foo")
    assert [c.key for c in conns] == ["SLACK_TOKEN", "GITHUB_TOKEN"]
    assert conns[0].label == "Slack token" and conns[0].hint == "xoxp-"
    assert conns[1].label == "GITHUB_TOKEN"  # falls back to the key name
    assert s.declared("missing") == []       # no file → nothing declared


def test_values_env_and_missing(tmp_path):
    s = _svc(tmp_path)
    _declare(tmp_path, "foo", [{"key": "SLACK_TOKEN", "label": "Slack"},
                               {"key": "GITHUB_TOKEN", "label": "GitHub"}])
    assert s.needs_connection("foo") is True
    assert [c.key for c in s.missing("foo")] == ["SLACK_TOKEN", "GITHUB_TOKEN"]
    s.set_value("SLACK_TOKEN", "  xoxp-abc  ")
    assert s.value("SLACK_TOKEN") == "xoxp-abc"          # trimmed
    assert s.env_for("foo") == {"SLACK_TOKEN": "xoxp-abc"}  # only the SET key is injected
    assert [c.key for c in s.missing("foo")] == ["GITHUB_TOKEN"]


def test_call_api_refuses_non_https_unknown_and_unconnected(tmp_path):
    s = _svc(tmp_path)
    assert "https" in s.call_api("http://slack.com/api/auth.test").lower()
    assert "connectable service" in s.call_api("https://evil.example.com/x")
    # known host but no token yet
    out = s.call_api("https://slack.com/api/auth.test")
    assert "isn't connected" in out and "Slack" in out


def test_call_api_attaches_bearer_and_never_returns_token(tmp_path, monkeypatch):
    s = _svc(tmp_path)
    s.set_value("SLACK_TOKEN", "tok-secret-123")
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b'{"ok":true,"team":"Acme"}'

    import helix.services.connections as mod

    def fake_open(req, timeout=0):
        captured["auth"] = req.get_header("Authorization")
        captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(mod._OPENER, "open", fake_open)
    out = s.call_api("https://slack.com/api/auth.test")
    assert out == '{"ok":true,"team":"Acme"}'
    assert captured["auth"] == "Bearer tok-secret-123"  # token attached server-side
    assert "tok-secret-123" not in out                  # never leaked back to the model


def test_call_api_never_follows_redirects():
    # The critical guard: a redirect must never be auto-followed (it would re-send the token to an
    # unvalidated host). The opener's handler returns None for every 3xx.
    import helix.services.connections as mod
    assert mod._NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://evil.example/x") is None


def test_service_for_url_matching():
    assert service_for_url("https://slack.com/api/conversations.list").id == "slack"
    assert service_for_url("https://api.github.com/user/repos").id == "github"
    assert service_for_url("https://files.slack.com/x").id == "slack"  # subdomain
    assert service_for_url("https://evil.example.com/slack.com").id != "slack" \
        if service_for_url("https://evil.example.com/slack.com") else True  # path can't spoof host
    assert service_for_url("https://evil.example.com") is None
    assert {s.env for s in KNOWN_SERVICES} == {"SLACK_TOKEN", "GITHUB_TOKEN"}


def test_detect_entry_prefers_a_backend_main_py(tmp_path):
    # The Slack-dashboard fix: a build with index.html + main.py runs the backend, not the landing page.
    ws = tmp_path / "app"
    ws.mkdir()
    (ws / "index.html").write_text("<h1>start here</h1>", encoding="utf-8")
    (ws / "main.py").write_text("print('server')", encoding="utf-8")
    kind, entry = BuildService(tmp_path, None, None)._detect_entry(ws)
    assert kind == AppKind.PYTHON and entry == "main.py"
    # a pure web app (no main.py) still resolves to its page
    ws2 = tmp_path / "web"
    ws2.mkdir()
    (ws2 / "index.html").write_text("<h1>app</h1>", encoding="utf-8")
    kind2, entry2 = BuildService(tmp_path, None, None)._detect_entry(ws2)
    assert kind2 == AppKind.HTML and entry2 == "index.html"
