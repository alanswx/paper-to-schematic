# Schematics — arcade schematic transcription harness

Claude-driven pipeline that converts paper arcade schematic scans into structured
digital documentation: KiCad designs, physical-board probe lists, and per-board
discrepancy logs.

The control loop is **Claude in conversation**, not a UI. The user describes a
goal ("transcribe Exidy 440 sheet 1"), Claude reads the relevant board's
`board.json`, picks skills from `.agents/skills/`, and drives them. The explorer
GUI is a passive viewer for human-in-the-loop verification — it shows what's
already in the graph; it does not control the workflow.

## Skill routing

Skills live canonically under `.agents/skills/<name>/SKILL.md` with symlinks at
`.claude/skills/<name>` for Claude Code compatibility. Read each SKILL.md before
invoking that skill — the SKILL.md is the contract.

- `.agents/skills/librarian/SKILL.md` — chip pinout database. Use when adding a
  new chip, looking up a pinout, or validating that all parts referenced by a
  board are in the library.
- `.agents/skills/schematic-graph/SKILL.md` — load/save/edit/validate the graph
  data structure for a board. Use when adding components, drawing nets, exporting
  to KiCad, or running ERC.
- `.agents/skills/explorer/SKILL.md` — local web viewer that overlays the current
  graph on the source schematic image. Read-mostly; the human uses it to draw
  bounding boxes and refine pin positions. Start the explorer when the user
  needs to visually verify a step.
- `.agents/skills/cartographer/SKILL.md` — image preprocessing (JP2 decode,
  contrast, deskew). Run once per source scan, before transcription.

## Boards

Each board lives under `boards/<board-id>/` with:

- `board.json` — drawing number, sheet inventory, off-page convention, the
  preferred scan source for each sheet.
- `graph.json` — current transcription state (components + nets), validated
  against `.agents/skills/schematic-graph/graph.schema.json`.
- `probes.csv` — physical-board verification list (rows conform to
  `.agents/skills/schematic-graph/probes.schema.json`).
- `discrepancies.md` — paper-vs-board diffs found during probing.

`board.json` is the entry point for any board. It points at the input scans
(under `exidy/` or another manufacturer-named folder) via relative paths.

## Inputs are immutable

Source scans (`exidy/scans/...`, `exidy/manuals/...`) are treated as read-only
inputs. Never edit them. Cartographer outputs derived PNGs alongside; those are
also derived artifacts and should not be hand-edited.

## Source of truth

- The chip library: `.agents/skills/librarian/chips.json` (managed by the
  librarian skill — never hand-edit).
- A board's graph: `boards/<id>/graph.json` (managed by the schematic-graph
  skill, plus the explorer for HITL operations).
- A board's probe list: `boards/<id>/probes.csv` (regenerated from the graph
  by the schematic-graph skill).
- A board's discrepancy log: `boards/<id>/discrepancies.md` (hand-edited by
  the human after physical-board probing).

## Workflow

1. **User:** "Transcribe Exidy 440 sheet 1."
2. **Claude:** reads `boards/exidy_440/board.json` to find the sheet PNG.
3. **Claude:** validates parts coverage with the librarian skill. For any
   unknown parts, fetches a primary datasheet, constructs an entry, runs
   `librarian.py add`, validates.
4. **Claude:** detects components on the sheet (manually for now; an
   identifier skill is planned).
5. **Claude:** for each component, calls the schematic-graph skill to add it
   to `graph.json`.
6. **User:** opens the explorer to visually verify and refine pin positions.
7. **Claude:** traces wires (manually + future tracer skill), classifies edge
   types (wire / label / sheet_zone / off_page / bus / implicit_power), adds
   nets to `graph.json`.
8. **Claude:** runs ERC, generates `probes.csv`, reports findings.
9. Iterate until ERC clean and the probe list is humanly walkthrough-able.

## Deterministic vs LLM — division of labour

Every step of the pipeline is one of these two flavours, and getting the split
right is what makes the harness work. The rule:

> **CV / file I/O / schemas are deterministic. Recognition is the LLM.**

Recognition means anything that involves reading text, identifying a chip,
deciding where a bbox should be, or interpreting a labelled signal name. CV
gets the deterministic geometry: tile, crop, mask, threshold, line skeleton,
connected components.

| Step                                | Deterministic side                              | LLM side                                                            |
|-------------------------------------|-------------------------------------------------|---------------------------------------------------------------------|
| Tile a sheet                        | `cartographer tile` (grid + overlap)            | —                                                                   |
| Identify chips on a tile            | —                                               | Claude reads tile PNGs and reports refdes/part/bbox                 |
| Place a chip's bbox                 | `graph_cli add-component` (file write + schema) | Claude estimates bbox edges from the tile                            |
| Refine bbox to chip outline         | `cartographer snap-board` *if* it's correct     | If snap is wrong, replace with vision-placed bbox (don't tune CV)   |
| Crop one chip at high resolution    | `cartographer crop-chip`                        | —                                                                   |
| Number the pins                     | —                                               | Claude reads the crop, emits `{"<pin>": [x,y], ...}`                |
| Set pin positions                   | `graph_cli set-pin-positions`                   | —                                                                   |
| Translate identical chips           | bbox-delta translation (deterministic)          | —                                                                   |
| Read net labels (MCD.0, etc.)       | —                                               | Claude reads chip crops and lists per-pin label text                |
| Add a labelled net                  | `graph_cli add-net`                             | —                                                                   |
| Trace direct wires (no labels)      | `tracer trace` (skeleton + CC + pin matching)   | —                                                                   |
| Decide junction-vs-crossing         | dot-detector morphology                         | If the schematic uses labels, skip CV junction detection altogether |
| Validate the schematic              | `graph_cli validate`, `kicad-cli sch erc`       | —                                                                   |
| Generate the probe list             | `graph_cli probe-list`                          | —                                                                   |

Anti-pattern signal: when CV recognition gives a wrong answer, the fix is
**not** to tune the CV harder (kernel sizes, fallbacks, size penalties).
Drop the step and have Claude read the image. We learned this twice — once
when pytesseract OCR for pin numbers was replaced with `crop-chip` + Claude
vision, and again when `snap-board` mis-snapped Dorado chips and we placed
the bboxes by visual reading instead.

Snap-bbox can stay as an *opportunistic* refinement: if it produces a chip-
sized rectangle near the vision bbox, accept it; if it shrinks or distorts,
keep the vision bbox. CV should never overrule recognition.

## Constraints

- **Never invent pinouts.** Hallucinated pin assignments propagate as wrong
  netlists. If a primary source can't be found for a pin, mark it `nc`.
- **Verify before writing to the library.** `librarian.py validate` runs on
  every change — pin counts, package matching, VCC/GND typing.
- **Off-page conventions are board-specific.** Read each board's
  `off_page_convention` field in `board.json` before classifying edges.
- **Probe list is a first-class output.** Anything the AI is uncertain about
  goes there, ranked. The human's time at the workbench is the limiting
  resource.
