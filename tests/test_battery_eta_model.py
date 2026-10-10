#!/usr/bin/env python3
"""Юніт-тести battery_eta_model.py — чисті функції, без мережі/HA/MQTT, час подається
явно (now_local=...), без time.sleep.

Запуск: python3 -m pytest tests/ -q (з кореня репо)
"""
import os
import sys
from datetime import date, datetime, time, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(_ROOT, "src"), _ROOT]
import battery_eta_model as m                                     # noqa: E402


# ───────────────────────── час/Київ ─────────────────────────

def test_kyiv_offset_summer_is_3():
    assert m.kyiv_utc_offset_hours(datetime(2026, 7, 1)) == 3


def test_kyiv_offset_winter_is_2():
    assert m.kyiv_utc_offset_hours(datetime(2026, 1, 15)) == 2


def test_kyiv_offset_dst_boundary_october():
    # 2026-10-25 — остання неділя жовтня; до 01:00 UTC ще +3, після — +2
    before = datetime(2026, 10, 25, 0, 59)
    after = datetime(2026, 10, 25, 1, 1)
    assert m.kyiv_utc_offset_hours(before) == 3
    assert m.kyiv_utc_offset_hours(after) == 2


def test_to_kyiv_adds_offset():
    dt = datetime(2026, 10, 9, 15, 0, 0)  # літній час, +3
    assert m.to_kyiv(dt) == datetime(2026, 10, 9, 18, 0, 0)


# ───────────────────────── вага дня ─────────────────────────

def test_recency_weight_half_life():
    assert abs(m.recency_weight(45, 45) - 0.5) < 1e-9
    assert m.recency_weight(0, 45) == 1.0
    assert m.recency_weight(-5, 45) == 1.0  # майбутнє/сьогодні — без штрафу


def test_recency_weight_zero_half_life_no_decay():
    assert m.recency_weight(1000, 0) == 1.0


def test_season_weight_same_day_is_1():
    assert m.season_weight(0, 45) == 1.0


def test_season_weight_decreases_with_distance():
    near = m.season_weight(10, 45)
    far = m.season_weight(180, 45)
    assert near > far


def test_circular_doy_diff_wraps_year():
    # 5 січня vs 360 грудня — реально близько (через межу року)
    diff = m.circular_doy_diff(5, 360, 365.25)
    assert diff < 15


def test_day_weight_drops_low_coverage_entirely():
    today = date(2026, 10, 9)
    w = m.day_weight(date(2026, 10, 8), today, coverage=0.05, min_coverage=0.15)
    assert w == 0.0


def test_day_weight_positive_for_good_day():
    today = date(2026, 10, 9)
    w = m.day_weight(date(2026, 10, 8), today, coverage=0.95)
    assert w > 0


def test_effective_n_days_equal_weights():
    assert abs(m.effective_n_days([1.0, 1.0, 1.0, 1.0]) - 4.0) < 1e-9


def test_effective_n_days_skewed_weights_less_than_count():
    w = [1.0, 0.01, 0.01, 0.01]
    eff = m.effective_n_days(w)
    assert eff < 4.0
    assert eff > 0.9  # домінуюча вага все одно дає ~1 ефективну добу


def test_effective_n_days_empty():
    assert m.effective_n_days([]) == 0.0


# ───────────────────────── профіль навантаження ─────────────────────────

def _day(date_iso, hourly, coverage=1.0, sessions=None, charge_bins=None):
    return {"date": date_iso, "coverage": coverage,
            "hourly_load_w": hourly, "sessions": sessions or [],
            "charge_bins": charge_bins or {}}


def test_weighted_hourly_profile_basic_average():
    days = [
        _day("2026-10-07", [500.0] * 24),
        _day("2026-10-08", [700.0] * 24),
    ]
    today = date(2026, 10, 9)
    # з однаковою вагою (близькі дні, високий half_life) очікуємо ~середнє
    prof = m.weighted_hourly_profile(days, today, half_life_days=1000, sigma_days=0)
    assert abs(prof["all"][12] - 600.0) < 1.0
    assert prof["n_days"] == 2
    assert prof["oldest"] == "2026-10-07"
    assert prof["weekday_split"] is False
    assert prof["weekday"] is None


