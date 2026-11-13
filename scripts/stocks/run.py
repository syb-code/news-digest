"""The private stock run, one phase per workflow step (contract section 10.8).

    python -I scripts/stocks/run.py <phase> --dir "$RUNNER_TEMP/stocks" [--now ISO] [--config PATH]

Phases, each in its own step so a secret reaches only the step that needs it:

  config   STOCKS_INGEST_KEY, WORKER_URL  reads the ticker list, the model and
           each ticker's learning state from the Worker. Every ticker is
           registered with "::add-mask::" before any other output.
  fetch    TIINGO_API_KEY, SEC_USER_AGENT, FMP_API_KEY (optional)  prices,
           SEC filings, FINRA short data, FRED series, the calendar, news,
           13F tables and the House trades artifact. Names, CIKs and CUSIPs
           are masked before the first line. Fifteen minutes at most; a
           ticker past that keeps its cached data.
  analyze  no secret  per ticker: the learner (grade, refit, log the call),
           indicators, levels, short volume, macro impact, valuation and
           holders, and a document with levels from the code; then the
           market overview without its summary.
  claude   ANTHROPIC_API_KEY  the overview summary and one assessment per
           ticker, checked in code; a failed call keeps the code levels.
  upload   STOCKS_INGEST_KEY, WORKER_URL  checks every state and document and
           saves them, and the overview, in the Worker.
  local    all of the above in one process on the owner's machine, with
           --tickers A,B instead of the Worker's list and --out DIR instead
           of the upload.

Privacy: the repository, the Actions logs and the artifacts are public. The
log of this run holds counts, durations, model ids, token counts, costs,
source names and a ticker's position ("ticker 3 of 8"), never a ticker, a
company name, a CIK, a CUSIP, a price or a path under the private folder.
Exceptions are logged by type only. Files are written under --dir/private
(mode 0700, refused inside the repository), named by position, never by
ticker; the workflow deletes the folder at the end of the job.

Isolation: each ticker is worked on alone, with its own state; the shared
market inputs (SPY, FRED, the calendar, news, the 13F tables) are read-only
and never written into a state.
"""
import argparse
import copy
import json
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != _HERE]
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import stocks  # noqa: E402
from stocks import DISCLAIMER, privacy, round_pct, round_price  # noqa: E402

PHASES = ("config", "fetch", "analyze", "claude", "upload", "local")
FETCH_BUDGET_SECONDS = 15 * 60
# The claude step's timeout in stocks.yml is 15 minutes: no call starts unless
# it can end (CALL_BUDGET_SECONDS) inside 13, and every answer is written as it
# comes.
CLAUDE_STEP_SECONDS = 13 * 60
FETCH_WORKERS = 3
INSIDERS_SHOWN = 60
CONGRESS_SHOWN = 40
CALENDAR_BACK_DAYS = 3
CALENDAR_AHEAD_DAYS = 30
NEWS_SHOWN = 30
EPS_STALE_DAYS = {"four_quarters": 200, "annual": 550}
ITEMS_JSON = os.path.join(stocks.ROOT, "docs", "data", "items.json")

INSIDER_NOTE = ("Open market trades are codes P and S. Grants, exercises, tax withholding and gifts are listed "
                "but not counted.")
FUNDS_NOTE = ("13F filings show long US positions at quarter end, filed up to 45 days later. Options and short "
              "positions are not included.")
CONGRESS_NOTE = ("From House periodic transaction reports read from their PDFs. Paper filings cannot be read and "
                 "are skipped. Senate trades only with an FMP key. Amounts are ranges, filed up to 45 days after "
                 "the trade.")
CONGRESS_LABEL = "Members of Congress, best effort"


class PhaseError(Exception):
    """A phase cannot go on. The message is plain, fixed text."""


# ---------------------------------------------------------------- dependencies

class Deps:
    """What a run reaches outside this process, replaceable in tests:
    the environment, the HTTP transports, the Anthropic client, the clock."""

    def __init__(self, environ=None, transport=None, worker_transport=None, anthropic_client=None,
                 sleep=time.sleep, clock=time.monotonic, install_hooks=False, claude_budget=None):
        self.environ = os.environ if environ is None else environ
        self.transport = transport
        self.worker_transport = worker_transport
        self.anthropic_client = anthropic_client
        self.sleep = sleep
        self.clock = clock
        self.install_hooks = install_hooks
        self.claude_budget = claude_budget

    def env(self, name):
        return str(self.environ.get(name) or "").strip()

    def http(self, user_agent, budget=None):
        return _shared_http(user_agent, transport=self.transport, sleep=self.sleep, clock=self.clock, budget=budget)

    def worker(self):
        from stocks.worker_client import WorkerClient

        return WorkerClient(self.env("WORKER_URL"), self.env("STOCKS_INGEST_KEY"),
                            transport=self.worker_transport, sleep=self.sleep)

    def claude_client(self):
        if self.anthropic_client is not None:
            return self.anthropic_client
        from stocks import claude

        return claude.make_client()


def _shared_http(user_agent, **kwargs):
    """One HttpClient for the fetch threads (its SEC spacing and counters are
    thread safe)."""
    from stocks.data import HttpClient

    return HttpClient(user_agent, **kwargs)


# ---------------------------------------------------------------- files

class Paths:
    """The private working folder. File names hold a ticker's position, never the ticker."""

    def __init__(self, base):
        self.base = os.path.realpath(os.path.abspath(base))
        self.private = privacy.private_dir(self.base)
        self.public = os.path.join(self.base, "public")

    def p(self, *parts):
        return os.path.join(self.private, *parts)

    @property
    def config(self):
        return self.p("config.json")

    @property
    def market(self):
        return self.p("market.json")

    @property
    def run(self):
        return self.p("run.json")

    @property
    def masks(self):
        return self.p("masks.json")

    @property
    def overview(self):
        return self.p("out", "overview.json")

    @property
    def house(self):
        return os.path.join(self.public, "house.json")

    def state(self, i):
        return self.p("state", f"{i:02d}.json")

    def data(self, i):
        return self.p("data", f"{i:02d}.json")

    def out_state(self, i):
        return self.p("out", "state", f"{i:02d}.json")

    def out_ticker(self, i):
        return self.p("out", "ticker", f"{i:02d}.json")


def make_dir(path):
    """path and its missing parents, each created with mode 0700."""
    if os.path.isdir(path):
        return
    make_dir(os.path.dirname(path))
    os.mkdir(path, 0o700)


def write_json(path, doc):
    make_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(stocks.dumps(doc))
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- small helpers

def plural(n, word, many=None):
    return f"{n:,} {word if n == 1 else (many or word + 's')}"


def parse_now(value):
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def ny_today(now):
    from stocks.data import trading_days

    return trading_days.ny_time(now).date()


def load_cfg(path):
    import config

    try:
        return config.load_config(path, check=("stocks",))["stocks"]
    except config.ConfigError as e:
        # ConfigError names a key of config.yml; config.yml never holds a ticker.
        raise PhaseError(stocks.plain(str(e), 300)) from None


def private_values(conf):
    """The values of the private config to mask: tickers, then override CUSIPs and names."""
    values = list(conf.get("tickers") or [])
    for entry in (conf.get("overrides") or {}).values():
        if isinstance(entry, dict):
            values += list(entry.get("cusips") or []) + list(entry.get("names") or [])
    return values


def register_known(red, paths):
    """Mask everything private the earlier phases learned, before any other output."""
    conf = read_json(paths.config, {}) or {}
    red.register(private_values(conf))
    known = read_json(paths.masks, {}) or {}
    red.register([v for v in known.get("values") or [] if isinstance(v, (str, int)) and not isinstance(v, bool)])
    return conf


def save_masks(red, paths, values):
    known = read_json(paths.masks, {}) or {}
    have = [v for v in known.get("values") or [] if isinstance(v, (str, int)) and not isinstance(v, bool)]
    for v in values:
        if v not in (None, "") and not isinstance(v, bool) and v not in have:
            have.append(v)
    write_json(paths.masks, {"values": have})


def update_run(paths, phase, info, cost=None):
    run = read_json(paths.run, {}) or {}
    run.setdefault("phases", {})[phase] = info
    if cost is not None:
        run["cost_usd"] = round(float(run.get("cost_usd") or 0.0) + float(cost), 6)
    run.setdefault("cost_usd", 0.0)
    write_json(paths.run, run)
    return run


