// The whole Worker for the news digest in one file, for a Worker that has no
// summaries code of its own. Paste all of it into an empty Worker (Cloudflare
// dashboard > Workers & Pages > your Worker > Edit code, replacing what is
// there) and click Deploy.
//
// It is three blocks and one last line, in this order:
//   worker/auth-guard.js  the lock: every web request needs the access key
//   worker/scheduler.js   the clock: starts the morning and noon runs on time
//   worker/sources.js     the source editor: add and remove the X accounts
//                         from the dashboard
//   the "export default" line at the very end, which wires the three together
//
// What this Worker needs (Settings > Variables and Secrets, then Settings >
// Trigger Events):
//   ACCESS_KEY         Secret. A long random value (openssl rand -base64 32).
//                      Type the same value into the dashboard's "Set key" button.
//   ALLOWED_ORIGINS    Text. The origin of your dashboard, such as
//                      https://your-name.github.io (scheme and host, no path).
//   GH_DISPATCH_TOKEN  Secret. A fine-grained GitHub token for this one
//                      repository with Actions "Read and write" and nothing else.
//   GH_REPO            Text. owner/name of your repository.
//   Two cron triggers  45 12,13 * * *   and   0 19,20 * * *
//
// This Worker writes no summaries: /analyze, /extract and /yt-transcript
// answer 501, and the dashboard then makes its short local summaries. For
// summaries, keep your own code and paste the three blocks into it as their
// headers describe.
//
// Each block below starts with its own notes. They are written for pasting the
// blocks one at a time, so the pasting steps in them are already done here;
// the settings they describe are the ones listed above.
//
// This file is the three files joined, and a test keeps it that way. Change
// the three files, then rebuild it: python3 tests/test_sources.py --write-worker

// Access guard for the news-extract Cloudflare Worker.
//
// The worker holds your paid API keys (OpenAI, Whisper, ...). Its URL is
// public, because the dashboard calls it from the browser, so without this guard
// anyone who reads app.js can spend your credits or use /extract as a free proxy.
//
// The guard admits a request only when it carries `Authorization: Bearer <ACCESS_KEY>`.
// ACCESS_KEY is a Cloudflare secret, never stored in this repository. Set it in the
// Cloudflare dashboard (Workers & Pages > news-extract > Settings > Variables and
// Secrets > Add > type Secret), or from a terminal:
//
//   npx wrangler secret put ACCESS_KEY --name news-extract
//
// Generate a value with: openssl rand -base64 32
// Type the same value once into the dashboard's "Set key" button.
//
// The origin allowlist only stops other websites from calling the worker
// through a visitor's browser. A script can fake its Origin, so the key is
// the real lock. The allowlist is a setting of your Worker, not of this file:
// in the same Variables and Secrets page add ALLOWED_ORIGINS (type Text) with
// the origin of your dashboard, such as https://your-name.github.io (scheme
// and host only, no path). Several origins are separated by commas; add
// http://localhost:8000 to the list to try the dashboard from your own
// machine. Without ALLOWED_ORIGINS only the local addresses in
// DEFAULT_ALLOWED_ORIGINS below are admitted, so a browser on your published
// dashboard gets 403 "origin not allowed" until you set it.
//
// How to add it (Cloudflare dashboard > Workers & Pages > news-extract > Edit code):
//   1. Paste this whole file at the very top of your worker's code, above everything else.
//   2. Find the line that starts with `export default {` and change only those words to
//      `const original = {`.
//   3. Paste this line at the very bottom of your worker's code:
//
//        export default { ...original, fetch: protect((request, env, ctx) => original.fetch(request, env, ctx)) };
//
//      With the digest scheduler (worker/scheduler.js, which starts the daily
//      GitHub runs from a cron trigger), paste that file right below this one
//      and use this line instead, so the cron events reach dispatchDigest and
//      every web request still goes through the guard:
//
//        export default { ...original, fetch: protect((request, env, ctx) => original.fetch(request, env, ctx)), scheduled: dispatchDigest };
//
//   4. Click Deploy.
//
// If your code has `addEventListener("fetch", ...)` instead of `export default {`, it uses the
// older format. Then skip steps 2 and 3, and inside that addEventListener block replace
//   event.respondWith(handleRequest(event.request))
// with (use your handler's real name if it is not handleRequest)
//   event.respondWith(protect((req) => handleRequest(req))(event.request, { ACCESS_KEY: globalThis.ACCESS_KEY, LIMITER: globalThis.LIMITER, ALLOWED_ORIGINS: globalThis.ALLOWED_ORIGINS }, event))
//
// Optional spend brake: add a Workers rate limiting binding named LIMITER to the
// worker (wrangler: "ratelimits": [{ "name": "LIMITER", "namespace_id": "1001",
// "simple": { "limit": 30, "period": 60 } }]). When it is present, the guard allows
// at most that many requests per minute in total and answers 429 above it.
//
// Check it works: `curl -i https://news-extract.<your-subdomain>.workers.dev/extract?url=https://example.com`
// must answer 401. A 503 means ACCESS_KEY is not set on this worker.

