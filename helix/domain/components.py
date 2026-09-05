"""The component library — the real parts a maker builds with, as data: outline, height, mounting
holes, ports, apertures, how it mounts, and how sure we are. Pure, no I/O.

WHY: an enclosure is only as good as the numbers its pockets are sized from. Until now those numbers
were typed by the model from memory into each design ("XIAO_W, XIAO_H = 22, 18") and were guesses.
Here they are a library entry with a source and a confidence, shared by the parts list, the
enclosure generator (helix_parts' BOARDS are rendered from this catalog), the AR fit check, and the
Amazon search phrase that finds the part. A dimension we don't know is NOT here: an unknown hole
pattern means `holes=()` and `mount="pocket"` — a wrong hole is worse than a pocket.

Contract: READ_ME/MAKER_FLOW.md §2. Keep the schema names exactly; fill the catalog (≥ 90 parts).

Conventions (plan view, the part lying flat, component side up, origin at its bottom-left):
- length = x, width = y, height = the tallest point (headers, cans, lenses, shafts noted when they
  are not counted).
- A Port's `x` is measured along its side from the side's LEFT end as seen from OUTSIDE the part:
  front (y=0) → x_plan; back (y=W) → L − x_plan; left (x=0) → W − y_plan; right (x=L) → y_plan.
  Ports on a short edge that are centred get x = W/2 (left/right) or L/2 (front/back).
- Confidence: ≥ 0.85 the outline came from a manufacturer drawing or product page that states it;
  0.7–0.85 a datasheet-grade number with an approximate detail (a port offset read off a photo);
  ≤ 0.7 community-measured (generic modules that vary by vendor — leave 0.5 mm more room).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

CATEGORIES = (
    "mcu", "camera", "mic", "amp", "speaker", "battery", "charger", "power", "switch", "button",
    "display", "sensor", "motor", "driver", "led", "connector", "storage", "comm", "misc",
)
MOUNTS = ("standoff", "rails", "pocket", "clip", "strap", "adhesive")
SIDES = ("front", "back", "left", "right")
FACES = ("top", "bottom", "front", "back", "left", "right")
TAGS = ("vision", "hearing", "speaking", "compute", "power", "charging", "sensing", "display",
        "motion", "storage", "wireless", "lighting", "input")
PORT_KINDS = ("usb_c", "micro_usb", "usb_a", "barrel_5_5", "jst_ph", "jst_xh", "sd", "hdmi",
              "audio_3_5", "header", "antenna", "other")
APERTURE_KINDS = ("lens", "mic", "speaker", "led", "screen", "button", "sensor", "vent", "shaft",
                  "antenna")
SOURCES = ("datasheet", "measured", "community", "derived")


@dataclass(frozen=True)
class Hole:
    """A mounting hole, mm from the component's bottom-left corner in plan view."""

    x: float
    y: float
    d: float = 3.2


@dataclass(frozen=True)
class Port:
    """A connector leaving one side of the component (the enclosure needs an opening there)."""

    kind: str          # usb_c | micro_usb | usb_a | barrel_5_5 | jst_ph | jst_xh | sd | hdmi | audio_3_5 | header | antenna | other
    side: str          # front | back | left | right (plan view, +y is "back")
    x: float           # centre offset along that side, mm from the side's left end viewed from outside
    width: float = 0.0   # opening the enclosure needs; 0 = library default for the kind
    height: float = 0.0


@dataclass(frozen=True)
class Aperture:
    """Something that must see out: a lens, a mic hole, a speaker cone, an LED, a screen, a button."""

    kind: str          # lens | mic | speaker | led | screen | button | sensor | vent | shaft | antenna
    x: float           # plan-view position from bottom-left, mm
    y: float
    d: float = 0.0     # round
    w: float = 0.0     # rectangular
    h: float = 0.0
    face: str = "top"  # which way it looks: top (component face up) | bottom | front | back | left | right


@dataclass(frozen=True)
class Component:
    key: str
    name: str
    category: str
    length: float                  # L (x) mm, lying flat
    width: float                   # W (y) mm
    height: float                  # H (z) mm — the tallest point, connectors and headers included
    holes: tuple[Hole, ...] = ()
    ports: tuple[Port, ...] = ()
    apertures: tuple[Aperture, ...] = ()
    mount: str = "standoff"        # standoff | rails | pocket | clip | strap | adhesive
    clearance: float = 0.5         # extra mm per side the enclosure adds around it
    aliases: tuple[str, ...] = ()
    search: str = ""               # the Amazon search phrase that finds this part
    source: str = "datasheet"      # datasheet | measured | community | derived
    confidence: float = 0.9        # 1.0 official drawing … < 0.7 = approx (leave 0.5 mm more room)
    tags: tuple[str, ...] = ()
    notes: str = ""

    @property
    def approx(self) -> bool:
        return self.confidence < 0.7

    @property
    def footprint(self) -> tuple[float, float]:
        return (self.length, self.width)


# ----- helpers -----
_NORM_RE = re.compile(r"[^a-z0-9]+")
_FILLER = frozenset({"a", "an", "the", "module", "board", "breakout", "sensor", "chip", "dev", "kit",
                     "development", "mini", "v1", "v2", "v3", "v4", "version"})


def _norm(text: str) -> str:
    return _NORM_RE.sub("", (text or "").lower())


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9.]+", (text or "").lower()) if w]


def _lipo_code(text: str) -> str | None:
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", text or "")
    return m.group(1) if m else None


def lipo_from_code(code: str) -> Component | None:
    """A pouch LiPo cell from its size code TTWWLL: thickness (0.1 mm), width, length in mm —
    "603048" is 6.0 × 30 × 48 mm. Tolerates surrounding text ("3.7V 603048 500mAh")."""
    c = _lipo_code(code or "")
    if c is None:
        return None
    t, w, l = int(c[0:2]) / 10.0, float(c[2:4]), float(c[4:6])
    if t <= 0 or w <= 0 or l <= 0:
        return None
    return Component(
        key=f"lipo_{c}", name=f"LiPo cell {c} ({l:g} × {w:g} × {t:g} mm)", category="battery",
        length=l, width=w, height=t, mount="pocket", clearance=1.0,
        ports=(Port("jst_ph", "left", w / 2),),
        aliases=(c, f"lipo {c}"), search=f"3.7V {c} lipo battery JST", source="derived",
        confidence=0.8, tags=("power",),
        notes="Size code = thickness/10, width, length. Leads leave a short edge; leave a lead bay. "
              "Real cells run up to 1 mm over the code once the wrap and tabs are counted.",
    )


def adhoc(name: str, length: float, width: float, height: float, *, category: str = "misc",
          mount: str = "pocket", source: str = "measured", confidence: float = 0.7) -> Component:
    """A part the user measured (camera ruler) or read off a listing — a pocket-mounted box."""
    dims = sorted((abs(float(length)), abs(float(width)), abs(float(height))), reverse=True)
    key = "adhoc_" + (_norm(name) or "part")[:40]
    return Component(key=key, name=" ".join((name or "part").split())[:80], category=category,
                     length=dims[0], width=dims[1], height=dims[2], mount=mount, source=source,
                     confidence=confidence)


_NUM = r"(\d+(?:[.,]\d+)?)"
_UNIT = r"(mm|cm|inches|inch|in|\"|″|”)"
_SEP = r"\s*(?:[x×X*]|by)\s*"
_DIM_RE = re.compile(
    rf"{_NUM}\s*{_UNIT}?{_SEP}{_NUM}\s*{_UNIT}?(?:{_SEP}{_NUM}\s*{_UNIT}?)?"
)


def dims_from_text(text: str) -> tuple[float, float, float] | None:
    """Read "1.02 x 0.67 x 0.2 inches", "26x17x4.5mm", "27 mm × 40.5 mm", "2.16 x 1.06 inches",
    '0.8" x 0.7"' from an Amazon spec line to mm, sorted L ≥ W ≥ H. Two numbers read as a flat part
    (height 0). A unit given once — after any number, or as a trailing word — applies to all; no
    unit at all reads as millimetres. None when nothing dimension-shaped is there."""
    t = text or ""
    m = _DIM_RE.search(t)
    if not m:
        return None
    nums = [m.group(1), m.group(3), m.group(5)]
    units = [u for u in (m.group(2), m.group(4), m.group(6)) if u]
    if not units:  # a trailing unit word right after the match ("… x 0.2 inches" is caught above; "… x 0.2 in." too)
        tail = t[m.end():m.end() + 10].lower().lstrip(" :()-")
        for u in ("inches", "inch", "in", "mm", "cm", '"'):
            if tail.startswith(u):
                units = [u]
                break
    unit = (units[0] if units else "mm").lower()
    k = 25.4 if unit.startswith("in") or unit in ('"', "″", "”") else (10.0 if unit == "cm" else 1.0)
    try:
        vals = [float(n.replace(",", ".")) * k if n else 0.0 for n in nums]
    except ValueError:
        return None
    vals = sorted((round(v, 2) for v in vals), reverse=True)
    if vals[0] <= 0 or vals[1] <= 0:
        return None
    return (vals[0], vals[1], vals[2])


def to_json(c: Component) -> dict:
    return asdict(c)


def from_json(d: dict) -> Component:
    if not isinstance(d, dict) or not d.get("key") or any(d.get(k) is None for k in ("length", "width", "height")):
        raise ValueError("a component needs a key and length/width/height")
    return Component(
        key=str(d["key"]), name=str(d.get("name") or d["key"]), category=str(d.get("category") or "misc"),
        length=float(d["length"]), width=float(d["width"]), height=float(d["height"]),
        holes=tuple(Hole(**h) for h in d.get("holes") or ()),
        ports=tuple(Port(**p) for p in d.get("ports") or ()),
        apertures=tuple(Aperture(**a) for a in d.get("apertures") or ()),
        mount=str(d.get("mount") or "standoff"), clearance=float(d.get("clearance", 0.5)),
        aliases=tuple(d.get("aliases") or ()), search=str(d.get("search") or ""),
        source=str(d.get("source") or "datasheet"), confidence=float(d.get("confidence", 0.9)),
        tags=tuple(d.get("tags") or ()), notes=str(d.get("notes") or ""),
    )


# ----- the catalog -----
CATALOG: dict[str, Component] = {}


def _add(c: Component) -> Component:
    CATALOG[c.key] = c
    return c


# Short hands used below (plan view; see the conventions in the module docstring).
_H = Hole
_P = Port
_A = Aperture

