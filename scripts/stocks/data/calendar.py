"""Market-moving dates: FOMC meetings, BLS and BEA release schedules, and past
release dates for event studies (contract section 5.6).

Sources, all public pages:
  FOMC   the Federal Reserve's meeting calendar page (statement 14:00 New York)
  BLS    the BLS release schedule as iCalendar (CPI, jobs report, PPI, JOLTS)
  BEA    the BEA release schedule as iCalendar (GDP, personal income and outlays)
  history: the BLS CPI release archive, and every past FOMC statement date on
           the calendar page

Parsers are tolerant: a markup change makes them return fewer events, never
raise on a single odd row. fetch_calendar collects what each source gives and
reports a failed source under "errors" with a plain reason.
"""
import re
from datetime import date, datetime, time as dtime, timedelta, timezone

from stocks.data import FetchError

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_ICS = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
BLS_ARCHIVE = "https://www.bls.gov/bls/news-release/{release}.htm"
BLS_SCHEDULE_PAGE = "https://www.bls.gov/schedule/news_release/{release}.htm"
BEA_SCHEDULE_PAGE = "https://www.bea.gov/news/schedule"
NY_TZ = "America/New_York"
HISTORY_START = "2021-01-01"

FOMC_TITLE = "FOMC meeting, statement and press conference"
BLS_KINDS = {
    "Consumer Price Index": ("cpi", "CPI release", "cpi"),
    "Employment Situation": ("jobs", "Jobs report", "empsit"),
    "Producer Price Index": ("ppi", "PPI release", "ppi"),
    "Job Openings and Labor Turnover Survey": ("jolts", "JOLTS release", "jolts"),
}
GDP_RE = re.compile(r"^(GDP \((Advance|Second|Third) Estimate\)|Gross Domestic Product, )")
PCE_PREFIX = "Personal Income and Outlays, "
KINDS = ("fomc", "cpi", "jobs", "ppi", "jolts", "gdp", "pce")

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def _ny():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(NY_TZ)
    except Exception:
        return None


def _to_ny(dt):
    tz = _ny()
    if tz is not None:
        return dt.astimezone(tz)
    from stocks.data import trading_days

    return trading_days.ny_time(dt)


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _event(day, time_s, kind, title, source, flags=None):
    return {"date": day, "time": time_s, "tz": NY_TZ, "kind": kind, "title": title,
            "flags": dict(flags or {}), "source": source}


# ---------------------------------------------------------------- FOMC

_PANEL_RE = re.compile(r"<h4[^>]*>\s*(?:<a[^>]*>)?\s*(\d{4}) FOMC Meetings\s*(?:</a>)?\s*</h4>", re.I)
_DIV_RE = re.compile(r'<div class="([^"]*)"[^>]*>', re.I)
_MONTH_RE = re.compile(r'fomc-meeting__month[^"]*"[^>]*>\s*(?:<strong>)?\s*([A-Za-z/]+)', re.I)
_DATE_RE = re.compile(r'fomc-meeting__date[^"]*"[^>]*>\s*([^<]+?)\s*<', re.I)
_STATEMENT_RE = re.compile(r"monetary(\d{8})a\.htm", re.I)
_TWO_DAY_RE = re.compile(r"^(\d{1,2})-(\d{1,2})(\*?)$")


def _month_number(name):
    return _MONTHS.get(str(name or "").strip().lower()[:3])


def _fomc_rows(panel):
    """(start, end) offsets of each meeting row in a year panel: a div whose
    class list holds the token fomc-meeting itself."""
    starts = [m.start() for m in _DIV_RE.finditer(panel) if "fomc-meeting" in m.group(1).split()]
    return [(s, starts[i + 1] if i + 1 < len(starts) else len(panel)) for i, s in enumerate(starts)]


