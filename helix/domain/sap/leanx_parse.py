"""leanx.eu table page → TableDef. Pure: takes HTML text, returns a record. No network here.

leanx.eu (Warren IT Services) republishes the SAP ECC data dictionary one table per page at
https://leanx.eu/sap/table/<name>/ — every field with its English description, key flag, data
element, check table, type/length/decimals and fixed values, and the declared foreign keys with
their FULL compound columns. It is the catalog's source of truth for field-level facts.

Page shape (Tailwind markup, server-rendered, stable as of 2026-09):
  <h1 class="text-3xl …">SAP Table AFRU</h1>
  <h2 class="text-xl …">Order Confirmations</h2>
  <h3>AFRU table fields</h3> then <table class="w-full"> with <thead> Field | Data element |
  Checktable | Datatype | Length | Decimals | Possible values, and <tbody> rows:
    <tr class="bg-blue-50">            ← a KEY field (non-key rows: <tr class="hover:bg-gray-50">)
      <td><div class="font-medium text-gray-900">MANDT</div>
          <div class="text-sm text-gray-500">Client</div></td>       ← name, description
      <td>MANDT</td>                                                  ← data element
      <td><a href="/sap/table/t000/">T000</a></td>                    ← check table (may be empty)
      <td><div>CLNT</div><div></div></td>                             ← datatype (second div: empty)
      <td>3</td><td>0</td>                                            ← length, decimals
      <td><button onclick="toggleCollapse('collapse-N')">…</button></td>
    <tr id="collapse-N" class="hidden"><td colspan="7"><table> Value | Description rows </table>
  <h3>AFRU foreign key\n relationships</h3> (the heading wraps) then <table class="w-full"> with
  <thead> Table | Field | Foreign key table | Foreign key field | Check table | Check field and one
  <tbody> row per key COLUMN of each relationship:
    AFRU | APLZL | AFRU | MANDT | AFVC | MANDT
    AFRU | APLZL | AFRU | AUFPL | AFVC | AUFPL
    AFRU | APLZL | AFRU | APLZL | AFVC | APLZL
  Group rows by (Field, Check table) to rebuild the compound key. 'Foreign key table' is usually
  the page's own table; when it is another table or SYST the column comes from elsewhere and the
  FkRef is `partial` with the host expression qualified ('AFVC.BEDID', 'SYST.MANDT'). 'Foreign key
  field' can be '*' (generic) — keep the pair but mark the reference partial. The page renders
  the generic case with '*' in the 'Foreign key table' column and an EMPTY field; a constant
  check (SAP's "'0001'" foreign-key field) shows the quoted literal there — kept verbatim as the
  host expression, partial too.
  A structure has no FK section at all. Nested tables (possible values) sit INSIDE the fields
  table's rows: parse with html.parser tracking table depth, never by cutting at the first </table>.
  An unknown table is an HTTP 404 whose body is the search page: `is_table_page(html)` is False.

Sections are recognised by their header rows (Field | Data element …, Table | Field | Foreign key
table …, Value | Description), not by the h3 text: every page's intro paragraph also says
"foreign key relationships, if any", so a phrase search would find an FK section on a structure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser

from helix.domain.sap.model import FieldDef, FkRef, TableDef, normalize_field, normalize_table

TABLE_URL = "https://leanx.eu/sap/table/{name}/"
SOURCE = "leanx"

_H1_NAME = re.compile(r"^SAP Table (\S+)$")
_KEY_ROW_CLASS = "bg-blue-50"
_VALUES_ROW_ID = "collapse-"
_NULL_SENTINEL = "NULL"
_GENERIC = "*"


def table_url(name: str) -> str:
    """The canonical page URL for a table (lower-case name; namespace slashes kept)."""
    return TABLE_URL.format(name=normalize_table(name).lower())


def _squash(text: str) -> str:
    return " ".join(text.split())


def _int(text: str) -> int:
    try:
        return int(_squash(text))
    except ValueError:
        return 0


# ----- the skeleton the handler builds: tables → rows → cells, nested where the page nests -----

@dataclass
class _Cell:
    header: bool = False                       # a <th>
    text: list[str] = field(default_factory=list)      # every text node in the cell
    own: list[str] = field(default_factory=list)       # text outside any <div>/<a>/<span>
    divs: list[str] = field(default_factory=list)      # each top-level <div>'s text, in order
    links: list[str] = field(default_factory=list)     # each <a>'s text, in order
    italics: list[str] = field(default_factory=list)   # each italic <span> (the NULL sentinel)
    tables: list[_Table] = field(default_factory=list)  # tables nested inside the cell
    # parse-time state
    div_depth: int = 0
    div_buf: list[str] = field(default_factory=list)
    a_buf: list[str] | None = None
    span_buf: list[str] | None = None

    @property
    def all_text(self) -> str:
        return _squash("".join(self.text))

    @property
    def own_text(self) -> str:
        return _squash("".join(self.own))


@dataclass
class _Row:
    classes: tuple[str, ...] = ()
    id: str = ""
    cells: list[_Cell] = field(default_factory=list)

    @property
    def is_header(self) -> bool:
        return bool(self.cells) and all(c.header for c in self.cells)

    @property
    def is_key(self) -> bool:
        return _KEY_ROW_CLASS in self.classes

    @property
    def is_values(self) -> bool:
        return self.id.startswith(_VALUES_ROW_ID) or "hidden" in self.classes


@dataclass
class _Table:
    rows: list[_Row] = field(default_factory=list)
    row: _Row | None = None      # the row being parsed
    cell: _Cell | None = None    # the cell being parsed

    def header(self) -> tuple[str, ...]:
        for r in self.rows:
            if r.is_header:
                return tuple(c.all_text.lower() for c in r.cells)
        return ()

    def body(self) -> list[_Row]:
        return [r for r in self.rows if not r.is_header and r.cells]


class _PageHandler(HTMLParser):
    """Walks the page once. Tables are kept on a stack so a possible-values table nested inside a
    fields-table cell lands in that cell (`_Cell.tables`) and never disturbs the outer row — the
    reason the parser is depth-aware rather than a split on '</table>'."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)   # entities arrive unescaped in handle_data
        self.h1 = ""
        self.h2 = ""
        self.tables: list[_Table] = []            # top-level tables in page order
        self._stack: list[_Table] = []
        self._heading: str | None = None          # "h1" | "h2" while inside one
        self._heading_buf: list[str] = []

    # --- helpers ---
    @property
    def _top(self) -> _Table | None:
        return self._stack[-1] if self._stack else None

    @property
    def _cell(self) -> _Cell | None:
        top = self._top
        return top.cell if top else None

    def _close_cell(self) -> None:
        top = self._top
        if top and top.cell is not None:
            top.cell = None

    def _close_row(self) -> None:
        top = self._top
        if top is None:
            return
        self._close_cell()
        if top.row is not None:
            top.rows.append(top.row)
            top.row = None

    # --- tags ---
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "table":
            self._stack.append(_Table())
            return
        if tag in ("h1", "h2") and not self._stack:
            if (tag == "h1" and not self.h1) or (tag == "h2" and self.h1 and not self.h2):
                self._heading, self._heading_buf = tag, []
            return
        top = self._top
        if top is None:
            return
        if tag == "tr":
            self._close_row()                       # tolerate a missing </tr>
            top.row = _Row(classes=tuple(a.get("class", "").split()), id=a.get("id", ""))
        elif tag in ("td", "th"):
            if top.row is None:
                top.row = _Row()
            self._close_cell()                      # tolerate a missing </td>
            top.cell = _Cell(header=tag == "th")
            top.row.cells.append(top.cell)
        elif top.cell is not None:
            cell = top.cell
            if tag == "div":
                if cell.div_depth == 0:
                    cell.div_buf = []
                cell.div_depth += 1
            elif tag == "a":
                cell.a_buf = []
            elif tag == "span" and "italic" in a.get("class", "").split():
                cell.span_buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if not self._stack:
                return
            self._close_row()
            done = self._stack.pop()
            parent_cell = self._cell
            if parent_cell is not None:
                parent_cell.tables.append(done)
            else:
                self.tables.append(done)
            return
        if tag in ("h1", "h2") and self._heading == tag:
            text = _squash("".join(self._heading_buf))
            if tag == "h1":
                self.h1 = text
            else:
                self.h2 = text
            self._heading = None
            return
        top = self._top
        if top is None:
            return
        if tag == "tr":
            self._close_row()
        elif tag in ("td", "th"):
            self._close_cell()
        elif top.cell is not None:
            cell = top.cell
            if tag == "div" and cell.div_depth > 0:
                cell.div_depth -= 1
                if cell.div_depth == 0:
                    cell.divs.append(_squash("".join(cell.div_buf)))
                    cell.div_buf = []
            elif tag == "a" and cell.a_buf is not None:
                cell.links.append(_squash("".join(cell.a_buf)))
                cell.a_buf = None
            elif tag == "span" and cell.span_buf is not None:
                cell.italics.append(_squash("".join(cell.span_buf)))
                cell.span_buf = None

    def handle_data(self, data: str) -> None:
        if self._heading is not None:
            self._heading_buf.append(data)
            return
        cell = self._cell
        if cell is None:
            return
        cell.text.append(data)
        if cell.div_depth > 0:
            cell.div_buf.append(data)
        if cell.a_buf is not None:
            cell.a_buf.append(data)
        if cell.span_buf is not None:
            cell.span_buf.append(data)
        if cell.div_depth == 0 and cell.a_buf is None and cell.span_buf is None:
            cell.own.append(data)


