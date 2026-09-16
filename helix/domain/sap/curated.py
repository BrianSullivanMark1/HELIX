"""The expert layer — what SAP's dictionary does NOT say, written down once, with its keys.

The dictionary (model.py) knows fields and declared foreign keys. It does not know that an order's
statuses live in JEST joined on OBJNR, that AFRU.ARBID is CRHD.OBJID (with OBJTY='A'), that a WBS
element reaches an order through AFPO.PROJN, or that JEST needs INACT='' and TJ02T needs SPRAS='E'
(one character — 'EN' returns nothing). Every SAP reporting bug the user hit today was one of these.
So this module holds them as DATA the join engine and the SQL writer consume: curated join edges
with their full compound keys, the standard filters, plain-English notes, the business vocabulary
("posting date" → AFRU.BUDAT), reusable SQL recipes, and the catalog's scope.

Every edge starts `verified=False`; the verification pass flips it after confirming both sides'
fields against the dictionary. Nothing here is recited to the user as fact unless it is either
dictionary-backed or marked verified — an unverified edge is offered as "curated, unverified".

Pure data, no I/O. Keep entries short and sourced; a wrong join here is the bug this layer exists
to end.
"""
from __future__ import annotations

from dataclasses import dataclass

from helix.domain.sap.model import Filter, JoinEdge

# The language key SAP stores: ONE character. English is 'E' (German 'D'). ISO 'EN' lives only in
# T002.LAISO — a text-table filter on 'EN' silently returns no rows.
LANGUAGE = "E"

# Production orders in AUFK — the table also holds internal, maintenance and process orders.
PRODUCTION_ORDER_TYPE = "10"


def _f(table: str, field: str, value: str, why: str, *, op: str = "=",
       optional: bool = False) -> Filter:
    return Filter(table=table, field=field, op=op, value=value, why=why, optional=optional)


# ----- standard filters: added whenever the table takes part in a query -----
# `optional=True` marks a sensible default the user may drop (a plant filter is the user's own and
# is not listed here — the SQL writer adds it from the EDW overlay's plant). Language filters for
# text tables are NOT listed: the writer applies SPRAS = LANGUAGE to any table whose key has a LANG
# field (dictionary-driven, so it is never missed on a table nobody thought of).
STANDARD_FILTERS: dict[str, tuple[Filter, ...]] = {
    "AUFK": (
        _f("AUFK", "AUTYP", PRODUCTION_ORDER_TYPE,
           "production orders only — AUFK also holds internal orders (01), networks (20), "
           "maintenance orders (30) and process orders (40)", optional=True),
        _f("AUFK", "LOEKZ", "", "orders not flagged for deletion", optional=True),
    ),
    "JEST": (
        _f("JEST", "INACT", "", "active statuses only — JEST keeps every status ever set on the "
           "object; INACT='X' marks the ones no longer active"),
    ),
    "CRHD": (
        _f("CRHD", "OBJTY", "A", "work centers only — CRHD also holds other capacity objects; "
           "OBJTY 'A' is a work center"),
    ),
    "AFRU": (
        _f("AFRU", "STOKZ", "", "leave out reversal documents (STOKZ='X' is a reversal)",
           optional=True),
        _f("AFRU", "STZHL", "00000000", "leave out confirmations that were later reversed "
           "(STZHL points at the reversing confirmation)", optional=True),
    ),
    "AFVC": (
        _f("AFVC", "LOEKZ", "", "operations not deleted from the order", optional=True),
    ),
    "RESB": (
        _f("RESB", "XLOEK", "", "components not deleted from the order", optional=True),
    ),
    "MARC": (
        _f("MARC", "LVORM", "", "material not flagged for deletion at the plant", optional=True),
    ),
    "MARA": (
        _f("MARA", "LVORM", "", "material not flagged for deletion", optional=True),
    ),
    "PRPS": (
        _f("PRPS", "LOEVM", "", "WBS elements not flagged for deletion", optional=True),
    ),
    "EKPO": (
        _f("EKPO", "LOEKZ", "", "purchase order items not deleted (LOEKZ 'L' is deleted)",
           optional=True),
    ),
}


def _j(left: str, right: str, on, card: str = "", *, kind: str = "curated", note: str = "",
       filters: tuple[Filter, ...] = (), source: str = "curated") -> JoinEdge:
    pairs = tuple((p, p) if isinstance(p, str) else (p[0], p[1]) for p in on)
    return JoinEdge(left=left, right=right, on=pairs, kind=kind, cardinality=card,
                    filters=filters, note=note, source=source, verified=False)


