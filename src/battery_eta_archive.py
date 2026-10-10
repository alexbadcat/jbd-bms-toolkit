#!/usr/bin/env python3
"""battery_eta_archive — власний накопичувальний архів день-агрегатів (JSONL) і
навчання кешованої моделі з нього. ТІЛЬКИ ЧИТАННЯ з HA (історія за добу).

⚠️ Чому власний архів, а не просто «тягнути історію HA щоразу»: recorder HA за
замовчуванням тримає сирі стани лише ~10 діб (purge_keep_days) — як джерело «за
весь час» на нього покладатись не можна. Цей архів росте інкрементально на 1
рядок/добу і є єдиним довгостроковим джерелом правди для навченої моделі
(погодинний профіль навантаження, ККД розряду, крива хвоста заряду).

Own incrementally-growing day-aggregate archive (JSONL) + retrain-from-archive,
because HA's recorder only keeps raw history for its purge window (~10 days by
default) — this archive is the real "since we fixed the BMS" long-term memory.
"""
import json
import os
import urllib.error
from datetime import date, datetime, timedelta, timezone

import battery_eta_model as model
from battery_eta_config import (ARCHIVE_FILE, ETA_BACKFILL_BATCH_DAYS, ETA_HALF_LIFE_DAYS,
                                 ETA_MIN_DAY_COVERAGE, ETA_SEASON_SIGMA_DAYS, ETA_STATS_MAX_DAYS,
                                 ETA_STATS_SINCE, ETA_WEEKDAY_SPLIT_MIN_DAYS, HIST_ENTITIES,
                                 BMS1, BMS2, HaError, INV)
from battery_eta_ha import ha_history_range


# ───────────────────────── ряди → сітка (допоміжне) ─────────────────────────

def _floatseries(pairs):
    out = []
    for t, v in pairs:
        if v in (None, "unknown", "unavailable"):
            continue
        try:
            out.append((t, float(v)))
        except (TypeError, ValueError):
            continue
    return out


def _boolseries(pairs):
    return [(t, v == "on") for t, v in pairs if v not in (None, "unknown", "unavailable")]


def _ffill_with_age(series, grid_times):
    """Протягнути останнє відоме значення на сітку + вік (сек) на кожній точці —
    щоб відрізнити «реально живе» від «застигле ffill через дірку» (покриття доби)."""
    out = []
    ages = []
    i = 0
    last = None
    last_t = None
    for g in grid_times:
        while i < len(series) and series[i][0] <= g:
            last = series[i][1]
            last_t = series[i][0]
            i += 1
        out.append(last)
        ages.append((g - last_t).total_seconds() if last_t else None)
    return out, ages


# ───────────────────────── побудова день-агрегату ─────────────────────────

