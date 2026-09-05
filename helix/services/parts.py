"""PartsService — the durable bill of materials behind a project, and the ledger of what was carted.

The problem this solves: over a long build conversation ("IronEye") the parts list lived only in the
model's memory of the chat. Every time the user asked for "the BOM table" it was re-derived, and the
ASINs drifted from turn to turn (the same microphone got three different ids across one afternoon).
Here the list is a record: a named project holds rows with a name, a spec, the planned quantity, the
verified ASIN and price once resolved, and a status — needed, on hand, staged, carted. "Stage the
IronEye parts" then means exactly the needed rows at exactly their planned quantities, and a handoff
to Amazon flips them to carted with the date and the estimated spend, so "what did I buy for IronEye
and when?" answers from the ledger. Expense tracking on HELIX's side; Amazon's order history stays
the truth for money actually charged.

Stored in data/helix_parts.json (guard-safe like the other volatile stores). Pure bookkeeping — no
network; the resolving and the carting are the shopping service's job.
"""
from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass, replace
from typing import Callable

from helix.domain.shopping import clamp_quantity, extract_asin, read_price
from helix.logging_setup import get_logger
from helix.ports.stores import SettingsStore

_LOG = get_logger("parts")

_KEY = "projects"
_LEDGER_KEY = "handoffs"
STATUSES = ("need", "on_hand", "staged", "carted")
_STATUS_WORDS = {
    "need": "need", "needed": "need", "to order": "need", "order": "need", "buy": "need", "todo": "need",
    "on hand": "on_hand", "on_hand": "on_hand", "have": "on_hand", "owned": "on_hand", "already have": "on_hand",
    "staged": "staged", "in cart": "carted", "carted": "carted", "ordered": "carted", "bought": "carted",
}
_MAX_ROWS = 80
_MAX_PROJECTS = 40
_MAX_LEDGER = 200
_CLEAR_WORDS = frozenset({"all", "everything", "every item", "clear", "the whole list"})


@dataclass(frozen=True)
class Part:
    name: str
    quantity: int = 1
    spec: str = ""
    asin: str = ""
    price: float | None = None
    status: str = "need"
    note: str = ""
    updated: str = ""

    @property
    def key(self) -> str:
        return _norm(self.name)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _squash(text: str) -> str:
    """A project name as spoken: case- and space-insensitive ('iron eye' is 'IronEye')."""
    return _norm(text).replace(" ", "")


def _slug(project: str) -> str:
    return " ".join((project or "").strip().split())[:60]


