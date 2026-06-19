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


# --------------------------------------------------------------------------- #
# Categorization — a pure keyword heuristic that groups the list by aisle (produce, dairy, …) so the
# Grocery screen can show it organized. No I/O, no schema change: it reads the existing item names.
# --------------------------------------------------------------------------- #

OTHER_CATEGORY = "Other"
CATEGORY_ORDER = [
    "Produce", "Dairy & Eggs", "Meat & Seafood", "Bakery",
    "Frozen", "Pantry", "Beverages", "Household", OTHER_CATEGORY,
]
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Produce": (
        "apple", "banana", "orange", "lemon", "lime", "grape", "berry", "strawberr", "blueberr",
        "lettuce", "spinach", "kale", "salad", "tomato", "potato", "onion", "garlic", "carrot",
        "celery", "pepper", "cucumber", "broccoli", "avocado", "mushroom", "zucchini", "fruit",
        "veg", "herb", "cilantro", "parsley", "ginger", "corn", "melon", "peach", "pear",
    ),
    "Dairy & Eggs": (
        "milk", "egg", "cheese", "butter", "yogurt", "yoghurt", "cream", "sour cream", "cottage",
        "half and half", "half-and-half", "creamer",
    ),
    "Meat & Seafood": (
        "chicken", "beef", "steak", "pork", "bacon", "sausage", "turkey", "ham", "lamb", "meat",
        "fish", "salmon", "tuna", "shrimp", "seafood", "ground", "ribs",
    ),
    "Bakery": (
        "bread", "bagel", "bun", "roll", "tortilla", "muffin", "croissant", "cake", "donut",
        "pita", "baguette", "pastry",
    ),
    "Frozen": (
        "frozen", "ice cream", "popsicle", "pizza", "waffle",
    ),
    "Pantry": (
        "rice", "pasta", "noodle", "flour", "sugar", "salt", "spice", "oil", "olive oil", "vinegar",
        "sauce", "ketchup", "mustard", "mayo", "cereal", "oat", "bean", "lentil", "soup", "can",
        "canned", "peanut butter", "jam", "jelly", "honey", "snack", "chip", "cracker", "cookie",
        "coffee", "tea", "broth", "stock", "spaghetti", "tomato sauce", "baking",
    ),
    "Beverages": (
        "water", "juice", "soda", "pop", "cola", "beer", "wine", "drink", "lemonade", "seltzer",
        "kombucha", "gatorade", "sparkling",
    ),
    "Household": (
        "paper towel", "toilet paper", "tissue", "napkin", "soap", "detergent", "cleaner", "bleach",
        "sponge", "trash bag", "foil", "wrap", "dish", "laundry", "shampoo", "toothpaste",
        "deodorant", "diaper", "wipe", "battery", "lightbulb", "light bulb",
    ),
}


def categorize(item: str) -> str:
    """Best-effort aisle for an item name (pure keyword match); falls back to 'Other'."""
    text = (item or "").lower()
    for category in CATEGORY_ORDER:
        for keyword in _CATEGORY_KEYWORDS.get(category, ()):
            if keyword in text:
                return category
    return OTHER_CATEGORY


def group_items(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group a list of `{item, qty}` rows by category, in CATEGORY_ORDER. Empty categories are dropped."""
    buckets: dict[str, list[dict]] = {}
    for row in items:
        buckets.setdefault(categorize(row.get("item", "")), []).append(row)
    return [(cat, buckets[cat]) for cat in CATEGORY_ORDER if cat in buckets]
