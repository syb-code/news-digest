// Private stocks for the news digest: the owner's ticker list, settings and
// analyses, kept in a Cloudflare KV namespace and shown on the dashboard only
// to the holder of the access key.
//
// What it does:
//   - The dashboard (with the key) reads the list, the market overview and
//     one analysis per ticker, adds and removes tickers, picks the Claude
//     model, asks for a run now ("Run analysis now") and stops a run that is
//     going ("Stop the run"). Before it starts a run, the Worker reads
//     GitHub's list of stocks.yml runs: no start while a run is queued or
//     going, and none until GitHub lists the run it was asked for (at most 2
//     minutes). A run that failed or was stopped frees the button at once.
//     When GitHub cannot be read, the old rule stands: at most one start
//     every 30 minutes. These routes run behind protect() from
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
//      stocks.yml, read its list of runs and stop a run; the token needs
//      nothing more than Actions "Read and write" on this one repository.
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
//     answers 200 (500 means STOCKS_KV is not bound);
//   curl -i https://<your-worker>.workers.dev/stocks/run -H "Authorization: Bearer <ACCESS_KEY>"
//     answers 200 with "checked": true once GitHub's list of runs can be read
//     ("reason": "not set up" means GH_REPO or GH_DISPATCH_TOKEN is missing).
//
// Routes behind the access key (JSON, cache-control: no-store):
//   GET  /stocks/list        the list in order, with each analysis's summary
//   GET  /stocks/overview    the market overview (404 before the first run)
//   GET  /stocks/t/<T>       one ticker's analysis
//   POST /stocks/tickers     {"action": "add" or "remove", "ticker": "..."}; remove also forgets
//                            that ticker's analysis, learning state and overrides
//   GET  /stocks/settings    the model and the allowed models
//   POST /stocks/settings    {"model": "..."}, one of STOCKS_MODELS
//   GET  /stocks/run         whether a stocks.yml run is going, and the newest run:
//                            {checked, active, can_start, available_at, requested_at, run}
//   POST /stocks/stop        stops every stocks.yml run that is queued or going: 200 {ok, stopped},
//                            409 while a run asked for is still being created, 502 when GitHub
//                            cannot be read or does not stop a run
//   POST /stocks/refresh     starts stocks.yml now: 409 while a run is going, 429 until GitHub lists
//                            the run of the last start, at most 2 minutes (30 minutes when GitHub
//                            cannot be read)
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
const STOCKS_META_MAX = 1024; // bytes of JSON, the KV limit for the metadata of a key
// The spaces Python's str.strip() removes (str.isspace), not trim()'s: U+001C to U+001F and U+0085 are
// spaces there, U+FEFF is not.
const STOCKS_SPACES = /^[\t-\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\t-\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g;
const STOCKS_KINDS = { overview: "stocks.overview", ticker: "stocks.ticker", state: "stocks.state" };
const STOCKS_MIN_INGEST_KEY = 32;
const STOCKS_REFRESH_MIN_MS = 30 * 60 * 1000;
const STOCKS_WORKFLOW = "stocks.yml";
const STOCKS_BRANCH = "main";
const STOCKS_TIME = "05:45"; // America/Los_Angeles, the morning cron of worker/scheduler.js
const STOCKS_RETRY_MS = 10000;
const STOCKS_RECENT_MS = 120000;
const STOCKS_NO_KV = "STOCKS_KV is not bound on this worker";
// GitHub takes a few seconds to a minute to list a run it was asked to start:
// for this long after a start with no run listed, the run is still being created.
const STOCKS_START_GRACE_MS = 2 * 60 * 1000;
const STOCKS_CLOCK_SKEW_MS = 60 * 1000; // between this Worker's clock and GitHub's
const STOCKS_RUNS_MAX_BYTES = 1048576; // of GitHub's list of runs, read at most
const STOCKS_RUN_ACTIVE = ["requested", "queued", "in_progress", "waiting", "pending"];
const STOCKS_RUN_STATUSES = [...STOCKS_RUN_ACTIVE, "completed"];
const STOCKS_RUN_CONCLUSIONS = ["success", "failure", "cancelled", "skipped", "timed_out", "action_required", "neutral",
  "stale", "startup_failure"];
const STOCKS_RUN_KINDS = { "Stocks (manual)": "manual", "Stocks (scheduled)": "scheduled" }; // run-name of stocks.yml
const STOCKS_ISO_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$/; // GitHub's times are UTC

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
  const ticker = value.replace(STOCKS_SPACES, "").toUpperCase().replace(/[./]/g, "-");
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

async function stocksListed(env, ticker) {
  return (await stocksConfig(env)).tickers.includes(ticker);
}

// The overrides of the listed tickers only, whatever the spelling of their keys.
function stocksOverridesOf(overrides, tickers) {
  return Object.fromEntries(Object.entries(overrides).filter(([k]) => tickers.includes(stocksNormalize(k))));
}

async function stocksModel(env) {
  const doc = await stocksRead(env, "stocks:settings");
  return doc && STOCKS_MODELS.includes(doc.model) ? doc.model : STOCKS_DEFAULT_MODEL;
}

function stocksSettingsBody(model) {
  return { model, default_model: STOCKS_DEFAULT_MODEL, allowed_models: [...STOCKS_MODELS] };
}

// The body as text, at most cap bytes of UTF-8: { text } or { status, error }.
// Reading stops at cap + 1 bytes, so a body without content-length (chunked)
// is never held in full.
async function stocksBody(request, cap) {
  const declared = Number(request.headers.get("content-length") || "0");
  if (Number.isFinite(declared) && declared > cap) return { status: 413, error: "too large" };
  const chunks = [];
  let size = 0;
  if (request.body) {
    const reader = request.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > cap) {
        reader.cancel().catch(() => {});
        return { status: 413, error: "too large" };
      }
      chunks.push(value);
    }
  }
  const bytes = new Uint8Array(size);
  let at = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, at);
    at += chunk.byteLength;
  }
  const text = new TextDecoder().decode(bytes);
  if (new TextEncoder().encode(text).length > cap) return { status: 413, error: "too large" };
  return { text };
}

