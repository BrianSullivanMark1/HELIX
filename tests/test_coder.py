"""ClaudeCodeCli progress-description tests — the coder's stream is turned into fluent live commentary."""
from __future__ import annotations

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
