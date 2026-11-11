"""Offline tests for scripts/fetch_and_build.py. requests is faked, except in
one test that reads from a local server on 127.0.0.1.

Needs the packages in requirements.lock:
  pip install --require-hashes -r requirements-build.lock
  pip install --require-hashes --no-build-isolation -r requirements.lock
  python3 -m unittest discover -s tests
"""
import io
import json
import os
import shutil
import signal
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

from support import (  # support also puts scripts/ on sys.path
    CHANNEL_ID, DRIP, OTHER_URL, SUBSTACK_URL, YT_ABOUT_URL, YT_FEED_URL,
    FakeSession, LocalServer, fixture_bytes, ok, status,
)

import requests

import fetch_and_build as fab

NOW = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)

THEMES = {
    "Macro/Fed": ["fed", "fomc", "interest rates", "powell"],
    "Crypto": ["bitcoin", "btc", "crypto", "coinbase"],
    "AI/ML": ["ai", "machine learning", "generative", "openai", "nvidia"],
    "Tech/Earnings": ["earnings", "Shopify", "COIN"],
}


def themes_of(text):
    return fab.assign_themes(text, fab.compile_themes(THEMES))


def rss_feed(items):
    """An RSS document whose <item> elements hold the given XML fragments."""
    body = "".join(f"<item>{i}</item>" for i in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode("utf-8")


def prev_item(iid, source, url):
    return {"id": iid, "title": "Earlier post", "url": url, "source": source, "source_type": "rss",
            "published": "2026-10-14T00:00:00+00:00", "summary": "", "read_minutes": 1,
            "duration_sec": None, "themes": [], "bucket": "highlight"}


class ThemeMatchingTests(unittest.TestCase):
    def test_ai_is_a_whole_word(self):
        self.assertEqual(themes_of("He said it again, explained the chain"), [])
        self.assertEqual(themes_of("AI agents are here"), ["AI/ML"])
        self.assertEqual(themes_of("Two new AIs"), ["AI/ML"])
        self.assertEqual(themes_of("AI-powered search"), ["AI/ML"])

    def test_fed_does_not_match_feed(self):
        self.assertEqual(themes_of("Our feed was spoofed"), [])
        self.assertEqual(themes_of("The Fed holds"), ["Macro/Fed"])
        self.assertEqual(themes_of("what the feds want"), ["Macro/Fed"])

    def test_all_caps_keyword_is_case_sensitive(self):
        self.assertEqual(themes_of("bitcoin rallies"), ["Crypto"])
        self.assertNotIn("Tech/Earnings", themes_of("bitcoin rallies"))
        self.assertNotIn("Tech/Earnings", themes_of("a coin toss"))
        self.assertIn("Tech/Earnings", themes_of("COIN jumps 8%"))

    def test_other_keywords_ignore_case_and_allow_plural(self):
        self.assertEqual(themes_of("shopify results"), ["Tech/Earnings"])
        self.assertEqual(themes_of("Interest   Rates rise"), ["Macro/Fed"])
        self.assertEqual(themes_of("cheaper Mortgages"), [])  # not a keyword here
        self.assertEqual(sorted(themes_of("OpenAI and Nvidia earnings")), ["AI/ML", "Tech/Earnings"])

    def test_empty_config(self):
        self.assertEqual(fab.assign_themes("anything", fab.compile_themes(None)), [])
        self.assertEqual(fab.assign_themes("anything", fab.compile_themes({"X": None})), [])


class BucketTests(unittest.TestCase):
    def test_youtube_is_deeper_except_shorts(self):
        self.assertEqual(fab.classify_bucket("youtube", "https://www.youtube.com/watch?v=x", 1), "deeper")
        self.assertEqual(fab.classify_bucket("youtube", "https://www.youtube.com/shorts/x", 1), "highlight")

    def test_rss_uses_read_minutes(self):
        self.assertEqual(fab.classify_bucket("rss", "https://a.example/p", 5), "highlight")
        self.assertEqual(fab.classify_bucket("rss", "https://a.example/p", 6), "deeper")

    def test_read_minutes_come_from_full_content(self):
        raw = {
            "title": "Long one",
            "link": "https://a.example/long",
            "summary": "Short teaser.",
            "content": [{"value": "<p>" + " ".join(["word"] * 1300) + "</p>"}],
        }
        item = fab.normalize_entry(raw, "A", "rss", [], {}, fab.utc_now())
        self.assertEqual(item["read_minutes"], 6)
        self.assertEqual(item["bucket"], "deeper")
        self.assertEqual(item["summary"], "Short teaser.")

    def test_tags_do_not_count_as_words(self):
        self.assertEqual(fab.estimate_read_time('<a href="x"><span class="y">one</span></a>'), 1)
        self.assertEqual(fab.estimate_read_time(""), 1)

    def test_many_unclosed_angle_brackets_are_fast(self):
        # No ">" follows them, so the old pattern <[^>]+> took several seconds
        # here (quadratic).
        body = "<p>" + " ".join(["word"] * 1300) + "</p>" + "<" * 100_000
        start = time.monotonic()
        self.assertEqual(fab.estimate_read_time(body), 6)
        self.assertLess(time.monotonic() - start, 1)


class UrlAndDedupeTests(unittest.TestCase):
    def test_web_url(self):
        self.assertEqual(fab.web_url("https://a.example/x"), "https://a.example/x")
        self.assertEqual(fab.web_url("HTTP://a.example/x"), "HTTP://a.example/x")
        for bad in ("javascript:alert(1)", "data:text/html,x", "mailto:a@b", "/relative", "", None, "https://"):
            self.assertIsNone(fab.web_url(bad), bad)

    def test_null_url_items_are_not_collapsed(self):
        prev = []
        session = FakeSession({SUBSTACK_URL: ok(fixture_bytes("substack_feed.xml"))})
        sources = [fab.Source("Example", "rss", SUBSTACK_URL)]
        items, status_list = fab.build_items(sources, session, [], prev, fab.utc_now())
        no_url = [i for i in items if i["url"] is None]
        self.assertEqual(len(no_url), 2)
        self.assertEqual(len({i["id"] for i in no_url}), 2)
        # The repost shares its url with the first post and is dropped.
        urls = [i["url"] for i in items if i["url"]]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(len(items), 5)
        self.assertEqual(status_list[0]["count"], 5)

    def test_same_url_in_two_sources_is_kept_once(self):
        body = fixture_bytes("substack_feed.xml")
        session = FakeSession({SUBSTACK_URL: ok(body), OTHER_URL: ok(body)})
        sources = [fab.Source("A", "rss", SUBSTACK_URL), fab.Source("B", "rss", OTHER_URL)]
        items, status_list = fab.build_items(sources, session, [], [], fab.utc_now())
        self.assertEqual(status_list[0]["count"], 5)
        # B's web links are duplicates; only its two link-less posts are new
        # (their ids include the source label).
        self.assertEqual(status_list[1]["count"], 2)


class FetchTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(fab, "RETRY_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_retries_once_on_5xx(self):
        session = FakeSession({SUBSTACK_URL: [status(503), ok(fixture_bytes("substack_feed.xml"))]})
        entries, error = fab.fetch_feed(session, "Example", SUBSTACK_URL)
        self.assertIsNone(error)
        self.assertEqual(len(entries), 6)
        self.assertEqual(session.calls, [SUBSTACK_URL, SUBSTACK_URL])

    def test_retries_once_on_connection_error(self):
        session = FakeSession({SUBSTACK_URL: [requests.ConnectionError("reset"), requests.ConnectionError("reset")]})
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            entries, error = fab.fetch_feed(session, "Example", SUBSTACK_URL)
        self.assertEqual((entries, error), ([], "connection error"))
        self.assertEqual(len(session.calls), 2)
        self.assertIn("[warn] Example: ConnectionError", out.getvalue())

    def test_4xx_is_not_retried(self):
        session = FakeSession({SUBSTACK_URL: status(404)})
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            entries, error = fab.fetch_feed(session, "Example", SUBSTACK_URL)
        self.assertEqual((entries, error), ([], "HTTP 404"))
        self.assertEqual(len(session.calls), 1)
        self.assertIn("[warn] Example: HTTP 404", out.getvalue())

    def test_timeout_is_not_retried(self):
        session = FakeSession({SUBSTACK_URL: requests.ReadTimeout("slow")})
        entries, error = fab.fetch_feed(session, "Example", SUBSTACK_URL)
        self.assertEqual((entries, error), ([], "timeout"))
        self.assertEqual(len(session.calls), 1)

    def test_html_page_is_not_a_feed(self):
        session = FakeSession({SUBSTACK_URL: ok(b"<html><body><p>Sign in</p></body></html>")})
        entries, error = fab.fetch_feed(session, "Example", SUBSTACK_URL)
        self.assertEqual(entries, [])
        self.assertEqual(error, "not a feed")

    def test_oversized_body_is_refused(self):
        session = FakeSession({SUBSTACK_URL: ok(b"x" * 2048)})
        with mock.patch.object(fab, "MAX_BYTES", 1024):
            entries, error = fab.fetch_feed(session, "Example", SUBSTACK_URL)
        self.assertEqual((entries, error), ([], "response too large"))

    def test_feedparser_never_gets_a_string(self):
        session = FakeSession({SUBSTACK_URL: ok(fixture_bytes("substack_feed.xml"))})
        real_parse = fab.feedparser.parse
        seen = []

        def spy(source, *args, **kwargs):
            seen.append(source)
            return real_parse(source, *args, **kwargs)

        with mock.patch.object(fab.feedparser, "parse", side_effect=spy):
            fab.fetch_feed(session, "Example", SUBSTACK_URL)
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], io.BytesIO)


