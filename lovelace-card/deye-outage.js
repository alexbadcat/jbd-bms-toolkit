/* deye-outage.js — вкладка «Відключення» для deye-card (v47+).
   Динамічно підключається з deye-card.js через import(), той самий патерн, що й
   deye-graphs.js (тримає головний файл компактним).

   🔑 ПЕРШОДЖЕРЕЛО (замість календарів HA — ті показують лише ШАБЛОН тижня без типу,
   банер виглядав би «червоним = точно» навіть коли факт зелений): JSON графіка
   відключень конкретного обленерго/черги, що публікується на GitHub у форматі
   проєкту Baskerville42/outage-data-ua (приклад: outage-data-ua/main/data/kyiv-region.json —
   інші області мають свої файли в тому ж репо; можна підняти й власний сумісний JSON).
   УВАГА: URL і назва черги/групи — ЦЕ ТВОЇ РЕГІОНАЛЬНІ ДАНІ, дефолту нема навмисно —
   задаються ЛИШЕ через конфіг картки `outage: {source_url, group}`; без них вкладка
   взагалі не показується (card._outageEnabled() у deye-card.js).
   CORS у джерела Baskerville42 відкритий (access-control-allow-origin: *) — fetch()
   прямо з браузера.
   Структура очікуваного JSON (формат outage-data-ua):
     • fact.data[<unix-сек північ за місцевим часом>][<group>]["1".."24"] — ФАКТ на
       конкретний день (ключ "1" = 00-01 … "24" = 23-24), статуси:
       yes/no/maybe/first/second/mfirst/msecond.
     • fact.update — рядок "DD.MM.YYYY HH:MM", час публікації.
     • preset.data[<group>][1..7] (1=Пн..7=Нд) — тижневий ШАБЛОН (орієнтовно), той самий формат.
   Факт на сьогодні/завтра є не завжди (публікується ввечері) → якщо нема факту на день,
   падаємо на шаблон тижня по дню тижня, з явною позначкою джерела в UI.
   Кеш: 10 хв у памʼяті (щоб не бити GitHub щохвилинним REFRESH_MS) + localStorage-фолбек
   (якщо зовсім нема інтернету — показуємо останній відомий кеш з позначкою). Якщо й кешу
   нема — опційний фолбек на календарі HA (outage.calendar_scheduled/calendar_planned,
   напр. інтеграція «Світло»/svitlo чи yasno), якщо вони задані в конфізі; якщо ні —
   показуємо помилку без крашу.

   Інші джерела (без pfx — глобальні хелпери, усі опційні через конфіг outage.*):
   • outage.calendar_scheduled / outage.calendar_planned — фолбек №2, календарі HA
     (REST `/api/calendars/<entity>?start=…&end=…`, hass.callApi('GET', …)).
   • outage.emergency_entities — масив binary_sensor.* аварійних відключень; якщо
     активні й «зараз» всередині вікна «нема світла», попап показує тип «АВАРІЙНЕ»
     замість звичайного «СВІТЛА НЕМАЄ».
   • sensor.<inverter_prefix>_grid_l1_voltage — історія за 7 діб через card._fetchHistory()
     (та сама функція, що й тач-графіки історії): <150В = нема мережі.
   • BMS-паки картки (card._bmsPfx()): …_zalishok_iemnosti (Ah) + …_realna_iemnist
     (Ah, як «номінальна» — окремого nominal-сенсора нема) →
     оцінка автономності: (залишок − 15% номінальної) × 51.2 В / load_power.

   API: new DeyeOutageTab(card).mount(containerEl) / .unmount()
   card — інстанс DeyeCard: card._hass, card._config.outage, card._eid(), card._on(),
   card._n(), card._st(), card._estate(), card._bmsPfx(), card._bms(), card._loadW(),
   card._fetchHistory(), card._critState(), card._modeExplain(), card._shelvesTxt(),
   card._shelvesWarnTxt(). Нічого в HA не пишемо. */

const DAY_MS = 86400000;
const HALF_MS = 1800000;
const DTEK_TTL = 10 * 60 * 1000;      // кеш DTEK-json у памʼяті — 10 хв
const DTEK_LS_KEY_PFX = 'deye-outage-dtek-v1'; // localStorage-фолбек, якщо зовсім нема інтернету (+group у ключі)
const CAL_TTL = 5 * 60 * 1000;
const HIST_TTL = 5 * 60 * 1000;
const REFRESH_MS = 60 * 1000;    // перемальовка (таймлайн/статус) — раз на хвилину, поки вкладка відкрита

const STATUS_COLOR = { yes: '#34c759', maybe: '#ff9f0a', no: '#ff3b30', emergency: '#8b0000' };
const STATUS_LABEL_SHORT = { maybe: 'можливо', no: 'світла немає' };
const STATUS_LABEL_BIG = { yes: 'СВІТЛО Є', maybe: 'МОЖЛИВО ВІДКЛЮЧЕННЯ', no: 'СВІТЛА НЕМАЄ', emergency: 'АВАРІЙНЕ ВІДКЛЮЧЕННЯ' };
const WEEKDAY_FULL = { 1: 'Понеділок', 2: 'Вівторок', 3: 'Середа', 4: 'Четвер', 5: "П'ятниця", 6: 'Субота', 7: 'Неділя' };

// розбиває погодинний статус ДТЕК на дві півгодинні клітинки (для first/second/mfirst/msecond)
function halfSlots(status) {
  switch (status) {
    case 'no': return ['no', 'no'];
    case 'maybe': return ['maybe', 'maybe'];
    case 'first': return ['no', 'yes'];
    case 'second': return ['yes', 'no'];
    case 'mfirst': return ['maybe', 'yes'];
    case 'msecond': return ['yes', 'maybe'];
    case 'yes': default: return ['yes', 'yes'];
  }
}

