#!/usr/bin/env python3
"""jbd_settings — dumps and writes JBD BMS (UP16S-series) service registers over
RS485 using the DD frame (protection thresholds, capacity, balancer parameters),
with an explicit writable-register allowlist, save-and-reverify semantics, and a
JSON backup/restore workflow for anything touched before a write.

Читає сервісні регістри (пороги захисту, ємність, параметри балансира) і зберігає
JSON — страховка перед будь-якою зміною параметрів і база для аналізу.
Уміє й ПИСАТИ окремий регістр (--set), бо наш блок говорить DD-кадром JBD, а НЕ
Modbus (перевірено 2026-08-30: на еталонний Modbus-кадр 0x78 блок відповідає 0 байт),
тож задокументований спільнотою Modbus-варіант (gist ECO-WORTHY, JBD-ES-UP) нам не
підходить — пишемо тим же DD-кадром, що й читаємо.

⚠️ БЕЗПЕКА ЧИТАННЯ: дамп ТІЛЬКИ ЧИТАЄ; якщо потрібен сервісний режим, вихід — БЕЗ
збереження (0x0000). ⚠️ БЕЗПЕКА ЗАПИСУ: --set вимагає --yes; пише лише регістри з
allowlist WRITABLE; вихід-зі-збереженням (0x2828) і перевірка персиста свіжою сесією;
роби бекап (jbd_settings.py --save) ПЕРЕД записом. finally-вихід не лишає BMS у сервісі.

Використання:
  ./jbd_settings.py                    # дамп на екран
  ./jbd_settings.py --save FILE        # + зберегти JSON (бекап перед записом!)
  ./jbd_settings.py --raw              # усі регістри як є, без розшифровки
  ./jbd_settings.py --read 0x2D        # прочитати ДОВІЛЬНИЙ регістр (hex/dec/біти)
  ./jbd_settings.py --set 0x2A=3400 --yes   # записати регістр (мВ), з верифікацією

Порт за замовчуванням бере env BMS_PORT, якщо він заданий (інакше /dev/ttyUSB0).
"""
import argparse
import datetime
import json
import os
import struct
import sys
import time

try:
    import serial
except ImportError:                                              # pragma: no cover
    sys.exit("Немає pyserial. Запускай: uv run --with pyserial jbd_settings.py")

PORT, BAUD = os.environ.get("BMS_PORT", "/dev/ttyUSB0"), 9600
START, END = 0xDD, 0x77
READ, WRITE = 0xA5, 0x5A
FACTORY_ENTER, FACTORY_EXIT = 0x00, 0x01
PASS_ENTER = 0x5678          # пароль входу в сервісний режим
PASS_EXIT_NOSAVE = 0x0000    # вихід БЕЗ збереження
PASS_EXIT_SAVE = 0x2828      # вихід ЗІ ЗБЕРЕЖЕННЯМ у EEPROM (класичний JBD-опкод)

# Регістри, які дозволено писати (свідомий allowlist — щоб не зачепити захисти
# випадковим fat-finger). Розширюй обдумано. Кодування — VMV (мВ) для всіх нижче.
WRITABLE = {
    0x2A: "Старт балансування (напруга комірки), мВ",
    0x2B: "Поріг дельти для балансування, мВ",
    0x12: "Напруга 100% (комірка), мВ",
    0x13: "Напруга розрядженої (комірка), мВ",
    # FuncConfig (біти): 2=balance_en, 3=chg_balance_en (баланс ПІД ЗАРЯДОМ), 4/5=LED.
    # 2026-09-29: у старої стояло 471 (біт3=0) — балансир жив лише «статично» у спокої,
    # де LFP і так сходиться; під зарядом (де розкид 30-40 мВ) не працював → 479.
    0x2D: "FuncConfig (бітова маска)",
    # Відпускання OVP комірки: заводські 3380 → після спрацювання пак стояв
    # заблокованим до помітного розряду (24.09). 3500 = релаксація за хвилини.
    0x25: "Перенапруга комірки — відпускання, мВ",
    # Температури — СИРІ одиниці 0.1 K (2731 = 0 °C, 2781 = 5 °C)! LFP нижче 0 °C
    # не заряджають (плейтинг): заводські −5/0 °C → 0/+5 °C.
    0x1A: "Переохолодження при заряді (0.1 K)",
    0x1B: "Переохолодження при заряді — відпускання (0.1 K)",
}

