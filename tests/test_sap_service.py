"""SapService (helix/services/sap.py): the faculty behind the five sap_* tools and the SAP panel,
on a hand-built shipped catalog (ten TableDefs as json.gz + index.json in a tmp dir), a fake
leanx fetcher, a frozen clock and a JsonSettings for the EDW record — no network, no Container.

What is pinned: a table summary leads with the key, the compound join and the provenance; an
unknown table is fetched exactly once and the miss is a plain sentence (or the fetch-off
sentence); a join from a confirmation to its WBS element goes through the order item; the SQL
names the EDW views and keeps compound ON clauses; a pasted column list marks the table present
with its custom columns and warns about columns the EDW lacks; the per-turn block fires only when
a table or a business term is named; the WIP report lists all 27 columns with its query; and
nothing raises on garbage.
"""
from __future__ import annotations

import gzip
import json
import logging
import shutil
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from helix.adapters.json_settings import JsonSettings
from helix.adapters.sap_catalog import CatalogStore
from helix.domain.sap.curated import CURATED_JOINS, WIP_REPORT
from helix.domain.sap.model import FieldDef, FkRef, JoinEdge, TableDef, normalize_table
from helix.services import sap as sap_module
from helix.services.sap import (
    EDW_KEY,
    FETCH_SETTING,
    FOR_TURN_CHARS,
    TABLE_TEXT_MAX,
    WIDE_REST_MAX,
    SapService,
)

# ----- fixtures: a small dictionary with the real keys and one declared foreign key each -----


def _fields(*specs: str) -> tuple[FieldDef, ...]:
    """'MANDT*:CLNT:3=Client' → key field, type CLNT, length 3, description Client."""
    out = []
    for i, spec in enumerate(specs):
        head, _, description = spec.partition("=")
        name, _, rest = head.partition(":")
        dtype, _, length = rest.partition(":")
        out.append(FieldDef(name=name.rstrip("*"), key=name.endswith("*"),
                            data_type=dtype or "CHAR", length=int(length or 10),
                            description=description, position=i + 1))
    return tuple(out)


def _table(name: str, description: str, *fields: str, fk: FkRef | None = None,
           module: str = "PP", component: str = "") -> TableDef:
    return TableDef(name=name, description=description, module=module, component=component,
                    fields=_fields(*fields), foreign_keys=(fk,) if fk else (), source="leanx",
                    source_url=f"https://leanx.eu/sap/table/{name.lower()}/",
                    fetched_at="2026-09-01")


def _client_fk() -> FkRef:
    return FkRef(field="MANDT", check_table="T000", columns=(("MANDT", "MANDT"),), kind="KEY")


AFRU = _table(
    "AFRU", "Order Confirmations",
    "MANDT*:CLNT:3=Client", "RUECK*:NUMC:10=Completion confirmation number",
    "RMZHL*:NUMC:8=Confirmation counter", "AUFNR:CHAR:12=Order number",
    "AUFPL:NUMC:10=Routing number of operations", "APLZL:NUMC:8=General counter for order",
    "VORNR:CHAR:4=Operation number", "ARBID:NUMC:8=Object ID of the resource",
    "WERKS:CHAR:4=Plant", "BUDAT:DATS:8=Posting date", "ERSDA:DATS:8=Entry date",
    "ERZET:TIMS:6=Entry time", "ISDD:DATS:8=Actual start date", "LMNGA:QUAN:13=Yield",
    "XMNGA:QUAN:13=Scrap", "ISMNW:QUAN:7=Actual work", "STOKZ:CHAR:1=Reversed",
    "STZHL:NUMC:8=Reversal counter", "AUERU:CHAR:1=Final confirmation",
    "PERNR:NUMC:8=Personnel number", "LTXA1:CHAR:40=Confirmation text",
    fk=FkRef(field="APLZL", check_table="AFVC", kind="KEY",
             columns=(("MANDT", "MANDT"), ("AUFPL", "AUFPL"), ("APLZL", "APLZL"))),
    component="PP-SFC-EXE-CON",
)
AFVC = _table(
    "AFVC", "Operation within an order",
    "MANDT*:CLNT:3=Client", "AUFPL*:NUMC:10=Routing number of operations",
    "APLZL*:NUMC:8=General counter for order", "VORNR:CHAR:4=Operation number",
    "LTXA1:CHAR:40=Operation short text", "ARBID:NUMC:8=Object ID of the resource",
    "STEUS:CHAR:4=Control key", "RUECK:NUMC:10=Completion confirmation number",
    "OBJNR:CHAR:22=Object number", "PROJN:NUMC:8=WBS element", "WERKS:CHAR:4=Plant",
    "LOEKZ:CHAR:1=Deletion flag",
    fk=_client_fk(),
)
AUFK = _table(
    "AUFK", "Order master data",
    "MANDT*:CLNT:3=Client", "AUFNR*:CHAR:12=Order number", "AUART:CHAR:4=Order type",
    "AUTYP:NUMC:2=Order category", "WERKS:CHAR:4=Plant", "KTEXT:CHAR:40=Description",
    "OBJNR:CHAR:22=Object number", "PSPEL:NUMC:8=WBS element", "LOEKZ:CHAR:1=Deletion flag",
    "ERDAT:DATS:8=Created on", "KOSTV:CHAR:10=Responsible cost center",
    fk=FkRef(field="WERKS", check_table="T001W", kind="REF",
             columns=(("MANDT", "MANDT"), ("WERKS", "WERKS"))),
    module="CO",
)
AFPO = _table(
    "AFPO", "Order item",
    "MANDT*:CLNT:3=Client", "AUFNR*:CHAR:12=Order number", "POSNR*:NUMC:4=Order item number",
    "MATNR:CHAR:18=Material number", "DWERK:CHAR:4=Plant", "PROJN:NUMC:8=WBS element",
    "CHARG:CHAR:10=Batch", "PSMNG:QUAN:13=Order item quantity", "WEMNG:QUAN:13=Delivered",
    "KDAUF:CHAR:10=Sales order", "KDPOS:NUMC:6=Sales order item", "PLNUM:CHAR:10=Planned order",
    fk=FkRef(field="MATNR", check_table="MARA", kind="REF",
             columns=(("MANDT", "MANDT"), ("MATNR", "MATNR"))),
)
PRPS = _table(
    "PRPS", "WBS (Work Breakdown Structure) Element Master Data",
    "MANDT*:CLNT:3=Client", "PSPNR*:NUMC:8=WBS element", "POSID:CHAR:24=WBS element id",
    "POST1:CHAR:40=Description", "PSPHI:NUMC:8=Project definition",
    "OBJNR:CHAR:22=Object number", "WERKS:CHAR:4=Plant", "LOEVM:CHAR:1=Deletion flag",
    fk=_client_fk(), module="PS",
)
MAKT = _table(
    "MAKT", "Material Descriptions",
    "MANDT*:CLNT:3=Client", "MATNR*:CHAR:18=Material number", "SPRAS*:LANG:1=Language key",
    "MAKTX:CHAR:40=Material description",
    fk=FkRef(field="MATNR", check_table="MARA", kind="KEY",
             columns=(("MANDT", "MANDT"), ("MATNR", "MATNR"))),
    module="MM",
)
CRHD = _table(
    "CRHD", "Work Center Header",
    "MANDT*:CLNT:3=Client", "OBJTY*:CHAR:2=Object type", "OBJID*:NUMC:8=Object ID",
    "ARBPL:CHAR:8=Work center", "WERKS:CHAR:4=Plant",
    fk=FkRef(field="WERKS", check_table="T001W", kind="REF",
             columns=(("MANDT", "MANDT"), ("WERKS", "WERKS"))),
)
CRTX = _table(
    "CRTX", "Text for the Work Center",
    "MANDT*:CLNT:3=Client", "OBJTY*:CHAR:2=Object type", "OBJID*:NUMC:8=Object ID",
    "SPRAS*:LANG:1=Language key", "KTEXT:CHAR:40=Short description",
    fk=FkRef(field="OBJID", check_table="CRHD", kind="KEY",
             columns=(("MANDT", "MANDT"), ("OBJTY", "OBJTY"), ("OBJID", "OBJID"))),
)
JEST = _table(
    "JEST", "Individual Object Status",
    "MANDT*:CLNT:3=Client", "OBJNR*:CHAR:22=Object number", "STAT*:CHAR:5=Object status",
    "INACT:CHAR:1=Status inactive",
    fk=_client_fk(), module="CA",
)
TJ02T = _table(
    "TJ02T", "System status texts",
    "ISTAT*:CHAR:5=System status", "SPRAS*:LANG:1=Language key", "TXT04:CHAR:4=Short text",
    "TXT30:CHAR:30=Long text",
    fk=FkRef(field="SPRAS", check_table="T002", kind="KEY", columns=(("SPRAS", "SPRAS"),)),
    module="CA",
)
SHIPPED = (AFRU, AFVC, AUFK, AFPO, PRPS, MAKT, CRHD, CRTX, JEST, TJ02T)

