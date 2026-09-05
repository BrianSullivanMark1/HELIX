"""The component library (MAKER_FLOW §2): the schema round-trips, lookups never guess, LiPo codes
and listing dimensions read as millimetres, kit_for maps needs to roles, the catalog holds ≥ 90 real
parts whose numbers are internally consistent — and the ComponentService reads like a friend and
never names a fenced tool."""
from __future__ import annotations

import pytest

from helix.domain import components as lib
from helix.domain.components import (
    APERTURE_KINDS, CATALOG, CATEGORIES, FACES, MOUNTS, PORT_KINDS, SIDES, SOURCES, TAGS,
    Aperture, Component, Hole, Port, adhoc, dims_from_text, find, from_json, kit_for, lipo_from_code,
    need_phrases, search, to_json,
)
from helix.services.components import ComponentService, _split_needs
from helix.services.parts import PartsService

FENCED = ("design_enclosure", "save_parts", "stage_parts", "add_to_cart", "open_cart", "check_fit",
          "camera_measure", "build_3d_model", "remove_parts", "check_amazon_cart", "print_hologram")


class _Store:
    def __init__(self):
        self.d = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


# ----- schema -----
def test_schema_round_trips_through_json_and_tolerates_missing_optionals():
    c = CATALOG["pi_camera_v2"]
    d = to_json(c)
    assert d["holes"][0] == {"x": 2.0, "y": 2.0, "d": 2.2}
    assert from_json(d) == c
    bare = from_json({"key": "thing", "length": 10, "width": 5, "height": 2})
    assert bare.name == "thing" and bare.category == "misc" and bare.mount == "standoff"
    assert bare.holes == () and bare.ports == () and bare.confidence == 0.9 and not bare.approx
    with pytest.raises(ValueError):
        from_json({"key": "no-dims", "length": 10})
    with pytest.raises(ValueError):
        from_json({"length": 1, "width": 1, "height": 1})
    with pytest.raises(ValueError):
        from_json("not a dict")


def test_component_properties():
    c = Component(key="k", name="n", category="misc", length=10, width=4, height=2, confidence=0.6)
    assert c.approx and c.footprint == (10, 4)
    assert not Component(key="k", name="n", category="misc", length=1, width=1, height=1, confidence=0.7).approx


# ----- find / search -----
@pytest.mark.parametrize("spoken, key", [
    ("xiao s3 sense", "xiao_esp32s3_sense"),
    ("XIAO ESP32-S3 Sense", "xiao_esp32s3_sense"),
    ("the xiao s3 sense board", "xiao_esp32s3_sense"),
    ("esp32 cam", "esp32_cam"),
    ("ESP32-CAM", "esp32_cam"),
    ("max 98357", "max98357a"),
    ("MAX98357A", "max98357a"),
    ("esp32_devkitc", "esp32_devkitc"),
    ("pi zero", "pi_zero_2w"),
    ("Raspberry Pi Pico W", "pi_pico_w"),
    ("uno", "arduino_uno"),
    ("hc-sr04", "hc_sr04"),
    ("28mm speaker", "speaker_28mm"),
    ("tp4056", "tp4056_usb_c"),
    ("inmp 441", "inmp441"),
    ("SS12D00", "switch_ss12d00"),
    ("nema 17", "nema17"),
])
def test_find_resolves_keys_names_and_spoken_aliases(spoken, key):
    assert find(spoken) is CATALOG[key]


def test_find_never_guesses():
    assert find("") is None and find("   ") is None
    assert find("esp32") is None          # several boards
    assert find("oled") is None           # several displays
    assert find("speaker") is None        # use search()
    assert find("warp drive") is None
    assert find("the module") is None     # filler only


