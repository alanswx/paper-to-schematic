# Sheet 1 (Video RAM & MPX) — Hand-Check List

Net total: **124**. All `validate` and `untyped-nets` gates pass.

This document lists items where the AI transcription is uncertain or incomplete and needs human verification against the original schematic.

## Uncertain net labels

| Refdes.pin | Net assigned | Confidence | Issue |
|---|---|---|---|
| 15H.15 | (omitted) | ✗ | Reads "K3" or "X3" — outside the I0..I5 pattern of the rest of the H column |
| 18F.15 | (omitted) | ✗ | Reads "MUX1" or "M001" |
| 16F.3 | (omitted) | ✗ | Label illegible ("EY"/"L4"/"L7"?) |
| 16F.5 (output) | (omitted) | ✗ | Reads "BR" or "B12" — breaks the B0..B11 numbering |
| 16F.14 / 16F.15 | (omitted) | ✗ | No legible labels in current crop |
| 23E.11 | `SCANBUS_C` (clock) | ⚠ | Spelling uncertain — possibly `SCANBUSC`, `SCAN_BUS_C`, or split differently |
| 13Fa.1 | `INTIO` | ⚠ | Reads "$INT1/O" or "/INT1/0" — likely a write strobe |
| 13Fb.15 | `SCAN_IS` | ⚠ | Reads "SCAN.IS" or "SCANIS" |
| 6H.5 | `/REA` | ⚠ | Off-page label "$REA" or "/REA" — spelling unclear |

## Pin/symbol-level patterns assumed (verify against drawing)

| Item | Assumption made | Verify |
|---|---|---|
| 21F-19F mux pin-2,3 jumper | Pins 1, 2, 3 jumpered together → tied to address line at pin 1 | Possibly only pin 1↔2 jumpered, with pin 3 stub unused |
| 17F.15, 15F.15 | Tied to +5V (D4 pulled high) | Confirm +5V vs GND symbol nearby |
| 17F.2, 15F.2 | Tied to +5V (D2 pulled high) | Same |
| RAM /CS (pin 20) | All 24 RAMs tied to GND (always selected, OE gates output) | Check |
| RAM data bus per row | All 6 RAMs in row tap shared 8-bit bus to that row's '245 | Confirmed in row-B/C crops; assumed for D/E |
| '245 right side `D0..D7` | All 4 row '245s drive a common 8-bit data bus (bus contention prevented by `CSn`) | Verify the right-hand labels are single-digit `D7..D0` and not row-specific |

## Missing / not yet wired

| Block | What's missing |
|---|---|
| **14F (74LS138)** outputs | 6 outputs (Y2..Y7) read as `RS0..RS5` or `BS0..BS5` — likely the 6 column /OE signals fanning to RAM pin 18. Mapping (which Yn goes to which column) **not done**. Pin 1 (A) appears to take `B11` (15F output); pin 2 (B), pin 3 (C), and pin 6 (E3 enable) inputs need re-reading. |
| **RAM /OE pins (24 chips × pin 18)** | Each row strip shows 6 labels that look identical across rows (e.g. `RB5..RB0` style). Assumed to be 6 column-wide /OE signals shared by all 4 rows, driven by 14F outputs — **not emitted**. |
| **6H 74LS04 inverter** | Only 1 of 6 inverters wired (input pin 5 → `/REA`, output pin 6 → `/RLD`). The other 5 inverters drive other shift-register controls and likely `CLK13A`; not mapped. |
| **'166 CLR\\ (pin 15)** | Top control pin of each shift register; appears tied via shared wire (probably to system reset) — not emitted. |
| **'166 CLK_INH (pin 6)** | Likely tied to GND or driven by an inverter output — not emitted. |
| **'166 SER (pin 1)** | Vertical wire on left, possibly serial-chained — not analyzed. The convention says each '166 produces an independent SDx so SER may be tied off. |
| **Mux pin 14/1 high pulls** | Several muxes tie unused data inputs (pin 14 = D5, pin 1 = D3) to +5V. 21H/20H/19H/18H/17F/15F handled. Check whether 17H/16H/15H also need additional +5V endpoints. |

## Components placed but functionally unrouted

| Refdes | Part | Status |
|---|---|---|
| 14F | 74LS138 | Power only; outputs not wired to RAM /OE pins |
| 6H | 74LS04 | 1 of 6 inverters wired |

## Recommended re-crop targets

- **14F**: tighter crop with brightness/contrast adjustment to read the 8 output labels and the 3 select-input labels precisely.
- **RAM /OE strip per row**: a full-width read of each row's 6 OE labels to confirm they really are column-shared (`RBn` only) vs row-specific (`RBn`/`RCn`/`RDn`/`REn`).
- **16F mux**: re-read all 5 input pins and the output label.
- **6H inverter**: identify all 6 inverter sections and trace their input/output pairs.
- **'166 control wires**: per-chip pin 1, 6, 15 routes — likely small wire stubs in the lower-right of each '166.
