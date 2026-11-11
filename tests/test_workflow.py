"""Offline checks of .github/workflows/daily.yml: triggers, gates, permissions,
which jobs see which secrets, which jobs run on a morning or a noon run, and
the two shell steps that guard what is committed and published (run here with
bash and jq against temporary folders). Also worker/scheduler.js: static
checks, and its dispatch and retry behavior run in node with fetch mocked."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

import support

WORKFLOWS = os.path.join(support.ROOT, ".github", "workflows")
DAILY = os.path.join(WORKFLOWS, "daily.yml")
SCHEDULER = os.path.join(support.ROOT, "worker", "scheduler.js")
GMAIL_SECRETS = {"GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "SENDER_EMAIL", "RECIPIENT_EMAIL"}
JOBS = ("build", "brief", "render", "commit", "deploy", "email")
MORNING_ONLY = ("brief", "render", "email")


def load():
    with open(DAILY, encoding="utf-8") as f:
        text = f.read()
    return text, yaml.safe_load(text)


def strings(node):
    """Every string inside a parsed YAML node."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from strings(k)
            yield from strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from strings(v)


def step(job, name):
    for s in job["steps"]:
        if s.get("name") == name:
            return s
    raise AssertionError(f"no step named {name!r}")


class Ctx:
    """Attribute access for expression contexts; a missing key reads as ''."""

    def __init__(self, **values):
        self.__dict__.update(values)

    def __getattr__(self, name):
        return ""


def evaluate(expr, ctx):
    """A small evaluator for the if: expressions used in daily.yml: ==, &&,
    ||, !, string literals, contexts and the status functions."""
    expr = expr.strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2].strip()
    status_fns = ("success()", "failure()", "always()", "cancelled()")
    implicit = not any(fn in expr for fn in status_fns)
    py = expr.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    value = eval(py, {"__builtins__": {}}, ctx)  # noqa: S307  (our own workflow text)
    return bool(value) and (ctx["success"]() if implicit else True)


def simulate(doc, run="morning", enabled="true", ref="refs/heads/main", fail=(), candidates="success"):
    """Which jobs run and how they end, given that the jobs named in fail
    fail and every other job that runs succeeds."""
    results = {}
    for name in JOBS:  # the file lists the jobs in dependency order
        job = doc["jobs"][name]
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        outputs = {"build": Ctx(sha="abc", candidates=candidates if run == "morning" else "skipped")}
        need_ctx = Ctx(**{n: Ctx(result=results[n], outputs=outputs.get(n, Ctx())) for n in needs})
        ctx = {
            "vars": Ctx(DIGEST_ENABLED=enabled), "inputs": Ctx(run=run), "github": Ctx(ref=ref),
            "needs": need_ctx,
            "success": lambda: all(results[n] == "success" for n in needs),
            "cancelled": lambda: False, "always": lambda: True,
            "failure": lambda: any(results[n] == "failure" for n in needs),
        }
        if evaluate(job.get("if", "true"), ctx) if "if" in job else ctx["success"]():
            results[name] = "failure" if name in fail else "success"
        else:
            results[name] = "skipped"
    return {name: r for name, r in results.items() if r != "skipped"}


class TriggerAndGateTests(unittest.TestCase):
    def setUp(self):
        self.text, self.doc = load()

    def test_only_daily_yml_remains(self):
        self.assertEqual(sorted(os.listdir(WORKFLOWS)), ["daily.yml"])

    def test_trigger_is_workflow_dispatch_only(self):
        on = self.doc.get("on", self.doc.get(True))  # YAML 1.1 reads a bare on as true
        self.assertEqual(list(on), ["workflow_dispatch"])
        run = on["workflow_dispatch"]["inputs"]["run"]
        self.assertEqual((run["type"], run["options"], run["default"]), ("choice", ["morning", "noon"], "morning"))
        for word in ("pull_request", "pull_request_target", "schedule:", "push:", "workflow_run"):
            self.assertNotIn(word, self.text)
        self.assertEqual(self.doc["run-name"], "Daily digest (${{ inputs.run }})")

    def test_every_job_is_gated_on_digest_enabled(self):
        self.assertEqual(list(self.doc["jobs"]), list(JOBS))
        for name, job in self.doc["jobs"].items():
            self.assertIn("vars.DIGEST_ENABLED == 'true'", job["if"], name)
        self.assertNotIn("github.repository ==", self.text)
        self.assertEqual(simulate(self.doc, enabled=""), {})
        self.assertEqual(simulate(self.doc, enabled="false", run="noon"), {})

    def test_morning_runs_every_job(self):
        self.assertEqual(simulate(self.doc), dict.fromkeys(JOBS, "success"))

    def test_noon_skips_brief_render_and_email(self):
        self.assertEqual(simulate(self.doc, run="noon"), {"build": "success", "commit": "success", "deploy": "success"})
        for name in MORNING_ONLY:
            self.assertIn("inputs.run == 'morning'", self.doc["jobs"][name]["if"], name)
        for name in ("build", "commit", "deploy"):
            self.assertNotIn("inputs.run", self.doc["jobs"][name]["if"], name)
        self.assertEqual(step(self.doc["jobs"]["build"], "Pick the learning candidates")["if"], "inputs.run == 'morning'")

    def test_candidates_cannot_stop_the_build(self):
        candidates = step(self.doc["jobs"]["build"], "Pick the learning candidates")
        self.assertIs(candidates["continue-on-error"], True)
        # Feeds (6 minutes at most), candidates, and 2 minutes to set up and upload.
        self.assertLessEqual(candidates["timeout-minutes"] + 6 + 2, self.doc["jobs"]["build"]["timeout-minutes"])

    def test_email_waits_for_the_commit_but_not_for_the_deploy(self):
        jobs = self.doc["jobs"]
        self.assertEqual(jobs["email"]["needs"], ["build", "commit"])
        self.assertEqual(jobs["deploy"]["needs"], "commit")
        for name, job in jobs.items():
            needs = job.get("needs") or []
            self.assertNotIn("email", [needs] if isinstance(needs, str) else needs, name)
        got = simulate(self.doc, fail=("deploy",))
        self.assertEqual((got["deploy"], got["email"]), ("failure", "success"))

    def test_the_email_job_is_the_one_send_email_looks_for(self):
        # send_email.py finds the previous digest by the name of this job in
        # GET /actions/runs/{id}/jobs, which is the key unless the job has a name.
        import send_email
        self.assertEqual(send_email.EMAIL_JOB, "email")
        self.assertNotIn("name", self.doc["jobs"]["email"])
        self.assertEqual(send_email.WORKFLOW_FILE, os.path.basename(DAILY))
        self.assertIn(send_email.MORNING_MARK, self.doc["run-name"].replace("${{ inputs.run }}", "morning"))


def limits(script):
    """The (seconds, kill-after seconds) of each timeout command in a step."""
    return [(int(n), int(k)) for k, n in re.findall(r"timeout --kill-after=(\d+) (\d+) python ", script)]