class TimeLimitTests(unittest.TestCase):
    """A source that takes too long fails on its own and the run goes on."""

    def setUp(self):
        for name, value in (("SOURCE_SECONDS", 1), ("RETRY_DELAY", 0)):
            patcher = mock.patch.object(fab, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @unittest.skipUnless(hasattr(signal, "setitimer"), "needs signal.setitimer")
    def test_slow_drip_host_fails_its_source_only(self):
        session = fab.new_session()
        session.trust_env = False  # no proxy between the test and 127.0.0.1
        handler = signal.getsignal(signal.SIGALRM)
        with LocalServer({"/drip": DRIP, "/feed": fixture_bytes("substack_feed.xml")}) as server:
            prev = [prev_item("old", "Slow", "https://slow.example/p/old")]
            sources = [fab.Source("Slow", "rss", server.url("/drip")), fab.Source("Example", "rss", server.url("/feed"))]
            start = time.monotonic()
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                items, status_list = fab.build_items(sources, session, [], prev, NOW)
            elapsed = time.monotonic() - start
        # The drip would take 100000 x 0.05 s; the source limit is 1 s.
        self.assertLess(elapsed, 5)
        self.assertIn("[warn] Slow: not finished within 1.0 s", out.getvalue())
        self.assertEqual(status_list, [
            {"label": "Slow", "type": "rss", "ok": False, "count": 1, "error": "timeout"},
            {"label": "Example", "type": "rss", "ok": True, "count": 5, "error": None},
        ])
        self.assertEqual([i["id"] for i in items if i.get("stale")], ["old"])
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertIs(signal.getsignal(signal.SIGALRM), handler)

    @unittest.skipUnless(hasattr(signal, "setitimer"), "needs signal.setitimer")
    def test_slow_parse_fails_its_source_only(self):
        # feedparser's sanitizer is quadratic on a run of "<!--": this feed takes
        # several seconds to parse.
        hostile = rss_feed(["<title>Hostile</title><link>https://bad.example/p</link>"
                            f"<description><![CDATA[{'<!--' * 60_000}]]></description>"])
        session = FakeSession({SUBSTACK_URL: ok(hostile), OTHER_URL: ok(fixture_bytes("substack_feed.xml"))})
        sources = [fab.Source("Hostile", "rss", SUBSTACK_URL), fab.Source("Example", "rss", OTHER_URL)]
        start = time.monotonic()
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            items, status_list = fab.build_items(sources, session, [], [], NOW)
        self.assertLess(time.monotonic() - start, 4)
        self.assertIn("[warn] Hostile: not finished within 1.0 s", out.getvalue())
        self.assertEqual(status_list[0]["error"], "timeout")
        self.assertEqual(status_list[1], {"label": "Example", "type": "rss", "ok": True, "count": 5, "error": None})

    def test_spent_run_budget_skips_the_remaining_sources(self):
        session = FakeSession({SUBSTACK_URL: ok(fixture_bytes("substack_feed.xml"))})
        prev = [prev_item("old", "Example", "https://example.substack.com/p/old")]
        sources = [fab.Source("Example", "rss", SUBSTACK_URL)]
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            items, status_list = fab.build_items(sources, session, [], prev, NOW, fab.RunClock(0))
        self.assertEqual(session.calls, [])
        self.assertIn("budget is spent", out.getvalue())
        self.assertEqual(status_list[0], {"label": "Example", "type": "rss", "ok": False, "count": 1,
                                          "error": "skipped, out of time"})
        self.assertTrue(items[0]["stale"])


class RunSecondsTests(unittest.TestCase):
    """FEED_RUN_SECONDS shortens the run's wall clock (the morning run of
    daily.yml sets it) and can never lengthen it."""

    def test_default_and_override(self):
        self.assertEqual(fab.run_seconds({}), fab.RUN_SECONDS)
        self.assertEqual(fab.run_seconds({"FEED_RUN_SECONDS": ""}), fab.RUN_SECONDS)
        self.assertEqual(fab.run_seconds({"FEED_RUN_SECONDS": " 180 "}), 180)
        self.assertEqual(fab.run_seconds({"FEED_RUN_SECONDS": str(fab.RUN_SECONDS)}), fab.RUN_SECONDS)

    def test_bad_values_keep_the_default_and_say_so(self):
        for bad in ("0", "29", "361", "3600", "-5", "3m", "1e2", "180.5"):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(fab.run_seconds({"FEED_RUN_SECONDS": bad}), fab.RUN_SECONDS, bad)
            self.assertIn("[warn] FEED_RUN_SECONDS must be a whole number from 30 to 360", out.getvalue())

    def test_main_gives_the_run_clock_the_shorter_budget(self):
        seen = []

        class Stop(Exception):
            pass

        def clock(seconds=None):
            seen.append(seconds)
            raise Stop

        for env, want in (({"FEED_RUN_SECONDS": "180"}, 180), ({}, fab.RUN_SECONDS)):
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(fab, "RunClock", clock), \
                    mock.patch.object(fab.config, "load_config", return_value={"feeds": {}, "themes": {}}), \
                    mock.patch.object(fab, "load_json", return_value=None):
                with self.assertRaises(Stop):
                    fab.main(session=object())
            self.assertEqual(seen.pop(), want)


class PipelineTestCase(unittest.TestCase):
    """Runs main() against a temporary copy of the repo layout."""

    FEEDS = {
        "youtube": [{"handle": "ExampleChannel"}],
        "rss": [{"label": "Example", "feed_url": SUBSTACK_URL}],
        "websites": [],
        "twitter_embeds": [{"handle": "Polymarket"}, {"handle": "zerohedge"}],
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="news-digest-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        os.makedirs(os.path.join(self.tmp, "docs", "data"))
        self.paths = {
            "ROOT": self.tmp,
            "CONFIG_YML": os.path.join(self.tmp, "config.yml"),
            "DATA_OUT": os.path.join(self.tmp, "docs", "data", "items.json"),
            "TWITTER_OUT": os.path.join(self.tmp, "docs", "data", "twitter_posts.json"),
            "CHANNEL_CACHE": os.path.join(self.tmp, "data", "youtube_channels.json"),
        }
        for name, value in list(self.paths.items()) + [("RETRY_DELAY", 0)]:
            patcher = mock.patch.object(fab, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # A fixed clock, so the fixture dates stay inside the carry-over limit.
        patcher = mock.patch.object(fab, "utc_now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_feeds(self.FEEDS)

    def write_feeds(self, feeds):
        with open(self.paths["CONFIG_YML"], "w", encoding="utf-8") as f:
            json.dump({"feeds": feeds, "themes": THEMES}, f)  # JSON is valid YAML

    def routes(self, **overrides):
        routes = {
            YT_ABOUT_URL: ok(fixture_bytes("youtube_about.html")),
            YT_FEED_URL: ok(fixture_bytes("youtube_feed.xml")),
            SUBSTACK_URL: ok(fixture_bytes("substack_feed.xml")),
        }
        routes.update(overrides)
        return routes

    def run_main(self, session):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            fab.main(session=session)
        return out.getvalue()

    def read(self, key):
        with open(self.paths[key], "r", encoding="utf-8") as f:
            return json.load(f)

    def raw(self, key):
        with open(self.paths[key], "rb") as f:
            return f.read()


class ContractTests(PipelineTestCase):
    def test_outputs_match_the_contract(self):
        session = FakeSession(self.routes())
        self.run_main(session)
        doc = self.read("DATA_OUT")
        self.assertEqual(set(doc), {"generated_at", "items", "sources"})
        datetime.fromisoformat(doc["generated_at"])
        required = {
            "id": str, "title": str, "source": str, "source_type": str, "published": str,
            "first_seen": str, "summary": str, "read_minutes": int, "themes": list, "bucket": str,
        }
        self.assertEqual(len(doc["items"]), 7)
        for item in doc["items"]:
            for key, typ in required.items():
                self.assertIsInstance(item[key], typ, key)
            self.assertTrue(item["url"] is None or item["url"].startswith(("https://", "http://")))
            self.assertIsNone(item["duration_sec"])
            self.assertIn(item["source_type"], ("youtube", "rss"))
            self.assertIn(item["bucket"], ("highlight", "deeper"))
            self.assertIsNotNone(datetime.fromisoformat(item["published"]).tzinfo)
            self.assertEqual(item["first_seen"], NOW.isoformat())  # all new on the first run
            self.assertNotIn("stale", item)
        published = [datetime.fromisoformat(i["published"]) for i in doc["items"]]
        self.assertEqual(published, sorted(published, reverse=True))

        by_title = {i["title"]: i for i in doc["items"]}
        self.assertEqual(by_title["An hour on NVIDIA earnings"]["bucket"], "deeper")
        self.assertEqual(by_title["An hour on NVIDIA earnings"]["source"], "@ExampleChannel")
        self.assertEqual(by_title["COIN in 60 seconds"]["bucket"], "highlight")
        self.assertIn("Tech/Earnings", by_title["COIN in 60 seconds"]["themes"])
        self.assertEqual(by_title["A long essay on generative models"]["bucket"], "deeper")
        self.assertEqual(by_title["The Fed said nothing new about interest rates"]["bucket"], "highlight")
        self.assertEqual(by_title["Bitcoin and the feed of the week"]["themes"], ["Crypto"])

        self.assertEqual(doc["sources"], [
            {"label": "@ExampleChannel", "type": "youtube", "ok": True, "count": 2, "error": None},
            {"label": "Example", "type": "rss", "ok": True, "count": 5, "error": None},
        ])

        tw = self.read("TWITTER_OUT")
        self.assertEqual(set(tw), {"generated_at", "accounts"})
        self.assertEqual(tw["accounts"], [
            {"handle": "Polymarket", "url": "https://x.com/Polymarket", "posts": []},
            {"handle": "zerohedge", "url": "https://x.com/zerohedge", "posts": []},
        ])
        self.assertEqual(self.read("CHANNEL_CACHE"), {"ExampleChannel": CHANNEL_ID})

    def test_no_x_requests_without_nitter_instances(self):
        session = FakeSession(self.routes())
        self.run_main(session)
        self.assertFalse([u for u in session.calls if "nitter" in u or "/rss" in u])

    def test_config_errors_stop_the_run(self):
        with open(self.paths["CONFIG_YML"], "w", encoding="utf-8") as f:
            f.write("feeds:\n  rss: https://a.example/feed\n")
        with self.assertRaises(SystemExit) as cm:
            self.run_main(FakeSession(self.routes()))
        self.assertIn("feeds.rss must be a list", str(cm.exception))

    def test_a_mistake_in_the_learn_section_does_not_stop_the_feeds(self):
        with open(self.paths["CONFIG_YML"], "w", encoding="utf-8") as f:
            json.dump({"feeds": self.FEEDS, "themes": THEMES, "learn": {"effort": "extreme"}}, f)
        self.run_main(FakeSession(self.routes()))
        self.assertEqual(len(self.read("DATA_OUT")["items"]), 7)

    def test_nitter_instances_are_opt_in(self):
        cfg = dict(self.FEEDS, nitter_instances=["https://nitter.example"])
        self.write_feeds(cfg)
        session = FakeSession(self.routes())
        self.run_main(session)
        self.assertIn("https://nitter.example/Polymarket/rss", session.calls)


class CacheTests(PipelineTestCase):
    def test_handle_is_resolved_once_then_cached(self):
        session = FakeSession(self.routes())
        self.run_main(session)
        self.assertEqual(session.calls.count(YT_ABOUT_URL), 1)
        session2 = FakeSession(self.routes())
        self.run_main(session2)
        self.assertNotIn(YT_ABOUT_URL, session2.calls)
        self.assertIn(YT_FEED_URL, session2.calls)

    def test_channel_id_in_feeds_skips_the_lookup(self):
        self.write_feeds(dict(self.FEEDS, youtube=[{"handle": "ExampleChannel", "channel_id": CHANNEL_ID, "label": "Example TV"}]))
        session = FakeSession(self.routes())
        self.run_main(session)
        self.assertNotIn(YT_ABOUT_URL, session.calls)
        self.assertEqual(self.read("CHANNEL_CACHE"), {})
        self.assertEqual(self.read("DATA_OUT")["sources"][0]["label"], "Example TV")

    def test_404_on_a_cached_channel_drops_it_from_the_cache(self):
        self.run_main(FakeSession(self.routes()))
        self.run_main(FakeSession(self.routes(**{YT_FEED_URL: status(404)})))
        self.assertEqual(self.read("CHANNEL_CACHE"), {})


class CarryOverTests(PipelineTestCase):
    def test_failed_source_keeps_its_items_marked_stale(self):
        self.run_main(FakeSession(self.routes()))
        before = {i["id"]: i for i in self.read("DATA_OUT")["items"] if i["source"] == "Example"}
        out = self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: status(503)})))
        self.assertIn("[warn] Example: HTTP 503", out)
        doc = self.read("DATA_OUT")
        after = {i["id"]: i for i in doc["items"] if i["source"] == "Example"}
        self.assertEqual(set(after), set(before))
        self.assertTrue(all(i["stale"] is True for i in after.values()))
        self.assertTrue(all("stale" not in i for i in doc["items"] if i["source"] != "Example"))
        src = {s["label"]: s for s in doc["sources"]}
        self.assertEqual(src["Example"], {"label": "Example", "type": "rss", "ok": False, "count": 5, "error": "HTTP 503"})
        self.assertTrue(src["@ExampleChannel"]["ok"])

        # Back to normal: fresh items again, without the stale flag.
        self.run_main(FakeSession(self.routes()))
        doc = self.read("DATA_OUT")
        self.assertTrue(all("stale" not in i for i in doc["items"]))
        self.assertTrue(all(s["ok"] for s in doc["sources"]))

    def test_unresolvable_handle_keeps_previous_items(self):
        self.run_main(FakeSession(self.routes()))
        os.remove(self.paths["CHANNEL_CACHE"])
        self.run_main(FakeSession(self.routes(**{YT_ABOUT_URL: status(429)})))
        doc = self.read("DATA_OUT")
        yt = [i for i in doc["items"] if i["source_type"] == "youtube"]
        self.assertEqual(len(yt), 2)
        self.assertTrue(all(i["stale"] for i in yt))
        self.assertEqual(doc["sources"][0]["error"], "handle not resolved")

    def test_zero_entries_after_items_counts_as_a_failure(self):
        self.run_main(FakeSession(self.routes()))
        empty = b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
        self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: ok(empty)})))
        src = {s["label"]: s for s in self.read("DATA_OUT")["sources"]}
        self.assertEqual(src["Example"]["error"], "no entries")
        self.assertFalse(src["Example"]["ok"])

    def test_zero_entries_from_a_new_source_is_fine(self):
        empty = b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
        self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: ok(empty)})))
        src = {s["label"]: s for s in self.read("DATA_OUT")["sources"]}
        self.assertEqual(src["Example"], {"label": "Example", "type": "rss", "ok": True, "count": 0, "error": None})

    def test_carried_items_are_rechecked(self):
        prev = [
            {"id": "a", "title": "Old", "url": "https://a.example/old", "source": "S", "source_type": "rss",
             "published": "2025-01-01T00:00:00+00:00", "summary": "", "read_minutes": 1, "duration_sec": None,
             "themes": ["Stale theme"], "bucket": "highlight"},
            {"id": "b", "title": "Recent", "url": "javascript:x", "source": "S", "source_type": "rss",
             "published": "2026-10-14T00:00:00+00:00", "summary": "", "read_minutes": 9, "duration_sec": None,
             "themes": [], "bucket": "highlight"},
            {"id": "c", "title": "No date", "url": "https://a.example/c", "source": "S", "source_type": "rss"},
        ]
        out = fab.carry_over(prev, "rss", [])
        self.assertEqual([i["id"] for i in out], ["a", "b"])  # an old item is kept too
        self.assertEqual(out[0]["themes"], [])            # recomputed with the current rules
        self.assertIsNone(out[1]["url"])                  # re-checked on the way through
        self.assertEqual(out[1]["bucket"], "deeper")
        self.assertTrue(all(i["stale"] for i in out))

    def test_previous_file_without_sources_is_accepted(self):
        # The files written before this change have no "sources" and no "stale".
        legacy = {"items": [{
            "id": "x", "title": "Legacy post", "url": "https://example.substack.com/p/legacy",
            "source": "Example", "source_type": "rss", "published": fab.iso(fab.utc_now() - timedelta(days=2)),
            "summary": "", "read_minutes": 1, "duration_sec": None, "themes": [], "bucket": "highlight",
        }], "generated_at": "2026-10-14T23:37:45.889276+00:00"}
        with open(self.paths["DATA_OUT"], "w", encoding="utf-8") as f:
            json.dump(legacy, f)
        self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: requests.ConnectionError("down")})))
        doc = self.read("DATA_OUT")
        legacy_items = [i for i in doc["items"] if i["id"] == "x"]
        self.assertEqual(len(legacy_items), 1)
        self.assertTrue(legacy_items[0]["stale"])