class PartsService:
    def __init__(self, store: SettingsStore, clock: Callable[[], str] | None = None) -> None:
        self._store = store
        self._clock = clock or (lambda: "")
        self._lock = threading.RLock()

    # ----- storage -----
    def _load(self) -> dict[str, list[dict]]:
        raw = self._store.get(_KEY) or {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, data: dict[str, list[dict]]) -> None:
        self._store.set(_KEY, data)

    def _find_project(self, data: dict, project: str) -> str | None:
        want = _squash(project)
        if not want:
            return None
        for name in data:
            if _squash(name) == want:
                return name
        for name in data:  # a partial spoken name ("iron eye" for "IronEye BOM")
            if want in _squash(name) or _squash(name) in want:
                return name
        return None

    def projects(self) -> list[str]:
        with self._lock:
            return list(self._load().keys())

    def rows(self, project: str) -> list[Part]:
        with self._lock:
            data = self._load()
            name = self._find_project(data, project)
            return [self._part(r) for r in (data.get(name) or [])] if name else []

    @staticmethod
    def _part(raw: dict) -> Part:
        return Part(
            name=str(raw.get("name") or ""), quantity=clamp_quantity(raw.get("quantity", 1)),
            spec=str(raw.get("spec") or ""), asin=str(raw.get("asin") or ""),
            price=read_price(raw.get("price")), status=str(raw.get("status") or "need"),
            note=str(raw.get("note") or ""), updated=str(raw.get("updated") or ""),
        )

    # ----- writes -----
    def save(self, project: str, raw_items) -> str:
        """Create or update a project's rows (upsert by name). Each item: name, quantity, spec,
        asin (bare or Amazon link), price, status (need/on hand/carted), note. A row's given
        fields overwrite; omitted fields keep what was there."""
        proj = _slug(project)
        if not proj:
            return "Which project is this parts list for? Give it a short name."
        if not isinstance(raw_items, list) or not raw_items:
            return "Nothing to save — pass the parts as a list, each with at least a name."
        with self._lock:
            data = self._load()
            name = self._find_project(data, proj) or proj
            if name not in data and len(data) >= _MAX_PROJECTS:
                return f"That's {_MAX_PROJECTS} projects already — retire one before adding another."
            rows = [self._part(r) for r in (data.get(name) or [])]
            saved: list[str] = []
            skipped: list[str] = []
            for entry in raw_items:
                if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
                    skipped.append("an unnamed part" if isinstance(entry, dict) else str(entry)[:40])
                    continue
                pname = " ".join(str(entry["name"]).split())[:80]
                existing = next((r for r in rows if r.key == _norm(pname)), None)
                base = existing or Part(name=pname)
                fields: dict = {"name": pname if existing is None else existing.name,
                                "updated": self._clock()}
                if "quantity" in entry and entry["quantity"] is not None:
                    fields["quantity"] = clamp_quantity(entry["quantity"])
                if entry.get("spec") is not None:
                    fields["spec"] = " ".join(str(entry["spec"]).split())[:160]
                if entry.get("note") is not None:
                    fields["note"] = " ".join(str(entry["note"]).split())[:160]
                if entry.get("asin"):
                    asin = extract_asin(str(entry["asin"]))
                    if asin is None:
                        skipped.append(f"{pname} (not a real ASIN or Amazon link — row saved without it)")
                    else:
                        fields["asin"] = asin
                if entry.get("price") is not None:
                    p = read_price(entry["price"])
                    if p is not None:
                        fields["price"] = p
                if entry.get("status"):
                    st = _STATUS_WORDS.get(str(entry["status"]).strip().lower().replace("-", " "))
                    if st:
                        fields["status"] = st
                part = replace(base, **fields)
                if existing is None:
                    if len(rows) >= _MAX_ROWS:
                        skipped.append(f"{pname} — the list is full at {_MAX_ROWS} rows")
                        continue
                    rows.append(part)
                else:
                    rows[rows.index(existing)] = part
                saved.append(f"{part.name} x{part.quantity}" + (f" ({part.asin})" if part.asin else ""))
            data[name] = [asdict(r) for r in rows]
            self._save(data)
        out = f"Saved to the '{name}' parts list: " + "; ".join(saved) + "." if saved else ""
        if skipped:
            out += " Skipped: " + "; ".join(skipped) + "."
        return (out + " " + self._recap_line(name, rows)).strip()

    def remove(self, project: str, which: str) -> str:
        w = _norm(which)
        with self._lock:
            data = self._load()
            name = self._find_project(data, project)
            if name is None:
                return f"I don't have a parts list called '{project}'." + self._known(data)
            if w in {_norm(x) for x in _CLEAR_WORDS}:
                del data[name]
                self._save(data)
                return f"Dropped the whole '{name}' parts list."
            rows = [self._part(r) for r in data[name]]
            hits = [r for r in rows if w and (w == _norm(r.asin) or w in r.key)]
            if not hits:
                return f"Nothing in '{name}' matches '{which}'. " + self._recap_line(name, rows)
            rows = [r for r in rows if r not in hits]
            data[name] = [asdict(r) for r in rows]
            self._save(data)
        return f"Took {'; '.join(h.name for h in hits)} off '{name}'. " + self._recap_line(name, rows)

    def set_status(self, project: str, asins: list[str] | tuple[str, ...], status: str) -> list[str]:
        """Flip the rows with these ASINs to `status`; returns the names touched."""
        if status not in STATUSES:
            return []
        wanted = {a.upper() for a in asins}
        touched: list[str] = []
        with self._lock:
            data = self._load()
            name = self._find_project(data, project)
            if name is None:
                return []
            rows = [self._part(r) for r in data[name]]
            for i, r in enumerate(rows):
                if r.asin.upper() in wanted:
                    rows[i] = replace(r, status=status, updated=self._clock())
                    touched.append(r.name)
            data[name] = [asdict(r) for r in rows]
            self._save(data)
        return touched

    def resolve(self, project: str, part_name: str, asin: str, price: float | None) -> bool:
        """Record the ASIN (and price) a part resolved to. False when the row doesn't exist."""
        with self._lock:
            data = self._load()
            name = self._find_project(data, project)
            if name is None:
                return False
            rows = [self._part(r) for r in data[name]]
            key = _norm(part_name)
            for i, r in enumerate(rows):
                if r.key == key or (key and key in r.key):
                    rows[i] = replace(r, asin=asin.upper(), price=price if price is not None else r.price,
                                      updated=self._clock())
                    data[name] = [asdict(x) for x in rows]
                    self._save(data)
                    return True
        return False

    # ----- the ledger -----
    def record_handoff(self, *, project: str, items: list[dict], est_total: float | None,
                       how: str) -> None:
        """One line per cart handed to Amazon: when, for what, what, and roughly how much."""
        with self._lock:
            ledger = self._store.get(_LEDGER_KEY) or []
            if not isinstance(ledger, list):
                ledger = []
            ledger.append({"at": self._clock(), "project": _slug(project), "items": items,
                           "est_total": est_total, "how": how})
            self._store.set(_LEDGER_KEY, ledger[-_MAX_LEDGER:])

    def ledger(self, project: str = "", limit: int = 12) -> list[dict]:
        with self._lock:
            rows = self._store.get(_LEDGER_KEY) or []
        if not isinstance(rows, list):
            return []
        want = _norm(project)
        if want:
            rows = [r for r in rows if want in _norm(str(r.get("project") or ""))]
        return list(rows)[-limit:]

    # ----- read-only recap (readable on autonomous runs — DESCRIBES, never coaches a fenced tool) -----
    def show(self, project: str = "") -> str:
        with self._lock:
            data = self._load()
            name = self._find_project(data, project) if project else None
            if project and name is None:
                return f"No parts list called '{project}' is saved." + self._known(data)
            names = [name] if name else list(data.keys())
            if not names:
                return ("No parts lists are saved yet. A project's bill of materials — names, planned "
                        "quantities, verified Amazon ids and prices, what's on hand vs. still needed — "
                        "is kept here once one is saved.")
            out: list[str] = []
            for n in names:
                rows = [self._part(r) for r in data.get(n) or []]
                out.append(self._table(n, rows))
            hand = self.ledger(project or "", limit=5)
        if hand:
            out.append("Handed to Amazon (most recent last):")
            for h in hand:
                items = h.get("items") or []
                total = h.get("est_total")
                out.append(f"- {str(h.get('at') or '')[:16]} {h.get('project') or ''}: "
                           f"{len(items)} product{'s' if len(items) != 1 else ''}"
                           + (f", about ${total:,.2f}" if isinstance(total, (int, float)) else "")
                           + f" ({h.get('how') or ''})")
        return "\n".join(out)

    def _table(self, name: str, rows: list[Part]) -> str:
        lines = [f"Parts list '{name}' ({len(rows)} row{'s' if len(rows) != 1 else ''}):"]
        for i, r in enumerate(rows, start=1):
            bits = [f"qty {r.quantity}", {"need": "still needed", "on_hand": "on hand",
                                             "staged": "staged for the cart", "carted": "carted"}.get(r.status, r.status)]
            if r.asin:
                bits.append(f"ASIN {r.asin}")
            if r.price is not None:
                bits.append(f"about ${r.price:,.2f}")
            if r.spec:
                bits.append(r.spec)
            if r.note:
                bits.append(r.note)
            lines.append(f"{i}. {r.name} — " + "; ".join(bits))
        lines.append(self._recap_line(name, rows))
        return "\n".join(lines)

    @staticmethod
    def _recap_line(name: str, rows: list[Part]) -> str:
        need = [r for r in rows if r.status == "need"]
        unresolved = [r for r in need if not r.asin]
        priced = [r for r in need if r.price is not None]
        est = sum(r.price * r.quantity for r in priced)  # type: ignore[operator]
        parts = [f"{len(need)} of {len(rows)} still needed"]
        if unresolved:
            parts.append(f"{len(unresolved)} without an Amazon id yet ({', '.join(r.name for r in unresolved[:5])})")
        if priced:
            parts.append(f"needed rows with a price come to about ${est:,.2f}")
        return f"'{name}': " + "; ".join(parts) + "."

    @staticmethod
    def _known(data: dict) -> str:
        return f" Saved lists: {', '.join(data.keys())}." if data else " No lists are saved yet."