def parse_fomc(html):
    """Every regular (two-day) meeting on the calendar page, as Events dated on
    the statement day (the second day), oldest first."""
    html = str(html or "")
    heads = list(_PANEL_RE.finditer(html))
    events = []
    for i, head in enumerate(heads):
        year = int(head.group(1))
        panel = html[head.end(): heads[i + 1].start() if i + 1 < len(heads) else len(html)]
        has_footer = bool(re.search(r"panel-footer[^>]*>[^<]*Summary of Economic Projections", panel, re.I))
        for start, end in _fomc_rows(panel):
            row = panel[start:end]
            mm, dm = _MONTH_RE.search(row), _DATE_RE.search(row)
            if not mm or not dm:
                continue
            days = " ".join(dm.group(1).split())
            two = _TWO_DAY_RE.match(days)
            if not two:
                continue  # notation votes and unscheduled meetings
            months = [_month_number(x) for x in mm.group(1).split("/")]
            if not months or any(m is None for m in months):
                continue
            d1, d2 = int(two.group(1)), int(two.group(2))
            m1 = months[0]
            m2 = months[1] if len(months) > 1 else (m1 + 1 if d2 < d1 else m1)
            y2 = year + 1 if m2 == 13 else year
            m2 = 1 if m2 == 13 else m2
            try:
                first = date(year, m1, d1)
                second = date(y2, m2, d2)
            except ValueError:
                continue
            stmt = _STATEMENT_RE.search(row)
            if stmt:
                try:
                    s = stmt.group(1)
                    linked = date(int(s[:4]), int(s[4:6]), int(s[6:]))
                    if abs((linked - second).days) <= 3:
                        second = linked
                except ValueError:
                    pass
            star = bool(two.group(3))
            proj_link = bool(re.search(r"Projection Materials", row, re.I))
            if star or proj_link:
                projections = True
            elif has_footer:
                projections = False
            else:
                projections = None
            events.append(_event(second.isoformat(), "14:00", "fomc", FOMC_TITLE, FOMC_URL, {
                "start_date": first.isoformat(),
                "press_conference": True,
                "press_conference_confirmed": bool(re.search(r">\s*Press Conference\s*<", row, re.I)),
                "projections": projections,
                "tentative": not has_footer,
            }))
    events.sort(key=lambda e: e["date"])
    out, seen = [], set()
    for e in events:
        if e["date"] not in seen:
            seen.add(e["date"])
            out.append(e)
    return out


# ---------------------------------------------------------------- iCalendar

def _unfold(text):
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    for ln in lines:
        if ln[:1] in (" ", "\t") and out:
            out[-1] += ln[1:]
        else:
            out.append(ln)
    return out


def _unescape(value):
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            n = value[i + 1]
            out.append("\n" if n in "nN" else n)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _split_prop(line):
    """(NAME, {PARAM: value}, value) of one content line."""
    in_quote = False
    for i, c in enumerate(line):
        if c == '"':
            in_quote = not in_quote
        elif c == ":" and not in_quote:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None, {}, ""
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().upper()] = v.strip().strip('"')
    return parts[0].strip().upper(), params, value


def _zone(tzid):
    if not tzid or tzid.replace("-", " ").lower() in ("us eastern", "eastern standard time", "us/eastern"):
        return _ny()
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tzid)
    except Exception:
        return _ny()


