*[Українська версія](protocol-jbd-dd.uk.md)*

# JBD classic "DD" protocol

Covers the classic JBD/Jiabaida RS485 protocol as implemented by `jbd_bms.py` and
`jbd_settings.py`. This is the protocol spoken by first-generation JBD UP-series
BMS units (e.g. UP16S010). Newer "polyglot" firmware (UP16S019 rev.2 and similar)
still answers DD reads, but several service writes are silently ignored on it —
see [protocol-es-up.md](protocol-es-up.md) for the protocol that actually writes
on that firmware.

## Frame format

```
DD [addr] [action] [cmd] [len] [data...] [crc_hi] [crc_lo] 77
```

- `0xDD` / `0x77` — fixed start/end bytes.
- `addr` — bus address of the pack (`0x00` = master/DIP-0000, non-zero for a
  second pack sharing the bus via a Y-split). **This is a UP-series extension.**
  The classic 7-byte frame some JBD docs describe
  (`DD A5 03 00 FF FD 77`, no address byte) is **silently dropped** by UP16S-series
  units — always send the address-extended form.
- `action` — `0xA5` = READ, `0x5A` = WRITE.
- `cmd` — command/register number (see tables below).
- `len` — length of `data` in bytes (0 for most reads).
- `crc_hi`/`crc_lo` — checksum, see below.

Response frame: `DD [addr] [cmd] [status] [len] [data...] [crc_hi] [crc_lo] 77`
(`status == 0` means OK; non-zero means the BMS rejected/errored the command).

### Checksum

```
checksum = (0x10000 - sum(bytes from addr through the end of data)) & 0xFFFF
```

i.e. the sum covers `addr, action/status, cmd, len, data...` — **including the
address byte**, packed big-endian as two bytes before the trailing `0x77`.
This is the formula that must match on both request and response; a frame with
an intact `DD...77` envelope but a bad checksum is a corrupted frame, not valid
data (see [lessons-learned.md](lessons-learned.md) for why this matters on a
shared bus with two packs).

### Request retries

The BMS sleeps after ~5 minutes without bus traffic (per datasheet §4.8); the
first frame after waking is often lost. `jbd_bms.py` retries automatically and
classifies failures (silence / wrong echoed address / truncated frame / bad CRC
/ non-zero status) rather than treating any non-response as fatal immediately.

## Two address spaces in the same frame format

- **Info commands** (`cmd` 0x03/0x04/0x05) — readable at any time, no service
  mode needed. Return a structured data block (see "Basic info block" below).
- **Service/config registers** (`cmd` 0x10-0x31, 0xAD-0xB0+, 0xE0) — a subset is
  readable without entering service mode; the rest (and all writes) require
  entering service mode first.

## Service mode (factory mode)

| Register | Direction | Value | Meaning |
|---|---|---|---|
| `0x00` | WRITE | `0x5678` | Enter service/factory mode |
| `0x01` | WRITE | `0x0000` | Exit **without** saving |
| `0x01` | WRITE | `0x2828` | Exit **with** saving to EEPROM |

Write flow used by this toolkit: enter (`0x00=0x5678`) → write target register →
exit-with-save (`0x01=0x2828`), always in a `finally` block so an exception
mid-write still leaves the BMS out of service mode. Read-only sessions always
exit **without** saving (`0x0000`), so a pure dump can never modify anything —
even if something unexpected happens mid-read.

## Basic info block (`cmd 0x03`)

| Offset | Len | Field | Scale |
|---|---|---|---|
| 0 | 2 | Pack voltage | ×0.01 V |
| 2 | 2 | Current (signed) | ×0.01 A, + = charge, − = discharge |
| 4 | 2 | Remaining capacity | ×0.01 Ah |
| 6 | 2 | Nominal (full) capacity | ×0.01 Ah |
| 8 | 2 | Cycle count | — |
| 10 | 2 | Production date (raw) | — |
| 12-13 | 2 | Balance status, cells 1-16 (bit per cell) | — |
| 14-15 | 2 | Balance status, cells 17-32 (bit per cell) | — |
| 16 | 2 | Protection status bitmask | see below |
| 18 | 1 | Firmware version | high nibble `.` low nibble |
| 19 | 1 | SOC | % |
| 20 | 1 | MOSFET bitmask | bit0 = charging, bit1 = discharging |
| 21 | 1 | Cell count | — |
| 22 | 2 | Alarm status bitmask | — |
| 24 | 2 | Ambient temperature | 0.1 K (`(v-2731)/10` = °C) |
| 26 | 2 | FET temperature | 0.1 K |
| 28 | 1 | NTC sensor count | then that many ×2-byte temps follow |