# =====================================================================================================
# MCU boards — Espressif dev-kit user guides, Seeed wiki, Arduino docs / Eagle files, raspberrypi.com
# mechanical drawings, PJRC. Dev boards without mounting holes are clamped (rails) or pocketed.
# =====================================================================================================
_add(Component(
    key="esp32_devkitc", name="ESP32 DevKitC V4 (38-pin)", category="mcu",
    length=55.0, width=28.0, height=13.0, mount="rails", clearance=0.5,
    ports=(_P("micro_usb", "left", 14.0),),
    aliases=("esp32 devkit", "esp32 dev board", "esp32-devkitc", "esp32 wroom devkit", "esp32 devkitc v4",
             "esp32 38 pin", "esp32 devkit 38"),
    search="ESP32 DevKitC V4 development board 38 pin", source="community", confidence=0.65,
    tags=("compute", "wireless"),
    notes="No mounting holes — clamp with rails. Espressif's V4 is ~54.4–55 × 27.9–28; clones differ by a "
          "millimetre. The 30-pin clone is a separate entry (esp32_devkitc_30). Height counts the pins below.",
))
_add(Component(
    key="esp32_devkitc_30", name="ESP32 DevKit V1 (30-pin, DOIT)", category="mcu",
    length=51.5, width=25.4, height=13.0, mount="rails", clearance=0.7,
    ports=(_P("micro_usb", "left", 12.7),),
    aliases=("esp32 30 pin", "esp32 devkit v1", "doit esp32", "esp32 devkit 30", "esp32 30pin"),
    search="ESP32 DevKit V1 30 pin development board", source="community", confidence=0.55,
    tags=("compute", "wireless"),
    notes="Vendors list 48–52 mm long; measure yours before a tight pocket. No holes.",
))
_add(Component(
    key="esp32_s3_devkitc", name="ESP32-S3-DevKitC-1 (44-pin)", category="mcu",
    length=70.0, width=28.0, height=13.0, mount="rails", clearance=0.6,
    ports=(_P("usb_c", "left", 8.0), _P("usb_c", "left", 20.0)),
    aliases=("esp32 s3 devkit", "esp32-s3-devkitc-1", "esp32s3 devkitc", "esp32 s3 dev board",
             "esp32-s3 devkitc"),
    search="ESP32-S3-DevKitC-1 N16R8 development board", source="community", confidence=0.6,
    tags=("compute", "wireless"),
    notes="Two USB-C on one short edge (UART and native USB), about 12 mm apart — offsets approximate. "
          "70 × 28 per espboards.dev; no mounting holes.",
))
_add(Component(
    key="esp32_cam", name="ESP32-CAM (AI-Thinker, OV2640)", category="mcu",
    length=27.0, width=40.5, height=12.0, mount="pocket", clearance=0.6,
    ports=(_P("sd", "back", 13.5, width=13.0, height=3.0), _P("header", "left", 13.5), _P("header", "right", 13.5)),
    apertures=(_A("lens", 13.5, 20.0, d=8.5, face="top"), _A("led", 20.0, 6.0, d=3.0, face="top")),
    aliases=("esp32 cam", "esp32-cam", "ai thinker esp32 cam", "esp32cam", "esp 32 cam", "esp32 camera"),
    search="ESP32-CAM WiFi Bluetooth camera module OV2640", source="datasheet", confidence=0.7,
    tags=("compute", "vision", "wireless"),
    notes="Board 40.5 × 27 × 4.5 (AI-Thinker); height here counts the pin headers underneath and the "
          "folded-over camera on top. Lens position is approximate (camera folds over the middle of the "
          "board) — measure yours. microSD slot faces the antenna end; no mounting holes. No IR night "
          "vision version exists as one board — pair it with an 850 nm OV2640 lens module.",
))
_add(Component(
    key="esp32_cam_mb", name="ESP32-CAM-MB USB programmer base", category="mcu",
    length=27.0, width=40.0, height=24.0, mount="pocket", clearance=0.6,
    ports=(_P("micro_usb", "front", 13.5),),
    aliases=("esp32 cam mb", "esp32-cam-mb", "cam mb base", "esp32 cam programmer"),
    search="ESP32-CAM-MB micro USB programmer base CH340", source="community", confidence=0.55,
    tags=("compute",),
    notes="The base the ESP32-CAM plugs into; 40 × 27 per vendors. Height is the stack with the CAM "
          "on top (approximate).",
))
_add(Component(
    key="xiao_esp32s3", name="Seeed XIAO ESP32-S3", category="mcu",
    length=21.0, width=17.8, height=4.5, mount="pocket", clearance=0.5,
    ports=(_P("usb_c", "left", 8.9), _P("antenna", "right", 8.9)),
    aliases=("xiao esp32 s3", "xiao s3", "seeed xiao esp32s3", "xiao esp32-s3", "xiao esp32s3"),
    search="Seeed Studio XIAO ESP32S3", source="datasheet", confidence=0.9,
    tags=("compute", "wireless"),
    notes="21 × 17.8 mm (Seeed wiki). Castellated, no mounting holes; a rib-walled pocket holds it. "
          "u.FL antenna pigtail leaves the end opposite USB-C.",
))
_add(Component(
    key="xiao_esp32s3_sense", name="Seeed XIAO ESP32-S3 Sense", category="mcu",
    length=21.0, width=17.8, height=15.0, mount="pocket", clearance=0.5,
    ports=(_P("usb_c", "left", 8.9), _P("sd", "right", 8.9, width=12.0, height=2.5)),
    apertures=(_A("lens", 8.0, 8.9, d=8.0, face="top"), _A("mic", 12.5, 3.0, d=1.5, face="top")),
    aliases=("xiao esp32 s3 sense", "xiao s3 sense", "seeed xiao sense", "xiao esp32-s3 sense",
             "xiao sense", "xiao esp32s3 sense", "seeed studio xiao esp32s3 sense"),
    search="Seeed Studio XIAO ESP32S3 Sense camera", source="datasheet", confidence=0.75,
    tags=("compute", "vision", "hearing", "storage", "wireless"),
    notes="21 × 17.8 × 15 mm with the expansion board fitted (Seeed wiki). The camera sits at the USB-C "
          "end (the flex folds back over the board); the FPC connector, the PDM mic and the microSD "
          "are at the antenna end — mic about 8.5 mm from that end per Seeed's expansion-board DXF. "
          "Lens ±2 mm: confirm with the camera ruler before boring.",
))
_add(Component(
    key="xiao_esp32c3", name="Seeed XIAO ESP32-C3", category="mcu",
    length=21.0, width=17.5, height=4.5, mount="pocket", clearance=0.5,
    ports=(_P("usb_c", "left", 8.75), _P("antenna", "right", 8.75)),
    aliases=("xiao esp32 c3", "xiao c3", "seeed xiao esp32c3", "xiao esp32-c3"),
    search="Seeed Studio XIAO ESP32C3", source="datasheet", confidence=0.9,
    tags=("compute", "wireless"),
    notes="21 × 17.5 mm (Seeed). External u.FL antenna required for Wi-Fi/BLE — leave a path for it.",
))
_add(Component(
    key="esp32_c3_supermini", name="ESP32-C3 SuperMini", category="mcu",
    length=22.5, width=18.0, height=5.0, mount="pocket", clearance=0.6,
    ports=(_P("usb_c", "left", 9.0),),
    aliases=("esp32 c3 supermini", "c3 supermini", "esp32c3 super mini", "esp32-c3 super mini"),
    search="ESP32-C3 SuperMini development board", source="community", confidence=0.55,
    tags=("compute", "wireless"),
    notes="Clone sizes vary (22–23 × 18); ceramic antenna at the end opposite USB-C — keep metal off it.",
))
_add(Component(
    key="esp8266_nodemcu", name="NodeMCU ESP8266 (Amica, CP2102)", category="mcu",
    length=48.6, width=25.9, height=13.0, mount="rails", clearance=0.6,
    ports=(_P("micro_usb", "left", 12.95),),
    aliases=("nodemcu", "node mcu", "esp8266 nodemcu", "nodemcu amica", "esp8266 dev board", "esp8266"),
    search="NodeMCU ESP8266 CP2102 Amica development board", source="community", confidence=0.65,
    tags=("compute", "wireless"),
    notes="Amica (narrow) version, 48 × 26 per vendors; it has corner holes but their positions are not "
          "verified here — rails. The wide LoLin V3 is a separate entry.",
))
_add(Component(
    key="nodemcu_lolin_v3", name="NodeMCU ESP8266 LoLin V3 (wide, CH340)", category="mcu",
    length=58.0, width=31.0, height=13.0, mount="rails", clearance=0.6,
    ports=(_P("micro_usb", "left", 15.5),),
    aliases=("lolin v3", "nodemcu v3", "nodemcu lolin", "esp8266 lolin v3", "nodemcu ch340"),
    search="NodeMCU ESP8266 LoLin V3 CH340 development board", source="community", confidence=0.65,
    tags=("compute", "wireless"),
    notes="58 × 31 × 13 per vendors. Too wide for a half breadboard; no verified holes — rails.",
))
_add(Component(
    key="wemos_d1_mini", name="Wemos / LOLIN D1 mini (ESP8266)", category="mcu",
    length=34.2, width=25.6, height=8.0, mount="rails", clearance=0.5,
    ports=(_P("micro_usb", "left", 12.8),),
    aliases=("d1 mini", "wemos d1", "lolin d1 mini", "wemos d1 mini", "d1mini"),
    search="Wemos D1 mini ESP8266 development board", source="datasheet", confidence=0.85,
    tags=("compute", "wireless"),
    notes="34.2 × 25.6 (WEMOS docs). Micro-USB on a short edge; no mounting holes. Height with headers "
          "fitted is ~13.",
))
_add(Component(
    key="arduino_uno", name="Arduino Uno R3", category="mcu",
    length=68.58, width=53.34, height=15.0,
    holes=(_H(13.97, 2.54, 3.2), _H(66.04, 7.62, 3.2), _H(66.04, 35.56, 3.2), _H(15.24, 50.8, 3.2)),
    ports=(_P("other", "left", 15.3, width=13.0, height=12.0), _P("barrel_5_5", "left", 45.7)),
    mount="standoff", clearance=0.5,
    aliases=("uno", "arduino uno", "uno r3", "arduino uno r3", "arduino"),
    search="Arduino Uno R3 board", source="datasheet", confidence=0.95,
    tags=("compute",),
    notes="Board 2.7 × 2.1 in; holes from the Arduino Eagle file (0.55/0.1, 2.6/0.3, 2.6/1.4, 0.6/2.0 in). "
          "USB-B and the barrel jack overhang the left edge by ~6 mm; their offsets are ±1 mm.",
))
_add(Component(
    key="arduino_uno_r4", name="Arduino Uno R4 (Minima / WiFi)", category="mcu",
    length=68.58, width=53.34, height=15.0,
    holes=(_H(13.97, 2.54, 3.2), _H(66.04, 7.62, 3.2), _H(66.04, 35.56, 3.2), _H(15.24, 50.8, 3.2)),
    ports=(_P("usb_c", "left", 15.3), _P("barrel_5_5", "left", 45.7)),
    mount="standoff", clearance=0.5,
    aliases=("uno r4", "arduino uno r4", "uno r4 wifi", "uno r4 minima", "arduino r4"),
    search="Arduino Uno R4 WiFi board", source="datasheet", confidence=0.9,
    tags=("compute", "wireless"),
    notes="Same outline and hole pattern as the Uno R3 (Arduino keeps the form factor); USB-C replaces USB-B.",
))
_add(Component(
    key="arduino_mega", name="Arduino Mega 2560 R3", category="mcu",
    length=101.6, width=53.34, height=15.0,
    holes=(_H(13.97, 2.54, 3.2), _H(66.04, 7.62, 3.2), _H(66.04, 35.56, 3.2), _H(15.24, 50.8, 3.2),
           _H(96.52, 2.54, 3.2), _H(90.17, 50.8, 3.2)),
    ports=(_P("other", "left", 15.3, width=13.0, height=12.0), _P("barrel_5_5", "left", 45.7)),
    mount="standoff", clearance=0.5,
    aliases=("mega", "arduino mega", "mega 2560", "arduino mega 2560"),
    search="Arduino Mega 2560 R3 board", source="datasheet", confidence=0.95,
    tags=("compute",),
    notes="4 × 2.1 in; the Uno's four holes plus two more at the far end (3.8/0.1 and 3.55/2.0 in).",
))
_add(Component(
    key="arduino_nano", name="Arduino Nano (every / classic)", category="mcu",
    length=43.18, width=17.78, height=8.0,
    holes=(_H(1.27, 1.27, 1.8), _H(41.91, 1.27, 1.8), _H(1.27, 16.51, 1.8), _H(41.91, 16.51, 1.8)),
    ports=(_P("other", "left", 8.9, width=9.0, height=5.0),),
    mount="standoff", clearance=0.5,
    aliases=("nano", "arduino nano", "nano every", "arduino nano every"),
    search="Arduino Nano board ATmega328P", source="datasheet", confidence=0.85,
    tags=("compute",),
    notes="Board 1.7 × 0.7 in (Arduino quotes 45 × 18 with the USB overhang). Corner holes are 1.8 mm — "
          "M1.6 or a pin, not M2. Mini-USB (classic) / micro-USB (Every) on a short edge.",
))
_add(Component(
    key="arduino_nano_esp32", name="Arduino Nano ESP32", category="mcu",
    length=45.0, width=18.0, height=8.0, mount="pocket", clearance=0.5,
    ports=(_P("usb_c", "left", 9.0),),
    aliases=("nano esp32", "arduino nano esp32"),
    search="Arduino Nano ESP32 board", source="datasheet", confidence=0.75,
    tags=("compute", "wireless"),
    notes="Nano form factor (Arduino: 45 × 18); holes not verified — pocket. USB-C on a short edge.",
))
_add(Component(
    key="arduino_pro_mini", name="Arduino Pro Mini", category="mcu",
    length=33.0, width=17.8, height=4.0, mount="pocket", clearance=0.5,
    aliases=("pro mini", "arduino pro mini"),
    search="Arduino Pro Mini 328 5V 16MHz", source="datasheet", confidence=0.85,
    tags=("compute",),
    notes="1.3 × 0.7 in, no USB (FTDI header on a short edge), no holes. Height with headers ~11.",
))
_add(Component(
    key="pi_4", name="Raspberry Pi 4 Model B", category="mcu",
    length=85.0, width=56.0, height=20.0,
    holes=(_H(3.5, 3.5, 2.7), _H(61.5, 3.5, 2.7), _H(3.5, 52.5, 2.7), _H(61.5, 52.5, 2.7)),
    ports=(_P("usb_c", "front", 11.2), _P("hdmi", "front", 26.0, width=8.0, height=4.0),
           _P("hdmi", "front", 39.5, width=8.0, height=4.0), _P("audio_3_5", "front", 54.0),
           _P("usb_a", "right", 9.0, width=15.0, height=16.0), _P("usb_a", "right", 27.0, width=15.0, height=16.0),
           _P("other", "right", 45.75, width=16.5, height=14.0), _P("sd", "left", 28.0)),
    mount="standoff", clearance=0.8,
    aliases=("pi 4", "raspberry pi 4", "rpi4", "pi4", "raspberry pi 4b", "pi 4b"),
    search="Raspberry Pi 4 Model B 4GB", source="datasheet", confidence=0.9,
    tags=("compute", "wireless", "storage"),
    notes="85 × 56, M2.5 holes 58 × 49 at 3.5 from the edges (official drawing). Right edge: USB 2.0 at 9, "
          "USB 3.0 at 27, Ethernet at 45.75 (±0.5); front: USB-C 11.2, micro-HDMI 26 / 39.5, audio 54. "
          "Height counts the USB stacks; add airflow.",
))
_add(Component(
    key="pi_5", name="Raspberry Pi 5", category="mcu",
    length=85.0, width=56.0, height=20.0,
    holes=(_H(3.5, 3.5, 2.7), _H(61.5, 3.5, 2.7), _H(3.5, 52.5, 2.7), _H(61.5, 52.5, 2.7)),
    ports=(_P("usb_c", "front", 11.2), _P("hdmi", "front", 26.0, width=8.0, height=4.0),
           _P("hdmi", "front", 39.5, width=8.0, height=4.0),
           _P("other", "right", 10.25, width=16.5, height=14.0), _P("usb_a", "right", 27.0, width=15.0, height=16.0),
           _P("usb_a", "right", 45.0, width=15.0, height=16.0), _P("sd", "left", 28.0)),
    mount="standoff", clearance=0.8,
    aliases=("pi 5", "raspberry pi 5", "rpi5", "pi5"),
    search="Raspberry Pi 5 8GB", source="datasheet", confidence=0.85,
    tags=("compute", "wireless", "storage"),
    notes="Same outline and hole pattern as the Pi 4. Ethernet moved back to the bottom-right (10.25) with "
          "the USB stacks above it (±1 mm); no audio jack; power button on the left edge. Runs hot — vents.",
))
_add(Component(
    key="pi_zero_2w", name="Raspberry Pi Zero 2 W", category="mcu",
    length=65.0, width=30.0, height=5.0,
    holes=(_H(3.5, 3.5, 2.75), _H(61.5, 3.5, 2.75), _H(3.5, 26.5, 2.75), _H(61.5, 26.5, 2.75)),
    ports=(_P("hdmi", "front", 12.4, width=12.0, height=4.5), _P("micro_usb", "front", 41.4),
           _P("micro_usb", "front", 54.0), _P("sd", "left", 15.0), _P("other", "right", 15.0, width=17.0, height=1.5)),
    mount="standoff", clearance=0.5,
    aliases=("pi zero", "pi zero 2", "pi zero 2 w", "raspberry pi zero 2 w", "pi zero w", "rpi zero",
             "raspberry pi zero"),
    search="Raspberry Pi Zero 2 W", source="datasheet", confidence=0.9,
    tags=("compute", "wireless", "storage"),
    notes="65 × 30, M2.5 holes 58 × 23 at 3.5 from the edges (official drawing). Front edge: mini-HDMI "
          "12.4, USB data 41.4, USB power 54. Camera ribbon leaves the right edge. Height is bare; with a "
          "GPIO header fitted use 13.",
))
_add(Component(
    key="pi_pico", name="Raspberry Pi Pico", category="mcu",
    length=51.0, width=21.0, height=5.0,
    holes=(_H(2.0, 4.8, 2.1), _H(49.0, 4.8, 2.1), _H(2.0, 16.2, 2.1), _H(49.0, 16.2, 2.1)),
    ports=(_P("micro_usb", "left", 10.5),),
    mount="standoff", clearance=0.5,
    aliases=("pico", "pi pico", "raspberry pi pico", "rp2040 pico", "pico 2"),
    search="Raspberry Pi Pico RP2040", source="datasheet", confidence=0.95,
    tags=("compute",),
    notes="51 × 21, four 2.1 mm holes on a 47 × 11.4 pattern (datasheet). Height is bare (~4 with the USB); "
          "with pin headers down use 13. Pico 2 keeps the outline.",
))
_add(Component(
    key="pi_pico_w", name="Raspberry Pi Pico W", category="mcu",
    length=51.0, width=21.0, height=5.0,
    holes=(_H(2.0, 4.8, 2.1), _H(49.0, 4.8, 2.1), _H(2.0, 16.2, 2.1), _H(49.0, 16.2, 2.1)),
    ports=(_P("micro_usb", "left", 10.5),),
    mount="standoff", clearance=0.5,
    aliases=("pico w", "pi pico w", "raspberry pi pico w", "pico wireless", "pico 2 w"),
    search="Raspberry Pi Pico W wireless", source="datasheet", confidence=0.95,
    tags=("compute", "wireless"),
    notes="Same outline and holes as the Pico; the antenna is at the end opposite USB — keep metal off it.",
))
_add(Component(
    key="pi_camera_v2", name="Raspberry Pi Camera Module 2", category="camera",
    length=25.0, width=23.86, height=9.0,
    holes=(_H(2.0, 2.0, 2.2), _H(23.0, 2.0, 2.2), _H(2.0, 14.5, 2.2), _H(23.0, 14.5, 2.2)),
    ports=(_P("other", "back", 12.5, width=17.0, height=1.5),),
    apertures=(_A("lens", 13.8, 14.4, d=8.5, face="top"),),
    mount="standoff", clearance=0.5,
    aliases=("pi camera", "pi camera v2", "raspberry pi camera module 2", "picam v2", "camera module v2",
             "pi cam v2", "raspberry pi camera v2"),
    search="Raspberry Pi Camera Module V2 8MP", source="datasheet", confidence=0.95,
    tags=("vision",),
    notes="Official drawing: 25 × 23.862, four 2.2 mm holes on 21 × 12.5, lens centred left-right in line "
          "with the upper hole row (12.5, 14.5); the 8.5 mm sensor block stands ~4 mm proud. Ribbon leaves "
          "the front edge.",
))
_add(Component(
    key="pi_camera_v3", name="Raspberry Pi Camera Module 3", category="camera",
    length=25.0, width=23.86, height=11.5,
    holes=(_H(2.0, 2.0, 2.2), _H(23.0, 2.0, 2.2), _H(2.0, 14.5, 2.2), _H(23.0, 14.5, 2.2)),
    ports=(_P("other", "front", 12.5, width=17.0, height=1.5),),
    apertures=(_A("lens", 12.5, 14.4, d=10.8, face="top"),),
    mount="standoff", clearance=0.5,
    aliases=("pi camera v3", "camera module 3", "raspberry pi camera module 3", "picam v3", "pi cam v3",
             "camera module 3 wide", "camera module 3 noir"),
    search="Raspberry Pi Camera Module 3 12MP autofocus", source="datasheet", confidence=0.95,
    tags=("vision",),
    notes="Official drawing: same outline and 21 × 12.5 hole pattern as v2; the 10.8 mm lens housing is "
          "centred at (12.5, 14.4). Standard 11.5 tall, Wide 12.4. NoIR variants are the same body.",
))
_add(Component(
    key="teensy_40", name="PJRC Teensy 4.0", category="mcu",
    length=35.6, width=17.8, height=4.6, mount="pocket", clearance=0.5,
    ports=(_P("micro_usb", "left", 8.9),),
    aliases=("teensy", "teensy 4", "teensy 4.0", "teensy40"),
    search="PJRC Teensy 4.0 development board", source="datasheet", confidence=0.9,
    tags=("compute",),
    notes="1.4 × 0.7 in (PJRC); no mounting holes. Height with headers ~11.",
))