function pad2(n) { return String(n).padStart(2, '0'); }
function fmtHM(d) { return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); }
function fmtWd(d) { return d.toLocaleDateString('uk-UA', { weekday: 'short' }).replace('.', ''); }
function fmtDM(d) { return pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1); }
function fmtDur(ms) {
  if (ms == null || !isFinite(ms) || ms < 0) ms = 0;
  const totalMin = Math.round(ms / 60000);
  const h = Math.floor(totalMin / 60), m = totalMin % 60;
  if (h <= 0) return `${m} хв`;
  return m ? `${h} год ${m} хв` : `${h} год`;
}
function localDayStart(ms) { const d = new Date(ms); d.setHours(0, 0, 0, 0); return d.getTime(); }
function factUpdHM(s) { if (!s) return '—'; const parts = String(s).split(' '); return parts[1] || s; }
// укр. плюралізація: 1 відключення / 2-4 відключення / 5+ відключень
function pluralOutage(n) {
  const n10 = n % 10, n100 = n % 100;
  if (n10 === 1 && n100 !== 11) return `${n} відключення`;
  if (n10 >= 2 && n10 <= 4 && (n100 < 10 || n100 >= 20)) return `${n} відключення`;
  return `${n} відключень`;
}

const CSS = `
  .owrap{display:flex;flex-direction:column;gap:14px;}
  .oload{padding:30px 10px;text-align:center;color:var(--secondary-text-color);font-size:.85rem;}
  .oerr{padding:20px 10px;text-align:center;color:var(--error-color,#ff3b30);font-size:.82rem;}
  .oerr-mini{font-size:.72rem;color:var(--error-color,#ff3b30);background:color-mix(in srgb,#ff3b30 12%,transparent);
    border-radius:10px;padding:7px 10px;line-height:1.4;}
  .osect{display:flex;align-items:center;gap:6px;font-size:.62rem;font-weight:700;text-transform:uppercase;
    letter-spacing:.05em;color:var(--secondary-text-color);margin:2px 2px 0;}

  .ostatus{display:grid;grid-template-columns:1.5fr .8fr 1fr;gap:8px;padding:14px;border-radius:16px;
    background:var(--secondary-background-color);border:1px solid var(--divider-color);transition:border-color .2s;}
  .ostatus.os-green{border-color:#34c759;}
  .ostatus.os-amber{border-color:#ff9f0a;}
  .ostatus.os-red{border-color:#ff3b30;animation:ocritpulse 1.6s ease-in-out infinite;}
  @keyframes ocritpulse{0%,100%{box-shadow:0 0 0 1px color-mix(in srgb,#ff3b30 40%,transparent);}50%{box-shadow:0 0 14px color-mix(in srgb,#ff3b30 55%,transparent);}}
  .ostate{display:flex;flex-direction:column;gap:2px;min-width:0;justify-content:center;}
  .ostate b{font-size:1rem;font-weight:800;color:var(--primary-text-color);line-height:1.2;}
  .ostate span{font-size:.72rem;font-weight:700;color:var(--secondary-text-color);}
  .osoc,.oauto{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;
    border-left:1px solid var(--divider-color);padding-left:8px;}
  .osoc b,.oauto b{font-size:.94rem;font-weight:800;color:var(--primary-text-color);font-variant-numeric:tabular-nums;white-space:nowrap;}
  .osoc span,.oauto span{font-size:.58rem;font-weight:700;text-transform:uppercase;letter-spacing:.03em;color:var(--secondary-text-color);}

  .otl-legend{display:flex;flex-wrap:wrap;gap:12px;margin:2px 2px 0;}
  .oll{display:inline-flex;align-items:center;gap:4px;font-size:.64rem;color:var(--secondary-text-color);}
  .oll i{width:9px;height:9px;border-radius:3px;display:inline-block;}
  .oll i.half{background:linear-gradient(135deg,#ff3b30 50%,#34c759 50%);}

  .otl-row{display:flex;flex-direction:column;gap:3px;margin-bottom:6px;}
  .otl-lbl{display:flex;align-items:center;gap:6px;font-size:.76rem;font-weight:800;color:var(--primary-text-color);}
  .otl-lbl .otl-now-lbl{font-size:.62rem;font-weight:700;color:var(--acc);}
  .otl-bar{position:relative;height:30px;border-radius:9px;cursor:pointer;
    background:color-mix(in srgb,#34c759 16%,var(--secondary-background-color));
    overflow:hidden;border:1px solid var(--divider-color);}
  .otl-seg{position:absolute;top:0;bottom:0;cursor:pointer;}
  .otl-hgrid{position:absolute;inset:0;pointer-events:none;mix-blend-mode:overlay;opacity:.4;
    background-image:repeating-linear-gradient(90deg,transparent 0,transparent calc(100%/24 - 1px),rgba(255,255,255,.4) calc(100%/24 - 1px),rgba(255,255,255,.4) calc(100%/24));}
  .otl-now{position:absolute;top:0;bottom:0;width:2px;background:var(--acc);box-shadow:0 0 5px var(--acc);pointer-events:none;z-index:2;}
  .otl-ticks{position:relative;height:13px;margin-top:1px;}
  .otl-tick{position:absolute;top:0;font-size:.56rem;font-weight:600;color:var(--secondary-text-color);opacity:.65;white-space:nowrap;}
  .otl-note{font-size:.68rem;color:var(--secondary-text-color);opacity:.75;font-style:italic;padding:2px 1px 0;}
  .otl-note-ok{color:#34c759;opacity:1;font-style:normal;font-weight:700;}
  .otl-src{font-size:.6rem;color:var(--secondary-text-color);opacity:.7;padding:1px 1px 0;}

  .otl-windows{display:flex;flex-direction:column;gap:3px;margin-top:2px;}
  .otl-wrow{display:flex;align-items:center;gap:7px;font-size:.74rem;padding:5px 9px;border-radius:9px;
    background:var(--secondary-background-color);cursor:pointer;}
  .otl-wrow i{width:9px;height:9px;border-radius:3px;flex:none;}
  .otl-wrow span{color:var(--primary-text-color);font-weight:700;white-space:nowrap;}
  .otl-wrow b{color:var(--secondary-text-color);font-weight:700;flex:1;}
  .otl-wrow em{color:var(--secondary-text-color);font-style:normal;font-weight:700;white-space:nowrap;}

  .onext{display:flex;flex-direction:column;gap:4px;padding:11px 13px;border-radius:12px;background:var(--secondary-background-color);}
  .onext .orow{font-size:.8rem;color:var(--primary-text-color);}
  .onext .orow b{font-weight:800;}
  .onext .ofact-ok{color:#34c759;font-weight:700;}
  .osrc{font-size:.6rem;color:var(--secondary-text-color);opacity:.7;margin-top:3px;}
  .odtekmeta{font-size:.6rem;color:var(--secondary-text-color);opacity:.7;text-align:right;margin-top:-6px;}

  .omode{display:flex;flex-direction:column;gap:5px;padding:12px 13px;border-radius:12px;background:var(--secondary-background-color);}
  .omode .ol1{font-size:.86rem;font-weight:800;color:var(--primary-text-color);}
  .omode .ol2{font-size:.74rem;color:var(--secondary-text-color);}
  .omode .critw{color:#ff3b30;font-weight:800;}
  .omode .oflag{font-size:.72rem;color:var(--secondary-text-color);margin-top:3px;padding-top:5px;border-top:1px solid var(--divider-color);}
  .omode .oflag b{color:var(--primary-text-color);font-weight:800;}

  .ohist{display:flex;flex-direction:column;gap:5px;}
  .ohrow{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:.78rem;
    padding:7px 11px;border-radius:10px;background:var(--secondary-background-color);}
  .ohrow span{color:var(--secondary-text-color);}
  .ohrow b{font-weight:800;color:var(--primary-text-color);white-space:nowrap;}
  .ohsum{font-size:.72rem;font-weight:700;color:var(--secondary-text-color);text-align:right;padding:3px 4px 0;}
  .ohempty{font-size:.78rem;color:var(--secondary-text-color);opacity:.75;padding:10px 2px;text-align:center;}

  .oovl{position:fixed;inset:0;z-index:50;display:flex;align-items:flex-end;justify-content:center;
    background:rgba(0,0,0,0);backdrop-filter:blur(0px);transition:background .22s,backdrop-filter .22s;}
  .oovl.show{background:rgba(0,0,0,.5);backdrop-filter:blur(7px);}
  .opanel{width:min(420px,94vw);max-height:86vh;overflow:auto;margin:0 0 max(env(safe-area-inset-bottom),10px);
    background:color-mix(in srgb,var(--card-background-color) 90%,transparent);
    border:1px solid var(--divider-color);border-radius:22px 22px 16px 16px;
    box-shadow:0 -10px 40px rgba(0,0,0,.45);padding:14px 16px 16px;
    transform:translateY(40px) scale(.96);opacity:0;transition:transform .24s cubic-bezier(.2,.9,.2,1),opacity .18s;}
  .oovl.show .opanel{transform:translateY(0) scale(1);opacity:1;}
  @media(min-width:560px){.oovl{align-items:center;}.opanel{border-radius:20px;margin:0;}}
  .oph{display:flex;align-items:center;gap:9px;padding:0 0 10px;font-size:1rem;font-weight:800;color:var(--primary-text-color);}
  .oph .opclose{margin-left:auto;border:none;background:var(--secondary-background-color);color:var(--secondary-text-color);
    width:30px;height:30px;border-radius:50%;cursor:pointer;flex:none;display:inline-flex;align-items:center;justify-content:center;font-size:.9rem;}
  .opbig{font-size:1.05rem;font-weight:900;letter-spacing:.01em;padding:4px 0 10px;}
  .oprow{display:flex;justify-content:space-between;gap:10px;font-size:.78rem;padding:6px 0;border-top:1px solid var(--divider-color);}
  .oprow span{color:var(--secondary-text-color);}
  .oprow b{color:var(--primary-text-color);font-weight:700;text-align:right;}
  .opnote{font-size:.74rem;color:var(--secondary-text-color);padding-top:8px;margin-top:4px;border-top:1px solid var(--divider-color);}
  .opnote b{color:var(--primary-text-color);}
`;

