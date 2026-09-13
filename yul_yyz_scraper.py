#!/usr/bin/env python3
"""
YUL <-> Toronto flight data SCRAPER (collection only — no price analysis here).

Collects three kinds of raw data per week, so a separate analysis step can
compare "bought together" vs "bought separately":

  1. separate_outbound : YUL -> YYZ and YUL -> YTZ, Monday, departing after 17:00
  2. separate_return    : YYZ -> YUL and YTZ -> YUL, Wednesday, earliest flight only
  3. roundtrip          : all 4 airport-pair combinations (YYZ/YYZ, YYZ/YTZ,
                          YTZ/YYZ, YTZ/YTZ) for the same Monday/Wednesday, outbound
                          leg still filtered to depart after 17:00

  KNOWN LIBRARY LIMITATION: for round-trip queries, fast-flights only exposes
  outbound-leg detail (time, airline, duration) — the return leg's exact
  departure time isn't parsed out of Google's payload at all, only the total
  round-trip price. We still record the return *date* and total price (that's
  what the cost comparison needs), but return_departure/return_arrival will be
  blank for booking_type=roundtrip rows. This isn't something we can patch
  without reverse-engineering the library's own JS/protobuf parser further.

Only ever queries YUL (never YHU). Direct flights only (max_stops=0) to keep
leg-matching in round-trip results unambiguous.

This script does NOT compute totals, apply ground transport costs, or decide
what's "best" — it just appends raw observations to a CSV. Use the companion
analysis.html to do the actual comparison (open it and load the CSV(s) it produces).

--- On not getting blocked ---
There's no trick that guarantees this won't get flagged eventually — it's still
scraping a public page in a gray legal/ToS area (see the disclaimer fast-flights
itself carries). What's implemented below are the practices that genuinely reduce
the odds for a low-volume personal tool, because bot detection mostly keys off
*regularity* (metronomic timing, identical request order every run, bursty
back-to-back calls) rather than raw request count at this scale:
  - Randomized (jittered) delays between requests instead of a fixed interval
  - Randomized query order each run, so the request sequence isn't identical every time
  - Exponential backoff with jitter on failures, instead of hammering retries
  - A longer "cool-down" pause every few requests, mimicking human browsing pauses
  - A hard cap + warning if you point --weeks at a value that would fire a lot of
    requests in one run
None of this spoofs identity or bypasses a login/paywall — it's pacing, not evasion.
If Google starts serving CAPTCHAs or empty results consistently, that's a signal to
back off (increase delays, reduce --weeks, run less often) rather than to escalate.

Setup:
    pip install fast-flights --break-system-packages

Usage:
    python yul_yyz_scraper.py --weeks 6 --out prices.csv
    python yul_yyz_scraper.py --weeks 6 --out prices.csv --min-delay 4 --max-delay 10
    python yul_yyz_scraper.py --date 2026-09-28   # auto-named flights_scraper_Sep28_<today>.csv
    python yul_yyz_scraper.py --date 2026-09-28 --day-scan --weekdays-only   # compare Mon-Fri that week
    python yul_yyz_scraper.py --dates 2026-09-28,2026-10-26   # track 2 staggered weeks in one run,
                                                                # e.g. for booking-lead-time comparison
    python yul_yyz_scraper.py --dates 2026-09-28,2026-10-26 --nights 1,2   # track 1-night AND 2-night
                                                                # stays for the same weeks in one run —
                                                                # separate_outbound is only queried once
                                                                # per Monday, shared across both night
                                                                # lengths (see rows_separate_outbound
                                                                # call site in main())

Suggested cron (once daily — running much more often is what tends to draw attention):
    0 8 * * * /usr/bin/python3 /path/to/yul_yyz_scraper.py --out /path/to/prices.csv
"""

import argparse
import csv
import os
import random
import time
from datetime import datetime, timedelta

from fast_flights import FlightQuery, Passengers, create_query, get_flights
from fast_flights.exceptions import FlightsNotFound

MAX_RETRIES = 3
COOLDOWN_EVERY_N_REQUESTS = 8
COOLDOWN_SECONDS_RANGE = (25, 55)
_request_count = 0

OUTBOUND_MIN_HOUR = 17  # depart YUL after 17:00 on Mondays
RETURN_MIN_HOUR = 5     # "first flight" means first *morning* flight — excludes
                         # red-eyes that technically land just after midnight
                         # (00:30 is numerically "earlier" than 06:50 but isn't
                         # what "first flight of the day" means in practice)
