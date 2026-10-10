#!/usr/bin/env python3
"""battery_eta_outage — парсинг вікон відключень ДТЕК (атрибут `windows` у
binary_sensor.outage_definite_24h/outage_possible_24h, формат «DD.MM HH:00–HH:00 (текст)»).
Чисті функції, без мережі/IO — винесено з battery_eta_model.py окремо (інша царина:
не навчання/прогноз батареї, а читання чужого сторожового пакета ДТЕК).
"""
import re
from datetime import date, datetime, timedelta, time

_WIN_RE = re.compile(r"(\d{2})\.(\d{2})\s+(\d{2}):(\d{2})\s*[–-]\s*(\d{2}):(\d{2})\s*\((.*)\)")


def parse_outage_window_line(line, ref_year):
    """«DD.MM HH:00–HH:00 (текст)» → {"start","end","note","is_outage"} або None.
    ⚠️ Сенсор outage_definite_24h/possible_24h перелічує ЛИШЕ вікна відключень, а текст у
    дужках — ТИП відключення: «немає» = світла немає всю годину, «перші/другі 30хв» —
    половина години, «можливо…» — можливе. Тож кожен рядок — відключення."""
    mo = _WIN_RE.match(line.strip())
    if not mo:
        return None
    dd, mm, h1, mi1, h2, mi2, note = mo.groups()
    d = date(ref_year, int(mm), int(dd))
    start = datetime.combine(d, time(int(h1) % 24, int(mi1)))
    end = datetime.combine(d, time(int(h2) % 24, int(mi2)))
    if end <= start:
        end += timedelta(days=1)
    return {"start": start, "end": end, "note": note, "is_outage": True}


def next_outage_block(windows_strs, now_local):
    """(start, end) найближчого суцільного блоку відключення, що ще не закінчився, або None."""
    items = [parse_outage_window_line(s, now_local.year) for s in (windows_strs or [])]
    items = sorted((it for it in items if it and it["is_outage"] and it["end"] > now_local),
                   key=lambda it: it["start"])
    if not items:
        return None
    start, end = items[0]["start"], items[0]["end"]
    for it in items[1:]:
        if it["start"] <= end:
            end = max(end, it["end"])
        else:
            break
    return start, end


def next_outage_block_end(windows_strs, now_local):
    """Кінець найближчого суцільного (contiguous, година-в-годину) блоку відключення,
    що ще не закінчився — або None, якщо найближчим часом відключень не заплановано."""
    items = [parse_outage_window_line(s, now_local.year) for s in (windows_strs or [])]
    items = [it for it in items if it]
    items.sort(key=lambda it: it["start"])
    block_end = None
    in_block = False
    for it in items:
        if it["is_outage"] and it["end"] > now_local:
            if not in_block:
                in_block = True
                block_end = it["end"]
            elif block_end is not None and it["start"] <= block_end:
                block_end = it["end"]
            else:
                break
        elif in_block:
            break
    return block_end
