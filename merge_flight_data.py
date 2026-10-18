#!/usr/bin/env python3
"""
merge_flight_data.py — standalone regeneration tool for the YUL/YYZ flight pricing
dashboard (PricingAnalysis.html). The separate Trends page was retired 2026-10-17.

This is NOT the scraper (that is yul_yyz_scraper.py).

What this script does, every time you run it:
  1. Reads the dataset embedded in PricingAnalysis.html (packed or legacy format).
  2. Merges in new CSVs (or wipes everything and starts fresh, with --reset).
  3. Flags any run_timestamp collision between old and new data.
  4. Compacts the data (keeps only the cheapest row per run/route/date — the only
     row the dashboard ever uses) and prunes trips that departed > 6 months ago.
  5. Re-embeds the dataset (column-packed), stamps last-updated / booked weeks.
  6. Prints the best-price report, anomaly check, gap check, buy signals.
  7. Price-drop alerts and buy-signal alerts via ntfy.sh.

Usage:
    python merge_flight_data.py --html PricingAnalysis.html --csv new1.csv new2.csv ...
    python merge_flight_data.py --html PricingAnalysis.html --csv new1.csv --reset
    python merge_flight_data.py --html PricingAnalysis.html --refresh
(--trends is still accepted for backward compatibility and ignored.)
"""

import argparse
import csv
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta

# Current format: one line `let datasetPacked = {"cols":[...],"rows":[[...],...]}; // packed data...`
PACKED_RE = re.compile(r'^let datasetPacked = (.*); // packed data, stamped by merge_flight_data\.py$', re.M)
PACKED_MARK = '// packed data, stamped by merge_flight_data.py'
# Legacy format (before 2026-10-17): `let dataset = [ {...}, ... ]; // ...` — read-only support.
LEGACY_RE = re.compile(r'let dataset = (\[.*?\]); //', re.S)

PRUNE_DEPARTED_DAYS = 183   # drop rows for trips that departed more than ~6 months ago

TRANSPORT_COST_BY_AIRPORT = {'YYZ': 12, 'YTZ': 0, 'YUL': 0, 'YHU': 0}  # YHU cost is a PLACEHOLDER, not confirmed


def transport_for(airport):
    return TRANSPORT_COST_BY_AIRPORT.get(airport, 0)


# --- Anchor-date exceptions ------------------------------------------------
# Tracked weeks normally depart Monday. The 3rd Thursday of each month is ALSO
# tracked (1-night ad hoc comparisons) — scaled back 2026-09-27 from a full
# weekly Thursday cadence to once/month, on the assumption prices move roughly
# uniformly across a month, so daily/weekly Thursday sampling was overkill.
# Uses a day-of-month range rather than computing "the 3rd Thursday" directly:
# the Nth occurrence of any weekday always falls within a fixed day-of-month
# window regardless of the month (3rd: days 15-21), so "Thursday AND day 15-21"
# is exactly "the 3rd Thursday" — mirrors the HTML's JS exactly, keep in sync.
# ANCHOR_SUPPRESSED/ANCHOR_ADDED remain available for one-off holiday exceptions
# — currently empty since the Oct 12/13 exception was removed when October left
# scope.
ANCHOR_SUPPRESSED = set()
ANCHOR_ADDED = set()


def is_tracked_anchor(date_str):
    if date_str in ANCHOR_SUPPRESSED:
        return False
    d = datetime.strptime(date_str, '%Y-%m-%d').date()
    is_third_thursday = d.weekday() == 3 and 15 <= d.day <= 21  # Thu=3
    return d.weekday() == 0 or is_third_thursday or date_str in ANCHOR_ADDED


# Rolling windows now come from tracking_window.py (shared with run_daily.sh), so
# what gets scraped, alerted on, gap-checked and shown can never drift apart.
import tracking_window as tw


def rolling_window_dates():
    return [d.isoformat() for d in tw.mondays()]


def rolling_thursday_dates():
    return [d.isoformat() for d in tw.thursdays()]


def iso_week_key(date_str):
    d = datetime.strptime(date_str, '%Y-%m-%d').date()
    iso_year, iso_week, _ = d.isocalendar()
    return f'{iso_year}-W{iso_week}'


# --- CSV / dataset I/O -------------------------------------------------------

def load_csv_rows(path):
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


def unpack(packed):
    cols = packed['cols']
    return [dict(zip(cols, v)) for v in packed['rows']]


