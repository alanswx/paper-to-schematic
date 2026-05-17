---
name: schematic-graph
description: Load, edit, validate, render, and export a board's schematic graph. Use when adding components or nets, setting pin positions, running lint or ERC gates, generating probes.csv, rendering overlays, or exporting KiCad schematics.
---

# Schematic-graph skill

Owns the in-memory and on-disk representation of a board's transcribed schematic:
the graph of **components** (nodes) and **nets** (edges), plus the auxiliary
artifacts `probes.csv` (physical-board verification list) and `discrepancies.md`
(paper-vs-board diffs).

## When to use

- Adding, removing, or editing components in `boards/<id>/graph.json`.
- Adding wires / labels / sheet-zone refs / off-page connectors / buses /
  implicit-power edges between component pins.
- Validating a graph against the schema and ERC-style sanity checks.
- **Rendering an overlay of the current graph onto the source PNG —
  the primary visual checkpoint after every graphical edit.**
- Generating the ranked `probes.csv` from the current graph.
- Exporting a `.kicad_sch`.

## The render-overlay loop

After every graphical edit (add-component, set-pin-positions, add-net),
re-render the overlay and read it back. This is the cheapest possible
correctness check and the user has explicitly asked for it to gate every
stage:

```bash
python3 .agents/skills/schematic-graph/graph_cli.py render-overlay \
  --board <id> --sheet <n> --out /tmp/<board>_s<n>_overlay.png
```

The overlay shows component bboxes (orange), pin positions (pink dots),
and net-label texts (green) drawn on top of the source scan. Use
`--no-pins` while bboxes are still being placed and `--no-nets` while
pin positions are still being verified — staged overlays are easier to
read than a fully populated one.

Don't batch six edits and render once. Render after each step. The user
will explicitly ask "did you re-render and check?" — answering that with
"the schema validates" is not the right answer.

## CLI

```bash
# Render the current graph as an overlay on the source PNG. Run this after
# every graphical edit and read it back to verify.
python3 .agents/skills/schematic-graph/graph_cli.py render-overlay \
  --board exidy_440 --sheet 1 [--no-pins] [--no-nets] [--out /tmp/o.png]

# Append a component (validates part against the librarian, rejects duplicates,
# requires a non-degenerate bbox). --source is one of ai|human|datasheet|probe.
python3 .agents/skills/schematic-graph/graph_cli.py add-component \
  --board exidy_440 --refdes U14C --part 74LS245 --sheet 1 \
  --bbox 1820,2240,2080,2660 --source ai --confidence 0.92

# Remove a component by refdes
python3 .agents/skills/schematic-graph/graph_cli.py remove-component \
  --board exidy_440 --refdes U14C

# Attach the tight chip-outline rectangle (Stage 1.5). bbox stays as the
# loose click-target / pin-area extent; body_bbox is what the KiCad export
# uses to size the rendered symbol body so it matches the original drawing.
# See identifier/SKILL.md § Two bboxes per component.
python3 .agents/skills/schematic-graph/graph_cli.py set-body-bbox \
  --board exidy_440 --refdes U14C --bbox 1840,2260,2060,2640

# List all components, or just one sheet
python3 .agents/skills/schematic-graph/graph_cli.py list-components --board exidy_440
python3 .agents/skills/schematic-graph/graph_cli.py list-components --board exidy_440 --sheet 1

# Validate the graph: refdes uniqueness, parts in the librarian, bbox sanity,
# net endpoint refdes coverage, edge_type uniformity per net, sheet_zone refs.
# REJECTS null edge_types — every endpoint must declare its connection kind.
python3 .agents/skills/schematic-graph/graph_cli.py validate --board exidy_440

# Per-sheet pickup signal — run on session start to see where to resume.
# Reports component count, pin-position coverage, named net count, ERC
# state, and the inferred Stage (0=not started, 1=bboxes, 2=pins done,
# 3=nets, 5=ERC clean) for every sheet.
python3 .agents/skills/schematic-graph/graph_cli.py pipeline-status --board exidy_440

# Stage-3 gate — list nets touching a sheet whose endpoints have null
# edge_type or labels with empty names. Must return PASS before Stage 3 ends.
python3 .agents/skills/schematic-graph/graph_cli.py untyped-nets \
  --board exidy_440 --sheet 1

# Cross-checks the graph against the source PNG, the librarian, and net
# topology heuristics. FAIL on blank-bbox chips, pins floating outside the
# bbox, malformed pin numbers; WARN on chips with zero nets, gappy bus members,
# over-connected nets.
python3 .agents/skills/schematic-graph/graph_cli.py lint \
  --board exidy_440 --sheet 1

# Stage-5 gate — categorises the most-recent ERC report into blocking
# (construction bugs) / cross-sheet expected (resolves later) / benign
# (cosmetic) / other (real wiring mistakes), with one PASS/FAIL line.
python3 .agents/skills/schematic-graph/graph_cli.py erc-summary \
  --board exidy_440 --sheet 1

# Rasterise the most-recent export to PNG so the agent can Read it back
# and visually compare to the source overlay.
python3 .agents/skills/schematic-graph/graph_cli.py render-kicad \
  --board exidy_440 --sheet 1 --out /tmp/exidy_s1_kicad.png

# Add a net: 2+ endpoints sharing one edge_type. Auto-fills sheet from each
# component. --kind: signal/power/ground/clock/bus_member.
# --edge-type: wire/label/sheet_zone/off_page/bus/implicit_power.
python3 .agents/skills/schematic-graph/graph_cli.py add-net \
  --board exidy_440 --name CSC --kind signal --edge-type wire \
  --endpoints "U14C.6,U13C.4,U12C.6" --source ai

# sheet_zone edges require --zone-ref (the original 4C6-style notation):
python3 .agents/skills/schematic-graph/graph_cli.py add-net \
  --board exidy_440 --name A0 --kind signal --edge-type sheet_zone \
  --endpoints "U7A.10,U4F.5" --zone-ref 4C6

# Remove or list nets:
python3 .agents/skills/schematic-graph/graph_cli.py remove-net --board exidy_440 --name CSC
python3 .agents/skills/schematic-graph/graph_cli.py list-nets --board exidy_440 [--sheet 1]
```

