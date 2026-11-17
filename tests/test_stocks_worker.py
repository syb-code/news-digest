"""Offline checks of worker/stocks.js, the private stocks block of the Worker.

  static     a block to paste (no export, no name shared with the other
             blocks), the settings allowlist equals Python's, the header
             names the setup steps, plain punctuation, no key or ticker in a
             log line.
  workerd    worker/worker.js run in workerd through miniflare, with a real
             KV binding (STOCKS_KV) and GitHub replaced by a second local
             worker: every route and status of contract section 11, both
             keys, the size caps, the ticker check, the 30 ticker limit,
             remove forgets the state and the analysis, the refresh limit,
             and the cron at 05:45 (both workflows) and 12:00 (daily only).
             Also the races and limits of the security review: two refreshes
             together, a remove during a slow upload, metadata over 1024
             bytes, bodies that never end, the overrides of a removed ticker
             and Python's spaces in a ticker. Skipped when node or miniflare
             is missing.
  block      stocks.js's functions run in node with a KV that refuses writes:
             a refused save answers 500 with a safe log line, and the refresh
             record is put back on the second try. Skipped without node.
  client     worker_client.py's answer parsing of a deeply nested state.

Tickers here are public examples (AAPL, BRK-B, MSFT) or made up.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import stocks_support as ss  # puts scripts/ on sys.path

import stocks

WORKER_DIR = os.path.join(ss.ROOT, "worker")
STOCKS_JS = os.path.join(WORKER_DIR, "stocks.js")
WORKER_JS = os.path.join(WORKER_DIR, "worker.js")
ACCESS = "access-key-for-the-dashboard-0123456789"
INGEST = "ingest-key-for-the-github-job-0123456789"
TOKEN = "github-token-for-tests"
PAGES = "https://owner.github.io"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def find_miniflare():
    """The miniflare package folder: $STOCKS_TEST_MINIFLARE, the build sandbox's copy, or npm's global root."""
    candidates = [os.environ.get("STOCKS_TEST_MINIFLARE", ""),
                  "/tmp/claude-0/final-scratch/wd/node_modules/miniflare"]
    if shutil.which("npm"):
        try:
            root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=20).stdout.strip()
            candidates.append(os.path.join(root, "miniflare"))
        except (OSError, subprocess.SubprocessError):
            pass
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "package.json")):
            return c
    return None


class StaticTests(unittest.TestCase):
    def setUp(self):
        self.text = read(STOCKS_JS)

    def test_a_block_to_paste(self):
        self.assertNotRegex(self.text, r"(?m)^\s*export\b")
        for name in ("function isStocksIngest(pathname)", "function isStocksPath(pathname)",
                     "async function handleStocksIngest(request, env, ctx)",
                     "async function handleStocks(request, env, ctx)",
                     "async function routeKeyHolder(request, env, ctx)", "function routeWorker(request, env, ctx)",
                     "async function dispatchStocks(event, env, ctx)", "async function scheduledAll(event, env, ctx)"):
            self.assertIn(name, self.text)
        self.assertIn("await keyMatches(given, key)", self.text)  # constant time, from auth-guard.js
        self.assertIn("protect(routeKeyHolder)", self.text)
        self.assertIn("digestLocalTime(event.scheduledTime)", self.text)
        self.assertIn("const STOCKS_REFRESH_MIN_MS = 30 * 60 * 1000;", self.text)
        self.assertIn('const STOCKS_WORKFLOW = "stocks.yml";', self.text)

    def test_the_header_gives_the_setup_and_the_last_lines(self):
        header = self.text.split("const STOCKS_MODELS")[0]
        for needed in ("STOCKS_KV", "STOCKS_INGEST_KEY", "openssl rand -base64 32", "kv namespace create",
                       "STOCKS_ENABLED", "GH_DISPATCH_TOKEN", "GH_REPO"):
            self.assertIn(needed, header)
        lines = re.findall(r"(?m)^//\s+(export default \{.*\};)$", header)
        self.assertEqual(lines[0], "export default { fetch: routeWorker, scheduled: scheduledAll };")
        self.assertIn("isStocksIngest(new URL(request.url).pathname) ? handleStocksIngest(request, env, ctx) : protect(",
                      lines[1])
        self.assertIn("scheduled: scheduledAll }", lines[1])

    def test_models_match_python_and_config(self):
        import config

        js = json.loads(re.search(r"const STOCKS_MODELS = (\[.*?\]);", self.text).group(1))
        self.assertEqual(tuple(js), stocks.ALLOWED_MODELS)
        self.assertEqual(tuple(config.load_config(check=("stocks",))["stocks"]["models"]), stocks.ALLOWED_MODELS)
        self.assertIn(f'const STOCKS_DEFAULT_MODEL = "{stocks.DEFAULT_MODEL}";', self.text)
        self.assertIn(f"const STOCKS_MAX_TICKERS = {stocks.MAX_TICKERS};", self.text)
        js_re = re.search(r"const STOCKS_TICKER = /(.*?)/;", self.text).group(1)
        self.assertEqual(js_re, stocks.TICKER_RE.pattern)
        caps = re.search(r"const STOCKS_CAPS = \{ config: (\d+), settings: (\d+), overview: (\d+), ticker: (\d+), "
                         r"state: (\d+) \};", self.text).groups()
        self.assertEqual([int(c) for c in caps], [stocks.CONFIG_MAX_BYTES, 4096, stocks.OVERVIEW_MAX_BYTES,
                                                  stocks.DOC_MAX_BYTES, stocks.STATE_MAX_BYTES])

    def test_logs_hold_no_ticker_key_or_body(self):
        messages = re.findall(r"(?:console\.log|new Error)\(\s*[`\"]([^`\"]*)", self.text)
        self.assertGreaterEqual(len(messages), 8)
        for m in messages:
            self.assertTrue(m.startswith("Stocks: "), m)
            for leak in ("${ticker}", "${key}", "${body", "${given}", "${raw}", "TOKEN}", "${doc"):
                self.assertNotIn(leak, m)
        for line in self.text.splitlines():
            if "console." in line or "new Error(" in line:
                self.assertNotIn("headers", line)

    def test_plain_punctuation(self):
        for text in (self.text, read(WORKER_JS)):
            self.assertNotIn(chr(0x2014), text)
            self.assertNotIn(chr(0x2013), text)


HARNESS = r"""
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
const require = createRequire(import.meta.url);
const { Miniflare } = require(process.argv[2]);
const script = readFileSync(process.argv[3], "utf8");
const plan = JSON.parse(readFileSync(process.argv[4], "utf8"));
// GitHub: every request is logged. The list of stocks.yml runs answers the
// case's runs (a list of runs, {status} for an error, {raw} for a body as it
// is); a cancel answers the case's cancels[id], 202 by default; any other
// request takes the next of the case's answers, 204 by default. One request at
// a time touches the log and the answers, so requests sent together lose none.
const GITHUB = `let chain = Promise.resolve();
function locked(fn) { const p = chain.then(fn); chain = p.catch(() => {}); return p; }
export default { async fetch(request, env) {
  const body = request.method === "GET" ? null : await request.text();
  const path = new URL(request.url).pathname;
  const isList = request.method === "GET" && path.endsWith("/actions/workflows/stocks.yml/runs");
  const cancelId = request.method === "POST" && path.endsWith("/cancel") ? path.split("/").at(-2) : null;
  const answer = await locked(async () => {
    const log = JSON.parse((await env.LOG.get("log")) || "[]");
    log.push({ url: request.url, method: request.method, auth: request.headers.get("authorization"),
               agent: request.headers.get("user-agent"), body });
    await env.LOG.put("log", JSON.stringify(log));
    if (isList || cancelId) return null;
    const answers = JSON.parse((await env.LOG.get("answers")) || "[]");
    const next = answers.length ? answers.shift() : 204;
    await env.LOG.put("answers", JSON.stringify(answers));
    return next;
  });
  if (isList) {
    const runs = JSON.parse((await env.LOG.get("runs")) || "[]");
    if (Array.isArray(runs)) return new Response(JSON.stringify({ total_count: runs.length, workflow_runs: runs }), { status: 200 });
    if (runs.raw !== undefined) return new Response(runs.raw, { status: 200 });
    return new Response(JSON.stringify({ message: "Server said " + runs.status }), { status: runs.status });
  }
  if (cancelId) {
    const status = JSON.parse((await env.LOG.get("cancels")) || "{}")[cancelId] || 202;
    return new Response(JSON.stringify(status === 202 ? {} : { message: "Server said " + status }), { status });
  }
  if (typeof answer === "object" && answer.delay) await new Promise((resolve) => setTimeout(resolve, answer.delay));
  if (typeof answer === "object") return new Response(JSON.stringify(answer.body), { status: answer.status });
  return new Response(answer === 204 ? null : JSON.stringify({ message: "Server said " + answer }), { status: answer });
} };`;
// "ago:N" anywhere in a seed or a list of runs is the time N seconds before the case runs.
const when = (v) => (typeof v === "string" && /^ago:-?\d+$/.test(v) ? new Date(Date.now() - Number(v.slice(4)) * 1000).toISOString()
  : Array.isArray(v) ? v.map(when)
  : v && typeof v === "object" ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, when(x)])) : v);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// A body sent slowly ({first, wait}: the first bytes, a pause, the rest) or
