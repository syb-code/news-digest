"""SEC EDGAR: the ticker map, submissions, earnings per share from XBRL company
facts, Form 4 insider transactions and 13F fund holdings (contract section 5.3).

Every request goes through HttpClient, which sends the caller's User-Agent (SEC
asks for a contact string with an email) and keeps SEC requests at most five a
second. Nothing here prints; errors are FetchError or ValueError with plain
reasons that never name a company, a CIK or a URL.

Funds are compared quarter to quarter by CUSIP, never by name: the same CUSIP
changes its issuer name between filings ("BANK AMERICA CORP" became "BANK OF
AMER CORP"). A ticker is tied to a CUSIP once, by CUSIP from the private
overrides or by a careful match of the issuer's name (issuer_key), and the
CUSIP is then kept in the ticker's own state.
"""
import io
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

from stocks import finra_symbol, normalize_ticker, round_pct, round_price
from stocks.data import FetchError

TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE = "https://data.sec.gov/submissions/{name}"
FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{name}"
INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/index.json"
FOLDER = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/"

ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
CUSIP_RE = re.compile(r"^[0-9A-Z]{9}$")
PAGE_NAME_RE = re.compile(r"^CIK\d{10}-submissions-\d{3}\.json$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DTD_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)

NO_EPS_NOTE = "No earnings per share in the SEC data yet."
STALE_EPS_NOTE = "The latest earnings per share in the SEC data are too old to use."
# Days from the end of the latest period after which EPS is not used: about 15
# months for four quarters, 21 for a fiscal year (a 20-F comes up to 4 months
# after the year ends).
EPS_MAX_AGE_DAYS = {"four_quarters": 460, "annual": 640}
NOTICE_NOTE = "Holdings for this quarter are reported by another manager."
AMBIGUOUS_NOTE = "Several share classes match; add the CUSIP to the overrides."
SHARED_KEY_NOTE = "Another company has a similar name, so 13F holdings are matched only by CUSIP; add it to the overrides."
MAX_FORM4 = 120

EPS_CONCEPTS = (
    ("us-gaap", "EarningsPerShareDiluted"),
    ("us-gaap", "EarningsPerShareBasicAndDiluted"),
    ("ifrs-full", "DilutedEarningsLossPerShare"),
    ("us-gaap", "EarningsPerShareBasic"),
    ("ifrs-full", "BasicEarningsLossPerShare"),
)

CODE_LABELS = {
    "P": "Open market or private purchase",
    "S": "Open market or private sale",
    "A": "Grant or award",
    "M": "Exercise or conversion of a derivative",
    "F": "Shares withheld for tax or exercise price",
    "G": "Gift",
    "D": "Disposition to the issuer",
    "C": "Conversion of a derivative",
    "X": "Exercise of an in-the-money derivative",
    "J": "Other acquisition or disposition",
    "W": "Acquired or disposed by will",
    "I": "Discretionary transaction",
    "K": "Equity swap",
    "V": "Voluntary report",
    "E": "Expiration of a short derivative",
    "H": "Expiration of a long derivative",
    "L": "Small acquisition",
    "O": "Exercise of an out-of-the-money derivative",
    "U": "Shares tendered",
    "Z": "Voting trust deposit or withdrawal",
}

DOMESTIC_FORMS = {"10-K", "10-Q", "10-KT", "10-QT"}
FOREIGN_FORMS = {"20-F", "40-F", "6-K", "20-FR", "40-FR"}
PERIODIC_FORMS = {"10-Q", "10-K", "20-F", "40-F", "10-KT", "10-QT"}
FORM4_FORMS = {"4", "4/A"}
FUND_FORMS = {"13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A"}


# ---------------------------------------------------------------- helpers

def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _day(value):
    s = str(value or "").strip()[:10]
    if not DATE_RE.match(s):
        return None
    try:
        date.fromisoformat(s)
    except ValueError:
        return None
    return s


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = _day(value)
    if s is None:
        raise ValueError("not a date")
    return date.fromisoformat(s)


def _cik(value):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("not a CIK") from None
    if n <= 0:
        raise ValueError("not a CIK")
    return n


def _accession(value):
    s = str(value or "").strip()
    if not ACCESSION_RE.match(s):
        raise ValueError("not an accession number")
    return s


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _safe_xml(xml):
    """xml as bytes, refusing DTDs and entities (the documents never need them)."""
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    if not isinstance(xml, (bytes, bytearray)):
        raise ValueError("not an XML document")
    data = bytes(xml)
    if _DTD_RE.search(data):
        raise ValueError("the XML document declares entities")
    return data


def _parse_xml(xml):
    try:
        return ET.fromstring(_safe_xml(xml))
    except ET.ParseError:
        raise ValueError("the XML document cannot be read") from None


def _child(el, *names):
    """The first descendant along a path of local names, or None."""
    cur = el
    for name in names:
        if cur is None:
            return None
        nxt = None
        for c in cur:
            if _local(c.tag) == name:
                nxt = c
                break
        cur = nxt
    return cur


def _text(el, *names):
    node = _child(el, *names) if names else el
    if node is None or node.text is None:
        return None
    s = " ".join(node.text.split())
    return s or None


def _children(el, name):
    return [c for c in el if _local(c.tag) == name] if el is not None else []


def _bool(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "y")


# ---------------------------------------------------------------- ticker map

def parse_company_tickers(doc):
    """{"AAPL": {"cik": 320193, "name": "Apple Inc.", "classes": 1}} from
    company_tickers.json. classes counts the tickers SEC lists for that CIK.
    The first listing of a ticker wins."""
    if isinstance(doc, dict):
        entries = list(doc.values())
    elif isinstance(doc, list):
        entries = doc
    else:
        raise ValueError("the ticker map is not a JSON object")
    out, per_cik = {}, {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        t = normalize_ticker(e.get("ticker"))
        try:
            cik = _cik(e.get("cik_str", e.get("cik")))
        except ValueError:
            continue
        name = " ".join(str(e.get("title") or "").split())
        if not t or not name or t in out:
            continue
        out[t] = {"cik": cik, "name": name}
        per_cik[cik] = per_cik.get(cik, 0) + 1
    if not out:
        raise ValueError("the ticker map is empty")
    # How many companies (CIKs) share each issuer_key: a 13F name match is only
    # safe for a key that one company has (TARGET, GRAHAM and FIRST BANCORP are
    # each the key of several registrants).
    key_ciks = {}
    for v in out.values():
        k = issuer_key(v["name"])
        if k:
            key_ciks.setdefault(k, set()).add(v["cik"])
    for v in out.values():
        v["classes"] = per_cik[v["cik"]]
        v["key_ciks"] = len(key_ciks.get(issuer_key(v["name"]), ())) or 1
    return out


def company_tickers(http):
    return parse_company_tickers(http.get(TICKERS, cache=True).json())


# ---------------------------------------------------------------- submissions

def _arrays(block):
    """Rows (dicts) from a block of parallel arrays."""
    if not isinstance(block, dict):
        return []
    forms = block.get("form")
    if not isinstance(forms, list):
        return []
    keys = [k for k, v in block.items() if isinstance(v, list) and len(v) == len(forms)]
    return [{k: block[k][i] for k in keys} for i in range(len(forms))]


def _filings(subs):
    """Every filing row in subs: the recent block plus an older page, if read."""
    filings = subs.get("filings") if isinstance(subs, dict) else None
    if not isinstance(filings, dict):
        return []
    rows = _arrays(filings.get("recent"))
    rows += _arrays(filings.get("older"))
    return rows


def submissions(http, cik, since=None):
    """The submissions JSON as served. With since (a date), when the recent
    block's oldest filing is newer than since and SEC lists older pages, the
    first older page is read too (one request) and kept under
    filings["older"], so form4_refs and fund_refs see a full year."""
    url = SUBMISSIONS.format(cik=_cik(cik))
    doc = http.get(url, cache=True).json()
    if not isinstance(doc, dict) or not isinstance(doc.get("filings"), dict):
        raise ValueError("the submissions answer has no filings")
    if since is None:
        return doc
    since = _as_date(since)
    recent = _arrays(doc["filings"].get("recent"))
    days = [d for d in (_day(r.get("filingDate")) for r in recent) if d]
    pages = doc["filings"].get("files")
    if not days or min(days) <= since.isoformat() or not isinstance(pages, list) or not pages:
        return doc
    first = pages[0] if isinstance(pages[0], dict) else {}
    name = str(first.get("name") or "")
    if not PAGE_NAME_RE.match(name):
        return doc
    try:
        older = http.get(SUBMISSIONS_PAGE.format(name=name), cache=True).json()
    except (FetchError, ValueError):
        return doc
    if not isinstance(older, dict):
        return doc
    out = dict(doc)
    out["filings"] = dict(doc["filings"])
    out["filings"]["older"] = older
    return out


def filer_type(subs):
    """"domestic" (files 10-K or 10-Q), "foreign" (20-F, 40-F or 6-K and no
    10-K or 10-Q) or "unknown"."""
    forms = {str(r.get("form") or "").upper().replace("/A", "") for r in _filings(subs)}
    if forms & DOMESTIC_FORMS:
        return "domestic"
    if forms & FOREIGN_FORMS:
        return "foreign"
    return "unknown"


def identity(ticker, tickers_map, subs, cusips=None):
    """The Identity of a ticker. cik and name are None when SEC does not know it.
    classes is the number of tickers SEC lists for the company (used to decide
    a 13F name match between share classes)."""
    t = normalize_ticker(ticker)
    if t is None:
        raise ValueError("not a ticker")
    entry = (tickers_map or {}).get(t) or {}
    cik = entry.get("cik")
    name = entry.get("name")
    if isinstance(subs, dict):
        if cik is None:
            try:
                cik = _cik(subs.get("cik"))
            except ValueError:
                cik = None
        if not name:
            name = " ".join(str(subs.get("name") or "").split()) or None
    classes = entry.get("classes")
    key_ciks = entry.get("key_ciks")
    if key_ciks is None and name and tickers_map:
        k = issuer_key(name)
        key_ciks = len({v.get("cik") for v in tickers_map.values() if issuer_key(v.get("name")) == k}) or None
    if classes is None and cik is not None:
        classes = sum(1 for v in (tickers_map or {}).values() if v.get("cik") == cik) or None
    if classes is None and isinstance(subs, dict) and isinstance(subs.get("tickers"), list):
        classes = len([x for x in subs["tickers"] if x]) or None
    exchanges = []
    if isinstance(subs, dict) and isinstance(subs.get("exchanges"), list):
        for x in subs["exchanges"]:
            x = " ".join(str(x or "").split())
            if x and x not in exchanges:
                exchanges.append(x)
    clean = []
    for c in cusips or ():
        c = str(c or "").strip().upper()
        if CUSIP_RE.match(c) and c not in clean:
            clean.append(c)
    return {
        "ticker": t,
        "cik": cik,
        "name": name,
        "filer_type": filer_type(subs) if isinstance(subs, dict) else "unknown",
        "exchanges": exchanges,
        "cusips": clean,
        "finra_symbol": finra_symbol(t),
        "issuer_key": issuer_key(name) if name else None,
        "classes": classes or 1,
        # None when the SEC map is not at hand: a name match is then not safe.
        "key_ciks": (key_ciks or 1) if tickers_map else None,
    }


# ---------------------------------------------------------------- earnings per share

def company_facts(http, cik):
    """The companyfacts JSON; {} when SEC has no XBRL facts for the company (404)."""
    resp = http.get(FACTS.format(cik=_cik(cik)), allow=(404,))
    if resp.status == 404:
        return {}
    doc = resp.json()
    if not isinstance(doc, dict):
        raise ValueError("the company facts answer is not a JSON object")
    return doc


def eps_for(http, cik, today=None):
    """EpsSummary for a CIK that never raises: a failed request gives
    not_available with a plain note."""
    try:
        return eps_summary(company_facts(http, cik), today=today)
    except FetchError:
        return not_available_eps("Earnings per share could not be read from the SEC today.")
    except ValueError:
        return not_available_eps("The SEC data for earnings per share could not be read.")


def _eps_units(units):
    """The (unit, rows) pairs of a concept's per-share units, in preference
    order: USD/shares first, then the unit with the most rows."""
    if not isinstance(units, dict):
        return []
    per_share = {u: rows for u, rows in units.items()
                 if isinstance(u, str) and re.match(r"^[A-Z]{3}/shares$", u) and isinstance(rows, list) and rows}
    order = sorted(per_share, key=lambda u: (u != "USD/shares", -len(per_share[u]), u))
    return [(u, per_share[u]) for u in order]


def _periods(rows):
    """Rows with start, end and a value, one per (start, end): the latest filed."""
    best = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        start, end, val = _day(r.get("start")), _day(r.get("end")), _num(r.get("val"))
        if not start or not end or val is None or end < start:
            continue
        filed = _day(r.get("filed")) or ""
        key = (start, end)
        if key not in best or filed >= best[key]["filed"]:
            best[key] = {"start": start, "end": end, "eps": val, "form": str(r.get("form") or ""),
                         "filed": filed,
                         "days": (date.fromisoformat(end) - date.fromisoformat(start)).days + 1}
    return list(best.values())


def _summarize_eps(rows):
    periods = _periods(rows)
    quarters = [p for p in periods if 80 <= p["days"] <= 100]
    annual = [p for p in periods if 350 <= p["days"] <= 380]
    nine = {p["start"]: p for p in periods if 260 <= p["days"] <= 285}
    by_end = {}
    for q in quarters:
        cur = by_end.get(q["end"])
        if cur is None or q["filed"] >= cur["filed"]:
            by_end[q["end"]] = dict(q, derived=False)
    for a in annual:
        n = nine.get(a["start"])
        if n is None or n["end"] >= a["end"]:
            continue
        start = (date.fromisoformat(n["end"]) + timedelta(days=1)).isoformat()
        days = (date.fromisoformat(a["end"]) - date.fromisoformat(start)).days + 1
        if not (80 <= days <= 100) or a["end"] in by_end:
            continue
        by_end[a["end"]] = {"start": start, "end": a["end"], "eps": a["eps"] - n["eps"], "form": a["form"],
                            "filed": max(a["filed"], n["filed"]), "days": days, "derived": True}
    qs = [by_end[e] for e in sorted(by_end)]
    annual.sort(key=lambda p: (p["end"], p["filed"]))
    latest_annual = annual[-1] if annual else None
    return qs, latest_annual


def _quarter_out(q):
    return {"start": q["start"], "end": q["end"], "eps": round(q["eps"], 4), "form": q["form"],
            "filed": q["filed"] or None, "derived": bool(q["derived"])}


def _annual_out(a):
    if a is None:
        return None
    return {"start": a["start"], "end": a["end"], "eps": round(a["eps"], 4), "form": a["form"],
            "filed": a["filed"] or None}


def not_available_eps(note=NO_EPS_NOTE):
    return {"status": "not_available", "concept": None, "unit": None, "currency": None, "basis": None,
            "eps_ttm": None, "quarters": [], "annual": None, "latest_end": None, "latest_filed": None,
            "note": note}


def _eps_candidate(taxonomy, name, unit, rows):
    """The EpsSummary one concept and unit give, or None."""
    quarters, annual = _summarize_eps(rows)
    last4 = quarters[-4:]
    latest_filed = max((_day(r.get("filed")) or "" for r in rows if isinstance(r, dict)), default="") or None
    base = {"status": "ok", "concept": f"{taxonomy}:{name}", "unit": unit, "currency": unit.split("/")[0],
            "latest_filed": latest_filed, "note": None}
    four = (len(last4) == 4
            and 250 <= (date.fromisoformat(last4[-1]["end"]) - date.fromisoformat(last4[0]["end"])).days <= 300
            and (annual is None or annual["end"] <= last4[-1]["end"]))
    if four:
        return dict(base, basis="four_quarters", eps_ttm=round(sum(q["eps"] for q in last4), 4),
                    quarters=[_quarter_out(q) for q in last4], annual=_annual_out(annual),
                    latest_end=last4[-1]["end"])
    if annual is not None:
        return dict(base, basis="annual", eps_ttm=round(annual["eps"], 4),
                    quarters=[_quarter_out(q) for q in quarters[-4:]], annual=_annual_out(annual),
                    latest_end=annual["end"])
    return None


def eps_summary(facts, today=None):
    """Trailing earnings per share (EpsSummary): the sum of the last four
    discrete quarters (Q4 derived as the year minus the first nine months),
    else the latest fiscal year (the usual case for 20-F filers), else
    not_available.

    Every concept and per-share unit is tried and the one whose result ends
    latest wins (the concept order, then USD, breaks a tie): a filer that
    stopped tagging one concept years ago still tags another one today. With
    today (a date), a result that ends more than EPS_MAX_AGE_DAYS before it is
    not used (STALE_EPS_NOTE)."""
    all_facts = facts.get("facts") if isinstance(facts, dict) else None
    if not isinstance(all_facts, dict):
        return not_available_eps()
    best, best_key, rank = None, None, 0
    for taxonomy, name in EPS_CONCEPTS:
        concept = (all_facts.get(taxonomy) or {}).get(name) if isinstance(all_facts.get(taxonomy), dict) else None
        if not isinstance(concept, dict):
            continue
        for unit, rows in _eps_units(concept.get("units")):
            rank += 1
            found = _eps_candidate(taxonomy, name, unit, rows)
            if found is None:
                continue
            key = (found["latest_end"], -rank)
            if best_key is None or key > best_key:
                best, best_key = found, key
    if best is None:
        return not_available_eps()
    if today is not None:
        age = (_as_date(today) - date.fromisoformat(best["latest_end"])).days
        if age > EPS_MAX_AGE_DAYS[best["basis"]]:
            return not_available_eps(STALE_EPS_NOTE)
    return best


def eps_due(subs, cache, now, max_age_days=30):
    """True when EPS should be read again: no usable cache, a 10-Q, 10-K, 20-F
    or 40-F filed after the cached latest_filed, or a cache older than
    max_age_days."""
    if not isinstance(cache, dict) or not isinstance(cache.get("summary"), dict):
        return True
    fetched = str(cache.get("fetched_at") or "")
    try:
        when = datetime.fromisoformat(fetched)
    except ValueError:
        return True
    now_d = now.date() if isinstance(now, datetime) else _as_date(now)
    if (now_d - when.date()).days > max_age_days:
        return True
    latest = _day(cache.get("latest_filed")) or _day(cache["summary"].get("latest_filed")) or ""
    for r in _filings(subs):
        form = str(r.get("form") or "").upper().replace("/A", "")
        filed = _day(r.get("filingDate")) or ""
        if form in PERIODIC_FORMS and filed > latest:
            return True
    return False


# ---------------------------------------------------------------- Form 4

def _document_name(primary):
    """The XML file name of a filing's primary document: any leading xsl...
    folder removed (that folder serves the rendered HTML)."""
    name = str(primary or "").strip()
    name = re.sub(r"^xsl[^/]*/", "", name)
    if "/" in name or not name.lower().endswith(".xml") or name.startswith("."):
        return None
    return name


def form4_refs(subs, since):
    """FilingRefs of Forms 4 and 4/A filed on or after since, newest first."""
    since_s = _as_date(since).isoformat()
    try:
        cik = _cik(subs.get("cik"))
    except (ValueError, AttributeError):
        raise ValueError("the submissions have no CIK") from None
    out, seen = [], set()
    for r in _filings(subs):
        form = str(r.get("form") or "").strip().upper()
        filed = _day(r.get("filingDate"))
        if form not in FORM4_FORMS or not filed or filed < since_s:
            continue
        try:
            acc = _accession(r.get("accessionNumber"))
        except ValueError:
            continue
        doc = _document_name(r.get("primaryDocument"))
        if acc in seen or doc is None:
            continue
        seen.add(acc)
        out.append({"cik": cik, "accession": acc, "form": form, "filed": filed,
                    "report_date": _day(r.get("reportDate")), "document": doc})
    out.sort(key=lambda x: (x["filed"], x["accession"]), reverse=True)
    return out


def form4_url(ref):
    return ARCHIVE.format(cik=_cik(ref["cik"]), accession_no_dashes=_accession(ref["accession"]).replace("-", ""),
                          name=urllib.parse.quote(str(ref["document"]), safe=""))


def _role(rel):
    if rel is None:
        return None
    parts = []
    if _bool(_text(rel, "isDirector")):
        parts.append("Director")
    if _bool(_text(rel, "isOfficer")):
        title = _text(rel, "officerTitle")
        parts.append(f"Officer ({title})" if title else "Officer")
    if _bool(_text(rel, "isTenPercentOwner")):
        parts.append("10 percent owner")
    if _bool(_text(rel, "isOther")):
        other = _text(rel, "otherText")
        parts.append(f"Other ({other})" if other else "Other")
    return ", ".join(parts) or None


def _value(el, *path):
    """The <value> under a path (Form 4 wraps every field in one)."""
    return _text(el, *path, "value")


def parse_form4(xml, ref):
    """InsiderTxns of one Form 4 (derivative rows kept with derivative: true)."""
    root = _parse_xml(xml)
    if _local(root.tag) != "ownershipDocument":
        raise ValueError("not a Form 4 document")
    owners = _children(root, "reportingOwner")
    names = [n for n in (_text(o, "reportingOwnerId", "rptOwnerName") for o in owners) if n]
    owner = " and ".join(names) or None
    owner_cik = _text(owners[0], "reportingOwnerId", "rptOwnerCik") if owners else None
    role = _role(_child(owners[0], "reportingOwnerRelationship")) if owners else None
    plan = _bool(_text(root, "aff10b5One"))
    period = _day(_text(root, "periodOfReport"))
    form = str(ref.get("form") or _text(root, "documentType") or "4")
    # An amendment restates the original filing (filed on dateOfOriginalSubmission).
    original = _day(_text(root, "dateOfOriginalSubmission")) if form.upper() == "4/A" else None
    url = form4_url(ref)
    out = []
    for table, row_name, derivative in (("nonDerivativeTable", "nonDerivativeTransaction", False),
                                        ("derivativeTable", "derivativeTransaction", True)):
        for tx in _children(_child(root, table), row_name):
            code = (_text(tx, "transactionCoding", "transactionCode") or "").upper()[:1] or None
            shares = _num(_value(tx, "transactionAmounts", "transactionShares"))
            price = _num(_value(tx, "transactionAmounts", "transactionPricePerShare"))
            after = _num(_value(tx, "postTransactionAmounts", "sharesOwnedFollowingTransaction"))
            value = round(shares * price, 2) if shares is not None and price is not None else None
            out.append({
                "accession": ref.get("accession"),
                "form": form,
                "filed": ref.get("filed"),
                "date": _day(_value(tx, "transactionDate")) or period,
                "owner": owner,
                "owner_cik": owner_cik,
                "role": role,
                "code": code,
                "code_label": CODE_LABELS.get(code, "Other"),
                "acquired_disposed": _value(tx, "transactionAmounts", "transactionAcquiredDisposedCode"),
                "shares": shares,
                "price": round_price(price),
                "value": value,
                "shares_after": after,
                "ownership": _value(tx, "ownershipNature", "directOrIndirectOwnership"),
                "nature": _value(tx, "ownershipNature", "natureOfOwnership"),
                "plan_10b5_1": plan,
                "derivative": derivative,
                "security": _value(tx, "securityTitle"),
                "url": url,
                "period": period or _day(ref.get("report_date")),
                "amends": original,
            })
    return out


def drop_amended(txns):
    """Transactions without the rows a Form 4/A restates: the original Form 4
    rows of the same owner and period of report filed on the amendment's
    dateOfOriginalSubmission."""
    amended = {(t.get("owner_cik"), t.get("period"), t.get("amends")) for t in txns
               if str(t.get("form") or "").upper() == "4/A" and t.get("amends") and t.get("period")}
    if not amended:
        return list(txns)
    return [t for t in txns if not (str(t.get("form") or "").upper() == "4"
                                    and (t.get("owner_cik"), t.get("period"), t.get("filed")) in amended)]


def insider_transactions(http, subs, since, cached=None, limit=MAX_FORM4):
    """Form 4 transactions since a date, reading only filings not in the cache.

    cached: {"seen": {accession: filed}, "transactions": [InsiderTxn]} from the
    ticker's state. Returns {"transactions": [...] newest first, "seen": {...},
    "status": "ok" | "partial" | "not_available", "read": n, "failed": n,
    "skipped": n}. Documents beyond limit wait for the next run."""
    cached = cached if isinstance(cached, dict) else {}
    seen = {k: v for k, v in (cached.get("seen") or {}).items() if isinstance(k, str)}
    since_s = _as_date(since).isoformat()
    txns = [t for t in (cached.get("transactions") or [])
            if isinstance(t, dict) and str(t.get("filed") or "") >= since_s]
    refs = [r for r in form4_refs(subs, since) if r["accession"] not in seen]
    read = failed = 0
    for ref in refs[:limit]:
        try:
            body = http.get(form4_url(ref)).body
            txns.extend(parse_form4(body, ref))
            seen[ref["accession"]] = ref["filed"]
            read += 1
        except (FetchError, ValueError) as e:
            failed += 1
            # 429, or 403 from SEC (its answer to too many requests), or no time left: stop asking.
            if isinstance(e, FetchError) and (e.status in (403, 429) or e.reason == "time budget used up"):
                break
    skipped = max(0, len(refs) - read - failed)
    seen = {k: v for k, v in seen.items() if str(v or "") >= since_s}
    # Cached and new documents never overlap (by accession); drop exact copies only.
    unique, keys = [], set()
    for t in txns:
        k = json.dumps(t, sort_keys=True, default=str)
        if k not in keys:
            keys.add(k)
            unique.append(t)
    txns = sorted(drop_amended(unique), key=lambda t: (str(t.get("date") or ""), str(t.get("filed") or ""),
                                                       str(t.get("accession") or "")), reverse=True)
    status = "ok" if failed == 0 and skipped == 0 else "partial"
    return {"transactions": txns, "seen": seen, "status": status, "read": read, "failed": failed,
            "skipped": skipped}


# ---------------------------------------------------------------- 13F

def fund_refs(subs):
    """13F-HR, 13F-HR/A and 13F-NT filings, newest first (FundFilingRef)."""
    try:
        cik = _cik(subs.get("cik"))
    except (ValueError, AttributeError):
        raise ValueError("the submissions have no CIK") from None
    out, seen = [], set()
    for r in _filings(subs):
        form = str(r.get("form") or "").strip().upper()
        if form not in FUND_FORMS:
            continue
        period, filed = _day(r.get("reportDate")), _day(r.get("filingDate"))
        try:
            acc = _accession(r.get("accessionNumber"))
        except ValueError:
            continue
        if not period or not filed or acc in seen:
            continue
        seen.add(acc)
        out.append({"cik": cik, "accession": acc, "form": form, "period": period, "filed": filed})
    out.sort(key=lambda x: (x["filed"], x["accession"]), reverse=True)
    return out


def _by_period(refs):
    groups = {}
    for r in refs:
        groups.setdefault(r["period"], []).append(r)
    for g in groups.values():
        g.sort(key=lambda x: (x["filed"], x["accession"]))
    return groups


def covers_needed(refs, n=2):
    """The amendments (13F-HR/A) whose cover pages pick_periods needs to read,
    for the latest n periods with holdings."""
    out, counted = [], 0
    groups = _by_period(refs)
    for period in sorted(groups, reverse=True):
        g = groups[period]
        if g[-1]["form"].startswith("13F-NT"):
            continue
        out.extend(r for r in g if r["form"] == "13F-HR/A")
        counted += 1
        if counted >= n:
            break
    return out


def pick_periods(refs, covers=None, n=2):
    """The latest n distinct periods that have holdings, newest first:
    {"cik", "period", "accession", "form", "filed", "merge": [refs]}.

    Per period the latest 13F-HR; a later 13F-HR/A whose cover says
    RESTATEMENT replaces it; a NEW HOLDINGS amendment is listed in merge (its
    rows are added to the original's). An amendment whose cover was not read
    is left out. A period whose latest filing is a 13F-NT is skipped (another
    manager reports its holdings, NOTICE_NOTE)."""
    covers = covers or {}
    picks = []
    groups = _by_period(refs)
    for period in sorted(groups, reverse=True):
        g = groups[period]
        if g[-1]["form"].startswith("13F-NT"):
            continue
        base, merge = None, []
        for r in g:
            if r["form"] == "13F-HR":
                base, merge = r, []
            elif r["form"] == "13F-HR/A":
                kind = str((covers.get(r["accession"]) or {}).get("amendment_type") or "").upper()
                if kind == "RESTATEMENT":
                    base, merge = r, []
                elif kind == "NEW HOLDINGS" and base is not None:
                    merge.append(r)
        if base is None:
            continue
        picks.append({"cik": base["cik"], "period": period, "accession": base["accession"], "form": base["form"],
                      "filed": base["filed"], "merge": [dict(m) for m in merge]})
        if len(picks) >= n:
            break
    return picks


def filing_index(http, cik, accession):
    """The files of a filing: [{"name", "size"}] from its index.json."""
    acc = _accession(accession).replace("-", "")
    doc = http.get(INDEX.format(cik=_cik(cik), accession_no_dashes=acc), cache=True).json()
    items = ((doc or {}).get("directory") or {}).get("item") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise ValueError("the filing index has no items")
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "")
        if not name or "/" in name:
            continue
        size = _num(it.get("size"))
        out.append({"name": name, "size": int(size) if size is not None else None})
    return out


def info_table_name(items):
    """The information table of a 13F filing: the .xml file that is not the
    cover (primary_doc.xml). Names vary: infotable.xml, 56757.xml,
    form13fInfoTable.xml. The largest wins when there are several."""
    xmls = [i for i in items or () if isinstance(i, dict) and str(i.get("name") or "").lower().endswith(".xml")
            and str(i.get("name")).lower() != "primary_doc.xml"]
    if not xmls:
        return None
    xmls.sort(key=lambda i: (-(i.get("size") or 0), i["name"]))
    return xmls[0]["name"]


def _cover_date(value):
    s = str(value or "").strip()
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", s)
    if m:
        s = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    return _day(s)


def parse_cover(xml):
    """A 13F cover page (primary_doc.xml)."""
    root = _parse_xml(xml)
    if _local(root.tag) != "edgarSubmission":
        raise ValueError("not a 13F cover page")
    cover = _child(root, "formData", "coverPage")
    summary = _child(root, "formData", "summaryPage")
    if cover is None:
        raise ValueError("the 13F cover page is missing")
    others = []
    for path in (("otherManagersInfo",), ):
        for om in _children(_child(cover, *path), "otherManager"):
            others.append({"cik": _text(om, "cik"), "name": _text(om, "name")})
    for om2 in _children(_child(summary, "otherManagers2Info"), "otherManager2"):
        om = _child(om2, "otherManager")
        if om is not None:
            others.append({"cik": _text(om, "cik"), "name": _text(om, "name")})
    entries = _num(_text(summary, "tableEntryTotal")) if summary is not None else None
    value = _num(_text(summary, "tableValueTotal")) if summary is not None else None
    amendment = _text(cover, "amendmentInfo", "amendmentType")
    return {
        "form": _text(root, "headerData", "submissionType"),
        "period": _cover_date(_text(cover, "reportCalendarOrQuarter")
                              or _text(root, "headerData", "filerInfo", "periodOfReport")),
        "report_type": _text(cover, "reportType"),
        "is_amendment": _bool(_text(cover, "isAmendment")),
        "amendment_type": amendment.upper() if amendment else None,
        "manager": _text(cover, "filingManager", "name"),
        "other_managers": others,
        "entries": int(entries) if entries is not None else None,
        "value_total": value,
    }


def parse_info_table(xml):
    """{"holdings": {cusip: Holding}, "options_rows": n, "prn_rows": n, "rows": n}.

    Rows are summed by CUSIP (one issuer often spans several rows, one per
    other manager). Option rows (putCall) and principal-amount rows (PRN) are
    counted but not added to shares."""
    data = _safe_xml(xml)
    holdings, rows, options, prn = {}, 0, 0, 0
    try:
        for _event, el in ET.iterparse(io.BytesIO(data), events=("end",)):
            if _local(el.tag) != "infoTable":
                continue
            rows += 1
            fields = {}
            for c in el.iter():
                name = _local(c.tag)
                if name in ("nameOfIssuer", "titleOfClass", "cusip", "value", "sshPrnamt", "sshPrnamtType",
                            "putCall") and c.text is not None:
                    fields[name] = " ".join(c.text.split())
            el.clear()
            if fields.get("putCall"):
                options += 1
                continue
            if str(fields.get("sshPrnamtType") or "SH").upper() != "SH":
                prn += 1
                continue
            cusip = str(fields.get("cusip") or "").upper()
            shares = _num(fields.get("sshPrnamt"))
            if not CUSIP_RE.match(cusip) or shares is None:
                continue
            value = _num(fields.get("value")) or 0.0
            h = holdings.get(cusip)
            if h is None:
                holdings[cusip] = {"cusip": cusip, "name": fields.get("nameOfIssuer") or "",
                                   "title": fields.get("titleOfClass") or "", "shares": shares,
                                   "value_usd": value, "rows": 1}
            else:
                h["shares"] += shares
                h["value_usd"] += value
                h["rows"] += 1
    except ET.ParseError:
        raise ValueError("the information table cannot be read") from None
    return {"holdings": holdings, "options_rows": options, "prn_rows": prn, "rows": rows}


def merge_tables(tables):
    """Several information tables of one period (an original and its NEW
    HOLDINGS amendments) summed by CUSIP."""
    out = {"holdings": {}, "options_rows": 0, "prn_rows": 0, "rows": 0}
    for t in tables:
        for k in ("options_rows", "prn_rows", "rows"):
            out[k] += int(t.get(k) or 0)
        for cusip, h in (t.get("holdings") or {}).items():
            cur = out["holdings"].get(cusip)
            if cur is None:
                out["holdings"][cusip] = dict(h)
            else:
                cur["shares"] += h["shares"]
                cur["value_usd"] += h["value_usd"]
                cur["rows"] += h["rows"]
    return out


def read_cover(http, ref):
    """The parsed cover page of a filing (primary_doc.xml)."""
    url = ARCHIVE.format(cik=_cik(ref["cik"]), accession_no_dashes=_accession(ref["accession"]).replace("-", ""),
                         name="primary_doc.xml")
    return parse_cover(http.get(url).body)


def read_info_table(http, cik, accession):
    """The parsed information table of one 13F filing (index.json, then the table)."""
    name = info_table_name(filing_index(http, cik, accession))
    if name is None:
        raise ValueError("the filing has no information table")
    url = ARCHIVE.format(cik=_cik(cik), accession_no_dashes=_accession(accession).replace("-", ""),
                         name=urllib.parse.quote(name, safe=""))
    return parse_info_table(http.get(url).body)


def fund_periods(http, fund_cik, n=2):
    """(picks, notes) for one fund: its latest n periods with holdings. Reads
    the fund's submissions and the cover pages of recent amendments only."""
    subs = submissions(http, fund_cik)
    refs = fund_refs(subs)
    covers = {}
    for r in covers_needed(refs, n):
        try:
            covers[r["accession"]] = read_cover(http, r)
        except (FetchError, ValueError):
            pass
    notes = []
    groups = _by_period(refs)
    latest = sorted(groups, reverse=True)[:1]
    if latest and groups[latest[0]][-1]["form"].startswith("13F-NT"):
        notes.append(NOTICE_NOTE)
    return pick_periods(refs, covers, n), notes


def period_table(http, pick):
    """The information table of a picked period, NEW HOLDINGS amendments merged in."""
    tables = [read_info_table(http, pick["cik"], pick["accession"])]
    for m in pick.get("merge") or ():
        tables.append(read_info_table(http, m["cik"], m["accession"]))
    return tables[0] if len(tables) == 1 else merge_tables(tables)


# ---------------------------------------------------------------- matching a ticker to a holding

_ABBREVIATIONS = {
    "FINL": "FINANCIAL", "AMER": "AMERICA", "INTL": "INTERNATIONAL", "PETE": "PETROLEUM",
    "TECHNOLOGIES": "TECHNOLOGY", "TECH": "TECHNOLOGY", "SYS": "SYSTEMS", "SVCS": "SERVICES",
    "MGMT": "MANAGEMENT", "NATL": "NATIONAL", "COMMUNICATIONS": "COMMUNICATION",
    "PHARMACEUTICALS": "PHARMACEUTICAL", "ENTMT": "ENTERTAINMENT", "MATLS": "MATERIALS",
    "RES": "RESOURCES", "BANCORPORATION": "BANCORP", "AIRLS": "AIRLINES",
    # Added after a live check of a 3,151-row table: common 13F abbreviations.
    "TR": "TRUST", "INDS": "INDUSTRIES", "HLDG": "HOLDING", "RLTY": "REALTY", "PPTYS": "PROPERTIES",
    "PPTY": "PROPERTY", "RESH": "RESEARCH", "UTD": "UNITED", "ELECTRS": "ELECTRONICS", "PRODS": "PRODUCTS",
    "INVT": "INVESTMENT", "MKTS": "MARKETS", "CENTY": "CENTURY", "INS": "INSURANCE",
}
_TRAILING = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC", "LLC", "LP",
    "L", "P", "HOLDINGS", "HLDGS", "HOLDING", "GROUP", "GRP", "SA", "S", "A", "NV", "N", "V", "AG", "SE",
    "SPA", "NEW", "THE", "CL", "CLASS", "COM",
    "DEL",  # Delaware, as in "BERKSHIRE HATHAWAY INC DEL"
}
_STATE_SUFFIX_RE = re.compile(r"\s*/(?:[A-Z]{2}/?|NEW/)\s*$")