The CLI is the only sanctioned way to add components programmatically. The
explorer (HITL) edits the same `graph.json` directly via its PUT endpoint —
both paths must produce schema-valid output.

## Files

- `graph.schema.json` — canonical schema for `boards/<id>/graph.json`. Required
  fields: `board`, `sheets`, `components`, `nets`. Each component has `refdes`,
  `part` (Librarian key), `sheet`, `bbox`, optional `pin_positions`. Each net
  endpoint declares an `edge_type` from the six allowed kinds (`wire`, `label`,
  `sheet_zone`, `off_page`, `bus`, `implicit_power`).
- `probes.schema.json` — schema for one row of `probes.csv`. Columns:
  `priority, net, endpoints, reason, suggested_test, status`.
- `discrepancies.md` — template for the per-board `discrepancies.md` file.

## Invariants

- One net's endpoints **may not mix edge types** — a wire and a label resolving
  to the "same" net is two nets that should be merged explicitly with provenance.
- **Single-endpoint nets are valid** for `edge_type` ∈ {`label`, `sheet_zone`,
  `off_page`} — they represent named signals whose other ends live on sheets
  that haven't been transcribed yet. `wire`, `bus`, and `implicit_power`
  still require ≥2 endpoints (a one-endpoint physical wire is a floating
  pin and an error).
- `component.part` **must exist in the librarian** (run `librarian.py coverage`
  before saving). The schema does not enforce this; validation does.
- `pin_positions` keys must be valid pin numbers from the part's `chips.json`
  entry. Missing `pin_positions` triggers default DIP layout in the explorer.
- Sheet-zone edges (`edge_type: sheet_zone`) must include `sheet_zone_ref`
  in their evidence (the original `4C6`-style notation), per the board's
  `off_page_convention`.

## Constraints

- Schema validation does not check semantics. Add a separate ERC pass before
  trusting a graph: floating outputs, multi-driver shorts, unconnected power,
  bus-tap arity mismatches.
- `probes.csv` regenerates from `graph.json` — never hand-edit. Update the
  graph and regenerate.
- `discrepancies.md` is the one file in this skill that humans **do** hand-edit
  — it logs physical-board probing results.

## KiCad-export gotchas

- `/` and `'` in net names break KiCad's schematic loader; the export
  rewrites them: `'` → `~{...}` overbar, `/` → `_`. The graph.json keeps
  the original human-readable name.
- Plain `(label ...)` cannot carry `(shape ...)` — only `(global_label
  ...)` and `(hierarchical_label ...)` accept it. The export uses
  `global_label` for `sheet_zone` / `off_page` (cross-sheet) and plain
  `label` for `label` (in-sheet).
- ERC will report many "Pin not connected" errors during transcription —
  this is expected when most nets are cross-sheet labels and the other
  sheets haven't been done yet. **Use `erc-summary` for the verdict
  rather than reading the full ERC text:** it splits cross-sheet
  expected counts (noise) from blocking categories and real wiring
  errors so the gate is one line, not a thousand.
