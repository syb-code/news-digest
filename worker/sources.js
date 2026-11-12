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
