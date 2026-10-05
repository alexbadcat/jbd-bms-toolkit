#!/usr/bin/env python3
"""One-off seed of top_spread.state.json from Home Assistant history (48h), so
a "spread got worse overnight" watchdog automation has something to compare
against on day one, instead of waiting for the first live top-of-charge session.

Бере історію сенсора "Дельта комірок" (cell_delta_mv — вже = max(cells)-min(cells)
у мВ, той самий показник, що top_spread рахує сам) по REST /api/history/period
з ОБОВ'ЯЗКОВИМ end_time (без нього HA віддає тільки останню добу, а нам треба 48
год) і бере максимум за вікно як top_spread_last seed. Розбаланс на полиці LFP
невидимий (3-7 мВ), тож максимум дельти за 48 год — це майже завжди і є пік на
верхньому коліні, навіть без фільтра по cell_max.

Пише ЛИШЕ якщо на диску ще нема top_spread_last для цього addr (TopSpreadTracker.
seed_last сам це перевіряє) — безпечно перезапускати, не затре живі дані.

HA-креди: env HA_URL / HA_TOKEN, або файл .env поряд зі скриптом. Entity ID
сенсорів дельти на пак — через env HA_DELTA_SENSOR_<addr> (напр.
HA_DELTA_SENSOR_0=sensor.batareia_deye_bms_delta_komirok); дефолти нижче
відповідають двопаковій конфігурації з наших прикладів — зміни під свою.

Запуск: python3 src/top_spread_seed_from_ha.py
"""
import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from top_spread import TopSpreadTracker                           # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
HOURS_BACK = 48

# addr -> (entity_id дельти, людська назва для логу). Перевизначається через
# env HA_DELTA_SENSOR_<addr> / HA_DELTA_LABEL_<addr>.
DELTA_SENSORS = {
    0: (os.environ.get("HA_DELTA_SENSOR_0", "sensor.batareia_deye_bms_delta_komirok"),
        os.environ.get("HA_DELTA_LABEL_0", "pack 0")),
    1: (os.environ.get("HA_DELTA_SENSOR_1", "sensor.batareia_deye_bms_2_delta_komirok"),
        os.environ.get("HA_DELTA_LABEL_1", "pack 1")),
}


def load_env():
    """env-змінні мають пріоритет; .env поряд зі скриптом — fallback."""
    env = dict(os.environ)
    dotenv = HERE / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    if "HA_URL" not in env or "HA_TOKEN" not in env:
        raise SystemExit(
            "Задай HA_URL і HA_TOKEN через env або .env поряд зі скриптом.")
    return env


def fetch_history(url, token, entity_id, start, end):
    q = urllib.parse.urlencode({
        "filter_entity_id": entity_id,
        "end_time": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "minimal_response": "",
        "no_attributes": "",
    })
    full = f"{url}/api/history/period/{start.strftime('%Y-%m-%dT%H:%M:%SZ')}?{q}"
    req = urllib.request.Request(full, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    env = load_env()
    url = env["HA_URL"].strip().rstrip("/")
    token = env["HA_TOKEN"].strip()
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=HOURS_BACK)

    tracker = TopSpreadTracker()

    for addr, (entity_id, label) in DELTA_SENSORS.items():
        try:
            series = fetch_history(url, token, entity_id, start, end)
        except Exception as e:                                   # noqa: BLE001
            print("[seed] %s (addr %d): помилка запиту історії: %s" % (label, addr, e))
            continue
        points = series[0] if series else []
        best_mv, best_at = None, None
        for p in points:
            try:
                v = float(p["state"])
            except (KeyError, TypeError, ValueError):
                continue
            if best_mv is None or v > best_mv:
                best_mv = v
                best_at = p.get("last_changed") or p.get("last_updated")
        if best_mv is None:
            print("[seed] %s (addr %d): в історії за %d год не знайдено жодної придатної точки"
                  % (label, addr, HOURS_BACK))
            continue
        tracker.seed_last(addr, best_mv, at=best_at)
        print("[seed] %s (addr %d): top_spread_last seed = %.1f мВ (%s), %d точок за %d год"
              % (label, addr, best_mv, best_at, len(points), HOURS_BACK))

    print("[seed] готово →", tracker._path)


if __name__ == "__main__":
    main()
