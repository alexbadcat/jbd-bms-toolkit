*[Українська версія](balancing.uk.md)*

# Passive balancing

## The physics, briefly

A passive balancer bleeds excess charge off the fullest cells through a
resistor, at roughly **30-60 mA** per cell on this hardware. It can only ever
pull the strongest cells *down* toward the weakest — it cannot push a weak
cell up. Two consequences that shape everything below:

1. **Balancing only matters near the top of the LFP curve** (cell voltage
   ≳3.45V). LFP's voltage-vs-SOC curve is almost flat through the middle
   (3.25-3.40V) — cell-to-cell deltas there are typically 3-7 mV even when the
   *real* capacity mismatch between cells is significant. The mismatch only
   becomes visible as voltage spread once cells approach full, where weaker
   cells' voltage rises faster for the same remaining charge headroom.
2. At 30-60 mA, the balancer is weak relative to charge currents of tens of
   amps — it cannot "catch up" a meaningfully imbalanced pack in one charge
   cycle. Convergence takes repeated top-of-curve sessions over days.

## Balance trigger modes are mutually exclusive

Both firmware families expose **exactly two** balance-trigger modes, and the
firmware does **not** support running both at once:

- **Charge-only** — the balancer only bleeds cells while charge current is
  flowing.
- **Static** — the balancer only bleeds cells while the pack is at rest (no
  significant current: CV-hold pause, OVP state, idle).

This was confirmed with live tests on the classic-DD pack: with `0x2D = 479`
(bit3=1, charge-only) and the pack sitting at `I=0.0A` for 15 minutes with
max cell 3.611V and a 36 mV spread (well above the threshold), the balancer
stayed completely off. With `0x2D = 471` (bit3=0, static-only), the balancer
engaged *immediately* at `I=0`, cycling between two groups of cells
(`[3,9,11,13,15]` vs `[6,8,10,12,14,16]`) — but the same setting produced no
balancing at all under a 32A charge current. So despite how the community
register map describes bit3 ("enable balancing during charge"), it actually
behaves as a **mode selector**, not an additional permission layered on top
of some base balancing behavior.

On the ES-UP side, register `0x1C7C` (absolute address, see
[protocol-es-up.md](protocol-es-up.md)) controls the same choice for the
newer firmware: `0` = charge-only, `1` = static. Confirmed live: value `1`
actively balances while the pack sits in OVP at `I≈0`.

