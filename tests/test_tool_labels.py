"""The V3 vocabulary — speakable tool labels (the "calawpee" class of bug) and the creation words."""
from __future__ import annotations

from helix.domain.tool_labels import friendly_tool_label as shimmed_label
from helix.domain.vocabulary import (
    _TOOL_PHRASES,
    friendly_tool_label,
    kind_label,
    kind_title,
    resolve_kind,
)


def test_known_tools_map_to_plain_spoken_phrases():
    assert friendly_tool_label("call_api") == "Checking that service"
    assert friendly_tool_label("check_email") == "Checking your inbox"
    assert friendly_tool_label("build_3d_model") == "Projecting the hologram"
    assert friendly_tool_label("view_screen") == "Looking at your screen"


def test_mcp_prefixed_names_are_stripped():
    assert friendly_tool_label("mcp__helix__call_api") == "Checking that service"


def test_unknown_tool_falls_back_to_neutral_phrase():
    assert friendly_tool_label("some_new_tool") == "Working…"
    assert friendly_tool_label("") == "Working…"


def test_no_label_ever_contains_an_underscore_or_raw_identifier():
    # Whatever a tool is named, its spoken label must never leak an underscore (the voice mangles it).
    for name in ("call_api", "build_app", "set_agent_enabled", "read_file", "mcp__x__weird_name"):
        assert "_" not in friendly_tool_label(name)


def test_every_mapped_phrase_is_spoken_safe():
    for name, phrase in _TOOL_PHRASES.items():
        assert "_" not in phrase, name
        assert phrase == phrase.strip()


def test_tool_labels_shim_still_exports_the_label():
    assert shimmed_label("call_api") == "Checking that service"


def test_kind_labels_render_the_v3_words():
    assert kind_label("task") == "protocol"
    assert kind_label("model", plural=True) == "holograms"
    assert kind_label("knowledge") == "vault"
    assert kind_label("app") == "app"
    assert kind_label("agent", plural=True) == "agents"
    assert kind_title("task", plural=True) == "Protocols"


def test_unknown_kind_never_crashes_a_label():
    assert kind_label("gizmo") == "gizmo"
    assert kind_label("gizmo", plural=True) == "gizmos"
    assert kind_label("") == "thing"


def test_legacy_and_new_words_both_resolve_to_internal_kinds():
    # The user can keep every word they learned in V2 — and the new V3 words work too.
    assert resolve_kind("flow") == "task"
    assert resolve_kind("protocol") == "task"
    assert resolve_kind("3D Model") == "model"
    assert resolve_kind("hologram") == "model"
    assert resolve_kind("knowledge base") == "knowledge"
    assert resolve_kind("vault") == "knowledge"
    assert resolve_kind("app") == "app"
    assert resolve_kind("nonsense") is None