# A table the fake leanx can serve on demand (not shipped).
AFKO = _table(
    "AFKO", "Order header data PP orders",
    "MANDT*:CLNT:3=Client", "AUFNR*:CHAR:12=Order number", "AUFPL:NUMC:10=Routing number",
    "RSNUM:NUMC:10=Reservation", "PLNBEZ:CHAR:18=Material", "DISPO:CHAR:3=MRP controller",
    "FTRMI:DATS:8=Actual release date", "GLTRP:DATS:8=Basic finish date",
    fk=_client_fk(),
)


def _write_gz(path: Path, table: TableDef) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(table.to_dict(), fh)


def _shipped(root: Path, *tables: TableDef) -> Path:
    (root / "tables").mkdir(parents=True, exist_ok=True)
    for t in tables:
        _write_gz(root / "tables" / f"{t.name}.json.gz", t)
    rows = [{"name": t.name, "description": t.description, "module": t.module,
             "component": t.component, "keys": list(t.primary_key()), "fields": len(t.fields),
             "source": t.source} for t in tables]
    (root / "index.json").write_text(
        json.dumps({"v": 1, "built_at": "2026-09-14", "tables": rows}), "utf-8")
    return root


class _Clock:
    def __init__(self, at: datetime = datetime(2026, 9, 15, 10, 30)) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


class _Fetcher:
    """Stands in for CatalogStore.fetch_table: serves the pages it was given (writing them to
    layer 1 as the real fetcher does) and records every call."""

    def __init__(self, pages: dict[str, TableDef] | None = None) -> None:
        self.pages = dict(pages or {})
        self.calls: list[tuple[str, str]] = []

    def __call__(self, store: CatalogStore, name: str, *, today: str = "") -> TableDef | None:
        n = normalize_table(name)
        self.calls.append((n, today))
        table = self.pages.get(n)
        if table is None:
            return None
        table = replace(table, fetched_at=today or table.fetched_at)
        store.put(table)
        return table


@pytest.fixture
def fetcher(monkeypatch) -> _Fetcher:
    fake = _Fetcher()

    def fetch_table(store, name, *, today=""):     # a function, so it binds like the real method
        return fake(store, name, today=today)

    monkeypatch.setattr(CatalogStore, "fetch_table", fetch_table)
    return fake


@pytest.fixture
def parts(tmp_path, fetcher):
    """(service, clock, settings, edw path) on a fresh shipped catalog in tmp_path."""
    shipped = _shipped(tmp_path / "shipped", *SHIPPED)
    catalog = CatalogStore(tmp_path / "data", shipped=shipped)
    clock = _Clock()
    settings = JsonSettings(tmp_path / "settings.json")
    edw_path = tmp_path / "helix_sap_edw.json"
    svc = SapService(catalog, JsonSettings(edw_path), clock, settings=settings)
    return svc, clock, settings, edw_path


@pytest.fixture
def svc(parts) -> SapService:
    return parts[0]


# ----- table_text -----

