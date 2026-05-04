#!/usr/bin/env python3
"""Schematic-graph CLI — manage boards/<id>/graph.json.

Read SKILL.md before invoking.
"""
import argparse
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SKILL_DIR.parent.parent.parent
LIBRARIAN_DIR = PROJECT_ROOT / ".agents" / "skills" / "librarian"


def board_dir(board_id: str) -> Path:
    return PROJECT_ROOT / "boards" / board_id


def graph_path(board_id: str) -> Path:
    return board_dir(board_id) / "graph.json"


def empty_graph(board: dict) -> dict:
    return {
        "board": {
            "id": board["id"],
            "drawing_number": board["drawing_number"],
            "title": board.get("title", ""),
            "manufacturer": board.get("manufacturer", ""),
            "year": board.get("year"),
        },
        "sheets": [
            {
                "index": s["index"],
                "title": s["title"],
                "scan_path": s["scan_path"],
                "scan_pixel_size": s.get("scan_pixel_size"),
            }
            for s in board["sheets"]
        ],
        "components": [],
        "nets": [],
    }


def load_graph(board_id: str) -> dict:
    p = graph_path(board_id)
    if p.exists():
        return json.loads(p.read_text())
    board = json.loads((board_dir(board_id) / "board.json").read_text())
    return empty_graph(board)


def save_graph(board_id: str, graph: dict) -> None:
    graph_path(board_id).write_text(json.dumps(graph, indent=2) + "\n")


def load_chips() -> dict:
    return json.loads((LIBRARIAN_DIR / "chips.json").read_text())


def find_part(chips: dict, query: str):
    if query in chips["parts"]:
        return query
    for name, p in chips["parts"].items():
        if query in p.get("aliases", []):
            return name
    return None


def parse_bbox(s: str):
    try:
        vals = [float(v) for v in s.split(",")]
    except ValueError:
        raise SystemExit(f"--bbox must be x1,y1,x2,y2 in numbers; got {s!r}")
    if len(vals) != 4:
        raise SystemExit(f"--bbox needs 4 values; got {len(vals)}")
    if vals[0] >= vals[2] or vals[1] >= vals[3]:
        raise SystemExit(f"degenerate bbox (x1>=x2 or y1>=y2): {vals}")
    return vals


def cmd_add_component(args):
    graph = load_graph(args.board)
    if any(c["refdes"] == args.refdes for c in graph["components"]):
        print(f"refdes already exists: {args.refdes}", file=sys.stderr)
        sys.exit(1)
    chips = load_chips()
    canon = find_part(chips, args.part)
    if not canon:
        print(f"unknown part: {args.part!r} — add via librarian skill first",
              file=sys.stderr)
        sys.exit(1)
    bbox = parse_bbox(args.bbox)
    comp = {
        "refdes": args.refdes,
        "part": canon,
        "sheet": args.sheet,
        "bbox": bbox,
        "evidence": {"source": args.source},
    }
    if args.confidence is not None:
        comp["evidence"]["confidence"] = args.confidence
    if args.note:
        comp["evidence"]["note"] = args.note
    graph["components"].append(comp)
    save_graph(args.board, graph)
    print(f"added {args.refdes} ({canon}) on sheet {args.sheet} bbox={bbox} "
          f"source={args.source}")