// Used only while the Worker has no ALLOWED_ORIGINS variable.
const DEFAULT_ALLOWED_ORIGINS = [
  // Your dashboard's origin does not go here: set ALLOWED_ORIGINS on the Worker
  // to your GitHub Pages origin, such as https://your-name.github.io (see above).
  "http://localhost:8000", // python3 -m http.server 8000 --bind 127.0.0.1 -d docs
  "http://127.0.0.1:8000",
];

// The origins a browser may call from: the comma-separated ALLOWED_ORIGINS
// variable when it names at least one, else DEFAULT_ALLOWED_ORIGINS.
function allowedOrigins(env) {
  const given = String((env && env.ALLOWED_ORIGINS) || "")
    .split(",")
    .map((origin) => origin.trim().replace(/\/+$/, ""))
    .filter(Boolean);
  return given.length ? given : DEFAULT_ALLOWED_ORIGINS;
}

function corsHeaders(request, env) {
  const origin = request.headers.get("origin") || "";
  const headers = new Headers({
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "authorization, content-type",
    "access-control-max-age": "86400",
    vary: "origin",
  });
  if (allowedOrigins(env).includes(origin)) headers.set("access-control-allow-origin", origin);
  return headers;
}

async function sha256(text) {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
}

// Hashing both sides first makes the comparison constant-time and length-independent.
async function keyMatches(given, expected) {
  const [a, b] = await Promise.all([sha256(given), sha256(expected)]);
  return crypto.subtle.timingSafeEqual(a, b);
}

// The guard's own refusals carry x-guard: deny, so the dashboard can tell them
// apart from a 401 or 503 that your code relays from another site.
function deny(status, message, request, env) {
  const headers = corsHeaders(request, env);
  headers.set("content-type", "application/json");
  headers.set("x-guard", "deny");
  headers.set("access-control-expose-headers", "x-guard");
  return new Response(JSON.stringify({ error: message }), { status, headers });
}

// Returns a Response to send back when the request is refused, or null to let it through.
async function guard(request, env) {
  const origin = request.headers.get("origin");
  if (origin && !allowedOrigins(env).includes(origin)) return deny(403, "origin not allowed", request, env);

  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(request, env) });

  // Fail closed: with no ACCESS_KEY configured, nobody gets in.
  if (!env.ACCESS_KEY) return deny(503, "worker access key is not configured", request, env);

  const auth = request.headers.get("authorization") || "";
  const given = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!given || !(await keyMatches(given, env.ACCESS_KEY))) return deny(401, "unauthorized", request, env);

  if (env.LIMITER) {
    const { success } = await env.LIMITER.limit({ key: "all" });
    if (!success) return deny(429, "rate limited", request, env);
  }

  return null;
}