# ----- curated joins: (left, right, ON pairs, cardinality left:right) -----
# ON pairs: a bare 'FIELD' means the same name both sides; ('LEFT_FIELD', 'RIGHT_FIELD') otherwise.
# MANDT is written out so a compound key is never silently missing its client column.
CURATED_JOINS: tuple[JoinEdge, ...] = (
    # --- the order: header, PP header, items, operations, confirmations, components ---
    _j("AUFK", "AFKO", ("MANDT", "AUFNR"), "1:1",
       note="every order header (AUFK) has one AFKO row with its PP data: routing number AUFPL, "
            "reservation RSNUM, material PLNBEZ, quantities and dates"),
    _j("AUFK", "AFPO", ("MANDT", "AUFNR"), "1:n",
       note="order items — a production order normally has one (POSNR '0001'): the material made, "
            "order and delivered quantity, batch, WBS element (PROJN)"),
    _j("AFKO", "AFVC", ("MANDT", "AUFPL"), "1:n",
       note="the order's operations hang off AFKO.AUFPL (the routing number of the order); "
            "AFVC's key is AUFPL + APLZL"),
    _j("AFVC", "AFVV", ("MANDT", "AUFPL", "APLZL"), "1:1",
       note="quantities, dates and standard values of the operation"),
    _j("AFVC", "AFRU", ("MANDT", "AUFPL", "APLZL"), "1:n",
       note="all confirmations posted on the operation — BOTH columns, never AUFPL alone (every "
            "operation of the order shares the AUFPL)"),
    _j("AFVC", "AFRU", ("MANDT", "RUECK"), "1:n",
       note="the direct route: AFVC.RUECK is the confirmation number of the operation and AFRU's "
            "key is RUECK + RMZHL (one row per confirmation posted)"),
    _j("AFRU", "AUFK", ("MANDT", "AUFNR"), "n:1",
       note="the order a confirmation belongs to (AFRU carries the order number)"),
    _j("AFRU", "AFKO", ("MANDT", "AUFNR"), "n:1",
       note="the PP header of the confirmed order"),
    _j("AFKO", "RESB", ("MANDT", "RSNUM"), "1:n",
       note="the order's components: the reservation RSNUM on AFKO; RESB's key is RSNUM + RSPOS + "
            "RSART. BDMNG is the requirement, ENMNG the quantity already withdrawn"),
    _j("RESB", "AFVC", ("MANDT", "AUFPL", "APLZL"), "n:1",
       note="the operation a component is assigned to"),
    _j("RESB", "AUFK", ("MANDT", "AUFNR"), "n:1", note="the order a component belongs to"),
    # --- work centers ---
    _j("AFRU", "CRHD", ("MANDT", ("ARBID", "OBJID")), "n:1",
       filters=STANDARD_FILTERS["CRHD"],
       note="the work center a confirmation was posted at: ARBID is the internal object id; "
            "CRHD.ARBPL is the work center key people read. CRHD has no description — that is "
            "CRTX"),
    _j("AFVC", "CRHD", ("MANDT", ("ARBID", "OBJID")), "n:1", filters=STANDARD_FILTERS["CRHD"],
       note="the work center of the operation (AFVC.ARBID = CRHD.OBJID)"),
    _j("PLPO", "CRHD", ("MANDT", ("ARBID", "OBJID")), "n:1", filters=STANDARD_FILTERS["CRHD"],
       note="the work center of a routing operation — PLPO stores ARBID (internal id), not ARBPL"),
    _j("CRHD", "CRTX", ("MANDT", "OBJTY", "OBJID"), "1:n", kind="text",
       note="work center description: CRTX.KTEXT (short), one row per language — filter SPRAS. "
            "(There is no CRHTX; the text table is CRTX)"),
    _j("CRHD", "CRCA", ("MANDT", "OBJTY", "OBJID"), "1:n",
       note="capacity assignment of the work center"),
    _j("CRCA", "KAKO", ("MANDT", "KAPID"), "n:1", note="capacity header"),
    _j("AFRU", "KAKO", ("MANDT", "KAPID"), "n:1",
       note="the capacity the confirmation was posted on"),
    _j("KAKO", "KAKT", ("MANDT", "KAPID"), "1:n", kind="text", note="capacity description"),
    _j("AFVC", "KBED", ("MANDT", "BEDID"), "1:n",
       note="capacity requirements of the operation (KBED key BEDID + BEDZL)"),
    _j("KBED", "KAKO", ("MANDT", "KAPID"), "n:1", note="the capacity a requirement loads"),
    # --- statuses ---
    _j("AUFK", "JEST", ("MANDT", "OBJNR"), "1:n", filters=STANDARD_FILTERS["JEST"],
       note="the order's statuses: system statuses (STAT 'I0002' = REL, 'I0009' = CNF …) and user "
            "statuses ('E….'); AUFK.OBJNR is 'OR' + the order number"),
    _j("AFVC", "JEST", ("MANDT", "OBJNR"), "1:n", filters=STANDARD_FILTERS["JEST"],
       note="the operation's statuses (AFVC has its own status object OBJNR)"),
    _j("QMEL", "JEST", ("MANDT", "OBJNR"), "1:n", filters=STANDARD_FILTERS["JEST"],
       note="the notification's statuses"),
    _j("PRPS", "JEST", ("MANDT", "OBJNR"), "1:n", filters=STANDARD_FILTERS["JEST"],
       note="the WBS element's statuses"),
    _j("JEST", "TJ02T", (("STAT", "ISTAT"),), "n:1", kind="text",
       note="system status texts: TJ02T.TXT04 is the 4-letter code shown on screen (REL, CNF, "
            "PCNF, TECO, DLV…), TXT30 the long text. TJ02T's key is ISTAT + SPRAS and it has NO "
            "MANDT (client-independent) — join STAT = ISTAT, filter SPRAS only; user statuses "
            "(STAT starting 'E') do not match here, they need TJ30T"),
    _j("AUFK", "JSTO", ("MANDT", "OBJNR"), "1:1",
       note="the status object: JSTO.STSMA is the user-status profile the object uses"),
    _j("JEST", "TJ30T", ("MANDT", ("STAT", "ESTAT")), "n:1", kind="text",
       note="user status texts — TJ30T's key is MANDT + STSMA + ESTAT + SPRAS, so the profile "
            "must also match: AND TJ30T.STSMA = JSTO.STSMA (JSTO joined on the same OBJNR). The "
            "'user_status' recipe writes this correctly"),
    # --- WBS / project ---
    _j("AFPO", "PRPS", ("MANDT", ("PROJN", "PSPNR")), "n:1", filters=STANDARD_FILTERS["PRPS"],
       note="the WBS element of the order item: AFPO.PROJN is the internal WBS number; "
            "PRPS.POSID is the WBS id you see on screen and POST1 its description"),
    _j("AUFK", "PRPS", ("MANDT", ("PSPEL", "PSPNR")), "n:1", filters=STANDARD_FILTERS["PRPS"],
       note="the WBS element assigned on the order header (AUFK.PSPEL); often the same as the "
            "item's PROJN — check both"),
    _j("AFVC", "PRPS", ("MANDT", ("PROJN", "PSPNR")), "n:1", filters=STANDARD_FILTERS["PRPS"],
       note="a WBS element on the operation itself (rare; usually blank)"),
    _j("PRPS", "PROJ", ("MANDT", ("PSPHI", "PSPNR")), "n:1",
       note="the project definition the WBS element belongs to: PROJ.PSPID is the project id, "
            "PROJ.POST1 its description (a 'program name' is usually this)"),
    _j("AUFK", "COBRB", ("MANDT", "OBJNR"), "1:n",
       note="settlement rule lines of the order; KONTY says what receives the costs "
            "('PSP' = WBS element, 'KS' cost center, 'OR' order, 'MAT' material)"),
    _j("COBRB", "PRPS", ("MANDT", ("PS_PSP_PNR", "PSPNR")), "n:1",
       note="the WBS element receiving the settlement (COBRB.KONTY = 'PSP')",
       filters=(_f("COBRB", "KONTY", "PSP", "WBS receivers only"),)),
    # --- material ---
    _j("AFPO", "MARA", ("MANDT", "MATNR"), "n:1", note="the material master of what is being made"),
    _j("AFPO", "MARC", ("MANDT", "MATNR", ("DWERK", "WERKS")), "n:1",
       note="plant data of the material (DWERK is the order item's plant): MRP controller "
            "DISPO, production scheduler FEVOR, procurement type BESKZ"),
    _j("AFPO", "MAKT", ("MANDT", "MATNR"), "1:n", kind="text",
       note="material description MAKT.MAKTX — one row per language, filter SPRAS"),
    _j("AFKO", "MARA", ("MANDT", ("PLNBEZ", "MATNR")), "n:1",
       note="the order's material is AFKO.PLNBEZ (same as AFPO.MATNR on the item)"),
    _j("AFKO", "MAKT", ("MANDT", ("PLNBEZ", "MATNR")), "1:n", kind="text",
       note="material description for the order's material"),
    _j("MARA", "MAKT", ("MANDT", "MATNR"), "1:n", kind="text", note="material description"),
    _j("MARA", "MARC", ("MANDT", "MATNR"), "1:n", note="plant-level data, one row per plant"),
    _j("MARC", "MARD", ("MANDT", "MATNR", "WERKS"), "1:n",
       note="storage-location stock: MARD.LABST is unrestricted stock, INSME in inspection"),
    _j("MARC", "MBEW", ("MANDT", "MATNR", ("WERKS", "BWKEY")), "1:1",
       note="valuation (price VERPR/STPRS) — BWKEY is the valuation area, the plant when "
            "valuation is at plant level"),
    _j("MARC", "T024D", ("MANDT", "WERKS", "DISPO"), "n:1",
       note="the MRP controller's name: T024D.DSNAM (no language key on T024D)"),
    _j("MARC", "T024F", ("MANDT", "WERKS", "FEVOR"), "n:1",
       note="the production scheduler's name (T024F.TXT)"),
    _j("MARC", "MAPL", ("MANDT", "MATNR", "WERKS"), "1:n",
       note="routings assigned to the material at the plant"),
    _j("MARA", "T134", ("MANDT", "MTART"), "n:1", note="material type"),
    _j("MARA", "T023", ("MANDT", "MATKL"), "n:1", note="material group"),
    _j("RESB", "MARA", ("MANDT", "MATNR"), "n:1", note="the component material"),
    _j("RESB", "MAKT", ("MANDT", "MATNR"), "1:n", kind="text", note="component description"),
    _j("RESB", "MARD", ("MANDT", "MATNR", "WERKS", "LGORT"), "n:1",
       note="stock at the component's storage location (shortage = BDMNG − ENMNG vs LABST)"),
    _j("AFPO", "MCHA", ("MANDT", "MATNR", ("DWERK", "WERKS"), "CHARG"), "n:1",
       note="the batch's plant-level master record (AFPO.CHARG)"),
    _j("AFPO", "MCH1", ("MANDT", "MATNR", "CHARG"), "n:1", note="the batch master record"),
    _j("MCHA", "MCHB", ("MANDT", "MATNR", "WERKS", "CHARG"), "1:n",
       note="batch stock per storage location"),
    # --- planned orders, MRP ---
    _j("AFPO", "PLAF", ("MANDT", "PLNUM"), "n:1",
       note="the planned order the production order was converted from (AFPO.PLNUM)"),
    _j("PLAF", "MARC", ("MANDT", "MATNR", ("PLWRK", "WERKS")), "n:1",
       note="plant data of the planned material"),
    _j("MDKP", "MDTB", ("MANDT", "DTNUM"), "1:n",
       note="MRP list items: MDTB.AUSSL is the exception (MRP message) key, DELKZ the element "
            "type ('FE' production order, 'PA' planned order, 'BE' purchase order, 'BA' purchase "
            "requisition), DELNR its number, DAT00 its date"),
    _j("MDKP", "MARC", ("MANDT", "MATNR", ("PLWRK", "WERKS")), "n:1",
       note="the material/plant the MRP list belongs to (MDKP.DTART 'MD' is the MRP list)"),
    # The dictionary, not memory: MDTB carries the exception KEY (AUSSL), T458B its text (AUSLT,
    # keyed MANDT + SPRAS + AUSSL); the message NUMBER (AUSKT) lives on T458A only.
    _j("MDTB", "T458B", ("MANDT", "AUSSL"), "n:1", kind="text",
       note="MRP exception message texts: T458B.AUSLT per language (filter SPRAS)"),
    _j("AFPO", "MKAL", ("MANDT", "MATNR", ("DWERK", "WERKS"), "VERID"), "n:1",
       note="the production version used (AFPO.VERID)"),
    # --- routing / BOM ---
    _j("AFKO", "PLKO", ("MANDT", "PLNTY", "PLNNR", "PLNAL"), "n:1",
       note="the routing the order was created from (PLKO also has ZAEHL + DATUV: several change "
            "states — take the one valid on the order date)"),
    _j("PLKO", "PLAS", ("MANDT", "PLNTY", "PLNNR", "PLNAL"), "1:n",
       note="the routing's operation sequence assignments (PLNFL sequence, PLNKN operation node)"),
    _j("PLAS", "PLPO", ("MANDT", "PLNTY", "PLNNR", "PLNKN"), "n:1",
       note="the routing operation (PLPO key PLNTY + PLNNR + PLNKN + ZAEHL)"),
    _j("MAPL", "PLKO", ("MANDT", "PLNTY", "PLNNR", "PLNAL"), "n:1", note="the routing header"),
    _j("AFVC", "PLPO", ("MANDT", "PLNTY", "PLNNR", "PLNKN"), "n:1",
       note="the routing operation the order operation was copied from"),
    _j("AFVC", "T430", ("MANDT", "STEUS"), "n:1",
       note="the control key (confirmation required? external processing? scheduling?)"),
    _j("PLPO", "T430", ("MANDT", "STEUS"), "n:1", note="the control key"),
    _j("T430", "T430T", ("MANDT", "STEUS"), "1:n", kind="text", note="control key text"),
    _j("MAST", "STKO", ("MANDT", "STLNR", "STLAL"), "n:1",
       filters=(_f("STKO", "STLTY", "M", "material BOMs (STKO also holds equipment/order BOMs)"),),
       note="the BOM header for the material's BOM link (MAST: MATNR + WERKS + STLAN usage)"),
    _j("STKO", "STAS", ("MANDT", "STLTY", "STLNR", "STLAL"), "1:n",
       note="item selection — the way from a header to its items"),
    _j("STAS", "STPO", ("MANDT", "STLTY", "STLNR", "STLKN"), "n:1",
       note="the BOM item: STPO.IDNRK is the component material, MENGE its quantity"),
    _j("STPO", "MARA", ("MANDT", ("IDNRK", "MATNR")), "n:1", note="the component material"),
    # --- serial numbers ---
    _j("AFPO", "SER05", ("MANDT", ("AUFNR", "PPAUFNR"), ("POSNR", "PPPOSNR")), "1:1",
       note="the serial-number object list of the order item — SER05 spells the order "
            "PPAUFNR / PPPOSNR, not AUFNR / POSNR"),
    _j("SER05", "OBJK", ("MANDT", "OBKNR"), "1:n",
       note="the serial numbers themselves: OBJK.SERNR (and MATNR, EQUNR)"),
    _j("SER03", "OBJK", ("MANDT", "OBKNR"), "1:n",
       note="serial numbers on a goods movement (SER03: MBLNR + MJAHR + ZEILE)"),
    _j("SER01", "OBJK", ("MANDT", "OBKNR"), "1:n", note="serial numbers on a delivery item"),
    _j("OBJK", "EQUI", ("MANDT", "EQUNR"), "n:1",
       note="the equipment record behind a serial number (serialized materials are equipment)"),
    _j("EQUI", "EQKT", ("MANDT", "EQUNR"), "1:n", kind="text", note="equipment description EQKTX"),
    # --- goods movements ---
    _j("AFKO", "AUFM", ("MANDT", "AUFNR"), "1:n",
       note="goods movements posted for the order (issues and receipts): BWART movement type, "
            "MENGE quantity, MBLNR/MJAHR/ZEILE the material document line"),
    _j("AUFM", "MSEG", ("MANDT", "MBLNR", "MJAHR", "ZEILE"), "1:1",
       note="the material document line"),
    _j("MKPF", "MSEG", ("MANDT", "MBLNR", "MJAHR"), "1:n",
       note="material document header → lines (both columns: document numbers repeat across "
            "years)"),
    _j("MSEG", "AUFK", ("MANDT", "AUFNR"), "n:1", note="the order a movement was posted for"),
    _j("MSEG", "T156", ("MANDT", "BWART"), "n:1",
       note="movement type (261 issue to order, 101 receipt…)"),
    _j("T156", "T156T", ("MANDT", "BWART"), "1:n", kind="text", note="movement type text"),
    # --- purchasing (external processing, shortages) ---
    _j("EKKO", "EKPO", ("MANDT", "EBELN"), "1:n", note="purchase order items"),
    _j("EKPO", "EKET", ("MANDT", "EBELN", "EBELP"), "1:n",
       note="schedule lines: EINDT delivery date, MENGE"),
    _j("EKPO", "EKBE", ("MANDT", "EBELN", "EBELP"), "1:n",
       note="PO history: goods receipts (VGABE '1'), invoices ('2')"),
    _j("EKPO", "EKKN", ("MANDT", "EBELN", "EBELP"), "1:n",
       note="account assignment: the order (AUFNR) or WBS element (PS_PSP_PNR) a PO item is for"),
    _j("EKKO", "LFA1", ("MANDT", "LIFNR"), "n:1", note="the vendor"),
    _j("EKPO", "MARA", ("MANDT", "MATNR"), "n:1", note="the ordered material"),
    _j("EKPO", "MARC", ("MANDT", "MATNR", "WERKS"), "n:1",
       note="the ordered material at the receiving plant"),
    _j("EKPO", "EBAN", ("MANDT", "BANFN", "BNFPO"), "n:1",
       note="the purchase requisition an item came from"),
    _j("AFVC", "LFA1", ("MANDT", "LIFNR"), "n:1",
       note="the vendor of an externally processed operation"),
    # --- sales ---
    _j("VBAK", "VBAP", ("MANDT", "VBELN"), "1:n", note="sales order items"),
    _j("VBAP", "VBEP", ("MANDT", "VBELN", "POSNR"), "1:n", note="schedule lines"),
    _j("AFPO", "VBAP", ("MANDT", ("KDAUF", "VBELN"), ("KDPOS", "POSNR")), "n:1",
       note="the sales order item a make-to-order production order is for (AFPO.KDAUF/KDPOS)"),
    _j("VBAK", "KNA1", ("MANDT", "KUNNR"), "n:1", note="the customer"),
    # --- quality notifications ---
    _j("QMEL", "QMFE", ("MANDT", "QMNUM"), "1:n", note="notification items (defects)"),
    _j("QMEL", "QMMA", ("MANDT", "QMNUM"), "1:n", note="activities"),
    _j("QMEL", "QMSM", ("MANDT", "QMNUM"), "1:n", note="tasks"),
    _j("QMFE", "QMUR", ("MANDT", "QMNUM", "FENUM"), "1:n", note="causes of a defect item"),
    _j("QMEL", "AUFK", ("MANDT", "AUFNR"), "n:1", note="the order the notification refers to"),
    _j("QMEL", "MARA", ("MANDT", "MATNR"), "n:1", note="the material"),
    # QMEL has no EQUNR: the technical object of a notification lives on QMIH (one row per
    # notification), and the vendor field on QMEL is LIFNUM, not LIFNR.
    _j("QMEL", "QMIH", ("MANDT", "QMNUM"), "1:1",
       note="the notification's technical-object data: equipment EQUNR, location ILOAN, "
            "assembly BAUTL"),
    _j("QMIH", "EQUI", ("MANDT", "EQUNR"), "n:1", note="the equipment / serialized unit"),
    _j("QMEL", "TQ80", ("MANDT", "QMART"), "n:1", note="notification type (Q1, Q2, Q3, F2, F3 …)"),
    _j("TQ80", "TQ80_T", ("MANDT", "QMART"), "1:n", kind="text", note="notification type text"),
    _j("QMEL", "LFA1", ("MANDT", ("LIFNUM", "LIFNR")), "n:1",
       note="the vendor on a vendor complaint (QMEL.LIFNUM)"),
    # --- organisation, texts, people ---
    _j("AUFK", "T001W", ("MANDT", "WERKS"), "n:1", note="plant name T001W.NAME1"),
    # T003O / T003P (and ADRP below) spell the client column CLIENT, CDHDR / CDPOS spell it
    # MANDANT — a join on MANDT there is a column that does not exist.
    _j("AUFK", "T003O", (("MANDT", "CLIENT"), "AUART"), "n:1",
       note="order type (T003O's client column is CLIENT)"),
    _j("T003O", "T003P", ("CLIENT", "AUART"), "1:n", kind="text", note="order type text"),
    _j("AUFK", "AFIH", ("MANDT", "AUFNR"), "1:1",
       note="the maintenance-order header (PM/CS orders): equipment EQUNR, location ILOAN, "
            "PRIOK priority — production orders have no PRIOK"),
    _j("AFIH", "EQUI", ("MANDT", "EQUNR"), "n:1", note="the equipment a maintenance order is for"),
    _j("AFRU", "TRUG", ("MANDT", "WERKS", "GRUND"), "n:1", note="variance reason"),
    _j("TRUG", "TRUGT", ("MANDT", "WERKS", "GRUND"), "1:n", kind="text",
       note="variance reason text"),
    _j("AFRU", "T006", ("MANDT", ("GMEIN", "MSEHI")), "n:1", note="unit of the confirmed quantity"),
    _j("T006", "T006A", ("MANDT", "MSEHI"), "1:n", kind="text", note="unit texts"),
    _j("AFRU", "PA0002", ("MANDT", "PERNR"), "n:1",
       note="the person who did the work (personnel number): VORNA / NACHN — PA0002 is "
            "time-dependent, take ENDDA = '99991231'",
       filters=(_f("PA0002", "ENDDA", "99991231", "the current personal-data record"),)),
    _j("AUFK", "USR21", ("MANDT", ("ERNAM", "BNAME")), "n:1",
       note="the SAP user who created the order → USR21.PERSNUMBER → ADRP for the name"),
    _j("AFRU", "USR21", ("MANDT", ("ERNAM", "BNAME")), "n:1",
       note="the user who entered the confirmation"),
    _j("USR21", "ADRP", (("MANDT", "CLIENT"), "PERSNUMBER"), "n:1",
       note="the user's name: ADRP.NAME_TEXT (client column CLIENT; the key also has DATE_FROM "
            "and NATION — take the international version)",
       filters=(_f("ADRP", "NATION", "", "the international address version — ADRP keeps one "
                   "row per version"),)),
    _j("CDHDR", "CDPOS", ("MANDANT", "OBJECTCLAS", "OBJECTID", "CHANGENR"), "1:n",
       note="change document lines (FNAME field, VALUE_OLD/VALUE_NEW); the client column is "
            "MANDANT on both"),
    _j("AUFK", "CSKS", ("MANDT", "KOKRS", ("KOSTV", "KOSTL")), "n:1",
       note="the responsible cost center (CSKS is time-dependent: also DATBI ≥ today)"),
    _j("CSKS", "CSKT", ("MANDT", "KOKRS", "KOSTL", "DATBI"), "1:n", kind="text",
       note="cost center text"),
    _j("AFRU", "CSLA", ("MANDT", ("LEARR", "LSTAR")), "n:1",
       note="the activity type confirmed (CSLA is time-dependent: KOKRS + LSTAR + DATBI)"),
)


