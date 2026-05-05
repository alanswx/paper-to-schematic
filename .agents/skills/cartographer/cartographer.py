#!/usr/bin/env python3
"""Cartographer — image preprocessing for schematic scans.

Read SKILL.md before invoking. Run via the project venv:
  .venv/bin/python .agents/skills/cartographer/cartographer.py <cmd> ...
"""
import argparse
import json
import sys
from pathlib import Path


def _render_pdf_to_temp(pdf_path: Path, page: int, dpi: int) -> Path:
    """Render one PDF page to a tempfile PNG at the requested DPI; returns
    the PNG path. Uses pdftocairo (poppler) — same dependency as decode-pdf.
    Caller is responsible for cleanup (the file is in the system temp dir,
    so it'll be reaped eventually if not deleted)."""
    import shutil, subprocess, tempfile
    if not shutil.which("pdftocairo"):
        print("pdftocairo not on PATH (brew install poppler)", file=sys.stderr); sys.exit(2)
    if not pdf_path.exists():
        print(f"pdf not found: {pdf_path}", file=sys.stderr); sys.exit(1)
    td = tempfile.mkdtemp(prefix="carto_pdf_")
    stem = Path(td) / "page"
    r = subprocess.run(
        ["pdftocairo", "-png", "-r", str(dpi),
         "-f", str(page), "-l", str(page),
         "-singlefile", str(pdf_path), str(stem)],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        print(f"pdftocairo failed: {r.stderr.strip()}", file=sys.stderr); sys.exit(1)
    produced = Path(td) / "page.png"
    if not produced.exists():
        print("pdftocairo produced no output", file=sys.stderr); sys.exit(1)
    return produced


def cmd_tile(args):
    try:
        from PIL import Image
    except ImportError:
        print("Pillow required. Run: .venv/bin/pip install -r "
              ".agents/skills/cartographer/requirements.txt", file=sys.stderr)
        sys.exit(2)

    if args.pdf:
        src = _render_pdf_to_temp(Path(args.pdf).resolve(), args.page, args.dpi)
    elif args.input:
        src = Path(args.input).resolve()
    else:
        print("provide either input PNG or --pdf <path>", file=sys.stderr); sys.exit(1)
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


def _snap_one(img_gray, bbox, search_pad: int, line_min_len: int, min_size: int,
              max_size: int = 500):
    """Find the chip-outline rectangle near `bbox` via contour search on the
    line-skeleton (long horizontal + long vertical edges only).

    Returns (x1, y1, x2, y2) snapped, or None if nothing plausible was found.

    The AI's bbox center may not even fall inside the chip (we observed it
    landing on pin labels several chip-widths to the side). So we don't scan
    from center — we look for ANY chip-sized rectangle in the ROI and pick the
    closest match.

    Algorithm:
      1. Threshold the ROI (Otsu, inverted — outlines = white).
      2. Open horizontally → only long horizontal segments survive (chip
         top/bottom edges + long pin lines).
         Open vertically → only long vertical segments survive (chip
         left/right edges).
      3. Bridge corner gaps with a small dilation so each chip outline is one
         connected component, but not enough to merge with adjacent chips.
      4. Find contours, filter by size [min_size, max_size], pick the one
         whose center is closest to the AI bbox center.
    """
    import cv2

    H, W = img_gray.shape
    x1, y1, x2, y2 = bbox
    cx_t = (x1 + x2) / 2
    cy_t = (y1 + y2) / 2

    rx1 = max(0, int(min(x1, x2) - search_pad))
    ry1 = max(0, int(min(y1, y2) - search_pad))
    rx2 = min(W, int(max(x1, x2) + search_pad))
    ry2 = min(H, int(max(y1, y2) + search_pad))
    if rx2 - rx1 < 4 or ry2 - ry1 < 4:
        return None

    roi = img_gray[ry1:ry2, rx1:rx2]
    _, binary = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    horiz = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (line_min_len, 1)))
    vert = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, line_min_len)))

    # Bridge pin-tick gaps in the chip's left/right vertical edges using a
    # vertical close, and any small gaps in horizontal edges using a horizontal
    # close. Done on each layer separately so we don't smear lines across
    # orientations (which could connect adjacent chips).
    vert = cv2.morphologyEx(vert, cv2.MORPH_CLOSE,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (1, 15)),
                             iterations=1)
    horiz = cv2.morphologyEx(horiz, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (15, 1)),
                              iterations=1)

    skeleton = cv2.bitwise_or(horiz, vert)
    # Final small close at corners (where horizontal meets vertical).
    skeleton = cv2.morphologyEx(skeleton, cv2.MORPH_CLOSE,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
                                iterations=1)

    # RETR_CCOMP gives a 2-level hierarchy: top-level outer contours, and holes
    # inside them. The chip outline forms a closed loop, so the chip's INTERIOR
    # is a hole. We score holes (not outer contours) so the result naturally
    # excludes the merged-with-pin-lines outer blob.
    contours, hierarchy = cv2.findContours(skeleton, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)

    target_cx = cx_t - rx1
    target_cy = cy_t - ry1

    best = None
    best_score = float("inf")
    if hierarchy is not None:
        for i, c in enumerate(contours):
            # hierarchy[0][i] = [next, prev, first_child, parent]
            is_hole = hierarchy[0][i][3] != -1
            if not is_hole:
                continue
            bx, by, bw, bh = cv2.boundingRect(c)
            if bw < min_size or bh < min_size:
                continue
            if bw > max_size or bh > max_size:
                continue
            ccx = bx + bw / 2
            ccy = by + bh / 2
            dist = ((ccx - target_cx) ** 2 + (ccy - target_cy) ** 2) ** 0.5
            score = dist
            if score < best_score:
                best_score = score
                best = (bx, by, bw, bh)

    if best is None:
        # Fallback: when the line-skeleton hole-detection fails (typically
        # because the chip outline doesn't form a closed loop in the
        # extracted skeleton), try Hough lines. Find pairs of long
        # horizontal + long vertical line segments that frame a rectangle
        # near the target center.
        ai_w = abs(x2 - x1)
        ai_h = abs(y2 - y1)
        return _snap_via_hough(roi, binary, target_cx, target_cy,
                                rx1, ry1, min_size, max_size, line_min_len,
                                ai_w, ai_h)

    bx, by, bw, bh = best
    # Pad by a few pixels to include the outline itself (the hole is the interior).
    pad = 4
    return (max(0, bx - pad) + rx1,
            max(0, by - pad) + ry1,
            min(rx2 - rx1, bx + bw + pad) + rx1,
            min(ry2 - ry1, by + bh + pad) + ry1)


