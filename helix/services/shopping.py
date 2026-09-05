"""ShoppingService — HELIX's hands (and now eyes) for the user's Amazon cart. Stages, never spends.

The faculty in one breath: the model SEARCHES Amazon through HELIX's own reads (live cards with
prices, stars, Prime — no more snippet-guessing), STAGES a verified id at a planned quantity (the
listing is read before it's accepted: a dead or wrong ASIN is refused, the live price is recorded),
keeps the staged list on disk (a restart mid-shop loses nothing), and on the user's go HANDS the
cart to Amazon by driving HELIX's own Chrome window — each product page's real Add-to-Cart button at
the real quantity — and READS THE CART BACK so what it reports is what Amazon holds. Parts lists
(the BOM behind a project) link in: "stage the IronEye parts" stages the needed rows at their
planned counts, and a handoff flips them to carted with the date and estimated spend.

Checkout stays a human act: nothing here can press Buy. User-driven only — the BUILD_TOOLS fence
keeps every cart mutation (and any browser launch) off autonomous agent runs; the read-only search
and recap tools are the only ones a watcher can reach, and their text never coaches a fenced tool.

When Chrome can't be driven (no Chrome, a wedged profile) the old one-URL handoff is the fallback —
honest about its catch: Amazon bounces that link through a sign-in page whenever the session's
authentication is over an hour old, so the user may see a password prompt before the cart.
"""
from __future__ import annotations

import threading
import webbrowser
from dataclasses import asdict, replace
from typing import Callable

from helix.domain.amazon import format_listing, format_products
from helix.domain.events import CartChanged, ProductsFound
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
_STAGED_KEY = "staged"
_HANDOFF_KEY = "last_handoff"
_SEARCH_LIMIT = 8

_LINK_CATCH = ("Amazon may show its own sign-in page first when the browser's session is more than "
               "an hour old; the pre-loaded cart appears right after signing in.")


