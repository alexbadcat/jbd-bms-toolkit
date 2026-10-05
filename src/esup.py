#!/usr/bin/env python3
"""esup — client for the second ("ES-UP" / Eco-Worthy Modbus-RTU) protocol some
newer JBD "polyglot" BMS firmwares speak alongside the classic DD frame; needed
because that firmware silently ignores DD writes to several service registers.

Навіщо окремий модуль поряд з jbd_bms.py: НОВА батарея (UP16S019 rev.2, addr=1)
розуміє ДВА протоколи на тій самій RS485-шині. Класичний DD-кадр (jbd_bms.py) з
неї читає телеметрію, але запис службових полів у DD залочений прошивкою (0x12,
калібрування 0xAE/0xAF — ack=OK і мовчки ігнорує). А от ES-UP-канал (FC 0x79)
пише БЕЗ жодного factory-паролю — саме ним рідний MJBDTools міняє параметри.
Стара батарея (addr=0) ES-UP НЕ розуміє (класична JBD) — тож колізій на шині нема.

Навіщо окремий модуль поряд з jbd_bms.py: НОВА батарея (UP16S019 rev.2, addr=1)
розуміє ДВА протоколи на тій самій RS485-шині. Класичний DD-кадр (jbd_bms.py) з
неї читає телеметрію, але запис службових полів у DD залочений прошивкою (0x12,
калібрування 0xAE/0xAF — ack=OK і мовчки ігнорує). А от ES-UP-канал (FC 0x79)
пише БЕЗ жодного factory-паролю — саме ним рідний MJBDTools міняє параметри.
Стара батарея (addr=0) ES-UP НЕ розуміє (класична JBD) — тож колізій на шині нема.

Формат кадрів (розвідано 2026-09-29, звірено з gist PhracturedBlue/7ef6195):
  ЧИТАННЯ 0x78: [addr][78][startHi][startLo][endHi][endLo][00 00][crcLo][crcHi]
    відповідь:  [addr][78][start:2][end:2][dlen:2][data…][crc:2]
  ЗАПИС  0x79: [addr][79][start:2][end:2][dlen:2][11 4A 42 44][payload][crc:2]
    dlen = 4 (маркер «JBD») + len(payload); маркер 11 4A 42 44 обовʼязковий.
  Дані MSB-first; CRC16 init=0xFFFF poly=0xA001 LSB-first (як у bms_probe_multi).

⚠️ Демон jbd2mqtt тримає шину — перед ES-UP-сесією зупини його (kill по pidfile),
після — підніми (start.sh). Одночасний доступ дає обрізані кадри.
"""
import struct
import time

import serial

BAUD = 9600
READ, WRITE = 0x78, 0x79
MARKER = b"\x11\x4A\x42\x44"

# Блок 0x2000 (Capacity), офсети U16 MSB-first — звірено читанням нової 2026-09-29.
CAP_NOMINAL, CAP_FULL, CAP_REMAIN, CAP_SOC = 0x00, 0x02, 0x04, 0x06

# Блок 0x3800 — калібрувальні gain-коефіцієнти (base 10000 = ×1.0000). ЗАПИС
# залочений у звичайному режимі (ack=OK, ігнор), відкривається лише в режимі
# DV/DP (0x2900=0x5A02). Ідентифіковано за ефектом на струмі 2026-09-29:
#   +0 = charge-current gain  (під зарядом зміна ×1.10 дала струм ×1.097 ✅)
#   +4 = discharge-current gain
#   +8..+38 = калібрування 16 комірок (близько 10000, напруги точні)
# Заводські: charge=10460, discharge=10452.
GAIN_CHARGE, GAIN_DISCHARGE = 0x00, 0x04
# Блок 0x1C00 (user-поля, пишуться звичайним 0x79): +4 BalanceV, +6 BalanceDiff, +12 FullAdjV,
# +14 FullAdjC, +120 SleepV, +122 SleepDelay, +124 BALANCE MODE, +126 RS485 type, +128 CAN type.
# BALANCE MODE (2026-10-01, живий тест у стані OVP): 0 = баланс ЛИШЕ під зарядом (заводське —
# у OVP/спокої балансир мовчить, саме коли розкид найбільший), 1 = балансує і без струму
# (статично). Виставлено 1 на новій. Регістр запису: 0x1C00 + 124 = 0x1C7C.
BALANCE_MODE_ADDR = 0x1C7C
# Лічильник циклів нової: читається у 0x3900+16 і 0x2000+10 (дзеркала), DD basic bytes 8-9 —
# теж звідси. ЗАПИС приймає лише 0x200A (0x2000+10); 0x3910 і DD 0x17 — ігноруються.
# 2026-09-29 мій factory reset (0x290E) обнулив його (було 10) → 01.10 повернуто 11.
CYCLES_ADDR = 0x200A
CELL_GAIN_OFF = 0x08                          # +8..+38 = 16 коефіцієнтів комірок (≈10006-10028)