def test_find_loose_accepts_one_identifier_plus_descriptors_only():
    from helix.domain.components import find_loose
    assert find_loose("MAX98357A amp").key == "max98357a"
    assert find_loose("Pi Zero 2 W board").key == "pi_zero_2w"
    assert find_loose("INMP441 I2S mic").key == "inmp441"
    assert find_loose("xiao s3 sense").key == "xiao_esp32s3_sense"
    assert find_loose("LED ring") is None            # "ring" is a real remainder — not a 5 mm LED
    assert find_loose("esp32 board") is None         # several boards
    assert find_loose("uno and nano") is None        # two parts in one name
    assert find_loose("Camera brain") is None and find_loose("") is None


def test_find_reads_a_lipo_code_from_battery_text():
    c = find("3.7V 603048 500mAh")
    assert c is not None and c.key == "lipo_603048" and (c.length, c.width, c.height) == (48.0, 30.0, 6.0)
    assert find("603048").key == "lipo_603048"
    assert find("lipo 503450").key == "lipo_503450"
    assert find("part number 123456 bracket") is None  # six digits with no battery words is not a cell


def test_search_ranks_partial_matches_and_filters_by_category():
    hits = search("speaker")
    assert hits and all(c.category == "speaker" for c in hits[:5])
    assert search("esp32")[0].category == "mcu"
    assert search("i2s")[0].key in ("inmp441", "max98357a", "max98357a_adafruit", "inmp441_round")
    assert all(c.category == "mic" for c in search("i2s", category="mic"))
    assert search("") == [] and search("zzzz-nothing") == []
    assert search("xiao sense")[0].key == "xiao_esp32s3_sense"


# ----- LiPo codes, ad-hoc, dims -----
def test_lipo_from_code_shapes_a_cell():
    c = lipo_from_code("603048")
    assert c.key == "lipo_603048" and c.category == "battery" and c.mount == "pocket"
    assert (c.length, c.width, c.height) == (48.0, 30.0, 6.0) and c.clearance == 1.0
    assert c.ports[0].kind == "jst_ph" and c.ports[0].side == "left" and c.ports[0].x == 15.0
    assert c.source == "derived" and c.confidence == 0.8 and "power" in c.tags
    assert lipo_from_code("3.7V 103450 2000mAh lipo").key == "lipo_103450"
    assert lipo_from_code("nothing") is None and lipo_from_code("000000") is None
    assert lipo_from_code("12345678") is None  # eight digits is not a code


def test_adhoc_sorts_dims_and_keys_from_the_name():
    c = adhoc("Mystery amp", 4, 21.1, 17.6)
    assert c.key == "adhoc_mysteryamp" and (c.length, c.width, c.height) == (21.1, 17.6, 4.0)
    assert c.mount == "pocket" and c.source == "measured" and c.confidence == 0.7 and c.category == "misc"
    assert adhoc("", 1, 2, 3).key == "adhoc_part"


@pytest.mark.parametrize("text, expected", [
    ("1.02 x 0.67 x 0.2 inches", (25.91, 17.02, 5.08)),
    ("Item dimensions L x W x H: 1.02 x 0.67 x 0.2 inches", (25.91, 17.02, 5.08)),
    ("26x17x4.5mm", (26.0, 17.0, 4.5)),
    ("26 x 17 x 4.5 mm", (26.0, 17.0, 4.5)),
    ("27 mm × 40.5 mm", (40.5, 27.0, 0.0)),
    ("27mm×40.5mm", (40.5, 27.0, 0.0)),
    ("2.16 x 1.06 inches", (54.86, 26.92, 0.0)),
    ('0.8" x 0.7" x 0.1"', (20.32, 17.78, 2.54)),
    ('0.8" x 0.7"', (20.32, 17.78, 0.0)),
    ("4.3 x 2.1 cm", (43.0, 21.0, 0.0)),
    ("Size: 45*20*15", (45.0, 20.0, 15.0)),
    ("40.5 by 27 by 4.5 mm", (40.5, 27.0, 4.5)),
    ("Dimensions 1.0 x 0.7 x 0.2 in", (25.4, 17.78, 5.08)),
])
def test_dims_from_text_reads_listing_lines_to_mm(text, expected):
    assert dims_from_text(text) == expected


