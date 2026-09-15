"""The SAP data-dictionary records HELIX reasons from — pure data, no I/O.

WHY: HELIX guessed SAP field names from memory (PLPO.ARBPL, CRHD.KTEXT, AFRU.BUZEIT — none exist)
and the user validated each guess against the real tables. A TableDef is the dictionary's own
answer: every field with SAP's English description, its key flag, data element, type and check
table, plus the declared foreign keys with their FULL compound columns (AFRU→AFVC is
MANDT+AUFPL+APLZL, never AUFPL alone). Everything here carries its source and date, so an answer can
say where it came from.

Two kinds of relationship live on top of the dictionary:
  FkRef   — a foreign key SAP declared (a check-table validation); the raw material for joins.
  JoinEdge — a join HELIX will actually write: an FkRef turned into (left_field, right_field) pairs,
             or a curated edge for the joins the dictionary never declares (AUFK.OBJNR = JEST.OBJNR,
             AFRU.ARBID = CRHD.OBJID) — with the filters the join needs to be correct.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

MODEL_VERSION = 1

CLIENT_FIELD = "MANDT"
LANGUAGE_TYPE = "LANG"
DATE_TYPE = "DATS"
TIME_TYPE = "TIMS"
# The dictionary's own date/time "empty" values — a DATS is never NULL in SAP, it is '00000000'.
EMPTY_DATE = "00000000"
EMPTY_TIME = "000000"

# Top-level module labels the catalog groups tables under (the user's browsing vocabulary).
MODULES: tuple[str, ...] = (
    "PP", "CO", "PS", "MM", "SD", "QM", "PM", "FI", "LO", "WM", "LE", "HR", "CA", "BC",
)
MODULE_NAMES: dict[str, str] = {
    "PP": "Production Planning", "CO": "Controlling", "PS": "Project System",
    "MM": "Materials Management", "SD": "Sales & Distribution", "QM": "Quality Management",
    "PM": "Plant Maintenance", "FI": "Financial Accounting", "LO": "Logistics (general)",
    "WM": "Warehouse Management", "LE": "Logistics Execution", "HR": "Human Resources",
    "CA": "Cross-Application", "BC": "Basis",
}


def normalize_table(name: str) -> str:
    """Table names are upper-case, no whitespace; a 'TV_' EDW view prefix is stripped so the user
    can say either 'AFRU' or 'TV_AFRU'."""
    n = " ".join(str(name or "").split()).upper()
    if n.startswith("TV_") and len(n) > 3:
        n = n[3:]
    return n


def normalize_field(name: str) -> str:
    return " ".join(str(name or "").split()).upper()


@dataclass(frozen=True)
class FieldDef:
    """One column of a table, as the dictionary describes it."""

    name: str
    description: str = ""
    key: bool = False
    data_element: str = ""
    domain: str = ""
    check_table: str = ""
    data_type: str = ""      # CHAR NUMC DATS TIMS QUAN CURR CLNT LANG UNIT CUKY DEC INT4 RAW …
    length: int = 0
    decimals: int = 0
    position: int = 0
    # Fixed values the source shows for the field, as (code, meaning) — e.g. STOKZ:
    # ("X", "Flag set").
    values: tuple[tuple[str, str], ...] = ()

    @property
    def is_date(self) -> bool:
        return self.data_type == DATE_TYPE

    @property
    def is_time(self) -> bool:
        return self.data_type == TIME_TYPE

    @property
    def is_language(self) -> bool:
        return self.data_type == LANGUAGE_TYPE

    @property
    def is_client(self) -> bool:
        return self.name == CLIENT_FIELD

    def type_label(self) -> str:
        """'CHAR(12)', 'QUAN(13,3)', 'DATS(8)' — how a field's type reads in a listing."""
        if not self.data_type:
            return ""
        if self.decimals:
            return f"{self.data_type}({self.length},{self.decimals})"
        return f"{self.data_type}({self.length})" if self.length else self.data_type


@dataclass(frozen=True)
class FkRef:
    """A foreign key SAP declared on `field`, pointing at `check_table`. `columns` are the FULL
    key mapping, one (host_expr, check_field) pair per key column of the check table. `host_expr`
    is normally a field of this table ('AUFPL'); when the dictionary takes the column from another
    structure or a system field it is qualified ('AFVC.BEDID', 'SYST.MANDT') and the reference is
    `partial` — usable as a hint, not as a join on its own."""

    field: str
    check_table: str
    columns: tuple[tuple[str, str], ...]
    partial: bool = False
    kind: str = ""          # KEY | REF | TEXT | "" (the dictionary's dependency factor, when known)
    cardinality: str = ""   # e.g. "1:CN" (left:right, dictionary notation), when known

    @property
    def join_columns(self) -> tuple[tuple[str, str], ...]:
        """Only the pairs whose host side is a plain field of this table — what a join can use."""
        return tuple((h, c) for h, c in self.columns if "." not in h)

    @property
    def joinable(self) -> bool:
        return bool(self.join_columns) and len(self.join_columns) == len(self.columns)


