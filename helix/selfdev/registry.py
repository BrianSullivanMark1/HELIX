"""Registry of the apps a user has BUILT, which appear as cards in the launcher menu (§forge).

The product ships with this list EMPTY — the menu starts blank. Each app the user builds through the
Console is registered here so the launcher shows its card with a remove (✕) affordance. Core screens
(Settings, Archive) are NOT listed here and cannot be removed. Keeping this a plain list means the
launcher stays in sync after a restart.

Each entry: {"key": "...", "label": "...", "subtitle": "..."}. `key` matches the panel the app
registers in HelixMainWindow's `views` dict and the `show_screen` key, so the launcher card opens it.
"""
from __future__ import annotations

MENU_FEATURES: list[dict] = [
    # Ships EMPTY: the menu starts blank and fills only with apps the user builds. Each built app
    # lands here as {"key": "...", "label": "...", "subtitle": "..."} so the launcher shows its card.
]


def feature_keys() -> set[str]:
    return {f.get("key", "") for f in MENU_FEATURES if f.get("key")}
