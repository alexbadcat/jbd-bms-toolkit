#!/usr/bin/env python3
"""jbd2mqtt — bridges one or more JBD/Jiabaida UP16S-series LiFePO4 BMS packs on
a shared RS485 bus to MQTT with Home Assistant MQTT-discovery, publishing the
per-cell detail (16 cell voltages, balance delta, SOH, real capacity, cycles,
MOSFET state, protection flags) a CAN/Pylon inverter link never exposes.

Публікує з HA-discovery те, чого інвертор по CAN не віддає ФІЗИЧНО (ідуть лише
агрегати): напруги всіх 16 комірок, дельту балансу, SOH, реальну ємність, цикли,
стан MOSFET і прапорці захисту.

Підтримує КІЛЬКА паків на одній RS485-шині (Y-розгалуження зовнішніх портів):
BMS_ADDRS=0,1 у env. Пак addr=0 — історичний вузол deye_bms (сутності HA
й історія збережені), addr=1 → deye_bms_2 і т.д. У деяких нових прошивок BMS
відповідає на будь-яку НЕнульову адресу — тому опитуй кожен пак строго за своєю
адресою (перевір сканом перед продакшеном, якщо паків більше одного).

Креди MQTT — у jbd2mqtt.env поряд зі скриптом (git-ignored), див.
jbd2mqtt.env.example. Запуск: вручну, через systemd (deploy/systemd/) або cron.
"""
import json
import os
import sys
import time

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jbd_bms import read_all                                     # noqa: E402
from balance_mode import BalanceModeFollower                     # noqa: E402
from top_spread import TopSpreadTracker                          # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    env, path = {}, os.path.join(HERE, "jbd2mqtt.env")
    if os.path.exists(path):
        for line in open(path):
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
PORT = CFG.get("SERIAL_PORT", os.environ.get("BMS_PORT", "/dev/ttyUSB0"))
ADDRS = [int(a) for a in CFG.get("BMS_ADDRS", CFG.get("BMS_ADDR", "0")).split(",")]
POLL = int(CFG.get("POLL_INTERVAL", "30"))
# Слідкувач режиму балансування (balance_mode.py) — вимкнути без правки коду: =0
BALANCE_MODE_FOLLOW = CFG.get("BALANCE_MODE_FOLLOW", "1") not in ("0", "false", "False")


def node_for(addr: int) -> str:
    # addr 0 = історичний вузол без суфікса, щоб не зламати сутності/історію HA
    return "deye_bms" if addr == 0 else "deye_bms_%d" % (addr + 1)


def device_for(addr: int) -> dict:
    node = node_for(addr)
    if addr == 0:
        return {
            "identifiers": [node],
            "name": "Батарея Deye (BMS)",
            "manufacturer": "LS Battery / JBD",
            "model": "UP16S010 · LiFePO4 16S 150Ah (BYD Blade)",
        }
    return {
        "identifiers": [node],
        "name": "Батарея Deye №%d (BMS)" % (addr + 1),
        "manufacturer": "LS Battery / JBD",
        "model": "UP16S010 rev.2 (WiFi) · LiFePO4 16S 150Ah",
    }


# (об'єкт, назва, одиниця, device_class, state_class, поле в JSON)
SENSORS = [
    ("soc", "Заряд", "%", "battery", "measurement", "soc"),
    ("voltage", "Напруга пакета", "V", "voltage", "measurement", "voltage"),
    ("current", "Струм", "A", "current", "measurement", "current"),
    ("power", "Потужність", "W", "power", "measurement", "power"),
    ("remaining_ah", "Залишок ємності", "Ah", None, "measurement", "remaining_ah"),
    ("nominal_ah", "Реальна ємність", "Ah", None, "measurement", "nominal_ah"),
    ("soh", "Здоров'я (SOH)", "%", None, "measurement", "soh_percent"),
    ("cycles", "Циклів", None, None, "total_increasing", "cycles"),
    ("cell_delta", "Дельта комірок", "mV", "voltage", "measurement", "cell_delta_mv"),
    ("cell_min", "Мінімальна комірка", "V", "voltage", "measurement", "cell_min"),
    ("cell_max", "Максимальна комірка", "V", "voltage", "measurement", "cell_max"),
    # Як бачить сама BMS (без компенсації №1 у нової): по цьому працюють її OVP/балансир
    ("cell_max_raw", "Максимальна комірка (сира, BMS)", "V", "voltage", "measurement", "cell_max_raw"),
    ("temp_ambient", "Температура середовища", "°C", "temperature", "measurement", "temp_ambient"),
    ("temp_fet", "Температура MOSFET", "°C", "temperature", "measurement", "temp_fet"),
    # "charge" (балансує лише під зарядом) / "static" (лише в спокої) / "unknown";
    # веде balance_mode.py — пасивний балансир JBD не вміє обидва режими одразу.
    ("balance_mode", "Режим балансування", None, None, None, "balance_mode"),
]
# Датчики в самому пакеті — їх чотири, серед комірок
TEMP_COUNT = 4
CELL_COUNT = 16


