# Sega Turbo Transcription Double-Check List

Last updated: 2026-05-21

## Sheet 1

- Verify `IC62` / `UPB425` pin 23 against a primary source. The schematic appears to associate this area with `H8`, but the current librarian entry marks pin 23 as `NC`; do not reconnect it until the pinout is confirmed.
- Revisit removed power endpoints on `IC119`, `IC69`, `IC108a`, and `IC128e`. They were removed because the current KiCad symbol/pin placement created pin conflicts.
- Recheck `TV_CLR` and `CMP_SYNC`. These labels were not kept because the current split-component refs/pins did not align cleanly with active unit pins.
- Review remaining zero-net or low-coverage discretes from lint: `R2`, `R7`, `RP42`, `C_BYP1`, `C_BYP2`, `LED1`, `R29`, `RA`, `C15`, `R32`, `R31`, `R30`, `C17`, `R5`, `C4`, `C16`, `R3`, and `IC128e`.
- Add tight `body_bbox` rectangles for visual polish. Sheet 1 is electrically passing, but body-bbox coverage is still 0%.
- Visually compare the right-angle label/bus paths against the original scan, especially dense CPU address/control routing.

## Sheet 2

- Sheet 2 is currently the clean reference sheet: pins, paths, `body_bbox`, lint, and ERC are passing.
- Keep an eye out for regressions if shared library entries or exporter behavior changes.

## Sheet 3

- Add tight `body_bbox` rectangles for visual polish. Sheet 3 is electrically passing, but body-bbox coverage is still 0%.
- Review split IC handling if any Stage 5/6 export behavior changes. Split units were trimmed to active pins plus power/ground.
- Recheck any label-only nets that depend on later sheets once their destination sheets are transcribed.

## Sheet 5

- Resolve the two LS14 triangle labels near `CN4` before adding them. The scan reads like `IC12` and `IC8`, but those designators already exist elsewhere in the graph with different parts, so they were not added in the sheet-5 bbox pass.
- Decide how to represent the unlabeled `2SA473 x15` transistor group. The schematic does not show individual `Q` refdes labels in the visible scan.
- Decide how to represent the repeated `CR` filter/debounce blocks/arrays around `CN4`, `IC127`, and `CN5`; they are clear drawn components but do not yet have a library entry.
- `CN5` intentionally has an elongated bbox because it is a single drawn vertical connector. Lint warns on aspect ratio, but visual overlay shows it covers the connector.
- Sheet 5 currently has bboxes only; pins and nets are not placed yet, so zero-net lint warnings are expected.

## Sheet 7

- Initial bboxes are placed for `IC1`, `IC2`, `IC3`, `IC4`, `IC13`, `IC14`, `IC15`, `IC16`, `IC27`, `IC28`, `IC29`, `IC30`, `IC41`, `IC42`, `IC43`, `IC44`, `IC69d`, `IC69e`, and `RA_S7`.
- `IC1` and `IC2` are printed as `2732 or 2716`; the graph currently uses `2716` so the sheet can move through the existing librarian coverage.
- `IC69d` and `IC69e` are split `74LS04` inverter units. Confirm package/unit naming before pin placement, because other `IC69` units already exist on earlier sheets.
- `RA_S7` is printed as a `4.7K` resistor array without a clear refdes. Rename it if a board inventory or silkscreen refdes turns up.

## Sheet 8

- Initial bboxes are placed for known-library P-ROM components: `PROM_CN1`, `PROM_CN2`, `PROM_IC1`, `PROM_IC16`, `PROM_IC28`, `PROM_IC7`, `PROM_IC8`, `PROM_R1`, and `PROM_LED1`.
- P-ROM board components are prefixed with `PROM_` because the P-ROM board reuses refdes numbers that already exist on the CPU board sheets.
- Library coverage has been added and bboxes are placed for `uPC624D`, `uPC159`, `SN75365`, `MC14016B`, `TL084`, and `74LS626` devices.
- Several analog/clock bboxes on `PROM_IC43*`, `PROM_IC44*`, and `PROM_IC45*` are intentionally loose around split drawn units. Hand-tighten before pin placement.
- Passives around the analog/clock section are only partially represented. Add the remaining resistors, capacitors, diodes, and regulator after deciding how much passive detail should be captured on the P-ROM board.

## Sheet 9

- Initial bboxes are placed for known-library P-ROM components: `PROM_IC10`, `PROM_IC29`, `PROM_IC30a`, `PROM_IC30b`, `PROM_IC37`, `PROM_SW1`, `PROM_RA1`, and `PROM_CN3`.
- Library coverage has been added and bboxes are placed for `TBP18S030`, `UPB426D` / `PB426D`, `74LS150`, and `74LS377` devices.
- `IC19` is printed as `376` and is not yet added. Verify whether this is a 74LS376-family part and add its librarian entry before placing the bbox.
- `PROM_IC30a` and `PROM_IC30b` are split `74LS74` units for the two visible `IC30` flip-flop sections. Confirm the split-unit naming before pins.
- `PROM_IC37` is a loose bbox around the split `74LS109` drawing. It will need hand adjustment before pin placement.

## Sheet 10

- Initial bboxes are placed for known-library P-ROM components: `PROM_IC71`, `PROM_IC83`, `PROM_IC95`, `PROM_IC101`, `PROM_IC54`, `PROM_IC84`, `PROM_IC102`, `PROM_IC59a`, `PROM_IC59b`, `PROM_IC59c`, `PROM_IC59d`, and `PROM_R2`.
- `PROM_IC84` and `PROM_IC102` are printed as `2764 or 2364`; the graph currently uses `2764` so the sheet can move through the existing librarian coverage.
- Library coverage has been added and bboxes are placed for `74LS195` and `74LS11` devices.
- `PROM_IC59a` through `PROM_IC59d` are split `74LS00` NAND units. Confirm split-package naming before pins.

## Cross-Sheet / Library

- Confirm split-package conventions across the board before final probe generation, especially when a package is represented as multiple schematic units.
- Re-run librarian coverage after each new sheet and verify every newly encountered part against the inventory or a primary datasheet.
- After sheets 4+ are added, revisit single-ended label nets from sheets 1-3 and merge/resolve them where their counterparts appear.
- Regenerate and review `probes.csv` only after the active sheet has passed validate, lint, untyped-net, ERC, and render checks.
