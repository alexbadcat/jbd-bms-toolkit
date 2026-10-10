#!/usr/bin/env python3
"""battery_eta_config — конфіг battery_eta.* (env + .env-файли поряд, шляхи, константи).
Винесено окремо, щоб і демон (battery_eta.py), і будівничий архіву (battery_eta_archive.py)
читали ОДНІ Й ТІ Ж значення, не дублюючи завантаження env.

Shared config loader + constants for the battery_eta daemon/archive-builder pair.
"""
import os


class HaError(RuntimeError):
    """Помилка походу в Home Assistant REST (HTTP/парсинг) — відрізнити від мережевих."""


HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_FILE = os.path.join(HERE, "battery_eta_archive.jsonl")
STATE_FILE = os.path.join(HERE, "battery_eta_state.json")


def _read_kv_file(path):
    out = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def load_env():
    """Пріоритет: process env > battery_eta.env (власний, опційний) > jbd2mqtt.env
    (спільний MQTT-брокер) > .env (HA-креди)."""
    env = {}
    env.update(_read_kv_file(os.path.join(HERE, ".env")))
    env.update(_read_kv_file(os.path.join(HERE, "jbd2mqtt.env")))
    env.update(_read_kv_file(os.path.join(HERE, "battery_eta.env")))
    env.update(os.environ)
    return env


CFG = load_env()

HA_URL = CFG.get("HA_URL", "").strip().rstrip("/")
HA_TOKEN = CFG.get("HA_TOKEN", "").strip()

MQTT_HOST = CFG.get("MQTT_HOST", "localhost")
MQTT_PORT = int(CFG.get("MQTT_PORT", "1883"))
MQTT_USER = CFG.get("MQTT_USER", "")
MQTT_PASS = CFG.get("MQTT_PASS", "")

INV = CFG.get("ETA_INV_PREFIX", "inverter_deye")
BMS1 = CFG.get("ETA_BMS_PREFIX_1", "batareia_deye_bms")
BMS2 = CFG.get("ETA_BMS_PREFIX_2", "batareia_deye_no2_bms")

# Межа знизу для архіву — раніше цієї дати статистика отруєна (лікування BMS№2: ліміт
# розряду 0 А 01-04.10; старий SOC недораховував ~6 Аг до вирівнювання 05.10 15:55 Київ).
ETA_STATS_SINCE = CFG.get("ETA_STATS_SINCE", "2026-10-05T12:55:00+00:00")
ETA_STATS_MAX_DAYS = int(CFG.get("ETA_STATS_MAX_DAYS", "365"))
ETA_HALF_LIFE_DAYS = float(CFG.get("ETA_HALF_LIFE_DAYS", "45"))
ETA_SEASON_SIGMA_DAYS = float(CFG.get("ETA_SEASON_SIGMA_DAYS", "45"))
ETA_WEEKDAY_SPLIT_MIN_DAYS = int(CFG.get("ETA_WEEKDAY_SPLIT_MIN_DAYS", "10"))
ETA_RETRAIN_HOURS = float(CFG.get("ETA_RETRAIN_HOURS", "6"))
ETA_POLL_SECONDS = int(CFG.get("ETA_POLL_SECONDS", "45"))
ETA_EMA_TAU_MIN = float(CFG.get("ETA_EMA_TAU_MIN", "4"))
ETA_BACKFILL_BATCH_DAYS = int(CFG.get("ETA_BACKFILL_BATCH_DAYS", "21"))
ETA_MIN_DAY_COVERAGE = float(CFG.get("ETA_MIN_DAY_COVERAGE", "0.15"))

NODE = "battery_eta"
AVAIL_TOPIC = f"{NODE}/availability"
STATE_TOPIC = f"{NODE}/state"

HIST_ENTITIES = [
    f"sensor.{INV}_load_power", f"binary_sensor.{INV}_grid",
    f"sensor.{BMS1}_zalishok_iemnosti", f"sensor.{BMS1}_napruga_paketa", f"sensor.{BMS1}_strum",
    f"sensor.{BMS2}_zalishok_iemnosti", f"sensor.{BMS2}_napruga_paketa", f"sensor.{BMS2}_strum",
    f"sensor.{INV}_battery", f"number.{INV}_battery_max_charging_current",
]
