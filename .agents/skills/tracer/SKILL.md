# Tracer skill

CV-assisted wire detection on a full schematic sheet. Produces *proposed*
nets — pin-pin connections inferred from the source image — that the human
verifies in the explorer or via `graph_cli import-traced-nets`.

The tracer reads the source PNG and the current `graph.json` (for component
bboxes + pin positions) and emits a JSON file listing connected pin groups.
It does **not** modify `graph.json` directly — proposed nets go through an
import step so the AI provenance stays explicit.

## When to use

- After component bboxes are reasonably correct AND pin positions are
  reasonably accurate (manual `N`-mode placement, or a clean DIP-default
  layout). The tracer's accuracy depends on how close `pin_positions` sit
  to the actual schematic pin endpoints.
- For a first-pass net suggestion when manual net-drawing on every wire
  would be tedious (sheet 1 of Exidy 440 has ~150 nets).
- Re-run after each round of pin-position refinement; the tracer is cheap.

## CLI

```bash
# Trace a sheet, emit proposed nets to JSON.
.venv/bin/python .agents/skills/tracer/tracer.py trace \
  --board exidy_440 --sheet 1 --out /tmp/exidy_s1_traces.json
# Optional: --debug /tmp/exidy_s1_skeleton.png to write the line skeleton

# Import proposed nets into the graph (gives them a prefixed name + ai source).
python3 .agents/skills/schematic-graph/graph_cli.py import-traced-nets \
  --board exidy_440 --from /tmp/exidy_s1_traces.json --prefix T_

# Then re-validate and (optionally) re-export to KiCad.
python3 .agents/skills/schematic-graph/graph_cli.py validate --board exidy_440
python3 .agents/skills/schematic-graph/graph_cli.py export-kicad \
  --board exidy_440 --sheet 1 --validate
```

## Algorithm

1. Threshold the source PNG (Otsu, inverted — outlines = white).
2. **Mask out chip bodies** using each component's bbox (slightly inset to
   keep pin tick marks visible). Chip outlines and interior text would
   otherwise dominate the connectivity graph.
3. **Extract the line skeleton:** morphological OPEN with a long horizontal
   kernel keeps long horizontal segments; long vertical kernel keeps
   verticals. OR them together. Schematic wires are axis-aligned, so this
   captures essentially all wire pixels while erasing pin tick marks
   (short, 5–15 px) and label text (small).
4. Bridge right-angle turns with a 3×3 morphological CLOSE (one iteration).
5. **Connected components** on the skeleton. Each component is a maximal
   set of skeleton pixels connected by 8-neighbor adjacency.
6. **Pin → component assignment:** for each pin position in the graph,
   sample a small window in the labeled image; assign the pin to the
   most-occurring nonzero label in that window (within a search radius).
7. Group pins by component label. Components touching ≥2 pins become
   proposed nets.

## Known limitations (v1)

- **Over-connection at line crossings.** The current pipeline doesn't
  distinguish a *junction dot* (electrical connection) from a *crossover*
  (no connection). Two unrelated wires that cross will merge into one
  proposed net. Fixing this needs dot detection — a separate pass.
- **Pin positions must be reasonably accurate.** If `pin_positions` are
  still at the auto-DIP default and the chip's schematic symbol uses a
  different layout, the pin-to-component assignment misses the wire. Run
  pin-numbering mode (`N`) in the explorer first.
- **Labels and sheet-zone refs are ignored.** The tracer only finds
  *direct* wire connectivity; it can't infer labeled nets like `A0` ↔ `A0`
  on different sheets. Those go through the explorer's `W` mode with
  `edge_type = label` or `sheet_zone`.
- **No bus arity check.** Bus lines (`D[0..7]`) appear as one fat skeleton
  segment touching many pins; v1 may suggest a single 8-pin net for a bus.
  Add `edge_type = bus` per endpoint after import.

## Output JSON format

```json
{
  "board": "exidy_440",
  "sheet": 1,
  "proposed_nets": [
    {
      "label": 7,
      "endpoints": [
        { "refdes": "U14C", "pin": "1" },
        { "refdes": "U13C", "pin": "5" }
      ],
      "confidence": 0.5,
      "skeleton_area_px": 1342
    }
  ]
}
```
