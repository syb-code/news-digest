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
                "/stocks/refresh"}
LINK_HOSTS = {"sec.gov", "house.gov", "federalreserve.gov", "bls.gov", "bea.gov", "finra.org", "senate.gov"}


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
const doc = JSON.parse(fs.readFileSync(path.join(fixDir, 'doc.json'), 'utf8'));
const overview = JSON.parse(fs.readFileSync(path.join(fixDir, 'overview.json'), 'utf8'));
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };

function worker() {
  const w = { tickers: ['AAPL', 'EXMPL'], model: 'claude-sonnet-5-5', refreshed: false, calls: [] };
  w.handle = (method, url, headers, body) => {
    const p = new URL(url).pathname;
    w.calls.push([method, p, body || '', headers.authorization === 'Bearer ' + KEY]);
    const json = (status, b, extra) => ({ status, headers: Object.assign({ 'content-type': 'application/json' }, extra || {}), body: JSON.stringify(b) });
    if (headers.authorization !== 'Bearer ' + KEY) return json(401, { error: 'unauthorized' }, { 'x-guard': 'deny' });
    if (p === '/stocks/list') return json(200, { max: 30,
      tickers: w.tickers.map((t) => (t === 'AAPL' ? { ticker: t, status: doc.status, name: doc.header.name, close: doc.header.last_close, change_pct: doc.header.change_pct, stance: doc.assessment.stance, confidence: doc.assessment.confidence, data_as_of: doc.data_as_of, generated_at: doc.generated_at } : { ticker: t, status: 'pending' })),
      settings: { model: w.model, default_model: 'claude-sonnet-5-5', allowed_models: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5'] },
      refresh: { min_minutes: 30, requested_at: null, available_at: null }, overview: { generated_at: overview.generated_at } });
    if (p === '/stocks/overview') return json(200, overview);
    if (p === '/stocks/t/AAPL') return json(200, doc);
    if (p.startsWith('/stocks/t/')) return json(404, { error: 'no analysis yet' });
    if (p === '/stocks/settings') { w.model = JSON.parse(body).model; return json(200, { ok: true, model: w.model }); }
    if (p === '/stocks/tickers') {
      const b = JSON.parse(body);
      w.tickers = b.action === 'add' ? w.tickers.concat([b.ticker]) : w.tickers.filter((t) => t !== b.ticker);
      return json(200, { ok: true, changed: true, tickers: w.tickers });
    }
    if (p === '/stocks/refresh') return json(202, { ok: true, requested_at: new Date().toISOString(), available_at: new Date(Date.now() + 1800000).toISOString() });
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
  let browser;
  try { browser = await pw.chromium.launch(); } catch (e) { srv.close(); process.exit(3); }
  const out = {};
  const open = async (w, key, scheme, width) => {
    const ctx = await browser.newContext({ viewport: { width: width || 1280, height: 900 }, colorScheme: scheme || 'dark', reducedMotion: 'reduce' });
    const page = await ctx.newPage();
    const errors = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
    page.on('pageerror', (e) => errors.push(e.message));
    await page.addInitScript(() => document.addEventListener('securitypolicyviolation', (e) => console.error('CSP violation ' + e.violatedDirective)));
    if (key) await page.addInitScript((k) => localStorage.setItem('nd_worker_key_v1', k), key);
    if (scheme) await page.addInitScript((t) => localStorage.setItem('theme', t), scheme);
    await page.route(ORIGIN + '/**', async (route) => {
      const q = route.request();
      const r = w.handle(q.method(), q.url(), q.headers(), q.postData());
      await route.fulfill({ status: r.status, body: r.body, headers: Object.assign({ 'access-control-allow-origin': '*', 'access-control-expose-headers': 'x-guard', 'access-control-allow-headers': 'authorization, content-type' }, r.headers) });
    });
    await page.goto(base);
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
                             capture_output=True, text=True, timeout=180)
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


if __name__ == "__main__":
    unittest.main()
