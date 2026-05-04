# Identifier skill

Visual identification of components on a schematic. The identifier is **Claude
itself**, not a separate vision model. The skill provides the workflow contract
and the tile-based image access pattern; the actual recognition happens by
reading tile PNGs in the conversation.

## When to use

- The user asks to "annotate all chips on sheet N" or similar.
- After cartographer has tiled a sheet (or as the first step in such a request,
  in which case run cartographer first).
- To produce a first-pass component list that the human will refine in the
  explorer (edit, delete, add what was missed).

## Why tiles

Source schematic PNGs are typically 5000×4500+ pixels. Reading them directly
through the Read tool downsamples the image so far that chip labels become
illegible. Tiles of ~2000×1500 pixels can be read at near-native resolution and
text is recognizable.

Tiles overlap by a fraction (default 10%) so chips at tile borders aren't cut
in half. To prevent the same chip being annotated twice, each tile has an
`owned_bbox` (its non-overlapping interior); a chip is owned by the tile whose
`owned_bbox` contains the chip's center.

## Workflow

```
1. Read boards/<id>/board.json. Find the target sheet's scan_path.
2. Run cartographer to tile the sheet:
   .venv/bin/python .agents/skills/cartographer/cartographer.py tile \
     <sheet_path> --out /tmp/<board>_s<n>_tiles --grid 3x3 --overlap 0.1
3. Read /tmp/.../manifest.json — note each tile's source_bbox and owned_bbox.
4. For each tile in the manifest, in order:
   a. Read the tile PNG.
   b. Identify every chip in the tile: rectangle outline + refdes text +
      part-number text (e.g. "U14C  74LS245").
   c. For each chip:
      - Compute its bbox in TILE-LOCAL pixel coords (just from looking).
      - Compute its center: (bbox.x1+bbox.x2)/2, (bbox.y1+bbox.y2)/2.
      - Translate the center to SOURCE coords: add tile.source_bbox[0],[1].
      - Translate the full bbox to SOURCE coords the same way.
      - Discard the chip if its source-coord center is NOT inside this tile's
        owned_bbox (it'll be owned by an adjacent tile).
      - If the part isn't in the librarian, do not skip — record it with
        part="UNKNOWN_<text>" and report at the end so the user can decide
        whether to add it via the librarian skill.
      - Run schematic-graph add-component with --source ai and a confidence:
        - 0.9+ for clean printed text on an undamaged chip
        - 0.6–0.8 for hand-corrected text or partial occlusion
        - ≤0.4 for legible-outline-only (record with --note describing what's
          ambiguous)
5. After processing all tiles, run:
   - schematic-graph list-components --board <id> --sheet <n>
   - librarian coverage boards/<id>/graph.json
6. Report:
   - how many components were added
   - how many were UNKNOWN_* and need user attention
   - a summary the user can scan before opening the explorer
```

## CLI summary

```bash
# Tile a sheet
.venv/bin/python .agents/skills/cartographer/cartographer.py tile \
  exidy/scans/pages/logic_sheet1_video_ram_mpx.png \
  --out /tmp/exidy_440_s1_tiles --grid 3x3 --overlap 0.1

# Translate a tile-local bbox to source coords (sanity helper)
.venv/bin/python .agents/skills/cartographer/cartographer.py to-source \
  --manifest /tmp/exidy_440_s1_tiles/manifest.json \
  --tile r0c1 --bbox 320,180,520,420

# Append a component
python3 .agents/skills/schematic-graph/graph_cli.py add-component \
  --board exidy_440 --refdes U14C --part 74LS245 --sheet 1 \
  --bbox 1820,2240,2080,2660 --source ai --confidence 0.92
```

## Constraints

- **Don't invent refdes.** If no refdes is shown near a chip, use a tentative
  `U?<sheet><col><row>` placeholder and add a note in the evidence.
- **Don't invent part numbers.** Smudged or cut-off text → part `UNKNOWN_<best
  guess fragment>`, not a guess.
- **Confidence is calibrated.** Don't use 0.95+ for hand-drawn or noisy text.
  The explorer uses confidence to decide which probe-list rows to suggest.
- **Append-only per pass.** If the user asks to re-run identification, do
  `schematic-graph remove-component` first for the affected sheet, or refuse
  duplicates. Never silently overwrite human edits.
- **Stop at the first illegible region.** If a tile is mostly unreadable, say
  so and ask the user — don't fabricate from low-confidence guesses.
