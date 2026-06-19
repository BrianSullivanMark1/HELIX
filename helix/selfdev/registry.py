"""Registry of SELF-ADDED features that appear in the launcher menu (§selfdev).

When HELIX writes a new user-facing feature for itself, it registers the feature here so the launcher
shows it with a remove (✕) affordance. Core features (Investments, Home, Work, Learning, Settings) are
NOT listed here and cannot be removed. Removing a self-added feature deletes its code AND its entry here
(via `remove_feature`). Keeping this a plain list the coder edits means the launcher stays in sync after
a restart.

Each entry: {"key": "...", "label": "...", "subtitle": "..."}. `key` must match the panel the feature
registers in HelixMainWindow's `views` dict and the `show_screen` key, so the launcher card opens it.
"""
from __future__ import annotations

MENU_FEATURES: list[dict] = [
    # Self-added features land here, e.g.:
    # {"key": "weather", "label": "Weather", "subtitle": "local forecast"},
]


def feature_keys() -> set[str]:
    return {f.get("key", "") for f in MENU_FEATURES if f.get("key")}
