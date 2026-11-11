/* News digest dashboard, app.js v7.
   Renders data/items.json, data/twitter_posts.json and the entry for the latest learning
   briefing (learn/index.json), and sends the items you tick to your worker for a summary.
   - The worker's address comes from window.NEWS_DIGEST_CONFIG.workerUrl (assets/js/config.js,
     written by the deploy job from the WORKER_URL repository variable). When it is empty, every
     summary is the local one and the "Set key" button is hidden.
   - The page runs under a strict Content-Security-Policy, so this file never writes inline
     styles or event-handler attributes. It shows and hides things with classes or the hidden
     attribute, and wires every control with addEventListener.
   - Every value that reaches innerHTML goes through esc(), and every link through httpUrl(),
     which allows absolute http(s) URLs only.
   - Summaries cost money, so a run never starts twice, only sends what you can see and have
     ticked, skips anything already summarized in this tab, and unticks what it finished. */
(() => {
  'use strict';

  // The URL is not a secret. The worker answers only requests that carry the access key, which
  // you type into the "Set key" dialog. The key is kept in this browser's localStorage only.
  // Only a bare https origin is used, the same check the deploy job makes; anything else is ''.
  const WORKER_BASE_URL = workerBase(window.NEWS_DIGEST_CONFIG);
  function workerBase(cfg) {
    const u = cfg && typeof cfg === 'object' && typeof cfg.workerUrl === 'string' ? cfg.workerUrl.trim().replace(/\/+$/, '') : '';
    return /^https:\/\/[A-Za-z0-9.-]+$/.test(u) ? u : '';
  }
  const HAS_WORKER = !!WORKER_BASE_URL;
  const WORKER_TIMEOUT_MS = 180000;
  const KEY_STORE = 'nd_worker_key_v1';
  const READ_KEY = 'nd_read_v1';
  const SELECT_KEY = 'nd_select_v1';
  const CACHE_KEY = 'nd_analysis_v1'; // sessionStorage: analyses already paid for in this tab
  const CACHE_MAX = 60;
  const READ_MAX = 2000;

  // ---------- storage: every access can throw (private windows, blocked site data) ----------
  function storeGet(area, key) { try { return window[area].getItem(key); } catch (e) { return null; } }
  function storeSet(area, key, value) { try { window[area].setItem(key, value); return true; } catch (e) { return false; } }
  function storeRemove(area, key) { try { window[area].removeItem(key); } catch (e) { /* nothing to do */ } }
  function loadSet(key) {
    try {
      const v = JSON.parse(storeGet('localStorage', key) || '[]');
      return new Set(Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []);
    } catch (e) {
      return new Set();
    }
  }
  function saveSet(key, set) { storeSet('localStorage', key, JSON.stringify(Array.from(set))); }

  // ---------- DOM ----------
  const $ = (id) => document.getElementById(id);
  const list = $('items-list');
  const moreWrap = $('items-more-wrap');
  const moreBtn = $('items-more');
  const countEl = $('items-count');
  const searchInput = $('search');
  const filterSource = $('filter-source');
  const filterTheme = $('filter-theme');
  const filterUnread = $('filter-unread');
  const markAllBtn = $('mark-all-read');
  const segButtons = Array.from(document.querySelectorAll('[data-bucket]'));
  const summarizeBtns = Array.from(document.querySelectorAll('[data-summarize]'));
  const panelBtn = $('summarize-selected');
  const deeperPanel = $('deeper');
  const selCount = $('sel-count');
  const selHidden = $('sel-hidden');
  const deeperMsg = $('deeper-msg');
  const dives = $('dives');
  const divesFoot = $('dives-foot');
  const clearDivesBtn = $('clear-dives');
  const selbar = $('selbar');
  const clearSelBtn = $('clear-selection');
  const xList = $('x-list');
  const xCount = $('x-count');
  const setKeyBtn = $('set-key');
  const keyDialog = $('key-dialog');
  const keyForm = $('key-form');
  const keyInput = $('key-input');
  const keyShow = $('key-show');
  const keyState = $('key-state');
  const keyError = $('key-error');
  const keyForget = $('key-forget');
  const keyCancel = $('key-cancel');

  // ---------- state ----------
  let items = [];
  let itemsById = new Map();
  let idx = null;
  let bucket = 'all';
  let visibleIds = new Set();
  let generatedAt = null;
  let running = false;
  let progress = '';
  let panelBtnInView = 'IntersectionObserver' in window;
  const readSet = loadSet(READ_KEY);
  const selectedSet = loadSet(SELECT_KEY);
  const memCache = new Map();

  // ---------- small helpers ----------
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC[c]); }
  // Only absolute http(s) links are kept. null, '', relative paths, javascript: and data: give null.
  function httpUrl(u) {
    if (typeof u !== 'string' || !u.trim()) return null;
    try {
      const p = new URL(u.trim());
      return p.protocol === 'https:' || p.protocol === 'http:' ? p.href : null;
    } catch (e) {
      return null;
    }
  }
  function safeUrl(u) { const h = httpUrl(u); return h ? esc(h) : '#'; }
  function str(v) { return typeof v === 'string' ? v.trim() : ''; }
  function strList(v, max) {
    return Array.isArray(v) ? v.filter((s) => typeof s === 'string' && s.trim()).map((s) => s.trim()).slice(0, max || 30) : [];
  }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
  function reduceMotion() { try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; } }

  // ---------- dates ----------
  const fmtTime = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
  const fmtDay = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
  const fmtDayYear = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  const fmtFull = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  const fmtToday = new Intl.DateTimeFormat(undefined, { weekday: 'long', month: 'long', day: 'numeric' });
  function dayKey(d) { return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate(); }
  function isToday(t) { return t > 0 && dayKey(new Date(t)) === dayKey(new Date()); }
  function isYesterday(t) { const y = new Date(); y.setDate(y.getDate() - 1); return t > 0 && dayKey(new Date(t)) === dayKey(y); }
  function when(t) {
    if (!t) return 'Date unknown';
    if (isToday(t)) return 'Today ' + fmtTime.format(t);
    if (isYesterday(t)) return 'Yesterday ' + fmtTime.format(t);
    return (new Date(t).getFullYear() === new Date().getFullYear() ? fmtDay : fmtDayYear).format(t);
  }
  function ago(t) {
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 90) return 'Just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    if (isYesterday(t)) return 'Yesterday';
    if (s < 7 * 86400) return Math.max(2, Math.round(s / 86400)) + 'd ago';
    return fmtDay.format(t);
  }

  // ---------- items ----------
  function normalizeItem(raw) {
    if (!raw || typeof raw !== 'object') return null;
    const id = typeof raw.id === 'string' || typeof raw.id === 'number' ? String(raw.id) : '';
    if (!id) return null;
    const t = Date.parse(raw.published);
    const readMinutes = Number.isFinite(raw.read_minutes) ? Math.max(1, Math.round(raw.read_minutes)) : 1;
    const durationSec = Number.isFinite(raw.duration_sec) && raw.duration_sec > 0 ? Math.round(raw.duration_sec) : null;
    let bucket = raw.bucket === 'deeper' || raw.bucket === 'highlight' ? raw.bucket : '';
    if (!bucket) bucket = durationSec !== null ? (durationSec <= 300 ? 'highlight' : 'deeper') : (readMinutes <= 5 ? 'highlight' : 'deeper');
    return {
      id,
      title: str(raw.title) || 'Untitled',
      url: httpUrl(raw.url),
      source: str(raw.source) || 'Unknown source',
      type: raw.source_type === 'youtube' ? 'youtube' : 'rss',
      published: typeof raw.published === 'string' ? raw.published : '',
      time: Number.isFinite(t) ? t : 0,
      summary: str(raw.summary),
      readMinutes,
      durationSec,
      themes: strList(raw.themes, 12),
      bucket,
      stale: raw.stale === true,
    };
  }
  function lengthLabel(i) {
    if (i.type === 'youtube' || i.durationSec) {
      return i.durationSec ? 'Video · ' + Math.max(1, Math.round(i.durationSec / 60)) + ' min' : 'Video';
    }
    return i.readMinutes + ' min read';
  }

  function getJson(path) {
    return fetch(path + '?ts=' + Date.now(), { cache: 'no-store' }).then((r) => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  $('today-label').textContent = 'Daily digest · ' + fmtToday.format(new Date());
  updateKeyBtn();
  if (!HAS_WORKER) {
    // No worker on this site: summaries are local only, so there is no key to set.
    setKeyBtn.hidden = true;
    $('deeper-hint').textContent = 'Tick items in the list, then summarize them here. This site has no summary worker set up, so you get a short local summary.';
    $('foot-worker').textContent = 'Summaries are made in this browser. No summary worker is set up for this site.';
  }

  getJson('learn/index.json')
    .then(renderBriefing)
    .catch(() => { /* no briefing yet, or it could not be read: the entry stays hidden */ });

  getJson('data/items.json')
    .then(onItems)
    .catch(() => {
      list.innerHTML = '<li class="empty">Could not load the digest. Try again in a minute.</li>';
      countEl.textContent = '';
      $('m-updated').textContent = 'Unknown';
    });

  getJson('data/twitter_posts.json')
    .then((d) => renderX(d && Array.isArray(d.accounts) ? d.accounts : []))
    .catch(() => { xList.innerHTML = '<li class="hint">Could not load the accounts.</li>'; });

  function onItems(data) {
    const raw = data && Array.isArray(data.items) ? data.items : [];
    const seen = new Set();
    items = [];
    raw.forEach((r) => {
      const i = normalizeItem(r);
      if (i && !seen.has(i.id)) { seen.add(i.id); items.push(i); }
    });
    items.sort((a, b) => b.time - a.time);
    itemsById = new Map(items.map((i) => [i.id, i]));
    pruneSets();
    const t = Date.parse(data && data.generated_at);
    generatedAt = Number.isFinite(t) ? t : null;
    renderMetrics(data && Array.isArray(data.sources) ? data.sources : null);
    populateFilters();
    buildIndex();
    render();
  }

  // Ticks for items that left the digest are dropped, and the read list is capped so it cannot grow forever.
  function pruneSets() {
    let changed = false;
    selectedSet.forEach((id) => { if (!itemsById.has(id)) { selectedSet.delete(id); changed = true; } });
    if (changed) saveSet(SELECT_KEY, selectedSet);
    if (readSet.size > READ_MAX) {
      const ids = Array.from(readSet);
      const keep = ids.filter((id) => itemsById.has(id));
      const room = READ_MAX - keep.length;
      const others = room > 0 ? ids.filter((id) => !itemsById.has(id)).slice(-room) : [];
      readSet.clear();
      others.concat(keep).forEach((id) => readSet.add(id));
      saveSet(READ_KEY, readSet);
    }
  }

  // ---------- metrics ----------
  function renderMetrics(sources) {
    $('m-items').textContent = String(items.length);
    $('m-new').textContent = String(items.filter((i) => isToday(i.time)).length);
    const health = $('health');
    const valid = sources ? sources.filter((s) => s && typeof s === 'object') : [];
    if (valid.length) {
      const bad = valid.filter((s) => s.ok !== true);
      $('m-sources').textContent = (valid.length - bad.length) + ' of ' + valid.length;
      $('m-sources-label').textContent = 'sources updating';
      if (bad.length) {
        const name = (s) => str(s.label) || 'Unnamed source';
        health.textContent = 'Not updating: ' + bad.map(name).join(', ');
        health.title = bad.map((s) => name(s) + (str(s.error) ? ': ' + str(s.error) : '')).join('\n');
      }
      health.hidden = !bad.length;
    } else {
      const n = new Set(items.map((i) => i.source)).size;
      $('m-sources').textContent = String(n);
      $('m-sources-label').textContent = n === 1 ? 'source in the digest' : 'sources in the digest';
      health.hidden = true;
    }
    renderUpdated();
  }
  function renderUpdated() {
    if (!generatedAt) { if (items.length) $('m-updated').textContent = 'Unknown'; return; }
    $('m-updated').textContent = ago(generatedAt);
    $('m-updated-label').textContent = 'last updated · ' + fmtFull.format(generatedAt);
  }
  setInterval(renderUpdated, 60000);

  // ---------- filters and search ----------
  function populateFilters() {
    const byName = (a, b) => a.localeCompare(b);
    fillSelect(filterSource, Array.from(new Set(items.map((i) => i.source))).sort(byName));
    fillSelect(filterTheme, Array.from(new Set([].concat(...items.map((i) => i.themes)))).sort(byName));
  }
  function fillSelect(sel, values) {
    const keep = sel.value;
    while (sel.options.length > 1) sel.remove(1);
    values.forEach((v) => {
      const o = document.createElement('option');
      o.value = v;
      o.textContent = v;
      sel.appendChild(o);
    });
    sel.value = values.indexOf(keep) >= 0 ? keep : '';
  }

  function buildIndex() {
    idx = null;
    if (!window.lunr || !items.length) return;
    try {
      idx = window.lunr(function () {
        this.ref('id');
        this.field('title');
        this.field('summary');
        items.forEach((d) => this.add({ id: d.id, title: d.title, summary: d.summary }));
      });
    } catch (e) {
      idx = null;
    }
  }

  // Case-insensitive substring match on title and summary, plus lunr's word matches
  // (stemmed, so "agents" finds "agent"). Lunr's query syntax throws QueryParseError on
  // input like "foo:", "~", "^" or a lone "+" or "-"; then the substring match stands alone.
  function matchIds(q) {
    const needle = q.toLowerCase();
    const ids = new Set();
    items.forEach((i) => { if ((i.title + '\n' + i.summary).toLowerCase().indexOf(needle) >= 0) ids.add(i.id); });
    if (idx) {
      try { idx.search(q).forEach((r) => ids.add(r.ref)); } catch (e) { /* QueryParseError: substring only */ }
    }
    return ids;
  }

  // Every filter except the length buttons. render(), "Mark all read" and "Summarize" all go
  // through visibleItems(), so they act on exactly what is on screen.
  function baseFiltered() {
    const q = searchInput.value.trim();
    const ids = q ? matchIds(q) : null;
    const src = filterSource.value;
    const th = filterTheme.value;
    const unread = filterUnread.checked;
    return items.filter((i) =>
      (!ids || ids.has(i.id)) &&
      (!src || i.source === src) &&
      (!th || i.themes.indexOf(th) >= 0) &&
      (!unread || !readSet.has(i.id)));
  }
  function visibleItems(base) {
    const b = base || baseFiltered();
    return bucket === 'all' ? b : b.filter((i) => i.bucket === bucket);
  }

  // ---------- list ----------
  // The list opens on the newest PAGE items. "Show all" expands it, and a search always
  // shows every match. Filters still apply to the full list first.
  const PAGE = 10;
  let expanded = false;
  let shownIds = new Set();
  function shownItems(vis) {
    return expanded || searchInput.value.trim() ? vis : vis.slice(0, PAGE);
  }

  function render(focus) {
    const base = baseFiltered();
    const vis = visibleItems(base);
    const shown = shownItems(vis);
    visibleIds = new Set(vis.map((i) => i.id));
    shownIds = new Set(shown.map((i) => i.id));
    if (!items.length) list.innerHTML = '<li class="empty">No items yet. The next run adds them.</li>';
    else list.innerHTML = shown.length ? shown.map(itemHtml).join('') : '<li class="empty">Nothing matches these filters.</li>';
    countEl.textContent = 'Showing ' + shown.length + ' of ' + vis.length;
    const canToggle = vis.length > PAGE && !searchInput.value.trim();
    moreWrap.hidden = !canToggle;
    moreBtn.textContent = expanded ? 'Show fewer' : 'Show all ' + vis.length;
    moreBtn.setAttribute('aria-expanded', String(expanded));
    const counts = { all: base.length, highlight: 0, deeper: 0 };
    base.forEach((i) => { counts[i.bucket] += 1; });
    document.querySelectorAll('[data-count-for]').forEach((el) => {
      el.textContent = String(counts[el.getAttribute('data-count-for')] || 0);
    });
    updateSelectionUi();
    if (focus) restoreFocus(focus);
  }

  function itemHtml(i) {
    const read = readSet.has(i.id);
    const selected = selectedSet.has(i.id);
    const cls = ['item', read ? 'is-read' : 'is-unread'];
    if (selected) cls.push('is-selected');
    if (i.stale) cls.push('is-stale');
    const id = esc(i.id);
    const title = esc(i.title);
    const titleHtml = i.url
      ? '<a class="item-link" href="' + safeUrl(i.url) + '" target="_blank" rel="noopener noreferrer">' + title + '</a>'
      : '<span>' + title + '</span>';
    // The middle dots between parts come from CSS, so a wrapped line never starts with one.
    const meta = '<span>' + esc(i.source) + '</span>' +
      (i.time ? '<time datetime="' + esc(new Date(i.time).toISOString()) + '">' + esc(when(i.time)) + '</time>' : '<span>' + esc(when(0)) + '</span>') +
      '<span class="tag">' + esc(lengthLabel(i)) + '</span>';
    const chips = (!read && isToday(i.time) ? '<span class="chip c-solid">New</span>' : '') +
      i.themes.map((t) => '<span class="chip c-cyan">' + esc(t) + '</span>').join('');
    return '<li class="' + cls.join(' ') + '" data-id="' + id + '">' +
      '<label class="pick"><input class="select-box" type="checkbox" data-id="' + id + '"' + (selected ? ' checked' : '') +
        ' aria-label="Select for a summary: ' + title + '"><span class="tk" aria-hidden="true"><svg viewBox="0 0 16 16" focusable="false"><path d="M3.5 8.5l3 3L12.5 5"/></svg></span></label>' +
      '<div class="item-body">' +
        '<h3 class="item-title">' + titleHtml + '</h3>' +
        '<p class="item-meta">' + meta + '</p>' +
        (i.summary ? '<p class="item-summary">' + esc(i.summary) + '</p>' : '') +
        '<div class="item-chips">' + chips + '</div>' +
        (i.stale ? '<p class="stale">From the last good fetch</p>' : '') +
      '</div>' +
      '<div class="item-actions"><button type="button" class="ghost read-toggle" data-id="' + id + '">' +
        (read ? 'Mark unread' : 'Mark read') + '<span class="vh">: ' + title + '</span></button></div>' +
      '</li>';
  }

  function rowFor(id) {
    for (const li of list.children) { if (li.getAttribute('data-id') === id) return li; }
    return null;
  }
  function refreshRow(id, focusSel) {
    const li = rowFor(id);
    const it = itemsById.get(id);
    if (!li || !it) return;
    const tpl = document.createElement('template');
    tpl.innerHTML = itemHtml(it);
    const fresh = tpl.content.firstElementChild;
    li.replaceWith(fresh);
    if (focusSel) { const el = fresh.querySelector(focusSel); if (el) el.focus(); }
  }
  // After a full render the row may be gone (for example under "Unread only"): focus the next one.
  function restoreFocus(focus) {
    const li = rowFor(focus.id) || list.children[Math.min(focus.index, list.children.length - 1)];
    const el = li && li.querySelector('.read-toggle');
    if (el) el.focus();
  }
  function setRead(id, read) {
    if (read) readSet.add(id); else readSet.delete(id);
    saveSet(READ_KEY, readSet);
  }
  function markOpened(link) {
    const li = link.closest('li.item');
    const id = li && li.getAttribute('data-id');
    if (!id || readSet.has(id)) return;
    setRead(id, true);
    // After the click finishes, so the link opens first; the row stays put until the next filter change.
    // Redrawing the row replaces the link, so a link that had focus gets it back on the new row.
    const hadFocus = document.activeElement === link;
    setTimeout(() => refreshRow(id, hadFocus ? 'a.item-link' : null), 0);
  }

  list.addEventListener('click', (e) => {
    const toggle = e.target.closest('.read-toggle');
    if (toggle) {
      const id = toggle.getAttribute('data-id');
      setRead(id, !readSet.has(id));
      if (filterUnread.checked) render({ id, index: Array.prototype.indexOf.call(list.children, toggle.closest('li')) });
      else refreshRow(id, '.read-toggle');
      return;
    }
    const link = e.target.closest('a.item-link');
    if (link) markOpened(link);
  });
  list.addEventListener('auxclick', (e) => {
    const link = e.button === 1 && e.target.closest('a.item-link');
    if (link) markOpened(link);
  });
  list.addEventListener('change', (e) => {
    const cb = e.target.closest('.select-box');
    if (!cb) return;
    const id = cb.getAttribute('data-id');
    if (cb.checked) selectedSet.add(id); else selectedSet.delete(id);
    saveSet(SELECT_KEY, selectedSet);
    const li = cb.closest('li.item');
    if (li) li.classList.toggle('is-selected', cb.checked);
    updateSelectionUi();
  });

  let renderQueued = false;
  function scheduleRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => { renderQueued = false; render(); });
  }
  searchInput.addEventListener('input', scheduleRender);
  filterSource.addEventListener('change', () => render());
  filterTheme.addEventListener('change', () => render());
  filterUnread.addEventListener('change', () => render());
  segButtons.forEach((b) => b.addEventListener('click', () => {
    bucket = b.getAttribute('data-bucket') || 'all';
    segButtons.forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    render();
  }));
  moreBtn.addEventListener('click', () => {
    const firstNew = expanded ? null : visibleItems()[PAGE];
    expanded = !expanded;
    render();
    if (firstNew) {
      const row = list.querySelector('li[data-id="' + CSS.escape(firstNew.id) + '"]');
      const link = row && row.querySelector('a, input');
      if (link) link.focus({ preventScroll: true });
    } else {
      moreBtn.scrollIntoView({ block: 'nearest' });
    }
  });
  // Marks what is on screen, so items folded away by "Show all" stay unread.
  markAllBtn.addEventListener('click', () => {
    const vis = visibleItems().filter((i) => shownIds.has(i.id));
    if (!vis.length) return;
    vis.forEach((i) => readSet.add(i.id));
    saveSet(READ_KEY, readSet);
    render();
  });

  // ---------- selection ----------
  function selectedVisible() { return items.filter((i) => visibleIds.has(i.id) && selectedSet.has(i.id)); }
  function updateSelectionUi() {
    const n = selectedVisible().length;
    let hidden = 0;
    selectedSet.forEach((id) => { if (itemsById.has(id) && !visibleIds.has(id)) hidden += 1; });
    summarizeBtns.forEach((b) => {
      b.disabled = running || n === 0;
      if (running) b.setAttribute('aria-busy', 'true'); else b.removeAttribute('aria-busy');
      b.textContent = running ? progress : (n ? 'Summarize ' + n + ' selected' : 'Tick items to summarize');
    });
    selCount.textContent = n + ' selected';
    selHidden.hidden = !hidden;
    selHidden.textContent = !hidden ? '' : hidden === 1
      ? '1 more ticked item is hidden by the filters, so it is left out.'
      : hidden + ' more ticked items are hidden by the filters, so they are left out.';
    clearDivesBtn.disabled = running;
    const showBar = (n > 0 || running) && !panelBtnInView;
    selbar.hidden = !showBar;
    document.body.classList.toggle('has-selbar', showBar);
  }
  if ('IntersectionObserver' in window) {
    new IntersectionObserver((entries) => {
      panelBtnInView = entries[entries.length - 1].isIntersecting;
      updateSelectionUi();
    }).observe(panelBtn);
  }
  function unselect(id) {
    selectedSet.delete(id);
    saveSet(SELECT_KEY, selectedSet);
    const li = rowFor(id);
    if (li) {
      li.classList.remove('is-selected');
      const cb = li.querySelector('.select-box');
      if (cb) cb.checked = false;
    }
  }
  clearSelBtn.addEventListener('click', () => {
    Array.from(selectedSet).forEach(unselect);
    updateSelectionUi();
  });

  // ---------- worker key ----------
  function getWorkerKey() { return storeGet('localStorage', KEY_STORE) || ''; }
  function setWorkerKey(k) {
    if (k) storeSet('localStorage', KEY_STORE, k); else storeRemove('localStorage', KEY_STORE);
    updateKeyBtn();
  }
  function updateKeyBtn() {
    const has = !!getWorkerKey();
    setKeyBtn.textContent = has ? 'Key set' : 'Set key';
    setKeyBtn.classList.toggle('is-set', has);
    setKeyBtn.title = has ? 'The worker access key is set in this browser. Change or forget it.' : 'Set the worker access key for summaries';
  }
  window.addEventListener('storage', (e) => { if (e.key === KEY_STORE || e.key === null) updateKeyBtn(); });

  let keyResolve = null;
  let keyOpener = null;
  // Resolves with 'saved', 'kept', 'forgot' or 'cancel' once the dialog closes.
  function openKeyDialog() {
    if (typeof keyDialog.showModal !== 'function') {
      const k = window.prompt('Worker access key (stored only in this browser).', '');
      if (k !== null && k.trim()) setWorkerKey(k.trim());
      return Promise.resolve(k !== null && k.trim() ? 'saved' : 'cancel');
    }
    if (keyDialog.open) return Promise.resolve('cancel');
    return new Promise((resolve) => {
      keyResolve = resolve;
      keyOpener = document.activeElement;
      keyDialog.returnValue = '';
      keyInput.value = '';
      keyInput.type = 'password';
      keyShow.textContent = 'Show';
      keyError.hidden = true;
      keyError.textContent = '';
      const has = !!getWorkerKey();
      keyState.textContent = has ? 'A key is set in this browser.' : 'No key is set in this browser yet.';
      keyForget.disabled = !has;
      keyDialog.showModal();
      keyInput.focus();
    });
  }
  function closeKeyDialog(result) { if (keyDialog.open) keyDialog.close(result); }
  keyDialog.addEventListener('close', () => {
    keyInput.value = '';
    keyInput.type = 'password';
    const result = keyDialog.returnValue || 'cancel';
    if (keyResolve) { const done = keyResolve; keyResolve = null; done(result); }
    if (result === 'saved' && (deeperMsg.dataset.kind === 'auth' || deeperMsg.dataset.kind === 'nokey')) clearMsg();
    if (keyOpener && document.contains(keyOpener) && !keyOpener.disabled) keyOpener.focus();
    else if (!panelBtn.disabled) panelBtn.focus(); // the opener went away (for example the message's own button)
    keyOpener = null;
  });
  keyForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const v = keyInput.value.trim();
    if (!v) {
      if (getWorkerKey()) { closeKeyDialog('kept'); return; }
      keyError.textContent = 'Type the key, or press Cancel.';
      keyError.hidden = false;
      keyInput.focus();
      return;
    }
    setWorkerKey(v);
    closeKeyDialog('saved');
  });
  keyShow.addEventListener('click', () => {
    const show = keyInput.type === 'password';
    keyInput.type = show ? 'text' : 'password';
    keyShow.textContent = show ? 'Hide' : 'Show';
    keyInput.focus();
  });
  keyForget.addEventListener('click', () => { setWorkerKey(''); closeKeyDialog('forgot'); });
  keyCancel.addEventListener('click', () => closeKeyDialog('cancel'));
  setKeyBtn.addEventListener('click', () => { openKeyDialog(); });
  document.addEventListener('click', (e) => {
    if (e.target.closest && e.target.closest('[data-open-key]')) openKeyDialog();
  });

  // ---------- worker calls ----------
  // Returns { kind, status, body }. kind is one of: ok, nokey, auth (the guard refused the key,
  // which is then forgotten), unconfigured (the guard has no key set), rate (the guard's 429),
  // http (any other error status, usually about this one item), timeout or network.
  async function workerFetch(path, init) {
    if (!HAS_WORKER) return { kind: 'noworker' };
    const key = getWorkerKey();
    if (!key) return { kind: 'nokey' };
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), WORKER_TIMEOUT_MS) : 0;
    const headers = Object.assign({}, init && init.headers, { authorization: 'Bearer ' + key });
    try {
      const res = await fetch(WORKER_BASE_URL + path, Object.assign({}, init, {
        headers,
        credentials: 'omit',
        cache: 'no-store',
        referrerPolicy: 'no-referrer',
        signal: ctrl ? ctrl.signal : undefined,
      }));
      let body = null;
      try { body = await res.json(); } catch (e) { if (e && e.name === 'AbortError') return { kind: 'timeout' }; }
      if (res.ok) return { kind: 'ok', status: res.status, body };
      // The guard (worker/auth-guard.js) marks its own refusals with x-guard: deny and exposes that
      // header to this page. Only those speak for the key or the whole worker: the key is forgotten
      // only on the guard's 401. A 401, 503 or 429 without the mark came from the worker's own code,
      // for example relayed from an upstream site, so it is a note on this one item and the key stays.
      const fromGuard = res.headers.get('x-guard') === 'deny';
      if (fromGuard && res.status === 401) {
        setWorkerKey('');
        return { kind: 'auth', status: 401 };
      }
      if (fromGuard && res.status === 503) return { kind: 'unconfigured', status: 503 };
      if (fromGuard && res.status === 429) return { kind: 'rate', status: 429 };
      return { kind: 'http', status: res.status };
    } catch (e) {
      return { kind: e && e.name === 'AbortError' ? 'timeout' : 'network' };
    } finally {
      clearTimeout(timer);
    }
  }
  // These stop the worker for the rest of a run: every other item would fail the same way.
  const STOPS = { noworker: true, nokey: true, auth: true, unconfigured: true, rate: true };

  // Exact host match, so a look-alike such as notyoutu.be or evil.example/x.youtube.com is not a video.
  function youTubeId(u) {
    let url;
    try { url = new URL(u); } catch (e) { return null; }
    const host = url.hostname.toLowerCase();
    let id = null;
    if (host === 'youtu.be') id = url.pathname.slice(1).split('/')[0];
    else if (host === 'youtube.com' || host.endsWith('.youtube.com')) {
      if (url.pathname.indexOf('/shorts/') === 0) id = url.pathname.split('/')[2];
      else if (url.searchParams.get('v')) id = url.searchParams.get('v');
      else { const m = url.pathname.match(/^\/(?:embed|live)\/([\w-]+)/); if (m) id = m[1]; }
    }
    return id && /^[\w-]{6,20}$/.test(id) ? id : null;
  }

  // Keeps only well-formed fields: tldr must be a string, lists must hold strings, and quote
  // entries may be strings, objects or junk (null, numbers), which are dropped.
  function normalizeAnalysis(a) {
    if (typeof a === 'string') a = { tldr: a };
    if (!a || typeof a !== 'object' || Array.isArray(a)) return null;
    const quotes = Array.isArray(a.notable_quotes) ? a.notable_quotes.map((q) => {
      if (typeof q === 'string') return { quote: q.trim(), at: '' };
      if (q && typeof q === 'object' && !Array.isArray(q)) {
        const at = typeof q.at === 'string' || typeof q.at === 'number' ? String(q.at).trim() : '';
        return { quote: str(q.quote), at };
      }
      return null;
    }).filter((q) => q && q.quote).slice(0, 12) : [];
    const out = {
      tldr: str(a.tldr),
      key_points: strList(a.key_points),
      pros: strList(a.pros),
      cons: strList(a.cons),
      notable_quotes: quotes,
      actions: strList(a.actions),
    };
    const empty = !out.tldr && !out.key_points.length && !out.pros.length && !out.cons.length && !out.notable_quotes.length && !out.actions.length;
    return empty ? null : out;
  }

  async function analyzeViaWorker(it) {
    const yt = youTubeId(it.url);
    // YouTube items ask the worker to transcribe the first ten or so minutes with Whisper
    // ('preview'). That is a cost and quality choice: set force_whisper and prefer_whisper to
    // false to let the worker try the free captions first.
    const payload = yt
      ? { yt_id: yt, title: it.title, url: it.url, source: it.source, published: it.published, force_whisper: true, prefer_whisper: true, mode: 'preview', detail: 'long' }
      : { url: it.url, title: it.title, source: it.source, published: it.published, detail: 'long' };
    const r = await workerFetch('/analyze', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (r.kind !== 'ok') return r;
    const a = normalizeAnalysis(r.body && typeof r.body === 'object' ? r.body.analysis : null);
    return a ? { kind: 'ok', analysis: a } : { kind: 'malformed' };
  }

  // Full text for the local summary when /analyze failed for this one item.
  async function fetchFullText(it) {
    const textOf = (r) => (r.kind === 'ok' && r.body && typeof r.body.text === 'string' ? r.body.text.trim() : '');
    const yt = youTubeId(it.url);
    if (yt) {
      const r = await workerFetch('/yt-transcript?id=' + encodeURIComponent(yt));
      if (textOf(r)) return { kind: 'ok', text: textOf(r) };
      if (STOPS[r.kind]) return r;
    }
    const r2 = await workerFetch('/extract?url=' + encodeURIComponent(it.url));
    if (textOf(r2)) return { kind: 'ok', text: textOf(r2) };
    return r2.kind === 'ok' ? { kind: 'malformed' } : r2;
  }

  // ---------- analysis cache, per tab ----------
  function cacheLoad() {
    try {
      const v = JSON.parse(storeGet('sessionStorage', CACHE_KEY) || '[]');
      return Array.isArray(v) ? v.filter((e) => Array.isArray(e) && typeof e[0] === 'string') : [];
    } catch (e) {
      return [];
    }
  }
  function cacheGet(id) {
    if (memCache.has(id)) return memCache.get(id);
    const hit = cacheLoad().find((e) => e[0] === id);
    const a = hit ? normalizeAnalysis(hit[1]) : null;
    if (a) memCache.set(id, a);
    return a;
  }
  function cachePut(id, a) {
    memCache.set(id, a);
    const entries = cacheLoad().filter((e) => e[0] !== id);
    entries.push([id, a]);
    storeSet('sessionStorage', CACHE_KEY, JSON.stringify(entries.slice(-CACHE_MAX)));
  }

  // ---------- deeper dives ----------
  const MESSAGES = {
    auth: 'The worker rejected the access key. Set it again.',
    unconfigured: 'The worker has no access key configured yet.',
    rate: 'The worker is rate limited. Try again in a minute.',
    network: 'Could not reach the worker. Check your connection and try again.',
    nokey: 'No access key is set, so these are local summaries.',
  };
  function showMsg(kind) {
    deeperMsg.textContent = '';
    deeperMsg.dataset.kind = kind;
    deeperMsg.classList.toggle('is-info', kind === 'nokey');
    const p = document.createElement('p');
    p.textContent = MESSAGES[kind];
    deeperMsg.appendChild(p);
    const p2 = document.createElement('p');
    p2.textContent = kind === 'nokey'
      ? 'Set the key to get full summaries from your worker.'
      : 'Until then you get local summaries, and those items stay ticked so you can try again.';
    deeperMsg.appendChild(p2);
    if (kind === 'auth' || kind === 'nokey') {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ghost';
      b.textContent = 'Set key';
      b.setAttribute('data-open-key', '');
      deeperMsg.appendChild(b);
    }
  }
  function clearMsg() {
    deeperMsg.textContent = '';
    delete deeperMsg.dataset.kind;
  }

  function diveHead(it, tagText) {
    const title = it.url
      ? '<a href="' + safeUrl(it.url) + '" target="_blank" rel="noopener noreferrer">' + esc(it.title) + '</a>'
      : esc(it.title);
    return '<h3 class="dive-title">' + title + '</h3>' +
      '<p class="dive-meta"><span>' + esc(it.source) + '</span><span>' + esc(when(it.time)) + '</span>' +
      '<span class="tag">' + esc(tagText) + '</span></p>';
  }
  function listHtml(title, arr) {
    return arr.length ? '<h4>' + esc(title) + '</h4><ul>' + arr.map((s) => '<li>' + esc(s) + '</li>').join('') + '</ul>' : '';
  }
  function analysisHtml(it, a, cached) {
    let h = '<article class="dive" data-id="' + esc(it.id) + '">' + diveHead(it, cached ? 'Cached' : 'Worker');
    if (a.tldr) h += '<p class="dive-tldr"><b>TL;DR</b>' + esc(a.tldr) + '</p>';
    h += listHtml('Key points', a.key_points) + listHtml('Upsides', a.pros) + listHtml('Risks and caveats', a.cons);
    if (a.notable_quotes.length) {
      h += '<h4>Notable quotes</h4><ul>' + a.notable_quotes.map((q) =>
        '<li>“' + esc(q.quote) + '”' + (q.at ? '<span class="at">' + esc(q.at) + '</span>' : '') + '</li>').join('') + '</ul>';
    }
    h += listHtml('Actions', a.actions);
    return h + '</article>';
  }
  function fallbackHtml(it, bullets, note) {
    let h = '<article class="dive is-local" data-id="' + esc(it.id) + '">' + diveHead(it, 'Local summary');
    if (note) h += '<p class="dive-note">' + esc(note) + '</p>';
    h += bullets.length
      ? '<ul>' + bullets.map((b) => '<li>' + esc(b) + '</li>').join('') + '</ul>'
      : '<p class="dive-note">There is no text to summarize. Open the item to read it.</p>';
    return h + '</article>';
  }
  function noteFor(r) {
    switch (r.kind) {
      case 'nolink': return 'This item has no web link, so the summary uses the feed text only.';
      case 'http': return 'The worker could not summarize this one (HTTP ' + r.status + ').';
      case 'malformed': return 'The worker sent an answer this page could not use.';
      case 'timeout': return 'The worker took too long on this one.';
      case 'network': return 'The worker could not be reached.';
      default: return '';
    }
  }
  function localBullets(it, text) {
    const bullets = summarizeText(text || it.summary || '', 6);
    if (bullets.length) return bullets;
    return it.summary ? [it.summary] : [];
  }
  // A section replaces any earlier one for the same item, so a rerun never shows it twice.
  function placeDive(run, id, html) {
    Array.from(dives.querySelectorAll('.dive')).forEach((d) => {
      if (d.getAttribute('data-id') !== id) return;
      const parent = d.parentElement;
      d.remove();
      if (parent && parent !== run && !parent.children.length) parent.remove();
    });
    run.insertAdjacentHTML('beforeend', html);
  }

  async function summarizeOne(it, state) {
    const cached = cacheGet(it.id);
    if (cached) return { html: analysisHtml(it, cached, true), ok: true };
    let r;
    if (!it.url) r = { kind: 'nolink' };
    else if (state.workerOff) r = { kind: state.workerOff };
    else r = await analyzeViaWorker(it);
    if (r.kind === 'ok') {
      cachePut(it.id, r.analysis);
      return { html: analysisHtml(it, r.analysis, false), ok: true };
    }
    if (STOPS[r.kind] && !state.workerOff) { state.workerOff = r.kind; showMsg(r.kind); }
    if (r.kind === 'network' && !state.netShown) { state.netShown = true; showMsg('network'); }
    let text = '';
    if ((r.kind === 'http' || r.kind === 'malformed') && !state.workerOff) {
      const full = await fetchFullText(it);
      if (full.kind === 'ok') text = full.text;
      else if (STOPS[full.kind] && !state.workerOff) { state.workerOff = full.kind; showMsg(full.kind); }
    }
    // With no link, or no worker on this site, the local summary is final and the item is unticked.
    return { html: fallbackHtml(it, localBullets(it, text), noteFor(r)), ok: r.kind === 'nolink' || r.kind === 'noworker' };
  }

  async function summarize() {
    if (running) return; // in-flight guard: clicks during a run do nothing
    const targets = selectedVisible();
    if (!targets.length) return;
    running = true;
    progress = 'Starting…';
    updateSelectionUi();
    clearMsg();
    const state = { workerOff: HAS_WORKER ? null : 'noworker', netShown: false };
    const run = document.createElement('div');
    run.className = 'run';
    dives.prepend(run);
    divesFoot.hidden = false;
    let askedKey = false;
    try {
      if (HAS_WORKER && !getWorkerKey() && targets.some((i) => i.url && !cacheGet(i.id))) {
        progress = 'Waiting for the key…';
        updateSelectionUi();
        askedKey = true;
        await openKeyDialog();
        if (!getWorkerKey()) { state.workerOff = 'nokey'; showMsg('nokey'); }
      }
      for (let n = 0; n < targets.length; n += 1) {
        const it = targets[n];
        if (!selectedSet.has(it.id)) continue; // unticked while the run was going
        progress = 'Summarizing ' + (n + 1) + ' of ' + targets.length + '…';
        updateSelectionUi();
        let out;
        try {
          out = await summarizeOne(it, state);
        } catch (err) {
          out = { html: fallbackHtml(it, localBullets(it, ''), 'Something went wrong with this one, so this is a short local summary.'), ok: false };
        }
        placeDive(run, it.id, out.html);
        if (out.ok) unselect(it.id);
      }
    } finally {
      running = false;
      progress = '';
      if (!run.children.length) run.remove();
      divesFoot.hidden = !dives.children.length;
      updateSelectionUi();
      // The key dialog closed while the summarize buttons were disabled, so it had nothing to give
      // focus back to and Tab would restart at the top of the page. Put focus back in the panel,
      // without scrolling, so a reader who scrolled away during the run stays where they are.
      // A quick run can end before the browser moves focus off the closed dialog, so that counts too.
      const lost = !document.activeElement || document.activeElement === document.body || keyDialog.contains(document.activeElement);
      if (askedKey && lost) {
        const t = !panelBtn.disabled ? panelBtn : (run.querySelector('a') || clearDivesBtn);
        if (t) t.focus({ preventScroll: true });
      }
    }
  }

  summarizeBtns.forEach((b) => b.addEventListener('click', () => {
    if (running) return;
    if (b.hasAttribute('data-scroll')) deeperPanel.scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'start' });
    summarize();
  }));
  clearDivesBtn.addEventListener('click', () => {
    if (running) return;
    dives.textContent = '';
    divesFoot.hidden = true;
    clearMsg();
  });

  // ---------- today's briefing ----------
  // learn/index.json: {"latest": "YYYY-MM-DD" or null, "days": [{"date", "topics": [titles]}]}.
  // The entry shows only for a well-formed latest date; the link is built from that date alone.
  const fmtBriefDay = new Intl.DateTimeFormat(undefined, { weekday: 'long', month: 'long', day: 'numeric', timeZone: 'UTC' });
  function briefDate(v) {
    if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return null;
    const d = new Date(v + 'T00:00:00Z');
    return Number.isFinite(d.getTime()) && d.toISOString().slice(0, 10) === v ? d : null;
  }
  function localDay() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }
  function renderBriefing(data) {
    const latest = data && typeof data === 'object' ? data.latest : null;
    const day = briefDate(latest);
    if (!day) return;
    const days = Array.isArray(data.days) ? data.days : [];
    const entry = days.find((d) => d && typeof d === 'object' && d.date === latest);
    const titles = strList(entry && entry.topics, 8).map((t) => (t.length > 160 ? t.slice(0, 159).trimEnd() + '…' : t));
    $('brief-label').textContent = latest === localDay() ? "Today's briefing" : 'Latest briefing';
    $('brief-date').textContent = fmtBriefDay.format(day);
    $('brief-meta').textContent = titles.length ? plural(titles.length, 'topic', 'topics') : 'No topics';
    const ol = $('brief-topics');
    ol.textContent = '';
    titles.forEach((t) => {
      const li = document.createElement('li');
      li.textContent = t;
      ol.appendChild(li);
    });
    ol.hidden = !titles.length;
    $('brief-open').setAttribute('href', 'learn/' + latest + '/');
    $('briefing').hidden = false;
    $('nav-briefing').hidden = false;
  }

  // ---------- On X ----------
  function renderX(accounts) {
    const valid = accounts.filter((a) => a && typeof a === 'object' && str(a.handle));
    xCount.hidden = !valid.length;
    xCount.textContent = plural(valid.length, 'account', 'accounts');
    if (!valid.length) { xList.innerHTML = '<li class="hint">No accounts listed.</li>'; return; }
    let anyPosts = false;
    const rows = valid.map((a) => {
      const handle = esc(str(a.handle).replace(/^@+/, ''));
      const url = httpUrl(a.url);
      const posts = Array.isArray(a.posts) ? a.posts.filter((p) => p && typeof p === 'object' && str(p.text)).slice(0, 2) : [];
      if (posts.length) anyPosts = true;
      let h = '<li class="acct"><div class="acct-row"><span class="acct-handle">@' + handle + '</span>';
      if (url) {
        h += '<a class="acct-link" href="' + safeUrl(url) + '" target="_blank" rel="noopener noreferrer">Open profile' +
          '<span aria-hidden="true"> ↗</span><span class="vh"> of @' + handle + ' on X</span></a>';
      }
      h += '</div>';
      if (posts.length) {
        h += '<ul class="posts">' + posts.map((p) => {
          const t = Date.parse(p.published);
          const purl = httpUrl(p.url);
          const text = esc(str(p.text));
          return '<li class="post">' +
            (Number.isFinite(t) ? '<time datetime="' + esc(new Date(t).toISOString()) + '">' + esc(fmtFull.format(t)) + '</time>' : '') +
            (purl ? '<a href="' + safeUrl(purl) + '" target="_blank" rel="noopener noreferrer">' + text + '</a>' : text) +
            '</li>';
        }).join('') + '</ul>';
      }
      return h + '</li>';
    });
    xList.innerHTML = rows.join('') +
      (anyPosts ? '' : '<li class="hint accts-note">Posts are not collected right now, so each account links to its profile.</li>');
  }

  // ---------- local summary: the top sentences by word frequency ----------
  const STOP = new Set(('a,an,the,of,in,on,for,to,from,by,with,as,at,that,this,these,those,and,or,not,be,is,are,was,were,been,being,has,have,had,do,does,did,will,would,shall,should,can,could,may,might,must,if,then,else,when,while,about,into,over,after,before,up,down,out,off,again,further,here,there,why,how,all,any,both,each,few,more,most,other,some,such,no,nor,only,own,same,so,than,too,very').split(','));
  function tokenizeSentences(text) {
    return String(text || '').replace(/\s+/g, ' ')
      .split(/(?:\.|\?|!)\s+(?=[A-Z0-9"'(])/)
      .filter((s) => s && s.trim().length > 20)
      .slice(0, 60);
  }
  function tokenizeWords(s) { return s.toLowerCase().match(/[a-z0-9']+/g) || []; }
  function summarizeText(text, maxSentences) {
    const max = maxSentences || 6;
    const sentences = tokenizeSentences(text);
    if (sentences.length <= max) return sentences.map((s) => s.trim());
    const tf = Object.create(null);
    const sWords = sentences.map(tokenizeWords);
    sWords.forEach((words) => words.forEach((w) => { if (!STOP.has(w)) tf[w] = (tf[w] || 0) + 1; }));
    const scores = sWords.map((words) => words.reduce((acc, w) => acc + (STOP.has(w) ? 0 : (tf[w] || 0)), 0));
    return scores.map((s, i) => [s, i])
      .sort((a, b) => b[0] - a[0])
      .slice(0, max)
      .map((x) => x[1])
      .sort((a, b) => a - b)
      .map((i) => sentences[i].trim());
  }
})();
