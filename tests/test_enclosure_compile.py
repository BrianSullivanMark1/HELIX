"""domain.enclosure on the REAL kernel: the generated model.py for the three reference specs
(MAKER_FLOW §8 bar 2) and the AR marker are executed the way helix/cad/runner.py executes them —
helix_parts seeded beside model.py, build() called, parts normalized and laid out, an STL exported,
the runner's own print checks run — and then the assembled fit the coder prompt demands is MEASURED:
every tower meets its mirrored hole, the lip ring sits inside the rebate with a clearance, not a
gap or a collision. A few seconds per test; kept few.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

build123d = pytest.importorskip("build123d")

from helix.cad import runner  # noqa: E402
from helix.domain import cadpy  # noqa: E402
from helix.domain import enclosure as E  # noqa: E402
from helix.domain.components import CATALOG, Aperture, Component, Hole, Port, lipo_from_code  # noqa: E402

from build123d import Compound, Cylinder, Box, Pos, Rot, Align, export_stl  # noqa: E402

CM = (Align.CENTER, Align.CENTER, Align.MIN)


# ----- the reference specs: real catalog parts where the catalog has them, doubles otherwise -----

def _part(key: str, **double) -> Component:
    c = CATALOG.get(key)
    return c if c is not None else Component(key="t_" + key, **double)


def ironeye_spec() -> E.EnclosureSpec:
    xiao = _part("xiao_esp32s3_sense", name="XIAO ESP32-S3 Sense", category="mcu", length=21.0, width=17.5,
                 height=10.0, mount="pocket", ports=(Port("usb_c", "left", 8.75),),
                 apertures=(Aperture("lens", 10.5, 8.75, d=8.0), Aperture("mic", 4.0, 3.0, d=1.5)), confidence=0.85)
    amp = _part("max98357a", name="MAX98357A", category="amp", length=19.4, width=17.8, height=6.0, mount="pocket",
                source="community", confidence=0.6)
    spk = _part("speaker_28mm", name="Speaker 28 mm", category="speaker", length=28.0, width=28.0, height=5.0,
                mount="pocket", apertures=(Aperture("speaker", 14.0, 14.0, d=26.0),), source="community", confidence=0.7)
    chg = _part("tp4056_usb_c", name="TP4056 USB-C", category="charger", length=28.0, width=17.5, height=5.0,
                mount="pocket", ports=(Port("usb_c", "left", 8.75),), source="community", confidence=0.6)
    sw = _part("switch_ss12d00", name="SS12D00 slide switch", category="switch", length=8.6, width=5.7, height=3.5,
               mount="pocket", aliases=("ss12d00",), source="community", confidence=0.6)
    return E.EnclosureSpec("IronEye", (
        E.Item(xiao, label="CAM", face="front"), E.Item(amp, label="AMP"), E.Item(spk, label="SPK", face="front"),
        E.Item(lipo_from_code("603048"), label="BAT", on_lid=True), E.Item(chg, label="CHG", face="left"),
        E.Item(sw, label="SW", face="top"),
    ), mount="strap")


def relay_spec() -> E.EnclosureSpec:
    esp = _part("esp32_devkitc", name="ESP32 DevKitC", category="mcu", length=55.0, width=28.0, height=13.0,
                mount="rails", ports=(Port("micro_usb", "left", 14.0),), source="community", confidence=0.65)
    relay = Component(key="t_relay_2ch", name="Relay module, 2 channel", category="driver", length=50.5, width=38.5,
                      height=19.0, ports=(Port("other", "back", 25.0, width=40.0, height=12.0),), mount="pocket",
                      source="community", confidence=0.6)
    jack = Component(key="t_barrel_panel", name="DC barrel jack 5.5 x 2.1, panel mount", category="connector",
                     length=15.0, width=12.0, height=12.0, ports=(Port("barrel_5_5", "front", 6.0),), mount="clip",
                     source="community", confidence=0.6)
    return E.EnclosureSpec("Relay box", (
        E.Item(esp, label="ESP32", face="left"), E.Item(relay, label="RELAY", face="top"), E.Item(jack, label="DC", face="left"),
    ), mount="din")


def pi_spec() -> E.EnclosureSpec:
    pi = Component(key="t_pi_zero_2w", name="Raspberry Pi Zero 2 W", category="mcu", length=65.0, width=30.0, height=8.0,
                   holes=(Hole(3.5, 3.5, 2.7), Hole(3.5, 26.5, 2.7), Hole(61.5, 3.5, 2.7), Hole(61.5, 26.5, 2.7)),
                   ports=(Port("hdmi", "front", 12.4), Port("micro_usb", "front", 41.4), Port("micro_usb", "front", 54.0)),
                   mount="standoff", confidence=0.9)
    cam = Component(key="t_pi_camera_v2", name="Raspberry Pi Camera Module v2", category="camera", length=25.0,
                    width=24.0, height=9.0, apertures=(Aperture("lens", 12.5, 12.0, d=8.5),), mount="pocket",
                    source="community", confidence=0.6)
    return E.EnclosureSpec("Pi Zero cam", (E.Item(pi, label="PI", face="bottom"), E.Item(cam, label="CAM", face="front")),
                           mount="wall_tabs")


# ----- the runner's way of executing a design -----

@pytest.fixture(scope="module")
def lib(tmp_path_factory):
    """helix_parts.py seeded once per module, importable exactly as the runner imports it."""
    ws = tmp_path_factory.mktemp("lib")
    (ws / cadpy.HELIX_LIB_FILE).write_text(cadpy.HELIX_LIB, encoding="utf-8")
    sys.path.insert(0, str(ws))
    sys.modules.pop("helix_parts", None)
    mod = importlib.import_module("helix_parts")
    yield mod
    sys.path.remove(str(ws))
    sys.modules.pop("helix_parts", None)


def compile_model(src: str, ws: Path) -> list[tuple[str, object]]:
    assert cadpy.inspect_source(src) == []
    (ws / "model.py").write_text(src, encoding="utf-8")
    ns: dict = {"__name__": "helix_model", "__file__": str(ws / "model.py")}
    exec(compile(src, str(ws / "model.py"), "exec"), ns)  # noqa: S102 — the runner's own path
    parts = runner._norm_parts(ns["build"]())
    assert parts
    return parts


def print_checks(parts, ws: Path) -> tuple[list[str], list]:
    """The runner's meta: lay the parts out, export the STL, run its printability verdicts."""
    arranged = runner._arrange(parts)
    shapes = [p for _, p in arranged]
    whole = shapes[0] if len(shapes) == 1 else Compound(children=shapes)
    stl = ws / "model.stl"
    export_stl(whole, str(stl))
    return runner._print_warnings(arranged, stl), arranged


