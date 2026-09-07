"""Single-instance lock — the core exclusion/wait/fallback logic (no Qt needed), plus the real Windows
named mutex where we run it. The activation IPC (QLocalServer/QLocalSocket) is best-effort and covered by
the fake-signal routing tests below rather than by spinning up a real GUI."""
from __future__ import annotations

import sys
import time

import pytest

from helix.app import single_instance as si


@pytest.fixture(autouse=True)
def _reset_primary_guard():
    """become_primary_or_signal stashes the primary in a module global for the process lifetime; reset it
    around each test so one test's 'we are primary' doesn't leak into the next."""
    saved = si._PRIMARY_GUARD
    si._PRIMARY_GUARD = None
    try:
        yield
    finally:
        si._PRIMARY_GUARD = saved


class _SharedLock:
    """Stand-in for the OS object: whichever backend grabs `owner` first holds it until released."""

    def __init__(self):
        self.owner = None


class _FakeBackend:
    def __init__(self, shared: _SharedLock):
        self._shared = shared

    def try_acquire(self) -> bool:
        if self._shared.owner is None:
            self._shared.owner = self
        return self._shared.owner is self


class _CountingBackend:
    """False for the first `flip_after` tries, then True — models an outgoing instance releasing the lock."""

    def __init__(self, flip_after: int):
        self.calls = 0
        self._flip_after = flip_after

    def try_acquire(self) -> bool:
        self.calls += 1
        return self.calls > self._flip_after


def test_first_instance_wins_second_is_locked_out(tmp_path):
    shared = _SharedLock()
    first = si.InstanceGuard(tmp_path, backend=_FakeBackend(shared))
    second = si.InstanceGuard(tmp_path, backend=_FakeBackend(shared))
    assert first.acquire(wait_seconds=0) is True
    assert second.acquire(wait_seconds=0) is False


def test_no_wait_gives_up_after_one_try(tmp_path):
    backend = _CountingBackend(flip_after=99)  # never frees within the test
    guard = si.InstanceGuard(tmp_path, backend=backend)
    assert guard.acquire(wait_seconds=0) is False
    assert backend.calls == 1  # a fresh launch does not linger — it decides immediately


def test_relaunch_waits_then_takes_over(tmp_path, monkeypatch):
    monkeypatch.setattr(si.time, "sleep", lambda _s: None)  # don't actually wait in the test
    backend = _CountingBackend(flip_after=3)  # the outgoing instance releases on the 4th poll
    guard = si.InstanceGuard(tmp_path, backend=backend)
    assert guard.acquire(wait_seconds=5) is True
    assert backend.calls == 4


def test_falls_back_to_lock_file_when_primary_backend_raises(tmp_path):
    class _Boom:
        def try_acquire(self):
            raise OSError("mutex primitive unavailable")

    guard = si.InstanceGuard(tmp_path, backend=_Boom())
    # Never blocks startup: it drops to the QLockFile backend (and, if that is also unavailable, proceeds
    # as primary). Either way the app is allowed to launch.
    assert guard.acquire(wait_seconds=0) is True
    assert isinstance(guard._backend, si._LockFileBackend)


def test_a_completely_broken_lock_still_launches(tmp_path, monkeypatch):
    class _Boom:
        def try_acquire(self):
            raise OSError("nope")

    monkeypatch.setattr(si, "_LockFileBackend", lambda *a, **k: _Boom())  # even the fallback fails
    guard = si.InstanceGuard(tmp_path, backend=_Boom())
    assert guard.acquire(wait_seconds=0) is True  # a broken lock must never brick the launcher


def test_lock_and_server_names_are_stable_and_distinct(tmp_path):
    other = tmp_path / "other"
    assert si._lock_name(tmp_path) == si._lock_name(tmp_path)  # deterministic
    assert si._lock_name(tmp_path) != si._server_name(tmp_path)  # the two channels don't collide
    assert si._lock_name(tmp_path) != si._lock_name(other)  # per data dir


def test_become_primary_is_idempotent_and_never_signals(tmp_path, monkeypatch):
    signalled = []
    monkeypatch.setattr(si, "_signal_existing_instance", lambda d: signalled.append(d))
    monkeypatch.setattr(si, "_make_primary_backend", lambda d, n: _FakeBackend(_SharedLock()))
    assert si.become_primary_or_signal(tmp_path, is_relaunch=False) is True
    assert si.become_primary_or_signal(tmp_path, is_relaunch=False) is True  # no-op second call
    assert signalled == []


def test_duplicate_launch_signals_the_running_instance(tmp_path, monkeypatch):
    signalled = []
    monkeypatch.setattr(si, "_signal_existing_instance", lambda d: signalled.append(d))

    class _AlwaysTaken:
        def try_acquire(self):
            return False

    monkeypatch.setattr(si, "_make_primary_backend", lambda d, n: _AlwaysTaken())
    assert si.become_primary_or_signal(tmp_path, is_relaunch=False) is False
    assert signalled == [tmp_path]  # asked the live instance to come to the front


def test_relaunch_force_proceeds_rather_than_leaving_nothing_running(tmp_path, monkeypatch):
    # A self-relaunch whose outgoing instance overruns the wait must NOT exit (that would leave zero live
    # HELIX instances); it starts anyway and reclaims the lock in the background.
    signalled = []
    reclaimed = []
    monkeypatch.setattr(si, "_RELAUNCH_WAIT_SECONDS", 0.05)  # don't actually wait 30s in the test
    monkeypatch.setattr(si, "_signal_existing_instance", lambda d: signalled.append(d))
    monkeypatch.setattr(si.InstanceGuard, "acquire_in_background", lambda self, **k: reclaimed.append(True))

    class _NeverFrees:
        def try_acquire(self):
            return False

    monkeypatch.setattr(si, "_make_primary_backend", lambda d, n: _NeverFrees())
    assert si.become_primary_or_signal(tmp_path, is_relaunch=True) is True  # launched, not abandoned
    assert si._PRIMARY_GUARD is not None
    assert signalled == []  # a relaunch surfaces nobody — it IS the instance now
    assert reclaimed == [True]  # and it keeps trying to take the real lock


