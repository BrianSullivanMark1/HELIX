"""Per-location inventory store (settings-backed): the latest items HELIX saw at each camera.

Keyed by location name (e.g. "fridge", "laundry"), so you can ask "what's in the fridge?" without a
re-scan, and later detect changes / low stock to feed reordering. Qt-free; pass in `settings`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

INVENTORY_SETTING = "vision_inventory"  # {location: {"items": [...], "summary": str, "updated_at": str}}


def store(settings: Any, location: str, result: dict) -> dict:
    """Save the latest scan for a location. Returns the stored record."""
    key = (location or "").strip().lower()
    inv = dict(settings.get(INVENTORY_SETTING) or {})
    rec = {
        "items": list(result.get("items") or []),
        "summary": str(result.get("summary") or ""),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    inv[key] = rec
    settings.set(INVENTORY_SETTING, inv)
    return rec


def get(settings: Any, location: str) -> dict | None:
    return (settings.get(INVENTORY_SETTING) or {}).get((location or "").strip().lower())


def all_locations(settings: Any) -> list[str]:
    return sorted((settings.get(INVENTORY_SETTING) or {}).keys())
