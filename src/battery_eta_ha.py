#!/usr/bin/env python3
"""battery_eta_ha — тонкий REST-клієнт Home Assistant для battery_eta.*: лише GET
(стан зараз + історія по добі), жодних сервіс-викликів/запису — ТІЛЬКИ ЧИТАННЯ.

Minimal read-only HA REST client (current states + per-day history) shared by the
live daemon (battery_eta.py) and the archive builder (battery_eta_archive.py).
"""
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from battery_eta_config import HA_TOKEN, HA_URL


def ha_get(path, timeout=30):
    req = urllib.request.Request(HA_URL + path, headers={"Authorization": "Bearer " + HA_TOKEN})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ha_states_all():
    return ha_get("/api/states", timeout=30)


def ha_history_period(entities, start_utc, end_utc):
    """Один виклик /api/history/period — РІВНО за одну добу (чи менше), інакше HA
    повертає лише першу добу вікна. end_time ОБОВ'ЯЗКОВО url-encoded."""
    q = urllib.parse.urlencode({
        "filter_entity_id": ",".join(entities),
        "minimal_response": "", "no_attributes": "",
        "end_time": end_utc.isoformat()})
    path = "/api/history/period/" + urllib.parse.quote(start_utc.isoformat()) + "?" + q
    return ha_get(path, timeout=180)


def ha_history_range(entities, start_utc, end_utc):
    """Тягне історію ПО ДОБІ (HA-граблі: без цього віддає лише першу добу вікна),
    мерджить у {entity_id: [(dt_utc, state_str), ...]}."""
    by = {}
    t = start_utc
    while t < end_utc:
        te = min(t + timedelta(days=1), end_utc)
        for block in ha_history_period(entities, t, te):
            if not block:
                continue
            eid = block[0].get("entity_id")
            for s in block:
                v = s.get("state")
                ts = s.get("last_changed") or s.get("last_updated")
                if ts is None:
                    continue
                by.setdefault(eid, []).append(
                    (datetime.fromisoformat(ts.replace("Z", "+00:00")), v))
        t = te
    for eid in by:
        by[eid].sort(key=lambda p: p[0])
    return by
