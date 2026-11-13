/* News digest dashboard, stocks.js v1.
   The owner's private stock section. It shows only while the site has a worker, an access key is
   set in this browser and the worker's GET /stocks/list answers. A visitor's page sends no request
   and shows nothing: the section and its nav link stay hidden and empty.
   - Every request goes through window.NewsDigest.workerFetch (app.js), so the key, the timeout and
     the guard's 401 rule (the key is forgotten) live in one place. When the key goes away this file
     empties the section and hides it again.
   - Stock data lives in memory only: never in localStorage, sessionStorage, the address or the
     page title, and nothing is logged.
   - The page runs under a strict Content-Security-Policy. Every value reaches the DOM through
     textContent, charts are SVG built with createElementNS and styled by classes only, and links
     are https to a short list of public data hosts (digest items: any https address). */
(() => {
  'use strict';

  const bridge = window.NewsDigest;
  const $ = (id) => document.getElementById(id);
  const section = $('stocks');
  const navLink = $('nav-stocks');
  if (!bridge || typeof bridge.workerFetch !== 'function' || !section || !navLink) return;
  if (!bridge.hasWorker()) return; // a site without a worker never shows this section
  const metaEl = $('st-meta');
  const toolbar = $('st-toolbar');
  const msgEl = $('st-msg');
  const manage = $('st-manage');
  const tabsEl = $('st-tabs');
  const panels = $('st-panels');

  // ---------- fixed lists, the same as scripts/stocks/__init__.py and config.yml (tests check) ----------
  const STOCKS_MODELS = ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5'];
  const DEFAULT_MODEL = 'claude-sonnet-5-5';
  const MODEL_NAMES = { 'claude-opus-5-5': 'Opus 5.5', 'claude-sonnet-5-5': 'Sonnet 5.5', 'claude-haiku-5-5': 'Haiku 5.5' };
  // US dollars per million tokens, as in config.yml (stocks.models).
  const MODEL_PRICES = {
    'claude-opus-5-5': { input: 4.0, output: 20.0, cache_read: 0.20 },
    'claude-sonnet-5-5': { input: 2.0, output: 10.0, cache_read: 0.10 },
    'claude-haiku-5-5': { input: 0.10, output: 0.50, cache_read: 0.01 },
  };
  // Typical tokens of one trading day: one assessment per stock and the market summary.
  // Adaptive thinking is billed as output, so output is a range.
  const TYPICAL = { stockIn: 15000, stockOut: [3000, 6000], overviewIn: 7000, overviewOut: [2000, 4000] };
  const TRADING_DAYS_A_MONTH = 21;
  // A run's spend cap in US dollars, as in config.yml (stocks.max_run_usd); the worker's list may send its own.
  const MAX_RUN_USD = 3.0;
  const TICKER_RE = /^[A-Z]{1,6}(?:-[A-Z]{1,3})?$/;
  const MAX_TICKERS = 30;
  const DISCLAIMER = 'Not financial advice. This is an automated reading of public data for one person, and it can be wrong. Do your own research before you buy or sell anything.';
  const LINK_HOSTS = ['sec.gov', 'house.gov', 'federalreserve.gov', 'bls.gov', 'bea.gov', 'finra.org', 'senate.gov'];
  const POLL_MS = 2 * 60 * 1000;          // after "Run analysis now": look for new results this often
  const POLL_FOR_MS = 40 * 60 * 1000;     // and for this long
  const RANGES = [['3M', 63], ['6M', 126], ['1Y', 252], ['All', 0]];
  const STOP_KINDS = { rate: true, unconfigured: true, network: true, timeout: true, nokey: true, noworker: true };
  const PREFETCH_MAX = 4;                 // documents loaded before their tab is opened
  const NY = 'America/New_York';
  const SVGNS = 'http://www.w3.org/2000/svg';

  // ---------- state, in memory only ----------
  let gen = 0;               // bumped by every teardown; an answer from before it is dropped
  let list = null;           // the normalized /stocks/list answer
  let overview = null;       // the overview document
  let overviewState = '';    // 'ok', 'none' (404) or 'error'
  const docs = new Map();    // ticker -> { doc } | { none: true } | { error: text }
  const loading = new Set();
  let docEpoch = 0;          // bumped when the documents are dropped; an answer from before it is dropped too
  let current = 'market';
  let manageOpen = false;
  let editing = false;
  let runBusy = false;
  let armed = '';            // the ticker whose Remove button waits for the second click
  let armTimer = 0;
  let runTimer = 0;
  let chartRange = 126;
  let chartStyle = 'candles';
  let pollBase = null;
  let pollUntil = 0;
  let pollTimer = 0;
  let minuteTimer = 0;
  let countdowns = [];
  const timers = new Set();
  const observers = new Set();

  // ---------- small helpers ----------
  const obj = (v) => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});
  const arr = (v) => (Array.isArray(v) ? v : []);
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  // A real calendar day as YYYY-MM-DD: 2026-13-01 or 2026-02-30 is not one (Intl throws on it).
  function isDay(v) {
    if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return false;
    const [y, m, d] = v.split('-').map(Number);
    const t = new Date(Date.UTC(y, m - 1, d));
    return t.getUTCFullYear() === y && t.getUTCMonth() === m - 1 && t.getUTCDate() === d;
  }
  function txt(v, max) {
    if (typeof v !== 'string') return '';
    const s = v.replace(/\s+/g, ' ').trim();
    return max && s.length > max ? s.slice(0, max - 1).replace(/\s+\S*$/, '') + '...' : s;
  }
  function paras(v, max) {
    if (typeof v !== 'string') return [];
    return v.split(/\n\s*\n/).map((p) => txt(p, max || 2000)).filter(Boolean).slice(0, 8);
  }
  function strs(v, max, n) { return arr(v).map((s) => txt(s, max)).filter(Boolean).slice(0, n || 12); }
  const oneOf = (v, allowed) => (allowed.indexOf(v) >= 0 ? v : '');
  function time(v) { const t = typeof v === 'string' ? Date.parse(v) : NaN; return Number.isFinite(t) ? t : null; }
  function plural(n, one, many) { return NF0.format(n) + ' ' + (n === 1 ? one : many); }
  function sentence(s) { s = txt(s, 200); if (!s) return ''; s = s.charAt(0).toUpperCase() + s.slice(1); return /[.!?]$/.test(s) ? s : s + '.'; }
  function normalizeTicker(v) {
    if (typeof v !== 'string') return null;
    const s = v.trim().toUpperCase().replace(/[./]/g, '-');
    return TICKER_RE.test(s) ? s : null;
  }
  function modelName(id) {
    if (MODEL_NAMES[id]) return MODEL_NAMES[id];
    return typeof id === 'string' && /^[a-z0-9.-]{1,60}$/.test(id) ? id : '';
  }

  function h(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null && text !== '') e.textContent = String(text);
    return e;
  }
  function put(parent, child) { parent.appendChild(child); return child; }
  function add(parent, ...kids) { kids.forEach((k) => { if (k) parent.appendChild(typeof k === 'string' ? document.createTextNode(k) : k); }); return parent; }
  function button(cls, text) { const b = h('button', cls, text); b.type = 'button'; return b; }
  function svg(tag, attrs, cls) {
    const e = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach((k) => e.setAttribute(k, String(attrs[k])));
    if (cls) e.setAttribute('class', cls);
    return e;
  }
  function later(fn, ms) {
    const id = setTimeout(() => { timers.delete(id); fn(); }, Math.max(0, Math.min(ms, 2147000000)));
    timers.add(id);
    return id;
  }
  function cancel(id) { if (id) { clearTimeout(id); timers.delete(id); } return 0; }

  // Only https, no user name or password, and only the public data hosts below (or, for an item
  // from the digest, any https host). Anything else is shown as plain text.
  function okUrl(u, anyHost) {
    if (typeof u !== 'string' || !u.trim()) return null;
    let p;
    try { p = new URL(u.trim()); } catch (e) { return null; }
    if (p.protocol !== 'https:' || p.username || p.password) return null;
    const host = p.hostname.toLowerCase();
    if (!anyHost && !LINK_HOSTS.some((d) => host === d || host.endsWith('.' + d))) return null;
    return p.href;
  }
  function link(url, text, anyHost, cls) {
    const href = okUrl(url, anyHost);
    if (!href) return h('span', cls || '', text);
    const a = h('a', cls || '', text);
    a.href = href;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    return a;
  }

  // ---------- numbers and dates ----------
  const NF = (o) => new Intl.NumberFormat('en-US', o);
  const NF0 = NF({ maximumFractionDigits: 0 });
  const NF1 = NF({ minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const NF2 = NF({ minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const NF2S = NF({ minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero' });
  const NF3 = NF({ minimumFractionDigits: 3, maximumFractionDigits: 3 });
  const NF4 = NF({ minimumFractionDigits: 4, maximumFractionDigits: 4 });
  const COMPACT = NF({ notation: 'compact', maximumFractionDigits: 1 });
  const USD = NF({ style: 'currency', currency: 'USD' });
  const NA = 'n/a';
  function money(x) { return isNum(x) ? (Math.abs(x) > 0 && Math.abs(x) < 1 ? NF({ style: 'currency', currency: 'USD', minimumFractionDigits: 4 }).format(x) : USD.format(x)) : NA; }
  function moneyIn(x, cur) {
    if (!isNum(x)) return NA;
    if (!/^[A-Z]{3}$/.test(cur || '') || cur === 'USD') return USD.format(x);
    try { return NF({ style: 'currency', currency: cur }).format(x); } catch (e) { return NF2.format(x) + ' ' + cur; }
  }
  function signedMoney(x) { return isNum(x) ? (x > 0 ? '+' : '') + USD.format(x) : NA; }
  function usd(x) { return isNum(x) ? '$' + NF2.format(x) : NA; }
  function usd4(x) { return isNum(x) ? '$' + (x < 1 ? NF4 : NF2).format(x) : NA; }
  function pct(x, d, signed) {
    if (!isNum(x)) return NA;
    const f = NF({ minimumFractionDigits: d === undefined ? 2 : d, maximumFractionDigits: d === undefined ? 2 : d, signDisplay: signed ? 'exceptZero' : 'auto' });
    return f.format(x) + '%';
  }
  function share(r, d) { return isNum(r) ? pct(r * 100, d === undefined ? 0 : d) : NA; }
  function compact(x) { return isNum(x) ? COMPACT.format(x) : NA; }
  function moneyCompact(x) { return isNum(x) ? (x < 0 ? '-$' : '$') + COMPACT.format(Math.abs(x)) : NA; }
  function signCls(x) { return isNum(x) && x > 0 ? 'st-up' : isNum(x) && x < 0 ? 'st-down' : ''; }

  const fmtDay = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
  const fmtDayShort = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
  const fmtWeekday = new Intl.DateTimeFormat('en-US', { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' });
  const fmtMonth = new Intl.DateTimeFormat('en-US', { month: 'short', year: 'numeric', timeZone: 'UTC' });
  const fmtMonthOnly = new Intl.DateTimeFormat('en-US', { month: 'short', timeZone: 'UTC' });
  const fmtStamp = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  const fmtClock = new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit' });
  const fmtLocalTime = new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
  const fmtNY = new Intl.DateTimeFormat('en-US', { timeZone: NY, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  const utcNoon = (d) => new Date(d + 'T12:00:00Z');
  function day(d) { return isDay(d) ? fmtDay.format(utcNoon(d)) : NA; }
  function dayShort(d) { return isDay(d) ? fmtDayShort.format(utcNoon(d)) : NA; }
  function stamp(iso) { const t = time(iso); return t === null ? NA : fmtStamp.format(t); }
  function clock(t) { return fmtClock.format(t); }
  function nyParts(t) { const o = {}; fmtNY.formatToParts(new Date(t)).forEach((p) => { o[p.type] = p.value; }); return o; }
  function nyDay(t) { const p = nyParts(t); return p.year + '-' + p.month + '-' + p.day; }
  // The moment a New York wall-clock time happens, without a time zone library.
  function nyInstant(d, hm) {
    const [y, mo, dd] = d.split('-').map(Number);
    const [hh, mm] = /^\d{2}:\d{2}$/.test(hm || '') ? hm.split(':').map(Number) : [12, 0];
    const want = Date.UTC(y, mo - 1, dd, hh, mm);
    let t = want;
    for (let k = 0; k < 2; k += 1) {
      const p = nyParts(t);
      t += want - Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute);
    }
    return t;
  }
  function dayDiff(a, b) { return Math.round((Date.parse(b + 'T00:00:00Z') - Date.parse(a + 'T00:00:00Z')) / 86400000); }

  // ---------- talking to the worker ----------
  async function api(path, init) {
    const g = gen;
    let r;
    try { r = await bridge.workerFetch(path, init); } catch (e) { r = { kind: 'network' }; }
    if (g !== gen) return { kind: 'stale' };
    if (r && r.kind === 'auth') { teardown(); return { kind: 'stale' }; } // app.js already forgot the key
    return r || { kind: 'network' };
  }
  function post(path, body) {
    return api(path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body || {}) });
  }
  function workerSays(r) { return txt(obj(r && r.body).error, 200); }
  function problem(r, what) {
    if (r.kind === 'unconfigured') return 'The worker has no access key configured yet.';
    if (r.kind === 'rate') return 'The worker is rate limited. Try again in a minute.';
    if (r.kind === 'network') return 'Could not reach the worker. Check your connection and try again.';
    if (r.kind === 'timeout') return 'The worker took too long. Try again in a minute.';
    if (r.kind === 'nokey' || r.kind === 'noworker') return 'Set the worker access key first.';
    const why = workerSays(r);
    return (what || 'The worker could not do that') + (r.status ? ' (HTTP ' + r.status + ').' : '.') + (why ? ' The worker said: ' + sentence(why) : '');
  }

  function normEntry(e) {
    e = obj(e);
    const t = normalizeTicker(e.ticker);
    if (!t) return null;
    return {
      ticker: t,
      status: oneOf(e.status, ['pending', 'ok', 'partial', 'unknown_ticker', 'no_prices', 'error']) || 'pending',
      name: txt(e.name, 80),
      close: isNum(e.close) ? e.close : null,
      change_pct: isNum(e.change_pct) ? e.change_pct : null,
      stance: oneOf(e.stance, ['bullish', 'neutral', 'bearish']),
      confidence: oneOf(e.confidence, ['low', 'medium', 'high']),
      data_as_of: isDay(e.data_as_of) ? e.data_as_of : '',
      hit: normHit(e.hit),
    };
  }
  // The learner's hit rate as the worker keeps it with the list (no document needed).
  function normHit(v) {
    const o = obj(v);
    if (!isNum(o.rate) || o.rate < 0 || o.rate > 1 || !Number.isInteger(o.n) || o.n < 1) return null;
    const kind = oneOf(o.kind, ['live', 'backtest']);
    return kind ? { rate: o.rate, n: o.n, kind, h: Number.isInteger(o.h) ? String(o.h) : '' } : null;
  }
  function normList(body) {
    const b = obj(body);
    const seen = new Set();
    const tickers = [];
    arr(b.tickers).forEach((e) => {
      const n = normEntry(e);
      if (n && !seen.has(n.ticker) && tickers.length < MAX_TICKERS) { seen.add(n.ticker); tickers.push(n); }
    });
    const st = obj(b.settings);
    const allowed = arr(st.allowed_models);
    // Both sides check the model: only names in this file's list and in the worker's list are offered.
    let models = STOCKS_MODELS.filter((m) => !allowed.length || allowed.indexOf(m) >= 0);
    if (!models.length) models = STOCKS_MODELS.slice();
    const model = models.indexOf(st.model) >= 0 ? st.model : (models.indexOf(DEFAULT_MODEL) >= 0 ? DEFAULT_MODEL : models[0]);
    const rf = obj(b.refresh);
    const max = Number.isInteger(b.max) && b.max > 0 && b.max <= MAX_TICKERS ? b.max : MAX_TICKERS;
    const cap = isNum(st.max_run_usd) && st.max_run_usd > 0 && st.max_run_usd <= 1000 ? st.max_run_usd : MAX_RUN_USD;
    return {
      tickers, max, model, models, cap,
      refresh: { requestedAt: time(rf.requested_at), availableAt: time(rf.available_at) },
      overviewAt: time(obj(b.overview).generated_at),
    };
  }
  function entry(t) { return list ? list.tickers.find((e) => e.ticker === t) || null : null; }

  // ---------- showing, hiding, loading ----------
  function reveal() { section.hidden = false; navLink.hidden = false; }
  function teardown() {
    gen += 1;
    timers.forEach((id) => clearTimeout(id));
    timers.clear();
    if (minuteTimer) clearInterval(minuteTimer);
    minuteTimer = 0;
    observers.forEach((o) => o.disconnect());
    observers.clear();
    list = null;
    overview = null;
    overviewState = '';
    dropDocs();
    current = 'market';
    manageOpen = false;
    editing = false;
    runBusy = false;
    armed = '';
    armTimer = 0;
    runTimer = 0;
    pollBase = null;
    pollUntil = 0;
    pollTimer = 0;
    countdowns = [];
    [metaEl, toolbar, msgEl, manage, tabsEl, panels].forEach((e) => { e.textContent = ''; });
    msgEl.classList.remove('is-info');
    manage.hidden = true;
    section.hidden = true;
    navLink.hidden = true;
  }
  // The owner sees why the section is empty (a worker without stocks code, a missing binding).
  function showOnly(text, more, retry) {
    [toolbar, manage, tabsEl, panels, metaEl].forEach((e) => { e.textContent = ''; });
    manage.hidden = true;
    reveal();
    showMsg('warn', text, more, retry);
  }

  async function load() {
    if (!bridge.hasKey()) return;
    const g = gen;
    const r = await api('/stocks/list');
    if (r.kind === 'stale') return;
    if (r.kind === 'ok') {
      list = normList(r.body);
      reveal();
      clearMsg();
      await loadOverview();
      if (g !== gen || !list) return;
      if (current !== 'market' && !entry(current)) current = 'market';
      renderAll();
      resumePolling();
      prefetch();
      return;
    }
    if (r.kind === 'http' && (r.status === 404 || r.status === 501)) {
      showOnly('This Worker has no stocks code yet. Paste worker/stocks.js into it.');
    } else if (r.kind === 'http' && r.status === 500 && /STOCKS_KV/.test(workerSays(r))) {
      showOnly('Bind a KV namespace named STOCKS_KV to the Worker.');
    } else {
      showOnly(problem(r, 'Could not load the stock list'), '', () => { teardown(); load(); });
    }
  }
  async function loadOverview() {
    const r = await api('/stocks/overview');
    if (r.kind === 'stale') return;
    const b = obj(r.body);
    if (r.kind === 'ok' && b.kind === 'stocks.overview') { overview = b; overviewState = 'ok'; }
    else if (r.kind === 'http' && r.status === 404) { overview = null; overviewState = 'none'; }
    else { overview = null; overviewState = 'error'; }
  }
  // After a run: everything again, the open tab kept when it still exists.
  async function reloadAll(text) {
    const g = gen;
    const r = await api('/stocks/list');
    if (r.kind === 'stale') return;
    if (r.kind !== 'ok') { showMsg('warn', problem(r, 'Could not load the new results')); return; }
    list = normList(r.body);
    dropDocs();
    await loadOverview();
    if (g !== gen || !list) return;
    if (current !== 'market' && !entry(current)) current = 'market';
    renderAll();
    if (text) showMsg('info', text);
    prefetch();
  }

  // The first few documents in the background, one at a time, so those tabs open at once; the
  // others load when their tab opens. The list's hit rates come with the list, so a long list never
  // costs a request per stock on page load (the guard allows 30 requests a minute). Documents stay
  // in memory only.
  function prefetch() {
    const g = gen;
    const ep = docEpoch;
    const queue = list.tickers.filter((e) => e.status !== 'pending' && !docs.has(e.ticker)).map((e) => e.ticker)
      .slice(0, PREFETCH_MAX);
    const run = async () => {
      while (queue.length && g === gen && ep === docEpoch) {
        const t = queue.shift();
        if (docs.has(t) || loading.has(t)) continue;
        const kind = await fetchDoc(t);
        if (STOP_KINDS[kind]) return;
      }
    };
    run();
  }
  // Every document and every request for one is forgotten (after a run, a request from before it
  // could otherwise bring back an old analysis).
  function dropDocs() { docEpoch += 1; docs.clear(); loading.clear(); }
  async function fetchDoc(t) {
    const ep = docEpoch;
    loading.add(t);
    const r = await api('/stocks/t/' + encodeURIComponent(t));
    if (r.kind === 'stale') return r.kind;
    if (ep !== docEpoch) return 'stale';
    loading.delete(t);
    const b = obj(r.body);
    if (r.kind === 'ok') {
      docs.set(t, b.kind === 'stocks.ticker' && b.ticker === t ? { doc: b } : { error: 'The worker sent something that is not an analysis of ' + t + '.' });
    } else if (r.kind === 'http' && r.status === 404) {
      docs.set(t, { none: true, why: workerSays(r) });
    } else {
      docs.set(t, { error: problem(r, 'Could not load the analysis') });
    }
    if (current === t) renderPanel();
    else if (current === 'market') refreshYours();
    return r.kind;
  }

  // ---------- messages ----------
  function showMsg(kind, text, more, retry) {
    msgEl.textContent = '';
    msgEl.classList.toggle('is-info', kind === 'info');
    add(msgEl, h('p', '', text));
    if (more) add(msgEl, h('p', '', more));
    if (retry) {
      const b = button('ghost', 'Try again');
      b.addEventListener('click', retry);
      add(msgEl, b);
    }
  }
  function clearMsg() { msgEl.textContent = ''; msgEl.classList.remove('is-info'); }

  // ---------- rendering: frame ----------
  function renderAll() {
    renderMeta();
    renderToolbar();
    renderManage();
    renderTabs();
    renderPanel();
  }
  function renderMeta() {
    const n = list.tickers.length;
    const asOf = overview && isDay(overview.data_as_of) ? overview.data_as_of : '';
    metaEl.textContent = plural(n, 'stock', 'stocks') + (asOf ? ', data as of ' + dayShort(asOf) : '');
  }

  function runWaitMs() { const at = list && list.refresh.availableAt; return at ? at - Date.now() : 0; }
  function polling() { return !!pollTimer && Date.now() < pollUntil; }
  function runLine() {
    const run = overview ? obj(overview.run) : {};
    const at = time(run.finished_at) || time(overview && overview.generated_at);
    if (!at) return overviewState === 'none' ? 'No run has finished yet. The first one fills this section.' : '';
    const parts = ['Last run ' + fmtStamp.format(at) + (run.reason === 'manual' ? ', started by hand.' : ', on schedule.')];
    if (Number.isInteger(run.tickers)) {
      const bits = [];
      if (Number.isInteger(run.ok)) bits.push(NF0.format(run.ok) + ' ok');
      if (Number.isInteger(run.partial) && run.partial) bits.push(NF0.format(run.partial) + ' partial');
      if (Number.isInteger(run.failed) && run.failed) bits.push(NF0.format(run.failed) + ' failed');
      parts.push(plural(run.tickers, 'stock', 'stocks') + (bits.length ? ': ' + bits.join(', ') : '') + '.');
    }
    if (isNum(run.cost_usd)) parts.push('Cost ' + usd4(run.cost_usd) + (modelName(run.model) ? ' with ' + modelName(run.model) : '') + '.');
    return parts.join(' ');
  }
  function renderToolbar() {
    runTimer = cancel(runTimer);
    const hadFocus = toolbar.contains(document.activeElement) ? document.activeElement.id : '';
    toolbar.textContent = '';
    const bar = h('div', 'st-bar');
    const info = h('div', 'st-bar-info');
    const line = runLine();
    if (line) add(info, h('p', 'st-run', line));
    const wait = runWaitMs();
    let next = '';
    if (polling()) next = 'Waiting for the results of the run you started. This page checks every 2 minutes while it is open.';
    else if (wait > 0) next = 'Run analysis now is available again at ' + clock(Date.now() + wait) + '.';
    if (next) add(info, h('p', 'st-next', next));
    const acts = h('div', 'st-bar-acts');
    const run = button('btn btn-primary btn-sm', runBusy ? 'Starting the run' : 'Run analysis now');
    run.id = 'st-run';
    run.disabled = runBusy || wait > 0;
    if (runBusy) run.setAttribute('aria-busy', 'true');
    run.addEventListener('click', onRun);
    const set = button('btn btn-sm', manageOpen ? 'Close settings' : 'Settings');
    set.id = 'st-settings';
    set.setAttribute('aria-expanded', String(manageOpen));
    set.setAttribute('aria-controls', 'st-manage');
    set.addEventListener('click', () => {
      manageOpen = !manageOpen;
      renderManage();
      renderToolbar();
      const first = manageOpen ? manage.querySelector('select, input') : null;
      if (first) first.focus(); else { const s = $('st-settings'); if (s) s.focus(); }
    });
    add(acts, run, set);
    add(bar, info, acts);
    add(toolbar, bar);
    if (wait > 0) runTimer = later(renderToolbar, wait + 1000);
    if (hadFocus) { const f = $(hadFocus); if (f && !f.disabled) f.focus(); }
  }

  // ---------- settings: model, the list, add ----------
  // A trading day's rough cost from typical token counts, held at the run's spend cap: a run stops
  // asking the model there and the stocks after it get levels from the code. fit: the stocks that
  // fit under the cap at the high end of the typical cost.
  function costRange(model, n, cap) {
    const p = MODEL_PRICES[model];
    if (!p) return null;
    const one = (inT, outT) => (inT * p.input + outT * p.output) / 1e6;
    const stock = [0, 1].map((k) => one(TYPICAL.stockIn, TYPICAL.stockOut[k]));
    const ov = [0, 1].map((k) => one(TYPICAL.overviewIn, TYPICAL.overviewOut[k]));
    const [lo, hi] = [0, 1].map((k) => n * stock[k] + ov[k]);
    return { lo: Math.min(lo, cap), hi: Math.min(hi, cap), capped: hi > cap, fit: Math.max(0, Math.floor((cap - ov[1]) / stock[1])) };
  }
  // Cents, "less than $0.01" below a cent, and whole dollars from $10 when whole is set.
  function roughUsd(x, whole) {
    if (x < 0.01) return 'less than $0.01';
    return whole && x >= 10 ? '$' + NF0.format(x) : usd(x);
  }
  function roughRange(lo, hi, whole) {
    const a = roughUsd(lo, whole);
    const b = roughUsd(hi, whole);
    if (a === b) return lo < 0.01 ? a : 'about ' + a;
    return lo < 0.01 ? 'up to about ' + b : 'about ' + a + ' to ' + b;
  }
  function capLine(c, cap, name, max) {
    const at = 'A run stops asking the model at its ' + usd(cap) + ' spend cap';
    if (c.fit < 1) return at + ', which leaves no room for a stock with ' + name + '. Every stock gets levels from the code.';
    if (c.capped) return at + ', which fits about ' + plural(c.fit, 'stock', 'stocks') + ' with ' + name + '. The others get levels from the code.';
    return at + ', which fits ' + (c.fit >= max ? 'a full list of ' + plural(max, 'stock', 'stocks') : 'about ' + plural(c.fit, 'stock', 'stocks')) + ' with ' + name + '.';
  }
  function renderManage() {
    const focusId = manage.contains(document.activeElement) ? document.activeElement.id || '' : '';
    const focusTicker = manage.contains(document.activeElement) ? document.activeElement.getAttribute('data-ticker') || '' : '';
    const draft = $('st-ticker') ? $('st-ticker').value : '';
    manage.textContent = '';
    manage.hidden = !manageOpen;
    if (!manageOpen || !list) return;
    const n = list.tickers.length;
    const box = h('div', 'fig st-card st-mgrid');

    // the model
    const m = h('div', 'st-mcol');
    const lab = h('label', 'col-h', 'Model for the assessments');
    lab.htmlFor = 'st-model';
    const sel = h('select', 'input');
    sel.id = 'st-model';
    list.models.forEach((id) => {
      const c = costRange(id, n, list.cap);
      const o = h('option', '', MODEL_NAMES[id] + (c ? ', ' + roughRange(c.lo, c.hi) + ' a day' + (c.capped ? ', capped' : '') : ''));
      o.value = id;
      sel.appendChild(o);
    });
    sel.value = list.model;
    sel.disabled = editing;
    sel.addEventListener('change', onModel);
    const c = costRange(list.model, n, list.cap);
    // Whole dollars from $10 a month; cents below, so Haiku's few cents do not read as $0.
    const month = c ? roughRange(c.lo * TRADING_DAYS_A_MONTH, c.hi * TRADING_DAYS_A_MONTH, true) : '';
    add(m, lab, sel,
      h('p', 'hint', 'Rough cost for ' + plural(n, 'stock', 'stocks') + ' and the market summary on each trading day, from typical token counts' +
        (month ? ': ' + month + ' a month with ' + MODEL_NAMES[list.model] + '.' : '.')),
      c ? h('p', 'hint', capLine(c, list.cap, MODEL_NAMES[list.model], list.max)) : null,
      h('p', 'hint', 'Saved in the worker. The next run uses it.'));

    // the list
    const l = h('div', 'st-mcol');
    add(l, h('p', 'col-h', 'Your list, ' + n + ' of ' + list.max));
    if (!n) add(l, h('p', 'hint', 'No stocks yet. Add a ticker on the right.'));
    else {
      const ul = h('ul', 'st-mlist');
      list.tickers.forEach((e) => {
        const li = h('li', 'st-mrow');
        add(li, h('span', 'st-msym', e.ticker), h('span', 'st-mname', e.name || (e.status === 'pending' ? 'Waiting for its first run' : '')));
        const ask = armed === e.ticker;
        const b = button('ghost st-remove' + (ask ? ' is-armed' : ''), ask ? 'Confirm remove' : 'Remove');
        b.setAttribute('data-ticker', e.ticker);
        b.setAttribute('aria-label', (ask ? 'Confirm: remove ' : 'Remove ') + e.ticker + (ask ? ', with its analysis and what it learned' : ''));
        b.disabled = editing;
        b.addEventListener('click', () => onRemove(e.ticker));
        add(li, b);
        ul.appendChild(li);
      });
      add(l, ul);
      if (armed) add(l, h('p', 'hint hint-note', 'Removing ' + armed + ' deletes its analysis and everything it learned. Press Confirm remove to go ahead.'));
    }

    // add a ticker
    const a = h('form', 'st-mcol st-add');
    a.noValidate = true;
    const al = h('label', 'col-h', 'Add a stock');
    al.htmlFor = 'st-ticker';
    const row = h('div', 'x-edit-row');
    const input = h('input', 'input');
    input.id = 'st-ticker';
    input.type = 'text';
    input.placeholder = 'Ticker, such as BRK-B';
    input.maxLength = 12;
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.setAttribute('autocapitalize', 'characters');
    input.setAttribute('autocorrect', 'off');
    input.value = draft;
    const go = h('button', 'btn btn-sm', 'Add');
    go.type = 'submit';
    go.id = 'st-add-btn';
    go.disabled = editing || n >= list.max;
    if (editing) go.setAttribute('aria-busy', 'true');
    add(row, input, go);
    add(a, al, row, h('p', 'hint', n >= list.max
      ? 'The list is full. Remove a stock to add another.'
      : '1 to 6 letters, with an optional class such as BRK-B. Up to ' + list.max + ' stocks. A new stock gets its first analysis with the next run.'));
    a.addEventListener('submit', onAdd);

    add(box, m, l, a);
    manage.appendChild(box);
    let f = focusTicker ? manage.querySelector('[data-ticker="' + focusTicker + '"]') : null;
    if (!f && focusId) f = $(focusId);
    if (f && !f.disabled) f.focus();
  }

  async function onModel(ev) {
    const sel = ev.target;
    const want = sel.value;
    if (editing || STOCKS_MODELS.indexOf(want) < 0 || list.models.indexOf(want) < 0 || want === list.model) { sel.value = list.model; return; }
    editing = true;
    renderManage();
    clearMsg();
    const r = await post('/stocks/settings', { model: want });
    if (r.kind === 'stale') return;
    editing = false;
    const got = obj(r.body).model;
    if (r.kind === 'ok' && obj(r.body).ok === true && STOCKS_MODELS.indexOf(got) >= 0) {
      list.model = got;
      showMsg('info', 'Saved. The next run uses ' + MODEL_NAMES[got] + '.');
    } else if (r.kind === 'http' && r.status === 400) {
      showMsg('warn', 'The worker did not accept that model.', 'Nothing was changed.');
    } else {
      showMsg('warn', problem(r, 'Could not save the model'), 'Nothing was changed.');
    }
    renderManage();
    const s = $('st-model');
    if (s) s.focus();
  }
  function onAdd(ev) {
    ev.preventDefault();
    if (editing) return;
    const input = $('st-ticker');
    const t = normalizeTicker(input.value);
    let bad = '';
    if (!t) bad = 'Type a ticker: 1 to 6 letters, with an optional class such as BRK-B.';
    else if (entry(t)) bad = t + ' is already in the list.';
    else if (list.tickers.length >= list.max) bad = 'The list holds ' + list.max + ' stocks at most. Remove one first.';
    if (bad) { showMsg('warn', bad); input.focus(); return; }
    editTickers('add', t);
  }
  function onRemove(t) {
    if (editing) return;
    if (armed !== t) {
      armed = t;
      armTimer = cancel(armTimer);
      armTimer = later(() => { armed = ''; renderManage(); }, 8000);
      renderManage();
      const b = manage.querySelector('[data-ticker="' + t + '"]');
      if (b) b.focus();
      return;
    }
    armTimer = cancel(armTimer);
    editTickers('remove', t);
  }
  async function editTickers(action, t) {
    editing = true;
    renderManage();
    clearMsg();
    const r = await post('/stocks/tickers', { action, ticker: t });
    if (r.kind === 'stale') return;
    editing = false;
    armed = '';
    const b = obj(r.body);
    if (r.kind === 'ok' && b.ok === true) {
      if (action === 'remove') { docs.delete(t); if (current === t) current = 'market'; }
      if (action === 'add') { const input = $('st-ticker'); if (input) input.value = ''; }
      const fresh = await api('/stocks/list');
      if (fresh.kind === 'stale') return;
      if (fresh.kind === 'ok') list = normList(fresh.body);
      else {
        // the edit went through; build the list from the answer until the next load
        const names = arr(b.tickers).map(normalizeTicker).filter(Boolean);
        list.tickers = names.map((n) => entry(n) || normEntry({ ticker: n, status: 'pending' }));
      }
      if (current !== 'market' && !entry(current)) current = 'market';
      renderAll();
      if (action === 'add') {
        showMsg('info', b.changed === false ? t + ' was already in the list.' : 'Added ' + t + '. Its first analysis comes with the next run.',
          b.changed === false ? '' : 'Press Run analysis now to get it sooner.');
      } else {
        showMsg('info', b.changed === false ? t + ' was not in the list.' : 'Removed ' + t + ', with its analysis and everything it had learned.');
      }
    } else {
      if (r.kind === 'http' && r.status === 409) showMsg('warn', sentence(workerSays(r)) || 'The list is full.', 'Nothing was changed.');
      else if (r.kind === 'http' && r.status === 400) showMsg('warn', 'The worker did not accept that ticker.', workerSays(r) ? 'It said: ' + sentence(workerSays(r)) : 'Nothing was changed.');
      else showMsg('warn', problem(r, 'Could not save the change'), 'Nothing was changed.');
      renderManage();
    }
    const focus = action === 'add' ? $('st-ticker') : $('st-ticker') || $('st-settings');
    if (focus) focus.focus();
  }

  // ---------- run analysis now ----------
  async function onRun() {
    if (runBusy || runWaitMs() > 0) return;
    runBusy = true;
    renderToolbar();
    clearMsg();
    const before = overview ? time(overview.generated_at) : null;
    const r = await post('/stocks/refresh', {});
    if (r.kind === 'stale') return;
    runBusy = false;
    const b = obj(r.body);
    if (r.kind === 'ok' && b.ok === true) {
      list.refresh.requestedAt = time(b.requested_at) || Date.now();
      list.refresh.availableAt = time(b.available_at) || Date.now() + 30 * 60000;
      startPolling(before);
      showMsg('info', 'Started. Results in about 15 minutes.', 'This page checks for them every 2 minutes while it is open.');
    } else if (r.kind === 'http' && r.status === 429) {
      const secs = isNum(b.retry_after_seconds) && b.retry_after_seconds > 0 ? Math.min(b.retry_after_seconds, 86400) : 30 * 60;
      list.refresh.availableAt = Date.now() + secs * 1000;
      showMsg('info', 'A run was started recently.', 'Run analysis now is available again at ' + clock(list.refresh.availableAt) + '.');
    } else if (r.kind === 'http' && r.status === 500) {
      showMsg('warn', 'The worker cannot start runs yet.', workerSays(r) ? 'It said: ' + sentence(workerSays(r)) : 'Set GH_REPO and GH_DISPATCH_TOKEN on the worker.');
    } else if (r.kind === 'http' && r.status === 502) {
      showMsg('warn', 'GitHub did not take the request.', 'Try again in a few minutes.');
    } else {
      showMsg('warn', problem(r, 'Could not start a run'));
    }
    renderToolbar();
    // The clicked button is gone or turned off, so the message takes the focus.
    const again = $('st-run');
    if (again && !again.disabled) again.focus();
    else { msgEl.tabIndex = -1; msgEl.focus(); }
  }
  function startPolling(base) {
    pollBase = base;
    pollUntil = Date.now() + POLL_FOR_MS;
    schedulePoll();
  }
  function schedulePoll() {
    pollTimer = cancel(pollTimer);
    if (Date.now() >= pollUntil) { pollUntil = 0; return; }
    pollTimer = later(pollTick, POLL_MS);
  }
  async function pollTick() {
    pollTimer = 0;
    if (document.visibilityState === 'hidden') { schedulePoll(); return; }
    const r = await api('/stocks/overview');
    if (r.kind === 'stale') return;
    const at = r.kind === 'ok' ? time(obj(r.body).generated_at) : null;
    if (at && at !== pollBase) {
      pollUntil = 0;
      await reloadAll('New results are in.');
      return;
    }
    schedulePoll();
    renderToolbar();
  }
  // A run the owner started a little while ago, and whose results are not in yet.
  function resumePolling() {
    const req = list.refresh.requestedAt;
    const done = overview ? time(overview.generated_at) : null;
    if (req && Date.now() - req < POLL_FOR_MS && (!done || done < req) && !polling()) {
      pollBase = done;
      pollUntil = req + POLL_FOR_MS;
      schedulePoll();
      renderToolbar();
    }
  }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && pollTimer && Date.now() < pollUntil) { pollTimer = cancel(pollTimer); pollTick(); }
  });

  // ---------- tabs ----------
  function tabKeys() { return ['market'].concat(list.tickers.map((e) => e.ticker)); }
  function renderTabs() {
    tabsEl.textContent = '';
    const bar = h('div', 'st-tablist');
    bar.setAttribute('role', 'tablist');
    bar.setAttribute('aria-label', 'Market and stocks');
    tabKeys().forEach((k, i) => {
      const b = button('st-tab');
      b.id = 'st-tab-' + i;
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-controls', 'st-panel');
      b.setAttribute('data-key', k);
      if (k === 'market') b.textContent = 'Market';
      else {
        const e = entry(k);
        add(b, h('span', 'st-tab-t', k));
        if (e && isNum(e.change_pct)) add(b, ' ', h('span', 'st-tab-c ' + signCls(e.change_pct), pct(e.change_pct, 1, true)));
        else if (e && e.status === 'pending') add(b, ' ', h('span', 'st-tab-c', 'New'));
      }
      b.addEventListener('click', () => select(k));
      bar.appendChild(b);
    });
    bar.addEventListener('keydown', (ev) => {
      const keys = tabKeys();
      const at = keys.indexOf(current);
      let to = -1;
      if (ev.key === 'ArrowRight') to = (at + 1) % keys.length;
      else if (ev.key === 'ArrowLeft') to = (at - 1 + keys.length) % keys.length;
      else if (ev.key === 'Home') to = 0;
      else if (ev.key === 'End') to = keys.length - 1;
      if (to < 0) return;
      ev.preventDefault();
      select(keys[to], true);
    });
    tabsEl.appendChild(bar);
    markTabs();
  }
  function markTabs() {
    Array.from(tabsEl.querySelectorAll('[role="tab"]')).forEach((b) => {
      const on = b.getAttribute('data-key') === current;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    });
  }
  function select(k, focus) {
    if (tabKeys().indexOf(k) < 0) k = 'market';
    current = k;
    markTabs();
    renderPanel();
    const b = tabsEl.querySelector('[data-key="' + k + '"]');
    if (b) {
      if (focus) b.focus();
      if (typeof b.scrollIntoView === 'function') b.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  }
  function renderPanel() {
    // A rebuilt panel keeps the keyboard focus: on the same stock's button in Your stocks, else the panel.
    const active = document.activeElement;
    const inside = !!active && panels.contains(active);
    const back = inside && active.classList.contains('st-open') ? active.getAttribute('data-ticker') || '' : '';
    observers.forEach((o) => o.disconnect());
    observers.clear();
    countdowns = [];
    if (minuteTimer) { clearInterval(minuteTimer); minuteTimer = 0; }
    panels.textContent = '';
    const p = h('div', 'st-panel');
    p.id = 'st-panel';
    p.setAttribute('role', 'tabpanel');
    p.tabIndex = 0;
    const tab = tabsEl.querySelector('[data-key="' + current + '"]');
    if (tab) p.setAttribute('aria-labelledby', tab.id);
    panels.appendChild(p);
    try {
      if (current === 'market') renderMarket(p); else renderTicker(p, current);
    } catch (e) {
      // the last resort: whatever broke, the panel says so and the disclaimer stays
      p.textContent = '';
      add(p, add(h('div', 'st-grid'), add(card('Not available', '', 'st-c12'), h('p', 'hint', 'This tab could not be shown.'))), footer([], DISCLAIMER));
    }
    if (inside) {
      const f = back ? p.querySelector('.st-open[data-ticker="' + back + '"]') : null;
      (f || p).focus({ preventScroll: true });
    }
  }

  // ---------- building blocks ----------
  function card(title, tag, cls) {
    const c = h('section', 'fig st-card' + (cls ? ' ' + cls : ''));
    const head = h('div', 'st-card-head');
    add(head, h('h3', 'label st-h', title));
    if (tag) add(head, h('span', 'tag', tag));
    c.appendChild(head);
    return c;
  }
  // One card from its builder; a value that breaks the builder leaves that card alone "Not available".
  function safe(title, cls, build) {
    try { return build(); } catch (e) {
      const c = card(title, '', cls);
      add(c, h('p', 'hint', 'Not available. This part of the page could not be shown.'));
      return c;
    }
  }
  function note(text, cls) { return text ? h('p', 'hint ' + (cls || ''), text) : null; }
  function notes(list2) { const out = h('div', 'st-notes'); strs(list2, 400, 6).forEach((t) => add(out, h('p', 'hint', t))); return out.firstChild ? out : null; }
  function read(text) {
    const ps = paras(text, 900);
    if (!ps.length) return null;
    const box = h('div', 'st-read');
    add(box, h('p', 'st-read-h', "Claude's read"));
    ps.forEach((t) => add(box, h('p', '', t)));
    return box;
  }
  function kv(rows) {
    const dl = h('dl', 'st-kv');
    rows.forEach((r) => {
      if (!r) return;
      const [k, v, cls] = r;
      const dd = h('dd', cls || '');
      if (v instanceof Node) dd.appendChild(v); else dd.textContent = v === undefined || v === null || v === '' ? NA : String(v);
      add(dl, h('dt', '', k), dd);
    });
    return dl;
  }
  // cols: [label, 'num'?]; rows: arrays of cells (string, Node, or [text, cls]). A table marked
  // stack turns into label and value rows on a phone instead of scrolling sideways.
  function table(label, cols, rows, stack) {
    const wrap = h('div', 'st-tw');
    wrap.setAttribute('role', 'region');
    wrap.setAttribute('aria-label', label);
    wrap.tabIndex = 0;
    const t = h('table', 'st-table' + (stack ? ' st-stack-sm' : ''));
    const thead = h('thead');
    const tr = h('tr');
    cols.forEach(([c, cls]) => { const th = h('th', cls || '', c); th.scope = 'col'; tr.appendChild(th); });
    thead.appendChild(tr);
    const tb = h('tbody');
    rows.forEach((row) => {
      const r = h('tr');
      row.forEach((cell, i) => {
        const td = h('td', cols[i] && cols[i][1] ? cols[i][1] : '');
        if (cols[i]) td.setAttribute('data-label', cols[i][0]);
        if (cell instanceof Node) td.appendChild(cell);
        else if (Array.isArray(cell)) { td.textContent = cell[0] === null || cell[0] === undefined || cell[0] === '' ? NA : String(cell[0]); if (cell[1]) td.className = ((cols[i] && cols[i][1]) || '') + ' ' + cell[1]; }
        else td.textContent = cell === null || cell === undefined || cell === '' ? NA : String(cell);
        if (!td.textContent) td.classList.add('is-empty');
        r.appendChild(td);
      });
      tb.appendChild(r);
    });
    add(t, thead, tb);
    wrap.appendChild(t);
    return wrap;
  }
  function chip(text, cls) { return h('span', 'chip ' + (cls || ''), text); }
  function stanceChip(s) { return s ? chip(s === 'bullish' ? 'Bullish' : s === 'bearish' ? 'Bearish' : 'Neutral', s === 'bullish' ? 'c-cyan' : s === 'bearish' ? 'c-ox' : '') : null; }
  function bullets(items, cls) {
    if (!items.length) return null;
    const ul = h('ul', 'st-bullets' + (cls ? ' ' + cls : ''));
    items.forEach((t) => add(ul, t instanceof Node ? add(h('li'), t) : h('li', '', t)));
    return ul;
  }
  function footer(lines, disclaimer) {
    const f = h('footer', 'st-foot');
    lines.filter(Boolean).forEach((t) => add(f, h('p', '', t)));
    add(f, h('p', 'st-disclaimer', txt(disclaimer, 400) || DISCLAIMER));
    return f;
  }
  function costText(c) {
    c = obj(c);
    if (!isNum(c.cost_usd)) return '';
    const tok = isNum(c.input_tokens) && isNum(c.output_tokens) ? ' (' + NF0.format(c.input_tokens) + ' tokens in, ' + NF0.format(c.output_tokens) + ' out)' : '';
    return (c.cost_estimated ? 'about ' : '') + usd4(c.cost_usd) + tok;
  }

  // ---------- the Market tab ----------
  function renderMarket(p) {
    const grid = h('div', 'st-grid');
    if (overviewState !== 'ok') {
      const c = card('Market overview');
      add(c, h('p', '', overviewState === 'none' ? 'No market overview yet. It comes with the first run.' : 'The market overview could not be loaded.'));
      if (overviewState === 'error') {
        const b = button('ghost', 'Try again');
        b.addEventListener('click', async () => { await loadOverview(); if (list) { renderMeta(); renderToolbar(); renderPanel(); } });
        add(c, b);
      }
      c.classList.add('st-c12');
      add(grid, c, safe('Your stocks', 'st-c12', yoursCard));
      add(p, grid, footer([], DISCLAIMER));
      return;
    }
    const ov = overview;
    add(grid, add(h('div', 'st-c7 st-stack'), safe('What matters this week', 'st-summary', () => summaryCard(ov)), safe('Macro news', 'st-newscard', () => newsCard(ov))),
      safe('Upcoming catalysts', 'st-c5', () => calendarCard(ov)), safe('Macro dashboard', 'st-c12', () => macroCard(ov)), safe('Your stocks', 'st-c12', yoursCard));
    p.appendChild(grid);
    const s = obj(ov.summary);
    const lines = [];
    if (s.source === 'model' && modelName(ov.model)) lines.push('Summary by ' + modelName(ov.model) + (costText(ov.cost) ? ', cost ' + costText(ov.cost) : '') + '.');
    lines.push(madeLine(ov));
    p.appendChild(footer(lines, ov.disclaimer));
    minuteTimer = setInterval(tickCountdowns, 60000);
  }

  function summaryCard(ov) {
    const s = obj(ov.summary);
    const c = card('What matters this week', s.source === 'model' && modelName(ov.model) ? 'By ' + modelName(ov.model) : '', 'st-summary');
    const headline = txt(s.headline, 160);
    const body = paras(s.summary, 1200);
    if (s.source !== 'model' || (!headline && !body.length)) { add(c, h('p', 'hint', 'No summary today.')); return c; }
    if (headline) add(c, h('p', 'st-headline', headline));
    body.forEach((t) => add(c, h('p', 'st-lede', t)));
    const week = arr(s.this_week).map(obj).filter((w) => isDay(w.date) && txt(w.event, 160)).slice(0, 8);
    if (week.length) {
      add(c, h('h4', 'st-sub', 'This week'));
      const ol = h('ol', 'st-week');
      week.forEach((w) => {
        const li = h('li');
        add(li, h('span', 'st-week-d', fmtWeekday.format(utcNoon(w.date))), h('span', 'st-week-e', txt(w.event, 160)));
        if (txt(w.why_it_matters, 400)) add(li, h('span', 'st-week-w', txt(w.why_it_matters, 400)));
        ol.appendChild(li);
      });
      add(c, ol);
    }
    const watch = strs(s.watch, 300, 5);
    const risks = strs(s.risks, 300, 5);
    if (watch.length || risks.length) {
      const two = h('div', 'st-two');
      if (watch.length) add(two, add(h('div'), h('h4', 'st-sub', 'Watch'), bullets(watch)));
      if (risks.length) add(two, add(h('div'), h('h4', 'st-sub', 'Risks'), bullets(risks)));
      add(c, two);
    }
    return c;
  }

  const KIND_NAMES = { fomc: 'Fed', cpi: 'Inflation', ppi: 'Inflation', pce: 'Inflation', jobs: 'Jobs', jolts: 'Jobs', gdp: 'Growth' };
  function countdown(ev, inst) {
    const days = dayDiff(nyDay(Date.now()), ev.date);
    if (days > 1) return 'In ' + days + ' days';
    if (days === 1) return 'Tomorrow';
    if (days === 0) {
      if (!ev.time) return 'Today';
      const left = inst - Date.now();
      if (left <= 0) return 'Earlier today';
      const mins = Math.round(left / 60000);
      if (mins < 60) return 'In ' + Math.max(1, mins) + ' min';
      return 'Today, in ' + Math.floor(mins / 60) + ' h' + (mins % 60 ? ' ' + (mins % 60) + ' min' : '');
    }
    if (days === -1) return 'Yesterday';
    return -days + ' days ago';
  }
  function tickCountdowns() { countdowns.forEach(({ el, ev, inst }) => { el.textContent = countdown(ev, inst); }); }
  function calendarCard(ov) {
    const c = card('Upcoming catalysts', 'Next 30 days', 'st-c5');
    const today = nyDay(Date.now());
    const evs = arr(ov.calendar).map(obj).filter((e) => isDay(e.date) && txt(e.title, 200)).slice(0, 40);
    if (!evs.length) { add(c, h('p', 'hint', 'No releases on the calendar for the next 30 days.')); return c; }
    const rows = evs.map((e) => {
      const ev = { date: e.date, time: /^\d{2}:\d{2}$/.test(e.time || '') ? e.time : '' };
      const inst = nyInstant(ev.date, ev.time);
      const when = h('div', 'st-when');
      const cd = h('span', 'st-count', countdown(ev, inst));
      add(when, h('span', 'st-when-d', fmtWeekday.format(utcNoon(e.date))), cd);
      countdowns.push({ el: cd, ev, inst });
      const what = h('div', 'st-what');
      const src = typeof e.source === 'string' && !/\.ics(?:$|\?)/i.test(e.source) ? e.source : '';
      add(what, src ? link(src, txt(e.title, 200)) : h('span', '', txt(e.title, 200)));
      if (KIND_NAMES[e.kind]) add(what, h('span', 'st-kind', KIND_NAMES[e.kind]));
      const f = obj(e.flags);
      const words = [];
      if (f.press_conference === true) words.push('Press conference');
      if (f.projections === true) words.push('Projections');
      if (f.tentative === true) words.push('Tentative date');
      if (isDay(f.start_date) && f.start_date !== e.date) words.push('Meeting starts ' + dayShort(f.start_date));
      const t = ev.time ? fmtLocalTime.format(inst) : '';
      const timeCell = h('span', '', t || NA);
      if (ev.time) timeCell.title = ev.time + ' in New York';
      const row = [when, what, timeCell, h('span', '', words.join(', '))];
      row.past = e.date < today;
      return row;
    });
    const tw = table('Upcoming catalysts', [['When'], ['Event'], ['Time', 'nowrap'], ['Notes']], rows, true);
    Array.from(tw.querySelectorAll('tbody tr')).forEach((tr, i) => { if (rows[i].past) tr.classList.add('st-past'); });
    add(c, tw, note('Times are shown in your time zone. Release dates come from the Federal Reserve, BLS and BEA calendars.'));
    return c;
  }

  function signedRow(r) { return r.id === 'payrolls' || /change/i.test(r.label); }
  function unitText(v, unit, signed) {
    if (!isNum(v)) return NA;
    const sd = signed ? 'exceptZero' : 'auto';
    if (unit === 'percent') return NF({ minimumFractionDigits: 1, maximumFractionDigits: 2, signDisplay: sd }).format(v) + '%';
    if (unit === 'points') return NF({ minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: sd }).format(v) + ' pts';
    if (unit === 'dollars') return (v < 0 ? '-' : signed && v > 0 ? '+' : '') + '$' + NF2.format(Math.abs(v));
    if (unit === 'thousands') return NF({ maximumFractionDigits: 0, signDisplay: sd }).format(v) + 'K';
    return NF({ minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: sd }).format(v);
  }
  function changeText(v, unit) {
    if (unit === 'percent' || unit === 'points') return NF2S.format(v) + (Math.abs(v) === 1 ? ' pt' : ' pts');
    return unitText(v, unit, true);
  }
  function asOfText(d, freq) {
    if (!isDay(d)) return NA;
    if (freq === 'monthly') return fmtMonth.format(utcNoon(d));
    if (freq === 'quarterly') return 'Q' + (Math.floor((Number(d.slice(5, 7)) - 1) / 3) + 1) + ' ' + d.slice(0, 4);
    return day(d);
  }
  function macroCard(ov) {
    const c = card('Macro dashboard', '', 'st-c12');
    const rows = arr(ov.macro).map(obj).filter((r) => txt(r.label, 80)).slice(0, 30);
    if (!rows.length) { add(c, h('p', 'hint', 'No macro data today.')); return c; }
    const ul = h('ul', 'st-tiles');
    rows.forEach((r) => {
      const li = h('li', 'st-tile');
      const ok = r.status !== 'not_available' && isNum(r.value);
      const signed = signedRow(r);
      const display = txt(r.display, 40);
      add(li, h('span', 'col-h st-tile-l', txt(r.label, 80)));
      add(li, h('b', 'st-tile-v' + (ok ? '' : ' is-na'), ok ? (display ? display + (r.unit === 'percent' ? '%' : '') : unitText(r.value, r.unit, signed)) : 'Not available'));
      if (ok && isNum(r.change)) {
        const ch = h('span', 'st-tile-c');
        const dir = r.change > 0 ? 'up' : r.change < 0 ? 'down' : '';
        if (dir) add(ch, h('span', 'st-arrow', dir === 'up' ? '▲' : '▼'));
        if (ch.firstChild) ch.firstChild.setAttribute('aria-hidden', 'true');
        add(ch, dir ? (dir === 'up' ? 'Up ' : 'Down ') + changeText(Math.abs(r.change), r.unit).replace(/^\+/, '') : 'No change');
        add(li, ch);
      }
      if (ok) {
        add(li, h('span', 'st-tile-m', 'As of ' + asOfText(r.as_of, r.frequency)));
        if (isNum(r.prior)) add(li, h('span', 'st-tile-m', 'Before: ' + unitText(r.prior, r.unit, signed) + (isDay(r.prior_as_of) ? ', ' + asOfText(r.prior_as_of, r.frequency) : '')));
      } else add(li, h('span', 'st-tile-m', 'The series could not be read today.'));
      ul.appendChild(li);
    });
    add(c, ul, note('From FRED (Federal Reserve Bank of St. Louis). Fed funds shows the target range; the change compares the latest reading with the one before it.'));
    return c;
  }

  const NEWS_KIND = { fed: 'Federal Reserve', bls: 'BLS', bea: 'BEA', digest: 'Digest' };
  function newsCard(ov) {
    const c = card('Macro news', '', 'st-newscard');
    const why = new Map();
    arr(obj(ov.summary).news).map(obj).forEach((n) => { if (typeof n.url === 'string' && txt(n.why, 300)) why.set(n.url, txt(n.why, 300)); });
    const items = arr(ov.news).map(obj).filter((n) => txt(n.title, 300)).slice(0, 30);
    if (!items.length) { add(c, h('p', 'hint', 'No macro news in the last two weeks.')); return c; }
    items.sort((a, b) => (why.has(b.url) ? 1 : 0) - (why.has(a.url) ? 1 : 0));
    const ul = h('ul', 'st-news');
    const FIRST = 6;
    items.forEach((n, i) => {
      const li = h('li', 'st-news-i');
      if (i >= FIRST) li.hidden = true;
      const meta = h('p', 'st-news-m');
      add(meta, h('span', 'st-news-s', txt(n.source, 60) || NEWS_KIND[n.kind] || 'Source'));
      const t = time(n.published);
      if (t !== null) add(meta, h('span', '', fmtDayShort.format(new Date(nyDay(t) + 'T12:00:00Z'))));
      if (why.has(n.url)) add(meta, h('span', 'st-pick', 'Picked by Claude'));
      add(li, meta, add(h('p', 'st-news-t'), link(n.url, txt(n.title, 300), n.kind === 'digest')));
      if (txt(n.summary, 300)) add(li, h('p', 'st-news-x', txt(n.summary, 300)));
      if (why.has(n.url)) add(li, h('p', 'st-news-why', 'Why it matters: ' + why.get(n.url)));
      ul.appendChild(li);
    });
    add(c, ul);
    if (items.length > FIRST) {
      const more = button('ghost st-more', 'Show all ' + items.length + ' items');
      more.setAttribute('aria-expanded', 'false');
      more.addEventListener('click', () => {
        const open = more.getAttribute('aria-expanded') !== 'true';
        Array.from(ul.children).forEach((li, i) => { if (i >= FIRST) li.hidden = !open; });
        more.setAttribute('aria-expanded', String(open));
        more.textContent = open ? 'Show fewer' : 'Show all ' + items.length + ' items';
      });
      add(c, more);
    }
    return c;
  }

  function hitInfo(t) {
    const e = entry(t);
    if (e && e.hit) return e.hit;
    const rec = docs.get(t);
    if (!rec || !rec.doc) return null;
    const lb = obj(rec.doc.learner);
    const sc = obj(lb.scorecard);
    const hz = Object.keys(obj(sc.live)).concat(Object.keys(obj(sc.backtest))).filter((k) => /^\d{1,3}$/.test(k)).map(Number);
    if (!hz.length) return null;
    const k = String(Math.max.apply(null, hz));
    const live = obj(obj(sc.live)[k]);
    if (Number.isInteger(live.n) && live.n >= 10 && isNum(live.hit_rate)) return { rate: live.hit_rate, n: live.n, kind: 'live', h: k };
    const bt = obj(obj(sc.backtest)[k]);
    if (Number.isInteger(bt.n) && bt.n > 0 && isNum(bt.hit_rate)) return { rate: bt.hit_rate, n: bt.n, kind: 'backtest', h: k };
    return null;
  }
  function statusWord(s) {
    return { pending: 'Waiting for its first run', partial: 'Some data missing', unknown_ticker: 'Unknown ticker', no_prices: 'No prices', error: 'Failed' }[s] || '';
  }
  function yoursCard() {
    const n = list.tickers.length;
    const c = card('Your stocks', n + ' of ' + list.max, 'st-c12');
    c.id = 'st-yours';
    if (!n) { add(c, h('p', 'hint', 'No stocks yet. Open Settings and add a ticker.')); return c; }
    const rows = list.tickers.map((e) => {
      const name = h('button', 'st-open');
      name.type = 'button';
      add(name, h('span', 'st-open-t', e.ticker), h('span', 'st-open-n', e.name || statusWord(e.status)));
      name.setAttribute('aria-label', 'Open ' + e.ticker + (e.name ? ', ' + e.name : ''));
      name.setAttribute('data-ticker', e.ticker);
      name.addEventListener('click', () => select(e.ticker, true));
      const hitCell = h('span', 'st-hit');
      hitCell.setAttribute('data-ticker', e.ticker);
      fillHit(hitCell, e.ticker);
      const stance = h('span', 'st-stance');
      const sc = stanceChip(e.stance);
      if (sc) add(stance, sc);
      if (e.confidence) add(stance, h('span', 'st-conf', e.confidence.charAt(0).toUpperCase() + e.confidence.slice(1)));
      if (!sc && statusWord(e.status)) add(stance, h('span', 'st-conf', statusWord(e.status)));
      return [name, [e.close === null ? null : usd(e.close)], [pct(e.change_pct, 2, true), signCls(e.change_pct)], stance, hitCell, [e.data_as_of ? dayShort(e.data_as_of) : null]];
    });
    add(c, table('Your stocks', [['Stock'], ['Last close', 'num'], ['Day', 'num'], ['Stance'], ['Hit rate', 'num'], ['Data as of', 'num']], rows, true),
      note('Hit rate: how often the learner called the direction right over its longest horizon, on graded live calls once there are 10, else on its backtest. Each stock learns only from its own history.'));
    return c;
  }
  function fillHit(cell, t) {
    const hit = hitInfo(t);
    cell.textContent = '';
    if (hit) add(cell, share(hit.rate), h('span', 'st-hit-n', hit.kind + ', ' + NF0.format(hit.n)));
    else cell.textContent = loading.has(t) ? 'Loading' : NA;
  }
  // A document came in: only the hit rate cells change, in place, so the keyboard focus stays put.
  function refreshYours() {
    if (!list) return;
    panels.querySelectorAll('#st-yours .st-hit[data-ticker]').forEach((cell) => fillHit(cell, cell.getAttribute('data-ticker')));
  }

  // ---------- a ticker tab ----------
  // The cards of a ticker tab in order: title and grid classes (for the "Not available" stand-in), builder.
  const TICKER_CARDS = [
    ['Price chart', 'st-c12 st-chartcard', chartCard], ['Assessment', 'st-c8 st-assess', assessmentCard],
    ["What this stock's learner says", 'st-c4 st-learner', learnerCard], ['Technicals', 'st-c4', technicalsCard],
    ['Support and resistance', 'st-c4', levelsCard], ['Valuation', 'st-c4', valuationCard], ['Short selling', 'st-c12 st-shorts', shortsCard],
    ['Company insiders', 'st-c12', insidersCard], ['Famous funds', 'st-c12', fundsCard], ['Members of Congress, best effort', 'st-c12', congressCard],
    ['Macro impact', 'st-c12', macroImpactCard],
  ];
  function renderTicker(p, t) {
    const e = entry(t) || normEntry({ ticker: t });
    const rec = docs.get(t);
    if (!rec || rec.none) {
      add(p, tickerHead(t, null, e));
      const c = card('Analysis', '', 'st-c12');
      if (!rec && e.status !== 'pending') {
        add(c, h('p', 'hint', 'Loading the analysis.'));
        if (!loading.has(t)) fetchDoc(t);
      } else if (rec && rec.why === 'not in the list') {
        add(c, h('p', '', t + ' is no longer in the list.'));
      } else {
        add(c, h('p', '', 'No analysis yet. The first one comes with the next run.'),
          h('p', 'hint', 'On its first run a stock is backtested on its own history, so its learner starts with a track record.'));
      }
      add(p, add(h('div', 'st-grid'), c), footer([], DISCLAIMER));
      return;
    }
    if (rec.error) {
      add(p, tickerHead(t, null, e));
      const c = card('Analysis', '', 'st-c12');
      const b = button('ghost', 'Try again');
      b.addEventListener('click', () => { docs.delete(t); renderPanel(); });
      add(c, h('p', '', rec.error), b);
      add(p, add(h('div', 'st-grid'), c), footer([], DISCLAIMER));
      return;
    }
    const doc = rec.doc;
    let head;
    try { head = tickerHead(t, doc, e); } catch (err) { head = tickerHead(t, null, e); }
    add(p, head);
    const ns = notes(doc.notes);
    if (ns) { ns.classList.add('msg', 'is-info'); add(p, ns); }
    const grid = h('div', 'st-grid');
    if (doc.status === 'unknown_ticker') {
      const c = card('Unknown ticker', '', 'st-c12');
      add(c, h('p', '', 'SEC and Tiingo do not know ' + t + '. Check the symbol, or remove it in Settings.'));
      add(grid, c);
    } else {
      TICKER_CARDS.forEach(([title, cls, build]) => add(grid, safe(title, cls, () => build(doc))));
    }
    let lines = [];
    try { lines = tickerFootLines(doc); } catch (err) { lines = []; }
    add(p, grid, footer(lines, doc.disclaimer));
  }
  // The footer says who wrote the assessment only when there is one (not for an unknown ticker),
  // and leaves out a date it does not have.
  function tickerFootLines(doc) {
    const lines = [];
    const a = obj(doc.assessment);
    if (doc.llm === true && modelName(doc.model)) lines.push('Assessment by ' + modelName(doc.model) + (costText(doc.cost) ? ', cost ' + costText(doc.cost) : '') + '.');
    else if (doc.status !== 'unknown_ticker' && (a.source === 'model' || a.source === 'code')) lines.push('No model wrote an assessment today; the stance and levels come from the code.');
    lines.push(madeLine(doc));
    return lines;
  }
  function madeLine(d) {
    return [time(d.generated_at) !== null ? 'Generated ' + stamp(d.generated_at) + '.' : '', isDay(d.data_as_of) ? 'Data as of ' + day(d.data_as_of) + '.' : ''].filter(Boolean).join(' ');
  }
  function tickerHead(t, doc, e) {
    const hd = obj(doc && doc.header);
    const a = obj(doc && doc.assessment);
    const wrap = h('div', 'st-thead');
    const id = h('div', 'st-thead-id');
    const name = txt(hd.name, 120) || e.name;
    const title = h('h3', 'st-tname');
    add(title, h('span', 'st-tsym', t));
    if (name) add(title, ' ', h('span', 'st-tfull', name));
    add(id, title);
    const bits = [];
    const ex = strs(hd.exchanges, 30, 3);
    if (ex.length) bits.push(ex.join(', '));
    if (hd.filer_type === 'domestic') bits.push('US filer');
    else if (hd.filer_type === 'foreign') bits.push('Foreign private issuer');
    if (isDay(hd.first_date) && Number.isInteger(hd.bars)) bits.push(plural(hd.bars, 'trading day', 'trading days') + ' since ' + day(hd.first_date));
    if (bits.length) add(id, h('p', 'st-tmeta', bits.join('. ') + '.'));
    const chips = h('div', 'st-chips');
    const stance = oneOf(a.stance, ['bullish', 'neutral', 'bearish']) || e.stance;
    const sc = stanceChip(stance);
    if (sc) add(chips, sc);
    const conf = oneOf(a.confidence, ['low', 'medium', 'high']) || e.confidence;
    if (conf) add(chips, chip(conf.charAt(0).toUpperCase() + conf.slice(1) + ' confidence'));
    if (a.levels_source === 'code') add(chips, chip('Levels from the code'));
    if (doc && statusWord(doc.status)) add(chips, chip(statusWord(doc.status), 'c-ox'));
    else if (!doc && e.status === 'pending') add(chips, chip('New'));
    if (chips.firstChild) add(id, chips);
    const px = h('div', 'st-thead-px');
    const close = isNum(hd.last_close) ? hd.last_close : e.close;
    add(px, h('p', 'st-price', isNum(close) ? usd(close) : ''));
    const chg = isNum(hd.change_pct) ? hd.change_pct : e.change_pct;
    if (isNum(chg)) add(px, h('p', 'st-pchg ' + signCls(chg), (isNum(hd.change) ? signedMoney(hd.change) + ' ' : '') + '(' + pct(chg, 2, true) + ')'));
    const asOf = (doc && isDay(doc.data_as_of)) ? doc.data_as_of : e.data_as_of;
    if (asOf) add(px, h('p', 'st-tmeta', 'Close on ' + day(asOf)));
    add(wrap, id, px);
    return wrap;
  }

  // ---------- the price chart ----------
  function chartData(doc) {
    const c = obj(doc.chart);
    const dates = arr(c.dates);
    if (dates.length < 2 || !dates.every(isDay)) return null;
    const out = { dates };
    ['open', 'high', 'low', 'close', 'volume', 'sma20', 'sma50', 'sma200', 'rsi14'].forEach((k) => {
      const a = arr(c[k]);
      out[k] = dates.map((_, i) => (isNum(a[i]) ? a[i] : null));
    });
    return out.close.some(isNum) ? out : null;
  }
  function planLevels(a) {
    const out = [];
    arr(a.entry_zones).map(obj).forEach((z, i) => { if (isNum(z.low) && isNum(z.high) && z.low <= z.high) out.push({ kind: 'zone', low: z.low, high: z.high, label: 'Entry ' + (i + 1) }); });
    arr(a.targets).map(obj).forEach((z, i) => { if (isNum(z.price)) out.push({ kind: 'target', low: z.price, high: z.price, label: 'Target ' + (i + 1) }); });
    const st = obj(a.stop);
    if (isNum(st.price)) out.push({ kind: 'stop', low: st.price, high: st.price, label: 'Stop' });
    return out;
  }
  function niceStep(span, count) {
    const raw = span / Math.max(1, count);
    const p = Math.pow(10, Math.floor(Math.log10(raw)));
    const f = raw / p;
    return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
  }
  function decimalsFor(step) { return Math.max(0, Math.min(4, Math.ceil(-Math.log10(step) + 0.0001))); }
  function pathOf(xs, ys) {
    let d = '';
    let pen = false;
    for (let i = 0; i < xs.length; i += 1) {
      if (ys[i] === null) { pen = false; continue; }
      d += (pen ? 'L' : 'M') + xs[i].toFixed(1) + ' ' + ys[i].toFixed(1);
      pen = true;
    }
    return d;
  }
  function lastNum(a) { for (let i = a.length - 1; i >= 0; i -= 1) if (isNum(a[i])) return a[i]; return null; }
  function rangeName(m, n) { const r = RANGES.find(([, v]) => v === m); return m >= n || !r ? 'all ' + NF0.format(n) + ' trading days' : { 63: 'the last 3 months', 126: 'the last 6 months', 252: 'the last year' }[m]; }

  // Draws all three panes; returns the pieces the pointer and keyboard need, or null.
  function drawPrice(holder, cd, plan, ui) {
    const width = Math.round(holder.clientWidth || 0);
    if (!width) return null;
    holder.textContent = '';
    const W = Math.max(280, width);
    const narrow = W < 560;
    const n = cd.dates.length;
    const m = chartRange && chartRange < n ? chartRange : n;
    const start = n - m;
    const M = { l: 4, r: narrow ? 52 : 64, t: 12 };
    const PH = narrow ? 210 : 300;
    const VH = narrow ? 44 : 60;
    const RH = narrow ? 70 : 86;
    const GAP = 20;
    const pT = M.t, pB = pT + PH, vT = pB + GAP, vB = vT + VH, rT = vB + GAP, rB = rT + RH, H = rB + 22;
    const plotR = W - M.r;
    const plotW = plotR - M.l;
    const step = plotW / m;
    const X = (i) => M.l + (i - start + 0.5) * step;
    let ohlc = 0;
    for (let i = start; i < n; i += 1) if ([cd.open[i], cd.high[i], cd.low[i], cd.close[i]].every(isNum)) ohlc += 1;
    const candles = chartStyle === 'candles' && ohlc >= m * 0.8;
    let lo = Infinity;
    let hi = -Infinity;
    const take = (v) => { if (isNum(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } };
    for (let i = start; i < n; i += 1) {
      if (candles) { take(cd.low[i]); take(cd.high[i]); } else take(cd.close[i]);
      take(cd.sma20[i]); take(cd.sma50[i]); take(cd.sma200[i]);
    }
    if (!Number.isFinite(lo)) return null;
    const span0 = Math.max(hi - lo, hi * 0.02);
    plan.forEach((L) => {
      L.on = L.low >= lo - span0 * 0.35 && L.high <= hi + span0 * 0.35;
      if (L.on) { lo = Math.min(lo, L.low); hi = Math.max(hi, L.high); }
    });
    const pad = (hi - lo) * 0.06 || hi * 0.02 || 1;
    lo -= pad;
    hi += pad;
    const Y = (v) => pB - ((v - lo) / (hi - lo)) * PH;
    const RY = (v) => rB - (v / 100) * RH;

    const close = cd.close[n - 1] !== null ? cd.close[n - 1] : lastNum(cd.close);
    const root = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, role: 'img', focusable: 'false',
      'aria-label': 'Price chart for ' + rangeName(m, n) + ', to ' + day(cd.dates[n - 1]) + '. Last close ' + money(close) + '.' }, 'st-svg');

    // grid and price labels
    const ys = niceStep(hi - lo, narrow ? 4 : 5);
    const pf = NF({ minimumFractionDigits: decimalsFor(ys), maximumFractionDigits: decimalsFor(ys) });
    const closeY = isNum(close) ? Math.max(pT + 8, Math.min(pB - 8, Y(close))) : -1e9;
    for (let v = Math.ceil(lo / ys) * ys; v <= hi; v += ys) {
      const y = Y(v);
      put(root, svg('line', { x1: M.l, x2: plotR, y1: y, y2: y }, 'st-ch-grid'));
      if (Math.abs(y - closeY) > 13) put(root, svg('text', { x: plotR + 6, y: y + 3.5 }, 'st-ch-axis')).textContent = pf.format(v);
    }
    // the plan: entry zones, targets, stop
    plan.filter((L) => L.on && L.kind === 'zone').forEach((L) => {
      put(root, svg('rect', { x: M.l, y: Y(L.high), width: plotW, height: Math.max(2, Y(L.low) - Y(L.high)) }, 'st-ch-zone'));
    });
    // prices
    if (candles) {
      const bw = Math.max(1, Math.min(12, step * 0.62));
      const g = svg('g', {}, 'st-ch-candles');
      for (let i = start; i < n; i += 1) {
        const o = cd.open[i], hh = cd.high[i], l = cd.low[i], c = cd.close[i];
        if (![o, hh, l, c].every(isNum)) continue;
        const up = c >= o;
        const x = X(i);
        add(g, svg('line', { x1: x, x2: x, y1: Y(hh), y2: Y(l) }, 'st-ch-wick'));
        const top = Y(Math.max(o, c));
        add(g, svg('rect', { x: x - bw / 2, y: top, width: bw, height: Math.max(1, Y(Math.min(o, c)) - top) }, up ? 'st-ch-up' : 'st-ch-down'));
      }
      add(root, g);
    } else {
      const xs = [];
      const yv = [];
      for (let i = start; i < n; i += 1) { xs.push(X(i)); yv.push(isNum(cd.close[i]) ? Y(cd.close[i]) : null); }
      add(root, svg('path', { d: pathOf(xs, yv) }, 'st-ch-close'));
    }
    [['sma200', 'st-ch-s200'], ['sma50', 'st-ch-s50'], ['sma20', 'st-ch-s20']].forEach(([k, cls]) => {
      const xs = [];
      const yv = [];
      for (let i = start; i < n; i += 1) { xs.push(X(i)); yv.push(isNum(cd[k][i]) ? Y(cd[k][i]) : null); }
      const d = pathOf(xs, yv);
      if (d) add(root, svg('path', { d }, cls));
    });
    plan.filter((L) => L.on).forEach((L) => {
      const y = Y(L.kind === 'zone' ? L.high : L.low);
      if (L.kind !== 'zone') put(root, svg('line', { x1: M.l, x2: plotR, y1: y, y2: y }, L.kind === 'stop' ? 'st-ch-stop' : 'st-ch-target'));
      put(root, svg('text', { x: M.l + 6, y: y - 4 }, 'st-ch-label')).textContent = L.label + ' ' + money(L.low) + (L.kind === 'zone' ? ' to ' + money(L.high) : '');
    });
    if (isNum(close)) {
      const y = closeY;
      const tagG = svg('g', {}, 'st-ch-last');
      put(tagG, svg('rect', { x: plotR + 2, y: y - 9, width: M.r - 4, height: 18, rx: 0 }));
      put(tagG, svg('text', { x: plotR + 6, y: y + 4 })).textContent = NF2.format(close);
      add(root, tagG);
    }
    // volume
    put(root, svg('line', { x1: M.l, x2: plotR, y1: vB, y2: vB }, 'st-ch-sep'));
    let vmax = 0;
    for (let i = start; i < n; i += 1) if (isNum(cd.volume[i])) vmax = Math.max(vmax, cd.volume[i]);
    if (vmax > 0) {
      const g = svg('g', {}, 'st-ch-vols');
      const bw = Math.max(1, Math.min(12, step * 0.7));
      for (let i = start; i < n; i += 1) {
        const v = cd.volume[i];
        if (!isNum(v) || v <= 0) continue;
        const hgt = Math.max(1, (v / vmax) * VH);
        const down = i > 0 && isNum(cd.close[i - 1]) && isNum(cd.close[i]) && cd.close[i] < cd.close[i - 1];
        add(g, svg('rect', { x: X(i) - bw / 2, y: vB - hgt, width: bw, height: hgt }, 'st-ch-vol' + (down ? ' is-down' : '')));
      }
      add(root, g);
      put(root, svg('text', { x: plotR + 6, y: vT + 8 }, 'st-ch-axis')).textContent = compact(vmax);
    }
    put(root, svg('text', { x: M.l + 2, y: vT + 9 }, 'st-ch-pane')).textContent = 'Volume';
    // RSI 14
    put(root, svg('rect', { x: M.l, y: RY(70), width: plotW, height: RY(30) - RY(70) }, 'st-ch-band'));
    [30, 70].forEach((v) => {
      put(root, svg('line', { x1: M.l, x2: plotR, y1: RY(v), y2: RY(v) }, 'st-ch-guide'));
      put(root, svg('text', { x: plotR + 6, y: RY(v) + 3.5 }, 'st-ch-axis')).textContent = String(v);
    });
    const rx = [];
    const ry = [];
    for (let i = start; i < n; i += 1) { rx.push(X(i)); ry.push(isNum(cd.rsi14[i]) ? RY(Math.max(0, Math.min(100, cd.rsi14[i]))) : null); }
    const rd = pathOf(rx, ry);
    if (rd) add(root, svg('path', { d: rd }, 'st-ch-rsi'));
    put(root, svg('text', { x: M.l + 2, y: rT + 9 }, 'st-ch-pane')).textContent = 'RSI 14';
    // dates: a tick at each month that has room
    let lastX = -1e9;
    let prev = cd.dates[start].slice(0, 7);
    for (let i = start + 1; i < n; i += 1) {
      const mo = cd.dates[i].slice(0, 7);
      if (mo === prev) continue;
      prev = mo;
      const x = X(i);
      if (x - lastX < (narrow ? 42 : 54) || x > plotR - 14) continue;
      lastX = x;
      put(root, svg('line', { x1: x, x2: x, y1: rB, y2: rB + 4 }, 'st-ch-sep'));
      const label = mo.slice(5) === '01' ? mo.slice(0, 4) : fmtMonthOnly.format(utcNoon(mo + '-15'));
      put(root, svg('text', { x, y: rB + 16, 'text-anchor': 'middle' }, 'st-ch-axis')).textContent = label;
    }
    // the crosshair, and a transparent layer that takes the pointer
    const cross = svg('g', { visibility: 'hidden' }, 'st-ch-crossg');
    const vline = put(cross, svg('line', { x1: 0, x2: 0, y1: pT, y2: rB }, 'st-ch-cross'));
    const dot = put(cross, svg('circle', { cx: 0, cy: 0, r: 3.5 }, 'st-ch-dot'));
    add(root, cross);
    const hit = put(root, svg('rect', { x: M.l, y: pT, width: plotW, height: rB - pT }, 'st-ch-hit'));
    holder.appendChild(root);

    let cur = -1;
    const show = (i) => {
      i = Math.max(start, Math.min(n - 1, i));
      cur = i;
      const x = X(i);
      vline.setAttribute('x1', x.toFixed(1));
      vline.setAttribute('x2', x.toFixed(1));
      if (isNum(cd.close[i])) { dot.setAttribute('cx', x.toFixed(1)); dot.setAttribute('cy', Y(cd.close[i]).toFixed(1)); dot.setAttribute('visibility', 'inherit'); }
      else dot.setAttribute('visibility', 'hidden');
      cross.setAttribute('visibility', 'visible');
      ui.readout(i);
    };
    const hide = () => { cur = -1; cross.setAttribute('visibility', 'hidden'); ui.readout(n - 1); };
    const at = (ev) => {
      const r = root.getBoundingClientRect();
      const px = ((ev.clientX - r.left) * W) / (r.width || W);
      return start + Math.floor((px - M.l) / step);
    };
    hit.addEventListener('pointermove', (ev) => show(at(ev)));
    hit.addEventListener('pointerdown', (ev) => show(at(ev)));
    hit.addEventListener('pointerleave', hide);
    ui.readout(n - 1);
    return { show, hide, cur: () => cur, start, n, offChart: plan.filter((L) => !L.on) };
  }

  // A chart draws after its card is in the page; a value that breaks it leaves a note in its place.
  function drawSafe(holder, draw) {
    try { return draw(); } catch (e) {
      holder.textContent = '';
      add(holder, h('p', 'hint', 'Not available. The chart could not be drawn.'));
      return null;
    }
  }
  function chartCard(doc) {
    const cd = chartData(doc);
    const c = card('Price chart', cd ? 'Adjusted daily prices' : '', 'st-c12 st-chartcard');
    if (!cd) { add(c, h('p', 'hint', 'No price history to draw.')); return c; }
    const n = cd.dates.length;
    const plan = planLevels(obj(doc.assessment));
    const tools = h('div', 'st-chart-tools');
    const ranges = h('div', 'seg-ctl');
    ranges.setAttribute('role', 'group');
    ranges.setAttribute('aria-label', 'Time range');
    RANGES.forEach(([label, v]) => {
      if (v && v >= n && label !== 'All') return;
      const b = button('', label);
      b.setAttribute('aria-pressed', String(chartRange === v || (v === 0 && chartRange >= n)));
      b.addEventListener('click', () => { chartRange = v; Array.from(ranges.children).forEach((x) => x.setAttribute('aria-pressed', String(x === b))); redraw(true); });
      ranges.appendChild(b);
    });
    if (!ranges.querySelector('[aria-pressed="true"]')) ranges.lastChild.setAttribute('aria-pressed', 'true');
    const styles = h('div', 'seg-ctl');
    styles.setAttribute('role', 'group');
    styles.setAttribute('aria-label', 'Chart style');
    [['Candles', 'candles'], ['Line', 'line']].forEach(([label, v]) => {
      const b = button('', label);
      b.setAttribute('aria-pressed', String(chartStyle === v));
      b.addEventListener('click', () => { chartStyle = v; Array.from(styles.children).forEach((x) => x.setAttribute('aria-pressed', String(x === b))); redraw(true); });
      styles.appendChild(b);
    });
    add(tools, ranges, styles);
    const readout = h('p', 'st-readout');
    const holder = h('div', 'st-chart');
    holder.tabIndex = 0;
    holder.setAttribute('aria-label', 'Price chart. Use the left and right arrow keys to read each day.');
    const legend = h('ul', 'st-legend');
    const caption = h('p', 'hint st-cap');
    const ui = {
      readout(i) {
        readout.textContent = '';
        const prev = i > 0 ? cd.close[i - 1] : null;
        const chg = isNum(prev) && isNum(cd.close[i]) && prev ? ((cd.close[i] - prev) / prev) * 100 : null;
        add(readout, h('b', '', day(cd.dates[i])));
        [['Open', money(cd.open[i])], ['High', money(cd.high[i])], ['Low', money(cd.low[i])], ['Close', money(cd.close[i])],
          ['Day', pct(chg, 2, true)], ['20d', money(cd.sma20[i])], ['50d', money(cd.sma50[i])], ['200d', money(cd.sma200[i])],
          ['RSI', isNum(cd.rsi14[i]) ? NF1.format(cd.rsi14[i]) : NA], ['Volume', compact(cd.volume[i])]].forEach(([k, v]) => {
          if (v === NA && k !== 'Close') return;
          add(readout, add(h('span', 'st-ro'), h('span', 'st-ro-k', k + ' '), v));
        });
      },
    };
    let ctl = null;
    let drawnW = 0;
    const redraw = (force) => {
      const w = Math.round(holder.clientWidth || 0);
      if (!w || (!force && Math.abs(w - drawnW) < 4)) return;
      drawnW = w;
      ctl = drawSafe(holder, () => drawPrice(holder, cd, plan, ui));
      legend.textContent = '';
      [[candles() ? 'Daily candles' : 'Close', candles() ? 'st-sw-candle' : 'st-sw-close'], ['20 day average', 'st-sw-s20'], ['50 day average', 'st-sw-s50'], ['200 day average', 'st-sw-s200']]
        .concat(plan.some((L) => L.kind === 'zone') ? [['Entry zone', 'st-sw-zone']] : [])
        .concat(plan.some((L) => L.kind === 'target') ? [['Target', 'st-sw-target']] : [])
        .concat(plan.some((L) => L.kind === 'stop') ? [['Stop', 'st-sw-stop']] : [])
        .forEach(([label, cls]) => add(legend, add(h('li'), h('span', 'st-sw ' + cls), label)));
      (ctl ? ctl.offChart : []).forEach((L) => add(legend, h('li', 'st-off', L.label + ' at ' + money(L.low) + ' is off the chart')));
    };
    const candles = () => chartStyle === 'candles' && holder.querySelector('.st-ch-candles') !== null;
    holder.addEventListener('keydown', (ev) => {
      if (!ctl) return;
      const cur = ctl.cur() < 0 ? ctl.n : ctl.cur();
      let to = null;
      if (ev.key === 'ArrowLeft') to = cur - 1;
      else if (ev.key === 'ArrowRight') to = Math.min(ctl.n - 1, cur + 1);
      else if (ev.key === 'PageUp') to = cur - 21;
      else if (ev.key === 'PageDown') to = Math.min(ctl.n - 1, cur + 21);
      else if (ev.key === 'Home') to = ctl.start;
      else if (ev.key === 'End') to = ctl.n - 1;
      else if (ev.key === 'Escape') { ctl.hide(); return; }
      if (to === null) return;
      ev.preventDefault();
      ctl.show(to);
    });
    holder.addEventListener('blur', () => { if (ctl) ctl.hide(); });
    const tech = obj(doc.technicals);
    const sma = obj(tech.sma);
    const close = lastNum(cd.close);
    caption.textContent = 'Daily prices adjusted for splits and dividends. Last close ' + money(close) + ' on ' + day(cd.dates[n - 1]) +
      '. 20 day average ' + money(isNum(sma['20']) ? sma['20'] : lastNum(cd.sma20)) + ', 50 day ' + money(isNum(sma['50']) ? sma['50'] : lastNum(cd.sma50)) +
      ', 200 day ' + money(isNum(sma['200']) ? sma['200'] : lastNum(cd.sma200)) + '. RSI ' + (isNum(lastNum(cd.rsi14)) ? NF1.format(lastNum(cd.rsi14)) : NA) + '.';
    add(c, tools, readout, holder, legend, caption);
    if (typeof ResizeObserver === 'function') {
      const ro = new ResizeObserver(() => redraw(false));
      ro.observe(holder);
      observers.add(ro);
    }
    requestAnimationFrame(() => redraw(false));
    return c;
  }

  // ---------- the assessment ----------
  function candidateLabel(levels, id) {
    if (typeof id !== 'string') return '';
    const c = arr(obj(levels).candidates).map(obj).find((x) => x.id === id);
    return c ? txt(c.label, 80) : '';
  }
  function dist(price, close) { return isNum(price) && isNum(close) && close ? pct(((price - close) / close) * 100, 1, true) : NA; }
  function assessmentCard(doc) {
    const a = obj(doc.assessment);
    const lv = obj(doc.levels);
    const close = isNum(obj(doc.header).last_close) ? obj(doc.header).last_close : obj(doc.technicals).close;
    const c = card('Assessment', a.source === 'model' ? (modelName(doc.model) ? 'By ' + modelName(doc.model) : 'By Claude') : 'From the code', 'st-c8 st-assess');
    const stance = oneOf(a.stance, ['bullish', 'neutral', 'bearish']);
    const conf = oneOf(a.confidence, ['low', 'medium', 'high']);
    const top = h('div', 'st-verdict');
    if (stance) add(top, h('p', 'st-big st-stance-' + stance, stance.charAt(0).toUpperCase() + stance.slice(1)));
    if (conf) add(top, h('p', 'st-verdict-c', conf.charAt(0).toUpperCase() + conf.slice(1) + ' confidence'));
    add(c, top);
    paras(a.summary, 900).forEach((t) => add(c, h('p', 'st-lede', t)));

    const rows = [];
    const lbl = (...ids) => ids.map((x) => candidateLabel(lv, x)).filter(Boolean).join(' to ');
    arr(a.entry_zones).map(obj).forEach((z, i) => {
      if (!isNum(z.low) || !isNum(z.high)) return;
      rows.push(['Entry zone ' + (i + 1), money(z.low) + ' to ' + money(z.high), dist(z.low, close) + ' to ' + dist(z.high, close), lbl(z.low_ref, z.high_ref), txt(z.rationale, 300)]);
    });
    arr(a.targets).map(obj).forEach((z, i) => {
      if (!isNum(z.price)) return;
      rows.push(['Target ' + (i + 1) + (Number.isInteger(z.horizon_days) ? ', ' + plural(z.horizon_days, 'day', 'days') : ''), money(z.price), dist(z.price, close), lbl(z.ref), txt(z.rationale, 300)]);
    });
    const st = obj(a.stop);
    if (isNum(st.price)) rows.push(['Stop', money(st.price), dist(st.price, close), lbl(st.ref), txt(st.rationale, 300)]);
    add(c, h('h4', 'st-sub', 'Price points'));
    if (rows.length) {
      const tw = table('Entry zones, targets and stop', [['Level'], ['Price', 'num'], ['From close', 'num'], ['Based on', 'mid'], ['Why', 'wide']], rows.map((r) => r.map((x) => x || NA)), true);
      add(c, tw);
      const z = obj(arr(a.entry_zones)[0]);
      const t1 = obj(arr(a.targets)[0]);
      if (isNum(z.low) && isNum(z.high) && isNum(t1.price) && isNum(st.price)) {
        const mid = (z.low + z.high) / 2;
        const rr = mid > st.price ? (t1.price - mid) / (mid - st.price) : null;
        if (isNum(rr) && rr > 0) add(c, note('From the middle of the first entry zone, the first target is ' + NF1.format(rr) + ' times as far as the stop.'));
      }
    } else add(c, h('p', 'hint', 'No price points today.'));
    if (a.levels_source === 'code') add(c, note('Levels from the code: they come from the ticker\'s own support, resistance, averages and ATR bands, not from the model.', 'hint-note'));
    if (stance === 'bearish' && rows.length && !arr(a.targets).some((t) => isNum(obj(t).price))) add(c, note('No target: the view is bearish, so there is no upside target. The entry zone is a level to watch, not a signal to buy.', 'hint-note'));
    const cmm = txt(a.change_my_mind, 400);
    if (cmm) add(c, h('h4', 'st-sub', 'What would change my mind'), h('p', '', cmm));
    const risks = strs(a.risks, 240, 5);
    if (risks.length) add(c, h('h4', 'st-sub', 'Risks'), bullets(risks));
    const probs = strs(a.problems, 300, 6);
    if (probs.length) add(c, h('h4', 'st-sub', 'Checks on the answer'), bullets(probs, 'st-muted'));
    return c;
  }

  // ---------- technicals, levels, valuation ----------
  function withDist(v, d) { return isNum(v) ? money(v) + (isNum(d) ? ' (' + pct(d, 1, true) + ')' : '') : NA; }
  function technicalsCard(doc) {
    const t = obj(doc.technicals);
    const c = card('Technicals', isDay(t.as_of) ? dayShort(t.as_of) : '', 'st-c4');
    if (!isNum(t.close)) { add(c, h('p', 'hint', 'Not available today.')); return c; }
    const sma = obj(t.sma), ema = obj(t.ema), d = obj(t.distance_pct), macd = obj(t.macd), bb = obj(t.bollinger), r52 = obj(t.range_52w), rs = obj(t.rel_strength), vol = obj(t.volume);
    const rsiWord = isNum(t.rsi14) ? (t.rsi14 >= 70 ? ', overbought' : t.rsi14 <= 30 ? ', oversold' : '') : '';
    const cross = macd.cross === 'bullish' || macd.cross === 'bearish'
      ? (macd.cross === 'bullish' ? 'Bullish' : 'Bearish') + ' cross ' + (macd.cross_days_ago === 0 ? 'today' : Number.isInteger(macd.cross_days_ago) ? plural(macd.cross_days_ago, 'day', 'days') + ' ago' : '')
      : 'No recent cross';
    add(c, kv([
      ['Trend', t.trend === 'up' ? 'Up' : t.trend === 'down' ? 'Down' : t.trend === 'sideways' ? 'Sideways' : NA, t.trend === 'up' ? 'st-up' : t.trend === 'down' ? 'st-down' : ''],
      ['20 day average', withDist(sma['20'], d.sma20)],
      ['50 day average', withDist(sma['50'], d.sma50)],
      ['200 day average', withDist(sma['200'], d.sma200)],
      ['EMA 12, 26, 50', [ema['12'], ema['26'], ema['50']].map((x) => (isNum(x) ? NF2.format(x) : NA)).join(', ')],
      ['RSI 14', isNum(t.rsi14) ? NF1.format(t.rsi14) + rsiWord : NA],
      ['MACD', isNum(macd.macd) ? NF2.format(macd.macd) + ', signal ' + (isNum(macd.signal) ? NF2.format(macd.signal) : NA) : NA],
      ['MACD histogram', isNum(macd.hist) ? NF2S.format(macd.hist) + '. ' + cross : NA, signCls(macd.hist)],
      ['Bollinger bands', isNum(bb.upper) ? money(bb.lower) + ' to ' + money(bb.upper) : NA],
      ['Bollinger %B, width', (isNum(bb.pct_b) ? NF2.format(bb.pct_b) : NA) + ', ' + pct(bb.bandwidth_pct, 1)],
      ['ATR 14', isNum(t.atr14) ? money(t.atr14) + ' (' + pct(t.atr_pct, 1) + ' of price)' : NA],
      ['52 week high', isNum(r52.high) ? money(r52.high) + ', ' + dayShort(r52.high_date) + ' (' + pct(r52.from_high_pct, 1, true) + ')' : NA],
      ['52 week low', isNum(r52.low) ? money(r52.low) + ', ' + dayShort(r52.low_date) + ' (' + pct(r52.from_low_pct, 1, true) + ')' : NA],
      ['Volume vs 20 day average', isNum(vol.ratio20) ? NF2.format(vol.ratio20) + 'x (' + compact(vol.last) + ' vs ' + compact(vol.avg20) + ')' : NA],
    ]));
    const rows = [['20 days', '_20d'], ['60 days', '_60d'], ['1 year', '_252d']].filter(([, k]) => isNum(rs['ret' + k + '_pct']) || isNum(rs['excess' + k + '_pct']))
      .map(([label, k]) => [label, [pct(rs['ret' + k + '_pct'], 1, true), signCls(rs['ret' + k + '_pct'])], [pct(rs['spy_ret' + k + '_pct'], 1, true)], [pct(rs['excess' + k + '_pct'], 1, true), signCls(rs['excess' + k + '_pct'])]]);
    if (rows.length) add(c, h('h4', 'st-sub', 'Relative strength vs ' + (txt(rs.vs, 8) || 'SPY')), table('Relative strength', [['Period'], ['Stock', 'num'], [txt(rs.vs, 8) || 'SPY', 'num'], ['Ahead by', 'num']], rows));
    add(c, read(obj(doc.assessment).technical));
    return c;
  }
  function levelsCard(doc) {
    const lv = obj(doc.levels);
    const close = obj(doc.technicals).close;
    const c = card('Support and resistance', '', 'st-c4');
    const rowsOf = (xs) => arr(xs).map(obj).filter((x) => isNum(x.price)).map((x) => [txt(x.id, 4).toUpperCase(), money(x.price), dist(x.price, close), Number.isInteger(x.touches) ? String(x.touches) : NA, dayShort(x.last_touch)]);
    const res = rowsOf(lv.resistances);
    const sup = rowsOf(lv.supports);
    const cols = [['Level'], ['Price', 'num'], ['From close', 'num'], ['Touches', 'num'], ['Last', 'num']];
    add(c, h('h4', 'st-sub', 'Resistance above'), res.length ? table('Resistance levels', cols, res) : h('p', 'hint', 'None in the last year above the close.'));
    add(c, h('h4', 'st-sub', 'Support below'), sup.length ? table('Support levels', cols, sup) : h('p', 'hint', 'None in the last year below the close.'));
    const cands = arr(lv.candidates).map(obj).filter((x) => isNum(x.price) && txt(x.label, 80));
    if (cands.length) {
      const det = h('details', 'st-details');
      add(det, h('summary', '', cands.length === 1 ? 'The one price a plan may use' : 'All ' + cands.length + ' prices a plan may use'));
      cands.sort((x, y) => y.price - x.price);
      add(det, table('Candidate prices', [['Price', 'num'], ['From close', 'num'], ['What it is']], cands.map((x) => [money(x.price), dist(x.price, close), txt(x.label, 80)])));
      add(c, det);
    }
    const tol = obj(lv.tolerance);
    add(c, note('Levels are clusters of turning points over the last year. A plan price must sit within ' + (isNum(tol.pct) ? NF1.format(tol.pct) : '0.5') + ' percent of one of these prices, or a tenth of an ATR.'));
    return c;
  }
  function valuationCard(doc) {
    const v = obj(doc.valuation);
    const c = card('Valuation', '', 'st-c4');
    const annual = v.eps_basis === 'annual';
    let big = 'Not available';
    if (v.status === 'ok' && isNum(v.pe_ttm)) big = NF1.format(v.pe_ttm);
    else if (v.status === 'not_meaningful') big = 'Not meaningful';
    add(c, h('p', 'st-big' + (big.length > 8 ? ' st-big-word' : ''), big), h('p', 'st-big-l', annual ? 'P/E on the last fiscal year' : 'P/E on the last four quarters'));
    if (txt(v.note, 300)) add(c, h('p', 'hint hint-note', txt(v.note, 300)));
    if (v.status === 'not_available' && !isNum(v.eps_ttm)) { add(c, read(obj(doc.assessment).valuation)); return c; }
    const fx = obj(v.fx);
    const cur = /^[A-Z]{3}$/.test(v.eps_currency || '') ? v.eps_currency : 'USD';
    add(c, kv([
      ['Diluted EPS', moneyIn(v.eps_ttm, cur) + (annual ? ', fiscal year' : ', four quarters')],
      cur !== 'USD' ? ['In dollars', isNum(v.eps_usd) ? money(v.eps_usd) + (isNum(fx.usd_per_unit) ? ' at ' + NF4.format(fx.usd_per_unit) + ' per ' + cur : '') : NA] : null,
      ['Price used', money(v.price_used)],
      ['Latest period', day(v.latest_end)],
      ['Filed', day(v.latest_filed)],
      ['Source', txt(v.concept, 80) || NA, 'st-mono'],
    ]));
    if (v.stale === true) add(c, note('The latest report is old, so this P/E may be out of date.', 'hint-note'));
    if (v.split_adjusted === true) add(c, note('Earnings per share were adjusted for a split after the last report.'));
    if (isDay(fx.date)) add(c, note('Exchange rate from FRED (' + txt(fx.series, 20) + ') on ' + day(fx.date) + '.'));
    const qs = arr(v.quarters).map(obj).filter((q) => isDay(q.end) && isNum(q.eps)).slice(-8);
    if (qs.length) add(c, h('h4', 'st-sub', 'Quarters'), table('Quarterly earnings per share', [['Period end'], ['EPS', 'num'], ['Form'], ['Filed', 'num']],
      qs.map((q) => [day(q.end) + (q.derived ? ' *' : ''), [moneyIn(q.eps, cur), signCls(q.eps) === 'st-down' ? 'st-down' : ''], txt(q.form, 10), dayShort(q.filed)])));
    if (qs.some((q) => q.derived)) add(c, note('* The fourth quarter is the annual report minus the first three quarters.'));
    add(c, read(obj(doc.assessment).valuation));
    return c;
  }

  // ---------- short selling ----------
  function zWord(label) { const w = txt(label, 30); return w ? w.charAt(0).toUpperCase() + w.slice(1) : ''; }
  function drawRatio(holder, dates, ratio, mean, std, readout) {
    const width = Math.round(holder.clientWidth || 0);
    if (!width) return;
    holder.textContent = '';
    const W = Math.max(260, width);
    const H = W < 560 ? 140 : 160;
    const M = { l: 4, r: 50, t: 10, b: 22 };
    const n = dates.length;
    const plotR = W - M.r;
    const plotW = plotR - M.l;
    const step = plotW / n;
    const X = (i) => M.l + (i + 0.5) * step;
    let lo = Infinity;
    let hi = -Infinity;
    ratio.forEach((v) => { if (isNum(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
    if (isNum(mean) && isNum(std)) { lo = Math.min(lo, mean - 2 * std); hi = Math.max(hi, mean + 2 * std); }
    if (!Number.isFinite(lo)) return;
    const pad = (hi - lo) * 0.08 || 0.01;
    lo = Math.max(0, lo - pad);
    hi = Math.min(1, hi + pad);
    const Y = (v) => M.t + (1 - (v - lo) / (hi - lo)) * (H - M.t - M.b);
    const root = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, role: 'img', focusable: 'false',
      'aria-label': 'Daily short sale volume ratio over ' + n + ' days, with this stock\'s own average and one standard deviation either side.' }, 'st-svg');
    const ys = niceStep((hi - lo) * 100, 3) / 100;
    for (let v = Math.ceil(lo / ys) * ys; v <= hi; v += ys) {
      put(root, svg('line', { x1: M.l, x2: plotR, y1: Y(v), y2: Y(v) }, 'st-ch-grid'));
      put(root, svg('text', { x: plotR + 6, y: Y(v) + 3.5 }, 'st-ch-axis')).textContent = NF0.format(v * 100) + '%';
    }
    if (isNum(mean) && isNum(std)) {
      put(root, svg('rect', { x: M.l, y: Y(Math.min(hi, mean + std)), width: plotW, height: Math.max(1, Y(Math.max(lo, mean - std)) - Y(Math.min(hi, mean + std))) }, 'st-ch-band'));
      put(root, svg('line', { x1: M.l, x2: plotR, y1: Y(mean), y2: Y(mean) }, 'st-ch-mean'));
    }
    add(root, svg('path', { d: pathOf(dates.map((_, i) => X(i)), ratio.map((v) => (isNum(v) ? Y(v) : null))) }, 'st-ch-ratio'));
    const li = ratio.length - 1;
    if (isNum(ratio[li])) put(root, svg('circle', { cx: X(li), cy: Y(ratio[li]), r: 3 }, 'st-ch-dot'));
    let lastX = -1e9;
    let prev = dates[0].slice(0, 7);
    for (let i = 1; i < n; i += 1) {
      const mo = dates[i].slice(0, 7);
      if (mo === prev) continue;
      prev = mo;
      const x = X(i);
      if (x - lastX < 46 || x > plotR - 14) continue;
      lastX = x;
      put(root, svg('text', { x, y: H - 6, 'text-anchor': 'middle' }, 'st-ch-axis')).textContent = mo.slice(5) === '01' ? mo.slice(0, 4) : fmtMonthOnly.format(utcNoon(mo + '-15'));
    }
    const cross = svg('line', { x1: 0, x2: 0, y1: M.t, y2: H - M.b, visibility: 'hidden' }, 'st-ch-cross');
    add(root, cross);
    const hit = put(root, svg('rect', { x: M.l, y: M.t, width: plotW, height: H - M.t - M.b }, 'st-ch-hit'));
    const show = (i) => {
      i = Math.max(0, Math.min(n - 1, i));
      cross.setAttribute('x1', X(i).toFixed(1));
      cross.setAttribute('x2', X(i).toFixed(1));
      cross.setAttribute('visibility', 'visible');
      readout(i);
    };
    hit.addEventListener('pointermove', (ev) => { const r = root.getBoundingClientRect(); show(Math.floor((((ev.clientX - r.left) * W) / (r.width || W) - M.l) / step)); });
    hit.addEventListener('pointerleave', () => { cross.setAttribute('visibility', 'hidden'); readout(n - 1); });
    holder.appendChild(root);
    readout(n - 1);
  }
  function shortsCard(doc) {
    const s = obj(doc.shorts);
    const c = card('Short selling', 'FINRA', 'st-c12 st-shorts');
    if (s.status === 'not_available' || !Object.keys(s).length) {
      add(c, h('p', 'hint', 'Short sale data is not available today.'), notes(s.notes));
      return c;
    }
    const L = obj(s.latest);
    const a5 = obj(s.avg5);
    const cols = h('div', 'st-two st-two-wide');
    const left = h('div');
    add(left, h('p', 'st-big', share(L.ratio, 1)), h('p', 'st-big-l', 'of volume sold short on ' + day(L.date)));
    const chips = h('div', 'st-chips');
    if (zWord(L.label)) add(chips, chip(zWord(L.label), L.label === 'unusually high' || L.label === 'high' ? 'c-ox' : L.label === 'low' || L.label === 'unusually low' ? 'c-cyan' : ''));
    if (isNum(L.z)) add(chips, chip('z ' + NF2S.format(L.z)));
    add(left, chips);
    add(left, kv([
      ['This stock\'s own average', isNum(L.mean) ? share(L.mean, 1) + (isNum(L.std) ? ', give or take ' + share(L.std, 1) : '') : NA],
      ['Percentile', isNum(L.percentile) ? 'Higher than ' + NF0.format(L.percentile) + ' percent of the last ' + (Number.isInteger(L.n) && L.n > 0 ? plural(L.n, 'day', 'days') : 'year') : NA],
      ['5 day average', isNum(a5.ratio) ? share(a5.ratio, 1) + (isNum(a5.z) ? ', z ' + NF2S.format(a5.z) : '') + (zWord(a5.label) ? ', ' + txt(a5.label, 30) : '') : NA],
      ['Short volume', isNum(L.short_volume) ? compact(L.short_volume) + ' of ' + compact(L.total_volume) + ' shares' : NA],
    ]));
    const right = h('div');
    const daily = obj(s.daily);
    const dates = arr(daily.dates);
    const ratio = dates.map((_, i) => (isNum(arr(daily.ratio)[i]) ? arr(daily.ratio)[i] : null));
    if (dates.length >= 5 && dates.every(isDay)) {
      const ro = h('p', 'st-readout');
      const holder = h('div', 'st-chart st-chart-sm');
      const sv = arr(daily.short_volume);
      const tv = arr(daily.total_volume);
      const readout = (i) => {
        ro.textContent = '';
        add(ro, h('b', '', day(dates[i])), add(h('span', 'st-ro'), h('span', 'st-ro-k', 'Short '), share(ratio[i], 1)));
        if (isNum(sv[i]) && isNum(tv[i])) add(ro, add(h('span', 'st-ro'), h('span', 'st-ro-k', 'Shares '), compact(sv[i]) + ' of ' + compact(tv[i])));
      };
      add(right, h('h4', 'st-sub', 'Daily short volume ratio, against its own average'), ro, holder);
      let drawnW = 0;
      const redraw = () => { const w = Math.round(holder.clientWidth || 0); if (w && Math.abs(w - drawnW) >= 4) { drawnW = w; drawSafe(holder, () => drawRatio(holder, dates, ratio, L.mean, L.std, readout)); } };
      if (typeof ResizeObserver === 'function') { const o = new ResizeObserver(redraw); o.observe(holder); observers.add(o); }
      requestAnimationFrame(redraw);
      const legend = h('ul', 'st-legend');
      add(legend, add(h('li'), h('span', 'st-sw st-sw-ratio'), 'Short volume ratio'), add(h('li'), h('span', 'st-sw st-sw-mean'), 'Own average'), add(h('li'), h('span', 'st-sw st-sw-band'), 'One standard deviation'));
      add(right, legend);
    }
    add(cols, left, right);
    add(c, cols);
    const si = obj(s.interest);
    const rows = arr(si.rows).map(obj).filter((r) => isDay(r.settlement_date)).slice(-8).reverse();
    add(c, h('h4', 'st-sub', 'Short interest'));
    if (rows.length) {
      add(c, table('Short interest', [['Settlement'], ['Shares short', 'num'], ['Change', 'num'], ['Average daily volume', 'num'], ['Days to cover', 'num']],
        rows.map((r) => [day(r.settlement_date), compact(r.short_interest), [pct(r.change_pct, 1, true), signCls(r.change_pct)], compact(r.avg_daily_volume), isNum(r.days_to_cover) ? NF2.format(r.days_to_cover) : NA]), true));
    } else add(c, h('p', 'hint', 'Short interest is not available for this stock yet.'));
    add(c, notes(s.notes), read(obj(doc.assessment).shorts));
    return c;
  }

  // ---------- holders ----------
  function holdersStatus(block, none) {
    if (block.status === 'not_available') return h('p', 'hint', 'Not available today.');
    if (block.status === 'none') return h('p', 'hint', none);
    return null;
  }
  function insidersCard(doc) {
    const ins = obj(obj(doc.holders).insiders);
    const c = card('Company insiders', 'Forms 3 and 4', 'st-c12');
    const s = obj(ins.summary);
    const txs = arr(ins.transactions).map(obj).slice(0, 60);
    const st = holdersStatus(ins, 'No insider filings in the last ' + (Number.isInteger(ins.window_days) ? ins.window_days : 365) + ' days.');
    if (st && !txs.length) { add(c, st, note(txt(ins.note, 300)), read(obj(doc.assessment).holders)); return c; }
    if (Number.isInteger(s.open_market_buys) || Number.isInteger(s.open_market_sells)) {
      add(c, h('p', 'st-lede', 'Since ' + day(s.since) + ': ' + plural(s.open_market_buys || 0, 'open market buy', 'open market buys') + (s.buy_value ? ' (' + moneyCompact(s.buy_value) + ')' : '') + ' and ' +
        plural(s.open_market_sells || 0, 'open market sale', 'open market sales') + (s.sell_value ? ' (' + moneyCompact(s.sell_value) + ')' : '') + ' by ' + plural(s.insiders || 0, 'insider', 'insiders') +
        (isNum(s.net_shares) ? ', net ' + NF({ maximumFractionDigits: 0, signDisplay: 'exceptZero' }).format(s.net_shares) + ' shares.' : '.')));
    }
    if (txs.length) {
      const rows = txs.map((t) => {
        const code = h('span', 'st-code');
        add(code, h('span', 'st-code-l', txt(t.code, 2) || '?'), ' ' + (txt(t.code_label, 60) || 'Other'));
        const tags = [];
        if (t.derivative === true) tags.push('derivative');
        if (t.plan_10b5_1 === true) tags.push('10b5-1 plan');
        if (t.ownership === 'I') tags.push('indirect');
        if (tags.length) add(code, h('span', 'st-code-x', tags.join(', ')));
        const sign = t.acquired_disposed === 'D' ? -1 : t.acquired_disposed === 'A' ? 1 : 0;
        const shares = isNum(t.shares) ? NF({ maximumFractionDigits: 0, signDisplay: sign ? 'always' : 'auto' }).format(sign ? sign * t.shares : t.shares) : NA;
        return [dayShort(t.date), txt(t.owner, 80), txt(t.role, 80), code, [shares, sign < 0 ? 'st-down' : sign > 0 ? 'st-up' : ''], money(t.price), moneyCompact(t.value), compact(t.shares_after), link(t.url, 'Form ' + (txt(t.form, 4) || '4'))];
      });
      add(c, table('Insider transactions', [['Date'], ['Insider'], ['Role'], ['Transaction'], ['Shares', 'num'], ['Price', 'num'], ['Value', 'num'], ['Owned after', 'num'], ['Filing']], rows, true));
    }
    add(c, note(txt(ins.note, 300)), read(obj(doc.assessment).holders));
    return c;
  }
  const CHANGE_WORDS = { new: ['New', 'c-cyan'], added: ['Added', 'c-cyan'], trimmed: ['Trimmed', 'c-ox'], exited: ['Exited', 'c-ox'], unchanged: ['Unchanged', ''] };
  function fundsCard(doc) {
    const f = obj(obj(doc.holders).funds);
    const c = card('Famous funds', Number.isInteger(f.funds_checked) ? plural(f.funds_checked, 'fund checked', 'funds checked') : '13F', 'st-c12');
    const rows = arr(f.rows).map(obj).filter((r) => txt(r.fund, 80));
    const st = holdersStatus(f, 'None of the funds checked held it in their last two reports.');
    if (st && !rows.length) { add(c, st, note(txt(f.note, 300))); return c; }
    if (!rows.length) { add(c, h('p', 'hint', 'None of the funds checked held it in their last two reports.'), note(txt(f.note, 300))); return c; }
    add(c, table('Fund holdings', [['Fund'], ['Quarter end'], ['Shares', 'num'], ['Value', 'num'], ['Change'], ['Shares changed', 'num'], ['Filing']],
      rows.map((r) => {
        const w = CHANGE_WORDS[r.change] || ['Unknown', ''];
        const chg = r.change === 'new' ? 'New position' : isNum(r.change_shares) ? NF({ maximumFractionDigits: 0, signDisplay: 'exceptZero' }).format(r.change_shares) + (isNum(r.change_pct) ? ' (' + pct(r.change_pct, 1, true) + ')' : '') : NA;
        return [txt(r.fund, 80), day(r.period), compact(r.shares), moneyCompact(r.value_usd), chip(w[0], w[1]), [chg, signCls(r.change_shares)], link(r.url, (txt(r.form, 10) || '13F') + ', ' + dayShort(r.filed))];
      }), true));
    add(c, note(txt(f.note, 300)));
    return c;
  }
  function congressCard(doc) {
    const g = obj(obj(doc.holders).congress);
    const c = card(txt(g.label, 80) || 'Members of Congress, best effort', 'Best effort', 'st-c12');
    const cov = obj(g.coverage);
    const covWord = (v) => (v === 'ok' ? 'read' : v === 'off' ? 'off' : txt(v, 40) || 'unknown');
    add(c, h('p', 'hint', 'House reports: ' + covWord(cov.house) + '. Senate reports: ' + covWord(cov.senate) + '.'));
    const trades = arr(g.trades).map(obj).filter((t) => txt(t.member, 80)).slice(0, 40);
    if (!trades.length) add(c, h('p', 'hint', g.status === 'not_available' ? 'Not available today.' : 'No trades by members of Congress found in the last year.'));
    else {
      add(c, table('Trades by members of Congress', [['Traded'], ['Member'], ['Chamber'], ['Type'], ['Owner'], ['Amount', 'num'], ['Filed'], ['Report']],
        trades.map((t) => [dayShort(t.date), txt(t.member, 80), (txt(t.chamber, 10) || 'House') + (txt(t.district, 6) ? ', ' + txt(t.district, 6) : ''), txt(t.type, 20), txt(t.owner, 20), txt(t.amount_range, 40), dayShort(t.filed), link(t.source, 'Report')]), true));
    }
    add(c, note(txt(g.note, 400)));
    return c;
  }

  // ---------- macro impact ----------
  function evidence(t) { if (!isNum(t)) return ''; const a = Math.abs(t); return a >= 3 ? 'strong' : a >= 2 ? 'clear' : a >= 1 ? 'weak' : 'no clear link'; }
  function macroImpactCard(doc) {
    const mi = obj(doc.macro_impact);
    const c = card('Macro impact', Number.isInteger(mi.window_days) ? 'Last ' + mi.window_days + ' trading days' : '', 'st-c12');
    if (mi.status !== 'ok') { add(c, h('p', 'hint', 'Not enough history to measure yet.'), notes(mi.notes)); return c; }
    const b = obj(mi.market_beta);
    if (isNum(b.beta)) add(c, h('p', 'st-lede', 'Market beta ' + NF2.format(b.beta) + ': on a day the market (SPY) moves 1 percent, this stock has moved about ' + NF2.format(b.beta) + ' percent the same way' + (Number.isInteger(b.n) ? ', over ' + NF0.format(b.n) + ' days.' : '.')));
    const fs = arr(mi.factors).map(obj).filter((f) => txt(f.label, 60));
    if (fs.length) {
      add(c, table('Sensitivity to macro moves', [['Factor'], ['Move', 'num'], ['Per'], ['Evidence'], ['In plain words', 'wide']],
        fs.map((f) => {
          const ok = f.status === 'ok' && isNum(f.move_pct);
          const ev = txt(f.band, 30) || evidence(f.t);
          const plainText = txt(f.text, 300) || (ok ? (Math.abs(f.t) < 1 ? 'No clear link to the ' + txt(f.label, 60).toLowerCase() + ' after the market\'s move.'
            : 'Tends to ' + (f.move_pct > 0 ? 'rise' : 'fall') + ' about ' + NF2.format(Math.abs(f.move_pct)) + ' percent ' + txt(f.unit, 40) + ' in the ' + txt(f.label, 60).toLowerCase() + ', after the market\'s move (' + ev + ' evidence).') : 'Not enough history yet.');
          return [txt(f.label, 60), [ok ? pct(f.move_pct, 2, true) : NA, ok && Math.abs(f.t) >= 2 ? signCls(f.move_pct) : ''], txt(f.unit, 40).replace(/^per /, ''), ok ? ev.charAt(0).toUpperCase() + ev.slice(1) + (isNum(f.t) ? ' (t ' + NF1.format(f.t) + ')' : '') : NA, plainText];
        }), true));
    }
    const evs = arr(mi.events).map(obj).filter((e) => txt(e.event, 20));
    if (evs.length) {
      add(c, h('h4', 'st-sub', 'On FOMC and CPI days'), table('Moves on event days', [['Event'], ['Days', 'num'], ['Average move', 'num'], ['Average size', 'num'], ['Normal day', 'num'], ['Ratio', 'num'], ['Last']],
        evs.map((e) => {
          const last = obj(e.last);
          return [txt(e.event, 20), Number.isInteger(e.n) ? String(e.n) : NA, [pct(e.avg_move_pct, 2, true), signCls(e.avg_move_pct)], pct(e.avg_abs_move_pct, 2), pct(e.normal_abs_move_pct, 2), isNum(e.ratio) ? NF2.format(e.ratio) + 'x' : NA,
            isDay(last.date) ? dayShort(last.date) + ', ' + pct(last.move_pct, 2, true) : NA];
        }), true));
    }
    add(c, notes(mi.notes), read(obj(doc.assessment).macro));
    return c;
  }

  // ---------- the learner ----------
  const FEATURE_NAMES = {
    ret_5: '5 day return', ret_20: '20 day return', ret_60: '60 day return', dist_sma20: 'Distance from the 20 day average',
    dist_sma50: 'Distance from the 50 day average', dist_sma200: 'Distance from the 200 day average', rsi14: 'RSI 14',
    macd_hist: 'MACD histogram', pct_b: 'Bollinger %B', atr_pct: 'ATR as a share of price', vol_ratio: 'Volume against its average',
    rs_spy_20: '20 day return against SPY', spy_ret_5: 'SPY 5 day return', d_dgs10_5: '10 year yield, 5 day change', vix_rel: 'VIX level',
    short_ratio_z: 'Short volume ratio, z-score',
  };
  function meter(p, base) {
    const m = svg('svg', { viewBox: '0 0 100 10', preserveAspectRatio: 'none', 'aria-hidden': 'true', focusable: 'false' }, 'st-meter');
    add(m, svg('rect', { x: 0, y: 2, width: 100, height: 6 }, 'st-meter-bg'));
    add(m, svg('rect', { x: 0, y: 2, width: Math.max(0, Math.min(100, p * 100)).toFixed(1), height: 6 }, 'st-meter-v'));
    add(m, svg('line', { x1: 50, x2: 50, y1: 0, y2: 10 }, 'st-meter-mid'));
    if (isNum(base)) add(m, svg('line', { x1: (base * 100).toFixed(1), x2: (base * 100).toFixed(1), y1: 0, y2: 10 }, 'st-meter-base'));
    return m;
  }
  function scoreLine(label, s) {
    s = obj(s);
    if (!Number.isInteger(s.n) || s.n === 0) return label + ': no graded calls yet.';
    let t = label + ': right ' + share(s.hit_rate) + ' of ' + plural(s.n, 'call', 'calls');
    if (isNum(s.brier) && isNum(s.base_brier)) t += '; Brier ' + NF3.format(s.brier) + ' against ' + NF3.format(s.base_brier) + ' for the base rate';
    if (isNum(s.skill)) t += ' (' + (s.skill >= 0 ? NF1.format(s.skill * 100) + ' percent better' : NF1.format(-s.skill * 100) + ' percent worse') + ')';
    else if (s.n < 10) t += ' (too few to judge)';
    return t + '.';
  }
  function learnerCard(doc) {
    const lb = obj(doc.learner);
    const c = card('What this stock\'s learner says', '', 'st-c4 st-learner');
    const probs = obj(lb.probabilities);
    const base = obj(lb.base_rate);
    const hz = Object.keys(probs).filter((k) => /^\d{1,3}$/.test(k) && isNum(probs[k])).sort((a, b) => a - b);
    // The learner's own note says which horizon is still the base rate; the generic line only without it.
    if (lb.status === 'warming_up' && !/base rate/.test(txt(lb.note, 300))) add(c, h('p', 'hint hint-note', 'Warming up: this stock has too little history for a model yet, so its base rate stands in.'));
    if (txt(lb.note, 300)) add(c, h('p', 'hint', txt(lb.note, 300)));
    hz.forEach((k) => {
      const row = h('div', 'st-prob');
      add(row, h('p', 'st-prob-t', 'Up in ' + k + ' days: ' + NF0.format(probs[k] * 100) + ' percent'), meter(probs[k], base[k]));
      if (isNum(base[k])) add(row, h('p', 'st-prob-b', 'Base rate ' + NF0.format(base[k] * 100) + ' percent: how often it rose over ' + k + ' days in its own history.'));
      add(c, row);
    });
    if (!hz.length) add(c, h('p', 'hint', 'No probabilities today.'));
    const sc = obj(lb.scorecard);
    const keys = Array.from(new Set(Object.keys(obj(sc.live)).concat(Object.keys(obj(sc.backtest))))).filter((k) => /^\d{1,3}$/.test(k)).sort((a, b) => a - b);
    if (keys.length) {
      add(c, h('h4', 'st-sub', 'Scorecard, this stock only'));
      keys.forEach((k) => {
        add(c, h('p', 'st-score-h', k + ' day calls'), bullets([scoreLine('Live', obj(sc.live)[k]), scoreLine('Backtest', obj(sc.backtest)[k])], 'st-score'));
      });
      const k = keys[keys.length - 1];
      const liveCal = obj(obj(sc.live)[k]);
      const src = Number.isInteger(liveCal.n) && liveCal.n >= 30 ? ['live', liveCal] : ['backtest', obj(obj(sc.backtest)[k])];
      const bins = arr(src[1].calibration).map(obj).filter((b) => isNum(b.lo) && isNum(b.hi));
      if (bins.some((b) => b.n > 0)) {
        add(c, h('h4', 'st-sub', 'Calibration, ' + k + ' day ' + src[0] + ' calls'),
          table('Calibration', [['Forecast up'], ['Calls', 'num'], ['Average forecast', 'num'], ['Went up', 'num']],
            bins.map((b) => [NF0.format(b.lo * 100) + ' to ' + NF0.format(b.hi * 100) + '%', NF0.format(b.n || 0), b.n ? share(b.mean_p) : NA, b.n ? share(b.freq) : NA])),
          note('A well calibrated learner\'s "went up" share matches its average forecast in each row.'));
      }
    }
    const as = obj(sc.assessments);
    if (Number.isInteger(as.n)) {
      add(c, h('h4', 'st-sub', 'Claude\'s past calls on this stock'),
        h('p', 'st-small', as.n ? plural(as.n, 'call', 'calls') + ' graded after 20 days. Stance right ' + share(as.stance_hit_rate) + '. Target reached first ' + (as.target || 0) + ', stop first ' + (as.stop || 0) + ', neither ' + (as.neither || 0) + '.' : 'None graded yet. A call is graded 20 trading days after it was made.'));
    }
    const lessons = arr(lb.lessons).map(obj).filter((l) => txt(l.text, 220)).slice(-12).reverse();
    add(c, h('h4', 'st-sub', 'Lessons, this stock only'));
    if (lessons.length) add(c, bullets(lessons.map((l) => add(h('span'), isDay(l.date) ? h('span', 'st-lesson-d', dayShort(l.date) + ' ') : null, txt(l.text, 220)))));
    else add(c, h('p', 'hint', 'None yet. A lesson is added only when a graded call teaches something specific.'));
    const feats = strs(lb.features, 30, 30).map((f) => FEATURE_NAMES[f] || f);
    const bits = [];
    if (Number.isInteger(lb.training_rows)) bits.push('Trained on ' + plural(lb.training_rows, 'day', 'days') + ' of this stock\'s own history');
    if (feats.length) bits.push(plural(feats.length, 'signal', 'signals'));
    if (bits.length) {
      const det = h('details', 'st-details');
      add(det, h('summary', '', bits.join(', ')), bullets(feats, 'st-muted'));
      add(c, det);
    }
    return c;
  }

  // ---------- start ----------
  if (typeof bridge.onKeyChange === 'function') bridge.onKeyChange((has) => { teardown(); if (has) load(); });
  if (bridge.hasKey()) load();
})();