def issuer_key(name):
    """A company name reduced so SEC's title and a 13F's issuer name compare:
    "ALLY FINL INC" and "Ally Financial Inc." both give "ALLY FINANCIAL"."""
    s = str(name or "").upper().replace("&", " AND ")
    while True:
        cut = _STATE_SUFFIX_RE.sub("", s)
        if cut == s:
            break
        s = cut
    s = re.sub(r"[.,'()]", "", s)
    words = [_ABBREVIATIONS.get(w, w) for w in re.split(r"[\s\-]+", s) if w]
    while words and words[-1] in _TRAILING:
        words.pop()
    return " ".join(words)


_DEBT_RE = re.compile(r"NOTE|BOND|DEBT|%")


def _class_letter(ticker):
    m = re.match(r"^[A-Z]+-([A-Z]{1,3})$", str(ticker or ""))
    return m.group(1) if m else None


def _override_for(overrides, ident):
    if not isinstance(overrides, dict):
        return {}
    t = ident.get("ticker")
    if t and isinstance(overrides.get(t), dict):
        return overrides[t]
    return overrides


def match_holding_detail(table, ident, overrides=None):
    """(Holding or None, note or None). First by CUSIP (the ticker's known
    CUSIPs and the private overrides), else by issuer_key of the name (and of
    override names); debt-like classes are dropped; several share classes are
    decided by the ticker's class letter (BRK-B takes "CL B"), else, for a
    company with one ticker, by a title starting with "COM"; otherwise the
    match is ambiguous and nothing is returned."""
    holdings = (table or {}).get("holdings") or {}
    ov = _override_for(overrides, ident)
    cusips = []
    for c in list(ident.get("cusips") or ()) + list(ov.get("cusips") or ()):
        c = str(c or "").strip().upper()
        if CUSIP_RE.match(c) and c not in cusips:
            cusips.append(c)
    for c in cusips:
        if c in holdings:
            return holdings[c], None
    keys = set()
    key_ciks = ident.get("key_ciks", 1)
    shared = key_ciks is None or int(key_ciks) > 1
    if shared:
        pass  # another company has the same key: only a CUSIP or an override name may match
    elif ident.get("issuer_key"):
        keys.add(ident["issuer_key"])
    elif ident.get("name"):
        keys.add(issuer_key(ident["name"]))
    for n in ov.get("names") or ():
        k = issuer_key(n)
        if k:
            keys.add(k)
    keys.discard("")
    if not keys:
        return None, (SHARED_KEY_NOTE if shared else None)
    found = [h for h in holdings.values() if issuer_key(h.get("name")) in keys]
    found = [h for h in found if not _DEBT_RE.search(str(h.get("title") or "").upper())]
    if not found:
        return None, None
    if len(found) == 1:
        return found[0], None
    letter = _class_letter(ident.get("ticker"))
    if letter:
        pat = re.compile(r"\bCL(?:ASS)?\s*" + re.escape(letter) + r"\b")
        hits = [h for h in found if pat.search(str(h.get("title") or "").upper())]
        if len(hits) == 1:
            return hits[0], None
    elif int(ident.get("classes") or 1) == 1:
        hits = [h for h in found if str(h.get("title") or "").upper().startswith("COM")]
        if len(hits) == 1:
            return hits[0], None
    return None, AMBIGUOUS_NOTE


