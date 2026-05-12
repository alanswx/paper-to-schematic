# Shared bus-rail polylines short distinct nets together

**Component:** `.agents/skills/schematic-graph/kicad_export.py`, `.agents/skills/path-tracer/SKILL.md`
**Symptom:** When the same polyline is applied to every member of a bus (per the path-tracer SKILL's "Buses" recommendation), KiCad's ERC reports `pin_to_pin` errors merging the bus members into a single net.
**Severity:** Forces label-typed workaround for buses — the export can't currently render the visible bus rail without shorting the nets.
**First observed:** Z80 SBC sheet 1, May 2026 (commit `50cbadf`).

## What the SKILL.md says

`path-tracer/SKILL.md § Buses`:

> When the source draws a bus as one thick rail (e.g. `A0..A12`), trace the rail polyline ONCE and apply it to every member net. Save effort by grouping bus members by name pattern (`X.0..X.N` or `X0..XN`) and reusing the rail path for each.
>
> The KiCad-side bus rendering using `(bus …)` is a future improvement — for now, every member net carrying the same path is enough to make the overlay look right and avoid pin-to-pin diagonals.

The recommendation assumes that two distinct nets emitting wires on identical coordinates is harmless. In KiCad's electrical model, it's not.

## What goes wrong

The exporter emits one `(wire (pts …))` element per net per polyline segment, with no deduplication. Two distinct nets that share a polyline segment produce two overlapping `(wire)` elements on the same grid coordinates.

KiCad's connectivity engine sees the overlap as a single conductor connecting both pins on either end. Both pin endpoints — which originally belonged to different nets — become electrically the same node.

## Minimal reproducer

Z80 SBC: 24 bus members (A0–A15, D0–D7) with shared trunk at `x=430`. Each member's polyline includes a long vertical segment at `x=430` spanning from the top to the bottom of the chip rows. Re-export:

```bash
.venv/bin/python /tmp/trace_buses.py     # script that committed all 24 paths
.venv/bin/python .agents/skills/schematic-graph/graph_cli.py erc-summary --board z80_sbc --sheet 1
# other (16): pin_to_pin=15, multiple_net_names=1
```

Sample conflict from the ERC report:

```
[pin_to_pin]: Pins of type Output and Output are connected
    @(160.02 mm, 173.99 mm): Symbol U2 Pin 35 [A5, Output, Line]
    @(160.02 mm, 198.12 mm): Symbol U2 Pin 40 [A10, Output, Line]
```

Pin 35 (A5) and pin 40 (A10) are on different nets, but their bus polylines both include the segment along `x=160.02 mm` (= the shared trunk after grid-snap). KiCad reports them as connected.

## Fix candidates

1. **Implement `(bus …)` emission** (recommended). The proper KiCad construct. When the exporter detects a group of nets matching `X0..XN` or `X.0..X.N` with a shared path, emit:
   ```
   (bus (pts (xy x1 y1) (xy x2 y2) …))
   (bus_entry (at <pin_x> <pin_y>) (size 2.54 0))
   (label "A0" (at …))
   ```
   This is exactly how human-drawn KiCad schematics render buses. ERC understands `(bus)` as a visual aggregation, not a single net.

2. **Per-member trunk offsets**. Auto-spread each bus member's vertical to its own grid line (`x = 430 + member_index * 1.27`). 16 members would occupy a 20 mm band of parallel verticals. Visually closer to a thick rail than no rail at all, but uglier than `(bus …)`. Easier to implement than #1 — just modify the path before emission. Spacing must exceed `KICAD_GRID_MM`; otherwise members snap to the same grid line and collide again.

3. **Deduplicate overlapping wire segments at emit time** — keep just one `(wire)` for any segment shared by ≥2 nets. The remaining nets connect via labels at their endpoints, the shared visual wire belongs to no specific net (or to all of them — KiCad won't care about ownership for a wire segment shared across labels). Risky: KiCad's electrical engine still treats overlapping pin connections as merged unless the shared wire is replaced by a `(bus)` or some other non-conductor element.

4. **Reject shared paths at write time** — `set-net-path` could refuse to commit a polyline that overlaps with an existing higher-provenance net's polyline (or warn loudly). Doesn't solve the rendering problem; just makes the failure mode visible at the right moment.

(1) is the right long-term fix; (2) is a 20-line patch to the exporter that buys visual fidelity until (1) lands.

## Validation when fixed

Re-run the bus-tracing script that this bug doc references:

```bash
.venv/bin/python /tmp/trace_buses.py
.venv/bin/python .agents/skills/schematic-graph/graph_cli.py export-kicad --board z80_sbc --validate
.venv/bin/python .agents/skills/schematic-graph/graph_cli.py erc-summary --board z80_sbc --sheet 1
# expect pin_to_pin=0
.venv/bin/python .agents/skills/schematic-graph/graph_cli.py render-kicad --board z80_sbc --sheet 1
# expect address + data buses visible as continuous rails in the rendered PNG,
# NOT as 24 stacked label endpoints at each chip
```

## Related

- `bugs/polyline-pin-snap-mismatch.md` — separate bug, same symptom shape (`wire_dangling` from path math), already fixed in `dcb5a8e`.
- `.agents/skills/path-tracer/SKILL.md § Buses` — the workaround instructions that this bug invalidates. Should be updated once (1) or (2) ships.