# ----- from the skeleton to the record -----

@dataclass(frozen=True)
class _Page:
    name: str
    description: str
    fields_table: _Table | None
    fk_table: _Table | None


def _scan(html: str) -> _Page:
    h = _PageHandler()
    h.feed(html or "")
    h.close()
    m = _H1_NAME.match(h.h1)
    name = normalize_table(m.group(1)) if m else ""
    fields_table = fk_table = None
    for t in h.tables:
        head = t.header()
        if fields_table is None and head[:2] == ("field", "data element") and len(head) >= 6:
            fields_table = t
        elif fk_table is None and "foreign key table" in head:
            fk_table = t
    return _Page(name=name, description=h.h2, fields_table=fields_table, fk_table=fk_table)


def is_table_page(html: str) -> bool:
    """True when the HTML is a table page (has the 'SAP Table <NAME>' h1 and a fields table),
    False for the search page leanx serves on a 404 or any other document. The 404 page's h1 is
    'SAP Table Not Found' — two words, so the name pattern already rejects it — and it has no
    table at all."""
    page = _scan(html)
    return bool(page.name) and page.fields_table is not None


def _values(row: _Row) -> tuple[tuple[str, str], ...]:
    """The (Value, Description) pairs of a collapse row's nested table. The value cell renders a
    blank code as an italic 'NULL' span; SAP's blank is '' (a CHAR(1) flag not set), so that is
    what the record carries."""
    out: list[tuple[str, str]] = []
    for cell in row.cells:
        for t in cell.tables:
            if t.header() and t.header()[0] != "value":
                continue
            for r in t.body():
                if len(r.cells) < 2:
                    continue
                v, d = r.cells[0], r.cells[1]
                value = "" if _NULL_SENTINEL in v.italics else v.all_text
                out.append((value, d.all_text))
    return tuple(out)


