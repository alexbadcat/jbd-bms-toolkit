# Deye Lovelace Card

🇺🇦 [Українською](README.uk.md)

A custom [Home Assistant](https://www.home-assistant.io/) Lovelace card for a Deye hybrid
inverter (tested on SUN-5K-SG03LP1 via the [Solarman](https://github.com/davidrapan/ha-solarman)
integration) plus up to two JBD BMS battery packs bridged through
[`jbd2mqtt.py`](../src/jbd2mqtt.py) from this toolkit.

The card UI text is Ukrainian (not translated) — everything else (entity IDs, outage
data source, calendars) is configurable so the card works with **any** prefix/region.

![Deye card preview](screenshots/deye-card-preview.png)

## Features

- Animated energy-flow diagram (grid / generator / battery / house around the inverter),
  three layouts: `hub`, `vertical`, `mini`.
- Battery state: SOC, voltage, current, temperature, capacity.
- Per-cell view for up to **two** BMS packs: 16 cells each, live balancing glow/wave on
  the cells the BMS is actively bleeding right now, delta/SOH/cycles/actual capacity.
- Four-position work-mode selector (Eco / Auto / Emergency / Balance) bound to an
  `input_select` helper, plus control popups for battery/grid/generator inverter settings
  (sliders, selects, switches, a generator-boost action button).
- **Graphs tab** (`deye-graphs.js`): SOC, power flows, cell delta, min/max cell voltage
  (with OVP threshold lines) + a balancing activity strip, charging-current limit — 6h/24h/7d
  ranges, tap-and-drag crosshair.
- **Outages tab** (`deye-outage.js`, optional): today/tomorrow outage timeline from a
  DTEK-style JSON schedule (fact + weekly preset fallback), tap a window for a popup with
  details, battery autonomy estimate, 7-day outage history reconstructed from grid-voltage
  history.
- Tap any numeric value to open a full-screen history chart (pan / pinch-zoom / crosshair).
- A Lovelace visual card editor (prefix, title, layout, scale, animation, colors) — advanced
  options below are YAML-only.

## Installation

1. Copy all three files into your Home Assistant `/config/www/` folder (a subfolder is fine,
   e.g. `/config/www/deye-card/`):
   - `deye-card.js`
   - `deye-graphs.js`
   - `deye-outage.js`

   (`deye-graphs.js` and `deye-outage.js` are loaded dynamically by `deye-card.js` via
   `import()` from the same folder as the resource you register below — keep all three
   files together.)

2. In Home Assistant: **Settings → Dashboards → ⋮ → Resources → Add resource**
   - URL: `/local/deye-card/deye-card.js?v=1` (adjust path; the `?v=1` is a cache-buster —
     bump it every time you update the file, see Troubleshooting below)
   - Resource type: **JavaScript module**

3. Add the card to a dashboard, either via the UI card picker (search "Deye") or as YAML
   (see examples below).

## Minimal config

```yaml
type: custom:deye-card
prefix: inverter_deye      # entity prefix used by your Solarman/Deye integration
```

With just this, you get the diagram, battery details, inverter details, graphs — anything
that depends on optional helpers (work-mode selector, monthly tariff cost, outage tab,
generator boost) simply doesn't render if the underlying entity doesn't exist. Nothing
errors out.

## Full config reference

```yaml
type: custom:deye-card

# ── core ──
prefix: inverter_deye          # Solarman/Deye entity prefix: sensor.<prefix>_battery, etc.
title: Deye Inverter           # optional, defaults to "Інвертор Deye"
title_size: 1.25               # optional, header font size in rem
layout: hub                    # hub | vertical | mini
scale: 1                       # 0.5–1.5, overall zoom
animate: true                  # false = disable flow/chevron animation (low-power kiosk displays)
colors:                        # optional, any subset; shown are the defaults
  grid: '#5ac8fa'
  generator: '#ff9f0a'
  battery: '#34c759'
  house: '#0a84ff'
  accent: '#ff8a3d'

# ── BMS packs (JBD bridge, see ../src/) ──
# single pack:
bms_prefix: battery_deye_bms
# OR up to two packs (bms_prefixes wins if both are set):
bms_prefixes:
  - battery_deye_bms
  - battery_deye_bms_2
bms_names:                     # optional, section headers in the expanded card
  - "Battery #1 (BMS)"
  - "Battery #2 (BMS)"

# ── optional overrides / helpers ──
time_to_full: sensor.time_to_full_charge     # "X h Y min"-style sensor, shown while charging
time_to_empty: sensor.time_to_empty          # same, shown while discharging
grid_meter_entity: sensor.my_external_meter  # override the grid-power source (defaults to
                                              # sensor.<prefix>_grid_power)

# four-position work-mode selector (input_select). Matches examples/home-assistant/helpers.yaml:
mode_entity: input_select.ess_mode
# map the card's four modes onto YOUR option strings (omit if your options are the
# Ukrainian «Еко / Авто / Критичний / Балансування»):
mode_options:
  eco: Eco
  auto: Auto
  emergency: Emergency
  balance: Balance

# monthly consumption-by-tariff-zone section (hides if none of these exist)
month_entities:
  day_kwh: sensor.consumed_zone_day
  night_kwh: sensor.consumed_zone_night
  day_uah: sensor.deye_cost_day
  night_uah: sensor.deye_cost_night
  total_uah: sensor.deye_cost_total

# "critical mode" auto-reasons banner — all optional, wire up your own automation/helpers
# (or omit entirely: the banner then just always shows the manual mode_entity state)
outage_entities:
  schedule: binary_sensor.outage_schedule_today       # a scheduled outage is expected today
  emergency: binary_sensor.outage_emergency_unified    # an emergency outage is active
  seen_today: input_boolean.deye_outage_seen_today     # there already was a >10min outage today
  possible24h: binary_sensor.outage_possible_24h        # "maybe" outages in the next 24h
  today_source: sensor.outage_dtek_today_source         # free-text source label for the above

# generator-boost action button in the "Generator" popup (hidden if the script doesn't exist)
gen_boost:
  script: script.deye_gen_boost     # a script you write yourself: raise charging current/peak
                                     # for an hour, then restore the two values below
  current: 30                       # A, restored if you cancel the boost early
  peak_shaving: 2500                # W, restored if you cancel the boost early

# Outages tab — OMIT ENTIRELY to not show the tab at all. source_url and group are
# REQUIRED together (no sensible default exists — it's your region/queue).
outage:
  # a JSON file in the format used by https://github.com/Baskerville42/outage-data-ua
  # (one file per oblast; you can also self-host a compatible JSON)
  source_url: https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json
  group: GPV1.1                                   # the queue/group key inside that JSON
  # optional fallback #2, only used if the JSON above is unreachable AND there's no cache yet
  calendar_scheduled: calendar.my_region_scheduled_outages
  calendar_planned: calendar.my_region_planned_outages
  # optional: binary_sensor(s) that flip an outage window's popup label to "EMERGENCY"
  emergency_entities:
    - binary_sensor.svitlo_my_region_emergency_outages
```

## Required/expected sensors

The card never errors on a missing entity — a section/row/tab just doesn't render. That said,
here's what each `prefix` powers (naming follows the
[Solarman](https://github.com/davidrapan/ha-solarman) integration's Deye entity scheme):

| Domain | Entities (with `prefix: inverter_deye`) |
|---|---|
| sensor | `battery`, `battery_power`, `battery_state`, `battery_voltage`, `battery_current`, `battery_temperature`, `battery_capacity`, `grid_power`, `generator_power`, `load_power`, `power_losses`, `output_l1_power`, `external_ct1_power`, `grid_l1_voltage`, `grid_frequency`, `temperature`, `device_state`, `device_alarm`, `today_battery_charge`, `today_battery_discharge`, `today_energy_import`, `today_energy_export`, `today_load_consumption` |
| binary_sensor | `grid`, `generator`, `connection` |
| select | `work_mode`, `energy_pattern`, `io_mode` |
| switch | `off_grid`, `generator`, `battery_wake_up`, `battery_grid_charging`, `battery_generator_charging` |
| number | `battery_max_charging_current`, `battery_max_discharging_current`, `battery_low_soc`, `battery_shutdown_soc`, `battery_restart_soc`, `battery_grid_charging_start`, `battery_grid_charging_current`, `zero_export_power`, `battery_generator_charging_start`, `battery_generator_charging_current`, `generator_peak_shaving`, `program_1_soc` … `program_6_soc` |

BMS entities (with `bms_prefix: battery_deye_bms`, from this toolkit's
[`jbd2mqtt.py`](../src/jbd2mqtt.py) MQTT-discovery bridge) use Ukrainian object names because
Home Assistant derives the `entity_id` by slugifying `"<device friendly name> <sensor
name>"` at first discovery, and the bridge's device/sensor names are Ukrainian:

| Suffix on `sensor.<bms_prefix>_…` | Meaning |
|---|---|
| `zariad` | SOC, % |
| `napruga_paketa` | pack voltage, V |
| `strum` | current, A |
| `delta_komirok` | cell delta, mV |
| `minimalna_komirka` / `maksimalna_komirka` | min/max single cell voltage, V |
| `zdorov_ia_soh` | SOH, % |
| `tsikliv` | charge cycles |
| `realna_iemnist` | actual capacity, Ah |
| `zalishok_iemnosti` | remaining capacity, Ah (used for the outage-tab autonomy estimate) |
| `temperatura_1` … `temperatura_4` | pack temperature sensors |
| `komirka_1` … `komirka_16` | per-cell voltage, V |
| `balansuvannia_komirok` | text: comma-separated cell numbers currently balancing, or `—` |
| `rezhim_balansuvannia` | `charge` / `static` / `unknown` |

If your bridge/device names differ, the resulting `entity_id` will too — check
**Developer Tools → States** and set `bms_prefix`/`bms_prefixes` to match whatever you
actually got (it's the common prefix before the suffix, e.g. for
`sensor.battery_deye_bms_zariad` the prefix is `battery_deye_bms`).

## Troubleshooting

- **Card doesn't update after editing the `.js` file on disk.** Browsers aggressively cache
  JS modules. Bump the `?v=` query string on the resource URL in
  **Settings → Dashboards → Resources** every time you redeploy the file (and hard-refresh,
  ⇧⌘R / Ctrl+Shift+R). `deye-graphs.js`/`deye-outage.js` are cache-busted automatically by the
  card itself (`DeyeCard.GRAPHS_V` / `DeyeCard.OUTAGE_V` static counters) — bump those two
  numbers in `deye-card.js` only if you edit those two files and need clients to pick up the
  change immediately.
- **Blank/empty card in the new "Sections" dashboard view.** Sections layout requires
  `getGridOptions()` (the card has one) but also a dashboard that has finished loading
  `hass` before the card mounts; if it stays blank, remove and re-add the card, or check the
  browser console for an import error (wrong resource path/case-sensitive filename).
- **"Не вдалось завантажити модуль…" error inside a tab.** The dynamic `import()` of
  `deye-graphs.js`/`deye-outage.js` failed — almost always a wrong path. Both files must sit
  in the exact same folder as `deye-card.js` under `/config/www/`.
- **Outages tab doesn't show up.** It's hidden unless `outage.source_url` and `outage.group`
  are both set in the card config — see Full config reference above.
- **Console shows CORS errors fetching the outage JSON.** Only works with a source that sends
  `Access-Control-Allow-Origin: *` (raw GitHub content does); you can't point it at an
  arbitrary website without CORS.