def build_day_aggregate(day_local_date, raw):
    """raw: {entity_id: [(dt_utc,val_str), ...]} за календарну добу day_local_date (Київ).
    Формат результату — див. docstring battery_eta_model.weighted_hourly_profile/
    weighted_efficiency/weighted_charge_taper (одна доба архіву)."""
    off = model.kyiv_utc_offset_hours(datetime.combine(day_local_date, datetime.min.time()))
    start_utc = datetime.combine(day_local_date, datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=off)
    end_utc = start_utc + timedelta(days=1)

    load = _floatseries(raw.get(f"sensor.{INV}_load_power", []))
    grid = _boolseries(raw.get(f"binary_sensor.{INV}_grid", []))
    ah1 = _floatseries(raw.get(f"sensor.{BMS1}_zalishok_iemnosti", []))
    v1 = _floatseries(raw.get(f"sensor.{BMS1}_napruga_paketa", []))
    i1 = _floatseries(raw.get(f"sensor.{BMS1}_strum", []))
    ah2 = _floatseries(raw.get(f"sensor.{BMS2}_zalishok_iemnosti", []))
    v2 = _floatseries(raw.get(f"sensor.{BMS2}_napruga_paketa", []))
    i2 = _floatseries(raw.get(f"sensor.{BMS2}_strum", []))
    soc = _floatseries(raw.get(f"sensor.{INV}_battery", []))
    maxchg = _floatseries(raw.get(f"number.{INV}_battery_max_charging_current", []))

    step = timedelta(minutes=1)
    grid_times = []
    t = start_utc
    while t < end_utc:
        grid_times.append(t)
        t += step
    n = len(grid_times)

    loadf, load_age = _ffill_with_age(load, grid_times)
    gridf, _ = _ffill_with_age(grid, grid_times)
    ah1f, _ = _ffill_with_age(ah1, grid_times)
    v1f, _ = _ffill_with_age(v1, grid_times)
    i1f, _ = _ffill_with_age(i1, grid_times)
    ah2f, _ = _ffill_with_age(ah2, grid_times)
    v2f, _ = _ffill_with_age(v2, grid_times)
    i2f, _ = _ffill_with_age(i2, grid_times)
    socf, _ = _ffill_with_age(soc, grid_times)
    maxchgf, _ = _ffill_with_age(maxchg, grid_times)

    # ── погодинний профіль навантаження + покриття доби ──
    hour_sum = [0.0] * 24
    hour_cnt = [0] * 24
    fresh = 0
    for k in range(n):
        lt = grid_times[k] + timedelta(hours=off)
        age = load_age[k]
        if loadf[k] is not None and age is not None and age <= 900:
            hour_sum[lt.hour] += loadf[k]
            hour_cnt[lt.hour] += 1
            fresh += 1
    hourly = [(hour_sum[h] / hour_cnt[h] if hour_cnt[h] else None) for h in range(24)]
    coverage = fresh / n if n else 0.0

    # ── off-grid сесії розряду: ККД = load_Wh / battery_Wh (найчистіший сигнал) ──
    sessions = []
    k = 0
    while k < n:
        if not gridf[k]:
            j = k
            while j < n and not gridf[j]:
                j += 1
            load_wh = sum((loadf[x] or 0.0) for x in range(k, j)) * (1.0 / 60.0)
            a1s, a1e = ah1f[k], ah1f[j - 1]
            vv1 = [v1f[x] for x in range(k, j) if v1f[x] is not None]
            a2s, a2e = ah2f[k], ah2f[j - 1]
            vv2 = [v2f[x] for x in range(k, j) if v2f[x] is not None]
            batt_wh = 0.0
            have_batt = False
            if a1s is not None and a1e is not None and vv1:
                batt_wh += max(0.0, a1s - a1e) * (sum(vv1) / len(vv1))
                have_batt = True
            if a2s is not None and a2e is not None and vv2:
                batt_wh += max(0.0, a2s - a2e) * (sum(vv2) / len(vv2))
                have_batt = True
            dur_min = j - k
            quality_ok = have_batt and dur_min >= 5 and batt_wh > 1.0
            if dur_min >= 2:
                sessions.append({
                    "energy_load_wh": round(load_wh, 1),
                    "energy_battery_wh": round(batt_wh, 1),
                    "duration_min": dur_min,
                    "quality_ok": bool(quality_ok),
                })
            k = j
        else:
            k += 1

    # ── хвіст заряду: бінуємо ratio=струм/уставка по SOC (лише SOC>=80, явний заряд) ──
    charge_bins = {}
    for k in range(n):
        if not gridf[k] or socf[k] is None or socf[k] < 80:
            continue
        mx = maxchgf[k]
        if mx is None or mx < 1.0:
            continue
        ia = max(0.0, i1f[k]) if i1f[k] is not None else 0.0
        ib = max(0.0, i2f[k]) if i2f[k] is not None else 0.0
        total_i = ia + ib
        if total_i < 0.5:
            continue
        ratio = total_i / mx
        if ratio > 1.3:  # явний викид/перехідний момент — не довіряємо
            continue
        b = int(round(socf[k] / 2.0) * 2)
        s, c = charge_bins.get(str(b), [0.0, 0.0])
        charge_bins[str(b)] = [s + ratio, c + 1.0]

    return {
        "date": day_local_date.isoformat(),
        "coverage": round(coverage, 3),
        "hourly_load_w": [round(v, 1) if v is not None else None for v in hourly],
        "sessions": sessions,
        "charge_bins": charge_bins,
    }


