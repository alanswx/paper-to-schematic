#!/usr/bin/env python3
"""Librarian — CLI for the chip pinout database.

Read SKILL.md before invoking. Never hand-edit chips.json — go through the
add subcommand so validation runs.
"""
import argparse
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
CHIPS_PATH = SKILL_DIR / "chips.json"
SCHEMA_PATH = SKILL_DIR / "chips.schema.json"


def load_chips() -> dict:
    return json.loads(CHIPS_PATH.read_text())


def save_chips(data: dict) -> None:
    CHIPS_PATH.write_text(json.dumps(data, indent=2) + "\n")


def find_part(data: dict, query: str):
    parts = data["parts"]
    if query in parts:
        return query, parts[query]
    for name, p in parts.items():
        if query in p.get("aliases", []):
            return name, p
    return None, None


def validate_part(name: str, part: dict) -> list:
    errs = []
    kind = part.get("kind", "ic")
    if kind == "discrete":
        if not part.get("kicad_symbol"):
            errs.append("discrete is missing kicad_symbol (e.g. 'Device:R')")
        pc = part.get("pin_count")
        if pc is None:
            errs.append("discrete is missing pin_count")
        elif pc < 1:
            errs.append(f"pin_count must be >=1: {pc}")
        if part.get("polarized") and pc not in (None, 2):
            # Polarized only meaningful for 2-pin parts (CP, D, LED).
            errs.append(f"polarized=true on a {pc}-pin part is unusual; verify")
        return errs

    # ic kind (default)
    pkg = part.get("package", "")
    if not pkg.startswith("DIP-"):
        return [f"package not DIP-N: {pkg!r}"]
    try:
        n = int(pkg.split("-", 1)[1])
    except ValueError:
        return [f"package malformed: {pkg!r}"]
    pins = part.get("pins", [])
    if len(pins) != n:
        errs.append(f"{pkg} but {len(pins)} pins listed")
    nums = sorted(p.get("n") for p in pins if isinstance(p.get("n"), int))
    if nums != list(range(1, n + 1)):
        errs.append(f"pin numbers not contiguous 1..{n}: {nums}")
    by_n = {p["n"]: p for p in pins if "n" in p}
    if "vcc_pin" in part:
        v = by_n.get(part["vcc_pin"])
        if not v or v.get("type") != "power":
            errs.append(f"vcc_pin={part['vcc_pin']} not typed 'power': {v}")
    if "gnd_pin" in part:
        g = by_n.get(part["gnd_pin"])
        if not g or g.get("type") != "ground":
            errs.append(f"gnd_pin={part['gnd_pin']} not typed 'ground': {g}")
    seen = set()
    for p in pins:
        if "type" not in p:
            errs.append(f"pin {p.get('n','?')} missing type")
        if "name" not in p:
            errs.append(f"pin {p.get('n','?')} missing name")
        if p.get("n") in seen:
            errs.append(f"duplicate pin number {p.get('n')}")
        seen.add(p.get("n"))
    return errs


def cmd_list(args):
    data = load_chips()
    parts = data["parts"]
    print(f"{len(parts)} parts in library:\n")
    for name in sorted(parts):
        p = parts[name]
        aliases = ", ".join(p.get("aliases", []))
        suffix = f"  (aka {aliases})" if aliases else ""
        print(f"  {name:12s}  {p['package']:8s}  {p['description']}{suffix}")


def cmd_show(args):
    data = load_chips()
    name, part = find_part(data, args.part)
    if not part:
        print(f"unknown part: {args.part!r}", file=sys.stderr)
        sys.exit(1)
    if name != args.part:
        print(f"# resolved alias {args.part!r} → {name}")
    print(f"# {name} — {part['description']}")
    print(f"# package: {part['package']}")
    if "datasheet" in part:
        print(f"# datasheet: {part['datasheet']}")
    if part.get("aliases"):
        print(f"# aliases: {', '.join(part['aliases'])}")
    print(f"# VCC pin {part.get('vcc_pin','?')}  GND pin {part.get('gnd_pin','?')}")
    print(f"# {len(part['pins'])} pins:")
    for pin in part["pins"]:
        grp = f"  [{pin['group']}]" if pin.get("group") else ""
        print(f"  {pin['n']:3d}  {pin['name']:8s}  {pin['type']}{grp}")


def cmd_validate(args):
    data = load_chips()
    parts = data["parts"]
    fail = False
    for name in sorted(parts):
        errs = validate_part(name, parts[name])
        if errs:
            fail = True
            print(f"FAIL  {name}")
            for e in errs:
                print(f"        {e}")
        else:
            print(f"  ok  {name}")
    print(f"\n{len(parts)} parts checked")
    if fail:
        sys.exit(1)


def cmd_coverage(args):
    """Check that every component.part referenced by a graph.json exists in the library."""
    graph = json.loads(Path(args.graph).read_text())
    data = load_chips()
    needed = {c["part"] for c in graph.get("components", [])}
    missing = []
    found = []
    for q in sorted(needed):
        name, part = find_part(data, q)
        (found if part else missing).append(q)
    print(f"{len(found)}/{len(needed)} parts present:")
    for n in found:
        print(f"  ok      {n}")
    for n in missing:
        print(f"  MISSING {n}")
    if missing:
        sys.exit(1)


def cmd_add(args):
    data = load_chips()
    if args.part in data["parts"]:
        print(f"already present: {args.part}", file=sys.stderr)
        sys.exit(1)
    if args.from_file:
        entry = json.loads(Path(args.from_file).read_text())
    elif args.json:
        entry = json.loads(args.json)
    else:
        if sys.stdin.isatty():
            print("provide entry via --from-file <path>, --json '<...>', or stdin", file=sys.stderr)
            sys.exit(2)
        entry = json.loads(sys.stdin.read())
    errs = validate_part(args.part, entry)
    if errs:
        print(f"validation failed for {args.part}:", file=sys.stderr)
        for e in errs:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)
    # Check alias collisions
    for existing_name, existing in data["parts"].items():
        for a in entry.get("aliases", []):
            if a in existing.get("aliases", []) or a == existing_name:
                print(f"alias collision: {a!r} already used by {existing_name}", file=sys.stderr)
                sys.exit(1)
    data["parts"][args.part] = entry
    save_chips(data)
    print(f"added {args.part} ({entry['package']}, {len(entry['pins'])} pins)")


def main():
    ap = argparse.ArgumentParser(prog="librarian", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="list all parts")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="show one part's pinout")
    sp.add_argument("part")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("validate", help="validate the entire library")
    sp.set_defaults(fn=cmd_validate)

    sp = sub.add_parser("coverage", help="check a board's parts coverage")
    sp.add_argument("graph", help="path to a board's graph.json")
    sp.set_defaults(fn=cmd_coverage)

    sp = sub.add_parser("add", help="add a new part (validated)")
    sp.add_argument("part", help="canonical part key, e.g. 6809E")
    sp.add_argument("--from-file", help="read JSON entry from file")
    sp.add_argument("--json", help="JSON entry as inline arg")
    sp.set_defaults(fn=cmd_add)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
