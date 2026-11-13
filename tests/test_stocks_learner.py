"""Offline tests for the per-ticker learner (scripts/stocks/analysis/learner.py):
the model, the walk forward bootstrap, the daily update, grading, scorecards,
lessons, the size cap and, above all, isolation between tickers.

Tickers here are public examples (AAPL, MSFT) or made up (EXMPL, OTHR).
"""
import copy
import json
import math
import time
import types
import unittest

import stocks_support as ss  # puts scripts/ on sys.path

from stocks import dumps, size_of
from stocks.analysis import indicators, learner, levels, macro_impact, shorts
from stocks.analysis.learner import IsolationError

CFG = dict(ss.EXAMPLE_CFG)
NOW = "2026-11-13T13:50:00+00:00"
LATER = "2026-11-14T13:50:00+00:00"
REF = ss.fixture_json("analysis_reference.json")


def make(n=500, seed=21, end="2026-11-12", short_days=252):
    start = ss.start_for(end, n)
    bars = ss.synthetic_bars(n, seed=seed, start=start, price=40.0 + seed)
    spy = ss.synthetic_spy(n, start=start)
    dates = [b["date"] for b in bars]
    return bars, spy, ss.synthetic_fred(dates), ss.synthetic_short_volume(dates[-short_days:], seed=seed)


def boot(ticker, data, now=NOW, state=None):
    return learner.bootstrap(state or learner.new_state(ticker, now), *data, CFG, now)


def update(state, data, now=LATER):
    return learner.daily_update(state, *data, CFG, now)


def cut(data, k):
    """The inputs as they were k bars ago (k > 0)."""
    bars, spy, fred, sv = data
    last = bars[-1 - k]["date"]
    return (bars[:-k], [b for b in spy if b["date"] <= last],
            {s: [p for p in pts if p[0] <= last] for s, pts in fred.items()}, [r for r in sv if r["date"] <= last])


def containers(obj, out=None):
    """ids of every dict and list inside obj."""
    out = set() if out is None else out
    if isinstance(obj, (dict, list)):
        out.add(id(obj))
        for v in (obj.values() if isinstance(obj, dict) else obj):
            containers(v, out)
    return out


def assessment(stance="bullish", low=None, high=None, target=None, stop=None):
    return {"source": "model", "levels_source": "model", "stance": stance, "confidence": "medium",
            "summary": "Text.", "technical": "", "valuation": "", "shorts": "", "holders": "", "macro": "",
            "risks": [], "entry_zones": [] if low is None else [{"low": low, "high": high, "rationale": "r"}],
            "targets": [] if target is None else [{"price": target, "horizon_days": 20, "rationale": "r"}],
            "stop": None if stop is None else {"price": stop, "rationale": "r"},
            "change_my_mind": "", "lessons_added": [], "problems": []}


class Fixtures:
    """Two tickers' inputs and their bootstrapped states, built once."""
    _built = None

    @classmethod
    def get(cls):
        if cls._built is None:
            a, b = make(500, 21), make(500, 22)
            cls._built = {"a": a, "b": b, "sa": boot("AAPL", a), "sb": boot("MSFT", b)}
        return cls._built


