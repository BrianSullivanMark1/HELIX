"""The Snowflake writer (helix/domain/sap/sql.py): golden SQL for a three-table plan with a DATS
column, a recipe CTE and the plant filter; compound ON keys kept whole; LEFT JOIN for lookup hops
(text tables, and n:1 hops off a non-key column) with the edge's filters in the ON clause and the
table's own filters NULL-tolerant in WHERE; a reserved-word alias quoted; optional filters dropped
on request; the overlay's and the dictionary's warnings; quote_literal; ROOT.* for no columns; a
recipe whose base table is absent skipped with a warning. Plans are built BY HAND from model.py —
the graph is not imported, so this file pins the writer alone."""
from __future__ import annotations

from helix.domain.sap.curated import RECIPES, STANDARD_FILTERS
from helix.domain.sap.model import FieldDef, Filter, JoinEdge, JoinPlan, JoinStep, TableDef
from helix.domain.sap.sql import (
    ColumnSpec,
    EdwNaming,
    QuerySpec,
    column_name,
    quote_literal,
    render_column,
    render_filter,
    render_recipe_cte,
    write_sql,
)

# ----- a small dictionary: the tables the plans below touch -----


def _table(name: str, *fields: tuple, source: str = "leanx") -> TableDef:
    """fields: (NAME, TYPE) or (NAME, TYPE, key)."""
    return TableDef(
        name=name, source=source, fetched_at="2026-09-15",
        fields=tuple(FieldDef(name=f[0], data_type=f[1], key=bool(f[2]) if len(f) > 2 else False)
                     for f in fields),
    )


AFRU = _table(
    "AFRU", ("MANDT", "CLNT", True), ("RUECK", "NUMC", True), ("RMZHL", "NUMC", True),
    ("AUFNR", "CHAR"), ("AUFPL", "NUMC"), ("APLZL", "NUMC"), ("WERKS", "CHAR"), ("BUDAT", "DATS"),
    ("ERZET", "TIMS"), ("STOKZ", "CHAR"), ("STZHL", "NUMC"), ("LMNGA", "QUAN"), ("ARBID", "NUMC"),
)
AUFK = _table(
    "AUFK", ("MANDT", "CLNT", True), ("AUFNR", "CHAR", True), ("OBJNR", "CHAR"), ("AUTYP", "NUMC"),
    ("LOEKZ", "CHAR"), ("KTEXT", "CHAR"), ("WERKS", "CHAR"),
)
AFPO = _table(
    "AFPO", ("MANDT", "CLNT", True), ("AUFNR", "CHAR", True), ("POSNR", "NUMC", True),
    ("MATNR", "CHAR"), ("CHARG", "CHAR"), ("DWERK", "CHAR"), ("PROJN", "NUMC"),
)
AFKO = _table("AFKO", ("MANDT", "CLNT", True), ("AUFNR", "CHAR", True), ("AUFPL", "NUMC"),
              ("RSNUM", "NUMC"), ("PLNBEZ", "CHAR"))
AFVC = _table("AFVC", ("MANDT", "CLNT", True), ("AUFPL", "NUMC", True), ("APLZL", "NUMC", True),
              ("VORNR", "CHAR"), ("OBJNR", "CHAR"), ("ARBID", "NUMC"), ("PROJN", "NUMC"))
PRPS = _table("PRPS", ("MANDT", "CLNT", True), ("PSPNR", "NUMC", True), ("POSID", "CHAR"),
              ("POST1", "CHAR"), ("LOEVM", "CHAR"))
JEST = _table("JEST", ("MANDT", "CLNT", True), ("OBJNR", "CHAR", True), ("STAT", "CHAR", True),
              ("INACT", "CHAR"))
CRHD = _table("CRHD", ("MANDT", "CLNT", True), ("OBJTY", "CHAR", True), ("OBJID", "NUMC", True),
              ("ARBPL", "CHAR"))
CRTX = _table("CRTX", ("MANDT", "CLNT", True), ("OBJTY", "CHAR", True), ("OBJID", "NUMC", True),
              ("SPRAS", "LANG", True), ("KTEXT", "CHAR"))
MARA = _table("MARA", ("MANDT", "CLNT", True), ("MATNR", "CHAR", True), ("MTART", "CHAR"))
MAKT = _table("MAKT", ("MANDT", "CLNT", True), ("MATNR", "CHAR", True), ("SPRAS", "LANG", True),
              ("MAKTX", "CHAR"))
TABLES = {t.name: t for t in (AFRU, AUFK, AFPO, AFKO, AFVC, PRPS, JEST, CRHD, CRTX, MARA, MAKT)}

NAMING = EdwNaming(plant="1006")

AFRU_AUFK = JoinEdge("AFRU", "AUFK", (("MANDT", "MANDT"), ("AUFNR", "AUFNR")), kind="curated",
                     cardinality="n:1", note="the order a confirmation belongs to")