# =====================================================================================================
# Cameras and lenses (bare modules) — Arducam/AI-Thinker listings; positions community.
# =====================================================================================================
_add(Component(
    key="ov2640_24pin", name="OV2640 camera module, 24-pin FPC (ESP32-CAM lens block)", category="camera",
    length=8.5, width=8.5, height=6.5, mount="pocket", clearance=0.3,
    apertures=(_A("lens", 4.25, 4.25, d=8.0, face="top"),),
    aliases=("ov2640", "ov2640 camera", "ov2640 24 pin", "esp32 cam lens", "ov2640 lens"),
    search="OV2640 camera module 24 pin FPC ESP32-CAM", source="community", confidence=0.55,
    tags=("vision",),
    notes="The lens block only (the 8.5 mm square that pokes through a lid); the FPC tail is 24-pin 0.5 mm. "
          "Fisheye/wide variants stand taller (up to ~10). A lens bore of 9 mm clears it.",
))
_add(Component(
    key="ov2640_night_vision", name="OV2640 850 nm night-vision lens module (no IR filter)", category="camera",
    length=8.5, width=8.5, height=7.0, mount="pocket", clearance=0.3,
    apertures=(_A("lens", 4.25, 4.25, d=8.0, face="top"),),
    aliases=("night vision camera", "ir camera module", "ov2640 night vision", "ov2640 ir", "850nm camera"),
    search="OV2640 night vision camera module 850nm no IR filter ESP32-CAM", source="community",
    confidence=0.5, tags=("vision",),
    notes="Night vision for an ESP32-CAM is this lens swap plus separate 850 nm IR LEDs — there is no "
          "single ESP32-CAM with built-in night vision. Sees IR; daytime colours shift.",
))

# =====================================================================================================
# Microphones — generic INMP441 boards (community), electret capsules (standard size).
# =====================================================================================================
_add(Component(
    key="inmp441", name="INMP441 I2S MEMS microphone breakout (square)", category="mic",
    length=14.0, width=14.0, height=3.0, mount="pocket", clearance=0.5,
    apertures=(_A("mic", 7.0, 7.0, d=1.5, face="bottom"),),
    aliases=("inmp 441", "i2s mic", "i2s microphone", "inmp441 mic", "inmp441 module"),
    search="INMP441 I2S microphone module", source="community", confidence=0.6,
    tags=("hearing",),
    notes="~14 × 14 mm board (vendors). The INMP441 is a BOTTOM-port mic: the sound hole is in the PCB on "
          "the side opposite the chip — that side needs the 1.5 mm hole to the outside. Pins along one edge.",
))
_add(Component(
    key="inmp441_round", name="INMP441 I2S MEMS microphone breakout (round)", category="mic",
    length=14.0, width=14.0, height=3.0, mount="pocket", clearance=0.5,
    apertures=(_A("mic", 7.0, 7.0, d=1.5, face="bottom"),),
    aliases=("inmp441 round", "round i2s mic", "inmp441 circle"),
    search="INMP441 I2S microphone module round", source="community", confidence=0.6,
    tags=("hearing",),
    notes="Ø14 mm variant of the same board; bottom-port like the square one.",
))
_add(Component(
    key="electret_mic_9_7", name="Electret microphone capsule Ø9.7 mm", category="mic",
    length=9.7, width=9.7, height=6.7, mount="pocket", clearance=0.3,
    apertures=(_A("mic", 4.85, 4.85, d=2.0, face="top"),),
    aliases=("electret mic", "electret microphone", "9.7mm mic", "condenser mic capsule"),
    search="electret microphone capsule 9.7mm", source="datasheet", confidence=0.85,
    tags=("hearing",),
    notes="Standard 9.7 × 6.7 capsule (also 6 × 2.7 and 9.7 × 4.5 exist). Needs a preamp (MAX9814 etc.).",
))

# =====================================================================================================
# Amplifiers — Adafruit product page (datasheet), generic clones (community).
# =====================================================================================================
_add(Component(
    key="max98357a", name="MAX98357A I2S amplifier breakout (generic 4-pack)", category="amp",
    length=19.4, width=17.8, height=6.0, mount="pocket", clearance=0.5,
    aliases=("max98357", "max 98357", "max 98357a", "i2s amp", "i2s amplifier", "max98357a module"),
    search="MAX98357A I2S 3W Class D amplifier breakout", source="community", confidence=0.6,
    tags=("speaking",),
    notes="Clones copy Adafruit's 0.8 × 0.7 in outline; some run 20 × 18. Screw terminal on a short edge "
          "adds ~6 mm height; pins along the other. Verify yours.",
))
_add(Component(
    key="max98357a_adafruit", name="Adafruit MAX98357A I2S amplifier breakout", category="amp",
    length=19.4, width=17.8, height=3.0, mount="pocket", clearance=0.5,
    aliases=("adafruit max98357a", "adafruit i2s amp", "max98357a adafruit"),
    search="Adafruit MAX98357A I2S Class D amplifier", source="datasheet", confidence=0.9,
    tags=("speaking",),
    notes="19.4 × 17.8 × 3.0 (product page, header not fitted). Two mounting holes exist but their "
          "positions aren't verified here — pocket. Screw terminal on a short edge.",
))
_add(Component(
    key="pam8403", name="PAM8403 2×3 W class-D amplifier board", category="amp",
    length=21.0, width=18.0, height=3.0, mount="pocket", clearance=0.5,
    aliases=("pam 8403", "pam8403 amp", "mini amplifier board", "pam8403 module"),
    search="PAM8403 mini 5V digital amplifier board", source="community", confidence=0.6,
    tags=("speaking",),
    notes="21 × 18 × 3 without the volume pot; the pot version is ~26 × 20 × 15. Analog in — not I2S.",
))

# =====================================================================================================
# Speakers — Adafruit pages for the metal 28 mm, vendor listings for the rest (community).
# =====================================================================================================
_add(Component(
    key="speaker_20mm", name="Speaker Ø20 mm 8 Ω 0.5 W", category="speaker",
    length=20.0, width=20.0, height=5.0, mount="pocket", clearance=0.4,
    apertures=(_A("speaker", 10.0, 10.0, d=18.0, face="top"),),
    aliases=("20mm speaker", "mini speaker 20", "20 mm speaker"),
    search="20mm 8 ohm 0.5W mini speaker", source="community", confidence=0.65, tags=("speaking",),
    notes="Thin cone types run 3–5 mm; ring pocket plus a grille.",
))
_add(Component(
    key="speaker_28mm", name="Speaker Ø28 mm 8 Ω 2 W", category="speaker",
    length=28.0, width=28.0, height=5.0, mount="pocket", clearance=0.4,
    apertures=(_A("speaker", 14.0, 14.0, d=26.0, face="top"),),
    aliases=("28mm speaker", "mini speaker 28", "28 mm speaker"),
    search="28mm 8 ohm 2W mini speaker", source="community", confidence=0.7, tags=("speaking",),
    notes="Adafruit's metal 28 mm is 4.5 tall; generic 2 W ones 5–6. Round cone; grille over the face.",
))
_add(Component(
    key="speaker_36mm", name="Speaker Ø36 mm 8 Ω 2 W", category="speaker",
    length=36.0, width=36.0, height=5.0, mount="pocket", clearance=0.4,
    apertures=(_A("speaker", 18.0, 18.0, d=33.0, face="top"),),
    aliases=("36mm speaker", "36 mm speaker"),
    search="36mm 8 ohm 2W speaker", source="community", confidence=0.65, tags=("speaking",),
    notes="Vendors quote 3.5–4.8 mm thick; the magnet may add to 6.",
))
_add(Component(
    key="speaker_40mm", name="Speaker Ø40 mm 4 Ω 3 W", category="speaker",
    length=40.0, width=40.0, height=6.0, mount="pocket", clearance=0.4,
    apertures=(_A("speaker", 20.0, 20.0, d=37.0, face="top"),),
    aliases=("40mm speaker", "40 mm speaker", "3w speaker"),
    search="40mm 4 ohm 3W full range speaker", source="community", confidence=0.5, tags=("speaking",),
    notes="Height varies a lot: thin ones ~6 mm, Adafruit's 3968 is 20 mm deep — measure yours.",
))
_add(Component(
    key="speaker_20x30", name="Speaker 20 × 30 mm oval/rect 8 Ω 1 W", category="speaker",
    length=30.0, width=20.0, height=5.0, mount="pocket", clearance=0.4,
    apertures=(_A("speaker", 15.0, 10.0, w=27.0, h=17.0, face="top"),),
    aliases=("2030 speaker", "20x30 speaker", "oval speaker 20x30", "30x20 speaker"),
    search="2030 speaker 8 ohm 1W 20x30mm", source="community", confidence=0.6, tags=("speaking",),
    notes="Rectangular 'phone' speaker; ~5 mm deep.",
))
_add(Component(
    key="speaker_30x40", name="Speaker 30 × 40 mm rect 4 Ω 3 W", category="speaker",
    length=40.0, width=30.0, height=5.0, mount="pocket", clearance=0.4,
    apertures=(_A("speaker", 20.0, 15.0, w=37.0, h=27.0, face="top"),),
    aliases=("3040 speaker", "30x40 speaker", "40x30 speaker", "rectangular speaker 3040"),
    search="3040 speaker 4 ohm 3W 30x40mm", source="community", confidence=0.6, tags=("speaking",),
    notes="~5 mm deep; magnet bump in the middle of the back.",
))

# =====================================================================================================
# Batteries and holders — IEC cell sizes (datasheet), Adafruit pouch cell (page), holders (community).
# =====================================================================================================
_add(Component(
    key="lipo_500mah_adafruit", name="LiPo 3.7 V 500 mAh (Adafruit 1578)", category="battery",
    length=36.0, width=29.0, height=4.75, mount="pocket", clearance=1.0,
    ports=(_P("jst_ph", "front", 14.5),),
    aliases=("adafruit 500mah lipo", "500mah lipo", "lipo 500"),
    search="Adafruit lithium ion polymer battery 3.7V 500mAh", source="datasheet", confidence=0.9,
    tags=("power",),
    notes="29 × 36 × 4.75 with a JST-PH lead (product page). Leave a lead bay; never pinch the pouch.",
))
_add(Component(
    key="cell_18650", name="18650 Li-ion cell", category="battery",
    length=65.2, width=18.4, height=18.4, mount="pocket", clearance=0.6,
    aliases=("18650", "18650 cell", "18650 battery", "li-ion 18650"),
    search="18650 lithium ion battery button top 3000mAh", source="datasheet", confidence=0.95,
    tags=("power",),
    notes="Ø18.4 × 65.2 unprotected; protected cells run to 69–70 long. Use a holder or a proper pack.",
))
_add(Component(
    key="holder_18650_1", name="18650 holder, 1 cell (wire leads)", category="battery",
    length=77.0, width=20.8, height=15.0, mount="pocket", clearance=0.6,
    aliases=("18650 holder", "single 18650 holder", "1x18650 holder", "18650 case"),
    search="18650 battery holder single cell wire leads", source="community", confidence=0.6,
    tags=("power",),
    notes="~77 × 21 × 15 plastic holder; leads leave one end. Vendors vary by 1–2 mm.",
))
_add(Component(
    key="holder_18650_2", name="18650 holder, 2 cells", category="battery",
    length=77.0, width=40.0, height=15.0, mount="pocket", clearance=0.6,
    aliases=("2x18650 holder", "dual 18650 holder", "two 18650 holder", "18650 holder 2"),
    search="18650 battery holder 2 cell", source="community", confidence=0.65,
    tags=("power",),
    notes="77 × 40 × 15 (Canaduino SMD type; some run 86 long with the solder tabs).",
))
_add(Component(
    key="cell_cr2032", name="CR2032 coin cell", category="battery",
    length=20.0, width=20.0, height=3.2, mount="pocket", clearance=0.3,
    aliases=("cr2032", "coin cell", "cr2032 battery", "2032"),
    search="CR2032 3V lithium coin cell", source="datasheet", confidence=0.95, tags=("power",),
    notes="Ø20 × 3.2 (IEC). Pair with a holder for a replaceable cell.",
))
_add(Component(
    key="holder_cr2032", name="CR2032 holder (through-hole, 20 mm)", category="battery",
    length=25.0, width=22.0, height=5.5, mount="pocket", clearance=0.5,
    aliases=("cr2032 holder", "coin cell holder", "2032 holder", "cr2032 battery holder"),
    search="CR2032 coin cell battery holder PCB mount", source="community", confidence=0.5,
    tags=("power",),
    notes="Sizes vary widely (MPD BS-7 is 9 mm tall); this is the common flat clip type — measure yours.",
))
_add(Component(
    key="cell_aa", name="AA cell", category="battery",
    length=50.5, width=14.5, height=14.5, mount="pocket", clearance=0.5,
    aliases=("aa", "aa battery", "aa cell", "double a"),
    search="AA batteries", source="datasheet", confidence=0.95, tags=("power",),
    notes="Ø14.5 × 50.5 (IEC LR6). A neat calibration object for the camera ruler too.",
))
_add(Component(
    key="holder_aa_2", name="AA holder, 2 cells (side by side)", category="battery",
    length=58.0, width=32.0, height=16.0, mount="pocket", clearance=0.6,
    aliases=("2xaa holder", "aa battery holder", "2 aa holder", "double aa holder"),
    search="2 x AA battery holder with leads", source="community", confidence=0.5,
    tags=("power",),
    notes="Typical ~58 × 32 × 16; vendors vary. Leads leave one end.",
))

