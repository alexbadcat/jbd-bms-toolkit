#!/usr/bin/env python3
"""bms_probe_multi — protocol/baud-rate scanner for an unknown JBD BMS on the
serial bus: tries classic DD, DD with an address byte, Modbus RTU (FC 0x03/0x04)
and a PACE/Seplos ASCII probe at 9600/19200/115200, and prints any raw reply so
you can identify which protocol and address a given pack answers to.

Зонд батареї: пробуємо різні протоколи/швидкості, друкуємо сирі байти.
"""
import os, serial, struct, time, sys

PORT = os.environ.get("BMS_PORT", "/dev/ttyUSB0")

def crc_modbus(d):
    crc = 0xFFFF
    for b in d:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)

def jbd_classic(cmd=0x03):
    body = bytes([0xA5, cmd, 0x00])
    crc = (0x10000 - sum(body[1:])) & 0xFFFF
    return bytes([0xDD]) + body + struct.pack(">H", crc) + bytes([0x77])

def jbd_up(addr, cmd=0x03):
    body = bytes([addr, 0xA5, cmd, 0x00])
    crc = (0x10000 - sum(body)) & 0xFFFF
    return bytes([0xDD]) + body + struct.pack(">H", crc) + bytes([0x77])

def pace_ascii(adr=1):
    # PACE/Seplos: ~[ver=25][adr][cid1=46][cid2=42][len]...
    body = "25%02X4642E002%02X" % (adr, adr)
    # LCHKSUM спрощено: беремо готовий шаблон для adr01
    return ("~25014642E00201FD30\r").encode()

def main():
    tests = []
    for baud in (9600, 19200, 115200):
        tests.append(("JBD-classic 0x03", baud, jbd_classic()))
        tests.append(("JBD-UP addr0", baud, jbd_up(0)))
        tests.append(("JBD-UP addr1", baud, jbd_up(1)))
        for sid in (1, 2):
            tests.append(("Modbus f03 id%d" % sid, baud, bytes([sid, 0x03, 0x00, 0x00, 0x00, 0x10]) + crc_modbus(bytes([sid,3,0,0,0,0x10]))))
            tests.append(("Modbus f04 id%d" % sid, baud, bytes([sid, 0x04, 0x00, 0x00, 0x00, 0x10]) + crc_modbus(bytes([sid,4,0,0,0,0x10]))))
        tests.append(("PACE ascii adr1", baud, pace_ascii()))

    for name, baud, frame in tests:
        try:
            with serial.Serial(PORT, baud, timeout=1.2) as ser:
                ser.reset_input_buffer()
                ser.write(frame)
                time.sleep(0.9)
                resp = ser.read(200)
            if resp:
                print("🎯 %s @%d → %d байт: %s" % (name, baud, len(resp), resp[:60].hex(" ")))
            else:
                print("   %s @%d → тиша" % (name, baud))
        except Exception as e:
            print("   %s @%d → ERR %s" % (name, baud, e))


if __name__ == "__main__":
    main()