class ModelTest(unittest.TestCase):
    def test_fit_matches_scikit_learn(self):
        ref = REF["logistic"]
        rows = [{"date": f"r{i:03d}", "close": 1.0, "i": i, "x": x, "y": {"1": y}}
                for i, (x, y) in enumerate(zip(ref["x"], ref["y"]))]
        p = learner.fit(rows, ["a", "b", "c"], 1, lam=ref["lambda"])
        self.assertEqual(p["kind"], "logistic_l2")
        for got, want in zip(p["w"], ref["w"]):
            self.assertAlmostEqual(got, want, places=5)
        self.assertAlmostEqual(p["b"], ref["b"], places=5)
        for got, want in zip(p["mean"], ref["mean"]):
            self.assertAlmostEqual(got, want, places=8)
        for got, want in zip(p["std"], ref["std"]):
            self.assertAlmostEqual(got, want, places=8)
        self.assertEqual(p["n"], 400)
        self.assertEqual(p["lambda"], ref["lambda"])
        self.assertEqual(p["base_rate"], round(sum(ref["y"]) / 400, 4))
        self.assertEqual(p["fitted_on"], "r399")
        self.assertLessEqual(p["iterations"], 25)

    def test_warm_start(self):
        ref = REF["logistic"]
        rows = [{"date": str(i), "close": 1.0, "i": i, "x": x, "y": {"1": y}}
                for i, (x, y) in enumerate(zip(ref["x"], ref["y"]))]
        cold = learner.fit(rows[:380], ["a", "b", "c"], 1, lam=10.0)
        warm = learner.fit(rows, ["a", "b", "c"], 1, lam=10.0, warm=cold)
        fresh = learner.fit(rows, ["a", "b", "c"], 1, lam=10.0)
        self.assertLess(warm["iterations"], fresh["iterations"])
        for a, b in zip(warm["w"], fresh["w"]):
            self.assertAlmostEqual(a, b, places=7)
        mismatched = learner.fit(rows, ["a", "b", "c"], 1, lam=10.0, warm={"w": [1.0], "b": 0.0})
        self.assertEqual(mismatched["iterations"], fresh["iterations"])     # another feature list: from scratch

    def test_predict_clips_and_base_rate(self):
        params = {"kind": "logistic_l2", "w": [50.0], "b": 0.0, "mean": [0.0], "std": [1.0]}
        self.assertEqual(learner.predict(params, [10.0]), 0.98)
        self.assertEqual(learner.predict(params, [-10.0]), 0.02)
        self.assertEqual(learner.predict(params, [None]), 0.5)               # a missing value is the mean
        self.assertEqual(learner.predict({"kind": "base_rate", "base_rate": 0.61}, []), 0.61)
        self.assertEqual(learner.predict({"kind": "base_rate", "base_rate": None}, []), 0.5)
        self.assertEqual(learner.predict({}, []), 0.5)

    def test_scorecard_math(self):
        entries = []
        for p, y in ((0.8, 1), (0.3, 0), (0.6, 0), (0.4, 1)):
            entries.append({"p_up": {"5": p}, "base_rate": {"5": 0.5}, "outcome": {"5": {"y": y}}})
        entries.append({"p_up": {"5": 0.9}, "base_rate": {"5": 0.5}, "outcome": {"5": None}})     # not graded
        sc = learner.scorecard(entries, 5)
        self.assertEqual(sc["n"], 4)
        self.assertEqual(sc["hit_rate"], 0.5)
        self.assertEqual(sc["brier"], round((0.04 + 0.09 + 0.36 + 0.36) / 4, 4))
        self.assertEqual(sc["base_brier"], 0.25)
        self.assertIsNone(sc["skill"])                                       # fewer than 10
        self.assertEqual(sc["up_rate"], 0.5)
        bins = {(b["lo"], b["hi"]): b for b in sc["calibration"]}
        self.assertEqual(len(bins), 5)
        self.assertEqual(bins[(0.8, 1.0)]["n"], 1)
        self.assertEqual(bins[(0.8, 1.0)]["freq"], 1.0)
        self.assertEqual(bins[(0.2, 0.4)]["mean_p"], 0.3)
        self.assertEqual(bins[(0.4, 0.6)]["n"], 1)
        self.assertEqual(bins[(0.6, 0.8)]["freq"], 0.0)
        self.assertEqual(bins[(0.0, 0.2)], {"lo": 0.0, "hi": 0.2, "n": 0, "mean_p": None, "freq": None})
        ten = entries[:4] * 3
        sc10 = learner.scorecard(ten, "5")
        self.assertEqual(sc10["skill"], round(1 - 0.2125 / 0.25, 4))
        empty = learner.scorecard([], 5)
        self.assertEqual((empty["n"], empty["hit_rate"], empty["brier"]), (0, None, None))

    def test_excursions_by_hand(self):
        closes = [100.0, 101.0, 99.0, 102.0, 98.0, 100.0]
        bars = [ss.synthetic_bars(1)[0] | {"date": d, "close": c, "high": c + 1, "low": c - 1}
                for d, c in zip(ss.weekdays("2026-03-02", 6), closes)]
        ex = learner.excursions(bars, horizon=2, min_n=3, similar=False)
        # t=0: lows 100, 98 -> -0.02; highs 102, 100 -> +0.02 ... computed by hand for t = 0 to 3
        dds = [98 / 100 - 1, 98 / 101 - 1, 97 / 99 - 1, 97 / 102 - 1]
        rus = [102 / 100 - 1, 103 / 101 - 1, 103 / 99 - 1, 101 / 102 - 1]
        from stocks.analysis import quantile
        self.assertEqual(ex["drawdown_q"], {q: round(quantile(dds, int(q) / 100), 4) for q in ("25", "50", "75")})
        self.assertEqual(ex["runup_q"], {q: round(quantile(rus, int(q) / 100), 4) for q in ("25", "50", "75")})
        self.assertEqual((ex["n"], ex["scope"], ex["horizon"]), (4, "all", 2))
        self.assertIsNone(learner.excursions(bars, horizon=2, min_n=5))
        bars, _, _, _ = make(600, 3)
        sim = learner.excursions(bars)
        self.assertEqual(sim["scope"], "similar")
        self.assertGreaterEqual(sim["n"], 120)
        self.assertLess(sim["n"], sim["n_all"])
        self.assertLessEqual(sim["drawdown_q"]["25"], sim["drawdown_q"]["50"])
        self.assertLessEqual(sim["drawdown_q"]["75"], 0)
        self.assertGreaterEqual(sim["runup_q"]["25"], 0)


