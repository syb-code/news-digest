#!/usr/bin/env python3
"""Turn briefing.json into the day's slide deck and files. Uses no secrets.

    python scripts/learn/render.py --briefing <path> [--candidates <path>]

Writes, for the briefing's date (YYYY-MM-DD, America/Los_Angeles):
  docs/learn/YYYY-MM-DD/briefing.json   the briefing, checked again here
  docs/learn/YYYY-MM-DD/briefing.pptx   the slide deck: a title slide, three
                                        slides per topic (what happened; why it
                                        matters; read first and the open
                                        question) and a closing slide
  docs/learn/index.json                 {"latest": date, "days": [...]}, newest first
  learn/seen.json                       the day's topics, so a topic is not
                                        repeated later without a new development
It writes no HTML: the commit job copies docs/learn/viewer.html into the dated
folder as index.html. When GITHUB_OUTPUT is set, it also records date=YYYY-MM-DD
there for the next workflow step.

briefing.json comes from another job, so it is checked before use: its date
must be a real date, every link https, every text a capped string.
"""
import argparse
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import learn  # noqa: E402
from learn import log, warn  # noqa: E402

from pptx import Presentation  # noqa: E402
from pptx.dml.color import RGBColor  # noqa: E402
from pptx.enum.shapes import MSO_SHAPE  # noqa: E402
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE  # noqa: E402
from pptx.opc.constants import RELATIONSHIP_TYPE as RT  # noqa: E402
from pptx.util import Inches, Pt  # noqa: E402

# ---------------------------------------------------------------- style

# Fonts are named, not embedded: PowerPoint, Keynote and Google Slides use DM
# Serif Display and Outfit when they are installed (both are free Google
# Fonts) and fall back to a default serif and sans serif when they are not.
HEADING_FONT = "DM Serif Display"
BODY_FONT = "Outfit"

# The dark palette of the dashboard (docs/assets/css/style.css) and of the
# slideshow (docs/assets/learn/slides.css).
BACKGROUND = RGBColor(0x07, 0x07, 0x08)  # paper, near black
PANEL = RGBColor(0x11, 0x11, 0x13)       # cards
ACCENT = RGBColor(0x7E, 0xCE, 0xCC)      # cyan: accents, headings, links
TEXT = RGBColor(0xED, 0xED, 0xEB)        # body text
MUTED = RGBColor(0x9D, 0x9D, 0x9D)       # secondary text

SLIDE_W, SLIDE_H = Inches(13.333), Inches(7.5)
MARGIN = Inches(0.7)

MAX_TOPICS = 8
MAX_INDEX_DAYS = 400
FIELD_CHARS = {"title": 160, "what_happened": 3600, "why_it_matters": 3000, "open_question": 600,
               "why": 500, "email_summary": 1500, "source": 120, "read_title": 300, "lane": 60}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# ---------------------------------------------------------------- briefing checks

def _text(value, limit, multiline=False):
    s = _CONTROL_RE.sub("", str(value if isinstance(value, (str, int, float)) else "")).replace("\r", "")
    if not multiline:
        s = learn.one_line(s)
    else:
        s = "\n\n".join(learn.one_line(p) for p in re.split(r"\n\s*\n", s) if p.strip())
    return learn.truncate_words(s, limit)


def _int(value, default, low, high):
    if isinstance(value, bool):
        return default
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _float(value, default=0.0):
    if isinstance(value, bool):
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if v == v and 0 <= v < 1e6 else default


