#!/usr/bin/env python3
"""jbd_analyze — pulls per-cell BMS history from the Home Assistant REST API
and classifies whether a pack's cell-voltage spread is capacity-driven (holds
steady at rest) or resistance-driven (grows under load, relaxes at rest).

Тягне історію 16 комірок + струм BMS + SOC за N діб, ресемплить на сітку і рахує:
- хто «втікач» (найвища/найнижча комірка, як часто);
- 🔑 розкид у СПОКОЇ (|I|<порог) vs під ЗАРЯДОМ vs РОЗРЯДОМ — відповідь «ємність
  чи опір»: якщо розкид тримається у спокої → різниця ЄМНОСТІ/SOC; якщо росте лише
  під струмом і релаксує у спокої → ВНУТРІШНІЙ ОПІР;
- тренд дельти в часі (як діяли наші втручання);
- поведінку на верхівці заряду (де саме комірки розʼїжджаються).

HA-креди: env HA_URL / HA_TOKEN, або файл .env поряд зі скриптом (той самий
формат, що й jbd2mqtt.env: KEY=VALUE, # — коментар). Entity ID сенсорів
налаштовуються через HA_BMS_ENTITY_PREFIX (дефолт "batareia_deye_bms" — звір зі
своєю інсталяцією HA).

Запуск: ./jbd_analyze.py [--days 7] [--rest-a 3]
"""
import argparse
import datetime
import json
import os
import pathlib
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent


def load_env():
    """env-змінні мають пріоритет; .env поряд зі скриптом — fallback."""
    env = dict(os.environ)
    dotenv = HERE / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


ENV = load_env()
# Перевірка HA_URL/HA_TOKEN — ЛІНИВА (у main(), після argparse), щоб --help
# працював і без виставлених кредів.
U = ENV.get("HA_URL", "").strip().rstrip("/")
T = ENV.get("HA_TOKEN", "").strip()

BMS = ENV.get("HA_BMS_ENTITY_PREFIX", "batareia_deye_bms")
CELLS = ["sensor.%s_komirka_%d" % (BMS, i) for i in range(1, 17)]
CUR = "sensor.%s_strum" % BMS            # струм: + заряд / − розряд
SOCK = "sensor.%s_zariad" % BMS          # SOC від BMS
DELTA = "sensor.%s_delta_komirok" % BMS