class FeatureTest(unittest.TestCase):
    def test_features_use_only_the_past(self):
        bars, spy, fred, sv = make(800, 5)
        names, rows = learner.feature_rows(bars, spy, fred, sv)
        names2, rows2 = learner.feature_rows(bars[:740], spy, fred, sv)
        self.assertIn("dist_sma200", names2)
        self.assertEqual(names, names2)
        by_date = {r["date"]: r["x"] for r in rows}
        for r in rows2[:-1]:
            self.assertEqual(r["x"], by_date[r["date"]])          # adding later bars never changes an earlier row

    def test_feature_values(self):
        bars, spy, fred, sv = make(400, 5)
        cols = learner.feature_columns(bars, spy, fred, sv)
        i = 350
        closes = [b["close"] for b in bars]
        self.assertAlmostEqual(cols["ret_20"][i], math.log(closes[i] / closes[i - 20]))
        self.assertAlmostEqual(cols["dist_sma50"][i], closes[i] / (sum(closes[i - 49:i + 1]) / 50) - 1)
        r = indicators.rsi(closes, 14)[i]
        self.assertAlmostEqual(cols["rsi14"][i], (r - 50) / 50)
        spy_c = {b["date"]: b["close"] for b in spy}
        k = [b["date"] for b in spy].index(bars[i]["date"])
        self.assertAlmostEqual(cols["spy_ret_5"][i], math.log(spy[k]["close"] / spy[k - 5]["close"]))
        self.assertAlmostEqual(cols["rs_spy_20"][i], cols["ret_20"][i] - math.log(spy_c[bars[i]["date"]] /
                                                                                   spy[k - 20]["close"]))
        d10 = dict(fred["DGS10"])
        dates = [p[0] for p in fred["DGS10"]]
        j = dates.index(bars[i]["date"])
        self.assertAlmostEqual(cols["d_dgs10_5"][i], d10[dates[j]] - d10[dates[j - 5]])
        self.assertAlmostEqual(cols["vix_rel"][i], dict(fred["VIXCLS"])[bars[i]["date"]] / 20 - 1)
        vols = [b["volume"] for b in bars]
        self.assertAlmostEqual(cols["vol_ratio"][i], math.log(vols[i] / (sum(vols[i - 19:i + 1]) / 20)))
        z = learner.short_z_by_date(sv)
        d = sv[100]["date"]
        ratios = [r["short_volume"] / r["total_volume"] for r in sv]
        base = ratios[40:100]
        mu = sum(base) / 60
        sd = math.sqrt(sum((v - mu) ** 2 for v in base) / 59)
        self.assertAlmostEqual(z[d], (ratios[100] - mu) / sd)
        self.assertNotIn(sv[39]["date"], z)                                    # fewer than 40 days before it

    def test_selection_for_short_and_long_histories(self):
        for n, has_200 in ((250, False), (300, False), (500, False), (722, False), (723, True), (900, True)):
            bars, spy, fred, sv = make(n, 6)
            names, rows = learner.feature_rows(bars, spy, fred, sv)
            self.assertEqual("dist_sma200" in names, has_200, n)
            self.assertTrue(rows)
            labeled = [r for r in rows if r["y"]["20"] is not None]
            self.assertGreaterEqual(len(labeled), min(150, n - 80), n)
        bars, spy, fred, sv = make(500, 6, short_days=252)
        self.assertNotIn("short_ratio_z", learner.feature_rows(bars, spy, fred, sv)[0])   # 212 rows with a z
        bars, spy, fred, sv = make(500, 6, short_days=320)
        names, rows = learner.feature_rows(bars, spy, fred, sv)
        self.assertIn("short_ratio_z", names)
        k = names.index("short_ratio_z")
        self.assertEqual(rows[0]["x"][k], 0.0)                              # before the short history: a normal day
        names, _ = learner.feature_rows(bars, None, {}, [])
        for f in ("rs_spy_20", "spy_ret_5", "d_dgs10_5", "vix_rel", "short_ratio_z"):
            self.assertNotIn(f, names)
        self.assertEqual(list(names), [f for f in learner.FEATURES if f in names])

    def test_labels(self):
        bars, spy, fred, sv = make(300, 7)
        _, rows = learner.feature_rows(bars, spy, fred, sv)
        closes = [b["close"] for b in bars]
        r = rows[10]
        self.assertEqual(r["y"]["5"], 1 if closes[r["i"] + 5] > closes[r["i"]] else 0)
        self.assertIsNone(rows[-1]["y"]["5"])
        self.assertIsNone(rows[-20]["y"]["20"])
        self.assertIsNotNone(rows[-21]["y"]["20"])


