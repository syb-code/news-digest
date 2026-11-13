"""Read config.yml, the one settings file of the news digest.

    from config import load_config
    cfg = load_config()          # config.yml at the repository root
    cfg["feeds"]["rss"]          # always present, a list
    cfg["learn"]["effort"]       # always present, checked
    cfg["learn"]["prices"]       # {model id: {"input": 4.0, "output": 20.0}}, dollars per million tokens
    cfg["stocks"]["models"]      # the three models the private stock run may use, with their prices

load_config fills in a default for every key that is missing or empty, and
raises ConfigError, naming the key, for a value of the wrong kind. A key it
does not know is kept as it is, so a section added later needs no change here.

    load_config(check=("feeds", "themes"))

checks only the sections named, so a mistake in the learn section cannot stop
the feeds from being fetched; a section not checked is still filled in when it
is missing, and otherwise kept as written.

PyYAML is imported only when a file is read, so importing this module costs
nothing in a job that does not read the file.
"""
import copy
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.yml")

SECTIONS = ("timezone", "schedule", "feeds", "themes", "learn", "stocks")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
FEED_LISTS = ("youtube", "rss", "websites", "twitter_embeds", "nitter_instances")
LEARN_INTS = ("max_output_tokens", "max_input_chars", "topics_min", "topics_max", "lookback_hours")
HHMM_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
FALLBACKS_OFF = ("none", "off", "false", "")
PRICE_KEYS = ("input", "output")
STOCKS_INTS = ("max_output_tokens", "overview_max_output_tokens", "max_input_chars", "parallel_requests",
               "bootstrap_years", "min_train_rows", "refit_every", "lessons_cap", "chart_days", "insider_days",
               "congress_days")
