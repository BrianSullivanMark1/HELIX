"""The EDW overlay (helix/domain/sap/overlay.py): every shape of paste parse_columns reads, the
INFORMATION_SCHEMA export in its variants, the three-valued status/has_column answers, the custom
and missing column diffs against the dictionary, record/mark/forget, the JSON round-trip, and a
corrupt document reading as empty defaults. Pure — no store, no Container, no I/O."""
from __future__ import annotations

import json

import pytest

from helix.domain.sap.model import normalize_table
from helix.domain.sap.overlay import (
    MISSING,
    PRESENT,
    UNKNOWN,
    EdwOverlay,
    EdwTable,
    parse_columns,
    parse_information_schema,
)

# The AFRU column list exactly as the user pasted it into HELIX from the EDW view — with the
# site's own ZZ* fields at the end, which no SAP dictionary knows.
AFRU_PASTE = (
    "MANDT RUECK RMZHL ERSDA ERNAM LAEDA AENAM BUDAT ARBID WERKS LTXA1 TXTSP ISERH ZEIER ILE01 "
    "ISM01 ILE02 ISM02 ILE03 ISM03 ILE04 ISM04 ILE05 ISM05 ILE06 ISM06 ABARB ISMNW ISMNE LEARR "
    "IDAUR IDAUE ZCODE LOART QUALF ANZMA LOGRP GMNGA LMNGA XMNGA GMEIN MEINH GRUND PERNR ISDD "
    "ISDZ IERD IERZ ISBD ISBZ IEBD IEBZ ISAD ISAZ IEDD IEDZ PEDD PEDZ WABLNR WEBLNR AUERU AUSOR "
    "STNDR MANUR MEILR AUFPL APLZL AUFNR APLFL VORNR SUMNR OFM01 OFE01 LEK01 OFM02 OFE02 LEK02 "
    "OFM03 OFE03 LEK03 OFM04 OFE04 LEK04 OFM05 OFE05 LEK05 OFM06 OFE06 LEK06 OFMNW OFMNE LEKNW "
    "ODAUR ODAUE STOKZ STZHL SMENG RUECK_MST RMZHL_MST PDSNR KAPID SPLIT ZAUSW ORIND ORIGF CANUM "
    "BELNR_IST BELNR_UMB RMNGA CATSBELNR SATZA ERZET CATSPRICE CATSTCURR CATSPEINH BEMOT IPRZ1 "
    "IPRE1 IPRK1 EXNAM EXERD EXERZ PRZ01 OPRZ1 OPRE1 SKOKRS SKOSTL NODAT ISMNU OFMNU PACKNO EXTID "
    "SCHGRUP KAPTPROG OBMAT OBCHA LICHA MYEAR ME_SFCID ME_2ND_CONF_QTY ROLE_ID UCMAT UCCHA WTY_IND "
    "ZZOPERATION_TYPE ZZUPDATE_ON_UTC"
)
AFRU_COLUMNS = tuple(AFRU_PASTE.split())
# What the dictionary would hand over: the same list without the site's additions.
AFRU_DICTIONARY = tuple(c for c in AFRU_COLUMNS if not c.startswith("ZZ"))

TODAY = "2026-09-15"


# ----- parse_columns: every shape of paste -----

def test_parse_columns_reads_a_space_separated_worksheet_copy():
    cols = parse_columns(AFRU_PASTE)
    assert cols == AFRU_COLUMNS
    assert len(cols) == len(set(cols)) == 146
    assert cols[:3] == ("MANDT", "RUECK", "RMZHL")
    assert cols[-2:] == ("ZZOPERATION_TYPE", "ZZUPDATE_ON_UTC")


def test_parse_columns_reads_commas_newlines_and_tabs():
    assert parse_columns("mandt, rueck, rmzhl") == ("MANDT", "RUECK", "RMZHL")
    assert parse_columns("MANDT,RUECK,RMZHL,") == ("MANDT", "RUECK", "RMZHL")
    assert parse_columns("MANDT\nRUECK\nRMZHL\n") == ("MANDT", "RUECK", "RMZHL")
    assert parse_columns("MANDT\r\nRUECK\r\n\r\nRMZHL") == ("MANDT", "RUECK", "RMZHL")
    assert parse_columns("MANDT\tRUECK\tRMZHL") == ("MANDT", "RUECK", "RMZHL")
    assert parse_columns("MANDT; RUECK;RMZHL") == ("MANDT", "RUECK", "RMZHL")


