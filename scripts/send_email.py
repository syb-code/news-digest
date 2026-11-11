#!/usr/bin/env python3
"""Send the morning digest email through the Gmail API.

This script runs in the only job that holds the Gmail secrets, so it uses the
Python standard library and nothing else: no third-party package is installed
or imported where the secrets are.

The digest lists the items whose published time falls in [start, end):

  end    when this run was created (created_at of GITHUB_RUN_ID, read from the
         GitHub REST API with the job's read-only token), or the time the
         script runs when that cannot be read. created_at stays the same when
         a run is re-run; run_started_at does not, and a re-run sends from the
         data its first attempt built, so the window must not move with it.
  start  when the previous digest's run was created: the latest completed
         morning run of daily.yml on main, created before this one, whose
         "email" job ended in success (GET /actions/runs/{id}/jobs). A run
         counts when its email went out even if another job failed, so those
         items do not come twice; a day GitHub skipped, or a run whose email
         failed, is covered by the next email, up to 7 days back. With no
         such run (none in the last 7 days, the API cannot be reached, or a
         local run without GITHUB_TOKEN), start is 24 hours before end.

Late arrivals: a feed can list an item some time after its published time.
When start comes from a previous run, an item published up to a day before
start is added if its first_seen is more than an hour after start: the
previous run had already built its data by then, so that email did not list
it. Items without first_seen (from an older items.json) never count as late.

When docs/learn/<date>/briefing.json exists for today's date (in the time zone
of config.yml), the email opens with a "Today's briefing" section: each topic
linked to <SITE_URL>/learn/<date>/, the first sentence of why it matters, and
the briefing's summary. Without the file the section is left out.

Some items can still be missed: one missing from items.json when the email is
built. That happens when the refresh step fails and the copy committed on main
is used; the email then says so.
"""
import base64
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DATA = os.path.join(ROOT, "docs", "data", "items.json")
LEARN_DIR = os.path.join(ROOT, "docs", "learn")
CONFIG_PATH = os.path.join(ROOT, "config.yml")

GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
# Scopes a send-only token may carry. Anything else (https://mail.google.com/
# can read and delete the mailbox) prints a ::warning:: on every run; the email
# still sends.
ALLOWED_SCOPES = {SEND_SCOPE, "openid", "https://www.googleapis.com/auth/userinfo.email"}

ENV_NAMES = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "SENDER_EMAIL", "RECIPIENT_EMAIL")
WINDOW_LENGTH = timedelta(hours=24)
# How far back the start may reach to cover skipped or failed days.
MAX_CATCH_UP = timedelta(days=7)
# Late arrivals (see the module docstring): how far before start an item may be
# published, and how long after its start the previous run had its data.
LATE_LOOKBACK = timedelta(hours=24)
BUILD_SLACK = timedelta(hours=1)
MAX_PER_SECTION = 40
HTTP_TIMEOUT = 30

GITHUB_API = "https://api.github.com"
WORKFLOW_FILE = "daily.yml"
EMAIL_JOB = "email"         # the job of daily.yml that runs this script
# The email job only runs on main, so only runs on main have sent an email.
WORKFLOW_BRANCH = "main"
MORNING_MARK = "(morning)"  # run-name of daily.yml: "Daily digest (morning)"
RUNS_PER_PAGE = 50          # two runs a day for 7 days, plus manual ones
JOBS_PER_PAGE = 100         # six jobs a run, times its attempts
MAX_JOB_LOOKUPS = 10        # morning runs whose jobs are read: one a day for 7 days, plus manual ones
GITHUB_TIMEOUT = 15
FALLBACK_NOTE = (
    "Built from the last committed data because the refresh step failed. Some recent items may be missing."
)

DEFAULT_TIMEZONE = "America/Los_Angeles"
TIMEZONE_RE = re.compile(r"""^timezone:[ \t]*["']?([A-Za-z0-9_+\-/]+)["']?[ \t]*(?:#.*)?$""", re.MULTILINE)
SITE_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~%/-]*)?")
REPO_RE = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
MAX_BRIEFING_BYTES = 1024 * 1024
MAX_TOPICS = 8
MAX_TITLE = 200
MAX_WHY = 320
MAX_SUMMARY = 1200

