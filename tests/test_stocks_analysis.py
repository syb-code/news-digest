"""Offline tests for the stock section's analysis layer (scripts/stocks/analysis/):
indicators, levels and candidates, short volume, macro impact. The learner has
its own file (test_stocks_learner.py).

Reference values come from tests/fixtures/stocks/analysis_reference.json
(pandas, statsmodels and scikit-learn, computed outside the repository) and
from hand-computed series below.
"""
import math
import os
import re
import unittest

import stocks_support as ss  # puts scripts/ on sys.path

from stocks import dumps
from stocks.analysis import indicators, levels, macro_impact, mean, median, quantile, shorts, solve

REF = ss.fixture_json("analysis_reference.json")
ANALYSIS_DIR = os.path.join(ss.ROOT, "scripts", "stocks", "analysis")


def bar(d, close, high=None, low=None, volume=1000.0, open_=None):
    return {"date": d, "open": close if open_ is None else open_, "high": close if high is None else high,
            "low": close if low is None else low, "close": close, "volume": volume, "raw_close": close,
            "raw_volume": int(volume), "div": 0.0, "split": 1.0}


def series_bars(closes, start="2026-01-05", spread=0.0):
    dates = ss.weekdays(start, len(closes))
    return [bar(d, c, c + spread, c - spread) for d, c in zip(dates, closes)]


class ReferenceIndicatorTest(unittest.TestCase):
    """Indicator values against pandas on the fixture's 80 bars."""

    @classmethod
    def setUpClass(cls):
        cls.bars = REF["bars"]
        cls.closes = [b["close"] for b in cls.bars]
        cls.exp = REF["indicators"]

    def assertSeries(self, got, want, tol=1e-6):
        self.assertEqual(len(got), len(want))
        for i, (g, w) in enumerate(zip(got, want)):
            if w is None:
                self.assertIsNone(g, f"index {i}")
            else:
                self.assertIsNotNone(g, f"index {i}")
                self.assertAlmostEqual(g, w, delta=tol, msg=f"index {i}")

    def test_sma_and_ema(self):
        self.assertSeries(indicators.sma(self.closes, 20), self.exp["sma20"])
        self.assertSeries(indicators.ema(self.closes, 12), self.exp["ema12"])
        self.assertSeries(indicators.ema(self.closes, 26), self.exp["ema26"])

    def test_macd(self):
        m = indicators.macd(self.closes)
        self.assertSeries(m["macd"], self.exp["macd"])
        self.assertSeries(m["signal"], self.exp["signal"])
        self.assertSeries(m["hist"], self.exp["hist"])
        self.assertIsNone(m["signal"][32])
        self.assertIsNotNone(m["signal"][33])        # 26 + 9 - 2: the signal seeds on its first 9 values

    def test_bollinger(self):
        b = indicators.bollinger(self.closes)
        self.assertSeries(b["mid"], self.exp["bb_mid"])
        self.assertSeries(b["upper"], self.exp["bb_upper"])
        self.assertSeries(b["lower"], self.exp["bb_lower"])
        i = 50
        self.assertAlmostEqual(b["pct_b"][i], (self.closes[i] - b["lower"][i]) / (b["upper"][i] - b["lower"][i]))
        self.assertAlmostEqual(b["bandwidth_pct"][i], (b["upper"][i] - b["lower"][i]) / b["mid"][i] * 100)

    def test_rsi_and_atr(self):
        self.assertSeries(indicators.rsi(self.closes, 14), self.exp["rsi14"])
        self.assertSeries(indicators.atr(self.bars, 14), self.exp["atr14"])

    def test_rsi_matches_wilders_published_example(self):
        tb = REF["rsi_textbook"]
        got = indicators.rsi(tb["closes"], 14)
        self.assertEqual([round(v, 2) for v in got[14:]], tb["published"])
        self.assertTrue(all(v is None for v in got[:14]))