def test_a_table_summary_leads_with_the_key_the_compound_join_and_the_provenance(svc, fetcher):
    text = svc.table_text("tv_afru")
    lines = text.splitlines()
    assert lines[0] == ("AFRU — Order Confirmations (PP/PP-SFC-EXE-CON). Source: SAP data "
                        "dictionary via leanx.eu, read 2026-09-01.")
    assert lines[1] == "Key: MANDT + RUECK + RMZHL"
    assert lines[2] == "EDW: TV_AFRU not recorded"
    assert lines[3].startswith("About: Confirmations — every time work is booked")
    # the compound key, verified against the fixture dictionary; the direction is AFRU's
    assert ("  AFRU → AFVC on MANDT, AUFPL, APLZL (n:1, verified) — all confirmations posted on "
            "the operation — BOTH columns, never AUFPL alone") in text
    # a curated join to a table the catalog does not hold stays unverified and says so
    assert "  AFRU → AFKO on MANDT, AUFNR (n:1, curated, unverified)" in text
    assert "  AFRU → CRHD on MANDT, ARBID = OBJID (n:1, verified)" in text
    assert "[needs CRHD.OBJTY = 'A']" in text
    assert "  AFRU.STOKZ = '' — leave out reversal documents (STOKZ='X' is a reversal) " \
           "(optional)" in text
    assert "Recipes:" not in text                 # AFRU is a recipe's source, not a join base
    afvc = svc.table_text("AFVC")
    assert "Recipes: system_status (on OBJNR — All active system statuses of an object on one " \
           "row (e.g. 'REL PCNF MACM'))" in afvc
    assert "latest_confirmation (on AUFPL+APLZL — The most recent valid confirmation per " \
           "operation)" in afvc
    assert "Fields (21):" in text
    assert "  RUECK (key)  NUMC(10)  Completion confirmation number — Confirmation number" in text
    assert "  BUDAT  DATS(8)  Posting date — Posting date of the confirmation." in text
    assert "MANDT → T000" not in text and "T000" not in text   # the client check is never a join
    assert text.index("Key:") < text.index("Joins:") < text.index("Fields (")
    assert len(text) <= TABLE_TEXT_MAX
    assert fetcher.calls == []                    # a shipped table never triggers a fetch


def test_the_fields_section_pages_through_the_dictionary(svc, monkeypatch):
    monkeypatch.setattr(sap_module, "FIELDS_PER_PAGE", 5)
    page = svc.table_text("AFRU", section="fields", offset=5)
    assert page.startswith("AFRU fields — showing 6-10 of 21; ask for offset 10 for the next page. "
                           "Source: SAP data dictionary via leanx.eu, read 2026-09-01.")
    assert "  APLZL  NUMC(8)  General counter for order" in page
    assert "  RUECK (key)" not in page
    last = svc.table_text("AFRU", section="fields", offset=20)
    assert "showing 21-21 of 21." in last and "ask for offset" not in last
    assert svc.table_text("AFRU", section="fields", offset=99) == \
        "AFRU has 21 fields; offset 99 is past the end (the last page starts at 20)."
    # code values ride along on the fields page
    assert "STOKZ  CHAR(1)  Reversed — 'X' = this row IS a reversal document. [values: '' normal " \
           "confirmation, 'X' reversal document]" in \
        svc.table_text("AFRU", section="fields", offset=15)


def test_the_joins_section_lists_every_edge_with_its_filters(svc):
    text = svc.table_text("JEST", section="joins")
    assert text.startswith("JEST (key MANDT + OBJNR + STAT) —")
    assert "  JEST → TJ02T on STAT = ISTAT (n:1, verified)" in text
    assert "  JEST → AUFK on MANDT, OBJNR (n:1, verified)" in text
    assert "      needs JEST.INACT = '' — active statuses only" in text
    assert "Recipes that join here" not in text          # JEST is a recipe's source, not a base
    summary = svc.table_text("TJ02T")
    assert "Language key: SPRAS — filter SPRAS = 'E' (one character; 'EN' returns nothing)" \
        in summary
    assert "  TJ02T.SPRAS = 'E' — one language" in summary


def test_an_unknown_table_is_fetched_once_and_the_miss_is_a_plain_sentence(svc, fetcher):
    text = svc.table_text("MDTB")
    assert text.startswith("MDTB isn't in the SAP dictionary source (leanx.eu) — is it a custom "
                           "(Z) table? Paste its columns and I'll record it.")
    assert fetcher.calls == [("MDTB", "2026-09-15")]
    assert svc.table_dict("MDTB", fetch=False) is None and len(fetcher.calls) == 1


def test_a_fetched_table_lands_in_the_catalog_and_verifies_the_joins_that_touch_it(parts, fetcher):
    svc, _clock, _settings, _edw = parts
    fetcher.pages["AFKO"] = AFKO
    assert "AFRU → AFKO on MANDT, AUFNR (n:1, curated, unverified)" in svc.table_text("AFRU")
    text = svc.table_text("afko")
    assert text.startswith("AFKO — Order header data PP orders (PP). Source: SAP data dictionary "
                           "via leanx.eu, read 2026-09-15.")
    assert "Key: MANDT + AUFNR" in text
    assert fetcher.calls == [("AFKO", "2026-09-15")]
    assert "AFRU → AFKO on MANDT, AUFNR (n:1, verified)" in svc.table_text("AFRU")
    assert svc.status_dict()["extensions"] == 1
    plan = svc.join_dict(["AFRU", "AFKO"])
    assert [(s["left"], s["right"], s["verified"]) for s in plan["plan"]] == \
        [("AFRU", "AFKO", True)]
    assert len(fetcher.calls) == 1                # in the catalog now: never fetched again


def test_fetching_off_says_so_and_never_calls_the_fetcher(parts, fetcher):
    svc, _clock, settings, _edw = parts
    settings.set(FETCH_SETTING, False)
    assert svc.table_text("MDTB") == (
        "MDTB isn't in my catalog and on-demand fetching is off (setting sap_fetch_tables). "
        "Turn it on, or paste the table's columns and I'll record them.")
    assert fetcher.calls == [] and svc.status_dict()["fetch_allowed"] is False
    settings.set(FETCH_SETTING, "true")           # read live: a toggle takes effect at once
    svc.table_text("MDTB")
    assert fetcher.calls == [("MDTB", "2026-09-15")]