def vol(shape) -> float:
    try:
        return float(shape.volume) if shape is not None else 0.0
    except Exception:  # noqa: BLE001 — an empty result has no volume
        return 0.0


def assert_prints_clean(parts, layout: E.Layout, ws: Path):
    """Nothing floating, nothing too big, no support-class overhang, every part on Z=0, and the
    shells measure what the layout says."""
    warns, arranged = print_checks(parts, ws)
    assert warns == [], warns
    L, W, H = layout.outer
    named = dict(parts)
    for name, shape in parts:
        bb = shape.bounding_box()
        assert abs(bb.min.Z) < 0.01, (name, bb.min.Z)
        for solid in shape.solids():
            assert abs(solid.bounding_box().min.Z) < 0.01, (name, "a piece floats")
        assert max(bb.size.X, bb.size.Y, bb.size.Z) < 250.0, name
    base_bb, lid_bb = named["base"].bounding_box(), named["lid"].bounding_box()
    assert abs(base_bb.size.X - L) < 0.5 and abs(base_bb.size.Y - W) < 0.5
    over = E.LID_OVERHANG.get(layout.mount, 0.0)
    assert abs(lid_bb.size.X - (L + 2 * over)) < 0.5 and abs(lid_bb.size.Y - W) < 0.5
    base_in, lid_in = layout.split
    floor = layout.floor
    assert abs(lid_bb.size.Z - (floor + lid_in)) < 0.05
    # the towers top out 0.5 mm shy of the lid's inner floor (the tower formula)
    tower_top = floor + base_in + lid_in - 0.5
    ring_top = floor + base_in + 2.0 + layout.lip_h
    assert abs(base_bb.size.Z - max(tower_top if layout.screws else 0.0, ring_top)) < 0.05
    # the exported layout says where the shells sit in the arranged mesh
    origins = E.print_origins(layout)
    for name, shape in arranged:
        bb = shape.bounding_box()
        inset = over if name == "lid" else 0.0
        assert abs(bb.min.X + inset - origins[name][0]) < 0.05, (name, bb.min.X, origins[name])
        assert abs(bb.min.Y - origins[name][1]) < 0.05, name
    return arranged