class FirstSeenTests(PipelineTestCase):
    def items(self):
        return {i["title"]: i for i in self.read("DATA_OUT")["items"]}

    def test_first_seen_stays_put_and_new_items_get_the_run_time(self):
        older = [f"<title>Post {n}</title><link>https://example.substack.com/p/post-{n}</link>"
                 f"<pubDate>{format_datetime(NOW - timedelta(hours=n + 1), usegmt=True)}</pubDate>"
                 for n in range(2)]
        self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: ok(rss_feed(older))})))
        later = NOW + timedelta(hours=6)
        newer = ("<title>Post new</title><link>https://example.substack.com/p/new</link>"
                 f"<pubDate>{format_datetime(NOW + timedelta(hours=1), usegmt=True)}</pubDate>")
        with mock.patch.object(fab, "utc_now", return_value=later):
            self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: ok(rss_feed([newer] + older))})))
        items = self.items()
        self.assertEqual(items["Post 0"]["first_seen"], NOW.isoformat())
        self.assertEqual(items["Post 1"]["first_seen"], NOW.isoformat())
        self.assertEqual(items["Post new"]["first_seen"], later.isoformat())
        self.assertEqual(items["Post new"]["published"], (NOW + timedelta(hours=1)).isoformat())

    def test_stale_items_keep_their_first_seen(self):
        self.run_main(FakeSession(self.routes()))
        with mock.patch.object(fab, "utc_now", return_value=NOW + timedelta(days=1)):
            self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: status(503)})))
        stale = [i for i in self.read("DATA_OUT")["items"] if i.get("stale")]
        self.assertTrue(stale)
        self.assertTrue(all(i["first_seen"] == NOW.isoformat() for i in stale))

    def test_items_from_an_older_file_get_their_published_time(self):
        legacy = {"items": [{
            "id": fab.md5_hex("https://example.substack.com/p/a"), "title": "Legacy post",
            "url": "https://example.substack.com/p/a", "source": "Example", "source_type": "rss",
            "published": "2026-10-14T08:00:00+00:00", "summary": "", "read_minutes": 1,
            "duration_sec": None, "themes": [], "bucket": "highlight",
        }, {
            "id": "future", "title": "Dated ahead", "url": "https://example.substack.com/p/future",
            "source": "Example", "source_type": "rss", "published": "2026-10-20T00:00:00+00:00",
            "summary": "", "read_minutes": 1, "duration_sec": None, "themes": [], "bucket": "highlight",
        }]}
        with open(self.paths["DATA_OUT"], "w", encoding="utf-8") as f:
            json.dump(legacy, f)
        self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: status(503)})))
        items = self.items()
        self.assertEqual(items["Legacy post"]["first_seen"], "2026-10-14T08:00:00+00:00")
        self.assertEqual(items["Dated ahead"]["first_seen"], NOW.isoformat())  # never after the run

    def test_entry_seen_again_in_the_feed_keeps_the_older_file_time(self):
        url = "https://example.substack.com/p/kept"
        legacy = {"items": [{
            "id": fab.md5_hex(url), "title": "Kept", "url": url, "source": "Example", "source_type": "rss",
            "published": "2026-10-14T08:00:00+00:00", "first_seen": "2026-10-14T09:30:00+00:00",
            "summary": "", "read_minutes": 1, "duration_sec": None, "themes": [], "bucket": "highlight",
        }]}
        with open(self.paths["DATA_OUT"], "w", encoding="utf-8") as f:
            json.dump(legacy, f)
        feed = rss_feed([f"<title>Kept</title><link>{url}</link>"
                         f"<pubDate>{format_datetime(datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc), usegmt=True)}</pubDate>"])
        self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: ok(feed)})))
        self.assertEqual(self.items()["Kept"]["first_seen"], "2026-10-14T09:30:00+00:00")