STOCKS_TRANSFORMS = ("level", "yoy", "diff", "range", "thousands")
STOCKS_QUOTES = ("usd_per_unit", "units_per_usd")
SERIES_ID_RE = re.compile(r"^[A-Z0-9]{2,30}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
MACRO_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
TICKERS_PRIVATE = "The ticker list is private: keep it in the Worker, not in config.yml."

DEFAULTS = {
    "timezone": "America/Los_Angeles",
    "schedule": {"morning": "05:45", "noon": "12:00"},
    "feeds": {name: [] for name in FEED_LISTS},
    "themes": {},
    "learn": {
        "model": "claude-opus-5-5",
        "effort": "medium",
        "max_output_tokens": 12000,
        "max_input_chars": 40000,
        "topics_min": 3,
        "topics_max": 5,
        "lookback_hours": 36,
        "price_per_mtok": {"input": 4.0, "output": 20.0},
        # By model id, for a run that a fallback model answers. A model listed
        # here is priced from here; price_per_mtok is for any other model.
        "prices": {
            "claude-opus-5-5": {"input": 4.0, "output": 20.0},
            "claude-opus-5": {"input": 5.0, "output": 25.0},
            "claude-opus-4-8": {"input": 5.0, "output": 25.0},
        },
        "fallbacks": "default",
    },
    # The private stock section (scripts/stocks/). No ticker is ever listed
    # here: the list lives in the Cloudflare Worker's KV namespace.
    "stocks": {
        "model": "claude-sonnet-5-5",
        "effort": "medium",
        "max_output_tokens": 10000,
        "overview_max_output_tokens": 8000,
        "max_input_chars": 80000,
        "max_run_usd": 3.0,
        "parallel_requests": 4,
        "models": {
            "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "fallbacks": "default"},
            "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10, "fallbacks": "default"},
            "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "fallbacks": "none"},
        },
        "fallback_prices": {
            "claude-opus-5": {"input": 5.0, "output": 25.0},
            "claude-opus-4-8": {"input": 5.0, "output": 25.0},
            "claude-sonnet-5": {"input": 2.0, "output": 10.0},
        },
        "horizons": [5, 20],
        "bootstrap_years": 5,
        "min_train_rows": 150,
        "refit_every": 21,
        "lessons_cap": 12,
        "chart_days": 400,
        "insider_days": 365,
        "congress_days": 365,
        "funds": [
            {"name": "Berkshire Hathaway", "cik": 1067983},
            {"name": "Pershing Square", "cik": 2026053},
            {"name": "ARK Investment Management", "cik": 1697748},
            {"name": "Bridgewater Associates", "cik": 1350694},
            {"name": "Renaissance Technologies", "cik": 1037389},
            {"name": "Citadel Advisors", "cik": 1423053},
            {"name": "Duquesne Family Office", "cik": 1536411},
            {"name": "Appaloosa", "cik": 1656456},
            {"name": "Soros Fund Management", "cik": 1029160},
            {"name": "Tiger Global Management", "cik": 1167483},
            {"name": "Coatue Management", "cik": 1135730},
            {"name": "Third Point", "cik": 1040273},
            {"name": "Viking Global Investors", "cik": 1103804},
            {"name": "D. E. Shaw", "cik": 1009207},
            {"name": "Lone Pine Capital", "cik": 1061165},
            {"name": "Elliott Investment Management", "cik": 1791786},
        ],
        "macro": [
            {"id": "fed_funds", "label": "Fed funds target range", "series": ["DFEDTARL", "DFEDTARU"],
             "transform": "range", "unit": "percent"},
            {"id": "dgs2", "label": "2 year Treasury yield", "series": ["DGS2"], "transform": "level", "unit": "percent"},
            {"id": "dgs10", "label": "10 year Treasury yield", "series": ["DGS10"], "transform": "level",
             "unit": "percent"},
            {"id": "t10y2y", "label": "10 year minus 2 year", "series": ["T10Y2Y"], "transform": "level",
             "unit": "points"},
            {"id": "cpi_yoy", "label": "CPI, year over year", "series": ["CPIAUCSL"], "transform": "yoy",
             "unit": "percent"},
            {"id": "core_cpi_yoy", "label": "Core CPI, year over year", "series": ["CPILFESL"], "transform": "yoy",
             "unit": "percent"},
            {"id": "core_pce_yoy", "label": "Core PCE, year over year", "series": ["PCEPILFE"], "transform": "yoy",
             "unit": "percent"},
            {"id": "unrate", "label": "Unemployment rate", "series": ["UNRATE"], "transform": "level",
             "unit": "percent"},
            {"id": "payrolls", "label": "Payrolls, change", "series": ["PAYEMS"], "transform": "diff",
             "unit": "thousands"},
            {"id": "gdp", "label": "Real GDP growth, annualized", "series": ["A191RL1Q225SBEA"], "transform": "level",
             "unit": "percent"},
            {"id": "vix", "label": "VIX", "series": ["VIXCLS"], "transform": "level", "unit": "index"},
            {"id": "dollar", "label": "Dollar index (broad)", "series": ["DTWEXBGS"], "transform": "level",
             "unit": "index"},
            {"id": "wti", "label": "WTI oil", "series": ["DCOILWTICO"], "transform": "level", "unit": "dollars"},
            {"id": "hy_spread", "label": "High yield spread", "series": ["BAMLH0A0HYM2"], "transform": "level",
             "unit": "percent"},
            {"id": "breakeven_5y", "label": "5 year breakeven inflation", "series": ["T5YIE"], "transform": "level",
             "unit": "percent"},
            {"id": "claims", "label": "Initial jobless claims", "series": ["ICSA"], "transform": "thousands",
             "unit": "thousands"},
        ],
        "fx": {
            "EUR": {"series": "DEXUSEU", "quote": "usd_per_unit"},
            "GBP": {"series": "DEXUSUK", "quote": "usd_per_unit"},
            "JPY": {"series": "DEXJPUS", "quote": "units_per_usd"},
            "CAD": {"series": "DEXCAUS", "quote": "units_per_usd"},
            "CHF": {"series": "DEXSZUS", "quote": "units_per_usd"},
        },
        "trading_days": {"data_ready": "18:00"},
    },
}


class ConfigError(ValueError):
    """config.yml is missing, unreadable, or holds a value of the wrong kind."""


def load_config(path=None, check=SECTIONS):
    """The settings in path (default: config.yml at the repository root) as a
    dict, with every default filled in. Raises ConfigError for a section named
    in check."""
    path = path or CONFIG_PATH
    name = os.path.basename(path)
    import yaml  # here, not at the top: see the module docstring

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(f"{name} not found at {path}") from None
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"{name} could not be read: {e}") from None
    return normalize(raw, name, check)


