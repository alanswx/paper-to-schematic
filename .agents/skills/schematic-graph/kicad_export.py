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
import re
from pathlib import Path

# A chip drawn one-gate-per-symbol (e.g. quad-gate 74LSxx, MC10124) is modeled
# in the graph as N components with refdes <chipid><letter> (g01a, g01b, …)
# all referencing the same librarian part. The librarian carries the full
# pinout with a per-pin `group` tag; we use the tag to filter pin emission so
# only the active gate's pins land on each sub-component.
_REFDES_SUBUNIT = re.compile(r"^([a-z]+\d+)([a-z])$")
_UNIT_GROUP_PATTERNS = (
    re.compile(r"^g\d+$", re.IGNORECASE),     # 74LS00/04/08/32/175 quad-gate
    re.compile(r"^ff\d+$", re.IGNORECASE),    # 74LS74/174 multi-flip-flop
    re.compile(r"^[A-Z]$"),                   # MC10124 A/B/C/D
)


def _unit_groups(part: dict) -> list[str]:
    """Return the chip's sub-unit group names sorted into unit order, or [] if
    the part has no unit-shaped groups. The result is structurally valid (all
    groups match the unit name pattern) but does NOT prove the part is used
    as multi-unit on a given board — that requires graph evidence.
    """
    groups: set[str] = set()
    for pin in part.get("pins", []):
        if pin.get("type") in ("power", "ground"):
            continue
        g = pin.get("group")
        if g and g != "common":
            groups.add(g)
    if len(groups) < 2:
        return []
    if not all(any(p.match(g) for p in _UNIT_GROUP_PATTERNS) for g in groups):
        return []
    return sorted(groups, key=lambda s: (s.lower(), s))


def _multi_unit_parts(components: list[dict], chips: dict) -> set[str]:
    """Identify part keys that this board genuinely uses as multi-unit, based
    on the presence of letter-suffix refdes in the graph. A part with
    unit-shaped groups (e.g. 74LS245's A/B) but only single-unit refdes is
    treated as a regular chip — its A/B groups are functional clusters, not
    sub-units."""
    out: set[str] = set()
    for comp in components:
        if not _REFDES_SUBUNIT.match(comp["refdes"]):
            continue
        part = chips["parts"].get(comp["part"])
        if part and _unit_groups(part):
            out.add(comp["part"])
    return out


def _comp_unit_letter(refdes: str, part_key: str, multi_unit_parts: set[str]) -> str | None:
    """Return the sub-unit letter (e.g. 'a','b','c','d') for a sub-unit
    component, or None for a regular full-chip component."""
    if part_key not in multi_unit_parts:
        return None
    m = _REFDES_SUBUNIT.match(refdes)
    return m.group(2) if m else None


def _comp_lib_id(part_key: str, unit_letter: str | None) -> str:
    """Lib-symbol id used in the schematic for this component."""
    return f"{part_key}_{unit_letter}" if unit_letter else part_key


def _comp_active_pins(part: dict, unit_letter: str | None) -> list[dict]:
    """Pins that are visible on this component's symbol. For full-chip
    components: every pin. For sub-units: that gate's group + common +
    power/ground, in the same order synth_symbol's compact layout uses.
    For discretes (no `pins` array in librarian): synthesise pin records
    from `pin_count`/`polarized`."""
    if part.get("kind") == "discrete":
        pc = part.get("pin_count") or 2
        if part.get("polarized") and pc == 2:
            return [{"n": 1, "name": "+", "type": "passive"},
                    {"n": 2, "name": "-", "type": "passive"}]
        return [{"n": i + 1, "name": str(i + 1), "type": "passive"}
                for i in range(pc)]
    if unit_letter is None:
        return list(part.get("pins") or [])
    units = _unit_groups(part)
    idx = ord(unit_letter) - ord("a")
    if not units or not (0 <= idx < len(units)):
        return list(part.get("pins") or [])
    unit_group = units[idx]
    active = []
    for p in part.get("pins") or []:
        if p.get("type") in ("power", "ground"):
            active.append(p)
            continue
        g = p.get("group")
        if g == unit_group or g == "common":
            active.append(p)
    if not active:
        return list(part.get("pins") or [])

    def _order_key(pin):
        t = pin.get("type", "")
        g = pin.get("group", "")
        return (
            0 if g == unit_group else (1 if g == "common" else 2),
            0 if t in ("input", "clock") else
            1 if t in ("output", "tri_state", "open_collector", "bidir") else
            2 if t == "passive" else 3,
            pin["n"],
        )
    return sorted(active, key=_order_key)

# A3 - small margin for legibility.
PAPER_W_MM = 420.0
PAPER_H_MM = 297.0
PAPER_MARGIN_MM = 12.7  # multiple of grid so margin doesn't put placements off-grid
PIN_LENGTH_MM = 2.54  # KiCad standard 0.1"
PIN_PITCH_MM = 2.54
KICAD_GRID_MM = 1.27   # 50 mil — KiCad's default connection grid; ERC fails endpoints off this


