"""Offline checks of the owner's edits of the X accounts, end to end:

  .github/workflows/sources.yml  static checks, and its two shell steps run
                                 verbatim with bash, jq and git against a
                                 scratch repository (gh is a recording fake)
  worker/sources.js              handleSources in node with fetch mocked,
                                 behind protect() from worker/auth-guard.js,
                                 wired by the two lines its header gives
  worker/worker.js               stays the four blocks joined (stocks.js is
                                 tested in tests/test_stocks_worker.py), and
                                 behaves the same
  scripts/fetch_and_build.py     reads data/x_accounts.json when it is valid,
                                 else feeds.twitter_embeds from config.yml
  docs/                          the page holds no edit control of its own

To rebuild worker/worker.js after changing one of the four blocks:
  python3 tests/test_sources.py --write-worker
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

import support
from test_fetch_and_build import PipelineTestCase as _Pipeline

import fetch_and_build as fab
from support import FakeSession

WORKFLOW = os.path.join(support.ROOT, ".github", "workflows", "sources.yml")
WORKER_DIR = os.path.join(support.ROOT, "worker")
BLOCKS = ("auth-guard.js", "scheduler.js", "sources.js", "stocks.js")
WORKER_JS = os.path.join(WORKER_DIR, "worker.js")
X_ACCOUNTS = os.path.join(support.ROOT, "data", "x_accounts.json")
CLOCK_LINE = "export default { fetch: protect(handleSources), scheduled: dispatchDigest };"
WORKER_LINE = "export default { fetch: routeWorker, scheduled: scheduledAll };"  # the last line of worker.js
HANDLE = "^[A-Za-z0-9_]{1,15}$"
NOT_A_HANDLE = ["", "a-b", "a b", "a.b", "x" * 16, "../../etc", "a;id", "$(id)", "`id`", "a\nb", "ok\n::error::x",
                "名前", "a'b", 'a"b', "@@two", "-rf"]


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def block(name):
    return read(os.path.join(WORKER_DIR, name))


def worker_body():
    """What worker/worker.js holds below its header."""
    return "\n".join(block(name) for name in BLOCKS) + "\n" + WORKER_LINE + "\n"


def worker_header(text):
    """The header of worker.js: its comment lines up to and with the first blank line."""
    return text[:text.index("\n\n") + 2]


def load():
    text = read(WORKFLOW)
    return text, yaml.safe_load(text)


def step(job, name):
    for s in job["steps"]:
        if s.get("name") == name:
            return s
    raise AssertionError(f"no step named {name!r}")


# ---------------------------------------------------------------- the workflow, as text

class WorkflowStaticTests(unittest.TestCase):
    def setUp(self):
        self.text, self.doc = load()
        self.job = self.doc["jobs"]["edit"]

    def test_trigger_is_workflow_dispatch_only(self):
        on = self.doc.get("on", self.doc.get(True))  # YAML 1.1 reads a bare on as true
        self.assertEqual(list(on), ["workflow_dispatch"])
        inputs = on["workflow_dispatch"]["inputs"]
        self.assertEqual(list(inputs), ["action", "handle"])
        self.assertEqual((inputs["action"]["type"], inputs["action"]["options"]), ("choice", ["add", "remove"]))
        self.assertEqual(inputs["handle"]["type"], "string")
        self.assertTrue(all(i["required"] is True for i in inputs.values()))
        for word in ("pull_request", "pull_request_target", "schedule:", "push:", "workflow_run", "workflow_call"):
            self.assertNotIn(word, self.text)
        self.assertEqual(self.doc["run-name"], "X accounts (${{ inputs.action }} @${{ inputs.handle }})")

    def test_one_job_gated_on_the_switch_and_on_main(self):
        self.assertEqual(list(self.doc["jobs"]), ["edit"])
        self.assertEqual(self.job["if"], "vars.DIGEST_ENABLED == 'true' && github.ref == 'refs/heads/main'")
        self.assertLessEqual(self.job["timeout-minutes"], 5)
        self.assertNotIn("environment", self.job)

    def test_permissions(self):
        self.assertEqual(self.doc["permissions"], {})
        self.assertEqual(self.job["permissions"], {"contents": "write", "actions": "write"})

    def test_no_secret_and_the_token_reaches_one_step(self):
        self.assertNotIn("secrets.", self.text)
        with_token = [s for s in self.job["steps"] if "github.token" in json.dumps(s)]
        self.assertEqual([s["name"] for s in with_token], ["Publish the change with a noon run"])
        self.assertEqual(with_token[0]["env"], {"GH_TOKEN": "${{ github.token }}", "GH_REPO": "${{ github.repository }}"})
        self.assertEqual(with_token[0]["if"], "steps.edit.outputs.changed == 'true'")

    def test_no_third_party_code(self):
        uses = [s["uses"] for s in self.job["steps"] if "uses" in s]
        self.assertEqual(uses, ["actions/checkout@v5"])
        self.assertEqual(self.job["steps"][0]["with"], {"ref": "main"})  # the newest main, not the commit of the request
        runs = "\n".join(s.get("run", "") for s in self.job["steps"])
        self.assertNotRegex(runs, r"\b(pip3?|python3?|npm|npx|node|curl|wget|docker)\b")

    def test_inputs_reach_the_script_through_the_environment_only(self):
        for s in self.job["steps"]:
            self.assertNotIn("${{", s.get("run", ""), s.get("name"))
        edit = step(self.job, "Edit the list and push")
        self.assertEqual(edit["env"], {"ACTION": "${{ inputs.action }}", "HANDLE": "${{ inputs.handle }}"})
        self.assertEqual(edit["id"], "edit")
        # The two inputs appear in the file in the names, the group and that env block, nowhere else.
        self.assertEqual(self.text.count("${{ inputs."), 6)

    def test_the_handle_is_checked_before_anything_is_written(self):
        script = step(self.job, "Edit the list and push")["run"]
        check = script.index(f'[[ "$handle" =~ {HANDLE} ]]')
        for later in ("git config", "git fetch", "git commit", "git push", "jq --arg"):
            self.assertLess(check, script.index(later), later)
        self.assertIn("max=50", script)
        self.assertIn("ascii_downcase", script)
        # Nothing prints the raw input: a line of it could be read as a workflow command.
        for line in script.splitlines():
            if "echo" in line:
                self.assertNotIn("$HANDLE", line, line)
                self.assertNotIn("$ACTION", line, line)

    def test_two_edits_never_race(self):
        # The same edit runs once at a time; different edits are kept apart by
        # starting again from the newest main, never by merging two copies.
        self.assertEqual(self.doc["concurrency"], {"group": "x-accounts-${{ inputs.action }}-${{ inputs.handle }}",
                                                   "cancel-in-progress": False})
        script = step(self.job, "Edit the list and push")["run"]
        self.assertIn("git fetch --quiet origin main", script)
        self.assertIn("git reset --quiet --hard FETCH_HEAD", script)
        self.assertIn("git push --quiet origin HEAD:main", script)
        self.assertNotIn("--force", script)
        self.assertIn('git config user.name "github-actions[bot]"', script)

    def test_the_noon_run_publishes_the_change(self):
        script = step(self.job, "Publish the change with a noon run")["run"]
        self.assertIn("gh workflow run daily.yml --ref main -f run=noon", script)
        with open(os.path.join(os.path.dirname(WORKFLOW), "daily.yml"), encoding="utf-8") as f:
            daily = yaml.safe_load(f)
        on = daily.get("on", daily.get(True))
        self.assertIn("noon", on["workflow_dispatch"]["inputs"]["run"]["options"])

    def test_plain_punctuation_and_sentence_case(self):
        self.assertNotIn(chr(0x2014), self.text)
        self.assertNotIn(chr(0x2013), self.text)
        messages = re.findall(r"::(?:error|warning)::([^\"]+)", self.text)
        messages += re.findall(r'echo "([^":][^"]*)"', self.text)
        messages = [m for m in messages if not m.startswith("changed=")]
        self.assertGreaterEqual(len(messages), 12)
        for message in messages:
            first = message.split()[0]
            # A sentence starts with a capital, a file name, a handle or a shell variable.
            self.assertTrue(first[0].isupper() or first[0] in "$@" or re.search(r"[./]\w", first), message)
            self.assertTrue(message.rstrip().endswith("."), message)


# ---------------------------------------------------------------- the workflow's shell, run for real

FAKE_GH = """#!/bin/bash
# A gh that records its calls. "run list" answers from $GH_LIST through the
# real jq, the way gh applies --jq; "workflow run" succeeds unless GH_RUN=fail.
printf '%s\\n' "$*" >> "$GH_CALLS"
if [ "$1 $2" = "run list" ]; then
  if [ "$GH_LIST" = fail ]; then echo "gh: could not list the runs" >&2; exit 1; fi
  expr=""
  while [ $# -gt 0 ]; do if [ "$1" = "--jq" ]; then expr="$2"; fi; shift; done
  jq "$expr" <<< "$GH_LIST"
  exit 0
fi
if [ "$1 $2" = "workflow run" ]; then
  if [ "$GH_RUN" = fail ]; then echo "gh: HTTP 403" >&2; exit 1; fi
  exit 0
fi
exit 2
"""

# A git that, at the first push, lets another edit reach main first: the commit
# waiting in $OTHER is pushed, so the push it was asked for is refused once.
RACING_GIT = """#!/bin/bash
if [ "$1" = push ] && [ ! -e "$RACE_DONE" ]; then
  : > "$RACE_DONE"
  "$REAL_GIT" -C "$OTHER" push -q origin HEAD:main
fi
exec "$REAL_GIT" "$@"
"""


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq") and shutil.which("git"), "needs bash, jq and git")
class WorkflowShellTests(unittest.TestCase):
    """Runs the steps the way GitHub does (bash -eo pipefail), in a clone of a
    scratch repository whose origin is a bare repository next to it."""

    START = ["Polymarket", "zerohedge"]

    def setUp(self):
        _, doc = load()
        self.job = doc["jobs"]["edit"]
        self.tmp = tempfile.mkdtemp(prefix="news-digest-sources-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.isolated = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "HOME": self.tmp}
        self.origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "repo")
        self.runner_temp = os.path.join(self.tmp, "runner")
        os.makedirs(self.runner_temp)
        self.git("init", "-q", "--bare", "-b", "main", self.origin, cwd=self.tmp)
        self.git("clone", "-q", self.origin, self.repo, cwd=self.tmp)
        self.write("data/x_accounts.json", json.dumps({"accounts": self.START}, indent=2) + "\n")
        self.write("docs/data/twitter_posts.json", json.dumps({"accounts": [
            {"handle": h, "url": f"https://x.com/{h}", "posts": []} for h in self.START]}))
        self.write("README.md", "readme\n")
        self.commit("start")

    def git(self, *args, cwd=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **self.isolated}
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, rel, text, repo=None):
        path = os.path.join(repo or self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def commit(self, message, repo=None):
        self.git("add", "-A", cwd=repo)
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", message, cwd=repo)
        self.git("push", "-q", "origin", "HEAD:main", cwd=repo)

    def bash(self, script, env):
        path = os.path.join(self.tmp, "step.sh")
        with open(path, "w", encoding="utf-8") as f:
            f.write(script)
        output = os.path.join(self.tmp, "github_output")
        if os.path.exists(output):
            os.remove(output)
        full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "RUNNER_TEMP": self.runner_temp,
                "GITHUB_OUTPUT": output, **self.isolated, **env}
        res = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", path], cwd=self.repo,
                             env=full, capture_output=True, text=True, timeout=60)
        res.output = read(output) if os.path.exists(output) else ""
        return res

    def edit(self, action, handle, **env):
        return self.bash(step(self.job, "Edit the list and push")["run"], {"ACTION": action, "HANDLE": handle, **env})

    def on_main(self, rel="data/x_accounts.json"):
        return self.git("show", f"main:{rel}", cwd=self.origin)

    def accounts(self):
        return json.loads(self.on_main())["accounts"]

    def commits(self):
        return self.git("log", "--format=%s", "main", cwd=self.origin).splitlines()

    # ------------------------------------------------ add and remove

    def test_add_commits_and_pushes_the_new_list(self):
        res = self.edit("add", "NASA")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.accounts(), self.START + ["NASA"])
        self.assertEqual(res.output, "changed=true\n")
        self.assertEqual(self.commits()[0], "add @NASA to the X accounts")
        self.assertEqual(self.git("log", "-1", "--format=%an <%ae>", "main", cwd=self.origin),
                         "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>")
        self.assertEqual(self.git("show", "--name-only", "--format=", "main", cwd=self.origin), "data/x_accounts.json")
        self.assertEqual(self.on_main() + "\n", json.dumps({"accounts": self.START + ["NASA"]}, indent=2) + "\n")
        self.assertIn("Saved: add @NASA to the X accounts. The list now holds 3 accounts.", res.stdout)

    def test_a_leading_at_sign_is_dropped(self):
        res = self.edit("add", "@NASA")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.accounts(), self.START + ["NASA"])

    def test_adding_a_listed_handle_in_any_case_changes_nothing(self):
        for handle in ("zerohedge", "ZeroHedge", "POLYMARKET"):
            res = self.edit("add", handle)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertEqual(res.output, "changed=false\n")
            self.assertIn(f"@{handle} is already in the list. Nothing to do.", res.stdout)
        self.assertEqual(self.accounts(), self.START)
        self.assertEqual(self.commits(), ["start"])

    def test_remove_is_case_insensitive(self):
        res = self.edit("remove", "POLYMARKET")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.accounts(), ["zerohedge"])
        self.assertEqual(res.output, "changed=true\n")
        self.assertEqual(self.commits()[0], "remove @POLYMARKET from the X accounts")
        res = self.edit("remove", "zerohedge")
        self.assertEqual(self.accounts(), [])  # an empty list is still the list
        self.assertEqual(self.on_main().split(), ["{", '"accounts":', "[]", "}"])

    def test_removing_a_handle_that_is_not_listed_changes_nothing(self):
        res = self.edit("remove", "NASA")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(res.output, "changed=false\n")
        self.assertIn("@NASA is not in the list. Nothing to do.", res.stdout)
        self.assertEqual(self.commits(), ["start"])

    # ------------------------------------------------ what is refused

    def test_a_bad_handle_is_refused_before_anything_is_written(self):
        marker = os.path.join(self.tmp, "ran")
        for handle in NOT_A_HANDLE + [f"$(touch {marker})", f"x; touch {marker}", f"`touch {marker}`"]:
            with self.subTest(handle=handle):
                res = self.edit("add", handle)
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
                self.assertEqual(res.stdout, "::error::The handle must be 1 to 15 letters, numbers or underscores.\n")
                self.assertEqual(res.output, "")
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.commits(), ["start"])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_bad_action_is_refused(self):
        for action in ("", "delete", "add remove", "add\n", "$(id)"):
            res = self.edit(action, "NASA")
            self.assertEqual(res.returncode, 1, action)
            self.assertEqual(res.stdout, "::error::The action must be add or remove.\n")
        self.assertEqual(self.commits(), ["start"])

    def test_the_list_stops_at_fifty(self):
        full = [f"account_{n}" for n in range(50)]
        self.write("data/x_accounts.json", json.dumps({"accounts": full}))
        self.commit("fifty")
        res = self.edit("add", "NASA")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("::error::The list already holds 50 accounts. Remove one first.", res.stdout)
        self.assertEqual(self.accounts(), full)
        self.assertEqual(self.edit("add", "ACCOUNT_7").output, "changed=false\n")  # a listed one is no error
        self.assertEqual(self.edit("remove", "account_0").returncode, 0)
        self.assertEqual(self.edit("add", "NASA").returncode, 0)
        self.assertEqual(self.accounts(), full[1:] + ["NASA"])

    def test_a_broken_list_is_left_alone(self):
        for broken in ("{broken", "[]", '{"accounts": "zerohedge"}', '{"accounts": ["ok", "not ok"]}',
                       '{"accounts": ["ok", 5]}', '{"handles": []}'):
            with self.subTest(broken=broken):
                self.write("data/x_accounts.json", broken)
                self.commit("broken")
                res = self.edit("add", "NASA")
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
                self.assertIn("::error::data/x_accounts.json is not a list of X handles.", res.stdout)
                self.assertEqual(self.on_main(), broken)

    def test_a_link_in_place_of_the_list_is_refused(self):
        os.remove(os.path.join(self.repo, "data/x_accounts.json"))
        os.symlink("../README.md", os.path.join(self.repo, "data/x_accounts.json"))
        self.commit("link")
        res = self.edit("add", "NASA")
        self.assertEqual(res.returncode, 1)
        self.assertIn("::error::data/x_accounts.json is not a regular file.", res.stdout)

    # ------------------------------------------------ no list yet

    def test_without_a_list_it_starts_from_the_published_accounts(self):
        os.remove(os.path.join(self.repo, "data/x_accounts.json"))
        self.commit("no list")
        res = self.edit("add", "NASA")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.accounts(), self.START + ["NASA"])
        self.assertIn("Starting data/x_accounts.json from the accounts in docs/data/twitter_posts.json.", res.stdout)

    def test_without_a_list_or_published_accounts_it_starts_empty(self):
        os.remove(os.path.join(self.repo, "data/x_accounts.json"))
        self.write("docs/data/twitter_posts.json", "{broken")
        self.commit("nothing")
        res = self.edit("add", "NASA")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.accounts(), ["NASA"])

    # ------------------------------------------------ two edits at once

    def other_clone(self):
        other = os.path.join(self.tmp, "other")
        self.git("clone", "-q", self.origin, other, cwd=self.tmp)
        return other

    def test_it_starts_from_the_newest_main(self):
        # The checkout is older than main: an edit and a daily commit landed after it.
        other = self.other_clone()
        self.write("data/x_accounts.json", json.dumps({"accounts": self.START + ["Earlier"]}), repo=other)
        self.write("docs/data/items.json", '{"items": [1]}', repo=other)
        self.commit("earlier edit and data", repo=other)
        res = self.edit("add", "NASA")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.accounts(), self.START + ["Earlier", "NASA"])
        self.assertEqual(self.on_main("docs/data/items.json"), '{"items": [1]}')
        self.assertEqual(self.commits()[:2], ["add @NASA to the X accounts", "earlier edit and data"])

    def racing(self, other_accounts):
        """An edit that reaches main between this run's commit and its push."""
        other = self.other_clone()
        self.write("data/x_accounts.json", json.dumps({"accounts": other_accounts}, indent=2) + "\n", repo=other)
        self.git("add", "-A", cwd=other)
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "the other edit", cwd=other)
        folder = os.path.join(self.tmp, "bin")
        os.makedirs(folder)
        with open(os.path.join(folder, "git"), "w", encoding="utf-8") as f:
            f.write(RACING_GIT)
        os.chmod(os.path.join(folder, "git"), 0o755)
        return {"PATH": folder + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"), "REAL_GIT": shutil.which("git"),
                "OTHER": other, "RACE_DONE": os.path.join(self.tmp, "race-done")}

    def test_an_edit_pushed_meanwhile_is_kept_and_this_one_is_made_again(self):
        # Both edits append after the same last line: a rebase or a merge of the
        # two files would conflict. Starting again from the new main does not.
        res = self.edit("add", "NASA", **self.racing(self.START + ["Meanwhile"]))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("Main changed while this edit was being made. Trying again from the new main (1 of 5).", res.stdout)
        self.assertEqual(self.accounts(), self.START + ["Meanwhile", "NASA"])
        self.assertEqual(self.commits(), ["add @NASA to the X accounts", "the other edit", "start"])
        self.assertEqual(res.output, "changed=true\n")

    def test_the_same_edit_pushed_meanwhile_leaves_nothing_to_do(self):
        res = self.edit("add", "NASA", **self.racing(self.START + ["nasa"]))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.accounts(), self.START + ["nasa"])
        self.assertEqual(self.commits(), ["the other edit", "start"])
        self.assertEqual(res.output, "changed=false\n")

    def test_a_remove_pushed_meanwhile_does_not_come_back(self):
        res = self.edit("add", "NASA", **self.racing(["zerohedge"]))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.accounts(), ["zerohedge", "NASA"])

    # ------------------------------------------------ the noon run

    def publish(self, runs, run="ok"):
        folder = os.path.join(self.tmp, "gh-bin")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "gh"), "w", encoding="utf-8") as f:
            f.write(FAKE_GH)
        os.chmod(os.path.join(folder, "gh"), 0o755)
        calls = os.path.join(self.tmp, "gh-calls.txt")
        if os.path.exists(calls):
            os.remove(calls)
        res = self.bash(step(self.job, "Publish the change with a noon run")["run"], {
            "PATH": folder + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"), "GH_CALLS": calls,
            "GH_LIST": runs if isinstance(runs, str) else json.dumps([{"status": s} for s in runs]),
            "GH_RUN": run, "GH_TOKEN": "t", "GH_REPO": "owner/news-digest"})
        return res, read(calls).splitlines() if os.path.exists(calls) else []

    START_NOON = "workflow run daily.yml --ref main -f run=noon"

    def test_a_noon_run_is_started(self):
        for runs in ([], ["completed", "completed"], ["in_progress", "completed"]):
            res, calls = self.publish(runs)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertEqual(len(calls), 2, calls)
            self.assertTrue(calls[0].startswith("run list --workflow daily.yml "), calls[0])
            self.assertEqual(calls[1], self.START_NOON)
            self.assertIn("Started a noon run of the daily digest to publish the change.", res.stdout)

    def test_a_daily_run_that_has_not_started_publishes_the_change(self):
        # A second request would take the waiting run's place in the queue,
        # and the waiting run could be the morning one, with the email.
        for runs in (["queued"], ["in_progress", "pending"], ["waiting", "completed"], ["requested"]):
            res, calls = self.publish(runs)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertEqual(len(calls), 1, calls)
            self.assertIn("A daily run is already waiting to start. It publishes this change.", res.stdout)

    def test_an_unreadable_list_of_runs_still_starts_the_noon_run(self):
        res, calls = self.publish("fail")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(calls[-1], self.START_NOON)

    def test_a_noon_run_that_cannot_start_is_a_warning(self):
        res, calls = self.publish([], run="fail")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)  # the list is saved all the same
        self.assertIn("::warning::The list is saved, but the noon run could not be started.", res.stdout)


