/* deye-graphs.js — вкладка «Графіки» для deye-card (v44+).
   Динамічно підключається з deye-card.js через import(), щоб не роздувати головний файл
   (ліміт здорового глузду ~130 КБ). Дані по «звичайних» числових сенсорах ідуть ЧЕРЕЗ
   card._fetchHistory() — ту саму функцію, яку вже юзають тач-графіки історії по комірках
   (v41). Вона коректно передає end_time (⚠️ ГРАБЛІ: /api/history/period/<start> БЕЗ
   end_time віддає лише одну добу від start) — тут другий парсер історії НЕ пишемо.
   Для текстового сенсора «активне балансування» (стан ≠ «—», а не число) потрібен інший
   парсинг значення, тому fetchRawHistory() нижче — окремий тонкий фетчер, що повторює ТУ Ж
   схему запиту (WS history/history_during_period → фолбек REST history/period з end_time).

   API: new DeyeGraphsTab(card).mount(containerEl) / .unmount()
   card — інстанс DeyeCard: читаємо card._hass, card._pfx, card._bmsPfx(), card._eid(),
   card._bmsEid(), card._config, card._fetchHistory(). Нічого в HA не пишемо. */

const RANGES = { '6h': 6 * 36e5, '24h': 24 * 36e5, '7d': 7 * 864e5 };
const RANGE_ORDER = ['6h', '24h', '7d'];
const RANGE_LABELS = { '6h': '6г', '24h': '24г', '7d': '7д' };
const TTL = 60000; // кеш відповідей history API на 60с (не смикати API при кожному перемальовуванні)
const REFRESH_MS = 5 * 60 * 1000; // перемальовка з новими даними — раз на 5 хв, поки вкладка відкрита

function fmtVal(v, unit) {
  if (v == null || !isFinite(v)) return '—';
  const a = Math.abs(v);
  const dec = unit === '%' || unit === 'А' ? (a >= 100 ? 0 : 1) : a >= 100 ? 0 : a >= 10 ? 1 : 2;
  return v.toFixed(dec).replace('.', ',') + (unit ? ' ' + unit : '');
}
function fmtAxisVal(v) {
  const a = Math.abs(v);
  const dec = a >= 100 ? 0 : a >= 10 ? 1 : 2;
  return v.toFixed(dec).replace('.', ',');
}
function fmtAxisTime(t, span) {
  const d = new Date(t);
  if (span <= 26 * 36e5) return d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit' });
  if (span <= 8 * 864e5) return d.toLocaleDateString('uk-UA', { day: '2-digit', month: '2-digit' }) + ' ' + d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit' });
  return d.toLocaleDateString('uk-UA', { day: '2-digit', month: '2-digit' });
}
function nearestPoint(pts, t) {
  if (!pts || !pts.length) return null;
  let best = pts[0], bd = Infinity;
  for (const p of pts) { const d = Math.abs(p.t - t); if (d < bd) { bd = d; best = p; } }
  return best;
}
function isActiveBalance(s) {
  if (s == null) return false;
  const t = String(s).trim();
  return t !== '' && t !== '—' && t !== '-' && t !== 'unavailable' && t !== 'unknown';
}

// той самий запит-патерн, що й card._fetchHistory (WS → REST-фолбек, ЗАВЖДИ з end_time),
// але зберігаємо СИРИЙ стан (рядок), бо «балансуються комірки 7, 12» — не число
async function fetchRawHistory(hass, eid, startMs, endMs) {
  const startIso = new Date(startMs).toISOString();
  const endIso = new Date(endMs).toISOString();
  let raw = null;
  if (hass.callWS) {
    try {
      const res = await hass.callWS({
        type: 'history/history_during_period', start_time: startIso, end_time: endIso,
        entity_ids: [eid], minimal_response: true, no_attributes: true, significant_changes_only: false,
      });
      raw = res && res[eid];
    } catch (e) { /* фолбек нижче на REST */ }
  }
  if (!raw) {
    const path = `history/period/${startIso}?filter_entity_id=${encodeURIComponent(eid)}&end_time=${encodeURIComponent(endIso)}&minimal_response`;
    const res = await hass.callApi('GET', path);
    raw = res && res[0];
  }
  if (!raw || !raw.length) return [];
  return raw.map(e => {
    const lu = e.lu != null ? e.lu * 1000 : null;
    const t = lu != null ? lu : (e.last_changed ? Date.parse(e.last_changed) : (e.last_updated ? Date.parse(e.last_updated) : null));
    const s = e.s !== undefined ? e.s : e.state;
    return { t, s };
  }).filter(p => p.t).sort((a, b) => a.t - b.t);
}

