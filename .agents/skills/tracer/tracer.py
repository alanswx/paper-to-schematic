#!/usr/bin/env python3
"""Tracer — CV-assisted wire detection on schematic sheets.

Read SKILL.md before invoking. Run via the project venv:
  .venv/bin/python .agents/skills/tracer/tracer.py <cmd> ...
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SKILL_DIR.parent.parent.parent


def board_dir(board_id: str) -> Path:
    return PROJECT_ROOT / "boards" / board_id


def trace_sheet(image_path: Path, components, *,
                line_min_len: int = 25,
                bbox_inset: int = 4,
                pin_search_radius: int = 25,
                debug_path: Path | None = None):
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("opencv-python and numpy required. Install via .venv:\n"
              "  .venv/bin/pip install -r .agents/skills/cartographer/requirements.txt",
              file=sys.stderr)
        sys.exit(2)

    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"failed to load image: {image_path}")
    H, W = img.shape

    # Threshold (inverted: outlines = white, paper = black).
    _, binary = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # Mask out chip bodies. Inset so we don't erase pin tick marks at the edges.
    masked = binary.copy()
    for comp in components:
        if "bbox" not in comp:
            continue
        x1, y1, x2, y2 = comp["bbox"]
        ix1 = int(x1 + bbox_inset)
        iy1 = int(y1 + bbox_inset)
        ix2 = int(x2 - bbox_inset)
        iy2 = int(y2 - bbox_inset)
        if ix1 < ix2 and iy1 < iy2:
            cv2.rectangle(masked, (ix1, iy1), (ix2, iy2), 0, -1)

    # Extract long horizontal and vertical segments — drops pin ticks (short)
    # and text characters (small connected regions).
    horiz = cv2.morphologyEx(
        masked, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (line_min_len, 1)))
    vert = cv2.morphologyEx(
        masked, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, line_min_len)))
    skeleton = cv2.bitwise_or(horiz, vert)

    # Bridge right-angle corners and small gaps so a wire-with-bends is one
    # connected component instead of multiple segments.
    skeleton = cv2.morphologyEx(
        skeleton, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1)

    # Connected components.
    n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        skeleton, connectivity=8)

    # For each pin, vote for the most-common nearby skeleton label.
    pin_to_label = {}
    for comp in components:
        if not comp.get("pin_positions"):
            continue
        for pin_num, (px, py) in comp["pin_positions"].items():
            px_i, py_i = int(px), int(py)
            x0 = max(0, px_i - pin_search_radius)
            y0 = max(0, py_i - pin_search_radius)
            x1 = min(W, px_i + pin_search_radius)
            y1 = min(H, py_i + pin_search_radius)
            window = labels[y0:y1, x0:x1]
            nonzero = window[window > 0]
            if nonzero.size == 0:
                continue
            counts = Counter(int(v) for v in nonzero.flatten())
            lbl, _ = counts.most_common(1)[0]
            pin_to_label[(comp["refdes"], str(pin_num))] = lbl

    # Group pins by label.
    groups: dict[int, list[tuple[str, str]]] = {}
    for pin, lbl in pin_to_label.items():
        groups.setdefault(lbl, []).append(pin)

    proposed = []
    for lbl, pins in sorted(groups.items()):
        if len(pins) < 2:
            continue
        area = int(stats[lbl, cv2.CC_STAT_AREA])
        proposed.append({
            "label": int(lbl),
            "endpoints": [{"refdes": r, "pin": p} for (r, p) in pins],
            "confidence": 0.5,  # tracer is approximate; HITL refines
            "skeleton_area_px": area,
        })

    if debug_path:
        # Write the line skeleton overlaid on a faded copy of the original.
        bg = (img // 2 + 128).astype(np.uint8)
        debug = cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)
        debug[skeleton > 0] = (0, 255, 0)
        # Draw pin positions as circles, color-coded by whether they were
        # successfully assigned to a skeleton component.
        for comp in components:
            if not comp.get("pin_positions"):
                continue
            for pin_num, (px, py) in comp["pin_positions"].items():
                color = (255, 200, 50) if (comp["refdes"], str(pin_num)) in pin_to_label else (50, 50, 255)
                cv2.circle(debug, (int(px), int(py)), 5, color, -1)
        # Draw bbox outlines for context.
        for comp in components:
            if "bbox" not in comp:
                continue
            x1, y1, x2, y2 = (int(v) for v in comp["bbox"])
            cv2.rectangle(debug, (x1, y1), (x2, y2), (50, 50, 50), 1)
        cv2.imwrite(str(debug_path), debug)

    return {
        "skeleton_pixels": int((skeleton > 0).sum()),
        "components_found": int(n_labels - 1),
        "pins_assigned": len(pin_to_label),
        "proposed_nets": proposed,
    }


def cmd_trace(args):
    graph_path = board_dir(args.board) / "graph.json"
    if not graph_path.exists():
        print(f"no graph: {graph_path}", file=sys.stderr)
        sys.exit(1)
    graph = json.loads(graph_path.read_text())

    sheet_meta = next((s for s in graph["sheets"] if s["index"] == args.sheet), None)
    if not sheet_meta:
        print(f"sheet {args.sheet} not in board {args.board}", file=sys.stderr)
        sys.exit(1)
    image_path = (board_dir(args.board) / sheet_meta["scan_path"]).resolve()

    components = [c for c in graph["components"] if c["sheet"] == args.sheet]
    print(f"tracing {image_path.name} ({len(components)} components on sheet {args.sheet})")
    print(f"  line-min-len={args.line_min_len}  pin-search-radius={args.pin_search_radius}")

    result = trace_sheet(
        image_path, components,
        line_min_len=args.line_min_len,
        bbox_inset=args.bbox_inset,
        pin_search_radius=args.pin_search_radius,
        debug_path=Path(args.debug) if args.debug else None,
    )

    out = {
        "board": args.board,
        "sheet": args.sheet,
        "skeleton_pixels": result["skeleton_pixels"],
        "components_found": result["components_found"],
        "pins_assigned": result["pins_assigned"],
        "proposed_nets": result["proposed_nets"],
    }
    out_path = Path(args.out) if args.out else (board_dir(args.board) / f"sheet{args.sheet}_traces.json")
    out_path.write_text(json.dumps(out, indent=2))
    n = len(result["proposed_nets"])
    big = sum(1 for n_ in result["proposed_nets"] if len(n_["endpoints"]) > 8)
    print(f"  skeleton={result['skeleton_pixels']:,} px  cc={result['components_found']:,}  "
          f"pins assigned={result['pins_assigned']}")
    print(f"  proposed nets: {n} (with >8 endpoints, likely buses or over-connected: {big})")
    print(f"  wrote {out_path}")
    if args.debug:
        print(f"  debug image: {args.debug}")


def main():
    ap = argparse.ArgumentParser(prog="tracer", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("trace", help="propose nets by tracing wires on a sheet")
    sp.add_argument("--board", required=True)
    sp.add_argument("--sheet", type=int, required=True)
    sp.add_argument("--out", help="output JSON path")
    sp.add_argument("--line-min-len", type=int, default=25,
                    help="min line length to count as a wire (default: 25)")
    sp.add_argument("--bbox-inset", type=int, default=4,
                    help="how far to inset chip-bbox masks (default: 4 px)")
    sp.add_argument("--pin-search-radius", type=int, default=25,
                    help="how far from each pin to search for a connected skeleton component")
    sp.add_argument("--debug", help="write a debug PNG showing skeleton + pin assignments")
    sp.set_defaults(fn=cmd_trace)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
