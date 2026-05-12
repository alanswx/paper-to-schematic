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
    # Discretes (R, C, Crystal, …) carry an instance-level value. Refuse to
    # add a value_required part without one, since the BOM aggregates by it.
    part_def = chips["parts"][canon]
    if args.value:
        comp["value"] = args.value
    elif part_def.get("value_required"):
        print(f"part {canon!r} requires --value (resistance/capacitance/etc.)",
              file=sys.stderr); sys.exit(1)
    graph["components"].append(comp)
    save_graph(args.board, graph)
    val_str = f" value={args.value!r}" if args.value else ""
    print(f"added {args.refdes} ({canon}) on sheet {args.sheet} bbox={bbox}{val_str} "
          f"source={args.source}")


def cmd_set_pin_positions(args):
    """Replace a component's pin_positions from a JSON map of pin→[x,y].

    Used by the Claude-driven pin-numbering workflow: cartographer crop-chip
    produces a high-res image of the chip; Claude reads each pin number and
    its position in source coordinates; this command commits them.

    --json accepts either inline JSON ('{\"1\":[100,200],...}') or @file.
    """
    graph = load_graph(args.board)
    comp = next((c for c in graph["components"] if c["refdes"] == args.refdes), None)
    if not comp:
        print(f"refdes not in graph: {args.refdes}", file=sys.stderr); sys.exit(1)

    raw = args.json
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text()
    data = json.loads(raw)

    if not isinstance(data, dict):
        print("--json must be an object: {pin_number: [x,y], ...}", file=sys.stderr)
        sys.exit(1)
    out = {}
    for k, v in data.items():
        if not isinstance(v, list) or len(v) != 2:
            print(f"pin {k}: expected [x,y], got {v}", file=sys.stderr); sys.exit(1)
        out[str(k)] = [float(v[0]), float(v[1])]

    if args.merge:
        existing = comp.get("pin_positions") or {}
        existing.update(out)
        comp["pin_positions"] = existing
    else:
        comp["pin_positions"] = out
    save_graph(args.board, graph)
    mode = "merged" if args.merge else "replaced"
    print(f"{mode} {len(out)} pin position(s) on {args.refdes}")


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


def cmd_set_body_bbox(args):
    """Attach a tight body_bbox (chip-outline rectangle) to a component.
    bbox stays as the click-target / pin-area extent; body_bbox is what the
    KiCad export uses to size the rendered symbol body. Pass --clear to
    revert to the click-target bbox as the body."""
    graph = load_graph(args.board)
    comp = next((c for c in graph["components"] if c["refdes"] == args.refdes), None)
    if not comp:
        print(f"no such refdes: {args.refdes}", file=sys.stderr); sys.exit(1)
    if args.clear:
        comp.pop("body_bbox", None)
        save_graph(args.board, graph)
        print(f"cleared body_bbox on {args.refdes}")
        return
    if not args.bbox:
        print("provide --bbox x1,y1,x2,y2 or --clear", file=sys.stderr); sys.exit(1)
    bb = parse_bbox(args.bbox)
    # Sanity: body_bbox should sit inside the click-target bbox (with some
    # slack for floating-point). Hard to enforce strictly without false
    # positives, but warn on egregious cases.
    cb = comp.get("bbox") or [0, 0, 0, 0]
    if (bb[0] < cb[0] - 5 or bb[1] < cb[1] - 5 or
            bb[2] > cb[2] + 5 or bb[3] > cb[3] + 5):
        print(f"warning: body_bbox {bb} extends outside click-target bbox {cb} — "
              f"unusual; double-check both rectangles", file=sys.stderr)
    comp["body_bbox"] = bb
    save_graph(args.board, graph)
    print(f"set body_bbox on {args.refdes}: {bb}")


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
    if len(out) < 1:
        raise SystemExit(f"need at least 1 endpoint, got {len(out)}")
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


def cmd_extend_net(args):
    """Append endpoints to an existing net. Cross-sheet labels (MCD.0, RCPR0',
    TTLTrue.D, …) naturally accumulate endpoints as more sheets are
    transcribed; this is the first-class verb for that. The new endpoints
    must share the existing net's edge_type; duplicates are skipped; if the
    net doesn't exist, error (use add-net to create)."""
    graph = load_graph(args.board)
    net = next((n for n in graph.get("nets", []) if n["name"] == args.name), None)
    if not net:
        print(f"no such net: {args.name} (use add-net to create)", file=sys.stderr)
        sys.exit(1)
    existing_et = net["endpoints"][0].get("edge_type") if net["endpoints"] else None
    edge_type = args.edge_type or existing_et
    if not edge_type:
        print(f"net {args.name} has no endpoints with edge_type and --edge-type wasn't given",
              file=sys.stderr); sys.exit(1)
    if existing_et and edge_type != existing_et:
        print(f"--edge-type {edge_type!r} disagrees with existing endpoints' {existing_et!r}",
              file=sys.stderr); sys.exit(1)
    if edge_type not in EDGE_TYPES:
        print(f"--edge-type must be one of {EDGE_TYPES}", file=sys.stderr); sys.exit(1)

    eps = parse_endpoints(args.add_endpoints)
    refdes_index = {c["refdes"]: c for c in graph["components"]}
    existing_keys = {(e["refdes"], str(e["pin"])) for e in net["endpoints"]}
    added, skipped = [], []
    for refdes, pin in eps:
        comp = refdes_index.get(refdes)
        if not comp:
            print(f"endpoint {refdes}.{pin}: refdes not in graph", file=sys.stderr); sys.exit(1)
        pin_val = int(pin) if pin.isdigit() else pin
        key = (refdes, str(pin_val))
        if key in existing_keys:
            skipped.append(f"{refdes}.{pin}")
            continue
        ep = {
            "refdes": refdes,
            "pin": pin_val,
            "sheet": comp.get("sheet"),
            "edge_type": edge_type,
        }
        if edge_type == "sheet_zone":
            if not args.zone_ref:
                print("sheet_zone edges require --zone-ref (e.g. 4C6)", file=sys.stderr); sys.exit(1)
            ep["sheet_zone_ref"] = args.zone_ref
        ep["evidence"] = {"source": args.source}
        if args.note:
            ep["evidence"]["note"] = args.note
        net["endpoints"].append(ep)
        existing_keys.add(key)
        added.append(f"{refdes}.{pin}")
    save_graph(args.board, graph)
    msg = f"extended net {args.name}: +{len(added)} endpoint(s)"
    if added: msg += f" ({', '.join(added)})"
    if skipped: msg += f"; skipped {len(skipped)} duplicate(s) ({', '.join(skipped)})"
    print(msg)


def _parse_path(spec: str) -> list:
    """Parse 'x1,y1;x2,y2;…' → [[x,y], …]. Whitespace tolerant."""
    out = []
    for tok in spec.split(";"):
        tok = tok.strip()
        if not tok: continue
        try:
            x, y = (float(v.strip()) for v in tok.split(","))
        except Exception:
            raise SystemExit(f"path point malformed: {tok!r} (want 'x,y')")
        out.append([x, y])
    if len(out) < 2:
        raise SystemExit("path needs at least 2 points")
    return out


def _validate_orthogonal(path: list, tol: float = 0.5) -> None:
    """Tracer SKILL.md mandates right-angle segments only — KiCad's grid plus
    schematic-style routing both require it. Allow tiny float drift via tol."""
    for i in range(1, len(path)):
        x0, y0 = path[i - 1]
        x1, y1 = path[i]
        dx = abs(x1 - x0); dy = abs(y1 - y0)
        if dx > tol and dy > tol:
            raise SystemExit(
                f"path segment {i-1}→{i} is diagonal: ({x0},{y0})→({x1},{y1}). "
                f"Right-angle (H or V) segments only.")


