"""Offline checks of .github/workflows/stocks.yml (contract section 13): the
trigger, the gates, permissions, which step sees which secret, which job
installs what, and the privacy invariants: no cache, no artifact, no commit,
no Pages and no output in the private job, no expression inside a script, and
the private files removed at the end. Also actionlint, when it is installed,
and the locks.
"""
import os
import re
import shutil
import subprocess
import unittest

import yaml

import stocks_support as ss

WORKFLOW = os.path.join(ss.ROOT, ".github", "workflows", "stocks.yml")
ACTIONLINT = os.environ.get("ACTIONLINT", "/tmp/claude-0/final-scratch/al/bin/actionlint")
SECRET_STEPS = {
    "STOCKS_INGEST_KEY": {"Read the settings", "Save the results in the Worker"},
    "TIINGO_API_KEY": {"Fetch the data"},
    "SEC_USER_AGENT": {"Fetch the data"},
    "FMP_API_KEY": {"Fetch the data"},
    "ANTHROPIC_API_KEY": {"Ask Claude for the assessments"},
}


def load():
    with open(WORKFLOW, encoding="utf-8") as f:
        text = f.read()
    return text, yaml.safe_load(text)


def step(job, name):
    for s in job["steps"]:
        if s.get("name") == name:
            return s
    raise AssertionError(f"no step named {name!r}")


class StocksWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text, self.doc = load()
        self.jobs = self.doc["jobs"]

    def test_trigger_is_workflow_dispatch_only(self):
        on = self.doc.get("on", self.doc.get(True))
        self.assertEqual(list(on), ["workflow_dispatch"])
        reason = on["workflow_dispatch"]["inputs"]["reason"]
        self.assertEqual((reason["type"], reason["options"], reason["default"]),
                         ("choice", ["scheduled", "manual"], "scheduled"))
        for word in ("pull_request", "schedule:", "push:", "workflow_run", "workflow_call"):
            self.assertNotIn(word, self.text)
        self.assertEqual(self.doc["run-name"], "Stocks (${{ inputs.reason }})")
        self.assertEqual(self.doc["permissions"], {})
        self.assertEqual(self.doc["concurrency"], {"group": "stocks", "cancel-in-progress": False})

    def test_jobs_and_gates(self):
        self.assertEqual(list(self.jobs), ["gate", "congress", "analyze"])
        self.assertEqual(self.jobs["gate"]["if"],
                         "vars.DIGEST_ENABLED == 'true' && vars.STOCKS_ENABLED == 'true' && "
                         "github.ref == 'refs/heads/main'")
        self.assertEqual(self.jobs["congress"]["needs"], "gate")
        self.assertEqual(self.jobs["congress"]["if"], "needs.gate.outputs.run == 'true'")
        self.assertEqual(self.jobs["analyze"]["needs"], ["gate", "congress"])
        self.assertEqual(self.jobs["analyze"]["if"], "${{ !cancelled() && needs.gate.outputs.run == 'true' }}")
        self.assertEqual({k: j["timeout-minutes"] for k, j in self.jobs.items()},
                         {"gate": 3, "congress": 8, "analyze": 45})
        for name, job in self.jobs.items():
            self.assertEqual(job["permissions"], {"contents": "read"}, name)
            self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(self.jobs["analyze"]["environment"], "digest")
        self.assertNotIn("environment", self.jobs["gate"])
        self.assertNotIn("environment", self.jobs["congress"])
        gate = step(self.jobs["gate"], "Check the trading day")
        self.assertEqual(gate["env"], {"REASON": "${{ inputs.reason }}"})
        self.assertIn('python3 -I scripts/stocks/data/trading_days.py gate --reason "$REASON" >> "$GITHUB_OUTPUT"',
                      gate["run"])
        self.assertEqual(self.jobs["gate"]["outputs"], {"run": "${{ steps.gate.outputs.run }}"})

    def test_secrets_reach_only_their_steps(self):
        for job_name, job in self.jobs.items():
            for s in job["steps"]:
                env = s.get("env") or {}
                for name, value in env.items():
                    if "secrets." in str(value):
                        self.assertEqual(job_name, "analyze")
                        self.assertEqual(value, "${{ secrets.%s }}" % name)
                        self.assertIn(s["name"], SECRET_STEPS[name], name)
                self.assertNotIn("GITHUB_TOKEN", str(s))
                self.assertNotIn("github.token", str(s))
        for name, steps in SECRET_STEPS.items():
            self.assertEqual(self.text.count("secrets." + name), len(steps), name)
        self.assertEqual(len(re.findall(r"secrets\.", self.text)), sum(len(v) for v in SECRET_STEPS.values()))
        for name in ("Read the settings", "Save the results in the Worker"):
            self.assertEqual(step(self.jobs["analyze"], name)["env"]["WORKER_URL"], "${{ vars.WORKER_URL }}")

    def test_the_private_job_keeps_everything_private(self):
        analyze = self.jobs["analyze"]
        self.assertNotIn("outputs", analyze)
        uses = [s.get("uses", "") for s in analyze["steps"]]
        for banned in ("upload-artifact", "actions/cache", "upload-pages-artifact", "deploy-pages"):
            self.assertFalse([u for u in uses if banned in u], banned)
        for s in analyze["steps"]:
            self.assertNotIn("outputs", s)
            self.assertNotIn("cache", s.get("with") or {})
            run = s.get("run") or ""
            self.assertNotIn("${{", run)
            for word in ("git ", "set -x", "echo", "printenv", "env |", "cat ", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY",
                         "GITHUB_ENV"):
                self.assertNotIn(word, run, s.get("name"))
        last = analyze["steps"][-1]
        self.assertEqual((last["name"], last["if"], last["run"].strip()),
                         ("Remove the private files", "always()", 'rm -rf "$RUNNER_TEMP/stocks"'))
        for phase, name in (("config", "Read the settings"), ("fetch", "Fetch the data"),
                            ("analyze", "Analyze each ticker"), ("claude", "Ask Claude for the assessments"),
                            ("upload", "Save the results in the Worker")):
            s = step(analyze, name)
            self.assertIn(f'python -I scripts/stocks/run.py {phase} --dir "$RUNNER_TEMP/stocks"', s["run"])
        self.assertEqual(step(analyze, "Fetch the data")["if"], "steps.config.outcome == 'success'")
        self.assertEqual(step(analyze, "Analyze each ticker")["if"], "steps.fetch.outcome == 'success'")
        claude_step = step(analyze, "Ask Claude for the assessments")
        self.assertEqual((claude_step["if"], claude_step["continue-on-error"]),
                         ("steps.analyze.outcome == 'success'", True))
        self.assertEqual(step(analyze, "Save the results in the Worker")["if"],
                         "${{ !cancelled() && steps.analyze.outcome == 'success' }}")
        download = step(analyze, "Download the House trades")
        self.assertEqual((download["uses"], download["continue-on-error"], download["with"]["path"]),
                         ("actions/download-artifact@v4", True, "${{ runner.temp }}/stocks/public"))
        for word in ("commit", "git push", "pages", "docs/"):
            self.assertNotIn(word, "\n".join(s.get("run") or "" for s in analyze["steps"]))

    def test_no_job_commits_caches_or_publishes(self):
        for name, job in self.jobs.items():
            for s in job["steps"]:
                self.assertNotIn("actions/cache", s.get("uses", ""), name)
                self.assertNotIn("pages", s.get("uses", ""), name)
                if "setup-python" in s.get("uses", ""):
                    self.assertEqual(s["with"], {"python-version": "3.12"})
                if "checkout" in s.get("uses", ""):
                    self.assertEqual(s["with"]["persist-credentials"], False)
                self.assertNotIn("git commit", s.get("run") or "")
                self.assertNotIn("${{", s.get("run") or "", s.get("name"))

    def test_each_job_installs_only_its_lock(self):
        def installs(job):
            return [s["run"].strip() for s in job["steps"] if "pip install" in (s.get("run") or "")]

        self.assertEqual(installs(self.jobs["gate"]), [])
        self.assertFalse([s for s in self.jobs["gate"]["steps"] if "setup-python" in s.get("uses", "")])
        self.assertEqual(installs(self.jobs["congress"]),
                         ["pip install --require-hashes --only-binary :all: -r requirements-stocks.lock"])
        self.assertEqual(installs(self.jobs["analyze"]),
                         ["pip install --require-hashes --only-binary :all: -r requirements-brief.lock"])

    def test_the_congress_job_publishes_public_data_only(self):
        job = self.jobs["congress"]
        run = step(job, "Read the House trade reports")["run"]
        self.assertIn('python -I scripts/stocks/data/congress.py house --out "$RUNNER_TEMP/congress/house.json" '
                      "--days 365 --max-seconds 300", run)
        up = step(job, "Upload the House trades")
        self.assertEqual(up["uses"], "actions/upload-artifact@v4")
        self.assertEqual(up["with"], {"name": "stocks-congress", "path": "${{ runner.temp }}/congress/house.json",
                                      "if-no-files-found": "error", "retention-days": 1})
        self.assertFalse([s for s in job["steps"] if s.get("env")])

    def test_names_are_plain_and_in_sentence_case(self):
        for job in self.jobs.values():
            for s in job["steps"]:
                if "name" in s:
                    self.assertRegex(s["name"], r"^[A-Z][a-z]")
                    for word in s["name"].split()[1:]:
                        if word[:1].isupper():  # sentence case: capitals only for names
                            self.assertIn(word, ("Claude", "House", "Worker", "Anthropic", "SDK"), s["name"])
        self.assertNotIn(chr(0x2014), self.text)
        self.assertNotIn(chr(0x2013), self.text)
        header = self.text.split("\non:")[0]
        for needed in ("STOCKS_KV", "::add-mask::", "$RUNNER_TEMP/stocks", "public", "STOCKS_ENABLED"):
            self.assertIn(needed, header)

    @unittest.skipUnless(os.path.isfile(ACTIONLINT) or shutil.which("actionlint"), "needs actionlint")
    def test_actionlint(self):
        exe = ACTIONLINT if os.path.isfile(ACTIONLINT) else shutil.which("actionlint")
        res = subprocess.run([exe, WORKFLOW], capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


class SettingsListTests(unittest.TestCase):
    def test_env_example_names_every_new_setting(self):
        with open(os.path.join(ss.ROOT, ".env.example"), encoding="utf-8") as f:
            names = [line.strip()[:-1] for line in f if line.strip() and not line.startswith("#")]
        for name in ("STOCKS_ENABLED", "TIINGO_API_KEY", "SEC_USER_AGENT", "FMP_API_KEY", "STOCKS_INGEST_KEY",
                     "STOCKS_KV"):
            self.assertEqual(names.count(name), 1, name)
        with open(os.path.join(ss.ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("## Private stock analysis", readme)
        for name in ("STOCKS_KV", "STOCKS_INGEST_KEY", "TIINGO_API_KEY", "SEC_USER_AGENT", "STOCKS_ENABLED"):
            self.assertIn(name, readme)
        self.assertNotIn(chr(0x2014), readme)


class LockTests(unittest.TestCase):
    def test_the_stocks_lock_is_pypdf_alone_with_hashes(self):
        with open(os.path.join(ss.ROOT, "requirements-stocks.in"), encoding="utf-8") as f:
            pins = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        self.assertEqual(pins, ["pypdf==6.9.2"])
        with open(os.path.join(ss.ROOT, "requirements-stocks.lock"), encoding="utf-8") as f:
            lock = f.read()
        self.assertEqual(re.findall(r"(?m)^([A-Za-z0-9_.\-]+)==", lock), ["pypdf"])
        self.assertIn("pypdf==6.9.2", lock)
        self.assertIn("--hash=sha256:662cf29bcb419a36a1365232449624ab40b7c2d0cfc28e54f42eeecd1fd7e844", lock)
        self.assertIn("--hash=sha256:7f850faf2b0d4ab936582c05da32c52214c2b089d61a316627b5bfb5b0dab46c", lock)

    def test_the_private_job_needs_nothing_beyond_the_brief_lock(self):
        with open(os.path.join(ss.ROOT, "requirements-brief.lock"), encoding="utf-8") as f:
            names = {n.lower() for n in re.findall(r"(?m)^([A-Za-z0-9_.\-]+)==", f.read())}
        self.assertTrue({"anthropic", "pyyaml"} <= names)
        self.assertFalse({"numpy", "requests", "pypdf"} & names)


if __name__ == "__main__":
    unittest.main()
