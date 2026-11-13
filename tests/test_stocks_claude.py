"""Offline tests of scripts/stocks/claude.py: the request shape, the model
allowlist, server-side fallbacks (opus and sonnet only, never haiku), the
price rule and the other checks of an answer, refusals, cut-off answers, API
errors, the spend cap and the cost from usage.iterations. The Anthropic SDK
is only needed for the error classes; those tests skip without it.
"""
import copy
import json
import threading
import unittest
from datetime import date
from types import SimpleNamespace

import stocks_fakes as F
import stocks_support as ss

import config
import stocks
from stocks import claude

CFG = config.load_config(check=("stocks",))["stocks"]
TODAY = date(2026, 11, 13)

try:
    import importlib.util
    HAVE_SDK = importlib.util.find_spec("anthropic") is not None
except ImportError:
    HAVE_SDK = False


def doc():
    return ss.example_document("AAPL")


def ticker_request(model="claude-sonnet-5-5", cfg=CFG):
    d = doc()
    job = claude.ticker_job(d, {"status": "ok", "lessons": []}, {"macro": [], "calendar": []}, cfg, model,
                            "Assessment 1 of 1")
    return job, d


def good_answer(d):
    payload = {"levels": d["levels"], "technicals": d["technicals"]}
    return F.ticker_answer(payload)


