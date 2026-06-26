"""Cancellation + cleanup tests — a 'stop' must actually halt a build, and the user can remove the work.

Service-layer only (real GitRepo + BuildService, fake coders); no Qt. These lock the behaviour Brian
asked for: stop mid-build, then offer to remove/roll back the half-finished app/model/task.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from helix.adapters.api_coder import ApiCoder
from helix.adapters.git_repo import GitRepo
from helix.adapters.signal_bus import SignalBus
from helix.adapters.system_clock import SystemClock
from helix.domain.errors import BuildCancelled
from helix.domain.events import BuildDeleted
from helix.domain.models import slugify
from helix.ports.coder import CoderResult
from helix.services.builds import BuildService
from helix.services.cancel import BuildHandle, CancelToken
from helix.services.conversation import STOPPED_REPLY, ConversationService
from helix.services.forge import ForgeService

GIT = GitRepo()
CLOCK = SystemClock()


# ----- CancelToken -----
def test_cancel_token_signals_and_carries_the_build():
    t = CancelToken()
    assert not t.is_set() and not t.cancelled and t.build is None
    t.build = BuildHandle("my-app", "My App", iterating=False)
    t.cancel()
    assert t.is_set() and t.cancelled
    assert t.build.slug == "my-app" and t.build.iterating is False


# ----- forge build cancellation + cleanup -----
class _Coder:
    name = "fake"

    def __init__(self, fn):
        self._fn = fn  # fn(repo_dir, cancel) -> CoderResult

    def available(self):
        return True

    def run_task(self, repo_dir, prompt, *, on_progress=None, cancel=None):
        return self._fn(Path(repo_dir), cancel)


def _forge(coder) -> tuple[ForgeService, BuildService, Path]:
    root = Path(tempfile.mkdtemp()) / "app"
    root.mkdir(parents=True)
    GIT.init(root)
    (root / "README.md").write_text("base", encoding="utf-8")
    GIT.commit_all(root, "base")
    builds = BuildService(root / "data" / "builds", GIT, CLOCK)
    forge = ForgeService(builds, coder, SignalBus(), GIT, root, [root / "data" / "s.json"])
    return forge, builds, root


def _ok_writing(repo_dir: Path, cancel):
    (repo_dir / "index.html").write_text("<h1>app</h1>", encoding="utf-8")
    return CoderResult(ok=True, summary="built")


def _stops_mid_build(repo_dir: Path, cancel):
    if cancel is not None:  # simulate the user pressing 'stop' while the coder is working
        cancel.cancel()
    return CoderResult(ok=False, summary="", error="cancelled")


def test_new_build_cancelled_raises_and_records_handle():
    forge, builds, _ = _forge(_Coder(_stops_mid_build))
    token = CancelToken()
    with pytest.raises(BuildCancelled) as exc:
        forge.build("My App", "do it", cancel=token)
    assert exc.value.iterating is False
    assert token.build is not None and token.build.slug == slugify("My App")
    assert builds.workspace(token.build.slug).exists()  # created but NOT finalized


def test_discard_removes_a_cancelled_new_build():
    forge, builds, _ = _forge(_Coder(_stops_mid_build))
    token = CancelToken()
    with pytest.raises(BuildCancelled):
        forge.build("Scratch", "do it", cancel=token)
    ws = builds.workspace(token.build.slug)
    assert ws.exists()
    forge.discard_build(token.build)  # the user said "remove it"
    assert not ws.exists()  # gone entirely, since it never finished


def test_discard_rolls_back_a_cancelled_iteration():
    # First a successful build, then a cancelled CHANGE to it — rollback must keep the prior good version.
    forge_ok, builds, root = _forge(_Coder(_ok_writing))
    app = forge_ok.build("Keeper", "v1")
    ws = builds.workspace(app.slug)
    assert (ws / "index.html").exists()

    forge_cancel = ForgeService(builds, _Coder(_stops_mid_build), SignalBus(), GIT, root, [])
    token = CancelToken()
    with pytest.raises(BuildCancelled) as exc:
        forge_cancel.build("Keeper", "change it", cancel=token)  # same name → iteration
    assert exc.value.iterating is True
    assert token.build is not None and token.build.iterating is True
    forge_cancel.discard_build(token.build)
    # the workspace (and its committed v1) survives a rolled-back iteration
    assert ws.exists() and (ws / "index.html").exists()


def test_discard_publishes_build_deleted():
    events = []
    root = Path(tempfile.mkdtemp()) / "app"
    root.mkdir(parents=True)
    GIT.init(root)
    (root / "README.md").write_text("base", encoding="utf-8")
    GIT.commit_all(root, "base")
    builds = BuildService(root / "data" / "builds", GIT, CLOCK)
    bus = SignalBus()
    bus.subscribe(BuildDeleted, events.append)
    forge = ForgeService(builds, _Coder(_stops_mid_build), bus, GIT, root, [])
    token = CancelToken()
    with pytest.raises(BuildCancelled):
        forge.build("Gone", "do it", cancel=token)
    forge.discard_build(token.build)
    assert any(isinstance(e, BuildDeleted) for e in events)


# ----- coder honours the cancel signal -----
class _RaisingChat:
    def chat(self, *a, **k):
        raise AssertionError("the model must not be called once the turn is already cancelled")


def test_api_coder_returns_cancelled_without_calling_the_model():
    coder = ApiCoder(_RaisingChat(), lambda: "key")
    token = CancelToken()
    token.cancel()
    res = coder.run_task(Path(tempfile.mkdtemp()), "build something", cancel=token)
    assert not res.ok and res.error == "cancelled"


# ----- conversation aborts a cancelled turn -----
class _Usage:
    input_tokens = 0
    output_tokens = 0
    cost_usd = 0.0


class _Reply:
    def __init__(self, *, wants_tools=False, text="", tool_uses=()):
        self.wants_tools = wants_tools
        self.text = text
        self.tool_uses = tool_uses
        self.blocks = ()
        self.usage = _Usage()


class _Call:
    def __init__(self, name, args):
        self.name = name
        self.args = args
        self.id = "call-1"


class _Chat:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def chat(self, turns, system=None, tools=None):
        self.calls += 1
        return self._replies.pop(0)


class _Tools:
    def __init__(self, exc=None):
        self._exc = exc

    def specs(self):
        return []

    def dispatch(self, name, args, *, on_progress=None, cancel=None):
        if self._exc:
            raise self._exc
        return "done"


class _Store:
    def __init__(self):
        self.msgs = []

    def append(self, m):
        self.msgs.append(m)

    def recent(self, n):
        return self.msgs[-n:]


class _Memory:
    def record_usage(self, *a):
        pass


def _conversation(chat, tools):
    return ConversationService(chat, tools, _Store(), _Memory(), CLOCK, "sys")


def test_turn_cancelled_before_start_never_calls_the_model():
    chat = _RaisingChat()
    conv = _conversation(chat, _Tools())
    token = CancelToken()
    token.cancel()
    assert conv.run_turn("hi", cancel=token) == STOPPED_REPLY


def test_build_cancelled_mid_turn_ends_with_stopped_reply():
    chat = _Chat([_Reply(wants_tools=True, tool_uses=(_Call("build_app", {"name": "X"}),))])
    conv = _conversation(chat, _Tools(exc=BuildCancelled("x", "X", False)))
    assert conv.run_turn("build X") == STOPPED_REPLY


# ----- the cleanup yes/no parser (Console) -----
def test_cleanup_yes_no_parsing():
    from helix.ui.console_view import _NO, _YES

    for yes in ("yes", "yeah remove it", "sure", "delete it", "go ahead", "get rid of it"):
        assert _YES.search(yes) and not _NO.search(yes), yes
    for no in ("no", "nope", "keep it", "leave it", "don't"):
        assert _NO.search(no), no