- `kicad-cli`'s "Failed to load schematic" message has no detail. If
  this happens, run `validate` (sexp parse) to confirm the file is at
  least syntactically valid, then check label/global_label syntax — the
  most common cause is an attribute KiCad rejects.
- Symbol-local +Y is UP, schematic-page +Y is DOWN. The exporter flips
  py when computing wire endpoints; if you add new pin emission code,
  use `pin_endpoint_mm[(refdes, str(n))] = (x_mm + px, y_mm - py)`.

## What the export tool does deterministically (don't re-implement these)

- Snaps every emitted coordinate to KiCad's 50-mil (1.27 mm) connection
  grid. Eliminates `endpoint_off_grid` violations by construction.
- Auto-emits a `power_out` flag and global_label per unique power pin
  name (VCC, GND, …) on the sheet, so chips' `power_in` pins are driven.
  Eliminates `power_pin_not_driven` by construction.
- Writes a `sym-lib-table` and a `<board>.kicad_pro` next to the .kicad_sch
  so KiCad treats the directory as a project and recognises the inlined
  `user` library (the `lib_symbol_issues` warning still appears in the
  ERC output but is benign — the symbols are embedded under lib_symbols
  inside each .kicad_sch and load fine).
- Refuses to write a .kicad_sch when `validate` finds errors. Override
  with `--allow-invalid` only when consciously inspecting a partial
  state — never as a way to silence a real failure.

## Visual round-trip beyond the overlay (KiCad PDF)

The KiCad export uses 1:1 source-pixel→mm scaling, so chips land at
roughly the same RELATIVE positions in the exported PDF as in the source
scan. A side-by-side of the source overlay and the KiCad PDF is a useful
final check — labels and components in obviously different positions
indicate either a missing chip in the export or a transcription error.

(Future: `--bg-image` flag to embed the source PNG directly behind the
KiCad symbols, so a single PDF carries both the original drawing and
the transcribed graph for visual diff.)

## Known exporter gaps — fix when working on this skill

The visual fidelity of the KiCad export is good but not done. The
items below are concrete changes to `kicad_export.py` (or sibling
files) that the next session working on this skill should pick up.
Each one is a small, well-scoped patch with a clear acceptance test.

### 1. Discrete symbols render as `??` placeholders in KiCad GUI

**Symptom**: open any exported `.kicad_sch` containing discretes
(R/C/Crystal/SW_Push/DB9_Male/…) in the KiCad GUI. The refdes and
value text show correctly (R1, 2k2, …) but the symbol body is a red
`??` placeholder. Chip lib_symbols work fine because we inline them
in `(lib_symbols …)`; only discretes — which reference stock libs
like `Device:R` — fail.

**Cause**: KiCad's symbol resolver walks the **project**-level
`sym-lib-table` (in the project directory) before the user-global
one. Our `export-kicad` writes the `.kicad_sch` but never writes a
project `sym-lib-table`, so when stock libs aren't picked up from
the user-global config, lookup fails and the GUI shows `??`.

**Fix**: in `cmd_export_kicad`, after writing the per-sheet
`.kicad_sch` files, write `boards/<id>/kicad/sym-lib-table` listing
exactly the stock libs the board uses. Walk every component, collect
`part["kicad_symbol"]`, split on `:` to get the lib name, dedupe.
Then emit:

```
(sym_lib_table
  (lib (name "Device")    (type "KiCad") (uri "${KICAD8_SYMBOL_DIR}/Device.kicad_sym"))
  (lib (name "Switch")    (type "KiCad") (uri "${KICAD8_SYMBOL_DIR}/Switch.kicad_sym"))
  (lib (name "Connector") (type "KiCad") (uri "${KICAD8_SYMBOL_DIR}/Connector.kicad_sym"))
)
```

`${KICAD8_SYMBOL_DIR}` is set by every supported KiCad install.

**Also do** the analogous `fp-lib-table` (the footprint property
already points at `Package_DIP:…` and `Resistor_THT:…`; same
resolution gap).

**Acceptance**: open `boards/z80_sbc/kicad/z80_sbc_s1_….kicad_sch`
in KiCad. R/C/X1/SW1/J1 render with their proper symbol shapes
(zigzag, parallel lines, quartz oval, push-button, D-sub). No red
`??` boxes. `kicad-cli sch export bom` includes every discrete with
populated Value and Footprint columns.

### 2. `render-kicad` console dump is too low-resolution

