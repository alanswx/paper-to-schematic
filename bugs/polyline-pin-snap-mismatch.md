# Polyline ↔ pin-tip grid-snap mismatch

**Component:** `.agents/skills/schematic-graph/kicad_export.py`
**Symptom:** `wire_dangling` ERC errors on hand-traced polylines whose source-pixel coords match a chip's pin position exactly.
**Severity:** Annoying, not blocking — workaround is to use `edge_type: "label"` for short pin-to-pin connections instead of `wire` + polyline.
**First observed:** Z80 SBC sheet 1, May 2026 (commit `6e09388`).

## What goes wrong

The exporter converts coordinates from source pixels to KiCad millimetres in two different code paths that don't agree after grid-snap.

For a **chip pin tip** (faithful symbol branch, ~`kicad_export.py:662–675`):

```python
instance_origin_mm = (_snap(M + cx_px * scale), _snap(M + cy_px * scale))
local_offset_mm    = (_snap((ix - cx_px) * scale), _snap(-(iy - cy_px) * scale))
pin_tip_mm         = instance_origin_mm + local_offset_mm
```

Two independent snaps — one for the instance origin, one for the per-pin local offset.

For a **polyline point** (faithful wire routing branch, ~`kicad_export.py:830–843`):

```python
pt_mm = (_snap(M + p[0] * scale), _snap(M + p[1] * scale))
```

One snap on the whole sum.

These two formulas don't agree for every grid placement, because `_snap(a) + _snap(b) ≠ _snap(a + b)` in general. The difference is always 0 or ±1 grid unit (±1.27 mm at the default 50-mil grid).

## Minimal reproducer

`boards/z80_sbc/`, U1 pin 14 (T1OUT):

- bbox `[154, 81, 267, 216]`, so `cx_px = 210.5`, `cy_px = 148.5`
- pin position in graph: `[155, 167]`
- scale ≈ `0.3772` mm/px (A3 paper, 1000×720 sheet)
- `PAPER_MARGIN_MM = 12.7`
- `KICAD_GRID_MM = 1.27`

**Pin-tip path:**
- `instance_origin.x = _snap(12.7 + 210.5 × 0.3772) = _snap(92.10) = 92.71`
- `local_offset.x   = _snap((155 - 210.5) × 0.3772) = _snap(-20.94) = -20.32`
- `pin_tip.x = 92.71 - 20.32 = 72.39`

**Polyline path (same source coord 155):**
- `pt.x = _snap(12.7 + 155 × 0.3772) = _snap(71.166) = 71.12`

**Off by exactly 1.27 mm = one grid unit.** Wire starts at `71.12`, pin tip is at `72.39`, KiCad reports `wire_dangling`.

Same arithmetic on Y: pin tip lands at `74.93`, polyline at `76.20` — also 1.27 mm off.

## Why it bit us

The Z80 SBC sheet has 11 short pin-to-pin connections between U1 (MAX232) and J1 (DB9) plus the four MAX232 charge-pump caps. Hand-traced polylines using the pin's source-pixel coords as endpoints all landed one grid unit off the actual pin tip, producing 9 `wire_dangling` ERC errors. Worked around by converting those nets to `edge_type: "label"`.

## Fix candidates

The right fix is to make the two formulas agree. Options, roughly ordered by invasiveness:

1. **Make `pin_endpoint_mm` use the single-snap formula** (recommended). Change the faithful branch to:
   ```python
   pin_tip_mm = (_snap(M + ix * scale), _snap(M + iy * scale))
   ```
   Then `local_offset_for_lib_symbol = pin_tip_mm - instance_origin_mm` is what goes into the symbol definition. The lib-symbol pin coords would no longer be guaranteed-on-grid relative to the body, but the *absolute* pin tip would be guaranteed on-grid and would match every polyline point at the same source coord.

2. **Snap polyline points to the same offset frame.** For each polyline point, find the nearest component and apply that component's `instance_origin_mm + _snap((p - cx) * scale)` formula. Heavy — polylines aren't owned by any single component.

3. **Snap source-pixel coords to a sub-grid in the graph itself.** Round graph pin positions and path points to the nearest source-px multiple of `KICAD_GRID_MM / scale ≈ 3.37 px` before persisting. Loses bbox-fitting precision, painful for users dragging pins in the explorer.

4. **Reduce `KICAD_GRID_MM` to a finer setting.** Cosmetic only — the snap mismatch still exists, just at a smaller absolute magnitude. ERC would still trip below 0.1 mm tolerance.

(1) seems cleanest; it preserves the explorer/graph data model and only touches the exporter's pin-position calculation.

## Validation when fixed

```bash
# z80_sbc has hand-traced polylines (committed as labels for now). Convert
# back to wire-typed with explicit paths and re-run:
PY=.venv/bin/python; CLI=.agents/skills/schematic-graph/graph_cli.py
$PY $CLI remove-net --board z80_sbc --name RS232_TX
$PY $CLI add-net --board z80_sbc --source ai --name RS232_TX --kind signal --edge-type wire --endpoints "J1.2,U1.14"
$PY $CLI set-net-path --board z80_sbc --name RS232_TX --source ai --path "155,167; 100,167; 100,127; 63,127"
$PY $CLI export-kicad --board z80_sbc --validate
$PY $CLI erc-summary --board z80_sbc --sheet 1
# expect wire_dangling=0
```
