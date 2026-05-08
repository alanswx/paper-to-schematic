# Path-tracer skill

LLM-driven wire-path tracing for faithful KiCad export.

Connectivity already lives in `graph.json` (each net's `endpoints`). This
skill annotates each wire-typed net with a **routed polyline** — the actual
right-angle path it takes on the original drawing — so the KiCad export
emits `(wire …)` segments that match the source artwork instead of the
one-corner Manhattan fallback.

This is recognition work: vision reads the image, identifies which ink
belongs to a given net, follows it corner by corner, and writes the path.
**Do not** try to do this with CV — the prior attempt (line skeleton +
connected components) failed on dot-vs-crossing junctions, T-junctions
where a stub leaves the bus, and dense regions where multiple wires
travel along adjacent grid lines. Vision handles all three natively.

## When to invoke

After Stage 3 named nets are committed and you want the explorer overlay
+ KiCad export to look like the original. Rendering only — connectivity
is canonical via `endpoints`, so a missing or wrong path doesn't break
ERC, validation, or the netlist. This means it's safe to ship in
batches; sheet-by-sheet is the natural unit.

## Skip

- `edge_type: "label"` / `"sheet_zone"` / `"off_page"` nets. Those
  render as a label/global_label/off-page connector at each pin in the
  original; there's no continuous wire to trace. The exporter already
  handles them with text-only emission.
- Power/ground nets (`VCC`, `GND`, `VEE`) when their endpoints are
  already labelled in the original — same reason.

## CLI

```bash
# What's left to trace on this sheet?
python3 .agents/skills/schematic-graph/graph_cli.py untraced-nets \
  --board <id> --sheet <n>

# Read a region of the sheet at high resolution. On vector_pdf boards
# this auto-renders from the source PDF at 600 DPI; on raster_scan
# boards it crops the canonical scan. The first stdout line is
# `[source] …` — paste it into your reasoning so coordinate translation
# is unambiguous.
.venv/bin/python .agents/skills/cartographer/cartographer.py crop-region \
  --board <id> --sheet <n> --bbox x1,y1,x2,y2 --out /tmp/region.png

# Commit a polyline. Right-angle segments only — diagonals are rejected
# at write time so they can't sneak into KiCad. Source 'tracer' < 'ai' <
# 'human' for provenance precedence.
python3 .agents/skills/schematic-graph/graph_cli.py set-net-path \
  --board <id> --name <net> --source ai \
  --path "x1,y1; x2,y1; x2,y2; x3,y2"

# Re-export and visually verify.
python3 .agents/skills/schematic-graph/graph_cli.py export-kicad \
  --board <id> --sheet <n> --validate
python3 .agents/skills/schematic-graph/graph_cli.py render-kicad \
  --board <id> --sheet <n>
```

## Workflow

The unit of work is a **region**, not a single wire. One vision read of
a tile produces multiple polylines — that's the only economical way to
trace a sheet.

1. **List what's left.** `untraced-nets --sheet N` returns wire-typed
   nets without a path. Read it; pick a region of the sheet to start.
2. **Crop the region** at high DPI via `crop-region`. Pick a bbox big
   enough to contain several whole wires (most of a chip's pin column +
   the wires leaving it, ~1500 source-pixels square is a reasonable
   default). Read the saved PNG.
3. **Identify each wire by endpoint pair.** For every net in the
   `untraced-nets` output that has at least one endpoint inside the
   region, find that endpoint's pin position on the crop and follow the
   ink visually until you reach another endpoint of the same net. Note
   every corner (where the line turns 90°) in **source-image pixel
   coordinates**, not crop-local coordinates — translate using the
   `(cx + offset)` / `((cx + offset) / scale)` formula crop-region
   prints.
4. **Right-angle only.** If you see a diagonal, the source uses curved
   ink at a corner — the underlying segments are still axis-aligned;
   pick the corner pixel. If a wire is genuinely drawn diagonal (rare —
   only resistor leads in some old schematics), leave the net untraced
   rather than write a bad path.
5. **Commit**: `set-net-path --source ai --path "x1,y1;x2,y1;x2,y2;…"`.
   The verb rejects diagonals and refuses to overwrite a higher-
   provenance path without `--force`.
6. **Move to the next region.** Track which regions you've covered so
   you don't re-trace.

## Junctions (connection dots)

Where two wires CROSS at a point AND CONNECT, the original draws a
connection dot. KiCad needs an explicit `(junction (at x y))` at that
point or it'll treat the crossing as independent. Recognise the dot in
the same vision read; emit per-sheet:

```bash
# (no CLI yet — set sheets[N].junctions directly via a small script
# until set-junctions is added)
```

If two wires cross WITHOUT a dot, that's an intentional "crossing,
no connect" — DO NOT add a junction.

## Buses

When the source draws a bus as one thick rail (e.g. `A0..A12`), trace
the rail polyline ONCE and apply it to every member net. Save effort
by grouping bus members by name pattern (`X.0..X.N` or `X0..XN`) and
reusing the rail path for each.

The KiCad-side bus rendering using `(bus …)` is a future improvement —
for now, every member net carrying the same path is enough to make the
overlay look right and avoid pin-to-pin diagonals.

## Acceptance

- Per sheet, when you decide it's "done":
  `untraced-nets --sheet N` should be empty *or* every remaining net is
  one you intentionally skipped (diagonal-only, occluded, ambiguous —
  document why in a comment if needed).
- `export-kicad --sheet N --validate` runs clean (no new
  endpoint_off_grid or pin_to_pin errors introduced by paths).
- `render-kicad --sheet N` produces a PNG that visually matches the
  source's wire layout.

## Why this is recognition, not CV

CV walked the line skeleton in pixel space and produced connectivity,
but it failed at:
- **Junction vs. crossing** — pixel topology can't distinguish them
  reliably; the dot-detector morphology was always at the edge of
  working.
- **Dense buses** — adjacent grid-aligned wires merge in the skeleton.
- **Stubs leaving a bus** — T-junctions where one of the three legs is
  a single short stub got misclassified.

Vision treats each of these as a visual question, which is what they
are. Don't try to "fix the CV" if a path is wrong; replace it with a
vision-read path. Same anti-pattern guard as `snap-bbox` — recognition
overrules deterministic geometry, never the other way around.
