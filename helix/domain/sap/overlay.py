"""What the user's EDW actually has — the overlay on the dictionary. Pure.

Honeywell's EDW replicates SAP ECC into Snowflake as EDW.SRC_SAPECC_ARP.TV_<TABLE> views, but not
every table (no CRHTX, no DD03T) and with the site's own columns added (ZZOPERATION_TYPE …). There
is no live connection, so the overlay is what the user pastes in: a column list for a table
("MANDT RUECK RMZHL …" as they copied it from a worksheet), an INFORMATION_SCHEMA.COLUMNS export
(TABLE_NAME, COLUMN_NAME rows), or a plain "CRHTX doesn't exist". HELIX then never proposes a
table the EDW lacks and can say which columns of a query are confirmed.

Persisted as one JSON document (the service keeps it in data/helix_sap_edw.json):
  {"prefix": "EDW.SRC_SAPECC_ARP", "view_prefix": "TV_", "plant": "1006",
   "tables": {"AFRU": {"present": true, "columns": ["MANDT", …], "recorded_at": "2026-09-15",
                       "source": "pasted"},
              "CRHTX": {"present": false, "recorded_at": "2026-09-15", "source": "user"}}}

Three answers, never a fourth: a table is PRESENT (the user said so, or pasted its columns), MISSING
(the user said so), or UNKNOWN (nobody has said). A column is known to exist only when the table's
column list was recorded; a "present" mark without columns still answers None for every column,
because "the table exists" is not "the column exists" — the ZZ* fields are the proof of that.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field

from helix.domain.sap.model import normalize_field, normalize_table

PRESENT, MISSING, UNKNOWN = "present", "missing", "unknown"

DEFAULT_PREFIX = "EDW.SRC_SAPECC_ARP"
DEFAULT_VIEW_PREFIX = "TV_"

# What a column or table name may look like once upper-cased: SAP's own names (MANDT, RUECK_MST,
# ZZOPERATION_TYPE) and the namespaced ones ('/BIC/ZFOO'). Anything else in a paste — a number, a
# '(120 rows)' footer, a stray word with a dash — is not a column and is dropped.
IDENTIFIER = re.compile(r"/?[A-Z_][A-Z0-9_/]*")

# Splitting a paste into tokens: every separator a worksheet copy or a SELECT list can carry. '/'
# is deliberately NOT a separator (namespaces) and neither is '.': a qualified 'r.BUDAT' is
# handled after the split so the column survives and the alias does not.
_SEPARATORS = re.compile(r"[\s,;|()\[\]{}\"'`=<>+*]+")

# The select list of a query fragment: everything between SELECT and the next FROM (or the end).
# A CTE gives several fragments; all of them are read.
_SELECT_LIST = re.compile(r"\bSELECT\b(.*?)(?=\bFROM\b|$)", re.DOTALL)

# SQL words that sit in a SELECT list without being columns. None of them is an SAP field name.
# `AS` is handled apart: it also swallows the alias after it, so 'r.BUDAT AS LAST_WORKED' records
# BUDAT — the column the EDW has, not the name the query invented.
_SQL_WORDS = frozenset({
    "SELECT", "DISTINCT", "ALL", "TOP", "FROM", "CASE", "WHEN", "THEN", "ELSE", "END", "NULL",
    "OVER", "PARTITION", "BY", "ORDER", "GROUP", "ASC", "DESC", "AND", "OR", "NOT", "IN", "IS",
    "LIKE", "WITHIN", "CAST", "COALESCE", "NULLIF", "SUM", "COUNT", "MAX", "MIN", "AVG",
    "LISTAGG", "TO_DATE", "TO_VARCHAR", "TO_CHAR", "DATEDIFF", "CURRENT_DATE", "ROW_NUMBER",
})

_REQUIRED_HEADERS = ("TABLE_NAME", "COLUMN_NAME")
_POSITION_HEADER = "ORDINAL_POSITION"
_NO_POSITION = 10**9   # rows without a usable ORDINAL_POSITION sort after the ones that have one


@dataclass(frozen=True)
class EdwTable:
    name: str
    present: bool | None = None            # None = unknown
    columns: tuple[str, ...] = ()
    recorded_at: str = ""
    source: str = ""                        # pasted | information_schema | user | seed


@dataclass
class EdwOverlay:
    prefix: str = DEFAULT_PREFIX
    view_prefix: str = DEFAULT_VIEW_PREFIX
    plant: str = ""
    tables: dict[str, EdwTable] = field(default_factory=dict)

    def _get(self, table: str) -> EdwTable | None:
        return self.tables.get(normalize_table(table))

    # ----- questions the writer and the tools ask -----
    def status(self, table: str) -> str:
        """PRESENT / MISSING / UNKNOWN for a table name (TV_ prefix tolerated)."""
        t = self._get(table)
        if t is None or t.present is None:
            return UNKNOWN
        return PRESENT if t.present else MISSING

    def columns(self, table: str) -> tuple[str, ...]:
        """The recorded column list, () when none was recorded (or the table is unknown)."""
        t = self._get(table)
        return t.columns if t is not None else ()

    def has_column(self, table: str, column: str) -> bool | None:
        """True/False when the table's columns are recorded, None when unknown. A table marked
        MISSING answers False for every column: the EDW has no such column, whatever its name."""
        t = self._get(table)
        if t is None:
            return None
        if t.present is False:
            return False
        if not t.columns:
            return None
        return normalize_field(column) in t.columns

    def custom_columns(self, table: str, dictionary_fields: tuple[str, ...]) -> tuple[str, ...]:
        """Recorded columns the dictionary does not know (ZZ* and the like). () when no columns are
        recorded — and () when the dictionary has no fields for the table, because "every column
        is custom" would be the catalog's gap talking, not the site's."""
        cols = self.columns(table)
        known = {normalize_field(f) for f in dictionary_fields if normalize_field(f)}
        if not cols or not known:
            return ()
        return tuple(c for c in cols if c not in known)

    def missing_columns(self, table: str, dictionary_fields: tuple[str, ...]) -> tuple[str, ...]:
        """Dictionary fields the EDW did not replicate (recorded columns only)."""
        cols = self.columns(table)
        if not cols:
            return ()
        have = set(cols)
        out: list[str] = []
        for f in dictionary_fields:
            n = normalize_field(f)
            if n and n not in have and n not in out:
                out.append(n)
        return tuple(out)

    # ----- recording -----
    def record_columns(self, table: str, columns, *, recorded_at: str,
                       source: str = "pasted") -> EdwTable:
        """Record the table's real column list — a pasted string (parse_columns reads it) or an
        iterable of names. Recording columns marks the table present. An empty result raises
        ValueError rather than wiping a list already recorded: a bad paste must not erase a good
        one."""
        name = normalize_table(table)
        if not name:
            raise ValueError("a table name is needed to record columns")
        cols = parse_columns(columns) if isinstance(columns, str) else _clean_columns(columns)
        if not cols:
            raise ValueError(f"no column names found for {name}")
        entry = EdwTable(name=name, present=True, columns=cols,
                         recorded_at=_text(recorded_at), source=_text(source) or "pasted")
        self.tables[name] = entry
        return entry

    def mark(self, table: str, present: bool, *, recorded_at: str,
             source: str = "user") -> EdwTable:
        """'AFRU exists' / 'CRHTX does not'. Marking a table present keeps a column list already
        recorded (and the source it came from); marking it missing drops the list — a missing
        table has no columns, and a stale list would let has_column say True for a view that is
        not there."""
        name = normalize_table(table)
        if not name:
            raise ValueError("a table name is needed to mark it present or missing")
        old = self.tables.get(name)
        present = bool(present)
        keep = old.columns if (old is not None and present) else ()
        entry = EdwTable(name=name, present=present, columns=keep, recorded_at=_text(recorded_at),
                         source=(old.source if keep and old is not None and old.source
                                 else _text(source) or "user"))
        self.tables[name] = entry
        return entry

    def forget(self, table: str) -> bool:
        """Drop everything recorded about a table; True when there was something to drop."""
        return self.tables.pop(normalize_table(table), None) is not None

    # ----- (de)serialization -----
    def to_dict(self) -> dict:
        """The persisted document (module docstring). Tables are written sorted so the file on
        disk diffs cleanly; 'columns' is written only when a list was recorded."""
        tables: dict[str, dict] = {}
        for name in sorted(self.tables):
            t = self.tables[name]
            row: dict = {"present": t.present}
            if t.columns:
                row["columns"] = list(t.columns)
            row["recorded_at"] = t.recorded_at
            row["source"] = t.source
            tables[name] = row
        return {"prefix": self.prefix, "view_prefix": self.view_prefix, "plant": self.plant,
                "tables": tables}

    @classmethod
    def from_dict(cls, d: dict) -> EdwOverlay:
        """Tolerant: a corrupt or foreign document reads as an empty overlay with defaults, and a
        junk table entry is skipped rather than poisoning the rest. A plain list of names or a bare
        bool as a table's value is accepted (hand-edited files happen)."""
        out = cls()
        if not isinstance(d, dict):
            return out
        prefix = _text(d.get("prefix"))
        if prefix:
            out.prefix = prefix
        view_prefix = d.get("view_prefix")
        if isinstance(view_prefix, str):      # '' is a real answer: views with no prefix at all
            out.view_prefix = view_prefix.strip().upper()
        out.plant = _text(d.get("plant"))
        raw = d.get("tables")
        if not isinstance(raw, dict):
            return out
        for key, value in raw.items():
            name = normalize_table(key)
            if not name:
                continue
            entry = _table_from(name, value)
            if entry is not None:
                out.tables[name] = entry
        return out