def pack(dataset):
    cols = []
    for r in dataset:                      # union of keys, first-seen order
        for k in r:
            if k not in cols:
                cols.append(k)
    return {'cols': cols, 'rows': [[r.get(k, '') for k in cols] for r in dataset]}


def extract_dataset(html_text):
    m = PACKED_RE.search(html_text)
    if m:
        return unpack(json.loads(m.group(1)))
    m = LEGACY_RE.search(html_text)
    if m:
        return json.loads(m.group(1))
    raise ValueError('Could not find embedded dataset in HTML — has the file structure changed?')


def embed_dataset(html_text, dataset):
    data_json = json.dumps(pack(dataset), separators=(',', ':'))
    if '; //' in data_json or '\n' in data_json:
        raise ValueError('Packed data contains a forbidden sequence; refusing to embed.')
    new_html, n = PACKED_RE.subn(lambda m: f'let datasetPacked = {data_json}; {PACKED_MARK}', html_text, count=1)
    if n != 1:
        raise ValueError('Failed to re-embed dataset — `let datasetPacked = ...` line not found exactly once. '
                         'Is this the new dashboard template? (run upgrade_dashboard.py)')
    return new_html


# --- Compaction ------------------------------------------------------------

def _row_key(r):
    bt = r['booking_type']
    if bt == 'separate_outbound':
        k = (r['outbound_date'], r['outbound_from'], r['outbound_to'])
    elif bt == 'separate_return':
        k = (r['return_date'], r['return_from'], r['return_to'])
    else:
        k = (r['outbound_date'], r['return_date'], r['outbound_from'], r['outbound_to'], r['return_from'], r['return_to'])
    return (r['run_timestamp'], bt) + k


def compact(rows, today=None):
    """Keep only the cheapest row per (run, booking type, route/date key) — first one on
    ties, original order preserved. analyze() only ever uses that row, so every number
    the dashboard shows is unchanged (verified on real data 2026-10-17). Also drops rows
    for trips that departed more than PRUNE_DEPARTED_DAYS ago."""
    today = today or date.today()
    cutoff = (today - timedelta(days=PRUNE_DEPARTED_DAYS)).isoformat()
    best = {}
    for i, r in enumerate(rows):
        travel = r.get('outbound_date') or r.get('return_date') or ''
        if travel and travel < cutoff:
            continue
        k = _row_key(r)
        p = float(r['price'])
        if k not in best or p < best[k][0]:
            best[k] = (p, i)
    return [rows[i] for i in sorted(i for _, i in best.values())]


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


BOOKED_SUB_RE = re.compile(r'let bookedWeeks = [^;]*;[^\n]*')
START_SUB_RE = re.compile(r'let trackingStart = [^;]*;[^\n]*')


def stamp_tracking(html_text, booked, start):
    """Embed booked weeks + tracking start so the static page can hide booked weeks
    and start the calendar at the right month (the page cannot read Mac files)."""
    arr = json.dumps(sorted(m.isoformat() for m in booked))
    html_text, n1 = BOOKED_SUB_RE.subn(lambda m: f"let bookedWeeks = {arr}; // stamped by merge_flight_data.py", html_text, count=1)
    html_text, n2 = START_SUB_RE.subn(lambda m: f"let trackingStart = '{start.isoformat()}'; // stamped by merge_flight_data.py", html_text, count=1)
    if n1 != 1 or n2 != 1:
        raise ValueError('Failed to stamp bookedWeeks/trackingStart — is the HTML missing those lines?')
    return html_text


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


# --- Buy signal (mirrors buildSeries()/typicalAt()/signalAt() in the HTML's JS) ---
# Series = one point per scrape DAY per tracked date: best total from analyze() run on
# that day's rows only. Built from ALL data (booked weeks too — they still count for the
# typical price). Labels: buy / fair / spike / pricey / new. KEEP IN SYNC WITH THE JS.
SIGNAL = {'windowDays': 14, 'buyPct': 0.03, 'fairPct': 0.10, 'minObs': 3, 'typicalDays': 30, 'priceyPct': 0.10}
SIGNAL_LABELS = {'buy': 'Buy now', 'fair': 'Fair', 'spike': 'Spiked', 'pricey': 'Above typical', 'new': 'Too new'}


def _days(a, b):
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def _median(v):
    s = sorted(v)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def build_series(dataset, nights):
    by_day = defaultdict(list)
    for r in dataset:
        d = (r.get('run_timestamp') or '')[:10]
        if d:
            by_day[d].append(r)
    series = defaultdict(list)
    for day in sorted(by_day):
        best = {}
        for c in analyze(by_day[day], nights):
            if not is_tracked_anchor(c['outbound_date']):
                continue
            k = c['outbound_date']
            if k not in best or c['total'] < best[k]['total']:
                best[k] = c
        for k, c in best.items():
            series[k].append({'day': day, 'total': c['total'], 'method': c['booking_type'],
                              'airports': c['out_airport'] + '/' + c['in_airport']})
    return dict(series)