def assert_assembled_fit(parts, layout: E.Layout):
    """The coder prompt's assembly checks, measured: flip the lid about Y onto the base — the lip
    seats in its rebate (no collision), the rims meet (pushing 0.5 mm closer collides), the lip is
    inside the cavity with only its clearance (shifting 0.5 mm sideways collides) — and every
    tower has its mirrored hole where the layout says."""
    named = dict(parts)
    base, lid = named["base"], named["lid"]
    L, W, H = layout.outer
    floor = layout.floor
    lid_asm = Pos(0, 0, H) * (Rot(0, 180, 0) * lid)
    bb = lid_asm.bounding_box()
    assert abs(bb.max.Z - H) < 0.05 and abs(bb.min.Z - (floor + layout.split[0])) < 0.05
    assert vol(base & lid_asm) < 2.0, "the halves collide when assembled"
    assert vol(base & (Pos(0, 0, -0.5) * lid_asm)) > 50.0, "the rims never meet"
    assert vol(base & (Pos(0.5, 0, 0) * lid_asm)) > 1.0, "the lip is not inside the cavity"
    assert vol(base & (Pos(0, 0.5, 0) * lid_asm)) > 1.0
    for s in layout.screws:
        tx, ty = s["x"] - L / 2, s["y"] - W / 2
        hx, hy = -tx, ty                       # the lid's own frame is the plan mirrored in x
        r_t = E.SCREWS[s["size"]]["od"] / 2
        boss = Pos(tx, ty, floor + 1) * Cylinder(r_t - 0.2, 2.0, align=CM)
        assert vol(base & boss) > 20.0, ("no tower at", s)
        pin = Pos(hx, hy, -0.5) * Cylinder(0.8, floor + 1.0, align=CM)
        assert vol(lid & pin) < 0.5, ("no hole through the lid at", s)
        # in the assembled frame the hole lands on the tower: a pin through both is clear of the lid
        # and hits the tower's insert bore (empty) — i.e. nothing blocks a screw
        through = Pos(tx, ty, H - floor - 0.6) * Cylinder(0.8, floor + 1.2, align=CM)
        assert vol(lid_asm & through) < 0.5, ("the lid's hole does not land on the tower", s)


def plan_and_compile(spec, ws):
    layout = E.plan_layout(spec)
    src = E.model_source(spec, layout)
    (ws / "layout.json").write_text(str(E.layout_json(layout)), encoding="utf-8")
    return layout, src, compile_model(src, ws)


# ----- the tests -----

def test_the_ironeye_class_wearable_compiles_prints_clean_and_fits_together(lib, tmp_path):
    spec = ironeye_spec()
    layout, src, parts = plan_and_compile(spec, tmp_path)
    assert [n for n, _ in parts] == ["base", "lid"]
    assert not [p for p in E.validate(spec, layout) if "overlap" in p or "outside" in p]
    assert_prints_clean(parts, layout, tmp_path)
    assert_assembled_fit(parts, layout)
    base = dict(parts)["base"]
    # the camera bore and the speaker grille really go through the front face
    cam = [p for p in layout.placed if p.label == "CAM"][0]
    lens = [a for a in cam.apertures if a["kind"] == "lens"][0]
    L, W, _ = layout.outer
    probe = Pos(lens["x"] - L / 2, lens["y"] - W / 2, -0.5) * Cylinder(lens["d"] / 2 - 0.3, layout.floor + 1.0, align=CM)
    assert vol(base & probe) < 0.5, "the lens bore is blocked"
    spk = [p for p in layout.placed if p.label == "SPK"][0]
    g = spk.apertures[0]
    disc = Pos(g["x"] - L / 2, g["y"] - W / 2, -0.5) * Cylinder(g["d"] / 2, layout.floor + 1.0, align=CM)
    solid_disc = vol(disc)
    assert 0.25 * solid_disc < vol(base & disc) < 0.85 * solid_disc, "the grille is not a field of holes"
    # the USB-C opening is really through the left wall, at the charger
    usb = [a for a in layout.apertures if a["kind"] == "usb_c"][0]
    slot = Pos(-L / 2 + layout.wall / 2, usb["x"] - W / 2, usb["z"]) * Box(layout.wall + 1, usb["w"] - 1.0, usb["h"] - 1.0)
    assert vol(base & slot) < 0.5, "the USB-C opening is blocked"
    # a maker's enclosure, not a bucket: hand-sized and every part named on the inside
    assert L <= 130 and W <= 90 and layout.outer[2] <= 35
    assert sorted(p.label for p in layout.placed) == ["AMP", "BAT", "CAM", "CHG", "SPK", "SW"]