def match_holding(table, ident, overrides=None):
    return match_holding_detail(table, ident, overrides)[0]


def fund_row(fund, cur, prev, cur_ref, prev_ref):
    """A FundRow, or None when the fund held the stock in neither period.
    cur and prev are {"shares", "value_usd"} (None or 0 shares: not held)."""
    shares = float((cur or {}).get("shares") or 0.0)
    value = float((cur or {}).get("value_usd") or 0.0)
    has_prev = prev_ref is not None
    prev_shares = float((prev or {}).get("shares") or 0.0) if has_prev else None
    prev_value = float((prev or {}).get("value_usd") or 0.0) if has_prev else None
    if shares <= 0 and not prev_shares:
        return None
    if not has_prev or prev_shares <= 0:
        change, change_shares, change_pct = "new", shares if has_prev else None, None
    elif shares <= 0:
        change, change_shares, change_pct = "exited", -prev_shares, -100.0
    else:
        diff = shares - prev_shares
        change = "added" if diff > 0 else "trimmed" if diff < 0 else "unchanged"
        change_shares, change_pct = diff, round_pct(diff / prev_shares * 100.0)
    return {
        "fund": fund.get("name"),
        "cik": _cik(fund.get("cik")),
        "period": cur_ref.get("period"),
        "previous_period": prev_ref.get("period") if has_prev else None,
        "shares": shares,
        "value_usd": value,
        "prev_shares": prev_shares,
        "prev_value_usd": prev_value,
        "change": change,
        "change_shares": change_shares,
        "change_pct": change_pct,
        "form": cur_ref.get("form"),
        "filed": cur_ref.get("filed"),
        "url": FOLDER.format(cik=_cik(cur_ref.get("cik") or fund.get("cik")),
                             accession_no_dashes=_accession(cur_ref["accession"]).replace("-", "")),
    }


