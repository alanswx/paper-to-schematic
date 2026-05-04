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


EDGE_TYPES = ("wire", "label", "sheet_zone", "off_page", "bus", "implicit_power")
NET_KINDS = ("signal", "power", "ground", "clock", "bus_member")


def parse_endpoints(spec: str):
    """Parse 'refdes.pin,refdes.pin,...' → list of (refdes, pin)."""
    out = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "." not in tok:
            raise SystemExit(f"endpoint malformed (no '.'): {tok!r}")
        refdes, pin = tok.rsplit(".", 1)
        out.append((refdes.strip(), pin.strip()))
    if len(out) < 2:
        raise SystemExit(f"need at least 2 endpoints, got {len(out)}")
    return out


def cmd_add_net(args):
    graph = load_graph(args.board)
    if "nets" not in graph:
        graph["nets"] = []
    if any(n["name"] == args.name for n in graph["nets"]):
        print(f"net name already exists: {args.name}", file=sys.stderr)
        sys.exit(1)
    if args.kind not in NET_KINDS:
        print(f"--kind must be one of {NET_KINDS}", file=sys.stderr); sys.exit(1)
    if args.edge_type not in EDGE_TYPES:
        print(f"--edge-type must be one of {EDGE_TYPES}", file=sys.stderr); sys.exit(1)

    eps = parse_endpoints(args.endpoints)
    refdes_index = {c["refdes"]: c for c in graph["components"]}
    out_eps = []
    for refdes, pin in eps:
        comp = refdes_index.get(refdes)
        if not comp:
            print(f"endpoint {refdes}.{pin}: refdes not in graph", file=sys.stderr); sys.exit(1)
        ep = {
            "refdes": refdes,
            "pin": int(pin) if pin.isdigit() else pin,
            "sheet": comp.get("sheet"),
            "edge_type": args.edge_type,
        }
        if args.edge_type == "sheet_zone":
            if not args.zone_ref:
                print("sheet_zone edges require --zone-ref (e.g. 4C6)", file=sys.stderr); sys.exit(1)
            ep["sheet_zone_ref"] = args.zone_ref
        ep["evidence"] = {"source": args.source}
        if args.note:
            ep["evidence"]["note"] = args.note
        out_eps.append(ep)

    net = {"name": args.name, "kind": args.kind, "endpoints": out_eps}
    graph["nets"].append(net)
    save_graph(args.board, graph)
    print(f"added net {args.name} ({args.kind}, {args.edge_type}) "
          f"with {len(out_eps)} endpoints: " +
          ", ".join(f"{e['refdes']}.{e['pin']}" for e in out_eps))


def cmd_remove_net(args):
    graph = load_graph(args.board)
    nets = graph.get("nets", [])
    n_before = len(nets)
    graph["nets"] = [n for n in nets if n["name"] != args.name]
    if len(graph["nets"]) == n_before:
        print(f"no such net: {args.name}", file=sys.stderr); sys.exit(1)
    save_graph(args.board, graph)
    print(f"removed net {args.name}")


def cmd_list_nets(args):
    graph = load_graph(args.board)
    nets = graph.get("nets", [])
    if args.sheet is not None:
        nets = [n for n in nets if any(e.get("sheet") == args.sheet for e in n["endpoints"])]
    label = f" touching sheet {args.sheet}" if args.sheet is not None else ""
    print(f"{len(nets)} nets{label}:\n")
    for net in sorted(nets, key=lambda n: n["name"]):
        eps = " ⟷ ".join(f"{e['refdes']}.{e['pin']}" for e in net["endpoints"])
        et = net["endpoints"][0].get("edge_type", "?") if net["endpoints"] else "?"
        kind = net.get("kind", "?")
        print(f"  {net['name']:10s}  [{kind}/{et}]  {eps}")


def _find_kicad_cli():
    """Return path to kicad-cli, or None."""
    import shutil
    p = shutil.which("kicad-cli")
    if p:
        return p
    mac = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    if Path(mac).exists():
        return mac
    return None


