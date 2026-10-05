*[Українська версія](soc-calibration.uk.md)*

# SOC calibration across parallel packs

When two packs share a bus and sit at the same pack voltage, their reported
SOC can still drift apart over time — this document covers why, and how to
fix it on each protocol.

## Two independent root causes

### 1. The "`0x12` vs CV" trap

Per the vendor's SOC-reset rule (see
[protocol-jbd-dd.md](protocol-jbd-dd.md#the-soc100-reset-rule)): the SOC
counter only resets to 100% when the **average** cell voltage exceeds
register `0x12` ("cell full-charge voltage") continuously for 5 seconds.

If the inverter's constant-voltage (CV) hold point sits just under that
average threshold, the reset condition is **never satisfied** — the counter
then only moves via coulomb-counting and self-discharge accounting (register
`0x14`), slowly drifting downward with no periodic re-anchor to 100%.

Concrete example from the field: CV hold at 57.6V pack voltage over 16 cells
averages to 3.597V/cell, while `0x12` was set to `3.600V` — just 3 mV out of
reach. The pack never crossed the threshold during normal CV operation, so it
never reset, accumulating roughly 0.4-0.5 Ah/day of apparent "self-discharge"
at 0A. **Fix**: lower `0x12` slightly (e.g. `3600 → 3575` mV) so the real CV
plateau reliably crosses it and the reset actually fires each cycle.

### 2. Current-gain mismatch between packs

Each BMS has its own current-sensing calibration gain, and if two packs'
gains diverge, their coulomb counters drift apart even while physically
carrying the same current (since it's a shared bus/parallel connection).

**Diagnosis method**: compare the daily net-charge integral (charge-Ah
integral minus discharge-Ah integral, over one closed day/cycle) between
packs. A healthy pack should net close to zero (small negative, from real
coulombic losses). One pack measured netting **-0.7 Ah/day** (honest) while
the other netted **+2.8 Ah/day from nowhere** — decomposed as roughly **+1.5%
over-counting on charge** and **-1.5% under-counting on discharge**
(confirmed asymmetric, not a zero-offset bug, since both read ~0A correctly
at true idle).

This points to a skewed gain pair in that pack's current-measurement
calibration — ES-UP block `0x3800`, offsets `+0` (charge gain) / `+4`
(discharge gain), base `10000` = ×1.0000, factory values observed around
`10460` / `10452`. Lowering the charge gain by ~1.5% (`10460 → 10303`) had a
directly proportional, measurable effect on the reported current (a test
×1.10 gain change produced a ×1.097 measured current change — confirming the
gain maps linearly onto the reading), and measurably reduced the daily
net-charge drift.

## How to actually fix it, per protocol

### Classic DD (older pack)

- **`0x12`** (cell full-charge voltage, mV) — lower slightly if the
  inverter's CV plateau doesn't reliably cross it (see trap above).
- **`0xE0`** (remaining capacity, `u16`, 10 mAh units) — can be force-written
  directly as a one-off SOC correction. Verify by effect via `remaining_ah`
  in the basic info block (`cmd 0x03`); the BMS rounds the result to the
  nearest whole percent of nominal capacity (field example: target 88.41 Ah
  against a 148.5 Ah nominal capacity landed at exactly 89.09 Ah = 60%).
- **`0xAD`/`0xAE`/`0xAF`** (current zero/charge/discharge calibration, 10 mA
  units, write the real measured current) — available and working on classic
  DD-only hardware; **turned out locked/no-op on the newer "polyglot"
  firmware** (ack accepted, zero measurable effect) — use the ES-UP path
  below for that firmware instead.

```bash
python jbd_settings.py --addr 0 --set 0x12=3575 --yes
python jbd_settings.py --addr 0 --set 0xE0=8841 --yes
```

### ES-UP (newer "polyglot" pack)

- **`0x2004`** (`esup.set_remaining_ah`) — writes the remaining-capacity
  counter directly; same idea as DD `0xE0` (SOC = Remaining/Full, recomputed
  by the BMS). Does **not** touch gain or protection thresholds, only the
  counter. Verify by reading back (tolerance ~0.1 Ah to account for drift
  under load).
- **`0x3800`** `+0`/`+4` (charge/discharge current gain) — the actual
  root-cause fix for a persistent daily drift. Requires the DV/DP unlock
  sequence described in [protocol-es-up.md](protocol-es-up.md#dvdp-debug-mode-needed-to-write-0x3800).
  **Iterate conservatively** (small steps like the −1.5% example above —
  undercorrecting is safer than overcorrecting) and re-measure the daily
  net-charge integral after each step; never guess blind on a battery in
  active use.
- **`0x3800` `+8..+38`** (per-cell voltage gain) — can independently correct
  a single cell's reported voltage if it alone reads systematically high or
  low due to a corrupted gain coefficient, as opposed to a real imbalance —
  see [balancing.md](balancing.md#cell-1-case-study) for a worked example and
  [protocol-es-up.md](protocol-es-up.md#-key-finding-write-addressing-inside-0x3800-is-by-u16-index-not-byte-offset)
  for the write-indexing gotcha that must be understood first.

```python
import esup
esup.set_remaining_ah(port, ah=143.9, addr=1)
esup.set_gain(port, esup.GAIN_CHARGE, 10303, addr=1)
```

## Worked example

Two packs sitting at the same 53.7V pack voltage with cells in the 3.33-3.39V
range (true SOC roughly equal, ~96% for both) reported: old pack 96%
(142.5/148.5 Ah), new pack 100% (149.7/150 Ah). Root causes, diagnosed
separately:

- The new pack had **falsely reset to 100%** during a balancing session, when
  one cell (not the pack average — but see the actual trigger rule, which
  genuinely is average-based; a transient average crossing can happen briefly
  during active balancing even if steady-state wouldn't) touched its
  full-charge threshold momentarily while other cells hadn't caught up.
- The old pack's CV hold sat at 3.597-3.601V average — hovering right at
  (sometimes just under) its `0x12` threshold of 3.600V, so it was
  inconsistently resetting and accumulating a small self-discharge deficit
  between resets.

Aligned operationally by writing the new pack's remaining-Ah counter down to
match reality (`143.9 Ah = 95.9%`) via ES-UP `0x2004` — a one-off correction,
not a fix for the underlying gain skew, which is addressed separately and
more slowly via the gain-calibration path above.

## Operational note: which pack's SOC actually matters

In a dual-pack system where one BMS acts as the CAN master toward the
inverter, the inverter's displayed/used SOC comes **only from the master
pack**. A SOC mismatch between packs is a monitoring and bookkeeping
accuracy problem — it does not by itself destabilize the inverter — but it
should still be corrected, because dashboards, cycle counting, alarms, and
any automation built on a pack's own reported SOC need that number to be
trustworthy for that pack specifically.
