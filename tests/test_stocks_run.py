"""Offline tests of the stock run (scripts/stocks/run.py and worker_client.py).

The whole run, phase by phase as the workflow runs it, against a fake web of
public sources (fixtures and synthetic prices), a fake Worker with an
in-memory KV and a fake Anthropic client. The checks that matter most:

  - the log holds no ticker, company name, CIK or CUSIP except on the
    "::add-mask::" lines, and those come first in each phase; no price is
    printed;
  - each ticker's Claude request holds nothing of another ticker;
  - nothing is written into the repository, and private file names hold a
    position, never a ticker;
  - one ticker failing never stops the others.

Tickers are public examples (AAPL, MSFT) or made up (EXMPL).
"""
import contextlib
import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import stocks_fakes as F
import stocks_support as ss

import stocks
from stocks import run, worker_client
from stocks.analysis import learner

TICKERS = ["AAPL", "MSFT", "EXMPL"]
PRIVATE = ["AAPL", "MSFT", "EXMPL", "Apple Inc.", "MICROSOFT CORP", "320193", "0000320193", "789019", "0000789019"]
PHASES = ("config", "fetch", "analyze", "claude", "upload")
ENV = {"WORKER_URL": "https://worker.example.workers.dev", "STOCKS_INGEST_KEY": F.INGEST_KEY,
       "SEC_USER_AGENT": F.USER_AGENT, "TIINGO_API_KEY": "tiingo-key-for-tests", "ANTHROPIC_API_KEY": "sk-test"}


