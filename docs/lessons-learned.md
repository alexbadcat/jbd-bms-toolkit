*[Українська версія](lessons-learned.uk.md)*

# Lessons learned

Pitfalls hit in the field while building and operating this toolkit against
real hardware. Read this before writing anything to a BMS you depend on.

## Bus collisions with the polling daemon

Any manual read/write session (`jbd_settings.py`, `esup.py`-based scripts)
run **while the `jbd2mqtt.py` polling daemon is alive** on the same RS485 bus
causes collisions: garbled or truncated frames, and in the worst case an
apparent "setting" value that's actually just the instantaneous pack voltage
bleeding into the wrong register slot because two devices answered
overlapping requests on a shared line.

**Always stop the daemon before any settings/calibration session, restart it
after.** This applies equally to the classic-DD path and the ES-UP path —
both share the same physical bus.

## A write that looks like it failed may still have executed

Serial comms over a shared bus are inherently lossy — a call that times out,
returns an error, or gives an ambiguous acknowledgement does **not** reliably
mean nothing happened on the BMS side. Treat every ambiguous outcome as "may
have executed" and **re-read the actual state** afterward rather than trusting
the call's return value or retrying blindly.

## Verify persistence with a fresh session

Some values look "written" immediately after a write — the BMS even
acknowledges them — but don't survive a service-mode exit / EEPROM save, and
only reveal their real, final value on a **fresh** read: a new serial
connection, a fresh service-mode entry, not just a read-back within the same
write transaction. `jbd_settings.py`'s `do_set()` enforces this explicitly:
after writing and saving, it re-enters a brand new session and re-reads the
register before declaring success.

## "accepted=True" does not mean "saved" or "had any effect"

An acknowledgement from the BMS only means it didn't return an error status
for that specific frame — nothing more. Two concrete cases observed on real
hardware:

- DD-protocol service writes to register `0x12` on the newer "polyglot"
  firmware ack as accepted, then **silently revert** on the next fresh
  read-back.
- DD current-calibration registers `0xAD`-`0xAF` ack as accepted on that same
  firmware with **zero measurable effect** on the reported current.

**Always verify by effect** (measure the thing the register is supposed to
control, independently — current, voltage, reported capacity) or by a
fresh-session read-back. Never trust the ack alone. This is why every write
helper in this toolkit (`write_reg`, `do_set_remaining`, `do_calibrate`,
`set_gain`) returns and prints an explicit before/accepted/after comparison
rather than just a boolean "did it work."

## A corrupted calibration coefficient can masquerade as a vendor lock

A cell voltage reading that looks permanently, consistently off by a fixed
offset — even when it refuses to be fixed by the vendor's own official
software over the same RS485 link (which, in this case, returned a
"calibration failed" error) — is not necessarily a real hardware lock. In
this project's case it turned out to be a DIY-tool write-indexing bug (see
[protocol-es-up.md](protocol-es-up.md#-key-finding-write-addressing-inside-0x3800-is-by-u16-index-not-byte-offset)):
a write intended for one register landed on a neighboring one because of an
undocumented index-vs-byte-offset addressing quirk in the firmware's debug
mode. **Always suspect your own addressing logic before concluding "the
vendor locked it."**

## Interrupted calls are not safely "no-ops"

Design write helpers to assume a call might be interrupted mid-transaction
(exception, timeout, process kill) at any point, and make sure the BMS is
never left in a half-configured state as a result. Concretely, this toolkit
always wraps the service-mode exit in a `finally` block — so even if the
write itself throws, the BMS still gets taken out of service mode cleanly
(exiting without save if nothing meaningful happened, with save if a write
was attempted).

## Bad frames can look like good data if you don't check enough

A real production bug: Home Assistant showed SOC momentarily jumping to
impossible values (3%, 183%) while the true pack voltage was rock steady.
Root cause: on a Y-split bus carrying two packs, occasionally a corrupted or
shifted frame still had intact `DD...77` start/end markers and passed a
naive parser that only checked those outer bytes plus the status byte. Fix
required three layers of validation on every received frame:

1. The echoed address byte must match the address that was actually queried.
2. The frame length must fully cover the declared payload length plus the
   trailing CRC bytes (reject anything truncated).
3. The CRC itself (computed over address+action/status+cmd+len+data, see
   [protocol-jbd-dd.md](protocol-jbd-dd.md#checksum)) must match.

...plus a final plausibility guard as a last line of defense even after CRC
passes: pack voltage must fall within `30-62V` and SOC within `0-100%`,
otherwise the frame is rejected as implausible rather than published as real
data. A battery management system feeding dashboards and automations is not
a place to publish "technically CRC-valid but physically impossible" data.

## Inverter-side detail that looks like a bug but isn't

On the Deye inverter used in this setup: setting the charge current limit to
0A does **not** stop CV (constant-voltage) hold — it only disables
grid-charging specifically; the inverter can still hold the battery at its CV
setpoint via other charge sources (e.g. solar). SOC and the voltage target
the inverter requests come from the BMS acting as CAN master over Pylon/
Lithium protocol, not from inverter-side configuration — so a "stuck at the
same voltage despite 0A limit" symptom is not a bug, it's how that limit is
scoped.

## General takeaway

Every lesson above reduces to the same discipline: **don't trust a single
signal.** Not an ack byte, not a one-shot read, not an outwardly intact frame
envelope, not "the vendor's tool also can't do it." Cross-check with an
independent measurement (effect, fresh read, plausibility bound, protocol-
level CRC) before treating any BMS interaction as confirmed.
