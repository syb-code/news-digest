// Private stocks for the news digest: the owner's ticker list, settings and
// analyses, kept in a Cloudflare KV namespace and shown on the dashboard only
// to the holder of the access key.
//
// What it does:
//   - The dashboard (with the key) reads the list, the market overview and
//     one analysis per ticker, adds and removes tickers, picks the Claude
//     model, and can ask for a run now ("Run analysis now", at most once
//     every 30 minutes). These routes run behind protect() from
//     worker/auth-guard.js, like every other web request.
//   - The GitHub job (.github/workflows/stocks.yml) reads the list and each
//     ticker's learning state, and saves the states, the analyses and the
//     overview, through the ingest routes under /stocks/ingest/. They are not
//     behind protect(): they take their own key, STOCKS_INGEST_KEY, compared
//     in constant time (keyMatches from worker/auth-guard.js), and refuse any
//     request that comes from a browser (one with an Origin header).
//   - The cron at 05:45 America/Los_Angeles starts stocks.yml next to the
//     morning digest run (scheduledAll below). The workflow itself skips the
//     days the NYSE is closed.
//
// Nothing private is in the repository: the list, the settings, the analyses
// and what each ticker learns live only in this KV namespace. Worker logs
// never hold a ticker, a key or a body; they start with "Stocks: ".
//
// Setup, once:
//
//   1. Create the KV namespace. Cloudflare dashboard > Storage & Databases >
//      KV > Create a namespace, named for example news-digest-stocks. Or from
//      a terminal: npx wrangler kv namespace create news-digest-stocks
//
//   2. Bind it to the Worker as STOCKS_KV. Workers & Pages > your Worker >
//      Settings > Bindings > Add > KV namespace. Variable name: STOCKS_KV,
//      namespace: the one from step 1.
//
//   3. The ingest key. Generate a value with: openssl rand -base64 32
//      It must differ from ACCESS_KEY and be at least 32 characters. Add it
//      twice, with the same value:
//        Cloudflare: Settings > Variables and Secrets > Add, type Secret,
//                    name STOCKS_INGEST_KEY
//                    (or: npx wrangler secret put STOCKS_INGEST_KEY --name <worker>)
//        GitHub:     Settings > Environments > digest > Add secret,
//                    name STOCKS_INGEST_KEY
//      GH_DISPATCH_TOKEN and GH_REPO (see worker/scheduler.js) also start
//      stocks.yml; the token needs nothing more than Actions "Read and write"
//      on this one repository.
//
//   4. Edit code: paste this whole file right below worker/sources.js, which
//      sits below worker/scheduler.js and the access guard. Then make the last
//      line of your Worker one of these two, and click Deploy.
//
//      A Worker with no summaries code of its own (worker/worker.js is this,
//      ready to paste in one piece):
//
//        export default { fetch: routeWorker, scheduled: scheduledAll };
//
//      A Worker that also has its own summaries code, kept in `original` as
//      the guard's steps describe: the ingest routes go to
//      handleStocksIngest, and every other request through the guard to
//      /x-accounts, /stocks... or your code.
//
//        export default { ...original, fetch: (request, env, ctx) => (isStocksIngest(new URL(request.url).pathname) ? handleStocksIngest(request, env, ctx) : protect((r, e, c) => { const p = new URL(r.url).pathname; return p === "/x-accounts" ? handleSources(r, e, c) : isStocksPath(p) ? handleStocks(r, e, c) : original.fetch(r, e, c); })(request, env, ctx)), scheduled: scheduledAll };
//
//   5. In GitHub, add the variable STOCKS_ENABLED with the value true
//      (Settings > Secrets and variables > Actions > Variables), and the
//      secrets TIINGO_API_KEY and SEC_USER_AGENT to the digest environment
//      (see the README).
//
// Check it (use your own address and keys):
//   curl -i https://<your-worker>.workers.dev/stocks/ingest/config
//     answers 401 (no key) or 503 (STOCKS_INGEST_KEY is not set);
//   curl -i https://<your-worker>.workers.dev/stocks/ingest/config -H "Authorization: Bearer <STOCKS_INGEST_KEY>"
//     answers 200 with {"version": 1, "tickers": [], ...};
//   curl -i https://<your-worker>.workers.dev/stocks/list -H "Authorization: Bearer <ACCESS_KEY>"
//     answers 200 (500 means STOCKS_KV is not bound).
//
// Routes behind the access key (JSON, cache-control: no-store):
//   GET  /stocks/list        the list in order, with each analysis's summary
//   GET  /stocks/overview    the market overview (404 before the first run)
//   GET  /stocks/t/<T>       one ticker's analysis
//   POST /stocks/tickers     {"action": "add" or "remove", "ticker": "..."}; remove also forgets
//                            that ticker's analysis and learning state
//   GET  /stocks/settings    the model and the allowed models
//   POST /stocks/settings    {"model": "..."}, one of STOCKS_MODELS
//   POST /stocks/refresh     starts stocks.yml now, at most once every 30 minutes
// Ingest routes (STOCKS_INGEST_KEY, no browser):
//   GET  /stocks/ingest/config, GET and PUT /stocks/ingest/state/<T>,
//   PUT  /stocks/ingest/ticker/<T>, PUT /stocks/ingest/overview