def clean_briefing(doc):
    """The briefing in contract shape with every value checked, or raises
    ValueError when it has no valid date."""
    if not isinstance(doc, dict):
        raise ValueError("briefing.json is not a JSON object")
    day = learn.valid_date(doc.get("date"))
    if not day:
        raise ValueError(f"briefing.json has no valid date: {doc.get('date')!r}")
    usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
    topics = []
    ids = set()
    for t in (doc.get("topics") if isinstance(doc.get("topics"), list) else [])[:MAX_TOPICS]:
        if not isinstance(t, dict):
            continue
        title = learn.plain_punctuation(_text(t.get("title"), FIELD_CHARS["title"]))
        if not title:
            continue
        tid = learn.slugify(t.get("id") or title)
        while tid in ids:
            tid += "-x"
        ids.add(tid)
        read_first = []
        for r in t.get("read_first") if isinstance(t.get("read_first"), list) else []:
            if isinstance(r, dict) and learn.is_https(r.get("url")):
                read_first.append({
                    "title": learn.plain_punctuation(_text(r.get("title"), FIELD_CHARS["read_title"])) or "Read",
                    "url": str(r["url"]).strip(),
                    "source": _text(r.get("source"), FIELD_CHARS["source"]),
                    "why": _text(r.get("why"), FIELD_CHARS["why"]),
                })
        sig = t.get("signals") if isinstance(t.get("signals"), dict) else {}
        topics.append({
            "id": tid,
            "title": title,
            "lanes": [_text(x, FIELD_CHARS["lane"]) for x in (t.get("lanes") or []) if isinstance(x, str)][:3],
            "off_lane": bool(t.get("off_lane")),
            "what_happened": _text(t.get("what_happened"), FIELD_CHARS["what_happened"], multiline=True),
            "why_it_matters": _text(t.get("why_it_matters"), FIELD_CHARS["why_it_matters"], multiline=True),
            "read_first": read_first[:5],
            "open_question": _text(t.get("open_question"), FIELD_CHARS["open_question"]),
            "reading_minutes": _int(t.get("reading_minutes"), 3, 1, 30),
            "signals": {
                "sources": _int(sig.get("sources"), 1, 0, 100),
                "primary": bool(sig.get("primary")),
                "new_since_last_week": bool(sig.get("new_since_last_week")),
                "lane_hits": _int(sig.get("lane_hits"), 0, 0, 100),
                "score": round(_float(sig.get("score")), 2),
            },
        })
    clean_usage = {
        "input_tokens": _int(usage.get("input_tokens"), 0, 0, 10**9),
        "output_tokens": _int(usage.get("output_tokens"), 0, 0, 10**9),
        "cost_usd": round(_float(usage.get("cost_usd")), 6),
    }
    if usage.get("cost_estimated") is True:  # a model with no entry in learn.prices wrote part of it
        clean_usage["cost_estimated"] = True
    return {
        "date": day,
        "generated_at": learn.iso(learn.parse_iso(doc.get("generated_at")) or learn.utc_now()),
        "model": _text(doc.get("model"), 80) or "unknown",
        "llm": doc.get("llm") is True,
        "usage": clean_usage,
        "email_summary": _text(doc.get("email_summary"), FIELD_CHARS["email_summary"]),
        "topics": topics,
    }


# ---------------------------------------------------------------- deck helpers

def long_date(day):
    d = date.fromisoformat(day)
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}"


def set_background(slide):
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = BACKGROUND


def add_bar(slide, left, top, width, height, color=ACCENT):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def add_text(slide, left, top, width, height, paragraphs, anchor=MSO_ANCHOR.TOP, shrink=False):
    """A text box. paragraphs is a list of lists of runs; a run is a dict with
    text, font, size, color and optional bold, italic and url."""
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Inches(0.05)
    tf.margin_top = tf.margin_bottom = Inches(0.03)
    if shrink:
        tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE  # PowerPoint shrinks text that overflows
    for n, runs in enumerate(paragraphs):
        p = tf.paragraphs[0] if n == 0 else tf.add_paragraph()
        space = runs[0].get("space_after", 6) if runs else 6
        p.space_after = Pt(space)
        if runs and runs[0].get("line_spacing"):
            p.line_spacing = runs[0]["line_spacing"]  # a share of the font's own line height
        for spec in runs:
            run = p.add_run()
            run.text = spec["text"]
            font = run.font
            font.name = spec.get("font", BODY_FONT)
            font.size = Pt(spec.get("size", 16))
            font.bold = spec.get("bold", False)
            font.italic = spec.get("italic", False)
            font.color.rgb = spec.get("color", TEXT)
            if spec.get("url"):
                run.hyperlink.address = spec["url"]
    return box


def heading_run(text, size, color=TEXT):
    return {"text": text, "font": HEADING_FONT, "size": size, "color": color}


def body_run(text, size=16, color=TEXT, **extra):
    return {"text": text, "font": BODY_FONT, "size": size, "color": color, **extra}