def test_weighted_hourly_profile_fills_gaps():
    hourly = [None] * 24
    hourly[10] = 1000.0
    days = [_day("2026-10-08", hourly)]
    today = date(2026, 10, 9)
    prof = m.weighted_hourly_profile(days, today, half_life_days=1000, sigma_days=0)
    assert prof["all"][10] == 1000.0
    assert prof["all"][9] == 1000.0  # найближча відома по колу
    assert all(v is not None for v in prof["all"])


def test_weighted_hourly_profile_recent_day_dominates():
    days = [
        _day("2026-01-01", [100.0] * 24),   # дуже старий і несезонний
        _day("2026-10-08", [900.0] * 24),   # свіжий
    ]
    today = date(2026, 10, 9)
    prof = m.weighted_hourly_profile(days, today, half_life_days=20, sigma_days=30)
    assert prof["all"][0] > 700.0


def test_weighted_hourly_profile_weekday_split_activates_with_enough_days():
    days = []
    # 10 будніх (пн) + 4 вихідних (сб) днів — split_min_days=10 типово
    base = date(2026, 1, 5)  # понеділок
    for i in range(10):
        days.append(_day((base + timedelta(days=7 * i)).isoformat(), [500.0] * 24))
    base_sat = date(2026, 1, 10)
    for i in range(4):
        days.append(_day((base_sat + timedelta(days=7 * i)).isoformat(), [900.0] * 24))
    today = date(2026, 6, 1)
    prof = m.weighted_hourly_profile(days, today, half_life_days=1000, sigma_days=0, split_min_days=10)
    assert prof["weekday_split"] is True
    assert prof["weekday"][0] < prof["weekend"][0]


def test_profile_for_returns_all_when_no_split():
    prof = {"weekday_split": False, "all": [1] * 24, "weekday": None, "weekend": None}
    assert m.profile_for(prof, is_weekend=True) == [1] * 24


def test_profile_for_returns_weekend_when_split():
    prof = {"weekday_split": True, "all": [1] * 24, "weekday": [2] * 24, "weekend": [3] * 24}
    assert m.profile_for(prof, is_weekend=True) == [3] * 24
    assert m.profile_for(prof, is_weekend=False) == [2] * 24


# ───────────────────────── ефективність (ККД) ─────────────────────────

def test_weighted_efficiency_default_when_no_sessions():
    eff, n = m.weighted_efficiency([], date(2026, 10, 9), default=0.91)
    assert eff == 0.91
    assert n == 0


def test_weighted_efficiency_computes_ratio():
    days = [_day("2026-10-08", [500.0] * 24, sessions=[
        {"energy_battery_wh": 1000.0, "energy_load_wh": 920.0, "quality_ok": True},
    ])]
    eff, n = m.weighted_efficiency(days, date(2026, 10, 9), half_life_days=1000, sigma_days=0)
    assert abs(eff - 0.92) < 1e-6
    assert n == 1


def test_weighted_efficiency_rejects_implausible_sessions():
    days = [_day("2026-10-08", [500.0] * 24, sessions=[
        {"energy_battery_wh": 1000.0, "energy_load_wh": 50.0, "quality_ok": True},  # ratio 0.05 — сміття
    ])]
    eff, n = m.weighted_efficiency(days, date(2026, 10, 9), default=0.9)
    assert n == 0
    assert eff == 0.9


def test_weighted_efficiency_ignores_quality_flagged_false():
    days = [_day("2026-10-08", [500.0] * 24, sessions=[
        {"energy_battery_wh": 1000.0, "energy_load_wh": 920.0, "quality_ok": False},
    ])]
    eff, n = m.weighted_efficiency(days, date(2026, 10, 9), default=0.77)
    assert n == 0
    assert eff == 0.77


# ───────────────────────── крива хвоста заряду ─────────────────────────

def test_weighted_charge_taper_falls_back_to_default():
    curve = m.weighted_charge_taper([], date(2026, 10, 9))
    assert curve == m._DEFAULT_TAPER


def test_weighted_charge_taper_learns_from_bins():
    days = [_day("2026-10-08", [500.0] * 24, charge_bins={"90": [0.8, 1.0], "98": [0.1, 1.0]})]
    curve = m.weighted_charge_taper(days, date(2026, 10, 9), half_life_days=1000, sigma_days=0)
    assert abs(curve[90] - 0.8) < 1e-6
    assert abs(curve[98] - 0.1) < 1e-6


