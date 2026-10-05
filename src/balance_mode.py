#!/usr/bin/env python3
"""balance_mode — follower that keeps each JBD BMS pack's passive-balancer mode
("balance while charging" vs "balance at rest") matched to its current state
(cell voltage gate + debounced current), writing through the DD or ES-UP
protocol depending on which family that pack's address belongs to.

balance_mode — слідкувач за режимом балансування BMS (JBD), залежно від стану.

Пасивний балансир JBD має ДВА взаємовиключні режими (прошивка не дає обидва
одночасно): «під зарядом» (балансує лише коли тече зарядний струм) і
«статичний» (балансує лише в спокої/CV-паузі/OVP). Цей модуль вирішує, який
режим АКТУАЛЬНО потрібен пакету зараз, і переключає BMS, якщо він не збігається
з тим, що вже виставлено.

Логіка на пак (BalanceModeFollower.step, викликається з main-циклу jbd2mqtt):
  - cell_max — гейт «балансування взагалі можливе», з гістерезисом: вмикається
    від 3.40 В (старт балансу в обох BMS), вимикається лише нижче 3.35 В.
    Поки гейт вимкнений — нічого не читаємо й не пишемо (на полиці режим
    неважливий, уникаємо зайвого зносу EEPROM).
  - Поки гейт увімкнений: струм ≥ +1.0 А стабільно ≥120с → бажаний "charge";
    |струм| ≤ 0.5 А стабільно ≥120с → бажаний "static"; 0.5..1.0 А — мертва
    зона, бажаний режим НЕ міняється (і таймер стабільності скидається).
  - Запис лише коли бажаний режим ≠ останнього відомого, і не частіше ніж
    раз на 5 хв на пак (той самий таймер рахує і ретраї після невдачі).
  - Після запису — перечитування й підтвердження; 3 невдачі поспіль → лог
    «залишаю як є» і пауза запису 1 год на цей пак (читання даних це не чіпає).

Протоколи запису (два різних BMS на одній RS485-шині, розрізняються адресою):
  - addr 0 (стара, класичний DD-протокол JBD): регістр 0x2D (FuncConfig),
    біт3 = chg_balance_en. Пишемо ЧЕРЕЗ читання поточного значення й зміну
    лише біта3 (щоб не зачепити інші біти — balance_en/LED), через
    jbd_settings.write_reg (сервісний режим, вихід ЗІ збереженням у EEPROM).
  - addr != 0 (нова, ES-UP/«поліглот»): esup.write_reg на
    esup.BALANCE_MODE_ADDR (0x1C7C) — звичайний запис, без сервісного режиму,
    0=під зарядом / 1=статично.

⚠️ step()/startup() робити ТІЛЬКИ з main-циклу демона jbd2mqtt, між читаннями
того ж пака, в тому самому процесі (serial.Serial відкривається й закривається
на кожен виклик) — щоб не було гонок на спільній шині з іншим процесом.

Стійкість до ребуту: цей модуль СВІДОМО не кешує стан у файл — джерелом правди
є сама BMS (EEPROM: стара зберігає через вихід 0x2828, нова — звичайним 0x79).
При кожному старті демона (systemd/cron → jbd2mqtt.py)
BalanceModeFollower.startup() перечитує ФАКТИЧНИЙ режим напряму з BMS — ніякий
стан процесу не губиться, бо його й не було постійного. А далі step() у циклі
рахує бажаний режим ЗАНОВО з поточних cell_max/current на кожному такті (а не
з різниці станів), тож якщо після ребуту desired ≠ known — це виправиться тими
самими витримками (120с стабільності + 5хв між записами), не чекаючи окремої
"зміни стану": сам факт "струм уже ≥1А" — і є умова.
"""
import os
import struct
import sys
import time

try:
    import serial
except ImportError:                                              # pragma: no cover
    sys.exit("Немає pyserial. Запускай через: uv run --with pyserial jbd2mqtt.py")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import esup                                                       # noqa: E402
import jbd_settings                                               # noqa: E402

OLD_REG = 0x2D
OLD_BAL_BIT = 0x08        # FuncConfig біт3: chg_balance_en (баланс ПІД ЗАРЯДОМ)
NEW_CHARGE, NEW_STATIC = 0, 1   # esup.BALANCE_MODE_ADDR (0x1C7C)