def test_parse_columns_takes_the_select_list_of_a_query_fragment():
    assert parse_columns("SELECT MANDT, RUECK, RMZHL FROM EDW.SRC_SAPECC_ARP.TV_AFRU") == (
        "MANDT", "RUECK", "RMZHL")
    # lower case, qualified names, an alias, a function, DISTINCT — and nothing from the FROM/WHERE
    sql = ("select distinct r.aufnr, r.vornr, r.budat as last_worked, sum(r.lmnga) as yield\n"
           "from tv_afru r\njoin tv_aufk k on k.aufnr = r.aufnr\nwhere r.werks = '1006'")
    assert parse_columns(sql) == ("AUFNR", "VORNR", "BUDAT", "LMNGA")
    # no FROM at all: everything after SELECT is the list
    assert parse_columns("SELECT AUFNR, VORNR") == ("AUFNR", "VORNR")
    # a CTE: both select lists are read, the table names between them are not
    cte = ("WITH x AS (SELECT OBJNR, STAT FROM TV_JEST) "
           "SELECT AUFNR, KTEXT FROM TV_AUFK JOIN x ON 1 = 1")
    assert parse_columns(cte) == ("OBJNR", "STAT", "AUFNR", "KTEXT")
    assert parse_columns("SELECT * FROM TV_AFRU") == ()


def test_parse_columns_drops_what_is_not_an_identifier_and_keeps_namespaces_and_tv_names():
    assert parse_columns("1006 MANDT 42 RUECK 3.5 RMZHL. --") == ("MANDT", "RUECK", "RMZHL")
    assert parse_columns("RUECK-MST") == ()                  # a dash is not part of a name
    assert parse_columns('"MANDT" \'RUECK\' `RMZHL`') == ("MANDT", "RUECK", "RMZHL")
    # a BW-style namespace and an underscore start are identifiers
    assert parse_columns("/BIC/ZFOO _TMP /SAPAPO/X") == ("/BIC/ZFOO", "_TMP", "/SAPAPO/X")
    # only TABLE names lose the view prefix — a column called TV_SOMETHING keeps it
    assert parse_columns("TV_AFRU TV_X") == ("TV_AFRU", "TV_X")
    assert normalize_table("TV_AFRU") == "AFRU"
    assert parse_columns("") == () and parse_columns(None) == () and parse_columns("  \n ") == ()
    assert parse_columns("--- , ; ( ) 12 3.4") == ()


def test_parse_columns_de_duplicates_and_keeps_order():
    assert parse_columns("RUECK MANDT rueck RMZHL Mandt") == ("RUECK", "MANDT", "RMZHL")


# ----- parse_information_schema -----

CSV_EXPORT = (
    "COLUMN_NAME,ORDINAL_POSITION,TABLE_NAME,DATA_TYPE\n"
    "RMZHL,2,TV_AFRU,TEXT\n"
    "MANDT,1,TV_AFRU,TEXT\n"
    "RUECK,3,TV_AFRU,TEXT\n"
    "OBJNR,2,TV_JEST,TEXT\n"
    "MANDT,1,TV_JEST,TEXT\n"
    "STAT,3,TV_JEST,TEXT\n"
)


def test_parse_information_schema_reads_a_csv_export_in_any_column_order_and_honours_position():
    out = parse_information_schema(CSV_EXPORT)
    assert out == {"AFRU": ("MANDT", "RMZHL", "RUECK"), "JEST": ("MANDT", "OBJNR", "STAT")}
    assert list(out) == ["AFRU", "JEST"]              # tables in first-seen order


def test_parse_information_schema_reads_a_tab_separated_export_with_lowercase_headers():
    tsv = "table_name\tcolumn_name\nTV_AFRU\tMANDT\nTV_AFRU\tRUECK\nTV_CRTX\tKTEXT\n"
    assert parse_information_schema(tsv) == {"AFRU": ("MANDT", "RUECK"), "CRTX": ("KTEXT",)}
    # without ORDINAL_POSITION the export's own order is the order
    tsv2 = "TABLE_NAME\tCOLUMN_NAME\nTV_AFRU\tRUECK\nTV_AFRU\tMANDT\n"
    assert parse_information_schema(tsv2) == {"AFRU": ("RUECK", "MANDT")}