⚠️ A pitfall hit in production: bytes 12-13 are cells **1-16**, bytes 14-15 are
cells **17-32** — reading them as one 32-bit big-endian integer instead of two
separate 16-bit halves puts bits 0-15 on the wrong cell range (16S packs showed
"no balancing" for a week because of this).

### Protection bitmask (register offset 16, bit → meaning)

```
0  cell overvoltage        7  discharge undertemperature
1  cell undervoltage       8  charge overcurrent
2  pack overvoltage        9  discharge overcurrent
3  pack undervoltage       10 short circuit
4  charge overtemperature  11 ADC error
5  charge undertemperature 12 MOSFET software lock
6  discharge overtemperature
```

## Service registers (`jbd_settings.py` `REGISTERS` table)

| Reg | Name | Unit |
|---|---|---|
| `0x10` | Design (nominal) capacity | Ah |
| `0x11` | Cycle capacity | Ah |
| `0x12` | 🔑 Cell full-charge voltage (SOC=100% trigger, see below) | mV |
| `0x13` | Cell empty voltage | mV |
| `0x14` | Self-discharge rate | raw |
| `0x15` | Production date | raw |
| `0x16` | Serial number | raw |
| `0x17` | Cycle counter | raw |
| `0x18`/`0x19` | Charge overtemperature / release | °C (`(v-2731)/10`) |
| `0x1A`/`0x1B` | Charge undertemperature / release | °C — see note below |
| `0x1C`/`0x1D` | Discharge overtemperature / release | °C |
| `0x1E`/`0x1F` | Discharge undertemperature / release | °C |
| `0x20`/`0x21` | Pack overvoltage / release | V (×0.01, "×100" scale) |
| `0x22`/`0x23` | Pack undervoltage / release | V |
| `0x24`/`0x25` | Cell overvoltage / release | mV |
| `0x26`/`0x27` | Cell undervoltage / release | mV |
| `0x28`/`0x29` | Charge / discharge overcurrent | A (×0.01) |
| `0x2A` | 🔑 Balance start voltage (cell) | mV |
| `0x2B` | 🔑 Balance window (delta threshold) | mV |
| `0x2C` | Pack overvoltage delay | ms |
| `0x2D` | 🔑 `FuncConfig` bitmask — see below | raw bits |
| `0x2E`/`0x2F` | Cell overvoltage / undervoltage delay | ms |
| `0x30`/`0x31` | Charge / discharge overcurrent delay | ms |

⚠️ Register `0x1A`/`0x1B` ("charge under-temperature") use **raw 0.1 K units**,
not °C directly: `2731 = 0°C`, `2781 = 5°C`. LFP cells should not charge below
0°C (lithium plating risk) — factory defaults of roughly `−5/0°C` are often
raised to `0/+5°C` for safety margin.

⚠️ **Registers from `0x32` onward are not configuration** — they mirror
instantaneous per-cell voltages (register `0x32` was observed returning the live
voltage of cell 3 at the moment of the read). `jbd_settings.py` deliberately
stops reading the table at `0x31` to avoid presenting live telemetry as a
"setting".

### `0x2D` FuncConfig bits

| Bit | Meaning |
|---|---|
| 2 | `balance_en` — balancing enabled at all |
| 3 | `chg_balance_en` — **selects balance trigger mode**: 1 = charge-only, 0 = static-only (see [balancing.md](balancing.md) — these are mutually exclusive modes, not an extra permission) |
| 4/5 | LED indicator bits |
| 12 | Capacity-counter unit multiplier (affects how `0xE0` below is interpreted) |

Observed factory/working values: `471` (`0x01D7`, bit3=0, static-only) and `479`
(bit3=1, charge-only).