def cmd_export_kicad(args):
    import sys
    sys.path.insert(0, str(SKILL_DIR))
    import kicad_export

    graph = load_graph(args.board)
    chips = load_chips()
    out_dir = Path(args.out_dir) if args.out_dir else (board_dir(args.board) / "kicad")
    out_dir.mkdir(parents=True, exist_ok=True)

    sheets = ([s["index"] for s in graph["sheets"] if s["index"] == args.sheet]
              if args.sheet is not None
              else [s["index"] for s in graph["sheets"]])
    if not sheets:
        print(f"no matching sheet(s)", file=sys.stderr)
        sys.exit(1)

    written = []
    for idx in sheets:
        sheet_meta = next(s for s in graph["sheets"] if s["index"] == idx)
        # Filename: <board>_sheet<N>_<title>.kicad_sch (slugified title)
        title = sheet_meta.get("title", f"sheet{idx}").lower()
        slug = "".join(c if c.isalnum() else "_" for c in title).strip("_")
        fname = f"{args.board}_s{idx}_{slug}.kicad_sch"
        fpath = out_dir / fname
        text = kicad_export.gen_sch(graph, chips, idx, project_name=args.board)
        fpath.write_text(text)
        written.append(fpath)
        print(f"  wrote {fpath} ({len(text):,} bytes)")

    if args.validate:
        print("\n--- validation ---")
        for fp in written:
            try:
                tree = kicad_export.parse_sexp(fp.read_text())
                # Check root is (kicad_sch ...).
                if tree[0] != "kicad_sch":
                    print(f"  FAIL {fp.name}: root is {tree[0]!r}")
                    continue
                # Count interesting children.
                n_libs = 0
                n_syms = 0
                n_wires = 0
                for child in tree[1:]:
                    if isinstance(child, list) and child:
                        if child[0] == "lib_symbols":
                            n_libs = sum(1 for c in child[1:] if isinstance(c, list) and c and c[0] == "symbol")
                        elif child[0] == "symbol":
                            n_syms += 1
                        elif child[0] == "wire":
                            n_wires += 1
                print(f"  ok   {fp.name} — sexp valid, {n_libs} lib symbols, {n_syms} components, {n_wires} wires")
            except Exception as e:
                print(f"  FAIL {fp.name}: {e}")

        cli = _find_kicad_cli()
        if cli:
            import subprocess
            print(f"\n--- kicad-cli sch erc ({cli}) ---")
            for fp in written:
                rep = fp.with_suffix(".erc.txt")
                r = subprocess.run(
                    [cli, "sch", "erc", "--severity-all", "--output", str(rep), str(fp)],
                    capture_output=True, text=True, timeout=120
                )
                # ERC may exit non-zero if violations exist (without --exit-code-violations
                # it should be zero unless the file fails to load).
                if r.returncode != 0:
                    print(f"  WARN {fp.name}: kicad-cli rc={r.returncode}")
                    if r.stderr.strip():
                        print(f"        stderr: {r.stderr.strip()[:300]}")
                else:
                    print(f"  ok   {fp.name}: kicad-cli loaded the file (rc=0)")
                if rep.exists():
                    rep_text = rep.read_text()
                    # Count violations from the ERC report header
                    import re
                    m = re.search(r'(\d+)\s+ERC violation', rep_text)
                    if m:
                        print(f"        ERC report: {m.group(1)} violation(s) → {rep.name}")
                    else:
                        print(f"        ERC report → {rep.name}")
        else:
            print("\n(kicad-cli not found on PATH or at /Applications/KiCad/...; skipping ERC check)")


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

    refdes_index = {c["refdes"]: c for c in graph["components"]}
    net_names = set()
    for net in graph.get("nets", []):
        name = net.get("name")
        if not name:
            errs.append(f"net missing name: {net}"); continue
        if name in net_names:
            errs.append(f"duplicate net name: {name}")
        net_names.add(name)
        eps = net.get("endpoints", [])
        if len(eps) < 2:
            errs.append(f"net {name}: needs ≥2 endpoints, has {len(eps)}")
        edge_types = {ep.get("edge_type") for ep in eps}
        if len(edge_types) > 1:
            errs.append(f"net {name}: mixed edge_types {edge_types} (one net should use one type)")
        for ep in eps:
            if ep.get("refdes") not in refdes_index:
                errs.append(f"net {name}: endpoint refdes {ep.get('refdes')!r} not in graph")
            et = ep.get("edge_type")
            if et not in EDGE_TYPES:
                errs.append(f"net {name}: bad edge_type {et!r}")
            if et == "sheet_zone" and not ep.get("sheet_zone_ref"):
                errs.append(f"net {name}: sheet_zone endpoint missing sheet_zone_ref")

    if errs:
        print(f"FAIL — {len(errs)} issues:")
        for e in errs:
            print(f"  {e}")
        sys.exit(1)
    print(f"ok — {len(graph['components'])} components, "
          f"{len(graph.get('nets', []))} nets, all parts in librarian")


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

    sp = sub.add_parser("add-net", help="add a net with 2+ endpoints sharing one edge_type")
    sp.add_argument("--board", required=True)
    sp.add_argument("--name", required=True, help="unique net name, e.g. /CSC, A0, GND")
    sp.add_argument("--kind", default="signal",
                    choices=NET_KINDS)
    sp.add_argument("--edge-type", required=True,
                    choices=EDGE_TYPES,
                    help="all endpoints share this edge_type")
    sp.add_argument("--endpoints", required=True,
                    help="comma-separated refdes.pin list, e.g. 'U14C.1,U13C.5,U12B.7'")
    sp.add_argument("--zone-ref", help="sheet-zone reference (required when edge-type=sheet_zone), e.g. 4C6")
    sp.add_argument("--source", choices=["ai", "human", "datasheet", "probe"], default="ai")
    sp.add_argument("--note")
    sp.set_defaults(fn=cmd_add_net)

    sp = sub.add_parser("remove-net")
    sp.add_argument("--board", required=True)
    sp.add_argument("--name", required=True)
    sp.set_defaults(fn=cmd_remove_net)

    sp = sub.add_parser("list-nets")
    sp.add_argument("--board", required=True)
    sp.add_argument("--sheet", type=int, help="restrict to nets with at least one endpoint on this sheet")
    sp.set_defaults(fn=cmd_list_nets)

    sp = sub.add_parser("export-kicad", help="emit a .kicad_sch file per sheet")
    sp.add_argument("--board", required=True)
    sp.add_argument("--sheet", type=int, help="single sheet (default: all sheets)")
    sp.add_argument("--out-dir", help="output directory (default: boards/<id>/kicad/)")
    sp.add_argument("--validate", action="store_true",
                    help="parse output as s-expression after writing; if KiCad CLI is on PATH or at the standard macOS location, also run sch erc")
    sp.set_defaults(fn=cmd_export_kicad)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
