"""Privacy checks of the stock section, with no private value in the test.

  privacy.py   masking (::add-mask:: once per value, with the other spellings
               of a ticker and both spellings of a CIK), redaction, the
               exception hook that prints only a type, and the private folder.
  repository   no file holds a ticker list or a stored stock key, nothing
               under docs/ holds stock data, every fixture is described in
               its README, and, only when the orchestrator sets
               STOCKS_PRIVATE_TICKERS (a comma list) and STOCKS_PRIVATE_NAMES
               (a "|" list) at run time, no file holds any of them. The list is
               never committed: a 1 to 6 letter symbol cannot be hidden by a
               hash, so it is only ever supplied from outside.
"""
import contextlib
import fnmatch
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

import stocks_support as ss

import stocks
from stocks import privacy

ROOT = ss.ROOT
# Files the news side writes every day from public feeds: news can name any
# company, so the scan of names skips them, and the scan of symbols too.
NEWS_DATA = ("docs/data/", "docs/learn/", "learn/seen.json", "learn/lanes.md", "tests/fixtures/youtube_feed.xml")
# The files of the stock section, where no tracked company may be named.
STOCK_FILES = ("scripts/stocks/", "tests/test_stocks_", "tests/stocks_", "tests/fixtures/stocks/",
               "worker/stocks.js", "worker/worker.js", ".github/workflows/stocks.yml", "docs/assets/js/stocks.js",
               "requirements-stocks", ".env.example", "README.md")


def tracked_files():
    if shutil.which("git") and os.path.isdir(os.path.join(ROOT, ".git")):
        res = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, timeout=60)
        if res.returncode == 0:
            return sorted({p for p in res.stdout.decode("utf-8").split("\0") if p})
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules")]
        out += [os.path.relpath(os.path.join(base, f), ROOT) for f in files]
    return sorted(out)


def text_of(rel):
    try:
        with open(os.path.join(ROOT, rel), "rb") as f:
            data = f.read()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def stocks_section(config_text):
    """The stocks: block of config.yml."""
    m = re.search(r"(?ms)^stocks:\n.*?(?=^\S|\Z)", config_text)
    return m.group(0) if m else ""


class RedactorTests(unittest.TestCase):
    def test_masks_once_with_every_spelling(self):
        out = io.StringIO()
        r = privacy.Redactor(out=out)
        r.register("BRK-B", 1067983, "1067983", None, "", ["EXMPL"])
        r.register("BRK-B")
        lines = out.getvalue().splitlines()
        self.assertEqual(lines, ["::add-mask::BRK-B", "::add-mask::BRK.B", "::add-mask::BRK/B",
                                 "::add-mask::1067983", "::add-mask::0001067983", "::add-mask::EXMPL"])
        self.assertEqual(r.redact("BRK.B and 0001067983 rose; EXMPLE stays"), "*** and *** rose; EXMPLE stays")

    def test_a_percent_sign_and_lines_are_escaped(self):
        out = io.StringIO()
        r = privacy.Redactor(out=out)
        r.register("100% Example\nCo")
        self.assertEqual(out.getvalue().splitlines(), ["::add-mask::100%25 Example", "::add-mask::Co"])

    def test_log_and_warn_redact(self):
        r = privacy.Redactor(out=io.StringIO())
        r.register("EXMPL")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r.log("Saved EXMPL.")
            r.warn("EXMPL failed.")
        self.assertEqual(buf.getvalue().splitlines(), ["Saved ***.", "[warn] *** failed."])

    def test_the_exception_hook_prints_the_type_only(self):
        r = privacy.Redactor(out=io.StringIO())
        saved = (sys.excepthook, threading.excepthook)
        self.addCleanup(lambda: (setattr(sys, "excepthook", saved[0]), setattr(threading, "excepthook", saved[1])))
        privacy.install_excepthook(r)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                raise KeyError("EXMPL secret detail")
            except KeyError as e:
                sys.excepthook(type(e), e, e.__traceback__)
        self.assertEqual(buf.getvalue(), "Stopped: KeyError\n")
        self.assertEqual(privacy.error_name(ValueError("x")), "ValueError")

    def test_the_private_folder(self):
        tmp = tempfile.mkdtemp(prefix="stocks-private-")
        self.addCleanup(shutil.rmtree, tmp)
        path = privacy.private_dir(os.path.join(tmp, "d"))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)
        with self.assertRaises(stocks.StocksError):
            privacy.private_dir(os.path.join(ROOT, "scratch"))
        self.assertFalse(os.path.exists(os.path.join(ROOT, "scratch")))
        with self.assertRaises(stocks.StocksError):
            privacy.private_dir(tmp, repo_roots=[tmp])
        self.assertEqual(privacy.find_private("AAPL rose, AAPLX fell", ["AAPL", "MSFT"]), ["AAPL"])


class RepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tracked_files()

    def test_no_file_holds_a_ticker_list_or_a_stored_key(self):
        stored = re.compile(r"stocks:(?:state|ticker):[A-Z]")
        for rel in self.files:
            text = text_of(rel)
            if text is None:
                continue
            if rel.endswith((".yml", ".yaml")):
                self.assertNotRegex(text, r"(?m)^\s*tickers\s*:", rel)
            if not rel.startswith("tests/"):
                self.assertNotRegex(text, stored, rel)
        import config

        with self.assertRaises(config.ConfigError) as cm:
            config.normalize({"stocks": {"tickers": ["EXMPL"]}}, check=("stocks",))
        self.assertIn("The ticker list is private", str(cm.exception))

    def test_nothing_under_docs_holds_stock_data(self):
        kinds = re.compile(r'"kind"\s*:\s*"stocks\.(?:ticker|state|overview|congress)"')
        for rel in self.files:
            if not rel.startswith("docs/"):
                continue
            text = text_of(rel)
            if text is None:
                continue
            if rel.endswith(".json"):
                self.assertNotRegex(text, kinds, rel)
                self.assertNotIn("stocks.state", text, rel)
            self.assertNotIn("Not financial advice. This is an automated reading", text if rel.endswith(".json")
                             else "", rel)

    def test_every_fixture_is_described(self):
        folder = os.path.join(ss.FIXTURES)
        with open(os.path.join(folder, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        described = re.findall(r"`([^`]+)`", readme)
        for name in sorted(os.listdir(folder)):
            if name == "README.md":
                continue
            self.assertTrue(any(fnmatch.fnmatch(name, d) for d in described), f"{name} has no README row")

    @unittest.skipUnless(os.environ.get("STOCKS_PRIVATE_TICKERS"), "set STOCKS_PRIVATE_TICKERS to scan for them")
    def test_no_private_ticker_or_name_in_any_file(self):
        symbols = [s.strip() for s in os.environ["STOCKS_PRIVATE_TICKERS"].split(",") if s.strip()]
        names = [s.strip() for s in os.environ.get("STOCKS_PRIVATE_NAMES", "").split("|") if s.strip()]
        found = []
        for rel in self.files:
            if rel.startswith(NEWS_DATA):
                continue
            text = text_of(rel)
            if text is None:
                continue
            if rel == "config.yml":
                hits = privacy.find_private(text, symbols) + privacy.find_private(stocks_section(text), names, True)
            elif rel.startswith(STOCK_FILES):
                hits = privacy.find_private(text, symbols) + privacy.find_private(text, names, True)
            else:
                hits = privacy.find_private(text, symbols)
            if hits:
                found.append(f"{rel}: {len(hits)} private value{'s' if len(hits) != 1 else ''}")
        # The message names the file and a count, never the value itself.
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