# ----- notes: what a table IS, in the words HELIX uses -----
TABLE_NOTES: dict[str, str] = {
    "AUFK": "Order master — one row per order of every kind (production, process, maintenance, "
            "internal). The anchor: AUFNR, type AUART/AUTYP, plant WERKS, description KTEXT, and "
            "OBJNR, the status object every status join hangs off.",
    "AFKO": "The PP header of an order: routing number AUFPL (operations hang off it), reservation "
            "RSNUM (components), material PLNBEZ, order quantity GAMNG, dates (basic GSTRP/GLTRP, "
            "scheduled GSTRS/GLTRS, actual GSTRI/GLTRI, release FTRMI), MRP controller DISPO.",
    "AFPO": "Order item — what the order produces: material MATNR, plant DWERK, quantity PSMNG, "
            "delivered WEMNG, batch CHARG, WBS element PROJN, sales order KDAUF/KDPOS, planned "
            "order PLNUM.",
    "AFVC": "Order operation — one row per operation: number VORNR, text LTXA1, work center ARBID "
            "(= CRHD.OBJID), control key STEUS, confirmation number RUECK, status object OBJNR. "
            "Key AUFPL + APLZL.",
    "AFVV": "Operation quantities and dates: earliest/latest start and finish (FSAVD…SSEDD), "
            "actual start/finish (ISDD/IEDD), operation quantity MGVRG, confirmed yield LMNGA and "
            "scrap XMNGA, standard values VGW01–06.",
    "AFRU": "Confirmations — every time work is booked on an operation: posting date BUDAT, entry "
            "date/time ERSDA/ERZET, yield LMNGA, scrap XMNGA, work ISMNW, actual start/finish "
            "ISDD/IEDD, work center ARBID, person PERNR. Reversals: STOKZ / STZHL. There is no "
            "posting TIME on AFRU — ERZET is the entry time.",
    "RESB": "Reservations / order components: material MATNR, requirement BDMNG, withdrawn ENMNG, "
            "storage location LGORT, the operation AUFPL/APLZL it belongs to.",
    "CRHD": "Work center master: ARBPL is the work center key, OBJID the internal id that AFVC, "
            "AFRU and PLPO point at (ARBID). No description here — CRTX has it.",
    "CRTX": "Work center descriptions per language (KTEXT).",
    "JEST": "Individual object status: one row per status ever set on an object (OBJNR); "
            "INACT = 'X' when no longer active. System statuses start with 'I', user statuses 'E'.",
    "JSTO": "Status object header: which user-status profile (STSMA) an object uses.",
    "TJ02T": "System status texts (client-independent): ISTAT → TXT04 (REL, CNF, TECO…).",
    "TJ30T": "User status texts per profile: STSMA + ESTAT → TXT04.",
    "PRPS": "WBS elements: PSPNR internal number, POSID the id shown, POST1 description, PSPHI the "
            "project it belongs to.",
    "PROJ": "Project definitions: PSPNR internal, PSPID the id shown, POST1 description.",
    "COBRB": "Settlement rule lines: where an object's costs go (KONTY 'PSP' WBS, 'KS' cost "
             "center, 'OR' order, 'MAT' material).",
    "MARA": "Material master, general data: type MTART, group MATKL, base unit MEINS.",
    "MARC": "Material master, plant data: MRP controller DISPO, production scheduler FEVOR, "
            "procurement BESKZ, lead time PLIFZ, in-house production time DZEIT.",
    "MAKT": "Material descriptions per language (MAKTX).",
    "MARD": "Storage-location stock: LABST unrestricted, INSME in inspection, SPEME blocked.",
    "T024D": "MRP controllers per plant: DISPO → name DSNAM.",
    "T024F": "Production schedulers per plant: FEVOR → text TXT.",
    "MDKP": "MRP list / stock-requirements list header per material and plant (DTART 'MD' = MRP "
            "list).",
    "MDTB": "MRP list items: the elements (orders, planned orders, POs, requirements) with their "
            "exception keys AUSSL — the 'MRP messages' (texts in T458B.AUSLT).",
    "PLAF": "Planned orders: PLNUM, material MATNR, plant PLWRK, quantity GSMNG, dates "
            "PSTTR/PEDTR.",
    "PLKO": "Routing (task list) headers: PLNTY type ('N' routing), PLNNR group, PLNAL group "
            "counter.",
    "PLPO": "Routing operations: VORNR, text LTXA1, work center ARBID, control key STEUS, "
            "standard values.",
    "PLAS": "Routing sequence ↔ operation assignments (the link PLKO → PLPO goes through here).",
    "MAPL": "Which routings a material uses at a plant.",
    "MAST": "Material → BOM link (BOM number STLNR, alternative STLAL, usage STLAN).",
    "STKO": "BOM headers.", "STPO": "BOM items (component IDNRK, quantity MENGE).",
    "STAS": "BOM item selection (header → items).",
    "KAKO": "Capacity headers.", "KBED": "Capacity requirements per operation.",
    "SER05": "Serial-number object lists for production order items.",
    "OBJK": "The serial numbers themselves (SERNR, MATNR, equipment EQUNR) behind an object list "
            "OBKNR.",
    "EQUI": "Equipment master (serialized units are equipment).",
    "AUFM": "Goods movements for orders (a view-like table of MSEG rows by order).",
    "MKPF": "Material document headers.",
    "MSEG": "Material document lines (movement type BWART, quantity MENGE).",
    "EKKO": "Purchase order headers.",
    "EKPO": "Purchase order items.", "EKET": "PO schedule lines.",
    "EKBE": "PO history (goods receipts, invoices).",
    "EKKN": "PO account assignments (order, WBS).",
    "EBAN": "Purchase requisitions.",
    "LFA1": "Vendor master (general).", "KNA1": "Customer master (general).",
    "VBAK": "Sales order headers.", "VBAP": "Sales order items.", "VBEP": "Sales schedule lines.",
    "QMEL": "Quality notifications: type QMART, material, order AUFNR, vendor LIFNUM, dates, "
            "status object OBJNR. The equipment is on QMIH, not here.",
    "QMIH": "Notification technical-object data (one row per notification): equipment EQUNR, "
            "location ILOAN, assembly BAUTL.",
    "QMFE": "Notification items (defects).", "QMMA": "Notification activities.",
    "QMSM": "Notification tasks.", "QMUR": "Defect causes.",
    "TQ80": "Notification types.", "T001W": "Plants (NAME1).", "T003O": "Order types.",
    "T003P": "Order type texts.", "T430": "Control keys.", "T430T": "Control key texts.",
    "TRUG": "Variance reasons for confirmations.", "TRUGT": "Variance reason texts.",
    "T006": "Units of measure.", "T006A": "Unit of measure texts.",
    "T156": "Movement types.", "T156T": "Movement type texts.",
    "PA0002": "Personal data (names) per personnel number, time-dependent.",
    "USR21": "User → person (address) assignment.", "ADRP": "Person names (NAME_TEXT).",
    "CDHDR": "Change document headers.", "CDPOS": "Change document lines.",
    "STXH": "Long-text headers (order/operation texts). The bodies in STXL are compressed and "
            "cannot be read in SQL.",
    "CSKS": "Cost centers (time-dependent).",
    "CSKT": "Cost center texts.", "CSLA": "Activity types.",
    "AFIH": "Maintenance-order header (PM/CS): equipment, functional location, priority PRIOK.",
    "MCHA": "Batch master per plant.",
    "MCHB": "Batch stock per storage location.", "MCH1": "Batch master.",
    "MKAL": "Production versions.", "MBEW": "Material valuation (prices).",
}

