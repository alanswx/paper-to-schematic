# Identifier skill

Visual identification of components AND of fine details (pin numbers, net
labels) on a schematic. The identifier is **Claude itself**, not a separate
vision model — no OCR libraries. The skill provides the workflow contract and
the image-access patterns; the actual recognition happens by reading PNGs in
conversation.

Behaviour split: see `AGENTS.md § Deterministic vs LLM`. CV is for tiling,
cropping, masking, and file I/O. The LLM does anything that involves reading
text, identifying a chip, deciding where a bbox should be, or interpreting a
labelled signal name.

## When to use

- The user asks to "annotate all chips on sheet N" or similar.
- After cartographer has tiled a sheet (or as the first step in such a
  request, in which case run cartographer first).
- To produce a first-pass component list and grow it pin by pin and net by
  net into a complete sheet transcription.

## The core loop: render → read → flag → fix → render

The single most important rule of this skill: **after every graphical edit,
re-render the overlay and read it back.** This is the cheapest possible
feedback loop and it is what makes the harness work. The order is always:

```
edit graph.json → render-overlay → read overlay PNG → flag what's wrong →
fix → render-overlay → read again → repeat until nothing's wrong → next step
```

`schematic-graph render-overlay` produces a PNG showing every component
bbox, every pin position, and every named-net label drawn on top of the
source scan. Mis-placed bboxes, pins floating in empty space, labels in
the wrong column — all of these are obvious in the overlay and invisible
when you only have `graph.json`. Don't skip this step. Don't batch six
edits and render once. Render after each step.

```bash
python3 .agents/skills/schematic-graph/graph_cli.py render-overlay \
  --board <id> --sheet <n> --out /tmp/<board>_s<n>_overlay.png
# then Read the PNG and report mismatches
```

## Why tiles

Source schematic PNGs are typically 5000×4500+ pixels. Reading them
directly downsamples the image so far that chip labels become illegible.
Tiles of ~2000×1500 pixels read at near-native resolution.

Tiles overlap by a fraction (default 10%) so chips at tile borders aren't
cut in half. Each tile has an `owned_bbox` (its non-overlapping interior);
a chip is owned by the tile whose `owned_bbox` contains the chip's centre.

## Reading a crop — coordinate translation

`cartographer tile`, `crop-chip`, and `crop-region` all output **at native
source resolution** (a 250-px-wide region in source coords is a 250-px-wide
PNG). The crop-coord → source-coord translation is a simple add: `source_x
= crop_x + offset_x` (each command prints the offset on stdout).

### The downsampled-display scale trap

**Claude doesn't necessarily see the crop at native resolution.** When the
PNG is larger than the model's vision-patch budget (very tall or very wide
crops in particular — a 318×1668 px Z80 crop, say), the IMAGE shown to
the model is downsampled. The features you can identify (pin numbers, chip
outlines) are still there, but the COORDINATES you'd read off the displayed
image are display-pixel, not crop-pixel. Adding the source-offset to those
coords lands you somewhere hundreds of pixels off the actual chip.

Two safe ways to defeat this:

- **Keep crops small.** A crop ≤ 1500 px on its longer dimension is almost
  certainly shown at native resolution. If a chip is too tall/wide, take
  multiple smaller crops (top half, bottom half) and translate each
  separately. Smaller is also faster for the model.
- **Verify after every set-pin-positions by reading the overlay back.**
  This is the only way to catch a scale error after the fact — the overlay
  shows your pins as pink dots on the source. If they don't sit at the
  pin tick marks, the placement is wrong regardless of what arithmetic
  you did. *Do this every time, not just when something looks off.*

`graph_cli set-pin-positions` now rejects pins more than ~2× the
bbox-diagonal away from the bbox centre — the typical fingerprint of a
forgotten offset or a downsampled-display scale error. The rejection
message includes the fix instructions. If you see it, do NOT pass
`--allow-out-of-bbox`; re-read the crop, recompute the coords, resubmit.

### sips wrap-up

**Do NOT enlarge crops with `sips -z H W`** before reading them. With both
dimensions specified, sips DISTORTS the image (independent H and V scale
factors); applying a single scale to features read out of the displayed
image then introduces a systematic per-axis offset of 10–50 source pixels
per component — invisible in the displayed PNG but obvious in the overlay
later. The fixes:

- **Read the native crop directly.** Same trap as above — but at least no
  axis distortion.
- **If you need a closer view, take a smaller crop** (e.g. drop `--pad`
  or call `crop-region` for a sub-rectangle of the chip).