def other_names(identities, ticker):
    """Company names of the other tracked tickers (for the lessons filter)."""
    return tuple(sorted({str(ident.get("name")) for t, ident in identities.items()
                         if t != ticker and isinstance(ident, dict) and ident.get("name")}))


def clean_state(ticker, state, tickers, cik=None, names=()):
    """state when it passes learner.check_state for this ticker, else None.
    A lesson that names another tracked ticker (or company) is dropped, not
    the whole state."""
    from stocks.analysis import learner

    if not isinstance(state, dict):
        return None
    try:
        learner.check_state(ticker, state, tuple(tickers), cik=cik)
    except ValueError:  # IsolationError is a ValueError
        return None
    return learner.prune_lessons(state, tuple(tickers), tuple(names))


# ---------------------------------------------------------------- phase: config

def phase_config(args, deps, red, paths, now):
    from stocks.worker_client import WorkerError

    cfg = load_cfg(args.config)
    try:
        client = deps.worker()
        conf = client.get_config(models=cfg["models"], default=cfg["model"])
    except WorkerError as e:
        raise PhaseError(f"Could not read the settings from the Worker: {e.reason}.") from None
    tickers = conf["tickers"]
    # The first output of the phase: every private value is masked before anything is printed.
    red.register(private_values(conf))
    write_json(paths.run, {"reason": args.reason, "started_at": stocks.iso(now), "phases": {}, "cost_usd": 0.0})
    if not tickers:
        update_run(paths, "config", {"ok": False, "tickers": 0})
        raise PhaseError("The ticker list is empty. Add a ticker on the dashboard, with the key set.")
    info = client.last_config_info or {}
    if info.get("dropped"):
        red.warn(f"{plural(info['dropped'], 'entry', 'entries')} in the ticker list did not look like a ticker "
                 "and were left out.")
    if info.get("over_limit"):
        red.warn(f"The list holds more than {stocks.MAX_TICKERS} tickers; the first {stocks.MAX_TICKERS} are used.")
    if info.get("model_reset"):
        red.warn(f"The model in the settings is not allowed; using {conf['model']}.")

    n = len(tickers)
    state_errors, fresh, found = [], 0, 0
    for i, t in enumerate(tickers):
        remove(paths.state(i))
        try:
            state = client.get_state(t)
        except WorkerError as e:
            state_errors.append(i)
            red.warn(f"Could not read the saved state of ticker {i + 1} of {n} ({e.reason}). It is analyzed "
                     "from scratch today and its saved state is left as it is.")
            continue
        if state is None:
            fresh += 1
            continue
        checked = clean_state(t, state, tickers)
        if checked is None:
            fresh += 1
            red.warn(f"The saved state of ticker {i + 1} of {n} did not pass the checks, so it starts again.")
            continue
        found += 1
        write_json(paths.state(i), checked)
    write_json(paths.config, {"tickers": tickers, "model": conf["model"], "overrides": conf.get("overrides") or {},
                              "reason": args.reason, "state_errors": state_errors})
    update_run(paths, "config", {"ok": True, "tickers": n, "states": found, "new": fresh,
                                 "unreadable": len(state_errors)})
    red.log(f"Read the settings: {plural(n, 'ticker')}, model {conf['model']}.")
    red.log(f"Learning states: {found} saved, {fresh} new" + (f", {len(state_errors)} could not be read" if state_errors
                                                            else "") + ".")
    return 0


# ---------------------------------------------------------------- phase: fetch

def _reason(e):
    from stocks.data import safe_reason

    return safe_reason(e)


def _cached_bars(cache):
    from stocks.data import prices

    if not isinstance(cache, dict):
        return []
    try:
        return prices.bars_from_cache(cache)
    except ValueError:
        return []


def _identity_from(ticker, tmap, subs, prev, cusips):
    """The ticker's identity from the SEC map and its submissions; fields SEC
    did not give today come from the state's last identity."""
    from stocks.data import sec

    ident = sec.identity(ticker, tmap, subs, cusips=cusips)
    prev = prev if isinstance(prev, dict) else {}
    if ident.get("cik") is None and prev.get("cik") is not None:
        ident["cik"] = prev["cik"]
    if not ident.get("name") and prev.get("name"):
        ident["name"] = prev["name"]
        ident["issuer_key"] = sec.issuer_key(prev["name"])
    if ident.get("filer_type") == "unknown" and prev.get("filer_type") in ("domestic", "foreign"):
        ident["filer_type"] = prev["filer_type"]
    if not ident.get("exchanges") and prev.get("exchanges"):
        ident["exchanges"] = list(prev["exchanges"])
    return ident


class FetchContext:
    def __init__(self, deps, cfg, conf, http, now, tmap, bundle):
        self.deps = deps
        self.cfg = cfg
        self.conf = conf
        self.http = http
        self.now = now
        self.today = ny_today(now)
        self.tmap = tmap or {}
        self.bundle = bundle
        self.tiingo_key = deps.env("TIINGO_API_KEY")
        self.fmp_key = deps.env("FMP_API_KEY")
        self.tiingo_stop = threading.Event()
        self.tiingo_reason = None if self.tiingo_key else "no Tiingo key"