def test_the_relay_box_compiles_with_rails_a_panel_jack_and_a_din_clip(lib, tmp_path):
    spec = relay_spec()
    layout, src, parts = plan_and_compile(spec, tmp_path)
    assert [n for n, _ in parts] == ["base", "lid", "din_clip"]
    assert_prints_clean(parts, layout, tmp_path)
    assert_assembled_fit(parts, layout)
    clip = dict(parts)["din_clip"]
    bb = clip.bounding_box()
    assert abs(bb.size.X - 46.0) < 0.05 and abs(bb.size.Y - 46.0) < 0.05 and len(clip.solids()) == 1
    # the clip's screws and the lid's inserts sit at the same ±x (the symmetric pairing)
    lid = dict(parts)["lid"]
    for sx in (-1.0, 1.0):
        pin = Pos(sx * E.DIN_HOLE_X, 0, -0.5) * Cylinder(0.8, 3.0, align=CM)
        assert vol(clip & pin) < 0.5 and vol(lid & pin) < 0.5
        boss = Pos(sx * E.DIN_HOLE_X, 0, layout.floor + 1) * Cylinder(3.0, 2.0, align=CM)
        assert vol(lid & boss) > 10.0
    # the panel jack's hole and the DevKitC's USB slot are through the left wall
    L, W, _ = layout.outer
    for a in layout.apertures:
        if a["face"] != "left":
            continue
        d = a.get("d", min(a["w"], a["h"]))
        probe = Pos(-L / 2 + layout.wall / 2, a["x"] - W / 2, a["z"]) * Rot(0, 90, 0) * Cylinder(d / 2 - 0.5, layout.wall + 1, align=(Align.CENTER, Align.CENTER, Align.CENTER))
        assert vol(dict(parts)["base"] & probe) < 0.5, a


def test_the_pi_zero_camera_case_compiles_with_standoffs_a_lens_bore_and_wall_tabs(lib, tmp_path):
    spec = pi_spec()
    layout, src, parts = plan_and_compile(spec, tmp_path)
    assert_prints_clean(parts, layout, tmp_path)
    assert_assembled_fit(parts, layout)
    base = dict(parts)["base"]
    L, W, _ = layout.outer
    pi = [p for p in layout.placed if p.label == "PI"][0]
    # a boss stands under every one of the Pi's four holes, on the floor, STANDOFF_H tall
    for hx, hy in pi.holes:
        px, py = E._plan_point(hx, hy, 65.0, 30.0, pi.rot, pi.flip)
        x, y = pi.x + pi.slack + px - L / 2, pi.y + pi.slack + py - W / 2
        ring = Pos(x, y, layout.floor + 0.5) * Cylinder(2.4, E.STANDOFF_H - 1.0, align=CM)
        assert vol(base & ring) > 5.0, ("no standoff at", hx, hy)
        pin = Pos(x, y, layout.floor + 0.5) * Cylinder(0.6, E.STANDOFF_H - 1.0, align=CM)
        assert vol(base & pin) < 0.2, ("no pilot hole at", hx, hy)
    cam = [p for p in layout.placed if p.label == "CAM"][0]
    lens = cam.apertures[0]
    probe = Pos(lens["x"] - L / 2, lens["y"] - W / 2, -0.5) * Cylinder(lens["d"] / 2 - 0.3, layout.floor + 1.0, align=CM)
    assert vol(base & probe) < 0.5
    lid = dict(parts)["lid"]
    for sx in (-1.0, 1.0):
        hole = Pos(sx * (L / 2 + 6.0), 0, -0.5) * Cylinder(E.TAB_HOLE / 2 - 0.4, layout.floor + 1.0, align=CM)
        assert vol(lid & hole) < 0.2, "the wall tab's screw hole is blocked"


