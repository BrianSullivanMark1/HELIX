"""domain.enclosure — the enclosure planner, pure and fast (no kernel here; see
test_enclosure_compile.py for the build123d side).

The planner is deterministic packing plus one fixed shell recipe. These pins hold the CONTRACT
(MAKER_FLOW §4/§6): the packing never overlaps or leaves the cavity, face hints put a part against
its wall and open that wall, plate-face items are flipped and get their bores, on_lid items land
on the lid clear of the lip and the towers, problems are recorded (never raised), layout_json has
the §6 shape with the outer-corner origin, and the emitted model.py passes cadpy's gate and reads
back through parse_brief / parse_params. The test parts are DOUBLES with unique keys, so the pins
hold whatever the live catalog says about the real parts.
"""
from __future__ import annotations

import dataclasses

from helix.domain import cadpy
from helix.domain import enclosure as E
from helix.domain.components import Aperture, Component, Hole, Port, lipo_from_code

# ----- test doubles (unique keys: never shadowed by the catalog) -----

XIAO = Component(
    key="t_xiao", name="XIAO ESP32-S3 Sense (double)", category="mcu", length=21.0, width=17.5, height=10.0,
    mount="pocket", clearance=0.5, ports=(Port("usb_c", "left", 8.75),),
    apertures=(Aperture("lens", 10.5, 8.75, d=8.0, face="top"), Aperture("mic", 4.0, 3.0, d=1.5, face="top")),
    source="datasheet", confidence=0.85, tags=("compute", "vision", "hearing"),
)
AMP = Component(key="t_amp", name="MAX98357A (double)", category="amp", length=19.4, width=17.8, height=6.0,
                mount="pocket", clearance=0.5, source="community", confidence=0.6, tags=("speaking",))
SPK = Component(key="t_spk", name="Speaker 28 mm (double)", category="speaker", length=28.0, width=28.0, height=5.0,
                mount="pocket", clearance=0.4, apertures=(Aperture("speaker", 14.0, 14.0, d=26.0, face="top"),),
                source="community", confidence=0.7, tags=("speaking",))
CHG = Component(key="t_chg", name="TP4056 USB-C (double)", category="charger", length=28.0, width=17.5, height=5.0,
                mount="pocket", clearance=0.5, ports=(Port("usb_c", "left", 8.75),),
                source="community", confidence=0.6, tags=("charging",))
SW = Component(key="t_ss12d00", name="SS12D00 slide switch (double)", category="switch", length=8.6, width=5.7,
               height=3.5, mount="pocket", clearance=0.3, aliases=("ss12d00",), source="community",
               confidence=0.6, tags=("input",))
LIPO = lipo_from_code("603048")
PI = Component(
    key="t_pi_zero", name="Pi Zero 2 W (double)", category="mcu", length=65.0, width=30.0, height=8.0,
    holes=(Hole(3.5, 3.5, 2.7), Hole(3.5, 26.5, 2.7), Hole(61.5, 3.5, 2.7), Hole(61.5, 26.5, 2.7)),
    ports=(Port("hdmi", "front", 12.4), Port("micro_usb", "front", 41.4), Port("micro_usb", "front", 54.0),
           Port("sd", "left", 15.0)),
    mount="standoff", clearance=0.5, source="datasheet", confidence=0.9, tags=("compute",),
)
CAMV2 = Component(key="t_picam", name="Pi Camera v2 (double)", category="camera", length=25.0, width=24.0,
                  height=9.0, apertures=(Aperture("lens", 12.5, 12.0, d=8.5, face="top"),), mount="standoff",
                  holes=(Hole(2.0, 2.0, 2.2), Hole(23.0, 2.0, 2.2), Hole(2.0, 22.0, 2.2), Hole(23.0, 22.0, 2.2)),
                  clearance=0.5, source="community", confidence=0.6, tags=("vision",))
ESP = Component(key="t_esp32", name="ESP32 DevKitC (double)", category="mcu", length=55.0, width=28.0, height=13.0,
                mount="rails", clearance=0.5, ports=(Port("micro_usb", "left", 14.0),), source="community",
                confidence=0.65, tags=("compute",))
RELAY = Component(key="t_relay", name="Relay 2ch (double)", category="driver", length=50.5, width=38.5, height=19.0,
                  ports=(Port("other", "back", 25.0, width=40.0, height=12.0),), mount="pocket", clearance=0.5,
                  source="community", confidence=0.6, tags=("power",))
