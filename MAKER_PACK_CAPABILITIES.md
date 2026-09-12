# MAKER_PACK_CAPABILITIES

**What the CAD / enclosure / camera / printing stack does today — written so the Phase 3 decision about
its future is made with the facts in hand rather than from memory.**

```
Written 2026-09-07 · against HELIX v3.0.0 · branch: mark1
Status: inventory only. This document decides nothing.
```

> **Why this exists.** `HELIX_MARK1_PLAN.md` parks the maker pack and schedules a decision about it after
> Phase 3. A decision like that goes wrong in one specific way: someone remembers the maker flow as "the
> 3D printing thing", concludes it is unrelated to the fleet, and deletes a component library that four
> other subsystems import. This document is the antidote. It is an inventory, not an argument.
>
> Sources: `READ_ME/MAKER_FLOW.md` §2–§10, `README.md` Part II, and the file tree. Sizes are **bytes on
> disk** as of this date, not line counts — nothing here was executed this session, so no timing or test
> result below is a fresh measurement. Where MAKER_FLOW records a measured number, it is cited as its
> claim, not re-verified.

---

## 1. What it actually does

Five capabilities, in the order a real job runs through them.

### 1.1 Pick real parts

`suggest_components` reads a catalog of **132 real components** with length × width × height, ports,
apertures, an Amazon search phrase, and **a confidence score per entry**. 55 entries are at confidence
≥ 0.85. Mounting holes are present on **17 boards only** — the ones where a manufacturer drawing gave
real positions (Arduino Uno / R4 / Mega / Nano, Pi 4 / 5 / Zero 2 W / Pico / Pico W, Pi Camera v2 / v3,
1602 and 2004 LCDs, NEMA 17, 28BYJ-48, 30 and 40 mm fans).

Everything else gets a pocket and no holes, on purpose, and says so in its note. The governing rule is
worth quoting because it is the reason the whole subsystem is trustworthy: **a wrong hole is worse than
a pocket.**

Resolution order when a part is named: catalog → loose name → LiPo cell code → ad-hoc dimensions the
user gave. A part that cannot be resolved is reported as unknown rather than guessed.

### 1.2 Generate the box

`design_enclosure` is **deterministic Python** — no model call, no coder run. `plan_layout` packs the
parts with rotation and clearance; the output is a two-half shell with a lip ring and rebate, screw
towers sized for heat-set inserts, a wire trench, standoffs **only** for verified holes, debossed
labels, and real cut geometry per part: lens bores, mic holes, speaker grilles, LED and screen windows,
switch slots, port slots.

Wall hints put a part against its wall and open every port facing it. Plate hints cut its bore through
the front. `on_lid` parts sit on the lid's inner face, clear of the lip band.

`validate` returns plain lines: overlaps, out-of-cavity parts, off-wall apertures, bed violations, thin
walls.

**No dimension is ever typed from memory.** Every number comes from the catalog, a listing, or the
camera's ruler. That constraint is pinned by a test on the persona prompt itself
(`tests/test_prompts.py`), not just by convention.

### 1.3 Check the fit before printing

`check_fit` projects the hologram over the live camera view at **1:1 true scale**, with a labelled ghost
rectangle per component on the cavity floor, lens and mic marks, wall apertures and screw rings. You lay
the real parts inside their outlines before anything prints.

Scale comes from a two-click calibration against a known length — credit-card long or short edge, a
printable HELIX marker, a US quarter, an AA cell, or a typed millimetre value — stored against the
tracker's base frame so it survives camera drift (MAKER_FLOW records ±1 % over a 10 % zoom swing in the
test harness). **Uncalibrated measuring is refused**, never shown in pixels.

`camera_measure` is the same machinery pointed at a real object: measure a part you are holding, in real
millimetres, and the measurement becomes a catalog-grade number.

### 1.4 Compile and view

A hologram is a **program**, not a mesh: `model.py`, Python on the build123d B-rep kernel, in
millimetres, with a `# --- Parameters ---` block carrying `[min..max..step]` ranges. "Make it wider" is
an edit to a named parameter, not a regeneration.

build123d drags in the OCCT kernel (~2 s import, heavy resident memory), so **the app process never
imports it**. `helix/cad/runner.py` is spawned as a subprocess and is the only importer in the codebase.
One run writes the whole artifact set: STL, STEP (what Bambu Studio slices natively), per-part 3MF, the
critic's preview PNG, and a meta report with bounding box, volume and PLA grams. A resident worker
serves the Studio's slider recompiles in about 0.6 s.

`cadpy.inspect_source` is a safety gate as well as a linter: import allowlist, no I/O builtins, no
dunders, no top-level geometry. A design file executes, so it is treated as code that executes.

**Loaded meshes** (added 2026-09-06) is a fourth way a hologram exists: `load_hologram_parts` takes
someone else's STL files — files, a folder, a glob, or a release zip — measures each off its vertices,
and writes an ordinary `model.py` with a `PARTS` table and one `scale` parameter. Sets that overflow one
P1S plate are shelf-packed onto plates. STEP is skipped with a note (a triangulation has no B-rep), the
3MF is written per part so one non-manifold file from the wild cannot cost the set its export, and steep
faces are reported as `SUPPORTS` lines rather than coder errors, since no coder pass can re-author a
loaded file.

