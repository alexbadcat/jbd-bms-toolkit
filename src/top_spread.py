#!/usr/bin/env python3
"""top_spread — tracks the real cell-voltage spread of a LiFePO4 pack at the top
of charge (cell_max >= 3.45V), the only region where capacity imbalance between
cells is actually visible; persists only the last completed session to disk.

top_spread — метрика розбалансу «на верху» пакета LiFePO4 (JBD BMS).

На полиці LFP (3.25-3.40 В) розбаланс невидимий: дельта комірок 3-7 мВ, навіть
якщо реальний розкид ємності між комірками великий. Реальна картина видно лише
на верхньому коліні (cell_max ≥ 3.45 В), коли слабші комірки доганяють сильні
повільніше й розрив росте.

Сесія верху — період, поки cell_max ≥ 3.45 В (гейт старту; після старту сесія
триває навіть якщо max тимчасово просів у смугу 3.40-3.45, бо це все ще коліно).
Під час сесії тримаємо максимум (cell_max - cell_min) і номери комірок, на яких
він стався. Сесія завершується, коли cell_max тримається < 3.40 В ≥10 хв
(DEBOUNCE — щоб короткі провали в дельтах при MQTT-джиттері не різали сесію
навпіл).

Публічне API:
  TopSpreadTracker() — завантажує персист (top_spread.state.json поряд зі
  скриптом) при створенні.
  .update(addr, data, now=None) -> dict з полями top_spread_now,
  top_spread_last, top_spread_last_at, top_spread_hi_cell, top_spread_lo_cell
  — саме це мерджити в MQTT-стан пака. `data` — те, що повернув
  jbd_bms.read_all (беремо cells/cell_max/cell_min).

Персист: лише ОСТАННЯ завершена сесія (top_spread_last*) — джерело для нічного
сторожа має пережити ребут демона. Поточна сесія (top_spread_now) навмисно НЕ
персиститься: після рестарту вона просто перерахується заново з наступних
тактів, поки гейт активний (як і в balance_mode.py — не ускладнюємо державу
заради даних, які й так швидко відновлюються).
"""
import json
import os
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "top_spread.state.json")

SESSION_ON_V = 3.45          # cell_max — від цього стартує сесія верху
SESSION_OFF_V = 3.40         # cell_max — нижче: кандидат на завершення сесії
SESSION_OFF_HOLD_S = 600     # тримати < OFF стільки, перш ніж закрити сесію


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)          # атомарно — не лишити битий файл при краші на запису


class TopSpreadTracker:
    def __init__(self, state_file=STATE_FILE):
        self._path = state_file
        self._persist = _load(state_file)   # {"<addr>": {last_mv, last_at, last_hi_cell, last_lo_cell}}
        self._live = {}                      # {addr: {active, peak_mv, peak_hi, peak_lo, below_since}}

    def _lv(self, addr):
        return self._live.setdefault(addr, {
            "active": False,
            "peak_mv": None,
            "peak_hi": None,
            "peak_lo": None,
            "below_since": None,
        })

    def _ps(self, addr):
        return self._persist.setdefault(str(addr), {
            "last_mv": None,
            "last_at": None,
            "last_hi_cell": None,
            "last_lo_cell": None,
        })

    def seed_last(self, addr, mv, at=None, hi_cell=None, lo_cell=None):
        """Підсадити top_spread_last ззовні (напр. зі скрипта ініціалізації з
        історії HA) — лише якщо ще нема живішого/свіжішого значення на диску."""
        ps = self._ps(addr)
        if ps["last_mv"] is not None:
            return   # вже є реальне значення (з живої сесії чи попереднього seed) — не затираємо
        ps["last_mv"] = round(mv, 1)
        ps["last_at"] = at or _now_iso()
        ps["last_hi_cell"] = hi_cell
        ps["last_lo_cell"] = lo_cell
        _save(self._path, self._persist)

    def update(self, addr: int, data: dict, now=None) -> dict:
        now = time.monotonic() if now is None else now
        cells = data.get("cells")
        cell_max = data.get("cell_max")
        cell_min = data.get("cell_min")
        lv = self._lv(addr)
        ps = self._ps(addr)

        if cell_max is not None and cell_min is not None:
            if lv["active"]:
                spread = round((cell_max - cell_min) * 1000, 1)
                if lv["peak_mv"] is None or spread > lv["peak_mv"]:
                    lv["peak_mv"] = spread
                    if cells:
                        lv["peak_hi"] = cells.index(max(cells)) + 1
                        lv["peak_lo"] = cells.index(min(cells)) + 1
                if cell_max < SESSION_OFF_V:
                    if lv["below_since"] is None:
                        lv["below_since"] = now
                    elif now - lv["below_since"] >= SESSION_OFF_HOLD_S:
                        ps["last_mv"] = lv["peak_mv"]
                        ps["last_at"] = _now_iso()
                        ps["last_hi_cell"] = lv["peak_hi"]
                        ps["last_lo_cell"] = lv["peak_lo"]
                        _save(self._path, self._persist)
                        lv["active"] = False
                        lv["peak_mv"] = None
                        lv["peak_hi"] = None
                        lv["peak_lo"] = None
                        lv["below_since"] = None
                else:
                    lv["below_since"] = None
            elif cell_max >= SESSION_ON_V:
                lv["active"] = True
                lv["below_since"] = None
                spread = round((cell_max - cell_min) * 1000, 1)
                lv["peak_mv"] = spread
                if cells:
                    lv["peak_hi"] = cells.index(max(cells)) + 1
                    lv["peak_lo"] = cells.index(min(cells)) + 1

        hi_cell = lv["peak_hi"] if lv["active"] else ps["last_hi_cell"]
        lo_cell = lv["peak_lo"] if lv["active"] else ps["last_lo_cell"]
        return {
            "top_spread_now": lv["peak_mv"] if lv["active"] else None,
            "top_spread_last": ps["last_mv"],
            "top_spread_last_at": ps["last_at"],
            "top_spread_hi_cell": hi_cell,
            "top_spread_lo_cell": lo_cell,
        }