def test_parse_information_schema_tolerates_quotes_crlf_junk_rows_and_a_title_line():
    text = (
        "Query results\r\n"
        "\r\n"
        '"TABLE_NAME","COLUMN_NAME","ORDINAL_POSITION"\r\n'
        '"TV_AFRU","MANDT","1"\r\n'
        '"tv_afru","rueck","2"\r\n'
        '"TV_AFRU","MANDT","1"\r\n'                       # a duplicate row is one column
        '"TV_AFRU","","3"\r\n'                            # no column name
        '"","XYZ","4"\r\n'                                # no table name
        '"TV_AFRU","RMZHL","not a number"\r\n'            # a bad position sorts last, not lost
        "(6 rows)\r\n"
        "\r\n"
    )
    assert parse_information_schema(text) == {"AFRU": ("MANDT", "RUECK", "RMZHL")}
    # a semicolon export (an EU-locale spreadsheet) and a pipe dump both read
    one = {"AUFK": ("AUFNR",)}
    assert parse_information_schema("TABLE_NAME;COLUMN_NAME\nTV_AUFK;AUFNR\n") == one
    assert parse_information_schema("TABLE_NAME | COLUMN_NAME\nTV_AUFK | AUFNR\n") == one


def test_parse_information_schema_needs_table_name_and_column_name():
    assert parse_information_schema("TABLE,COLUMN\nTV_AFRU,MANDT\n") == {}
    assert parse_information_schema("COLUMN_NAME\nMANDT\n") == {}
    assert parse_information_schema("") == {} and parse_information_schema(None) == {}
    assert parse_information_schema(AFRU_PASTE) == {}
    assert parse_information_schema("TABLE_NAME,COLUMN_NAME\n") == {}


# ----- status / columns / has_column -----

def _overlay() -> EdwOverlay:
    o = EdwOverlay(plant="1006")
    o.record_columns("AFRU", AFRU_PASTE, recorded_at=TODAY)
    o.mark("CRHTX", False, recorded_at=TODAY)
    o.mark("AUFK", True, recorded_at=TODAY)
    return o


def test_status_answers_present_missing_or_unknown_with_the_tv_prefix_tolerated():
    o = _overlay()
    assert o.status("AFRU") == PRESENT == "present"
    assert o.status("TV_AFRU") == o.status("afru") == o.status(" tv_afru ") == PRESENT
    assert o.status("CRHTX") == MISSING == "missing"
    assert o.status("AUFK") == PRESENT                      # marked present, no columns needed
    assert o.status("DD03T") == UNKNOWN == "unknown"
    assert o.status("") == UNKNOWN
    # a hand-built entry whose flag nobody set is unknown, not missing
    o.tables["PLPO"] = EdwTable(name="PLPO")
    assert o.status("PLPO") == UNKNOWN


def test_columns_and_has_column_answer_true_false_or_none():
    o = _overlay()
    assert o.columns("TV_AFRU") == AFRU_COLUMNS
    assert o.columns("AUFK") == () and o.columns("nope") == ()
    assert o.has_column("AFRU", "BUDAT") is True and o.has_column("afru", "budat") is True
    assert o.has_column("AFRU", "ZZOPERATION_TYPE") is True
    assert o.has_column("AFRU", "BUZEIT") is False          # the field HELIX used to invent
    assert o.has_column("AUFK", "AUFNR") is None            # present, but no column list recorded
    assert o.has_column("DD03T", "FIELDNAME") is None       # nobody has said anything
    assert o.has_column("CRHTX", "KTEXT") is False          # a missing table has no columns at all


# ----- custom / missing columns against the dictionary -----

