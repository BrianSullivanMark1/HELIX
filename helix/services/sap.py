"""SapService — HELIX's SAP data-model faculty: the catalog, the join engine, the EDW overlay and
the Snowflake writer behind five tools and the SAP panel.

The rule this service exists for (READ_ME/SAP.md): field names, keys, joins and filters are READ
from the dictionary and the curated layer, never recited from memory. Every answer names its
provenance — "SAP data dictionary via leanx.eu, read 2026-09-15", "curated join, verified",
"curated join, unverified", "your EDW column list (pasted 2026-09-15)" — and an unknown is an
unknown: "MDTB isn't in the SAP dictionary source" (after an on-demand fetch found nothing) or
"…on-demand fetching is off" (when the setting is off), never a guessed field.

Model-facing methods (`*_text`) return finished, size-capped text with the load-bearing facts
FIRST (keys, joins, standard filters, EDW availability), because only ~1,500 characters of a tool
result survive into later turns. Panel-facing methods (`*_dict`) return plain dicts for the
routes. Both sit on the same pure logic in helix/domain/sap. The text methods never raise: a
fault is a sentence (logged), because a tool result is the model's ear and a traceback in it is
worse than silence.

Verification: every curated edge starts unverified (curated.py). The first time the graph is
used — and again after a fetch lands — each edge whose two tables the catalog holds is checked
field by field against the dictionary, and the graph is built with the confirmed edges flagged,
in curated order (the graph's tie-break; moving an edge would reroute a plan). An edge that names
a field the dictionary lacks stays unverified and is logged — that IS the bug this faculty exists
to end, surfacing in the log rather than in a query.

Thread rules: tools call this from the turn's worker thread, routes from the uvicorn loop thread;
the overlay store is a JsonSettings (its own lock) and this service serialises its own
read-modify-write — and every use of the (not thread-safe) JoinGraph — with an RLock. Catalog
reads are cached and cheap; a fetch is at most a few seconds, throttled by the adapter, and never
runs under the lock.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from dataclasses import replace
from functools import wraps

from helix.adapters.sap_catalog import WIDE_SOURCE, CatalogStore
from helix.domain.sap import curated
from helix.domain.sap.curated import (
    BUSINESS_TERMS,
    CODE_VALUES,
    CURATED_JOINS,
    FIELD_NOTES,
    RECIPES,
    STANDARD_FILTERS,
    TABLE_NOTES,
    WIP_REPORT,
    Term,
)
from helix.domain.sap.graph import CLIENT_TABLE, JoinGraph
from helix.domain.sap.model import (
    FieldDef,
    Filter,
    JoinEdge,
    JoinPlan,
    TableDef,
    normalize_field,
    normalize_table,
)
from helix.domain.sap.overlay import (
    MISSING,
    PRESENT,
    EdwOverlay,
    EdwTable,
    parse_information_schema,
)
from helix.domain.sap.sql import (
    DEFAULT_LIMIT,
    ColumnSpec,
    EdwNaming,
    QuerySpec,
    SqlResult,
    quote_literal,
    write_sql,
)
from helix.logging_setup import get_logger
from helix.ports.clock import Clock
from helix.ports.stores import SettingsStore

_LOG = get_logger("sap")

EDW_KEY = "edw"                     # the overlay document's key in its JsonSettings file
FETCH_SETTING = "sap_fetch_tables"  # settings: on-demand leanx.eu fetching (default True)
TABLE_TEXT_MAX = 12_000             # a table_text reply's ceiling (fields page through `offset`)
FIELDS_PER_PAGE = 60
FOR_TURN_CHARS = 1_600              # the per-turn block ceiling (like verified.for_turn)
LOOKUP_TEXT_MAX = 6_000
# ECC-wide field hits (tables outside the catalog) a lookup shows beside catalogued hits: the wide
# index is ordered by table name and truncated, so left uncapped it fills a lookup for 'posting
# date' with /LSIERP/… tables alphabetically ahead of anything a production report reads.
WIDE_REST_MAX = 4
# The user's plant, seeded only when no EDW record exists yet: the writer marks the filter
# optional and `set_plant` with nothing clears it, so a wrong seed costs one sentence, while no
# seed costs a plant filter on every query until someone remembers to set it.
DEFAULT_PLANT = "1006"
LEANX = "leanx.eu"

SUMMARY_JOINS = 14                  # joins shown in a table summary before "… section joins"
SUMMARY_FIELDS = 30                 # key + noted fields shown in a summary
FOR_TURN_TABLES = 2
FOR_TURN_JOINS = 3
EDW_LIST_MAX = 60                   # tables listed by 'show'
_LIST_SHOWN = 8                     # names shown before "…" in a compact list

# The WIP report is per ORDER OPERATION: AFVC is the root, the order (AFKO → AUFK) and its item
# (AFPO) hang off it, the WBS element and project come through the item (AFPO.PROJN — the header's
# PSPEL is usually blank), the work center through AFVC.ARBID, and everything aggregated or
# three-way (statuses, last confirmation, serials, MRP messages, shortages, the order's MRP
# controller with its name on the order's plant) arrives as a recipe CTE so the operation rows are
# never multiplied.
WIP_TABLES: tuple[str, ...] = (
    "AFVC", "AFKO", "AUFK", "AFPO", "AFVV", "PRPS", "PROJ", "MAKT", "CRHD", "CRTX",
)
WIP_RECIPES: tuple[str, ...] = (
    "system_status", "latest_confirmation", "serial_numbers", "mrp_controller", "mrp_messages",
    "component_shortages",
)
WIP_NOTES: tuple[str, ...] = (
    "MRP Controller is the order's own AFKO.DISPO with its T024D name looked up on the order's "
    "plant (AUFK.WERKS) — the mrp_controller recipe, not the material's controller from MARC.",
    "Columns with no standard SAP source (Rating Rwk, Priority, Notes) are written as NULL with "
    "their label so the report keeps its shape — replace them with the site's Z-fields once the "
    "EDW column list shows them.",
)
REPORTS: dict[str, str] = {"wip": "the WIP report — one row per order operation, 27 columns"}

_WORD = re.compile(r"[a-z0-9][a-z0-9_/]*")
_STOP = frozenset({
    "the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "is", "are", "what", "which",
    "how", "do", "does", "i", "my", "me", "this", "that", "it", "its", "with", "by", "from", "at",
    "as", "be", "can", "could", "should", "would", "please", "find", "show", "tell", "give", "get",
    "sap", "where", "want", "need", "know", "look", "up", "about", "there", "any", "some",
})
# A table name as people type it — upper-case, 3–10 characters, '/' for a namespace — standing on
# its own (not inside a longer word, and not 'AFRU.BUDAT': that is a field reference, whose table
# half still matches on its own). Lower-case mentions are left alone on purpose: 'mara' is a
# table, 'mast' is a table, and so are a dozen English words.
_TABLE_TOKEN = re.compile(r"(?<![A-Za-z0-9_/])[A-Z][A-Z0-9_/]{2,9}(?![A-Za-z0-9_/])")
_CURATED_BY_KEY: dict[tuple, JoinEdge] = {e.key: e for e in CURATED_JOINS}


# ----- small pure helpers -----
def _words(text: object) -> list[str]:
    """The query words that carry meaning: lowercase, stopwords out, order kept, duplicates
    dropped. A query of nothing but stopwords keeps its words rather than matching nothing."""
    raw = _WORD.findall(str(text or "").lower())
    out: list[str] = []
    for w in raw:
        if w not in out:
            out.append(w)
    kept = [w for w in out if w not in _STOP]
    return kept or out


def _phrase_words(phrase: str) -> list[str]:
    return _WORD.findall(phrase.lower())


def _listing(items: Iterable[str], shown: int = _LIST_SHOWN) -> str:
    names = list(items)
    if len(names) <= shown:
        return ", ".join(names)
    return ", ".join(names[:shown]) + f", … ({len(names) - shown} more)"


def _gist(text: str, *, at_least: int = 30, at_most: int = 100) -> str:
    """The first clause of a description — cut at the first ' — ', ', ' or '; ' past `at_least`
    characters (so 'yield, scrap and work' is not cut at its comma; a full stop is not a cut
    because 'e.g. ' has one), capped at `at_most`."""
    s = " ".join(str(text or "").split()).rstrip(".")
    cut = len(s)
    for sep in (" — ", ", ", "; "):
        at = s.find(sep, at_least)
        if at != -1:
            cut = min(cut, at)
    s = s[:cut].rstrip(" ,.")
    return s if len(s) <= at_most else s[: at_most - 1].rstrip() + "…"


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    tail = f"\n… (cut at {limit} characters — ask for one section, or a field offset)"
    return text[: limit - len(tail)].rstrip() + tail


def _on_text(edge: JoinEdge) -> str:
    """'MANDT, AUFPL, APLZL' / 'MANDT, ARBID = OBJID' — the join columns as a summary reads them."""
    return ", ".join(lf if lf == rf else f"{lf} = {rf}" for lf, rf in edge.on)


def _on_compact(edge: JoinEdge) -> str:
    """'MANDT+AUFPL+APLZL' / 'MANDT+ARBID=OBJID' — the per-turn block's spelling."""
    return "+".join(lf if lf == rf else f"{lf}={rf}" for lf, rf in edge.on)