def gain_write_addr(data_off: int) -> int:
    """Адреса ЗАПИСУ для офсету даних блоку 0x3800.

    ⚠️ У DV/DP-режимі прошивка UP16S019 адресує запис у 0x3800 НЕ байтами, а
    індексом u16: кадр на 0x3800+k лягає в офсет даних 2k. Інцидент 2026-09-29:
    запис «discharge-gain» на 0x3804 ліг у комірку №1 (+8, 10006→10452, показ
    +4.46%), «відкат» на 0x3808 — у комірку №5 (+16). Вилікувано записом
    10006 на 0x3804 і 10018 на 0x3808. Читання (0x78) — звичайне, байтове.
    """
    return 0x3800 + data_off // 2
DVDP_MODE, NORMAL_MODE = 0x5A02, 0x0000       # 0x2900 debug-режим


def crc16(d: bytes) -> bytes:
    crc = 0xFFFF
    for b in d:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


def read_block(ser: serial.Serial, start: int, end: int, addr: int = 1) -> "bytes | None":
    """Прочитати діапазон регістрів [start, end). None = немає валідної відповіді."""
    frame = bytes([addr, READ]) + struct.pack(">HH", start, end) + b"\x00\x00"
    frame += crc16(frame)
    ser.reset_input_buffer()
    ser.write(frame)
    ser.flush()
    time.sleep(0.45)
    raw = ser.read(600)
    if len(raw) < 10 or raw[1] != READ:
        return None
    dlen = struct.unpack(">H", raw[6:8])[0]
    if len(raw) < 8 + dlen + 2 or crc16(raw[:8 + dlen]) != raw[8 + dlen:10 + dlen]:
        return None
    return raw[8:8 + dlen]


def write_reg(ser: serial.Serial, start: int, end: int, payload: bytes,
              addr: int = 1) -> "bytes | None":
    """Записати payload у діапазон [start, end). Повертає сирий ack-кадр або None."""
    body = MARKER + payload
    frame = bytes([addr, WRITE]) + struct.pack(">HH", start, end)
    frame += struct.pack(">H", len(body)) + body
    frame += crc16(frame)
    ser.reset_input_buffer()
    ser.write(frame)
    ser.flush()
    time.sleep(0.6)
    return ser.read(64) or None


def read_gains(port: str, addr: int = 1) -> "dict | None":
    """Калібрувальні gain з блоку 0x3800: charge / discharge (base 10000)."""
    with serial.Serial(port, BAUD, timeout=1.5) as ser:
        time.sleep(0.2)
        d = read_block(ser, 0x3800, 0x3820, addr)
    if not d or len(d) < 8:
        return None
    u16 = lambda i: struct.unpack(">H", d[i:i + 2])[0]      # noqa: E731
    return {"charge": u16(GAIN_CHARGE), "discharge": u16(GAIN_DISCHARGE)}


