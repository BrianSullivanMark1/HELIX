"""The API's power-cycle contract, at the ASGI layer.

The quit route must flip the instance to DYING the moment it accepts — the snapshot probe answers
503 from then on. This is what closed the second half of the icon-click race: the real teardown
takes seconds after /api/shell/quit answers, and a click in that window used to probe the dying
server, get a 200, open a tab on it and step aside — leaving the user a connection error and no
HELIX running (the 2026-09-03 07:59 drill caught it live).

Driven as PLAIN ASGI on purpose — no starlette.testclient/httpx: importing that stack at collection
time made an unrelated PyQt6 UI test die with a native access violation ~90s later in a full run
(green without the import, crashed with it, twice each). An in-test ASGI driver has no such reach.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from helix.api.server import EventHub, build_app


class _Settings:
    def __init__(self, **kv):
        self._d = dict(kv)

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class _Shell:
    def snapshot(self):
        return {"t": "snapshot", "authed": True}


def _app():
    container = SimpleNamespace(
        settings=_Settings(web_token="tok-test"),
        paths=SimpleNamespace(builds="does-not-exist"),  # mounted with check_dir=False
    )
    return build_app(container, _Shell(), EventHub(), None)


def _call(app, method: str, path: str, token: str | None = "tok-test") -> int:
    """One HTTP request straight through the ASGI stack; returns the status code. The host header
    matters — the app's origin guard only serves 127.0.0.1/localhost."""
    headers = [(b"host", b"127.0.0.1:8737")]
    if token is not None:
        headers.append((b"x-helix-token", token.encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
        "method": method, "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8737),
    }
    status: list[int] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status.append(int(message["status"]))

    asyncio.run(app(scope, receive, send))
    return status[0]


def test_snapshot_answers_while_alive_and_refuses_unauthenticated():
    app = _app()
    assert _call(app, "GET", "/api/snapshot") == 200
    assert _call(app, "GET", "/api/snapshot", token=None) == 401  # the probe token is not optional


def test_a_refused_quit_does_not_mark_the_instance_dying():
    app = _app()  # no quit hook wired (a bare test app)
    assert _call(app, "POST", "/api/shell/quit") == 501
    assert _call(app, "GET", "/api/snapshot") == 200


def test_an_accepted_quit_reads_dead_immediately():
    app = _app()
    app.state.quit = lambda: None  # webboot's hook, defanged for the test
    assert _call(app, "POST", "/api/shell/quit") == 200
    # No sleep: the flag must flip in the ROUTE, before the 0.4s timer or any teardown — an icon
    # click can land within milliseconds of the Quit button.
    assert _call(app, "GET", "/api/snapshot") == 503