# ---------------------------------------------------------------- the funds flow, one ticker at a time

def period_entry(table, ident, pick, overrides=None):
    """(entry, cusip, note) for one ticker and one picked fund period: the entry
    to keep in that ticker's caches.funds[cik]["periods"][period] (shares 0
    when not held), the CUSIP that matched (to keep in the ticker's identity),
    and a note when the match was ambiguous. The table is a shared read-only
    input; nothing of another ticker is read or written."""
    holding, note = match_holding_detail(table, ident, overrides)
    known = set(known_cusips(ident, overrides))
    if holding:
        known.add(holding["cusip"])
    entry = {"accession": pick["accession"], "form": pick["form"], "filed": pick["filed"],
             "shares": float(holding["shares"]) if holding else 0.0,
             "value_usd": float(holding["value_usd"]) if holding else 0.0,
             "cusips": sorted(known)}
    merged = [m["accession"] for m in pick.get("merge") or ()]
    if merged:
        entry["merge"] = merged
    return entry, (holding["cusip"] if holding else None), note


def entry_current(entry, pick):
    """True when a cached period entry was read from the filing(s) the pick names
    (a later restatement or NEW HOLDINGS amendment makes it stale)."""
    if not isinstance(entry, dict):
        return False
    return entry.get("accession") == pick["accession"] and \
        list(entry.get("merge") or []) == [m["accession"] for m in pick.get("merge") or ()]


