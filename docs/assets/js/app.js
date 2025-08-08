(function(){
  const listHighlights = document.getElementById('list-highlights');
  const listDeeper = document.getElementById('list-deeper');
  const filterSource = document.getElementById('filter-source');
  const filterTheme = document.getElementById('filter-theme');
  const searchInput = document.getElementById('search');
  const filterUnread = document.getElementById('filter-unread');
  const markAllBtn = document.getElementById('mark-all-read');

  const READ_KEY = 'nd_read_v1';
  let items = [];
  let idx = null;
  let readSet = loadReadSet();

  fetch('data/items.json?ts=' + Date.now())
    .then(r => r.json())
    .then(data => {
      items = (data.items || []).map(i => ({...i, _fresh: isFresh(i.published)}));
      populateFilters(items);
      buildIndex(items);
      render();
    });

  function loadReadSet(){
    try {
      const raw = localStorage.getItem(READ_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return new Set(arr);
    } catch(e){ return new Set(); }
  }
  function saveReadSet(){
    try { localStorage.setItem(READ_KEY, JSON.stringify(Array.from(readSet))); } catch(e){}
  }
  function markRead(id){
    readSet.add(id); saveReadSet();
  }
  function markUnread(id){
    readSet.delete(id); saveReadSet();
  }

  function populateFilters(items){
    const sources = Array.from(new Set(items.map(i=>i.source))).sort();
    const themes = Array.from(new Set(items.flatMap(i=>i.themes||[]))).sort();
    for (const s of sources){
      const opt = document.createElement('option');
      opt.value = s; opt.textContent = s;
      filterSource.appendChild(opt);
    }
    for (const t of themes){
      const opt = document.createElement('option');
      opt.value = t; opt.textContent = t;
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
    const dd = subset.filter(i=>i.bucket === 'deeper');

    listHighlights.innerHTML = hi.map(renderItem).join('') || '<li class="item">No items</li>';
    listDeeper.innerHTML = dd.map(renderItem).join('') || '<li class="item">No items</li>';
  }

  function renderItem(i){
    const dt = new Date(i.published);
    const when = dt.toLocaleString([], {hour:'2-digit', minute:'2-digit'}) + ' · ' + dt.toLocaleDateString();
    const themes = (i.themes||[]).join(', ');
    const isRead = readSet.has(i.id);
    const classes = ['item'];
    if (isRead) classes.push('read'); else classes.push('unread');
    if (!isRead && i._fresh) classes.push('fresh');

    const freshBadge = (!isRead && i._fresh) ? '<span class="badge fresh" title="new today">NEW</span>' : '';

    return `<li class="${classes.join(' ')}" data-id="${i.id}">
      <div class="row-top" style="display:flex;justify-content:space-between;gap:8px;align-items:center;">
        <a href="${i.url}" target="_blank" rel="noopener noreferrer" class="item-link">${escapeHtml(i.title)}</a>
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

  // Event delegation for link clicks & toggle buttons
  function onListClick(e){
    const toggle = e.target.closest('.read-toggle');
    if (toggle){
      const id = toggle.getAttribute('data-id');
      if (readSet.has(id)) markUnread(id); else markRead(id);
      render();
      return;
    }
    const link = e.target.closest('a.item-link');
    if (link){
      const li = e.target.closest('li.item');
      const id = li && li.getAttribute('data-id');
      if (id){ markRead(id); }
      // let the navigation happen
    }
  }
  listHighlights.addEventListener('click', onListClick);
  listDeeper.addEventListener('click', onListClick);

  // Controls
  filterSource.addEventListener('change', render);
  filterTheme.addEventListener('change', render);
  if (filterUnread) filterUnread.addEventListener('change', render);
  searchInput.addEventListener('input', () => { render(); });

  if (markAllBtn){
    markAllBtn.addEventListener('click', () => {
      // mark all currently visible items as read
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
      subset.forEach(i => readSet.add(i.id));
      saveReadSet();
      render();
    });
  }
})();