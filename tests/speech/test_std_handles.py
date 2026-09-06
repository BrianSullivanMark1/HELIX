"""Every synthesis subprocess must be created with EXPLICIT std handles (PIPE/DEVNULL/STDOUT) —
never by inheriting the parent's. A windowless PyQt process can hold invalid/corrupted std handles,
and a Windows child that inherits one dies at spawn with WinError 50 — the log's repeated
"TTS failed: [WinError 50]". Explicit handles remove that failure path entirely."""
from __future__ import annotations

import ast
import inspect
import subprocess

from helix.adapters import speech
from helix.adapters.speech import OsSpeechOut, _WarmMediaPlayer

_EXPLICIT = (subprocess.PIPE, subprocess.DEVNULL, subprocess.STDOUT)


class _FakeStdin:
    def __init__(self) -> None:
        self.wrote: list[str] = []

    def write(self, s: str) -> None:
        self.wrote.append(s)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeStdout:
    def readline(self) -> str:
        return "DONE\n"


class _FakeProc:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout()

    def poll(self):
        return None  # "still running"

    def wait(self) -> int:
        return 0

    def kill(self) -> None:
        pass


def _capture_spawns(monkeypatch) -> list[dict]:
    """Replace subprocess.Popen with a spy that records each spawn's keyword arguments."""
    calls: list[dict] = []

    def popen(cmd, **kwargs):
        calls.append(kwargs)
        return _FakeProc()

    monkeypatch.setattr(speech.subprocess, "Popen", popen)
    return calls


def _assert_explicit(kwargs: dict) -> None:
    for name in ("stdin", "stdout", "stderr"):
        handle = kwargs.get(name)
        assert handle in _EXPLICIT, (
            f"{name} would be INHERITED from the parent (got {handle!r}) — a corrupted inherited "
            f"handle kills the child at spawn with WinError 50"
        )


def test_windows_os_voice_spawns_with_explicit_std_handles(monkeypatch):
    calls = _capture_spawns(monkeypatch)
    monkeypatch.setattr(speech.platform, "system", lambda: "Windows")
    out = OsSpeechOut()
    out.speak("hello")
    assert len(calls) == 1
    _assert_explicit(calls[0])
    assert calls[0]["stdin"] is subprocess.PIPE  # the text is piped in — stdin must stay writable


def test_mac_os_voice_spawns_with_explicit_std_handles(monkeypatch):
    calls = _capture_spawns(monkeypatch)
    monkeypatch.setattr(speech.platform, "system", lambda: "Darwin")
    out = OsSpeechOut()
    out.speak("hello")
    assert len(calls) == 1
    _assert_explicit(calls[0])


def test_warm_media_player_spawns_with_explicit_std_handles(monkeypatch):
    calls = _capture_spawns(monkeypatch)
    player = _WarmMediaPlayer()
    assert player.play("x.mp3", 100) is True
    assert len(calls) == 1
    _assert_explicit(calls[0])
    # the request/response protocol needs both pipes live
    assert calls[0]["stdin"] is subprocess.PIPE and calls[0]["stdout"] is subprocess.PIPE


def test_every_spawn_in_the_speech_adapter_names_all_three_std_handles():
    """Static sweep: NO subprocess call in the speech adapter — present or future — may leave any of
    stdin/stdout/stderr to be inherited. Catches a new spawn site the behavioral tests don't drive."""
    tree = ast.parse(inspect.getsource(speech))
    spawns = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("Popen", "run", "call", "check_call", "check_output")
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert spawns, "the sweep found no spawn sites — is it scanning the right module?"
    for node in spawns:
        given = {k.arg for k in node.keywords}
        if "capture_output" in given:  # capture_output=True pins stdout+stderr explicitly
            given |= {"stdout", "stderr"}
        missing = {"stdin", "stdout", "stderr"} - given
        assert not missing, (
            f"speech.py line {node.lineno}: std handle(s) inherited from the parent: "
            f"{sorted(missing)} — pass subprocess.PIPE or subprocess.DEVNULL explicitly"
        )