# =====================================================================================================
# Chargers and power — TP4056 / MT3608 / LM2596 modules (community), Adafruit USB-C breakout (page),
# DC-005 barrel jack (standard).
# =====================================================================================================
_add(Component(
    key="tp4056_usb_c", name="TP4056 USB-C LiPo charger with protection", category="charger",
    length=28.0, width=17.5, height=5.0, mount="pocket", clearance=0.5,
    ports=(_P("usb_c", "left", 8.75),),
    aliases=("tp4056", "tp4056 usb c", "tp 4056", "lipo charger", "tp4056 type c", "usb c lipo charger"),
    search="TP4056 USB-C lithium battery charger module protection", source="community",
    confidence=0.6, tags=("charging", "power"),
    notes="Vendors quote 26–29 × 17–17.5 × 4.3–5. USB-C leaves a short edge; B+/B−/OUT pads on the other. "
          "Charges at 1 A by default — fine for cells ≥ 1000 mAh, swap R3 for smaller cells.",
))
_add(Component(
    key="tp4056_micro_usb", name="TP4056 micro-USB LiPo charger with protection", category="charger",
    length=26.0, width=17.0, height=4.5, mount="pocket", clearance=0.5,
    ports=(_P("micro_usb", "left", 8.5),),
    aliases=("tp4056 micro usb", "tp4056 microusb", "micro usb lipo charger"),
    search="TP4056 micro USB lithium battery charger module protection", source="community",
    confidence=0.65, tags=("charging", "power"),
    notes="The classic 26 × 17 board; micro-USB on a short edge.",
))
_add(Component(
    key="mt3608", name="MT3608 boost converter (2–24 V in, up to 28 V out)", category="power",
    length=36.0, width=17.0, height=14.0, mount="pocket", clearance=0.5,
    aliases=("mt 3608", "boost converter", "step up module", "mt3608 module"),
    search="MT3608 DC-DC step up boost converter module 2A", source="community", confidence=0.7,
    tags=("power",),
    notes="36 × 17 × 14 with the trimmer standing tall; the trimmer needs top access if you'll adjust it.",
))
_add(Component(
    key="buck_lm2596", name="LM2596 buck converter module", category="power",
    length=43.2, width=21.3, height=14.0, mount="pocket", clearance=0.5,
    aliases=("lm2596", "lm 2596", "buck converter", "step down module", "lm2596 module"),
    search="LM2596 DC-DC buck converter step down module", source="community", confidence=0.65,
    tags=("power",),
    notes="43 × 21 × 14; two diagonal 3 mm holes exist but vendors place them differently — pocket. "
          "The trimmer wants top access.",
))
_add(Component(
    key="usb_c_breakout", name="Adafruit USB-C breakout (downstream)", category="connector",
    length=20.4, width=14.2, height=5.0, mount="pocket", clearance=0.4,
    ports=(_P("usb_c", "left", 7.1),),
    aliases=("usb c breakout", "usb-c breakout", "type c breakout", "usb c port breakout"),
    search="USB Type C breakout board female", source="datasheet", confidence=0.85,
    tags=("power", "charging"),
    notes="20.4 × 14.2 × 5 (product page). Generic 6-pin breakouts are ~ 20 × 10.",
))
_add(Component(
    key="barrel_jack_5521", name="DC barrel jack 5.5 × 2.1 mm (DC-005, PCB)", category="connector",
    length=14.4, width=9.0, height=11.0, mount="pocket", clearance=0.3,
    ports=(_P("barrel_5_5", "left", 4.5),),
    aliases=("barrel jack", "dc jack", "5.5x2.1 jack", "dc-005", "power jack", "barrel connector"),
    search="DC-005 5.5x2.1mm DC power jack PCB mount", source="datasheet", confidence=0.8,
    tags=("power",),
    notes="Body 14.4 × 9 × 11, board-mounted (no bushing): the plug passes through an 11 × 11 mm wall "
          "window (the generator's barrel_5_5 opening). Panel-mount jacks (Ø8 thread, 12 mm nut) are a "
          "different part — mount='clip' — and get a Ø8.2 bushing hole instead.",
))
_add(Component(
    key="screw_terminal_2", name="Screw terminal 2-pin 5.08 mm (KF301)", category="connector",
    length=10.2, width=7.6, height=10.0, mount="pocket", clearance=0.3,
    aliases=("screw terminal", "kf301", "terminal block 2 pin", "2 pin screw terminal"),
    search="KF301 5.08mm 2 pin screw terminal block", source="community", confidence=0.65,
    tags=("power",),
    notes="Wires enter from the side; screws from the top — leave a screwdriver path.",
))

# =====================================================================================================
# Switches, buttons, knobs — C&K/standard drawings (SS12D00, tact, KCD1), Adafruit buzzer page,
# generic encoder/pot (community).
# =====================================================================================================
_add(Component(
    key="switch_ss12d00", name="Slide switch SS12D00 (SPDT, 3-pin)", category="switch",
    length=8.6, width=3.6, height=8.5, mount="pocket", clearance=0.2,
    apertures=(_A("button", 4.3, 1.8, w=8.5, h=3.6, face="top"),),
    aliases=("ss12d00", "slide switch", "ss-12d00", "mini slide switch", "ss12d00g4"),
    search="SS12D00 slide switch 3 pin SPDT", source="datasheet", confidence=0.85,
    tags=("input", "power"),
    notes="Body 8.6 × 3.6 × 4.4 with a 4 mm lever (G4 handle) on top; pins add 3.5 below. The wall slot is "
          "8.5 × 3.6 (helix_parts switch_slot('ss12d00')).",
))
_add(Component(
    key="switch_kcd1", name="Rocker switch KCD1 (21 × 15 mm)", category="switch",
    length=21.0, width=15.0, height=25.0, mount="clip", clearance=0.2,
    apertures=(_A("button", 10.5, 7.5, w=19.2, h=13.2, face="top"),),
    aliases=("kcd1", "rocker switch", "kcd1-101", "21x15 rocker", "mini rocker switch"),
    search="KCD1 rocker switch 21x15mm ON OFF", source="datasheet", confidence=0.85,
    tags=("input", "power"),
    notes="Face 21 × 15; snaps into a 19.2 × 13.2 panel cutout in a 1–3 mm wall; 25 deep with terminals.",
))
_add(Component(
    key="tactile_6x6", name="Tactile switch 6 × 6 mm", category="button",
    length=6.0, width=6.0, height=5.0, mount="pocket", clearance=0.2,
    apertures=(_A("button", 3.0, 3.0, d=3.5, face="top"),),
    aliases=("tact switch", "6x6 tactile", "tactile button", "6mm tactile switch", "6x6x5"),
    search="6x6x5mm tactile push button switch", source="datasheet", confidence=0.9,
    tags=("input",),
    notes="6 × 6 × 5 (4.3 body + plunger); taller plungers exist (7, 9, 13 mm). Pin pitch 4.5 × 6.5.",
))
_add(Component(
    key="tactile_12x12", name="Tactile switch 12 × 12 × 7.3 mm", category="button",
    length=12.0, width=12.0, height=7.3, mount="pocket", clearance=0.2,
    apertures=(_A("button", 6.0, 6.0, d=4.0, face="top"),),
    aliases=("12x12 tactile", "12mm tactile switch", "12x12x7.3", "big tactile switch"),
    search="12x12x7.3mm tactile push button switch with caps", source="datasheet", confidence=0.9,
    tags=("input",),
    notes="Square 3.8 mm plunger; the snap-on caps are Ø12–13 — bore 13.5 if you use one.",
))
_add(Component(
    key="push_button_12mm", name="Latching push button, 12 mm panel mount (metal)", category="button",
    length=14.3, width=14.3, height=29.4, mount="clip", clearance=0.2,
    apertures=(_A("button", 7.15, 7.15, d=12.4, face="top"),),
    aliases=("12mm push button", "latching push button", "12mm latching switch", "metal push button 12mm",
             "12 mm button"),
    search="12mm latching push button switch metal waterproof", source="datasheet", confidence=0.8,
    tags=("input", "power"),
    notes="Threads through a Ø12.2 hole (helix_parts switch_slot('push_12') = 12.4) with a nut behind; "
          "bezel Ø14.3, ~29 long with terminals. LED-ring versions need 5 pins.",
))
_add(Component(
    key="encoder_ky040", name="Rotary encoder module KY-040", category="button",
    length=30.0, width=18.0, height=30.0, mount="pocket", clearance=0.5,
    apertures=(_A("shaft", 12.0, 9.0, d=7.2, face="top"),),
    aliases=("ky-040", "ky040", "rotary encoder", "encoder module", "ky 040"),
    search="KY-040 rotary encoder module with knob", source="community", confidence=0.5,
    tags=("input",),
    notes="Board sizes vary by vendor (26–30 × 18); the encoder's Ø7 threaded bushing takes a 7.2 mm "
          "hole (switch_slot('ky040')). Shaft position approximate — measure yours.",
))
_add(Component(
    key="pot_10k", name="Potentiometer 10 kΩ WH148 (Ø16, 15 mm shaft)", category="button",
    length=17.0, width=17.0, height=35.0, mount="clip", clearance=0.3,
    apertures=(_A("shaft", 8.5, 8.5, d=7.2, face="top"),),
    aliases=("10k pot", "potentiometer", "wh148", "10k potentiometer", "volume pot"),
    search="WH148 10K potentiometer 15mm shaft knob", source="community", confidence=0.7,
    tags=("input",),
    notes="Body Ø16.4 × ~20 with a 15 mm Ø6 D-shaft on an M7 bushing (7.2 mm hole, nut on the outside).",
))
_add(Component(
    key="microswitch_kw11", name="Micro switch KW11-3Z with lever", category="switch",
    length=19.8, width=6.4, height=10.2, mount="pocket", clearance=0.3,
    aliases=("micro switch", "limit switch", "kw11", "kw11-3z", "lever switch"),
    search="KW11-3Z micro limit switch lever 3 pin", source="community", confidence=0.7,
    tags=("input",),
    notes="Body 19.8 × 6.4 × 10.2; two Ø2.5 mounting holes 9.5 apart (positions not verified here).",
))
_add(Component(
    key="buzzer_12mm", name="Buzzer Ø12 mm (active, 5 V)", category="speaker",
    length=12.0, width=12.0, height=9.7, mount="pocket", clearance=0.3,
    apertures=(_A("speaker", 6.0, 6.0, d=3.0, face="top"),),
    aliases=("buzzer", "piezo buzzer", "12mm buzzer", "active buzzer", "passive buzzer"),
    search="12mm active buzzer 5V", source="datasheet", confidence=0.9,
    tags=("speaking",),
    notes="Ø12 × 9.7, pins 7.6 apart (Adafruit page). Passive ones are the same can; a 3 mm sound hole "
          "over the top suffices.",
))

