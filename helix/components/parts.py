"""The electronics components build/wish list (§components).

A settings-backed list that voice ("add a Raspberry Pi Zero 2 W") and the Components screen feed.
Each part carries a quantity and optional notes. Ordering goes through DigiKey/Mouser via
`helix/components/vendors.py` and is always confirmed before anything leaves the machine. Qt-free,
pure — mirrors `helix/home/groceries.py` so there is no new storage layer.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

# [{"item": "Raspberry Pi Zero 2 W", "qty": 1, "notes": "headers pre-soldered", "added": "..."}]
COMPONENTS_LIST_SETTING = "components_list"
# [{"vendor": "mouser", "items": ["..."], "count": 3, "placed": "YYYY-MM-DD HH:MM"}]
COMPONENTS_ORDER_HISTORY_SETTING = "components_order_history"


def list_items(settings: Any) -> list[dict]:
    out: list[dict] = []
    for row in (settings.get(COMPONENTS_LIST_SETTING) or []):
        if isinstance(row, dict) and row.get("item"):
            out.append(
                {
                    "item": str(row["item"]).strip(),
                    "qty": max(1, int(row.get("qty", 1) or 1)),
                    "notes": str(row.get("notes", "") or "").strip(),
                }
            )
        elif isinstance(row, str) and row.strip():
            out.append({"item": row.strip(), "qty": 1, "notes": ""})
    return out


def add_item(settings: Any, item: str, qty: int = 1, notes: str = "") -> dict:
    item = (item or "").strip()
    if not item:
        raise ValueError("no item given")
    qty = max(1, int(qty or 1))
    notes = (notes or "").strip()
    items = list_items(settings)
    for row in items:
        if row["item"].lower() == item.lower():
            row["qty"] = int(row.get("qty", 1)) + qty
            if notes:
                row["notes"] = notes
            settings.set(COMPONENTS_LIST_SETTING, items)
            return row
    rec = {"item": item, "qty": qty, "notes": notes, "added": datetime.now().strftime("%Y-%m-%d %H:%M")}
    items.append(rec)
    settings.set(COMPONENTS_LIST_SETTING, items)
    return rec


def remove_item(settings: Any, item: str) -> bool:
    want = (item or "").strip().lower()
    items = list_items(settings)
    kept = [r for r in items if r["item"].lower() != want]
    settings.set(COMPONENTS_LIST_SETTING, kept)
    return len(kept) < len(items)


def clear(settings: Any) -> None:
    settings.set(COMPONENTS_LIST_SETTING, [])


def summary(settings: Any) -> str:
    items = list_items(settings)
    if not items:
        return "The components list is empty."
    return ", ".join(r["item"] + (f" x{r['qty']}" if r["qty"] > 1 else "") for r in items)


# --------------------------------------------------------------------------- #
# Order history — a small settings-backed log so the Components screen can show what was ordered and
# when. Newest first; bounded so it can't grow without limit.
# --------------------------------------------------------------------------- #

_HISTORY_MAX = 50


def record_order(settings: Any, vendor: str, items: list[dict]) -> dict:
    """Append an order to the history and return the recorded entry."""
    names = [str(r.get("item", "")).strip() for r in items if r.get("item")]
    rec = {
        "vendor": (vendor or "").strip().lower(),
        "items": names,
        "count": len(names),
        "placed": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    history = order_history(settings)
    history.insert(0, rec)
    settings.set(COMPONENTS_ORDER_HISTORY_SETTING, history[:_HISTORY_MAX])
    return rec


def order_history(settings: Any) -> list[dict]:
    out: list[dict] = []
    for row in (settings.get(COMPONENTS_ORDER_HISTORY_SETTING) or []):
        if isinstance(row, dict) and row.get("placed"):
            out.append(row)
    return out