def test_an_edw_only_table_is_described_from_the_recorded_columns(svc, fetcher):
    svc.edw_text("record_columns", table="ZZWIP", text="MANDT, AUFNR, ZZ_RATING, ZZ_NOTES")
    text = svc.table_text("ZZWIP")
    assert text.startswith("ZZWIP — not in the SAP dictionary — columns as your EDW lists them. "
                           "Source: your EDW column list (pasted 2026-09-15).")
    assert "Key: unknown — recorded from your EDW column list" in text
    assert "EDW: TV_ZZWIP present, 4 columns recorded" in text
    # leanx was asked before recording (to tell custom columns apart) and again on the read —
    # the real fetcher remembers a miss for the session, so the second never leaves the store
    assert fetcher.calls == [("ZZWIP", "2026-09-15"), ("ZZWIP", "2026-09-15")]


# ----- join_text / sql_text -----

def test_a_confirmation_reaches_its_wbs_element_through_the_order_item(svc):
    text = svc.join_text(["AFRU", "PRPS"])
    assert text.startswith("Join plan for AFRU, PRPS — FROM AFRU")
    assert "  AFRU → AUFK ON AFRU.MANDT = AUFK.MANDT AND AFRU.AUFNR = AUFK.AUFNR " \
           "(n:1, verified)" in text
    assert "  AUFK → AFPO ON AUFK.MANDT = AFPO.MANDT AND AUFK.AUFNR = AFPO.AUFNR " \
           "(1:n, verified)" in text
    assert ("  AFPO → PRPS ON AFPO.MANDT = PRPS.MANDT AND AFPO.PROJN = PRPS.PSPNR (n:1, verified)"
            " — the WBS element of the order item") in text
    assert "  PRPS.LOEVM = '' — WBS elements not flagged for deletion (optional)" in text
    assert "SQL skeleton:\nSELECT AFRU.*\nFROM EDW.SRC_SAPECC_ARP.TV_AFRU AS AFRU\n" in text
    assert "  ON PRPS.MANDT = AFPO.MANDT AND PRPS.PSPNR = AFPO.PROJN\n" in text
    assert "WHERE AFRU.WERKS = '1006'  -- plant (optional)" in text
    assert "EDW: TV_AFRU not recorded; TV_AUFK not recorded; TV_AFPO not recorded; " \
           "TV_PRPS not recorded" in text


def test_join_text_names_the_unreachable_and_the_root_can_be_chosen(svc):
    text = svc.join_text(["AFVC", "AFRU", "ZZNOPE"], root="AFRU")
    assert text.startswith("Join plan for AFVC, AFRU, ZZNOPE — FROM AFRU")
    assert "  AFRU → AFVC ON AFRU.MANDT = AFVC.MANDT AND AFRU.AUFPL = AFVC.AUFPL AND " \
           "AFRU.APLZL = AFVC.APLZL (n:1, verified)" in text
    assert "Unreachable: ZZNOPE" in text
    assert "ZZNOPE could not be joined into this query — it is left out" in text
    assert "ZZNOPE: no join path from AFRU, AFVC within 6 hops; ZZNOPE has no known joins" in text
    # a curated table the catalog does not hold is still reachable — through the expert's edges
    mrp = svc.join_text(["AUFK", "MDTB"])
    assert "  MDKP → MDTB ON MDKP.MANDT = MDTB.MANDT AND MDKP.DTNUM = MDTB.DTNUM " \
           "(1:n, curated, unverified)" in mrp
    assert svc.join_text([]) == "Which tables should I join? Give me two or more names."
    assert "FROM AFRU (no joins)" in svc.join_text("AFRU")


def test_sql_text_writes_the_edw_view_names_and_the_compound_on_clause(svc):
    columns = [{"table": "AFRU", "field": "budat"}, {"table": "AFVC", "field": "VORNR"},
               {"table": "AFRU", "field": "LMNGA", "expression": "SUM({})", "alias": "YIELD"}]
    since = [{"table": "AFRU", "field": "BUDAT", "op": ">=", "value": "20260101"}]
    text = svc.sql_text(["AFRU", "AFVC"], columns, filters=since)
    sql = text.split("\nWarnings:")[0]
    assert sql.startswith("SELECT TO_DATE(NULLIF(AFRU.BUDAT, '00000000'), 'YYYYMMDD') AS BUDAT,\n"
                          "       AFVC.VORNR,\n       SUM(AFRU.LMNGA) AS YIELD\n"
                          "FROM EDW.SRC_SAPECC_ARP.TV_AFRU AS AFRU\n"
                          "LEFT JOIN EDW.SRC_SAPECC_ARP.TV_AFVC AS AFVC\n"
                          "  ON AFVC.MANDT = AFRU.MANDT AND AFVC.AUFPL = AFRU.AUFPL "
                          "AND AFVC.APLZL = AFRU.APLZL\n"
                          "WHERE AFRU.WERKS = '1006'  -- plant (optional)\n")
    assert "  AND AFRU.BUDAT >= '20260101'\nLIMIT 100;" in sql
    assert "  AFRU: no column list recorded — I can't confirm its columns exist in the EDW" in text
    assert svc.sql_text([]) == "Which tables should the query read? Give me at least one name."
    # a field the dictionary lacks is the guessed-name bug, and is warned about
    guessed = svc.sql_text(["AFRU"], [{"table": "AFRU", "field": "BUZEIT"}])
    assert "AFRU.BUZEIT is not a field of AFRU in the dictionary" in guessed


def test_a_recipe_rides_along_as_a_cte(svc):
    text = svc.sql_text(["AFVC", "AFRU"], [{"table": "AFVC", "field": "VORNR"},
                                           {"table": "last_conf", "field": "LAST_WORKED"}],
                        recipes=["latest_confirmation"], limit=5)
    assert text.startswith("WITH last_conf AS (\n")
    assert "LEFT JOIN last_conf ON last_conf.AUFPL = AFVC.AUFPL AND " \
           "last_conf.APLZL = AFVC.APLZL" in text
    assert "last_conf.LAST_WORKED" in text and "LIMIT 5;" in text


# ----- the EDW record -----

PASTE = ("MANDT\nRUECK\nRMZHL\nAUFNR\nAUFPL\nAPLZL\nBUDAT\nWERKS\nSTOKZ\nSTZHL\n"
         "ZZ_SHIFT\nZZ_OPERATOR\n")


