#!/usr/bin/env python3
"""Pick the morning learning briefing's topics. Uses no secrets and no model.

    python scripts/learn/candidates.py --out <path>

Reads config.yml (the learn block, through scripts/config.py),
docs/data/items.json, the lane sources in learn/lanes.md and learn/seen.json,
and writes the candidate topics as JSON to --out for brief.py and render.py.

How topics are picked, step by step (every step is deterministic):

1. Pool. Items from items.json first seen or published in the last
   lookback_hours, and published in the last 7 days (so a newly added feed's
   backlog stays out), plus the entries of every lane source published in that
   window. Lane sources are fetched with the helpers in fetch_and_build.py
   (timeouts, size cap, one retry, a time limit per source and for the run); a
   source that fails is logged and skipped. Only https links are kept; a lane
   entry's http link is upgraded to https.

2. Terms. Each title and summary is reduced to normalized tokens (see
   learn.tokens: lower case, stopwords and URLs dropped, "GPT-5.5" also gives
   the joined token gpt5.5). Per-source boilerplate is removed first: a term in
   at least half of a source's items in items.json (3 items or more) is that
   source's boilerplate, like a podcast's "subscribe" paragraph. A term is
   distinctive when at most max(5, 8%) of the background items contain it.
   The background is every item in items.json (weeks of feeds) plus the lane
   entries, so words that come up all the time ("claude", "model") are not
   distinctive, while a new story's terms ("gpt5.5") are, however many of
   today's items cover it.

3. Clusters. Two items are linked, with a strength, when:
     same link (ignoring utm_ and other tracking parameters)            100
     near-duplicate titles: Jaccard of title terms >= 0.6                50+
     different sources, >= 2 shared distinctive title terms
     different sources, >= 4 shared distinctive terms (title or summary)
       and at least 1 of them in both titles
     same source and both titles bare version numbers (release feeds)    40
   For the shared-term rules the strength is 2 x shared title terms + shared
   terms. Links are merged strongest first (ties by item key), and two
   clusters merge only when more than half of the item pairs between them are
   linked (average linkage), so a roundup post that mentions two stories joins
   the one it shares most with instead of chaining them together.

4. Score. Only the four signals, weighted by the WEIGHTS table below. Lane
   relevance counts each distinct lane keyword found in the cluster's titles and
   summaries, plus one per lane whose own source supplied an item.

5. Novelty. A cluster that names the same story as a topic logged in
   learn/seen.json in the last 30 days (a shared link, or learn.terms_overlap
   on their terms) is skipped as seen, unless it brings a new development
   after that topic was logged: an item first seen later from a source the
   topic did not have, or a primary item with a link the topic did not have.
   A follow-up from a source the topic already had is not enough. Topics
   logged for today's date are ignored, so a re-run of the morning run picks
   the same topics again instead of the leftovers.

6. Selection. Clusters are ranked by score (then sources, then primary). All
   at or above THRESHOLD are taken up to topics_max; when fewer than topics_min
   pass, the next best fill up to topics_min. The best off-lane cluster (no lane
   keyword hits) is always included when one exists: it replaces the lowest
   pick when the list is full. An off-lane cluster is never the only topic
   while an on-lane cluster exists: the best on-lane one is added, or takes
   its place when topics_max is 1.

   A sparse day gives fewer topics than topics_min, or none: there are only
   as many topics as there are clusters that were not skipped as seen. That
   is expected on a quiet day or when the lane sources fail, and the run log
   says so.
"""
import argparse
import math
import os
import re
import sys
import time
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetch_and_build as fab  # noqa: E402  (needs scripts/ on sys.path)
import learn  # noqa: E402
from learn import log, warn  # noqa: E402

# ---------------------------------------------------------------- the score

# The only place the score is defined. score = sum of the weighted signals.
#
# | signal               | weight | counted as                                        | cap         |
# |----------------------|--------|---------------------------------------------------|-------------|
# | sources              | 2.0    | each distinct source after the first              | 3 (6.0)     |
# | primary              | 1.5    | any item from a primary source (lab, vendor,      | once (1.5)  |
# |                      |        | paper or release feed)                            |             |
# | new_since_last_week  | 1.0    | nothing first seen 7 to 30 days ago in items.json,| once (1.0)  |
# |                      |        | and no seen.json topic logged 7+ days ago, covers |             |
# |                      |        | the same story                                    |             |
# | lane_hits            | 0.4    | each distinct lane keyword hit, plus one per lane | 3 (1.2)     |
# |                      |        | whose own source supplied an item                 |             |
#
# Highest possible score: 9.7. A topic needs THRESHOLD to be picked on its own.
WEIGHTS = {
    "sources": {"weight": 2.0, "cap": 3},
    "primary": {"weight": 1.5},
    "new_since_last_week": {"weight": 1.0},
    "lane_hits": {"weight": 0.4, "cap": 3},
}
THRESHOLD = 3.0

