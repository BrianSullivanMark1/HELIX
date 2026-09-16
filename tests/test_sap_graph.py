"""JoinGraph: curated edges over dictionary edges, the weighted shortest path that goes through
the order and never through T000, a text table or a shared status object, plans with a valid
join order, aliases, filters (standard, edge, language) and unreachable tables, and explain().

The dictionary here is hand-made: a few fields per table with the real keys, MAKT/CRTX/TJ02T
keyed on a LANG field (TJ02T without MANDT, as in SAP), every table with a KEY dependency on T000
and a REF dependency on T001W — the two "shared by chance" routes the graph must refuse."""
from __future__ import annotations

from dataclasses import replace

from helix.domain.sap.curated import CURATED_JOINS, STANDARD_FILTERS
from helix.domain.sap.graph import JoinGraph
from helix.domain.sap.model import FieldDef, Filter, FkRef, JoinEdge, TableDef

# ----- a small dictionary -----


def _fields(*specs: str) -> tuple[FieldDef, ...]:
    """'MANDT*:CLNT' → key field of type CLNT; 'AUFNR' → plain CHAR field."""
    out = []
    for i, spec in enumerate(specs):
        name, _, dtype = spec.partition(":")
        out.append(FieldDef(name=name.rstrip("*"), key=name.endswith("*"),
                            data_type=dtype or "CHAR", position=i + 1))
    return tuple(out)


def _client_fk() -> FkRef:
    return FkRef(field="MANDT", check_table="T000", columns=(("MANDT", "MANDT"),), kind="KEY")


def _plant_fk(field: str = "WERKS", kind: str = "REF") -> FkRef:
    return FkRef(field=field, check_table="T001W",
                 columns=(("MANDT", "MANDT"), (field, "WERKS")), kind=kind)


def _table(name: str, description: str, *fields: str, fks: tuple[FkRef, ...] = ()) -> TableDef:
    return TableDef(name, description, fields=_fields(*fields), foreign_keys=fks, source="test")


DICTIONARY: dict[str, TableDef] = {
    "AFRU": _table("AFRU", "Order Confirmations",
                   "MANDT*:CLNT", "RUECK*:NUMC", "RMZHL*:NUMC", "AUFNR", "AUFPL:NUMC", "APLZL:NUMC",
                   "VORNR", "ARBID:NUMC", "WERKS", "BUDAT:DATS", "STOKZ", "STZHL:NUMC",
                   "PERNR:NUMC",
                   fks=(_client_fk(), _plant_fk())),
    "AFVC": _table("AFVC", "Operation within an order",
                   "MANDT*:CLNT", "AUFPL*:NUMC", "APLZL*:NUMC", "VORNR", "ARBID:NUMC", "RUECK:NUMC",
                   "OBJNR", "STEUS", "PROJN:NUMC", "LOEKZ",
                   fks=(_client_fk(),)),
    "AUFK": _table("AUFK", "Order master data",
                   "MANDT*:CLNT", "AUFNR*", "OBJNR", "AUART", "AUTYP:NUMC", "WERKS", "PSPEL:NUMC",
                   "KTEXT", "LOEKZ",
                   fks=(_client_fk(), _plant_fk())),
    "AFPO": _table("AFPO", "Order item",
                   "MANDT*:CLNT", "AUFNR*", "POSNR*:NUMC", "MATNR", "DWERK", "PROJN:NUMC", "CHARG",
                   "PSMNG:QUAN",
                   fks=(_client_fk(), _plant_fk("DWERK"))),
    "PRPS": _table("PRPS", "WBS (Work Breakdown Structure) Element Master Data",
                   "MANDT*:CLNT", "PSPNR*:NUMC", "POSID", "POST1", "PSPHI:NUMC", "OBJNR", "WERKS",
                   "LOEVM",
                   fks=(_client_fk(), _plant_fk())),
    "MAKT": _table("MAKT", "Material Descriptions",
                   "MANDT*:CLNT", "MATNR*", "SPRAS*:LANG", "MAKTX",
                   fks=(_client_fk(),
                        FkRef("MATNR", "MARA", (("MANDT", "MANDT"), ("MATNR", "MATNR")),
                              kind="KEY"))),
    "TJ02T": _table("TJ02T", "System status texts", "ISTAT*", "SPRAS*:LANG", "TXT04", "TXT30"),
    "JEST": _table("JEST", "Individual Object Status", "MANDT*:CLNT", "OBJNR*", "STAT*", "INACT",
                   fks=(_client_fk(),)),
    "CRHD": _table("CRHD", "Work Center Header",
                   "MANDT*:CLNT", "OBJTY*", "OBJID*:NUMC", "ARBPL", "WERKS",
                   fks=(_client_fk(), _plant_fk())),
    "CRTX": _table("CRTX", "Text for the Work Center or Production Resource/Tool",
                   "MANDT*:CLNT", "OBJTY*", "OBJID*:NUMC", "SPRAS*:LANG", "KTEXT",
                   fks=(_client_fk(),)),
    "T001W": _table("T001W", "Plants/Branches", "MANDT*:CLNT", "WERKS*", "NAME1",
                    fks=(_client_fk(),)),
    "T000": _table("T000", "Clients", "MANDT*:CLNT", "MTEXT"),
    # not curated anywhere — its only edges are the dictionary's
    "XTAB": _table("XTAB", "A custom table", "MANDT*:CLNT", "XKEY*", "WERKS",
                   fks=(_client_fk(), _plant_fk(kind="KEY"))),
}


