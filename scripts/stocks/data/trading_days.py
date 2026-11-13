#!/usr/bin/env python3
"""NYSE trading days, computed in code (contract section 5.9).

    python -I scripts/stocks/data/trading_days.py gate --reason scheduled|manual
    python -I scripts/stocks/data/trading_days.py holidays --year 2027

The gate prints exactly "run=true" or "run=false" to stdout (for
$GITHUB_OUTPUT) and one plain line to stderr. A manual run always goes ahead.
"Today" is the date in America/New_York.

Holidays follow the NYSE rules: New Year's Day, Martin Luther King Jr. Day,
Washington's Birthday, Good Friday, Memorial Day, Juneteenth (from 2022),
Independence Day, Labor Day, Thanksgiving and Christmas. A holiday on a
Saturday closes the Friday before, one on a Sunday the Monday after, except
that a Saturday New Year's Day closes nothing (December 31 is a trading day).
Early closes are trading days. Special closures, such as national days of
mourning, are listed in SPECIAL_CLOSURES below; add new ones there.

This module imports only the standard library, because the gate job runs it
with the runner's own python3 and installs nothing.
"""
import os
import sys

if __name__ == "__main__":
    # Run as a script: this folder holds calendar.py, which must not shadow the
    # standard library module that zoneinfo imports.
    _HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != _HERE]

import argparse  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402

NY_TZ = "America/New_York"

# Unscheduled full-day closures. Public facts; add new ones here.
SPECIAL_CLOSURES = {date(2025, 1, 9): "National day of mourning"}

_HOLIDAY_CACHE = {}


def easter(year):
    """Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year, month, weekday, n):
    """The n-th given weekday (Monday is 0) of a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d):
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year):
    """{date: name} of the regular NYSE holidays that close the market in year."""
    year = int(year)
    cached = _HOLIDAY_CACHE.get(year)
    if cached is not None:
        return dict(cached)
    h = {}
    new_year = date(year, 1, 1)
    if new_year.weekday() == 6:
        h[date(year, 1, 2)] = "New Year's Day"
    elif new_year.weekday() < 5:
        h[new_year] = "New Year's Day"
    h[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    h[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    h[easter(year) - timedelta(days=2)] = "Good Friday"
    h[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        h[_observed(date(year, 6, 19))] = "Juneteenth"
    h[_observed(date(year, 7, 4))] = "Independence Day"
    h[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    h[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving"
    h[_observed(date(year, 12, 25))] = "Christmas"
    _HOLIDAY_CACHE[year] = dict(h)
    return h


def closure_name(d):
    """Why the NYSE is closed on d: a holiday or special closure name,
    "Saturday", "Sunday", or None on a trading day."""
    if d in SPECIAL_CLOSURES:
        return SPECIAL_CLOSURES[d]
    name = nyse_holidays(d.year).get(d)
    if name:
        return name
    if d.weekday() == 5:
        return "Saturday"
    if d.weekday() == 6:
        return "Sunday"
    return None


def is_trading_day(d, extra_closures=()):
    if d.weekday() >= 5 or d in extra_closures:
        return False
    return closure_name(d) is None


def previous_trading_day(d, extra_closures=()):
    """The last trading day strictly before d."""
    d = d - timedelta(days=1)
    while not is_trading_day(d, extra_closures):
        d -= timedelta(days=1)
    return d


def next_trading_day(d, extra_closures=()):
    """The first trading day strictly after d."""
    d = d + timedelta(days=1)
    while not is_trading_day(d, extra_closures):
        d += timedelta(days=1)
    return d


def add_trading_days(d, n, extra_closures=()):
    """d moved n trading days forward (n < 0: back). n = 0 returns d."""
    n = int(n)
    step = next_trading_day if n > 0 else previous_trading_day
    for _ in range(abs(n)):
        d = step(d, extra_closures)
    return d


def trading_days_between(a, b, extra_closures=()):
    """The number of trading days in (a, b]; negative when b is before a."""
    if b < a:
        return -trading_days_between(b, a, extra_closures)
    count = 0
    d = a + timedelta(days=1)
    while d <= b:
        if is_trading_day(d, extra_closures):
            count += 1
        d += timedelta(days=1)
    return count


# ---------------------------------------------------------------- New York time

def _us_eastern_offset(utc_dt):
    """UTC offset of New York at utc_dt, from the US rules since 2007 (used
    only when the system has no time zone database)."""
    year = utc_dt.year
    dst_start = datetime.combine(_nth_weekday(year, 3, 6, 2), datetime.min.time(), timezone.utc) + timedelta(hours=7)
    dst_end = datetime.combine(_nth_weekday(year, 11, 6, 1), datetime.min.time(), timezone.utc) + timedelta(hours=6)
    return timedelta(hours=-4) if dst_start <= utc_dt < dst_end else timedelta(hours=-5)


def ny_time(now):
    """now (an aware datetime; naive is taken as UTC) in New York time."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo(NY_TZ))
    except Exception:  # no tz database: compute the offset from the US rules
        utc = now.astimezone(timezone.utc)
        return utc.astimezone(timezone(_us_eastern_offset(utc)))


def _parse_hhmm(value):
    hh, mm = str(value).split(":")
    hh, mm = int(hh), int(mm)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("data_ready is not an HH:MM time")
    return hh, mm


def last_closed_day(now, data_ready="18:00", extra_closures=()):
    """The latest trading day whose close is published: today (New York) when it
    is a trading day and the New York time is at or after data_ready, else the
    trading day before today."""
    local = ny_time(now)
    today = local.date()
    hh, mm = _parse_hhmm(data_ready)
    if is_trading_day(today, extra_closures) and (local.hour, local.minute) >= (hh, mm):
        return today
    return previous_trading_day(today, extra_closures)


def gate(today, reason, extra_closures=()):
    """(run, message) for the stock run: a manual run always goes ahead; a
    scheduled run only on NYSE trading days."""
    if reason == "manual":
        return True, f"Manual run on {today.isoformat()}: going ahead."
    if reason != "scheduled":
        raise ValueError("reason must be scheduled or manual")
    day = today.isoformat()
    if is_trading_day(today, extra_closures):
        return True, f"{day} is an NYSE trading day. Going ahead."
    if today.weekday() >= 5:
        return False, f"{day} is a {closure_name(today)}. Nothing to do."
    if today in nyse_holidays(today.year):
        return False, f"{day} is an NYSE holiday ({nyse_holidays(today.year)[today]}). Nothing to do."
    name = SPECIAL_CLOSURES.get(today) or "special closure"
    return False, f"The NYSE is closed on {day} ({name}). Nothing to do."


def main(argv=None, now=None):
    parser = argparse.ArgumentParser(description="NYSE trading days.")
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("gate", help="Print run=true on an NYSE trading day (always for a manual run).")
    g.add_argument("--reason", choices=("scheduled", "manual"), required=True)
    g.add_argument("--date", help="Check this YYYY-MM-DD date instead of today in New York (for tests).")
    h = sub.add_parser("holidays", help="List the NYSE holidays of a year.")
    h.add_argument("--year", type=int, required=True)
    args = parser.parse_args(argv)

    if args.command == "holidays":
        for d, name in sorted(nyse_holidays(args.year).items()):
            print(f"{d.isoformat()} {name}")
        return 0

    if args.date:
        try:
            today = date.fromisoformat(args.date)
        except ValueError:
            parser.error("--date must be YYYY-MM-DD")
    else:
        today = ny_time(now or datetime.now(timezone.utc)).date()
    run, message = gate(today, args.reason)
    print("run=true" if run else "run=false", flush=True)
    print(message, file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
