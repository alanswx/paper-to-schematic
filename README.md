# Schematics — arcade schematic transcription

Convert paper arcade schematic scans into structured digital documentation:
KiCad designs, physical-board probe lists, and paper-vs-board discrepancy
logs.

The project runs as a **Claude-driven harness**. You describe a goal
("annotate sheet 1", "add the 6809E"); Claude routes through skills under
`.agents/skills/` to do the work. A passive **Explorer** GUI handles
human-in-the-loop steps (drawing boxes, refining pin positions, toggling
`verified`).

See `AGENTS.md` for skill routing details.

## Setup (once)

```bash
python3 -m venv .venv
.venv/bin/pip install -r .agents/skills/cartographer/requirements.txt
```

Optional system tools:

- `opj_decompress` (`brew install openjpeg`) — only reliable JP2 decoder for
  archive.org scans on macOS. `sips` and ImageMagick both fail on those JP2s.
- KiCad 8 or 9 — for the eventual `.kicad_sch` export and ERC step.

## End-to-end: adding a new arcade board

### 1. Get the scans

Find the service manual on archive.org or similar. Many scans rotate D-size
sheets to fit letter pages and lose ~3× resolution; prefer copies that keep
sheets at native landscape size.

Drop originals into a manufacturer-named folder at the repo root, e.g.
`exidy/manuals/<game>.pdf` and `exidy/scans/jp2/<game>_archive_jp2.zip`.
**These inputs are immutable — never edit them.**

### 2. Decode and clean to PNG

If the scan is a JP2 archive (typical for archive.org):

```bash
exidy/tools/decode_jp2.sh <jp2-zip> <page-index> <out.png>
```

> **Not yet built** as a cartographer subcommand — the existing shell script
> in `exidy/tools/` is what the future `cartographer decode` will replace.
> Image cleaning (contrast, denoise, deskew) is also not yet built.

### 3. Create `board.json`

Add a board folder at `boards/<board-id>/`. Drop a `board.json` describing
the drawing number, sheet inventory, and the off-page-connector convention
the schematic uses. See `boards/exidy_440/board.json` as a complete example.
Required fields: `id`, `drawing_number`, `sheets[]` with each sheet's
`scan_path` (relative to the board folder).

> A `board.schema.json` validator is **not yet built**.

### 4. Annotate chips

Ask Claude: *"annotate sheet 1."* Claude will:

1. Run `cartographer tile` on the sheet PNG.
2. Read each tile in conversation, identifying chips by their bounding box,
   refdes (`U14C`), and part (`74LS245`).
3. Append each via `graph_cli add-component` with `--source ai` and a
   calibrated confidence (`0.9+` for clean printed text, `0.5–0.7` for
   hand-corrected, `≤0.4` for outline-only).
4. Report unrecognized parts.

Manual equivalents:

```bash
.venv/bin/python .agents/skills/cartographer/cartographer.py tile \
  exidy/scans/pages/sheet1.png \
  --out /tmp/<board>_s1_tiles --grid 3x3 --overlap 0.1

python3 .agents/skills/schematic-graph/graph_cli.py add-component \
  --board <board> --refdes U14C --part 74LS245 --sheet 1 \
  --bbox 1820,2240,2080,2660 --source ai --confidence 0.92
```

See `.agents/skills/identifier/SKILL.md` for the per-tile workflow contract
(coordinate translation, dedup via `owned_bbox`, confidence calibration).

### 5. Add missing parts to the librarian

If the schematic uses a chip not in `chips.json`, ask Claude: *"add the 6809E
to the librarian — fetch the Motorola datasheet."* Claude finds a primary
source, constructs the entry, runs `librarian.py add`, and validates.
**Never invent pinouts** — the SKILL.md enforces this and the validator
checks pin counts, package matching, and VCC/GND typing.

Manual equivalents:

```bash
python3 .agents/skills/librarian/librarian.py show 6809E
python3 .agents/skills/librarian/librarian.py add 6809E --from-file entry.json
python3 .agents/skills/librarian/librarian.py validate
python3 .agents/skills/librarian/librarian.py coverage boards/<board>/graph.json
```

### 6. HITL verification in the Explorer