class RequestTests(unittest.TestCase):
    def test_the_request_follows_brief_py(self):
        job, _ = ticker_request()
        r = job["request"]
        self.assertEqual(sorted(r), ["betas", "fallbacks", "max_tokens", "messages", "model", "output_config",
                                     "system", "thinking"])
        self.assertEqual(r["thinking"], {"type": "adaptive"})
        self.assertEqual(r["output_config"]["effort"], CFG["effort"])
        self.assertEqual(r["output_config"]["format"], {"type": "json_schema", "schema": claude.TICKER_SCHEMA})
        self.assertEqual(r["max_tokens"], CFG["max_output_tokens"])
        self.assertEqual(r["system"], [{"type": "text", "text": claude.SYSTEM_TICKER,
                                        "cache_control": {"type": "ephemeral"}}])
        self.assertEqual((r["betas"], r["fallbacks"]), (["server-side-fallback-2026-07-01"], "default"))
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, r)
        text = r["messages"][0]["content"]
        self.assertTrue(text.startswith("Here is today's data for one stock as JSON. Write the assessment.\n<stock>\n"))
        self.assertLessEqual(len(text), CFG["max_input_chars"])

    def test_fallbacks_for_opus_and_sonnet_only_never_haiku(self):
        for model, on in (("claude-opus-5-5", True), ("claude-sonnet-5-5", True), ("claude-haiku-5-5", False)):
            r = ticker_request(model)[0]["request"]
            self.assertEqual("fallbacks" in r and "betas" in r, on, model)
        # Even when config.yml says otherwise, Haiku gets none.
        cfg = copy.deepcopy(CFG)
        cfg["models"]["claude-haiku-5-5"]["fallbacks"] = "default"
        self.assertFalse(claude.fallbacks_on("claude-haiku-5-5", cfg))
        self.assertNotIn("fallbacks", ticker_request("claude-haiku-5-5", cfg)[0]["request"])
        self.assertEqual(CFG["models"]["claude-haiku-5-5"]["fallbacks"], "none")
        off = copy.deepcopy(CFG)
        off["models"]["claude-sonnet-5-5"]["fallbacks"] = "none"
        self.assertNotIn("betas", ticker_request("claude-sonnet-5-5", off)[0]["request"])

    def test_the_model_allowlist(self):
        self.assertEqual(claude.check_model("claude-opus-5-5", CFG), "claude-opus-5-5")
        for bad in ("claude-fable-5-2", "gpt-4", None, 5, "claude-sonnet-5-5 "):
            self.assertEqual(claude.check_model(bad, CFG), "claude-sonnet-5-5", bad)
        with self.assertRaises(ValueError):
            claude.build_request(CFG, "claude-fable-5-2", "s", "m", {}, 10)
        self.assertEqual(tuple(CFG["models"]), stocks.ALLOWED_MODELS)
        self.assertEqual(stocks.FALLBACK_MODELS, ("claude-opus-5-5", "claude-sonnet-5-5"))

    def test_the_schema_is_the_same_for_every_ticker(self):
        a = ticker_request()[0]["request"]["output_config"]
        b = claude.ticker_job(ss.example_document("MSFT"), {}, {}, CFG, "claude-sonnet-5-5", "x")["request"]
        self.assertEqual(a, b["output_config"])
        self.assertNotIn("enum", json.dumps(claude.TICKER_SCHEMA["properties"]["targets"]))

    def test_the_message_is_cut_to_fit(self):
        d = doc()
        big = d["holders"]["insiders"]["transactions"][0]
        d["holders"]["insiders"]["transactions"] = [dict(big, owner=f"Owner {i}") for i in range(15)]
        full, cuts = claude.ticker_message(d, {}, {"macro": [], "calendar": []}, 10 ** 9)
        self.assertEqual(cuts, 0)
        small, cuts = claude.ticker_message(d, {}, {"macro": [], "calendar": []}, len(full) - 500)
        self.assertGreater(cuts, 0)
        self.assertLess(len(small), len(full))

    def test_the_overview_schema_lists_the_input_dates_and_links(self):
        ov = ss.example_overview()
        job = claude.overview_job(ov, TODAY, CFG, "claude-sonnet-5-5")
        schema = job["request"]["output_config"]["format"]["schema"]
        dates = schema["properties"]["this_week"]["items"]["properties"]["date"]["enum"]
        # The next 21 days of the calendar only.
        self.assertEqual(dates, sorted({e["date"] for e in ov["calendar"] if e["date"] <= "2026-12-04"}))
        urls = schema["properties"]["news"]["items"]["properties"]["url"]["enum"]
        self.assertEqual(urls, sorted(n["url"] for n in ov["news"]))
        empty = dict(ov, calendar=[], news=[])
        schema = claude.overview_job(empty, TODAY, CFG, "claude-sonnet-5-5")["request"]["output_config"]["format"]["schema"]
        self.assertEqual(schema["properties"]["news"]["items"]["properties"]["url"]["enum"], [claude.PLACEHOLDER])
        self.assertEqual(job["request"]["max_tokens"], CFG["overview_max_output_tokens"])


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.doc = doc()
        self.cands = {c["id"]: c["price"] for c in self.doc["levels"]["candidates"]}

    def test_a_good_answer_keeps_the_model_levels(self):
        a = claude.validate_ticker_answer(good_answer(self.doc), self.doc)
        self.assertEqual((a["source"], a["levels_source"], a["problems"]), ("model", "model", []))
        self.assertEqual(a["entry_zones"][0]["low_ref"], "atr_m2")
        self.assertEqual(a["stop"]["ref"], "atr_m3")

    def test_an_off_candidate_target_falls_back_to_code_levels(self):
        ans = F.ticker_answer({"levels": self.doc["levels"], "technicals": self.doc["technicals"]}, off_candidate=True)
        a = claude.validate_ticker_answer(ans, self.doc)
        self.assertEqual((a["source"], a["levels_source"]), ("model", "code"))
        self.assertIn("One target was not near any level the code proposed, so it was dropped.", a["problems"])
        self.assertIn(claude.LEVELS_FROM_CODE, a["problems"])
        allowed = set(self.cands.values())
        for z in a["entry_zones"]:
            self.assertIn(z["low"], allowed)
            self.assertIn(z["high"], allowed)
        self.assertIn(a["stop"]["price"], allowed)
        self.assertEqual(a["summary"], ans["summary"])  # the texts are kept

    def test_tolerance_and_the_other_rules(self):
        ans = good_answer(self.doc)
        atr = self.doc["levels"]["atr14"]
        ans["entry_zones"][0]["low"] = round(ans["entry_zones"][0]["low"] + 0.05 * atr, 4)  # inside the tolerance
        self.assertEqual(claude.validate_ticker_answer(ans, self.doc)["levels_source"], "model")
        stop_high = good_answer(self.doc)
        stop_high["stop"]["price"] = self.cands["atr_m1"]
        self.assertEqual(claude.validate_ticker_answer(stop_high, self.doc)["levels_source"], "code")
        wide = good_answer(self.doc)
        wide["entry_zones"].insert(0, {"low": self.cands["atr_m3"], "high": self.cands["atr_p3"], "rationale": "x"})
        a = claude.validate_ticker_answer(wide, self.doc)
        self.assertIn("An entry zone was wider than three ATR, so it was dropped.", a["problems"])
        self.assertEqual(a["levels_source"], "model")
        bad_horizon = good_answer(self.doc)
        bad_horizon["targets"][0]["horizon_days"] = 400
        self.assertEqual(claude.validate_ticker_answer(bad_horizon, self.doc)["levels_source"], "code")

    def test_texts_are_plain_and_bounded(self):
        ans = good_answer(self.doc)
        ans["summary"] = "**Strong** trend " + chr(0x2014) + " up. " + "word " * 400
        ans["risks"] = [f"Risk {i}." for i in range(9)] + [""]
        ans["lessons"] = ["One.", "Two.", "Three."]
        a = claude.validate_ticker_answer(ans, self.doc)
        self.assertNotIn("**", a["summary"])
        self.assertNotIn(chr(0x2014), a["summary"])
        self.assertLessEqual(len(a["summary"]), 900)
        self.assertEqual(len(a["risks"]), 5)
        self.assertEqual(a["lessons_added"], ["One.", "Two."])
        empty = good_answer(self.doc)
        empty["summary"] = "  "
        with self.assertRaises(claude.InvalidOutput):
            claude.validate_ticker_answer(empty, self.doc)
        for bad in ([], {"stance": "up"}, {**good_answer(self.doc), "confidence": "total"}):
            with self.assertRaises(claude.InvalidOutput):
                claude.validate_ticker_answer(bad, self.doc)

    def test_the_code_assessment(self):
        a = claude.code_assessment(self.doc, ["A note."])
        self.assertEqual((a["source"], a["levels_source"], a["confidence"], a["summary"]),
                         ("code", "code", "low", claude.CODE_SUMMARY))
        self.assertTrue(a["entry_zones"] and a["targets"] and a["stop"])
        self.assertEqual(a["problems"], ["A note."])

    def test_the_overview_answer_drops_what_is_not_in_the_input(self):
        ov = ss.example_overview()
        job = claude.overview_job(ov, TODAY, CFG, "claude-sonnet-5-5")
        data = {"headline": "A headline " + chr(0x2013) + " here", "summary": "Text.",
                "this_week": [{"date": "2026-12-04", "event": "Jobs report", "why_it_matters": "Rates."},
                              {"date": "2031-01-01", "event": "Invented", "why_it_matters": "x"}],
                "watch": ["a"] * 9, "risks": [],
                "news": [{"url": ov["news"][0]["url"], "why": "y"}, {"url": "https://evil.example/x", "why": "z"},
                         {"url": ov["news"][0]["url"], "why": "again"}]}
        out = job["validate"](data)
        self.assertEqual([x["date"] for x in out["this_week"]], ["2026-12-04"])
        self.assertEqual([x["url"] for x in out["news"]], [ov["news"][0]["url"]])
        self.assertEqual(len(out["watch"]), 5)
        self.assertNotIn(chr(0x2013), out["headline"])
        with self.assertRaises(claude.InvalidOutput):
            job["validate"]({**data, "summary": ""})


