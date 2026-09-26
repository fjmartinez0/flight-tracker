#!/usr/bin/env python3
"""
merge_flight_data.py — standalone regeneration tool for the YUL/YYZ flight pricing
dashboard (PricingAnalysis.html / PricingAnalysisTrends.html).

This is NOT the scraper. It has no knowledge of how flights_scraper_*.csv files get
produced (that lives in yul_yyz_scraper.py, which was never shared in the chat this
was built in — every CLI flag mentioned in this project's docs is inferred from CSV
filenames and past usage, not verified against real source).

What this script actually does, every time you run it:
  1. Reads the dataset already embedded in PricingAnalysis.html.
  2. Merges in new CSVs (or wipes everything and starts fresh, with --reset).
  3. Flags any run_timestamp collision between old and new data (should never happen
     — if it does, you're probably re-ingesting a batch you already merged).
  4. Re-embeds the merged dataset into both HTML files, in place.
  5. Runs the same price analysis the dashboard's JS does (latest-run price vs.
     all-time low, per tracked week) and prints a status report.

Usage:
    python merge_flight_data.py --html PricingAnalysis.html --trends PricingAnalysisTrends.html --csv new1.csv new2.csv ...
    python merge_flight_data.py --html PricingAnalysis.html --trends PricingAnalysisTrends.html --csv new1.csv new2.csv --reset

If --trends is omitted, only the main dashboard file is updated.
"""

import argparse
import csv
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

DATASET_RE = re.compile(r'let dataset = (\[.*?\]); //', re.S)
DATASET_SUB_RE = re.compile(r'let dataset = \[.*?\]; //[^\n]*', re.S)

TRANSPORT_COST_BY_AIRPORT = {'YYZ': 12, 'YTZ': 0, 'YUL': 0, 'YHU': 0}  # YHU cost is a PLACEHOLDER, not confirmed


def transport_for(airport):
    return TRANSPORT_COST_BY_AIRPORT.get(airport, 0)


# --- Anchor-date exceptions ------------------------------------------------
# Tracked weeks normally depart Monday. Holiday-shifted exceptions go here as two
# parallel sets, mirroring the JS in PricingAnalysis.html exactly (§6 of
# PROJECT_CONTEXT.md). Currently empty — the Oct 12/13 exception from this
# project's October-tracking phase was removed when October was dropped from
# scope entirely (2026-09-13). If a new exception ever comes up, add it here AND
# in the HTML's ANCHOR_SUPPRESSED / ANCHOR_ADDED, or the two will disagree about
# which dates are "tracked."
ANCHOR_SUPPRESSED = set()
ANCHOR_ADDED = {'2026-11-05', '2026-11-12', '2026-12-17', '2027-01-07', '2027-01-14'}  # ad hoc Thursday comparisons, promoted to tracked anchors 2026-09-13


def is_tracked_anchor(date_str):
    if date_str in ANCHOR_SUPPRESSED:
        return False
    weekday = datetime.strptime(date_str, '%Y-%m-%d').weekday()  # Mon=0
    return weekday == 0 or date_str in ANCHOR_ADDED


# Mirrors the exact rolling-window logic embedded in run_daily.sh's scraper
# invocation — kept here too so the gap-check (below) validates against what
# SHOULD have been scraped today, not a hardcoded list that silently goes stale
# (this replaced a hardcoded `expected` list that had exactly this problem —
# it was missing dates within two weeks of being written, see PROJECT_CONTEXT.md).
# If the rolling-window logic ever changes in run_daily.sh, mirror the change here.
ROLLING_WINDOW_FLOOR = datetime(2026, 11, 2).date()  # never generate dates before this


def add_months(d, months):
    import calendar
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def rolling_window_dates(today=None, months_ahead=5):
    today = today or datetime.now().date()
    days_ahead = (7 - today.weekday()) % 7  # Monday=0; 0 if today IS Monday
    next_monday = today + timedelta(days=days_ahead)
    start = max(next_monday, ROLLING_WINDOW_FLOOR)
    end = add_months(today, months_ahead)
    dates = []
    d = start
    while d <= end:
        dates.append(d.isoformat())
        d += timedelta(days=7)
    return dates