def fetch_ticker(ctx, i, ticker, state):
    """TickerData (5.10) of one ticker, plus the caches its state will keep.
    Every source fails soft; nothing is logged here."""
    from stocks.data import FetchError, congress, finra, prices, sec

    cfg = ctx.cfg
    state = state if isinstance(state, dict) else {}
    caches = copy.deepcopy(state.get("caches") if isinstance(state.get("caches"), dict) else {})
    prev_ident = state.get("identity") if isinstance(state.get("identity"), dict) else {}
    override = (ctx.conf.get("overrides") or {}).get(ticker) or {}
    errors = {}

    # Prices (Tiingo): a cached day makes no request; auth or rate problems stop Tiingo for the run.
    bars, info, price_status = [], None, "ok"
    if ctx.tiingo_key and not ctx.tiingo_stop.is_set():
        try:
            cache, bars, info = prices.update_cache(caches.get("prices"), ctx.http, ticker, ctx.tiingo_key, ctx.now,
                                                    cfg["bootstrap_years"], cfg["trading_days"]["data_ready"])
            caches["prices"] = cache
        except prices.PriceError as e:
            errors["prices"] = e.reason
            price_status = "not_found" if e.kind == "not_found" else "failed"
            if e.kind in ("auth", "rate"):
                ctx.tiingo_stop.set()
                ctx.tiingo_reason = "Tiingo refused the key" if e.kind == "auth" else "Tiingo rate limit"
        except (FetchError, ValueError) as e:
            errors["prices"] = _reason(e)
            price_status = "failed"
        except Exception as e:  # a parser bug in one source never costs the others
            errors["prices"] = privacy.error_name(e)
            price_status = "failed"
    else:
        errors["prices"] = ctx.tiingo_reason or "Tiingo stopped"
        price_status = "failed"
    if not bars:
        bars = _cached_bars(caches.get("prices"))
        if bars and price_status != "ok":
            price_status = "cached"

    # SEC: the company, its filings, earnings per share and Form 4.
    entry = ctx.tmap.get(ticker) or {}
    cik = entry.get("cik") or prev_ident.get("cik")
    cusips = list(prev_ident.get("cusips") or []) + list(override.get("cusips") or [])
    subs = None
    since_insiders = ctx.today - timedelta(days=int(cfg["insider_days"]))
    if cik:
        try:
            subs = sec.submissions(ctx.http, cik, since=since_insiders)
        except (FetchError, ValueError) as e:
            errors["sec"] = _reason(e)
        except Exception as e:
            errors["sec"] = privacy.error_name(e)
    try:
        ident = _identity_from(ticker, ctx.tmap, subs, prev_ident, cusips)
    except Exception as e:
        errors["sec"] = errors.get("sec") or privacy.error_name(e)
        subs = None
        ident = _identity_from(ticker, ctx.tmap, None, prev_ident, cusips)

    eps_cache = caches.get("eps") if isinstance(caches.get("eps"), dict) else None
    try:
        due = subs is not None and sec.eps_due(subs, eps_cache, ctx.now)
        summary = sec.eps_for(ctx.http, ident["cik"], today=ctx.today) if due else None
    except Exception as e:
        due, summary = False, None
        errors["eps"] = privacy.error_name(e)
    if due:
        if summary.get("status") == "ok" or eps_cache is None or summary.get("note") in (sec.NO_EPS_NOTE,
                                                                                         sec.STALE_EPS_NOTE):
            eps_cache = {"summary": summary, "fetched_at": stocks.iso(ctx.now),
                         "latest_filed": summary.get("latest_filed")}
            caches["eps"] = eps_cache
        else:
            errors["eps"] = "not read"
    eps = (eps_cache or {}).get("summary") or sec.not_available_eps(
        "Earnings per share could not be read from the SEC today." if cik else sec.NO_EPS_NOTE)

    form4_cache = caches.get("form4") if isinstance(caches.get("form4"), dict) else {}
    # A state that had to drop old Form 4 rows (trim_state) starts its list there: older filings
    # would only be dropped again.
    trimmed = form4_cache.get("trimmed_before")
    trimmed = trimmed if isinstance(trimmed, str) and len(trimmed) == 10 and trimmed > since_insiders.isoformat() \
        else None
    since_form4 = date.fromisoformat(trimmed) if trimmed else since_insiders
    if subs is not None:
        try:
            f4 = sec.insider_transactions(ctx.http, subs, since_form4, cached=form4_cache)
            caches["form4"] = {"seen": f4["seen"], "transactions": f4["transactions"]}
            if trimmed:
                caches["form4"]["trimmed_before"] = trimmed
            form4 = {"transactions": f4["transactions"], "seen": f4["seen"], "status": f4["status"],
                     "trimmed_before": trimmed}
            if f4["status"] != "ok":
                errors["form4"] = "some filings not read"
        except Exception as e:
            errors["form4"] = _reason(e) if isinstance(e, (FetchError, ValueError)) else privacy.error_name(e)
            form4 = {"transactions": list(form4_cache.get("transactions") or []),
                     "seen": dict(form4_cache.get("seen") or {}), "status": "cached" if form4_cache else "not_available",
                     "trimmed_before": trimmed}
    else:
        form4 = {"transactions": list(form4_cache.get("transactions") or []),
                 "seen": dict(form4_cache.get("seen") or {}),
                 "status": "cached" if form4_cache else "not_available", "trimmed_before": trimmed}

    # FINRA short sale volume and short interest (deltas on the cached rows).
    symbol = stocks.finra_symbol(ticker)
    cached_sv = finra.volume_rows(caches.get("short_volume"))
    try:
        sv_rows, sv_info = finra.refresh_short_volume(ctx.http, symbol, cached_sv, ctx.today)
    except Exception as e:
        sv_rows, sv_info = cached_sv, {"reason": privacy.error_name(e)}
    caches["short_volume"] = finra.volume_cache(sv_rows)
    if sv_info.get("reason"):
        errors["short_volume"] = sv_info["reason"]
    si_cache = caches.get("short_interest") if isinstance(caches.get("short_interest"), dict) else {}
    try:
        si_rows, si_info = finra.refresh_short_interest(ctx.http, symbol, si_cache.get("rows"), ctx.today)
    except Exception as e:
        si_rows = [r for r in si_cache.get("rows") or [] if isinstance(r, dict)]
        si_info = {"reason": privacy.error_name(e)}
    caches["short_interest"] = {"rows": si_rows}
    if si_info.get("reason"):
        errors["short_interest"] = si_info["reason"]

    # Members of Congress: the House artifact (every member's trades) and, with a key, Senate trades.
    cached_trades = [t for t in ((caches.get("congress") or {}).get("trades") or []) if isinstance(t, dict)]
    since_congress = ctx.today - timedelta(days=int(cfg["congress_days"]))
    house, house_cov = None, "not available"
    if ctx.bundle is not None:
        try:
            house = congress.for_ticker(ctx.bundle.get("trades") or [], ticker, since_congress)
            house_cov = "ok"
        except Exception as e:
            errors["congress"] = privacy.error_name(e)
    else:
        errors["congress"] = "no House artifact"
    if house is None:
        house = [t for t in cached_trades if t.get("chamber") == "House"]
    try:
        senate, senate_cov = congress.senate_trades(ctx.http, ticker, ctx.fmp_key)
    except Exception:
        senate, senate_cov = [], "error"
    if senate_cov != "ok":
        senate = [t for t in cached_trades if t.get("chamber") == "Senate"] if senate_cov != "off" else []
    trades = sorted(house + senate, key=lambda x: (x.get("date") or "", x.get("filed") or "", x.get("member") or ""),
                    reverse=True)
    caches["congress"] = {"trades": trades}

    return {"index": i, "ticker": ticker, "identity": ident, "bars": bars, "price_info": info,
            "price_status": price_status, "eps": eps, "form4": form4,
            "short_volume": sv_rows, "short_interest": si_rows,
            "funds": [], "funds_checked": 0, "congress": trades,
            "congress_coverage": {"house": house_cov, "senate": senate_cov},
            "errors": errors, "caches": caches}


def saved_ticker_data(ctx, i, ticker, state):
    """TickerData from the ticker's saved state alone, for a ticker whose fetch
    stopped: the analysis then runs on the data of the last run."""
    from stocks.data import finra, sec

    state = state if isinstance(state, dict) else {}
    caches = copy.deepcopy(state.get("caches") if isinstance(state.get("caches"), dict) else {})
    prev = state.get("identity") if isinstance(state.get("identity"), dict) else {}
    try:
        ident = _identity_from(ticker, ctx.tmap, None, prev, list(prev.get("cusips") or []))
    except ValueError:
        ident = {"ticker": ticker, "cik": prev.get("cik"), "name": prev.get("name"), "cusips": [],
                 "filer_type": prev.get("filer_type") or "unknown", "exchanges": [], "issuer_key": None}
    form4 = caches.get("form4") if isinstance(caches.get("form4"), dict) else {}
    eps = ((caches.get("eps") or {}).get("summary") if isinstance(caches.get("eps"), dict) else None) or \
        sec.not_available_eps("Earnings per share could not be read from the SEC today.")
    trades = [t for t in ((caches.get("congress") or {}).get("trades") or []) if isinstance(t, dict)]
    return {"index": i, "ticker": ticker, "identity": ident, "bars": _cached_bars(caches.get("prices")),
            "price_info": None, "price_status": "cached", "eps": eps,
            "form4": {"transactions": list(form4.get("transactions") or []), "seen": dict(form4.get("seen") or {}),
                      "status": "cached" if form4 else "not_available",
                      "trimmed_before": form4.get("trimmed_before")},
            "short_volume": finra.volume_rows(caches.get("short_volume")),
            "short_interest": list((caches.get("short_interest") or {}).get("rows") or []),
            "funds": [], "funds_checked": 0, "congress": trades,
            "congress_coverage": {"house": "not available", "senate": "off"},
            "errors": {"fetch": "stopped"}, "caches": caches}


def fetch_funds(ctx, results):
    """The funds flow (5.3): each fund's latest two periods; a period some
    ticker's cache lacks is downloaded once and matched for every ticker
    that lacks it. Tables are dropped after matching. Returns counts."""
    from stocks.data import FetchError, sec

    funds = ctx.cfg["funds"]
    picks_by_cik, failed_funds = {}, 0
    for fund in funds:
        try:
            picks, _notes = sec.fund_periods(ctx.http, fund["cik"], n=2)
        except (FetchError, ValueError):
            failed_funds += 1
            continue
        picks_by_cik[fund["cik"]] = picks
    overrides = ctx.conf.get("overrides") or {}

    def needed_now():
        needed = {}
        for r in results:
            if r is None:
                continue
            ident = r["identity"]
            if not (ident.get("issuer_key") or ident.get("cusips")):
                continue
            cusips = sec.known_cusips(ident, overrides.get(r["ticker"]))
            fcache = r["caches"].setdefault("funds", {})
            for cik, picks in picks_by_cik.items():
                entry = fcache.setdefault(str(cik), {})
                periods = entry.setdefault("periods", {})
                for pick in sec.periods_to_read(periods, picks[:2], cusips):
                    key = (cik, pick["period"], pick["accession"])
                    needed.setdefault(key, (pick, []))[1].append(r)
        return needed

    # Newest period first: a company renamed between quarters is found by name in
    # the newer filing, and the CUSIP learned there finds it in the older one. A
    # second pass re-matches entries read before a ticker learned a CUSIP (tables
    # stay in memory for this call only).
    tables, failed_keys = {}, set()
    tables_read = tables_failed = 0
    for _pass in range(2):
        needed = needed_now()
        todo = [k for k in sorted(needed, key=lambda k: (k[1], k[0], k[2]), reverse=True) if k not in failed_keys]
        if not todo:
            break
        for key in todo:
            pick, rows = needed[key]
            table = tables.get(key)
            if table is None:
                try:
                    table = sec.period_table(ctx.http, pick)
                except (FetchError, ValueError):
                    tables_failed += 1
                    failed_keys.add(key)
                    continue
                tables_read += 1
                tables[key] = table
            for r in rows:
                entry, cusip, _note = sec.period_entry(table, r["identity"], pick, overrides.get(r["ticker"]) or None)
                r["caches"]["funds"][str(key[0])]["periods"][pick["period"]] = entry
                if cusip and cusip not in r["identity"]["cusips"]:
                    r["identity"]["cusips"].append(cusip)
    tables.clear()
    for r in results:
        if r is None:
            continue
        fcache = r["caches"].setdefault("funds", {})
        for cik, picks in picks_by_cik.items():  # keep only the periods still in use
            periods = (fcache.get(str(cik)) or {}).get("periods") or {}
            keep = {p["period"] for p in picks[:2]}
            fcache[str(cik)] = {"periods": {k: v for k, v in periods.items() if k in keep}}
        rows, checked = sec.ticker_fund_rows(funds, fcache, picks_by_cik)
        r["funds"], r["funds_checked"] = rows, checked
        known = r["identity"].get("issuer_key") or r["identity"].get("cusips")
        if checked < len(funds) and known:
            r["errors"]["funds"] = f"{len(funds) - checked} not checked"
    return {"funds": len(funds), "funds_failed": failed_funds, "tables_read": tables_read,
            "tables_failed": tables_failed}