def test_taper_ratio_interpolates():
    curve = {80: 1.0, 100: 0.0}
    assert abs(m.taper_ratio_at_soc(curve, 90) - 0.5) < 1e-6


def test_taper_ratio_clamps_outside_range():
    curve = {80: 1.0, 100: 0.0}
    assert m.taper_ratio_at_soc(curve, 50) == 1.0
    assert m.taper_ratio_at_soc(curve, 150) == 0.0


def test_taper_ratio_empty_curve_is_full_power():
    assert m.taper_ratio_at_soc({}, 95) == 1.0


# ───────────────────────── поріг розряду ─────────────────────────

def _progs(socs, times=("01:00", "07:00", "09:00", "13:00", "17:00", "23:20")):
    return [{"time": time.fromisoformat(t + ":00" if len(t) == 5 else t), "soc": s}
            for t, s in zip(times, socs)]


def test_current_program_slot_picks_active():
    progs = _progs([90, 55, 55, 55, 20, 100])
    slot = m.current_program_slot(progs, datetime(2026, 10, 9, 18, 0))
    assert slot["soc"] == 20  # активний з 17:00


def test_current_program_slot_wraps_to_last_before_first():
    progs = _progs([90, 55, 55, 55, 20, 100])
    slot = m.current_program_slot(progs, datetime(2026, 10, 9, 0, 30))
    assert slot["soc"] == 100  # до 01:00 активний останній слот (23:20) доби


def test_current_program_slot_empty():
    assert m.current_program_slot([], datetime(2026, 10, 9, 18, 0)) is None


def test_threshold_offgrid_is_flat_shutdown_soc():
    segs = m.build_discharge_threshold_schedule(
        datetime(2026, 10, 9, 18, 0), programs=_progs([90, 55, 55, 55, 20, 100]),
        grid_on=False, capacity_wh=15000, shutdown_soc=10)
    assert len(segs) == 1
    assert segs[0][0] == 0
    assert abs(segs[0][1] - 1500.0) < 1e-6
    assert "немає мережі" in segs[0][2]


def test_threshold_ongrid_current_slot_shelf():
    # зараз 18:00 — активний слот program_5 (17:00) soc=20
    segs = m.build_discharge_threshold_schedule(
        datetime(2026, 10, 9, 18, 0), programs=_progs([90, 55, 55, 55, 20, 100]),
        grid_on=True, capacity_wh=15000, shutdown_soc=10)
    assert abs(segs[0][1] - 15000 * 0.20) < 1e-6


def test_threshold_ongrid_future_slot_change_included():
    segs = m.build_discharge_threshold_schedule(
        datetime(2026, 10, 9, 18, 0), programs=_progs([90, 55, 55, 55, 20, 100]),
        grid_on=True, capacity_wh=15000, shutdown_soc=10, horizon_min=24 * 60)
    minutes = [s[0] for s in segs]
    assert 0 in minutes
    # наступна зміна — 23:20 (P6=100%) через 5*60+20 = 320 хв
    assert any(abs(mi - 320) < 1 for mi in minutes)


def test_threshold_no_programs():
    segs = m.build_discharge_threshold_schedule(datetime(2026, 10, 9, 18, 0), [], True, 15000, 10)
    assert segs[0][1] == 0.0


# ───────────────────────── прогноз розряду ─────────────────────────

def test_forecast_depletion_constant_load_matches_analytic():
    # поріг 0 Вт·год, енергія 1000 Вт·год, навантаження 500 Вт, ККД 1.0 → 120 хв
    segs = [(0, 0.0, "поріг")]
    r = m.forecast_depletion(1000.0, 500.0, None, segs, efficiency=1.0,
                              now_local=None, step_min=5, horizon_min=600, blend_minutes=0)
    assert r["reached"] is True
    assert abs(r["minutes"] - 120) <= 5