def repo_snapshot():
    """Every file of the repository with its size and modification time."""
    out = {}
    for base, dirs, files in os.walk(ss.ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for name in files:
            p = os.path.join(base, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


class RunCase(unittest.TestCase):
    """A scratch folder outside the repository, the fakes, and a runner."""

    tickers = TICKERS

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stocks-run-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = os.path.join(self.tmp, "stocks")
        os.makedirs(os.path.join(self.dir, "public"))
        with open(os.path.join(self.dir, "public", "house.json"), "w", encoding="utf-8") as f:
            json.dump(F.house_bundle("AAPL"), f)
        self.items = os.path.join(self.tmp, "items.json")
        with open(self.items, "w", encoding="utf-8") as f:
            json.dump({"items": [{"title": "Fed officials weigh the next move", "url": "https://news.example.com/fed",
                                  "source": "Example News", "published": "2026-11-12T20:00:00+00:00",
                                  "themes": ["Macro/Fed"], "summary": "Rates."}]}, f)
        patcher = mock.patch.object(run, "ITEMS_JSON", self.items)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bars = {"AAPL": F.series_bars(7, 180.0), "MSFT": F.series_bars(9, 300.0)}
        self.web = F.FakeWeb(prices=self.bars, spy=F.series_bars(11, 420.0))
        self.worker = F.FakeWorker(self.tickers)
        self.claude = F.FakeAnthropic()
        self.env = dict(ENV)

    def deps(self, **kw):
        args = {"environ": self.env, "transport": self.web, "worker_transport": self.worker,
                "anthropic_client": self.claude, "sleep": lambda s: None}
        args.update(kw)
        return run.Deps(**args)

    def phase(self, name, now=F.NOW, deps=None, extra=()):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = run.main([name, "--dir", self.dir, "--now", now, *extra], deps=deps or self.deps())
        return code, buf.getvalue()

    def run_all(self, now=F.NOW, deps=None):
        out = {}
        for name in PHASES:
            code, text = self.phase(name, now, deps)
            self.assertEqual(code, 0, f"{name}:\n{text}")
            out[name] = text
        return out


class FullRunTests(RunCase):
    def test_the_whole_run_saves_every_document(self):
        before = set(repo_snapshot())
        written = []
        real = run.write_json

        def recording(path, doc):
            written.append(os.path.realpath(path))
            return real(path, doc)

        with mock.patch.object(run, "write_json", recording):
            logs = self.run_all()
        self.assertEqual(sorted(set(repo_snapshot()) - before), [], "the run wrote into the repository")
        self.assertTrue(written)
        base = os.path.realpath(self.dir) + os.sep
        self.assertEqual([p for p in written if not p.startswith(base)], [])
        kv = self.worker.kv
        for t in TICKERS:
            self.assertIn(f"stocks:ticker:{t}", kv)
            self.assertIn(f"stocks:state:{t}", kv)
            learner.check_state(t, kv[f"stocks:state:{t}"], tuple(TICKERS))
        doc = kv["stocks:ticker:AAPL"]
        self.assertIn(doc["status"], ("ok", "partial"))
        self.assertEqual((doc["llm"], doc["model"]), (True, "claude-sonnet-5-5"))
        self.assertEqual((doc["assessment"]["source"], doc["assessment"]["levels_source"]), ("model", "model"))
        self.assertEqual(doc["disclaimer"], stocks.DISCLAIMER)
        self.assertEqual(doc["data_as_of"], F.AS_OF)
        self.assertEqual(doc["valuation"]["status"], "ok")
        self.assertEqual(doc["valuation"]["eps_ttm"], 8.97)
        self.assertEqual(doc["holders"]["congress"]["status"], "ok")
        self.assertEqual(doc["holders"]["congress"]["label"], "Members of Congress, best effort")
        self.assertGreater(len(doc["holders"]["insiders"]["transactions"]), 0)
        self.assertEqual(doc["shorts"]["status"], "ok")
        self.assertEqual(doc["learner"]["status"], "ok")
        self.assertLessEqual(stocks.size_of(doc), stocks.DOC_MAX_BYTES)
        msft = kv["stocks:ticker:MSFT"]
        self.assertEqual(msft["holders"]["congress"]["status"], "none")
        self.assertEqual(msft["shorts"]["status"], "not_available")
        self.assertEqual(msft["status"], "partial")
        self.assertEqual(kv["stocks:ticker:EXMPL"]["status"], "unknown_ticker")
        ov = kv["stocks:overview"]
        self.assertEqual(ov["summary"]["source"], "model")
        self.assertEqual(ov["run"]["tickers"], 3)
        self.assertAlmostEqual(ov["run"]["cost_usd"], 3 * 0.054, places=6)
        self.assertIn("Saved 3 analyses and the market overview, 3 learning states.", logs["upload"])
        self.assertIn("Total cost of the run: $0.1620.", logs["upload"])

    def test_the_log_holds_no_private_value(self):
        logs = self.run_all()
        prices = set()
        for bars in self.bars.values():
            prices |= F.prices_text(bars)
        for phase, text in logs.items():
            lines = text.splitlines()
            masks = [i for i, line in enumerate(lines) if line.startswith("::add-mask::")]
            self.assertTrue(masks, phase)
            self.assertEqual(masks, list(range(len(masks))), f"{phase}: the mask lines must come first")
            for line in lines[len(masks):]:
                self.assertEqual(ss_find(line, PRIVATE), [], f"{phase}: {line}")
                for p in prices:
                    self.assertNotIn(p, line, f"{phase}: a price in {line!r}")
                self.assertNotIn(self.dir, line)
                self.assertNotIn("tiingo-key-for-tests", line)
                self.assertNotIn(F.INGEST_KEY, line)
                self.assertNotIn(F.USER_AGENT, line)
                self.assertNotIn(chr(0x2014), line)
                self.assertTrue(line[:1].isupper() or line.startswith("[warn] "), line)
        self.assertIn("::add-mask::AAPL", logs["config"].splitlines()[0])
        for value in ("Apple Inc.", "0000320193", "320193"):
            self.assertIn(f"::add-mask::{value}", logs["fetch"])

    def test_private_files_are_named_by_position_and_stay_outside_the_repository(self):
        self.run_all()
        names = []
        for base, _dirs, files in os.walk(os.path.join(self.dir, "private")):
            names += [os.path.relpath(os.path.join(base, f), self.dir) for f in files]
            self.assertEqual(os.stat(base).st_mode & 0o777, 0o700)
        self.assertIn(os.path.join("private", "data", "00.json"), names)
        for name in names:
            for t in TICKERS:
                self.assertNotIn(t, name)
        self.assertEqual(os.stat(os.path.join(self.dir, "private", "config.json")).st_mode & 0o777, 0o600)

    def test_each_claude_request_holds_only_its_own_ticker(self):
        self.run_all()
        reqs = self.claude.requests
        self.assertEqual(len(reqs), 3)  # the overview and two tickers (the unknown one has no prices)
        own = {"AAPL": ["AAPL", "Apple Inc.", "320193"], "MSFT": ["MSFT", "MICROSOFT CORP", "789019"]}
        seen = set()
        for req in reqs:
            kind, payload = F.payload_of(req)
            text = req["messages"][0]["content"]
            if kind == "market":
                self.assertEqual(ss_find(text, PRIVATE), [])
                continue
            t = payload["ticker"]
            seen.add(t)
            for other, values in own.items():
                if other != t:
                    self.assertEqual(ss_find(text, values + ["EXMPL"]), [], f"{t} request holds {other}")
            self.assertEqual(payload["learner"]["status"], "ok")
            self.assertNotIn("predictions", json.dumps(payload["learner"]))
        self.assertEqual(seen, {"AAPL", "MSFT"})

    def test_requests_to_the_outside_carry_the_right_identity(self):
        self.run_all()
        for r in self.worker.requests:
            self.assertEqual(r["headers"]["Authorization"], f"Bearer {F.INGEST_KEY}")
            self.assertEqual(r["headers"]["User-Agent"], "news-digest-stocks/1.0")
            self.assertNotIn("Origin", r["headers"])
        for r in self.web.requests:
            self.assertEqual(r["headers"]["User-Agent"], F.USER_AGENT)
            self.assertNotIn("tiingo-key-for-tests", r["url"])
            if "tiingo" in r["url"]:
                self.assertEqual(r["headers"]["Authorization"], "Token tiingo-key-for-tests")
            else:
                self.assertNotIn("Authorization", r["headers"])

    def test_the_next_day_learns_on_the_saved_state(self):
        self.run_all()
        first = copy.deepcopy(self.worker.kv["stocks:state:AAPL"])
        # One more trading day of prices, then the next morning's run.
        nxt = F.series_bars(7, 180.0, n=F.BARS + 1, end="2026-11-13")
        self.bars["AAPL"][:] = nxt
        self.web.requests.clear()
        self.run_all(now="2026-11-16T13:50:00+00:00")
        second = self.worker.kv["stocks:state:AAPL"]
        self.assertEqual(len(second["predictions"]), len(first["predictions"]) + 1)
        self.assertEqual(second["last_as_of"], "2026-11-13")
        self.assertEqual(second["created"], first["created"])
        tiingo = [r["url"] for r in self.web.requests if "/tiingo/daily/AAPL/" in r["url"]]
        self.assertEqual(len(tiingo), 1)
        self.assertIn("startDate=2026-11-05", tiingo[0])  # a delta: the last cached day minus 7
        form4 = [r for r in self.web.requests if r["url"].endswith("/form4.xml")]
        self.assertEqual(form4, [])  # every filing was read on the first day
        # A second run on the same day replaces that day's call, it does not add one.
        self.run_all(now="2026-11-16T14:20:00+00:00")
        self.assertEqual(len(self.worker.kv["stocks:state:AAPL"]["predictions"]), len(second["predictions"]))

    def test_the_lessons_are_bounded_and_never_name_another_ticker(self):
        self.claude.lessons = ["This stock fell after CPI days twice.", "It trades like MSFT does.",
                               "A third lesson that is never kept."]
        self.run_all()
        lessons = [x["text"] for x in self.worker.kv["stocks:state:AAPL"]["lessons"]]
        self.assertEqual(lessons, ["This stock fell after CPI days twice."])
        # A lesson may name its own ticker.
        msft = [x["text"] for x in self.worker.kv["stocks:state:MSFT"]["lessons"]]
        self.assertEqual(msft, ["This stock fell after CPI days twice.", "It trades like MSFT does."])
        self.assertEqual(self.worker.kv["stocks:ticker:AAPL"]["assessment"]["lessons_added"],
                         ["This stock fell after CPI days twice."])


def ss_find(text, values):
    from stocks import privacy

    return privacy.find_private(text, values)


class FailureTests(RunCase):
    def test_one_failing_ticker_never_stops_the_others(self):
        real = run.fetch_ticker

        def flaky(ctx, i, ticker, state):
            if i == 1:
                raise RuntimeError("boom for " + ticker)
            return real(ctx, i, ticker, state)

        with mock.patch.object(run, "fetch_ticker", flaky):
            logs = self.run_all()
        self.assertIn("[warn] Fetching ticker 2 of 3 stopped (RuntimeError); it is analyzed on its saved data.",
                      logs["fetch"])
        self.assertNotIn("boom", "".join(logs.values()))
        self.assertIn(self.worker.kv["stocks:ticker:AAPL"]["status"], ("ok", "partial"))
        # No saved state yet, so no prices: the others are not held up.
        self.assertEqual(self.worker.kv["stocks:ticker:MSFT"]["status"], "no_prices")
        self.assertIn("The data could not be updated today, so this analysis uses the data of the last run.",
                      self.worker.kv["stocks:ticker:MSFT"]["notes"])

    def test_a_stopped_fetch_uses_the_saved_data(self):
        self.run_all()
        real = run.fetch_ticker

        def flaky(ctx, i, ticker, state):
            if i == 0:
                raise RuntimeError("boom")
            return real(ctx, i, ticker, state)

        with mock.patch.object(run, "fetch_ticker", flaky):
            self.run_all(now="2026-11-13T15:00:00+00:00")
        doc = self.worker.kv["stocks:ticker:AAPL"]
        self.assertIn(doc["status"], ("ok", "partial"))
        self.assertEqual(doc["data_as_of"], F.AS_OF)
        self.assertEqual(doc["assessment"]["source"], "model")

    def test_a_failed_analysis_is_logged_by_type_only(self):
        real = run.analyze_ticker

        def flaky(i, ticker, *a, **k):
            if ticker == "AAPL":
                raise ZeroDivisionError("secret detail")
            return real(i, ticker, *a, **k)

        self.phase("config")
        self.phase("fetch")
        with mock.patch.object(run, "analyze_ticker", flaky):
            code, text = self.phase("analyze")
        self.assertEqual(code, 0)
        self.assertIn("Analyzed 1 of 3 tickers, 1 failed (ZeroDivisionError).", text)
        self.assertTrue(text.splitlines()[-1].startswith("Analyzed 1 ticker: 1 with a model"), text)
        self.assertNotIn("secret detail", text)

    def test_an_unreadable_state_is_not_overwritten(self):
        self.worker.fail["/stocks/ingest/state/MSFT"] = 500
        logs = self.run_all()
        self.assertIn("[warn] Could not read the saved state of ticker 2 of 3 (HTTP 500).", logs["config"])
        self.assertNotIn("stocks:state:MSFT", self.worker.kv)
        self.assertIn("stocks:ticker:MSFT", self.worker.kv)
        puts = [r["url"] for r in self.worker.requests if r["method"] == "PUT"]
        self.assertFalse([u for u in puts if u.endswith("/state/MSFT")])

    def test_a_state_of_another_ticker_starts_again(self):
        other = learner.new_state("MSFT", F.NOW)
        self.worker.kv["stocks:state:AAPL"] = other
        logs = self.run_all()
        self.assertIn("[warn] The saved state of ticker 1 of 3 did not pass the checks, so it starts again.",
                      logs["config"])
        self.assertEqual(self.worker.kv["stocks:state:AAPL"]["ticker"], "AAPL")

    def test_no_claude_key_keeps_the_code_levels(self):
        self.env.pop("ANTHROPIC_API_KEY")
        logs = self.run_all(deps=self.deps(anthropic_client=None))
        doc = self.worker.kv["stocks:ticker:AAPL"]
        self.assertEqual((doc["llm"], doc["model"], doc["assessment"]["source"]), (False, None, "code"))
        self.assertIn("The assessment is unavailable today: no Claude key is set for the run.", doc["notes"])
        self.assertTrue(doc["assessment"]["entry_zones"])
        self.assertEqual(self.worker.kv["stocks:overview"]["summary"]["source"], "none")
        self.assertIn("ANTHROPIC_API_KEY is not set", logs["claude"])
        self.assertIn("Spend cap: $3.00 per run. Actual cost: $0.0000 for 0 requests. No request was sent.",
                      logs["claude"])

    def test_a_refused_key_stops_every_call(self):
        self.claude.behavior = lambda kind, payload, req: F.api_error(__import__("anthropic").AuthenticationError, 401)
        logs = self.run_all()
        self.assertIn("the API key was refused", logs["claude"])
        for t in ("AAPL", "MSFT"):
            self.assertEqual(self.worker.kv[f"stocks:ticker:{t}"]["assessment"]["source"], "code")
        self.assertLessEqual(len(self.claude.requests), 3)

    def test_the_settings_and_the_worker_must_be_readable(self):
        self.worker.fail["/stocks/ingest/config"] = 401
        code, text = self.phase("config")
        self.assertEqual(code, 1)
        self.assertIn("Stopped: Could not read the settings from the Worker: unauthorized.", text)
        self.worker.fail.clear()
        self.worker.kv["stocks:config"]["tickers"] = []
        code, text = self.phase("config")
        self.assertEqual(code, 1)
        self.assertIn("Stopped: The ticker list is empty.", text)
        self.env["WORKER_URL"] = "https://worker.example.workers.dev/path"
        code, text = self.phase("config")
        self.assertEqual(code, 1)
        self.assertNotIn("worker.example", text)

    def test_fetch_needs_the_contact_address(self):
        self.phase("config")
        self.env.pop("SEC_USER_AGENT")
        code, text = self.phase("fetch")
        self.assertEqual(code, 1)
        lines = text.splitlines()
        self.assertTrue(all(line.startswith("::add-mask::") for line in lines[:-1]))
        self.assertTrue(lines[-1].startswith("Stopped: SEC_USER_AGENT is not set."))

    def test_without_tiingo_the_saved_prices_are_used(self):
        self.run_all()
        self.env.pop("TIINGO_API_KEY")
        logs = self.run_all(now="2026-11-13T23:30:00+00:00")
        self.assertIn("TIINGO_API_KEY is not set", logs["fetch"])
        doc = self.worker.kv["stocks:ticker:AAPL"]
        self.assertIn(f"Prices could not be updated today; the chart ends on {F.AS_OF}.", doc["notes"])
        self.assertEqual(doc["data_as_of"], F.AS_OF)

    def test_the_folder_inside_the_repository_is_refused(self):
        inside = os.path.join(ss.ROOT, "stocks-scratch-for-a-test")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = run.main(["config", "--dir", inside], deps=self.deps())
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(inside))
        self.assertIn("Stopped: The working directory must be outside the repository.", buf.getvalue())

    def test_upload_checks_the_states_again(self):
        for name in ("config", "fetch", "analyze", "claude"):
            self.phase(name)
        path = os.path.join(self.dir, "private", "out", "state", "00.json")
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        st["spy"] = []                                   # another ticker's or a shared input's data
        st["lessons"].append({"date": "2026-11-12", "text": "Unlike MSFT, it held up.", "source": "model"})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)
        code, text = self.phase("upload")
        self.assertEqual(code, 0)
        self.assertIn("[warn] The learning state of ticker 1 of 3 was not saved (IsolationError).", text)
        self.assertNotIn("stocks:state:AAPL", self.worker.kv)

    def test_upload_drops_a_lesson_that_names_another_ticker(self):
        for name in ("config", "fetch", "analyze", "claude"):
            self.phase(name)
        path = os.path.join(self.dir, "private", "out", "state", "00.json")
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        st["lessons"] += [{"date": "2026-11-12", "text": "Unlike MSFT, it held up.", "source": "model"},
                          {"date": "2026-11-12", "text": "Microsoft news moved it.", "source": "model"},
                          {"date": "2026-11-12", "text": "A close below the 50 day average led lower.",
                           "source": "model"}]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)
        code, text = self.phase("upload")
        self.assertEqual(code, 0)
        saved = [l["text"] for l in self.worker.kv["stocks:state:AAPL"]["lessons"]]
        self.assertIn("A close below the 50 day average led lower.", saved)
        self.assertFalse(any("MSFT" in t or "Microsoft" in t for t in saved))