export class DeyeOutageTab {
  constructor(card) {
    this.card = card;
    this.container = null;
    this.cache = new Map();   // key -> {at, data} — для календарного фолбеку/історії
    this.timer = null;
    this._loaded = false;
    this._popupOpen = false;
    this._segMap = new Map();
    this._segSeq = 0;
    this._dtekCache = null;   // {at, json, fresh}
    // конфіг картки: outage: {source_url, group, calendar_scheduled?, calendar_planned?, emergency_entities?}
    const oc = (card._config && card._config.outage) || {};
    this.dtekUrl = oc.source_url || null;
    this.group = oc.group || null;
    this.calScheduled = oc.calendar_scheduled || null;
    this.calPlanned = oc.calendar_planned || null;
    this.emergencyEids = Array.isArray(oc.emergency_entities) ? oc.emergency_entities : [];
    this.dtekLsKey = DTEK_LS_KEY_PFX + ':' + (this.group || 'none');
  }

  mount(container) {
    this.container = container;
    if (!this.dtekUrl || !this.group) {
      container.innerHTML = `<style>${CSS}</style><div class="oerr">Вкладку «Відключення» не налаштовано.<br>Додай у конфіг картки <code>outage: { source_url: '…', group: '…' }</code> — дивись README.</div>`;
      return;
    }
    container.innerHTML = `<style>${CSS}</style><div class="oload">Завантаження…</div>`;
    this._load();
    this.timer = setInterval(() => { if (this.container) this._load(); }, REFRESH_MS);
  }