# Калібрувальні тригери (мапа Overkill Solar JBD_REGISTER_MAP). Це НЕ конфіги:
# запис = дія «поточний реальний струм дорівнює цьому значенню», BMS сама
# перераховує gain. Read-back не визначений (читання віддає сміття/напругу),
# тому верифікація — ЗА ЕФЕКТОМ: вимірюваний струм після запису має зійтися
# із записаним. Одиниці 10 мА; писати ПІД СТАБІЛЬНИМ струмом того ж напряму!
# Привід: дрейф SOC №2 ~2%/добу — її charge-gain ~+1.5%, discharge ~−1.5%
# (добові інтеграли 2026-09-28: заряд 96.7 vs 95.1, розряд 93.8 vs 95.8 Аг).
CALIB = {
    0xAD: "Калібрування НУЛЯ струму (писати 0 при I=0!)",
    0xAE: "Калібрування струму ЗАРЯДУ, 10 мА (писати РЕАЛЬНИЙ струм під зарядом)",
    0xAF: "Калібрування струму РОЗРЯДУ, 10 мА (писати РЕАЛЬНИЙ струм під розрядом)",
}

# (регістр, назва, як розшифрувати сире значення)
V100 = lambda v: "%.2f В" % (v / 100)      # noqa: E731  напруга пакета, 10 мВ
VMV = lambda v: "%d мВ" % v                # noqa: E731  напруга комірки, мВ
AMP = lambda v: "%.2f А" % (v / 100)       # noqa: E731
TEMP = lambda v: "%.1f °C" % ((v - 2731) / 10)  # noqa: E731
AH = lambda v: "%.2f А·год" % (v / 100)    # noqa: E731
MS = lambda v: "%d с" % v                  # noqa: E731
RAW = lambda v: "0x%04X (%d)" % (v, v)     # noqa: E731

REGISTERS = [
    (0x10, "Проектна ємність", AH),
    (0x11, "Ємність циклу", AH),
    (0x12, "Напруга 100% (комірка)", VMV),
    (0x13, "Напруга розрядженої (комірка)", VMV),
    (0x14, "Саморозряд", RAW),
    (0x15, "Дата виробництва", RAW),
    (0x16, "Серійний номер", RAW),
    (0x17, "Лічильник циклів", RAW),
    (0x18, "Перегрів при заряді", TEMP),
    (0x19, "Перегрів при заряді — відпускання", TEMP),
    (0x1A, "Переохолодження при заряді", TEMP),
    (0x1B, "Переохолодження при заряді — відпускання", TEMP),
    (0x1C, "Перегрів при розряді", TEMP),
    (0x1D, "Перегрів при розряді — відпускання", TEMP),
    (0x1E, "Переохолодження при розряді", TEMP),
    (0x1F, "Переохолодження при розряді — відпускання", TEMP),
    (0x20, "Перенапруга пакета", V100),
    (0x21, "Перенапруга пакета — відпускання", V100),
    (0x22, "Недонапруга пакета", V100),
    (0x23, "Недонапруга пакета — відпускання", V100),
    (0x24, "Перенапруга комірки", VMV),
    (0x25, "Перенапруга комірки — відпускання", VMV),
    (0x26, "Недонапруга комірки", VMV),
    (0x27, "Недонапруга комірки — відпускання", VMV),
    (0x28, "Надструм заряду", AMP),
    (0x29, "Надструм розряду", AMP),
    (0x2A, "🔑 Старт балансування (напруга комірки)", VMV),
    (0x2B, "🔑 Поріг дельти для балансування", VMV),
    (0x2C, "Затримка перенапруги пакета", MS),
    (0x2D, "FuncConfig: біт2 balance_en, біт3 chg_balance_en (баланс під зарядом), біт4/5 LED", RAW),
    (0x2E, "Затримка перенапруги комірки", MS),
    (0x2F, "Затримка недонапруги комірки", MS),
    (0x30, "Затримка надструму заряду", MS),
    (0x31, "Затримка надструму розряду", MS),
    # ⚠️ З 0x32 починаються НЕ налаштування, а дзеркало напруг комірок:
    # у дампі 2026-08-19 регістр 0x32 віддав 3329 мВ — рівно напругу комірки 3
    # тієї ж миті. Тому далі не читаємо, щоб не видавати сміття за параметри.
]


