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

# ── адаптивний резерв (battery_eta_reserve) ──
# Погодинна історія напруги мережі (SQLite з таблицею voltage_hourly(ts_hour, avg, min)) —
# з неї рахуються реальні епізоди відключень для режиму ризику. Порожньо → лише HA-історія.
ETA_OUTAGE_DB = CFG.get("ETA_OUTAGE_DB", "")
RES_RECENT_DAYS = int(CFG.get("RES_RECENT_DAYS", "14"))         # «свіже» вікно режиму
RES_PERCENTILE = float(CFG.get("RES_PERCENTILE", "90"))
RES_PRIOR_STRENGTH = float(CFG.get("RES_PRIOR_STRENGTH", "8"))  # скільки свіжих епізодів «перевішують» минулий рік
RES_MIN_H = float(CFG.get("RES_MIN_H", "2"))
RES_MAX_H = float(CFG.get("RES_MAX_H", "24"))
RES_ATTACK_FLOOR_H = float(CFG.get("RES_ATTACK_FLOOR_H", "12"))
RES_ALERT_FLOOR_H = float(CFG.get("RES_ALERT_FLOOR_H", "0"))  # 0 = вимкнено: там, де тривога 5–20 год/добу щодня, «активна тривога» нічого не прогнозує
RES_DELAY_H = float(CFG.get("RES_DELAY_H", "0.5"))              # запізнення включення після вікна
RES_WINDOW_MARGIN = float(CFG.get("RES_WINDOW_MARGIN", "1.15"))
RES_ECO_FLOOR = int(CFG.get("RES_ECO_FLOOR", "20"))
RES_NIGHT_SOC_CALM = int(CFG.get("RES_NIGHT_SOC_CALM", "90"))
RES_CALM_RISK_H = float(CFG.get("RES_CALM_RISK_H", "3"))
RES_TOPUP_LEAD_H = float(CFG.get("RES_TOPUP_LEAD_H", "2"))
RES_NIGHT_START_H = int(CFG.get("RES_NIGHT_START_H", "23"))
RES_NIGHT_END_H = int(CFG.get("RES_NIGHT_END_H", "7"))
# сутності ескалації (порожньо — не використовувати)
RES_EMERGENCY_ENTITY = CFG.get("RES_EMERGENCY_ENTITY", "binary_sensor.outage_emergency_unified")
RES_SEEN_TODAY_ENTITY = CFG.get("RES_SEEN_TODAY_ENTITY", "")  # лише фолбек без журналу доби
RES_ALERT_ENTITY = CFG.get("RES_ALERT_ENTITY", "")
RES_OUTAGE_ENTITIES = CFG.get("RES_OUTAGE_ENTITIES", "binary_sensor.outage_definite_24h,binary_sensor.outage_possible_24h")
# масована атака: сумарна тривога за RES_ATTACK_WINDOW_H ≥ RES_ATTACK_ALARM_H → запас RES_POST_ATTACK_FLOOR_H.
# ⚠️ Поріг калібрувати під свою громаду: короткі пороги в регіонах із щоденними тривогами горять постійно.
RES_ALARM_HISTORY_ENTITY = CFG.get("RES_ALARM_HISTORY_ENTITY", "")
RES_ATTACK_ALARM_H = float(CFG.get("RES_ATTACK_ALARM_H", "0"))  # 0 = вимкнено: тривога ≠ відключення (дані 30.09–15.10: у дні 14+ год тривоги відключення були графікові, план їх і так враховує); удар по мережі ловлять ГАВ і «поза графіком»
RES_PLAN_MIN_STEP = int(CFG.get("RES_PLAN_MIN_STEP", "10"))  # гістерезис полички, %
RES_ATTACK_WINDOW_H = float(CFG.get("RES_ATTACK_WINDOW_H", "24"))
RES_POST_ATTACK_FLOOR_H = float(CFG.get("RES_POST_ATTACK_FLOOR_H", "14"))
# погода: атрибут temperature у weather.*; холод/мороз піднімають запас
RES_WEATHER_ENTITY = CFG.get("RES_WEATHER_ENTITY", "weather.pogoda_ua")
RES_COLD_TEMP = float(CFG.get("RES_COLD_TEMP", "-5"))
RES_COLD_FLOOR_H = float(CFG.get("RES_COLD_FLOOR_H", "0"))  # 0 = вимкнено: вплив холоду вже видно через довші вікна ДТЕК і статистику (подвійний облік)
RES_FROST_TEMP = float(CFG.get("RES_FROST_TEMP", "-10"))
RES_FROST_FLOOR_H = float(CFG.get("RES_FROST_FLOOR_H", "0"))
# відключення ПОЗА графіком сьогодні ≥ RES_UNSCHED_MIN хв → ескалація до атакового рівня
# (графікові — очікувані, вони вже в плані; допуск на запізнення/ранній старт — RES_SCHED_TOLERANCE_MIN)
RES_UNSCHED_MIN = float(CFG.get("RES_UNSCHED_MIN", "10"))
RES_SCHED_TOLERANCE_MIN = float(CFG.get("RES_SCHED_TOLERANCE_MIN", "30"))