AUFK_AFPO = JoinEdge("AUFK", "AFPO", (("MANDT", "MANDT"), ("AUFNR", "AUFNR")), kind="curated",
                     cardinality="1:n", note="order items")


def _plan(root: str, *edges: JoinEdge, filters: tuple[Filter, ...] = ()) -> JoinPlan:
    """A plan the way the graph would build it: each edge joins its right table onto its left,
    aliases are the table names, in FROM/JOIN order."""
    aliases = [(root, root)]
    steps = []
    for e in edges:
        steps.append(JoinStep(edge=e, alias_left=e.left, alias_right=e.right))
        aliases.append((e.right, e.right))
    return JoinPlan(root=root, steps=tuple(steps), aliases=tuple(aliases), filters=filters)


# ----- the small pieces -----

def test_quote_literal_doubles_embedded_quotes():
    assert quote_literal("1006") == "'1006'"
    assert quote_literal("") == "''"
    assert quote_literal("O'Brien's") == "'O''Brien''s'"


def test_edw_naming_qualifies_a_table_and_tolerates_the_view_prefix():
    assert NAMING.qualified("AFRU") == "EDW.SRC_SAPECC_ARP.TV_AFRU"
    assert NAMING.qualified("tv_afru") == "EDW.SRC_SAPECC_ARP.TV_AFRU"
    assert EdwNaming(prefix="", view_prefix="").qualified("AFRU") == "AFRU"


def test_render_filter_covers_equals_in_lists_like_and_null_tests():
    assert render_filter(Filter("AUFK", "AUTYP", "=", "10"), "AUFK") == "AUFK.AUTYP = '10'"
    assert render_filter(Filter("JEST", "INACT", "=", ""), "J") == "J.INACT = ''"
    assert render_filter(Filter("JEST", "STAT", "in", "I0002, 'I0009'"), "JEST") == \
        "JEST.STAT IN ('I0002', 'I0009')"
    assert render_filter(Filter("AUFK", "KTEXT", "LIKE", "%O'NEIL%"), "AUFK") == \
        "AUFK.KTEXT LIKE '%O''NEIL%'"
    assert render_filter(Filter("AFPO", "CHARG", "is null"), "AFPO") == "AFPO.CHARG IS NULL"
    assert render_filter(Filter("AFPO", "CHARG", "IS NOT NULL", "ignored"), "AFPO") == \
        "AFPO.CHARG IS NOT NULL"
    assert render_filter(Filter("AFRU", "BUDAT", ">=", "20260101"), "AFRU") == \
        "AFRU.BUDAT >= '20260101'"


def test_render_column_converts_dates_and_times_only_when_the_dictionary_says_so():
    budat = ColumnSpec("AFRU", "BUDAT")
    assert render_column(budat, "AFRU", AFRU, convert_dates=True) == \
        "TO_DATE(NULLIF(AFRU.BUDAT, '00000000'), 'YYYYMMDD') AS BUDAT"
    assert render_column(ColumnSpec("AFRU", "ERZET"), "AFRU", AFRU, convert_dates=True) == \
        "TO_TIME(NULLIF(AFRU.ERZET, '000000'), 'HH24MISS') AS ERZET"
    # switched off, or an unknown TableDef: the raw reference — never a guessed conversion
    assert render_column(budat, "AFRU", AFRU, convert_dates=False) == "AFRU.BUDAT"
    assert render_column(budat, "AFRU", None, convert_dates=True) == "AFRU.BUDAT"
    # an explicit alias wins, and a label that is not an identifier is quoted for Snowflake
    assert render_column(ColumnSpec("AFRU", "BUDAT", alias="POSTED"), "AFRU", AFRU,
                         convert_dates=True) == \
        "TO_DATE(NULLIF(AFRU.BUDAT, '00000000'), 'YYYYMMDD') AS POSTED"
    assert render_column(ColumnSpec("AUFK", "KTEXT", alias="Order Text"), "AUFK", AUFK,
                         convert_dates=True) == 'AUFK.KTEXT AS "Order Text"'
    assert render_column(ColumnSpec("aufk", "ktext"), "AUFK", AUFK, convert_dates=True) == \
        "AUFK.KTEXT"


def test_an_expression_column_is_written_verbatim_with_its_alias():
    days_old = "DATEDIFF('day', TO_DATE(NULLIF(AFKO.FTRMI, '00000000'), 'YYYYMMDD'), CURRENT_DATE)"
    col = ColumnSpec("", "", alias="DAYS_OLD", expression=days_old)
    assert render_column(col, "", None, convert_dates=True) == f"{days_old} AS DAYS_OLD"
    assert column_name(col) == "DAYS_OLD"
    bare = ColumnSpec("", "", expression="COUNT(*)")
    assert render_column(bare, "", None, convert_dates=True) == "COUNT(*)"


