#!/usr/bin/env python3
"""Cartographer — image preprocessing for schematic scans.

Read SKILL.md before invoking. Run via the project venv:
  .venv/bin/python .agents/skills/cartographer/cartographer.py <cmd> ...
"""
import argparse
import json
import sys
from pathlib import Path


def cmd_tile(args):
    try:
        from PIL import Image
    except ImportError:
        print("Pillow required. Run: .venv/bin/pip install -r "
              ".agents/skills/cartographer/requirements.txt", file=sys.stderr)
        sys.exit(2)

    src = Path(args.input).resolve()
    if not src.exists():
        print(f"input not found: {src}", file=sys.stderr)
        sys.exit(1)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    img = Image.open(src)
    W, H = img.size

    try:
        rows, cols = (int(x) for x in args.grid.lower().split("x"))
    except ValueError:
        print(f"invalid --grid {args.grid!r}; expected ROWSxCOLS, e.g. 3x3", file=sys.stderr)
        sys.exit(1)

    overlap = args.overlap
    base_w = W / cols
    base_h = H / rows
    pad_w = base_w * overlap
    pad_h = base_h * overlap

    tiles = []
    for r in range(rows):
        for c in range(cols):
            x1 = max(0, int(c * base_w - pad_w))
            y1 = max(0, int(r * base_h - pad_h))
            x2 = min(W, int((c + 1) * base_w + pad_w))
            y2 = min(H, int((r + 1) * base_h + pad_h))
            tile = img.crop((x1, y1, x2, y2))
            tile_id = f"r{r}c{c}"
            tile_path = out / f"{src.stem}_{tile_id}.png"
            tile.save(tile_path)
            # Per-tile, the "owned" interior region (no overlap padding) — the
            # identifier uses this to deduplicate chips that fall in two tiles.
            owned = [
                int(c * base_w),
                int(r * base_h),
                int((c + 1) * base_w),
                int((r + 1) * base_h),
            ]
            tiles.append({
                "id": tile_id,
                "row": r,
                "col": c,
                "path": str(tile_path.relative_to(out)),
                "source_bbox": [x1, y1, x2, y2],
                "owned_bbox": owned,
                "size": [x2 - x1, y2 - y1],
            })

    manifest = {
        "source_image": str(src),
        "source_size": [W, H],
        "grid": [rows, cols],
        "overlap": overlap,
        "tiles": tiles,
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    print(f"tiled {src.name} ({W}×{H}) → {rows}×{cols} = {len(tiles)} tiles "
          f"(overlap {overlap*100:.0f}%) in {out}")
    print(f"manifest: {manifest_path}")
    for t in tiles:
        sb = t["source_bbox"]
        print(f"  {t['id']}: {t['size'][0]}×{t['size'][1]}  "
              f"src_bbox=[{sb[0]},{sb[1]},{sb[2]},{sb[3]}]")


def cmd_to_source(args):
    """Translate a tile-local bbox to source-image coordinates.

    Useful for the identifier when reporting bboxes after looking at a tile.
    Reads the manifest, finds the tile, applies the offset.
    """
    manifest = json.loads(Path(args.manifest).read_text())
    tile = next((t for t in manifest["tiles"] if t["id"] == args.tile), None)
    if not tile:
        print(f"unknown tile id: {args.tile}", file=sys.stderr)
        sys.exit(1)
    try:
        x1, y1, x2, y2 = (float(v) for v in args.bbox.split(","))
    except ValueError:
        print("--bbox must be x1,y1,x2,y2 in tile-local pixels", file=sys.stderr)
        sys.exit(1)
    sx, sy = tile["source_bbox"][0], tile["source_bbox"][1]
    out = [x1 + sx, y1 + sy, x2 + sx, y2 + sy]
    print(",".join(f"{v:.0f}" for v in out))


def main():
    ap = argparse.ArgumentParser(prog="cartographer", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("tile", help="cut a sheet PNG into overlapping tiles")
    sp.add_argument("input")
    sp.add_argument("--out", required=True)
    sp.add_argument("--grid", default="3x3", help="ROWSxCOLS, default 3x3")
    sp.add_argument("--overlap", type=float, default=0.1, help="fractional overlap, default 0.1")
    sp.set_defaults(fn=cmd_tile)

    sp = sub.add_parser("to-source", help="translate a tile-local bbox to source coords")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--tile", required=True)
    sp.add_argument("--bbox", required=True)
    sp.set_defaults(fn=cmd_to_source)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
