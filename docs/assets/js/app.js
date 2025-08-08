(function(){
  const WORKER_BASE_URL = ''; // optional: e.g., 'https://your-worker.example.workers.dev'

  const listHighlights = document.getElementById('list-highlights');
  const deeperContainer = document.getElementById('deeper-container');
  const listDeeper = document.getElementById('list-deeper');
  const filterSource = document.getElementById('filter-source');
  const filterTheme = document.getElementById('filter-theme');
  const searchInput = document.getElementById('search');
  const filterUnread = document.getElementById('filter-unread');
  const markAllBtn = document.getElementById('mark-all-read');
  const summarizeBtn = document.getElementById('summarize-selected');

  const READ_KEY = 'nd_read_v1';
  const SELECT_KEY = 'nd_select_v1';
  let items = [];
  let idx = null;
  let readSet = loadSet(READ_KEY);
  let selectedSet = loadSet(SELECT_KEY);

  fetch('data/items.json?ts=' + Date.now())
    .then(r => r.json())
    .then(data => {
      items = (data.items || []).map(i => ({...i, _fresh: isFresh(i.published)}));
      populateFilters(items);
      buildIndex(items);
      render();
    });

  function loadSet(key){
    try { return new Set(JSON.parse(localStorage.getItem(key) || '[]')); } catch(e){ return new Set(); }
  }
  function saveSet(key, set){ try { localStorage.setItem(key, JSON.stringify(Array.from(set))); } catch(e){} }

  function populateFilters(items){
    const sources = Array.from(new Set(items.map(i=>i.source))).sort();
    const themes = Array.from(new Set(items.flatMap(i=>i.themes||[]))).sort();
    for (const s of sources){
      const opt = document.createElement('option'); opt.value = s; opt.textContent = s;
      filterSource.appendChild(opt);
    }
    for (const t of themes){
      const opt = document.createElement('option'); opt.value = t; opt.textContent = t;
      filterTheme.appendChild(opt);
    }
  }

  function buildIndex(items){
    idx = lunr(function () {
      this.ref('id');
      this.field('title');
      this.field('summary');
      items.forEach(function (doc) { this.add(doc) }, this);
    });
  }

  function isFresh(publishedIso){
    try {
      const d = new Date(publishedIso);
      const now = new Date();
      return d.getFullYear() === now.getFullYear() &&
             d.getMonth() === now.getMonth() &&
             d.getDate() === now.getDate();
    } catch(e){ return false; }
  }

  function render(){
    const q = searchInput.value.trim();
    let subset = items.slice();

    if (q && idx){
      const ids = new Set(idx.search(q).map(r=>r.ref));
      subset = subset.filter(i=>ids.has(i.id));
    }
    const src = filterSource.value;
    const th = filterTheme.value;
    if (src) subset = subset.filter(i=>i.source === src);
    if (th) subset = subset.filter(i=>(i.themes||[]).includes(th));
    if (filterUnread && filterUnread.checked) subset = subset.filter(i=>!readSet.has(i.id));

    const hi = subset.filter(i=>i.bucket === 'highlight');

    listHighlights.innerHTML = hi.map(renderHighlight).join('') || '<li class="item">No items</li>';
  }

  function renderHighlight(i){
    const dt = new Date(i.published);
    const when = dt.toLocaleString([], {hour:'2-digit', minute:'2-digit'}) + ' · ' + dt.toLocaleDateString();
    const themes = (i.themes||[]).join(', ');
    const isRead = readSet.has(i.id);
    const classes = ['item'];
    if (isRead) classes.push('read'); else classes.push('unread');
    if (!isRead && i._fresh) classes.push('fresh');

    const checked = selectedSet.has(i.id) ? 'checked' : '';
    const freshBadge = (!isRead && i._fresh) ? '<span class="badge fresh" title="new today">NEW</span>' : '';

    return `<li class="${classes.join(' ')}" data-id="${i.id}">
      <div class="row-top" style="display:flex;justify-content:space-between;gap:8px;align-items:center;">
        <div class="select-col">
          <input class="select-box" type="checkbox" data-id="${i.id}" ${checked} aria-label="Select for summary">
          <a href="${i.url}" target="_blank" rel="noopener noreferrer" class="item-link">${escapeHtml(i.title)}</a>
        </div>
        <div class="row-actions" style="display:flex;gap:6px;align-items:center;">
          ${freshBadge}
          <button class="read-toggle btn-ghost" data-id="${i.id}" aria-pressed="${isRead}">${isRead ? 'mark_unread' : 'mark_read'}</button>
        </div>
      </div>
      <div class="meta">${escapeHtml(i.source)} · ${when}${themes ? ' · ' + escapeHtml(themes) : ''}</div>
      ${i.summary ? `<div class="meta">${escapeHtml(i.summary)}</div>` : ''}
    </li>`;
  }

  function escapeHtml(s){
    return (s||'').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  }

  // Event delegation for highlight list
  listHighlights.addEventListener('click', (e) => {
    const toggle = e.target.closest('.read-toggle');
    if (toggle){
      const id = toggle.getAttribute('data-id');
      if (readSet.has(id)) { readSet.delete(id); } else { readSet.add(id); }
      saveSet(READ_KEY, readSet);
      render();
      return;
    }
    const link = e.target.closest('a.item-link');
    if (link){
      const li = e.target.closest('li.item');
      const id = li && li.getAttribute('data-id');
      if (id){ readSet.add(id); saveSet(READ_KEY, readSet); }
    }
    const cb = e.target.closest('.select-box');
    if (cb){
      const id = cb.getAttribute('data-id');
      if (cb.checked) selectedSet.add(id); else selectedSet.delete(id);
      saveSet(SELECT_KEY, selectedSet);
    }
  });

  // Controls
  filterSource.addEventListener('change', render);
  filterTheme.addEventListener('change', render);
  if (filterUnread) filterUnread.addEventListener('change', render);
  searchInput.addEventListener('input', () => { render(); });

  const markAllBtn = document.getElementById('mark-all-read');
  if (markAllBtn){
    markAllBtn.addEventListener('click', () => {
      const q = searchInput.value.trim();
      let subset = items.slice();
      if (q && idx){
        const ids = new Set(idx.search(q).map(r=>r.ref));
        subset = subset.filter(i=>ids.has(i.id));
      }
      const src = filterSource.value;
      const th = filterTheme.value;
      if (src) subset = subset.filter(i=>i.source === src);
      if (th) subset = subset.filter(i=>(i.themes||[]).includes(th));
      subset.filter(i=>i.bucket==='highlight').forEach(i => readSet.add(i.id));
      saveSet(READ_KEY, readSet);
      render();
    });
  }

  if (summarizeBtn){
    summarizeBtn.addEventListener('click', async () => {
      const selected = items.filter(i => selectedSet.has(i.id));
      if (!selected.length){
        deeperContainer.innerHTML = '<p class="meta">No highlights selected. Tick the boxes, then try again.</p>';
        return;
      }
      deeperContainer.innerHTML = '<p class="meta">Summarizing…</p>';
      const sections = [];
      for (const it of selected){
        const text = await fetchFullText(it);
        const summary = summarizeText(text || (it.summary || it.title || ''));
        const bullets = summaryToBullets(summary, 6);
        sections.push(renderDeepSection(it, bullets));
      }
      deeperContainer.innerHTML = sections.join('');
      listDeeper.style.display = 'none';
    });
  }

  function renderDeepSection(it, bullets){
    const dt = new Date(it.published);
    const when = dt.toLocaleDateString();
    const header = `<h4>${escapeHtml(it.title)} <span class="meta">· ${escapeHtml(it.source)} · ${when}</span> — <a href="${it.url}" target="_blank" rel="noopener">open</a></h4>`;
    const list = `<ul>${bullets.map(b=>`<li>${escapeHtml(b)}</li>`).join('')}</ul>`;
    return `<div class="deep-section">${header}${list}</div>`;
  }

  async function fetchFullText(it){
    try {
      if (WORKER_BASE_URL){
        if (isYouTube(it.url)){
          const id = youTubeId(it.url);
          if (id){
            const r = await fetch(`${WORKER_BASE_URL}/yt-transcript?id=${encodeURIComponent(id)}`);
            if (r.ok){ const j = await r.json(); return j.text || ''; }
          }
        }
        const r = await fetch(`${WORKER_BASE_URL}/extract?url=${encodeURIComponent(it.url)}`);
        if (r.ok){ const j = await r.json(); return j.text || ''; }
      }
    } catch(e){}
    return (it.summary || it.title || '');
  }

  function isYouTube(u){ return /youtube\.com|youtu\.be/.test(u); }
  function youTubeId(u){
    try {
      const url = new URL(u);
      if (url.hostname.includes('youtu.be')) return url.pathname.slice(1);
      if (url.searchParams.get('v')) return url.searchParams.get('v');
      const m = url.pathname.match(/\/embed\/([\w-]+)/); if (m) return m[1];
    } catch(e){}
    return null;
  }

  // Minimal extractive summarizer
  const STOP = new Set(("a,an,the,of,in,on,for,to,from,by,with,as,at,that,this,these,those,and,or,not,be,is,are,was,were,been,being,has,have,had,do,does,did,will,would,shall,should,can,could,may,might,must,if,then,else,when,while,about,into,over,after,before,up,down,out,off,again,further,here,there,why,how,all,any,both,each,few,more,most,other,some,such,no,nor,only,own,same,so,than,too,very").split(','));
  function tokenizeSentences(text){
    return (text||'').replace(/\s+/g,' ')
      .split(/(?<=[.!?])\s+(?=[A-Z0-9"'\(])/)
      .filter(s => s && s.trim().length > 20).slice(0, 60);
  }
  function tokenizeWords(s){ return s.toLowerCase().match(/[a-z0-9']+/g) || []; }
  function summarizeText(text, maxSentences=6){
    const sentences = tokenizeSentences(text);
    if (sentences.length <= maxSentences) return sentences;
    const tf = Object.create(null);
    const sWords = sentences.map(tokenizeWords);
    for (const words of sWords){ for (const w of words){ if (!STOP.has(w)) tf[w] = (tf[w]||0)+1; } }
    const scores = sWords.map(words => words.reduce((acc,w)=>acc + (STOP.has(w)?0:(tf[w]||0)), 0));
    const idx = scores.map((s,i)=>[s,i]).sort((a,b)=>b[0]-a[0]).slice(0, maxSentences).map(x=>x[1]).sort((a,b)=>a-b);
    return idx.map(i=>sentences[i]);
  }
  function summaryToBullets(sentences, maxBullets=6){
    return (sentences||[]).slice(0, maxBullets).map(s=>s.trim());
  }
})();