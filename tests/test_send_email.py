"""Offline tests for scripts/send_email.py (standard library only, no network)."""
import ast
import base64
import contextlib
import email
import email.policy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

import support  # noqa: F401  (puts scripts/ on sys.path)

import send_email as se

UTC = timezone.utc
LA = ZoneInfo("America/Los_Angeles")
# A winter morning run: 5:45 AM PST is 13:45 UTC.
THIS_START = datetime(2026, 11, 11, 13, 45, 12, tzinfo=UTC)
PREV_START = datetime(2026, 11, 10, 13, 45, 40, tzinfo=UTC)


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def item(title="Post", url="https://a.example/p", source="Source", bucket="highlight",
         published="2026-11-11T10:00:00+00:00", **extra):
    base = {
        "id": title, "title": title, "url": url, "source": source, "source_type": "rss",
        "published": published, "first_seen": published, "summary": "", "read_minutes": 3,
        "duration_sec": None, "themes": [], "bucket": bucket,
    }
    base.update(extra)
    return base


def topic(title="Agents get a memory", why="It changes how loops are built. More detail follows.", **extra):
    base = {
        "id": "t", "title": title, "lanes": ["Agents and loops"], "off_lane": False,
        "what_happened": "x", "why_it_matters": why,
        "read_first": [{"title": "Paper", "url": "https://a.example/paper", "source": "A", "why": "y"}],
        "open_question": "q", "reading_minutes": 3,
        "signals": {"sources": 2, "primary": True, "new_since_last_week": True, "lane_hits": 2, "score": 1.0},
    }
    base.update(extra)
    return base


def briefing_doc(date="2026-11-11", topics=None, **extra):
    base = {
        "date": date, "generated_at": "2026-11-11T13:50:00+00:00", "model": "claude-opus-5-5", "llm": True,
        "usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0},
        "email_summary": "Three things moved this week.",
        "topics": [topic()] if topics is None else topics,
    }
    base.update(extra)
    return base


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="news-digest-test-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def write_briefing(self, doc, date="2026-11-11", raw=None):
        folder = os.path.join(self.tmp, "learn", date)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "briefing.json"), "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(doc))
        return os.path.join(self.tmp, "learn")


class SafeUrlTests(unittest.TestCase):
    def test_keeps_web_links(self):
        self.assertEqual(se.safe_url("https://a.example/x"), "https://a.example/x")
        self.assertEqual(se.safe_url("http://a.example/x"), "http://a.example/x")

    def test_replaces_everything_else(self):
        for bad in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "mailto:a@b",
                    "/relative", "", None, 42):
            self.assertEqual(se.safe_url(bad), "#", bad)


