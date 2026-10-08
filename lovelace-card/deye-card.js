/* Deye SUN-5K-SG03LP1 hybrid inverter — glass card (visionos): animated energy-flow diagram (hub / vertical) + battery SOC */
class DeyeCard extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: 'open' }); this._sSig = null; this._vSig = null; }

  setConfig(c) {
    this._config = c || {};
    this._pfx = (c && c.prefix) || 'inverter_deye';
    this._title = (c && c.title) || null;
    this._titleSize = (c && Number(c.title_size)) || null;
    this._layout = ['vertical', 'mini'].includes(c && c.layout) ? c.layout : 'hub';
    this._colors = (c && c.colors) || {};
    const sc = c && (c.scale != null ? c.scale : c.size);
    this._scale = Math.min(1.5, Math.max(0.5, Number(sc) || 1));
    // сенсори-оцінки часу (укр-рядки «X год Y хв»); показуються в mini лише при заряді/розряді
    this._t2full = (c && c.time_to_full) || 'sensor.chas_do_povnogo_zariadu';
    this._t2empty = (c && c.time_to_empty) || 'sensor.chas_do_rozriadu';
    // анімації потоку/шевронів — постійний driver кадрів; на слабких GPU-кіосках
    // (Mali-T720, планшет-передпокій) весь viewport перемальовується щокадру → лаги.
    // animate:false вимикає всі keyframes (крапки/шеврони/пульс), лишаючи статичну схему.
    this._noAnim = (c && c.animate === false);
    this.toggleAttribute('na', this._noAnim);
    // вкладка «Стан»/«Графіки» розгорнутої картки — запам'ятовується в localStorage per-prefix
    if (this._tab == null) {
      try { this._tab = localStorage.getItem(`deye-card-tab:${this._pfx}`) || 'state'; } catch (e) { this._tab = 'state'; }
    }
    // вкладка «Відключення» лишається в localStorage лише якщо юзер реально налаштував
    // outage: {source_url, group} у конфізі картки — без цього вкладки не існує
    if (this._tab === 'outage' && !this._outageEnabled()) this._tab = 'state';
    this._sSig = null; this._vSig = null; if (this._hass) this._render();
  }
  static getConfigElement() { return document.createElement('deye-card-editor'); }
  static getStubConfig() { return { prefix: 'inverter_deye', layout: 'hub' }; }
  getCardSize() { return this._layout === 'mini' ? 2 : Math.max(1, Math.round(9 * (this._scale || 1))); }
  getGridOptions() {
    if (this._layout === 'mini') return { columns: 12, min_columns: 6 };
    const s = this._scale || 1;
    return { columns: Math.max(6, Math.round(12 * s)), min_columns: 5 }; // auto-height: без rows, щоб картка не overflow-ила grid-cell і не налазила на сусідні
  }
  static get DEFAULT_COLORS() {
    return { grid: '#5ac8fa', generator: '#ff9f0a', battery: '#34c759', house: '#0a84ff', accent: '#ff8a3d' };
  }
  // версія модуля вкладки графіків (/local/deye-graphs.js?v=N) — бампати при зміні deye-graphs.js,
  // щоб браузер не тримав старий кеш динамічного import() (ha_resource.py кеш-бастить лише сам deye-card.js)
  static get GRAPHS_V() { return 3; }
  // версія модуля вкладки «Відключення» (/local/deye-outage.js?v=N) — той самий кеш-бастинг патерн
  static get OUTAGE_V() { return 5; }
  _col(k) { return (this._colors && this._colors[k]) || DeyeCard.DEFAULT_COLORS[k]; }

  connectedCallback() {
    clearInterval(this._poll);
    this._poll = setInterval(() => {
      if (!this._hass || document.visibilityState === 'hidden') return;
      const p = this._pfx;
      const ids = [
        `sensor.${p}_battery`, `sensor.${p}_battery_power`, `sensor.${p}_battery_state`,
        `sensor.${p}_grid_power`, `sensor.${p}_generator_power`, `sensor.${p}_load_power`,
        // потрібні, щоб перерахувати навантаження, коли дея занулює load_power (див. _loadW)
        `sensor.${p}_power_losses`, `sensor.${p}_output_l1_power`, `sensor.${p}_external_ct1_power`,
        `binary_sensor.${p}_grid`, `binary_sensor.${p}_generator`, `binary_sensor.${p}_connection`,
      ];
      this._hass.callService('homeassistant', 'update_entity', { entity_id: ids });
    }, 5000);
  }
  disconnectedCallback() {
    clearInterval(this._poll);
    if (this._graphsTab) { this._graphsTab.unmount(); this._graphsTab = null; }
    if (this._outageTab) { this._outageTab.unmount(); this._outageTab = null; }
  }

  // ── state helpers ──
  _eid(suf, dom) { return `${dom || 'sensor'}.${this._pfx}_${suf}`; }
  _st(suf, dom) { const e = this._hass && this._hass.states[this._eid(suf, dom)]; return e ? e.state : null; }
  _n(suf, dom) { const v = parseFloat(this._st(suf, dom)); return isFinite(v) ? v : 0; }
  _on(suf) { return this._st(suf, 'binary_sensor') === 'on'; }

  // ── ЗВІДКИ БЕРЕМО ЦИФРИ (переглянуто 2026-09-16) ───────────────────────
  // • grid_power деї («Обмін з мережею (CT)») — головне джерело вводу.
  //   Звірено балансом: grid − load − заряд_BMS − втрати ≈ 0 (до десятків Вт),
  //   заряд незалежно підтверджений BMS по RS485.
  // • external_ct1_power — CT ПЕРЕВСТАНОВЛЕНО на фазний провід 15.09.2026,
  //   тепер чесний (38 Вт залишку на вводі при розряді в дім).
  // • KWS-302WF (vvidnii_avtomat) БІЛЬШЕ НЕ ДЖЕРЕЛО: шунт занижує 20-37% на
  //   великих струмах, на холостих завищує. Еталоном був до 09.2026 —
  //   поки CT стояв неправильно.
  // ⚠️ Втрати ОКРЕМО НЕ віднімаємо — вони вже всередині output_l1_power.
  // ── дані з BMS батарей (міст jbd2mqtt → MQTT → HA, два паки на Y-шині) ──
  // Дея по Pylon-CAN віддає лише агрегати майстра; покомірки, SOH і другий
  // пак приходять звідси.
  _bmsPfx() {
    if (this._config.bms_prefixes) return this._config.bms_prefixes;
    if (this._config.bms_prefix) return [this._config.bms_prefix];
    return ['batareia_deye_bms', 'batareia_deye_no2_bms'];
  }
  _bms(suf, pi = 0) {
    const pfx = this._bmsPfx()[pi];
    if (!pfx) return null;
    const e = this._hass && this._hass.states[`sensor.${pfx}_${suf}`];
    if (!e || e.state === 'unavailable' || e.state === 'unknown') return null;
    const v = parseFloat(e.state);
    return isFinite(v) ? v : null;
  }
  // entity_id для BMS-сенсора пака pi (для data-hist — тап-на-графік історії)
  _bmsEid(suf, pi = 0) { const pfx = this._bmsPfx()[pi]; return pfx ? `sensor.${pfx}_${suf}` : ''; }
  // data-hist атрибут: тап по значенню відкриває повноекранний графік історії цієї сутності
  _h(eid) { return eid ? ` data-hist="${eid}"` : ''; }

  _cells(pi = 0) {
    const out = [];
    for (let i = 1; i <= 16; i++) {
      const v = this._bms('komirka_' + i, pi);
      if (v === null) return [];        // BMS-міст лежить — секцію просто не малюємо
      out.push(v);
    }
    return out;
  }

  // номери комірок, що ПРЯМО ЗАРАЗ активно балансуються (jbd2mqtt публікує
  // sensor...balansuvannia_komirok: «—» коли нікого, інакше номери через кому «7, 12»)
  _balCells(pi = 0) {
    const st = this._estate(this._bmsEid('balansuvannia_komirok', pi));
    const out = new Set();
    if (!st || st === 'unavailable' || st === 'unknown' || st === '—' || st === '-') return out;
    st.split(',').forEach(s => { const n = parseInt(s.trim(), 10); if (isFinite(n)) out.add(n); });
    return out;
  }

  // Ввід з мережі — grid_power деї (звірений балансом, див. блок вище).
  // grid_meter_entity у конфізі — ручний оверрайд на зовнішній лічильник.
  _gridW() {
    const meter = this._config.grid_meter_entity;
    if (meter) {
      const v = parseFloat(this._hass?.states?.[meter]?.state);
      if (isFinite(v)) return v;
    }
    return this._n('grid_power');
  }

  _loadW(bp) {
    const gs = this._st('grid_power');
    if (gs === null || gs === 'unavailable' || gs === 'unknown') return this._n('load_power');
    const est = this._gridW() + this._n('output_l1_power');   // втрати вже в output
    return est > 0 ? est : 0;
  }


  // спільні обчислення станів/напрямів для рендера, патча і сигнатур
  _calc() {
    const soc = this._n('battery');
    const bp = this._n('battery_power');
    const gp = this._gridW();
    const genp = this._n('generator_power');
    const lp = this._loadW(bp);
    const TH = 25;
    const bst = this._st('battery_state');
    const battCharge = bst ? bst === 'charging' : bp < -TH;
    const battDischarge = bst ? bst === 'discharging' : bp > TH;
    const gridImport = gp > TH, gridExport = gp < -TH;
    const gridOn = this._st('grid', 'binary_sensor') == null ? true : this._on('grid');
    const genOn = this._on('generator') || genp > 15;
    const online = this._st('connection', 'binary_sensor') == null ? true : this._on('connection');
    const devOk = (this._st('device_state') || 'Normal') === 'Normal' && (this._st('device_alarm') || 'OK') === 'OK';
    const socCol = this._socColor(soc);
    const tEid = battCharge ? this._t2full : battDischarge ? this._t2empty : null;
    const tSt = tEid ? this._estate(tEid) : null;
    const timeShown = !!(tSt && /\d/.test(tSt));
    return { soc, bp, gp, genp, lp, bst, battCharge, battDischarge, gridImport, gridExport, gridOn, genOn, online, devOk, socCol, tEid, tSt, timeShown };
  }

  // значення для точкового оновлення [data-u]-вузлів без перебудови DOM
  _updMap(c) {
    const u = {};
    if (this._layout === 'mini' && !this._expanded) {
      u.m_soc = String(Math.round(c.soc));
      u.m_bstate = c.battCharge ? 'заряд ' + this._fmtW(Math.abs(c.bp)) : c.battDischarge ? 'розряд ' + this._fmtW(c.bp) : 'спокій';
      u.m_house = this._fmtW(c.lp);
      u.m_grid = c.gridOn ? this._fmtW(Math.abs(c.gp)) : 'НЕМА';
      if (c.genOn) u.m_gen = this._fmtW(c.genp);
      if (c.timeShown) u.m_time = c.tSt;
      this._bmsPfx().forEach((pfx, pi) => {
        const s = this._bms('zariad', pi);
        if (s === null) return;
        const v = this._bms('napruga_paketa', pi), a = this._bms('strum', pi);
        u[`mb${pi}_soc`] = Math.round(s) + '%';
        u[`mb${pi}_v`] = v === null ? '' : v.toFixed(2).replace('.', ',') + ' В';
        u[`mb${pi}_a`] = a !== null && Math.abs(a) >= 0.5 ? (a > 0 ? '+' : '−') + Math.abs(a).toFixed(1).replace('.', ',') + ' А' : '';
      });
    } else {
      u.soc_txt = String(Math.round(c.soc));
      u.grid_v = c.gridImport ? '↓ ' + this._fmtW(Math.abs(c.gp)) : c.gridExport ? '↑ ' + this._fmtW(Math.abs(c.gp)) : (c.gridOn ? '0 Вт' : 'нема');
      u.gen_v = c.genOn ? this._fmtW(c.genp) : 'вимк';
      u.house_v = this._fmtW(c.lp);
      u.pill_batt_l = `Батарея · ${Math.round(c.soc)}%`;
      u.pill_batt_v = (c.battCharge ? '+ ' : c.battDischarge ? '− ' : '') + this._fmtW(Math.abs(c.bp));
      u.pill_grid_v = this._fmtW(Math.abs(c.gp));
      u.pill_house_v = this._fmtW(c.lp);
      if (c.genOn) u.pill_gen_v = this._fmtW(c.genp);
      u.d_volt = this._n('battery_voltage').toFixed(1).replace('.', ',') + ' В';
      u.d_curr = this._n('battery_current').toFixed(1).replace('.', ',') + ' А';
      u.d_btemp = this._n('battery_temperature').toFixed(1).replace('.', ',') + ' °C';
      u.d_cap = this._fmtKwh(this._n('battery_capacity'));
      u.d_soc = Math.round(c.soc) + ' %';
      u.i_temp = this._n('temperature').toFixed(1).replace('.', ',') + ' °C';
      u.i_freq = this._n('grid_frequency').toFixed(2).replace('.', ',') + ' Гц';
      u.t_charge = this._fmtKwh(this._n('today_battery_charge'));
      u.t_dis = this._fmtKwh(this._n('today_battery_discharge'));
      u.t_imp = this._fmtKwh(this._n('today_energy_import'));
      u.t_exp = this._fmtKwh(this._n('today_energy_export'));
      u.t_load = this._fmtKwh(this._n('today_load_consumption'));
      this._bmsPfx().forEach((pfx, pi) => {
        const s = this._bms('zariad', pi);
        if (s === null) return;
        const v = this._bms('napruga_paketa', pi), a = this._bms('strum', pi);
        const d = this._bms('delta_komirok', pi), t = this._bms('temperatura_1', pi);
        u[`b${pi}_soc`] = Math.round(s) + ' %';
        u[`b${pi}_v`] = v === null ? '—' : v.toFixed(2).replace('.', ',') + ' В';
        u[`b${pi}_a`] = a === null ? '—' : (a > 0 ? '+' : '') + a.toFixed(1).replace('.', ',') + ' А';
        u[`b${pi}_t`] = t === null ? '—' : t.toFixed(1).replace('.', ',') + ' °C';
        u[`b${pi}_d`] = d === null ? '—' : d.toFixed(0) + ' мВ';
        u[`b${pi}_st`] = a !== null && a > 0.5 ? `заряд ${this._fmtW(Math.abs((v ?? 0) * a))}`
          : a !== null && a < -0.5 ? `розряд ${this._fmtW(Math.abs((v ?? 0) * a))}` : 'спокій';
      });
      const m = this._month();
      if (m.mdk !== null || m.mnk !== null) {
        u.mo_dk = m.mdk === null ? '—' : this._fmtKwh(m.mdk);
        u.mo_nk = m.mnk === null ? '—' : this._fmtKwh(m.mnk);
        u.mo_tk = (m.mdk === null || m.mnk === null) ? '—' : this._fmtKwh(m.mdk + m.mnk);
        u.mo_du = this._fmtUah(m.mdu);
        u.mo_nu = this._fmtUah(m.mnu);
        u.mo_tu = this._fmtUah(m.mtu);
        u.mo_total = m.mtu === null ? '' : this._fmtUah(m.mtu);
      }
    }
    return u;
  }

  // точковий патч: текст + рівень батареї + SOC-бар; DOM живе, анімації НЕ рестартують
  _patch(upd, c) {
    const r = this.shadowRoot;
    for (const k in upd) {
      const el = r.querySelector(`[data-u="${k}"]`);
      if (el && el.textContent !== upd[k]) el.textContent = upd[k];
    }
    const pct = Math.max(0, Math.min(100, c.soc));
    const bar = r.querySelector('.msocbar'); if (bar) bar.style.width = pct + '%';
    const bf = r.querySelector('.bfill');
    if (bf && bf.dataset.ih) {
      const y0 = parseFloat(bf.dataset.y0), ih = parseFloat(bf.dataset.ih), fh = ih * pct / 100;
      bf.setAttribute('y', (y0 + ih - fh).toFixed(1)); bf.setAttribute('height', fh.toFixed(1));
    }
  }

  set hass(h) {
    this._hass = h;
    const c = this._calc();
    // структурна сигнатура: перебудова DOM ТІЛЬКИ коли міняється схема (напрями/стани/режими)
    // деталі «Стан»-вкладки (заряд/розряд, живий glow балансування комірок тощо) НЕ мають
    // форсувати перебудову, поки юзер на вкладці «Графіки» — balансуючі комірки міняються
    // кожні ~30с, і без цього гварда графіки постійно ремонтувались би (втрата обраного
    // діапазону + зайвий рефетч великих 7д-масивів на кожен тік hass)
    const stateSig = (this._tab === 'graphs' || this._tab === 'outage') ? '' : [
      c.battCharge, c.battDischarge, c.gridOn, c.gridImport, c.gridExport, c.genOn, c.devOk, c.online, c.socCol, c.timeShown, c.bst,
      this._st('work_mode', 'select'), this._st('energy_pattern', 'select'),
      this._st('device_state'), this._st('device_alarm'), this._mode(),
      // банер реального критичного режиму: причини авто-екстреного дня + фактичні полички/струм
      this._autoReasons().join(','), this._progSoc().join(','), this._maxChgA(),
      // живість і напрям BMS-паків: поява/зникнення чи зміна заряд/розряд = перебудова
      this._bmsPfx().map((p, i) => {
        if (this._bms('zariad', i) === null) return 'x';
        const a = this._bms('strum', i);
        return a !== null && a > 0.5 ? 'c' : a !== null && a < -0.5 ? 'd' : 'i';
      }).join(''),
      // які комірки зараз балансуються — зміна списку теж перебудовує (живий glow у DOM)
      this._bmsPfx().map((p, i) => Array.from(this._balCells(i)).sort((a, b) => a - b).join(',')).join('|'),
      // поява/зникнення місячної секції
      (() => { const m = this._month(); return m.mdk !== null || m.mnk !== null ? 1 : 0; })(),
    ].join('|');
    const sSig = [this._layout, this._expanded ? 1 : 0, this._tab, stateSig].join('|');
    const upd = this._updMap(c);
    const vSig = JSON.stringify(upd) + '|' + Math.round(c.soc);
    if (sSig !== this._sSig) {
      this._sSig = sSig; this._vSig = vSig;
      if (!this._popupOpen) this._render();
    } else if (vSig !== this._vSig) {
      this._vSig = vSig;
      if (!this._popupOpen) this._patch(upd, c);
    }
  }

  // місячні зони: кВт·год з utility_meter, грн з template-сенсорів (пакет deye_energy.yaml)
  _month() {
    const me = this._config.month_entities || {
      day_kwh: 'sensor.spozhito_zony_den', night_kwh: 'sensor.spozhito_zony_nich',
      day_uah: 'sensor.deye_cost_day', night_uah: 'sensor.deye_cost_night', total_uah: 'sensor.deye_cost_total',
    };
    const n = eid => { const v = parseFloat(this._estate(eid)); return isFinite(v) ? v : null; };
    return { mdk: n(me.day_kwh), mnk: n(me.night_kwh), mdu: n(me.day_uah), mnu: n(me.night_uah), mtu: n(me.total_uah) };
  }
  _fmtUah(v) { return v === null ? '—' : v.toFixed(0) + ' грн'; }

  _ic(n, c) { return `<ha-icon icon="mdi:${n}" class="${c || 'hi'}"></ha-icon>`; }
  // тонкий груп-роздільник між великими блоками розгорнутого вигляду (не в mini)
  _sep(icon, label) { return `<div class="gsep"><span class="gsepline"></span><span class="gsepl">${this._ic(icon, 'gsi')}${label}</span><span class="gsepline"></span></div>`; }
  _fmtW(w) { const a = Math.abs(w); return a >= 1000 ? (w / 1000).toFixed(2).replace('.', ',') + ' кВт' : Math.round(w) + ' Вт'; }
  _fmtKwh(v) { return (Math.round(v * 10) / 10).toString().replace('.', ',') + ' кВт·год'; }
  _socColor(s) { return s >= 50 ? '#34c759' : s >= 20 ? '#ffc107' : '#ff3b30'; }
  _moreInfo(eid) { this.dispatchEvent(new CustomEvent('hass-more-info', { detail: { entityId: eid }, bubbles: true, composed: true })); }
  _dur(p) { const k = Math.min(1, Math.abs(p) / 4000); return (3.2 - k * 2.4).toFixed(2); }

  // node positions per layout (viewBox 0 0 300 300); inverter at centre
  _geo() {
    const cx = 150, cy = 150, hw = 30, hh = 46, R = 28;
    const nodes = this._layout === 'vertical'
      ? { grid: [54, 52], gen: [246, 52], batt: [256, 168], house: [150, 262] }
      : { grid: [46, 148], gen: [150, 40], batt: [150, 256], house: [254, 148] };
    return { cx, cy, hw, hh, R, nodes };
  }
  // edge of inverter rect along the ray centre→node
  _invEdge(g, tx, ty) {
    const dx = tx - g.cx, dy = ty - g.cy;
    const sx = dx ? g.hw / Math.abs(dx) : 1e9, sy = dy ? g.hh / Math.abs(dy) : 1e9;
    const t = Math.min(sx, sy);
    return [g.cx + dx * t, g.cy + dy * t];
  }
  _nodeEdge(g, tx, ty, r) {
    const dx = tx - g.cx, dy = ty - g.cy, len = Math.hypot(dx, dy) || 1;
    return [tx - dx / len * (r || g.R), ty - dy / len * (r || g.R)];
  }

  _particles(d, color, dur, n) {
    n = n || 3; let out = '';
    for (let i = 0; i < n; i++)
      out += `<circle r="3.3" fill="${color}" class="pdot"><animateMotion dur="${dur}s" begin="-${(dur * i / n).toFixed(2)}s" repeatCount="indefinite" path="${d}"/></circle>`;
    return out;
  }
  // one flow line + animated particles. dir>0 = toward inverter (node→inv), dir<0 = inverter→node, 0 = idle
  _flow(g, key, dir, power, color) {
    const [tx, ty] = g.nodes[key];
    const r = key === 'batt' ? 30 : g.R;
    const a = this._invEdge(g, tx, ty);              // inverter side
    const b = this._nodeEdge(g, tx, ty, r);          // node side
    const lineD = `M${a[0].toFixed(1)},${a[1].toFixed(1)}L${b[0].toFixed(1)},${b[1].toFixed(1)}`;
    const active = dir !== 0 && Math.abs(power) > 15;
    const base = `<path d="${lineD}" fill="none" stroke="${active ? color : 'var(--divider-color)'}" stroke-width="${active ? 3 : 2}" stroke-linecap="round" opacity="${active ? 0.5 : 0.6}"/>`;
    if (!active) return base;
    const dotD = dir > 0 ? `M${b[0].toFixed(1)},${b[1].toFixed(1)}L${a[0].toFixed(1)},${a[1].toFixed(1)}`
      : `M${a[0].toFixed(1)},${a[1].toFixed(1)}L${b[0].toFixed(1)},${b[1].toFixed(1)}`;
    return base + this._particles(dotD, color, this._dur(power), Math.abs(power) > 1500 ? 4 : 3);
  }

  // realistic-ish Deye SUN-5K body
  _inverter(g, online, devOk) {
    const x = g.cx - g.hw, y = g.cy - g.hh, w = g.hw * 2, h = g.hh * 2;
    const led = (i, on, c) => `<circle cx="${x + 12 + i * 9}" cy="${y + h - 13}" r="2.5" fill="${on ? c : '#3a4150'}" ${on ? `class="ledon"` : ''}/>`;
    return `
      <g class="inv ${online ? '' : 'dim'}" data-node="inv" style="cursor:pointer">
        <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="9" fill="url(#invbody)" stroke="#9aa3af" stroke-width="1.2"/>
        <rect x="${x + 6}" y="${y + 7}" width="${w - 12}" height="22" rx="4" fill="#0d1117" stroke="#2b313c" stroke-width="1"/>
        <text x="${g.cx}" y="${y + 21}" text-anchor="middle" font-size="9" font-weight="700" fill="${devOk ? '#36d07a' : '#ff6b6b'}" font-family="monospace">${devOk ? '5.0kW' : 'ERR'}</text>
        <text x="${g.cx}" y="${y + 40}" text-anchor="middle" font-size="8.5" font-weight="800" fill="#5b6472" letter-spacing="1">DEYE</text>
        <g>${led(0, online, '#36d07a')}${led(1, devOk, '#36d07a')}${led(2, !devOk, '#ffb020')}</g>
        ${[0, 1, 2, 3, 4].map(i => `<rect x="${x + 8}" y="${y + h - 30 + i * 3.2}" width="${w - 16}" height="1.4" rx="0.7" fill="#aab2bd" opacity=".7"/>`).join('')}
      </g>`;
  }

  // battery glyph filled by SOC at node position
  _battery(g, soc, charging) {
    const [bx, by] = g.nodes.batt;
    const W = 40, H = 56, x = bx - W / 2, y = by - H / 2;
    const col = this._socColor(soc);
    const innerH = H - 8, fillH = innerH * Math.max(0, Math.min(100, soc)) / 100;
    return `
      <g class="batt" data-node="batt" style="cursor:pointer">
        <rect x="${bx - 7}" y="${y - 5}" width="14" height="6" rx="2" fill="#8b93a0"/>
        <rect x="${x}" y="${y}" width="${W}" height="${H}" rx="7" fill="var(--card-background-color)" stroke="${col}" stroke-width="2.2"/>
        <clipPath id="bclip"><rect x="${x + 4}" y="${y + 4}" width="${W - 8}" height="${innerH}" rx="3"/></clipPath>
        <g clip-path="url(#bclip)">
          <rect x="${x + 4}" y="${y + 4 + (innerH - fillH)}" width="${W - 8}" height="${fillH}" fill="${col}" class="bfill" data-y0="${y + 4}" data-ih="${innerH}"/>
        </g>
        <text x="${bx}" y="${by + 4}" text-anchor="middle" font-size="13" font-weight="800" fill="var(--primary-text-color)" data-u="soc_txt"${this._h(this._eid('battery'))}>${Math.round(soc)}</text>
        <text x="${bx}" y="${by + 15}" text-anchor="middle" font-size="7" font-weight="700" fill="var(--secondary-text-color)">%</text>
        ${charging ? `<g class="boltw"><path d="M${bx + 1},${y + 8} l-7,11 h5 l-3,9 9,-13 h-5 z" fill="#ffff" stroke="${col}" stroke-width="1.4" stroke-linejoin="round"/></g>` : ''}
      </g>`;
  }

  // labelled node (grid / generator / house); підписи над колом СТЕКОМ вище кола — не налазять
  _node(g, key, icon, label, value, color, active, eid) {
    const [nx, ny] = g.nodes[key];
    const below = ny >= 150;
    const ly = below ? ny + g.R + 12 : ny - g.R - 18;
    const vy = ly + 11;
    return `
      <g class="node ${active ? 'act' : ''}" data-node="${key}" style="cursor:pointer">
        <circle cx="${nx}" cy="${ny}" r="${g.R}" fill="color-mix(in srgb,${color} 14%,var(--card-background-color))" stroke="${color}" stroke-width="${active ? 2.4 : 1.6}"/>
        ${this._svgIcon(icon, nx, ny, color)}
        <text x="${nx}" y="${ly}" text-anchor="middle" font-size="9" font-weight="700" fill="var(--secondary-text-color)">${label}</text>
        <text x="${nx}" y="${vy}" text-anchor="middle" font-size="10" font-weight="800" fill="${active ? color : 'var(--secondary-text-color)'}" data-u="${key}_v"${this._h(eid)}>${value}</text>
      </g>`;
  }
  // ha-icon doesn't render inside <svg>; use a foreignObject so mdi shows on the node
  _svgIcon(icon, x, y, color) {
    return `<foreignObject x="${x - 12}" y="${y - 14}" width="24" height="24" style="overflow:visible;pointer-events:none">
      <ha-icon icon="mdi:${icon}" style="--mdc-icon-size:22px;width:22px;height:22px;color:${color}"></ha-icon></foreignObject>`;
  }

  // ── mini layout: один рядок, тільки головні показники ──
  _renderMini() {
    const online = this._st('connection', 'binary_sensor') == null ? true : this._on('connection');
    const soc = this._n('battery');
    const bp = this._n('battery_power');           // − заряд / + розряд
    const gp = this._gridW();              // + імпорт / − експорт
    const genp = this._n('generator_power');
    const lp = this._loadW(bp);
    const genOn = this._on('generator') || genp > 15;
    const gridOn = this._st('grid', 'binary_sensor') == null ? true : this._on('grid');
    const TH = 25;
    const bst = this._st('battery_state'); // напрям — від інвертора, пороги лише фолбек
    const battCharge = bst ? bst === 'charging' : bp < -TH;
    const battDischarge = bst ? bst === 'discharging' : bp > TH;
    const gridImport = gp > TH, gridExport = gp < -TH;
    const cGrid = this._col('grid'), cGen = this._col('generator'), cHouse = this._col('house'), cAcc = this._col('accent');
    const socCol = this._socColor(soc);
    const devOk = (this._st('device_state') || 'Normal') === 'Normal' && (this._st('device_alarm') || 'OK') === 'OK';

    const battCol = battCharge ? '#34c759' : battDischarge ? '#ff9f0a' : 'var(--secondary-text-color)';
    const battLbl = battCharge ? 'заряд ' + this._fmtW(Math.abs(bp)) : battDischarge ? 'розряд ' + this._fmtW(bp) : 'спокій';
    const gridLbl = gridOn ? (gridImport ? 'імпорт' : gridExport ? 'експорт' : 'мережа') : 'мережа';
    const gridVal = gridOn ? this._fmtW(Math.abs(gp)) : 'НЕМА';
    const gridCol = gridOn ? (gridImport || gridExport ? cGrid : 'var(--primary-text-color)') : '#ff3b30';

    // маленька батарейка з рівнем SOC; при заряді/розряді — анімовані шеврони вгору/вниз
    const W = 24, H = 38, innerH = H - 6, fillH = innerH * Math.max(0, Math.min(100, soc)) / 100;
    const chevs = battCharge
      ? `<path class="chev up" d="M10,30 l5,-5 l5,5"/><path class="chev up" style="animation-delay:-.75s" d="M10,30 l5,-5 l5,5"/>`
      : battDischarge
        ? `<path class="chev dn" d="M10,18 l5,5 l5,-5"/><path class="chev dn" style="animation-delay:-.75s" d="M10,18 l5,5 l5,-5"/>`
        : '';
    const glyph = `<svg class="mbsvg" viewBox="0 0 ${W + 6} ${H + 8}">
        <rect x="${3 + W / 2 - 5}" y="1" width="10" height="4.5" rx="1.5" fill="#8b93a0"/>
        <rect x="3" y="5.5" width="${W}" height="${H}" rx="5" fill="var(--secondary-background-color)" stroke="${socCol}" stroke-width="1.8"/>
        <clipPath id="mbclip"><rect x="6" y="8.5" width="${W - 6}" height="${innerH}" rx="2.5"/></clipPath>
        <g clip-path="url(#mbclip)">
          <rect x="6" y="${8.5 + (innerH - fillH)}" width="${W - 6}" height="${fillH}" fill="${socCol}" class="bfill" data-y0="8.5" data-ih="${innerH}"/>
          ${chevs}
        </g>
        ${battCharge ? `<g class="boltw"><path d="M${3 + W / 2 + 1},13 l-6,10 h4.4 l-2.6,8 8,-11.6 h-4.4 z" fill="#fff" stroke="${socCol}" stroke-width="1.2" stroke-linejoin="round"/></g>` : ''}
      </svg>`;

    const stat = (node, icon, lbl, val, col) => `
      <div class="mstat" data-node="${node}">
        <span class="msv" style="color:${col}" data-u="m_${node}">${val}</span>
        <span class="msl">${this._ic(icon, 'mi')}${lbl}</span>
      </div>`;

    // анімовані лінії-потоки міні-схеми: крапки біжать у напрямку руху енергії
    const flow = (active, rev, color, dead) => `
      <div class="mflow ${rev ? 'rev' : ''} ${dead ? 'dead' : ''}" style="--fc:${color}">
        ${active && !dead ? '<i></i><i style="animation-delay:-.8s"></i>' : ''}
      </div>`;
    const battActive = battCharge || battDischarge;
    // батарея↔будинок: розряд → крапки до будинку; заряд → крапки в батарею
    const flow1 = flow(battActive, battCharge, battCharge ? '#34c759' : battDischarge ? '#ff9f0a' : 'var(--divider-color)', false);
    // будинок↔мережа: імпорт → крапки до будинку; експорт → у мережу; блекаут → червоний пунктир
    const flow2 = flow(gridImport || gridExport, gridImport, gridExport ? cHouse : cGrid, !gridOn);

    // рядок оцінки часу — ТІЛЬКИ під час заряду/розряду і тільки якщо сенсор віддав число
    const tEid = battCharge ? this._t2full : battDischarge ? this._t2empty : null;
    const tSt = tEid ? this._estate(tEid) : null;
    const timeRow = tSt && /\d/.test(tSt) ? `
      <div class="mtime" data-eid="${tEid}" style="color:${battCharge ? '#34c759' : '#ff9f0a'}">
        ${this._ic(battCharge ? 'battery-clock' : 'clock-end', 'mi')}
        <span class="mtl">${battCharge ? 'до повного заряду' : 'заряду лишилось'}</span>
        <b data-u="m_time">${tSt}</b>
      </div>` : '';

    // рядок обох BMS-паків: SOC + напруга (+струм коли тече) — лише якщо є 2-й пак
    const packs = this._bmsPfx()
      .map((pfx, pi) => ({ pi, soc: this._bms('zariad', pi), v: this._bms('napruga_paketa', pi), a: this._bms('strum', pi) }))
      .filter(p => p.soc !== null);
    const bmsRow = packs.length >= 2 ? `
      <div class="mbms">${packs.map(p => `
        <span class="mbp">${this._ic(p.a !== null && p.a > 0.5 ? 'battery-charging' : p.a !== null && p.a < -0.5 ? 'battery-minus' : 'battery-outline', 'mi')}№${p.pi + 1}
          <b style="color:${this._socColor(p.soc)}" data-u="mb${p.pi}_soc">${Math.round(p.soc)}%</b>
          <span data-u="mb${p.pi}_v">${p.v === null ? '' : p.v.toFixed(2).replace('.', ',') + ' В'}</span>
          <span data-u="mb${p.pi}_a">${p.a !== null && Math.abs(p.a) >= 0.5 ? (p.a > 0 ? '+' : '−') + Math.abs(p.a).toFixed(1).replace('.', ',') + ' А' : ''}</span>
        </span>`).join('')}
      </div>` : '';

    const anim = this._anim ? 'pop' : ''; this._anim = false;
    this.shadowRoot.innerHTML = `${this._css()}
      <ha-card class="mini ${online ? '' : 'moff'} ${anim}" style="--acc:${cAcc};zoom:${this._scale || 1};">
        <div class="mrow">
          <div class="mbatt" data-node="batt" title="Акумулятор">
            ${glyph}
            <div class="mpct">
              <b style="color:${socCol}"><span data-u="m_soc">${Math.round(soc)}</span><i>%</i></b>
              <span class="msl" style="color:${battCol}" data-u="m_bstate">${battLbl}</span>
            </div>
          </div>
          ${flow1}
          ${stat('house', 'home-lightning-bolt', 'будинок', this._fmtW(lp), cHouse)}
          ${flow2}
          ${stat('grid', gridOn ? 'transmission-tower' : 'transmission-tower-off', gridLbl, gridVal, gridCol)}
          ${genOn ? stat('gen', 'engine', 'генератор', this._fmtW(genp), cGen) : ''}
          ${(() => { const cs = this._critState(); return `<span class="mcrit mcrit-${cs.key}" title="${cs.label}${cs.warn ? ' · ' + this._shelvesWarnTxt() : ''}">${cs.icon}</span>`; })()}
          ${devOk ? '' : `<span class="malert" data-node="inv" title="${this._st('device_state') || ''} / ${this._st('device_alarm') || ''}">${this._ic('alert-circle', 'mi')}</span>`}
        </div>
        ${timeRow}
        ${bmsRow}
        <div class="msocbar" style="width:${Math.max(0, Math.min(100, soc))}%;background:${socCol};color:${socCol}"></div>
      </ha-card>`;
    // у mini один жест: тап будь-де по картці розгортає повну версію
    this.shadowRoot.querySelector('ha-card').addEventListener('click', () => {
      this._expanded = true; this._anim = true; this._render();
    });
  }

  _render() {
    const h = this._hass; if (!h) return;
    if (this._layout === 'mini' && !this._expanded) {
      if (this._graphsTab) { this._graphsTab.unmount(); this._graphsTab = null; }
      if (this._outageTab) { this._outageTab.unmount(); this._outageTab = null; }
      return this._renderMini();
    }
    if (this._tab !== 'graphs' && this._graphsTab) { this._graphsTab.unmount(); this._graphsTab = null; }
    if (this._tab !== 'outage' && this._outageTab) { this._outageTab.unmount(); this._outageTab = null; }
    const p = this._pfx;
    const online = this._st('connection', 'binary_sensor') == null ? true : this._on('connection');
    const title = this._title || 'Інвертор Deye';

    // readings
    const soc = this._n('battery');
    const bp = this._n('battery_power');           // − заряд / + розряд
    const gp = this._gridW();              // + імпорт / − експорт
    const genp = this._n('generator_power');
    const lp = this._loadW(bp);
    const genOn = this._on('generator') || genp > 15;
    const gridOn = this._st('grid', 'binary_sensor') == null ? true : this._on('grid');

    // напрям батареї — від сенсора інвертора (він ігнорує мікро-дрейф ±20Вт); пороги лише фолбек
    const TH = 25;
    const bst = this._st('battery_state');
    const battCharge = bst ? bst === 'charging' : bp < -TH;
    const battDischarge = bst ? bst === 'discharging' : bp > TH;
    const gridImport = gp > TH, gridExport = gp < -TH;

    const g = this._geo();
    const cGrid = this._col('grid'), cGen = this._col('generator'), cHouse = this._col('house'), cBatt = this._col('battery'), cAcc = this._col('accent');
    const battCol = battCharge ? '#34c759' : battDischarge ? '#ff9f0a' : cBatt;

    // flows: dir>0 → into inverter, dir<0 → out of inverter
    const flows =
      this._flow(g, 'grid', gridImport ? 1 : gridExport ? -1 : 0, gp, gridExport ? cHouse : cGrid) +
      this._flow(g, 'gen', genOn ? 1 : 0, genp, cGen) +
      this._flow(g, 'batt', battDischarge ? 1 : battCharge ? -1 : 0, bp, battCol) +
      this._flow(g, 'house', lp > TH ? -1 : 0, lp, cHouse);

    const devOk = (this._st('device_state') || 'Normal') === 'Normal' && (this._st('device_alarm') || 'OK') === 'OK';

    // «Критичний режим» — банер РЕАЛЬНОГО стану (блекаут/генератор/ручний форс/авто-екстрений
    // день/еко/балансування) + чотирипозиційний селектор Еко|Авто|Критичний|Балансування
    // (input_select.deye_mode, замінює колишній тумблер input_boolean.deye_winter_ready).
    const cs = this._critState();
    const mode = cs.mode;
    // чотирипозиційний селектор — на вузьких картках (<400px, @container dc) перемикається
    // на сітку 2×2 зі скороченими підписами (.mlf/.mls, CSS-перемикач), щоб не ламати верстку
    const modeSel = `
      <div class="modesel">
        <button class="mseg ${mode === 'Еко' ? 'on' : ''}" data-mode="Еко">🌿 <span class="mlf">Еко</span><span class="mls">Еко</span></button>
        <button class="mseg ${mode === 'Авто' ? 'on' : ''}" data-mode="Авто">🤖 <span class="mlf">Авто</span><span class="mls">Авто</span></button>
        <button class="mseg ${mode === 'Критичний' ? 'on' : ''}" data-mode="Критичний">🚨 <span class="mlf">Критичний</span><span class="mls">Крит</span></button>
        <button class="mseg ${mode === 'Балансування' ? 'on' : ''}" data-mode="Балансування">⚖️ <span class="mlf">Балансування</span><span class="mls">Баланс</span></button>
      </div>`;
    const critBar = `
      <div class="critbar cb-${cs.key}" title="Режим роботи — input_select.deye_mode">
        <span class="crithead">
          <span class="crit-ic">${cs.icon}</span>
          <span class="critt">
            <b>${cs.label}</b>
            <span class="crits">${this._modeExplain()}</span>
            <span class="crits">полички ${this._shelvesTxt()}${cs.warn ? ' <span class="critw">' + this._shelvesWarnTxt() + '</span>' : ''}</span>
          </span>
        </span>
        ${modeSel}
      </div>`;

    const gridVal = gridImport ? '↓ ' + this._fmtW(Math.abs(gp)) : gridExport ? '↑ ' + this._fmtW(Math.abs(gp)) : (gridOn ? '0 Вт' : 'нема');
    const genVal = genOn ? this._fmtW(genp) : 'вимк';
    const houseVal = this._fmtW(lp);

    const diagram = `
      <svg class="diag" viewBox="0 -18 300 338">
        <defs>
          <linearGradient id="invbody" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#f3f5f8"/><stop offset="0.5" stop-color="#dde2e9"/><stop offset="1" stop-color="#c4cbd5"/>
          </linearGradient>
        </defs>
        ${flows}
        ${this._node(g, 'grid', gridOn ? 'transmission-tower' : 'transmission-tower-off', 'Мережа', gridVal, cGrid, gridImport || gridExport, this._eid('grid_power'))}
        ${this._node(g, 'gen', 'engine', 'Генератор', genVal, cGen, genOn, this._eid('generator_power'))}
        ${this._node(g, 'house', 'home', 'Будинок', houseVal, cHouse, lp > TH, this._eid('load_power'))}
        ${this._battery(g, soc, battCharge)}
        ${this._inverter(g, online, devOk)}
      </svg>`;

    // flow stat pills
    const battStateRaw = this._st('battery_state') || (battCharge ? 'charging' : battDischarge ? 'discharging' : 'idle');
    const battStateTxt = { idle: 'Спокій', charging: 'Заряд', discharging: 'Розряд', standby: 'Очікування' }[battStateRaw] || battStateRaw;
    const battSign = battCharge ? '+ ' : battDischarge ? '− ' : '';
    const pills = `
      <div class="pills">
        <div class="pill" data-node="batt" style="--c:${battCol}">${this._ic('battery-charging-high', 'pi')}<div class="pt"><span class="pl" data-u="pill_batt_l">Батарея · ${Math.round(soc)}%</span><span class="pv" data-u="pill_batt_v"${this._h(this._eid('battery_power'))}>${battSign}${this._fmtW(Math.abs(bp))}</span></div></div>
        <div class="pill" data-node="grid" style="--c:${gridExport ? cHouse : cGrid}">${this._ic('transmission-tower', 'pi')}<div class="pt"><span class="pl">Мережа · ${gridImport ? 'імпорт' : gridExport ? 'експорт' : 'спокій'}</span><span class="pv" data-u="pill_grid_v"${this._h(this._eid('grid_power'))}>${this._fmtW(Math.abs(gp))}</span></div></div>
        <div class="pill" data-node="house" style="--c:${cHouse}">${this._ic('home-lightning-bolt', 'pi')}<div class="pt"><span class="pl">Будинок</span><span class="pv" data-u="pill_house_v"${this._h(this._eid('load_power'))}>${houseVal}</span></div></div>
        ${genOn ? `<div class="pill" data-node="gen" style="--c:${cGen}">${this._ic('engine', 'pi')}<div class="pt"><span class="pl">Генератор</span><span class="pv" data-u="pill_gen_v"${this._h(this._eid('generator_power'))}>${this._fmtW(genp)}</span></div></div>` : ''}
      </div>`;

    // detail sections
    const wm = { 'Zero Export To CT': 'Нуль-експорт (CT)', 'Zero Export To Load': 'Нуль-експорт', 'Selling First': 'Продаж', 'Energy Pattern': '—' }[this._st('work_mode', 'select')] || (this._st('work_mode', 'select') || '—');
    const ep = { 'Battery First': 'Батарея', 'Load First': 'Навантаження' }[this._st('energy_pattern', 'select')] || (this._st('energy_pattern', 'select') || '—');
    const row = (icon, label, value, c, u, eid) => `<div class="drow"${this._h(eid)}>${this._ic(icon, 'di')}<span class="dl">${label}</span><span class="dv" ${u ? `data-u="${u}"` : ''} ${c ? `style="color:${c}"` : ''}>${value}</span></div>`;

    const battSect = `
      <div class="sect">${this._ic('battery', 'si')}<span>Акумулятор</span></div>
      <div class="dgrid">
        ${row('flash', 'Напруга', this._n('battery_voltage').toFixed(1).replace('.', ',') + ' В', null, 'd_volt', this._eid('battery_voltage'))}
        ${row('current-dc', 'Струм', this._n('battery_current').toFixed(1).replace('.', ',') + ' А', null, 'd_curr', this._eid('battery_current'))}
        ${row('thermometer', 'Темп.', this._n('battery_temperature').toFixed(1).replace('.', ',') + ' °C', null, 'd_btemp', this._eid('battery_temperature'))}
        ${row('battery-heart-variant', 'Стан', battStateTxt, battCol)}
        ${row('database', 'Ємність', this._fmtKwh(this._n('battery_capacity')), null, 'd_cap', this._eid('battery_capacity'))}
        ${row('gauge', 'Заряд', Math.round(soc) + ' %', this._socColor(soc), 'd_soc', this._eid('battery'))}
      </div>`;

    // BMS-секція на КОЖЕН живий пак (після 2026-09-15 їх два на одній Y-шині)
    const packNames = this._config.bms_names || ['Батарея №1 (BMS)', 'Батарея №2 (BMS)'];
    let cellsSect = '';
    this._bmsPfx().forEach((pfx, pi) => {
      const cells = this._cells(pi);
      if (cells.length !== 16) return;
      const lo = Math.min(...cells), hi = Math.max(...cells);
      const bsoc = this._bms('zariad', pi);
      const bv = this._bms('napruga_paketa', pi);
      const ba = this._bms('strum', pi);
      const delta = this._bms('delta_komirok', pi);
      const soh = this._bms('zdorov_ia_soh', pi);
      const cyc = this._bms('tsikliv', pi);
      const cap = this._bms('realna_iemnist', pi);
      const bt = this._bms('temperatura_1', pi);
      // Дельта — головний індикатор: пасивний балансир JBD (20-60 мА на 150 Ah)
      // витягує розбіжність приблизно до 50 мВ, далі вже потрібна увага.
      const dCol = delta === null ? '' : delta < 30 ? '#2a9d4a' : delta < 100 ? '#e0a800' : 'var(--error-color)';
      const aCol = ba === null || Math.abs(ba) < 0.5 ? '' : ba > 0 ? '#34c759' : '#ff9f0a';
      // явний стан пака: заряд (зел.) / розряд (оранж.) / спокій — по знаку струму BMS
      const pCharge = ba !== null && ba > 0.5, pDis = ba !== null && ba < -0.5;
      const pState = pCharge
        ? `заряд ${this._fmtW(Math.abs((bv ?? 0) * ba))}`
        : pDis ? `розряд ${this._fmtW(Math.abs((bv ?? 0) * ba))}` : 'спокій';
      const pCol = pCharge ? '#34c759' : pDis ? '#ff9f0a' : 'var(--secondary-text-color)';
      // живий стан балансування комірок (jbd2mqtt, ~30с цикл) — glow+хвиля на активних барах
      const balSet = this._balCells(pi);
      const balList = Array.from(balSet).sort((a, b) => a - b);
      const balBadge = balList.length
        ? `<span class="balbadge" title="Комірки, що зараз балансуються">${this._ic('scale-balance', 'bi')}баланс: ${balList.join(', ')}</span>` : '';
      // сепаратор МІЖ блоками: перед 1-м паком — межа «інвертор/мережа/навантаження ↔ батареї»,
      // перед 2-м паком — межа «Батарея №1 ↔ Батарея №2»
      cellsSect += (pi === 0
          ? this._sep('battery-bolt', 'Батареї (BMS)')
          : this._sep('battery-bolt', packNames[pi] || `Батарея №${pi + 1} (BMS)`))
        + `
      <div class="sect">${this._ic(pCharge ? 'battery-charging' : 'view-grid', 'si')}<span>${packNames[pi] || `Батарея №${pi + 1} (BMS)`}</span>${balBadge}<span class="pstate" style="color:${pCol}" data-u="b${pi}_st">${pState}</span></div>
      <div class="cells">${cells.map((v, i) => {
        const pct = Math.max(4, Math.min(100, ((v - 2.9) / 0.8) * 100));
        const isBal = balSet.has(i + 1);
        const col = isBal ? '#ff9f0a' : v === lo ? '#e0a800' : v === hi ? '#4a9eff' : 'var(--acc)';
        return `<div class="cell${isBal ? ' bal' : ''}"${this._h(this._bmsEid('komirka_' + (i + 1), pi))} title="Комірка ${i + 1}: ${v.toFixed(3)} В${isBal ? ' · балансується' : ''}">
                  <div class="cbar${isBal ? ' bal' : ''}"><i style="height:${pct}%;background:${col}"></i>${isBal ? '<b class="cwave"></b>' : ''}</div>
                  <span class="cnum">${i + 1}</span>
                </div>`;
      }).join('')}</div>
      <div class="dgrid">
        ${row('gauge', 'Заряд', bsoc === null ? '—' : Math.round(bsoc) + ' %', this._socColor(bsoc ?? 0), `b${pi}_soc`, this._bmsEid('zariad', pi))}
        ${row('flash', 'Напруга', bv === null ? '—' : bv.toFixed(2).replace('.', ',') + ' В', null, `b${pi}_v`, this._bmsEid('napruga_paketa', pi))}
        ${row('current-dc', 'Струм', ba === null ? '—' : (ba > 0 ? '+' : '') + ba.toFixed(1).replace('.', ',') + ' А', aCol, `b${pi}_a`, this._bmsEid('strum', pi))}
        ${row('thermometer', 'Темп.', bt === null ? '—' : bt.toFixed(1).replace('.', ',') + ' °C', null, `b${pi}_t`, this._bmsEid('temperatura_1', pi))}
        ${row('arrow-expand-vertical', 'Дельта', delta === null ? '—' : delta.toFixed(0) + ' мВ', dCol, `b${pi}_d`, this._bmsEid('delta_komirok', pi))}
        ${row('heart-pulse', 'Здоров\'я', soh === null ? '—' : soh.toFixed(0) + ' %')}
        ${row('battery-sync', 'Циклів', cyc === null ? '—' : String(Math.round(cyc)))}
        ${row('database-check', 'Ємність факт.', cap === null ? '—' : cap.toFixed(1).replace('.', ',') + ' А·год')}
      </div>`;
    });

    // сепаратор: межа «батареї (BMS) ↔ решта (інвертор/тарифи)»
    const restSep = this._sep('cog', 'Інвертор і тарифи');

    const invSect = `
      <div class="sect">${this._ic('cog', 'si')}<span>Інвертор</span></div>
      <div class="dgrid">
        ${row('sine-wave', 'Режим', wm)}
        ${row('priority-high', 'Пріоритет', ep)}
        ${row(devOk ? 'check-circle' : 'alert-circle', 'Статус', this._st('device_state') || '—', devOk ? '#34c759' : '#ff3b30')}
        ${row('thermometer', 'Темп.', this._n('temperature').toFixed(1).replace('.', ',') + ' °C', null, 'i_temp', this._eid('temperature'))}
        ${row('current-ac', 'Частота', this._n('grid_frequency').toFixed(2).replace('.', ',') + ' Гц', null, 'i_freq', this._eid('grid_frequency'))}
        ${row(devOk ? 'shield-check' : 'shield-alert', 'Аларм', this._st('device_alarm') || '—', devOk ? '#34c759' : '#ffc107')}
      </div>`;

    // місячний облік купленого з мережі: кВт·год і грн по зонах (пакет deye_energy.yaml)
    const { mdk, mnk, mdu, mnu, mtu } = this._month();
    const fmtUah = this._fmtUah;
    const me = this._config.month_entities || {
      day_kwh: 'sensor.spozhito_zony_den', night_kwh: 'sensor.spozhito_zony_nich',
      day_uah: 'sensor.deye_cost_day', night_uah: 'sensor.deye_cost_night', total_uah: 'sensor.deye_cost_total',
    };
    const monthSect = (mdk === null && mnk === null) ? '' : `
      <div class="sect">${this._ic('calendar-month', 'si')}<span>Куплено за місяць</span><span class="pstate"${this._h(me.total_uah)} data-u="mo_total" style="color:var(--acc)">${mtu === null ? '' : fmtUah(mtu)}</span></div>
      <div class="dgrid">
        ${row('weather-sunny', 'День', mdk === null ? '—' : this._fmtKwh(mdk), null, 'mo_dk', me.day_kwh)}
        ${row('cash', 'День, грн', fmtUah(mdu), null, 'mo_du', me.day_uah)}
        ${row('weather-night', 'Ніч', mnk === null ? '—' : this._fmtKwh(mnk), null, 'mo_nk', me.night_kwh)}
        ${row('cash', 'Ніч, грн', fmtUah(mnu), null, 'mo_nu', me.night_uah)}
        ${row('sigma', 'Разом', (mdk === null || mnk === null) ? '—' : this._fmtKwh(mdk + mnk), null, 'mo_tk')}
        ${row('cash-multiple', 'Разом, грн', fmtUah(mtu), 'var(--acc)', 'mo_tu', me.total_uah)}
      </div>`;

    const todaySect = `
      <div class="sect">${this._ic('calendar-today', 'si')}<span>Сьогодні</span></div>
      <div class="chips">
        <div class="chip"${this._h(this._eid('today_battery_charge'))}>${this._ic('battery-plus-variant', 'ci')}<span data-u="t_charge">${this._fmtKwh(this._n('today_battery_charge'))}</span></div>
        <div class="chip"${this._h(this._eid('today_battery_discharge'))}>${this._ic('battery-minus-variant', 'ci')}<span data-u="t_dis">${this._fmtKwh(this._n('today_battery_discharge'))}</span></div>
        <div class="chip"${this._h(this._eid('today_energy_import'))}>${this._ic('transmission-tower-import', 'ci')}<span data-u="t_imp">${this._fmtKwh(this._n('today_energy_import'))}</span></div>
        <div class="chip"${this._h(this._eid('today_energy_export'))}>${this._ic('transmission-tower-export', 'ci')}<span data-u="t_exp">${this._fmtKwh(this._n('today_energy_export'))}</span></div>
        <div class="chip"${this._h(this._eid('today_load_consumption'))}>${this._ic('home-lightning-bolt', 'ci')}<span data-u="t_load">${this._fmtKwh(this._n('today_load_consumption'))}</span></div>
      </div>`;

    // таб-бар «Стан | Графіки | [Відключення]» — тільки в розгорнутому вигляді; вибір памʼятається
    // в localStorage. Вкладка «Відключення» показується ЛИШЕ коли в конфізі заданий outage:
    // {source_url, group} — без цього самого джерела графіка відключень нема, тож нема й вкладки.
    const outageEnabled = this._outageEnabled();
    if (this._tab === 'outage' && !outageEnabled) this._tab = 'state';
    const tabsBar = `
      <div class="tabs">
        <button class="tab ${this._tab === 'state' || !this._tab ? 'on' : ''}" data-tab="state">Стан</button>
        <button class="tab ${this._tab === 'graphs' ? 'on' : ''}" data-tab="graphs">Графіки</button>
        ${outageEnabled ? `<button class="tab ${this._tab === 'outage' ? 'on' : ''}" data-tab="outage">Відключення</button>` : ''}
      </div>`;
    const stateBody = `
        ${diagram}
        ${pills}
        ${battSect}
        ${cellsSect}
        ${restSep}
        ${invSect}
        ${todaySect}
        ${monthSect}`;
    const graphsBody = `<div class="gwrap" id="gwrap"></div>`;
    const outageBody = `<div id="owrap"></div>`;

    const anim = this._anim ? 'pop' : ''; this._anim = false;
    this.shadowRoot.innerHTML = `${this._css()}
      <ha-card class="${anim}" style="--acc:${cAcc};--ts:${this._titleSize ? this._titleSize + 'rem' : '1.25rem'};zoom:${this._scale || 1};">
        <div class="hdr">
          <div class="ttl">${this._ic('solar-power-variant', 'ht')} ${title}</div>
          <div class="hbtns">
            ${this._layout === 'mini' ? `<button class="gear" data-collapse="1" title="Згорнути до міні">${this._ic('chevron-up', 'wic')}</button>` : ''}
            <button class="gear" data-popup="mode" title="Режими роботи">${this._ic('tune-variant', 'wic')}</button>
            <span class="wchip" style="color:${online ? '#2a9d4a' : 'var(--error-color)'}" title="${online ? 'Онлайн' : 'Офлайн'}">${this._ic(online ? 'wifi' : 'wifi-off', 'wic')}</span>
          </div>
        </div>
        ${critBar}
        ${tabsBar}
        ${this._tab === 'graphs' ? graphsBody : this._tab === 'outage' ? outageBody : stateBody}
      </ha-card>`;
    this._wire();
    if (this._tab === 'graphs') this._mountGraphs();
    else if (this._tab === 'outage') this._mountOutage();
  }

  // опційна кнопка-форсаж генератора (попап «Генератор») — свій script.* + норма відкату
  _genBoostCfg() {
    const g = (this._config && this._config.gen_boost) || {};
    return { script: g.script || 'script.deye_gen_boost', current: g.current != null ? g.current : 30, peak: g.peak_shaving != null ? g.peak_shaving : 2500 };
  }

  // чи налаштована вкладка «Відключення» (джерело ДТЕК-JSON + група/черга в конфізі)
  _outageEnabled() {
    const oc = this._config && this._config.outage;
    return !!(oc && oc.source_url && oc.group);
  }

  // ── вкладка «Графіки»: динамічний import() окремого модуля (тримає головний файл компактним) ──
  _setTab(t) {
    if (t === 'outage' && !this._outageEnabled()) return;
    if (this._tab === t) return;
    this._tab = t;
    try { localStorage.setItem(`deye-card-tab:${this._pfx}`, t); } catch (e) { /* приватний режим — ок, просто не запамʼятається */ }
    this._anim = false;
    this._render();
  }

  async _mountGraphs() {
    const host = this.shadowRoot.querySelector('#gwrap');
    if (!host) return;
    // структурний rebuild (sSig) може статись, поки юзер на вкладці «Графіки» — напр. через
    // живу зміну списку комірок, що балансуються (теж у sSig). Ремонт вкладки неминучий
    // (новий #gwrap = нові canvas), але вибраний діапазон і кеш фетчів варто зберегти,
    // щоб юзера не відкидало на 24г і не смикало API повторно без потреби.
    const prevRange = this._graphsTab ? this._graphsTab.range : null;
    const prevCache = this._graphsTab ? this._graphsTab.cache : null;
    try {
      const mod = await import(`./deye-graphs.js?v=${DeyeCard.GRAPHS_V}`);
      // поки чекали на модуль, могли закрити картку/перемкнути вкладку/перебудувати DOM
      if (!this._hass || this._tab !== 'graphs') return;
      const gwrapNow = this.shadowRoot.querySelector('#gwrap');
      if (!gwrapNow) return;
      if (this._graphsTab) this._graphsTab.unmount();
      this._graphsTab = new mod.DeyeGraphsTab(this);
      if (prevRange) this._graphsTab.range = prevRange;
      if (prevCache) this._graphsTab.cache = prevCache;
      this._graphsTab.mount(gwrapNow);
    } catch (e) {
      console.error('deye-card: не вдалось завантажити deye-graphs.js', e);
      const gwrapNow = this.shadowRoot.querySelector('#gwrap');
      if (gwrapNow) gwrapNow.innerHTML = `<div style="padding:24px 10px;text-align:center;color:var(--secondary-text-color);font-size:.85rem">
        Не вдалось завантажити модуль графіків (deye-graphs.js). Перевір ресурс /local/deye-graphs.js.</div>`;
    }
  }

  // ── вкладка «Відключення»: так само динамічний import() окремого модуля ──
  async _mountOutage() {
    const host = this.shadowRoot.querySelector('#owrap');
    if (!host) return;
    try {
      const mod = await import(`./deye-outage.js?v=${DeyeCard.OUTAGE_V}`);
      if (!this._hass || this._tab !== 'outage') return;
      const owrapNow = this.shadowRoot.querySelector('#owrap');
      if (!owrapNow) return;
      if (this._outageTab) this._outageTab.unmount();
      this._outageTab = new mod.DeyeOutageTab(this);
      this._outageTab.mount(owrapNow);
    } catch (e) {
      console.error('deye-card: не вдалось завантажити deye-outage.js', e);
      const owrapNow = this.shadowRoot.querySelector('#owrap');
      if (owrapNow) owrapNow.innerHTML = `<div style="padding:24px 10px;text-align:center;color:var(--secondary-text-color);font-size:.85rem">
        Не вдалось завантажити модуль вкладки «Відключення» (deye-outage.js). Перевір ресурс /local/deye-outage.js.</div>`;
    }
  }

  _wire() {
    // node tap → control popup (батарея/мережа/генератор/інвертор); будинок → more-info
    const popupNode = { batt: 'batt', grid: 'grid', gen: 'gen', inv: 'mode' };
    this.shadowRoot.querySelectorAll('[data-node]').forEach(el => {
      const k = el.dataset.node;
      el.addEventListener('click', () => {
        if (popupNode[k]) this._openPopup(popupNode[k]);
        else this._moreInfo(`sensor.${this._pfx}_load_power`);
      });
    });
    this.shadowRoot.querySelectorAll('[data-popup]').forEach(b =>
      b.addEventListener('click', e => { e.stopPropagation(); this._openPopup(b.dataset.popup); }));
    this.shadowRoot.querySelectorAll('[data-eid]').forEach(el =>
      el.addEventListener('click', e => { e.stopPropagation(); this._moreInfo(el.dataset.eid); }));
    const col = this.shadowRoot.querySelector('[data-collapse]');
    if (col) col.addEventListener('click', e => { e.stopPropagation(); this._expanded = false; this._anim = true; this._render(); });
    this.shadowRoot.querySelectorAll('[data-tab]').forEach(b =>
      b.addEventListener('click', e => { e.stopPropagation(); this._setTab(b.dataset.tab); }));
    // чотирипозиційний селектор режиму в банері — input_select.deye_mode
    this.shadowRoot.querySelectorAll('[data-mode]').forEach(b =>
      b.addEventListener('click', e => { e.stopPropagation(); this._setSelect(this._modeEid(), this._modeMap()[b.dataset.mode]); }));
    // тап по будь-якому показнику з entity_id → повноекранний графік історії (тільки
    // розгорнутий вигляд; stopPropagation, щоб не зʼїдало клік попапу/контролів на батьку)
    this.shadowRoot.querySelectorAll('[data-hist]').forEach(el => {
      el.addEventListener('click', e => { e.stopPropagation(); this._openHistory(el.dataset.hist || el.getAttribute('data-hist')); });
    });
  }

  // ── control popups (visionos glass, gree-style float-in) ──
  _svc(domain, service, data) { if (this._hass) this._hass.callService(domain, service, data); }
  _setSelect(eid, opt) { this._svc('select', 'select_option', { entity_id: eid, option: opt }); }
  _setNum(eid, val) { this._svc('number', 'set_value', { entity_id: eid, value: val }); }
  _toggle(eid) { this._svc(eid.split('.')[0], 'toggle', { entity_id: eid }); }
  _estate(eid) { const e = this._hass && this._hass.states[eid]; return e ? e.state : null; }
  // «Режим роботи» — чотирипозиційний input_select.deye_mode («Еко» / «Авто» / «Критичний» /
  // «Балансування»), замінює колишній тумблер input_boolean.deye_winter_ready (виведений з ужитку).
  _modeEid() { return (this._config && this._config.mode_entity) || 'input_select.deye_mode'; }
  // mode_options: мапа внутрішніх (українських) режимів картки на опції ВАШОГО input_select,
  // напр. { eco: 'Eco', auto: 'Auto', emergency: 'Emergency', balance: 'Balance' }.
  // Without it the card expects the Ukrainian option names «Еко/Авто/Критичний/Балансування».
  _modeMap() {
    const o = (this._config && this._config.mode_options) || {};
    return { 'Еко': o.eco || 'Еко', 'Авто': o.auto || 'Авто',
             'Критичний': o.emergency || 'Критичний', 'Балансування': o.balance || 'Балансування' };
  }
  _mode() {
    const raw = this._estate(this._modeEid());
    const m = this._modeMap();
    return Object.keys(m).find(k => m[k] === raw) || 'Авто';
  }
  // пояснювальний рядок під банером — ЗАВЖДИ про селектор (незалежно від фактичного
  // override блекаутом/генератором, який йде в cs.label вище).
  _modeExplain() {
    const mode = this._mode();
    if (mode === 'Балансування') return this._balanceExplain();
    if (mode === 'Критичний') return 'примусово 100%';
    if (mode === 'Еко') return 'примусово еко';
    const reasons = this._autoReasons();
    if (reasons.length) return `АВТО → критичний: ${reasons.join(', ')}`;
    // v49: профіль «еко+» (POSSIBLE, deye_outage_guard v5.2) — звичайне ЕКО по дню
    // (P2–P4=55, P5=20), єдина відмінність від «еко» — вночі заряд до 100% (P1,P6=100 замість 90)
    const poss = this._autoPossible();
    if (poss) {
      const win = poss.windowsTxt ? ` (можливі відключення: ${poss.windowsTxt})` : ' (можливі відключення)';
      return `АВТО → еко+${win} — як еко, але вночі заряд до 100%`;
    }
    return 'АВТО → еко (відключень не очікується)';
  }
  // «Балансування» (⚖️) — полички як у «Критичний», але заряд циклом малим струмом:
  // ліміт 5 А → стоп на 3.70 В (0 А) → релаксація до 3.55 → знову 5 А. Живий стан беремо з
  // BMS: sensor...bms_rezhim_balansuvannia (charge/static/unknown) + ...balansuvannia_komirok
  // (номери комірок, що ПРЯМО ЗАРАЗ балансуються) для обох паків — «стара» (pi=0) й «нова» (pi=1).
  _balModeTxt(pi) {
    const raw = this._estate(this._bmsEid('rezhim_balansuvannia', pi));
    return raw === 'charge' ? 'під зарядом' : raw === 'static' ? 'статичний' : '—';
  }
  _balPackTxt(pi) {
    const cells = Array.from(this._balCells(pi)).sort((a, b) => a - b);
    return `${this._balModeTxt(pi)} (${cells.length ? cells.join(', ') : '—'})`;
  }
  _balanceExplain() {
    const a = this._maxChgA(); const aTxt = a === null ? '—' : Math.round(a) + ' А';
    const oldCells = this._balCells(0), newCells = this._balCells(1);
    if (!oldCells.size && !newCells.size) {
      return `цикл 0↔5 А, стоп на 3.70 В; ліміт зараз ${aTxt}; балансир спить (комірки нижче 3.40 або струм поза режимом)`;
    }
    return `цикл 0↔5 А, стоп на 3.70 В; ліміт зараз ${aTxt}; баланс: стара ${this._balPackTxt(0)}, нова ${this._balPackTxt(1)}`;
  }

  // ── РЕАЛЬНИЙ критичний режим (не лише ручний тумблер) ──
  // Автоматизація-сторож (на твій розсуд — власний пакет HA) тримає полички на 100%, коли
  // БУДЬ-ЩО з трьох спрацювало; хелпери/сенсори без pfx (не inverter_deye_*), тому напряму
  // через _estate. Усі п'ять — опційні: конфігуруються через outage_entities, кожен можна
  // лишити порожнім/невказаним — відповідна перевірка просто ніколи не спрацює (без помилок).
  _outageEids() {
    const oe = (this._config && this._config.outage_entities) || {};
    return {
      schedule: oe.schedule || 'binary_sensor.outage_schedule_today',
      emergency: oe.emergency || 'binary_sensor.outage_emergency_unified',
      seenToday: oe.seen_today || 'input_boolean.deye_outage_seen_today',
      // профіль «еко+» (можливі, НЕ точні відключення) — опційний сторожовий пакет
      possible24h: oe.possible24h || 'binary_sensor.outage_possible_24h',
      todaySource: oe.today_source || 'sensor.outage_dtek_today_source',
    };
  }
  // причини автоматичного екстреного дня (профіль EMERGENCY) — порожній масив, якщо жодна не спрацювала
  _autoReasons() {
    const e = this._outageEids(), out = [];
    if (this._estate(e.schedule) === 'on') out.push('графік відключень на добу');
    if (this._estate(e.emergency) === 'on') out.push('аварійні відключення');
    if (this._estate(e.seenToday) === 'on') out.push('було відключення >10 хв (до 00:00)');
    return out;
  }
  // v49: профіль «еко+» (deye_outage_guard v5.2, POSSIBLE) — можливі відключення, не точні.
  // Активний лише коли немає жодної критичної причини (_autoReasons() порожній).
  // binary_sensor.outage_possible_24h = on → атрибути містять список вікон; формат атрибута
  // ще не стабілізований бекендом, тому підтримуємо кілька можливих форм (windows/periods/ranges/slots).
  _autoPossible() {
    const e = this._outageEids();
    const st = this._hass && this._hass.states[e.possible24h];
    if (!st || st.state !== 'on') return null;
    const attrs = st.attributes || {};
    const raw = attrs.windows || attrs.periods || attrs.ranges || attrs.slots || attrs.list || [];
    const windowsTxt = Array.isArray(raw) && raw.length
      ? raw.map(w => this._fmtWindow(w)).filter(Boolean).join(', ')
      : '';
    return { windowsTxt, source: this._estate(e.todaySource) || '' };
  }
  // «HH:MM–HH:MM» з рядка / [start,end] / {start,end} / {from,to}
  _fmtWindow(w) {
    if (typeof w === 'string') return w;
    if (Array.isArray(w) && w.length >= 2) return `${w[0]}–${w[1]}`;
    if (w && typeof w === 'object') {
      const a = w.start || w.from || w.s, b = w.end || w.to || w.e;
      if (a && b) return `${a}–${b}`;
    }
    return '';
  }
  // шість поличок SOC program_1..6 + макс. струм заряду — фактичний стан Deye, а не намір
  _progSocEids() { const p = this._pfx; return [1, 2, 3, 4, 5, 6].map(n => `number.${p}_program_${n}_soc`); }
  _progSoc() { return this._progSocEids().map(eid => { const v = parseFloat(this._estate(eid)); return isFinite(v) ? v : null; }); }
  _maxChgA() { const v = parseFloat(this._estate(`number.${this._pfx}_battery_max_charging_current`)); return isFinite(v) ? v : null; }
  _shelvesAt100() { const s = this._progSoc(); return s.every(v => v === 100); }
  // v49: P6 (program_6 soc) — маркер профілю deye_outage_guard v5.2: еко+ (ніч до 100%) → P6=100,
  // звичайне еко → P6=90. (P1 не звіряти — його чіпає сторож комірок.)
  _shelfP6() { return this._progSoc()[5]; }
  // текст попередження про розбіжність поличок із активним профілем — спільний для банера
  // картки та блоку «Режим» у вкладці «Відключення» (той самий текст в обох місцях)
  _shelvesWarnTxt() {
    const cs = this._critState();
    if (!cs.warn) return '';
    return cs.key === 'ecoplus' ? '⚠ полички ще не еко+ (P6≠100)' : '⚠ полички ще не 100';
  }
  _shelvesTxt() {
    const s = this._progSoc().map(v => v === null ? '—' : Math.round(v)).join('/');
    const a = this._maxChgA(); const aTxt = a === null ? '—' : Math.round(a) + ' А';
    return `${s} · ${aTxt}`;
  }
  // зведений стан банера — пріоритет: блекаут > генератор > ручний критичний (селектор) >
  // балансування (селектор) > еко-примусово (селектор) > авто-екстрений день > еко.
  // Фактичний стан Deye (полички) НЕ впливає на пріоритет — лише підсвічує розбіжність (⚠).
  _critState() {
    const gridOn = this._st('grid', 'binary_sensor') == null ? true : this._on('grid');
    const genp = this._n('generator_power');
    const genOn = this._on('generator') || genp > 15;
    const mode = this._mode();
    const reasons = this._autoReasons();
    let st;
    if (!gridOn) st = { key: 'blackout', icon: '⚡', label: 'Немає мережі — на батареях' };
    else if (genOn) st = { key: 'gen', icon: '⛽', label: 'Генератор — заряд до 100%' };
    else if (mode === 'Критичний') st = { key: 'manual', icon: '🚨', label: 'Критичний режим — вручну (100%)' };
    else if (mode === 'Балансування') st = { key: 'balancing', icon: '⚖️', label: 'Балансування — цикл 0↔5 А' };
    else if (mode === 'Еко') st = { key: 'eco', icon: '🌿', label: 'Еко-режим — примусово' };
    else if (reasons.length) st = { key: 'auto', icon: '🚨', label: `Критичний режим — АВТО: ${reasons.join(', ')}` };
    // v49: профіль «еко+» (POSSIBLE) — можливі (не точні) відключення найближчі 24 год
    else if (this._autoPossible()) st = { key: 'ecoplus', icon: '🟡', label: 'Еко+ — можливі відключення' };
    else st = { key: 'eco', icon: '🌿', label: 'Еко-режим' };
    st.mode = mode;
    st.reasons = reasons;
    st.shelvesOk = this._shelvesAt100();
    st.warn = st.key === 'ecoplus' ? this._shelfP6() !== 100 : (st.key !== 'eco' && !st.shelvesOk);
    return st;
  }

  _popupDefs() {
    const p = this._pfx;
    return {
      mode: { title: 'Режими роботи', icon: 'tune-variant', controls: [
        // Режим роботи — чотирипозиційний input_select.deye_mode: Еко (примусово еко-полички),
        // Авто (екстрений, якщо графік/аварійні/було відключення — інакше еко), Критичний
        // (примусово 100%/80А), Балансування (полички як Критичний, цикл заряду 0↔5 А).
        // Замінює колишній тумблер input_boolean.deye_winter_ready.
        { type: 'select', eid: this._modeEid(), label: 'Режим роботи', map: (() => { const m = this._modeMap(); return { [m['Еко']]: '🌿 Еко', [m['Авто']]: '🤖 Авто', [m['Критичний']]: '🚨 Критичний', [m['Балансування']]: '⚖️ Балансування' }; })() },
        { type: 'select', eid: `select.${p}_work_mode`, label: 'Режим', map: { 'Export First': 'Продаж надлишку', 'Zero Export To Load': 'Нуль-експорт', 'Zero Export To CT': 'Нуль-експорт (CT)' } },
        { type: 'select', eid: `select.${p}_energy_pattern`, label: 'Пріоритет', map: { 'Battery First': 'Батарея', 'Load First': 'Навантаження' } },
        { type: 'select', eid: `select.${p}_io_mode`, label: 'Порт I/O', map: { 'Generator': 'Генератор', 'SmartLoad': 'SmartLoad', 'Microinverter': 'Мікроінвертор' } },
        { type: 'switch', eid: `switch.${p}_off_grid`, label: 'Острівний режим' },
      ] },
      batt: { title: 'Акумулятор', icon: 'battery-cog', controls: [
        { type: 'num', eid: `number.${p}_battery_max_charging_current`, label: 'Макс. струм заряду', min: 0, max: 240, step: 5, unit: 'А' },
        { type: 'num', eid: `number.${p}_battery_max_discharging_current`, label: 'Макс. струм розряду', min: 0, max: 240, step: 5, unit: 'А' },
        { type: 'num', eid: `number.${p}_battery_low_soc`, label: 'Низький заряд', min: 0, max: 100, step: 1, unit: '%' },
        { type: 'num', eid: `number.${p}_battery_shutdown_soc`, label: 'Вимкнення', min: 0, max: 100, step: 1, unit: '%' },
        { type: 'num', eid: `number.${p}_battery_restart_soc`, label: 'Перезапуск', min: 0, max: 100, step: 1, unit: '%' },
        { type: 'switch', eid: `switch.${p}_battery_wake_up`, label: 'Пробудження BMS' },
      ] },
      grid: { title: 'Мережа', icon: 'transmission-tower', controls: [
        { type: 'switch', eid: `switch.${p}_battery_grid_charging`, label: 'Заряд від мережі' },
        { type: 'num', eid: `number.${p}_battery_grid_charging_start`, label: 'Старт заряду при SOC', min: 0, max: 100, step: 1, unit: '%' },
        { type: 'num', eid: `number.${p}_battery_grid_charging_current`, label: 'Струм заряду', min: 0, max: 240, step: 5, unit: 'А' },
        { type: 'num', eid: `number.${p}_zero_export_power`, label: 'Потужн. нуль-експорту', min: -20, max: 500, step: 10, unit: 'Вт' },
      ] },
      gen: { title: 'Генератор', icon: 'engine', controls: [
        { type: 'switch', eid: `switch.${p}_generator`, label: 'Генератор' },
        { type: 'switch', eid: `switch.${p}_battery_generator_charging`, label: 'Заряд від генератора' },
        { type: 'num', eid: `number.${p}_battery_generator_charging_start`, label: 'Старт заряду при SOC', min: 0, max: 100, step: 1, unit: '%' },
        { type: 'num', eid: `number.${p}_battery_generator_charging_current`, label: 'Струм заряду', min: 0, max: 240, step: 5, unit: 'А' },
        { type: 'num', eid: `number.${p}_generator_peak_shaving`, label: 'Поріг відбору з генератора', min: 500, max: 5000, step: 100, unit: 'Вт' },
        { type: 'action', eid: this._genBoostCfg().script, icon: 'fuel',
          label: 'Форсаж на годину', labelRun: 'Форсаж триває · вимкнути',
          // Значення норми тримаємо тут: при достроковому скасуванні скрипт
          // перерваний на delay і повернути їх сам уже не може — це робить картка.
          // Опційно: власний script.* + «нормальні» значення для відкату — через
          // конфіг gen_boost: {script, current, peak_shaving}. Якщо скрипта нема
          // в HA — _ctrlHTML() сам ховає цю кнопку (без помилок).
          restore: { [`number.${p}_battery_generator_charging_current`]: this._genBoostCfg().current,
                     [`number.${p}_generator_peak_shaving`]: this._genBoostCfg().peak },
          hint: 'Заряд 30→40 А (90% генератора) на 60 хв. Натисни ще раз, щоб вимкнути достроково.' },
      ] },
    };
  }

  _ctrlHTML(c, i) {
    const cur = this._estate(c.eid);
    if (cur == null) return '';
    const acc = this._col('accent');
    if (c.type === 'select') {
      const opts = (this._hass.states[c.eid].attributes.options) || Object.keys(c.map || {});
      const pills = opts.map(o => `<button class="opt ${o === cur ? 'on' : ''}" data-eid="${c.eid}" data-opt="${o.replace(/"/g, '&quot;')}">${(c.map && c.map[o]) || o}</button>`).join('');
      return `<div class="pc" style="--d:${i * 40}ms"><div class="pcl">${c.label}</div><div class="opts">${pills}</div></div>`;
    }
    if (c.type === 'action') {
      // Кнопка-дія: запускає HA-скрипт. Поки він виконується (state === 'on')
      // підсвічуємо, щоб було видно, що форсаж триває.
      const running = cur === 'on';
      return `<div class="pc" style="--d:${i * 40}ms">
        <button class="actbtn ${running ? 'run' : ''}" data-run="${c.eid}"
                data-restore="${c.restore ? encodeURIComponent(JSON.stringify(c.restore)) : ''}">
          ${this._ic(c.icon || 'flash', 'wic')}<span>${running ? (c.labelRun || c.label) : c.label}</span>
        </button>
        ${c.hint ? `<div class="acthint">${c.hint}</div>` : ''}
      </div>`;
    }
    if (c.type === 'switch') {
      const on = cur === 'on';
      return `<div class="pc swrow ${on ? 'on' : ''}" style="--d:${i * 40}ms" data-eid="${c.eid}" data-sw="1"><div class="pcl">${c.label}</div><span class="sw"><span class="kn"></span></span></div>`;
    }
    // num → slider
    const v = parseFloat(cur); const vv = isFinite(v) ? v : c.min;
    return `<div class="pc" style="--d:${i * 40}ms">
      <div class="pcl">${c.label}<b class="nv" style="color:${acc}">${(Math.round(vv * 10) / 10).toString().replace('.', ',')} ${c.unit}</b></div>
      <input class="sl" type="range" data-eid="${c.eid}" min="${c.min}" max="${c.max}" step="${c.step}" value="${vv}" style="--c:${acc}">
    </div>`;
  }

  _openPopup(kind) {
    const def = this._popupDefs()[kind]; if (!def) return;
    this._popupOpen = true; this._popupKind = kind;
    if (this._ov) this._ov.remove();
    const ov = document.createElement('div'); ov.className = 'ovl';
    ov.innerHTML = `<style>${this._popupCss()}</style>
      <div class="panel" style="--acc:${this._col('accent')}">
        <div class="ph">${this._ic(def.icon, 'phi')}<span>${def.title}</span><button class="pclose" title="Закрити">${this._ic('close', 'wic')}</button></div>
        <div class="pbody">${def.controls.map((c, i) => this._ctrlHTML(c, i)).join('')}</div>
        <div class="pfoot"><button class="moreinfo" data-mi="${def.controls[0] ? def.controls[0].eid : ''}">${this._ic('open-in-new', 'wic')} Усі параметри</button></div>
      </div>`;
    ov.addEventListener('click', e => { if (e.target === ov) this._closePopup(); });
    this.shadowRoot.appendChild(ov); this._ov = ov;
    this._wirePopup();
    requestAnimationFrame(() => ov.classList.add('show'));
  }

  _wirePopup() {
    const ov = this._ov; if (!ov) return;
    ov.querySelector('.pclose').addEventListener('click', () => this._closePopup());
    ov.querySelectorAll('[data-run]').forEach(b => b.addEventListener('click', e => {
      e.stopPropagation();
      const eid = b.dataset.run;
      const running = this._estate(eid) === 'on';
      if (running) {
        // Скрипт зупиняється посеред delay, тож норму повертаємо звідси самі
        this._svc('script', 'turn_off', { entity_id: eid });
        const r = b.dataset.restore ? JSON.parse(decodeURIComponent(b.dataset.restore)) : {};
        Object.entries(r).forEach(([k, v]) => this._setNum(k, v));
        b.classList.remove('run');
      } else {
        this._svc('script', 'turn_on', { entity_id: eid });
        b.classList.add('run');
      }
    }));
    const mi = ov.querySelector('.moreinfo');
    if (mi) mi.addEventListener('click', () => { const e = mi.dataset.mi; this._closePopup(); if (e) this._moreInfo(e); });
    ov.querySelectorAll('.opt').forEach(b => b.addEventListener('click', () => {
      this._setSelect(b.dataset.eid, b.dataset.opt);
      b.parentElement.querySelectorAll('.opt').forEach(x => x.classList.toggle('on', x === b));
    }));
    ov.querySelectorAll('[data-sw]').forEach(r => r.addEventListener('click', () => {
      this._toggle(r.dataset.eid); r.classList.toggle('on');
    }));
    ov.querySelectorAll('.sl').forEach(s => {
      const lbl = s.parentElement.querySelector('.nv'); const u = lbl.textContent.replace(/^[\d.,\-]+\s*/, '');
      s.addEventListener('input', e => { lbl.textContent = (+e.target.value).toString().replace('.', ',') + ' ' + u; });
      s.addEventListener('change', e => this._setNum(e.target.dataset.eid, +e.target.value));
    });
  }

  _closePopup() {
    const ov = this._ov; this._popupOpen = false; this._popupKind = null;
    if (!ov) return;
    ov.classList.remove('show');
    setTimeout(() => { if (ov) ov.remove(); this._ov = null; this._render(); }, 220);
  }

  // ══════════════════ ГРАФІК ІСТОРІЇ (touch: pan/pinch-zoom/crosshair) ══════════════════
  // Повноекранний оверлей з canvas-графіком по тапу на будь-якому показнику з entity_id.
  // Дані з HA history API (WS history/history_during_period, фолбек REST history/period).
  // Жест-модель на Pointer Events (єдиний код і для тач, і для миші):
  //  · 1 палець швидкий рух → пан (зсув вікна часу);
  //  · 1 палець тап/тримання без різкого руху → курсор-перехрестя, тягнеш — їде;
  //  · 2 пальці → pinch-zoom навколо центру щипка;
  //  · миша: drag=пан, wheel=зум навколо курсора, hover (без кнопки)=курсор;
  //  · подвійний тап/клік → скинути зум до поточного пресету;
  //  · свайп вниз по шапці або клік поза панеллю → закрити.
  _histPresets() { return { '3h': 3 * 36e5, '24h': 24 * 36e5, '7d': 7 * 864e5, '30d': 30 * 864e5 }; }

  _openHistory(eid) {
    if (!this._hass || !eid) return;
    const st = this._hass.states[eid];
    if (!st) return;
    this._popupOpen = true; // блокує фоновий _render/_patch, поки оверлей відкритий (як і control-попап)
    if (this._histOv) this._histOv.remove();
    const attrs = st.attributes || {};
    const name = attrs.friendly_name || eid;
    const unit = attrs.unit_of_measurement || '';
    const isBinary = eid.startsWith('binary_sensor.');
    const ov = document.createElement('div'); ov.className = 'hovl';
    ov.innerHTML = `<style>${this._histCss()}</style>
      <div class="hpanel">
        <div class="hhdr"><div class="hname">${name}</div><button class="hclose" title="Закрити">${this._ic('close', 'wic')}</button></div>
        <div class="hstats">
          <div class="hstat"><span class="hsl">поточне</span><b class="hsv" data-hs="cur">—</b></div>
          <div class="hstat"><span class="hsl">мін.</span><b class="hsv" data-hs="min">—</b></div>
          <div class="hstat"><span class="hsl">макс.</span><b class="hsv" data-hs="max">—</b></div>
          <div class="hstat"><span class="hsl">серед.</span><b class="hsv" data-hs="avg">—</b></div>
        </div>
        <div class="hcanwrap"><canvas class="hcanvas"></canvas><div class="htip" hidden></div><div class="hloading">Завантаження…</div></div>
        <div class="hpresets">
          <button data-p="3h">3г</button><button data-p="24h" class="on">24г</button><button data-p="7d">7д</button><button data-p="30d">30д</button>
        </div>
      </div>`;
    ov.addEventListener('click', e => { if (e.target === ov) this._closeHistory(); });
    this.shadowRoot.appendChild(ov); this._histOv = ov;
    const canvas = ov.querySelector('.hcanvas');
    // старт з нормальним 24г-вікном одразу (не 0..0) — щоб перший кадр до відповіді
    // history API не малював вісь часу від епохи (порожній canvas з правильною сіткою,
    // без спалаху «01.01.1970»)
    const nowMs = Date.now(), initStart = nowMs - 24 * 36e5;
    this._hs = {
      eid, unit, isBinary, points: [], dataStart: initStart, dataEnd: nowMs, viewStart: initStart, viewEnd: nowMs,
      preset: '24h', cursor: null, pointers: new Map(), mode: null, pinchStartDist: 0, pinchStartView: null, pinchCenterX: 0,
      lastTapT: 0, lastTapX: 0, holdTimer: null, extendTimer: null, cw: 0, ch: 0, suppressNextTap: false,
      canvas, ctx: canvas.getContext('2d'),
      el: {
        cur: ov.querySelector('[data-hs="cur"]'), min: ov.querySelector('[data-hs="min"]'),
        max: ov.querySelector('[data-hs="max"]'), avg: ov.querySelector('[data-hs="avg"]'),
        tip: ov.querySelector('.htip'), loading: ov.querySelector('.hloading'), wrap: ov.querySelector('.hcanwrap'),
      },
    };
    this._wireHistory();
    requestAnimationFrame(() => { ov.classList.add('show'); this._resizeHistCanvas(); this._loadHistory('24h'); });
  }

  _closeHistory() {
    const ov = this._histOv, hsAtClose = this._hs; this._popupOpen = false;
    if (this._hs) {
      clearTimeout(this._hs.holdTimer); clearTimeout(this._hs.extendTimer);
      if (this._hs.ro) this._hs.ro.disconnect();
    }
    if (!ov) { this._hs = null; this._render(); return; }
    ov.classList.remove('show');
    setTimeout(() => {
      // захист від гонки: якщо за ці 200мс встигли ЗНОВУ відкрити графік (нова ov/_hs),
      // не чіпаємо новішу сесію — прибираємо тільки ТОЙ overlay, що закривали
      if (ov) ov.remove();
      if (this._histOv === ov) this._histOv = null;
      if (this._hs === hsAtClose) this._hs = null;
      if (this._histOv == null) this._render();
    }, 200);
  }

  _wireHistory() {
    const ov = this._histOv, hs = this._hs; if (!ov || !hs) return;
    ov.querySelector('.hclose').addEventListener('click', () => this._closeHistory());
    ov.querySelectorAll('[data-p]').forEach(b => b.addEventListener('click', () => {
      ov.querySelectorAll('[data-p]').forEach(x => x.classList.toggle('on', x === b));
      this._loadHistory(b.dataset.p);
    }));
    // свайп вниз по шапці — закрити
    const hhdr = ov.querySelector('.hhdr'); let sy = null;
    hhdr.addEventListener('pointerdown', e => { sy = e.clientY; });
    hhdr.addEventListener('pointermove', e => { if (sy != null && e.clientY - sy > 60) { this._closeHistory(); sy = null; } });
    hhdr.addEventListener('pointerup', () => { sy = null; });
    hhdr.addEventListener('pointercancel', () => { sy = null; });
    // resize
    hs.ro = new ResizeObserver(() => this._resizeHistCanvas());
    hs.ro.observe(hs.el.wrap);
    // canvas gestures — Pointer Events: один код-шлях тач + миша
    const c = hs.canvas;
    c.style.touchAction = 'none';
    c.addEventListener('pointerdown', e => this._histPointerDown(e), { passive: false });
    c.addEventListener('pointermove', e => this._histPointerMove(e), { passive: false });
    c.addEventListener('pointerup', e => this._histPointerUp(e), { passive: false });
    c.addEventListener('pointercancel', e => this._histPointerUp(e), { passive: false });
    c.addEventListener('wheel', e => this._histWheel(e), { passive: false });
    c.addEventListener('contextmenu', e => e.preventDefault());
  }

  _resizeHistCanvas() {
    const hs = this._hs; if (!hs) return;
    const wrap = hs.el.wrap; const r = wrap.getBoundingClientRect();
    hs.cw = Math.max(1, r.width); hs.ch = Math.max(1, r.height);
    const dpr = window.devicePixelRatio || 1;
    hs.canvas.width = Math.round(hs.cw * dpr); hs.canvas.height = Math.round(hs.ch * dpr);
    hs.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._drawHistory();
  }

  _setHistLoading(on) { const hs = this._hs; if (hs) hs.el.loading.hidden = !on; }

  async _loadHistory(preset, opts) {
    const hs = this._hs; if (!hs) return;
    opts = opts || {};
    const win = this._histPresets()[preset] || this._histPresets()['24h'];
    hs.preset = preset;
    const now = Date.now();
    const start = opts.start != null ? opts.start : now - win;
    const end = opts.end != null ? opts.end : now;
    this._setHistLoading(true);
    try {
      const pts = await this._fetchHistory(hs.eid, start, end);
      if (!this._hs || this._hs !== hs) return; // оверлей закрили поки чекали на відповідь
      if (opts.merge && hs.points.length) {
        hs.points = this._mergePoints(hs.points, pts);
        hs.dataStart = Math.min(hs.dataStart, start); hs.dataEnd = Math.max(hs.dataEnd, end);
      } else {
        hs.points = pts; hs.dataStart = start; hs.dataEnd = end;
        hs.viewStart = start; hs.viewEnd = end;
      }
      this._drawHistory();
    } catch (err) {
      console.error('deye-card: history fetch failed', err);
    } finally {
      if (this._hs === hs) this._setHistLoading(false);
    }
  }

  async _fetchHistory(eid, startMs, endMs) {
    const startIso = new Date(startMs).toISOString();
    const endIso = new Date(endMs).toISOString();
    let raw = null;
    if (this._hass.callWS) {
      try {
        const res = await this._hass.callWS({
          type: 'history/history_during_period', start_time: startIso, end_time: endIso,
          entity_ids: [eid], minimal_response: true, no_attributes: true, significant_changes_only: false,
        });
        raw = res && res[eid];
      } catch (e) { /* фолбек нижче на REST */ }
    }
    if (!raw) {
      const path = `history/period/${startIso}?filter_entity_id=${encodeURIComponent(eid)}&end_time=${encodeURIComponent(endIso)}&minimal_response`;
      const res = await this._hass.callApi('GET', path);
      raw = res && res[0];
    }
    if (!raw || !raw.length) return [];
    return raw.map(e => {
      const lu = e.lu != null ? e.lu * 1000 : null;
      const t = lu != null ? lu : (e.last_changed ? Date.parse(e.last_changed) : (e.last_updated ? Date.parse(e.last_updated) : null));
      const sRaw = e.s !== undefined ? e.s : e.state;
      let v = parseFloat(sRaw);
      if (!isFinite(v)) v = sRaw === 'on' ? 1 : sRaw === 'off' ? 0 : null;
      return { t, v };
    }).filter(p => p.t && p.v !== null).sort((a, b) => a.t - b.t);
  }

  _mergePoints(a, b) {
    const map = new Map();
    a.concat(b).forEach(p => map.set(p.t, p));
    return [...map.values()].sort((x, y) => x.t - y.t);
  }

  _maybeExtendLoadedRange() {
    const hs = this._hs; if (!hs) return;
    clearTimeout(hs.extendTimer);
    hs.extendTimer = setTimeout(() => {
      if (!this._hs || this._hs !== hs) return;
      const span = hs.viewEnd - hs.viewStart;
      const margin = span * 0.25;
      if (hs.viewStart < hs.dataStart + margin) {
        const chunk = Math.max(span, hs.dataEnd - hs.dataStart) || this._histPresets()['24h'];
        this._loadHistory(hs.preset, { start: hs.dataStart - chunk, end: hs.dataStart, merge: true });
      }
    }, 260);
  }

  // ── малювання ──
  _visiblePoints() {
    const hs = this._hs; if (!hs) return [];
    const { viewStart, viewEnd, points } = hs;
    let lo = -1, hi = points.length;
    for (let i = 0; i < points.length; i++) { if (points[i].t <= viewStart) lo = i; if (points[i].t >= viewEnd) { hi = i; break; } }
    const from = Math.max(0, lo), to = Math.min(points.length - 1, hi);
    return points.slice(from, to + 1);
  }

  _fmtHistVal(v) {
    const hs = this._hs; if (!hs) return '';
    if (hs.isBinary) return v >= 0.5 ? 'увімк' : 'вимк';
    const a = Math.abs(v);
    const dec = a >= 100 ? 0 : a >= 10 ? 1 : 2;
    return v.toFixed(dec).replace('.', ',') + (hs.unit ? ' ' + hs.unit : '');
  }

  _fmtAxisTime(t, span) {
    const d = new Date(t);
    if (span <= 26 * 36e5) return d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit' });
    if (span <= 8 * 864e5) return d.toLocaleDateString('uk-UA', { day: '2-digit', month: '2-digit' }) + ' ' + d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit' });
    return d.toLocaleDateString('uk-UA', { day: '2-digit', month: '2-digit' });
  }

  _drawHistory() {
    const hs = this._hs; if (!hs || !hs.ctx) return;
    const { ctx, cw, ch } = hs;
    if (!cw || !ch) return;
    ctx.clearRect(0, 0, cw, ch);
    const css = getComputedStyle(this);
    const acc = css.getPropertyValue('--acc').trim() || '#ff8a3d';
    const fg = (css.getPropertyValue('--secondary-text-color').trim()) || '#8a93a0';
    const grid = (css.getPropertyValue('--divider-color').trim()) || 'rgba(140,140,140,.3)';
    const padL = 46, padR = 10, padT = 10, padB = 24;
    const plotW = Math.max(1, cw - padL - padR), plotH = Math.max(1, ch - padT - padB);
    const vis = this._visiblePoints();
    hs.plot = { padL, padT, plotW, plotH };
    // мін/макс/серед/поточне по видимому вікну
    let min = Infinity, max = -Infinity, sum = 0, n = 0, cur = null;
    vis.forEach(p => { if (p.v < min) min = p.v; if (p.v > max) max = p.v; sum += p.v; n++; });
    if (hs.points.length) cur = hs.points[hs.points.length - 1].v;
    if (!n) { min = 0; max = hs.isBinary ? 1 : 1; }
    if (hs.isBinary) { min = 0; max = 1; }
    if (min === max) { min -= 1; max += 1; }
    const padV = (max - min) * 0.08 || 1;
    const vMin = hs.isBinary ? -0.1 : min - padV, vMax = hs.isBinary ? 1.1 : max + padV;
    hs.el.cur.textContent = cur == null ? '—' : this._fmtHistVal(cur);
    hs.el.min.textContent = n ? this._fmtHistVal(min) : '—';
    hs.el.max.textContent = n ? this._fmtHistVal(max) : '—';
    hs.el.avg.textContent = n ? this._fmtHistVal(sum / n) : '—';
    const x = t => padL + ((t - hs.viewStart) / (hs.viewEnd - hs.viewStart || 1)) * plotW;
    const y = v => padT + plotH - ((v - vMin) / (vMax - vMin || 1)) * plotH;
    // сітка + підписи значень
    ctx.font = '10px sans-serif'; ctx.fillStyle = fg; ctx.strokeStyle = grid; ctx.lineWidth = 1;
    const rows = 4;
    for (let i = 0; i <= rows; i++) {
      const vv = vMin + (vMax - vMin) * (1 - i / rows);
      const yy = padT + (plotH * i) / rows;
      ctx.beginPath(); ctx.moveTo(padL, yy + 0.5); ctx.lineTo(padL + plotW, yy + 0.5); ctx.globalAlpha = 0.35; ctx.stroke(); ctx.globalAlpha = 1;
      if (!hs.isBinary) { ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; ctx.fillText(this._fmtHistVal(vv), padL - 6, yy); }
    }
    // підписи часу знизу — адаптивна кількість міток під ширину
    const span = hs.viewEnd - hs.viewStart;
    const cols = Math.max(2, Math.min(6, Math.round(plotW / 90)));
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    for (let i = 0; i <= cols; i++) {
      const t = hs.viewStart + (span * i) / cols;
      const xx = padL + (plotW * i) / cols;
      ctx.beginPath(); ctx.moveTo(xx + 0.5, padT); ctx.lineTo(xx + 0.5, padT + plotH); ctx.globalAlpha = 0.18; ctx.stroke(); ctx.globalAlpha = 1;
      ctx.fillText(this._fmtAxisTime(t, span), Math.min(Math.max(xx, padL + 20), padL + plotW - 20), padT + plotH + 5);
    }
    // лінія + залита область (степ для binary)
    if (vis.length) {
      ctx.beginPath();
      if (hs.isBinary) {
        ctx.moveTo(x(vis[0].t), y(vis[0].v));
        for (let i = 1; i < vis.length; i++) { ctx.lineTo(x(vis[i].t), y(vis[i - 1].v)); ctx.lineTo(x(vis[i].t), y(vis[i].v)); }
        ctx.lineTo(x(hs.viewEnd), y(vis[vis.length - 1].v));
      } else {
        ctx.moveTo(x(vis[0].t), y(vis[0].v));
        for (let i = 1; i < vis.length; i++) ctx.lineTo(x(vis[i].t), y(vis[i].v));
      }
      ctx.strokeStyle = acc; ctx.lineWidth = 2.2; ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.stroke();
      // залив під лінією
      ctx.lineTo(x(vis[vis.length - 1].t), padT + plotH); ctx.lineTo(x(vis[0].t), padT + plotH); ctx.closePath();
      const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
      grad.addColorStop(0, this._alpha(acc, 0.35)); grad.addColorStop(1, this._alpha(acc, 0.02));
      ctx.fillStyle = grad; ctx.fill();
    }
    // курсор-перехрестя
    if (hs.cursor) {
      const cx = x(hs.cursor.t), cy = y(hs.cursor.v);
      ctx.beginPath(); ctx.moveTo(cx + 0.5, padT); ctx.lineTo(cx + 0.5, padT + plotH); ctx.strokeStyle = fg; ctx.globalAlpha = 0.55; ctx.lineWidth = 1; ctx.stroke(); ctx.globalAlpha = 1;
      ctx.beginPath(); ctx.arc(cx, cy, 4.5, 0, Math.PI * 2); ctx.fillStyle = acc; ctx.fill();
      ctx.lineWidth = 2; ctx.strokeStyle = '#fff'; ctx.stroke();
      const tip = hs.el.tip;
      tip.hidden = false;
      tip.textContent = `${this._fmtHistVal(hs.cursor.v)} · ${new Date(hs.cursor.t).toLocaleString('uk-UA', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}`;
      let left = cx; left = Math.min(Math.max(left, 40), cw - 40);
      tip.style.left = left + 'px';
    } else if (hs.el.tip) hs.el.tip.hidden = true;
  }

  _alpha(hex, a) {
    // приймає #rrggbb; якщо прийшло щось інше (напр. вже rgba/var) — фолбек на акцентний помаранч
    const m = /^#([0-9a-f]{6})$/i.exec(hex || '');
    if (!m) return `rgba(255,138,61,${a})`;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  _setCursorAtX(x) {
    const hs = this._hs; if (!hs || !hs.points.length) return;
    const t = hs.viewStart + (x / hs.cw) * (hs.viewEnd - hs.viewStart);
    let best = hs.points[0], bestD = Infinity;
    for (const p of hs.points) { const d = Math.abs(p.t - t); if (d < bestD) { bestD = d; best = p; } }
    hs.cursor = { t: best.t, v: best.v };
    this._drawHistory();
  }
  _hideCursor() { const hs = this._hs; if (!hs) return; hs.cursor = null; this._drawHistory(); }

  // ── жести: pointer events (тач і миша одним кодом) ──
  _histPointerDown(e) {
    const hs = this._hs; if (!hs) return;
    e.preventDefault();
    // синтетичні/деякі edge-case поінтери можуть кинути виняток на capture — не рвемо жест
    try { hs.canvas.setPointerCapture(e.pointerId); } catch (err) { /* no-op */ }
    const rect = hs.canvas.getBoundingClientRect();
    hs.pointers.set(e.pointerId, { x: e.clientX - rect.left, y: e.clientY - rect.top });
    if (hs.pointers.size === 1) {
      const p = [...hs.pointers.values()][0];
      hs.downX = p.x; hs.downY = p.y; hs.downViewStart = hs.viewStart; hs.downViewEnd = hs.viewEnd;
      hs.mode = null;
      clearTimeout(hs.holdTimer);
      // якщо палець(курсор) затримується без різкого руху — це намір курсора, не пану
      hs.holdTimer = setTimeout(() => { if (hs.mode === null) { hs.mode = 'cursor'; this._setCursorAtX(hs.downX); } }, 160);
    } else if (hs.pointers.size === 2) {
      clearTimeout(hs.holdTimer); hs.mode = 'pinch';
      const pts = [...hs.pointers.values()];
      hs.pinchStartDist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
      hs.pinchStartView = [hs.viewStart, hs.viewEnd];
      hs.pinchCenterX = (pts[0].x + pts[1].x) / 2;
      this._hideCursor();
    }
  }

  _histPointerMove(e) {
    const hs = this._hs; if (!hs || !hs.pointers.has(e.pointerId)) {
      // hover миші без натиснутої кнопки — живий курсор
      if (hs && e.pointerType === 'mouse' && e.buttons === 0) {
        const rect = hs.canvas.getBoundingClientRect();
        this._setCursorAtX(e.clientX - rect.left);
      }
      return;
    }
    e.preventDefault();
    const rect = hs.canvas.getBoundingClientRect();
    hs.pointers.set(e.pointerId, { x: e.clientX - rect.left, y: e.clientY - rect.top });
    if (!hs.cw) return; // канвас ще не отримав розмір (перший кадр до resize) — жест ігноруємо, не ділимо на 0
    if (hs.pointers.size === 2) {
      const pts = [...hs.pointers.values()];
      const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
      const cx = (pts[0].x + pts[1].x) / 2;
      const [s0, e0] = hs.pinchStartView;
      const span0 = e0 - s0;
      const anchorT = s0 + (hs.pinchCenterX / hs.cw) * span0;
      let newSpan = span0 * (hs.pinchStartDist / dist);
      const minSpan = 5 * 60 * 1000, maxSpan = 60 * 864e5;
      newSpan = Math.max(minSpan, Math.min(maxSpan, newSpan));
      const ns = anchorT - (cx / hs.cw) * newSpan;
      hs.viewStart = ns; hs.viewEnd = ns + newSpan;
      this._drawHistory(); this._maybeExtendLoadedRange();
      return;
    }
    const p = [...hs.pointers.values()][0];
    const dx = p.x - hs.downX, dy = p.y - hs.downY;
    if (hs.mode === null) {
      if (Math.abs(dx) > 7 || Math.abs(dy) > 7) { clearTimeout(hs.holdTimer); hs.mode = 'pan'; this._hideCursor(); }
    }
    if (hs.mode === 'pan') {
      const span = hs.downViewEnd - hs.downViewStart;
      const dt = -(dx / hs.cw) * span;
      hs.viewStart = hs.downViewStart + dt; hs.viewEnd = hs.downViewEnd + dt;
      this._drawHistory(); this._maybeExtendLoadedRange();
    } else if (hs.mode === 'cursor') {
      this._setCursorAtX(p.x);
    }
  }

  _histPointerUp(e) {
    const hs = this._hs; if (!hs) return;
    const wasTwo = hs.pointers.size === 2;
    hs.pointers.delete(e.pointerId);
    clearTimeout(hs.holdTimer);
    if (wasTwo && hs.pointers.size === 1) {
      // одна рука лишилась після щипка — перебазувати пан від поточної позиції, без стрибка;
      // і НЕ трактувати відпускання цього останнього пальця як тап (інакше щипок лишає
      // по собі зайвий курсор-перехрестя)
      const p = [...hs.pointers.values()][0];
      hs.downX = p.x; hs.downY = p.y; hs.downViewStart = hs.viewStart; hs.downViewEnd = hs.viewEnd; hs.mode = null;
      hs.suppressNextTap = true;
      return;
    }
    if (hs.pointers.size === 0) {
      if (hs.mode === null && !hs.suppressNextTap) {
        // короткий тап без руху → показати курсор; подвійний тап → скинути зум
        const now = Date.now();
        if (hs.lastTapT && now - hs.lastTapT < 320 && Math.abs(hs.downX - hs.lastTapX) < 24) {
          this._loadHistory(hs.preset); this._hideCursor(); hs.lastTapT = 0;
        } else {
          this._setCursorAtX(hs.downX); hs.lastTapT = now; hs.lastTapX = hs.downX;
        }
      }
      hs.mode = null; hs.suppressNextTap = false;
    }
  }

  _histWheel(e) {
    const hs = this._hs; if (!hs || !hs.cw) return;
    e.preventDefault();
    const rect = hs.canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const factor = e.deltaY > 0 ? 1.18 : 1 / 1.18;
    const span = hs.viewEnd - hs.viewStart;
    const anchorT = hs.viewStart + (x / hs.cw) * span;
    let newSpan = Math.max(5 * 60 * 1000, Math.min(60 * 864e5, span * factor));
    const ns = anchorT - (x / hs.cw) * newSpan;
    hs.viewStart = ns; hs.viewEnd = ns + newSpan;
    this._drawHistory(); this._maybeExtendLoadedRange();
  }

  _histCss() {
    return `
      .hovl{position:fixed;inset:0;z-index:40;background:rgba(0,0,0,0);backdrop-filter:blur(0px);
        transition:background .2s,backdrop-filter .2s;display:flex;}
      .hovl.show{background:rgba(0,0,0,.55);backdrop-filter:blur(6px);}
      .hpanel{margin:auto;width:min(920px,96vw);height:min(640px,92vh);display:flex;flex-direction:column;
        background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:18px;
        box-shadow:0 20px 60px rgba(0,0,0,.5);padding:10px 14px 14px;
        transform:scale(.94);opacity:0;transition:transform .22s cubic-bezier(.2,.9,.2,1),opacity .18s;}
      .hovl.show .hpanel{transform:scale(1);opacity:1;}
      .hhdr{display:flex;align-items:center;gap:10px;padding:6px 2px 10px;touch-action:none;cursor:grab;}
      .hname{font-size:1.05rem;font-weight:800;color:var(--primary-text-color);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
      .hclose{border:none;background:var(--secondary-background-color);color:var(--secondary-text-color);width:34px;height:34px;
        border-radius:50%;cursor:pointer;flex:none;display:inline-flex;align-items:center;justify-content:center;}
      .hstats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px;}
      .hstat{display:flex;flex-direction:column;align-items:center;gap:2px;padding:8px 4px;border-radius:12px;background:var(--secondary-background-color);}
      .hsl{font-size:.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--secondary-text-color);}
      .hsv{font-size:1rem;font-weight:800;color:var(--primary-text-color);font-variant-numeric:tabular-nums;}
      .hcanwrap{position:relative;flex:1;min-height:0;border-radius:14px;overflow:hidden;background:var(--secondary-background-color);}
      .hcanvas{width:100%;height:100%;display:block;touch-action:none;cursor:crosshair;}
      .htip{position:absolute;top:8px;pointer-events:none;background:var(--card-background-color);
        border:1px solid var(--divider-color);border-radius:10px;padding:5px 9px;font-size:.76rem;font-weight:700;
        color:var(--primary-text-color);transform:translateX(-50%);white-space:nowrap;box-shadow:0 4px 14px rgba(0,0,0,.3);}
      .hloading{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:.85rem;font-weight:700;
        color:var(--secondary-text-color);background:color-mix(in srgb,var(--secondary-background-color) 65%,transparent);}
      .hloading[hidden]{display:none;}
      .hpresets{display:flex;gap:8px;margin-top:10px;}
      .hpresets button{flex:1;padding:9px;border-radius:12px;border:1px solid var(--divider-color);background:var(--secondary-background-color);
        color:var(--primary-text-color);font-weight:700;font-size:.85rem;cursor:pointer;}
      .hpresets button.on{background:var(--acc);color:#fff;border-color:transparent;}
      @media(max-width:520px){.hpanel{width:100vw;height:100vh;border-radius:0;padding:8px 8px 10px;}}
    `;
  }

  _popupCss() {
    return `
      .ovl{position:fixed;inset:0;z-index:30;display:flex;align-items:flex-end;justify-content:center;
        background:rgba(0,0,0,0);backdrop-filter:blur(0px);transition:background .22s,backdrop-filter .22s;}
      .ovl.show{background:rgba(0,0,0,.45);backdrop-filter:blur(7px);}
      .panel{width:min(440px,94vw);max-height:86vh;overflow:auto;margin:0 0 max(env(safe-area-inset-bottom),10px);
        background:color-mix(in srgb,var(--card-background-color) 86%,transparent);
        border:1px solid var(--divider-color);border-radius:24px 24px 18px 18px;
        box-shadow:0 -10px 40px rgba(0,0,0,.45);padding:6px 16px 16px;
        transform:translateY(40px) scale(.96);opacity:0;transition:transform .26s cubic-bezier(.2,.9,.2,1),opacity .2s;}
      .ovl.show .panel{transform:translateY(0) scale(1);opacity:1;}
      @media(min-width:560px){.ovl{align-items:center;}.panel{border-radius:22px;margin:0;}}
      .ph{position:sticky;top:0;display:flex;align-items:center;gap:9px;padding:12px 2px 12px;font-size:1.1rem;font-weight:800;color:var(--primary-text-color);
        background:linear-gradient(var(--card-background-color),transparent);}
      .ph .pclose{margin-left:auto;border:none;background:var(--secondary-background-color);color:var(--secondary-text-color);width:32px;height:32px;border-radius:50%;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;}
      ha-icon.phi{--mdc-icon-size:22px;width:22px;height:22px;color:var(--acc);}
      ha-icon.wic{--mdc-icon-size:18px;width:18px;height:18px;}
      .pbody{display:flex;flex-direction:column;gap:13px;padding:4px 0;}
      .actbtn{width:100%;display:flex;align-items:center;justify-content:center;gap:8px;
        padding:11px 14px;border:0;border-radius:14px;cursor:pointer;font:inherit;font-weight:600;
        color:#fff;background:var(--acc);opacity:.92;transition:transform .15s,opacity .2s;}
      .actbtn:active{transform:scale(.97);}
      .actbtn.run{background:#e0a800;animation:pulse 1.6s ease-in-out infinite;}
      @keyframes pulse{0%,100%{opacity:.85}50%{opacity:1}}
      .acthint{font-size:.72rem;opacity:.6;padding:6px 4px 0;line-height:1.35;}
      .pc{opacity:0;transform:translateY(10px);animation:pcin .34s forwards;animation-delay:var(--d,0ms);}
      @keyframes pcin{to{opacity:1;transform:none;}}
      .pcl{display:flex;align-items:center;font-size:.86rem;font-weight:700;color:var(--secondary-text-color);margin-bottom:7px;}
      .pcl b{margin-left:auto;font-size:.95rem;font-weight:800;font-variant-numeric:tabular-nums;}
      .opts{display:flex;flex-wrap:wrap;gap:7px;}
      .opt{flex:1 1 auto;min-width:78px;padding:9px 10px;border-radius:12px;border:1px solid var(--divider-color);
        background:var(--secondary-background-color);color:var(--primary-text-color);font-size:.82rem;font-weight:700;cursor:pointer;transition:.15s;}
      .opt.on{background:var(--acc);color:#fff;border-color:transparent;box-shadow:0 4px 14px color-mix(in srgb,var(--acc) 45%,transparent);}
      .swrow{display:flex;align-items:center;cursor:pointer;}
      .swrow .pcl{margin-bottom:0;}
      .sw{margin-left:auto;width:48px;height:28px;border-radius:16px;background:var(--divider-color);position:relative;transition:.2s;flex:none;}
      .sw .kn{position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#fff;transition:.2s;box-shadow:0 1px 3px rgba(0,0,0,.35);}
      .swrow.on .sw{background:var(--acc);}
      .swrow.on .sw .kn{left:23px;}
      .sl{-webkit-appearance:none;appearance:none;width:100%;height:7px;border-radius:5px;background:var(--secondary-background-color);outline:none;}
      .sl::-webkit-slider-thumb{-webkit-appearance:none;width:22px;height:22px;border-radius:50%;background:var(--c,#ff8a3d);cursor:pointer;box-shadow:0 2px 6px rgba(0,0,0,.4);border:2px solid var(--card-background-color);}
      .sl::-moz-range-thumb{width:22px;height:22px;border-radius:50%;background:var(--c,#ff8a3d);cursor:pointer;border:2px solid var(--card-background-color);}
      .pfoot{padding-top:6px;}
      .moreinfo{width:100%;display:flex;align-items:center;justify-content:center;gap:7px;padding:11px;border-radius:13px;
        border:1px dashed var(--divider-color);background:transparent;color:var(--secondary-text-color);font-size:.85rem;font-weight:700;cursor:pointer;}
      .moreinfo:hover{color:var(--primary-text-color);border-color:var(--acc);}
    `;
  }

  _css() {
    return `<style>
      :host{--acc:#ff8a3d;}
      /* глушник анімацій для слабких GPU-кіосків (config animate:false) */
      :host([na]) *{animation:none!important;}
      /* фон/радіус/тінь/blur НЕ перевизначаємо — їх малює нативний ha-card з активної теми */
      ha-card{padding:16px;position:relative;overflow:hidden;container-type:inline-size;container-name:dc;}
      ha-icon.hi{--mdc-icon-size:20px;width:20px;height:20px;}
      ha-icon.ht{--mdc-icon-size:24px;width:24px;height:24px;color:var(--acc);vertical-align:0;position:relative;top:-1px;}
      ha-icon.wic{--mdc-icon-size:16px;width:16px;height:16px;vertical-align:-3px;}
      /* таб-бар «Стан | Графіки» розгорнутого вигляду */
      .tabs{display:flex;gap:4px;margin:0 0 12px;background:var(--secondary-background-color);border-radius:14px;padding:3px;}
      .tabs .tab{flex:1;padding:8px 10px;border:none;background:transparent;border-radius:11px;color:var(--secondary-text-color);
        font-weight:700;font-size:.82rem;cursor:pointer;transition:background .15s,color .15s;font-family:inherit;}
      .tabs .tab.on{background:var(--card-background-color);color:var(--primary-text-color);box-shadow:0 2px 8px rgba(0,0,0,.18);}
      /* тонкий груп-роздільник між блоками (Стан) */
      .gsep{display:flex;align-items:center;gap:8px;margin:14px 2px 9px;}
      .gsep .gsepline{flex:1;height:1px;background:var(--divider-color);opacity:.6;}
      .gsep .gsepl{display:flex;align-items:center;gap:4px;font-size:.6rem;font-weight:700;text-transform:uppercase;
        letter-spacing:.05em;color:var(--secondary-text-color);opacity:.7;white-space:nowrap;}
      ha-icon.gsi{--mdc-icon-size:12px;width:12px;height:12px;}
      .hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;gap:8px;}
      .ttl{font-size:var(--ts,1.25rem);font-weight:700;color:var(--primary-text-color);display:flex;align-items:center;gap:7px;line-height:1.1;min-width:0;}
      .hbtns{display:flex;align-items:center;gap:7px;flex:none;}
      .wchip{display:inline-flex;align-items:center;background:var(--secondary-background-color);border-radius:14px;padding:3px 9px;flex:none;}
      .gear{display:inline-flex;align-items:center;justify-content:center;background:var(--secondary-background-color);color:var(--acc);border:none;border-radius:14px;padding:5px 8px;cursor:pointer;transition:transform .12s;}
      .gear:active{transform:scale(.9);}
      /* «Режим роботи» — банер реального стану + чотирипозиційний селектор нагорі розгорнутої картки */
      .critbar{display:flex;flex-direction:column;gap:10px;padding:12px 14px;margin:0 0 10px;border-radius:14px;
        background:var(--secondary-background-color);border:1px solid var(--divider-color);transition:background .2s,border-color .2s,box-shadow .2s;}
      .crithead{display:flex;align-items:center;gap:9px;flex:1;min-width:0;}
      .crit-ic{flex:none;font-size:1.15rem;line-height:1;}
      .critt{display:flex;flex-direction:column;gap:2px;min-width:0;}
      .critbar b{font-size:.86rem;font-weight:800;color:var(--primary-text-color);line-height:1.2;}
      .critbar .crits{font-size:.72rem;font-weight:600;color:var(--secondary-text-color);}
      .critw{color:#ff3b30;font-weight:800;margin-left:4px;}
      /* ручний форс — акцентний колір теми */
      .cb-manual{background:color-mix(in srgb,var(--acc) 16%,var(--card-background-color));border-color:var(--acc);box-shadow:0 0 0 1px color-mix(in srgb,var(--acc) 35%,transparent) inset;}
      /* генератор — теплий помаранч, тимчасовий стан */
      .cb-gen{background:color-mix(in srgb,#ff9f0a 16%,var(--card-background-color));border-color:#ff9f0a;}
      .cb-gen b{color:#ff9f0a;}
      /* авто-екстрений день — червоне, щоб НЕ виникало спокуси «увімкнути» тумблер */
      .cb-auto{background:color-mix(in srgb,#ff3b30 16%,var(--card-background-color));border-color:#ff3b30;animation:critpulse 2.2s ease-in-out infinite;}
      .cb-auto b{color:#ff3b30;}
      /* блекаут — найурочистіше: мережі нема прямо зараз */
      .cb-blackout{background:color-mix(in srgb,#ff3b30 24%,var(--card-background-color));border-color:#ff3b30;animation:critpulse 1.3s ease-in-out infinite;}
      .cb-blackout b{color:#ff3b30;}
      /* еко — тихий, сірий, не привертає уваги */
      .cb-eco{opacity:.72;}
      .cb-eco b{color:var(--secondary-text-color);font-weight:700;}
      /* балансування — нейтрально-бірюзовий, не тривожний, але й не «тихий» як еко */
      .cb-balancing{background:color-mix(in srgb,#30d5c8 16%,var(--card-background-color));border-color:#30d5c8;}
      .cb-balancing b{color:#30d5c8;}
      /* v49: еко+ — можливі (не точні) відключення: жовтий/бурштиновий, помітний, але не тривожний як авто-критичний */
      .cb-ecoplus{background:color-mix(in srgb,#ffcc00 18%,var(--card-background-color));border-color:#ffcc00;}
      .cb-ecoplus b{color:#c79400;}
      @keyframes critpulse{
        0%,100%{box-shadow:0 0 0 1px color-mix(in srgb,#ff3b30 45%,transparent),0 0 6px color-mix(in srgb,#ff3b30 30%,transparent);}
        50%{box-shadow:0 0 0 1px color-mix(in srgb,#ff3b30 85%,transparent),0 0 16px color-mix(in srgb,#ff3b30 65%,transparent);}
      }
      /* чотирипозиційний сегментований селектор Еко|Авто|Критичний|Балансування — сітка 4×1,
         на вузьких картках (<400px, @container dc нижче) перемикається на 2×2 зі скороченими
         підписами (.mlf сховано/.mls показано), щоб не ламати верстку на телефоні */
      .modesel{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;}
      .modesel .mseg{padding:8px 4px;border-radius:11px;border:1px solid var(--divider-color);
        background:var(--card-background-color);color:var(--primary-text-color);font-size:.78rem;font-weight:700;
        cursor:pointer;font-family:inherit;transition:background .15s,color .15s,transform .1s;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
      .modesel .mseg:active{transform:scale(.96);}
      .modesel .mseg.on{background:var(--acc);color:#fff;border-color:transparent;box-shadow:0 3px 10px color-mix(in srgb,var(--acc) 45%,transparent);}
      .modesel .mls{display:none;}
      .mcrit{flex:none;font-size:.95rem;line-height:1;filter:drop-shadow(0 0 3px color-mix(in srgb,var(--acc) 60%,transparent));}
      .mcrit-auto,.mcrit-blackout{filter:drop-shadow(0 0 4px color-mix(in srgb,#ff3b30 70%,transparent));}
      .mcrit-gen{filter:drop-shadow(0 0 3px color-mix(in srgb,#ff9f0a 60%,transparent));}
      .mcrit-balancing{filter:drop-shadow(0 0 3px color-mix(in srgb,#30d5c8 60%,transparent));}
      .mcrit-ecoplus{filter:drop-shadow(0 0 3px color-mix(in srgb,#ffcc00 70%,transparent));}
      .diag{width:100%;max-width:340px;display:block;margin:0 auto;aspect-ratio:300/338;}
      .pdot{filter:drop-shadow(0 0 3px currentColor);}
      .inv.dim{opacity:.45;}
      .ledon{animation:blink 2.2s steps(66) infinite;}
      @keyframes blink{0%,100%{opacity:1}50%{opacity:.35}}
      .boltw{animation:blink 1.1s steps(33) infinite;}
      .node.act circle{filter:drop-shadow(0 0 5px color-mix(in srgb,currentColor 40%,transparent));}
      .bfill{transition:y .6s cubic-bezier(.2,.8,.2,1),height .6s cubic-bezier(.2,.8,.2,1),fill .3s;}
      /* тап-по-значенню → графік історії: ледь помітна афорданс без захаращення */
      [data-hist]{cursor:pointer;border-radius:6px;transition:background .12s;}
      [data-hist]:active{background:color-mix(in srgb,var(--acc) 16%,transparent);}
      .drow[data-hist]{padding:2px 4px;margin:-2px -4px;}
      .chip[data-hist]{cursor:pointer;}
      .cell[data-hist]{cursor:pointer;}
      /* flow pills */
      .pills{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:6px 0 4px;}
      .pill{display:flex;align-items:center;gap:9px;padding:9px 11px;border-radius:14px;background:var(--secondary-background-color);border:1px solid var(--divider-color);cursor:pointer;transition:transform .1s;}
      .pill:active{transform:scale(.97);}
      ha-icon.pi{--mdc-icon-size:20px;width:20px;height:20px;color:var(--c);flex:none;}
      .pt{display:flex;flex-direction:column;min-width:0;line-height:1.15;}
      .pl{font-size:.66rem;font-weight:700;color:var(--secondary-text-color);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
      .pv{font-size:.92rem;font-weight:800;color:var(--c);}
      /* detail sections */
      .sect{display:flex;align-items:center;gap:6px;font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--secondary-text-color);margin:13px 2px 7px;}
      ha-icon.si{--mdc-icon-size:15px;width:15px;height:15px;color:var(--acc);}
      .cells{display:flex;gap:3px;align-items:flex-end;padding:6px 10px 2px;}
      .cell{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;}
      .cbar{width:100%;height:38px;border-radius:4px;background:rgba(127,127,127,.16);
            display:flex;align-items:flex-end;overflow:hidden;position:relative;}
      .cbar i{display:block;width:100%;border-radius:4px;transition:height .4s ease;}
      .cnum{font-size:.62rem;opacity:.55;line-height:1;}
      /* живий індикатор активного балансування комірки (JBD скидає надлишок заряду):
         бурштиновий пульсуючий glow навколо бару + «хвиля» згори вниз усередині —
         суто CSS @keyframes, без JS-таймерів; вимикається animate:false (:host([na]))
         і системним prefers-reduced-motion (нижче). */
      .cell.bal .cnum{color:#ff9f0a;font-weight:800;}
      .cbar.bal{animation:balglow 1.6s ease-in-out infinite;}
      @keyframes balglow{
        0%,100%{box-shadow:0 0 0 1px color-mix(in srgb,#ff9f0a 45%,transparent),0 0 6px color-mix(in srgb,#ff9f0a 40%,transparent);}
        50%{box-shadow:0 0 0 1px color-mix(in srgb,#ff9f0a 80%,transparent),0 0 14px color-mix(in srgb,#ff9f0a 75%,transparent);}
      }
      .cwave{position:absolute;left:0;right:0;top:-60%;height:60%;pointer-events:none;
        background:linear-gradient(180deg,transparent,rgba(255,255,255,.65) 55%,transparent);
        animation:cwave 1.1s linear infinite;}
      @keyframes cwave{0%{transform:translateY(0);}100%{transform:translateY(220%);}}
      .balbadge{display:inline-flex;align-items:center;gap:3px;font-size:.66rem;font-weight:700;color:#ff9f0a;animation:pulse 1.6s ease-in-out infinite;}
      ha-icon.bi{--mdc-icon-size:13px;width:13px;height:13px;color:#ff9f0a;}
      @media (prefers-reduced-motion: reduce){
        .cbar.bal{animation:none;box-shadow:0 0 0 1px color-mix(in srgb,#ff9f0a 70%,transparent);}
        .cwave{display:none;}
        .balbadge{animation:none;}
        .cb-auto,.cb-blackout{animation:none;box-shadow:0 0 0 1px color-mix(in srgb,#ff3b30 70%,transparent);}
      }
      .dgrid{display:grid;grid-template-columns:1fr 1fr;gap:6px 12px;}
      .drow{display:flex;align-items:flex-start;gap:7px;min-width:0;}
      ha-icon.di{--mdc-icon-size:16px;width:16px;height:16px;color:var(--secondary-text-color);flex:none;}
      .dl{font-size:.78rem;color:var(--secondary-text-color);flex:none;}
      .dv{font-size:.82rem;font-weight:700;color:var(--primary-text-color);margin-left:auto;text-align:right;white-space:normal;overflow-wrap:anywhere;}
      /* today chips */
      .chips{display:flex;flex-wrap:wrap;gap:7px;}
      .chip{display:inline-flex;align-items:center;gap:5px;padding:6px 10px;border-radius:13px;background:var(--secondary-background-color);font-size:.78rem;font-weight:700;color:var(--primary-text-color);}
      ha-icon.ci{--mdc-icon-size:15px;width:15px;height:15px;color:var(--acc);}

      /* ── mini layout ── */
      ha-card.mini{padding:10px 14px 12px;}
      ha-card.mini.moff{opacity:.55;}
      .mrow{display:flex;align-items:center;gap:13px;min-height:48px;}
      .mbatt{display:flex;align-items:center;gap:9px;cursor:pointer;flex:none;}
      .mbsvg{width:30px;height:46px;flex:none;display:block;}
      .mpct{display:flex;flex-direction:column;gap:2px;line-height:1.05;}
      .mpct b{font-size:1.3rem;font-weight:800;font-variant-numeric:tabular-nums;}
      .mpct b i{font-style:normal;font-size:.75rem;font-weight:700;opacity:.75;margin-left:1px;}
      .mstat{display:flex;flex-direction:column;align-items:center;text-align:center;gap:3px;line-height:1.1;cursor:pointer;min-width:0;flex:none;transition:transform .1s;}
      .mstat:active{transform:scale(.95);}
      .msv{font-size:.98rem;font-weight:800;white-space:nowrap;font-variant-numeric:tabular-nums;}
      .msl{display:inline-flex;align-items:center;gap:4px;font-size:.64rem;font-weight:700;color:var(--secondary-text-color);white-space:nowrap;}
      /* лінії-потоки міні-схеми */
      /* contain:paint — перемальовка замкнена в межах лінії (22×12px), а не всієї картки:
         без цього кожен кадр анімації інвалідовував великий регіон і на слабкому GPU
         (планшет-кіоск) RenderThread їв ~93% ядра ЦІЛОДОБОВО. */
      .mflow{flex:1 1 22px;min-width:14px;height:12px;position:relative;overflow:visible;contain:paint;}
      .mflow::before{content:'';position:absolute;left:0;right:0;top:50%;height:2px;margin-top:-1px;border-radius:1px;background:var(--fc,var(--divider-color));opacity:.3;}
      .mflow.dead::before{background:repeating-linear-gradient(90deg,#ff3b30 0 4px,transparent 4px 8px);opacity:.55;}
      /* крапка = ::before на повнорозмірному i, рух через transform (композитор, без layout-джиттера) */
      /* steps(N) = тривалість×30 → рівно 30 кадрів/с замість 60: між кроками transform не
         змінюється, тож браузеру нема чого перемальовувати. На лінії ~46px крок <1px —
         око різниці не бачить, а рендер-навантаження вдвічі менше (планшет-кіоск 24/7). */
      .mflow i{position:absolute;inset:0;animation:fdot 1.6s steps(48) infinite;will-change:transform,opacity;transform:translateZ(0);}
      /* світіння — радіальним градієнтом, НЕ box-shadow: блюр-тінь перемальовується щокадру
         (paint) і не дає крапці стати композиторним шаром; градієнт растеризується один раз. */
      .mflow i::before{content:'';position:absolute;left:-5.5px;top:50%;width:11px;height:11px;margin-top:-5.5px;border-radius:50%;
        background:radial-gradient(circle,var(--fc) 0 24%,color-mix(in srgb,var(--fc) 55%,transparent) 42%,transparent 70%);}
      .mflow.rev i{animation-name:fdotr;}
      @keyframes fdot{0%{transform:translateX(0);opacity:0}12%{opacity:1}88%{opacity:1}100%{transform:translateX(100%);opacity:0}}
      @keyframes fdotr{0%{transform:translateX(100%);opacity:0}12%{opacity:1}88%{opacity:1}100%{transform:translateX(0);opacity:0}}
      /* шеврони заряду/розряду всередині батарейки */
      .chev{fill:none;stroke:#fff;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;opacity:0;}
      .chev.up{animation:chvup 1.5s steps(45) infinite;}
      .chev.dn{animation:chvdn 1.5s steps(45) infinite;}
      @keyframes chvup{0%{transform:translateY(11px);opacity:0}25%{opacity:.95}100%{transform:translateY(-15px);opacity:0}}
      @keyframes chvdn{0%{transform:translateY(-13px);opacity:0}25%{opacity:.95}100%{transform:translateY(13px);opacity:0}}
      ha-card.pop{animation:cardin .28s cubic-bezier(.2,.9,.2,1);}
      @keyframes cardin{from{opacity:.4;transform:scale(.97);}to{opacity:1;transform:none;}}
      ha-icon.mi{--mdc-icon-size:13px;width:13px;height:13px;flex:none;}
      .malert{color:#ff3b30;flex:none;cursor:pointer;animation:blink 1.4s steps(42) infinite;}
      .mtime{display:flex;align-items:center;gap:6px;margin-top:9px;padding-top:8px;border-top:1px solid var(--divider-color);font-size:.74rem;cursor:pointer;animation:pcin .3s;}
      .mtime .mtl{color:var(--secondary-text-color);font-weight:700;}
      .mtime b{margin-left:auto;font-weight:800;font-variant-numeric:tabular-nums;white-space:nowrap;}
      @keyframes pcin{from{opacity:0;transform:translateY(4px);}to{opacity:1;transform:none;}}
      .msocbar{position:absolute;left:0;bottom:0;height:3px;border-radius:0 2px 2px 0;transition:width .6s cubic-bezier(.2,.8,.2,1);box-shadow:0 0 7px currentColor;}
      .mbms{display:flex;justify-content:center;gap:14px;margin-top:8px;padding-top:7px;border-top:1px solid var(--divider-color);font-size:.74rem;color:var(--secondary-text-color);font-variant-numeric:tabular-nums;}
      .mbms .mbp{display:flex;align-items:center;gap:4px;white-space:nowrap;}
      .mbms b{font-weight:800;}
      .sect .pstate{margin-left:auto;font-size:.72rem;font-weight:700;}
      @container dc (max-width: 340px){
        .mrow{gap:9px;}
        .mpct b{font-size:1.15rem;}
        .msv{font-size:.88rem;}
        .msl{font-size:.58rem;}
      }
      /* не найширша картка (<520px, звичайний розмір на дашборді й телефон) — селектор
         режиму на 2×2 зі скороченими підписами (Еко/Авто/Крит/Баланс): живий замір показав,
         що повне слово «Балансування» в 1/4 ряду не влазить навіть на ~460px картці */
      @container dc (max-width: 520px){
        .modesel{grid-template-columns:repeat(2,1fr);}
        .modesel .mlf{display:none;}
        .modesel .mls{display:inline;}
      }
      @container dc (max-width: 260px){
        ha-card.mini{padding:9px 11px 11px;}
        .mbsvg{width:26px;height:40px;}
        .msl{gap:3px;}
        .mstat[data-node="gen"]{display:none;}
      }

      @container dc (max-width: 300px){
        .ttl{font-size:calc(var(--ts,1.25rem) - .15rem);}
        .diag{max-width:260px;}
        .pills{gap:6px;} .pill{padding:8px 9px;gap:7px;}
        .pv{font-size:.84rem;} .pl{font-size:.62rem;}
        .cells{display:flex;gap:3px;align-items:flex-end;padding:6px 10px 2px;}
      .cell{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;}
      .cbar{width:100%;height:38px;border-radius:4px;background:rgba(127,127,127,.16);
            display:flex;align-items:flex-end;overflow:hidden;}
      .cbar i{display:block;width:100%;border-radius:4px;transition:height .4s ease;}
      .cnum{font-size:.62rem;opacity:.55;line-height:1;}
      .dgrid{gap:5px 10px;} .dl,.dv{font-size:.74rem;}
      }
      @container dc (max-width: 232px){
        ha-card{padding:11px;}
        .diag{max-width:210px;}
        .pills{grid-template-columns:1fr;} .cells{display:flex;gap:3px;align-items:flex-end;padding:6px 10px 2px;}
      .cell{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;}
      .cbar{width:100%;height:38px;border-radius:4px;background:rgba(127,127,127,.16);
            display:flex;align-items:flex-end;overflow:hidden;}
      .cbar i{display:block;width:100%;border-radius:4px;transition:height .4s ease;}
      .cnum{font-size:.62rem;opacity:.55;line-height:1;}
      .dgrid{grid-template-columns:1fr;}
        .ttl{font-size:calc(var(--ts,1.25rem) - .3rem);}
      }
      @container dc (max-width: 180px){
        ha-card{padding:9px;}
        .diag{max-width:170px;}
        .hdr{margin-bottom:3px;} .gear{padding:4px 6px;}
        .pill{padding:7px 8px;gap:6px;border-radius:12px;}
        .pv{font-size:.78rem;} .pl{font-size:.58rem;}
        .chips{gap:5px;} .chip{padding:5px 8px;font-size:.7rem;}
        .sect{margin:9px 2px 5px;}
      }
    </style>`;
  }
}
customElements.define('deye-card', DeyeCard);

