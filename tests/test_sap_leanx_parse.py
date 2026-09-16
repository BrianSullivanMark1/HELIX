"""leanx_parse: a leanx.eu table page → TableDef, against trimmed copies of the real pages in
tests/fixtures/sap (fetched 2026-09-15). Every fact the catalog will recite — key flags,
descriptions, check tables, types, fixed values, compound foreign keys — is pinned here, because
the parser is the point where HELIX stopped guessing SAP field names and started reading them.
The pages are the site's real markup: nested possible-values tables inside the fields table, the
wrapped 'foreign key relationships' heading, FK rows that borrow a column from another structure
or from SYST, the generic '*' column, entities in descriptions, whitespace everywhere."""
from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import pytest

from helix.domain.sap.leanx_parse import is_table_page, parse_table, table_url
from helix.domain.sap.model import FkRef, TableDef

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sap"
TABLE_PAGES = ("afru_head", "jest", "tj02t", "makt", "t024d", "rkpf_fk")


def _html(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


@cache
def _table(name: str) -> TableDef:
    return parse_table(_html(name), fetched_at="2026-09-15")


def _fk(table: TableDef, field: str, check: str) -> FkRef:
    found = [fk for fk in table.foreign_keys if fk.field == field and fk.check_table == check]
    assert len(found) == 1, (field, check, table.foreign_keys)
    return found[0]


# ----- hand-built pages, leanx-shaped, for the cases the fixtures do not show -----

def _field_row(name: str, desc: str = "", *, key: bool = False, delem: str = "", check: str = "",
               dtype: str = "CHAR", length: str = "1", decimals: str = "0",
               values: tuple[tuple[str, str], ...] | None = None, desc_div: bool = True) -> str:
    cls = "bg-blue-50" if key else "hover:bg-gray-50"
    check_html = f'<a href="/sap/table/{check.lower()}/">\n  {check}   </a>' if check else ""
    desc_html = f'<div class="text-sm text-gray-500">{desc}</div>' if desc_div else ""
    row = (f'<tr class="{cls}"><td><div class="font-medium text-gray-900">{name}</div>{desc_html}'
           f"</td><td>{delem}</td><td>{check_html}</td><td><div>{dtype}</div><div></div></td>"
           f"<td>{length}</td><td>{decimals}</td><td>"
           + ("<button onclick=\"toggleCollapse('collapse-1')\">Possible values</button>"
              if values else "") + "</td></tr>")
    if values:
        body = "".join(
            "<tr><td>" + ('<span class="text-gray-500 italic">NULL</span>' if v is None else v)
            + f"</td><td>{d}</td></tr>" for v, d in values)
        row += ('<tr id="collapse-1" class="hidden"><td colspan="7"><div><table><thead><tr>'
                "<th>Value</th><th>Description</th></tr></thead><tbody>" + body
                + "</tbody></table></div></td></tr>")
    return row


def _fk_row(field: str, fk_table: str, fk_field: str, check: str, check_field: str,
            table: str = "ZTEST") -> str:
    return (f'<tr class="hover:bg-gray-50"><td>{table}</td><td>{field}</td><td>{fk_table}</td>'
            f'<td>{fk_field}</td><td><a href="/sap/table/{check.lower()}/">{check}</a>'
            f"<div>desc</div></td><td>{check_field}</td></tr>")


def _page(rows: str, fk_rows: str | None = "", name: str = "ZTEST",
          description: str = "A test table") -> str:
    """The skeleton of a leanx page: the h1/h2, the intro paragraph (which always mentions
    'foreign key relationships, if any'), the fields table and — unless fk_rows is None — an
    FK table around `fk_rows`."""
    fk = "" if fk_rows is None else (
        f"<h3>{name} foreign key\n relationships</h3><table><thead><tr><th>Table</th>"
        "<th>Field</th><th>Foreign key table</th><th>Foreign key field</th><th>Check table</th>"
        f"<th>Check field</th></tr></thead><tbody>{fk_rows}</tbody></table>")
    return (f"<html><body><h1>SAP Table {name}</h1><h2>{description}</h2>"
            f"<p>Additionally we provide an overview of foreign key relationships, if any, that "
            f"link {name} to other SAP tables.</p><h3>{name} table fields</h3>"
            "<table><thead><tr><th>Field</th><th>Data element</th><th>Checktable</th>"
            "<th>Datatype</th><th>Length</th><th>Decimals</th><th>Possible values</th></tr>"
            f"</thead><tbody>{rows}</tbody></table>{fk}</body></html>")


# ----- URLs and page recognition -----

def test_table_url_is_the_lower_case_page_url_with_namespace_slashes_kept():
    assert table_url("AFRU") == "https://leanx.eu/sap/table/afru/"
    assert table_url("/BEV1/LULDEGRP") == "https://leanx.eu/sap/table//bev1/luldegrp/"
    assert table_url(" tv_afru ") == "https://leanx.eu/sap/table/afru/"


def test_every_fixture_table_page_is_recognised():
    for name in TABLE_PAGES:
        assert is_table_page(_html(name)), name


def test_the_not_found_page_and_other_documents_are_not_table_pages():
    assert not is_table_page(_html("notfound"))       # h1 'SAP Table Not Found', no table
    assert not is_table_page("")
    assert not is_table_page("<html><body><h1>SAP Table AFRU</h1><p>no table</p></body></html>")
    with pytest.raises(ValueError):
        parse_table(_html("notfound"))


# ----- (8) name and description from the headings, provenance -----

def test_name_and_description_come_from_the_h1_and_h2():
    jest = _table("jest")
    assert jest.name == "JEST" and jest.description == "Individual Object Status"
    afru = _table("afru_head")
    assert afru.name == "AFRU" and afru.description == "Order Confirmations"
    assert _table("t024d").description == "MRP controllers"


def test_provenance_is_leanx_with_the_canonical_url_unless_one_is_given():
    afru = _table("afru_head")
    assert afru.source == "leanx" and afru.fetched_at == "2026-09-15"
    assert afru.source_url == "https://leanx.eu/sap/table/afru/"
    given = parse_table(_html("jest"), source_url="https://leanx.eu/sap/table/jest/?x=1")
    assert given.source_url == "https://leanx.eu/sap/table/jest/?x=1"


# ----- (1) key fields are the blue rows -----

def test_key_fields_are_the_blue_rows():
    assert _table("jest").primary_key() == ("MANDT", "OBJNR", "STAT")
    assert not _table("jest").field("INACT").key
    assert _table("afru_head").primary_key() == ("MANDT", "RUECK", "RMZHL")
    assert _table("makt").primary_key() == ("MANDT", "MATNR", "SPRAS")


def test_tj02t_is_keyed_by_istat_and_spras_with_no_client_field():
    tj = _table("tj02t")
    assert tj.primary_key() == ("ISTAT", "SPRAS")
    assert not tj.has_field("MANDT")
    assert tj.language_key() == "SPRAS" and tj.is_text_table
    assert tj.field_names() == ("ISTAT", "SPRAS", "TXT04", "TXT30")


# ----- (2) description, (3) check table, (4) type/length/decimals -----

def test_description_is_the_second_div_of_the_field_cell():
    afru = _table("afru_head")
    assert afru.field("MANDT").description == "Client"
    assert afru.field("RUECK").description == "Completion confirmation number for the operation"
    assert afru.field("STOKZ").description == "Indicator: Document Has Been Reversed"
    assert _table("makt").field("MAKTG").description == (
        "Material description in upper case for matchcodes")


def test_check_table_is_the_link_text_and_blank_when_the_cell_has_none():
    afru = _table("afru_head")
    assert afru.field("MANDT").check_table == "T000"
    assert afru.field("RUECK").check_table == ""
    makt = _table("makt")
    assert makt.field("MATNR").check_table == "MARA"
    assert makt.field("SPRAS").check_table == "T002"
    assert makt.field("MAKTX").check_table == ""


def test_data_element_type_length_and_decimals_are_read_from_their_cells():
    afru = _table("afru_head")
    mandt, rueck = afru.field("MANDT"), afru.field("RUECK")
    assert (mandt.data_element, mandt.data_type, mandt.length, mandt.decimals) == (
        "MANDT", "CLNT", 3, 0)
    assert (rueck.data_element, rueck.data_type, rueck.length) == ("CO_RUECK", "NUMC", 10)
    assert afru.field("ERSDA").is_date and afru.field("ERSDA").type_label() == "DATS(8)"
    assert _table("makt").field("MAKTX").type_label() == "CHAR(40)"
    assert _table("jest").field("OBJNR").type_label() == "CHAR(22)"
    assert _table("makt").field("SPRAS").is_language


def test_a_quantity_field_keeps_its_decimals_and_missing_numbers_become_zero():
    page = _page(_field_row("MENGE", "Quantity", delem="MENGE_D", dtype="QUAN", length="13",
                            decimals="3")
                 + _field_row("ODD", "", length="", decimals="", desc_div=False))
    t = parse_table(page)
    assert t.field("MENGE").type_label() == "QUAN(13,3)"
    odd = t.field("ODD")
    assert (odd.length, odd.decimals, odd.description, odd.data_element) == (0, 0, "", "")


# ----- (5) possible values from the hidden nested table -----

def test_possible_values_come_from_the_hidden_sub_table_with_the_null_sentinel_blank():
    assert _table("afru_head").field("STOKZ").values == (
        ("X", "Flag set. Event has occurred."), ("", "Flag is Not Set"))
    assert _table("jest").field("INACT").values == (("", "Active"), ("X", "Not active"))
    assert _table("afru_head").field("MANDT").values == ()
    assert _table("rkpf_fk").field("KZVER").values == (("X", "Yes"), ("", "No"))


def test_nested_value_tables_do_not_end_the_fields_table_early():
    # STOKZ's sub-table closes with a </table> of its own; the fields after it and the whole FK
    # table further down must still be there — the parser tracks depth, it does not split.
    afru = _table("afru_head")
    assert afru.field_names() == (
        "MANDT", "RUECK", "RMZHL", "ERSDA", "ERNAM", "LAEDA", "AENAM", "BUDAT", "ARBID",
        "WERKS", "LTXA1", "STOKZ")
    assert len(afru.foreign_keys) == 35
    jest = _table("jest")
    assert jest.field_names() == ("MANDT", "OBJNR", "STAT", "INACT", "CHGNR")   # CHGNR after INACT
    assert [f.position for f in jest.fields] == [1, 2, 3, 4, 5]


def test_a_literal_null_value_text_that_is_not_the_italic_sentinel_is_kept():
    page = _page(_field_row("FLAG", "Flag", values=((None, "Off"), ("NULL", "The word"))))
    assert parse_table(page).field("FLAG").values == (("", "Off"), ("NULL", "The word"))


# ----- (6) foreign keys grouped per (field, check table) with their full compound columns -----

def test_foreign_keys_are_grouped_by_field_and_check_table_in_page_order():
    afru = _table("afru_head")
    assert afru.foreign_keys[0] == FkRef(
        field="APLZL", check_table="AFVC",
        columns=(("MANDT", "MANDT"), ("AUFPL", "AUFPL"), ("APLZL", "APLZL")))
    assert afru.foreign_keys[0].joinable and not afru.foreign_keys[0].partial
    assert afru.check_tables()[:4] == ("AFVC", "AUFK", "TBMOT", "KBED")
    assert _fk(afru, "TXTSP", "T002").columns == (("TXTSP", "SPRAS"),)
    assert _table("makt").foreign_keys == (
        FkRef("MANDT", "T000", (("MANDT", "MANDT"),)),
        FkRef("MATNR", "MARA", (("MANDT", "MANDT"), ("MATNR", "MATNR"))),
        FkRef("SPRAS", "T002", (("SPRAS", "SPRAS"),)),
    )
    assert _table("tj02t").foreign_keys == (
        FkRef("ISTAT", "TJ02", (("ISTAT", "ISTAT"),)),
        FkRef("SPRAS", "T002", (("SPRAS", "SPRAS"),)),
    )
    assert len(_table("jest").fks_to("ONR00")) == 1
    assert _fk(_table("jest"), "OBJNR", "ONR00").columns == (("MANDT", "MANDT"), ("OBJNR", "OBJNR"))


def test_a_column_borrowed_from_another_table_is_qualified_and_makes_the_reference_partial():
    afru = _table("afru_head")
    canum = _fk(afru, "CANUM", "KBED")
    assert canum.columns == (
        ("MANDT", "MANDT"), ("AFVC.BEDID", "BEDID"), ("AFVC.BEDZL", "BEDZL"), ("CANUM", "CANUM"))
    assert canum.partial and not canum.joinable
    assert canum.join_columns == (("MANDT", "MANDT"), ("CANUM", "CANUM"))
    assert _fk(afru, "LEARR", "CSLA").columns == (
        ("AFVGD.KOKRS", "KOKRS"), ("LEARR", "LSTAR"), ("BUDAT", "DATBI"), ("MANDT", "MANDT"))
    assert _fk(_table("rkpf_fk"), "ANLN1", "ANLH").columns == (
        ("ANLN1", "ANLN1"), ("MANDT", "MANDT"), ("RESB.BUKRS", "BUKRS"))
    assert sum(1 for fk in afru.foreign_keys if fk.partial) == 2


def test_a_system_field_column_is_qualified_as_syst():
    rkpf = _table("rkpf_fk")
    assert _fk(rkpf, "ZZREGION", "ZREG") == FkRef(
        "ZZREGION", "ZREG", (("SYST.MANDT", "MANDT"), ("ZZREGION", "ZZREGION")), partial=True)
    assert rkpf.primary_key() == ("MANDT", "RSNUM") and len(rkpf.foreign_keys) == 21


def test_a_generic_star_column_keeps_the_pair_and_marks_the_reference_partial():
    assert _fk(_table("t024d"), "PRCTR", "CEPC") == FkRef(
        "PRCTR", "CEPC",
        (("MANDT", "MANDT"), ("PRCTR", "PRCTR"), ("*", "DATBI"), ("*", "KOKRS")), partial=True)
    assert _fk(_table("rkpf_fk"), "ZZSTATE", "T005S").columns == (
        ("SYST.MANDT", "MANDT"), ("*", "LAND1"), ("ZZSTATE", "BLAND"))
    assert _fk(_table("t024d"), "WERKS", "T001W") == FkRef(
        "WERKS", "T001W", (("MANDT", "MANDT"), ("WERKS", "WERKS")))


def test_a_constant_check_keeps_the_quoted_literal_as_the_host_expression():
    # SAP's "'0001'" foreign-key field (PA0001.PREAS → T530E.INFTY): the literal sits in the
    # 'Foreign key table' column and the field is blank.
    page = _page(_field_row("PREAS", "Reason", key=False),
                 _fk_row("PREAS", "ZTEST", "MANDT", "T530E", "MANDT")
                 + _fk_row("PREAS", "&#039;0001&#039;", "", "T530E", "INFTY")
                 + _fk_row("PREAS", "ZTEST", "PREAS", "T530E", "PREAS"))
    fk = parse_table(page).foreign_keys[0]
    assert fk.columns == (("MANDT", "MANDT"), ("'0001'", "INFTY"), ("PREAS", "PREAS"))
    assert fk.partial


# ----- (7) a structure: no FK section and no key fields -----

def _without_fk_section(html: str, name: str = "TJ02T") -> str:
    """The page up to the FK section's container div. Anchored on the h3, not on the words
    'foreign key' — the intro paragraph uses them on every page."""
    h3 = re.search(rf"<h3[^>]*>\s*{name} foreign key", html)
    assert h3, "no FK heading"
    cut = html.rindex('<div class="bg-white border rounded-lg overflow-hidden">', 0, h3.start())
    return html[:cut]


def test_a_page_with_no_fk_section_and_no_key_fields_is_a_structure():
    # The intro paragraph still says "foreign key relationships, if any" — that phrase must not
    # count as an FK section.
    html = _without_fk_section(_html("tj02t")).replace("bg-blue-50", "hover:bg-gray-50")
    t = parse_table(html)
    assert t.category == "STRUCT" and t.is_structure
    assert t.foreign_keys == () and t.primary_key() == ()
    assert t.field_names() == ("ISTAT", "SPRAS", "TXT04", "TXT30")


def test_a_table_with_keys_or_an_fk_section_is_not_a_structure():
    assert _table("tj02t").category == "" and not _table("tj02t").is_structure
    keys_only = parse_table(_without_fk_section(_html("tj02t")))
    assert keys_only.category == "" and keys_only.primary_key() == ("ISTAT", "SPRAS")
    assert not keys_only.is_structure
    fks_only = parse_table(_html("tj02t").replace("bg-blue-50", "hover:bg-gray-50"))
    assert fks_only.category == "" and len(fks_only.foreign_keys) == 2


# ----- (9) whitespace squashed, entities unescaped -----

def test_whitespace_is_squashed_and_entities_unescaped():
    assert _table("t024d").field("DSTEL").description == "MRP controller's telephone number"
    # the source wraps 'T000' / 'AFVC' in newlines and runs of spaces inside the <a>
    assert _table("afru_head").field("MANDT").check_table == "T000"
    assert _table("afru_head").foreign_keys[0].check_table == "AFVC"
    page = _page(_field_row("NAME1", "Name  &amp;\n   address &lt;line 1&gt;", delem=" NAME1 \n"))
    f = parse_table(page).field("NAME1")
    assert f.description == "Name & address <line 1>" and f.data_element == "NAME1"


def test_fields_carry_their_position_on_the_page():
    assert [f.position for f in _table("afru_head").fields] == list(range(1, 13))
    assert _table("afru_head").field("STOKZ").position == 12


def test_a_parsed_table_survives_the_model_round_trip():
    for name in TABLE_PAGES:
        t = _table(name)
        assert TableDef.from_dict(t.to_dict()) == t, name