def test_a_recipe_cte_substitutes_the_view_names_and_the_language():
    text = render_recipe_cte(RECIPES["system_status"], EdwNaming(language="D"))
    assert text.startswith("sys_status AS (\n  SELECT j.OBJNR,\n")
    assert "  FROM EDW.SRC_SAPECC_ARP.TV_JEST j\n" in text
    assert "JOIN EDW.SRC_SAPECC_ARP.TV_TJ02T t ON t.ISTAT = j.STAT AND t.SPRAS = 'D'\n" in text
    assert "{T:" not in text and "{LANG}" not in text and text.endswith("\n)")


# ----- the golden query -----

GOLDEN = """WITH sys_status AS (
  SELECT j.OBJNR,
         LISTAGG(t.TXT04, ' ') WITHIN GROUP (ORDER BY t.TXT04) AS SYSTEM_STATUS
  FROM EDW.SRC_SAPECC_ARP.TV_JEST j
  JOIN EDW.SRC_SAPECC_ARP.TV_TJ02T t ON t.ISTAT = j.STAT AND t.SPRAS = 'E'
  WHERE j.INACT = ''
  GROUP BY j.OBJNR
)
SELECT AFRU.RUECK,
       AFRU.RMZHL,
       AUFK.AUFNR,
       AUFK.KTEXT,
       AFPO.MATNR,
       TO_DATE(NULLIF(AFRU.BUDAT, '00000000'), 'YYYYMMDD') AS BUDAT,
       sys_status.SYSTEM_STATUS
FROM EDW.SRC_SAPECC_ARP.TV_AFRU AS AFRU
LEFT JOIN EDW.SRC_SAPECC_ARP.TV_AUFK AS AUFK
  ON AUFK.MANDT = AFRU.MANDT AND AUFK.AUFNR = AFRU.AUFNR
JOIN EDW.SRC_SAPECC_ARP.TV_AFPO AS AFPO
  ON AFPO.MANDT = AUFK.MANDT AND AFPO.AUFNR = AUFK.AUFNR
LEFT JOIN sys_status ON sys_status.OBJNR = AUFK.OBJNR
WHERE AFRU.WERKS = '1006'  -- plant (optional)
  AND AFRU.STOKZ = ''  -- leave out reversal documents (STOKZ='X' is a reversal) (optional)
  AND (AUFK.AUTYP = '10' OR AUFK.AUFNR IS NULL)  -- production orders only — AUFK also holds \
internal orders (01), networks (20), maintenance orders (30) and process orders (40) (optional)
LIMIT 100;"""


def _golden_plan() -> JoinPlan:
    return _plan("AFRU", AFRU_AUFK, AUFK_AFPO,
                 filters=(STANDARD_FILTERS["AFRU"][0], STANDARD_FILTERS["AUFK"][0]))


def _golden_spec(**overrides) -> QuerySpec:
    kw = dict(
        tables=("AFRU", "AUFK", "AFPO"),
        columns=(ColumnSpec("AFRU", "RUECK"), ColumnSpec("AFRU", "RMZHL"),
                 ColumnSpec("AUFK", "AUFNR"), ColumnSpec("AUFK", "KTEXT"),
                 ColumnSpec("AFPO", "MATNR"), ColumnSpec("AFRU", "BUDAT"),
                 ColumnSpec("sys_status", "SYSTEM_STATUS")),
        recipes=("system_status",),
    )
    kw.update(overrides)
    return QuerySpec(**kw)


def test_a_three_table_plan_with_a_recipe_renders_the_golden_sql():
    result = write_sql(_golden_spec(), _golden_plan(), NAMING, tables=TABLES)
    assert result.sql == GOLDEN
    assert result.warnings == ()
    assert result.columns == ("RUECK", "RMZHL", "AUFNR", "KTEXT", "MATNR", "BUDAT", "SYSTEM_STATUS")
    assert result.plan is _golden_plan() or result.plan == _golden_plan()
    # the recipe's own note rides along, prefixed by its CTE name
    assert any(n.startswith("sys_status: Join sys_status on the object's OBJNR")
               for n in result.notes)
    # AUFK is a lookup off AFRU.AUFNR (not AFRU's key), and the writer says what that means
    assert ("AUFK is LEFT JOINed, so a row without a AUFK match keeps its columns NULL rather "
            "than disappearing") in result.notes


AFVC_AFRU = JoinEdge("AFVC", "AFRU", (("MANDT", "MANDT"), ("AUFPL", "AUFPL"), ("APLZL", "APLZL")),
                     kind="curated", cardinality="1:n")


