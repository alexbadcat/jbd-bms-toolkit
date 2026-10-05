*[Українська версія](protocol-es-up.uk.md)*

# ES-UP (Eco-Worthy / "polyglot") protocol

Some newer JBD/LS-Battery firmware revisions (identified here on a UP16S019
rev.2 "polyglot" unit) answer the classic [DD protocol](protocol-jbd-dd.md) for
reads, but **silently ignore DD writes** to several service registers (full-charge
voltage `0x12`, current calibration `0xAE`/`0xAF` — they acknowledge the write
as accepted, then revert on the next fresh read). On the same RS485 bus, these
units *also* speak a second, Modbus-RTU-flavored protocol — referred to here by
its identifying marker bytes, "ES-UP" (also seen as "Eco-Worthy") — and this
second channel is how the vendor's own tool actually changes parameters. This
protocol is implemented in `esup.py` and `esup_factory_reset.py`.

A pack that only speaks classic DD (no "polyglot" firmware) does not answer on
this protocol at all, so there's no collision risk running both channels on the
same bus as long as each pack only responds to its own address.

## Frame format

Same physical layer as DD: 9600 8N1, same RS485 bus, different address on the
wire.

**Read (function code `0x78`):**
```
request:  [addr][0x78][startHi][startLo][endHi][endLo][00][00][crcLo][crcHi]
response: [addr][0x78][start:2][end:2][dlen:2][data...][crc:2]
```

**Write (function code `0x79`):**
```
[addr][0x79][start:2][end:2][dlen:2][11 4A 42 44][payload][crc:2]
```
`dlen = 4 (marker) + len(payload)`. The 4-byte marker `11 4A 42 44` ("JBD") is
**mandatory** — frames without it are rejected.

All multi-byte fields are MSB-first (big-endian), **except the CRC**, which is
packed LSB-first.

### CRC16

Standard Modbus-style CRC16: init `0xFFFF`, polynomial `0xA001`, result packed
little-endian (`struct.pack("<H", crc)`).

```python
def crc16(d: bytes) -> bytes:
    crc = 0xFFFF
    for b in d:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)
```

### No unlock needed for user-level writes

Unlike the DD protocol's factory-mode password dance, **plain ES-UP writes
(`0x79`) to the user-parameter blocks need no password or service-mode entry at
all** — you just write, and it takes effect immediately. This is the channel
the vendor's own JBDTools software uses. The one block that *is* locked by
default is the calibration block `0x3800` — see "DV/DP debug mode" below.

## Register blocks

### `0x1000` — Pack status (read-only telemetry, 162 bytes)

| Offset | Field | Scale |
|---|---|---|
| +0 | Pack voltage | /1000 → V |
| +4 | Current | `(raw - 300000) / 100` → A |
| +8 | SOC | /100 → % |
| +10 | Remaining capacity | /100 → Ah |
| +12 | Full capacity | /100 → Ah |
| +14 | Rated capacity | /100 → Ah |
| +70..+104 | 16 cell voltages | /1000 → V, one `u16` per cell |

### `0x1C00` — User parameters (writable via plain `0x79`, 136 bytes)

| Offset | Absolute addr | Field |
|---|---|---|
| +4 | `0x1C04` | Balance start voltage (`BalanceV`) |
| +6 | `0x1C06` | Balance window (`BalanceDiff`) |
| +12 | `0x1C0C` | Full-charge-adjust voltage (`FullAdjustV`) — e.g. `5760` = 57.60V pack-level 100% threshold |
| +14 | `0x1C0E` | Full-charge-adjust current (`FullAdjustC`) — e.g. `1500` = 15.00A |
| +16 | `0x1C10` | BMS code string (device identifier) |
| +120 | `0x1C78` | Sleep voltage (`SleepV`) |
| +122 | `0x1C7A` | Sleep delay (`SleepDelay`) |
| +124 | `0x1C7C` | **Balance mode**: `0` = charge-only, `1` = static — see [balancing.md](balancing.md) |
| +126 | `0x1C7E` | RS485 protocol type |
| +128 | `0x1C80` | CAN protocol type |

### `0x2000` — Capacity (64 bytes)

| Offset | Absolute addr | Field |
|---|---|---|
| +0 | `0x2000` | Nominal capacity (÷100 → Ah) |
| +2 | `0x2002` | Full capacity (÷100 → Ah) |
| +4 | `0x2004` | Remaining capacity (÷100 → Ah) — **write-target for SOC correction**, see below |
| +6 | `0x2006` | SOC (÷100 → %) |
| +10 | `0x200A` | Cycle count — **write accepted ONLY here** (mirrors at `0x2000+10`, `0x3900+16`, and classic-DD `0x17` are read-only/ignored for writes) |
| +26..+40 | `0x201A`-`0x2028` | OCV table — 4 points of `(voltage/1000, SOC/100)` — on the observed unit, mostly unfilled/zero |
| +52 | `0x2034` | `RSNS` (sense-resistor-related constant), observed `210` |

### `0x3800` — Calibration gains (32 bytes, **write-locked by default**)

Base `10000` = gain ×1.0000.

| Offset | Field | Factory value observed |
|---|---|---|
| +0 | Charge-current gain | `10460` |
| +4 | Discharge-current gain | `10452` |
| +8..+38 | 16 per-cell voltage-measurement gains (one `u16` each, cell 1..16) | `~10006`-`10028` |

### `0x3900` — Factory thresholds mirror (32 bytes, read-only reference)

Observed: `BalanceV 3300`, balance delta, sleep settings, `FullAdjustV 5680`,
`FullAdjustC 1500`, a second full-adjust voltage `5760` at offset +58.