class DeyeCardEditor extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: 'open' }); this._config = {}; }
  setConfig(c) { this._config = Object.assign({}, c); this._render(); }
  set hass(h) { this._hass = h; if (!this._built) this._render(); }

  _emit() { this.dispatchEvent(new CustomEvent('config-changed', { detail: { config: this._config }, bubbles: true, composed: true })); }
  _set(key, val) {
    const c = Object.assign({}, this._config);
    if (val === '' || val == null) delete c[key]; else c[key] = val;
    this._config = c; this._emit();
  }
  _setColor(key, val) {
    const colors = Object.assign({}, this._config.colors || {});
    const def = DeyeCard.DEFAULT_COLORS[key];
    if (!val || val.toLowerCase() === def.toLowerCase()) delete colors[key]; else colors[key] = val;
    const c = Object.assign({}, this._config);
    if (Object.keys(colors).length) c.colors = colors; else delete c.colors;
    this._config = c; this._emit();
  }
  _resetAll() {
    const keep = this._config.prefix ? { prefix: this._config.prefix } : {};
    this._config = keep; this._emit(); this._render();
  }

  _render() {
    this._built = true;
    const cfg = this._config || {};
    const cols = cfg.colors || {};
    const D = DeyeCard.DEFAULT_COLORS;
    const labels = { grid: 'Мережа', generator: 'Генератор', battery: 'Батарея', house: 'Будинок', accent: 'Акцент (інвертор)' };
    const colorRows = Object.keys(D).map(k => {
      const cur = cols[k] || D[k];
      return `<div class="row colorrow">
        <span class="cdot" style="background:${cur}"></span>
        <label>${labels[k]}</label>
        <input type="color" data-ckey="${k}" value="${cur}">
        <button class="rst" data-reset="${k}" title="Скинути">↺</button>
      </div>`;
    }).join('');
    const sc = cfg.scale != null ? cfg.scale : (cfg.size != null ? cfg.size : 1);
    const layout = ['vertical', 'mini'].includes(cfg.layout) ? cfg.layout : 'hub';
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;}
      .form{display:flex;flex-direction:column;gap:14px;padding:4px 2px;}
      .row{display:flex;align-items:center;gap:10px;}
      .row label{flex:1;color:var(--primary-text-color);font-size:.95rem;}
      input[type=text],input[type=number]{flex:1;padding:8px 10px;border-radius:8px;border:1px solid var(--divider-color,#ccc);background:var(--card-background-color,#fff);color:var(--primary-text-color);font-size:.95rem;}
      input[type=range]{flex:1;}
      .seg{display:flex;gap:6px;flex:1;}
      .seg button{flex:1;padding:8px;border-radius:9px;border:1px solid var(--divider-color);background:var(--card-background-color);color:var(--primary-text-color);cursor:pointer;font-size:.9rem;font-weight:600;}
      .seg button.on{background:var(--primary-color,#ff8a3d);color:#fff;border-color:transparent;}
      .sect{font-weight:700;color:var(--primary-text-color);margin-top:6px;font-size:.9rem;letter-spacing:.3px;}
      .hint{color:var(--secondary-text-color);font-size:.8rem;margin-top:-6px;}
      .colorrow input[type=color]{width:42px;height:30px;border:none;background:none;padding:0;cursor:pointer;}
      .cdot{width:16px;height:16px;border-radius:50%;flex:none;box-shadow:0 0 0 1px rgba(0,0,0,.15) inset;}
      .scaleval{min-width:46px;text-align:right;color:var(--secondary-text-color);font-variant-numeric:tabular-nums;}
      .rst{border:none;background:var(--secondary-background-color,#eee);color:var(--secondary-text-color);border-radius:7px;width:28px;height:28px;cursor:pointer;font-size:15px;}
      .rst:hover{color:var(--primary-text-color);}
      .resetall{margin-top:4px;padding:10px;border:1px solid var(--divider-color);border-radius:10px;background:var(--secondary-background-color);color:var(--primary-text-color);cursor:pointer;font-size:.9rem;font-weight:700;display:flex;align-items:center;justify-content:center;gap:8px;}
      .resetall:hover{border-color:var(--error-color);color:var(--error-color);}
    </style>
    <div class="form">
      <div class="row"><label>Префікс сутностей</label><input type="text" id="f-prefix" value="${cfg.prefix || ''}" placeholder="inverter_deye"></div>
      <div class="row"><label>Назва (необов'язково)</label><input type="text" id="f-title" value="${cfg.title || ''}" placeholder="Інвертор Deye"></div>
      <div class="row"><label>Компонування</label>
        <div class="seg">
          <button data-layout="hub" class="${layout === 'hub' ? 'on' : ''}">Хаб</button>
          <button data-layout="vertical" class="${layout === 'vertical' ? 'on' : ''}">Вертикальний</button>
          <button data-layout="mini" class="${layout === 'mini' ? 'on' : ''}">Міні</button>
        </div>
      </div>
      <div class="row"><label>Масштаб</label><input type="range" id="f-scale" min="0.5" max="1.5" step="0.05" value="${sc}"><span class="scaleval" id="scaleval">${(+sc).toFixed(2)}×</span></div>
      <div class="row"><label>Розмір заголовка</label><input type="range" id="f-tsize" min="0.9" max="2" step="0.05" value="${cfg.title_size || 1.25}"><span class="scaleval" id="tsizeval">${(+(cfg.title_size || 1.25)).toFixed(2)}rem</span></div>
      <div class="row"><label>Анімація</label>
        <div class="seg">
          <button data-anim="on" class="${cfg.animate === false ? '' : 'on'}">Увімкнена</button>
          <button data-anim="off" class="${cfg.animate === false ? 'on' : ''}">Екорежим</button>
        </div>
      </div>
      <div class="hint">Екорежим глушить анімації потоку/шевронів — для слабких GPU планшетів-кіосків (24/7).</div>
      <div class="sect">Кольори</div>
      <div class="hint">Кольори вузлів і потоків. ↺ — повернути дефолт.</div>
      ${colorRows}
      <button class="resetall" id="resetall">↺ Скинути все до замовчувань</button>
    </div>`;

    const $ = s => this.shadowRoot.querySelector(s);
    $('#f-prefix').addEventListener('change', e => this._set('prefix', e.target.value.trim()));
    $('#f-title').addEventListener('change', e => this._set('title', e.target.value.trim()));
    this.shadowRoot.querySelectorAll('[data-layout]').forEach(b =>
      b.addEventListener('click', () => { this._set('layout', b.dataset.layout); this._render(); }));
    $('#f-scale').addEventListener('input', e => { $('#scaleval').textContent = (+e.target.value).toFixed(2) + '×'; });
    $('#f-scale').addEventListener('change', e => this._set('scale', +e.target.value));
    $('#f-tsize').addEventListener('input', e => { $('#tsizeval').textContent = (+e.target.value).toFixed(2) + 'rem'; });
    $('#f-tsize').addEventListener('change', e => this._set('title_size', +e.target.value));
    this.shadowRoot.querySelectorAll('[data-anim]').forEach(b =>
      b.addEventListener('click', () => { this._set('animate', b.dataset.anim === 'on'); this._render(); }));
    this.shadowRoot.querySelectorAll('input[type=color]').forEach(inp =>
      inp.addEventListener('change', e => {
        this._setColor(e.target.dataset.ckey, e.target.value);
        const dot = e.target.parentElement.querySelector('.cdot'); if (dot) dot.style.background = e.target.value;
      }));
    this.shadowRoot.querySelectorAll('.rst').forEach(b =>
      b.addEventListener('click', () => { this._setColor(b.dataset.reset, null); this._render(); }));
    $('#resetall').addEventListener('click', () => this._resetAll());
  }
}
customElements.define('deye-card-editor', DeyeCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({ type: 'deye-card', name: 'Інвертор Deye', description: 'Deye SUN-5K — анімована схема потоку енергії (мережа/генератор/АКБ/будинок) + заряд батареї; layout: hub / vertical / mini', preview: true });
console.info('%c DEYE-CARD v5 ', 'background:#ff8a3d;color:#fff');
