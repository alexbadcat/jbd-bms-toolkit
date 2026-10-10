#!/usr/bin/env python3
"""Юніт-тести battery_eta_outage.py (парсинг вікон відключень ДТЕК) — чисті функції."""
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(_ROOT, "src"), _ROOT]
import battery_eta_outage as mo                                   # noqa: E402


def test_parse_outage_window_line_outage():
    it = mo.parse_outage_window_line("09.10 21:00–22:00 (другі 30хв)", 2026)
    assert it["is_outage"] is True
    assert it["start"] == datetime(2026, 10, 9, 21, 0)
    assert it["end"] == datetime(2026, 10, 9, 22, 0)


def test_parse_outage_window_line_nemaie_is_outage():
    # «немає» = світла немає всю годину — це ВІДКЛЮЧЕННЯ, не його відсутність
    it = mo.parse_outage_window_line("09.10 22:00–23:00 (немає)", 2026)
    assert it["is_outage"] is True


def test_parse_outage_window_line_invalid():
    assert mo.parse_outage_window_line("щось не те", 2026) is None


def test_parse_outage_window_line_midnight_wrap():
    it = mo.parse_outage_window_line("09.10 23:00–00:00 (немає)", 2026)
    assert it["end"] == datetime(2026, 10, 10, 0, 0)


def test_next_outage_block_end_single_hour():
    windows = ["09.10 21:00–22:00 (другі 30хв)", "09.10 23:00–00:00 (немає)"]
    now = datetime(2026, 10, 9, 15, 5)
    assert mo.next_outage_block_end(windows, now) == datetime(2026, 10, 9, 22, 0)


def test_next_outage_block_end_contiguous_merges():
    # реальний набір вікон з 09.10: 21–22 (другі 30хв), 22–23 і 23–00 (немає) → один блок до 00:00
    windows = ["09.10 21:00–22:00 (другі 30хв)", "09.10 22:00–23:00 (немає)", "09.10 23:00–00:00 (немає)"]
    now = datetime(2026, 10, 9, 15, 5)
    assert mo.next_outage_block_end(windows, now) == datetime(2026, 10, 10, 0, 0)
    assert mo.next_outage_block(windows, now) == (datetime(2026, 10, 9, 21, 0), datetime(2026, 10, 10, 0, 0))


def test_next_outage_block_end_none_when_all_past():
    windows = ["09.10 08:00–09:00 (немає)", "09.10 09:00–10:00 (немає)"]
    now = datetime(2026, 10, 9, 15, 5)
    assert mo.next_outage_block_end(windows, now) is None
    assert mo.next_outage_block(windows, now) is None


def test_next_outage_block_end_empty_list():
    assert mo.next_outage_block_end([], datetime(2026, 10, 9, 15, 5)) is None