# =====================================================================================================
# Displays — Adafruit/Waveshare pages (datasheet), 1602/2004 (standard drawings), generics (community).
# =====================================================================================================
_add(Component(
    key="oled_096_ssd1306", name="OLED 0.96\" 128×64 SSD1306 (generic 4-pin I2C)", category="display",
    length=27.5, width=27.5, height=4.5, mount="pocket", clearance=0.4,
    apertures=(_A("screen", 13.75, 12.5, w=24.0, h=13.0, face="top"),),
    aliases=("0.96 oled", "ssd1306", "0.96 inch oled", "oled 128x64", "0.96\" oled", "small oled"),
    search="0.96 inch OLED display 128x64 I2C SSD1306", source="community", confidence=0.6,
    tags=("display",),
    notes="27 × 27–28 × 4 (with the header standing ~12 behind). Active area 21.7 × 10.9, offset toward "
          "the edge away from the pins. Corner holes exist on many but vary — pocket. Window approximate.",
))
_add(Component(
    key="oled_096_adafruit", name="Adafruit 0.96\" 128×64 OLED (STEMMA QT)", category="display",
    length=29.2, width=26.7, height=6.2, mount="pocket", clearance=0.4,
    apertures=(_A("screen", 14.6, 14.5, w=26.6, h=19.0, face="top"),),
    aliases=("adafruit 0.96 oled", "adafruit ssd1306", "oled stemma qt 0.96"),
    search="Adafruit monochrome 0.96 128x64 OLED STEMMA QT", source="datasheet", confidence=0.85,
    tags=("display",),
    notes="29.2 × 26.7 × 6.2, glass 26.6 × 19 (product page); mounting holes 24 mm apart — positions not "
          "verified, pocket.",
))
_add(Component(
    key="oled_13_sh1106", name="OLED 1.3\" 128×64 SH1106 (generic 4-pin I2C)", category="display",
    length=35.4, width=33.5, height=4.5, mount="pocket", clearance=0.4,
    apertures=(_A("screen", 17.7, 15.5, w=31.0, h=17.0, face="top"),),
    aliases=("1.3 oled", "sh1106", "1.3 inch oled", "1.3\" oled"),
    search="1.3 inch OLED display 128x64 I2C SH1106", source="community", confidence=0.55,
    tags=("display",),
    notes="~35.4 × 33.5; the glass is 34.5 × 23 with a 29.4 × 14.7 active area. Window approximate.",
))
_add(Component(
    key="oled_13_adafruit", name="Adafruit 1.3\" 128×64 OLED (STEMMA QT)", category="display",
    length=35.6, width=33.0, height=6.2, mount="pocket", clearance=0.4,
    apertures=(_A("screen", 17.8, 17.5, w=34.5, h=23.0, face="top"),),
    aliases=("adafruit 1.3 oled", "adafruit sh1107", "oled stemma qt 1.3"),
    search="Adafruit monochrome 1.3 128x64 OLED STEMMA QT", source="datasheet", confidence=0.85,
    tags=("display",),
    notes="35.6 × 33 × 6.2, screen 34.5 × 23, active 29.4 × 14.7; four 2.5 mm holes on 30.5 × 28 — "
          "not placed here (centring unverified), pocket.",
))
_add(Component(
    key="st7789_154_adafruit", name="Adafruit 1.54\" 240×240 TFT ST7789 (EYESPI)", category="display",
    length=43.7, width=41.8, height=5.5, mount="pocket", clearance=0.4,
    apertures=(_A("screen", 21.85, 21.0, w=32.0, h=31.0, face="top"),),
    ports=(_P("sd", "back", 21.85, width=13.0, height=3.0),),
    aliases=("1.54 tft", "st7789 1.54", "adafruit 1.54 tft", "1.54 inch ips"),
    search="Adafruit 1.54 240x240 wide angle TFT LCD ST7789", source="datasheet", confidence=0.85,
    tags=("display", "storage"),
    notes="43.7 × 41.8 × 5.5, active 32 × 31 (product page). Four 2.5 mm holes on 1.5 × 1.4 in — not "
          "placed (centring unverified). microSD on the back edge.",
))
_add(Component(
    key="st7789_20_adafruit", name="Adafruit 2.0\" 320×240 IPS TFT ST7789 (EYESPI)", category="display",
    length=59.2, width=35.5, height=3.7, mount="pocket", clearance=0.4,
    apertures=(_A("screen", 29.6, 17.75, w=40.8, h=30.6, face="top"),),
    aliases=("2.0 tft", "st7789 2 inch", "adafruit 2.0 tft", "2 inch ips tft"),
    search="Adafruit 2.0 320x240 color IPS TFT ST7789", source="datasheet", confidence=0.85,
    tags=("display",),
    notes="59.2 × 35.5 × 3.7 (product page); no mounting holes. The 2.0\" active area is 40.8 × 30.6.",
))
_add(Component(
    key="st7789_20_waveshare", name="Waveshare 2\" 240×320 IPS LCD module ST7789", category="display",
    length=58.0, width=35.0, height=5.0, mount="pocket", clearance=0.4,
    apertures=(_A("screen", 29.0, 17.5, w=40.8, h=30.6, face="top"),),
    aliases=("waveshare 2 inch lcd", "2inch lcd module", "waveshare st7789"),
    search="Waveshare 2inch LCD module 240x320 ST7789 SPI", source="datasheet", confidence=0.8,
    tags=("display",),
    notes="58 × 35 outline, display 30.6 × 40.8 (Waveshare wiki); height approximate (pins behind).",
))
_add(Component(
    key="lcd_1602", name="LCD 16×2 character (1602A)", category="display",
    length=80.0, width=36.0, height=12.0,
    holes=(_H(2.5, 2.5, 2.5), _H(77.5, 2.5, 2.5), _H(2.5, 33.5, 2.5), _H(77.5, 33.5, 2.5)),
    apertures=(_A("screen", 40.0, 18.0, w=66.0, h=17.0, face="top"),),
    mount="standoff", clearance=0.4,
    aliases=("1602", "16x2 lcd", "1602 lcd", "16x2 display", "lcd1602", "character lcd"),
    search="1602 LCD 16x2 character display module blue", source="datasheet", confidence=0.9,
    tags=("display",),
    notes="80 × 36 × 12 with backlight; four 2.5 mm holes on 75 × 31, 2.5 from the edges (module drawing). "
          "Viewing area ~64.5 × 16, slightly toward the pin edge. With an I2C backpack the height is ~20.",
))
_add(Component(
    key="lcd_2004", name="LCD 20×4 character (2004A)", category="display",
    length=98.0, width=60.0, height=12.0,
    holes=(_H(2.5, 2.5, 2.5), _H(95.5, 2.5, 2.5), _H(2.5, 57.5, 2.5), _H(95.5, 57.5, 2.5)),
    apertures=(_A("screen", 49.0, 30.0, w=78.0, h=26.0, face="top"),),
    mount="standoff", clearance=0.4,
    aliases=("2004", "20x4 lcd", "2004 lcd", "20x4 display", "lcd2004"),
    search="2004 LCD 20x4 character display module", source="datasheet", confidence=0.8,
    tags=("display",),
    notes="98 × 60 × 12; holes on 93 × 55, 2.5 from the edges (2004A drawing). Viewing area ~76 × 25.5.",
))
_add(Component(
    key="lcd_12864_ks0108", name="Graphic LCD 128×64 KS0108 (12864A)", category="display",
    length=93.0, width=70.0, height=13.0, mount="pocket", clearance=0.5,
    apertures=(_A("screen", 46.5, 37.0, w=71.0, h=40.0, face="top"),),
    aliases=("12864", "ks0108", "128x64 lcd", "graphic lcd 12864", "12864a"),
    search="12864 KS0108 128x64 graphic LCD module", source="community", confidence=0.65,
    tags=("display",),
    notes="93 × 70 × 13; view area 70 × 38.8; four corner holes exist but aren't placed here — pocket.",
))
_add(Component(
    key="tm1637_4digit", name="TM1637 4-digit 0.36\" 7-segment module", category="display",
    length=42.0, width=24.0, height=12.0, mount="pocket", clearance=0.4,
    apertures=(_A("screen", 21.0, 12.0, w=31.0, h=15.0, face="top"),),
    aliases=("tm1637", "4 digit display", "7 segment display module", "tm1637 clock display"),
    search="TM1637 4 digit 0.36 inch 7 segment display module", source="community", confidence=0.65,
    tags=("display",),
    notes="42 × 24 × 12 with four Ø2.2 corner holes (not placed). The digit block is ~30 × 14.",
))
_add(Component(
    key="max7219_8x8", name="MAX7219 8×8 LED matrix module (single)", category="display",
    length=50.0, width=32.0, height=15.0, mount="pocket", clearance=0.4,
    apertures=(_A("screen", 25.0, 16.0, w=32.0, h=32.0, face="top"),),
    aliases=("max7219", "8x8 matrix", "led matrix module", "max7219 matrix", "dot matrix module"),
    search="MAX7219 8x8 dot matrix LED display module", source="community", confidence=0.6,
    tags=("display", "lighting"),
    notes="50 × 32 × 15 with a 32 × 32 matrix; four Ø3 holes (not placed). The 4-in-1 is 128 × 32.",
))

# =====================================================================================================
# Sensors — Adafruit STEMMA boards (pages), AM2302/HC-SR501/TSOP (datasheets), GY-* generics (community).
# =====================================================================================================
_add(Component(
    key="dht22", name="DHT22 / AM2302 temperature-humidity sensor", category="sensor",
    length=25.1, width=15.1, height=7.7, mount="pocket", clearance=0.4,
    apertures=(_A("vent", 12.5, 7.5, w=14.0, h=10.0, face="top"),),
    aliases=("dht22", "am2302", "dht 22", "humidity sensor"),
    search="DHT22 AM2302 temperature humidity sensor", source="datasheet", confidence=0.9,
    tags=("sensing",),
    notes="15.1 × 25.1 × 7.7 (AM2302 datasheet); the grille face must see air — vent it. One Ø3 lug hole at "
          "the top (not placed). The 3-pin module versions are ~ 38 × 20.",
))
_add(Component(
    key="dht11", name="DHT11 temperature-humidity sensor", category="sensor",
    length=15.5, width=12.0, height=5.5, mount="pocket", clearance=0.4,
    apertures=(_A("vent", 7.75, 6.0, w=10.0, h=8.0, face="top"),),
    aliases=("dht11", "dht 11"),
    search="DHT11 temperature humidity sensor module", source="datasheet", confidence=0.8,
    tags=("sensing",),
    notes="15.5 × 12 × 5.5 blue block; vent the grille side.",
))
_add(Component(
    key="bme280_gy", name="BME280 breakout GY-BME280 (6-pin, 3.3 V)", category="sensor",
    length=15.4, width=11.6, height=2.4, mount="pocket", clearance=0.4,
    apertures=(_A("vent", 7.7, 5.8, d=4.0, face="top"),),
    aliases=("bme280", "gy-bme280", "bme 280", "bme280 module"),
    search="GY-BME280 3.3V temperature humidity pressure sensor module", source="community",
    confidence=0.65, tags=("sensing",),
    notes="~15.4 × 11.6; one Ø3 hole at one end. Needs an air path to read humidity/pressure.",
))
_add(Component(
    key="bme280_adafruit", name="Adafruit BME280 (STEMMA QT)", category="sensor",
    length=25.2, width=18.0, height=4.6, mount="pocket", clearance=0.4,
    apertures=(_A("vent", 12.6, 9.0, d=4.0, face="top"),),
    aliases=("adafruit bme280", "bme280 stemma"),
    search="Adafruit BME280 I2C SPI temperature humidity pressure STEMMA QT", source="datasheet",
    confidence=0.85, tags=("sensing",),
    notes="25.2 × 18 × 4.6 (product page); STEMMA QT jacks on both short edges; two holes (not placed).",
))
_add(Component(
    key="bmp280_gy", name="BMP280 breakout GY-BMP280-3.3", category="sensor",
    length=15.0, width=11.5, height=2.5, mount="pocket", clearance=0.4,
    aliases=("bmp280", "gy-bmp280", "bmp 280", "pressure sensor module"),
    search="GY-BMP280-3.3 barometric pressure sensor module", source="community", confidence=0.65,
    tags=("sensing",),
    notes="11.5 × 15 (component datasheet page); one hole at one end (not placed).",
))
_add(Component(
    key="hc_sr04", name="HC-SR04 ultrasonic distance sensor", category="sensor",
    length=45.0, width=20.0, height=15.0, mount="pocket", clearance=0.5,
    apertures=(_A("sensor", 10.0, 10.0, d=16.5, face="top"), _A("sensor", 35.0, 10.0, d=16.5, face="top")),
    ports=(_P("header", "front", 22.5, width=11.0, height=3.0),),
    aliases=("hc-sr04", "hcsr04", "ultrasonic sensor", "ultrasonic distance sensor", "hc sr04"),
    search="HC-SR04 ultrasonic distance sensor module", source="datasheet", confidence=0.7,
    tags=("sensing",),
    notes="45 × 20 × 15 (user guide). The two Ø16 transducers sit ~25 mm apart (read off the drawing, ±1) — "
          "16.5 mm bores through the wall they face; the sensor usually stands on edge against that wall.",
))
_add(Component(
    key="hc_sr501", name="HC-SR501 PIR motion sensor", category="sensor",
    length=32.2, width=24.3, height=20.5, mount="pocket", clearance=0.5,
    apertures=(_A("sensor", 16.1, 12.15, d=23.5, face="top"),),
    aliases=("hc-sr501", "pir sensor", "pir motion sensor", "hc sr501", "motion sensor"),
    search="HC-SR501 PIR motion sensor module", source="datasheet", confidence=0.75,
    tags=("sensing",),
    notes="32 × 24 board, 20.5 tall with the Ø23 Fresnel dome; the dome pokes through a 23.5 mm bore. Two "
          "trimmers and a jumper on the back. Two corner holes (not placed).",
))
_add(Component(
    key="mpu6050_gy521", name="MPU-6050 breakout GY-521", category="sensor",
    length=21.0, width=16.0, height=3.0, mount="pocket", clearance=0.4,
    aliases=("mpu6050", "gy-521", "mpu 6050", "gy521", "imu module", "accelerometer gyro"),
    search="GY-521 MPU-6050 accelerometer gyroscope module", source="community", confidence=0.65,
    tags=("sensing", "motion"),
    notes="21 × 16 × 3; two Ø3 holes at diagonal corners (not placed). Height with the header ~11.",
))
_add(Component(
    key="mpu6050_adafruit", name="Adafruit MPU-6050 (STEMMA QT)", category="sensor",
    length=26.0, width=17.8, height=4.6, mount="pocket", clearance=0.4,
    aliases=("adafruit mpu6050", "mpu6050 stemma"),
    search="Adafruit MPU-6050 6-DoF accel gyro STEMMA QT", source="datasheet", confidence=0.85,
    tags=("sensing", "motion"),
    notes="26 × 17.8 × 4.6 with four 2.5 mm holes (product page; positions not placed).",
))
_add(Component(
    key="mlx90640_adafruit", name="Adafruit MLX90640 thermal camera (55°)", category="sensor",
    length=25.7, width=17.7, height=16.0, mount="pocket", clearance=0.4,
    apertures=(_A("sensor", 12.85, 8.85, d=9.0, face="top"),),
    aliases=("mlx90640", "thermal camera", "ir thermal camera", "mlx 90640", "thermal sensor"),
    search="Adafruit MLX90640 IR thermal camera breakout", source="datasheet", confidence=0.8,
    tags=("vision", "sensing"),
    notes="25.7 × 17.7 × 16 (product page; the can is the height). The 32×24 thermal array looks out of the "
          "can — a 9 mm bore centred on the can (position approximate, ±1).",
))
_add(Component(
    key="vl53l0x_gy530", name="VL53L0X ToF distance breakout GY-530", category="sensor",
    length=25.0, width=10.7, height=3.0, mount="pocket", clearance=0.4,
    apertures=(_A("sensor", 12.5, 5.35, w=5.0, h=3.0, face="top"),),
    aliases=("vl53l0x", "gy-530", "tof sensor", "laser distance sensor", "gy530"),
    search="GY-530 VL53L0X time of flight distance sensor module", source="community", confidence=0.55,
    tags=("sensing",),
    notes="The long thin 6-pin board (~25 × 10.7); vendors also sell a 13 × 18 square one. The sensor "
          "window must see out — a small clear opening, no glass in front.",
))
_add(Component(
    key="vl53l0x_adafruit", name="Adafruit VL53L0X ToF distance sensor", category="sensor",
    length=21.0, width=18.0, height=2.8, mount="pocket", clearance=0.4,
    apertures=(_A("sensor", 10.5, 9.0, w=5.0, h=3.0, face="top"),),
    aliases=("adafruit vl53l0x", "vl53l0x stemma"),
    search="Adafruit VL53L0X time of flight distance sensor", source="datasheet", confidence=0.85,
    tags=("sensing",),
    notes="21 × 18 × 2.8 (product page); four holes (not placed). Window position approximate.",
))
_add(Component(
    key="mq2_module", name="MQ-2 gas/smoke sensor module", category="sensor",
    length=32.0, width=20.0, height=22.0, mount="pocket", clearance=0.5,
    apertures=(_A("vent", 10.0, 10.0, d=17.0, face="top"),),
    aliases=("mq-2", "mq2", "gas sensor", "smoke sensor", "mq 2"),
    search="MQ-2 gas smoke sensor module", source="community", confidence=0.65,
    tags=("sensing",),
    notes="32 × 20 board, 22 tall with the Ø16 sensor can (position approximate). Runs warm; vent it.",
))
_add(Component(
    key="tsop38238", name="IR receiver TSOP38238 (38 kHz)", category="sensor",
    length=5.0, width=4.8, height=6.95, mount="pocket", clearance=0.3,
    apertures=(_A("sensor", 2.5, 2.4, d=4.0, face="front"),),
    aliases=("tsop38238", "ir receiver", "tsop 38238", "infrared receiver", "38khz ir receiver"),
    search="TSOP38238 IR receiver 38kHz", source="datasheet", confidence=0.95,
    tags=("sensing", "input"),
    notes="Minicast 5.0 W × 6.95 H × 4.8 D (Vishay). The dome looks out of the front; IR passes through a "
          "4 mm hole or dark-tinted plastic. Leads 2.54 pitch.",
))
_add(Component(
    key="ds18b20_probe", name="DS18B20 waterproof temperature probe (Ø6 × 50)", category="sensor",
    length=50.0, width=6.0, height=6.0, mount="clip", clearance=0.3,
    aliases=("ds18b20", "ds18b20 probe", "waterproof temperature sensor", "temperature probe"),
    search="DS18B20 waterproof temperature sensor probe", source="datasheet", confidence=0.8,
    tags=("sensing",),
    notes="Stainless tube Ø6 × 50 on a cable; a 6.4 mm through-hole with a grommet, or a clip inside.",
))
_add(Component(
    key="ds3231_zs042", name="DS3231 RTC module ZS-042 (with CR2032)", category="sensor",
    length=38.0, width=22.0, height=14.0, mount="pocket", clearance=0.5,
    aliases=("ds3231", "zs-042", "rtc module", "real time clock", "ds3231 rtc"),
    search="DS3231 RTC real time clock module AT24C32", source="community", confidence=0.7,
    tags=("sensing",),
    notes="38 × 22 × 14 with the coin cell fitted (vendors). Battery slides out of one end — leave access.",
))
_add(Component(
    key="rc522", name="RC522 RFID reader module (13.56 MHz)", category="sensor",
    length=60.0, width=39.5, height=5.0, mount="pocket", clearance=0.5,
    aliases=("rc522", "rfid module", "mfrc522", "rfid reader", "rc-522"),
    search="RC522 RFID reader module 13.56MHz with card", source="community", confidence=0.6,
    tags=("sensing", "input"),
    notes="~60 × 40 (vendors), 5 tall bare, ~13 with the right-angle header. The antenna coil is the board; "
          "keep it against a thin plastic wall, no metal.",
))
_add(Component(
    key="bh1750_gy302", name="BH1750 light sensor GY-302", category="sensor",
    length=18.5, width=13.5, height=3.0, mount="pocket", clearance=0.4,
    apertures=(_A("sensor", 9.0, 6.75, d=3.0, face="top"),),
    aliases=("bh1750", "gy-302", "light sensor", "lux sensor", "gy302"),
    search="GY-302 BH1750 light intensity sensor module", source="community", confidence=0.55,
    tags=("sensing",),
    notes="~18.5 × 13.5; the sensor needs a clear window (position approximate).",
))