const STOCKS_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"];
const STOCKS_DEFAULT_MODEL = "claude-sonnet-5-5";
const STOCKS_MAX_TICKERS = 30;
const STOCKS_TICKER = /^[A-Z]{1,6}(?:-[A-Z]{1,3})?$/; // the same check as scripts/stocks/__init__.py
const STOCKS_BAD_TICKER = "a ticker is 1 to 6 letters, with an optional class such as BRK-B";
const STOCKS_MAX_BODY = 2000; // characters of JSON accepted from the dashboard
const STOCKS_CAPS = { config: 16384, settings: 4096, overview: 262144, ticker: 524288, state: 1048576 };
const STOCKS_KINDS = { overview: "stocks.overview", ticker: "stocks.ticker", state: "stocks.state" };
const STOCKS_MIN_INGEST_KEY = 32;
const STOCKS_REFRESH_MIN_MS = 30 * 60 * 1000;
const STOCKS_WORKFLOW = "stocks.yml";
const STOCKS_BRANCH = "main";
const STOCKS_TIME = "05:45"; // America/Los_Angeles, the morning cron of worker/scheduler.js
const STOCKS_RETRY_MS = 10000;
const STOCKS_RECENT_MS = 120000;
const STOCKS_NO_KV = "STOCKS_KV is not bound on this worker";

function stocksAnswer(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

// A stored JSON text sent back as it is.
function stocksText(text) {
  return new Response(text, { status: 200, headers: { "content-type": "application/json", "cache-control": "no-store" } });
}

// The ingest routes' own refusals: x-guard: deny, and no CORS headers.
function stocksDeny(status, message) {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", "x-guard": "deny" },
  });
}

function stocksNormalize(value) {
  if (typeof value !== "string") return null;
  const ticker = value.trim().toUpperCase().replace(/[./]/g, "-");
  return STOCKS_TICKER.test(ticker) ? ticker : null;
}

function isStocksIngest(pathname) {
  return pathname === "/stocks/ingest" || pathname.startsWith("/stocks/ingest/");
}

function isStocksPath(pathname) {
  return pathname === "/stocks" || pathname.startsWith("/stocks/");
}