def fetch_market(ctx):
    """SPY, FRED, the calendar and news: shared, read-only inputs."""
    from stocks.data import FetchError, calendar, fred, news, prices

    cfg, errors = ctx.cfg, {}
    spy = []
    if ctx.tiingo_key and not ctx.tiingo_stop.is_set():
        try:
            spy = prices.spy_bars(ctx.http, ctx.tiingo_key, ctx.now, cfg["bootstrap_years"])
        except prices.PriceError as e:
            errors["spy"] = e.reason
            if e.kind in ("auth", "rate"):
                ctx.tiingo_stop.set()
                ctx.tiingo_reason = "Tiingo refused the key" if e.kind == "auth" else "Tiingo rate limit"
        except (FetchError, ValueError) as e:
            errors["spy"] = _reason(e)
        except Exception as e:
            errors["spy"] = privacy.error_name(e)
    else:
        errors["spy"] = ctx.tiingo_reason or "Tiingo stopped"
    ids = []
    for row in cfg["macro"]:
        ids += list(row["series"])
    ids += list(fred.FACTOR_SERIES) + [v["series"] for v in cfg["fx"].values()]
    ids = list(dict.fromkeys(ids))
    try:
        fred_data, fred_errors = fred.fetch_many(ctx.http, ids, fred.run_starts(ids, ctx.today))
        if fred_errors:
            errors["fred"] = f"{len(fred_errors)} series"
    except Exception as e:  # one shared source never stops the run
        fred_data = {}
        errors["fred"] = privacy.error_name(e)
    try:
        cal = calendar.fetch_calendar(ctx.http, ctx.today)
        if cal.get("errors"):
            errors["calendar"] = ", ".join(sorted(cal["errors"]))
    except Exception as e:
        cal = {"events": [], "history": {}, "errors": {}}
        errors["calendar"] = privacy.error_name(e)
    try:
        items, news_errors = news.collect(ctx.http, ITEMS_JSON, ctx.now)
        if news_errors:
            errors["news"] = f"{len(news_errors)} feeds"
    except Exception as e:
        items = []
        errors["news"] = privacy.error_name(e)
    return {"spy": spy, "fred": fred_data, "calendar": cal, "news": items, "tickers_map_count": len(ctx.tmap),
            "fred_series": len(ids), "fred_ok": len(fred_data), "errors": errors}


def phase_fetch(args, deps, red, paths, now):
    from stocks.data import FetchError, congress, sec

    conf = register_known(red, paths)
    tickers = conf.get("tickers") or []
    if not tickers:
        raise PhaseError("There is no ticker list from the config step.")
    cfg = load_cfg(args.config)
    user_agent = deps.env("SEC_USER_AGENT")
    if not user_agent:
        raise PhaseError("SEC_USER_AGENT is not set. SEC, BLS and FRED refuse requests without a contact address.")
    started = deps.clock()
    http = deps.http(user_agent, budget=FETCH_BUDGET_SECONDS)
    states = [read_json(paths.state(i)) for i in range(len(tickers))]

    # The SEC ticker map gives each ticker's company name and CIK: masked before the first line.
    try:
        tmap = sec.company_tickers(http)
        map_error = None
    except (FetchError, ValueError) as e:
        tmap, map_error = {}, _reason(e)
    private = []
    for t, st in zip(tickers, states):
        entry = tmap.get(t) or {}
        ident = (st or {}).get("identity") or {}
        for v in (entry.get("name"), entry.get("cik"), ident.get("name"), ident.get("cik")):
            if v not in (None, ""):
                private.append(v)
        private += list(ident.get("cusips") or [])
    red.register(private)
    save_masks(red, paths, private)

    bundle = None
    if os.path.isfile(paths.house):
        try:
            bundle = congress.load_bundle(paths.house)
        except ValueError:
            red.warn("The House trades artifact did not pass the checks, so it is not used.")
    ctx = FetchContext(deps, cfg, conf, http, now, tmap, bundle)
    if map_error:
        red.warn(f"The SEC ticker map could not be read ({map_error}); names and CIKs come from the saved states.")
    if not ctx.tiingo_key:
        red.warn("TIINGO_API_KEY is not set, so prices come from the saved states only.")

    # Files of an earlier run in a reused folder never stand in for today's.
    for i in range(stocks.MAX_TICKERS):
        remove(paths.data(i))
    remove(paths.market)
    market = fetch_market(ctx)
    write_json(paths.market, market)

    n = len(tickers)
    results = [None] * n
    failed = []

    def one(i):
        return fetch_ticker(ctx, i, tickers[i], states[i])

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix="fetch") as pool:
        futures = {pool.submit(one, i): i for i in range(n)}
        for fut, i in futures.items():
            try:
                results[i] = fut.result()
            except Exception as e:  # one ticker never stops the others
                failed.append((i, privacy.error_name(e)))
    for i, name in failed:
        red.warn(f"Fetching ticker {i + 1} of {n} stopped ({name}); it is analyzed on its saved data.")
        try:
            results[i] = saved_ticker_data(ctx, i, tickers[i], states[i])
        except Exception:
            results[i] = None
    try:
        funds_info = fetch_funds(ctx, results)
    except Exception as e:  # the funds never stop the run
        funds_info = {"funds": len(cfg["funds"]), "error": privacy.error_name(e)}
        red.warn(f"The fund holdings could not be read today ({privacy.error_name(e)}).")

    new_cusips = []
    for r in results:
        if r is not None:
            write_json(paths.data(r["index"]), r)
            new_cusips += list(r["identity"].get("cusips") or [])
    save_masks(red, paths, new_cusips)  # masked first thing in the next step

    with_prices = sum(1 for r in results if r is not None and len(r["bars"]) >= 2)
    fresh = sum(1 for r in results if r is not None and r["price_status"] == "ok" and len(r["bars"]) >= 2)
    sources = set()
    for r in results:
        if r is not None:
            sources.update(r["errors"])
    sources.update(market["errors"])
    seconds = int(round(deps.clock() - started))
    red.log(f"Read the market data: {market['fred_ok']} of {market['fred_series']} FRED series, "
            f"{plural(len(market['calendar'].get('events') or []), 'calendar event')}, "
            f"{plural(len(market['news']), 'news item')}, SPY {'read' if market['spy'] else 'not read'}.")
    if "funds" in funds_info and "tables_read" in funds_info:
        red.log(f"Fund holdings: {funds_info['funds'] - funds_info['funds_failed']} of {funds_info['funds']} funds "
                f"read, {plural(funds_info['tables_read'], 'quarterly table')} downloaded.")
    if bundle is None:
        red.warn("No House trades today: the congress job's artifact is missing.")
    red.log(f"Fetched data for {with_prices} of {n} tickers in {seconds} seconds ({fresh} with new prices); "
            f"{plural(len(sources), 'source')} had a problem.")
    if sources:
        red.log("Sources with a problem: " + ", ".join(sorted(sources)) + ".")
    update_run(paths, "fetch", {"ok": True, "tickers": n, "with_prices": with_prices, "fresh": fresh,
                                "failed": len(failed), "seconds": seconds, "requests": http.requests,
                                "sources_with_problems": sorted(sources)})
    if with_prices == 0 and not market["spy"] and not market["fred"] and not market["calendar"].get("events"):
        raise PhaseError("Nothing at all could be fetched.")
    return 0