TORONTO_AIRPORTS = ["YYZ", "YTZ"]  # kept separate deliberately
HOME_AIRPORT = "YUL"  # never YHU

CSV_FIELDS = [
    "run_timestamp",
    "booking_type",  # separate_outbound | separate_return | roundtrip
    "outbound_date", "outbound_from", "outbound_to",
    "outbound_departure", "outbound_arrival", "outbound_duration_min",
    "return_date", "return_from", "return_to",
    "return_departure", "return_arrival", "return_duration_min",
    "price", "airlines",
]


def next_weekday(start: datetime, weekday: int) -> datetime:
    days_ahead = (weekday - start.weekday()) % 7
    days_ahead = days_ahead or 7
    return start + timedelta(days=days_ahead)


def upcoming_mondays(n_weeks: int):
    today = datetime.now()
    first_monday = next_weekday(today, 0)
    return [(first_monday + timedelta(weeks=i)).date() for i in range(n_weeks)]


def _safe_get(q, label=""):
    """Fetch with exponential backoff + jitter on failure. Tracks a running
    request count so the caller can insert cool-down pauses periodically."""
    global _request_count
    _request_count += 1

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return get_flights(q)
        except FlightsNotFound:
            print(f"    [no flights] {label}: Google returned zero results for this exact query")
            return []
        except Exception as e:
            if attempt == MAX_RETRIES:
                print(f"  [warn] query failed after {MAX_RETRIES} attempts: {e}")
                return []
            backoff = (2 ** attempt) + random.uniform(0, 2)
            print(f"  [retry {attempt}/{MAX_RETRIES}] {e} -- backing off {backoff:.1f}s")
            time.sleep(backoff)
    return []


def jittered_sleep(min_s, max_s):
    """Sleep a randomized duration instead of a fixed interval, and throw in
    an occasional longer cool-down to break up steady, bot-like timing."""
    global _request_count
    if _request_count and _request_count % COOLDOWN_EVERY_N_REQUESTS == 0:
        cooldown = random.uniform(*COOLDOWN_SECONDS_RANGE)
        print(f"  [cooldown] pausing {cooldown:.0f}s after {_request_count} requests")
        time.sleep(cooldown)
    else:
        time.sleep(random.uniform(min_s, max_s))


def query_one_way(date, from_airport, to_airport):
    q = create_query(
        flights=[FlightQuery(date=date.isoformat(), from_airport=from_airport, to_airport=to_airport)],
        trip="one-way",
        seat="economy",
        passengers=Passengers(adults=1),
        max_stops=0,
    )
    return _safe_get(q, label=f"{from_airport}->{to_airport} {date}")


def query_round_trip(out_date, back_date, out_to, back_from):
    q = create_query(
        flights=[
            FlightQuery(date=out_date.isoformat(), from_airport=HOME_AIRPORT, to_airport=out_to),
            FlightQuery(date=back_date.isoformat(), from_airport=back_from, to_airport=HOME_AIRPORT),
        ],
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=1),
        max_stops=0,
    )
    return _safe_get(q, label=f"RT {HOME_AIRPORT}->{out_to} {out_date} / {back_from}->{HOME_AIRPORT} {back_date}")


def normalize_time(t):
    """Google's payload is an inconsistent sparse array: minutes can be a
    missing trailing element (18,) instead of (18, 0), or an explicit None
    (None, 30) / (18, None) / (None, None). Normalize all of these to a
    clean (hour, minute) pair with 0 for anything missing or None."""
    if not t:
        return (0, 0)
    parts = list(t) + [0, 0]
    h = parts[0] if parts[0] is not None else 0
    m = parts[1] if parts[1] is not None else 0
    return (h, m)


def dep_hour(leg):
    return normalize_time(leg.departure.time)[0]


def dep_minutes(leg):
    h, m = normalize_time(leg.departure.time)
    return h * 60 + m


def fmt_time(t):
    h, mi = normalize_time(t)
    return f"{h:02d}:{mi:02d}"