def iso_week_key(date_str):
    d = datetime.strptime(date_str, '%Y-%m-%d').date()
    iso_year, iso_week, _ = d.isocalendar()
    return f'{iso_year}-W{iso_week}'


# --- CSV / dataset I/O -------------------------------------------------------

def load_csv_rows(path):
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


def extract_dataset(html_text):
    m = DATASET_RE.search(html_text)
    if not m:
        raise ValueError('Could not find embedded dataset in HTML — has the file structure changed?')
    return json.loads(m.group(1))


def embed_dataset(html_text, dataset, note='embedded data from scraper run(s)'):
    data_json = json.dumps(dataset)
    new_html, n = DATASET_SUB_RE.subn(f'let dataset = {data_json}; // {note}', html_text, count=1)
    if n != 1:
        raise ValueError('Failed to re-embed dataset — regex did not match exactly once.')
    return new_html


LAST_UPDATED_SUB_RE = re.compile(r'let lastUpdated = [^;]*;[^\n]*')


def stamp_last_updated(html_text, timestamp_iso):
    new_html, n = LAST_UPDATED_SUB_RE.subn(
        f"let lastUpdated = '{timestamp_iso}'; // stamped by merge_flight_data.py",
        html_text, count=1
    )
    if n != 1:
        raise ValueError('Failed to stamp lastUpdated — regex did not match exactly once. '
                          'Has the HTML lost its `let lastUpdated = ...;` line?')
    return new_html


# --- Price analysis (mirrors analyze() in the HTML's <script>, exactly) -----

