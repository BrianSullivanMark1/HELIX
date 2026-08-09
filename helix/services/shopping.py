"""ShoppingService — HELIX's hands for the user's Amazon cart. Stages, never spends.

The flow is voice-shaped: the model resolves each product the user names (or shows the camera) to a
verified ASIN via its live web search, STAGES it here, reads the list back, and only on the user's
go-ahead opens Amazon's own add-to-cart page in their browser — pre-loaded, additive, and entirely
Amazon's UI from there. Checkout stays a human act: this service's one outward capability is opening
a URL whose only power is to pre-fill a cart.

User-driven only — the BUILD_TOOLS fence keeps every cart mutation (and the browser launch) off
autonomous agent runs, so text a watcher processes can never stage merchandise or pop a cart.
The staged list is session-scoped (in memory, never persisted) and clears once handed to Amazon,
because the add-to-cart link ADDS on every open — re-opening a stale list would duplicate lines
in the user's real cart.
"""
from __future__ import annotations

import threading
import webbrowser

from helix.domain.shopping import (
    MAX_ITEMS,
    CartItem,
    cart_url,
    clamp_quantity,
    extract_asin,
    price_summary,
    read_price,
)
from helix.logging_setup import get_logger

_LOG = get_logger("shopping")

_LABEL_CHARS = 80  # a staged label is one spoken-list line, not a product page

_CLEAR_WORDS = frozenset({"all", "everything", "every item", "clear", "clear it", "the whole cart"})