# ---------------------------------------------------------------- phase: analyze

def _date_diff(a, b):
    try:
        return (date.fromisoformat(str(b)[:10]) - date.fromisoformat(str(a)[:10])).days
    except ValueError:
        return None


def valuation_block(eps, bars, fred_data, fx_cfg, as_of):
    """Valuation (8.5): trailing P/E from SEC diluted EPS, in dollars."""
    from stocks.data import fred, sec

    eps = eps if isinstance(eps, dict) else {}
    last = bars[-1] if bars else {}
    price = round_price(last.get("raw_close"))
    base = {"status": "not_available", "pe_ttm": None, "eps_ttm": None, "eps_basis": eps.get("basis"),
            "eps_currency": eps.get("currency"), "eps_usd": None, "fx": None, "concept": eps.get("concept"),
            "quarters": list(eps.get("quarters") or []), "latest_end": eps.get("latest_end"),
            "latest_filed": eps.get("latest_filed"), "stale": False, "price_used": price, "split_adjusted": False,
            "note": eps.get("note")}
    if eps.get("status") != "ok" or eps.get("eps_ttm") is None:
        base["note"] = eps.get("note") or "No earnings per share in the SEC data yet."
        return base
    ended = _date_diff(eps.get("latest_end"), as_of)
    if ended is not None and ended > sec.EPS_MAX_AGE_DAYS.get(eps.get("basis"), sec.EPS_MAX_AGE_DAYS["four_quarters"]):
        # Earnings that ended years ago (a filer that stopped tagging EPS, kept in a saved cache)
        # give no P/E: the rule of sec.eps_summary, applied again to the saved summary.
        base["note"] = sec.STALE_EPS_NOTE
        return base
    ttm = float(eps["eps_ttm"])
    base["eps_ttm"] = ttm
    currency = str(eps.get("currency") or "USD").upper()
    rate = fred.fx_usd_per_unit(fred_data or {}, currency, fx_cfg)
    if rate is None:
        base["note"] = f"Earnings are reported in {currency} and there is no exchange rate for it today."
        return base
    usd_per_unit, series, fx_date = rate
    if currency != "USD":
        base["fx"] = {"currency": currency, "series": series, "usd_per_unit": usd_per_unit, "date": fx_date}
    eps_usd = ttm * usd_per_unit
    filed = str(eps.get("latest_filed") or "")
    factor = 1.0
    for b in bars:
        s = b.get("split")
        if filed and str(b.get("date") or "") > filed and isinstance(s, (int, float)) and s > 0 and s != 1:
            factor *= float(s)
    if factor != 1.0:
        eps_usd /= factor
        base["split_adjusted"] = True
    base["eps_usd"] = round_price(eps_usd)
    age = _date_diff(eps.get("latest_end"), as_of)
    limit = EPS_STALE_DAYS.get(eps.get("basis"), 200)
    base["stale"] = bool(age is not None and age > limit)
    if eps_usd <= 0:
        base["status"] = "not_meaningful"
        span = "over the last fiscal year" if eps.get("basis") == "annual" else "over the last four quarters"
        base["note"] = f"Earnings {span} are negative, so P/E is not meaningful."
        return base
    if price is None:
        base["note"] = "There is no price to compare with earnings yet."
        return base
    base["status"] = "ok"
    base["pe_ttm"] = round(price / eps_usd, 2)
    base["note"] = (f"The latest earnings in the SEC data are for a period that ended {age} days ago."
                    if base["stale"] else None)
    return base


def insiders_block(form4, days, as_of):
    form4 = form4 if isinstance(form4, dict) else {}
    since = (date.fromisoformat(as_of) - timedelta(days=int(days))).isoformat() if as_of else ""
    trimmed = form4.get("trimmed_before")
    short_list = isinstance(trimmed, str) and trimmed > since
    if short_list:
        since = trimmed
    txns = [t for t in form4.get("transactions") or [] if isinstance(t, dict)
            and str(t.get("date") or t.get("filed") or "") >= since]
    txns.sort(key=lambda t: (str(t.get("date") or ""), str(t.get("filed") or ""), str(t.get("accession") or "")),
              reverse=True)
    buys = [t for t in txns if t.get("code") == "P" and not t.get("derivative")]
    sells = [t for t in txns if t.get("code") == "S" and not t.get("derivative")]

    def total(rows, key):
        return round(sum(float(t.get(key) or 0.0) for t in rows), 2)

    summary = {"open_market_buys": len(buys), "open_market_sells": len(sells),
               "buy_shares": total(buys, "shares"), "sell_shares": total(sells, "shares"),
               "buy_value": total(buys, "value"), "sell_value": total(sells, "value"),
               "net_shares": round(total(buys, "shares") - total(sells, "shares"), 2),
               "insiders": len({t.get("owner") for t in buys + sells if t.get("owner")}), "since": since}
    if txns:
        status = "ok"
    elif form4.get("status") == "not_available":
        status = "not_available"
    else:
        status = "none"
    note = INSIDER_NOTE
    if short_list:
        note += (f" This stock's insiders file so many reports that only those since {since} are kept, so the "
                 "totals cover that shorter time.")
    return {"status": status, "window_days": int(days), "transactions": txns[:INSIDERS_SHOWN], "summary": summary,
            "note": note}


def funds_block(rows, checked):
    status = "ok" if rows else ("none" if checked else "not_available")
    return {"status": status, "funds_checked": int(checked or 0), "rows": list(rows or []), "note": FUNDS_NOTE}


def congress_block(trades, coverage, days, as_of):
    since = (date.fromisoformat(as_of) - timedelta(days=int(days))).isoformat() if as_of else ""
    kept = [t for t in trades or [] if isinstance(t, dict) and str(t.get("date") or t.get("filed") or "") >= since]
    coverage = dict(coverage or {"house": "not available", "senate": "off"})
    if kept:
        status = "ok"
    elif coverage.get("house") == "ok" or coverage.get("senate") == "ok":
        status = "none"
    else:
        status = "not_available"
    return {"status": status, "label": CONGRESS_LABEL, "coverage": coverage, "trades": kept[:CONGRESS_SHOWN],
            "note": CONGRESS_NOTE}


def header_block(ticker, ident, bars):
    """Header (8.1). last_close is the raw close. The day's move comes from the
    adjusted closes, so a split on the last day is not shown as a crash;
    prev_close is the previous close on the last close's basis, so change is
    last_close minus prev_close."""
    ident = ident or {}
    last = bars[-1] if bars else {}
    prev = bars[-2] if len(bars) > 1 else {}
    lc = round_price(last.get("raw_close"))
    adj_last, adj_prev = round_price(last.get("close")), round_price(prev.get("close"))
    ratio = adj_last / adj_prev if adj_last and adj_prev and adj_last > 0 and adj_prev > 0 else None
    pc = None if lc is None or ratio is None else round_price(lc / ratio)
    change = None if pc is None else round_price(lc - lc / ratio)
    pct = None if ratio is None else round_pct((ratio - 1.0) * 100.0)
    return {"ticker": ticker, "name": ident.get("name"), "cik": ident.get("cik"),
            "exchanges": list(ident.get("exchanges") or []), "filer_type": ident.get("filer_type") or "unknown",
            "currency": "USD", "first_date": bars[0]["date"] if bars else None, "last_close": lc,
            "prev_close": pc, "change": change, "change_pct": pct, "bars": len(bars)}


def notes_for(data, bars):
    errors = data.get("errors") or {}
    notes = []
    last = bars[-1]["date"] if bars else None
    if errors.get("fetch"):
        notes.append("The data could not be updated today, so this analysis uses the data of the last run.")
    elif errors.get("prices") and last:
        notes.append(f"Prices could not be updated today; the chart ends on {last}.")
    if errors.get("short_volume"):
        rows = data.get("short_volume") or []
        when = rows[-1]["date"] if rows else None
        notes.append("Short volume could not be read today" + (f"; the numbers are from {when}." if when else "."))
    if errors.get("sec"):
        notes.append("SEC filings could not be read today; earnings and insider trades are from the saved data.")
    elif errors.get("form4"):
        notes.append("Some insider filings could not be read today; the next run reads them.")
    if errors.get("funds"):
        notes.append(f"Some funds could not be checked today ({errors['funds']}).")
    if errors.get("congress"):
        notes.append("The House trades could not be read today.")
    return notes


