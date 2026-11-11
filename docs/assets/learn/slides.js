/* Learning briefing slideshow, slides.js v2.
   Loaded by docs/learn/viewer.html, which the commit job copies into each dated folder
   (docs/learn/YYYY-MM-DD/index.html). It reads ./briefing.json from that folder and builds
   the slides: a title slide, three slides per topic (what happened; why it matters; what to
   read first and the open question), and a closing slide. The same order as briefing.pptx.
   - The page runs under a strict Content-Security-Policy: no inline script or style. This file
     never writes markup strings or style attributes. Every piece of text goes in with
     textContent, every element with createElement, and slides show, hide and change size
     with a class.
   - The briefing came from a model and from feeds, so it is treated as hostile: each value is
     type checked and capped, and a link is kept only when it is an absolute https URL.
   - No scrolling text where the screen allows: a slide whose text is taller than the screen
     steps down the type sizes in slides.css (.fit-1 to .fit-8) until it fits. Only when the
     smallest size is still too long does the slide scroll, under a "Scroll for more" cue.
   - Navigation: arrow keys, Page Up and Page Down, Home and End, space (shift+space goes back),
     swipe, the on-screen buttons, and #slide-N in the address for a direct link. */
(() => {
  'use strict';

  const MAX_BYTES = 2000000;
  const MAX_TOPICS = 8;
  const MAX_LINKS = 5;
  const SLIDES_PER_TOPIC = 3;
  const FIT_STEPS = 8;          // slides.css defines .fit-1 to .fit-8, each a little smaller
  const TWO_COLUMNS_FROM = 600; // characters of prose set in two columns on a wide screen
  const CAP = { title: 160, what_happened: 3600, why_it_matters: 3000, open_question: 600, why: 500,
    email_summary: 1500, source: 120, read_title: 300, lane: 60, model: 80 };

  const $ = (id) => document.getElementById(id);
  const deck = $('deck');
  const controls = $('controls');
  const prevBtn = $('prev');
  const nextBtn = $('next');
  const countEl = $('count');
  const countLive = $('count-live');
  const progress = $('progress');
  const barDate = $('bar-date');
  const pptxLink = $('pptx-link');

  let slides = [];
  let current = 0;
  let more = null; // the "Scroll for more" cue

  // ---------- small helpers ----------
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null && text !== '') e.textContent = text;
    return e;
  }
  // A string with control characters removed, trimmed and capped, or ''.
  function text(v, max, multiline) {
    if (typeof v !== 'string') return '';
    let s = v.replace(multiline ? /[\u0000-\u0009\u000b-\u001f\u007f]/g : /[\u0000-\u001f\u007f]/g, multiline ? '' : ' ');
    s = multiline ? s.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n') : s.replace(/\s+/g, ' ');
    s = s.trim();
    return s.length > max ? s.slice(0, max - 1).trimEnd() + '…' : s;
  }
  function int(v, dflt, lo, hi) {
    return typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, Math.round(v))) : dflt;
  }
  // Only absolute https links are kept, the same rule as render.py. Plain http, javascript:,
  // data:, relative paths and junk give null.
  function httpsUrl(u) {
    if (typeof u !== 'string' || !u.trim()) return null;
    try {
      const p = new URL(u.trim());
      return p.protocol === 'https:' && p.hostname ? p.href : null;
    } catch (e) {
      return null;
    }
  }
  function validDate(v) {
    if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return null;
    const d = new Date(v + 'T00:00:00Z');
    return Number.isFinite(d.getTime()) && d.toISOString().slice(0, 10) === v ? d : null;
  }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
  function reduceMotion() { try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; } }

  const fmtLong = new Intl.DateTimeFormat('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
  const fmtShort = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });

  // ---------- the briefing, checked ----------
  function clean(doc) {
    if (!doc || typeof doc !== 'object' || Array.isArray(doc)) return null;
    const day = validDate(doc.date);
    if (!day) return null;
    if (doc.topics != null && !Array.isArray(doc.topics)) return null;
    const usage = doc.usage && typeof doc.usage === 'object' ? doc.usage : {};
    const topics = [];
    (doc.topics || []).slice(0, MAX_TOPICS).forEach((t) => {
      if (!t || typeof t !== 'object' || Array.isArray(t)) return;
      const title = text(t.title, CAP.title);
      if (!title) return;
      const reads = (Array.isArray(t.read_first) ? t.read_first : []).map((r) => {
        if (!r || typeof r !== 'object') return null;
        const url = httpsUrl(r.url);
        if (!url) return null;
        return { title: text(r.title, CAP.read_title) || 'Read', url, source: text(r.source, CAP.source), why: text(r.why, CAP.why) };
      }).filter(Boolean).slice(0, MAX_LINKS);
      const sig = t.signals && typeof t.signals === 'object' ? t.signals : {};
      topics.push({
        title,
        lanes: (Array.isArray(t.lanes) ? t.lanes : []).map((x) => text(x, CAP.lane)).filter(Boolean).slice(0, 3),
        offLane: t.off_lane === true,
        whatHappened: text(t.what_happened, CAP.what_happened, true),
        whyItMatters: text(t.why_it_matters, CAP.why_it_matters, true),
        readFirst: reads,
        openQuestion: text(t.open_question, CAP.open_question),
        minutes: int(t.reading_minutes, 3, 1, 30),
        signals: {
          sources: int(sig.sources, 1, 0, 100),
          primary: sig.primary === true,
          fresh: sig.new_since_last_week === true,
          laneHits: int(sig.lane_hits, 0, 0, 100),
        },
      });
    });
    const cost = typeof usage.cost_usd === 'number' && Number.isFinite(usage.cost_usd) && usage.cost_usd >= 0 ? usage.cost_usd : null;
    const costEstimated = usage.cost_estimated === true;
    return {
      date: doc.date,
      day,
      llm: doc.llm === true,
      model: text(doc.model, CAP.model),
      cost,
      costEstimated,
      summary: text(doc.email_summary, CAP.email_summary),
      topics,
    };
  }

  function laneLabel(t) { return t.offLane || !t.lanes.length ? 'Outside your lanes' : t.lanes.join(', '); }
  function signalsLine(t) {
    const s = t.signals;
    const parts = [plural(s.sources, 'source', 'sources')];
    if (s.primary) parts.push('primary source');
    if (s.fresh) parts.push('new since last week');
    parts.push(plural(s.laneHits, 'lane hit', 'lane hits'));
    parts.push('about ' + plural(t.minutes, 'minute', 'minutes'));
    return parts;
  }

  // ---------- building blocks ----------
  // Paragraphs split on blank lines; single line breaks inside one stay (CSS white-space: pre-line).
  function paragraphs(parent, body, emptyText) {
    const parts = body ? body.split(/\n{2,}/).filter((p) => p.trim()) : [];
    if (!parts.length) { parent.appendChild(el('p', 'muted', emptyText)); return; }
    parts.forEach((p) => parent.appendChild(el('p', 'para', p)));
  }
  function extLink(url, label, cls) {
    const a = el('a', cls, label);
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    return a;
  }
  function slide(kind, label) {
    const s = el('section', 'slide slide-' + kind);
    s.setAttribute('aria-roledescription', 'slide');
    s.setAttribute('aria-label', label);
    const inner = el('div', 'slide-inner');
    inner.addEventListener('scroll', updateMore, { passive: true });
    s.appendChild(inner);
    return { s, inner };
  }
  // The header the three slides of a topic share: its number and lane, then its title.
  function topicHead(inner, t, n, total) {
    const eb = el('p', 'eyebrow');
    eb.appendChild(el('span', 'eyebrow-n', 'Topic ' + n + ' of ' + total));
    eb.appendChild(el('span', t.offLane || !t.lanes.length ? 'lane lane-off' : 'lane', laneLabel(t)));
    inner.appendChild(eb);
    inner.appendChild(el('h2', 'topic-title', t.title));
    inner.appendChild(el('span', 'rule'));
  }

  // ---------- slides ----------
  function titleSlide(b) {
    const { s, inner } = slide('title', 'Title');
    inner.appendChild(el('p', 'eyebrow', 'Learning briefing'));
    inner.appendChild(el('h1', 'deck-title', fmtLong.format(b.day)));
    inner.appendChild(el('span', 'rule'));
    if (b.summary) inner.appendChild(el('p', 'lede', b.summary));
    if (b.topics.length) {
      const ol = el('ol', 'toc');
      b.topics.forEach((t, i) => {
        const li = el('li', 'toc-item');
        li.appendChild(el('span', 'toc-n', String(i + 1)));
        const body = el('span', 'toc-body');
        const a = el('a', 'toc-link', t.title);
        a.href = '#slide-' + (2 + i * SLIDES_PER_TOPIC);
        body.appendChild(a);
        body.appendChild(el('span', 'toc-lane', laneLabel(t)));
        li.appendChild(body);
        ol.appendChild(li);
      });
      inner.appendChild(ol);
      const minutes = b.topics.reduce((sum, t) => sum + t.minutes, 0);
      inner.appendChild(el('p', 'foot', plural(b.topics.length, 'topic', 'topics') + ', about ' + plural(minutes, 'minute', 'minutes') + ' of reading'));
    } else {
      inner.appendChild(el('p', 'lede', 'No topics today.'));
      inner.appendChild(el('p', 'foot', 'Nothing new passed the bar today.'));
    }
    return s;
  }

  // One section of a topic's prose on its own slide: what happened, or why it matters.
  function proseSlide(t, n, total, head, body) {
    const { s, inner } = slide('prose', 'Topic ' + n + ': ' + head.toLowerCase());
    topicHead(inner, t, n, total);
    inner.appendChild(el('h3', 'col-head', head));
    const prose = el('div', body.length >= TWO_COLUMNS_FROM ? 'prose prose-2' : 'prose');
    paragraphs(prose, body, 'Not given for this topic.');
    inner.appendChild(prose);
    return s;
  }

  function readSlide(t, n, total) {
    const { s, inner } = slide('read', 'Topic ' + n + ': read first and open question');
    topicHead(inner, t, n, total);
    const cols = el('div', 'cols');
    const left = el('div', 'col');
    left.appendChild(el('h3', 'col-head', 'Read first'));
    if (t.readFirst.length) {
      const ol = el('ol', 'reads');
      t.readFirst.forEach((r) => {
        const li = el('li', 'read');
        li.appendChild(extLink(r.url, r.title, 'read-link'));
        let host = '';
        try { host = new URL(r.url).hostname.replace(/^www\./, ''); } catch (e) { /* kept out above */ }
        const meta = el('p', 'read-meta');
        meta.appendChild(el('span', 'read-source', r.source || host));
        if (r.source && host) meta.appendChild(el('span', 'read-host', host));
        li.appendChild(meta);
        if (r.why) li.appendChild(el('p', 'read-why', r.why));
        ol.appendChild(li);
      });
      left.appendChild(ol);
    } else {
      left.appendChild(el('p', 'muted', 'No links for this topic.'));
    }
    const card = el('aside', 'question');
    card.setAttribute('aria-label', 'Open question');
    card.appendChild(el('h3', 'col-head', 'Open question'));
    card.appendChild(el('p', 'question-text', t.openQuestion || 'None given for this topic.'));
    cols.appendChild(left);
    cols.appendChild(card);
    inner.appendChild(cols);
    const sig = el('ul', 'signals');
    sig.setAttribute('aria-label', 'Why this topic made the list');
    signalsLine(t).forEach((p) => sig.appendChild(el('li', '', p)));
    inner.appendChild(sig);
    return s;
  }

  function closingSlide(b) {
    const { s, inner } = slide('close', 'The end');
    inner.appendChild(el('span', 'rule'));
    inner.appendChild(el('h2', 'deck-title', "That's the briefing"));
    let how = 'Topics are ranked by importance: how many sources covered them, primary sources, what changed since last week and fit with your lanes.';
    if (b.topics.some((t) => t.offLane)) how += ' One topic comes from outside your lanes.';
    inner.appendChild(el('p', 'lede', how));
    let made;
    if (b.llm) {
      made = 'Written by ' + (b.model || 'a model') + ' from the day\'s feeds.';
      if (b.cost !== null) made += ' Cost of this run: ' + (b.costEstimated ? 'about ' : '') + '$' + b.cost.toFixed(2) + '.';
    } else {
      made = 'Built from the feeds without a model today.';
    }
    inner.appendChild(el('p', 'muted', made));
    const actions = el('p', 'actions');
    const home = el('a', 'pill pill-gold', 'Back to the dashboard');
    home.href = '../../';
    actions.appendChild(home);
    const again = el('a', 'pill', 'Start over');
    again.href = '#slide-1';
    actions.appendChild(again);
    const pptx = el('a', 'pill', 'Download .pptx');
    pptx.href = './briefing.pptx';
    pptx.setAttribute('download', '');
    actions.appendChild(pptx);
    inner.appendChild(actions);
    inner.appendChild(el('p', 'foot', fmtLong.format(b.day)));
    return s;
  }

  // ---------- states ----------
  function showStatus(title, detail) {
    if (!$('status-title')) return; // the slides were already built
    document.title = 'Learning briefing';
    $('status-title').textContent = title;
    $('status-text').textContent = detail;
    $('status-actions').hidden = false;
    controls.hidden = true;
    pptxLink.hidden = true;
  }

  function build(b) {
    document.title = 'Learning briefing, ' + fmtShort.format(b.day);
    barDate.textContent = fmtLong.format(b.day);
    pptxLink.hidden = false;
    const total = b.topics.length;
    const list = [titleSlide(b)];
    b.topics.forEach((t, i) => {
      // SLIDES_PER_TOPIC of them; the contents links on the title slide count on that
      list.push(proseSlide(t, i + 1, total, 'What happened', t.whatHappened));
      list.push(proseSlide(t, i + 1, total, 'Why it matters', t.whyItMatters));
      list.push(readSlide(t, i + 1, total));
    });
    list.push(closingSlide(b));
    deck.textContent = '';
    list.forEach((s, i) => {
      s.id = 'slide-' + (i + 1);
      deck.appendChild(s);
    });
    more = el('p', 'more', 'Scroll for more');
    more.setAttribute('aria-hidden', 'true'); // a visual cue; a screen reader reads the whole slide anyway
    more.hidden = true;
    deck.appendChild(more);
    slides = list;
    progress.max = slides.length;
    controls.hidden = false;
    go(fromHash() || 1, { instant: true, replace: true });
  }

  // ---------- fit: no scrolling text where the screen allows ----------
  function overflows(box) { return box.scrollHeight - box.clientHeight > 1; }
  // The current slide's text box when it is taller than the screen, else null.
  function scrollable() {
    const box = slides[current] && slides[current].querySelector('.slide-inner');
    return box && overflows(box) ? box : null;
  }
  // Starts a slide at full size and steps it down until its text fits. Called for the slide on
  // screen only (a hidden slide has no size to measure), each time it is shown or the screen changes.
  function fit(s) {
    const box = s.querySelector('.slide-inner');
    if (!box) return;
    const set = (level) => { for (let i = 1; i <= FIT_STEPS; i += 1) s.classList.toggle('fit-' + i, i === level); };
    let level = 0;
    set(level);
    while (level < FIT_STEPS && overflows(box)) { level += 1; set(level); }
    // Text that is still too long scrolls inside the slide, and the keyboard can reach it there.
    if (overflows(box)) {
      box.setAttribute('tabindex', '0');
    } else if (box.hasAttribute('tabindex')) {
      if (document.activeElement === box) deck.focus({ preventScroll: true });
      box.removeAttribute('tabindex');
    }
  }
  // The cue shows while there is text below the fold of the current slide.
  function updateMore() {
    if (!more) return;
    const box = scrollable();
    const below = !!box && box.scrollTop + box.clientHeight < box.scrollHeight - 4;
    more.hidden = !below;
    deck.classList.toggle('has-more', below);
  }
  let refitQueued = false;
  function refit() {
    if (refitQueued || !slides.length) return;
    refitQueued = true;
    requestAnimationFrame(() => {
      refitQueued = false;
      fit(slides[current]);
      updateMore();
    });
  }
  window.addEventListener('resize', refit);
  // The web fonts set a little differently from the fallbacks the first fit was measured with.
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(refit, () => {});
  if (document.fonts && document.fonts.addEventListener) document.fonts.addEventListener('loadingdone', refit);

  // ---------- navigation ----------
  function fromHash() {
    const m = /^#slide-(\d{1,3})$/.exec(location.hash);
    if (!m) return 0;
    const n = parseInt(m[1], 10);
    return n >= 1 && n <= slides.length ? n : 0;
  }
  function go(n, opts) {
    if (!slides.length) return;
    const o = opts || {};
    const to = Math.min(slides.length, Math.max(1, n)) - 1;
    const back = to < current;
    const active = document.activeElement; // read before the slide it may be in is hidden
    slides.forEach((s, i) => {
      const on = i === to;
      s.classList.toggle('is-current', on);
      s.classList.toggle('from-left', on && back && !o.instant);
      s.classList.toggle('from-right', on && !back && !o.instant && i !== current);
    });
    current = to;
    fit(slides[to]);
    const inner = slides[to].querySelector('.slide-inner');
    if (inner) inner.scrollTop = 0;
    updateMore();
    // Focus on a link in a slide that just went away (a contents link, Start over) goes to the deck.
    if (active && active !== document.body && deck.contains(active) && !slides[to].contains(active)) deck.focus({ preventScroll: true });
    countEl.textContent = (to + 1) + ' / ' + slides.length;
    countLive.textContent = 'Slide ' + (to + 1) + ' of ' + slides.length;
    progress.value = to + 1;
    endButton(prevBtn, to === 0, nextBtn, to === slides.length - 1);
    endButton(nextBtn, to === slides.length - 1, prevBtn, to === 0);
    // The address names the slide (#slide-N), except on the first, so a plain link stays plain.
    const hash = to === 0 ? '' : '#slide-' + (to + 1);
    if (location.hash !== hash) {
      try { history.replaceState(null, '', hash || location.pathname + location.search); } catch (e) { /* a sandboxed frame: the address stays */ }
    }
  }
  function step(d) { go(current + 1 + d); }
  // Previous is off on the first slide and Next on the last. A disabled button drops the
  // keyboard focus to the top of the page, so a focused button first hands it to the other
  // one (or to the deck when that one is off too).
  function endButton(btn, off, other, otherOff) {
    if (off && document.activeElement === btn) {
      if (otherOff) {
        deck.focus({ preventScroll: true });
      } else {
        other.disabled = false;
        other.focus({ preventScroll: true });
      }
    }
    btn.disabled = off;
  }

  prevBtn.addEventListener('click', () => step(-1));
  nextBtn.addEventListener('click', () => step(1));
  // The address changed without go(): a contents link, an edited address, or the browser's Back
  // and Forward buttons. A plain address is the first slide, so Back from #slide-N to it shows
  // slide 1 again. Any other anchor (the skip link's #deck) leaves the slide alone.
  window.addEventListener('hashchange', () => {
    const n = fromHash();
    if (n) go(n);
    else if (location.hash === '' || location.hash === '#') go(1);
  });

  // A slide still taller than the screen scrolls inside itself: space reads down it before moving
  // on, and the up and down arrows scroll it.
  document.addEventListener('keydown', (e) => {
    if (!slides.length || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
    const t = e.target;
    const tag = t && t.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (t && t.isContentEditable)) return;
    const onControl = tag === 'BUTTON' || tag === 'A';
    let d = 0;
    switch (e.key) {
      case 'ArrowRight': case 'PageDown': d = 1; break;
      case 'ArrowLeft': case 'PageUp': d = -1; break;
      case 'Home': e.preventDefault(); go(1); return;
      case 'End': e.preventDefault(); go(slides.length); return;
      case 'ArrowDown': case 'ArrowUp': {
        const box = scrollable();
        if (!box || t === box) return; // nothing to scroll, or the focused box scrolls itself
        e.preventDefault();
        box.scrollBy({ top: (e.key === 'ArrowDown' ? 1 : -1) * box.clientHeight * 0.4, behavior: 'auto' });
        return;
      }
      case ' ': case 'Spacebar': {
        if (onControl) return; // space presses the focused button or link
        const box = scrollable();
        if (box) {
          const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
          const atStart = box.scrollTop <= 4;
          if (!e.shiftKey && !atEnd) { e.preventDefault(); box.scrollBy({ top: box.clientHeight * 0.85, behavior: reduceMotion() ? 'auto' : 'smooth' }); return; }
          if (e.shiftKey && !atStart) { e.preventDefault(); box.scrollBy({ top: -box.clientHeight * 0.85, behavior: reduceMotion() ? 'auto' : 'smooth' }); return; }
        }
        d = e.shiftKey ? -1 : 1;
        break;
      }
      default: return;
    }
    e.preventDefault();
    step(d);
  });

  // Swipe: a mostly horizontal stroke of 50 px or more within a second. Vertical strokes scroll.
  let touch = null;
  deck.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) { touch = null; return; }
    touch = { x: e.touches[0].clientX, y: e.touches[0].clientY, t: Date.now() };
  }, { passive: true });
  deck.addEventListener('touchend', (e) => {
    if (!touch || !e.changedTouches.length) return;
    const dx = e.changedTouches[0].clientX - touch.x;
    const dy = e.changedTouches[0].clientY - touch.y;
    const quick = Date.now() - touch.t < 1000;
    touch = null;
    if (quick && Math.abs(dx) >= 50 && Math.abs(dx) > Math.abs(dy) * 1.5) step(dx < 0 ? 1 : -1);
  }, { passive: true });
  deck.addEventListener('touchcancel', () => { touch = null; }, { passive: true });

  // ---------- load ----------
  fetch('briefing.json', { cache: 'no-cache', credentials: 'same-origin' })
    .then((r) => {
      if (r.status === 404) throw Object.assign(new Error('missing'), { kind: 'missing' });
      if (!r.ok) throw Object.assign(new Error('http'), { kind: 'http' });
      return r.text();
    })
    .then((body) => {
      if (body.length > MAX_BYTES) throw Object.assign(new Error('large'), { kind: 'bad' });
      let doc;
      try { doc = JSON.parse(body); } catch (e) { throw Object.assign(new Error('json'), { kind: 'bad' }); }
      const b = clean(doc);
      if (!b) throw Object.assign(new Error('shape'), { kind: 'bad' });
      build(b);
    })
    .catch((err) => {
      const kind = err && err.kind;
      if (kind === 'missing') showStatus('No briefing here yet', 'There is no briefing for this day. The morning run writes one; the dashboard links to the latest.');
      else if (kind === 'bad') showStatus('This briefing could not be read', 'The briefing file is damaged or in an unexpected format, so nothing from it is shown. The slides may still be in briefing.pptx.');
      else showStatus('Could not load the briefing', 'Check your connection and reload the page.');
    });
})();
