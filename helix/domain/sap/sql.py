"""The Snowflake writer for the user's SAP replica in the EDW. Pure, deterministic.

The EDW mirrors SAP ECC as views named EDW.SRC_SAPECC_ARP.TV_<TABLE> with SAP's own column names.
The writer turns a JoinPlan plus requested columns into SQL the user can paste into a Snowflake
worksheet or Tableau custom SQL:

  - every table qualified through EdwNaming (prefix + view prefix), aliased by its plan alias;
  - every join ON clause carries the FULL compound key (MANDT included) — never AUFPL alone;
  - a LOOKUP hop is a LEFT JOIN: a hop into a text table (edge kind 'text', or 1:n into a table
    whose key has a LANG field), or an n:1 hop whose left-side ON columns are not all key fields
    of the left table (AFPO.PROJN → PRPS, AFVC.ARBID → CRHD, MARC → T024D) — master data the row
    may simply not have, and an inner join would silently drop an operation whose order has no
    WBS element. A hop on the left table's own key (AFVC.AUFPL → AFKO), a 1:1 or 1:n hop, or a
    hop whose left TableDef is unknown stays an inner JOIN;
  - a LEFT JOINed table's edge filters (CRHD.OBJTY = 'A', PA0002.ENDDA = '99991231',
    SPRAS = 'E') go into its ON clause, never WHERE — a WHERE on a LEFT JOINed column turns the
    join back into an inner one; its table-level filters (the plan's STANDARD_FILTERS, PRPS.LOEVM
    = '') stay in WHERE but NULL-tolerant, `(PRPS.LOEVM = '' OR PRPS.PSPNR IS NULL)`, and a filter
    already in an ON clause is never written a second time;
  - the plan's other filters in WHERE, each with a trailing comment saying why
    (curated.Filter.why);
  - the plant filter from the naming (WERKS = '1006') on the root table when the dictionary shows
    it has that field, marked optional in a comment so the user can drop it;
  - SAP names kept as-is (the user's rule) — a DATS column becomes
    TO_DATE(NULLIF(alias.FIELD, '00000000'), 'YYYYMMDD') AS FIELD and a TIMS column
    TO_TIME(NULLIF(alias.FIELD, '000000'), 'HH24MISS') AS FIELD when `convert_dates` (default on),
    so Tableau sees dates, not strings; an explicit alias wins;
  - recipes (curated.RECIPES) as CTEs in a WITH block, LEFT JOINed on their join_on columns;
  - a LIMIT, default 100;
  - warnings for anything the EDW overlay cannot confirm: a table marked missing, a column not in
    the recorded column list, a table with no recorded columns at all (unknown, not wrong) — and
    for a field the dictionary itself does not know, which is the guessed-name bug this faculty
    exists to end.

Nothing here talks to Snowflake; the output is text and a list of sentences.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from helix.domain.sap.curated import RECIPES, Recipe
from helix.domain.sap.model import (
    CLIENT_FIELD,
    EMPTY_DATE,
    EMPTY_TIME,
    Filter,
    JoinPlan,
    JoinStep,
    TableDef,
    normalize_field,
    normalize_table,
)

DEFAULT_PREFIX = "EDW.SRC_SAPECC_ARP"
DEFAULT_VIEW_PREFIX = "TV_"
DEFAULT_LIMIT = 100

# What an overlay's status() answers (overlay.PRESENT / MISSING / UNKNOWN). Spelled here so the
# writer takes any object with status()/has_column() and never imports the overlay module.
PRESENT, MISSING, UNKNOWN = "present", "missing", "unknown"

_TABLE_TOKEN = re.compile(r"\{T:([A-Za-z0-9_]+)\}")
_LANG_TOKEN = "{LANG}"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_INDENT = "  "
_SELECT_CONTINUATION = ",\n" + " " * len("SELECT ")
_NULL_OPS = ("IS NULL", "IS NOT NULL")
_LIST_OPS = ("IN", "NOT IN")
# Output names Snowflake refuses bare: `AS ORDER` is a syntax error (the WIP report's order-number
# column failed on exactly that), `AS "ORDER"` is a column called ORDER. Compared upper-cased,
# since Snowflake folds an unquoted identifier to upper case anyway.
_RESERVED: frozenset[str] = frozenset("""
    ALL ALTER AND ANY AS ASC BETWEEN BY CASE CAST CHECK COLUMN CONNECT CONSTRAINT CREATE CROSS
    CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER DATE DEFAULT DELETE DESC
    DISTINCT DROP ELSE END EXCEPT EXISTS FALSE FOLLOWING FOR FROM FULL GRANT GROUP HAVING ILIKE
    IN INCREMENT INNER INSERT INTERSECT INTO IS ISSUE JOIN KEY LATERAL LEFT LIKE LIMIT LOCALTIME
    LOCALTIMESTAMP MINUS NATURAL NOT NULL OF ON OR ORDER OUTER OVER PARTITION PRIMARY QUALIFY
    RANGE REFERENCES REGEXP RIGHT RLIKE ROW ROWS SAMPLE SCHEMA SELECT SET SOME START TABLE
    TABLESAMPLE THEN TIME TIMESTAMP TO TOP TRIGGER TRUE UNION UNIQUE UPDATE USER USING VALUES
    VIEW WHEN WHENEVER WHERE WITH
