#!/usr/bin/env python3
"""battery_eta — демон прогнозу «скільки лишилось на батареях» для Deye SUN-5K-SG03LP1
+ 2 паралельних LiFePO4 16S паки (BMS по CAN+RS485). ТІЛЬКИ ЧИТАННЯ: ходить у HA REST
по стан/історію, нічого не пише ні в інвертор, ні в BMS — публікує готовий прогноз назад
у HA через MQTT-discovery (новий вузол `battery_eta`, існуючі сенсори не чіпає).

Battery time-to-empty / time-to-full daemon for a Deye hybrid inverter with two parallel
LiFePO4 packs. Read-only against the inverter/BMS: it only polls Home Assistant's REST API
(current states; day-by-day history is handled by battery_eta_archive.py) and republishes
a learned forecast back into HA over MQTT discovery. All the actual math (weighted hourly
load profile, discharge efficiency, charge-taper curve, forward time-integration,
self-check bookkeeping) lives in the pure, unit-tested `battery_eta_model.py`.

Чому стара реалізація (template-сенсори sensor.chas_do_rozriadu/povnogo_zariadu) брехала:
- енергія пакета бралась як 7400 Вт·год — УДВІЧІ менше реальних ~15200 (два паки);
- поріг розряду завжди 10% — ігнорував, що Deye тримає батарею на поличці ПОТОЧНОГО
  слоту програми (часто 55-90%), поки є мережа, а не садить у нуль;
- навантаження — середнє за 1 хв (statistics mean) без жодного зв'язку з профілем доби;
- заряд рахувався лінійно, хоча останні ~5% йдуть у рази повільніше (CC→CV хвіст).

Модель (деталі й формули — battery_eta_model.py, архів — battery_eta_archive.py):
  • Енергія зараз = Σ(remaining_Ah × жива напруга) по обох BMS.
  • Ємність зараз = Σ(realna_iemnist_Ah, з урахуванням SOH) × середня напруга, з фолбеком
    на номінальні sensor.inverter_deye_battery_capacity (кВт·год), якщо BMS-міст лежить.
  • Поріг розряду: Є МЕРЕЖА → поличка АКТИВНОГО і майбутніх слотів програми Deye.
    НЕМАЄ МЕРЕЖІ → battery_shutdown_soc (Low SOC лише піднімає алярм — апаратний обрив
    виходу перевірено по форумах Deye саме на Shutdown SOC, не на Low).
  • Навантаження в прогнозі — блендинг живого EMA → навченого погодинного профілю з
    ВЛАСНОГО накопичувального архіву (growing JSONL, не HA-recorder — той чистить ~10 діб).
  • Самоперевірка: прогноз «спорожніє через N хв» (момент втрати мережі) звіряється з
    фактом при поверненні мережі (і так само для заряду) — MAPE в атрибутах.

Креди: HA_URL/HA_TOKEN з env або .env поряд, MQTT — з
jbd2mqtt.env (той самий брокер, що й BMS-міст) — див. battery_eta_config.py. Публічний
репо jbd-bms-toolkit синкає цей файл — ніяких приватних IP/шляхів у коді, лише в
.env-файлах (git-ignored).

Запуск: вручну, через battery_eta_start.sh (pidfile) або watchdog-registry (лід додає).
"""
import json
import os
import sys
import time
import urllib.error
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import battery_eta_model as model                                 # noqa: E402
import battery_eta_outage as outage                               # noqa: E402
from battery_eta_archive import backfill, retrain                 # noqa: E402,F401
from battery_eta_config import (AVAIL_TOPIC, BMS1, BMS2, HaError, INV, MQTT_HOST, MQTT_PASS,  # noqa: E402
                                 MQTT_PORT, MQTT_USER, NODE, STATE_FILE, STATE_TOPIC,
                                 ETA_EMA_TAU_MIN, ETA_POLL_SECONDS, ETA_RETRAIN_HOURS,
                                 HA_TOKEN, HA_URL)
from battery_eta_ha import ha_states_all                          # noqa: E402