# =====================================================================================================
# Motors and moving parts — TowerPro / Kiatronics datasheets, NEMA standard, PC-fan standard.
# =====================================================================================================
_add(Component(
    key="servo_sg90", name="Micro servo SG90 (9 g)", category="motor",
    length=32.2, width=12.2, height=31.0, mount="pocket", clearance=0.3,
    apertures=(_A("shaft", 10.85, 6.1, d=7.0, face="top"),),  # from the tab end (body starts ~4.85 in)
    aliases=("sg90", "9g servo", "micro servo", "sg90 servo", "sg 90"),
    search="SG90 9g micro servo motor", source="datasheet", confidence=0.85,
    tags=("motion",),
    notes="Body 22.5 × 12.2 × 22.7, 32.2 over the tabs, 31 to the top of the horn (TowerPro). Tab holes are "
          "Ø2 on ~27.5–28 centres — not placed here; a 23 × 12.5 cutout with the tabs screwed to the wall "
          "is the usual mount. Shaft is 6 mm from one end (approximate).",
))
_add(Component(
    key="servo_mg90s", name="Micro servo MG90S (metal gear)", category="motor",
    length=32.5, width=12.2, height=28.5, mount="pocket", clearance=0.3,
    apertures=(_A("shaft", 10.85, 6.1, d=7.0, face="top"),),  # from the tab end (body starts ~4.85 in)
    aliases=("mg90s", "mg90", "metal gear micro servo", "mg90s servo"),
    search="MG90S metal gear micro servo", source="datasheet", confidence=0.85,
    tags=("motion",),
    notes="22.8 × 12.2 × 28.5 body; same mount as the SG90 (cutout 23 × 12.5).",
))
_add(Component(
    key="servo_mg996r", name="Standard servo MG996R", category="motor",
    length=53.6, width=20.0, height=42.9, mount="pocket", clearance=0.3,
    apertures=(_A("shaft", 16.5, 10.0, d=8.0, face="top"),),  # from the tab end (body starts ~6.5 in)
    aliases=("mg996r", "mg996", "mg995", "standard servo", "mg996r servo"),
    search="MG996R metal gear standard servo", source="datasheet", confidence=0.85,
    tags=("motion",),
    notes="40.7 × 19.7 × 42.9 body, 53.6 over the tabs (Handson drawing). Standard-size tab holes are on a "
          "49.5 × 10 pattern (Ø4.3) — not placed here; cutout 41 × 20.5. Shaft ~10 mm from one end.",
))
_add(Component(
    key="nema17", name="NEMA 17 stepper motor (42 mm, 40 long)", category="motor",
    length=42.3, width=42.3, height=40.0,
    holes=(_H(5.65, 5.65, 3.2), _H(36.65, 5.65, 3.2), _H(5.65, 36.65, 3.2), _H(36.65, 36.65, 3.2)),
    apertures=(_A("shaft", 21.15, 21.15, d=22.5, face="top"),),
    mount="standoff", clearance=0.5,
    aliases=("nema 17", "nema17 stepper", "17hs4401", "stepper motor nema 17", "42 stepper"),
    search="NEMA 17 stepper motor 17HS4401 1.8 degree", source="datasheet", confidence=0.95,
    tags=("motion",),
    notes="42.3 square, M3 holes on 31 × 31, Ø22 × 2 pilot boss, Ø5 × 24 shaft (NEMA 17 standard; body "
          "length 34/40/48 by model — this is the common 40). Height excludes the shaft.",
))
_add(Component(
    key="stepper_28byj48", name="Stepper 28BYJ-48 (5 V, geared)", category="motor",
    length=42.0, width=28.0, height=19.0,
    holes=(_H(3.5, 14.0, 4.2), _H(38.5, 14.0, 4.2)),
    apertures=(_A("shaft", 21.0, 22.0, d=9.5, face="top"),),
    mount="standoff", clearance=0.5,
    aliases=("28byj-48", "28byj48", "28byj", "5v stepper", "small stepper motor"),
    search="28BYJ-48 5V stepper motor with ULN2003 driver", source="datasheet", confidence=0.8,
    tags=("motion",),
    notes="Ø28 body, 19 tall, ears on 35 mm centres with Ø4.2 holes (Kiatronics datasheet); the Ø5 × 10 "
          "shaft on its Ø9 boss sits 8 mm off the body centre, away from the lead (direction approximate).",
))
_add(Component(
    key="motor_tt_gear", name="TT gear motor (yellow, 1:48)", category="motor",
    length=70.0, width=22.5, height=18.6, mount="pocket", clearance=0.5,
    aliases=("tt motor", "yellow gear motor", "tt gear motor", "dc gear motor tt"),
    search="TT gear motor 1:48 DC 3-6V robot wheel", source="community", confidence=0.5,
    tags=("motion",),
    notes="~65 body + shaft ~70 overall × 22.5 × 18.6; double-sided 5.5 mm D shaft. Vendors vary — measure.",
))
_add(Component(
    key="vibration_motor_10mm", name="Coin vibration motor Ø10 × 2.7", category="motor",
    length=10.0, width=10.0, height=2.7, mount="adhesive", clearance=0.3,
    aliases=("vibration motor", "coin motor", "haptic motor", "10mm vibration motor"),
    search="10mm coin vibration motor 3V", source="datasheet", confidence=0.75,
    tags=("motion",),
    notes="Ø10 × 2.7 (also 8 × 2.7 and 12 × 3.4). Sticks down with its own adhesive pad.",
))
_add(Component(
    key="fan_30mm", name="Fan 30 × 30 × 10 mm (5 V)", category="motor",
    length=30.0, width=30.0, height=10.0,
    holes=(_H(3.0, 3.0, 3.2), _H(27.0, 3.0, 3.2), _H(3.0, 27.0, 3.2), _H(27.0, 27.0, 3.2)),
    apertures=(_A("vent", 15.0, 15.0, d=28.0, face="top"),),
    mount="standoff", clearance=0.3,
    aliases=("30mm fan", "3010 fan", "30x30 fan", "small fan 30mm"),
    search="30mm 5V cooling fan 3010", source="datasheet", confidence=0.9,
    tags=("motion",),
    notes="Standard 30 mm fan: holes on 24 × 24 (3 from the edges), Ø28 airway. 3007 is 7 mm thick.",
))
_add(Component(
    key="fan_40mm", name="Fan 40 × 40 × 10 mm (5 V / 12 V)", category="motor",
    length=40.0, width=40.0, height=10.0,
    holes=(_H(4.0, 4.0, 3.2), _H(36.0, 4.0, 3.2), _H(4.0, 36.0, 3.2), _H(36.0, 36.0, 3.2)),
    apertures=(_A("vent", 20.0, 20.0, d=38.0, face="top"),),
    mount="standoff", clearance=0.3,
    aliases=("40mm fan", "4010 fan", "40x40 fan", "case fan 40mm"),
    search="40mm 5V cooling fan 4010", source="datasheet", confidence=0.9,
    tags=("motion",),
    notes="Standard 40 mm fan: holes on 32 × 32 (4 from the edges), Ø38 airway. 4020 is 20 mm thick.",
))

# =====================================================================================================
# Drivers and relays — Pololu (A4988 outline), generic modules (community).
# =====================================================================================================
_add(Component(
    key="driver_uln2003", name="ULN2003 stepper driver board (for 28BYJ-48)", category="driver",
    length=35.0, width=31.0, height=11.0, mount="pocket", clearance=0.5,
    aliases=("uln2003", "uln2003 driver", "28byj driver board", "uln 2003"),
    search="ULN2003 stepper motor driver board 28BYJ-48", source="community", confidence=0.65,
    tags=("motion",),
    notes="35 × 31 × 11 with four corner M3 holes (positions not verified — pocket). XH-5 socket on one edge.",
))
_add(Component(
    key="driver_l298n", name="L298N dual H-bridge motor driver module", category="driver",
    length=43.0, width=43.0, height=27.0, mount="pocket", clearance=0.5,
    aliases=("l298n", "l298n driver", "motor driver module", "l298", "h bridge module"),
    search="L298N motor driver module dual H bridge", source="community", confidence=0.7,
    tags=("motion", "power"),
    notes="43 × 43 × 27 (heatsink is the height); four Ø3 corner holes ~37 apart (not verified — pocket). "
          "Screw terminals on three edges need side access.",
))
_add(Component(
    key="driver_a4988", name="A4988 stepper driver (StepStick)", category="driver",
    length=20.3, width=15.2, height=12.0, mount="pocket", clearance=0.4,
    aliases=("a4988", "a4988 driver", "stepstick", "a 4988"),
    search="A4988 stepper motor driver module with heatsink", source="datasheet", confidence=0.8,
    tags=("motion",),
    notes="0.6 × 0.8 in (Pololu outline); 12 tall with the heatsink, pins 15.24 apart underneath.",
))
_add(Component(
    key="driver_tmc2209", name="TMC2209 stepper driver (StepStick)", category="driver",
    length=20.3, width=15.2, height=12.0, mount="pocket", clearance=0.4,
    aliases=("tmc2209", "tmc 2209", "tmc2208", "silent stepper driver"),
    search="TMC2209 stepper motor driver V1.3", source="community", confidence=0.65,
    tags=("motion",),
    notes="Same StepStick footprint as the A4988 (vendor boards vary a few tenths). Runs hot — heatsink up.",
))
_add(Component(
    key="relay_1ch", name="Relay module, 1 channel (5 V)", category="driver",
    length=50.0, width=26.0, height=19.0, mount="pocket", clearance=0.5,
    aliases=("relay module", "1 channel relay", "single relay module", "relay 1ch", "5v relay module"),
    search="5V 1 channel relay module optocoupler", source="community", confidence=0.6,
    tags=("power",),
    notes="~50 × 26 × 19; corner holes vary by vendor (not placed — pocket). Mains on the screw terminals — "
          "keep 3 mm clearance and a separate low-voltage side.",
))
_add(Component(
    key="relay_2ch", name="Relay module, 2 channel (5 V)", category="driver",
    length=50.5, width=38.5, height=19.0, mount="pocket", clearance=0.5,
    aliases=("2 channel relay", "dual relay module", "relay 2ch", "2ch relay", "two channel relay"),
    search="5V 2 channel relay module optocoupler", source="community", confidence=0.6,
    tags=("power",),
    notes="~50.5 × 38.5 × 19; holes vary (not placed). Terminals on the long edge need side access.",
))
_add(Component(
    key="relay_4ch", name="Relay module, 4 channel (5 V)", category="driver",
    length=75.0, width=55.0, height=19.0, mount="pocket", clearance=0.5,
    aliases=("4 channel relay", "quad relay module", "relay 4ch", "4ch relay", "four channel relay"),
    search="5V 4 channel relay module optocoupler", source="community", confidence=0.6,
    tags=("power",),
    notes="~75 × 55 × 19 (the common Songle board); holes vary (not placed).",
))