async function stocksRead(env, key) {
  const text = await env.STOCKS_KV.get(key);
  if (text === null || text === undefined) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

async function stocksConfig(env) {
  const doc = await stocksRead(env, "stocks:config");
  const tickers = doc && Array.isArray(doc.tickers) ? doc.tickers.map(stocksNormalize).filter(Boolean) : [];
  const overrides = doc && doc.overrides && typeof doc.overrides === "object" && !Array.isArray(doc.overrides)
    ? doc.overrides : {};
  return { version: 1, tickers: [...new Set(tickers)].slice(0, STOCKS_MAX_TICKERS), overrides };
}

async function stocksModel(env) {
  const doc = await stocksRead(env, "stocks:settings");
  return doc && STOCKS_MODELS.includes(doc.model) ? doc.model : STOCKS_DEFAULT_MODEL;
}

function stocksSettingsBody(model) {
  return { model, default_model: STOCKS_DEFAULT_MODEL, allowed_models: [...STOCKS_MODELS] };
}

// The body as text, at most cap bytes of UTF-8: { text } or { status, error }.
async function stocksBody(request, cap) {
  const declared = Number(request.headers.get("content-length") || "0");
  if (Number.isFinite(declared) && declared > cap) return { status: 413, error: "too large" };
  const text = await request.text();
  if (new TextEncoder().encode(text).length > cap) return { status: 413, error: "too large" };
  return { text };
}

// A small JSON body from the dashboard: the object, or null.
async function stocksSmallJson(request) {
  try {
    const text = await request.text();
    if (text.length > STOCKS_MAX_BODY) return null;
    const body = JSON.parse(text);
    return body && typeof body === "object" && !Array.isArray(body) ? body : null;
  } catch {
    return null;
  }
}

function stocksNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stocksWord(value, limit) {
  return typeof value === "string" ? value.slice(0, limit) : null;
}

// The learner's hit rate over its longest horizon: graded live calls once there
// are 10, else its backtest. Kept in the metadata so the dashboard's list needs
// no document (30 documents a page load would pass the guard's rate limit).
function stocksHit(doc) {
  const learner = doc.learner && typeof doc.learner === "object" ? doc.learner : {};
  const card = learner.scorecard && typeof learner.scorecard === "object" ? learner.scorecard : {};
  const live = card.live && typeof card.live === "object" ? card.live : {};
  const back = card.backtest && typeof card.backtest === "object" ? card.backtest : {};
  const horizons = Object.keys(live).concat(Object.keys(back)).filter((k) => /^\d{1,3}$/.test(k)).map(Number);
  if (!horizons.length) return null;
  const key = String(Math.max(...horizons));
  const usable = (s, least) => (s && typeof s === "object" && Number.isInteger(s.n) && s.n >= least
    && stocksNumber(s.hit_rate) !== null ? s : null);
  const fromLive = usable(live[key], 10);
  if (fromLive) return { rate: fromLive.hit_rate, n: fromLive.n, kind: "live", h: Number(key) };
  const fromBack = usable(back[key], 1);
  if (fromBack) return { rate: fromBack.hit_rate, n: fromBack.n, kind: "backtest", h: Number(key) };
  return null;
}

// The KV metadata of an analysis, so /stocks/list reads one list() call.
function stocksMeta(doc) {
  const header = doc.header && typeof doc.header === "object" ? doc.header : {};
  const assessment = doc.assessment && typeof doc.assessment === "object" ? doc.assessment : {};
  return {
    name: stocksWord(header.name, 60),
    close: stocksNumber(header.last_close),
    change_pct: stocksNumber(header.change_pct),
    stance: stocksWord(assessment.stance, 12),
    confidence: stocksWord(assessment.confidence, 12),
    status: stocksWord(doc.status, 20),
    data_as_of: stocksWord(doc.data_as_of, 10),
    generated_at: stocksWord(doc.generated_at, 32),
    hit: stocksHit(doc),
  };
}

// Every key under a prefix with its metadata.
async function stocksList(env, prefix) {
  const out = new Map();
  let cursor;
  for (let page = 0; page < 20; page += 1) {
    const result = await env.STOCKS_KV.list({ prefix, cursor });
    for (const key of result.keys || []) out.set(key.name, key.metadata || null);
    if (result.list_complete || !result.cursor) break;
    cursor = result.cursor;
  }
  return out;
}

// ---------------------------------------------------------------- the ingest routes (the GitHub job)

async function handleStocksIngest(request, env, ctx) {
  if (request.headers.get("origin")) return stocksDeny(403, "browsers may not call ingest routes");
  const key = String(env.STOCKS_INGEST_KEY || "");
  if (key.length < STOCKS_MIN_INGEST_KEY || (env.ACCESS_KEY && key === env.ACCESS_KEY)) {
    return stocksDeny(503, "the ingest key is not configured");
  }
  const auth = request.headers.get("authorization") || "";
  const given = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!given || !(await keyMatches(given, key))) return stocksDeny(401, "unauthorized");
  if (!env.STOCKS_KV) return stocksAnswer(500, { error: STOCKS_NO_KV });

  const path = new URL(request.url).pathname.replace(/\/+$/, "");
  if (path === "/stocks/ingest/config") {
    if (request.method !== "GET") return stocksAnswer(405, { error: "use GET" });
    const config = await stocksConfig(env);
    return stocksAnswer(200, { version: 1, tickers: config.tickers, model: await stocksModel(env), overrides: config.overrides });
  }
  if (path === "/stocks/ingest/overview") {
    if (request.method !== "PUT") return stocksAnswer(405, { error: "use PUT" });
    return stocksSave(request, env, "overview", null);
  }
  const match = path.match(/^\/stocks\/ingest\/(state|ticker)\/([^/]+)$/);
  if (!match) return stocksAnswer(404, { error: "not found" });
  const kind = match[1];
  let raw;
  try {
    raw = decodeURIComponent(match[2]);
  } catch {
    raw = "";
  }
  const ticker = stocksNormalize(raw);
  if (kind === "state" && request.method === "GET") {
    if (!ticker || ticker !== raw) return stocksAnswer(400, { error: STOCKS_BAD_TICKER });
    if (!(await stocksConfig(env)).tickers.includes(ticker)) return stocksAnswer(409, { error: "not in the list" });
    const text = await env.STOCKS_KV.get(`stocks:state:${ticker}`);
    return text === null || text === undefined ? stocksAnswer(404, { error: "no state" }) : stocksText(text);
  }
  if (request.method !== "PUT") return stocksAnswer(405, { error: kind === "state" ? "use GET or PUT" : "use PUT" });
  if (!ticker || ticker !== raw) return stocksAnswer(400, { error: STOCKS_BAD_TICKER });
  return stocksSave(request, env, kind, ticker);
}