# Brand palette (light theme: email clients vary too much for dark mode).
BG = "#f4f3ef"
CARD = "#fbfaf7"
INK = "#0d0d0d"
MUTED = "#55554f"
LINE = "#d6d4cd"
ACCENT = "#1f6f7a"
OXBLOOD = "#6f291e"
SANS = "'Geist', 'Helvetica Neue', Arial, sans-serif"
MONO = "'Geist Mono', ui-monospace, Menlo, Consolas, monospace"


def safe_url(u):
    """Feed links are untrusted: only http(s) links are rendered, anything else becomes '#'."""
    u = str(u or "").strip()
    return u if u.lower().startswith(("https://", "http://")) else "#"


def check_env():
    """Return the five settings, or exit naming the missing ones (names only)."""
    missing = [n for n in ENV_NAMES if not os.environ.get(n)]
    if missing:
        # Names only; never print a secret's value.
        sys.exit("Missing secrets in the digest environment: " + ", ".join(missing))
    return {n: os.environ[n] for n in ENV_NAMES}


# ---------------------------------------------------------------- settings

def load_timezone(path=None):
    """The time zone named by the top-level timezone line of config.yml (read
    with a pattern, since this script has no YAML parser), else
    America/Los_Angeles, else UTC when this machine has no time zone data."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    name = DEFAULT_TIMEZONE
    try:
        with open(path or CONFIG_PATH, "r", encoding="utf-8") as f:
            m = TIMEZONE_RE.search(f.read())
        if m:
            name = m.group(1)
    except OSError:
        pass
    for candidate in (name, DEFAULT_TIMEZONE):
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            print(f"Time zone {candidate!r} is not available here.")
    return timezone.utc


def site_url(environ=None):
    """Where the dashboard is published, ending in "/": SITE_URL when it is a
    plain https URL, else the GitHub Pages address of GITHUB_REPOSITORY, else None."""
    environ = os.environ if environ is None else environ
    given = str(environ.get("SITE_URL") or "").strip()
    if given:
        if SITE_URL_RE.fullmatch(given):
            return given.rstrip("/") + "/"
        print("SITE_URL is not a plain https:// address, so the Pages address is used instead.")
    m = REPO_RE.fullmatch(str(environ.get("GITHUB_REPOSITORY") or ""))
    if not m:
        return None
    owner, repo = m.group(1).lower(), m.group(2)
    if repo.lower() == f"{owner}.github.io":
        return f"https://{owner}.github.io/"
    return f"https://{owner}.github.io/{repo}/"


# ---------------------------------------------------------------- HTTP (stdlib)

def _post(url, data, headers, what):
    """POST and return the parsed JSON reply. On failure exit with the HTTP
    status and at most 300 characters of the reply body. The request data and
    the environment are never printed."""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read(4096).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        body = " ".join(body.split())[:300]
        sys.exit(f"{what} failed: HTTP {e.code} {body}")
    except urllib.error.URLError as e:
        sys.exit(f"{what} failed: {e.reason}")
    except (OSError, ValueError) as e:
        sys.exit(f"{what} failed: {type(e).__name__}")


def get_access_token(client_id, client_secret, refresh_token):
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("ascii")
    tok = _post(GMAIL_TOKEN_URL, data, {"Content-Type": "application/x-www-form-urlencoded"}, "Token refresh")
    if not isinstance(tok, dict):
        sys.exit("Token refresh failed: unexpected reply")
    scopes = set(str(tok.get("scope") or "").split())
    extra = scopes - ALLOWED_SCOPES
    if extra:
        # Keep sending, but flag it on the run page. Scope names are public identifiers, not secrets.
        print(
            "::warning::The Gmail refresh token can do more than send mail (" + " ".join(sorted(extra))
            + "). Mint a send-only token (scripts/oauth_local_example.py), update GMAIL_REFRESH_TOKEN,"
            " then revoke the old grant at https://myaccount.google.com/permissions."
        )
    if not tok.get("access_token"):
        sys.exit("Token refresh failed: no access_token in the reply")
    return tok["access_token"]


def send_message(access_token, msg):
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    data = json.dumps({"raw": raw}).encode("utf-8")
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    return _post(GMAIL_SEND_URL, data, headers, "Gmail send")


def _github_get(url, token):
    """GET a GitHub REST API URL with the job's token. Returns (data, None) or
    (None, a short reason). The token is never printed."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=GITHUB_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, str(e.reason)
    except (OSError, ValueError) as e:
        return None, type(e).__name__