""".split())


@dataclass(frozen=True)
class EdwNaming:
    """How the EDW names things: `qualified('AFRU')` → 'EDW.SRC_SAPECC_ARP.TV_AFRU'."""

    prefix: str = DEFAULT_PREFIX
    view_prefix: str = DEFAULT_VIEW_PREFIX
    plant: str = ""           # the user's plant filter value ('' = none)
    plant_field: str = "WERKS"
    language: str = "E"

    def qualified(self, table: str) -> str:
        """'TV_AFRU' and 'AFRU' both come back as the one view name — the user says either."""
        view = f"{self.view_prefix}{normalize_table(table)}"
        return f"{self.prefix}.{view}" if self.prefix else view


@dataclass(frozen=True)
class ColumnSpec:
    table: str
    field: str
    alias: str = ""           # output name; '' keeps the SAP field name
    expression: str = ""      # a raw expression instead of alias.field (recipes, DATEDIFF …)


@dataclass(frozen=True)
class QuerySpec:
    tables: tuple[str, ...]
    columns: tuple[ColumnSpec, ...] = ()      # () = root.*
    filters: tuple[Filter, ...] = ()          # user filters, in addition to the plan's
    recipes: tuple[str, ...] = ()             # curated.RECIPES names to include as CTEs
    root: str = ""
    limit: int = DEFAULT_LIMIT
    convert_dates: bool = True
    include_optional_filters: bool = True     # the optional standard filters (AUTYP='10' …)


@dataclass(frozen=True)
class SqlResult:
    sql: str
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    plan: JoinPlan | None = None
    columns: tuple[str, ...] = field(default_factory=tuple)   # output column names in order


def quote_literal(value: str) -> str:
    """'…' with embedded quotes doubled."""
    return "'" + str("" if value is None else value).replace("'", "''") + "'"


def _bare(item: str) -> str:
    """One IN-list member as the user wrote it — 'I0002' or I0002 — down to its bare text, so the
    writer quotes every member exactly once."""
    s = item.strip()
    if len(s) >= 2 and s[0] == s[-1] == "'":
        s = s[1:-1].replace("''", "'")
    return s


def render_filter(flt: Filter, alias: str) -> str:
    """One WHERE condition: alias.FIELD = 'value' (IN lists, IS NULL, LIKE handled), no comment."""
    ref = f"{alias}.{normalize_field(flt.field)}"
    op = " ".join(str(flt.op or "=").split()).upper()
    if op in _NULL_OPS:
        return f"{ref} {op}"
    if op in _LIST_OPS:
        members = ", ".join(quote_literal(_bare(v)) for v in str(flt.value or "").split(","))
        return f"{ref} {op} ({members})"
    return f"{ref} {op} {quote_literal(flt.value)}"


def column_name(col: ColumnSpec) -> str:
    """The output name a column lands under: its alias, else the SAP field name as SAP spells it."""
    alias = (col.alias or "").strip()
    return alias if alias else normalize_field(col.field)


def _as_name(name: str) -> str:
    """A label that is not a plain identifier ("Posting Date"), or is a reserved word (ORDER),
    has to be double-quoted for Snowflake; a plain one is written bare so the SQL reads like the
    user's own."""
    if _IDENTIFIER.match(name) and name.upper() not in _RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'