// Checks and stores one document of the job: the kind, the version, the
// ticker, the size; an analysis also gets its metadata.
async function stocksSave(request, env, kind, ticker) {
  if (ticker && !(await stocksConfig(env)).tickers.includes(ticker)) {
    return stocksAnswer(409, { error: "not in the list" });
  }
  const body = await stocksBody(request, STOCKS_CAPS[kind]);
  if (body.error) return stocksAnswer(body.status, { error: body.error });
  let doc = null;
  try {
    doc = JSON.parse(body.text);
  } catch {
    doc = null;
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) return stocksAnswer(400, { error: "send a JSON object" });
  if (doc.kind !== STOCKS_KINDS[kind]) return stocksAnswer(400, { error: `kind must be ${STOCKS_KINDS[kind]}` });
  if (doc.version !== 1) return stocksAnswer(400, { error: "version must be 1" });
  if (ticker && doc.ticker !== ticker) return stocksAnswer(400, { error: "the document is for another ticker" });
  const bytes = new TextEncoder().encode(body.text).length;
  if (kind === "overview") {
    const generated = stocksWord(doc.generated_at, 32);
    await env.STOCKS_KV.put("stocks:overview", body.text, { metadata: { generated_at: generated } });
    console.log(`Stocks: saved the market overview, ${bytes} bytes`);
  } else if (kind === "ticker") {
    await env.STOCKS_KV.put(`stocks:ticker:${ticker}`, body.text, { metadata: stocksMeta(doc) });
    console.log(`Stocks: saved a ticker analysis, ${bytes} bytes`);
  } else {
    await env.STOCKS_KV.put(`stocks:state:${ticker}`, body.text);
    console.log(`Stocks: saved a learning state, ${bytes} bytes`);
  }
  return stocksAnswer(200, { ok: true, bytes });
}

// ---------------------------------------------------------------- the dashboard's routes (behind protect)