def analyze(rows, nights):
    sep_out = [r for r in rows if r['booking_type'] == 'separate_outbound']
    sep_ret = [r for r in rows if r['booking_type'] == 'separate_return']
    # KNOWN ISSUE (scraper-side): round-trip queries with a mismatched return airport
    # appear to silently mirror the outbound airport instead of erroring. Only trust
    # matched-airport round-trip rows.
    rt = [r for r in rows if r['booking_type'] == 'roundtrip' and r['outbound_to'] == r['return_from']]

    def latest_min(rows_list, key_fn):
        latest_ts = {}
        for r in rows_list:
            k = key_fn(r)
            if k not in latest_ts or r['run_timestamp'] > latest_ts[k]:
                latest_ts[k] = r['run_timestamp']
        out = {}
        for r in rows_list:
            k = key_fn(r)
            if r['run_timestamp'] != latest_ts[k]:
                continue
            price = float(r['price'])
            if k not in out or price < out[k][0]:
                out[k] = (price, r)
        return out

    def all_time_min(rows_list, key_fn):
        out = {}
        for r in rows_list:
            k = key_fn(r)
            price = float(r['price'])
            if k not in out or price < out[k][0]:
                out[k] = (price, r)
        return out

    sep_out_key = lambda r: (r['outbound_date'], r['outbound_from'], r['outbound_to'])
    sep_ret_key = lambda r: (r['return_date'], r['return_from'], r['return_to'])
    rt_key = lambda r: (r['outbound_date'], r['return_date'], r['outbound_from'], r['outbound_to'], r['return_from'], r['return_to'])

    min_sep_out = latest_min(sep_out, sep_out_key)
    min_sep_ret = latest_min(sep_ret, sep_ret_key)
    min_rt = latest_min(rt, rt_key)

    all_sep_out = all_time_min(sep_out, sep_out_key)
    all_sep_ret = all_time_min(sep_ret, sep_ret_key)
    all_rt = all_time_min(rt, rt_key)

    weeks = sorted({r['outbound_date'] for r in sep_out + rt if r['outbound_date']})

    # Airport sets are derived from whatever's actually in the data, not hardcoded —
    # mirrors the JS exactly, so adding YHU (or any new airport) to the scraper's
    # output makes it show up here automatically, no code change needed.
    mtl_set = set()
    for r in sep_out + rt:
        if r.get('outbound_from'):
            mtl_set.add(r['outbound_from'])
    for r in sep_ret + rt:
        if r.get('return_to'):
            mtl_set.add(r['return_to'])
    mtl_airports = sorted(mtl_set) if mtl_set else ['YUL']

    to_set = set()
    for r in sep_out + rt:
        if r.get('outbound_to'):
            to_set.add(r['outbound_to'])
    for r in sep_ret + rt:
        if r.get('return_from'):
            to_set.add(r['return_from'])
    to_airports = sorted(to_set) if to_set else ['YYZ', 'YTZ']

    combos = []
    for outbound_date in weeks:
        d = datetime.strptime(outbound_date, '%Y-%m-%d').date()
        return_date = (d + timedelta(days=nights)).isoformat()
        iso_year, iso_week, _ = d.isocalendar()

        for out_mtl in mtl_airports:
          for out_airport in to_airports:
            for in_airport in to_airports:
              for in_mtl in mtl_airports:
                sep_out_e = min_sep_out.get((outbound_date, out_mtl, out_airport))
                sep_ret_e = min_sep_ret.get((return_date, in_airport, in_mtl))
                rt_e = min_rt.get((outbound_date, return_date, out_mtl, out_airport, in_airport, in_mtl))

                commute = transport_for(out_mtl) + transport_for(out_airport) + transport_for(in_airport) + transport_for(in_mtl)

                sep_flight = sep_out_e[0] + sep_ret_e[0] if (sep_out_e and sep_ret_e) else None
                rt_flight = rt_e[0] if rt_e else None
                if sep_flight is None and rt_flight is None:
                    continue

                if sep_flight is not None and (rt_flight is None or sep_flight <= rt_flight):
                    booking_type, flight_cost = 'separate', sep_flight
                else:
                    booking_type, flight_cost = 'roundtrip', rt_flight
                total = flight_cost + commute

                all_sep_e = all_sep_out.get((outbound_date, out_mtl, out_airport))
                all_ret_e = all_sep_ret.get((return_date, in_airport, in_mtl))
                all_rt_e = all_rt.get((outbound_date, return_date, out_mtl, out_airport, in_airport, in_mtl))
                all_sep_flight = all_sep_e[0] + all_ret_e[0] if (all_sep_e and all_ret_e) else None
                all_rt_flight = all_rt_e[0] if all_rt_e else None

                all_time_total, all_time_type, all_time_seen = None, None, None
                if all_sep_flight is not None or all_rt_flight is not None:
                    if all_sep_flight is not None and (all_rt_flight is None or all_sep_flight <= all_rt_flight):
                        all_time_type = 'separate'
                        all_time_total = all_sep_flight + commute
                        d1, d2 = all_sep_e[1]['run_timestamp'], all_ret_e[1]['run_timestamp']
                        all_time_seen = max(d1, d2)[:10]
                    else:
                        all_time_type = 'roundtrip'
                        all_time_total = all_rt_flight + commute
                        all_time_seen = all_rt_e[1]['run_timestamp'][:10]

                combos.append({
                    'outbound_date': outbound_date, 'return_date': return_date,
                    'out_mtl': out_mtl, 'out_airport': out_airport,
                    'in_airport': in_airport, 'in_mtl': in_mtl,
                    'iso_week': f'{iso_year}-W{iso_week}',
                    'booking_type': booking_type, 'total': total,
                    'all_time_min_total': all_time_total,
                    'all_time_min_type': all_time_type,
                    'all_time_min_seen': all_time_seen,
                })
    return combos


def best_price_report(dataset):
    """Prints the same per-week best-price table generated after every merge."""
    for nights in (1, 2):
        print(f'\n--- {nights} Night(s) ---')
        combos = analyze(dataset, nights)
        tracked = [c for c in combos if is_tracked_anchor(c['outbound_date'])]
        by_week = defaultdict(list)
        for c in tracked:
            by_week[c['outbound_date']].append(c)
        for outbound_date in sorted(by_week):
            group = by_week[outbound_date]
            best = min(group, key=lambda c: c['total'])
            iso = best['iso_week']
            line = f"  {iso} ({outbound_date}): ${best['total']:.0f} ({best['booking_type']})"
            if best['all_time_min_total'] is not None and best['all_time_min_total'] < best['total']:
                line += f"  [all-time low ${best['all_time_min_total']:.0f}, seen {best['all_time_min_seen']}]"
            print(line)