class BootstrapTest(unittest.TestCase):
    def test_state_shape(self):
        st = Fixtures.get()["sa"]
        self.assertEqual(tuple(st), learner.STATE_KEYS)
        self.assertEqual((st["kind"], st["version"], st["ticker"]), ("stocks.state", 1, "AAPL"))
        m = st["model"]
        self.assertEqual((m["kind"], m["status"], m["horizons"]), ("logistic_l2", "ok", [5, 20]))
        for h in ("5", "20"):
            p = m["params"][h]
            self.assertEqual(len(p["w"]), len(st["features"]))
            self.assertIn(p["lambda"], learner.LAMBDAS)
            for k in ("w", "b", "mean", "std", "lambda", "n", "base_rate", "fitted_on", "iterations"):
                self.assertIn(k, p)
        bt = st["backtest"]
        self.assertEqual((bt["method"], bt["min_train"], bt["step"]), ("walk_forward", 150, 21))
        self.assertGreater(bt["scorecard"]["5"]["n"], 250)
        self.assertEqual(bt["rows"], bt["scorecard"]["5"]["n"])
        self.assertEqual(len(st["predictions"]), 1)
        e = st["predictions"][0]
        self.assertEqual(e["as_of"], "2026-11-12")
        self.assertEqual(e["due"], {"5": "2026-11-19", "20": "2026-12-11"})        # NYSE days, Thanksgiving skipped
        self.assertEqual(e["outcome"], {"5": None, "20": None})
        self.assertTrue(all(0.02 <= p <= 0.98 for p in e["p_up"].values()))
        self.assertEqual(st["last_as_of"], "2026-11-12")
        self.assertEqual(st["last_run"]["at"], NOW)
        self.assertIsNotNone(m["excursions"])
        learner.check_state("AAPL", st)
        dumps(st)

    def test_walk_forward_has_no_look_ahead(self):
        data = make(400, 8)
        names, rows = learner.feature_rows(*data)
        c = learner._cfg(CFG)
        full, _, _, _ = learner._walk_forward(rows, names, 5, c)
        # Change every label after a date: predictions made before that date stay the same.
        k = len(rows) - 60
        changed = copy.deepcopy(rows)
        for r in changed[k:]:
            r["y"]["5"] = None if r["y"]["5"] is None else 1 - r["y"]["5"]
        alt, _, _, _ = learner._walk_forward(changed, names, 5, c)
        cutoff = rows[k - 5]["date"]
        a = [p for p, _, _, d in full if d <= cutoff]
        b = [p for p, _, _, d in alt if d <= cutoff]
        self.assertTrue(a)
        self.assertEqual(a, b)

    def test_determinism(self):
        data = make(400, 9)
        one = dumps(boot("EXMPL", data))
        two = dumps(boot("EXMPL", copy.deepcopy(data)))
        self.assertEqual(one, two)

    def test_short_history_warms_up(self):
        data = make(95, 10)
        st = boot("EXMPL", data)
        self.assertEqual((st["model"]["kind"], st["model"]["status"]), ("base_rate", "warming_up"))
        self.assertIsNone(st["backtest"])
        self.assertNotIn("dist_sma200", st["features"])
        e = st["predictions"][-1]
        for h, p in e["p_up"].items():
            self.assertEqual(p, st["model"]["params"][h]["base_rate"])
            observed = st["model"]["params"][h]["observed_rate"]
            self.assertLessEqual(abs(p - 0.5), abs((0.5 if observed is None else observed) - 0.5))
        block = learner.learner_block(st)
        self.assertEqual(block["status"], "warming_up")
        self.assertTrue(block["note"].startswith("Low confidence, 95 days of history."))
        self.assertEqual(block["confidence"], "low")
        st2 = update(st, data)                       # the same day again: still one entry
        self.assertEqual(len(st2["predictions"]), 1)
        tiny = boot("EXMPL", make(3, 10))
        self.assertEqual(tiny["predictions"][-1]["p_up"], {"5": 0.5, "20": 0.5})

    def test_young_listing_gets_a_model_with_low_confidence(self):
        st = boot("EXMPL", make(320, 11))
        self.assertEqual(st["model"]["status"], "ok")
        self.assertNotIn("dist_sma200", st["features"])
        self.assertEqual(learner.learner_block(st)["note"], "Low confidence, 320 days of history.")
        long = Fixtures.get()["sa"]
        self.assertEqual(learner.learner_block(long)["note"], "Low confidence, 500 days of history.")

    def test_speed_and_size_after_five_years(self):
        data = make(1560, 12, short_days=300)
        bars = data[0]
        state = learner.new_state("EXMPL", NOW)
        state["caches"] = {
            "prices": {"fields": ["date", "open", "high", "low", "close", "volume", "raw_close", "raw_volume",
                                  "div", "split"],
                       "rows": [[b["date"], b["open"], b["high"], b["low"], b["close"], b["volume"], b["raw_close"],
                                 b["raw_volume"], b["div"], b["split"]] for b in bars],
                       "first_date": bars[0]["date"], "last_date": bars[-1]["date"], "fetched_at": NOW},
            "short_volume": {"fields": ["date", "short_volume", "exempt_volume", "total_volume"],
                             "rows": [[r["date"], r["short_volume"], r["exempt_volume"], r["total_volume"]]
                                      for r in data[3]], "last_date": data[3][-1]["date"]}}
        start = time.perf_counter()
        st = learner.bootstrap(state, *data, CFG, NOW)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 7.5, "a full bootstrap must take under 60 seconds for 8 tickers")
        self.assertIn("dist_sma200", st["features"])
        self.assertIn("short_ratio_z", st["features"])
        self.assertEqual(st["model"]["training"]["rows"], 1260)         # the last 5 years of labeled rows
        self.assertEqual(st["caches"], state["caches"])                  # the learner never touches the caches
        # A year of daily calls with assessments on top still fits well under 1 MiB.
        e = st["predictions"][0]
        st["predictions"] = [dict(copy.deepcopy(e), as_of=f"x{i:04d}",
                                  assessment=learner._compact_assessment(assessment("bullish", 1, 2, 3, 0.5), "m"))
                             for i in range(400)]
        self.assertLess(size_of(st), 1_048_576)