def _snap_via_hough(roi, binary, target_cx, target_cy, rx1, ry1,
                    min_size, max_size, line_min_len,
                    ai_w=0, ai_h=0):
    """Hough-line fallback: find pairs of (top, bottom) horizontal lines and
    (left, right) vertical lines that frame the chip body near (target_cx,
    target_cy). The skeleton-hole approach assumes a closed loop; this one
    just needs four straight edges anywhere in the ROI."""
    import cv2
    import numpy as np

    h, w = binary.shape
    lines = cv2.HoughLinesP(
        binary, rho=1, theta=np.pi / 180,
        threshold=80,
        minLineLength=max(line_min_len, 30),
        maxLineGap=10,
    )
    if lines is None:
        return None

    horiz, vert = [], []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        if dy <= 2 and dx >= line_min_len:
            horiz.append((min(y1, y2), min(x1, x2), max(x1, x2)))  # (y, x_lo, x_hi)
        elif dx <= 2 and dy >= line_min_len:
            vert.append((min(x1, x2), min(y1, y2), max(y1, y2)))   # (x, y_lo, y_hi)

    if len(horiz) < 2 or len(vert) < 2:
        return None

    # Cluster lines by position so duplicates from Hough's segment-splitting
    # don't re-detect the same edge.
    def cluster(items, axis_key, tol=4):
        items = sorted(items, key=lambda v: v[axis_key])
        groups = []
        for it in items:
            if groups and abs(it[axis_key] - groups[-1][-1][axis_key]) <= tol:
                groups[-1].append(it)
            else:
                groups.append([it])
        # collapse each group: position = mean, span = union
        out = []
        for g in groups:
            pos = sum(v[axis_key] for v in g) / len(g)
            lo = min(v[1] for v in g)
            hi = max(v[2] for v in g)
            out.append((pos, lo, hi))
        return out

    horiz = cluster(horiz, 0)
    vert = cluster(vert, 0)

    # For each (left, right, top, bottom) quadruple, score by proximity to
    # target + size sanity.
    best = None
    best_score = float("inf")
    for vi, (vlx, vly0, vly1) in enumerate(vert):
        for vrx, vry0, vry1 in vert[vi + 1:]:
            if vrx - vlx < min_size or vrx - vlx > max_size:
                continue
            if not (vlx <= target_cx <= vrx):
                continue
            for hi, (hty, htx0, htx1) in enumerate(horiz):
                for hby, hbx0, hbx1 in horiz[hi + 1:]:
                    if hby - hty < min_size or hby - hty > max_size:
                        continue
                    if not (hty <= target_cy <= hby):
                        continue
                    # Each horizontal line must span a substantial portion
                    # of (vlx..vrx); otherwise it's not really part of this
                    # rectangle.
                    span_required = (vrx - vlx) * 0.4
                    if (htx1 - htx0) < span_required: continue
                    if (hbx1 - hbx0) < span_required: continue
                    # And each vertical similarly for hty..hby.
                    vspan = (hby - hty) * 0.4
                    if (vly1 - vly0) < vspan: continue
                    if (vry1 - vry0) < vspan: continue
                    cx = (vlx + vrx) / 2
                    cy = (hty + hby) / 2
                    rw = vrx - vlx
                    rh = hby - hty
                    dist = ((cx - target_cx) ** 2 + (cy - target_cy) ** 2) ** 0.5
                    # Penalize size mismatch vs the AI's bbox so we prefer the
                    # FULL chip rectangle over an inner text-bbox sub-rectangle.
                    size_pen = 0.0
                    if ai_w > 0 and ai_h > 0:
                        size_pen = (abs(1 - rw / ai_w) + abs(1 - rh / ai_h)) * max(ai_w, ai_h) * 0.5
                    # Bonus for larger area (when AI sizing is unknown).
                    score = dist + size_pen - rw * rh * 0.001
                    if score < best_score:
                        best_score = score
                        best = (vlx, hty, vrx, hby)

    if best is None:
        return None
    bx1, by1, bx2, by2 = best
    pad = 2
    return (max(0, bx1 - pad) + rx1,
            max(0, by1 - pad) + ry1,
            bx2 + pad + rx1,
            by2 + pad + ry1)