def check_anomalies(dataset):
    """Heuristic: flag when 3+ unrelated tracked weeks show the identical current
    total in the same run — the fare-bucket-lockstep signature seen multiple times
    in this project's history (Aug 14, Aug 20-22, Sep 5). Checks both booking
    methods separately, since a lockstep move has happened on the separate-booking
    side too (Sep 5) and a round-trip-only check would miss it."""
    combos = analyze(dataset, 2)
    tracked = [c for c in combos if is_tracked_anchor(c['outbound_date'])]
    for booking_type in ('roundtrip', 'separate'):
        subset = [c for c in tracked if c['booking_type'] == booking_type]
        by_price = defaultdict(set)
        for c in subset:
            by_price[round(c['total'])].add(c['outbound_date'])
        flagged = {p: dates for p, dates in by_price.items() if len(dates) >= 3}
        if flagged:
            print(f'\n⚠ Possible fare-bucket-wide price ({booking_type}, identical across 3+ unrelated weeks):')
            for price, dates in flagged.items():
                print(f'  ${price}: {sorted(dates)}')


def load_snapshot(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}  # corrupt/missing snapshot shouldn't crash the whole merge


def save_snapshot(path, snapshot):
    with open(path, 'w') as f:
        json.dump(snapshot, f)


def current_prices(dataset):
    """Best (latest-run) total per tracked anchor date, for both stay lengths.
    Keyed 'YYYY-MM-DD|N' -> price. This is what gets diffed against the previous
    run's snapshot to detect real drops."""
    snapshot = {}
    for nights in (1, 2):
        combos = analyze(dataset, nights)
        tracked = [c for c in combos if is_tracked_anchor(c['outbound_date'])]
        by_date = defaultdict(list)
        for c in tracked:
            by_date[c['outbound_date']].append(c)
        for outbound_date, group in by_date.items():
            best = min(c['total'] for c in group)
            snapshot[f'{outbound_date}|{nights}'] = best
    return snapshot


def detect_drops(old_snapshot, new_snapshot, pct_threshold, abs_threshold):
    """A drop counts if it clears EITHER threshold (whichever is more lenient for
    that price point) — a $50 drop on a $200 fare is huge (25%) and should always
    fire even if pct_threshold were set higher; a 15% drop on a $2000 fare is $300,
    real money, and should fire even if abs_threshold were set higher. Requiring
    both would miss real drops at either end of the price range."""
    drops = []
    for key, new_price in new_snapshot.items():
        old_price = old_snapshot.get(key)
        if old_price is None or old_price <= 0:
            continue  # no prior data point to compare against yet
        if new_price >= old_price:
            continue
        drop_abs = old_price - new_price
        drop_pct = (drop_abs / old_price) * 100
        if drop_pct >= pct_threshold or drop_abs >= abs_threshold:
            date_str, nights = key.split('|')
            drops.append({
                'date': date_str, 'nights': int(nights),
                'old': old_price, 'new': new_price,
                'drop_abs': drop_abs, 'drop_pct': drop_pct,
            })
    drops.sort(key=lambda d: -d['drop_pct'])
    return drops