class DailyUpdateTest(unittest.TestCase):
    def test_idempotent_same_day(self):
        f = Fixtures.get()
        once = update(f["sa"], f["a"], LATER)
        twice = update(once, f["a"], LATER)
        self.assertEqual(dumps(once), dumps(twice))
        self.assertEqual(len(once["predictions"]), 1)
        self.assertEqual(once["predictions"][0]["p_up"], f["sa"]["predictions"][0]["p_up"])
        other_time = update(once, f["a"], "2026-11-14T20:00:00+00:00")
        self.assertEqual(other_time["predictions"], once["predictions"])
        self.assertEqual(other_time["model"]["params"], once["model"]["params"])

    def test_grading_after_h_bars(self):
        data = Fixtures.get()["a"]
        bars = data[0]
        st = boot("AAPL", cut(data, 25))
        e0 = st["predictions"][0]
        i0 = len(bars) - 26
        self.assertEqual(e0["as_of"], bars[i0]["date"])
        st = update(st, cut(data, 20))                         # 5 bars later
        self.assertEqual(len(st["predictions"]), 2)
        out = st["predictions"][0]["outcome"]
        c0, c5 = bars[i0]["close"], bars[i0 + 5]["close"]
        self.assertEqual(out["5"], {"y": 1 if c5 > c0 else 0, "ret_pct": round((c5 / c0 - 1) * 100, 2),
                                    "graded_on": bars[i0 + 5]["date"]})
        self.assertIsNone(out["20"])
        self.assertEqual(st["scorecard"]["live"]["5"]["n"], 1)
        self.assertEqual(st["scorecard"]["live"]["20"]["n"], 0)
        st = update(st, data, "2026-11-15T13:50:00+00:00")     # 25 bars after the first call
        out = st["predictions"][0]["outcome"]
        c20 = bars[i0 + 20]["close"]
        self.assertEqual(out["20"]["y"], 1 if c20 > c0 else 0)
        self.assertEqual(st["predictions"][1]["outcome"]["20"]["graded_on"], bars[i0 + 25]["date"])
        self.assertEqual(st["scorecard"]["live"]["5"]["n"], 2)
        self.assertEqual(len(st["predictions"]), 3)
        self.assertEqual(learner.grade(st, bars)["predictions"], st["predictions"])     # nothing left to grade

    def test_assessment_grading(self):
        base, spy, fred, sv = make(300, 13, end="2026-09-30")
        close = base[-1]["close"]
        st = boot("EXMPL", (base, spy, fred, sv))
        as_of = base[-1]["date"]
        a = assessment("bullish", close * 0.96, close * 0.98, close * 1.05, close * 0.93)
        st = learner.attach_assessment(st, as_of, a, [], "claude-sonnet-5-5", 12)
        rec = st["predictions"][-1]["assessment"]
        self.assertEqual(rec["model"], "claude-sonnet-5-5")
        self.assertEqual(rec["grade"], {"stance_correct": None, "levels": None, "graded_on": None})

        def future(path):
            dates = ss.weekdays(ss.weekdays(as_of, 2)[1], len(path))
            return base + [ss.synthetic_bars(1)[0] | {"date": d, "close": close * c, "high": close * h,
                                                       "low": close * lo}
                           for d, (c, h, lo) in zip(dates, path)]

        up = [(1.01, 1.02, 1.0)] * 2 + [(1.04, 1.06, 1.0)] + [(1.03, 1.04, 1.01)] * 17
        g = learner.grade(st, future(up))["predictions"][-1]["assessment"]["grade"]
        self.assertEqual((g["stance_correct"], g["levels"]), (True, "target"))
        down = [(0.99, 1.0, 0.98)] * 3 + [(0.92, 0.95, 0.92)] + [(0.95, 1.06, 0.94)] * 16
        g = learner.grade(st, future(down))["predictions"][-1]["assessment"]["grade"]
        self.assertEqual((g["stance_correct"], g["levels"]), (False, "stop"))
        both = [(1.0, 1.06, 0.92)] + [(1.0, 1.0, 1.0)] * 19
        self.assertEqual(learner.grade(st, future(both))["predictions"][-1]["assessment"]["grade"]["levels"], "stop")
        flat = [(1.001, 1.01, 0.99)] * 20
        g = learner.grade(st, future(flat))["predictions"][-1]["assessment"]["grade"]
        self.assertEqual(g["levels"], "neither")
        self.assertEqual(g["graded_on"], future(flat)[-1]["date"])
        early = learner.grade(st, future(flat[:19]))["predictions"][-1]["assessment"]["grade"]
        self.assertIsNone(early["graded_on"])                       # not before 20 bars
        neutral = learner.attach_assessment(st, as_of, assessment("neutral"), [], "m", 12)
        g = learner.grade(neutral, future(flat))["predictions"][-1]["assessment"]["grade"]
        self.assertEqual((g["stance_correct"], g["levels"]), (True, None))      # a 0.1 percent move is small
        graded = learner.grade(st, future(up))
        self.assertEqual(graded["scorecard"]["assessments"],
                         {"n": 1, "stance_hit_rate": 1.0, "target": 1, "stop": 0, "neither": 0})

    def test_feature_change_rebuilds_the_model(self):
        f = Fixtures.get()
        bars, spy, fred, sv = f["a"]
        st = update(f["sa"], (bars, spy, {}, sv), LATER)                # FRED could not be read today
        self.assertNotIn("vix_rel", st["features"])
        self.assertEqual(st["backtest"]["created"], LATER)
        self.assertEqual(len(st["model"]["params"]["5"]["w"]), len(st["features"]))
        self.assertEqual(len(st["predictions"]), 1)
        back = update(st, f["a"], "2026-11-15T13:50:00+00:00")
        self.assertEqual(back["features"], f["sa"]["features"])

    def test_quarterly_backtest_refresh(self):
        data = make(460, 14)
        st = boot("EXMPL", cut(data, 70))
        st = update(st, cut(data, 10), LATER)
        self.assertEqual(st["backtest"]["created"], NOW)          # 60 bars later: kept
        st = update(st, data, "2026-11-15T13:50:00+00:00")
        self.assertEqual(st["backtest"]["created"], "2026-11-15T13:50:00+00:00")
        self.assertEqual(st["backtest"]["as_of"], data[0][-1]["date"])

    def test_stale_bars_log_nothing_new(self):
        f = Fixtures.get()
        st = update(f["sa"], cut(f["a"], 3))
        self.assertEqual(len(st["predictions"]), 1)
        self.assertEqual(st["last_as_of"], "2026-11-12")

    def test_refuses_another_kind_of_state(self):
        f = Fixtures.get()
        old = dict(copy.deepcopy(f["sa"]), version=2)
        with self.assertRaises(ValueError) as e:
            update(old, f["a"])
        self.assertNotIsInstance(e.exception, IsolationError)


