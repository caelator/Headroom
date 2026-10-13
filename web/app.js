(() => {
  'use strict';

  const POLL_MS = 10000;
  const FAST_POLL_MS = 2500;
  const FAST_WINDOW_MS = 60000;
  const TIMEOUT_MS = 8000;
  const HISTORY_REFRESH_MS = 30000;
  const LOCAL_REFRESH_MS = 60000;
  const VIEWS = ['overview', 'history', 'models', 'settings'];
  const VIEW_TITLES = { overview: 'Overview', history: 'History', models: 'Models', settings: 'Settings' };
  const WRITE_HEADERS = { 'Content-Type': 'application/json', 'X-AIUsage-Action': 'local' };
  const SVGNS = 'http://www.w3.org/2000/svg';
  const DASHES = ['', '7 4', '2 4', '9 4 2 4', '4 2', '1 5'];
  const WIN_STATES = ['fresh', 'stale', 'unknown', 'expired', 'unlimited'];
  const DEGRADED = new Set(['auth_required', 'unavailable', 'error', 'stale']);
  const STATUS_META = {
    verified: { label: 'Verified', glyph: '✓', cls: 'ok' },
    partial: { label: 'Partial', glyph: '◐', cls: 'warn' },
    auth_required: { label: 'Sign-in needed', glyph: '⚑', cls: 'info' },
    unavailable: { label: 'Unavailable', glyph: '✕', cls: 'muted' },
    error: { label: 'Error', glyph: '!', cls: 'crit' },
    stale: { label: 'Stale', glyph: '◔', cls: 'muted' },
  };
  const WIN_LABEL = { fresh: 'Fresh', stale: 'Stale', unknown: 'Unknown', expired: 'Expired', unlimited: 'Unlimited' };
  const WIN_BADGE = { fresh: 'ok', stale: 'muted', unknown: 'muted', expired: 'muted', unlimited: 'info' };

  const state = {
    data: null,
    raw: '',
    renderKey: '',
    lastGoodAt: null,
    offline: false,
    lastError: null,
    failures: 0,
    polling: false,
    pollTimer: null,
    fastUntil: 0,
    refreshing: false,
    saving: false,
    settingsDirty: false,
    view: 'overview',
    open: new Set(),
    history: { provider: null, hours: 24, metric: 'remaining', data: null, key: '', fetchedAt: 0, error: null, loading: false },
    local: { days: 7, data: null, error: null, fetchedAt: 0, loading: false },
    summary: { data: null, error: null, fetchedAt: 0, loading: false },
  };

  const $ = (sel, root = document) => root.querySelector(sel);

  /* ---------- DOM helpers (no HTML string interpolation anywhere) ---------- */

  function appendKids(el, kids) {
    for (const k of kids.flat(Infinity)) {
      if (k === null || k === undefined || k === false) continue;
      el.append(k instanceof Node ? k : document.createTextNode(String(k)));
    }
  }

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'href') {
          if (isAllowedHref(v)) el.setAttribute('href', v);
        } else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? '' : String(v));
      }
    }
    appendKids(el, kids);
    return el;
  }

  function s(tag, attrs, ...kids) {
    const el = document.createElementNS(SVGNS, tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        el.setAttribute(k, String(v));
      }
    }
    appendKids(el, kids);
    return el;
  }

  function isAllowedHref(v) {
    if (typeof v !== 'string') return false;
    if (v.startsWith('#') || v === '/api/export.csv') return true;
    return safeExternal(v) !== null;
  }

  function safeExternal(u) {
    if (typeof u !== 'string' || !u) return null;
    try {
      const url = new URL(u);
      if (url.protocol !== 'https:' || url.username || url.password) return null;
      return url.href;
    } catch {
      return null;
    }
  }

  /* ---------- formatting ---------- */

  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const clampPct = (v) => Math.min(100, Math.max(0, v));
  const intFmt = new Intl.NumberFormat();
  const compactFmt = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
  const fmtInt = (n) => (isNum(n) ? intFmt.format(n) : '—');
  const fmtCompact = (n) => (isNum(n) ? compactFmt.format(n) : '—');
  const fmtPct = (v) => (isNum(v) ? `${+v.toFixed(1)}%` : '—');
  const fmtRate = (v) => (isNum(v) ? `${+v.toFixed(1)} % pts/h` : '—');
  const nonEmpty = (v) => (typeof v === 'string' && v.trim() ? v.trim() : null);
  const truncate = (str, n) => (str.length > n ? `${str.slice(0, n - 1)}…` : str);
  const plural = (n, one, many) => `${fmtInt(n)} ${n === 1 ? one : many}`;

  function humanize(key) {
    const t = String(key).replace(/[_-]+/g, ' ').trim();
    return t ? t.charAt(0).toUpperCase() + t.slice(1) : '';
  }

  function parseTs(v) {
    if (typeof v !== 'string' || !v) return null;
    const t = Date.parse(v);
    return Number.isNaN(t) ? null : t;
  }

  function fmtDur(ms) {
    const sec = Math.round(Math.abs(ms) / 1000);
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60);
    if (m < 60) return `${m}m`;
    const hr = Math.floor(m / 60);
    if (hr < 48) return m % 60 ? `${hr}h ${m % 60}m` : `${hr}h`;
    const d = Math.floor(hr / 24);
    return hr % 24 ? `${d}d ${hr % 24}h` : `${d}d`;
  }

  function fmtSpan(seconds) {
    return isNum(seconds) ? (seconds < 1 ? '0s' : fmtDur(seconds * 1000)) : '—';
  }

  function fmtMinutes(m) {
    if (!isNum(m) || m <= 0) return '—';
    if (m < 90) return `${+m.toFixed(0)} min`;
    if (m < 2880) return `${+(m / 60).toFixed(1)} h`;
    return `${+(m / 1440).toFixed(1)} d`;
  }

  function relText(ts) {
    const diff = ts - Date.now();
    if (Math.abs(diff) < 5000) return 'now';
    return diff > 0 ? `in ${fmtDur(diff)}` : `${fmtDur(diff)} ago`;
  }

  const dtfCache = new Map();
  function getDtf(kind) {
    const tz = state.data && typeof state.data.timezone === 'string' ? state.data.timezone : '';
    const key = `${kind}|${tz}`;
    let f = dtfCache.get(key);
    if (f) return f;
    const base =
      kind === 'axisTime' ? { hour: '2-digit', minute: '2-digit' }
        : kind === 'axisDay' ? { weekday: 'short', hour: '2-digit', minute: '2-digit' }
          : { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' };
    try {
      f = new Intl.DateTimeFormat(undefined, tz ? { ...base, timeZone: tz } : base);
    } catch {
      f = new Intl.DateTimeFormat(undefined, base);
    }
    dtfCache.set(key, f);
    return f;
  }
  const fmtAbs = (ts) => getDtf('full').format(ts);

  function timeEl(iso, empty) {
    const ts = parseTs(iso);
    if (ts === null) return h('span', { class: 'muted' }, empty || '—');
    const el = h('time', { datetime: new Date(ts).toISOString(), 'data-ts': ts, title: fmtAbs(ts) });
    el.textContent = relText(ts);
    return el;
  }

  function whenEl(iso) {
    const ts = parseTs(iso);
    if (ts === null) return h('span', { class: 'muted' }, '—');
    return h('span', { class: 'when' }, timeEl(iso), ' ', h('span', { class: 'muted abs' }, `(${fmtAbs(ts)})`));
  }

  function tickTimes() {
    if (document.visibilityState !== 'visible') return;
    commit(); // Invalidate percentages and projections at the deadline between responses.
    if (state.view === 'models' && state.local.data?.import?.last_scan && Date.now()-parseTs(state.local.data.import.last_scan)>=90000) renderModels();
    for (const el of document.querySelectorAll('time[data-ts]')) {
      const t = relText(Number(el.dataset.ts));
      if (el.textContent !== t) el.textContent = t;
    }
  }

  function formatLoose(v, depth = 0) {
    if (v === null || v === undefined) return '—';
    if (typeof v === 'number') return fmtInt(v);
    if (typeof v === 'boolean') return v ? 'yes' : 'no';
    if (typeof v === 'string') return truncate(v, 80);
    if (Array.isArray(v)) return v.length ? `${v.length} item${v.length === 1 ? '' : 's'}` : 'none';
    if (typeof v === 'object' && depth < 2) {
      const parts = Object.entries(v).slice(0, 6).map(([k, x]) => `${humanize(k)}: ${formatLoose(x, depth + 1)}`);
      return parts.length ? parts.join(', ') : 'none';
    }
    return '…';
  }

  /* ---------- network ---------- */

  async function api(path, opts = {}) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), opts.timeout || TIMEOUT_MS);
    try {
      const res = await fetch(path, { cache: 'no-store', credentials: 'same-origin', ...opts.init, signal: ctl.signal });
      const text = await res.text();
      let json = null;
      try {
        json = text ? JSON.parse(text) : null;
      } catch {
        json = null;
      }
      return { ok: res.ok, status: res.status, text, json };
    } finally {
      clearTimeout(timer);
    }
  }

  function describeError(res) {
    const e = res.json && res.json.error;
    if (typeof e === 'string' && e) return truncate(e, 200);
    if (e && typeof e.message === 'string' && e.message) return truncate(e.message, 200);
    return `HTTP ${res.status}`;
  }

  function errText(err) {
    if (err && err.name === 'AbortError') return 'request timed out';
    return truncate((err && err.message) || 'network error', 160);
  }

  /* ---------- snapshot model ---------- */

  function normalizeSnapshot(j) {
    return {
      ...j,
      settings: j.settings && typeof j.settings === 'object' ? j.settings : {},
      warnings: Array.isArray(j.warnings) ? j.warnings.map(String) : [],
      providers: j.providers
        .filter((p) => p && typeof p === 'object')
        .map((p) => ({
          ...p,
          id: String(p.id === undefined || p.id === null ? '' : p.id),
          name: String(p.name || p.id || 'Unknown provider'),
          status: typeof p.status === 'string' ? p.status : 'unavailable',
          plan: p.plan && typeof p.plan === 'object' ? p.plan : {},
          auth: p.auth && typeof p.auth === 'object' ? p.auth : {},
          windows: (Array.isArray(p.windows) ? p.windows : [])
            .filter((w) => w && typeof w === 'object')
            .map((w) => ({
              ...w,
              id: String(w.id === undefined || w.id === null ? '' : w.id),
              label: String(w.label || w.id || 'Window'),
              velocity: w.velocity && typeof w.velocity === 'object' ? w.velocity : {},
            })),
        })),
    };
  }

  function levelOf(pct) {
    const st = state.data && state.data.settings;
    if (!isNum(pct) || !st) return 'muted';
    if (isNum(st.critical_remaining_pct) && pct <= st.critical_remaining_pct) return 'crit';
    if (isNum(st.warn_remaining_pct) && pct <= st.warn_remaining_pct) return 'warn';
    return 'ok';
  }

  const effStatus = (p) => {
    if (p.status === 'verified' || p.status === 'partial') {
      if (state.offline || (p.windows.some(w=>['fresh','unlimited'].includes(w.status)) && !p.windows.some(w=>['fresh','unlimited'].includes(winState(w,p))))) return 'stale';
    }
    return p.status;
  };
  const isDegraded = (p) => state.offline || DEGRADED.has(p.status);

  function winState(w, p) {
    let st = WIN_STATES.includes(w.status) ? w.status : 'unknown';
    if (st === 'fresh' || st === 'unlimited') {
      const r = parseTs(w.resets_at);
      if (r !== null && r <= Date.now()) st = 'expired';
      else if (parseTs(w.fresh_until) !== null && parseTs(w.fresh_until) <= Date.now()) st = 'stale';
      else if (isDegraded(p)) st = 'stale';
    }
    return st;
  }

  function tierInfo(p) {
    const plan = p.plan;
    const declared = nonEmpty(plan.declared_tier);
    const verified = plan.verification === 'provider' ? nonEmpty(plan.verified_tier) : null;
    const declaredIsGoal = !!(declared && /highest.*plan|highest.*available/i.test(declared));
    const mismatch = !!(declared && verified && !declaredIsGoal && declared.toLowerCase() !== verified.toLowerCase());
    const label = verified === 'promax' ? 'Pro Max' : verified === 'max' ? 'Max' : verified;
    if (verified) return { kind: 'verified', label, declared, verified, mismatch };
    if (declared) return { kind: 'declared', label: declared, declared, verified: null, mismatch: false };
    return { kind: 'unknown', label: 'Tier unknown', declared: null, verified: null, mismatch: false };
  }

  function forecastFor(x) {
    if (!x) return { kind: 'none' };
    const v = x.w.velocity;
    if (x.st !== 'fresh') return { kind: 'stale', v };
    if (x.w.remaining_pct === 0) return {kind:'exhausted',v};
    if (v.confidence === 'observed' && isNum(v.pct_per_hour)) return { kind: 'observed', v };
    if (v.confidence === 'insufficient') return { kind: 'insufficient', v };
    return { kind: 'unavailable', v };
  }

  function hasRunout(p) {
    return p.windows.some((w) => {
      const x = { w, st: winState(w, p) };
      return forecastFor(x).kind === 'observed' && w.velocity.exhausts_before_reset === true;
    });
  }

  function nextReset(wins) {
    const now = Date.now();
    let future = null;
    let past = null;
    for (const { w } of wins) {
      const t = parseTs(w.resets_at);
      if (t === null) continue;
      if (t > now) {
        if (!future || t < future.t) future = { t, iso: w.resets_at, label: w.label };
      } else if (!past || t > past.t) {
        past = { t, iso: w.resets_at, label: w.label };
      }
    }
    return { future, past };
  }

  function sampleNote(v) {
    if (!isNum(v.points)) return 'no sample count reported';
    return `${plural(v.points, 'sample', 'samples')} over ${fmtSpan(v.observed_span_seconds)}`;
  }

  function headlineFor(p, lim, wins) {
    const lastKnown = lim && isNum(lim.w.remaining_pct) ? lim.w.remaining_pct : isNum(p.remaining_pct) ? p.remaining_pct : null;
    const winLabel = lim ? lim.w.label : null;
    const byStatus = { auth_required: ['Sign-in needed', 'info'], unavailable: ['Unavailable', 'muted'], error: ['Error', 'crit'] };
    if (byStatus[p.status]) {
      return {
        kind: p.status,
        text: byStatus[p.status][0],
        level: byStatus[p.status][1],
        caption: lastKnown !== null ? `remaining unknown now · last known ${fmtPct(lastKnown)}` : 'remaining unknown',
        meterState: lastKnown !== null ? 'stale' : 'unknown',
        meterPct: lastKnown,
      };
    }
    const st = lim ? lim.st : null;
    if (st === 'fresh' || (!lim && !state.offline && p.status !== 'stale' && isNum(p.remaining_pct))) {
      const pct = lim && isNum(lim.w.remaining_pct) ? lim.w.remaining_pct : p.remaining_pct;
      if (isNum(pct)) {
        return {
          kind: 'value',
          text: fmtPct(pct),
          level: levelOf(pct),
          caption: winLabel ? `remaining · limiting: ${winLabel}` : 'remaining · window not specified',
          meterState: 'fresh',
          meterPct: pct,
        };
      }
    }
    if (st === 'unlimited' || (!lim && wins.length > 0 && wins.every((x) => x.st === 'unlimited'))) {
      return { kind: 'unlimited', text: 'Unlimited', level: 'info', caption: 'no quota cap reported', meterState: 'unlimited', meterPct: null };
    }
    if (st === 'expired') {
      return {
        kind: 'expired',
        text: 'Reset passed',
        level: 'muted',
        caption: lastKnown !== null ? `awaiting a fresh reading · ${fmtPct(lastKnown)} is from before the reset` : 'awaiting a fresh reading',
        meterState: 'expired',
        meterPct: null,
      };
    }
    if (st === 'stale' || effStatus(p) === 'stale' || state.offline) {
      return {
        kind: 'stale',
        text: 'Stale',
        level: 'muted',
        caption: lastKnown !== null ? `not current · last known ${fmtPct(lastKnown)}` : 'no current reading',
        meterState: lastKnown !== null ? 'stale' : 'unknown',
        meterPct: lastKnown,
      };
    }
    return { kind: 'unknown', text: 'Unknown', level: 'muted', caption: 'provider did not report remaining quota', meterState: 'unknown', meterPct: null };
  }

  /* ---------- small components ---------- */

  function badge(text, cls, glyph, title) {
    return h('span', { class: `badge b-${cls}`, title },
      glyph ? h('span', { class: 'glyph', 'aria-hidden': 'true' }, glyph) : null, text);
  }

  function meter(pct, st, label, large) {
    const el = h('div', { class: `meter m-${st}${large ? ' meter-lg' : ''}` });
    const fill = h('span', { class: 'meter-fill' });
    if ((st === 'fresh' || st === 'stale') && isNum(pct)) fill.style.width = `${clampPct(pct)}%`;
    else if (st === 'unlimited') fill.style.width = '100%';
    else fill.style.width = '0';
    el.append(fill);
    if (st === 'fresh' && isNum(pct)) {
      el.setAttribute('role', 'meter');
      el.setAttribute('aria-valuemin', '0');
      el.setAttribute('aria-valuemax', '100');
      el.setAttribute('aria-valuenow', String(+pct.toFixed(1)));
      el.setAttribute('aria-valuetext', `${fmtPct(pct)} remaining`);
      el.setAttribute('aria-label', label);
    } else {
      el.setAttribute('aria-hidden', 'true');
    }
    const cfg = state.data && state.data.settings;
    if (large && cfg && (st === 'fresh' || st === 'stale')) {
      for (const [val, cls] of [[cfg.warn_remaining_pct, 'tick-warn'], [cfg.critical_remaining_pct, 'tick-crit']]) {
        if (!isNum(val)) continue;
        const tick = h('span', { class: `tick ${cls}`, 'aria-hidden': 'true' });
        tick.style.left = `${clampPct(val)}%`;
        el.append(tick);
      }
    }
    return el;
  }

  function metric(label, value, sub, cls) {
    return h('div', { class: `metric${cls ? ` ${cls}` : ''}` },
      h('dt', null, label),
      h('dd', { class: 'mv' }, value),
      sub ? h('dd', { class: 'ms' }, sub) : null);
  }

  function kv(label, value) {
    return h('div', { class: 'kv' }, h('dt', null, label), h('dd', null, value));
  }

  function extLink(href, text, cls, fk) {
    return h('a', { class: cls, href, target: '_blank', rel: 'noopener noreferrer', 'data-fk': fk },
      text, h('span', { class: 'sr-only' }, ' (opens in a new tab)'), h('span', { 'aria-hidden': 'true' }, ' ↗'));
  }

  const emptyMsg = (text) => h('p', { class: 'empty' }, text);

  function setKids(el, ...kids) {
    el.replaceChildren();
    appendKids(el, kids);
  }

  function newestIso(list) {
    let best = null;
    let bestTs = -Infinity;
    for (const iso of list) {
      const t = parseTs(iso);
      if (t !== null && t > bestTs) { best = iso; bestTs = t; }
    }
    return best;
  }

  function buildTable(caption, headers, rows, numCols) {
    const num = new Set(numCols || []);
    const table = h('table', { class: 'tbl' }, h('caption', { class: 'sr-only' }, caption));
    table.append(h('thead', null, h('tr', null, headers.map((t, i) => h('th', { scope: 'col', class: num.has(i) ? 'num' : null }, t)))));
    table.append(h('tbody', null, rows.map((r) => h('tr', null,
      r.map((c, i) => (i === 0 ? h('th', { scope: 'row' }, c) : h('td', { class: num.has(i) ? 'num' : null }, c)))))));
    return h('div', { class: 'table-wrap', role: 'region', tabindex: '0', 'aria-label': caption }, table);
  }

  function dl(items, cls) {
    return h('dl', { class: cls || 'kvs' }, items);
  }

  /* ---------- overview ---------- */

  function tierLine(tier) {
    const line = h('p', { class: 'tier' });
    if (tier.kind === 'verified') {
      line.append(h('strong', null, tier.label), ' ', badge('Provider-verified', 'ok', '✓'));
      if (tier.mismatch) line.append(' ', badge(`Declared: ${tier.declared}`, 'warn', '!', 'Declared tier differs from the provider-verified tier'));
    } else if (tier.kind === 'declared') {
      line.append(h('strong', null, tier.label), ' ', badge('Declared, unverified', 'warn', '?', 'Not confirmed by the provider'));
    } else {
      line.append(h('span', { class: 'muted' }, tier.label), ' ', badge('Unverified', 'muted', '?'));
    }
    return line;
  }

  function burnMetric(x, fc, safe) {
    if (fc.kind === 'observed' || (fc.kind === 'exhausted' && fc.v.confidence==='observed' && isNum(fc.v.pct_per_hour))) {
      const v = fc.v;
      const rate = v.pct_per_hour;
      let cmp = null;
      if (rate > 0 && isNum(safe)) cmp = rate > safe ? h('span', { class: 'flag-bad' }, ' · above safe pace') : ' · within safe pace';
      if (rate <= 0) return metric('Burn rate', 'No measurable change', `over ${sampleNote(v)} · provider rounding may hide small usage`);
      return metric('Burn rate', fmtRate(rate), [`observed, ${sampleNote(v)}`, cmp]);
    }
    if (fc.kind === 'insufficient') return metric('Burn rate', 'Not enough samples', sampleNote(fc.v), 'is-dim');
    if (fc.kind === 'unavailable') return metric('Burn rate', 'Unavailable', 'no usable observations', 'is-dim');
    if (fc.kind === 'stale') return metric('Burn rate', '—', 'reading is not current', 'is-dim');
    return metric('Burn rate', '—', x ? null : 'no limiting window', 'is-dim');
  }

  function projectionMetric(fc) {
    const dt = 'Projection';
    if (fc.kind === 'exhausted') return metric(dt,'Already exhausted','wait for this allowance to reset');
    if (fc.kind === 'observed') {
      const v = fc.v;
      if (v.exhausts_before_reset === true) {
        return metric(dt, h('span', { class: 'proj-bad' }, 'Runs out before reset'),
          [parseTs(v.eta_exhaustion_at) !== null ? ['≈ ', whenEl(v.eta_exhaustion_at)] : 'ETA not reported', ' · if the observed rate holds']);
      }
      if (v.exhausts_before_reset === false) {
        return metric(dt, h('span', { class: 'proj-ok' }, 'Lasts until reset'),
          [isNum(v.predicted_remaining_at_reset) ? `~${fmtPct(v.predicted_remaining_at_reset)} left at reset` : null, ' · if the observed rate holds']);
      }
      return metric(dt, 'No projection', v.pct_per_hour === 0 ? 'provider rounding may hide small usage' : 'provider gave no reset to project against', 'is-dim');
    }
    if (fc.kind === 'insufficient') return metric(dt, 'No forecast', 'withheld until enough samples exist', 'is-dim');
    if (fc.kind === 'stale') return metric(dt, '—', 'withheld: reading is not current', 'is-dim');
    return metric(dt, '—', null, 'is-dim');
  }

  function metricsBlock(p, lim) {
    const fc = forecastFor(lim);
    const safe = lim && lim.st === 'fresh' ? lim.w.velocity.safe_pace_pct_per_hour : null;
    const items = [burnMetric(lim, fc, safe), projectionMetric(fc)];
    items.push(isNum(safe)
      ? metric('Safe pace', `≤ ${fmtRate(safe)}`, lim.w.remaining_pct === 0 ? 'allowance exhausted; wait for this window to reset' : 'keeps quota lasting until reset')
      : metric('Safe pace', '—', 'needs a current reading and reset time', 'is-dim'));
    const newest = lim && parseTs(lim.w.observed_at) !== null ? lim.w.observed_at : newestIso(p.windows.map((w) => w.observed_at));
    items.push(metric('Freshness',
      newest ? ['observed ', timeEl(newest)] : 'No timestamp',
      `source: ${p.source ? truncate(String(p.source), 60) : 'not reported'}`));
    return dl(items, 'metrics');
  }

  function forecastNote(lim) {
    const fc = forecastFor(lim);
    if (fc.kind === 'observed' && fc.v.pct_per_hour === 0) return null;
    if (fc.kind === 'observed') return `Forecast is conditional: it assumes the observed rate continues (${sampleNote(fc.v)}) and stops at the next reset.`;
    if (fc.kind === 'insufficient') return 'Forecast withheld: not enough observed samples yet.';
    if (fc.kind === 'stale') return 'Forecast withheld: reading is not current.';
    return null;
  }

  function winRows(p, wins, lim) {
    const ul = h('ul', { class: 'win-list', 'aria-label': `${p.name} quota windows` });
    for (const x of wins) {
      const { w, st } = x;
      const pct = isNum(w.remaining_pct) ? w.remaining_pct : null;
      const last = isNum(w.last_known_remaining_pct) ? w.last_known_remaining_pct : pct;
      const lvl = st === 'fresh' ? levelOf(pct) : 'muted';
      let val;
      if (st === 'fresh' && pct !== null) val = h('span', { class: `wv lvl-text-${lvl}` }, fmtPct(pct), h('span', { class: 'sr-only' }, ' remaining'));
      else if (st === 'stale') val = h('span', { class: 'wv is-dim' }, 'stale', last !== null ? h('span', { class: 'muted' }, ` · last ${fmtPct(last)}`) : null);
      else if (st === 'expired') val = h('span', { class: 'wv is-dim' }, 'reset passed');
      else if (st === 'unlimited') val = h('span', { class: 'wv' }, 'unlimited');
      else val = h('span', { class: 'wv is-dim' }, 'unknown');
      const rs = parseTs(w.resets_at);
      ul.append(h('li', { class: `win-row lvl-${lvl}` },
        h('span', { class: 'wl' }, w.label, lim && lim.w === w ? h('span', { class: 'tag' }, 'limiting') : null),
        val,
        meter(pct, st === 'expired' ? 'unknown' : st, `${p.name} ${w.label} remaining`, false),
        h('span', { class: 'wr muted' }, rs !== null ? ['resets ', timeEl(w.resets_at)] : 'reset not reported')));
    }
    return ul;
  }

  function windowDetail(x) {
    const { w, st } = x;
    const v = w.velocity;
    const fc = forecastFor(x);
    let remaining;
    if (st === 'fresh') remaining = fmtPct(w.remaining_pct);
    else if (st === 'stale') remaining = `not current (last known ${fmtPct(w.last_known_remaining_pct ?? w.remaining_pct)})`;
    else if (st === 'expired') remaining = 'window reset since this reading';
    else if (st === 'unlimited') remaining = 'unlimited';
    else remaining = 'unknown';
    const items = [
      kv('Remaining', remaining),
      kv('Used (provider-reported)', st === 'fresh' && isNum(w.used_pct) ? fmtPct(w.used_pct) : '—'),
      kv('Window length', fmtMinutes(w.window_minutes)),
      kv('Resets', whenEl(w.resets_at)),
      kv('Observed', whenEl(w.observed_at)),
      kv('Source', w.source ? truncate(String(w.source), 80) : '—'),
      kv('Forecast basis', v.confidence ? `${v.confidence} · ${sampleNote(v)}` : '—'),
    ];
    if (fc.kind === 'observed') {
      items.push(
        kv('Burn rate', fmtRate(v.pct_per_hour)),
        kv('Exhaustion ETA', v.eta_exhaustion_at ? whenEl(v.eta_exhaustion_at) : '—'),
        kv('Before reset?', v.exhausts_before_reset === true ? 'yes (if rate holds)' : v.exhausts_before_reset === false ? 'no (if rate holds)' : '—'),
        kv('Predicted at reset', isNum(v.predicted_remaining_at_reset) ? fmtPct(v.predicted_remaining_at_reset) : '—'),
        kv('Safe pace', isNum(v.safe_pace_pct_per_hour) ? `≤ ${fmtRate(v.safe_pace_pct_per_hour)}` : '—'));
    }
    return h('section', { class: 'win-detail' },
      h('h4', null, w.label, ' ', badge(WIN_LABEL[st], WIN_BADGE[st])),
      dl(items));
  }

  function planDetail(p, tier) {
    const plan = p.plan;
    const items = [
      kv('Declared tier', tier.declared || 'not declared'),
      kv('Provider-verified tier', tier.verified || (plan.verification === 'provider' ? 'provider reported none' : 'not verified')),
      kv('Verification', plan.verification === 'provider' ? ['provider ', plan.verified_at ? ['· ', whenEl(plan.verified_at)] : null] : 'unverified'),
      kv('Auth', `${p.auth.method ? truncate(String(p.auth.method), 60) : 'unknown'} · ${p.auth.verified ? 'verified' : 'not verified'}`),
      kv('Source', p.source ? truncate(String(p.source), 80) : '—'),
      kv('Last attempt', p.last_attempt_at ? whenEl(p.last_attempt_at) : '—'),
      kv('Provider status', (STATUS_META[p.status] || { label: p.status }).label),
    ];
    if (p.wallet && !state.offline && parseTs(p.wallet.fresh_until)>Date.now() && isNum(p.wallet.balance)) items.push(kv(p.wallet.label || 'Extra balance', `${fmtInt(p.wallet.balance)} ${p.wallet.unit || 'units'} · separate from plan quota`));
    return h('section', { class: 'win-detail' }, h('h4', null, 'Plan & connection'), dl(items));
  }

  function buildDetails(p, wins, tier) {
    const key = `details:${p.id}`;
    const d = h('details', { class: 'more' });
    d.append(h('summary', { 'data-fk': `sum:${p.id}` }, 'Full details'));
    d.append(planDetail(p, tier));
    for (const x of wins) d.append(windowDetail(x));
    if (state.open.has(key)) d.open = true;
    d.addEventListener('toggle', () => {
      if (d.open) state.open.add(key);
      else state.open.delete(key);
    });
    return d;
  }

  function actionsRow(p) {
    const row = h('div', { class: 'card-actions' });
    const href = safeExternal(p.connection_url);
    const needsAuth = p.status === 'auth_required';
    if (href) {
      row.append(extLink(href, needsAuth ? `Sign in at ${p.name} portal` : `${p.name} account portal`, needsAuth ? 'btn btn-primary' : 'btn', `portal:${p.id}`));
    } else {
      row.append(h('span', { class: 'muted small' }, 'No official portal link supplied'));
    }
    row.append(h('a', {
      class: 'btn', href: historyLink(p.id), 'data-fk': `hist:${p.id}`,
      onclick: () => { state.history.provider = p.id; },
    }, 'History'));
    return row;
  }

  function buildCard(p, idx) {
    const eff = effStatus(p);
    const wins = p.windows.map((w) => ({ w, st: winState(w, p) }));
    const lim = wins.find((x) => x.w.id === p.limiting_window_id) || null;
    const head = headlineFor(p, lim, wins);
    const tier = tierInfo(p);
    const titleId = `card-title-${idx}`;
    const meta = STATUS_META[eff] || { label: eff, glyph: '?', cls: 'muted' };
    const card = h('article', { class: `card lvl-${head.level} st-${eff}`, 'aria-labelledby': titleId });

    card.append(h('header', { class: 'card-head' },
      h('div', { class: 'card-id' }, h('h2', { id: titleId, class: 'card-title' }, p.name), tierLine(tier)),
      badge(meta.label, meta.cls, meta.glyph, state.offline && eff !== p.status ? 'Local API not responding; showing last good data' : undefined)));

    card.append(h('div', { class: 'headline' },
      h('p', { class: `big big-${head.kind}` }, head.kind === 'value' ? h('span', { class: 'sr-only' }, 'Remaining ') : null, head.text),
      h('p', { class: 'caption' }, head.caption)));
    card.append(meter(head.meterPct, head.meterState, `${p.name} remaining`, true));

    const nr = nextReset(wins);
    if (lim && parseTs(lim.w.resets_at) > Date.now()) nr.future = {iso:lim.w.resets_at,label:lim.w.label};
    const resetLine = h('p', { class: 'reset-line' }, h('span', { class: 'k' }, lim && nr.future && nr.future.iso === lim.w.resets_at ? 'Limiting reset' : 'Next reset'), ' ');
    if (nr.future) resetLine.append(whenEl(nr.future.iso), h('span', { class: 'muted' }, ` · ${nr.future.label}`));
    else if (nr.past) resetLine.append(h('span', { class: 'muted' }, 'scheduled time passed '), timeEl(nr.past.iso));
    else resetLine.append(h('span', { class: 'muted' }, 'not reported'));
    card.append(resetLine);

    card.append(metricsBlock(p, lim));
    const note = forecastNote(lim);
    if (note) card.append(h('p', { class: 'fine' }, note));

    if (p.status === 'auth_required') {
      card.append(h('p', { class: 'note note-info' }, 'Quota is unknown until this account is signed in. Headroom reuses existing sessions and never takes credentials here; sign in at the official portal, then refresh.'));
    }
    if (p.error && (p.error.code || p.error.message)) {
      card.append(h('p', { class: 'note note-crit' }, h('strong', null, nonEmpty(p.error.code) || 'error'), ': ', truncate(String(p.error.message || ''), 200)));
    }

    if (wins.length) card.append(winRows(p, wins, lim));
    else card.append(h('p', { class: 'muted small' }, 'No quota windows reported.'));
    card.append(buildDetails(p, wins, tier));
    card.append(actionsRow(p));
    return card;
  }

  function renderSkeleton() {
    const box = $('#cards');
    box.setAttribute('aria-busy', 'true');
    box.replaceChildren(...[0, 1, 2, 3, 4].map(() => h('div', { class: 'card skeleton', 'aria-hidden': 'true' },
      h('div', { class: 'sk sk-line w60' }), h('div', { class: 'sk sk-big' }), h('div', { class: 'sk sk-line' }), h('div', { class: 'sk sk-line w80' }))));
  }

  function renderOverviewEmpty() {
    const box = $('#cards');
    const msg = `Can't reach the local Headroom API (${state.lastError || 'no response'}). Nothing is shown until a first snapshot arrives.`;
    if (box.dataset.empty === msg) return;
    box.dataset.empty = msg;
    box.setAttribute('aria-busy', 'false');
    box.replaceChildren(h('div', { class: 'panel' }, h('p', null, msg),
      h('button', { type: 'button', class: 'btn', onclick: () => pollSnapshot() }, 'Retry now')));
  }

  function renderSummary() {
    const box = $('#summary');
    const counts = {};
    let runout = 0;
    for (const p of state.data.providers) {
      const st = effStatus(p);
      counts[st] = (counts[st] || 0) + 1;
      if (!isDegraded(p) && hasRunout(p)) runout += 1;
      if (p.windows.some(w => winState(w,p)==='fresh' && w.remaining_pct === 0)) counts.exhausted=(counts.exhausted || 0)+1;
    }
    const chips = [];
    for (const key of ['verified', 'partial', 'auth_required', 'unavailable', 'error', 'stale']) {
      if (!counts[key]) continue;
      const m = STATUS_META[key];
      chips.push(h('li', null, badge(`${counts[key]} ${m.label.toLowerCase()}`, m.cls, m.glyph)));
    }
    if (counts.exhausted) chips.push(h('li', null, badge(`${counts.exhausted} plan allowance exhausted`, 'crit', '!')));
    if (runout) chips.push(h('li', null, badge(`${runout} projected to run out before reset`, 'crit', '!')));
    box.replaceChildren(...chips);
  }

  function renderNotices() {
    const box = $('#notices');
    const warnings = state.data.warnings;
    if (!warnings.length) { box.replaceChildren(); return; }
    const key = 'notices';
    const d = h('details', { class: 'more notices' },
      h('summary', { 'data-fk': 'sum:notices' }, plural(warnings.length, 'service notice', 'service notices')),
      h('ul', { class: 'plain' }, warnings.map((w) => h('li', null, truncate(w, 300)))));
    if (state.open.has(key)) d.open = true;
    d.addEventListener('toggle', () => {
      if (d.open) state.open.add(key);
      else state.open.delete(key);
    });
    box.replaceChildren(d);
  }

  function renderOverview() {
    const box = $('#cards');
    delete box.dataset.empty;
    box.setAttribute('aria-busy', 'false');
    const providers = state.data.providers;
    if (!providers.length) box.replaceChildren(emptyMsg('The local API reported no providers yet.'));
    else box.replaceChildren(...providers.map(buildCard));
    renderGlance();
    renderSummary();
    renderNotices();
  }

  function renderGlance() {
    $('#glance').replaceChildren(...state.data.providers.map(p => {
      const wins=p.windows.map(w=>({w,st:winState(w,p)}));
      const lim=wins.find(x=>x.w.id===p.limiting_window_id)||null;
      const head=headlineFor(p,lim,wins);
      return h('a',{class:`glance-item lvl-${head.level}`,href:historyLink(p.id),onclick:()=>{state.history.provider=p.id;}},
        h('span',{class:'glance-name'},p.id==='codex'?'Codex':p.name),
        h('strong',{class:`lvl-text-${head.level}`},head.text),
        h('span',{class:'muted small'},lim?lim.w.label:'quota unavailable'));
    }));
  }

  function renderLocalSummary() {
    const el = $('#local-summary');
    const sm = state.summary;
    if (sm.data) {
      const models = Array.isArray(sm.data.models) ? sm.data.models : [];
      const t = sm.data.totals && typeof sm.data.totals === 'object' ? sm.data.totals : {};
      const tokens = isNum(t.total_tokens) ? t.total_tokens : models.reduce((n, m) => n + (isNum(m.total_tokens) ? m.total_tokens : 0), 0);
      const reqs = isNum(t.requests) ? t.requests : models.reduce((n, m) => n + (isNum(m.requests) ? m.requests : 0), 0);
      const unknown = models.some((m) => m.unknown_usage);
      const stale=sm.error || (parseTs(sm.data.import?.last_scan) !== null && Date.now()-parseTs(sm.data.import.last_scan)>90000);
      el.textContent = `${stale ? 'Last-known local accounting · ' : ''}Today (Bogota): ${plural(tokens, 'token', 'tokens')} · ${plural(reqs, 'request', 'requests')} · ${plural(models.length, 'tracked model', 'tracked models')}${unknown ? ' · some usage unknown' : ''}. Local counters, separate from plan quota %.`;
    } else if (sm.error) {
      el.textContent = `Local model accounting unavailable: ${sm.error}`;
    } else {
      el.textContent = 'Loading local token counters…';
    }
  }

  /* ---------- history ---------- */

  function ensureHistoryProvider() {
    const provs = state.data ? state.data.providers : [];
    const hs = state.history;
    if (!state.data) return;
    if (!provs.some((p) => p.id === hs.provider)) hs.provider = provs.length ? provs[0].id : null;
  }

  function renderHistoryControls() {
    ensureHistoryProvider();
    const sel = $('#hist-provider');
    const provs = state.data ? state.data.providers : [];
    const sig = provs.map((p) => `${p.id}:${p.name}`).join('|');
    if (sel.dataset.sig !== sig) {
      sel.replaceChildren(...provs.map((p) => h('option', { value: p.id }, p.name)));
      sel.dataset.sig = sig;
    }
    if (state.history.provider !== null) sel.value = state.history.provider;
    $('#hist-hours').value = String(state.history.hours);
    $('#hist-metric').value = state.history.metric;
  }

  async function loadHistory() {
    const hs = state.history;
    ensureHistoryProvider();
    if (hs.provider === null || hs.loading) return;
    const key = `${hs.provider}|${hs.hours}`;
    if (hs.key !== key) hs.data = null;
    hs.loading = true;
    $('#hist-status').textContent = 'Loading history…';
    if (!hs.data) renderHistory();
    try {
      const res = await api(`/api/history?provider=${encodeURIComponent(hs.provider)}&hours=${encodeURIComponent(hs.hours)}`);
      if (!res.ok || !res.json || !Array.isArray(res.json.series)) throw new Error(res.ok ? 'Unexpected response from /api/history' : describeError(res));
      if (key !== `${hs.provider}|${hs.hours}`) return;
      hs.data = res.json;
      hs.key = key;
      hs.error = null;
      hs.fetchedAt = Date.now();
    } catch (err) {
      if (key !== `${hs.provider}|${hs.hours}`) return;
      hs.error = errText(err);
      if (hs.key !== key) hs.data = null;
    } finally {
      hs.loading = false;
      renderHistory();
      if (state.view === 'history' && hs.provider !== null && `${hs.provider}|${hs.hours}` !== key) loadHistory();
    }
  }

  function prepSeries(data, metricKind) {
    const field = metricKind === 'used' ? 'used_pct' : 'remaining_pct';
    return data.series.filter((sr) => sr && typeof sr === 'object').map((sr, i) => {
      const pts = (Array.isArray(sr.points) ? sr.points : [])
        .filter((pt) => pt && typeof pt === 'object')
        .map((pt) => ({ t: parseTs(pt.observed_at), v: isNum(pt[field]) ? pt[field] : null, reset: parseTs(pt.resets_at) }))
        .filter((pt) => pt.t !== null)
        .sort((a, b) => a.t - b.t);
      const deltas = [];
      for (let k = 1; k < pts.length; k += 1) if (pts[k].t > pts[k - 1].t) deltas.push(pts[k].t - pts[k - 1].t);
      deltas.sort((a, b) => a - b);
      const median = deltas.length ? deltas[Math.floor(deltas.length / 2)] : 0;
      const gapMs = 90000;
      const { segs, resets } = splitSegments(pts, gapMs);
      return { idx: i, id: String(sr.window_id === undefined ? i : sr.window_id), label: String(sr.label || sr.window_id || `Window ${i + 1}`), points: pts, segs, resets, gapMs };
    });
  }

  function splitSegments(points, gapMs) {
    const segs = [];
    const resets = [];
    let cur = [];
    let lastReset = null;
    const flush = () => { if (cur.length) segs.push(cur); cur = []; };
    for (const pt of points) {
      const changed = lastReset !== null && pt.reset !== null && pt.t >= lastReset && pt.reset > lastReset + 1000;
      if (changed) { resets.push(lastReset); flush(); }
      if (pt.reset !== null) lastReset = pt.reset;
      if (pt.v === null) { flush(); continue; }
      if (cur.length && pt.t - cur[cur.length - 1].t > gapMs) flush();
      cur.push(pt);
    }
    flush();
    return { segs, resets };
  }

  function nearestPoint(sr, t) {
    let best = null;
    for (const pt of sr.points) {
      if (pt.v === null) continue;
      if (best === null || Math.abs(pt.t - t) < Math.abs(best.t - t)) best = pt;
    }
    return best && Math.abs(best.t - t) <= sr.gapMs ? best : null;
  }

  function drawChart(box, series, hours, metricKind) {
    const width = Math.max(280, Math.floor(box.clientWidth || 600));
    const height = width < 520 ? 240 : 320;
    const m = { l: 40, r: 12, t: 12, b: 28 };
    const iw = width - m.l - m.r;
    const ih = height - m.t - m.b;
    let tmax = Date.now();
    for (const sr of series) for (const pt of sr.points) if (pt.t > tmax) tmax = pt.t;
    const tmin = tmax - hours * 3600000;
    const x = (t) => m.l + Math.min(1, Math.max(0, (t - tmin) / (tmax - tmin))) * iw;
    const y = (v) => m.t + (1 - clampPct(v) / 100) * ih;
    const label = `${metricKind === 'used' ? 'Used' : 'Remaining'} percent over the last ${hours} hours, ${series.length} quota window${series.length === 1 ? '' : 's'}. Focus and use left and right arrow keys to read values.`;
    const svg = s('svg', { class: 'chart', width, height, viewBox: `0 0 ${width} ${height}`, role: 'group', tabindex: '0', 'aria-label': label, 'aria-describedby': 'hist-readout' });

    const grid = s('g', { class: 'grid' });
    for (const v of [0, 25, 50, 75, 100]) {
      grid.append(s('line', { x1: m.l, x2: m.l + iw, y1: y(v), y2: y(v) }));
      grid.append(s('text', { x: m.l - 6, y: y(v) + 4, 'text-anchor': 'end' }, `${v}%`));
    }
    const axisKind = hours <= 24 ? 'axisTime' : 'axisDay';
    for (let i = 0; i <= 4; i += 1) {
      const t = tmin + ((tmax - tmin) * i) / 4;
      grid.append(s('text', { x: x(t), y: height - 8, 'text-anchor': i === 0 ? 'start' : i === 4 ? 'end' : 'middle' }, getDtf(axisKind).format(t)));
    }
    svg.append(grid);

    const cfg = state.data && state.data.settings;
    if (cfg) {
      for (const [val, cls, name] of [[cfg.warn_remaining_pct, 'thr-warn', 'warn'], [cfg.critical_remaining_pct, 'thr-crit', 'critical']]) {
        if (!isNum(val)) continue;
        const yv = metricKind === 'used' ? 100 - val : val;
        svg.append(s('line', { class: `thr ${cls}`, x1: m.l, x2: m.l + iw, y1: y(yv), y2: y(yv) }));
        svg.append(s('text', { class: `thr-label ${cls}`, x: m.l + iw - 2, y: y(yv) - 3, 'text-anchor': 'end' }, `${name} threshold`));
      }
    }

    const totalPts = series.reduce((n, sr) => n + sr.points.length, 0);
    for (const sr of series) {
      const g = s('g', { class: `series s-${sr.idx % 6}` });
      for (const seg of sr.segs) {
        if (seg.length === 1) {
          g.append(s('circle', { cx: x(seg[0].t), cy: y(seg[0].v), r: 3.5 }));
        } else {
          g.append(s('path', { d: seg.map((pt, i) => `${i ? 'L' : 'M'}${x(pt.t).toFixed(1)} ${y(pt.v).toFixed(1)}`).join(' '), 'stroke-dasharray': DASHES[sr.idx % 6], 'stroke-width': 2, 'stroke-linejoin': 'round' }));
          if (totalPts <= 80) for (const pt of seg) g.append(s('circle', { cx: x(pt.t), cy: y(pt.v), r: 2 }));
        }
      }
      for (const rt of sr.resets) {
        if (rt >= tmin && rt <= tmax) g.append(s('line', { class: 'reset-tick', x1: x(rt), x2: x(rt), y1: m.t + ih - 10, y2: m.t + ih }));
      }
      svg.append(g);
    }

    const cursor = s('line', { class: 'cursor', y1: m.t, y2: m.t + ih, visibility: 'hidden' });
    svg.append(cursor);

    const times = [...new Set(series.flatMap((sr) => sr.points.map((pt) => pt.t)))].sort((a, b) => a - b);
    const readout = $('#hist-readout');
    const srLive = $('#hist-sr');
    readout.textContent = 'Hover the chart, or focus it and use the arrow keys, to read values.';
    let cursorIdx = times.length - 1;
    const show = (t, announce) => {
      cursor.setAttribute('x1', x(t));
      cursor.setAttribute('x2', x(t));
      cursor.setAttribute('visibility', 'visible');
      const parts = series.map((sr) => {
        const pt = nearestPoint(sr, t);
        return `${sr.label}: ${pt ? fmtPct(pt.v) : 'no reading'}`;
      });
      const text = `${fmtAbs(t)} — ${parts.join(' · ')}`;
      readout.textContent = text;
      if (announce) srLive.textContent = text;
    };
    const nearestTime = (t) => {
      let bi = 0;
      for (let i = 1; i < times.length; i += 1) if (Math.abs(times[i] - t) < Math.abs(times[bi] - t)) bi = i;
      return bi;
    };
    svg.addEventListener('pointermove', (ev) => {
      if (!times.length) return;
      const r = svg.getBoundingClientRect();
      const px = ((ev.clientX - r.left) * width) / (r.width || width);
      const t = tmin + ((px - m.l) / iw) * (tmax - tmin);
      cursorIdx = nearestTime(t);
      show(times[cursorIdx], false);
    });
    svg.addEventListener('keydown', (ev) => {
      if (!times.length) return;
      if (ev.key === 'ArrowLeft') cursorIdx = Math.max(0, cursorIdx - 1);
      else if (ev.key === 'ArrowRight') cursorIdx = Math.min(times.length - 1, cursorIdx + 1);
      else if (ev.key === 'Home') cursorIdx = 0;
      else if (ev.key === 'End') cursorIdx = times.length - 1;
      else return;
      ev.preventDefault();
      show(times[cursorIdx], true);
    });
    const hadFocus = box.contains(document.activeElement);
    box.replaceChildren(svg);
    if (hadFocus) svg.focus({ preventScroll: true });
  }

  function renderLegend(series) {
    $('#hist-legend').replaceChildren(...series.map((sr) => {
      let latest = null;
      for (const pt of sr.points) if (pt.v !== null) latest = pt;
      const swatch = s('svg', { class: `swatch s-${sr.idx % 6}`, width: 30, height: 10, viewBox: '0 0 30 10', 'aria-hidden': 'true' },
        s('line', { x1: 1, x2: 29, y1: 5, y2: 5, 'stroke-width': 2.5, 'stroke-dasharray': DASHES[sr.idx % 6] }));
      return h('li', null, swatch, h('span', null, sr.label),
        h('span', { class: 'muted' }, latest ? [`latest ${fmtPct(latest.v)} · `, timeEl(new Date(latest.t).toISOString())] : 'no readings'));
    }));
  }

  function renderHistoryTable(series) {
    const rows = series.map((sr) => {
      const vals = sr.points.filter((pt) => pt.v !== null);
      const latest = vals.length ? vals[vals.length - 1] : null;
      const lowest = vals.length ? Math.min(...vals.map((pt) => pt.v)) : null;
      return [sr.label, fmtInt(sr.points.length), latest ? fmtPct(latest.v) : '—', lowest !== null ? fmtPct(lowest) : '—',
        sr.points.length ? timeEl(new Date(sr.points[sr.points.length - 1].t).toISOString()) : '—'];
    });
    $('#hist-table').replaceChildren(h('details', { class: 'more' },
      h('summary', null, 'Data table'),
      buildTable('Quota history summary per window', ['Window', 'Samples', 'Latest', 'Lowest', 'Newest sample'], rows, [1, 2, 3])));
  }

  function renderHistory() {
    const hs = state.history;
    const box = $('#hist-chart');
    const status = $('#hist-status');
    if (hs.loading) status.textContent = 'Loading history…';
    else status.textContent = hs.error ? `Could not refresh history (${hs.error}).${hs.data ? ' Showing the last data received.' : ''}` : '';
    if (!hs.data) {
      box.replaceChildren(emptyMsg(hs.loading ? 'Loading history…' : hs.error ? `History unavailable: ${hs.error}` : 'Select a provider to see its quota history.'));
      $('#hist-legend').replaceChildren();
      $('#hist-table').replaceChildren();
      $('#hist-note').textContent = '';
      return;
    }
    const series = prepSeries(hs.data, hs.metric);
    const total = series.reduce((n, sr) => n + sr.points.length, 0);
    if (!total) {
      box.replaceChildren(emptyMsg(`No quota snapshots recorded for this provider in the last ${hs.hours} hours. History accumulates as background sync runs.`));
      $('#hist-legend').replaceChildren();
      $('#hist-table').replaceChildren();
      $('#hist-note').textContent = '';
      return;
    }
    drawChart(box, series, hs.hours, hs.metric);
    renderLegend(series);
    renderHistoryTable(series);
    $('#hist-note').textContent = 'Lines break where readings are missing, where gaps exceed 90 seconds, or where a quota window reset (ticks on the baseline). Nothing is interpolated across them. Dashed horizontal lines are your warning thresholds.';
  }

  /* ---------- models / local usage ---------- */

  async function fetchLocal(days) {
    const res = await api(`/api/local-usage?days=${encodeURIComponent(days)}`);
    if (res.json && typeof res.json.error === 'string' && res.json.error) throw new Error(truncate(res.json.error, 200));
    if (!res.ok || !res.json) throw new Error(describeError(res));
    return res.json;
  }

  async function loadSummary() {
    const sm = state.summary;
    if (sm.loading) return;
    sm.loading = true;
    try {
      sm.data = await fetchLocal(1);
      sm.error = null;
    } catch (err) {
      sm.error = errText(err);
    } finally {
      sm.loading = false;
      sm.fetchedAt = Date.now();
      renderLocalSummary();
    }
  }

  async function loadLocal() {
    const ls = state.local;
    if (ls.loading) return;
    const days = ls.days;
    ls.loading = true;
    $('#models-status').textContent = 'Loading local usage…';
    if (!ls.data) renderModels();
    try {
      const data = await fetchLocal(days);
      if (ls.days !== days) return;
      ls.data = data;
      ls.error = null;
      ls.shownDays = days;
    } catch (err) {
      if (ls.days !== days) return;
      ls.error = errText(err);
      if (ls.shownDays !== days) ls.data = null;
    } finally {
      ls.loading = false;
      ls.fetchedAt = Date.now();
      renderModels();
      if (state.view === 'models' && ls.days !== days) loadLocal();
    }
  }

  function drawDaily(box, daily) {
    const rows = daily
      .filter((d) => d && typeof d === 'object' && typeof d.day === 'string')
      .sort((a, b) => (a.day < b.day ? -1 : a.day > b.day ? 1 : 0))
      .slice(-120);
    if (!rows.length) { box.replaceChildren(); return; }
    const width = Math.max(280, Math.floor(box.clientWidth || 600));
    const height = 190;
    const m = { l: 46, r: 8, t: 10, b: 26 };
    const iw = width - m.l - m.r;
    const ih = height - m.t - m.b;
    const max = Math.max(1, ...rows.map((d) => (isNum(d.total_tokens) ? d.total_tokens : 0)));
    const slot = iw / rows.length;
    const bw = Math.max(1, slot * 0.72);
    const svg = s('svg', { class: 'chart', width, height, viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': `Daily local token totals, ${rows.length} days. Data table follows.` });
    const grid = s('g', { class: 'grid' });
    for (const f of [0, 0.5, 1]) {
      const yy = m.t + (1 - f) * ih;
      grid.append(s('line', { x1: m.l, x2: m.l + iw, y1: yy, y2: yy }));
      grid.append(s('text', { x: m.l - 6, y: yy + 4, 'text-anchor': 'end' }, fmtCompact(max * f)));
    }
    rows.forEach((d, i) => {
      if (!isNum(d.total_tokens)) return;
      const bh = (d.total_tokens / max) * ih;
      svg.append(s('rect', { class: 'bar', x: m.l + i * slot + (slot - bw) / 2, y: m.t + ih - bh, width: bw, height: Math.max(bh, d.total_tokens > 0 ? 1 : 0) },
        s('title', null, `${d.day}: ${fmtInt(d.total_tokens)} tokens, ${fmtInt(d.requests)} requests`)));
    });
    const labelIdx = rows.length > 2 ? [0, Math.floor((rows.length - 1) / 2), rows.length - 1] : rows.map((_, i) => i);
    for (const i of labelIdx) {
      grid.append(s('text', { x: m.l + i * slot + slot / 2, y: height - 8, 'text-anchor': i === 0 ? 'start' : i === rows.length - 1 ? 'end' : 'middle' }, rows[i].day));
    }
    svg.prepend(grid);
    const table = h('details', { class: 'more' }, h('summary', null, 'Daily data table'),
      buildTable('Daily local tokens and requests', ['Day', 'Tokens', 'Requests'], rows.slice().reverse().map((d) => [d.day, fmtInt(d.total_tokens), fmtInt(d.requests)]), [1, 2]));
    box.replaceChildren(h('h2', null, 'Tokens per day'), svg, table);
  }

  function renderModels() {
    const ls = state.local;
    const status = $('#models-status');
    if (ls.loading) status.textContent = 'Loading local usage…';
    else status.textContent = ls.error ? `Local usage unavailable (${ls.error}).${ls.data ? ' Showing the last data received.' : ''}` : '';
    $('#models-days').value = String(ls.days);
    const data = ls.data;
    if (!data) {
      for (const id of ['models-totals', 'models-velocity', 'models-daily', 'models-table', 'models-warnings']) $(`#${id}`).replaceChildren();
      if (!ls.loading && !ls.error) $('#models-table').replaceChildren(emptyMsg('No local usage loaded yet.'));
      return;
    }
    const models = (Array.isArray(data.models) ? data.models : []).filter((r) => r && typeof r === 'object');
    const totals = data.totals && typeof data.totals === 'object' ? data.totals : {};
    const tiles = [];
    for (const [k, v] of Object.entries(totals)) {
      if (v === null || typeof v === 'object') continue;
      const pctLike = /pct|percent/i.test(k);
      const text = typeof v === 'number' ? (pctLike ? fmtPct(v) : fmtInt(v)) : formatLoose(v);
      tiles.push(h('div', { class: 'tile' }, h('p', { class: 'tile-v' }, text), h('p', { class: 'tile-k' }, humanize(k))));
    }
    $('#models-totals').replaceChildren(...tiles);
    const live=Array.isArray(data.velocity)?data.velocity:[];
    const lastScan=parseTs(data.import?.last_scan);
    const rateFresh=!ls.error && !state.offline && lastScan !== null && Date.now()-lastScan<90000;
    $('#models-velocity').replaceChildren(h('h2',null,'Current token velocity'),
      h('p',{class:'muted small'},'Known local tokens per minute across all tracked models. Cached input is included; missing usage remains uncounted.'),
      h('div',{class:'tiles'},live.map(r=>h('div',{class:'tile'},
        h('p',{class:'tile-v'},rateFresh?`${fmtCompact(r.total_tokens_per_minute)} tokens/min`:'Unavailable'),
        h('p',{class:'tile-k'},`Last ${r.window_minutes} minutes`),
        h('p',{class:'muted small'},rateFresh?`${fmtInt(Math.round(r.requests_per_hour))} requests/hour · ${r.unknown_usage || 0} unreported`:'collector reading is not current')))));
    if (lastScan !== null && !ls.error) status.textContent=`Collector scanned ${relText(lastScan)}${rateFresh?'':' · last-known totals; current rate unavailable'}`;
    drawDaily($('#models-daily'), Array.isArray(data.daily) ? data.daily : []);

    if (models.length) {
      const rows = models.map((r) => [
        String(r.display_name || r.model || 'unknown model'),
        r.provider ? String(r.provider) : '—',
        fmtInt(r.requests),
        isNum(r.total_tokens) ? fmtInt(r.total_tokens) : h('span', { class: 'muted' }, 'unknown'),
        formatLoose(r.known_counts),
        r.unknown_usage ? badge(`unknown: ${formatLoose(r.unknown_usage)}`, 'warn', '?') : h('span', { class: 'muted' }, 'none'),
      ]);
      $('#models-table').replaceChildren(h('h2', null, 'Models'),
        buildTable('Local model accounting', ['Model', 'Provider', 'Requests', 'Total tokens', 'Known counts', 'Unknown usage'], rows, [2, 3]));
    } else {
      $('#models-table').replaceChildren(emptyMsg('No model usage recorded for this period.'));
    }
    const warnings = Array.isArray(data.warnings) ? data.warnings.map(String) : [];
    $('#models-warnings').replaceChildren(...(warnings.length
      ? [h('details', { class: 'more' }, h('summary', null, plural(warnings.length, 'accounting notice', 'accounting notices')),
        h('ul', { class: 'plain' }, warnings.map((w) => h('li', null, truncate(w, 300)))))]
      : []));
  }

  /* ---------- settings / connections / diagnostics ---------- */

  function renderConnections() {
    const box = $('#conn-list');
    const provs = state.data ? state.data.providers : [];
    if (!provs.length) { box.replaceChildren(emptyMsg('No providers reported yet.')); return; }
    box.replaceChildren(...provs.map((p) => {
      const tier = tierInfo(p);
      const eff = effStatus(p);
      const meta = STATUS_META[eff] || { label: eff, glyph: '?', cls: 'muted' };
      let verdict;
      if (tier.kind === 'verified' && tier.declared && /highest.*plan/i.test(tier.declared)) verdict = badge('Provider tier shown; highest-plan claim is separate', 'warn', '?');
      else if (tier.kind === 'verified' && tier.declared && !tier.mismatch) verdict = badge('Declared tier matches provider', 'ok', '✓');
      else if (tier.mismatch) verdict = badge('Declared differs from provider, trust verified', 'warn', '!');
      else if (tier.kind === 'verified') verdict = badge('Verified by provider; no declared tier', 'ok', '✓');
      else if (tier.kind === 'declared') verdict = badge('Declared only, not verified by provider', 'warn', '?');
      else verdict = badge('No tier information', 'muted', '?');
      const href = safeExternal(p.connection_url);
      return h('article', { class: 'conn' },
        h('header', { class: 'card-head' }, h('h3', null, p.name), badge(meta.label, meta.cls, meta.glyph)),
        dl([
          kv('Declared tier', tier.declared || 'not declared'),
          kv('Provider-verified tier', tier.verified || (p.plan.verification === 'provider' ? 'provider reported none' : 'not verified')),
          kv('Verification', p.plan.verification === 'provider' ? ['by provider ', p.plan.verified_at ? ['· ', timeEl(p.plan.verified_at)] : null] : 'unverified'),
          kv('Auth', `${p.auth.method ? truncate(String(p.auth.method), 60) : 'unknown'} · ${p.auth.verified ? 'verified' : 'not verified'}`),
        ]),
        h('p', null, verdict),
        p.id==='muse' ? h('button',{type:'button',class:'btn','data-fk':'muse-refresh',onclick:refreshNow},'Refresh Muse connection') : null,
        href ? extLink(href, `Open ${p.name} account portal`, 'btn', `conn-portal:${p.id}`) : h('span', { class: 'muted small' }, 'No official portal link supplied'));
    }));
  }

  function renderDiagnostics() {
    const d = state.data;
    const meta = [
      kv('Local API', state.offline ? `not responding (${state.lastError || 'unknown'})` : d ? 'reachable' : 'waiting for first response'),
      kv('Last successful read', state.lastGoodAt ? timeEl(new Date(state.lastGoodAt).toISOString()) : '—'),
      kv('Consecutive failures', fmtInt(state.failures)),
      kv('Snapshot updated', d ? whenEl(d.updated_at) : '—'),
      kv('Server timezone', d && d.timezone ? String(d.timezone) : '—'),
      kv('Synchronizing', d ? (d.synchronizing ? 'yes' : 'no') : '—'),
      kv('Background sync', d && isNum(d.settings.refresh_interval_seconds) ? `every ${fmtDur(d.settings.refresh_interval_seconds * 1000)}` : d && isNum(d.refresh_interval_seconds) ? `every ${fmtDur(d.refresh_interval_seconds * 1000)}` : '—'),
      kv('This page', `re-reads the snapshot every ${POLL_MS / 1000}s while visible`),
    ];
    $('#diag-meta').replaceChildren(dl(meta));
    if (!d) {
      $('#diag-sources').replaceChildren(emptyMsg('No snapshot yet.'));
      $('#diag-warnings').replaceChildren(emptyMsg('No notices.'));
      return;
    }
    const rows = d.providers.map((p) => {
      const newest = newestIso(p.windows.map((w) => w.observed_at));
      const eff = effStatus(p);
      return [
        p.name,
        (STATUS_META[eff] || { label: eff }).label,
        p.source ? truncate(String(p.source), 60) : '—',
        `${p.auth.method ? truncate(String(p.auth.method), 40) : 'unknown'}${p.auth.verified ? ' ✓' : ' (unverified)'}`,
        p.last_attempt_at ? timeEl(p.last_attempt_at) : '—',
        newest ? timeEl(newest) : '—',
        p.error && (p.error.code || p.error.message) ? `${nonEmpty(p.error.code) || 'error'}: ${truncate(String(p.error.message || ''), 120)}` : '—',
      ];
    });
    $('#diag-sources').replaceChildren(buildTable('Provider source status', ['Provider', 'Status', 'Source', 'Auth', 'Last attempt', 'Newest reading', 'Error'], rows));
    $('#diag-warnings').replaceChildren(d.warnings.length
      ? h('ul', { class: 'plain' }, d.warnings.map((w) => h('li', null, truncate(w, 300))))
      : emptyMsg('No service notices.'));
  }

  function populateSettings() {
    if (state.settingsDirty || !state.data) return;
    const st = state.data.settings;
    const interval = isNum(st.refresh_interval_seconds) ? st.refresh_interval_seconds : state.data.refresh_interval_seconds;
    $('#set-warn').value = isNum(st.warn_remaining_pct) ? String(st.warn_remaining_pct) : '';
    $('#set-crit').value = isNum(st.critical_remaining_pct) ? String(st.critical_remaining_pct) : '';
    $('#set-interval').value = isNum(interval) ? String(interval) : '';
  }

  function readSettingsForm() {
    const interval = $('#set-interval').valueAsNumber;
    const warn = $('#set-warn').valueAsNumber;
    const crit = $('#set-crit').valueAsNumber;
    if (!Number.isInteger(interval) || interval < 10 || interval > 60) return { error: 'Background sync interval must be a whole number of seconds between 10 and 60 under the freshness policy.', field: 'set-interval' };
    if (!Number.isFinite(warn) || warn < 0 || warn > 100) return { error: 'The warning threshold must be a number between 0 and 100.', field: 'set-warn' };
    if (!Number.isFinite(crit) || crit < 0 || crit > 100) return { error: 'The critical threshold must be a number between 0 and 100.', field: 'set-crit' };
    if (crit >= warn) return { error: 'The critical threshold must be lower than the warning threshold.', field: 'set-crit' };
    return { values: { refresh_interval_seconds: interval, warn_remaining_pct: warn, critical_remaining_pct: crit } };
  }

  function setFormError(msg, field) {
    const el = $('#settings-error');
    for (const id of ['set-warn', 'set-crit', 'set-interval']) $(`#${id}`).removeAttribute('aria-invalid');
    if (!msg) { el.hidden = true; el.textContent = ''; return; }
    el.hidden = false;
    el.textContent = msg;
    if (field) {
      const input = $(`#${field}`);
      input.setAttribute('aria-invalid', 'true');
      input.focus();
    }
  }

  async function saveSettings(ev) {
    ev.preventDefault();
    if (state.saving) return;
    const r = readSettingsForm();
    if (r.error) { setFormError(r.error, r.field); return; }
    setFormError(null);
    state.saving = true;
    $('#settings-save').setAttribute('aria-disabled', 'true');
    try {
      const res = await api('/api/settings', { init: { method: 'POST', headers: WRITE_HEADERS, body: JSON.stringify(r.values) } });
      if (!res.ok) throw new Error(describeError(res));
      const saved = res.json && res.json.settings;
      if (!saved || typeof saved !== 'object') throw new Error('Unexpected response from /api/settings');
      if (state.data) {
        state.data.settings = { ...state.data.settings, ...saved };
        if (isNum(saved.refresh_interval_seconds)) state.data.refresh_interval_seconds = saved.refresh_interval_seconds;
      }
      state.settingsDirty = false;
      state.renderKey = '';
      toast('Settings saved.');
      if (state.data) withFocusKept(renderAll);
    } catch (err) {
      const msg = `Settings were not saved: ${errText(err)}`;
      setFormError(msg);
      toast(msg, 'error');
    } finally {
      state.saving = false;
      $('#settings-save').removeAttribute('aria-disabled');
    }
  }

  /* ---------- chrome ---------- */

  let toastTimer = null;
  function toast(msg, kind) {
    const el = $('#toast');
    el.className = `toast${kind === 'error' ? ' t-error' : ''}`;
    el.textContent = msg;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.textContent = ''; }, kind === 'error' ? 9000 : 4500);
  }

  function renderStatus() {
    const el = $('#status-line');
    const d = state.data;
    if (!d) { el.textContent = state.lastError ? 'No snapshot received yet' : 'Loading snapshot…'; return; }
    const interval = isNum(d.settings.refresh_interval_seconds) ? d.settings.refresh_interval_seconds : d.refresh_interval_seconds;
    setKids(el,
      h('span', null, 'Snapshot ', timeEl(d.updated_at, 'time unknown')),
      d.timezone ? h('span', null, `Times in ${d.timezone}`) : null,
      h('span', { class: d.synchronizing ? 'sync-on' : null }, d.synchronizing ? 'Syncing providers…' : isNum(interval) ? `Background sync every ${fmtDur(interval * 1000)}` : 'Background sync interval unknown'));
  }

  function renderBanner() {
    const el = $('#banner');
    if (!state.offline) {
      if (!el.hidden) { el.hidden = true; el.replaceChildren(); el.dataset.sig = ''; }
      return;
    }
    const sig = `${state.lastError}|${state.lastGoodAt}`;
    if (!el.hidden && el.dataset.sig === sig) return;
    el.dataset.sig = sig;
    el.hidden = false;
    setKids(el,
      h('strong', null, 'Local API not responding. '),
      state.data && state.lastGoodAt
        ? ['Showing last good data received ', timeEl(new Date(state.lastGoodAt).toISOString()), '. Values are marked stale and forecasts are withheld. ']
        : 'No data received yet. ',
      h('span', { class: 'muted' }, `(${state.lastError || 'no response'}; retrying every ${POLL_MS / 1000}s)`));
  }

  function syncRefreshButtons() {
    for (const b of document.querySelectorAll('[data-action="refresh"]')) {
      b.textContent = state.refreshing ? 'Requesting…' : 'Refresh now';
      if (state.refreshing) b.setAttribute('aria-disabled', 'true');
      else b.removeAttribute('aria-disabled');
    }
  }

  function withFocusKept(fn) {
    const a = document.activeElement;
    const fk = a && a.dataset ? a.dataset.fk : null;
    fn();
    if (!fk) return;
    for (const el of document.querySelectorAll('[data-fk]')) {
      if (el.dataset.fk === fk) {
        if (document.activeElement !== el) el.focus({ preventScroll: true });
        break;
      }
    }
  }

  function renderAll() {
    renderOverview();
    renderHistoryControls();
    renderConnections();
    renderDiagnostics();
    populateSettings();
    renderStatus();
    renderBanner();
    renderLocalSummary();
  }

  function expiredSig(d) {
    const now = Date.now();
    return d.providers.map((p) => p.windows.map((w) => {
      const t = parseTs(w.resets_at), f = parseTs(w.fresh_until);
      return t !== null && t <= now ? 'e' : f !== null && f <= now ? 's' : 'f';
    }).join('')).join('.');
  }

  function commit() {
    if (!state.data) { renderOverviewEmpty(); return; }
    const key = `${state.raw}|${state.offline}|${expiredSig(state.data)}`;
    if (key === state.renderKey) return;
    state.renderKey = key;
    withFocusKept(renderAll);
  }

  /* ---------- polling ---------- */

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    if (document.visibilityState !== 'visible') return;
    const fast = Date.now() < state.fastUntil && state.data && state.data.synchronizing;
    state.pollTimer = setTimeout(pollSnapshot, fast ? FAST_POLL_MS : POLL_MS);
  }

  function refreshSecondary() {
    const now = Date.now();
    if (state.view === 'history' && !state.history.loading && now - state.history.fetchedAt > HISTORY_REFRESH_MS) loadHistory();
    if (state.view === 'models' && !state.local.loading && now - state.local.fetchedAt > LOCAL_REFRESH_MS) loadLocal();
    if (state.view === 'overview' && !state.summary.loading && now - state.summary.fetchedAt > LOCAL_REFRESH_MS) loadSummary();
  }

  async function pollSnapshot() {
    if (state.polling) return;
    state.polling = true;
    clearTimeout(state.pollTimer);
    try {
      const res = await api('/api/plans');
      if (!res.ok || !res.json || !Array.isArray(res.json.providers)) {
        throw new Error(res.ok ? 'unexpected response from /api/plans' : describeError(res));
      }
      state.data = normalizeSnapshot(res.json);
      state.raw = res.text;
      state.offline = false;
      state.failures = 0;
      state.lastError = null;
      state.lastGoodAt = Date.now();
      commit();
      renderStatus();
      renderBanner();
      refreshSecondary();
      if (state.view === 'history' && !state.history.data && !state.history.loading) loadHistory();
    } catch (err) {
      state.failures += 1;
      state.offline = true;
      state.lastError = errText(err);
      commit();
      renderStatus();
      renderBanner();
      if (state.view === 'settings') renderDiagnostics();
    } finally {
      state.polling = false;
      schedulePoll();
    }
  }

  async function refreshNow() {
    if (state.refreshing) return;
    state.refreshing = true;
    syncRefreshButtons();
    try {
      const res = await api('/api/refresh', { init: { method: 'POST', headers: WRITE_HEADERS, body: '{}' } });
      if (res.status !== 202 && !res.ok) throw new Error(describeError(res));
      state.fastUntil = Date.now() + FAST_WINDOW_MS;
      toast('Refresh requested. Providers are syncing in the background.');
      clearTimeout(state.pollTimer);
      state.pollTimer = setTimeout(pollSnapshot, 800);
    } catch (err) {
      toast(`Refresh failed: ${errText(err)}`, 'error');
    } finally {
      state.refreshing = false;
      syncRefreshButtons();
    }
  }

  /* ---------- routing & events ---------- */

  function showView(v, focus) {
    state.view = v;
    for (const sec of document.querySelectorAll('.view')) sec.hidden = sec.dataset.view !== v;
    for (const a of document.querySelectorAll('.tabs a')) {
      if (a.dataset.view === v) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    }
    document.title = `Headroom · ${VIEW_TITLES[v]}`;
    if (focus) $(`#t-${v}`).focus();
    if (v === 'overview') loadSummary();
    else if (v === 'history') { renderHistoryControls(); loadHistory(); }
    else if (v === 'models') loadLocal();
    else if (v === 'settings') { renderDiagnostics(); populateSettings(); }
  }

  function historyLink(provider) {
    return `#history?provider=${encodeURIComponent(provider)}&hours=${state.history.hours}&metric=${state.history.metric}`;
  }

  function updateViewHash() {
    const hash=state.view==='history' ? historyLink(state.history.provider) : state.view==='models' ? `#models?days=${state.local.days}` : '#'+state.view;
    window.history.replaceState(null,'',hash);
  }

  const hashView = () => {
    const [v,query=''] = location.hash.replace(/^#/, '').split('?');
    const params=new URLSearchParams(query);
    if (v==='history') {
      const provider=params.get('provider');
      if (provider && /^[a-z0-9_-]{1,80}$/.test(provider)) state.history.provider=provider;
      const hours=Number(params.get('hours'));
      if ([6,24,72,168].includes(hours)) state.history.hours=hours;
      if (['remaining','used'].includes(params.get('metric'))) state.history.metric=params.get('metric');
    }
    if (v==='models' && params.has('days') && [0,1,7,30].includes(Number(params.get('days')))) state.local.days=Number(params.get('days'));
    return VIEWS.includes(v) ? v : null;
  };

  let resizeFrame = 0;
  function onResize() {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      if (state.view === 'history' && state.history.data) renderHistory();
      else if (state.view === 'models' && state.local.data) renderModels();
    });
  }

  function bindEvents() {
    window.addEventListener('hashchange', () => {
      const v = hashView();
      if (v) showView(v, true);
    });
    window.addEventListener('resize', onResize);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') { commit(); pollSnapshot(); }
      else clearTimeout(state.pollTimer);
    });
    for (const b of document.querySelectorAll('[data-action="refresh"]')) b.addEventListener('click', refreshNow);

    $('#hist-provider').addEventListener('change', (ev) => { state.history.provider = ev.target.value; state.history.data=null; state.history.error=null; updateViewHash(); renderHistory(); loadHistory(); });
    $('#hist-hours').addEventListener('change', (ev) => { state.history.hours = Number(ev.target.value); state.history.data=null; state.history.error=null; updateViewHash(); renderHistory(); loadHistory(); });
    $('#hist-metric').addEventListener('change', (ev) => { state.history.metric = ev.target.value === 'used' ? 'used' : 'remaining'; updateViewHash(); renderHistory(); });
    $('#hist-reload').addEventListener('click', () => loadHistory());
    $('#models-days').addEventListener('change', (ev) => { state.local.days = Number(ev.target.value); state.local.data=null; state.local.error=null; updateViewHash(); renderModels(); loadLocal(); });
    $('#models-reload').addEventListener('click', () => loadLocal());

    const form = $('#settings-form');
    form.addEventListener('submit', saveSettings);
    form.addEventListener('input', () => { state.settingsDirty = true; setFormError(null); });
    $('#settings-reset').addEventListener('click', () => {
      state.settingsDirty = false;
      setFormError(null);
      populateSettings();
    });
  }

  function init() {
    bindEvents();
    renderSkeleton();
    showView(hashView() || 'overview', false);
    pollSnapshot();
    setInterval(tickTimes, 1000);
  }

  init();
})();