def send_ntfy(topic, drops):
    lines = []
    for d in drops:
        lines.append(f"{d['date']} ({d['nights']}N): ${d['old']:.0f} -> ${d['new']:.0f} "
                     f"(-${d['drop_abs']:.0f}, -{d['drop_pct']:.0f}%)")
    body = '\n'.join(lines)
    title = f'{len(drops)} flight price drop(s) detected'
    req = urllib.request.Request(
        f'https://ntfy.sh/{topic}',
        data=body.encode('utf-8'),
        headers={'Title': title, 'Priority': 'high', 'Tags': 'airplane,moneybag'},
        method='POST',
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f'Notification sent to ntfy.sh/{topic}')
    except Exception as e:
        print(f'[warn] Failed to send notification: {e}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--html', required=True, help='Path to PricingAnalysis.html (updated in place)')
    ap.add_argument('--trends', help='Path to PricingAnalysisTrends.html (optional, updated in place)')
    ap.add_argument('--csv', nargs='+', required=True, help='New CSV file(s) to merge in')
    ap.add_argument('--reset', action='store_true',
                     help='Discard all existing embedded data and start fresh from only the given CSVs')
    ap.add_argument('--ntfy-topic', help='ntfy.sh topic to notify on a significant price drop (omit to skip notifications entirely)')
    ap.add_argument('--alert-pct', type=float, default=15.0, help='Alert if a tracked date drops by at least this %% (default 15)')
    ap.add_argument('--alert-abs', type=float, default=50.0, help='Alert if a tracked date drops by at least this $ amount (default 50)')
    ap.add_argument('--snapshot', default='.price_snapshot.json', help='Path to the price-snapshot file used to detect drops (default: .price_snapshot.json in cwd)')
    args = ap.parse_args()

    html_text = open(args.html).read()
    existing = [] if args.reset else extract_dataset(html_text)
    before = len(existing)
    existing_ts = {r['run_timestamp'] for r in existing}

    new_rows = []
    new_ts = set()
    for path in args.csv:
        rows = load_csv_rows(path)
        new_rows.extend(rows)
        new_ts.update(r['run_timestamp'] for r in rows)

    collisions = existing_ts & new_ts
    merged = existing + new_rows

    print(f'{"RESET" if args.reset else "MERGE"}: {before} -> {len(merged)} rows '
          f'({"discarded old, " if args.reset else ""}+{len(new_rows)} from {len(args.csv)} file(s))')
    print(f'Timestamp collisions: {sorted(collisions) if collisions else "none"}')
    if collisions:
        print('  ^ This usually means you are re-ingesting a batch already merged. Check before trusting this output.')

    generated_at = datetime.now().astimezone().isoformat(timespec='seconds')

    new_html = embed_dataset(html_text, merged)
    new_html = stamp_last_updated(new_html, generated_at)
    with open(args.html, 'w') as f:
        f.write(new_html)
    print(f'Wrote {args.html} (last updated: {generated_at})')

    if args.trends:
        trends_text = open(args.trends).read()
        new_trends = embed_dataset(trends_text, merged)
        new_trends = stamp_last_updated(new_trends, generated_at)
        with open(args.trends, 'w') as f:
            f.write(new_trends)
        print(f'Wrote {args.trends} (last updated: {generated_at})')

    best_price_report(merged)
    check_anomalies(merged)

    # Gap check against what SHOULD have been scraped today: the rolling 5-month
    # window (same logic as run_daily.sh — see rolling_window_dates() above) plus
    # the 5 fixed Thursday ad hoc anchors (§6/ANCHOR_ADDED), which aren't part of
    # the rolling window since they're one-off comparison dates, not a recurring
    # weekly cadence. This replaced a hardcoded list that went stale within two
    # weeks of being written (see PROJECT_CONTEXT.md) — computing it fresh each
    # run means it can't go stale the same way again.
    expected = rolling_window_dates() + sorted(ANCHOR_ADDED)
    present = {r['outbound_date'] for r in merged if r.get('outbound_date')}
    missing = [d for d in expected if d not in present]
    if missing:
        print(f'\nMissing expected tracked dates: {missing}')

    # Price-drop detection: compare this run's best prices against the previous
    # run's snapshot (not all-time history — specifically the run immediately
    # before this one), so this catches real intraday drops whether running once
    # or multiple times a day. Runs regardless of --ntfy-topic so drops are always
    # visible in the log; only the push notification itself is conditional.
    old_snapshot = load_snapshot(args.snapshot)
    new_snapshot = current_prices(merged)
    drops = detect_drops(old_snapshot, new_snapshot, args.alert_pct, args.alert_abs)
    save_snapshot(args.snapshot, new_snapshot)

    if drops:
        print(f'\n💰 {len(drops)} price drop(s) meeting threshold (>={args.alert_pct}% or >=${args.alert_abs}):')
        for d in drops:
            print(f"  {d['date']} ({d['nights']}N): ${d['old']:.0f} -> ${d['new']:.0f} "
                  f"(-${d['drop_abs']:.0f}, -{d['drop_pct']:.0f}%)")
        if args.ntfy_topic:
            send_ntfy(args.ntfy_topic, drops)
        else:
            print('  (no --ntfy-topic given — drop detected but no notification sent)')
    elif not old_snapshot:
        print('\n[info] No previous price snapshot found — this is the first run with '
              'alerting enabled, nothing to compare against yet. Next run will have a baseline.')


if __name__ == '__main__':
    main()