### 1.5 Print

`print_hologram` sends a finished hologram to a Bambu Lab P1S over the LAN with a print sheet;
`printer_status` reads the printer.

---

## 2. The surface, by the numbers

### 2.1 Tools — 14 of the 80

| Tool | Fenced? | Group |
|---|---|---|
| `suggest_components` | read | parts |
| `show_parts` | read | parts |
| `save_parts` · `remove_parts` | **F** | parts |
| `design_enclosure` | **F** | enclosure |
| `check_fit` | **F** | AR |
| `camera_measure` | **F** | AR |
| `project_hologram` | **F** | AR (also vision) |
| `build_3d_model` | **F** | holograms |
| `load_hologram_parts` | **F** | holograms |
| `file_hologram` | **F** | holograms |
| `install_cad_engine` | **F** | holograms |
| `print_hologram` | **F** | printing |
| `printer_status` | read | printing |

Three readable, eleven fenced into `BUILD_TOOLS`.

### 2.2 Python

| File | Bytes | What |
|---|---:|---|
| `helix/domain/components.py` | 110,268 | the 132-part catalog |
| `helix/services/model_baker.py` | 80,363 | the hologram compile / critique / repair loop |
| `helix/domain/enclosure.py` | 76,530 | the deterministic generator |
| `helix/services/maker.py` | 58,484 | `MakerService` — the brain of the flow |
| `helix/domain/cadpy.py` | 55,694 | the hologram language + `helix_parts` |
| `helix/cad/runner.py` | 24,640 | the compile worker subprocess |
| `helix/services/components.py` | 21,563 | row resolution |
| `helix/services/parts.py` | 21,105 | parts lists / BOM |
| `helix/adapters/build123d_cad.py` | 19,294 | the CAD adapter |
| `helix/services/render_kit.py` | 16,912 | rendering |
| `helix/adapters/bambu_printer.py` | 11,323 | the P1S |
| `helix/domain/meshes.py` | 9,333 | plate packing |
| `helix/services/stl_measure.py` | 8,272 | measuring a mesh |
| `helix/adapters/blockade_skybox.py` | 7,332 | skybox (opt-in, keyed) |
| `helix/adapters/tripo3d.py` | 6,336 | neural reference models (opt-in, keyed) |
| `helix/ports/cad.py` | 3,901 | the `CadEngine` contract |
| | **≈ 531 KB** | |

### 2.3 Tests

Sixteen test files are maker-owned or maker-dominated, **≈ 254 KB** on disk:

`test_model_baker` · `test_maker` · `test_enclosure` · `test_enclosure_compile` · `test_camera_measure` ·
`test_load_parts` · `test_components` · `test_cadpy` · `test_parts` · `test_render_kit` ·
`test_mesh_compile` · `test_print_analysis` · `test_meshes` · `test_printer_tools` · `test_tripo3d` ·
`test_blockade`

MAKER_FLOW records the counts of several: 58 in `test_components` (including per-entry catalog
invariants), 29 in `test_maker`, 23 in `test_camera_measure`, 16 pure packer tests, and
`test_enclosure_compile` running the real kernel in about 45 seconds.

**That 45-second compile test is a fact worth holding onto.** It is one of the few tests in this repo
that exercises a real external kernel, and the nightly dream's merge gate runs the full suite. Whatever
happens to the maker pack, that gate's runtime changes with it.

### 2.4 Frontend

| File | Bytes | Shared with vision? |
|---|---:|---|
| `web/src/components/CameraDock.tsx` | 46,605 | **yes** — the camera panel itself |
| `web/src/pages/Studio.tsx` | 23,064 | no |
| `web/src/lib/track.ts` | 15,741 | **yes** |
| `web/src/components/ArHologram.tsx` | 12,878 | no |
| `web/src/lib/overlay.ts` | 10,627 | **yes** |
| `web/src/lib/measure.ts` | 10,045 | no |
| `web/src/pages/Viewer.tsx` | 3,772 | partly |
| `web/src/lib/stl.ts` | 1,448 | no |

---

## 3. What it is coupled to — read this before deciding anything

The maker pack is **not** a clean island. Five couplings, in descending order of how expensive they make
removal:

### 3.1 Holograms need the component library

`domain/cadpy.py`'s `render_boards()` writes the `helix_parts` BOARDS block **from
`domain/components.py`**. That is why "a case for an Arduino Uno" comes out fitting. Delete the catalog
and every authored hologram loses its hardware knowledge, including holograms that have nothing to do
with the enclosure generator.

### 3.2 The AR stack is half vision