def render_column(col: ColumnSpec, alias: str, table: TableDef | None, *,
                  convert_dates: bool) -> str:
    """The SELECT list item for a column: the raw reference, a date/time conversion for DATS/TIMS
    fields (when the TableDef says so), or the given expression; with `AS alias` when needed."""
    name = column_name(col)
    if col.expression:
        expr = col.expression.strip()
        return f"{expr} AS {_as_name(name)}" if name else expr
    fld = normalize_field(col.field)
    ref = f"{alias}.{fld}"
    fdef = table.field(fld) if table is not None else None
    if convert_dates and fdef is not None and fdef.is_date:
        return f"TO_DATE(NULLIF({ref}, '{EMPTY_DATE}'), 'YYYYMMDD') AS {_as_name(name)}"
    if convert_dates and fdef is not None and fdef.is_time:
        return f"TO_TIME(NULLIF({ref}, '{EMPTY_TIME}'), 'HH24MISS') AS {_as_name(name)}"
    if name != fld:
        return f"{ref} AS {_as_name(name)}"
    return ref


def render_recipe_cte(recipe: Recipe, naming: EdwNaming) -> str:
    """'<cte_name> AS (\n  <body>\n)' with {T:X} → the qualified view and {LANG} → the language."""
    body = _TABLE_TOKEN.sub(lambda m: naming.qualified(m.group(1)), recipe.cte_sql)
    body = body.replace(_LANG_TOKEN, naming.language.replace("'", "''"))
    indented = "\n".join(_INDENT + line if line else line for line in body.splitlines())
    return f"{recipe.cte_name} AS (\n{indented}\n)"


# ----- the pieces write_sql is assembled from -----

def _aliases(plan: JoinPlan) -> tuple[tuple[str, str], ...]:
    """(alias, TABLE) in FROM/JOIN order. The graph fills plan.aliases; a plan built by hand may
    leave it empty, so the root and the steps' own aliases are the fallback."""
    out: list[tuple[str, str]] = [(a, normalize_table(t)) for a, t in plan.aliases]
    known = {a for a, _t in out}
    root = normalize_table(plan.root)
    if root not in {t for _a, t in out}:
        out.insert(0, (root, root))
        known.add(root)
    for step in plan.steps:
        sides = ((step.alias_left, step.edge.left), (step.alias_right, step.edge.right))
        for alias, table in sides:
            if alias not in known:
                out.append((alias, normalize_table(table)))
                known.add(alias)
    return tuple(out)


def _alias_of(aliases: tuple[tuple[str, str], ...], table: str) -> str:
    t = normalize_table(table)
    for a, tab in aliases:
        if tab == t:
            return a
    return t


def _table_of(aliases: tuple[tuple[str, str], ...], alias: str, fallback: str) -> str:
    for a, tab in aliases:
        if a == alias:
            return tab
    return normalize_table(fallback)


def _join_groups(recipe: Recipe) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """join_on grouped by base table, order kept: (("AUFK","OBJNR"), ("AFVC","OBJNR")) are two
    alternatives, (("AFVC","AUFPL"), ("AFVC","APLZL")) is one compound key."""
    groups: dict[str, list[str]] = {}
    for table, fld in recipe.join_on:
        groups.setdefault(normalize_table(table), []).append(normalize_field(fld))
    return tuple((t, tuple(fs)) for t, fs in groups.items())


def _is_text_hop(step: JoinStep, defs: dict[str, TableDef], plan: JoinPlan) -> bool:
    """A hop that must not drop rows: the edge says text, or it is 1:n into a table the dictionary
    marks as a text table (a LANG key field), or 1:n into a table the plan filters on SPRAS — the
    graph's own mark of a text table, for a table whose TableDef the catalog does not hold."""
    edge = step.edge
    if edge.kind == "text":
        return True
    if edge.cardinality != "1:n":
        return False
    right = normalize_table(edge.right)
    tdef = defs.get(right)
    if tdef is not None and tdef.is_text_table:
        return True
    return any(normalize_table(f.table) == right and normalize_field(f.field) == "SPRAS"
               for f in plan.filters)