def test_a_compound_key_keeps_every_on_pair():
    afvc_afru = AFVC_AFRU
    result = write_sql(QuerySpec(tables=("AFVC", "AFRU"), columns=(ColumnSpec("AFRU", "RUECK"),)),
                       _plan("AFVC", afvc_afru), EdwNaming(), tables=TABLES)
    assert ("JOIN EDW.SRC_SAPECC_ARP.TV_AFRU AS AFRU\n"
            "  ON AFRU.MANDT = AFVC.MANDT AND AFRU.AUFPL = AFVC.AUFPL AND AFRU.APLZL = AFVC.APLZL\n"
            ) in result.sql
    assert "WHERE" not in result.sql          # no plant, no plan filters, no user filters
    assert result.sql.endswith("\nLIMIT 100;")


def test_a_text_table_hop_is_a_left_join_with_its_language_filter_in_the_on_clause():
    crhd_crtx = JoinEdge("CRHD", "CRTX",
                         (("MANDT", "MANDT"), ("OBJTY", "OBJTY"), ("OBJID", "OBJID")),
                         kind="text", cardinality="1:n")
    afru_crhd = JoinEdge("AFRU", "CRHD", (("MANDT", "MANDT"), ("ARBID", "OBJID")), kind="curated",
                         cardinality="n:1", filters=STANDARD_FILTERS["CRHD"])
    spras = Filter("CRTX", "SPRAS", "=", "E", "one language — SAP's language key is one character")
    plan = _plan("AFRU", afru_crhd, crhd_crtx, filters=(STANDARD_FILTERS["CRHD"][0], spras))
    spec = QuerySpec(tables=("AFRU", "CRHD", "CRTX"),
                     columns=(ColumnSpec("AFRU", "RUECK"), ColumnSpec("CRHD", "ARBPL"),
                              ColumnSpec("CRTX", "KTEXT")))
    result = write_sql(spec, plan, NAMING, tables=TABLES)
    # CRHD is a lookup too (ARBID is not AFRU's key), so its edge filter rides in its ON clause
    assert ("LEFT JOIN EDW.SRC_SAPECC_ARP.TV_CRHD AS CRHD\n"
            "  ON CRHD.MANDT = AFRU.MANDT AND CRHD.OBJID = AFRU.ARBID\n"
            "  AND CRHD.OBJTY = 'A'  -- work centers only — CRHD also holds other capacity "
            "objects; OBJTY 'A' is a work center\n"
            "LEFT JOIN EDW.SRC_SAPECC_ARP.TV_CRTX AS CRTX\n"
            "  ON CRTX.MANDT = CRHD.MANDT AND CRTX.OBJTY = CRHD.OBJTY AND CRTX.OBJID = CRHD.OBJID\n"
            "  AND CRTX.SPRAS = 'E'  -- one language — SAP's language key is one character\n"
            "WHERE AFRU.WERKS = '1006'  -- plant (optional)\n"
            "LIMIT 100;") in result.sql
    # neither filter is repeated in WHERE — there it would undo the LEFT JOIN
    assert result.sql.count("CRTX.SPRAS") == 1 and result.sql.count("CRHD.OBJTY = 'A'") == 1
    assert any("CRTX is LEFT JOINed" in n for n in result.notes)
    assert any("CRHD is LEFT JOINed" in n for n in result.notes)


def test_a_one_to_many_hop_into_a_dictionary_text_table_is_a_left_join():
    # the dictionary edge has no 'text' kind; the LANG key on MAKT is what makes it a text table
    mara_makt = JoinEdge("MARA", "MAKT", (("MANDT", "MANDT"), ("MATNR", "MATNR")), kind="fk",
                         cardinality="1:n")
    result = write_sql(QuerySpec(tables=("MARA", "MAKT"), columns=(ColumnSpec("MAKT", "MAKTX"),)),
                       _plan("MARA", mara_makt), EdwNaming(), tables=TABLES)
    assert "\nLEFT JOIN EDW.SRC_SAPECC_ARP.TV_MAKT AS MAKT\n" in result.sql
    # the same 1:n edge into a table nobody marked as text stays an inner join
    result2 = write_sql(QuerySpec(tables=("AUFK", "AFPO"), columns=(ColumnSpec("AFPO", "MATNR"),)),
                        _plan("AUFK", AUFK_AFPO), EdwNaming(), tables=TABLES)
    assert "\nJOIN EDW.SRC_SAPECC_ARP.TV_AFPO AS AFPO\n" in result2.sql
    assert "LEFT JOIN" not in result2.sql


# ----- lookup hops: master data a row may not have must not drop the row -----

AFPO_PRPS = JoinEdge("AFPO", "PRPS", (("MANDT", "MANDT"), ("PROJN", "PSPNR")), kind="curated",
                     cardinality="n:1", filters=STANDARD_FILTERS["PRPS"],
                     note="the WBS element of the order item")