# Field-level notes for the questions that come up: (TABLE, FIELD) → sentence.
FIELD_NOTES: dict[tuple[str, str], str] = {
    ("AFRU", "BUDAT"): "Posting date of the confirmation. There is no posting time; ERZET is the "
                       "entry time and ERSDA the entry date.",
    ("AFRU", "ERZET"): "Entry time (clock time the confirmation was entered) — the only time stamp "
                       "of the posting on AFRU.",
    ("AFRU", "ISDD"): "Actual execution start date (with ISDZ the time) — what the person entered, "
                      "often the same as BUDAT but not the same thing.",
    ("AFRU", "IEDD"): "Actual execution finish date (IEDZ time).",
    ("AFRU",
     "ARBID"): "Work center as an internal object id — join CRHD.OBJID (OBJTY 'A') for ARBPL.",
    ("AFRU", "VORNR"): "Operation/activity number, right on the confirmation.",
    ("AFRU",
     "RUECK"): "Confirmation number (the operation's; AFVC.RUECK matches) — with RMZHL the key.",
    ("AFRU", "RMZHL"): "Confirmation counter: the n-th confirmation posted under this RUECK.",
    ("AFRU", "STOKZ"): "'X' = this row IS a reversal document.",
    ("AFRU",
     "STZHL"): "Counter of the confirmation that reversed this one (00000000 = not reversed).",
    ("AFRU", "LMNGA"): "Confirmed yield.", ("AFRU", "XMNGA"): "Confirmed scrap.",
    ("AFRU", "RMNGA"): "Confirmed rework quantity.",
    ("AFRU", "ISMNW"): "Actual work confirmed (activity 1 is ISM01 … 6 is ISM06).",
    ("AFRU", "AUERU"): "'X' = final confirmation.",
    ("AFRU", "PERNR"): "Personnel number of the person who did the work.",
    ("AUFK", "OBJNR"): "Status object number ('OR' + order number) — the key into JEST/JSTO/COBRB.",
    ("AUFK", "AUTYP"): "Order category: 10 production, 40 process, 30 maintenance, 01 internal, 20 "
                       "network.",
    ("AUFK", "AUART"): "Order type (customizing, e.g. PP01) — T003P has the text.",
    ("AUFK", "KTEXT"): "Order description.",
    ("AUFK", "PSPEL"): "WBS element (internal number) assigned on the order header → PRPS.PSPNR.",
    ("AUFK", "ERDAT"): "Created on.", ("AUFK", "IDAT1"): "Technically completed on.",
    ("AUFK", "IDAT2"): "Closed on.", ("AUFK", "IDAT3"): "Deletion flag set on.",
    ("AUFK", "LOEKZ"): "Deletion flag.", ("AUFK", "KOSTV"): "Responsible cost center.",
    ("AFKO", "AUFPL"): "Routing number of the order's operations — the key into AFVC/AFVV/AFRU.",
    ("AFKO", "RSNUM"): "Reservation number — the key into RESB (components).",
    ("AFKO", "PLNBEZ"): "The material being produced (same as AFPO.MATNR).",
    ("AFKO", "GAMNG"): "Total order quantity.", ("AFKO", "IGMNG"): "Confirmed yield for the order.",
    ("AFKO", "GSTRP"): "Basic start date.",
    ("AFKO", "GLTRP"): "Basic finish date (the order's due date).",
    ("AFKO", "GSTRS"): "Scheduled start.", ("AFKO", "GLTRS"): "Scheduled finish.",
    ("AFKO", "GSTRI"): "Actual start.", ("AFKO", "GLTRI"): "Actual finish (confirmed).",
    ("AFKO", "GETRI"): "Actual finish date.", ("AFKO", "FTRMI"): "Actual release date.",
    ("AFKO", "FTRMS"): "Scheduled release date.", ("AFKO", "FTRMP"): "Planned release date.",
    ("AFKO", "DISPO"): "MRP controller (name in T024D via the plant AUFK.WERKS).",
    ("AFKO", "FEVOR"): "Production scheduler (name in T024F).",
    ("AFPO", "PROJN"): "WBS element (internal number) → PRPS.PSPNR; PRPS.POSID is the readable id.",
    ("AFPO", "PSMNG"): "Order item quantity.",
    ("AFPO", "WEMNG"): "Quantity delivered (goods receipts).",
    ("AFPO", "CHARG"): "Batch.", ("AFPO", "DWERK"): "Plant of the item.",
    ("AFPO", "KDAUF"): "Sales order (make-to-order).", ("AFPO", "KDPOS"): "Sales order item.",
    ("AFPO", "PLNUM"): "Planned order converted into this order.",
    ("AFPO", "LTRMI"): "Actual delivery date.", ("AFPO", "ELIKZ"): "'X' = delivery completed.",
    ("AFVC", "VORNR"): "Operation/activity number.", ("AFVC", "LTXA1"): "Operation description.",
    ("AFVC", "ARBID"): "Work center (internal id) → CRHD.OBJID.",
    ("AFVC", "STEUS"): "Control key (T430).",
    ("AFVC", "RUECK"): "Confirmation number → AFRU.RUECK.",
    ("AFVC", "OBJNR"): "Status object of the operation → JEST.",
    ("AFVC", "PROJN"): "WBS element on the operation (internal) → PRPS.PSPNR.",
    ("AFVC", "APLFL"): "Sequence number within the routing.",
    ("AFVV", "MGVRG"): "Operation quantity.",
    ("AFVV", "LMNGA"): "Confirmed yield on the operation.",
    ("AFVV", "XMNGA"): "Confirmed scrap on the operation.",
    ("AFVV", "FSAVD"): "Earliest scheduled start.", ("AFVV", "FSEDD"): "Earliest scheduled finish.",
    ("AFVV", "SSAVD"): "Latest scheduled start.", ("AFVV", "SSEDD"): "Latest scheduled finish.",
    ("AFVV", "ISDD"): "Actual start of the operation.",
    ("AFVV", "IEDD"): "Actual finish of the operation.",
    ("AFVV", "BEARZ"): "Processing time.", ("AFVV", "DAUNO"): "Normal duration.",
    ("CRHD", "ARBPL"): "The work center key people read (e.g. 'MECH01').",
    ("CRHD", "OBJID"): "Internal object id — what ARBID on AFVC/AFRU/PLPO points at.",
    ("CRHD", "OBJTY"): "'A' = work center.",
    ("CRTX", "KTEXT"): "Work center short description.",
    ("JEST", "STAT"): "Status code: 'I0002' REL, 'I0009' CNF, 'I0010' PCNF, 'I0045' TECO, 'I0046' "
                      "CLSD, 'I0012' DLV … (TJ02T has the texts); 'E…' are user statuses.",
    ("JEST", "INACT"): "'X' = no longer active. Filter INACT = '' for current statuses.",
    ("TJ02T", "ISTAT"): "The status code (= JEST.STAT).",
    ("TJ02T", "TXT04"): "The 4-letter status shown on screen.",
    ("PRPS", "POSID"): "The WBS element id shown on screen.", ("PRPS", "POST1"): "WBS description.",
    ("PRPS", "PSPHI"): "The project definition (→ PROJ.PSPNR).",
    ("PROJ", "PSPID"): "The project id shown on screen.", ("PROJ", "POST1"): "Project description.",
    ("MAKT", "MAKTX"): "Material description (one row per language — filter SPRAS = 'E').",
    ("MARC", "DISPO"): "MRP controller.", ("MARC", "FEVOR"): "Production scheduler.",
    ("T024D", "DSNAM"): "MRP controller's name.",
    ("MDTB", "AUSSL"): "MRP exception (message) key — texts in T458B.AUSLT; the message number "
                       "AUSKT is on T458A, not here.",
    ("MDTB", "DELKZ"): "MRP element type: FE production order, PA planned order, BE purchase "
                       "order, BA purchase requisition, LA delivery schedule line.",
    ("MDTB", "DELNR"): "The element's number (for DELKZ 'FE' the production order number).",
    ("RESB", "BDMNG"): "Requirement quantity.", ("RESB", "ENMNG"): "Quantity withdrawn so far.",
    ("RESB", "XLOEK"): "Deleted.", ("MARD", "LABST"): "Unrestricted-use stock.",
    ("OBJK", "SERNR"): "Serial number.", ("SER05", "OBKNR"): "Object list number → OBJK.",
    ("COBRB", "KONTY"): "Receiver type: PSP WBS element, KS cost center, OR order, MAT material.",
    ("COBRB", "PS_PSP_PNR"): "WBS element (internal) receiving the settlement → PRPS.PSPNR.",
    ("QMEL", "QMART"): "Notification type (Q1 customer complaint, Q2 vendor complaint, Q3 internal "
                       "problem, F2/F3 are custom or PM types — TQ80_T has the texts).",
    ("QMEL", "AUFNR"): "The order the notification refers to.",
}