class SixOClockTests(unittest.TestCase):
    """The morning email is meant to arrive by 6:00: 15 minutes after 5:45."""

    SETUP = 60      # checkout, setup-python and pip install in build (cached), plus the uploads
    BRIEF_SETUP = 35  # checkout, setup-python, the SDK's wheels and the artifact
    RENDER = 80
    COMMIT = 25
    EMAIL = 35
    PICKUP = 10     # a runner taking a job

    def setUp(self):
        self.text, self.doc = load()
        self.jobs = self.doc["jobs"]

    def test_job_timeouts(self):
        self.assertLessEqual(self.jobs["brief"]["timeout-minutes"], 6)
        self.assertLessEqual(self.jobs["render"]["timeout-minutes"], 5)
        self.assertLessEqual(self.jobs["commit"]["timeout-minutes"], 5)
        self.assertLessEqual(self.jobs["email"]["timeout-minutes"], 5)
        self.assertLessEqual(step(self.jobs["build"], "Pick the learning candidates")["timeout-minutes"], 2)

    def test_morning_feeds_get_a_shorter_budget(self):
        env = step(self.jobs["build"], "Fetch and build")["env"]
        self.assertEqual(env, {"FEED_RUN_SECONDS": "${{ inputs.run == 'morning' && '180' || '' }}"})

    def test_slow_steps_are_limited_inside_the_step_and_fall_back(self):
        candidates = step(self.jobs["build"], "Pick the learning candidates")
        got = limits(candidates["run"])
        self.assertEqual(len(got), 2)
        self.assertLessEqual(sum(n + k for n, k in got), candidates["timeout-minutes"] * 60)
        self.assertIn("candidates.py --no-fetch --out", candidates["run"])
        brief = step(self.jobs["brief"], "Write the briefing")
        got = limits(brief["run"])
        self.assertEqual(len(got), 2)
        self.assertLessEqual(self.BRIEF_SETUP + sum(n + k for n, k in got), self.jobs["brief"]["timeout-minutes"] * 60)
        self.assertIn('ANTHROPIC_API_KEY="" timeout', brief["run"])
        # No timeout-minutes on this step: it would end the step as a failure
        # with no briefing, where the limits inside it end in the fallback.
        self.assertNotIn("timeout-minutes", brief)

    def test_the_worst_morning_still_sends_by_six(self):
        feeds = int(re.search(r"'(\d+)'", step(self.jobs["build"], "Fetch and build")["env"]["FEED_RUN_SECONDS"]).group(1))
        candidates = sum(n + k for n, k in limits(step(self.jobs["build"], "Pick the learning candidates")["run"]))
        brief = sum(n + k for n, k in limits(step(self.jobs["brief"], "Write the briefing")["run"]))
        chain = ["build", "brief", "render", "commit", "email"]  # what the email waits for, in order
        needs = {name: self.jobs[name].get("needs") or [] for name in chain}
        for earlier, later in zip(chain, chain[1:]):
            self.assertIn(earlier, needs[later] if isinstance(needs[later], list) else [needs[later]])
        total = (self.PICKUP * len(chain) + self.SETUP + feeds + candidates + self.BRIEF_SETUP + brief
                 + self.RENDER + self.COMMIT + self.EMAIL)
        self.assertLessEqual(total, 15 * 60, f"{total} seconds from 5:45")

    def test_failures_degrade_without_stopping_the_feeds_or_the_email(self):
        got = simulate(self.doc, candidates="failure")
        self.assertEqual(set(got), {"build", "commit", "deploy", "email"})
        got = simulate(self.doc, fail=("render",))
        self.assertEqual(got, {"build": "success", "brief": "success", "render": "failure",
                               "commit": "success", "deploy": "success", "email": "success"})
        got = simulate(self.doc, fail=("brief",))
        self.assertEqual(got, {"build": "success", "brief": "failure", "commit": "success", "deploy": "success",
                               "email": "success"})
        got = simulate(self.doc, fail=("build",))
        self.assertEqual(got, {"build": "failure", "email": "success"})  # sends from the committed data
        got = simulate(self.doc, fail=("commit",))
        self.assertEqual(set(got), {"build", "brief", "render", "commit", "email"})

    def test_other_branches_never_write_send_or_see_secrets(self):
        self.assertEqual(simulate(self.doc, ref="refs/heads/feature"), {"build": "success"})
        self.assertEqual(simulate(self.doc, ref="refs/heads/feature", run="noon"), {"build": "success"})


class PermissionAndSecretTests(unittest.TestCase):
    def setUp(self):
        self.text, self.doc = load()
        self.jobs = self.doc["jobs"]

    def test_permissions(self):
        self.assertEqual(self.doc["permissions"], {})
        expected = {
            "build": {"contents": "read"},
            "brief": {"contents": "read"},
            "render": {"contents": "read"},
            "commit": {"contents": "write"},
            "deploy": {"contents": "read", "pages": "write", "id-token": "write"},
            "email": {"contents": "read", "actions": "read"},
        }
        self.assertEqual({n: j["permissions"] for n, j in self.jobs.items()}, expected)

    def test_secrets_reach_one_step_of_brief_and_email_only(self):
        self.assertNotIn("secrets: inherit", self.text)
        for name, job in self.jobs.items():
            refs = set(re.findall(r"secrets\.([A-Z_]+)", " ".join(strings(job))))
            want = {"brief": {"ANTHROPIC_API_KEY"}, "email": GMAIL_SECRETS}.get(name, set())
            self.assertEqual(refs, want, name)
        brief = step(self.jobs["brief"], "Write the briefing")
        self.assertEqual(brief["env"], {"ANTHROPIC_API_KEY": "${{ secrets.ANTHROPIC_API_KEY }}"})
        send = step(self.jobs["email"], "Send digest")
        self.assertEqual({k for k, v in send["env"].items() if "secrets." in v}, GMAIL_SECRETS)
        for name in ("brief", "email"):
            with_secrets = [s for s in self.jobs[name]["steps"] if "secrets." in " ".join(strings(s))]
            self.assertEqual(len(with_secrets), 1, name)
        self.assertEqual(self.text.count("ANTHROPIC_API_KEY: ${{"), 1)

    def test_environments(self):
        self.assertEqual(self.jobs["brief"]["environment"], "digest")
        self.assertEqual(self.jobs["email"]["environment"], "digest")
        self.assertEqual(self.jobs["deploy"]["environment"]["name"], "github-pages")
        for name in ("build", "render", "commit"):
            self.assertNotIn("environment", self.jobs[name], name)
        for name in ("brief", "email"):
            self.assertIn("github.ref == 'refs/heads/main'", self.jobs[name]["if"], name)

    def test_only_the_email_step_gets_the_actions_token(self):
        self.assertEqual(self.text.count("GITHUB_TOKEN:"), 1)
        self.assertEqual(step(self.jobs["email"], "Send digest")["env"]["GITHUB_TOKEN"], "${{ github.token }}")

    def test_credentials_are_not_left_on_disk_except_for_the_push(self):
        for name, job in self.jobs.items():
            for s in job["steps"]:
                if str(s.get("uses", "")).startswith("actions/checkout@"):
                    persist = (s.get("with") or {}).get("persist-credentials")
                    self.assertEqual(persist, None if name == "commit" else False, name)


