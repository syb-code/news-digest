"""Offline tests for the learning briefing (scripts/learn/). No network: feeds
come from a fake session, Claude from a fake client or a local server on
127.0.0.1.

The brief.py tests that need the anthropic SDK and the render.py tests that
need python-pptx are skipped when those packages are not installed:
  pip install --require-hashes -r requirements-brief.lock   (anthropic)
  python3 -m unittest discover -s tests
"""
import http.server
import importlib.util
import io
import json
import os
import shutil
import re
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from support import FIXTURES, ROOT, FakeSession, fixture_bytes, ok  # support also puts scripts/ on sys.path

import fetch_and_build as fab
import learn
from learn import brief, candidates

HAS_ANTHROPIC = importlib.util.find_spec("anthropic") is not None
HAS_PPTX = importlib.util.find_spec("pptx") is not None
if HAS_ANTHROPIC:
    import anthropic
if HAS_PPTX:
    from pptx import Presentation
    from pptx.dml.color import RGBColor

    from learn import render

LEARN_FIXTURES = os.path.join(FIXTURES, "learn")
NOW = datetime(2026, 11, 11, 13, 45, tzinfo=timezone.utc)  # 5:45 AM in Los Angeles
OPENAI_FEED = "https://openai.example.com/news/rss.xml"
OLLAMA_FEED = "https://github.com/ollama/ollama/releases.atom"
BROKEN_FEED = "https://broken.example.com/feed"
BOILERPLATE = ("Subscribe to the podcast version wherever you listen. Get it ad free on patreon. "
               "Learn more about the show and our sponsors.")


def fixture_path(name):
    return os.path.join(LEARN_FIXTURES, name)


