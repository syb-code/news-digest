// Scheduler for the news digest: a Cloudflare cron trigger that starts the
// GitHub Actions runs on time.
//
// GitHub's own schedule runs this repository's workflows hours late, so the
// Worker that already serves the dashboard's summaries starts them instead,
// through workflow_dispatch on .github/workflows/daily.yml:
//   05:45 America/Los_Angeles  run "morning"  feeds, briefing, email, dashboard
//   12:00 America/Los_Angeles  run "noon"     feeds and dashboard only
//
// Cloudflare cron triggers run in UTC, and Los Angeles is UTC-7 in summer and
// UTC-8 in winter, so each local time gets one expression that fires at both
// UTC hours. dispatchDigest then works out the local time of the firing and
// starts a run only when it is exactly 05:45 or 12:00 there: one run per time,
// every day, on both sides of a clock change.
//
//   45 12,13 * * *   (12:45 UTC is 05:45 PDT, 13:45 UTC is 05:45 PST)
//   0 19,20 * * *    (19:00 UTC is 12:00 PDT, 20:00 UTC is 12:00 PST)
//
// To change the times, change DIGEST_RUNS below and both cron expressions,
// and the schedule note in config.yml.
//
// Setup, once:
//
//   1. A GitHub token that can only start this workflow. GitHub > Settings >
//      Developer settings > Personal access tokens > Fine-grained tokens >
//      Generate new token. Repository access: "Only select repositories" and
//      pick this one repository. Permissions: Repository permissions >
//      Actions: "Read and write". Nothing else (GitHub adds Metadata:
//      read-only to every token by itself). Set an expiry and a reminder to
//      replace the token before it runs out.
//
//   2. Cloudflare dashboard > Workers & Pages > your Worker > Settings >
//      Variables and Secrets:
//        GH_DISPATCH_TOKEN  type Secret, the token from step 1
//        GH_REPO            type Text, owner/name of this repository, such as
//                           your-name/news-digest
//      Or from a terminal: npx wrangler secret put GH_DISPATCH_TOKEN --name <worker>
//
//   3. Same Worker > Settings > Trigger Events > Add > Cron Triggers. Add the
//      two expressions above, one at a time, exactly as written.
//
//   4. Edit code: paste this whole file right below the access guard
//      (worker/auth-guard.js) at the top of your Worker, then change the last
//      line of your Worker, the one the guard's steps added, to:
//
//        export default { ...original, fetch: protect((request, env, ctx) => original.fetch(request, env, ctx)), scheduled: dispatchDigest };
//
//      If your own code already has a scheduled handler, call both from one
//      function instead. Click Deploy.
//
//   5. Check it: in the GitHub repository, set the variable DIGEST_ENABLED to
//      true (Settings > Secrets and variables > Actions > Variables). After the
//      next 05:45 or 12:00, Workers > your Worker > Logs shows "News digest:
//      started the morning run", and the Actions tab shows the run. A firing
//      at the other UTC hour logs "not a digest time": that is expected.
//      Locally: npx wrangler dev --test-scheduled, then open
//      http://localhost:8787/__scheduled?cron=45+12,13+*+*+*
//
// One firing never starts two runs. A second request is sent only when the
// first one cannot have started a run:
//   GitHub answers 2xx       the run started; done.
//   GitHub answers 4xx       refused (token, repository, workflow); no retry.
//   GitHub answers 5xx       it sometimes starts the run all the same, so the
//                            Worker waits, then reads the list of runs. A run
//                            of this kind created in the last 2 minutes means
//                            it started: done. None: one retry. If the list
//                            cannot be read, no retry.
//   GitHub is not reachable  the same wait and look, then one retry unless
//                            the list shows the run.
// A firing that could not start its run throws, so the cron event shows as
// failed in the Worker's logs; start the run by hand from the Actions tab
// (Daily digest > Run workflow).
//
// The token is sent to api.github.com only and never logged. Nothing in this
// block needs the dashboard's access key, and the scheduled handler is not
// reachable from the web.

const DIGEST_TIME_ZONE = "America/Los_Angeles";
const DIGEST_RUNS = { "05:45": "morning", "12:00": "noon" }; // local time -> workflow input "run"
const DIGEST_WORKFLOW = "daily.yml";
const DIGEST_BRANCH = "main";
const DIGEST_RETRY_MS = 10000; // wait this long before looking for the run, and before the one retry
const DIGEST_RECENT_MS = 120000; // a run created this recently is the one this firing asked for