// The access key must never travel further than this worker: drop it before your
// code runs, so a handler that forwards request headers upstream cannot leak it.
function withoutKey(request) {
  const headers = new Headers(request.headers);
  headers.delete("authorization");
  return new Request(request, { headers });
}

// Adds the CORS headers to your worker's normal response.
function withCors(response, request, env) {
  const out = new Response(response.body, response);
  corsHeaders(request, env).forEach((value, name) => out.headers.set(name, value));
  return out;
}

// guard + withoutKey + withCors around an existing fetch handler.
function protect(handler) {
  return async (request, env, ctx) => {
    const blocked = await guard(request, env);
    if (blocked) return blocked;
    const response = await handler(withoutKey(request), env, ctx);
    return withCors(response, request, env);
  };
}

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

// Source editor for the news digest: lets the dashboard's owner add and remove
// the X accounts in the "On X" panel from the dashboard itself.
//
// The dashboard sends POST /x-accounts with {"action": "add" or "remove",
// "handle": "name"}. This block checks the handle and starts the repository's
// "X accounts" workflow (.github/workflows/sources.yml) through
// workflow_dispatch. The workflow edits data/x_accounts.json, commits it and
// starts a noon run, so the published dashboard shows the change a couple of
// minutes later. The Worker never writes to the repository itself: its token
// can start workflows and nothing else.
//
// Who may edit: handleSources only ever runs behind protect() from
// worker/auth-guard.js, so a request reaches it only with the Worker's access
// key, and a browser only from an origin in ALLOWED_ORIGINS. Visitors of the
// dashboard have no key, so the buttons are not shown to them, and a request
// they send by hand gets 401 from the guard before this code runs.
//
// Setup, once. All four settings are in the Cloudflare dashboard > Workers &
// Pages > your Worker > Settings > Variables and Secrets:
//   ACCESS_KEY         type Secret. The key you type into the dashboard's
//                      "Set key" button (see worker/auth-guard.js).
//   ALLOWED_ORIGINS    type Text. The origin of your dashboard, such as
//                      https://your-name.github.io (scheme and host, no path).
//   GH_DISPATCH_TOKEN  type Secret. The same fine-grained token the scheduler
//                      uses: this one repository, Actions "Read and write",
//                      nothing else (see worker/scheduler.js, step 1).
//   GH_REPO            type Text. owner/name of this repository.
//
// Edit code: paste this whole file right below the scheduler
// (worker/scheduler.js), which sits below the access guard. Then make the last
// line of your Worker one of these two, and click Deploy.
//
//   A Worker that is the clock and the source editor, with no summaries code
//   of its own (worker/worker.js is this, ready to paste in one piece):
//
//        export default { fetch: protect(handleSources), scheduled: dispatchDigest };
//
//   A Worker that also has its own summaries code, kept in `original` as the
//   guard's steps describe: /x-accounts goes to handleSources and every other
//   path to your code.
//
//        export default { ...original, fetch: protect((request, env, ctx) => (new URL(request.url).pathname === "/x-accounts" ? handleSources(request, env, ctx) : original.fetch(request, env, ctx))), scheduled: dispatchDigest };
//
// Answers, all JSON:
//   202 {"ok": true, ...}     GitHub took the request; the workflow does the edit.
//   400 {"error": "..."}      The request is not an add or a remove of a valid handle.
//   405 {"error": "..."}      /x-accounts was called with a method other than POST.
//   500 {"error": "..."}      GH_REPO or GH_DISPATCH_TOKEN is not set on this Worker.
//   502 {"error": "..."}      GitHub refused the request or could not be reached.
//   501 {"error": "summaries are not set up on this worker"}
//                             Any other path. A Worker with only the clock and
//                             this block has no /analyze, /extract or
//                             /yt-transcript; the dashboard then writes its
//                             short local summaries without an error note.
//
// Check it (use your own address and key; a wrong key must answer 401):
//   curl -i -X POST https://<your-worker>.workers.dev/x-accounts \
//     -H "Authorization: Bearer <ACCESS_KEY>" -H "Content-Type: application/json" \
//     -d '{"action":"add","handle":"NASA"}'
// It answers 202, and the repository's Actions tab shows "X accounts (add @NASA)".
//
// The token is sent to api.github.com only and never logged or sent back.

