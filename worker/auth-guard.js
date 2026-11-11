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
