"""Single instance — one HELIX per data directory, safe against double/triple/multi-clicking the icon.

A *fresh* launch that finds an instance already running does NOT start a second app: it asks the running
one to bring its window to the front and then exits immediately. A *self-relaunch* (restart / watchdog /
self-heal, marked with ``--relaunch``) instead WAITS for the outgoing instance to release the lock and
then takes over — so the always-on cadence survives an in-place restart.

Two layers, deliberately separate:
  * Exclusion — a per-data-dir OS lock. On Windows this is a named mutex (auto-released the instant the
    owning process dies, so a crash/kill can never strand the lock), acquired through ``ctypes`` with NO
    PyQt6 import: main.py consults this BEFORE the voice STT is pre-warmed, and pre-warm must precede any
    PyQt6 import (a documented faster-whisper-on-Windows launch crash). Off Windows we fall back to Qt's
    QLockFile — importing Qt early there is harmless.
  * Activation — the primary listens on a QLocalServer; a would-be second instance connects to it to say
    "raise your window". Qt is imported LAZILY and only on paths that are allowed to import it (the
    already-a-duplicate signalling path never pre-warms voice, so importing Qt there is fine).

This module is under the protected ``helix/app/`` prefix, so the self-improving coder can never edit it —
correct for a startup/lifecycle primitive.
"""
from __future__ import annotations

import hashlib
import sys
import threading
import time
from pathlib import Path

from helix.logging_setup import get_logger

_LOG = get_logger("single_instance")

# A --relaunch waits this long for the OUTGOING instance to die and free the lock. It must exceed that
# process's worst-case aboutToQuit teardown — build-queue 2×3s + self-dev 3s + console ~3s + voice ~4s
# ≈ 16s — with headroom, or the restart could bounce off the still-held lock and leave nothing running.
_RELAUNCH_WAIT_SECONDS = 30.0
_RECLAIM_WAIT_SECONDS = 120.0  # after a FORCED relaunch (below), keep reclaiming the real lock this long
_POLL_SECONDS = 0.25           # gap between lock retries while waiting
# Keep trying to reach the primary's activation server this long. A cold boot (voice STT pre-warm +
# container build + interrupted-work recovery) can take many seconds before it starts listening, so this
# must comfortably outlast a cold start or a second click during boot silently fails to raise the window.
_SIGNAL_TIMEOUT_SECONDS = 20.0
_CONNECT_MS = 200              # per-attempt QLocalSocket connect timeout
_ERROR_ALREADY_EXISTS = 183    # Win32: CreateMutexW succeeded but the named object was already there

# Held for the whole process lifetime once we become the primary — keeps the mutex handle / lock file
# alive (releasing it would let a second instance slip in). Also makes the guard idempotent per-process.
_PRIMARY_GUARD: "InstanceGuard | None" = None


def _digest(data_dir: Path | str) -> str:
    """A stable, collision-resistant tag for a data directory (case-folded — Windows paths are)."""
    norm = str(Path(data_dir).resolve()).lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _lock_name(data_dir: Path | str) -> str:
    return f"helix-singleton-{_digest(data_dir)}"


def _server_name(data_dir: Path | str) -> str:
    return f"helix-activate-{_digest(data_dir)}"