# ---------------------------------------------------------------- limits

# Both stay below the 85 seconds the workflow gives this step (daily.yml), so a
# slow morning keeps the lane items already fetched instead of losing them all.
LANE_SOURCE_SECONDS = 15      # wall clock for one lane source
LANE_RUN_SECONDS = 60         # wall clock for all lane sources together
LANE_ENTRIES_PER_SOURCE = 15  # newest entries kept per lane source
MAX_PUBLISHED_AGE_DAYS = 7    # an item published longer ago stays out of the pool
LAST_WEEK_DAYS = (7, 30)      # "last week" for new_since_last_week, in days ago
SUMMARY_CHARS = 500           # summary kept per item in the candidates file
ITEMS_PER_TOPIC = 8           # items kept per topic in the candidates file
TERMS_PER_TOPIC = 12

TITLE_JACCARD = 0.6
DISTINCTIVE_MIN = 5           # a term in at most max(DISTINCTIVE_MIN, DISTINCTIVE_SHARE
DISTINCTIVE_SHARE = 0.08      # x background items) items is distinctive
BOILERPLATE_SHARE = 0.5
BOILERPLATE_MIN_ITEMS = 3
SHARED_HOSTS = frozenset({"github.com", "substack.com", "youtube.com", "medium.com", "x.com", "twitter.com"})
VERSION_TITLE_RE = re.compile(r"^(?:release\s+)?v?\d+(?:[.\-]\w+)*$|^b\d+$", re.IGNORECASE)


# ---------------------------------------------------------------- items

TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "si"})


def link_key(url):
    """The url without fragment, trailing slash or tracking parameters (utm_*
    and the like), host in lower case, other parameters sorted: two links to one
    page get the same key. The query string stays (a YouTube video is ?v=)."""
    try:
        p = urlparse(url)
        params = parse_qsl(p.query, keep_blank_values=True)
    except ValueError:
        return str(url)
    host = (p.hostname or "").lower().removeprefix("www.")
    kept = sorted((k, v) for k, v in params if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS)
    return urlunparse(("https", host, p.path.rstrip("/"), "", urlencode(kept), ""))


def site_host(url):
    """The host a feed's posts live on: the feed host without www., rss. or export."""
    host = (urlparse(url).hostname or "").lower()
    for prefix in ("www.", "rss.", "export.", "feeds."):
        host = host.removeprefix(prefix)
    return host


def make_item(title, url, source, published, summary, first_seen=None, primary=False, lanes=(), from_lane=False):
    # Titles are shown to the reader as they are: no em or en dashes.
    title = learn.plain_punctuation(fab.clean_text(title)) or "Untitled"
    if from_lane and sum(1 for w in re.findall(r"[A-Za-z]{2,}", title)) < 2:
        title = f"{source}: {title}"  # a release feed's bare version number
    return {
        "title": title[:300],
        "url": url,
        "key": link_key(url),
        "source": fab.clean_text(source)[:120] or "Unknown",
        "published": learn.iso(published),
        "first_seen": learn.iso(first_seen) if first_seen else None,
        "summary": learn.truncate_words(fab.clean_text(summary), SUMMARY_CHARS),
        "primary": bool(primary),
        "source_lanes": list(lanes),
        "version_title": bool(VERSION_TITLE_RE.match(title.split(": ", 1)[-1].strip())),
    }


