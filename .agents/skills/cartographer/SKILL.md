# Cartographer skill

Image preprocessing for schematic scans. Decodes archive-format inputs (JP2,
PDF), produces cleaned per-sheet PNGs, tiles them for AI annotation, and
refines AI-estimated bboxes via CV (`snap-bbox` / `snap-board`). Downstream
skills (explorer, identifier, tracer, schematic-graph) consume the PNG outputs.

## CLI

```bash
.venv/bin/python .agents/skills/cartographer/cartographer.py <cmd> ...
```

```bash
# Decode one page from an archive.org-style JP2 zip → PNG.
# Requires opj_decompress (brew install openjpeg). The zip layout
# (cheyenne_jp2/cheyenne_0021.jp2 etc.) is auto-detected.
decode-jp2 <zip> <page> --out <png>

# Render one PDF page → PNG (uses pdftocairo / poppler).
decode-pdf <pdf> [--page N] [--dpi 300] --out <png>

# Light cleanup: percentile-based contrast stretch and optional Otsu binarize.
# Useful when scans are yellowed and the line/text contrast is poor.
clean <input.png> --out <output.png> [--lo-pct 2 --hi-pct 98] [--threshold]

# Cut a sheet PNG into overlapping tiles. Each tile is small enough for
# the identifier skill to read at near-native resolution. Manifest records
# per-tile source_bbox + owned_bbox for coordinate translation and dedup.
tile <input.png> --out <dir> [--grid 3x3] [--overlap 0.1]

# Translate a tile-local bbox to source coords (sanity helper for the
# identifier workflow).
to-source --manifest <manifest.json> --tile <id> --bbox x1,y1,x2,y2

# Snap a single tentative bbox to the nearest chip-outline rectangle in
# the source image. Hole-detection on a line-skeleton; --debug writes
# red=original, green=snapped + the horiz/vert/binary intermediate layers.
snap-bbox --image <png> --bbox x1,y1,x2,y2 [--search-pad 120] [--debug out.png]

# Snap every component on a board's graph.json. Skips verified components
# unless --include-verified. Drops pin_positions so the explorer regenerates
# defaults against the new bbox.
snap-board --board <id> [--sheet N] [--dry-run] [--include-verified]

# Crop one chip + margin so the LLM can read pin numbers / labels at high
# resolution. Source-aware: on vector_pdf boards the harness re-renders the
# source PDF at 600 DPI on the fly and rescales the bbox; on raster_scan
# boards it crops the canonical scan. The first stdout line is `[source]
# vector_pdf board → rendering …` (PDF path) or absent (scan path) — paste
# it into your reasoning. The printed `to translate (cx, cy) → graph/scan
# coords` formula converts crop-local coords back to graph-coord pin
# positions.
crop-chip --board <id> --refdes <r> --out <png> [--pad 80] [--no-pdf]
          [--pdf <path>] [--page N] [--dpi 600]
```

## Source routing

Each board declares its source kind in `board.json`:

```json
"source": {
  "kind": "vector_pdf",                            // or "raster_scan"
  "pdf_path": "../../<mfr>/<board>.pdf",          // vector_pdf only
  "default_render_dpi": 600,                       // vector_pdf only
  "scan_dpi": 300                                  // optional
}
```

Tools branch on this so a less-thinking LLM can call `crop-chip` without
deciding whether to use the PDF — the harness picks. `vector_pdf` boards
get crisp text from on-demand 600 DPI re-renders; `raster_scan` boards
fall back to the canonical scan because high-DPI on a raster source just
upscales noise. Use `--no-pdf` to override on a vector_pdf board.

## Workflow for a brand-new board

1. **Acquire scans.** Drop the source PDFs and JP2 zips into a manufacturer
   folder, e.g. `<manufacturer>/manuals/`, `<manufacturer>/scans/jp2/`.
   Treat as immutable.
2. **Decode** each sheet from JP2 to PNG with `decode-jp2`. Output goes to
   `<manufacturer>/scans/pages/<descriptive_name>.png`.
3. (Optional) **Clean** if the scan has poor contrast (yellowed paper):
   `clean <raw.png> --out <cleaned.png>` and reference the cleaned PNG from
   `board.json`.
4. Continue with the README's onboarding steps (board.json → annotate
   → snap-board → HITL → trace → KiCad export).

## Constraints

- `opj_decompress` (OpenJPEG) is the only reliable JP2 decoder on macOS for
  the archive.org scans encountered so far. ImageMagick / `sips` both fail
  on those JP2s. The CLI errors with a clear install-hint if it's missing.
- `pdftocairo` (from poppler) is the dependency for `decode-pdf`. Install
  with `brew install poppler`.
- Scans are immutable inputs. Cartographer outputs go to a derived path,
  never overwrite originals.
- A board's `board.json` should reference the cleaned PNG paths, not the
  raw JP2 paths.