LWT_TOPIC = "deye_bms/daemon/availability"


def publish_discovery(client, addr: int):
    """HA сам створить пристрій і всі сутності — руками нічого додавати не треба."""
    node = node_for(addr)
    device = device_for(addr)
    state_topic = f"{node}/state"
    avail_topic = f"{node}/availability"

    def cfg_topic(kind, obj):
        return f"homeassistant/{kind}/{node}/{obj}/config"

    def base(name, obj):
        return {
            "name": name,
            "unique_id": f"{node}_{obj}",
            "state_topic": state_topic,
            # Дві умови доступності (обидві мають бути online): спільний LWT-топік
            # демона (падіння процесу → offline для ВСІХ паків, не лише addr0 —
            # аудит 2026-10-04: нова застигала «живою») + власний топік пака
            # (втрата звʼязку саме з ним → offline лише для нього).
            "availability": [{"topic": LWT_TOPIC}, {"topic": avail_topic}],
            "availability_mode": "all",
            "device": device,
        }

    for obj, name, unit, dclass, sclass, field in SENSORS:
        cfg = base(name, obj)
        cfg["value_template"] = "{{ value_json.%s }}" % field
        if unit:
            cfg["unit_of_measurement"] = unit
        if dclass:
            cfg["device_class"] = dclass
        if sclass:
            cfg["state_class"] = sclass
        client.publish(cfg_topic("sensor", obj), json.dumps(cfg, ensure_ascii=False),
                       retain=True)

    for i in range(1, CELL_COUNT + 1):
        cfg = base("Комірка %d" % i, "cell_%d" % i)
        cfg.update({
            "value_template": "{{ value_json.cells[%d] }}" % (i - 1),
            "unit_of_measurement": "V",
            "device_class": "voltage",
            "state_class": "measurement",
            "entity_category": "diagnostic",
        })
        client.publish(cfg_topic("sensor", "cell_%d" % i),
                       json.dumps(cfg, ensure_ascii=False), retain=True)

    for i in range(1, TEMP_COUNT + 1):
        cfg = base("Температура %d" % i, "temp_%d" % i)
        cfg.update({
            "value_template": "{{ value_json.temperatures[%d] }}" % (i - 1),
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
            "entity_category": "diagnostic",
        })
        client.publish(cfg_topic("sensor", "temp_%d" % i),
                       json.dumps(cfg, ensure_ascii=False), retain=True)

    for obj, name, field in (("charging", "Заряд дозволено", "charging"),
                             ("discharging", "Розряд дозволено", "discharging")):
        cfg = base(name, obj)
        cfg.update({
            "value_template": "{{ 'ON' if value_json.%s else 'OFF' }}" % field,
            "entity_category": "diagnostic",
        })
        client.publish(cfg_topic("binary_sensor", obj),
                       json.dumps(cfg, ensure_ascii=False), retain=True)

    cfg = base("Захисти BMS", "protections")
    cfg.update({
        "value_template": "{{ value_json.protection_text }}",
        "entity_category": "diagnostic",
    })
    client.publish(cfg_topic("sensor", "protections"),
                   json.dumps(cfg, ensure_ascii=False), retain=True)

    # Які комірки ЗАРАЗ зціджує балансир (порожньо = балансир спить). Раніше було
    # лише в MQTT-топіку — тепер і в HA-історії, щоб аналізувати розбаланс.
    cfg = base("Балансування комірок", "balancing")
    cfg.update({
        "value_template": "{{ value_json.balancing_cells | join(', ') if value_json.balancing_cells else '—' }}",
        "entity_category": "diagnostic",
    })
    client.publish(cfg_topic("sensor", "balancing"),
                   json.dumps(cfg, ensure_ascii=False), retain=True)

    # Метрика розбалансу «на верху» (top_spread.py) — реальний розкид на коліні
    # (cell_max >= 3.45), невидимий на полиці. top_spread_now — поточна сесія
    # (null поза сесією), top_spread_last — остання завершена (переживає ребут,
    # з диска/seed-скрипта). Комірки hi/lo і час останньої сесії — атрибутами.
    cfg = base("Розкид на верху (зараз)", "top_spread_now")
    cfg.update({
        "value_template": "{{ value_json.top_spread_now if value_json.top_spread_now is not none else none }}",
        "unit_of_measurement": "mV",
        "device_class": "voltage",
        "state_class": "measurement",
        "json_attributes_topic": state_topic,
        "json_attributes_template":
            "{{ {'hi_cell': value_json.top_spread_hi_cell, 'lo_cell': value_json.top_spread_lo_cell} | tojson }}",
    })
    client.publish(cfg_topic("sensor", "top_spread_now"),
                   json.dumps(cfg, ensure_ascii=False), retain=True)

    cfg = base("Розкид на верху (остання сесія)", "top_spread_last")
    cfg.update({
        "value_template": "{{ value_json.top_spread_last if value_json.top_spread_last is not none else none }}",
        "unit_of_measurement": "mV",
        "device_class": "voltage",
        "state_class": "measurement",
        "json_attributes_topic": state_topic,
        "json_attributes_template":
            "{{ {'hi_cell': value_json.top_spread_hi_cell, 'lo_cell': value_json.top_spread_lo_cell, "
            "'at': value_json.top_spread_last_at} | tojson }}",
    })
    client.publish(cfg_topic("sensor", "top_spread_last"),
                   json.dumps(cfg, ensure_ascii=False), retain=True)