def build(cmd, addr=0, action=READ, payload=b""):
    """Кадр UP-серії: DD [addr] [action] [cmd] [len] [дані] [crc_hi] [crc_lo] 77."""
    body = bytes([addr, action, cmd, len(payload)]) + payload
    crc = (0x10000 - sum(body)) & 0xFFFF
    return bytes([START]) + body + struct.pack(">H", crc) + bytes([END])


def talk(ser, cmd, addr=0, action=READ, payload=b"", retries=2):
    for attempt in range(retries):
        ser.reset_input_buffer()
        ser.write(build(cmd, addr, action, payload))
        ser.flush()
        time.sleep(0.25)
        raw = ser.read(256)
        if len(raw) >= 8 and raw[0] == START and raw[-1] == END:
            if raw[3] != 0:
                return None            # BMS відповіла «помилка» — регістр недоступний
            return raw[5:5 + raw[4]]
        if attempt < retries - 1:
            time.sleep(0.3)
    return None


def read_all(port, addr, use_factory):
    out, entered = {}, False
    with serial.Serial(port, BAUD, timeout=1.2) as ser:
        time.sleep(0.2)
        try:
            if use_factory:
                talk(ser, FACTORY_ENTER, addr, WRITE, struct.pack(">H", PASS_ENTER))
                entered = True
                time.sleep(0.3)
            for reg, name, fmt in REGISTERS:
                d = talk(ser, reg, addr)
                if d and len(d) >= 2:
                    out[reg] = struct.unpack(">H", d[:2])[0]
                time.sleep(0.06)
        finally:
            if entered:
                # ВИХІД БЕЗ ЗБЕРЕЖЕННЯ — гарантія, що нічого не змінилось
                talk(ser, FACTORY_EXIT, addr, WRITE,
                     struct.pack(">H", PASS_EXIT_NOSAVE))
    return out


def read_reg(ser, reg, addr=0):
    d = talk(ser, reg, addr)
    return struct.unpack(">H", d[:2])[0] if d and len(d) >= 2 else None


def write_reg(port, addr, reg, value):
    """Запис одного регістра в сервісному режимі з виходом ЗІ ЗБЕРЕЖЕННЯМ.

    Повертає dict із before / accepted / in_ram — верифікацію персиста робить
    викликач окремим свіжим read_all (щоб перевірити, що save реально ліг у EEPROM).
    ⚠️ Вихід-зі-збереженням стоїть у finally: навіть при винятку не лишаємо BMS
    у сервісному режимі. Якщо запис не прийнявся — цінності save не буде, значення
    просто лишиться старим (безпечно).
    """
    res = {"before": None, "accepted": False, "in_ram": None}
    with serial.Serial(port, BAUD, timeout=1.2) as ser:
        time.sleep(0.2)
        entered = False
        try:
            talk(ser, FACTORY_ENTER, addr, WRITE, struct.pack(">H", PASS_ENTER))
            entered = True
            time.sleep(0.3)
            res["before"] = read_reg(ser, reg, addr)
            time.sleep(0.1)
            ack = talk(ser, reg, addr, WRITE, struct.pack(">H", value))
            res["accepted"] = ack is not None      # None = BMS відповіла помилкою
            time.sleep(0.2)
            res["in_ram"] = read_reg(ser, reg, addr)
        finally:
            if entered:
                # ЗБЕРЕЖЕННЯ у EEPROM. Якщо запис вище не прийнявся — тут просто
                # нема чого зберігати, шкоди нема.
                talk(ser, FACTORY_EXIT, addr, WRITE, struct.pack(">H", PASS_EXIT_SAVE))
                time.sleep(0.3)
    return res