class SignalModel:
    def __init__(self, dataset):
        self.series = {n: build_series(dataset, n) for n in (1, 2)}
        self._typ = {}

    def typical_at(self, n, day):
        k = (n, day)
        if k not in self._typ:
            v = [o['total'] for obs in self.series[n].values() for o in obs
                 if 0 <= _days(day, o['day']) < SIGNAL['typicalDays']]
            self._typ[k] = _median(v) if v else None
        return self._typ[k]

    def signal_at(self, obs, i, n):
        p, d = obs[i]['total'], obs[i]['day']
        win = [o for o in obs[:i + 1] if _days(d, o['day']) <= SIGNAL['windowDays']]
        low = min(o['total'] for o in win)
        typ = self.typical_at(n, d)
        if len(win) < SIGNAL['minObs']:
            return 'new', low, typ
        if typ is not None and p > typ * (1 + SIGNAL['priceyPct']):
            label = 'pricey'
        elif p <= low * (1 + SIGNAL['buyPct']):
            label = 'buy'
        elif p <= low * (1 + SIGNAL['fairPct']):
            label = 'fair'
        else:
            label = 'spike'
        return label, low, typ

    def current(self, booked, start, end):
        """Latest label for every unbooked tracked date inside [start, end], both stay lengths."""
        out = []
        for n in (1, 2):
            for wk, obs in self.series[n].items():
                d = date.fromisoformat(wk)
                if d < start or d > end or tw.is_booked(d, booked):
                    continue
                label, low, typ = self.signal_at(obs, len(obs) - 1, n)
                out.append({'key': f'{wk}|{n}', 'date': wk, 'nights': n, 'label': label,
                            'price': obs[-1]['total'], 'low': low, 'typ': typ, 'day': obs[-1]['day']})
        return sorted(out, key=lambda x: (x['date'], x['nights']))


def signal_report(states):
    if not states:
        return
    print('\n--- Buy signals (unbooked weeks in the tracking window) ---')
    for s in states:
        typ = f"typical ${s['typ']:.0f}" if s['typ'] is not None else 'typical —'
        print(f"  {s['date']} {s['nights']}N: {SIGNAL_LABELS[s['label']]:<13} ${s['price']:.0f}  "
              f"(14d low ${s['low']:.0f}, {typ}, as of {s['day']})")


def buy_alerts(states, state_path, today_str):
    """Alert only on a TRANSITION into 'buy' for a key we have seen before. Keys never
    seen before are recorded silently (no alert storm on the first run or when a new
    week enters the window). Only points scraped today are evaluated, so a --test run
    (one date scraped) can't fire on stale data, and stale keys keep their old state."""
    prev = load_snapshot(state_path)
    first_run = not prev
    new_state = dict(prev)
    fired = []
    for s in states:
        if s['day'] != today_str:
            continue
        k = s['key']
        if k in prev and prev[k] != 'buy' and s['label'] == 'buy':
            fired.append(s)
        new_state[k] = s['label']
    save_snapshot(state_path, new_state)
    if first_run:
        print(f'\n[info] Buy-signal state initialised silently ({len(new_state)} week/stay keys). '
              'Alerts start on the next run, only when a week newly becomes "Buy now".')
    return fired