__all__ = ["backfill"]  # ре-експорт для ручних прогонів (python3 -c "import battery_eta as b; b.backfill()")


# ───────────────────────── MQTT discovery ─────────────────────────

def device():
    return {
        "identifiers": [NODE],
        "name": "Прогноз батареї (ETA)",
        "manufacturer": "Власний (Deye + 2×LiFePO4 BMS)",
        "model": "battery_eta.py",
    }


def cfg_topic(kind, obj):
    return f"homeassistant/{kind}/{NODE}/{obj}/config"


def base_cfg(name, obj):
    return {
        "name": name,
        "unique_id": f"{NODE}_{obj}",
        "state_topic": STATE_TOPIC,
        "availability": [{"topic": AVAIL_TOPIC}],
        "device": device(),
    }


def publish_discovery(client):
    specs = [
        ("time_to_empty_min", "Батарея: вистачить на", "min", "duration", "time_to_empty_min"),
        ("time_to_full_min", "До повного заряду", "min", "duration", "time_to_full_min"),
    ]
    for obj, name, unit, dclass, field in specs:
        cfg = base_cfg(name, obj)
        cfg.update({
            "value_template": "{{ value_json.%s }}" % field,
            "unit_of_measurement": unit, "device_class": dclass, "state_class": "measurement",
        })
        client.publish(cfg_topic("sensor", obj), json.dumps(cfg, ensure_ascii=False), retain=True)

    for obj, name, field, attrs_from in (
            ("time_to_empty_text", "Батарея: заряду лишилось", "time_to_empty_text", "empty"),
            ("time_to_full_text", "Батарея: до повного заряду", "time_to_full_text", "full")):
        cfg = base_cfg(name, obj)
        cfg.update({
            "value_template": "{{ value_json.%s }}" % field,
            "json_attributes_topic": STATE_TOPIC,
            "json_attributes_template": "{{ value_json.%s_attrs | tojson }}" % attrs_from,
        })
        client.publish(cfg_topic("sensor", obj), json.dumps(cfg, ensure_ascii=False), retain=True)


# ───────────────────────── живий стан з HA ─────────────────────────