See [protocol-jbd-dd.md](protocol-jbd-dd.md#0x2d-funcconfig-bits) and
[protocol-es-up.md](protocol-es-up.md#0x1c00--user-parameters-writable-via-plain-0x79-136-bytes)
for the exact register addresses.

## The mode follower (`balance_mode.py`)

Since neither mode is universally correct — a pack idling in CV-hold for hours
needs static mode to use that time, but a pack charging hard with a real
knee-area imbalance needs charge-only mode to even get a chance to balance —
this toolkit runs a small state machine, `BalanceModeFollower`, **inside the
polling daemon's main loop** (`jbd2mqtt.py`), that decides the needed mode per
pack and writes it only when it actually needs to change.

⚠️ This must run from the same process, between reads of the same pack, as the
daemon that owns the bus — a standalone script polling independently would
race with the daemon's own reads and corrupt frames on the shared RS485 bus.

### Decision logic (per pack address)

1. **Gate**: whether balancing is possible at all right now, with hysteresis
   to avoid chattering — turns ON at `cell_max ≥ 3.40V`, turns OFF only below
   `3.35V`. While the gate is off, nothing is read or written for this
   decision (saves unnecessary EEPROM wear on a pack sitting on the flat part
   of the curve where mode doesn't matter).
2. While gated **on**:
   - current `≥ 1.0A` held stably for `≥120s` → desired mode = `"charge"`
   - `|current| ≤ 0.5A` held stably for `≥120s` → desired mode = `"static"`
   - `0.5-1.0A` is a **dead zone** — desired mode doesn't change, and the
     120s stability timer resets if the current later leaves this zone in
     either direction.
3. A write only happens when the desired mode differs from the last known
   mode, and **no more than once per 5 minutes per pack** (the same timer
   also rate-limits retries after a failed write).
4. After a write, the follower re-reads to confirm. **3 consecutive
   confirmation failures** → stop retrying for **1 hour** on that pack (reads
   of telemetry are unaffected — only the mode-write attempts pause).

### No persisted process state — by design

The follower does **not** cache "known mode" to a file across daemon
restarts. The BMS's own EEPROM is the single source of truth: on daemon
startup, `BalanceModeFollower.startup()` re-reads the **actual** mode
directly from each pack. The step logic then recomputes the desired mode
fresh from current telemetry (`cell_max`/`current`) on every tick — it's not
based on a state diff — so if a restart happens mid-transition, the system
just re-converges on its own via the same debounce/rate-limit rules, with no
separate "recovery" logic needed.

### Why the old pack writes a known constant, not read-modify-write

The classic-DD pack's mode bit lives inside a larger bitmask register
(`0x2D`, `FuncConfig`), which also controls `balance_en` and LED indicator
bits. The naive approach — read the current value, flip just bit3, write it
back — failed in production: on 2026-10-03 the follower once read a
**corrupted frame** off the shared bus (`0x80E7`, bus glitch, not the real
register value) and would have written that garbage straight back, wiping
out the LED and other unrelated bits along with it.

The fix: never read-modify-write this register. Always write a **known-good
constant** (`OLD_FUNC_BASE = 0x01D7`, i.e. decimal 471) with only bit3 set or
cleared depending on target mode:

```python
OLD_FUNC_BASE = 0x01D7   # factory baseline: balance_en + LED bits, bit3=0 (static)
new_raw = (OLD_FUNC_BASE | 0x08) if target == "charge" else (OLD_FUNC_BASE & ~0x08 & 0xFFFF)
```

This makes a single corrupted read harmless — the write always lands on a
value known to be correct in every bit except the one being intentionally
changed.

## The `top_spread` metric (`top_spread.py`)

A dedicated tracker, `TopSpreadTracker`, measures the real cell-voltage spread
specifically **during top-of-curve sessions**, since (per the physics section
above) spread on the flat part of the curve is meaningless noise.

- **Session start**: `cell_max ≥ 3.45V`.
- **Session continues** through brief dips into the `3.40-3.45V` band (still
  considered "the knee") without ending the session.
- **Session end**: `cell_max` stays `< 3.40V` for **≥10 minutes** — the
  10-minute hold (debounce) exists specifically to avoid a session being cut
  in half by a brief MQTT-polling glitch or a momentary voltage dip.
- During a session, the tracker keeps the **peak** `(cell_max - cell_min)` and
  which two cells it occurred between.

Published fields (merge into the MQTT state payload):

| Field | Meaning |
|---|---|
| `top_spread_now` | Peak spread of the *current* session, in mV; `null` outside a session |
| `top_spread_last` | Peak spread of the *last completed* session |
| `top_spread_last_at` | Timestamp of the last completed session |
| `top_spread_hi_cell` / `top_spread_lo_cell` | Which cells had the max / min voltage at the peak |

`top_spread_now` is intentionally **not** persisted to disk — after a daemon
restart it simply rebuilds itself from the next few ticks while the gate is
active. `top_spread_last` **is** persisted (`top_spread.state.json` next to the
script), because it's meant to survive a reboot and feed a nightly "is this
pack still imbalanced" automation check.

## Case study: healing a 374 mV spread

A newer pack with a corrupted cell-measurement gain (see
[protocol-es-up.md](protocol-es-up.md#-key-finding-write-addressing-inside-0x3800-is-by-u16-index-not-byte-offset))
also had a genuinely large top-of-curve imbalance once the gain corruption
was separately fixed. Treatment over roughly two days, using repeated cycles
of:

```
small-current charge (balancer engaged, charge-only or static as needed)
  → rest in CV-hold / static-balance
    → partial discharge
      → repeat
```

brought the top-of-curve spread down: **374 mV → 146 mV → 59 mV** over
successive sessions, continuing down toward the high-teens (mV) as the
weakest cells caught up. Along the way, the balance window register (`0x2B`
on classic DD, the `BalanceDiff` field on ES-UP) was found parked at a wide
value (`60`) that had been set temporarily to avoid over-tugging the middle
of the pack during an earlier phase — narrowing it back to `20` noticeably
sped up convergence of the last lagging cells.

### Cell #1 case study

Separately, one cell on the newer pack appeared "stuck" roughly +45 mV high
relative to its neighbors — this looked like a real imbalance but was
actually a **corrupted voltage-measurement gain coefficient** (ES-UP block
`0x3800`, data offset +8, i.e. cell #1's gain), caused by the write-indexing
bug documented in [protocol-es-up.md](protocol-es-up.md). It was not a
balancing problem at all, and no amount of balancer time would have fixed
it. Cured by writing the coefficient back to its factory value (`10006`)
through the correctly-indexed write address (`0x3804`).

**Lesson**: before spending days "balancing" a cell that refuses to
converge, check whether its reported voltage is itself believable — a
constant, unchanging offset on one cell regardless of pack state is a
calibration/measurement problem, not a charge-imbalance problem.

## Recommendations

- Set the same balance-start voltage (`0x2A` / `BalanceV`) on all packs
  sharing a bus — values around 3350-3400 mV worked well on this hardware.
- Once cells have converged, keep the balance window (`0x2B` / `BalanceDiff`)
  tight (around 20 mV) — a wide window set temporarily to tug the pack
  middle should be narrowed back down once that phase is done.
- Run the mode-follower logic **inside the polling process**, never as a
  separate concurrent script — bus collisions corrupt frames on both sides.
- Default to **static mode** when in doubt: LFP's real cell-to-cell mismatch
  reveals itself more reliably at rest (constant voltage hold, OVP) than
  under high charge current, which tends to mask small deltas in the
  per-cell readings.