JACK = Component(key="t_jack", name="Panel barrel jack (double)", category="connector", length=15.0, width=12.0,
                 height=12.0, ports=(Port("barrel_5_5", "front", 6.0),), mount="clip", clearance=0.5,
                 source="community", confidence=0.6, tags=("power",))


def ironeye(**kw) -> E.EnclosureSpec:
    items = (E.Item(XIAO, label="CAM", face="front"), E.Item(AMP, label="AMP"), E.Item(SPK, label="SPK", face="front"),
             E.Item(LIPO, label="BAT", on_lid=True), E.Item(CHG, label="CHG", face="left"),
             E.Item(SW, label="SW", face="top"))
    args = {"mount": "strap"}
    args.update(kw)
    return E.EnclosureSpec("IronEye", items, **args)


def relay_box(**kw) -> E.EnclosureSpec:
    items = (E.Item(ESP, label="ESP32", face="left"), E.Item(RELAY, label="RELAY", face="top"),
             E.Item(JACK, label="DC", face="left"))
    args = {"mount": "din"}
    args.update(kw)
    return E.EnclosureSpec("Relay box", items, **args)


def pi_case(**kw) -> E.EnclosureSpec:
    items = (E.Item(PI, label="PI", face="bottom"), E.Item(CAMV2, label="CAM", face="front"))
    args = {"mount": "wall_tabs"}
    args.update(kw)
    return E.EnclosureSpec("Pi Zero cam", items, **args)


def by_label(layout: E.Layout) -> dict[str, E.Placed]:
    return {p.label: p for p in layout.placed}


# ----- determinism and the packing invariants -----

def test_planning_is_deterministic_and_never_raises():
    a, b = E.plan_layout(ironeye()), E.plan_layout(ironeye())
    assert a == b
    assert E.layout_json(a) == E.layout_json(b)
    assert E.model_source(ironeye(), a) == E.model_source(ironeye(), b)
    for spec in (ironeye(), relay_box(), pi_case()):
        layout = E.plan_layout(spec)
        assert layout.placed and layout.outer[0] > 0 and layout.inner[2] > 0
    # an empty list still yields a small, valid shell (nothing to place is not an error)
    empty = E.EnclosureSpec('Odd "name"\\', ())
    layout = E.plan_layout(empty)
    assert layout.placed == () and layout.problems == () and layout.outer[0] > 2 * layout.wall
    src = E.model_source(empty, layout)
    assert cadpy.inspect_source(src) == [] and cadpy.parse_brief(src)["title"] == "Odd 'name'"
    # a label typed with quotes never reaches the source as code
    odd = E.plan_layout(E.EnclosureSpec("X", (E.Item(AMP, label='A"MP\\'),)))
    assert odd.placed[0].label == "AMP" and cadpy.inspect_source(E.model_source(empty, odd)) == []


def test_nothing_overlaps_and_everything_stays_inside_the_cavity():
    for spec in (ironeye(), relay_box(), pi_case()):
        layout = E.plan_layout(spec)
        L, W, H = layout.outer
        wall = layout.wall
        problems = E.validate(spec, layout)
        assert not [p for p in problems if "overlap" in p or "outside" in p or "taller" in p], problems
        rects = [(p, p.rect()) for p in layout.placed]
        for p, r in rects:
            assert r[0] >= wall - 1e-6 and r[1] >= wall - 1e-6, (p.label, r)
            assert r[2] <= L - wall + 1e-6 and r[3] <= W - wall + 1e-6, (p.label, r)
            cavity = layout.split[1] if p.on_lid else layout.split[0]
            assert p.z_top <= cavity
        # neighbours on the same half keep the wire trench between their ribs
        for i, (a, ra) in enumerate(rects):
            for b, rb in rects[i + 1:]:
                if a.on_lid != b.on_lid:
                    continue
                assert not E._overlap(ra, rb, gap=spec.channel - 1e-6), (a.label, b.label)
        # towers keep air from every pocket
        for s in layout.screws:
            r_t = E.SCREWS[s["size"]]["od"] / 2.0
            for p, r in rects:
                dx = max(r[0] - s["x"], 0.0, s["x"] - r[2])
                dy = max(r[1] - s["y"], 0.0, s["y"] - r[3])
                assert (dx * dx + dy * dy) ** 0.5 >= r_t + E.FEATURE_AIR - 1e-6, (p.label, s)
        # the cavity holds the tallest base part plus air, and the lid its own stack
        base_tall = max((p.z_top for p in layout.placed if not p.on_lid), default=0.0)
        assert layout.split[0] >= base_tall + E.AIR - 1e-6
        assert layout.inner[2] == layout.split[0] + layout.split[1]
        assert layout.outer == (layout.inner[0] + 2 * wall, layout.inner[1] + 2 * wall, layout.inner[2] + 2 * layout.floor)