// визначення шести показників-графіків (5 панелей; смуга балансування вбудована під 4-ту)
function chartDefs(card) {
  const bms = card._bmsPfx();
  const pn = card._config.bms_names || ['Батарея №1 (BMS)', 'Батарея №2 (BMS)'];
  const has2 = !!bms[1];
  const shortName = i => (pn[i] || `Батарея №${i + 1}`).replace(' (BMS)', '');
  return [
    {
      id: 'soc', title: 'SOC батарей', unit: '%', min: 0, max: 100,
      series: [
        { eid: card._eid('battery'), label: 'Інвертор', color: '#0a84ff' },
        { eid: card._bmsEid('zariad', 0), label: shortName(0), color: '#34c759' },
        has2 ? { eid: card._bmsEid('zariad', 1), label: shortName(1), color: '#ff9f0a' } : null,
      ].filter(Boolean),
    },
    {
      id: 'power', title: 'Потужності', unit: 'Вт',
      series: [
        { eid: card._eid('battery_power'), label: 'Батарея (+ заряд)', color: '#34c759', invert: true },
        { eid: card._eid('grid_power'), label: 'Мережа (+ імпорт)', color: '#5ac8fa' },
        { eid: card._eid('load_power'), label: 'Навантаження', color: '#0a84ff' },
      ],
    },
    {
      id: 'delta', title: 'Дельта комірок', unit: 'мВ',
      series: [
        { eid: card._bmsEid('delta_komirok', 0), label: shortName(0), color: '#34c759' },
        has2 ? { eid: card._bmsEid('delta_komirok', 1), label: shortName(1), color: '#ff9f0a' } : null,
      ].filter(Boolean),
    },
    {
      id: 'cells', title: 'Макс./мін. комірка', unit: 'В',
      thresholds: [
        { v: 3.62, label: '3.62 сторож', color: '#e0a800' },
        { v: 3.70, label: '3.70', color: '#ff9f0a' },
        { v: 3.75, label: '3.75 OVP', color: '#ff3b30' },
      ],
      series: [
        { eid: card._bmsEid('maksimalna_komirka', 0), label: `${shortName(0)} макс`, color: '#4a9eff' },
        { eid: card._bmsEid('minimalna_komirka', 0), label: `${shortName(0)} мін`, color: '#2a9d4a' },
        has2 ? { eid: card._bmsEid('maksimalna_komirka', 1), label: `${shortName(1)} макс`, color: '#ff9f0a' } : null,
        has2 ? { eid: card._bmsEid('minimalna_komirka', 1), label: `${shortName(1)} мін`, color: '#c76bff' } : null,
      ].filter(Boolean),
      balanceStrip: [
        { eid: card._bmsEid('balansuvannia_komirok', 0), label: shortName(0), color: '#34c759' },
        has2 ? { eid: card._bmsEid('balansuvannia_komirok', 1), label: shortName(1), color: '#ff9f0a' } : null,
      ].filter(Boolean),
    },
    {
      id: 'current', title: 'Струм заряду', unit: 'А',
      series: [
        { eid: card._eid('battery_max_charging_current', 'number'), label: 'Ліміт (уставка)', color: '#8b93a0', dashed: true },
        { eid: card._bmsEid('strum', 0), label: shortName(0), color: '#34c759' },
        has2 ? { eid: card._bmsEid('strum', 1), label: shortName(1), color: '#ff9f0a' } : null,
      ].filter(Boolean),
    },
  ];
}