`check_fit`, `camera_measure` and `project_hologram` are maker capabilities running on the vision pack's
camera panel, tracker and overlay code. `CameraDock.tsx` (46 KB) and `track.ts` (16 KB) serve both.
**Cutting maker does not remove them; cutting vision breaks maker.** Any pack boundary drawn between
these two has to be drawn deliberately, and `HELIX_MARK1_PLAN.md` §4.5's "no tool is claimed by two
packs" test will force that decision rather than let it drift.

### 3.3 `build_3d_model` is a Forge kind

Hologram is one of the five creation kinds, and `model` is a **persisted `kind` string** —
`README.md` is explicit that persisted kind strings never change. Existing hologram builds on disk
carry it. Removing the kind is a data-migration question, not a code question.

### 3.4 The frozen build knows about it

`build.py` names lazily-imported packages explicitly, because PyInstaller's static scan is trusted for
module-scope imports only, and a missing entry **fails at runtime in the frozen app, not at build time.**
Any change to what the maker pack imports is a change to `build.py`, and the failure mode is silent
until someone runs the packaged app and asks for a hologram.

### 3.5 It is the reason a rule exists

The maker flow is where "no dimension from memory" was learned, and the persona paragraph enforcing it is
pinned by `tests/test_prompts.py`. That discipline is now general to HELIX. Removing the flow that taught
it should not remove the rule.

---

## 4. Known limits, as the record states them

Carried forward from `READ_ME/MAKER_FLOW.md` §9–§10 so the decision is not made against a rosier picture
than the one the authors wrote down:

- **Slide lids are not generated.** Recorded as a known problem; such a request is built as a screw lid.
- **Wall-opening centre heights are estimates.** The schema has no connector height. Every affected entry
  carries `note="centre height estimated…"` — honest, but it is an estimate in a subsystem whose whole
  claim is that it does not estimate.
- **Packing is a greedy heuristic**, roughly 70 % fill. Deterministic and valid, not optimal.
- **Labels need a font OCCT can find** (Arial, on this machine) or they degrade to a tag.
- **Lens and mic positions on the XIAO Sense and ESP32-CAM come from photos**, ±2 mm.
- **`design_enclosure` compiles on the turn's worker** (15–20 s), not through the build queue. No coder
  runs, so there is little to narrate — but the orb waits for the kernel. This is the one place in the
  codebase that bends "nothing blocks the orb".
- **The backend cannot tell whether the camera panel is calibrated**, so `check_fit`'s reply teaches the
  calibration card rather than claiming a scale.
- **The ghost frame assumes the body is the first (leftmost) part** of a multi-part STL, centred in y.
  True for the generator's own output; not guaranteed for anything else.
- **The print sheet's per-part sizes are computed from the shell recipe** and labelled "planned"; only
  the overall size is measured off the mesh.

---

## 5. The options, stated neutrally

Not a recommendation. The plan says this is decided after Phase 3, and it should be.

| Option | What it means | Cost | What it buys |
|---|---|---|---|
| **A — Park it** (current) | `pack_maker` off by default on DESKTOP, never in `CLOUD_TOOLS`. Code stays, tests stay, nothing is deleted. | Test-suite runtime, including the 45 s kernel compile, on every dream merge gate. Maintenance drag when a refactor crosses it. | Zero risk. Reversible in one setting. |
| **B — Park it and skip its slow tests** | As A, plus a marker so the kernel-compile tests run nightly rather than on every gate. | The gate no longer proves the maker pack still compiles. | Faster self-modification cycle, which is the thing the fleet work depends on. |
| **C — Extract it** | Move the pack to its own repo, consumed as a dependency. | Real work: §3.1's catalog coupling and §3.2's shared AR code both have to be cut properly. | Core HELIX gets materially smaller. The pack survives intact. |
| **D — Remove it** | Delete the code, the tests, the tools, the kind. | §3.3's persisted `kind` migration; §3.4's build changes; a real loss of a working, tested capability with no equivalent elsewhere. | ~785 KB of source and tests off the maintenance surface. |

Two observations that will still be true when the decision comes:

1. **B is available immediately and costs almost nothing.** If the maker pack's real cost to the fleet
   work is dream-gate latency, B addresses that without touching a single capability. It is worth doing
   before Phase 1 if the gate turns out to be slow in practice.
2. **The difference between C and D is whether the capability should continue to exist at all**, not
   whether HELIX should carry it. That is a question about the next two years of the shop, and it is not
   a question the fleet work needs answered.

---

## 6. What to check before deciding

Four things nobody can answer from the source, and all four change the answer:

1. **Has the maker flow been used since 2026-09-06?** MAKER_FLOW's worked examples (IronEye, the InMoov
   right hand) are the last recorded runs. A pack used monthly and a pack used once are different objects.
2. **Is the P1S still on the LAN?** A printer adapter with no printer is dead weight in every option.
3. **How long does the full suite actually take**, and what fraction is the kernel compile? That single
   number decides whether option B matters.
4. **Does anything in the shop depend on a printed part** whose source hologram lives only in HELIX's
   `data/builds/`? `data/` is gitignored and never bundled — the design files for real parts may exist in
   exactly one place, on one machine.