# What a code means, for the fields where it matters. (TABLE, FIELD) → {code: meaning}.
CODE_VALUES: dict[tuple[str, str], dict[str, str]] = {
    ("AUFK", "AUTYP"): {"01": "internal order", "10": "production order", "20": "network",
                        "30": "maintenance order", "40": "process order"},
    ("JEST", "INACT"): {"": "active", "X": "inactive (no longer set)"},
    ("AFRU", "STOKZ"): {"": "normal confirmation", "X": "reversal document"},
    ("AFRU", "AUERU"): {"": "partial confirmation", "X": "final confirmation"},
    ("CRHD", "OBJTY"): {"A": "work center"},
    ("COBRB", "KONTY"): {"PSP": "WBS element", "KS": "cost center", "OR": "order",
                         "MAT": "material", "SK": "G/L account", "ANL": "asset"},
    ("MDTB", "DELKZ"): {"FE": "production order", "PA": "planned order", "BE": "purchase order",
                        "BA": "purchase requisition", "LA": "delivery schedule line",
                        "AR": "dependent requirement", "KB": "customer order"},
    ("MDTB", "PLUMI"): {"+": "receipt", "-": "issue"},
    ("JEST", "STAT"): {"I0001": "CRTD created", "I0002": "REL released", "I0009": "CNF confirmed",
                       "I0010": "PCNF partially confirmed", "I0012": "DLV delivered",
                       "I0045": "TECO technically completed", "I0046": "CLSD closed",
                       "I0013": "DLFL deletion flag", "I0076": "DLT deleted",
                       "I0016": "PRC pre-costed", "I0028": "PRT printed", "I0042": "LKD locked",
                       "I0104": "MACM material committed", "I0115": "MSPT material shortage"},
    ("PLKO", "PLNTY"): {"N": "routing", "R": "reference operation set", "2": "master recipe",
                        "Q": "inspection plan", "A": "general maintenance task list"},
    ("MSEG", "BWART"): {"101": "goods receipt for order", "261": "goods issue to order",
                        "262": "reversal of 261",
                        "102": "reversal of 101", "531": "by-product receipt",
                        "311": "transfer storage location to storage location"},
}


# ----- the business vocabulary: what people say → where it is -----
@dataclass(frozen=True)
class Term:
    words: str          # what the user says
    table: str
    field: str
    note: str = ""


