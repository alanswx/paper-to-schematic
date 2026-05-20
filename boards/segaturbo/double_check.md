# Sega Turbo Transcription Double-Check List

Last updated: 2026-05-19

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

## Cross-Sheet / Library

- Confirm split-package conventions across the board before final probe generation, especially when a package is represented as multiple schematic units.
- Re-run librarian coverage after each new sheet and verify every newly encountered part against the inventory or a primary datasheet.
- After sheets 4+ are added, revisit single-ended label nets from sheets 1-3 and merge/resolve them where their counterparts appear.
- Regenerate and review `probes.csv` only after the active sheet has passed validate, lint, untyped-net, ERC, and render checks.
