*[Українська версія](installation.uk.md)*

# Installation guide

Step-by-step setup of `jbd-bms-toolkit`: reading a JBD/Jiabaida LiFePO4 BMS
over RS485, bridging it to MQTT/Home Assistant, and (optionally) tuning
balancing and capacity settings. Every command below is copy-paste ready and
matches the actual CLI flags in the scripts (`--help` always works too).

## 0. What you need

**Hardware**

- A JBD/Jiabaida "UP"-series LiFePO4 BMS (this toolkit was built and tested
  against UP16S010 and UP16S019 rev.2 "polyglot" units; other UP-series models
  that speak the same DD frame should work for reading — see
  [protocol-jbd-dd.md](protocol-jbd-dd.md)).
- A USB-to-RS485 adapter. An FTDI-chipset one is recommended (the udev rule in
  `deploy/udev/99-rs485.rules` is written for FTDI's vendor/product ID —
  adjust it if you use a different chipset, e.g. CH340).
- A cable from the adapter to the BMS's **RS485** port (not "Parallel"/"UART"
  if your BMS has several RJ45 ports — the RS485 pinout is in your BMS's own
  datasheet; the pinout for UP16S010 is noted in `src/jbd_bms.py`'s docstring
  as a reference point, but **check your own unit's datasheet**, pinouts vary
  between JBD OEM batches).
- A Linux host to run the bridge: a Raspberry Pi, a NAS, a small server, or a
  VM — anything that can keep a USB device attached and run Python 3.9+.
- An MQTT broker (e.g. Mosquitto) reachable from that host.
- Home Assistant with the MQTT integration configured and pointed at the same
  broker (for MQTT discovery to pick the entities up automatically).

## 1. Wire it up

- Connect the USB-RS485 adapter's A/B lines to the BMS's RS485 port's A/B
  pins, and GND to GND. Leave the adapter's termination/bias jumpers at
  their default unless you have signal integrity issues on a long run.
- Serial parameters are fixed by the BMS firmware: **9600 baud, 8N1**. None of
  the scripts here expose a baud-rate flag — if your BMS doesn't answer at
  9600, it's a wiring or address problem, not a baud-rate one.
- **Pack address**: each BMS on the bus is identified by an address, normally
  set via a DIP switch or jumper block on the BMS itself (consult your
  model's manual — on UP-series units, `0000` is typically address 0, the
  default "master"). If you're only running one pack, leave it at the
  default (address 0) — you won't need to touch `BMS_ADDRS`/`--addr` at all.
- **Multiple packs in parallel**: wire every pack's RS485 port to the same
  bus (a Y-split on the external connector works — this is a shared,
  multi-drop bus, not point-to-point), and give each pack a **distinct**
  address via its DIP switch before connecting it. Note which address is
  "classic DD" vs. "ES-UP/polyglot" firmware — see step 7 and
  [protocol-es-up.md](protocol-es-up.md) for why that distinction matters for
  writes (not for reads).

## 2. Install

```bash
git clone <this-repo-url> jbd-bms-toolkit
cd jbd-bms-toolkit
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

For running the test suite too:

```bash
pip install -r requirements-dev.txt
```

## 3. First read — confirm the wiring and address

With the daemon **not** running yet (nothing else should hold the serial port):

```bash
python3 src/jbd_bms.py --port /dev/ttyUSB0
```

This prints the pack summary (voltage, current, SOC, capacity, temperatures,
protections) and all cell voltages. If you get `немає відповіді` /
"no response" errors, double check A/B polarity, GND, the DIP address, and
that you're on the RS485 (not "Parallel") connector.

If you don't know what address a given pack answers to, or you're trying to
figure out whether it's classic DD, Modbus, or something else entirely, scan it:

```bash
python3 src/bms_probe_multi.py
```

This is a fixed scan (no CLI flags) that tries classic DD, DD-with-address,
Modbus RTU (function codes 3/4) and a PACE/Seplos ASCII probe at 9600/19200/
115200 baud and prints any raw reply. It only probes the bus when actually
invoked (run directly, like any other script here — it does have the usual
`if __name__ == "__main__"` guard). Edit the `PORT` constant at the top, or
set env `BMS_PORT`, if your adapter isn't `/dev/ttyUSB0`.

Once you know the address, re-run `jbd_bms.py --addr N` for each pack to
confirm they all answer individually:

```bash
python3 src/jbd_bms.py --port /dev/ttyUSB0 --addr 1 --json
```

## 4. Back up settings before touching anything

**Before any write**, dump and save the current service registers — this is
your rollback if a write goes wrong:

```bash
python3 src/jbd_settings.py --port /dev/ttyUSB0 --addr 0 --save backup-pack0.json
python3 src/jbd_settings.py --port /dev/ttyUSB0 --addr 1 --save backup-pack1.json
```

`jbd_settings.py` with no `--save`/`--set`/`--read` flags just dumps the
decoded registers to the screen. Keep these backups somewhere safe outside
the repo working tree.

## 5. Configure and do a manual run

```bash
cp jbd2mqtt.env.example jbd2mqtt.env
$EDITOR jbd2mqtt.env
```

Fill in at minimum `MQTT_HOST` and, if your pack address isn't 0, `BMS_ADDRS`
(comma-separated, e.g. `BMS_ADDRS=0,1`). See the comments in
`jbd2mqtt.env.example` for every variable this toolkit reads (MQTT, serial
port, balancing follower, Home Assistant REST API for the optional analysis
scripts).

Run it in the foreground first, from the repo root, so you can see errors
immediately:

```bash
cd src && python3 jbd2mqtt.py
```

In another terminal, confirm data is flowing over MQTT:

```bash
mosquitto_sub -h <MQTT_HOST> -u <MQTT_USER> -P <MQTT_PASS> -t 'deye_bms/#' -v
mosquitto_sub -h <MQTT_HOST> -u <MQTT_USER> -P <MQTT_PASS> -t 'homeassistant/#' -v   # discovery configs, once at connect
```

Then check Home Assistant: with MQTT discovery enabled, a new device
("Батарея Deye (BMS)" / similarly named for additional packs) should appear
under **Settings → Devices & Services → MQTT** within a few seconds of the
bridge connecting, with all cell/voltage/current/temperature/balance
entities already attached — no manual `configuration.yaml` entity definitions
needed. Stop the foreground run (Ctrl-C) once you've confirmed this.

## 6. Run it as a service

**systemd** (recommended for a Pi/server/NAS):

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin jbd2mqtt
sudo mkdir -p /opt/jbd-bms-toolkit
sudo cp -r . /opt/jbd-bms-toolkit/
sudo cp jbd2mqtt.env /opt/jbd-bms-toolkit/jbd2mqtt.env   # your filled-in config
sudo chown -R jbd2mqtt:jbd2mqtt /opt/jbd-bms-toolkit

# If you installed deps into a venv rather than system Python, edit
# ExecStart in the unit below to point at that venv's python3 first.
sudo cp deploy/systemd/jbd2mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jbd2mqtt.service
```