def _is_lookup_hop(step: JoinStep, defs: dict[str, TableDef], plan: JoinPlan,
                   left_table: str) -> bool:
    """A hop that must not drop rows: a text-table hop, or an n:1 hop on columns that are not the
    left table's own key — AFPO.PROJN → PRPS, AFVC.ARBID → CRHD, MARC → T024D — master data the
    row may simply not have (an order with no WBS element, a material with no MRP controller).
    A hop on the left table's key (AFVC.AUFPL → AFKO) reaches the row's own parent and stays
    inner; so does a hop whose left TableDef is unknown, because a guessed LEFT JOIN is a guess."""
    if _is_text_hop(step, defs, plan):
        return True
    edge = step.edge
    if edge.cardinality != "n:1" or not edge.on:
        return False
    tdef = defs.get(left_table)
    if tdef is None:
        return False
    for left_f, _right_f in edge.on:
        fdef = tdef.field(left_f)
        if fdef is None or not fdef.key:
            return True
    return False


def _ident(flt: Filter, table: str | None = None) -> tuple[str, str, str, str]:
    """(TABLE, FIELD, OP, value) — the identity the graph de-duplicates filters on, so the writer
    never says one twice either."""
    return (normalize_table(flt.table if table is None else table), normalize_field(flt.field),
            " ".join(str(flt.op or "=").split()).upper(), str(flt.value or ""))


def _edge_filter_idents(step: JoinStep, left_t: str, right_t: str) -> set[tuple[str, ...]]:
    """The identities of the edge's own filters. One that names neither side belongs to the right
    side — the JoinEdge contract, and what the graph does when it merges them into the plan."""
    sides = {normalize_table(step.edge.left), normalize_table(step.edge.right), left_t, right_t}
    out: set[tuple[str, ...]] = set()
    for flt in step.edge.filters:
        table = normalize_table(flt.table)
        out.add(_ident(flt, table if table in sides else right_t))
    return out


def _is_language_filter(flt: Filter, tdef: TableDef | None) -> bool:
    """The one-language filter the graph adds to a text table: on the dictionary's LANG key, or
    on SPRAS by convention when the table is not in the dictionary."""
    fld = normalize_field(flt.field)
    key = tdef.language_key() if tdef is not None else ""
    if key:
        return fld == key
    return fld == "SPRAS" or (flt.why or "").strip().lower().startswith("one language")


def _null_column(step: JoinStep) -> str:
    """The column whose NULL says 'no match': the first ON pair's right side, skipping the client
    column — `AUFK.AUFNR IS NULL` reads as 'no order', `AUFK.MANDT IS NULL` reads as a puzzle."""
    for _left_f, right_f in step.edge.on:
        fld = normalize_field(right_f)
        if fld != CLIENT_FIELD:
            return fld
    return normalize_field(step.edge.on[0][1]) if step.edge.on else ""


def _comment(flt: Filter) -> str:
    parts = [flt.why.strip()] if flt.why and flt.why.strip() else []
    if flt.optional:
        parts.append("(optional)")
    return f"  -- {' '.join(parts)}" if parts else ""


def _where_item(flt: Filter, alias: str) -> str:
    return render_filter(flt, alias) + _comment(flt)


def _null_tolerant_item(flt: Filter, alias: str, null_ref: str) -> str:
    """`(PRPS.LOEVM = '' OR PRPS.PSPNR IS NULL)  -- why`: a table-level filter on a LEFT JOINed
    table, written so a row with no match passes it instead of failing a NULL comparison — which
    is what a plain WHERE on a LEFT JOINed column does, quietly undoing the LEFT JOIN."""
    if not null_ref:
        return _where_item(flt, alias)
    return f"({render_filter(flt, alias)} OR {null_ref} IS NULL)" + _comment(flt)