# ---------------------------------------------------------------- the Worker blocks, as text

class WorkerTextTests(unittest.TestCase):
    def setUp(self):
        self.sources = block("sources.js")
        self.worker = read(WORKER_JS)

    def wiring_lines(self):
        return re.findall(r"(?m)^//\s+(export default \{.*\};)$", self.sources)

    def test_sources_is_a_block_to_paste(self):
        self.assertNotRegex(self.sources, r"(?m)^\s*export\b")
        self.assertIn("async function handleSources(request, env, ctx)", self.sources)
        self.assertIn('const SOURCES_WORKFLOW = "sources.yml";', self.sources)
        self.assertIn('const SOURCES_BRANCH = "main";', self.sources)
        self.assertIn('const SOURCES_PATH = "/x-accounts";', self.sources)
        self.assertIn(f"const SOURCES_HANDLE = /{HANDLE}/;", self.sources)
        self.assertIn("https://api.github.com/repos/${repo}/actions/workflows/${SOURCES_WORKFLOW}/dispatches", self.sources)
        self.assertIn('{ error: "summaries are not set up on this worker" }', self.sources)
        for setting in ("ACCESS_KEY", "ALLOWED_ORIGINS", "GH_DISPATCH_TOKEN", "GH_REPO"):
            self.assertIn(setting, self.sources.split("const SOURCES_WORKFLOW")[0])

    def test_the_header_gives_both_last_lines(self):
        lines = self.wiring_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], CLOCK_LINE)
        self.assertIn('new URL(request.url).pathname === "/x-accounts" ? handleSources(request, env, ctx) : '
                      "original.fetch(request, env, ctx)", lines[1])
        for line in lines:
            self.assertIn("fetch: protect(", line)  # never reachable without the guard
            self.assertIn("scheduled: dispatchDigest }", line)

    def test_the_blocks_share_no_name(self):
        names = {}
        for name in BLOCKS:
            for found in re.findall(r"(?m)^(?:async )?(?:function|const|let|var) ([A-Za-z_$][\w$]*)", block(name)):
                self.assertNotIn(found, names, f"{found} is in {name} and {names.get(found)}")
                names[found] = name
        self.assertTrue({"protect", "dispatchDigest", "handleSources"} <= set(names))

    def test_the_token_is_never_logged_or_sent_back(self):
        for line in self.sources.splitlines():
            if "console." in line or "return sourcesAnswer(" in line or "new Error(" in line:
                self.assertNotIn("GH_DISPATCH_TOKEN}", line)
                self.assertNotIn("headers", line)
        self.assertEqual(self.sources.count("env.GH_DISPATCH_TOKEN}"), 1)  # the Authorization header, once

    def test_plain_punctuation_and_log_lines(self):
        for text in (self.sources, self.worker):
            self.assertNotIn(chr(0x2014), text)
            self.assertNotIn(chr(0x2013), text)
        messages = re.findall(r"console\.log\(\s*[`\"]([^`\"]*)", self.sources)
        self.assertGreaterEqual(len(messages), 3)
        for message in messages:
            self.assertTrue(message.startswith("News digest: "), message)

    def test_worker_js_is_the_four_blocks_and_the_last_line(self):
        header = worker_header(self.worker)
        self.assertEqual(self.worker, header + worker_body(),
                         "worker/worker.js is out of date: run python3 tests/test_sources.py --write-worker")
        self.assertTrue(self.worker.endswith("\n" + WORKER_LINE + "\n"))
        self.assertEqual(re.findall(r"(?m)^[ \t]*export\b.*$", self.worker), [WORKER_LINE])

    def test_worker_js_header_names_what_it_needs(self):
        header = worker_header(self.worker)
        lines = header.splitlines()
        self.assertGreaterEqual(len(lines), 10)
        self.assertEqual(lines[-1], "")
        self.assertTrue(all(line == "" or line.startswith("//") for line in lines), "the header is comments only")
        for needed in ("ACCESS_KEY", "ALLOWED_ORIGINS", "GH_DISPATCH_TOKEN", "GH_REPO", "45 12,13 * * *", "0 19,20 * * *",
                       "worker/auth-guard.js", "worker/scheduler.js", "worker/sources.js", "worker/stocks.js",
                       "STOCKS_KV", "STOCKS_INGEST_KEY"):
            self.assertIn(needed, header)
        scheduler = block("scheduler.js")
        for cron in ("45 12,13 * * *", "0 19,20 * * *"):
            self.assertIn(cron, scheduler)