def test_dims_from_text_refuses_non_dimensions():
    assert dims_from_text("") is None and dims_from_text(None) is None
    assert dims_from_text("3.7V 603048 500mAh") is None
    assert dims_from_text("0 x 0 mm") is None
    assert dims_from_text("a 2x speaker set") is None


# ----- needs → roles -----
def test_need_phrases_take_every_role_in_a_sentence_whole_word_and_longest_first():
    assert need_phrases("speech that runs on a battery") == ["speech", "battery"]
    assert need_phrases("night vision please") == ["night vision"]
    assert need_phrases("a microcontroller") == ["microcontroller"]   # never "mic"
    assert lib.need_tag("microcontroller") == "compute"
    assert lib.need_tag("night vision") == "vision" and lib.need_tag("Mic") == "hearing"
    assert lib.need_tag("warp drive") is None and lib.need_tag("") is None


def test_kit_for_maps_need_words_to_ranked_roles_and_keeps_unknowns():
    kit = kit_for(["vision", "hearing", "speaking", "battery", "charging", "compute", "wifi", "display",
                   "night vision", "warp drive", "", "MOTION"])
    assert kit["unknown"] == ["warp drive"]
    for role in ("vision", "hearing", "speaking", "power", "charging", "compute", "wireless", "display", "motion"):
        assert kit[role] and all(isinstance(c, Component) and role in c.tags for c in kit[role])
    assert kit["vision"][0].key in ("xiao_esp32s3_sense", "pi_camera_v3", "esp32_cam")
    assert kit["hearing"][0].key in ("inmp441", "xiao_esp32s3_sense")
    assert {c.key for c in kit["speaking"][:2]} <= {"max98357a", "speaker_28mm", "max98357a_adafruit"}
    assert kit["charging"][0].key == "tp4056_usb_c"
    assert kit["compute"][0].category == "mcu"
    assert kit_for([]) == {} and kit_for(["nonsense"]) == {"unknown": ["nonsense"]}
    # a specific need word pulls its parts to the front
    assert kit_for(["temperature"])["sensing"][0].key in ("dht22", "bme280_gy", "ds18b20_probe")
    assert kit_for(["servo"])["motion"][0].key == "servo_sg90"
    # ranking: an IR LED is tagged vision but is not a camera — it never leads the vision role
    assert kit["vision"][0].key != "ir_led_850nm"


# ----- catalog invariants -----
def test_catalog_size_and_verified_share():
    assert len(CATALOG) >= 90
    assert sum(1 for c in CATALOG.values() if c.confidence >= 0.85) >= 25
    assert len({c.key for c in CATALOG.values()}) == len(CATALOG)
    for key in ("xiao_esp32s3_sense", "esp32_devkitc", "max98357a", "inmp441", "tp4056_usb_c", "speaker_28mm"):
        assert key in CATALOG  # the six seed keys survive


