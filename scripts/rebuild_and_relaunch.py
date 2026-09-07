"""rebuild_and_relaunch.py — rebuild the frozen HELIX and bring it back (READ_ME/DREAM.md §6).

Spawned DETACHED by helix/adapters/rebuild.py after a dream session applied changes to the source;
the app quits right after spawning it. Pure stdlib — it runs outside the app, on the dev Python.

    python scripts/rebuild_and_relaunch.py <job.json>

The job (written by the Rebuilder): {"source_root", "python", "exe", "data_dir", "port", "token",
"launch", "reason", "requested_at"}. The flow, every step reversible:

  1. wait until no HELIX.exe runs (<= 180 s; else abort and log — nothing is touched);
  2. set dist/HELIX aside as dist/HELIX.prev (an older .prev is replaced);
  3. run build.py in source_root (output into this log, <= 40 min);
  4. on success start `launch` and wait for http://127.0.0.1:<port>/api/snapshot?t=<token> to answer
     200 (<= 240 s);
  5. on a failed build, or an app that never answers (it is stopped first), put dist/HELIX.prev back
     as dist/HELIX and start THAT;
  6. write data/rebuild/last_result.json {"ok", "built", "restored", "seconds", "message", "at"} —
     the morning report reads it.

Every step is a function with its edges injectable (the process list, the clock, the build, the
launcher, the liveness probe), so tests drive the whole flow with fakes and no real build.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

EXE_NAME = "HELIX.exe"
EXIT_WAIT_S = 180.0      # how long the old app gets to exit before we give up on the night
BUILD_TIMEOUT_S = 2400.0  # build.py (PyInstaller + the web face) — forty minutes
APP_WAIT_S = 240.0       # the relaunched app must answer within this
KILL_WAIT_S = 60.0       # after taskkill, before the folder can be swapped back
POLL_S = 2.0
PREV_SUFFIX = ".prev"
RESULT_NAME = "last_result.json"
REQUIRED_KEYS = ("source_root", "python", "exe", "data_dir", "port", "token", "launch")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}", flush=True)


def load_job(path: Path) -> dict:
    """The job file, validated: every required key present and non-empty (a job the Rebuilder
    did not write is refused rather than guessed at)."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("the job file is not an object")
    missing = [k for k in REQUIRED_KEYS if k not in data or data[k] in (None, "")]
    if missing:
        raise ValueError("the job file is missing " + ", ".join(missing))
    return data


# ----- processes -----
def helix_running(exe_name: str = EXE_NAME, *, run=subprocess.run) -> bool:
    """Is any process with this image name alive? tasklist is a Windows built-in — stdlib subprocess,
    no third-party module. Off Windows there is no frozen HELIX to wait for."""
    if sys.platform != "win32":
        return False
    try:
        proc = run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/FO", "CSV", "/NH"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=30, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return exe_name.lower() in (proc.stdout or "").lower()


def wait_for_exit(is_running, timeout_s: float = EXIT_WAIT_S, *, sleep=time.sleep,
                  now=time.monotonic, every_s: float = POLL_S) -> bool:
    """True once is_running() is False; False when it still is at the deadline."""
    deadline = now() + timeout_s
    while is_running():
        if now() >= deadline:
            return False
        sleep(every_s)
    return True


def kill_pid(pid: int | None, *, run=subprocess.run) -> bool:
    """Stop the ONE process this script started (and its children) by pid — never by image name.
    An image-name kill would stop every HELIX on the machine, including a live one that has nothing
    to do with this rebuild; with no pid (a shortcut launch) there is nothing safe to stop, so the
    caller leaves both builds in place and says so. True when a kill was issued."""
    if sys.platform != "win32" or not pid:
        return False
    try:
        run(["taskkill", "/F", "/T", "/PID", str(int(pid))], capture_output=True, text=True,
            timeout=60, creationflags=_NO_WINDOW)
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


# ----- the dist folder -----
def force_rmtree(path: Path) -> None:
    """Remove a build folder even when it holds read-only files (git objects in built apps), the
    way build.py does."""
    path = Path(path)
    if not path.exists():
        return

    def _onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onerror)