# ----- face hints -----

def test_a_wall_hint_puts_the_part_against_that_wall_and_opens_it():
    layout = E.plan_layout(ironeye())
    p = by_label(layout)
    L, W, _ = layout.outer
    chg = p["CHG"]
    assert chg.omit == "left" and chg.x == layout.wall           # the pocket backs onto the wall itself
    assert chg.rot == 0                                         # its USB-C side (left) faces the left wall
    sw = p["SW"]
    assert sw.omit == "top" and abs(sw.y + sw.h - (W - layout.wall)) < 1e-6
    # the switch stands with its face against the wall: along = length, into = height, standing = width
    assert (round(sw.w - 2 * sw.slack, 2), round(sw.h - 2 * sw.slack, 2)) == (8.6, 3.5) and sw.z_top == 5.7
    openings = {(a["face"], a["kind"]): a for a in layout.apertures}
    usb = openings[("left", "usb_c")]
    assert usb["for"] == "CHG" and usb["w"] == 12.0 and usb["h"] == 7.0 and usb["halves"] == ["base"]
    assert chg.y <= usb["x"] <= chg.y + chg.h                  # x on a left wall = the plan y, inside the pocket's span
    assert usb["plan"] == [0.0, usb["x"]] and "estimated" in usb["note"]
    assert usb["z"] - usb["h"] / 2 >= layout.floor + 0.2 - 1e-6  # never into the floor plate
    slot = openings[("top", "switch")]
    assert slot["switch"] == "ss12d00" and (slot["w"], slot["h"]) == (8.5, 3.6) and slot["shape"] == "slot"
    assert sw.x <= slot["x"] <= sw.x + sw.w and slot["plan"] == [slot["x"], W]


def test_a_plate_hint_flips_the_part_and_cuts_its_apertures_through_the_front():
    layout = E.plan_layout(ironeye())
    cam = by_label(layout)["CAM"]
    assert cam.flip is True and cam.face == "front" and cam.mount == "pocket"
    cuts = {a["kind"]: a for a in cam.apertures}
    assert set(cuts) == {"lens", "mic"}
    lens = cuts["lens"]
    assert lens["d"] == 9.0 and lens["recess_d"] == 12.0 and lens["face"] == "front"
    # face-down mirrors the component in x: the lens at (10.5, 8.75) lands at outline x + (21 - 10.5)
    ox, oy = cam.x + cam.slack, cam.y + cam.slack
    assert abs(lens["x"] - (ox + 21.0 - 10.5)) < 0.02 and abs(lens["y"] - (oy + 8.75)) < 0.02
    spk = by_label(layout)["SPK"]
    assert spk.mount == "ring" and spk.apertures[0]["grille"] is True and spk.apertures[0]["d"] == 26.0
    # the XIAO's USB-C is not on a wall: said plainly, never invented
    assert any("CAM" in p and "usb_c" in p and "inside the box" in p for p in layout.problems)


def test_on_lid_items_land_on_the_lid_clear_of_the_lip_band_and_the_towers():
    layout = E.plan_layout(ironeye())
    bat = by_label(layout)["BAT"]
    assert bat.on_lid and bat.mount == "bay"
    L, W, _ = layout.outer
    band = 0.15 + max(1.2, E.WALL_MAX * 0.6) + E.FEATURE_AIR
    r = bat.rect()
    assert r[0] >= layout.wall + band - 1e-6 and r[2] <= L - layout.wall - band + 1e-6
    assert r[1] >= layout.wall + band - 1e-6 and r[3] <= W - layout.wall - band + 1e-6
    assert layout.split[1] >= bat.z_top + 0.5 - 1e-6
    # the base cavity is sized by the base's own stack — the battery rides above it
    assert layout.split[0] == max(p.z_top for p in layout.placed if not p.on_lid) + E.AIR