// one that sends n bytes and then stays open.
function streamed(c, text) {
  const bytes = new TextEncoder().encode(text);
  return new ReadableStream({
    async start(controller) {
      if (c.open) {
        controller.enqueue(new Uint8Array(c.open).fill(120));
        return;
      }
      controller.enqueue(bytes.slice(0, c.slow.first));
      await sleep(c.slow.wait);
      controller.enqueue(bytes.slice(c.slow.first));
      controller.close();
    },
  });
}
async function send(mf, c) {
  const seen = {};
  const headers = { ...(c.headers || {}) };
  const init = { method: c.method || "GET", headers };
  if (c.body !== undefined) init.body = typeof c.body === "string" ? c.body : JSON.stringify(c.body);
  if (c.repeat) init.body = "x".repeat(c.repeat);
  if (c.slow || c.open) {
    init.body = streamed(c, init.body || "");
    init.duplex = "half";
  }
  const pending = mf.dispatchFetch("https://worker.example" + c.path, init);
  pending.catch(() => {});
  const r = await Promise.race([pending, sleep(c.timeout || 60000).then(() => null)]);
  if (!r) return { status: "no answer" };
  seen.status = r.status;
  seen.guard = r.headers.get("x-guard");
  seen.cors = r.headers.get("access-control-allow-origin");
  seen.cache = r.headers.get("cache-control");
  const text = await r.text();
  try { seen.body = JSON.parse(text); } catch { seen.body = text; }
  return seen;
}
const out = {};
for (const inst of plan) {
  const lines = [];
  const mf = new Miniflare({
    log: undefined,
    handleRuntimeStdio(stdout, stderr) {
      for (const s of [stdout, stderr]) s.on("data", (d) => lines.push(...String(d).split("\n").filter(Boolean)));
    },
    workers: [
      { name: "main", modules: true, script, compatibilityDate: "2024-09-23", bindings: inst.bindings,
        kvNamespaces: inst.kv ? { STOCKS_KV: "stocks" } : [], outboundService: "github" },
      { name: "github", modules: true, script: GITHUB, compatibilityDate: "2024-09-23", kvNamespaces: { LOG: "log" } },
    ],
  });
  const log = await mf.getKVNamespace("LOG", "github");
  const kv = inst.kv ? await mf.getKVNamespace("STOCKS_KV", "main") : null;
  for (const c of inst.cases) {
    await log.put("log", "[]");
    await log.put("answers", JSON.stringify(c.answers || []));
    await log.put("runs", JSON.stringify(c.runs === undefined ? [] : when(c.runs)));
    await log.put("cancels", JSON.stringify(c.cancels || {}));
    for (const [k, v] of Object.entries(c.seed || {})) {
      if (v === null) await kv.delete(k);
      else await kv.put(k, JSON.stringify(when(v)));
    }
    let seen = {};
    if (c.scheduled) {
      const worker = await mf.getWorker("main");
      const res = await worker.scheduled({ scheduledTime: new Date(c.scheduled), cron: "45 12,13 * * *" });
      seen.outcome = res.outcome;
    } else if (c.together) {
      // Requests sent at the same time, each after its own delay in ms.
      seen.answers = await Promise.all(c.together.map(async (q) => {
        await sleep(q.after || 0);
        return send(mf, q);
      }));
    } else {
      seen = await send(mf, c);
    }
    seen.github = JSON.parse((await log.get("log")) || "[]");
    if (kv && c.keys) {
      seen.kv = {};
      for (const k of c.keys) {
        const got = await kv.getWithMetadata(k);
        seen.kv[k] = got.value === null ? null : { value: JSON.parse(got.value), metadata: got.metadata };
      }
    }
    out[`${inst.name}: ${c.name}`] = seen;
  }
  await new Promise((resolve) => setTimeout(resolve, 200));
  out[`${inst.name}: logs`] = lines;
  await mf.dispose();
}
console.log(JSON.stringify(out));
"""


def key(k=ACCESS, origin=PAGES):
    h = {"authorization": f"Bearer {k}"}
    if origin:
        h["origin"] = origin
    return h


def ingest(k=INGEST):
    return {"authorization": f"Bearer {k}"}


def state(ticker, **extra):
    doc = {"kind": "stocks.state", "version": 1, "ticker": ticker, "lessons": [], "predictions": []}
    doc.update(extra)
    return doc


def analysis(ticker):
    return {"kind": "stocks.ticker", "version": 1, "ticker": ticker, "status": "ok", "notes": [],
            "header": {"ticker": ticker, "name": "Apple Inc." + " long name" * 10, "last_close": 270.5,
                       "change_pct": 1.25},
            "assessment": {"stance": "bullish", "confidence": "medium"}, "data_as_of": "2026-11-12",
            "learner": {"scorecard": {"live": {"5": {"n": 3, "hit_rate": 1.0}, "20": {"n": 0, "hit_rate": None}},
                                      "backtest": {"5": {"n": 442, "hit_rate": 0.5045},
                                                   "20": {"n": 412, "hit_rate": 0.4709}}}},
            "generated_at": "2026-11-13T13:58:02+00:00", "disclaimer": stocks.DISCLAIMER}


FULL = {"ACCESS_KEY": ACCESS, "ALLOWED_ORIGINS": PAGES, "STOCKS_INGEST_KEY": INGEST, "GH_REPO": "owner/news-digest",
        "GH_DISPATCH_TOKEN": TOKEN}
MANY = [f"Z{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(29)]  # made-up symbols
OLD_RUN = "2026-01-02T03:04:05.000Z"
TINY = -1.2345678901234567e-300  # 24 characters in JSON
CONTROLS = chr(1) * 60  # 6 bytes a character in JSON
LONE = chr(0xD800) * 60  # a lone surrogate: also 6 bytes a character in JSON, and not removed
# Python's str.strip() spaces, and two characters it keeps (U+FEFF, U+200B) and NUL.
SPELLINGS = [chr(c) for c in range(0x10000) if chr(c).isspace()] + [chr(0xFEFF), chr(0x200B), chr(0)]
OVERRIDES = {"AAPL": {"names": ["Apple"]}, "aapl": {"cusips": ["037833100"]}, "msft": {"names": ["Microsoft"]},
             "ZZZQ": {"names": ["Made up"]}}


REPO_RUNS = "https://github.com/owner/news-digest/actions/runs/"
LIST_URL = "https://api.github.com/repos/owner/news-digest/actions/workflows/stocks.yml/runs?branch=main&per_page=20"
HOSTILE_URL = "https://evil.example/owner/news-digest/actions/runs/41"


def gh_run(id_, status, conclusion=None, ago=60, title="Stocks (manual)", **extra):
    """One run as GitHub's list shows it, created `ago` seconds before the case runs."""
    run = {"id": id_, "name": "Stocks", "status": status, "conclusion": conclusion, "event": "workflow_dispatch",
           "display_title": title, "created_at": f"ago:{ago}", "updated_at": f"ago:{max(ago - 30, 0)}",
           "run_started_at": f"ago:{ago}", "html_url": REPO_RUNS + str(id_), "head_branch": "main"}
    run.update(extra)
    return run


# Runs that must not reach the page as they are: GitHub's own link and a made-up
# title, a status and a conclusion outside the lists, times that are not times,
# and ids that are not run ids (those runs are dropped).
HOSTILE = [gh_run(41, "hacking", "pwned", title="Stocks (manual) <img src=x onerror=alert(1)>",
                  html_url=HOSTILE_URL, created_at="yesterday", updated_at=12345, run_started_at=None),
           gh_run("42", "queued"), gh_run(-43, "queued"), gh_run(44.5, "queued"), gh_run(2 ** 60, "queued"),
           gh_run(True, "queued"), "not a run", None]


def heavy(ticker, text):
    """An analysis whose every metadata text is `text`, with the longest numbers."""
    return {"kind": "stocks.ticker", "version": 1, "ticker": ticker, "status": text, "data_as_of": text,
            "generated_at": text, "header": {"name": text, "last_close": TINY, "change_pct": TINY},
            "assessment": {"stance": text, "confidence": text},
            "learner": {"scorecard": {"backtest": {"999": {"n": 10 ** 21, "hit_rate": TINY}}}}}