def _lookup(name: str) -> TableDef | None:
    return DICTIONARY.get(name)


def _graph(**kw) -> JoinGraph:
    return JoinGraph(CURATED_JOINS, lookup=_lookup, **kw)


def _route(edges) -> list[str]:
    """['AFRU', 'AUFK', 'AFPO', 'PRPS'] — the tables a chain of oriented edges visits."""
    return [edges[0].left, *(e.right for e in edges)] if edges else []


def _filter_ids(plan) -> set[tuple[str, str, str, str]]:
    return {(f.table, f.field, f.op, f.value) for f in plan.filters}


def _curated(left: str, right: str) -> JoinEdge:
    return next(e for e in CURATED_JOINS if (e.left, e.right) == (left, right))


# ----- edges -----

def test_a_curated_edge_supersedes_the_dictionary_edge_on_the_same_columns():
    g = _graph()
    to_mara = [e for e in g.edges_of("MAKT") if e.right == "MARA"]
    assert len(to_mara) == 1
    assert to_mara[0].kind == "text" and to_mara[0].on == (("MANDT", "MANDT"), ("MATNR", "MATNR"))


def test_the_dictionary_never_overrides_the_expert_whichever_arrives_first():
    g = JoinGraph(lookup=_lookup)
    g.add_table(DICTIONARY["MAKT"])                 # the fk edge MAKT → MARA lands first
    assert [e.kind for e in g.edges_of("MAKT") if e.right == "MARA"] == ["fk"]
    g.add_edge(_curated("MARA", "MAKT"))
    assert [e.kind for e in g.edges_of("MAKT") if e.right == "MARA"] == ["text"]
    g.add_table(DICTIONARY["MAKT"])                 # re-indexing the table changes nothing
    assert [e.kind for e in g.edges_of("MAKT") if e.right == "MARA"] == ["text"]


def test_a_verified_edge_replaces_its_unverified_twin():
    g = _graph()
    edge = _curated("AFRU", "AUFK")
    assert not edge.verified
    g.add_edge(replace(edge, verified=True))
    to_aufk = [e for e in g.edges_of("AFRU") if e.right == "AUFK"]
    assert len(to_aufk) == 1 and to_aufk[0].verified


def test_edges_of_orients_every_edge_with_the_table_on_the_left_and_curated_first():
    g = _graph()
    edges = g.edges_of("AUFK")
    assert edges and all(e.left == "AUFK" for e in edges)
    kinds = [e.kind for e in edges]
    assert "fk" in kinds and kinds.index("fk") > max(i for i, k in enumerate(kinds) if k != "fk")
    assert {"AFKO", "AFPO", "AFRU", "JEST", "T001W", "T000"} <= set(g.neighbours("AUFK"))


