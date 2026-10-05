#!/usr/bin/env python3
"""jbd_bms — reads a JBD/Jiabaida UP16S-series LiFePO4 BMS pack directly over RS485.
Returns everything the DD protocol exposes (16 cell voltages, balancing state,
cycles, real capacity, SOH, protections) — the per-cell detail a CAN/Pylon
inverter link to the same BMS never forwards.

Навіщо: інвертор по CAN/Pylon віддає лише агрегати (SOC, напруга, струм, ліміти) —
покомірок там немає ФІЗИЧНО, це стеля протоколу. Рідний протокол JBD по RS485
дає все: 16 комірок, баланс, цикли, реальну ємність, SOH, захисти.

Підключення (розпіновка з даташита JBD-UP16S010, розділ 7 — звір зі своєю моделлю):
    RJ45 пін 2 (або 7) → A     |  9600 8N1
    RJ45 пін 1 (або 8) → B     |  DIP на BMS має бути 0000 (MASTER)
    RJ45 пін 3 (або 6) → GND   |  роз'єм RS485, НЕ «Parallel»!

Використання:
  ./jbd_bms.py                      # усе: пакет + комірки
  ./jbd_bms.py --cells              # тільки напруги комірок
  ./jbd_bms.py --json               # машиночитано (для MQTT-моста)
  ./jbd_bms.py --port /dev/ttyUSB1
  ./jbd_bms.py --addr 1             # другий пак на тій самій шині

Порт за замовчуванням бере env BMS_PORT, якщо він заданий (інакше /dev/ttyUSB0).
"""
import argparse
import json
import os
import struct
import sys
import time

try:
    import serial
except ImportError:                                              # pragma: no cover
    sys.exit("Немає pyserial. Запускай через: uv run --with pyserial jbd_bms.py")

DEFAULT_PORT = os.environ.get("BMS_PORT", "/dev/ttyUSB0")
BAUD = 9600          # даташит: «The recognized baud rate is 9600bps»
START, END = 0xDD, 0x77
READ = 0xA5
CMD_BASIC, CMD_CELLS, CMD_NAME = 0x03, 0x04, 0x05

# Прапорці захистів (регістр protection_state, біти 0..12)
PROTECTION_BITS = [
    "перенапруга комірки", "недонапруга комірки", "перенапруга пакета",
    "недонапруга пакета", "перегрів при заряді", "переохолодження при заряді",
    "перегрів при розряді", "переохолодження при розряді", "надструм заряду",
    "надструм розряду", "коротке замикання", "помилка АЦП", "заблоковано ПЗ",
]


def build(cmd: int, addr: int = 0) -> bytes:
    """Кадр запиту для UP-серії.

    ⚠️ UP16S010 НЕсумісна зі звичайним протоколом JBD: після стартового 0xDD
    додано байт АДРЕСИ (для multidrop-шини паралельних паків), і контрольна
    сума рахується від нього, тобто по чотирьох байтах замість двох.
    Класичний семибайтовий кадр `DD A5 03 00 FF FD 77` ця модель мовчки
    відкидає — саме на цьому ми згаяли вечір 2026-08-19.
    Формат: DD [addr] A5 [cmd] 00 [crc_hi] [crc_lo] 77
    """
    body = bytes([addr, READ, cmd, 0x00])
    crc = (0x10000 - sum(body)) & 0xFFFF          # checksum = −sum
    return bytes([START]) + body + struct.pack(">H", crc) + bytes([END])