def test_custom_columns_finds_the_sites_own_zz_fields_on_afru():
    o = _overlay()
    assert o.custom_columns("AFRU", AFRU_DICTIONARY) == ("ZZOPERATION_TYPE", "ZZUPDATE_ON_UTC")
    assert o.custom_columns("TV_AFRU", tuple(f.lower() for f in AFRU_DICTIONARY)) == (
        "ZZOPERATION_TYPE", "ZZUPDATE_ON_UTC")
    assert o.custom_columns("AFRU", AFRU_COLUMNS) == ()
    assert o.custom_columns("AUFK", ("MANDT", "AUFNR")) == ()      # no columns recorded → no answer
    assert o.custom_columns("AFRU", ()) == ()                       # no dictionary → nothing to say


def test_missing_columns_lists_dictionary_fields_the_edw_did_not_replicate():
    o = _overlay()
    dictionary = ("MANDT", "RUECK", "RMZHL", "BUZEIT", "GHOST", "ghost", "BUDAT")
    assert o.missing_columns("AFRU", dictionary) == ("BUZEIT", "GHOST")
    assert o.missing_columns("AFRU", AFRU_DICTIONARY) == ()
    assert o.missing_columns("AUFK", ("MANDT", "AUFNR")) == ()     # recorded columns only
    assert o.missing_columns("CRHTX", ("KTEXT",)) == ()
    assert o.missing_columns("AFRU", ()) == ()


# ----- record / mark / forget -----

def test_record_columns_takes_a_paste_or_a_list_and_refuses_an_empty_one():
    o = EdwOverlay()
    t = o.record_columns("tv_afru", "mandt, rueck\nrmzhl", recorded_at=TODAY)
    assert t == EdwTable(name="AFRU", present=True, columns=("MANDT", "RUECK", "RMZHL"),
                         recorded_at=TODAY, source="pasted")
    assert o.tables["AFRU"] is t and list(o.tables) == ["AFRU"]
    t2 = o.record_columns("AUFK", ["mandt", "AUFNR", "aufnr", "42", ""], recorded_at=TODAY,
                          source="information_schema")
    assert t2.columns == ("MANDT", "AUFNR") and t2.present is True
    assert t2.source == "information_schema"
    # a paste with nothing in it must not wipe a recorded list
    with pytest.raises(ValueError):
        o.record_columns("AFRU", "1006 -- ()", recorded_at=TODAY)
    with pytest.raises(ValueError):
        o.record_columns("AFRU", [], recorded_at=TODAY)
    with pytest.raises(ValueError):
        o.record_columns("", "MANDT", recorded_at=TODAY)
    assert o.columns("AFRU") == ("MANDT", "RUECK", "RMZHL")
    # recording again replaces the list outright
    again = o.record_columns("AFRU", "MANDT RUECK", recorded_at="2026-09-16")
    assert again.columns == ("MANDT", "RUECK") and again.recorded_at == "2026-09-16"


def test_mark_missing_drops_columns_and_mark_present_keeps_them():
    o = EdwOverlay()
    gone = o.mark("TV_CRHTX", False, recorded_at=TODAY)
    assert gone == EdwTable(name="CRHTX", present=False, recorded_at=TODAY, source="user")
    assert o.status("CRHTX") == MISSING
    o.record_columns("AFRU", AFRU_PASTE, recorded_at=TODAY, source="information_schema")
    # "AFRU exists" on a table with a recorded list keeps the list and where it came from
    kept = o.mark("AFRU", True, recorded_at="2026-09-16")
    assert kept.columns == AFRU_COLUMNS and kept.source == "information_schema"
    assert kept.recorded_at == "2026-09-16" and kept.present is True
    # "AFRU is gone" drops the list — has_column must not keep saying True
    dropped = o.mark("AFRU", False, recorded_at="2026-09-17")
    assert dropped.columns == () and dropped.source == "user"
    assert o.has_column("AFRU", "BUDAT") is False
    # marking it present again after that is a bare flag: the columns are not resurrected
    back = o.mark("AFRU", True, recorded_at="2026-09-18", source="seed")
    assert back.columns == () and back.source == "seed" and o.has_column("AFRU", "BUDAT") is None
    with pytest.raises(ValueError):
        o.mark("", True, recorded_at=TODAY)