BUSINESS_TERMS: tuple[Term, ...] = (
    Term("order number, production order, order", "AUFK", "AUFNR"),
    Term("order description, order text", "AUFK", "KTEXT"),
    Term("order type", "AUFK", "AUART", "text in T003P"),
    Term("order category", "AUFK", "AUTYP"),
    Term("plant", "AUFK", "WERKS", "name in T001W.NAME1"),
    Term("status object", "AUFK", "OBJNR"),
    Term("system status, order status", "JEST", "STAT", "texts in TJ02T.TXT04 (ISTAT), INACT = ''"),
    Term("user status", "JEST", "STAT", "'E…' codes; texts in TJ30T via the profile JSTO.STSMA"),
    Term("wbs element, wbs", "PRPS", "POSID", "reached from AFPO.PROJN or AUFK.PSPEL"),
    Term("wbs description", "PRPS", "POST1"),
    Term("project, program, project definition", "PROJ", "PSPID", "PROJ.POST1 is the description"),
    Term("settlement rule, settlement receiver", "COBRB", "KONTY"),
    Term("mrp controller", "AFKO", "DISPO", "name in T024D.DSNAM (via plant)"),
    Term("mrp controller name", "T024D", "DSNAM"),
    Term("production scheduler", "AFKO", "FEVOR", "name in T024F"),
    Term("release date, released on", "AFKO", "FTRMI", "actual release date"),
    Term("basic start date, start date", "AFKO", "GSTRP"),
    Term("basic finish date, finish date, due date", "AFKO", "GLTRP"),
    Term("scheduled start", "AFKO", "GSTRS"), Term("scheduled finish", "AFKO", "GLTRS"),
    Term("actual start", "AFKO", "GSTRI"), Term("actual finish", "AFKO", "GLTRI"),
    Term("order quantity, total quantity", "AFKO", "GAMNG"),
    Term("confirmed quantity, confirmed yield", "AFKO", "IGMNG",
         "per operation AFVV.LMNGA / AFRU.LMNGA"),
    Term("routing number", "AFKO", "AUFPL",
         "the order's operation key; the routing GROUP is PLNNR"),
    Term("routing group, task list", "AFKO", "PLNNR", "with PLNTY and PLNAL → PLKO"),
    Term("reservation", "AFKO", "RSNUM", "→ RESB components"),
    Term("material number, material", "AFPO", "MATNR", "also AFKO.PLNBEZ"),
    Term("material description", "MAKT", "MAKTX", "SPRAS = 'E'"),
    Term("item quantity", "AFPO", "PSMNG"),
    Term("delivered quantity, goods receipt quantity", "AFPO", "WEMNG"),
    Term("open quantity", "AFPO", "PSMNG",
         "PSMNG − WEMNG (order level); AFVV.MGVRG − LMNGA − XMNGA (operation)"),
    Term("batch", "AFPO", "CHARG"), Term("sales order", "AFPO", "KDAUF", "item KDPOS"),
    Term("planned order", "AFPO", "PLNUM", "→ PLAF"),
    Term("production version", "AFPO", "VERID", "→ MKAL"),
    Term("operation, operation number, activity", "AFVC", "VORNR", "also on AFRU.VORNR"),
    Term("operation description, operation text", "AFVC", "LTXA1"),
    Term("work center", "AFVC", "ARBID",
         "internal id → CRHD.OBJID (OBJTY 'A'); CRHD.ARBPL is the key"),
    Term("work center key, work center name", "CRHD", "ARBPL"),
    Term("work center description", "CRTX", "KTEXT", "SPRAS = 'E'"),
    Term("control key", "AFVC", "STEUS", "text in T430T"),
    Term("confirmation number", "AFVC", "RUECK", "→ AFRU.RUECK (+ RMZHL)"),
    Term("operation quantity", "AFVV", "MGVRG"),
    Term("earliest start", "AFVV", "FSAVD"), Term("earliest finish", "AFVV", "FSEDD"),
    Term("latest start", "AFVV", "SSAVD"),
    Term("latest finish, operation due date", "AFVV", "SSEDD"),
    Term("slack, float", "AFVV", "SSAVD",
         "latest start − earliest start (SSAVD − FSAVD) is the total float"),
    Term("actual operation start", "AFVV", "ISDD"), Term("actual operation finish", "AFVV", "IEDD"),
    Term("confirmation, confirmations, time ticket", "AFRU", "RUECK"),
    Term("posting date", "AFRU", "BUDAT"),
    Term("posting time, entry time", "AFRU", "ERZET",
         "there is no posting time; ERZET is entry time"),
    Term("entry date", "AFRU", "ERSDA"), Term("entered by", "AFRU", "ERNAM"),
    Term("yield", "AFRU", "LMNGA"),
    Term("scrap", "AFRU", "XMNGA"), Term("rework quantity", "AFRU", "RMNGA"),
    Term("actual work", "AFRU", "ISMNW"),
    Term("personnel number, employee, operator", "AFRU", "PERNR", "name in PA0002"),
    Term("last worked, last confirmation", "AFRU", "BUDAT",
         "latest per operation — the 'latest_confirmation' recipe"),
    Term("final confirmation", "AFRU", "AUERU"),
    Term("reversal, reversed", "AFRU", "STOKZ", "and STZHL"),
    Term("variance reason", "AFRU", "GRUND", "text in TRUGT"),
    Term("component, components, bom component", "RESB", "MATNR"),
    Term("requirement quantity", "RESB", "BDMNG"),
    Term("withdrawn quantity, issued quantity", "RESB", "ENMNG"),
    Term("shortage, short quantity", "RESB", "BDMNG",
         "BDMNG − ENMNG against MARD.LABST at MATNR/WERKS/LGORT"),
    Term("storage location", "RESB", "LGORT"),
    Term("stock, unrestricted stock", "MARD", "LABST"),
    Term("mrp message, mrp exception, exception message", "MDTB", "AUSSL",
         "texts in T458B.AUSLT; DELKZ 'FE' + DELNR = order"),
    Term("serial number, serial numbers", "OBJK", "SERNR", "via SER05 (order item) → OBJK"),
    Term("equipment", "EQUI", "EQUNR", "text EQKT.EQKTX"),
    Term("goods movement, goods issue, goods receipt", "AUFM", "BWART",
         "per order; MSEG is the document line"),
    Term("material document", "MKPF", "MBLNR", "with MJAHR"),
    Term("movement type", "MSEG", "BWART", "text in T156T"),
    Term("purchase order", "EKPO", "EBELN", "item EBELP; header EKKO"),
    Term("purchase requisition", "EBAN", "BANFN"),
    Term("vendor, supplier", "LFA1", "LIFNR", "name NAME1"),
    Term("customer", "KNA1", "KUNNR", "name NAME1"),
    Term("quality notification, notification", "QMEL", "QMNUM"),
    Term("notification type", "QMEL", "QMART", "text in TQ80_T"),
    Term("defect, defect item", "QMFE", "FENUM"),
    Term("cost center", "AUFK", "KOSTV", "text in CSKT"),
    Term("activity type", "AFRU", "LEARR", "→ CSLA.LSTAR"),
    Term("created on", "AUFK", "ERDAT"),
    Term("created by", "AUFK", "ERNAM", "name via USR21 → ADRP"),
    Term("technically completed", "AUFK", "IDAT1"), Term("closed on", "AUFK", "IDAT2"),
    Term("deletion flag", "AUFK", "LOEKZ"),
    Term("field description, field name, english name of a field, what a field "
         "means", "DD03T", "DDTEXT",
         "SAP's own field-text table — not in the EDW; HELIX's catalog carries the descriptions "
         "instead"),
    Term("long text, order long text, notes", "STXH", "TDNAME",
         "STXL bodies are compressed — not readable in SQL"),
    Term("priority", "AFIH", "PRIOK",
         "maintenance orders only; production orders have no standard priority"),
)


# ----- SQL recipes: building blocks the writer drops in as CTEs -----
# Templates use {T:TABLE} for the qualified EDW table and {LANG} for the language literal.
@dataclass(frozen=True)
class Recipe:
    name: str
    description: str
    tables: tuple[str, ...]        # tables the recipe reads (for the EDW availability check)
    cte_name: str
    cte_sql: str                   # the CTE body (a SELECT), with {T:...} tokens
    # (base_table, base_field) → cte column, e.g. (("AUFK", "OBJNR"),) joins on cte.OBJNR
    join_on: tuple[tuple[str, str], ...]
    columns: tuple[tuple[str, str], ...]   # (cte column, meaning)
    note: str = ""