// "HH:MM" of a moment in DIGEST_TIME_ZONE, 24-hour clock.
function digestLocalTime(when) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: DIGEST_TIME_ZONE,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(when));
  const part = (type) => (parts.find((p) => p.type === type) || { value: "" }).value;
  return `${part("hour")}:${part("minute")}`;
}

// GitHub's short error message, never the request: the request holds the token.
async function digestErrorDetail(response) {
  try {
    const text = await response.text();
    const message = JSON.parse(text).message;
    return String(message || text).slice(0, 200);
  } catch {
    return "";
  }
}

function digestHeaders(env) {
  return {
    Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "news-digest-scheduler",
  };
}

// One request to start the run. { ok } when GitHub took it; otherwise what went
// wrong, and whether GitHub may have started the run all the same (a 5xx, or
// no answer at all).
async function digestDispatch(repo, run, env) {
  const url = `https://api.github.com/repos/${repo}/actions/workflows/${DIGEST_WORKFLOW}/dispatches`;
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { ...digestHeaders(env), "Content-Type": "application/json" },
      body: JSON.stringify({ ref: DIGEST_BRANCH, inputs: { run } }),
    });
    if (response.ok) return { ok: true, status: response.status };
    const problem = `GitHub answered ${response.status} ${await digestErrorDetail(response)}`.trim();
    return { ok: false, problem, unsure: response.status >= 500, answered: true };
  } catch (err) {
    const problem = `GitHub could not be reached (${err && err.name ? err.name : "error"})`;
    return { ok: false, problem, unsure: true, answered: false };
  }
}

// Whether a run of this kind was created in the last DIGEST_RECENT_MS: true,
// false, or null when the list of runs cannot be read. The workflow names its
// runs "Daily digest (morning)" and "Daily digest (noon)".
async function digestRunExists(repo, run, env) {
  const url = `https://api.github.com/repos/${repo}/actions/workflows/${DIGEST_WORKFLOW}/runs`
    + `?event=workflow_dispatch&branch=${DIGEST_BRANCH}&per_page=20`;
  try {
    const response = await fetch(url, { method: "GET", headers: digestHeaders(env) });
    if (!response.ok) return null;
    const runs = (await response.json()).workflow_runs;
    if (!Array.isArray(runs)) return null;
    const since = Date.now() - DIGEST_RECENT_MS;
    return runs.some((r) => r && Date.parse(r.created_at) >= since
      && (typeof r.display_title !== "string" || r.display_title.includes(`(${run})`)));
  } catch {
    return null;
  }
}

// The scheduled handler. event.scheduledTime is the minute the cron fired for.
async function dispatchDigest(event, env, ctx) {
  const local = digestLocalTime(event.scheduledTime);
  const run = DIGEST_RUNS[local];
  if (!run) {
    console.log(`News digest: ${local} in ${DIGEST_TIME_ZONE} is not a digest time, nothing to start`);
    return;
  }
  const repo = String(env.GH_REPO || "");
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo) || !env.GH_DISPATCH_TOKEN) {
    // Thrown, so the cron event shows as failed in the Worker's logs.
    throw new Error("News digest: set GH_REPO (owner/name) and the GH_DISPATCH_TOKEN secret on this Worker");
  }
  let result = await digestDispatch(repo, run, env);
  if (!result.ok && result.unsure) {
    console.log(`News digest: the ${run} run may not have started: ${result.problem}. Looking for it before any retry`);
    await new Promise((resolve) => setTimeout(resolve, DIGEST_RETRY_MS));
    const exists = await digestRunExists(repo, run, env);
    if (exists === true) {
      console.log(`News digest: the ${run} run did start, so no second request was sent`);
      return;
    }
    if (exists === null && result.answered) {
      // GitHub answered 5xx and the list cannot be read: the run may exist, so a retry could start a second one.
      throw new Error(`News digest: the ${run} run may not have started: ${result.problem}, and the list of runs `
        + "could not be read. No retry was sent, so check the Actions tab");
    }
    result = await digestDispatch(repo, run, env);
  }
  if (result.ok) {
    console.log(`News digest: started the ${run} run, GitHub answered ${result.status}`);
    return;
  }
  console.log(`News digest: the ${run} run did not start: ${result.problem}`);
  throw new Error(`News digest: the ${run} run did not start: ${result.problem}`);
}