def rows_separate_outbound(monday_date):
    rows = []
    airports = TORONTO_AIRPORTS.copy()
    random.shuffle(airports)  # vary request order run to run
    for to_airport in airports:
        for f in query_one_way(monday_date, HOME_AIRPORT, to_airport):
            leg = f.flights[0]
            if dep_hour(leg) < OUTBOUND_MIN_HOUR:
                continue
            rows.append({
                "booking_type": "separate_outbound",
                "outbound_date": monday_date.isoformat(),
                "outbound_from": HOME_AIRPORT, "outbound_to": to_airport,
                "outbound_departure": fmt_time(leg.departure.time),
                "outbound_arrival": fmt_time(leg.arrival.time),
                "outbound_duration_min": leg.duration,
                "return_date": "", "return_from": "", "return_to": "",
                "return_departure": "", "return_arrival": "", "return_duration_min": "",
                "price": f.price, "airlines": "|".join(f.airlines),
            })
    return rows


def rows_separate_return(return_date):
    rows = []
    airports = TORONTO_AIRPORTS.copy()
    random.shuffle(airports)
    for from_airport in airports:
        results = query_one_way(return_date, from_airport, HOME_AIRPORT)
        candidates = [f for f in results if dep_hour(f.flights[0]) >= RETURN_MIN_HOUR]
        if not candidates:
            print(f"    [warn] no {from_airport}->YUL flights at/after {RETURN_MIN_HOUR}:00 on {return_date} "
                  f"({len(results)} total results, all before cutoff)")
            continue
        earliest = min(candidates, key=lambda f: dep_minutes(f.flights[0]))
        leg = earliest.flights[0]
        rows.append({
            "booking_type": "separate_return",
            "outbound_date": "", "outbound_from": "", "outbound_to": "",
            "outbound_departure": "", "outbound_arrival": "", "outbound_duration_min": "",
            "return_date": return_date.isoformat(),
            "return_from": from_airport, "return_to": HOME_AIRPORT,
            "return_departure": fmt_time(leg.departure.time),
            "return_arrival": fmt_time(leg.arrival.time),
            "return_duration_min": leg.duration,
            "price": earliest.price, "airlines": "|".join(earliest.airlines),
        })
    return rows


def rows_roundtrip(monday_date, return_date):
    rows = []
    combos = [(o, b) for o in TORONTO_AIRPORTS for b in TORONTO_AIRPORTS]
    random.shuffle(combos)  # vary request order run to run
    for out_to, back_from in combos:
            results = query_round_trip(monday_date, return_date, out_to, back_from)
            raw_count = len(results)
            too_early = sum(1 for f in results if len(f.flights) >= 1 and dep_hour(f.flights[0]) < OUTBOUND_MIN_HOUR)
            matches = [f for f in results if len(f.flights) >= 1 and dep_hour(f.flights[0]) >= OUTBOUND_MIN_HOUR]
            print(f"    YUL->{out_to} / {back_from}->YUL: {raw_count} raw results, "
                  f"{too_early} before {OUTBOUND_MIN_HOUR}:00, {len(matches)} kept")
            matches.sort(key=lambda f: f.price)
            for f in matches[:5]:  # keep a few cheapest options per combo, not just one
                out_leg = f.flights[0]
                in_leg = f.flights[1] if len(f.flights) >= 2 else None  # return-leg detail usually unavailable
                rows.append({
                    "booking_type": "roundtrip",
                    "outbound_date": monday_date.isoformat(),
                    "outbound_from": HOME_AIRPORT, "outbound_to": out_to,
                    "outbound_departure": fmt_time(out_leg.departure.time),
                    "outbound_arrival": fmt_time(out_leg.arrival.time),
                    "outbound_duration_min": out_leg.duration,
                    "return_date": return_date.isoformat(),
                    "return_from": back_from, "return_to": HOME_AIRPORT,
                    "return_departure": fmt_time(in_leg.departure.time) if in_leg else "",
                    "return_arrival": fmt_time(in_leg.arrival.time) if in_leg else "",
                    "return_duration_min": in_leg.duration if in_leg else "",
                    "price": f.price, "airlines": "|".join(f.airlines),
                })
    return rows


def default_filename(label):
    """Auto-name output files so daily cron runs naturally produce one file
    per day without needing to remember to change --out manually. Running
    the script twice on the same day appends into that same day's file
    (append_to_csv already handles this), so "one file per day" holds even
    with multiple runs."""
    today_str = datetime.now().strftime("%Y%m%d")
    return f"flights_scraper_{label}_{today_str}.csv"


def append_to_csv(path, rows):
    file_exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        for r in rows:
            writer.writerow(r)