# Які адреси на шині говорять ES-UP ("нова" прошивка) замість класичного DD
# ("стара"). env BMS_ESUP_ADDRS="1,2" — список адрес ES-UP; порожньо/не задано →
# той самий дефолт, що й раніше: лише addr==0 вважається "старою" DD-адресою,
# усе інше — ES-UP.
_ESUP_ADDRS_ENV = os.environ.get("BMS_ESUP_ADDRS", "").strip()
ESUP_ADDRS = ({int(x) for x in _ESUP_ADDRS_ENV.split(",") if x.strip()}
              if _ESUP_ADDRS_ENV else None)

GATE_ON_V = 3.40          # cell_max — вище: балансування можливе (старт балансу в обох BMS = 3400)
GATE_OFF_V = 3.35         # cell_max — нижче: гарантовано вимкнено (гістерезис)
CURRENT_CHARGE_A = 1.0    # струм ≥ це стабільно → бажаний "charge"
CURRENT_STATIC_A = 0.5    # |струм| ≤ це стабільно → бажаний "static"
DEBOUNCE_S = 120          # скільки тримати умову, перш ніж міняти бажаний режим
MIN_WRITE_INTERVAL_S = 300    # мінімум між (спробами) запису в один пак
MAX_FAILS = 3
FAIL_PAUSE_S = 3600

_LABEL = {"charge": "під зарядом", "static": "статичний", "unknown": "невідомо"}


def _label(mode: str) -> str:
    return _LABEL.get(mode, mode)


def _is_old(addr: int) -> bool:
    if ESUP_ADDRS is not None:
        return addr not in ESUP_ADDRS
    return addr == 0


def _read_old(port: str, addr: int) -> "int | None":
    """Прочитати 0x2D (FuncConfig) у сервісному режимі; вихід БЕЗ збереження."""
    with serial.Serial(port, jbd_settings.BAUD, timeout=1.2) as ser:
        time.sleep(0.2)
        entered = False
        try:
            jbd_settings.talk(ser, jbd_settings.FACTORY_ENTER, addr, jbd_settings.WRITE,
                               struct.pack(">H", jbd_settings.PASS_ENTER))
            entered = True
            time.sleep(0.3)
            return jbd_settings.read_reg(ser, OLD_REG, addr)
        finally:
            if entered:
                jbd_settings.talk(ser, jbd_settings.FACTORY_EXIT, addr, jbd_settings.WRITE,
                                   struct.pack(">H", jbd_settings.PASS_EXIT_NOSAVE))


def _read_new(port: str, addr: int) -> "int | None":
    with serial.Serial(port, esup.BAUD, timeout=1.5) as ser:
        time.sleep(0.2)
        d = esup.read_block(ser, 0x1C00, 0x1C88, addr)
    if not d or len(d) < 126:
        return None
    return struct.unpack(">H", d[124:126])[0]


def _decode(addr: int, raw) -> str:
    if raw is None:
        return "unknown"
    if _is_old(addr):
        return "charge" if (raw & OLD_BAL_BIT) else "static"
    if raw == NEW_CHARGE:
        return "charge"
    if raw == NEW_STATIC:
        return "static"
    return "unknown"


# Базове значення FuncConfig старої (заводське 0x01D7 = 471: balance_en, LED, …).
# ⚠️ НЕ read-modify-write: 2026-10-03 слідкувач прочитав битий кадр (0x80E7) і записав
# його назад — злетіли біти LED/біт8. Пишемо тільки відому константу ± біт3.
OLD_FUNC_BASE = 0x01D7


def _write_old(port: str, addr: int, target: str) -> bool:
    new_raw = (OLD_FUNC_BASE | OLD_BAL_BIT) if target == "charge" else (OLD_FUNC_BASE & ~OLD_BAL_BIT & 0xFFFF)
    jbd_settings.write_reg(port, addr, OLD_REG, new_raw)
    time.sleep(0.5)
    return _decode(addr, _read_old(port, addr)) == target


def _write_new(port: str, addr: int, target: str) -> bool:
    value = NEW_CHARGE if target == "charge" else NEW_STATIC
    with serial.Serial(port, esup.BAUD, timeout=1.5) as ser:
        time.sleep(0.2)
        esup.write_reg(ser, esup.BALANCE_MODE_ADDR, esup.BALANCE_MODE_ADDR + 2,
                        struct.pack(">H", value), addr)
    time.sleep(0.5)
    return _decode(addr, _read_new(port, addr)) == target