def cmd_snap_bbox(args):
    try:
        import cv2
    except ImportError:
        print("opencv-python required. Install: .venv/bin/pip install -r "
              ".agents/skills/cartographer/requirements.txt", file=sys.stderr)
        sys.exit(2)

    img = cv2.imread(args.image, cv2.IMREAD_GRAYSCALE)
    if img is None:
        print(f"failed to load image: {args.image}", file=sys.stderr)
        sys.exit(1)

    try:
        bbox = tuple(float(v) for v in args.bbox.split(","))
    except ValueError:
        print(f"--bbox malformed: {args.bbox}", file=sys.stderr)
        sys.exit(1)
    if len(bbox) != 4:
        print(f"--bbox needs 4 values: {args.bbox}", file=sys.stderr)
        sys.exit(1)

    snapped = _snap_one(img, bbox, args.search_pad, args.line_min_len, args.min_size,
                        max_size=args.max_size)

    if snapped is None:
        # Fall back to the input bbox so callers can chain safely.
        out = tuple(int(v) for v in bbox)
        print(",".join(str(v) for v in out))
        if not args.quiet:
            print("(no rectangle found; returned input bbox unchanged)", file=sys.stderr)
    else:
        out = tuple(int(v) for v in snapped)
        print(",".join(str(v) for v in out))

    if args.debug:
        import numpy as np
        x1, y1, x2, y2 = (int(v) for v in bbox)
        H, W = img.shape
        rx1 = max(0, x1 - args.search_pad - 20)
        ry1 = max(0, y1 - args.search_pad - 20)
        rx2 = min(W, x2 + args.search_pad + 20)
        ry2 = min(H, y2 + args.search_pad + 20)

        # Composite: original (left) + horiz layer (mid) + vert layer (right)
        roi_inner = img[
            max(0, y1 - args.search_pad):min(H, y2 + args.search_pad),
            max(0, x1 - args.search_pad):min(W, x2 + args.search_pad)
        ]
        _, bin_dbg = cv2.threshold(roi_inner, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        h_dbg = cv2.morphologyEx(bin_dbg, cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (args.line_min_len, 1)))
        v_dbg = cv2.morphologyEx(bin_dbg, cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (1, args.line_min_len)))

        debug = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        cv2.rectangle(debug, (x1, y1), (x2, y2), (0, 0, 255), 3)
        if snapped is not None:
            sx1, sy1, sx2, sy2 = (int(v) for v in snapped)
            cv2.rectangle(debug, (sx1, sy1), (sx2, sy2), (0, 255, 0), 3)
        cv2.imwrite(args.debug, debug[ry1:ry2, rx1:rx2])
        cv2.imwrite(args.debug.replace(".png", "_horiz.png"), h_dbg)
        cv2.imwrite(args.debug.replace(".png", "_vert.png"), v_dbg)
        cv2.imwrite(args.debug.replace(".png", "_binary.png"), bin_dbg)
        print(f"debug image: {args.debug} (+ _horiz, _vert, _binary)", file=sys.stderr)