def _github_env(environ):
    """(token, repo) when both are usable, else None."""
    token = environ.get("GITHUB_TOKEN")
    repo = environ.get("GITHUB_REPOSITORY") or ""
    if not token or not REPO_RE.fullmatch(repo):
        return None
    return token, repo


# ---------------------------------------------------------------- window

def run_created(run):
    """When a run was created. created_at is the same for every attempt of a
    run; run_started_at moves to the start of the latest re-run, so it is only
    used when created_at is missing."""
    if not isinstance(run, dict):
        return None
    return parse_published(run.get("created_at")) or parse_published(run.get("run_started_at"))


def current_run_start(environ=None):
    """When this run was created (created_at of GITHUB_RUN_ID, the same on a
    re-run), or None with a printed note."""
    environ = os.environ if environ is None else environ
    gh = _github_env(environ)
    run_id = str(environ.get("GITHUB_RUN_ID") or "")
    if gh is None or not run_id.isdigit():
        print("No GITHUB_TOKEN, GITHUB_REPOSITORY or GITHUB_RUN_ID, so the digest ends now.")
        return None
    token, repo = gh
    data, why = _github_get(f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}", token)
    started = run_created(data)
    if started is None:
        print(f"Could not read when this run started ({why or 'unexpected reply'}), so the digest ends now.")
    return started


def email_job_succeeded(repo, run_id, token):
    """(True or False, None): whether the email job of a run ended in success
    in any of its attempts. (None, a short reason) when that cannot be read."""
    query = urllib.parse.urlencode({"filter": "all", "per_page": JOBS_PER_PAGE})
    data, why = _github_get(f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}/jobs?{query}", token)
    if why is not None:
        return None, why
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return None, "unexpected reply"
    sent = any(isinstance(j, dict) and j.get("name") == EMAIL_JOB and j.get("conclusion") == "success" for j in jobs)
    return sent, None


def previous_run_time(environ=None, end=None):
    """When the run of the previous digest was created: the latest completed
    morning run of daily.yml on main whose email job ended in success, from the
    GitHub REST API with the job's read-only token. None, with a printed note,
    when there is none or it cannot be read. The current run (GITHUB_RUN_ID),
    noon runs, and with end the runs created at or after end or more than
    MAX_CATCH_UP before it, are skipped. The token is never printed."""
    environ = os.environ if environ is None else environ
    gh = _github_env(environ)
    if gh is None:
        print("No GITHUB_TOKEN or GITHUB_REPOSITORY, so the digest covers the 24 hours before its end.")
        return None
    token, repo = gh
    current = str(environ.get("GITHUB_RUN_ID") or "")
    query = urllib.parse.urlencode({"status": "completed", "branch": WORKFLOW_BRANCH, "per_page": RUNS_PER_PAGE})
    data, why = _github_get(f"{GITHUB_API}/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs?{query}", token)
    if why is not None:
        print(f"Could not read the previous run ({why}), so the digest covers the 24 hours before its end.")
        return None
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    candidates = []
    too_old = False
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict) or str(run.get("id")) == current or not str(run.get("id")).isdigit():
            continue
        if MORNING_MARK not in str(run.get("display_title") or ""):
            continue
        created = run_created(run)
        if created is None or (end is not None and created >= end):
            continue
        if end is not None and created <= end - MAX_CATCH_UP:
            too_old = True
            continue
        candidates.append((created, str(run["id"])))
    candidates.sort(reverse=True)
    for created, run_id in candidates[:MAX_JOB_LOOKUPS]:
        sent, why = email_job_succeeded(repo, run_id, token)
        if sent is None:
            print(f"Could not read the jobs of the previous run ({why}), so the digest covers the 24 hours"
                  " before its end.")
            return None
        if sent:
            return created
    if too_old:
        print("The previous morning digest is more than 7 days back, so the digest covers the 24 hours"
              " before its end.")
    else:
        print("No previous morning run that sent its email was found, so the digest covers the 24 hours"
              " before its end.")
    return None


