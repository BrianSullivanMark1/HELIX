"""RemoteService — the remote companion's fail-closed policy: off by default, token-gated, fenced."""
from __future__ import annotations

from helix.services.remote import ENABLED_KEY, LAN_KEY, RemoteService


class _Store:
    def __init__(self, seed=None):
        self.d = dict(seed or {})

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


class _Conv:
    def __init__(self):
        self.calls = []

    def run_turn(self, text, *, allow_builds=True, persist=True, **kw):
        self.calls.append({"text": text, "allow_builds": allow_builds, "persist": persist})
        return f"answer: {text}"


class _Agents:
    def run(self, name, **kw):
        return f"ran {name}"


def _svc(enabled=True, lan=False, conv=None):
    settings = _Store({ENABLED_KEY: enabled, LAN_KEY: lan})
    secrets = _Store()
    svc = RemoteService(settings, secrets, conversation=conv or _Conv(), agents=_Agents())
    if enabled:
        svc.ensure_token()
    return svc


def _hdr(svc, host="127.0.0.1:8770", auth=True):
    h = {"host": host}
    if auth:
        h["authorization"] = "Bearer " + svc.token()
    return h


def test_off_by_default_serves_nothing():
    svc = RemoteService(_Store(), _Store())  # nothing enabled
    assert svc.enabled() is False
    status, _ct, _body = svc.handle("GET", "/status", {"host": "127.0.0.1"}, b"", "127.0.0.1")
    assert status == 404


def test_bind_is_loopback_unless_lan_opted_in():
    assert _svc(lan=False).bind_host() == "127.0.0.1"
    assert _svc(lan=True).bind_host() == "0.0.0.0"


def test_status_requires_the_token():
    svc = _svc()
    bad = svc.handle("GET", "/status", _hdr(svc, auth=False), b"", "127.0.0.1")
    assert bad[0] == 401
    ok = svc.handle("GET", "/status", _hdr(svc), b"", "127.0.0.1")
    assert ok[0] == 200 and b"status" in ok[2]


def test_ask_runs_fenced_autonomously():
    conv = _Conv()
    svc = _svc(conv=conv)
    status, _ct, body = svc.handle("POST", "/ask", _hdr(svc), b'{"text":"what is up"}', "127.0.0.1")
    assert status == 200 and b"answer: what is up" in body
    # the remote path MUST be the fenced, hermetic posture — no builds, nothing persisted
    assert conv.calls[0]["allow_builds"] is False and conv.calls[0]["persist"] is False


def test_agent_route_runs_a_saved_agent():
    svc = _svc()
    status, _ct, body = svc.handle("POST", "/agent", _hdr(svc), b'{"name":"morning brief"}', "127.0.0.1")
    assert status == 200 and b"ran morning brief" in body


def test_dns_rebinding_host_is_rejected():
    svc = _svc()
    # a domain Host (what a rebinding attacker uses) is refused even with a valid token
    bad = svc.handle("GET", "/status", _hdr(svc, host="evil.example.com"), b"", "1.2.3.4")
    assert bad[0] == 403


def test_wrong_token_is_rejected():
    svc = _svc()
    h = {"host": "127.0.0.1", "authorization": "Bearer not-the-real-token"}
    assert svc.handle("GET", "/status", h, b"", "127.0.0.1")[0] == 401


def test_body_size_cap():
    svc = _svc()
    big = b'{"text":"' + b"x" * 20000 + b'"}'
    assert svc.handle("POST", "/ask", _hdr(svc), big, "127.0.0.1")[0] == 413


def test_rate_limit_kicks_in():
    svc = _svc()
    last = 200
    for _ in range(40):
        last = svc.handle("GET", "/status", _hdr(svc), b"", "9.9.9.9")[0]
    assert last == 429  # too many requests from one IP in the window


def test_companion_page_served_without_token():
    svc = _svc()
    status, ctype, body = svc.handle("GET", "/", {"host": "127.0.0.1"}, b"", "127.0.0.1")
    assert status == 200 and "text/html" in ctype and b"HELIX" in body