def test_forecast_depletion_never_reached_within_horizon():
    segs = [(0, -10000.0, "недосяжний поріг")]
    r = m.forecast_depletion(1000.0, 500.0, None, segs, efficiency=1.0,
                              now_local=None, step_min=5, horizon_min=60, blend_minutes=0)
    assert r["reached"] is False
    assert r["minutes"] is None


def test_forecast_depletion_efficiency_shortens_time():
    segs = [(0, 0.0, "поріг")]
    fast = m.forecast_depletion(1000.0, 500.0, None, segs, efficiency=0.5, blend_minutes=0, horizon_min=600)
    slow = m.forecast_depletion(1000.0, 500.0, None, segs, efficiency=1.0, blend_minutes=0, horizon_min=600)
    assert fast["minutes"] < slow["minutes"]


def test_forecast_depletion_segment_change_lowers_threshold_extends_time():
    # поріг після 60 хв падає нижче — продовжуємо far
    segs_flat = [(0, 400.0, "high")]
    segs_drop = [(0, 400.0, "high"), (60, 0.0, "low")]
    r_flat = m.forecast_depletion(1000.0, 500.0, None, segs_flat, efficiency=1.0, blend_minutes=0, horizon_min=600)
    r_drop = m.forecast_depletion(1000.0, 500.0, None, segs_drop, efficiency=1.0, blend_minutes=0, horizon_min=600)
    assert r_drop["reached"] is True
    assert r_drop["minutes"] > r_flat["minutes"]


def test_forecast_depletion_uses_profile_after_blend():
    now_local = datetime(2026, 10, 9, 0, 0)
    profile = [2000.0] * 24  # набагато важче за EMA — має пришвидшити після блендингу
    segs = [(0, 0.0, "поріг")]
    with_profile = m.forecast_depletion(1000.0, 100.0, profile, segs, efficiency=1.0,
                                         now_local=now_local, blend_minutes=10, horizon_min=120)
    without_profile = m.forecast_depletion(1000.0, 100.0, None, segs, efficiency=1.0,
                                            now_local=now_local, blend_minutes=10, horizon_min=1000)
    assert with_profile["minutes"] < without_profile["minutes"]


# ───────────────────────── прогноз заряду ─────────────────────────

def test_forecast_charge_reaches_target():
    curve = {80: 1.0, 100: 1.0}  # без хвоста — постійний струм
    r = m.forecast_charge(energy_wh=7500.0, capacity_wh=15000.0, target_soc=100.0,
                           pack_voltage_v=52.0, max_current_a=50.0, taper_curve=curve,
                           step_min=5, horizon_min=1000)
    assert r["reached"] is True
    assert r["minutes"] > 0


def test_forecast_charge_taper_slows_near_top():
    curve_flat = {80: 1.0, 100: 1.0}
    curve_taper = {80: 1.0, 95: 1.0, 100: 0.05}
    common = dict(capacity_wh=15000.0, target_soc=100.0, pack_voltage_v=52.0,
                   max_current_a=50.0, step_min=5, horizon_min=3000)
    r_flat = m.forecast_charge(energy_wh=14000.0, taper_curve=curve_flat, **common)
    r_taper = m.forecast_charge(energy_wh=14000.0, taper_curve=curve_taper, **common)
    assert r_taper["minutes"] > r_flat["minutes"]


def test_forecast_charge_zero_max_current_never_reached():
    r = m.forecast_charge(energy_wh=1000.0, capacity_wh=15000.0, target_soc=100.0,
                           pack_voltage_v=52.0, max_current_a=0.0, taper_curve={}, horizon_min=60)
    assert r["reached"] is False


def test_forecast_charge_already_above_target():
    r = m.forecast_charge(energy_wh=15000.0, capacity_wh=15000.0, target_soc=90.0,
                           pack_voltage_v=52.0, max_current_a=50.0, taper_curve={}, horizon_min=60)
    assert r["reached"] is True
    assert r["minutes"] == 0


# ───────────────────────── форматування ─────────────────────────

def test_fmt_hm_minutes_only():
    assert m.fmt_hm(45) == "45 хв"


def test_fmt_hm_hours_and_minutes():
    assert m.fmt_hm(145) == "2 год 25 хв"


def test_fmt_hm_none():
    assert m.fmt_hm(None) == "—"


def test_fmt_hm_over_a_day():
    assert m.fmt_hm(25 * 60) == "1 дн 1 год"


