#!/usr/bin/env python3
"""Юніт-тести top_spread.py — без реального демона, з ізольованим tmp-файлом
персисту (tmp_path) і явно поданим часом (now=), без time.sleep.

Запуск: python3 -m pytest tests/ -q (з кореня репо)
"""
import os
import sys

# модулі лежать у корені (приватний репо) або в src/ (публічний jbd-bms-toolkit)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(_ROOT, "src"), _ROOT]
import top_spread as ts                                           # noqa: E402


def cells16(lo, hi, hi_idx=12, lo_idx=0):
    """16 комірок: база trohи вище lo (щоб lo_idx лишався ЄДИНИМ мінімумом),
    hi_idx = hi (єдиний максимум), lo_idx = lo."""
    base = round(lo + 0.01, 3)
    c = [base] * 16
    c[hi_idx] = hi
    c[lo_idx] = lo
    return c


def data(cell_max, cell_min, cells=None):
    d = {"cell_max": cell_max, "cell_min": cell_min}
    if cells is not None:
        d["cells"] = cells
    return d


def test_no_session_below_gate_stays_empty(tmp_path):
    t = ts.TopSpreadTracker(state_file=str(tmp_path / "state.json"))
    now = 1000.0
    for _ in range(5):
        r = t.update(0, data(3.40, 3.39), now=now)
        now += 30
    assert r["top_spread_now"] is None
    assert r["top_spread_last"] is None


def test_session_tracks_peak_and_cells():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        t = ts.TopSpreadTracker(state_file=os.path.join(d, "state.json"))
        now = 1000.0
        cells = cells16(3.40, 3.50, hi_idx=12, lo_idx=3)
        r = t.update(1, data(3.50, 3.40, cells), now=now)
        assert r["top_spread_now"] == 100.0
        assert r["top_spread_hi_cell"] == 13          # 1-indexed
        assert r["top_spread_lo_cell"] == 4
        # зростає далі
        now += 30
        cells2 = cells16(3.38, 3.52, hi_idx=12, lo_idx=3)
        r = t.update(1, data(3.52, 3.38, cells2), now=now)
        assert r["top_spread_now"] == 140.0
        assert r["top_spread_last"] is None             # сесія ще не завершена


def test_session_ends_after_10min_below_3_40():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        t = ts.TopSpreadTracker(state_file=os.path.join(d, "state.json"))
        now = 1000.0
        cells = cells16(3.40, 3.55, hi_idx=12, lo_idx=3)
        t.update(0, data(3.55, 3.40, cells), now=now)          # сесія стартує, peak=150
        now += 30
        r = t.update(0, data(3.38, 3.30), now=now)             # впало нижче OFF, below_since=now
        assert r["top_spread_now"] == 150.0                    # ще активна (debounce)
        now += 600                                               # рівно 600с від below_since — поріг ">=600" спрацьовує
        r = t.update(0, data(3.38, 3.30), now=now)
        assert r["top_spread_now"] is None
        assert r["top_spread_last"] == 150.0
        assert r["top_spread_hi_cell"] == 13
        assert r["top_spread_lo_cell"] == 4
        assert r["top_spread_last_at"] is not None


def test_dip_between_off_and_on_does_not_end_session():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        t = ts.TopSpreadTracker(state_file=os.path.join(d, "state.json"))
        now = 1000.0
        cells = cells16(3.40, 3.50, hi_idx=12, lo_idx=3)
        t.update(0, data(3.50, 3.40, cells), now=now)
        now += 30
        # просів у смугу 3.40-3.45 — сесія не закрита (ще вище SESSION_OFF_V)
        r = t.update(0, data(3.42, 3.40), now=now)
        assert r["top_spread_now"] is not None
        now += 1000
        r = t.update(0, data(3.46, 3.41), now=now)
        assert r["top_spread_now"] is not None
        assert r["top_spread_last"] is None


def test_persists_last_across_new_tracker_instance():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        t1 = ts.TopSpreadTracker(state_file=path)
        now = 1000.0
        cells = cells16(3.40, 3.60, hi_idx=5, lo_idx=9)
        t1.update(0, data(3.60, 3.40, cells), now=now)
        now += 30
        t1.update(0, data(3.30, 3.20), now=now)                # below_since виставлено тут
        now += 600
        t1.update(0, data(3.30, 3.20), now=now)                # закриває сесію
        t2 = ts.TopSpreadTracker(state_file=path)               # новий інстанс = "після ребуту"
        r = t2.update(0, data(3.20, 3.10), now=0.0)             # поза гейтом, нема активної сесії
        assert r["top_spread_last"] == 200.0
        assert r["top_spread_hi_cell"] == 6
        assert r["top_spread_lo_cell"] == 10


def test_two_packs_independent():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        t = ts.TopSpreadTracker(state_file=os.path.join(d, "state.json"))
        now = 1000.0
        t.update(0, data(3.50, 3.49), now=now)      # пак 0: малий розкид
        t.update(1, data(3.60, 3.30), now=now)      # пак 1: великий розкид
        r0 = t.update(0, data(3.50, 3.49), now=now)
        r1 = t.update(1, data(3.60, 3.30), now=now)
        assert r0["top_spread_now"] == 10.0
        assert r1["top_spread_now"] == 300.0


def test_seed_last_only_when_empty():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        t = ts.TopSpreadTracker(state_file=os.path.join(d, "state.json"))
        t.seed_last(0, 123.4, at="2026-09-30T00:00:00+00:00")
        r = t.update(0, data(3.20, 3.19), now=0.0)
        assert r["top_spread_last"] == 123.4
        # друга спроба seed — не повинна затерти вже наявне значення
        t.seed_last(0, 999.9, at="2026-10-01T00:00:00+00:00")
        r = t.update(0, data(3.20, 3.19), now=1.0)
        assert r["top_spread_last"] == 123.4


def test_missing_cells_falls_back_to_no_cell_numbers():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        t = ts.TopSpreadTracker(state_file=os.path.join(d, "state.json"))
        r = t.update(0, data(3.50, 3.40), now=0.0)      # нема "cells" у data
        assert r["top_spread_now"] == 100.0
        assert r["top_spread_hi_cell"] is None
        assert r["top_spread_lo_cell"] is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
