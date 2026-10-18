#!/bin/bash
# run_daily.sh — scrape + merge + publish.   Usage:  ./run_daily.sh [--test]
#   (no flag)  full run: rolling 6-month window of Mondays (1 & 2 nights) + 3rd Thursdays
#   --test     SHORT run: scrape ONE date (1 night), then the same merge -> delete CSV ->
#              push -> notify chain, so you can confirm notifications in ~2 minutes.
# Which dates get scraped comes from tracking_window.py + booked_weeks.txt
# (see the `booked` command). Raw CSVs are deleted after merging.

set -e

PROJECT_DIR="/Users/fernandomartinez/flight-tracker"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"

TEST_MODE=0
[ "$1" = "--test" ] && TEST_MODE=1

if [ "$TEST_MODE" = 1 ]; then
  LOG_FILE="$PROJECT_DIR/logs/run_$(date +%Y%m%d_%H%M%S)_TEST.log"
  TAG="🧪 TEST — "
else
  LOG_FILE="$PROJECT_DIR/logs/run_$(date +%Y%m%d_%H%M%S).log"
  TAG=""
fi

# ntfy topic lives in .ntfy_topic (never overwritten by new deliveries of this script).
NTFY_TOPIC_FILE="$PROJECT_DIR/.ntfy_topic"
if [ -f "$NTFY_TOPIC_FILE" ]; then
  NTFY_TOPIC=$(cat "$NTFY_TOPIC_FILE" | tr -d '[:space:]')
else
  NTFY_TOPIC=""
fi

# Low-priority "run status" ping. Unlike the old version it does NOT hide errors:
# the HTTP status (or curl's error) is written to the log, so a failed delivery is visible.
notify() {
  local message="$1"
  local title="$2"
  if [ -z "$NTFY_TOPIC" ]; then
    echo "[notify] SKIPPED — no topic ($NTFY_TOPIC_FILE missing or empty)"
    return 0
  fi
  local code
  code=$(/usr/bin/curl -sS --max-time 20 -o /dev/null -w "%{http_code}" \
    -d "$message" -H "Title: $title" -H "Priority: low" "https://ntfy.sh/$NTFY_TOPIC" 2>&1) || true
  echo "[notify] topic=$NTFY_TOPIC title='$title' -> HTTP/curl result: $code"
}

mkdir -p "$PROJECT_DIR/logs"
cd "$PROJECT_DIR"

# Tell the user if the run dies mid-way (scraper crash etc.), instead of silence.
on_exit() {
  local rc=$?
  if [ $rc -ne 0 ]; then
    {
      echo "=== Run FAILED with exit code $rc: $(date) ==="
      notify "Run crashed (exit $rc) — see the log: $(basename "$LOG_FILE")" "🚨 ${TAG}Flight Tracker — Run Crashed"
    } >> "$LOG_FILE" 2>&1
  fi
  rmdir "$PROJECT_DIR/.publish.lock" 2>/dev/null || true
}
trap on_exit EXIT

{
  echo "=== Run started: $(date) ${TAG}==="
  "$PYTHON" tracking_window.py info
  START=$("$PYTHON" tracking_window.py start)

  ROLLING_DATES=$("$PYTHON" tracking_window.py mondays)
  ROLLING_THURSDAYS=$("$PYTHON" tracking_window.py thursdays)

  if [ "$TEST_MODE" = 1 ]; then
    FIRST_DATE=$(echo "$ROLLING_DATES" | cut -d, -f1)
    echo "--- TEST: scraping ONE date only: $FIRST_DATE (1 night) ---"
    "$PYTHON" -u yul_yyz_scraper.py --dates "$FIRST_DATE" --nights 1
  else
    echo "--- Regular weekly tracking (Mondays, booked weeks skipped) ---"
    echo "Dates: $ROLLING_DATES"
    "$PYTHON" -u yul_yyz_scraper.py --dates "$ROLLING_DATES" --nights 1,2

    echo "--- Ad hoc comparisons (3rd Thursday of each month) ---"
    echo "Dates: $ROLLING_THURSDAYS"
    if [ -n "$ROLLING_THURSDAYS" ]; then
      "$PYTHON" -u yul_yyz_scraper.py --scan-dates "$ROLLING_THURSDAYS" --nights 1
    fi
  fi

  echo "--- Merging today's CSVs into the dashboard ---"
  TODAY=$(date +%Y%m%d)
  NEW_CSVS=$(ls flights_scraper_*_"$TODAY".csv 2>/dev/null || true)

  if [ -z "$NEW_CSVS" ]; then
    echo "[warn] No CSVs found matching today's date ($TODAY) — scraper may have failed silently."
    notify "No new data produced today ($TODAY) — check the log." "⚠️ ${TAG}Flight Tracker — Run Failed"
  else
    NTFY_ARGS=""
    [ -n "$NTFY_TOPIC" ] && NTFY_ARGS="--ntfy-topic $NTFY_TOPIC"

    # Wait (up to ~2 min) if a `booked` command is publishing right now.
    for i in $(seq 1 24); do mkdir .publish.lock 2>/dev/null && break || sleep 5; done

    "$PYTHON" merge_flight_data.py \
      --html PricingAnalysis.html \
      --csv $NEW_CSVS \
      --alert-pct 15 \
      --alert-abs 50 \
      $NTFY_ARGS

    echo "--- Deleting today's CSVs (data is merged into the HTML) ---"
    rm -f $NEW_CSVS

    echo "--- Publishing to GitHub Pages ---"
    git add PricingAnalysis.html
    git commit -m "${TAG}Daily update: $(date +%Y-%m-%d)" || echo "[info] nothing to commit"
    if git push; then
      rmdir .publish.lock 2>/dev/null || true
      notify "Dashboard updated and live: $(date +'%b %d, %I:%M %p'). Tracking from $START." "✅ ${TAG}Flight Tracker — Data Ready"
    else
      rmdir .publish.lock 2>/dev/null || true
      notify "git push FAILED — data merged locally but the live dashboard was NOT updated." "🚨 ${TAG}Flight Tracker — Push Failed"
    fi
  fi

  echo "=== Run finished: $(date) ==="
} >> "$LOG_FILE" 2>&1