const CSS = `
  .grange{display:flex;gap:8px;margin:2px 2px 14px;}
  .grange button{flex:1;padding:8px;border-radius:12px;border:1px solid var(--divider-color);background:var(--secondary-background-color);
    color:var(--primary-text-color);font-weight:700;font-size:.82rem;cursor:pointer;font-family:inherit;}
  .grange button.on{background:var(--acc);color:#fff;border-color:transparent;}
  .gchart{margin:0 2px 20px;}
  .gchart:last-child{margin-bottom:4px;}
  .gtitle{font-size:.74rem;font-weight:800;text-transform:uppercase;letter-spacing:.05em;color:var(--secondary-text-color);margin-bottom:6px;}
  .glegend{display:flex;flex-wrap:wrap;gap:6px 12px;margin-bottom:7px;}
  .gli{display:inline-flex;align-items:center;gap:5px;font-size:.66rem;font-weight:700;color:var(--secondary-text-color);}
  .gli i{width:10px;height:10px;border-radius:3px;display:inline-block;flex:none;}
  .gli-th i{border-radius:50%;}
  .gcanwrap{position:relative;height:150px;border-radius:12px;overflow:hidden;background:var(--secondary-background-color);touch-action:pan-y;}
  .gcv{width:100%;height:100%;display:block;cursor:crosshair;touch-action:pan-y;}
  .gtip{position:absolute;top:6px;pointer-events:none;background:var(--card-background-color);border:1px solid var(--divider-color);
    border-radius:10px;padding:6px 9px;font-size:.68rem;font-weight:600;color:var(--primary-text-color);transform:translateX(-50%);
    white-space:nowrap;box-shadow:0 4px 14px rgba(0,0,0,.3);line-height:1.55;z-index:2;}
  .gtiptime{margin-top:2px;font-size:.6rem;font-weight:700;color:var(--secondary-text-color);}
  .gloading{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:.76rem;font-weight:700;
    color:var(--secondary-text-color);background:color-mix(in srgb,var(--secondary-background-color) 70%,transparent);}
  .gloading[hidden]{display:none;}
  .gstripwrap{position:relative;height:20px;margin-top:5px;border-radius:7px;overflow:hidden;background:var(--secondary-background-color);}
  .gstrip{width:100%;height:100%;display:block;}
  .gstriplbl{position:absolute;left:6px;top:50%;transform:translateY(-50%);font-size:.58rem;font-weight:700;
    color:var(--secondary-text-color);opacity:.8;pointer-events:none;}
`;

export class DeyeGraphsTab {
  constructor(card) {
    this.card = card;
    this.range = '24h';
    this.cache = new Map();
    this.container = null;
    this.timer = null;
    this.defs = null;
    this.state = new Map();
  }

  mount(container) {
    this.container = container;
    this.defs = chartDefs(this.card);
    container.innerHTML = `<style>${CSS}</style>${this._skeleton()}`;
    this._wireRange();
    this.defs.forEach(def => this._wireChart(def));
    this._loadAndDrawAll();
    this.timer = setInterval(() => { if (this.container) this._loadAndDrawAll(); }, REFRESH_MS);
  }