# ---------------------------------------------------------------- the Worker, run in node

# Runs a whole Worker in node, one request or one cron firing per case, with
# fetch and console replaced, and prints what each case did as JSON. The Worker
# is the text in sources[case.worker] with its last "export default" turned
# into a return. crypto.subtle.timingSafeEqual exists in Workers only, so the
# harness supplies it. answers are GitHub's replies in order: an HTTP status,
# or "network" for a request that throws.
WORKER_HARNESS = r"""
import { readFileSync } from "node:fs";
import { timingSafeEqual, webcrypto } from "node:crypto";
const sources = JSON.parse(readFileSync(process.argv[2], "utf8"));
const cases = JSON.parse(readFileSync(process.argv[3], "utf8"));
const TOKEN = "test-token-value";
const cryptoShim = { subtle: {
  digest: (...args) => webcrypto.subtle.digest(...args),
  timingSafeEqual: (a, b) => timingSafeEqual(Buffer.from(a), Buffer.from(b)),
} };
const out = {};
for (const [name, c] of Object.entries(cases)) {
  const answers = [...(c.answers || [])];
  const seen = { requests: [], logs: [] };
  const fetchImpl = async (url, init) => {
    seen.requests.push({ url: String(url), method: init.method, headers: init.headers, body: init.body || null });
    const answer = answers.shift();
    if (answer === undefined) throw new Error("the test has no answer left");
    if (answer === "network") throw new TypeError("fetch failed");
    return new Response(answer === 204 ? null : JSON.stringify({ message: "Server said " + answer }), { status: answer });
  };
  const worker = new Function("fetch", "console", "crypto", "setTimeout", sources[c.worker || "clock"])(
    fetchImpl, { log: (...parts) => seen.logs.push(parts.join(" ")) }, cryptoShim, (fn) => fn());
  const env = c.env === undefined
    ? { ACCESS_KEY: "right-key", ALLOWED_ORIGINS: "https://copier.github.io", GH_REPO: "owner/news-digest", GH_DISPATCH_TOKEN: TOKEN }
    : c.env;
  if (c.scheduled) {
    try { await worker.scheduled({ scheduledTime: Date.parse(c.scheduled) }, env, {}); seen.outcome = "ok"; }
    catch (err) { seen.outcome = "threw"; seen.logs.push(String(err && err.message)); }
  } else {
    const headers = {};
    if (c.origin !== null) headers.origin = c.origin || "https://copier.github.io";
    if (c.key !== null) headers.authorization = "Bearer " + (c.key || "right-key");
    const init = { method: c.method || "POST", headers };
    if (c.body !== undefined && init.method !== "GET") {
      headers["content-type"] = "application/json";
      init.body = typeof c.body === "string" ? c.body : JSON.stringify(c.body);
    }
    const response = await worker.fetch(new Request("https://worker.example" + (c.path || "/x-accounts"), init), env, {});
    seen.status = response.status;
    seen.guard = response.headers.get("x-guard");
    seen.allow = response.headers.get("access-control-allow-origin");
    seen.type = response.headers.get("content-type");
    seen.text = await response.text();
  }
  seen.tokenLeaked = JSON.stringify([seen.logs, seen.text || ""]).includes(TOKEN);
  seen.unused = answers.length;
  out[name] = seen;
}
console.log(JSON.stringify(out));
"""