def test_recording_a_column_list_marks_the_table_present_with_its_custom_columns(parts):
    svc, _clock, _settings, edw_path = parts
    msg = svc.edw_text("record_columns", table="afru", text=PASTE)
    assert msg.startswith("Recorded 12 columns for TV_AFRU (2 custom: ZZ_SHIFT, ZZ_OPERATOR). "
                          "11 dictionary fields the EDW lacks: VORNR, ARBID, ERSDA, ERZET, ISDD, "
                          "LMNGA, XMNGA, ISMNW, AUERU, PERNR")
    text = svc.table_text("AFRU")
    assert ("EDW: TV_AFRU present, 12 columns recorded (custom: ZZ_SHIFT, ZZ_OPERATOR); "
            "11 dictionary fields the EDW lacks: VORNR, ARBID, ERSDA, ERZET, ISDD, LMNGA, "
            "… (5 more)") in text
    assert "  RUECK (key)  NUMC(10)  Completion confirmation number  [EDW ✓]" in text
    assert "  BUDAT  DATS(8)  Posting date  [EDW ✓]" in text
    # the record is on disk under its key, and a reopened service reads it back
    raw = json.loads(edw_path.read_text("utf-8"))
    assert raw[EDW_KEY]["tables"]["AFRU"]["columns"][-1] == "ZZ_OPERATOR"
    assert raw[EDW_KEY]["tables"]["AFRU"]["recorded_at"] == "2026-09-15"
    reopened = SapService(svc._catalog, JsonSettings(edw_path), _Clock())
    assert reopened.overlay().status("AFRU") == "present" and reopened.naming().plant == "1006"
    # the writer now confirms columns — and names the one the EDW lacks
    sql = svc.sql_text(["AFRU"], [{"table": "AFRU", "field": "RUECK"},
                                  {"table": "AFRU", "field": "LMNGA"}])
    assert "AFRU.LMNGA is not in the column list you gave for TV_AFRU" in sql
    assert "AFRU.RUECK" not in sql.split("Warnings:")[1]
    # a bad paste never erases the good list
    assert svc.edw_text("record_columns", table="AFRU", text="12 34 (0.4s)").startswith(
        "No column names found in what you pasted for AFRU — nothing recorded")
    assert svc.overlay().columns("AFRU")[0] == "MANDT"


def test_an_information_schema_export_records_every_table_it_names(svc):
    export = ("TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION\n"
              "TV_AFVC,MANDT,1\nTV_AFVC,AUFPL,2\nTV_AFVC,APLZL,3\nTV_AFVC,ZZ_CELL,4\n"
              "TV_JEST,MANDT,1\nTV_JEST,OBJNR,2\nTV_JEST,STAT,3\nTV_JEST,INACT,4\n")
    assert svc.edw_text("information_schema", text=export) == (
        "Recorded 2 tables from the INFORMATION_SCHEMA export: AFVC (4 columns, 1 custom), "
        "JEST (4 columns).")
    edw = svc.edw_dict()
    rows = [(t["name"], t["status"], t["columns"], t["custom"], t["source"]) for t in edw["tables"]]
    assert rows == [
        ("AFVC", "present", 4, ["ZZ_CELL"], "information_schema"),
        ("JEST", "present", 4, [], "information_schema"),
    ]
    assert svc.status_dict()["edw_tables_recorded"] == 2
    assert svc.edw_text("information_schema", text="no header here\nAFRU,MANDT").startswith(
        "No TABLE_NAME / COLUMN_NAME header found")


def test_missing_present_forget_prefix_plant_and_show(svc):
    assert svc.edw_text("missing", table="CRTX") == (
        "Noted: TV_CRTX is not in your EDW (told 2026-09-15). I won't propose it, and a query "
        "that needs it will say so.")
    text = svc.join_text(["AFRU", "CRHD", "CRTX"])
    assert "TV_CRTX is not in your EDW (you told me on 2026-09-15)" in text
    assert "EDW: TV_AFRU not recorded; TV_CRHD not recorded; TV_CRTX missing " \
           "(told on 2026-09-15)" in text
    assert svc.edw_text("present", table="CRTX") == (
        "Noted: TV_CRTX is in your EDW (no column list yet — paste it and I'll confirm columns "
        "too).")
    assert svc.edw_text("forget", table="CRTX") == "Forgot what I had recorded about TV_CRTX."
    assert svc.edw_text("forget", table="CRTX") == "Nothing was recorded about TV_CRTX."
    assert svc.edw_text("set_prefix", text="PROD.SAP.TV_") == \
        "Views now read as PROD.SAP.TV_<TABLE>."
    assert svc.naming().prefix == "PROD.SAP" and svc.naming().view_prefix == "TV_"
    assert svc.edw_text("set_prefix", text="EDW.SRC_SAPECC_ARP") == \
        "Views now read as EDW.SRC_SAPECC_ARP.TV_<TABLE>."
    assert svc.edw_text("set_plant", text="") == \
        "Plant filter cleared — queries no longer filter on WERKS."
    assert "WERKS" not in svc.sql_text(["AFRU"], [{"table": "AFRU", "field": "RUECK"}])
    assert svc.edw_text("set_plant", text="1006").startswith("Plant filter is now WERKS = '1006'")
    svc.edw_text("record_columns", table="AFRU", text=PASTE)
    show = svc.edw_text("show")
    assert show.splitlines()[0] == \
        "EDW views read as EDW.SRC_SAPECC_ARP.TV_<TABLE>; plant 1006; language 'E'."
    assert "1 tables recorded:" in show
    assert "  TV_AFRU present, 12 columns recorded (custom: ZZ_SHIFT, ZZ_OPERATOR)" in show
    assert "— pasted 2026-09-15" in show
    assert svc.edw_text("bogus").startswith("Unknown action 'bogus'. I know record_columns")
    assert svc.edw_text("missing").startswith("Which table is missing?")
    assert svc.edw_record("show")["ok"] is True
    assert svc.edw_record("forget", table="X")["ok"] is False