def pool_from_items(items_doc, now, since, primary_hosts):
    """(window items, last-week items) from items.json."""
    window, older = [], []
    oldest_published = now - timedelta(days=MAX_PUBLISHED_AGE_DAYS)
    week_from, week_to = now - timedelta(days=LAST_WEEK_DAYS[1]), now - timedelta(days=LAST_WEEK_DAYS[0])
    for raw in (items_doc or {}).get("items") or []:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        if not learn.is_https(url):
            continue
        published = learn.parse_iso(raw.get("published"))
        first_seen = learn.parse_iso(raw.get("first_seen"))
        if published is None:
            continue
        published = min(published, now)
        reached = first_seen or published
        host = site_host(url)
        item = make_item(raw.get("title"), url, raw.get("source"), published, raw.get("summary"),
                         first_seen=first_seen, primary=host in primary_hosts)
        if (reached >= since or published >= since) and published >= oldest_published:
            window.append(item)
        elif week_from <= reached < week_to:
            older.append(item)
    return window, older


def primary_site_hosts(lanes):
    hosts = set()
    for lane in lanes:
        for src in lane.sources:
            host = site_host(src["url"])
            if src["primary"] and host and host not in SHARED_HOSTS:
                hosts.add(host)
    return hosts


def lane_source_list(lanes):
    """One entry per distinct feed url: label, url, primary, lanes."""
    by_url = {}
    for lane in lanes:
        for src in lane.sources:
            entry = by_url.setdefault(src["url"], {"label": src["label"], "url": src["url"], "primary": False, "lanes": []})
            entry["primary"] = entry["primary"] or src["primary"]
            if lane.name not in entry["lanes"]:
                entry["lanes"].append(lane.name)
    return list(by_url.values())


def fetch_lane_items(lanes, session, now, since, run_seconds=LANE_RUN_SECONDS):
    """The lane sources' entries published since `since`, and one status row
    per source. A source that fails is logged and skipped; the run goes on."""
    items, status = [], []
    deadline = time.monotonic() + run_seconds
    for src in lane_source_list(lanes):
        left = deadline - time.monotonic()
        if left <= 0:
            warn(f"lane source {src['label']}: skipped, out of time")
            status.append({"label": src["label"], "ok": False, "count": 0, "error": "skipped, out of time"})
            continue
        try:
            with fab.time_limit(min(LANE_SOURCE_SECONDS, left)):
                entries, error = fab.fetch_feed(session, src["label"], src["url"])
                fresh = []
                if error is None:
                    for raw in entries:
                        published = fab.entry_published(raw)
                        if published is None or published < since:
                            continue
                        url = fab.web_url(raw.get("link") or raw.get("url"))
                        if url and url.lower().startswith("http://"):
                            url = "https://" + url[7:]
                        if not learn.is_https(url):
                            continue
                        summary = fab.extract_summary(raw.get("summary") or raw.get("description") or fab.entry_body(raw), SUMMARY_CHARS)
                        fresh.append(make_item(raw.get("title"), url, src["label"], min(published, now), summary,
                                               primary=src["primary"], lanes=src["lanes"], from_lane=True))
                fresh.sort(key=lambda i: (i["published"], i["key"]), reverse=True)
                fresh = fresh[:LANE_ENTRIES_PER_SOURCE]
        except fab.FetchError as e:
            warn(f"lane source {src['label']}: {e.detail}")
            error, fresh = e.reason, []
        except Exception as e:  # one malformed feed must not stop the run
            warn(f"lane source {src['label']}: could not read entries: {type(e).__name__}: {e}")
            error, fresh = "parse error", []
        if error is not None:
            warn(f"lane source {src['label']} ({src['url']}) failed: {error}")
        items.extend(fresh)
        status.append({"label": src["label"], "ok": error is None, "count": len(fresh), "error": error})
    return items, status


def merge_duplicates(items):
    """One item per link: a lane entry and an items.json item with the same
    link become one item that keeps the lane and primary information."""
    by_key = {}
    for it in items:
        old = by_key.get(it["key"])
        if old is None:
            by_key[it["key"]] = dict(it)
            continue
        old["primary"] = old["primary"] or it["primary"]
        old["source_lanes"] = sorted(set(old["source_lanes"]) | set(it["source_lanes"]))
        if len(it["summary"]) > len(old["summary"]):
            old["summary"] = it["summary"]
    return sorted(by_key.values(), key=lambda i: i["key"])


# ---------------------------------------------------------------- terms