async function handleStocks(request, env, ctx) {
  if (!env.STOCKS_KV) return stocksAnswer(500, { error: STOCKS_NO_KV });
  const path = new URL(request.url).pathname.replace(/\/+$/, "");
  const method = request.method;
  const only = (wanted) => (method === wanted ? null : stocksAnswer(405, { error: `use ${wanted}` }));

  if (path === "/stocks/list") return only("GET") || stocksListAnswer(env);
  if (path === "/stocks/overview") {
    const refused = only("GET");
    if (refused) return refused;
    const text = await env.STOCKS_KV.get("stocks:overview");
    return text === null || text === undefined ? stocksAnswer(404, { error: "no overview yet" }) : stocksText(text);
  }
  if (path.startsWith("/stocks/t/")) {
    const refused = only("GET");
    if (refused) return refused;
    let raw;
    try {
      raw = decodeURIComponent(path.slice("/stocks/t/".length));
    } catch {
      raw = "";
    }
    const ticker = stocksNormalize(raw);
    if (!ticker) return stocksAnswer(400, { error: STOCKS_BAD_TICKER });
    if (!(await stocksConfig(env)).tickers.includes(ticker)) return stocksAnswer(404, { error: "not in the list" });
    const text = await env.STOCKS_KV.get(`stocks:ticker:${ticker}`);
    return text === null || text === undefined ? stocksAnswer(404, { error: "no analysis yet" }) : stocksText(text);
  }
  if (path === "/stocks/tickers") return only("POST") || stocksEditList(request, env);
  if (path === "/stocks/settings") {
    if (method === "GET") return stocksAnswer(200, stocksSettingsBody(await stocksModel(env)));
    if (method !== "POST") return stocksAnswer(405, { error: "use GET or POST" });
    const body = await stocksSmallJson(request);
    const model = body && STOCKS_MODELS.includes(body.model) ? body.model : null; // one of three fixed names
    if (!model) return stocksAnswer(400, { error: "that model is not allowed" });
    await env.STOCKS_KV.put("stocks:settings", JSON.stringify({ version: 1, model, updated_at: new Date().toISOString() }));
    console.log(`Stocks: the model is now ${model}`);
    return stocksAnswer(200, { ok: true, model });
  }
  if (path === "/stocks/refresh") return only("POST") || stocksRefresh(env);
  return stocksAnswer(404, { error: "not found" });
}

async function stocksRefreshInfo(env) {
  const doc = await stocksRead(env, "stocks:refresh");
  const at = doc && typeof doc.requested_at === "string" ? Date.parse(doc.requested_at) : NaN;
  if (!Number.isFinite(at)) return { requested_at: null, available_at: null, wait_ms: 0 };
  const available = at + STOCKS_REFRESH_MIN_MS;
  return {
    requested_at: new Date(at).toISOString(),
    available_at: new Date(available).toISOString(),
    wait_ms: Math.max(0, available - Date.now()),
  };
}

async function stocksListAnswer(env) {
  const config = await stocksConfig(env);
  const metas = await stocksList(env, "stocks:ticker:");
  const overviewMeta = (await stocksList(env, "stocks:overview")).get("stocks:overview");
  const refresh = await stocksRefreshInfo(env);
  const tickers = config.tickers.map((ticker) => {
    const meta = metas.get(`stocks:ticker:${ticker}`);
    const m = meta && typeof meta === "object" ? meta : {};
    return {
      ticker,
      status: meta ? (m.status || "ok") : "pending",
      name: m.name ?? null,
      close: m.close ?? null,
      change_pct: m.change_pct ?? null,
      stance: m.stance ?? null,
      confidence: m.confidence ?? null,
      data_as_of: m.data_as_of ?? null,
      generated_at: m.generated_at ?? null,
      hit: m.hit ?? null,
    };
  });
  return stocksAnswer(200, {
    tickers,
    max: STOCKS_MAX_TICKERS,
    settings: stocksSettingsBody(await stocksModel(env)),
    refresh: { min_minutes: STOCKS_REFRESH_MIN_MS / 60000, requested_at: refresh.requested_at, available_at: refresh.available_at },
    overview: { generated_at: overviewMeta && overviewMeta.generated_at ? overviewMeta.generated_at : null },
  });
}