PLAN = [
    {"name": "full", "kv": True, "bindings": FULL, "cases": [
        {"name": "ingest, no key", "path": "/stocks/ingest/config"},
        {"name": "ingest, from a browser", "path": "/stocks/ingest/config", "headers": {**ingest(), "origin": PAGES}},
        {"name": "ingest, wrong key", "path": "/stocks/ingest/config", "headers": ingest("wrong")},
        {"name": "ingest, the access key", "path": "/stocks/ingest/config", "headers": ingest(ACCESS)},
        {"name": "ingest config, empty", "path": "/stocks/ingest/config", "headers": ingest()},
        {"name": "list, no key", "path": "/stocks/list", "headers": {"origin": PAGES}},
        {"name": "list, the ingest key", "path": "/stocks/list", "headers": key(INGEST)},
        {"name": "list, empty", "path": "/stocks/list", "headers": key()},
        {"name": "add", "method": "POST", "path": "/stocks/tickers", "headers": key(),
         "body": {"action": "add", "ticker": " aapl "}},
        {"name": "add a class share", "method": "POST", "path": "/stocks/tickers", "headers": key(),
         "body": {"action": "add", "ticker": "brk.b"}},
        {"name": "add again", "method": "POST", "path": "/stocks/tickers", "headers": key(),
         "body": {"action": "add", "ticker": "AAPL"}},
        {"name": "add, bad ticker", "method": "POST", "path": "/stocks/tickers", "headers": key(),
         "body": {"action": "add", "ticker": "not a ticker"}},
        {"name": "add, bad action", "method": "POST", "path": "/stocks/tickers", "headers": key(),
         "body": {"action": "delete", "ticker": "AAPL"}},
        {"name": "add, too long", "method": "POST", "path": "/stocks/tickers", "headers": key(),
         "body": {"action": "add", "ticker": "AAPL", "pad": "x" * 2100}},
        {"name": "add, not json", "method": "POST", "path": "/stocks/tickers", "headers": key(), "body": "{oops"},
        {"name": "analysis before the run", "path": "/stocks/t/AAPL", "headers": key()},
        {"name": "analysis, not listed", "path": "/stocks/t/MSFT", "headers": key()},
        {"name": "analysis, bad ticker", "path": "/stocks/t/12345", "headers": key()},
        {"name": "overview before the run", "path": "/stocks/overview", "headers": key()},
        {"name": "ingest state, none yet", "path": "/stocks/ingest/state/AAPL", "headers": ingest()},
        {"name": "ingest state, not listed", "path": "/stocks/ingest/state/MSFT", "headers": ingest()},
        {"name": "ingest state, lower case", "path": "/stocks/ingest/state/aapl", "headers": ingest()},
        {"name": "put state", "method": "PUT", "path": "/stocks/ingest/state/AAPL", "headers": ingest(),
         "body": state("AAPL", lessons=[{"date": "2026-11-12", "text": "A lesson."}]), "keys": ["stocks:state:AAPL"]},
        {"name": "put state, class share", "method": "PUT", "path": "/stocks/ingest/state/BRK-B", "headers": ingest(),
         "body": state("BRK-B")},
        {"name": "put state, another ticker inside", "method": "PUT", "path": "/stocks/ingest/state/AAPL",
         "headers": ingest(), "body": state("BRK-B")},
        {"name": "put state, wrong kind", "method": "PUT", "path": "/stocks/ingest/state/AAPL", "headers": ingest(),
         "body": {**state("AAPL"), "kind": "stocks.ticker"}},
        {"name": "put state, wrong version", "method": "PUT", "path": "/stocks/ingest/state/AAPL",
         "headers": ingest(), "body": {**state("AAPL"), "version": 2}},
        {"name": "put state, not json", "method": "PUT", "path": "/stocks/ingest/state/AAPL", "headers": ingest(),
         "body": "{oops"},
        {"name": "put state, a list", "method": "PUT", "path": "/stocks/ingest/state/AAPL", "headers": ingest(),
         "body": [1, 2]},
        {"name": "put state, not listed", "method": "PUT", "path": "/stocks/ingest/state/MSFT", "headers": ingest(),
         "body": state("MSFT")},
        {"name": "put state, too large", "method": "PUT", "path": "/stocks/ingest/state/AAPL", "headers": ingest(),
         "repeat": stocks.STATE_MAX_BYTES + 1},
        {"name": "put analysis", "method": "PUT", "path": "/stocks/ingest/ticker/AAPL", "headers": ingest(),
         "body": analysis("AAPL"), "keys": ["stocks:ticker:AAPL"]},
        {"name": "put analysis, too large", "method": "PUT", "path": "/stocks/ingest/ticker/AAPL",
         "headers": ingest(), "repeat": stocks.DOC_MAX_BYTES + 1},
        {"name": "put overview", "method": "PUT", "path": "/stocks/ingest/overview", "headers": ingest(),
         "body": {**ss.example_overview()}},
        {"name": "put overview, wrong kind", "method": "PUT", "path": "/stocks/ingest/overview", "headers": ingest(),
         "body": {"kind": "stocks.state", "version": 1}},
        {"name": "put overview, too large", "method": "PUT", "path": "/stocks/ingest/overview", "headers": ingest(),
         "repeat": stocks.OVERVIEW_MAX_BYTES + 1},
        {"name": "ingest state, saved", "path": "/stocks/ingest/state/AAPL", "headers": ingest()},
        {"name": "list, after the run", "path": "/stocks/list", "headers": key()},
        {"name": "analysis", "path": "/stocks/t/AAPL", "headers": key()},
        {"name": "analysis, dot spelling", "path": "/stocks/t/brk.b", "headers": key()},
        {"name": "overview", "path": "/stocks/overview", "headers": key()},
        {"name": "settings", "path": "/stocks/settings", "headers": key()},
        {"name": "settings, haiku", "method": "POST", "path": "/stocks/settings", "headers": key(),
         "body": {"model": "claude-haiku-5-5"}},
        {"name": "settings, not allowed", "method": "POST", "path": "/stocks/settings", "headers": key(),
         "body": {"model": "claude-fable-5-2"}},
        {"name": "ingest config, after edits", "path": "/stocks/ingest/config", "headers": ingest()},
        {"name": "refresh", "method": "POST", "path": "/stocks/refresh", "headers": key()},
        {"name": "refresh again", "method": "POST", "path": "/stocks/refresh", "headers": key()},
        {"name": "remove", "method": "POST", "path": "/stocks/tickers", "headers": key(),
         "body": {"action": "remove", "ticker": "AAPL"}, "keys": ["stocks:state:AAPL", "stocks:ticker:AAPL",
                                                                   "stocks:config"]},
        {"name": "remove again", "method": "POST", "path": "/stocks/tickers", "headers": key(),
         "body": {"action": "remove", "ticker": "AAPL"}},
        *[{"name": f"fill {t}", "method": "POST", "path": "/stocks/tickers", "headers": key(),
           "body": {"action": "add", "ticker": t}} for t in MANY + ["ZZZ"]],
        {"name": "list, wrong method", "method": "POST", "path": "/stocks/list", "headers": key(), "body": {}},
        {"name": "tickers, wrong method", "path": "/stocks/tickers", "headers": key()},
        {"name": "ingest config, wrong method", "method": "PUT", "path": "/stocks/ingest/config",
         "headers": ingest(), "body": {}},
        {"name": "unknown stocks path", "path": "/stocks/nothing", "headers": key()},
        {"name": "unknown ingest path", "path": "/stocks/ingest/nothing", "headers": ingest()},
        {"name": "summaries", "path": "/analyze", "method": "POST", "headers": key(), "body": {}},
        {"name": "x accounts still work", "method": "POST", "path": "/x-accounts", "headers": key(),
         "body": {"action": "add", "handle": "NASA"}},
        {"name": "cron 05:45 summer", "scheduled": "2026-07-15T12:45:00Z"},
        {"name": "cron 05:45 winter", "scheduled": "2026-01-15T13:45:00Z"},
        {"name": "cron noon", "scheduled": "2026-01-15T20:00:00Z"},
        {"name": "cron off time", "scheduled": "2026-07-15T13:45:00Z"},
        {"name": "cron, github refuses both", "scheduled": "2026-07-15T12:45:00Z", "answers": [404, 404]},
    ]},
    {"name": "no kv", "kv": False, "bindings": FULL, "cases": [
        {"name": "list", "path": "/stocks/list", "headers": key()},
        {"name": "ingest config", "path": "/stocks/ingest/config", "headers": ingest()},
        {"name": "cron 05:45", "scheduled": "2026-07-15T12:45:00Z"},
    ]},
    {"name": "no ingest key", "kv": True, "bindings": {k: v for k, v in FULL.items() if k != "STOCKS_INGEST_KEY"},
     "cases": [{"name": "ingest config", "path": "/stocks/ingest/config", "headers": ingest()}]},
    {"name": "short ingest key", "kv": True, "bindings": {**FULL, "STOCKS_INGEST_KEY": "short-key"},
     "cases": [{"name": "ingest config", "path": "/stocks/ingest/config", "headers": ingest("short-key")}]},
    {"name": "ingest key equals access key", "kv": True, "bindings": {**FULL, "STOCKS_INGEST_KEY": ACCESS},
     "cases": [{"name": "ingest config", "path": "/stocks/ingest/config", "headers": ingest(ACCESS)}]},
    {"name": "no repo", "kv": True, "bindings": {k: v for k, v in FULL.items() if k != "GH_REPO"}, "cases": [
        {"name": "refresh", "method": "POST", "path": "/stocks/refresh", "headers": key()},
        {"name": "cron 05:45", "scheduled": "2026-07-15T12:45:00Z"},
        {"name": "run", "path": "/stocks/run", "headers": key(), "runs": [gh_run(1, "in_progress")]},
        {"name": "run, a claim", "path": "/stocks/run", "headers": key(), "seed": {"stocks:refresh": {"requested_at": "ago:300"}}},
        {"name": "stop", "method": "POST", "path": "/stocks/stop", "headers": key(), "keys": ["stocks:refresh"]},
    ]},
    {"name": "no token", "kv": True, "bindings": {k: v for k, v in FULL.items() if k != "GH_DISPATCH_TOKEN"}, "cases": [
        {"name": "run", "path": "/stocks/run", "headers": key()},
        {"name": "stop", "method": "POST", "path": "/stocks/stop", "headers": key()},
    ]},
    {"name": "runs", "kv": True, "bindings": FULL, "cases": [
        # GET /stocks/run in each state
        {"name": "run, no key", "path": "/stocks/run", "headers": {"origin": PAGES}},
        {"name": "run, the ingest key", "path": "/stocks/run", "headers": key(INGEST)},
        {"name": "run, wrong method", "method": "POST", "path": "/stocks/run", "headers": key(), "body": {}},
        {"name": "run, none", "path": "/stocks/run", "headers": key(), "seed": {"stocks:refresh": None}},
        {"name": "run, queued", "path": "/stocks/run", "headers": key(), "runs": [gh_run(11, "queued", ago=20)]},
        {"name": "run, in progress", "path": "/stocks/run", "headers": key(),
         "runs": [gh_run(10, "completed", "success", ago=86400, title="Stocks (scheduled)"),
                  gh_run(12, "in_progress", ago=300, title="Stocks (scheduled)")]},
        {"name": "run, failed", "path": "/stocks/run", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:100"}}, "runs": [gh_run(13, "completed", "failure", ago=90)]},
        {"name": "run, cancelled", "path": "/stocks/run", "headers": key(),
         "runs": [gh_run(14, "completed", "cancelled", ago=200)]},
        {"name": "run, starting", "path": "/stocks/run", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:20"}}, "runs": []},
        {"name": "run, starting with an older run listed", "path": "/stocks/run", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:20"}},
         "runs": [gh_run(15, "completed", "success", ago=3600, title="Stocks (scheduled)")]},
        {"name": "run, listed within the clock skew", "path": "/stocks/run", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:20"}}, "runs": [gh_run(16, "queued", ago=50)]},
        {"name": "run, unreadable", "path": "/stocks/run", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:60"}}, "runs": {"status": 500}},
        {"name": "run, bad json", "path": "/stocks/run", "headers": key(), "seed": {"stocks:refresh": None},
         "runs": {"raw": "{not json"}},
        {"name": "run, no list in the answer", "path": "/stocks/run", "headers": key(), "runs": {"raw": '{"total_count": 1}'}},
        {"name": "run, hostile", "path": "/stocks/run", "headers": key(), "runs": HOSTILE},
        # POST /stocks/stop
        {"name": "stop, no key", "method": "POST", "path": "/stocks/stop", "headers": {"origin": PAGES}},
        {"name": "stop, wrong method", "path": "/stocks/stop", "headers": key()},
        {"name": "stop, two going", "method": "POST", "path": "/stocks/stop", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:300"}}, "keys": ["stocks:refresh"],
         "runs": [gh_run(21, "in_progress", ago=250), gh_run(20, "pending", ago=260, title="Stocks (scheduled)"),
                  gh_run(19, "completed", "success", ago=3600)]},
        {"name": "stop, one already ending", "method": "POST", "path": "/stocks/stop", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:300"}}, "keys": ["stocks:refresh"],
         "runs": [gh_run(22, "in_progress", ago=250)], "cancels": {"22": 409}},
        {"name": "stop, a cancel fails", "method": "POST", "path": "/stocks/stop", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:300"}}, "keys": ["stocks:refresh"],
         "runs": [gh_run(23, "in_progress", ago=250), gh_run(24, "queued", ago=100)], "cancels": {"24": 500}},
        {"name": "stop, inside the grace", "method": "POST", "path": "/stocks/stop", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:20"}}, "keys": ["stocks:refresh"], "runs": []},
        {"name": "stop, nothing going", "method": "POST", "path": "/stocks/stop", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:600"}}, "keys": ["stocks:refresh"],
         "runs": [gh_run(25, "completed", "success", ago=550)]},
        {"name": "stop, unreadable", "method": "POST", "path": "/stocks/stop", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:300"}}, "keys": ["stocks:refresh"], "runs": {"status": 502}},
        # POST /stocks/refresh against the runs
        {"name": "refresh, a run is going", "method": "POST", "path": "/stocks/refresh", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": OLD_RUN}}, "keys": ["stocks:refresh"],
         "runs": [gh_run(31, "in_progress", ago=120, title="Stocks (scheduled)")]},
        # inside the grace and inside the old 30 minutes, but the run is listed and over
        {"name": "refresh, right after a failed run", "method": "POST", "path": "/stocks/refresh", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:90"}}, "keys": ["stocks:refresh"],
         "runs": [gh_run(32, "completed", "failure", ago=80)]},
        {"name": "refresh, right after a stop", "method": "POST", "path": "/stocks/refresh", "headers": key(),
         "seed": {"stocks:refresh": None}, "keys": ["stocks:refresh"],
         "runs": [gh_run(33, "completed", "cancelled", ago=120)]},
        {"name": "refresh, inside the grace", "method": "POST", "path": "/stocks/refresh", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:30"}}, "keys": ["stocks:refresh"], "runs": []},
        {"name": "refresh, unreadable, inside 30 minutes", "method": "POST", "path": "/stocks/refresh", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:300"}}, "runs": {"status": 500}},
        {"name": "refresh, bad json, inside 30 minutes", "method": "POST", "path": "/stocks/refresh", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:300"}}, "runs": {"raw": "<html>"}},
        {"name": "refresh, unreadable, after 30 minutes", "method": "POST", "path": "/stocks/refresh", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:3600"}}, "runs": {"status": 500}, "keys": ["stocks:refresh"]},
        {"name": "refresh, then stop inside the grace, then the run state", "seed": {"stocks:refresh": None},
         "together": [{"method": "POST", "path": "/stocks/refresh", "headers": key()},
                      {"method": "POST", "path": "/stocks/stop", "headers": key(), "after": 600},
                      {"path": "/stocks/run", "headers": key(), "after": 700}]},
        # A run that ended in the minute before the click is not the run asked for: GitHub has not
        # listed the new one yet (the stand-in never does), so the start grace holds.
        *[{"name": f"two refreshes in turn, {label}", "seed": seed, "runs": runs,
           "together": [{"method": "POST", "path": "/stocks/refresh", "headers": key()},
                        {"method": "POST", "path": "/stocks/refresh", "headers": key(), "after": 1500},
                        {"path": "/stocks/run", "headers": key(), "after": 1800}]}
          for label, seed, runs in (
              ("a run stopped 40 s before", {"stocks:refresh": None}, [gh_run(50, "completed", "cancelled", ago=40)]),
              ("a run stopped 58 s before", {"stocks:refresh": None}, [gh_run(50, "completed", "cancelled", ago=58)]),
              ("a start that failed 50 s before", {"stocks:refresh": {"requested_at": "ago:55"}},
               [gh_run(51, "completed", "startup_failure", ago=50)]),
              ("a scheduled run that finished 30 s before", {"stocks:refresh": None},
               [gh_run(52, "completed", "success", ago=30, title="Stocks (scheduled)")]),
              ("a run stopped 90 s before", {"stocks:refresh": None}, [gh_run(53, "completed", "cancelled", ago=90)]))],
        {"name": "run, stop, run, a run stopped 45 s before", "seed": {"stocks:refresh": None}, "keys": ["stocks:refresh"],
         "runs": [gh_run(54, "completed", "cancelled", ago=45)],
         "together": [{"method": "POST", "path": "/stocks/refresh", "headers": key()},
                      {"method": "POST", "path": "/stocks/stop", "headers": key(), "after": 1000},
                      {"method": "POST", "path": "/stocks/refresh", "headers": key(), "after": 2000}]},
        # The claim names the newest run listed when it was written; a run with a higher id is the one asked for.
        {"name": "run, the run asked for is listed", "path": "/stocks/run", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:20", "after_id": 55}},
         "runs": [gh_run(56, "queued", ago=5), gh_run(55, "completed", "failure", ago=30)]},
        {"name": "run, only the run before is listed", "path": "/stocks/run", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:20", "after_id": 55}},
         "runs": [gh_run(55, "completed", "failure", ago=30)]},
        {"name": "run, the claim is older than the grace", "path": "/stocks/run", "headers": key(),
         "seed": {"stocks:refresh": {"requested_at": "ago:130", "after_id": 55}},
         "runs": [gh_run(55, "completed", "failure", ago=140)]},
    ]},
    {"name": "github refuses", "kv": True, "bindings": FULL, "cases": [
        {"name": "refresh", "method": "POST", "path": "/stocks/refresh", "headers": key(), "answers": [403],
         "keys": ["stocks:refresh"]},
        {"name": "refresh, 5xx then taken", "method": "POST", "path": "/stocks/refresh", "headers": key(),
         "answers": [502, 204], "keys": ["stocks:refresh"]},
        {"name": "refresh, two together while GitHub is slow", "seed": {"stocks:refresh": None},
         "answers": [{"status": 204, "delay": 800}],
         "together": [{"method": "POST", "path": "/stocks/refresh", "headers": key()},
                      {"method": "POST", "path": "/stocks/refresh", "headers": key(), "after": 100}]},
        {"name": "refresh refused, the run before is kept",
         "seed": {"stocks:refresh": {"requested_at": OLD_RUN, "after_id": 40}},
         "method": "POST", "path": "/stocks/refresh", "headers": key(), "answers": [403], "keys": ["stocks:refresh"]},
    ]},
    {"name": "edges", "kv": True, "bindings": FULL, "cases": [
        {"name": "remove during an upload",
         "seed": {"stocks:config": {"version": 1, "tickers": ["AAPL", "BRK-B"], "overrides": {}}},
         "together": [{"method": "PUT", "path": "/stocks/ingest/state/BRK-B", "headers": ingest(),
                       "body": state("BRK-B", lessons=[{"date": "2026-11-12", "text": "Old."}]),
                       "slow": {"first": 10, "wait": 600}},
                      {"method": "POST", "path": "/stocks/tickers", "headers": key(),
                       "body": {"action": "remove", "ticker": "BRK-B"}, "after": 200}],
         "keys": ["stocks:state:BRK-B", "stocks:config"]},
        {"name": "put analysis, control characters", "method": "PUT", "path": "/stocks/ingest/ticker/AAPL",
         "headers": ingest(), "body": heavy("AAPL", CONTROLS), "keys": ["stocks:ticker:AAPL"]},
        {"name": "put analysis, escapes over 1024 bytes", "method": "PUT", "path": "/stocks/ingest/ticker/AAPL",
         "headers": ingest(), "body": heavy("AAPL", LONE), "keys": ["stocks:ticker:AAPL"]},
        {"name": "settings, a body that never ends", "method": "POST", "path": "/stocks/settings", "headers": key(),
         "open": 4 * 2000 + 1, "timeout": 5000},
        {"name": "put state, a body that never ends", "method": "PUT", "path": "/stocks/ingest/state/AAPL",
         "headers": ingest(), "open": stocks.STATE_MAX_BYTES + 1, "timeout": 5000},
        {"name": "ingest config, overrides of listed tickers only", "path": "/stocks/ingest/config", "headers": ingest(),
         "seed": {"stocks:config": {"version": 1, "tickers": ["AAPL", "MSFT"], "overrides": OVERRIDES}}},
        {"name": "remove a ticker with overrides", "method": "POST", "path": "/stocks/tickers", "headers": key(),
         "body": {"action": "remove", "ticker": "aapl"}, "keys": ["stocks:config"]},
        {"name": "ingest config, after the remove", "path": "/stocks/ingest/config", "headers": ingest()},
        # BRK-B is not in the list here, so a remove only says whether the spelling is a ticker.
        *[{"name": f"spelling {ord(ch):04x}", "method": "POST", "path": "/stocks/tickers", "headers": key(),
           "body": {"action": "remove", "ticker": f"{ch}brk.b{ch}"}} for ch in SPELLINGS],
    ]},
]


# Each case of "runs" starts from the claim it names, none when it names none.
for _case in next(i for i in PLAN if i["name"] == "runs")["cases"]:
    _case.setdefault("seed", {"stocks:refresh": None})


def seconds_between(a, b):
    """Seconds from one ISO time to another."""
    def parse(v):
        return datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))

    return round((parse(b) - parse(a)).total_seconds())


def dispatches(seen):
    """The requests that asked GitHub to start a run."""
    return [c for c in seen["github"] if c["method"] == "POST" and c["url"].endswith("/dispatches")]


def cancels(seen):
    """The run ids GitHub was asked to cancel, in order."""
    return [int(c["url"].split("/")[-2]) for c in seen["github"] if c["url"].endswith("/cancel")]


@unittest.skipUnless(shutil.which("node") and find_miniflare(), "needs node and miniflare")
class WorkerdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp(prefix="stocks-worker-")
        cls.addClassCleanup(shutil.rmtree, tmp)
        paths = {}
        for name, data in (("harness.mjs", HARNESS), ("plan.json", json.dumps(PLAN))):
            paths[name] = os.path.join(tmp, name)
            with open(paths[name], "w", encoding="utf-8") as f:
                f.write(data)
        res = subprocess.run(["node", paths["harness.mjs"], find_miniflare(), WORKER_JS, paths["plan.json"]],
                             capture_output=True, text=True, timeout=300, cwd=tmp)
        if res.returncode != 0:
            raise AssertionError("the miniflare harness failed: " + res.stderr[-2000:])
        cls.got = json.loads(res.stdout.strip().splitlines()[-1])

    def r(self, name, inst="full"):
        return self.got[f"{inst}: {name}"]

    def status(self, name, inst="full"):
        seen = self.r(name, inst)
        return seen["status"], seen["body"]

    def test_ingest_needs_its_own_key_and_no_browser(self):
        for name, status, error in (("ingest, no key", 401, "unauthorized"),
                                    ("ingest, from a browser", 403, "browsers may not call ingest routes"),
                                    ("ingest, wrong key", 401, "unauthorized"),
                                    ("ingest, the access key", 401, "unauthorized")):
            seen = self.r(name)
            self.assertEqual((seen["status"], seen["body"], seen["guard"], seen["cors"]),
                             (status, {"error": error}, "deny", None), name)
        for inst in ("no ingest key", "short ingest key", "ingest key equals access key"):
            seen = self.r("ingest config", inst)
            self.assertEqual((seen["status"], seen["body"], seen["guard"]),
                             (503, {"error": "the ingest key is not configured"}, "deny"), inst)
        self.assertEqual(self.status("ingest config, empty"),
                         (200, {"version": 1, "tickers": [], "model": "claude-sonnet-5-5", "overrides": {}}))

    def test_key_holder_routes_sit_behind_the_guard(self):
        for name in ("list, no key", "list, the ingest key"):
            seen = self.r(name)
            self.assertEqual((seen["status"], seen["guard"]), (401, "deny"), name)
        seen = self.r("list, empty")
        self.assertEqual(seen["status"], 200)
        self.assertEqual(seen["cache"], "no-store")
        self.assertEqual(seen["cors"], PAGES)
        self.assertEqual(seen["body"], {
            "tickers": [], "max": 30,
            "settings": {"model": "claude-sonnet-5-5", "default_model": "claude-sonnet-5-5",
                         "allowed_models": list(stocks.ALLOWED_MODELS)},
            "refresh": {"min_minutes": 30, "requested_at": None, "available_at": None},
            "overview": {"generated_at": None}})

    def test_the_list_edits(self):
        self.assertEqual(self.status("add"), (200, {"ok": True, "changed": True, "tickers": ["AAPL"]}))
        self.assertEqual(self.status("add a class share"),
                         (200, {"ok": True, "changed": True, "tickers": ["AAPL", "BRK-B"]}))
        self.assertEqual(self.status("add again"), (200, {"ok": True, "changed": False, "tickers": ["AAPL", "BRK-B"]}))
        bad = {"error": "a ticker is 1 to 6 letters, with an optional class such as BRK-B"}
        self.assertEqual(self.status("add, bad ticker"), (400, bad))
        self.assertEqual(self.status("add, bad action"), (400, {"error": "the action must be add or remove"}))
        for name in ("add, too long", "add, not json"):
            self.assertEqual(self.status(name)[0], 400, name)
            self.assertIsNone(self.r(name)["guard"], name)

    def test_reading_before_and_after_the_run(self):
        self.assertEqual(self.status("analysis before the run"), (404, {"error": "no analysis yet"}))
        self.assertEqual(self.status("analysis, not listed"), (404, {"error": "not in the list"}))
        self.assertEqual(self.status("analysis, bad ticker")[0], 400)
        self.assertEqual(self.status("overview before the run"), (404, {"error": "no overview yet"}))
        status, doc = self.status("analysis")
        self.assertEqual((status, doc["ticker"], doc["kind"]), (200, "AAPL", "stocks.ticker"))
        self.assertEqual(self.status("analysis, dot spelling"), (404, {"error": "no analysis yet"}))
        status, ov = self.status("overview")
        self.assertEqual((status, ov["kind"]), (200, "stocks.overview"))
        listed = self.status("list, after the run")[1]
        self.assertEqual([t["ticker"] for t in listed["tickers"]], ["AAPL", "BRK-B"])
        first, second = listed["tickers"]
        self.assertEqual(first["status"], "ok")
        self.assertEqual((first["stance"], first["confidence"], first["close"], first["change_pct"]),
                         ("bullish", "medium", 270.5, 1.25))
        self.assertEqual(len(first["name"]), 60)
        self.assertEqual(first["hit"], {"rate": 0.4709, "n": 412, "kind": "backtest", "h": 20})
        self.assertEqual(second["status"], "pending")
        self.assertIsNone(second["hit"])
        self.assertEqual(listed["overview"]["generated_at"], ss.example_overview()["generated_at"])

    def test_the_ingest_writes(self):
        self.assertEqual(self.status("ingest state, none yet"), (404, {"error": "no state"}))
        self.assertEqual(self.status("ingest state, not listed"), (409, {"error": "not in the list"}))
        self.assertEqual(self.status("ingest state, lower case")[0], 400)
        status, body = self.status("put state")
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(self.r("put state")["kv"]["stocks:state:AAPL"]["value"]["ticker"], "AAPL")
        self.assertEqual(self.status("put state, class share")[0], 200)
        for name in ("put state, another ticker inside", "put state, wrong kind", "put state, wrong version",
                     "put state, not json", "put state, a list"):
            self.assertEqual(self.status(name)[0], 400, name)
        self.assertEqual(self.status("put state, not listed"), (409, {"error": "not in the list"}))
        for name in ("put state, too large", "put analysis, too large", "put overview, too large"):
            self.assertEqual(self.status(name), (413, {"error": "too large"}), name)
        self.assertEqual(self.status("put analysis")[0], 200)
        meta = self.r("put analysis")["kv"]["stocks:ticker:AAPL"]["metadata"]
        self.assertEqual(sorted(meta), ["change_pct", "close", "confidence", "data_as_of", "generated_at", "hit",
                                        "name", "stance", "status"])
        # The longest horizon's backtest until 10 live calls are graded (the list needs no document).
        self.assertEqual(meta["hit"], {"rate": 0.4709, "n": 412, "kind": "backtest", "h": 20})
        self.assertLessEqual(len(json.dumps(meta).encode("utf-8")), 1024)
        self.assertEqual(self.status("put overview")[0], 200)
        self.assertEqual(self.status("put overview, wrong kind")[0], 400)
        status, saved = self.status("ingest state, saved")
        self.assertEqual((status, saved["lessons"][0]["text"]), (200, "A lesson."))

    def test_settings_allowlist(self):
        self.assertEqual(self.status("settings"), (200, {"model": "claude-sonnet-5-5",
                                                         "default_model": "claude-sonnet-5-5",
                                                         "allowed_models": list(stocks.ALLOWED_MODELS)}))
        self.assertEqual(self.status("settings, haiku"), (200, {"ok": True, "model": "claude-haiku-5-5"}))
        self.assertEqual(self.status("settings, not allowed"), (400, {"error": "that model is not allowed"}))
        self.assertEqual(self.status("ingest config, after edits")[1]["model"], "claude-haiku-5-5")

    def test_refresh_is_rate_limited_and_starts_stocks_yml(self):
        status, body = self.status("refresh")
        self.assertEqual((status, body["ok"]), (202, True))
        # GitHub's list of runs first (nothing going), then the start.
        calls = self.r("refresh")["github"]
        self.assertEqual([(c["method"], c["url"]) for c in calls], [
            ("GET", LIST_URL),
            ("POST", "https://api.github.com/repos/owner/news-digest/actions/workflows/stocks.yml/dispatches")])
        self.assertEqual(json.loads(calls[1]["body"]), {"ref": "main", "inputs": {"reason": "manual"}})
        for c in calls:
            self.assertEqual((c["auth"], c["agent"]), (f"Bearer {TOKEN}", "news-digest-stocks"))
        # GitHub lists no run yet: the start grace of 2 minutes, not 30.
        self.assertEqual(seconds_between(body["requested_at"], body["available_at"]), 120)
        seen = self.r("refresh again")
        self.assertEqual((seen["status"], seen["body"]["error"], seen["guard"]),
                         (429, "a run was started recently", None))
        self.assertTrue(60 <= seen["body"]["retry_after_seconds"] <= 120, seen["body"])
        self.assertEqual(dispatches(seen), [])
        self.assertEqual(self.status("refresh", "no repo")[0], 500)
        refused = self.r("refresh", "github refuses")
        self.assertEqual((refused["status"], refused["kv"]["stocks:refresh"]), (502, None))
        retried = self.r("refresh, 5xx then taken", "github refuses")
        self.assertEqual((retried["status"], len(dispatches(retried))), (202, 2))
        self.assertIsNotNone(retried["kv"]["stocks:refresh"])

    def test_remove_forgets_everything_and_the_list_stops_at_thirty(self):
        status, body = self.status("remove")
        self.assertEqual((status, body["changed"], body["tickers"]), (200, True, ["BRK-B"]))
        kv = self.r("remove")["kv"]
        self.assertIsNone(kv["stocks:state:AAPL"])
        self.assertIsNone(kv["stocks:ticker:AAPL"])
        self.assertEqual(kv["stocks:config"]["value"]["tickers"], ["BRK-B"])
        self.assertEqual(self.status("remove again")[1]["changed"], False)
        for t in MANY:
            self.assertEqual(self.status(f"fill {t}")[0], 200, t)
        self.assertEqual(self.status("fill ZZZ"), (409, {"error": "the list already holds 30 tickers"}))

    def test_methods_paths_and_the_other_blocks(self):
        self.assertEqual(self.status("list, wrong method"), (405, {"error": "use GET"}))
        self.assertEqual(self.status("tickers, wrong method"), (405, {"error": "use POST"}))
        self.assertEqual(self.status("ingest config, wrong method"), (405, {"error": "use GET"}))
        self.assertEqual(self.status("unknown stocks path"), (404, {"error": "not found"}))
        self.assertEqual(self.status("unknown ingest path"), (404, {"error": "not found"}))
        seen = self.r("summaries")
        self.assertEqual((seen["status"], seen["body"], seen["guard"]),
                         (501, {"error": "summaries are not set up on this worker"}, None))
        x = self.r("x accounts still work")
        self.assertEqual(x["status"], 202)
        self.assertTrue(x["github"][0]["url"].endswith("/actions/workflows/sources.yml/dispatches"))

    def test_missing_kv_says_so(self):
        for name in ("list", "ingest config"):
            self.assertEqual(self.status(name, "no kv"), (500, {"error": "STOCKS_KV is not bound on this worker"}))

    def test_the_cron_starts_both_runs_at_545_and_only_the_digest_at_noon(self):
        def workflows(name, inst="full"):
            return [(c["url"].split("/workflows/")[1].split("/")[0], json.loads(c["body"])["inputs"])
                    for c in self.r(name, inst)["github"]]

        both = [("daily.yml", {"run": "morning"}), ("stocks.yml", {"reason": "scheduled"})]
        self.assertEqual(workflows("cron 05:45 summer"), both)
        self.assertEqual(workflows("cron 05:45 winter"), both)
        self.assertEqual(workflows("cron noon"), [("daily.yml", {"run": "noon"})])
        self.assertEqual(workflows("cron off time"), [])
        for name in ("cron 05:45 summer", "cron 05:45 winter", "cron noon", "cron off time"):
            self.assertEqual(self.r(name)["outcome"], "ok", name)
        # Both refused: both were tried, and the event fails.
        self.assertEqual(len(self.r("cron, github refuses both")["github"]), 2)
        self.assertEqual(self.r("cron, github refuses both")["outcome"], "exception")
        self.assertEqual(workflows("cron 05:45", "no kv"), [("daily.yml", {"run": "morning"})])
        self.assertIn("Stocks: not set up on this worker, nothing to start", "\n".join(self.got["no kv: logs"]))
        no_repo = self.r("cron 05:45", "no repo")
        self.assertEqual((no_repo["outcome"], no_repo["github"]), ("exception", []))

    def test_logs_hold_no_ticker_key_or_token(self):
        logs = "\n".join(line for k, v in self.got.items() if k.endswith(": logs") for line in v)
        self.assertIn("Stocks: saved a ticker analysis", logs)
        self.assertIn("Stocks: the manual run did not start", logs)
        for leak in ("AAPL", "BRK-B", "MSFT", "Microsoft", ACCESS, INGEST, TOKEN, "A lesson.", "Old."):
            self.assertNotIn(leak, logs)

    def test_two_refreshes_together_start_one_run(self):
        seen = self.r("refresh, two together while GitHub is slow", "github refuses")
        self.assertEqual([a["status"] for a in seen["answers"]], [202, 429])
        self.assertEqual(seen["answers"][1]["body"]["error"], "a run was started recently")
        self.assertEqual(len(dispatches(seen)), 1)
        # GitHub refuses: the record of the run before is put back as it was.
        kept = self.r("refresh refused, the run before is kept", "github refuses")
        self.assertEqual((kept["status"], len(dispatches(kept))), (502, 1))
        self.assertEqual(kept["kv"]["stocks:refresh"]["value"], {"requested_at": OLD_RUN, "after_id": 40})

    def test_a_remove_during_an_upload_wins(self):
        seen = self.r("remove during an upload", "edges")
        upload, remove = seen["answers"]
        self.assertEqual((remove["status"], remove["body"]["changed"]), (200, True))
        self.assertEqual((upload["status"], upload["body"]), (409, {"error": "not in the list"}))
        self.assertIsNone(seen["kv"]["stocks:state:BRK-B"])
        self.assertEqual(seen["kv"]["stocks:config"]["value"]["tickers"], ["AAPL"])

    def test_metadata_always_fits_in_1024_bytes(self):
        # 200 means KV took the metadata (over 1024 bytes it refuses, and the upload used to answer 500).
        for name in ("put analysis, control characters", "put analysis, escapes over 1024 bytes"):
            seen = self.r(name, "edges")
            self.assertEqual((seen["status"], seen["body"]["ok"]), (200, True), name)
            meta = seen["kv"]["stocks:ticker:AAPL"]["metadata"]
            stored = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.assertLessEqual(len(stored), 1024, name)
            self.assertEqual(meta["close"], TINY, name)
        meta = self.r("put analysis, control characters", "edges")["kv"]["stocks:ticker:AAPL"]["metadata"]
        self.assertEqual((meta["name"], meta["status"]), ("", ""))  # control characters are removed
        meta = self.r("put analysis, escapes over 1024 bytes", "edges")["kv"]["stocks:ticker:AAPL"]["metadata"]
        self.assertIsNone(meta["name"])  # too long in JSON: the name goes first, the other texts stay
        self.assertTrue(meta["status"] and meta["stance"] and meta["generated_at"])

    def test_bodies_that_never_end_are_cut_at_the_cap(self):
        seen = self.r("settings, a body that never ends", "edges")
        self.assertEqual((seen["status"], seen["body"]), (400, {"error": "that model is not allowed"}))
        seen = self.r("put state, a body that never ends", "edges")
        self.assertEqual((seen["status"], seen["body"]), (413, {"error": "too large"}))

    def test_a_removed_ticker_leaves_no_overrides(self):
        listed = {k: v for k, v in OVERRIDES.items() if k != "ZZZQ"}
        self.assertEqual(self.status("ingest config, overrides of listed tickers only", "edges")[1]["overrides"],
                         listed)
        status, body = self.status("remove a ticker with overrides", "edges")
        self.assertEqual((status, body["tickers"]), (200, ["MSFT"]))
        stored = self.r("remove a ticker with overrides", "edges")["kv"]["stocks:config"]["value"]
        self.assertEqual(stored["overrides"], {"msft": OVERRIDES["msft"], "ZZZQ": OVERRIDES["ZZZQ"]})
        self.assertEqual(self.status("ingest config, after the remove", "edges")[1]["overrides"],
                         {"msft": OVERRIDES["msft"]})

    def test_tickers_lose_the_spaces_python_strips(self):
        for ch in SPELLINGS:
            want = 200 if stocks.normalize_ticker(f"{ch}brk.b{ch}") else 400
            self.assertEqual(self.status(f"spelling {ord(ch):04x}", "edges")[0], want, f"U+{ord(ch):04X}")
        # Where trim() and Python's strip() differ.
        for code, want in (("001c", 200), ("0085", 200), ("feff", 400)):
            self.assertEqual(self.status(f"spelling {code}", "edges")[0], want, code)

    # ------------------------------------------------------------ the runs: GET /stocks/run, POST /stocks/stop

    def test_the_run_routes_sit_behind_the_guard(self):
        for name in ("run, no key", "run, the ingest key", "stop, no key"):
            seen = self.r(name, "runs")
            self.assertEqual((seen["status"], seen["guard"], seen["github"]), (401, "deny", []), name)
        self.assertEqual(self.status("run, wrong method", "runs"), (405, {"error": "use GET"}))
        self.assertEqual(self.status("stop, wrong method", "runs"), (405, {"error": "use POST"}))
        seen = self.r("run, none", "runs")
        self.assertEqual((seen["cache"], seen["cors"], seen["guard"]), ("no-store", PAGES, None))

    def test_the_run_state_reads_githubs_list_of_runs(self):
        seen = self.r("run, none", "runs")
        self.assertEqual((seen["status"], seen["body"]), (200, {
            "checked": True, "active": False, "can_start": True, "available_at": None, "requested_at": None, "run": None}))
        self.assertEqual([(c["method"], c["url"], c["auth"]) for c in seen["github"]],
                         [("GET", LIST_URL, f"Bearer {TOKEN}")])
        status, body = self.status("run, queued", "runs")
        self.assertEqual((status, body["checked"], body["active"], body["can_start"]), (200, True, True, False))
        self.assertEqual(body["run"], {"id": 11, "status": "queued", "conclusion": None, "kind": "manual",
                                       "created_at": body["run"]["created_at"], "updated_at": body["run"]["updated_at"],
                                       "started_at": body["run"]["started_at"], "url": REPO_RUNS + "11"})
        self.assertRegex(body["run"]["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        # Newest first, whatever GitHub's order.
        body = self.status("run, in progress", "runs")[1]
        self.assertEqual((body["active"], body["can_start"], body["run"]["id"], body["run"]["status"], body["run"]["kind"]),
                         (True, False, 12, "in_progress", "scheduled"))
        body = self.status("run, failed", "runs")[1]
        self.assertEqual((body["active"], body["can_start"], body["available_at"]), (False, True, None))
        self.assertEqual((body["run"]["status"], body["run"]["conclusion"]), ("completed", "failure"))
        body = self.status("run, cancelled", "runs")[1]
        self.assertEqual((body["can_start"], body["run"]["conclusion"], body["run"]["url"]), (True, "cancelled", REPO_RUNS + "14"))

    def test_a_run_still_being_created_holds_the_start_for_two_minutes(self):
        for name in ("run, starting", "run, starting with an older run listed"):
            body = self.status(name, "runs")[1]
            self.assertEqual((body["checked"], body["active"], body["can_start"]), (True, False, False), name)
            self.assertEqual(seconds_between(body["requested_at"], body["available_at"]), 120, name)
        self.assertEqual(self.status("run, starting with an older run listed", "runs")[1]["run"]["id"], 15)
        # A run created a little before the claim, by GitHub's clock, is the one asked for.
        body = self.status("run, listed within the clock skew", "runs")[1]
        self.assertEqual((body["active"], body["can_start"], body["available_at"]), (True, False, None))

    def test_without_github_the_claim_rule_of_30_minutes_stands(self):
        body = self.status("run, unreadable", "runs")[1]
        self.assertEqual((body["checked"], body["reason"], body["active"], body["can_start"], body["run"]),
                         (False, "GitHub could not be read", False, False, None))
        self.assertEqual(seconds_between(body["requested_at"], body["available_at"]), 1800)
        for name in ("run, bad json", "run, no list in the answer"):
            body = self.status(name, "runs")[1]
            self.assertEqual((body["checked"], body["reason"], body["can_start"]), (False, "GitHub could not be read", True), name)
        # Not set up: no request to GitHub, the claim alone, and still 200 so the page works.
        for inst in ("no repo", "no token"):
            seen = self.r("run", inst)
            self.assertEqual((seen["status"], seen["body"], seen["github"]), (200, {
                "checked": False, "reason": "not set up", "active": False, "can_start": True, "available_at": None,
                "requested_at": None, "run": None}, []), inst)
        body = self.status("run, a claim", "no repo")[1]
        self.assertEqual((body["checked"], body["can_start"]), (False, False))
        self.assertEqual(seconds_between(body["requested_at"], body["available_at"]), 1800)

    def test_githubs_link_and_title_never_reach_the_page(self):
        status, body = self.status("run, hostile", "runs")
        self.assertEqual(status, 200)
        # Only run 41 has a run id; its link is built from GH_REPO and the id.
        self.assertEqual(body["run"], {"id": 41, "status": "unknown", "conclusion": None, "kind": "unknown",
                                       "created_at": None, "updated_at": None, "started_at": None,
                                       "url": REPO_RUNS + "41"})
        self.assertEqual((body["active"], body["can_start"]), (False, True))  # a status GitHub may add never holds the button
        text = json.dumps(self.r("run, hostile", "runs")["body"])
        for leak in ("evil.example", "onerror", "<img", "hacking", "pwned", "yesterday"):
            self.assertNotIn(leak, text)

    def test_stop_cancels_exactly_the_runs_that_are_going_and_clears_the_claim(self):
        seen = self.r("stop, two going", "runs")
        self.assertEqual((seen["status"], seen["body"]), (200, {"ok": True, "stopped": 2}))
        self.assertEqual(cancels(seen), [21, 20])
        for c in seen["github"]:
            if c["url"].endswith("/cancel"):
                self.assertEqual((c["method"], c["auth"]), ("POST", f"Bearer {TOKEN}"))
                self.assertTrue(c["url"].startswith("https://api.github.com/repos/owner/news-digest/actions/runs/"))
        self.assertEqual(dispatches(seen), [])
        self.assertIsNone(seen["kv"]["stocks:refresh"])
        # 409: GitHub is already ending that run, which counts as stopped.
        seen = self.r("stop, one already ending", "runs")
        self.assertEqual((seen["status"], seen["body"], cancels(seen)), (200, {"ok": True, "stopped": 1}, [22]))
        self.assertIsNone(seen["kv"]["stocks:refresh"])
        # Nothing going: nothing to cancel, and the claim goes.
        seen = self.r("stop, nothing going", "runs")
        self.assertEqual((seen["status"], seen["body"], cancels(seen)), (200, {"ok": True, "stopped": 0}, []))
        self.assertIsNone(seen["kv"]["stocks:refresh"])

    def test_stop_keeps_the_claim_when_github_does_not_stop_a_run(self):
        seen = self.r("stop, a cancel fails", "runs")
        self.assertEqual(seen["status"], 502)
        self.assertEqual(seen["body"], {"error": "GitHub did not stop the run: GitHub answered 500 Server said 500", "stopped": 1})
        self.assertEqual(cancels(seen), [24, 23])
        self.assertIsNotNone(seen["kv"]["stocks:refresh"])
        seen = self.r("stop, unreadable", "runs")
        self.assertEqual((seen["status"], seen["body"], cancels(seen)),
                         (502, {"error": "GitHub could not be read, so nothing was stopped"}, []))
        self.assertIsNotNone(seen["kv"]["stocks:refresh"])
        for inst in ("no repo", "no token"):
            seen = self.r("stop", inst)
            self.assertEqual((seen["status"], seen["body"], seen["github"]),
                             (500, {"error": "this worker cannot stop runs: set GH_REPO and GH_DISPATCH_TOKEN"}, []), inst)
        self.assertIsNotNone(self.r("stop", "no repo")["kv"]["stocks:refresh"])

    def test_stop_inside_the_grace_says_the_run_is_still_starting(self):
        seen = self.r("stop, inside the grace", "runs")
        self.assertEqual((seen["status"], seen["body"]["error"]), (409, "the run is still starting, try again in a minute"))
        self.assertTrue(60 <= seen["body"]["retry_after_seconds"] <= 100, seen["body"])
        self.assertEqual(cancels(seen), [])
        self.assertIsNotNone(seen["kv"]["stocks:refresh"])
        # Start, then Stop before GitHub lists the run, then the state: still starting.
        start, stop, state = self.r("refresh, then stop inside the grace, then the run state", "runs")["answers"]
        self.assertEqual(start["status"], 202)
        self.assertEqual((stop["status"], stop["body"]["error"]), (409, "the run is still starting, try again in a minute"))
        self.assertEqual((state["status"], state["body"]["can_start"], state["body"]["requested_at"]),
                         (200, False, start["body"]["requested_at"]))
        self.assertEqual(state["body"]["available_at"], start["body"]["available_at"])

    def test_a_run_that_ended_just_before_the_click_is_not_the_run_asked_for(self):
        for label in ("a run stopped 40 s before", "a run stopped 58 s before", "a start that failed 50 s before",
                      "a scheduled run that finished 30 s before", "a run stopped 90 s before"):
            seen = self.r(f"two refreshes in turn, {label}", "runs")
            first, second, state = seen["answers"]
            self.assertEqual((first["status"], second["status"]), (202, 429), label)
            self.assertEqual(second["body"]["error"], "a run was started recently", label)
            self.assertTrue(100 <= second["body"]["retry_after_seconds"] <= 120, (label, second["body"]))
            self.assertEqual(len(dispatches(seen)), 1, label)  # one run, not two
            body = state["body"]
            self.assertEqual((body["checked"], body["active"], body["can_start"]), (True, False, False), label)
            self.assertEqual(body["available_at"], first["body"]["available_at"], label)
        # Run, Stop before GitHub lists the run, Run again: the run is still starting, and the claim stays.
        seen = self.r("run, stop, run, a run stopped 45 s before", "runs")
        start, stop, again = seen["answers"]
        self.assertEqual(start["status"], 202)
        self.assertEqual((stop["status"], stop["body"]["error"]), (409, "the run is still starting, try again in a minute"))
        self.assertEqual((again["status"], again["body"]["error"]), (429, "a run was started recently"))
        self.assertEqual((len(dispatches(seen)), cancels(seen)), (1, []))
        self.assertEqual(seen["kv"]["stocks:refresh"]["value"], {"requested_at": start["body"]["requested_at"], "after_id": 54})

    def test_the_claim_knows_the_run_it_was_written_after(self):
        seen = self.r("refresh, right after a failed run", "runs")
        self.assertEqual(seen["kv"]["stocks:refresh"]["value"], {"requested_at": seen["body"]["requested_at"], "after_id": 32})
        seen = self.r("refresh, inside the grace", "runs")  # refused: the claim is not touched
        self.assertNotIn("after_id", seen["kv"]["stocks:refresh"]["value"])
        # Without GitHub's list there is no run id to keep.
        seen = self.r("refresh, unreadable, after 30 minutes", "runs")
        self.assertEqual(seen["kv"]["stocks:refresh"]["value"], {"requested_at": seen["body"]["requested_at"]})
        body = self.status("run, the run asked for is listed", "runs")[1]
        self.assertEqual((body["active"], body["can_start"], body["available_at"], body["run"]["id"]), (True, False, None, 56))
        body = self.status("run, only the run before is listed", "runs")[1]
        self.assertEqual((body["active"], body["can_start"], body["run"]["id"]), (False, False, 55))
        self.assertEqual(seconds_between(body["requested_at"], body["available_at"]), 120)
        # After the grace, a run that was asked for but never listed no longer holds the button.
        body = self.status("run, the claim is older than the grace", "runs")[1]
        self.assertEqual((body["active"], body["can_start"], body["available_at"]), (False, True, None))

    def test_refresh_is_refused_while_a_run_is_going(self):
        seen = self.r("refresh, a run is going", "runs")
        self.assertEqual((seen["status"], seen["body"], seen["guard"]), (409, {"error": "a run is going", "active": True}, None))
        self.assertEqual(dispatches(seen), [])
        self.assertEqual(seen["kv"]["stocks:refresh"]["value"], {"requested_at": OLD_RUN})  # no claim written

    def test_a_failed_or_stopped_run_frees_the_start_at_once(self):
        for name in ("refresh, right after a failed run", "refresh, right after a stop"):
            seen = self.r(name, "runs")
            self.assertEqual((seen["status"], seen["body"]["ok"]), (202, True), name)
            self.assertEqual(len(dispatches(seen)), 1, name)
            self.assertEqual(seen["kv"]["stocks:refresh"]["value"]["requested_at"], seen["body"]["requested_at"], name)
            self.assertEqual(seconds_between(seen["body"]["requested_at"], seen["body"]["available_at"]), 120, name)

    def test_refresh_inside_the_grace_answers_429(self):
        seen = self.r("refresh, inside the grace", "runs")
        self.assertEqual((seen["status"], seen["body"]["error"]), (429, "a run was started recently"))
        self.assertTrue(60 <= seen["body"]["retry_after_seconds"] <= 90, seen["body"])
        self.assertEqual(dispatches(seen), [])

    def test_refresh_falls_back_to_30_minutes_when_github_cannot_be_read(self):
        for name in ("refresh, unreadable, inside 30 minutes", "refresh, bad json, inside 30 minutes"):
            seen = self.r(name, "runs")
            self.assertEqual((seen["status"], seen["body"]["error"]), (429, "a run was started recently"), name)
            self.assertTrue(1400 <= seen["body"]["retry_after_seconds"] <= 1500, seen["body"])
            self.assertEqual(dispatches(seen), [], name)
        seen = self.r("refresh, unreadable, after 30 minutes", "runs")
        self.assertEqual((seen["status"], len(dispatches(seen))), (202, 1))
        self.assertEqual(seconds_between(seen["body"]["requested_at"], seen["body"]["available_at"]), 1800)

    def test_the_run_log_lines_hold_no_id_of_a_secret(self):
        logs = "\n".join(self.got["runs: logs"])
        self.assertIn("Stocks: stop asked, 2 runs cancelled", logs)
        self.assertIn("Stocks: stop asked, 1 run cancelled", logs)
        self.assertIn("Stocks: stop asked, 1 of 2 runs cancelled: GitHub answered 500", logs)
        self.assertIn("Stocks: stop asked, but the list of runs could not be read, so nothing was stopped", logs)
        for leak in (TOKEN, ACCESS, INGEST, "evil.example", "onerror", "Bearer"):
            self.assertNotIn(leak, logs)


BLOCK = r"""
import { readFileSync } from "node:fs";
const text = readFileSync(process.argv[2], "utf8");
const logs = [];
console.log = (...parts) => logs.push(parts.join(" "));
const block = new Function(text + "\nreturn { stocksSave, stocksRefreshRestore, stocksRunOf };")();
const out = {};
// KV refuses every write, with a message that names the key.
const refusing = {
  async get(name) { return name === "stocks:config" ? JSON.stringify({ tickers: ["AAPL"] }) : null; },
  async put(name) { throw new Error(`KV PUT failed: 429 Too Many Requests (${name})`); },
  async delete() {},
};
const doc = JSON.stringify({ kind: "stocks.state", version: 1, ticker: "AAPL", lessons: [] });
const saved = await block.stocksSave(new Request("https://worker.example/", { method: "PUT", body: doc }),
  { STOCKS_KV: refusing }, "state", "AAPL");
out.save = { status: saved.status, cache: saved.headers.get("cache-control"), body: await saved.json(),
  logs: logs.splice(0) };
// KV refuses the first writes it is given, then takes the rest.
const writes = [];
let refusals = 0;
const flaky = {
  async put(name, value) { if (refusals-- > 0) throw new Error("KV PUT failed: 429"); writes.push([name, value]); },
  async delete(name) { if (refusals-- > 0) throw new Error("KV DELETE failed: 429"); writes.push([name, null]); },
};
for (const [label, before, refuse] of [["put back", { requested_at: "2026-01-02T03:04:05.000Z", after_id: 41 }, 1],
  ["put back, no id", { requested_at: "2026-01-02T03:04:05.000Z" }, 0], ["deleted", null, 1], ["refused twice", null, 2]]) {
  writes.length = 0;
  refusals = refuse;
  const started = Date.now();
  await block.stocksRefreshRestore({ STOCKS_KV: flaky }, before);
  out[label] = { writes: [...writes], ms: Date.now() - started, logs: logs.splice(0) };
}
// One run of GitHub's list at a time, as GitHub might send it.
const repo = "owner/news-digest";
const good = { id: 7, status: "completed", conclusion: "timed_out", display_title: "Stocks (scheduled)",
  created_at: "2026-11-17T20:26:00Z", updated_at: "2026-11-17T20:27:05.5Z", run_started_at: "2026-11-17T20:26:03Z",
  html_url: "https://evil.example/x" };
out.runOf = [good, { ...good, id: 0 }, { ...good, id: Number.MAX_SAFE_INTEGER + 1 }, { ...good, id: "7" },
  { ...good, status: "toString", conclusion: "constructor", display_title: "__proto__" },
  { ...good, created_at: "2026-11-17", updated_at: "+275760-09-13T00:00:00Z", run_started_at: "2026-02-30T00:00:00Z" },
  { ...good, created_at: "2026-11-17T20:26:00+01:00", updated_at: "2026-11-17T24:00:00Z" },
  { ...good, display_title: ["Stocks (manual)"] }, null, 7].map((r) => block.stocksRunOf(r, repo));
process.stdout.write(JSON.stringify(out) + "\n");
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class BlockTests(unittest.TestCase):
    """stocks.js's own functions in node, with a KV that refuses writes (miniflare's never does)."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp(prefix="stocks-block-")
        cls.addClassCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "block.mjs")
        with open(path, "w", encoding="utf-8") as f:
            f.write(BLOCK)
        res = subprocess.run(["node", path, STOCKS_JS], capture_output=True, text=True, timeout=60, cwd=tmp)
        if res.returncode != 0:
            raise AssertionError("the block harness failed: " + res.stderr[-2000:])
        cls.got = json.loads(res.stdout.strip().splitlines()[-1])

    def test_a_refused_write_answers_500_with_a_safe_log_line(self):
        save = self.got["save"]
        self.assertEqual((save["status"], save["body"], save["cache"]),
                         (500, {"error": "the document could not be saved"}, "no-store"))
        self.assertEqual(save["logs"], ["Stocks: KV did not save a learning state (429)"])

    def test_the_refresh_record_is_put_back_on_the_second_try(self):
        put_back = self.got["put back"]
        # The whole record of the run before, the run id it was written after included.
        self.assertEqual(put_back["writes"], [["stocks:refresh", '{"requested_at":"2026-01-02T03:04:05.000Z","after_id":41}']])
        self.assertGreaterEqual(put_back["ms"], 1000)  # KV takes one write a second for a key
        self.assertEqual(self.got["put back, no id"]["writes"], [["stocks:refresh", '{"requested_at":"2026-01-02T03:04:05.000Z"}']])
        self.assertEqual(self.got["deleted"]["writes"], [["stocks:refresh", None]])
        for label in ("put back", "put back, no id", "deleted"):
            self.assertEqual(self.got[label]["logs"], [], label)
        self.assertEqual(self.got["refused twice"]["writes"], [])
        self.assertEqual(self.got["refused twice"]["logs"],
                         ["Stocks: the refresh limit could not be put back, so the next manual run may wait up to 30 minutes"])

    def test_one_run_of_githubs_list_keeps_only_checked_fields(self):
        good, *rest = self.got["runOf"]
        self.assertEqual(good, {"id": 7, "status": "completed", "conclusion": "timed_out", "kind": "scheduled",
                                "created_at": "2026-11-17T20:26:00.000Z", "updated_at": "2026-11-17T20:27:05.500Z",
                                "started_at": "2026-11-17T20:26:03.000Z",
                                "url": "https://github.com/owner/news-digest/actions/runs/7"})
        no_id, too_big, text_id, odd_names, odd_times, offsets, list_title, none, number = rest
        self.assertEqual([no_id, too_big, text_id, none, number], [None] * 5)
        self.assertEqual((odd_names["status"], odd_names["conclusion"], odd_names["kind"]), ("unknown", None, "unknown"))
        # Not a full ISO time, beyond four digits of year, or no such day: no time.
        self.assertEqual((odd_times["created_at"], odd_times["updated_at"], odd_times["started_at"]), (None, None, None))
        self.assertEqual((offsets["created_at"], offsets["updated_at"]), (None, None))  # GitHub's times are UTC
        self.assertEqual(list_title["kind"], "unknown")


class ClientTests(unittest.TestCase):
    def test_a_deeply_nested_answer_is_a_worker_error(self):
        from stocks import worker_client as wc

        with self.assertRaises(wc.WorkerError) as cm:
            wc.WorkerClient._parse({}, b"[" * 100000)
        self.assertEqual(cm.exception.reason, "the answer is not JSON")
        # A state like that: get_state fails with WorkerError, so the run starts that one ticker fresh.
        deep = b'{"kind": "stocks.state", "version": 1, "x": ' + b"[" * 100000 + b"]" * 100000 + b"}"
        client = wc.WorkerClient("https://worker.example", INGEST, transport=lambda *a: (200, {}, deep))
        with self.assertRaises(wc.WorkerError):
            client.get_state("AAPL")


if __name__ == "__main__":
    unittest.main()
