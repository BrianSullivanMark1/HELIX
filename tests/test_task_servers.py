"""App servers must not outlive the HELIX that launched them. The trap this pins: a build's server
kept running after HELIX quit (a rebuild, a crash), with the build's folder as its working
directory, so Windows refused to move the folder and "remove the music player" failed with "it's
open or running" long after the HELIX that opened it was gone. Now every launch records a pid
file, quitting stops the children, boot sweeps stale pid files, and a delete releases first."""
from __future__ import annotations

import json
from pathlib import Path

from helix.domain.models import App, AppKind, BuildKind
from helix.services import tasks as tasks_mod
from helix.services.tasks import PID_FILE, TaskService, _is_python_image


class _Proc:
    _next = 4000

    def __init__(self, *args, **kwargs):
        _Proc._next += 1
        self.pid = _Proc._next
        self.args = args
        self.kwargs = kwargs
        self.terminated = False
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        self.alive = False

    def wait(self):
        return 0


class _Builds:
    def __init__(self, root: Path, *slugs: str):
        self.dir = root
        self._apps = []
        for s in slugs:
            (root / s).mkdir(parents=True, exist_ok=True)
            (root / s / "main.py").write_text("print('hi')\n", encoding="utf-8")
            self._apps.append(App(slug=s, name=s.title(), request="", kind=AppKind.PYTHON,
                                  build_kind=BuildKind.APP, entry_point="main.py"))

    def list(self):
        return list(self._apps)

    def workspace(self, slug: str) -> Path:
        return self.dir / slug


class _World:
    """A fake process table: pid -> image path; terminate() removes the pid."""

    def __init__(self, **images):
        self.images = dict(images)
        self.terminated: list[int] = []

    def probe(self, pid):
        return self.images.get(pid)

    def terminate(self, pid):
        self.terminated.append(pid)
        self.images.pop(pid, None)


def _svc(tmp_path, *slugs, world=None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(tasks_mod.subprocess, "Popen", _Proc)
    world = world or _World()
    builds = _Builds(tmp_path / "builds", *slugs)
    return TaskService(builds, probe=world.probe, terminate=world.terminate), builds, world


def test_run_records_the_servers_pid_and_stop_clears_it(tmp_path, monkeypatch):
    svc, builds, _ = _svc(tmp_path, "music-player", monkeypatch=monkeypatch)
    assert svc.run("music-player", port=3001, headless=True)
    pid_file = builds.workspace("music-player") / PID_FILE
    assert json.loads(pid_file.read_text())["pid"] == svc._procs["music-player"].pid
    assert svc.is_running("music-player")
    svc.stop("music-player")
    assert not svc.is_running("music-player") and not pid_file.exists()


def test_stop_all_terminates_every_child_at_quit(tmp_path, monkeypatch):
    svc, builds, _ = _svc(tmp_path, "a", "b", monkeypatch=monkeypatch)
    svc.run("a", port=1, headless=True)
    svc.run("b", headless=False)
    procs = dict(svc._procs)
    assert svc.stop_all() == 2
    assert all(p.terminated for p in procs.values())
    assert not any((builds.workspace(s) / PID_FILE).exists() for s in ("a", "b"))
    assert svc.stop_all() == 0  # idempotent


def test_reap_kills_an_orphaned_python_server_recorded_in_the_pid_file(tmp_path):
    world = _World(**{"14676": None})
    world.images = {14676: r"C:\Python311\pythonw.EXE"}
    svc, builds, world = _svc(tmp_path, "music-player", world=world)
    ws = builds.workspace("music-player")
    (ws / PID_FILE).write_text(json.dumps({"pid": 14676}), encoding="utf-8")
    assert svc.reap("music-player") is True
    assert world.terminated == [14676] and not (ws / PID_FILE).exists()
    assert svc.release("music-player") is False  # nothing left to release
    assert svc.reap("music-player") is False      # and the file is gone


def test_reap_never_kills_a_recycled_pid_that_belongs_to_something_else(tmp_path):
    world = _World()
    world.images = {777: r"C:\Program Files\Google\Chrome\Application\chrome.exe"}
    svc, builds, world = _svc(tmp_path, "notes", world=world)
    ws = builds.workspace("notes")
    (ws / PID_FILE).write_text(json.dumps({"pid": 777}), encoding="utf-8")
    assert svc.reap("notes") is False
    assert world.terminated == [] and not (ws / PID_FILE).exists()  # stale file cleared, nothing shot


def test_reap_tolerates_garbage_and_dead_pids(tmp_path):
    svc, builds, world = _svc(tmp_path, "x")
    ws = builds.workspace("x")
    (ws / PID_FILE).write_text("not json", encoding="utf-8")
    assert svc.reap("x") is False
    (ws / PID_FILE).write_text(json.dumps({"pid": 99999}), encoding="utf-8")  # exited long ago
    assert svc.reap("x") is False and not (ws / PID_FILE).exists()
    assert svc.reap("no-such-build") is False


def test_boot_sweep_reaps_only_untracked_pid_files(tmp_path, monkeypatch):
    world = _World()
    world.images = {501: r"C:\py\python.exe", 502: r"C:\py\python.exe"}
    svc, builds, world = _svc(tmp_path, "old", "live", world=world, monkeypatch=monkeypatch)
    (builds.workspace("old") / PID_FILE).write_text(json.dumps({"pid": 501}), encoding="utf-8")
    svc.run("live", port=2, headless=True)  # tracked by THIS HELIX — never swept
    live_pid = svc._procs["live"].pid
    (builds.workspace("live") / PID_FILE).write_text(json.dumps({"pid": live_pid}), encoding="utf-8")
    assert svc.reap_orphans() == ["old"]
    assert world.terminated == [501]
    assert svc.is_running("live")


def test_release_reports_whether_anything_was_running(tmp_path, monkeypatch):
    svc, builds, world = _svc(tmp_path, "srv", monkeypatch=monkeypatch)
    assert svc.release("srv") is False
    svc.run("srv", port=3, headless=True)
    assert svc.release("srv") is True
    assert not svc.is_running("srv")


def test_python_image_check():
    assert _is_python_image(r"C:\Users\b\AppData\Local\Programs\Python\Python311\pythonw.EXE")
    assert _is_python_image("/usr/bin/python3")
    assert not _is_python_image(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    assert not _is_python_image(None) and not _is_python_image("")


def test_process_image_answers_for_this_process_and_not_for_nonsense():
    from helix.services.tasks import process_image
    import os

    mine = process_image(os.getpid())
    assert mine and _is_python_image(mine)
    assert process_image(0) is None and process_image(-5) is None


def test_confirm_delete_releases_the_folder_before_removing(tmp_path, monkeypatch):
    from helix.services.tools import ToolRegistry

    svc, builds, world = _svc(tmp_path, "music-player", monkeypatch=monkeypatch)
    svc.run("music-player", port=3001, headless=True)
    proc = svc._procs["music-player"]
    order: list[str] = []

    class _Forge:
        def remove_build(self, name):
            order.append(f"remove:{name}:running={svc.is_running('music-player')}")
            return True

    reg = ToolRegistry(_Forge(), builds, tasks=svc)
    assert reg.confirm_delete("Music Player") == "Removed 'Music Player'."
    assert proc.terminated and order == ["remove:Music Player:running=False"]