def quiet(fn, *args, **kwargs):
    """Run fn with its log lines captured; returns (result, log text)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


def feed_item(title, source, hours_ago, url, summary="", first_seen_hours=None):
    return {
        "id": url, "title": title, "url": url, "source": source, "source_type": "rss",
        "published": ago(hours_ago), "first_seen": ago(hours_ago if first_seen_hours is None else first_seen_hours),
        "summary": summary, "read_minutes": 3, "duration_sec": None, "themes": [], "bucket": "highlight",
    }


def story_items():
    """items.json items: a GPT-5.5 story in three newsletters (one of them a
    roundup that also mentions Ollama), a Fed story in two sources (off lane),
    three videos from one channel that share only boilerplate, and old items."""
    return [
        feed_item("OpenAI ships GPT-5.5: 2M context window", "Newsletter A", 3, "https://a.example.com/p/gpt-5-5",
                  "OpenAI shipped GPT-5.5 today. The context window grows to 2M tokens."),
        feed_item("GPT-5.5 is here: what the 2M context window means", "Newsletter B", 2, "https://b.example.com/p/gpt-5-5-context",
                  "A first look at the 2M context window in GPT-5.5."),
        feed_item("Last Week in AI #348: GPT-5.5, Ollama MLX backend and more", "Last Week in AI", 1,
                  "https://lastweek.example.com/p/348", "GPT-5.5 ships with a 2M context window; Ollama adds an MLX backend."),
        feed_item("Fed holds interest rates steady", "Macro Weekly", 5, "https://macro.example.com/fed-holds",
                  "The Fed left interest rates unchanged and signaled patience."),
        feed_item("Why the Fed held interest rates steady", "@Monetary-Matters", 4, "https://www.youtube.com/watch?v=fed123",
                  "Powell explains why the Fed held interest rates steady."),
        feed_item("Agents are eating software", "@AIDailyBrief", 6, "https://www.youtube.com/watch?v=v1", BOILERPLATE),
        feed_item("Robotics startups raise record funding", "@AIDailyBrief", 7, "https://www.youtube.com/watch?v=v2", BOILERPLATE),
        feed_item("Chip export rules change again", "@AIDailyBrief", 80, "https://www.youtube.com/watch?v=v3", BOILERPLATE),
        # First seen today, but published long ago (a newly added feed's backlog): left out.
        feed_item("A 2025 post about GPT-4", "New Feed", 24 * 40, "https://new.example.com/old", "Old.", first_seen_hours=1),
        # Not https: left out.
        feed_item("Insecure link", "Newsletter A", 2, "http://insecure.example.com/x", "Plain http."),
    ]


def lane_routes():
    return {
        OPENAI_FEED: ok(fixture_bytes(os.path.join("learn", "openai_feed.xml"))),
        OLLAMA_FEED: ok(fixture_bytes(os.path.join("learn", "ollama_releases.atom"))),
    }


def settings(**learn_overrides):
    cfg = {"timezone": "America/Los_Angeles", "learn": dict(learn.LEARN_DEFAULTS)}
    cfg["learn"].update(learn_overrides)
    cfg["learn"] = learn.clean_learn_settings(cfg["learn"])
    return cfg


def build(items, seen=(), lane_items=None, now=NOW, lanes_path=None, **learn_overrides):
    """The candidates document for items (and the fixture lanes), offline."""
    with open(lanes_path or fixture_path("lanes.md"), encoding="utf-8") as f:
        lanes = learn.parse_lanes(f.read())[0]
    if lane_items is None:
        with mock.patch.object(fab, "RETRY_DELAY", 0):
            (lane_items, status), _ = quiet(candidates.fetch_lane_items, lanes, FakeSession(lane_routes()), now,
                                            now - timedelta(hours=36))
    else:
        status = []
    doc, log = quiet(candidates.build_candidates, settings(**learn_overrides), {"items": items}, lanes, list(seen),
                     now, lane_items, status)
    return doc


def topic_titled(doc, words):
    for t in doc["topics"]:
        if all(w.lower() in " ".join(i["title"] for i in t["items"]).lower() for w in words):
            return t
    return None


# ---------------------------------------------------------------- lanes.md

class LanesFileTests(unittest.TestCase):
    def test_repository_lanes_file(self):
        with open(learn.LANES_MD, encoding="utf-8") as f:
            text = f.read()
        lanes, problems = learn.parse_lanes(text)
        self.assertEqual(problems, [])
        self.assertEqual([l.name for l in lanes], [
            "Best practices", "Best skills", "Best processes", "Efficiencies", "Frontier models",
            "Agents and loops", "Local inference", "AI in marketing"])
        for lane in lanes:
            self.assertTrue(lane.watch, lane.name)
            self.assertGreaterEqual(len(lane.keywords), 3, lane.name)
            self.assertTrue(3 <= len(lane.sources) <= 6, lane.name)
            self.assertTrue(any(s["primary"] for s in lane.sources), lane.name)
            for s in lane.sources:
                self.assertTrue(s["url"].startswith("https://"), s)
        self.assertIn("not checked live", text.split("## ")[0])
        self.assertIn("run log", text.split("## ")[0])
        for dash in ("\u2014", "\u2013"):
            self.assertNotIn(dash, text)

    def test_repository_lanes_know_the_model_names(self):
        """A headline that names models by their short names is on the
        Frontier models lane, not off lane."""
        item = feed_item("Fable 5.2 vs Opus 5.5 and When to Use Each", "@AIDailyBrief", 3,
                         "https://www.youtube.com/watch?v=fable52")
        doc = build([item], lane_items=[], lanes_path=learn.LANES_MD)
        topic = doc["topics"][0]
        self.assertFalse(topic["off_lane"])
        self.assertEqual(topic["lanes"][0], "Frontier models")
        self.assertGreaterEqual(topic["signals"]["lane_hits"], 2)
        self.assertIn("Frontier models (opus, fable)", " ".join(topic["explanation"]))
        with open(learn.LANES_MD, encoding="utf-8") as f:
            lanes = {lane.name: lane.keywords for lane in learn.parse_lanes(f.read())[0]}
        for kw in ("claude", "opus", "sonnet", "haiku", "fable", "mythos", "gpt", "gemini", "llama", "mistral",
                   "grok", "deepseek"):
            self.assertIn(kw, lanes["Frontier models"])
        # A finance headline stays off lane with the wider keyword lists.
        tax = feed_item("IRS Announces 2027 Tax Changes \u2014 What You Need to Know", "@clearvaluetax9382", 3,
                        "https://www.youtube.com/watch?v=tax2027", "New brackets and a larger standard deduction.")
        doc = build([tax], lane_items=[], lanes_path=learn.LANES_MD)
        self.assertTrue(doc["topics"][0]["off_lane"])

    def test_format(self):
        text = "\n".join([
            "# Lanes",
            "Keywords: above the first lane, ignored",
            "## One",
            "Watch: the first lane",
            "Keywords: alpha, beta gamma ,  , alpha",
            "Sources:",
            "- Lab blog | https://lab.example.com/feed.xml | primary",
            "- Commentary | https://c.example.com/rss",
            "- No url",
            "- Plain http | http://insecure.example.com/feed",
            "- Odd flag | https://odd.example.com/feed | featured",
            "Something else",
            "## Two",
            "Watch: second",
            "## one",
            "Keywords: delta",
        ])
        lanes, problems = learn.parse_lanes(text)
        self.assertEqual([l.name for l in lanes], ["One", "Two"])
        one = lanes[0]
        self.assertEqual(one.watch, "the first lane")
        self.assertEqual(one.keywords, ["alpha", "beta gamma", "delta"])  # the repeated heading merged
        self.assertEqual([(s["label"], s["primary"]) for s in one.sources],
                         [("Lab blog", True), ("Commentary", False), ("Odd flag", False)])
        joined = "\n".join(problems)
        self.assertIn("line 9: a source needs", joined)
        self.assertIn("line 10: Plain http: the feed url must start with https://", joined)
        self.assertIn("line 11: Odd flag: only 'primary'", joined)
        self.assertIn("line 12: not a Watch:", joined)
        self.assertIn("line 15: lane 'one' appears twice", joined)
        self.assertIn("lane 'Two' has no keywords", joined)


# ---------------------------------------------------------------- terms

class TermTests(unittest.TestCase):
    def test_tokens(self):
        toks = learn.tokens("Anthropic\u2019s $1.4T IPO: GPT-5.5 vs Claude Opus 5.5 in late 2026, llama.cpp b6543 "
                            "https://example.com/x, issue 347")
        for t in ("anthropic", "1.4t", "ipo", "gpt5.5", "gpt", "5.5", "opus5.5", "llama.cpp", "b6543", "347"):
            self.assertIn(t, toks)
        for t in ("anthropics", "2026", "late2026", "vs", "in", "example.com", "issue347"):
            self.assertNotIn(t, toks)

    def test_terms_overlap(self):
        self.assertTrue(learn.terms_overlap({"gpt5.5", "context", "window", "openai"}, {"gpt5.5", "context", "window", "2m", "x"}))
        self.assertFalse(learn.terms_overlap({"gpt5.5", "context", "window", "openai"}, {"gpt5.5", "fed", "rates", "x"}))
        self.assertTrue(learn.terms_overlap({"fed", "rates", "powell"}, {"fed", "rates", "powell", "x", "y"}))
        self.assertFalse(learn.terms_overlap({"fed", "rates"}, {"fed", "rates", "powell"}))  # two terms are not enough

    def test_plain_punctuation(self):
        self.assertEqual(learn.plain_punctuation("Fast \u2014 and cheap"), "Fast, and cheap")
        self.assertEqual(learn.plain_punctuation("pages 3\u20135"), "pages 3 to 5")
        self.assertEqual(learn.plain_punctuation("\u2014 lead"), "lead")
        self.assertEqual(learn.plain_punctuation("end \u2014."), "end.")

    def test_is_https_rejects_control_characters(self):
        self.assertTrue(learn.is_https("https://a.example.com/p?q=1"))
        for bad in ("https://a.example.com/p\x01q", "https://a.example.com/p\x00", "https://a.example.com/\x7fp",
                    "https://a.example.com/p\x85q", "https://a.example.com/p\ufffeq", "https://a.example.com/p q",
                    "https://a.example.com/p\tq", "http://a.example.com/p", "https://user@a.example.com/p"):
            self.assertFalse(learn.is_https(bad), repr(bad))

    def test_feed_link_with_a_control_character_is_left_out(self):
        items = story_items() + [feed_item("GPT-5.5 context window: a bad link", "Newsletter C", 2,
                                           "https://c.example.com/p\x01q", "GPT-5.5 and its 2M context window.")]
        doc = build(items)
        urls = [i["url"] for t in doc["topics"] for i in t["items"]] + [u for t in doc["topics"] for u in t["urls"]]
        self.assertFalse([u for u in urls if "c.example.com" in u])

    def test_feed_titles_lose_their_dashes(self):
        item = candidates.make_item("IRS Announces 2027 Tax Changes \u2014 What You Need to Know", "https://t.example.com/a",
                                    "@clearvaluetax9382", NOW, "Pages 3\u20135.")
        self.assertEqual(item["title"], "IRS Announces 2027 Tax Changes, What You Need to Know")
        doc = build([feed_item("IRS Announces 2027 Tax Changes \u2014 What You Need to Know", "@clearvaluetax9382", 3,
                               "https://www.youtube.com/watch?v=tax2027")], lane_items=[])
        self.assertEqual(doc["topics"][0]["title"], "IRS Announces 2027 Tax Changes, What You Need to Know")
        self.assertEqual(doc["topics"][0]["items"][0]["title"], doc["topics"][0]["title"])


# ---------------------------------------------------------------- clustering

class ClusteringTests(unittest.TestCase):
    def setUp(self):
        self.doc = build(story_items())

    def test_same_story_clusters_across_sources(self):
        gpt = topic_titled(self.doc, ["GPT-5.5"])
        self.assertIsNotNone(gpt)
        self.assertEqual(sorted(gpt["sources"]), ["Last Week in AI", "Newsletter A", "Newsletter B", "OpenAI news"])
        self.assertEqual(gpt["title"], "Introducing GPT-5.5")  # the primary item names the topic
        self.assertTrue(gpt["items"][0]["primary"])
        # The lane entry's tracking parameter is gone from the stored link.
        self.assertIn("https://openai.example.com/index/introducing-gpt-5-5?utm_source=rss",
                      [i["url"] for i in gpt["items"]])

    def test_roundup_does_not_chain_two_stories(self):
        gpt = topic_titled(self.doc, ["GPT-5.5"])
        ollama = topic_titled(self.doc, ["v0.20.0"])
        self.assertIsNotNone(ollama)
        self.assertNotEqual(gpt["id"], ollama["id"])
        self.assertEqual(ollama["sources"], ["Ollama releases"])

    def test_release_title_gets_its_source_and_https(self):
        ollama = topic_titled(self.doc, ["v0.20.0"])
        self.assertEqual(ollama["items"][0]["title"], "Ollama releases: v0.20.0")
        self.assertTrue(ollama["items"][0]["url"].startswith("https://github.com/"))

    def test_boilerplate_does_not_join_one_channels_videos(self):
        items = story_items()
        pool, _ = candidates.pool_from_items({"items": items}, NOW, NOW - timedelta(hours=36), set())
        boilerplate = candidates.boilerplate_by_source({"items": items})
        candidates.add_terms(pool, boilerplate)
        distinctive, _ = candidates.distinctive_terms(candidates.background_terms({"items": items}, [], boilerplate))
        videos = [i for i in pool if i["source"] == "@AIDailyBrief"]
        self.assertEqual(len(videos), 2)
        self.assertNotIn("patreon", videos[0]["terms"])
        self.assertEqual(candidates.link_strength(videos[0], videos[1], distinctive), 0)
        groups = candidates.cluster_items(pool, distinctive)
        video_idx = [pool.index(v) for v in videos]
        self.assertFalse(any(set(video_idx) <= set(g) for g in groups))

    def test_window(self):
        titles = [i["title"] for t in self.doc["topics"] for i in t["items"]]
        self.assertNotIn("A 2025 post about GPT-4", titles)
        self.assertNotIn("Insecure link", titles)
        self.assertNotIn("An older post about safety evaluations", titles)

    def test_deterministic(self):
        again = build(story_items())
        self.assertEqual(json.dumps(self.doc, sort_keys=True), json.dumps(again, sort_keys=True))

    def test_link_key(self):
        k = candidates.link_key
        self.assertEqual(k("https://www.Example.com/a/?utm_source=x&b=2&a=1#frag"), "https://example.com/a?a=1&b=2")
        self.assertNotEqual(k("https://www.youtube.com/watch?v=one"), k("https://www.youtube.com/watch?v=two"))


# ---------------------------------------------------------------- scoring

class ScoringTests(unittest.TestCase):
    def test_weights_table(self):
        self.assertEqual(candidates.WEIGHTS, {
            "sources": {"weight": 2.0, "cap": 3},
            "primary": {"weight": 1.5},
            "new_since_last_week": {"weight": 1.0},
            "lane_hits": {"weight": 0.4, "cap": 3},
        })
        self.assertEqual(candidates.THRESHOLD, 3.0)

    def test_score_arithmetic_and_caps(self):
        score, why = candidates.score_signals(4, True, True, 4)
        self.assertEqual(score, 6.0 + 1.5 + 1.0 + 1.2)
        self.assertEqual(why[0], "4 sources: +6.0")
        self.assertEqual(candidates.score_signals(9, True, True, 20)[0], 9.7)  # every cap
        self.assertEqual(candidates.score_signals(1, False, False, 0)[0], 0.0)
        self.assertEqual(candidates.score_signals(2, False, True, 1)[0], 3.4)

    def test_signals_in_candidates(self):
        doc = build(story_items())
        gpt = topic_titled(doc, ["GPT-5.5"])
        self.assertEqual(gpt["signals"]["sources"], 4)
        self.assertTrue(gpt["signals"]["primary"])
        self.assertTrue(gpt["signals"]["new_since_last_week"])
        self.assertGreaterEqual(gpt["signals"]["lane_hits"], 3)
        self.assertEqual(gpt["score"], 9.7)
        self.assertEqual(gpt["lanes"][0], "Frontier models")
        fed = topic_titled(doc, ["Fed"])
        self.assertTrue(fed["off_lane"])
        self.assertEqual(fed["lanes"], [])
        self.assertEqual(fed["score"], 3.0)
        # Ranked by score, best first.
        scores = [t["score"] for t in doc["topics"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_candidates_shape(self):
        doc = build(story_items())
        self.assertEqual(doc["date"], "2026-11-11")
        self.assertTrue(3 <= len(doc["topics"]) <= 5)
        for t in doc["topics"]:
            self.assertEqual(set(t), {"id", "title", "lanes", "off_lane", "score", "signals", "explanation",
                                      "follow_up", "terms", "sources", "urls", "items"})
            self.assertEqual(set(t["signals"]), {"sources", "primary", "new_since_last_week", "lane_hits", "score"})
            self.assertLessEqual(len(t["items"]), candidates.ITEMS_PER_TOPIC)
            for i in t["items"]:
                self.assertTrue(i["url"].startswith("https://"))
                self.assertLessEqual(len(i["summary"]), candidates.SUMMARY_CHARS)


def fake_topic(tid, score, off_lane=False, sources=1):
    return {"id": tid, "score": score, "off_lane": off_lane,
            "signals": {"sources": sources, "primary": False}}


class SelectionTests(unittest.TestCase):
    def test_threshold_max_and_min(self):
        topics = [fake_topic(f"t{n}", s) for n, s in enumerate([8, 7, 6, 5, 4.5, 4, 3.5])]
        self.assertEqual([t["id"] for t in candidates.select_topics(topics, 3, 5)], ["t0", "t1", "t2", "t3", "t4"])
        low = [fake_topic("a", 2.0), fake_topic("b", 1.5), fake_topic("c", 1.0), fake_topic("d", 0.5)]
        self.assertEqual([t["id"] for t in candidates.select_topics(low, 3, 5)], ["a", "b", "c"])  # filled to min
        self.assertEqual(candidates.select_topics([], 3, 5), [])

    def test_off_lane_always_included(self):
        topics = [fake_topic("a", 9), fake_topic("b", 8), fake_topic("c", 7), fake_topic("off", 1.0, off_lane=True)]
        self.assertEqual([t["id"] for t in candidates.select_topics(topics, 3, 5)], ["a", "b", "c", "off"])
        # A full list gives up its lowest pick for the best off-lane topic.
        self.assertEqual([t["id"] for t in candidates.select_topics(topics, 2, 3)], ["a", "b", "off"])
        two_off = topics + [fake_topic("off2", 2.0, off_lane=True)]
        self.assertEqual([t["id"] for t in candidates.select_topics(two_off, 3, 3)], ["a", "b", "off2"])
        # No off-lane topic at all: nothing to add.
        self.assertEqual(len(candidates.select_topics(topics[:3], 3, 5)), 3)

    def test_off_lane_is_never_the_only_topic_when_an_on_lane_one_exists(self):
        pick = lambda topics, lo, hi: [t["id"] for t in candidates.select_topics(topics, lo, hi)]  # noqa: E731
        # Only the off-lane topic passes the threshold and one topic is enough: the best on-lane one joins it.
        topics = [fake_topic("off", 3.0, off_lane=True), fake_topic("on", 1.8), fake_topic("on2", 1.0)]
        self.assertEqual(pick(topics, 1, 5), ["off", "on"])
        # Room for one topic only: it is the on-lane one.
        self.assertEqual(pick(topics, 1, 1), ["on"])
        self.assertEqual(pick([fake_topic("a", 9), fake_topic("off", 1.0, off_lane=True)], 1, 1), ["a"])
        # Nothing on lane at all: the off-lane topic stands alone.
        self.assertEqual(pick([fake_topic("off", 1.0, off_lane=True)], 3, 5), ["off"])

    def test_off_lane_in_end_to_end(self):
        doc = build(story_items(), topics_min=1, topics_max=2)
        self.assertEqual(len(doc["topics"]), 2)
        self.assertTrue(doc["topics"][-1]["off_lane"])


# ---------------------------------------------------------------- novelty

def seen_entry(doc_topic, logged_hours_ago, sources=None, urls=None, day=None):
    """A seen.json entry logged that many hours ago. Its date is an earlier
    day's unless day is given: entries dated today are ignored (a re-run)."""
    return {
        "id": doc_topic["id"], "title": doc_topic["title"],
        "date": day or min((NOW - timedelta(hours=logged_hours_ago)).date().isoformat(), "2026-11-10"),
        "logged_at": ago(logged_hours_ago),
        "terms": list(doc_topic["terms"]),
        "urls": list(doc_topic["urls"] if urls is None else urls),
        "sources": list(doc_topic["sources"] if sources is None else sources),
        "primary": True,
    }