def _num(states, eid):
    s = states.get(eid)
    if not s:
        return None
    v = s.get("state")
    if v in (None, "unknown", "unavailable"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bool(states, eid, default=True):
    s = states.get(eid)
    if not s or s.get("state") in (None, "unknown", "unavailable"):
        return default
    return s.get("state") == "on"


def _parse_hms(s):
    from datetime import time as dtime
    h, m, sec = (s.split(":") + ["0", "0", "0"])[:3]
    return dtime(int(h), int(m), int(sec))


def read_programs(states):
    out = []
    for n in range(1, 7):
        t = states.get(f"time.{INV}_program_{n}_time", {}).get("state")
        soc = _num(states, f"number.{INV}_program_{n}_soc")
        if t and soc is not None:
            out.append({"time": _parse_hms(t), "soc": soc})
    return out


def read_live(states):
    """Витягає з bulk /api/states усе потрібне живому циклу одним проходом."""
    grid_on = _bool(states, f"binary_sensor.{INV}_grid", True)
    ah1, v1, i1 = (_num(states, f"sensor.{BMS1}_zalishok_iemnosti"),
                   _num(states, f"sensor.{BMS1}_napruga_paketa"), _num(states, f"sensor.{BMS1}_strum"))
    ah2, v2, i2 = (_num(states, f"sensor.{BMS2}_zalishok_iemnosti"),
                   _num(states, f"sensor.{BMS2}_napruga_paketa"), _num(states, f"sensor.{BMS2}_strum"))
    nom1 = _num(states, f"sensor.{BMS1}_realna_iemnist")
    nom2 = _num(states, f"sensor.{BMS2}_realna_iemnist")
    load_w = _num(states, f"sensor.{INV}_load_power") or 0.0
    shutdown_soc = _num(states, f"number.{INV}_battery_shutdown_soc") or 10.0
    max_current = _num(states, f"number.{INV}_battery_max_charging_current") or 0.0
    nameplate_kwh = _num(states, f"sensor.{INV}_battery_capacity")

    volts = [v for v in (v1, v2) if v is not None]
    avg_v = sum(volts) / len(volts) if volts else 52.0
    energy_wh = 0.0
    have_energy = False
    if ah1 is not None and v1 is not None:
        energy_wh += ah1 * v1
        have_energy = True
    if ah2 is not None and v2 is not None:
        energy_wh += ah2 * v2
        have_energy = True

    cap_wh = None
    if nom1 is not None or nom2 is not None:
        cap_wh = sum(v for v in (nom1, nom2) if v is not None) * avg_v
    if not cap_wh and nameplate_kwh is not None:
        cap_wh = nameplate_kwh * 1000.0
        if not have_energy:
            soc_inv = _num(states, f"sensor.{INV}_battery")
            if soc_inv is not None:
                energy_wh = cap_wh * soc_inv / 100.0
                have_energy = True

    charge_a = max(0.0, i1 or 0.0) + max(0.0, i2 or 0.0)
    outage = states.get("binary_sensor.outage_definite_24h", {})
    outage_windows = (outage.get("attributes", {}) or {}).get("windows")
    outage_on = outage.get("state") == "on"

    return {
        "grid_on": grid_on, "energy_wh": energy_wh, "cap_wh": cap_wh or 0.0, "avg_v": avg_v,
        "load_w": load_w, "shutdown_soc": shutdown_soc, "max_current_a": max_current, "charge_a": charge_a,
        "outage_windows": outage_windows, "outage_on": outage_on,
    }


# ───────────────────────── стан демона (EMA, кеш моделі, self-check) ─────────────────────────

def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def _save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


# ───────────────────────── один тік живого циклу ─────────────────────────

def tick(state, client, log=print):
    states_list = ha_states_all()
    states = {s["entity_id"]: s for s in states_list}
    live = read_live(states)
    programs = read_programs(states)

    now_utc = datetime.now(timezone.utc)
    now_local = model.to_kyiv(now_utc)
    is_weekend = now_local.weekday() >= 5

    # EMA живого навантаження — щоб перший час після зміни режиму не стрибав на холодний профіль
    prev_ema = state.get("ema_load_w")
    last_tick = state.get("last_tick_utc")
    dt_s = (now_utc - datetime.fromisoformat(last_tick)).total_seconds() if last_tick else ETA_POLL_SECONDS
    dt_s = max(1.0, min(dt_s, 3600.0))
    alpha = 1.0 - pow(2.718281828, -dt_s / (ETA_EMA_TAU_MIN * 60.0))
    ema = live["load_w"] if prev_ema is None else prev_ema + alpha * (live["load_w"] - prev_ema)
    state["ema_load_w"] = ema
    state["last_tick_utc"] = now_utc.isoformat()

    mdl = state.get("model") or {}
    profile = mdl.get("profile") or {"all": None, "weekday_split": False, "weekday": None, "weekend": None,
                                      "n_days": 0, "eff_n_days": 0, "oldest": None}
    hourly = model.profile_for(profile, is_weekend) if profile.get("all") else None
    efficiency = mdl.get("efficiency", 0.92)
    taper = mdl.get("taper") or {}

    segs = model.build_discharge_threshold_schedule(
        now_local, programs, live["grid_on"], live["cap_wh"], live["shutdown_soc"])
    dep = model.forecast_depletion(live["energy_wh"], ema, hourly, segs,
                                    efficiency=efficiency, now_local=now_local)

    # Прогноз «а якщо світло зникне зараз»: увесь дім із батареї до порогу вимкнення інвертора.
    segs_off = model.build_discharge_threshold_schedule(
        now_local, programs, False, live["cap_wh"], live["shutdown_soc"])
    dep_off = model.forecast_depletion(live["energy_wh"], ema, hourly, segs_off,
                                        efficiency=efficiency, now_local=now_local)
    # Мережа є, а батарея вже на поличці слоту (або нижче) — вона НЕ розряджається, дім живить
    # мережа. «Вистачить на 0 хв» тут брехня; корисна цифра — резерв на випадок відключення.
    standby = live["grid_on"] and dep["reached"] and (dep["minutes"] or 0) <= 5
    if standby:
        dep_show = dict(dep_off, label="резерв, якщо зникне світло")
    else:
        dep_show = dep

    cur_slot = model.current_program_slot(programs, now_local)
    target_soc = cur_slot["soc"] if cur_slot else 100.0
    chg = model.forecast_charge(live["energy_wh"], live["cap_wh"], target_soc, live["avg_v"],
                                 live["max_current_a"], taper)

    # ── згладжування виводу (анти-джиттер) ──
    disp = state.setdefault("display", {})
    if disp.get("standby") != standby:      # змінився сенс цифри — не згладжувати між режимами
        disp.pop("empty_min", None)
    disp["standby"] = standby
    empty_min = model.smooth_display(disp.get("empty_min"), dep_show["minutes"])
    full_min = model.smooth_display(disp.get("full_min"), chg["minutes"])
    disp["empty_min"] = empty_min
    disp["full_min"] = full_min

    # ── самоперевірка: мережа зникла/повернулась, заряд почався/дійшов до цілі ──
    records = state.setdefault("records", [])
    prev_grid = state.get("prev_grid_on")
    if prev_grid is True and live["grid_on"] is False:
        records.append(model.start_forecast_record(now_utc.isoformat(), "empty", dep["minutes"]))
    if prev_grid is False and live["grid_on"] is True:
        model.finalize_forecast_records(records, now_utc.isoformat(), "empty")
    state["prev_grid_on"] = live["grid_on"]

    was_charging = state.get("prev_charging", False)
    is_charging = live["charge_a"] > 1.0 and live["grid_on"]
    if (not was_charging) and is_charging:
        records.append(model.start_forecast_record(now_utc.isoformat(), "full", chg["minutes"]))
    if was_charging and not is_charging:
        model.finalize_forecast_records(records, now_utc.isoformat(), "full")
    state["prev_charging"] = is_charging
    state["records"] = model.prune_records(records, 200)

    mape_empty = model.compute_mape(state["records"], "empty")
    mape_full = model.compute_mape(state["records"], "full")

    # ── запас/дефіцит до кінця запланованого (DTEK) вікна відключення ──
    # Рахуємо від резерву БЕЗ мережі: якщо відключення вже йде — до кінця блоку, якщо попереду —
    # на всю тривалість блоку (батарея до його початку не розряджається нижче полички).
    outage_margin = None
    block = outage.next_outage_block(live["outage_windows"], now_local) if live["outage_on"] else None
    if block and dep_off["minutes"] is not None:
        b_start, b_end = block
        need_min = (b_end - max(b_start, now_local)).total_seconds() / 60.0
        outage_margin = round(dep_off["minutes"] - need_min, 1)
    elif block and dep_off["minutes"] is None:
        outage_margin = 24 * 60.0                              # горизонт 24 год не вичерпано

    if standby:
        empty_text = "резерв " + model.fmt_hm(empty_min) if empty_min is not None else "резерв > 24 год"
    else:
        empty_text = model.eta_text(empty_min, dep_show["label"], now_local)
    # на верху (≥99.5% цілі, струм уже не тече) — «заряджено», а не залишкові «5 хв»
    at_target = live["cap_wh"] > 0 and live["energy_wh"] >= live["cap_wh"] * target_soc / 100.0 * 0.995
    if at_target and live["charge_a"] < 1.0:
        full_min = 0.0
        full_text = "заряджено"
    else:
        full_text = model.eta_text(full_min, "ціль %d%%" % round(target_soc), now_local)

    payload = {
        "time_to_empty_min": round(empty_min, 1) if empty_min is not None else None,
        "time_to_full_min": round(full_min, 1) if full_min is not None else None,
        "time_to_empty_text": empty_text,
        "time_to_full_text": full_text,
        "empty_attrs": {
            "до_часу": model.fmt_clock(now_local, empty_min),
            "режим": "резерв (мережа є, батарея на поличці)" if standby else (
                "розряд до полички (мережа є)" if live["grid_on"] else "відключення: розряд до вимкнення"),
            "причина_порогу": dep_show["label"],
            "поріг_досягнуто_в_моделі": dep_show["reached"],
            "резерв_без_мережі_хв": dep_off["minutes"],
            "відключення_дтек": ("%s–%s" % (block[0].strftime("%d.%m %H:%M"), block[1].strftime("%H:%M"))) if block else None,
            "навантаження_зараз_вт": round(live["load_w"], 1),
            "навантаження_ema_вт": round(ema, 1),
            "джерело": "EMA→профіль" if hourly else "лише EMA (модель ще не навчена)",
            "ккд_інвертора": round(efficiency, 3),
            "є_мережа": live["grid_on"],
            "вистачить_до_кінця_відключення_дтек": (
                None if outage_margin is None else ("так" if outage_margin >= 0 else "ні")),
            "запас_дефіцит_хв": outage_margin,
            "модель_діб": profile.get("n_days"),
            "модель_ефективних_діб": profile.get("eff_n_days"),
            "модель_найстарша_доба": profile.get("oldest"),
            "модель_навчена": state.get("last_trained"),
            "поділ_будні_вихідні": profile.get("weekday_split"),
            "самоперевірка_mape_%": mape_empty,
        },
        "full_attrs": {
            "до_часу": model.fmt_clock(now_local, full_min),
            "ціль_soc_%": target_soc,
            "струм_заряду_а": round(live["charge_a"], 1),
            "ліміт_струму_а": live["max_current_a"],
            "самоперевірка_mape_%": mape_full,
            "модель_навчена": state.get("last_trained"),
        },
    }
    client.publish(STATE_TOPIC, json.dumps(payload, ensure_ascii=False))
    client.publish(AVAIL_TOPIC, "online", retain=True)
    log("[tick] empty=%s full=%s grid=%s load=%.0fВт energy=%.0fВтг cap=%.0fВтг" %
        (empty_text, full_text, live["grid_on"], live["load_w"], live["energy_wh"], live["cap_wh"]))


# ───────────────────────── main ─────────────────────────

def _model_is_stale(state):
    last_trained = state.get("last_trained")
    if not last_trained:
        return True
    age_s = (datetime.now(timezone.utc) - datetime.fromisoformat(last_trained)).total_seconds()
    return age_s > ETA_RETRAIN_HOURS * 3600.0


def main():
    if not HA_URL or not HA_TOKEN:
        raise SystemExit("Задай HA_URL/HA_TOKEN (env або .env поряд зі скриптом).")

    state = _load_state()
    client = mqtt.Client(client_id="battery_eta")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(AVAIL_TOPIC, "offline", retain=True)

    def on_connect(c, userdata, flags, rc):
        print("[mqtt] connected rc=%s" % rc, flush=True)
        publish_discovery(c)
        c.publish(AVAIL_TOPIC, "online", retain=True)

    client.on_connect = on_connect
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()

    if _model_is_stale(state):
        try:
            retrain(state)
        except (urllib.error.URLError, HaError, OSError, ValueError) as e:
            print(time.strftime("%m-%d %H:%M:%S ") + "[retrain] старт впав: %s" % e, flush=True)
    _save_state(state)

    fails = 0
    while True:
        try:
            tick(state, client)
            fails = 0
        except Exception as e:                                   # noqa: BLE001
            fails += 1
            print(time.strftime("%m-%d %H:%M:%S ") + "[tick] помилка (%d): %s" % (fails, e), flush=True)
            if fails >= 3:
                client.publish(AVAIL_TOPIC, "offline", retain=True)
        if _model_is_stale(state):
            try:
                retrain(state)
            except (urllib.error.URLError, HaError, OSError, ValueError) as e:
                print(time.strftime("%m-%d %H:%M:%S ") + "[retrain] впав: %s" % e, flush=True)
        _save_state(state)
        time.sleep(ETA_POLL_SECONDS)


if __name__ == "__main__":
    main()