@pytest.mark.parametrize("key", sorted(CATALOG))
def test_catalog_entry_is_consistent(key):
    c = CATALOG[key]
    assert c.key == key and key == key.lower() and " " not in key
    assert c.name and c.search, key
    assert c.category in CATEGORIES and c.mount in MOUNTS and c.source in SOURCES
    assert c.length > 0 and c.width > 0 and c.height > 0
    assert 0.0 <= c.confidence <= 1.0 and c.clearance >= 0
    assert c.tags and all(t in TAGS for t in c.tags), key
    assert all(isinstance(a, str) and a for a in c.aliases)
    if c.source == "community":
        assert c.confidence <= 0.7, f"{key}: community parts are approximate"
    for h in c.holes:
        assert isinstance(h, Hole) and 0 < h.x < c.length and 0 < h.y < c.width and h.d > 0, (key, h)
        assert c.mount == "standoff", f"{key}: holes mean standoffs"
    for p in c.ports:
        assert isinstance(p, Port) and p.side in SIDES and p.kind in PORT_KINDS, (key, p)
        along = c.width if p.side in ("left", "right") else c.length
        assert 0 <= p.x <= along, (key, p)
        assert p.width >= 0 and p.height >= 0
    for a in c.apertures:
        assert isinstance(a, Aperture) and a.face in FACES and a.kind in APERTURE_KINDS, (key, a)
        assert 0 <= a.x <= c.length and 0 <= a.y <= c.width, (key, a)
        assert (a.d > 0) or (a.w > 0 and a.h > 0), (key, a)
    # spoken aliases resolve back to the entry (no two entries share an alias)
    for alias in c.aliases:
        assert find(alias) is c, (key, alias)
    assert find(key) is c and find(c.name) is c


def test_the_cadpy_twins_exist_with_the_same_keys():
    for key in ("arduino_uno", "arduino_mega", "arduino_nano", "pi_pico", "pi_4", "esp32_devkitc",
                "esp8266_nodemcu", "wemos_d1_mini", "relay_1ch", "relay_2ch", "relay_4ch", "buck_lm2596",
                "breadboard_half"):
        assert key in CATALOG, key
    assert "pi_zero_2w" in CATALOG  # cadpy's pi_zero
    # the ones with verified drawings carry holes; the community modules honestly do not
    assert CATALOG["arduino_uno"].holes and CATALOG["pi_4"].holes and CATALOG["pi_pico"].holes
    assert not CATALOG["esp32_devkitc"].holes and not CATALOG["relay_4ch"].holes
    assert CATALOG["esp32_devkitc"].mount == "rails"


def test_the_reference_specs_parts_are_all_in_the_library():
    """MAKER_FLOW §8.2: the three reference enclosures resolve every part."""
    for spoken in ("xiao esp32s3 sense", "max98357a", "28mm speaker", "3.7V 603048 lipo", "tp4056 usb c",
                   "ss12d00", "esp32 devkitc", "2 channel relay", "barrel jack", "pi zero 2 w", "pi camera v2"):
        assert find(spoken) is not None, spoken


def test_pi_camera_holes_match_the_official_drawing():
    c = CATALOG["pi_camera_v2"]
    xs = sorted({h.x for h in c.holes}); ys = sorted({h.y for h in c.holes})
    assert xs == [2.0, 23.0] and ys == [2.0, 14.5]      # 21 × 12.5 pattern, 2.2 mm holes
    assert all(h.d == 2.2 for h in c.holes)
    lens = c.apertures[0]
    assert lens.kind == "lens" and (lens.x, lens.y) == (13.8, 14.4)  # RPI-CAM-V2 drawing
    assert c.ports[0].side == "back"  # the ribbon leaves the edge opposite the holes


# ----- the service -----
def _svc(rows=None):
    parts = PartsService(_Store(), clock=lambda: "2026-09-04T12:00")
    if rows:
        parts.save("IronEye", rows)
    return ComponentService(parts), parts


def test_resolve_catalog_then_lipo_then_adhoc():
    svc, _ = _svc()
    assert svc.resolve("xiao s3 sense").key == "xiao_esp32s3_sense"
    assert svc.resolve("3.7V 603048 500mAh").key == "lipo_603048"
    a = svc.resolve("mystery amp", dims=(21.1, 17.6, 4))
    assert a.key == "adhoc_mysteryamp" and (a.length, a.width, a.height) == (21.1, 17.6, 4.0)
    b = svc.resolve("some blob", dims="1.02 x 0.67 x 0.2 inches")
    assert (b.length, b.width, b.height) == (25.91, 17.02, 5.08)
    assert svc.resolve("mystery amp") is None and svc.resolve("", dims=(1, 2, 3)) is None
    assert svc.resolve("thing", dims="no size here") is None and svc.resolve("thing", dims=(0, 0, 0)) is None