- **If you must resize, use `sips -Z N`** (capital `Z`, single max
  dimension) which preserves aspect.

When in doubt, verify by re-cropping the suspect region natively after
placing a bbox and reading it back — a correctly-placed component
overlaps its bbox; an offset of 20+ pixels is the sips-stretch
fingerprint.

## Workflow

### Stage 1 — bbox round-trip (do this first, completely)

```
1. Read boards/<id>/board.json. Find the target sheet's scan_path.
2. Tile the sheet:
   .venv/bin/python .agents/skills/cartographer/cartographer.py tile \
     <sheet_path> --out /tmp/<board>_s<n>_tiles --grid 3x3 --overlap 0.1
3. Read /tmp/.../manifest.json. Note each tile's source_bbox and
   owned_bbox.
4. For each tile in the manifest:
   a. Read the tile PNG.
   b. Identify every chip in the tile: rectangle outline + refdes text +
      part-number text (e.g. "U14C  74LS245").
   c. For each chip:
      - Anchor the bbox by writing down two points in the displayed image:
        chip top-left and chip bottom-right (in displayed-pixel coords),
        before estimating tile-local coords. Vague "I'll just eyeball
        it" estimates are the #1 source of wrong bboxes downstream.
      - Translate to source coords by adding tile.source_bbox[0],[1].
      - Discard the chip if its source-coord centre is NOT inside this
        tile's owned_bbox (it'll be owned by an adjacent tile).
      - If the part isn't in the librarian: record it as
        part="UNKNOWN_<text>" and report at the end. Do not skip and do
        not guess.
      - graph_cli add-component with --source ai and a calibrated
        confidence (0.9+ clean printed text, 0.6–0.8 partial / hand
        corrected, ≤0.4 outline-only).

5. RENDER OVERLAY. Read the PNG. MANDATORY after every add-component
   batch — the CLI now prints the render-overlay command to run at the
   end of every add-component / set-pin-positions / set-body-bbox so
   you can copy-paste.
   python3 .agents/skills/schematic-graph/graph_cli.py render-overlay \
     --board <id> --sheet <n> --no-pins --no-nets

6. Flag every bbox that does not visibly cover the chip body on the
   source scan. Common failure modes:
     - bbox shifted left/right/up/down by a tile-translation error
     - bbox too small (only encloses chip label text)
     - bbox too large (includes adjacent text or wires)
     - chip drawn on the page but no bbox at all (missed the chip)
     - bbox is for the WRONG CHIP — two chips of the same part nearby

7. Fix each flagged bbox via graph_cli remove-component +
   add-component with corrected coords (or a Python edit of graph.json
   for batch fixes).

8. RE-RENDER, re-read. Repeat steps 5–7 until every chip on the page
   has a clean bbox. This is the gate to Stage 2.
```

**Bbox → pins is one-way.** If you change a chip's bbox AFTER its
pin_positions have been placed, the pins do NOT auto-translate.
- Explorer drag updates pin_positions to follow the moving bbox.
- CLI `set-body-bbox`, direct graph.json edits, and `add-component`
  remove+re-add cycles do NOT. Those leave the pin_positions at their
  old absolute source coords, which are now stale.
- `set-body-bbox` now WARNS if existing pin_positions look stale after
  the update. Heed the warning: re-run set-pin-positions to fix.

**Aspect-ratio rule for Stage 2 prep.** When the bbox is settled, look
at its aspect ratio. Tall (h > 1.5×w) or wide (w > 1.5×h) means the
chip is drawn rotated. Skip the explorer's function-based auto-fill
for rotated chips and go straight to set-pin-positions with an explicit
JSON map of pin numbers read off a high-res crop.

CV-snap is a deterministic refinement, not a recognition step. **If
snap-board produces a worse bbox than the vision-placed one, keep the
vision bbox and skip snap for that chip.** See `AGENTS.md` for the
anti-pattern note about tuning CV harder when recognition fails.

#### Two bboxes per component: click-target vs. body

The `bbox` field is intentionally **loose** — it's the click-target /
pin-area extent. It includes the chip body PLUS the surrounding pin
tick marks and any small label text so the explorer's hit-test catches
near-edge clicks and the auto-fill DIP layout has somewhere to put pins.
This is the bbox you place in Stage 1.

The KiCad export needs a tighter rectangle — just the chip's drawn
outline (the rectangle the original schematic actually drew, no pin
ticks, no label clutter). That's `body_bbox`:

```
graph_cli set-body-bbox --board <id> --refdes <r> --bbox x1,y1,x2,y2
```

**Capture it as part of Stage 1**, right after you place a bbox you're
satisfied with:

1. Look at the chip on the source. Identify the *drawn rectangle* —
   the boundary the original artist stroked. Pin ticks and refdes
   text sit OUTSIDE this rectangle.
2. Record those four edges in source-pixel coords.
3. `graph_cli set-body-bbox …`

When `body_bbox` is set, the KiCad-rendered symbol body matches the
original drawing's chip outline. When it's absent, the export falls
back to the click-target bbox shrunk by one pin-length on each side
(workable but visibly chunky).

`cartographer snap-bbox` produces what would be a good `body_bbox`
when it succeeds. If you're already running `snap-board`, consider
writing the snapped result into `body_bbox` rather than overwriting
`bbox` — the looser click-target stays useful for the explorer's
hit-test even when the body is tight.

### Stage 1b — discretes (resistors, caps, switches, connectors, crystals)

Discretes are a separate identification pass from chips. They look
visually distinct (zigzag/rectangle for R, parallel lines for C,
cylinder with `+` for CP, oval with internal mass for crystal, push-
button silhouette, D-sub trapezoid for connectors). Most sheets have a
handful; the Z80 SBC example has 3 resistors, 5 caps, 1 crystal, 1 DB9,
1 push button.

The librarian carries generic entries with `kind: "discrete"`:
`R`, `R_SMD`, `C`, `CP` (polarized), `D`, `LED`, `Crystal`, `SW_Push`,
`SW_SPDT`, `Conn_01x02`/`06`/`10`/`16`/`20`, `DB9_Female`, `DB9_Male`.
Add more as boards demand them.

```
1. Skim the sheet for non-rectangular components. Note refdes (R1, C2,
   X1, J1, …), placement bbox, and — for parts where it's printed —
   the value (1k, 22p, 7.3728MHz).
2. graph_cli add-component --board <id> --refdes R1 --part R --sheet 1 \
     --bbox <x1,y1,x2,y2> --value 2k2
3. Pin positions for 2-pin parts auto-fill to the bbox endpoints.
   Multi-pin connectors (DB9, Conn_01xN) need pin numbers placed in
   source order — drag them in the explorer or call set-pin-positions.
4. For polarized parts (CP, D, LED): the librarian's pin "1" is the +
   / anode and pin "2" is the - / cathode. Place pin 1 on the lead the
   source marks with + (or the unbanded end for diodes).
```

Discretes don't need crop-chip / pin-number reading — there's nothing
printed on the part body to identify them, and the librarian entries
have no per-pin functional names. The KiCad export references stock
symbols (`Device:R`, `Switch:SW_Push`, …) and stock footprints from
the part entry, so once added they appear correctly in the schematic
and BOM without further setup.

### Stage 2 — pin positions (one chip-class at a time)

**When the auto-fill default is wrong** (which is most of the time):

- Chips drawn ROTATED 90° (tall narrow bbox: height > 1.5× width, or
  wide flat bbox: width > 1.5× height). The function-based auto-fill
  is orientation-blind — it puts power-on-top/ground-on-bottom even
  for chips drawn vertically with power/GND on the LEFT side. Always
  read pin numbers explicitly for rotated chips.
- Chips with functional pin grouping where the source draws pin
  numbers in functional order, not DIP-numerical (the Z80 source has
  all address pins on the right side in numerical order 5,4,3,…,1,40,
  39,…,30, then control + data — the function-defaults won't reproduce
  that layout).
- Sub-gates (74LSxx quad gates, MC10124, etc.) — handled by the
  multi-unit detector when the librarian groups match `g\d+`,
  `ff\d+`, `ms\d+`, or single A-Z. If a chip's source is split into
  per-gate symbols (one half of a 74LS123 monostable, half of a 74LS74
  flip-flop), use a letter-suffix refdes (`IC129a`, `IC129b`) so the
  exporter emits each as its own compact symbol with only that gate's
  pins. If the librarian's groups don't match a unit pattern, ADD a
  group rename in chips.json (or extend the pattern in kicad_export.py)
  before placing pins — otherwise you'll end up with 16 pins crammed
  into a 5-pin-wide bbox.

The strict workflow:

```
1. crop-chip → high-res PNG (small crops! ≤1500 px longest side, to
   avoid the downsampled-display scale trap — see "Reading a crop").
2. Read the PNG. Note each pin's number AND its position in CROP-pixel
   coords. Add the printed crop-origin offset to get source coords.
3. graph_cli set-pin-positions --board <id> --refdes <r> --json @<file>
   * The CLI now REJECTS pins more than ~2× the bbox-diagonal from the
     bbox centre. That catches forgotten offsets and downsampled-scale
     errors at submit time. If you see the rejection, FIX the coords —
     do not pass --allow-out-of-bbox to mute the check.
4. RENDER OVERLAY (now with pins). Read it back. MANDATORY — not
   "when something looks off". Every pin should sit at a pin tick on
   the source drawing; pins floating in empty space or buried inside
   the chip body are wrong. If even ONE pin is misplaced, the whole
   placement is suspect (you mis-read pin numbers, mis-translated coords,
   or hit the downsampled-display scale trap) — re-do it.
5. After visual confirmation, advance to the next chip.
6. For other instances of the SAME chip drawn identically (e.g. four
   2716 EPROMs stacked vertically), use bbox-delta translation:
     graph_cli clone-pins --from h61 --to i61,c61,b61
   Then re-render and verify each clone's pins land on the right spots.
```

**Source routing**: `crop-chip` reads `board.json`'s `source` block and
auto-picks the input. On `vector_pdf` boards (Dorado family), the harness
re-renders the source PDF at 600 DPI on the fly and rescales the bbox; the
LLM doesn't need to pass `--pdf`. On `raster_scan` boards (Exidy), the
canonical scan is the source of truth — high-DPI is unavailable. The first
line of `crop-chip`'s output is `[source] vector_pdf board → rendering …`
or absent (scan path); paste that into your reasoning so the next step
knows what coordinate space the crop came from. Translate render-coord pin
positions back to graph/scan coords using the printed `to translate …`
formula before calling `set-pin-positions`.

For Exidy-style hand-drawn schematics without printed pin numbers, skip
this stage; the function-based default layout is correct enough.

### Stage 3 — named nets (cross-sheet labels)

Schematics that use named nets (MCA.00, MCD.0, …) instead of explicit
wires are very common. Read the labels:

```
1. crop-chip on each chip whose pins carry labels.
2. From the crop, write down (refdes, pin) → label_text for every
   labelled pin. Build a CSV.
3. Group by label_text. For each unique label, graph_cli add-net with
   --edge-type=label and all the pins as endpoints.
4. Single-endpoint label nets ARE valid — they represent cross-sheet
   signals whose other ends live on sheets that haven't been transcribed
   yet. Don't try to invent additional endpoints.
5. RENDER OVERLAY (now with net labels). Read it back. Flag wrong
   pin-to-label assignments.
```

**Stage 3 acceptance gate** (paste before moving on):

```
$ python3 .agents/skills/schematic-graph/graph_cli.py validate --board <id>
ok — ...

$ python3 .agents/skills/schematic-graph/graph_cli.py untyped-nets --board <id> --sheet <n>
PASS — no untyped nets on sheet <n>

$ python3 .agents/skills/schematic-graph/graph_cli.py lint --board <id> --sheet <n>
PASS ...
```

If `untyped-nets` returns FAIL, walk every listed net: read the source
crop again, decide whether it's `label` (named off-page net), `wire`
(local), or `sheet_zone` (zoned cross-sheet ref), and add-net with the
right --edge-type. **Stage 3 is not done until untyped-nets is PASS.**

### Stage 4 — direct-wire nets (Exidy-style only)

For schematics that use direct wires rather than labels:

```
1. Run tracer trace --board <id> --sheet <n>.
2. graph_cli import-traced-nets to bring proposed nets into graph.json.
3. RENDER OVERLAY. Read it back. Flag over-connected nets (a "net" with
   20+ endpoints is almost certainly a CV crossing-detection failure,
   not a real bus).
4. Fix with explicit add-net / remove-net.
```

If `tracer` reports `pins assigned=0`, your pin positions are wrong —
go back to Stage 2 before retrying the tracer.

### Stage 5 — KiCad export (final round-trip)

```
1. graph_cli export-kicad --board <id> --sheet <n> --validate
   (refuses to write if validate finds issues; the LLM should never
   pass --allow-invalid without an explicit reason)
2. graph_cli erc-summary --board <id> --sheet <n>
3. graph_cli render-kicad --board <id> --sheet <n> --out /tmp/<id>_s<n>_kicad.png
4. Read the rendered PNG. Compare to the source overlay. Components and
   labels should be in roughly the same RELATIVE positions because KiCad
   export uses 1:1 source-pixel→mm scaling.
```