def document_status(doc):
    blocks = [doc.get("valuation"), doc.get("shorts"), doc.get("macro_impact")]
    blocks += list((doc.get("holders") or {}).values())
    return "partial" if any(isinstance(b, dict) and b.get("status") == "not_available" for b in blocks) else "ok"


def base_document(ticker, now, status, header, notes):
    return {"kind": "stocks.ticker", "version": stocks.DOC_VERSION, "ticker": ticker, "status": status,
            "notes": list(notes), "header": header, "model": None, "llm": False, "cost": _empty_cost(),
            "generated_at": stocks.iso(now), "data_as_of": None, "disclaimer": DISCLAIMER}


def _empty_cost():
    return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.0, "cost_estimated": False}


def fit_document(doc):
    """The document under DOC_MAX_BYTES: shorter chart, short volume and lists first."""
    if stocks.size_of(doc) <= stocks.DOC_MAX_BYTES:
        return doc
    chart = doc.get("chart") or {}
    for days in (250, 120):
        for k, v in list(chart.items()):
            if isinstance(v, list):
                chart[k] = v[-days:]
        if stocks.size_of(doc) <= stocks.DOC_MAX_BYTES:
            return doc
    daily = (doc.get("shorts") or {}).get("daily") or {}
    for k, v in list(daily.items()):
        if isinstance(v, list):
            daily[k] = v[-60:]
    holders = doc.get("holders") or {}
    for key, n in (("insiders", 20), ("congress", 15)):
        block = holders.get(key) or {}
        for field in ("transactions", "trades"):
            if isinstance(block.get(field), list):
                block[field] = block[field][:n]
    if stocks.size_of(doc) > stocks.DOC_MAX_BYTES:
        raise ValueError("the document is too large")
    return doc


def analyze_ticker(i, ticker, tickers, cfg, data, state, market, identities, now):
    """(document, state to save or None) for one ticker, from its own data and
    state and the shared market inputs. The state is checked before and after."""
    from stocks import claude
    from stocks.analysis import indicators, learner, levels, macro_impact, shorts

    others = tuple(tickers)
    ident = data.get("identity") or {}
    cik = ident.get("cik")
    bars = [b for b in data.get("bars") or [] if isinstance(b, dict)]
    st = clean_state(ticker, state, tickers, cik=cik) if state else None
    if st is None:
        st = learner.new_state(ticker, stocks.iso(now))
    st["identity"] = {"cik": cik, "name": ident.get("name"), "filer_type": ident.get("filer_type") or "unknown",
                      "cusips": list(ident.get("cusips") or []), "finra_symbol": stocks.finra_symbol(ticker),
                      "issuer_key": ident.get("issuer_key"), "exchanges": list(ident.get("exchanges") or [])}
    st["caches"] = data.get("caches") or {}
    header = header_block(ticker, ident, bars)
    notes = notes_for(data, bars)

    if len(bars) < 2:
        unknown = data.get("price_status") == "not_found" and not cik
        status = "unknown_ticker" if unknown else "no_prices"
        notes.append("Neither SEC nor Tiingo knows this ticker. Check the spelling." if unknown else
                     "There are no prices for this ticker yet, so there is no analysis today.")
        doc = base_document(ticker, now, status, header, notes)
        st["updated"] = stocks.iso(now)
        st = learner.trim_state(st, stocks.STATE_MAX_BYTES, insider_days=cfg["insider_days"],
                                congress_days=cfg["congress_days"], bootstrap_years=cfg["bootstrap_years"])
        learner.check_state(ticker, st, others, cik=cik)
        return doc, st

    spy = market.get("spy") or []
    fred_data = market.get("fred") or {}
    short_volume = data.get("short_volume") or []
    st = learner.daily_update(st, bars, spy, fred_data, short_volume, cfg, stocks.iso(now), ticker=ticker,
                              others=others)
    tech = indicators.technicals(bars, spy)
    excursions = (st.get("model") or {}).get("excursions")
    try:
        lv = levels.levels_block(bars, tech, excursions)
    except (ValueError, TypeError, ZeroDivisionError):
        lv = {"supports": [], "resistances": [], "candidates": [], "atr14": tech.get("atr14"),
              "tolerance": {"pct": levels.TOL_PCT, "atr_share": levels.TOL_ATR_SHARE}}
    as_of = bars[-1]["date"]
    calendar = market.get("calendar") or {}
    doc = base_document(ticker, now, "ok", header, notes)
    doc.update({
        "chart": indicators.chart(bars, cfg["chart_days"]),
        "technicals": tech,
        "levels": lv,
        "valuation": valuation_block(data.get("eps"), bars, fred_data, cfg["fx"], as_of),
        "shorts": shorts.summarize(short_volume, data.get("short_interest") or []),
        "holders": {"insiders": insiders_block(data.get("form4"), cfg["insider_days"], as_of),
                    "funds": funds_block(data.get("funds"), data.get("funds_checked")),
                    "congress": congress_block(data.get("congress"), data.get("congress_coverage"),
                                               cfg["congress_days"], as_of)},
        "macro_impact": macro_impact.macro_impact(bars, spy, fred_data, calendar.get("history") or {}),
        "data_as_of": as_of,
    })
    doc["learner"] = learner.learner_block(st)
    doc["assessment"] = claude.code_assessment(doc)
    doc["status"] = document_status(doc)
    st = learner.attach_assessment(st, st.get("last_as_of") or as_of, doc["assessment"], [], None,
                                   cfg["lessons_cap"], others, other_names(identities, ticker))
    st = learner.trim_state(st, stocks.STATE_MAX_BYTES, insider_days=cfg["insider_days"],
                            congress_days=cfg["congress_days"], bootstrap_years=cfg["bootstrap_years"])
    learner.check_state(ticker, st, others, cik=cik)
    doc["learner"] = learner.learner_block(st)
    return fit_document(doc), st


def overview_document(cfg, market, now, run):
    from stocks import claude
    from stocks.data import fred, trading_days

    today = ny_today(now)
    lo = (today - timedelta(days=CALENDAR_BACK_DAYS)).isoformat()
    hi = (today + timedelta(days=CALENDAR_AHEAD_DAYS)).isoformat()
    events = [e for e in ((market.get("calendar") or {}).get("events") or [])
              if isinstance(e, dict) and lo <= str(e.get("date") or "") <= hi]
    events.sort(key=lambda e: (e["date"], e.get("time") or "", e.get("kind") or ""))
    spy = market.get("spy") or []
    as_of = spy[-1]["date"] if spy else trading_days.last_closed_day(now, cfg["trading_days"]["data_ready"]).isoformat()
    return {"kind": "stocks.overview", "version": stocks.OVERVIEW_VERSION, "generated_at": stocks.iso(now),
            "data_as_of": as_of, "calendar": events, "macro": fred.macro_table(market.get("fred") or {}, cfg["macro"]),
            "news": list(market.get("news") or [])[:NEWS_SHOWN], "summary": claude.empty_summary(), "model": None,
            "cost": _empty_cost(), "run": run, "disclaimer": DISCLAIMER}


def empty_overview(now, run):
    """An overview with no market data (the overview step failed)."""
    from stocks import claude

    return {"kind": "stocks.overview", "version": stocks.OVERVIEW_VERSION, "generated_at": stocks.iso(now),
            "data_as_of": None, "calendar": [], "macro": [], "news": [], "summary": claude.empty_summary(),
            "model": None, "cost": _empty_cost(), "run": run, "disclaimer": DISCLAIMER}