def test_fmt_clock_adds_minutes():
    now = datetime(2026, 10, 9, 20, 0)
    assert m.fmt_clock(now, 65) == "21:05"


def test_fmt_clock_none_minutes():
    assert m.fmt_clock(datetime(2026, 10, 9), None) is None


def test_eta_text_short_horizon_is_hm():
    assert m.eta_text(90, "поличка 55%") == "1 год 30 хв"


def test_eta_text_long_horizon_is_clock():
    now = datetime(2026, 10, 9, 10, 0)
    txt = m.eta_text(400, "поличка 55% о 17:00", now_local=now, long_horizon_min=150)
    assert txt.startswith("до ")
    assert "16:40" in txt


def test_eta_text_none():
    assert m.eta_text(None, "x") == "—"


# ───────────────────────── згладжування ─────────────────────────

def test_smooth_display_small_change_ignored():
    assert m.smooth_display(100, 101) == 100


def test_smooth_display_big_change_updates():
    assert m.smooth_display(100, 130) == 130


def test_smooth_display_none_to_value_updates():
    assert m.smooth_display(None, 50) == 50


def test_smooth_display_value_to_none_updates():
    assert m.smooth_display(50, None) is None


def test_smooth_display_relative_threshold_on_large_values():
    # 600 → 620: diff=20 >= abs(3) але < 5% від 600 (=30) → лишаємо старе
    assert m.smooth_display(600, 620) == 600
    # 600 → 650: diff=50 >= 5% (30) → оновлюємо
    assert m.smooth_display(600, 650) == 650


# ───────────────────────── самоперевірка (прогноз vs факт) ─────────────────────────

def test_start_forecast_record_shape():
    r = m.start_forecast_record("2026-10-09T10:00:00", "empty", 120, meta={"a": 1})
    assert r["resolved"] is False
    assert r["predicted_minutes"] == 120
    assert r["meta"] == {"a": 1}


def test_finalize_forecast_records_computes_error():
    recs = [m.start_forecast_record("2026-10-09T10:00:00", "empty", 120)]
    m.finalize_forecast_records(recs, "2026-10-09T12:00:00", "empty")  # 120 хв фактично
    assert recs[0]["resolved"] is True
    assert abs(recs[0]["actual_minutes"] - 120.0) < 1e-6
    assert abs(recs[0]["pct_error"] - 0.0) < 1e-6


def test_finalize_forecast_records_ignores_other_kind():
    recs = [m.start_forecast_record("2026-10-09T10:00:00", "full", 60)]
    m.finalize_forecast_records(recs, "2026-10-09T12:00:00", "empty")
    assert recs[0]["resolved"] is False


def test_finalize_forecast_records_does_not_resolve_twice():
    recs = [m.start_forecast_record("2026-10-09T10:00:00", "empty", 120)]
    m.finalize_forecast_records(recs, "2026-10-09T12:00:00", "empty")
    first_actual = recs[0]["actual_minutes"]
    m.finalize_forecast_records(recs, "2026-10-09T15:00:00", "empty")
    assert recs[0]["actual_minutes"] == first_actual


def test_compute_mape_averages_last_n():
    recs = [
        {"resolved": True, "kind": "empty", "pct_error": 10.0},
        {"resolved": True, "kind": "empty", "pct_error": 20.0},
        {"resolved": True, "kind": "full", "pct_error": 999.0},
        {"resolved": False, "kind": "empty", "pct_error": 5.0},
    ]
    assert m.compute_mape(recs, "empty") == 15.0


def test_compute_mape_none_when_empty():
    assert m.compute_mape([], "empty") is None


def test_prune_records_keeps_last_n():
    recs = list(range(300))
    pruned = m.prune_records(recs, keep=200)
    assert len(pruned) == 200
    assert pruned[-1] == 299


def test_taper_ratio_accepts_string_keys_after_json_roundtrip():
    # крива, що пройшла через json-стан, має ключі-рядки — регресія 10.10.2026 (TypeError після рестарту)
    import json
    curve = json.loads(json.dumps({90.0: 1.0, 100.0: 0.2}))
    assert abs(m.taper_ratio_at_soc(curve, 95.0) - 0.6) < 1e-9