def _plant_filter(naming: EdwNaming, root: str, root_def: TableDef | None,
                  notes: list[str]) -> Filter | None:
    """The plant filter only when the dictionary shows the root table has the field — WERKS on
    JEST would be a guessed column, the very thing this writer exists to stop."""
    if not naming.plant:
        return None
    fld = normalize_field(naming.plant_field)
    if root_def is None:
        notes.append(f"no plant filter: {root} is not in the catalog, so I can't confirm it has "
                     f"{fld} — add {root}.{fld} = {quote_literal(naming.plant)} yourself if it "
                     f"does")
        return None
    if not root_def.has_field(fld):
        notes.append(f"no plant filter: {root} has no {fld} field — filter the plant on a joined "
                     f"table that has one")
        return None
    return Filter(table=root, field=fld, op="=", value=naming.plant, why="plant", optional=True)


def _recorded_at(overlay, table: str) -> str:
    """The date the user told us about a table, when the overlay keeps one (EdwOverlay.tables);
    a bare status()/has_column() overlay simply has no date to give."""
    tables = getattr(overlay, "tables", None)
    entry = tables.get(table) if isinstance(tables, dict) else None
    if isinstance(entry, dict):
        return str(entry.get("recorded_at") or "")
    return str(getattr(entry, "recorded_at", "") or "")


def _dictionary_warnings(defs: dict[str, TableDef], refs: list[tuple[str, str]]) -> list[str]:
    out: list[str] = []
    for table, fld in refs:
        tdef = defs.get(table)
        if tdef is not None and not tdef.has_field(fld):
            out.append(f"{table}.{fld} is not a field of {table} in the dictionary")
    return out


def _overlay_warnings(overlay, naming: EdwNaming, tables: tuple[str, ...],
                      refs: list[tuple[str, str]]) -> list[str]:
    """What the user's EDW cannot confirm. Column checks only run on a table recorded as present:
    an unknown table already carries its one warning, a missing one is wrong regardless."""
    out: list[str] = []
    if overlay is None:
        return out
    for table in tables:
        status = overlay.status(table)
        view = f"{naming.view_prefix}{table}"
        if status == MISSING:
            when = _recorded_at(overlay, table)
            told = f"you told me on {when}" if when else "you told me"
            out.append(f"{view} is not in your EDW ({told})")
        elif status != PRESENT:
            out.append(f"{table}: no column list recorded — I can't confirm its columns exist in "
                       f"the EDW")
    for table, fld in refs:
        if overlay.status(table) == PRESENT and overlay.has_column(table, fld) is False:
            out.append(f"{table}.{fld} is not in the column list you gave for "
                       f"{naming.view_prefix}{table}")
    return out


