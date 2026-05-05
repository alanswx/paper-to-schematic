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

# List all components, or just one sheet
python3 .agents/skills/schematic-graph/graph_cli.py list-components --board exidy_440
python3 .agents/skills/schematic-graph/graph_cli.py list-components --board exidy_440 --sheet 1

# Validate the graph: refdes uniqueness, parts in the librarian, bbox sanity,
# net endpoint refdes coverage, edge_type uniformity per net, sheet_zone refs.
python3 .agents/skills/schematic-graph/graph_cli.py validate --board exidy_440

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
  sheets haven't been done yet. The error count drops as more sheets
  come online; treat it as noise until the full board is in.
- `kicad-cli`'s "Failed to load schematic" message has no detail. If
  this happens, run `validate` (sexp parse) to confirm the file is at
  least syntactically valid, then check label/global_label syntax — the
  most common cause is an attribute KiCad rejects.

## Visual round-trip beyond the overlay (KiCad PDF)

The KiCad export uses 1:1 source-pixel→mm scaling, so chips land at
roughly the same RELATIVE positions in the exported PDF as in the source
scan. A side-by-side of the source overlay and the KiCad PDF is a useful
final check — labels and components in obviously different positions
indicate either a missing chip in the export or a transcription error.

(Future: `--bg-image` flag to embed the source PNG directly behind the
KiCad symbols, so a single PDF carries both the original drawing and
the transcribed graph for visual diff.)
