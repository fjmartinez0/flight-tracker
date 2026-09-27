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

# ntfy topic lives in a SEPARATE file, not hardcoded here — this file gets
# regenerated every time the script changes, which kept silently wiping out the
# real topic back to a placeholder. .ntfy_topic is a one-time, one-line file
# that never gets touched by future deliveries of this script.
NTFY_TOPIC_FILE="$PROJECT_DIR/.ntfy_topic"
if [ -f "$NTFY_TOPIC_FILE" ]; then
  NTFY_TOPIC=$(cat "$NTFY_TOPIC_FILE" | tr -d '[:space:]')
else
  NTFY_TOPIC=""
fi

# Sends a push notification via ntfy.sh if a real topic is configured; silently
# does nothing if .ntfy_topic doesn't exist or is empty. Used for the
# unconditional "run finished" ping below, separate from merge_flight_data.py's
# own high-priority price-drop alerts — this one is low-priority so it doesn't
# feel as urgent as an actual price drop, but still confirms the pipeline is alive.
notify() {
  local message="$1"
  local title="$2"
  if [ -n "$NTFY_TOPIC" ]; then
    curl -s -d "$message" -H "Title: $title" -H "Priority: low" "ntfy.sh/$NTFY_TOPIC" > /dev/null 2>&1
  fi
}

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
end = add_months(start, 5)
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

  echo "--- Ad hoc Thursday comparisons (3rd Thursday of each month, rolling 5-6 months) ---"
  ROLLING_THURSDAYS=$("$PYTHON" -c "
import calendar
from datetime import date, timedelta

def add_months(d, months):
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)

def first_weekday_on_or_after(d, target_weekday):
    days_ahead = (target_weekday - d.weekday()) % 7
    return d + timedelta(days=days_ahead)

def third_thursday_of_month(year, month):
    first_of_month = date(year, month, 1)
    first_thu = first_weekday_on_or_after(first_of_month, 3)  # Thu=3
    return first_thu + timedelta(days=14)

today = date.today()
FLOOR = date(2026, 11, 2)
floor_month_start = date(FLOOR.year, FLOOR.month, 1)
today_month_start = date(today.year, today.month, 1)
candidate_month = max(today_month_start, floor_month_start)
dates = []
for i in range(6):  # current/floor month plus 5 more, matching the Monday window's span
    m = add_months(candidate_month, i)
    thu3 = third_thursday_of_month(m.year, m.month)
    if thu3 >= max(today, FLOOR):
        dates.append(thu3.isoformat())
print(','.join(dates))
")
  echo "Rolling 3rd-Thursday window: $ROLLING_THURSDAYS"
  "$PYTHON" -u yul_yyz_scraper.py \
    --scan-dates "$ROLLING_THURSDAYS" \
    --nights 1

  echo "--- Merging today's CSVs into the dashboard ---"
  TODAY=$(date +%Y%m%d)
  NEW_CSVS=$(ls flights_scraper_*_"$TODAY".csv 2>/dev/null || true)

  if [ -z "$NEW_CSVS" ]; then
    echo "[warn] No CSVs found matching today's date ($TODAY) — scraper may have failed silently. Check above output."
    notify "No new data produced today ($TODAY) — check the log, the scraper may have failed." "⚠️ Flight Tracker — Run Failed"
  else
    NTFY_ARGS=""
    if [ -n "$NTFY_TOPIC" ]; then
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
    if git push; then
      notify "Dashboard updated and live: $(date +'%b %d, %I:%M %p')." "✅ Flight Tracker — Data Ready"
    else
      notify "git push FAILED — data was merged locally but the live dashboard was NOT updated. Check logs." "🚨 Flight Tracker — Push Failed"
    fi
  fi

  echo "=== Run finished: $(date) ==="
} >> "$LOG_FILE" 2>&1