def test_resolve_parts_maps_rows_by_key_name_code_and_dims_and_names_the_rest():
    svc, _ = _svc([
        {"name": "Camera brain", "component": "xiao s3 sense", "face": "front"},
        {"name": "MAX98357A amp"},
        {"name": "LiPo 603048", "on_lid": True},
        {"name": "Mystery board", "length": 21.1, "width": 17.6, "height": 4},
        {"name": "Nothing known"},
        {"name": "Battery pack", "spec": "3.7V 103450 2000mAh"},
    ])
    resolved, unresolved = svc.resolve_parts("iron eye")
    got = {row.name: c.key for row, c in resolved}
    assert got == {"Camera brain": "xiao_esp32s3_sense", "MAX98357A amp": "max98357a",
                   "LiPo 603048": "lipo_603048", "Mystery board": "adhoc_mysteryboard",
                   "Battery pack": "lipo_103450"}
    assert [r.name for r in unresolved] == ["Nothing known"]
    assert svc.resolve_parts("no such project") == ([], [])
    assert ComponentService(None).resolve_parts("IronEye") == ([], [])


def test_split_needs_reads_a_spoken_brief():
    needs = _split_needs("a hat cam with vision, hearing and speech that runs on a battery and charges "
                         "over usb-c, night vision, warp drive")
    assert needs[:4] == ["vision", "hearing", "speech", "battery"]
    assert "night vision" in needs and "warp drive" in needs
    assert _split_needs("") == []


def test_suggest_reads_like_a_friend_and_names_no_fenced_tool():
    svc, _ = _svc()
    text = svc.suggest("a hat cam with vision, hearing and speech, runs on a battery, charges over usb-c, "
                       "night vision, and a warp drive")
    for role in ("Vision", "Hearing", "Speaking", "Power", "Charging"):
        assert role in text, role
    # 2–3 candidates per role, each with a size and a reason
    vision = text.split("Vision (camera):")[1].split("\n\n")[0]
    lines = [ln for ln in vision.splitlines() if ln.startswith("- ")]
    assert 2 <= len(lines) <= 3 and all("×" in ln and " mm" in ln and ": " in ln for ln in lines)
    # honesty: what the library doesn't know, and what doesn't exist
    assert "No single ESP32-CAM board has built-in IR night vision" in text
    assert "850 nm" in text and "warp drive" in text
    assert "(approx)" in text
    for fenced in FENCED:
        assert fenced not in text, fenced
    assert "Seeed XIAO ESP32-S3 Sense" in text and "TP4056" in text


def test_suggest_with_nothing_mappable_asks_plainly():
    svc, _ = _svc()
    text = svc.suggest("")
    assert "Tell me what the device should do" in text
    text = svc.suggest("warp drive and a flux capacitor")
    assert "I don't have library parts for" in text and "warp drive" in text
    for fenced in FENCED:
        assert fenced not in text


def test_suggest_covers_a_sensor_node():
    svc, _ = _svc()
    text = svc.suggest("a garden sensor node: temperature, humidity, a screen, wifi")
    assert "Sensing" in text and "Display" in text and "Wireless" in text
    assert "DHT22" in text or "BME280" in text


def test_describe_is_one_honest_line():
    line = ComponentService.describe(CATALOG["xiao_esp32s3_sense"])
    assert line.startswith("Seeed XIAO ESP32-S3 Sense — 21 × 17.8 × 15 mm; pocket mount")
    assert "USB-C on the left" in line and "lens (top)" in line and "confidence 0.75 (datasheet)" in line
    assert "\n" not in line
    approx = ComponentService.describe(CATALOG["max98357a"])
    assert "approx" in approx and "community" in approx
    holes = ComponentService.describe(CATALOG["arduino_uno"])
    assert "4 mounting holes" in holes and "standoff mount" in holes