class IsolationTest(unittest.TestCase):
    def test_check_state_refusals(self):
        st = copy.deepcopy(Fixtures.get()["sa"])
        st["identity"] = {"cik": 320193, "name": "Apple Inc."}
        self.assertIs(learner.check_state("AAPL", st, ("MSFT",), cik=320193), st)
        with self.assertRaises(IsolationError):
            learner.check_state("MSFT", st)
        with self.assertRaises(IsolationError):
            learner.check_state("AAPL", st, cik=789019)
        with self.assertRaises(IsolationError):
            learner.check_state("AAPL", dict(st, extra={"spy": []}))
        with self.assertRaises(IsolationError):
            learner.check_state("AAPL", dict(st, identity={"ticker": "MSFT"}))
        named = dict(st, lessons=[{"date": "2026-11-12", "text": "Moves with MSFT on earnings.", "source": "model"}])
        with self.assertRaises(IsolationError):
            learner.check_state("AAPL", named, ("MSFT", "AAPL"))
        learner.check_state("AAPL", named, ("AAPL",))                          # the ticker itself is not "another"
        dotted = dict(st, lessons=[{"date": "2026-11-12", "text": "Unlike BRK.B it pays no dividend."}])
        with self.assertRaises(IsolationError):
            learner.check_state("AAPL", dotted, ("BRK-B",))
        lower = dict(st, lessons=[{"date": "2026-11-12", "text": "The msft pattern did not repeat."}])
        learner.check_state("AAPL", lower, ("MSFT",))                          # whole word, case-sensitive
        for bad in (None, [], dict(st, kind="stocks.ticker"), dict(st, version=2), dict(st, predictions={})):
            with self.assertRaises(ValueError):
                learner.check_state("AAPL", bad)
            if bad is not None and not isinstance(bad, list) and bad.get("kind") == "stocks.ticker":
                with self.assertRaises(ValueError) as e:
                    learner.check_state("AAPL", bad)
                self.assertNotIsInstance(e.exception, IsolationError)

    def test_runs_check_the_ticker_they_are_given(self):
        f = Fixtures.get()
        with self.assertRaises(IsolationError):
            learner.daily_update(f["sa"], *f["a"], CFG, LATER, ticker="MSFT")
        with self.assertRaises(IsolationError):
            learner.bootstrap(f["sb"], *f["a"], CFG, LATER, ticker="AAPL")
        named = learner.attach_assessment(f["sa"], "2026-11-12", assessment(), ["Quiet weeks end in gaps."], "m", 12)
        named["lessons"][0]["text"] = "Follows MSFT."              # as if edited by hand in KV
        with self.assertRaises(IsolationError):
            learner.daily_update(named, *f["a"], CFG, LATER, ticker="AAPL", others=("MSFT",))
        learner.daily_update(f["sa"], *f["a"], CFG, LATER, ticker="AAPL", others=("MSFT",))

    def test_same_output_alone_together_and_in_either_order(self):
        f = Fixtures.get()
        a, b = f["a"], f["b"]
        a1, b1 = cut(a, 1), cut(b, 1)

        def day1(order):
            states = {}
            for t, d in order:
                states[t] = boot(t, d)
            return states

        def day2(states, order):
            out = dict(states)
            for t, d in order:
                out[t] = update(states[t], d)
            return out

        alone = day2(day1([("AAPL", a1)]), [("AAPL", a)])["AAPL"]
        ab = day2(day1([("AAPL", a1), ("MSFT", b1)]), [("AAPL", a), ("MSFT", b)])
        ba = day2(day1([("MSFT", b1), ("AAPL", a1)]), [("MSFT", b), ("AAPL", a)])
        self.assertEqual(dumps(alone), dumps(ab["AAPL"]))
        self.assertEqual(dumps(alone), dumps(ba["AAPL"]))
        self.assertEqual(dumps(ab["MSFT"]), dumps(ba["MSFT"]))
        self.assertNotEqual(dumps(ab["AAPL"]["model"]), dumps(ab["MSFT"]["model"]))
        self.assertTrue(containers(ab["AAPL"]).isdisjoint(containers(ab["MSFT"])))

    def test_states_are_never_shared(self):
        f = Fixtures.get()
        before = dumps(f["sa"])
        out = update(f["sa"], f["a"])
        self.assertTrue(containers(out).isdisjoint(containers(f["sa"])))
        out["predictions"][0]["p_up"]["5"] = 0.0
        out["model"]["params"]["5"]["w"][0] = 99.0
        out["features"].append("x")
        self.assertEqual(dumps(f["sa"]), before)
        for fn in (lambda s: learner.grade(s, f["a"][0]),
                   lambda s: learner.attach_assessment(s, "2026-11-12", assessment(), ["A lesson."], "m", 12),
                   lambda s: learner.trim_state(s, 10_000_000)):
            res = fn(f["sa"])
            self.assertTrue(containers(res).isdisjoint(containers(f["sa"])))
            self.assertEqual(dumps(f["sa"]), before)
        block = learner.learner_block(f["sa"])
        self.assertTrue(containers(block).isdisjoint(containers(f["sa"])))
        ctx = learner.claude_context(f["sa"])
        ctx["lessons"].append("changed")
        self.assertEqual(dumps(f["sa"]), before)

    def test_shared_inputs_are_not_copied_into_a_state(self):
        f = Fixtures.get()
        st = f["sa"]
        text = dumps(st)
        spy, fred = f["a"][1], f["a"][2]
        self.assertFalse(any(repr(b["close"]) in text for b in spy[-300:]))
        self.assertFalse(any(repr(v) in text for _, v in fred["DTWEXBGS"][-300:]))
        self.assertEqual(st["caches"], {})
        self.assertEqual(set(st), set(learner.STATE_KEYS))

    def test_no_module_level_state(self):
        mods = (learner, indicators, levels, shorts, macro_impact)

        def snapshot():
            snap = {}
            for m in mods:
                for name, value in vars(m).items():
                    if isinstance(value, (dict, list, set)):
                        snap[(m.__name__, name)] = json.dumps(value, sort_keys=True, default=repr)
                    self.assertFalse(hasattr(value, "cache_info"), f"{m.__name__}.{name} is a cache")
            return snap

        before = snapshot()
        f = Fixtures.get()
        update(f["sb"], f["b"])
        boot("OTHR", make(200, 30))
        self.assertEqual(snapshot(), before)
        for m in mods:
            self.assertFalse(any(isinstance(v, types.GeneratorType) for v in vars(m).values()))

    def test_claude_context_holds_only_this_ticker(self):
        f = Fixtures.get()
        sa = learner.attach_assessment(f["sa"], "2026-11-12", assessment(), ["Gaps up after a quiet week."], "m", 12)
        sb = learner.attach_assessment(f["sb"], "2026-11-12", assessment(), ["Fades after large opening gaps."],
                                       "m", 12)
        ctx_a = dumps(learner.claude_context(sa))
        self.assertIn("Gaps up after a quiet week.", ctx_a)
        self.assertNotIn("Fades after large opening gaps.", ctx_a)
        self.assertNotIn("MSFT", ctx_a)
        self.assertNotIn("AAPL", ctx_a)
        for key, value in learner.claude_context(sa)["probabilities"].items():
            self.assertEqual(value, sa["predictions"][-1]["p_up"][key])