def cmd_snap_board(args):
    """Run snap-bbox on every component on a board (or one sheet) and rewrite graph.json."""
    try:
        import cv2
    except ImportError:
        print("opencv-python required.", file=sys.stderr)
        sys.exit(2)

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    board_dir = project_root / "boards" / args.board
    graph_path = board_dir / "graph.json"
    if not graph_path.exists():
        print(f"no graph: {graph_path}", file=sys.stderr)
        sys.exit(1)

    graph = json.loads(graph_path.read_text())
    sheets = {s["index"]: s for s in graph["sheets"]}

    # Cache decoded sheet images by sheet index.
    sheet_imgs = {}
    def get_sheet_img(idx):
        if idx not in sheet_imgs:
            scan_path = (board_dir / sheets[idx]["scan_path"]).resolve()
            img = cv2.imread(str(scan_path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                raise RuntimeError(f"failed to load sheet {idx} at {scan_path}")
            sheet_imgs[idx] = img
        return sheet_imgs[idx]

    summary = {"snapped": 0, "unchanged": 0, "skipped": 0}
    for comp in graph["components"]:
        if args.sheet is not None and comp["sheet"] != args.sheet:
            continue
        if comp.get("verified") and not args.include_verified:
            summary["skipped"] += 1
            continue
        img = get_sheet_img(comp["sheet"])
        old = comp["bbox"]
        new = _snap_one(img, tuple(old), args.search_pad, args.line_min_len,
                         args.min_size, max_size=args.max_size)
        if new is None:
            summary["unchanged"] += 1
            print(f"  - {comp['refdes']:6s} no rectangle found, kept {old}")
            continue
        summary["snapped"] += 1
        print(f"  + {comp['refdes']:6s} {[int(v) for v in old]} → {[int(v) for v in new]}")
        comp["bbox"] = list(new)
        # Pin positions were derived from the old bbox; regenerate.
        comp.pop("pin_positions", None)

    if not args.dry_run:
        graph_path.write_text(json.dumps(graph, indent=2) + "\n")
        print(f"\nwrote {graph_path}")
    else:
        print("\n(dry-run; graph.json not written)")
    print(f"summary: snapped={summary['snapped']} unchanged={summary['unchanged']} skipped={summary['skipped']}")


def cmd_decode_jp2(args):
    """Decode a single JP2 page from an archive.org-style zip to PNG via opj_decompress."""
    import re
    import shutil
    import subprocess
    import tempfile
    import zipfile

    if not shutil.which("opj_decompress"):
        print("opj_decompress not on PATH. Install: brew install openjpeg", file=sys.stderr)
        sys.exit(2)

    zip_path = Path(args.zip).resolve()
    if not zip_path.exists():
        print(f"zip not found: {zip_path}", file=sys.stderr)
        sys.exit(1)

    page = args.page
    page_str = f"{int(page):04d}" if str(page).isdigit() else str(page)

    # Find the prefix used inside the zip: usually <name>_jp2/<name>_<NNNN>.jp2.
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    candidates = [n for n in names if n.endswith(".jp2") and "_jp2/" in n]
    if not candidates:
        print(f"no JP2 entries found in {zip_path}", file=sys.stderr)
        sys.exit(1)
    # Strip the trailing digits + .jp2 to get the common prefix.
    sample = candidates[0]
    m = re.match(r"^(.*?)(\d+)\.jp2$", sample)
    if not m:
        print(f"could not detect JP2 prefix from {sample}", file=sys.stderr)
        sys.exit(1)
    prefix = m.group(1)
    entry = f"{prefix}{page_str}.jp2"
    if entry not in candidates:
        # Try without zero-padding.
        alt = f"{prefix}{int(page)}.jp2"
        if alt in candidates:
            entry = alt
        else:
            print(f"page {page!r} ({page_str!r}) not in zip; first few entries:\n  " +
                  "\n  ".join(candidates[:5]), file=sys.stderr)
            sys.exit(1)

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(zip_path) as z:
            z.extract(entry, td)
        extracted = Path(td) / entry
        r = subprocess.run(
            ["opj_decompress", "-i", str(extracted), "-o", str(out_path)],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            print(f"opj_decompress failed: {r.stderr.strip()}", file=sys.stderr)
            sys.exit(r.returncode)

    size = out_path.stat().st_size
    print(f"wrote {out_path} ({size:,} bytes) from {entry}")


def cmd_decode_pdf(args):
    """Render a single page of a PDF to PNG. Uses kicad-cli if available, else pdftocairo."""
    import shutil
    import subprocess

    src = Path(args.pdf).resolve()
    if not src.exists():
        print(f"pdf not found: {src}", file=sys.stderr); sys.exit(1)
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    if shutil.which("pdftocairo"):
        # pdftocairo writes <stem>-<page>.png; we'll render to a temp + rename.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            stem = Path(td) / "page"
            r = subprocess.run(
                ["pdftocairo", "-png", "-r", str(args.dpi),
                 "-f", str(args.page), "-l", str(args.page),
                 "-singlefile", str(src), str(stem)],
                capture_output=True, text=True
            )
            if r.returncode != 0:
                print(f"pdftocairo failed: {r.stderr.strip()}", file=sys.stderr); sys.exit(1)
            produced = Path(td) / "page.png"
            if not produced.exists():
                print("pdftocairo produced no output", file=sys.stderr); sys.exit(1)
            produced.replace(out)
        print(f"wrote {out} (pdftocairo, page {args.page}, {args.dpi} dpi)")
    else:
        print("pdftocairo not on PATH (brew install poppler)", file=sys.stderr); sys.exit(2)


def _detect_skew_angle(img_gray, max_angle_deg: float = 10.0):
    """Estimate the rotation needed to make horizontal lines truly horizontal.

    Returns angle in degrees (positive = counter-clockwise correction needed),
    or 0 if no dominant orientation found within ±max_angle_deg.

    Uses HoughLinesP on a thresholded copy: collects line segments, takes
    those near horizontal (within max_angle_deg of 0°), returns the median
    angle. Schematic line-art has plenty of long horizontals so this is
    robust without needing text orientation detection.
    """
    import cv2
    import numpy as np

    _, binary = cv2.threshold(img_gray, 0, 255,
                               cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    H, W = binary.shape
    min_len = max(80, W // 50)  # only count long-ish lines
    lines = cv2.HoughLinesP(
        binary, rho=1, theta=np.pi / 720, threshold=120,
        minLineLength=min_len, maxLineGap=10
    )
    if lines is None:
        return 0.0
    angles = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        ang = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        # Wrap to (-90, 90].
        if ang > 90: ang -= 180
        if ang <= -90: ang += 180
        # Keep near-horizontal lines only.
        if abs(ang) <= max_angle_deg:
            angles.append(ang)
    if len(angles) < 5:
        return 0.0
    return float(np.median(angles))


def _rotate_image(img, angle_deg: float):
    """Rotate `img` counter-clockwise by `angle_deg`. White (255) padding."""
    import cv2
    H, W = img.shape[:2]
    center = (W / 2, H / 2)
    M = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    return cv2.warpAffine(img, M, (W, H),
                           flags=cv2.INTER_CUBIC,
                           borderValue=255)


def cmd_crop_chip(args):
    """Crop a high-resolution image of a chip + surrounding margin.

    Output goes to a path Claude can read. Used in the pin-numbering
    workflow: Claude reads the crop, identifies each pin's number and its
    position in the source image, then calls schematic-graph
    set-pin-positions to commit them. This pattern keeps the OCR / vision
    work on the LLM side, with the skill just producing well-framed
    images of the regions to read.
    """
    try:
        import cv2
    except ImportError:
        print("opencv-python required.", file=sys.stderr); sys.exit(2)

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    bdir = project_root / "boards" / args.board
    graph_path = bdir / "graph.json"
    if not graph_path.exists():
        print(f"no graph: {graph_path}", file=sys.stderr); sys.exit(1)
    graph = json.loads(graph_path.read_text())

    comp = next((c for c in graph["components"] if c["refdes"] == args.refdes), None)
    if not comp:
        print(f"refdes not in graph: {args.refdes}", file=sys.stderr); sys.exit(1)

    sheet = next((s for s in graph["sheets"] if s["index"] == comp["sheet"]), None)
    if not sheet:
        print(f"sheet {comp['sheet']} not in graph", file=sys.stderr); sys.exit(1)

    # Bbox is in scan-pixel coords. If we render fresh from PDF at a higher
    # DPI, scale the bbox by render_size / scan_size so the crop still frames
    # the chip. Padding (in scan px) gets scaled the same way.
    if args.pdf:
        page = args.page if args.page is not None else sheet["index"]
        render_path = _render_pdf_to_temp(Path(args.pdf).resolve(), page, args.dpi)
        img = cv2.imread(str(render_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            print(f"failed to load rendered PDF page: {render_path}", file=sys.stderr); sys.exit(1)
        H, W = img.shape
        scan_w, scan_h = sheet.get("scan_pixel_size") or (W, H)
        scale = W / scan_w if scan_w else 1.0
        source_label = f"PDF {args.pdf} p{page}@{args.dpi}dpi"
    else:
        img_path = (bdir / sheet["scan_path"]).resolve()
        img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            print(f"failed to load: {img_path}", file=sys.stderr); sys.exit(1)
        H, W = img.shape
        scale = 1.0
        source_label = str(img_path)

    bbox = comp["bbox"]
    x1, y1, x2, y2 = (int(v * scale) for v in bbox)
    pad = int(args.pad * scale)
    rx1 = max(0, x1 - pad)
    ry1 = max(0, y1 - pad)
    rx2 = min(W, x2 + pad)
    ry2 = min(H, y2 + pad)

    crop = img[ry1:ry2, rx1:rx2]
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), crop)
    print(f"wrote {out} ({crop.shape[1]}x{crop.shape[0]} px) from {source_label}")
    if scale != 1.0:
        print(f"render scale vs scan: {scale:.3f}× (graph bbox in scan px multiplied to fit render)")
    print(f"crop origin in render: ({rx1}, {ry1})")
    print(f"chip bbox in render: ({x1}, {y1}) → ({x2}, {y2})")
    if scale != 1.0:
        print(f"to translate render (cx, cy) → graph/scan coords: "
              f"((cx + {rx1}) / {scale:.6f}, (cy + {ry1}) / {scale:.6f})")
    else:
        print(f"to convert crop-local (cx, cy) → source: (cx + {rx1}, cy + {ry1})")


def cmd_clean(args):
    """Cleanup pass: contrast stretch, optional median denoise, optional
    deskew, optional Otsu binarize. Operations apply in this order:

      1. denoise (median blur)
      2. deskew (Hough-based rotation correction)
      3. contrast stretch
      4. threshold

    All four are independent flags.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("opencv-python required.", file=sys.stderr); sys.exit(2)

    src = Path(args.input).resolve()
    if not src.exists():
        print(f"input not found: {src}", file=sys.stderr); sys.exit(1)
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    img = cv2.imread(str(src), cv2.IMREAD_GRAYSCALE)
    if img is None:
        print(f"failed to load: {src}", file=sys.stderr); sys.exit(1)

    ops = []

    # 1. Denoise (median preserves edges; non-local means is heavier).
    if args.denoise:
        img = cv2.medianBlur(img, args.denoise_size)
        ops.append(f"median{args.denoise_size}")

    # 2. Deskew.
    skew_angle = 0.0
    if args.deskew:
        skew_angle = _detect_skew_angle(img, max_angle_deg=args.deskew_max_deg)
        if abs(skew_angle) > 0.05:
            img = _rotate_image(img, skew_angle)
            ops.append(f"deskew{skew_angle:+.2f}°")
        else:
            ops.append("deskew=0°")

    # 3. Percentile-based contrast stretch — robust to outliers.
    lo = np.percentile(img, args.lo_pct)
    hi = np.percentile(img, args.hi_pct)
    if hi <= lo:
        print(f"contrast stretch degenerate (lo={lo}, hi={hi}); skipping stretch",
              file=sys.stderr)
    else:
        img = np.clip(
            (img.astype(np.float32) - lo) * 255.0 / (hi - lo),
            0, 255).astype(np.uint8)
        ops.append(f"contrast{lo:.1f}..{hi:.1f}")

    # 4. Optional binarization for downstream CV.
    if args.threshold:
        _, img = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        ops.append("otsu")

    cv2.imwrite(str(out), img)
    print(f"wrote {out} ({src.stat().st_size:,}→{out.stat().st_size:,} bytes; "
          f"ops: {', '.join(ops) if ops else 'none'})")


def main():
    ap = argparse.ArgumentParser(prog="cartographer", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("tile", help="cut a sheet PNG into overlapping tiles")
    sp.add_argument("input", nargs="?", help="source PNG (or use --pdf to render on demand)")
    sp.add_argument("--out", required=True)
    sp.add_argument("--grid", default="3x3", help="ROWSxCOLS, default 3x3")
    sp.add_argument("--overlap", type=float, default=0.1, help="fractional overlap, default 0.1")
    sp.add_argument("--pdf", help="render this PDF page on the fly (alternative to a PNG input)")
    sp.add_argument("--page", type=int, default=1, help="PDF page (1-based, used with --pdf)")
    sp.add_argument("--dpi", type=int, default=600, help="DPI for --pdf rendering (default 600)")
    sp.set_defaults(fn=cmd_tile)

    sp = sub.add_parser("to-source", help="translate a tile-local bbox to source coords")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--tile", required=True)
    sp.add_argument("--bbox", required=True)
    sp.set_defaults(fn=cmd_to_source)

    sp = sub.add_parser("snap-bbox", help="snap a tentative bbox to the nearest chip-outline rectangle")
    sp.add_argument("--image", required=True, help="source PNG path")
    sp.add_argument("--bbox", required=True, help="x1,y1,x2,y2 in source pixels")
    sp.add_argument("--search-pad", type=int, default=120, help="pixels of search margin around bbox")
    sp.add_argument("--line-min-len", type=int, default=30, help="min straight-line length to count as outline")
    sp.add_argument("--min-size", type=int, default=50, help="min width/height of the chip-interior hole")
    sp.add_argument("--max-size", type=int, default=500, help="max width/height of the chip-interior hole")
    sp.add_argument("--debug", help="path to write a debug PNG (red=original, green=snapped)")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(fn=cmd_snap_bbox)

    sp = sub.add_parser("snap-board", help="snap every component on a board's graph.json")
    sp.add_argument("--board", required=True)
    sp.add_argument("--sheet", type=int, help="restrict to one sheet index")
    sp.add_argument("--search-pad", type=int, default=120)
    sp.add_argument("--line-min-len", type=int, default=30)
    sp.add_argument("--min-size", type=int, default=50)
    sp.add_argument("--max-size", type=int, default=500)
    sp.add_argument("--include-verified", action="store_true",
                    help="also re-snap components flagged verified (default skips them)")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(fn=cmd_snap_board)

    sp = sub.add_parser("crop-chip",
                        help="crop a high-res image of one chip + margin "
                             "for Claude to read pin numbers / labels from")
    sp.add_argument("--board", required=True)
    sp.add_argument("--refdes", required=True)
    sp.add_argument("--pad", type=int, default=80,
                    help="padding (in scan-px) around the bbox (default 80)")
    sp.add_argument("--out", required=True, help="output PNG path")
    sp.add_argument("--pdf", help="render the chip from this PDF page instead of the canonical scan "
                                  "(use when scan text is too small; bbox is auto-rescaled)")
    sp.add_argument("--page", type=int, help="PDF page (1-based, used with --pdf)")
    sp.add_argument("--dpi", type=int, default=600, help="DPI for --pdf rendering (default 600)")
    sp.set_defaults(fn=cmd_crop_chip)

    sp = sub.add_parser("decode-jp2",
                        help="decode one JP2 page from an archive.org-style zip → PNG (needs opj_decompress)")
    sp.add_argument("zip", help="archive.org JP2 zip")
    sp.add_argument("page", help="page number (1-based, integer or 0-padded string)")
    sp.add_argument("--out", required=True, help="output PNG path")
    sp.set_defaults(fn=cmd_decode_jp2)

    sp = sub.add_parser("decode-pdf",
                        help="render one page of a PDF → PNG (needs pdftocairo from poppler)")
    sp.add_argument("pdf")
    sp.add_argument("--page", type=int, default=1)
    sp.add_argument("--dpi", type=int, default=300)
    sp.add_argument("--out", required=True)
    sp.set_defaults(fn=cmd_decode_pdf)

    sp = sub.add_parser("clean",
                        help="contrast/denoise/deskew/threshold a scan")
    sp.add_argument("input")
    sp.add_argument("--out", required=True)
    sp.add_argument("--lo-pct", type=float, default=2.0,
                    help="lower percentile for contrast stretch (default 2.0)")
    sp.add_argument("--hi-pct", type=float, default=98.0,
                    help="upper percentile for contrast stretch (default 98.0)")
    sp.add_argument("--denoise", action="store_true",
                    help="apply a median-blur denoise pass before contrast stretch")
    sp.add_argument("--denoise-size", type=int, default=3,
                    help="median blur kernel size (must be odd, default 3)")
    sp.add_argument("--deskew", action="store_true",
                    help="rotate to make horizontal Hough lines truly horizontal")
    sp.add_argument("--deskew-max-deg", type=float, default=10.0,
                    help="ignore detected angles beyond ±this many degrees (default 10)")
    sp.add_argument("--threshold", action="store_true",
                    help="also Otsu-threshold to binary")
    sp.set_defaults(fn=cmd_clean)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