def test_acquire_in_background_reclaims_once_the_lock_frees(tmp_path, monkeypatch):
    monkeypatch.setattr(si, "_POLL_SECONDS", 0.01)  # spin fast in the test
    backend = _CountingBackend(flip_after=1)  # the outgoing instance "frees" the lock on the 2nd try
    guard = si.InstanceGuard(tmp_path, backend=backend)
    guard.acquire_in_background(give_up_after=5)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and backend.calls < 2:
        time.sleep(0.02)
    assert backend.calls >= 2  # it kept trying and took the lock once it came free


def test_web_mode_duplicate_never_dials_the_qt_activation_server(tmp_path, monkeypatch):
    """The web shell never starts the QLocalServer, so a duplicate signalling it spins its full 20s
    connect-retry loop at nothing — the icon re-click 'felt dead' for exactly that long, and a click
    during a quit outwaited the very lock release it needed. signal=False (the web path) must skip it."""
    signalled = []
    monkeypatch.setattr(si, "_signal_existing_instance", lambda d: signalled.append(d))

    class _AlwaysTaken:
        def try_acquire(self):
            return False

    monkeypatch.setattr(si, "_make_primary_backend", lambda d, n: _AlwaysTaken())
    assert si.become_primary_or_signal(tmp_path, is_relaunch=False, signal=False) is False
    assert signalled == []  # no Qt, no 20-second spin — the caller opens a browser tab instead


def test_after_quit_takeover_waits_for_the_lock_then_boots(tmp_path, monkeypatch):
    """The quit-then-click race: the dying instance holds the lock for a few seconds after its server
    stopped answering. The click must WAIT for the release and become primary — that click IS the
    restart the user asked for."""
    monkeypatch.setattr(si.time, "sleep", lambda _s: None)
    backend = _CountingBackend(flip_after=3)  # the outgoing instance releases on the 4th poll
    monkeypatch.setattr(si, "_make_primary_backend", lambda d, n: backend)
    assert si.become_primary_after_quit(tmp_path, wait_seconds=5) is True
    assert si._PRIMARY_GUARD is not None
    assert backend.calls == 4


def test_after_quit_takeover_never_boots_a_rival_against_a_healthy_primary(tmp_path, monkeypatch):
    """If the probe failed but the lock never frees, a HEALTHY primary holds it — force-booting a rival
    would fight it for the port. Unlike --relaunch, this path steps aside."""
    monkeypatch.setattr(si.time, "sleep", lambda _s: None)

    class _NeverFrees:
        def try_acquire(self):
            return False

    monkeypatch.setattr(si, "_make_primary_backend", lambda d, n: _NeverFrees())
    assert si.become_primary_after_quit(tmp_path, wait_seconds=0.05) is False
    assert si._PRIMARY_GUARD is None  # not primary, and no background force-reclaim either


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named-mutex backend")
def test_real_windows_mutex_excludes_a_second_guard(tmp_path):
    first = si.InstanceGuard(tmp_path)  # real _WindowsMutexBackend
    assert first.acquire(wait_seconds=0) is True
    second = si.InstanceGuard(tmp_path)
    assert second.acquire(wait_seconds=0) is False
    assert first is not None  # keep the mutex handle alive for the duration of the assertions


# ---------------------------------------------------------------------------------------------
# The other half of the restart story: the icon click's backend probe (cli.backend_alive /
# open_running_face). A tab must only ever be opened at a backend that PROVED it is alive; a dead
# one returns False so the gate waits for the lock instead of leaving the user a connection error.
# ---------------------------------------------------------------------------------------------

def _fake_backend(status: int = 200):
    """A minimal live 'HELIX backend': answers /api/snapshot on an ephemeral localhost port."""
    import http.server
    import threading

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server's contract
            self.send_response(status if self.path == "/api/snapshot" else 404)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):  # keep the test output quiet
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_backend_alive_proves_life_and_death():
    from helix.app.cli import backend_alive

    srv = _fake_backend()
    try:
        assert backend_alive(srv.server_address[1], "tok") is True
    finally:
        srv.shutdown()
    # the port is closed now: a dead backend must read as dead, fast, with no exception
    assert backend_alive(srv.server_address[1], "tok", tries=1, timeout=0.3) is False
    # a TEARING-DOWN backend answers 503 (the quit route flips it) — that is dead too, not alive
    dying = _fake_backend(status=503)
    try:
        assert backend_alive(dying.server_address[1], "tok", tries=1) is False
    finally:
        dying.shutdown()


def test_open_running_face_opens_only_a_live_backend(tmp_path, monkeypatch):
    import json

    opened: list[str] = []
    monkeypatch.setenv("HELIX_DATA_DIR", str(tmp_path))
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    from helix.app import cli

    srv = _fake_backend()
    port = srv.server_address[1]
    (tmp_path / "helix_settings.json").write_text(
        json.dumps({"web_token": "tok-abc", "web_port": port}), encoding="utf-8")
    try:
        assert cli.open_running_face() is True
        assert opened and f":{port}/?t=tok-abc" in opened[0]
    finally:
        srv.shutdown()
    # same settings, dead backend: no tab, False — the caller takes over the lock instead
    opened.clear()
    assert cli.open_running_face() is False
    assert opened == []