def cmd_set_net_path(args):
    """Attach a routed polyline to an existing net. The exporter consumes
    `path` (when present) to emit (wire ...) segments matching the original
    artwork; without a path it falls back to a one-corner Manhattan route.

    Path coords are source-image pixels. Right-angle segments only — diagonals
    are rejected at write time so we don't carry bad data into KiCad."""
    graph = load_graph(args.board)
    net = next((n for n in graph.get("nets", []) if n["name"] == args.name), None)
    if not net:
        print(f"no such net: {args.name}", file=sys.stderr); sys.exit(1)
    if args.clear:
        net.pop("path", None)
        net.pop("path_source", None)
        save_graph(args.board, graph)
        print(f"cleared path on {args.name}")
        return
    if not args.path:
        print("provide --path 'x1,y1;x2,y2;…' or --clear", file=sys.stderr); sys.exit(1)
    pts = _parse_path(args.path)
    _validate_orthogonal(pts)
    # Provenance precedence: human > ai > tracer. Refuse to overwrite a human
    # path with a lower-confidence source unless --force.
    existing_src = net.get("path_source")
    rank = {"tracer": 0, "ai": 1, "human": 2}
    if existing_src and rank.get(args.source, 0) < rank.get(existing_src, 0) and not args.force:
        print(f"net {args.name} has existing path_source={existing_src}; "
              f"refusing to overwrite with {args.source} (use --force to override)",
              file=sys.stderr); sys.exit(1)
    net["path"] = pts
    net["path_source"] = args.source
    save_graph(args.board, graph)
    print(f"set path on {args.name}: {len(pts)} points, source={args.source}")


def cmd_untraced_nets(args):
    """List wire-typed nets that lack a path. Pickup signal for the tracer
    skill: it iterates this list and reads source crops to fill in paths.
    Skips label/sheet_zone/off_page nets — those don't get drawn lines, just
    label text at endpoints, so a path would be wasted."""
    graph = load_graph(args.board)
    refdes_sheets = {c["refdes"]: c.get("sheet") for c in graph["components"]}
    rows = []
    for net in graph.get("nets", []):
        eps = net.get("endpoints", [])
        if not eps: continue
        et = eps[0].get("edge_type")
        if et != "wire": continue
        if net.get("path"): continue
        if args.sheet is not None:
            if not any(refdes_sheets.get(ep.get("refdes")) == args.sheet for ep in eps):
                continue
        rows.append(net)
    label = f" on sheet {args.sheet}" if args.sheet is not None else ""
    print(f"{len(rows)} wire-typed net(s){label} without a path:")
    for net in rows[:50]:
        eps_short = ", ".join(f"{e['refdes']}.{e['pin']}@s{e.get('sheet','?')}"
                              for e in net["endpoints"][:6])
        more = f" (+{len(net['endpoints']) - 6} more)" if len(net["endpoints"]) > 6 else ""
        print(f"  {net['name']:18s}  {eps_short}{more}")
    if len(rows) > 50:
        print(f"  … and {len(rows) - 50} more")


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


def cmd_import_traced_nets(args):
    """Apply tracer JSON output to a board's graph."""
    src = json.loads(Path(args.from_file).read_text())
    if src.get("board") and src["board"] != args.board:
        print(f"warning: tracer file is for board {src['board']!r}, importing into {args.board!r}",
              file=sys.stderr)

    proposed = src.get("proposed_nets", [])
    if not proposed:
        print("no proposed nets in input"); return

    graph = load_graph(args.board)
    if "nets" not in graph:
        graph["nets"] = []

    refdes_index = {c["refdes"]: c for c in graph["components"]}
    existing_names = {n["name"] for n in graph["nets"]}

    added = 0
    skipped_size = 0
    skipped_existing = 0
    skipped_unknown = 0

    for prop in proposed:
        eps = prop.get("endpoints", [])
        if len(eps) < 2:
            continue
        if len(eps) > args.max_endpoints:
            skipped_size += 1
            continue

        # Build endpoint records
        out_eps = []
        bad = False
        for ep in eps:
            comp = refdes_index.get(ep["refdes"])
            if not comp:
                bad = True
                break
            out_eps.append({
                "refdes": ep["refdes"],
                "pin": ep["pin"],
                "sheet": comp.get("sheet"),
                "edge_type": "wire",
                "evidence": {
                    "source": "ai",
                    "confidence": prop.get("confidence", 0.5),
                    "note": f"tracer: skeleton component {prop.get('label','?')}",
                },
            })
        if bad:
            skipped_unknown += 1
            continue

        # Pick a unique name. Refdes-based naming would be nicer but we don't
        # know the schematic-side label yet; use a prefixed counter.
        i = 1
        while True:
            name = f"{args.prefix}{i}"
            if name not in existing_names:
                break
            i += 1
        existing_names.add(name)

        graph["nets"].append({
            "name": name,
            "kind": "signal",
            "endpoints": out_eps,
        })
        added += 1

    save_graph(args.board, graph)
    print(f"added {added} traced net(s)")
    if skipped_size:
        print(f"  skipped {skipped_size} (>{args.max_endpoints} endpoints)")
    if skipped_unknown:
        print(f"  skipped {skipped_unknown} (unknown refdes)")


def _net_priority(net, components_by_refdes):
    """Rank a net for the physical-probe list. HIGH > MED > LOW.

    HIGH:
      - kind in {power, ground}
      - net touches >1 sheet (any chance to mistrace at the boundary)
      - any endpoint is sheet_zone / off_page (cross-sheet linkage)
    MED:
      - any endpoint touches an ai-source component with confidence <0.7
      - kind=clock (timing critical)
    LOW: otherwise.
    """
    kind = net.get("kind", "signal")
    eps = net.get("endpoints", [])
    if kind in ("power", "ground"):
        return "HIGH", f"{kind} net — short or open is catastrophic"

    sheets = {ep.get("sheet") for ep in eps if ep.get("sheet") is not None}
    if len(sheets) > 1:
        return "HIGH", f"crosses {len(sheets)} sheets — easy to mistrace at boundary"

    edge_types = {ep.get("edge_type") for ep in eps}
    if edge_types & {"sheet_zone", "off_page"}:
        return "HIGH", "uses sheet-zone / off-page connector — cross-sheet inference"

    low_conf_ais = []
    for ep in eps:
        comp = components_by_refdes.get(ep.get("refdes"))
        if not comp:
            continue
        ev = comp.get("evidence", {}) or {}
        if ev.get("source") == "ai" and (ev.get("confidence") or 1.0) < 0.7:
            low_conf_ais.append(ep["refdes"])
    if low_conf_ais:
        return "MED", f"touches AI low-confidence component(s): {', '.join(sorted(set(low_conf_ais)))}"

    if kind == "clock":
        return "MED", "clock net — timing-critical"

    return "LOW", "spot-check"


def _net_suggested_test(net):
    """Concrete physical-probe instructions."""
    eps = net.get("endpoints", [])
    if len(eps) < 2:
        return "(net has <2 endpoints; nothing to probe)"
    edge = eps[0].get("edge_type", "wire")
    pairs = []
    anchor = eps[0]
    for other in eps[1:]:
        pairs.append(f"{anchor['refdes']}.{anchor['pin']} ↔ {other['refdes']}.{other['pin']}")
    if edge == "implicit_power":
        return f"Power-off DMM continuity, expect short: {'; '.join(pairs)}"
    if edge in ("sheet_zone", "off_page"):
        return (f"Power-off DMM continuity across the off-page link: "
                f"{'; '.join(pairs)}; verify the cross-sheet net name matches.")
    if edge == "bus":
        return (f"Power-off DMM continuity for each bus member: {'; '.join(pairs)}; "
                f"verify no shorts to the adjacent bus members.")
    return f"Power-off DMM continuity: {'; '.join(pairs)} should all beep."


def _component_priority(comp):
    """Verify-on-bench priority for a single component. Verified or
    high-confidence human entries → LOW; uncertain AI entries → HIGH/MED."""
    ev = comp.get("evidence", {}) or {}
    if comp.get("verified"):
        return None  # already verified, skip
    src = ev.get("source")
    conf = ev.get("confidence")
    if src == "human":
        return ("LOW", "human-added but not verified")
    if src == "datasheet":
        return ("LOW", "datasheet-derived, awaiting board confirmation")
    # AI source (default)
    if conf is None:
        return ("MED", "AI-added, no confidence recorded")
    if conf < 0.5:
        return ("HIGH", f"AI-added, low confidence {conf:.2f}")
    if conf < 0.8:
        return ("MED", f"AI-added, moderate confidence {conf:.2f}")
    return ("LOW", f"AI-added, high confidence {conf:.2f}")