class LabelTests(PipelineTestCase):
    def test_sources_that_share_a_label_are_kept_apart(self):
        other = fixture_bytes("substack_feed.xml").replace(b"example.substack.com", b"other.example.com")
        self.write_feeds(dict(self.FEEDS, rss=[{"feed_url": SUBSTACK_URL}, {"feed_url": OTHER_URL}]))
        out = self.run_main(FakeSession(self.routes(**{OTHER_URL: ok(other)})))
        self.assertIn("[warn] more than one source is labeled 'Newsletter'", out)
        a, b = "Newsletter (example.substack.com)", "Newsletter (other.example.com)"
        self.assertEqual([s["label"] for s in self.read("DATA_OUT")["sources"]], ["@ExampleChannel", a, b])

        # When A fails, only A's items carry over; B's stay fresh and counted.
        self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: status(503), OTHER_URL: ok(other)})))
        doc = self.read("DATA_OUT")
        src = {s["label"]: s for s in doc["sources"]}
        self.assertEqual(src[a], {"label": a, "type": "rss", "ok": False, "count": 5, "error": "HTTP 503"})
        self.assertEqual(src[b], {"label": b, "type": "rss", "ok": True, "count": 5, "error": None})
        self.assertTrue(all(i.get("stale") for i in doc["items"] if i["source"] == a))
        self.assertTrue(all("stale" not in i for i in doc["items"] if i["source"] == b))

    def test_same_host_gets_a_number(self):
        sources = [
            fab.Source("News", "rss", "https://www.example.com/a"),
            fab.Source("News", "rss", "https://www.example.com/b"),
            fab.Source("News", "rss", error="invalid feed_url"),
            fab.Source("News (example.com) 3", "rss", "https://c.example/feed"),
            fab.Source("Solo", "rss", "https://solo.example/feed"),
        ]
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            fab.make_labels_unique(sources)
        self.assertEqual([s.label for s in sources], [
            "News (example.com)", "News (example.com) 2", "News", "News (example.com) 3", "Solo",
        ])