async function stocksEditList(request, env) {
  const body = await stocksSmallJson(request);
  if (!body) return stocksAnswer(400, { error: "send JSON with an action and a ticker" });
  if (body.action !== "add" && body.action !== "remove") return stocksAnswer(400, { error: "the action must be add or remove" });
  const ticker = stocksNormalize(body.ticker);
  if (!ticker) return stocksAnswer(400, { error: STOCKS_BAD_TICKER });
  const config = await stocksConfig(env);
  const has = config.tickers.includes(ticker);
  if (body.action === "add") {
    if (has) return stocksAnswer(200, { ok: true, changed: false, tickers: config.tickers });
    if (config.tickers.length >= STOCKS_MAX_TICKERS) {
      return stocksAnswer(409, { error: `the list already holds ${STOCKS_MAX_TICKERS} tickers` });
    }
    config.tickers.push(ticker);
  } else {
    if (!has) return stocksAnswer(200, { ok: true, changed: false, tickers: config.tickers });
    config.tickers = config.tickers.filter((t) => t !== ticker);
    // Removing forgets everything: the analysis and what the ticker learned.
    await env.STOCKS_KV.delete(`stocks:ticker:${ticker}`);
    await env.STOCKS_KV.delete(`stocks:state:${ticker}`);
  }
  const saved = { version: 1, tickers: config.tickers, overrides: config.overrides, updated_at: new Date().toISOString() };
  await env.STOCKS_KV.put("stocks:config", JSON.stringify(saved));
  const done = body.action === "add" ? "added a ticker" : "removed a ticker";
  console.log(`Stocks: ${done}, ${config.tickers.length} in the list`);
  return stocksAnswer(200, { ok: true, changed: true, tickers: config.tickers });
}

function stocksHeaders(env) {
  return {
    Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "news-digest-stocks",
  };
}

// GitHub's short error message, never the request: the request holds the token.
async function stocksErrorDetail(response) {
  try {
    const text = await response.text();
    const message = JSON.parse(text).message;
    return String(message || text).slice(0, 200);
  } catch {
    return "";
  }
}

// One request to start stocks.yml. { ok } when GitHub took it; otherwise what
// went wrong, and whether GitHub may have started the run all the same.
async function stocksDispatch(repo, reason, env) {
  const url = `https://api.github.com/repos/${repo}/actions/workflows/${STOCKS_WORKFLOW}/dispatches`;
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { ...stocksHeaders(env), "Content-Type": "application/json" },
      body: JSON.stringify({ ref: STOCKS_BRANCH, inputs: { reason } }),
    });
    if (response.ok) return { ok: true, status: response.status };
    const problem = `GitHub answered ${response.status} ${await stocksErrorDetail(response)}`.trim();
    return { ok: false, problem, unsure: response.status >= 500, answered: true };
  } catch (err) {
    return { ok: false, problem: `GitHub could not be reached (${err && err.name ? err.name : "error"})`, unsure: true, answered: false };
  }
}

function stocksRepoReady(env) {
  return /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(String(env.GH_REPO || "")) && !!env.GH_DISPATCH_TOKEN;
}

async function stocksRefresh(env) {
  const info = await stocksRefreshInfo(env);
  if (info.wait_ms > 0) {
    return stocksAnswer(429, { error: "a run was started recently", retry_after_seconds: Math.ceil(info.wait_ms / 1000) });
  }
  if (!stocksRepoReady(env)) {
    console.log("Stocks: set GH_REPO (owner/name) and the GH_DISPATCH_TOKEN secret on this Worker");
    return stocksAnswer(500, { error: "this worker cannot start runs: set GH_REPO and GH_DISPATCH_TOKEN" });
  }
  const repo = String(env.GH_REPO);
  let result = await stocksDispatch(repo, "manual", env);
  if (!result.ok && result.unsure) result = await stocksDispatch(repo, "manual", env);
  if (!result.ok) {
    console.log(`Stocks: the manual run did not start: ${result.problem}`);
    return stocksAnswer(502, { error: `the run did not start: ${result.problem}` });
  }
  const now = Date.now();
  const requested = new Date(now).toISOString();
  await env.STOCKS_KV.put("stocks:refresh", JSON.stringify({ requested_at: requested }));
  console.log(`Stocks: started a manual run, GitHub answered ${result.status}`);
  return stocksAnswer(202, { ok: true, requested_at: requested, available_at: new Date(now + STOCKS_REFRESH_MIN_MS).toISOString() });
}