def _table_from(name: str, value) -> EdwTable | None:
    if isinstance(value, bool):
        return EdwTable(name=name, present=value)
    if isinstance(value, (list, tuple)):
        cols = _clean_columns(value)
        return EdwTable(name=name, present=True, columns=cols) if cols else None
    if not isinstance(value, dict):
        return None
    present = _present(value.get("present"))
    raw_cols = value.get("columns")
    if isinstance(raw_cols, str):             # a hand-edited file: the paste record_columns takes
        cols = parse_columns(raw_cols)
    else:
        cols = _clean_columns(raw_cols) if isinstance(raw_cols, (list, tuple)) else ()
    if cols and present is None:
        present = True        # a recorded column list is proof the table is there
    if present is False:
        cols = ()             # the flag wins, as mark() does: a missing table has no columns
    return EdwTable(name=name, present=present, columns=cols,
                    recorded_at=_text(value.get("recorded_at")), source=_text(value.get("source")))


def _present(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return {"true": True, "yes": True, "1": True, "x": True,
                "false": False, "no": False, "0": False}.get(v.strip().lower())
    return None


def _text(v) -> str:
    return "" if v is None else " ".join(str(v).split())


def _clean_columns(names) -> tuple[str, ...]:
    """Column names from an iterable: upper-cased, identifiers only, de-duplicated, order kept."""
    out: list[str] = []
    try:
        items = list(names or ())
    except TypeError:
        return ()
    for raw in items:
        n = normalize_field(raw)
        if n and IDENTIFIER.fullmatch(n) and n not in out:
            out.append(n)
    return tuple(out)


def _column_token(raw: str) -> str:
    """'r.BUDAT' → 'BUDAT', 'RUECK.' → 'RUECK'; '' when the token is not an identifier."""
    parts = [p for p in raw.split(".") if p]
    if not parts:
        return ""
    name = parts[-1]
    return name if IDENTIFIER.fullmatch(name) else ""


def parse_columns(text: str) -> tuple[str, ...]:
    """Column names out of whatever the user pasted: space/comma/newline/tab separated names, a
    'SELECT a, b, c' fragment (the identifiers between SELECT and FROM; 'r.BUDAT AS X' reads as
    BUDAT), or a one-column-per-line worksheet copy. Upper-cased, de-duplicated, order kept;
    anything that is not a plausible SAP/Snowflake identifier is dropped. A 'TV_' prefix on a
    COLUMN is kept as-is — only table names get the view prefix stripped."""
    up = str(text or "").upper()
    if "SELECT" in up:
        fragments = [m.group(1) for m in _SELECT_LIST.finditer(up)]
        if fragments:
            up = " ".join(fragments)
    out: list[str] = []
    seen: set[str] = set()
    skip_alias = False
    for raw in _SEPARATORS.split(up):
        if not raw:
            continue
        if skip_alias:
            skip_alias = False
            continue
        if raw == "AS":
            skip_alias = True
            continue
        if raw in _SQL_WORDS:
            continue
        name = _column_token(raw)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def _delimiter(line: str) -> str:
    """Tab beats comma beats semicolon beats pipe — judged on the header line, where a stray
    comma inside a description cell cannot mislead."""
    for d in ("\t", ",", ";", "|"):
        if d in line:
            return d
    return ","


def _cells(line: str, delimiter: str) -> list[str]:
    return next(csv.reader([line], delimiter=delimiter, skipinitialspace=True), [])


def _cell(raw: str) -> str:
    return raw.strip().strip("'\"`").strip().upper()


def _position(raw: str) -> int | None:
    try:
        return int(float(raw.strip().strip("'\"`")))
    except (ValueError, TypeError, AttributeError):
        return None


def parse_information_schema(text: str) -> dict[str, tuple[str, ...]]:
    """An INFORMATION_SCHEMA.COLUMNS export (CSV or tab-separated, header row with TABLE_NAME and
    COLUMN_NAME in any column order and any case; ORDINAL_POSITION honoured when present) →
    {table: columns} with the TV_ prefix stripped from table names. Quotes, CRLF, a title line
    above the header, blank lines and a '(n rows)' footer are all tolerated; a document without
    the two required headers is {} — never a guess at which column is which."""
    lines = str(text or "").splitlines()
    header: dict[str, int] = {}
    delimiter = ","
    start = 0
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        delimiter = _delimiter(line)
        idx = {_cell(c): k for k, c in enumerate(_cells(line, delimiter))}
        if all(h in idx for h in _REQUIRED_HEADERS):
            header, start = idx, i + 1
            break
    if not header:
        return {}
    t_at, c_at = header["TABLE_NAME"], header["COLUMN_NAME"]
    p_at = header.get(_POSITION_HEADER)
    found: dict[str, list[tuple[int, int, str]]] = {}
    rows = csv.reader(lines[start:], delimiter=delimiter, skipinitialspace=True)
    for n, row in enumerate(rows):
        if len(row) <= max(t_at, c_at):
            continue
        table = normalize_table(_cell(row[t_at]))
        column = normalize_field(_cell(row[c_at]))
        if not table or not IDENTIFIER.fullmatch(table) or not IDENTIFIER.fullmatch(column):
            continue
        pos = _position(row[p_at]) if p_at is not None and p_at < len(row) else None
        found.setdefault(table, []).append((pos if pos is not None else _NO_POSITION, n, column))
    out: dict[str, tuple[str, ...]] = {}
    for table, entries in found.items():
        cols: list[str] = []
        for _pos, _n, column in sorted(entries):
            if column not in cols:
                cols.append(column)
        out[table] = tuple(cols)
    return out