def test_a_lookup_hop_is_a_left_join_with_the_edge_filter_in_the_on_clause():
    # PROJN is not a key of AFPO: an order item with no WBS element has no PRPS row, and the
    # user's WIP report lost every such operation to an inner join
    plan = _plan("AUFK", AUFK_AFPO, AFPO_PRPS, filters=(STANDARD_FILTERS["PRPS"][0],))
    spec = QuerySpec(tables=("AUFK", "AFPO", "PRPS"),
                     columns=(ColumnSpec("AFPO", "MATNR"), ColumnSpec("PRPS", "POSID")))
    result = write_sql(spec, plan, EdwNaming(), tables=TABLES)
    assert result.sql == (
        "SELECT AFPO.MATNR,\n"
        "       PRPS.POSID\n"
        "FROM EDW.SRC_SAPECC_ARP.TV_AUFK AS AUFK\n"
        "JOIN EDW.SRC_SAPECC_ARP.TV_AFPO AS AFPO\n"
        "  ON AFPO.MANDT = AUFK.MANDT AND AFPO.AUFNR = AUFK.AUFNR\n"
        "LEFT JOIN EDW.SRC_SAPECC_ARP.TV_PRPS AS PRPS\n"
        "  ON PRPS.MANDT = AFPO.MANDT AND PRPS.PSPNR = AFPO.PROJN\n"
        "  AND PRPS.LOEVM = ''  -- WBS elements not flagged for deletion (optional)\n"
        "LIMIT 100;")
    # the edge filter is written once, in ON — a second copy in WHERE would undo the LEFT JOIN
    assert result.sql.count("PRPS.LOEVM") == 1 and "WHERE" not in result.sql
    assert ("PRPS is LEFT JOINed with its filters in the ON clause, so a row without a PRPS "
            "match keeps its columns NULL rather than disappearing") in result.notes
    assert result.warnings == ()


def test_a_hop_on_the_left_tables_own_key_stays_an_inner_join():
    # AUFPL is a key of AFVC: AFKO is the operation's own order header, never absent
    afvc_afko = JoinEdge("AFVC", "AFKO", (("MANDT", "MANDT"), ("AUFPL", "AUFPL")), kind="curated",
                         cardinality="n:1")
    result = write_sql(QuerySpec(tables=("AFVC", "AFKO"), columns=(ColumnSpec("AFKO", "AUFNR"),)),
                       _plan("AFVC", afvc_afko), EdwNaming(), tables=TABLES)
    assert "\nJOIN EDW.SRC_SAPECC_ARP.TV_AFKO AS AFKO\n" in result.sql
    assert "LEFT JOIN" not in result.sql and result.notes == ()
    # a 1:1 hop and a 1:n hop are not lookups either, whatever the columns
    aufk_afko = JoinEdge("AUFK", "AFKO", (("MANDT", "MANDT"), ("AUFNR", "AUFNR")), kind="curated",
                         cardinality="1:1")
    result2 = write_sql(QuerySpec(tables=("AUFK", "AFKO", "AFPO"),
                                  columns=(ColumnSpec("AFKO", "AUFPL"),)),
                        _plan("AUFK", aufk_afko, AUFK_AFPO), EdwNaming(), tables=TABLES)
    assert "LEFT JOIN" not in result2.sql


def test_an_unknown_left_table_def_keeps_the_inner_join():
    # ZZTAB is not in the catalog, so nobody can say whether AUFNR is its key — the writer does
    # not guess a LEFT JOIN, and its filters stay plain
    zz_aufk = JoinEdge("ZZTAB", "AUFK", (("MANDT", "MANDT"), ("AUFNR", "AUFNR")), kind="curated",
                       cardinality="n:1")
    plan = _plan("ZZTAB", zz_aufk, filters=(STANDARD_FILTERS["AUFK"][0],))
    result = write_sql(QuerySpec(tables=("ZZTAB", "AUFK"), columns=(ColumnSpec("AUFK", "KTEXT"),)),
                       plan, EdwNaming(), tables=TABLES)
    assert "\nJOIN EDW.SRC_SAPECC_ARP.TV_AUFK AS AUFK\n" in result.sql
    assert "LEFT JOIN" not in result.sql and "IS NULL" not in result.sql
    assert "\nWHERE AUFK.AUTYP = '10'  -- production orders only" in result.sql


