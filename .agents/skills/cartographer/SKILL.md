# Cartographer skill (stub)

Image preprocessing for schematic scans. Decodes archive-format inputs (JP2 in
particular), cleans up the image (contrast, denoise, deskew), and produces the
PNG inputs that downstream skills (explorer, identifier, tracer) consume.

## Status

**Stub.** No CLI yet. The Exidy 440 scans were preprocessed by hand using the
shell scripts in `exidy/tools/` (`decode_jp2.sh`, `crop_titleblock.sh`). When
this skill is fleshed out, those scripts move here.

## Planned CLI

```bash
# Decode a JP2 zip entry to PNG
cartographer decode <jp2-zip> <page-index> <output.png>

# Clean a scan: contrast + denoise + optional deskew
cartographer clean <input.png> <output.png>

# Inventory a manual PDF: page count, sheet titles (OCR title block)
cartographer inventory <manual.pdf>
```

## Constraints

- `opj_decompress` (OpenJPEG) is the only reliable JP2 decoder on macOS for
  the archive.org scans encountered so far. ImageMagick / `sips` both fail.
- Scans are immutable inputs. Cartographer outputs go to a derived path,
  never overwrite originals.
- A board's `board.json` should reference the cleaned PNG paths, not the
  raw JP2 paths.
