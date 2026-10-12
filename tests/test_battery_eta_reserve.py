"""Тести адаптивного резерву (battery_eta_reserve): режими ризику, ескалація, план поличок.
Запуск: python3 -m pytest tests/ -q (з кореня репо)."""
import os
import sys
from datetime import datetime, time, timedelta

# модулі лежать у корені (приватний репо) або в src/ (публічний jbd-bms-toolkit)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(_ROOT, "src"), _ROOT]

import battery_eta_reserve as r                                     # noqa: E402

PROGS = [{"time": time(1, 0), "soc": 100}, {"time": time(7, 0), "soc": 55},
         {"time": time(9, 0), "soc": 55}, {"time": time(13, 0), "soc": 55},
         {"time": time(17, 0), "soc": 20}, {"time": time(23, 20), "soc": 100}]
CAP = 17000.0
FLAT = [700.0] * 24


def test_percentile_nearest_rank():
    assert r.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9
    assert r.percentile([], 90) is None


def test_episodes_from_hourly_merges_contiguous_hours():
    t0 = datetime(2026, 1, 10, 0)
    rows = [(t0 + timedelta(hours=i), 0.0 if 2 <= i < 6 else 230.0, 0.0 if 2 <= i < 6 else 220.0)
            for i in range(10)]
    eps = r.episodes_from_hourly(rows)
    assert eps == [(t0 + timedelta(hours=2), 4.0)]


def test_risk_hours_blends_recent_with_last_year_season():
    now = datetime(2026, 1, 20, 12)
    last_year = [(now - timedelta(days=365) + timedelta(days=d), 15.0) for d in range(-10, 10)]
    recent = [(now - timedelta(days=d), 4.0) for d in range(1, 3)]       # лише 2 свіжі епізоди
    rk = r.risk_hours(last_year + recent, now)
    # 2 свіжих проти prior 8 → вага свіжих 0.2: 0.2*4 + 0.8*15 = 12.8
    assert abs(rk["hours"] - 12.8) < 0.01
    assert rk["recent_n"] == 2 and rk["last_year_n"] == 20


def test_risk_hours_calm_when_no_data():
    rk = r.risk_hours([], datetime(2026, 7, 1, 12))
    assert rk["hours"] == 2.0


def test_escalate_emergency_and_alert():
    h, why = r.escalate(4.0, attack_level_h=29.0, emergency_on=True)
    assert h == 24.0 and why == "аварійні відключення"
    h, why = r.escalate(4.0, attack_level_h=0.0, alert_on=True)
    assert h == 8.0 and why == "повітряна тривога"
    h, why = r.escalate(10.0, attack_level_h=0.0, alert_on=True)
    assert h == 10.0 and why == "статистика відключень"


def test_energy_for_flat_profile_with_efficiency():
    e = r.energy_for(datetime(2026, 10, 13, 17), 5.0, FLAT, 700.0, 0.875)
    assert abs(e - 4000.0) < 1.0                                       # 5 год × 700 Вт / 0.875


def _plan(now, blocks, risk_h, energy=CAP):
    return r.plan(now, PROGS, CAP, energy, 10, blocks, risk_h, "тест", FLAT, 700.0, 0.875)


def test_plan_calm_day_keeps_eco_shelves():
    p = _plan(datetime(2026, 7, 1, 10), [], 2.0)
    soc = {s["time"]: s["soc"] for s in p["slots"]}
    assert soc["09:00"] == 20 and soc["13:00"] == 20 and soc["17:00"] == 20
    assert soc["01:00"] == 90 and soc["23:20"] == 90                  # без ризику ніч не до 100
    assert not any(s["grid"] for s in p["slots"] if s["time"] in ("09:00", "13:00", "17:00"))


def test_plan_scheduled_evening_window_reserves_only_what_is_needed():
    now = datetime(2026, 10, 13, 10)
    blocks = [(datetime(2026, 10, 13, 17), datetime(2026, 10, 13, 22))]
    p = _plan(now, blocks, 3.0)
    soc = {s["time"]: s["soc"] for s in p["slots"]}
    # 5.5 год × 700 / 0.875 × 1.15 ≈ 5060 Вт·год + 10% (1700) = 6760 → 39.8% → 40
    assert soc["09:00"] == 40 and soc["13:00"] == 40
    assert soc["17:00"] == 40                                          # у самому вікні тримаємо до кінця
    assert soc["01:00"] == 100 and soc["23:20"] == 100                # ризик є → ніч до сотки
    assert soc["09:00"] < 100                                          # головне: НЕ 100% цілий день


def test_plan_attack_regime_holds_full():
    p = _plan(datetime(2026, 2, 6, 12), [], 20.0)
    soc = {s["time"]: s["soc"] for s in p["slots"]}
    assert soc["13:00"] == 100 and soc["17:00"] == 100


def test_plan_daytime_topup_only_when_window_is_close_and_energy_short():
    now = datetime(2026, 10, 13, 15)
    blocks = [(datetime(2026, 10, 13, 17), datetime(2026, 10, 13, 22))]
    low = _plan(now, blocks, 3.0, energy=CAP * 0.25)
    full = _plan(now, blocks, 3.0, energy=CAP * 0.9)
    g_low = {s["time"]: s["grid"] for s in low["slots"]}
    g_full = {s["time"]: s["grid"] for s in full["slots"]}
    assert g_low["13:00"] is True                                      # вікно за 2 год, енергії мало
    assert g_full["13:00"] is False                                    # енергії вистачає — не платимо денний тариф


def test_plan_window_after_cheap_charge_does_not_inflate_today():
    # вікно завтра о 09:00 — до нього буде дешевий нічний заряд, тож сьогодні вдень не тримаємо
    now = datetime(2026, 10, 13, 12)
    blocks = [(datetime(2026, 10, 14, 9), datetime(2026, 10, 14, 13))]
    p = _plan(now, blocks, 3.0)
    soc = {s["time"]: s["soc"] for s in p["slots"]}
    assert soc["13:00"] <= 25 and soc["17:00"] <= 25


def test_alarm_hours_counts_last_day_only():
    now = datetime(2026, 10, 13, 12)
    ch = [(now - timedelta(hours=30), "on"), (now - timedelta(hours=26), "off"),     # поза вікном
          (now - timedelta(hours=10), "on"), (now - timedelta(hours=7), "off"),
          (now - timedelta(hours=1), "on")]                                         # триває
    assert abs(r.alarm_hours(ch, now) - 4.0) < 1e-6


def test_alarm_hours_ongoing_from_before_window():
    now = datetime(2026, 10, 13, 12)
    assert abs(r.alarm_hours([(now - timedelta(hours=30), "on")], now) - 24.0) < 1e-6


def test_escalate_post_attack_and_frost():
    h, why = r.escalate(4.0, 0.0, post_attack=True)
    assert h == 14.0 and why == "після масованої атаки"
    h, why = r.escalate(4.0, 0.0, outdoor_temp=-12)
    assert h == 8.0 and why.startswith("мороз")
    h, why = r.escalate(4.0, 0.0, outdoor_temp=-6)
    assert h == 6.0 and why.startswith("холод")
    h, why = r.escalate(4.0, 0.0, outdoor_temp=5)
    assert h == 4.0


def test_alarm_hours_ignores_changes_after_now():
    now = datetime(2026, 10, 5, 12)
    ch = [(now - timedelta(hours=2), "on"), (now - timedelta(hours=1), "off"),
          (now + timedelta(days=3), "on")]                                          # з «майбутнього»
    assert abs(r.alarm_hours(ch, now) - 1.0) < 1e-6