class ShoppingService:
    def __init__(self, *, opener=None) -> None:
        # The one outward touchpoint, injectable for tests: opener(url) launches the default
        # browser. webbrowser.open is thread-safe and detaches (os.startfile under Windows), so
        # calling it from the tool worker thread is the same posture as DesktopService.open_program.
        self._opener = opener or webbrowser.open
        self._items: list[CartItem] = []
        # Tool dispatch runs on worker threads (Console turn, remote companion) — one lock keeps
        # add/remove/open readable as atomic cart edits. The lock is NEVER held across the opener
        # call (a BROWSER-env launcher can block for the browser's whole lifetime); while an open
        # is in flight, _opening makes edits wait their turn instead of racing the handover.
        self._lock = threading.Lock()
        self._opening = False

    # ----- staging -----
    def add(self, raw_items) -> str:
        """Stage items the model resolved. Each entry: {"name": …, "asin": …, "quantity": …} —
        asin may be a bare id or an Amazon link (the ASIN is read out of it, never guessed here).
        Bad entries are refused by name so the model can ask the user instead of shipping a guess."""
        if not isinstance(raw_items, list) or not raw_items:
            return "Nothing staged — pass each item with its name and its ASIN (or Amazon link)."
        added: list[str] = []
        rejected: list[str] = []
        with self._lock:
            if self._opening:
                return ("Hold on — the cart page is opening in the browser right now; stage more "
                        "once it's handed over.")
            for entry in raw_items:
                if not isinstance(entry, dict):
                    rejected.append(str(entry)[:60] or "an unreadable entry")
                    continue
                label = " ".join(str(entry.get("name") or "").split())[:_LABEL_CHARS]
                asin = extract_asin(str(entry.get("asin") or ""))
                if asin is None:
                    rejected.append(label or str(entry.get("asin") or "")[:60] or "an unnamed item")
                    continue
                quantity = clamp_quantity(entry.get("quantity", 1))
                price = read_price(entry.get("price"))
                existing = next((i for i in self._items if i.asin == asin), None)
                if existing is not None:
                    # Same product staged again = "more of it": quantities add, the first label
                    # stays, and a fresh price read replaces a stale (or missing) one.
                    merged = CartItem(existing.label, asin,
                                      clamp_quantity(existing.quantity + quantity),
                                      price if price is not None else existing.price)
                    self._items[self._items.index(existing)] = merged
                    added.append(f"{merged.label or asin} (now {merged.quantity})")
                    continue
                if len(self._items) >= MAX_ITEMS:
                    rejected.append(f"{label or asin} — the staged cart is full at {MAX_ITEMS} "
                                    "products; the rest can go in a second round after this "
                                    "cart is handed over on the user's go-ahead")
                    continue
                self._items.append(CartItem(label or asin, asin, quantity, price))
                added.append(f"{label or asin} x{quantity}"
                             + (f" at ${price:,.2f}" if price is not None else ""))
            summary = self._summary_locked()
        parts: list[str] = []
        if added:
            parts.append("Staged: " + "; ".join(added) + ".")
        if rejected:
            parts.append(
                "Couldn't stage: " + "; ".join(rejected) + ". No real ASIN there — never guess one; "
                "ask the user for the product's Amazon link or its ASIN, or search the web again."
            )
        parts.append(summary)
        return " ".join(parts)

    def remove(self, which: str) -> str:
        """Un-stage by (part of) an item's name or its ASIN; 'everything' clears the staged list."""
        w = " ".join(str(which or "").strip().lower().split())
        if not w:
            return "Which item should I take out? Part of its name, its ASIN, or 'everything'."
        with self._lock:
            if self._opening:
                return ("Hold on — the cart page is opening in the browser right now; the staged "
                        "list is mid-handover.")
            if w in _CLEAR_WORDS:
                n = len(self._items)
                self._items.clear()
                return f"Cleared the staged cart ({n} item{'s' if n != 1 else ''} dropped)."
            hits = [i for i in self._items if w == i.asin.lower() or w in i.label.lower()]
            for h in hits:
                self._items.remove(h)
            summary = self._summary_locked()
        if not hits:
            return f"Nothing staged matches '{which}'. {summary}"
        names = "; ".join(h.label for h in hits)
        return f"Took out {names}. {summary}"

    def show(self) -> str:
        """READ-ONLY recap of what's staged. Deliberately DESCRIBES and never commands: this recap
        is readable on autonomous runs (it's outside the BUILD_TOOLS fence, like list_reminders),
        so its text must never coach a model into calling a fenced cart tool."""
        with self._lock:
            if not self._items:
                return ("The staged list is empty — nothing is queued for Amazon here. (Whatever "
                        "is already in the user's own Amazon cart, on Amazon's side, is untouched.) "
                        "A product is staged only once it has a verified ASIN from a real Amazon "
                        "product link.")
            lines = [f"{n}. {i.label} — quantity {i.quantity} (ASIN {i.asin})"
                     + (f" — about ${i.price:,.2f} each" if i.price is not None else "")
                     for n, i in enumerate(self._items, start=1)]
            # When nothing has a price read, SAY so — the honest anchor for "what's the total?"
            # (a model holding a spoken-total template must find a refusal to lean on, not a blank).
            money = price_summary(self._items) or (
                "No prices were read for these items — a total can't be estimated yet.")
        return "Staged for the Amazon cart:\n" + "\n".join(lines) + f"\n{money}" + (
            "\nThe cart opens for the user's own review and checkout only on their go-ahead.")

    # ----- the one outward act -----
    def open_cart(self) -> str:
        """Open the user's browser on Amazon's cart page with every staged item pre-loaded. Only
        pre-fills — nothing is purchased by this call, ever. Clears the staged list on success
        (Amazon's link is additive; re-opening a stale list would duplicate cart lines).

        The opener runs OUTSIDE the lock: a BROWSER-env launcher can block until the browser
        exits, and holding the lock through that would wedge every cart tool (and the turns
        parked on them) for the duration. _opening covers the gap — a second open is refused and
        edits wait, so the handover can neither double-fire nor race a mid-flight change."""
        with self._lock:
            if self._opening:
                return "The cart page is already opening in the browser — give it a moment."
            if not self._items:
                return "The staged cart is empty — stage items with add_to_cart first."
            url = cart_url(self._items)
            count = len(self._items)
            self._opening = True
        try:
            ok = self._opener(url)
        except Exception as exc:  # noqa: BLE001 - a browser hiccup must never crash a turn
            _LOG.warning("could not open the cart page: %s", exc)
            ok = False
        with self._lock:
            self._opening = False
            if ok is False:  # webbrowser.open reports failure as False; treat exceptions the same
                return ("I couldn't open the browser for the cart just now — the staged items are "
                        "still here; try again or read them back with show_cart.")
            self._items.clear()
        return (
            f"Amazon's cart page is opening in the user's browser with {count} "
            f"item{'s' if count != 1 else ''} pre-loaded. Nothing has been purchased — they review "
            "the cart and check out themselves on Amazon. The staged list here is now clear; stage "
            "fresh items to add more."
        )

    # ----- helpers -----
    def _summary_locked(self) -> str:
        """One status line; the caller already holds the lock."""
        if not self._items:
            return "The staged cart is now empty."
        total = sum(i.quantity for i in self._items)
        money = price_summary(self._items)
        return (f"Staged cart: {len(self._items)} product{'s' if len(self._items) != 1 else ''}, "
                f"{total} item{'s' if total != 1 else ''} in all."
                + (f" {money}" if money else "")
                + " Read it back to the user; the cart opens only on their go-ahead.")
