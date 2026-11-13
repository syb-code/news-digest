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
             Skipped when node or miniflare is missing.

Tickers here are public examples (AAPL, BRK-B) or made up.
"""
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
const GITHUB = `export default { async fetch(request, env) {
  const log = JSON.parse((await env.LOG.get("log")) || "[]");
  const body = request.method === "GET" ? null : await request.text();
  log.push({ url: request.url, method: request.method, auth: request.headers.get("authorization"),
             agent: request.headers.get("user-agent"), body });
  await env.LOG.put("log", JSON.stringify(log));
  const answers = JSON.parse((await env.LOG.get("answers")) || "[]");
  const answer = answers.length ? answers.shift() : 204;
  await env.LOG.put("answers", JSON.stringify(answers));
  if (typeof answer === "object") return new Response(JSON.stringify(answer.body), { status: answer.status });
  return new Response(answer === 204 ? null : JSON.stringify({ message: "Server said " + answer }), { status: answer });
} };`;
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
    const seen = {};
    if (c.scheduled) {
      const worker = await mf.getWorker("main");
      const res = await worker.scheduled({ scheduledTime: new Date(c.scheduled), cron: "45 12,13 * * *" });
      seen.outcome = res.outcome;
    } else {
      const headers = { ...(c.headers || {}) };
      const init = { method: c.method || "GET", headers };
      if (c.body !== undefined) init.body = typeof c.body === "string" ? c.body : JSON.stringify(c.body);
      if (c.repeat) init.body = "x".repeat(c.repeat);
      const r = await mf.dispatchFetch("https://worker.example" + c.path, init);
      seen.status = r.status;
      seen.guard = r.headers.get("x-guard");
      seen.cors = r.headers.get("access-control-allow-origin");
      seen.cache = r.headers.get("cache-control");
      const text = await r.text();
      try { seen.body = JSON.parse(text); } catch { seen.body = text; }
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
    ]},
    {"name": "github refuses", "kv": True, "bindings": FULL, "cases": [
        {"name": "refresh", "method": "POST", "path": "/stocks/refresh", "headers": key(), "answers": [403],
         "keys": ["stocks:refresh"]},
        {"name": "refresh, 5xx then taken", "method": "POST", "path": "/stocks/refresh", "headers": key(),
         "answers": [502, 204], "keys": ["stocks:refresh"]},
    ]},
]


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
        calls = self.r("refresh")["github"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"],
                         "https://api.github.com/repos/owner/news-digest/actions/workflows/stocks.yml/dispatches")
        self.assertEqual(json.loads(calls[0]["body"]), {"ref": "main", "inputs": {"reason": "manual"}})
        self.assertEqual(calls[0]["auth"], f"Bearer {TOKEN}")
        seen = self.r("refresh again")
        self.assertEqual((seen["status"], seen["body"]["error"], seen["guard"]),
                         (429, "a run was started recently", None))
        self.assertTrue(1700 <= seen["body"]["retry_after_seconds"] <= 1800)
        self.assertEqual(seen["github"], [])
        self.assertEqual(self.status("refresh", "no repo")[0], 500)
        refused = self.r("refresh", "github refuses")
        self.assertEqual((refused["status"], refused["kv"]["stocks:refresh"]), (502, None))
        retried = self.r("refresh, 5xx then taken", "github refuses")
        self.assertEqual((retried["status"], len(retried["github"])), (202, 2))
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
        for leak in ("AAPL", "BRK-B", ACCESS, INGEST, TOKEN, "A lesson."):
            self.assertNotIn(leak, logs)


if __name__ == "__main__":
    unittest.main()