def window_start(end, prev_start):
    """Start of the digest that ends at end: prev_start (when the run of the
    previous digest was created) when it is before end and less than
    MAX_CATCH_UP before it, else WINDOW_LENGTH before end."""
    if prev_start is not None and end - MAX_CATCH_UP < prev_start < end:
        return prev_start
    return end - WINDOW_LENGTH


def parse_published(value):
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def select_window(items, start, end, late_after=None):
    """Items published in [start, end), newest first. With late_after, also the
    late arrivals: published up to LATE_LOOKBACK before start and first seen
    after late_after."""
    out = []
    for it in items:
        dt = parse_published(it.get("published"))
        if dt is None:
            continue
        if start <= dt < end:
            out.append((dt, it))
        elif late_after is not None and start - LATE_LOOKBACK <= dt < start:
            seen = parse_published(it.get("first_seen"))
            if seen is not None and seen > late_after:
                out.append((dt, it))
    out.sort(key=lambda pair: pair[0], reverse=True)
    return [it for _, it in out]


def split_sections(items):
    quick = [i for i in items if i.get("bucket") != "deeper"]
    deeper = [i for i in items if i.get("bucket") == "deeper"]
    return [("Quick reads", quick), ("Long reads and videos", deeper)]


def load_data(path=None):
    with open(path or DOCS_DATA, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return [], []
    items = [i for i in data.get("items") or [] if isinstance(i, dict)]
    sources = [s for s in data.get("sources") or [] if isinstance(s, dict)]
    return items, sources


# ---------------------------------------------------------------- briefing

def one_line(value):
    return " ".join(str(value or "").split())


def clip(text, limit):
    if len(text) <= limit:
        return text
    cut = text[: limit - 3]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.") + "..."


def first_sentence(text, limit=MAX_WHY):
    text = one_line(text)
    return clip(SENTENCE_END_RE.split(text, 1)[0], limit) if text else ""


def load_briefing(date, learn_dir=None):
    """Today's briefing from docs/learn/<date>/briefing.json, reduced to what the
    email shows, or None when there is none to show. The file is generated
    from feed text, so every value is checked here and escaped when rendered."""
    if not DATE_RE.fullmatch(date):
        return None
    path = os.path.join(learn_dir or LEARN_DIR, date, "briefing.json")
    try:
        if os.path.getsize(path) > MAX_BRIEFING_BYTES:
            print("The briefing file is too large, so the email leaves it out.")
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"No briefing for {date}, so the email leaves that section out.")
        return None
    except (OSError, ValueError) as e:
        print(f"Could not read the briefing ({type(e).__name__}), so the email leaves it out.")
        return None
    if not isinstance(data, dict) or data.get("date") != date or not isinstance(data.get("topics"), list):
        print("The briefing file is not for today or has no topic list, so the email leaves it out.")
        return None
    topics = []
    for t in data["topics"]:
        if not isinstance(t, dict):
            continue
        title = clip(one_line(t.get("title")), MAX_TITLE)
        if not title:
            continue
        lanes = t.get("lanes") if isinstance(t.get("lanes"), list) else []
        minutes = t.get("reading_minutes")
        topics.append({
            "title": title,
            "why": first_sentence(t.get("why_it_matters")),
            "lanes": [clip(one_line(lane), 40) for lane in lanes[:3] if one_line(lane)],
            "off_lane": t.get("off_lane") is True,
            "minutes": minutes if isinstance(minutes, int) and not isinstance(minutes, bool) and 0 < minutes < 60 else None,
        })
        if len(topics) == MAX_TOPICS:
            break
    if not topics:
        print("The briefing has no topics, so the email leaves it out.")
        return None
    return {
        "date": date,
        "topics": topics,
        "summary": clip(one_line(data.get("email_summary")), MAX_SUMMARY),
        "llm": data.get("llm") is not False,
    }


