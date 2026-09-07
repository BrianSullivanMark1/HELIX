"""Resolving `claude.exe` — and never blaming the token for a broken CLI.

The bug this pins: the Claude desktop app ships as an MSIX package, so its bundled claude.exe lives
in the package's LocalCache. A non-packaged process (HELIX) can SEE that file but Windows may refuse
to launch it, which surfaces as FileNotFoundError from CreateProcess and reaches the user as the
Agent SDK's CLINotFoundError. The old resolver returned the newest desktop-app copy on the strength
of `is_file()` alone and only consulted PATH when no desktop copy existed at all — so a working
standalone CLI sitting on PATH was never tried, the subscription rail died, and the fallback message
told the user to check a token that was perfectly good.

Invariants: candidates are ordered (override → newest desktop version → PATH) and de-duplicated;
a candidate that will not START is skipped rather than returned; launchability is probed at most once
per path (spawning a ~278 MB exe is not free); and when no rail works the message names the CLI, not
the credential.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import helix.adapters.claude_code_cli as cli
from helix.domain.errors import MissingApiKey
from helix.domain.models import Role
from helix.ports.llm import Text, Turn


@pytest.fixture(autouse=True)
def _clean_cache():
    cli.reset_cli_cache()
    yield
    cli.reset_cli_cache()


def _fake_probe(monkeypatch, launchable: set[str], calls: list[str] | None = None):
    """Replace the real subprocess probe: only paths in `launchable` start successfully."""
    def _run(cmd, **kwargs):
        path = cmd[0]
        if calls is not None:
            calls.append(path)
        if path not in launchable:
            raise FileNotFoundError(2, "The system cannot find the file specified", path)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(cli.subprocess, "run", _run)


# ----- candidate ordering -----

def test_candidates_are_ordered_override_then_newest_then_path(monkeypatch, tmp_path):
    msix = tmp_path / "Packages" / "Claude_abc" / "LocalCache" / "Roaming" / "Claude" / "claude-code"
    for ver in ("2.1.9", "2.1.221", "2.0.1"):
        (msix / ver).mkdir(parents=True)
        (msix / ver / "claude.exe").write_text("x")
    override = tmp_path / "override" / "claude.exe"
    override.parent.mkdir(parents=True)
    override.write_text("x")
    on_path = tmp_path / "npm" / "claude.exe"
    on_path.parent.mkdir(parents=True)
    on_path.write_text("x")

    monkeypatch.setenv(cli.CLI_OVERRIDE_ENV, str(override))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _n: str(on_path))

    got = cli.cli_candidates()
    assert got[0] == str(override), "the explicit override must win"
    # 221 > 9 numerically: version ordering is per-component, not lexicographic
    assert [Path(p).parent.name for p in got[1:4]] == ["2.1.221", "2.1.9", "2.0.1"]
    assert got[-1] == str(on_path), "PATH is the last resort, but it IS a candidate"


def test_candidates_are_deduplicated(monkeypatch, tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("x")
    monkeypatch.setenv(cli.CLI_OVERRIDE_ENV, str(exe))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _n: str(exe))
    assert cli.cli_candidates() == [str(exe)], "the same exe reached two ways is probed once"


# ----- launch validation: the actual fix -----

def test_unlaunchable_msix_copy_is_skipped_for_the_working_path(monkeypatch, tmp_path):
    msix = tmp_path / "Packages" / "Claude_abc" / "LocalCache" / "Roaming" / "Claude" / "claude-code"
    (msix / "2.1.221").mkdir(parents=True)
    broken = msix / "2.1.221" / "claude.exe"
    broken.write_text("x")            # exists on disk...
    working = tmp_path / "npm" / "claude.exe"
    working.parent.mkdir(parents=True)
    working.write_text("x")

    monkeypatch.delenv(cli.CLI_OVERRIDE_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _n: str(working))
    _fake_probe(monkeypatch, launchable={str(working)})   # ...but only the PATH one starts

    assert cli.resolve_claude_cli() == str(working)
    assert cli.cli_unavailable_reason() is None


def test_resolve_returns_none_when_nothing_launches(monkeypatch, tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("x")
    monkeypatch.setenv(cli.CLI_OVERRIDE_ENV, str(exe))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _n: None)
    _fake_probe(monkeypatch, launchable=set())

    assert cli.resolve_claude_cli() is None
    reason = cli.cli_unavailable_reason()
    assert reason and "none of them will start" in reason
    assert "npm install -g @anthropic-ai/claude-code" in reason


def test_nonzero_exit_counts_as_unlaunchable(monkeypatch, tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("x")
    monkeypatch.setenv(cli.CLI_OVERRIDE_ENV, str(exe))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _n: None)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    assert cli.resolve_claude_cli() is None


def test_probe_timeout_is_not_fatal(monkeypatch, tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("x")
    monkeypatch.setenv(cli.CLI_OVERRIDE_ENV, str(exe))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _n: None)

    def _timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 60)
    monkeypatch.setattr(cli.subprocess, "run", _timeout)
    assert cli.resolve_claude_cli() is None       # degrades, never raises


def test_launchability_is_probed_once_per_path(monkeypatch, tmp_path):
    """resolve_claude_cli() is called on every turn (and every active() check). If it spawned a
    278 MB exe each time, the fix would trade a dead rail for a slow one."""
    exe = tmp_path / "claude.exe"
    exe.write_text("x")
    monkeypatch.setenv(cli.CLI_OVERRIDE_ENV, str(exe))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _n: None)
    calls: list[str] = []
    _fake_probe(monkeypatch, launchable={str(exe)}, calls=calls)

    for _ in range(25):
        assert cli.resolve_claude_cli() == str(exe)
    assert len(calls) == 1, f"probed {len(calls)} times; must be cached"

    cli.reset_cli_cache()                 # ...but a fresh install can be picked up on demand
    assert cli.resolve_claude_cli() == str(exe)
    assert len(calls) == 2


def test_allow_probe_false_never_spawns(monkeypatch, tmp_path):
    """The Qt GUI thread must never block on a claude.exe spawn. SettingsView's 'which brain is live'
    label is built before the first frame and refreshed on every Save; probing there froze the window
    for the length of the spawn (measured at 765 ms, worst case the full probe timeout)."""
    exe = tmp_path / "claude.exe"
    exe.write_text("x")
    monkeypatch.setenv(cli.CLI_OVERRIDE_ENV, str(exe))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _n: None)

    def _explode(*a, **k):
        raise AssertionError("allow_probe=False spawned a subprocess")
    monkeypatch.setattr(cli.subprocess, "run", _explode)

    # Unprobed and not allowed to probe: answers optimistically rather than blocking.
    assert cli.resolve_claude_cli(allow_probe=False) == str(exe)
    assert cli.cli_unavailable_reason(allow_probe=False) is None


def test_allow_probe_false_still_honours_a_warm_cache(monkeypatch, tmp_path):
    """Once the startup daemon thread has probed, the GUI path must report the REAL answer — the
    optimistic guess is only for the gap before that probe lands."""
    broken = tmp_path / "broken" / "claude.exe"
    broken.parent.mkdir(parents=True)
    broken.write_text("x")
    monkeypatch.setenv(cli.CLI_OVERRIDE_ENV, str(broken))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _n: None)

    _fake_probe(monkeypatch, launchable=set())
    assert cli.resolve_claude_cli() is None          # the real (probing) resolution, e.g. off-thread

    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-probed")))
    assert cli.resolve_claude_cli(allow_probe=False) is None   # cached negative, no new spawn
    assert "none of them will start" in (cli.cli_unavailable_reason(allow_probe=False) or "")


def test_subscription_active_can_be_asked_without_probing(monkeypatch, tmp_path):
    import helix.adapters.agent_sdk_chat as brain_mod
    from helix.adapters.agent_sdk_chat import SubscriptionBrain

    monkeypatch.setattr(brain_mod, "sdk_importable", lambda: True)
    exe = tmp_path / "claude.exe"
    exe.write_text("x")
    monkeypatch.setenv(cli.CLI_OVERRIDE_ENV, str(exe))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _n: None)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("GUI path probed")))

    brain = SubscriptionBrain(lambda: "sk-ant-oat01-fake", "sys", workdir=".")
    assert brain.active(allow_probe=False) is True
    assert brain.why_inactive(allow_probe=False) is None


def test_probe_timeout_is_bounded():
    """A --version that takes longer than this is broken, and the ceiling is what a mis-placed
    GUI-thread call would freeze for in the worst case. Keep it short."""
    assert cli._PROBE_TIMEOUT_S <= 20.0


def test_no_candidates_at_all_says_so(monkeypatch):
    monkeypatch.delenv(cli.CLI_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _n: None)
    reason = cli.cli_unavailable_reason()
    assert reason and "No Claude Code CLI was found" in reason


# ----- the honest message -----

def test_why_inactive_names_the_real_missing_piece(monkeypatch):
    from helix.adapters.agent_sdk_chat import SubscriptionBrain
    import helix.adapters.agent_sdk_chat as brain_mod

    monkeypatch.setattr(brain_mod, "sdk_importable", lambda: True)

    no_token = SubscriptionBrain(lambda: "", "sys", workdir=".")
    assert "setup-token" in (no_token.why_inactive() or "")

    monkeypatch.setattr(brain_mod, "cli_unavailable_reason", lambda **_kw: "CLI is wedged")
    with_token = SubscriptionBrain(lambda: "sk-ant-oat01-fake", "sys", workdir=".")
    assert with_token.why_inactive() == "CLI is wedged", "a good token must not be blamed"

    monkeypatch.setattr(brain_mod, "cli_unavailable_reason", lambda **_kw: None)
    assert with_token.why_inactive() is None

    monkeypatch.setattr(brain_mod, "sdk_importable", lambda: False)
    assert "claude-agent-sdk" in (with_token.why_inactive() or "")


class _NoKeyApi:
    """The API rail with no key — exactly what AnthropicChat raises."""

    def chat(self, turns, system=None, tools=None):
        raise MissingApiKey(
            "Claude isn't reachable right now. Check your subscription token (run "
            "claude setup-token) or add a Claude API key in Settings."
        )


class _DeadSub:
    def __init__(self, reason):
        self._reason = reason

    def active(self):
        return False

    def why_inactive(self):
        return self._reason


def test_both_rails_down_reports_the_cli_not_the_token():
    from helix.adapters.agent_sdk_chat import PreferredChat

    chat = PreferredChat(_DeadSub("Found 1 Claude Code CLI but it will not start."), _NoKeyApi())
    with pytest.raises(MissingApiKey) as err:
        chat.chat([Turn(Role.USER, (Text("hi"),))])
    msg = str(err.value)
    assert "will not start" in msg, "the actual cause must reach the user"
    assert "Check your subscription token" not in msg, "must not blame a good token"


def test_usable_subscription_keeps_the_generic_message():
    """If the subscription rail is fine and the turn still failed, we know nothing extra — don't
    invent a CLI problem."""
    from helix.adapters.agent_sdk_chat import PreferredChat

    chat = PreferredChat(_DeadSub(None), _NoKeyApi())
    with pytest.raises(MissingApiKey) as err:
        chat.chat([Turn(Role.USER, (Text("hi"),))])
    assert "Check your subscription token" in str(err.value)


def test_missing_api_key_still_propagates_as_missing_api_key():
    from helix.adapters.agent_sdk_chat import PreferredChat

    chat = PreferredChat(_DeadSub("cli broken"), _NoKeyApi())
    with pytest.raises(MissingApiKey):
        chat.chat([Turn(Role.USER, (Text("hi"),))])


# ----- the Windows command-line ceiling -----
#
# Windows caps a whole command line at 32,767 chars, and the Agent SDK passes the system prompt as an
# ARGUMENT. HELIX's persona grew to 31 KB; with the flags and ~50 bridged tool names the orb's spawn
# measured 33,434 chars, so CreateProcess failed with WinError 206 and the SDK reported it as
# "Claude Code not found at <path>" — a launchable CLI and a valid token both blamed for a string
# that was simply too long. The persona only ever grows, so this needs a pin, not a memory.


def _fake_registry(names):
    from dataclasses import dataclass

    @dataclass
    class _Spec:
        name: str
        description: str
        input_schema: dict

    class _Registry:
        def specs(self):
            return [_Spec(n, f"the {n} tool", {"type": "object", "properties": {}}) for n in names]

    return _Registry()


def test_the_orb_spawn_fits_inside_the_windows_command_line_limit(monkeypatch, tmp_path):
    """The real persona + the real tool surface must produce a command line Windows will accept."""
    import subprocess as sp

    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    import helix.adapters.agent_sdk_chat as brain_mod
    from helix.adapters.agent_sdk_chat import SubscriptionBrain
    from helix.services.prompts import CONSOLE_SYSTEM

    monkeypatch.setattr(brain_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    # 60 plausible tool names — MORE than HELIX ships today, so the pin holds as the surface grows.
    names = tuple(f"a_helix_tool_number_{i:02d}" for i in range(60))
    brain = SubscriptionBrain(lambda: "sk-ant-oat01-fake", CONSOLE_SYSTEM,
                              tools=_fake_registry(names), workdir=str(tmp_path))
    opts = brain._options(names, "claude-sonnet-4-6", "low", brain._orb_sinks)

    transport = SubprocessCLITransport(prompt="hi", options=opts)
    transport._cli_path = str(tmp_path / "claude.exe")
    line = sp.list2cmdline(transport._build_command())
    assert len(line) < 32767, (
        f"the orb's command line is {len(line)} chars — over the Windows ceiling, so every turn dies "
        f"in CreateProcess with WinError 206 and is reported as a missing CLI"
    )
    assert CONSOLE_SYSTEM not in line, "the persona must not ride the command line at all"


def test_a_long_system_prompt_goes_to_a_file_and_a_short_one_stays_inline(monkeypatch, tmp_path):
    import helix.adapters.agent_sdk_chat as brain_mod

    monkeypatch.setattr(brain_mod.tempfile, "gettempdir", lambda: str(tmp_path))

    short = "be helpful"
    assert brain_mod._system_prompt_arg(short) == short, "a short prompt needs no file"

    long = "x" * (brain_mod.SYSTEM_PROMPT_FILE_OVER + 1)
    arg = brain_mod._system_prompt_arg(long)
    assert arg["type"] == "file"
    assert Path(arg["path"]).read_text(encoding="utf-8") == long, "the prompt must survive the trip"
    # Content-addressed: the same persona reuses one file instead of littering one per turn.
    assert brain_mod._system_prompt_arg(long)["path"] == arg["path"]
    assert brain_mod._system_prompt_arg(long + "!")["path"] != arg["path"]


def test_an_unwritable_temp_dir_falls_back_to_the_inline_prompt(monkeypatch, tmp_path):
    """A disk that refuses must cost us the old behaviour, never the turn."""
    import helix.adapters.agent_sdk_chat as brain_mod

    monkeypatch.setattr(brain_mod.tempfile, "gettempdir", lambda: str(tmp_path))

    def _no(*_a, **_kw):
        raise OSError("read-only")

    monkeypatch.setattr(brain_mod.Path, "mkdir", _no)
    long = "y" * (brain_mod.SYSTEM_PROMPT_FILE_OVER + 1)
    assert brain_mod._system_prompt_arg(long) == long


class _HealthySubThatFailed:
    """Token saved, SDK present, CLI launchable — and the turn still blew up. Exactly the shape of
    the too-long command line: why_inactive() sees nothing wrong because nothing STRUCTURAL is."""

    def active(self):
        return False

    def why_inactive(self):
        return None

    def last_failure(self):
        return "Claude Code not found at: C:\\...\\claude.exe"


def test_a_healthy_looking_rail_reports_the_turn_failure_not_the_token():
    from helix.adapters.agent_sdk_chat import PreferredChat

    chat = PreferredChat(_HealthySubThatFailed(), _NoKeyApi())
    with pytest.raises(MissingApiKey) as err:
        chat.chat([Turn(Role.USER, (Text("hi"),))])
    msg = str(err.value)
    assert "Claude Code not found" in msg, "the real failure must reach the user"
    assert "Check your subscription token" not in msg, "must not blame a token that is saved and fine"


def test_a_working_turn_clears_the_remembered_failure():
    """A stale error must never be blamed for a later problem."""
    from helix.adapters.agent_sdk_chat import SubscriptionBrain

    brain = SubscriptionBrain(lambda: "sk-ant-oat01-fake", "sys", workdir=".")
    assert brain.last_failure() is None
    brain._note_failure(RuntimeError("pipe died"))
    assert brain.last_failure() == "pipe died"
    assert brain._note_success("hello") == "hello"
    assert brain.last_failure() is None