def boilerplate_by_source(items_doc, extra_items=()):
    """For each source, the terms in at least BOILERPLATE_SHARE of its items
    (counted over every items.json item of that source, and the lane items),
    when it has BOILERPLATE_MIN_ITEMS items or more."""
    texts = {}
    for raw in (items_doc or {}).get("items") or []:
        if isinstance(raw, dict):
            texts.setdefault(fab.clean_text(raw.get("source")), []).append(set(learn.tokens(raw.get("summary"))))
    for it in extra_items:
        texts.setdefault(it["source"], []).append(set(learn.tokens(it["summary"])))
    out = {}
    for source, sets in texts.items():
        if len(sets) < BOILERPLATE_MIN_ITEMS:
            continue
        counts = {}
        for s in sets:
            for t in s:
                counts[t] = counts.get(t, 0) + 1
        out[source] = {t for t, c in counts.items() if c / len(sets) >= BOILERPLATE_SHARE}
    return out


def add_terms(items, boilerplate):
    for it in items:
        bp = boilerplate.get(it["source"], set())
        it["title_terms"] = set(learn.tokens(it["title"]))
        it["terms"] = it["title_terms"] | (set(learn.tokens(it["summary"])) - bp)


def background_terms(items_doc, lane_items, boilerplate):
    """The term set of every background item: each items.json item and each
    lane entry, one per link."""
    sets, keys = [], set()
    raw_items = [r for r in (items_doc or {}).get("items") or [] if isinstance(r, dict)]
    for raw in raw_items:
        key = link_key(str(raw.get("url") or "")) if raw.get("url") else str(raw.get("id"))
        if key in keys:
            continue
        keys.add(key)
        bp = boilerplate.get(fab.clean_text(raw.get("source")), set())
        sets.append(set(learn.tokens(raw.get("title"))) | (set(learn.tokens(raw.get("summary"))) - bp))
    for it in lane_items:
        if it["key"] not in keys:
            keys.add(it["key"])
            sets.append(it["terms"])
    return sets


def distinctive_terms(term_sets):
    """(distinctive terms, document frequency) over the background term sets."""
    df = {}
    for terms in term_sets:
        for t in terms:
            df[t] = df.get(t, 0) + 1
    limit = max(DISTINCTIVE_MIN, math.ceil(DISTINCTIVE_SHARE * len(term_sets)))
    return {t for t, n in df.items() if n <= limit}, df


# ---------------------------------------------------------------- clusters

def link_strength(a, b, distinctive):
    """How strongly two items are the same story (0 when they are not)."""
    if a["key"] == b["key"]:
        return 100.0
    ta, tb = a["title_terms"], b["title_terms"]
    if len(ta) >= 2 and len(tb) >= 2:
        jac = len(ta & tb) / len(ta | tb)
        if jac >= TITLE_JACCARD:
            return 50.0 + jac
    if a["source"] == b["source"]:
        return 40.0 if a["version_title"] and b["version_title"] else 0.0
    shared_title = ta & tb & distinctive
    shared_all = a["terms"] & b["terms"] & distinctive
    if len(shared_title) >= 2 or (len(shared_all) >= 4 and shared_title):
        return 2.0 * len(shared_title) + len(shared_all)
    return 0.0


def cluster_items(items, distinctive):
    """Group items into stories (see step 3 in the module docstring). Returns
    lists of item indexes, each sorted, the lists ordered by their first index."""
    n = len(items)
    linked, pairs = set(), []
    for i in range(n):
        for j in range(i + 1, n):
            s = link_strength(items[i], items[j], distinctive)
            if s > 0:
                linked.add((i, j))
                pairs.append((-s, items[i]["key"], items[j]["key"], i, j))
    pairs.sort()
    parent = list(range(n))
    members = {i: [i] for i in range(n)}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, _, _, i, j in pairs:
        ri, rj = find(i), find(j)
        if ri == rj:
            continue
        a, b = members[ri], members[rj]
        cross = sum(1 for x in a for y in b if (min(x, y), max(x, y)) in linked)
        if cross * 2 > len(a) * len(b):
            root, other = min(ri, rj), max(ri, rj)
            parent[other] = root
            members[root] = sorted(a + b)
            del members[other]
    return sorted(members.values())


def cluster_terms(items, distinctive, df):
    """The topic's top terms: title terms count twice, rarer terms first."""
    weight = {}
    for it in items:
        for t in it["terms"] & distinctive:
            weight[t] = weight.get(t, 0) + (2 if t in it["title_terms"] else 1)
    ranked = sorted(weight, key=lambda t: (-weight[t], df.get(t, 0), t))
    return ranked[:TERMS_PER_TOPIC]


# ---------------------------------------------------------------- lanes