def parse_nights_arg(raw):
    """Parses '1', '2', or '1,2' into a sorted list of unique positive ints."""
    try:
        vals = sorted(set(int(x.strip()) for x in raw.split(",") if x.strip()))
    except ValueError:
        raise argparse.ArgumentTypeError(f"--nights must be a comma-separated list of integers, got {raw!r}")
    if not vals or any(v < 1 for v in vals):
        raise argparse.ArgumentTypeError("--nights values must be positive integers")
    return vals


def main():
    global RETURN_MIN_HOUR
    parser = argparse.ArgumentParser(description="YUL <-> Toronto data scraper (collection only)")
    parser.add_argument("--weeks", type=int, default=6, help="How many upcoming Mondays (ignored if --date, --dates, or --day-scan is set)")
    parser.add_argument("--date", type=str, default=None,
                         help="Target a specific Monday (YYYY-MM-DD) instead of 'next N weeks'. "
                              "In --day-scan mode, this is the anchor start date instead (any weekday).")
    parser.add_argument("--dates", type=str, default=None,
                         help="Comma-separated list of Mondays (YYYY-MM-DD,YYYY-MM-DD,...) to track "
                              "in one run — e.g. for comparing booking lead time across staggered weeks. "
                              "Each date gets its own auto-named output file unless --out is given, "
                              "in which case all of them are appended into that one shared file. "
                              "Every date must be a Monday — use --scan-dates for arbitrary weekdays.")
    parser.add_argument("--scan-dates", type=str, default=None,
                         help="Comma-separated list of specific outbound dates (YYYY-MM-DD,...) to "
                              "scrape ad hoc — any weekday, not just Monday. Unlike --day-scan, this "
                              "hits only the exact dates you list, with no 7-day fan-out. Each date is "
                              "paired with date + --nights as the return date(s), same request pattern "
                              "as --dates otherwise (separate_outbound shared across --nights values). "
                              "Cannot be combined with --dates or --day-scan.")
    parser.add_argument("--day-scan", action="store_true",
                         help="Scan all 7 days of the week starting at --date as candidate outbound "
                              "dates (each paired with outbound_date + --nights as the return date), "
                              "to compare which day of the week is cheapest to fly. Requires --date. "
                              "Only a single --nights value is supported in this mode.")
    parser.add_argument("--weekdays-only", action="store_true",
                         help="With --day-scan, skip Saturday/Sunday outbound dates.")
    parser.add_argument("--nights", type=parse_nights_arg, default=[2],
                         help="Length(s) of stay in nights, comma-separated (default 2, matching Mon->Wed). "
                              "Pass multiple values (e.g. --nights 1,2) to track several stay lengths for "
                              "the same Monday in one run — separate_outbound is only queried once per "
                              "Monday and shared across all requested night lengths, since that query "
                              "doesn't depend on stay length. separate_return and roundtrip are queried "
                              "once per (Monday, nights) pair, since those depend on the actual return date.")
    parser.add_argument("--out", default=None,
                         help="Output CSV path. If omitted, auto-named as flights_scraper_<label>_<YYYYMMDD>.csv "
                              "using today's date, so daily cron runs naturally produce one file per day.")
    parser.add_argument("--min-delay", type=float, default=3.0, help="Min seconds between requests (jittered)")
    parser.add_argument("--max-delay", type=float, default=7.0, help="Max seconds between requests (jittered)")
    parser.add_argument("--return-min-hour", type=int, default=RETURN_MIN_HOUR,
                         help="Earliest acceptable return departure hour (24h), to exclude red-eyes")
    args = parser.parse_args()
    RETURN_MIN_HOUR = args.return_min_hour

    def parse_monday(date_str, allow_day_scan_anchor=False):
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        if not allow_day_scan_anchor and d.weekday() != 0:
            print(f"[error] {d} is a {d.strftime('%A')}, not a Monday. "
                  f"Pass the Monday of the week you want, or use --day-scan to test other weekdays on purpose.")
            return None
        return d

    def parse_any_date(date_str):
        try:
            return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            print(f"[error] {date_str!r} isn't a valid YYYY-MM-DD date.")
            return None

    # Reject incompatible flag combinations up front rather than letting one
    # silently win (this is exactly what caused the earlier confusion where
    # --dates + --day-scan together quietly ignored --day-scan).
    exclusive_flags = [args.dates is not None, args.scan_dates is not None, args.day_scan]
    if sum(exclusive_flags) > 1:
        print("[error] --dates, --scan-dates, and --day-scan are mutually exclusive — pick one.")
        return

    jobs = []
    if args.dates:
        for date_str in args.dates.split(","):
            monday = parse_monday(date_str.strip())
            if monday is None:
                return
            jobs.append(([monday], monday.strftime("%b%d")))
        print(f"Tracking {len(jobs)} week(s) in this run, nights={args.nights}: {', '.join(j[1] for j in jobs)}\n")
    elif args.scan_dates:
        scan_days = []
        for date_str in args.scan_dates.split(","):
            d = parse_any_date(date_str)
            if d is None:
                return
            scan_days.append(d)
        jobs.append((scan_days, "scan_" + "_".join(d.strftime("%b%d") for d in scan_days)))
        weekday_names = ", ".join(f"{d} ({d.strftime('%A')})" for d in scan_days)
        print(f"Ad hoc scan of {len(scan_days)} specific date(s), nights={args.nights}: {weekday_names}\n")
    elif args.day_scan:
        if not args.date:
            print("[error] --day-scan requires --date (the anchor date to start the 7-day scan from).")
            return
        if len(args.nights) != 1:
            print("[error] --day-scan only supports a single --nights value.")
            return
        anchor = parse_monday(args.date, allow_day_scan_anchor=True)
        days = range(7)
        mondays = [anchor + timedelta(days=i) for i in days]
        if args.weekdays_only:
            mondays = [d for d in mondays if d.weekday() < 5]
        label = anchor.strftime("%b%d") + "_dowscan"
        jobs.append((mondays, label))
        scope = "weekdays" if args.weekdays_only else "all 7 days"
        print(f"Day-of-week scan ({scope}): {anchor} through {anchor + timedelta(days=6)}, "
              f"each paired with a {args.nights[0]}-night return.")
        print(f"Same time filters apply every day: depart YUL after {OUTBOUND_MIN_HOUR}:00, "
              f"return after {RETURN_MIN_HOUR}:00. Adjust with --return-min-hour if weekday/weekend "
              f"travel patterns should differ.\n")
        est_requests = len(mondays) * 8
        print(f"[info] ~{est_requests} requests for this scan. Consider running this scan on its own, "
              f"not stacked with a regular daily cron run.\n")
    elif args.date:
        monday = parse_monday(args.date)
        if monday is None:
            return
        jobs.append(([monday], monday.strftime("%b%d")))
        print(f"Targeting specific week: Monday {monday}, nights={args.nights}\n")
    else:
        mondays = upcoming_mondays(args.weeks)
        random.shuffle(mondays)
        est_requests = args.weeks * 8 * len(args.nights)
        if est_requests > 80:
            print(f"[warn] --weeks {args.weeks} with --nights {args.nights} means ~{est_requests} requests "
                  f"in one run — that's a lot of traffic in a single sitting. Consider a lower --weeks and "
                  f"running more often instead, so volume per run stays modest.\n")
        jobs.append((mondays, "rolling"))

    run_ts = datetime.now().isoformat(timespec="seconds")

    for mondays, label in jobs:
        out_path = args.out if args.out else default_filename(label)
        all_rows = []

        for monday in mondays:
            print(f"[{label}] Date: {monday} ({monday.strftime('%A')}), nights={args.nights}")

            print("  separate outbound (shared across all --nights values)...")
            all_rows.extend(rows_separate_outbound(monday))
            jittered_sleep(args.min_delay, args.max_delay)

            for n in args.nights:
                return_date = monday + timedelta(days=n)
                print(f"  [{n} night(s)] separate return ({return_date}, {return_date.strftime('%A')})...")
                all_rows.extend(rows_separate_return(return_date))
                jittered_sleep(args.min_delay, args.max_delay)

                print(f"  [{n} night(s)] roundtrip combos ({monday} -> {return_date})...")
                all_rows.extend(rows_roundtrip(monday, return_date))
                jittered_sleep(args.min_delay, args.max_delay)

        for r in all_rows:
            r["run_timestamp"] = run_ts

        if not all_rows:
            print(f"[{label}] No matching flights found this run.\n")
            continue

        append_to_csv(out_path, all_rows)
        print(f"[{label}] Saved {len(all_rows)} rows to {out_path}\n")

    print("Load the CSV(s) above into PricingAnalysis.html (comparison) or PricingAnalysisTrends.html (trend over time).")


if __name__ == "__main__":
    main()