"""friendly_tool_label — the orb never speaks a raw tool name (the "calawpee" class of bug)."""
from __future__ import annotations

from helix.domain.tool_labels import friendly_tool_label


def test_known_tools_map_to_plain_spoken_phrases():
    assert friendly_tool_label("call_api") == "Checking that service"
    assert friendly_tool_label("check_email") == "Checking your inbox"
    assert friendly_tool_label("build_3d_model") == "Modeling that"


def test_mcp_prefixed_names_are_stripped():
    assert friendly_tool_label("mcp__helix__call_api") == "Checking that service"


def test_unknown_tool_falls_back_to_neutral_phrase():
    assert friendly_tool_label("some_new_tool") == "Working…"
    assert friendly_tool_label("") == "Working…"


def test_no_label_ever_contains_an_underscore_or_raw_identifier():
    # Whatever a tool is named, its spoken label must never leak an underscore (the voice mangles it).
    for name in ("call_api", "build_app", "set_agent_enabled", "read_file", "mcp__x__weird_name"):
        assert "_" not in friendly_tool_label(name)