  unmount() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.container = null;
    if (this._escHandler) { document.removeEventListener('keydown', this._escHandler); this._escHandler = null; }
  }

  async _load() {
    if (!this.container || !this.card._hass || this._popupOpen) return;
    try {
      const now = Date.now();
      const todayStart = localDayStart(now);
      const [dtek, hist] = await Promise.all([this._fetchDtek(), this._historyIntervals()]);
      if (!this.container || this._popupOpen) return; // вкладку закрили / відкрили попап, поки чекали на відповіді
      let html;
      this._segMap = new Map(); this._segSeq = 0;
      if (dtek.json) {
        const todayInfo = this._dayInfo(dtek.json, todayStart);
        const tomorrowInfo = this._dayInfo(dtek.json, todayStart + DAY_MS);
        html = this._composeHTML(now, todayStart, todayInfo, tomorrowInfo, hist, dtek);
      } else {
        const [todaySeg, tomorrowSeg] = await Promise.all([
          this._calDaySegments(todayStart), this._calDaySegments(todayStart + DAY_MS),
        ]);
        if (!this.container || this._popupOpen) return;
        html = this._composeFallbackHTML(now, todayStart, todaySeg, tomorrowSeg, hist);
      }
      this._loaded = true;
      this.container.innerHTML = html;
      this._wire();
    } catch (err) {
      console.error('deye-outage: load failed', err);
      if (this.container && !this._loaded) {
        this.container.innerHTML = `<style>${CSS}</style><div class="oerr">Не вдалось завантажити дані відключень.</div>`;
      }
    }
  }

  // ── DTEK JSON: кеш 10 хв у памʼяті, фолбек на localStorage, якщо fetch не вдався ──
  async _fetchDtek() {
    const now = Date.now();
    if (this._dtekCache && now - this._dtekCache.at < DTEK_TTL) return this._dtekCache;
    try {
      const res = await fetch(this.dtekUrl, { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const json = await res.json();
      const rec = { at: now, json, fresh: true };
      this._dtekCache = rec;
      try { localStorage.setItem(this.dtekLsKey, JSON.stringify({ at: now, json })); } catch (e) { /* приватний режим тощо — ігноруємо */ }
      return rec;
    } catch (err) {
      console.error('deye-outage: DTEK fetch failed, пробуємо кеш', err);
      try {
        const raw = localStorage.getItem(this.dtekLsKey);
        if (raw) {
          const cached = JSON.parse(raw);
          const rec = { at: cached.at, json: cached.json, fresh: false };
          this._dtekCache = rec;
          return rec;
        }
      } catch (e) { /* кеш теж недоступний */ }
      return { at: now, json: null, fresh: false };
    }
  }

  // ── факт/шаблон на конкретний день з DTEK-json ──
  _factHourMapFor(json, dayStartMs) {
    const fd = json && json.fact && json.fact.data;
    if (!fd) return null;
    const exact = fd[String(Math.round(dayStartMs / 1000))];
    if (exact) return exact[this.group] || null;
    // фолбек: шукаємо запис з тією ж календарною датою (на випадок дрібного зсуву tz)
    const target = new Date(dayStartMs);
    for (const k of Object.keys(fd)) {
      const d = new Date(Number(k) * 1000);
      if (d.getFullYear() === target.getFullYear() && d.getMonth() === target.getMonth() && d.getDate() === target.getDate()) {
        return fd[k][this.group] || null;
      }
    }
    return null;
  }

  _presetHourMapFor(json, dayStartMs) {
    const pd = json && json.preset && json.preset.data && json.preset.data[this.group];
    if (!pd) return null;
    const wd = ((new Date(dayStartMs).getDay() + 6) % 7) + 1; // 1=Пн..7=Нд
    return { map: pd[String(wd)] || null, weekday: wd };
  }

  _dayInfo(json, dayStartMs) {
    const factMap = this._factHourMapFor(json, dayStartMs);
    if (factMap) return { slots: this._hourMapToSlots(factMap), source: 'fact', factUpdate: json.fact.update };
    const p = this._presetHourMapFor(json, dayStartMs);
    if (p && p.map) return { slots: this._hourMapToSlots(p.map), source: 'preset', weekday: p.weekday };
    return { slots: null, source: null };
  }

  _hourMapToSlots(hourMap) {
    const slots = [];
    for (let h = 1; h <= 24; h++) {
      const st = hourMap[String(h)] || 'yes';
      const pair = halfSlots(st);
      slots.push(pair[0], pair[1]);
    }
    return slots;
  }

  // 48 півгодинних клітинок → злиті сусідні відрізки одного статусу
  _mergeSegments(slots, dayStartMs) {
    const out = [];
    let i = 0;
    while (i < slots.length) {
      const st = slots[i];
      let j = i + 1;
      while (j < slots.length && slots[j] === st) j++;
      out.push({ startMs: dayStartMs + i * HALF_MS, endMs: dayStartMs + j * HALF_MS, status: st });
      i = j;
    }
    return out;
  }

  _regSeg(seg, dayStartMs, meta) {
    const id = String(this._segSeq++);
    this._segMap.set(id, Object.assign({}, seg, { dayStartMs, source: meta.source, factUpdate: meta.factUpdate, weekday: meta.weekday }));
    return id;
  }

  // ── календар HA: фолбек №2, лише коли DTEK json зовсім недоступний (нема ні fetch, ні кешу) ──
  // Опційний: outage.calendar_scheduled / outage.calendar_planned у конфізі картки.
  // Якщо не задано — просто нема подій з цього джерела (без запитів і без помилок).
  async _calDaySegments(dayStart) {
    const dayEnd = dayStart + DAY_MS;
    const [scheduled, planned] = await Promise.all([
      this._calEvents(this.calScheduled, dayStart, dayEnd),
      this._calEvents(this.calPlanned, dayStart, dayEnd),
    ]);
    return { scheduled: this._clip(scheduled, dayStart, dayEnd), planned: this._clip(planned, dayStart, dayEnd) };
  }

  async _calEvents(calEid, startMs, endMs) {
    if (!calEid) return [];
    const key = `cal:${calEid}:${startMs}`;
    const c = this.cache.get(key);
    if (c && Date.now() - c.at < CAL_TTL) return c.data;
    let data = [];
    try {
      const startIso = new Date(startMs).toISOString(), endIso = new Date(endMs).toISOString();
      const res = await this.card._hass.callApi('GET',
        `calendars/${calEid}?start=${encodeURIComponent(startIso)}&end=${encodeURIComponent(endIso)}`);
      data = (res || []).map(e => ({
        start: Date.parse((e.start && (e.start.dateTime || e.start.date)) || ''),
        end: Date.parse((e.end && (e.end.dateTime || e.end.date)) || ''),
      })).filter(e => isFinite(e.start) && isFinite(e.end) && e.end > e.start);
    } catch (err) { console.error('deye-outage: calendar fetch failed', calEid, err); }
    this.cache.set(key, { at: Date.now(), data });
    return data;
  }

  _clip(events, dayStart, dayEnd) {
    return events
      .map(e => ({ start: Math.max(e.start, dayStart), end: Math.min(e.end, dayEnd) }))
      .filter(e => e.end > e.start)
      .sort((a, b) => a.start - b.start);
  }

  // ── 7-денна історія: card._fetchHistory() на напрузі вводу, <150В = нема мережі ──
  async _historyIntervals() {
    const now = Date.now();
    const key = 'h7d';
    const c = this.cache.get(key);
    if (c && now - c.at < HIST_TTL) return c.data;
    const start = now - 7 * DAY_MS;
    const eid = this.card._eid('grid_l1_voltage');
    const TH = 150;
    const out = [];
    try {
      const pts = await this.card._fetchHistory(eid, start, now);
      let state = null, segStart = null;
      pts.forEach(p => {
        const isOut = p.v < TH;
        if (state === null) { state = isOut; if (isOut) segStart = start; return; }
        if (isOut !== state) {
          if (state) out.push({ start: segStart, end: p.t });
          state = isOut;
          if (isOut) segStart = p.t;
        }
      });
      if (state) out.push({ start: segStart, end: now });
    } catch (err) { console.error('deye-outage: history fetch failed', err); }
    // шум <1хв (глюк вимірювання/дрейф) не рахуємо за відключення
    const data = out.filter(o => o.end - o.start >= 60000);
    this.cache.set(key, { at: now, data });
    return data;
  }

  // ── автономність: (залишок ємності обох паків − 15% сумарної «номінальної») × 51.2В / навантаження ──
  _autonomyText() {
    const card = this.card;
    let remain = 0, nominal = 0, any = false;
    card._bmsPfx().forEach((pfx, pi) => {
      const r = card._bms('zalishok_iemnosti', pi), n = card._bms('realna_iemnist', pi);
      if (r !== null) { remain += r; any = true; }
      if (n !== null) nominal += n;
    });
    if (!any) return '—';
    const loadW = card._loadW();
    if (loadW < 50) return '> 48 год';
    const usableAh = remain - nominal * 0.15;
    if (usableAh <= 0) return '~0 год';
    const hours = (usableAh * 51.2) / loadW;
    if (hours > 48) return '> 48 год';
    const h = Math.floor(hours), m = Math.round((hours - h) * 60);
    return m ? `${h} год ${m} хв` : `${h} год`;
  }

  // ── верхній статус мережі ──
  _topStatusHTML() {
    const card = this.card;
    const gridOn = card._on('grid');
    const genp = card._n('generator_power');
    const genOn = card._on('generator') || genp > 15;
    let title, sub, cls;
    if (!gridOn) {
      const st = card._hass.states[card._eid('grid', 'binary_sensor')];
      const since = st && st.last_changed ? Date.parse(st.last_changed) : null;
      const dur = since ? Date.now() - since : null;
      title = `🔌 Світла нема${since ? ' з ' + fmtHM(new Date(since)) : ''}`;
      sub = dur != null ? `вже ${fmtDur(dur)}` : '';
      cls = 'red';
    } else if (genOn) {
      title = '⛽ Генератор'; sub = card._fmtW(genp); cls = 'amber';
    } else {
      title = '⚡ Мережа є'; sub = ''; cls = 'green';
    }
    const soc = card._n('battery');
    return `
      <div class="ostatus os-${cls}">
        <div class="ostate"><b>${title}</b>${sub ? `<span>${sub}</span>` : ''}</div>
        <div class="osoc"><b>${Math.round(soc)}%</b><span>SOC</span></div>
        <div class="oauto"><b>${this._autonomyText()}</b><span>на батареях</span></div>
      </div>`;
  }

  _legendHTML() {
    return `<div class="otl-legend">
      <span class="oll"><i style="background:${STATUS_COLOR.yes}"></i>світло є</span>
      <span class="oll"><i style="background:${STATUS_COLOR.maybe}"></i>можливо</span>
      <span class="oll"><i style="background:${STATUS_COLOR.no}"></i>світла немає</span>
      <span class="oll"><i class="half"></i>половина години — інший тип</span>
    </div>`;
  }

  _hourTicks() {
    return [0, 3, 6, 9, 12, 15, 18, 21, 24].map(h => {
      const pct = h / 24 * 100;
      const tf = h === 0 ? 'translateX(0)' : h === 24 ? 'translateX(-100%)' : 'translateX(-50%)';
      return `<div class="otl-tick" style="left:${pct}%;transform:${tf}">${pad2(h === 24 ? 24 : h)}</div>`;
    }).join('');
  }

  _segDiv(seg, dayStart, segId) {
    const l = (seg.startMs - dayStart) / DAY_MS * 100;
    const w = Math.max(0.3, (seg.endMs - seg.startMs) / DAY_MS * 100);
    const bg = STATUS_COLOR[seg.status] || '#8a93a0';
    return `<div class="otl-seg" data-seg="${segId}" style="left:${l.toFixed(3)}%;width:${w.toFixed(3)}%;background:${bg}"></div>`;
  }

  _windowsListHTML(regs) {
    if (!regs.length) return '';
    return `<div class="otl-windows">${regs.map(r => {
      const s = r.seg;
      const col = STATUS_COLOR[s.status] || '#8a93a0';
      const lbl = STATUS_LABEL_SHORT[s.status] || s.status;
      return `<div class="otl-wrow" data-seg="${r.id}"><i style="background:${col}"></i>` +
        `<span>${fmtHM(new Date(s.startMs))}–${fmtHM(new Date(s.endMs))}</span>` +
        `<b>${lbl}</b><em>${fmtDur(s.endMs - s.startMs)}</em></div>`;
    }).join('')}</div>`;
  }

  // ── таймлайн одного дня (DTEK-режим): 48 півгодинних клітинок, факт або шаблон ──
  _timelineHTML(label, dayInfo, dayStartMs, nowMs) {
    let segHTML = '', listHTML = '', note = '';
    if (!dayInfo.slots) {
      note = `<div class="otl-note">дані відсутні</div>`;
    } else {
      const merged = this._mergeSegments(dayInfo.slots, dayStartMs).filter(s => s.status !== 'yes');
      const regs = merged.map(s => ({ id: this._regSeg(s, dayStartMs, dayInfo), seg: s }));
      segHTML = regs.map(r => this._segDiv(r.seg, dayStartMs, r.id)).join('');
      listHTML = this._windowsListHTML(regs);
      if (!regs.length) {
        note = dayInfo.source === 'fact'
          ? `<div class="otl-note otl-note-ok">✅ за фактом ДТЕК відключень немає</div>`
          : `<div class="otl-note">за шаблоном тижня відключень немає</div>`;
      }
    }
    const showNow = nowMs != null && nowMs >= dayStartMs && nowMs < dayStartMs + DAY_MS;
    const nowPct = showNow ? (nowMs - dayStartMs) / DAY_MS * 100 : 0;
    const nowMarker = showNow ? `<div class="otl-now" style="left:${nowPct.toFixed(3)}%"></div>` : '';
    const srcTxt = !dayInfo.slots ? ''
      : dayInfo.source === 'fact' ? `факт ДТЕК · опубліковано ${factUpdHM(dayInfo.factUpdate)}`
        : `шаблон тижня (${WEEKDAY_FULL[dayInfo.weekday] || ''}) · факт ще не опубліковано`;
    return `
      <div class="otl-row">
        <div class="otl-lbl">${label}${showNow ? '<span class="otl-now-lbl">● зараз</span>' : ''}</div>
        <div class="otl-bar">${segHTML}${nowMarker}<div class="otl-hgrid"></div></div>
        <div class="otl-ticks">${this._hourTicks()}</div>
        ${note}
        ${listHTML}
        ${srcTxt ? `<div class="otl-src">${srcTxt}</div>` : ''}
      </div>`;
  }

  // ── наступне відключення — об'єднаний список non-yes сьогодні+завтра, з того ж DTEK-джерела ──
  _nextOutageInfo(now, todayStart, todayInfo, tomorrowInfo) {
    const segsToday = todayInfo.slots ? this._mergeSegments(todayInfo.slots, todayStart).filter(s => s.status !== 'yes') : [];
    const segsTomorrow = tomorrowInfo.slots ? this._mergeSegments(tomorrowInfo.slots, todayStart + DAY_MS).filter(s => s.status !== 'yes') : [];
    const all = segsToday.concat(segsTomorrow).sort((a, b) => a.startMs - b.startMs);
    const current = all.find(s => s.startMs <= now && now < s.endMs);
    const upcoming = all.find(s => s.startMs > now);
    return { current, upcoming, todayEmpty: !segsToday.length };
  }

  _srcSummary(todayInfo) {
    if (todayInfo.source === 'fact') return `джерело: ДТЕК (факт на сьогодні) · черга ${this.group}`;
    if (todayInfo.source === 'preset') return `джерело: ДТЕК (тижневий шаблон) · черга ${this.group}`;
    return 'джерело: невідомо';
  }

  _nextRowHTML(now, info, todayInfo) {
    const txt = s => STATUS_LABEL_SHORT[s.status] || s.status;
    let body;
    if (info.current) {
      body = `<div class="orow">⚡ зараз: <b style="color:${STATUS_COLOR[info.current.status]}">${txt(info.current)}</b> до ${fmtHM(new Date(info.current.endMs))} (ще ${fmtDur(info.current.endMs - now)})</div>` +
        (info.upcoming ? `<div class="orow">🔌 далі: <b>${txt(info.upcoming)}</b> ${fmtWd(new Date(info.upcoming.startMs))} ${fmtHM(new Date(info.upcoming.startMs))} (через ${fmtDur(info.upcoming.startMs - now)})</div>` : '');
    } else if (info.upcoming) {
      body = `<div class="orow">🔌 наступне: <b style="color:${STATUS_COLOR[info.upcoming.status]}">${txt(info.upcoming)}</b> ${fmtWd(new Date(info.upcoming.startMs))} ${fmtHM(new Date(info.upcoming.startMs))}–${fmtHM(new Date(info.upcoming.endMs))} (через ${fmtDur(info.upcoming.startMs - now)})</div>`;
    } else {
      body = `<div class="orow">наступних відключень не видно в доступних даних (сьогодні+завтра)</div>`;
    }
    const factNote = (todayInfo.source === 'fact' && info.todayEmpty)
      ? `<div class="orow ofact-ok">✅ за фактом ДТЕК сьогодні відключень не заплановано (оновлено ${factUpdHM(todayInfo.factUpdate)})</div>`
      : '';
    return `<div class="onext">${factNote}${body}<div class="osrc">${this._srcSummary(todayInfo)}</div></div>`;
  }

  // ── блок «Режим» — той самий _critState()/_modeExplain(), що й банер картки ──
  _modeBlockHTML() {
    const card = this.card;
    const cs = card._critState();
    const explain = card._modeExplain();
    const seen = card._estate('input_boolean.deye_outage_seen_today') === 'on';
    return `
      <div class="omode">
        <div class="ol1">${cs.icon} ${cs.label}</div>
        <div class="ol2">${explain}</div>
        <div class="ol2">полички ${card._shelvesTxt()}${cs.warn ? ' <span class="critw">' + card._shelvesWarnTxt() + '</span>' : ''}</div>
        <div class="oflag">сьогодні було відключення &gt;10 хв: <b>${seen ? 'так' : 'ні'}</b></div>
      </div>`;
  }

  // ── історія 7 діб ──
  _historyHTML(hist) {
    if (!hist.length) return `<div class="ohempty">За останні 7 діб відключень мережі не зафіксовано.</div>`;
    const sorted = hist.slice().sort((a, b) => b.start - a.start);
    const rows = sorted.map(h => {
      const d = new Date(h.start), de = new Date(h.end);
      return `<div class="ohrow"><span>${fmtWd(d)} ${pad2(d.getDate())}.${pad2(d.getMonth() + 1)} ${fmtHM(d)}–${fmtHM(de)}</span><b>${fmtDur(h.end - h.start)}</b></div>`;
    }).join('');
    const totalMs = hist.reduce((s, h) => s + (h.end - h.start), 0);
    const totalH = (Math.round(totalMs / 360000) / 10).toString().replace('.', ',');
    return `<div class="ohist">${rows}</div><div class="ohsum">за 7 діб: ${pluralOutage(hist.length)}, ${totalH} год</div>`;
  }

  _composeHTML(now, todayStart, todayInfo, tomorrowInfo, hist, dtek) {
    const nextInfo = this._nextOutageInfo(now, todayStart, todayInfo, tomorrowInfo);
    const fetchedTxt = fmtHM(new Date(dtek.at)) + (dtek.fresh ? '' : ' (кеш, інтернет недоступний)');
    return `<style>${CSS}</style><div class="owrap">
      ${this._topStatusHTML()}
      ${this._sect('Графік відключень · черга ' + this.group)}
      ${this._legendHTML()}
      ${this._timelineHTML('Сьогодні', todayInfo, todayStart, now)}
      ${this._timelineHTML('Завтра', tomorrowInfo, todayStart + DAY_MS, null)}
      <div class="odtekmeta">дані отримано ${fetchedTxt}</div>
      ${this._nextRowHTML(now, nextInfo, todayInfo)}
      ${this._sect('Режим')}
      ${this._modeBlockHTML()}
      ${this._sect('Історія за 7 діб (факт по напрузі)')}
      ${this._historyHTML(hist)}
    </div>`;
  }

  // ── резервний режим: DTEK json зовсім недоступний (нема ні fetch, ні кешу) — старі календарі HA ──
  _composeFallbackHTML(now, todayStart, todaySeg, tomorrowSeg, hist) {
    const toRegs = (seg, dayStart) => {
      const list = seg.scheduled.map(e => ({ startMs: e.start, endMs: e.end, status: 'no' }))
        .concat(seg.planned.map(e => ({ startMs: e.start, endMs: e.end, status: 'maybe' })))
        .sort((a, b) => a.startMs - b.startMs);
      return list.map(s => ({ id: this._regSeg(s, dayStart, { source: 'calendar-fallback' }), seg: s }));
    };
    const todayRegs = toRegs(todaySeg, todayStart);
    const tomorrowRegs = toRegs(tomorrowSeg, todayStart + DAY_MS);
    const rowHTML = (label, regs, dayStart, nowMs) => {
      const segHTML = regs.map(r => this._segDiv(r.seg, dayStart, r.id)).join('');
      const listHTML = this._windowsListHTML(regs);
      const showNow = nowMs != null;
      const nowPct = showNow ? (nowMs - dayStart) / DAY_MS * 100 : 0;
      const nowMarker = showNow ? `<div class="otl-now" style="left:${nowPct.toFixed(3)}%"></div>` : '';
      const note = !regs.length ? `<div class="otl-note">відключень не видно (резервне джерело)</div>` : '';
      return `<div class="otl-row">
        <div class="otl-lbl">${label}${showNow ? '<span class="otl-now-lbl">● зараз</span>' : ''}</div>
        <div class="otl-bar">${segHTML}${nowMarker}<div class="otl-hgrid"></div></div>
        <div class="otl-ticks">${this._hourTicks()}</div>${note}${listHTML}
        <div class="otl-src">резервне джерело (календар HA) · орієнтовно</div></div>`;
    };
    const all = todayRegs.concat(tomorrowRegs).map(r => r.seg).sort((a, b) => a.startMs - b.startMs);
    const current = all.find(s => s.startMs <= now && now < s.endMs);
    const upcoming = all.find(s => s.startMs > now);
    const txt = s => STATUS_LABEL_SHORT[s.status] || s.status;
    const nextTxt = current
      ? `зараз: <b style="color:${STATUS_COLOR[current.status]}">${txt(current)}</b> до ${fmtHM(new Date(current.endMs))}`
      : upcoming
        ? `наступне: <b style="color:${STATUS_COLOR[upcoming.status]}">${txt(upcoming)}</b> ${fmtWd(new Date(upcoming.startMs))} ${fmtHM(new Date(upcoming.startMs))} (через ${fmtDur(upcoming.startMs - now)})`
        : 'відключень не видно (резервне джерело)';
    return `<style>${CSS}</style><div class="owrap">
      ${this._topStatusHTML()}
      <div class="oerr-mini">⚠ ДТЕК JSON недоступний (нема інтернету й нема кешу) — показано орієнтовний графік з календаря HA (резервне джерело, не точний факт).</div>
      ${this._sect('Графік відключень · черга ' + this.group + ' (резерв)')}
      ${this._legendHTML()}
      ${rowHTML('Сьогодні', todayRegs, todayStart, now)}
      ${rowHTML('Завтра', tomorrowRegs, todayStart + DAY_MS, null)}
      <div class="onext"><div class="orow">${nextTxt}</div></div>
      ${this._sect('Режим')}
      ${this._modeBlockHTML()}
      ${this._sect('Історія за 7 діб (факт по напрузі)')}
      ${this._historyHTML(hist)}
    </div>`;
  }

  _sect(label) { return `<div class="osect">${label}</div>`; }

  // ── wiring: клік на вікно (бар або рядок списку) → попап з повною інфою ──
  _wire() {
    if (!this.container) return;
    this.container.querySelectorAll('[data-seg]').forEach(el => {
      el.addEventListener('click', () => {
        const entry = this._segMap.get(el.dataset.seg);
        if (entry) this._openPopup(entry);
      });
    });
  }

  _openPopup(entry) {
    if (!this.container) return;
    this._popupOpen = true;
    const card = this.card;
    const now = Date.now();
    let big = STATUS_LABEL_BIG[entry.status] || entry.status;
    let bigColor = STATUS_COLOR[entry.status] || '#8a93a0';
    const isNow = now >= entry.startMs && now < entry.endMs;
    if (entry.status === 'no' && isNow && this.emergencyEids.some(eid => card._estate(eid) === 'on')) {
      big = STATUS_LABEL_BIG.emergency; bigColor = STATUS_COLOR.emergency;
    }
    const dayLabel = `${fmtWd(new Date(entry.startMs))} ${fmtDM(new Date(entry.startMs))}`;
    const timeLabel = `${fmtHM(new Date(entry.startMs))}–${fmtHM(new Date(entry.endMs))}`;
    const dur = fmtDur(entry.endMs - entry.startMs);
    let stateTxt;
    if (now < entry.startMs) stateTxt = `почнеться через ${fmtDur(entry.startMs - now)}`;
    else if (now < entry.endMs) stateTxt = `триває, лишилось ${fmtDur(entry.endMs - now)}`;
    else stateTxt = 'минуло';
    const srcTxt = entry.source === 'fact'
      ? `ДТЕК факт на ${fmtDM(new Date(entry.dayStartMs))}, опубліковано ${factUpdHM(entry.factUpdate)}`
      : entry.source === 'preset'
        ? `тижневий шаблон ДТЕК, ${WEEKDAY_FULL[entry.weekday] || ''}`
        : 'орієнтовно (резервне джерело — календар HA, не офіційний графік ДТЕК)';
    const gridNow = card._on('grid') ? 'є' : 'нема';

    const ov = document.createElement('div');
    ov.className = 'oovl';
    ov.innerHTML = `
      <div class="opanel">
        <div class="oph"><span>${dayLabel} · ${timeLabel}</span><button class="opclose" aria-label="Закрити">✕</button></div>
        <div class="opbig" style="color:${bigColor}">${big}</div>
        <div class="oprow"><span>тривалість</span><b>${dur}</b></div>
        <div class="oprow"><span>стан</span><b>${stateTxt}</b></div>
        <div class="oprow"><span>джерело</span><b>${srcTxt}</b></div>
        <div class="oprow"><span>черга</span><b>${this.group}</b></div>
        <div class="opnote">реальний стан мережі зараз: <b>${gridNow}</b></div>
      </div>`;
    this.container.appendChild(ov);
    requestAnimationFrame(() => ov.classList.add('show'));
    const close = () => this._closePopup(ov);
    ov.addEventListener('click', e => { if (e.target === ov) close(); });
    ov.querySelector('.opclose').addEventListener('click', close);
    this._escHandler = e => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', this._escHandler);
  }

  _closePopup(ov) {
    if (this._escHandler) { document.removeEventListener('keydown', this._escHandler); this._escHandler = null; }
    if (ov && ov.parentNode) { ov.classList.remove('show'); setTimeout(() => ov.remove(), 220); }
    this._popupOpen = false;
    this._load(); // підвантажити свіжий стан — поки попап був відкритий, періодичний _load() пропускався
  }
}
