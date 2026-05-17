---
name: explorer
description: Local read-mostly schematic viewer for human verification. Use when starting or consulting the web explorer to view source sheets with graph overlays, draw component bboxes, or refine pin positions interactively.
---

# Explorer skill

Local web viewer that renders a board's source schematic image with the current
`graph.json` overlay. The Explorer is **read-mostly**: it displays what's in
the graph and lets the human do mouse-driven HITL operations (drawing
component bboxes, refining pin positions). The agent does not interact with
the Explorer programmatically — it edits `graph.json` directly via the
schematic-graph skill, and the Explorer reflects those edits on reload.

## When to use

- The user wants to visually verify a step Claude has just done.
- The human needs to draw component bounding boxes by hand (until the
  identifier skill is built).
- The human needs to refine auto-generated pin positions to match where pins
  actually appear on the schematic.

## Run

```bash
python3 .agents/skills/explorer/server.py
# then open http://127.0.0.1:8765/
```

Stdlib HTTP server, no dependencies. `VIEWER_PORT` and `VIEWER_BOARD` env vars
override defaults.

## What the Explorer shows

- The selected board's sheet PNG, with pan/zoom.
- All `components` for the current sheet, drawn as colored bboxes with
  refdes + part labels.
- For the selected component: pin overlay (color-coded by pin type),
  draggable to refine positions; full pin table in the sidebar.

## What the Explorer can do (HITL only)

- Draw a bbox + assign a refdes + part name → appends a component to
  `graph.json`. Part must already exist in the Librarian; if not, the user
  asks Claude to add it via the librarian skill.
- Drag pin handles → updates `pin_positions` in `graph.json`.
- `⌘S` / `^S` → PUTs `graph.json` to disk.

## What the Explorer cannot do

- Run ERC, generate KiCad, fetch datasheets, add chips to the Librarian, or
  trace nets. Those go through Claude + the relevant skill.

## Architecture

`server.py` is a stdlib `http.server.ThreadingHTTPServer` with these endpoints:

| Method | Path                  | Purpose                                |
|--------|-----------------------|----------------------------------------|
| GET    | `/`                   | the SPA                                |
| GET    | `/static/*`           | JS/CSS                                 |
| GET    | `/api/board`          | `boards/<id>/board.json`               |
| GET    | `/api/chips`          | `.agents/skills/librarian/chips.json`  |
| GET    | `/api/graph`          | `boards/<id>/graph.json`               |
| GET    | `/api/sheet/<n>.png`  | resolves to the scan path in board.json|
| PUT    | `/api/graph`          | saves the graph                        |

Path-traversal guard: scan paths must resolve under `PROJECT_ROOT`.

## Known overlay gaps — fix when working on this skill

### 1. Bus members render as N parallel wires, not a single rail

`drawNets()` (and the server-side `render-overlay` PNG) draws each
bus member's polyline independently. When the exporter eventually
emits `(bus …)` for shared-trunk groups (see
`schematic-graph/SKILL.md § Known exporter gaps #6`), the overlay
should match: detect bus-member groups and draw one thick rail with
short stubs to each member's pin, instead of overlapping 16 right-
angle wires.

**Fix**: share the bus-grouping detection logic between the exporter
and the overlay. Both paths need the same answer for "which nets are
part of this bus, and where's the trunk?" — implement once, call
from both. Render the trunk as a thicker line and per-member stubs
as thin perpendiculars.

**Acceptance**: an address bus on the explorer overlay looks like
a single thick rail with branching stubs, identical to what KiCad
renders after the bus-export fix.
