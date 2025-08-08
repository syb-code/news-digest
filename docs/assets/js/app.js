/* News Digest app.js — v5.1 (safe rendering)
   - No nested template literals (avoids copy/paste breakage)
   - Works with the Cloudflare Worker /analyze
*/
(function(){
  const WORKER_BASE_URL = 'news-extract.mikayell9.workers.dev';

  // ---------- DOM ----------
  var listHighlights = document.getElementById('list-highlights');
  var deeperContainer = document.getElementById('deeper-container');
  var listDeeper = document.getElementById('list-deeper');
  var filterSource = document.getElementById('filter-source');
  var filterTheme  = document.getElementById('filter-theme');
  var searchInput  = document.getElementById('search');
  var filterUnread = document.getElementById('filter-unread');
  var markAllBtn   = document.getElementById('mark-all-read');
  var summarizeBtn = document.getElementById('summarize-selected');

  // ---------- STATE ----------
  var READ_KEY   = 'nd_read_v1';
  var SELECT_KEY = 'nd_select_v1';
  var items = [];
  var idx   = null; // lunr index
  var readSet     = loadSet(READ_KEY);
  var selectedSet = loadSet(SELECT_KEY);

  // ---------- INIT ----------
  fetch('data/items.json?ts=' + Date.now())
    .then(function(r){ return r.json(); })
    .then(function(data){
      items = (data.items || []).map(function(i){ i._fresh = isFresh(i.published); return i; });
      populateFilters(items);
      buildIndex(items);
      render();
    })
    .catch(function(){
      listHighlights.innerHTML = '<li class="item">Could not load items.json</li>';
    });

  // ---------- HELPERS ----------
  function loadSet(key){
    try { return new Set(JSON.parse(localStorage.getItem(key) || '[]')); }
    catch(e){ return new Set(); }
  }
  function saveSet(key, set){
    try { localStorage.setItem(key, JSON.stringify(Array.from(set))); }
    catch(e){}
  }
  function populateFilters(items){
    var sources = Array.from(new Set(items.map(function(i){ return i.source; }))).sort();
    var themes  = Array.from(new Set([].concat.apply([], items.map(function(i){ return i.themes || []; })))).sort();
    sources.forEach(function(s){
      var opt = document.createElement('option'); opt.value = s; opt.textContent = s; filterSource.appendChild(opt);
    });
    themes.forEach(function(t){
      var opt = document.createElement('option'); opt.value = t; opt.textContent = t; filterTheme.appendChild(opt);
    });
  }
  function buildIndex(items){
    if (!window.lunr) return;
    idx = lunr(function () {
      this.ref('id');
      this.field('title');
      this.field('summary');
      items.forEach(function (doc) { this.add(doc); }, this);
    });
  }
  function isFresh(publishedIso){
    try {
      var d = new Date(publishedIso);
      var now = new Date();
      return d.getFullYear() === now.getFullYear() &&
             d.getMonth()     === now.getMonth()     &&
             d.getDate()      === now.getDate();
    } catch(e){ return false; }
  }
  function escapeHtml(s){
    return String(s||'').replace(/[&<>"']/g, function(m){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m];
    });
  }

  // ---------- RENDER ----------
  function render(){
    var q   = (searchInput.value || '').trim();
    var subset = items.slice();

    if (q && idx){
      var ids = new Set(idx.search(q).map(function(r){ return r.ref; }));
      subset = subset.filter(function(i){ return ids.has(i.id); });
    }
    var src = filterSource.value;
    var th  = filterTheme.value;
    if (src) subset = subset.filter(function(i){ return i.source === src; });
    if (th)  subset = subset.filter(function(i){ return (i.themes||[]).indexOf(th) >= 0; });
    if (filterUnread && filterUnread.checked) subset = subset.filter(function(i){ return !readSet.has(i.id); });

    var hi = subset.filter(function(i){ return i.bucket === 'highlight'; });
    var html = hi.map(renderHighlight).join('');
    listHighlights.innerHTML = html || '<li class="item">No items</li>';
  }

  function renderHighlight(i){
    var dt    = new Date(i.published);
    var when  = dt.toLocaleString([], {hour:'2-digit', minute:'2-digit'}) + ' · ' + dt.toLocaleDateString();
    var themes = (i.themes || []).join(', ');
    var isRead = readSet.has(i.id);

    var classes = ['item'];
    if (isRead) classes.push('read'); else classes.push('unread');
    if (!isRead && i._fresh) classes.push('fresh');

    var checked    = selectedSet.has(i.id) ? ' checked' : '';
    var freshBadge = (!isRead && i._fresh) ? '<span class="badge fresh" title="new today">NEW</span>' : '';

    var html = '';
    html += '<li class="' + classes.join(' ') + '" data-id="' + i.id + '">';
    html +=   '<div class="row-top" style="display:flex;justify-content:space-between;gap:8px;align-items:center;">';
    html +=     '<div class="select-col">';
    html +=       '<input class="select-box" type="checkbox" data-id="' + i.id + '"' + checked + ' aria-label="Select for analysis">';
    html +=       '<a href="' + i.url + '" target="_blank" rel="noopener noreferrer" class="item-link">' + escapeHtml(i.title) + '</a>';
    html +=     '</div>';
    html +=     '<div class="row-actions" style="display:flex;gap:6px;align-items:center;">';
    html +=       freshBadge;
    html +=       '<button class="read-toggle btn-ghost" data-id="' + i.id + '" aria-pressed="' + isRead + '">' + (isRead ? 'mark_unread' : 'mark_read') + '</button>';
    html +=     '</div>';
    html +=   '</div>';
    html +=   '<div class="meta">' + escapeHtml(i.source) + ' · ' + when + (themes ? ' · ' + escapeHtml(themes) : '') + '</div>';
    if (i.summary){
      html +=   '<div class="meta">' + escapeHtml(i.summary) + '</div>';
    }
    html += '</li>';
    return html;
  }

  // ---------- EVENTS ----------
  listHighlights.addEventListener('click', function(e){
    var toggle = e.target.closest('.read-toggle');
    if (toggle){
      var id = toggle.getAttribute('data-id');
      if (readSet.has(id)) readSet.delete(id); else readSet.add(id);
      saveSet(READ_KEY, readSet);
      render();
      return;
    }
    var link = e.target.closest('a.item-link');
    if (link){
      var li = e.target.closest('li.item');
      var id2 = li && li.getAttribute('data-id');
      if (id2){ readSet.add(id2); saveSet(READ_KEY, readSet); }
      return;
    }
    var cb = e.target.closest('.select-box');
    if (cb){
      var id3 = cb.getAttribute('data-id');
      if (cb.checked) selectedSet.add(id3); else selectedSet.delete(id3);
      saveSet(SELECT_KEY, selectedSet);
      return;
    }
  });

  filterSource.addEventListener('change', render);
  filterTheme .addEventListener('change', render);
  if (filterUnread) filterUnread.addEventListener('change', render);
  searchInput.addEventListener('input', render);

  if (markAllBtn){
    markAllBtn.addEventListener('click', function(){
      var q = (searchInput.value || '').trim();
      var subset = items.slice();
      if (q && idx){
        var ids = new Set(idx.search(q).map(function(r){ return r.ref; }));
        subset = subset.filter(function(i){ return ids.has(i.id); });
      }
      var src = filterSource.value;
      var th  = filterTheme.value;
      if (src) subset = subset.filter(function(i){ return i.source === src; });
      if (th)  subset = subset.filter(function(i){ return (i.themes||[]).indexOf(th) >= 0; });
      subset.filter(function(i){ return i.bucket==='highlight'; }).forEach(function(i){ readSet.add(i.id); });
      saveSet(READ_KEY, readSet);
      render();
    });
  }

  if (summarizeBtn){
    summarizeBtn.addEventListener('click', async function(){
      var selected = items.filter(function(i){ return selectedSet.has(i.id) && i.bucket === 'highlight'; });
      if (!selected.length){
        if (deeperContainer){ deeperContainer.innerHTML = '<p class="meta">No highlights selected. Tick the boxes, then click summarize_selected.</p>'; }
        return;
      }
      if (deeperContainer){ deeperContainer.innerHTML = '<p class="meta">Analyzing…</p>'; }

      var sections = [];
      for (const it of selected){
        var analysis = await analyzeViaWorker(it);
        if (analysis){
          sections.push(renderAnalysisSection(it, analysis));
        } else {
          var text = await fetchFullText(it);
          var summary = summarizeText(text || it.summary || it.title || '', 6);
          var bullets = summaryToBullets(summary, 6);
          sections.push(renderFallbackSection(it, bullets));
        }
      }
      if (deeperContainer){ deeperContainer.innerHTML = sections.join(''); }
      if (listDeeper) listDeeper.style.display = 'none';
    });
  }

  // ---------- Worker helpers ----------
  function isYouTube(u){ return /(?:^|\.)youtube\.com|youtu\.be/.test(u); }
  function youTubeId(u){
    try {
      var url = new URL(u);
      if (url.hostname.indexOf('youtu.be') >= 0) return url.pathname.slice(1);
      if (url.pathname.indexOf('/shorts/') === 0) return url.pathname.split('/')[2];
      if (url.searchParams.get('v')) return url.searchParams.get('v');
      var m = url.pathname.match(/\/embed\/([\w-]+)/); if (m) return m[1];
    } catch(e){}
    return null;
  }

  async function fetchFullText(it){
    try {
      if (WORKER_BASE_URL){
        if (isYouTube(it.url)){
          var id = youTubeId(it.url);
          if (id){
            var r = await fetch(WORKER_BASE_URL + '/yt-transcript?id=' + encodeURIComponent(id));
            if (r.ok){
              var j = await r.json();
              if (j && j.text) return j.text;
            }
          }
        }
        var r2 = await fetch(WORKER_BASE_URL + '/extract?url=' + encodeURIComponent(it.url));
        if (r2.ok){
          var j2 = await r2.json();
          if (j2 && j2.text) return j2.text;
        }
      }
    } catch(e){}
    return (it.summary || it.title || '');
  }

  async function analyzeViaWorker(it){
    if (!WORKER_BASE_URL) return null;
    try {
      var id = isYouTube(it.url) ? youTubeId(it.url) : null;
      var payload = id ? { yt_id: id, title: it.title, url: it.url, source: it.source, published: it.published }
                       : { url: it.url, title: it.title, source: it.source, published: it.published };
      var r = await fetch(WORKER_BASE_URL + '/analyze', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!r.ok) return null;
      var j = await r.json();
      return j.analysis || null;
    } catch(e){
      return null;
    }
  }

  // ---------- Rendering of analysis ----------
  function renderAnalysisSection(it, a){
    var dt = new Date(it.published);
    var when = dt.toLocaleDateString();
    var header = '<h4>' + escapeHtml(it.title) + ' <span class="meta">· ' + escapeHtml(it.source) + ' · ' + when + '</span> — <a href="' + it.url + '" target="_blank" rel="noopener">open</a></h4>';
    var tldr = a.tldr ? '<p><strong>TL;DR:</strong> ' + escapeHtml(a.tldr) + '</p>' : '';
    var keys = Array.isArray(a.key_points) ? ('<h5>Key Points</h5><ul>' + a.key_points.map(function(s){ return '<li>' + escapeHtml(s) + '</li>'; }).join('') + '</ul>') : '';
    var pros = Array.isArray(a.pros) ? ('<h5>Upsides</h5><ul>' + a.pros.map(function(s){ return '<li>' + escapeHtml(s) + '</li>'; }).join('') + '</ul>') : '';
    var cons = Array.isArray(a.cons) ? ('<h5>Risks / Caveats</h5><ul>' + a.cons.map(function(s){ return '<li>' + escapeHtml(s) + '</li>'; }).join('') + '</ul>') : '';
    var quotes = Array.isArray(a.notable_quotes) ? ('<h5>Notable Quotes</h5><ul>' + a.notable_quotes.map(function(q){ return '<li>“' + escapeHtml(q.quote || '') + '” ' + (q.at ? '<span class="meta">(' + escapeHtml(q.at) + ')</span>' : '') + '</li>'; }).join('') + '</ul>') : '';
    var actions = Array.isArray(a.actions) ? ('<h5>Actions</h5><ul>' + a.actions.map(function(s){ return '<li>' + escapeHtml(s) + '</li>'; }).join('') + '</ul>') : '';

    return '<div class="deep-section">' + header + tldr + keys + pros + cons + quotes + actions + '</div>';
  }

  function renderFallbackSection(it, bullets){
    var dt = new Date(it.published);
    var when = dt.toLocaleDateString();
    var header = '<h4>' + escapeHtml(it.title) + ' <span class="meta">· ' + escapeHtml(it.source) + ' · ' + when + '</span> — <a href="' + it.url + '" target="_blank" rel="noopener">open</a></h4>';
    var list = '<ul>' + bullets.map(function(b){ return '<li>' + escapeHtml(b) + '</li>'; }).join('') + '</ul>';
    return '<div class="deep-section">' + header + '<p class="meta">Fallback summary</p>' + list + '</div>';
  }

  // ---------- Tiny extractive summarizer ----------
  var STOP = new Set(("a,an,the,of,in,on,for,to,from,by,with,as,at,that,this,these,those,and,or,not,be,is,are,was,were,been,being,has,have,had,do,does,did,will,would,shall,should,can,could,may,might,must,if,then,else,when,while,about,into,over,after,before,up,down,out,off,again,further,here,there,why,how,all,any,both,each,few,more,most,other,some,such,no,nor,only,own,same,so,than,too,very").split(','));

  function tokenizeSentences(text){
    return String(text||'')
      .replace(/\s+/g,' ')
      .split(/(?<=[.!?])\s+(?=[A-Z0-9"'(])/)
      .filter(function(s){ return s && s.trim().length > 20; })
      .slice(0, 60);
  }
  function tokenizeWords(s){
    return s.toLowerCase().match(/[a-z0-9']+/g) || [];
  }
  function summarizeText(text, maxSentences){
    maxSentences = maxSentences || 6;
    var sentences = tokenizeSentences(text);
    if (sentences.length <= maxSentences) return sentences;
    var tf = Object.create(null);
    var sWords = sentences.map(tokenizeWords);
    sWords.forEach(function(words){
      words.forEach(function(w){ if (!STOP.has(w)) tf[w] = (tf[w]||0)+1; });
    });
    var scores = sWords.map(function(words){
      return words.reduce(function(acc,w){ return acc + (STOP.has(w)?0:(tf[w]||0)); }, 0);
    });
    var idxs = scores.map(function(s,i){ return [s,i]; }).sort(function(a,b){ return b[0]-a[0]; })
                     .slice(0, maxSentences).map(function(x){ return x[1]; }).sort(function(a,b){ return a-b; });
    return idxs.map(function(i){ return sentences[i]; });
  }
  function summaryToBullets(sentences, maxBullets){
    maxBullets = maxBullets || 6;
    return (sentences||[]).slice(0, maxBullets).map(function(s){ return s.trim(); });
  }
})();