@dataclass(frozen=True)
class TableDef:
    """One SAP table as the dictionary describes it, with where and when that description was
    read."""

    name: str
    description: str = ""
    module: str = ""          # one of MODULES, or "" when unknown
    # application component or package text, e.g. "PP-SFC-EXE-CON" / "Confirmations"
    component: str = ""
    category: str = ""        # TRANSP POOL CLUSTER STRUCT VIEW or ""
    delivery_class: str = ""  # A C L G E S W or ""
    fields: tuple[FieldDef, ...] = ()
    foreign_keys: tuple[FkRef, ...] = ()
    source: str = ""          # leanx | alexhatzen | svn11x | edw | curated
    source_url: str = ""
    fetched_at: str = ""      # ISO date "YYYY-MM-DD"

    # ----- lookups -----
    def field(self, name: str) -> FieldDef | None:
        n = normalize_field(name)
        for f in self.fields:
            if f.name == n:
                return f
        return None

    def has_field(self, name: str) -> bool:
        return self.field(name) is not None

    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def primary_key(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.key)

    def key_without_client(self) -> tuple[str, ...]:
        return tuple(k for k in self.primary_key() if k != CLIENT_FIELD)

    def language_key(self) -> str:
        """The LANG-typed key field (SPRAS on MAKT, CRTX, TJ02T …) — the mark of a text table."""
        for f in self.fields:
            if f.key and f.is_language:
                return f.name
        return ""

    @property
    def is_text_table(self) -> bool:
        return bool(self.language_key())

    @property
    def is_structure(self) -> bool:
        """A structure has no key fields — it never holds rows and is never in an EDW."""
        if self.category in ("STRUCT", "INTTAB"):
            return True
        return not self.primary_key() and not self.foreign_keys

    def fks_to(self, table: str) -> tuple[FkRef, ...]:
        t = normalize_table(table)
        return tuple(fk for fk in self.foreign_keys if fk.check_table == t)

    def check_tables(self) -> tuple[str, ...]:
        seen: list[str] = []
        for fk in self.foreign_keys:
            if fk.check_table and fk.check_table not in seen:
                seen.append(fk.check_table)
        return tuple(seen)

    # ----- (de)serialization: one JSON document per table -----
    def to_dict(self) -> dict:
        return {
            "v": MODEL_VERSION,
            "name": self.name, "description": self.description, "module": self.module,
            "component": self.component, "category": self.category,
            "delivery_class": self.delivery_class,
            "source": self.source, "source_url": self.source_url, "fetched_at": self.fetched_at,
            "fields": [
                {
                    "name": f.name, "description": f.description, "key": f.key,
                    "data_element": f.data_element, "domain": f.domain,
                    "check_table": f.check_table, "data_type": f.data_type,
                    "length": f.length, "decimals": f.decimals, "position": f.position,
                    "values": [list(v) for v in f.values],
                }
                for f in self.fields
            ],
            "foreign_keys": [
                {
                    "field": fk.field, "check_table": fk.check_table,
                    "columns": [list(c) for c in fk.columns], "partial": fk.partial,
                    "kind": fk.kind, "cardinality": fk.cardinality,
                }
                for fk in self.foreign_keys
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> TableDef:
        fields = tuple(
            FieldDef(
                name=normalize_field(f.get("name", "")),
                description=str(f.get("description") or ""),
                key=bool(f.get("key")), data_element=str(f.get("data_element") or ""),
                domain=str(f.get("domain") or ""),
                check_table=normalize_table(f.get("check_table") or ""),
                data_type=str(f.get("data_type") or "").upper(),
                length=int(f.get("length") or 0), decimals=int(f.get("decimals") or 0),
                position=int(f.get("position") or 0),
                values=tuple((str(a), str(b)) for a, b in (f.get("values") or []) if a is not None),
            )
            for f in (d.get("fields") or [])
        )
        fks = tuple(
            FkRef(
                field=normalize_field(fk.get("field", "")),
                check_table=normalize_table(fk.get("check_table") or ""),
                columns=tuple((str(h), str(c)) for h, c in (fk.get("columns") or [])),
                partial=bool(fk.get("partial")), kind=str(fk.get("kind") or ""),
                cardinality=str(fk.get("cardinality") or ""),
            )
            for fk in (d.get("foreign_keys") or [])
        )
        return cls(
            name=normalize_table(d.get("name", "")), description=str(d.get("description") or ""),
            module=str(d.get("module") or ""), component=str(d.get("component") or ""),
            category=str(d.get("category") or ""),
            delivery_class=str(d.get("delivery_class") or ""),
            fields=fields, foreign_keys=fks, source=str(d.get("source") or ""),
            source_url=str(d.get("source_url") or ""), fetched_at=str(d.get("fetched_at") or ""),
        )

    def with_module(self, module: str, component: str = "") -> TableDef:
        return replace(self, module=module or self.module, component=component or self.component)


@dataclass(frozen=True)
class Filter:
    """A WHERE condition a table needs for a query to be right — `JEST.INACT = ''` (active statuses
    only), `TJ02T.SPRAS = 'E'` (one language), `AUFK.AUTYP = '10'` (production orders). `why` is the
    sentence HELIX gives when it adds it."""

    table: str
    field: str
    op: str = "="       # = | <> | IN | LIKE | IS NULL | IS NOT NULL
    # a plain literal (quoted by the SQL writer) or, for IN, comma-separated literals
    value: str = ""
    why: str = ""
    # a sensible default the user may drop (a plant filter), not a correctness rule
    optional: bool = False


@dataclass(frozen=True)
class JoinEdge:
    """A join HELIX writes between two tables: `on` is the full list of (left_field, right_field)
    pairs — compound keys stay compound. `filters` are conditions the RIGHT side needs for the join
    to mean what it says (OBJTY='A' on CRHD, INACT='' on JEST, SPRAS='E' on a text table)."""

    left: str
    right: str
    on: tuple[tuple[str, str], ...]
    kind: str = "fk"            # fk | curated | text
    cardinality: str = ""       # "1:1" | "1:n" | "n:1" | "n:m" | "" — left : right
    filters: tuple[Filter, ...] = ()
    note: str = ""
    source: str = ""            # where the edge came from (a URL, "dictionary", "curated")
    verified: bool = False      # a verifier confirmed both sides' fields exist in the dictionary

    def reversed(self) -> JoinEdge:
        card = {"1:n": "n:1", "n:1": "1:n"}.get(self.cardinality, self.cardinality)
        return replace(self, left=self.right, right=self.left,
                       on=tuple((rf, lf) for lf, rf in self.on), cardinality=card)

    @property
    def key(self) -> tuple:
        """Identity for de-duplication: the unordered pair plus the sorted column pairs."""
        a, b = sorted(((self.left, self.on),
                       (self.right, tuple((rf, lf) for lf, rf in self.on))))
        return (a[0], b[0], tuple(sorted(a[1])))

    def touches(self, table: str) -> bool:
        return table in (self.left, self.right)

    def other(self, table: str) -> str:
        return self.right if table == self.left else self.left

    def as_conditions(self, left_alias: str, right_alias: str) -> tuple[str, ...]:
        """'L.AUFPL = R.AUFPL', … — the join's ON clause pieces for the given aliases."""
        return tuple(f"{left_alias}.{lf} = {right_alias}.{rf}" for lf, rf in self.on)


def edges_from_fks(table: TableDef) -> tuple[JoinEdge, ...]:
    """Every joinable declared foreign key of `table` as a JoinEdge (this table on the left). A key
    into a text table (the check table's key ends in a LANG field the host lacks) is left to the
    text-table logic elsewhere; here only full, plain-field mappings become edges."""
    out: list[JoinEdge] = []
    for fk in table.foreign_keys:
        if not fk.joinable or not fk.check_table or fk.check_table == table.name:
            continue
        card = ""
        if fk.kind == "KEY":
            card = "n:1"
        kind = f" ({fk.kind})" if fk.kind else ""
        out.append(JoinEdge(
            left=table.name, right=fk.check_table, on=fk.join_columns, kind="fk",
            cardinality=card, source="dictionary",
            note=f"declared foreign key on {table.name}.{fk.field}{kind}",
        ))
    return tuple(out)


@dataclass(frozen=True)
class JoinStep:
    """One hop of a join plan: `edge` joins `alias_right` (a fresh table) onto `alias_left`."""

    edge: JoinEdge
    alias_left: str
    alias_right: str


@dataclass(frozen=True)
class JoinPlan:
    """How to reach every requested table from `root`: the ordered hops, the aliases, the filters
    each hop needs, and any table no path reached (with a sentence saying so)."""

    root: str
    steps: tuple[JoinStep, ...] = ()
    aliases: tuple[tuple[str, str], ...] = ()   # (alias, table) in FROM/JOIN order
    filters: tuple[Filter, ...] = ()
    unreachable: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def alias_of(self, table: str) -> str:
        for a, t in self.aliases:
            if t == table:
                return a
        return table

    @property
    def tables(self) -> tuple[str, ...]:
        return tuple(t for _a, t in self.aliases)
