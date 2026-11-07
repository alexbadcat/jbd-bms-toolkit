"""Продовження точного блоку відключень прилеглими «можливими» вікнами (battery_eta_outage)."""
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(_ROOT, "src"), _ROOT]

import battery_eta_outage as o                                     # noqa: E402

NOW = datetime(2026, 11, 7, 18, 20)
DEF = ["07.11 18:00–19:00 (другі 30хв)", "07.11 19:00–20:00 (немає)", "07.11 23:00–00:00 (немає)",
       "07.11 20:00–21:00 (немає)", "07.11 21:00–22:00 (немає)", "07.11 22:00–23:00 (немає)"]
POS = ["08.11 00:00–01:00 (можливо)", "08.11 01:00–02:00 (можливо перші 30хв)", "08.11 05:00–06:00 (можливо)"]


def test_definite_block_ends_at_midnight():
    assert o.next_outage_block(DEF, NOW) == (datetime(2026, 11, 7, 18), datetime(2026, 11, 8, 0))


def test_extends_through_adjacent_possible_only():
    blk = o.next_outage_block(DEF, NOW)
    # 00:00–02:00 впритул продовжує блок; 05:00 — окремий, не приклеюється
    assert o.extend_with_possible(blk, POS, NOW) == datetime(2026, 11, 8, 2)


def test_no_possible_keeps_block_end():
    blk = o.next_outage_block(DEF, NOW)
    assert o.extend_with_possible(blk, [], NOW) == blk[1]
    assert o.extend_with_possible(None, POS, NOW) is None