// A small JSON body from the dashboard: the object, or null.
async function stocksSmallJson(request) {
  try {
    const read = await stocksBody(request, STOCKS_MAX_BODY * 4); // no character takes more than 4 bytes
    if (read.error) return null;
    const text = read.text;
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

// A text for the metadata: no control characters (6 bytes each in JSON), at most limit characters.
function stocksWord(value, limit) {
  return typeof value === "string" ? value.replace(/[\u0000-\u001f\u007f-\u009f]/g, "").slice(0, limit) : null;
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
  const meta = {
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
  // KV refuses metadata over 1024 bytes of JSON: the texts go, the name first, until it fits.
  for (const field of ["name", "generated_at", "data_as_of", "confidence", "stance", "status"]) {
    if (new TextEncoder().encode(JSON.stringify(meta)).length <= STOCKS_META_MAX) break;
    meta[field] = null;
  }
  return meta;
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
    const overrides = stocksOverridesOf(config.overrides, config.tickers);
    return stocksAnswer(200, { version: 1, tickers: config.tickers, model: await stocksModel(env), overrides });
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
  if (ticker && !(await stocksListed(env, ticker))) return stocksAnswer(409, { error: "not in the list" });
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
  // Remove forgets everything: a ticker removed while its body was on the way is not written.
  if (ticker && !(await stocksListed(env, ticker))) return stocksAnswer(409, { error: "not in the list" });
  const what = { overview: "the market overview", ticker: "a ticker analysis", state: "a learning state" }[kind];
  try {
    if (kind === "overview") {
      const generated = stocksWord(doc.generated_at, 32);
      await env.STOCKS_KV.put("stocks:overview", body.text, { metadata: { generated_at: generated } });
    } else if (kind === "ticker") {
      await env.STOCKS_KV.put(`stocks:ticker:${ticker}`, body.text, { metadata: stocksMeta(doc) });
    } else {
      await env.STOCKS_KV.put(`stocks:state:${ticker}`, body.text);
    }
  } catch (err) {
    const code = String((err && err.message) || "").match(/\b[45]\d\d\b/); // only the status, never the key
    console.log(`Stocks: KV did not save ${what}${code ? ` (${code[0]})` : ""}`);
    return stocksAnswer(500, { error: "the document could not be saved" });
  }
  console.log(`Stocks: saved ${what}, ${bytes} bytes`);
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
  if (path === "/stocks/run") return only("GET") || stocksRunAnswer(env);
  if (path === "/stocks/stop") return only("POST") || stocksStop(env);
  return stocksAnswer(404, { error: "not found" });
}

// The claim of the last manual start, stocks:refresh: {requested_at, after_id}.
// after_id is the highest run id GitHub listed when the claim was written (none
// when GitHub could not be read): the run asked for gets a higher one.
async function stocksRefreshInfo(env) {
  const doc = await stocksRead(env, "stocks:refresh");
  const at = doc && typeof doc.requested_at === "string" ? Date.parse(doc.requested_at) : NaN;
  if (!Number.isFinite(at)) return { requested_at: null, available_at: null, wait_ms: 0, after_id: null };
  const available = at + STOCKS_REFRESH_MIN_MS;
  return {
    requested_at: new Date(at).toISOString(),
    available_at: new Date(available).toISOString(),
    wait_ms: Math.max(0, available - Date.now()),
    after_id: Number.isSafeInteger(doc.after_id) && doc.after_id >= 0 ? doc.after_id : null,
  };
}

// The claim as it is stored, or null when there is none.
function stocksClaimRecord(claim) {
  if (!claim || !claim.requested_at) return null;
  return claim.after_id === null || claim.after_id === undefined
    ? { requested_at: claim.requested_at } : { requested_at: claim.requested_at, after_id: claim.after_id };
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
    // Removing forgets everything: the overrides, the analysis and what the ticker learned.
    config.overrides = Object.fromEntries(Object.entries(config.overrides).filter(([k]) => stocksNormalize(k) !== ticker));
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

// Writes the claim record to stocks:refresh, or deletes it when the record is
// null. KV takes one write a second for a key, so a refused write is tried once
// more a second later. True when it was written.
async function stocksRefreshSet(env, record) {
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      if (record) await env.STOCKS_KV.put("stocks:refresh", JSON.stringify(record));
      else await env.STOCKS_KV.delete("stocks:refresh");
      return true;
    } catch {
      if (attempt === 1) await new Promise((resolve) => setTimeout(resolve, 1100));
    }
  }
  return false;
}

// Puts stocks:refresh back as it was before (the whole record, or none) when GitHub did not take the run.
async function stocksRefreshRestore(env, record) {
  if (await stocksRefreshSet(env, record)) return;
  console.log("Stocks: the refresh limit could not be put back, so the next manual run may wait up to 30 minutes");
}

// A time from GitHub as an ISO string, or null. Date.parse turns February 30
// into March 2, so the day and the time must come back as they were sent.
function stocksTime(value) {
  if (typeof value !== "string" || !STOCKS_ISO_TIME.test(value)) return null;
  const at = Date.parse(value);
  if (!Number.isFinite(at)) return null;
  const iso = new Date(at).toISOString();
  return iso.slice(0, 19) === value.slice(0, 19) ? iso : null;
}

// One run from GitHub's list with only what the dashboard needs, each field
// checked: null when the id is not a run id. GitHub's own link and title are
// never passed on: the link is built from GH_REPO and the id, and the title
// only says which kind of run it is.
function stocksRunOf(run, repo) {
  if (!run || typeof run !== "object") return null;
  const id = run.id;
  if (!Number.isSafeInteger(id) || id <= 0) return null;
  // A status GitHub may add later counts as not going, so it never holds the button for good.
  const status = STOCKS_RUN_STATUSES.includes(run.status) ? run.status : "unknown";
  const conclusion = STOCKS_RUN_CONCLUSIONS.includes(run.conclusion) ? run.conclusion : null;
  const title = typeof run.display_title === "string" ? run.display_title : "";
  return {
    id,
    status,
    conclusion,
    kind: Object.hasOwn(STOCKS_RUN_KINDS, title) ? STOCKS_RUN_KINDS[title] : "unknown",
    created_at: stocksTime(run.created_at),
    updated_at: stocksTime(run.updated_at),
    started_at: stocksTime(run.run_started_at),
    url: `https://github.com/${repo}/actions/runs/${id}`,
  };
}

// The recent runs of stocks.yml on main, newest first, or null when GitHub
// cannot be read (no answer, not 2xx, too large, not JSON). The token goes to
// api.github.com only.
async function stocksRuns(env) {
  const repo = String(env.GH_REPO);
  const url = `https://api.github.com/repos/${repo}/actions/workflows/${STOCKS_WORKFLOW}/runs`
    + `?branch=${STOCKS_BRANCH}&per_page=20`;
  try {
    const response = await fetch(url, { method: "GET", headers: stocksHeaders(env) });
    if (!response.ok) {
      if (response.body) response.body.cancel().catch(() => {});
      return null;
    }
    const read = await stocksBody(response, STOCKS_RUNS_MAX_BYTES);
    if (read.error) return null;
    const doc = JSON.parse(read.text);
    if (!doc || typeof doc !== "object" || !Array.isArray(doc.workflow_runs)) return null;
    const runs = doc.workflow_runs.slice(0, 100).map((run) => stocksRunOf(run, repo)).filter(Boolean);
    const at = (run) => (run.created_at ? Date.parse(run.created_at) : -Infinity);
    return runs.sort((a, b) => (at(b) - at(a)) || (b.id - a.id));
  } catch {
    return null;
  }
}

// The one view of the runs that GET /stocks/run, POST /stocks/stop and POST
// /stocks/refresh share. claim is the stocks:refresh record.
//   GitHub can be read: active when a run is queued or going. A claim younger
//     than STOCKS_START_GRACE_MS with the run it asked for not listed yet is a
//     run still being created (starting). can_start when neither. GitHub's run
//     ids only grow, so the run asked for is one with an id above the claim's
//     after_id. A run that ended a moment before the click is not it. Only a
//     claim without after_id (written while GitHub could not be read) goes by
//     the time: a run created since the claim, less the clock skew.
//   GitHub cannot be read, or GH_REPO or the token is missing (checked false):
//     the 30 minute rule of the claim alone.
async function stocksRunState(env) {
  const claim = await stocksRefreshInfo(env);
  const ready = stocksRepoReady(env);
  const runs = ready ? await stocksRuns(env) : null;
  if (!runs) {
    return {
      checked: false, reason: ready ? "GitHub could not be read" : "not set up", active: false,
      can_start: claim.wait_ms <= 0, available_at: claim.wait_ms > 0 ? claim.available_at : null, wait_ms: claim.wait_ms,
      starting: false, run: null, runs: [], active_runs: [], claim,
    };
  }
  const now = Date.now();
  const claimAt = claim.requested_at ? Date.parse(claim.requested_at) : null;
  const age = claimAt === null ? null : now - claimAt;
  const listed = claim.after_id !== null
    ? runs.some((run) => run.id > claim.after_id)
    : runs.some((run) => run.created_at && Date.parse(run.created_at) >= claimAt - STOCKS_CLOCK_SKEW_MS);
  const starting = age !== null && age < STOCKS_START_GRACE_MS && age > -STOCKS_CLOCK_SKEW_MS && !listed;
  const activeRuns = runs.filter((run) => STOCKS_RUN_ACTIVE.includes(run.status));
  const graceEnd = claimAt === null ? now : claimAt + STOCKS_START_GRACE_MS;
  return {
    checked: true, reason: null, active: activeRuns.length > 0, can_start: !activeRuns.length && !starting,
    available_at: starting ? new Date(graceEnd).toISOString() : null, wait_ms: starting ? Math.max(0, graceEnd - now) : 0,
    starting, run: runs[0] || null, runs, active_runs: activeRuns, claim,
  };
}

async function stocksRunAnswer(env) {
  const state = await stocksRunState(env);
  const body = { checked: state.checked };
  if (!state.checked) body.reason = state.reason;
  return stocksAnswer(200, {
    ...body, active: state.active, can_start: state.can_start, available_at: state.available_at,
    requested_at: state.claim.requested_at, run: state.run,
  });
}

// Asks GitHub to cancel one run. { ok } when it took the request (202) or the
// run is already ending or over (409); otherwise what went wrong.
async function stocksCancel(repo, id, env) {
  const url = `https://api.github.com/repos/${repo}/actions/runs/${id}/cancel`;
  try {
    const response = await fetch(url, { method: "POST", headers: stocksHeaders(env) });
    if (response.status === 202 || response.status === 409) return { ok: true, status: response.status };
    return { ok: false, problem: `GitHub answered ${response.status} ${await stocksErrorDetail(response)}`.trim() };
  } catch (err) {
    return { ok: false, problem: `GitHub could not be reached (${err && err.name ? err.name : "error"})` };
  }
}

// Stops every stocks.yml run that is queued or going, then clears the claim so
// Run analysis now is free once GitHub has stopped them.
async function stocksStop(env) {
  if (!stocksRepoReady(env)) {
    console.log("Stocks: set GH_REPO (owner/name) and the GH_DISPATCH_TOKEN secret on this Worker");
    return stocksAnswer(500, { error: "this worker cannot stop runs: set GH_REPO and GH_DISPATCH_TOKEN" });
  }
  const state = await stocksRunState(env);
  if (!state.checked) {
    console.log("Stocks: stop asked, but the list of runs could not be read, so nothing was stopped");
    return stocksAnswer(502, { error: "GitHub could not be read, so nothing was stopped" });
  }
  if (!state.active_runs.length && state.starting) {
    return stocksAnswer(409, {
      error: "the run is still starting, try again in a minute",
      retry_after_seconds: Math.max(1, Math.ceil(state.wait_ms / 1000)),
    });
  }
  const repo = String(env.GH_REPO);
  let stopped = 0;
  let problem = "";
  for (const run of state.active_runs) {
    const result = await stocksCancel(repo, run.id, env);
    if (result.ok) stopped += 1;
    else if (!problem) problem = result.problem;
  }
  const runs = (n) => `${n} ${n === 1 ? "run" : "runs"}`;
  if (problem) {
    // The claim stays: a run that is still going keeps Run analysis now off.
    console.log(`Stocks: stop asked, ${stopped} of ${runs(state.active_runs.length)} cancelled: ${problem}`);
    return stocksAnswer(502, { error: `GitHub did not stop the run: ${problem}`, stopped });
  }
  if (state.claim.requested_at && !(await stocksRefreshSet(env, null))) {
    console.log("Stocks: the refresh limit could not be cleared after the stop, so the next manual run may wait up to 30 minutes");
  }
  console.log(`Stocks: stop asked, ${runs(stopped)} cancelled`);
  return stocksAnswer(200, { ok: true, stopped });
}

async function stocksRefresh(env) {
  const state = await stocksRunState(env);
  if (state.checked && state.active) return stocksAnswer(409, { error: "a run is going", active: true });
  if (!state.can_start) {
    return stocksAnswer(429, { error: "a run was started recently", retry_after_seconds: Math.max(1, Math.ceil(state.wait_ms / 1000)) });
  }
  if (!stocksRepoReady(env)) {
    console.log("Stocks: set GH_REPO (owner/name) and the GH_DISPATCH_TOKEN secret on this Worker");
    return stocksAnswer(500, { error: "this worker cannot start runs: set GH_REPO and GH_DISPATCH_TOKEN" });
  }
  const repo = String(env.GH_REPO);
  const hold = state.checked ? STOCKS_START_GRACE_MS : STOCKS_REFRESH_MIN_MS;
  // A second request that came in while GitHub's list was read has written its
  // claim by now: it starts the run, this one is refused.
  const latest = await stocksRefreshInfo(env);
  if (latest.requested_at && latest.requested_at !== state.claim.requested_at) {
    const left = Date.parse(latest.requested_at) + hold - Date.now();
    return stocksAnswer(429, { error: "a run was started recently", retry_after_seconds: Math.max(1, Math.ceil(left / 1000)) });
  }
  // The record is written before GitHub is asked, so a second request while
  // GitHub answers is refused; it is put back as it was if GitHub does not take the run.
  // It keeps the highest run id GitHub listed just now: the run asked for gets a higher one.
  const now = Date.now();
  const requested = new Date(now).toISOString();
  const record = { requested_at: requested };
  if (state.checked) record.after_id = state.runs.reduce((max, run) => Math.max(max, run.id), 0);
  await env.STOCKS_KV.put("stocks:refresh", JSON.stringify(record));
  let result = await stocksDispatch(repo, "manual", env);
  if (!result.ok && result.unsure) result = await stocksDispatch(repo, "manual", env);
  if (!result.ok) {
    await stocksRefreshRestore(env, stocksClaimRecord(state.claim));
    console.log(`Stocks: the manual run did not start: ${result.problem}`);
    return stocksAnswer(502, { error: `the run did not start: ${result.problem}` });
  }
  console.log(`Stocks: started a manual run, GitHub answered ${result.status}`);
  return stocksAnswer(202, { ok: true, requested_at: requested, available_at: new Date(now + hold).toISOString() });
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