class SettingsTests(TempDirCase):
    def tz_from(self, text):
        path = os.path.join(self.tmp, "config.yml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            return se.load_timezone(path)

    def test_timezone_comes_from_config(self):
        self.assertEqual(str(se.load_timezone()), "America/Los_Angeles")  # the committed config.yml
        self.assertEqual(str(self.tz_from("feeds: {}\ntimezone: Europe/Paris  # mine\n")), "Europe/Paris")
        self.assertEqual(str(self.tz_from('timezone: "Asia/Tokyo"\n')), "Asia/Tokyo")

    def test_bad_or_missing_timezone_falls_back(self):
        self.assertEqual(str(self.tz_from("timezone: Mars/Base\n")), "America/Los_Angeles")
        self.assertEqual(str(self.tz_from("learn:\n  timezone: Europe/Paris\n")), "America/Los_Angeles")
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(str(se.load_timezone(os.path.join(self.tmp, "missing.yml"))), "America/Los_Angeles")

    def test_site_url(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(se.site_url({"SITE_URL": "https://news.example.com"}), "https://news.example.com/")
            self.assertEqual(se.site_url({"SITE_URL": "https://me.github.io/digest/"}), "https://me.github.io/digest/")
            self.assertEqual(se.site_url({"GITHUB_REPOSITORY": "Some-Owner/news-digest"}),
                             "https://some-owner.github.io/news-digest/")
            self.assertEqual(se.site_url({"GITHUB_REPOSITORY": "me/me.github.io"}), "https://me.github.io/")
            self.assertIsNone(se.site_url({}))
            for bad in ('https://x.example/"><script>', "http://plain.example", "javascript:alert(1)",
                        "https://x.example/a b"):
                self.assertEqual(se.site_url({"SITE_URL": bad, "GITHUB_REPOSITORY": "o/r"}), "https://o.github.io/r/", bad)
        self.assertIn("SITE_URL is not a plain https:// address", out.getvalue())


class WindowStartTests(unittest.TestCase):
    END = THIS_START
    DAY = END - timedelta(hours=24)

    def test_no_previous_run_covers_24_hours(self):
        self.assertEqual(se.window_start(self.END, None), self.DAY)

    def test_starts_where_the_previous_morning_run_started(self):
        self.assertEqual(se.window_start(self.END, PREV_START), PREV_START)
        two_days = self.END - timedelta(days=2, minutes=3)
        self.assertEqual(se.window_start(self.END, two_days), two_days)

    def test_previous_start_not_before_this_one_is_ignored(self):
        self.assertEqual(se.window_start(self.END, self.END), self.DAY)
        self.assertEqual(se.window_start(self.END, self.END + timedelta(minutes=1)), self.DAY)

    def test_lookback_is_capped_at_seven_days(self):
        self.assertEqual(se.window_start(self.END, self.END - timedelta(days=8)), self.DAY)
        self.assertEqual(se.window_start(self.END, self.END - timedelta(days=7)), self.DAY)
        almost = self.END - timedelta(days=7) + timedelta(minutes=1)
        self.assertEqual(se.window_start(self.END, almost), almost)

    def test_consecutive_runs_meet_exactly(self):
        # Each digest starts where the previous one ended, however late the runs start.
        starts = [datetime(2026, 11, d, 13, 45, tzinfo=UTC) + timedelta(minutes=m)
                  for d, m in ((7, 0), (8, 12), (9, 1), (10, 40), (11, 2))]
        items = [item(f"t{n}", published=(starts[0] + timedelta(minutes=37 * n)).isoformat()) for n in range(150)]
        seen = []
        for prev, cur in zip(starts, starts[1:]):
            seen += [i["title"] for i in se.select_window(items, se.window_start(cur, prev), cur)]
        inside = [i["title"] for i in items if starts[0] <= datetime.fromisoformat(i["published"]) < starts[-1]]
        self.assertEqual(sorted(seen), sorted(inside))
        self.assertEqual(len(seen), len(set(seen)))


class SelectWindowTests(unittest.TestCase):
    START = PREV_START
    END = THIS_START

    def titles(self, items, late_after=None):
        return [i["title"] for i in se.select_window(items, self.START, self.END, late_after)]

    def test_start_inclusive_end_exclusive(self):
        items = [
            item("before", published="2026-11-10T13:45:39+00:00"),
            item("at start", published="2026-11-10T13:45:40+00:00"),
            item("naive is utc", published="2026-11-11T01:00:00"),
            item("at end", published="2026-11-11T13:45:12+00:00"),
            item("after", published="2026-11-11T13:50:00+00:00"),
            item("bad date", published="yesterday"),
        ]
        self.assertEqual(self.titles(items), ["naive is utc", "at start"])

    def test_late_arrivals(self):
        late_after = self.START + se.BUILD_SLACK
        items = [
            # Listed by the feed after the previous run had built its data.
            item("late", published="2026-11-10T13:30:00+00:00", first_seen="2026-11-10T20:00:00+00:00"),
            item("late, seen this morning", published="2026-11-10T09:00:00+00:00",
                 first_seen="2026-11-11T13:46:00+00:00"),
            # Seen by the previous run, so that email had it.
            item("in the last email", published="2026-11-10T13:30:00+00:00", first_seen="2026-11-10T13:47:00+00:00"),
            item("seen at noon the day before", published="2026-11-10T03:00:00+00:00",
                 first_seen="2026-11-09T20:00:00+00:00"),
            # Too old to count as late.
            item("old", published="2026-11-09T12:00:00+00:00", first_seen="2026-11-10T20:00:00+00:00"),
            item("no first_seen", published="2026-11-10T13:30:00+00:00", first_seen=None),
            item("on time", published="2026-11-11T05:00:00+00:00"),
        ]
        self.assertEqual(self.titles(items), ["on time"])
        self.assertEqual(self.titles(items, late_after), ["on time", "late", "late, seen this morning"])


def jobs_reply(email="success", **others):
    """The reply of GET /actions/runs/{id}/jobs: one entry per job, the email
    job with the given conclusion (None leaves it out)."""
    jobs = [{"name": name, "conclusion": conclusion} for name, conclusion in
            {"build": "success", "commit": "success", "deploy": "success", **others}.items()]
    if email is not None:
        jobs.append({"name": "email", "conclusion": email})
    return {"total_count": len(jobs), "jobs": jobs}


def run_id_of(url):
    """The run id in a .../actions/runs/{id}/jobs URL, else None."""
    path = url.split("?", 1)[0]
    return path.split("/")[-2] if path.endswith("/jobs") else None


class GithubApiCase(unittest.TestCase):
    ENV = {"GITHUB_TOKEN": "github-token-value", "GITHUB_REPOSITORY": "owner/news-digest", "GITHUB_RUN_ID": "300"}

    def call(self, fn, reply, env=None, jobs=None, **kwargs):
        """Run fn against a fake API. reply is a dict to return as JSON, raw
        bytes, or an exception to raise. jobs maps a run id to the reply for
        its jobs (default: its email job ended in success). Returns (result,
        stdout)."""
        self.calls = []
        jobs = jobs or {}

        def opener(req, timeout=None):
            self.calls.append((req, timeout))
            run_id = run_id_of(req.full_url)
            answer = reply if run_id is None else jobs.get(int(run_id), jobs_reply())
            if isinstance(answer, BaseException):
                raise answer
            return Resp(answer if isinstance(answer, bytes) else json.dumps(answer).encode())

        out = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=opener), contextlib.redirect_stdout(out):
            got = fn(dict(self.ENV if env is None else env), **kwargs)
        self.assertNotIn("github-token-value", out.getvalue())
        return got, out.getvalue()

    def job_lookups(self):
        return [int(run_id_of(req.full_url)) for req, _ in self.calls if run_id_of(req.full_url)]

    def check_headers(self, req, timeout):
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("Authorization"), "Bearer github-token-value")
        self.assertEqual(req.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(req.get_header("X-github-api-version"), "2022-11-28")
        self.assertEqual(timeout, 15)


def morning(run_id, created, **extra):
    return {"id": run_id, "display_title": "Daily digest (morning)", "created_at": created, **extra}


class PreviousRunTests(GithubApiCase):
    def lookup(self, reply, env=None, jobs=None, end=None):
        return self.call(se.previous_run_time, reply, env, jobs, end=end)

    def test_request(self):
        self.lookup({"workflow_runs": []})
        self.assertEqual(len(self.calls), 1)
        req, timeout = self.calls[0]
        url = req.full_url
        self.assertTrue(url.startswith(
            "https://api.github.com/repos/owner/news-digest/actions/workflows/daily.yml/runs?"), url)
        query = se.urllib.parse.parse_qs(url.split("?", 1)[1])
        # Completed, not success: a run whose email went out counts even when another job failed.
        self.assertEqual(query, {"status": ["completed"], "branch": ["main"], "per_page": ["50"]})
        self.check_headers(req, timeout)

    def test_jobs_request(self):
        self.lookup({"workflow_runs": [morning(298, "2026-11-10T13:45:40Z")]})
        req, timeout = self.calls[1]
        base, query = req.full_url.split("?", 1)
        self.assertEqual(base, "https://api.github.com/repos/owner/news-digest/actions/runs/298/jobs")
        # filter=all: the email job of an earlier attempt counts too.
        self.assertEqual(se.urllib.parse.parse_qs(query), {"filter": ["all"], "per_page": ["100"]})
        self.check_headers(req, timeout)

    def test_takes_the_latest_morning_run_other_than_this_one(self):
        runs = [
            morning(300, "2026-11-11T13:45:12Z"),
            {"id": 299, "display_title": "Daily digest (noon)", "created_at": "2026-11-10T20:00:05Z"},
            morning(298, "2026-11-10T13:45:40Z"),
            morning(297, "2026-11-09T13:45:00Z"),
            {"id": 296, "display_title": "Something else", "created_at": "2026-11-11T01:00:00Z"},
        ]
        got, _ = self.lookup({"workflow_runs": runs})
        self.assertEqual(got, PREV_START)
        self.assertEqual(self.job_lookups(), [298])  # newest first, and it stops at the first that sent

    def test_a_run_counts_when_its_email_went_out_even_if_the_run_failed(self):
        # Render, deploy or the push failed, so the run ended as failure, but
        # the email was sent: the next digest must start there, not a day earlier.
        runs = [morning(298, "2026-11-10T13:45:40Z", conclusion="failure"),
                morning(297, "2026-11-09T13:45:00Z", conclusion="success")]
        jobs = {298: jobs_reply(email="success", render="failure", deploy="failure")}
        got, _ = self.lookup({"workflow_runs": runs}, jobs=jobs)
        self.assertEqual(got, PREV_START)

    def test_a_run_whose_email_did_not_go_out_is_passed_over(self):
        runs = [morning(299, "2026-11-10T13:45:40Z", conclusion="success"),
                morning(298, "2026-11-09T13:45:10Z", conclusion="failure"),
                morning(297, "2026-11-08T13:45:00Z", conclusion="success")]
        for not_sent in (jobs_reply(email="failure"), jobs_reply(email="skipped"), jobs_reply(email="cancelled"),
                         jobs_reply(email=None), {"jobs": []}):
            jobs = {299: not_sent, 298: jobs_reply(email="failure")}
            got, _ = self.lookup({"workflow_runs": runs}, jobs=jobs)
            self.assertEqual(got, datetime(2026, 11, 8, 13, 45, tzinfo=UTC), not_sent)
            self.assertEqual(self.job_lookups(), [299, 298, 297])

    def test_an_email_sent_by_a_rerun_counts(self):
        # filter=all lists the jobs of every attempt: failed first, then sent.
        jobs = {298: {"jobs": [{"name": "email", "conclusion": "failure", "run_attempt": 1},
                               {"name": "email", "conclusion": "success", "run_attempt": 2}]}}
        got, _ = self.lookup({"workflow_runs": [morning(298, "2026-11-10T13:45:40Z")]}, jobs=jobs)
        self.assertEqual(got, PREV_START)

    def test_a_rerun_keeps_the_time_the_run_was_created(self):
        # run_started_at moves when a run is re-run; created_at does not.
        runs = [morning(298, "2026-11-10T13:45:40Z", run_started_at="2026-11-10T22:00:00Z", run_attempt=2)]
        got, _ = self.lookup({"workflow_runs": runs})
        self.assertEqual(got, PREV_START)

    def test_falls_back_to_run_started_at(self):
        got, _ = self.lookup({"workflow_runs": [
            {"id": 299, "display_title": "Daily digest (morning)", "run_started_at": "2026-11-10T13:45:00Z"}]})
        self.assertEqual(got, datetime(2026, 11, 10, 13, 45, tzinfo=UTC))

    def test_only_runs_created_in_the_seven_days_before_the_end_are_read(self):
        runs = [morning(310, "2026-11-11T18:00:00Z"),   # created after this run (this one is a late re-run)
                morning(280, "2026-11-04T13:45:12Z"),   # exactly 7 days back
                morning(270, "2026-11-03T13:45:00Z")]
        got, out = self.lookup({"workflow_runs": runs}, end=THIS_START)
        self.assertIsNone(got)
        self.assertEqual(self.job_lookups(), [])
        self.assertIn("more than 7 days back", out)
        runs.append(morning(285, "2026-11-04T13:45:13Z"))
        got, _ = self.lookup({"workflow_runs": runs}, end=THIS_START)
        self.assertEqual(got, datetime(2026, 11, 4, 13, 45, 13, tzinfo=UTC))
        self.assertEqual(self.job_lookups(), [285])

    def test_job_lookups_are_capped(self):
        runs = [morning(100 + n, f"2026-11-10T{n:02d}:00:00Z") for n in range(20)]
        jobs = {100 + n: jobs_reply(email="failure") for n in range(20)}
        got, out = self.lookup({"workflow_runs": runs}, jobs=jobs)
        self.assertIsNone(got)
        self.assertEqual(self.job_lookups(), list(range(119, 109, -1)))
        self.assertIn("No previous morning run that sent its email was found", out)

    def test_only_the_current_run_or_noon_runs(self):
        got, out = self.lookup({"workflow_runs": [
            morning(300, "2026-11-11T13:45:12Z"),
            {"id": 299, "display_title": "Daily digest (noon)", "created_at": "2026-11-10T20:00:00Z"},
            {"id": 298, "created_at": "2026-11-10T13:45:00Z"},
        ]})
        self.assertIsNone(got)
        self.assertEqual(self.job_lookups(), [])
        self.assertIn("No previous morning run that sent its email was found", out)

    def test_api_errors_fall_back(self):
        errors = [
            urllib.error.HTTPError("https://api.github.com/x", 403, "Forbidden", {}, io.BytesIO(b"{}")),
            urllib.error.URLError("Name or service not known"),
            TimeoutError("timed out"),
        ]
        for err in errors:
            got, out = self.lookup(err)
            self.assertIsNone(got, err)
            self.assertIn("Could not read the previous run", out)
        self.assertIn("HTTP 403", self.lookup(errors[0])[1])

    def test_a_jobs_error_falls_back_instead_of_guessing(self):
        runs = {"workflow_runs": [morning(298, "2026-11-10T13:45:40Z"), morning(297, "2026-11-09T13:45:00Z")]}
        error = urllib.error.HTTPError("https://api.github.com/x", 502, "Bad Gateway", {}, io.BytesIO(b""))
        for bad in (error, b"not json", b"[]", {"jobs": "x"}):
            got, out = self.lookup(runs, jobs={298: bad})
            self.assertIsNone(got, bad)
            self.assertEqual(self.job_lookups(), [298])  # the older run is not taken in its place
            self.assertIn("Could not read the jobs of the previous run", out)
        self.assertIn("HTTP 502", self.lookup(runs, jobs={298: error})[1])

    def test_bad_replies_fall_back(self):
        for reply in (b"not json", b"[]", {"workflow_runs": "x"}, {"workflow_runs": [None, {"id": 1}]},
                      {"workflow_runs": [{"id": 1, "display_title": "(morning)", "created_at": "soon"}]},
                      {"workflow_runs": [{"id": "1/../2", "display_title": "(morning)",
                                          "created_at": "2026-11-10T13:45:00Z"}]}):
            got, _ = self.lookup(reply)
            self.assertIsNone(got, reply)
            self.assertEqual(self.job_lookups() if isinstance(reply, dict) else [], [])

    def test_missing_env_makes_no_request(self):
        for env in ({}, {"GITHUB_REPOSITORY": "owner/news-digest"}, {"GITHUB_TOKEN": "github-token-value"},
                    {"GITHUB_TOKEN": "github-token-value", "GITHUB_REPOSITORY": "owner/x?y=1"}):
            got, out = self.lookup({"workflow_runs": []}, env)
            self.assertIsNone(got)
            self.assertEqual(self.calls, [])
            self.assertIn("No GITHUB_TOKEN or GITHUB_REPOSITORY", out)


class CurrentRunTests(GithubApiCase):
    def test_reads_when_this_run_was_created(self):
        got, _ = self.call(se.current_run_start, {"id": 300, "created_at": "2026-11-11T13:45:12Z"})
        self.assertEqual(got, THIS_START)
        req, timeout = self.calls[0]
        self.assertEqual(req.full_url, "https://api.github.com/repos/owner/news-digest/actions/runs/300")
        self.check_headers(req, timeout)

    def test_a_rerun_ends_where_the_first_attempt_did(self):
        # "Re-run failed jobs" at 14:00 after the 05:45 email failed: the data
        # is the first attempt's, so the digest must still end at 05:45.
        got, _ = self.call(se.current_run_start, {"id": 300, "run_attempt": 2, "created_at": "2026-11-11T13:45:12Z",
                                                  "run_started_at": "2026-11-11T22:00:00Z"})
        self.assertEqual(got, THIS_START)

    def test_falls_back_to_run_started_at(self):
        got, _ = self.call(se.current_run_start, {"id": 300, "run_started_at": "2026-11-11T13:45:12Z"})
        self.assertEqual(got, THIS_START)

    def test_failures_end_the_digest_now(self):
        for reply in (urllib.error.HTTPError("https://api.github.com/x", 404, "Not Found", {}, io.BytesIO(b"")),
                      b"[]", {"created_at": "later"}):
            got, out = self.call(se.current_run_start, reply)
            self.assertIsNone(got)
            self.assertIn("so the digest ends now", out)

    def test_missing_env_makes_no_request(self):
        for env in ({"GITHUB_TOKEN": "github-token-value", "GITHUB_REPOSITORY": "owner/news-digest"},
                    dict(self.ENV, GITHUB_RUN_ID="../1")):
            got, out = self.call(se.current_run_start, {}, env)
            self.assertIsNone(got)
            self.assertEqual(self.calls, [])


class BriefingTests(TempDirCase):
    def load(self, doc, date="2026-11-11", raw=None):
        learn = self.write_briefing(doc, raw=raw)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            got = se.load_briefing(date, learn)
        return got, out.getvalue()

    def test_valid_briefing(self):
        long_title = "word " * 80
        doc = briefing_doc(topics=[
            topic(),
            topic(title=long_title, why="No end mark here", lanes=[], off_lane=True, reading_minutes=90),
            "not a topic",
            topic(title="   "),
        ])
        got, _ = self.load(doc)
        self.assertEqual(got["date"], "2026-11-11")
        self.assertTrue(got["llm"])
        self.assertEqual(got["summary"], "Three things moved this week.")
        self.assertEqual(len(got["topics"]), 2)
        first, second = got["topics"]
        self.assertEqual(first, {"title": "Agents get a memory", "why": "It changes how loops are built.",
                                 "lanes": ["Agents and loops"], "off_lane": False, "minutes": 3})
        self.assertLessEqual(len(second["title"]), se.MAX_TITLE)
        self.assertTrue(second["title"].endswith("..."))
        self.assertEqual(second["why"], "No end mark here")
        self.assertTrue(second["off_lane"])
        self.assertIsNone(second["minutes"])

    def test_topics_are_capped(self):
        got, _ = self.load(briefing_doc(topics=[topic(title=f"T{n}") for n in range(20)]))
        self.assertEqual(len(got["topics"]), se.MAX_TOPICS)

    def test_no_model_flag(self):
        got, _ = self.load(briefing_doc(llm=False))
        self.assertFalse(got["llm"])

    def test_unusable_files_are_left_out(self):
        cases = [
            (briefing_doc(date="2026-11-10"), None, "not for today"),
            (briefing_doc(topics="x"), None, "no topic list"),
            (briefing_doc(topics=[]), None, "no topics"),
            ([1, 2], None, "no topic list"),
            (None, "{not json", "Could not read"),
        ]
        for doc, raw, note in cases:
            got, out = self.load(doc, raw=raw)
            self.assertIsNone(got, note)
            self.assertIn(note, out)

    def test_missing_or_oversized(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertIsNone(se.load_briefing("2026-11-11", os.path.join(self.tmp, "learn")))
        self.assertIn("No briefing for 2026-11-11", out.getvalue())
        self.assertIsNone(se.load_briefing("../../etc", os.path.join(self.tmp, "learn")))
        with mock.patch.object(se, "MAX_BRIEFING_BYTES", 10):
            got, out = self.load(briefing_doc())
        self.assertIsNone(got)
        self.assertIn("too large", out)

    def test_first_sentence(self):
        self.assertEqual(se.first_sentence("One. Two."), "One.")
        self.assertEqual(se.first_sentence("Version 4.5 ships. More."), "Version 4.5 ships.")
        self.assertEqual(se.first_sentence("e.g. this one counts. Next"), "e.g. this one counts.")
        self.assertEqual(se.first_sentence(None), "")
        self.assertLessEqual(len(se.first_sentence("x " * 500)), se.MAX_WHY)


class HtmlTests(unittest.TestCase):
    START = PREV_START
    END = THIS_START
    BASE = "https://owner.github.io/news-digest/"

    def render(self, items, sources=(), fallback=False, briefing=None, base=BASE, tz=LA):
        return se.build_html(items, list(sources), self.START, self.END, fallback, briefing, base, tz)

    def briefing(self, **extra):
        doc = {"date": "2026-11-11", "summary": "Summary text.", "llm": True,
               "topics": [{"title": "Agents get a memory", "why": "It changes loops.", "lanes": ["Agents and loops"],
                           "off_lane": False, "minutes": 3}]}
        doc.update(extra)
        return doc

    def test_feed_text_is_escaped(self):
        hostile = item(
            title='<script>alert(1)</script>"><img src=x onerror=alert(2)>',
            url="javascript:alert(document.cookie)",
            source="A&B <i>",
            summary="<b>bold</b> & <a href='https://evil.example'>x</a>",
            themes=['"><svg onload=alert(3)>'],
        )
        out = self.render([hostile], [{"label": "<Bad> source", "ok": False, "error": "HTTP <500>"}])
        for raw in ("<script", "<img", "<svg", "<i>", "<b>", "javascript:", "<Bad>", "<500>", "evil.example'>"):
            self.assertNotIn(raw, out)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", out)
        self.assertIn('href="#"', out)
        self.assertIn("A&amp;B &lt;I&gt;", out)  # source tag, upper-cased before escaping
        self.assertNotIn("&AMP;", out)
        self.assertIn("&lt;Bad&gt; source &middot; HTTP &lt;500&gt;", out)

    def test_briefing_text_is_escaped(self):
        b = self.briefing(summary="<img src=x onerror=alert(1)> & more", topics=[{
            "title": '"><script>alert(1)</script>', "why": "<b>why</b>", "lanes": ["<svg onload=x>"],
            "off_lane": False, "minutes": 2}])
        out = self.render([item()], briefing=b)
        for raw in ("<script", "<img", "<svg", "<b>why"):
            self.assertNotIn(raw, out)
        self.assertIn("&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;", out)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt; &amp; more", out)

    def test_briefing_section(self):
        out = self.render([item("q")], briefing=self.briefing())
        self.assertIn("TODAY&#x27;S BRIEFING · 1", out)
        self.assertEqual(out.count('href="https://owner.github.io/news-digest/learn/2026-11-11/"'), 2)
        self.assertIn(">Agents get a memory</a>", out)
        self.assertIn("It changes loops.", out)
        self.assertIn("Summary text.", out)
        self.assertIn("Open the slides", out)
        self.assertIn("AGENTS AND LOOPS&nbsp;&nbsp;&middot;&nbsp;&nbsp;3 MIN", out)
        self.assertLess(out.index("TODAY&#x27;S BRIEFING"), out.index("QUICK READS"))
        self.assertNotIn(se.NO_MODEL_NOTE, out)

    def test_briefing_variants(self):
        b = self.briefing(llm=False, summary="", topics=[
            {"title": "Elsewhere", "why": "", "lanes": [], "off_lane": True, "minutes": None},
            {"title": "Bare", "why": "", "lanes": [], "off_lane": False, "minutes": None}])
        out = self.render([], briefing=b, base=None)
        self.assertIn(se.NO_MODEL_NOTE, out)
        self.assertIn("OUTSIDE THE LANES", out)
        self.assertNotIn("/learn/", out)
        self.assertNotIn("Open the slides", out)
        self.assertIn("Nothing new since the last digest.", out)

    def test_no_briefing_no_section(self):
        out = self.render([item()])
        self.assertNotIn("BRIEFING", out)
        self.assertNotIn("/learn/", out)

    def test_link_attribute_quotes_are_escaped(self):
        out = self.render([item(url='https://a.example/x" onmouseover="alert(1)')])
        self.assertIn('href="https://a.example/x&quot; onmouseover=&quot;alert(1)"', out)

    def test_sections_and_empty_state(self):
        out = self.render([item("q", bucket="highlight")])
        self.assertIn("QUICK READS · 1", out)
        self.assertNotIn("LONG READS AND VIDEOS", out)
        out = self.render([item("d", bucket="deeper")])
        self.assertIn("LONG READS AND VIDEOS · 1", out)
        self.assertNotIn("QUICK READS", out)
        out = self.render([])
        self.assertIn("Nothing new since the last digest.", out)
        self.assertNotIn("QUICK READS", out)
        self.assertNotIn("LONG READS", out)

    def test_each_section_is_capped(self):
        items = [item(f"t{n}", url=f"https://a.example/{n}") for n in range(45)]
        out = self.render(items)
        self.assertEqual(out.count('<a href="https://a.example/'), se.MAX_PER_SECTION)
        self.assertIn("5 more on the dashboard.", out)

    def test_footer_lists_failed_sources_only(self):
        sources = [
            {"label": "Good", "type": "rss", "ok": True, "count": 3, "error": None},
            {"label": "Broken", "type": "rss", "ok": False, "count": 2, "error": "timeout"},
        ]
        out = self.render([item()], sources)
        self.assertIn("Sources that could not be fetched this run: Broken &middot; timeout.", out)
        self.assertNotIn("Good &middot;", out)
        self.assertNotIn("could not be fetched", self.render([item()], sources[:1]))

    def test_header_shows_the_window_in_local_time(self):
        out = self.render([item()])
        self.assertIn("Published Tue Nov 10, 5:45 AM PST to Wed Nov 11, 5:45 AM PST", out)
        self.assertIn("DAILY DIGEST · WEDNESDAY, NOVEMBER 11", out)
        summer = se.build_html([], [], datetime(2026, 7, 1, 12, 45, tzinfo=UTC),
                               datetime(2026, 7, 2, 19, 0, tzinfo=UTC), tz=LA)
        self.assertIn("Published Wed Jul 1, 5:45 AM PDT to Thu Jul 2, 12:00 PM PDT", summer)

    def test_fallback_note(self):
        self.assertNotIn("last committed data", self.render([item()]))
        out = self.render([item()], fallback=True)
        self.assertEqual(out.count(se.FALLBACK_NOTE), 1)
        self.assertIn("Built from the last committed data because the refresh step failed.", out)

    def test_brand_and_copy(self):
        out = self.render([item()], fallback=True, briefing=self.briefing(llm=False))
        for token in ("#f4f3ef", "#fbfaf7", "#0d0d0d", "#55554f", "#d6d4cd", "#1f6f7a", "#6f291e", "Geist Mono"):
            self.assertIn(token, out)
        self.assertNotIn(chr(0x2014), out)  # no em dash
        self.assertNotIn(chr(0x2013), out)  # no en dash

    def test_tags(self):
        self.assertEqual(se.time_tag({"source_type": "youtube", "url": "https://www.youtube.com/watch?v=x"}), "Video")
        self.assertEqual(se.time_tag({"source_type": "youtube", "url": "https://www.youtube.com/shorts/x"}), "Short")
        self.assertEqual(se.time_tag({"source_type": "rss", "read_minutes": 7}), "7 min read")

    def test_message_size_stays_under_gmail_clipping(self):
        # Gmail clips an HTML body over about 102 KB.
        items = [item(f"A reasonably long title for post number {n}", url=f"https://a.example/p/{n}",
                      bucket="highlight" if n % 2 else "deeper", summary="word " * 60,
                      themes=["AI/ML", "Macro/Fed"]) for n in range(80)]
        topics = [{"title": "t " * 90, "why": "w " * 150, "lanes": ["Agents and loops"], "off_lane": False,
                   "minutes": 3}] * se.MAX_TOPICS
        out = self.render(items, briefing=self.briefing(topics=topics, summary="s " * 550))
        self.assertLess(len(out.encode("utf-8")), 100_000)


class MessageTests(unittest.TestCase):
    def test_subject_and_parts(self):
        msg = se.build_message("from@example.com", "to@example.com", [item()], [], PREV_START, THIS_START, tz=LA)
        parsed = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
        self.assertEqual(parsed["Subject"], "News digest · Wednesday, November 11")
        types = [p.get_content_type() for p in parsed.iter_parts()]
        self.assertEqual(types, ["text/plain", "text/html"])

    def test_text_part(self):
        briefing = {"date": "2026-11-11", "summary": "Summary text.", "llm": False, "topics": [
            {"title": "Agents get a memory", "why": "It changes loops.", "lanes": ["Agents and loops"],
             "off_lane": False, "minutes": 3},
            {"title": "Elsewhere", "why": "", "lanes": [], "off_lane": False, "minutes": None}]}
        text = se.build_text([item()], [], PREV_START, THIS_START, fallback=True, briefing=briefing,
                             base="https://o.github.io/r/", tz=LA)
        lines = text.splitlines()
        self.assertEqual(lines[:4], ["News digest · Wednesday, November 11",
                                     "Published Tue Nov 10, 5:45 AM PST to Wed Nov 11, 5:45 AM PST",
                                     se.FALLBACK_NOTE, ""])
        self.assertEqual(lines[4:15], [
            "Today's briefing (2)", "",
            "- Agents get a memory", "  Agents and loops · 3 min", "  It changes loops.",
            "- Elsewhere", "",
            "Summary text.", "",
            "Slides: https://o.github.io/r/learn/2026-11-11/", "",
        ])
        self.assertIn(se.NO_MODEL_NOTE, text)
        self.assertLess(text.index("Today's briefing"), text.index("Quick reads (1)"))


class EnvAndHttpTests(TempDirCase):
    SECRETS = {
        "GMAIL_CLIENT_ID": "client-id-value",
        "GMAIL_CLIENT_SECRET": "client-secret-value",
        "GMAIL_REFRESH_TOKEN": "refresh-token-value",
        "SENDER_EMAIL": "from@example.com",
        "RECIPIENT_EMAIL": "to@example.com",
    }
    GITHUB = {"GITHUB_TOKEN": "github-token-value", "GITHUB_REPOSITORY": "owner/news-digest", "GITHUB_RUN_ID": "300"}

    def test_check_env_names_missing_secrets_only(self):
        env = dict(self.SECRETS)
        del env["GMAIL_REFRESH_TOKEN"]
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as cm:
                se.check_env()
        msg = str(cm.exception.code)
        self.assertIn("GMAIL_REFRESH_TOKEN", msg)
        self.assertIn("digest environment", msg)
        self.assertNotIn("client-secret-value", msg)

    def test_http_error_prints_status_and_short_body_only(self):
        body = json.dumps({"error": "invalid_grant", "error_description": "x" * 1000}).encode()
        err = urllib.error.HTTPError(se.GMAIL_TOKEN_URL, 400, "Bad Request", {}, io.BytesIO(body))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(SystemExit) as cm:
                se.get_access_token("client-id-value", "client-secret-value", "refresh-token-value")
        msg = str(cm.exception.code)
        self.assertTrue(msg.startswith("Token refresh failed: HTTP 400 "))
        self.assertIn("invalid_grant", msg)
        self.assertLessEqual(len(msg) - len("Token refresh failed: HTTP 400 "), 300)
        for secret in ("client-secret-value", "refresh-token-value", "client-id-value"):
            self.assertNotIn(secret, msg)

    def fake_urlopen(self, reply):
        def opener(req, timeout=None):
            self.requests.append(req)
            return Resp(json.dumps(reply).encode())

        self.requests = []
        return opener

    def test_broad_scope_warns_but_still_sends(self):
        reply = {"access_token": "at", "scope": "https://mail.google.com/"}
        out = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=self.fake_urlopen(reply)), \
                contextlib.redirect_stdout(out):
            self.assertEqual(se.get_access_token("id", "secret", "refresh"), "at")
        self.assertIn("::warning::", out.getvalue())
        self.assertIn("https://mail.google.com/", out.getvalue())
        self.assertNotIn("secret", out.getvalue())

    def test_send_only_scope_is_accepted(self):
        reply = {"access_token": "at", "scope": se.SEND_SCOPE, "expires_in": 3599}
        with mock.patch("urllib.request.urlopen", side_effect=self.fake_urlopen(reply)):
            self.assertEqual(se.get_access_token("id", "secret", "refresh"), "at")
        req = self.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, se.GMAIL_TOKEN_URL)

    def run_main(self, items, now, env=None, runs_reply=None, this_run=None, briefing=None, jobs=None):
        """Run main against fake Gmail and GitHub APIs. runs_reply is the reply
        for the list of runs (a dict, or an exception to raise), this_run the
        reply for the current run, jobs the reply for the jobs of a run by its
        id (default: its email job ended in success). Returns (requests,
        stdout, email)."""
        sent = []
        this_run = {"id": 300, "created_at": THIS_START.isoformat()} if this_run is None else this_run
        jobs = jobs or {}

        def opener(req, timeout=None):
            sent.append(req)
            url = req.full_url
            if url.startswith(se.GITHUB_API):
                if run_id_of(url):
                    reply = jobs.get(int(run_id_of(url)), jobs_reply())
                else:
                    reply = this_run if url.rsplit("/", 2)[-2:-1] == ["runs"] and "/actions/runs/" in url else runs_reply
                if isinstance(reply, BaseException):
                    raise reply
                return Resp(json.dumps(reply).encode())
            if url == se.GMAIL_TOKEN_URL:
                return Resp(json.dumps({"access_token": "at", "scope": se.SEND_SCOPE}).encode())
            if url == se.GMAIL_SEND_URL:
                return Resp(json.dumps({"id": "msg-1"}).encode())
            raise AssertionError("unexpected request " + url)

        path = os.path.join(self.tmp, "items.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"generated_at": "2026-11-11T13:46:00+00:00", "items": items, "sources": []}, f)
        learn = self.write_briefing(briefing) if briefing else os.path.join(self.tmp, "learn")

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        with mock.patch.dict(os.environ, dict(self.SECRETS, **(env or {})), clear=True), \
                mock.patch.object(se, "DOCS_DATA", path), \
                mock.patch.object(se, "LEARN_DIR", learn), \
                mock.patch.object(se, "datetime", FixedDatetime), \
                mock.patch("urllib.request.urlopen", side_effect=opener), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            se.main()
        raw = json.loads(sent[-1].data.decode("utf-8"))["raw"]
        parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)
        for secret in ("refresh-token-value", "client-secret-value", "github-token-value"):
            self.assertNotIn(secret, out.getvalue())
        return sent, out.getvalue(), parsed

    @staticmethod
    def text(msg):
        return msg.get_body(preferencelist=("plain",)).get_content()

    def test_main_without_github_covers_the_last_24_hours(self):
        now = datetime(2026, 11, 11, 13, 52, tzinfo=UTC)
        items = [item("old", published="2026-11-10T13:00:00+00:00"), item("new", published="2026-11-11T13:50:00+00:00")]
        sent, out, msg = self.run_main(items, now)
        self.assertEqual([r.full_url for r in sent], [se.GMAIL_TOKEN_URL, se.GMAIL_SEND_URL])
        self.assertEqual(sent[1].get_header("Authorization"), "Bearer at")
        self.assertIn("Email sent, id: msg-1", out)
        self.assertIn("so the digest ends now", out)
        self.assertIn("No GITHUB_TOKEN or GITHUB_REPOSITORY", out)
        self.assertIn("new", self.text(msg))
        self.assertNotIn("old", self.text(msg))
        self.assertIn("Published Tue Nov 10, 5:52 AM PST to Wed Nov 11, 5:52 AM PST", self.text(msg))

    def test_main_runs_from_the_previous_morning_run_to_this_one(self):
        items = [item("sent yesterday", published="2026-11-10T13:00:00+00:00"),
                 item("overnight", published="2026-11-11T09:00:00+00:00"),
                 item("after the build", published="2026-11-11T13:47:00+00:00"),
                 item("late", published="2026-11-10T13:20:00+00:00", first_seen="2026-11-10T20:00:00+00:00")]
        runs = {"workflow_runs": [
            {"id": 299, "display_title": "Daily digest (noon)", "created_at": "2026-11-10T20:00:00Z"},
            {"id": 298, "display_title": "Daily digest (morning)", "created_at": PREV_START.isoformat()},
        ]}
        now = datetime(2026, 11, 11, 13, 55, tzinfo=UTC)
        sent, out, msg = self.run_main(items, now, self.GITHUB, runs)
        self.assertEqual([r.full_url.split("?")[0] for r in sent], [
            se.GITHUB_API + "/repos/owner/news-digest/actions/runs/300",
            se.GITHUB_API + "/repos/owner/news-digest/actions/workflows/daily.yml/runs",
            se.GITHUB_API + "/repos/owner/news-digest/actions/runs/298/jobs",
            se.GMAIL_TOKEN_URL, se.GMAIL_SEND_URL])
        self.assertEqual(sent[0].get_header("Authorization"), "Bearer github-token-value")
        self.assertEqual(msg["Subject"], "News digest · Wednesday, November 11")
        text = self.text(msg)
        self.assertIn("Published Tue Nov 10, 5:45 AM PST to Wed Nov 11, 5:45 AM PST", text)
        self.assertIn("overnight", text)
        self.assertIn("- late\n", text)
        self.assertNotIn("sent yesterday", text)
        self.assertNotIn("after the build", text)
        self.assertIn("2 items published between 2026-11-10T13:45:40+00:00 and 2026-11-11T13:45:12+00:00", out)

    def test_main_covers_a_skipped_day(self):
        items = [item("skipped day", published="2026-11-09T22:00:00+00:00"),
                 item("today", published="2026-11-11T08:00:00+00:00")]
        runs = {"workflow_runs": [
            {"id": 280, "display_title": "Daily digest (morning)", "created_at": "2026-11-09T13:46:00Z"}]}
        _, _, msg = self.run_main(items, datetime(2026, 11, 11, 13, 55, tzinfo=UTC), self.GITHUB, runs)
        text = self.text(msg)
        self.assertIn("Published Mon Nov 9, 5:46 AM PST to Wed Nov 11, 5:45 AM PST", text)
        self.assertIn("skipped day", text)
        self.assertIn("today", text)

    def test_main_uses_24_hours_when_the_previous_run_is_unusable(self):
        items = [item("two days ago", published="2026-11-09T20:00:00+00:00"),
                 item("today", published="2026-11-11T08:00:00+00:00")]
        now = datetime(2026, 11, 11, 13, 55, tzinfo=UTC)
        too_old = {"workflow_runs": [{"id": 200, "display_title": "Daily digest (morning)",
                                      "created_at": "2026-11-03T13:45:00Z"}]}
        error = urllib.error.HTTPError("https://api.github.com/x", 500, "Server Error", {}, io.BytesIO(b""))
        for reply, note in ((too_old, "more than 7 days"), (error, "HTTP 500")):
            sent, out, msg = self.run_main(items, now, self.GITHUB, reply)
            text = self.text(msg)
            self.assertIn("Published Tue Nov 10, 5:45 AM PST to Wed Nov 11, 5:45 AM PST", text, note)
            self.assertIn("today", text)
            self.assertNotIn("two days ago", text)
            self.assertIn(note, out)
            self.assertIn("Email sent, id: msg-1", out)

    def test_a_rerun_of_a_failed_email_loses_nothing(self):
        # Day 1: the 05:45 email fails; "Re-run failed jobs" sends it at 22:00
        # UTC from the items.json the first attempt built, which has nothing
        # published after 05:45. "morning" was first seen by the noon run.
        day1 = [item("overnight", published="2026-11-11T09:00:00+00:00")]
        day2 = day1 + [item("morning", published="2026-11-11T16:00:00+00:00", first_seen="2026-11-11T20:00:10+00:00"),
                       item("evening", published="2026-11-12T02:00:00+00:00")]
        rerun = {"id": 300, "run_attempt": 2, "created_at": THIS_START.isoformat(),
                 "run_started_at": "2026-11-11T22:00:00Z"}
        runs = {"workflow_runs": [morning(298, PREV_START.isoformat())]}
        _, out, msg = self.run_main(day1, datetime(2026, 11, 11, 22, 1, tzinfo=UTC), self.GITHUB, runs, this_run=rerun)
        self.assertIn("Published Tue Nov 10, 5:45 AM PST to Wed Nov 11, 5:45 AM PST", self.text(msg))
        self.assertIn("overnight", self.text(msg))
        # Day 2: the previous digest is the re-run run, counted from when it was
        # created, so everything published since 05:45 on day 1 is listed.
        next_start = datetime(2026, 11, 12, 13, 45, 20, tzinfo=UTC)
        runs = {"workflow_runs": [dict(rerun, display_title="Daily digest (morning)", conclusion="success"),
                                  morning(298, PREV_START.isoformat())]}
        jobs = {300: {"jobs": [{"name": "email", "conclusion": "failure", "run_attempt": 1},
                               {"name": "email", "conclusion": "success", "run_attempt": 2}]}}
        _, out, msg = self.run_main(day2, datetime(2026, 11, 12, 13, 55, tzinfo=UTC),
                                    dict(self.GITHUB, GITHUB_RUN_ID="301"), runs,
                                    this_run={"id": 301, "created_at": next_start.isoformat()}, jobs=jobs)
        text = self.text(msg)
        self.assertIn("Published Wed Nov 11, 5:45 AM PST to Thu Nov 12, 5:45 AM PST", text)
        self.assertIn("- morning\n", text)
        self.assertIn("- evening\n", text)
        self.assertNotIn("overnight", text)

    def test_a_run_that_failed_after_its_email_is_not_repeated(self):
        # Yesterday's render failed, so the run ended as failure, but its
        # email went out: today's digest starts there, not a day earlier.
        items = [item("sent yesterday", published="2026-11-10T09:00:00+00:00"),
                 item("overnight", published="2026-11-11T09:00:00+00:00")]
        runs = {"workflow_runs": [morning(298, PREV_START.isoformat(), conclusion="failure"),
                                  morning(297, "2026-11-09T13:45:00Z", conclusion="success")]}
        now = datetime(2026, 11, 11, 13, 55, tzinfo=UTC)
        _, _, msg = self.run_main(items, now, self.GITHUB, runs, jobs={298: jobs_reply(render="failure")})
        self.assertIn("Published Tue Nov 10, 5:45 AM PST to Wed Nov 11, 5:45 AM PST", self.text(msg))
        self.assertNotIn("sent yesterday", self.text(msg))
        # Had the email itself failed, yesterday's items come again.
        _, _, msg = self.run_main(items, now, self.GITHUB, runs, jobs={298: jobs_reply(email="failure")})
        self.assertIn("Published Mon Nov 9, 5:45 AM PST to Wed Nov 11, 5:45 AM PST", self.text(msg))
        self.assertIn("sent yesterday", self.text(msg))

    def test_main_includes_todays_briefing(self):
        runs = {"workflow_runs": []}
        now = datetime(2026, 11, 11, 13, 55, tzinfo=UTC)
        _, _, msg = self.run_main([item()], now, dict(self.GITHUB, SITE_URL="https://news.example.com"), runs,
                                  briefing=briefing_doc())
        html_part = msg.get_body(preferencelist=("html",)).get_content()
        self.assertIn('href="https://news.example.com/learn/2026-11-11/"', html_part)
        self.assertIn("Slides: https://news.example.com/learn/2026-11-11/", self.text(msg))
        self.assertIn("It changes how loops are built.", self.text(msg))
        self.assertNotIn("More detail follows.", self.text(msg))
        # Without SITE_URL, the GitHub Pages address of the repository.
        _, _, msg = self.run_main([item()], now, self.GITHUB, runs, briefing=briefing_doc())
        self.assertIn("Slides: https://owner.github.io/news-digest/learn/2026-11-11/", self.text(msg))

    def test_briefing_date_is_the_local_date(self):
        # 03:00 UTC on Nov 12 is still Nov 11 in Los Angeles.
        now = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)
        _, _, msg = self.run_main([item()], now, briefing=briefing_doc())
        self.assertIn("Today's briefing (1)", self.text(msg))
        self.assertEqual(msg["Subject"], "News digest · Wednesday, November 11")

    def test_main_without_a_briefing_leaves_the_section_out(self):
        _, out, msg = self.run_main([item()], datetime(2026, 11, 11, 13, 55, tzinfo=UTC))
        self.assertNotIn("briefing", self.text(msg).lower())
        self.assertIn("No briefing for 2026-11-11", out)

    def test_main_says_when_it_used_the_committed_data(self):
        now = datetime(2026, 11, 11, 13, 55, tzinfo=UTC)
        _, _, msg = self.run_main([item()], now, {"DIGEST_DATA": "fallback"})
        for kind in ("plain", "html"):
            self.assertIn(se.FALLBACK_NOTE, msg.get_body(preferencelist=(kind,)).get_content())
        _, _, msg = self.run_main([item()], now)
        for kind in ("plain", "html"):
            self.assertNotIn("last committed data", msg.get_body(preferencelist=(kind,)).get_content())


class StdlibOnlyTests(unittest.TestCase):
    def test_send_email_imports_only_the_standard_library(self):
        path = os.path.join(support.ROOT, "scripts", "send_email.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add((node.module or "").split(".")[0])
        self.assertTrue(modules)
        self.assertEqual(modules - set(sys.stdlib_module_names), set())


if __name__ == "__main__":
    unittest.main()
