#!/usr/bin/env python3
"""esup_factory_reset — runs an ES-UP factory reset (register 0x290E) on a JBD
"polyglot" BMS, backing up the touched register blocks first and automatically
restoring the user-writable fields (capacity, balance thresholds) afterwards.

esup_factory_reset — ES-UP factory reset нової BMS (UP16S019, addr=1) з бекапом
і авто-відновленням writable-параметрів.

НАВІЩО: коефіцієнт калібрування виміру комірки №1 (блок 0x3800+8) збився з
10006 на 10452 (2026-09-29, під час DV/DP-експериментів). Прямий запис +8
прошивка ігнорує в усіх режимах, DD 0xB0 залочено, ES-UP param default
(0x290C) — без ефекту, рідний JBDTools по RS485 теж відмовляє
(«校准电压失败», див. банк deye-bms2-jbdtools-also-locked-2026-09-29).
Лишається factory reset (0x290E, payload 5A5A) — має скинути калібрування
до заводського; але може скинути й user-поля, тому:

  1. бекап 0x1000/0x1C00/0x2000/0x3800/0x3900 у backups/bms2-PRE-FRESET-*;
  2. reset; чекаємо, поки BMS знову відповість;
  3. diff кожного блоку з бекапом;
  4. відновлюємо ТІЛЬКИ відомі writable-поля з бекапу через ES-UP write
     (FullAdjV/FullAdjC 0x1C0C/0x1C0E, BalV/BalDiff 0x1C04/0x1C06,
     Nominal/Full capacity 0x2000/0x2002) — якщо вони змінились;
  5. звіт: coef #1 до/після, що змінилось, що відновлено.

⚠️ Бойова батарея в паралелі з Deye. Запускати ТІЛЬКИ коли порт вільний
(ser2net/jbd2mqtt зупинені). Ідемпотентно: без --yes лише читає і друкує план.
"""
import argparse
import os
import struct
import sys
import time

import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from esup import read_block, write_reg, BAUD   # noqa: E402

PORT = os.environ.get("BMS_PORT", "/dev/ttyUSB0")
ADDR = int(os.environ.get("BMS_ESUP_ADDR", "1"))
BACKUP_DIR = os.environ.get("BMS_BACKUP_DIR", "backups")
BLOCKS = {"1000": (0x1000, 0x10A2), "1C00": (0x1C00, 0x1C88), "2000": (0x2000, 0x2040),
          "3800": (0x3800, 0x3820), "3900": (0x3900, 0x3920)}
# (блок, офсет, назва) — поля, які вміємо і хочемо відновити
RESTORE = [("1C00", 12, "FullAdjustV"), ("1C00", 14, "FullAdjustC"),
           ("1C00", 4, "BalanceV"), ("1C00", 6, "BalanceDiff"),
           ("2000", 0, "NominalAh"), ("2000", 2, "FullAh")]
u16 = lambda d, i: struct.unpack(">H", d[i:i + 2])[0]     # noqa: E731


def rb(ser, s, e, tries=6):
    for _ in range(tries):
        b = read_block(ser, s, e, ADDR)
        if b:
            return b
        time.sleep(0.8)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="реально виконати reset")
    a = ap.parse_args()
    ts = time.strftime("%Y%m%d-%H%M")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with serial.Serial(PORT, BAUD, timeout=1.5) as ser:
        time.sleep(0.3)
        before = {}
        for name, (s, e) in BLOCKS.items():
            b = rb(ser, s, e)
            if not b:
                sys.exit("❌ блок 0x%s не читається — порт зайнятий або BMS мовчить, нічого не роблю" % name)
            before[name] = b
            open(os.path.join(BACKUP_DIR, "bms-PRE-FRESET-%s-%s.bin" % (name, ts)), "wb").write(b)
        print("бекап PRE-FRESET ✅  coef#1=%d charge=%d FullAdjV=%d BalV=%d Nominal=%d"
              % (u16(before["3800"], 8), u16(before["3800"], 0), u16(before["1C00"], 12),
                 u16(before["1C00"], 4), u16(before["2000"], 0)))
        if not a.yes:
            print("(сухий прогін: додай --yes щоб виконати factory reset 0x290E=5A5A)")
            return
        ack = write_reg(ser, 0x290E, 0x2910, b"\x5A\x5A", ADDR)
        print("factory reset ack:", ack.hex() if ack else "тиша")
    # BMS може перезавантажитись — чекаємо і перечитуємо
    time.sleep(8)
    after = {}
    with serial.Serial(PORT, BAUD, timeout=1.5) as ser:
        time.sleep(0.3)
        for name, (s, e) in BLOCKS.items():
            after[name] = rb(ser, s, e, tries=12)
    missing = [n for n, b in after.items() if not b]
    if missing:
        print("❌ після reset не читаються блоки:", missing, "— перечитай пізніше, бекапи є")
        return
    print("після: coef#1=%d (ціль 10006) charge=%d FullAdjV=%d BalV=%d Nominal=%d Remain=%.1f"
          % (u16(after["3800"], 8), u16(after["3800"], 0), u16(after["1C00"], 12),
             u16(after["1C00"], 4), u16(after["2000"], 0), u16(after["2000"], 4) / 100))
    for name in BLOCKS:
        ch = [i for i in range(0, min(len(before[name]), len(after[name])) - 1, 2)
              if u16(before[name], i) != u16(after[name], i)]
        print("  0x%s змінені офсети: %s" % (name, ch or "нема"))
    # авто-відновлення writable-полів
    with serial.Serial(PORT, BAUD, timeout=1.5) as ser:
        time.sleep(0.3)
        for blk, off, label in RESTORE:
            base = BLOCKS[blk][0]
            old, new = u16(before[blk], off), u16(after[blk], off)
            if old != new:
                write_reg(ser, base + off, base + off + 2, struct.pack(">H", old), ADDR)
                time.sleep(0.5)
                chk = rb(ser, *BLOCKS[blk])
                print("  відновлено %s: %d→%d → зараз %s" % (label, new, old, u16(chk, off) if chk else "?"))


if __name__ == "__main__":
    main()
