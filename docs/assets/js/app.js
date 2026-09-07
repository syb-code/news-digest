/* News Digest app.js — v5.4 (extra safe)
   - No template literals
   - No regex lookbehind
   - No object-literal keys with quotes
*/
(function(){
  // IMPORTANT: include the protocol (https://)
  var WORKER_BASE_URL = "https://news-extract.mikayell9.workers.dev";
  // Cloudflare Turnstile sitekey (PUBLIC, safe to commit). Leave "" to disable:
  // with an empty key nothing changes — no extra script, no extra headers.
  // With a key set, every Worker call sends a fresh single-use token in the
  // X-Turnstile-Token header, which the Worker verifies before doing any work.
  var TURNSTILE_SITE_KEY = "";
  // Owner key (the primary lock — this site is public but only its owner summarizes).
  // Set to true FIRST, before "wrangler secret put OWNER_KEY" (ROLLOUT.md Step 6): the Worker
  // ignores the X-Owner-Key header until the secret exists, so the site keeps working while the
  // new file propagates. The key itself is NEVER written in this file: the browser asks for it
  // once and remembers it (localStorage). While false nothing changes — no prompt, no storage
  // access, no extra header, and a "#key=" URL fragment is left alone.
  var OWNER_KEY_ENABLED = false;
  var OWNER_KEY_STORAGE = "nd_owner_key_v1";
  var ownerKeyCache = "";         // the key for this page load (so a failing localStorage write cannot re-prompt per call)
  var ownerKeyRejected = false;   // set when the Worker answered 401 during the current summarize run
  var ownerKeyDeclined = false;   // set when the prompt was cancelled during the current summarize run
  console.log("news-digest app v5.4 loaded");

  // ---------- DOM ----------
 var listHighlights = document.getElementById("list-highlights");
  var deeperContainer = document.getElementById("deeper-container");
  var listDeeper = document.getElementById("list-deeper");
  var filterSource = document.getElementById("filter-source");
  var filterTheme  = document.getElementById("filter-theme");
  var searchInput  = document.getElementById("search");
  var filterUnread = document.getElementById("filter-unread");
  var markAllBtn   = document.getElementById("mark-all-read");
  var summarizeBtn = document.getElementById("summarize-selected");
  var listTwitter = document.getElementById("list-twitter");

  var READ_KEY   = "nd_read_v1";
  var SELECT_KEY = "nd_select_v1";
  var items = [];
  var idx   = null;
  var readSet     = loadSet(READ_KEY);
  var selectedSet = loadSet(SELECT_KEY);

  fetch("data/items.json?ts=" + Date.now())
    .then(function(r){ return r.json(); })
    .then(function(data){
      items = (data.items || []).map(function(i){ i._fresh = isFresh(i.published); return i; });
      populateFilters(items);
      buildIndex(items);
      render();
    })
    .catch(function(){ listHighlights.innerHTML = '<li class="item">Could not load items.json</li>'; });


  fetch("data/twitter_posts.json?ts=" + Date.now())
    .then(function(r){ return r.json(); })
    .then(function(data){ renderTwitter(data.accounts || []); })
    .catch(function(){
      if(listTwitter){ listTwitter.innerHTML = '<li class="item meta">Could not load twitter posts.</li>'; }
    });

  function loadSet(key){ try { return new Set(JSON.parse(localStorage.getItem(key) || "[]")); } catch(e){ return new Set(); } }
  function saveSet(key, set){ try { localStorage.setItem(key, JSON.stringify(Array.from(set))); } catch(e){} }

  function populateFilters(items){
    var sources = Array.from(new Set(items.map(function(i){ return i.source; }))).sort();
    var themes  = Array.from(new Set([].concat.apply([], items.map(function(i){ return i.themes || []; })))).sort();
    sources.forEach(function(s){ var o=document.createElement("option"); o.value=s; o.textContent=s; filterSource.appendChild(o); });
    themes.forEach(function(t){ var o=document.createElement("option"); o.value=t; o.textContent=t; filterTheme.appendChild(o); });
  }
  function buildIndex(items){
    if (!window.lunr) return;
    idx = lunr(function(){ this.ref("id"); this.field("title"); this.field("summary"); items.forEach(function(doc){ this.add(doc); }, this); });
  }
  function isFresh(p){ try{ var d=new Date(p), n=new Date(); return d.getFullYear()===n.getFullYear()&&d.getMonth()===n.getMonth()&&d.getDate()===n.getDate(); }catch(e){return false;} }
  function esc(s){ s=String(s||""); return s.replace(/[&<>\"']/g,function(ch){ if(ch==="&")return"&amp;"; if(ch==="<")return"&lt;"; if(ch===">")return"&gt;"; if(ch==="\"")return"&quot;"; return"&#39;";}); }



  function renderTwitter(accounts){
    if(!listTwitter) return;
    if(!accounts.length){
      listTwitter.innerHTML = '<li class="item meta">No twitter posts available.</li>';
      return;
    }

    var rows = [];
    accounts.forEach(function(acct){
      var posts = acct.posts || [];
      if(!posts.length){
        rows.push('<li class="item"><div><strong>@'+esc(acct.handle)+'</strong></div><div class="meta"><a href="'+esc(acct.url)+'" target="_blank" rel="noopener noreferrer">open profile</a></div></li>');
        return;
      }

      posts.slice(0, 2).forEach(function(post){
        var dt = new Date(post.published);
        var when = dt.toLocaleString([], {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"});
        rows.push('<li class="item"><div class="meta">@'+esc(acct.handle)+' · '+when+'</div><a class="item-link" href="'+esc(post.url)+'" target="_blank" rel="noopener noreferrer">'+esc(post.text)+'</a></li>');
      });
    });

    listTwitter.innerHTML = rows.join("") || '<li class="item meta">No twitter posts available.</li>';
  }

  function render(){
    var q=(searchInput.value||"").trim(), subset=items.slice();
    if(q&&idx){ var ids=new Set(idx.search(q).map(function(r){return r.ref;})); subset=subset.filter(function(i){return ids.has(i.id);});}
    var src=filterSource.value, th=filterTheme.value;
    if(src) subset=subset.filter(function(i){return i.source===src;});
    if(th)  subset=subset.filter(function(i){return (i.themes||[]).indexOf(th)>=0;});
    if(filterUnread&&filterUnread.checked) subset=subset.filter(function(i){return !readSet.has(i.id);});
    var hi=subset.filter(function(i){return i.bucket==="highlight";});
    listHighlights.innerHTML = hi.map(renderHighlight).join("") || '<li class="item">No items</li>';
  }

  function renderHighlight(i){
    var dt=new Date(i.published);
    var when=dt.toLocaleString([], {hour:"2-digit", minute:"2-digit"})+" · "+dt.toLocaleDateString();
    var themes=(i.themes||[]).join(", ");
    var isRead=readSet.has(i.id);
    var classes=["item"]; if(isRead)classes.push("read"); else classes.push("unread"); if(!isRead&&i._fresh)classes.push("fresh");
    var checked = selectedSet.has(i.id) ? " checked":"";
    var fresh = (!isRead&&i._fresh)?'<span class="badge fresh" title="new today">NEW</span>':"";
    var h=''; h+='<li class="'+classes.join(" ")+'" data-id="'+i.id+'">';
    h+='<div class="row-top" style="display:flex;justify-content:space-between;gap:8px;align-items:center;">';
    h+='<div class="select-col">';
    h+='<input class="select-box" type="checkbox" data-id="'+i.id+'"'+checked+' aria-label="Select for analysis">';
    h+='<a href="'+i.url+'" target="_blank" rel="noopener noreferrer" class="item-link">'+esc(i.title)+'</a></div>';
    h+='<div class="row-actions" style="display:flex;gap:6px;align-items:center;">'+fresh;
    h+='<button class="read-toggle btn-ghost" data-id="'+i.id+'" aria-pressed="'+isRead+'">'+(isRead?"mark_unread":"mark_read")+'</button>';
    h+='</div></div>';
    h+='<div class="meta">'+esc(i.source)+' · '+when+(themes?(' · '+esc(themes)):"")+'</div>';
    if(i.summary){ h+='<div class="meta">'+esc(i.summary)+'</div>'; }
    h+='</li>';
    return h;
  }

  listHighlights.addEventListener("click", function(e){
    var t=e.target.closest(".read-toggle"); if(t){ var id=t.getAttribute("data-id"); if(readSet.has(id))readSet.delete(id); else readSet.add(id); saveSet(READ_KEY,readSet); render(); return; }
    var a=e.target.closest("a.item-link"); if(a){ var li=e.target.closest("li.item"); var id2=li&&li.getAttribute("data-id"); if(id2){readSet.add(id2); saveSet(READ_KEY,readSet);} return; }
    var cb=e.target.closest(".select-box"); if(cb){ var id3=cb.getAttribute("data-id"); if(cb.checked)selectedSet.add(id3); else selectedSet.delete(id3); saveSet(SELECT_KEY,selectedSet); return; }
  });
  filterSource.addEventListener("change",render);
  filterTheme .addEventListener("change",render);
  if(filterUnread) filterUnread.addEventListener("change",render);
  searchInput.addEventListener("input",render);

  if(markAllBtn){
    markAllBtn.addEventListener("click", function(){
      var q=(searchInput.value||"").trim(), subset=items.slice();
      if(q&&idx){ var ids=new Set(idx.search(q).map(function(r){return r.ref;})); subset=subset.filter(function(i){return ids.has(i.id);});}
      var src=filterSource.value, th=filterTheme.value;
      if(src) subset=subset.filter(function(i){return i.source===src;});
      if(th)  subset=subset.filter(function(i){return (i.themes||[]).indexOf(th)>=0;});
      subset.filter(function(i){return i.bucket==="highlight";}).forEach(function(i){readSet.add(i.id);});
      saveSet(READ_KEY,readSet); render();
    });
  }

  if(summarizeBtn){
    summarizeBtn.addEventListener("click", async function(){
      var selected=items.filter(function(i){return selectedSet.has(i.id)&&i.bucket==="highlight";});
      if(!selected.length){ if(deeperContainer){ deeperContainer.innerHTML='<p class="meta">No highlights selected. Tick the boxes, then click summarize_selected.</p>'; } return; }
      if(deeperContainer){ deeperContainer.innerHTML='<p class="meta">Analyzing…</p>'; }
      ownerKeyRejected=false; ownerKeyDeclined=false;
      var sections=[];
      for(var k=0;k<selected.length;k++){
        var it=selected[k];
        var analysis=await analyzeViaWorker(it);
        if(analysis){ sections.push(renderAnalysisSection(it,analysis)); }
        else {
          var text=await fetchFullText(it);
          var summary=summarizeText(text||it.summary||it.title||"",6);
          var bullets=summaryToBullets(summary,6);
          sections.push(renderFallbackSection(it,bullets));
        }
      }
      // The notice is part of the same write as the sections (and the flags are only reset at the
      // START of a run), so an overlapping run (double-click) that finishes later still shows it.
      if(ownerKeyRejected){ sections.unshift('<p class="meta">Owner key rejected — click summarize_selected again to enter it.</p>'); }
      if(deeperContainer){ deeperContainer.innerHTML=sections.join(""); }
      if(listDeeper) listDeeper.style.display="none";
    });
  }

  // ---------- Turnstile (only active when TURNSTILE_SITE_KEY is non-empty) ----------
  var turnstileScriptPromise = null;   // loads api.js once
  var turnstileScriptEl = null;        // the <script> tag of the in-flight load (so a stalled one can be abandoned)
  var turnstileWidgetId = null;        // one invisible widget, rendered once
  var turnstileQueue = Promise.resolve(); // serializes token requests (one execute in flight)
  var turnstilePending = null;         // {resolve, reject, timer} for the in-flight execute
  var turnstileFailedAt = 0;           // circuit breaker: time of the last token failure (0 = none)
  var TURNSTILE_TIMEOUT_MS = 15000;    // max wait for one token — covers script load + widget render + execute
  var TURNSTILE_BACKOFF_MS = 60000;    // after a failure, skip Turnstile (fall back locally) for this long

  function loadTurnstileScript(){
    if(turnstileScriptPromise) return turnstileScriptPromise;
    turnstileScriptPromise = new Promise(function(resolve, reject){
      if(window.turnstile && typeof window.turnstile.render === "function"){ resolve(); return; }
      var s = document.createElement("script");
      turnstileScriptEl = s;
      // The script is loaded async, so we rely on the documented onload=<global> callback
      // (turnstile.ready() is only reliable for synchronously loaded scripts).
      window.ndTurnstileLoaded = function(){ resolve(); };
      s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=ndTurnstileLoaded";
      s.async = true;
      s.defer = true;
      s.onload = function(){ if(window.turnstile && typeof window.turnstile.render === "function") resolve(); };
      s.onerror = function(){
        abandonTurnstileScript(s);                       // don't leave a dead tag behind; a later attempt may retry
        reject(new Error("turnstile script failed to load"));
      };
      (document.head || document.body).appendChild(s);
    });
    return turnstileScriptPromise;
  }

  // Forget an in-flight api.js load (failed, or stalled past the deadline) so that the next
  // attempt after the back-off injects a fresh tag instead of awaiting the dead promise forever.
  function abandonTurnstileScript(s){
    if(!s) s = turnstileScriptEl;                      // null = "whatever is in flight now"
    if(s && s.parentNode) s.parentNode.removeChild(s);
    if(s === turnstileScriptEl){ turnstileScriptEl = null; turnstileScriptPromise = null; }
  }

  function settleTurnstile(err, token){
    var p = turnstilePending;
    if(!p) return;
    turnstilePending = null;
    clearTimeout(p.timer);
    if(err) p.reject(err); else p.resolve(token);
  }

  function ensureTurnstileWidget(){
    return loadTurnstileScript().then(function(){
      return new Promise(function(resolve, reject){
        if(turnstileWidgetId !== null){ resolve(turnstileWidgetId); return; }
        var ts = window.turnstile;
        if(!ts){ reject(new Error("turnstile unavailable")); return; }
        var container = document.createElement("div");
        container.id = "turnstile-container";
        container.setAttribute("aria-hidden", "true");
        // Keep it out of the layout but NOT display:none (that can stop the widget from loading).
        container.style.cssText = "position:fixed;left:0;bottom:0;width:0;height:0;overflow:hidden;";
        document.body.appendChild(container);
        var opts = {
          sitekey: TURNSTILE_SITE_KEY,
          execution: "execute",   // do nothing until turnstile.execute() is called
          size: "invisible",
          action: "digest",
          callback: function(token){ settleTurnstile(null, token); }
        };
        // Hyphenated option names required by the Turnstile API (bracket assignment keeps our no-quoted-keys rule).
        opts["error-callback"] = function(code){ settleTurnstile(new Error("turnstile error " + code)); };
        opts["timeout-callback"] = function(){ settleTurnstile(new Error("turnstile timeout")); };
        opts["expired-callback"] = function(){ /* we always reset before execute, nothing to do */ };
        // The script has executed (we are past its onload callback) and the page is long past
        // DOMContentLoaded, so render directly instead of via turnstile.ready().
        try {
          var id = ts.render(container, opts);
          if(!id){ reject(new Error("turnstile render failed")); return; }
          turnstileWidgetId = id;
          resolve(id);
        } catch(e){ reject(e); }
      });
    });
  }

  function executeTurnstile(id){
    return new Promise(function(resolve, reject){
      // Never let two executes overlap: settle any leftover pending (and clear its timer) before
      // installing ours, so no stale timer can ever reject the request that follows it.
      if(turnstilePending) settleTurnstile(new Error("turnstile execute superseded"));
      var timer = setTimeout(function(){ settleTurnstile(new Error("turnstile token timeout")); }, TURNSTILE_TIMEOUT_MS);
      turnstilePending = { resolve: resolve, reject: reject, timer: timer };
      try {
        var ts = window.turnstile;
        ts.reset(id);    // clear any spent token, then run a fresh challenge
        ts.execute(id);
      } catch(e){ settleTurnstile(e); }
    });
  }

  // One deadline over the WHOLE acquisition (script load + widget render + execute), not just
  // the execute phase: a black-holed api.js request fires neither onload nor onerror for a very
  // long time, and without this the first summarize would hang on "Analyzing…" for all items.
  function acquireTurnstileToken(){
    return new Promise(function(resolve, reject){
      var done = false;
      var deadline = setTimeout(function(){
        if(done) return; done = true;
        if(turnstileWidgetId === null) abandonTurnstileScript(null);   // still loading: retry with a fresh tag later
        settleTurnstile(new Error("turnstile not ready in time"));     // cancels an in-flight execute, if any
        reject(new Error("turnstile not ready in time"));
      }, TURNSTILE_TIMEOUT_MS);
      // A <script> removed after its fetch started still executes when the bytes finally arrive,
      // so an abandoned load can resume this chain later. Once the deadline has fired, stop here:
      // never run a stray execute() with no caller waiting (its timer would reject the next request).
      ensureTurnstileWidget().then(function(id){
        if(done) throw new Error("turnstile acquisition abandoned");
        return executeTurnstile(id);
      }).then(
        function(token){ if(done) return; done = true; clearTimeout(deadline); resolve(token); },
        function(err){ if(done) return; done = true; clearTimeout(deadline); reject(err); }
      );
    });
  }

  // Resolves with a FRESH single-use token. Concurrent callers are queued so only one
  // execute() runs at a time (a reset() would otherwise cancel a pending one).
  // Circuit breaker: after any failure (script blocked or stalled, challenge error, timeout)
  // every call for the next TURNSTILE_BACKOFF_MS rejects immediately, so a batch of items falls
  // back to the local summary at once instead of waiting one timeout per Worker call.
  function getTurnstileToken(){
    var run = function(){
      if(turnstileFailedAt && (Date.now() - turnstileFailedAt) < TURNSTILE_BACKOFF_MS){
        return Promise.reject(new Error("turnstile recently failed; backing off"));
      }
      return acquireTurnstileToken().then(
        function(token){ turnstileFailedAt = 0; return token; },
        function(err){ turnstileFailedAt = Date.now(); throw err; }
      );
    };
    var p = turnstileQueue.then(run, run);
    turnstileQueue = p.then(function(){}, function(){}); // keep the chain alive after a failure
    return p;
  }

  // ---------- Owner key (only active when OWNER_KEY_ENABLED is true) ----------
  function storeOwnerKey(key){ try { localStorage.setItem(OWNER_KEY_STORAGE, key); } catch(e){} }
  function forgetOwnerKey(){ ownerKeyCache = ""; try { localStorage.removeItem(OWNER_KEY_STORAGE); } catch(e){} }
  // HTTP header values are byte strings: fetch() throws a TypeError for any character above U+00FF
  // and refuses control characters, so such a key could never reach the Worker (it would fail
  // silently, forever, with no 401 to clear it). Real keys are base64 / base64url; accept printable ASCII.
  function isSendableOwnerKey(key){ return !/[^\x20-\x7e]/.test(key); }

  // One-time "#key=..." URL fragment: decoded, trimmed, validated, stored (localStorage + the
  // in-memory copy) and stripped from the address bar right away. Runs once at page load (see the
  // end of this file) so the key does not sit in the address bar / tab sync / session restore until
  // the first click, and again on every summarize call (a #key= link pasted into an already open
  // tab changes the fragment without reloading). The fragment never leaves the browser, but it does
  // land in browser history — prefer the prompt on shared machines. Returns the key or "".
  function consumeKeyFragment(){
    var hash = String(location.hash || "");
    if(hash.indexOf("#key=") !== 0) return "";
    var raw = hash.slice(5), key;
    try { key = decodeURIComponent(raw); } catch(e){ key = raw; }
    key = key.trim();
    try { history.replaceState(null, "", location.pathname + location.search); }
    catch(e2){ location.hash = ""; }
    if(key && !isSendableOwnerKey(key)){
      window.alert("Owner key not usable: it must be plain ASCII (letters, digits, - _ = + /). Nothing was saved — click summarize_selected again to enter it.");
      return "";
    }
    if(key){ storeOwnerKey(key); ownerKeyCache = key; }
    return key;
  }

  // Returns the owner key or throws Error("no owner key") — every caller already falls back to
  // the local summarizer on a throw. Sources, in order:
  //   (a) a "#key=..." URL fragment (consumeKeyFragment — normally already consumed at page load);
  //   (b) the in-memory copy from earlier in this page load;
  //   (c) localStorage (remembered from an earlier visit);
  //   (d) a prompt, asked once per page load when storage works and at most once per summarize
  //       run when it does not (a cancelled prompt is not repeated for the other items of the
  //       same run — they just use the local summary).
  // After the Worker answered 401 (see noteWorkerResponse) the stored key is gone and nothing is
  // asked again until the next click on summarize_selected.
  function getOwnerKey(){
    if(ownerKeyRejected || ownerKeyDeclined) throw new Error("no owner key");
    var key = consumeKeyFragment();
    var remembered = !!key;   // came from (a)/(b)/(c): already stored, nothing to write
    if(!key && ownerKeyCache){ key = ownerKeyCache; remembered = true; }
    if(!key){
      try { key = String(localStorage.getItem(OWNER_KEY_STORAGE) || "").trim(); } catch(e3){ key = ""; }
      remembered = !!key;
    }
    if(!key){
      var typed = window.prompt("Enter your news-digest owner key");
      key = typed ? String(typed).trim() : "";
    }
    if(key && !isSendableOwnerKey(key)){
      if(remembered) forgetOwnerKey();
      window.alert("Owner key not usable: it must be plain ASCII (letters, digits, - _ = + /). Nothing was saved — click summarize_selected again to enter it.");
      key = "";
    }
    if(!key){ ownerKeyDeclined = true; throw new Error("no owner key"); }
    if(!remembered) storeOwnerKey(key);
    ownerKeyCache = key;
    return key;
  }

  // Every Worker response passes through here: a 401 that carries "WWW-Authenticate: X-Owner-Key"
  // comes from the guard and means it rejected (or now requires) the owner key, so forget the
  // stored one and tell the user once the run is over. A 401 without that marker is the Worker's
  // handler relaying an upstream site's answer (a paywalled article) — the key was not checked,
  // so it is kept. (guard.js exposes the header via Access-Control-Expose-Headers.)
  function noteWorkerResponse(r){
    if(OWNER_KEY_ENABLED && r && r.status === 401 && isOwnerKey401(r)){ forgetOwnerKey(); ownerKeyRejected = true; }
    return r;
  }
  function isOwnerKey401(r){
    var v = "";
    try { v = String((r.headers && r.headers.get("WWW-Authenticate")) || ""); } catch(e){ v = ""; }
    return /(^|[\s,])X-Owner-Key([\s,]|$)/i.test(v);
  }

  // Extra headers for every Worker call: {} when both locks are off (so requests are unchanged);
  // otherwise X-Owner-Key and/or X-Turnstile-Token (both when both are enabled). The owner key
  // comes first because it is cheap and a missing key must not burn a Turnstile token. Throws if
  // the key / token could not be obtained.
  async function workerAuthHeaders(){
    var h = {};
    if(OWNER_KEY_ENABLED) h["X-Owner-Key"] = getOwnerKey();
    if(!TURNSTILE_SITE_KEY) return h;
    var token = await getTurnstileToken();
    h["X-Turnstile-Token"] = token;
    return h;
  }

  // fetch() wrapper for Worker GETs: byte-for-byte the old plain fetch(url) when both locks are off.
  async function workerGet(url){
    if(!TURNSTILE_SITE_KEY && !OWNER_KEY_ENABLED) return fetch(url);
    var r = await fetch(url, { headers: await workerAuthHeaders() });
    return noteWorkerResponse(r);
  }

  function isYouTube(u){ return /(?:^|\.)youtube\.com|youtu\.be/.test(u); }
  function youTubeId(u){
    try{ var url=new URL(u); if(url.hostname.indexOf("youtu.be")>=0)return url.pathname.slice(1);
      if(url.pathname.indexOf("/shorts/")===0)return url.pathname.split("/")[2];
      if(url.searchParams.get("v"))return url.searchParams.get("v");
      var m=url.pathname.match(/\/embed\/([\w-]+)/); if(m)return m[1];
    }catch(e){} return null;
  }
  async function fetchFullText(it){
    try{
      if(WORKER_BASE_URL){
        if(isYouTube(it.url)){
          var id=youTubeId(it.url);
          if(id){
            var r=await workerGet(WORKER_BASE_URL+"/yt-transcript?id="+encodeURIComponent(id));
            if(r.ok){ var j=await r.json(); if(j&&j.text)return j.text; }
          }
        }
        var r2=await workerGet(WORKER_BASE_URL+"/extract?url="+encodeURIComponent(it.url));
        if(r2.ok){ var j2=await r2.json(); if(j2&&j2.text)return j2.text; }
      }
    }catch(e){}
    return (it.summary||it.title||"");
  }
async function analyzeViaWorker(it){
  if (!WORKER_BASE_URL) return null;
  try {
    var id = isYouTube(it.url) ? youTubeId(it.url) : null;

    var payload = id
      ? {
          yt_id: id,
          title: it.title,
          url: it.url,
          source: it.source,
          published: it.published,
          // force proper long analysis for YouTube
          force_whisper: true,
          prefer_whisper: true,
          mode: 'preview',          // 'preview' (~10–12 min) or 'full' (whole video)
          detail: 'long'         // hint for the LLM to be thorough
        }
      : {
          url: it.url,
          title: it.title,
          source: it.source,
          published: it.published,
          detail: 'long'
        };

    // Owner key and/or fresh Turnstile token per call (tokens are single-use); a failure here
    // throws and we fall back to the local summarizer exactly as before.
    var headers = { 'content-type': 'application/json' };
    var auth = await workerAuthHeaders();
    for (var hk in auth) { if (Object.prototype.hasOwnProperty.call(auth, hk)) headers[hk] = auth[hk]; }

    var r = await fetch(WORKER_BASE_URL + '/analyze', {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(payload)
    });
    noteWorkerResponse(r);
    if (!r.ok) return null;
    var j = await r.json();
    return j.analysis || null;
  } catch(e){
    return null;
  }
}

  function renderAnalysisSection(it,a){
    var dt=new Date(it.published), when=dt.toLocaleDateString();
    var header="<h4>"+esc(it.title)+' <span class="meta">· '+esc(it.source)+" · "+when+'</span> — <a href="'+it.url+'" target="_blank" rel="noopener">open</a></h4>';
    var out='<div class="deep-section">'+header;
    if(a.tldr && a.tldr.trim()){ out+='<p><strong>TL;DR:</strong> '+esc(a.tldr)+'</p>'; }
    if(Array.isArray(a.key_points) && a.key_points.length){ out+='<h5>Key Points</h5><ul>'+a.key_points.map(function(s){return '<li>'+esc(s)+'</li>';}).join("")+'</ul>'; }
    if(Array.isArray(a.pros) && a.pros.length){ out+='<h5>Upsides</h5><ul>'+a.pros.map(function(s){return '<li>'+esc(s)+'</li>';}).join("")+'</ul>'; }
    if(Array.isArray(a.cons) && a.cons.length){ out+='<h5>Risks / Caveats</h5><ul>'+a.cons.map(function(s){return '<li>'+esc(s)+'</li>';}).join("")+'</ul>'; }
    if(Array.isArray(a.notable_quotes) && a.notable_quotes.length){ out+='<h5>Notable Quotes</h5><ul>'+a.notable_quotes.map(function(q){return '<li>“'+esc(q.quote||"")+'” '+(q.at?'<span class="meta">('+esc(q.at)+')</span>':"")+'</li>';}).join("")+'</ul>'; }
    if(Array.isArray(a.actions) && a.actions.length){ out+='<h5>Actions</h5><ul>'+a.actions.map(function(s){return '<li>'+esc(s)+'</li>';}).join("")+'</ul>'; }
    out+='</div>'; return out;
  }
  function renderFallbackSection(it,bullets){
    var dt=new Date(it.published), when=dt.toLocaleDateString();
    var header="<h4>"+esc(it.title)+' <span class="meta">· '+esc(it.source)+" · "+when+'</span> — <a href="'+it.url+'" target="_blank" rel="noopener">open</a></h4>';
    var list="<ul>"+bullets.map(function(b){return "<li>"+esc(b)+"</li>";}).join("")+"</ul>";
    return '<div class="deep-section">'+header+'<p class="meta">Fallback summary</p>'+list+'</div>';
  }

  var STOP = new Set(("a,an,the,of,in,on,for,to,from,by,with,as,at,that,this,these,those,and,or,not,be,is,are,was,were,been,being,has,have,had,do,does,did,will,would,shall,should,can,could,may,might,must,if,then,else,when,while,about,into,over,after,before,up,down,out,off,again,further,here,there,why,how,all,any,both,each,few,more,most,other,some,such,no,nor,only,own,same,so,than,too,very").split(","));
  function tokenizeSentences(text){ text=String(text||"").replace(/\s+/g," "); return text.split(/(?:\.|\?|!)\s+(?=[A-Z0-9\"'(])/).filter(function(s){return s && s.trim().length>20;}).slice(0,60); }
  function tokenizeWords(s){ return s.toLowerCase().match(/[a-z0-9']+/g)||[]; }
  function summarizeText(text,maxSentences){ maxSentences=maxSentences||6; var sentences=tokenizeSentences(text); if(sentences.length<=maxSentences) return sentences; var tf=Object.create(null); var sWords=sentences.map(tokenizeWords); sWords.forEach(function(words){ words.forEach(function(w){ if(!STOP.has(w)) tf[w]=(tf[w]||0)+1; }); }); var scores=sWords.map(function(words){ return words.reduce(function(acc,w){ return acc+(STOP.has(w)?0:(tf[w]||0)); },0); }); var idxs=scores.map(function(s,i){return [s,i];}).sort(function(a,b){return b[0]-a[0];}).slice(0,maxSentences).map(function(x){return x[1];}).sort(function(a,b){return a-b;}); return idxs.map(function(i){return sentences[i];}); }
  function summaryToBullets(sentences,maxBullets){ maxBullets=maxBullets||6; return (sentences||[]).slice(0,maxBullets).map(function(s){return s.trim();}); }

  // Consume a "#key=..." link as soon as the page loads (only when the lock is on — with the flag
  // off this line does nothing: no storage access, no history change, the fragment is left alone).
  if(OWNER_KEY_ENABLED){ try { consumeKeyFragment(); } catch(e){} }
})();