def _snap(v: float) -> float:
    """Round v to KiCad's 50-mil connection grid. Pin offsets relative to a
    component center are already grid-multiples by construction; snapping the
    component center is enough to put every emitted endpoint on-grid."""
    return round(v / KICAD_GRID_MM) * KICAD_GRID_MM

# Map a librarian `package` string to a KiCad stock footprint id. The lib
# (Package_DIP, Package_TO_SOT_THT, etc.) ships with kicad-footprints and is
# discoverable via the user-global fp-lib-table that KiCad sets up on first
# launch — no per-project library config needed for these.
#
# Width convention for DIPs: ≤20 pins → 0.3" body (7.62mm); ≥24 pins → 0.6"
# body (15.24mm). True for every chip currently in chips.json (the ≥24-pin
# parts are all memory/CPU which were always 0.6" wide). If a future chip
# breaks this rule it'll need an explicit override on the part.
PACKAGE_TO_KICAD_FP = {
    "DIP-8":  "Package_DIP:DIP-8_W7.62mm",
    "DIP-14": "Package_DIP:DIP-14_W7.62mm",
    "DIP-16": "Package_DIP:DIP-16_W7.62mm",
    "DIP-20": "Package_DIP:DIP-20_W7.62mm",
    "DIP-24": "Package_DIP:DIP-24_W15.24mm",
    "DIP-28": "Package_DIP:DIP-28_W15.24mm",
    "DIP-32": "Package_DIP:DIP-32_W15.24mm",
    "DIP-40": "Package_DIP:DIP-40_W15.24mm",
}


def kicad_footprint_for(part: dict) -> str:
    """Map a librarian part to a KiCad stock footprint id, or "" if unknown.
    Honours an explicit `kicad_footprint` override on the part when present."""
    explicit = part.get("kicad_footprint")
    if explicit:
        return explicit
    return PACKAGE_TO_KICAD_FP.get(part.get("package", ""), "")


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


