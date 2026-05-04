# Discrepancies — paper schematic vs. physical board

Per-board log of cases where the published schematic disagrees with what the
real PCB does. Each entry is anchored by a stable id so probe-list rows and
graph evidence can cite it.

When a `probes.csv` row resolves to `contradicted`, copy its details here,
record the probe result, and decide which side wins (almost always the board).
The ERC linter reads this file and suppresses warnings for nets that have a
matching `board_wins` entry.

---

## Entry template

```
### <id> — <one-line summary>

- **Date:**       YYYY-MM-DD
- **Prober:**     <name>
- **Instrument:** <DMM model / scope / logic analyzer>
- **Sheet:**      <sheet#> zone <zone> — <e.g. "drawing 77-0019 sheet 4, zone B3">
- **Net:**        <net name>
- **Endpoints (paper):**  refdes.pin; refdes.pin
- **Endpoints (board):**  refdes.pin; refdes.pin
- **Resolution:** board_wins | paper_wins | unresolved
- **Notes:**      free text — photos, oscilloscope captures, suspected ECN, etc.
```

---

<!-- Append entries below. Newest first. -->