class ShoppingService:
    def __init__(self, *, opener=None, web=None, driver=None, store=None, parts=None, bus=None,
                 clock: Callable[[], str] | None = None) -> None:
        # The link-handoff opener (fallback), injectable for tests. webbrowser.open is thread-safe
        # and detaches, the same posture as DesktopService.open_program.
        self._opener = opener or webbrowser.open
        self._web = web            # AmazonWeb — HELIX's own search/product reads (None = no eyes)
        self._driver = driver      # ChromeCart — the window HELIX drives to build the real cart
        self._store = store        # a SettingsStore (data/helix_cart.json) — the staged list survives restarts
        self._parts = parts        # PartsService — BOM rows link to staged lines; handoffs go to its ledger
        self._bus = bus
        self._clock = clock or (lambda: "")
        self._items: list[CartItem] = []
        self._catalog: dict[str, dict] = {}   # asin -> what a search/lookup read this session
        # Tool dispatch runs on worker threads (Console turn, remote companion) — one lock keeps
        # add/remove/open readable as atomic cart edits. The lock is NEVER held across the handoff
        # (a driven browser takes seconds per item; a BROWSER-env launcher can block for the
        # browser's lifetime); while a handoff is in flight, _opening makes edits wait their turn.
        self._lock = threading.Lock()
        self._opening = False
        self._load()

    # ----- persistence -----
    def _load(self) -> None:
        if self._store is None:
            return
        rows = self._store.get(_STAGED_KEY) or []
        items: list[CartItem] = []
        for r in rows if isinstance(rows, list) else []:
            try:
                asin = extract_asin(str(r.get("asin") or ""))
                if asin is None:
                    continue
                items.append(CartItem(
                    label=str(r.get("label") or asin)[:_LABEL_CHARS], asin=asin,
                    quantity=clamp_quantity(r.get("quantity", 1)), price=read_price(r.get("price")),
                    title=str(r.get("title") or "")[:200], image=str(r.get("image") or ""),
                    project=str(r.get("project") or "")[:60], note=str(r.get("note") or "")[:120],
                ))
            except (AttributeError, TypeError):
                continue
        self._items = items[:MAX_ITEMS]

    def _persist_locked(self) -> None:
        if self._store is not None:
            try:
                self._store.set(_STAGED_KEY, [asdict(i) for i in self._items])
            except Exception:  # noqa: BLE001 — a disk hiccup must never lose a turn
                _LOG.warning("couldn't persist the staged cart", exc_info=True)

    def _changed_locked(self) -> None:
        """Persist + tell the face. Called with the lock held; the event carries a snapshot so
        the handler never needs the lock back."""
        self._persist_locked()
        if self._bus is not None:
            snap = self._snapshot_locked()
            try:
                self._bus.publish(CartChanged(snapshot=snap))
            except Exception:  # noqa: BLE001
                _LOG.warning("cart event failed", exc_info=True)

    # ----- the face's view -----
    def _snapshot_locked(self) -> dict:
        priced = [i for i in self._items if i.price is not None]
        total = sum(i.quantity * i.price for i in priced)  # type: ignore[operator]
        last = (self._store.get(_HANDOFF_KEY) if self._store is not None else None) or {}
        return {
            "items": [
                {"label": i.label, "title": i.title, "asin": i.asin, "quantity": i.quantity,
                 "price": i.price, "image": i.image, "url": f"https://www.amazon.com/dp/{i.asin}",
                 "project": i.project, "note": i.note}
                for i in self._items
            ],
            "count": sum(i.quantity for i in self._items),
            "estimated_total": round(total, 2) if priced else None,
            "unpriced": len(self._items) - len(priced),
            "driver": bool(self._driver is not None and self._driver.available()),
            "opening": self._opening,
            "last_handoff": {
                "at": str(last.get("at") or ""), "how": str(last.get("how") or ""),
                "count": len(last.get("items") or []), "subtotal": str(last.get("subtotal") or ""),
            } if last else None,
        }

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    # ----- eyes: search + lookup -----
    def search(self, query: str, *, budget=None) -> str:
        """Live Amazon search: a numbered list for the model (price, stars, Prime, ASIN) and
        picture cards for the face. Read-only, nothing staged."""
        q = " ".join(str(query or "").split())[:160]
        if not q:
            return "What should I search Amazon for?"
        if self._web is None:
            return ("HELIX's own Amazon reads aren't wired on this build — search the web for the "
                    "product on amazon.com and stage it by its product link.")
        try:
            rows = self._web.search(q, limit=_SEARCH_LIMIT)
        except Exception as exc:  # noqa: BLE001 — AmazonUnavailable or a parser surprise
            _LOG.warning("amazon search failed: %s", exc)
            return (f"Amazon didn't answer HELIX's own search just now ({exc}). Fall back to your web "
                    "search for the product on amazon.com and stage it by its product LINK; the "
                    "listing is still verified before staging.")
        cap = read_price(budget) if budget is not None else None
        if cap is not None:
            within = [r for r in rows if r.price is None or r.price <= cap]
            over = len(rows) - len(within)
            rows = within
        for r in rows:
            self._catalog[r.asin] = {"title": r.title, "price": r.price, "image": r.image,
                                     "rating": r.rating, "reviews": r.reviews, "prime": r.prime}
        text = format_products(q, rows)
        if cap is not None and over:
            text += f"\n({over} result{'s' if over != 1 else ''} over the ${cap:,.2f} budget left out.)"
        self._show_products(f"Amazon: {q}", [self._card(r.asin) for r in rows])
        return text

    def lookup(self, asin_or_link: str) -> str:
        """Read one product page — the fields that answer 'is this the right part?'."""
        asin = extract_asin(str(asin_or_link or ""))
        if asin is None:
            return ("That isn't an Amazon product id or link. An ASIN is the 10-character id after "
                    "/dp/ in a product link; search_amazon finds one by description.")
        if self._web is None:
            return "HELIX's own Amazon reads aren't wired on this build — open the link with web fetch."
        try:
            listing = self._web.listing(asin)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("amazon lookup failed: %s", exc)
            return f"Amazon didn't answer the product read just now ({exc}); try again in a moment."
        if listing is None:
            return (f"Amazon has no product page for {asin} — the id is wrong or the listing is gone. "
                    "Don't stage it; search_amazon for the product instead.")
        self._catalog[asin] = {"title": listing.title, "price": listing.price, "image": listing.image,
                               "rating": listing.rating, "reviews": listing.reviews,
                               "prime": listing.prime, "can_add": listing.can_add}
        self._show_products("Amazon listing", [self._card(asin)])
        return format_listing(listing)

    def _card(self, asin: str) -> dict:
        c = self._catalog.get(asin, {})
        return {"asin": asin, "title": str(c.get("title") or asin), "price": c.get("price"),
                "rating": c.get("rating"), "reviews": c.get("reviews"), "prime": bool(c.get("prime")),
                "image": str(c.get("image") or ""), "url": f"https://www.amazon.com/dp/{asin}"}

    def _show_products(self, title: str, cards: list[dict]) -> None:
        if self._bus is None or not cards:
            return
        try:
            self._bus.publish(ProductsFound(title=title, items=tuple(cards)))
        except Exception:  # noqa: BLE001
            _LOG.warning("products event failed", exc_info=True)

    # ----- verification (the listing is read before an id is accepted) -----
    def _verify(self, asin: str) -> tuple[bool, dict]:
        """(ok, facts). ok=False means Amazon says there is no such product page — refuse it.
        facts: title/price/image/can_add when read; 'note' when the read couldn't happen."""
        hit = self._catalog.get(asin)
        if hit is not None and hit.get("title"):
            return True, dict(hit)
        if self._web is None:
            return True, {}
        try:
            listing = self._web.listing(asin)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("verify %s: %s", asin, exc)
            return True, {"note": "unverified: Amazon didn't answer the listing read"}
        if listing is None:
            return False, {}
        facts = {"title": listing.title, "price": listing.price, "image": listing.image,
                 "can_add": listing.can_add, "rating": listing.rating, "reviews": listing.reviews,
                 "prime": listing.prime}
        self._catalog[asin] = dict(facts)
        return True, facts

    # ----- staging -----
    def add(self, raw_items, *, project: str = "") -> str:
        """Stage items the model resolved. Each entry: {"name": …, "asin": …, "quantity": …,
        "price": …} — asin may be a bare id or an Amazon link. Every id is checked against the
        live listing (cached from this session's search when possible): a dead id is refused by
        name, and the price recorded is the one READ off Amazon when there is one."""
        if not isinstance(raw_items, list) or not raw_items:
            return "Nothing staged — pass each item with its name and its ASIN (or Amazon link)."
        proj = " ".join(str(project or "").split())[:60]
        added: list[str] = []
        rejected: list[str] = []
        with self._lock:
            if self._opening:
                return ("Hold on — the cart is being handed to Amazon right now; stage more once "
                        "it's over.")
            for entry in raw_items:
                if not isinstance(entry, dict):
                    rejected.append(str(entry)[:60] or "an unreadable entry")
                    continue
                label = " ".join(str(entry.get("name") or "").split())[:_LABEL_CHARS]
                asin = extract_asin(str(entry.get("asin") or ""))
                if asin is None:
                    rejected.append(label or str(entry.get("asin") or "")[:60] or "an unnamed item")
                    continue
                ok, facts = self._verify(asin)
                if not ok:
                    rejected.append(f"{label or asin} — Amazon has no product page for {asin}")
                    continue
                quantity = clamp_quantity(entry.get("quantity", 1))
                read = facts.get("price")
                price = read if read is not None else read_price(entry.get("price"))
                title = str(facts.get("title") or "")[:200]
                image = str(facts.get("image") or "")
                note = str(facts.get("note") or "")
                if facts.get("can_add") is False:
                    note = (note + "; " if note else "") + "the page has no plain Add-to-Cart button"
                existing = next((i for i in self._items if i.asin == asin), None)
                if existing is not None:
                    # Same product staged again = "more of it": quantities add, the first label
                    # stays, and a fresh read replaces a stale (or missing) price/title.
                    merged = replace(existing, quantity=clamp_quantity(existing.quantity + quantity),
                                     price=price if price is not None else existing.price,
                                     title=title or existing.title, image=image or existing.image,
                                     project=proj or existing.project, note=note or existing.note)
                    self._items[self._items.index(existing)] = merged
                    added.append(f"{merged.label or asin} (now {merged.quantity})")
                    self._link_part(proj or existing.project, merged.label, asin, merged.price)
                    continue
                if len(self._items) >= MAX_ITEMS:
                    rejected.append(f"{label or asin} — the staged cart is full at {MAX_ITEMS} "
                                    "products; the rest can go in a second round after this "
                                    "cart is handed over on the user's go-ahead")
                    continue
                self._items.append(CartItem(label or (title[:_LABEL_CHARS] if title else asin), asin,
                                            quantity, price, title=title, image=image, project=proj,
                                            note=note))
                line = f"{label or title[:60] or asin} x{quantity}"
                if price is not None:
                    line += f" at ${price:,.2f}" + (" (live)" if read is not None else " (as given)")
                if title and label and not _same_thing(label, title):
                    line += f" — listing reads '{title[:90]}'"
                if note:
                    line += f" [{note}]"
                added.append(line)
                self._link_part(proj, label or title, asin, price)
            summary = self._summary_locked()
            if added:
                self._changed_locked()
        parts: list[str] = []
        if added:
            parts.append("Staged: " + "; ".join(added) + ".")
        if rejected:
            parts.append(
                "Couldn't stage: " + "; ".join(rejected) + ". Never guess an id — search_amazon "
                "for the product and stage the ASIN from the results, or ask the user for its link."
            )
        parts.append(summary)
        return " ".join(parts)

    def _link_part(self, project: str, name: str, asin: str, price: float | None) -> None:
        if self._parts is None or not project:
            return
        try:
            if self._parts.resolve(project, name, asin, price):
                self._parts.set_status(project, [asin], "staged")
        except Exception:  # noqa: BLE001
            _LOG.warning("parts link failed", exc_info=True)

    def remove(self, which: str) -> str:
        """Un-stage by (part of) an item's name or its ASIN; 'everything' clears the staged list."""
        w = " ".join(str(which or "").strip().lower().split())
        if not w:
            return "Which item should I take out? Part of its name, its ASIN, or 'everything'."
        with self._lock:
            if self._opening:
                return ("Hold on — the cart is being handed to Amazon right now; the staged "
                        "list is mid-handover.")
            if w in _CLEAR_WORDS:
                n = len(self._items)
                dropped = list(self._items)
                self._items.clear()
                self._changed_locked()
                self._unstage_parts(dropped)
                return f"Cleared the staged cart ({n} item{'s' if n != 1 else ''} dropped)."
            hits = [i for i in self._items if w == i.asin.lower() or w in i.label.lower()
                    or w in i.title.lower()]
            for h in hits:
                self._items.remove(h)
            if hits:
                self._changed_locked()
            summary = self._summary_locked()
        if not hits:
            return f"Nothing staged matches '{which}'. {summary}"
        self._unstage_parts(hits)
        names = "; ".join(h.label for h in hits)
        return f"Took out {names}. {summary}"

    def _unstage_parts(self, items: list[CartItem]) -> None:
        if self._parts is None:
            return
        for i in items:
            if i.project:
                try:
                    self._parts.set_status(i.project, [i.asin], "need")
                except Exception:  # noqa: BLE001
                    pass

    def set_quantity(self, asin: str, quantity) -> str:
        """The cart panel's ± buttons: an exact count for one staged line (0 removes it)."""
        a = extract_asin(str(asin or ""))
        if a is None:
            return "Which item? Pass its ASIN."
        try:
            q = int(quantity)
        except (TypeError, ValueError):
            q = 1
        with self._lock:
            if self._opening:
                return "The cart is mid-handover — one moment."
            hit = next((i for i in self._items if i.asin == a), None)
            if hit is None:
                return f"Nothing staged with ASIN {a}."
            if q <= 0:
                self._items.remove(hit)
                self._changed_locked()
                summary = self._summary_locked()
            else:
                self._items[self._items.index(hit)] = replace(hit, quantity=clamp_quantity(q))
                self._changed_locked()
                summary = self._summary_locked()
        if q <= 0:
            self._unstage_parts([hit])
            return f"Took out {hit.label}. {summary}"
        return f"{hit.label}: quantity now {clamp_quantity(q)}. {summary}"

    def show(self) -> str:
        """READ-ONLY recap of what's staged. Deliberately DESCRIBES and never commands: this recap
        is readable on autonomous runs (it's outside the BUILD_TOOLS fence, like list_reminders),
        so its text must never coach a model into calling a fenced cart tool."""
        with self._lock:
            if not self._items:
                last = (self._store.get(_HANDOFF_KEY) if self._store is not None else None) or {}
                tail = ""
                if last:
                    tail = (f" The last cart handed to Amazon ({str(last.get('at') or '')[:16]}) had "
                            f"{len(last.get('items') or [])} product(s)"
                            + (f", Amazon's subtotal {last.get('subtotal')}" if last.get("subtotal") else "")
                            + ".")
                return ("The staged list is empty — nothing is queued for Amazon here. (Whatever "
                        "is already in the user's own Amazon cart, on Amazon's side, is untouched.) "
                        "A product is staged only once it has a verified ASIN from a real Amazon "
                        "listing." + tail)
            lines = [f"{n}. {i.label} — quantity {i.quantity} (ASIN {i.asin})"
                     + (f" — about ${i.price:,.2f} each" if i.price is not None else "")
                     + (f" — for {i.project}" if i.project else "")
                     + (f" [{i.note}]" if i.note else "")
                     for n, i in enumerate(self._items, start=1)]
            # When nothing has a price read, SAY so — the honest anchor for "what's the total?"
            money = price_summary(self._items) or (
                "No prices were read for these items — a total can't be estimated yet.")
        return "Staged for the Amazon cart:\n" + "\n".join(lines) + f"\n{money}" + (
            "\nThe cart is handed to Amazon for the user's own review and checkout only on their go-ahead.")

    # ----- the parts list bridge -----
    def stage_parts(self, project: str) -> str:
        """Stage every NEEDED row of a parts list that has an ASIN, at its planned quantity; name
        the rows still without an id so the model resolves them (search_amazon) and stages them
        with add_to_cart(project=…)."""
        if self._parts is None:
            return "Parts lists aren't wired on this build."
        rows = self._parts.rows(project)
        if not rows:
            return f"I don't have a parts list called '{project}'. Saved lists: " + (
                ", ".join(self._parts.projects()) or "none") + "."
        need = [r for r in rows if r.status == "need"]
        ready = [r for r in need if r.asin]
        missing = [r for r in need if not r.asin]
        out: list[str] = []
        if ready:
            out.append(self.add([{"name": r.name, "asin": r.asin, "quantity": r.quantity,
                                  "price": r.price} for r in ready], project=project))
        else:
            out.append(f"No needed rows on '{project}' have an Amazon id yet.")
        if missing:
            out.append("Still to resolve before they can be staged: "
                       + "; ".join(f"{r.name} (qty {r.quantity}" + (f", {r.spec}" if r.spec else "") + ")"
                                   for r in missing)
                       + ". Find each with search_amazon and stage it with add_to_cart, passing "
                       f"project='{project}' so the row is linked.")
        skipped = [r for r in rows if r.status != "need"]
        if skipped:
            out.append("Left alone (on hand, already staged, or carted): "
                       + ", ".join(r.name for r in skipped) + ".")
        return " ".join(out)

    # ----- the one outward act -----
    def open_cart(self, *, resend_last: bool = False, on_progress=None) -> str:
        """Hand the staged list to Amazon. With the Chrome driver: HELIX's own window adds each
        item at its quantity and opens the cart, which is then READ BACK — the report is what the
        cart holds. Without it: the one-URL handoff in the default browser (with its sign-in
        catch). Only pre-fills — nothing is purchased by this call, ever."""
        with self._lock:
            if self._opening:
                return "The cart is already opening — it is being handed to Amazon; give it a moment."
            if resend_last:
                return self._resend_locked()
            if not self._items:
                return "The staged cart is empty — stage items with add_to_cart first."
            items = list(self._items)
            self._opening = True
            self._changed_locked()
        try:
            if self._driver is not None and self._driver.available():
                try:
                    return self._drive(items, on_progress)
                except Exception as exc:  # noqa: BLE001 — ChromeCartError or a surprise
                    _LOG.warning("chrome cart handoff failed, falling back to the link: %s", exc)
                    how_failed = str(exc)[:120]
            else:
                how_failed = "no Chrome to drive" if self._driver is not None else ""
            return self._link(items, how_failed)
        finally:
            with self._lock:
                self._opening = False
                self._changed_locked()

    def _drive(self, items: list[CartItem], on_progress) -> str:
        results, state = self._driver.add_items(items, on_progress=on_progress)
        lines: list[str] = []
        done: list[CartItem] = []
        kept: list[CartItem] = []
        for item, res in zip(items, results):
            in_cart = state.quantity_of(item.asin) if state is not None else None
            if res.added > 0:
                done.append(item)
                line = f"{item.label} — added {res.added}"
                if in_cart is not None:
                    line += f" (Amazon's cart now holds {in_cart})"
                if res.reason:
                    line += f"; {res.reason}"
                lines.append(line)
            else:
                why = {
                    "needs-option": "the listing needs an option picked (size/color/pack) first",
                    "buying-options": "it sells only through 'See all buying options'",
                    "unavailable": "Amazon lists it as currently unavailable",
                    "robot": "Amazon showed a robot check in the window",
                    "no-button": "no Add-to-Cart button on the page",
                }.get(res.reason, res.reason or "couldn't add it")
                kept.append(replace(item, note=f"not added: {why}"))
                lines.append(f"{item.label} — NOT added: {why}")
        with self._lock:
            self._items = [i for i in self._items if i.asin not in {d.asin for d in done}]
            for k in kept:  # keep the misses staged, with the reason showing on the panel
                self._items = [k if i.asin == k.asin else i for i in self._items]
            if self._store is not None and done:
                self._store.set(_HANDOFF_KEY, {
                    "at": self._clock(), "how": "chrome",
                    "items": [asdict(i) for i in done],
                    "subtotal": state.subtotal if state is not None else "",
                })
            self._changed_locked()
        self._ledger(done, how="chrome", subtotal=(state.subtotal if state is not None else ""))
        head = (f"Amazon's cart is open in HELIX's own browser window with {len(done)} of {len(items)} "
                f"product{'s' if len(items) != 1 else ''} added.")
        if state is not None:
            if state.subtotal:
                head += f" Amazon's subtotal reads {state.subtotal}."
            if state.signed_in is False:
                head += (" That window isn't signed in to Amazon yet — the cart is a guest cart; "
                         "signing in there (once) merges it into the user's account, and checkout "
                         "needs the sign-in anyway.")
        else:
            head += " (The cart page couldn't be read back — ask the user to glance at it.)"
        tail = (" Nothing has been purchased — the user reviews and checks out on Amazon. Items that "
                "were added left the staged list; any that weren't stay staged with the reason.")
        return head + "\n" + "\n".join(lines) + tail

    def _link(self, items: list[CartItem], driver_problem: str) -> str:
        url = cart_url(items)
        try:
            ok = self._opener(url)
        except Exception as exc:  # noqa: BLE001 - a browser hiccup must never crash a turn
            _LOG.warning("could not open the cart page: %s", exc)
            ok = False
        if ok is False:
            return ("I couldn't open the browser for the cart just now — the staged items are "
                    "still here; try again or read them back with show_cart.")
        with self._lock:
            if self._store is not None:
                self._store.set(_HANDOFF_KEY, {"at": self._clock(), "how": "link",
                                               "items": [asdict(i) for i in items], "url": url})
            self._items.clear()
            self._changed_locked()
        self._ledger(items, how="link", subtotal="")
        why = f" (HELIX's own cart window couldn't be driven: {driver_problem}.)" if driver_problem else ""
        return (
            f"Amazon's add-to-cart page is opening in the user's browser with {len(items)} "
            f"item{'s' if len(items) != 1 else ''} pre-loaded.{why} Tell the user the catch: {_LINK_CATCH} "
            "If the cart didn't appear, open_cart with resend_last=true sends the same list again. "
            "Nothing has been purchased — they review the cart and check out themselves on Amazon. "
            "The staged list here is now clear."
        )

    def _resend_locked(self) -> str:
        last = (self._store.get(_HANDOFF_KEY) if self._store is not None else None) or {}
        url = str(last.get("url") or "")
        if not url:
            return "There's no earlier link handoff to resend — stage items and open the cart instead."
        try:
            ok = self._opener(url)
        except Exception:  # noqa: BLE001
            ok = False
        if ok is False:
            return "I couldn't open the browser just now."
        return (f"Re-sent the last cart link ({len(last.get('items') or [])} item(s)) to the browser. "
                f"{_LINK_CATCH} Careful: Amazon's link ADDS on every completed open, so if the first "
                "one did land, the cart now has doubles to trim.")

    def _ledger(self, items: list[CartItem], *, how: str, subtotal: str) -> None:
        if self._parts is None or not items:
            return
        priced = [i for i in items if i.price is not None]
        est = round(sum(i.quantity * i.price for i in priced), 2) if priced else None  # type: ignore[operator]
        by_project: dict[str, list[CartItem]] = {}
        for i in items:
            by_project.setdefault(i.project, []).append(i)
        try:
            for project, group in by_project.items():
                self._parts.record_handoff(
                    project=project, how=how + (f" · Amazon subtotal {subtotal}" if subtotal else ""),
                    est_total=(round(sum(i.quantity * i.price for i in group if i.price is not None), 2)
                               if any(i.price is not None for i in group) else None),
                    items=[{"label": i.label, "asin": i.asin, "quantity": i.quantity, "price": i.price}
                           for i in group])
                if project:
                    self._parts.set_status(project, [i.asin for i in group], "carted")
        except Exception:  # noqa: BLE001
            _LOG.warning("ledger write failed (est %s)", est, exc_info=True)

    # ----- the live cart, read from HELIX's window -----
    def check_amazon_cart(self) -> str:
        """Open HELIX's cart window on Amazon's cart page and read what it holds."""
        if self._driver is None or not self._driver.available():
            return ("I can't read the live Amazon cart on this machine (no Chrome to drive) — the "
                    "user can open amazon.com's cart themselves; show_cart recaps what's staged here.")
        try:
            state = self._driver.read_cart(show=True)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("cart read failed: %s", exc)
            return f"I couldn't read the Amazon cart just now ({str(exc)[:100]})."
        if state is None:
            return "The cart page opened but I couldn't read its rows — ask the user to look."
        if not state.rows:
            head = "Amazon's cart in HELIX's window is empty."
        else:
            head = (f"Amazon's cart holds {len(state.rows)} product{'s' if len(state.rows) != 1 else ''}"
                    + (f", subtotal {state.subtotal}" if state.subtotal else "") + ":")
        lines = [f"- {r.title or r.asin} — quantity {r.quantity}"
                 + (f" at ${r.price:,.2f}" if r.price is not None else "") + f" (ASIN {r.asin})"
                 for r in state.rows]
        if state.signed_in is False:
            lines.append("(This window isn't signed in to Amazon — it's a guest cart until the user "
                         "signs in there.)")
        return "\n".join([head, *lines])

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
                + " Read it back to the user; the cart is handed to Amazon only on their go-ahead.")


def _same_thing(label: str, title: str) -> bool:
    """A rough 'the user's words match the listing' check: most label words appear in the title."""
    words = [w for w in "".join(ch if ch.isalnum() else " " for ch in label.lower()).split() if len(w) > 2]
    if not words:
        return True
    t = title.lower()
    return sum(1 for w in words if w in t) >= max(1, (len(words) + 1) // 2)
