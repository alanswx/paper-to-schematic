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
- Generating the ranked `probes.csv` from the current graph.
- Exporting a `.kicad_sch` (planned).

## CLI

```bash
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
