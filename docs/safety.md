*[Українська версія](safety.uk.md)*

# Safety

Writing to a BMS's protection parameters, calibration registers, or capacity
counters carries real risk. A wrong value can weaken or disable a safety
threshold (overvoltage/undervoltage/overcurrent/overtemperature cutoffs),
corrupt SOC/capacity accounting, or — as documented in this project — silently
corrupt a per-cell calibration coefficient in a way that's hard to tell apart
from a real hardware fault.

- **Back up before you write anything.** Use `jbd_settings.py --save` (classic
  DD) or read and save the relevant block (ES-UP) before touching it, so you
  have a known-good baseline to restore from.
- **Never disable or weaken the BMS's own protection thresholds**
  (overvoltage/undervoltage/overcurrent/overtemperature) to work around a
  symptom. Those thresholds exist to prevent fire and cell damage. Tune
  balancing, calibration, and capacity-tracking parameters instead — treat any
  change to a protection threshold as something to reason through very
  carefully and keep easily reversible.
- **Only the registers this toolkit explicitly documents and allow-lists have
  been exercised and verified on real hardware here.** Writing undocumented
  registers is unverified territory with unknown side effects.
- **Verify every write by its real-world effect** (measured current, measured
  voltage, a fresh-session read-back) — never by the BMS's "accepted"
  acknowledgement alone. See [lessons-learned.md](lessons-learned.md).
- **Never write to the BMS while a polling daemon or any other process is also
  talking to it on the same bus.** Stop it first.
- This toolkit was built and tested against two specific JBD/LS-Battery
  firmware variants (classic-DD UP16S010, and LS/ES-UP "polyglot" UP16S019
  rev.2) behind a specific inverter (Deye SUN-5K-SG03LP1, Pylon/Lithium CAN
  protocol). Register meanings, offsets, and especially the DV/DP
  write-indexing behavior are **not guaranteed to be identical on other JBD
  BMS models or firmware revisions** — verify read-only against your own
  hardware before writing anything.
- This project and its documentation come from real operational experience,
  not from a vendor-certified procedure. You are solely responsible for
  anything you do to your own battery system.