const SOURCES_WORKFLOW = "sources.yml";
const SOURCES_BRANCH = "main";
const SOURCES_PATH = "/x-accounts";
const SOURCES_HANDLE = /^[A-Za-z0-9_]{1,15}$/; // an X handle; the workflow checks it again
const SOURCES_MAX_BODY = 2000; // characters of JSON accepted in a request

function sourcesAnswer(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

// GitHub's short error message, never the request: the request holds the token.
async function sourcesErrorDetail(response) {
  try {
    const text = await response.text();
    const message = JSON.parse(text).message;
    return String(message || text).slice(0, 200);
  } catch {
    return "";
  }
}

// One request to start the workflow. { ok } when GitHub took it; otherwise
// what went wrong, and whether a second request is worth sending (a 5xx, or no
// answer at all). A second request is safe: adding a handle that is there, or
// removing one that is not, changes nothing.
async function sourcesDispatch(repo, action, handle, env) {
  const url = `https://api.github.com/repos/${repo}/actions/workflows/${SOURCES_WORKFLOW}/dispatches`;
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "news-digest-sources",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: SOURCES_BRANCH, inputs: { action, handle } }),
    });
    if (response.ok) return { ok: true, status: response.status };
    const problem = `GitHub answered ${response.status} ${await sourcesErrorDetail(response)}`.trim();
    return { ok: false, problem, again: response.status >= 500 };
  } catch (err) {
    return { ok: false, problem: `GitHub could not be reached (${err && err.name ? err.name : "error"})`, again: true };
  }
}

// The fetch handler. Wrap it in protect(): it does not check the access key itself.
async function handleSources(request, env, ctx) {
  const path = new URL(request.url).pathname.replace(/\/+$/, "");
  if (path !== SOURCES_PATH) return sourcesAnswer(501, { error: "summaries are not set up on this worker" });
  if (request.method !== "POST") return sourcesAnswer(405, { error: "use POST" });

  let body = null;
  try {
    const text = await request.text();
    if (text.length <= SOURCES_MAX_BODY) body = JSON.parse(text);
  } catch {
    body = null;
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return sourcesAnswer(400, { error: "send JSON with an action and a handle" });
  }
  const action = body.action;
  if (action !== "add" && action !== "remove") return sourcesAnswer(400, { error: "the action must be add or remove" });
  const handle = typeof body.handle === "string" ? body.handle.trim().replace(/^@/, "") : "";
  if (!SOURCES_HANDLE.test(handle)) {
    return sourcesAnswer(400, { error: "the handle must be 1 to 15 letters, numbers or underscores" });
  }

  const repo = String(env.GH_REPO || "");
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo) || !env.GH_DISPATCH_TOKEN) {
    console.log("News digest: set GH_REPO (owner/name) and the GH_DISPATCH_TOKEN secret on this Worker");
    return sourcesAnswer(500, { error: "this worker is not set up to edit the list: set GH_REPO and GH_DISPATCH_TOKEN" });
  }

  let result = await sourcesDispatch(repo, action, handle, env);
  if (!result.ok && result.again) result = await sourcesDispatch(repo, action, handle, env);
  if (!result.ok) {
    console.log(`News digest: the request to ${action} @${handle} did not start: ${result.problem}`);
    return sourcesAnswer(502, { error: `the change was not saved: ${result.problem}` });
  }
  console.log(`News digest: asked GitHub to ${action} @${handle}, GitHub answered ${result.status}`);
  return sourcesAnswer(202, { ok: true, action, handle });
}

export default { fetch: protect(handleSources), scheduled: dispatchDigest };