Check it's alive and watch the logs:

```bash
sudo systemctl status jbd2mqtt.service
sudo journalctl -u jbd2mqtt.service -f
```

**Stable device name (udev)**: `/dev/ttyUSB0` can silently renumber after a
reboot or a USB replug if you have more than one serial adapter. Install
`deploy/udev/99-rs485.rules` (instructions are in the file's comments — find
your adapter's `idVendor`/`idProduct` with `udevadm info`, copy the rule to
`/etc/udev/rules.d/`, reload rules) to get a stable `/dev/rs485-bms` symlink,
then point `SERIAL_PORT=`/`BMS_PORT=` in `jbd2mqtt.env` at that instead.

**Docker** (alternative to systemd): `deploy/docker/Dockerfile` +
`deploy/docker/docker-compose.yml` build and run the bridge in a container
with `/dev/ttyUSB0` passed through via `devices:`. Config is read from
environment variables (`env_file: jbd2mqtt.env`) since the `.env`-file
auto-load only looks next to the script on disk. Adjust the `devices:` entry
to your actual device path first.

## 7. Balancing follower

The BMS's passive balancer has two mutually exclusive modes — "balance only
while charging" and "balance while at rest" — and only one can be active at a
time (see [balancing.md](balancing.md) for the physics and why this matters).
`balance_mode.py`'s `BalanceModeFollower`, wired into `jbd2mqtt.py`'s main
loop, watches each pack's cell voltage and current and switches the mode to
match, automatically, with hysteresis and rate-limited writes so it doesn't
hammer the BMS's EEPROM.

It's **on by default**. To turn it off without touching code:

```bash
# in jbd2mqtt.env
BALANCE_MODE_FOLLOW=0
```

If you have packs on both the classic DD protocol and the ES-UP/"polyglot"
protocol on the same bus, tell the follower which addresses are ES-UP (the
write path differs by protocol — see [protocol-es-up.md](protocol-es-up.md)):

```bash
# in jbd2mqtt.env — example: address 1 is the ES-UP pack, address 0 is classic DD
BMS_ESUP_ADDRS=1
```

Leave `BMS_ESUP_ADDRS` empty if you only have classic-DD packs (or only one
pack at address 0) — the default behavior (address 0 = classic DD, anything
else = ES-UP) is unchanged.

## 8. Calibration and SOC — read before you write

Capacity/SOC drift between parallel packs and current-sensor calibration are
covered in detail, with the exact register-level recipes, in
[soc-calibration.md](soc-calibration.md),
[protocol-jbd-dd.md](protocol-jbd-dd.md) and
[protocol-es-up.md](protocol-es-up.md) — read those before writing anything.

**The one rule that applies to every write in this toolkit: stop the
`jbd2mqtt` daemon (and `ser2net`, if you're running the example in
`examples/ser2net/`) before reading or writing settings by hand.** Two
processes polling/writing the same RS485 bus at once produces collided,
truncated frames — at best a failed write, at worst a write with garbage
data. Resume the daemon only after you've verified the change:

```bash
sudo systemctl stop jbd2mqtt.service      # or: kill the foreground/manual run
python3 src/jbd_settings.py --set 0x2A=3400 --yes     # example: balance-start voltage
sudo systemctl start jbd2mqtt.service
```

## 9. Home Assistant integration

MQTT discovery (step 5) gets every entity into HA with no YAML required. For
automations built on top of this data — cell-voltage guards, balance-mode
dashboards, nightly top-up routines — see
[home-assistant.md](home-assistant.md) and the ready-to-adapt examples in
[`examples/home-assistant/`](../examples/home-assistant/). For a Lovelace
card that visualizes the per-cell/balance data nicely, see
[`lovelace-card/README.md`](../lovelace-card/README.md).

## 10. Checklist — confirm everything works

- [ ] `python3 src/jbd_bms.py --addr <N>` returns sane data for every pack
      (voltage in the 30-62V ballpark for a 16S LFP pack, SOC 0-100%).
- [ ] `jbd2mqtt` is running (`systemctl status jbd2mqtt` is `active`, or the
      container is `Up`).
- [ ] `mosquitto_sub -t 'deye_bms/#' -v` shows a `state` message updating
      every `POLL_INTERVAL` seconds, and `availability` = `online`.
- [ ] Every pack shows up as its own device in HA → Devices & Services → MQTT,
      with cell, voltage, current, temperature and balance entities populated
      (not `unknown`/`unavailable`).
- [ ] If you enabled the balancing follower: the `Режим балансування`
      ("balance mode") sensor shows `charge` or `static`, not `unknown`,
      after the pack has spent a couple of minutes above the balance-start
      voltage.
- [ ] `python3 -m pytest tests/ -q` passes (sanity check that nothing in your
      checkout is broken — these tests don't touch real hardware).

## 11. Updating

```bash
cd /opt/jbd-bms-toolkit          # or wherever you deployed it
sudo systemctl stop jbd2mqtt.service
git pull
pip install -r requirements.txt --upgrade
sudo systemctl start jbd2mqtt.service
```

Diff `jbd2mqtt.env.example` against your `jbd2mqtt.env` occasionally — new
variables get added with safe defaults, but it's worth knowing what's new.

## 12. Troubleshooting

**No response from the BMS at all** (`немає відповіді`/timeouts from
`jbd_bms.py`): check A/B polarity (swap them — it's the most common mistake),
GND connection, that you're on the RS485 connector (not "Parallel"/UART on
multi-port BMS units), and the DIP address. Run `bms_probe_multi.py` to rule
out a protocol/baud mismatch entirely.

**Garbled/implausible values (SOC jumping between absurd numbers, voltage
outside 30-62V)**: almost always a **collision on a shared bus** — something
else (a manual script, `ser2net`, a second instance of `jbd2mqtt`) is talking
to the BMS at the same time. Make sure exactly one process owns the serial
port. `jbd_bms.py`'s `read_all()` already rejects physically-impossible
frames rather than passing them through, but if you're seeing this from raw
probing, it's a bus contention problem, not a BMS problem.

**Wrong pack keeps answering, or a pack doesn't respond on its expected
address**: confirm the DIP address physically matches what you configured in
`BMS_ADDRS`/`--addr`. Some "polyglot" firmware answers on *any* non-zero
address during a scan — if you have more than one pack, verify each
address individually (step 3) rather than assuming the DIP switch setting is
honored exactly as documented for your particular firmware revision.

**`Permission denied` opening `/dev/ttyUSB0`**: your user (or the systemd
service's `User=`) isn't in the `dialout` group:

```bash
sudo usermod -aG dialout <youruser>     # then log out/in, or:
sudo usermod -aG dialout jbd2mqtt        # for the systemd service user
```

(The provided `deploy/systemd/jbd2mqtt.service` already runs as
`Group=dialout`, so this mainly matters if you're running scripts manually
as your own user, or customized the unit.)

**HA shows the device/entities as `unavailable`**: check
`jbd2mqtt`'s own `availability` topic state and the daemon's logs
(`journalctl -u jbd2mqtt -f` or stdout if run manually) — the bridge marks a
pack offline after 3 consecutive read failures, and the whole daemon offline
via its LWT if the process dies outright. A "no response" burst in the log
right before `availability` flips to offline means it's a serial issue (see
above), not an MQTT/HA issue.

**A register write didn't take ("прийнято=True" but it didn't stick)**: don't
trust the accept acknowledgement alone — every write path in this toolkit
re-reads in a **fresh session** to verify persistence, and the scripts print
that verification result explicitly (`ЗБЕРЕЖЕНО` / `НЕ ЗБЕРЕГЛОСЯ`). If it
says it didn't persist, the daemon (or another process) was very likely still
polling the bus during the write — stop it (step 8) and retry.

**`ModuleNotFoundError: No module named 'serial'` / `'paho'`**: you're not in
the venv, or skipped `pip install -r requirements.txt`. Quick one-off runs
without installing anything: `uv run --with pyserial --with paho-mqtt python3 src/jbd_bms.py`.