def phase_analyze(args, deps, red, paths, now):
    conf = register_known(red, paths)
    tickers = conf.get("tickers") or []
    if not tickers:
        raise PhaseError("There is no ticker list from the config step.")
    cfg = load_cfg(args.config)
    market = read_json(paths.market, {}) or {}
    datas = [read_json(paths.data(i)) for i in range(len(tickers))]
    datas = [d if isinstance(d, dict) and d.get("ticker") == t and d.get("index") == i else None
             for i, (t, d) in enumerate(zip(tickers, datas))]
    identities = {t: (d or {}).get("identity") or {} for t, d in zip(tickers, datas)}
    n = len(tickers)
    counts = {"ok": 0, "partial": 0, "failed": 0, "model": 0, "warming": 0, "no_prices": 0}
    failures = []
    for i, t in enumerate(tickers):
        remove(paths.out_ticker(i))
        remove(paths.out_state(i))
        data = datas[i]
        try:
            if not isinstance(data, dict) or data.get("ticker") != t or data.get("index") != i:
                raise PhaseError("no data")
            doc, st = analyze_ticker(i, t, tickers, cfg, data, read_json(paths.state(i)), market, identities, now)
        except Exception as e:  # one ticker never stops the others
            counts["failed"] += 1
            failures.append("data" if isinstance(e, PhaseError) else privacy.error_name(e))
            header = header_block(t, identities.get(t), [])
            doc = base_document(t, now, "error", header,
                                ["The analysis of this ticker failed today. The next run tries again."])
            write_json(paths.out_ticker(i), doc)
            continue
        write_json(paths.out_ticker(i), doc)
        if st is not None:
            write_json(paths.out_state(i), st)
        status = doc["status"]
        if status in ("ok", "partial"):
            counts[status] += 1
            if (doc.get("learner") or {}).get("status") == "ok":
                counts["model"] += 1
            else:
                counts["warming"] += 1
        else:
            counts["no_prices"] += 1
    run = read_json(paths.run, {}) or {}
    run_info = {"reason": run.get("reason") or args.reason, "started_at": run.get("started_at") or stocks.iso(now),
                "finished_at": None, "tickers": n, "ok": counts["ok"], "partial": counts["partial"],
                "failed": counts["failed"] + counts["no_prices"], "cost_usd": 0.0, "model": conf.get("model")}
    try:
        overview = overview_document(cfg, market, now, run_info)
    except Exception as e:  # the ticker documents are kept; the overview is shown empty
        red.warn(f"The market overview could not be built ({privacy.error_name(e)}); it is saved without data.")
        overview = empty_overview(now, run_info)
    write_json(paths.overview, overview)
    built = counts["ok"] + counts["partial"]
    if failures:
        kinds = ", ".join(sorted(set(failures)))
        red.log(f"Analyzed {built} of {n} tickers, {counts['failed']} failed ({kinds}).")
    red.log(f"Analyzed {plural(built, 'ticker')}: {counts['model']} with a model, {counts['warming']} warming up"
            + (f", {counts['no_prices']} without prices" if counts["no_prices"] else "") + ".")
    update_run(paths, "analyze", {"ok": True, "tickers": n, "built": built, "failed": counts["failed"],
                                  "no_prices": counts["no_prices"], "with_model": counts["model"],
                                  "warming_up": counts["warming"]})
    if built == 0 and counts["no_prices"] == 0:
        raise PhaseError("No ticker document could be built.")
    return 0


# ---------------------------------------------------------------- phase: claude

def apply_assessment(paths, cfg, i, tickers, doc, st, result, identities):
    """Put one ticker's assessment (the model's, checked, or the code's) into
    its document and its own state, and write both. True when the model wrote
    it, "levels_from_code" when the model wrote it but its levels failed the
    checks, False when the code wrote it."""
    from stocks import claude
    from stocks.analysis import learner

    t = tickers[i]
    assessment, note = claude.assessment_from(doc, result)
    names = other_names(identities, t)
    lessons = learner.clean_lessons(assessment.get("lessons_added") or [], st.get("lessons") or [], t,
                                    tuple(tickers), names)
    assessment["lessons_added"] = lessons
    llm = result["outcome"] == "ok"
    served = result["served_by"] if llm else None
    doc["assessment"] = assessment
    doc["llm"] = bool(llm)
    doc["model"] = served
    doc["cost"] = result["cost"]
    if note:
        doc["notes"] = [x for x in doc.get("notes") or [] if x not in claude.UNAVAILABLE.values()] + [note]
    if llm and assessment.get("levels_source") == "code":
        llm = "levels_from_code"
    st = learner.attach_assessment(st, st.get("last_as_of") or doc.get("data_as_of"), assessment, lessons, served,
                                   cfg["lessons_cap"], tuple(tickers), names)
    st = learner.trim_state(st, stocks.STATE_MAX_BYTES, insider_days=cfg["insider_days"],
                            congress_days=cfg["congress_days"], bootstrap_years=cfg["bootstrap_years"])
    learner.check_state(t, st, tuple(tickers), cik=(st.get("identity") or {}).get("cik"))
    doc["learner"] = learner.learner_block(st)
    doc = fit_document(doc)
    write_json(paths.out_state(i), st)
    write_json(paths.out_ticker(i), doc)
    return llm


def phase_claude(args, deps, red, paths, now):
    from stocks import claude
    from stocks.analysis import learner

    conf = register_known(red, paths)
    tickers = conf.get("tickers") or []
    cfg = load_cfg(args.config)
    model = claude.check_model(conf.get("model"), cfg)
    overview = read_json(paths.overview)
    n = len(tickers)
    # A file that is not this position's ticker (a reused folder) is never used.
    docs = [d if isinstance(d, dict) and d.get("ticker") == t else None
            for t, d in zip(tickers, (read_json(paths.out_ticker(i)) for i in range(n)))]
    states = [x if isinstance(x, dict) and x.get("ticker") == t else None
              for t, x in zip(tickers, (read_json(paths.out_state(i)) for i in range(n)))]
    identities = {t: ((s or {}).get("identity") or {}) for t, s in zip(tickers, states)}
    today = ny_today(now)

    client = None
    if not deps.env("ANTHROPIC_API_KEY") and deps.anthropic_client is None:
        red.warn("ANTHROPIC_API_KEY is not set, so the overview has no summary and every ticker keeps its code levels.")
    else:
        try:
            client = deps.claude_client()
        except ImportError:
            red.warn("The anthropic SDK is not installed, so every ticker keeps its code levels.")
        except Exception as e:
            red.warn(f"Could not set up the Claude client ({privacy.error_name(e)}); every ticker keeps its code levels.")

    jobs, owners = [], []
    # A request that cannot be built costs that one answer, never the others.
    if isinstance(overview, dict):
        try:
            jobs.append(claude.overview_job(overview, today, cfg, model))
            owners.append(None)
        except Exception as e:
            red.warn(f"The overview request could not be built ({privacy.error_name(e)}); it has no summary today.")
    try:
        market = claude.market_inputs(overview or {}, today)
    except Exception as e:
        market = {"macro": [], "calendar": []}
        red.warn(f"The market inputs could not be read ({privacy.error_name(e)}); the tickers are asked without them.")
    for i, t in enumerate(tickers):
        doc, st = docs[i], states[i]
        if not isinstance(doc, dict) or not isinstance(st, dict) or not claude.has_levels(doc):
            continue
        try:
            context = learner.claude_context(st)  # this ticker's own track record and lessons only
            jobs.append(claude.ticker_job(doc, context, market, cfg, model, f"Assessment {i + 1} of {n}"))
        except Exception as e:
            red.warn(f"Ticker {i + 1} of {n} keeps its code levels ({privacy.error_name(e)}).")
            continue
        owners.append(i)
    if client is not None:
        red.log(f"Asking {model} (effort {cfg['effort']}) for {plural(len(jobs), 'answer')}: "
                f"{'the overview and ' if owners and owners[0] is None else ''}"
                f"{plural(sum(1 for o in owners if o is not None), 'ticker')}.")
    counts = {"total": 0.0, "model": 0, "code": 0}
    base_cost = float((read_json(paths.run, {}) or {}).get("cost_usd") or 0.0)

    def keep(k, result):
        """Writes one result as soon as it is known (the step can be stopped
        at its timeout; what was paid for is kept)."""
        cost = result["cost"]
        counts["total"] += float(cost["cost_usd"] or 0.0)
        job_owner = owners[k]
        if job_owner is None:
            if result["outcome"] == "ok":
                overview["summary"] = result["value"]
                overview["model"] = result["served_by"]
            overview["cost"] = cost
        else:
            i = job_owner
            try:
                llm = apply_assessment(paths, cfg, i, tickers, docs[i], states[i], result, identities)
            except Exception as e:  # that ticker keeps its document and state as analyzed
                red.warn(f"Ticker {i + 1} of {n} keeps its code levels ({privacy.error_name(e)}).")
                llm = False
            counts["model" if llm else "code"] += 1
            if llm == "levels_from_code":
                counts["model_code_levels"] = counts.get("model_code_levels", 0) + 1
        if isinstance(overview, dict):
            overview["run"]["cost_usd"] = round(base_cost + counts["total"], 6)
            overview["run"]["model"] = model
            write_json(paths.overview, overview)
        run = read_json(paths.run, {}) or {}
        run["cost_usd"] = round(base_cost + counts["total"], 6)
        run.setdefault("phases", {})["claude"] = {"ok": False, "partial": True, "model": counts["model"],
                                                  "code": counts["code"]}
        write_json(paths.run, run)

    deadline = deps.clock() + CLAUDE_STEP_SECONDS
    results, cap = claude.run_all(client, jobs, cfg, red.log, deps.claude_budget, on_result=keep, deadline=deadline,
                                  clock=deps.clock)
    if client is None or all(job.get("too_large") for job in jobs):
        red.log(cap.line() + " No request was sent.")
    # Tickers that had no call (no levels yet) keep their documents as analyzed.
    if isinstance(overview, dict):
        overview["run"]["model"] = model
        write_json(paths.overview, overview)
    swapped = counts.get("model_code_levels", 0)
    red.log(f"Assessments: {counts['model']} written by the model"
            + (f" ({swapped} of them with levels from the code, since the model's did not pass the checks)" if swapped else "")
            + f", {counts['code']} from the code alone.")
    run = read_json(paths.run, {}) or {}
    run["cost_usd"] = round(base_cost + counts["total"], 6)
    write_json(paths.run, run)
    update_run(paths, "claude", {"ok": True, "requests": cap.requests, "skipped": cap.skipped,
                                 "model": counts["model"], "code": counts["code"]})
    return 0