def test_every_port_facing_the_hinted_wall_opens_and_the_rest_are_named():
    layout = E.plan_layout(pi_case())
    pi = by_label(layout)["PI"]
    assert pi.mount == "standoff" and pi.holes == ((3.5, 3.5), (3.5, 26.5), (61.5, 3.5), (61.5, 26.5))
    assert pi.hole_d == 2.7 and pi.pocket_h == E.STANDOFF_H and pi.z_top == E.STANDOFF_H + 8.0
    bottom = [a for a in layout.apertures if a["face"] == "bottom" and a["for"] == "PI"]
    assert [a["kind"] for a in bottom] == ["hdmi", "micro_usb", "micro_usb"]
    ox = pi.x + pi.slack
    assert [round(a["x"] - ox, 2) for a in bottom] == [12.4, 41.4, 54.0]
    for a in bottom:
        assert a["z"] == layout.floor + E.STANDOFF_H + E.PORT_OPENINGS[a["kind"]][2]
    # the SD slot on the left side is not on the hinted wall: no opening, no complaint (it faces a
    # wall it never reaches — the plan says so only for ports that stay inside)
    assert not [a for a in layout.apertures if a["kind"] == "sd"]
    cam = by_label(layout)["CAM"]
    assert cam.flip and cam.apertures[0]["kind"] == "lens" and cam.mount == "standoff"


def test_wall_mounted_connectors_and_hole_less_boards():
    layout = E.plan_layout(relay_box())
    p = by_label(layout)
    assert p["ESP32"].mount == "rails" and p["ESP32"].pocket_h == E.RAIL_H and p["ESP32"].z_top == E.RAIL_H + 13.0
    dc = p["DC"]
    assert dc.mount == "clip" and dc.rib == 0.0 and dc.pocket_h == 0.0 and dc.omit == "left"
    hole = [a for a in layout.apertures if a["for"] == "DC"][0]
    assert hole["kind"] == "panel" and hole["d"] == E.PANEL_HOLES["barrel_5_5"] and hole["shape"] == "round"
    relay = p["RELAY"]
    assert relay.omit == "top" and relay.rot == 0     # its "back" side (the terminals) faces the top wall
    term = [a for a in layout.apertures if a["for"] == "RELAY"][0]
    assert term["face"] == "top" and (term["w"], term["h"]) == (40.0, 12.0) and "note" not in term
    usb = [a for a in layout.apertures if a["for"] == "ESP32"][0]
    assert usb["face"] == "left" and usb["z"] == layout.floor + E.RAIL_H + E.PORT_OPENINGS["micro_usb"][2]


def test_hints_that_cannot_be_honoured_are_reported_not_guessed():
    spec = E.EnclosureSpec("Odd", (
        E.Item(AMP, label="AMP", face="front"),                 # no aperture to cut
        E.Item(SPK, label="SPK"),                               # a speaker with no face: no grille
        E.Item(SPK, label="LIDSPK", face="front", on_lid=True), # front face but on the lid
        E.Item(AMP, label="AMP2", face="sideways"),             # not a face at all
        E.Item(Component(key="t_sw2", name="mystery switch", category="switch", length=6, width=6, height=4),
               label="SW", face="left"),                        # a switch with no known slot
    ), lid="slide", mount="magnets")
    layout = E.plan_layout(spec)
    text = " ".join(layout.problems)
    assert "AMP: faces 'front' but the library lists no aperture" in text
    assert "SPK: has a speaker aperture but no face" in text
    assert "LIDSPK: faces 'front' but sits on the lid" in text
    assert "AMP2: face 'sideways'" in text
    assert "SW: no slot size is known" in text
    assert "slide lid isn't generated yet" in text and "Mount 'magnets' is unknown" in text
    assert layout.lid == "screw" and layout.mount == "none"
    assert len(layout.placed) == 5 and not [a for a in layout.apertures if a["for"] == "SW"]


def test_quantities_expand_with_numbered_labels_and_default_labels_come_from_the_category():
    spec = E.EnclosureSpec("Twins", (E.Item(SPK, qty=2, face="front"), E.Item(AMP), E.Item(XIAO)))
    labels = [p.label for p in E.plan_layout(spec).placed]
    assert labels == ["SPK1", "SPK2", "AMP", "CAM"]


def test_a_part_too_big_for_the_bed_is_left_out_and_named():
    huge = Component(key="t_huge", name="a slab", category="misc", length=300.0, width=300.0, height=10.0, mount="pocket")
    layout = E.plan_layout(E.EnclosureSpec("Big", (E.Item(huge, label="SLAB"), E.Item(AMP, label="AMP"))))
    assert "SLAB" not in [p.label for p in layout.placed] and "AMP" in [p.label for p in layout.placed]
    assert any("SLAB" in p and "doesn't fit" in p for p in layout.problems)


# ----- validate -----

