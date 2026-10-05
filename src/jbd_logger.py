#!/usr/bin/env python3
"""jbd_logger — subscribes to the jbd2mqtt MQTT state topic and appends one
per-minute CSV row with every field the BMS exposes (all cells, deltas,
current/SOC/capacity, temperatures, balancing state, protections) — a dataset
for offline analysis of whether an imbalance is capacity- or resistance-driven.

Мета: чітка похвилинна картина, як комірки заряджаються/розряджаються, щоб
проаналізувати природу розбалансу (ємність vs опір vs саморозряд).

ДЖЕРЕЛО — MQTT-топік демона jbd2mqtt (`deye_bms/state`), а НЕ прямий серійний
порт: демон уже читає ВСЕ по RS485 і публікує повний JSON, тож логер лише
підписується (жодної гризні за /dev/ttyUSB0). У топіку є те, чого нема окремими
HA-сенсорами — зокрема `balancing_cells` (які саме комірки зараз зціджує балансир)
і `alarm_bitmask`.

Пише широкий CSV (jbd_detail.csv): усі 16 комірок, дельта, min/max + індекси,
струм зі знаком (+заряд/−розряд), потужність, SOC, залишок/реальна ємність, SOH,
цикли, 4 температури пакета + середовище + FET, стан заряд/розряд-MOSFET,
статус балансування (список комірок), захисти. Одна строка на хвилину.

Запуск:  nohup ./jbd_logger.py >> jbd_logger.out 2>&1 &
Статус:  ./jbd_logger.py --status
Стоп:    ./jbd_logger.py --stop
"""
import argparse
import datetime
import json
import os
import pathlib
import signal
import sys
import time

import paho.mqtt.client as mqtt

HERE = pathlib.Path(__file__).resolve().parent
CSV = HERE / "jbd_detail.csv"
PIDFILE = pathlib.Path("/run/jbd_logger.pid")
LOG_EVERY = int(os.environ.get("LOG_EVERY", "60"))     # секунд між строками
STALE_S = 180                                          # дані старіші → позначити

NODE = "deye_bms"
STATE_TOPIC = f"{NODE}/state"


def load_env():
    env, path = {}, HERE / "jbd2mqtt.env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


CFG = load_env()
MQTT_HOST = CFG.get("MQTT_HOST", os.environ.get("MQTT_HOST", "localhost"))
MQTT_PORT = int(CFG.get("MQTT_PORT", os.environ.get("MQTT_PORT", "1883")))
MQTT_USER = CFG.get("MQTT_USER", os.environ.get("MQTT_USER", ""))
MQTT_PASS = CFG.get("MQTT_PASS", os.environ.get("MQTT_PASS", ""))

CELLS = 16
COLS = (["ts", "soc", "pack_v", "current_a", "power_w", "remaining_ah",
         "real_ah", "soh", "cycles", "delta_mv", "cmin_v", "cmax_v",
         "hi_cell", "lo_cell"]
        + ["c%d" % i for i in range(1, CELLS + 1)]
        + ["t1", "t2", "t3", "t4", "temp_amb", "temp_fet",
           "charging", "discharging", "balancing", "protections"])

_latest = {"payload": None, "at": 0.0}


def on_connect(client, userdata, flags, rc):
    print("[mqtt] connected rc=%s" % rc, flush=True)
    client.subscribe(STATE_TOPIC)


def on_message(client, userdata, msg):
    try:
        _latest["payload"] = json.loads(msg.payload.decode())
        _latest["at"] = time.time()
    except Exception as e:
        print("[mqtt] bad payload: %s" % e, flush=True)


def _cells_mv(d):
    cells = d.get("cells") or []
    # cells у В → мВ
    return [round(c * 1000) for c in cells]


def write_row(d):
    new = not CSV.exists()
    mv = _cells_mv(d)
    if len(mv) == CELLS:
        hi = mv.index(max(mv)) + 1
        lo = mv.index(min(mv)) + 1
        delta = d.get("cell_delta_mv", max(mv) - min(mv))
        cmin = d.get("cell_min", min(mv) / 1000)
        cmax = d.get("cell_max", max(mv) / 1000)
    else:
        hi = lo = 0
        delta = d.get("cell_delta_mv", "")
        cmin = d.get("cell_min", "")
        cmax = d.get("cell_max", "")
        mv = mv + [""] * (CELLS - len(mv))
    temps = (d.get("temperatures") or []) + [""] * 4
    bal = d.get("balancing_cells") or []
    row = [
        datetime.datetime.now().isoformat(timespec="seconds"),
        d.get("soc", ""), d.get("voltage", ""), d.get("current", ""),
        d.get("power", ""), d.get("remaining_ah", ""), d.get("nominal_ah", ""),
        d.get("soh_percent", ""), d.get("cycles", ""), delta, cmin, cmax, hi, lo,
    ] + mv[:CELLS] + [
        temps[0], temps[1], temps[2], temps[3],
        d.get("temp_ambient", ""), d.get("temp_fet", ""),
        1 if d.get("charging") else 0, 1 if d.get("discharging") else 0,
        "|".join(str(x) for x in bal),      # напр. "1|2|8" = які комірки балансуються
        "|".join(d.get("protections") or []) or "-",
    ]
    with open(CSV, "a") as fh:
        if new:
            fh.write(",".join(COLS) + "\n")
        fh.write(",".join(str(x) for x in row) + "\n")


def run():
    if PIDFILE.exists():
        try:
            os.kill(int(PIDFILE.read_text().strip()), 0)
            sys.exit("jbd_logger вже працює")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    PIDFILE.write_text(str(os.getpid()))
    stop = {"f": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(f=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(f=True))

    client = mqtt.Client(client_id="jbd_logger")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()
    print("[logger] старт, кожні %d с → %s" % (LOG_EVERY, CSV), flush=True)

    while not stop["f"]:
        # чекаємо кратно секундам, але переривчасто
        for _ in range(LOG_EVERY):
            if stop["f"]:
                break
            time.sleep(1)
        if stop["f"]:
            break
        d = _latest["payload"]
        if not d:
            print("[logger] ще нема даних з MQTT", flush=True)
            continue
        if time.time() - _latest["at"] > STALE_S:
            print("[logger] дані застарілі (демон jbd2mqtt живий?)", flush=True)
        try:
            write_row(d)
        except Exception as e:
            print("[logger] запис впав: %s" % e, flush=True)

    client.loop_stop()
    try:
        PIDFILE.unlink()
    except FileNotFoundError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    a = ap.parse_args()
    if a.status:
        alive = PIDFILE.exists()
        print("логер:", ("працює pid %s" % PIDFILE.read_text().strip()) if alive else "не запущений")
        if CSV.exists():
            n = sum(1 for _ in open(CSV)) - 1
            import subprocess
            last = subprocess.run(["tail", "-1", str(CSV)], capture_output=True, text=True).stdout.strip()
            print("строк у %s: %d" % (CSV.name, max(0, n)))
            print("остання:", last)
        return
    if a.stop:
        if PIDFILE.exists():
            os.kill(int(PIDFILE.read_text().strip()), signal.SIGTERM)
            print("стоп надіслано")
        else:
            print("не запущений")
        return
    run()


if __name__ == "__main__":
    main()
