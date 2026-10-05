#!/usr/bin/env python3
"""Юніт-тести слідкувача режиму балансування (balance_mode.py) — без реального
серійного порту: BalanceModeFollower._write_and_verify підміняється фейком,
час подається явно через now=, а не через реальний time.sleep.

Запуск: з кореня репо — python3 -m pytest tests/ -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import balance_mode as bm                                         # noqa: E402


def make_follower(writes):
    """writes — список, куди фейковий запис додає (addr, target); завжди 'вдало'."""
    f = bm.BalanceModeFollower()

    def fake_write(port, addr, target):
        writes.append((addr, target))
        return True
    f._write_and_verify = fake_write
    return f


def data(cell_max, current):
    return {"cell_max": cell_max, "current": current}


def test_gate_below_3_40_ignored_forever():
    writes = []
    f = make_follower(writes)
    now = 1000.0
    for _ in range(20):
        mode = f.step("PORT", 0, data(3.35, 5.0), now=now)
        now += 30
    assert mode == "unknown"
    assert writes == []


def test_charge_after_120s_stable_above_gate():
    writes = []
    f = make_follower(writes)
    now = 1000.0
    # гейт активується (cell_max >= 3.45), струм стабільно ≥1А
    mode = f.step("PORT", 0, data(3.50, 5.2), now=now)
    assert mode == "unknown"           # ще нічого не змінилось на цьому такті
    assert writes == []
    now += 60
    mode = f.step("PORT", 0, data(3.50, 5.2), now=now)   # 60с стабільності — замало
    assert writes == []
    now += 61                           # разом 121с — уже досить
    mode = f.step("PORT", 0, data(3.50, 5.2), now=now)
    assert writes == [(0, "charge")]
    assert mode == "charge"


def test_static_when_current_near_zero():
    writes = []
    f = make_follower(writes)
    now = 1000.0
    f.step("PORT", 1, data(3.50, 0.1), now=now)
    now += 121
    mode = f.step("PORT", 1, data(3.50, 0.1), now=now)
    assert writes == [(1, "static")]
    assert mode == "static"


def test_dead_zone_does_not_switch_and_resets_debounce():
    writes = []
    f = make_follower(writes)
    now = 1000.0
    f.step("PORT", 0, data(3.50, 5.0), now=now)     # кандидат charge
    now += 100
    # впали в мертву зону 0.5..1.0А — кандидат має скинутись
    f.step("PORT", 0, data(3.50, 0.7), now=now)
    now += 100                                       # разом 200с від першого, але candidate скинуто
    mode = f.step("PORT", 0, data(3.50, 0.7), now=now)
    assert writes == []                               # мертва зона ніколи не пише
    assert mode == "unknown"


def test_gate_hysteresis_stays_active_between_3_40_and_3_45():
    writes = []
    f = make_follower(writes)
    now = 1000.0
    f.step("PORT", 0, data(3.50, 5.0), now=now)      # гейт увімкнувся
    now += 121
    f.step("PORT", 0, data(3.42, 5.0), now=now)       # між порогами — гейт лишається активним
    assert writes == [(0, "charge")]
    writes.clear()
    now += 400                                        # понад MIN_WRITE_INTERVAL
    f.step("PORT", 0, data(3.30, 0.0), now=now)        # різке падіння нижче 3.40 — гейт вимикається
    now += 400
    mode = f.step("PORT", 0, data(3.30, 0.0), now=now)
    assert writes == []                                # гейт вимкнено — записів більше нема
    assert mode == "charge"                            # known_mode не змінюється без гейта


def test_min_write_interval_blocks_immediate_retrigger():
    writes = []
    f = make_follower(writes)
    now = 1000.0
    f.step("PORT", 0, data(3.50, 5.0), now=now)
    now += 121
    f.step("PORT", 0, data(3.50, 5.0), now=now)       # записав "charge"
    assert writes == [(0, "charge")]
    now += 121                                         # струм упав, минуло лише 121с < 300с
    f.step("PORT", 0, data(3.50, 0.1), now=now)
    now += 121
    mode = f.step("PORT", 0, data(3.50, 0.1), now=now)
    assert writes == [(0, "charge")]                   # другого запису ще нема — не минуло 5 хв
    assert mode == "charge"
    now += 200                                         # разом з першого запису > 300с
    mode = f.step("PORT", 0, data(3.50, 0.1), now=now)
    assert writes == [(0, "charge"), (0, "static")]
    assert mode == "static"


def test_three_fails_pause_one_hour_then_retry():
    writes = []
    f = bm.BalanceModeFollower()
    calls = []

    def always_fail(port, addr, target):
        calls.append(target)
        return False
    f._write_and_verify = always_fail

    now = 1000.0
    f.step("PORT", 0, data(3.50, 5.0), now=now)
    now += 121
    f.step("PORT", 0, data(3.50, 5.0), now=now)        # спроба 1 — невдача
    assert len(calls) == 1
    now += 301
    f.step("PORT", 0, data(3.50, 5.0), now=now)        # спроба 2 — невдача
    assert len(calls) == 2
    now += 301
    f.step("PORT", 0, data(3.50, 5.0), now=now)        # спроба 3 — невдача → пауза 1 год
    assert len(calls) == 3

    now += 301                                          # у межах паузи — не пробує
    f.step("PORT", 0, data(3.50, 5.0), now=now)
    assert len(calls) == 3

    now += 3600                                         # пауза минула
    f.step("PORT", 0, data(3.50, 5.0), now=now)
    assert len(calls) == 4


def test_startup_reads_real_mode_and_unknown_on_failure(monkeypatch):
    monkeypatch.setattr(bm, "_read_old", lambda port, addr: 479)   # біт3=1 → charge
    monkeypatch.setattr(bm, "_read_new", lambda port, addr: None)  # нема відповіді → unknown
    f = bm.BalanceModeFollower()
    logs = []
    f.startup("PORT", [0, 1], log=logs.append)
    assert f.known_mode(0) == "charge"
    assert f.known_mode(1) == "unknown"
    assert len(logs) == 2


def test_decode_old_ignores_unrelated_bits():
    # 479 = 0b111011111, 471 = 0b111010111 — біти LED/balance_en однакові,
    # різниця лише в біті3
    assert bm._decode(0, 479) == "charge"
    assert bm._decode(0, 471) == "static"
    assert bm._decode(0, 479 | 0x10) == "charge"     # сторонній біт (LED) не заважає
    assert bm._decode(0, 471 & ~0x02) == "static"


def test_decode_new():
    assert bm._decode(1, 0) == "charge"
    assert bm._decode(1, 1) == "static"
    assert bm._decode(1, 7) == "unknown"
    assert bm._decode(1, None) == "unknown"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