def test_a_dictionary_foreign_key_appears_through_lookup_for_a_table_nobody_curated():
    g = _graph()
    plant = [e for e in g.edges_of("XTAB") if e.right == "T001W"]
    assert len(plant) == 1
    assert plant[0].kind == "fk" and plant[0].cardinality == "n:1"
    assert plant[0].on == (("MANDT", "MANDT"), ("WERKS", "WERKS"))
    # and from the other side, once XTAB has been seen
    back = g.direct("T001W", "XTAB")
    assert len(back) == 1 and back[0].left == "T001W" and back[0].cardinality == "1:n"


def test_direct_returns_both_operation_to_confirmation_joins_compound_first():
    g = _graph()
    edges = g.direct("AFVC", "AFRU")
    assert len(edges) == 2
    assert edges[0].on == (("MANDT", "MANDT"), ("AUFPL", "AUFPL"), ("APLZL", "APLZL"))
    assert edges[1].on == (("MANDT", "MANDT"), ("RUECK", "RUECK"))
    # the same pair from the confirmation's side comes back re-oriented
    other_way = g.direct("AFRU", "AFVC")
    assert [e.left for e in other_way] == ["AFRU", "AFRU"] and other_way[0].cardinality == "n:1"


def test_reversed_edges_keep_their_compound_columns_and_filters():
    g = _graph()
    hops = g.path("CRHD", "AFRU")
    assert len(hops) == 1
    edge = hops[0]
    assert edge.left == "CRHD" and edge.right == "AFRU"
    assert edge.on == (("MANDT", "MANDT"), ("OBJID", "ARBID"))
    assert edge.cardinality == "1:n"
    assert [(f.table, f.field, f.value) for f in edge.filters] == [("CRHD", "OBJTY", "A")]


def test_table_names_are_normalized_including_the_edw_view_prefix():
    g = _graph()
    assert _route(g.path("tv_afru", " TV_AUFK ")) == ["AFRU", "AUFK"]
    assert g.direct("tv_afvc", "afru") == g.direct("AFVC", "AFRU")


# ----- paths -----

def test_the_path_from_a_confirmation_to_a_wbs_element_goes_through_the_order_and_its_item():
    g = _graph()
    hops = g.path("AFRU", "PRPS")
    assert _route(hops) == ["AFRU", "AUFK", "AFPO", "PRPS"]
    assert hops[-1].on == (("MANDT", "MANDT"), ("PROJN", "PSPNR"))
    assert "T001W" not in _route(hops) and "T000" not in _route(hops)


def test_a_check_table_two_tables_share_by_chance_is_never_a_shortcut_between_them():
    """With the whole dictionary shipped, AFRU and PRPS both validate a unit of measure against
    T006 (AFRU.GMEIN, PRPS.USE04): AFRU → T006 → PRPS is two hops that tie the real route through
    the order on cost — and would join a confirmation to every WBS element with the same unit.
    Leaving a check table backwards along a REF dependency fans out like that, so it costs more
    than the curated route around it; the check table itself stays one hop away from either
    side."""
    def unit_fk(field: str) -> FkRef:
        return FkRef(field, "T006", (("MANDT", "MANDT"), (field, "MSEHI")), kind="REF")

    dictionary = dict(DICTIONARY)
    dictionary["AFRU"] = replace(DICTIONARY["AFRU"],
                                 foreign_keys=(*DICTIONARY["AFRU"].foreign_keys, unit_fk("GMEIN")))
    dictionary["PRPS"] = replace(DICTIONARY["PRPS"],
                                 foreign_keys=(*DICTIONARY["PRPS"].foreign_keys, unit_fk("USE04")))
    dictionary["T006"] = _table("T006", "Units of Measurement", "MANDT*:CLNT", "MSEHI*",
                                fks=(_client_fk(),))
    g = JoinGraph(CURATED_JOINS, lookup=dictionary.get)
    assert _route(g.path("AFRU", "PRPS")) == ["AFRU", "AUFK", "AFPO", "PRPS"]
    assert _route(g.path("AFRU", "T006")) == ["AFRU", "T006"]
    assert _route(g.path("T006", "PRPS")) == ["T006", "PRPS"]
    # and in a plan, where the WBS element is joined onto what is already there
    assert [s.edge.right for s in g.plan(["AFRU", "PRPS"]).steps] == ["AUFK", "AFPO", "PRPS"]