# ───────────────────────── архів (JSONL, день = рядок) ─────────────────────────

def read_archive():
    if not os.path.exists(ARCHIVE_FILE):
        return []
    days = []
    with open(ARCHIVE_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                days.append(json.loads(line))
            except ValueError:
                continue
    days.sort(key=lambda d: d["date"])
    return days


def write_archive(days):
    days = sorted(days, key=lambda d: d["date"])
    tmp = ARCHIVE_FILE + ".tmp"
    with open(tmp, "w") as f:
        for d in days:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    os.replace(tmp, ARCHIVE_FILE)


def upsert_days(new_days):
    """Додає/замінює дні за датою і одразу обрізає архів до [floor, today-MAX_DAYS..today]."""
    by_date = {d["date"]: d for d in read_archive()}
    for d in new_days:
        by_date[d["date"]] = d
    floor_date = date.fromisoformat(ETA_STATS_SINCE[:10])
    today = model.kyiv_date(datetime.now(timezone.utc))
    cutoff = max(floor_date, today - timedelta(days=ETA_STATS_MAX_DAYS))
    kept = [d for d in by_date.values() if date.fromisoformat(d["date"]) >= cutoff]
    write_archive(kept)
    return kept


# ───────────────────────── бекфіл відсутніх діб ─────────────────────────

def backfill(log=print):
    """Добудовує архів днями, яких там ще нема: від floor (ETA_STATS_SINCE) до ВЧОРА
    (сьогодні — неповна доба, її в архів не кладемо; живий стан дня веде основний цикл).
    Обмежено ETA_BACKFILL_BATCH_DAYS за один виклик — решту добере наступний ретрейн."""
    floor_date = date.fromisoformat(ETA_STATS_SINCE[:10])
    today = model.kyiv_date(datetime.now(timezone.utc))
    yesterday = today - timedelta(days=1)
    existing = {d["date"] for d in read_archive()}
    need = []
    d = floor_date
    while d <= yesterday:
        if d.isoformat() not in existing:
            need.append(d)
        d += timedelta(days=1)
    need = need[:ETA_BACKFILL_BATCH_DAYS]
    if not need:
        return 0
    new_days = []
    for d in need:
        off = model.kyiv_utc_offset_hours(datetime.combine(d, datetime.min.time()))
        start_utc = datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=off)
        end_utc = start_utc + timedelta(days=1)
        try:
            raw = ha_history_range(HIST_ENTITIES, start_utc, end_utc)
            new_days.append(build_day_aggregate(d, raw))
            log("[backfill] %s готово" % d.isoformat())
        except (urllib.error.URLError, HaError, OSError, ValueError) as e:
            log("[backfill] %s впало: %s" % (d.isoformat(), e))
    if new_days:
        upsert_days(new_days)
    return len(new_days)


# ───────────────────────── ретрейн: архів → кеш моделі ─────────────────────────

def retrain(state, log=print):
    """Мутує state["model"]/state["last_trained"] на місці (стан веде викликач)."""
    backfill(log)
    days = read_archive()
    today = model.kyiv_date(datetime.now(timezone.utc))
    profile = model.weighted_hourly_profile(
        days, today, ETA_HALF_LIFE_DAYS, ETA_SEASON_SIGMA_DAYS, ETA_WEEKDAY_SPLIT_MIN_DAYS,
        ETA_MIN_DAY_COVERAGE)
    efficiency, n_eff = model.weighted_efficiency(days, today, ETA_HALF_LIFE_DAYS, ETA_SEASON_SIGMA_DAYS)
    taper = model.weighted_charge_taper(days, today, ETA_HALF_LIFE_DAYS, ETA_SEASON_SIGMA_DAYS)
    state["model"] = {"profile": profile, "efficiency": efficiency, "eff_n_sessions": n_eff, "taper": taper}
    state["last_trained"] = datetime.now(timezone.utc).isoformat()
    log("[retrain] діб=%d (ефект.=%.1f) ККД=%.3f (n=%d сесій) найстарша=%s" %
        (profile["n_days"], profile["eff_n_days"], efficiency, n_eff, profile["oldest"]))