class WriteIfChangedTests(PipelineTestCase):
    def test_unchanged_run_writes_nothing(self):
        self.run_main(FakeSession(self.routes()))
        first = {k: self.raw(k) for k in ("DATA_OUT", "TWITTER_OUT", "CHANNEL_CACHE")}
        out = self.run_main(FakeSession(self.routes()))
        second = {k: self.raw(k) for k in ("DATA_OUT", "TWITTER_OUT", "CHANNEL_CACHE")}
        self.assertEqual(first, second)
        self.assertIn("No change in docs/data/items.json", out)
        self.assertIn("No change in docs/data/twitter_posts.json", out)

    def test_changed_items_are_written_with_a_new_timestamp(self):
        self.run_main(FakeSession(self.routes()))
        doc = self.read("DATA_OUT")
        doc["generated_at"] = "2000-01-01T00:00:00+00:00"
        with open(self.paths["DATA_OUT"], "w", encoding="utf-8") as f:
            json.dump(doc, f)
        self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: status(500)})))
        self.assertNotEqual(self.read("DATA_OUT")["generated_at"], "2000-01-01T00:00:00+00:00")

    def test_write_if_changed_helper(self):
        path = os.path.join(self.tmp, "x.json")
        now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(fab.write_if_changed(path, {"items": [1]}, now))
        later = now + timedelta(hours=1)
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertFalse(fab.write_if_changed(path, {"items": [1]}, later))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["generated_at"], now.isoformat())
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertTrue(fab.write_if_changed(path, {"items": [1, 2]}, later))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["generated_at"], later.isoformat())

    def test_entries_without_dates_keep_their_first_seen_time(self):
        feed = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>'
                b'<item><title>Undated</title><link>https://example.substack.com/p/undated</link></item>'
                b'</channel></rss>')
        self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: ok(feed)})))
        first = self.raw("DATA_OUT")
        later = NOW + timedelta(hours=3)
        with mock.patch.object(fab, "utc_now", return_value=later):
            self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: ok(feed)})))
        self.assertEqual(first, self.raw("DATA_OUT"))

    def test_more_undated_entries_than_the_cap_keep_the_same_items(self):
        feed = rss_feed([f"<title>Page {n}</title><link>https://example.substack.com/p/page-{n}</link>"
                         for n in range(80)])
        self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: ok(feed)})))
        first = self.raw("DATA_OUT")
        with mock.patch.object(fab, "utc_now", return_value=NOW + timedelta(hours=1)):
            out = self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: ok(feed)})))
        self.assertIn("No change in docs/data/items.json", out)
        self.assertEqual(first, self.raw("DATA_OUT"))
        titles = [i["title"] for i in self.read("DATA_OUT")["items"] if i["source"] == "Example"]
        # The first entries in feed order.
        self.assertEqual(sorted(titles), sorted(f"Page {n}" for n in range(fab.MAX_ITEMS_PER_SOURCE)))

    def test_new_undated_entry_does_not_push_out_stored_items(self):
        dated = [f"<title>Post {n}</title><link>https://example.substack.com/p/post-{n}</link>"
                 f"<pubDate>{format_datetime(NOW - timedelta(hours=n + 1), usegmt=True)}</pubDate>"
                 for n in range(fab.MAX_ITEMS_PER_SOURCE)]
        self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: ok(rss_feed(dated))})))
        first = self.raw("DATA_OUT")
        undated = "<title>Undated</title><link>https://example.substack.com/p/undated</link>"
        with mock.patch.object(fab, "utc_now", return_value=NOW + timedelta(hours=1)):
            self.run_main(FakeSession(self.routes(**{SUBSTACK_URL: ok(rss_feed([undated] + dated))})))
        self.assertEqual(first, self.raw("DATA_OUT"))


if __name__ == "__main__":
    unittest.main()