class CallTests(unittest.TestCase):
    def jobs(self, n=2, model="claude-sonnet-5-5"):
        d = doc()
        return [claude.ticker_job(d, {}, {}, CFG, model, f"Assessment {i + 1} of {n}") for i in range(n)], d

    def run_with(self, client, jobs, cfg=CFG, budget=None):
        lines = []
        results, cap = claude.run_jobs(client, jobs, cfg, lines.append, budget)
        return results, cap, lines

    def answer_client(self, d, **kw):
        return F.FakeAnthropic(behavior=lambda kind, payload, req: F.message(good_answer(d), **kw))

    def test_an_answer_is_validated_and_priced(self):
        jobs, d = self.jobs(1)
        results, cap, lines = self.run_with(self.answer_client(d), jobs)
        r = results[0]
        self.assertEqual((r["outcome"], r["served_by"]), ("ok", "claude-sonnet-5-5"))
        self.assertEqual(r["cost"], {"input_tokens": 12000, "output_tokens": 3000, "cache_read_tokens": 0,
                                     "cost_usd": 0.054, "cost_estimated": False})
        self.assertEqual(lines[0], "Assessment 1 of 1: claude-sonnet-5-5, end_turn, 12,000 in, 3,000 out, $0.0540.")
        self.assertEqual(lines[-1], "Spend cap: $3.00 per run. Actual cost: $0.0540 for 1 request.")

    def test_refusal_cut_off_and_early_stop_give_no_answer(self):
        jobs, d = self.jobs(1)
        cases = (("refusal", "refusal", SimpleNamespace(category="cyber")), ("max_tokens", "max_tokens", None),
                 ("pause_turn", "stopped", None))
        for stop, outcome, details in cases:
            client = F.FakeAnthropic(behavior=lambda k, p, r: F.message(good_answer(d), stop_reason=stop,
                                                                          stop_details=details))
            results, _, lines = self.run_with(client, jobs)
            self.assertEqual(results[0]["outcome"], outcome, stop)
            self.assertIsNone(results[0]["value"])
            self.assertGreater(results[0]["cost"]["cost_usd"], 0)  # still billed
        self.assertIn("category cyber", self.run_with(F.FakeAnthropic(
            behavior=lambda k, p, r: F.message("{}", stop_reason="refusal",
                                               stop_details=SimpleNamespace(category="cyber"))), jobs)[2][0])
        a, note = claude.assessment_from(d, {"outcome": "refusal"})
        self.assertEqual((a["source"], note), ("code", claude.UNAVAILABLE["refusal"]))

    def test_invalid_json_is_no_answer(self):
        jobs, _ = self.jobs(1)
        results, _, lines = self.run_with(F.FakeAnthropic(behavior=lambda k, p, r: F.message("not json")), jobs)
        self.assertEqual(results[0]["outcome"], "invalid")
        self.assertIn("Not used: the answer did not pass the checks.", lines[0])

    @unittest.skipUnless(HAVE_SDK, "needs the anthropic SDK")
    def test_api_errors_give_code_assessments(self):
        import anthropic

        jobs, _ = self.jobs(1)
        for cls, status in ((anthropic.RateLimitError, 429), (anthropic.InternalServerError, 500),
                            (anthropic.BadRequestError, 400), (anthropic.NotFoundError, 404),
                            (anthropic.PermissionDeniedError, 403)):
            err = F.api_error(cls, status)
            results, cap, _ = self.run_with(F.FakeAnthropic(behavior=lambda k, p, r, e=err: e), jobs)
            self.assertEqual(results[0]["outcome"], "api_error", cls.__name__)
            self.assertEqual(cap.spent, 0.0)
        err = anthropic.APIConnectionError(request=__import__("httpx2").Request("POST", "https://api.anthropic.com"))
        results, _, _ = self.run_with(F.FakeAnthropic(behavior=lambda k, p, r: err), jobs)
        self.assertEqual(results[0]["outcome"], "api_error")

    @unittest.skipUnless(HAVE_SDK, "needs the anthropic SDK")
    def test_a_refused_key_stops_every_later_call(self):
        import anthropic

        jobs, _ = self.jobs(4)
        cfg = dict(CFG, parallel_requests=1)
        client = F.FakeAnthropic(behavior=lambda k, p, r: F.api_error(anthropic.AuthenticationError, 401))
        results, _, _ = self.run_with(client, jobs, cfg)
        self.assertEqual([r["outcome"] for r in results], ["auth"] * 4)
        self.assertEqual(len(client.requests), 1)

    def test_a_slow_call_is_stopped(self):
        jobs, d = self.jobs(1)
        gate = threading.Event()
        self.addCleanup(gate.set)

        def slow(kind, payload, req):
            gate.wait(5)
            return F.message(good_answer(d))

        results, cap, lines = self.run_with(F.FakeAnthropic(behavior=slow), jobs, budget=0.05)
        self.assertEqual(results[0]["outcome"], "too_slow")
        self.assertIn("1 request stopped for taking too long", lines[-1])
        self.assertGreater(cap.held, 0)

    def test_the_spend_cap(self):
        jobs, d = self.jobs(5)
        one = claude.reservation_usd(jobs[0]["request"], CFG)
        # Room for one worst case: after the first call its actual cost plus the next worst case is too much.
        cfg = dict(CFG, max_run_usd=one * 1.2, parallel_requests=1)
        client = self.answer_client(d)
        results, cap, lines = self.run_with(client, [dict(j, kind="ticker") for j in jobs], cfg)
        outcomes = [r["outcome"] for r in results]
        self.assertEqual(outcomes, ["ok"] + ["spend_cap"] * 4)
        self.assertEqual(len(client.requests), 1)
        self.assertIn("Spend cap reached: 4 tickers got code levels only.", lines)
        a, note = claude.assessment_from(d, results[-1])
        self.assertEqual(note, claude.UNAVAILABLE["spend_cap"])
        # The worst case counts max_tokens at the output price, twice with fallbacks.
        r = jobs[0]["request"]
        chars = len(r["messages"][0]["content"]) + len(claude.SYSTEM_TICKER)
        self.assertAlmostEqual(one, CFG["max_output_tokens"] * 10 / 1e6 * 2 + chars / 3 * 2 / 1e6)

    def test_cost_from_iterations_at_each_model(self):
        use = F.usage(iterations=[
            SimpleNamespace(type="message", model="claude-sonnet-5-5", input_tokens=10000, output_tokens=500,
                            cache_read_input_tokens=0, cache_creation_input_tokens=0, cache_creation=None),
            SimpleNamespace(type="fallback_message", model="claude-sonnet-5", input_tokens=10000,
                            output_tokens=2000, cache_read_input_tokens=1000, cache_creation_input_tokens=0,
                            cache_creation=None)])
        cost = claude.usage_cost(use, "claude-sonnet-5-5", CFG, "claude-sonnet-5")
        expected = (10000 * 2 + 500 * 10) / 1e6 + (10000 * 2 + 2000 * 10 + 1000 * 0.2) / 1e6
        self.assertAlmostEqual(cost["cost_usd"], round(expected, 6))
        self.assertEqual((cost["input_tokens"], cost["output_tokens"], cost["cost_estimated"]), (21000, 2500, False))
        unknown = claude.usage_cost(F.usage(1000, 1000), "claude-sonnet-5-5", CFG, "claude-mystery-9")
        self.assertTrue(unknown["cost_estimated"])
        self.assertAlmostEqual(unknown["cost_usd"], (1000 * 5 + 1000 * 25) / 1e6)  # the highest output price listed
        long_haiku = claude.usage_cost(F.usage(120000, 1000), "claude-haiku-5-5", CFG, "claude-haiku-5-5")
        self.assertAlmostEqual(long_haiku["cost_usd"], (120000 * 0.5 + 1000 * 2.5) / 1e6)
        short_haiku = claude.usage_cost(F.usage(50000, 1000), "claude-haiku-5-5", CFG, "claude-haiku-5-5")
        self.assertAlmostEqual(short_haiku["cost_usd"], (50000 * 0.1 + 1000 * 0.5) / 1e6)

    def test_a_fallback_answer_is_marked(self):
        jobs, d = self.jobs(1)
        msg = F.message(good_answer(d), model="claude-sonnet-5")
        results, _, lines = self.run_with(F.FakeAnthropic(behavior=lambda k, p, r: msg), jobs)
        self.assertEqual((results[0]["outcome"], results[0]["served_by"]), ("ok", "claude-sonnet-5"))
        self.assertIn("A fallback model wrote the answer.", lines[0])

    def test_nothing_is_sent_without_a_client_or_when_too_large(self):
        jobs, _ = self.jobs(2)
        results, cap = claude.run_all(None, jobs, CFG, lambda s: None)
        self.assertEqual([r["outcome"] for r in results], ["no_key", "no_key"])
        big = dict(jobs[0], too_large=True)
        lines = []
        results, _ = claude.run_all(F.FakeAnthropic(), [big], CFG, lines.append)
        self.assertEqual(results[0]["outcome"], "too_large")
        self.assertEqual(lines, ["Assessment 1 of 2: not sent, the data was too large."])


if __name__ == "__main__":
    unittest.main()
