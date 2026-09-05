# The Maker Flow — from "I want a thing" to a printed part, with the components figured out

Status: SPEC + build contracts (2026-09-04). Four workstreams build to this document at once; the
contracts below are binding so the pieces meet. When the build lands, this file is updated to describe
what exists (keep the contract sections; add a "What shipped" section).

## 0. Why

Brian's IronEye session (Sept 4) showed the shape of the problem. He wanted a hat camera with vision,
hearing, and speech, printed on his Bambu P1S. What actually happened over four hours:

- HELIX guessed component dimensions (`XIAO_W, XIAO_H = 22, 18`, `ESP_W, ESP_H = 52, 29`) and baked
  them into `model.py` as constants. The pockets were sized from memory, not from the parts.
- Choosing components (which mic, which amp, a night-vision camera that doesn't exist as one board)
  took dozens of turns and screenshots; the BOM was re-derived from chat memory every time.
- Fit was never checked against anything real before printing. The AR panel can project a hologram
  over the camera view, but its scale is whatever the user drags it to, so it can't answer "will
  the XIAO fit in that pocket".
- Every enclosure was a fresh 15-minute LLM CAD run whose quality varied.

The maker flow makes this: **describe the thing → HELIX proposes the components (real parts, real
sizes, live prices) → the enclosure is generated from the parts list with correct pockets, standoffs,
ports and apertures → the design is checked at true scale over the camera, next to the real parts →
print.** Designing to print becomes easy because the numbers come from a library and a ruler, not from
memory.

## 1. The four workstreams (phase 1 runs A, B, C in parallel; D follows)

| Workstream | Owns (may create/modify ONLY these) | Must not touch |
|---|---|---|
| **A — Components** | `helix/domain/components.py` (fill the catalog; keep the schema), NEW `helix/services/components.py`, `helix/services/parts.py` (add the fields in §3), NEW `tests/test_components.py`, `tests/test_parts.py` (extend) | cadpy.py, enclosure.py, web/, tools.py, prompts.py, shell.py, server.py |
| **B — Enclosure** | NEW `helix/domain/enclosure.py`, `helix/domain/cadpy.py` (HELIX_LIB + HELIX_LIB_DOC + a `render_boards()`), NEW `tests/test_enclosure.py`, NEW `tests/test_enclosure_compile.py`, `tests/test_cadpy.py` (extend) | components.py schema (read only), web/, tools.py, prompts.py, shell.py, server.py, parts.py |
| **C — AR measure & true scale** | NEW `web/src/lib/measure.ts`, `web/src/components/CameraDock.tsx`, `web/src/components/ArHologram.tsx`, `web/src/lib/store.ts`, `web/src/lib/overlay.ts`, `helix/api/shell.py` (camera measure/holograms parts only), `helix/api/server.py` (camera routes only), `helix/services/camera.py`, NEW `tests/test_camera_measure.py`, `tests/test_camera_panel.py` (extend) | tools.py, prompts.py, container.py, Studio.tsx, cadpy.py, enclosure.py, components.py |
| **D — Brain** (after A, B, C) | `helix/services/tools.py`, `helix/services/prompts.py`, `helix/app/container.py`, `helix/services/conversation.py` (fence), NEW `helix/services/maker.py`, `web/src/pages/Studio.tsx`, `helix/api/server.py` (studio/hologram routes only), NEW `tests/test_maker.py`, `tests/test_prompts.py`, `READ_ME/ARCHITECTURE.md`, `READ_ME/README.md`, this file's "What shipped" | the files A/B/C own except where a contract says D wires them |

Shared rules for every workstream:
- Python 3.11, build123d 0.11.1 is installed in dev (`python -c "import build123d"`); tests run with
  `python -m pytest tests/<file> -q -p no:cacheprovider -W ignore`. The web builds with
  `cd web && npm run build` (tsc + vite; must pass clean).
- Never commit, never push, never touch `data/` or `%LOCALAPPDATA%\HELIX`, never run `build.py`, never
  launch or quit the live HELIX. Write files with the Write/Edit tools (this machine's Bash heredocs
  mangle backslash escapes).
- Every new behavior gets a test. Pure-Python tests are fast; kernel compile tests are allowed and
  expected for the enclosure generator (a few seconds each — keep them few and meaningful).
- Text that reaches the model or the user: plain, honest, no invented numbers. A dimension you are
  not sure of is marked (`confidence`, `approx`), never rounded into confidence.
- Nothing here can spend money or print without the user: `print_hologram` stays as it is (human
  go-ahead), the Amazon faculty stages and never buys, the camera opens only at the user's request.
  New tools that open the camera or write the user's parts lists/builds join `BUILD_TOOLS`.

## 2. Component library — the contract (`helix/domain/components.py`)

Pure data, no I/O. The schema (already stubbed; keep names exactly):

```python
@dataclass(frozen=True)
class Hole:      x: float; y: float; d: float = 3.2          # mm from the component's bottom-left, plan view
@dataclass(frozen=True)
class Port:      kind: str; side: str; x: float; width: float = 0.0; height: float = 0.0
    # kind: usb_c | micro_usb | usb_a | barrel_5_5 | jst_ph | jst_xh | sd | hdmi | audio_3_5 | header | antenna | other
    # side: front | back | left | right (looking down at the component, +y is "back")
    # x: offset of the port's centre along that side, mm from the side's left end viewed from outside
    # width/height: the opening the enclosure needs; 0 = the library default for that kind
@dataclass(frozen=True)
class Aperture:  kind: str; x: float; y: float; d: float = 0.0; w: float = 0.0; h: float = 0.0; face: str = "top"
    # kind: lens | mic | speaker | led | screen | button | sensor | vent | shaft | antenna
    # x, y from bottom-left in plan view; d for round, w×h for rectangular; face: which way it looks
@dataclass(frozen=True)
class Component:
    key: str; name: str; category: str
    length: float; width: float; height: float   # L(x) × W(y) × H(z) mm, lying flat; H = tallest point
    holes: tuple[Hole, ...] = (); ports: tuple[Port, ...] = (); apertures: tuple[Aperture, ...] = ()
    mount: str = "standoff"    # standoff | rails | pocket | clip | strap | adhesive
    clearance: float = 0.5     # extra mm per side the enclosure adds around it
    aliases: tuple[str, ...] = (); search: str = ""   # Amazon search phrase for this part
    source: str = "datasheet"  # datasheet | measured | community | derived
    confidence: float = 0.9    # 1.0 = official drawing; < 0.7 = leave 0.5 mm more room (approx)
    tags: tuple[str, ...] = () # functional: vision, hearing, speaking, compute, power, charging, sensing, display, motion, storage, wireless, lighting, input
    notes: str = ""
```

Categories: `mcu camera mic amp speaker battery charger power switch button display sensor motor driver
led connector storage comm misc`.

Functions (implement all; tests pin them):
- `CATALOG: dict[str, Component]` — **at least 90 real parts** covering the maker staples: ESP32
  DevKitC (V4, 30-pin and 38-pin), ESP32-S3 DevKitC, ESP32-CAM (AI-Thinker, incl. lens position and
  the MB programmer base), XIAO ESP32-S3 (and Sense), XIAO ESP32-C3, ESP8266 NodeMCU, Wemos D1 mini,
  Arduino Uno R3/R4, Nano, Nano ESP32, Mega, Pro Mini, Pi 4B, Pi 5, Pi Zero 2 W, Pi Pico/Pico W,
  Pi Camera Module v2/v3, Teensy 4.0, MAX98357A (Adafruit and the generic 4-pack), INMP441 (square
  and round), PAM8403, TP4056 (micro-USB and USB-C), MT3608, LM2596, 18650 holder (1 and 2 cell),
  LiPo cells by code, CR2032 holder, speakers (Ø20/28/36/40 round; 20×30, 30×40 rect), OLED 0.96"/1.3"
  SSD1306, 1.54"/2.0" ST7789, 16×2 LCD, 128×64 KS0108, MicroSD breakout, DHT22, BME280, BMP280,
  HC-SR04, HC-SR501 PIR, MPU6050, MLX90640 breakout, VL53L0X, SG90/MG90S/MG996R servos, NEMA 17,
  28BYJ-48 + ULN2003, L298N, A4988/TMC2209, WS2812 rings 8/12/16/24 and 8-stick, 5 mm/3 mm LEDs,
  slide switch SS12D00, rocker KCD1, tactile 6×6 and 12×12, latching push button 12 mm, rotary encoder
  KY-040, 10k pot, buzzer 12 mm, DS3231, RC522, NEO-6M/NEO-8M GPS, nRF24L01, SX1276 LoRa, HC-05,
  barrel jack 5.5×2.1, USB-C breakout, 4-ch relay (exists), JST-PH/XH connectors as ports, OV2640
  24-pin camera module (lens block), IR receiver TSOP38238, MQ-2 gas sensor, 7-segment TM1637,
  MAX7219 8×8. Dimensions come from datasheets / manufacturer pages verified by web search where they
  exist; community-measured parts carry `source="community"` and `confidence <= 0.7`. **Never invent
  hole positions**: when unsure, omit holes and set `mount="pocket"` (or `rails`) — a wrong hole is
  worse than a pocket. Record the search phrase that finds the part on Amazon.
- `find(text) -> Component | None` — key, alias, or name; case/space/hyphen-insensitive; a spoken
  variant ("xiao s3 sense", "esp32 cam", "max 98357") resolves. Returns None rather than guessing.
- `search(text, category=None) -> list[Component]` — ranked partial matches (name/aliases/tags/notes).
- `lipo_from_code(code) -> Component | None` — the LiPo size code TTWWLL (e.g. "603048" → 6.0 × 30 × 48
  mm): thickness, width, length in mm; component key `lipo_<code>`, category battery, mount pocket,
  ports one `jst_ph` on a short side, clearance 1.0, source derived, confidence 0.8; tolerate "3.7V
  603048 500mAh" style text.
- `adhoc(name, length, width, height, *, category="misc", mount="pocket", source="measured", confidence=0.7) -> Component`
  — a part the user measured or read off a listing.
- `kit_for(needs: list[str]) -> dict[str, list[Component]]` — need words ("vision", "camera", "hearing",
  "mic", "speaking", "speaker", "audio", "battery", "power", "charging", "compute", "brain", "wifi",
  "display", "screen", "motion", "sensing", "night vision") → role → ranked candidates from tags, best
  first (prefer datasheet-confidence, integrated modules, common parts). Unknown need words are
  returned under `"unknown"`.
- `to_json(c) -> dict` / `from_json(d) -> Component` — round-trip; `from_json` tolerates missing
  optional fields and rejects (ValueError) a missing key/dims.
- `dims_from_text(text) -> tuple[float, float, float] | None` — read "1.02 x 0.67 x 0.2 inches",
  "26 x 17 x 4.5 mm", "27mm×40.5mm" from an Amazon spec line to mm (sorted descending: L ≥ W ≥ H).

`helix/services/components.py` — `ComponentService(parts: PartsService, amazon=None)`:
- `resolve(name_or_key, *, dims=None) -> Component | None` — catalog first (find), then a LiPo code
  in the text, then ad-hoc when dims are given.
- `suggest(needs_text: str) -> str` — the model-facing text for "which components do I need": roles,
  2–3 candidates each with size and why, an honest line about what the library doesn't know. Never
  names a fenced tool (readable on autonomous runs).
- `resolve_parts(project) -> tuple[list[tuple[Part, Component]], list[Part]]` — every row of a
  parts list mapped to a Component (by `component` key, then by name, then LiPo code, then ad-hoc
  from the row's length/width/height), and the rows it could not resolve.
- `describe(c) -> str` — one line: name, L×W×H, mount, ports, apertures, confidence.

## 3. Parts list rows gain physical fields (`helix/services/parts.py`, workstream A)

Add to `Part` (all optional, JSON round-trip, `save()` accepts them, `show()` prints them when set):
`component: str = ""` (catalog key), `length: float | None = None`, `width: float | None = None`,
`height: float | None = None` (ad-hoc or measured mm), `face: str = ""` (which enclosure wall the
part's aperture/port should reach: front/back/left/right/top/bottom), `on_lid: bool = False`.
Add `PartsService.set_dims(project, part_name, length, width, height, *, source="measured") -> bool`.

## 4. Enclosure generator — the contract (`helix/domain/enclosure.py`, workstream B)

Pure Python that emits **model.py source** for the existing pipeline (compile/bake/studio/AR untouched):

```python
@dataclass(frozen=True)
class Item:        component: Component; qty: int = 1; label: str = ""; face: str = ""; on_lid: bool = False
@dataclass(frozen=True)
class EnclosureSpec:
    name: str; items: tuple[Item, ...]
    wall: float = 2.0; clearance: float = 0.6; corner_r: float = 3.0; floor: float = 2.0
    lid: str = "screw"        # screw | snap | slide
    mount: str = "none"       # none | wall_tabs | strap | din | flat_feet
    channel: float = 4.0      # wire trench width between pockets
    labels: bool = True       # deboss component labels beside pockets
    bed: tuple[float, float, float] = (256.0, 256.0, 256.0)
@dataclass(frozen=True)
class Placed:      key: str; label: str; x: float; y: float; w: float; h: float; rot: int; face: str; mount: str; on_lid: bool; z_top: float
@dataclass(frozen=True)
class Layout:      outer: tuple[float, float, float]; inner: tuple[float, float, float]; wall: float; floor: float; placed: tuple[Placed, ...]; apertures: tuple[dict, ...]; screws: tuple[dict, ...]; lid: str; problems: tuple[str, ...]

def plan_layout(spec: EnclosureSpec) -> Layout       # deterministic packing (see below); never raises for a fit problem — records it in .problems
def model_source(spec: EnclosureSpec, layout: Layout) -> str   # model.py text, helix_parts only, passes cadpy.inspect_source
def layout_json(layout: Layout) -> dict                         # §6 shape, written to assets/layout.json by the tool
def validate(spec: EnclosureSpec, layout: Layout) -> list[str]  # overlaps, out of cavity, aperture off its wall, bed, thin walls
def calibration_marker_source() -> str                          # the printable HELIX AR marker (§5)
```

Packing: a rectangle packer with rotation (0/90), each footprint grown by `component.clearance +
spec.clearance` per side (approx parts get +0.5), a `channel`-wide trench between neighbours, boards
with holes on standoffs (their height from `component.height` + 3 mm air), hole-less boards in
rib-walled pockets (`pocket_for`), loose parts (speaker, battery, switch) in pockets or bays.
Items with a `face` hint are placed against that wall so their port/aperture reaches it; a camera lens
or speaker with `face="front"` on a face-down front shell gets its bore/grille through the plate face.
`on_lid` items go on the lid's inner face. Inner height = tallest standing part + air + lid features.
The shell follows the existing two-half rules (lip_ring + lip_rebate, screw towers with the tower
formula, mirrored mating written out), prints face-down, and every part passes the runner's checks
(no FLOATING, no TOO BIG, OVERHANG below the warning line). Parameter block: `wall`, `clearance`,
`corner_r`, `lid_style`, `label_deep`, and one `<label>_extra` slack per pocket so the studio's
sliders can loosen a pocket without re-planning. The docstring brief lists Parts and Assembly the
way the coder prompt requires; a `# --- Layout ---` comment table (label, x, y, w, h, face) lets the
LLM edit path see the plan.

`helix_parts` additions (embedded in `cadpy.HELIX_LIB`, documented in `HELIX_LIB_DOC`, each with a
pure test and a compile test): `pocket(l, w, h, rib=1.6, clear=FIT)`, `pocket_for(comp_key_or_dims)`,
`lens_bore(d, depth)`, `grille(d, hole=1.6, pitch=2.8)` (hex-pattern speaker grille cutter), `mic_hole(d=1.5)`,
`screen_window(w, h, r=1)`, `switch_slot(kind)` (`ss12d00` slide 8.5×3.6, `kcd1` rocker 13.5×19.2,
`tact_6` Ø6.5, `push_12` Ø12.4, `ky040` Ø7.2), `battery_bay(l, w, h)`, `wire_notch(w, depth)`,
`led_window(d)`, `deboss_text(text, size, depth)` (build123d `Text`; if the font is unavailable in the
worker it degrades to a shallow rectangular tag, never an error), plus `render_boards(catalog)` in
cadpy that writes the `BOARDS` block from `components.CATALOG` so `board("xiao_esp32s3_sense")` works
in model.py — the catalog is the single source of truth (keep the existing keys working).

The HELIX AR marker: an 80 × 80 × 3 mm plate; a 2 mm debossed border square exactly 80 mm outside; four
bold corner squares and a centre ring in a pattern the human eye and a webcam read at a glance; the
text "HELIX 80 mm" debossed; prints flat, no supports. Its outer square is the scale reference.

## 5. AR measure & true scale — the contract (workstream C)

The camera panel gains a **Measure** mode and the hologram layer gains **true scale** and **component
ghosts**. Pure geometry lives in `web/src/lib/measure.ts` (unit-tested logic in TS is not required,
but keep it pure and small).

- **Calibration**: the user sets a scale from something of known size in the same plane as the parts:
  two clicks across a known length. Presets in a small picker: credit card long edge **85.60 mm** (ISO/IEC
  7810 ID-1), card short edge 53.98, HELIX marker 80.0, US quarter 24.26, AA cell length 50.5, and
  a typed custom mm. Result: `mmPerPx` at the BASE tracker frame (store it with the tracker snapshot
  so it survives camera drift through `relative()`), shown in the panel ("0.19 mm/px · calibrated on a
  card"). Uncalibrated measurements are refused with a one-line hint, never shown in pixels.
- **Measuring**: drag = a distance (mm, 1 decimal); shift-drag = a box (w × h mm). Each measurement
  gets a label (editable, defaults "part 1", "part 2"…) and lives in a list in the panel with ✕. A
  **ruler** toggle draws a 10 mm grid over the calibrated plane. All of it rides the tracker.
- **Send to HELIX**: `POST /api/camera/{cam_id}/measure` with
  `{"mm_per_px": 0.19, "reference": "card long edge", "items": [{"kind": "box", "label": "XIAO", "w_mm": 21.1, "h_mm": 17.6}, {"kind": "distance", "label": "hole pitch", "mm": 15.2}]}`.
  The shell (`camera_measured`) turns it into one plain line — `Measured: XIAO 21.1 × 17.6 mm; hole
  pitch 15.2 mm (0.19 mm/px, card long edge)` — and (a) settles a parked `CameraCommand("measure")`
  with that line when a tool is waiting, else (b) posts it as a system bubble and submits it as a
  turn so HELIX reacts ("that's the XIAO — saving 21.1 × 17.6 to the IronEye list").
- **The measure command**: `CameraCommandRequested` with command `"measure"` and payload
  `{"prompt": "<what to measure>"}` opens/raises the panel in Measure mode with the prompt shown (like a
  parked look); it is settled by the Send (above) or by ✕ (reply "The user cancelled the measurement.").
  Workstream D's `camera_measure` tool calls `_camera_command("measure", {...})` and waits up to 300 s.
- **True scale**: when calibrated, a projected hologram is placed at `size = 1 / (mmPerPx · frameWidth)`
  (1 mm on the plate = 1 mm on the desk) with a "1:1" badge; the user can still drag/scale, and a
  "↺ 1:1" button snaps back. Uncalibrated projection behaves as today.
- **Component ghosts**: `GET /api/camera/holograms` rows gain `"layout": <assets/layout.json or null>`
  and the `camera.hologram` event carries it; `ArHologram` draws each layout component as a labelled
  translucent rectangle on the floor plane in the hologram's frame (layout origin = outer bottom-left
  → STL centred coords: `x - L/2, y - W/2`), plus aperture marks, so the real parts can be laid inside
  their ghost pockets on the desk and compared. `project_hologram` gains nothing new at the tool level;
  the layout rides automatically when the hologram has one.

## 6. `assets/layout.json` — shared shape (B writes the dict, D writes the file, C reads it)

```json
{"units": "mm", "name": "IronEye", "outer": [115.0, 48.0, 35.0], "inner": [110.0, 43.0, 30.0],
 "wall": 2.5, "floor": 2.0, "lid": "screw",
 "components": [{"key": "xiao_esp32s3_sense", "label": "CAM", "x": 84.0, "y": 12.0, "w": 22.5, "h": 18.5, "rot": 0,
                 "face": "front", "mount": "pocket", "on_lid": false, "z_top": 9.0,
                 "apertures": [{"kind": "lens", "x": 93.0, "y": 21.0, "d": 8.0, "face": "front"}]}],
 "apertures": [{"face": "left", "kind": "usb_c", "x": 24.0, "z": 8.0, "w": 10.0, "h": 4.0, "for": "CHG"}],
 "screws": [{"x": 5.0, "y": 5.0, "size": "M2", "insert": 3.2}],
 "problems": []}
```
Coordinates: plan view, x right, y "back", origin at the enclosure's OUTER bottom-left corner, mm.

## 7. Tools and the flow — the contract (workstream D)

New tools (all in `ToolRegistry`, dispatch via a new `helix/services/maker.py` `MakerService` that
composes components + parts + enclosure + builds/baker/repo; wire in `container.py`):
- `suggest_components(project, needs)` — readable (not fenced): `ComponentService.suggest`, plus, when
  the user asked about buying, the model follows with `search_amazon`. Text ends with how to save them
  (`save_parts` with `component` keys) — but since this tool is readable, name no fenced tool in its
  text; the persona teaches the next step.
- `design_enclosure(project, lid?, mount?, wall?, name?)` — FENCED. Resolves the project's parts
  (unresolved rows are named back with what's missing — a size, a catalog match — and the tool stops
  if any NEEDED row is unresolved), plans the layout, generates model.py, creates or updates the MODEL
  build (`BuildService.create`/workspace, write `model.py` + `assets/layout.json`, `model_baker.prepare`
  + `bake`, `repo.commit_all`, publish `BuildCreated`/`BuildIterated` so the menu and studio refresh),
  runs `validate` + the baked meta's `print_warnings`, and returns a plain fit report: outer size, each
  component's pocket, apertures per wall, screws, PLA grams, problems. The design is a normal hologram
  afterwards: "make the wall 3 mm" goes through the existing edit path; the studio's sliders work.
- `check_fit(name)` — FENCED (opens the camera): opens the panel if needed and projects the named
  hologram with its layout (`_camera_command("hologram", {...})`); the reply tells the model whether
  the view is calibrated (the panel's measure state) and how to calibrate if not.
- `camera_measure(what)` — FENCED: `_camera_command("measure", {"prompt": what})`, waits (≤ 300 s) for
  the measured line, returns it. The persona then saves dims with `save_parts`/`set_dims`.
- `print_hologram` gains a **print sheet** in its reply and in the studio: settings for the P1S
  (0.2 mm layers, 3 walls, 15 % infill, no supports unless the meta says OVERHANG), the parts to
  print with sizes and PLA grams, the screws/inserts list from the layout, and the assembly order from
  the brief. `MakerService.print_sheet(slug) -> str`; the studio shows it in the Print panel.
- `build_3d_model`'s description gains one line: for an enclosure around known parts, prefer
  `design_enclosure` (deterministic, correct pockets) and use `build_3d_model` for everything else and
  for edits.

Persona (CONSOLE_SYSTEM, one new paragraph "THE MAKER FLOW"): when the user wants to build a device —
"a hat cam with vision and sound", "a sensor node for the garden" — run the flow: (1) suggest_components
and talk the choices through in one breath each (size, why, price if asked via search_amazon); (2)
save_parts with `component` keys and quantities, `face` hints for cameras/ports/speakers, `on_lid`
for batteries; (3) design_enclosure and read the fit report back briefly; (4) offer check_fit on the
camera — calibrate with a credit card once, lay the real parts inside the ghost pockets; parts still
unknown get camera_measure; (5) print_hologram on their go, with the print sheet. Never hand-type
dimensions you didn't read from the library, a listing, or the ruler.

Studio (`Studio.tsx`): a **Components & fit** panel when `layout.json` exists (rows: label, part,
pocket size, mount, wall/aperture; problems in amber), a "Check fit on camera" button (`POST
/api/camera/open` then `POST /api/holograms/{slug}/project` → shell projects with layout), and the print
sheet in the Print panel. Routes D adds: `GET /api/holograms/{slug}` payload gains `"layout"` and
`"print_sheet"`; `POST /api/holograms/{slug}/project`.

Fence: `design_enclosure`, `check_fit`, `camera_measure` join `BUILD_TOOLS`; `suggest_components` stays
readable and coaches no fenced tool.

## 8. Quality bars (reviewers hold the line on these)

1. **No invented dimensions.** Catalog entries cite a source kind and confidence; a reviewer audits a
   random 15 against the web. Unknown → omitted holes and a pocket, never a guess.
2. **Every generated enclosure compiles and passes the runner's print checks** for the three reference
   specs (an IronEye-class wearable: XIAO ESP32-S3 Sense + MAX98357A + Ø28 speaker + LiPo 603048 +
   TP4056 USB-C + SS12D00 slide switch; an ESP32 DevKitC + 2-ch relay box with a barrel jack; a Pi
   Zero 2 W + Camera v2 case with a lens bore), and the studio's sliders still work on the output.
3. **Measurements are real millimetres**: the calibration math is exact for the similarity model
   (scale changes with camera drift are compensated), refuses to answer uncalibrated, and the sent
   line reads plainly.
4. **The safety posture is unchanged**: fence membership, no new outbound hosts, readable texts name
   no fenced tool, the camera opens only on the user's request, nothing prints or buys by itself.
5. **The suite is green and the web builds clean**; nothing in the existing hologram/AR/printer flows
   regresses (the coder prompt's rules, the studio, the marker-less projection, the Bambu path).

## 9. What shipped (2026-09-04)

The four workstreams landed; the contracts above held (§2–§7 describe what exists; this section
says what is real, what was cut, and what to know before trusting a number).

**A — Components.** `helix/domain/components.py` carries 132 parts (55 at confidence ≥ 0.85; 17
with mounting holes read from manufacturer drawings — Arduino Uno/R4/Mega/Nano, Pi 4/5/Zero 2 W/
Pico/Pico W, Pi Camera v2/v3, 1602/2004 LCDs, NEMA 17, 28BYJ-48, 30/40 mm fans). Every unverified
hole pattern is omitted (`mount=pocket`/`rails`) and says so in its note. `find` / `find_loose` /
`search` / `lipo_from_code` / `adhoc` / `dims_from_text` / `kit_for` / `to_json` / `from_json` /
`format_dims` all exist and are pinned by `tests/test_components.py` (58 tests, incl. per-entry
catalog invariants). `helix/services/components.py` resolves rows (catalog → loose name → LiPo code
→ ad-hoc dims) and writes the readable brief. `Part` gained `component`, `length`, `width`,
`height`, `face`, `on_lid`; `PartsService.set_dims` exists; `save()` accepts the fields.
Known: lens/mic positions on the XIAO Sense and ESP32-CAM are from photos (±2 mm); Adafruit boards
whose pages give hole spacing but not positions carry no holes on purpose.

**B — Enclosure.** `helix/domain/enclosure.py` (`Item`, `EnclosureSpec`, `Placed`, `Layout`,
`plan_layout`, `model_source`, `layout_json`, `validate`, `calibration_marker_source`,
`print_origins`) and the `helix_parts` additions (`pocket`, `pocket_for`, `battery_bay`,
`lens_bore`, `grille`, `mic_hole`, `led_window`, `screen_window`, `switch_slot`, `port_slot`,
`wire_notch`, `deboss_text` → `deboss_tag` fallback) with `render_boards()` writing the BOARDS block
from the catalog. The three §8 reference specs and the marker compile with zero print warnings and
a measured assembled fit (`tests/test_enclosure_compile.py`, ~45 s); 16 pure tests pin the packer.
Two real fit flaws in the existing lip joint were fixed on the way (corner slivers; a flat over the
void). Reference sizes with the live catalog: IronEye 105 × 74 × 28.5 mm (4 × M3, strap tabs), relay
box 125 × 84 × 32.5 + DIN clip, Pi Zero cam 128 × 41 × 24 (wall tabs).
Cut / limits: **slide lids are not generated** (recorded as a problem; built as screw); wall-opening
centre heights are estimates (the schema has no connector height) and every such entry carries
`note="centre height estimated…"`; packing is a deterministic greedy heuristic (~70 % fill), valid and
repeatable but not optimal; labels need a font OCCT can find (Arial here) or degrade to a tag.

**C — AR measure & true scale.** The camera panel's Measure mode (📐), the reference picker (card
long/short edge, HELIX marker, US quarter, AA cell, custom mm), two-click calibration stored with the
tracker snapshot (drift-compensated: ±1 % over a 10 % zoom swing in the harness), drag/shift-drag
measurements with editable labels, a 10 mm grid, the refusal when uncalibrated, Send → `POST
/api/camera/{id}/measure` → the one plain `Measured: …` line (settles a parked `camera_measure` or
becomes a turn), `{"cancel": true}` → "The user cancelled the measurement.", a closed panel →
"The camera panel closed before a measurement was sent." True-scale projection (1:1 badge, ↺ 1:1) and
component ghosts (labelled amber rectangles on the cavity floor, top plane at `z_top`, lens/mic marks,
wall apertures, screw rings) from the `layout` that rides `GET /api/camera/holograms` and the
`camera.hologram` event. 23 tests in `tests/test_camera_measure.py`.
Known: the ghost frame assumes the body is the first (leftmost) part of a multi-part STL and centred
in y — true for the generator's output (base first); the shell cannot see whether the panel is
calibrated, so `check_fit`'s reply teaches the card instead of claiming a scale.

**D — Brain.** `helix/services/maker.py` (`MakerService`: `suggest`, `design_enclosure`,
`print_sheet`, `project` for `check_fit`, `measure` for `camera_measure`, `find_model`, `layout`),
wired in `helix/app/container.py` and offered by `ToolRegistry` as `suggest_components` (readable),
`design_enclosure`, `check_fit`, `camera_measure` (fenced — `BUILD_TOOLS`). `save_parts` learned the
physical fields (`component`, `length`, `width`, `height`, `face`, `on_lid`); `print_hologram`'s reply
and the studio's Print panel carry the print sheet; `build_3d_model` points enclosures around known
parts at `design_enclosure`. The persona's "THE MAKER FLOW" paragraph runs the five steps in order
and forbids hand-typed dimensions (`tests/test_prompts.py` pins it). The studio gained the
**Components & fit** panel and "Check fit on camera" (`POST /api/holograms/{slug}/project`, through
the shell's own camera command path); `GET /api/holograms/{slug}` carries `layout` and
`print_sheet`. Spoken phrases exist for all four tools. `tests/test_maker.py` (29 tests) pins the
files written, the events, the report, every refusal, the rollbacks (a new build deleted, an update
reset), the print sheet, the camera hand-offs, the registry, the fence and the routes.
Worked example (real kernel, scratch data dir): the IronEye list (XIAO ESP32-S3 Sense front, MAX98357A,
Ø28 speaker front, LiPo 603048 on the lid, TP4056 USB-C left, SS12D00 top, M2 screws) →
`design_enclosure(mount="hat")` → 16.7 s → "Designed 'IronEye enclosure': a two-half shell 105 × 74 ×
28.5 mm outside (101 × 70 × 24.5 inside), 2 mm walls, screw-down lid, two strap tabs on the lid",
six pockets with their cuts (lens bore Ø9 with a Ø12 recess and a Ø1.5 mic hole for the XIAO, a Ø26 hex
grille for the speaker), a 12 × 7 USB-C opening on the left wall (height flagged as estimated), an
8.5 × 3.6 switch slot on the top wall, 4 × M3x10 into M3 inserts, 240 × 74 × 26 mm on the plate, about
73 g of PLA solid, no print warnings, one honest problem (the XIAO's USB-C and SD stay inside the box
because its face is the front). A second call with `lid="snap"` updated the same hologram in 14.4 s
(three commits: scaffold, build, build).
Cut / limits: `design_enclosure` compiles on the turn's worker (15–20 s) rather than through the
build queue — no coder runs, so there is nothing to narrate beyond two progress lines, but the orb
waits for the kernel; the enclosure's name update keeps the hologram's existing name; a fit check
cannot confirm the panel's calibration from the backend; the print sheet's per-part sizes are
computed from the shell recipe (labelled "planned") while the overall size is measured off the mesh.
