"""Offline checks of the published pages themselves: docs/index.html, the
slideshow template docs/learn/viewer.html and the scripts and styles they load.

The deploy job and the commit job count on a few exact strings in these files
(the Content-Security-Policy, the empty worker address, the viewer's relative
paths), and the slideshow counts on rules that are easy to undo in a later
edit: https links only, three slides a topic, text that steps down in size
until it fits, a Back button that keeps the slide and the address in sync and
keyboard focus that survives a button turning off. The first half reads the
files; the second half runs docs/assets/learn/slides.js in node against a
small stand-in for the page."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser

import support

DOCS = os.path.join(support.ROOT, "docs")
INDEX = os.path.join(DOCS, "index.html")
VIEWER = os.path.join(DOCS, "learn", "viewer.html")
CONFIG_JS = os.path.join(DOCS, "assets", "js", "config.js")
APP_JS = os.path.join(DOCS, "assets", "js", "app.js")
SLIDES_JS = os.path.join(DOCS, "assets", "learn", "slides.js")
STOCKS_JS = os.path.join(DOCS, "assets", "js", "stocks.js")
STYLE_CSS = os.path.join(DOCS, "assets", "css", "style.css")
CONFIG_YML = os.path.join(support.ROOT, "config.yml")
SLIDES_CSS = os.path.join(DOCS, "assets", "learn", "slides.css")
FONTS = os.path.join(DOCS, "assets", "learn", "fonts")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'")
FOOTER = "Feeds refresh twice a day. The morning email and briefing follow."
# What a line of JavaScript can use to turn a string into markup or code.
HTML_SINKS = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|"
                        r"\.style\b|setAttribute\(\s*['\"]style|cssText|srcdoc")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def strip_js_comments(text):
    """The script without its comments (none of these files has // or /* inside a string
    except https://, which the look-behind keeps)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?<![:'\"])//[^\n]*", "", text)


class Page(HTMLParser):
    """Every tag of a page with its attributes, plus the text inside script and style tags."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags = []      # (tag, {attribute: value})
        self.inline = []    # (tag, text) for script and style
        self.text = []      # (enclosing tag, text) for everything else
        self._stack = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag not in ("meta", "link", "input", "br", "img", "hr"):
            self._stack.append(tag)

    def handle_endtag(self, tag):
        while self._stack and self._stack.pop() != tag:
            pass

    def handle_data(self, data):
        if not data.strip():
            return
        inside = self._stack[-1] if self._stack else ""
        (self.inline if inside in ("script", "style") else self.text).append((inside, data.strip()))

    def metas(self, key, value):
        return [a.get("content") for tag, a in self.tags if tag == "meta" and a.get(key) == value]

    def ids(self):
        return {a["id"] for _, a in self.tags if a.get("id")}

    def references(self):
        return [(tag, name, a[name]) for tag, a in self.tags for name in ("href", "src", "action", "data") if name in a]


class DashboardPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(INDEX)
        cls.page = Page(cls.text)

    def test_the_policy_is_exact(self):
        self.assertEqual(self.page.metas("http-equiv", "Content-Security-Policy"), [CSP])

    def test_connect_src_self_appears_once_in_the_whole_file(self):
        # The deploy job's "Build the site" step fails unless this holds, comments included.
        self.assertEqual(self.text.count("connect-src 'self'"), 1)

    def test_no_inline_script_style_or_handler(self):
        self.assertEqual(self.page.inline, [])
        for tag, attrs in self.page.tags:
            self.assertNotIn("style", attrs, tag)
            self.assertEqual([a for a in attrs if a.lower().startswith("on")], [], tag)
            if tag == "script":
                self.assertTrue(attrs.get("src"), "a script tag without src")

    def test_config_loads_before_the_app(self):
        scripts = [a["src"].split("?")[0] for tag, a in self.page.tags if tag == "script"]
        self.assertLess(scripts.index("assets/js/config.js"), scripts.index("assets/js/app.js"))

    def test_the_footer_names_no_fixed_time(self):
        # config.yml and the Worker set the schedule, so the page must not repeat it.
        footer = self.text[self.text.index('<footer class="site-footer">'):self.text.index("</footer>")]
        self.assertIn(f"<p>{FOOTER}</p>", footer)
        self.assertNotRegex(footer, r"\d{1,2}:\d{2}|\bAM\b|\bPM\b|\bnoon\b|Pacific|\bUTC\b")


class ConfigTests(unittest.TestCase):
    def test_the_committed_worker_address_is_empty(self):
        # The deploy job writes the real address into the published copy only.
        code = strip_js_comments(read(CONFIG_JS)).strip()
        self.assertEqual(code, 'window.NEWS_DIGEST_CONFIG = { workerUrl: "" };')

    def test_no_worker_address_in_the_dashboard_script(self):
        self.assertNotRegex(read(APP_JS), r"workers\.dev")
        self.assertIn("window.NEWS_DIGEST_CONFIG", read(APP_JS))


class ViewerPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(VIEWER)
        cls.page = Page(cls.text)
        cls.script = read(SLIDES_JS)
        cls.code = strip_js_comments(cls.script)

    def test_the_policy_is_exact(self):
        self.assertEqual(self.page.metas("http-equiv", "Content-Security-Policy"), [CSP])
        self.assertEqual(self.page.metas("name", "referrer"), ["no-referrer"])

    def test_no_inline_script_style_or_handler(self):
        self.assertEqual(self.page.inline, [])
        for tag, attrs in self.page.tags:
            self.assertNotEqual(tag, "style")
            self.assertNotIn("style", attrs, tag)
            self.assertEqual([a for a in attrs if a.lower().startswith("on")], [], tag)
            if tag == "script":
                self.assertTrue(attrs.get("src"), "a script tag without src")

    def test_every_path_is_relative_to_a_dated_folder(self):
        # The commit job copies this file to docs/learn/<date>/index.html unchanged.
        allowed = {"../../", "#deck", "./briefing.pptx", "../../assets/favicon.svg", "../../assets/js/theme.js?v=1"}
        refs = self.page.references()
        self.assertGreaterEqual(len(refs), 8)
        for tag, name, value in refs:
            self.assertTrue(value in allowed or value.startswith("../../assets/learn/"), f"{tag} {name}={value}")
        for tag, name, value in refs:
            if value.startswith("../../assets/"):
                path = os.path.join(DOCS, "assets", value[len("../../assets/"):].split("?")[0])
                self.assertTrue(os.path.isfile(path), value)

    def test_the_script_reads_only_its_own_briefing(self):
        self.assertEqual(re.findall(r"\bfetch\(\s*([^,)]+)", self.code), ["'briefing.json'"])
        self.assertNotRegex(self.code, r"XMLHttpRequest|WebSocket|EventSource|sendBeacon|import\(")

    def test_the_script_finds_every_element_it_asks_for(self):
        wanted = set(re.findall(r"\$\('([a-z-]+)'\)", self.code))
        self.assertGreaterEqual(len(wanted), 10)
        self.assertEqual(wanted - self.page.ids(), set())

    def test_no_markup_from_strings_in_the_slideshow_script(self):
        self.assertIsNone(HTML_SINKS.search(self.code), HTML_SINKS.search(self.code))
        self.assertIn("textContent", self.code)

    def test_no_markup_from_strings_in_the_dashboard_briefing_code(self):
        app = read(APP_JS)
        start = app.index("// ---------- today's briefing ----------")
        section = strip_js_comments(app[start:app.index("// ----------", start + 20)])
        self.assertIn("function renderBriefing", section)
        self.assertIsNone(HTML_SINKS.search(section), HTML_SINKS.search(section))

    def test_only_https_links(self):
        # The same rule as render.py's clean_briefing, so a hand-edited briefing.json cannot show an http link.
        self.assertIn("p.protocol === 'https:'", self.code)
        self.assertNotRegex(self.code, r"['\"]http:")
        self.assertNotRegex(self.text + self.script + read(SLIDES_CSS), r"http://")

    def test_fonts_are_self_hosted(self):
        names = sorted(n for n in os.listdir(FONTS) if n.endswith(".woff2"))
        self.assertEqual(names, ["dm-serif-display-latin-400-normal.woff2", "outfit-latin-400-normal.woff2",
                                 "outfit-latin-600-normal.woff2"])
        for name in names:
            with open(os.path.join(FONTS, name), "rb") as f:
                self.assertEqual(f.read(4), b"wOF2", name)
        self.assertIn("SIL OPEN FONT LICENSE", read(os.path.join(FONTS, "OFL.txt")).upper())
        urls = re.findall(r"url\(([^)]*)\)", read(SLIDES_CSS))
        self.assertEqual(sorted(u.strip("\"'") for u in urls), ["fonts/" + n for n in names])

    def test_plain_punctuation(self):
        for path in (INDEX, VIEWER, SLIDES_JS, SLIDES_CSS, CONFIG_JS):
            self.assertNotRegex(read(path), "[\u2013\u2014]", os.path.basename(path))


class SlideStyleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = re.sub(r"/\*.*?\*/", "", read(SLIDES_CSS), flags=re.S)
        cls.code = strip_js_comments(read(SLIDES_JS))

    def rule(self, selector):
        found = re.search(r"^" + re.escape(selector) + r"\s*\{([^}]*)\}", self.css, flags=re.M)
        self.assertIsNotNone(found, selector)
        return found.group(1)

    def test_the_page_is_one_column_no_wider_than_the_screen(self):
        # Without a column template the implicit column grows with its content and
        # overflow: hidden clips it: at 320 px the Next button ended at 329 px.
        body = self.rule("body")
        self.assertRegex(body, r"grid-template-columns:\s*minmax\(0,\s*1fr\)\s*;")
        self.assertRegex(body, r"overflow:\s*hidden")
        self.assertRegex(self.rule(".deck"), r"min-width:\s*0")

    def test_one_fit_class_for_each_step_of_the_script(self):
        steps = int(re.search(r"const FIT_STEPS = (\d+);", self.code).group(1))
        found = re.findall(r"^\.fit-(\d+)\s*\{\s*--k:\s*([0-9.]+);\s*\}", self.css, flags=re.M)
        self.assertEqual([int(n) for n, _ in found], list(range(1, steps + 1)))
        scales = [1.0] + [float(k) for _, k in found]
        self.assertEqual(scales, sorted(scales, reverse=True))
        self.assertEqual(len(set(scales)), len(scales))
        self.assertGreaterEqual(scales[-1], 0.5)

    def test_reading_text_follows_the_step_and_has_a_floor(self):
        for selector in (".para", ".read-why", ".lede", ".toc-link", ".question-text", ".topic-title"):
            self.assertRegex(self.rule(selector), r"font-size:\s*max\(0\.\d+rem|font-size:\s*max\(1(\.\d+)?rem", selector)
            self.assertIn("var(--k)", self.rule(selector), selector)
        self.assertRegex(self.rule(".para"), r"font-size:\s*max\(0\.75rem,")  # 12 px is the smallest reading text

    def test_the_scroll_cue_is_styled_and_not_printed(self):
        self.assertRegex(self.rule(".more"), r"position:\s*absolute")
        self.assertIn("'Scroll for more'", self.code)
        printed = self.css[self.css.index("@media print"):]
        self.assertRegex(printed, r"\.more\s*\{\s*display:\s*none\s*!important")

    def test_the_viewer_asks_for_the_current_script_and_style(self):
        version = re.search(r"slides\.js v(\d+)", read(SLIDES_JS)).group(1)
        text = read(VIEWER)
        self.assertIn(f"slides.js?v={version}", text)
        self.assertIn(f"slides.css?v={version}", text)


# Runs slides.js in node against a stand-in for the page: elements that keep
# their children, classes, attributes and listeners; a history with Back and
# Forward; and a layout where a slide's text is as tall as it has characters,
# times 1 - 0.08 for each fit step. A button that turns off while it has the
# focus drops the focus to the body, as in a browser. Prints what happened.
SLIDES_HARNESS = r"""
import { readFileSync } from "node:fs";
const source = readFileSync(process.argv[2], "utf8");
const briefing = process.argv[3];

const layout = { screen: 1e9 };
const doc = { activeElement: null, title: "", listeners: {}, body: null };
class ClassList {
  constructor(el) { this.el = el; }
  names() { return this.el.className.split(/\s+/).filter(Boolean); }
  contains(c) { return this.names().includes(c); }
  toggle(c, force) {
    const on = force === undefined ? !this.contains(c) : !!force;
    const rest = this.names().filter((n) => n !== c);
    this.el.className = (on ? [...rest, c] : rest).join(" ");
    return on;
  }
  [Symbol.iterator]() { return this.names()[Symbol.iterator](); }
}
class El {
  constructor(tag, id) {
    this.tagName = tag.toUpperCase(); this.id = id || ""; this.children = []; this.parentNode = null;
    this.attrs = {}; this.listeners = {}; this.className = ""; this.own = ""; this.hidden = false;
    this.off = false; this.scrollTop = 0; this.classList = new ClassList(this);
  }
  get textContent() { return this.own + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.children.forEach((c) => { c.parentNode = null; }); this.children = []; this.own = v == null ? "" : String(v); }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  contains(o) { for (let n = o; n; n = n.parentNode) if (n === this) return true; return false; }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelectorAll(sel) { return this.all().filter((e) => e.classList.contains(sel.slice(1))); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type, ev) { (this.listeners[type] || []).forEach((fn) => fn(ev || {})); }
  focus() { if (!this.off) doc.activeElement = this; }
  click() { if (!this.off) this.fire("click"); }
  get disabled() { return this.off; }
  set disabled(v) { this.off = !!v; if (this.off && doc.activeElement === this) doc.activeElement = doc.body; }
  scrollBy(o) { this.scrollTop = Math.max(0, Math.min(this.scrollHeight - this.clientHeight, this.scrollTop + o.top)); this.fire("scroll"); }
  get scrollHeight() {
    const step = Number((/fit-(\d+)/.exec(this.parentNode.className) || [0, 0])[1]);
    return Math.ceil(this.textContent.length * (1 - 0.08 * step));
  }
  get clientHeight() { return Math.min(this.scrollHeight, layout.screen); }
}
const body = new El("body");
doc.body = body; doc.activeElement = body;
const add = (parent, tag, id) => parent.appendChild(new El(tag, id));
const bar = add(body, "header");
add(bar, "p", "bar-date"); add(bar, "a", "pptx-link");
const deck = add(body, "main", "deck");
const status = add(deck, "section", "status");
add(status, "h1", "status-title"); add(status, "p", "status-text"); add(status, "p", "status-actions");
const controls = add(body, "nav", "controls");
const prev = add(controls, "button", "prev");
add(controls, "p", "count"); add(controls, "progress", "progress"); add(controls, "p", "count-live");
const next = add(controls, "button", "next");

const frames = [];
const win = { listeners: {}, addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  matchMedia: () => ({ matches: true }) };
const fire = (type) => (win.listeners[type] || []).forEach((fn) => fn({}));
const location = { hash: "", pathname: "/learn/2026-11-11/", search: "" };
const entries = [""]; let at = 0;
const history = { replaceState(a, b, url) { location.hash = url.startsWith("#") ? url : ""; entries[at] = location.hash; } };
const visit = (hash) => { entries.splice(at + 1); entries.push(hash); at += 1; location.hash = hash; fire("hashchange"); };
const travel = (d) => { at += d; const was = location.hash; location.hash = entries[at]; if (was !== location.hash) fire("hashchange"); };
const documentStub = {
  getElementById: (id) => [body, ...body.all()].find((e) => e.id === id) || null,
  createElement: (tag) => new El(tag),
  addEventListener(type, fn) { (doc.listeners[type] = doc.listeners[type] || []).push(fn); },
  get activeElement() { return doc.activeElement; }, get body() { return doc.body; },
  get title() { return doc.title; }, set title(v) { doc.title = v; },
};
const press = (key, shiftKey) => (doc.listeners.keydown || []).forEach((fn) => fn(
  { key, shiftKey: !!shiftKey, target: doc.activeElement, defaultPrevented: false, preventDefault() {} }));
const fetchStub = async () => ({ status: 200, ok: true, text: async () => briefing });

new Function("document", "window", "location", "history", "fetch", "requestAnimationFrame", source)(
  documentStub, win, location, history, fetchStub, (fn) => frames.push(fn));
for (let i = 0; i < 5; i += 1) await new Promise((done) => setTimeout(done, 0));
const paint = () => { while (frames.length) frames.shift()(); };

const slides = () => deck.children.filter((c) => c.classList.contains("slide"));
const shown = () => slides().filter((s) => s.classList.contains("is-current")).map((s) => s.id).join(",");
const where = () => [shown(), location.hash];
const focus = () => (doc.activeElement === body ? "body" : doc.activeElement.id || doc.activeElement.className);
const more = () => deck.querySelector(".more");
const box = () => slides().find((s) => s.classList.contains("is-current")).querySelector(".slide-inner");
const fit = () => ({ step: Number((/fit-(\d+)/.exec(box().parentNode.className) || [0, 0])[1]), cue: !more().hidden,
  shaded: deck.classList.contains("has-more"), tabindex: box().getAttribute("tabindex"), over: box().scrollHeight - box().clientHeight });
const out = {};

out.title = doc.title;
out.count = slides().length;
out.counter = documentStub.getElementById("count").textContent;
out.labels = slides().map((s) => s.getAttribute("aria-label"));
out.heads = slides().map((s) => s.querySelectorAll(".col-head").map((h) => h.textContent));
out.titles = slides().map((s) => (s.querySelector(".topic-title") || { textContent: "" }).textContent);
out.contents = deck.querySelectorAll(".toc-link").map((a) => a.href);
out.links = deck.all().filter((e) => e.target === "_blank").map((a) => [a.textContent, a.href, a.rel]);
out.cueText = more().textContent;
out.start = [where(), prev.disabled, next.disabled];

// the Back button
const back = {};
visit(out.contents[1]);            back.contentsLink = where();
press("ArrowRight");               back.arrow = where();
travel(-1);                        back.back = [where(), documentStub.getElementById("count").textContent, prev.disabled];
travel(1);                         back.forward = where();
visit("#deck");                    back.skipLink = where();
press("ArrowRight");               back.arrowAfterSkip = where();
visit("#slide-999");               back.noSuchSlide = where();
visit("");                         back.emptied = where();
out.back = back;

// focus when a button turns off
const keep = {};
const last = slides().length;
visit("#slide-" + (last - 1));
next.focus(); next.click();        keep.end = [shown(), next.disabled, focus()];
prev.click();                      keep.backOne = [shown(), next.disabled, focus()];
visit("#slide-2");
prev.focus(); prev.click();        keep.start = [shown(), prev.disabled, focus()];
press("End"); doc.activeElement = body;
press("ArrowLeft"); press("ArrowRight"); keep.keyboard = [shown(), next.disabled, focus()];
const link = deck.querySelector(".toc-link");
press("Home"); link.focus(); press("ArrowRight"); keep.fromLink = [shown(), focus()];
out.focus = keep;

// fit: the text steps down until it fits, and the cue shows only when it cannot
visit("#slide-2");
const chars = box().textContent.length;
const sizes = { fits: chars + 10, tight: Math.ceil(chars * 0.8), small: Math.ceil(chars * 0.2) };
out.fit = { chars };
for (const [name, screen] of Object.entries(sizes)) {
  layout.screen = screen; fire("resize"); fire("resize"); paint();
  out.fit[name] = fit();
}
box().scrollBy({ top: 1e9 });      out.fit.atTheEnd = fit();
box().scrollBy({ top: -1e9 });     out.fit.atTheTopAgain = fit();
press("ArrowDown");                out.fit.arrowDown = box().scrollTop > 0;
press(" ");                        out.fit.space = [shown(), box().scrollTop > 0];
for (let i = 0; i < 9 && shown() === "slide-2"; i += 1) press(" ");
out.fit.spaceAtTheEnd = shown();
press("ArrowLeft");                out.fit.cueAgain = fit().cue;
press("Home");                     out.fit.otherSlide = [shown(), fit()];
visit("#slide-2"); layout.screen = 1e9; fire("resize"); paint();
out.fit.roomAgain = fit();
console.log(JSON.stringify(out));
"""

LONG = " ".join(["A sentence of plain words that fills the slide."] * 40)
BRIEFING = {
    "date": "2026-11-11", "model": "claude-opus-5-5", "llm": True, "usage": {"cost_usd": 0.2, "cost_estimated": True},
    "email_summary": "Two topics today.",
    "topics": [
        {"title": "First topic", "lanes": ["Frontier models"], "off_lane": False,
         "what_happened": LONG + "\n\n" + LONG, "why_it_matters": "It matters.",
         "read_first": [
             {"title": "Secure", "url": "https://example.com/a", "source": "Example", "why": "The source."},
             {"title": "Plain http", "url": "http://example.com/b", "source": "Example", "why": "Dropped."},
             {"title": "Script", "url": "javascript:alert(1)", "source": "Example", "why": "Dropped."},
             {"title": "Data", "url": "data:text/html,<script>alert(1)</script>", "source": "Example", "why": "Dropped."},
             {"title": "Relative", "url": "/learn/", "source": "Example", "why": "Dropped."},
             {"title": "Other scheme", "url": "ftp://example.com/c", "source": "Example", "why": "Dropped."},
         ],
         "open_question": "What next?", "reading_minutes": 3, "signals": {"sources": 3, "primary": True, "lane_hits": 2}},
        {"title": "<img src=x onerror=alert(1)> second topic", "lanes": [], "off_lane": True,
         "what_happened": "Short.", "why_it_matters": "Short too.",
         "read_first": [{"title": "Also secure", "url": "HTTPS://Example.org/x", "source": "", "why": ""}],
         "open_question": "", "reading_minutes": 1, "signals": {}},
    ],
}


@unittest.skipUnless(shutil.which("node"), "needs node")
class SlideshowBehaviorTests(unittest.TestCase):
    """docs/assets/learn/slides.js run in node on a two topic briefing."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp(prefix="news-digest-slides-")
        cls.addClassCleanup(shutil.rmtree, tmp)
        harness = os.path.join(tmp, "harness.mjs")
        with open(harness, "w", encoding="utf-8") as f:
            f.write(SLIDES_HARNESS)
        res = subprocess.run(["node", harness, SLIDES_JS, json.dumps(BRIEFING)],
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise AssertionError("the slideshow harness failed: " + res.stderr)
        cls.got = json.loads(res.stdout)

    def test_three_slides_a_topic(self):
        got = self.got
        self.assertEqual(got["count"], 8)  # a title slide, three for each of two topics, a closing slide
        self.assertEqual(got["counter"], "1 / 8")
        self.assertEqual(got["title"], "Learning briefing, Nov 11, 2026")
        self.assertEqual(got["labels"], [
            "Title",
            "Topic 1: what happened", "Topic 1: why it matters", "Topic 1: read first and open question",
            "Topic 2: what happened", "Topic 2: why it matters", "Topic 2: read first and open question",
            "The end"])
        self.assertEqual(got["heads"], [[], ["What happened"], ["Why it matters"], ["Read first", "Open question"],
                                        ["What happened"], ["Why it matters"], ["Read first", "Open question"], []])
        self.assertEqual(got["titles"][1:4], ["First topic"] * 3)
        self.assertEqual(got["titles"][4], "<img src=x onerror=alert(1)> second topic")  # text, never markup

    def test_contents_links_open_each_topics_first_slide(self):
        self.assertEqual(self.got["contents"], ["#slide-2", "#slide-5"])

    def test_only_https_links_are_shown(self):
        self.assertEqual(self.got["links"], [["Secure", "https://example.com/a", "noopener noreferrer"],
                                             ["Also secure", "https://example.org/x", "noopener noreferrer"]])

    def test_the_first_slide_has_a_plain_address(self):
        self.assertEqual(self.got["start"], [["slide-1", ""], True, False])

    def test_back_and_forward_keep_slide_and_address_in_sync(self):
        back = self.got["back"]
        self.assertEqual(back["contentsLink"], ["slide-5", "#slide-5"])
        self.assertEqual(back["arrow"], ["slide-6", "#slide-6"])
        # Back from #slide-N to the plain address used to leave slide 6 on screen.
        self.assertEqual(back["back"], [["slide-1", ""], "1 / 8", True])
        self.assertEqual(back["forward"], ["slide-6", "#slide-6"])

    def test_an_anchor_that_is_not_a_slide_leaves_the_slide_alone(self):
        back = self.got["back"]
        self.assertEqual(back["skipLink"], ["slide-6", "#deck"])
        self.assertEqual(back["arrowAfterSkip"], ["slide-7", "#slide-7"])
        self.assertEqual(back["noSuchSlide"][0], "slide-7")
        self.assertEqual(back["emptied"], ["slide-1", ""])

    def test_focus_moves_to_the_other_button_when_one_turns_off(self):
        keep = self.got["focus"]
        self.assertEqual(keep["end"], ["slide-8", True, "prev"])      # was: focus dropped to the body
        self.assertEqual(keep["backOne"], ["slide-7", False, "prev"])
        self.assertEqual(keep["start"], ["slide-1", True, "next"])

    def test_focus_elsewhere_is_left_alone(self):
        keep = self.got["focus"]
        self.assertEqual(keep["keyboard"], ["slide-8", True, "body"])
        self.assertEqual(keep["fromLink"], ["slide-2", "deck"])  # a link in a slide that went away

    def test_text_steps_down_until_it_fits(self):
        fit = self.got["fit"]
        self.assertGreater(fit["chars"], 3000)
        self.assertEqual(fit["fits"], {"step": 0, "cue": False, "shaded": False, "tabindex": None, "over": 0})
        # 80 percent of the room: steps 1 and 2 (92 and 84 percent) are too tall, step 3 (76) fits
        self.assertEqual(fit["tight"], {"step": 3, "cue": False, "shaded": False, "tabindex": None, "over": 0})
        self.assertEqual(fit["roomAgain"]["step"], 0)

    def test_scroll_for_more_shows_only_when_the_smallest_step_is_too_long(self):
        fit = self.got["fit"]
        self.assertEqual(self.got["cueText"], "Scroll for more")
        small = fit["small"]
        self.assertEqual((small["step"], small["cue"], small["shaded"], small["tabindex"]), (8, True, True, "0"))
        self.assertGreater(small["over"], 4)
        self.assertFalse(fit["atTheEnd"]["cue"])
        self.assertFalse(fit["atTheEnd"]["shaded"])
        self.assertTrue(fit["atTheTopAgain"]["cue"])
        self.assertTrue(fit["cueAgain"])
        self.assertEqual(fit["otherSlide"][0], "slide-1")
        self.assertFalse(fit["otherSlide"][1]["cue"])
        self.assertEqual(fit["roomAgain"], {"step": 0, "cue": False, "shaded": False, "tabindex": None, "over": 0})

    def test_keys_read_down_a_slide_that_scrolls_before_moving_on(self):
        fit = self.got["fit"]
        self.assertTrue(fit["arrowDown"])
        self.assertEqual(fit["space"], ["slide-2", True])
        self.assertEqual(fit["spaceAtTheEnd"], "slide-3")


# ---------------------------------------------------------------- the private stock section
# docs/assets/js/stocks.js fills the hidden section of index.html from the Worker, only while
# the access key is set. These checks read the files; StocksBrowserTests below runs the page.

STOCK_ROUTES = {"/stocks/list", "/stocks/overview", "/stocks/t/", "/stocks/tickers", "/stocks/settings",
                "/stocks/refresh", "/stocks/run", "/stocks/stop"}
# The data hosts a document may link to. github.com is not one of them: the one GitHub link, to a
# run's page, has its own exact check (RUN_LINK), so a document can never link to github.com.
LINK_HOSTS = {"sec.gov", "house.gov", "federalreserve.gov", "bls.gov", "bea.gov", "finra.org", "senate.gov"}
STOCKS_WORKER_JS = os.path.join(support.ROOT, "worker", "stocks.js")


def js_constant(code, name):
    """The literal assigned to `const NAME = ...;` in a script, as text."""
    found = re.search(r"const " + name + r" = (.+?);\n", code, flags=re.S)
    if not found:
        raise AssertionError(name + " is not defined")
    return found.group(1)


class StockSectionPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(INDEX)
        cls.page = Page(cls.text)
        start = cls.text.index('<section class="stocks"')
        cls.section = cls.text[start:cls.text.index("</section>", start)]

    def attrs(self, id_):
        found = [a for _, a in self.page.tags if a.get("id") == id_]
        self.assertEqual(len(found), 1, id_)
        return found[0]

    def test_the_section_and_its_nav_link_are_hidden(self):
        self.assertIn("hidden", self.attrs("stocks"))
        self.assertIn("hidden", self.attrs("nav-stocks"))
        self.assertEqual(self.attrs("nav-stocks").get("href"), "#stocks")
        self.assertIn("hidden", self.attrs("st-manage"))

    def test_the_section_holds_only_its_heading_and_empty_containers(self):
        # A visitor's copy of the page carries no stock data and no control: stocks.js builds them.
        no_comments = re.sub(r"<!--.*?-->", "", self.section, flags=re.S)
        words = " ".join(re.sub(r"<[^>]+>", " ", no_comments).split())
        self.assertEqual(words, "04 Stocks")
        for id_ in ("st-meta", "st-toolbar", "st-msg", "st-manage", "st-tabs", "st-panels"):
            self.assertRegex(no_comments, r'id="' + id_ + r'"[^>]*>\s*</(div|span)>', id_)
        self.assertNotRegex(no_comments, r"<(input|select|button|form|table|svg|a)\b")

    def test_it_sits_between_the_intro_and_the_controls(self):
        t = self.text
        self.assertLess(t.index('<section class="intro"'), t.index('<section class="stocks"'))
        self.assertLess(t.index('<section class="stocks"'), t.index('<div class="controls-band">'))

    def test_stocks_js_loads_last_deferred_at_its_own_version(self):
        scripts = [a for tag, a in self.page.tags if tag == "script"]
        names = [a["src"].split("?")[0] for a in scripts]
        self.assertEqual(names[-1], "assets/js/stocks.js")
        self.assertLess(names.index("assets/js/app.js"), names.index("assets/js/stocks.js"))
        self.assertIn("defer", scripts[-1])
        version = re.search(r"stocks\.js v(\d+)", read(STOCKS_JS)).group(1)
        self.assertEqual(scripts[-1]["src"], "assets/js/stocks.js?v=" + version)


class StockScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = read(STOCKS_JS)
        cls.code = strip_js_comments(cls.script)

    def test_every_request_goes_through_the_bridge(self):
        self.assertIn("window.NewsDigest", self.code)
        self.assertIn("bridge.workerFetch(", self.code)
        self.assertNotRegex(self.code, r"(?<![.\w])fetch\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon|import\(")
        self.assertEqual(len(re.findall(r"workerFetch\(", self.code)), 1)  # one helper, api(), makes every call

    def test_it_asks_only_for_the_contracts_routes(self):
        self.assertEqual(set(re.findall(r"'(/stocks/[a-z/]*)'", self.code)), STOCK_ROUTES)
        self.assertNotRegex(self.code, r"/stocks/ingest")

    def test_no_stock_data_in_storage_the_address_or_the_title(self):
        self.assertNotRegex(self.code, r"localStorage|sessionStorage|indexedDB|document\.cookie|caches\.")
        self.assertNotRegex(self.code, r"location\.(hash|search|href)\s*=|pushState|replaceState|document\.title")
        self.assertNotRegex(self.code, r"console\.")

    def test_no_markup_from_strings(self):
        self.assertIsNone(HTML_SINKS.search(self.code), HTML_SINKS.search(self.code))
        self.assertIn("textContent", self.code)
        self.assertIn("createElementNS", self.code)

    def test_only_https_links_to_public_data_hosts(self):
        self.assertIn("p.protocol !== 'https:'", self.code)
        # The SVG namespace is a name, not a link, and the only http: string allowed.
        self.assertEqual(self.code.count("'http://www.w3.org/2000/svg'"), 1)
        self.assertNotRegex(self.code.replace("'http://www.w3.org/2000/svg'", ""), r"['\"]http:")
        hosts = set(re.findall(r"'([a-z.]+)'", js_constant(self.code, "LINK_HOSTS")))
        self.assertEqual(hosts, LINK_HOSTS)

    def test_the_one_github_link_is_a_run_page_only(self):
        pattern = re.search(r"const RUN_LINK = /(.*)/;", self.code).group(1)
        self.assertEqual(pattern, r"^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/actions\/runs\/\d{1,20}$")
        self.assertIn("p.hostname === 'github.com' && p.href === u", self.code)
        self.assertNotIn("github.com", js_constant(self.code, "LINK_HOSTS"))

    def test_the_run_states_match_the_worker(self):
        worker = strip_js_comments(read(STOCKS_WORKER_JS))
        def listed(code, name):
            return re.findall(r"[\'\"]([a-z_]+)[\'\"]", re.search(r"const " + name + r" = \[(.*?)\];", code, flags=re.S).group(1))
        self.assertEqual(listed(self.code, "RUN_ACTIVE"), listed(worker, "STOCKS_RUN_ACTIVE"))
        self.assertEqual(listed(self.code, "RUN_CONCLUSIONS"), listed(worker, "STOCKS_RUN_CONCLUSIONS"))
        self.assertTrue(set(listed(self.code, "RUN_FAILED")) <= set(listed(worker, "STOCKS_RUN_CONCLUSIONS")))

    def test_it_finds_every_element_it_asks_for(self):
        wanted = set(re.findall(r"\$\('([a-z-]+)'\)", self.code))
        static = {"stocks", "nav-stocks", "st-meta", "st-toolbar", "st-msg", "st-manage", "st-tabs", "st-panels"}
        self.assertTrue(static <= wanted, static - wanted)
        built = set(re.findall(r"\.id = '([a-z-]+)'", self.code))  # the controls stocks.js makes itself
        self.assertEqual(wanted - Page(read(INDEX)).ids() - built, set())

    def test_the_models_prices_and_rules_match_python_and_config(self):
        import yaml

        import stocks_support  # noqa: F401  (puts scripts/ on sys.path)
        import stocks

        with open(CONFIG_YML, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)["stocks"]
        models = re.findall(r"'([a-z0-9-]+)'", js_constant(self.code, "STOCKS_MODELS"))
        self.assertEqual(tuple(models), tuple(stocks.ALLOWED_MODELS))
        self.assertEqual(set(models), set(cfg["models"]))
        self.assertEqual(js_constant(self.code, "DEFAULT_MODEL"), "'" + stocks.DEFAULT_MODEL + "'")
        prices = {m: {k: float(v) for k, v in re.findall(r"(input|output|cache_read): ([0-9.]+)", body)}
                  for m, body in re.findall(r"'([a-z0-9-]+)': \{([^}]*)\}", js_constant(self.code, "MODEL_PRICES"))}
        self.assertEqual(prices, {m: {k: float(cfg["models"][m][k]) for k in ("input", "output", "cache_read")}
                                  for m in cfg["models"]})
        self.assertEqual(js_constant(self.code, "TICKER_RE"), "/" + stocks.TICKER_RE.pattern + "/")
        self.assertEqual(int(js_constant(self.code, "MAX_TICKERS")), stocks.MAX_TICKERS)
        self.assertEqual(js_constant(self.code, "DISCLAIMER"), "'" + stocks.DISCLAIMER + "'")

    def test_the_spend_cap_default_is_the_configs(self):
        import yaml

        with open(CONFIG_YML, encoding="utf-8") as f:
            cap = yaml.safe_load(f)["stocks"]["max_run_usd"]
        # the cost estimate stops here unless the worker's list sends settings.max_run_usd
        self.assertEqual(float(js_constant(self.code, "MAX_RUN_USD")), float(cap))
        self.assertIn("st.max_run_usd", self.code)

    def test_plain_punctuation(self):
        for path in (STOCKS_JS, STYLE_CSS, INDEX, APP_JS):
            self.assertNotRegex(read(path), "[–—]", os.path.basename(path))

    def test_chart_colours_are_tokens_for_both_themes(self):
        css = read(STYLE_CSS)
        for name in ("--ch-s20", "--ch-s50", "--ch-s200"):
            self.assertEqual(len(re.findall(name + r":\s*#[0-9a-f]{6}", css)), 3, name)  # light, OS dark, chosen dark
        self.assertNotRegex(self.code, r"#[0-9a-fA-F]{6}\b")  # no colour is written by the script


class StockBridgeTests(unittest.TestCase):
    def test_app_js_exposes_one_frozen_bridge(self):
        code = strip_js_comments(read(APP_JS))
        self.assertEqual(code.count("window.NewsDigest"), 1)
        bridge = re.search(r"window\.NewsDigest = Object\.freeze\(\{(.*?)\}\);", code, flags=re.S)
        self.assertIsNotNone(bridge)
        keys = re.findall(r"^\s*(\w+)\s*(?::|,)", bridge.group(1), flags=re.M)
        self.assertEqual(keys, ["workerFetch", "hasWorker", "hasKey", "onKeyChange"])
        # The guard's 401 still forgets the key, which tells stocks.js to empty its section.
        self.assertIn("if (fromGuard && res.status === 401)", code)
        self.assertIn("keyListeners.slice().forEach", code)


# Runs the published page in headless chromium (playwright) against a stand-in Worker that
# serves stocks_support's example documents, the way the deploy job publishes it: the worker's
# origin added to connect-src and written into config.js. Prints what happened as JSON.
STOCKS_HARNESS = r"""
const { execSync } = require('child_process');
let pw;
try { pw = require(execSync('npm root -g').toString().trim() + '/playwright'); } catch (e) { process.exit(3); }
const fs = require('fs'); const path = require('path'); const http = require('http');
const [docsDir, siteDir, fixDir] = process.argv.slice(2);
const ORIGIN = 'https://worker.example.com';
const KEY = 'test-access-key-123';
execSync('cp -R ' + JSON.stringify(docsDir) + ' ' + JSON.stringify(siteDir));
const idx = path.join(siteDir, 'index.html');
fs.writeFileSync(idx, fs.readFileSync(idx, 'utf8').replace("connect-src 'self'", "connect-src 'self' " + ORIGIN));
fs.writeFileSync(path.join(siteDir, 'assets/js/config.js'), 'window.NEWS_DIGEST_CONFIG = { workerUrl: "' + ORIGIN + '" };\n');
// The same site under /fast/ with quick looks at the run state (1 s, and 0.3 s for 3 s after Stop the
// run), for the cases that follow a run through several looks.
execSync('cp -R ' + JSON.stringify(siteDir) + ' ' + JSON.stringify(siteDir + '.fast'));
fs.renameSync(siteDir + '.fast', path.join(siteDir, 'fast'));
{
  const sj = path.join(siteDir, 'fast', 'assets/js/stocks.js');
  let code = fs.readFileSync(sj, 'utf8');
  for (const [a, b] of [['const RUN_CHECK_MS = 30 * 1000;', 'const RUN_CHECK_MS = 1000;'],
    ['const STOP_CHECK_MS = 10 * 1000;', 'const STOP_CHECK_MS = 300;'], ['const STOP_CHECK_FOR_MS = 2 * 60 * 1000;', 'const STOP_CHECK_FOR_MS = 3000;']]) {
    if (!code.includes(a)) throw new Error('stocks.js no longer has ' + a);
    code = code.replace(a, b);
  }
  fs.writeFileSync(sj, code);
}
const doc = JSON.parse(fs.readFileSync(path.join(fixDir, 'doc.json'), 'utf8'));
const overview = JSON.parse(fs.readFileSync(path.join(fixDir, 'overview.json'), 'utf8'));
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };

// The worker's GET /stocks/run: nothing going, and a run of GitHub's list as the worker passes it on.
const IDLE = { checked: true, active: false, can_start: true, available_at: null, requested_at: null, run: null };
const iso = (ms) => new Date(Date.now() + ms).toISOString();
const ghRun = (id, status, conclusion, kind, url) => ({ id, status, conclusion: conclusion || null, kind: kind || 'manual',
  created_at: iso(-60000), updated_at: iso(-10000), started_at: iso(-55000), url: url || 'https://github.com/owner/news-digest/actions/runs/' + id });
const state = (run, more) => Object.assign({}, IDLE, { active: !!run && run.status !== 'completed', can_start: !run || run.status === 'completed', run }, more || {});

// opts: tickers, docs (ticker -> document), meta (ticker -> list fields), settings (more list
// settings), refresh, overview, model, delay (path -> ms, once), abort (a path prefix that fails),
// run (the /stocks/run answer) and runStatus, stop (the /stocks/stop answer) and afterStop (the run
// state from then on), refreshAnswer (the /stocks/refresh answer) and afterRefresh (likewise).
// w.runSeq, when set: the /stocks/run answers in turn, the last one kept.
function worker(opts) {
  opts = opts || {};
  const w = { tickers: (opts.tickers || ['AAPL', 'EXMPL']).slice(), docs: opts.docs || { AAPL: doc }, meta: opts.meta || {},
    settings: opts.settings || {}, refresh: opts.refresh || null, overview: opts.overview || overview, delay: opts.delay || {},
    abort: opts.abort || '', model: opts.model || 'claude-sonnet-5-5', calls: [],
    run: opts.run || IDLE, runStatus: opts.runStatus || 200, stop: opts.stop || { status: 200, body: { ok: true, stopped: 1 } },
    afterStop: opts.afterStop || null, refreshAnswer: opts.refreshAnswer || null, afterRefresh: opts.afterRefresh || null };
  const entry = (t) => {
    const d = w.docs[t];
    const e = d ? { ticker: t, status: d.status, name: (d.header || {}).name, close: (d.header || {}).last_close, change_pct: (d.header || {}).change_pct, stance: (d.assessment || {}).stance, confidence: (d.assessment || {}).confidence, data_as_of: d.data_as_of, generated_at: d.generated_at } : { ticker: t, status: 'pending' };
    return Object.assign(e, w.meta[t] || {});
  };
  w.handle = (method, url, headers, body) => {
    const p = new URL(url).pathname;
    w.calls.push([method, p, body || '', headers.authorization === 'Bearer ' + KEY]);
    const json = (status, b, extra) => ({ status, headers: Object.assign({ 'content-type': 'application/json' }, extra || {}), body: JSON.stringify(b) });
    if (headers.authorization !== 'Bearer ' + KEY) return json(401, { error: 'unauthorized' }, { 'x-guard': 'deny' });
    if (p === '/stocks/list') return json(200, { max: 30,
      tickers: w.tickers.map(entry),
      settings: Object.assign({ model: w.model, default_model: 'claude-sonnet-5-5', allowed_models: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5'] }, w.settings),
      refresh: w.refresh || { min_minutes: 30, requested_at: null, available_at: null }, overview: { generated_at: w.overview.generated_at } });
    if (p === '/stocks/overview') return json(200, w.overview);
    if (p.startsWith('/stocks/t/')) { const d = w.docs[decodeURIComponent(p.slice(10))]; return d ? json(200, d) : json(404, { error: 'no analysis yet' }); }
    if (p === '/stocks/settings') { w.model = JSON.parse(body).model; return json(200, { ok: true, model: w.model }); }
    if (p === '/stocks/tickers') {
      const b = JSON.parse(body);
      w.tickers = b.action === 'add' ? w.tickers.concat([b.ticker]) : w.tickers.filter((t) => t !== b.ticker);
      return json(200, { ok: true, changed: true, tickers: w.tickers });
    }
    if (p === '/stocks/refresh') {
      if (w.afterRefresh) w.run = w.afterRefresh();
      const a = w.refreshAnswer ? w.refreshAnswer() : { status: 202, body: { ok: true, requested_at: iso(0), available_at: iso(1800000) } };
      return json(a.status, a.body);
    }
    if (p === '/stocks/run') {
      if (w.runSeq && w.runSeq.length) w.run = w.runSeq.length > 1 ? w.runSeq.shift() : w.runSeq[0];
      return json(w.runStatus, w.runStatus === 200 ? w.run : { error: 'not found' });
    }
    if (p === '/stocks/stop') { if (w.afterStop) w.run = w.afterStop(); return json(w.stop.status, w.stop.body); }
    return json(404, { error: 'not found' });
  };
  return w;
}

(async () => {
  const srv = http.createServer((req, res) => {
    let p = decodeURIComponent(new URL(req.url, 'http://x').pathname);
    if (p.endsWith('/')) p += 'index.html';
    const f = path.join(siteDir, p);
    if (!f.startsWith(siteDir) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'content-type': TYPES[path.extname(f)] || 'application/octet-stream' });
    res.end(fs.readFileSync(f));
  });
  await new Promise((ok) => srv.listen(0, '127.0.0.1', ok));
  const base = 'http://127.0.0.1:' + srv.address().port + '/index.html';
  const fastBase = 'http://127.0.0.1:' + srv.address().port + '/fast/index.html';
  let browser;
  try { browser = await pw.chromium.launch(); } catch (e) { srv.close(); process.exit(3); }
  const out = {};
  const open = async (w, key, scheme, width, quiet, fast) => {
    const ctx = await browser.newContext({ viewport: { width: width || 1280, height: 900 }, colorScheme: scheme || 'dark', reducedMotion: 'reduce' });
    const page = await ctx.newPage();
    const errors = [];
    page.on('console', (m) => { if (m.type() === 'error' && !(quiet && quiet.test(m.text()))) errors.push(m.text()); });
    page.on('pageerror', (e) => errors.push(e.message));
    await page.addInitScript(() => document.addEventListener('securitypolicyviolation', (e) => console.error('CSP violation ' + e.violatedDirective)));
    if (key) await page.addInitScript((k) => localStorage.setItem('nd_worker_key_v1', k), key);
    if (scheme) await page.addInitScript((t) => localStorage.setItem('theme', t), scheme);
    await page.route(ORIGIN + '/**', async (route) => {
      const q = route.request();
      const p = new URL(q.url()).pathname;
      if (w.abort && p.startsWith(w.abort)) { w.calls.push([q.method(), p, '', true]); await route.abort('failed'); return; }
      const r = w.handle(q.method(), q.url(), q.headers(), q.postData()); // the answer as of the request
      if (w.delay[p]) { const ms = w.delay[p]; delete w.delay[p]; await new Promise((ok) => setTimeout(ok, ms)); }
      await route.fulfill({ status: r.status, body: r.body, headers: Object.assign({ 'access-control-allow-origin': '*', 'access-control-expose-headers': 'x-guard', 'access-control-allow-headers': 'authorization, content-type' }, r.headers) });
    });
    await page.goto(fast ? fastBase : base);
    await page.waitForTimeout(800);
    return { ctx, page, errors };
  };
  const shown = (page) => page.evaluate(() => [!document.getElementById('stocks').hidden, !document.getElementById('nav-stocks').hidden, document.getElementById('st-panels').children.length]);
  try {
    { // a visitor
      const w = worker();
      const { ctx, page, errors } = await open(w, '');
      out.visitor = { shown: await shown(page), calls: w.calls.length, errors };
      await ctx.close();
    }
    for (const scheme of ['dark', 'light']) { // the key holder
      const w = worker();
      const { ctx, page, errors } = await open(w, KEY, scheme);
      await page.waitForSelector('.st-tile');
      const r = { shown: await shown(page) };
      r.market = await page.evaluate(() => ({
        tabs: Array.from(document.querySelectorAll('[role=tab]')).map((t) => t.getAttribute('data-key')),
        catalysts: document.querySelectorAll('#st-panel .st-c5 tbody tr').length, tiles: document.querySelectorAll('.st-tile').length,
        news: document.querySelectorAll('.st-news-i').length, headline: document.querySelector('.st-headline').textContent,
        yours: document.querySelectorAll('#st-yours tbody tr').length, disclaimer: document.querySelector('.st-disclaimer').textContent }));
      await page.click('[role=tab][data-key="AAPL"]');
      await page.waitForSelector('.st-chart svg');
      r.ticker = await page.evaluate(() => ({
        name: document.querySelector('.st-tname').textContent, candles: document.querySelectorAll('.st-ch-candles rect').length,
        lines: ['s20', 's50', 's200', 'rsi', 'ratio'].filter((k) => document.querySelector('.st-ch-' + k)).length,
        guides: document.querySelectorAll('.st-ch-guide').length, cards: document.querySelectorAll('#st-panel .st-card').length,
        insiders: document.querySelectorAll('#st-panel a[href^="https://www.sec.gov/"]').length, disclaimer: document.querySelector('.st-disclaimer').textContent,
        styled: document.querySelectorAll('#stocks [style]').length, wide: document.documentElement.scrollWidth > window.innerWidth }));
      if (scheme === 'dark') {
        await page.click('#st-settings');
        await page.selectOption('#st-model', 'claude-haiku-5-5');
        await page.waitForFunction(() => /Haiku 5\.5/.test(document.getElementById('st-msg').textContent));
        await page.fill('#st-ticker', 'not a ticker');
        await page.click('#st-add-btn');
        await page.fill('#st-ticker', 'brk.b');
        await page.click('#st-add-btn');
        await page.waitForSelector('.st-remove[data-ticker="BRK-B"]');
        await page.click('.st-remove[data-ticker="EXMPL"]');
        await page.click('.st-remove[data-ticker="EXMPL"]');
        await page.waitForFunction(() => /Removed EXMPL/.test(document.getElementById('st-msg').textContent));
        await page.click('#st-run');
        await page.waitForFunction(() => /Started/.test(document.getElementById('st-msg').textContent));
        r.posts = w.calls.filter((c) => c[0] === 'POST').map((c) => c[1] + ' ' + c[2]);
        r.runDisabled = await page.evaluate(() => document.getElementById('st-run').disabled);
        r.allKeyed = w.calls.every((c) => c[3]);
        r.storage = await page.evaluate(() => Object.keys(localStorage).sort().concat(Object.keys(sessionStorage)));
        r.address = await page.evaluate(() => location.hash + location.search);
        await page.evaluate(() => { localStorage.setItem('nd_worker_key_v1', 'wrong'); window.dispatchEvent(new StorageEvent('storage', { key: 'nd_worker_key_v1' })); });
        await page.waitForFunction(() => localStorage.getItem('nd_worker_key_v1') === null && document.getElementById('stocks').hidden);
        r.after401 = { shown: await shown(page), key: await page.evaluate(() => localStorage.getItem('nd_worker_key_v1')) };
        r.errors = errors.filter((e) => !/status of 401/.test(e));
      } else r.errors = errors;
      out[scheme] = r;
      await ctx.close();
    }
    { // a phone
      const w = worker();
      const { ctx, page, errors } = await open(w, KEY, 'dark', 360);
      await page.waitForSelector('.st-tile');
      const market = await page.evaluate(() => document.documentElement.scrollWidth);
      await page.click('[role=tab][data-key="AAPL"]');
      await page.waitForSelector('.st-chart svg');
      out.phone = { widths: [market, await page.evaluate(() => document.documentElement.scrollWidth)], errors };
      await ctx.close();
    }
    const copy = (v) => JSON.parse(JSON.stringify(v));
    const hits = (page) => page.evaluate(() => Array.from(document.querySelectorAll('#st-yours .st-hit')).map((c) => c.textContent));
    const docCalls = (w) => w.calls.filter((c) => c[1].startsWith('/stocks/t/')).length;
    { // a long list: hit rates from the list, a few documents, and the focus stays while one comes in
      const zs = ['ZZA', 'ZZB', 'ZZC', 'ZZD', 'ZZE', 'ZZF', 'ZZG'];
      const meta = {};
      zs.forEach((t) => { meta[t] = { status: 'ok', name: 'Example ' + t, hit: { rate: 0.61, n: 120, kind: 'backtest', h: 20 } }; });
      const w = worker({ tickers: ['AAPL'].concat(zs), meta, delay: { '/stocks/t/AAPL': 2500 } });
      const { ctx, page, errors } = await open(w, KEY, 'dark', 0, /status of 404/); // the ZZ stocks have no analysis
      await page.focus('#st-yours .st-open'); // the first stock, AAPL
      const before = (await hits(page))[0];
      await page.waitForFunction(() => /%/.test(document.querySelector('#st-yours .st-hit').textContent), null, { timeout: 10000 });
      for (let i = 0; i < 50 && docCalls(w) < 4; i += 1) await page.waitForTimeout(100);
      await page.waitForTimeout(800); // and no more than that
      out.longList = { before, hits: await hits(page), docCalls: docCalls(w), errors,
        focus: await page.evaluate(() => document.activeElement.className + ', ' + document.activeElement.getAttribute('aria-label')) };
      await ctx.close();
    }
    { // documents that cannot be fetched: no cell or tab is left on Loading
      const meta = { ZZA: { status: 'ok' }, ZZB: { status: 'ok' } };
      const w = worker({ tickers: ['AAPL', 'ZZA', 'ZZB'], meta, abort: '/stocks/t/' });
      const { ctx, page, errors } = await open(w, KEY, 'dark', 0, /Failed to load resource/);
      const r = { hits: await hits(page) };
      await page.click('[role=tab][data-key="ZZA"]');
      await page.waitForTimeout(600);
      r.tab = await page.evaluate(() => document.getElementById('st-panel').innerText);
      r.errors = errors;
      out.failing = r;
      await ctx.close();
    }
    { // new results while an older request for a document is still out: the old answer is dropped
      const now = Date.now();
      const newer = copy(doc);
      newer.header.name = 'Newer Example Inc.';
      const w = worker({ overview: Object.assign(copy(overview), { generated_at: new Date(now - 3600000).toISOString() }),
        refresh: { min_minutes: 30, requested_at: new Date(now - 60000).toISOString(), available_at: new Date(now + 1740000).toISOString() },
        run: state(ghRun(400, 'in_progress')), delay: { '/stocks/t/AAPL': 2500 } });
      const { ctx, page, errors } = await open(w, KEY, 'dark');
      w.overview = Object.assign(copy(overview), { generated_at: new Date().toISOString() });
      w.docs = { AAPL: newer };
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange'))); // checks for results at once
      await page.waitForTimeout(3200);
      await page.click('[role=tab][data-key="AAPL"]');
      await page.waitForTimeout(300);
      out.newRun = { name: await page.evaluate(() => document.querySelector('.st-tfull').textContent),
        msg: await page.evaluate(() => document.getElementById('st-msg').textContent), errors };
      await ctx.close();
    }
    { // values that do not fit: an impossible day, a far timestamp, one fund, an unknown ticker
      const bad = copy(doc);
      bad.holders.insiders.transactions[0].date = '2026-13-01';
      bad.valuation.latest_end = '2026-02-30';
      bad.holders.funds.funds_checked = 1;
      Object.assign(bad.shorts.latest, { percentile: 91.6, n: 251 });
      const unknown = { kind: 'stocks.ticker', version: 1, ticker: 'EXMPL', status: 'unknown_ticker', notes: [], header: { ticker: 'EXMPL', name: null },
        model: null, llm: false, cost: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cost_usd: 0, cost_estimated: false },
        generated_at: doc.generated_at, data_as_of: null, disclaimer: doc.disclaimer };
      const ov = copy(overview);
      ov.news[0].published = '+275760-09-13T00:00:00Z'; // a valid Date, but no day the day format can print
      const w = worker({ docs: { AAPL: bad, EXMPL: unknown }, overview: ov });
      const { ctx, page, errors } = await open(w, KEY, 'dark');
      const panel = () => page.evaluate(() => ({
        cards: Array.from(document.querySelectorAll('#st-panel .st-card')).map((c) => c.querySelector('h3').textContent + (/Not available\. This part/.test(c.textContent) ? ' (not available)' : '')),
        foot: Array.from(document.querySelectorAll('#st-panel .st-foot p')).map((x) => x.textContent) }));
      const r = { market: await panel() };
      await page.click('[role=tab][data-key="AAPL"]');
      await page.waitForSelector('.st-chart svg', { timeout: 5000 }).catch(() => {}); // a blank panel is checked below
      r.ticker = await panel();
      r.texts = await page.evaluate(() => {
        const dd = (k) => { const dt = Array.from(document.querySelectorAll('#st-panel dt')).find((x) => x.textContent === k); return dt ? dt.nextElementSibling.textContent : null; };
        const cardOf = (title) => Array.from(document.querySelectorAll('#st-panel .st-card')).find((c) => c.querySelector('h3').textContent === title);
        const inner = (c, sel) => (c && c.querySelector(sel) ? c.querySelector(sel).textContent : null);
        return { percentile: dd('Percentile'), latest: dd('Latest period'), funds: inner(cardOf('Famous funds'), '.tag'),
          insiderDay: inner(cardOf('Company insiders'), 'tbody td') };
      });
      await page.click('[role=tab][data-key="EXMPL"]');
      await page.waitForTimeout(400);
      r.unknown = await panel();
      r.errors = errors;
      out.badValues = r;
      await ctx.close();
    }
    { // the cost estimate for a few lists, models and spend caps
      const names = Array.from({ length: 30 }, (_, i) => 'Z' + String.fromCharCode(65 + (i % 26)) + (i >= 26 ? 'A' : ''));
      out.cost = {};
      for (const [label, model, n, settings] of [['opus30', 'claude-opus-5-5', 30, {}], ['haiku1', 'claude-haiku-5-5', 1, {}],
        ['opus30cap', 'claude-opus-5-5', 30, { max_run_usd: 1.5 }], ['sonnet0', 'claude-sonnet-5-5', 0, {}]]) {
        const w = worker({ tickers: names.slice(0, n), docs: {}, model, settings });
        const { ctx, page, errors } = await open(w, KEY, 'dark');
        await page.click('#st-settings');
        out.cost[label] = await page.evaluate(() => ({ options: Array.from(document.querySelectorAll('#st-model option')).map((o) => o.textContent),
          hints: Array.from(document.querySelectorAll('#st-manage .st-mcol:first-child .hint')).map((x) => x.textContent) }));
        out.cost[label].errors = errors;
        await ctx.close();
      }
    }
    // ---------- the run on GitHub: GET /stocks/run and POST /stocks/stop ----------
    const bar = (page) => page.evaluate(() => {
      const run = document.getElementById('st-run');
      const stop = document.getElementById('st-stop');
      const a = document.querySelector('#st-toolbar a');
      return { lines: Array.from(document.querySelectorAll('#st-toolbar .st-bar-info p')).map((x) => x.textContent),
        runDisabled: run.disabled, stop: stop ? { text: stop.textContent, cls: stop.className, disabled: stop.disabled, busy: stop.getAttribute('aria-busy') } : null,
        link: a ? { href: a.getAttribute('href'), text: a.textContent, target: a.target, rel: a.rel } : null,
        buttons: Array.from(document.querySelectorAll('#st-toolbar button')).map((b) => b.id),
        msg: document.getElementById('st-msg').textContent, msgLines: Array.from(document.querySelectorAll('#st-msg p')).map((x) => x.textContent),
        focus: document.activeElement ? document.activeElement.id : '',
        wide: document.documentElement.scrollWidth, styled: document.querySelectorAll('#stocks [style]').length };
    });
    // A time as the page writes it: 12:27 PM today, Nov 16, 12:27 PM another day.
    const moment = (page, t) => page.evaluate((t) => {
      const d = new Date(t);
      const c = new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit' }).format(d);
      return d.toDateString() === new Date().toDateString() ? c : new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(d);
    }, t);
    const posts = (w, path) => w.calls.filter((c) => c[0] === 'POST' && c[1] === path).length;
    const reads = (w) => w.calls.filter((c) => c[1] === '/stocks/run').length;
    out.runs = {};
    { // a run is going: Stop the run, one POST, then Run analysis now once GitHub says it is over
      const going = ghRun(501, 'in_progress');
      const w = worker({ run: state(going), afterStop: () => state(Object.assign({}, going, { status: 'completed', conclusion: 'cancelled', updated_at: iso(0) })),
        delay: { '/stocks/stop': 800 } });
      const { ctx, page, errors } = await open(w, KEY, 'dark', 390);
      await page.waitForSelector('#st-stop');
      const r = { going: await bar(page), started: await moment(page, going.started_at) };
      await page.click('#st-stop');
      await page.waitForFunction(() => document.getElementById('st-stop') && document.getElementById('st-stop').textContent === 'Stopping');
      r.busy = await bar(page);
      await page.waitForFunction(() => /^Stopped\./.test(document.getElementById('st-msg').textContent));
      r.stopped = await bar(page);
      r.readsBefore = reads(w);
      await page.waitForFunction(() => !document.getElementById('st-run').disabled, null, { timeout: 20000 });
      r.after = await bar(page);
      r.stoppedAt = await moment(page, w.run.run.updated_at);
      r.stops = posts(w, '/stocks/stop');
      r.refreshes = posts(w, '/stocks/refresh');
      r.readsAfter = reads(w);
      r.errors = errors;
      out.runs.going = r;
      await ctx.close();
    }
    for (const [label, run, width] of [['failed', ghRun(502, 'completed', 'failure'), 390], ['timedOut', ghRun(503, 'completed', 'timed_out', 'scheduled'), 0],
      ['cancelled', ghRun(504, 'completed', 'cancelled'), 0], ['succeeded', ghRun(505, 'completed', 'success'), 0],
      ['evilHost', ghRun(506, 'completed', 'failure', 'manual', 'https://evil.example/owner/news-digest/actions/runs/506'), 0],
      ['lookalike', ghRun(507, 'completed', 'failure', 'manual', 'https://github.com.evil.example/owner/news-digest/actions/runs/507'), 0],
      ['query', ghRun(508, 'completed', 'failure', 'manual', 'https://github.com/owner/news-digest/actions/runs/508?x=1'), 0],
      ['plainHttp', ghRun(509, 'completed', 'failure', 'manual', 'http://github.com/owner/news-digest/actions/runs/509'), 0]]) {
      // The claim of the old 30 minute rule is still on: GitHub's view frees the button all the same.
      const now = Date.now();
      const w = worker({ run: state(run, { requested_at: iso(-300000) }), refresh: { min_minutes: 30, requested_at: new Date(now - 300000).toISOString(), available_at: new Date(now + 1500000).toISOString() } });
      const { ctx, page, errors } = await open(w, KEY, 'dark', width);
      out.runs[label] = Object.assign(await bar(page), { at: await moment(page, run.updated_at), errors });
      await ctx.close();
    }
    { // inside the start grace: no Stop, and the time it is free again
      const at = Date.now() + 90000;
      const w = worker({ run: Object.assign({}, IDLE, { can_start: false, available_at: new Date(at).toISOString(), requested_at: iso(-30000) }) });
      const { ctx, page, errors } = await open(w, KEY, 'dark');
      out.runs.grace = Object.assign(await bar(page), { at: await page.evaluate((t) => new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit' }).format(new Date(t)), at), errors });
      await ctx.close();
    }
    { // after Run analysis now: GitHub lists the run, it fails, and the page stops waiting at once
      const w = worker({ refreshAnswer: () => ({ status: 202, body: { ok: true, requested_at: iso(0), available_at: iso(2000) } }),
        afterRefresh: () => state(Object.assign(ghRun(510, 'completed', 'failure'), { created_at: iso(1000), updated_at: iso(1500) })) });
      const { ctx, page, errors } = await open(w, KEY, 'dark');
      await page.click('#st-run');
      await page.waitForFunction(() => /^Started\./.test(document.getElementById('st-msg').textContent));
      const r = { waiting: await bar(page) };
      await page.waitForFunction(() => /stopped with an error/.test(document.getElementById('st-toolbar').textContent), null, { timeout: 15000 });
      r.ended = await bar(page);
      const polls = w.calls.filter((c) => c[1] === '/stocks/overview').length;
      await page.waitForTimeout(500);
      r.overviewAfter = w.calls.filter((c) => c[1] === '/stocks/overview').length - polls;
      r.errors = errors;
      out.runs.follow = r;
      await ctx.close();
    }
    { // Run analysis now while a run is going (another tab, or the schedule): 409, then Stop shows
      const w = worker({ refreshAnswer: () => ({ status: 409, body: { error: 'a run is going', active: true } }),
        afterRefresh: () => state(ghRun(511, 'queued', null, 'scheduled')) });
      const { ctx, page, errors } = await open(w, KEY, 'dark', 0, /status of 409/); // the browser logs the 409 it was sent
      await page.click('#st-run');
      await page.waitForSelector('#st-stop');
      out.runs.busy409 = Object.assign(await bar(page), { errors });
      await ctx.close();
    }
    for (const [label, opts, quiet] of [
      ['unread', { run: Object.assign({}, IDLE, { checked: false, reason: 'GitHub could not be read', can_start: false }) }],
      ['notSetUp', { run: Object.assign({}, IDLE, { checked: false, reason: 'not set up', can_start: false }) }],
      ['oldWorker', { runStatus: 404 }, /status of 404/]]) {
      // Without GitHub's view the list's rule stands: a start 10 minutes ago, results in since.
      const now = Date.now();
      const w = worker(Object.assign({ refresh: { min_minutes: 30, requested_at: new Date(now - 600000).toISOString(), available_at: new Date(now + 1200000).toISOString() },
        overview: Object.assign(copy(overview), { generated_at: new Date(now - 300000).toISOString() }) }, opts));
      const { ctx, page, errors } = await open(w, KEY, 'dark', 0, quiet);
      out.runs[label] = Object.assign(await bar(page), { errors });
      await ctx.close();
    }
    // ---------- following a run through several looks (the /fast/ site) ----------
    // A wait that does not throw: whether it came, so a page that never gets there fails its own test.
    const until = (page, fn, ms) => page.waitForFunction(fn, null, { timeout: ms || 10000 }).then(() => true, () => false);
    const ended = (page) => until(page, () => /stopped with an error/.test(document.getElementById('st-toolbar').textContent));
    { // the run before failed at startup 20 s ago; GitHub has not listed the new run after several looks
      const before = Object.assign(ghRun(520, 'completed', 'startup_failure'), { created_at: iso(-20000), updated_at: iso(-15000) });
      const w = worker({ run: state(before),
        refreshAnswer: () => ({ status: 202, body: { ok: true, requested_at: iso(0), available_at: iso(120000) } }),
        afterRefresh: () => Object.assign({}, IDLE, { can_start: false, available_at: iso(120000), requested_at: iso(0), run: before }) });
      const { ctx, page, errors } = await open(w, KEY, 'dark', 390, null, true);
      const r = { before: await bar(page) };
      await page.click('#st-run');
      await page.waitForFunction(() => /^Started\./.test(document.getElementById('st-msg').textContent));
      r.started = await bar(page);
      const reads0 = reads(w);
      await page.waitForTimeout(3500);
      r.later = await bar(page);
      r.looks = reads(w) - reads0;
      await page.evaluate(() => document.getElementById('st-run').click()); // a click on the off button does nothing
      await page.waitForTimeout(300);
      r.refreshes = posts(w, '/stocks/refresh');
      r.errors = errors;
      out.runs.awaited = r;
      await ctx.close();
    }
    for (const kind of ['scheduled', 'manual']) {
      // A run is going; one look gets the worker's "GitHub could not be read"; then the run fails.
      const going = ghRun(530, 'in_progress', null, kind);
      const failed = Object.assign({}, going, { status: 'completed', conclusion: 'failure', updated_at: iso(0) });
      const UNREAD = Object.assign({}, IDLE, { checked: false, reason: 'GitHub could not be read', can_start: true });
      const seq = [state(going), UNREAD, state(going), state(failed)];
      const w = worker(kind === 'scheduled' ? { run: seq[0] } : {
        refreshAnswer: () => ({ status: 202, body: { ok: true, requested_at: iso(0), available_at: iso(120000) } }),
        afterRefresh: () => { w.runSeq = seq.slice(); return w.run; } });
      if (kind === 'scheduled') w.runSeq = seq.slice();
      const { ctx, page, errors } = await open(w, KEY, 'dark', 390, null, true);
      if (kind === 'manual') await page.click('#st-run');
      await until(page, () => !!document.getElementById('st-stop'));
      const r = { going: await bar(page) };
      await until(page, () => /Could not check GitHub/.test(document.getElementById('st-toolbar').textContent));
      r.blip = await bar(page);
      await until(page, () => !/Could not check GitHub/.test(document.getElementById('st-toolbar').textContent));
      r.back = await bar(page);
      await ended(page);
      r.ended = await bar(page);
      r.reads = reads(w);
      r.errors = errors;
      out.runs['blip' + kind[0].toUpperCase() + kind.slice(1)] = r;
      await ctx.close();
    }
    { // GitHub could not be read when the page loaded; after Run analysis now it can, and the run is going
      const w = worker({ run: Object.assign({}, IDLE, { checked: false, reason: 'GitHub could not be read' }),
        afterRefresh: () => state(ghRun(535, 'in_progress')) });
      const { ctx, page, errors } = await open(w, KEY, 'dark', 0, null, true);
      const r = { before: await bar(page) };
      await page.click('#st-run');
      await until(page, () => !!document.getElementById('st-stop'));
      r.after = await bar(page);
      r.errors = errors;
      out.runs.unreadFirst = r;
      await ctx.close();
    }
    { // the run started here saves its results, then ends with an error (the congress job, say)
      const w = worker({ refreshAnswer: () => ({ status: 202, body: { ok: true, requested_at: iso(0), available_at: iso(120000) } }),
        afterRefresh: () => {
          w.overview = Object.assign(copy(overview), { generated_at: iso(700), run: Object.assign({}, overview.run, { reason: 'manual', finished_at: iso(700) }) });
          return state(Object.assign(ghRun(540, 'completed', 'failure'), { created_at: iso(500), updated_at: iso(900) }));
        } });
      const { ctx, page, errors } = await open(w, KEY, 'dark', 390, null, true);
      const lists0 = w.calls.filter((c) => c[1] === '/stocks/list').length;
      await page.click('#st-run');
      await until(page, () => document.getElementById('st-msg').textContent === 'New results are in.');
      await ended(page);
      out.runs.savedThenFailed = Object.assign(await bar(page), { lists: w.calls.filter((c) => c[1] === '/stocks/list').length - lists0,
        at: await moment(page, w.run.run.updated_at), errors });
      await ctx.close();
    }
    { // Stop the run is taken, but GitHub still lists the run as going when the wait after it is over
      const going = ghRun(550, 'in_progress');
      const w = worker({ run: state(going) });
      const { ctx, page, errors } = await open(w, KEY, 'dark', 390, null, true);
      await page.waitForSelector('#st-stop');
      await page.click('#st-stop');
      await page.waitForFunction(() => /^Stopped\./.test(document.getElementById('st-msg').textContent));
      const r = { stopped: await bar(page) };
      await until(page, () => /^GitHub has not stopped the run yet\./.test(document.getElementById('st-msg').textContent));
      r.notYet = await bar(page);
      await page.waitForTimeout(1500);
      r.later = await bar(page);
      w.run = state(Object.assign({}, going, { status: 'completed', conclusion: 'cancelled', updated_at: iso(0) })); // GitHub stops it after all
      await until(page, () => !document.getElementById('st-run').disabled);
      r.ended = await bar(page);
      r.stops = posts(w, '/stocks/stop');
      r.errors = errors;
      out.runs.neverStops = r;
      await ctx.close();
    }
  } finally {
    await browser.close();
    srv.close();
  }
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class StocksBrowserTests(unittest.TestCase):
    """docs/ in headless chromium with a stand-in Worker; skipped when playwright or chromium is missing."""

    @classmethod
    def setUpClass(cls):
        import stocks_support

        tmp = tempfile.mkdtemp(prefix="news-digest-stocks-")
        cls.addClassCleanup(shutil.rmtree, tmp)
        fix = os.path.join(tmp, "fixtures")
        os.mkdir(fix)
        cls.doc = stocks_support.example_document("AAPL")
        cls.overview = stocks_support.example_overview()
        for name, value in (("doc.json", cls.doc), ("overview.json", cls.overview)):
            with open(os.path.join(fix, name), "w", encoding="utf-8") as f:
                json.dump(value, f, allow_nan=False)
        harness = os.path.join(tmp, "harness.js")
        with open(harness, "w", encoding="utf-8") as f:
            f.write(STOCKS_HARNESS)
        res = subprocess.run(["node", harness, DOCS, os.path.join(tmp, "site"), fix],
                             capture_output=True, text=True, timeout=300)
        if res.returncode == 3:
            raise unittest.SkipTest("needs playwright with chromium")
        if res.returncode != 0:
            raise AssertionError("the stocks browser harness failed: " + res.stderr[-2000:])
        cls.got = json.loads(res.stdout)

    def test_a_visitor_sees_nothing_and_sends_nothing(self):
        self.assertEqual(self.got["visitor"], {"shown": [False, False, 0], "calls": 0, "errors": []})

    def test_the_key_holder_sees_the_market_tab_first(self):
        for scheme in ("dark", "light"):
            got = self.got[scheme]
            self.assertEqual(got["shown"], [True, True, 1], scheme)
            m = got["market"]
            self.assertEqual(m["tabs"], ["market", "AAPL", "EXMPL"])
            self.assertEqual(m["catalysts"], len(self.overview["calendar"]))
            self.assertEqual(m["tiles"], len(self.overview["macro"]))
            self.assertEqual(m["news"], len(self.overview["news"]))
            self.assertEqual(m["headline"], self.overview["summary"]["headline"])
            self.assertEqual(m["yours"], 2)
            self.assertEqual(m["disclaimer"], self.overview["disclaimer"])

    def test_a_ticker_tab_draws_the_chart_and_every_block(self):
        for scheme in ("dark", "light"):
            t = self.got[scheme]["ticker"]
            self.assertTrue(t["name"].startswith("AAPL"), scheme)
            self.assertEqual(t["candles"], 126)          # the default range: six months of daily candles
            self.assertEqual(t["lines"], 5)              # SMA 20, 50, 200, RSI and the short volume ratio
            self.assertEqual(t["guides"], 2)             # RSI 30 and 70
            self.assertEqual(t["cards"], 11)
            self.assertGreaterEqual(t["insiders"], 2)    # the Form 4 and the 13F links
            self.assertEqual(t["disclaimer"], self.doc["disclaimer"])
            self.assertEqual((t["styled"], t["wide"]), (0, False))

    def test_edits_send_exactly_the_contracts_requests(self):
        got = self.got["dark"]
        self.assertEqual(got["posts"], [
            '/stocks/settings {"model":"claude-haiku-5-5"}',
            '/stocks/tickers {"action":"add","ticker":"BRK-B"}',
            '/stocks/tickers {"action":"remove","ticker":"EXMPL"}',
            "/stocks/refresh {}",
        ])
        self.assertTrue(got["runDisabled"])
        self.assertTrue(got["allKeyed"])

    def test_nothing_is_stored_or_put_in_the_address(self):
        got = self.got["dark"]
        self.assertEqual(got["storage"], ["nd_worker_key_v1", "theme"])  # the key and the theme the harness set
        self.assertEqual(got["address"], "")

    def test_the_guards_401_forgets_the_key_and_empties_the_section(self):
        self.assertEqual(self.got["dark"]["after401"], {"shown": [False, False, 0], "key": None})

    def test_no_console_errors_or_policy_violations(self):
        for name in ("dark", "light", "phone"):
            self.assertEqual(self.got[name]["errors"], [], name)

    def test_a_phone_never_scrolls_sideways(self):
        self.assertEqual(self.got["phone"]["widths"], [360, 360])

    def test_a_long_list_takes_hit_rates_from_the_list_and_a_few_documents(self):
        got = self.got["longList"]
        self.assertEqual(got["docCalls"], 4)  # PREFETCH_MAX: the others load when their tab opens
        self.assertRegex(got["hits"][0], r"^\d+%(live|backtest), [\d,]+$")  # from AAPL's document
        self.assertEqual(got["hits"][1:], ["61%backtest, 120"] * 7)     # from the list, no document needed
        self.assertEqual(got["errors"], [])

    def test_the_focus_stays_in_your_stocks_while_a_document_comes_in(self):
        got = self.got["longList"]
        self.assertNotRegex(got["before"], r"%")  # the document was not in yet when the button took the focus
        self.assertEqual(got["focus"], "st-open, Open AAPL, Apple Inc.")

    def test_documents_that_cannot_be_fetched_leave_nothing_on_loading(self):
        got = self.got["failing"]
        self.assertEqual(got["hits"], ["n/a"] * 3)
        self.assertNotIn("Loading", got["tab"])
        self.assertIn("Could not reach the worker", got["tab"])
        self.assertIn("TRY AGAIN", got["tab"].upper())
        self.assertEqual(got["errors"], [])

    def test_an_answer_from_before_new_results_is_dropped(self):
        got = self.got["newRun"]
        self.assertEqual(got["name"], "Newer Example Inc.")
        self.assertEqual(got["msg"], "New results are in.")
        self.assertEqual(got["errors"], [])

    def test_a_bad_value_costs_one_card_at_most(self):
        got = self.got["badValues"]
        self.assertEqual(got["errors"], [])
        # an impossible day is not a day: the cell says n/a and every card is there
        self.assertEqual(len(got["ticker"]["cards"]), 11)
        self.assertFalse([c for c in got["ticker"]["cards"] if "not available" in c])
        self.assertEqual((got["texts"]["insiderDay"], got["texts"]["latest"]), ("n/a", "n/a"))
        # a timestamp no day format can print takes down its card only
        self.assertEqual(got["market"]["cards"], ["What matters this week", "Macro news (not available)", "Upcoming catalysts",
                                                  "Macro dashboard", "Your stocks"])
        for name in ("market", "ticker", "unknown"):
            self.assertEqual(got[name]["foot"][-1], self.doc["disclaimer"], name)

    def test_words_not_ordinals_and_plurals(self):
        texts = self.got["badValues"]["texts"]
        self.assertEqual(texts["percentile"], "Higher than 92 percent of the last 251 days")
        self.assertEqual(texts["funds"], "1 fund checked")

    def test_an_unknown_ticker_has_no_lines_about_an_assessment_or_a_missing_date(self):
        foot = self.got["badValues"]["unknown"]["foot"]
        self.assertEqual(self.got["badValues"]["unknown"]["cards"], ["Unknown ticker"])
        self.assertEqual(len(foot), 2)
        self.assertRegex(foot[0], r"^Generated [A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [AP]M\.$")

    # ---------------------------------------------------------------- the run on GitHub

    def test_a_run_that_is_going_shows_stop_and_one_click_stops_it(self):
        r = self.got["runs"]["going"]
        going = r["going"]
        self.assertEqual(going["lines"][-1], "A run is going. Started at " + r["started"] + " by hand.")
        self.assertTrue(going["runDisabled"])
        self.assertEqual(going["stop"], {"text": "Stop the run", "cls": "btn btn-sm", "disabled": False, "busy": None})
        self.assertEqual(going["buttons"], ["st-run", "st-stop", "st-settings"])
        # While the worker answers: Stopping, busy.
        self.assertEqual(r["busy"]["stop"], {"text": "Stopping", "cls": "btn btn-sm", "disabled": True, "busy": "true"})
        stopped = r["stopped"]
        self.assertEqual(stopped["msg"], "Stopped. Run analysis now is ready once GitHub has stopped it, usually within a minute.")
        self.assertEqual(stopped["lines"][-1], "The run is stopping. GitHub usually takes under a minute.")
        self.assertIsNone(stopped["stop"])
        self.assertTrue(stopped["runDisabled"])  # not before GitHub says the run is over
        self.assertEqual(stopped["focus"], "st-msg")  # the clicked button is gone
        after = r["after"]
        self.assertFalse(after["runDisabled"])
        self.assertIsNone(after["stop"])
        self.assertEqual(after["lines"], ["The last run was stopped at " + r["stoppedAt"] + "."])
        self.assertEqual((r["stops"], r["refreshes"]), (1, 0))  # exactly one POST /stocks/stop
        self.assertGreater(r["readsAfter"], r["readsBefore"])  # it looked again before it freed the button
        self.assertEqual(r["errors"], [])

    def test_a_run_that_ended_with_an_error_links_to_github(self):
        runs = self.got["runs"]
        failed = runs["failed"]
        self.assertEqual(failed["lines"], ["The last run stopped with an error at " + failed["at"] + ". See what happened on GitHub"])
        self.assertEqual(failed["link"], {"href": "https://github.com/owner/news-digest/actions/runs/502",
                                          "text": "See what happened on GitHub", "target": "_blank", "rel": "noopener noreferrer"})
        # The claim of the old 30 minute rule is still on, but the run is over: the button is free.
        self.assertFalse(failed["runDisabled"])
        self.assertIsNone(failed["stop"])
        self.assertEqual(runs["timedOut"]["lines"][0],
                         "The last run stopped with an error at " + runs["timedOut"]["at"] + ". See what happened on GitHub")
        # Anything but a run page on github.com over https is no link.
        for label in ("evilHost", "lookalike", "query", "plainHttp"):
            got = runs[label]
            self.assertIsNone(got["link"], label)
            self.assertEqual(got["lines"], ["The last run stopped with an error at " + got["at"] + "."], label)

    def test_a_stopped_run_and_a_finished_run(self):
        runs = self.got["runs"]
        self.assertEqual(runs["cancelled"]["lines"], ["The last run was stopped at " + runs["cancelled"]["at"] + "."])
        self.assertRegex(runs["succeeded"]["lines"][0], r"^Last run [A-Z][a-z]{2} \d{1,2}, ")  # the overview's line, as before
        for label in ("cancelled", "succeeded"):
            self.assertFalse(runs[label]["runDisabled"], label)
            self.assertIsNone(runs[label]["stop"], label)
            self.assertIsNone(runs[label]["link"], label)

    def test_inside_the_start_grace_the_button_waits(self):
        grace = self.got["runs"]["grace"]
        self.assertEqual(grace["lines"][-1], "Run analysis now is available again at " + grace["at"] + ".")
        self.assertTrue(grace["runDisabled"])
        self.assertIsNone(grace["stop"])

    def test_the_page_stops_waiting_when_the_run_it_started_fails(self):
        follow = self.got["runs"]["follow"]
        self.assertIn("Waiting for the results of the run you started. This page checks every 2 minutes while it is open.",
                      follow["waiting"]["lines"])
        self.assertTrue(follow["waiting"]["runDisabled"])
        ended = follow["ended"]
        self.assertTrue(ended["lines"][0].startswith("The last run stopped with an error at "), ended["lines"])
        self.assertFalse([line for line in ended["lines"] if line.startswith("Waiting")])
        self.assertEqual(ended["msg"], "")  # "Started. Results in about 15 minutes." is gone
        self.assertFalse(ended["runDisabled"])
        self.assertEqual(follow["overviewAfter"], 0)  # and no more looks for results
        self.assertEqual(follow["errors"], [])

    def test_run_analysis_now_while_a_run_is_going_offers_stop(self):
        busy = self.got["runs"]["busy409"]
        self.assertEqual(busy["msg"], "A run is already going. Stop it first, or wait for its results.")
        self.assertTrue(busy["lines"][-1].startswith("A run is going. Started at "), busy["lines"])
        self.assertTrue(busy["lines"][-1].endswith(" on schedule."), busy["lines"])
        self.assertTrue(busy["runDisabled"])
        self.assertEqual(busy["stop"]["text"], "Stop the run")
        self.assertEqual(busy["focus"], "st-msg")

    def test_without_githubs_view_the_page_works_as_before(self):
        runs = self.got["runs"]
        self.assertIn("Could not check GitHub for a run just now.", runs["unread"]["lines"])
        for label in ("unread", "notSetUp", "oldWorker"):
            got = runs[label]
            self.assertRegex(got["lines"][-1], r"^Run analysis now is available again at \d{1,2}:\d{2}\s[AP]M\.$", label)
            self.assertTrue(got["runDisabled"], label)
            self.assertIsNone(got["stop"], label)
            if label != "unread":
                self.assertFalse([line for line in got["lines"] if "GitHub" in line], label)

    def test_the_run_states_raise_no_errors_and_fit_a_phone(self):
        runs = self.got["runs"]
        for label, got in runs.items():
            self.assertEqual(got["errors"], [], label)
        for got in (runs["going"]["going"], runs["going"]["stopped"], runs["going"]["after"], runs["failed"],
                    runs["awaited"]["later"], runs["blipManual"]["blip"], runs["blipScheduled"]["blip"],
                    runs["savedThenFailed"], runs["neverStops"]["notYet"]):
            self.assertEqual((got["wide"], got["styled"]), (390, 0))

    def test_a_start_waits_for_its_own_run_not_the_one_before(self):
        r = self.got["runs"]["awaited"]
        # Before the click, the run before is the newest run: it stopped at startup.
        self.assertTrue(r["before"]["lines"][0].startswith("The last run stopped with an error at "), r["before"]["lines"])
        self.assertFalse(r["before"]["runDisabled"])
        waiting = "Waiting for the results of the run you started. This page checks every 2 minutes while it is open."
        for label in ("started", "later"):
            got = r[label]
            # The worker still names the run before as the newest run; the page waits for its own.
            self.assertEqual(got["lines"][-1], waiting, label)
            self.assertRegex(got["lines"][0], r"^Last run [A-Z][a-z]{2} \d{1,2}, ", label)
            self.assertFalse([line for line in got["lines"] if "error" in line or "stopped" in line], label)
            self.assertTrue(got["runDisabled"], label)
            self.assertIsNone(got["stop"], label)
            self.assertEqual(got["msgLines"][0], "Started. Results in about 15 minutes.", label)
        self.assertGreaterEqual(r["looks"], 3)  # it kept looking
        self.assertEqual(r["refreshes"], 1)  # and a second click started nothing

    def test_one_unread_answer_does_not_end_the_following(self):
        for label in ("blipScheduled", "blipManual"):
            r = self.got["runs"][label]
            for state in ("going", "blip", "back"):
                got = r[state]
                self.assertTrue(got["runDisabled"], (label, state))
                self.assertEqual((got["stop"] or {}).get("text"), "Stop the run", (label, state))
                self.assertTrue([line for line in got["lines"] if line.startswith("A run is going.")], (label, state))
            self.assertIn("Could not check GitHub for a run just now.", r["blip"]["lines"])
            self.assertNotIn("Could not check GitHub for a run just now.", r["back"]["lines"])
            # The next looks came, and the page saw the run end.
            self.assertGreaterEqual(r["reads"], 4, label)
            ended = r["ended"]
            self.assertTrue(ended["lines"][0].startswith("The last run stopped with an error at "), (label, ended["lines"]))
            self.assertFalse([line for line in ended["lines"] if line.startswith("Waiting")], label)
            self.assertFalse(ended["runDisabled"], label)
            self.assertIsNone(ended["stop"], label)
        self.assertEqual(self.got["runs"]["blipManual"]["ended"]["msg"], "")  # "Started." is gone with the waiting

    def test_a_page_without_githubs_view_looks_again_while_it_waits(self):
        r = self.got["runs"]["unreadFirst"]
        self.assertIn("Could not check GitHub for a run just now.", r["before"]["lines"])
        self.assertIsNone(r["before"]["stop"])
        after = r["after"]
        self.assertEqual((after["stop"] or {}).get("text"), "Stop the run")
        self.assertTrue([line for line in after["lines"] if line.startswith("A run is going. Started at ")], after["lines"])
        self.assertNotIn("Could not check GitHub for a run just now.", after["lines"])
        self.assertTrue(after["runDisabled"])

    def test_a_run_that_saved_its_results_then_failed_keeps_them(self):
        got = self.got["runs"]["savedThenFailed"]
        self.assertEqual(got["msg"], "New results are in.")
        self.assertGreaterEqual(got["lists"], 1)  # the section loaded again
        self.assertRegex(got["lines"][0], r"^Last run [A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [AP]M, started by hand\. ")
        self.assertEqual(got["lines"][1], "The last run stopped with an error at " + got["at"] + ". See what happened on GitHub")
        self.assertEqual(len(got["lines"]), 2)  # no waiting line
        self.assertFalse(got["runDisabled"])

    def test_a_stop_that_does_not_take_says_so(self):
        r = self.got["runs"]["neverStops"]
        self.assertEqual(r["stopped"]["msgLines"], ["Stopped. Run analysis now is ready once GitHub has stopped it, usually within a minute."])
        for label in ("notYet", "later"):
            got = r[label]
            self.assertEqual(got["msgLines"], ["GitHub has not stopped the run yet.", "Try Stop the run again."], label)
            self.assertEqual(got["stop"], {"text": "Stop the run", "cls": "btn btn-sm", "disabled": False, "busy": None}, label)
            self.assertTrue(got["runDisabled"], label)
        self.assertEqual(r["stops"], 1)
        # When GitHub does stop it later, the warning goes.
        self.assertEqual((r["ended"]["msg"], r["ended"]["stop"], r["ended"]["runDisabled"]), ("", None, False))
        self.assertTrue(r["ended"]["lines"][0].startswith("The last run was stopped at "), r["ended"]["lines"])

    def test_the_cost_estimate_rounds_and_stops_at_the_spend_cap(self):
        cost = self.got["cost"]
        self.assertEqual(cost["opus30"]["options"], ["Opus 5.5, about $3.00 a day, capped",
                                                     "Sonnet 5.5, about $1.83 to $2.75 a day", "Haiku 5.5, about $0.09 to $0.14 a day"])
        self.assertEqual(cost["opus30"]["hints"][:2], [
            "Rough cost for 30 stocks and the market summary on each trading day, from typical token counts: about $63 a month with Opus 5.5.",
            "A run stops asking the model at its $3.00 spend cap, which fits about 16 stocks with Opus 5.5. The others get levels from the code."])
        # cents under $10 a month, and less than a cent said so
        self.assertEqual(cost["haiku1"]["options"][2], "Haiku 5.5, less than $0.01 a day")
        self.assertEqual(cost["haiku1"]["hints"][:2], [
            "Rough cost for 1 stock and the market summary on each trading day, from typical token counts: about $0.10 to $0.15 a month with Haiku 5.5.",
            "A run stops asking the model at its $3.00 spend cap, which fits a full list of 30 stocks with Haiku 5.5."])
        # the worker's own cap wins
        self.assertEqual(cost["opus30cap"]["options"][:2], ["Opus 5.5, about $1.50 a day, capped",
                                                            "Sonnet 5.5, about $1.50 a day, capped"])
        self.assertEqual(cost["opus30cap"]["hints"][1],
                         "A run stops asking the model at its $1.50 spend cap, which fits about 7 stocks with Opus 5.5. The others get levels from the code.")
        for label, got in cost.items():
            self.assertEqual(got["errors"], [], label)
            for text in got["options"] + got["hints"]:
                self.assertNotRegex(text, r"\$0(\.00)?(?![.\d])|about less", label)  # never "$0 to $0" or "$0.00"


if __name__ == "__main__":
    unittest.main()
