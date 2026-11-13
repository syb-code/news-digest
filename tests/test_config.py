"""Offline tests for scripts/config.py and the committed config.yml."""
import os
import shutil
import tempfile
import unittest

import support  # noqa: F401  (puts scripts/ on sys.path)

import config


class CommittedConfigTests(unittest.TestCase):
    def test_the_repository_config_loads(self):
        cfg = config.load_config()
        self.assertEqual(cfg["timezone"], "America/Los_Angeles")
        self.assertEqual(cfg["schedule"], {"morning": "05:45", "noon": "12:00"})
        self.assertEqual(set(cfg["feeds"]) >= set(config.FEED_LISTS), True)
        self.assertEqual([y["handle"] for y in cfg["feeds"]["youtube"]],
                         ["AIDailyBrief", "Monetary-Matters"])
        self.assertEqual(len(cfg["feeds"]["rss"]), 4)
        self.assertEqual(cfg["feeds"]["rss"][1]["label"], "Lenny\u2019s Newsletter (Substack)")
        self.assertEqual(cfg["feeds"]["nitter_instances"], [])
        self.assertEqual(cfg["themes"]["Tech/Earnings"][-1], "COIN")
        self.assertEqual(cfg["learn"], {
            "model": "claude-opus-5-5", "effort": "medium", "max_output_tokens": 12000,
            "max_input_chars": 40000, "topics_min": 3, "topics_max": 5, "lookback_hours": 36,
            "price_per_mtok": {"input": 4.0, "output": 20.0}, "fallbacks": "default",
            "prices": {
                "claude-opus-5-5": {"input": 4.0, "output": 20.0},
                "claude-opus-5": {"input": 5.0, "output": 25.0},
                "claude-opus-4-8": {"input": 5.0, "output": 25.0},
            },
        })
        # The price of the configured model is the same in both places.
        self.assertEqual(cfg["learn"]["prices"][cfg["learn"]["model"]], cfg["learn"]["price_per_mtok"])

    def test_the_old_files_are_gone(self):
        for old in ("feeds.yml", "themes.yml"):
            self.assertFalse(os.path.exists(os.path.join(support.ROOT, old)), old)

    def test_no_secrets_or_addresses_in_the_config(self):
        with open(config.CONFIG_PATH, encoding="utf-8") as f:
            text = f.read()
        for marker in ("@gmail", "workers.dev", "sk-ant", "ghp_", "github_pat_"):
            self.assertNotIn(marker, text)
        self.assertNotIn(chr(0x2014), text)
        self.assertNotIn(chr(0x2013), text)


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="news-digest-config-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def load(self, text):
        path = os.path.join(self.tmp, "config.yml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return config.load_config(path)

    def assert_rejected(self, text, *words):
        with self.assertRaises(config.ConfigError) as cm:
            self.load(text)
        for word in words:
            self.assertIn(word, str(cm.exception))

    def test_empty_file_gets_every_default(self):
        cfg = self.load("")
        self.assertEqual(cfg, config.DEFAULTS)
        cfg["feeds"]["rss"].append("x")  # a copy, not the defaults themselves
        self.assertEqual(config.DEFAULTS["feeds"]["rss"], [])

    def test_partial_sections_are_completed(self):
        cfg = self.load("feeds:\n  rss:\n    - feed_url: https://a.example/feed\nlearn:\n  effort: high\n"
                        "  price_per_mtok:\n    input: 2\n")
        self.assertEqual(cfg["feeds"]["rss"], [{"feed_url": "https://a.example/feed"}])
        self.assertEqual(cfg["feeds"]["youtube"], [])
        self.assertEqual(cfg["learn"]["effort"], "high")
        self.assertEqual(cfg["learn"]["model"], "claude-opus-5-5")
        self.assertEqual(cfg["learn"]["price_per_mtok"], {"input": 2.0, "output": 20.0})
        self.assertEqual(cfg["schedule"], {"morning": "05:45", "noon": "12:00"})

    def test_null_lists_and_single_values(self):
        cfg = self.load("feeds:\n  youtube:\n  nitter_instances: https://n.example\nthemes:\n  A: one\n  B:\n")
        self.assertEqual(cfg["feeds"]["youtube"], [])
        self.assertEqual(cfg["feeds"]["nitter_instances"], ["https://n.example"])
        self.assertEqual(cfg["themes"], {"A": ["one"], "B": []})

    def test_unquoted_time_is_read_back(self):
        # YAML 1.1 reads 05:45 without quotes as the number 345.
        self.assertEqual(self.load("schedule:\n  morning: 05:45\n")["schedule"]["morning"], "05:45")

    def test_unknown_keys_are_kept(self):
        cfg = self.load("extra:\n  a: 1\nlearn:\n  note: hi\n")
        self.assertEqual(cfg["extra"], {"a": 1})
        self.assertEqual(cfg["learn"]["note"], "hi")

    def test_fallbacks(self):
        self.assertIsNone(self.load("learn:\n  fallbacks: none\n")["learn"]["fallbacks"])
        self.assertIsNone(self.load("learn:\n  fallbacks:\n")["learn"]["fallbacks"])
        self.assertIsNone(self.load("learn:\n  fallbacks: false\n")["learn"]["fallbacks"])
        self.assertEqual(self.load("learn:\n  fallbacks: [claude-opus-5]\n")["learn"]["fallbacks"], ["claude-opus-5"])
        self.assert_rejected("learn:\n  fallbacks: 3\n", "learn.fallbacks")
        self.assert_rejected("learn:\n  fallbacks: []\n", "learn.fallbacks")

    def test_prices_by_model(self):
        # Left out: the built-in table. Written: exactly what is written, as numbers.
        self.assertEqual(self.load("learn:\n  model: claude-opus-5\n")["learn"]["prices"],
                         config.DEFAULTS["learn"]["prices"])
        self.assertEqual(self.load("learn:\n  prices:\n")["learn"]["prices"], config.DEFAULTS["learn"]["prices"])
        cfg = self.load("learn:\n  prices:\n    claude-haiku-4-5: {input: 1, output: 5, cache_read: 0.1}\n"
                        "    ' claude-opus-5 ':\n      input: 5\n      output: 25\n")
        self.assertEqual(cfg["learn"]["prices"], {
            "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1},
            "claude-opus-5": {"input": 5.0, "output": 25.0},
        })
        self.assertEqual(cfg["learn"]["price_per_mtok"], {"input": 4.0, "output": 20.0})  # still the fallback
        # An empty map turns the table off, so price_per_mtok prices every model.
        self.assertEqual(self.load("learn:\n  prices: {}\n")["learn"]["prices"], {})
        cfg["learn"]["prices"]["claude-opus-5"]["input"] = 99  # a copy, not the defaults themselves
        self.assertEqual(config.DEFAULTS["learn"]["prices"]["claude-opus-5"]["input"], 5.0)

    def test_bad_prices_name_the_model_and_the_key(self):
        cases = [
            ("learn:\n  prices: 4\n", "learn.prices must be a mapping"),
            ("learn:\n  prices:\n    claude-opus-5: 5\n", "learn.prices.claude-opus-5 must be a mapping"),
            ("learn:\n  prices:\n    claude-opus-5: {input: 5}\n", "learn.prices.claude-opus-5.output is missing"),
            ("learn:\n  prices:\n    claude-opus-5: {input: 5, output: cheap}\n", "learn.prices.claude-opus-5.output"),
            ("learn:\n  prices:\n    claude-opus-5: {input: -1, output: 25}\n", "learn.prices.claude-opus-5.input"),
            ("learn:\n  prices:\n    claude-opus-5: {input: true, output: 25}\n", "learn.prices.claude-opus-5.input"),
            ("learn:\n  prices:\n    claude-opus-5: {input: 5, output: 25, cache_read: x}\n", "cache_read"),
            ("learn:\n  prices:\n    5: {input: 5, output: 25}\n", "keyed by model id"),
        ]
        for text, message in cases:
            with self.subTest(text=text):
                self.assert_rejected(text, message)

    def test_wrong_kinds_name_the_key(self):
        cases = [
            ("- a\n- b\n", "top level"),
            ("timezone: Mars/Olympus\n", "timezone"),
            ("timezone: 5\n", "timezone"),
            ("schedule:\n  morning: '25:00'\n", "schedule.morning"),
            ("schedule: daily\n", "schedule"),
            ("feeds:\n  rss: https://a.example/feed\n", "feeds.rss"),
            ("themes:\n  A:\n    x: 1\n", "themes.A"),
            ("learn:\n  effort: extreme\n", "learn.effort"),
            ("learn:\n  model: ''\n", "learn.model"),
            ("learn:\n  max_output_tokens: 0\n", "learn.max_output_tokens"),
            ("learn:\n  topics_max: 4.5\n", "learn.topics_max"),
            ("learn:\n  lookback_hours: true\n", "learn.lookback_hours"),
            ("learn:\n  topics_min: 6\n", "topics_min"),
            ("learn:\n  price_per_mtok:\n    output: -1\n", "price_per_mtok.output"),
            ("learn: [1]\n", "learn"),
        ]
        for text, key in cases:
            with self.subTest(text=text):
                self.assert_rejected(text, key)

    def test_check_limits_which_sections_can_fail(self):
        text = "learn:\n  effort: extreme\nfeeds:\n  rss: [{feed_url: https://a.example/feed}]\n"
        self.assert_rejected(text, "learn.effort")
        path = os.path.join(self.tmp, "config.yml")
        cfg = config.load_config(path, check=("feeds", "themes"))
        self.assertEqual(cfg["feeds"]["rss"], [{"feed_url": "https://a.example/feed"}])
        self.assertEqual(cfg["feeds"]["youtube"], [])
        self.assertEqual(cfg["learn"], {"effort": "extreme"})  # as written, not checked
        self.assertEqual(cfg["schedule"], config.DEFAULTS["schedule"])  # missing, so filled in
        with self.assertRaises(config.ConfigError):
            config.load_config(path, check=("learn",))

    def test_missing_or_broken_file(self):
        with self.assertRaises(config.ConfigError) as cm:
            config.load_config(os.path.join(self.tmp, "nope.yml"))
        self.assertIn("not found", str(cm.exception))
        self.assert_rejected("feeds: [\n", "could not be read")



class StocksSectionTests(unittest.TestCase):
    """The stocks section (contract 14): defaults, the model allowlist, and the
    rule that the ticker list never lives in config.yml."""

    setUp = LoadConfigTests.setUp
    load = LoadConfigTests.load
    assert_rejected = LoadConfigTests.assert_rejected

    def stocks(self, text):
        return self.load(text)["stocks"]

    def test_the_committed_section_matches_the_defaults(self):
        committed = config.load_config(check=("stocks",))["stocks"]
        self.assertEqual(committed, config.DEFAULTS["stocks"])
        import stocks

        self.assertEqual(tuple(committed["models"]), stocks.ALLOWED_MODELS)
        self.assertEqual(committed["model"], stocks.DEFAULT_MODEL)
        self.assertEqual(committed["models"]["claude-sonnet-5-5"],
                         {"input": 2.0, "output": 10.0, "cache_read": 0.1, "fallbacks": "default"})
        self.assertEqual(committed["models"]["claude-opus-5-5"],
                         {"input": 4.0, "output": 20.0, "cache_read": 0.2, "fallbacks": "default"})
        self.assertEqual(committed["models"]["claude-haiku-5-5"],
                         {"input": 0.1, "output": 0.5, "cache_read": 0.01, "fallbacks": "none"})
        self.assertEqual(len(committed["funds"]), 16)
        self.assertEqual(len({f["cik"] for f in committed["funds"]}), 16)
        self.assertEqual([m["id"] for m in committed["macro"]][:3], ["fed_funds", "dgs2", "dgs10"])

    def test_a_partial_section_is_completed(self):
        cfg = self.stocks("stocks:\n  effort: high\n  models:\n    claude-opus-5-5: {input: 5}\n")
        self.assertEqual(cfg["effort"], "high")
        self.assertEqual(cfg["models"]["claude-opus-5-5"]["input"], 5.0)
        self.assertEqual(cfg["models"]["claude-opus-5-5"]["output"], 20.0)
        self.assertEqual(cfg["max_run_usd"], 3.0)

    def test_haiku_never_gets_fallbacks(self):
        cfg = self.stocks("stocks:\n  models:\n    claude-haiku-5-5: {fallbacks: default}\n")
        self.assertEqual(cfg["models"]["claude-haiku-5-5"]["fallbacks"], "none")

    def test_the_ticker_list_is_refused_anywhere_in_the_section(self):
        for text in ("stocks:\n  tickers: [EXMPL]\n", "stocks:\n  trading_days:\n    tickers: []\n",
                     "stocks:\n  funds:\n    - {name: A, cik: 1, tickers: [EXMPL]}\n"):
            with self.subTest(text=text):
                self.assert_rejected(text, "The ticker list is private: keep it in the Worker, not in config.yml.")

    def test_bad_values_name_the_key(self):
        cases = [
            ("stocks:\n  model: claude-fable-5-2\n", "stocks.model"),
            ("stocks:\n  models:\n    claude-fable-5-2: {input: 1, output: 1, cache_read: 1}\n", "stocks.models"),
            ("stocks:\n  models:\n    claude-opus-5-5: {fallbacks: sometimes}\n", "fallbacks"),
            ("stocks:\n  models:\n    claude-opus-5-5: {output: -1}\n", "stocks.models.claude-opus-5-5.output"),
            ("stocks:\n  effort: extreme\n", "stocks.effort"),
            ("stocks:\n  max_output_tokens: 0\n", "stocks.max_output_tokens"),
            ("stocks:\n  parallel_requests: true\n", "stocks.parallel_requests"),
            ("stocks:\n  max_run_usd: -1\n", "stocks.max_run_usd"),
            ("stocks:\n  horizons: [5, 5]\n", "stocks.horizons"),
            ("stocks:\n  horizons: [0]\n", "stocks.horizons"),
            ("stocks:\n  horizons: []\n", "stocks.horizons"),
            ("stocks:\n  funds: [{name: A, cik: 1}, {name: B, cik: 1}]\n", "twice"),
            ("stocks:\n  funds: [{name: A, cik: x}]\n", "stocks.funds[0].cik"),
            ("stocks:\n  macro: [{id: x, label: X, series: [bad id], transform: level}]\n", "stocks.macro.x.series"),
            ("stocks:\n  macro: [{id: x, label: X, series: [DGS10], transform: cube}]\n", "transform"),
            ("stocks:\n  macro: [{id: x, label: X, series: [DGS10], transform: range}]\n", "two series"),
            ("stocks:\n  fx:\n    EUR: {series: DEXUSEU, quote: sideways}\n", "stocks.fx.EUR.quote"),
            ("stocks:\n  fx:\n    euro: {series: DEXUSEU, quote: usd_per_unit}\n", "currency codes"),
            ("stocks:\n  trading_days:\n    data_ready: '25:00'\n", "stocks.trading_days.data_ready"),
        ]
        for text, key in cases:
            with self.subTest(text=text):
                self.assert_rejected(text, key)


if __name__ == "__main__":
    unittest.main()
