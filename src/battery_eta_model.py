#!/usr/bin/env python3
"""battery_eta_model — чисті функції прогнозу часу до розряду/повного заряду пари
LiFePO4-паків Deye SUN-5K-SG03LP1. Без мережі/IO — усе з аргументів, легко тестується.

Pure (network-free) model for "time to empty" / "time to full" of two parallel
LiFePO4 BMS packs behind a Deye hybrid inverter: a weighted (recency + seasonal)
hourly load profile learned from a growing own archive, an inverter discharge
efficiency learned from real blackout sessions, a learned CC→CV charge-taper
curve, a piecewise discharge threshold built from Deye's time-of-use program
shelves (grid on) or `shutdown_soc` (off-grid — Low SOC only warns, it does not
disconnect, verified against Deye forum reports), forward time-integration in
fixed steps, self-check (predicted-vs-actual) bookkeeping and output smoothing.

Енергія пакета = Σ(remaining_Ah × напруга) по обох BMS — правдивіше за SOC%,
бо напруга жива, а не номінальна. Навантаження в прогнозі — блендинг поточного
згладженого (EMA) з навченим погодинним профілем: спочатку довіряємо живому
значенню, через `blend_minutes` — повністю профілю. Зважування дня в архіві —
добуток м'якого recency-спаду (напівперіод, старі дні не щезають миттєво) і
сезонної близькості (дні того ж періоду року важать більше) і коефіцієнта
покриття доби (дірява/неповна доба важить менше).
"""
import math
from datetime import date, datetime, time, timedelta

# ───────────────────────── час: Київ (EU DST, без zoneinfo — py3.8 у проді) ─────────────────────────


def _last_sunday_utc_1am(year, month):
    """01:00 UTC останньої неділі місяця — момент переходу DST у ЄС (і в Україні)."""
    if month == 12:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, month + 1, 1)
    last_day = (nxt - timedelta(days=1)).replace(hour=1, minute=0, second=0, microsecond=0)
    back = (last_day.weekday() - 6) % 7  # Sunday.weekday() == 6
    return last_day - timedelta(days=back)


def kyiv_utc_offset_hours(dt_utc):
    """+3 (EEST) з останньої неділі березня 01:00 UTC по останню неділю жовтня 01:00 UTC,
    інакше +2 (EET). dt_utc — naive або aware, порівнюємо лише календарно-наївно."""
    naive = dt_utc.replace(tzinfo=None) if dt_utc.tzinfo else dt_utc
    y = naive.year
    start = _last_sunday_utc_1am(y, 3)
    end = _last_sunday_utc_1am(y, 10)
    return 3 if start <= naive < end else 2


def to_kyiv(dt_utc):
    """UTC (aware чи naive) → наївний локальний Київський datetime."""
    naive = dt_utc.replace(tzinfo=None) if dt_utc.tzinfo else dt_utc
    return naive + timedelta(hours=kyiv_utc_offset_hours(dt_utc))


def kyiv_date(dt_utc):
    return to_kyiv(dt_utc).date()


# ───────────────────────── вага дня в архіві: recency × сезон × покриття ─────────────────────────


def recency_weight(age_days, half_life_days=45.0):
    """М'який спад: старі дні не зникають миттєво, лише втрачають вагу вдвічі щопівперіоду."""
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (max(0.0, age_days) / half_life_days)


def day_of_year(d):
    return d.timetuple().tm_yday


def circular_doy_diff(doy_a, doy_b, year_len=365.25):
    diff = abs(doy_a - doy_b)
    return min(diff, year_len - diff)


def season_weight(doy_diff, sigma_days=45.0):
    """Дні того ж періоду року (той самий сезон) важать більше — гаусіан по круговій
    відстані дня року. sigma_days<=0 вимикає сезонність (усі дні року рівноважні)."""
    if sigma_days <= 0:
        return 1.0
    return math.exp(-(doy_diff ** 2) / (2.0 * sigma_days ** 2))