def known_cusips(ident, overrides=None):
    """The CUSIPs a ticker is matched by: its identity's and its overrides'."""
    out = []
    ov = _override_for(overrides, ident or {})
    for c in list((ident or {}).get("cusips") or ()) + list(ov.get("cusips") or ()):
        c = str(c or "").strip().upper()
        if CUSIP_RE.match(c) and c not in out:
            out.append(c)
    return out


def periods_to_read(fund_periods_cache, picks, cusips=None):
    """The picks whose period one ticker's cache (caches.funds[cik]["periods"])
    lacks or holds from other filings. With cusips (the ticker's CUSIPs now),
    also an entry that found no shares while the ticker had fewer CUSIPs: the
    company may have been listed under another name in that filing, and a
    CUSIP learned since then can find it (compare by CUSIP, never by name)."""
    cache = fund_periods_cache if isinstance(fund_periods_cache, dict) else {}
    now = {str(c) for c in cusips or ()}
    out = []
    for p in picks or ():
        entry = cache.get(p["period"])
        if not entry_current(entry, p):
            out.append(p)
        elif now and not float(entry.get("shares") or 0.0) > 0 and \
                {str(c) for c in entry.get("cusips") or ()} < now:
            out.append(p)
    return out


def ticker_fund_rows(funds, fund_cache, picks_by_cik):
    """(FundRows, funds checked) for one ticker from its own cache.

    funds: [{"name", "cik"}]; fund_cache: the ticker's caches.funds
    ({cik: {"periods": {period: entry}}}); picks_by_cik: {cik: pick_periods
    result} (shared). A fund counts as checked when its latest picked periods
    are all in the cache; a fund that held the stock in either period gives a
    row."""
    rows, checked = [], 0
    for fund in funds or ():
        try:
            cik = _cik(fund.get("cik"))
        except (ValueError, AttributeError):
            continue
        picks = (picks_by_cik or {}).get(cik) or (picks_by_cik or {}).get(str(cik)) or []
        periods = ((fund_cache or {}).get(str(cik)) or (fund_cache or {}).get(cik) or {}).get("periods") or {}
        if not picks or periods_to_read(periods, picks[:2]):
            continue
        checked += 1
        cur_ref = picks[0]
        prev_ref = picks[1] if len(picks) > 1 else None
        row = fund_row({"name": fund.get("name"), "cik": cik}, periods[cur_ref["period"]],
                       periods[prev_ref["period"]] if prev_ref else None, cur_ref, prev_ref)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: (-r["value_usd"], r["fund"] or ""))
    return rows, checked