RECIPES: dict[str, Recipe] = {
    "system_status": Recipe(
        name="system_status",
        description="All active system statuses of an object on one row (e.g. 'REL PCNF MACM'), "
                    "the way the order screen shows them.",
        tables=("JEST", "TJ02T"), cte_name="sys_status",
        cte_sql=(
            "SELECT j.OBJNR,\n"
            "       LISTAGG(t.TXT04, ' ') WITHIN GROUP (ORDER BY t.TXT04) AS SYSTEM_STATUS\n"
            "FROM {T:JEST} j\n"
            "JOIN {T:TJ02T} t ON t.ISTAT = j.STAT AND t.SPRAS = '{LANG}'\n"
            "WHERE j.INACT = ''\n"
            "GROUP BY j.OBJNR"
        ),
        join_on=(("AUFK", "OBJNR"), ("AFVC", "OBJNR"), ("QMEL", "OBJNR"), ("PRPS", "OBJNR")),
        columns=(("SYSTEM_STATUS", "space-separated active system statuses"),),
        note="Join sys_status on the object's OBJNR (AUFK for the order, AFVC for an operation).",
    ),
    "user_status": Recipe(
        name="user_status",
        description="All active user statuses of an object on one row, resolved through the "
                    "object's status profile.",
        tables=("JEST", "JSTO", "TJ30T"), cte_name="usr_status",
        cte_sql=(
            "SELECT j.OBJNR,\n"
            "       LISTAGG(t.TXT04, ' ') WITHIN GROUP (ORDER BY t.TXT04) AS USER_STATUS\n"
            "FROM {T:JEST} j\n"
            "JOIN {T:JSTO} s ON s.MANDT = j.MANDT AND s.OBJNR = j.OBJNR\n"
            "JOIN {T:TJ30T} t ON t.MANDT = j.MANDT AND t.STSMA = s.STSMA AND t.ESTAT = j.STAT\n"
            "                 AND t.SPRAS = '{LANG}'\n"
            "WHERE j.INACT = '' AND j.STAT LIKE 'E%'\n"
            "GROUP BY j.OBJNR"
        ),
        join_on=(("AUFK", "OBJNR"), ("AFVC", "OBJNR"), ("QMEL", "OBJNR")),
        columns=(("USER_STATUS", "space-separated active user statuses"),),
    ),
    "latest_confirmation": Recipe(
        name="latest_confirmation",
        description="The most recent valid confirmation per operation — 'last worked' — with its "
                    "posting date, person and work center.",
        tables=("AFRU",), cte_name="last_conf",
        cte_sql=(
            "SELECT r.AUFPL, r.APLZL, r.AUFNR, r.VORNR, r.BUDAT AS LAST_WORKED, r.ERSDA, r.ERZET,\n"
            "       r.PERNR, r.ARBID, r.LMNGA, r.XMNGA, r.ISMNW, r.AUERU, r.LTXA1\n"
            "FROM {T:AFRU} r\n"
            "WHERE r.STOKZ = '' AND r.STZHL = '00000000'\n"
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY r.AUFPL, r.APLZL\n"
            "                           ORDER BY r.BUDAT DESC, r.ERSDA DESC, r.ERZET DESC, r.RMZHL "
            "DESC) = 1"
        ),
        join_on=(("AFVC", "AUFPL"), ("AFVC", "APLZL")),
        columns=(("LAST_WORKED", "posting date of the latest confirmation (DATS)"),
                 ("PERNR", "who posted it"), ("AUERU", "'X' if it was the final confirmation")),
        note="Join last_conf on AFVC.AUFPL = last_conf.AUFPL AND AFVC.APLZL = last_conf.APLZL.",
    ),
    "confirmed_totals": Recipe(
        name="confirmed_totals",
        description="Total confirmed yield, scrap and work per operation across all valid "
                    "confirmations.",
        tables=("AFRU",), cte_name="conf_totals",
        cte_sql=(
            "SELECT r.AUFPL, r.APLZL, SUM(r.LMNGA) AS CONFIRMED_YIELD, SUM(r.XMNGA) AS "
            "CONFIRMED_SCRAP,\n"
            "       SUM(r.ISMNW) AS CONFIRMED_WORK, COUNT(*) AS CONFIRMATIONS, MAX(r.BUDAT) AS "
            "LAST_POSTING\n"
            "FROM {T:AFRU} r\n"
            "WHERE r.STOKZ = '' AND r.STZHL = '00000000'\n"
            "GROUP BY r.AUFPL, r.APLZL"
        ),
        join_on=(("AFVC", "AUFPL"), ("AFVC", "APLZL")),
        columns=(("CONFIRMED_YIELD", "sum of LMNGA"), ("CONFIRMED_SCRAP", "sum of XMNGA"),
                 ("LAST_POSTING", "max BUDAT")),
    ),
    "serial_numbers": Recipe(
        name="serial_numbers",
        description="All serial numbers of an order item on one row.",
        tables=("SER05", "OBJK"), cte_name="serials",
        # SER05 spells the order PPAUFNR / PPPOSNR; the CTE renames them so the writer's join
        # (serials.AUFNR = AFPO.AUFNR …) reads as the item's own key.
        cte_sql=(
            "SELECT s.PPAUFNR AS AUFNR, s.PPPOSNR AS POSNR,\n"
            "       LISTAGG(o.SERNR, ', ') WITHIN GROUP (ORDER BY o.SERNR) AS SERIAL_NUMBERS,\n"
            "       COUNT(*) AS SERIAL_COUNT\n"
            "FROM {T:SER05} s\n"
            "JOIN {T:OBJK} o ON o.MANDT = s.MANDT AND o.OBKNR = s.OBKNR\n"
            "GROUP BY s.PPAUFNR, s.PPPOSNR"
        ),
        join_on=(("AFPO", "AUFNR"), ("AFPO", "POSNR")),
        columns=(("SERIAL_NUMBERS", "comma-separated serial numbers"),
                 ("SERIAL_COUNT", "how many")),
    ),
    "mrp_controller": Recipe(
        name="mrp_controller",
        description="The order's own MRP controller (AFKO.DISPO) with the name from T024D — the "
                    "name lives under the ORDER's plant (AUFK.WERKS), a three-table condition no "
                    "pairwise join can express, so it is a recipe.",
        tables=("AFKO", "AUFK", "T024D"), cte_name="mrp_ctl",
        cte_sql=(
            "SELECT k.AUFNR, k.DISPO, d.DSNAM,\n"
            "       k.DISPO || ' ' || COALESCE(d.DSNAM, '') AS MRP_CONTROLLER\n"
            "FROM {T:AFKO} k\n"
            "JOIN {T:AUFK} a ON a.MANDT = k.MANDT AND a.AUFNR = k.AUFNR\n"
            "LEFT JOIN {T:T024D} d ON d.MANDT = a.MANDT AND d.WERKS = a.WERKS AND d.DISPO = k.DISPO"
        ),
        join_on=(("AUFK", "AUFNR"), ("AFKO", "AUFNR")),
        columns=(("MRP_CONTROLLER", "code and name"), ("DISPO", "the controller code"),
                 ("DSNAM", "the controller's name")),
    ),
    "mrp_messages": Recipe(
        name="mrp_messages",
        description="The MRP exception messages currently on a production order (from the MRP "
                    "list).",
        tables=("MDKP", "MDTB", "T458B"), cte_name="mrp_msg",
        # The dictionary's names: MDTB.AUSSL is the exception key, T458B (MANDT + SPRAS + AUSSL)
        # holds its text AUSLT. AUSKT — the message number — is on T458A and not on MDTB.
        cte_sql=(
            "SELECT b.DELNR AS AUFNR,\n"
            "       LISTAGG(DISTINCT b.AUSSL || ' ' || COALESCE(x.AUSLT, ''), '; ') AS "
            "MRP_MESSAGE\n"
            "FROM {T:MDTB} b\n"
            "JOIN {T:MDKP} k ON k.MANDT = b.MANDT AND k.DTNUM = b.DTNUM AND k.DTART = 'MD'\n"
            "LEFT JOIN {T:T458B} x ON x.MANDT = b.MANDT AND x.AUSSL = b.AUSSL\n"
            "                     AND x.SPRAS = '{LANG}'\n"
            "WHERE b.DELKZ = 'FE' AND b.AUSSL <> ''\n"
            "GROUP BY b.DELNR"
        ),
        join_on=(("AUFK", "AUFNR"),),
        columns=(("MRP_MESSAGE", "exception keys with their texts"),),
        note="MRP exception keys (MDTB.AUSSL) with their T458B texts, per order (DELKZ 'FE').",
    ),
    "component_shortages": Recipe(
        name="component_shortages",
        description="Components of an order whose open requirement exceeds unrestricted stock at "
                    "their storage location.",
        tables=("RESB", "MARD"), cte_name="shortages",
        cte_sql=(
            "SELECT c.RSNUM,\n"
            "       LISTAGG(c.MATNR || ' (' || TO_VARCHAR(c.BDMNG - c.ENMNG - COALESCE(d.LABST, 0))"
            " || ')', '; ')\n"
            "           WITHIN GROUP (ORDER BY c.RSPOS) AS ITEM_SHORTAGES,\n"
            "       SUM(c.BDMNG - c.ENMNG - COALESCE(d.LABST, 0)) AS SHORT_QTY,\n"
            "       COUNT(*) AS SHORT_ITEMS\n"
            "FROM {T:RESB} c\n"
            "LEFT JOIN {T:MARD} d ON d.MANDT = c.MANDT AND d.MATNR = c.MATNR AND d.WERKS = "
            "c.WERKS\n"
            "                    AND d.LGORT = c.LGORT\n"
            "WHERE c.XLOEK = '' AND c.KZEAR = '' AND (c.BDMNG - c.ENMNG) > COALESCE(d.LABST, 0)\n"
            "GROUP BY c.RSNUM"
        ),
        join_on=(("AFKO", "RSNUM"),),
        columns=(("ITEM_SHORTAGES", "short components with the short quantity"),
                 ("SHORT_QTY", "total short quantity"),
                 ("SHORT_ITEMS", "how many components are short")),
        note="A simple availability rule (requirement − withdrawn vs. unrestricted stock at the "
             "component's storage location); SAP's own ATP check is richer.",
    ),
}