# ---------------------------------------------------------------- phase: upload

def phase_upload(args, deps, red, paths, now):
    from stocks.analysis import learner
    from stocks.worker_client import WorkerError

    conf = register_known(red, paths)
    tickers = conf.get("tickers") or []
    state_errors = set(conf.get("state_errors") or [])
    n = len(tickers)
    try:
        client = deps.worker()
    except WorkerError as e:
        raise PhaseError(f"Could not reach the Worker: {e.reason}.") from None
    saved = states = 0
    problems = 0
    out_states = [read_json(paths.out_state(i)) for i in range(n)]
    identities = {t: ((s or {}).get("identity") or {}) for t, s in zip(tickers, out_states)
                  if isinstance(s, dict) and s.get("ticker") == t}
    for i, t in enumerate(tickers):
        doc = read_json(paths.out_ticker(i))
        st = out_states[i]
        if isinstance(st, dict) and i not in state_errors:
            try:
                learner.check_state(t, st, tuple(tickers), cik=(st.get("identity") or {}).get("cik"))
                st = learner.prune_lessons(st, tuple(tickers), other_names(identities, t))
                if stocks.size_of(st) > stocks.STATE_MAX_BYTES:
                    raise ValueError("too large")
                client.put_state(t, st)
                states += 1
            except (ValueError, WorkerError) as e:
                problems += 1
                reason = e.reason if isinstance(e, WorkerError) else privacy.error_name(e)
                red.warn(f"The learning state of ticker {i + 1} of {n} was not saved ({reason}).")
        if isinstance(doc, dict):
            if doc.get("ticker") != t or doc.get("kind") != "stocks.ticker":
                problems += 1
                red.warn(f"The analysis of ticker {i + 1} of {n} did not pass the checks and was not saved.")
                continue
            try:
                client.put_ticker(t, doc)
                saved += 1
            except WorkerError as e:
                problems += 1
                red.warn(f"The analysis of ticker {i + 1} of {n} was not saved ({e.reason}).")
    overview = read_json(paths.overview)
    overview_saved = False
    if isinstance(overview, dict):
        run = read_json(paths.run, {}) or {}
        overview["run"]["finished_at"] = stocks.iso(now if args.now else stocks.utc_now())
        overview["run"]["cost_usd"] = round(float(run.get("cost_usd") or 0.0), 6)
        try:
            client.put_overview(overview)
            overview_saved = True
        except WorkerError as e:
            problems += 1
            red.warn(f"The market overview was not saved ({e.reason}).")
    red.log(f"Saved {plural(saved, 'analysis', 'analyses')}"
            + (" and the market overview" if overview_saved else "") + f", {plural(states, 'learning state')}.")
    run = update_run(paths, "upload", {"ok": saved > 0, "documents": saved, "states": states,
                                       "overview": overview_saved, "problems": problems})
    red.log(f"Total cost of the run: ${float(run.get('cost_usd') or 0.0):.4f}.")
    if saved == 0:
        raise PhaseError("No analysis was saved.")
    return 0


# ---------------------------------------------------------------- phase: local

def phase_local(args, deps, red, paths, now):
    """Every phase in one process, for the owner's own machine: the ticker list
    from --tickers, the states from and the results to --out (outside the
    repository). Nothing is sent to a Worker."""
    from stocks import claude

    if not args.tickers or not args.out:
        raise PhaseError("The local phase needs --tickers and --out.")
    out = os.path.realpath(os.path.abspath(args.out))
    for root in (stocks.ROOT, os.environ.get("GITHUB_WORKSPACE", "")):
        if root and privacy._inside(out, os.path.realpath(root)):
            raise PhaseError("--out must be outside the repository.")
    tickers = []
    for raw in args.tickers.split(","):
        t = stocks.normalize_ticker(raw)
        if t and t not in tickers:
            tickers.append(t)
    red.register(tickers)
    if not tickers or len(tickers) > stocks.MAX_TICKERS:
        raise PhaseError(f"Give 1 to {stocks.MAX_TICKERS} tickers.")
    cfg = load_cfg(args.config)
    write_json(paths.run, {"reason": "manual", "started_at": stocks.iso(now), "phases": {}, "cost_usd": 0.0})
    for i, t in enumerate(tickers):
        remove(paths.state(i))
        st = clean_state(t, read_json(os.path.join(out, "state", f"{t}.json")), tickers)
        if st is not None:
            write_json(paths.state(i), st)
    write_json(paths.config, {"tickers": tickers, "model": claude.check_model(cfg["model"], cfg), "overrides": {},
                              "reason": "manual", "state_errors": []})
    for phase in (phase_fetch, phase_analyze, phase_claude):
        phase(args, deps, red, paths, now)
    make_dir(out)
    for i, t in enumerate(tickers):
        for src, kind in ((paths.out_state(i), "state"), (paths.out_ticker(i), "ticker")):
            if os.path.isfile(src):
                dest = os.path.join(out, kind, f"{t}.json")
                make_dir(os.path.dirname(dest))
                shutil.copyfile(src, dest)
    if os.path.isfile(paths.overview):
        shutil.copyfile(paths.overview, os.path.join(out, "overview.json"))
    red.log("Wrote the results to the --out folder.")
    return 0


# ---------------------------------------------------------------- main

RUNNERS = {"config": phase_config, "fetch": phase_fetch, "analyze": phase_analyze, "claude": phase_claude,
           "upload": phase_upload, "local": phase_local}


def build_parser():
    parser = argparse.ArgumentParser(description="The private stock run, one phase at a time.")
    parser.add_argument("phase", choices=PHASES)
    parser.add_argument("--dir", required=True, help="the private working folder, outside the repository")
    parser.add_argument("--now", default=None, help="a fixed clock, ISO 8601 (tests and local runs)")
    parser.add_argument("--config", default=None, help="config.yml (default: the repository's)")
    parser.add_argument("--reason", default="scheduled", choices=("scheduled", "manual"))
    parser.add_argument("--tickers", default=None, help="local only: tickers separated by commas")
    parser.add_argument("--out", default=None, help="local only: where to read states and write results")
    return parser


def main(argv=None, deps=None):
    deps = deps or Deps(install_hooks=True)
    red = privacy.Redactor()
    if deps.install_hooks:
        privacy.install_excepthook(red)
    args = build_parser().parse_args(argv)
    try:
        now = parse_now(args.now) or stocks.utc_now()
    except ValueError:
        stocks.log("Stopped: --now is not an ISO 8601 time.")
        return 1
    try:
        paths = Paths(args.dir)
    except (stocks.StocksError, OSError) as e:
        stocks.log(f"Stopped: {e if isinstance(e, stocks.StocksError) else 'the working folder cannot be made'}")
        return 1
    try:
        return RUNNERS[args.phase](args, deps, red, paths, now)
    except PhaseError as e:
        red.log(f"Stopped: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