def _unique(items: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for s in items:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return tuple(out)


def write_sql(spec: QuerySpec, plan: JoinPlan, naming: EdwNaming, *,
              tables: dict[str, TableDef] | None = None,
              overlay=None) -> SqlResult:
    """Render the query. `tables` maps name → TableDef for type-aware rendering (dates, existence
    checks); `overlay` is an overlay.EdwOverlay (or anything with status(table) and
    has_column(table, column); None = no availability warnings).
    Recipes named in the spec are rendered as CTEs (curated.RECIPES) and LEFT JOINed onto the
    first plan table their join_on names; a recipe none of whose base tables is in the plan is
    skipped with a warning. `include_optional_filters=False` drops the optional standard filters
    but not the plant filter — that one is the user's own setting; clear `naming.plant` to drop it.
    Output layout:

        WITH sys_status AS (
          …
        )
        SELECT AUFK.AUFNR,
               TO_DATE(NULLIF(AFRU.BUDAT, '00000000'), 'YYYYMMDD') AS BUDAT
        FROM EDW.SRC_SAPECC_ARP.TV_AFRU AS AFRU
        LEFT JOIN EDW.SRC_SAPECC_ARP.TV_AUFK AS AUFK
          ON AUFK.MANDT = AFRU.MANDT AND AUFK.AUFNR = AFRU.AUFNR
        LEFT JOIN sys_status ON sys_status.OBJNR = AUFK.OBJNR
        WHERE AFRU.WERKS = '1006'  -- plant (optional)
          AND (AUFK.AUTYP = '10' OR AUFK.AUFNR IS NULL)  -- production orders only — … (optional)
        LIMIT 100;
    """
    defs = {normalize_table(k): v for k, v in (tables or {}).items()}
    aliases = _aliases(plan)
    present = tuple(t for _a, t in aliases)
    root = normalize_table(plan.root)
    root_alias = _alias_of(aliases, root)
    include = spec.include_optional_filters
    warnings: list[str] = [f"{t} could not be joined into this query — it is left out"
                           for t in plan.unreachable]
    notes: list[str] = list(plan.notes)
    refs: list[tuple[str, str]] = []      # every (TABLE, FIELD) a clause touches, for the checks

    # ----- recipes → CTEs, each hooked onto the first plan table its join_on names -----
    ctes: list[tuple[Recipe, str, tuple[str, ...]]] = []
    for name in spec.recipes:
        recipe = RECIPES.get(str(name).strip().lower())
        if recipe is None:
            warnings.append(f"no recipe named '{name}' — skipped (I know {', '.join(RECIPES)})")
            continue
        groups = _join_groups(recipe)
        chosen = next(((t, fs) for t, fs in groups if t in present), None)
        if chosen is None:
            wanted = " or ".join(t for t, _fs in groups)
            warnings.append(f"recipe '{recipe.name}' joins on {wanted}, none of which is in this "
                            f"query — skipped")
            continue
        ctes.append((recipe, chosen[0], chosen[1]))
        refs.extend((chosen[0], f) for f in chosen[1])
        if recipe.note:
            notes.append(f"{recipe.cte_name}: {recipe.note}")
    cte_alias = {r.cte_name.upper(): r.cte_name for r, _t, _fs in ctes}

    # ----- SELECT -----
    names: list[str] = []
    uncatalogued: list[str] = []
    if spec.columns:
        items: list[str] = []
        for col in spec.columns:
            if col.expression:
                items.append(render_column(col, "", None, convert_dates=spec.convert_dates))
                names.append(column_name(col) or col.expression.strip())
                continue
            table, fld = normalize_table(col.table), normalize_field(col.field)
            if not fld:
                warnings.append(f"a column on {table or '?'} with neither a field nor an "
                                f"expression was skipped")
                continue
            if table in cte_alias:
                items.append(render_column(col, cte_alias[table], None, convert_dates=False))
            else:
                if table in present:
                    refs.append((table, fld))
                    if table not in defs:
                        uncatalogued.append(table)
                else:
                    warnings.append(f"{table}.{fld}: {table} is not in this query's join plan")
                items.append(render_column(col, _alias_of(aliases, table), defs.get(table),
                                           convert_dates=spec.convert_dates))
            names.append(column_name(col))
        select = "SELECT " + _SELECT_CONTINUATION.join(items) if items else f"SELECT {root_alias}.*"
    else:
        select = f"SELECT {root_alias}.*"
        rdef = defs.get(root)
        names = list(rdef.field_names()) if rdef is not None else [f"{root_alias}.*"]
    for table in _unique(uncatalogued):
        notes.append(f"{table} is not in the catalog — its columns are written as given "
                     f"(no date conversion, no field check)")

    # ----- FROM / JOIN -----
    lines = [select, f"FROM {naming.qualified(root)} AS {root_alias}"]
    # a LEFT JOINed table → 'alias.COLUMN' whose NULL means 'no match', for the NULL-tolerant
    # WHERE; `in_on` is every filter already written into an ON clause, so WHERE skips it
    left_joined: dict[str, str] = {}
    in_on: set[tuple[str, str, str, str]] = set()
    for step in plan.steps:
        edge = step.edge
        left_t = _table_of(aliases, step.alias_left, edge.left)
        right_t = _table_of(aliases, step.alias_right, edge.right)
        lookup = _is_lookup_hop(step, defs, plan, left_t)
        lines.append(f"{'LEFT JOIN' if lookup else 'JOIN'} {naming.qualified(right_t)} "
                     f"AS {step.alias_right}")
        if edge.on:
            pairs = " AND ".join(f"{step.alias_right}.{normalize_field(right_f)} = "
                                 f"{step.alias_left}.{normalize_field(left_f)}"
                                 for left_f, right_f in edge.on)
            lines.append(f"{_INDENT}ON {pairs}")
            for left_f, right_f in edge.on:
                refs.append((left_t, normalize_field(left_f)))
                refs.append((right_t, normalize_field(right_f)))
        else:
            lines.append(f"{_INDENT}ON 1 = 1  -- no join columns known for this edge")
            warnings.append(f"{right_t} is joined to {left_t} on no columns — the edge has no "
                            f"ON pairs, so this is a cross join")
        if lookup:
            null_col = _null_column(step)
            left_joined[right_t] = f"{step.alias_right}.{null_col}" if null_col else ""
            # the edge's own filters and the language filter belong to the join itself; the
            # table's other filters are the WHERE's business (NULL-tolerant, below)
            own = _edge_filter_idents(step, left_t, right_t)
            moved = [f for f in plan.filters
                     if normalize_table(f.table) == right_t and (include or not f.optional)
                     and (_ident(f) in own or _is_language_filter(f, defs.get(right_t)))]
            for flt in moved:
                lines.append(f"{_INDENT}AND {_where_item(flt, step.alias_right)}")
                refs.append((right_t, normalize_field(flt.field)))
                in_on.add(_ident(flt))
            how = " with its filters in the ON clause" if moved else ""
            notes.append(f"{right_t} is LEFT JOINed{how}, so a row without a {right_t} match "
                         f"keeps its columns NULL rather than disappearing")
    for recipe, base, fields in ctes:
        base_alias = _alias_of(aliases, base)
        pairs = " AND ".join(f"{recipe.cte_name}.{f} = {base_alias}.{f}" for f in fields)
        lines.append(f"LEFT JOIN {recipe.cte_name} ON {pairs}")

    # ----- WHERE: plant first, then the plan's filters, then the user's -----
    conditions: list[str] = []
    plant = _plant_filter(naming, root, defs.get(root), notes)
    if plant is not None:
        conditions.append(_where_item(plant, root_alias))
        refs.append((root, normalize_field(plant.field)))
    for flt in plan.filters:
        table = normalize_table(flt.table)
        if _ident(flt) in in_on or (flt.optional and not include):
            continue
        if table not in present:
            warnings.append(f"the plan filters {table}.{normalize_field(flt.field)} but {table} "
                            f"is not in the query")
        alias = _alias_of(aliases, table)
        if table in left_joined:
            conditions.append(_null_tolerant_item(flt, alias, left_joined[table]))
        else:
            conditions.append(_where_item(flt, alias))
        refs.append((table, normalize_field(flt.field)))
    for flt in spec.filters:
        table = normalize_table(flt.table)
        if table in cte_alias:
            alias = cte_alias[table]
        else:
            alias = _alias_of(aliases, table)
            if table in present:
                refs.append((table, normalize_field(flt.field)))
            else:
                warnings.append(f"filter on {table}.{normalize_field(flt.field)}: {table} is not "
                                f"in this query")
        conditions.append(_where_item(flt, alias))
    if conditions:
        lines.append("WHERE " + conditions[0])
        lines.extend(f"{_INDENT}AND {c}" for c in conditions[1:])
    if spec.limit and int(spec.limit) > 0:
        lines.append(f"LIMIT {int(spec.limit)}")
    sql = "\n".join(lines) + ";"
    if ctes:
        sql = "WITH " + ",\n".join(render_recipe_cte(r, naming) for r, _b, _f in ctes) + "\n" + sql

    # ----- what the dictionary and the EDW cannot confirm -----
    checked = list(present)
    for recipe, _b, _f in ctes:
        checked.extend(normalize_table(t) for t in recipe.tables)
    warnings.extend(_dictionary_warnings(defs, refs))
    warnings.extend(_overlay_warnings(overlay, naming, _unique(checked), refs))
    return SqlResult(sql=sql, warnings=_unique(warnings), notes=_unique(notes), plan=plan,
                     columns=tuple(names))