class ThirdPartyCodeTests(unittest.TestCase):
    def setUp(self):
        self.text, self.doc = load()
        self.jobs = self.doc["jobs"]

    def runs(self, name):
        return "\n".join(s.get("run", "") for s in self.jobs[name]["steps"])

    def uses(self, name):
        return {s["uses"].split("@")[0] for s in self.jobs[name]["steps"] if "uses" in s}

    def test_brief_installs_only_the_brief_lock(self):
        runs = self.runs("brief")
        self.assertIn("pip install --require-hashes --only-binary :all: -r requirements-brief.lock", runs)
        self.assertEqual(runs.count("pip install"), 1)
        self.assertNotRegex(runs, r"-r requirements(-build)?\.lock")
        self.assertIn('python scripts/learn/brief.py "${args[@]}"', runs)
        self.assertIn('args=(--candidates "$RUNNER_TEMP/learn/candidates.json" --out "$RUNNER_TEMP/learn/briefing.json")', runs)

    def test_build_and_render_use_the_main_lock(self):
        for name in ("build", "render"):
            runs = self.runs(name)
            self.assertIn("pip install --require-hashes -r requirements-build.lock", runs)
            self.assertIn("pip install --require-hashes --no-build-isolation -r requirements.lock", runs)

    def test_commit_and_email_run_no_package(self):
        self.assertEqual(self.uses("commit"), {"actions/checkout", "actions/download-artifact"})
        self.assertEqual(self.uses("email"), {"actions/checkout", "actions/download-artifact"})
        for name in ("commit", "email"):
            self.assertNotRegex(self.runs(name), r"\bpip3?\s")
        self.assertNotIn("python", self.runs("commit"))
        self.assertIn("python3 -I scripts/send_email.py", self.runs("email"))
        self.assertIn('echo "DIGEST_DATA=fallback" >> "$GITHUB_ENV"', self.runs("email"))

    def test_deploy_uses_the_pages_actions(self):
        self.assertEqual(self.uses("deploy"), {"actions/checkout", "actions/configure-pages",
                                               "actions/upload-pages-artifact", "actions/deploy-pages"})
        self.assertEqual(step(self.jobs["deploy"], "Build the site")["env"], {"WORKER_URL": "${{ vars.WORKER_URL }}"})
        self.assertIn("^https://[A-Za-z0-9.-]+$", self.runs("deploy"))

    def test_copy_never_takes_html_from_an_artifact(self):
        script = step(self.jobs["commit"], "Copy the checked files into the repository")["run"]
        for line in script.splitlines():
            if line.strip().startswith("cp "):
                self.assertNotRegex(line, r"\$LEARN\S*\.html", line)
        self.assertIn('cp docs/learn/viewer.html "docs/learn/$day/index.html"', script)

    def test_copy_in_the_log_is_plain(self):
        for s in strings(self.doc):
            self.assertNotIn(chr(0x2014), s)
            self.assertNotIn(chr(0x2013), s)

    def test_messages_in_the_log_are_sentence_case(self):
        messages = re.findall(r"::(?:error|warning)::([^\"]+)", self.text)
        messages += re.findall(r'echo "([^"]+)"; return 1', self.text)
        messages += [m for m in re.findall(r'echo "([^":$][^"]*)"\n', self.text)]
        self.assertGreaterEqual(len(messages), 20)
        for message in messages:
            first = message.split()[0]
            # A sentence starts with a capital, a file name or a shell variable.
            self.assertTrue(first[0].isupper() or first[0] == "$" or re.search(r"[./]\w", first), message)


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "needs bash and jq")
class ShellStepTests(unittest.TestCase):
    """Runs the two guarding steps the way GitHub does (bash -eo pipefail)."""

    def setUp(self):
        _, doc = load()
        self.jobs = doc["jobs"]
        self.tmp = tempfile.mkdtemp(prefix="news-digest-wf-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))

    def read(self, rel):
        with open(os.path.join(self.repo, rel), "rb") as f:
            return f.read()

    def bash(self, script, env):
        path = os.path.join(self.tmp, "step.sh")
        with open(path, "w", encoding="utf-8") as f:
            f.write(script)
        full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "RUNNER_TEMP": self.tmp, **env}
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", path], cwd=self.repo,
                              env=full, capture_output=True, text=True, timeout=60)

    # ------------------------------------------------ commit: copy the checked files

    DAY = "2026-11-11"
    VIEWER = b"<!doctype html><title>viewer</title>"

    def artifacts(self, learn=True, **override):
        data = os.path.join(self.tmp, "data")
        files = {
            "docs/data/items.json": '{"items": []}',
            "docs/data/twitter_posts.json": '{"accounts": []}',
            "data/youtube_channels.json": "{}",
        }
        learn_dir = os.path.join(self.tmp, "learn-out")
        learn_files = {
            "docs/learn/index.json": '{"latest": "%s", "days": [{"date": "%s", "topics": ["A"]}]}' % (self.DAY, self.DAY),
            f"docs/learn/{self.DAY}/briefing.json": '{"date": "%s", "topics": []}' % self.DAY,
            f"docs/learn/{self.DAY}/briefing.pptx": b"PK\x03\x04rest-of-zip",
            f"docs/learn/{self.DAY}/index.html": "<script>alert(1)</script>",  # must never be copied
            "learn/seen.json": '{"version": 1, "topics": []}',
            "docs/evil.html": "<script>alert(2)</script>",
        }
        for rel, body in files.items():
            if override.get(rel, "") is not None:
                self.write(os.path.join(data, rel), override.get(rel, body))
        if learn:
            for rel, body in learn_files.items():
                if override.get(rel, "") is not None:
                    self.write(os.path.join(learn_dir, rel), override.get(rel, body))
        self.write(os.path.join(self.repo, "docs/learn/viewer.html"), self.VIEWER)
        return {"DATA": data, "LEARN": learn_dir}

    def copy(self, env):
        script = step(self.jobs["commit"], "Copy the checked files into the repository")["run"]
        res = self.bash(script, env)
        listed = []
        if os.path.exists(os.path.join(self.tmp, "commit-files.txt")):
            with open(os.path.join(self.tmp, "commit-files.txt"), encoding="utf-8") as f:
                listed = f.read().split()
        return res, listed

    # ------------------------------------------------ build: slow steps and the data artifact

    def fake_python(self, mode):
        """A python on PATH that records each call and plays brief.py or
        candidates.py: mode "ok" writes --out; "hang" sleeps on the first form
        of the call (with the key, or with the lane sources) and writes --out
        on the fallback form; "fail" exits 3 on the first form; "broken"
        exits 3 always."""
        folder = os.path.join(self.tmp, "bin")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "python")
        with open(path, "w", encoding="utf-8") as f:
            f.write("""#!/bin/bash
first=yes
case " $* " in *" --no-fetch "*) first=no ;; esac
case "$1" in *brief.py) [ -n "${ANTHROPIC_API_KEY:-}" ] || first=no ;; esac
echo "$first $*" >> "$CALLS"
out=""
while [ $# -gt 0 ]; do if [ "$1" = "--out" ]; then out="$2"; fi; shift; done
if [ "$MODE" = broken ]; then exit 3; fi
if [ "$first" = yes ]; then
  if [ "$MODE" = hang ]; then exec sleep 30; fi
  if [ "$MODE" = fail ]; then exit 3; fi
fi
echo "{\\"first\\": \\"$first\\"}" > "$out"
""")
        os.chmod(path, 0o755)
        calls = os.path.join(self.tmp, "calls.txt")
        return {"PATH": folder + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"), "MODE": mode, "CALLS": calls}

    def slow_step(self, job, name, mode, **env):
        script = step(self.jobs[job], name)["run"]
        # One second in place of the real limit, so a hung run is cut off quickly.
        script, changed = re.subn(r"(timeout --kill-after=\d+) \d+ (python )", r"\1 1 \2", script, count=1)
        self.assertEqual(changed, 1)
        fake = self.fake_python(mode)
        if os.path.exists(fake["CALLS"]):
            os.remove(fake["CALLS"])
        res = self.bash(script, {**fake, **env})
        calls = []
        if os.path.exists(fake["CALLS"]):
            with open(fake["CALLS"], encoding="utf-8") as f:
                calls = [line.split()[0] for line in f.read().splitlines()]
        return res, calls

    def learn_file(self, name):
        path = os.path.join(self.tmp, "learn", name)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_candidates_fall_back_to_the_feeds_alone(self):
        res, calls = self.slow_step("build", "Pick the learning candidates", "ok")
        self.assertEqual((res.returncode, calls), (0, ["yes"]), res.stdout + res.stderr)
        self.assertEqual(self.learn_file("candidates.json"), {"first": "yes"})
        for mode in ("hang", "fail"):  # lane sources too slow, or a crash
            res, calls = self.slow_step("build", "Pick the learning candidates", mode)
            self.assertEqual((res.returncode, calls), (0, ["yes", "no"]), mode + res.stdout + res.stderr)
            self.assertEqual(self.learn_file("candidates.json"), {"first": "no"})
            self.assertIn("::warning::The lane sources took too long", res.stdout)
        res, calls = self.slow_step("build", "Pick the learning candidates", "broken")
        self.assertNotEqual(res.returncode, 0)  # the step may fail: continue-on-error
        self.assertIsNone(self.learn_file("candidates.json"))

    def test_a_slow_model_gets_the_headline_briefing_instead(self):
        os.makedirs(os.path.join(self.tmp, "learn"))
        res, calls = self.slow_step("brief", "Write the briefing", "ok", ANTHROPIC_API_KEY="key")
        self.assertEqual((res.returncode, calls), (0, ["yes"]), res.stdout + res.stderr)
        for mode in ("hang", "fail"):
            res, calls = self.slow_step("brief", "Write the briefing", mode, ANTHROPIC_API_KEY="key")
            self.assertEqual((res.returncode, calls), (0, ["yes", "no"]), mode + res.stdout + res.stderr)
            self.assertEqual(self.learn_file("briefing.json"), {"first": "no"})  # written without the key
            self.assertIn("::warning::The model did not finish the briefing in time", res.stdout)
        res, calls = self.slow_step("brief", "Write the briefing", "broken", ANTHROPIC_API_KEY="key")
        self.assertNotEqual(res.returncode, 0)

    def stage(self, files):
        for rel in files:
            self.write(os.path.join(self.repo, rel), "{}")
        res = self.bash(step(self.jobs["build"], "Stage the data files")["run"], {})
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        root = os.path.join(self.tmp, "digest-data")
        return sorted(os.path.relpath(os.path.join(folder, name), root)
                      for folder, _, names in os.walk(root) for name in names)

    def test_the_data_artifact_keeps_its_layout_when_a_file_is_missing(self):
        upload = step(self.jobs["build"], "Upload data files")["with"]
        # One folder, not a list of files: upload-artifact roots a list at the
        # folder the files share, which is docs/data without youtube_channels.json.
        self.assertEqual(upload["path"], "${{ runner.temp }}/digest-data")
        self.assertEqual(upload["if-no-files-found"], "error")
        both = ["docs/data/items.json", "docs/data/twitter_posts.json"]
        self.assertEqual(self.stage(both), both)
        self.assertEqual(self.stage(both + ["data/youtube_channels.json"]),
                         ["data/youtube_channels.json"] + both)
        # What commit and email read from the downloaded artifact is there.
        res, listed = self.copy({"DATA": os.path.join(self.tmp, "digest-data"), "LEARN": os.path.join(self.tmp, "none")})
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(sorted(listed), ["data/youtube_channels.json"] + both)
        self.assertIn("digest-data/docs/data/items.json", step(self.jobs["email"],
                      "Use the refreshed items.json when there is one")["env"]["SRC"])

    def test_staging_starts_clean_and_skips_links(self):
        self.write(os.path.join(self.tmp, "digest-data", "old.json"), "{}")
        os.makedirs(os.path.join(self.repo, "docs/data"))
        os.symlink("/etc/passwd", os.path.join(self.repo, "docs/data/twitter_posts.json"))
        self.assertEqual(self.stage(["docs/data/items.json"]), ["docs/data/items.json"])

    def test_noon_copies_the_data_files(self):
        res, listed = self.copy(self.artifacts(learn=False))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(listed, ["docs/data/items.json", "docs/data/twitter_posts.json", "data/youtube_channels.json"])
        self.assertEqual(self.read("docs/data/items.json"), b'{"items": []}')

    def test_morning_copies_the_briefing_and_the_repository_viewer(self):
        res, listed = self.copy(self.artifacts())
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(listed[3:], ["docs/learn/index.json", f"docs/learn/{self.DAY}/briefing.json",
                                      f"docs/learn/{self.DAY}/briefing.pptx", f"docs/learn/{self.DAY}/index.html",
                                      "learn/seen.json"])
        self.assertEqual(self.read(f"docs/learn/{self.DAY}/index.html"), self.VIEWER)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "docs/evil.html")))
        self.assertEqual(self.read(f"docs/learn/{self.DAY}/briefing.pptx"), b"PK\x03\x04rest-of-zip")

    def test_bad_data_files_stop_the_commit(self):
        cases = [
            ({"docs/data/items.json": "[1, 2]"}, "not a JSON object"),
            ({"docs/data/items.json": "{broken"}, "not a JSON object"),
            ({"docs/data/items.json": None, "docs/data/twitter_posts.json": None,
              "data/youtube_channels.json": None}, "none of the expected data files"),
        ]
        for override, message in cases:
            with self.subTest(message=message):
                shutil.rmtree(self.tmp)
                os.makedirs(self.repo)
                res, _ = self.copy(self.artifacts(learn=False, **override))
                self.assertEqual(res.returncode, 1)
                self.assertIn(message, res.stdout)

    def test_a_link_in_the_artifact_is_refused(self):
        env = self.artifacts(learn=False)
        target = os.path.join(env["DATA"], "docs/data/items.json")
        os.remove(target)
        os.symlink("/etc/passwd", target)
        res, _ = self.copy(env)
        self.assertEqual(res.returncode, 1)
        self.assertIn("not a regular file", res.stdout)

    def test_bad_briefing_files_are_left_out_but_the_data_is_kept(self):
        day = self.DAY
        cases = [
            ({f"docs/learn/{day}/briefing.pptx": b"<html>not a zip"}, "not a zip file"),
            ({f"docs/learn/{day}/briefing.json": '{"date": "2026-11-10", "topics": []}'}, "not the briefing of"),
            ({"docs/learn/index.json": '{"latest": "../../x", "days": []}'}, "not a date"),
            ({"docs/learn/index.json": '{"latest": null, "days": []}'}, "no latest day"),
            ({"learn/seen.json": "[]"}, "not a JSON object"),
            ({f"docs/learn/{day}/briefing.pptx": None}, "not a regular file"),
        ]
        for override, message in cases:
            with self.subTest(message=message):
                shutil.rmtree(self.tmp)
                os.makedirs(self.repo)
                res, listed = self.copy(self.artifacts(**override))
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertIn("::warning::Briefing files left out", res.stdout)
                self.assertIn(message, res.stdout)
                self.assertEqual(len(listed), 3)
                self.assertFalse(os.path.exists(os.path.join(self.repo, "docs/learn/index.json")))

    def test_oversized_slides_and_a_missing_viewer_are_left_out(self):
        env = self.artifacts()
        with open(os.path.join(env["LEARN"], f"docs/learn/{self.DAY}/briefing.pptx"), "r+b") as f:
            f.truncate(20_000_001)
        res, listed = self.copy(env)
        self.assertIn("larger than 20000000 bytes", res.stdout)
        self.assertEqual(len(listed), 3)
        shutil.rmtree(self.tmp)
        os.makedirs(self.repo)
        env = self.artifacts()
        os.remove(os.path.join(self.repo, "docs/learn/viewer.html"))
        res, listed = self.copy(env)
        self.assertIn("viewer.html in the repository is not a regular file", res.stdout)
        self.assertEqual(len(listed), 3)

    # ------------------------------------------------ commit: commit and push

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_commit_adds_only_the_listed_files_and_pushes(self):
        isolated = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "HOME": self.tmp}

        def git(*args, cwd=None):
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **isolated}
            return subprocess.run(["git", *args], cwd=cwd or self.repo, env=env, check=True,
                                  capture_output=True, text=True).stdout.strip()

        origin = os.path.join(self.tmp, "origin.git")
        git("init", "-q", "--bare", "-b", "main", origin, cwd=self.tmp)
        shutil.rmtree(self.repo)
        git("clone", "-q", origin, self.repo, cwd=self.tmp)
        self.write(os.path.join(self.repo, "docs/data/items.json"), '{"items": []}')
        self.write(os.path.join(self.repo, "README.md"), "readme")
        git("add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "start")
        git("push", "-q", "origin", "HEAD:main")

        script = step(self.jobs["commit"], "Commit and push if changed")["run"]
        output = os.path.join(self.tmp, "github_output")
        listed = os.path.join(self.tmp, "commit-files.txt")
        env = {**isolated, "RUN": "morning", "GITHUB_OUTPUT": output}

        # Nothing changed: no commit, and the sha is the current one.
        with open(listed, "w", encoding="utf-8") as f:
            f.write("docs/data/items.json\n")
        res = self.bash(script, env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("No changes.", res.stdout)
        with open(output, encoding="utf-8") as f:
            self.assertEqual(f.read(), f"sha={git('rev-parse', 'HEAD')}\n")

        # Listed files are committed with a message that names the briefing;
        # a changed file that is not listed stays out.
        self.write(os.path.join(self.repo, "docs/data/items.json"), '{"items": [1]}')
        self.write(os.path.join(self.repo, f"docs/learn/{self.DAY}/briefing.json"), "{}")
        self.write(os.path.join(self.repo, "README.md"), "changed by something else")
        with open(listed, "w", encoding="utf-8") as f:
            f.write(f"docs/data/items.json\ndocs/learn/{self.DAY}/briefing.json\n")
        os.remove(output)
        res = self.bash(script, env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(git("log", "-1", "--format=%s", "main", cwd=origin),
                         "update items.json and the briefing (morning run)")
        self.assertEqual(git("show", "--name-only", "--format=", "main", cwd=origin).split(),
                         ["docs/data/items.json", f"docs/learn/{self.DAY}/briefing.json"])
        with open(output, encoding="utf-8") as f:
            self.assertEqual(f.read(), f"sha={git('rev-parse', 'main', cwd=origin)}\n")

        # A push that reached main meanwhile is rebased onto, not overwritten.
        other = os.path.join(self.tmp, "other")
        git("clone", "-q", origin, other, cwd=self.tmp)
        self.write(os.path.join(other, "notes.txt"), "pushed meanwhile")
        git("add", "notes.txt", cwd=other)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "meanwhile", cwd=other)
        git("push", "-q", "origin", "HEAD:main", cwd=other)
        self.write(os.path.join(self.repo, "docs/data/items.json"), '{"items": [2]}')
        with open(listed, "w", encoding="utf-8") as f:
            f.write("docs/data/items.json\n")
        res = self.bash(script, dict(env, RUN="noon"))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(git("log", "--format=%s", "-3", "main", cwd=origin).splitlines(),
                         ["update items.json (noon run)", "meanwhile", "update items.json and the briefing (morning run)"])

    # ------------------------------------------------ deploy: build the site

    CSP = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'self'; "
           "connect-src 'self'; object-src 'none'\">")

    def site(self, worker_url, csp=CSP):
        self.write(os.path.join(self.repo, "docs/index.html"), f"<!doctype html><head>{csp}</head>")
        self.write(os.path.join(self.repo, "docs/learn/viewer.html"), self.VIEWER)
        self.write(os.path.join(self.repo, f"docs/learn/{self.DAY}/index.html"), self.VIEWER)
        self.write(os.path.join(self.repo, "docs/learn/index.json"), "{}")
        self.write(os.path.join(self.repo, "docs/assets/js/config.js"), 'window.NEWS_DIGEST_CONFIG = { workerUrl: "" };\n')
        script = step(self.jobs["deploy"], "Build the site")["run"]
        res = self.bash(script, {"WORKER_URL": worker_url})
        config_js = self.read("_site/assets/js/config.js").decode() if res.returncode == 0 else ""
        index = self.read("_site/index.html").decode() if res.returncode == 0 else ""
        return res, config_js, index

    def test_worker_url_is_stamped_into_the_published_copy_only(self):
        for given in ("https://news-extract.example.workers.dev", "https://news-extract.example.workers.dev/"):
            res, config_js, index = self.site(given)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertEqual(config_js, 'window.NEWS_DIGEST_CONFIG = { workerUrl: "https://news-extract.example.workers.dev" };\n')
            self.assertIn("connect-src 'self' https://news-extract.example.workers.dev;", index)
            self.assertNotIn("workers.dev", self.read("docs/index.html").decode())
            self.assertNotIn("workers.dev", self.read("docs/assets/js/config.js").decode())

    def test_empty_or_bad_worker_url_turns_summaries_off(self):
        for given in ("", 'https://evil.example/"; alert(1); "', "http://plain.example", "https://a.example/path",
                      "https://a.example connect-src *", "https://a.example\n"):
            with self.subTest(given=given):
                res, config_js, index = self.site(given)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertEqual(config_js, 'window.NEWS_DIGEST_CONFIG = { workerUrl: "" };\n')
                self.assertIn("connect-src 'self'; object-src", index)
                if given:
                    self.assertIn("::warning::WORKER_URL must be", res.stdout)

    def test_the_viewer_template_is_not_published(self):
        for worker_url in ("https://w.example.workers.dev", ""):  # the step ends early without a Worker
            res, _, _ = self.site(worker_url)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertFalse(os.path.exists(os.path.join(self.repo, "_site/learn/viewer.html")))
            self.assertEqual(self.read(f"_site/learn/{self.DAY}/index.html"), self.VIEWER)  # the day's copy stays
            self.assertEqual(self.read("_site/learn/index.json"), b"{}")
            self.assertEqual(self.read("docs/learn/viewer.html"), self.VIEWER)  # the repository keeps the template

    def test_unexpected_csp_stops_the_deploy(self):
        res, _, _ = self.site("https://w.example.workers.dev", csp="<meta content=\"default-src 'self'\">")
        self.assertEqual(res.returncode, 1)
        self.assertIn("connect-src 'self' exactly once", res.stdout)


class EnvExampleTests(unittest.TestCase):
    """.env.example is committed: it is the list of setting names for a copier."""

    def test_gitignore_keeps_env_files_out_but_the_example_in(self):
        with open(os.path.join(support.ROOT, ".gitignore"), encoding="utf-8") as f:
            lines = [line.strip() for line in f]
        self.assertIn(".env", lines)
        self.assertLess(lines.index(".env.*"), lines.index("!.env.example"))  # the exception must come after

    @unittest.skipUnless(shutil.which("git") and os.path.isdir(os.path.join(support.ROOT, ".git")), "needs a git checkout")
    def test_git_does_not_ignore_the_example(self):
        def ignored(name):
            return subprocess.run(["git", "check-ignore", "-q", name], cwd=support.ROOT).returncode == 0

        self.assertFalse(ignored(".env.example"))
        for name in (".env", ".env.local", ".dev.vars"):
            self.assertTrue(ignored(name), name)

    def test_the_example_lists_names_without_values(self):
        with open(os.path.join(support.ROOT, ".env.example"), encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        names = [line[:-1] for line in lines]
        self.assertTrue(all(re.fullmatch(r"[A-Z_]+=", line) for line in lines), lines)
        for name in ("DIGEST_ENABLED", "WORKER_URL", "ANTHROPIC_API_KEY", "GMAIL_REFRESH_TOKEN", "ACCESS_KEY",
                     "ALLOWED_ORIGINS", "GH_DISPATCH_TOKEN", "GH_REPO", "FEED_RUN_SECONDS"):
            self.assertIn(name, names)


class SchedulerStaticTests(unittest.TestCase):
    def setUp(self):
        with open(SCHEDULER, encoding="utf-8") as f:
            self.text = f.read()

    def test_block_to_paste(self):
        self.assertNotRegex(self.text, r"(?m)^\s*export\b")
        self.assertIn("async function dispatchDigest(event, env, ctx)", self.text)
        self.assertIn("45 12,13 * * *", self.text)
        self.assertIn("0 19,20 * * *", self.text)
        self.assertIn("export default { ...original, fetch: protect(", self.text)
        self.assertIn("scheduled: dispatchDigest }", self.text)
        self.assertIn('const DIGEST_WORKFLOW = "daily.yml";', self.text)
        self.assertIn('const DIGEST_BRANCH = "main";', self.text)
        self.assertIn("https://api.github.com/repos/${repo}/actions/workflows/${DIGEST_WORKFLOW}/dispatches", self.text)
        self.assertIn('const DIGEST_RUNS = { "05:45": "morning", "12:00": "noon" };', self.text)
        for header in ("Authorization", "application/vnd.github+json", "X-GitHub-Api-Version", "2022-11-28",
                       "news-digest-scheduler"):
            self.assertIn(header, self.text)

    def test_token_is_never_logged(self):
        for line in self.text.splitlines():
            if "console." in line or "new Error(" in line:
                self.assertNotIn("GH_DISPATCH_TOKEN}", line)
                self.assertNotIn("headers", line)

    def test_plain_punctuation(self):
        self.assertNotIn(chr(0x2014), self.text)
        self.assertNotIn(chr(0x2013), self.text)

    def test_log_and_error_lines_are_sentence_case(self):
        messages = re.findall(r"(?:console\.log|new Error)\(\s*[`\"]([^`\"]*)", self.text)
        self.assertGreaterEqual(len(messages), 6)
        for message in messages:
            self.assertTrue(message.startswith("News digest: "), message)
        self.assertNotRegex(self.text, r"[`\"]news digest:")


# Runs dispatchDigest in node with fetch, setTimeout and console replaced, one
# scenario at a time, and prints what each one did as JSON. posts and lists are
# the answers to the dispatch requests and to the reads of the list of runs, in
# order: an HTTP status, "network" for a request that throws, or (lists only)
# the runs to return, each [seconds since it was created, its title].
SCHEDULER_HARNESS = r"""
import { readFileSync } from "node:fs";
const source = readFileSync(process.argv[2], "utf8");
const scenarios = JSON.parse(process.argv[3]);
const TOKEN = "test-token-value";
const make = new Function("fetch", "setTimeout", "console", source + "\nreturn dispatchDigest;");
const out = {};
for (const [name, sc] of Object.entries(scenarios)) {
  const posts = [...sc.posts], lists = [...(sc.lists || [])];
  const seen = { requests: [], waits: [], logs: [], outcome: "ok", error: "" };
  const fetchImpl = async (url, init) => {
    const method = (init && init.method) || "GET";
    seen.requests.push({ method, url: String(url), headers: init.headers, body: init.body || null });
    const answer = method === "POST" ? posts.shift() : lists.shift();
    if (answer === undefined) throw new Error("the test has no answer left for " + method);
    if (answer === "network") throw new TypeError("fetch failed");
    if (Array.isArray(answer)) {
      const workflow_runs = answer.map(([age, title], i) => (
        { id: 900 + i, created_at: new Date(Date.now() - age * 1000).toISOString(), display_title: title }));
      return new Response(JSON.stringify({ workflow_runs }), { status: 200 });
    }
    const body = answer === 204 ? null : JSON.stringify({ message: "Server said " + answer });
    return new Response(body, { status: answer });
  };
  const dispatch = make(fetchImpl, (fn, ms) => { seen.waits.push(ms); fn(); },
    { log: (...parts) => seen.logs.push(parts.join(" ")) });
  const env = sc.env === undefined ? { GH_REPO: "owner/news-digest", GH_DISPATCH_TOKEN: TOKEN } : sc.env;
  try {
    await dispatch({ scheduledTime: Date.parse(sc.when || "2026-07-15T12:45:00Z") }, env, {});
  } catch (err) {
    seen.outcome = "threw";
    seen.error = String(err && err.message);
  }
  seen.tokenLeaked = JSON.stringify([seen.logs, seen.error]).includes(TOKEN);
  seen.unused = posts.length + lists.length;
  out[name] = seen;
}
console.log(JSON.stringify(out));
"""

MORNING_TITLE = "Daily digest (morning)"


@unittest.skipUnless(shutil.which("node"), "needs node")
class SchedulerBehaviorTests(unittest.TestCase):
    """worker/scheduler.js with fetch mocked: one firing never starts two runs."""

    SCENARIOS = {
        "accepted": {"posts": [204]},
        "refused": {"posts": [401]},
        "5xx, run exists": {"posts": [502], "lists": [[[20, MORNING_TITLE], [86400, MORNING_TITLE]]]},
        "5xx, no run": {"posts": [503, 204], "lists": [[[86400, MORNING_TITLE], [30, "Daily digest (noon)"]]]},
        "5xx, old run only": {"posts": [500, 204], "lists": [[[121, MORNING_TITLE]]]},
        "5xx, list unreadable": {"posts": [502], "lists": [500]},
        "5xx, list unreachable": {"posts": [502], "lists": ["network"]},
        "5xx twice": {"posts": [502, 503], "lists": [[]]},
        "network, no run": {"posts": ["network", 204], "lists": [[]]},
        "network, run exists": {"posts": ["network"], "lists": [[[5, MORNING_TITLE]]]},
        "network, list unreachable": {"posts": ["network", 204], "lists": ["network"]},
        "network twice": {"posts": ["network", "network"], "lists": [[]]},
        "noon": {"posts": [502], "lists": [[[15, "Daily digest (noon)"], [22500, MORNING_TITLE]]],
                 "when": "2026-07-15T19:00:00Z"},
        "other utc hour": {"posts": [], "when": "2026-07-15T13:45:00Z"},
        "not configured": {"posts": [], "env": {}},
    }

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp(prefix="news-digest-sched-")
        cls.addClassCleanup(shutil.rmtree, tmp)
        harness = os.path.join(tmp, "harness.mjs")
        with open(harness, "w", encoding="utf-8") as f:
            f.write(SCHEDULER_HARNESS)
        res = subprocess.run(["node", harness, SCHEDULER, json.dumps(cls.SCENARIOS)],
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise AssertionError("the scheduler harness failed: " + res.stderr)
        cls.got = json.loads(res.stdout)

    def counts(self, name):
        seen = self.got[name]
        methods = [r["method"] for r in seen["requests"]]
        return methods.count("POST"), methods.count("GET"), seen["outcome"]

    def test_every_scenario_used_its_answers_and_kept_the_token_out_of_the_log(self):
        for name, seen in self.got.items():
            self.assertEqual(seen["unused"], 0, name)
            self.assertIs(seen["tokenLeaked"], False, name)
            for line in seen["logs"] + ([seen["error"]] if seen["error"] else []):
                self.assertTrue(line.startswith("News digest: "), line)

    def test_accepted_request_is_the_only_one(self):
        self.assertEqual(self.counts("accepted"), (1, 0, "ok"))
        request = self.got["accepted"]["requests"][0]
        self.assertEqual(request["url"],
                         "https://api.github.com/repos/owner/news-digest/actions/workflows/daily.yml/dispatches")
        self.assertEqual(json.loads(request["body"]), {"ref": "main", "inputs": {"run": "morning"}})
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-token-value")
        self.assertEqual(self.got["accepted"]["waits"], [])
        self.assertIn("News digest: started the morning run", self.got["accepted"]["logs"][0])

    def test_a_refusal_is_not_retried(self):
        self.assertEqual(self.counts("refused"), (1, 0, "threw"))
        self.assertIn("GitHub answered 401 Server said 401", self.got["refused"]["error"])

    def test_5xx_with_the_run_in_the_list_sends_no_second_request(self):
        self.assertEqual(self.counts("5xx, run exists"), (1, 1, "ok"))
        seen = self.got["5xx, run exists"]
        self.assertEqual(seen["waits"], [10000])
        self.assertEqual([r["method"] for r in seen["requests"]], ["POST", "GET"])
        lookup = seen["requests"][1]
        self.assertTrue(lookup["url"].startswith(
            "https://api.github.com/repos/owner/news-digest/actions/workflows/daily.yml/runs?"), lookup["url"])
        self.assertIn("event=workflow_dispatch", lookup["url"])
        self.assertEqual(lookup["headers"]["Authorization"], "Bearer test-token-value")
        self.assertIn("did start, so no second request was sent", seen["logs"][-1])

    def test_5xx_without_the_run_retries_once(self):
        # Yesterday's run, a run older than 2 minutes and a noon run are not this firing's run.
        for name in ("5xx, no run", "5xx, old run only"):
            self.assertEqual(self.counts(name), (2, 1, "ok"), name)
            self.assertEqual([r["method"] for r in self.got[name]["requests"]], ["POST", "GET", "POST"], name)

    def test_5xx_with_an_unreadable_list_never_retries(self):
        for name in ("5xx, list unreadable", "5xx, list unreachable"):
            self.assertEqual(self.counts(name), (1, 1, "threw"), name)
            self.assertIn("No retry was sent", self.got[name]["error"])

    def test_at_most_two_requests_ever(self):
        self.assertEqual(self.counts("5xx twice"), (2, 1, "threw"))
        self.assertEqual(self.counts("network twice"), (2, 1, "threw"))
        for name in self.got:
            self.assertLessEqual(self.counts(name)[0], 2, name)

    def test_a_network_error_is_retried_unless_the_run_exists(self):
        self.assertEqual(self.counts("network, no run"), (2, 1, "ok"))
        self.assertEqual(self.counts("network, list unreachable"), (2, 1, "ok"))
        self.assertEqual(self.counts("network, run exists"), (1, 1, "ok"))

    def test_noon_looks_for_a_noon_run(self):
        self.assertEqual(self.counts("noon"), (1, 1, "ok"))
        self.assertEqual(json.loads(self.got["noon"]["requests"][0]["body"])["inputs"], {"run": "noon"})

    def test_nothing_is_sent_off_time_or_unconfigured(self):
        self.assertEqual(self.counts("other utc hour"), (0, 0, "ok"))
        self.assertIn("is not a digest time", self.got["other utc hour"]["logs"][0])
        self.assertEqual(self.counts("not configured"), (0, 0, "threw"))
        self.assertIn("News digest: set GH_REPO", self.got["not configured"]["error"])


# Runs protect() from worker/auth-guard.js in node around a handler that reports
# what it saw. crypto.subtle.timingSafeEqual exists in Workers only, so the
# harness supplies it. Prints what each request got as JSON.
GUARD_HARNESS = r"""
import { readFileSync } from "node:fs";
import { timingSafeEqual, webcrypto } from "node:crypto";
const source = readFileSync(process.argv[2], "utf8");
const cases = JSON.parse(process.argv[3]);
const cryptoShim = { subtle: {
  digest: (...args) => webcrypto.subtle.digest(...args),
  timingSafeEqual: (a, b) => timingSafeEqual(Buffer.from(a), Buffer.from(b)),
} };
const protect = new Function("crypto", source + "\nreturn protect;")(cryptoShim);
const handler = protect(async (request) => new Response("handler saw key: " + request.headers.has("authorization")));
const out = {};
for (const [name, c] of Object.entries(cases)) {
  const headers = {};
  if (c.origin) headers.origin = c.origin;
  if (c.key) headers.authorization = "Bearer " + c.key;
  const response = await handler(new Request("https://worker.example/extract?url=x", { method: c.method || "GET", headers }), c.env, {});
  out[name] = { status: response.status, allow: response.headers.get("access-control-allow-origin"),
    guard: response.headers.get("x-guard"), body: await response.text() };
}
console.log(JSON.stringify(out));
"""

AUTH_GUARD = os.path.join(support.ROOT, "worker", "auth-guard.js")
PAGES = "https://copier.github.io"


@unittest.skipUnless(shutil.which("node"), "needs node")
class AuthGuardTests(unittest.TestCase):
    """worker/auth-guard.js: the key is still the lock, and the origin
    allowlist comes from the Worker's ALLOWED_ORIGINS variable."""

    KEY = {"ACCESS_KEY": "right-key"}
    SET = {"ACCESS_KEY": "right-key", "ALLOWED_ORIGINS": f"{PAGES}, https://news.example.com/"}
    CASES = {
        "no key": {"env": KEY},
        "wrong key": {"env": KEY, "key": "wrong-key"},
        "right key": {"env": KEY, "key": "right-key"},
        "no access key on the worker": {"env": {}, "key": "right-key"},
        "unset, localhost": {"env": KEY, "key": "right-key", "origin": "http://localhost:8000"},
        "unset, pages": {"env": KEY, "key": "right-key", "origin": PAGES},
        "blank, localhost": {"env": dict(KEY, ALLOWED_ORIGINS=" , "), "key": "right-key",
                             "origin": "http://127.0.0.1:8000"},
        "set, pages": {"env": SET, "key": "right-key", "origin": PAGES},
        "set, second origin": {"env": SET, "key": "right-key", "origin": "https://news.example.com"},
        "set, other site": {"env": SET, "key": "right-key", "origin": "https://evil.example"},
        "set, lookalike": {"env": SET, "key": "right-key", "origin": PAGES + ".evil.example"},
        "set, localhost": {"env": SET, "key": "right-key", "origin": "http://localhost:8000"},
        "set, pages, no key": {"env": SET, "origin": PAGES},
        "set, preflight": {"env": SET, "origin": PAGES, "method": "OPTIONS"},
        "set, preflight from another site": {"env": SET, "origin": "https://evil.example", "method": "OPTIONS"},
    }

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp(prefix="news-digest-guard-")
        cls.addClassCleanup(shutil.rmtree, tmp)
        harness = os.path.join(tmp, "harness.mjs")
        with open(harness, "w", encoding="utf-8") as f:
            f.write(GUARD_HARNESS)
        res = subprocess.run(["node", harness, AUTH_GUARD, json.dumps(cls.CASES)],
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise AssertionError("the guard harness failed: " + res.stderr)
        cls.got = json.loads(res.stdout)

    def test_the_key_is_still_the_lock(self):
        for name, status in (("no key", 401), ("wrong key", 401), ("no access key on the worker", 503),
                             ("set, pages, no key", 401)):
            self.assertEqual((self.got[name]["status"], self.got[name]["guard"]), (status, "deny"), name)
        self.assertEqual(self.got["right key"]["status"], 200)
        self.assertEqual(self.got["right key"]["body"], "handler saw key: false")

    def test_without_the_variable_only_the_local_addresses_are_admitted(self):
        for name in ("unset, localhost", "blank, localhost"):
            self.assertEqual(self.got[name]["status"], 200, name)
            self.assertIn(self.got[name]["allow"], ("http://localhost:8000", "http://127.0.0.1:8000"))
        self.assertEqual((self.got["unset, pages"]["status"], self.got["unset, pages"]["guard"]), (403, "deny"))
        self.assertIsNone(self.got["unset, pages"]["allow"])

    def test_the_variable_names_the_origins(self):
        self.assertEqual((self.got["set, pages"]["status"], self.got["set, pages"]["allow"]), (200, PAGES))
        second = self.got["set, second origin"]  # spaces and a trailing slash in the variable are forgiven
        self.assertEqual((second["status"], second["allow"]), (200, "https://news.example.com"))
        for name in ("set, other site", "set, lookalike", "set, localhost", "set, preflight from another site"):
            self.assertEqual((self.got[name]["status"], self.got[name]["allow"]), (403, None), name)
        self.assertEqual((self.got["set, preflight"]["status"], self.got["set, preflight"]["allow"]), (204, PAGES))

    def test_the_file_names_no_real_origin(self):
        with open(AUTH_GUARD, encoding="utf-8") as f:
            text = f.read()
        listed = re.search(r"const DEFAULT_ALLOWED_ORIGINS = \[(.*?)\];", text, re.S).group(1)
        self.assertEqual(re.findall(r'^\s*"([^"]+)"', listed, re.M), ["http://localhost:8000", "http://127.0.0.1:8000"])
        self.assertIn("set ALLOWED_ORIGINS on the Worker", listed)
        for origin in re.findall(r"https://[A-Za-z0-9.-]+\.github\.io", text):
            self.assertEqual(origin, "https://your-name.github.io")
        self.assertIn("ALLOWED_ORIGINS: globalThis.ALLOWED_ORIGINS", text)  # the older-format recipe passes it on
        self.assertNotIn(chr(0x2014), text)
        self.assertNotIn(chr(0x2013), text)


if __name__ == "__main__":
    unittest.main()