def do_calibrate(port, addr, reg, value):
    """Калібрування струму: запис + верифікація ЗА ЕФЕКТОМ (не read-back).

    Порівнюємо струм із 0x03-кадру до і після: після запису |I| має зійтися
    з ціллю value/100 А (±0.15 А або ±3%). 0xAD (нуль) — ціль |I| < 0.2 А.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from jbd_bms import read_all as basic_read
    before = basic_read(port, addr)["current"]
    print("Калібрування 0x%02X (%s): значення %d (%.2f А). Струм до: %.2f А"
          % (reg, CALIB[reg], value, value / 100, before))
    if reg == 0xAD and abs(before) > 0.3:
        sys.exit("❌ 0xAD пишеться ЛИШЕ у спокої, а зараз тече %.2f А." % before)
    if reg == 0xAE and before < 0.5:
        sys.exit("❌ 0xAE калібрується під ЗАРЯДОМ, а зараз %.2f А." % before)
    if reg == 0xAF and before > -0.5:
        sys.exit("❌ 0xAF калібрується під РОЗРЯДОМ, а зараз %.2f А." % before)
    r = write_reg(port, addr, reg, value)
    print("  прийнято=%s" % r["accepted"])
    time.sleep(1.5)
    after = basic_read(port, addr)["current"]
    target = 0.0 if reg == 0xAD else value / 100
    ok = (abs(after) < 0.2) if reg == 0xAD else (
        abs(abs(after) - target) <= max(0.15, target * 0.03))
    print("  струм після: %.2f А (ціль %.2f) → %s"
          % (after, target, "✅ КАЛІБРУВАННЯ ПРИЙНЯЛОСЬ" if ok else
             "❌ ефекту нема або криво — перевір/перекалібруй"))
    return ok


REMAIN_REG = 0xE0   # «Capacity remaining», u16, 10 мА·год (0x2D біт12=0), мапа Overkill Solar


def do_set_remaining(port, addr, value):
    """Записати залишок ємності (SOC-лічильник) — аналог «Editing capacity» у JBDTools.

    Read-back не визначений → верифікація ЗА ЕФЕКТОМ: remaining_ah у 0x03-кадрі
    має стати value/100. Допуск 1% повної ємності: BMS округлює залишок до ЦІЛОГО
    відсотка (перевірено 2026-10-05: 88.41 з 148.5 = 59.5% → лягло 89.09 = 60%).
    Значення не може перевищувати повну ємність (nominal_ah кадру 0x03).
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from jbd_bms import read_all as basic_read
    b = basic_read(port, addr)
    if not 0 < value <= round(b["nominal_ah"] * 100):
        sys.exit("❌ 0xE0=%d поза 0..%.2f А·год (повна ємність пака)." % (value, b["nominal_ah"]))
    print("Запис залишку 0x%02X: %.2f А·год. Було: %.2f / %.2f А·год, SOC %d%%"
          % (REMAIN_REG, value / 100, b["remaining_ah"], b["nominal_ah"], b["soc"]))
    r = write_reg(port, addr, REMAIN_REG, value)
    print("  прийнято=%s" % r["accepted"])
    time.sleep(1.5)
    a = basic_read(port, addr)
    ok = abs(a["remaining_ah"] - value / 100) <= b["nominal_ah"] * 0.01 + 0.2
    print("  після: %.2f А·год, SOC %d%% → %s" % (a["remaining_ah"], a["soc"],
          "✅ ЗАЛИШОК ЗАПИСАНО" if ok else "❌ ефекту нема"))
    return ok


def do_set(port, addr, reg, value):
    if reg == REMAIN_REG:
        return do_set_remaining(port, addr, value)
    if reg in CALIB:
        return do_calibrate(port, addr, reg, value)
    if reg not in WRITABLE:
        sys.exit("Регістр 0x%02X не в allowlist WRITABLE — навмисно не пишу." % reg)
    print("Запис 0x%02X (%s): %d мВ" % (reg, WRITABLE[reg], value))
    r = write_reg(port, addr, reg, value)
    print("  було=%s  прийнято=%s  у RAM після запису=%s"
          % (r["before"], r["accepted"], r["in_ram"]))
    # Верифікація персиста — СВІЖА сесія (заново factory-read)
    time.sleep(0.5)
    fresh = read_all(port, addr, use_factory=True).get(reg)
    ok = fresh == value
    print("  перечит свіжою сесією = %s  →  %s"
          % (fresh, "✅ ЗБЕРЕЖЕНО" if ok else "❌ НЕ ЗБЕРЕГЛОСЯ (лишилось старе)"))
    return ok


