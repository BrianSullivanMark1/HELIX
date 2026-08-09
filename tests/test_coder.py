"""ClaudeCodeCli tests — the coder's stream becomes fluent live commentary, and every way the
subprocess can end takes the whole process tree with it and names its own cause."""
from __future__ import annotations

import threading
from pathlib import Path

import helix.adapters.claude_code_cli as cli
from helix.adapters.claude_code_cli import _describe_event


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def test_prefers_model_narration_over_tool_label():
    ev = _assistant(
        {"type": "text", "text": "Adding the camera lens"},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "model.js"}},
    )
    assert _describe_event(ev) == "Adding the camera lens"


def test_falls_back_to_a_tool_label_when_no_narration():
    ev = _assistant({"type": "tool_use", "name": "Write", "input": {"file_path": "index.html"}})
    assert _describe_event(ev) == "Writing index.html"


def test_only_first_line_of_narration_is_used():
    ev = _assistant({"type": "text", "text": "Shaping the body\nthen the mount"})
    assert _describe_event(ev) == "Shaping the body"


def test_non_assistant_events_are_ignored():
    assert _describe_event({"type": "result"}) is None
    assert _describe_event({"type": "assistant", "message": {"content": []}}) is None


# ----- subprocess lifecycle: no orphaned engine child, no mislabelled cause -----

class _FakeProc:
    """Stands in for claude.exe: emits stream-json lines, then ends cleanly, explodes mid-stream, or
    hangs until something kills it. Counts BARE kills separately from tree kills."""

    def __init__(self, lines=(), *, explode=None, hang=False, stderr_lines=()):
        self.pid = 31337
        self.returncode = None
        self.bare_kills = 0
        self._lines = list(lines)
        self._explode = explode
        self._hang = threading.Event() if hang else None
        self.stderr = iter(list(stderr_lines))
        self.stdout = self._stream()

    def _stream(self):
        for line in self._lines:
            yield line
        if self._explode is not None:
            raise self._explode
        if self._hang is not None:
            self._hang.wait(10)  # ceiling so a broken test fails instead of wedging the suite

    def poll(self):
        return self.returncode

    def kill(self):
        self.bare_kills += 1
        self._release(-9)

    def simulate_tree_kill(self):
        self._release(1)  # a killed claude.exe exits nonzero — this is the "exited 1" the log used to show

    def _release(self, code):
        if self.returncode is None:
            self.returncode = code
        if self._hang is not None:
            self._hang.set()

    def wait(self, timeout=None):
        self._release(0)
        return self.returncode


def _run(monkeypatch, proc, *, timeout=60, cancel=None):
    """run_task against a fake subprocess, with _kill_tree spied (and made to actually settle the
    fake). Returns (result, tree_kill_targets)."""
    kills: list = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_k: proc)
    monkeypatch.setattr(cli, "_kill_tree", lambda p: (kills.append(p), p.simulate_tree_kill()))
    coder = cli.ClaudeCodeCli(lambda: "sk-ant-fake", cli_path="C:/fake/claude.exe", timeout=timeout)
    return coder.run_task(Path("."), "build it", cancel=cancel), kills


def test_stream_failure_kills_the_whole_tree(monkeypatch):
    """A bare proc.kill() on the stream-failure path left claude.exe's child engine process running
    (and billing) after HELIX had already given up on the build."""
    proc = _FakeProc(['{"type":"assistant","message":{"content":[]}}\n'],
                     explode=OSError("pipe died"))
    res, kills = _run(monkeypatch, proc)

    assert not res.ok and "stream failed" in (res.error or "")
    assert kills == [proc], "the child engine process must not be orphaned"
    assert proc.bare_kills == 0


def test_timeout_is_reported_as_a_timeout(monkeypatch):
    """The ceiling firing used to reach the log and the user as 'Coder exited 1' — the exit code of a
    killed process, which names no cause at all."""
    proc = _FakeProc(hang=True, stderr_lines=["thinking...\n"])
    res, kills = _run(monkeypatch, proc, timeout=0.05)

    assert not res.ok
    assert "timed out" in (res.error or ""), res.error
    assert "exited 1" not in (res.error or "")
    assert kills == [proc]


def test_a_plain_nonzero_exit_still_reports_the_exit_code(monkeypatch):
    proc = _FakeProc(stderr_lines=["auth failed\n"])
    proc.returncode = 2
    res, kills = _run(monkeypatch, proc)

    assert not res.ok and "Coder exited 2" in (res.error or "")
    assert "timed out" not in (res.error or "")
    assert kills == []


def test_clean_run_kills_nothing(monkeypatch):
    proc = _FakeProc(['{"type":"result","result":"built the thing"}\n'])
    res, kills = _run(monkeypatch, proc)

    assert res.ok and res.summary == "built the thing"
    assert kills == [] and proc.bare_kills == 0