def set_gain(port: str, which: int, value: int, addr: int = 1) -> dict:
    """Записати калібрувальний gain (which = GAIN_CHARGE|GAIN_DISCHARGE) через
    режим DV/DP (інакше залочено). Персист-верифікація свіжою сесією.

    ⚠️ КАЛІБРУВАННЯ БОЙОВОЇ BMS: міняє лише облік ємності (Аг/SOC), не захисти
    напруги. Роби бекап блоку 0x3800 перед серією. Крок консервативний —
    завищений gain даємо вниз, недокрут краще за перекрут, ітеруй по добових
    інтегралах струму (нетто за замкнутий цикл має бути ~0)."""
    with serial.Serial(port, BAUD, timeout=1.5) as ser:
        time.sleep(0.2)
        b = read_block(ser, 0x3800, 0x3820, addr)
        before = struct.unpack(">H", b[which:which + 2])[0] if b else None
        write_reg(ser, 0x2900, 0x2902, struct.pack(">H", DVDP_MODE), addr)
        time.sleep(0.4)
        wa = gain_write_addr(which)
        write_reg(ser, wa, wa + 2, struct.pack(">H", value), addr)
        time.sleep(0.4)
        write_reg(ser, 0x2900, 0x2902, struct.pack(">H", NORMAL_MODE), addr)
        time.sleep(0.4)
    time.sleep(1.0)
    with serial.Serial(port, BAUD, timeout=1.5) as ser:      # свіжа сесія = персист
        time.sleep(0.2)
        f = read_block(ser, 0x3800, 0x3820, addr)
        fresh = struct.unpack(">H", f[which:which + 2])[0] if f else None
    return {"before": before, "target": value, "fresh": fresh, "ok": fresh == value}


def set_cell_gain(port: str, cell_no: int, value: int, addr: int = 1) -> dict:
    """Коефіцієнт виміру напруги комірки cell_no (1..16), заводські ≈10006-10028.
    Той самий DV/DP-шлях, що й set_gain; адресація — див. gain_write_addr."""
    if not 1 <= cell_no <= 16:
        raise ValueError("cell_no 1..16")
    return set_gain(port, CELL_GAIN_OFF + 2 * (cell_no - 1), value, addr)


def read_capacity(port: str, addr: int = 1) -> "dict | None":
    """Nominal / Full / Remaining (А·год) і SOC (%) з блоку 0x2000."""
    with serial.Serial(port, BAUD, timeout=1.5) as ser:
        time.sleep(0.2)
        d = read_block(ser, 0x2000, 0x2040, addr)
    if not d or len(d) < 8:
        return None
    u16 = lambda i: struct.unpack(">H", d[i:i + 2])[0]      # noqa: E731
    return {
        "nominal_ah": u16(CAP_NOMINAL) / 100,
        "full_ah": u16(CAP_FULL) / 100,
        "remaining_ah": u16(CAP_REMAIN) / 100,
        "soc": u16(CAP_SOC) / 100,
    }


def set_remaining_ah(port: str, ah: float, addr: int = 1) -> dict:
    """Переписати лічильник Remaining Capacity (А·год) — SOC = Remain/Full BMS
    перерахує сама. Верифікація читанням назад (±0.1 А·год допуск на дрейф під
    струмом). НЕ калібрування: чіпає лише лічильник, не gain і не захисти."""
    raw = max(0, min(0xFFFF, round(ah * 100)))
    with serial.Serial(port, BAUD, timeout=1.5) as ser:
        time.sleep(0.2)
        before = read_block(ser, 0x2000, 0x2040, addr)
        ack = write_reg(ser, 0x2000 + CAP_REMAIN, 0x2000 + CAP_REMAIN + 2,
                        struct.pack(">H", raw), addr)
        time.sleep(0.5)
        after = read_block(ser, 0x2000, 0x2040, addr)
    u16 = lambda d, i: struct.unpack(">H", d[i:i + 2])[0]   # noqa: E731
    got = u16(after, CAP_REMAIN) if after else None
    return {
        "before_ah": u16(before, CAP_REMAIN) / 100 if before else None,
        "target_ah": raw / 100,
        "after_ah": got / 100 if got is not None else None,
        "ack": ack.hex() if ack else None,
        "ok": got is not None and abs(got - raw) <= 10,
    }