  unmount() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.state.forEach(st => { if (st.ro) st.ro.disconnect(); });
    this.state.clear();
    this.container = null;
  }

  _skeleton() {
    const rangeBtns = RANGE_ORDER.map(r => `<button data-r="${r}" class="${r === this.range ? 'on' : ''}">${RANGE_LABELS[r]}</button>`).join('');
    const charts = this.defs.map(def => {
      const legend = def.series.map(s => `<span class="gli"><i style="background:${s.color}"></i>${s.label}</span>`).join('')
        + (def.thresholds ? def.thresholds.map(t => `<span class="gli gli-th"><i style="background:${t.color}"></i>${t.label}</span>`).join('') : '');
      return `
      <div class="gchart" data-id="${def.id}">
        <div class="gtitle">${def.title}</div>
        <div class="glegend">${legend}</div>
        <div class="gcanwrap"><canvas class="gcv"></canvas><div class="gtip" hidden></div><div class="gloading">Завантаження…</div></div>
        ${def.balanceStrip ? `<div class="gstripwrap"><canvas class="gstrip"></canvas><div class="gstriplbl">баланс</div></div>` : ''}
      </div>`;
    }).join('');
    return `<div class="grange">${rangeBtns}</div>${charts}`;
  }

  _wireRange() {
    this.container.querySelectorAll('.grange [data-r]').forEach(b => b.addEventListener('click', () => {
      if (b.dataset.r === this.range) return;
      this.range = b.dataset.r;
      this.container.querySelectorAll('.grange [data-r]').forEach(x => x.classList.toggle('on', x === b));
      this._loadAndDrawAll();
    }));
  }

  _wireChart(def) {
    const root = this.container.querySelector(`.gchart[data-id="${def.id}"]`);
    const canvas = root.querySelector('.gcv');
    const tip = root.querySelector('.gtip');
    const wrap = root.querySelector('.gcanwrap');
    const strip = def.balanceStrip ? root.querySelector('.gstrip') : null;
    const st = {
      canvas, ctx: canvas.getContext('2d'), tip, wrap, strip, stripCtx: strip ? strip.getContext('2d') : null,
      seriesData: [], stripData: [], start: 0, end: 0, cw: 0, ch: 0, cursor: null,
    };
    this.state.set(def.id, st);
    st.ro = new ResizeObserver(() => this._resizeChart(def.id));
    st.ro.observe(wrap);
    const onPos = e => { const r = canvas.getBoundingClientRect(); this._setCursor(def.id, e.clientX - r.left); };
    canvas.addEventListener('pointerdown', onPos);
    canvas.addEventListener('pointermove', onPos);
    canvas.addEventListener('pointerleave', () => { st.cursor = null; this._draw(def); });
  }

  // ключ кешу — eid+діапазон (БЕЗ start): start «зсувається» на кожен виклик разом з
  // Date.now(), точна рівність ніколи б не збіглася і кеш завжди мимо. 60с TTL достатньо
  // грубий, щоб дрібний зсув вікна на секунди не впливав на те, що видно на графіку.
  async _fetchCached(eid, start, end) {
    const key = `${eid}|${this.range}`;
    const c = this.cache.get(key);
    if (c && Date.now() - c.at < TTL) return c.pts;
    const pts = await this.card._fetchHistory(eid, start, end);
    this.cache.set(key, { at: Date.now(), start, pts });
    return pts;
  }

  async _fetchRawCached(eid, start, end) {
    const key = `raw:${eid}|${this.range}`;
    const c = this.cache.get(key);
    if (c && Date.now() - c.at < TTL) return c.pts;
    const raw = await fetchRawHistory(this.card._hass, eid, start, end);
    const pts = raw.map(p => ({ t: p.t, active: isActiveBalance(p.s) ? 1 : 0 }));
    this.cache.set(key, { at: Date.now(), start, pts });
    return pts;
  }

  async _loadAndDrawAll() {
    if (!this.container || !this.card._hass) return;
    const win = RANGES[this.range] || RANGES['24h'];
    const now = Date.now();
    const start = now - win, end = now;
    this.defs.forEach(def => this._toggleLoading(def.id, true));
    const jobs = [];
    this.defs.forEach(def => {
      def.series.forEach(s => jobs.push(
        this._fetchCached(s.eid, start, end).then(pts => ({ id: def.id, kind: 'series', eid: s.eid, pts }))
          .catch(err => { console.error('deye-graphs: fetch fail', s.eid, err); return { id: def.id, kind: 'series', eid: s.eid, pts: [] }; })));
      if (def.balanceStrip) def.balanceStrip.forEach(s => jobs.push(
        this._fetchRawCached(s.eid, start, end).then(pts => ({ id: def.id, kind: 'strip', eid: s.eid, pts }))
          .catch(err => { console.error('deye-graphs: fetch fail', s.eid, err); return { id: def.id, kind: 'strip', eid: s.eid, pts: [] }; })));
    });
    const results = await Promise.all(jobs);
    if (!this.container) return; // вкладку закрили/перемкнули, поки чекали на відповіді
    this.defs.forEach(def => {
      const st = this.state.get(def.id); if (!st) return;
      st.start = start; st.end = end; st.cursor = null;
      st.seriesData = def.series.map(s => {
        const r = results.find(x => x.id === def.id && x.kind === 'series' && x.eid === s.eid);
        return r ? r.pts : [];
      });
      if (def.balanceStrip) {
        st.stripData = def.balanceStrip.map(s => {
          const r = results.find(x => x.id === def.id && x.kind === 'strip' && x.eid === s.eid);
          return r ? r.pts : [];
        });
      }
      this._toggleLoading(def.id, false);
      this._resizeChart(def.id);
    });
  }

  _toggleLoading(id, on) {
    const root = this.container && this.container.querySelector(`.gchart[data-id="${id}"] .gloading`);
    if (root) root.hidden = !on;
  }

  _resizeChart(id) {
    const st = this.state.get(id); if (!st) return;
    const r = st.wrap.getBoundingClientRect();
    st.cw = Math.max(1, r.width); st.ch = Math.max(1, r.height);
    const dpr = window.devicePixelRatio || 1;
    st.canvas.width = Math.round(st.cw * dpr); st.canvas.height = Math.round(st.ch * dpr);
    st.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (st.strip) {
      const sr = st.strip.getBoundingClientRect();
      st.stripW = Math.max(1, sr.width); st.stripH = Math.max(1, sr.height);
      st.strip.width = Math.round(st.stripW * dpr); st.strip.height = Math.round(st.stripH * dpr);
      st.stripCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    const def = this.defs.find(d => d.id === id);
    if (def) this._draw(def);
  }

  _setCursor(id, xpix) {
    const st = this.state.get(id); if (!st || !st.cw) return;
    const t = st.start + (xpix / st.cw) * (st.end - st.start);
    st.cursor = { t };
    const def = this.defs.find(d => d.id === id);
    if (def) this._draw(def);
  }

  _draw(def) {
    const st = this.state.get(def.id); if (!st || !st.cw || !st.ch) return;
    const { ctx, cw, ch } = st;
    ctx.clearRect(0, 0, cw, ch);
    const css = getComputedStyle(this.card);
    const fg = css.getPropertyValue('--secondary-text-color').trim() || '#8a93a0';
    const grid = css.getPropertyValue('--divider-color').trim() || 'rgba(140,140,140,.3)';
    const padL = 42, padR = 8, padT = 8, padB = 20;
    const plotW = Math.max(1, cw - padL - padR), plotH = Math.max(1, ch - padT - padB);
    st.plot = { padL, padT, plotW, plotH };

    const allSeries = def.series.map((s, i) => ({
      def: s,
      pts: (st.seriesData[i] || []).map(p => ({ t: p.t, v: s.invert ? -p.v : p.v })),
    }));
    let min = Infinity, max = -Infinity;
    allSeries.forEach(s => s.pts.forEach(p => { if (p.v < min) min = p.v; if (p.v > max) max = p.v; }));
    if (def.thresholds) def.thresholds.forEach(t => { if (t.v < min) min = t.v; if (t.v > max) max = t.v; });
    if (def.min != null) min = Math.min(isFinite(min) ? min : def.min, def.min);
    if (def.max != null) max = Math.max(isFinite(max) ? max : def.max, def.max);
    if (!isFinite(min) || !isFinite(max)) { min = 0; max = 1; }
    if (min === max) { min -= 1; max += 1; }
    const padV = (max - min) * 0.1 || 1;
    const vMin = min - padV, vMax = max + padV;
    const x = t => padL + ((t - st.start) / (st.end - st.start || 1)) * plotW;
    const y = v => padT + plotH - ((v - vMin) / (vMax - vMin || 1)) * plotH;

    ctx.font = '9px sans-serif'; ctx.fillStyle = fg; ctx.strokeStyle = grid; ctx.lineWidth = 1;
    const rows = 4;
    for (let i = 0; i <= rows; i++) {
      const vv = vMin + (vMax - vMin) * (1 - i / rows);
      const yy = padT + (plotH * i) / rows;
      ctx.beginPath(); ctx.moveTo(padL, yy + 0.5); ctx.lineTo(padL + plotW, yy + 0.5); ctx.globalAlpha = 0.3; ctx.stroke(); ctx.globalAlpha = 1;
      ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; ctx.fillText(fmtAxisVal(vv), padL - 5, yy);
    }
    const span = st.end - st.start;
    const cols = Math.max(2, Math.min(5, Math.round(plotW / 80)));
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    for (let i = 0; i <= cols; i++) {
      const t = st.start + (span * i) / cols;
      const xx = padL + (plotW * i) / cols;
      ctx.beginPath(); ctx.moveTo(xx + 0.5, padT); ctx.lineTo(xx + 0.5, padT + plotH); ctx.globalAlpha = 0.15; ctx.stroke(); ctx.globalAlpha = 1;
      ctx.fillText(fmtAxisTime(t, span), Math.min(Math.max(xx, padL + 18), padL + plotW - 18), padT + plotH + 4);
    }

    if (def.thresholds) def.thresholds.forEach(th => {
      const yy = y(th.v);
      ctx.save(); ctx.setLineDash([5, 4]); ctx.strokeStyle = th.color; ctx.globalAlpha = 0.8; ctx.lineWidth = 1.3;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + plotW, yy); ctx.stroke(); ctx.restore();
    });

    allSeries.forEach(s => {
      const pts = s.pts; if (!pts.length) return;
      ctx.beginPath();
      if (s.def.dashed) ctx.setLineDash([4, 3]);
      pts.forEach((p, i) => { const xx = x(p.t), yy = y(p.v); if (i === 0) ctx.moveTo(xx, yy); else ctx.lineTo(xx, yy); });
      ctx.strokeStyle = s.def.color; ctx.lineWidth = 1.8; ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.stroke();
      ctx.setLineDash([]);
    });

    if (st.cursor) {
      const cx = x(st.cursor.t);
      ctx.beginPath(); ctx.moveTo(cx + 0.5, padT); ctx.lineTo(cx + 0.5, padT + plotH);
      ctx.strokeStyle = fg; ctx.globalAlpha = 0.55; ctx.lineWidth = 1; ctx.stroke(); ctx.globalAlpha = 1;
      const lines = [];
      allSeries.forEach(s => {
        const p = nearestPoint(s.pts, st.cursor.t);
        if (!p) return;
        ctx.beginPath(); ctx.arc(x(p.t), y(p.v), 3.4, 0, Math.PI * 2); ctx.fillStyle = s.def.color; ctx.fill();
        ctx.lineWidth = 1.2; ctx.strokeStyle = '#fff'; ctx.stroke();
        lines.push(`<span style="color:${s.def.color}">●</span> ${s.def.label}: <b>${fmtVal(p.v, def.unit)}</b>`);
      });
      if (st.tip) {
        st.tip.hidden = false;
        st.tip.innerHTML = `${lines.join('<br>')}<div class="gtiptime">${new Date(st.cursor.t).toLocaleString('uk-UA', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}</div>`;
        st.tip.style.left = Math.min(Math.max(cx, 52), cw - 52) + 'px';
      }
    } else if (st.tip) st.tip.hidden = true;

    if (def.balanceStrip) this._drawStrip(def, st, x);
  }

  _drawStrip(def, st, xFn) {
    if (!st.strip || !st.stripCtx || !st.stripW) return;
    const ctx = st.stripCtx, w = st.stripW, h = st.stripH;
    ctx.clearRect(0, 0, w, h);
    const padL = st.plot.padL, plotW = st.plot.plotW;
    const rows = def.balanceStrip.length || 1;
    const rowH = h / rows;
    def.balanceStrip.forEach((s, ri) => {
      const pts = st.stripData[ri] || [];
      ctx.fillStyle = 'rgba(127,127,127,.14)';
      ctx.fillRect(padL, ri * rowH + 1, plotW, rowH - 2);
      for (let i = 0; i < pts.length; i++) {
        if (!pts[i].active) continue;
        const t0 = pts[i].t;
        const t1 = i + 1 < pts.length ? pts[i + 1].t : st.end;
        const xx0 = xFn(t0), xx1 = xFn(t1);
        ctx.globalAlpha = 0.9; ctx.fillStyle = s.color;
        ctx.fillRect(xx0, ri * rowH + 1, Math.max(1, xx1 - xx0), rowH - 2);
        ctx.globalAlpha = 1;
      }
    });
  }
}
