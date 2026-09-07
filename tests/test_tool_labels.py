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
    assert friendly_tool_label("view_camera") == "Looking through the camera"


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


def test_every_tool_the_registry_offers_has_a_phrase_of_its_own():
    # The sweep that was missing. Everything above walks the MAP and checks its properties, so a
    # tool added to the registry and never added to the map sails through — which is exactly what
    # happened to show_self_change: the most consequential read in the app (what am I about to
    # merge into HELIX's own source?) was narrated to the status line and to the voice as the
    # anonymous "Working…". Walking registry -> map makes the next half-wired tool fail here
    # instead of in the room.
    import inspect

    from helix.services.tools import ToolRegistry

    class _Present:
        """Stands in for any service the registry only needs to EXIST to offer its tools. specs()
        asks a couple of them a yes/no question (files.write_enabled()), never for real work."""

        def __getattr__(self, _name):
            return lambda *a, **k: True

    params = [n for n in inspect.signature(ToolRegistry.__init__).parameters if n != "self"]
    reg = ToolRegistry(**{n: _Present() for n in params})
    offered = sorted({spec.name for spec in reg.specs()})
    assert len(offered) > 40, "the registry stopped offering tools; the sweep would prove nothing"
    unlabelled = [n for n in offered if friendly_tool_label(n) == "Working…"]
    assert unlabelled == [], f"tools with no spoken phrase in _TOOL_PHRASES: {unlabelled}"


def test_reading_a_pending_self_change_is_narrated_as_a_read():
    # Its neighbours are "Drafting a change to myself" / "Applying the change" / "Discarding the
    # change", and this one only ever reads — the label must not sound like it applies anything.
    label = friendly_tool_label("show_self_change")
    assert label == "Reading the change"
    assert friendly_tool_label("mcp__helix__show_self_change") == label  # both rails, same words


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


def test_a_named_build_is_narrated_by_name():
    # "Building that" leaves the user guessing which of two in-flight builds moved. The call carries
    # the name, so both rails say it.
    assert friendly_tool_label("build_app", {"name": "Tip Calculator"}) == "Building Tip Calculator"
    assert friendly_tool_label("build_3d_model", {"name": "Dragon"}) == "Projecting Dragon"
    assert friendly_tool_label("rename_build", {"name": "Timer"}) == "Renaming Timer"


def test_the_name_survives_an_mcp_prefix():
    # THE bug this exists to stop: the subscription rail sees `mcp__helix__build_app`, so
    # personalizing before the prefix strip would silently never fire on that rail — only the API
    # rail would ever speak the name, and the two would tell the user different things.
    assert (friendly_tool_label("mcp__helix__build_app", {"name": "Tip Calculator"})
            == "Building Tip Calculator")


def test_a_personalized_label_never_trails_off_on_its_own():
    # The caller appends the ellipsis (a tool that has only STARTED); adding one here printed
    # "Building Tip Calculator……" on the status line.
    assert not friendly_tool_label("build_app", {"name": "Tip Calculator"}).endswith("…")


def test_a_nameless_or_unnamed_call_keeps_the_generic_phrase():
    # No args, empty args, a blank name, and a tool that carries no name at all all fall back.
    assert friendly_tool_label("build_app") == "Building that"
    assert friendly_tool_label("build_app", {}) == "Building that"
    assert friendly_tool_label("build_app", {"name": "   "}) == "Building that"
    assert friendly_tool_label("check_email", {"name": "Dave"}) == "Checking your inbox"


def test_installing_the_hologram_engine_is_narrated_in_the_users_word():
    # install_cad_engine blocks for about a minute, so its phrase is what the status bar shows the whole
    # time — in the user's word for the thing ("hologram engine"), never the package or the tool name.
    label = friendly_tool_label("install_cad_engine")
    assert label == "Installing the hologram engine"
    assert friendly_tool_label("mcp__helix__install_cad_engine") == label  # both rails, same words
    assert "build123d" not in label.lower() and "pip" not in label.lower()


def test_a_hologram_build_is_still_projected_on_both_rails():
    # A hologram is now a DESIGN, but "Projecting" stays its verb: conversation._progress_label keeps its
    # own copy of the named verb for the API rail, so a new word here alone would have the two rails
    # narrate one build differently ("Drafting Dragon" / "Projecting Dragon"). If the verb ever changes,
    # it changes in both places at once — and this pin is where that decision is made visible.
    assert friendly_tool_label("build_3d_model") == "Projecting the hologram"
    assert friendly_tool_label("build_3d_model", {"name": "Pipe Bracket"}) == "Projecting Pipe Bracket"


def test_pausing_says_it_touches_workflows_too():
    # One tool pauses a scheduled agent OR a scheduled workflow — the user names both the same way
    # ("pause the morning pipeline"), so the status line must not claim it only touches agents.
    label = friendly_tool_label("set_agent_enabled")
    assert "workflow" in label.lower()
    assert len(label) < 45  # it's a live status line, not a sentence
