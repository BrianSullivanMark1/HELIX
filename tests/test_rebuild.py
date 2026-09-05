"""The rebuild + relaunch (READ_ME/DREAM.md §6): the Rebuilder adapter (availability, the job file,
the detached spawn, the launch path), every step of scripts/rebuild_and_relaunch.py driven with fakes
(no real build, no real HELIX), the backup/restore of dist/HELIX, the result file, and the webboot
quit hook that answers RebuildRequested."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from helix.adapters import rebuild as rebuild_mod
from helix.adapters.rebuild import Rebuilder
from helix.adapters.signal_bus import SignalBus
from helix.domain.events import RebuildRequested

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location(
        "rebuild_and_relaunch", ROOT / "scripts" / "rebuild_and_relaunch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Settings:
    def __init__(self, **kv):
        self.d = dict(kv)

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


class _Clock:
    def now(self):
        return datetime(2026, 9, 5, 7, 0, 0)


def _source(tmp_path) -> Path:
    """A source tree shaped like the real one: scripts/rebuild_and_relaunch.py and dist/HELIX/HELIX.exe."""
    src = tmp_path / "HELIX_V3"
    (src / "scripts").mkdir(parents=True)
    (src / "scripts" / "rebuild_and_relaunch.py").write_text("# the script", encoding="utf-8")
    (src / "dist" / "HELIX").mkdir(parents=True)
    (src / "dist" / "HELIX" / "HELIX.exe").write_text("exe", encoding="utf-8")
    return src


def _paths(tmp_path, src, *, frozen=True, source=True, python=True):
    return SimpleNamespace(data=tmp_path / "data", root=src / "dist" / "HELIX", is_frozen=frozen,
                           source_root=src if source else None,
                           dev_python=str(tmp_path / "python.exe") if python else None)


def _rebuilder(tmp_path, **kw):
    src = kw.pop("src", None) or _source(tmp_path)
    paths = kw.pop("paths", None) or _paths(tmp_path, src)
    spawned: list[tuple] = []
    rb = Rebuilder(paths, kw.pop("settings", _Settings(web_port=8737, web_token="tok")),
                   spawn=lambda cmd, cwd, log: spawned.append((cmd, cwd, log)),
                   exe=kw.pop("exe", src / "dist" / "HELIX" / "HELIX.exe"),
                   home=kw.pop("home", tmp_path / "home"), clock=_Clock(), **kw)
    return rb, spawned, src


# ----------------------------------------------------------------------------- the adapter
def test_available_only_when_frozen_with_a_source_an_interpreter_the_script_and_the_right_dist(tmp_path):
    rb, _, src = _rebuilder(tmp_path)
    assert rb.available() and rb.why_unavailable() is None
    dev, _, _ = _rebuilder(tmp_path, src=src, paths=_paths(tmp_path, src, frozen=False))
    assert "running from source" in dev.why_unavailable()
    no_src, _, _ = _rebuilder(tmp_path, src=src, paths=_paths(tmp_path, src, source=False))
    assert "source repository" in no_src.why_unavailable()
    # It says where the key lives (helix_settings.json): the Settings card has no field for it.
    assert "helix_settings.json" in no_src.why_unavailable() and "in Settings" not in no_src.why_unavailable()
    no_py, _, _ = _rebuilder(tmp_path, src=src, paths=_paths(tmp_path, src, python=False))
    assert "Python interpreter" in no_py.why_unavailable() and "helix_settings.json" in no_py.why_unavailable()
    (src / "scripts" / "rebuild_and_relaunch.py").unlink()
    no_script, _, _ = _rebuilder(tmp_path, src=src)
    assert "rebuild script is missing" in no_script.why_unavailable()
    (src / "scripts" / "rebuild_and_relaunch.py").write_text("#", encoding="utf-8")
    elsewhere = tmp_path / "copy" / "HELIX.exe"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("exe", encoding="utf-8")
    moved, _, _ = _rebuilder(tmp_path, src=src, exe=elsewhere)
    assert "isn't the build in" in moved.why_unavailable()  # a rebuild there would not replace it


def test_schedule_writes_the_job_and_spawns_the_script_detached_into_the_log(tmp_path):
    rb, spawned, src = _rebuilder(tmp_path)
    job_path = rb.schedule(reason="applied 3 changes")
    assert job_path.parent == tmp_path / "data" / "rebuild" and job_path.name.startswith("job-20260905-070000")
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert set(job) == {"source_root", "python", "exe", "data_dir", "port", "token", "launch", "reason",
                        "requested_at"}  # exactly the §6 contract — nothing beyond it
    assert job["source_root"] == str(src) and job["python"] == str(tmp_path / "python.exe")
    assert job["exe"] == str(src / "dist" / "HELIX" / "HELIX.exe")
    assert job["data_dir"] == str(tmp_path / "data") and job["port"] == 8737 and job["token"] == "tok"
    assert job["launch"] == job["exe"]  # no shortcut on this desktop → the exe itself
    assert job["reason"] == "applied 3 changes" and job["requested_at"] == "2026-09-05T07:00:00"
    cmd, cwd, log = spawned[0]
    assert cmd == [job["python"], str(src / "scripts" / "rebuild_and_relaunch.py"), str(job_path)]
    assert cwd == src and log == tmp_path / "data" / "rebuild" / "rebuild.log"


def test_only_the_newest_job_file_is_kept(tmp_path):
    # The job carries the web token: an older job (served, or never run) must not accumulate.
    rb, _, _ = _rebuilder(tmp_path)
    job_dir = tmp_path / "data" / "rebuild"
    job_dir.mkdir(parents=True)
    stale = job_dir / "job-20260901-230000.json"
    stale.write_text('{"token": "old"}', encoding="utf-8")
    (job_dir / "rebuild.log").write_text("kept", encoding="utf-8")
    job_path = rb.schedule(reason="applied 1 change")
    assert sorted(p.name for p in job_dir.iterdir()) == [job_path.name, "rebuild.log"]
    assert not stale.exists()


def test_schedule_refuses_when_unavailable_and_touches_nothing(tmp_path):
    src = _source(tmp_path)
    rb, spawned, _ = _rebuilder(tmp_path, src=src, paths=_paths(tmp_path, src, frozen=False))
    with pytest.raises(RuntimeError):
        rb.schedule(reason="x")
    assert spawned == [] and not (tmp_path / "data" / "rebuild").exists()


def test_the_launch_path_prefers_the_desktop_shortcut(tmp_path):
    rb, _, src = _rebuilder(tmp_path)
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    (home / "Desktop" / "HELIX.lnk").write_text("lnk", encoding="utf-8")
    assert rb.launch_path() == home / "Desktop" / "HELIX.lnk"
    (home / "OneDrive" / "Desktop").mkdir(parents=True)
    (home / "OneDrive" / "Desktop" / "HELIX.lnk").write_text("lnk", encoding="utf-8")
    assert rb.launch_path() == home / "OneDrive" / "Desktop" / "HELIX.lnk"  # OneDrive first


def test_the_port_and_token_keys_match_the_web_server(tmp_path):
    from helix.api.server import DEFAULT_PORT, PORT_SETTING, TOKEN_SETTING
    from helix.services import dream as dream_mod
    from helix.services import selfdev as selfdev_mod

    assert (rebuild_mod.PORT_SETTING, rebuild_mod.TOKEN_SETTING, rebuild_mod.DEFAULT_PORT) == (
        PORT_SETTING, TOKEN_SETTING, DEFAULT_PORT)
    # The service reads the result file the adapter/script write, without importing the adapter.
    assert dream_mod.REBUILD_RESULT == Path(rebuild_mod.JOB_DIR) / rebuild_mod.RESULT_NAME
    assert rebuild_mod._FROZEN_ENV_POISON == selfdev_mod._FROZEN_ENV_POISON  # one scrub list
    rb, _, _ = _rebuilder(tmp_path, settings=_Settings(web_token="t"))
    assert json.loads(rb.schedule(reason="r").read_text(encoding="utf-8"))["port"] == DEFAULT_PORT


def test_last_result_reads_what_the_script_wrote(tmp_path):
    rb, _, _ = _rebuilder(tmp_path)
    assert rb.last_result() is None
    (tmp_path / "data" / "rebuild").mkdir(parents=True)
    (tmp_path / "data" / "rebuild" / "last_result.json").write_text('{"ok": true}', encoding="utf-8")
    assert rb.last_result() == {"ok": True}


def test_the_real_spawn_is_detached_with_stdio_into_the_log(tmp_path, monkeypatch):
    seen: dict = {}

    class _Popen:
        def __init__(self, cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw

    monkeypatch.setattr(rebuild_mod.subprocess, "Popen", _Popen)
    monkeypatch.setenv("PYTHONHOME", "C:/dist/HELIX/_internal")
    monkeypatch.setattr(rebuild_mod.sys, "frozen", True, raising=False)
    log = tmp_path / "rebuild.log"
    Rebuilder._spawn_detached(["py", "script", "job"], tmp_path, log)
    assert seen["cmd"] == ["py", "script", "job"] and seen["kw"]["cwd"] == str(tmp_path)
    assert seen["kw"]["stderr"] == subprocess.STDOUT and seen["kw"]["stdin"] == subprocess.DEVNULL
    assert "PYTHONHOME" not in seen["kw"]["env"]  # the bundle's env never reaches the dev Python
    if os.name == "nt":
        flags = seen["kw"]["creationflags"]
        assert flags & subprocess.DETACHED_PROCESS and flags & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert seen["kw"]["start_new_session"] is True
    assert "spawning: py script job" in log.read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- the script's steps
def test_load_job_validates_the_required_keys(tmp_path):
    s = _script()
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"source_root": "x", "python": "p"}), encoding="utf-8")
    with pytest.raises(ValueError) as err:
        s.load_job(path)
    assert "exe" in str(err.value) and "token" in str(err.value)
    full = {k: "v" for k in s.REQUIRED_KEYS}
    path.write_text(json.dumps(full), encoding="utf-8")
    assert s.load_job(path) == full


def test_wait_for_exit_polls_until_gone_or_the_deadline():
    s = _script()
    clock = [0.0]
    alive = [True, True, False]

    def is_running():
        return alive.pop(0) if alive else False

    def sleep(sec):
        clock[0] += sec

    assert s.wait_for_exit(is_running, 180, sleep=sleep, now=lambda: clock[0], every_s=2.0) is True
    assert clock[0] == 4.0
    clock[0] = 0.0
    assert s.wait_for_exit(lambda: True, 10, sleep=sleep, now=lambda: clock[0], every_s=2.0) is False
    assert clock[0] >= 10.0


def test_helix_running_reads_tasklist(monkeypatch):
    s = _script()
    monkeypatch.setattr(s.sys, "platform", "win32")
    calls: list = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(stdout='"HELIX.exe","1234","Console","1","250,000 K"\n')

    assert s.helix_running(run=run) is True
    assert calls[0][:4] == ["tasklist", "/FI", "IMAGENAME eq HELIX.exe", "/FO"]
    quiet = lambda cmd, **kw: SimpleNamespace(stdout="INFO: No tasks are running which match the specified criteria.\n")  # noqa: E731
    assert s.helix_running(run=quiet) is False

    def boom(cmd, **kw):
        raise OSError("no tasklist")

    assert s.helix_running(run=boom) is False


def test_backup_and_restore_swap_the_dist_folder_replacing_an_older_prev(tmp_path):
    s = _script()
    dist, prev = tmp_path / "HELIX", tmp_path / "HELIX.prev"
    assert s.backup_dist(dist, prev) is False  # nothing to keep yet
    dist.mkdir()
    (dist / "HELIX.exe").write_text("new-ish", encoding="utf-8")
    prev.mkdir()
    old = prev / "old.txt"
    old.write_text("ancient", encoding="utf-8")
    os.chmod(old, stat.S_IREAD)  # a read-only leftover (git objects in a built app) must not block it
    assert s.backup_dist(dist, prev) is True
    assert not dist.exists() and (prev / "HELIX.exe").read_text(encoding="utf-8") == "new-ish"
    assert not old.exists()
    (dist).mkdir()
    (dist / "broken.exe").write_text("half a build", encoding="utf-8")
    assert s.restore_dist(dist, prev) is True
    assert (dist / "HELIX.exe").read_text(encoding="utf-8") == "new-ish" and not prev.exists()
    assert not (dist / "broken.exe").exists()
    assert s.restore_dist(dist, prev) is False  # no prev left


def test_restore_dist_refuses_plainly_when_the_folder_cannot_be_cleared(tmp_path, monkeypatch):
    # A file in dist still held open (a build that would not die): force_rmtree swallows the
    # error and leaves the folder — the rename must not then crash on a folder that still exists.
    s = _script()
    dist, prev = tmp_path / "HELIX", tmp_path / "HELIX.prev"
    dist.mkdir()
    (dist / "HELIX.exe").write_text("locked", encoding="utf-8")
    prev.mkdir()
    (prev / "HELIX.exe").write_text("old", encoding="utf-8")
    monkeypatch.setattr(s, "force_rmtree", lambda p: None)  # nothing could be removed
    with pytest.raises(OSError) as err:
        s.restore_dist(dist, prev)
    assert "still in use" in str(err.value)
    assert (prev / "HELIX.exe").read_text(encoding="utf-8") == "old"  # the previous build is intact


def test_run_build_reports_exit_code_timeout_and_a_missing_interpreter(tmp_path):
    s = _script()
    seen: list = []

    def ok(cmd, **kw):
        seen.append((cmd, kw["cwd"], kw["timeout"]))
        return SimpleNamespace(returncode=0)

    assert s.run_build(tmp_path, "py", run=ok) == (True, "build.py exited 0")
    assert seen[0] == (["py", "build.py"], str(tmp_path), s.BUILD_TIMEOUT_S)
    assert s.run_build(tmp_path, "py", run=lambda cmd, **kw: SimpleNamespace(returncode=1)) == (
        False, "build.py exited 1")

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    ok_, note = s.run_build(tmp_path, "py", timeout_s=120, run=slow)
    assert ok_ is False and "did not finish within 2 minutes" in note

    def missing(cmd, **kw):
        raise FileNotFoundError("py")

    assert s.run_build(tmp_path, "py", run=missing)[0] is False


def test_wait_for_app_and_the_probe():
    s = _script()
    clock = [0.0]
    answers = [False, False, True]
    assert s.wait_for_app(lambda: answers.pop(0), 240, sleep=lambda x: clock.__setitem__(0, clock[0] + x),
                          now=lambda: clock[0], every_s=3.0) is True
    assert clock[0] == 6.0
    clock[0] = 0.0
    assert s.wait_for_app(lambda: False, 9, sleep=lambda x: clock.__setitem__(0, clock[0] + x),
                          now=lambda: clock[0], every_s=3.0) is False

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    urls: list[str] = []

    def opener(url, timeout=0):
        urls.append(url)
        return _Resp()

    assert s.app_answers(8737, "tok", opener=opener) is True
    assert urls == ["http://127.0.0.1:8737/api/snapshot?t=tok"]

    def refused(url, timeout=0):
        raise ConnectionRefusedError()

    assert s.app_answers(8737, "tok", opener=refused) is False


def test_write_result_has_the_contract_shape(tmp_path):
    s = _script()
    path = tmp_path / "rebuild" / "last_result.json"
    result = s.write_result(path, ok=True, built=True, restored=False, seconds=61.4,
                            message="rebuilt and relaunched", at=datetime(2026, 9, 5, 6, 41))
    assert result == {"ok": True, "built": True, "restored": False, "seconds": 61,
                      "message": "rebuilt and relaunched", "at": "2026-09-05T06:41:00"}
    assert json.loads(path.read_text(encoding="utf-8")) == result


# ----------------------------------------------------------------------------- the whole flow
class _World:
    """A fake machine: a dist folder, a process table, a build that succeeds or fails, an app that
    answers or not — and a record of every step the flow took."""

    def __init__(self, tmp_path, *, build_ok=True, app_answers=True, exits=True, lock_dist=False,
                 kill_works=True):
        self.src = tmp_path / "src"
        self.dist = self.src / "dist" / "HELIX"
        self.dist.mkdir(parents=True)
        (self.dist / "HELIX.exe").write_text("old", encoding="utf-8")
        self.data = tmp_path / "data"
        self.job = {
            "source_root": str(self.src), "python": "py", "exe": str(self.dist / "HELIX.exe"),
            "data_dir": str(self.data), "port": 8737, "token": "tok",
            "launch": str(self.dist / "HELIX.exe"), "reason": "applied 2 changes",
            "requested_at": "2026-09-05T07:00:00",
        }
        self.build_ok, self.app_answers_, self.exits, self.lock_dist = build_ok, app_answers, exits, lock_dist
        self.kill_works = kill_works
        self.start_pid = 4242
        self.steps: list[str] = []
        self.clock = 0.0
        self.running = True  # the old HELIX is still shutting down when the script starts

    def is_running(self):
        self.steps.append("probe-process")
        if self.exits:
            self.running = False
        return self.running

    def sleep(self, sec):
        self.clock += sec

    def now(self):
        return self.clock

    def build(self, source_root, python):
        self.steps.append("build")
        assert source_root == self.src and python == "py"
        if self.build_ok:
            self.dist.mkdir(parents=True, exist_ok=True)
            (self.dist / "HELIX.exe").write_text("new", encoding="utf-8")
            return True, "build.py exited 0"
        self.dist.mkdir(parents=True, exist_ok=True)
        (self.dist / "half.dll").write_text("x", encoding="utf-8")
        return False, "build.py exited 1"

    def start(self, launch):
        self.steps.append("start:" + Path(launch).read_text(encoding="utf-8"))
        self.running = True
        return self.start_pid  # the pid of the build we spawned (None = a shell launch, no pid)

    def probe(self):
        self.steps.append("probe-app")
        return self.app_answers_

    def kill(self):
        self.steps.append("kill")
        if self.kill_works:
            self.running = False

    def run(self, patch=None):
        s = _script()
        if patch is not None:
            patch(s)
        if self.lock_dist:
            s = _script()
            original = s.backup_dist

            def locked(dist, prev):
                raise OSError("WinError 32: the folder is in use")

            s.backup_dist = locked
            assert original is not locked
        return s.rebuild(self.job, is_running=self.is_running, sleep=self.sleep, now=self.now,
                         build=self.build, start=self.start, probe=self.probe, kill=self.kill,
                         clock=lambda: datetime(2026, 9, 5, 6, 41)), s

    def result_file(self):
        return json.loads((self.data / "rebuild" / "last_result.json").read_text(encoding="utf-8"))


def test_a_good_rebuild_backs_up_builds_relaunches_and_reports(tmp_path):
    w = _World(tmp_path)
    result, _ = w.run()
    assert result == {"ok": True, "built": True, "restored": False, "seconds": 0,
                      "message": "rebuilt and relaunched", "at": "2026-09-05T06:41:00"}
    assert w.result_file() == result
    assert w.steps == ["probe-process", "build", "start:new", "probe-app"]
    assert (w.dist / "HELIX.exe").read_text(encoding="utf-8") == "new"
    assert (w.src / "dist" / "HELIX.prev" / "HELIX.exe").read_text(encoding="utf-8") == "old"  # kept


def test_an_app_that_never_exits_aborts_without_touching_anything(tmp_path):
    w = _World(tmp_path, exits=False)
    result, _ = w.run()
    assert result["ok"] is False and result["built"] is False and result["restored"] is False
    assert "still running after 180 s" in result["message"]
    assert "build" not in w.steps and (w.dist / "HELIX.exe").read_text(encoding="utf-8") == "old"
    assert w.clock >= 180.0


def test_a_failed_build_restores_the_previous_build_and_relaunches_it(tmp_path):
    w = _World(tmp_path, build_ok=False)
    result, _ = w.run()
    assert result["ok"] is False and result["built"] is False and result["restored"] is True
    assert "build.py exited 1" in result["message"] and "restored the previous build" in result["message"]
    assert w.steps == ["probe-process", "build", "start:old"]
    assert (w.dist / "HELIX.exe").read_text(encoding="utf-8") == "old" and not (w.dist / "half.dll").exists()
    assert not (w.src / "dist" / "HELIX.prev").exists()


def test_a_new_build_that_never_answers_is_stopped_and_the_old_one_comes_back(tmp_path):
    w = _World(tmp_path, app_answers=False)
    result, _ = w.run()
    assert result["ok"] is False and result["built"] is True and result["restored"] is True
    assert "did not answer within 240 s" in result["message"]
    assert w.steps[:3] == ["probe-process", "build", "start:new"]
    assert "kill" in w.steps and w.steps[-1] == "start:old"
    assert (w.dist / "HELIX.exe").read_text(encoding="utf-8") == "old"


def test_a_new_build_that_cannot_be_stopped_leaves_both_builds_in_place_and_says_so(tmp_path):
    # taskkill did not end it: its files are still held, so swapping the folder would half-delete
    # the new build and crash on the rename. Nothing is touched; the message names where prev is.
    w = _World(tmp_path, app_answers=False, kill_works=False)
    w.running = False  # the OLD app has exited by the time the script starts
    w.exits = False    # …but once the NEW one is started it never goes away

    def is_running():
        w.steps.append("probe-process")
        return w.running

    w.is_running = is_running
    result, _ = w.run()
    assert result["ok"] is False and result["built"] is True and result["restored"] is False
    assert "could not be stopped within 60 s" in result["message"] and "HELIX.prev" in result["message"]
    assert "kill" in w.steps and w.steps[-1] != "start:old"
    assert (w.dist / "HELIX.exe").read_text(encoding="utf-8") == "new"  # untouched
    assert (w.src / "dist" / "HELIX.prev" / "HELIX.exe").read_text(encoding="utf-8") == "old"  # intact
    assert w.result_file() == result


def test_a_restore_that_cannot_clear_the_folder_is_reported_not_crashed(tmp_path):
    # Killed, but a handle lingers: restore_dist raises, the flow finishes with a plain result
    # (not "the rebuild script crashed" with restored=False and a half-deleted dist).
    w = _World(tmp_path, app_answers=False)
    result, _ = w.run(patch=lambda s: setattr(s, "force_rmtree", lambda p: None))
    assert result["ok"] is False and result["built"] is True and result["restored"] is False
    assert "could not put the previous build back" in result["message"] and "still in use" in result["message"]
    assert "kill" in w.steps and "start:old" not in w.steps  # nothing started on top of a locked folder
    assert (w.src / "dist" / "HELIX.prev" / "HELIX.exe").read_text(encoding="utf-8") == "old"
    failed = _World(tmp_path / "b", build_ok=False)
    result, _ = failed.run(patch=lambda s: setattr(s, "force_rmtree", lambda p: None))
    assert result["ok"] is False and result["restored"] is False
    assert "the build failed (build.py exited 1); could not put the previous build back" in result["message"]


def test_a_locked_dist_folder_relaunches_the_old_build_and_says_so(tmp_path):
    w = _World(tmp_path, lock_dist=True)
    result, _ = w.run()
    assert result["ok"] is False and result["built"] is False and result["restored"] is False
    assert "could not set the previous build aside" in result["message"]
    assert w.steps == ["probe-process", "start:old"]


def test_main_runs_a_job_file_end_to_end_with_fakes(tmp_path, monkeypatch, capsys):
    s = _script()
    w = _World(tmp_path)
    job = tmp_path / "job.json"
    job.write_text(json.dumps(w.job), encoding="utf-8")
    monkeypatch.setattr(s, "helix_running", lambda exe_name: False)
    monkeypatch.setattr(s, "run_build", w.build)
    monkeypatch.setattr(s, "start_app", w.start)
    monkeypatch.setattr(s, "app_answers", lambda port, token: True)
    monkeypatch.setattr(s.time, "sleep", lambda x: None)
    assert s.main([str(job)]) == 0
    assert w.result_file()["ok"] is True
    assert not job.exists()  # the job carried the web token; the result and the log are the record
    out = capsys.readouterr().out
    assert "rebuild requested: applied 2 changes" in out and "rebuilt and relaunched" in out
    assert "tok" not in out  # the token is never logged
    assert s.main([]) == 2 and s.main([str(tmp_path / "missing.json")]) == 2
    # A crashed rebuild still writes its result AND removes the job.
    job.write_text(json.dumps(w.job), encoding="utf-8")
    monkeypatch.setattr(s, "rebuild", lambda job: 1 / 0)
    assert s.main([str(job)]) == 1
    assert "crashed" in w.result_file()["message"] and not job.exists()


def test_the_script_is_stdlib_only():
    import ast

    tree = ast.parse((ROOT / "scripts" / "rebuild_and_relaunch.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert not (names - {"json", "os", "shutil", "stat", "subprocess", "sys", "time", "traceback",
                         "urllib", "datetime", "pathlib", "__future__"}), names


# ----------------------------------------------------------------------------- the quit hook
def test_webboot_answers_rebuild_requested_with_the_graceful_quit(monkeypatch):
    webboot = pytest.importorskip("helix.app.webboot")
    monkeypatch.setattr(webboot, "_REBUILD_QUIT_DELAY_S", 0.0)
    bus = SignalBus()
    app = SimpleNamespace(state=SimpleNamespace())
    quit_calls: list[int] = []
    done = threading.Event()

    def graceful_quit():
        quit_calls.append(1)
        done.set()

    handler = webboot.wire_rebuild_quit(bus, app, graceful_quit)
    assert callable(handler)
    bus.publish(RebuildRequested(reason="applied 3 changes"))
    assert app.state.quitting is True  # the snapshot probe answers 503 from this moment
    assert done.wait(5.0) and quit_calls == [1]


def test_a_silent_new_build_is_stopped_by_its_own_pid_never_by_image_name(tmp_path):
    """The 20:43 incident: an image-name taskkill would stop EVERY HELIX.exe on the machine, the
    owner's live one included. The script now stops only the pid it started, and with no pid (a
    shell launch) it leaves both builds in place and says so."""
    s = _script()
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert s.kill_pid(4242, run=fake_run) is True
    assert calls == [["taskkill", "/F", "/T", "/PID", "4242"]]
    assert s.kill_pid(None, run=fake_run) is False and len(calls) == 1
    assert not hasattr(s, "kill_helix")
    src = (ROOT / "scripts" / "rebuild_and_relaunch.py").read_text(encoding="utf-8")
    assert "/IM" not in src
    # A launch that gave no pid (a shortcut through the shell): a silent build is reported, never
    # killed by name, and the previous build stays untouched beside it.
    w = _World(tmp_path, app_answers=False)
    w.start_pid = None
    result, _ = w.run()
    assert "not stopped" in result["message"] and "kill" not in w.steps
    assert (w.src / "dist" / "HELIX.prev" / "HELIX.exe").read_text(encoding="utf-8") == "old"