def test_the_ar_marker_compiles_flat_and_true_to_80_mm(lib, tmp_path):
    parts = compile_model(E.calibration_marker_source(), tmp_path)
    warns, _ = print_checks(parts, tmp_path)
    assert warns == []
    plate = parts[0][1]
    bb = plate.bounding_box()
    assert abs(bb.size.X - 80.0) < 0.01 and abs(bb.size.Y - 80.0) < 0.01 and abs(bb.size.Z - 3.0) < 0.01
    assert abs(bb.min.Z) < 0.01 and len(plate.solids()) == 1
    # the border recess (0.8 deep, floor at z = 2.2) runs right to the plate's edge — the outer
    # square IS the reference; the corner squares and the centre ring are recessed too
    edge = Pos(39.0, 0, 2.3) * Box(1.6, 20.0, 1.0, align=CM)
    assert vol(plate & edge) < 0.01
    inside = Pos(30.0, 0, 2.3) * Box(4.0, 4.0, 1.0, align=CM)
    assert vol(plate & inside) > 10.0
    ring = Pos(8.5, 0, 2.3) * Box(2.0, 2.0, 1.0, align=CM)
    assert vol(plate & ring) < 0.01
    corner = Pos(30.0, 30.0, 2.3) * Box(6.0, 6.0, 1.0, align=CM)
    assert vol(plate & corner) < 0.01


def test_the_sliders_still_build_at_both_wall_extremes(lib, tmp_path):
    spec = pi_spec()
    layout = E.plan_layout(spec)
    src = E.model_source(spec, layout)
    for i, values in enumerate(({"wall": 1.6, "clearance": 0.2}, {"wall": 3.5, "clearance": 1.5, "corner_r": 6})):
        ws = tmp_path / f"slide{i}"
        ws.mkdir()
        moved = cadpy.set_params(src, values)
        assert {p.name: p.value for p in cadpy.parse_params(moved)}["wall"] == str(values["wall"])
        parts = compile_model(moved, ws)
        warns, _ = print_checks(parts, ws)
        assert warns == [], (values, warns)
        base = dict(parts)["base"].bounding_box()
        assert abs(base.size.X - (layout.inner[0] + 2 * values["wall"])) < 0.5


def test_a_snap_lid_with_feet_and_a_lid_side_port_notches_both_halves(lib, tmp_path):
    """The paths the three reference shells don't take: a friction (snap) lid — no towers, no
    holes — the four foot recesses on the back, and an on-lid charger whose USB-C straddles the
    joint, so BOTH halves carry a notch and the assembled walls are open at it."""
    chg = Component(key="t_chg_lid", name="TP4056 USB-C", category="charger", length=28.0, width=17.5, height=5.0,
                    mount="pocket", ports=(Port("usb_c", "left", 8.75),), source="community", confidence=0.6)
    amp = Component(key="t_amp2", name="MAX98357A", category="amp", length=19.4, width=17.8, height=6.0,
                    mount="pocket", source="community", confidence=0.6)
    spec = E.EnclosureSpec("Snap box", (E.Item(amp, label="AMP"), E.Item(chg, label="CHG", face="left", on_lid=True)),
                           lid="snap", mount="flat_feet")
    layout, src, parts = plan_and_compile(spec, tmp_path)
    assert layout.screws == () and layout.lid == "snap"
    assert_prints_clean(parts, layout, tmp_path)
    assert_assembled_fit(parts, layout)          # the lip still seats; no screws to check
    usb = [a for a in layout.apertures if a["kind"] == "usb_c"][0]
    assert usb["halves"] == ["base", "lid"], usb  # the plug straddles the rim: a notch in each half
    L, W, H = layout.outer
    base, lid = dict(parts)["base"], dict(parts)["lid"]
    lid_asm = Pos(0, 0, H) * (Rot(0, 180, 0) * lid)
    slot = Pos(-L / 2 + layout.wall / 2, usb["x"] - W / 2, usb["z"]) * Box(layout.wall + 1, usb["w"] - 1.0, usb["h"] - 1.0)
    assert vol(base & slot) < 0.5 and vol(lid_asm & slot) < 0.5, "the joint notch is not open on both halves"
    fx, fy = L / 2 - E.FOOT_INSET, W / 2 - E.FOOT_INSET
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            pad = Pos(sx * fx, sy * fy, -0.5) * Cylinder(E.FOOT_D / 2 - 0.5, 1.4, align=CM)
            assert vol(lid & pad) < 0.01, "a foot recess is missing"
    # the back is otherwise solid: a probe 0.9 mm into the plate meets ~25 mm³ of material
    assert vol(lid & (Pos(0, 0, -0.5) * Cylinder(3.0, 1.4, align=CM))) > 20.0