def on_connect(client, userdata, flags, rc):
    print("[mqtt] connected rc=%s" % rc, flush=True)
    for addr in ADDRS:
        client.publish(f"{node_for(addr)}/availability", "online", retain=True)
        publish_discovery(client, addr)
    client.publish(LWT_TOPIC, "online", retain=True)


def main():
    client = mqtt.Client(client_id="deye_bms")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    # LWT можна повісити лише на один топік — маркуємо офлайн головного;
    # решта вузлів впадуть в offline самі через лічильник fails при рестарті.
    client.will_set(LWT_TOPIC, "offline", retain=True)
    client.on_connect = on_connect
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()

    balancer = BalanceModeFollower() if BALANCE_MODE_FOLLOW else None
    if balancer:
        try:
            balancer.startup(PORT, ADDRS)
        except Exception as e:                                   # noqa: BLE001
            print(time.strftime("%m-%d %H:%M:%S ") + "[balance] старт слідкувача впав: %s" % e, flush=True)

    spread = TopSpreadTracker()

    fails = {a: 0 for a in ADDRS}
    while True:
        for addr in ADDRS:
            node = node_for(addr)
            try:
                data = read_all(PORT, addr)
                # Порожній список зручніший для шаблону, ніж відсутнє поле
                data["protection_text"] = ", ".join(data["protections"]) or "немає"
                if balancer:
                    try:
                        data["balance_mode"] = balancer.step(PORT, addr, data)
                    except Exception as e:                        # noqa: BLE001
                        data["balance_mode"] = balancer.known_mode(addr)
                        print(time.strftime("%m-%d %H:%M:%S ") + "[bms %d] крок слідкувача балансу впав: %s"
                              % (addr, e), flush=True)
                else:
                    data["balance_mode"] = "unknown"
                try:
                    data.update(spread.update(addr, data))
                except Exception as e:                            # noqa: BLE001
                    print(time.strftime("%m-%d %H:%M:%S ") + "[bms %d] top_spread впав: %s" % (addr, e), flush=True)
                client.publish(f"{node}/state", json.dumps(data, ensure_ascii=False))
                client.publish(f"{node}/availability", "online", retain=True)
                if fails[addr]:
                    print(time.strftime("%m-%d %H:%M:%S ") + "[bms %d] зв'язок відновлено після %d невдач"
                          % (addr, fails[addr]), flush=True)
                fails[addr] = 0
            except Exception as e:                               # noqa: BLE001
                fails[addr] += 1
                print(time.strftime("%m-%d %H:%M:%S ") + "[bms %d] помилка читання (%d): %s"
                      % (addr, fails[addr], e), flush=True)
                # BMS засинає через 5 хв без обміну — поодинокі збої нормальні,
                # offline піднімаємо лише коли справді втратили зв'язок
                if fails[addr] >= 3:
                    client.publish(f"{node}/availability", "offline", retain=True)
            # Пауза між паками — щоб кадри на спільній шині не наїжджали
            time.sleep(1.0)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