def briefing_url(base, date):
    return f"{base}learn/{date}/" if base else None


def topic_tags(topic):
    tags = ["Outside the lanes"] if topic["off_lane"] else list(topic["lanes"])
    if topic["minutes"]:
        tags.append(f"{topic['minutes']} min")
    return tags


NO_MODEL_NOTE = "Built from the headlines alone today, without the model."


# ---------------------------------------------------------------- rendering

def esc(value):
    return html.escape(str(value or ""), quote=True)


def _local(dt, tz):
    return dt.astimezone(tz or timezone.utc)


def format_day(dt, tz=None):
    d = _local(dt, tz)
    return f"{d:%A}, {d:%B} {d.day}"


def format_stamp(dt, tz=None):
    d = _local(dt, tz)
    hour = d.hour % 12 or 12
    return f"{d:%a} {d:%b} {d.day}, {hour}:{d:%M} {'AM' if d.hour < 12 else 'PM'} {d.tzname()}"


def time_tag(item):
    if item.get("source_type") == "youtube":
        return "Short" if "/shorts/" in str(item.get("url") or "") else "Video"
    try:
        minutes = max(1, int(item.get("read_minutes") or 1))
    except (TypeError, ValueError):
        minutes = 1
    return f"{minutes} min read"


LABEL_STYLE = f"font-family:{MONO};font-size:11px;line-height:16px;letter-spacing:0.08em;"
SEP = "&nbsp;&nbsp;&middot;&nbsp;&nbsp;"


def label_html(text, color=MUTED):
    # Upper-cased before escaping, so entities such as &amp; stay valid.
    return f'<span style="{LABEL_STYLE}color:{color};">{esc(str(text).upper())}</span>'


def section_head(title):
    return (
        f'<tr><td style="padding:28px 32px 0 32px;">'
        f'<div style="padding:0 0 8px 0;border-bottom:1px solid {INK};">{label_html(title, INK)}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
    )


SECTION_END = "</table></td></tr>"


def item_html(item):
    """One row. Styles sit on as few elements as possible: Gmail clips a
    message whose HTML passes about 102 KB."""
    url = esc(safe_url(item.get("url")))
    title = esc(item.get("title") or "Untitled")
    tags = esc(str(item.get("source") or "").upper()) + SEP + esc(time_tag(item).upper())
    summary = str(item.get("summary") or "")
    if len(summary) > 200:
        summary = summary[:197].rsplit(" ", 1)[0] + "..."
    themes = ", ".join(str(t) for t in item.get("themes") or [])
    parts = [
        f'<tr><td style="padding:14px 0;border-bottom:1px solid {LINE};font-family:{SANS};">',
        f'<div style="{LABEL_STYLE}color:{MUTED};margin:0 0 4px 0;">{tags}</div>',
        f'<a href="{url}" style="font-size:16px;line-height:22px;font-weight:600;color:{ACCENT};'
        f'text-decoration:none;">{title}</a>',
    ]
    if summary:
        parts.append(f'<div style="font-size:14px;line-height:20px;color:{MUTED};margin:4px 0 0 0;">{esc(summary)}</div>')
    if themes:
        parts.append(f'<div style="{LABEL_STYLE}color:{MUTED};margin:6px 0 0 0;">{esc(themes.upper())}</div>')
    parts.append("</td></tr>\n")
    return "".join(parts)


def section_html(title, items):
    shown = items[:MAX_PER_SECTION]
    rows = "".join(item_html(i) for i in shown)
    more = ""
    if len(items) > len(shown):
        more = (
            f'<tr><td style="padding:12px 0 0 0;font-family:{SANS};font-size:13px;line-height:18px;'
            f'color:{MUTED};">{len(items) - len(shown)} more on the dashboard.</td></tr>'
        )
    return section_head(f"{title} · {len(items)}") + rows + more + SECTION_END


