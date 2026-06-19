"""The household shopping list (§home groceries).

A simple settings-backed list that voice ("add milk"), fridge-cam scans, and low-stock checks feed.
Ordering goes through the official Fry's/Kroger Cart API (`helix/home/kroger.py`): HELIX adds the items
to your real cart and you tap checkout — real money + outward, so it is always confirmed. Qt-free.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

SHOPPING_LIST_SETTING = "shopping_list"  # [{"item": "milk", "qty": 1, "added": "YYYY-MM-DD HH:MM"}]


def list_items(settings: Any) -> list[dict]:
    out: list[dict] = []
    for row in (settings.get(SHOPPING_LIST_SETTING) or []):
        if isinstance(row, dict) and row.get("item"):
            out.append({"item": str(row["item"]).strip(), "qty": int(row.get("qty", 1) or 1)})
        elif isinstance(row, str) and row.strip():
            out.append({"item": row.strip(), "qty": 1})
    return out


def add_item(settings: Any, item: str, qty: int = 1) -> dict:
    item = (item or "").strip()
    if not item:
        raise ValueError("no item given")
    qty = max(1, int(qty or 1))
    items = list_items(settings)
    for row in items:
        if row["item"].lower() == item.lower():
            row["qty"] = int(row.get("qty", 1)) + qty
            settings.set(SHOPPING_LIST_SETTING, items)
            return row
    rec = {"item": item, "qty": qty, "added": datetime.now().strftime("%Y-%m-%d %H:%M")}
    items.append(rec)
    settings.set(SHOPPING_LIST_SETTING, items)
    return rec


def remove_item(settings: Any, item: str) -> bool:
    want = (item or "").strip().lower()
    items = list_items(settings)
    kept = [r for r in items if r["item"].lower() != want]
    settings.set(SHOPPING_LIST_SETTING, kept)
    return len(kept) < len(items)


def clear(settings: Any) -> None:
    settings.set(SHOPPING_LIST_SETTING, [])


def summary(settings: Any) -> str:
    items = list_items(settings)
    if not items:
        return "The shopping list is empty."
    return ", ".join(r["item"] + (f" x{r['qty']}" if r["qty"] > 1 else "") for r in items)
