#!/usr/bin/env python3
"""battery_eta_reserve — adaptive battery reserve planner: decides how much charge to keep
in each inverter time-of-use slot from the outage regime actually observed (rolling
percentiles of real outage durations, blended with the same season last year), the
published outage schedule, escalation signals (emergency outages, air-raid alert) and the
learned household load profile — instead of a fixed «100% all day» emergency profile.

Адаптивний резерв батареї. Замість «є відключення в добі → 100% цілодобово» рахуємо,
скільки енергії реально треба тримати, і коли:

  1. РЕЖИМ РИЗИКУ (години автономії F, які треба мати «на випадок» у будь-який момент):
     P90 тривалості реальних відключень за останні 14 діб, змішаний із P90 того ж сезону
     минулого року (вага минулого року тим більша, чим менше свіжих епізодів). Так схема сама
     переходить між «спокійним» (F≈2 год), «графіковим» (4–8 год) і «атаковим» (15+ год)
     режимами — дані 2025/26: лист–груд P90 5–8 год, кінець січня–лютий P90 9–15 (макс 41).
  2. ЕСКАЛАЦІЯ: аварійні відключення (ГАВ) / прапорець «сьогодні вже вимикало надовго» /
     активна повітряна тривога — піднімають F до «атакового» рівня.
  3. ГРАФІК: для кожного вікна відключення ДТЕК до наступного дешевого (нічного) заряду
     потрібна енергія на всю тривалість вікна + запізнення включення, за профілем навантаження.
  4. Поличка слоту програми = max(ризиковий резерв, потреба найближчих вікон) + поріг
     вимкнення інвертора, округлено вгору до 5%, у межах [eco_floor, 100].
  5. Ніч (дешевий тариф) — заряд до 100%, якщо попереду є хоч якийсь ризик, інакше до
     night_soc_calm. Денний дозаряд від мережі (дорогий тариф) — лише якщо до найближчого
     вікна резерву не вистачить.

Чисті функції без мережі/IO — тестуються окремо (tests/test_battery_eta_reserve.py).
"""
from datetime import datetime, timedelta
import math


# ───────────────────────── статистика відключень ─────────────────────────

def percentile(values, p):
    """Найближчий ранг (без інтерполяції) — стабільно на малих вибірках."""
    v = sorted(values)
    if not v:
        return None
    i = min(len(v) - 1, max(0, int(math.ceil(p / 100.0 * len(v))) - 1))
    return v[i]


def episodes_from_hourly(rows, off_avg_v=100.0, part_min_v=50.0):
    """rows: [(datetime_local, avg_v, min_v)] погодинно, по зростанню часу.
    Година без світла: avg < off_avg_v (повністю) або min < part_min_v (частково).
    Повертає [(start_local, hours)] суцільних епізодів."""
    eps, start, prev = [], None, None
    for ts, avg_v, min_v in rows:
        off = avg_v < off_avg_v or min_v < part_min_v
        if prev is not None and start is not None and (ts - prev) > timedelta(hours=1, minutes=5):
            eps.append((start, (prev - start).total_seconds() / 3600.0 + 1.0))   # дірка в даних
            start = None
        if off and start is None:
            start = ts
        elif not off and start is not None:
            eps.append((start, (ts - start).total_seconds() / 3600.0))
            start = None
        prev = ts
    if start is not None and prev is not None:
        eps.append((start, (prev - start).total_seconds() / 3600.0 + 1.0))
    return eps


def risk_hours(episodes, now_local, recent_days=14, season_window_days=30,
               p=90, prior_strength=8.0, min_h=2.0, max_h=24.0):
    """Години автономії F, які варто мати в запасі «будь-коли».
    Свіжий P90 (останні recent_days) змішується з P90 того ж сезону рік тому
    (±season_window_days) з вагою recent_n / (recent_n + prior_strength)."""
    recent = [h for s, h in episodes if now_local - timedelta(days=recent_days) <= s < now_local]
    ly_center = now_local - timedelta(days=365)
    last_year = [h for s, h in episodes
                 if abs((s - ly_center).total_seconds()) <= season_window_days * 86400]
    p_recent = percentile(recent, p)
    p_ly = percentile(last_year, p)
    if p_recent is None and p_ly is None:
        f, w = min_h, 0.0
    elif p_ly is None:
        f, w = p_recent, 1.0
    elif p_recent is None:
        f, w = p_ly, 0.0
    else:
        w = len(recent) / (len(recent) + prior_strength)
        f = w * p_recent + (1.0 - w) * p_ly
    return {
        "hours": max(min_h, min(max_h, f)),
        "recent_n": len(recent), "recent_p": p_recent,
        "last_year_n": len(last_year), "last_year_p": p_ly,
        "weight_recent": round(w, 2),
        "attack_level_h": max([h for h in last_year] + [h for h in recent] + [0.0]),
    }