def request(ser: serial.Serial, cmd: int, retries: int = 3, addr: int = 0) -> bytes:
    """Запит із повторами: BMS засинає через 5 хв без обміну (даташит 4.8),
    і перший пакет після сну часто губиться — це не помилка лінії."""
    causes = []
    for attempt in range(retries):
        ser.reset_input_buffer()
        ser.write(build(cmd, addr))
        ser.flush()
        time.sleep(0.25)
        raw = ser.read(256)
        if not raw:
            causes.append("тиша")
        elif len(raw) >= 8 and raw[0] == START and raw[-1] == END:
            # відповідь: DD [addr] [cmd] [status] [len] [дані…] [crc_hi] [crc_lo] 77
            # ⚠️ Y-шина з двома паками: биті/зсунуті кадри інколи мають цілі краї
            # DD…77 (баг «SOC=3%/183%» 2026-09-18), тому звіряємо echo-адресу,
            # повноту кадру і CRC (сума від байта адреси включно — перевірено живими
            # кадрами addr=1); битий кадр = класифікований retry, не дані.
            # Статус-байт дивимось ОСТАННІМ, лише в цілому кадрі: у битому на
            # позиції 3 лежить сміття, і ранній raise зʼїдав ретраї (2026-09-22).
            length = raw[4]
            if raw[1] != addr:
                causes.append("чужа адреса %d" % raw[1])
            elif len(raw) < 5 + length + 3:
                causes.append("обрізаний кадр %dб" % len(raw))
            elif (struct.unpack(">H", raw[5 + length:7 + length])[0]
                    != (0x10000 - sum(raw[1:5 + length])) & 0xFFFF):
                causes.append("бита CRC")
            elif raw[3] != 0:
                # цілий кадр зі status≠0 — BMS реально відмовила (зайнята?);
                # дамп для розслідування, і пробуємо ще раз, а не падаємо
                causes.append("status=0x%02X кадр=%s" % (raw[3], raw[:24].hex()))
            else:
                return raw[5:5 + length]
        else:
            causes.append("сміття %dб" % len(raw))
        if attempt < retries - 1:
            time.sleep(0.4)
    raise RuntimeError(
        "немає відповіді на 0x%02X [%s]. Перевір: A/B, GND, DIP, роз'єм RS485"
        % (cmd, ", ".join(causes)))


def parse_basic(d: bytes) -> dict:
    """Розкладка блоку 0x03 для UP-серії.

    ⚠️ Відрізняється від класичної JBD (звірено з реалізацією stefan064, яка
    зроблена саме під UP): статус балансира займає 4 байти, на зміщенні 22
    лежить Alarm Status, а не кількість NTC, і є два окремі датчики —
    середовища та FET — перед блоком температур.

     0  2  напруга пакета      ×0.01      18  1  версія ПЗ
     2  2  струм (int16)       ×0.01      19  1  SOC
     4  2  залишкова ємність   ×0.01      20  1  бітмаска MOSFET
     6  2  номінальна ємність  ×0.01      21  1  кількість комірок
     8  2  цикли                          22  2  статус тривог
    10  2  дата виробництва               24  2  температура середовища
    12  4  статус балансира               26  2  температура FET
    16  2  статус захистів                28  1  кількість датчиків, далі по 2 байти
    """
    # Кадр із валідною CRC, але закоротким блоком даних — теж брак (ловили
    # «index out of range» у демоні): чесна помилка замість трейсбека.
    if len(d) < 28:
        raise RuntimeError("короткий блок basic: %d байт" % len(d))
    u16 = lambda i: struct.unpack(">H", d[i:i + 2])[0]           # noqa: E731
    kelvin = lambda v: round((v - 2731) / 10, 1)                 # noqa: E731

    volts = u16(0) / 100
    amps = struct.unpack(">h", d[2:4])[0] / 100                  # + заряд / − розряд
    # Байти 12-13 = комірки 1-16, 14-15 = 17-32 (мапа JBD). Раніше читалось як один
    # ">I" і біти 0-15 припадали на 17-32 → HA 7 діб показувала «—» замість балансу.
    bal_lo, bal_hi = struct.unpack(">HH", d[12:16])
    balance = bal_lo | bal_hi << 16                              # 32 біти, по комірці
    protection = u16(16)
    soc = d[19]
    fet = d[20]
    cell_count = d[21]
    alarms = u16(22)

    temps = []
    if len(d) > 28:
        n = min(d[28], 4)
        temps = [kelvin(u16(29 + i * 2)) for i in range(n)
                 if len(d) >= 31 + i * 2]

    nominal = u16(6) / 100
    return {
        "voltage": round(volts, 2),
        "current": round(amps, 2),
        "power": round(volts * amps),
        "soc": soc,
        "remaining_ah": round(u16(4) / 100, 2),
        "nominal_ah": round(nominal, 2),
        # SOH = скільки лишилось від паспортних 150 А·год
        "soh_percent": round(nominal / 150 * 100, 1) if nominal else None,
        "cycles": u16(8),
        "cell_count": cell_count,
        "software_version": "%d.%d" % (d[18] >> 4, d[18] & 0x0F),
        "temperatures": temps,
        "temp_ambient": kelvin(u16(24)) if len(d) > 25 else None,
        "temp_fet": kelvin(u16(26)) if len(d) > 27 else None,
        "charging": bool(fet & 0x01),
        "discharging": bool(fet & 0x02),
        "balancing_cells": [i + 1 for i in range(cell_count) if balance >> i & 1],
        "protections": [n for i, n in enumerate(PROTECTION_BITS) if protection >> i & 1],
        "alarm_bitmask": alarms,
    }