def test_a_table_level_filter_on_a_left_joined_table_tolerates_a_missing_row():
    # AUFK's standard filters are the table's own, not the edge's: they stay in WHERE, but a
    # confirmation with no order must pass them — the NULL test is on the lookup's key column
    plan = _plan("AFRU", AFRU_AUFK, filters=STANDARD_FILTERS["AUFK"])
    spec = QuerySpec(tables=("AFRU", "AUFK"), columns=(ColumnSpec("AUFK", "KTEXT"),))
    result = write_sql(spec, plan, EdwNaming(), tables=TABLES)
    assert result.sql.endswith(
        "LEFT JOIN EDW.SRC_SAPECC_ARP.TV_AUFK AS AUFK\n"
        "  ON AUFK.MANDT = AFRU.MANDT AND AUFK.AUFNR = AFRU.AUFNR\n"
        "WHERE (AUFK.AUTYP = '10' OR AUFK.AUFNR IS NULL)  -- production orders only — AUFK also "
        "holds internal orders (01), networks (20), maintenance orders (30) and process orders "
        "(40) (optional)\n"
        "  AND (AUFK.LOEKZ = '' OR AUFK.AUFNR IS NULL)  -- orders not flagged for deletion "
        "(optional)\n"
        "LIMIT 100;")
    # dropping the optional filters drops them from WHERE like anywhere else
    bare = write_sql(QuerySpec(tables=("AFRU", "AUFK"), columns=(ColumnSpec("AUFK", "KTEXT"),),
                               include_optional_filters=False), plan, EdwNaming(), tables=TABLES)
    assert "WHERE" not in bare.sql and "LEFT JOIN" in bare.sql


def test_a_filter_already_in_the_on_clause_is_not_written_again_in_where():
    # the plan carries CRHD.OBJTY both as the edge's own filter and (de-duplicated by the graph)
    # as a table filter; a hand-made table-level filter on CRHD is the one that goes to WHERE
    afru_crhd = JoinEdge("AFRU", "CRHD", (("MANDT", "MANDT"), ("ARBID", "OBJID")), kind="curated",
                         cardinality="n:1", filters=STANDARD_FILTERS["CRHD"])
    named = Filter("CRHD", "ARBPL", "<>", "", "work centers with a key")
    plan = _plan("AFRU", afru_crhd, filters=(STANDARD_FILTERS["CRHD"][0], named))
    spec = QuerySpec(tables=("AFRU", "CRHD"), columns=(ColumnSpec("CRHD", "ARBPL"),))
    result = write_sql(spec, plan, EdwNaming(), tables=TABLES)
    assert result.sql.count("CRHD.OBJTY = 'A'") == 1
    assert "  ON CRHD.MANDT = AFRU.MANDT AND CRHD.OBJID = AFRU.ARBID\n  AND CRHD.OBJTY = 'A'" \
        in result.sql
    assert result.sql.endswith(
        "WHERE (CRHD.ARBPL <> '' OR CRHD.OBJID IS NULL)  -- work centers with a key\n"
        "LIMIT 100;")


def test_a_reserved_word_alias_is_quoted_for_snowflake():
    # `AUFK.AUFNR AS ORDER` is what the WIP report wrote, and Snowflake refused it
    assert render_column(ColumnSpec("AUFK", "AUFNR", alias="ORDER"), "AUFK", AUFK,
                         convert_dates=True) == 'AUFK.AUFNR AS "ORDER"'
    assert render_column(ColumnSpec("AUFK", "AUFNR", alias="order"), "AUFK", AUFK,
                         convert_dates=True) == 'AUFK.AUFNR AS "order"'
    assert render_column(ColumnSpec("AUFK", "AUFNR", alias="ORDER_NO"), "AUFK", AUFK,
                         convert_dates=True) == "AUFK.AUFNR AS ORDER_NO"
    # a date conversion and an expression carry the same rule
    assert render_column(ColumnSpec("AFRU", "BUDAT", alias="DATE"), "AFRU", AFRU,
                         convert_dates=True) == \
        "TO_DATE(NULLIF(AFRU.BUDAT, '00000000'), 'YYYYMMDD') AS \"DATE\""
    assert render_column(ColumnSpec("", "", alias="GROUP", expression="COUNT(*)"), "", None,
                         convert_dates=True) == 'COUNT(*) AS "GROUP"'
    spec = QuerySpec(tables=("AUFK",), columns=(ColumnSpec("AUFK", "AUFNR", alias="ORDER"),))
    result = write_sql(spec, _plan("AUFK"), EdwNaming(), tables=TABLES)
    assert result.sql.startswith('SELECT AUFK.AUFNR AS "ORDER"\n')
    assert result.columns == ("ORDER",)