def send_buy_ntfy(topic, fired):
    by_date = defaultdict(list)
    for s in fired:
        by_date[s['date']].append(s)
    lines = []
    for wk in sorted(by_date):
        d = datetime.strptime(wk, '%Y-%m-%d')
        parts = []
        for s in sorted(by_date[wk], key=lambda x: x['nights']):
            vs = f" ({round((s['price'] / s['typ'] - 1) * 100):+d}% vs typical)" if s['typ'] else ''
            parts.append(f"{s['nights']}N ${s['price']:.0f}{vs}")
        lines.append(f"{d:%a %b %d}: " + ' · '.join(parts))
    weeks = len(by_date)
    req = urllib.request.Request(
        f'https://ntfy.sh/{topic}',
        data='\n'.join(lines).encode('utf-8'),
        headers={'Title': f'Buy now: {weeks} week(s) at their recent low', 'Priority': 'high', 'Tags': 'green_circle,airplane'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f'[buy-alert] sent to ntfy.sh/{topic} -> HTTP {resp.status}')
    except Exception as e:
        print(f'[buy-alert] FAILED to send: {e}')


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
        booked = tw.load_booked()
        tracked = [c for c in combos if is_tracked_anchor(c['outbound_date'])
                   and not tw.is_booked(datetime.strptime(c['outbound_date'], '%Y-%m-%d').date(), booked)]
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
    ap.add_argument('--trends', help=argparse.SUPPRESS)  # retired 2026-10-17; accepted and ignored
    ap.add_argument('--csv', nargs='*', default=[], help='New CSV file(s) to merge in (omit with --refresh)')
    ap.add_argument('--refresh', action='store_true', help='Only re-stamp booked weeks / tracking start into the HTML (no CSVs, no alerts)')
    ap.add_argument('--reset', action='store_true',
                     help='Discard all existing embedded data and start fresh from only the given CSVs')
    ap.add_argument('--ntfy-topic', help='ntfy.sh topic for price-drop and buy-signal alerts (omit to skip notifications)')
    ap.add_argument('--alert-pct', type=float, default=15.0, help='Alert if a tracked date drops by at least this %% (default 15)')
    ap.add_argument('--alert-abs', type=float, default=50.0, help='Alert if a tracked date drops by at least this $ amount (default 50)')
    ap.add_argument('--snapshot', default='.price_snapshot.json', help='Price-snapshot file used to detect drops')
    ap.add_argument('--signal-state', default='.signal_state.json', help='Last buy-signal label per week (for buy alerts)')
    args = ap.parse_args()
    if not args.csv and not args.refresh:
        ap.error('give --csv FILE(s), or --refresh')
    if args.trends:
        print('[info] --trends is retired (Trends page removed) — ignoring it.')

    booked = tw.load_booked()
    start = tw.tracking_start(booked=booked)

    if args.refresh:
        t = open(args.html).read()
        t = stamp_tracking(t, booked, start)
        open(args.html, 'w').write(t)
        print(f'Refreshed {args.html}: {len(booked)} booked week(s), tracking starts {start}')
        return

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
    merged = compact(existing + new_rows)

    print(f'{"RESET" if args.reset else "MERGE"}: {before} -> {len(merged)} rows after compaction '
          f'({"discarded old, " if args.reset else ""}+{len(new_rows)} raw rows from {len(args.csv)} file(s))')
    print(f'Timestamp collisions: {sorted(collisions) if collisions else "none"}')
    if collisions:
        print('  ^ This usually means you are re-ingesting a batch already merged. Check before trusting this output.')

    generated_at = datetime.now().astimezone().isoformat(timespec='seconds')

    new_html = embed_dataset(html_text, merged)
    new_html = stamp_last_updated(new_html, generated_at)
    new_html = stamp_tracking(new_html, booked, start)
    with open(args.html, 'w') as f:
        f.write(new_html)
    print(f'Wrote {args.html} ({len(new_html) / 1e6:.1f} MB, last updated: {generated_at})')

    visible = [r for r in merged if not tw.is_booked(
        datetime.strptime(r.get('outbound_date') or r.get('return_date') or '1970-01-01', '%Y-%m-%d').date(), booked)]
    best_price_report(visible)
    check_anomalies(visible)

    # Gap check against what SHOULD have been scraped (tracking_window.py).
    expected = rolling_window_dates() + rolling_thursday_dates()
    present = {r['outbound_date'] for r in merged if r.get('outbound_date')}
    missing = [d for d in expected if d not in present]
    if missing:
        print(f'\nMissing expected tracked dates: {missing}')

    # Price-drop detection vs. the previous run's snapshot.
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

    # Buy signals (same rule as the dashboard) + alert on new "Buy now" weeks.
    try:
        model = SignalModel(merged)
        states = model.current(booked, start, tw.tracking_end(start))
        signal_report(states)
        fired = buy_alerts(states, args.signal_state, date.today().isoformat())
        if fired:
            print(f'\n🟢 {len(fired)} week/stay(s) newly at "Buy now":')
            for s in fired:
                print(f"  {s['date']} {s['nights']}N ${s['price']:.0f}")
            if args.ntfy_topic:
                send_buy_ntfy(args.ntfy_topic, fired)
            else:
                print('  (no --ntfy-topic given — no notification sent)')
    except Exception as e:  # never let the signal step break a run that already wrote the HTML
        print(f'[warn] buy-signal step failed: {e}')


if __name__ == '__main__':
    main()