def parse_cells(d: bytes) -> list:
    # Довжину обрізаємо до парної: RS485 — спільна шина, і якщо демон jbd2mqtt
    # опитує батарею одночасно з ручним запуском, відповідь може прийти обрізаною.
    return [struct.unpack(">H", d[i:i + 2])[0] / 1000
            for i in range(0, len(d) - len(d) % 2, 2)]


def read_all(port: str, addr: int = 0) -> dict:
    with serial.Serial(port, BAUD, timeout=1.5) as ser:
        time.sleep(0.2)
        info = parse_basic(request(ser, CMD_BASIC, addr=addr))
        cells = parse_cells(request(ser, CMD_CELLS, addr=addr))
    # Сирий максимум = як бачить сама BMS (по ньому працюють її OVP і балансир).
    # Зараз збігається з реальним; лишено окремим полем після інциденту 2026-09-29
    # (збитий coef комірки №1 нової, вилікувано записом у DV/DP по подвоєній адресі).
    cell_max_raw = max(cells) if cells else None
    # Страховка поверх CRC: фізично неможливі значення (16S LFP 40-60 В,
    # SOC 0-100) = битий кадр, що дивом пройшов, — краще помилка, ніж
    # сміття в HA (історія зі стрибками SOC 3%/183% у графіках).
    if not (30.0 < info["voltage"] < 62.0) or not (0 <= info["soc"] <= 100):
        raise RuntimeError(
            "неправдоподібний кадр: U=%.2fВ SOC=%d%%" % (info["voltage"], info["soc"]))
    if cells:
        info["cells"] = cells
        info["cell_min"] = min(cells)
        info["cell_max"] = max(cells)
        info["cell_max_raw"] = cell_max_raw
        # Дельта — головний показник здоров'я пакета: балансир JBD пасивний,
        # 20-60 мА на 150 Ah, тож розбіжність понад ~50 мВ він уже не витягує.
        info["cell_delta_mv"] = round((max(cells) - min(cells)) * 1000, 1)
    return info


def main():
    ap = argparse.ArgumentParser(description="Читання JBD BMS по RS485")
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--cells", action="store_true", help="тільки напруги комірок")
    ap.add_argument("--json", action="store_true", help="вивід JSON")
    ap.add_argument("--addr", type=int, default=0,
                    help="адреса пака на шині (DIP на BMS; 0 = майстер)")
    args = ap.parse_args()

    data = read_all(args.port, args.addr)

    if args.json:
        print(json.dumps(data, ensure_ascii=False))
        return

    if not args.cells:
        print("── ПАКЕТ ─────────────────────────────────────")
        print("  напруга        %.2f В" % data["voltage"])
        print("  струм          %+.2f А  (%s)" % (
            data["current"],
            "заряд" if data["current"] > 0.05 else
            "розряд" if data["current"] < -0.05 else "спокій"))
        print("  потужність     %+d Вт" % data["power"])
        print("  SOC            %d %%" % data["soc"])
        print("  залишок        %.2f А·год з %.2f А·год" % (
            data["remaining_ah"], data["nominal_ah"]))
        if data.get("soh_percent"):
            print("  SOH            %.1f %% (від паспортних 150 А·год)" % data["soh_percent"])
        print("  циклів         %d" % data["cycles"])
        print("  температури    %s" % ", ".join("%.1f°C" % t for t in data["temperatures"]))
        print("  MOSFET         заряд:%s  розряд:%s" % (
            "on" if data["charging"] else "OFF",
            "on" if data["discharging"] else "OFF"))
        if data["protections"]:
            print("  ⚠️ ЗАХИСТИ     %s" % ", ".join(data["protections"]))
        if data["balancing_cells"]:
            print("  балансується   комірки %s" % data["balancing_cells"])

    if "cells" in data:
        print("── КОМІРКИ (%d) ──────────────────────────────" % len(data["cells"]))
        lo, hi = data["cell_min"], data["cell_max"]
        for i, v in enumerate(data["cells"], 1):
            mark = " ← мін" if v == lo else " ← макс" if v == hi else ""
            bar = "█" * int((v - 2.5) / 1.2 * 30) if v > 2.5 else ""
            print("  %2d  %.3f В  %-30s%s" % (i, v, bar, mark))
        print("  дельта %.1f мВ %s" % (
            data["cell_delta_mv"],
            "✅ норма" if data["cell_delta_mv"] < 30 else
            "⚠️ балансир не витягує" if data["cell_delta_mv"] < 100 else
            "🚨 велика розбіжність"))


if __name__ == "__main__":
    main()