def compile_lanes(lanes):
    """Per lane, a list of (keyword, patterns) with the theme rules of
    fetch_and_build.py: whole words, optional plural s, ALL CAPS case sensitive."""
    out = []
    for lane in lanes:
        compiled = fab.compile_themes({kw: [kw] for kw in lane.keywords})
        out.append((lane.name, compiled))
    return out


def lane_hits(items, compiled_lanes):
    """{lane: [hits]}: the distinct keywords found in the items' titles and
    summaries, plus "source: <label>" when the lane's own source supplied an item."""
    text = " \n ".join(f"{it['title']} {it['summary']}" for it in items)
    hits = {}
    for lane, compiled in compiled_lanes:
        found = [kw for kw, patterns in compiled if any(p.search(text) for p in patterns)]
        if found:
            hits[lane] = found
    for it in items:
        for lane in it["source_lanes"]:
            tag = f"source: {it['source']}"
            if tag not in hits.setdefault(lane, []):
                hits[lane].append(tag)
    return hits


# ---------------------------------------------------------------- scoring

def score_signals(sources, primary, new_since_last_week, hits):
    """(score, explanation lines) from the four signals and WEIGHTS."""
    w = WEIGHTS
    extra = min(max(0, sources - 1), w["sources"]["cap"])
    counted_hits = min(hits, w["lane_hits"]["cap"])
    parts = [
        (extra * w["sources"]["weight"],
         f"{sources} source{'s' if sources != 1 else ''}: +{extra * w['sources']['weight']:.1f}"),
        (w["primary"]["weight"] if primary else 0.0,
         f"primary source: +{w['primary']['weight']:.1f}" if primary else "no primary source: +0.0"),
        (w["new_since_last_week"]["weight"] if new_since_last_week else 0.0,
         f"new since last week: +{w['new_since_last_week']['weight']:.1f}" if new_since_last_week
         else "already around last week: +0.0"),
        (counted_hits * w["lane_hits"]["weight"],
         f"{hits} lane hit{'s' if hits != 1 else ''}: +{counted_hits * w['lane_hits']['weight']:.1f}"),
    ]
    score = round(sum(p[0] for p in parts), 2)
    return score, [p[1] for p in parts]


def is_new_development(cluster, matched):
    """True when the cluster brings something the matched seen topics did not
    have: after the latest of them was logged, an item from a new source, or a
    primary item with a new link."""
    latest = max(learn.parse_iso(e["logged_at"]) for e in matched)
    known_urls = {link_key(u) for e in matched for u in e["urls"]}
    known_sources = {s for e in matched for s in e["sources"]}
    for it in cluster:
        reached = learn.parse_iso(it.get("first_seen")) or learn.parse_iso(it["published"])
        if reached is None or reached <= latest:
            continue
        if it["source"] not in known_sources or (it["primary"] and it["key"] not in known_urls):
            return True
    return False


def representative(cluster, links):
    """The item whose title names the topic: primary first, then the best
    linked, then the earliest."""
    return min(cluster, key=lambda it: (not it["primary"], -links.get(it["key"], 0), it["published"], it["key"]))