def _ics_start(params, value):
    """(aware datetime, all_day) of a DTSTART value, or (None, False)."""
    v = value.strip()
    try:
        if params.get("VALUE", "").upper() == "DATE" or re.match(r"^\d{8}$", v):
            d = datetime.strptime(v[:8], "%Y%m%d")
            tz = _zone(params.get("TZID"))
            return (d.replace(tzinfo=tz) if tz else d.replace(tzinfo=timezone(timedelta(hours=-5)))), True
        if v.endswith("Z"):
            return datetime.strptime(v[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc), False
        d = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
        tz = _zone(params.get("TZID"))
        if tz is None:
            from stocks.data import trading_days

            guess = d.replace(tzinfo=timezone.utc) + timedelta(hours=5)
            return d.replace(tzinfo=timezone(trading_days._us_eastern_offset(guess))), False
        return d.replace(tzinfo=tz), False
    except ValueError:
        return None, False


def parse_ics(text):
    """[{"summary", "start" (aware datetime), "uid", "all_day"}] of the VEVENTs.
    Accepts LF or CRLF and folded lines."""
    events, cur = [], None
    for line in _unfold(text):
        if not line.strip():
            continue
        name, params, value = _split_prop(line)
        if name == "BEGIN" and value.strip().upper() == "VEVENT":
            cur = {}
        elif name == "END" and value.strip().upper() == "VEVENT":
            if cur is not None and cur.get("summary") and cur.get("start") is not None:
                events.append({"summary": cur["summary"], "start": cur["start"], "uid": cur.get("uid") or "",
                               "all_day": cur.get("all_day", False)})
            cur = None
        elif cur is not None:
            if name == "SUMMARY":
                cur["summary"] = " ".join(_unescape(value).split())
            elif name == "DTSTART":
                cur["start"], cur["all_day"] = _ics_start(params, value)
            elif name == "UID":
                cur["uid"] = value.strip()
    return events


def _ics_event(e, kind, title, source):
    local = _to_ny(e["start"])
    return _event(local.date().isoformat(), None if e.get("all_day") else local.strftime("%H:%M"), kind, title,
                  source)


def bls_events(text):
    """CPI, jobs report, PPI and JOLTS release Events from the BLS schedule.
    Only exact summaries count ("State Job Openings and Labor Turnover" and
    "Employment Situation of Veterans" are other releases)."""
    out = []
    for e in parse_ics(text):
        hit = BLS_KINDS.get(e["summary"].strip())
        if hit:
            kind, title, page = hit
            out.append(_ics_event(e, kind, title, BLS_SCHEDULE_PAGE.format(release=page)))
    out.sort(key=lambda x: (x["date"], x["time"] or "", x["kind"]))
    return out


def bea_events(text):
    """GDP and Personal Income and Outlays (PCE) release Events from the BEA schedule."""
    out = []
    for e in parse_ics(text):
        s = e["summary"].strip()
        if GDP_RE.match(s):
            out.append(_ics_event(e, "gdp", s, BEA_SCHEDULE_PAGE))
        elif s.startswith(PCE_PREFIX):
            out.append(_ics_event(e, "pce", s, BEA_SCHEDULE_PAGE))
    out.sort(key=lambda x: (x["date"], x["time"] or "", x["kind"]))
    return out


def release_dates(archive_html, release):
    """Past release dates (oldest first) from a BLS archive page, read from links
    like /news.release/archives/cpi_MMDDYYYY.htm."""
    if not re.match(r"^[a-z0-9]{2,12}$", str(release or "")):
        raise ValueError("not a BLS release name")
    pat = re.compile(r"/news\.release/archives/" + release + r"_(\d{2})(\d{2})(\d{4})\.htm", re.I)
    out = set()
    for m in pat.finditer(str(archive_html or "")):
        try:
            out.add(date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat())
        except ValueError:
            continue
    return sorted(out)


def upcoming(events, today, days=30):
    """Events from today through today + days, by date and time."""
    t0 = _as_date(today)
    lo, hi = t0.isoformat(), (t0 + timedelta(days=int(days))).isoformat()
    out = [e for e in events or () if isinstance(e, dict) and lo <= str(e.get("date") or "") <= hi]
    return sorted(out, key=lambda e: (e["date"], e.get("time") or "", e.get("kind") or ""))


def fetch_calendar(http, today, back_days=31, ahead_days=400):
    """{"events": [Event] from back_days ago to ahead_days ahead, by date,
    "history": {"fomc": [past statement dates since 2021], "cpi": [past CPI
    release dates]}, "errors": {source: reason}}."""
    today = _as_date(today)
    lo = (today - timedelta(days=back_days)).isoformat()
    hi = (today + timedelta(days=ahead_days)).isoformat()
    t = today.isoformat()
    events, errors = [], {}
    history = {"fomc": [], "cpi": []}
    bls = []

    try:
        fomc = parse_fomc(http.get(FOMC_URL).text())
        if not fomc:
            errors["fomc"] = "no meetings found"
        events += fomc
        history["fomc"] = [e["date"] for e in fomc if HISTORY_START <= e["date"] < t]
    except (FetchError, ValueError) as e:
        errors["fomc"] = e.reason if isinstance(e, FetchError) else "bad data"
    try:
        bls = bls_events(http.get(BLS_ICS).text())
        if not bls:
            errors["bls"] = "no releases found"
        events += bls
    except (FetchError, ValueError) as e:
        errors["bls"] = e.reason if isinstance(e, FetchError) else "bad data"
    try:
        bea = bea_events(http.get(BEA_ICS).text())
        if not bea:
            errors["bea"] = "no releases found"
        events += bea
    except (FetchError, ValueError) as e:
        errors["bea"] = e.reason if isinstance(e, FetchError) else "bad data"
    cpi = set(e["date"] for e in bls if e["kind"] == "cpi" and e["date"] < t)
    try:
        cpi.update(d for d in release_dates(http.get(BLS_ARCHIVE.format(release="cpi")).text(), "cpi") if d < t)
    except (FetchError, ValueError) as e:
        errors["cpi_history"] = e.reason if isinstance(e, FetchError) else "bad data"
    history["cpi"] = sorted(d for d in cpi if d >= HISTORY_START)

    kept = [e for e in events if lo <= e["date"] <= hi]
    kept.sort(key=lambda e: (e["date"], e.get("time") or "", e["kind"]))
    return {"events": kept, "history": history, "errors": errors}