def cmd_probe_list(args):
    graph = load_graph(args.board)
    components_by_refdes = {c["refdes"]: c for c in graph["components"]}
    nets = graph.get("nets", [])

    rows = []

    # Component verification probes — every unverified component gets a row
    # asking the human to confirm the part number visually on the PCB.
    for comp in graph["components"]:
        prio = _component_priority(comp)
        if prio is None:
            continue
        priority, reason = prio
        bbox = comp.get("bbox", [])
        loc = (f"sheet {comp['sheet']} @ ({bbox[0]:.0f},{bbox[1]:.0f})"
               if len(bbox) == 4 else f"sheet {comp['sheet']}")
        rows.append({
            "priority": priority,
            "net": f"verify:{comp['refdes']}",
            "endpoints": comp["refdes"],
            "reason": reason + f"; {loc}",
            "suggested_test": (f"Locate chip {comp['refdes']} on the PCB and read the printed "
                                f"part number. Expect {comp['part']!r}; if different, edit in "
                                "the explorer (E) or via librarian if the part is not in the library."),
            "status": "open",
        })

    # Net probes.
    for net in nets:
        priority, reason = _net_priority(net, components_by_refdes)
        eps = net.get("endpoints", [])
        endpoints_str = ";".join(f"{e['refdes']}.{e['pin']}" for e in eps)
        rows.append({
            "priority": priority,
            "net": net["name"],
            "endpoints": endpoints_str,
            "reason": reason,
            "suggested_test": _net_suggested_test(net),
            "status": "open",
        })

    # Sort: HIGH first, then MED, then LOW; ties by net/refdes name.
    order = {"HIGH": 0, "MED": 1, "LOW": 2}
    rows.sort(key=lambda r: (order.get(r["priority"], 9), r["net"]))

    # Write CSV.
    out_path = Path(args.out) if args.out else (board_dir(args.board) / "probes.csv")
    import csv
    with open(out_path, "w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=["priority", "net", "endpoints", "reason", "suggested_test", "status"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Summary table to stdout.
    counts = {"HIGH": 0, "MED": 0, "LOW": 0}
    for r in rows:
        counts[r["priority"]] = counts.get(r["priority"], 0) + 1
    print(f"wrote {out_path} — {len(rows)} probe(s):  "
          f"HIGH={counts['HIGH']}  MED={counts['MED']}  LOW={counts['LOW']}")
    if args.verbose:
        print()
        for r in rows:
            print(f"  [{r['priority']}] {r['net']:12s}  {r['endpoints']}")
            print(f"        reason: {r['reason']}")
            print(f"        test:   {r['suggested_test']}")


def cmd_render_overlay(args):
    """Draw the current graph (bboxes + pin positions + net labels) over the
    source PNG so the LLM can read it back and flag mis-placed elements.

    This is the primary visual checkpoint of the transcription loop: after
    each graphical edit (component bboxes, pin positions, named-net labels),
    render the overlay and inspect it. If anything is off, fix it before
    moving on. The overlay is the cheapest way to spot 'this chip is
    nowhere near the chip body on the source' or 'pin 7 is floating in
    open space'."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        print("opencv-python required. Install via .venv:\n"
              "  .venv/bin/pip install -r .agents/skills/cartographer/requirements.txt",
              file=sys.stderr)
        sys.exit(2)
    import cv2

    graph = load_graph(args.board)
    sheet_meta = next((s for s in graph["sheets"] if s["index"] == args.sheet), None)
    if not sheet_meta:
        print(f"sheet {args.sheet} not in board {args.board}", file=sys.stderr)
        sys.exit(1)
    image_path = (board_dir(args.board) / sheet_meta["scan_path"]).resolve()
    img = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if img is None:
        print(f"failed to read {image_path}", file=sys.stderr); sys.exit(1)

    out = img.copy()
    H, W = img.shape[:2]

    # Component bboxes — orange thick rectangle, refdes + part labelled above.
    components = [c for c in graph["components"] if c["sheet"] == args.sheet]
    for c in components:
        if "bbox" not in c:
            continue
        x1, y1, x2, y2 = (int(v) for v in c["bbox"])
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 140, 255), 4)
        label = f"{c['refdes']} {c['part']}"
        ts = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)[0]
        ty = max(y1 - 8, ts[1] + 4)
        cv2.rectangle(out, (x1, ty - ts[1] - 4), (x1 + ts[0] + 8, ty + 4), (0, 140, 255), -1)
        cv2.putText(out, label, (x1 + 4, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

    # Pin positions — pink filled dot with pin number.
    if not args.no_pins:
        for c in components:
            for pin, pos in (c.get("pin_positions") or {}).items():
                px, py = int(pos[0]), int(pos[1])
                cv2.circle(out, (px, py), 7, (200, 80, 200), -1)
                cv2.putText(out, str(pin), (px + 8, py - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (180, 0, 180), 2)

    # Net labels — for each label/sheet_zone/off_page net, draw the net name
    # at every endpoint pin position. Skips pure-wire nets so the overlay
    # doesn't get cluttered (those will get drawn lines if/when wire mode
    # is wired up).
    if not args.no_nets:
        comp_by_ref = {c["refdes"]: c for c in components}
        for net in graph.get("nets", []):
            for ep in net.get("endpoints", []):
                if ep.get("sheet") != args.sheet:
                    continue
                if ep.get("edge_type") not in ("label", "sheet_zone", "off_page"):
                    continue
                comp = comp_by_ref.get(ep.get("refdes"))
                if not comp:
                    continue
                pos = (comp.get("pin_positions") or {}).get(str(ep.get("pin")))
                if not pos:
                    continue
                px, py = int(pos[0]), int(pos[1])
                cv2.putText(out, net["name"], (px + 12, py + 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 120, 0), 2)

    # Optional resize so a 9k×6k overlay doesn't gobble RAM when read back.
    if args.max_width and W > args.max_width:
        scale = args.max_width / W
        out = cv2.resize(out, (args.max_width, int(H * scale)))

    out_path = Path(args.out) if args.out else (board_dir(args.board) / f"sheet{args.sheet}_overlay.png")
    cv2.imwrite(str(out_path), out)
    print(f"wrote {out_path}  ({len(components)} bbox(es), "
          f"{sum(len(c.get('pin_positions') or {}) for c in components)} pin position(s), "
          f"{sum(1 for n in graph.get('nets', []) if any(e.get('sheet')==args.sheet for e in n.get('endpoints', [])))} net(s) on this sheet)")


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

    # Gate: refuse to export an invalid graph. The next agent should not be
    # able to commit a kicad_sch derived from a graph with null edge_types,
    # missing pin positions, or unknown parts. Use --allow-invalid only when
    # consciously inspecting a partial state.
    if not args.allow_invalid:
        errs = _collect_validation_errors(graph, chips)
        if errs:
            print(f"export aborted: graph.json has {len(errs)} validation error(s).",
                  file=sys.stderr)
            for e in errs[:30]:
                print(f"  {e}", file=sys.stderr)
            if len(errs) > 30:
                print(f"  ... and {len(errs) - 30} more (run validate to see all)",
                      file=sys.stderr)
            print("\nfix the graph or pass --allow-invalid to export anyway.",
                  file=sys.stderr)
            sys.exit(2)
    out_dir = Path(args.out_dir) if args.out_dir else (board_dir(args.board) / "kicad")
    out_dir.mkdir(parents=True, exist_ok=True)

    sheets = ([s["index"] for s in graph["sheets"] if s["index"] == args.sheet]
              if args.sheet is not None
              else [s["index"] for s in graph["sheets"]])
    if not sheets:
        print(f"no matching sheet(s)", file=sys.stderr)
        sys.exit(1)

    # Emit a minimal .kicad_pro so kicad-cli treats this directory as a
    # KiCad project and reads the sym-lib-table from it (instead of falling
    # back to the global config, which doesn't know about 'user').
    pro_path = out_dir / f"{args.board}.kicad_pro"
    if not pro_path.exists():
        pro_path.write_text(
            '{\n  "meta": {"filename": "' + args.board + '.kicad_pro", "version": 1},\n'
            '  "libraries": {"pinned_symbol_libs": [], "pinned_footprint_libs": []}\n}\n'
        )

    # Emit a sym-lib-table that registers the 'user' library used by the
    # exporter's lib_id refs. The synthesized symbols are embedded under
    # lib_symbols inside each .kicad_sch, so the URI can be empty — the
    # table just acknowledges the lib name to KiCad and silences the
    # "library 'user' is not in the configuration" warning.
    sym_lib_table = out_dir / "sym-lib-table"
    if not sym_lib_table.exists():
        sym_lib_table.write_text(
            '(sym_lib_table\n'
            '  (lib (name "user")(type "KiCad")(uri "${KIPRJMOD}/user.kicad_sym")(options "")(descr "embedded user symbols (cached in each .kicad_sch lib_symbols block)"))\n'
            ')\n'
        )
        # KiCad refuses to recognise the lib unless the .kicad_sym file
        # exists, even if empty. Write a minimal placeholder.
        user_sym = out_dir / "user.kicad_sym"
        if not user_sym.exists():
            user_sym.write_text(
                '(kicad_symbol_lib (version 20231120) (generator "paper-to-schematic") (generator_version "0.1"))\n'
            )

    written = []
    for idx in sheets:
        sheet_meta = next(s for s in graph["sheets"] if s["index"] == idx)
        # Filename: <board>_sheet<N>_<title>.kicad_sch (slugified title)
        title = sheet_meta.get("title", f"sheet{idx}").lower()
        slug = "".join(c if c.isalnum() else "_" for c in title).strip("_")
        fname = f"{args.board}_s{idx}_{slug}.kicad_sch"
        fpath = out_dir / fname
        scan_path = (board_dir(args.board) / sheet_meta["scan_path"]).resolve() if sheet_meta.get("scan_path") else None
        text = kicad_export.gen_sch(graph, chips, idx, project_name=args.board,
                                     bg_image=args.bg_image, scan_path=scan_path)
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


def _validate_board_against_schema(board: dict, schema: dict, path_hint: str = ""):
    """Hand-rolled minimal JSON-schema validator. Returns list of error strings.

    Supports the subset of JSON Schema actually used in board.schema.json:
    type, required, properties, additionalProperties, enum, pattern, minimum,
    maximum, minItems, maxItems, items.
    """
    import re
    errs = []

    def visit(value, sch, where):
        if not isinstance(sch, dict):
            return
        # type
        t = sch.get("type")
        if t:
            type_map = {
                "object": dict,
                "array": list,
                "string": str,
                "integer": int,
                "number": (int, float),
                "boolean": bool,
                "null": type(None),
            }
            expected = type_map.get(t, object)
            if expected is int and isinstance(value, bool):
                errs.append(f"{where}: expected {t}, got bool"); return
            if not isinstance(value, expected):
                errs.append(f"{where}: expected {t}, got {type(value).__name__}")
                return
        # enum
        if "enum" in sch and value not in sch["enum"]:
            errs.append(f"{where}: value {value!r} not in {sch['enum']}")
        # pattern
        if "pattern" in sch and isinstance(value, str):
            if not re.search(sch["pattern"], value):
                errs.append(f"{where}: {value!r} does not match {sch['pattern']!r}")
        # numeric bounds
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in sch and value < sch["minimum"]:
                errs.append(f"{where}: {value} < minimum {sch['minimum']}")
            if "maximum" in sch and value > sch["maximum"]:
                errs.append(f"{where}: {value} > maximum {sch['maximum']}")
        # array
        if isinstance(value, list):
            if "minItems" in sch and len(value) < sch["minItems"]:
                errs.append(f"{where}: {len(value)} < minItems {sch['minItems']}")
            if "maxItems" in sch and len(value) > sch["maxItems"]:
                errs.append(f"{where}: {len(value)} > maxItems {sch['maxItems']}")
            if "items" in sch:
                for i, item in enumerate(value):
                    visit(item, sch["items"], f"{where}[{i}]")
        # object
        if isinstance(value, dict):
            for r in sch.get("required", []):
                if r not in value:
                    errs.append(f"{where}: missing required field {r!r}")
            for k, v in value.items():
                pdef = (sch.get("properties") or {}).get(k)
                if pdef is not None:
                    visit(v, pdef, f"{where}.{k}")
                elif sch.get("additionalProperties") is False:
                    errs.append(f"{where}.{k}: unexpected field")

    visit(board, schema, path_hint or "$")
    return errs


def parse_discrepancies(text: str):
    """Parse a board's discrepancies.md into structured entries.

    Recognized format (matches .agents/skills/schematic-graph/discrepancies.md
    template):

      ### <id> — <one-line summary>
      - **Date:**       YYYY-MM-DD
      - **Prober:**     <name>
      - **Instrument:** <DMM / scope / logic analyzer>
      - **Sheet:**      <sheet#> zone <zone>
      - **Net:**        <net name>
      - **Endpoints (paper):**  refdes.pin; refdes.pin
      - **Endpoints (board):**  refdes.pin; refdes.pin
      - **Resolution:** board_wins | paper_wins | unresolved
      - **Notes:**      free text

    Lines that don't match are tolerated. Returns a list of dicts.
    """
    import re
    entries = []
    current = None

    def commit(cur):
        if cur is not None:
            entries.append(cur)

    for line in text.splitlines():
        # Header: ### <id> — <summary>  (em-dash required, since IDs often
        # contain hyphens themselves, e.g. EXIDY-001).
        m = re.match(r"^###\s+(.+?)\s*—\s*(.+)$", line)
        if m:
            commit(current)
            current = {"id": m.group(1).strip(),
                       "summary": m.group(2).strip(),
                       "fields": {},
                       "notes_lines": []}
            continue
        if current is None:
            continue
        # Field: - **Name:** value  (Name may contain spaces / parentheses)
        m = re.match(r"^-\s+\*\*([^:*]+?):\*\*\s*(.*)$", line)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_").replace("(", "").replace(")", "")
            current["fields"][key] = m.group(2).strip()
            continue

    commit(current)
    # Filter out noise (e.g. the template's literal placeholder header).
    real = []
    for e in entries:
        if "fields" not in e or not e["fields"]:
            continue
        # Heuristic: real entries have at least Date OR Resolution OR Net.
        if e["fields"].get("date") or e["fields"].get("resolution") or e["fields"].get("net"):
            real.append(e)
    return real


def cmd_discrepancies(args):
    bdir = board_dir(args.board)
    path = bdir / "discrepancies.md"
    if not path.exists():
        template = SKILL_DIR / "discrepancies.md"
        print(f"no discrepancies log: {path}")
        if template.exists():
            print(f"start one by copying the template:")
            print(f"  cp {template} {path}")
        return

    entries = parse_discrepancies(path.read_text())
    if not entries:
        print(f"{path}: no discrepancy entries (only template scaffolding)")
        return

    from collections import defaultdict
    by_res = defaultdict(list)
    for e in entries:
        by_res[e["fields"].get("resolution", "unresolved")].append(e)

    print(f"{path.name}: {len(entries)} discrepancy entries")
    for res in ("board_wins", "paper_wins", "unresolved"):
        n = len(by_res.get(res, []))
        if n:
            print(f"  {res:<12s} {n}")
    other = [r for r in by_res if r not in ("board_wins", "paper_wins", "unresolved")]
    for r in other:
        print(f"  {r:<12s} {len(by_res[r])}")

    # Cross-reference with the graph: every entry should refer to a net or
    # component currently in graph.json. Flag entries pointing at nets/refdes
    # that no longer exist (renamed, removed) so they can be reconciled.
    graph = load_graph(args.board)
    net_names = {n["name"] for n in graph.get("nets", [])}
    refdes_set = {c["refdes"] for c in graph.get("components", [])}
    stale_refs = []
    for e in entries:
        net = e["fields"].get("net")
        if net and net not in net_names:
            stale_refs.append(f"  {e['id']}: references net {net!r} (not in graph)")
        for which in ("endpoints_paper", "endpoints_board"):
            for tok in (e["fields"].get(which, "")).split(";"):
                tok = tok.strip()
                if not tok:
                    continue
                if "." in tok:
                    refdes = tok.split(".", 1)[0].strip()
                    if refdes and refdes not in refdes_set:
                        stale_refs.append(f"  {e['id']}: refdes {refdes!r} not in graph")
    if stale_refs:
        print(f"\n{len(stale_refs)} stale reference(s):")
        for s in stale_refs:
            print(s)

    if args.verbose:
        print()
        for e in entries:
            print(f"## {e['id']} — {e['summary']}")
            for k, v in e["fields"].items():
                print(f"  {k}: {v}")
            print()

    # Emit ERC-exclusion stubs for board_wins entries — placeholders that
    # downstream tooling can wire into kicad-cli's exclusion list once the
    # exact violation IDs are known.
    if args.emit_exclusions:
        out = bdir / "erc_exclusions.txt"
        with open(out, "w") as f:
            f.write("# ERC exclusions sourced from discrepancies.md\n")
            f.write("# Format: one entry per line, '<discrepancy-id>: <net>: <reason>'\n")
            f.write("# Wire these into kicad-cli sch erc --severity-exclusions when the\n")
            f.write("# exact violation IDs are known.\n\n")
            for e in by_res.get("board_wins", []):
                fld = e["fields"]
                f.write(f"{e['id']}: {fld.get('net','?')}: {e['summary']}\n")
        print(f"\nwrote {out} ({len(by_res.get('board_wins',[]))} board_wins exclusion stubs)")


def cmd_validate_board(args):
    bdir = board_dir(args.board)
    bfile = bdir / "board.json"
    if not bfile.exists():
        print(f"no board.json: {bfile}", file=sys.stderr); sys.exit(1)
    board = json.loads(bfile.read_text())
    schema = json.loads((SKILL_DIR / "board.schema.json").read_text())

    errs = _validate_board_against_schema(board, schema, "board")

    # Cross-checks beyond pure schema:
    if board.get("id") != args.board:
        errs.append(f"board.id={board.get('id')!r} doesn't match folder name {args.board!r}")

    seen_indices = set()
    for i, sheet in enumerate(board.get("sheets", [])):
        idx = sheet.get("index")
        if idx in seen_indices:
            errs.append(f"sheets[{i}]: duplicate index {idx}")
        seen_indices.add(idx)
        scan_path = sheet.get("scan_path")
        if scan_path:
            resolved = (bdir / scan_path).resolve()
            if not resolved.exists():
                errs.append(f"sheets[{i}]: scan_path does not exist: {resolved}")

    if errs:
        print(f"FAIL — {len(errs)} issues:")
        for e in errs:
            print(f"  {e}")
        sys.exit(1)
    print(f"ok — {bfile}: schema valid, {len(board.get('sheets', []))} sheet(s) on disk")


def _collect_validation_errors(graph: dict, chips: dict) -> list[str]:
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
        if not eps:
            errs.append(f"net {name}: has no endpoints")
        # Single-endpoint nets are valid for label / sheet_zone / off_page —
        # those represent named signals whose other end is on a different
        # sheet that hasn't been transcribed yet. For wire / bus /
        # implicit_power, single endpoints are an error.
        if len(eps) == 1:
            et = eps[0].get("edge_type")
            if et in ("wire", "bus", "implicit_power"):
                errs.append(
                    f"net {name}: single endpoint with edge_type {et!r} — "
                    f"only label/sheet_zone/off_page may have one endpoint")
        edge_types = {ep.get("edge_type") for ep in eps}
        if len(edge_types) > 1:
            errs.append(f"net {name}: mixed edge_types {edge_types} (one net should use one type)")
        for ep in eps:
            if ep.get("refdes") not in refdes_index:
                errs.append(f"net {name}: endpoint refdes {ep.get('refdes')!r} not in graph")
            et = ep.get("edge_type")
            if et is None:
                errs.append(
                    f"net {name}: endpoint {ep.get('refdes')}.{ep.get('pin')} has "
                    f"edge_type=null. Every endpoint must declare its connection "
                    f"kind (one of {EDGE_TYPES}). Use list-nets and untyped-nets "
                    f"to find these; classify with add-net (after remove-net) or "
                    f"by editing graph.json directly.")
            elif et not in EDGE_TYPES:
                errs.append(f"net {name}: bad edge_type {et!r}")
            if et == "sheet_zone" and not ep.get("sheet_zone_ref"):
                errs.append(f"net {name}: sheet_zone endpoint missing sheet_zone_ref")
            # Labels and off-page connectors are KEYED BY NAME — a null name
            # means there's nothing for KiCad to match against on other sheets.
            if et in ("label", "sheet_zone", "off_page") and not name:
                errs.append(f"net {name!r}: edge_type={et} requires a non-empty name")
    return errs


def cmd_validate(args):
    graph = load_graph(args.board)
    chips = load_chips()
    errs = _collect_validation_errors(graph, chips)
    if errs:
        print(f"FAIL — {len(errs)} issues:")
        for e in errs:
            print(f"  {e}")
        sys.exit(1)
    print(f"ok — {len(graph['components'])} components, "
          f"{len(graph.get('nets', []))} nets, all parts in librarian")


def cmd_pipeline_status(args):
    """Per-sheet pickup signal for a less-thinking LLM session: 'where am I?'.

    Reports each sheet's stage by inspecting graph state (component count,
    pin-position coverage, named net count) and ERC artifact presence
    (boards/<id>/kicad/*_s<n>_*.erc.txt). All numbers are derived — no
    state file to keep in sync.
    """
    graph = load_graph(args.board)
    chips = load_chips()
    bdir = board_dir(args.board)
    kicad_dir = bdir / "kicad"

    # Use the kicad_export predicates so pinned-% counts only the active
    # gate's pins on split-gate sub-units (otherwise g01a would never reach
    # 100% because pins 8-16 belong to gate B/C/D's refdes).
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "kicad_export",
        Path(__file__).resolve().parent / "kicad_export.py")
    kicad_export = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kicad_export)

    sheets = sorted(graph.get("sheets", []), key=lambda s: s["index"])
    print(f"board: {args.board}  sheets: {len(sheets)}")
    print(f"{'sheet':>5}  {'comps':>5}  {'pinned%':>7}  {'nets':>5}  {'erc':>10}  stage")
    for s in sheets:
        idx = s["index"]
        comps = [c for c in graph["components"] if c.get("sheet") == idx]
        n_comps = len(comps)
        multi_unit = kicad_export._multi_unit_parts(comps, chips)
        pinned = 0
        total_pins = 0
        for c in comps:
            part = chips["parts"].get(c.get("part"))
            if not part: continue
            letter = kicad_export._comp_unit_letter(c["refdes"], c["part"], multi_unit)
            active = kicad_export._comp_active_pins(part, letter)
            total_pins += len(active)
            placed = c.get("pin_positions") or {}
            pinned += sum(1 for p in active if str(p["n"]) in placed)
        pin_pct = (100.0 * pinned / total_pins) if total_pins else 0.0
        nets = [n for n in graph.get("nets", [])
                if any(ep.get("sheet") == idx for ep in n.get("endpoints", []))]
        n_nets = len(nets)

        # ERC artifact + blocking count.
        erc_status = "—"
        erc_blocking = 0
        matches = sorted(kicad_dir.glob(f"*_s{idx}_*.erc.txt")) if kicad_dir.exists() else []
        if matches:
            counts, error_counts = _parse_erc_counts(matches[-1])
            erc_blocking = sum(error_counts.get(k, 0) for k in ERC_BLOCKING)
            other_errors = sum(error_counts.get(k, 0) for k in counts
                               if k not in ERC_BLOCKING and k not in ERC_EXPECTED_CROSS_SHEET
                               and k not in ERC_BENIGN_WARNINGS)
            erc_blocking += other_errors
            erc_status = "PASS" if erc_blocking == 0 else f"FAIL({erc_blocking})"

        # Stage 6 — wire-path tracing. Count wire-typed nets on the sheet and
        # how many of them carry a `path` (faithful routing). Labels and
        # sheet_zone nets don't need paths.
        wire_nets = [n for n in nets
                     if n.get("endpoints") and n["endpoints"][0].get("edge_type") == "wire"]
        n_wire = len(wire_nets)
        n_traced = sum(1 for n in wire_nets if n.get("path"))
        trace_pct = (100.0 * n_traced / n_wire) if n_wire else 100.0

        # Body_bbox coverage — Stage 1.5. Cheap signal for whether the
        # KiCad-rendered chip outlines match the source.
        n_body_bbox = sum(1 for c in comps if c.get("body_bbox"))
        body_pct = (100.0 * n_body_bbox / n_comps) if n_comps else 0.0

        # Infer stage. The acceptance-gate progression:
        if n_comps == 0:
            stage = "0 — not started"
        elif pin_pct < 95.0:
            stage = f"1 — bboxes ({pin_pct:.0f}% pinned, finish Stage 2)"
        elif n_nets == 0:
            stage = "2 — pins done, no nets yet (Stage 3)"
        elif not matches:
            stage = "3 — nets present, no ERC run (Stage 5: export-kicad)"
        elif erc_blocking > 0:
            stage = f"5 — {erc_blocking} blocking ERC error(s) to fix"
        elif trace_pct < 100.0:
            stage = f"5 — ERC clean; Stage 6: {n_wire - n_traced} wire(s) need paths ({trace_pct:.0f}% traced)"
        elif body_pct < 100.0:
            stage = f"6 — paths done; body_bbox coverage {body_pct:.0f}% (Stage 1.5 for visual polish)"
        else:
            stage = "6 — ready (paths + body_bbox + ERC all clean)"

        print(f"{idx:>5}  {n_comps:>5}  {pin_pct:>6.0f}%  {n_nets:>5}  {erc_status:>10}  {stage}")


def cmd_untyped_nets(args):
    """List nets touching --sheet that have null edge_type (or null label
    where one is required). The Stage-3 acceptance gate: this command must
    return zero rows before the LLM moves on. Easier to read than `validate`
    for this single class of error."""
    graph = load_graph(args.board)
    refdes_sheets = {c["refdes"]: c.get("sheet") for c in graph["components"]}
    rows = []
    for net in graph.get("nets", []):
        eps = net.get("endpoints", [])
        touches = (args.sheet is None) or any(
            refdes_sheets.get(ep.get("refdes")) == args.sheet for ep in eps)
        if not touches:
            continue
        bad = [ep for ep in eps if ep.get("edge_type") is None]
        if bad:
            rows.append((net.get("name") or "<no-name>", len(eps), len(bad)))
        elif not net.get("name") and any(
                ep.get("edge_type") in ("label", "sheet_zone", "off_page") for ep in eps):
            rows.append((net.get("name") or "<no-name>", len(eps), 0))
    label = f" on sheet {args.sheet}" if args.sheet is not None else ""
    if not rows:
        print(f"PASS — no untyped nets{label}")
        return
    print(f"FAIL — {len(rows)} untyped/unnamed net(s){label}:")
    for name, n_eps, n_null in rows:
        print(f"  {name:18s}  endpoints={n_eps}  null_edge_types={n_null}")
    sys.exit(1)


# Categories we *can* and *should* eliminate by construction. If any of these
# show up in the ERC report after the export-side fixes (grid snap, power
# flags, library mapping), it's a process bug — not a transcription artifact.
ERC_BLOCKING = {
    "endpoint_off_grid",
    "power_pin_not_driven",
}
# Categories that are EXPECTED while the rest of the board hasn't been
# transcribed. Cross-sheet labels look isolated until their other ends land.
ERC_EXPECTED_CROSS_SHEET = {
    "isolated_pin_label",
    "label_dangling",
    "pin_not_connected",
    "pin_not_driven",
    "unconnected_wire_endpoint",
}
# Warnings the export emits as a known cosmetic side-effect: stacked
# global_labels at power pins and KiCad nagging that the inlined 'user'
# library isn't in its global config. Both produce a working netlist.
ERC_BENIGN_WARNINGS = {
    "label_multiple_wires",
    "lib_symbol_issues",
}


def _parse_erc_counts(rep_path: Path):
    """Return ({cat: count}, {cat: error_count}) for one .erc.txt file."""
    text = rep_path.read_text()
    import re
    counts = {}
    error_counts = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"\[([a-z_]+)\]:", line.strip())
        if m:
            cat = m.group(1)
            counts[cat] = counts.get(cat, 0) + 1
            sev = "warning"
            if i + 1 < len(lines) and "; error" in lines[i + 1]:
                sev = "error"
            if sev == "error":
                error_counts[cat] = error_counts.get(cat, 0) + 1
    return counts, error_counts


def _board_single_endpoint_labels(graph: dict) -> list[tuple[str, str]]:
    """Return [(net_name, "refdes.pin@sheet")] for named label-style nets that
    have only one endpoint across the whole board. After every sheet is
    transcribed, any name surfaced here is a real off-page connection that
    didn't land on its other side."""
    out = []
    for net in graph.get("nets", []):
        eps = net.get("endpoints", [])
        if len(eps) != 1:
            continue
        et = eps[0].get("edge_type")
        if et not in ("label", "sheet_zone", "off_page"):
            continue
        if not net.get("name"):
            continue
        ep = eps[0]
        out.append((net["name"], f"{ep.get('refdes')}.{ep.get('pin')}@sheet{ep.get('sheet')}"))
    out.sort()
    return out


def cmd_erc_summary(args):
    """Read the .erc.txt produced by the last export-kicad run, categorise
    violations, and print a single PASS/FAIL verdict the LLM can act on.

    With --sheet, looks at one sheet's report. Without --sheet (board-level
    mode), aggregates every sheet's report and additionally surfaces named
    labels that have only one endpoint across the whole board — those are
    the real off-page connections that didn't land on their other side."""
    bdir = board_dir(args.board)
    kicad_dir = bdir / "kicad"

    if args.report:
        rep = Path(args.report)
        counts, error_counts = _parse_erc_counts(rep)
        report_label = rep.name
        board_mode = False
    elif args.sheet is None:
        matches = sorted(kicad_dir.glob("*_s*_*.erc.txt"))
        if not matches:
            print(f"no ERC reports found at {kicad_dir}/*_s*_*.erc.txt — "
                  f"run export-kicad --validate first.", file=sys.stderr)
            sys.exit(2)
        counts, error_counts = {}, {}
        for m in matches:
            c, e = _parse_erc_counts(m)
            for k, v in c.items(): counts[k] = counts.get(k, 0) + v
            for k, v in e.items(): error_counts[k] = error_counts.get(k, 0) + v
        report_label = f"board {args.board} ({len(matches)} sheet report(s))"
        board_mode = True
    else:
        matches = sorted(kicad_dir.glob(f"*_s{args.sheet}_*.erc.txt"))
        if not matches:
            print(f"no ERC report found at {kicad_dir}/*_s{args.sheet}_*.erc.txt — "
                  f"run export-kicad --validate first.", file=sys.stderr)
            sys.exit(2)
        rep = matches[-1]
        counts, error_counts = _parse_erc_counts(rep)
        report_label = rep.name
        board_mode = False

    print(f"ERC report: {report_label}")
    if not counts and not board_mode:
        print("PASS — zero violations")
        return

    blocking = {k: v for k, v in counts.items() if k in ERC_BLOCKING}
    expected = {k: v for k, v in counts.items() if k in ERC_EXPECTED_CROSS_SHEET}
    benign = {k: v for k, v in counts.items() if k in ERC_BENIGN_WARNINGS}
    other = {k: v for k, v in counts.items()
             if k not in ERC_BLOCKING
             and k not in ERC_EXPECTED_CROSS_SHEET
             and k not in ERC_BENIGN_WARNINGS}

    def fmt(d):
        return ", ".join(f"{k}={v}" for k, v in sorted(d.items())) if d else "(none)"

    total_errors = sum(error_counts.values())
    total_warnings = sum(counts.values()) - total_errors
    print(f"  blocking ({sum(blocking.values())}): {fmt(blocking)}")
    print(f"  cross-sheet expected ({sum(expected.values())}): {fmt(expected)}")
    print(f"  benign ({sum(benign.values())}): {fmt(benign)}")
    print(f"  other ({sum(other.values())}): {fmt(other)}")
    print(f"  severity: {total_errors} error(s), {total_warnings} warning(s)")

    if board_mode:
        graph = load_graph(args.board)
        loners = _board_single_endpoint_labels(graph)
        print(f"\nsingle-endpoint named labels ({len(loners)}): "
              f"once every sheet is transcribed, these are real off-page "
              f"connections missing their other side.")
        for name, where in loners[:30]:
            print(f"  {name:20s}  {where}")
        if len(loners) > 30:
            print(f"  … and {len(loners) - 30} more")

    # The gate: blocking categories fail unconditionally; `other` fails when
    # there's at least one error (warnings in `other` get surfaced but don't
    # block). `expected` and `benign` are noise.
    other_errors = {k: error_counts.get(k, 0) for k in other if error_counts.get(k, 0) > 0}
    if blocking or other_errors:
        fail_count = sum(blocking.values()) + sum(other_errors.values())
        print(f"\nFAIL — {fail_count} blocking issue(s). Construction bugs in "
              f"`blocking` come from the graph or the export and should be 0; "
              f"errors in `other` (typically pin_to_pin) are real wiring mistakes "
              f"in the graph the LLM must fix.")
        sys.exit(1)
    print(f"\nPASS — only cross-sheet noise + benign library warnings remain "
          f"({total_warnings} warning(s) total). These resolve as the rest of "
          f"the board is transcribed.")


def cmd_render_kicad(args):
    """Wrap kicad-cli sch export svg + sips so the LLM doesn't need to
    remember the kicad-cli flags. Outputs a PNG the agent should Read back
    and visually compare to the source overlay."""
    cli = _find_kicad_cli()
    if not cli:
        print("kicad-cli not found", file=sys.stderr); sys.exit(2)
    bdir = board_dir(args.board)
    kicad_dir = bdir / "kicad"
    matches = sorted(kicad_dir.glob(f"*_s{args.sheet}_*.kicad_sch"))
    if not matches:
        print(f"no kicad_sch for sheet {args.sheet}; run export-kicad first.",
              file=sys.stderr); sys.exit(2)
    sch = matches[-1]
    out_dir = Path(args.out_dir) if args.out_dir else Path(f"/tmp/{args.board}_s{args.sheet}_kicad")
    out_dir.mkdir(parents=True, exist_ok=True)
    import subprocess
    subprocess.run([cli, "sch", "export", "svg", "--output", str(out_dir), str(sch)],
                   check=True, capture_output=True)
    svg = out_dir / f"{sch.stem}.svg"
    png = Path(args.out) if args.out else out_dir / f"{sch.stem}.png"
    r = subprocess.run(["sips", "-s", "format", "png", str(svg), "--out", str(png)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"sips failed: {r.stderr}", file=sys.stderr); sys.exit(2)
    print(f"wrote {png}")


def cmd_lint(args):
    """Run a battery of cheap correctness checks on a sheet. Each check fails
    independently; the LLM is expected to fix every FAIL before declaring the
    sheet done. This is a separate pass from `validate` (which checks the
    graph schema) and `erc-summary` (which categorises KiCad's view) — `lint`
    inspects the graph against the source PNG and the librarian, catching
    transcription mistakes that schemas can't see."""
    graph = load_graph(args.board)
    chips = load_chips()
    sheet_meta = next((s for s in graph["sheets"] if s["index"] == args.sheet), None)
    if not sheet_meta:
        print(f"sheet {args.sheet} not in board {args.board}", file=sys.stderr); sys.exit(1)

    components = [c for c in graph["components"] if c["sheet"] == args.sheet]
    nets = [n for n in graph.get("nets", [])
            if any(ep.get("sheet") == args.sheet for ep in n.get("endpoints", []))]
    refdes_index = {c["refdes"]: c for c in components}

    fails = []   # blocking
    warns = []   # advisory

    # --- Bbox sanity ---
    img = None
    try:
        import cv2
        img_path = (board_dir(args.board) / sheet_meta["scan_path"]).resolve()
        img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
    except Exception:
        pass

    for c in components:
        bbox = c.get("bbox") or []
        if len(bbox) != 4:
            fails.append(f"bbox/{c['refdes']}: malformed bbox {bbox}")
            continue
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if w <= 0 or h <= 0:
            fails.append(f"bbox/{c['refdes']}: degenerate {bbox}")
            continue
        # Plausible chip bbox: a DIP-16 at ~300 DPI is ~150–500 px wide,
        # ~250–800 px tall. Anything 10× off is almost certainly wrong.
        part = chips["parts"].get(c["part"], {})
        n_pins = len(part.get("pins") or [])
        if n_pins:
            min_dim = min(w, h); max_dim = max(w, h)
            if max_dim < 80:
                fails.append(f"bbox/{c['refdes']}: too small ({w:.0f}×{h:.0f}px) for a "
                             f"{n_pins}-pin chip; chip body likely missed")
            elif max_dim > 1200:
                warns.append(f"bbox/{c['refdes']}: very large ({w:.0f}×{h:.0f}px) — "
                             f"likely includes adjacent text or wires")
            elif min_dim / max_dim < 0.15:
                warns.append(f"bbox/{c['refdes']}: very elongated aspect "
                             f"{min_dim:.0f}:{max_dim:.0f} — verify it's a chip and not a label/wire region")
        # Inside the page.
        if img is not None:
            H, W = img.shape[:2]
            if bbox[0] < 0 or bbox[1] < 0 or bbox[2] > W or bbox[3] > H:
                fails.append(f"bbox/{c['refdes']}: extends outside page bounds ({W}×{H})")
            else:
                # If the bbox interior is blank (mean intensity > 248 on a uint8
                # grayscale page that has black ink on white), the bbox almost
                # certainly does not cover any chip body.
                x1, y1, x2, y2 = (int(v) for v in bbox)
                crop = img[y1:y2, x1:x2]
                if crop.size:
                    mean = crop.mean()
                    if mean > 248:
                        fails.append(f"bbox/{c['refdes']}: interior is blank "
                                     f"(mean px={mean:.1f}); bbox covers empty page")

    # --- Pin position sanity ---
    for c in components:
        bbox = c.get("bbox") or []
        if len(bbox) != 4:
            continue
        part = chips["parts"].get(c["part"], {})
        valid_pins = {str(p["n"]) for p in part.get("pins", [])}
        pp = c.get("pin_positions") or {}
        for pin, pos in pp.items():
            if pin not in valid_pins:
                fails.append(f"pin/{c['refdes']}.{pin}: pin number not in librarian for {c['part']}")
                continue
            if not (isinstance(pos, list) and len(pos) == 2):
                fails.append(f"pin/{c['refdes']}.{pin}: malformed position {pos}")
                continue
            x, y = pos
            # Pin should be on or just outside the bbox edge — give a 30px tolerance.
            tol = 30
            inside_x = (bbox[0] - tol) <= x <= (bbox[2] + tol)
            inside_y = (bbox[1] - tol) <= y <= (bbox[3] + tol)
            if not (inside_x and inside_y):
                fails.append(f"pin/{c['refdes']}.{pin}: position {pos} is far from "
                             f"bbox {bbox} — pin floating in space")
        # Coverage: components whose pins carry signals must have pin_positions
        # populated. Unknown pin positions cascade into wrong wire endpoints.
        if pp:
            missing = valid_pins - set(pp.keys())
            if missing:
                warns.append(f"pin/{c['refdes']}: {len(missing)} pin(s) without positions: "
                             f"{','.join(sorted(missing, key=lambda s: int(s) if s.isdigit() else 0))[:80]}")

    # --- Net coverage ---
    refs_with_nets = set()
    for net in nets:
        for ep in net.get("endpoints", []):
            refs_with_nets.add(ep.get("refdes"))
    for c in components:
        if c["refdes"] not in refs_with_nets:
            warns.append(f"coverage/{c['refdes']}: zero nets touch this chip — "
                         f"every chip should have at least power+ground+signal nets")

    # --- Net degree sanity ---
    for net in nets:
        eps = net.get("endpoints", [])
        if len(eps) > 25:
            warns.append(f"degree/{net.get('name')}: {len(eps)} endpoints — "
                         f"likely a CV crossing-detection failure or an unsplit bus")
        # A wire net with 1 endpoint is invalid — already caught by validate,
        # but show it here for completeness.
        if len(eps) < 2 and (eps and eps[0].get("edge_type") == "wire"):
            fails.append(f"degree/{net.get('name')}: single-endpoint wire net (illegal)")

    # --- Bus consistency: members like FOO.0..FOO.N should be contiguous ---
    import re
    bus_groups: dict[str, set[int]] = {}
    for net in nets:
        m = re.match(r"^([A-Za-z_]+)\.(\d+)$", net.get("name") or "")
        if m:
            bus_groups.setdefault(m.group(1), set()).add(int(m.group(2)))
    for bus, idxs in bus_groups.items():
        lo, hi = min(idxs), max(idxs)
        if (hi - lo + 1) != len(idxs):
            missing = sorted(set(range(lo, hi + 1)) - idxs)
            warns.append(f"bus/{bus}: members {sorted(idxs)} have gaps; "
                         f"missing {missing[:8]}{'...' if len(missing) > 8 else ''}")

    # --- Verdict ---
    print(f"lint --board {args.board} --sheet {args.sheet}: "
          f"{len(components)} component(s), {len(nets)} net(s)")
    for w in warns:
        print(f"  WARN  {w}")
    for f in fails:
        print(f"  FAIL  {f}")
    if fails:
        print(f"\nFAIL — {len(fails)} blocking issue(s); fix before moving on. "
              f"({len(warns)} warning(s) are advisory.)")
        sys.exit(1)
    if warns:
        print(f"\nPASS with {len(warns)} warning(s) — review and decide.")
        return
    print("\nPASS — no issues.")


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
    sp.add_argument("--value", help="instance-level value (1k, 22p, 7.3728MHz) — "
                                    "required for discretes flagged value_required")
    sp.add_argument("--source", choices=["ai", "human", "datasheet", "probe"], default="ai")
    sp.add_argument("--confidence", type=float)
    sp.add_argument("--note")
    sp.set_defaults(fn=cmd_add_component)

    sp = sub.add_parser("remove-component")
    sp.add_argument("--board", required=True)
    sp.add_argument("--refdes", required=True)
    sp.set_defaults(fn=cmd_remove_component)

    sp = sub.add_parser("set-pin-positions",
                        help="set pin_positions for a component from a JSON map")
    sp.add_argument("--board", required=True)
    sp.add_argument("--refdes", required=True)
    sp.add_argument("--json", required=True,
                    help='JSON object {pin: [x,y], ...} (inline or @file.json)')
    sp.add_argument("--merge", action="store_true",
                    help="merge with existing pin_positions instead of replacing")
    sp.set_defaults(fn=cmd_set_pin_positions)

    sp = sub.add_parser("set-body-bbox",
                        help="attach a tight chip-outline rectangle (body_bbox) "
                             "to a component; the KiCad export uses it to size "
                             "the rendered symbol body. bbox stays as the loose "
                             "click-target / pin-area extent.")
    sp.add_argument("--board", required=True)
    sp.add_argument("--refdes", required=True)
    sp.add_argument("--bbox", help="x1,y1,x2,y2 in source-image pixels")
    sp.add_argument("--clear", action="store_true",
                    help="remove the body_bbox (revert to bbox as body)")
    sp.set_defaults(fn=cmd_set_body_bbox)

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

    sp = sub.add_parser("validate-board",
                        help="check boards/<id>/board.json against board.schema.json + on-disk sanity")
    sp.add_argument("--board", required=True)
    sp.set_defaults(fn=cmd_validate_board)

    sp = sub.add_parser("discrepancies",
                        help="parse boards/<id>/discrepancies.md and report by resolution")
    sp.add_argument("--board", required=True)
    sp.add_argument("--verbose", action="store_true",
                    help="print every entry with its fields")
    sp.add_argument("--emit-exclusions", action="store_true",
                    help="write erc_exclusions.txt stubs for board_wins entries")
    sp.set_defaults(fn=cmd_discrepancies)

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

    sp = sub.add_parser("extend-net",
                        help="append endpoints to an existing net (for cross-sheet labels accumulating endpoints across sheets)")
    sp.add_argument("--board", required=True)
    sp.add_argument("--name", required=True, help="existing net name")
    sp.add_argument("--add-endpoints", required=True,
                    help="comma-separated refdes.pin list to append")
    sp.add_argument("--edge-type", choices=EDGE_TYPES,
                    help="must match the existing net's edge_type (defaults to it)")
    sp.add_argument("--zone-ref", help="required when edge-type is sheet_zone")
    sp.add_argument("--source", choices=["ai", "human", "datasheet", "probe"], default="ai")
    sp.add_argument("--note")
    sp.set_defaults(fn=cmd_extend_net)

    sp = sub.add_parser("set-net-path",
                        help="attach a routed polyline (right-angle segments) "
                             "to a net so the KiCad export draws faithful wires "
                             "instead of pin-to-pin diagonals")
    sp.add_argument("--board", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--path", help="'x1,y1;x2,y2;…' in source-image pixels; "
                                   "right-angle segments only")
    sp.add_argument("--clear", action="store_true",
                    help="remove the path on this net (revert to fallback routing)")
    sp.add_argument("--source", choices=["ai", "human", "tracer"], default="ai")
    sp.add_argument("--force", action="store_true",
                    help="overwrite a higher-provenance existing path "
                         "(human > ai > tracer)")
    sp.set_defaults(fn=cmd_set_net_path)

    sp = sub.add_parser("untraced-nets",
                        help="list wire-typed nets that lack a routed path — "
                             "the tracer skill's pickup signal")
    sp.add_argument("--board", required=True)
    sp.add_argument("--sheet", type=int, help="restrict to nets touching this sheet")
    sp.set_defaults(fn=cmd_untraced_nets)

    sp = sub.add_parser("remove-net")
    sp.add_argument("--board", required=True)
    sp.add_argument("--name", required=True)
    sp.set_defaults(fn=cmd_remove_net)

    sp = sub.add_parser("list-nets")
    sp.add_argument("--board", required=True)
    sp.add_argument("--sheet", type=int, help="restrict to nets with at least one endpoint on this sheet")
    sp.set_defaults(fn=cmd_list_nets)

    sp = sub.add_parser("import-traced-nets",
                        help="import proposed nets from tracer JSON output")
    sp.add_argument("--board", required=True)
    sp.add_argument("--from", dest="from_file", required=True,
                    help="JSON file from tracer.py trace")
    sp.add_argument("--prefix", default="T_",
                    help="prepend this to each generated net name (default: T_)")
    sp.add_argument("--max-endpoints", type=int, default=20,
                    help="skip proposed nets with more than this many endpoints "
                         "(usually buses or over-connected; default 20)")
    sp.set_defaults(fn=cmd_import_traced_nets)

    sp = sub.add_parser("probe-list", help="generate ranked physical-board verification list (probes.csv)")
    sp.add_argument("--board", required=True)
    sp.add_argument("--out", help="output CSV path (default: boards/<id>/probes.csv)")
    sp.add_argument("--verbose", action="store_true", help="print every row to stdout")
    sp.set_defaults(fn=cmd_probe_list)

    sp = sub.add_parser("render-overlay",
                        help="draw the current graph over the source PNG — primary visual "
                             "checkpoint after each graphical edit")
    sp.add_argument("--board", required=True)
    sp.add_argument("--sheet", type=int, required=True)
    sp.add_argument("--out", help="output PNG path (default: boards/<id>/sheet<N>_overlay.png)")
    sp.add_argument("--no-pins", action="store_true", help="skip pin-position dots")
    sp.add_argument("--no-nets", action="store_true", help="skip net-label texts")
    sp.add_argument("--max-width", type=int, default=2400,
                    help="resize wider sources down to this width so the overlay reads "
                         "back at near-native legibility (default: 2400; 0 disables)")
    sp.set_defaults(fn=cmd_render_overlay)

    sp = sub.add_parser("export-kicad", help="emit a .kicad_sch file per sheet")
    sp.add_argument("--board", required=True)
    sp.add_argument("--sheet", type=int, help="single sheet (default: all sheets)")
    sp.add_argument("--out-dir", help="output directory (default: boards/<id>/kicad/)")
    sp.add_argument("--validate", action="store_true",
                    help="parse output as s-expression after writing; if KiCad CLI is on PATH or at the standard macOS location, also run sch erc")
    sp.add_argument("--allow-invalid", action="store_true",
                    help="export even when graph_cli validate finds errors. "
                         "Use sparingly — the default refuses to write a "
                         ".kicad_sch from a graph with null edge_types, "
                         "missing pin_positions, or unknown parts.")
    sp.add_argument("--bg-image", action="store_true",
                    help="embed the source scan PNG (downsampled) as a background image "
                         "behind the chip symbols, for visual diff in the rendered PDF. "
                         "EXPERIMENTAL — KiCad's image scaling is finicky; the embedded "
                         "image position may need manual adjustment in eeschema. File "
                         "size grows by a few MB per sheet.")
    sp.set_defaults(fn=cmd_export_kicad)

    sp = sub.add_parser("untyped-nets",
                        help="list nets with null edge_type or missing label — "
                             "Stage-3 acceptance gate")
    sp.add_argument("--board", required=True)
    sp.add_argument("--sheet", type=int, help="restrict to nets touching this sheet")
    sp.set_defaults(fn=cmd_untyped_nets)

    sp = sub.add_parser("pipeline-status",
                        help="per-sheet pickup signal: components, pinned-pct, "
                             "nets, ERC, and the inferred Stage. Run on session "
                             "start to see where to resume.")
    sp.add_argument("--board", required=True)
    sp.set_defaults(fn=cmd_pipeline_status)

    sp = sub.add_parser("erc-summary",
                        help="categorise the last KiCad ERC report into "
                             "blocking / cross-sheet-expected / other and "
                             "emit a one-line PASS/FAIL verdict")
    sp.add_argument("--board", required=True)
    sp.add_argument("--sheet", type=int,
                    help="restrict to one sheet; omit to roll up every sheet's report")
    sp.add_argument("--report", help="path to the .erc.txt (default: auto-discover)")
    sp.set_defaults(fn=cmd_erc_summary)

    sp = sub.add_parser("render-kicad",
                        help="rasterise the last exported .kicad_sch via "
                             "kicad-cli + sips so it can be Read back")
    sp.add_argument("--board", required=True)
    sp.add_argument("--sheet", type=int, required=True)
    sp.add_argument("--out", help="output PNG path")
    sp.add_argument("--out-dir", help="intermediate dir for the SVG")
    sp.set_defaults(fn=cmd_render_kicad)

    sp = sub.add_parser("lint",
                        help="cross-check the graph against the source PNG, "
                             "the librarian, and bus/coverage heuristics")
    sp.add_argument("--board", required=True)
    sp.add_argument("--sheet", type=int, required=True)
    sp.set_defaults(fn=cmd_lint)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
