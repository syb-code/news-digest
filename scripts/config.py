"""Read config.yml, the one settings file of the news digest.

    from config import load_config
    cfg = load_config()          # config.yml at the repository root
    cfg["feeds"]["rss"]          # always present, a list
    cfg["learn"]["effort"]       # always present, checked
    cfg["learn"]["prices"]       # {model id: {"input": 4.0, "output": 20.0}}, dollars per million tokens

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

SECTIONS = ("timezone", "schedule", "feeds", "themes", "learn")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
FEED_LISTS = ("youtube", "rss", "websites", "twitter_embeds", "nitter_instances")
LEARN_INTS = ("max_output_tokens", "max_input_chars", "topics_min", "topics_max", "lookback_hours")
HHMM_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
FALLBACKS_OFF = ("none", "off", "false", "")
PRICE_KEYS = ("input", "output")

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
    checkers = {"timezone": _timezone, "schedule": _schedule, "feeds": _feeds, "themes": _themes, "learn": _learn}
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