def normalize(raw, name="config.yml", check=SECTIONS):
    """raw (a parsed file) checked and completed with DEFAULTS."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{name}: the top level must be a mapping of settings")
    cfg = copy.deepcopy(raw)
    checkers = {"timezone": _timezone, "schedule": _schedule, "feeds": _feeds, "themes": _themes, "learn": _learn,
                "stocks": _stocks}
    for section, checker in checkers.items():
        if section in check:
            cfg[section] = checker(raw.get(section), name)
        elif raw.get(section) is None:
            cfg[section] = copy.deepcopy(DEFAULTS[section])
    return cfg


def _mapping(value, key, name):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name}: {key} must be a mapping")
    return copy.deepcopy(value)


def _timezone(value, name):
    if value is None or value == "":
        return DEFAULTS["timezone"]
    if not isinstance(value, str):
        raise ConfigError(f"{name}: timezone must be a name such as America/Los_Angeles")
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"{name}: timezone {value!r} is not a known time zone") from None
    return value


def _hhmm(value, key, name):
    # Unquoted 05:45 is read by YAML 1.1 as the base-60 number 345.
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 24 * 60:
        value = f"{value // 60:02d}:{value % 60:02d}"
    if not isinstance(value, str) or not HHMM_RE.fullmatch(value.strip()):
        raise ConfigError(f'{name}: schedule.{key} must be a time such as "05:45"')
    return value.strip()


def _schedule(value, name):
    out = _mapping(value, "schedule", name)
    for key, default in DEFAULTS["schedule"].items():
        out[key] = default if out.get(key) in (None, "") else _hhmm(out[key], key, name)
    return out


def _feeds(value, name):
    out = _mapping(value, "feeds", name)
    for key in FEED_LISTS:
        items = out.get(key)
        if items is None:
            items = []
        elif key == "nitter_instances" and isinstance(items, str):
            items = [items]
        if not isinstance(items, list):
            raise ConfigError(f"{name}: feeds.{key} must be a list")
        out[key] = items
    return out


def _themes(value, name):
    out = _mapping(value, "themes", name)
    for theme, keywords in out.items():
        if keywords is None:
            keywords = []
        elif isinstance(keywords, str):
            keywords = [keywords]
        if not isinstance(keywords, list) or not all(isinstance(k, (str, int, float)) for k in keywords):
            raise ConfigError(f"{name}: themes.{theme} must be a list of keywords")
        out[theme] = [str(k) for k in keywords]
    return {str(k): v for k, v in out.items()}


def _positive_int(value, key, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{name}: learn.{key} must be a whole number of 1 or more")
    return value


def _price(value, key, name):
    """value as dollars per million tokens; key is the setting's full name."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"{name}: {key} must be a number of 0 or more")
    return float(value)


def _prices(value, name):
    """learn.prices: {model id: {"input": dollars, "output": dollars}} per
    million tokens. Both prices are required for a model that is listed; any
    other key of an entry (cache_read, say) must be a price too."""
    out = {}
    for model, entry in _mapping(value, "learn.prices", name).items():
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(f"{name}: learn.prices must be keyed by model id, not {model!r}")
        model = model.strip()
        entry = _mapping(entry, f"learn.prices.{model}", name)
        for key in PRICE_KEYS:
            if entry.get(key) is None:
                raise ConfigError(f"{name}: learn.prices.{model}.{key} is missing")
        out[model] = {str(key): _price(price, f"learn.prices.{model}.{key}", name) for key, price in entry.items()}
    return out


def _fallbacks(value, name):
    if value is None or value is False:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value.lower() in FALLBACKS_OFF:
            return None
        if value == "default":
            return "default"
    if isinstance(value, list) and value and all(isinstance(m, str) and m.strip() for m in value):
        return [m.strip() for m in value]
    raise ConfigError(f'{name}: learn.fallbacks must be "default", "none" or a list of model ids')


def _learn(value, name):
    out = _mapping(value, "learn", name)
    defaults = DEFAULTS["learn"]
    for key, default in defaults.items():
        if out.get(key) is None and key != "fallbacks":
            out[key] = copy.deepcopy(default)
    if "fallbacks" not in out:
        out["fallbacks"] = defaults["fallbacks"]

    if not isinstance(out["model"], str) or not out["model"].strip():
        raise ConfigError(f"{name}: learn.model must be a model id such as {defaults['model']}")
    out["model"] = out["model"].strip()
    if out["effort"] not in EFFORTS:
        raise ConfigError(f"{name}: learn.effort must be one of {', '.join(EFFORTS)}")
    for key in LEARN_INTS:
        out[key] = _positive_int(out[key], key, name)
    if out["topics_min"] > out["topics_max"]:
        raise ConfigError(f"{name}: learn.topics_min must not be more than learn.topics_max")

    prices = _mapping(out["price_per_mtok"], "learn.price_per_mtok", name)
    for key, default in defaults["price_per_mtok"].items():
        prices[key] = default if prices.get(key) is None else _price(prices[key], f"learn.price_per_mtok.{key}", name)
    out["price_per_mtok"] = prices
    # Filled in above when the key is missing; an empty mapping in the file
    # stays empty, so price_per_mtok is then the price of every model.
    out["prices"] = _prices(out["prices"], name)
    out["fallbacks"] = _fallbacks(out["fallbacks"], name)
    return out