def hist(entities, start, end):
    q = urllib.parse.urlencode({
        "filter_entity_id": ",".join(entities),
        "minimal_response": "", "no_attributes": "",
        "end_time": end.isoformat()})
    req = urllib.request.Request(
        U + "/api/history/period/" + urllib.parse.quote(start.isoformat()) + "?" + q,
        headers={"Authorization": "Bearer " + T})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def series(block):
    """[(datetime, float)] відсортовано, ігноруючи unknown/unavailable."""
    out = []
    for s in block:
        v = s.get("state")
        if v in (None, "unknown", "unavailable"):
            continue
        try:
            val = float(v)
        except ValueError:
            continue
        ts = s.get("last_changed") or s.get("last_updated")
        out.append((datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")), val))
    out.sort()
    return out


def ffill(ser, grid):
    """Протягнути останнє відоме значення на сітку."""
    out, i, last = [], 0, None
    for g in grid:
        while i < len(ser) and ser[i][0] <= g:
            last = ser[i][1]
            i += 1
        out.append(last)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--rest-a", type=float, default=3.0, help="поріг |струму| для 'спокою', А")
    ap.add_argument("--step-min", type=int, default=5)
    a = ap.parse_args()

    if not U or not T:
        raise SystemExit(
            "Задай HA_URL і HA_TOKEN через env або .env поряд зі скриптом "
            "(довгоживучий токен HA: профіль → Security → Long-Lived Access Tokens).")

    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=a.days)
    ents = CELLS + [CUR, SOCK, DELTA]
    raw = hist(ents, start, end)
    by = {}
    for block in raw:
        if not block:
            continue
        eid = block[0].get("entity_id")
        by[eid] = series(block)
    # сітка
    grid = []
    t = start
    while t <= end:
        grid.append(t)
        t += datetime.timedelta(minutes=a.step_min)
    cellsf = [ffill(by.get(c, []), grid) for c in CELLS]
    curf = ffill(by.get(CUR, []), grid)
    socf = ffill(by.get(SOCK, []), grid)

    # зібрати валідні точки (усі 16 комірок відомі)
    rows = []
    for k in range(len(grid)):
        cv = [cellsf[i][k] for i in range(16)]
        if any(v is None for v in cv):
            continue
        mv = [v * 1000 if v < 10 else v for v in cv]
        rows.append({"t": grid[k], "mv": mv, "cur": curf[k], "soc": socf[k],
                     "hi": mv.index(max(mv)), "lo": mv.index(min(mv)),
                     "spread": max(mv) - min(mv)})
    if not rows:
        print("Нема даних за період.")
        return

    n = len(rows)
    hours = n * a.step_min / 60.0
    from collections import Counter
    hic = Counter(r["hi"] + 1 for r in rows)
    loc = Counter(r["lo"] + 1 for r in rows)
    avg = [sum(r["mv"][i] for r in rows) / n for i in range(16)]

    # регіми по струму
    def bucket(pred):
        s = [r["spread"] for r in rows if pred(r["cur"])]
        return (len(s), sum(s) / len(s)) if s else (0, 0)
    rest_n, rest_sp = bucket(lambda c: c is not None and abs(c) < a.rest_a)
    chg_n, chg_sp = bucket(lambda c: c is not None and c >= a.rest_a)
    dis_n, dis_sp = bucket(lambda c: c is not None and c <= -a.rest_a)

    # розкид на верхівці (SOC>=95) у спокої vs весь час
    top_rest = [r["spread"] for r in rows
                if r["soc"] is not None and r["soc"] >= 95
                and r["cur"] is not None and abs(r["cur"]) < a.rest_a]

    # тренд дельти: перша/остання/мін/макс + по добах
    first, last = rows[0]["spread"], rows[-1]["spread"]
    dmin = min(r["spread"] for r in rows)
    dmax = max(r["spread"] for r in rows)

    print("=" * 60)
    print("АНАЛІЗ РОЗБАЛАНСУ КОМІРОК (історія HA, %.1f діб, %d точок/%dхв)" % (a.days, n, a.step_min))
    print("=" * 60)
    print("\n[ВТІКАЧІ]")
    print("  найвища комірка (%% часу):", ", ".join("№%d=%d%%" % (c, 100 * k // n) for c, k in hic.most_common(4)))
    print("  найнижча комірка (%% часу):", ", ".join("№%d=%d%%" % (c, 100 * k // n) for c, k in loc.most_common(4)))
    hi_by_avg = max(range(16), key=lambda i: avg[i]) + 1
    lo_by_avg = min(range(16), key=lambda i: avg[i]) + 1
    print("  за СЕРЕДНЬОЮ напругою: найвища №%d (%.0f мВ), найнижча №%d (%.0f мВ), розкид середніх %.0f мВ"
          % (hi_by_avg, avg[hi_by_avg - 1], lo_by_avg, avg[lo_by_avg - 1], avg[hi_by_avg - 1] - avg[lo_by_avg - 1]))

    print("\n[🔑 ЄМНІСТЬ vs ОПІР] середній розкид за режимом струму (поріг %.0f А):" % a.rest_a)
    print("  СПОКІЙ  (|I|<%.0f): розкид %.0f мВ  (%d точок)" % (a.rest_a, rest_sp, rest_n))
    print("  ЗАРЯД   (I>%.0f):   розкид %.0f мВ  (%d точок)" % (a.rest_a, chg_sp, chg_n))
    print("  РОЗРЯД  (I<-%.0f):  розкид %.0f мВ  (%d точок)" % (a.rest_a, dis_sp, dis_n))
    verdict = "недостатньо даних"
    if rest_n and chg_n:
        if chg_sp > rest_sp * 1.4:
            verdict = "розкид РОСТЕ під струмом і менший у спокої → є компонент ВНУТРІШНЬОГО ОПОРУ"
        elif rest_sp > 15:
            verdict = "розкид ТРИМАЄТЬСЯ у спокої → це різниця ЄМНОСТІ/SOC (опір ні до чого)"
        else:
            verdict = "розкид малий скрізь → пак практично зведений"
    print("  ВЕРДИКТ:", verdict)
    if top_rest:
        print("  розкид на ВЕРХІВЦІ (SOC>=95) у спокої: %.0f мВ (%d точок) — саме тут видно правду"
              % (sum(top_rest) / len(top_rest), len(top_rest)))

    print("\n[ТРЕНД ДЕЛЬТИ за період]")
    print("  початок %.0f → кінець %.0f мВ · мін %.0f · макс %.0f" % (first, last, dmin, dmax))
    # по добах
    days = {}
    for r in rows:
        d = r["t"].astimezone().date().isoformat()
        days.setdefault(d, []).append(r["spread"])
    for d in sorted(days):
        v = days[d]
        print("   %s: сер %.0f, мін %.0f, макс %.0f мВ (%d т.)" % (d, sum(v) / len(v), min(v), max(v), len(v)))

    print("\n[СЕРЕДНІ НАПРУГИ КОМІРОК, мВ]")
    line = "  " + "  ".join("№%d=%.0f" % (i + 1, avg[i]) for i in range(16))
    print(line)


if __name__ == "__main__":
    main()