class AssessmentAndLessonsTest(unittest.TestCase):
    def test_lessons(self):
        st = Fixtures.get()["sa"]
        st = learner.attach_assessment(st, "2026-11-12", assessment(), [
            "RSI above 70 led to a pullback within a week \u2014 twice.",
            "The second lesson is kept too.",
            "A third lesson in one run is dropped."], "m", 12)
        texts = [l["text"] for l in st["lessons"]]
        self.assertEqual(texts, ["RSI above 70 led to a pullback within a week, twice.", "The second lesson is kept too."])
        self.assertEqual(st["lessons"][0], {"date": "2026-11-12", "text": texts[0], "source": "model"})
        st = learner.attach_assessment(st, "2026-11-12", assessment(), [
            "the second lesson is kept too.",                       # a duplicate, ignoring case
            "Unlike MSFT, it held the 50 day average."], "m", 12, others=("MSFT", "AAPL"))
        self.assertEqual(len(st["lessons"]), 2)
        st = learner.attach_assessment(st, "2026-11-12", assessment(), [
            "Microsoft's results moved it more than its own.",       # another tracked company
            "x" * 300], "m", 12, others=("MSFT",), other_names=("Microsoft Corporation",))
        self.assertEqual(len(st["lessons"]), 3)
        self.assertLessEqual(len(st["lessons"][-1]["text"]), 200)
        self.assertEqual(learner.company_core("Apple Inc."), "Apple")
        self.assertEqual(learner.company_core("Example Holdings S.p.A."), "Example")
        for i in range(10):
            st = learner.attach_assessment(st, "2026-11-12", assessment(), [f"Lesson number {i}."], "m", 12)
        self.assertEqual(len(st["lessons"]), 12)
        self.assertEqual(st["lessons"][-1]["text"], "Lesson number 9.")
        self.assertEqual(st["lessons"][0]["text"], "The second lesson is kept too.")     # the oldest went first
        self.assertEqual(st["last_run"]["model"], "m")
        learner.check_state("AAPL", st, ("MSFT",))

    def test_attach_assessment_record(self):
        st = Fixtures.get()["sa"]
        a = assessment("bearish", 10.0, 11.0, 12.5, 9.0)
        out = learner.attach_assessment(st, "2026-11-12", a, [], None, 12)
        rec = out["predictions"][-1]["assessment"]
        self.assertEqual(rec["stance"], "bearish")
        self.assertEqual(rec["entry_zones"], [{"low": 10.0, "high": 11.0}])
        self.assertEqual(rec["targets"], [{"price": 12.5, "horizon_days": 20}])
        self.assertEqual(rec["stop"], {"price": 9.0})
        self.assertIsNone(rec["model"])
        missing = learner.attach_assessment(st, "2026-01-01", a, [], "m", 12)
        self.assertIsNone(missing["predictions"][-1]["assessment"])     # no call logged that day

    def test_learner_block_and_context(self):
        st = Fixtures.get()["sa"]
        block = learner.learner_block(st)
        for key in ("status", "features", "probabilities", "base_rate", "scorecard", "lessons", "training_rows",
                    "as_of"):
            self.assertIn(key, block)
        self.assertEqual(set(block["scorecard"]), {"live", "backtest", "assessments"})
        self.assertEqual(block["probabilities"], st["predictions"][-1]["p_up"])
        self.assertEqual(block["training_rows"], st["model"]["training"]["rows"])
        ctx = learner.claude_context(st)
        for key in ("probabilities", "backtest", "live", "recent_calls", "lessons"):
            self.assertIn(key, ctx)
        dumps(block)
        dumps(ctx)