# =====================================================================================================
# LEDs and lighting — LED package standards, Adafruit NeoPixel product pages, WS2812 8-ring (vendors).
# =====================================================================================================
_add(Component(
    key="led_5mm", name="LED 5 mm through-hole", category="led",
    length=5.0, width=5.0, height=8.6, mount="pocket", clearance=0.1,
    apertures=(_A("led", 2.5, 2.5, d=5.2, face="top"),),
    aliases=("5mm led", "led 5mm", "5 mm led", "led"),
    search="5mm LED assorted kit", source="datasheet", confidence=0.95,
    tags=("lighting",),
    notes="Ø5 dome, 8.6 tall body, Ø5.8 flange; a 5.2 mm hole holds it by friction (led_window(5.2)).",
))
_add(Component(
    key="led_3mm", name="LED 3 mm through-hole", category="led",
    length=3.0, width=3.0, height=5.4, mount="pocket", clearance=0.1,
    apertures=(_A("led", 1.5, 1.5, d=3.2, face="top"),),
    aliases=("3mm led", "led 3mm", "3 mm led"),
    search="3mm LED assorted kit", source="datasheet", confidence=0.95,
    tags=("lighting",),
    notes="Ø3 dome, 5.4 tall, Ø3.8 flange; a 3.2 mm hole.",
))
_add(Component(
    key="ir_led_850nm", name="IR LED 5 mm 850 nm (night-vision illuminator)", category="led",
    length=5.0, width=5.0, height=8.6, mount="pocket", clearance=0.1,
    apertures=(_A("led", 2.5, 2.5, d=5.2, face="top"),),
    aliases=("ir led", "850nm led", "infrared led", "night vision led", "ir illuminator"),
    search="850nm IR LED 5mm infrared emitter", source="datasheet", confidence=0.9,
    tags=("lighting", "vision"),
    notes="Same 5 mm package as a visible LED; 850 nm glows faintly red, 940 nm is invisible but dimmer to "
          "the camera. Several around the lens make an ESP32-CAM see at night.",
))
_add(Component(
    key="ws2812_ring_8", name="WS2812B ring, 8 LEDs (Ø32)", category="led",
    length=32.0, width=32.0, height=3.5, mount="pocket", clearance=0.3,
    apertures=(_A("led", 16.0, 16.0, d=32.0, face="top"),),
    aliases=("8 led ring", "neopixel ring 8", "ws2812 ring 8", "8 pixel ring"),
    search="WS2812B 8 bit RGB LED ring", source="community", confidence=0.7,
    tags=("lighting",),
    notes="Outer Ø32, inner Ø18 (vendors). Diffuse it with 1 mm of white PLA.",
))
_add(Component(
    key="ws2812_ring_12", name="NeoPixel ring, 12 LEDs (Ø36.8)", category="led",
    length=36.8, width=36.8, height=6.7, mount="pocket", clearance=0.3,
    apertures=(_A("led", 18.4, 18.4, d=36.8, face="top"),),
    aliases=("12 led ring", "neopixel ring 12", "ws2812 ring 12", "12 pixel ring"),
    search="NeoPixel ring 12 x 5050 RGB LED WS2812", source="datasheet", confidence=0.9,
    tags=("lighting",),
    notes="Outer 36.8, inner 23.3, 6.7 thick (Adafruit page — that thickness includes the pads/LEDs).",
))
_add(Component(
    key="ws2812_ring_16", name="NeoPixel ring, 16 LEDs (Ø44.5)", category="led",
    length=44.5, width=44.5, height=6.7, mount="pocket", clearance=0.3,
    apertures=(_A("led", 22.25, 22.25, d=44.5, face="top"),),
    aliases=("16 led ring", "neopixel ring 16", "ws2812 ring 16", "16 pixel ring"),
    search="NeoPixel ring 16 x 5050 RGB LED WS2812", source="datasheet", confidence=0.9,
    tags=("lighting",),
    notes="Outer 44.5, inner 31.7, 6.7 thick (Adafruit page).",
))
_add(Component(
    key="ws2812_ring_24", name="NeoPixel ring, 24 LEDs (Ø65.5)", category="led",
    length=65.5, width=65.5, height=3.2, mount="pocket", clearance=0.3,
    apertures=(_A("led", 32.75, 32.75, d=65.5, face="top"),),
    aliases=("24 led ring", "neopixel ring 24", "ws2812 ring 24", "24 pixel ring"),
    search="NeoPixel ring 24 x 5050 RGB LED WS2812", source="datasheet", confidence=0.9,
    tags=("lighting",),
    notes="Outer 65.5, inner 52.3, 3.2 thick (Adafruit page).",
))
_add(Component(
    key="ws2812_stick_8", name="NeoPixel stick, 8 LEDs", category="led",
    length=51.1, width=10.2, height=3.2, mount="pocket", clearance=0.3,
    apertures=(_A("led", 25.55, 5.1, w=51.0, h=10.0, face="top"),),
    aliases=("neopixel stick", "8 led stick", "ws2812 stick", "led stick 8"),
    search="NeoPixel stick 8 x 5050 RGB LED WS2812", source="datasheet", confidence=0.9,
    tags=("lighting",),
    notes="51.1 × 10.2 × 3.2 (Adafruit page); two mounting holes (not placed).",
))

# =====================================================================================================
# Connectors as parts (JST headers, from JST drawings — the plug side rides as a Port elsewhere).
# =====================================================================================================
_add(Component(
    key="jst_ph_2", name="JST PH 2-pin header (2.0 mm, B2B-PH-K-S)", category="connector",
    length=6.0, width=4.5, height=8.0, mount="pocket", clearance=0.2,
    aliases=("jst ph", "jst ph 2 pin", "ph 2.0 connector", "lipo connector", "jst-ph"),
    search="JST PH 2.0mm 2 pin connector kit", source="datasheet", confidence=0.75,
    tags=("power",),
    notes="Top-entry header ~6 × 4.5 × 8 tall; the plug adds ~8 mm cable path. The battery-lead standard.",
))
_add(Component(
    key="jst_xh_2", name="JST XH 2-pin header (2.5 mm, B2B-XH-A)", category="connector",
    length=10.0, width=5.75, height=7.0, mount="pocket", clearance=0.2,
    aliases=("jst xh", "jst xh 2 pin", "xh 2.5 connector", "jst-xh"),
    search="JST XH 2.5mm 2 pin connector kit", source="datasheet", confidence=0.7,
    tags=("power",),
    notes="Top-entry header ~10 × 5.75 × 7; the plug adds ~10 mm cable path. Common on 18650 holders.",
))

# =====================================================================================================
# Storage — Adafruit microSD breakout (page), generic SPI module (community).
# =====================================================================================================
_add(Component(
    key="microsd_adafruit", name="Adafruit micro SD SPI/SDIO breakout (3 V)", category="storage",
    length=25.4, width=22.8, height=3.5, mount="pocket", clearance=0.4,
    ports=(_P("sd", "left", 11.4, width=13.0, height=3.0),),
    aliases=("adafruit microsd", "microsd breakout", "micro sd breakout", "sd breakout"),
    search="Adafruit Micro SD SPI or SDIO card breakout board", source="datasheet", confidence=0.85,
    tags=("storage",),
    notes="25.4 × 22.8 × 3.5 (product page); the card slides in from a short edge — slot that wall.",
))
_add(Component(
    key="microsd_module", name="Micro SD card module (SPI, 6-pin, with regulator)", category="storage",
    length=42.0, width=24.0, height=12.0, mount="pocket", clearance=0.5,
    ports=(_P("sd", "left", 12.0, width=13.0, height=3.0),),
    aliases=("micro sd module", "sd card module", "microsd module", "tf card module", "sd card reader module"),
    search="Micro SD card module SPI TF card reader Arduino", source="community", confidence=0.55,
    tags=("storage",),
    notes="The big blue/red 6-pin module (~42 × 24; the Catalex type is 42 × 24 × 12 with the header). "
          "Card slot at one short edge.",
))

# =====================================================================================================
# Radios and comms — Ai-Thinker Ra-02 / Adafruit RFM95W (datasheet), nRF24/HC-05/NEO-6M (community).
# =====================================================================================================
_add(Component(
    key="nrf24l01", name="nRF24L01+ 2.4 GHz radio module", category="comm",
    length=29.0, width=15.2, height=4.6, mount="pocket", clearance=0.4,
    apertures=(_A("antenna", 22.0, 7.6, w=12.0, h=8.0, face="top"),),
    aliases=("nrf24l01", "nrf24", "nrf24l01+", "2.4ghz radio module", "nrf 24"),
    search="nRF24L01+ 2.4GHz wireless transceiver module", source="community", confidence=0.7,
    tags=("wireless",),
    notes="29 × 15.2 × 4.6 bare, ~12 with the 2×4 header; PCB antenna at one end (keep plastic there). The "
          "PA+LNA version has an SMA antenna and is 41 × 16.",
))
_add(Component(
    key="lora_ra02", name="LoRa SX1278 module Ra-02 (Ai-Thinker, 433 MHz)", category="comm",
    length=17.0, width=16.0, height=3.0, mount="pocket", clearance=0.3,
    ports=(_P("antenna", "back", 8.5),),
    aliases=("ra-02", "ra02", "sx1278", "sx1276", "lora module", "lora ra-02", "sx1276 lora"),
    search="Ai-Thinker Ra-02 SX1278 LoRa module 433MHz", source="datasheet", confidence=0.85,
    tags=("wireless",),
    notes="17 × 16 (Ai-Thinker spec); castellated SMD module with an IPEX antenna socket — needs a "
          "carrier or careful hand wiring. The SX1276 868/915 MHz Ra-01H is the same body.",
))
_add(Component(
    key="rfm95w_adafruit", name="Adafruit RFM95W LoRa radio breakout (868/915 MHz)", category="comm",
    length=29.0, width=25.0, height=4.0, mount="pocket", clearance=0.4,
    ports=(_P("antenna", "back", 14.5),),
    aliases=("rfm95w", "rfm95", "adafruit lora", "rfm95w breakout"),
    search="Adafruit RFM95W LoRa radio transceiver breakout 915MHz", source="datasheet", confidence=0.85,
    tags=("wireless",),
    notes="29 × 25 × 4 (product page); antenna as a wire, u.FL or SMA on one edge. Two holes (not placed).",
))
_add(Component(
    key="hc05", name="HC-05 Bluetooth module (ZS-040 carrier)", category="comm",
    length=43.0, width=17.0, height=7.0, mount="pocket", clearance=0.5,
    ports=(_P("header", "left", 8.5, width=16.0, height=3.0),),
    aliases=("hc-05", "hc05", "hc-06", "hc06", "bluetooth module", "zs-040"),
    search="HC-05 Bluetooth serial module ZS-040", source="community", confidence=0.6,
    tags=("wireless",),
    notes="~43 × 17 × 7 with the 6-pin right-angle header (vendors; the bare radio is 27 × 13). Antenna "
          "trace at the far end from the header.",
))
_add(Component(
    key="esp01", name="ESP-01 / ESP-01S Wi-Fi module", category="comm",
    length=24.8, width=14.3, height=5.0, mount="pocket", clearance=0.4,
    aliases=("esp-01", "esp01", "esp-01s", "esp8266 01"),
    search="ESP-01S ESP8266 WiFi module", source="community", confidence=0.7,
    tags=("wireless", "compute"),
    notes="24.8 × 14.3; the 2×4 header sticks out ~11 from one end. PCB antenna at the other end.",
))
_add(Component(
    key="neo6m_gy", name="GPS module NEO-6M / NEO-8M (GY-NEO6MV2)", category="comm",
    length=36.0, width=26.0, height=7.0, mount="pocket", clearance=0.5,
    ports=(_P("antenna", "back", 18.0),),
    aliases=("neo-6m", "neo6m", "neo-8m", "neo8m", "gps module", "gy-neo6mv2", "gps"),
    search="GY-NEO6MV2 NEO-6M GPS module with antenna", source="community", confidence=0.6,
    tags=("sensing", "wireless"),
    notes="Board ~36 × 26 (vendors quote 23–36 × 24–30 — measure yours); the 25 × 25 × 8 ceramic patch "
          "antenna is separate (gps_antenna_25) and needs sky, not metal.",
))
_add(Component(
    key="gps_antenna_25", name="GPS ceramic patch antenna 25 × 25 (u.FL/IPEX)", category="comm",
    length=25.0, width=25.0, height=8.0, mount="adhesive", clearance=0.3,
    apertures=(_A("antenna", 12.5, 12.5, w=25.0, h=25.0, face="top"),),
    aliases=("gps antenna", "ceramic gps antenna", "25x25 gps antenna", "patch antenna"),
    search="GPS active ceramic antenna 25x25mm IPEX", source="community", confidence=0.7,
    tags=("sensing", "wireless"),
    notes="25 × 25 × 8 with its ground plane; face up under a plastic (not metal, not carbon) lid.",
))

# =====================================================================================================
# Misc — breadboards and protoboard (standard sizes; the half breadboard varies by a millimetre).
# =====================================================================================================
_add(Component(
    key="breadboard_half", name="Half-size breadboard (400 points)", category="misc",
    length=83.0, width=55.0, height=10.0, mount="adhesive", clearance=0.5,
    aliases=("half breadboard", "400 point breadboard", "breadboard half", "small breadboard"),
    search="400 point half size breadboard", source="community", confidence=0.7,
    tags=("compute",),
    notes="82–83 × 54.5–55 × 8.5–10; peel-and-stick back. Rows of 5 at 2.54 pitch.",
))
_add(Component(
    key="breadboard_mini", name="Mini breadboard (170 points)", category="misc",
    length=46.0, width=35.0, height=8.5, mount="adhesive", clearance=0.5,
    aliases=("mini breadboard", "170 point breadboard", "tiny breadboard"),
    search="170 point mini breadboard", source="community", confidence=0.7,
    tags=("compute",),
    notes="~46 × 35 × 8.5; snaps to others on the sides; adhesive back.",
))
_add(Component(
    key="protoboard_5x7", name="Prototype PCB 5 × 7 cm (double-sided)", category="misc",
    length=70.0, width=50.0, height=1.6, mount="standoff", clearance=0.4,
    aliases=("protoboard", "perfboard 5x7", "5x7 pcb", "prototype board", "perf board"),
    search="5x7cm double sided prototype PCB perfboard", source="datasheet", confidence=0.85,
    tags=("compute",),
    notes="70 × 50 × 1.6, 2.54 grid; most have Ø2.5–3 corner holes ~2.5 in from the edges, but they aren't "
          "placed here (vendors differ) — standoffs under the corners work with a wide boss.",
))


# ----- lookup -----
def _candidates_for(t: str) -> list[Component]:
    out: list[Component] = []
    for c in CATALOG.values():
        if t == _norm(c.key) or t == _norm(c.name) or any(t == _norm(a) for a in c.aliases):
            out.append(c)
    return out


def find(text: str) -> Component | None:
    """Key, alias, or name — case/space/hyphen-insensitive; a spoken variant ("xiao s3 sense",
    "esp32 cam", "max 98357") resolves; a LiPo size code resolves too. Never guesses: a partial word
    that could mean several parts returns None (use search())."""
    t = _norm(text)
    if not t:
        return None
    if t in CATALOG:
        return CATALOG[t]
    hits = _candidates_for(t)
    if len(hits) == 1:
        return hits[0]
    if not hits:  # "the xiao s3 sense board" → drop filler words and try once more
        stripped = _norm(" ".join(w for w in _words(text) if w not in _FILLER))
        if stripped and stripped != t:
            hits = _candidates_for(stripped)
            if len(hits) == 1:
                return hits[0]
    code = _lipo_code(text)
    low = (text or "").lower()
    if code and (len(t) == 6 or any(w in low for w in ("lipo", "battery", "mah", "cell", "li-po", "lithium"))):
        return lipo_from_code(code)
    return None