def backup_dist(dist: Path, prev: Path) -> bool:
    """Set the current build aside as `prev` (an older prev is replaced). False = there was no
    current build to keep. Raises OSError when the folder is locked — the caller relaunches the old
    build and reports, because nothing has been touched yet."""
    dist, prev = Path(dist), Path(prev)
    if not dist.exists():
        return False
    force_rmtree(prev)
    dist.rename(prev)
    return True


def restore_dist(dist: Path, prev: Path) -> bool:
    """Put the previous build back (removing whatever a failed build left). False = no prev.
    Raises OSError when `dist` cannot be cleared (a file in it is still held open by a build that
    would not die) — the caller reports that plainly rather than crashing on the rename."""
    dist, prev = Path(dist), Path(prev)
    if not prev.exists():
        return False
    force_rmtree(dist)
    if dist.exists():
        raise OSError(f"{dist} is still in use and could not be cleared")
    prev.rename(dist)
    return True


# ----- building + launching -----
def run_build(source_root: Path, python: str, *, timeout_s: float = BUILD_TIMEOUT_S,
              run=subprocess.run) -> tuple[bool, str]:
    """`<python> build.py` in the source root. Output is NOT captured: this script's stdout is the
    rebuild log, so the build narrates straight into it."""
    try:
        proc = run([python, "build.py"], cwd=str(source_root), timeout=timeout_s,
                   creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, f"build.py did not finish within {int(timeout_s // 60)} minutes"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"build.py could not run: {exc}"
    code = getattr(proc, "returncode", 1)
    return code == 0, f"build.py exited {code}"


def start_app(launch: Path) -> int | None:
    """Start HELIX detached and return its pid. The exe is launched directly whenever it exists
    (it is what the desktop shortcut points at, with no arguments), because only a process we
    spawned ourselves has a pid we may later stop; a shortcut goes through the shell and returns
    None — such a launch is never killed, only left in place."""
    launch = Path(launch)
    if launch.suffix.lower() == ".lnk" and hasattr(os, "startfile"):
        os.startfile(str(launch))  # noqa: S606 — a shortcut needs ShellExecute
        return None
    kwargs: dict = {"cwd": str(launch.parent), "close_fds": True,
                    "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([str(launch)], **kwargs).pid


def app_answers(port: int, token: str, *, opener=urllib.request.urlopen, timeout: float = 3.0) -> bool:
    """One authenticated GET of /api/snapshot: 200 means the relaunched HELIX is alive."""
    try:
        with opener(f"http://127.0.0.1:{int(port)}/api/snapshot?t={token}", timeout=timeout) as resp:
            return getattr(resp, "status", 0) == 200
    except Exception:  # noqa: BLE001 — refused / reset / 503 during boot all mean "not yet"
        return False


def wait_for_app(probe, timeout_s: float = APP_WAIT_S, *, sleep=time.sleep, now=time.monotonic,
                 every_s: float = POLL_S) -> bool:
    deadline = now() + timeout_s
    while True:
        if probe():
            return True
        if now() >= deadline:
            return False
        sleep(every_s)


# ----- the result -----
def write_result(path: Path, *, ok: bool, built: bool, restored: bool, seconds: float,
                 message: str, at: datetime | None = None) -> dict:
    result = {
        "ok": bool(ok), "built": bool(built), "restored": bool(restored),
        "seconds": int(round(seconds)), "message": str(message),
        "at": (at or datetime.now().astimezone()).isoformat(timespec="seconds"),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
    tmp.replace(path)
    return result


# ----- the flow -----
def rebuild(job: dict, *, is_running=None, sleep=time.sleep, now=time.monotonic, build=None,
            start=None, probe=None, kill=None, clock=None) -> dict:
    """The whole night's tail end, with every edge injectable (resolved at call time, so a test can
    stand in for a step through the module too). Returns the result dict written to
    data/rebuild/last_result.json."""
    build = build or run_build
    start = start or start_app
    source_root = Path(job["source_root"])
    python = str(job["python"])
    exe = Path(job["exe"])
    exe_name = str(job.get("exe_name") or exe.name or EXE_NAME)
    dist = exe.parent
    prev = dist.with_name(dist.name + PREV_SUFFIX)
    launch = Path(job["launch"])
    data_dir = Path(job["data_dir"])
    # Only a process we started has a pid we may stop later: a shortcut launch hands the exe to the
    # shell and gives none back, so the exe is preferred whenever it is there to launch.
    started: dict = {"pid": None}

    def _start(target: Path):
        chosen = exe if exe.is_file() and target.suffix.lower() == ".lnk" else target
        started["pid"] = start(chosen)
        return started["pid"]

    result_path = data_dir / "rebuild" / RESULT_NAME
    is_running = is_running or (lambda: helix_running(exe_name))
    probe = probe or (lambda: app_answers(int(job["port"]), str(job["token"])))
    kill = kill or (lambda: kill_pid(started["pid"]))
    clock = clock or (lambda: datetime.now().astimezone())
    t0 = now()

    def finish(ok: bool, built: bool, restored: bool, message: str) -> dict:
        log(message)
        return write_result(result_path, ok=ok, built=built, restored=restored,
                            seconds=now() - t0, message=message, at=clock())

    log(f"rebuild requested: {job.get('reason') or '(no reason given)'}")
    log(f"waiting for {exe_name} to exit")
    if not wait_for_exit(is_running, EXIT_WAIT_S, sleep=sleep, now=now):
        return finish(False, False, False,
                      f"{exe_name} was still running after {int(EXIT_WAIT_S)} s; nothing was rebuilt")
    log(f"setting {dist} aside as {prev.name}")
    try:
        had_backup = backup_dist(dist, prev)
    except OSError as exc:
        _start(launch)
        return finish(False, False, False,
                      f"could not set the previous build aside ({exc}); relaunched the old build")
    log(f"running build.py in {source_root}")
    ok, note = build(source_root, python)
    built = bool(ok) and (dist / exe.name).is_file()
    if not built:
        why = note if not ok else f"{exe.name} is missing after the build"
        try:
            restored = restore_dist(dist, prev) if had_backup else False
        except OSError as exc:
            return finish(False, False, False, f"the build failed ({why}); could not put the "
                          f"previous build back ({exc}) — it is at {prev.name}")
        _start(launch)
        return finish(False, False, restored, f"the build failed ({why}); "
                      + ("restored the previous build and relaunched it" if restored
                         else "there was no previous build to restore"))
    log(f"built; starting {launch}")
    _start(launch)
    if wait_for_app(probe, APP_WAIT_S, sleep=sleep, now=now):
        return finish(True, True, False, "rebuilt and relaunched")
    silent = f"the new build did not answer within {int(APP_WAIT_S)} s"
    if started["pid"] is None:
        # A shell launch gave us nothing to stop safely; never reach for an image-name kill.
        return finish(False, True, False, f"{silent}; it was started through the shell so it was "
                      f"not stopped — the previous build is untouched at {prev.name}")
    log(f"the new build did not answer; stopping pid {started['pid']}")
    kill()
    if not wait_for_exit(is_running, KILL_WAIT_S, sleep=sleep, now=now):
        # Its files are still held open: swapping the folder now would half-delete it and crash on
        # the rename. Leave both builds in place and say so — the previous one is one rename away.
        return finish(False, True, False, f"{silent} and could not be stopped within "
                      f"{int(KILL_WAIT_S)} s; the previous build is untouched at {prev.name}")
    try:
        restored = restore_dist(dist, prev) if had_backup else False
    except OSError as exc:
        return finish(False, True, False, f"{silent}; could not put the previous build back "
                      f"({exc}) — it is at {prev.name}")
    if restored:
        _start(launch)
    return finish(False, True, restored, f"{silent}; "
                  + ("restored the previous build and relaunched it" if restored
                     else "there was no previous build to restore"))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) != 1:
        print("usage: rebuild_and_relaunch.py <job.json>", file=sys.stderr)
        return 2
    try:
        job = load_job(Path(argv[0]))
    except (OSError, ValueError) as exc:
        log(f"cannot read the job: {exc}")
        return 2
    try:
        result = rebuild(job)
    except Exception:  # noqa: BLE001 — the log is the only witness at 6 AM
        log("the rebuild crashed:\n" + traceback.format_exc())
        try:
            write_result(Path(job["data_dir"]) / "rebuild" / RESULT_NAME, ok=False, built=False,
                         restored=False, seconds=0, message="the rebuild script crashed — see rebuild.log")
        except OSError:
            pass
        return 1
    finally:
        # The job carries the web token and has served: the result file and the log are the record.
        try:
            Path(argv[0]).unlink(missing_ok=True)
        except OSError:
            log("could not remove the job file")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