def _fields(table: _Table) -> tuple[FieldDef, ...]:
    out: list[FieldDef] = []
    for row in table.body():
        if row.is_values:
            if out:
                out[-1] = replace(out[-1], values=_values(row))
            continue
        if len(row.cells) < 6:
            continue
        c = row.cells
        divs = c[0].divs
        name = normalize_field(divs[0] if divs else c[0].all_text)
        if not name:
            continue
        type_divs = c[3].divs
        out.append(FieldDef(
            name=name,
            description=divs[1] if len(divs) > 1 else "",
            key=row.is_key,
            data_element=c[1].all_text,
            check_table=normalize_table(c[2].links[0]) if c[2].links else "",
            data_type=(type_divs[0] if type_divs else c[3].all_text).upper(),
            length=_int(c[4].all_text),
            decimals=_int(c[5].all_text),
            position=len(out) + 1,
        ))
    return tuple(out)


def _host(self_name: str, fk_table: str, fk_field: str) -> tuple[str, bool]:
    """(host_expr, partial) for one FK row. A plain field of this table is joinable; anything
    else — a column of another structure, a system field, the generic '*', a constant — is kept
    as the page shows it but flagged so no join is written from it alone."""
    if _GENERIC in (fk_table, fk_field):
        return _GENERIC, True
    table = fk_table.upper()
    if not table or table == self_name:
        return fk_field, not fk_field
    if not fk_field:
        return fk_table, True            # a quoted constant: the literal sits in the table column
    return f"{table}.{fk_field}", True


def _foreign_keys(self_name: str, table: _Table) -> tuple[FkRef, ...]:
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    partial: dict[tuple[str, str], bool] = {}
    for row in table.body():
        if len(row.cells) < 6:
            continue
        c = row.cells
        fld = normalize_field(c[1].all_text)
        fk_table = c[2].all_text
        fk_field = normalize_field(c[3].all_text)
        check = normalize_table(c[4].links[0] if c[4].links else c[4].own_text)
        check_field = normalize_field(c[5].all_text)
        if not fld or not check:
            continue
        host, is_partial = _host(self_name, fk_table, fk_field)
        key = (fld, check)
        groups.setdefault(key, []).append((host, check_field))
        partial[key] = partial.get(key, False) or is_partial
    return tuple(
        FkRef(field=f, check_table=t, columns=tuple(cols), partial=partial[(f, t)])
        for (f, t), cols in groups.items()
    )


def parse_table(html: str, *, fetched_at: str = "", source_url: str = "") -> TableDef:
    """Parse one leanx table page. Raises ValueError when `html` is not a table page.

    Guarantees: field names/description/key/data_element/check_table/data_type/length/decimals
    filled as the page shows them (missing numbers → 0, missing text → ''); `values` carries the
    (Value, Description) pairs of the field's 'Possible values' sub-table with the NULL sentinel
    rendered as '' ; foreign keys grouped per (field, check_table) in page order with the column
    pairs in page order; `category` is 'STRUCT' when the page has no FK section AND no key fields,
    else ''; `source` is 'leanx'; `name` is the h1's table name upper-cased; `position` is
    the field's 1-based place on the page; `source_url` defaults to the canonical page URL."""
    page = _scan(html)
    if not page.name or page.fields_table is None:
        raise ValueError("not a leanx table page: no 'SAP Table <NAME>' h1 with a fields table")
    fields = _fields(page.fields_table)
    fks = _foreign_keys(page.name, page.fk_table) if page.fk_table is not None else ()
    has_keys = any(f.key for f in fields)
    category = "STRUCT" if page.fk_table is None and not has_keys else ""
    return TableDef(
        name=page.name,
        description=page.description,
        category=category,
        fields=fields,
        foreign_keys=fks,
        source=SOURCE,
        source_url=source_url or table_url(page.name),
        fetched_at=fetched_at,
    )