def day_weight(day_date, today_date, coverage=1.0, half_life_days=45.0, sigma_days=45.0, min_coverage=0.15):
    """Підсумкова вага доби в архіві для навчання. coverage < min_coverage (демон лежав,
    дірява доба) — доба повністю викидається (вага 0), а не просто применшується."""
    cov = max(0.0, min(1.0, coverage))
    if cov < min_coverage:
        return 0.0
    age_days = (today_date - day_date).days
    doy_diff = circular_doy_diff(day_of_year(day_date), day_of_year(today_date))
    return recency_weight(age_days, half_life_days) * season_weight(doy_diff, sigma_days) * cov


def effective_n_days(weights):
    """Ефективна кількість діб з урахуванням ваг (Kish effective sample size)."""
    s = sum(weights)
    if s <= 0:
        return 0.0
    s2 = sum(w * w for w in weights)
    return (s * s) / s2 if s2 > 0 else 0.0


def _fill_gaps(profile):
    """Години без даних — найближче відоме значення по колу (циклічна доба)."""
    n = len(profile)
    if all(v is None for v in profile):
        return [0.0] * n
    out = list(profile)
    for i in range(n):
        if out[i] is not None:
            continue
        for dist in range(1, n):
            found = None
            for cand in (i - dist, i + dist):
                c = cand % n
                if profile[c] is not None:
                    found = profile[c]
                    break
            if found is not None:
                out[i] = found
                break
    return out


# ───────────────────────── навчання з архіву (день-агрегатів) ─────────────────────────
# Формат одного дня в архіві (JSONL): див. docstring battery_eta.py (build_day_aggregate).