def briefing_html(briefing, base):
    url = briefing_url(base, briefing["date"])
    rows = []
    for t in briefing["topics"]:
        tags = SEP.join(esc(tag.upper()) for tag in topic_tags(t))
        title = esc(t["title"])
        link_style = f"font-size:16px;line-height:22px;font-weight:600;color:{ACCENT};text-decoration:none;"
        heading = (f'<a href="{esc(url)}" style="{link_style}">{title}</a>' if url
                   else f'<span style="{link_style}">{title}</span>')
        tag_div = f'<div style="{LABEL_STYLE}color:{MUTED};margin:0 0 4px 0;">{tags}</div>' if tags else ""
        rows.append(
            f'<tr><td style="padding:14px 0;border-bottom:1px solid {LINE};font-family:{SANS};">'
            f'{tag_div}{heading}'
            + (f'<div style="font-size:14px;line-height:20px;color:{MUTED};margin:4px 0 0 0;">{esc(t["why"])}</div>'
               if t["why"] else "")
            + "</td></tr>\n"
        )
    after = []
    if briefing["summary"]:
        after.append(f'<div style="font-size:15px;line-height:22px;color:{INK};margin:0 0 8px 0;">'
                     f'{esc(briefing["summary"])}</div>')
    if url:
        after.append(f'<a href="{esc(url)}" style="font-size:14px;line-height:20px;font-weight:600;color:{ACCENT};'
                     f'text-decoration:none;">Open the slides</a>')
    if not briefing["llm"]:
        after.append(f'<div style="font-size:13px;line-height:18px;color:{MUTED};margin:8px 0 0 0;">'
                     f'{esc(NO_MODEL_NOTE)}</div>')
    tail = (f'<tr><td style="padding:14px 0 0 0;font-family:{SANS};">{"".join(after)}</td></tr>' if after else "")
    return section_head(f"Today's briefing · {len(briefing['topics'])}") + "".join(rows) + tail + SECTION_END