_DESCRIPTORS = _FILLER | frozenset({
    "amp", "amplifier", "mic", "microphone", "speaker", "camera", "cam", "cell", "battery", "display",
    "screen", "driver", "motor", "servo", "switch", "button", "charger", "converter", "radio", "antenna",
    "holder", "pack", "unit", "part", "component", "for", "and", "of", "x", "pcs", "pack", "w", "with",
    "usb", "i2s", "i2c", "spi", "wifi", "ble", "bluetooth", "mems", "digital", "analog", "3v", "5v", "3.3v",
})


def find_loose(text: str) -> Component | None:
    """A parts-list name that contains one library identifier plus only descriptor words —
    "MAX98357A amp", "Pi Zero 2 W board" — resolves; "LED ring" (a real remainder) and names that
    could mean two parts do not. Still never guesses: the strict find() runs first."""
    hit = find(text)
    if hit is not None:
        return hit
    words = _words(text)
    if not words:
        return None
    n = len(words)
    taken: list[tuple[int, int, Component]] = []   # longest phrases first, no overlaps
    for size in range(n, 0, -1):
        for i in range(0, n - size + 1):
            j = i + size
            if any(not (j <= s or i >= e) for s, e, _ in taken):
                continue
            phrase = _norm(" ".join(words[i:j]))
            cands = [CATALOG[phrase]] if phrase in CATALOG else _candidates_for(phrase)
            if len(cands) == 1:
                taken.append((i, j, cands[0]))
    if not taken or len({c.key for _, _, c in taken}) != 1:
        return None
    covered = {k for s, e, _ in taken for k in range(s, e)}
    rest = [w for k, w in enumerate(words) if k not in covered and w not in _DESCRIPTORS]
    return taken[0][2] if not rest else None


def search(text: str, category: str | None = None) -> list[Component]:
    """Ranked partial matches over key, name, aliases, tags, notes."""
    t = _norm(text)
    if not t:
        return []
    words = [w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(w) > 1]
    scored: list[tuple[float, Component]] = []
    for c in CATALOG.values():
        if category and c.category != category:
            continue
        ident = " ".join([c.key, c.name, *c.aliases]).lower()
        hay = ident + " " + " ".join(c.tags) + " " + c.notes.lower()
        score = 0.0
        if t in _norm(c.key) or t in _norm(c.name) or any(t in _norm(a) for a in c.aliases):
            score += 5
        if any(t == _norm(a) for a in c.aliases) or t == _norm(c.key):
            score += 4
        score += sum(2.0 for w in words if w in ident)
        score += sum(0.5 for w in words if w in hay and w not in ident)
        if score > 0:
            scored.append((score + c.confidence, c))
    scored.sort(key=lambda s: (-s[0], s[1].key))
    return [c for _, c in scored]


_NEED_TAGS: dict[str, str] = {
    "vision": "vision", "camera": "vision", "see": "vision", "video": "vision", "night vision": "vision",
    "eyes": "vision", "eye": "vision", "watch": "vision", "look": "vision", "photo": "vision", "picture": "vision",
    "hearing": "hearing", "hear": "hearing", "ears": "hearing", "mic": "hearing", "microphone": "hearing",
    "listen": "hearing", "wake word": "hearing",
    "speaking": "speaking", "speech": "speaking", "speak": "speaking", "speaker": "speaking", "audio": "speaking",
    "talk": "speaking", "voice": "speaking", "sound": "speaking", "tts": "speaking",
    "battery": "power", "power": "power", "portable": "power", "batteries": "power", "lipo": "power",
    "charging": "charging", "charger": "charging", "charge": "charging", "charges": "charging",
    "rechargeable": "charging", "recharge": "charging", "usb-c charging": "charging", "usb-c": "charging",
    "usb c": "charging", "usb": "charging", "type-c": "charging",
    "compute": "compute", "brain": "compute", "mcu": "compute", "controller": "compute", "processor": "compute",
    "microcontroller": "compute", "esp32": "compute", "arduino": "compute", "raspberry pi": "compute",
    "wifi": "wireless", "wi-fi": "wireless", "bluetooth": "wireless", "wireless": "wireless", "radio": "wireless",
    "lora": "wireless", "ble": "wireless",
    "display": "display", "screen": "display", "oled": "display", "lcd": "display", "readout": "display",
    "motion": "motion", "servo": "motion", "motor": "motion", "move": "motion", "stepper": "motion",
    "fan": "motion", "vibrate": "motion", "haptic": "motion",
    "sensing": "sensing", "sensor": "sensing", "temperature": "sensing", "distance": "sensing",
    "humidity": "sensing", "pressure": "sensing", "gps": "sensing", "location": "sensing", "imu": "sensing",
    "thermal": "sensing", "gas": "sensing", "pir": "sensing", "presence": "sensing", "rfid": "sensing",
    "clock": "sensing", "time": "sensing",
    "storage": "storage", "sd card": "storage", "microsd": "storage", "recording": "storage", "record": "storage",
    "lighting": "lighting", "led": "lighting", "light": "lighting", "lights": "lighting", "neopixel": "lighting",
    "glow": "lighting",
    "input": "input", "button": "input", "knob": "input", "switch": "input", "buttons": "input", "dial": "input",
}
_NEED_KEYS = sorted(_NEED_TAGS, key=len, reverse=True)

# Parts a knowledgeable friend reaches for first — a small bonus in kit_for's ranking.
_POPULAR = frozenset({
    "xiao_esp32s3_sense", "esp32_cam", "esp32_devkitc", "pi_pico_w", "pi_zero_2w", "arduino_uno",
    "inmp441", "max98357a", "speaker_28mm", "tp4056_usb_c", "lipo_500mah_adafruit", "cell_18650",
    "holder_18650_1", "oled_096_ssd1306", "servo_sg90", "hc_sr04", "dht22", "bme280_gy", "led_5mm",
    "ws2812_ring_12", "switch_ss12d00", "tactile_6x6", "microsd_module", "pi_camera_v3", "ir_led_850nm",
    "nrf24l01", "neo6m_gy", "mpu6050_gy521", "buzzer_12mm",
})


# A specific need word names the parts a friend would reach for first (boosted in kit_for).
_NEED_PREFER: dict[str, tuple[str, ...]] = {
    "camera": ("pi_camera_v3", "xiao_esp32s3_sense", "esp32_cam", "pi_camera_v2"),
    "vision": ("xiao_esp32s3_sense", "pi_camera_v3", "esp32_cam", "pi_camera_v2"),
    "night vision": ("esp32_cam", "ov2640_night_vision", "ir_led_850nm", "pi_camera_v3"),
    "thermal": ("mlx90640_adafruit",),
    "mic": ("inmp441", "xiao_esp32s3_sense"), "microphone": ("inmp441", "xiao_esp32s3_sense"),
    "hearing": ("inmp441", "xiao_esp32s3_sense"), "listen": ("inmp441", "xiao_esp32s3_sense"),
    "wake word": ("inmp441", "xiao_esp32s3_sense"), "hear": ("inmp441", "xiao_esp32s3_sense"),
    "ears": ("inmp441", "xiao_esp32s3_sense"),
    "speaker": ("speaker_28mm", "max98357a", "speaker_40mm"), "speaking": ("max98357a", "speaker_28mm"),
    "speech": ("max98357a", "speaker_28mm"), "speak": ("max98357a", "speaker_28mm"),
    "voice": ("max98357a", "speaker_28mm"), "talk": ("max98357a", "speaker_28mm"),
    "audio": ("max98357a", "speaker_28mm"), "sound": ("max98357a", "speaker_28mm"), "tts": ("max98357a", "speaker_28mm"),
    "battery": ("lipo_500mah_adafruit", "cell_18650", "holder_18650_1"), "lipo": ("lipo_500mah_adafruit",),
    "portable": ("lipo_500mah_adafruit", "cell_18650"), "power": ("lipo_500mah_adafruit", "cell_18650"),
    "charging": ("tp4056_usb_c",), "charger": ("tp4056_usb_c",), "charge": ("tp4056_usb_c",),
    "charges": ("tp4056_usb_c",), "usb-c": ("tp4056_usb_c", "usb_c_breakout"), "usb c": ("tp4056_usb_c",),
    "usb": ("tp4056_usb_c",), "type-c": ("tp4056_usb_c",), "rechargeable": ("tp4056_usb_c",),
    "esp32": ("esp32_devkitc", "xiao_esp32s3", "esp32_s3_devkitc"), "arduino": ("arduino_uno", "arduino_nano"),
    "raspberry pi": ("pi_zero_2w", "pi_4", "pi_5"), "brain": ("xiao_esp32s3", "esp32_devkitc", "pi_pico_w"),
    "compute": ("xiao_esp32s3", "esp32_devkitc", "pi_pico_w"), "microcontroller": ("esp32_devkitc", "xiao_esp32s3", "pi_pico_w"),
    "mcu": ("esp32_devkitc", "xiao_esp32s3", "pi_pico_w"), "controller": ("esp32_devkitc", "xiao_esp32s3"),
    "processor": ("pi_zero_2w", "pi_5"),
    "wifi": ("xiao_esp32c3", "esp32_devkitc", "pi_pico_w"), "wi-fi": ("xiao_esp32c3", "esp32_devkitc", "pi_pico_w"),
    "bluetooth": ("xiao_esp32c3", "hc05"), "ble": ("xiao_esp32c3", "esp32_devkitc"),
    "lora": ("lora_ra02", "rfm95w_adafruit"), "radio": ("nrf24l01", "lora_ra02"), "wireless": ("xiao_esp32c3", "nrf24l01"),
    "screen": ("oled_096_ssd1306", "st7789_154_adafruit", "lcd_1602"), "display": ("oled_096_ssd1306", "lcd_1602"),
    "oled": ("oled_096_ssd1306", "oled_13_sh1106"), "lcd": ("lcd_1602", "lcd_2004"), "readout": ("oled_096_ssd1306", "tm1637_4digit"),
    "servo": ("servo_sg90", "servo_mg996r"), "stepper": ("nema17", "stepper_28byj48"), "motor": ("servo_sg90", "motor_tt_gear"),
    "fan": ("fan_30mm", "fan_40mm"), "vibrate": ("vibration_motor_10mm",), "haptic": ("vibration_motor_10mm",),
    "move": ("servo_sg90",), "motion": ("servo_sg90", "mpu6050_gy521"),
    "temperature": ("dht22", "bme280_gy", "ds18b20_probe"), "humidity": ("dht22", "bme280_gy"),
    "pressure": ("bme280_gy", "bmp280_gy"), "distance": ("hc_sr04", "vl53l0x_gy530"), "imu": ("mpu6050_gy521",),
    "gps": ("neo6m_gy", "gps_antenna_25"), "location": ("neo6m_gy", "gps_antenna_25"), "gas": ("mq2_module",),
    "pir": ("hc_sr501",), "presence": ("hc_sr501",), "rfid": ("rc522",), "clock": ("ds3231_zs042",), "time": ("ds3231_zs042",),
    "sensor": ("dht22", "bme280_gy", "hc_sr04"), "sensing": ("dht22", "bme280_gy", "hc_sr04"),
    "sd card": ("microsd_module",), "microsd": ("microsd_module",), "storage": ("microsd_module", "xiao_esp32s3_sense"),
    "recording": ("microsd_module", "xiao_esp32s3_sense"), "record": ("microsd_module",),
    "led": ("led_5mm", "ws2812_ring_12"), "light": ("led_5mm", "ws2812_ring_12"), "lights": ("ws2812_ring_12", "ws2812_stick_8"),
    "neopixel": ("ws2812_ring_12", "ws2812_stick_8"), "glow": ("ws2812_ring_12",), "lighting": ("led_5mm", "ws2812_ring_12"),
    "button": ("tactile_6x6", "push_button_12mm"), "buttons": ("tactile_6x6", "tactile_12x12"),
    "knob": ("encoder_ky040", "pot_10k"), "dial": ("pot_10k", "encoder_ky040"), "switch": ("switch_ss12d00", "switch_kcd1"),
    "input": ("tactile_6x6", "switch_ss12d00"),
}


def _need_match(need: str) -> tuple[str, str] | None:
    """(matched need key, role tag) for one need word or phrase — whole-word, longest phrase
    first ("night vision" before "vision"; "microcontroller" never reads as "mic")."""
    n = " ".join(str(need or "").lower().replace("_", " ").split())
    if not n:
        return None
    if n in _NEED_TAGS:
        return n, _NEED_TAGS[n]
    for k in _NEED_KEYS:
        if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", n):
            return k, _NEED_TAGS[k]
    return None


def need_tag(need: str) -> str | None:
    """One need word or phrase → the role tag (see _need_match)."""
    m = _need_match(need)
    return m[1] if m else None


def need_phrases(text: str) -> list[str]:
    """Every need phrase in a sentence, longest first, each stretch of text claimed once:
    "speech that runs on a battery" → ["speech", "battery"]; "night vision" stays one phrase."""
    n = " ".join(str(text or "").lower().replace("_", " ").split())
    if not n:
        return []
    taken: list[tuple[int, int, str]] = []
    for k in _NEED_KEYS:
        for m in re.finditer(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", n):
            if any(not (m.end() <= s or m.start() >= e) for s, e, _ in taken):
                continue
            taken.append((m.start(), m.end(), k))
    taken.sort()
    return [k for _, _, k in taken]


def _rank_key(c: Component, tag: str) -> tuple:
    """Best first: sure numbers, the parts a friend reaches for, integrated modules (more roles in
    one part); a part whose FIRST tag isn't this role (an IR LED asked for as "vision") drops back."""
    integrated = len([t for t in c.tags if t in TAGS])
    score = c.confidence + (0.15 if c.key in _POPULAR else 0.0) + 0.1 * min(integrated - 1, 2)
    if c.tags and c.tags[0] != tag:
        score -= 0.25
    return (-round(score, 3), c.category != "mcu", c.name)


def kit_for(needs: list[str]) -> dict[str, list]:
    """Need words → role tag → ranked candidates (best first: the parts a specific need word names,
    then datasheet-confidence, popular parts, integrated modules). Words the library can't map land
    as plain strings under "unknown"."""
    roles: dict[str, set[str]] = {}          # tag → keys a specific need word prefers
    order: list[str] = []
    unknown: list[str] = []
    for need in needs or []:
        n = " ".join(str(need or "").lower().split())
        if not n:
            continue
        m = _need_match(n)
        if m is None:
            unknown.append(n)
            continue
        key, tag = m
        if tag not in roles:
            roles[tag] = set()
            order.append(tag)
        roles[tag].update(_NEED_PREFER.get(key, ()))
    out: dict[str, list] = {}
    for tag in order:
        prefer = roles[tag]
        cands = [c for c in CATALOG.values() if tag in c.tags]
        cands.sort(key=lambda c: (c.key not in prefer, _rank_key(c, tag)))
        out[tag] = cands
    if unknown:
        out["unknown"] = unknown
    return out


def format_dims(c: Component) -> str:
    """"21 × 17.8 × 15 mm" — the way the model and the user should read a size."""
    return f"{c.length:g} × {c.width:g} × {c.height:g} mm"