OWN_CODE = """const original = {
  async fetch(request, env, ctx) {
    return new Response("own code: " + new URL(request.url).pathname + " key seen: " + request.headers.has("authorization"));
  },
};"""
DISPATCH_URL = "https://api.github.com/repos/owner/news-digest/actions/workflows/sources.yml/dispatches"
PAGES = "https://copier.github.io"


def as_function_body(source):
    """A module's text as a function body: its one export becomes the return."""
    assert len(re.findall(r"(?m)^export default ", source)) == 1, "one export default line"
    return re.sub(r"(?m)^export default ", "return ", source)


@unittest.skipUnless(shutil.which("node"), "needs node")
class WorkerBehaviorTests(unittest.TestCase):
    """handleSources behind the guard, in the two Workers the header of
    sources.js describes, and in worker/worker.js as committed."""

    ADD = {"action": "add", "handle": "NASA"}
    CASES = {
        "add": {"body": ADD, "answers": [204]},
        "remove": {"body": {"action": "remove", "handle": "zerohedge"}, "answers": [204]},
        "add with an at sign and spaces": {"body": {"action": "add", "handle": " @NASA "}, "answers": [204]},
        "trailing slash": {"path": "/x-accounts/", "body": ADD, "answers": [204]},
        "no key": {"body": ADD, "key": None},
        "wrong key": {"body": ADD, "key": "wrong-key"},
        "wrong origin": {"body": ADD, "origin": "https://evil.example"},
        "no origin, right key": {"body": ADD, "origin": None, "answers": [204]},
        "preflight": {"method": "OPTIONS", "key": None},
        "no access key on the worker": {"body": ADD, "origin": None,
                                        "env": {"GH_REPO": "owner/news-digest", "GH_DISPATCH_TOKEN": "t"}},
        "bad action": {"body": {"action": "delete", "handle": "NASA"}},
        "no action": {"body": {"handle": "NASA"}},
        "not json": {"body": "{broken"},
        "a list": {"body": ["add", "NASA"]},
        "no body": {},
        "too long": {"body": {"action": "add", "handle": "NASA", "padding": "x" * 3000}},
        "get": {"method": "GET"},
        "analyze": {"path": "/analyze", "body": {"url": "https://example.com/a"}},
        "extract": {"path": "/extract?url=https://example.com/a", "method": "GET"},
        "transcript": {"path": "/yt-transcript?id=abcdefghijk", "method": "GET"},
        "root": {"path": "/", "method": "GET"},
        "lookalike path": {"path": "/x-accounts/extra", "body": ADD},
        "not configured": {"body": ADD, "env": {"ACCESS_KEY": "right-key", "ALLOWED_ORIGINS": PAGES}},
        "bad repo": {"body": ADD, "env": {"ACCESS_KEY": "right-key", "ALLOWED_ORIGINS": PAGES,
                                          "GH_REPO": "owner/news-digest/../x", "GH_DISPATCH_TOKEN": "t"}},
        "github refuses": {"body": ADD, "answers": [404]},
        "github 5xx then takes it": {"body": ADD, "answers": [502, 204]},
        "github 5xx twice": {"body": ADD, "answers": [500, 503]},
        "network then takes it": {"body": ADD, "answers": ["network", 204]},
        "network twice": {"body": ADD, "answers": ["network", "network"]},
        "morning cron": {"scheduled": "2026-07-15T12:45:00Z", "answers": [204]},
        "noon cron": {"scheduled": "2026-01-15T20:00:00Z", "answers": [204]},
        "off-time cron": {"scheduled": "2026-07-15T13:45:00Z"},
        "own code, add": {"worker": "own", "body": ADD, "answers": [204]},
        "own code, no key": {"worker": "own", "body": ADD, "key": None},
        "own code, analyze": {"worker": "own", "path": "/analyze", "body": {"url": "https://example.com/a"}},
        "own code, analyze, no key": {"worker": "own", "path": "/analyze", "body": {}, "key": None},
        "own code, cron": {"worker": "own", "scheduled": "2026-07-15T19:00:00Z", "answers": [204]},
        "worker.js, add": {"worker": "file", "body": ADD, "answers": [204]},
        "worker.js, no key": {"worker": "file", "body": ADD, "key": None},
        "worker.js, wrong origin": {"worker": "file", "body": ADD, "origin": "https://evil.example"},
        "worker.js, bad handle": {"worker": "file", "body": {"action": "add", "handle": "not a handle"}},
        "worker.js, analyze": {"worker": "file", "path": "/analyze", "body": {}},
        "worker.js, cron": {"worker": "file", "scheduled": "2026-07-15T12:45:00Z", "answers": [204]},
    }
    for _n, _handle in enumerate(NOT_A_HANDLE + [5, None, ["NASA"], {"$ne": 1}, True]):
        CASES[f"bad handle {_n}"] = {"body": {"action": "add", "handle": _handle}}
    del _n, _handle

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp(prefix="news-digest-sources-node-")
        cls.addClassCleanup(shutil.rmtree, tmp)
        blocks = "\n".join(block(name) for name in BLOCKS)
        own_line = re.findall(r"(?m)^//\s+(export default \{.*\};)$", block("sources.js"))[1]
        workers = {
            "clock": as_function_body(blocks + "\n" + CLOCK_LINE),
            "own": as_function_body("\n".join([blocks, OWN_CODE, own_line])),
            "file": as_function_body(read(WORKER_JS)),
        }
        paths = {}
        for name, data in (("harness.mjs", WORKER_HARNESS), ("workers.json", json.dumps(workers)),
                           ("cases.json", json.dumps(cls.CASES))):
            paths[name] = os.path.join(tmp, name)
            with open(paths[name], "w", encoding="utf-8") as f:
                f.write(data)
        res = subprocess.run(["node", paths["harness.mjs"], paths["workers.json"], paths["cases.json"]],
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise AssertionError("the worker harness failed: " + res.stderr)
        cls.got = json.loads(res.stdout)

    def answer(self, name):
        seen = self.got[name]
        try:
            body = json.loads(seen["text"])
        except ValueError:
            body = seen["text"]
        return seen["status"], body, len(seen["requests"])

    def test_every_case_used_its_answers_and_kept_the_token_to_itself(self):
        for name, seen in self.got.items():
            self.assertEqual(seen["unused"], 0, name)
            self.assertIs(seen["tokenLeaked"], False, name)
            for line in seen["logs"]:
                # The stocks block (worker.js) logs its own lines, "Stocks: ...".
                self.assertTrue(line.startswith(("News digest: ", "Stocks: ")), line)

    def test_add_starts_the_workflow_once_with_the_right_inputs(self):
        self.assertEqual(self.answer("add"), (202, {"ok": True, "action": "add", "handle": "NASA"}, 1))
        request = self.got["add"]["requests"][0]
        self.assertEqual((request["url"], request["method"]), (DISPATCH_URL, "POST"))
        self.assertEqual(json.loads(request["body"]), {"ref": "main", "inputs": {"action": "add", "handle": "NASA"}})
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-token-value")
        self.assertEqual(request["headers"]["Accept"], "application/vnd.github+json")
        self.assertEqual(request["headers"]["User-Agent"], "news-digest-sources")
        self.assertEqual(self.got["add"]["allow"], PAGES)  # the guard's CORS headers are on the answer
        self.assertEqual(self.got["add"]["type"], "application/json")
        self.assertEqual(self.got["add"]["logs"], ["News digest: asked GitHub to add @NASA, GitHub answered 204"])

    def test_remove_and_tidied_handles(self):
        self.assertEqual(self.answer("remove"), (202, {"ok": True, "action": "remove", "handle": "zerohedge"}, 1))
        self.assertEqual(json.loads(self.got["remove"]["requests"][0]["body"])["inputs"],
                         {"action": "remove", "handle": "zerohedge"})
        for name in ("add with an at sign and spaces", "trailing slash", "no origin, right key"):
            self.assertEqual(self.answer(name)[0], 202, name)
            self.assertEqual(json.loads(self.got[name]["requests"][0]["body"])["inputs"]["handle"], "NASA", name)

    def test_only_the_key_holder_gets_through(self):
        for name, status in (("no key", 401), ("wrong key", 401), ("wrong origin", 403),
                             ("no access key on the worker", 503), ("own code, no key", 401),
                             ("own code, analyze, no key", 401), ("worker.js, no key", 401),
                             ("worker.js, wrong origin", 403)):
            self.assertEqual((self.got[name]["status"], self.got[name]["guard"], len(self.got[name]["requests"])),
                             (status, "deny", 0), name)
        self.assertEqual((self.got["preflight"]["status"], self.got["preflight"]["allow"]), (204, PAGES))
        self.assertEqual(self.got["preflight"]["requests"], [])

    def test_a_bad_request_is_400_and_starts_nothing(self):
        bad = [n for n in self.got if n.startswith("bad handle ")]
        self.assertEqual(len(bad), len(NOT_A_HANDLE) + 5)
        for name in bad + ["bad action", "no action", "not json", "a list", "no body", "too long", "worker.js, bad handle"]:
            status, body, requests = self.answer(name)
            self.assertEqual((status, requests), (400, 0), name)
            self.assertEqual(list(body), ["error"], name)
        self.assertEqual(self.answer("bad handle 1")[1], {"error": "the handle must be 1 to 15 letters, numbers or underscores"})
        self.assertEqual(self.answer("bad action")[1], {"error": "the action must be add or remove"})
        self.assertEqual(self.answer("get")[0::2], (405, 0))

    def test_any_other_path_says_summaries_are_not_set_up(self):
        for name in ("analyze", "extract", "transcript", "root", "lookalike path", "worker.js, analyze"):
            self.assertEqual(self.answer(name), (501, {"error": "summaries are not set up on this worker"}, 0), name)
            self.assertIsNone(self.got[name]["guard"], name)  # the dashboard must not read it as the guard's refusal

    def test_a_worker_that_is_not_set_up_says_so(self):
        for name in ("not configured", "bad repo"):
            status, body, requests = self.answer(name)
            self.assertEqual((status, requests), (500, 0), name)
            self.assertIn("set GH_REPO and GH_DISPATCH_TOKEN", body["error"])

    def test_github_problems_are_502_with_the_reason(self):
        status, body, requests = self.answer("github refuses")
        self.assertEqual((status, requests), (502, 1))  # a refusal is not retried
        self.assertEqual(body, {"error": "the change was not saved: GitHub answered 404 Server said 404"})
        self.assertEqual(self.answer("github 5xx then takes it")[0::2], (202, 2))
        self.assertEqual(self.answer("network then takes it")[0::2], (202, 2))
        self.assertEqual(self.answer("github 5xx twice")[0::2], (502, 2))
        status, body, requests = self.answer("network twice")
        self.assertEqual((status, requests), (502, 2))
        self.assertIn("GitHub could not be reached", body["error"])

    def test_the_clock_still_starts_the_daily_runs(self):
        for name, run in (("morning cron", "morning"), ("noon cron", "noon"), ("own code, cron", "noon"),
                          ("worker.js, cron", "morning")):
            seen = self.got[name]
            self.assertEqual(seen["outcome"], "ok", name)
            self.assertEqual(len(seen["requests"]), 1, name)
            self.assertEqual(seen["requests"][0]["url"],
                             "https://api.github.com/repos/owner/news-digest/actions/workflows/daily.yml/dispatches")
            self.assertEqual(json.loads(seen["requests"][0]["body"]), {"ref": "main", "inputs": {"run": run}})
        self.assertEqual(self.got["off-time cron"]["requests"], [])

    def test_a_worker_with_its_own_code_keeps_it_for_every_other_path(self):
        self.assertEqual(self.answer("own code, add"), (202, {"ok": True, "action": "add", "handle": "NASA"}, 1))
        self.assertEqual(self.got["own code, add"]["requests"][0]["url"], DISPATCH_URL)
        self.assertEqual(self.answer("own code, analyze"), (200, "own code: /analyze key seen: false", 0))

    def test_worker_js_does_the_same(self):
        self.assertEqual(self.answer("worker.js, add"), (202, {"ok": True, "action": "add", "handle": "NASA"}, 1))
        self.assertEqual(self.got["worker.js, add"]["requests"], self.got["add"]["requests"])


# ---------------------------------------------------------------- fetch_and_build.py

class XAccountsFileTests(_Pipeline):
    """data/x_accounts.json is the list of record once it exists and is valid."""

    CONFIG = [{"handle": "Polymarket", "url": "https://x.com/Polymarket", "posts": []},
              {"handle": "zerohedge", "url": "https://x.com/zerohedge", "posts": []}]

    def accounts(self, content=None):
        if content is not None:
            os.makedirs(os.path.dirname(self.paths["X_ACCOUNTS"]), exist_ok=True)
            with open(self.paths["X_ACCOUNTS"], "w", encoding="utf-8") as f:
                f.write(content if isinstance(content, str) else json.dumps(content))
        out = self.run_main(FakeSession(self.routes()))
        return self.read("TWITTER_OUT")["accounts"], out

    def test_without_the_file_the_config_list_is_used(self):
        accounts, out = self.accounts()
        self.assertEqual(accounts, self.CONFIG)
        self.assertNotIn("x_accounts.json", out)

    def test_the_file_wins_over_the_config_list(self):
        accounts, _ = self.accounts({"accounts": ["NASA", "zerohedge", "a_b_1"]})
        self.assertEqual(accounts, [{"handle": h, "url": f"https://x.com/{h}", "posts": []}
                                    for h in ("NASA", "zerohedge", "a_b_1")])

    def test_an_empty_list_means_no_accounts(self):
        accounts, _ = self.accounts({"accounts": []})
        self.assertEqual(accounts, [])

    def test_a_handle_listed_twice_is_shown_once(self):
        accounts, _ = self.accounts({"accounts": ["NASA", "nasa", "Nasa", "zerohedge"]})
        self.assertEqual([a["handle"] for a in accounts], ["NASA", "zerohedge"])

    def test_fifty_is_the_most(self):
        accounts, _ = self.accounts({"accounts": [f"account_{n}" for n in range(50)]})
        self.assertEqual(len(accounts), 50)
        accounts, out = self.accounts({"accounts": [f"account_{n}" for n in range(51)]})
        self.assertEqual(accounts, self.CONFIG)
        self.assertIn("[warn] data/x_accounts.json is not a list of at most 50 X handles", out)

    def test_a_file_that_is_not_valid_falls_back_to_the_config_list(self):
        for content in ("{broken", "[]", '"NASA"', {"accounts": "NASA"}, {"accounts": ["NASA", "not ok"]},
                        {"accounts": ["NASA", 5]}, {"accounts": ["@NASA"]}, {"accounts": ["NASA\n"]},
                        {"accounts": ["x" * 16]}, {"accounts": [{"handle": "NASA"}]}, {"handles": ["NASA"]}):
            with self.subTest(content=content):
                accounts, out = self.accounts(content)
                self.assertEqual(accounts, self.CONFIG)
                self.assertIn("[warn]", out)
                self.assertIn("x_accounts.json", out)

    def test_the_file_makes_no_request(self):
        session = FakeSession(self.routes())
        os.makedirs(os.path.dirname(self.paths["X_ACCOUNTS"]), exist_ok=True)
        with open(self.paths["X_ACCOUNTS"], "w", encoding="utf-8") as f:
            json.dump({"accounts": ["NASA"]}, f)
        self.run_main(session)
        self.assertFalse([u for u in session.calls if "x.com" in u or "NASA" in u])


class CommittedListTests(unittest.TestCase):
    def test_the_committed_list_is_one_the_build_accepts(self):
        with open(X_ACCOUNTS, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(list(doc), ["accounts"])
        self.assertLessEqual(len(doc["accounts"]), fab.MAX_X_ACCOUNTS)
        for handle in doc["accounts"]:
            self.assertRegex(handle, HANDLE)
        self.assertEqual(len({h.lower() for h in doc["accounts"]}), len(doc["accounts"]))
        self.assertEqual(fab.X_ACCOUNTS, X_ACCOUNTS)
        self.assertEqual(fab.X_HANDLE_RE.pattern, HANDLE[1:-1])  # used with fullmatch
        self.assertEqual(fab.MAX_X_ACCOUNTS, 50)

    def test_the_daily_run_never_overwrites_the_list(self):
        # The list is written by sources.yml alone: daily.yml reads it in the
        # checkout and must not stage, upload or commit a copy of it.
        self.assertNotIn("x_accounts", read(os.path.join(os.path.dirname(WORKFLOW), "daily.yml")))


# ---------------------------------------------------------------- the dashboard, as text

class DashboardTextTests(unittest.TestCase):
    def setUp(self):
        self.app = read(os.path.join(support.ROOT, "docs", "assets", "js", "app.js"))
        self.index = read(os.path.join(support.ROOT, "docs", "index.html"))

    def test_the_page_itself_holds_no_edit_control(self):
        panel = self.index[self.index.index('<section class="panel" id="on-x"'):]
        panel = panel[:panel.index("</section>")]
        self.assertIn('<div class="x-tools" id="x-tools" hidden></div>', panel)
        self.assertIn('<div class="msg" id="x-msg" role="status" aria-live="polite"></div>', panel)
        for tag in ("<form", "<input", "<button"):
            self.assertNotIn(tag, panel)

    def test_the_script_sends_the_edit_to_the_worker_with_the_same_check(self):
        self.assertIn("workerFetch('/x-accounts', {", self.app)
        self.assertIn(f"const X_HANDLE_RE = /{HANDLE}/;", self.app)
        self.assertIn("const X_MAX = 50;", self.app)
        self.assertIn("function canEditX() { return HAS_WORKER && xLoaded && !!getWorkerKey(); }", self.app)
        self.assertIn("Saved. The site updates in a couple of minutes.", self.app)

    def test_a_501_means_local_summaries_without_a_note(self):
        self.assertIn("if (r.kind === 'http' && r.status === 501) {", self.app)
        self.assertIn("summariesOff = true;", self.app)

    def test_plain_punctuation_in_the_new_copy(self):
        start = self.app.index("// ---------- On X ----------")
        section = self.app[start:self.app.index("// ---------- local summary", start)]
        self.assertNotIn(chr(0x2014), section)
        self.assertNotIn(chr(0x2013), section)


def write_worker():
    """Rebuild worker/worker.js: its header as it is, then the four blocks and the last line."""
    header = worker_header(read(WORKER_JS))
    with open(WORKER_JS, "w", encoding="utf-8") as f:
        f.write(header + worker_body())
    print("Wrote worker/worker.js")


if __name__ == "__main__":
    if "--write-worker" in sys.argv:
        write_worker()
    else:
        unittest.main()
