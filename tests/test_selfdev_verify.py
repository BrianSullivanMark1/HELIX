"""SelfDevService.verify — the full-suite check before an UNATTENDED merge (READ_ME/DREAM.md §5): a
fresh worktree of the branch, the suite on the configured interpreter, the captured tail, the web
build when it can be attempted, and approve(verified=True). Real git repos + a fake suite runner, so
no test suite runs inside a test — except the one pin of the real runner on a two-test toy tree."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from helix.adapters.git_repo import GitRepo
from helix.adapters.system_clock import SystemClock
from helix.ports.coder import CoderResult
from helix.services import selfdev as selfdev_mod
from helix.services.selfdev import SelfDevService, run_test_suite

GIT = GitRepo()


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class _Settings:
    def __init__(self):
        self._d = {}

    def get(self, k, default=None):
        return self._d.get(k, default)

    def set(self, k, v):
        self._d[k] = v


class _Coder:
    name = "fake"

    def __init__(self, fn):
        self._fn = fn

    def available(self):
        return True

    def run_task(self, repo_dir, prompt, *, on_progress=None, cancel=None, model=None):
        self._fn(Path(repo_dir))
        return CoderResult(ok=True, summary="ok")


def _repo() -> Path:
    repo = Path(tempfile.mkdtemp()) / "r"
    GIT.init(repo)
    _w(repo / "helix/services/conversation.py", "# conversation")
    _w(repo / "web/src/App.tsx", "// face")
    _w(repo / "tests/test_x.py", "def test_x():\n    pass\n")
    GIT.commit_all(repo, "base")
    return repo


class _Suite:
    """A fake SuiteRunner: records (worktree, python, timeout) and answers as told."""

    def __init__(self, ok=True, tail="41 passed", raises=None):
        self.ok, self.tail, self.raises = ok, tail, raises
        self.calls: list[tuple] = []

    def __call__(self, worktree, python, timeout_s):
        self.calls.append((Path(worktree), python, timeout_s))
        if self.raises:
            raise self.raises
        return self.ok, self.tail


def _gate(repo, fn, *, suite=None, web=None, python=None) -> SelfDevService:
    return SelfDevService(
        _Coder(fn), GIT, _Settings(), SystemClock(), repo, worktrees_dir=repo.parent / "wt",
        smoke_check=lambda p: (True, ""), data_dir=repo / "data", python=python,
        suite_runner=suite, web_build=web,
    )


def test_verify_runs_the_suite_in_a_fresh_worktree_on_the_configured_interpreter():
    repo = _repo()
    suite = _Suite()
    svc = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# improved"),
                suite=suite, python="C:/dev/python.exe")
    pc = svc.propose("improve conversation")
    assert svc.verify(pc.branch) == (True, "41 passed")
    (wt, python, timeout) = suite.calls[0]
    assert python == "C:/dev/python.exe" and timeout == selfdev_mod.VERIFY_TIMEOUT_S
    assert wt.name == pc.branch.replace("/", "_") + "-verify" and wt != repo
    assert not wt.exists()  # the worktree is gone again…
    assert pc.branch in GIT.list_branches(repo, "selfdev/")  # …and the branch untouched: verify never merges
    assert GIT.is_clean(repo)


def test_verify_is_red_when_the_suite_is_red_or_cannot_run():
    repo = _repo()
    red = _Suite(ok=False, tail="FAILED tests/test_x.py::test_x\n1 failed, 40 passed")
    svc = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# improved"), suite=red)
    pc = svc.propose("improve conversation")
    ok, tail = svc.verify(pc.branch)
    assert ok is False and "1 failed" in tail
    broken = _gate(repo, lambda wt: None, suite=_Suite(raises=RuntimeError("pytest exploded")))
    ok, tail = broken.verify(pc.branch)
    assert ok is False and "pytest exploded" in tail
    assert svc.verify("selfdev/nothing-here") == (False, "no such pending change.")
    assert svc.verify("HEAD") == (False, "no such pending change.")  # an arbitrary ref is refused


def _face_branch(repo: Path) -> str:
    """A selfdev/ branch that touches web/. propose() can never make one today — the Constitution's
    growable surface is .py only, so a coder's edit under web/ is refused at the gate — but §5 asks
    verify() to build the face when a branch DOES touch it (a hand-made branch, or the day that
    surface opens), so the branch is committed straight through git here."""
    base = GIT.current_branch(repo)
    branch = "selfdev/brighter-chip-0905-000000"
    GIT.create_branch(repo, branch)
    _w(repo / "web/src/App.tsx", "// brighter chip")
    GIT.commit_all(repo, "selfdev: brighter chip")
    GIT.checkout(repo, base)
    return branch


def test_a_branch_that_touched_the_face_also_builds_it_when_that_can_be_attempted():
    repo = _repo()
    branch = _face_branch(repo)
    built: list[Path] = []

    def web_ok(wt):
        built.append(Path(wt))
        return True, "vite built"

    svc = _gate(repo, lambda wt: None, suite=_Suite(), web=web_ok)
    assert svc.verify(branch)[0] is True and len(built) == 1
    assert built[0].name == branch.replace("/", "_") + "-verify" and not built[0].exists()
    # A red web build is a red verify, named as such.
    svc_red = _gate(repo, lambda wt: None, suite=_Suite(), web=lambda wt: (False, "tsc: error TS2304"))
    ok, tail = svc_red.verify(branch)
    assert ok is False and tail.startswith("the web face failed to build:") and "TS2304" in tail
    # None = could not be attempted (no npm / no node_modules in a fresh worktree): skipped, not failed.
    svc_skip = _gate(repo, lambda wt: None, suite=_Suite(), web=lambda wt: None)
    assert svc_skip.verify(branch) == (True, "41 passed")
    # …and a red SUITE is red before the face is even looked at.
    looked: list = []
    svc_suite_red = _gate(repo, lambda wt: None, suite=_Suite(ok=False, tail="1 failed"),
                          web=lambda wt: looked.append(wt) or (True, ""))
    assert svc_suite_red.verify(branch) == (False, "1 failed") and looked == []


def test_a_draft_that_left_the_face_alone_never_runs_the_web_build():
    repo = _repo()
    touched: list = []
    svc = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# improved"),
                suite=_Suite(), web=lambda wt: touched.append(wt) or (False, "should not run"))
    pc = svc.propose("improve conversation")
    assert svc.verify(pc.branch) == (True, "41 passed") and touched == []


def test_approve_says_when_the_full_suite_was_green_and_is_otherwise_unchanged():
    repo = _repo()
    svc = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# improved"))
    a = svc.propose("improve conversation")
    assert svc.approve(a.branch) == "Applied. Restart HELIX to load the new version."
    b = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# better")).propose("again")
    assert svc.approve(b.branch, verified=True) == (
        "Applied. Restart HELIX to load the new version. (full test suite green)")


def test_the_default_smoke_check_compiles_with_the_configured_interpreter(monkeypatch):
    # Frozen, sys.executable is HELIX.exe — an app, not a compiler. The gate binds the default smoke
    # check to the interpreter it was handed (AppPaths.dev_python) so approve() never launches a
    # second HELIX to byte-compile a branch.
    repo = _repo()
    seen: list = []

    def fake_compile(worktree, python=None):
        seen.append((Path(worktree), python))
        return True, ""

    monkeypatch.setattr(selfdev_mod, "compile_smoke_check", fake_compile)
    svc = SelfDevService(_Coder(lambda wt: None), GIT, _Settings(), SystemClock(), repo,
                         worktrees_dir=repo.parent / "wt", data_dir=repo / "data",
                         python="C:/dev/python.exe")
    assert svc._smoke(repo) == (True, "") and seen == [(repo, "C:/dev/python.exe")]
    plain = SelfDevService(_Coder(lambda wt: None), GIT, _Settings(), SystemClock(), repo,
                           worktrees_dir=repo.parent / "wt", data_dir=repo / "data")
    plain._smoke(repo)
    assert seen[-1] == (repo, sys.executable)  # dev: this very Python, as before


def test_a_frozen_helix_hands_a_child_python_a_clean_environment(monkeypatch):
    # PyInstaller's bootloader and runtime hooks set these for the bundle. Inherited by the dev
    # interpreter that runs the suite (or build.py) they point it at the bundle's internals — every
    # overnight verification would fail for a reason nobody can read at 6 AM.
    monkeypatch.setenv("PYTHONHOME", "C:/dist/HELIX/_internal")
    monkeypatch.setenv("QT_PLUGIN_PATH", "C:/dist/HELIX/_internal/PyQt6/Qt6/plugins")
    monkeypatch.setenv("HELIX_KEEP_ME", "1")
    frozen = selfdev_mod.child_env(frozen=True)
    assert "PYTHONHOME" not in frozen and "QT_PLUGIN_PATH" not in frozen and frozen["HELIX_KEEP_ME"] == "1"
    dev = selfdev_mod.child_env(frozen=False)
    assert dev["PYTHONHOME"] == "C:/dist/HELIX/_internal"  # development: the developer's own env
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")
        return type("P", (), {"returncode": 0, "stdout": "1 passed", "stderr": ""})()

    monkeypatch.setattr(selfdev_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(selfdev_mod.sys, "frozen", True, raising=False)
    assert run_test_suite(Path("wt"), "py", 10) == (True, "1 passed")
    assert seen["env"] is not None and "PYTHONHOME" not in seen["env"]
    assert selfdev_mod.compile_smoke_check(Path("wt"), "py") == (True, "") and "PYTHONHOME" not in seen["env"]


def test_the_suite_runs_in_an_allowlisted_environment_without_the_parents_secrets(tmp_path, monkeypatch):
    # verify() EXECUTES the draft's code (pytest imports it) — code no human has read, at 3 AM. The
    # parent's environment carries whatever the login shell exports (API keys, tokens): the child
    # gets an allowlist of what Python and npm need, never everything-minus-a-few, and its own
    # data dir under the worktree.
    monkeypatch.setenv("SKEPTIC_CANARY", "hunter2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("PYTHONHOME", "C:/dist/HELIX/_internal")  # dropped even in development
    monkeypatch.setenv("HELIX_DATA_DIR", "C:/somewhere/live")
    env = selfdev_mod.suite_env(tmp_path / "wt", frozen=False)
    assert "SKEPTIC_CANARY" not in env and "ANTHROPIC_API_KEY" not in env and "PYTHONHOME" not in env
    assert env["HELIX_DATA_DIR"] == str(tmp_path / "wt" / "data")  # pinned under the worktree
    assert "PATH" in env and env["PATH"] == os.environ["PATH"]
    assert set(env) - {"HELIX_DATA_DIR"} <= {k for k in os.environ if k.upper() in selfdev_mod._SUITE_ENV_ALLOW}
    # The real runner hands exactly that to pytest: a toy test proves the canary never arrives.
    _w(tmp_path / "tests" / "test_env.py",
       "import os\n"
       "def test_env():\n"
       "    assert 'SKEPTIC_CANARY' not in os.environ\n"
       "    assert 'ANTHROPIC_API_KEY' not in os.environ\n"
       f"    assert os.environ['HELIX_DATA_DIR'] == {str(tmp_path / 'data')!r}\n"
       "    assert os.environ.get('PATH')\n")
    ok, tail = run_test_suite(tmp_path, sys.executable, 120)
    assert ok is True, tail
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")
        return type("P", (), {"returncode": 0, "stdout": "built", "stderr": ""})()

    monkeypatch.setattr(selfdev_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(selfdev_mod.shutil, "which", lambda name: "npm")
    _w(tmp_path / "web" / "package.json", "{}")
    (tmp_path / "web" / "node_modules").mkdir()
    assert selfdev_mod.run_web_build(tmp_path) == (True, "built")  # the branch's vite config: same rule
    assert "SKEPTIC_CANARY" not in seen["env"] and seen["env"]["HELIX_DATA_DIR"] == str(tmp_path / "data")


def test_the_real_suite_runner_reports_green_and_red_with_a_tail(tmp_path):
    _w(tmp_path / "tests" / "test_toy.py", "def test_a():\n    assert True\n")
    ok, tail = run_test_suite(tmp_path, sys.executable, 120)
    assert ok is True and "1 passed" in tail
    _w(tmp_path / "tests" / "test_toy.py", "def test_a():\n    assert 1 == 2, 'toy red'\n")
    ok, tail = run_test_suite(tmp_path, sys.executable, 120)
    assert ok is False and "1 failed" in tail and "FAILED tests/test_toy.py::test_a" in tail
    assert len(tail.splitlines()) <= selfdev_mod.VERIFY_TAIL_LINES
    ok, tail = run_test_suite(tmp_path, str(tmp_path / "no-such-python.exe"), 120)
    assert ok is False and "could not run" in tail