## Current calibration registers (`0xAD`-`0xAF`)

| Reg | Meaning |
|---|---|
| `0xAD` | Zero-current calibration — write `0` **only while actual current is ~0A** |
| `0xAE` | Charge-current calibration, 10 mA units — write the **real measured current** while charging |
| `0xAF` | Discharge-current calibration, 10 mA units — write the **real measured current** while discharging |

These are **trigger registers, not configuration**: writing a value tells the
BMS "the real current right now equals this", and it recomputes its internal
gain. There is no defined read-back for these (reading returns noise/unrelated
data) — **verification must be by effect**: measure the current again after the
write and confirm it converges toward the target (tolerance ~0.15A or 3%,
whichever is larger). On classic-DD-only hardware these work as documented; on
the newer "polyglot" firmware they ack as accepted but have **zero measurable
effect** (see [protocol-es-up.md](protocol-es-up.md) and
[soc-calibration.md](soc-calibration.md)).

## Remaining-capacity register (`0xE0`)

`u16`, units of 10 mAh (valid when `0x2D` bit12 = 0). Writing it is the DD-level
equivalent of the "Editing capacity" action in vendor tools — it directly
overwrites the Ah counter used to compute SOC. Like the calibration registers,
there's no defined read-back, so verify by effect: re-read the basic info block
(`cmd 0x03`) and check `remaining_ah`. The BMS **rounds the result to the
nearest whole percent** of nominal capacity (observed: target 88.41 Ah against
148.5 Ah nominal landed at 89.09 Ah = exactly 60%).

## The SOC=100% reset rule

Per vendor documentation (JBDTools manual): the SOC counter resets to 100% when
the **average** cell voltage exceeds register `0x12` ("cell full-charge
voltage") continuously for 5 seconds. This is an *average*, not a per-cell
check — if an inverter's constant-voltage hold point sits just under the
threshold (e.g. CV 57.6V / 16 cells = 3.597V average vs `0x12 = 3.600V`), the
reset condition is **never met**, and the counter silently drifts via
self-discharge accounting (`0x14`) instead of periodically re-anchoring to
100%. See [soc-calibration.md](soc-calibration.md) for the full diagnosis and
fix (lowering `0x12` slightly, e.g. to 3575 mV).

## CLI usage (`jbd_bms.py`, `jbd_settings.py`)

```bash
# Read everything (pack + cells), human-readable
python jbd_bms.py --port /dev/ttyUSB0 --addr 0

# Cells only, machine-readable
python jbd_bms.py --addr 0 --cells --json

# Dump all known service registers (reads without service mode first,
# falls back to service mode read-only if too few registers answered)
python jbd_settings.py --addr 0

# Dump + save a JSON backup (ALWAYS do this before writing anything)
python jbd_settings.py --addr 0 --save backup-before-write.json

# Read arbitrary/undocumented registers (read-only, decodes dec/hex/bits)
python jbd_settings.py --addr 0 --read 0x12 0x2D 0x3D

# Write a register from the allowlist (requires --yes; verified by a fresh
# re-read after save)
python jbd_settings.py --addr 0 --set 0x2A=3400 --yes

# Write the remaining-capacity counter (SOC correction), value in 10 mAh units
python jbd_settings.py --addr 0 --set 0xE0=8841 --yes

# Calibrate charge-current gain (10 mA units) — MUST be done under real,
# stable charge current of that direction; verified by measuring current after
python jbd_settings.py --addr 0 --set 0xAE=590 --yes
```

`--set` only accepts registers in the tool's explicit allowlist (balance
thresholds, full/empty voltage, FuncConfig, release thresholds, charge
under-temperature) plus the special-cased `0xE0` (remaining capacity) and
`0xAD`-`0xAF` (current calibration) — anything else is refused on purpose.

## Reference

Register meanings and scaling were cross-checked against the community-maintained
JBD register map: [FurTrader/OverkillSolarBMS —
`Comm_Protocol_Documentation/JBD_REGISTER_MAP.md`](https://github.com/FurTrader/OverkillSolarBMS/blob/master/Comm_Protocol_Documentation/JBD_REGISTER_MAP.md).
