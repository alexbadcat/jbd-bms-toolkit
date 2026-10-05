# Lovelace-картка Deye

🇬🇧 [In English](README.md)

Кастомна картка для [Home Assistant](https://www.home-assistant.io/) під гібридний
інвертор Deye (перевірено на SUN-5K-SG03LP1 через інтеграцію
[Solarman](https://github.com/davidrapan/ha-solarman)) плюс до двох паків JBD BMS,
підключених через [`jbd2mqtt.py`](../src/jbd2mqtt.py) з цього тулкіта.

Текст інтерфейсу картки — українською (не перекладається); усе інше (entity_id,
джерело даних відключень, календарі) конфігурується, тож картка працює з **будь-яким**
префіксом/регіоном.

![Прев'ю картки Deye](screenshots/deye-card-preview.png)

## Можливості

- Анімована схема потоку енергії (мережа / генератор / батарея / будинок навколо
  інвертора), три компонування: `hub` (хаб), `vertical` (вертикальний), `mini` (міні).
- Стан батареї: заряд, напруга, струм, температура, ємність.
- Покомірковий перегляд до **двох** паків BMS: 16 комірок на пак, живий glow/хвиля
  балансування на тих комірках, що БМС зараз реально зціджує, дельта/SOH/цикли/реальна
  ємність.
- Чотирипозиційний селектор режиму роботи (Еко / Авто / Критичний / Балансування),
  привʼязаний до `input_select`-хелпера, плюс попапи керування батареєю/мережею/
  генератором/інвертором (повзунки, селекти, перемикачі, кнопка-дія форсажу генератора).
- **Вкладка «Графіки»** (`deye-graphs.js`): SOC, потужності, дельта комірок, мін./макс.
  комірка (з лініями порогів OVP) + смуга активності балансування, ліміт струму заряду —
  діапазони 6г/24г/7д, тач-перехрестя з перетягуванням.
- **Вкладка «Відключення»** (`deye-outage.js`, опційна): таймлайн сьогодні/завтра з
  JSON-графіка відключень у стилі ДТЕК (факт + фолбек на тижневий шаблон), тап по вікну
  відкриває попап з деталями, оцінка автономності на батареях, історія відключень за
  7 діб, відновлена з історії напруги мережі.
- Тап по будь-якому показнику відкриває повноекранний графік історії (пан / pinch-zoom /
  перехрестя).
- Візуальний редактор картки в Lovelace (префікс, назва, компонування, масштаб,
  анімація, кольори) — розширені опції нижче доступні лише через YAML.

## Встановлення

1. Скопіюй усі три файли в теку Home Assistant `/config/www/` (підтека теж ок, напр.
   `/config/www/deye-card/`):
   - `deye-card.js`
   - `deye-graphs.js`
   - `deye-outage.js`

   (`deye-graphs.js` і `deye-outage.js` підвантажуються динамічно з `deye-card.js` через
   `import()` з тієї ж теки, де зареєстрований ресурс нижче — тримай усі три файли разом.)

2. У Home Assistant: **Settings → Dashboards → ⋮ → Resources → Add resource**
   - URL: `/local/deye-card/deye-card.js?v=1` (підлаштуй шлях; `?v=1` — кеш-бастер,
     бамп його щоразу при оновленні файлу, див. Troubleshooting нижче)
   - Тип ресурсу: **JavaScript module**

3. Додай картку на дашборд — через UI-пікер карток (пошук «Deye») або YAML (приклади
   нижче).

## Мінімальний конфіг

```yaml
type: custom:deye-card
prefix: inverter_deye      # префікс сутностей твоєї інтеграції Solarman/Deye
```

З цим ти отримуєш схему, деталі батареї, деталі інвертора, графіки — усе, що залежить
від опційних хелперів (селектор режиму, вартість за тарифом, вкладка відключень, форсаж
генератора), просто не рендериться, якщо відповідної сутності нема. Нічого не падає з
помилкою.

## Повний конфіг (довідка)

```yaml
type: custom:deye-card

# ── основне ──
prefix: inverter_deye          # префікс Solarman/Deye: sensor.<prefix>_battery і т.д.
title: Інвертор Deye           # опційно, за замовчуванням "Інвертор Deye"
title_size: 1.25               # опційно, розмір шрифту заголовка, rem
layout: hub                    # hub | vertical | mini
scale: 1                       # 0.5–1.5, загальний масштаб
animate: true                  # false = вимкнути анімацію потоку/шевронів (слабкі кіоски)
colors:                        # опційно, будь-яка підмножина; показано дефолти
  grid: '#5ac8fa'
  generator: '#ff9f0a'
  battery: '#34c759'
  house: '#0a84ff'
  accent: '#ff8a3d'

# ── паки BMS (міст JBD, див. ../src/jbd2mqtt.py) ──
# один пак:
bms_prefix: batareia_deye_bms
# АБО до двох паків (bms_prefixes має пріоритет, якщо задані обидва):
bms_prefixes:
  - batareia_deye_bms
  - batareia_deye_bms_2
bms_names:                     # опційно, заголовки секцій у розгорнутій картці
  - "Батарея №1 (BMS)"
  - "Батарея №2 (BMS)"

# ── опційні оверрайди/хелпери ──
time_to_full: sensor.chas_do_povnogo_zariadu   # сенсор-рядок «X год Y хв», показується при заряді
time_to_empty: sensor.chas_do_rozriadu         # те саме, при розряді
grid_meter_entity: sensor.moy_zovnishniy_lichylnyk  # оверрайд джерела потужності мережі
                                                     # (дефолт — sensor.<prefix>_grid_power)

# чотирипозиційний селектор режиму (input_select). Відповідає examples/home-assistant/helpers.yaml:
mode_entity: input_select.ess_mode
# мапа чотирьох режимів картки на ВАШІ назви опцій (можна пропустити, якщо опції
# українські «Еко / Авто / Критичний / Балансування»):
mode_options:
  eco: Eco
  auto: Auto
  emergency: Emergency
  balance: Balance

# секція місячного споживання за тарифними зонами (ховається, якщо жодної сутності нема)
month_entities:
  day_kwh: sensor.spozhito_zony_den
  night_kwh: sensor.spozhito_zony_nich
  day_uah: sensor.deye_cost_day
  night_uah: sensor.deye_cost_night
  total_uah: sensor.deye_cost_total

# банер авто-причин «критичного режиму» — усе опційно, підключи свою автоматизацію/
# хелпери (або пропусти повністю: банер тоді просто завжди показує стан mode_entity)
outage_entities:
  schedule: binary_sensor.outage_schedule_today       # на сьогодні очікується відключення за графіком
  emergency: binary_sensor.outage_emergency_unified    # активне аварійне відключення
  seen_today: input_boolean.deye_outage_seen_today     # сьогодні вже було відключення >10 хв
  possible24h: binary_sensor.outage_possible_24h        # «можливі» відключення в найближчі 24г
  today_source: sensor.outage_dtek_today_source         # текстова мітка джерела для цього вище

# кнопка-дія форсажу генератора в попапі «Генератор» (ховається, якщо скрипта нема)
gen_boost:
  script: script.deye_gen_boost     # скрипт пишеш сам: підняти струм заряду/пік на годину,
                                     # потім повернути два значення нижче
  current: 30                       # А, повертається при достроковому скасуванні форсажу
  peak_shaving: 2500                # Вт, повертається при достроковому скасуванні форсажу

# Вкладка «Відключення» — ПРОПУСТИ ПОВНІСТЮ, щоб вкладка взагалі не показувалась.
# source_url і group ОБОВʼЯЗКОВІ РАЗОМ (розумного дефолту нема — це твій регіон/черга).
outage:
  # JSON-файл у форматі проєкту https://github.com/Baskerville42/outage-data-ua
  # (один файл на область; можеш підняти й власний сумісний JSON)
  source_url: https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json
  group: GPV1.1                                   # ключ черги/групи всередині того JSON
  # опційний фолбек №2, лише коли JSON вище недоступний І ще нема кешу
  calendar_scheduled: calendar.moyi_region_scheduled_outages
  calendar_planned: calendar.moyi_region_planned_outages
  # опційно: binary_sensor(и), що перемикають мітку попапу вікна на «АВАРІЙНЕ»
  emergency_entities:
    - binary_sensor.svitlo_moyi_region_emergency_outages
```

## Потрібні/очікувані сенсори

Картка ніколи не падає з помилкою через відсутню сутність — секція/рядок/вкладка просто
не рендериться. Але ось що саме живить `prefix` (найменування за схемою інтеграції
[Solarman](https://github.com/davidrapan/ha-solarman) для Deye):

| Домен | Сутності (при `prefix: inverter_deye`) |
|---|---|
| sensor | `battery`, `battery_power`, `battery_state`, `battery_voltage`, `battery_current`, `battery_temperature`, `battery_capacity`, `grid_power`, `generator_power`, `load_power`, `power_losses`, `output_l1_power`, `external_ct1_power`, `grid_l1_voltage`, `grid_frequency`, `temperature`, `device_state`, `device_alarm`, `today_battery_charge`, `today_battery_discharge`, `today_energy_import`, `today_energy_export`, `today_load_consumption` |
| binary_sensor | `grid`, `generator`, `connection` |
| select | `work_mode`, `energy_pattern`, `io_mode` |
| switch | `off_grid`, `generator`, `battery_wake_up`, `battery_grid_charging`, `battery_generator_charging` |
| number | `battery_max_charging_current`, `battery_max_discharging_current`, `battery_low_soc`, `battery_shutdown_soc`, `battery_restart_soc`, `battery_grid_charging_start`, `battery_grid_charging_current`, `zero_export_power`, `battery_generator_charging_start`, `battery_generator_charging_current`, `generator_peak_shaving`, `program_1_soc` … `program_6_soc` |

Сутності BMS (при `bms_prefix: batareia_deye_bms`, з мосту цього тулкіта
[`jbd2mqtt.py`](../src/jbd2mqtt.py) з HA-discovery) мають українські назви, бо Home
Assistant формує `entity_id` слагіфікацією рядка `"<дружня назва пристрою> <назва
сенсора>"` при першому discovery, а назви пристрою/сенсорів у мості — українські:

| Суфікс у `sensor.<bms_prefix>_…` | Значення |
|---|---|
| `zariad` | заряд, % |
| `napruga_paketa` | напруга пакета, В |
| `strum` | струм, А |
| `delta_komirok` | дельта комірок, мВ |
| `minimalna_komirka` / `maksimalna_komirka` | мін./макс. напруга комірки, В |
| `zdorov_ia_soh` | здоров'я (SOH), % |
| `tsikliv` | циклів заряду |
| `realna_iemnist` | реальна ємність, А·год |
| `zalishok_iemnosti` | залишок ємності, А·год (використовується в оцінці автономності вкладки «Відключення») |
| `temperatura_1` … `temperatura_4` | сенсори температури пакета |
| `komirka_1` … `komirka_16` | напруга кожної комірки, В |
| `balansuvannia_komirok` | текст: номери комірок через кому, що зараз балансуються, або `—` |
| `rezhim_balansuvannia` | `charge` / `static` / `unknown` |

Якщо назви твого мосту/пристрою відрізняються — відрізнятиметься й `entity_id`: перевір
**Developer Tools → States** і вистав `bms_prefix`/`bms_prefixes` під те, що реально
вийшло (це спільний префікс перед суфіксом, напр. для `sensor.batareia_deye_bms_zariad`
префікс — `batareia_deye_bms`).

## Усунення проблем

- **Картка не оновлюється після зміни `.js`-файлу на диску.** Браузери агресивно кешують
  JS-модулі. Бампай `?v=` у URL ресурсу в **Settings → Dashboards → Resources** щоразу
  при передеплої файлу (і жорсткий рефреш, ⇧⌘R / Ctrl+Shift+R). `deye-graphs.js`/
  `deye-outage.js` кеш-бастяться автоматично самою карткою (статичні лічильники
  `DeyeCard.GRAPHS_V` / `DeyeCard.OUTAGE_V`) — бампай ці два числа в `deye-card.js` лише
  якщо редагуєш саме ці два файли і треба, щоб клієнти підхопили зміну негайно.
- **Порожня картка в новому режимі дашборда «Sections».** Sections-розкладка потребує
  `getGridOptions()` (у картки він є), але й дашборд, що встиг завантажити `hass` до
  монтування картки; якщо лишається порожньою — видали й додай картку заново, або
  перевір консоль браузера на помилку імпорту (неправильний шлях/регістр файлу).
- **«Не вдалось завантажити модуль…» усередині вкладки.** Динамічний `import()`
  `deye-graphs.js`/`deye-outage.js` не спрацював — майже завжди неправильний шлях. Обидва
  файли мають лежати в тій самій теці, що й `deye-card.js`, під `/config/www/`.
- **Вкладка «Відключення» не зʼявляється.** Вона схована, доки в конфізі картки не
  задані ОБИДВА `outage.source_url` і `outage.group` — див. «Повний конфіг» вище.
- **У консолі CORS-помилки при фетчі JSON відключень.** Працює лише з джерелом, що
  віддає `Access-Control-Allow-Origin: *` (raw GitHub content — так); навести на
  довільний сайт без CORS не вийде.