class _WindowsMutexBackend:
    """A Win32 named mutex as an existence lock: the object lives as long as the first creator keeps its
    handle open, so every later CreateMutexW sees ERROR_ALREADY_EXISTS. The OS destroys it when the owning
    process exits (even on crash), so there is nothing stale to clean up."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._handle: int | None = None  # kept open for the process lifetime once we own the object

    def try_acquire(self) -> bool:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, self._name)
        err = ctypes.get_last_error()
        if not handle:
            raise OSError(err, "CreateMutexW failed")  # let the guard fall back to a lock file
        if err == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(handle)  # someone else owns the object; drop our extra handle
            return False
        self._handle = handle  # we created it first → hold the handle → we are the primary
        return True


class _LockFileBackend:
    """Portable fallback (non-Windows, or if the mutex path ever fails): Qt's QLockFile, which clears
    locks left by dead processes on its own. Kept alive for the process lifetime like the mutex handle."""

    def __init__(self, data_dir: Path | str, name: str) -> None:
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / f"{name}.lock"
        self._lock = None

    def try_acquire(self) -> bool:
        from PyQt6.QtCore import QLockFile

        if self._lock is None:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._lock = QLockFile(str(self._path))
        return self._lock.tryLock(0)


def _make_primary_backend(data_dir: Path | str, name: str):
    if sys.platform == "win32":
        return _WindowsMutexBackend(name)
    return _LockFileBackend(data_dir, name)


class InstanceGuard:
    """Owns the exclusion lock for one data directory. ``acquire`` returns True when we hold it."""

    def __init__(self, data_dir: Path | str, *, backend=None) -> None:
        self._data_dir = Path(data_dir)
        self._name = _lock_name(data_dir)
        self._backend = backend if backend is not None else _make_primary_backend(data_dir, self._name)

    def _try_once(self) -> bool:
        try:
            return self._backend.try_acquire()
        except Exception:  # noqa: BLE001 — the mutex primitive misbehaved; drop to the lock file once
            if isinstance(self._backend, _LockFileBackend):
                raise  # already the fallback and it still failed — let acquire() decide
            _LOG.warning("named-mutex lock failed; falling back to a lock file", exc_info=True)
            self._backend = _LockFileBackend(self._data_dir, self._name)
            return self._backend.try_acquire()

    def acquire(self, *, wait_seconds: float) -> bool:
        """Try to take the lock. ``wait_seconds`` > 0 keeps retrying that long (the relaunch path, waiting
        for the outgoing instance to die). Returns True if we now hold it, False if another instance does.
        If the lock primitive is completely broken we proceed as primary rather than block startup."""
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            try:
                if self._try_once():
                    return True
            except Exception:  # noqa: BLE001 — never let a broken lock keep HELIX from launching at all
                _LOG.exception("could not evaluate the single-instance lock; proceeding without exclusion")
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_POLL_SECONDS, remaining))

    def acquire_in_background(self, *, give_up_after: float = _RECLAIM_WAIT_SECONDS) -> None:
        """Keep trying to take the real OS lock from a daemon thread. Used only after a --relaunch had to
        force-proceed WITHOUT the lock (the outgoing instance overran its wait): once it finally exits we
        grab the lock so a later launch can't slip in as a rival primary. Best-effort; harmless if it never
        succeeds (we are already running as the de-facto primary either way)."""

        def _spin() -> None:
            deadline = time.monotonic() + give_up_after
            while time.monotonic() < deadline:
                try:
                    if self._try_once():
                        _LOG.info("relaunch reclaimed the single-instance lock (outgoing instance exited)")
                        return
                except Exception:  # noqa: BLE001 — the lock primitive is unusable; stop trying
                    return
                time.sleep(_POLL_SECONDS * 2)

        threading.Thread(target=_spin, name="helix-relock", daemon=True).start()


def _signal_existing_instance(data_dir: Path | str) -> None:
    """Best-effort: connect to the running HELIX and tell it to surface its window. Never raises. Retries
    briefly because the primary may still be starting its server on a near-simultaneous double-click."""
    try:
        from PyQt6.QtCore import QCoreApplication
        from PyQt6.QtNetwork import QLocalSocket
    except Exception:  # noqa: BLE001 — no QtNetwork means we simply can't ask it to raise; that's fine
        return
    # QLocalSocket needs a running application object for its event handling; create a throwaway one if
    # this process has none (the duplicate never builds a GUI — it's about to exit).
    _app = QCoreApplication.instance() or QCoreApplication([])
    name = _server_name(data_dir)
    deadline = time.monotonic() + _SIGNAL_TIMEOUT_SECONDS
    while True:
        sock = QLocalSocket()
        sock.connectToServer(name)
        if sock.waitForConnected(_CONNECT_MS):
            try:
                sock.write(b"raise\n")
                sock.flush()
                sock.waitForBytesWritten(_CONNECT_MS)
                sock.disconnectFromServer()
            except Exception:  # noqa: BLE001
                pass
            return
        sock.abort()
        if time.monotonic() >= deadline:
            return
        time.sleep(0.1)


def become_primary_or_signal(data_dir: Path | str, *, is_relaunch: bool) -> bool:
    """The single entry-point decision. Returns True if we hold the singleton (proceed to launch). Returns
    False after asking the already-running instance to come to the front (the caller should exit).

    Idempotent per-process: once we are the primary a second call is a no-op returning True — so the
    main.py gate and the cli.main backstop can both call it without fighting."""
    global _PRIMARY_GUARD
    if _PRIMARY_GUARD is not None:
        return True
    guard = InstanceGuard(data_dir)
    if guard.acquire(wait_seconds=_RELAUNCH_WAIT_SECONDS if is_relaunch else 0.0):
        _PRIMARY_GUARD = guard
        return True
    if is_relaunch:
        # The outgoing instance outlived the wait (an unusually slow teardown). A self-relaunch KNOWS the
        # old copy is on its way out, and for an always-on app a live instance beats none — so start
        # anyway rather than exit and leave HELIX dead. Reclaim the real lock in the background so that,
        # once the old process finally exits, a later launch still can't become a second primary.
        _LOG.error(
            "relaunch did not get the lock within %.0fs; starting anyway and reclaiming it in the background",
            _RELAUNCH_WAIT_SECONDS,
        )
        _PRIMARY_GUARD = guard
        guard.acquire_in_background()
        return True
    _signal_existing_instance(data_dir)
    return False


def start_activation_server(app, data_dir: Path | str, on_activate) -> "object | None":
    """Primary side: listen for second-launch pings and call ``on_activate`` (raise the window) for each.
    Returns the QLocalServer (keep a reference alive) or None if it couldn't listen. Best-effort — a
    failure here only costs the raise-on-reclick nicety, not the single-instance guarantee."""
    try:
        from PyQt6.QtNetwork import QLocalServer
    except Exception:  # noqa: BLE001
        _LOG.warning("QtNetwork unavailable; a second launch won't be able to raise the window")
        return None
    name = _server_name(data_dir)
    QLocalServer.removeServer(name)  # clear a socket file a crashed prior primary may have left (Unix)
    server = QLocalServer(app)

    def _handle() -> None:
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            if conn is not None:
                conn.disconnected.connect(conn.deleteLater)  # the connection itself is the whole signal
            try:
                on_activate()
            except Exception:  # noqa: BLE001
                _LOG.exception("activation handler failed")

    server.newConnection.connect(_handle)
    if not server.listen(name):
        _LOG.warning("could not listen for second-launch activations (%s): %s", name, server.errorString())
        return None
    _LOG.info("listening for second-launch activations on %s", name)
    return server