class TrimTest(unittest.TestCase):
    def build(self):
        st = copy.deepcopy(Fixtures.get()["sa"])
        e = st["predictions"][0]
        graded = dict(copy.deepcopy(e), outcome={"5": {"y": 1, "ret_pct": 1.0, "graded_on": "x"},
                                                 "20": {"y": 0, "ret_pct": -1.0, "graded_on": "x"}})
        st["predictions"] = [dict(copy.deepcopy(graded), as_of=f"2024-{i:04d}") for i in range(450)] + [e]
        st["caches"] = {
            "form4": {"seen": {}, "transactions": [{"date": "2024-01-02", "filed": "2024-01-03", "note": "x" * 400}] * 60
                      + [{"date": "2026-10-01", "filed": "2026-10-02"}]},
            "congress": {"trades": [{"date": "2024-03-01", "note": "y" * 400}] * 60 + [{"date": "2026-09-01"}]},
            "short_volume": {"fields": ["date", "short_volume", "exempt_volume", "total_volume"],
                             "rows": [[f"d{i:04d}", 1.0, 0.0, 2.0] for i in range(900)]},
            "prices": {"fields": ["date"], "rows": [[f"p{i:05d}", 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
                                                    for i in range(3000)], "first_date": "p00000"}}
        return st

    def test_order(self):
        st = self.build()
        steps = []
        full = size_of(st)
        self.assertEqual(learner.trim_state(st, full), st)                       # fits: unchanged
        t1 = learner.trim_state(st, full - 1)
        self.assertEqual(len(t1["predictions"]), 400)                           # graded calls beyond 400 go first
        self.assertEqual(t1["caches"], st["caches"])
        steps.append(size_of(t1))
        t2 = learner.trim_state(st, steps[-1] - 1)
        self.assertEqual(len(t2["caches"]["form4"]["transactions"]), 1)          # then old Form 4 transactions
        self.assertEqual(len(t2["caches"]["congress"]["trades"]), 61)
        steps.append(size_of(t2))
        t3 = learner.trim_state(st, steps[-1] - 1)
        self.assertEqual(len(t3["caches"]["congress"]["trades"]), 1)             # then old congress trades
        self.assertEqual(len(t3["caches"]["short_volume"]["rows"]), 900)
        steps.append(size_of(t3))
        t4 = learner.trim_state(st, steps[-1] - 1)
        self.assertEqual(len(t4["caches"]["short_volume"]["rows"]), 300)         # then short volume rows
        self.assertEqual(len(t4["caches"]["prices"]["rows"]), 3000)
        steps.append(size_of(t4))
        t5 = learner.trim_state(st, steps[-1] - 1)
        self.assertEqual(len(t5["caches"]["prices"]["rows"]), 5 * 252 + 300)     # then price rows
        self.assertEqual(t5["caches"]["prices"]["first_date"], t5["caches"]["prices"]["rows"][0][0])
        self.assertEqual(len(t5["predictions"]), 400)
        t6 = learner.trim_state(st, size_of(t5) - 1)
        self.assertLess(len(t6["predictions"]), 400)                             # last: the oldest calls
        self.assertLessEqual(size_of(t6), size_of(t5) - 1)
        with self.assertRaises(ValueError):
            learner.trim_state(st, 100)
        self.assertEqual(size_of(st), full)                                      # the input is never changed


if __name__ == "__main__":
    unittest.main()