class LocalRunTests(RunCase):
    def test_local_writes_to_the_out_folder(self):
        out = os.path.join(self.tmp, "out")
        code, text = self.phase("local", extra=("--tickers", "aapl,msft", "--out", out))
        self.assertEqual(code, 0, text)
        for name in ("ticker/AAPL.json", "state/AAPL.json", "ticker/MSFT.json", "overview.json"):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
        self.assertEqual(self.worker.requests, [])

    def test_local_refuses_an_out_folder_in_the_repository(self):
        code, text = self.phase("local", extra=("--tickers", "AAPL", "--out", os.path.join(ss.ROOT, "out")))
        self.assertEqual(code, 1)
        self.assertIn("--out must be outside the repository.", text)


# ---------------------------------------------------------------- worker_client.py

class WorkerClientTests(unittest.TestCase):
    def client(self, transport, **kw):
        return worker_client.WorkerClient("https://worker.example.workers.dev", F.INGEST_KEY, transport=transport,
                                          sleep=lambda s: None, **kw)

    def test_the_address_must_be_a_bare_https_origin(self):
        for bad in ("http://worker.example", "https://worker.example/path", "https://user@worker.example", "",
                    "https://worker.example?x=1", "worker.example"):
            with self.assertRaises(worker_client.WorkerError) as cm:
                worker_client.WorkerClient(bad, "key")
            self.assertNotIn("worker.example", cm.exception.reason)
        with self.assertRaises(worker_client.WorkerError):
            worker_client.WorkerClient("https://worker.example", "")

    def test_the_config_is_checked(self):
        fake = F.FakeWorker(["aapl", "AAPL", "brk.b", "not a ticker", 5] + [f"Z{chr(65 + i)}" for i in range(26)] +
                            ["QQA", "QQB", "QQC", "QQD", "QQE"], model="gpt-4",
                            overrides={"AAPL": {"cusips": ["037833100", "bad"], "names": ["Apple"]},
                                       "NOPE": {"cusips": ["037833100"]}})
        c = self.client(fake)
        cfg = c.get_config(models={m: {} for m in stocks.ALLOWED_MODELS}, default="claude-sonnet-5-5")
        self.assertEqual(cfg["tickers"][:2], ["AAPL", "BRK-B"])
        self.assertEqual(len(cfg["tickers"]), stocks.MAX_TICKERS)
        self.assertEqual(cfg["model"], "claude-sonnet-5-5")
        self.assertEqual(cfg["overrides"], {"AAPL": {"cusips": ["037833100"], "names": ["Apple"]}})
        self.assertEqual(c.last_config_info, {"dropped": 2, "over_limit": 3, "model_reset": True})

    def test_retries_once_on_5xx_and_never_sends_an_oversized_body(self):
        calls = []

        def flaky(method, url, headers, body, timeout):
            calls.append(url)
            return (503, {}, b"") if len(calls) == 1 else (200, {}, b'{"ok": true, "bytes": 12}')

        c = self.client(flaky)
        self.assertEqual(c.put_overview({"kind": "stocks.overview", "version": 1}), 12)
        self.assertEqual(len(calls), 2)
        with self.assertRaises(worker_client.WorkerError) as cm:
            c.put_overview({"kind": "stocks.overview", "pad": "x" * stocks.OVERVIEW_MAX_BYTES})
        self.assertEqual(cm.exception.reason, "too large")
        self.assertEqual(len(calls), 2)

    def test_errors_hold_no_ticker_or_body(self):
        def refuse(method, url, headers, body, timeout):
            return 409, {}, b'{"error": "AAPL is not in the list"}'

        with self.assertRaises(worker_client.WorkerError) as cm:
            self.client(refuse).put_state("AAPL", {"kind": "stocks.state"})
        self.assertEqual((cm.exception.reason, cm.exception.status), ("HTTP 409", 409))

    def test_a_missing_state_is_none_and_the_path_is_quoted(self):
        seen = []

        def none(method, url, headers, body, timeout):
            seen.append((method, url, headers))
            return 404, {}, b'{"error": "no state"}'

        self.assertIsNone(self.client(none).get_state("BRK-B"))
        method, url, headers = seen[0]
        self.assertEqual(url, "https://worker.example.workers.dev/stocks/ingest/state/BRK-B")
        self.assertNotIn("Origin", headers)
        self.assertEqual(headers["Content-Type"], "application/json")
        with self.assertRaises(worker_client.WorkerError):
            self.client(none).get_state("brk/b")  # only the normalized spelling is sent
        self.assertNotIn(F.INGEST_KEY, repr(self.client(none)))