# ---------------------------------------------------------------- stocks

def _has_key(node, key):
    """True when a mapping anywhere inside node has the key."""
    if isinstance(node, dict):
        return any(k == key or _has_key(v, key) for k, v in node.items())
    if isinstance(node, list):
        return any(_has_key(v, key) for v in node)
    return False


def _stocks_int(value, key, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{name}: stocks.{key} must be a whole number of 1 or more")
    return value


def _stocks_models(value, name, defaults):
    """stocks.models: exactly the allowed models, each with input, output and
    cache_read prices and fallbacks "default" or "none" (always "none" for
    Haiku: it has no server-side refusal fallback)."""
    from stocks import ALLOWED_MODELS  # the package next to this file; stdlib only

    given = _mapping(value, "stocks.models", name)
    out = {}
    for model in given:
        if not isinstance(model, str) or model.strip() not in ALLOWED_MODELS:
            raise ConfigError(f"{name}: stocks.models may list only {', '.join(ALLOWED_MODELS)}, not {model!r}")
    given = {str(k).strip(): v for k, v in given.items()}
    for model in ALLOWED_MODELS:
        entry = copy.deepcopy(defaults[model])
        entry.update(_mapping(given.get(model), f"stocks.models.{model}", name))
        clean = {}
        for key in ("input", "output", "cache_read"):
            if entry.get(key) is None:
                raise ConfigError(f"{name}: stocks.models.{model}.{key} is missing")
            clean[key] = _price(entry[key], f"stocks.models.{model}.{key}", name)
        fb = entry.get("fallbacks")
        fb = "none" if fb is None or fb is False else str(fb).strip().lower()
        if fb not in ("default", "none"):
            raise ConfigError(f'{name}: stocks.models.{model}.fallbacks must be "default" or "none"')
        clean["fallbacks"] = "none" if "haiku" in model else fb
        out[model] = clean
    return out


def _stocks_prices(value, name):
    out = {}
    for model, entry in _mapping(value, "stocks.fallback_prices", name).items():
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(f"{name}: stocks.fallback_prices must be keyed by model id, not {model!r}")
        model = model.strip()
        entry = _mapping(entry, f"stocks.fallback_prices.{model}", name)
        for key in PRICE_KEYS:
            if entry.get(key) is None:
                raise ConfigError(f"{name}: stocks.fallback_prices.{model}.{key} is missing")
        out[model] = {str(k): _price(v, f"stocks.fallback_prices.{model}.{k}", name) for k, v in entry.items()}
    return out


def _stocks_funds(value, name):
    if not isinstance(value, list):
        raise ConfigError(f"{name}: stocks.funds must be a list of funds with a name and a cik")
    out, ciks = [], set()
    for i, fund in enumerate(value):
        if not isinstance(fund, dict) or not isinstance(fund.get("name"), str) or not fund["name"].strip():
            raise ConfigError(f"{name}: stocks.funds[{i}] needs a name")
        cik = fund.get("cik")
        if isinstance(cik, bool) or not isinstance(cik, int) or cik < 1:
            raise ConfigError(f"{name}: stocks.funds[{i}].cik must be a positive whole number")
        if cik in ciks:
            raise ConfigError(f"{name}: stocks.funds lists the cik {cik} twice")
        ciks.add(cik)
        out.append({"name": " ".join(fund["name"].split()), "cik": cik})
    return out


def _stocks_macro(value, name):
    if not isinstance(value, list):
        raise ConfigError(f"{name}: stocks.macro must be a list of rows")
    out, ids = [], set()
    for i, row in enumerate(value):
        if not isinstance(row, dict):
            raise ConfigError(f"{name}: stocks.macro[{i}] must be a mapping")
        rid, label, series = row.get("id"), row.get("label"), row.get("series")
        if not isinstance(rid, str) or not MACRO_ID_RE.match(rid) or rid in ids:
            raise ConfigError(f"{name}: stocks.macro[{i}].id must be a unique short id such as dgs10")
        ids.add(rid)
        if not isinstance(label, str) or not label.strip():
            raise ConfigError(f"{name}: stocks.macro.{rid}.label is missing")
        if isinstance(series, str):
            series = [series]
        if not isinstance(series, list) or not series or \
                not all(isinstance(s, str) and SERIES_ID_RE.match(s) for s in series):
            raise ConfigError(f"{name}: stocks.macro.{rid}.series must list FRED series ids such as DGS10")
        if row.get("transform") not in STOCKS_TRANSFORMS:
            raise ConfigError(f"{name}: stocks.macro.{rid}.transform must be one of {', '.join(STOCKS_TRANSFORMS)}")
        if row["transform"] == "range" and len(series) != 2:
            raise ConfigError(f"{name}: stocks.macro.{rid} with transform range needs two series, lower and upper")
        clean = dict(row)
        clean.update({"id": rid, "label": " ".join(label.split()), "series": list(series),
                      "unit": str(row.get("unit") or "")})
        out.append(clean)
    return out


def _stocks_fx(value, name):
    out = {}
    for cur, entry in _mapping(value, "stocks.fx", name).items():
        if not isinstance(cur, str) or not CURRENCY_RE.match(cur):
            raise ConfigError(f"{name}: stocks.fx must be keyed by currency codes such as EUR, not {cur!r}")
        entry = _mapping(entry, f"stocks.fx.{cur}", name)
        if not isinstance(entry.get("series"), str) or not SERIES_ID_RE.match(entry["series"]):
            raise ConfigError(f"{name}: stocks.fx.{cur}.series must be a FRED series id such as DEXUSEU")
        if entry.get("quote") not in STOCKS_QUOTES:
            raise ConfigError(f"{name}: stocks.fx.{cur}.quote must be {' or '.join(STOCKS_QUOTES)}")
        out[cur] = {"series": entry["series"], "quote": entry["quote"]}
    return out


def _stocks(value, name):
    """The stocks section: the model, its limits and prices, the learner's
    settings, the funds, the macro table and the FX series. Never a ticker."""
    if _has_key(value, "tickers"):
        raise ConfigError(f"{name}: {TICKERS_PRIVATE}")
    out = _mapping(value, "stocks", name)
    defaults = DEFAULTS["stocks"]
    for key, default in defaults.items():
        if out.get(key) is None:
            out[key] = copy.deepcopy(default)

    out["models"] = _stocks_models(out["models"], name, defaults["models"])
    if not isinstance(out["model"], str) or out["model"].strip() not in out["models"]:
        raise ConfigError(f"{name}: stocks.model must be one of {', '.join(out['models'])}")
    out["model"] = out["model"].strip()
    if out["effort"] not in EFFORTS:
        raise ConfigError(f"{name}: stocks.effort must be one of {', '.join(EFFORTS)}")
    for key in STOCKS_INTS:
        out[key] = _stocks_int(out[key], key, name)
    out["max_run_usd"] = _price(out["max_run_usd"], "stocks.max_run_usd", name)
    out["fallback_prices"] = _stocks_prices(out["fallback_prices"], name)

    horizons = out["horizons"]
    if not isinstance(horizons, list) or not horizons or len(set(map(repr, horizons))) != len(horizons) or \
            not all(isinstance(h, int) and not isinstance(h, bool) and 1 <= h <= 120 for h in horizons):
        raise ConfigError(f"{name}: stocks.horizons must be a list of different whole numbers from 1 to 120")
    out["funds"] = _stocks_funds(out["funds"], name)
    out["macro"] = _stocks_macro(out["macro"], name)
    out["fx"] = _stocks_fx(out["fx"], name)

    days = _mapping(out["trading_days"], "stocks.trading_days", name)
    ready = days.get("data_ready")
    if ready in (None, ""):
        days["data_ready"] = defaults["trading_days"]["data_ready"]
    else:
        try:
            days["data_ready"] = _hhmm(ready, "data_ready", name)
        except ConfigError:
            raise ConfigError(f'{name}: stocks.trading_days.data_ready must be a time such as "18:00"') from None
    out["trading_days"] = days
    return out