def weighted_hourly_profile(days, today_date, half_life_days=45.0, sigma_days=45.0,
                             split_min_days=10, min_coverage=0.15):
    """Навчений погодинний профіль навантаження дому (Вт), зважений recency+сезон+покриття.
    Поділ будні/вихідні вмикається автоматично, коли назбиралось достатньо діб кожного типу."""
    all_sum = [0.0] * 24
    all_w = [0.0] * 24
    wk_sum = [0.0] * 24
    wk_w = [0.0] * 24
    we_sum = [0.0] * 24
    we_w = [0.0] * 24
    weights = []
    dates = []
    wk_days = set()
    we_days = set()
    for d in days:
        dd = date.fromisoformat(d["date"])
        w = day_weight(dd, today_date, d.get("coverage", 1.0), half_life_days, sigma_days, min_coverage)
        if w <= 0:
            continue
        weights.append(w)
        dates.append(dd)
        is_weekend = dd.weekday() >= 5
        (we_days if is_weekend else wk_days).add(dd)
        hourly = d.get("hourly_load_w") or [None] * 24
        for h in range(24):
            v = hourly[h] if h < len(hourly) else None
            if v is None:
                continue
            all_sum[h] += v * w
            all_w[h] += w
            if is_weekend:
                we_sum[h] += v * w
                we_w[h] += w
            else:
                wk_sum[h] += v * w
                wk_w[h] += w
    all_profile = [(all_sum[h] / all_w[h] if all_w[h] > 0 else None) for h in range(24)]
    split = len(wk_days) >= split_min_days and len(we_days) >= max(2, split_min_days // 3)
    result = {
        "all": _fill_gaps(all_profile),
        "n_days": len(weights),
        "eff_n_days": round(effective_n_days(weights), 1),
        "oldest": min(dates).isoformat() if dates else None,
        "weekday_split": split,
        "weekday": None,
        "weekend": None,
    }
    if split:
        result["weekday"] = _fill_gaps([(wk_sum[h] / wk_w[h] if wk_w[h] > 0 else None) for h in range(24)])
        result["weekend"] = _fill_gaps([(we_sum[h] / we_w[h] if we_w[h] > 0 else None) for h in range(24)])
    return result


def profile_for(profile, is_weekend):
    if profile.get("weekday_split") and profile.get("weekday") and profile.get("weekend"):
        return profile["weekend"] if is_weekend else profile["weekday"]
    return profile["all"]


def weighted_efficiency(days, today_date, half_life_days=45.0, sigma_days=45.0, default=0.92):
    """ККД інвертора (навантаження_Вт·год / енергія_батареї_Вт·год) з реальних off-grid
    сесій розряду — ваговано по доба-вазі × енергії сесії (велика сесія надійніша)."""
    num = 0.0
    den = 0.0
    n_sessions = 0
    for d in days:
        dd = date.fromisoformat(d["date"])
        w = day_weight(dd, today_date, d.get("coverage", 1.0), half_life_days, sigma_days, 0.0)
        if w <= 0:
            continue
        for s in d.get("sessions", []):
            if not s.get("quality_ok", True):
                continue
            bb = s.get("energy_battery_wh") or 0.0
            lb = s.get("energy_load_wh") or 0.0
            if bb <= 1.0:
                continue
            ratio = lb / bb
            if not (0.5 <= ratio <= 1.3):
                continue
            sess_w = w * min(bb, 5000.0)
            num += ratio * sess_w
            den += sess_w
            n_sessions += 1
    if den <= 0:
        return default, 0
    return max(0.75, min(1.05, num / den)), n_sessions


_DEFAULT_TAPER = {80: 1.0, 85: 1.0, 90: 0.9, 92: 0.75, 94: 0.6, 96: 0.4, 98: 0.2, 99: 0.1, 100: 0.03}


def weighted_charge_taper(days, today_date, half_life_days=45.0, sigma_days=45.0):
    """Навчена крива спаду струму заряду (CC→CV «хвіст») як частка від уставки ліміту,
    по комірках SOC% — незалежна від самого значення уставки (ratio, не ампери)."""
    sums = {}
    counts = {}
    for d in days:
        dd = date.fromisoformat(d["date"])
        w = day_weight(dd, today_date, d.get("coverage", 1.0), half_life_days, sigma_days, 0.0)
        if w <= 0:
            continue
        for b_str, sc in d.get("charge_bins", {}).items():
            b = int(b_str)
            s, c = sc[0], sc[1]
            if c <= 0:
                continue
            sums[b] = sums.get(b, 0.0) + s * w
            counts[b] = counts.get(b, 0.0) + c * w
    curve = {b: sums[b] / counts[b] for b in sums if counts.get(b, 0) > 0}
    return dict(sorted(curve.items())) if curve else dict(_DEFAULT_TAPER)


def taper_ratio_at_soc(curve, soc):
    if not curve:
        return 1.0
    # після json-стану ключі приходять рядками ("95.0") — нормалізуємо, інакше float<=str падає
    curve = {float(k): float(v) for k, v in curve.items()}
    keys = sorted(curve.keys())
    if soc <= keys[0]:
        return curve[keys[0]]
    if soc >= keys[-1]:
        return curve[keys[-1]]
    for i in range(len(keys) - 1):
        a, b = keys[i], keys[i + 1]
        if a <= soc <= b:
            va, vb = curve[a], curve[b]
            if b == a:
                return va
            t = (soc - a) / (b - a)
            return va + (vb - va) * t
    return curve[keys[-1]]


# ───────────────────────── поріг розряду (полички програм / shutdown_soc) ─────────────────────────


def current_program_slot(programs, now_local):
    """Активний зараз слот програми Deye (останній, чий час <= поточного локального
    часу, із загортанням через північ на останній слот доби). None, якщо програм нема."""
    if not programs:
        return None
    progs = sorted(programs, key=lambda p: p["time"])
    active = progs[-1]
    for p in progs:
        if p["time"] <= now_local.time():
            active = p
    return active


def build_discharge_threshold_schedule(now_local, programs, grid_on, capacity_wh,
                                        shutdown_soc, horizon_min=24 * 60):
    """programs: [{"time": datetime.time, "soc": float}, ...] (6 слотів Deye, будь-який
    порядок). Є мережа → кусково-постійний поріг по поличках активного й МАЙБУТНІХ слотів
    (слот змінюється щодня в той самий час). Немає мережі → одна поличка shutdown_soc на
    весь горизонт: Low SOC лише сигналізує тривогу, апаратний обрив інвертора — на
    Shutdown SOC (перевірено по форумах Deye/powerforum.co.za — Low Batt лише будить алярм,
    інвертор продовжує віддавати до Shutdown).
    Повертає список (minute_offset, threshold_wh, label), відсортований за minute_offset."""
    if not grid_on:
        thr = capacity_wh * shutdown_soc / 100.0
        return [(0, thr, "вимкнення інвертора %d%% (немає мережі)" % round(shutdown_soc))]
    if not programs:
        return [(0, 0.0, "немає даних про програми")]
    progs = sorted(programs, key=lambda p: p["time"])
    cur_slot = current_program_slot(progs, now_local)
    segs = [(0, capacity_wh * cur_slot["soc"] / 100.0,
             "поличка програми %d%% (поточний слот)" % round(cur_slot["soc"]))]
    base_date = now_local.date()
    events = []
    for days_ahead in (0, 1, 2):
        for p in progs:
            dt_p = datetime.combine(base_date + timedelta(days=days_ahead), p["time"])
            minute = (dt_p - now_local).total_seconds() / 60.0
            if 0 < minute <= horizon_min:
                events.append((minute, p))
    events.sort(key=lambda e: e[0])
    for minute, p in events:
        segs.append((minute, capacity_wh * p["soc"] / 100.0,
                     "поличка програми %d%% о %s" % (round(p["soc"]), p["time"].strftime("%H:%M"))))
    return segs


# ───────────────────────── прогноз уперед за часом ─────────────────────────


def forecast_depletion(energy_wh, ema_load_w, hourly_profile, threshold_segments,
                        efficiency=0.92, now_local=None, step_min=5, horizon_min=24 * 60,
                        blend_minutes=180):
    """Інтегрує енергію вперед кроками step_min, доки не впаде нижче порогу з
    threshold_segments (кусково-постійна функція minute_offset→поріг). Навантаження —
    блендинг living EMA (t=0) → навчений погодинний профіль (t>=blend_minutes)."""
    segs = sorted(threshold_segments, key=lambda s: s[0])

    def threshold_at(t):
        active = segs[0]
        for s in segs:
            if s[0] <= t:
                active = s
        return active

    def profile_w_at(t):
        if not hourly_profile:
            return ema_load_w
        if now_local is None:
            return ema_load_w
        hour = (now_local + timedelta(minutes=t)).hour
        v = hourly_profile[hour] if hour < len(hourly_profile) else None
        return v if v is not None else ema_load_w

    e = energy_wh
    t = 0.0
    _, _, last_label = threshold_at(0)
    while t < horizon_min:
        thr_t, thr_wh, label = threshold_at(t)
        last_label = label
        if e <= thr_wh:
            return {"minutes": int(round(t)), "label": label, "energy_at_event_wh": round(e, 1), "reached": True}
        blend = min(1.0, t / blend_minutes) if blend_minutes > 0 else 1.0
        load = ema_load_w * (1 - blend) + profile_w_at(t) * blend
        batt_draw_w = max(0.0, load) / max(efficiency, 0.5)
        e -= batt_draw_w * (step_min / 60.0)
        t += step_min
    return {"minutes": None, "label": last_label, "energy_at_event_wh": round(e, 1), "reached": False}


def forecast_charge(energy_wh, capacity_wh, target_soc, pack_voltage_v, max_current_a,
                     taper_curve, step_min=5, horizon_min=24 * 60):
    """Вперед по часу: струм = max_current_a(уставка) × навчена крива хвоста(SOC%).
    pack_voltage_v — спільна напруга шини (обидва паки паралельно, той самий вузол)."""
    if capacity_wh <= 0 or max_current_a <= 0:
        return {"minutes": None, "energy_at_event_wh": round(energy_wh, 1), "reached": False}
    target_wh = capacity_wh * target_soc / 100.0
    e = energy_wh
    t = 0.0
    while t < horizon_min:
        if e >= target_wh:
            return {"minutes": int(round(t)), "energy_at_event_wh": round(e, 1), "reached": True}
        soc = 100.0 * e / capacity_wh
        ratio = taper_ratio_at_soc(taper_curve, soc)
        power_w = max_current_a * ratio * pack_voltage_v
        e += power_w * (step_min / 60.0)
        t += step_min
    return {"minutes": None, "energy_at_event_wh": round(e, 1), "reached": False}


# ───────────────────────── форматування ─────────────────────────


def fmt_hm(minutes):
    if minutes is None:
        return "—"
    minutes = max(0, int(round(minutes)))
    h, m = divmod(minutes, 60)
    if h == 0:
        return "%d хв" % m
    if h >= 24:
        d, h = divmod(h, 24)
        return "%d дн %d год" % (d, h)
    return "%d год %d хв" % (h, m)


def fmt_clock(now_local, minutes):
    if minutes is None or now_local is None:
        return None
    return (now_local + timedelta(minutes=minutes)).strftime("%H:%M")


def eta_text(minutes, label, now_local=None, long_horizon_min=150):
    """«X год Y хв» для короткого горизонту, «до HH:MM (причина)» коли довго,
    «≈ до HH:MM» коли горизонт вичерпано й поріг так і не досягнуто (reached=False
    обробляється викликачем окремо — сюди приходить вже конкретне minutes)."""
    if minutes is None:
        return "—"
    if minutes <= long_horizon_min:
        return fmt_hm(minutes)
    clock = fmt_clock(now_local, minutes) if now_local else None
    if clock:
        return "до %s (%s)" % (clock, label)
    return fmt_hm(minutes)


# ───────────────────────── згладжування виводу (анти-джиттер) ─────────────────────────


def smooth_display(prev_minutes, new_minutes, min_change_abs=3, min_change_rel=0.05):
    """Не смикати показане число за дрібні коливання: оновлюємо, лише якщо нове
    значення відрізняється більш ніж на min_change_abs ХВИЛИН і більш ніж на
    min_change_rel (5%) відносно. None↔число завжди оновлює (поява/зникнення прогнозу)."""
    if prev_minutes is None or new_minutes is None:
        return new_minutes
    diff = abs(new_minutes - prev_minutes)
    if diff >= min_change_abs and diff >= prev_minutes * min_change_rel:
        return new_minutes
    return prev_minutes


# ───────────────────────── самоперевірка: прогноз vs факт ─────────────────────────


def _parse_iso(s):
    return datetime.fromisoformat(s)


def start_forecast_record(now_iso, kind, predicted_minutes, meta=None):
    return {"started_at": now_iso, "kind": kind, "predicted_minutes": predicted_minutes,
            "meta": meta or {}, "resolved": False}


def finalize_forecast_records(records, now_iso, kind):
    """Закриває ВСІ невирішені записи виду kind (подія щойно сталась: мережа повернулась /
    заряд дійшов до цілі). actual_minutes = now - started_at; error/pct_error — похибка
    прогнозу відносно фактичної тривалості."""
    now_dt = _parse_iso(now_iso)
    for r in records:
        if r.get("resolved") or r.get("kind") != kind:
            continue
        actual_min = (now_dt - _parse_iso(r["started_at"])).total_seconds() / 60.0
        r["actual_minutes"] = round(actual_min, 1)
        r["resolved"] = True
        if r.get("predicted_minutes") is not None and actual_min > 0:
            r["error_minutes"] = round(actual_min - r["predicted_minutes"], 1)
            r["pct_error"] = round(abs(r["error_minutes"]) / max(actual_min, 1.0) * 100.0, 1)
    return records


def compute_mape(records, kind, last_n=20):
    rs = [r for r in records if r.get("resolved") and r.get("kind") == kind and "pct_error" in r]
    rs = rs[-last_n:]
    if not rs:
        return None
    return round(sum(r["pct_error"] for r in rs) / len(rs), 1)


def prune_records(records, keep=200):
    """Необмежено рости не можна — лишаємо останні keep записів (resolved і pending)."""
    return records[-keep:] if len(records) > keep else records