def test_the_client_table_is_never_an_intermediate_hop_even_when_it_is_the_cheapest():
    # No curated edges: AFRU and PRPS share T000 (two KEY hops, 3.0) and T001W (two REF hops, 5.0
    # — the second walked backwards out of the check table).
    g = JoinGraph(lookup=_lookup)
    assert _route(g.path("AFRU", "PRPS")) == ["AFRU", "T001W", "PRPS"]
    # as an END, the client table is fine
    assert _route(g.path("AFRU", "T000")) == ["AFRU", "T000"]


def test_a_text_table_is_never_an_intermediate_hop():
    def edge(left, right, kind="curated"):
        return JoinEdge(left=left, right=right, on=(("K", "K"),), kind=kind)

    # TXT is a text table only because text edges point at it — no dictionary record at all
    g = JoinGraph((edge("A", "TXT", "text"), edge("B", "TXT", "text"),
                   edge("A", "C"), edge("C", "D"), edge("D", "B")))
    assert _route(g.path("A", "B")) == ["A", "C", "D", "B"]
    assert _route(g.path("A", "TXT")) == ["A", "TXT"]         # as an end it is reachable
    # LT is a text table only because the dictionary says so (a LANG key); the edges are plain
    lt = _table("LT", "texts", "MANDT*:CLNT", "K*", "SPRAS*:LANG", "TXT")
    plain = (edge("A", "LT"), edge("B", "LT"), edge("A", "C"), edge("C", "D"), edge("D", "B"))
    g2 = JoinGraph(plain, lookup=lambda n: lt if n == "LT" else None)
    assert _route(g2.path("A", "B")) == ["A", "C", "D", "B"]


def test_two_object_types_are_never_joined_through_the_status_table_they_share():
    # QMEL → JEST → PRPS would be QMEL.OBJNR = PRPS.OBJNR — a notification's status object is
    # never a WBS element's; that route is a hop shorter than the real one and must still lose.
    g = _graph()
    assert _route(g.path("QMEL", "PRPS")) == ["QMEL", "AUFK", "AFPO", "PRPS"]
    # but the status table IS the way to its own texts (JEST is left on a different column)
    assert _route(g.path("AFVC", "TJ02T")) == ["AFVC", "JEST", "TJ02T"]


def test_the_hop_cap_is_exact_not_a_cut_off_after_the_fact():
    g = _graph()
    assert g.path("AFRU", "PRPS", max_hops=1) == ()
    # under a cap of 2 the dearer 2-hop route (a usually-blank WBS field) is the answer …
    short = g.path("AFRU", "PRPS", max_hops=2)
    assert len(short) == 2 and short[-1].on[-1][1] == "PSPNR"
    # … and with room for 3 the cheaper route through the item wins
    assert _route(g.path("AFRU", "PRPS", max_hops=3)) == ["AFRU", "AUFK", "AFPO", "PRPS"]
    assert g.path("AFRU", "AFRU") == () and g.path("AFRU", "NOSUCH") == ()


def test_the_cheapest_route_to_a_status_text_is_order_status_text():
    g = _graph()
    hops = g.path("AUFK", "TJ02T")
    assert _route(hops) == ["AUFK", "JEST", "TJ02T"]
    assert hops[1].on == (("STAT", "ISTAT"),) and hops[1].kind == "text"


# ----- plans -----

def test_a_plan_for_work_center_texts_carries_objty_and_the_language_filter():
    g = _graph()
    plan = g.plan(["AFRU", "CRHD", "CRTX"])
    assert plan.root == "AFRU" and plan.unreachable == ()
    assert [(s.alias_left, s.alias_right) for s in plan.steps] == [
        ("AFRU", "CRHD"), ("CRHD", "CRTX")]
    assert plan.tables == ("AFRU", "CRHD", "CRTX")
    ids = _filter_ids(plan)
    assert ("CRHD", "OBJTY", "=", "A") in ids
    assert ("CRTX", "SPRAS", "=", "E") in ids
    assert ("AFRU", "STOKZ", "=", "") in ids
    # the edge's OBJTY filter and CRHD's standard filter are the same condition, said once
    assert sum(1 for f in plan.filters if (f.table, f.field) == ("CRHD", "OBJTY")) == 1
    lang = next(f for f in plan.filters if f.table == "CRTX")
    assert lang.why == "one language" and not lang.optional