def alarm_hours(changes, now, window_h=24.0):
    """changes: [(datetime, 'on'|'off')] по зростанню часу. Сумарні години тривоги за останні window_h."""
    start = now - timedelta(hours=window_h)
    changes = [(t, st) for t, st in changes if t <= now]        # майбутнє відносно now не рахуємо
    total, on_since, state = 0.0, None, None
    for t, st in changes:
        if t < start:
            state = st
            continue
        if on_since is None and state == "on":
            on_since = start
        if st == "on" and on_since is None:
            on_since = t
        elif st != "on" and on_since is not None:
            total += (t - on_since).total_seconds() / 3600.0
            on_since = None
        state = st
    if on_since is None and state == "on" and not any(t >= start for t, _ in changes):
        on_since = start
    if on_since is not None:
        total += (now - on_since).total_seconds() / 3600.0
    return total


def unscheduled_minutes(off_periods, blocks, now, tolerance_min=30):
    """Хвилини БЕЗ світла поза графіковими вікнами (±tolerance_min на запізнення/ранній старт).
    off_periods: [(start, end|None)] — None = триває; blocks: [(start, end)] вікон ДТЕК.
    Графікове відключення — очікувана подія (вже в плані), тривожне — те, що поза графіком."""
    tol = timedelta(minutes=tolerance_min)
    total = 0.0
    for st, en in off_periods:
        en = en or now
        if en <= st:
            continue
        # віднімаємо перетини з розширеними вікнами, крок 1 хв (періоди короткі, точність достатня)
        t = st
        while t < en:
            step = min(timedelta(minutes=1), en - t)
            mid = t + step / 2
            if not any(b0 - tol <= mid < b1 + tol for b0, b1 in blocks):
                total += step.total_seconds() / 60.0
            t += step
    return total


def escalate(f_hours, attack_level_h, emergency_on=False, seen_long_today=False, alert_on=False,
             attack_floor_h=12.0, alert_floor_h=8.0, post_attack=False, post_attack_floor_h=14.0,
             outdoor_temp=None, cold_temp=-5.0, cold_floor_h=6.0, frost_temp=-10.0, frost_floor_h=8.0):
    """Ескалація F: аварійні (ГАВ) або «сьогодні вже довго вимикало» → щонайменше атаковий
    рівень (max(attack_floor_h, найдовший епізод сезону, але ≤24)); масована атака за останню
    добу (довга тривога) → post_attack_floor_h (ГАВ без графіка 14–16 год у перші 24–48 год);
    активна тривога → alert_floor_h; мороз → cold/frost_floor_h. Повертає (hours, reason)."""
    reason = "статистика відключень"
    h = f_hours
    if outdoor_temp is not None:
        if outdoor_temp <= frost_temp and frost_floor_h > h:
            h, reason = frost_floor_h, "мороз %.0f °C" % outdoor_temp
        elif outdoor_temp <= cold_temp and cold_floor_h > h:
            h, reason = cold_floor_h, "холод %.0f °C" % outdoor_temp
    if post_attack and post_attack_floor_h > h:
        h, reason = post_attack_floor_h, "після масованої атаки"
    if emergency_on or seen_long_today:
        lvl = min(24.0, max(attack_floor_h, attack_level_h))
        if lvl > h:
            h, reason = lvl, ("аварійні відключення" if emergency_on else "сьогодні вимикало поза графіком")
    if alert_on and alert_floor_h > h:
        h, reason = alert_floor_h, "повітряна тривога"
    return h, reason


# ───────────────────────── енергія на інтервал за профілем ─────────────────────────

def energy_for(start_local, hours, hourly_profile, fallback_w, efficiency, step_min=15):
    """Вт·год з БАТАРЕЇ (з урахуванням ККД інвертора), щоб живити дім `hours` годин від
    start_local за погодинним профілем навантаження."""
    if hours <= 0:
        return 0.0
    eff = max(efficiency or 0.9, 0.5)
    total, t, end = 0.0, start_local, start_local + timedelta(hours=hours)
    while t < end:
        dt_h = min(step_min / 60.0, (end - t).total_seconds() / 3600.0)
        w = None
        if hourly_profile:
            w = hourly_profile[t.hour] if t.hour < len(hourly_profile) else None
        total += (w if w is not None else fallback_w) * dt_h
        t += timedelta(minutes=step_min)
    return total / eff


# ───────────────────────── план поличок на слоти ─────────────────────────