def _edge_tags(edge: JoinEdge) -> str:
    """'(1:n, verified)', '(n:1, curated, unverified)', '(n:1, dictionary)' — what an answer may
    claim about a join: verified means both sides' fields were found in the dictionary."""
    tags = [edge.cardinality] if edge.cardinality else []
    if edge.kind == "fk":
        tags.append("dictionary")
    elif edge.verified:
        tags.append("verified")
    else:
        tags += [edge.kind or "curated", "unverified"]
    return ", ".join(tags)


def _filter_prose(flt: Filter) -> str:
    """AUFK.AUTYP = '10' — production orders only … (optional). Prose that reads like SQL; the
    writer renders the real condition."""
    op = " ".join(str(flt.op or "=").split()).upper()
    ref = f"{flt.table}.{flt.field}"
    if op in ("IS NULL", "IS NOT NULL"):
        cond = f"{ref} {op}"
    elif op in ("IN", "NOT IN"):
        members = ", ".join(quote_literal(v.strip()) for v in str(flt.value or "").split(","))
        cond = f"{ref} {op} ({members})"
    else:
        cond = f"{ref} {op} {quote_literal(flt.value)}"
    if flt.why:
        cond += f" — {flt.why}"
    if flt.optional:
        cond += " (optional)"
    return cond


def _filter_row(flt: Filter) -> dict:
    return {"table": flt.table, "field": flt.field, "op": flt.op or "=", "value": flt.value,
            "why": flt.why, "optional": bool(flt.optional)}


def _edge_filters_short(edge: JoinEdge) -> str:
    """'CRHD.OBJTY = 'A'' — the filters a hop needs, in one bracket."""
    return "; ".join(f"{f.table}.{f.field} {f.op or '='} {quote_literal(f.value)}"
                     for f in edge.filters)


def _alias_of_label(label: str) -> str:
    """'Work Center Description' → WORK_CENTER_DESCRIPTION, 'Operation/Activity' →
    OPERATION_ACTIVITY: a bare identifier, so the SQL reads without quoted names."""
    return re.sub(r"[^A-Z0-9]+", "_", str(label or "").upper()).strip("_") or "COLUMN"


def _codes_text(table: str, fld: str) -> str:
    codes = CODE_VALUES.get((table, fld))
    if not codes:
        return ""
    shown = list(codes.items())[:4]
    text = ", ".join(f"{quote_literal(k)} {v}" for k, v in shown)
    return text + (", …" if len(codes) > 4 else "")


def _str_items(value: object) -> list[str]:
    """A list of strings out of whatever a caller sent: a JSON array, one name, 'AFRU, AFVC'."""
    if value is None:
        return []
    if isinstance(value, str):
        return [p for p in re.split(r"[,\s]+", value) if p]
    if isinstance(value, dict):
        return []
    try:
        return [str(v) for v in value if v is not None and str(v).strip()]
    except TypeError:
        return [str(value)]


def _dict_items(value: object) -> list:
    if value is None or isinstance(value, (str, bytes)):
        return []
    if isinstance(value, dict):
        return [value]
    try:
        return list(value)
    except TypeError:
        return []


def _column_spec(item: object) -> ColumnSpec | None:
    if isinstance(item, ColumnSpec):
        return item
    if not isinstance(item, dict):
        return None
    table = normalize_table(item.get("table") or "")
    fld = normalize_field(item.get("field") or "")
    alias = " ".join(str(item.get("alias") or "").split())
    expression = " ".join(str(item.get("expression") or "").split())
    if not fld and not expression:
        return None
    return ColumnSpec(table=table, field=fld, alias=alias, expression=expression)


def _filter_spec(item: object) -> Filter | None:
    if isinstance(item, Filter):
        return item
    if not isinstance(item, dict):
        return None
    table = normalize_table(item.get("table") or "")
    fld = normalize_field(item.get("field") or "")
    if not table or not fld:
        return None
    op = " ".join(str(item.get("op") or "=").split()).upper() or "="
    value = item.get("value")
    return Filter(table=table, field=fld, op=op, value="" if value is None else str(value),
                  why=" ".join(str(item.get("why") or "").split()))


def _truthy(raw: object) -> bool:
    """A settings value as a switch: the UI may store a bool, a 0/1, or the word."""
    if raw is None:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(raw)


def _from_overlay(name: str, entry: EdwTable | None) -> TableDef | None:
    """A table the dictionary does not know but the user's EDW does (a Z table whose columns were
    pasted): its columns as recorded, no types, no keys — so it can be named in a query and its
    provenance says exactly where it came from."""
    if entry is None or not entry.columns:
        return None
    return TableDef(
        name=name, description="not in the SAP dictionary — columns as your EDW lists them",
        fields=tuple(FieldDef(name=c, position=i + 1) for i, c in enumerate(entry.columns)),
        source="edw", fetched_at=entry.recorded_at,
    )