def test_validate_catches_overlaps_out_of_bed_thin_walls_and_apertures_off_their_wall():
    spec = ironeye(wall=1.0, floor=1.0, bed=(60.0, 60.0, 60.0))
    layout = E.plan_layout(ironeye())
    a = layout.placed[0]
    twin = dataclasses.replace(a, label="TWIN")
    off = {"face": "left", "kind": "usb_c", "x": 2.0, "z": 3.0, "w": 12.0, "h": 7.0, "for": "X"}
    broken = dataclasses.replace(layout, placed=layout.placed + (twin,), apertures=layout.apertures + (off,),
                                 wall=1.0, floor=1.0)
    problems = E.validate(spec, broken)
    text = "\n".join(problems)
    assert f"{a.label} and TWIN overlap" in text
    assert "exceeds the printer's bed" in text
    assert "fragile" in text and "too thin" in text
    assert "X: its usb_c opening runs off the left wall" in text
    assert E.validate(ironeye(), layout) == list(layout.problems)   # the plan itself is clean


# ----- layout_json (§6) -----

def test_layout_json_has_the_section_6_shape_with_the_outer_corner_origin():
    spec = ironeye()
    layout = E.plan_layout(spec)
    j = E.layout_json(layout)
    for key in ("units", "name", "outer", "inner", "wall", "floor", "lid", "components", "apertures", "screws", "problems"):
        assert key in j, key
    assert j["units"] == "mm" and j["name"] == "IronEye" and j["lid"] == "screw"
    assert j["outer"] == list(layout.outer) and j["inner"] == list(layout.inner)
    comp_keys = {"key", "label", "x", "y", "w", "h", "rot", "face", "mount", "on_lid", "z_top", "apertures"}
    for c in j["components"]:
        assert comp_keys <= set(c), c
        for a in c["apertures"]:
            assert {"kind", "x", "y", "face"} <= set(a) and ("d" in a or ("w" in a and "h" in a))
    for a in j["apertures"]:
        assert {"face", "kind", "x", "z", "w", "h", "for"} <= set(a)
    for s in j["screws"]:
        assert {"x", "y", "size", "insert"} <= set(s) and s["size"] in ("M2", "M3")
    # origin = the OUTER bottom-left: a pocket against the left wall starts one wall in
    chg = [c for c in j["components"] if c["label"] == "CHG"][0]
    assert chg["x"] == j["wall"] and chg["on_lid"] is False and chg["mount"] == "pocket"
    bat = [c for c in j["components"] if c["label"] == "BAT"][0]
    assert bat["on_lid"] is True and bat["key"] == "lipo_603048"
    # the print block says where the base's origin sits in the exported mesh (runner: parts along X, 8 mm gap)
    L, W, _ = layout.outer
    over = E.LID_OVERHANG["strap"]
    total = L + 8.0 + L + 2 * over
    assert j["print"]["origins"]["base"] == [round(-total / 2, 2), round(-W / 2, 2)]
    assert j["print"]["origins"]["lid"] == [round(-total / 2 + L + 8.0 + over, 2), round(-W / 2, 2)]
    assert j["print"]["lid_mirror_x"] is True and j["print"]["lid_overhang"] == over
    assert j["problems"] == list(layout.problems)


# ----- model_source -----

def test_model_source_passes_the_gate_and_reads_back_as_brief_params_and_layout():
    spec = ironeye()
    layout = E.plan_layout(spec)
    src = E.model_source(spec, layout)
    assert cadpy.inspect_source(src) == []
    brief = cadpy.parse_brief(src)
    assert brief["title"] == "IronEye"
    assert brief["parts"][0].startswith("base — front shell, prints face down")
    assert brief["parts"][1].startswith("lid — back panel, prints outside-face down")
    assert "lens for CAM" in brief["parts"][0] and "speaker for SPK" in brief["parts"][0]
    assert "usb_c for CHG (left wall)" in brief["parts"][0] and "strap tabs" in brief["parts"][1]
    assert "countersunk (DIN 965)" in brief["summary"] and "Mirrored pairing" in brief["summary"]
    params = {p.name: p for p in cadpy.parse_params(src)}
    for name in ("wall", "clearance", "corner_r", "lid_style", "label_deep"):
        assert name in params, name
    assert params["wall"].minimum == 1.6 and params["wall"].maximum == E.WALL_MAX and params["wall"].value == "2.0"
    assert params["lid_style"].choices == ("screw", "snap") and params["lid_style"].value == "screw"
    for p in layout.placed:
        assert f"{p.label.lower()}_extra" in params, p.label
        assert params[f"{p.label.lower()}_extra"].maximum == 1.0
    assert "# --- Layout ---" in src
    for p in layout.placed:
        assert f"#  {p.label:<9}" in src
    assert 'return {"base": base(), "lid": lid()}' in src
    assert "from helix_parts import *" in src and "import os" not in src
    # every helper the generator relies on exists in the seeded library
    for helper in ("pocket(", "battery_bay(", "lens_bore(", "grille(", "mic_hole(", "port_slot(", "wire_notch(",
                   "deboss_text(", "lip_ring(", "lip_rebate(", "screw_boss(", "csk_hole(", "strap_tab("):
        assert helper in src, helper
        assert f"def {helper[:-1]}" in cadpy.HELIX_LIB, helper