class BalanceModeFollower:
    """Тримає бажаний/відомий режим балансування per-addr і пише в BMS,
    коли той розходиться з бажаним (з гістерезисом і витримками — див. модуль)."""

    def __init__(self):
        self._state = {}

    def _st(self, addr):
        return self._state.setdefault(addr, {
            "gate_active": False,
            "candidate": None,
            "candidate_since": None,
            "known_mode": "unknown",
            "last_write": 0.0,
            "fails": 0,
            "pause_until": 0.0,
        })

    def known_mode(self, addr: int) -> str:
        return self._st(addr)["known_mode"]

    def startup(self, port: str, addrs, log=print):
        """Прочитати фактичний режим обох паків при старті демона."""
        for addr in addrs:
            try:
                raw = _read_old(port, addr) if _is_old(addr) else _read_new(port, addr)
            except Exception as e:                                # noqa: BLE001
                raw = None
                log(time.strftime("%m-%d %H:%M:%S ") + "[bms %d] старт: читання режиму балансу впало: %s"
                    % (addr, e))
            mode = _decode(addr, raw)
            self._st(addr)["known_mode"] = mode
            log(time.strftime("%m-%d %H:%M:%S ") + "[bms %d] режим балансу при старті: %s"
                % (addr, _label(mode)))

    def _write_and_verify(self, port, addr, target):
        if _is_old(addr):
            return _write_old(port, addr, target)
        return _write_new(port, addr, target)

    def step(self, port: str, addr: int, data: dict, now=None, log=print) -> str:
        """Один такт рішення для пака addr. `data` — те, що повернув jbd_bms.read_all
        (беремо cell_max/current). Повертає ПОТОЧНИЙ відомий режим ("charge"/
        "static"/"unknown") — саме це йде в MQTT-стан."""
        now = time.monotonic() if now is None else now
        st = self._st(addr)
        cell_max = data.get("cell_max")
        current = data.get("current")
        if cell_max is None or current is None:
            return st["known_mode"]

        if now < st["pause_until"]:
            return st["known_mode"]

        # гейт "балансування взагалі можливе" — з гістерезисом 3.40/3.45 В
        if st["gate_active"]:
            if cell_max < GATE_OFF_V:
                st["gate_active"] = False
        else:
            if cell_max >= GATE_ON_V:
                st["gate_active"] = True

        if not st["gate_active"]:
            st["candidate"] = None
            st["candidate_since"] = None
            return st["known_mode"]

        if current >= CURRENT_CHARGE_A:
            suggestion = "charge"
        elif abs(current) <= CURRENT_STATIC_A:
            suggestion = "static"
        else:
            suggestion = None      # мертва зона 0.5..1.0 А

        if suggestion is None:
            st["candidate"] = None
            st["candidate_since"] = None
            return st["known_mode"]

        if st["candidate"] != suggestion:
            st["candidate"] = suggestion
            st["candidate_since"] = now

        if now - st["candidate_since"] < DEBOUNCE_S:
            return st["known_mode"]            # ще не стабілізувалось 2 хв

        desired = suggestion
        if desired == st["known_mode"]:
            return st["known_mode"]            # уже те, що треба

        if now - st["last_write"] < MIN_WRITE_INTERVAL_S:
            return st["known_mode"]            # витримка між записами/ретраями

        log(time.strftime("%m-%d %H:%M:%S ") + "[bms %d] режим балансу: %s → %s (I=%+.1f А, max %.2f)"
            % (addr, _label(st["known_mode"]), _label(desired), current, cell_max))
        st["last_write"] = now
        try:
            ok = self._write_and_verify(port, addr, desired)
        except Exception as e:                                   # noqa: BLE001
            ok = False
            log(time.strftime("%m-%d %H:%M:%S ") + "[bms %d] запис режиму балансу впав: %s" % (addr, e))

        if ok:
            st["known_mode"] = desired
            st["fails"] = 0
        else:
            st["fails"] += 1
            log(time.strftime("%m-%d %H:%M:%S ") + "[bms %d] запис режиму балансу не підтвердився (%d/%d)"
                % (addr, st["fails"], MAX_FAILS))
            if st["fails"] >= MAX_FAILS:
                st["pause_until"] = now + FAIL_PAUSE_S
                st["fails"] = 0
                log(time.strftime("%m-%d %H:%M:%S ") + "[bms %d] режим балансу: залишаю як є, пауза 1 год"
                    % addr)
        return st["known_mode"]