class ReviewFixesRunTest(RunCase):
    """Review fixes in run.py: fail soft per source, results kept as they
    come, no data from a reused folder, and the staleness note."""

    def run_phases(self):
        out = {}
        for name in PHASES:
            code, text = self.phase(name)
            out[name] = (code, text)
        return out

    def test_a_market_source_bug_never_stops_the_run(self):
        from stocks.data import calendar, fred, news
        for target, name in ((calendar, "fetch_calendar"), (fred, "fetch_many"), (news, "collect")):
            with self.subTest(source=name):
                self.worker.kv = {k: v for k, v in self.worker.kv.items() if not k.startswith("stocks:ticker:")}
                with mock.patch.object(target, name, side_effect=KeyError("x")):
                    out = self.run_phases()
                self.assertEqual({k: v[0] for k, v in out.items()}, dict.fromkeys(PHASES, 0))
                self.assertIn("stocks:ticker:AAPL", self.worker.kv)

    def test_an_overview_bug_keeps_every_ticker_document(self):
        from stocks.data import fred
        with mock.patch.object(fred, "macro_table", side_effect=TypeError("x")):
            out = self.run_phases()
        self.assertEqual(out["analyze"][0], 0)
        self.assertIn("[warn] The market overview could not be built (TypeError)", out["analyze"][1])
        for t in TICKERS:
            self.assertIn(f"stocks:ticker:{t}", self.worker.kv)
        self.assertEqual(self.worker.kv["stocks:overview"]["macro"], [])

    def test_a_ticker_source_bug_keeps_its_fresh_prices(self):
        from stocks.data import finra
        with mock.patch.object(finra, "refresh_short_volume", side_effect=TypeError("x")), \
                mock.patch.object(finra, "refresh_short_interest", side_effect=TypeError("y")):
            out = self.run_phases()
        self.assertEqual(out["upload"][0], 0)
        for t in ("AAPL", "MSFT"):
            self.assertIn(self.worker.kv[f"stocks:ticker:{t}"]["status"], ("ok", "partial"))
            self.assertEqual(self.worker.kv[f"stocks:ticker:{t}"]["header"]["last_close"],
                             stocks.round_price(self.bars[t][-1]["close"]))

    def test_a_reused_folder_never_mixes_tickers(self):
        self.run_all()
        self.worker.kv["stocks:config"]["tickers"] = ["MSFT", "AAPL", "EXMPL"]
        self.worker.kv.pop("stocks:state:MSFT", None)
        real_fetch = run.fetch_ticker

        def boom(ctx, i, ticker, state):
            if i == 0:
                raise RuntimeError("x")
            return real_fetch(ctx, i, ticker, state)

        with mock.patch.object(run, "fetch_ticker", boom), \
                mock.patch.object(run, "saved_ticker_data", side_effect=RuntimeError("y")):
            for name in PHASES:
                self.phase(name)
        doc = self.worker.kv["stocks:ticker:MSFT"]
        self.assertEqual(doc["status"], "error")
        self.assertNotEqual(doc["header"].get("cik"), 320193)
        st = self.worker.kv.get("stocks:state:MSFT")
        self.assertTrue(st is None or (st.get("identity") or {}).get("cik") != 320193)

    def test_claude_results_are_written_as_they_come_and_late_calls_are_not_sent(self):
        for name in ("config", "fetch", "analyze"):
            self.phase(name)
        clock = {"t": 0.0}
        written = []
        real = run.apply_assessment

        def tracking(*a, **k):
            written.append((a[2], a[6]["outcome"]))
            clock["t"] += 600.0              # each answer takes a long time
            return real(*a, **k)

        with open(os.path.join(ss.ROOT, "config.yml"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("parallel_requests: 4", text)
        one_at_a_time = os.path.join(self.tmp, "config.yml")
        with open(one_at_a_time, "w", encoding="utf-8") as f:
            f.write(text.replace("parallel_requests: 4", "parallel_requests: 1"))
        with mock.patch.object(run, "apply_assessment", tracking):
            code, text = self.phase("claude", deps=self.deps(clock=lambda: clock["t"]),
                                    extra=("--config", one_at_a_time))
        self.assertEqual(code, 0)
        # The overview (no file write tracked) and the first ticker were asked; the clock then
        # passed the point where a call could end inside the step's time, so the second was not sent.
        self.assertIn("Out of time: 1 ticker got code levels only.", text)
        self.assertEqual(written, [(0, "ok"), (1, "out_of_time")])   # each written when its result came
        run_json = json.load(open(os.path.join(self.dir, "private", "run.json"), encoding="utf-8"))
        self.assertGreater(run_json["cost_usd"], 0.0)
        code, text = self.phase("upload")
        self.assertEqual(code, 0)
        kv = self.worker.kv
        self.assertEqual((kv["stocks:ticker:AAPL"]["llm"], kv["stocks:ticker:MSFT"]["llm"]), (True, False))
        self.assertEqual(kv["stocks:ticker:MSFT"]["assessment"]["source"], "code")

    def test_a_shortened_insider_list_says_so(self):
        txns = [{"date": "2026-10-01", "filed": "2026-10-02", "code": "S", "shares": 10.0, "value": 100.0,
                 "owner": "o", "derivative": False}]
        block = run.insiders_block({"transactions": txns, "trimmed_before": "2026-09-15"}, 365, "2026-11-12")
        self.assertEqual(block["summary"]["since"], "2026-09-15")
        self.assertIn("only those since 2026-09-15 are kept", block["note"])
        plain = run.insiders_block({"transactions": txns}, 365, "2026-11-12")
        self.assertEqual((plain["summary"]["since"], plain["note"]), ("2025-11-12", run.INSIDER_NOTE))

    def test_staleness_note_says_how_old(self):
        eps = {"status": "ok", "eps_ttm": 4.0, "basis": "four_quarters", "currency": "USD",
               "latest_end": "2026-01-31", "latest_filed": "2026-03-01", "quarters": []}
        bars = [{"date": "2026-11-12", "raw_close": 100.0, "split": 1.0}]
        v = run.valuation_block(eps, bars, {}, {}, "2026-11-12")
        self.assertTrue(v["stale"])
        self.assertEqual(v["note"], "The latest earnings in the SEC data are for a period that ended 285 days ago.")
        fresh = run.valuation_block(dict(eps, latest_end="2026-09-30"), bars, {}, {}, "2026-11-12")
        self.assertIsNone(fresh["note"])


if __name__ == "__main__":
    unittest.main()