```bash
python3 .agents/skills/explorer/server.py
# open http://127.0.0.1:8765/
```

For each AI-added component:

- Click to select.
- `V` toggles **verified** — green border, `✓` tag, ISO timestamp recorded.
- `E` opens an edit dialog (refdes / part). Pin layout regenerates if part
  changes.
- `D` deletes (false positive).
- `B` then drag = add a chip the AI missed; pick the part from the librarian
  autocomplete.
- Drag pin handles to refine pin positions when the auto-DIP layout doesn't
  match the actual schematic placement.

Save with `⌘S` / `^S`. The header shows `<verified>/<total> verified` so
you can track progress.

### 7. Trace nets

> **Not yet built.** Planned: per-pin net drawing in the Explorer (click pin
> → click pin), six edge types selectable per net (`wire`, `label`,
> `sheet_zone`, `off_page`, `bus`, `implicit_power`), automatic resolution
> of sheet-zone refs across sheets, plus a `tracer` skill for CV-assisted
> wire detection on the full image.

### 8. Run ERC

> **Not yet built.** Planned: export `.kicad_sch`, invoke
> `kicad-cli sch erc`, parse the output. ERC catches floating outputs,
> multi-driver nets, unconnected power, and bus-tap arity mismatches.

### 9. Probe the physical board

The `discrepancies.md` template lives at
`.agents/skills/schematic-graph/discrepancies.md`. Copy it to
`boards/<board>/discrepancies.md` and log each paper-vs-board diff you find
during physical probing (DMM continuity, scope on clock pins, etc.).

> The **probe list** (`probes.csv`) — automatically ranked physical
> verification targets sorted by power/ground first, then multi-sheet
> nets via `sheet_zone`, then AI low-confidence components, then ERC
> borderline cases — is **not yet built**.

### 10. Export to KiCad

> **Not yet built.** Planned: `graph_cli export-kicad --board <id>`. From
> there KiCad's own tools handle ERC, BOM, and (optionally) PCB layout.

## Skills quick reference

| Skill | Status | Purpose |
|---|---|---|
| **librarian**       | built       | Chip pinouts. CLI: `list`, `show`, `validate`, `coverage`, `add`. |
| **schematic-graph** | partial     | Graph storage + components. CLI: `add-component`, `remove-component`, `list-components`, `verify-component`, `unverify-component`, `validate`. Net + KiCad export not built. |
| **explorer**        | built       | HITL viewer. Pan/zoom, draw box, edit / delete / verify, drag pins. Net drawing not built. |
| **cartographer**    | partial     | `tile`, `to-source` built. JP2 decode and image cleaning not built (`exidy/tools/decode_jp2.sh` covers JP2 manually). |
| **identifier**      | workflow    | Per-tile chip identification by Claude. SKILL.md describes the workflow; Claude executes. |
| **tracer**          | not built   | Wire/junction/label detection across the full sheet. |
| **erc**             | not built   | KiCad ERC integration. |
| **probe-list**      | not built   | Generate ranked `probes.csv` from the graph. |

## What's there, what's missing

**Built end-to-end:**

- New chip → librarian `add` (datasheet citation enforced; pin / package /
  power-typing validated).
- Tile a sheet → AI identifies → append with provenance (`--source ai`,
  confidence).
- HITL component review (verify / edit / delete / redraw, pin refinement).

**Missing for ERC-clean KiCad output:**

- Net drawing UI + storage.
- Tracer skill (CV-assisted wire detection).
- ERC runner.
- KiCad export.
- Probe list generator.
- Discrepancy-log → ERC suppression integration.

**Missing for pipeline polish:**

- Cartographer JP2-decode CLI (shell script exists in `exidy/tools/`).
- Cartographer image cleaning (contrast / denoise / deskew).
- `board.schema.json` validator.
- Hierarchical-sheet rendering in the Explorer for sheet-zone refs.

## Milestone

ERC-clean KiCad output for the Exidy 440 logic board (drawing 77-0019) is
the proof-of-architecture target. One board round-tripping cleanly proves
the pipeline; everything after that scales by adding parts to the librarian
and pointing it at new `boards/<id>/` folders.