## DV/DP debug mode (needed to write `0x3800`)

Writing the calibration block `0x3800` in the normal operating mode gets
acknowledged (`ack=OK`) but **has no effect** — reads afterward show the
unchanged value. The block only actually accepts writes while the BMS is
switched into "Design Verification / Design Parameter" (DV/DP) debug mode:

```
write 0x2900 = 0x5A02   # enter DV/DP (debug) mode
...write to 0x3800 block...
write 0x2900 = 0x0000   # back to normal mode
```

`0x5A01` ("Tooling" mode) does **not** unlock this block — only `0x5A02`
(DV/DP) does. Persistence was confirmed across a fresh read session
(survives closing and reopening the serial connection); surviving an actual
power-cycle of the BMS itself was not independently verified.

### 🔑 Key finding: write addressing inside `0x3800` is by u16 *index*, not byte offset

In DV/DP mode, a write frame targeting address `0x3800 + k` lands at **data
offset `2k`** inside the block — i.e. the write address is an index into the
array of `u16` values, not a byte offset like every other block in this
protocol. Concretely:

| Write address | Lands at data offset | i.e. |
|---|---|---|
| `0x3804` (k=4) | +8 | cell #1's gain coefficient |
| `0x3808` (k=8) | +16 | cell #5's gain coefficient |

Reads (`0x78`) on this same block are ordinary byte-addressed reads — only
**writes** in DV/DP mode use this doubled indexing.

This caused a real incident: a write intended to tune the "discharge-current
gain" (byte offset +4, which under byte-addressing would be address `0x3804`)
was actually sent to the correctly-computed index address, but an *earlier*
manual attempt using naive byte-offset addressing landed on offset +8 instead
— silently corrupting cell #1's voltage-measurement gain (`10006 → 10452`,
causing the BMS to report cell #1 roughly +4.5% high, which tripped protection
logic). This initially looked like a vendor-side lock (confirmed as "stuck" even
via the vendor's own JBDTools, which refused to recalibrate it over RS485
either), until the indexing behavior above was identified and the correct
coefficient was written back through the correctly-indexed address. See
[lessons-learned.md](lessons-learned.md) and
[balancing.md](balancing.md#cell-1-case-study) for the full story.

The helper that computes the correct write address for a given data offset:

```python
def gain_write_addr(data_off: int) -> int:
    return 0x3800 + data_off // 2
```

## Command registers (actions, not stored data — under `0x29xx`)

| Address | Write value | Action |
|---|---|---|
| `0x2900` | `0x5A02` / `0x0000` | Enter / exit DV/DP debug mode |
| `0x2902` | — | MOS control |
| `0x2908` | — | Sleep |
| `0x290C` | `0x55AA` | Restore parameter defaults |
| `0x290E` | `0x5A5A` | Factory reset |
| `0x2912` | `0x44AA` | Restart |

⚠️ Factory reset (`0x290E`) does **not** fix a corrupted calibration
coefficient by itself, and may reset other user-writable fields to factory
defaults alongside it. `esup_factory_reset.py` wraps this safely:

1. Backs up blocks `0x1000`, `0x1C00`, `0x2000`, `0x3800`, `0x3900` to files.
2. Performs the reset (only with `--yes`; otherwise it's a dry run that just
   prints the plan).
3. Diffs every block against the pre-reset backup.
4. Automatically restores known user-writable fields that changed
   (`FullAdjustV`, `FullAdjustC`, `BalanceV`, `BalanceDiff`, nominal/full
   capacity) back to their pre-reset values via plain ES-UP writes.
5. Reports what changed and what was restored.

## Library usage (`esup.py`)

`esup.py` is a library module (no CLI). Key functions:

```python
import esup

# Current-calibration gains (base 10000 = x1.0000)
esup.read_gains(port, addr=1)                      # {"charge": ..., "discharge": ...}
esup.set_gain(port, esup.GAIN_CHARGE, 10303, addr=1)   # DV/DP wrapped + persistence-verified

# Per-cell voltage-measurement gain, cell_no 1..16
esup.set_cell_gain(port, cell_no=1, value=10006, addr=1)

# Capacity / SOC
esup.read_capacity(port, addr=1)                    # nominal/full/remaining Ah + soc %
esup.set_remaining_ah(port, ah=143.9, addr=1)        # SOC correction, see soc-calibration.md

# Low-level
esup.read_block(ser, start, end, addr=1)             # raw bytes
esup.write_reg(ser, start, end, payload, addr=1)     # raw ack frame or None
```

`esup_factory_reset.py` CLI:

```bash
# Dry run: reads + backs up only, prints the plan
python esup_factory_reset.py

# Actually perform the factory reset + auto-restore
python esup_factory_reset.py --yes
```

## What does NOT work on this firmware

- **DD-protocol service writes** to register `0x12` (full-charge voltage) ack
  as accepted but do **not persist** — a fresh read-back after a new session
  shows the old value unchanged.
- **DD calibration registers `0xAD`-`0xAF`** ack as accepted with **zero
  measurable effect** on the reported current.
- The vendor's own JBDTools software, over the same RS485 link, also **fails**
  to recalibrate a corrupted cell gain directly (reported a Chinese-language
  "calibration failed" error) — this is not a limitation specific to this
  toolkit.

## Reference

Frame layout and block offsets were cross-checked against the community
write-up at [gist.github.com/PhracturedBlue —
ES-UP / Eco-Worthy protocol notes](https://gist.github.com/PhracturedBlue/7ef619594eaa4c27f4ff068b461865b8),
then verified live against real hardware (register-by-register, by reading
back and by observed effect).