def test_a_plan_for_order_status_texts_joins_jest_then_tj02t_without_mandt():
    g = _graph()
    plan = g.plan(["AUFK", "TJ02T"])
    assert [(s.edge.left, s.edge.right) for s in plan.steps] == [
        ("AUFK", "JEST"), ("JEST", "TJ02T")]
    assert plan.steps[1].edge.on == (("STAT", "ISTAT"),)
    ids = _filter_ids(plan)
    assert ("JEST", "INACT", "=", "") in ids
    assert ("TJ02T", "SPRAS", "=", "E") in ids
    assert ("AUFK", "AUTYP", "=", "10") in ids
    assert next(f for f in plan.filters if f.field == "AUTYP").optional
    assert not next(f for f in plan.filters if f.field == "INACT").optional


def test_a_plan_grows_from_whichever_reached_table_is_nearest():
    g = _graph()
    plan = g.plan(["AFRU", "PRPS", "MAKT", "CRHD"])
    assert plan.unreachable == ()
    joined = {(s.alias_left, s.alias_right) for s in plan.steps}
    assert ("AFPO", "MAKT") in joined      # from the item already in the plan, not a new route
    assert ("AFRU", "CRHD") in joined
    assert plan.tables == ("AFRU", "AUFK", "AFPO", "PRPS", "MAKT", "CRHD")


def test_a_plan_does_not_join_the_next_table_through_a_status_table_it_already_holds():
    # JEST is in the plan (for the order's statuses); PRPS must still come through AFPO, not by
    # JEST.OBJNR = PRPS.OBJNR from the JEST already there.
    g = _graph()
    plan = g.plan(["AUFK", "JEST", "PRPS"])
    assert [(s.alias_left, s.alias_right) for s in plan.steps] == [
        ("AUFK", "JEST"), ("AUFK", "AFPO"), ("AFPO", "PRPS")]


def test_every_step_joins_onto_an_alias_already_present():
    g = _graph()
    wanted = ["AFRU", "AFVC", "AUFK", "AFKO", "AFPO", "PRPS", "PROJ", "CRHD", "CRTX", "MAKT",
              "JEST", "TJ02T"]
    plan = g.plan(wanted)
    assert plan.unreachable == ()
    present = [plan.root]
    for step in plan.steps:
        assert step.alias_left in present, f"{step.alias_right} joined onto {step.alias_left}"
        assert step.alias_right not in present
        present.append(step.alias_right)
    assert set(plan.tables) >= set(wanted)


def test_the_root_leads_even_when_named_later_and_an_explicit_root_is_added():
    g = _graph()
    plan = g.plan(["AFRU", "AUFK"], root="AUFK")
    assert plan.root == "AUFK" and plan.tables == ("AUFK", "AFRU")
    plan = g.plan(["CRHD"], root="AFRU")
    assert plan.root == "AFRU" and plan.tables == ("AFRU", "CRHD")
    empty = g.plan([])
    assert empty.root == "" and empty.steps == () and empty.notes


def test_a_repeated_table_gets_a_numbered_alias_by_a_different_join():
    g = _graph()
    plan = g.plan(["AUFK", "AFPO", "PRPS", "COBRB", "PRPS"])
    assert plan.unreachable == ()
    assert plan.aliases == (("AUFK", "AUFK"), ("AFPO", "AFPO"), ("PRPS", "PRPS"),
                            ("COBRB", "COBRB"), ("PRPS2", "PRPS"))
    second = plan.steps[-1]
    assert second.alias_right == "PRPS2" and second.alias_left == "COBRB"
    assert second.edge.on == (("MANDT", "MANDT"), ("PS_PSP_PNR", "PSPNR"))
    assert ("COBRB", "KONTY", "=", "PSP") in _filter_ids(plan)
    assert plan.alias_of("PRPS") == "PRPS"


