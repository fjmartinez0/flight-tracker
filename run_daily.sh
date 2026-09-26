#!/bin/bash
# run_daily.sh — daily flight-price scrape + merge + publish.
# Runs both the regular weekly-Monday tracking and the ad hoc Thursday
# comparisons, merges everything into the dashboard, and pushes to GitHub Pages.
# Raw CSVs are deleted after merging — their data lives on inside the HTML's
# embedded dataset, and they were never wanted in the git repo.

set -e  # stop on first real error, don't silently continue on a broken step

PROJECT_DIR="/Users/fernandomartinez/flight-tracker"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
LOG_FILE="$PROJECT_DIR/logs/run_$(date +%Y%m%d_%H%M%S).log"

# EDIT THIS: your ntfy.sh topic name (see SETUP_GUIDE.txt for how to get one).
# Leave as-is (unset/placeholder) to disable price-drop notifications entirely —
# drops still get logged either way, just not pushed to your phone.
NTFY_TOPIC="REPLACE_WITH_YOUR_NTFY_TOPIC"

mkdir -p "$PROJECT_DIR/logs"
cd "$PROJECT_DIR"

{
  echo "=== Run started: $(date) ==="

  echo "--- Regular weekly tracking (rolling 5-month window from today) ---"
  ROLLING_DATES=$("$PYTHON" -c "
import calendar
from datetime import date, timedelta

def add_months(d, months):
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)

today = date.today()
days_ahead = (7 - today.weekday()) % 7  # Monday=0; 0 if today IS Monday
next_monday = today + timedelta(days=days_ahead)
# Floor: never generate dates before Nov 2, 2026 — preserves the explicit
# 'already booked, exclude October' decision. Becomes a permanent no-op once
# real time passes this date naturally; safe to leave in place indefinitely.
FLOOR = date(2026, 11, 2)
start = max(next_monday, FLOOR)
end = add_months(today, 5)
dates = []
d = start
while d <= end:
    dates.append(d.isoformat())
    d += timedelta(days=7)
print(','.join(dates))
")
  echo "Rolling window: $ROLLING_DATES"
  "$PYTHON" -u yul_yyz_scraper.py \
    --dates "$ROLLING_DATES" \
    --nights 1,2

  echo "--- Ad hoc Thursday comparisons ---"
  "$PYTHON" -u yul_yyz_scraper.py \
    --scan-dates 2026-11-05,2026-11-12,2026-12-17,2027-01-07,2027-01-14 \
    --nights 1

  echo "--- Merging today's CSVs into the dashboard ---"
  TODAY=$(date +%Y%m%d)
  NEW_CSVS=$(ls flights_scraper_*_"$TODAY".csv 2>/dev/null || true)

  if [ -z "$NEW_CSVS" ]; then
    echo "[warn] No CSVs found matching today's date ($TODAY) — scraper may have failed silently. Check above output."
  else
    NTFY_ARGS=""
    if [ "$NTFY_TOPIC" != "REPLACE_WITH_YOUR_NTFY_TOPIC" ] && [ -n "$NTFY_TOPIC" ]; then
      NTFY_ARGS="--ntfy-topic $NTFY_TOPIC"
    fi

    "$PYTHON" merge_flight_data.py \
      --html PricingAnalysis.html \
      --trends PricingAnalysisTrends.html \
      --csv $NEW_CSVS \
      --alert-pct 15 \
      --alert-abs 50 \
      $NTFY_ARGS

    echo "--- Deleting today's CSVs (data is already merged into the HTML, not needed after this) ---"
    rm -f $NEW_CSVS

    echo "--- Publishing to GitHub Pages ---"
    git add PricingAnalysis.html PricingAnalysisTrends.html
    git commit -m "Daily update: $(date +%Y-%m-%d)" || echo "[info] nothing to commit"
    git push
  fi

  echo "=== Run finished: $(date) ==="
} >> "$LOG_FILE" 2>&1