**Stage 5 acceptance gate** (paste before declaring the sheet done):

```
$ graph_cli erc-summary --board <id> --sheet <n>
  blocking (0): (none)
  cross-sheet expected (...): ...    # noise, ignore
  benign (...): ...                  # cosmetic, ignore
  other (0): (none)
  PASS — ...
```

The four numeric assertions are non-negotiable:

| Category               | Required | If non-zero, do this                             |
|------------------------|----------|--------------------------------------------------|
| `blocking`             | **0**    | construction bug; re-export should clear it     |
| `other` errors         | **0**    | real wiring mistakes (pin_to_pin Output-Output) — open the .erc.txt, find the offending pins, fix the graph |
| cross-sheet expected   | any      | resolves when other sheets land                  |
| benign                 | any      | cosmetic; ignore                                  |

If `blocking` is non-zero, `export-kicad`'s built-in fixes (grid-snap,
power flags, sym-lib-table) didn't run — most likely you bypassed
validate with `--allow-invalid` or the export tool is broken. Don't
paper over it; fix the cause.

If `other` has any errors (typically `pin_to_pin`), open the .erc.txt,
read the two refdes/pin pairs, and reconcile in `graph.json`. These are
real wiring mistakes the LLM made and only the LLM can fix.

## CLI summary

```bash
# === Stage 1: bbox round-trip ===

# Tile a sheet
.venv/bin/python .agents/skills/cartographer/cartographer.py tile \
  <sheet_path> --out /tmp/<board>_s<n>_tiles --grid 3x3 --overlap 0.1

# Translate a tile-local bbox to source coords (sanity helper)
.venv/bin/python .agents/skills/cartographer/cartographer.py to-source \
  --manifest /tmp/exidy_440_s1_tiles/manifest.json \
  --tile r0c1 --bbox 320,180,520,420

# Add a component
python3 .agents/skills/schematic-graph/graph_cli.py add-component \
  --board exidy_440 --refdes U14C --part 74LS245 --sheet 1 \
  --bbox 1820,2240,2080,2660 --source ai --confidence 0.92

# RENDER OVERLAY (gate for Stage 2)
python3 .agents/skills/schematic-graph/graph_cli.py render-overlay \
  --board exidy_440 --sheet 1 --no-pins --no-nets

# Optional CV refinement (only if it agrees with vision)
.venv/bin/python .agents/skills/cartographer/cartographer.py snap-board \
  --board exidy_440 --sheet 1

# === Stage 2: pin positions ===

# High-res crop of one chip
.venv/bin/python .agents/skills/cartographer/cartographer.py crop-chip \
  --board dorado_base --refdes h61 --out /tmp/h61_crop.png

# Commit pin positions read from the crop
python3 .agents/skills/schematic-graph/graph_cli.py set-pin-positions \
  --board dorado_base --refdes h61 --json '@/tmp/h61_pins.json'

# Re-render with pins to verify
python3 .agents/skills/schematic-graph/graph_cli.py render-overlay \
  --board dorado_base --sheet 5

# === Stage 3: named nets ===

# Add a labelled bus net
python3 .agents/skills/schematic-graph/graph_cli.py add-net \
  --board dorado_base --name MCA.00 --kind signal --edge-type label \
  --endpoints 'f61.9,h61.19,i61.19,c61.19,b61.19' --source ai

# === Stage 5: KiCad export ===

python3 .agents/skills/schematic-graph/graph_cli.py export-kicad \
  --board <id> --sheet <n> --validate
```

## Constraints

- **Don't invent refdes.** If no refdes is shown near a chip, use a
  tentative `U?<sheet><col><row>` placeholder and add a note.
- **Don't invent part numbers.** Smudged or cut-off text →
  `UNKNOWN_<best guess fragment>`, not a guess.
- **Confidence is calibrated.** Don't use 0.95+ for hand-drawn or noisy
  text. The explorer uses confidence to rank probe-list rows.
- **Append-only per pass.** Re-running identification: do
  `remove-component` first for the affected sheet, or refuse duplicates.
  Never silently overwrite human edits.
- **Stop at the first illegible region.** If a tile is mostly
  unreadable, say so and ask the user — don't fabricate from low-
  confidence guesses.
- **The overlay is the gate.** Don't move from Stage N to Stage N+1
  until the Stage-N overlay reads cleanly. Each stage builds on the
  previous one's correctness; an unfixed Stage-1 bbox shift will
  cascade into wrong pin positions, wrong net assignments, and wrong
  KiCad output.