def test_an_unreachable_table_is_reported_with_its_known_joins():
    g = _graph()
    plan = g.plan(["AFRU", "NOSUCH"])
    assert plan.unreachable == ("NOSUCH",)
    assert any(n.startswith("NOSUCH:") and "no known joins" in n for n in plan.notes)
    # an island: curated among themselves, never bridged to the plan
    g.add_edge(JoinEdge(left="ISLA", right="ISLB", on=(("K", "K"),), kind="curated"))
    plan = g.plan(["AFRU", "ISLB"])
    assert plan.unreachable == ("ISLB",)
    note = next(n for n in plan.notes if n.startswith("ISLB:"))
    assert "ISLB joins ISLA" in note and "from AFRU" in note
    # a repeat with no other table to join it from
    plan = g.plan(["AUFK", "AUFK"])
    assert plan.unreachable == ("AUFK",) and any("a second AUFK" in n for n in plan.notes)


def test_the_callers_standard_filters_are_used_as_given():
    g = _graph()
    plan = g.plan(["AFRU", "AUFK"], standard_filters={})
    assert ("AUFK", "AUTYP", "=", "10") not in _filter_ids(plan)
    mine = {"AUFK": (Filter("AUFK", "WERKS", "=", "1006", "my plant", optional=True),)}
    plan = g.plan(["AFRU", "AUFK"], standard_filters=mine)
    assert ("AUFK", "WERKS", "=", "1006") in _filter_ids(plan)
    assert STANDARD_FILTERS["AUFK"][0] not in plan.filters


def test_a_language_filter_is_assumed_for_a_curated_text_table_the_dictionary_lacks():
    g = _graph()
    plan = g.plan(["MDTB", "T458B"])         # T458B is curated as a text table; no TableDef here
    lang = next(f for f in plan.filters if f.table == "T458B")
    assert (lang.field, lang.op, lang.value) == ("SPRAS", "=", "E")
    assert "assumed" in lang.why
    # and with another language
    plan = _graph(language="D").plan(["AFRU", "CRHD", "CRTX"])
    assert ("CRTX", "SPRAS", "=", "D") in _filter_ids(plan)


def test_a_plan_says_which_curated_joins_are_unverified():
    g = _graph()
    plan = g.plan(["AFRU", "AUFK"])
    assert any("unverified" in n and "AFRU → AUFK" in n for n in plan.notes)
    g.add_edge(replace(_curated("AFRU", "AUFK"), verified=True))
    assert not any("unverified" in n for n in g.plan(["AFRU", "AUFK"]).notes)


def test_a_reading_catalog_that_raises_leaves_the_table_without_dictionary_edges():
    def flaky(name: str) -> TableDef | None:
        if name == "XTAB":
            raise OSError("catalog file locked")
        return DICTIONARY.get(name)

    g = JoinGraph(CURATED_JOINS, lookup=flaky)
    assert g.edges_of("XTAB") == ()
    assert _route(g.path("AFRU", "AUFK")) == ["AFRU", "AUFK"]


# ----- explain -----

def test_explain_renders_one_line_per_hop_then_the_filters_with_reasons():
    g = _graph()
    plan = g.plan(["AFRU", "AUFK", "JEST", "TJ02T"])
    text = g.explain(plan)
    lines = text.splitlines()
    assert lines[0] == "FROM AFRU"
    assert lines[1] == ("AFRU → AUFK on MANDT, AUFNR (n:1, curated, unverified) — the order a "
                        "confirmation belongs to (AFRU carries the order number)")
    assert lines[3].startswith("JEST → TJ02T on STAT = ISTAT (n:1, text, unverified) — ")
    assert "Filters:" in lines
    assert any(line.startswith("  AUFK.AUTYP = '10' — production orders only")
               and line.endswith("(optional)") for line in lines)
    assert any(line.startswith("  JEST.INACT = '' — active statuses only")
               and "(optional)" not in line for line in lines)
    assert "  TJ02T.SPRAS = 'E' — one language" in lines
    assert "Unreachable" not in text


def test_explain_names_the_dictionary_and_the_unreachable():
    g = _graph()
    plan = g.plan(["XTAB", "T001W", "NOSUCH"])
    text = g.explain(plan)
    assert ("XTAB → T001W on MANDT, WERKS (n:1, dictionary) — declared foreign key on "
            "XTAB.WERKS (KEY)") in text
    assert "Unreachable: NOSUCH" in text and "Notes:" in text
    assert g.explain(g.plan(["AFRU"])).startswith("FROM AFRU (no joins)")
