"""KiCad schematic exporter — graph.json → .kicad_sch (S-expression).

Targets KiCad 8/10's sch format (version 20240108). Generates one file per
sheet. Each unique part used on the sheet gets a synthesized symbol
(rectangle body + pins from chips.json) inlined under lib_symbols. Component
instances are placed at scaled-down bbox positions; nets become wires
connecting their endpoint pins.

Coordinate system: source-image pixels are scaled to mm using a per-sheet
fit-to-A3 factor so the output fits on a 420×297 mm sheet.
"""
import hashlib
import json
import math
from pathlib import Path

# A3 - small margin for legibility.
PAPER_W_MM = 420.0
PAPER_H_MM = 297.0
PAPER_MARGIN_MM = 15.0
PIN_LENGTH_MM = 2.54  # KiCad standard 0.1"
PIN_PITCH_MM = 2.54

PIN_TYPE_MAP = {
    "input":           "input",
    "output":          "output",
    "bidir":           "bidirectional",
    "tri_state":       "tri_state",
    "passive":         "passive",
    "open_collector":  "open_collector",
    "power":           "power_in",
    "ground":          "power_in",
    "clock":           "input",
    "nc":              "unspecified",
}


def stable_uuid(seed: str) -> str:
    h = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _esc(s: str) -> str:
    """Quote a value as a KiCad string (with escaping)."""
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _kicad_label(name: str) -> str:
    """KiCad doesn't accept '/' or "'" in label/global_label names — they
    break the parser even when properly quoted. Map them to KiCad-friendly
    equivalents: '/' → '_' and trailing "'" → '~' overbar prefix (active-
    low convention). The graph.json keeps the human-readable name."""
    if name.endswith("'"):
        name = "~{" + name[:-1] + "}"
    return name.replace("/", "_")


def _f(v) -> str:
    """Format a float without trailing zeros, KiCad-friendly."""
    return f"{float(v):g}"


def synth_symbol(part_key: str, part: dict, lib: str = "user") -> str:
    """Generate a (symbol ...) lib entry for a part. Standard DIP layout."""
    pin_count = len(part["pins"])
    if pin_count == 0 or pin_count % 2:
        # Odd-pin chips not supported in v0; emit a placeholder symbol.
        return _placeholder_symbol(part_key, part, lib)

    half = pin_count // 2
    body_height = (half - 1) * PIN_PITCH_MM + 2 * PIN_PITCH_MM
    body_top = body_height / 2
    body_bot = -body_height / 2
    body_w = 12.7  # 0.5 inch wide body
    body_left = -body_w / 2
    body_right = body_w / 2

    # Pin positions: pins 1..half down the left, half+1..n up the right.
    pin_lines = []
    for p in part["pins"]:
        n = p["n"]
        if n <= half:
            slot = n - 1                    # 0 at top
            x = body_left - PIN_LENGTH_MM
            y = body_top - (slot + 0.5) * PIN_PITCH_MM
            angle = 0  # extends to the right toward body? let's see
            # KiCad convention: pin AT is the far end of the pin line. Angle is
            # the direction the pin LINE points outward (away from the body).
            # For a left-side pin pointing left, angle = 180.
            angle = 180
        else:
            slot = pin_count - n             # 0 at top-right (pin = pin_count)
            x = body_right + PIN_LENGTH_MM
            y = body_top - (slot + 0.5) * PIN_PITCH_MM
            angle = 0  # extends to the right (away from body)

        ktype = PIN_TYPE_MAP.get(p.get("type", "passive"), "passive")
        # KiCad pin name rules: ~XX for active-low becomes ~{XX} in sch v6+.
        name = p["name"]
        if name.startswith("~"):
            name = "~{" + name[1:] + "}"

        pin_lines.append(
            f'      (pin {ktype} line (at {_f(x)} {_f(y)} {angle}) (length {_f(PIN_LENGTH_MM)})\n'
            f'        (name {_esc(name)} (effects (font (size 1.27 1.27))))\n'
            f'        (number {_esc(str(n))} (effects (font (size 1.27 1.27))))\n'
            f'      )'
        )

    pins_block = "\n".join(pin_lines)

    sym = f'''  (symbol "{lib}:{part_key}"
    (pin_names (offset 0.508))
    (exclude_from_sim no)
    (in_bom yes)
    (on_board yes)
    (property "Reference" "U" (at 0 {_f(body_top + 2)} 0)
      (effects (font (size 1.27 1.27))))
    (property "Value" "{part_key}" (at 0 {_f(body_bot - 2)} 0)
      (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0)
      (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Datasheet" "" (at 0 0 0)
      (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Description" "{part.get('description', '')}" (at 0 0 0)
      (effects (font (size 1.27 1.27)) (hide yes)))
    (symbol "{part_key}_0_1"
      (rectangle (start {_f(body_left)} {_f(body_top)}) (end {_f(body_right)} {_f(body_bot)})
        (stroke (width 0.254) (type default))
        (fill (type background))
      )
    )
    (symbol "{part_key}_1_1"
{pins_block}
    )
  )'''
    return sym