class HandComputedIndicatorTest(unittest.TestCase):
    def test_sma_with_gaps(self):
        self.assertEqual(indicators.sma([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])
        self.assertEqual(indicators.sma([1, None, 3, 4, 5, 6], 2), [None, None, None, 3.5, 4.5, 5.5])
        self.assertEqual(indicators.sma([], 3), [])
        with self.assertRaises(ValueError):
            indicators.sma([1.0], 0)

    def test_ema_seed_and_recursion(self):
        # seed at index 2 = mean(1, 2, 3) = 2; alpha = 0.5
        self.assertEqual(indicators.ema([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])
        # leading gaps: seeded on the first n defined values in a row
        self.assertEqual(indicators.ema([None, None, 2, 4, 6, 10], 2), [None, None, None, 3.0, 5.0, 8.333333333333332])

    def test_rsi_edges(self):
        self.assertEqual(indicators.rsi([10.0] * 20, 14)[14:], [50.0] * 6)
        self.assertEqual(indicators.rsi([float(i) for i in range(20)], 14)[14:], [100.0] * 6)
        self.assertEqual(indicators.rsi([float(20 - i) for i in range(20)], 14)[14:], [0.0] * 6)
        self.assertEqual(indicators.rsi([1.0, 2.0], 14), [None, None])

    def test_atr_by_hand(self):
        bars = [bar("2026-01-05", 10, 11, 9), bar("2026-01-06", 12, 13, 11), bar("2026-01-07", 11, 12, 8)]
        # true ranges: 2 (first bar: high - low), max(2, 3, 1) = 3, max(4, 0, 4) = 4
        self.assertEqual(indicators.true_range(bars), [2, 3, 4])
        self.assertEqual(indicators.atr(bars, 2), [None, 2.5, 3.25])     # (2.5 * 1 + 4) / 2

    def test_log_return(self):
        out = indicators.log_return([100.0, 110.0, None, 121.0], 1)
        self.assertIsNone(out[0])
        self.assertAlmostEqual(out[1], math.log(1.1))
        self.assertIsNone(out[2])
        self.assertIsNone(out[3])
        self.assertAlmostEqual(indicators.log_return([100.0, 1.0, 121.0], 2)[2], math.log(1.21))

    def test_flat_bollinger_has_no_pct_b(self):
        b = indicators.bollinger([5.0] * 25)
        self.assertEqual(b["upper"][24], 5.0)
        self.assertIsNone(b["pct_b"][24])
        self.assertEqual(b["bandwidth_pct"][24], 0.0)

    def test_macd_cross(self):
        self.assertEqual(indicators.macd_cross([None, -1.0, -0.5, 0.2, 0.4]), ("bullish", 1))
        self.assertEqual(indicators.macd_cross([1.0, 0.5, 0.0, -0.1]), ("bearish", 0))
        hist = [-1.0, 1.0] + [1.0] * 10
        self.assertEqual(indicators.macd_cross(hist), (None, None))      # 10 bars ago is outside the window
        self.assertEqual(indicators.macd_cross([-1.0, 1.0] + [1.0] * 8), ("bullish", 8))

    def test_trend(self):
        self.assertEqual(indicators.trend(110, 105, 100, 300), "up")
        self.assertEqual(indicators.trend(90, 95, 100, 300), "down")
        self.assertEqual(indicators.trend(104, 105, 100, 300), "sideways")
        self.assertEqual(indicators.trend(110, 105, None, 120), "up")
        self.assertIsNone(indicators.trend(110, None, None, 30))


class TechnicalsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars, cls.spy, _, _ = ss.example_inputs(n=700)
        cls.tech = indicators.technicals(cls.bars, cls.spy)

    def test_shape_follows_the_contract(self):
        t = self.tech
        for key in ("as_of", "close", "sma", "ema", "distance_pct", "rsi14", "macd", "bollinger", "atr14", "atr_pct",
                    "range_52w", "rel_strength", "volume", "trend"):
            self.assertIn(key, t)
        self.assertEqual(set(t["sma"]), {"20", "50", "200"})
        self.assertEqual(set(t["ema"]), {"12", "26", "50"})
        self.assertEqual(set(t["macd"]), {"macd", "signal", "hist", "cross", "cross_days_ago"})
        self.assertEqual(set(t["bollinger"]), {"mid", "upper", "lower", "pct_b", "bandwidth_pct"})
        self.assertEqual(set(t["volume"]), {"last", "avg20", "avg50", "ratio20"})
        self.assertEqual(t["range_52w"]["days"], 252)
        self.assertEqual(t["as_of"], self.bars[-1]["date"])
        dumps(t)

    def test_values_are_consistent(self):
        t, closes = self.tech, [b["close"] for b in self.bars]
        self.assertAlmostEqual(t["sma"]["20"], mean(closes[-20:]), places=3)
        self.assertAlmostEqual(t["distance_pct"]["sma50"], round((closes[-1] / t["sma"]["50"] - 1) * 100, 2), places=1)
        window = self.bars[-252:]
        self.assertEqual(t["range_52w"]["high"], round(max(b["high"] for b in window), 4))
        self.assertEqual(t["range_52w"]["low"], round(min(b["low"] for b in window), 4))
        self.assertLessEqual(t["range_52w"]["from_high_pct"], 0)
        self.assertGreaterEqual(t["range_52w"]["from_low_pct"], 0)
        rs = t["rel_strength"]
        ret20 = (closes[-1] / closes[-21] - 1) * 100
        spy20 = (self.spy[-1]["close"] / self.spy[-21]["close"] - 1) * 100
        self.assertAlmostEqual(rs["ret_20d_pct"], round(ret20, 2))
        self.assertAlmostEqual(rs["spy_ret_20d_pct"], round(spy20, 2))
        self.assertAlmostEqual(rs["excess_20d_pct"], round(ret20 - spy20, 2), places=1)
        vols = [b["volume"] for b in self.bars]
        self.assertEqual(t["volume"]["ratio20"], round(vols[-1] / mean(vols[-20:]), 2))
        self.assertIn(t["trend"], ("up", "down", "sideways"))

    def test_short_history_and_no_benchmark(self):
        bars = self.bars[-30:]
        t = indicators.technicals(bars, None)
        self.assertIsNone(t["sma"]["200"])
        self.assertIsNone(t["sma"]["50"])
        self.assertIsNone(t["trend"])
        self.assertEqual(t["range_52w"]["days"], 30)
        self.assertIsNone(t["rel_strength"]["spy_ret_20d_pct"])
        self.assertIsNotNone(t["rel_strength"]["ret_20d_pct"])
        self.assertIsNone(t["rel_strength"]["ret_252d_pct"])
        dumps(t)

    def test_chart(self):
        ch = indicators.chart(self.bars, 400)
        lengths = {len(v) for v in ch.values()}
        self.assertEqual(lengths, {400})
        self.assertEqual(ch["dates"][-1], self.bars[-1]["date"])
        self.assertIsNotNone(ch["sma200"][0])          # computed on the whole history, then sliced
        short = indicators.chart(self.bars[:120], 400)
        self.assertEqual(len(short["dates"]), 120)
        self.assertIsNone(short["sma200"][-1])
        dumps(ch)

    def test_clean_bars_sorts_and_drops_bad_rows(self):
        rows = [bar("2026-01-07", 3.0), {"date": "2026-01-06", "close": None}, bar("2026-01-05", 1.0),
                bar("2026-01-07", 4.0), {"date": "2026-01-08", "close": 5.0}]
        out = indicators.clean_bars(rows)
        self.assertEqual([b["date"] for b in out], ["2026-01-05", "2026-01-07", "2026-01-08"])
        self.assertEqual(out[1]["close"], 4.0)
        self.assertEqual(out[2]["high"], 5.0)
        self.assertEqual(out[2]["volume"], 0.0)


class LevelsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A sawtooth between 90 and 110 with a period of 20 bars, ending at 100.
        closes = []
        for i in range(260):
            k = i % 20
            closes.append(90 + k * 2 if k <= 10 else 110 - (k - 10) * 2)
        closes += [100.0]
        cls.bars = series_bars(closes, spread=0.5)
        cls.tech = indicators.technicals(cls.bars, None)

    def test_pivots_and_clusters(self):
        lv = levels.find_levels(self.bars, self.tech["atr14"])
        self.assertTrue(lv["supports"])
        self.assertTrue(lv["resistances"])
        s1, r1 = lv["supports"][0], lv["resistances"][0]
        self.assertEqual(s1["id"], "s1")
        self.assertAlmostEqual(s1["price"], 89.5, places=4)
        self.assertAlmostEqual(r1["price"], 110.5, places=4)
        self.assertGreaterEqual(s1["touches"], 10)
        self.assertLessEqual(len(lv["supports"]), 4)
        close = self.bars[-1]["close"]
        self.assertTrue(all(s["price"] < close * 0.995 for s in lv["supports"]))
        self.assertTrue(all(r["price"] > close * 1.005 for r in lv["resistances"]))

    def test_pivot_ties_go_to_the_earliest_bar(self):
        closes = [10, 11, 12, 13, 14, 20, 20, 14, 13, 12, 11, 10, 10, 10, 10, 10, 10]
        bars = series_bars(closes)
        highs = [p for p in levels.pivots(bars, 252, 5) if p[2] == "high"]
        self.assertEqual(highs, [(bars[5]["date"], 20, "high")])
        lows = levels.pivots(bars, 252, 5)
        self.assertTrue(all(p[0] < bars[-5]["date"] for p in lows))      # the last k bars cannot be pivots

    def test_cluster(self):
        pts = [("2026-01-01", 100.0, "low"), ("2026-02-01", 100.8, "low"), ("2026-03-01", 103.0, "high")]
        out = levels.cluster(pts, 1.0)
        self.assertEqual([c["touches"] for c in out], [2, 1])
        self.assertAlmostEqual(out[0]["price"], 100.4)
        self.assertEqual(out[0]["last_touch"], "2026-02-01")

    def test_candidates(self):
        ex = {"horizon": 20, "n": 200, "scope": "all", "drawdown_q": {"25": -0.08, "50": -0.04, "75": -0.01},
              "runup_q": {"25": 0.02, "50": 0.05, "75": 0.09}}
        lv = levels.levels_block(self.bars, self.tech, ex)
        ids = [c["id"] for c in lv["candidates"]]
        for want in ("s1", "r1", "sma20", "sma50", "sma200", "bb_upper", "bb_lower", "atr_m1", "atr_m2", "atr_m3",
                     "atr_p1", "atr_p2", "atr_p3", "hi52", "lo52", "dd25", "dd50", "dd75", "ru25", "ru50", "ru75"):
            self.assertIn(want, ids)
        by = {c["id"]: c for c in lv["candidates"]}
        close, a = self.tech["close"], self.tech["atr14"]
        self.assertEqual(by["atr_m2"]["price"], round(close - 2 * a, 4))
        self.assertEqual(by["dd25"]["price"], round(close * 0.92, 4))
        self.assertEqual(by["dd25"]["label"], "Deep 20 day dip (25th percentile)")
        self.assertEqual(by["ru50"]["label"], "Typical 20 day rise (median)")
        self.assertEqual(by["sma50"]["label"], "50 day average")
        self.assertEqual(by["atr_m2"]["label"], "Close minus 2 ATR")
        self.assertTrue(re.match(r"^Support 1, \d+ touch(es)?$", by["s1"]["label"]))
        self.assertTrue(all(c["price"] > 0 for c in lv["candidates"]))
        self.assertEqual(lv["tolerance"], {"pct": 0.5, "atr_share": 0.10})
        self.assertEqual(lv["atr14"], a)
        similar = dict(ex, scope="similar")
        lab = {c["id"]: c["label"] for c in levels.candidates(self.bars, self.tech, lv, similar)}
        self.assertEqual(lab["dd75"], "Shallow 20 day dip in similar setups (75th percentile)")
        none = levels.levels_block(self.bars, self.tech, None)
        self.assertFalse(any(c["kind"] in ("drawdown_quantile", "runup_quantile") for c in none["candidates"]))

    def test_tolerance_and_match(self):
        self.assertAlmostEqual(levels.tolerance(100.0, 2.0), 0.5)          # 0.5 percent beats 0.1 x ATR
        self.assertAlmostEqual(levels.tolerance(100.0, 10.0), 1.0)         # 0.1 x ATR beats 0.5 percent
        self.assertAlmostEqual(levels.tolerance(100.0, None), 1.0)         # 1 percent without ATR
        cands = [{"id": "a", "kind": "support", "label": "", "price": 100.0},
                 {"id": "b", "kind": "support", "label": "", "price": 100.6},
                 {"id": "c", "kind": "support", "label": "", "price": 120.0}]
        self.assertEqual(levels.match_candidate(100.25, cands, 2.0)["id"], "a")
        self.assertEqual(levels.match_candidate(100.4, cands, 2.0)["id"], "b")
        self.assertIsNone(levels.match_candidate(110.0, cands, 2.0))
        self.assertEqual(levels.match_candidate(100.3, cands, 2.0)["id"], "a")   # equal distance: list order
        self.assertIsNone(levels.match_candidate(None, cands, 2.0))

    def test_fallback_plan_uses_only_candidates(self):
        for seed in (1, 2, 3, 4, 5):
            bars = ss.synthetic_bars(400, seed=seed)
            tech = indicators.technicals(bars, None)
            lv = levels.levels_block(bars, tech, None)
            plan = levels.fallback_plan(lv["candidates"], tech["close"], tech["atr14"])
            prices = {c["price"] for c in lv["candidates"]}
            by = {c["id"]: c for c in lv["candidates"]}
            zone = plan["entry_zones"][0]
            self.assertEqual((zone["low_ref"], zone["high_ref"]), ("atr_m2", "atr_m1"))
            self.assertIn(zone["low"], prices)
            self.assertIn(zone["high"], prices)
            self.assertEqual(plan["stop"]["ref"], "atr_m3")
            self.assertIn(plan["stop"]["price"], prices)
            t = plan["targets"][0]
            self.assertIn(t["price"], prices)
            self.assertEqual(by[t["ref"]]["price"], t["price"])
            self.assertGreaterEqual(t["price"], tech["close"] + tech["atr14"])
            self.assertEqual(t["horizon_days"], 20)
            self.assertLess(plan["stop"]["price"], zone["low"])
            self.assertEqual(plan["source"], "code")
            for c in lv["candidates"]:
                if c["kind"] in ("resistance", "runup_quantile") and tech["close"] + tech["atr14"] <= c["price"]:
                    self.assertLessEqual(t["price"], c["price"])
            self.assertEqual(zone["rationale"], "Two to one ATR below the last close.")

    def test_fallback_plan_without_atr(self):
        bars = series_bars([10.0, 10.5, 11.0])
        tech = indicators.technicals(bars, None)
        lv = levels.levels_block(bars, tech, None)
        self.assertEqual(levels.fallback_plan(lv["candidates"], tech["close"], tech["atr14"]),
                         {"entry_zones": [], "targets": [], "stop": None, "source": "code"})


class ShortsTest(unittest.TestCase):
    def test_ratio_series(self):
        rows = [{"date": "2026-11-11", "short_volume": 5.0, "total_volume": 10.0},
                {"date": "2026-11-10", "short_volume": 1.0, "total_volume": 4.0},
                {"date": "2026-11-12", "short_volume": 1.0, "total_volume": 0.0}]
        self.assertEqual(shorts.ratio_series(rows), [("2026-11-10", 0.25), ("2026-11-11", 0.5)])
        cache = {"fields": ["date", "short_volume", "exempt_volume", "total_volume"],
                 "rows": [["2026-11-10", 1.0, 0.0, 4.0]]}
        self.assertEqual(shorts.ratio_series(cache), [("2026-11-10", 0.25)])

    def test_zscore_by_hand(self):
        z = shorts.zscore([1, 2, 3, 4, 5, 10], window=5, min_n=3)
        self.assertEqual(z["n"], 5)
        self.assertEqual(z["mean"], 3.0)
        self.assertEqual(z["std"], round(math.sqrt(2.5), 4))
        self.assertEqual(z["z"], round(7 / math.sqrt(2.5), 2))
        self.assertEqual(z["percentile"], 100.0)
        z = shorts.zscore([5, 1, 2, 3, 4, 2.5], window=4, min_n=3)    # the window excludes the latest and is 4 long
        self.assertEqual(z["n"], 4)
        self.assertEqual(z["mean"], 2.5)
        self.assertEqual(z["percentile"], 50.0)
        self.assertIsNone(shorts.zscore([1, 2, 3], window=5, min_n=3)["z"])
        self.assertIsNone(shorts.zscore([2, 2, 2, 2, 3], window=5, min_n=3)["z"])     # no spread
        self.assertEqual(shorts.zscore([])["n"], 0)

    def test_labels(self):
        cases = {2.0: "unusually high", 1.99: "high", 1.0: "high", 0.99: "normal", -0.99: "normal", -1.0: "low",
                 -1.99: "low", -2.0: "unusually low", None: "not enough history"}
        for z, label in cases.items():
            self.assertEqual(shorts.z_label(z), label, z)

    def test_summarize_finra_fixture(self):
        by_date = {}
        for r in ss.fixture_json("finra_regsho_daily_aapl.json"):
            row = by_date.setdefault(r["tradeReportDate"], {"date": r["tradeReportDate"], "short_volume": 0.0,
                                                            "exempt_volume": 0.0, "total_volume": 0.0})
            row["short_volume"] += r["shortParQuantity"]
            row["exempt_volume"] += r["shortExemptParQuantity"]
            row["total_volume"] += r["totalParQuantity"]
        rows = list(by_date.values())
        interest = [{"settlement_date": "2026-10-30", "short_interest": 120763539.0, "previous": 128753092.0,
                     "change_pct": -6.21, "avg_daily_volume": 39858260.0, "days_to_cover": 3.03},
                    {"settlement_date": "2026-10-15", "short_interest": 128753092.0, "previous": 1.0,
                     "change_pct": 1.0, "avg_daily_volume": 1.0, "days_to_cover": 1.0}]
        out = shorts.summarize(rows, interest)
        self.assertEqual(out["status"], "ok")
        i = out["daily"]["dates"].index("2026-11-10")
        self.assertEqual(out["daily"]["ratio"][i], round(5044508.820252 / 9399064.247494, 4))
        self.assertEqual(out["daily"]["total_volume"][i], 9399064.0)
        self.assertEqual(out["latest"]["date"], out["daily"]["dates"][-1])
        self.assertEqual(out["latest"]["label"], "not enough history")       # the fixture holds a few weeks only
        self.assertIsNone(out["latest"]["z"])
        self.assertEqual(out["interest"]["latest"]["settlement_date"], "2026-10-30")
        self.assertIn(shorts.NOTE_FINRA, out["notes"])
        self.assertEqual(len({len(v) for v in out["daily"].values()}), 1)
        dumps(out)

    def test_summarize_long_history(self):
        dates = ss.weekdays("2025-06-02", 300)
        rows = ss.synthetic_short_volume(dates)
        out = shorts.summarize(rows, [])
        self.assertEqual(len(out["daily"]["dates"]), 252)
        self.assertEqual(out["latest"]["n"], 252)
        ratios = [r["short_volume"] / r["total_volume"] for r in rows]
        z = (ratios[-1] - mean(ratios[-253:-1])) / math.sqrt(
            sum((v - mean(ratios[-253:-1])) ** 2 for v in ratios[-253:-1]) / 251)
        self.assertEqual(out["latest"]["z"], round(z, 2))
        self.assertEqual(out["latest"]["label"], shorts.z_label(out["latest"]["z"]))
        self.assertIsNotNone(out["avg5"]["z"])
        self.assertEqual(out["interest"]["status"], "not_available")
        empty = shorts.summarize([], [])
        self.assertEqual(empty["status"], "not_available")
        self.assertIn(shorts.NOTE_FINRA, empty["notes"])
        dumps(empty)


class MacroImpactTest(unittest.TestCase):
    def test_ols_against_statsmodels(self):
        o = REF["ols"]
        fit = macro_impact.ols(o["y"], [o["x1"], o["x2"]])
        for got, want in zip(fit["coef"], o["coef"]):
            self.assertAlmostEqual(got, want, places=8)
        for got, want in zip(fit["se"], o["se"]):
            self.assertAlmostEqual(got, want, places=8)
        for got, want in zip(fit["t"], o["t"]):
            self.assertAlmostEqual(got, want, places=6)
        self.assertAlmostEqual(fit["r2"], o["r2"], places=8)
        self.assertEqual(fit["n"], len(o["y"]))

    def test_ols_exact_line_and_errors(self):
        fit = macro_impact.ols([2.0, 5.0, 8.0, 11.0], [[0.0, 1.0, 2.0, 3.0]])
        self.assertAlmostEqual(fit["coef"][0], 2.0)
        self.assertAlmostEqual(fit["coef"][1], 3.0)
        self.assertAlmostEqual(fit["r2"], 1.0)
        with self.assertRaises(ValueError):
            macro_impact.ols([1.0, 2.0], [[1.0, 2.0]])
        with self.assertRaises(ValueError):
            macro_impact.ols([1.0, 2.0, 3.0, 4.0], [[1.0, 1.0, 1.0, 1.0]])     # collinear with the intercept

    def test_factor_changes(self):
        pts = [("2026-01-05", 4.0), ("2026-01-06", 4.1), ("2026-01-08", 4.05)]
        ch = macro_impact.factor_changes(pts, "diff")
        self.assertEqual(sorted(ch), ["2026-01-06", "2026-01-08"])
        self.assertAlmostEqual(ch["2026-01-06"], 0.1)
        self.assertAlmostEqual(ch["2026-01-08"], -0.05)
        pct = macro_impact.factor_changes([("2026-01-05", 50.0), ("2026-01-06", 55.0)], "pct")
        self.assertAlmostEqual(pct["2026-01-06"], 10.0)

    def test_sensitivities_recover_a_known_model(self):
        spy = ss.synthetic_spy(700, start="2023-01-02")
        dates = [b["date"] for b in spy]
        fred = ss.synthetic_fred(dates, seed=4)
        m = macro_impact.daily_returns(spy)
        d10 = macro_impact.factor_changes(fred["DGS10"], "diff")
        import random
        rng = random.Random(1)
        close = 50.0
        bars = [bar(dates[0], close)]
        for d in dates[1:]:
            r = 0.02 + 1.4 * m[d] - 3.0 * d10[d] + rng.gauss(0, 0.1)       # percent
            close *= 1 + r / 100
            bars.append(bar(d, close))
        out = macro_impact.macro_impact(bars, spy, fred, {"fomc": [], "cpi": []})
        self.assertEqual(out["status"], "ok")
        self.assertAlmostEqual(out["market_beta"]["beta"], 1.4, delta=0.1)
        self.assertEqual(out["market_beta"]["n"], 504)
        f = {row["id"]: row for row in out["factors"]}
        self.assertEqual(f["dgs10"]["status"], "ok")
        self.assertAlmostEqual(f["dgs10"]["move_pct"], -0.3, delta=0.03)      # -3.0 per point is -0.30 per 0.10
        self.assertEqual(f["dgs10"]["band"], "strong")
        self.assertLess(f["dgs10"]["low_pct"], f["dgs10"]["move_pct"])
        self.assertRegex(f["dgs10"]["text"], r"^Tends to fall about 0\.\d\d percent per 0\.10 point rise in the 10 year yield")
        self.assertEqual(f["dgs10"]["unit"], "per 0.10 point rise")
        self.assertEqual(f["dgs10"]["n"], 504)
        self.assertLess(abs(f["oil"]["t"]), 4)
        self.assertEqual([r["id"] for r in out["factors"]], ["dgs10", "dgs2", "dollar", "oil", "vix"])
        self.assertIn(macro_impact.NOTE_PAST, out["notes"])
        dumps(out)

    def test_short_history_and_missing_inputs(self):
        bars = ss.synthetic_bars(60, start="2026-08-03")
        spy = ss.synthetic_spy(60, start="2026-08-03")
        fred = ss.synthetic_fred([b["date"] for b in bars])
        out = macro_impact.macro_impact(bars, spy, fred, {})
        self.assertEqual(out["status"], "not enough history")
        self.assertTrue(out["notes"][0].startswith("Low confidence, 60 days of history"))
        self.assertTrue(all(f["status"] == "not enough history" for f in out["factors"]))
        none = macro_impact.macro_impact(bars, None, {}, None)
        self.assertEqual(none["status"], "not_available")
        self.assertTrue(all(f["status"] == "not_available" for f in none["factors"]))
        dumps(none)

    def test_event_moves(self):
        dates = ss.weekdays("2026-01-05", 100)
        closes, c = [], 100.0
        moves = {}
        for i, d in enumerate(dates):
            if i == 0:
                closes.append(c)
                continue
            r = 0.01 if i % 2 else -0.01                   # normal days move 1 percent
            if i in (20, 50, 80):
                r = {20: 0.03, 50: -0.04, 80: 0.05}[i]
                moves[d] = r * 100
            c *= 1 + r
            closes.append(c)
        bars = series_bars(closes)
        events = sorted(moves) + ["2025-01-01", "2026-12-31"]
        out = macro_impact.event_moves(bars, events, "CPI")
        self.assertEqual(out["n"], 3)
        self.assertAlmostEqual(out["avg_move_pct"], round((3 - 4 + 5) / 3, 2))
        self.assertAlmostEqual(out["avg_abs_move_pct"], 4.0)
        self.assertAlmostEqual(out["normal_abs_move_pct"], 1.0)
        self.assertAlmostEqual(out["ratio"], 4.0)
        self.assertEqual(out["last"], {"date": sorted(moves)[-1], "move_pct": 5.0})
        self.assertEqual(out["status"], "not enough history")     # fewer than 4 events
        self.assertAlmostEqual(out["up_share"], round(2 / 3, 4))
        windowed = macro_impact.event_moves(bars, events, "CPI", window=40)
        self.assertEqual(windowed["n"], 1)
        empty = macro_impact.event_moves(bars, [], "FOMC")
        self.assertEqual(empty["n"], 0)
        self.assertIsNone(empty["last"])


class HelperTest(unittest.TestCase):
    def test_numeric_helpers(self):
        self.assertEqual(quantile([1, 2, 3, 4], 0.25), 1.75)       # numpy's linear rule
        self.assertEqual(median([3, 1, 2]), 2)
        self.assertIsNone(quantile([], 0.5))
        x = solve([[2.0, 1.0], [1.0, 3.0]], [3.0, 5.0])
        self.assertAlmostEqual(x[0], 0.8)
        self.assertAlmostEqual(x[1], 1.4)
        with self.assertRaises(ValueError):
            solve([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0])


class CopyAndPurityTest(unittest.TestCase):
    def test_no_dashes_and_no_prints(self):
        for name in sorted(os.listdir(ANALYSIS_DIR)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(ANALYSIS_DIR, name), encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("\u2014", text, name)
            self.assertNotIn("\u2013", text, name)
            self.assertNotRegex(text, r"\bprint\(", name)
            for banned in ("import numpy", "import requests", "datetime.now", "date.today", "time.time", "random"):
                self.assertNotIn(banned, text, f"{name}: {banned}")

    def test_support_examples_are_valid(self):
        doc = ss.example_document()
        for key in ("kind", "version", "ticker", "status", "notes", "header", "chart", "technicals", "levels",
                    "valuation", "shorts", "holders", "macro_impact", "learner", "assessment", "model", "llm",
                    "cost", "generated_at", "data_as_of", "disclaimer"):
            self.assertIn(key, doc)
        self.assertEqual(doc["kind"], "stocks.ticker")
        self.assertEqual(doc["ticker"], "AAPL")
        self.assertLess(len(dumps(doc).encode("utf-8")), 512 * 1024)
        prices = {c["price"] for c in doc["levels"]["candidates"]}
        a = doc["assessment"]
        self.assertTrue(all(z["low"] in prices and z["high"] in prices for z in a["entry_zones"]))
        self.assertIn(a["stop"]["price"], prices)
        self.assertEqual(ss.example_document("MSFT")["ticker"], "MSFT")
        doc["ticker"] = "changed"
        self.assertEqual(ss.example_document()["ticker"], "AAPL")       # a fresh copy each call
        ov = ss.example_overview()
        self.assertEqual(ov["kind"], "stocks.overview")
        for text in (dumps(ss.example_document()), dumps(ov)):
            self.assertNotIn("\u2014", text)
            self.assertNotIn("\u2013", text)


if __name__ == "__main__":
    unittest.main()
