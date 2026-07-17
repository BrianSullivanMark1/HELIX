"""The V3 vocabulary — one module that owns every user-facing word for what HELIX makes and does.

V3 renamed the creation taxonomy at the PRESENTATION layer only: internal kind strings ("app",
"task", "agent", "model", "knowledge") are persisted in build metadata and must stay stable forever,
so the new words live here and every surface (UI labels, spoken narration, prompts, progress pills)
renders through these helpers instead of hardcoding a word.

The V3 words:  App · Protocol · Agent · Hologram · Vault  — made by the Forge.
  - App       an interactive screen (unchanged).
  - Protocol  a saved procedure that DOES a thing when run — on command or on a rhythm (was Task/Flow).
  - Agent     an AI mind with a standing goal (unchanged); a scheduled agent is a "watcher".
  - Hologram  the visual channel: an interactive 3D object, scene, or animation (was Model).
  - Vault     the user's own saved notes, documents, and gathered results (was Knowledge).

Also here: friendly, speakable labels for internal tool names. The orb narrates what it's doing as it
works; if it ever uttered a raw tool name — "call_api", "build_3d_model" — the neural voice would
mangle it (an underscore has no sound, so "call_api" is read as one glued word, "calawpee"). Pure
data; depends on nothing, so any layer may import it.
"""
from __future__ import annotations

# internal kind → (singular, plural) display word. Kind strings are PERSISTED — never rename them.
_KIND_WORDS: dict[str, tuple[str, str]] = {
    "app": ("app", "apps"),
    "task": ("protocol", "protocols"),
    "agent": ("agent", "agents"),
    "model": ("hologram", "holograms"),
    "knowledge": ("vault", "vaults"),
}

# Legacy + spoken synonyms → internal kind. The orb understands every word the user learned in V2
# ("build me a flow", "delete that task", "show me a 3D model", "my knowledge base") forever.
KIND_SYNONYMS: dict[str, str] = {
    "app": "app", "apps": "app", "application": "app",
    "protocol": "task", "protocols": "task",
    "task": "task", "tasks": "task", "flow": "task", "flows": "task",
    "agent": "agent", "agents": "agent", "watcher": "agent", "watchers": "agent",
    "hologram": "model", "holograms": "model",
    "model": "model", "models": "model", "3d model": "model", "3d": "model",
    "vault": "knowledge", "vaults": "knowledge",
    "knowledge": "knowledge", "knowledge base": "knowledge", "notes": "knowledge",
}


def kind_label(kind: str, *, plural: bool = False) -> str:
    """The V3 display word for an internal creation kind ('task' → 'protocol'). Unknown kinds render
    as themselves so a novel kind never crashes a label."""
    words = _KIND_WORDS.get((kind or "").strip().lower())
    if words is None:
        k = (kind or "thing").strip().lower() or "thing"
        return k + "s" if plural and not k.endswith("s") else k
    return words[1] if plural else words[0]


def kind_title(kind: str, *, plural: bool = False) -> str:
    """The display word capitalized for tabs/headings ('task' → 'Protocols')."""
    return kind_label(kind, plural=plural).capitalize()


def resolve_kind(word: str) -> str | None:
    """Map a spoken/typed word (new, legacy, or synonym) to the internal kind, or None."""
    return KIND_SYNONYMS.get(" ".join((word or "").lower().split()))


# tool name → the short phrase HELIX says (or shows) while that tool runs. Kept plain and spoken-safe:
# no symbols, no identifiers, nothing the TTS would read letter-by-letter.
_TOOL_PHRASES: dict[str, str] = {
    "build_app": "Building that",
    "build_task": "Building that protocol",
    "build_3d_model": "Projecting the hologram",
    "create_agent": "Setting up that agent",
    "delete_build": "Removing that",
    "rename_build": "Renaming that",
    "open_build": "Opening it",
    "run_task": "Running that protocol",
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
    "search_knowledge": "Checking your vault",
    "create_knowledge": "Starting a vault",
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
    "find_images": "Finding the picture",
    "view_image": "Opening the picture",
    "view_screen": "Looking at your screen",
    "connect_service": "Setting up the connection",
    "go_to_sleep": "Resting my ears",
    "open_program": "Opening the program",
    "media_control": "Reaching the media keys",
    "system_status": "Checking the machine",
    "create_workflow": "Saving that workflow",
    "run_workflow": "Running the workflow",
    "list_workflows": "Checking your workflows",
}


def friendly_tool_label(name: str) -> str:
    """A short, speakable phrase for a tool call — never the raw identifier. Strips any MCP-style
    ``server__tool`` prefix and falls back to a neutral "Working…" for anything unmapped, so the voice
    never reads an underscore-laden name letter-by-letter."""
    key = (name or "").rsplit("__", 1)[-1].strip().lower()
    return _TOOL_PHRASES.get(key, "Working…")
