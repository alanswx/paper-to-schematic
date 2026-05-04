# Librarian skill

Maintains the deterministic chip pinout database at
`.agents/skills/librarian/chips.json`. The Librarian is the **source of truth**
for what every part in our boards looks like (pin numbers, names, types,
package). All schematic transcription depends on accurate Librarian entries —
a wrong pinout here propagates as a wrong netlist downstream.

## When to use

- A board references a part not yet in `chips.json`.
- The user asks to verify a pinout against a datasheet.
- Validating that all parts referenced by a board's `graph.json` exist in the
  library.

## CLI

`librarian.py` is the only sanctioned way to read or write `chips.json`. **Do
not hand-edit JSON.** Validation is part of every write path.

```bash
# List all known parts (table)
python3 .agents/skills/librarian/librarian.py list

# Show a single part's pinout
python3 .agents/skills/librarian/librarian.py show 74LS245

# Validate the entire library: schema, pin counts, VCC/GND typing
python3 .agents/skills/librarian/librarian.py validate

# Check whether every part referenced by a board's graph.json is in the library
python3 .agents/skills/librarian/librarian.py coverage boards/exidy_440/graph.json

# Add a part. Pipe a JSON entry on stdin or pass --from-file.
python3 .agents/skills/librarian/librarian.py add 6809E --from-file /tmp/6809e.json
```

## How to add a new part

1. **Confirm absence.** Run `librarian.py show <part>`. The CLI also matches
   `aliases`, so a hit on `SN74LS245N` blocks adding `74LS245` again.
2. **Find a primary source.** Vendor datasheet (Motorola, TI, Hitachi, NEC,
   Mitsubishi, etc.). Cite vendor + document number + year in the `datasheet`
   field. **Do not cite Wikipedia, blog posts, or schematic redraws** — only
   the original vendor datasheet.
3. **Construct the entry** conforming to `chips.schema.json`:
   - `package` is `DIP-<n>` and the pin list has exactly `n` entries.
   - Pin numbers run contiguously 1..n.
   - Each pin: `n` (integer), `name` (functional, leading `~` for active-low),
     `type` (`input`/`output`/`bidir`/`power`/`ground`/`clock`/`tri_state`/
     `passive`/`open_collector`/`nc`).
   - `vcc_pin` points to a pin typed `power`; `gnd_pin` to a pin typed `ground`.
   - `aliases` lists equivalent part numbers seen in the wild.
4. **Run `librarian.py add`**. The CLI validates before writing. If validation
   fails, fix the entry and retry — never bypass.
5. **Re-run `librarian.py validate`** as a sanity check.

## Constraints

- **Never invent pinouts.** If you cannot locate a primary source for a pin,
  mark it `type: nc` rather than guessing. Schematic transcription will fail
  loudly on `nc` rather than silently on a wrong assignment.
- The library is finite. Arcade-era logic is mostly 74xx-series (TI/Motorola/
  Fairchild), 4000-series CMOS, common 8-bit CPUs (Z80, 6502, 6809, 6809E,
  68000), SRAMs (HM6116, HM6264), DRAMs, and per-manufacturer customs (Atari,
  Namco, Sega, Konami, Capcom, Exidy). No RAG — every entry is hand-curated
  and cited.
- Active-low pins use leading `~` in `name` (e.g., `~OE`, `~CSC`, `~RESET`).
- Custom chips (e.g., Exidy `EX-44`) are valid Librarian entries when their
  pinout is documented; if undocumented, leave them out and they'll go on the
  probe list as "unknown chip."

## Schema

`chips.schema.json` is the canonical schema. Re-validate after every change.