# Body text is sized by an estimate of how much fits its box. Each topic gets
# three slides so that a topic at the top of the length the model is asked for
# (about 280 words of what happened, 250 of why it matters) still reads at
# BODY_TARGET_MIN or larger. Longer text than that steps down the list, and
# PowerPoint and LibreOffice also shrink a box that overflows (TEXT_TO_FIT_SHAPE).
BODY_SIZES = (18, 17, 16, 15, 14, 13, 12, 11, 10)
BODY_TARGET_MIN = 16
# Outfit averages 0.44 em per character of English prose and sets lines 1.26 em
# apart (both measured). 0.47 leaves room for ragged line ends. Prose is set
# at BODY_LINE_SPACING of the font's line height, which gives LINE_HEIGHT.
CHAR_WIDTH_EM = 0.47
BODY_LINE_SPACING = 0.95
LINE_HEIGHT = 1.2
PARAGRAPH_GAP_PT = 8
HEADING_LINE_HEIGHT = 1.38  # DM Serif Display's own line height, in ems (measured)
_SENTENCE_GAP_RE = re.compile(r"(?<=[.!?])\s+")


def text_height(text, size, width, line_height=LINE_HEIGHT):
    """The estimated height, in points, of text set at size in a box width EMU wide."""
    per_line = max(1, int((width / 12700 - 8) / (size * CHAR_WIDTH_EM)))
    paragraphs = [p for p in text.split("\n\n") if p] or [""]
    lines = sum(max(1, -(-len(p) // per_line)) for p in paragraphs)
    return lines * size * line_height + PARAGRAPH_GAP_PT * (len(paragraphs) - 1)


def body_size(texts, width, height, sizes=BODY_SIZES, line_height=LINE_HEIGHT):
    """The largest size in sizes at which every text in texts, estimated line
    by line, fits its own box of width x height (EMU); the smallest when none
    does."""
    if isinstance(texts, str):
        texts = [texts]
    for size in sizes:
        if all(text_height(t, size, width, line_height) <= height / 12700 - 6 for t in texts):
            return size
    return sizes[-1]


def split_columns(text):
    """text as two halves of about the same length, for two columns side by
    side. The break falls between paragraphs when that leaves the halves
    within a fifth of each other, else between sentences; a paragraph that
    is broken carries on at the top of the second column."""
    units = []  # (sentence, starts a paragraph)
    for paragraph in (p for p in text.split("\n\n") if p):
        for n, sentence in enumerate(x for x in _SENTENCE_GAP_RE.split(paragraph) if x):
            units.append((sentence, n == 0))
    if len(units) < 2:
        return text, ""
    total = sum(len(u[0]) for u in units)
    best, before = None, 0
    for i in range(1, len(units)):
        before += len(units[i - 1][0])
        off_balance = abs(2 * before - total)
        if units[i][1] and off_balance <= total * 0.2:
            off_balance -= total  # a paragraph break that is balanced enough always wins
        if best is None or off_balance < best[0]:
            best = (off_balance, i)

    def join(part):
        out = ""
        for sentence, starts in part:
            out += ("\n\n" if starts and out else " " if out else "") + sentence
        return out

    return join(units[:best[1]]), join(units[best[1]:])


def title_size(title, largest):
    """A heading size that keeps a long title on two lines."""
    n = len(title)
    if n <= 45:
        return largest
    if n <= 75:
        return largest - 4
    return largest - 8


def style_theme(prs):
    """Accent links and the deck's fonts in the theme, so links read on the dark
    background and any default text uses DM Serif Display and Outfit."""
    try:
        part = prs.slide_master.part.part_related_by(RT.THEME)
        xml = part.blob.decode("utf-8")
    except (KeyError, AttributeError, UnicodeDecodeError):
        return
    accent = "%02X%02X%02X" % (ACCENT[0], ACCENT[1], ACCENT[2])
    xml = re.sub(r"<a:hlink>.*?</a:hlink>", f'<a:hlink><a:srgbClr val="{accent}"/></a:hlink>', xml, count=1, flags=re.S)
    xml = re.sub(r"<a:folHlink>.*?</a:folHlink>", f'<a:folHlink><a:srgbClr val="{accent}"/></a:folHlink>', xml, count=1, flags=re.S)
    xml = re.sub(r'(<a:majorFont>\s*<a:latin typeface=")[^"]*"', rf'\g<1>{HEADING_FONT}"', xml, count=1)
    xml = re.sub(r'(<a:minorFont>\s*<a:latin typeface=")[^"]*"', rf'\g<1>{BODY_FONT}"', xml, count=1)
    part._blob = xml.encode("utf-8")


def signals_line(t):
    s = t["signals"]
    parts = [f"{s['sources']} source{'s' if s['sources'] != 1 else ''}"]
    if s["primary"]:
        parts.append("primary source")
    if s["new_since_last_week"]:
        parts.append("new since last week")
    parts.append(f"{s['lane_hits']} lane hit{'s' if s['lane_hits'] != 1 else ''}")
    parts.append(f"about {t['reading_minutes']} minute{'s' if t['reading_minutes'] != 1 else ''}")
    return "  |  ".join(parts)


def lane_label(t):
    if t["off_lane"] or not t["lanes"]:
        return "Outside your lanes"
    return ", ".join(t["lanes"])


def paragraphs_of(text, size, color=TEXT):
    return [[body_run(p, size, color, space_after=PARAGRAPH_GAP_PT, line_spacing=BODY_LINE_SPACING)]
            for p in text.split("\n\n") if p] or [[body_run("", size)]]


def notes(slide, text):
    slide.notes_slide.notes_text_frame.text = _CONTROL_RE.sub("", text)


# ---------------------------------------------------------------- slides

def title_slide(prs, b):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_background(s)
    add_bar(s, MARGIN, Inches(0.9), Inches(1.2), Inches(0.08))
    add_text(s, MARGIN, Inches(1.1), Inches(11.5), Inches(0.5), [[body_run("Learning briefing", 18, ACCENT)]])
    add_text(s, MARGIN, Inches(1.6), Inches(11.9), Inches(1.2), [[heading_run(long_date(b["date"]), 44)]])
    topics = b["topics"]
    if topics:
        rows = []
        for n, t in enumerate(topics, start=1):
            rows.append([heading_run(f"{n}  ", 22, ACCENT), body_run(t["title"], 20),
                         body_run(f"   {lane_label(t)}", 13, MUTED)])
        add_text(s, MARGIN, Inches(3.0), Inches(11.9), Inches(3.4), rows, shrink=True)
        minutes = sum(t["reading_minutes"] for t in topics)
        foot = f"{len(topics)} topic{'s' if len(topics) != 1 else ''}, about {minutes} minutes of reading"
    else:
        add_text(s, MARGIN, Inches(3.0), Inches(11.9), Inches(1.0), [[body_run("No topics today.", 22)]])
        foot = "Nothing new passed the bar today."
    add_text(s, MARGIN, Inches(6.6), Inches(11.9), Inches(0.5), [[body_run(foot, 14, MUTED)]])
    notes(s, b["email_summary"])
    return s


TOPIC_BODY_TOP = Inches(1.85)
TOPIC_BODY_BOTTOM = Inches(7.2)
COLUMN_GAP = Inches(0.4)


def topic_header(prs, t, n, total, section):
    """A new topic slide with the header every slide of a topic shares: the
    topic number and lane, the topic's title, and the section's heading."""
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_background(s)
    width = SLIDE_W - 2 * MARGIN
    add_text(s, MARGIN, Inches(0.35), width, Inches(0.33),
             [[body_run(f"Topic {n} of {total}  |  {lane_label(t)}", 14, ACCENT)]])
    add_text(s, MARGIN, Inches(0.66), width, Inches(0.7), [[heading_run(t["title"], title_size(t["title"], 26))]],
             anchor=MSO_ANCHOR.MIDDLE, shrink=True)
    add_text(s, MARGIN, Inches(1.4), width, Inches(0.42), [[heading_run(section, 20, ACCENT)]])
    return s


def prose_slide(prs, t, n, total, section, text):
    """One section of a topic's prose on its own slide, in two columns."""
    s = topic_header(prs, t, n, total, section)
    col_w = (SLIDE_W - 2 * MARGIN - COLUMN_GAP) // 2
    height = TOPIC_BODY_BOTTOM - TOPIC_BODY_TOP
    columns = split_columns(text)
    size = body_size([c for c in columns if c], col_w, height)  # one size for both columns
    for col, part in enumerate(columns):
        if part:
            add_text(s, MARGIN + col * (col_w + COLUMN_GAP), TOPIC_BODY_TOP, col_w, height,
                     paragraphs_of(part, size), shrink=True)
    notes(s, f"{t['title']}\n\n{section}\n{text}")
    return s


def read_first_size(read_first, width, height):
    """The size for the read-first notes (titles are set 2 points larger)."""
    for size in BODY_SIZES[BODY_SIZES.index(BODY_TARGET_MIN):]:
        needed = sum(text_height(r["title"], size + 2, width) + 2 + text_height(read_meta(r), size, width) + 12
                     for r in read_first)
        if needed <= height / 12700 - 6:
            return size
    return BODY_SIZES[-1]


def read_meta(r):
    return r["source"] + (f". {r['why']}" if r["why"] else "")


def topic_slides(prs, t, n, total):
    """A topic's three slides: what happened; why it matters; what to read
    first with the open question."""
    s1 = prose_slide(prs, t, n, total, "What happened", t["what_happened"])
    s2 = prose_slide(prs, t, n, total, "Why it matters", t["why_it_matters"])

    s3 = topic_header(prs, t, n, total, "Read first")
    list_w, list_h = Inches(6.7), Inches(6.65) - TOPIC_BODY_TOP
    size = read_first_size(t["read_first"], list_w, list_h)
    rows = []
    for r in t["read_first"]:
        rows.append([body_run(r["title"], size + 2, ACCENT, url=r["url"], space_after=2, line_spacing=BODY_LINE_SPACING)])
        rows.append([body_run(read_meta(r), size, MUTED, space_after=12, line_spacing=BODY_LINE_SPACING)])
    if not rows:
        rows = [[body_run("No links for this topic.", BODY_TARGET_MIN, MUTED)]]
    add_text(s3, MARGIN, TOPIC_BODY_TOP, list_w, list_h, rows, shrink=True)

    card_left, card_top = Inches(7.85), TOPIC_BODY_TOP + Inches(0.1)
    card_w, card_h = SLIDE_W - card_left - MARGIN, Inches(4.5)
    add_bar(s3, card_left, card_top, card_w, card_h, PANEL)
    add_bar(s3, card_left, card_top, Inches(0.08), card_h)
    add_text(s3, card_left + Inches(0.35), card_top + Inches(0.2), card_w - Inches(0.6), Inches(0.5),
             [[heading_run("Open question", 20, ACCENT)]])
    question_w, question_h = card_w - Inches(0.6), card_h - Inches(1.0)
    question_size = body_size(t["open_question"], question_w, question_h, (22, 20, 18, 16, 14, 12),
                              line_height=HEADING_LINE_HEIGHT)
    add_text(s3, card_left + Inches(0.35), card_top + Inches(0.8), question_w, question_h,
             [[heading_run(t["open_question"], question_size)]], shrink=True)
    add_text(s3, MARGIN, Inches(6.75), SLIDE_W - 2 * MARGIN, Inches(0.4), [[body_run(signals_line(t), 12, MUTED)]])
    links = "\n".join(f"{r['title']} ({r['source']}): {r['url']}" for r in t["read_first"])
    notes(s3, f"{t['title']}\n\nRead first\n{links}\n\nOpen question\n{t['open_question']}")
    return s1, s2, s3


def closing_slide(prs, b):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    set_background(s)
    add_bar(s, MARGIN, Inches(2.2), Inches(1.2), Inches(0.08))
    add_text(s, MARGIN, Inches(2.45), Inches(11.9), Inches(1.0), [[heading_run("That's the briefing", 40)]])
    how = ("Topics are ranked by importance: how many sources covered them, primary sources, what changed "
           "since last week and fit with your lanes.")
    if any(t["off_lane"] for t in b["topics"]):
        how += " One topic comes from outside your lanes."
    if b["llm"]:
        about = "about " if b["usage"].get("cost_estimated") else ""
        made = f"Written by {b['model']} from the day's feeds. Cost of this run: {about}${b['usage']['cost_usd']:.2f}."
    else:
        made = "Built from the feeds without a model today."
    add_text(s, MARGIN, Inches(3.6), Inches(11.0), Inches(2.0),
             [[body_run(how, 18, space_after=12)], [body_run(made, 16, MUTED)]])
    add_text(s, MARGIN, Inches(6.6), Inches(11.9), Inches(0.5), [[body_run(long_date(b["date"]), 14, ACCENT)]])
    return s


def build_deck(b, path):
    prs = Presentation()
    prs.slide_width, prs.slide_height = SLIDE_W, SLIDE_H
    style_theme(prs)
    prs.core_properties.title = f"Learning briefing, {b['date']}"
    prs.core_properties.author = "news digest"
    title_slide(prs, b)
    for n, t in enumerate(b["topics"], start=1):
        topic_slides(prs, t, n, len(b["topics"]))
    closing_slide(prs, b)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    prs.save(tmp)
    os.replace(tmp, path)
    return prs


# ---------------------------------------------------------------- index and seen

def updated_index(doc, day, titles):
    """docs/learn/index.json with the day's entry added or replaced, newest first."""
    days = []
    for d in (doc.get("days") if isinstance(doc, dict) and isinstance(doc.get("days"), list) else []):
        if isinstance(d, dict) and learn.valid_date(d.get("date")) and d["date"] != day:
            days.append({"date": d["date"], "topics": [_text(x, FIELD_CHARS["title"]) for x in (d.get("topics") or [])
                                                        if isinstance(x, str)][:MAX_TOPICS]})
    days.append({"date": day, "topics": list(titles)})
    days.sort(key=lambda d: d["date"], reverse=True)
    days = days[:MAX_INDEX_DAYS]
    return {"latest": days[0]["date"] if days else None, "days": days}


def seen_topics(b, candidates):
    """What learn/seen.json logs for each briefing topic: its terms, links and
    sources from the candidates file, or from the briefing alone without it."""
    by_id = {}
    raw = candidates.get("topics") if isinstance(candidates, dict) else None
    for c in raw if isinstance(raw, list) else []:
        if isinstance(c, dict) and c.get("id"):
            by_id[learn.slugify(c["id"])] = c
    out = []
    for t in b["topics"]:
        c = by_id.get(t["id"])
        if c:
            items = [i for i in c.get("items") or [] if isinstance(i, dict)]
            terms = [str(x) for x in c.get("terms") or [] if isinstance(x, str)]
            urls = [u for u in (c.get("urls") or [i.get("url") for i in items]) if learn.is_https(u)]
            sources = [learn.one_line(s) for s in (c.get("sources") or [i.get("source") for i in items]) if isinstance(s, str)]
            primary = bool((c.get("signals") or {}).get("primary"))
        else:
            terms = list(dict.fromkeys(learn.tokens(t["title"])))
            urls = [r["url"] for r in t["read_first"]]
            sources = list(dict.fromkeys(r["source"] for r in t["read_first"] if r["source"]))
            primary = t["signals"]["primary"]
        out.append({"id": t["id"], "title": t["title"], "terms": terms or list(dict.fromkeys(learn.tokens(t["title"]))),
                    "urls": urls, "sources": sources, "primary": primary})
    return out


# ---------------------------------------------------------------- main

def main(argv=None, now=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--briefing", required=True, help="briefing.json from brief.py")
    parser.add_argument("--candidates", default=None, help="the candidates JSON (for the seen.json terms)")
    parser.add_argument("--docs", default=learn.DOCS_DIR, help="the site folder (default: docs/)")
    parser.add_argument("--seen", default=learn.SEEN_JSON)
    args = parser.parse_args(argv)
    now = now or learn.utc_now()

    try:
        b = clean_briefing(learn.read_json(args.briefing))
    except ValueError as e:
        warn(str(e))
        return 1
    candidates = learn.read_json(args.candidates) if args.candidates else None

    day_dir = os.path.join(args.docs, "learn", b["date"])
    learn.write_json(os.path.join(day_dir, "briefing.json"), b)
    deck_path = os.path.join(day_dir, "briefing.pptx")
    prs = build_deck(b, deck_path)
    log(f"Wrote {deck_path} ({len(prs.slides)} slides)")

    index_path = os.path.join(args.docs, "learn", "index.json")
    index = updated_index(learn.read_json(index_path), b["date"], [t["title"] for t in b["topics"]])
    learn.write_json(index_path, index)
    log(f"Wrote {index_path} (latest {index['latest']}, {len(index['days'])} days)")

    entries = learn.updated_seen(learn.load_seen(args.seen), seen_topics(b, candidates), b["date"], now)
    learn.write_json(args.seen, {"version": 1, "topics": entries})
    log(f"Wrote {args.seen} ({len(entries)} topics logged)")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"date={b['date']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