def _placeholder_symbol(part_key: str, part: dict, lib: str) -> str:
    return f'''  (symbol "{lib}:{part_key}"
    (in_bom yes) (on_board yes)
    (property "Reference" "U" (at 0 5 0) (effects (font (size 1.27 1.27))))
    (property "Value" "{part_key}" (at 0 -5 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (symbol "{part_key}_0_1"
      (rectangle (start -5 5) (end 5 -5)
        (stroke (width 0.254) (type default)) (fill (type none)))
    )
  )'''


def compute_scale(sheet_pixel_size, max_w_mm, max_h_mm):
    if not sheet_pixel_size:
        return 0.05
    w, h = sheet_pixel_size
    sx = max_w_mm / w if w else 0.05
    sy = max_h_mm / h if h else 0.05
    return min(sx, sy)


def gen_sch(graph: dict, chips: dict, sheet_index: int, project_name: str = "schematic") -> str:
    """Produce a KiCad sch file for one sheet of a board."""
    sheet_meta = next((s for s in graph["sheets"] if s["index"] == sheet_index), None)
    if not sheet_meta:
        raise ValueError(f"sheet {sheet_index} not in graph")

    sheet_uuid = stable_uuid(f"{graph['board']['id']}/sheet/{sheet_index}")
    scale = compute_scale(
        sheet_meta.get("scan_pixel_size"),
        PAPER_W_MM - 2 * PAPER_MARGIN_MM,
        PAPER_H_MM - 2 * PAPER_MARGIN_MM,
    )

    components = [c for c in graph["components"] if c["sheet"] == sheet_index]
    nets = [n for n in graph.get("nets", [])
            if any(ep.get("sheet") == sheet_index for ep in n.get("endpoints", []))]

    # Build lib_symbols for unique parts present.
    parts_used = sorted({c["part"] for c in components})
    sym_defs = []
    for pk in parts_used:
        part = chips["parts"].get(pk)
        if not part:
            continue
        sym_defs.append(synth_symbol(pk, part))

    # Build symbol instances.
    inst_blocks = []
    pin_endpoint_mm = {}  # (refdes, pin_str) -> (x_mm, y_mm) for wire emission

    for comp in components:
        bbox = comp["bbox"]
        part = chips["parts"].get(comp["part"])
        if not part:
            continue
        # Place at the center of the bbox (KiCad symbols are centered around (0,0)).
        cx_px = (bbox[0] + bbox[2]) / 2
        cy_px = (bbox[1] + bbox[3]) / 2
        x_mm = PAPER_MARGIN_MM + cx_px * scale
        y_mm = PAPER_MARGIN_MM + cy_px * scale

        comp_uuid = stable_uuid(f"{graph['board']['id']}/sheet/{sheet_index}/{comp['refdes']}")

        # Compute mm position of each pin for wire endpoints, using the same DIP
        # layout the synthesized symbol uses.
        pin_count = len(part["pins"])
        if pin_count and pin_count % 2 == 0:
            half = pin_count // 2
            body_height = (half - 1) * PIN_PITCH_MM + 2 * PIN_PITCH_MM
            body_top = body_height / 2
            body_w = 12.7
            body_left = -body_w / 2
            body_right = body_w / 2
            for p in part["pins"]:
                n = p["n"]
                if n <= half:
                    slot = n - 1
                    px = body_left - PIN_LENGTH_MM
                    py = body_top - (slot + 0.5) * PIN_PITCH_MM
                else:
                    slot = pin_count - n
                    px = body_right + PIN_LENGTH_MM
                    py = body_top - (slot + 0.5) * PIN_PITCH_MM
                pin_endpoint_mm[(comp["refdes"], str(n))] = (x_mm + px, y_mm + py)

        ref_uuid_pins = []
        for p in part["pins"]:
            pu = stable_uuid(f"{comp_uuid}/pin/{p['n']}")
            ref_uuid_pins.append(f'    (pin "{p["n"]}" (uuid "{pu}"))')
        pins_block = "\n".join(ref_uuid_pins)

        inst = f'''  (symbol
    (lib_id "user:{comp['part']}")
    (at {_f(x_mm)} {_f(y_mm)} 0)
    (unit 1)
    (exclude_from_sim no)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "{comp_uuid}")
    (property "Reference" "{comp['refdes']}" (at {_f(x_mm)} {_f(y_mm - 12)} 0)
      (effects (font (size 1.27 1.27))))
    (property "Value" "{comp['part']}" (at {_f(x_mm)} {_f(y_mm + 12)} 0)
      (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at {_f(x_mm)} {_f(y_mm)} 0)
      (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Datasheet" "" (at {_f(x_mm)} {_f(y_mm)} 0)
      (effects (font (size 1.27 1.27)) (hide yes)))
{pins_block}
    (instances
      (project {_esc(project_name)}
        (path "/{sheet_uuid}"
          (reference "{comp['refdes']}") (unit 1)
        )
      )
    )
  )'''
        inst_blocks.append(inst)

    # Wires (edge_type=wire) connect each pair of endpoints with a straight
    # line. Labels (edge_type=label / sheet_zone / off_page) emit a KiCad
    # (label ...) at each endpoint's pin position so the netlist groups by
    # name even when the schematic was drawn with named nets instead of
    # explicit wires (Dorado-style).
    wire_blocks = []
    label_blocks = []
    for net in nets:
        eps_on_sheet = [e for e in net["endpoints"] if e.get("sheet") == sheet_index]
        if not eps_on_sheet:
            continue
        edge = eps_on_sheet[0].get("edge_type", "wire")

        if edge == "wire":
            if len(eps_on_sheet) < 2:
                continue
            anchor = eps_on_sheet[0]
            anchor_pos = pin_endpoint_mm.get((anchor["refdes"], str(anchor["pin"])))
            if not anchor_pos:
                continue
            for other in eps_on_sheet[1:]:
                other_pos = pin_endpoint_mm.get((other["refdes"], str(other["pin"])))
                if not other_pos:
                    continue
                wuuid = stable_uuid(f"{net['name']}/{anchor['refdes']}.{anchor['pin']}/{other['refdes']}.{other['pin']}")
                wire_blocks.append(f'''  (wire (pts (xy {_f(anchor_pos[0])} {_f(anchor_pos[1])}) (xy {_f(other_pos[0])} {_f(other_pos[1])}))
    (stroke (width 0) (type default))
    (uuid "{wuuid}")
  )''')
        elif edge in ("label", "sheet_zone", "off_page"):
            # Use (global_label ...) for sheet-spanning links (sheet_zone /
            # off_page) so KiCad's netlist matches by name across sheets;
            # plain (label ...) for in-sheet labelled nets.
            kw = "global_label" if edge in ("sheet_zone", "off_page") else "label"
            for ep in eps_on_sheet:
                pos = pin_endpoint_mm.get((ep["refdes"], str(ep["pin"])))
                if not pos:
                    continue
                # Tiny lead from pin into the label so the label is visibly
                # attached without overlapping the pin name.
                lead = 2.54
                lx, ly = pos[0] + lead, pos[1]
                luuid = stable_uuid(f"label/{net['name']}/{ep['refdes']}.{ep['pin']}")
                wuuid = stable_uuid(f"label-lead/{net['name']}/{ep['refdes']}.{ep['pin']}")
                wire_blocks.append(f'''  (wire (pts (xy {_f(pos[0])} {_f(pos[1])}) (xy {_f(lx)} {_f(ly)}))
    (stroke (width 0) (type default))
    (uuid "{wuuid}")
  )''')
                # (label ...) takes no (shape ...); only (global_label ...)
                # and (hierarchical_label ...) do — KiCad refuses to load
                # a plain label with a shape attribute.
                shape_attr = ' (shape input)' if kw == "global_label" else ""
                label_blocks.append(f'''  ({kw} {_esc(_kicad_label(net["name"]))}{shape_attr} (at {_f(lx)} {_f(ly)} 0)
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "{luuid}")
  )''')

    title = f"{graph['board'].get('title', graph['board']['id'])} sheet {sheet_index}: {sheet_meta.get('title', '')}"
    drawing_no = graph['board'].get('drawing_number', '')

    parts = [
        '(kicad_sch',
        '  (version 20240108)',
        '  (generator "paper-to-schematic")',
        '  (generator_version "0.1")',
        f'  (uuid "{sheet_uuid}")',
        '  (paper "A3")',
        '  (title_block',
        f'    (title {_esc(title)})',
        f'    (rev {_esc(drawing_no)})',
        f'    (company {_esc(graph["board"].get("manufacturer", ""))})',
        '  )',
        '  (lib_symbols',
        *sym_defs,
        '  )',
        *inst_blocks,
        *wire_blocks,
        *label_blocks,
        '  (sheet_instances',
        '    (path "/"',
        '      (page "1")',
        '    )',
        '  )',
        ')',
        '',
    ]
    return "\n".join(parts)


def parse_sexp(text: str):
    """Tiny S-expression parser — returns nested lists of strings."""
    pos = 0
    n = len(text)

    def skip_ws():
        nonlocal pos
        while pos < n and text[pos] in " \t\r\n":
            pos += 1

    def parse_atom():
        nonlocal pos
        if text[pos] == '"':
            pos += 1
            buf = []
            while pos < n and text[pos] != '"':
                if text[pos] == "\\" and pos + 1 < n:
                    buf.append(text[pos + 1])
                    pos += 2
                else:
                    buf.append(text[pos])
                    pos += 1
            pos += 1
            return '"' + "".join(buf) + '"'
        start = pos
        while pos < n and text[pos] not in " \t\r\n()":
            pos += 1
        return text[start:pos]

    def parse_list():
        nonlocal pos
        if text[pos] != "(":
            raise ValueError(f"expected '(' at {pos}")
        pos += 1
        out = []
        while True:
            skip_ws()
            if pos >= n:
                raise ValueError("unexpected EOF")
            if text[pos] == ")":
                pos += 1
                return out
            if text[pos] == "(":
                out.append(parse_list())
            else:
                out.append(parse_atom())

    skip_ws()
    return parse_list()