def _guarded(fallback: str | None = None):
    """The text methods never raise: a tool result is the model's ear. `fallback` None means
    'say what broke' (the tool relays the same sentence); for_turn falls back to '' because a
    per-turn block that fails must add nothing, not a sentence."""
    def deco(method):
        @wraps(method)
        def wrapper(self, *args, **kwargs):
            try:
                return method(self, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — a catalog fault is a sentence, logged, never a tool error
                _LOG.warning("SapService.%s failed", method.__name__, exc_info=True)
                if fallback is not None:
                    return fallback
                return f"The SAP catalog couldn't answer that: {exc}"
        return wrapper
    return deco


class SapService:
    def __init__(self, catalog: CatalogStore, edw_store: SettingsStore, clock: Clock, *,
                 settings: SettingsStore | None = None) -> None:
        """`edw_store` is the dedicated JsonSettings for data/helix_sap_edw.json; `settings` the
        app settings (FETCH_SETTING read live per call)."""
        self._catalog = catalog
        self._edw_store = edw_store
        self._clock = clock
        self._settings = settings
        self._lock = threading.RLock()
        self._overlay = self._load_overlay()
        # The graph is built on first use (and rebuilt after a fetch) by _rebuild, which verifies
        # the curated edges first and hands them over IN CURATED ORDER: the graph breaks ties
        # between equal-cost routes by the order the expert listed them, so verifying by
        # re-adding an edge (which moves it to the back of the list) would quietly reroute
        # AFRU → PRPS through the settlement rule instead of the order item.
        self._graph: JoinGraph | None = None
        self._seen: set[str] = set()              # tables handed to the graph by table()
        self._verified_keys: set[tuple] = set()   # curated edges confirmed against the dictionary
        self._join_faults: list[str] = []      # curated edges naming a field the dictionary lacks

    # ----- persistence of the overlay -----
    def _load_overlay(self) -> EdwOverlay:
        try:
            doc = self._edw_store.get(EDW_KEY)
        except Exception:  # noqa: BLE001 — a store hiccup reads as an empty record, never as a crash
            _LOG.warning("couldn't read the EDW record", exc_info=True)
            doc = None
        if doc is None:
            return EdwOverlay(plant=DEFAULT_PLANT)
        return EdwOverlay.from_dict(doc)

    def _save(self) -> None:
        """Called under the lock after every write; a failed save is logged, the session keeps
        the record in memory."""
        try:
            self._edw_store.set(EDW_KEY, self._overlay.to_dict())
        except Exception:  # noqa: BLE001 — persistence is best effort; the answer already stands
            _LOG.warning("couldn't persist the EDW record", exc_info=True)

    def _today(self) -> str:
        try:
            return self._clock.now().date().isoformat()
        except Exception:  # noqa: BLE001 — a broken clock must not stop a lookup; the catalog stamps today itself when told nothing
            return ""

    def _fetch_on(self) -> bool:
        """The live setting (read per call, so a toggle takes effect at once) AND the adapter's
        own switch."""
        on = True
        if self._settings is not None:
            try:
                on = _truthy(self._settings.get(FETCH_SETTING, True))
            except Exception:  # noqa: BLE001 — an unreadable setting keeps the default
                on = True
        return on and bool(self._catalog.fetch_allowed())

    # ----- catalog access (shared by tools and routes) -----
    def _describe_outside(self, name: str) -> TableDef | None:
        """Beyond layers 1/2: the wide index, then the user's EDW record. Good enough to name
        fields, types and provenance — never handed to the graph (see _lookup)."""
        tdef = self._catalog.describe_wide(name)
        if tdef is None:
            with self._lock:
                tdef = _from_overlay(name, self._overlay.tables.get(name))
        return tdef

    def _lookup(self, name: str) -> TableDef | None:
        """What the graph asks for a table it has not seen: layers 1/2 ONLY. A wide-index record
        has no key flags — CRTX would stop being a text table and lose its SPRAS filter, one row
        per language — and its foreign keys are single-column hints (AFRU.APLZL → AFVC without
        MANDT or AUFPL) that the graph would write as joins: the guessed-join bug, from the
        catalog's own gap. The curated edges cover such a table; the writer still gets its
        record for types and field checks."""
        return self._catalog.get(normalize_table(name))

    def table(self, name: str, *, fetch: bool = True) -> TableDef | None:
        """Layer 1/2 → on-demand fetch (when allowed and `fetch`) → wide index → the EDW record.
        A dictionary table (layers 1/2) is added to the join graph on first sight; a freshly
        fetched one has the graph rebuilt on its next use, so the curated joins that touch it
        get verified."""
        n = normalize_table(name)
        if not n:
            return None
        tdef = self._catalog.get(n)
        fresh = False
        if tdef is None and fetch and self._fetch_on():
            tdef = self._catalog.fetch_table(n, today=self._today())
            fresh = tdef is not None
        if tdef is not None:
            with self._lock:
                if fresh:
                    self._graph = None          # rebuilt, verified, on the next use
                if n not in self._seen:
                    self._seen.add(n)
                    if self._graph is not None:
                        self._graph.add_table(tdef)
            return tdef
        return self._describe_outside(n)

    def provenance(self, table: TableDef) -> str:
        """'SAP data dictionary via leanx.eu, read 2026-09-15' / 'wide index (svn11x snapshot,
        no keys)' / 'your EDW column list (pasted 2026-09-15)' — the sentence that follows any
        fact from it."""
        src = (table.source or "").strip().lower()
        when = f", read {table.fetched_at}" if table.fetched_at else ""
        if src == "leanx":
            return f"SAP data dictionary via {LEANX}{when}"
        if src == WIDE_SOURCE:
            return f"wide index ({table.source} snapshot, no keys)"
        if src == "edw":
            pasted = f" (pasted {table.fetched_at})" if table.fetched_at else ""
            return f"your EDW column list{pasted}"
        if src == "curated":
            return "curated (HELIX's expert layer)"
        if src:
            return f"SAP data dictionary via {table.source}{when}"
        return "an unrecorded source"

    # ----- the graph and the verification pass -----
    def _g(self) -> JoinGraph:
        """The graph, built (verified, in curated order) on first use and after a fetch. Call
        under the lock."""
        if self._graph is None:
            self._graph = self._rebuild()
        return self._graph

    def _rebuild(self) -> JoinGraph:
        """Verify every curated edge against the catalog, then build the graph from the edges in
        the order the expert listed them (verified ones flagged, not moved). The graph pulls
        dictionary edges lazily through `_lookup` (layers 1/2 — never a fetch: a shortest-path
        search must not wait on leanx.eu); the tables table() has already seen are handed over
        up front."""
        self._verified_keys = set()
        edges = [replace(e, verified=True) if self._verify(e) else e for e in CURATED_JOINS]
        graph = JoinGraph(edges, lookup=self._lookup, language=curated.LANGUAGE)
        for name in sorted(self._seen):
            tdef = self._catalog.get(name)
            if tdef is not None:
                graph.add_table(tdef)
        return graph

    def _verify(self, edge: JoinEdge) -> bool:
        """Confirm both sides' fields exist in the dictionary (layers 1/2 only — the wide index
        has every field of every table and would 'verify' anything, and an EDW column list is not
        the dictionary). A fault is logged once: it is the guessed-field bug, in the expert
        layer."""
        ldef, rdef = self._catalog.get(edge.left), self._catalog.get(edge.right)
        if ldef is None or rdef is None:
            return False
        missing = [f"{edge.left}.{lf}" for lf, _rf in edge.on if not ldef.has_field(lf)]
        missing += [f"{edge.right}.{rf}" for _lf, rf in edge.on if not rdef.has_field(rf)]
        if missing:
            fault = f"{edge.left} → {edge.right} names {', '.join(missing)}, not in the dictionary"
            if fault not in self._join_faults:
                self._join_faults.append(fault)
                _LOG.warning("curated SAP join %s", fault)
            return False
        self._verified_keys.add(edge.key)
        return True

    def _edges(self, name: str) -> tuple[JoinEdge, ...]:
        """Every join of `name`, oriented with it on the left, curated first — minus the client
        table: MANDT → T000 is a check the dictionary declares on every table and a join nobody
        writes."""
        with self._lock:
            edges = self._g().edges_of(name)
        return tuple(e for e in edges if e.right != CLIENT_TABLE)

    def _plan(self, names: list[str], root: str) -> JoinPlan:
        with self._lock:
            return self._g().plan(names, root=root or None)

    # ----- the overlay's answers, worded once -----
    def naming(self) -> EdwNaming:
        with self._lock:
            ov = self._overlay
            return EdwNaming(prefix=ov.prefix, view_prefix=ov.view_prefix, plant=ov.plant,
                             language=curated.LANGUAGE)

    def overlay(self) -> EdwOverlay:
        """The live record (mutate it only through edw_record, which saves)."""
        return self._overlay

    def _view(self, name: str) -> str:
        return f"{self._overlay.view_prefix}{name}"

    def _edw_phrase(self, name: str, tdef: TableDef | None = None) -> str:
        """'TV_AFRU present, 144 columns recorded (custom: ZZ…)' | 'TV_X missing (told on …)' |
        'TV_X not recorded'."""
        with self._lock:
            ov = self._overlay
            view = self._view(name)
            status = ov.status(name)
            entry = ov.tables.get(name)
            if status == PRESENT:
                cols = ov.columns(name)
                if not cols:
                    return f"{view} present (no column list recorded)"
                text = f"{view} present, {len(cols)} columns recorded"
                if tdef is not None and tdef.source != "edw":
                    custom = ov.custom_columns(name, tdef.field_names())
                    if custom:
                        text += f" (custom: {_listing(custom)})"
                    lacking = ov.missing_columns(name, tdef.field_names())
                    if lacking:
                        text += (f"; {len(lacking)} dictionary fields the EDW lacks: "
                                 f"{_listing(lacking, 6)}")
                return text
            if status == MISSING:
                when = entry.recorded_at if entry is not None else ""
                return f"{view} missing" + (f" (told on {when})" if when else " (you told me)")
            return f"{view} not recorded"

    def _short_status(self, name: str) -> str:
        """'present (144 cols)' / 'present' / 'missing' / 'unknown' — for headers and blocks."""
        with self._lock:
            status = self._overlay.status(name)
            cols = self._overlay.columns(name)
        if status == PRESENT:
            return f"present ({len(cols)} cols)" if cols else "present"
        return "missing" if status == MISSING else "unknown"

    def _edw_mark(self, table: str, fld: str, *, unknown: str = "") -> str:
        with self._lock:
            has = self._overlay.has_column(table, fld)
        if has is None:
            return f"[EDW {unknown}]" if unknown else ""
        return "[EDW ✓]" if has else "[EDW ✗]"

    # ----- shared renderers -----
    def _filters_of(self, name: str, tdef: TableDef | None) -> tuple[Filter, ...]:
        """The standard filters plus the language filter a text table needs (dictionary-driven:
        any table whose key has a LANG field)."""
        out = list(STANDARD_FILTERS.get(name, ()))
        lang = tdef.language_key() if tdef is not None else ""
        if lang:
            out.append(Filter(table=name, field=lang, op="=", value=curated.LANGUAGE,
                              why="one language — SAP's language key is one character "
                                  "('E', never 'EN')"))
        return tuple(out)

    def _recipes_of(self, name: str) -> list:
        return [r for r in RECIPES.values()
                if any(normalize_table(t) == name for t, _f in r.join_on)]

    def _field_line(self, table: str, f: FieldDef, *, codes: bool = False) -> str:
        bits = [f.name + (" (key)" if f.key else "")]
        if f.type_label():
            bits.append(f.type_label())
        if f.description:
            bits.append(f.description)
        line = "  ".join(bits)
        if f.check_table:
            line += f"  → {f.check_table}"
        mark = self._edw_mark(table, f.name)
        if mark:
            line += f"  {mark}"
        note = FIELD_NOTES.get((table, f.name), "")
        if note:
            line += f" — {note}"
        if codes:
            values = _codes_text(table, f.name)
            if not values and f.values:
                values = ", ".join(f"{quote_literal(k)} {v}" for k, v in f.values[:4])
            if values:
                line += f" [values: {values}]"
        return line

    def _join_line(self, name: str, edge: JoinEdge) -> str:
        line = f"{name} → {edge.right} on {_on_text(edge)} ({_edge_tags(edge)})"
        if edge.note:
            line += f" — {edge.note}"
        if edge.filters:
            line += f" [needs {_edge_filters_short(edge)}]"
        return line

    def _join_row(self, name: str, edge: JoinEdge) -> dict:
        """The panel's join row. `on` keeps the expert's own orientation: a curated edge written
        AFVC → AFRU is shown to AFRU as direction 'in' with AFVC's columns first."""
        orig = _CURATED_BY_KEY.get(edge.key)
        shown, direction = edge, "out"
        if orig is not None and orig.left != name:
            shown, direction = edge.reversed(), "in"
        return {
            "other": edge.right, "direction": direction,
            "on": [[lf, rf] for lf, rf in shown.on], "kind": shown.kind,
            "cardinality": shown.cardinality, "note": shown.note, "verified": bool(shown.verified),
            "source": shown.source, "filters": [_filter_row(f) for f in shown.filters],
        }

    def _unknown_sentence(self, name: str) -> str:
        if self._fetch_on():
            return (f"{name} isn't in the SAP dictionary source ({LEANX}) — is it a custom (Z) "
                    f"table? Paste its columns and I'll record it. (If {LEANX} was unreachable "
                    f"just now, ask again in a moment.)")
        return (f"{name} isn't in my catalog and on-demand fetching is off (setting "
                f"{FETCH_SETTING}). Turn it on, or paste the table's columns and I'll record them.")

    def _summary(self, name: str, tdef: TableDef) -> str:
        """Load-bearing facts first — the line the model will quote, then the key, the EDW
        status, the joins with their verification, the filters — and the field list last."""
        scope = "/".join(s for s in (tdef.module, tdef.component) if s)
        head = f"{name} — {tdef.description or 'no description'}"
        if scope:
            head += f" ({scope})"
        lines = [f"{head}. Source: {self.provenance(tdef)}."]
        if tdef.category in ("STRUCT", "INTTAB"):
            lines.append("A structure — it never holds rows and is never in an EDW; the tables "
                         "behind it are the ones to query.")
        keys = tdef.primary_key()
        if keys:
            lines.append("Key: " + " + ".join(keys))
        elif tdef.source == WIDE_SOURCE:
            lines.append("Key: unknown — the wide index carries no key flags (a fetch from "
                         f"{LEANX} would add them)")
        elif tdef.source == "edw":
            lines.append("Key: unknown — recorded from your EDW column list, not the dictionary")
        else:
            lines.append("Key: none recorded")
        lang = tdef.language_key()
        if lang:
            lines.append(f"Language key: {lang} — filter {lang} = "
                         f"{quote_literal(curated.LANGUAGE)} (one character; 'EN' returns nothing)")
        lines.append("EDW: " + self._edw_phrase(name, tdef))
        note = TABLE_NOTES.get(name, "")
        if note:
            lines.append(f"About: {note}")
        edges = self._edges(name)
        if edges:
            lines.append("Joins:")
            lines += [f"  {self._join_line(name, e)}" for e in edges[:SUMMARY_JOINS]]
            if len(edges) > SUMMARY_JOINS:
                lines.append(f"  … {len(edges) - SUMMARY_JOINS} more — section joins")
        else:
            lines.append("Joins: none known (no curated join, no dictionary foreign key)")
        filters = self._filters_of(name, tdef)
        if filters:
            lines.append("Filters:")
            lines += [f"  {_filter_prose(f)}" for f in filters]
        recipes = self._recipes_of(name)
        if recipes:
            lines.append("Recipes: " + "; ".join(
                f"{r.name} (on {'+'.join(f for t, f in r.join_on if normalize_table(t) == name)}"
                f" — {_gist(r.description)})" for r in recipes))
        total = len(tdef.fields)
        lines.append(f"Fields ({total}):")
        shown = [f for f in tdef.fields if f.key or (name, f.name) in FIELD_NOTES][:SUMMARY_FIELDS]
        lines += [f"  {self._field_line(name, f)}" for f in shown]
        if total > len(shown):
            pages = ", ".join(str(o) for o in range(0, total, FIELDS_PER_PAGE)[:6])
            lines.append(f"  … all {total} fields: section fields ({FIELDS_PER_PAGE} per page; "
                         f"offset {pages}{', …' if total > 6 * FIELDS_PER_PAGE else ''})")
        return "\n".join(lines)

    def _fields_page(self, name: str, tdef: TableDef, offset: object) -> str:
        try:
            start = max(0, int(offset or 0))
        except (TypeError, ValueError):
            start = 0
        total = len(tdef.fields)
        if not total:
            return f"{name} has no field list recorded ({self.provenance(tdef)})."
        if start >= total:
            last = ((total - 1) // FIELDS_PER_PAGE) * FIELDS_PER_PAGE
            return (f"{name} has {total} fields; offset {start} is past the end (the last page "
                    f"starts at {last}).")
        end = min(total, start + FIELDS_PER_PAGE)
        head = f"{name} fields — showing {start + 1}-{end} of {total}"
        if end < total:
            head += f"; ask for offset {end} for the next page"
        lines = [head + f". Source: {self.provenance(tdef)}."]
        lines += [f"  {self._field_line(name, f, codes=True)}" for f in tdef.fields[start:end]]
        return "\n".join(lines)

    def _joins_page(self, name: str, tdef: TableDef) -> str:
        edges = self._edges(name)
        keys = " + ".join(tdef.primary_key()) or "unknown"
        if not edges:
            return (f"{name} (key {keys}): no joins known — not curated, and no dictionary "
                    f"foreign key. Tell me the table it joins and on which columns.")
        lines = [f"{name} (key {keys}) — {len(edges)} joins, curated first:"]
        for e in edges:
            lines.append(f"  {self._join_line(name, e)}")
            for f in e.filters:
                lines.append(f"      needs {_filter_prose(f)}")
        recipes = self._recipes_of(name)
        if recipes:
            lines.append("Recipes that join here: " + ", ".join(r.name for r in recipes))
        return "\n".join(lines)

    # ----- model-facing text -----
    def _search(self, query: str, limit: int) -> tuple[list[dict], list[dict]]:
        """(field hits, table rows): the business vocabulary scored by word overlap, the index
        by name/description, the wide index by field — and, when the wide index has nothing (or
        is not shipped), the fields of the catalogued tables. Hits are de-duplicated on
        (table, field), best first."""
        qwords = _words(query)
        limit = max(1, int(limit or 1))
        hits: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def add(table: str, fld: str, description: str, dtype: str, note: str, source: str):
            key = (table, fld)
            if key in seen or len(hits) >= limit:
                return
            seen.add(key)
            with self._lock:
                edw = self._overlay.has_column(table, fld)
            hits.append({"table": table, "field": fld, "description": description, "type": dtype,
                         "edw": edw, "note": note, "source": source})

        if qwords:
            scored = [(self._term_score(qwords, t), i, t) for i, t in enumerate(BUSINESS_TERMS)]
            for _score, _i, term in sorted((s for s in scored if s[0] > 0),
                                          key=lambda s: (-s[0], s[1])):
                tdef = self.table(term.table, fetch=False)
                fdef = tdef.field(term.field) if tdef is not None else None
                description = fdef.description if fdef is not None else ""
                note = term.note or FIELD_NOTES.get((term.table, term.field), "")
                add(term.table, term.field, description, fdef.type_label() if fdef else "",
                    note, "vocabulary")
        tables = [{"name": r["name"], "description": r["description"], "module": r["module"],
                   "edw": self._overlay.status(r["name"])}
                  for r in self._catalog.search_index(query, limit=limit)]
        if qwords:
            # The catalogued tables' own fields next — every shipped or fetched TableDef, the
            # production-report scope first: they are the tables a report reads and their
            # descriptions are SAP's own, so 'posting date' lands on AFRU / MKPF / EKBE.BUDAT.
            for table, fdef in self._scan_catalogued_fields(qwords, limit):
                add(table, fdef.name, fdef.description, fdef.type_label(),
                    FIELD_NOTES.get((table, fdef.name), ""), "catalog")
            # The wide index only for tables the catalog does NOT hold: the in-scope ones the
            # build could not ship (COBRB — leanx has no page) first, then the ECC-wide rest,
            # capped when the catalog answered at all (WIDE_REST_MAX) and free to fill the list
            # when it did not — 'which table has a field called X' is what the index is for.
            known = set(self._catalog.names())
            wide = [r for r in self._catalog.search_wide_fields(qwords, limit=max(4 * limit, 40))
                    if r["table"] not in known]
            in_scope = [r for r in wide
                        if r["table"] in TABLE_NOTES or curated.scope_module(r["table"])]
            rest = [r for r in wide if r not in in_scope]
            if hits:
                rest = rest[:WIDE_REST_MAX]
            for r in in_scope + rest:
                add(r["table"], r["field"], r["description"], r["data_type"],
                    FIELD_NOTES.get((r["table"], r["field"]), ""), "wide index")
        return hits, tables

    @staticmethod
    def _term_score(qwords: list[str], term: Term) -> float:
        """3 for the exact phrase, 2+ when every query word is in a phrase, 1+ when at least 60%
        are — 0 otherwise."""
        best = 0.0
        qset = set(qwords)
        for phrase in term.words.split(","):
            pwords = _phrase_words(phrase)
            if not pwords:
                continue
            if pwords == qwords:
                return 3.0
            overlap = len(qset & set(pwords)) / len(qset)
            if overlap >= 0.6:
                best = max(best, 1.0 + overlap + (0.5 if qset <= set(pwords) else 0.0))
        return best

    def _scan_catalogued_fields(self, qwords: list[str], limit: int) -> list[tuple[str, FieldDef]]:
        """Field hits across every shipped/fetched table (a thousand TableDefs: a fifth of a
        second the first time, cached after): a query word equal to a field name first, then
        every query word in the description (the field means what was asked), then — for a
        one-word query only — a name prefix, then a name containing the word. A word buried in
        a longer name (DATE in DATE_FROM) is noise beside a phrase like 'posting date', so a
        multi-word query matches on meaning. At equal rank the production-report scope comes
        first, then the catalog's own order."""
        uppers = [w.upper() for w in qwords]
        single = uppers[0] if len(uppers) == 1 else ""
        ranked: list[tuple[int, int, int, str, FieldDef]] = []
        for i, name in enumerate(self._catalog.names()):
            tdef = self._catalog.get(name)
            if tdef is None:
                continue
            scope = 0 if (name in TABLE_NOTES or curated.scope_module(name)) else 1
            for f in tdef.fields:
                desc = f.description.lower()
                if any(f.name == u for u in uppers):
                    rank = 0
                elif all(w in desc for w in qwords):
                    rank = 1
                elif single and f.name.startswith(single):
                    rank = 2
                elif single and single in f.name:
                    rank = 3
                else:
                    continue
                ranked.append((rank, scope, i, name, f))
        ranked.sort(key=lambda r: (r[0], r[1], r[2], r[4].position))
        return [(name, f) for _r, _s, _i, name, f in ranked[:limit]]

    @_guarded()
    def lookup_text(self, query: str, *, limit: int = 12) -> str:
        """Word search: business terms (curated.BUSINESS_TERMS), table names/descriptions in the
        index, field names/descriptions in the wide index. Groups hits by table; says which
        tables the EDW has."""
        q = " ".join(str(query or "").split())
        if not q:
            return "What should I look up? Give me a business term, a field or a table name."
        hits, tables = self._search(q, limit)
        if not hits and not tables:
            return (f"Nothing in the catalog matches '{q}'. Try a table (AFRU), a field (ARBPL) "
                    f"or a business term (posting date, work center). Tell me a table for its "
                    f"full definition.")
        lines = [f"Lookup '{q}' — {len(hits)} field hits in "
                 f"{len({h['table'] for h in hits})} tables, {len(tables)} table matches:"]
        by_table: dict[str, list[dict]] = {}
        for h in hits:
            by_table.setdefault(h["table"], []).append(h)
        index_desc = {r["name"]: r["description"] for r in tables}
        for table, rows in by_table.items():
            desc = index_desc.get(table) or self._describe(table)
            lines.append(f"{table}{' — ' + desc if desc else ''} [EDW {self._short_status(table)}]")
            for h in rows:
                line = f"  {table}.{h['field']}"
                if h["description"]:
                    line += f" — {h['description']}"
                if h["type"]:
                    line += f" ({h['type']})"
                line += " " + self._edw_mark(table, h["field"], unknown="?")
                if h["note"]:
                    line += f" — {h['note']}"
                lines.append(line)
        rest = [r for r in tables if r["name"] not in by_table]
        if rest:
            lines.append("Tables:")
            for r in rest:
                mod = f" ({r['module']})" if r["module"] else ""
                lines.append(f"  {r['name']} — {r['description']}{mod} "
                             f"[EDW {self._short_status(r['name'])}]")
        lines.append("Tell me a table for its full definition.")
        return _cap("\n".join(lines), LOOKUP_TEXT_MAX)

    def _describe(self, table: str) -> str:
        tdef = self.table(table, fetch=False)
        if tdef is not None and tdef.description:
            return tdef.description
        note = TABLE_NOTES.get(table, "")
        return note.split(" — ")[0].split(":")[0] if note else ""

    @_guarded()
    def table_text(self, name: str, *, section: str = "summary", offset: int = 0) -> str:
        """section 'summary': what it is (curated note + dictionary description), provenance,
        primary key, EDW status (+ custom ZZ columns when recorded), the joins out and in (curated
        first, with keys and verification), the standard filters, the field count and the
        FieldDef lines for the key fields and the noted fields. 'fields': FIELDS_PER_PAGE fields
        from `offset` as 'NAME  TYPE  description  [→ CHECK]  [EDW ✓/✗]'. 'joins': every edge
        with its full key, cardinality, note and filters. Unknown table → the fetch/ask sentence."""
        n = normalize_table(name)
        if not n:
            return "Which table? Give me its name (AFRU, TV_AFRU …)."
        tdef = self.table(n)
        if tdef is None:
            return self._unknown_sentence(n)
        sec = " ".join(str(section or "summary").split()).lower()
        if sec == "fields":
            text = self._fields_page(n, tdef, offset)
        elif sec == "joins":
            text = self._joins_page(n, tdef)
        else:
            text = self._summary(n, tdef)
        return _cap(text, TABLE_TEXT_MAX)

    def _names(self, tables: object) -> list[str]:
        return list(dict.fromkeys(n for n in (normalize_table(t) for t in _str_items(tables)) if n))

    def _render(self, spec: QuerySpec, plan: JoinPlan) -> SqlResult:
        """write_sql over a plan, with every plan table's TableDef (for dates and field checks),
        the naming and the overlay. A column expression written as 'SUM({})' is completed with
        the column's alias.FIELD now that the plan has named the aliases."""
        defs: dict[str, TableDef] = {}
        for t in plan.tables:
            d = self.table(t, fetch=False)
            if d is not None:
                defs[t] = d
        cols = []
        for c in spec.columns:
            if c.expression and "{}" in c.expression and c.table and c.field:
                c = replace(c, expression=c.expression.replace(
                    "{}", f"{plan.alias_of(c.table)}.{c.field}"))
            cols.append(c)
        with self._lock:
            return write_sql(replace(spec, columns=tuple(cols)), plan, self.naming(), tables=defs,
                             overlay=self._overlay)

    @_guarded()
    def join_text(self, tables: Iterable[str], *, root: str = "") -> str:
        """The join plan in words plus a FROM/JOIN skeleton (no SELECT list) for the tables, the
        filters with reasons, and which tables the EDW lacks."""
        names = self._names(tables)
        if not names:
            return "Which tables should I join? Give me two or more names."
        for n in names:
            self.table(n)       # an unknown table is fetched here, so its keys are in the plan
        plan = self._plan(names, normalize_table(root))
        lines = [f"Join plan for {', '.join(names)} — FROM {plan.root}"
                 + ("" if plan.steps else " (no joins)")]
        for step in plan.steps:
            edge = step.edge
            on = " AND ".join(edge.as_conditions(step.alias_left, step.alias_right))
            line = f"  {step.alias_left} → {step.alias_right} ON {on} ({_edge_tags(edge)})"
            if edge.note:
                line += f" — {edge.note}"
            lines.append(line)
        if plan.filters:
            lines.append("Filters:")
            lines += [f"  {_filter_prose(f)}" for f in plan.filters]
        if plan.unreachable:
            lines.append("Unreachable: " + ", ".join(plan.unreachable))
        if plan.notes:
            lines.append("Notes:")
            lines += [f"  {n}" for n in plan.notes]
        result = self._render(QuerySpec(tables=tuple(names), root=plan.root), plan)
        lines += ["SQL skeleton:", result.sql]
        lines.append("EDW: " + "; ".join(
            self._edw_phrase(t) for t in (*plan.tables, *plan.unreachable)))
        if result.warnings:
            lines.append("Warnings:")
            lines += [f"  {w}" for w in result.warnings]
        return _cap("\n".join(lines), TABLE_TEXT_MAX)

    @_guarded()
    def sql_text(self, tables: Iterable[str], columns: Iterable[dict] = (), *,
                 filters: Iterable[dict] = (), recipes: Iterable[str] = (), root: str = "",
                 limit: int = 100) -> str:
        """The full query (write_sql) followed by its warnings and notes. `columns` items are
        {table, field, alias?, expression?}; `filters` items {table, field, op?, value}."""
        spec = self.query_spec(tables, columns, filters, recipes, root, limit)
        if not spec.tables:
            return "Which tables should the query read? Give me at least one name."
        result = self.write(spec)
        lines = [result.sql]
        if result.warnings:
            lines.append("Warnings:")
            lines += [f"  {w}" for w in result.warnings]
        if result.notes:
            lines.append("Notes:")
            lines += [f"  {n}" for n in result.notes]
        return _cap("\n".join(lines), TABLE_TEXT_MAX)

    def _report_spec(self) -> QuerySpec:
        columns = tuple(
            ColumnSpec(table="", field="", alias=_alias_of_label(col.label),
                       expression=col.expression or "NULL")
            for col in WIP_REPORT
        )
        return QuerySpec(tables=WIP_TABLES, columns=columns, recipes=WIP_RECIPES, root="AFVC",
                         limit=DEFAULT_LIMIT)

    @_guarded()
    def report_text(self, name: str = "wip") -> str:
        """A known report (curated.WIP_REPORT) column by column — label, source, status, note —
        and the full query for it."""
        key = " ".join(str(name or "wip").split()).lower()
        if key not in REPORTS:
            known = "; ".join(f"{k}: {v}" for k, v in REPORTS.items())
            return f"I know one report — {known}. Ask for it by that name."
        lines = [f"WIP report — {len(WIP_REPORT)} columns, one row per order operation "
                 f"(FROM AFVC; joins {', '.join(WIP_TABLES[1:])}; recipes "
                 f"{', '.join(WIP_RECIPES)}):"]
        for col in WIP_REPORT:
            line = f"  {col.label} — {col.source} ({col.status})"
            if col.note:
                line += f": {col.note}"
            lines.append(line)
        result = self.write(self._report_spec())
        lines += ["Query:", result.sql]
        if result.warnings:
            lines.append("Warnings:")
            lines += [f"  {w}" for w in result.warnings]
        lines.append("Notes:")
        lines += [f"  {n}" for n in (*WIP_NOTES, *result.notes)]
        return _cap("\n".join(lines), TABLE_TEXT_MAX)

    @_guarded()
    def edw_text(self, action: str, *, table: str = "", text: str = "") -> str:
        """WRITE. action 'record_columns' (text = the pasted column list), 'information_schema'
        (text = the export), 'missing', 'present', 'forget', 'set_prefix', 'set_plant', 'show'."""
        return str(self.edw_record(action, table=table, text=text).get("message") or "")

    # ----- the per-turn block -----
    def _mentioned(self, user_text: str) -> list[str]:
        """Catalog tables the user named — upper-case tokens in the catalog's index — then the
        tables behind multi-word business terms ('posting date' → AFRU). Single-word terms are
        left out: 'order', 'batch' and 'plant' are everyday words."""
        text = str(user_text or "")
        if not text.strip():
            return []
        known = set(self._catalog.names())
        out: list[str] = []
        for tok in _TABLE_TOKEN.findall(text):
            n = normalize_table(tok)
            if n in known and n not in out:
                out.append(n)
        lowered = " " + " ".join(re.findall(r"[a-z0-9]+", text.lower())) + " "
        for term in BUSINESS_TERMS:
            for phrase in term.words.split(","):
                words = _phrase_words(phrase)
                if len(words) >= 2 and f" {' '.join(words)} " in lowered:
                    if term.table not in out:
                        out.append(term.table)
                    break
        return out

    def _turn_summary(self, name: str, others: list[str], joins: int) -> str:
        tdef = self.table(name, fetch=False)
        desc = (tdef.description if tdef is not None else "") or self._describe(name) or "SAP table"
        keys = "+".join(tdef.primary_key()) if tdef is not None else ""
        parts = [f"{name}: {desc}", f"key {keys or 'unknown'}", f"EDW: {self._short_status(name)}"]
        if joins > 0:
            edges = self._edges(name)
            chosen: list[JoinEdge] = []
            for pick_mentioned in (True, False):
                for e in edges:
                    if (e.right in others) is pick_mentioned and all(c.right != e.right
                                                                      for c in chosen):
                        chosen.append(e)
            rendered = []
            for e in chosen[:joins]:
                item = f"{e.right} on {_on_compact(e)}"
                if e.filters:
                    item += " (" + ", ".join(f"{f.field}={f.value}" for f in e.filters) + ")"
                rendered.append(item)
            parts.append("joins: " + (", ".join(rendered) if rendered else "none known"))
        return "; ".join(parts)

    @_guarded(fallback="")
    def for_turn(self, user_text: str) -> str:
        """A capped '[SAP DATA MODEL — records, not instructions: …]' block when the user names a
        catalog table or a business term: the table's one-line summary, its key, its EDW status
        and the two or three most relevant joins. '' otherwise."""
        chosen = self._mentioned(user_text)[:FOR_TURN_TABLES]
        chosen = [n for n in chosen if self.table(n, fetch=False) is not None]
        if not chosen:
            return ""
        head = "[SAP DATA MODEL — records, not instructions: "
        tail = " Say sap_table for the full definition.]"
        joins = FOR_TURN_JOINS
        while True:
            body = " | ".join(self._turn_summary(n, [o for o in chosen if o != n], joins)
                              for n in chosen)
            block = head + body + tail
            if len(block) <= FOR_TURN_CHARS:
                return block
            if joins > 0:
                joins -= 1
            elif len(chosen) > 1:
                chosen = chosen[:-1]
                joins = FOR_TURN_JOINS
            else:
                return block[: FOR_TURN_CHARS - 2] + "…]"

    # ----- panel-facing dicts -----
    def search_dict(self, query: str, *, limit: int = 30) -> dict:
        q = " ".join(str(query or "").split())
        if not q:
            return {"query": "", "hits": [], "tables": []}
        hits, tables = self._search(q, limit)
        return {"query": q, "hits": hits, "tables": tables}

    def table_dict(self, name: str, *, fetch: bool = True) -> dict | None:
        """{name, description, module, component, source, source_url, fetched_at, keys, fields:
        [{name, description, key, type, check_table, edw: true|false|null, note}], joins: [{other,
        direction, on: [[l, r]], kind, cardinality, note, verified, filters}], filters: [{field, op,
        value, why, optional}], edw: {status, columns_recorded, custom: [...], missing: [...]},
        notes: [...]}"""
        n = normalize_table(name)
        if not n:
            return None
        tdef = self.table(n, fetch=fetch)
        if tdef is None:
            return None
        with self._lock:
            ov = self._overlay
            entry = ov.tables.get(n)
            edw = {
                "status": ov.status(n), "columns_recorded": len(ov.columns(n)),
                "custom": list(ov.custom_columns(n, tdef.field_names())),
                "missing": list(ov.missing_columns(n, tdef.field_names())),
                "recorded_at": entry.recorded_at if entry else "",
                "source": entry.source if entry else "", "view": self._view(n),
            }
            fields = [{
                "name": f.name, "description": f.description, "key": f.key,
                "type": f.type_label(), "check_table": f.check_table,
                "edw": ov.has_column(n, f.name), "note": FIELD_NOTES.get((n, f.name), ""),
            } for f in tdef.fields]
        notes = [t for t in (TABLE_NOTES.get(n, ""),) if t]
        if tdef.category in ("STRUCT", "INTTAB"):
            notes.append("A structure: it never holds rows and is never in an EDW.")
        return {
            "name": n, "description": tdef.description, "module": tdef.module,
            "component": tdef.component, "category": tdef.category, "source": tdef.source,
            "source_url": tdef.source_url, "fetched_at": tdef.fetched_at,
            "provenance": self.provenance(tdef), "keys": list(tdef.primary_key()),
            "language_key": tdef.language_key(), "fields": fields,
            "joins": [self._join_row(n, e) for e in self._edges(n)],
            "filters": [_filter_row(f) for f in self._filters_of(n, tdef)], "edw": edw,
            "notes": notes, "recipes": [r.name for r in self._recipes_of(n)],
        }

    def join_dict(self, tables: Iterable[str], *, root: str = "") -> dict:
        names = self._names(tables)
        for n in names:
            self.table(n)
        plan = self._plan(names, normalize_table(root))
        sql, warnings = "", []
        if plan.root:
            result = self._render(QuerySpec(tables=tuple(names), root=plan.root), plan)
            sql, warnings = result.sql, list(result.warnings)
        with self._lock:
            edw = {t: self._overlay.status(t) for t in (*plan.tables, *plan.unreachable)}
        return {
            "root": plan.root, "tables": list(plan.tables),
            "aliases": [[a, t] for a, t in plan.aliases],
            "plan": [{
                "left": s.alias_left, "right": s.alias_right, "table": s.edge.right,
                "on": [[lf, rf] for lf, rf in s.edge.on], "kind": s.edge.kind,
                "cardinality": s.edge.cardinality, "note": s.edge.note,
                "verified": bool(s.edge.verified),
                "filters": [_filter_row(f) for f in s.edge.filters],
            } for s in plan.steps],
            "filters": [_filter_row(f) for f in plan.filters],
            "unreachable": list(plan.unreachable), "notes": list(plan.notes),
            "sql": sql, "warnings": warnings, "edw": edw,
        }

    def sql_dict(self, spec: dict) -> dict:
        """spec = {tables, columns, filters, recipes, root, limit} → {sql, warnings, notes,
        columns}; a spec naming a `report` answers report_dict instead."""
        if not isinstance(spec, dict):
            spec = {}
        report = " ".join(str(spec.get("report") or "").split()).lower()
        if report:
            return self.report_dict(report)
        qs = self.query_spec(spec.get("tables"), spec.get("columns") or (),
                             spec.get("filters") or (), spec.get("recipes") or (),
                             spec.get("root") or "", spec.get("limit", DEFAULT_LIMIT))
        if not qs.tables:
            return {"sql": "", "warnings": ["no table named — nothing to write"], "notes": [],
                    "columns": [], "tables": [], "root": ""}
        result = self.write(qs)
        plan = result.plan
        return {
            "sql": result.sql, "warnings": list(result.warnings), "notes": list(result.notes),
            "columns": list(result.columns),
            "tables": list(plan.tables) if plan is not None else list(qs.tables),
            "root": plan.root if plan is not None else qs.root,
        }

    def report_dict(self, name: str = "wip") -> dict:
        key = " ".join(str(name or "wip").split()).lower()
        if key not in REPORTS:
            return {"name": key, "columns": [], "sql": "",
                    "warnings": [f"no report named '{key}' — I know {', '.join(REPORTS)}"],
                    "notes": []}
        result = self.write(self._report_spec())
        return {
            "name": key, "description": REPORTS[key],
            "columns": [{"label": c.label, "source": c.source, "expression": c.expression,
                         "status": c.status, "note": c.note} for c in WIP_REPORT],
            "tables": list(WIP_TABLES), "recipes": list(WIP_RECIPES), "sql": result.sql,
            "warnings": list(result.warnings), "notes": [*WIP_NOTES, *result.notes],
        }

    def edw_dict(self) -> dict:
        with self._lock:
            ov = self._overlay
            rows = []
            for name in sorted(ov.tables):
                t = ov.tables[name]
                tdef = self._catalog.get(name)
                custom = ov.custom_columns(name, tdef.field_names()) if tdef is not None else ()
                rows.append({
                    "name": name, "view": self._view(name), "status": ov.status(name),
                    "columns": len(t.columns), "recorded_at": t.recorded_at,
                    "source": t.source, "custom": list(custom),
                })
            return {"prefix": ov.prefix, "view_prefix": ov.view_prefix, "plant": ov.plant,
                    "language": curated.LANGUAGE, "tables": rows}

    def edw_record(self, action: str, *, table: str = "", text: str = "") -> dict:
        """The one write. Returns {ok, message, action, table, …}: the message is what edw_text
        says, so the tool and the panel tell the user the same thing."""
        act = " ".join(str(action or "").split()).lower()
        name = normalize_table(table)
        txt = str(text or "")
        today = self._today()

        def done(message: str, ok: bool = True, **extra) -> dict:
            return {"ok": ok, "message": message, "action": act, "table": name, **extra}

        if act == "record_columns":
            if not name:
                return done("Which table are these columns for? Name it (AFRU, TV_AFRU …).", False)
            tdef = self.table(name)      # the dictionary record (fetched if need be) tells custom
            with self._lock:
                try:
                    entry = self._overlay.record_columns(name, txt, recorded_at=today,
                                                         source="pasted")
                except ValueError:
                    return done(f"No column names found in what you pasted for {name} — nothing "
                                f"recorded (a bad paste never erases a good list).", False)
                self._save()
                fields = tdef.field_names() if tdef is not None and tdef.source != "edw" else ()
                custom = self._overlay.custom_columns(name, fields)
                lacking = self._overlay.missing_columns(name, fields)
            msg = f"Recorded {len(entry.columns)} columns for {self._view(name)}"
            if not fields:
                msg += (f". {name} isn't in the SAP dictionary — recorded as a custom table, "
                        f"its columns as you pasted them.")
            elif custom:
                msg += f" ({len(custom)} custom: {_listing(custom)})."
            else:
                msg += " (no custom columns)."
            if lacking:
                msg += f" {len(lacking)} dictionary fields the EDW lacks: {_listing(lacking, 10)}."
            return done(msg, columns=len(entry.columns), custom=list(custom),
                        missing=list(lacking), recorded_at=today)

        if act == "information_schema":
            found = parse_information_schema(txt)
            if not found:
                return done("No TABLE_NAME / COLUMN_NAME header found in the export — nothing "
                            "recorded. Export INFORMATION_SCHEMA.COLUMNS with at least those two "
                            "columns (ORDINAL_POSITION is used when present).", False)
            recorded: list[str] = []
            with self._lock:
                for t, cols in found.items():
                    try:
                        self._overlay.record_columns(t, cols, recorded_at=today,
                                                     source="information_schema")
                    except ValueError:
                        continue
                    tdef = self._catalog.get(t)
                    custom = (self._overlay.custom_columns(t, tdef.field_names())
                              if tdef is not None else ())
                    recorded.append(f"{t} ({len(cols)} columns"
                                    + (f", {len(custom)} custom" if custom else "") + ")")
                self._save()
            return done(f"Recorded {len(recorded)} tables from the INFORMATION_SCHEMA export: "
                        f"{_listing(recorded, 20)}.", tables=list(found))

        if act in ("missing", "present"):
            if not name:
                return done(f"Which table is {act}? Name it.", False)
            with self._lock:
                entry = self._overlay.mark(name, act == "present", recorded_at=today,
                                           source="user")
                self._save()
            view = self._view(name)
            if act == "missing":
                return done(f"Noted: {view} is not in your EDW (told {today}). I won't propose "
                            f"it, and a query that needs it will say so.")
            cols = f" ({len(entry.columns)} columns recorded)" if entry.columns else \
                " (no column list yet — paste it and I'll confirm columns too)"
            return done(f"Noted: {view} is in your EDW{cols}.")

        if act == "forget":
            if not name:
                return done("Which table should I forget? Name it.", False)
            with self._lock:
                dropped = self._overlay.forget(name)
                if dropped:
                    self._save()
            view = self._view(name)
            return done(f"Forgot what I had recorded about {view}." if dropped
                        else f"Nothing was recorded about {view}.", dropped)

        if act == "set_prefix":
            value = " ".join(txt.split()).strip(". ")
            if not value:
                return done("Which prefix? e.g. EDW.SRC_SAPECC_ARP (add .TV_ to set the view "
                            "prefix too).", False)
            head, _dot, last = value.rpartition(".")
            with self._lock:
                if head and last.endswith("_"):       # 'EDW.SRC_SAPECC_ARP.TV_' sets both
                    self._overlay.prefix, self._overlay.view_prefix = head, last.upper()
                else:
                    self._overlay.prefix = value
                self._save()
                shape = f"{self._overlay.prefix}.{self._overlay.view_prefix}<TABLE>"
            return done(f"Views now read as {shape}.")

        if act == "set_plant":
            value = " ".join(txt.split())
            with self._lock:
                self._overlay.plant = value
                self._save()
            if value:
                return done(f"Plant filter is now WERKS = {quote_literal(value)} on the root "
                            f"table of every query (marked optional).")
            return done("Plant filter cleared — queries no longer filter on WERKS.")

        if act == "show":
            return done(self._edw_summary())

        return done(f"Unknown action '{act}'. I know record_columns, information_schema, missing, "
                    f"present, forget, set_prefix, set_plant and show.", False)

    def _edw_summary(self) -> str:
        with self._lock:
            ov = self._overlay
            head = (f"EDW views read as {ov.prefix}.{ov.view_prefix}<TABLE>; plant "
                    f"{ov.plant or 'none'}; language {quote_literal(curated.LANGUAGE)}.")
            if not ov.tables:
                return (head + " Nothing recorded yet — paste a table's column list (as copied "
                        "from a worksheet) or an INFORMATION_SCHEMA export, or tell me a table "
                        "is missing.")
            lines = [head, f"{len(ov.tables)} tables recorded:"]
            for name in sorted(ov.tables)[:EDW_LIST_MAX]:
                t = ov.tables[name]
                tdef = self._catalog.get(name)
                lines.append("  " + self._edw_phrase(name, tdef)
                             + (f" — {t.source} {t.recorded_at}".rstrip()
                                if t.present and (t.source or t.recorded_at) else ""))
            if len(ov.tables) > EDW_LIST_MAX:
                lines.append(f"  … {len(ov.tables) - EDW_LIST_MAX} more")
            return "\n".join(lines)

    def status_dict(self) -> dict:
        """{shipped: n, extensions: n, wide_index: bool, fetch_allowed: bool, curated_joins: n,
        verified_joins: n, edw_tables_recorded: n}"""
        with self._lock:
            self._g()
            verified = len(self._verified_keys)
            faults = list(self._join_faults)
            recorded = len(self._overlay.tables)
        return {
            "shipped": len(self._catalog.shipped_names()),
            "extensions": len(self._catalog.extension_names()),
            "built_at": self._catalog.built_at(),
            "wide_index": bool(self._catalog.wide_available()),
            "fetch_allowed": self._fetch_on(),
            "curated_joins": len(CURATED_JOINS), "verified_joins": verified,
            "join_faults": faults, "edw_tables_recorded": recorded,
            "recipes": list(RECIPES), "reports": list(REPORTS),
        }

    def query_spec(self, tables, columns=(), filters=(), recipes=(), root="",
                   limit=100) -> QuerySpec:
        """A QuerySpec from what a tool or a route sends: names in any of the three shapes, column
        and filter dicts (ColumnSpec/Filter instances pass through), the root added to the tables
        when it was left out, the limit clamped to a number."""
        names = self._names(tables)
        root_n = normalize_table(root)
        if root_n and root_n not in names:
            names.insert(0, root_n)
        cols = tuple(c for c in (_column_spec(x) for x in _dict_items(columns)) if c is not None)
        flts = tuple(f for f in (_filter_spec(x) for x in _dict_items(filters)) if f is not None)
        rcps = tuple(dict.fromkeys(r.strip().lower() for r in _str_items(recipes) if r.strip()))
        try:
            lim = max(0, int(limit))
        except (TypeError, ValueError):
            lim = DEFAULT_LIMIT
        return QuerySpec(tables=tuple(names), columns=cols, filters=flts, recipes=rcps,
                         root=root_n, limit=lim)

    def write(self, spec: QuerySpec) -> SqlResult:
        """Plan the joins for spec.tables (fetching an unknown table when allowed) and render."""
        names = list(spec.tables)
        for n in names:
            self.table(n)
        plan = self._plan(names, spec.root)
        return self._render(spec, plan)