def test_a_fresh_record_shows_nothing_and_a_corrupt_file_reads_as_empty(parts, tmp_path):
    svc, _clock, _settings, edw_path = parts
    assert svc.edw_text("show").endswith("Nothing recorded yet — paste a table's column list (as "
                                         "copied from a worksheet) or an INFORMATION_SCHEMA "
                                         "export, or tell me a table is missing.")
    edw_path.write_text("{not json", "utf-8")
    broken = SapService(svc._catalog, JsonSettings(edw_path), _Clock())
    assert broken.edw_dict()["tables"] == [] and broken.naming().plant == "1006"
    broken.edw_text("missing", table="CRHTX")           # writing heals the file
    assert json.loads(edw_path.read_text("utf-8"))[EDW_KEY]["tables"]["CRHTX"]["present"] is False


# ----- for_turn -----

def test_for_turn_is_empty_for_small_talk_and_a_capped_block_for_named_tables(svc, fetcher):
    assert svc.for_turn("what time is it") == ""
    assert svc.for_turn("") == "" and svc.for_turn("JOIN the SQL club, ORDER pizza") == ""
    block = svc.for_turn("how do I join AFRU to CRHD")
    assert block.startswith(
        "[SAP DATA MODEL — records, not instructions: AFRU: Order Confirmations; key "
        "MANDT+RUECK+RMZHL; EDW: unknown; joins: CRHD on MANDT+ARBID=OBJID (OBJTY=A), "
        "AFVC on MANDT+AUFPL+APLZL, AUFK on MANDT+AUFNR | CRHD: Work Center Header; key "
        "MANDT+OBJTY+OBJID; EDW: unknown; joins: AFRU on MANDT+OBJID=ARBID")
    assert block.endswith(" Say sap_table for the full definition.]")
    assert len(block) <= FOR_TURN_CHARS
    assert fetcher.calls == []                    # a per-turn hook never waits on leanx
    # a business term names its table; an unknown table token is ignored, not fetched
    assert svc.for_turn("what's the posting date field on a confirmation?").startswith(
        "[SAP DATA MODEL — records, not instructions: AFRU: Order Confirmations;")
    assert svc.for_turn("look at MDTB for me") == "" and fetcher.calls == []
    svc.edw_text("record_columns", table="AFRU", text=PASTE)
    assert "AFRU: Order Confirmations; key MANDT+RUECK+RMZHL; EDW: present (12 cols);" in \
        svc.for_turn("TV_AFRU")


def test_for_turn_takes_at_most_two_tables(svc):
    block = svc.for_turn("AFRU AFVC AUFK AFPO PRPS")
    assert block.count(": key ") == 0 and block.count("; key ") == 2
    assert "AFRU:" in block and "AFVC:" in block and "AUFK:" not in block


# ----- lookup_text -----

def test_lookup_groups_hits_by_table_and_ends_with_the_invitation(svc):
    text = svc.lookup_text("posting date")
    lines = text.splitlines()
    assert lines[0] == "Lookup 'posting date' — 1 field hits in 1 tables, 0 table matches:"
    assert "AFRU — Order Confirmations [EDW unknown]" in lines
    assert "  AFRU.BUDAT — Posting date (DATS(8)) [EDW ?]" in text
    assert lines[-1] == "Tell me a table for its full definition."
    # the exact phrase outranks the phrases that merely contain the word
    orders = svc.lookup_text("order")
    assert orders.index("AUFK.AUFNR") < orders.index("AUFK.KTEXT") < orders.index("AFPO.KDAUF")
    # a query of several words needs 60% of them in a phrase: 'posting time' is not 'posting date'
    assert "AFRU.ERZET" not in text and "AFRU.ERZET" in svc.lookup_text("the posting time")
    by_name = svc.lookup_text("afru")
    assert "Tables:\n  AFRU — Order Confirmations (PP) [EDW unknown]" in by_name
    assert svc.lookup_text("unicorn stables").startswith("Nothing in the catalog matches "
                                                         "'unicorn stables'.")
    assert svc.lookup_text("").startswith("What should I look up?")
    # without a wide index the catalogued tables' own fields answer a field-name question
    assert "  CRHD.ARBPL — Work center (CHAR(8)) [EDW ?]" in svc.lookup_text("arbpl")


def _wide_index(shipped: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """shipped/wide.sqlite.gz in the agreed schema (helix/sapcatalog/README.md) holding
    (table, table description, field, field description) rows, every field a DATS."""
    db = shipped / "wide.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE tables(name TEXT PRIMARY KEY, description TEXT, category TEXT, "
        "delivery_class TEXT, module TEXT, component TEXT);"
        "CREATE TABLE fields(table_name TEXT, position INTEGER, name TEXT, description TEXT, "
        "data_element TEXT, domain TEXT, data_type TEXT, length INTEGER, decimals INTEGER, "
        "check_table TEXT);"
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
    )
    seen: set[str] = set()
    for i, (table, tdesc, field, fdesc) in enumerate(rows):
        if table not in seen:
            seen.add(table)
            conn.execute("INSERT INTO tables VALUES (?, ?, 'TRANSP', 'A', '', '')", (table, tdesc))
        conn.execute("INSERT INTO fields VALUES (?, ?, ?, ?, '', '', 'DATS', 8, 0, '')",
                     (table, i + 1, field, fdesc))
    conn.commit()
    conn.close()
    with open(db, "rb") as src, gzip.open(shipped / "wide.sqlite.gz", "wb") as out:
        shutil.copyfileobj(src, out)
    db.unlink()