def test_optional_filters_are_dropped_when_asked_but_the_plant_filter_stays():
    inact = STANDARD_FILTERS["JEST"][0]           # not optional: a correctness rule
    aufk_jest = JoinEdge("AUFK", "JEST", (("MANDT", "MANDT"), ("OBJNR", "OBJNR")), kind="curated",
                         cardinality="1:n", filters=(inact,))
    plan = _plan("AUFK", aufk_jest, filters=(STANDARD_FILTERS["AUFK"][0], inact))
    spec = QuerySpec(tables=("AUFK", "JEST"), columns=(ColumnSpec("JEST", "STAT"),),
                     include_optional_filters=False)
    result = write_sql(spec, plan, NAMING, tables=TABLES)
    assert result.sql.endswith(
        "WHERE AUFK.WERKS = '1006'  -- plant (optional)\n"
        "  AND JEST.INACT = ''  -- active statuses only — JEST keeps every status ever set on the "
        "object; INACT='X' marks the ones no longer active\n"
        "LIMIT 100;")
    assert "AUTYP" not in result.sql
    # and with the default the optional one is back, after the plant filter, marked optional
    default_spec = QuerySpec(tables=("AUFK", "JEST"), columns=(ColumnSpec("JEST", "STAT"),))
    with_optional = write_sql(default_spec, plan, NAMING, tables=TABLES).sql
    assert "  AND AUFK.AUTYP = '10'  -- production orders only" in with_optional
    assert with_optional.index("AUFK.WERKS") < with_optional.index("AUFK.AUTYP")


def test_user_filters_come_last_and_a_limit_of_zero_means_none():
    spec = QuerySpec(tables=("AFRU",), columns=(ColumnSpec("AFRU", "RUECK"),),
                     filters=(Filter("AFRU", "BUDAT", ">=", "20260101", "this year"),
                              Filter("AFRU", "STOKZ", "=", "")), limit=0)
    result = write_sql(spec, _plan("AFRU"), NAMING, tables=TABLES)
    assert result.sql == (
        "SELECT AFRU.RUECK\n"
        "FROM EDW.SRC_SAPECC_ARP.TV_AFRU AS AFRU\n"
        "WHERE AFRU.WERKS = '1006'  -- plant (optional)\n"
        "  AND AFRU.BUDAT >= '20260101'  -- this year\n"
        "  AND AFRU.STOKZ = '';")


class _FakeOverlay:
    """Only what the writer relies on — status() and has_column() — plus a `tables` dict of
    {name: {"recorded_at": …}} so the date the user reported a missing table comes through."""

    def __init__(self, columns: dict[str, tuple[str, ...]], missing: dict[str, str]) -> None:
        self.columns = columns
        self.missing = missing
        self.tables = {t: {"recorded_at": d} for t, d in missing.items()}

    def status(self, table: str) -> str:
        if table in self.missing:
            return "missing"
        return "present" if table in self.columns else "unknown"

    def has_column(self, table: str, column: str) -> bool | None:
        cols = self.columns.get(table)
        return None if cols is None else column in cols


def test_the_overlay_warns_about_missing_unknown_and_absent_columns():
    overlay = _FakeOverlay(
        columns={"AFRU": ("MANDT", "RUECK", "RMZHL", "AUFNR", "WERKS", "STOKZ", "BUDAT")},
        missing={"AUFK": "2026-09-15"},
    )
    spec = _golden_spec(columns=(ColumnSpec("AFRU", "RUECK"), ColumnSpec("AFRU", "LMNGA"),
                                 ColumnSpec("AFPO", "MATNR")))
    result = write_sql(spec, _golden_plan(), NAMING, tables=TABLES, overlay=overlay)
    unknown = "{}: no column list recorded — I can't confirm its columns exist in the EDW"
    assert "TV_AUFK is not in your EDW (you told me on 2026-09-15)" in result.warnings
    assert unknown.format("AFPO") in result.warnings
    assert "AFRU.LMNGA is not in the column list you gave for TV_AFRU" in result.warnings
    # the recipe's own tables are checked too (JEST and TJ02T have no recorded columns)
    assert unknown.format("JEST") in result.warnings
    # confirmed columns raise nothing, and the SQL is still written in full
    assert not any("AFRU.RUECK" in w or "AFRU.WERKS" in w for w in result.warnings)
    assert "AFRU.LMNGA" in result.sql
    # a missing table with no date recorded still warns
    undated = _FakeOverlay(columns={}, missing={"AUFK": ""})
    result2 = write_sql(_golden_spec(), _golden_plan(), NAMING, tables=TABLES, overlay=undated)
    assert "TV_AUFK is not in your EDW (you told me)" in result2.warnings
    # no overlay → no availability warnings at all
    assert write_sql(_golden_spec(), _golden_plan(), NAMING, tables=TABLES).warnings == ()


def test_a_field_the_dictionary_does_not_know_is_warned_about():
    # HELIX used to invent AFRU.BUZEIT; the dictionary says AFRU has no such field
    spec = QuerySpec(tables=("AFRU",), columns=(ColumnSpec("AFRU", "BUZEIT"),),
                     filters=(Filter("AFRU", "POSTED", "=", "X"),))
    result = write_sql(spec, _plan("AFRU"), EdwNaming(), tables=TABLES)
    assert "AFRU.BUZEIT is not a field of AFRU in the dictionary" in result.warnings
    assert "AFRU.POSTED is not a field of AFRU in the dictionary" in result.warnings
    # a table the catalog does not hold gets a note, not a guess
    unknown = write_sql(QuerySpec(tables=("ZZTAB",), columns=(ColumnSpec("ZZTAB", "BUDAT"),)),
                        _plan("ZZTAB"), NAMING, tables=TABLES)
    assert "SELECT ZZTAB.BUDAT\n" in unknown.sql and "TO_DATE" not in unknown.sql
    assert unknown.warnings == ()
    assert any(n.startswith("ZZTAB is not in the catalog") for n in unknown.notes)


