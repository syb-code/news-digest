"""Keeping private values out of the public log (contract section 10.7).

The repository, the Actions logs and the artifacts are public. The ticker
list, the company names, CIKs and CUSIPs that follow from it, and every number
of the analysis are private. The log lines of the stock run are built to hold
none of them: counts, durations, model ids, token counts, dollar costs, source
names and a ticker's position ("ticker 3 of 8"). This module is the backstop
behind that rule:

  Redactor        registers each private value with GitHub ("::add-mask::"),
                  so the runner hides it if it ever reaches the log, and
                  replaces it with "***" in every line this run prints;
  error_name      the type of an exception, never its message;
  install_excepthook
                  an uncaught exception prints "Stopped: <TypeName>" and
                  nothing else (no traceback, which can quote values);
  private_dir     the private working directory, created 0700, refused when
                  it is inside the repository;
  find_private    the private values a text holds as whole words (tests).
"""
import os
import re
import sys
import threading
import warnings

import stocks

MASK_PREFIX = "::add-mask::"
_CIK_RE = re.compile(r"^0*(\d{1,10})$")


def _escape_command_data(value):
    """A value as the data of a workflow command: the runner unescapes %25,
    %0D and %0A, so a literal percent sign must be written as %25."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _spellings(value):
    """Every spelling of one private value that is worth masking."""
    s = str(value).strip()
    if not s:
        return []
    out = [s]
    m = _CIK_RE.match(s)
    if m and isinstance(value, (int, str)) and not isinstance(value, bool):
        plain_cik = str(int(m.group(1)))
        out += [plain_cik, plain_cik.zfill(10)]
    ticker = stocks.normalize_ticker(s)
    if ticker is not None and ticker == s:
        out += [ticker.replace("-", "."), ticker.replace("-", "/")]
    seen, kept = set(), []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            kept.append(v)
    return kept


def _word_pattern(value):
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])")


class Redactor:
    """Masks private values for the rest of the job and redacts them here."""

    def __init__(self, out=None):
        self._out = out  # a file-like object for the mask lines; sys.stdout when None
        self._values = []
        self._known = set()
        self._patterns = []
        self._lock = threading.Lock()

    def __len__(self):
        return len(self._values)

    def register(self, *values):
        """Print "::add-mask::<v>" once for each new value (a ticker also in its
        "." and "/" spellings, a CIK plain and padded to 10 digits). Lists and
        tuples are walked; None and empty values are ignored. Multi-line values
        are masked line by line, as GitHub requires."""
        flat = []
        for v in values:
            if isinstance(v, (list, tuple, set, frozenset)):
                flat.extend(v)
            else:
                flat.append(v)
        new = []
        with self._lock:
            for v in flat:
                if v is None or isinstance(v, bool):
                    continue
                for line in str(v).splitlines() or [""]:
                    for s in _spellings(line if not isinstance(v, int) else v):
                        if s in self._known:
                            continue
                        self._known.add(s)
                        self._values.append(s)
                        new.append(s)
            if new:
                self._patterns = [_word_pattern(v) for v in sorted(self._values, key=len, reverse=True)]
        out = self._out or sys.stdout
        for s in new:
            out.write(MASK_PREFIX + _escape_command_data(s) + "\n")
        if new:
            out.flush()

    def redact(self, text):
        """text with every registered value (as a whole word) replaced by ***."""
        s = str(text)
        for pattern in self._patterns:
            s = pattern.sub("***", s)
        return s

    def log(self, message):
        stocks.log(self.redact(message))

    def warn(self, message):
        stocks.warn(self.redact(message))


def error_name(e):
    """The type of an exception, never its message (which can quote a value)."""
    return type(e).__name__


def install_excepthook(redactor):
    """An uncaught exception, in the main thread or a helper thread, prints one
    plain line with its type and nothing else. Python warnings print their
    category only."""

    def hook(exc_type, exc, tb):
        stocks.log(redactor.redact(f"Stopped: {getattr(exc_type, '__name__', 'Exception')}"))

    def thread_hook(args):
        stocks.log(redactor.redact(f"Stopped: {getattr(args.exc_type, '__name__', 'Exception')}"))

    def show_warning(message, category, filename, lineno, file=None, line=None):
        stocks.log(redactor.redact(f"Python warning: {getattr(category, '__name__', 'Warning')}"))

    sys.excepthook = hook
    threading.excepthook = thread_hook
    warnings.showwarning = show_warning


def _inside(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives
        return False


def private_dir(base, repo_roots=None):
    """base/private, created with mode 0700, as an absolute path. Refuses
    (StocksError) a base inside the repository or $GITHUB_WORKSPACE, and a
    private folder that is a symbolic link."""
    if not base or not str(base).strip():
        raise stocks.StocksError("No working directory was given.")
    real = os.path.realpath(os.path.abspath(str(base)))
    roots = list(repo_roots) if repo_roots is not None else [stocks.ROOT, os.environ.get("GITHUB_WORKSPACE", "")]
    for root in roots:
        if root and _inside(real, os.path.realpath(root)):
            raise stocks.StocksError("The working directory must be outside the repository.")
    os.makedirs(real, mode=0o700, exist_ok=True)
    path = os.path.join(real, "private")
    if os.path.islink(path):
        raise stocks.StocksError("The private folder must not be a link.")
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def find_private(text, values, ignore_case=False):
    """The values that text holds as whole words, in the order given."""
    s = str(text or "")
    flags = re.IGNORECASE if ignore_case else 0
    found = []
    for v in values:
        v = str(v or "").strip()
        if v and v not in found and re.search(r"(?<![A-Za-z0-9])" + re.escape(v) + r"(?![A-Za-z0-9])", s, flags):
            found.append(v)
    return found