def test_towers_and_holes_are_mirrored_pairs_written_into_the_source():
    spec = ironeye()
    layout = E.plan_layout(spec)
    src = E.model_source(spec, layout)
    L, W, _ = layout.outer
    assert len(layout.screws) in (2, 4)
    for s in layout.screws:
        tx, ty = round(s["x"] - L / 2, 2), round(s["y"] - W / 2, 2)
        assert f"lid hole ({-tx:g}, {ty:g}) <-> tower ({tx:g}, {ty:g})" in src
        assert f"({tx:g}, {ty:g})" in src.split("TOWERS = ")[1].split("\n")[0]
    assert "TOWER_H = BASE_IN + LID_IN - 0.5" in src           # the tower formula
    size = E.SCREWS[layout.screw_size]
    csk = f"csk_hole({size['csk'][0]:g}, {size['csk'][1]:g}"
    assert csk in src and size["const"] in src                   # the fixed insert <-> countersink pairing
    assert "Pos(-tx, ty, 0) * csk_hole" in src                   # the lid's holes are the towers mirrored in x
    # a small shell takes M2, a long one M3 — and the pairing follows
    assert layout.screw_size == ("M3" if max(layout.inner[:2]) >= E.M3_FROM else "M2")


def test_lid_styles_and_mount_options_are_real_geometry_in_the_source():
    base = ironeye()
    snap = E.plan_layout(ironeye(lid="snap"))
    assert snap.screws == () and snap.lid == "snap"
    src = E.model_source(ironeye(lid="snap"), snap)
    assert 'lid_style = "snap"' in src and "TOWERS = ()" in src and "lip_ring(" in src
    for mount, marker in (("strap", "strap_tab("), ("wall_tabs", "TAB_HOLE"), ("flat_feet", "rubber feet"),
                          ("din", "def din_clip()"), ("none", None)):
        layout = E.plan_layout(ironeye(mount=mount))
        text = E.model_source(ironeye(mount=mount), layout)
        assert cadpy.inspect_source(text) == []
        if marker:
            assert marker in text, mount
        if mount == "din":
            assert '"din_clip": din_clip()' in text and "INSERT_M3" in text
            assert E.layout_json(layout)["print"]["parts"] == ["base", "lid", "din_clip"]
    assert base.mount == "strap"


def test_the_layout_table_and_part_functions_carry_the_plan_in_plain_words():
    spec = pi_case()
    layout = E.plan_layout(spec)
    src = E.model_source(spec, layout)
    assert "def pi_mount():" in src and "def cam_mount():" in src
    # holes are written INTO the design (an inline BoardSpec), so the model is a faithful record
    assert 'BoardSpec("Pi Zero 2 W (double)", 65.0, 30.0, ((3.5, 3.5), (3.5, 26.5), (61.5, 3.5), (61.5, 26.5)), 2.7, 8.0)' in src
    assert "standoffs_for(" in src and "_flip(standoffs_for(" in src   # the face-down camera is mirrored
    assert "# through the front face" in src and "lens_bore(9.5, FLOOR + 1, recess_d=12.5" in src
    for kind in ("hdmi", "micro_usb"):
        assert f"# {kind} for PI:" in src
    assert "wall tabs" in src


def test_the_marker_source_parses_and_is_the_80_mm_reference():
    src = E.calibration_marker_source()
    assert cadpy.inspect_source(src) == []
    brief = cadpy.parse_brief(src)
    assert brief["title"] == "HELIX AR marker" and "80" in brief["summary"]
    params = {p.name: p for p in cadpy.parse_params(src)}
    assert params["size"].value == "80.0" and params["thick"].value == "3.0"
    assert 'deboss_text("HELIX 80 mm"' in src and "outer square" in src.lower()