def do_read(port, addr, regs):
    """Читання довільних регістрів (поза REGISTERS) — для розвідки невідомих полів.

    Тільки ЧИТАННЯ: вхід у сервісний режим потрібен, бо частина регістрів поза ним
    недоступна, вихід — БЕЗ збереження (0x0000), тож змінити нічого не може.
    Друкує dec/hex/біти: FuncConfig-подібні поля читаються саме побітово.
    """
    with serial.Serial(port, BAUD, timeout=1.2) as ser:
        time.sleep(0.2)
        entered = False
        try:
            talk(ser, FACTORY_ENTER, addr, WRITE, struct.pack(">H", PASS_ENTER))
            entered = True
            time.sleep(0.3)
            for reg in regs:
                v = read_reg(ser, reg, addr)
                if v is None:
                    print("  0x%02X  <недоступний>" % reg)
                    continue
                bits = " ".join(str(i) for i in range(16) if v >> i & 1) or "—"
                print("  0x%02X  %6d  0x%04X  0b%016d  біти: %s"
                      % (reg, v, v, int(bin(v)[2:]), bits))
                time.sleep(0.06)
        finally:
            if entered:
                talk(ser, FACTORY_EXIT, addr, WRITE,
                     struct.pack(">H", PASS_EXIT_NOSAVE))


def main():
    ap = argparse.ArgumentParser(description="Дамп/запис налаштувань JBD BMS (UP16S010, DD-кадр)")
    ap.add_argument("--port", default=PORT)
    ap.add_argument("--addr", type=int, default=0)
    ap.add_argument("--save", metavar="FILE", help="зберегти JSON")
    ap.add_argument("--raw", action="store_true", help="без розшифровки")
    ap.add_argument("--set", metavar="REG=VAL", help="записати регістр, напр. 0x2A=3400 (мВ). Треба --yes")
    ap.add_argument("--read", metavar="REG", nargs="+",
                    help="прочитати довільні регістри, напр. --read 0x2C 0x2D 0x3D")
    ap.add_argument("--yes", action="store_true", help="підтвердити запис у BMS")
    args = ap.parse_args()

    if args.read:
        regs = [int(r, 0) for r in args.read]
        print("── ДОВІЛЬНІ РЕГІСТРИ (тільки читання) ───────────────")
        do_read(args.port, args.addr, regs)
        return

    if args.set:
        if not args.yes:
            sys.exit("Запис у BMS — додай --yes для підтвердження.")
        reg_s, val_s = args.set.split("=", 1)
        reg = int(reg_s, 0)
        value = int(val_s, 0)
        ok = do_set(args.port, args.addr, reg, value)
        sys.exit(0 if ok else 1)

    print("Спроба 1: читання БЕЗ сервісного режиму (нульовий ризик)…")
    vals = read_all(args.port, args.addr, use_factory=False)
    if len(vals) < 3:
        print("  доступно лише %d регістрів → пробую через сервісний режим" % len(vals))
        print("  (вихід буде БЕЗ збереження, змінити нічого не можна)")
        vals = read_all(args.port, args.addr, use_factory=True)

    if not vals:
        sys.exit("BMS не віддала жодного регістра налаштувань.")

    print("\n── НАЛАШТУВАННЯ BMS (%d параметрів) ─────────────────" % len(vals))
    dump = {}
    for reg, name, fmt in REGISTERS:
        if reg not in vals:
            continue
        v = vals[reg]
        dump["0x%02X" % reg] = {"name": name, "raw": v, "value": fmt(v)}
        print("  0x%02X  %-42s %s" % (reg, name, v if args.raw else fmt(v)))

    if args.save:
        with open(args.save, "w") as fh:
            json.dump({
                "saved_at": datetime.datetime.now().astimezone().isoformat(),
                "address": args.addr,
                "registers": dump,
            }, fh, ensure_ascii=False, indent=1)
        print("\nЗбережено → %s" % args.save)


if __name__ == "__main__":
    main()
