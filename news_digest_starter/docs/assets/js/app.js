(function(){
  const listHighlights = document.getElementById('list-highlights');
  const listDeeper = document.getElementById('list-deeper');
  const filterSource = document.getElementById('filter-source');
  const filterTheme = document.getElementById('filter-theme');
  const searchInput = document.getElementById('search');

  let items = [];
  let idx = null;

  fetch('data/items.json?ts=' + Date.now())
    .then(r => r.json())
    .then(data => {
      items = data.items || [];
      populateFilters(items);
      buildIndex(items);
      render();
    });

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

    const hi = subset.filter(i=>i.bucket === 'highlight');
    const dd = subset.filter(i=>i.bucket === 'deeper');

    listHighlights.innerHTML = hi.map(renderItem).join('') || '<li class="item">No items</li>';
    listDeeper.innerHTML = dd.map(renderItem).join('') || '<li class="item">No items</li>';
  }

  function renderItem(i){
    const dt = new Date(i.published);
    const when = dt.toLocaleString([], {hour:'2-digit', minute:'2-digit'}) + ' · ' + dt.toLocaleDateString();
    const themes = (i.themes||[]).join(', ');
    return `<li class="item">
      <a href="${i.url}" target="_blank" rel="noopener noreferrer">${escapeHtml(i.title)}</a>
      <div class="meta">${escapeHtml(i.source)} · ${when}${themes ? ' · ' + escapeHtml(themes) : ''}</div>
      ${i.summary ? `<div class="meta">${escapeHtml(i.summary)}</div>` : ''}
    </li>`;
  }

  function escapeHtml(s){
    return (s||'').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  }

  filterSource.addEventListener('change', render);
  filterTheme.addEventListener('change', render);
  searchInput.addEventListener('input', () => { render(); });
})();