def test_no_columns_means_root_star():
    result = write_sql(QuerySpec(tables=("AFRU",)), _plan("AFRU"), NAMING, tables=TABLES)
    assert result.sql == (
        "SELECT AFRU.*\n"
        "FROM EDW.SRC_SAPECC_ARP.TV_AFRU AS AFRU\n"
        "WHERE AFRU.WERKS = '1006'  -- plant (optional)\n"
        "LIMIT 100;")
    assert result.columns == AFRU.field_names()
    # without a TableDef the column list can only say what the SQL says
    unknown = write_sql(QuerySpec(tables=("ZZTAB",)), _plan("ZZTAB"), EdwNaming())
    assert unknown.sql.startswith("SELECT ZZTAB.*\n") and unknown.columns == ("ZZTAB.*",)


def test_the_plant_filter_needs_a_dictionary_that_shows_the_field():
    # JEST has no WERKS: no filter, a note saying why
    result = write_sql(QuerySpec(tables=("JEST",), columns=(ColumnSpec("JEST", "STAT"),)),
                       _plan("JEST"), NAMING, tables=TABLES)
    assert "WHERE" not in result.sql
    assert any(n.startswith("no plant filter: JEST has no WERKS field") for n in result.notes)
    # an unknown TableDef: no guessed column, a note that says how to add it by hand
    unknown = write_sql(QuerySpec(tables=("ZZTAB",), columns=(ColumnSpec("ZZTAB", "X"),)),
                        _plan("ZZTAB"), NAMING, tables=TABLES)
    assert "WERKS" not in unknown.sql
    assert any("ZZTAB.WERKS = '1006'" in n for n in unknown.notes)
    # no plant configured: nothing, silently
    quiet = write_sql(QuerySpec(tables=("AFRU",)), _plan("AFRU"), EdwNaming(), tables=TABLES)
    assert "WHERE" not in quiet.sql and quiet.notes == ()


def test_a_recipe_whose_base_table_is_absent_warns_and_is_skipped():
    # serial_numbers hangs off AFPO; this plan stops at AUFK
    spec = QuerySpec(tables=("AFRU", "AUFK"), columns=(ColumnSpec("AUFK", "AUFNR"),),
                     recipes=("serial_numbers", "no_such_recipe"))
    result = write_sql(spec, _plan("AFRU", AFRU_AUFK), NAMING, tables=TABLES)
    assert "WITH" not in result.sql and "serials" not in result.sql
    assert any(w.startswith("recipe 'serial_numbers' joins on AFPO, none of which is in this query")
               for w in result.warnings)
    assert any(w.startswith("no recipe named 'no_such_recipe'") for w in result.warnings)


def test_a_compound_recipe_key_joins_on_every_column_of_the_first_present_group():
    spec = QuerySpec(tables=("AFVC", "AFRU"), columns=(ColumnSpec("AFVC", "VORNR"),),
                     recipes=("latest_confirmation", "system_status"))
    result = write_sql(spec, _plan("AFVC", AFVC_AFRU), EdwNaming(), tables=TABLES)
    assert result.sql.startswith("WITH last_conf AS (\n")
    assert "\n),\nsys_status AS (\n" in result.sql
    assert ("\nLEFT JOIN last_conf ON last_conf.AUFPL = AFVC.AUFPL "
            "AND last_conf.APLZL = AFVC.APLZL\n") in result.sql
    # system_status lists AUFK first, but AUFK is absent here — AFVC is the first present base
    assert "\nLEFT JOIN sys_status ON sys_status.OBJNR = AFVC.OBJNR\n" in result.sql


def test_an_unreachable_table_and_a_column_off_the_plan_are_warned_about():
    plan = JoinPlan(root="AFRU", aliases=(("AFRU", "AFRU"),), unreachable=("PRPS",),
                    notes=("no path from AFRU to PRPS",))
    spec = QuerySpec(tables=("AFRU", "PRPS"),
                     columns=(ColumnSpec("AFRU", "RUECK"), ColumnSpec("PRPS", "POSID")))
    result = write_sql(spec, plan, EdwNaming(), tables=TABLES)
    assert result.warnings[0] == "PRPS could not be joined into this query — it is left out"
    assert "PRPS.POSID: PRPS is not in this query's join plan" in result.warnings
    assert "no path from AFRU to PRPS" in result.notes