def test_lookup_puts_the_catalogued_tables_before_the_ecc_wide_index(tmp_path, fetcher):
    """On the real catalog 'posting date' came back as one AFRU line and eleven /LSIERP/… and
    BBP_… tables: the wide index is ordered by table name and truncated, so everything a
    production report reads sat behind the alphabet. The catalogued tables' own fields answer
    first, an in-scope table the build could not ship (MKPF here) comes from the wide index
    next, the ECC-wide rest is capped at WIDE_REST_MAX — and fills the list when nothing
    catalogued matched, because 'which table has a field called X' is what the index is for."""
    shipped = _shipped(tmp_path / "shipped", *SHIPPED)
    junk = [(f"/LSIERP/PROC_LOG{i}", "Payment trigger log", "POSTING_DATE", "Posting date")
            for i in range(8)]
    _wide_index(shipped, [
        *junk,
        ("AFRU", "Order Confirmations", "BUDAT", "Posting date"),     # shipped: the catalog's line
        ("MKPF", "Header: Material Document", "BUDAT", "Posting Date in the Document"),
        ("ZZTAB", "A site table", "ZZ_ONLY_HERE", "Only the wide index knows it"),
    ])
    svc = SapService(CatalogStore(tmp_path / "data", shipped=shipped),
                     JsonSettings(tmp_path / "edw.json"), _Clock(),
                     settings=JsonSettings(tmp_path / "settings.json"))
    text = svc.lookup_text("posting date")
    lines = text.splitlines()
    shown = 2 + WIDE_REST_MAX
    assert lines[0] == f"Lookup 'posting date' — {shown} field hits in {shown} tables, 0 table " \
                       "matches:"
    assert lines[1] == "AFRU — Order Confirmations [EDW unknown]"
    assert lines[3] == "MKPF — Header: Material Document [EDW unknown]"
    assert lines[4] == "  MKPF.BUDAT — Posting Date in the Document (DATS) [EDW ?]"
    assert text.count("/LSIERP/PROC_LOG") == 2 * WIDE_REST_MAX      # a table line + a field line
    # a field only the wide index knows is still found, and nothing caps it
    only = svc.lookup_text("zz_only_here")
    assert "  ZZTAB.ZZ_ONLY_HERE — Only the wide index knows it (DATS) [EDW ?]" in only
    assert fetcher.calls == []                    # a lookup never waits on leanx


def test_search_dict_carries_the_same_hits_for_the_panel(svc):
    svc.edw_text("record_columns", table="AFRU", text=PASTE)
    out = svc.search_dict("posting date", limit=5)
    assert out["query"] == "posting date" and len(out["hits"]) <= 5
    first = out["hits"][0]
    assert first["table"] == "AFRU" and first["field"] == "BUDAT" and first["edw"] is True
    assert first["type"] == "DATS(8)" and first["source"] == "vocabulary"
    assert svc.search_dict("AFRU")["tables"][0] == {
        "name": "AFRU", "description": "Order Confirmations", "module": "PP", "edw": "present"}
    assert svc.search_dict("") == {"query": "", "hits": [], "tables": []}


# ----- the WIP report -----

def test_the_wip_report_lists_every_column_and_writes_its_query(svc):
    text = svc.report_text("wip")
    assert text.startswith("WIP report — 27 columns, one row per order operation (FROM AFVC;")
    for col in WIP_REPORT:
        assert f"  {col.label} — {col.source} ({col.status})" in text, col.label
    assert len(WIP_REPORT) == 27
    sql = text.split("Query:\n", 1)[1].split("\nWarnings:")[0]
    assert sql.startswith("WITH sys_status AS (\n")
    assert "\nFROM EDW.SRC_SAPECC_ARP.TV_AFVC AS AFVC\n" in sql
    assert "JOIN EDW.SRC_SAPECC_ARP.TV_AFKO AS AFKO\n  ON AFKO.MANDT = AFVC.MANDT AND " \
           "AFKO.AUFPL = AFVC.AUFPL" in sql
    assert "JOIN EDW.SRC_SAPECC_ARP.TV_PRPS AS PRPS\n  ON PRPS.MANDT = AFPO.MANDT AND " \
           "PRPS.PSPNR = AFPO.PROJN" in sql
    # The MRP controller comes from the order (AFKO.DISPO on AUFK.WERKS) through a recipe, not
    # from the material's plant data — MARC/T024D are not joined into the report body.
    assert "mrp_ctl AS (" in sql and "LEFT JOIN mrp_ctl ON mrp_ctl.AUFNR = AUFK.AUFNR" in sql
    assert "TV_T024D AS T024D" not in sql and "TV_MARC AS MARC" not in sql
    assert "LEFT JOIN EDW.SRC_SAPECC_ARP.TV_CRTX AS CRTX\n" in sql
    assert "  AND CRTX.SPRAS = 'E'" in sql and "  AND MAKT.SPRAS = 'E'" in sql
    assert "       NULL AS RATING_RWK,\n" in sql and "       NULL AS PRIORITY,\n" in sql
    assert "       AFVC.VORNR AS OPERATION_ACTIVITY,\n" in sql
    assert "       sys_status.SYSTEM_STATUS AS SYSTEM_STATUS,\n" in sql
    assert "LEFT JOIN last_conf ON last_conf.AUFPL = AFVC.AUFPL AND " \
           "last_conf.APLZL = AFVC.APLZL" in sql
    assert "LEFT JOIN shortages ON shortages.RSNUM = AFKO.RSNUM" in sql
    assert "WHERE AFVC.WERKS = '1006'  -- plant (optional)" in sql
    assert "MRP Controller is the order's own AFKO.DISPO" in text
    assert svc.report_text("nope").startswith("I know one report — wip:")
    report = svc.report_dict("wip")
    assert [c["label"] for c in report["columns"]] == [c.label for c in WIP_REPORT]
    assert report["sql"] == sql and report["tables"][0] == "AFVC"
    assert svc.sql_dict({"tables": [], "report": "WIP"})["name"] == "wip"


# ----- the panel dicts -----