// ---------------------------------------------------------------- routing

// Behind the guard: the X accounts editor, the stocks, or 501 for anything else.
async function routeKeyHolder(request, env, ctx) {
  const pathname = new URL(request.url).pathname;
  if (pathname.replace(/\/+$/, "") === "/x-accounts") return handleSources(request, env, ctx);
  if (isStocksPath(pathname)) return handleStocks(request, env, ctx);
  return stocksAnswer(501, { error: "summaries are not set up on this worker" });
}

// The fetch handler of worker/worker.js: the ingest routes with their own
// key, every other request through the access guard.
function routeWorker(request, env, ctx) {
  if (isStocksIngest(new URL(request.url).pathname)) return handleStocksIngest(request, env, ctx);
  return protect(routeKeyHolder)(request, env, ctx);
}

// ---------------------------------------------------------------- the cron

// Whether a scheduled stocks run was created in the last STOCKS_RECENT_MS:
// true, false, or null when the list of runs cannot be read.
async function stocksRunExists(repo, env) {
  const url = `https://api.github.com/repos/${repo}/actions/workflows/${STOCKS_WORKFLOW}/runs`
    + `?event=workflow_dispatch&branch=${STOCKS_BRANCH}&per_page=20`;
  try {
    const response = await fetch(url, { method: "GET", headers: stocksHeaders(env) });
    if (!response.ok) return null;
    const runs = (await response.json()).workflow_runs;
    if (!Array.isArray(runs)) return null;
    const since = Date.now() - STOCKS_RECENT_MS;
    return runs.some((r) => r && Date.parse(r.created_at) >= since
      && (typeof r.display_title !== "string" || r.display_title.includes("(scheduled)")));
  } catch {
    return null;
  }
}

// Starts the scheduled stocks run at 05:45 America/Los_Angeles, with the same
// look-before-retry rule as dispatchDigest. Other times do nothing.
async function dispatchStocks(event, env, ctx) {
  if (digestLocalTime(event.scheduledTime) !== STOCKS_TIME) return;
  if (!env.STOCKS_KV) {
    console.log("Stocks: not set up on this worker, nothing to start");
    return;
  }
  if (!stocksRepoReady(env)) {
    throw new Error("Stocks: set GH_REPO (owner/name) and the GH_DISPATCH_TOKEN secret on this Worker");
  }
  const repo = String(env.GH_REPO);
  let result = await stocksDispatch(repo, "scheduled", env);
  if (!result.ok && result.unsure) {
    console.log(`Stocks: the scheduled run may not have started: ${result.problem}. Looking for it before any retry`);
    await new Promise((resolve) => setTimeout(resolve, STOCKS_RETRY_MS));
    const exists = await stocksRunExists(repo, env);
    if (exists === true) {
      console.log("Stocks: the scheduled run did start, so no second request was sent");
      return;
    }
    if (exists === null && result.answered) {
      throw new Error(`Stocks: the scheduled run may not have started: ${result.problem}, and the list of runs `
        + "could not be read. No retry was sent, so check the Actions tab");
    }
    result = await stocksDispatch(repo, "scheduled", env);
  }
  if (result.ok) {
    console.log(`Stocks: started the scheduled run, GitHub answered ${result.status}`);
    return;
  }
  console.log(`Stocks: the scheduled run did not start: ${result.problem}`);
  throw new Error(`Stocks: the scheduled run did not start: ${result.problem}`);
}

// The scheduled handler of worker/worker.js: the digest runs, then the stocks
// run. Both always run; the first error is thrown again afterwards, so the
// cron event shows as failed in the Worker's logs.
async function scheduledAll(event, env, ctx) {
  let first = null;
  for (const job of [dispatchDigest, dispatchStocks]) {
    try {
      await job(event, env, ctx);
    } catch (err) {
      if (!first) first = err;
    }
  }
  if (first) throw first;
}