def build_topics(items, older, seen_entries, compiled_lanes, now, background, today=None):
    """Every cluster as a scored topic dict, and the topics skipped as seen.
    items and older need their terms (add_terms) first; background is the list
    of background term sets (background_terms). today is the briefing's date
    (YYYY-MM-DD): seen entries logged for it come from an earlier run of the
    same day and are ignored."""
    distinctive, df = distinctive_terms(background)
    clusters = cluster_items(items, distinctive)
    week_ago = now - timedelta(days=LAST_WEEK_DAYS[0])
    gate_from = now - timedelta(days=learn.SEEN_GATE_DAYS)
    if today:
        seen_entries = learn.seen_before(seen_entries, today)
    recent_seen = [e for e in seen_entries if learn.parse_iso(e["logged_at"]) >= gate_from]
    old_seen = [e for e in recent_seen if learn.parse_iso(e["logged_at"]) <= week_ago]

    topics, skipped, used_ids = [], [], set()
    for idx in clusters:
        cluster = [items[i] for i in idx]
        links = {}
        for a in cluster:
            links[a["key"]] = sum(1 for b in cluster if b is not a and link_strength(a, b, distinctive) > 0)
        rep = representative(cluster, links)
        terms = cluster_terms(cluster, distinctive, df)
        keys = {it["key"] for it in cluster}
        urls = [it["url"] for it in cluster]

        matched = learn.seen_matches(recent_seen, terms, urls)
        matched += [e for e in recent_seen if e not in matched and keys & {link_key(u) for u in e["urls"]}]
        follow_up = None
        if matched:
            if not is_new_development(cluster, matched):
                last = max(matched, key=lambda e: e["logged_at"])
                skipped.append({"title": rep["title"], "seen_id": last["id"], "seen_date": last["date"]})
                continue
            last = max(matched, key=lambda e: e["logged_at"])
            follow_up = {"id": last["id"], "date": last["date"]}

        # new_since_last_week: no seen topic logged a week or more ago, and no
        # items.json item first seen 7 to 30 days ago, covers this story.
        around_last_week = any(e in matched for e in old_seen) or any(
            it["key"] in keys or learn.terms_overlap(terms, it["terms"]) for it in older
        )
        new = not around_last_week

        sources = sorted({it["source"] for it in cluster})
        primary = any(it["primary"] for it in cluster)
        hits = lane_hits(cluster, compiled_lanes)
        hit_count = sum(len(v) for v in hits.values())
        keyword_hits = sum(1 for v in hits.values() for h in v if not h.startswith("source: "))
        lane_order = {name: n for n, (name, _) in enumerate(compiled_lanes)}
        lanes = sorted(hits, key=lambda name: (-len(hits[name]), lane_order.get(name, 99)))[:3]
        score, explanation = score_signals(len(sources), primary, new, hit_count)
        if hits:
            explanation.append("lanes: " + "; ".join(f"{name} ({', '.join(hits[name])})" for name in lanes))
        if follow_up:
            explanation.append(f"follow-up to the topic of {follow_up['date']} with a new development")

        tid = learn.slugify(rep["title"])
        base, n = tid, 1
        while tid in used_ids:
            n += 1
            tid = f"{base}-{n}"
        used_ids.add(tid)

        # The representative first, then primary items, then the newest.
        ordered = sorted(cluster, key=lambda it: it["key"])
        ordered.sort(key=lambda it: it["published"], reverse=True)
        ordered.sort(key=lambda it: (it is not rep, not it["primary"]))
        ordered = ordered[:ITEMS_PER_TOPIC]
        topics.append({
            "id": tid,
            "title": learn.truncate_words(rep["title"], 140),
            "lanes": lanes,
            "off_lane": keyword_hits == 0 and not any(it["source_lanes"] for it in cluster),
            "score": score,
            "signals": {
                "sources": len(sources),
                "primary": primary,
                "new_since_last_week": new,
                "lane_hits": hit_count,
                "score": score,
            },
            "explanation": explanation,
            "follow_up": follow_up,
            "terms": terms,
            "sources": sources,
            "urls": urls[:20],
            "items": [{
                "title": it["title"],
                "url": it["url"],
                "source": it["source"],
                "published": it["published"],
                "summary": it["summary"],
                "primary": it["primary"],
            } for it in ordered],
        })
    return topics, skipped


def select_topics(topics, topics_min, topics_max, threshold=THRESHOLD):
    """The topics to brief, best first (see step 6 in the module docstring)."""
    ranked = sorted(topics, key=lambda t: (-t["score"], -t["signals"]["sources"], not t["signals"]["primary"], t["id"]))
    picked = [t for t in ranked if t["score"] >= threshold][:topics_max]
    for t in ranked:
        if len(picked) >= topics_min:
            break
        if t not in picked:
            picked.append(t)
    off = [t for t in ranked if t["off_lane"]]
    if off and not any(t["off_lane"] for t in picked):
        if len(picked) >= topics_max:
            picked = picked[: topics_max - 1]
        picked.append(off[0])
    # An off-lane topic widens the view; it is never the whole briefing while
    # an on-lane cluster exists.
    on = [t for t in ranked if not t["off_lane"]]
    if on and picked and all(t["off_lane"] for t in picked):
        if len(picked) >= topics_max:
            picked = picked[: topics_max - 1]
        picked.append(on[0])
    picked.sort(key=lambda t: ranked.index(t))
    return picked


# ---------------------------------------------------------------- main