def _pin_lib_line(n_pos: int, total_visible: int, p: dict,
                  body_left: float, body_right: float, body_top: float) -> str:
    """Emit one (pin ...) line in a DIP-style layout. n_pos is the 1-based
    visible-slot index used to compute pin position; total_visible is the
    overall pin count we're laying out. Pins go down the left then up the
    right, half-and-half. The pin's own number/name/type come from `p`."""
    half = max(1, total_visible // 2)
    if n_pos <= half:
        slot = n_pos - 1
        x = body_left - PIN_LENGTH_MM
        y = body_top - (slot + 0.5) * PIN_PITCH_MM
        angle = 180
    else:
        slot = total_visible - n_pos
        x = body_right + PIN_LENGTH_MM
        y = body_top - (slot + 0.5) * PIN_PITCH_MM
        angle = 0
    ktype = PIN_TYPE_MAP.get(p.get("type", "passive"), "passive")
    name = p["name"]
    if name.startswith("~"):
        name = "~{" + name[1:] + "}"
    return (
        f'      (pin {ktype} line (at {_f(x)} {_f(y)} {angle}) (length {_f(PIN_LENGTH_MM)})\n'
        f'        (name {_esc(name)} (effects (font (size 1.27 1.27))))\n'
        f'        (number {_esc(str(p["n"]))} (effects (font (size 1.27 1.27))))\n'
        f'      )'
    )


def _full_dip_pin_line(p: dict, body_left: float, body_right: float,
                       body_top: float, half: int, pin_count: int) -> str:
    """DIP layout where each pin sits at its own pin number's slot."""
    n = p["n"]
    if n <= half:
        n_pos = n
        total = pin_count
    else:
        n_pos = n
        total = pin_count
    return _pin_lib_line(n_pos, total, p, body_left, body_right, body_top)


def synth_symbol(part_key: str, part: dict, lib: str = "user", *,
                 unit_letter: str | None = None) -> str:
    """Generate a (symbol ...) lib entry.

    If unit_letter is None: emit the full DIP layout for the chip.

    If unit_letter is set (e.g. 'a'): emit a compact symbol containing only
    that gate's pins (per librarian `group` tag) plus power/ground/common.
    The lib_id is `{part_key}_{letter}`. Each sub-unit refdes gets its own
    such symbol so KiCad sees each gate as a self-contained component with
    full power, sized to its own pin count.
    """
    pin_count = len(part["pins"])
    if pin_count == 0 or pin_count % 2:
        return _placeholder_symbol(part_key, part, lib)

    if unit_letter is None:
        # Full-chip DIP layout (existing behavior).
        half = pin_count // 2
        body_height = (half - 1) * PIN_PITCH_MM + 2 * PIN_PITCH_MM
        body_top = body_height / 2
        body_bot = -body_height / 2
        body_w = 12.7
        body_left = -body_w / 2
        body_right = body_w / 2
        pin_lines = [
            _full_dip_pin_line(p, body_left, body_right, body_top, half, pin_count)
            for p in part["pins"]
        ]
        sym_id = part_key
    else:
        units = _unit_groups(part)
        idx = ord(unit_letter) - ord("a")
        if not units or not (0 <= idx < len(units)):
            # Fall back to full-chip if the part doesn't actually have
            # unit-shaped groups for this letter.
            return synth_symbol(part_key, part, lib, unit_letter=None)
        unit_group = units[idx]
        # Pins on this sub-unit: own gate's pins + common-group pins +
        # power/ground (so each sub-component looks like a complete chip
        # to KiCad's ERC and shows VCC/GND visually).
        active = []
        for p in part["pins"]:
            if p.get("type") in ("power", "ground"):
                active.append(p)
                continue
            g = p.get("group")
            if g == unit_group or g == "common":
                active.append(p)
        if not active:
            return synth_symbol(part_key, part, lib, unit_letter=None)
        # Lay out the active pins in a compact DIP: split roughly in half,
        # left/right. Sort so I/O comes first, then common, then power.
        def _order_key(pin):
            t = pin.get("type", "")
            g = pin.get("group", "")
            return (
                0 if g == unit_group else (1 if g == "common" else 2),
                0 if t in ("input", "clock") else
                1 if t in ("output", "tri_state", "open_collector", "bidir") else
                2 if t == "passive" else 3,
                pin["n"],
            )
        active_sorted = sorted(active, key=_order_key)
        total = len(active_sorted)
        # Compact body: enough rows for half the active pins plus a top/bot pad.
        rows = max(1, (total + 1) // 2)
        body_height = (rows - 1) * PIN_PITCH_MM + 2 * PIN_PITCH_MM
        body_top = body_height / 2
        body_bot = -body_height / 2
        body_w = 12.7
        body_left = -body_w / 2
        body_right = body_w / 2
        half = (total + 1) // 2
        pin_lines = [
            _pin_lib_line(i + 1, total, p, body_left, body_right, body_top)
            for i, p in enumerate(active_sorted)
        ]
        sym_id = f"{part_key}_{unit_letter}"

    pins_block = "\n".join(pin_lines)
    sym = f'''  (symbol "{lib}:{sym_id}"
    (pin_names (offset 0.508))
    (exclude_from_sim no)
    (in_bom yes)
    (on_board yes)
    (property "Reference" "U" (at 0 {_f(body_top + 2)} 0)
      (effects (font (size 1.27 1.27))))
    (property "Value" "{sym_id}" (at 0 {_f(body_bot - 2)} 0)
      (effects (font (size 1.27 1.27))))
    (property "Footprint" "{kicad_footprint_for(part)}" (at 0 0 0)
      (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Datasheet" "" (at 0 0 0)
      (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Description" "{part.get('description', '')}" (at 0 0 0)
      (effects (font (size 1.27 1.27)) (hide yes)))
    (symbol "{sym_id}_0_1"
      (rectangle (start {_f(body_left)} {_f(body_top)}) (end {_f(body_right)} {_f(body_bot)})
        (stroke (width 0.254) (type default))
        (fill (type background))
      )
    )
    (symbol "{sym_id}_1_1"
{pins_block}
    )
  )'''
    return sym


def synth_faithful_symbol(refdes: str, part_key: str, part: dict, comp: dict,
                          scale: float, lib: str = "user") -> str:
    """Per-component lib_symbol with body sized to the source bbox and pins
    at their actual placed positions (from comp.pin_positions). Used when the
    component has pin_positions set, so the KiCad-rendered schematic matches
    the original drawing's chip layout (instead of a generic DIP).

    Each component owns its own lib_symbol (sym_id = `_chip_<refdes>`)
    because two different `74LS00` instances on the same page may have been
    drawn at different sizes/orientations on the original.

    The body rectangle is the bbox shrunk by PIN_LENGTH on each side, so the
    visible pin line extends from the body to the pin's (at) coord — which
    sits on the schematic dot where wires attach.
    """
    bbox = comp["bbox"]
    pin_pos = comp.get("pin_positions") or {}
    # Instance origin is the centre of the click-target bbox (pin coords are
    # relative to this). Body rectangle prefers the tighter body_bbox so the
    # rendered chip outline matches the original drawing; falls back to bbox
    # shrunk by PIN_LENGTH on each side when body_bbox isn't set.
    cx_px = (bbox[0] + bbox[2]) / 2
    cy_px = (bbox[1] + bbox[3]) / 2
    body_bbox = comp.get("body_bbox") or bbox
    if body_bbox is bbox:
        bw_mm = abs(bbox[2] - bbox[0]) * scale
        bh_mm = abs(bbox[3] - bbox[1]) * scale
        body_half_w = max(1.0, bw_mm / 2 - PIN_LENGTH_MM)
        body_half_h = max(1.0, bh_mm / 2 - PIN_LENGTH_MM)
    else:
        # body_bbox is centred on its own midpoint; convert to offsets relative
        # to the instance origin (= bbox centre) so the body lands where it
        # was drawn on the source, not where bbox-centred would put it.
        body_cx = (body_bbox[0] + body_bbox[2]) / 2
        body_cy = (body_bbox[1] + body_bbox[3]) / 2
        body_half_w = max(1.0, abs(body_bbox[2] - body_bbox[0]) / 2 * scale)
        body_half_h = max(1.0, abs(body_bbox[3] - body_bbox[1]) / 2 * scale)
        # Shift so the body's centre aligns with where it sits on the source.
        # In symbol-local coords (Y up): +Y shift = body drawn ABOVE origin.
        dx = (body_cx - cx_px) * scale
        dy = -(body_cy - cy_px) * scale
        # Apply offset to the body rect; pin positions are unaffected because
        # they come from pin_pos directly.
        body_left = dx - body_half_w
        body_right = dx + body_half_w
        body_top = dy + body_half_h
        body_bot = dy - body_half_h
    if body_bbox is bbox:
        body_left = -body_half_w
        body_right = body_half_w
        body_top = body_half_h     # symbol-local +Y is up
        body_bot = -body_half_h

    # Snap the instance origin once. The lib_symbol's pin coords are then
    # derived as (absolute_pin_tip - instance_origin) where the absolute pin
    # tip uses the SAME single-snap formula every other coord-emitting path
    # uses (polylines, junctions, discretes). That way pin tips always agree
    # on absolute mm, even though the lib_symbol's local pin offset may not
    # be a whole grid unit (body↔pin shift up to half a grid is invisible at
    # paper scale and connectivity is what matters).
    inst_x_mm = _snap(PAPER_MARGIN_MM + cx_px * scale)
    inst_y_mm = _snap(PAPER_MARGIN_MM + cy_px * scale)

    pin_lines = []
    for p in part["pins"]:
        n_str = str(p["n"])
        if n_str not in pin_pos:
            continue
        ix, iy = pin_pos[n_str]
        pin_tip_x_mm = _snap(PAPER_MARGIN_MM + ix * scale)
        pin_tip_y_mm = _snap(PAPER_MARGIN_MM + iy * scale)
        # Symbol-local coords: relative to instance origin, with Y flipped
        # because schematic Y is down and symbol-local Y is up.
        x = pin_tip_x_mm - inst_x_mm
        y = -(pin_tip_y_mm - inst_y_mm)
        # Pick the body edge this pin is nearest, set the outward angle.
        dleft = abs(x - body_left)
        dright = abs(x - body_right)
        dtop = abs(y - body_top)
        dbot = abs(y - body_bot)
        m = min(dleft, dright, dtop, dbot)
        if m == dleft: angle = 180
        elif m == dright: angle = 0
        elif m == dtop: angle = 90
        else: angle = 270
        ktype = PIN_TYPE_MAP.get(p.get("type", "passive"), "passive")
        name = p["name"]
        if name.startswith("~"):
            name = "~{" + name[1:] + "}"
        pin_lines.append(
            f'      (pin {ktype} line (at {_f(x)} {_f(y)} {angle}) (length {_f(PIN_LENGTH_MM)})\n'
            f'        (name {_esc(name)} (effects (font (size 1.27 1.27))))\n'
            f'        (number {_esc(n_str)} (effects (font (size 1.27 1.27))))\n'
            f'      )'
        )
    pins_block = "\n".join(pin_lines) if pin_lines else ""

    sym_id = f"_chip_{refdes}"
    return f'''  (symbol "{lib}:{sym_id}"
    (pin_names (offset 0.508))
    (exclude_from_sim no)
    (in_bom yes)
    (on_board yes)
    (property "Reference" "U" (at 0 {_f(body_top + 2)} 0)
      (effects (font (size 1.27 1.27))))
    (property "Value" "{part_key}" (at 0 {_f(body_bot - 2)} 0)
      (effects (font (size 1.27 1.27))))
    (property "Footprint" "{kicad_footprint_for(part)}" (at 0 0 0)
      (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Datasheet" "" (at 0 0 0)
      (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Description" "{part.get('description', '')}" (at 0 0 0)
      (effects (font (size 1.27 1.27)) (hide yes)))
    (symbol "{sym_id}_0_1"
      (rectangle (start {_f(body_left)} {_f(body_top)}) (end {_f(body_right)} {_f(body_bot)})
        (stroke (width 0.254) (type default))
        (fill (type background))
      )
    )
    (symbol "{sym_id}_1_1"
{pins_block}
    )
  )'''


def synth_power_symbol(power_name: str, lib: str = "user") -> str:
    """A 1-pin power-source symbol whose single pin is power_out, named after
    the supplied net name (VCC, GND, +5V, …). Used to drive the chips'
    power_in pins so KiCad's ERC stops flagging power_pin_not_driven."""
    safe = "".join(c if c.isalnum() else "_" for c in power_name) or "PWR"
    return f'''  (symbol "{lib}:PWR_{safe}"
    (power) (pin_names (offset 0)) (exclude_from_sim no) (in_bom no) (on_board no)
    (property "Reference" "#PWR" (at 0 -3.81 0)
      (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "{power_name}" (at 0 3.81 0)
      (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (symbol "PWR_{safe}_0_1"
      (polyline (pts (xy -1.27 1.27) (xy 0 0) (xy 1.27 1.27))
        (stroke (width 0) (type default)) (fill (type none)))
    )
    (symbol "PWR_{safe}_1_1"
      (pin power_out line (at 0 0 90) (length 0)
        (name "{power_name}" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27))))
      )
    )
  )'''


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


def _bg_image_block(scan_path: Path, scale: float, max_px: int = 2000) -> str | None:
    """EXPERIMENTAL — KiCad refuses the resulting schematic ("Failed to
    load schematic") in current testing. The (image ...) format is not
    yet right; needs investigation against a known-good hand-authored
    KiCad sch with an embedded image. Left here as a starting point for
    the next iteration on visual round-trip with KiCad rendering.

    Build a (image ...) block that places the (downsampled) source PNG
    at the same coordinate origin as the synthesized chip symbols.
    Returns None if cv2 isn't available or the file can't be read."""
    try:
        import cv2, base64, io
    except ImportError:
        return None
    img = cv2.imread(str(scan_path), cv2.IMREAD_COLOR)
    if img is None:
        return None
    H, W = img.shape[:2]
    if max(W, H) > max_px:
        f = max_px / max(W, H)
        new_w = int(W * f); new_h = int(H * f)
        img = cv2.resize(img, (new_w, new_h))
        downsample_factor = f
    else:
        downsample_factor = 1.0
    ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    if not ok:
        return None
    b64 = base64.b64encode(bytes(buf)).decode("ascii")
    # Wrap base64 to 76-char lines for s-expression friendliness.
    lines = "\n      ".join(b64[i:i+76] for i in range(0, len(b64), 76))

    # KiCad places the image's CENTRE at (at x y). Native image scale 1.0
    # means render at the embedded PNG's native pixel size where 1 px is
    # (effectively) 25.4/300 mm if the PNG carried 300 DPI metadata, or
    # whatever the file claims. To get our target mm-per-source-pixel, we
    # use the ratio: target_mm_per_px / (downsampled_image's_native_mm_per_px).
    # In practice we assume cv2-encoded PNG = 100 DPI default, but KiCad
    # actually treats `scale 1.0` as "1 image pixel == 1 internal unit
    # roughly" — so we tune empirically: image_scale ≈ source_scale_factor /
    # downsample_factor * a constant. Treat this as a rough first cut and
    # adjust visually.
    image_scale = scale / max(downsample_factor, 1e-6) * 3.78  # 3.78 ≈ 96 DPI px-per-mm reciprocal
    cx_mm = PAPER_MARGIN_MM + (W / downsample_factor) * scale / 2 if downsample_factor else PAPER_MARGIN_MM
    cy_mm = PAPER_MARGIN_MM + (H / downsample_factor) * scale / 2 if downsample_factor else PAPER_MARGIN_MM
    image_uuid = stable_uuid(f"bg/{scan_path.name}")
    return (f'  (image (at {_f(cx_mm)} {_f(cy_mm)}) (scale {_f(image_scale)})\n'
            f'    (uuid "{image_uuid}")\n'
            f'    (data\n'
            f'      "{lines}"\n'
            f'    )\n'
            f'  )')


def gen_sch(graph: dict, chips: dict, sheet_index: int, project_name: str = "schematic", *, bg_image: bool = False, scan_path: Path | None = None) -> str:
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

    # Build lib_symbols.
    #   - kind="discrete" parts (R, C, SW_Push, …) reference KiCad stock
    #     symbols (Device:R, etc.) directly — no synthesis. The instance's
    #     lib_id is the stock id; KiCad resolves it from the global symbol
    #     library installed alongside KiCad.
    #   - kind="ic" with pin_positions: per-component faithful symbol sized
    #     to the source bbox with pins at placed positions (matches source).
    #   - kind="ic" without pin_positions: existing per-(part, letter)
    #     generic synthesized symbol — fallback for partly-transcribed sheets.
    multi_unit_parts = _multi_unit_parts(components, chips)
    sym_defs = []
    fallback_lib_ids: dict[tuple[str, str | None], dict] = {}
    faithful_refdes: set[str] = set()
    discrete_refdes: set[str] = set()
    for c in components:
        part = chips["parts"].get(c["part"])
        if not part:
            continue
        if part.get("kind") == "discrete":
            discrete_refdes.add(c["refdes"])
            continue  # no lib_symbol synthesis — KiCad has the stock one.
        if c.get("pin_positions"):
            sym_defs.append(synth_faithful_symbol(c["refdes"], c["part"], part, c, scale))
            faithful_refdes.add(c["refdes"])
        else:
            letter = _comp_unit_letter(c["refdes"], c["part"], multi_unit_parts)
            fallback_lib_ids[(c["part"], letter)] = part
    for (pk, letter), part in sorted(fallback_lib_ids.items(), key=lambda x: (x[0][0], x[0][1] or "")):
        sym_defs.append(synth_symbol(pk, part, unit_letter=letter))

    # Collect power/ground pins on this sheet, grouped by their library pin
    # name (typically "VCC" / "GND"). Each unique name gets one synthesized
    # power-source symbol + one instance + one global_label per chip pin.
    # Driving chips' power_in pins from a power_out source clears KiCad's
    # power_pin_not_driven errors automatically — the alternative is asking
    # the LLM to remember to add power flags every time, which it won't.
    # Each sub-unit instance carries its own copy of the chip's power pins
    # (since KiCad sees g01a/b/c/d as four separate components by refdes).
    # Each gets a global_label so they all join the same VCC/GND net.
    power_pin_groups: dict[str, list[tuple[str, int, str]]] = {}
    for comp in components:
        part = chips["parts"].get(comp["part"])
        if not part:
            continue
        # Discretes don't enumerate pins in the librarian — their power
        # connections are external (a resistor's two passive leads connect
        # to whatever the schematic says).
        for p in part.get("pins", []):
            if p.get("type") in ("power", "ground"):
                pname = p.get("name", "").lstrip("~") or ("VCC" if p["type"] == "power" else "GND")
                power_pin_groups.setdefault(pname, []).append(
                    (comp["refdes"], p["n"], p["type"]))

    for pname in sorted(power_pin_groups):
        sym_defs.append(synth_power_symbol(pname))

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
        x_mm = _snap(PAPER_MARGIN_MM + cx_px * scale)
        y_mm = _snap(PAPER_MARGIN_MM + cy_px * scale)

        comp_uuid = stable_uuid(f"{graph['board']['id']}/sheet/{sheet_index}/{comp['refdes']}")
        is_faithful = comp["refdes"] in faithful_refdes
        is_discrete = comp["refdes"] in discrete_refdes

        if is_discrete:
            # Discretes reference KiCad stock symbols (Device:R, Switch:SW_Push,
            # etc.) directly. We don't synthesize a lib_symbol; KiCad resolves
            # the lib_id from its global symbol library (installed alongside
            # KiCad). Pin endpoints come from comp.pin_positions snapped to
            # the grid — same as the IC faithful path. The stock symbol's
            # internal pin spacing may differ slightly from the source, so a
            # short visible offset between the stock symbol and the wire
            # endpoint is possible; the connectivity is still right.
            lib_id_full = part.get("kicad_symbol") or "Device:Unknown"
            pin_pos = comp.get("pin_positions") or {}
            pin_count = part.get("pin_count") or len(pin_pos)
            polarized = bool(part.get("polarized"))
            if polarized and pin_count == 2:
                derived_pins = [{"n": 1, "name": "+", "type": "passive"},
                                {"n": 2, "name": "-", "type": "passive"}]
            else:
                derived_pins = [{"n": i + 1, "name": str(i + 1), "type": "passive"}
                                for i in range(pin_count)]
            active_pins = [p for p in derived_pins if str(p["n"]) in pin_pos]
            for p in active_pins:
                ix, iy = pin_pos[str(p["n"])]
                ex_mm = _snap(PAPER_MARGIN_MM + ix * scale)
                ey_mm = _snap(PAPER_MARGIN_MM + iy * scale)
                pin_endpoint_mm[(comp["refdes"], str(p["n"]))] = (ex_mm, ey_mm)
        elif is_faithful:
            # Faithful path: pin tips use the single-snap formula
            # `_snap(M + ix * scale)` so they match polylines, junctions, and
            # discrete pins emitted by other code paths. (synth_faithful_symbol
            # uses the same formula internally and derives its lib-symbol
            # local coords from absolute pin tip minus instance origin.)
            lib_id = f"_chip_{comp['refdes']}"
            pin_pos = comp.get("pin_positions") or {}
            active_pins = [p for p in part["pins"] if str(p["n"]) in pin_pos]
            for p in active_pins:
                ix, iy = pin_pos[str(p["n"])]
                ex_mm = _snap(PAPER_MARGIN_MM + ix * scale)
                ey_mm = _snap(PAPER_MARGIN_MM + iy * scale)
                pin_endpoint_mm[(comp["refdes"], str(p["n"]))] = (ex_mm, ey_mm)
        else:
            unit_letter = _comp_unit_letter(comp["refdes"], comp["part"], multi_unit_parts)
            lib_id = _comp_lib_id(comp["part"], unit_letter)
            active_pins = _comp_active_pins(part, unit_letter)

            # Compute mm position of each active pin, mirroring synth_symbol's
            # layout (full-chip DIP for regular components, compact left/right
            # split for sub-units).
            pin_count = len(part["pins"])
            if unit_letter is None:
                if pin_count and pin_count % 2 == 0:
                    half = pin_count // 2
                    body_height = (half - 1) * PIN_PITCH_MM + 2 * PIN_PITCH_MM
                    body_top = body_height / 2
                    body_w = 12.7
                    body_left = -body_w / 2
                    body_right = body_w / 2
                    for p in active_pins:
                        n = p["n"]
                        if n <= half:
                            slot = n - 1
                            px = body_left - PIN_LENGTH_MM
                            py = body_top - (slot + 0.5) * PIN_PITCH_MM
                        else:
                            slot = pin_count - n
                            px = body_right + PIN_LENGTH_MM
                            py = body_top - (slot + 0.5) * PIN_PITCH_MM
                        pin_endpoint_mm[(comp["refdes"], str(n))] = (x_mm + px, y_mm - py)
            else:
                # Compact sub-unit layout: pins listed in active_pins order, half
                # on the left, half on the right.
                total = len(active_pins)
                rows = max(1, (total + 1) // 2)
                body_height = (rows - 1) * PIN_PITCH_MM + 2 * PIN_PITCH_MM
                body_top = body_height / 2
                body_w = 12.7
                body_left = -body_w / 2
                body_right = body_w / 2
                half = (total + 1) // 2
                for i, p in enumerate(active_pins):
                    n_pos = i + 1
                    if n_pos <= half:
                        slot = n_pos - 1
                        px = body_left - PIN_LENGTH_MM
                        py = body_top - (slot + 0.5) * PIN_PITCH_MM
                    else:
                        slot = total - n_pos
                        px = body_right + PIN_LENGTH_MM
                        py = body_top - (slot + 0.5) * PIN_PITCH_MM
                    pin_endpoint_mm[(comp["refdes"], str(p["n"]))] = (x_mm + px, y_mm - py)

        ref_uuid_pins = []
        for p in active_pins:
            pu = stable_uuid(f"{comp_uuid}/pin/{p['n']}")
            ref_uuid_pins.append(f'    (pin "{p["n"]}" (uuid "{pu}"))')
        pins_block = "\n".join(ref_uuid_pins)

        # Discretes use the stock symbol's full id ("Device:R"); ICs prepend
        # the "user:" lib prefix because the synthesized symbols live in
        # the inline lib_symbols block of this very .kicad_sch.
        if is_discrete:
            inst_lib_id = lib_id_full
            # Discretes show the *value* (1k, 22p, …) as the Value property —
            # that's what the BOM aggregates by. Fall back to the part name
            # when no value was set yet, so a placed-but-unsized resistor
            # still has a meaningful label.
            value_prop = comp.get("value") or comp["part"]
        else:
            inst_lib_id = f"user:{lib_id}"
            value_prop = comp["part"]

        inst = f'''  (symbol
    (lib_id "{inst_lib_id}")
    (at {_f(x_mm)} {_f(y_mm)} 0)
    (unit 1)
    (exclude_from_sim no)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "{comp_uuid}")
    (property "Reference" "{comp['refdes']}" (at {_f(x_mm)} {_f(y_mm - 12)} 0)
      (effects (font (size 1.27 1.27))))
    (property "Value" "{value_prop}" (at {_f(x_mm)} {_f(y_mm + 12)} 0)
      (effects (font (size 1.27 1.27))))
    (property "Footprint" "{kicad_footprint_for(part)}" (at {_f(x_mm)} {_f(y_mm)} 0)
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

    # Power-source pseudo-components: one (symbol ...) instance per unique
    # power-net name, placed in a column at the right margin. A global_label
    # at the source's pin position pairs with global_labels emitted at each
    # chip's power pin; KiCad's netlist groups them by name.
    pwr_x_mm = _snap(PAPER_W_MM - PAPER_MARGIN_MM - 6 * KICAD_GRID_MM)
    pwr_y0_mm = _snap(PAPER_MARGIN_MM + 4 * KICAD_GRID_MM)
    for i, pname in enumerate(sorted(power_pin_groups)):
        safe = "".join(c if c.isalnum() else "_" for c in pname) or "PWR"
        srefdes = f"#PWR_{safe}"
        sx = pwr_x_mm
        sy = _snap(pwr_y0_mm + i * 8 * KICAD_GRID_MM)
        suuid = stable_uuid(f"{graph['board']['id']}/sheet/{sheet_index}/{srefdes}")
        inst_blocks.append(f'''  (symbol
    (lib_id "user:PWR_{safe}")
    (at {_f(sx)} {_f(sy)} 0)
    (unit 1) (exclude_from_sim no) (in_bom no) (on_board no) (dnp no)
    (uuid "{suuid}")
    (property "Reference" "{srefdes}" (at {_f(sx)} {_f(sy - 5)} 0)
      (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "{pname}" (at {_f(sx)} {_f(sy + 3)} 0)
      (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at {_f(sx)} {_f(sy)} 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Datasheet" "" (at {_f(sx)} {_f(sy)} 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "{stable_uuid(suuid + '/pin/1')}"))
    (instances
      (project {_esc(project_name)}
        (path "/{sheet_uuid}"
          (reference "{srefdes}") (unit 1)
        )
      )
    )
  )''')

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
            # Faithful routing: when the net carries a `path` (source-image
            # pixel polyline from the tracer), emit one (wire) per consecutive
            # pair, snapped to the KiCad grid. Without a path, fall back to a
            # one-corner Manhattan route between each pair of endpoints — a
            # right-angle approximation that's at least not diagonal and gives
            # KiCad something loadable until the tracer fills the path in.
            net_path = net.get("path")
            if net_path:
                pts_mm = [(_snap(PAPER_MARGIN_MM + p[0] * scale),
                           _snap(PAPER_MARGIN_MM + p[1] * scale))
                          for p in net_path]
                for i in range(1, len(pts_mm)):
                    a, b = pts_mm[i - 1], pts_mm[i]
                    if a == b:
                        continue
                    wuuid = stable_uuid(f"{net['name']}/seg/{i}")
                    wire_blocks.append(f'''  (wire (pts (xy {_f(a[0])} {_f(a[1])}) (xy {_f(b[0])} {_f(b[1])}))
    (stroke (width 0) (type default))
    (uuid "{wuuid}")
  )''')
            else:
                anchor = eps_on_sheet[0]
                anchor_pos = pin_endpoint_mm.get((anchor["refdes"], str(anchor["pin"])))
                if not anchor_pos:
                    continue
                for other in eps_on_sheet[1:]:
                    other_pos = pin_endpoint_mm.get((other["refdes"], str(other["pin"])))
                    if not other_pos:
                        continue
                    # H-then-V Manhattan: corner at (other.x, anchor.y).
                    cx_mm, cy_mm = _snap(other_pos[0]), _snap(anchor_pos[1])
                    pts = [anchor_pos, (cx_mm, cy_mm), other_pos]
                    for i in range(1, len(pts)):
                        a, b = pts[i - 1], pts[i]
                        if a == b:
                            continue
                        wuuid = stable_uuid(f"{net['name']}/{anchor['refdes']}.{anchor['pin']}/{other['refdes']}.{other['pin']}/{i}")
                        wire_blocks.append(f'''  (wire (pts (xy {_f(a[0])} {_f(a[1])}) (xy {_f(b[0])} {_f(b[1])}))
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

    # Global labels for every power/ground pin (chip side AND source side)
    # so KiCad's netlist groups them by name, which clears
    # power_pin_not_driven and power_pin_not_connected for free.
    for pname, members in power_pin_groups.items():
        # Chip-side label at each power pin position.
        for refdes, pin_n, _ptype in members:
            pos = pin_endpoint_mm.get((refdes, str(pin_n)))
            if not pos:
                continue
            luuid = stable_uuid(f"pwrlbl/{pname}/{refdes}.{pin_n}")
            label_blocks.append(
                f'  (global_label {_esc(_kicad_label(pname))} (shape input) '
                f'(at {_f(pos[0])} {_f(pos[1])} 0)\n'
                f'    (effects (font (size 1.27 1.27)) (justify left))\n'
                f'    (uuid "{luuid}")\n  )')
    # Source-side label per power-source instance.
    for i, pname in enumerate(sorted(power_pin_groups)):
        sx = pwr_x_mm
        sy = _snap(pwr_y0_mm + i * 8 * KICAD_GRID_MM)
        luuid = stable_uuid(f"pwrlbl/{pname}/source")
        label_blocks.append(
            f'  (global_label {_esc(_kicad_label(pname))} (shape output) '
            f'(at {_f(sx)} {_f(sy)} 0)\n'
            f'    (effects (font (size 1.27 1.27)) (justify left))\n'
            f'    (uuid "{luuid}")\n  )')

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
        *([b for b in [_bg_image_block(scan_path, scale) if bg_image and scan_path else None] if b]),
        *inst_blocks,
        *wire_blocks,
        *label_blocks,
        # Connection-dot junctions: KiCad needs an explicit (junction) at every
        # spot where two wires cross AND connect. Without one, KiCad treats the
        # crossing as independent (potential phantom open). Tracer emits these
        # alongside paths.
        *[
            f'  (junction (at {_f(_snap(PAPER_MARGIN_MM + jx * scale))} {_f(_snap(PAPER_MARGIN_MM + jy * scale))}) (diameter 0) (color 0 0 0 0)\n    (uuid "{stable_uuid(f"junc/{sheet_index}/{ji}")}")\n  )'
            for ji, (jx, jy) in enumerate(sheet_meta.get("junctions") or [])
        ],
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