def test_the_panel_dicts_carry_the_same_facts_as_the_text(svc):
    svc.edw_text("record_columns", table="AFRU", text=PASTE)
    d = svc.table_dict("tv_afru")
    assert d["name"] == "AFRU" and d["keys"] == ["MANDT", "RUECK", "RMZHL"]
    assert d["provenance"] == "SAP data dictionary via leanx.eu, read 2026-09-01"
    assert d["edw"] == {"status": "present", "columns_recorded": 12,
                        "custom": ["ZZ_SHIFT", "ZZ_OPERATOR"],
                        "missing": ["VORNR", "ARBID", "ERSDA", "ERZET", "ISDD", "LMNGA", "XMNGA",
                                    "ISMNW", "AUERU", "PERNR", "LTXA1"],
                        "recorded_at": "2026-09-15", "source": "pasted", "view": "TV_AFRU"}
    budat = next(f for f in d["fields"] if f["name"] == "BUDAT")
    assert budat == {"name": "BUDAT", "description": "Posting date", "key": False,
                     "type": "DATS(8)", "check_table": "", "edw": True,
                     "note": "Posting date of the confirmation. There is no posting time; ERZET "
                             "is the entry time and ERSDA the entry date."}
    assert next(f for f in d["fields"] if f["name"] == "VORNR")["edw"] is False
    # the AFVC → AFRU edge is the expert's orientation: shown to AFRU as an inward join
    afvc = next(j for j in d["joins"] if j["other"] == "AFVC" and j["on"][1] == ["AUFPL", "AUFPL"])
    assert afvc["direction"] == "in" and afvc["cardinality"] == "1:n" and afvc["verified"] is True
    crhd = next(j for j in d["joins"] if j["other"] == "CRHD")
    assert crhd["direction"] == "out" and crhd["on"] == [["MANDT", "MANDT"], ["ARBID", "OBJID"]]
    assert crhd["filters"] == [{"table": "CRHD", "field": "OBJTY", "op": "=", "value": "A",
                                "why": "work centers only — CRHD also holds other capacity "
                                       "objects; OBJTY 'A' is a work center", "optional": False}]
    assert d["filters"][0]["field"] == "STOKZ" and d["filters"][0]["optional"] is True
    assert d["notes"][0].startswith("Confirmations — every time work is booked")
    assert svc.table_dict("NOPE", fetch=False) is None and svc.table_dict("") is None

    j = svc.join_dict(["AFRU", "PRPS"])
    assert j["root"] == "AFRU" and j["tables"] == ["AFRU", "AUFK", "AFPO", "PRPS"]
    assert j["plan"][2] == {"left": "AFPO", "right": "PRPS", "table": "PRPS",
                            "on": [["MANDT", "MANDT"], ["PROJN", "PSPNR"]], "kind": "curated",
                            "cardinality": "n:1", "note": j["plan"][2]["note"], "verified": True,
                            "filters": [{"table": "PRPS", "field": "LOEVM", "op": "=", "value": "",
                                         "why": "WBS elements not flagged for deletion",
                                         "optional": True}]}
    assert j["edw"] == {"AFRU": "present", "AUFK": "unknown", "AFPO": "unknown", "PRPS": "unknown"}
    assert j["sql"].startswith("SELECT AFRU.*\n")
    assert svc.join_dict([])["plan"] == [] and svc.join_dict([])["sql"] == ""

    s = svc.sql_dict({"tables": ["AFRU"], "columns": [{"table": "AFRU", "field": "RUECK"}],
                      "filters": [], "recipes": [], "root": "AFRU", "limit": 50})
    assert s["sql"].endswith("LIMIT 50;") and s["columns"] == ["RUECK"] and s["tables"] == ["AFRU"]
    assert svc.sql_dict({})["sql"] == "" and svc.sql_dict("junk")["warnings"]

    status = svc.status_dict()
    assert status["shipped"] == 10 and status["extensions"] == 0 and status["wide_index"] is False
    assert status["fetch_allowed"] is True and status["curated_joins"] == len(CURATED_JOINS)
    assert 0 < status["verified_joins"] < len(CURATED_JOINS)
    assert status["edw_tables_recorded"] == 1 and status["join_faults"] == []
    assert status["built_at"] == "2026-09-14" and "wip" in status["reports"]


def test_the_verification_pass_flips_only_the_joins_the_dictionary_confirms(svc, caplog):
    assert svc._graph is None                     # built on first use, not at boot
    n = svc.status_dict()["verified_joins"]
    assert 0 < n < len(CURATED_JOINS) and n == len(svc._verified_keys)
    graph = svc._graph
    assert svc.status_dict()["verified_joins"] == n and svc._graph is graph   # built once
    # curated order survives verification: the order item beats the settlement rule on a tie
    assert svc.join_dict(["AFRU", "PRPS"])["tables"] == ["AFRU", "AUFK", "AFPO", "PRPS"]
    # a join naming a field the fixture dictionary lacks stays unverified and is logged
    bad = JoinEdge("AFRU", "AUFK", (("MANDT", "MANDT"), ("BUZEIT", "AUFNR")), kind="curated")
    with caplog.at_level(logging.WARNING, logger="helix.sap"):
        assert svc._verify(bad) is False and svc._verify(replace(bad)) is False
    assert svc._join_faults == ["AFRU → AUFK names AFRU.BUZEIT, not in the dictionary"]
    assert sum("curated SAP join" in m for m in caplog.messages) == 1


# ----- nothing raises -----

@pytest.mark.parametrize("garbage", [None, 42, [], {}, "", "   ", "DROP TABLE;", "x" * 500,
                                     ["AFRU", None, 7], {"tables": None}, b"bytes"])
def test_nothing_raises_on_garbage_input(svc, garbage):
    for fn in (svc.lookup_text, svc.table_text, svc.join_text, svc.report_text, svc.for_turn):
        assert isinstance(fn(garbage), str)
    assert isinstance(svc.sql_text(garbage, garbage, filters=garbage, recipes=garbage,
                                   root=garbage, limit=garbage), str)
    assert isinstance(svc.edw_text(garbage, table=garbage, text=garbage), str)
    assert isinstance(svc.table_text("AFRU", section=garbage, offset=garbage), str)
    assert isinstance(svc.search_dict(garbage), dict)
    assert svc.table_dict(garbage) is None or isinstance(svc.table_dict(garbage), dict)
    assert isinstance(svc.join_dict(garbage), dict) and isinstance(svc.sql_dict(garbage), dict)
    assert isinstance(svc.report_dict(garbage), dict) and isinstance(svc.edw_record(garbage), dict)


def test_a_text_method_that_fails_inside_answers_with_a_sentence(svc, monkeypatch, caplog):
    def boom(*_a, **_k):
        raise OSError("catalog file corrupt")

    monkeypatch.setattr(svc._catalog, "get", boom)
    with caplog.at_level(logging.WARNING, logger="helix.sap"):
        assert svc.table_text("AFRU") == \
            "The SAP catalog couldn't answer that: catalog file corrupt"
        assert svc.for_turn("AFRU?") == ""
    assert any("SapService.table_text failed" in m for m in caplog.messages)
