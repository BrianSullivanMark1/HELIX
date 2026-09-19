"""The face builder and its routes: stale means src newer than dist; the build runs npm in web/;
one build at a time; Python newer than launch means 'restart to pick it up'."""
from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

from fastapi import FastAPI

from helix.adapters import face_build as fb
from helix.api import face_routes


def _tree(tmp_path, *, built: bool = True):
    (tmp_path / "web" / "src").mkdir(parents=True)
    (tmp_path / "web" / "src" / "App.tsx").write_text("x")
    (tmp_path / "web" / "package.json").write_text("{}")
    (tmp_path / "helix").mkdir()
    (tmp_path / "helix" / "a.py").write_text("x")
    if built:
        (tmp_path / "web" / "dist").mkdir()
        (tmp_path / "web" / "dist" / "index.html").write_text("<html>")
    return tmp_path


def _touch(p, when):
    os.utime(p, (when, when))


def test_fresh_when_dist_is_newer_than_every_source(tmp_path):
    root = _tree(tmp_path)
    now = time.time()
    _touch(root / "web" / "src" / "App.tsx", now - 100)
    _touch(root / "web" / "package.json", now - 100)
    _touch(root / "web" / "dist" / "index.html", now - 10)
    s = fb.FaceBuilder(root, runner=lambda a, c, t: fb.Ran(0, "", 0)).face_status()
    assert s["stale"] is False and s["never_built"] is False


def test_stale_when_a_source_file_is_newer_than_dist(tmp_path):
    root = _tree(tmp_path)
    now = time.time()
    _touch(root / "web" / "dist" / "index.html", now - 100)
    _touch(root / "web" / "src" / "App.tsx", now - 10)
    s = fb.FaceBuilder(root, runner=lambda a, c, t: fb.Ran(0, "", 0)).face_status()
    assert s["stale"] is True


def test_never_built_is_stale_and_says_so(tmp_path):
    root = _tree(tmp_path, built=False)
    s = fb.FaceBuilder(root, runner=lambda a, c, t: fb.Ran(0, "", 0)).face_status()
    assert s["stale"] is True and s["never_built"] is True


def test_node_modules_and_dist_do_not_count_as_source(tmp_path):
    root = _tree(tmp_path)
    now = time.time()
    _touch(root / "web" / "dist" / "index.html", now - 50)
    _touch(root / "web" / "src" / "App.tsx", now - 100)
    _touch(root / "web" / "package.json", now - 100)
    nm = root / "web" / "src" / "node_modules"
    nm.mkdir()
    (nm / "x.js").write_text("x")
    _touch(nm / "x.js", now)
    s = fb.FaceBuilder(root, runner=lambda a, c, t: fb.Ran(0, "", 0)).face_status()
    assert s["stale"] is False


def test_backend_stale_when_python_changed_after_launch(tmp_path):
    root = _tree(tmp_path)
    now = time.time()
    _touch(root / "helix" / "a.py", now - 100)
    b = fb.FaceBuilder(root, runner=lambda a, c, t: fb.Ran(0, "", 0), started_at=now - 50)
    assert b.backend_status()["stale"] is False
    _touch(root / "helix" / "a.py", now)
    assert b.backend_status()["stale"] is True


def test_build_runs_npm_in_web_and_keeps_the_tail(tmp_path):
    root = _tree(tmp_path)
    seen = []

    def runner(argv, cwd, timeout):
        seen.append((argv, cwd))
        return fb.Ran(0, "\n".join(f"line {i}" for i in range(60)) + "\n✓ built in 1.0s", 1.2)

    b = fb.FaceBuilder(root, runner=runner, npm="npm")
    r = b.build()
    assert r["ok"] is True and r["seconds"] == 1.2
    assert seen[0][0][1:] == ["run", "build"] and seen[0][1] == root / "web"
    assert r["output"].endswith("✓ built in 1.0s") and len(r["output"].splitlines()) == 40
    assert b.status()["last"] == r


def test_build_failure_is_one_sentence_plus_the_output(tmp_path):
    root = _tree(tmp_path)
    b = fb.FaceBuilder(root, runner=lambda a, c, t: fb.Ran(2, "error TS2304: Cannot find name 'x'", 0.4))
    r = b.build()
    assert r["ok"] is False and "failed" in r["error"] and "TS2304" in r["output"]


def test_a_second_build_while_one_runs_is_told_busy(tmp_path):
    import threading
    root = _tree(tmp_path)
    gate = threading.Event()

    def slow(argv, cwd, timeout):
        gate.wait(5)
        return fb.Ran(0, "ok", 0.1)

    b = fb.FaceBuilder(root, runner=slow)
    t = threading.Thread(target=b.build)
    t.start()
    time.sleep(0.05)
    r = b.build()
    assert r.get("busy") is True and r["error"] == fb.BUSY
    gate.set()
    t.join()


# ------------------------------------------------------------------------------ the routes

def _call(app, method, path):
    headers = [(b"host", b"127.0.0.1:8737")]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
             "method": method, "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": b"", "headers": headers, "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8737)}
    status, chunks = [], []
    done = {"v": False}

    async def receive():
        if done["v"]:
            await asyncio.sleep(3600)
        done["v"] = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            status.append(msg["status"])
        elif msg["type"] == "http.response.body":
            chunks.append(msg.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return status[0], json.loads(b"".join(chunks).decode() or "null")


def test_routes_status_build_and_the_503_when_absent(tmp_path):
    root = _tree(tmp_path)
    app = FastAPI()
    face = fb.FaceBuilder(root, runner=lambda a, c, t: fb.Ran(0, "✓ built", 0.3))
    face_routes.mount_face(app, SimpleNamespace(face=face))
    st, doc = _call(app, "GET", "/api/face/status")
    assert st == 200 and set(doc) == {"face", "backend", "building", "last"}
    st, doc = _call(app, "POST", "/api/face/build")
    assert st == 200 and doc["ok"] is True
    bare = FastAPI()
    face_routes.mount_face(bare, SimpleNamespace())
    assert _call(bare, "GET", "/api/face/status")[0] == 503


def test_restart_spawns_then_quits_and_refuses_without_a_hook():
    app = FastAPI()
    calls = []
    face_routes.mount_face(app, SimpleNamespace(restart=lambda: calls.append("restart")))
    st, doc = _call(app, "POST", "/api/face/restart")
    assert st == 501 and "relaunch hook" in doc["error"]       # tests have no graceful quit
    app.state.quit = lambda: calls.append("quit")
    st, doc = _call(app, "POST", "/api/face/restart")
    assert st == 200 and calls == ["restart"] and app.state.quitting is True
    time.sleep(0.6)
    assert calls == ["restart", "quit"]
