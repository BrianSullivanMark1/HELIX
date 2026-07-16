"""Trustworthy edits — iteration gets an EDIT prompt (not a rebuild), failed/cancelled builds still
revert escapes, a broken result gets one repair pass then fails honestly, and 'open it' works by voice."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from helix.adapters.api_coder import ApiCoder
from helix.adapters.git_repo import GitRepo
from helix.adapters.signal_bus import SignalBus
from helix.adapters.system_clock import SystemClock
from helix.domain.errors import BuildError
from helix.domain.events import BuildOpenRequested
from helix.domain.models import BuildKind
from helix.ports.coder import CoderResult
from helix.services.builds import BuildService
from helix.services.forge import ForgeService
from helix.services.tools import ToolRegistry

GIT = GitRepo()
CLOCK = SystemClock()


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class _Coder:
    """A scriptable coder: fn(ws, call_index) writes files; `ok` controls the reported result. Records
    every prompt it was handed, so tests can assert WHICH instruction a build used."""

    name = "fake"

    def __init__(self, fn, ok: bool = True) -> None:
        self._fn = fn
        self._ok = ok
        self.prompts: list[str] = []

    def available(self) -> bool:
        return True

    def run_task(self, repo_dir, prompt, *, on_progress=None, cancel=None):
        self.prompts.append(prompt)
        self._fn(Path(repo_dir), len(self.prompts) - 1)
        return CoderResult(ok=self._ok, summary="ok", error=None if self._ok else "boom")


def _build_repo() -> Path:
    root = Path(tempfile.mkdtemp()) / "app"
    GIT.init(root)
    _w(root / ".gitignore", "data/")
    _w(root / "README.md", "a")
    GIT.commit_all(root, "b")
    return root


def _forge(root: Path, coder: _Coder) -> ForgeService:
    bs = BuildService(root / "data" / "builds", GIT, CLOCK)
    return ForgeService(bs, coder, SignalBus(), GIT, root, [root / "data" / "s.json"])


# ---------- edit-aware prompts ----------

def test_fresh_app_gets_the_build_prompt_and_iteration_gets_the_edit_prompt():
    root = _build_repo()
    coder = _Coder(lambda ws, i: _w(ws / "index.html", "<h1>ok</h1>"))
    forge = _forge(root, coder)
    forge.build("Tip Calc", "a tip calculator")
    assert "Build a small, self-contained app" in coder.prompts[0]
    forge.build("Tip Calc", "make the button blue")
    assert "You are EDITING the existing app" in coder.prompts[1]
    assert "make the button blue" in coder.prompts[1]
    assert "Build a small, self-contained app" not in coder.prompts[1]


def test_task_iteration_gets_the_edit_protocol_prompt():
    root = _build_repo()
    coder = _Coder(lambda ws, i: _w(ws / "main.py", "print('hi')"))
    forge = _forge(root, coder)
    forge.build("Renamer", "rename my downloads", kind=BuildKind.TASK)
    assert "self-contained PROTOCOL" in coder.prompts[0]
    forge.build("Renamer", "also handle PDFs", kind=BuildKind.TASK)
    assert "EDITING the existing protocol" in coder.prompts[1]


def test_explicit_prompt_override_still_wins():
    root = _build_repo()
    coder = _Coder(lambda ws, i: _w(ws / "index.html", "<h1>ok</h1>"))
    _forge(root, coder).build("Custom", "x", prompt="CUSTOM INSTRUCTION")
    assert coder.prompts == ["CUSTOM INSTRUCTION"]


# ---------- escapes are reverted even when the build fails ----------

def test_failed_build_still_reverts_its_escaped_writes():
    root = _build_repo()

    def evil(ws, i):
        _w(ws / "index.html", "<h1>ok</h1>")
        _w(root / "helix" / "services" / "evil.py", "PWN")  # write into source…

    coder = _Coder(evil, ok=False)  # …then report failure (previously this skipped the scan)
    with pytest.raises(BuildError):
        _forge(root, coder).build("Sneaky", "x")
    assert not (root / "helix" / "services" / "evil.py").exists()


# ---------- the pre-finalize verify gate ----------

def test_broken_python_gets_one_repair_pass_then_ships():
    root = _build_repo()

    def fn(ws, call):
        if call == 0:
            _w(ws / "index.html", "<h1>ok</h1>")
            _w(ws / "app.py", "def broken(:\n")  # syntax error
        else:
            _w(ws / "app.py", "def fixed():\n    return 1\n")

    coder = _Coder(fn)
    app = _forge(root, coder).build("Fixable", "x")
    assert len(coder.prompts) == 2
    assert "failed its automatic check" in coder.prompts[1]
    assert app.entry_point == "index.html"  # finalized normally after the repair


def test_unfixable_build_fails_honestly_instead_of_shipping_broken():
    root = _build_repo()
    coder = _Coder(lambda ws, i: _w(ws / "main.py", "def broken(:\n"))
    with pytest.raises(BuildError, match="didn't pass its checks"):
        _forge(root, coder).build("Doomed", "x", kind=BuildKind.TASK)
    assert len(coder.prompts) == 2  # the repair pass was attempted


def test_task_without_main_py_fails_the_gate():
    root = _build_repo()
    coder = _Coder(lambda ws, i: _w(ws / "notes.txt", "not a program"))
    with pytest.raises(BuildError, match="main.py"):
        _forge(root, coder).build("Empty Flow", "x", kind=BuildKind.TASK)


# ---------- open_build ----------

def test_open_build_publishes_the_resolved_slug():
    root = _build_repo()
    coder = _Coder(lambda ws, i: _w(ws / "index.html", "<h1>ok</h1>"))
    forge = _forge(root, coder)
    forge.build("Tip Calc", "x")
    bus = SignalBus()
    opened: list = []
    bus.subscribe(BuildOpenRequested, opened.append)
    builds = BuildService(root / "data" / "builds", GIT, CLOCK)
    tools = ToolRegistry(forge, builds, bus=bus)
    out = tools.dispatch("open_build", {"name": "tip calc"})
    assert "Opening Tip Calc" in out
    assert len(opened) == 1 and opened[0].slug == "tip-calc"


def test_open_build_unknown_name_is_friendly():
    root = _build_repo()
    builds = BuildService(root / "data" / "builds", GIT, CLOCK)
    tools = ToolRegistry(_forge(root, _Coder(lambda ws, i: None)), builds, bus=SignalBus())
    assert "couldn't find" in tools.dispatch("open_build", {"name": "ghost"})


def test_open_build_is_denied_to_autonomous_agents():
    # open_build LAUNCHES a server app's main.py and yanks the UI — the same capability as run_task, so
    # an autonomous agent (allow_builds=False) must not have it, or an attacker email could run an app.
    from helix.services.conversation import BUILD_TOOLS
    assert "open_build" in BUILD_TOOLS


# ---------- verify-gate rollback ----------

def test_failed_verify_gate_rolls_an_iteration_back_to_its_last_good_version():
    root = _build_repo()
    _forge(root, _Coder(lambda ws, i: _w(ws / "index.html", "<h1>v1 works</h1>"))).build("My App", "v1")
    ws = root / "data" / "builds" / "my-app"
    assert (ws / "index.html").read_text(encoding="utf-8") == "<h1>v1 works</h1>"
    # An edit whose result never passes the gate (broken .py on both passes).
    coder_bad = _Coder(lambda ws2, i: _w(ws2 / "app.py", "def broken(:\n"))
    with pytest.raises(BuildError, match="didn't pass its checks"):
        _forge(root, coder_bad).build("My App", "add a broken helper")
    # The working v1 is restored on disk; the broken file is gone — not left for the user to open.
    assert (ws / "index.html").read_text(encoding="utf-8") == "<h1>v1 works</h1>"
    assert not (ws / "app.py").exists()
    assert not (ws / ".building").exists()  # marker cleared so startup recovery leaves it alone


def test_failed_verify_gate_removes_a_never_finalized_new_build():
    root = _build_repo()
    coder_bad = _Coder(lambda ws, i: _w(ws / "main.py", "def broken(:\n"))
    with pytest.raises(BuildError, match="didn't pass its checks"):
        _forge(root, coder_bad).build("Doomed Flow", "x", kind=BuildKind.TASK)
    # A brand-new build that never passed its checks must not linger as a broken menu entry.
    assert not (root / "data" / "builds" / "doomed-flow").exists()


# ---------- the API coder knows an edit from a build ----------

def test_api_coder_detects_existing_code(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("scaffold", encoding="utf-8")
    (ws / ".helixbuild.json").write_text("{}", encoding="utf-8")
    assert not ApiCoder._has_existing_code(ws)  # scaffold only → a fresh build
    (ws / "index.html").write_text("<h1>app</h1>", encoding="utf-8")
    assert ApiCoder._has_existing_code(ws)  # real files → an edit
