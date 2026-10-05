*[Українською](home-assistant.uk.md)*

# Home Assistant examples

This page documents the Home Assistant integration that goes with
`jbd2mqtt.py`, and the four example automations in
[`examples/home-assistant/`](../examples/home-assistant/).

These automations come from a real dual-pack LiFePO4 + Deye/Solarman setup and
are published with generalized entity IDs (`sensor.bms_a_*`, `number.inverter_*`,
...) — adjust them to whatever your own system's Home Assistant actually
discovered. They assume a Deye-family hybrid inverter talking Modbus/Solarman
to Home Assistant, but the underlying patterns (voltage-based current
throttling, a balance-cycle flag, drift-triggered night balancing, scheduled
slow top-up) apply to any inverter that exposes a settable charge-current
number entity and an SOC-target schedule.

## What `jbd2mqtt` publishes

`jbd2mqtt.py` polls each configured BMS over RS485 and publishes its state as
one MQTT JSON payload per pack, with [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
configs so Home Assistant creates the entities automatically — nothing to
hand-add in `configuration.yaml`.

Per pack (node `deye_bms` for the first configured address, `deye_bms_2`,
`deye_bms_3`, ... for the rest), you get:

- **Pack-level sensors**: SOC %, pack voltage, current, power, remaining Ah,
  real/nominal capacity, SOH %, cycle count.
- **Cell sensors**: `cell_1` .. `cell_16` individual voltages, plus
  `cell_min` / `cell_max` / `cell_delta`.
- **`cell_max_raw`**: the BMS's own, uncompensated reading of its highest
  cell. Only relevant if your BMS has a known per-cell voltage calibration
  offset that your software corrects for in the other sensors — the BMS's
  *hardware* OVP protection and its own balancer act on this raw value, not
  the corrected one. If both your packs read accurately, you don't need it.
- **Temperature sensors**: one per onboard sensor (commonly 4).
- **Diagnostics**: charging/discharging MOSFET state (binary sensors), active
  protection flags, which cells the passive balancer is currently bleeding.
- **`balance_mode`**: `charge` / `static` / `unknown` — which balancing mode
  the BMS is currently in. A passive JBD-style balancer typically can't
  balance in both "while charging" and "at rest" modes at once; a small
  follower process switches the BMS's setting to match what's actually
  happening (see the toolkit's main README for `balance_mode.py`).
- **`top_spread_now` / `top_spread_last`**: the real top-of-charge cell
  spread — see [Night balance guard](#night-balance-guard-night_balance_guardyaml)
  below for why this, specifically, is what the night guard watches.

**Availability**: every entity's `availability` combines two MQTT topics with
`availability_mode: all` — a shared daemon-wide LWT (`deye_bms/daemon/availability`,
set via MQTT's own will mechanism so it flips to `offline` immediately if the
process dies) and a per-pack topic (flipped to `offline` after a few
consecutive read failures on that pack specifically). Both must be `online`
for the entities to read as available — so a crashed poller shows *every*
pack offline, while a single pack losing RS485 contact (BMS went to sleep,
wiring issue) only marks that one pack offline.

## Installing the automations

1. Copy `helpers.yaml`'s `input_boolean` / `input_select` / `input_text`
   entries into your own helpers (via the UI — Settings → Devices & services →
   Helpers — or your YAML packages), with the same entity IDs or your own.
2. Copy the four automation YAML files into Home Assistant (paste into the
   automation editor's YAML mode, or drop into `automations.yaml` /
   a package).
3. **Rename every entity ID** in them to match what your own `jbd2mqtt`
   instance and inverter integration actually exposes — check
   *Developer tools → States* for the real names, or give your discovered
   entities explicit `entity_id`s via the UI so they're predictable.
4. Point the `notify.notify` calls at your own notify service.
5. Run *Developer tools → YAML → Check configuration* before reloading
   automations.

## The automations

### Cell voltage guard (`cell_voltage_guard.yaml`)

The core safety net: throttles the inverter's charge-current limit as any
monitored cell approaches the BMS's hardware overvoltage-protection (OVP)
trip point, so the BMS's own protection circuit never has to act.

| Cell voltage | Action |
|---|---|
| ≥ 3.70 V | **stop** — charge current → 0 A |
| ≥ 3.62 V | **slow** — charge current → 5 A (3 A during an active balance cycle) |
| ≤ 3.55 V | **resume** (partial) — current ladder steps back up |
| ≤ 3.50 V | **restore** (full) — charge current restored to its normal value (70/80 A) |

A 5-minute heartbeat re-evaluates the actual cell voltages independently of
the trigger events, so a threshold crossed while Home Assistant happened to
be restarting is caught within 5 minutes instead of silently missed. The
automation also recognizes an "emergency"/backup charging mode (detected via
the inverter's SOC-target schedule) where it intentionally leaves the SOC
target alone and only throttles current.

⚠️ **On a Deye/Solarman inverter, setting the charge-current limit to 0 A
does *not* by itself stop the inverter's CV (constant-voltage) hold on the
battery bus.** The inverter will keep the bus pinned near its configured
charge voltage even at a 0 A current limit, which can still push a hot cell
over the edge. A *hard* stop requires turning the grid-charging switch off —
`balance_hold.yaml` does exactly that around its pause/resume cycle.

### Balance hold (`balance_hold.yaml`)

Drives a dedicated "Balance" option on an ESS mode selector (`input_select`).
Passive balancing only does meaningful work on the pack's *top knee*
(roughly ≥ 3.45 V) — on the flat part of a LiFePO4 curve, voltage
differences between cells are mostly sensor and wiring noise, not real
capacity imbalance. So instead of holding the pack pinned at a high voltage
(risking OVP) or hoping balancing happens incidentally, this automation
cycles the charge current: charge a little → let the voltage relax with the
balancer working in its "static" (at-rest) mode → charge a little more —
letting `cell_voltage_guard.yaml`'s tighter current ladder do the actual
step-down/step-up work while this flag is set.

Includes a safety exit (grid power lost for 10 minutes, or SOC drops below
85%) that switches back to a normal auto mode, restores full charge current,
and notifies — so a balance cycle can't accidentally run through an outage
on reduced charging capacity.

### Night balance guard (`night_balance_guard.yaml`)

Rather than balancing on a fixed schedule, this watches the actual measured
imbalance. Once a night (around 01:00, with a 1-hour window of heartbeat
retries so a Home Assistant restart doesn't skip it), if the ESS is in a
normal auto/eco mode and the **top-of-charge spread** (`top_spread_last`,
mV) on either pack is **≥ 40 mV** — or that data is missing/stale (≥ 7
days old) — it saves the current mode and switches to "Balance" for the rest
of the night, letting `balance_hold.yaml` + `cell_voltage_guard.yaml` do the
actual work. In the morning (around 06:58, same heartbeat-window pattern) it
restores whatever mode was active before, with a summary notification of how
the spread changed overnight.

Watching the spread specifically *at the top of charge* matters: on the flat
part of the discharge curve the per-cell voltage delta is just a few
millivolts of measurement noise regardless of real imbalance, so a guard
that watched absolute cell delta all the time would be meaningless. Tracking
"the worst spread seen during the last session where the pack was actually
near full" is the signal that's actually informative.

### Nightly top-up (`nightly_topup.yaml`)

On alternating mornings (by default Tue/Thu/Sat — adjust the weekday list to
taste), runs a low-current (15 A) charge up to a 100% SOC target for a few
hours before sunrise, then relaxes the SOC target and current back to normal.

This exists because of how most JBD-style BMS firmware resets its "SOC =
100%" fuel-gauge marker: roughly, *the pack-average cell voltage must stay
above a configured threshold for several seconds*. Two parallel packs with
slightly different internal resistance reach that average at (very) slightly
different charge currents and times — left purely to a fast daily full
charge, their reported SOC and remaining-capacity numbers will silently
drift apart over weeks. A slow, deliberate top-up a few times a week, with
enough time at the top for *both* packs to actually hit the reset condition,
keeps the fuel gauges in sync without living at a high voltage every single
day.

This automation deliberately **does not** fight `cell_voltage_guard.yaml`:
its "stop" step only restores the charge current if it's still sitting at
the top-up's own marker value (15 A) — if the cell voltage guard has since
pulled it down further to protect a hot cell, the top-up leaves that alone.

## General caveats

- ⚠️ **A charge-current limit of 0 A does not stop CV voltage hold on a
  Deye/Solarman inverter.** If you need charging to *actually* stop (not
  just throttle to a trickle), turn the grid-charging switch off, not just
  the current limit down to zero.
- ⚠️ **Don't lower the maximum *discharge* current** to try to protect the
  pack from the load side — these automations only ever touch the *charge*
  current limit. Reducing discharge current limits your usable power and
  doesn't address the overvoltage risk these automations are built around
  (which only happens while charging).
- These thresholds (3.70 / 3.62 / 3.55 / 3.50 V, 40 mV) were tuned for a
  specific 16S LiFePO4 pack and BMS OVP setting with roughly a 30-second
  MQTT poll cycle. Check your own BMS's configured OVP voltage and poll
  interval before reusing these numbers — leave enough margin between your
  "stop" threshold and the BMS's hardware OVP trip for at least one full
  poll cycle's worth of voltage rise at full charge current.