def _set_verified(args, value: bool):
    graph = load_graph(args.board)
    comp = next((c for c in graph["components"] if c["refdes"] == args.refdes), None)
    if not comp:
        print(f"no such component: {args.refdes}", file=sys.stderr)
        sys.exit(1)
    if value:
        from datetime import datetime, timezone
        comp["verified"] = True
        comp["verified_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if args.by:
            comp["verified_by"] = args.by
    else:
        comp.pop("verified", None)
        comp.pop("verified_at", None)
        comp.pop("verified_by", None)
    save_graph(args.board, graph)
    state = "verified" if value else "unverified"
    print(f"{state} {args.refdes}")


def cmd_verify_component(args):
    _set_verified(args, True)


def cmd_unverify_component(args):
    _set_verified(args, False)


def cmd_remove_component(args):
    graph = load_graph(args.board)
    n_before = len(graph["components"])
    graph["components"] = [c for c in graph["components"] if c["refdes"] != args.refdes]
    if len(graph["components"]) == n_before:
        print(f"no such component: {args.refdes}", file=sys.stderr)
        sys.exit(1)
    save_graph(args.board, graph)
    print(f"removed {args.refdes}")


def cmd_list_components(args):
    graph = load_graph(args.board)
    comps = graph["components"]
    if args.sheet is not None:
        comps = [c for c in comps if c["sheet"] == args.sheet]
    label = f" on sheet {args.sheet}" if args.sheet is not None else ""
    print(f"{len(comps)} components{label}:\n")
    for c in sorted(comps, key=lambda c: (c["sheet"], c["refdes"])):
        bbox = c.get("bbox", [])
        bbox_str = (f"({bbox[0]:.0f},{bbox[1]:.0f})→({bbox[2]:.0f},{bbox[3]:.0f})"
                    if len(bbox) == 4 else "?")
        ev = c.get("evidence", {})
        src = ev.get("source", "?")
        conf = f" {ev['confidence']:.2f}" if "confidence" in ev else ""
        v = " ✓" if c.get("verified") else "  "
        print(f" {v} s{c['sheet']}  {c['refdes']:8s}  {c['part']:10s}  "
              f"{bbox_str}  [{src}{conf}]")


def cmd_validate(args):
    graph = load_graph(args.board)
    chips = load_chips()
    errs = []
    seen = set()
    for c in graph["components"]:
        rd = c.get("refdes")
        if not rd:
            errs.append(f"component missing refdes: {c}")
            continue
        if rd in seen:
            errs.append(f"duplicate refdes: {rd}")
        seen.add(rd)
        if not find_part(chips, c.get("part", "")):
            errs.append(f"{rd}: unknown part {c.get('part')!r}")
        bbox = c.get("bbox", [])
        if len(bbox) != 4:
            errs.append(f"{rd}: bbox malformed: {bbox}")
        elif bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            errs.append(f"{rd}: degenerate bbox: {bbox}")
        if "sheet" not in c:
            errs.append(f"{rd}: missing sheet")
    if errs:
        print(f"FAIL — {len(errs)} issues:")
        for e in errs:
            print(f"  {e}")
        sys.exit(1)
    print(f"ok — {len(graph['components'])} components, "
          f"{len(graph['nets'])} nets, all parts in librarian")


def main():
    ap = argparse.ArgumentParser(prog="schematic-graph",
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("add-component")
    sp.add_argument("--board", required=True)
    sp.add_argument("--refdes", required=True)
    sp.add_argument("--part", required=True, help="librarian key or alias")
    sp.add_argument("--sheet", type=int, required=True)
    sp.add_argument("--bbox", required=True, help="x1,y1,x2,y2 in source-image pixels")
    sp.add_argument("--source", choices=["ai", "human", "datasheet", "probe"], default="ai")
    sp.add_argument("--confidence", type=float)
    sp.add_argument("--note")
    sp.set_defaults(fn=cmd_add_component)

    sp = sub.add_parser("remove-component")
    sp.add_argument("--board", required=True)
    sp.add_argument("--refdes", required=True)
    sp.set_defaults(fn=cmd_remove_component)

    sp = sub.add_parser("verify-component", help="mark component as human-verified")
    sp.add_argument("--board", required=True)
    sp.add_argument("--refdes", required=True)
    sp.add_argument("--by", help="attribution (optional)")
    sp.set_defaults(fn=cmd_verify_component)

    sp = sub.add_parser("unverify-component", help="clear the verified flag")
    sp.add_argument("--board", required=True)
    sp.add_argument("--refdes", required=True)
    sp.add_argument("--by")
    sp.set_defaults(fn=cmd_unverify_component)

    sp = sub.add_parser("list-components")
    sp.add_argument("--board", required=True)
    sp.add_argument("--sheet", type=int)
    sp.set_defaults(fn=cmd_list_components)

    sp = sub.add_parser("validate")
    sp.add_argument("--board", required=True)
    sp.set_defaults(fn=cmd_validate)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