def test_every_new_helper_compiles_to_its_documented_geometry(lib):
    hp = lib
    p = hp.pocket(21.0, 17.5, 6.0, 1.6, 1.1)
    bb = p.bounding_box()
    assert (round(bb.size.X, 2), round(bb.size.Y, 2), round(bb.size.Z, 2)) == (26.4, 22.9, 6.0) and abs(bb.min.Z) < 1e-6
    assert vol(p & Box(20.0, 16.0, 6.0, align=CM)) < 1e-6, "a pocket has no floor and no cap"
    u = hp.pocket(28.0, 17.5, 4.0, 1.6, 1.6, omit="left")
    assert vol(u & Pos(-16.2, 0, 2) * Box(1.0, 10.0, 2.0)) < 1e-6, "the omitted rib is gone"
    assert vol(u & Pos(16.2, 0, 2) * Box(1.0, 10.0, 2.0)) > 10.0, "the other rib stays"
    pf = hp.pocket_for((19.4, 17.8, 6.0))
    assert abs(pf.bounding_box().size.Z - 5.0) < 1e-6
    key = "arduino_uno" if "arduino_uno" in hp.BOARDS else sorted(hp.BOARDS)[0]
    assert hp.pocket_for(key).bounding_box().size.X > hp.BOARDS[key].length
    bay = hp.battery_bay(48.0, 30.0, 6.5, 1.6, 1.6, side="right")
    assert vol(bay & Pos(26.4, 0, 3) * Box(1.0, 6.0, 2.0)) < 1e-6, "the lead gap is open"
    lb = hp.lens_bore(9.0, 3.0, recess_d=12.0)
    bb = lb.bounding_box()
    assert abs(bb.min.Z + 1.0) < 1e-6 and abs(bb.max.Z - 3.0) < 1e-6 and abs(bb.size.X - 12.0) < 1e-6
    g = hp.grille(26.0, depth=3.0)
    assert 40 <= len(g.solids()) <= 80 and g.bounding_box().size.X <= 26.0
    assert abs(hp.mic_hole(1.5, 3.0).bounding_box().size.X - 1.5) < 1e-6
    assert abs(hp.led_window(5.6, 3.0).bounding_box().size.X - 5.6) < 1e-6
    sw = hp.screen_window(24.0, 12.0, 1.0, 3.0).bounding_box()
    assert abs(sw.size.X - 24.0) < 1e-6 and abs(sw.size.Y - 12.0) < 1e-6 and abs(sw.min.Z + 1.0) < 1e-6
    for kind, (shape, w, h) in hp.SWITCH_SLOTS.items():
        bb = hp.switch_slot(kind, 3.0).bounding_box()
        assert abs(bb.size.X - w) < 1e-6 and abs(bb.size.Y - h) < 1e-6, kind
    ps = hp.port_slot(12.0, 7.0, 2.0).bounding_box()
    assert (round(ps.size.X, 2), round(ps.size.Y, 2), round(ps.size.Z, 2)) == (12.0, 4.0, 7.0)
    wn = hp.wire_notch(4.0, 5.0).bounding_box()
    assert abs(wn.min.Z) < 1e-6 and abs(wn.size.Z - 6.0) < 1e-6 and abs(wn.size.Y - 4.0) < 1e-6
    txt = hp.deboss_text("CAM", 3.4, 0.4)
    assert len(txt.solids()) >= 3 and abs(txt.bounding_box().size.Z - 0.6) < 1e-6
    tag = hp.deboss_tag("CAM", 3.4, 0.4)
    assert abs(tag.bounding_box().size.Z - 0.6) < 1e-6 and 5.0 < tag.bounding_box().size.X < 8.0
    assert hp.SWITCH_SLOTS == E.SWITCH_SLOTS