def build_html(items, sources, start, end, fallback=False, briefing=None, base=None, tz=None):
    sections = [(t, arr) for t, arr in split_sections(items) if arr]
    body = briefing_html(briefing, base) if briefing else ""
    if sections:
        body += "".join(section_html(t, arr) for t, arr in sections)
    else:
        body += (
            f'<tr><td style="padding:28px 32px 0 32px;font-family:{SANS};font-size:16px;line-height:24px;'
            f'color:{INK};">Nothing new since the last digest.</td></tr>'
        )
    failed = [s for s in sources if s.get("ok") is False]
    footer_lines = []
    if failed:
        names = "; ".join(
            esc(s.get("label")) + (f" &middot; {esc(s.get('error'))}" if s.get("error") else "") for s in failed
        )
        footer_lines.append(f"Sources that could not be fetched this run: {names}.")
    footer_lines.append("Sent by your news digest workflow.")
    footer = "".join(
        f'<p style="margin:0 0 6px 0;font-family:{SANS};font-size:12px;line-height:18px;color:{MUTED};">{line}</p>'
        for line in footer_lines
    )
    window = f"Published {esc(format_stamp(start, tz))} to {esc(format_stamp(end, tz))}"
    note = ""
    if fallback:
        note = (
            f'<div style="margin:4px 0 0 0;font-family:{SANS};font-size:13px;line-height:18px;'
            f'color:{MUTED};">{esc(FALLBACK_NOTE)}</div>'
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>News digest</title>
</head>
<body style="margin:0;padding:0;background-color:{BG};color:{INK};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{BG};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:640px;background-color:{CARD};border:1px solid {LINE};">
<tr><td style="padding:28px 32px 0 32px;">
<div style="margin:0 0 6px 0;">{label_html("Daily digest · " + format_day(end, tz))}</div>
<h1 style="margin:0;font-family:{SANS};font-size:28px;line-height:34px;font-weight:600;color:{INK};">News digest</h1>
<div style="margin:6px 0 0 0;font-family:{SANS};font-size:13px;line-height:18px;color:{MUTED};">{window}</div>{note}
</td></tr>
<tr><td style="padding:18px 32px 0 32px;"><div style="border-top:3px solid {OXBLOOD};font-size:0;line-height:0;">&nbsp;</div></td></tr>
{body}
<tr><td style="padding:28px 32px 28px 32px;">
<div style="border-top:1px solid {LINE};padding:14px 0 0 0;">{footer}</div>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""


def briefing_text(briefing, base):
    url = briefing_url(base, briefing["date"])
    lines = [f"Today's briefing ({len(briefing['topics'])})", ""]
    for t in briefing["topics"]:
        lines.append(f"- {t['title']}")
        if topic_tags(t):
            lines.append(f"  {' · '.join(topic_tags(t))}")
        if t["why"]:
            lines.append(f"  {t['why']}")
    lines.append("")
    if briefing["summary"]:
        lines += [briefing["summary"], ""]
    if url:
        lines += [f"Slides: {url}", ""]
    if not briefing["llm"]:
        lines += [NO_MODEL_NOTE, ""]
    return lines


def build_text(items, sources, start, end, fallback=False, briefing=None, base=None, tz=None):
    lines = [f"News digest · {format_day(end, tz)}", f"Published {format_stamp(start, tz)} to {format_stamp(end, tz)}"]
    if fallback:
        lines.append(FALLBACK_NOTE)
    lines.append("")
    if briefing:
        lines += briefing_text(briefing, base)
    sections = [(t, arr) for t, arr in split_sections(items) if arr]
    if not sections:
        lines += ["Nothing new since the last digest.", ""]
    for title, arr in sections:
        lines += [f"{title} ({len(arr)})", ""]
        for it in arr[:MAX_PER_SECTION]:
            lines.append(f"- {one_line(it.get('title')) or 'Untitled'}")
            lines.append(f"  {one_line(it.get('source'))} · {time_tag(it)}")
            url = safe_url(it.get("url"))
            if url != "#":
                lines.append(f"  {url}")
        if len(arr) > MAX_PER_SECTION:
            lines.append(f"{len(arr) - MAX_PER_SECTION} more on the dashboard.")
        lines.append("")
    failed = [s for s in sources if s.get("ok") is False]
    if failed:
        names = "; ".join(
            one_line(s.get("label")) + (f" · {one_line(s.get('error'))}" if s.get("error") else "") for s in failed
        )
        lines.append(f"Sources that could not be fetched this run: {names}.")
    lines.append("Sent by your news digest workflow.")
    return "\n".join(lines) + "\n"


def build_message(sender, recipient, items, sources, start, end, fallback=False, briefing=None, base=None, tz=None):
    msg = EmailMessage()
    msg["Subject"] = f"News digest · {format_day(end, tz)}"
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(build_text(items, sources, start, end, fallback, briefing, base, tz))
    msg.add_alternative(build_html(items, sources, start, end, fallback, briefing, base, tz), subtype="html")
    return msg


def main():
    env = check_env()
    now = datetime.now(timezone.utc)
    tz = load_timezone()
    end = min(current_run_start() or now, now)
    prev = previous_run_time(end=end)
    start = window_start(end, prev)
    late_after = None
    if prev is not None and start == prev:
        late_after = start + BUILD_SLACK
    # The workflow sets DIGEST_DATA=fallback when it sends from the copy committed on main.
    fallback = os.environ.get("DIGEST_DATA") == "fallback"
    items, sources = load_data()
    recent = select_window(items, start, end, late_after)
    print(f"{len(recent)} items published between {start.isoformat()} and {end.isoformat()}")
    briefing = load_briefing(_local(end, tz).date().isoformat())
    base = site_url()
    msg = build_message(env["SENDER_EMAIL"], env["RECIPIENT_EMAIL"], recent, sources, start, end, fallback,
                        briefing, base, tz)
    token = get_access_token(env["GMAIL_CLIENT_ID"], env["GMAIL_CLIENT_SECRET"], env["GMAIL_REFRESH_TOKEN"])
    res = send_message(token, msg)
    print("Email sent, id:", res.get("id"))


if __name__ == "__main__":
    main()
