#!/usr/bin/env python3
"""sniff_decode — decodes a pcap capture of a ser2net serial-over-TCP tunnel to
show exactly what a BMS vendor tool (e.g. JBDTools) sends to the BMS, useful for
reverse-engineering locked/undocumented registers without touching Windows.

sniff_decode — розбір pcap із serial-over-IP тунелю (ser2net :4001 ↔ BMS).

Навіщо: підгледіти, ЩО САМЕ шле рідний JBDTools у нову BMS (UP16S019 rev.2)
при логіні «Production Manager» і при cell-калібруванні — бо factory-поля
цієї прошивки мовчки ігнорують наш класичний вхід 0x5678 (user-поля пишуться,
factory — ні). Зловлений анлок-кадр вшиваємо в jbd_settings.py → далі все з
NAS, без Windows.

Використання:
  tcpdump -i any -w /tmp/bms.pcap 'tcp port 4001'        # на NAS, у фон
  ./sniff_decode.py /tmp/bms.pcap [--raw]

Напрямок визначаємо по TCP-порту: dst 4001 = JBDTools→BMS (запит),
src 4001 = BMS→JBDTools (відповідь). TCP-сегменти одного напрямку склеюємо
в потік і ріжемо на кадри по DD…77 (класичний/UP-DD) або по Modbus-заголовку
ES-UP (addr, 0x78/0x79). Без сторонніх бібліотек: pcap читаємо руками.
"""
import struct
import sys

PORT = 4001

# Відомі DD-регістри, щоб анлок було видно одразу
DD_REG = {
    0x00: "FACTORY_ENTER (0x5678)", 0x01: "FACTORY_EXIT (0x0000 / 0x2828 save)",
    0x03: "basic", 0x04: "cells", 0x05: "name", 0x06: "PASSWORD?", 0x10: "design_cap",
    0x12: "full_chg_vol", 0x2A: "bal_start", 0x2B: "bal_window", 0x2D: "func_config",
    0xAD: "cal_idle_current", 0xAE: "cal_charge_current", 0xAF: "cal_discharge_current",
    0xE2: "balance_control(force)",
}
for i in range(16):
    DD_REG[0xB0 + i] = "cal_cell_%d" % (i + 1)


def pcap_tcp_payloads(path):
    """Ітератор (src_port, dst_port, payload) з pcap (Ethernet/Linux-cooked + IPv4 + TCP)."""
    with open(path, "rb") as fh:
        gh = fh.read(24)
        if len(gh) < 24:
            return
        magic = struct.unpack("<I", gh[:4])[0]
        endian = "<" if magic in (0xA1B2C3D4, 0xA1B23C4D) else ">"
        linktype = struct.unpack(endian + "I", gh[20:24])[0]
        while True:
            ph = fh.read(16)
            if len(ph) < 16:
                return
            _, _, incl, _ = struct.unpack(endian + "IIII", ph)
            pkt = fh.read(incl)
            # L2: 1 = Ethernet (14), 113 = Linux cooked v1 (16), 276 = cooked v2 (20)
            off = {1: 14, 113: 16, 276: 20}.get(linktype)
            if off is None or len(pkt) < off + 20:
                continue
            ip = pkt[off:]
            if ip[0] >> 4 != 4 or ip[9] != 6:            # не IPv4 / не TCP
                continue
            ihl = (ip[0] & 0x0F) * 4
            total = struct.unpack(">H", ip[2:4])[0]
            tcp = ip[ihl:total]
            if len(tcp) < 20:
                continue
            sport, dport = struct.unpack(">HH", tcp[:4])
            doff = (tcp[12] >> 4) * 4
            payload = tcp[doff:]
            if payload:
                yield sport, dport, payload


def split_frames(stream: bytes):
    """Ріжемо байтовий потік на DD…77 кадри; хвости/сміття віддаємо як є."""
    out, i = [], 0
    while i < len(stream):
        if stream[i] == 0xDD:
            j = stream.find(b"\x77", i + 6)
            if j != -1:
                out.append(stream[i:j + 1]); i = j + 1; continue
        # ES-UP Modbus: [addr][0x78|0x79][start2][end2][len2]…crc2
        if i + 8 <= len(stream) and stream[i + 1] in (0x78, 0x79):
            dlen = struct.unpack(">H", stream[i + 6:i + 8])[0]
            n = 8 + dlen + 2
            if stream[i + 1] == 0x78 and dlen == 0:
                n = 10
            out.append(stream[i:i + n]); i += n; continue
        out.append(stream[i:i + 1]); i += 1
    return out


def describe(frame: bytes, req: bool) -> str:
    h = frame.hex()
    if frame[:1] == b"\xDD" and frame[-1:] == b"\x77" and len(frame) >= 7:
        # UP-DD з адресою: DD addr A5/5A reg len data… crc 77 ; класичний без addr: DD A5/5A reg len…
        if frame[2] in (0xA5, 0x5A):
            addr, mode, reg, ln = frame[1], frame[2], frame[3], frame[4]
            data = frame[5:5 + ln]
            fmt = "UP-DD addr=%d" % addr
        elif frame[1] in (0xA5, 0x5A):
            addr, mode, reg, ln = None, frame[1], frame[2], frame[3]
            data = frame[4:4 + ln]
            fmt = "DD"
        else:
            # відповідь: DD [addr] cmd status len data…
            return "%s  %s" % ("RESP" if not req else "?", h)
        kind = "WRITE" if mode == 0x5A else "READ"
        name = DD_REG.get(reg, "")
        flag = ""
        if mode == 0x5A and reg in (0x00, 0x01, 0x06) or (mode == 0x5A and 0xB0 <= reg <= 0xCF):
            flag = "   <<<<< ФАКТОРІ/АНЛОК/КАЛІБРУВАННЯ"
        return "%s %s reg=0x%02X %-24s data=%s%s" % (fmt, kind, reg, name, data.hex() or "-", flag)
    if len(frame) >= 8 and frame[1] in (0x78, 0x79):
        fc = "ES-UP READ" if frame[1] == 0x78 else "ES-UP WRITE"
        start, end = struct.unpack(">HH", frame[2:6])
        flag = "   <<<<< ES-UP WRITE" if frame[1] == 0x79 else ""
        return "%s addr=%d 0x%04X..0x%04X  %s%s" % (fc, frame[0], start, end, h[16:80], flag)
    return "raw %s" % h


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    raw = "--raw" in sys.argv
    to_bms, from_bms = bytearray(), bytearray()
    order = []
    for sport, dport, pl in pcap_tcp_payloads(path):
        if dport == PORT:
            order.append(("→BMS", pl)); to_bms += pl
        elif sport == PORT:
            order.append(("←BMS", pl)); from_bms += pl
    if raw:
        for d, pl in order:
            print(d, pl.hex())
        return
    print("=== JBDTools → BMS (%d байт) ===" % len(to_bms))
    for f in split_frames(bytes(to_bms)):
        if len(f) > 1:
            print("  ", describe(f, True))
    print("=== BMS → JBDTools (%d байт) ===" % len(from_bms))
    for f in split_frames(bytes(from_bms)):
        if len(f) > 1:
            print("  ", describe(f, False))


if __name__ == "__main__":
    main()