def _slot_bounds(programs, day_start):
    """6 слотів Deye у порядку часу → [(slot_index, start_dt, end_dt)] на добу від day_start."""
    progs = sorted(enumerate(programs), key=lambda ip: ip[1]["time"])
    out = []
    for k, (idx, p) in enumerate(progs):
        st = datetime.combine(day_start.date(), p["time"])
        nxt = progs[(k + 1) % len(progs)][1]["time"]
        en = datetime.combine(day_start.date(), nxt)
        if en <= st:
            en += timedelta(days=1)
        out.append((idx, st, en))
    return out


def is_cheap(t, night_start_h=23, night_end_h=7):
    return t.hour >= night_start_h or t.hour < night_end_h


def next_cheap_start(t, night_start_h=23, night_end_h=7):
    if is_cheap(t, night_start_h, night_end_h):
        return t
    c = t.replace(hour=night_start_h, minute=0, second=0, microsecond=0)
    return c if c > t else c + timedelta(days=1)


def plan(now_local, programs, cap_wh, energy_wh, shutdown_soc, outage_blocks, risk_h, risk_reason,
         hourly_profile, fallback_w, efficiency, delay_h=0.5, window_margin=1.15,
         eco_floor=20, night_soc_calm=90, calm_risk_h=3.0, topup_lead_h=3.0,
         night_start_h=23, night_end_h=7):
    """План поличок на 6 слотів програми.
    outage_blocks: [(start_local, end_local)] запланованих відключень (ДТЕК), уже злиті.
    Повертає {"slots": [{"index","time","soc","grid","why"}...] (у порядку індексів),
              "current_soc_floor", "risk_hours", "risk_reason", "explain"}."""
    if not programs or cap_wh <= 0:
        return None
    shutdown_wh = cap_wh * shutdown_soc / 100.0
    risk_wh = energy_for(now_local, risk_h, hourly_profile, fallback_w, efficiency)

    def need_at(t):
        """Скільки Вт·год треба мати в момент t: ризиковий запас + найбільше графікове вікно,
        що почнеться до наступного дешевого заряду (його вже не встигнемо дозарядити дешево)."""
        horizon = next_cheap_start(t + timedelta(minutes=1), night_start_h, night_end_h)
        sched = 0.0
        why = None
        for b_start, b_end in outage_blocks:
            if b_end <= t or b_start >= horizon + timedelta(hours=1):
                continue
            s = max(b_start, t)
            hrs = (b_end - s).total_seconds() / 3600.0 + delay_h
            e = energy_for(s, hrs, hourly_profile, fallback_w, efficiency) * window_margin
            if e > sched:
                sched, why = e, "відключення %s–%s" % (b_start.strftime("%H:%M"), b_end.strftime("%H:%M"))
        if sched >= risk_wh:
            return sched, why
        return risk_wh, "запас %.0f год (%s)" % (risk_h, risk_reason)

    any_risk = bool(outage_blocks) or risk_h > calm_risk_h
    slots = []
    for idx, st, en in _slot_bounds(programs, now_local):
        # слот, що вже йде — від «зараз»; майбутні — від свого початку
        if st <= now_local < en:
            t0 = now_local
        elif en <= now_local:
            st += timedelta(days=1)
            en += timedelta(days=1)
            t0 = st
        else:
            t0 = st
        # найбільша потреба в межах слоту (крок 30 хв)
        best, why, t = 0.0, None, t0
        while t < en:
            e, w = need_at(t)
            if e > best:
                best, why = e, w
            t += timedelta(minutes=30)
        cheap = is_cheap(st + (en - st) / 2, night_start_h, night_end_h)
        if cheap:
            soc = 100 if any_risk else night_soc_calm
            grid = True
            why = "нічний (дешевий) заряд" + ("" if any_risk else ", ризику нема")
        else:
            soc = 100.0 * (best + shutdown_wh) / cap_wh
            soc = int(min(100, max(eco_floor, math.ceil(soc / 5.0) * 5)))
            # дорогий денний дозаряд — лише якщо вікно близько, а енергії не вистачає
            soon = any(b_start - t0 <= timedelta(hours=topup_lead_h) and b_end > t0
                       for b_start, b_end in outage_blocks)
            grid = soon and energy_wh < cap_wh * soc / 100.0
        slots.append({"index": idx, "time": st.strftime("%H:%M"), "soc": soc, "grid": grid, "why": why,
                      "start": st, "end": en})
    slots.sort(key=lambda s: s["index"])
    cur = next((s for s in slots if s["start"] <= now_local < s["end"]), None)
    return {
        "slots": [{k: v for k, v in s.items() if k not in ("start", "end")} for s in slots],
        "current_soc_floor": cur["soc"] if cur else None,
        "current_why": cur["why"] if cur else None,
        "risk_hours": round(risk_h, 1),
        "risk_reason": risk_reason,
        "risk_wh": round(risk_wh),
    }
