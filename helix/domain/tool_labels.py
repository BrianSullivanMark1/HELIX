"""Friendly, speakable labels for the internal tool names.

The orb narrates what it's doing as it works. If it ever utters a raw tool name — "call_api",
"build_3d_model" — the neural voice mangles it (an underscore has no sound, so "call_api" is read as
one glued word, "calawpee"). This maps every tool to a short human phrase a non-coder understands, so
both brains (the API loop and the subscription session) narrate the SAME clean words and no internal
identifier is ever spoken. Pure data; depends on nothing, so any layer may import it.
"""
from __future__ import annotations

# tool name → the short phrase HELIX says (or shows) while that tool runs. Kept plain and spoken-safe:
# no symbols, no identifiers, nothing the TTS would read letter-by-letter.
_TOOL_PHRASES: dict[str, str] = {
    "build_app": "Building that",
    "build_task": "Building that flow",
    "build_3d_model": "Modeling that",
    "create_agent": "Setting up that agent",
    "delete_build": "Removing that",
    "rename_build": "Renaming that",
    "open_build": "Opening it",
    "run_task": "Running that flow",
    "run_agent": "Running that agent",
    "prioritize_build": "Reordering the queue",
    "cancel_build": "Stopping the build",
    "improve_helix": "Drafting a change to myself",
    "approve_self_change": "Applying the change",
    "reject_self_change": "Discarding the change",
    "list_self_changes": "Checking pending changes",
    "list_builds": "Checking the work",
    "list_apps": "Looking over what you've built",
    "think_harder": "Thinking it through",
    "search_knowledge": "Checking your knowledge",
    "create_knowledge": "Starting a knowledge base",
    "remember": "Saving that",
    "set_reminder": "Setting that reminder",
    "cancel_reminder": "Cancelling that reminder",
    "list_reminders": "Checking your reminders",
    "set_agent_enabled": "Updating that agent",
    "check_email": "Checking your inbox",
    "check_calendar": "Checking your calendar",
    "remember_about_me": "Remembering that",
    "set_location": "Saving your location",
    "call_api": "Checking that service",
    "list_folder": "Looking through the folder",
    "read_file": "Reading the file",
    "write_file": "Writing the file",
}


def friendly_tool_label(name: str) -> str:
    """A short, speakable phrase for a tool call — never the raw identifier. Strips any MCP-style
    ``server__tool`` prefix and falls back to a neutral "Working…" for anything unmapped, so the voice
    never reads an underscore-laden name letter-by-letter."""
    key = (name or "").rsplit("__", 1)[-1].strip().lower()
    return _TOOL_PHRASES.get(key, "Working…")