**Symptom**: `graph_cli render-kicad` produces a ~1200 px-wide PNG.
At that resolution, KiCad's `??` placeholders blur into chip-body
rectangles and silently look fine — meaning console review misses
problems the GUI would catch. Stack traces of pixel coords on small
chips also become impossible to read.

**Fix**: in `cmd_render_kicad`, either remove the `sips` resize
step entirely (the intermediate SVG is vector — no quality loss to
emit at native A3 resolution) or bump the resize target from
~1200 px to 2400+ px wide.

**Acceptance**: a dumped PNG of z80_sbc sheet 1 makes the discrete
symbol shapes individually identifiable when read by Claude. The
file size will increase substantially; that's fine for the
short-lived dumps in `/tmp/<board>_s<n>_kicad/`.

### 3. Yellow chip-body fill

**Symptom**: every chip in the KiCad export renders as a solid
yellow rectangle. Original schematics use only a pen-stroke outline.

**Fix**: in `synth_symbol` and `synth_faithful_symbol`, change
`(fill (type background))` → `(fill (type none))` on the body
rectangle. Two occurrences. The power symbol's polyline doesn't
need a change.

**Acceptance**: chips render as outlined rectangles, not filled.

### 4. Pin function names duplicated inside chip body

**Symptom**: every pin's functional name (T1OUT, A0, MREQ, …) is
drawn inside the chip body. The same text usually also appears as
a global_label at the pin tip (for cross-net connectivity). Doubled
labelling clutters the page. Original schematics show only the pin
number on the tick mark.

**Fix**: in `synth_symbol` and `synth_faithful_symbol`, add
`(pin_names hide)` to the lib_symbol header (alongside the existing
`(pin_names (offset 0.508))` line — replace the `(offset …)` form
with `hide`, or add a separate hide directive).

**Acceptance**: chip bodies show only the body rectangle and pin
numbers on the tick marks; pin function names disappear from inside
the body. Global_labels at pin tips remain — connectivity unchanged.

### 5. Power-source labels stacked in a right-margin column

**Symptom**: `gen_sch` emits one `#PWR_VCC`, `#PWR_GND`, etc.
pseudo-component per unique power-net name, all in a vertical stack
at the right edge of A3. Three or four labels in a column dominate
the right side of the page. Each chip's individual power pin ALSO
gets its own `(global_label "VCC")` flag at the pin position, so
there's both a column on the right AND duplicate flags scattered
across the page.

**Fix**: emit KiCad's stock `(power)` symbols (`power:VCC`,
`power:GND`, etc.) — small upward/downward arrows — directly at
each chip's power pin position instead of (or in addition to) the
right-edge column. Drop the column entirely once each chip's pin
carries its own local power flag.

**Acceptance**: KiCad render shows one small power arrow at each
chip's power pin (matching old-schematic convention); no centralised
power-flag column at the page edge. ERC still passes
(`power_pin_not_driven` count stays at 0).

### 6. Native KiCad `(bus …)` rendering for shared-trunk groups

**Symptom**: address buses (A0–A15) and data buses (D0–D7) emit
one `(wire …)` per member from chip A's pin to chip B's pin. Result
is a 16- or 8-wide ribbon of parallel right-angle traces instead of
one thick rail with member stubs. Functionally correct, visually
not how the original draws buses. The path-tracer SKILL.md now
*forbids* polyline sharing across members (see the bus-rail-shorts
fix); this item is the proper solution.

**Fix**: in `kicad_export.py`, detect bus groups (nets matching
`X.0..X.N` or `X0..XN` whose endpoints overlap chip-pin clusters).
Compute a trunk polyline that touches every member pin's row/column.
Emit:

```
(bus (pts (xy x1 y1) (xy x2 y2) …))
(bus_entry (at <pin_x> <pin_y>) (size 2.54 2.54))     ; per member
(label "A[0..15]" (at …))                              ; on the trunk
```

Then per-member, replace the `(wire …)` from rail to pin with a
short stub from the `bus_entry` to the pin. ERC understands `(bus)`
as a visual aggregation, not a single net — connectivity remains
per-member via the labels.

**Effort**: ~100–200 lines. Hardest piece is trunk-direction
detection and per-pin entry-side calculation (left vs. right of the
trunk).

**Acceptance**: Z80 SBC address/data buses render as a single thick
rail with stubs to each chip pin, matching the Grant Searle drawing
style. `erc-summary` stays clean (no `pin_to_pin` from shared
polylines, no `multiple_net_names`). After this lands, update
`path-tracer/SKILL.md § Buses` to retire the
"don't share polylines / use labels" workaround.