# ----- the catalog's scope: what the build ingests, by module (seed list; the build adds each
# table's check tables one hop out, which brings the T-tables and text tables along) -----
SCOPE: dict[str, tuple[str, ...]] = {
    "PP": (
        "AUFK", "AFKO", "AFPO", "AFVC", "AFVV", "AFVU", "AFFL", "AFFH", "AFAB", "AFRU", "AFRH",
        "AFRV", "AFRP1", "AFRP2", "AFRP3", "AFRP4", "AFRC", "AUFM", "RESB", "RKPF", "PLAF", "MDKP",
        "MDTB", "MDMA", "MDVM", "T458A", "T458B", "T024D", "T024F", "CRHD", "CRTX", "CRCO", "CRCA",
        "CRHH", "KAKO", "KAKT", "KAZY", "KBED", "KBKO", "KBEZ", "PLKO", "PLPO", "PLAS", "PLFL",
        "PLMZ", "PLFH", "MAPL", "PLKZ", "MKAL", "T430", "T430T", "T003O", "T003P", "TCO43", "T399X",
        "T438A", "T438T", "T439A", "T459A", "TRUG", "TRUGT", "MAST", "STKO", "STPO", "STAS",
        "STZU", "T416", "T416T", "CAUFV", "AFIH", "JEST", "JSTO", "JCDS", "TJ02", "TJ02T", "TJ04",
        "TJ20", "TJ20T", "TJ21", "TJ30", "TJ30T", "ONR00",
    ),
    "CO": (
        "COBRA", "COBRB", "COEP", "COSS", "COSP", "COBK", "COKEY", "CSKS", "CSKT", "CSLA",
        "CSLT", "TKA01", "TKA02", "COSL", "COSB",
    ),
    "PS": ("PROJ", "PRPS", "PRHI", "PRTE", "PRTX", "PSTX", "PSTT", "RPSCO"),
    "MM": (
        "MARA", "MARC", "MAKT", "MARM", "MARD", "MARDH", "MBEW", "MBEWH", "MVKE", "MLAN", "MVER",
        "MCHA", "MCHB", "MCH1", "MCHBH", "MSKA", "MSLB", "MSPR", "T023", "T023T", "T134", "T134T",
        "MKPF", "MSEG", "T156", "T156T", "T156X", "T157E", "EKKO", "EKPO", "EKET", "EKBE", "EKKN",
        "EKPV", "EBAN", "EBKN", "EINA", "EINE", "EORD", "LFA1", "LFB1", "LFM1", "T024", "T024E",
        "T024W",
        "T161", "T161T", "T001W", "T001K", "T001L", "T001", "T006", "T006A", "T005", "T005T",
        "TCURC", "TCURX", "T002", "T002T",
    ),
    "SD": ("VBAK", "VBAP", "VBEP", "VBKD", "VBPA", "VBFA", "VBUK", "VBUP", "LIKP", "LIPS", "KNA1",
           "KNB1", "KNVV", "TVKO", "TVKOT", "TVAK", "TVAKT", "TVTW", "TVTWT", "TSPA", "TSPAT"),
    "QM": ("QMEL", "QMFE", "QMMA", "QMSM", "QMUR", "QMIH", "QALS", "QAVE", "QAMB", "QAMR", "QASE",
           "QPCT",
           "QPGT", "QPGR", "QPCD", "QPMK", "QPMT", "TQ80", "TQ80_T", "TQ15", "TQ15T", "TQ30",
           "TQ30T", "QMAT", "QPAM"),
    "PM": ("EQUI", "EQKT", "EQBS", "EQUZ", "ILOA", "IFLOT", "IFLOTX", "SER01", "SER02", "SER03",
           "SER05", "SER06", "SER07", "OBJK", "T370T", "T370U", "VIQMEL"),
    "CA": ("CDHDR", "CDPOS", "STXH", "STXL", "KLAH", "KSSK", "AUSP", "CABN", "CABNT", "KSML",
           "SWOR", "DRAW", "DRAT", "DRAD", "ADRC", "ADRP", "ADR6", "USR01", "USR21", "TVARVC"),
    "HR": ("PA0001", "PA0002", "PA0105", "T527X", "T528T", "T001P", "T500P"),
    "FI": ("BKPF", "BSEG", "SKA1", "SKAT", "T001B", "T003", "T003T", "T009"),
    "BC": ("T000", "DD02L", "DD02T", "DD03L", "DD03T", "DD04T", "DD07T", "TADIR", "TSTC", "TSTCT"),
}


def scope_module(table: str) -> str:
    """The module a seed table is filed under ('' for a table outside the seed list)."""
    for module, names in SCOPE.items():
        if table in names:
            return module
    return ""


# ----- the WIP report: the user's 27 columns, each traced to its source -----
@dataclass(frozen=True)
class ReportColumn:
    label: str
    source: str        # "TABLE.FIELD", "recipe:<name>.<column>", "expr", or "custom"
    expression: str    # the Snowflake expression, with aliases as the join plan names them
    status: str        # "standard" | "derived" | "probable" | "custom" | "unavailable"
    note: str = ""


WIP_REPORT: tuple[ReportColumn, ...] = (
    ReportColumn("Order", "AUFK.AUFNR", "AUFK.AUFNR", "standard"),
    ReportColumn("Operation/Activity", "AFVC.VORNR", "AFVC.VORNR", "standard"),
    ReportColumn("System Status", "recipe:system_status.SYSTEM_STATUS",
                 "sys_status.SYSTEM_STATUS", "derived",
                 "JEST (INACT='') + TJ02T (ISTAT, SPRAS='E') aggregated per AUFK.OBJNR"),
    ReportColumn("WBS Element", "PRPS.POSID", "PRPS.POSID", "standard",
                 "via AFPO.PROJN = PRPS.PSPNR (fallback AUFK.PSPEL; COBRB for settlement "
                 "receivers)"),
    ReportColumn("Program Name", "PROJ.POST1", "PROJ.POST1", "probable",
                 "the project definition's description via PRPS.PSPHI = PROJ.PSPNR — confirm this "
                 "is "
                 "what 'program' means at the site; otherwise a custom field"),
    ReportColumn("MRP Controller", "recipe:mrp_controller.MRP_CONTROLLER",
                 "mrp_ctl.MRP_CONTROLLER", "derived",
                 "the order's AFKO.DISPO with its T024D.DSNAM name on the order's plant"),
    ReportColumn("Rating Rwk", "custom", "NULL", "custom", "no standard SAP field — a Z-field or "
                 "a spreadsheet column at the site; check the EDW column list for ZZ* names"),
    ReportColumn("Release Date", "AFKO.FTRMI", "TO_DATE(NULLIF(AFKO.FTRMI, '00000000'), "
                                               "'YYYYMMDD')", "standard",
                 "actual release date"),
    ReportColumn("Days Old", "expr", "DATEDIFF('day', TO_DATE(NULLIF(AFKO.FTRMI, '00000000'), "
                                     "'YYYYMMDD'), CURRENT_DATE)",
                 "derived", "days since release"),
    ReportColumn("Priority", "custom", "NULL", "custom",
                 "production orders have no standard priority (AFIH.PRIOK is maintenance only)"),
    ReportColumn("MRP Message", "recipe:mrp_messages.MRP_MESSAGE", "mrp_msg.MRP_MESSAGE",
                 "derived", "MDTB exception keys (AUSSL) for DELKZ='FE' and DELNR = the order, "
                 "texts from T458B.AUSLT"),
    ReportColumn("Batch", "AFPO.CHARG", "AFPO.CHARG", "standard"),
    ReportColumn("Serial Numbers", "recipe:serial_numbers.SERIAL_NUMBERS",
                 "serials.SERIAL_NUMBERS", "derived", "SER05 → OBJK per order item"),
    ReportColumn("Material Number", "AFPO.MATNR", "AFPO.MATNR", "standard", "same as AFKO.PLNBEZ"),
    ReportColumn("Material Description", "MAKT.MAKTX", "MAKT.MAKTX", "standard", "SPRAS = 'E'"),
    ReportColumn("Open Qty", "expr", "AFVV.MGVRG - AFVV.LMNGA - AFVV.XMNGA", "derived",
                 "operation level: operation qty − confirmed yield − scrap; order level is "
                 "AFPO.PSMNG − AFPO.WEMNG"),
    ReportColumn("Work Center", "CRHD.ARBPL", "CRHD.ARBPL", "standard",
                 "AFVC.ARBID = CRHD.OBJID, OBJTY='A'"),
    ReportColumn("Work Center Description", "CRTX.KTEXT", "CRTX.KTEXT", "standard", "SPRAS = 'E'"),
    ReportColumn("Operation Description", "AFVC.LTXA1", "AFVC.LTXA1", "standard"),
    ReportColumn("Due Date", "AFVV.SSEDD", "TO_DATE(NULLIF(AFVV.SSEDD, '00000000'), "
                                           "'YYYYMMDD')", "standard",
                 "operation latest finish; the order's due date is AFKO.GLTRP"),
    ReportColumn("Days Left", "expr", "DATEDIFF('day', CURRENT_DATE, TO_DATE(NULLIF(AFVV.SSEDD, "
                                      "'00000000'), 'YYYYMMDD'))",
                 "derived"),
    ReportColumn("Slack Time", "expr", "DATEDIFF('day', TO_DATE(NULLIF(AFVV.FSAVD, '00000000'), "
                                       "'YYYYMMDD'), "
                 "TO_DATE(NULLIF(AFVV.SSAVD, '00000000'), 'YYYYMMDD'))", "derived",
                 "total float: latest start − earliest start"),
    ReportColumn("Last Worked", "recipe:latest_confirmation.LAST_WORKED",
                 "TO_DATE(NULLIF(last_conf.LAST_WORKED, '00000000'), 'YYYYMMDD')", "derived",
                 "posting date of the latest valid confirmation on the operation"),
    ReportColumn("Days Sitting", "expr", "DATEDIFF('day', TO_DATE(NULLIF(last_conf.LAST_WORKED, "
                                         "'00000000'), 'YYYYMMDD'), CURRENT_DATE)",
                 "derived", "days since last worked"),
    ReportColumn("Short Qty", "recipe:component_shortages.SHORT_QTY", "shortages.SHORT_QTY",
                 "derived", "RESB requirement − withdrawn vs MARD unrestricted stock"),
    ReportColumn("Item Shortages", "recipe:component_shortages.ITEM_SHORTAGES",
                 "shortages.ITEM_SHORTAGES", "derived"),
    ReportColumn("Notes", "custom", "NULL", "custom",
                 "order long texts live in STXL, compressed — not readable in SQL; likely a "
                 "spreadsheet column"),
)