def test_forget_removes_a_table_and_says_whether_it_was_there():
    o = _overlay()
    assert o.forget("tv_afru") is True
    assert o.forget("AFRU") is False and o.forget("") is False and o.forget("DD03T") is False
    assert o.status("AFRU") == UNKNOWN and o.columns("AFRU") == ()
    assert o.has_column("AFRU", "BUDAT") is None
    assert set(o.tables) == {"CRHTX", "AUFK"}


# ----- persistence -----

def test_the_overlay_round_trips_through_its_document():
    o = _overlay()
    doc = o.to_dict()
    assert doc == {
        "prefix": "EDW.SRC_SAPECC_ARP", "view_prefix": "TV_", "plant": "1006",
        "tables": {
            "AFRU": {"present": True, "columns": list(AFRU_COLUMNS), "recorded_at": TODAY,
                     "source": "pasted"},
            "AUFK": {"present": True, "recorded_at": TODAY, "source": "user"},
            "CRHTX": {"present": False, "recorded_at": TODAY, "source": "user"},
        },
    }
    assert list(doc["tables"]) == ["AFRU", "AUFK", "CRHTX"]     # sorted: the file diffs cleanly
    # through JSON and back, both directions
    back = EdwOverlay.from_dict(json.loads(json.dumps(doc)))
    assert back == o
    assert back.to_dict() == doc
    assert back.tables["AFRU"].columns == AFRU_COLUMNS                # tuples again, not lists
    assert back.custom_columns("AFRU", AFRU_DICTIONARY) == ("ZZOPERATION_TYPE", "ZZUPDATE_ON_UTC")
    # a custom naming survives too, including an EDW whose views carry no prefix at all
    bare = EdwOverlay(prefix="PROD.SAP", view_prefix="", plant="")
    assert EdwOverlay.from_dict(bare.to_dict()) == bare


def test_a_corrupt_or_foreign_document_reads_as_empty_defaults():
    empty = EdwOverlay()
    for junk in (None, "{not json", 42, [], ["AFRU"], {}, {"facts": []}, {"tables": "AFRU"},
                 {"tables": ["AFRU"]},
                 {"prefix": None, "view_prefix": 7, "plant": None, "tables": None}):
        got = EdwOverlay.from_dict(junk)
        assert got == empty, junk
        assert got.prefix == "EDW.SRC_SAPECC_ARP" and got.view_prefix == "TV_" and got.plant == ""
        assert got.tables == {} and got.status("AFRU") == UNKNOWN
    # junk entries are skipped, the good ones kept; loose shapes are read for what they mean
    got = EdwOverlay.from_dict({
        "prefix": "  EDW.SRC_SAPECC_ARP ", "plant": 1006,
        "tables": {
            "": {"present": True},                                   # no name
            "TV_AFRU": {"present": "true", "columns": "MANDT RUECK"},  # a paste, hand-edited in
            "tv_jest": {"columns": ["mandt", "OBJNR", 3, None, "STAT", "STAT"]},  # flag implied
            "CRHTX": False,                                          # a bare flag
            "AUFK": ["MANDT", "AUFNR"],                              # a bare list
            "PLPO": 42, "PLKO": None, "MAKT": "present",             # not entries at all
            "RESB": {"present": False, "columns": ["MANDT"]},        # contradiction: the flag wins
            "MARA": {"present": "maybe", "recorded_at": None, "source": 5},
        },
    })
    assert got.prefix == "EDW.SRC_SAPECC_ARP" and got.plant == "1006" and got.view_prefix == "TV_"
    assert set(got.tables) == {"AFRU", "JEST", "CRHTX", "AUFK", "RESB", "MARA"}
    assert got.status("AFRU") == PRESENT and got.columns("AFRU") == ("MANDT", "RUECK")
    assert got.tables["JEST"] == EdwTable(name="JEST", present=True,
                                          columns=("MANDT", "OBJNR", "STAT"))
    assert got.status("CRHTX") == MISSING
    assert got.tables["AUFK"] == EdwTable(name="AUFK", present=True, columns=("MANDT", "AUFNR"))
    assert got.tables["RESB"] == EdwTable(name="RESB", present=False)
    assert got.tables["MARA"] == EdwTable(name="MARA", present=None, recorded_at="", source="5")
    assert got.status("MARA") == UNKNOWN