def build_candidates(settings, items_doc, lanes, seen_entries, now, lane_items=(), lane_status=()):
    learn_cfg = settings["learn"]
    since = now - timedelta(hours=learn_cfg["lookback_hours"])
    primary_hosts = primary_site_hosts(lanes)
    window, older = pool_from_items(items_doc, now, since, primary_hosts)
    pool = merge_duplicates(window + list(lane_items))
    boilerplate = boilerplate_by_source(items_doc, lane_items)
    add_terms(pool, boilerplate)
    add_terms(older, boilerplate)
    add_terms(lane_items, boilerplate)
    background = background_terms(items_doc, lane_items, boilerplate)
    compiled = compile_lanes(lanes)
    today = learn.local_date(now, settings["timezone"])
    topics, skipped = build_topics(pool, older, seen_entries, compiled, now, background, today=today)
    picked = select_topics(topics, learn_cfg["topics_min"], learn_cfg["topics_max"])
    failed = [s for s in lane_status if not s["ok"]]
    return {
        "version": 1,
        "date": today,
        "generated_at": learn.iso(now),
        "timezone": settings["timezone"],
        "window": {"since": learn.iso(since), "until": learn.iso(now), "lookback_hours": learn_cfg["lookback_hours"]},
        "lanes": [{"name": lane.name, "watch": lane.watch} for lane in lanes],
        "weights": WEIGHTS,
        "threshold": THRESHOLD,
        "stats": {
            "items_in_window": len(window),
            "lane_items": len(lane_items),
            "pool": len(pool),
            "clusters": len(topics) + len(skipped),
            "skipped_as_seen": len(skipped),
            "topics_min": learn_cfg["topics_min"],
            "below_topics_min": len(picked) < learn_cfg["topics_min"],
            "lane_sources_ok": sum(1 for s in lane_status if s["ok"]),
            "lane_sources_failed": [{"label": s["label"], "error": s["error"]} for s in failed],
        },
        "topics": picked,
        "seen_skipped": skipped[:50],
    }


def main(argv=None, session=None, now=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="where to write the candidates JSON")
    parser.add_argument("--items", default=learn.ITEMS_JSON)
    parser.add_argument("--lanes", default=learn.LANES_MD)
    parser.add_argument("--seen", default=learn.SEEN_JSON)
    parser.add_argument("--config", default=None, help="config.yml (default: the repository's)")
    parser.add_argument("--no-fetch", action="store_true", help="skip the lane sources (offline)")
    parser.add_argument("--now", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    now = now or learn.parse_iso(args.now) or learn.utc_now()
    settings = learn.load_settings(args.config)
    items_doc = learn.read_json(args.items)
    if not isinstance(items_doc, dict):
        warn(f"{args.items} is missing or not a JSON object; using lane sources only")
        items_doc = {}
    lanes = learn.load_lanes(args.lanes)
    seen_entries = learn.load_seen(args.seen)

    lane_items, lane_status = [], []
    if not args.no_fetch and lanes:
        since = now - timedelta(hours=settings["learn"]["lookback_hours"])
        lane_items, lane_status = fetch_lane_items(lanes, session or fab.new_session(), now, since)

    doc = build_candidates(settings, items_doc, lanes, seen_entries, now, lane_items, lane_status)
    learn.write_json(args.out, doc)
    st = doc["stats"]
    log(f"Learning candidates for {doc['date']}: {st['pool']} items ({st['lane_items']} from lane sources, "
        f"{st['lane_sources_ok']} of {len(lane_status)} lane sources ok), {st['clusters']} clusters, "
        f"{st['skipped_as_seen']} skipped as seen")
    for t in doc["topics"]:
        flag = " (off lane)" if t["off_lane"] else ""
        log(f"  {t['score']:.1f}  {t['title']}{flag}  [{'; '.join(t['explanation'][:4])}]")
    if not doc["topics"]:
        warn("no candidate topics today")
    if st["below_topics_min"]:
        failed = len(st["lane_sources_failed"])
        log(f"Sparse day: {len(doc['topics'])} topic{'s' if len(doc['topics']) != 1 else ''}, fewer than topics_min "
            f"({st['topics_min']}). Clusters not skipped as seen: {st['clusters'] - st['skipped_as_seen']} of {st['clusters']}" + (f", and {failed} lane source{'s' if failed != 1 else ''} failed" if failed else "")
            + ". The briefing is written with what there is.")
    log(f"Wrote {args.out}")
    return doc


if __name__ == "__main__":
    main()