class NoveltyTests(unittest.TestCase):
    def setUp(self):
        self.first = build(story_items())
        self.gpt = topic_titled(self.first, ["GPT-5.5"])

    def test_seen_topic_is_skipped(self):
        seen = [seen_entry(self.gpt, 0.5)]  # logged after every item arrived
        doc = build(story_items(), seen=seen)
        self.assertIsNone(topic_titled(doc, ["GPT-5.5"]))
        self.assertEqual(doc["seen_skipped"][0]["seen_id"], self.gpt["id"])
        self.assertEqual(doc["stats"]["skipped_as_seen"], 1)

    def test_new_source_after_logging_is_a_new_development(self):
        known = [s for s in self.gpt["sources"] if s != "Last Week in AI"]
        seen = [seen_entry(self.gpt, 1.5, sources=known)]  # the roundup arrived 1 hour ago, after this
        doc = build(story_items(), seen=seen)
        gpt = topic_titled(doc, ["GPT-5.5"])
        self.assertIsNotNone(gpt)
        self.assertEqual(gpt["follow_up"]["id"], self.gpt["id"])

    def test_new_primary_link_from_a_known_source_is_a_new_development(self):
        urls = [u for u in self.gpt["urls"] if "openai.example.com" not in u]
        seen = [seen_entry(self.gpt, 5, urls=urls)]  # logged at 08:45, before the 09:00 release post
        doc = build(story_items(), seen=seen)
        self.assertIsNotNone(topic_titled(doc, ["GPT-5.5"]))

    def test_follow_up_from_a_known_source_is_not_new(self):
        seen = [seen_entry(self.gpt, 2.5)]  # two posts arrived later, from sources the topic had
        doc = build(story_items(), seen=seen)
        self.assertIsNone(topic_titled(doc, ["GPT-5.5"]))

    def test_same_day_rerun_picks_the_same_topics(self):
        """A second morning run on the same day ignores what the first one
        logged, so it does not brief the leftovers."""
        logged_today = [seen_entry(t, 0.5, day=self.first["date"]) for t in self.first["topics"]]
        rerun = build(story_items(), seen=logged_today)
        self.assertEqual([t["id"] for t in rerun["topics"]], [t["id"] for t in self.first["topics"]])
        self.assertEqual(rerun["seen_skipped"], [])
        # The same entries under yesterday's date do count as seen.
        logged_yesterday = [seen_entry(t, 0.5) for t in self.first["topics"]]
        next_day = build(story_items(), seen=logged_yesterday)
        self.assertNotEqual([t["id"] for t in next_day["topics"]], [t["id"] for t in self.first["topics"]])
        self.assertEqual({s["seen_id"] for s in next_day["seen_skipped"]}, {t["id"] for t in self.first["topics"]})

    def test_updated_seen_replaces_the_days_entries(self):
        first = [{"id": "a", "title": "A", "terms": ["x"], "urls": ["https://a.example.com/1"], "sources": ["S"]},
                 {"id": "b", "title": "B", "terms": ["y"], "urls": [], "sources": []}]
        yesterday = learn.updated_seen([], [{"id": "old", "title": "Old"}], "2026-11-10", NOW - timedelta(days=1))
        entries = learn.updated_seen(yesterday, first, "2026-11-11", NOW)
        self.assertEqual([(e["id"], e["date"]) for e in entries],
                         [("old", "2026-11-10"), ("a", "2026-11-11"), ("b", "2026-11-11")])
        # The re-run briefs a different set: only that set stays logged for the day.
        rerun = learn.updated_seen(entries, [{"id": "b", "title": "B"}, {"id": "c", "title": "C"}], "2026-11-11",
                                   NOW + timedelta(minutes=40))
        self.assertEqual([(e["id"], e["date"]) for e in rerun],
                         [("old", "2026-11-10"), ("b", "2026-11-11"), ("c", "2026-11-11")])
        self.assertEqual(learn.seen_before(rerun, "2026-11-11"), yesterday)

    def test_topic_logged_before_the_gate_can_come_back(self):
        seen = [seen_entry(self.gpt, 24 * (learn.SEEN_GATE_DAYS + 1))]
        doc = build(story_items(), seen=seen)
        gpt = topic_titled(doc, ["GPT-5.5"])
        self.assertIsNotNone(gpt)
        self.assertIsNone(gpt["follow_up"])

    def test_old_items_mean_not_new_since_last_week(self):
        items = story_items() + [
            feed_item("GPT-5.5 rumors: OpenAI tests a 2M context window", "Newsletter A", 24 * 10,
                      "https://a.example.com/p/rumors", "Rumors say GPT-5.5 will have a 2M context window."),
        ]
        doc = build(items)
        gpt = topic_titled(doc, ["Introducing"])
        self.assertFalse(gpt["signals"]["new_since_last_week"])
        self.assertIn("already around last week: +0.0", gpt["explanation"])

    def test_topic_logged_over_a_week_ago_is_not_new_since_last_week(self):
        known = [s for s in self.gpt["sources"] if s != "Last Week in AI"]
        seen = [seen_entry(self.gpt, 24 * 9, sources=known)]
        doc = build(story_items(), seen=seen)
        gpt = topic_titled(doc, ["GPT-5.5"])
        self.assertIsNotNone(gpt)  # a new source since then
        self.assertFalse(gpt["signals"]["new_since_last_week"])


# ---------------------------------------------------------------- candidates.py main

class CandidatesMainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.items = os.path.join(self.tmp, "items.json")
        with open(self.items, "w", encoding="utf-8") as f:
            json.dump({"items": story_items()}, f)
        self.out = os.path.join(self.tmp, "cand.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_main(self, extra=()):
        argv = ["--out", self.out, "--items", self.items, "--lanes", fixture_path("lanes.md"),
                "--seen", os.path.join(self.tmp, "seen.json"), "--now", NOW.isoformat(), *extra]
        with mock.patch.object(fab, "RETRY_DELAY", 0), \
                mock.patch.object(learn, "load_settings", return_value=settings()):
            return quiet(candidates.main, argv, session=FakeSession(lane_routes()))

    def test_failing_lane_source_is_logged_and_the_run_goes_on(self):
        (doc, log) = self.run_main()
        self.assertIn("lane source Broken feed (https://broken.example.com/feed) failed", log)
        self.assertEqual(doc["stats"]["lane_sources_failed"], [{"label": "Broken feed", "error": "connection error"}])
        self.assertEqual(doc["stats"]["lane_sources_ok"], 2)
        with open(self.out, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["topics"], doc["topics"])
        self.assertIn("Wrote", log)

    def test_sparse_day_is_said_in_the_log(self):
        (doc, log) = self.run_main()
        self.assertFalse(doc["stats"]["below_topics_min"])
        self.assertNotIn("Sparse day", log)
        with open(self.items, "w", encoding="utf-8") as f:
            json.dump({"items": story_items()[3:5]}, f)  # the Fed story only
        (doc, log) = self.run_main(["--no-fetch"])
        self.assertEqual(len(doc["topics"]), 1)
        self.assertTrue(doc["stats"]["below_topics_min"])
        self.assertIn("Sparse day: 1 topic, fewer than topics_min (3). Clusters not skipped as seen: 1 of 1. "
                      "The briefing is written with what there is.", log)

    def test_no_fetch(self):
        (doc, _) = self.run_main(["--no-fetch"])
        self.assertEqual(doc["stats"]["lane_items"], 0)
        gpt = topic_titled(doc, ["GPT-5.5"])
        self.assertFalse(gpt["signals"]["primary"])


# ---------------------------------------------------------------- settings

class SettingsTests(unittest.TestCase):
    def test_clamps_and_defaults(self):
        s, log = quiet(learn.clean_learn_settings, {"model": "bad model!", "effort": "huge", "max_output_tokens": 10**9,
                                                    "topics_min": 9, "topics_max": 4, "price_per_mtok": {"input": "x"},
                                                    "fallbacks": "none"})
        self.assertIn("learn.model 'bad model!' is not a model id", log)
        self.assertEqual(s["model"], "claude-opus-5-5")
        self.assertEqual(s["effort"], "medium")
        self.assertEqual(s["max_output_tokens"], 64000)
        self.assertEqual((s["topics_min"], s["topics_max"]), (4, 4))
        self.assertEqual(s["price_per_mtok"], {"input": 4.0, "output": 20.0})
        self.assertIsNone(s["fallbacks"])
        self.assertEqual(learn.clean_learn_settings({"fallbacks": ["claude-opus-5", "claude-opus-5"]})["fallbacks"],
                         ["claude-opus-5"])
        self.assertEqual(learn.clean_learn_settings({})["fallbacks"], "default")

    def test_prices_by_model(self):
        self.assertEqual(learn.clean_learn_settings({})["prices"], learn.LEARN_DEFAULTS["prices"])
        s, log = quiet(learn.clean_learn_settings, {"prices": {
            "claude-opus-5": {"input": 5, "output": "25", "cache_read": 0.5},
            "claude-haiku-5-5": {"input": 0.1},          # no output price
            "not a model": {"input": 1, "output": 2},
            "claude-sonnet-5-5": "cheap",
        }})
        self.assertEqual(s["prices"], {"claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5}})
        self.assertEqual(log.count("left out"), 3)
        self.assertEqual(learn.clean_learn_settings({"prices": {}})["prices"], {})  # price_per_mtok for every model
        s, log = quiet(learn.clean_learn_settings, {"prices": ["x"]})
        self.assertEqual(s["prices"], learn.LEARN_DEFAULTS["prices"])
        self.assertIn("learn.prices", log)

    def test_repository_config_has_the_fallback_prices(self):
        s, _ = quiet(learn.load_settings)
        self.assertEqual(s["learn"]["prices"]["claude-opus-5-5"], {"input": 4.0, "output": 20.0})
        for model in ("claude-opus-5", "claude-opus-4-8"):
            self.assertEqual(s["learn"]["prices"][model], {"input": 5.0, "output": 25.0})

    def test_unreadable_config_falls_back_to_defaults(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-dir-for-test", "config.yml")
        s, log = quiet(learn.load_settings, missing)
        self.assertEqual(s["learn"], learn.clean_learn_settings({}))
        self.assertEqual(s["timezone"], "America/Los_Angeles")
        self.assertIn("using built-in learn settings", log)


# ---------------------------------------------------------------- brief.py

WORD = "word"


def model_topic(cand, words=520, urls=None, why="Read this first because it is the source."):
    """An answer topic of about `words` words."""
    body = " ".join([WORD] * (words - 60))
    half = len(body) // 2
    return {
        "id": cand["id"],
        "title": f"{cand['title']} \u2014 explained",
        "what_happened": body[:half] + ".\n\nSecond paragraph.",
        "why_it_matters": body[half:] + ".",
        "read_first": [{"url": u, "why": why} for u in (urls if urls is not None else [cand["items"][0]["url"]])],
        "open_question": "What changes next week?",
    }


def answer_for(cands, **kwargs):
    return {"email_summary": "Three topics today \u2014 models, local runs and the Fed.",
            "topics": [model_topic(c, **kwargs) for c in cands["topics"]]}


def message(answer=None, stop_reason="end_turn", usage=None, model="claude-opus-5-5", stop_details=None, blocks=None):
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=stop_details,
        model=model,
        content=blocks if blocks is not None else [SimpleNamespace(type="thinking", thinking=""),
                                                   SimpleNamespace(type="text", text=text)],
        usage=usage or SimpleNamespace(input_tokens=10000, output_tokens=5000, cache_read_input_tokens=0,
                                       cache_creation_input_tokens=0, cache_creation=None, iterations=None),
        _request_id="req_test",
    )


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    """Answers client.beta.messages.stream(**request) with a message, or
    raises the given exception. Records each request."""

    def __init__(self, outcome):
        self.requests = []

        def stream(**request):
            self.requests.append(request)
            if isinstance(outcome, BaseException):
                raise outcome
            return FakeStream(outcome)

        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=stream))


class BriefBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "briefing.json")
        self.cands = brief.load_candidates(fixture_path("candidates.json"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_brief(self, client=None, env=None, **learn_overrides):
        with mock.patch.object(learn, "load_settings", return_value=settings(**learn_overrides)):
            code, log = quiet(brief.main, ["--candidates", fixture_path("candidates.json"), "--out", self.out],
                              client=client, now=NOW, environ=env if env is not None else {"ANTHROPIC_API_KEY": "sk-test"})
        self.assertEqual(code, 0)
        with open(self.out, encoding="utf-8") as f:
            return json.load(f), log


class BriefWithoutSdkTests(BriefBase):
    def test_load_candidates_checks_the_input(self):
        fed = self.cands["topics"][2]
        self.assertEqual([i["url"] for i in fed["items"]],
                         ["https://macro.example.com/fed-holds", "https://www.youtube.com/watch?v=fed123"])
        self.assertEqual(self.cands["date"], "2026-11-11")

    def test_missing_key_writes_an_extractive_briefing(self):
        client = FakeClient(AssertionError("must not be called"))
        doc, log = self.run_brief(env={})
        self.assertIn("ANTHROPIC_API_KEY is not set", log)
        self.assertIn("Spend cap: 12000 output tokens per attempt, at most 24000 (about $0.54) when a refusal fallback "
                      "runs. Actual cost: $0.0000 (no model call).", log)
        self.assertFalse(doc["llm"])
        self.assertEqual(doc["model"], "extractive")
        self.assertEqual(doc["usage"], {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        self.assertEqual(client.requests, [])
        self.check_contract(doc)
        self.assertEqual([t["id"] for t in doc["topics"]], [t["id"] for t in self.cands["topics"]])
        gpt = doc["topics"][0]
        self.assertIn("4 sources covered this story, including a primary source.", gpt["why_it_matters"])
        self.assertIn("built without a model", gpt["why_it_matters"])
        self.assertNotIn("\u2014", gpt["what_happened"])  # the feed's em dash is gone
        self.assertEqual(gpt["read_first"][0]["why"], "Primary source.")
        self.assertIn("outside your lanes", doc["topics"][2]["why_it_matters"])
        self.assertTrue(doc["email_summary"].startswith("Today's briefing covers 3 topics:"))

    def check_contract(self, doc):
        self.assertEqual(set(doc), {"date", "generated_at", "model", "llm", "usage", "email_summary", "topics"})
        self.assertEqual(doc["date"], "2026-11-11")
        self.assertEqual(set(doc["usage"]), {"input_tokens", "output_tokens", "cost_usd"})
        for t in doc["topics"]:
            self.assertEqual(set(t), {"id", "title", "lanes", "off_lane", "what_happened", "why_it_matters",
                                      "read_first", "open_question", "reading_minutes", "signals"})
            self.assertEqual(set(t["signals"]), {"sources", "primary", "new_since_last_week", "lane_hits", "score"})
            self.assertIsInstance(t["reading_minutes"], int)
            for r in t["read_first"]:
                self.assertEqual(set(r), {"title", "url", "source", "why"})
                self.assertTrue(r["url"].startswith("https://"))

    def test_request_shape(self):
        cfg = settings()["learn"]
        text, topics = brief.fit_input(self.cands, cfg["max_input_chars"])
        req = brief.build_request(cfg, text, topics)
        self.assertEqual(req["model"], "claude-opus-5-5")
        self.assertEqual(req["max_tokens"], 12000)
        self.assertEqual(req["thinking"], {"type": "adaptive"})
        self.assertEqual(req["output_config"]["effort"], "medium")
        self.assertEqual(req["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(req["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(req["fallbacks"], "default")
        self.assertEqual(req["system"], brief.SYSTEM_PROMPT)
        self.assertNotIn("\u2014", brief.SYSTEM_PROMPT)
        content = req["messages"][0]["content"]
        self.assertIn("<candidates>", content)
        self.assertIn("introducing-gpt-5-5", content)

        def closed(schema):
            if schema.get("type") == "object":
                self.assertIs(schema["additionalProperties"], False)
                self.assertEqual(sorted(schema["required"]), sorted(schema["properties"]))
                for sub in schema["properties"].values():
                    closed(sub)
            if schema.get("type") == "array":
                closed(schema["items"])

        schema = req["output_config"]["format"]["schema"]
        closed(schema)
        topic_schema = schema["properties"]["topics"]["items"]
        self.assertEqual(topic_schema["properties"]["id"]["enum"], [t["id"] for t in self.cands["topics"]])
        self.assertIn("https://a.example.com/p/gpt-5-5",
                      topic_schema["properties"]["read_first"]["items"]["properties"]["url"]["enum"])

    def test_fallbacks_setting(self):
        off = brief.build_request(settings(fallbacks="none")["learn"], "x", self.cands["topics"])
        self.assertNotIn("betas", off)
        self.assertNotIn("fallbacks", off)
        listed = brief.build_request(settings(fallbacks=["claude-opus-5"])["learn"], "x", self.cands["topics"])
        self.assertEqual(listed["fallbacks"], [{"model": "claude-opus-5"}])

    def test_input_is_capped(self):
        full, topics = brief.fit_input(self.cands, 400000)
        self.assertEqual(len(topics), 3)
        text, topics = quiet(brief.fit_input, self.cands, 2000)[0]
        self.assertLessEqual(len(text), 2000)
        self.assertLess(len(text), len(full))
        tiny = dict(self.cands, topics=[dict(t, items=t["items"] * 4) for t in self.cands["topics"]] * 3)
        (text, topics), log = quiet(brief.fit_input, tiny, 2000)
        self.assertLessEqual(len(text), 2000)
        self.assertGreaterEqual(len(topics), 1)

    def test_cost_math(self):
        cfg = settings()["learn"]  # claude-opus-5-5 at $4/$20; claude-opus-5 and claude-opus-4-8 at $5/$25
        plain = SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=None,
                                cache_creation_input_tokens=None, cache_creation=None, iterations=None)
        record, text = brief.usage_record(plain, cfg)
        self.assertEqual(record, {"input_tokens": 1_000_000, "output_tokens": 100_000, "cost_usd": 6.0})
        self.assertIn("cost_usd=6.0000 (claude-opus-5-5 at $4.00/$20.00 per million tokens)", text)
        self.assertNotIn("estimate", text)
        self.assertEqual(brief.usage_record(None, cfg)[0], {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})

    def test_cache_prices(self):
        cfg = settings()["learn"]
        cached = SimpleNamespace(input_tokens=100_000, output_tokens=0, cache_read_input_tokens=1_000_000,
                                 cache_creation_input_tokens=300_000,
                                 cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=200_000, ephemeral_1h_input_tokens=100_000),
                                 iterations=None)
        # Opus 5.5 reads its cache at $0.20 per million tokens, 0.05 x the input price:
        # 0.1M x 4 + 1M x 0.20 + 0.2M x 5 + 0.1M x 8 = 0.4 + 0.2 + 1.0 + 0.8
        self.assertEqual(brief.prices_for("claude-opus-5-5", cfg)[0]["cache_read"], 0.2)
        self.assertEqual(brief.usage_record(cached, cfg)[0]["cost_usd"], 2.4)
        # Opus 5 reads at 0.1 x its $5 input price: 0.1M x 5 + 1M x 0.50 + 0.2M x 6.25 + 0.1M x 10
        self.assertEqual(brief.prices_for("claude-opus-5", cfg)[0]["cache_read"], 0.5)
        self.assertEqual(brief.usage_record(cached, cfg, served_by="claude-opus-5")[0]["cost_usd"], 3.25)
        # A cache_read price in learn.prices wins.
        own = settings(prices={"claude-opus-5-5": {"input": 4, "output": 20, "cache_read": 1.0}})["learn"]
        self.assertEqual(brief.usage_record(cached, own)[0]["cost_usd"], 3.2)

    def test_each_iteration_is_priced_at_its_models_rates(self):
        cfg = settings()["learn"]

        def iteration(kind, model, tokens_in, tokens_out):
            return SimpleNamespace(type=kind, model=model, input_tokens=tokens_in, output_tokens=tokens_out,
                                   cache_read_input_tokens=0, cache_creation_input_tokens=0, cache_creation=None)

        # The requested model declined part way (its output is billed), then a fallback model answered.
        with_fallback = SimpleNamespace(
            input_tokens=5, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0, cache_creation=None,
            iterations=[iteration("message", "claude-opus-5-5", 500_000, 10_000),
                        iteration("fallback_message", "claude-opus-5", 500_000, 50_000)])
        record, text = brief.usage_record(with_fallback, cfg, served_by="claude-opus-5")
        # 0.5M x 4 + 10K x 20 = 2.20, and 0.5M x 5 + 50K x 25 = 3.75: iterations, not the top-level usage
        self.assertEqual(record, {"input_tokens": 1_000_000, "output_tokens": 60_000, "cost_usd": 5.95})
        self.assertIn("claude-opus-5-5 at $4.00/$20.00 per million tokens: $2.2000", text)
        self.assertIn("claude-opus-5 at $5.00/$25.00 per million tokens: $3.7500", text)
        self.assertNotIn("estimate", text)
        # An iteration with no model is the requested model's; a dated id uses its base id's price.
        odd = SimpleNamespace(iterations=[iteration("message", None, 1_000_000, 0),
                                          iteration("fallback_message", "claude-opus-4-8-20260115", 1_000_000, 0)])
        self.assertEqual(brief.usage_record(odd, cfg)[0], {"input_tokens": 2_000_000, "output_tokens": 0, "cost_usd": 9.0})

    def test_unknown_model_is_priced_as_an_estimate(self):
        cfg = settings()["learn"]
        usage = SimpleNamespace(iterations=[SimpleNamespace(
            type="fallback_message", model="claude-next-9", input_tokens=1_000_000, output_tokens=100_000,
            cache_read_input_tokens=0, cache_creation_input_tokens=0, cache_creation=None)])
        record, text = brief.usage_record(usage, cfg, served_by="claude-next-9")
        self.assertEqual(record, {"input_tokens": 1_000_000, "output_tokens": 100_000, "cost_usd": 6.0,
                                  "cost_estimated": True})
        self.assertIn("an estimate: learn.prices in config.yml has no entry for claude-next-9", text)
        # The configured model is never an estimate: price_per_mtok is its price when learn.prices leaves it out.
        bare = settings(model="claude-sonnet-5-5", price_per_mtok={"input": 2, "output": 10}, prices={})["learn"]
        plain = SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000, iterations=None)
        self.assertEqual(brief.usage_record(plain, bare)[0], {"input_tokens": 1_000_000, "output_tokens": 100_000, "cost_usd": 3.0})

    def test_spend_cap(self):
        # One attempt is capped at max_output_tokens; a refusal fallback can at worst double the output.
        self.assertEqual(brief.spend_cap(settings()["learn"]), (24000, 0.54))            # 12000 x $20 + 12000 x $25
        self.assertEqual(brief.spend_cap(settings(fallbacks="none")["learn"]), (12000, 0.24))
        listed = settings(fallbacks=["claude-opus-5", "claude-opus-4-8"], max_output_tokens=10000)["learn"]
        self.assertEqual(brief.spend_cap(listed), (30000, 0.7))
        line = brief.spend_line(settings()["learn"], {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}, False)
        self.assertEqual(line, "Spend cap: 12000 output tokens per attempt, at most 24000 (about $0.54) when a refusal "
                               "fallback runs. Actual cost: $0.0000 (no model call).")
        self.assertIn("at worst double the output spend", " ".join(brief.__doc__.split()))

    def test_titles_and_open_question_copy(self):
        """Feed titles reach the briefing without em or en dashes, and a lane
        name is written as it is in lanes.md."""
        with open(fixture_path("candidates.json"), encoding="utf-8") as f:
            doc = json.load(f)
        doc["topics"][0]["lanes"] = ["AI in marketing", "Frontier models"]
        doc["topics"][0]["title"] = "GPT-5.5 \u2014 what changed"
        doc["topics"][0]["items"][0]["title"] = "Introducing GPT-5.5 \u2013 the 2M window"
        path = os.path.join(self.tmp, "cands.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        cands = brief.load_candidates(path)
        self.assertEqual(cands["topics"][0]["title"], "GPT-5.5, what changed")
        self.assertEqual(cands["topics"][0]["items"][0]["title"], "Introducing GPT-5.5, the 2M window")
        topics, summary = brief.extractive_briefing_parts(cands["topics"])
        self.assertEqual(topics[0]["title"], "GPT-5.5, what changed")
        self.assertEqual(topics[0]["read_first"][0]["title"], "Introducing GPT-5.5, the 2M window")
        self.assertEqual(topics[0]["open_question"], "What does this change for your work on AI in marketing?")
        self.assertIn("It fits your AI in marketing and Frontier models lanes.", topics[0]["why_it_matters"])
        (topics, _), _ = quiet(brief.validate_output, answer_for(cands), cands["topics"])  # the model's answer
        self.assertEqual(topics[0]["read_first"][0]["title"], "Introducing GPT-5.5, the 2M window")
        for dash in ("\u2014", "\u2013"):
            self.assertNotIn(dash, json.dumps(topics, ensure_ascii=False) + summary)

    def test_sparse_day_is_said_in_the_log_and_not_in_the_email_summary(self):
        with open(fixture_path("candidates.json"), encoding="utf-8") as f:
            doc = json.load(f)
        doc["topics"] = doc["topics"][:1]
        path = os.path.join(self.tmp, "sparse.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        with mock.patch.object(learn, "load_settings", return_value=settings()):
            code, log = quiet(brief.main, ["--candidates", path, "--out", self.out], now=NOW, environ={})
        self.assertEqual(code, 0)
        self.assertIn("Sparse day: the briefing has 1 topic, fewer than topics_min (3)", log)
        with open(self.out, encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual(written["email_summary"], "Today's briefing covers 1 topic: Introducing GPT-5.5.")
        doc, log = self.run_brief(env={})  # three topics: nothing to say
        self.assertNotIn("Sparse day", log)

    def test_validate_output_rejects_links_that_are_not_the_topics_own(self):
        cands = self.cands
        foreign = cands["topics"][1]["items"][0]["url"]
        answer = answer_for(cands)
        answer["topics"][0]["read_first"] = [
            {"url": "https://evil.example.com/phish", "why": "Bad."},
            {"url": foreign, "why": "Another topic's link."},
            {"url": "javascript:alert(1)", "why": "Bad."},
            {"url": cands["topics"][0]["items"][1]["url"], "why": "Good."},
        ]
        answer["topics"][1]["read_first"] = [{"url": "https://evil.example.com/2", "why": "Bad."}]
        (topics, summary), log = quiet(brief.validate_output, answer, cands["topics"])
        self.assertEqual([r["url"] for r in topics[0]["read_first"]], [cands["topics"][0]["items"][1]["url"]])
        self.assertEqual(topics[0]["read_first"][0]["title"], cands["topics"][0]["items"][1]["title"])
        self.assertEqual(topics[0]["read_first"][0]["source"], "Newsletter A")
        self.assertEqual(log.count("rejected a read-first link"), 4)
        # Every link rejected: the topic falls back to its own first sources.
        self.assertEqual([r["url"] for r in topics[1]["read_first"]], [cands["topics"][1]["items"][0]["url"]])
        self.assertNotIn("\u2014", topics[0]["title"])
        self.assertNotIn("\u2014", summary)

    def test_validate_output_failures(self):
        cands = self.cands
        missing = answer_for(cands)
        missing["topics"].pop()
        with self.assertRaisesRegex(brief.InvalidOutput, "missing topics"):
            brief.validate_output(missing, cands["topics"])
        empty = answer_for(cands)
        empty["topics"][0]["why_it_matters"] = "  "
        with self.assertRaisesRegex(brief.InvalidOutput, "why_it_matters is empty"):
            brief.validate_output(empty, cands["topics"])
        short = {"email_summary": "x", "topics": [model_topic(c, words=90) for c in cands["topics"]]}
        with self.assertRaisesRegex(brief.InvalidOutput, "words, outside"):
            brief.validate_output(short, cands["topics"])
        with self.assertRaises(brief.InvalidOutput):
            brief.validate_output({"topics": "no"}, cands["topics"])
        long_ok = answer_for(cands)
        long_ok["topics"][0]["open_question"] = "Why? " * 400
        (topics, _), log = quiet(brief.validate_output, long_ok, cands["topics"])
        self.assertLessEqual(len(topics[0]["open_question"]), brief.FIELD_CHARS["open_question"])
        self.assertIn("open_question was", log)

    def test_word_targets(self):
        (topics, _), _ = quiet(brief.validate_output, answer_for(self.cands, words=560), self.cands["topics"])
        for t in topics:
            self.assertTrue(brief.WORDS_TARGET[0] <= brief.topic_words(t) <= brief.WORDS_TARGET[1])
            self.assertEqual(t["reading_minutes"], 3)


@unittest.skipUnless(HAS_ANTHROPIC, "needs the anthropic SDK (requirements-brief.lock)")
class BriefWithSdkTests(BriefBase):
    def test_success(self):
        client = FakeClient(message(answer_for(self.cands)))
        doc, log = self.run_brief(client)
        self.assertTrue(doc["llm"])
        self.assertEqual(doc["model"], "claude-opus-5-5")
        self.assertEqual(doc["usage"], {"input_tokens": 10000, "output_tokens": 5000, "cost_usd": 0.14})
        self.assertIn("Claude usage: model=claude-opus-5-5 stop_reason=end_turn input_tokens=10000", log)
        self.assertIn("cost_usd=0.1400", log)
        self.assertIn("Actual cost: $0.1400.", log)  # next to the cap, on every run
        self.assertIn("Spend cap: 12000 output tokens per attempt, at most 24000", log)
        self.assertIn("request_id=req_test", log)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual([t["id"] for t in doc["topics"]], [t["id"] for t in self.cands["topics"]])
        self.assertEqual(doc["topics"][2]["off_lane"], True)
        self.assertEqual(doc["topics"][0]["signals"]["score"], 9.7)
        self.assertEqual(doc["email_summary"], "Three topics today, models, local runs and the Fed.")
        self.assertNotIn("sk-test", json.dumps(doc) + log)

    def fallback_usage(self, model="claude-opus-5"):
        entry = dict(cache_read_input_tokens=0, cache_creation_input_tokens=0, cache_creation=None)
        return SimpleNamespace(input_tokens=1, output_tokens=1, **entry, iterations=[
            SimpleNamespace(type="message", model="claude-opus-5-5", input_tokens=10_000, output_tokens=2_000, **entry),
            SimpleNamespace(type="fallback_message", model=model, input_tokens=10_000, output_tokens=5_000, **entry)])

    def test_fallback_model_served(self):
        blocks = [SimpleNamespace(type="text", text='{"partial'),
                  SimpleNamespace(type="fallback", text=None),
                  SimpleNamespace(type="text", text=json.dumps(answer_for(self.cands)))]
        doc, log = self.run_brief(FakeClient(message(blocks=blocks, usage=self.fallback_usage(), model="claude-opus-5")))
        self.assertTrue(doc["llm"])
        self.assertEqual(doc["model"], "claude-opus-5")
        self.assertIn("the fallback model claude-opus-5 wrote the answer", log)
        # 10K x $4 + 2K x $20 for the attempt that declined, 10K x $5 + 5K x $25 for the fallback model
        self.assertEqual(doc["usage"], {"input_tokens": 20_000, "output_tokens": 7_000, "cost_usd": 0.255})
        self.assertIn("Actual cost: $0.2550.", log)
        self.assertNotIn("the answer was read from", log)

    def test_fallback_in_the_middle_of_the_answer(self):
        """The model declines part way through and the fallback model carries
        on from the partial text: the answer is both sides of the block."""
        text = json.dumps(answer_for(self.cands))
        cut = len(text) // 3
        blocks = [SimpleNamespace(type="thinking", thinking=""),
                  SimpleNamespace(type="text", text=text[:cut]),
                  SimpleNamespace(type="fallback", text=None),
                  SimpleNamespace(type="thinking", thinking=""),
                  SimpleNamespace(type="text", text=text[cut:])]
        doc, log = self.run_brief(FakeClient(message(blocks=blocks, usage=self.fallback_usage(), model="claude-opus-5")))
        self.assertTrue(doc["llm"])
        self.assertEqual(doc["model"], "claude-opus-5")
        self.assertEqual([t["id"] for t in doc["topics"]], [t["id"] for t in self.cands["topics"]])
        self.assertIn("Note: the answer was read from the text before and after the fallback block, joined", log)
        # Neither reading is JSON: the briefing falls back to the headlines and the log names both.
        blocks[-1] = SimpleNamespace(type="text", text="and then nothing useful")
        doc, log = self.run_brief(FakeClient(message(blocks=blocks, usage=self.fallback_usage(), model="claude-opus-5")))
        self.assertFalse(doc["llm"])
        self.assertIn("the text after the fallback block:", log)
        self.assertIn("the text before and after the fallback block, joined:", log)

    def test_unpriced_fallback_model_marks_the_cost_as_an_estimate(self):
        client = FakeClient(message(answer_for(self.cands), usage=self.fallback_usage("claude-next-9"), model="claude-next-9"))
        doc, log = self.run_brief(client)
        self.assertTrue(doc["llm"])
        # the fallback's tokens at price_per_mtok ($4/$20): 0.08 + 0.14
        self.assertEqual(doc["usage"], {"input_tokens": 20_000, "output_tokens": 7_000, "cost_usd": 0.22,
                                        "cost_estimated": True})
        self.assertIn("Actual cost: $0.2200 (an estimate).", log)

    def test_client_timeout_and_retries(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            client = brief.make_client()
        self.assertEqual(client.max_retries, 1)
        self.assertEqual(client.timeout, 240)
        self.assertLess(brief.CALL_BUDGET_SECONDS, 245)  # the workflow stops the step at 245 seconds

    def test_slow_call_falls_back_to_the_headlines(self):
        release = threading.Event()

        class SlowStream(FakeStream):
            def get_final_message(self):
                release.wait(10)
                return self.msg

        client = FakeClient(message(answer_for(self.cands)))
        client.beta.messages.stream = lambda **request: SlowStream(message(answer_for(self.cands)))
        try:
            with mock.patch.object(brief, "CALL_BUDGET_SECONDS", 0.05):
                doc, log = self.run_brief(client)
        finally:
            release.set()
        self.assertFalse(doc["llm"])
        self.assertEqual(len(doc["topics"]), 3)
        self.assertIn("Claude call stopped: no complete answer after 0.05 seconds; writing the briefing without a model", log)
        self.assertEqual(doc["usage"]["cost_usd"], 0.0)
        self.assertIn("Actual cost: $0.0000 recorded (the call did not return its usage).", log)

    def test_refusal(self):
        details = SimpleNamespace(type="refusal", category="cyber", explanation=None)
        doc, log = self.run_brief(FakeClient(message("", stop_reason="refusal", stop_details=details)))
        self.assertFalse(doc["llm"])
        self.assertIn("declined the request (refusal, category cyber)", log)
        self.assertEqual(doc["usage"]["output_tokens"], 5000)  # the attempt is still paid for
        self.assertEqual(len(doc["topics"]), 3)

    def test_max_tokens(self):
        doc, log = self.run_brief(FakeClient(message('{"email_summary": "cut', stop_reason="max_tokens")))
        self.assertFalse(doc["llm"])
        self.assertIn("cut off at max_tokens (12000)", log)

    def test_invalid_json(self):
        doc, log = self.run_brief(FakeClient(message("not json")))
        self.assertFalse(doc["llm"])
        self.assertIn("did not pass the checks", log)

    def api_error(self, cls, status):
        import httpx2

        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx2.Response(status, request=request, headers={"request-id": "req_err"})
        return cls("error", response=response, body=None)

    def test_api_errors(self):
        import httpx2

        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        cases = [
            (self.api_error(anthropic.RateLimitError, 429), "rate limited (429)"),
            (self.api_error(anthropic.AuthenticationError, 401), "the API key was refused (401)"),
            (self.api_error(anthropic.NotFoundError, 404), "not found (404)"),
            (self.api_error(anthropic.BadRequestError, 400), "rejected (400)"),
            (self.api_error(anthropic.InternalServerError, 500), "server error (500)"),
            (self.api_error(anthropic.APIStatusError, 418), "HTTP 418"),
            (anthropic.APITimeoutError(request=request), "timed out"),
            (anthropic.APIConnectionError(request=request), "could not reach the API"),
        ]
        for error, expected in cases:
            with self.subTest(type(error).__name__):
                doc, log = self.run_brief(FakeClient(error))
                self.assertFalse(doc["llm"])
                self.assertIn(expected, log)
                self.assertEqual(doc["usage"]["cost_usd"], 0.0)
                self.assertEqual(len(doc["topics"]), 3)

    def test_unexpected_error_still_writes_a_briefing(self):
        doc, log = self.run_brief(FakeClient(TypeError("surprise")))
        self.assertFalse(doc["llm"])
        self.assertIn("unexpected error while writing the briefing (TypeError: surprise)", log)

    def test_real_sdk_against_a_local_server(self):
        """The real SDK sends our request: beta header, fallbacks, effort,
        json_schema format, adaptive thinking, streaming."""
        answer = json.dumps(answer_for(self.cands))
        seen = {}

        def sse():
            events = [
                ("message_start", {"type": "message_start", "message": {
                    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
                    "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 2000, "output_tokens": 1, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 500}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": answer}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                   "usage": {"output_tokens": 4000}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["path"] = self.path
                seen["headers"] = {k.lower(): v for k, v in self.headers.items()}
                seen["body"] = json.loads(self.rfile.read(int(self.headers["content-length"])))
                body = sse()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = anthropic.Anthropic(api_key="sk-test", base_url=f"http://127.0.0.1:{server.server_address[1]}",
                                         max_retries=0, http_client=anthropic.DefaultHttpxClient(trust_env=False))
            doc, log = self.run_brief(client)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(seen["path"], "/v1/messages")
        self.assertEqual(seen["headers"]["anthropic-beta"], "server-side-fallback-2026-07-01")
        body = seen["body"]
        self.assertEqual(body["fallbacks"], "default")
        self.assertEqual(body["output_config"]["effort"], "medium")
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertIs(body["stream"], True)
        self.assertEqual(body["max_tokens"], 12000)
        self.assertTrue(doc["llm"])
        # 2000 x $4 + 500 x $0.20 (Opus 5.5 cache read) + 4000 x $20, per million tokens
        self.assertEqual(doc["usage"], {"input_tokens": 2500, "output_tokens": 4000, "cost_usd": 0.0881})


# ---------------------------------------------------------------- render.py

def extractive_briefing(tmp):
    out = os.path.join(tmp, "briefing.json")
    with mock.patch.object(learn, "load_settings", return_value=settings()):
        quiet(brief.main, ["--candidates", fixture_path("candidates.json"), "--out", out], now=NOW, environ={})
    return out


@unittest.skipUnless(HAS_PPTX, "needs python-pptx (requirements.lock)")
class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.docs = os.path.join(self.tmp, "docs")
        self.seen = os.path.join(self.tmp, "seen.json")
        shutil.copy(os.path.join(ROOT, "learn", "seen.json"), self.seen)
        self.briefing = extractive_briefing(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_render(self, briefing=None, now=NOW):
        argv = ["--briefing", briefing or self.briefing, "--candidates", fixture_path("candidates.json"),
                "--docs", self.docs, "--seen", self.seen]
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": os.path.join(self.tmp, "gh_output")}):
            code, log = quiet(render.main, argv, now=now)
        return code, log

    def read(self, *parts):
        with open(os.path.join(*parts), encoding="utf-8") as f:
            return json.load(f)

    def test_files(self):
        code, log = self.run_render()
        self.assertEqual(code, 0)
        day = os.path.join(self.docs, "learn", "2026-11-11")
        self.assertEqual(sorted(os.listdir(day)), ["briefing.json", "briefing.pptx"])  # no HTML
        written = self.read(day, "briefing.json")
        self.assertEqual(written, render.clean_briefing(self.read(self.briefing)))
        with open(os.path.join(self.tmp, "gh_output"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "date=2026-11-11\n")
        with open(os.path.join(day, "briefing.pptx"), "rb") as f:
            self.assertEqual(f.read(4), b"PK\x03\x04")

    def test_deck(self):
        self.run_render()
        prs = Presentation(os.path.join(self.docs, "learn", "2026-11-11", "briefing.pptx"))
        self.assertEqual(len(prs.slides), 2 + 3 * 3)  # title, three per topic, closing
        fonts, colors, links = set(), set(), []
        for slide in prs.slides:
            self.assertEqual(slide.background.fill.fore_color.rgb, RGBColor(0x0F, 0x2A, 0x1F))
            for shape in slide.shapes:
                if not shape.has_text_frame:
                    if shape.fill.type is not None:
                        colors.add(str(shape.fill.fore_color.rgb))
                    continue
                for p in shape.text_frame.paragraphs:
                    for run in p.runs:
                        fonts.add(run.font.name)
                        colors.add(str(run.font.color.rgb))
                        if run.hyperlink.address:
                            links.append(run.hyperlink.address)
        self.assertEqual(fonts, {"DM Serif Display", "Outfit"})
        self.assertIn("C9A227", colors)
        self.assertIn("https://openai.example.com/index/introducing-gpt-5-5", links)
        self.assertTrue(all(link.startswith("https://") for link in links))
        title_texts = [s.text_frame.text for s in prs.slides[0].shapes if s.has_text_frame]
        self.assertIn("Wednesday, November 11, 2026", title_texts)
        self.assertIn("Built from the feeds without a model today.",
                      " ".join(s.text_frame.text for s in prs.slides[-1].shapes if s.has_text_frame))

    def test_long_text_and_bad_values(self):
        doc = self.read(self.briefing)
        doc["llm"], doc["model"] = True, "claude-opus-5-5"
        doc["topics"][0]["what_happened"] = "\n\n".join(["A long paragraph of words. " * 20] * 6)
        doc["topics"][0]["read_first"].append({"title": "Bad", "url": "javascript:alert(1)", "source": "x", "why": "y"})
        doc["topics"][1]["title"] = "Ctrl\x07 char"
        doc["topics"].append("not a topic")
        path = os.path.join(self.tmp, "edited.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        code, _ = self.run_render(path)
        self.assertEqual(code, 0)
        written = self.read(self.docs, "learn", "2026-11-11", "briefing.json")
        self.assertEqual(len(written["topics"]), 3)
        self.assertNotIn("javascript:alert(1)", json.dumps(written))
        self.assertEqual(written["topics"][1]["title"], "Ctrl char")
        prs = Presentation(os.path.join(self.docs, "learn", "2026-11-11", "briefing.pptx"))
        self.assertIn("Written by claude-opus-5-5",
                      " ".join(s.text_frame.text for s in prs.slides[-1].shapes if s.has_text_frame))

    def test_bad_date_is_refused(self):
        doc = self.read(self.briefing)
        doc["date"] = "../../etc"
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        code, log = self.run_render(path)
        self.assertEqual(code, 1)
        self.assertIn("no valid date", log)
        self.assertFalse(os.path.exists(os.path.join(self.docs, "learn")))

    def test_no_topics(self):
        doc = self.read(self.briefing)
        doc["topics"] = []
        path = os.path.join(self.tmp, "empty.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        self.assertEqual(self.run_render(path)[0], 0)
        prs = Presentation(os.path.join(self.docs, "learn", "2026-11-11", "briefing.pptx"))
        self.assertEqual(len(prs.slides), 2)
        self.assertEqual(self.read(self.docs, "learn", "index.json")["days"][0]["topics"], [])

    def test_index(self):
        os.makedirs(os.path.join(self.docs, "learn"))
        with open(os.path.join(self.docs, "learn", "index.json"), "w", encoding="utf-8") as f:
            json.dump({"latest": "2026-11-11", "days": [
                {"date": "2026-11-09", "topics": ["Older"]},
                {"date": "2026-11-11", "topics": ["Replaced by this run"]},
                {"date": "bad", "topics": ["Dropped"]},
                {"date": "2026-11-10", "topics": ["Middle"]},
            ]}, f)
        self.run_render()
        index = self.read(self.docs, "learn", "index.json")
        self.assertEqual(set(index), {"latest", "days"})
        self.assertEqual(index["latest"], "2026-11-11")
        self.assertEqual([d["date"] for d in index["days"]], ["2026-11-11", "2026-11-10", "2026-11-09"])
        self.assertEqual(index["days"][0]["topics"], [t["title"] for t in self.read(self.briefing)["topics"]])
        self.assertEqual(render.updated_index(None, "2026-11-12", ["A"]),
                         {"latest": "2026-11-12", "days": [{"date": "2026-11-12", "topics": ["A"]}]})

    def test_seen_is_updated_and_a_rerun_does_not_duplicate(self):
        self.run_render()
        seen = self.read(self.seen)
        self.assertEqual(seen["version"], 1)
        self.assertEqual(len(seen["topics"]), 3)
        gpt = next(e for e in seen["topics"] if e["id"] == "introducing-gpt-5-5")
        self.assertEqual(gpt["date"], "2026-11-11")
        self.assertEqual(gpt["terms"], ["gpt5.5", "gpt", "5.5", "2m", "context", "window"])
        self.assertIn("https://a.example.com/p/gpt-5-5", gpt["urls"])
        self.assertIn("OpenAI news", gpt["sources"])
        self.assertTrue(gpt["primary"])
        self.run_render(now=NOW + timedelta(minutes=30))
        self.assertEqual(len(self.read(self.seen)["topics"]), 3)
        # A re-run that briefs other topics replaces the day's entries.
        doc = self.read(self.briefing)
        doc["topics"] = doc["topics"][:1]
        doc["topics"][0]["id"], doc["topics"][0]["title"] = "another-topic", "Another topic"
        path = os.path.join(self.tmp, "rerun.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        self.run_render(path, now=NOW + timedelta(minutes=40))
        self.assertEqual([e["id"] for e in self.read(self.seen)["topics"]], ["another-topic"])
        # Entries older than SEEN_KEEP_DAYS are pruned.
        old = learn.updated_seen(learn.load_seen(self.seen), [], "2027-03-01", NOW)
        self.assertEqual(old, [])

    def test_seen_without_candidates(self):
        argv = ["--briefing", self.briefing, "--docs", self.docs, "--seen", self.seen]
        quiet(render.main, argv, now=NOW)
        gpt = next(e for e in self.read(self.seen)["topics"] if e["id"] == "introducing-gpt-5-5")
        self.assertIn("gpt5.5", gpt["terms"])
        self.assertTrue(gpt["urls"])

    def test_seen_feeds_the_next_days_candidates(self):
        """A topic shown today is skipped tomorrow when nothing new came in."""
        self.run_render()
        seen = learn.load_seen(self.seen)
        items = [feed_item("Fed holds interest rates steady", "Macro Weekly", 5, "https://macro.example.com/fed-holds",
                           "The Fed left interest rates unchanged and signaled patience.")]
        doc = build(items, seen=seen, lane_items=[], now=NOW + timedelta(days=1))
        self.assertEqual(doc["date"], "2026-11-12")
        self.assertEqual(doc["topics"], [])
        self.assertEqual(doc["seen_skipped"][0]["seen_id"], "fed-holds-interest-rates-steady")
        # A second run today does not count this morning's log: the topic is picked again.
        doc = build(items, seen=seen, lane_items=[])
        self.assertEqual([t["id"] for t in doc["topics"]], ["fed-holds-interest-rates-steady"])


PROSE = ("OpenAI shipped the new model today, and the context window grows to two million tokens. The release notes say it "
         "handles long documents and large codebases in one request, and that pricing stays the same for prompts under "
         "the old limit. Early testers report better tool use and fewer dropped instructions in long sessions. Anthropic "
         "and Google have not answered yet, although both have said larger windows are coming this quarter. Independent "
         "benchmarks are still thin, so the practical ceiling for retrieval quality at that size remains unclear.")


def prose(words, paragraphs=3):
    """Ordinary English prose of exactly that many words, in short paragraphs
    that end at a sentence end."""
    pool = (PROSE + " ") * (words // len(PROSE.split()) + 1)
    picked = pool.split()[:words]
    picked[-1] = picked[-1].rstrip(",.") + "."
    parts, current = [], []
    for word in picked:
        current.append(word)
        if len(current) >= words / paragraphs and word.endswith(".") and len(parts) < paragraphs - 1:
            parts.append(" ".join(current))
            current = []
    return "\n\n".join(parts + ([" ".join(current)] if current else []))


@unittest.skipUnless(HAS_PPTX, "needs python-pptx (requirements.lock)")
class SlideLayoutTests(unittest.TestCase):
    """The deck, opened with python-pptx: three slides per topic, and body
    text at 16 points or more for a topic at the top of the length the model
    is asked for."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def deck(self, topics):
        doc = {"date": "2026-11-11", "generated_at": NOW.isoformat(), "model": "claude-opus-5-5", "llm": True,
               "usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.14}, "email_summary": "One topic.",
               "topics": topics}
        path = os.path.join(self.tmp, "briefing.pptx")
        render.build_deck(render.clean_briefing(doc), path)
        return Presentation(path)

    def long_topic(self):
        return {
            "id": "long-topic", "title": "OpenAI ships a model with a two million token context window",
            "lanes": ["Frontier models"], "off_lane": False,
            "what_happened": prose(280), "why_it_matters": prose(250),
            "read_first": [{"title": f"Read-first link number {n} with a title of ordinary length",
                            "url": f"https://a.example.com/p/{n}", "source": "Newsletter A",
                            "why": "It is the primary source and has the numbers the other posts quote."}
                           for n in (1, 2, 3)],
            "open_question": "Does a window this large change how you split work between retrieval and context, "
                             "or does quality fall off long before the limit?",
            "reading_minutes": 3,
            "signals": {"sources": 4, "primary": True, "new_since_last_week": True, "lane_hits": 4, "score": 9.7},
        }

    @staticmethod
    def texts(slide):
        return [s.text_frame.text for s in slide.shapes if s.has_text_frame]

    @staticmethod
    def runs(slide):
        return [r for s in slide.shapes if s.has_text_frame for p in s.text_frame.paragraphs for r in p.runs]

    def test_three_slides_per_topic(self):
        topic = self.long_topic()
        self.assertEqual(learn.word_count(topic["what_happened"]), 280)
        self.assertEqual(learn.word_count(topic["why_it_matters"]), 250)
        prs = self.deck([topic])
        self.assertEqual(len(prs.slides), 2 + 3)
        what, why, read = prs.slides[1], prs.slides[2], prs.slides[3]
        for slide in (what, why, read):
            self.assertIn("Topic 1 of 1  |  Frontier models", self.texts(slide))
            self.assertIn(topic["title"], self.texts(slide))
        self.assertIn("What happened", self.texts(what))
        self.assertNotIn("Why it matters", self.texts(what))
        self.assertIn("Why it matters", self.texts(why))
        self.assertIn("Read first", self.texts(read))
        self.assertIn("Open question", self.texts(read))
        self.assertIn(topic["open_question"], self.texts(read))
        # Each prose slide holds the whole text, in two columns of about the same length.
        for slide, text in ((what, topic["what_happened"]), (why, topic["why_it_matters"])):
            columns = [t for t in self.texts(slide) if len(t) > 200]
            self.assertEqual(len(columns), 2)
            self.assertEqual(" ".join(" ".join(columns).split()), " ".join(text.split()))
            self.assertLess(abs(len(columns[0]) - len(columns[1])), 0.25 * len(text))
            self.assertIn(text, slide.notes_slide.notes_text_frame.text)
        self.assertEqual([r.hyperlink.address for r in self.runs(read) if r.hyperlink.address],
                         ["https://a.example.com/p/1", "https://a.example.com/p/2", "https://a.example.com/p/3"])

    def test_body_text_is_16_points_or_more_at_the_top_of_the_length_range(self):
        prs = self.deck([self.long_topic()])
        # Every run of prose; the one-line signals footnote ("4 sources  |  ...") is a label, not body text.
        body = [r for slide in list(prs.slides)[1:4] for r in self.runs(slide) if len(r.text) > 60 and "  |  " not in r.text]
        self.assertGreaterEqual(len(body), 3 + 3 + 3 + 1)  # paragraphs, link notes and the question
        for r in body:
            self.assertGreaterEqual(r.font.size.pt, 16, r.text[:60])
        # Nothing is placed off the slide.
        for slide in prs.slides:
            for shape in slide.shapes:
                self.assertGreaterEqual(shape.left, 0)
                self.assertGreaterEqual(shape.top, 0)
                self.assertLessEqual(shape.left + shape.width, prs.slide_width)
                self.assertLessEqual(shape.top + shape.height, prs.slide_height)
        # Text far over the range still fits a slide: it steps down in size and never below 10 points.
        longer = dict(self.long_topic(), what_happened=prose(520))
        sizes = {r.font.size.pt for r in self.runs(self.deck([longer]).slides[1]) if len(r.text) > 60}
        self.assertEqual(len(sizes), 1)
        self.assertTrue(10 <= sizes.pop() < 16)

    def test_split_columns(self):
        left, right = render.split_columns("One. Two.\n\nThree. Four.")
        self.assertEqual((left, right), ("One. Two.", "Three. Four."))
        left, right = render.split_columns("A short one.\n\n" + "A much longer sentence follows here. " * 8)
        self.assertLess(abs(len(left) - len(right)), 80)  # broken inside the long paragraph, between sentences
        self.assertTrue(left.startswith("A short one.\n\nA much longer"))
        self.assertEqual(render.split_columns("Only one sentence"), ("Only one sentence", ""))

    def test_control_character_in_a_link_does_not_stop_the_deck(self):
        topic = self.long_topic()
        topic["read_first"][0]["url"] = "https://a.example.com/p\x01q"
        topic["read_first"][1]["title"] = "A title \u2014 with a dash"
        topic["title"] = "A headline \u2013 with a dash"
        clean = render.clean_briefing({"date": "2026-11-11", "topics": [topic]})
        self.assertEqual([r["url"] for r in clean["topics"][0]["read_first"]],
                         ["https://a.example.com/p/2", "https://a.example.com/p/3"])
        self.assertEqual(clean["topics"][0]["title"], "A headline, with a dash")
        self.assertEqual(clean["topics"][0]["read_first"][0]["title"], "A title, with a dash")
        prs = self.deck([topic])  # raised "All strings must be XML compatible" before
        links = [r.hyperlink.address for r in self.runs(prs.slides[3]) if r.hyperlink.address]
        self.assertEqual(links, ["https://a.example.com/p/2", "https://a.example.com/p/3"])

    def test_estimated_cost_is_marked(self):
        doc = {"date": "2026-11-11", "llm": True, "model": "claude-next-9", "topics": [],
               "usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.22, "cost_estimated": True}}
        clean = render.clean_briefing(doc)
        self.assertIs(clean["usage"]["cost_estimated"], True)
        path = os.path.join(self.tmp, "est.pptx")
        render.build_deck(clean, path)
        closing = " ".join(self.texts(Presentation(path).slides[-1]))
        self.assertIn("Cost of this run: about $0.22.", closing)
        doc["usage"]["cost_estimated"] = "yes"  # only a real true is kept
        self.assertNotIn("cost_estimated", render.clean_briefing(doc)["usage"])


class LaneBudgetTests(unittest.TestCase):
    """The lane fetch must finish inside the time the workflow gives the candidates step."""

    def test_lane_budget_fits_the_workflow_step(self):
        import re
        with open(os.path.join(ROOT, ".github", "workflows", "daily.yml"), encoding="utf-8") as f:
            workflow = f.read()
        limit = int(re.search(r"timeout --kill-after=\d+ (\d+) python scripts/learn/candidates\.py --out", workflow).group(1))
        # Leave room for start-up, clustering and writing the file.
        self.assertLessEqual(candidates.LANE_RUN_SECONDS + 15, limit)
        self.assertLessEqual(candidates.LANE_SOURCE_SECONDS, candidates.LANE_RUN_SECONDS)


if __name__ == "__main__":
    unittest.main()
