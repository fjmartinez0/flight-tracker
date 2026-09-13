#!/bin/bash
# run_daily.sh — daily flight-price scrape + merge + publish.
# Runs both the regular weekly-Monday tracking and the ad hoc Thursday
# comparisons, merges everything into the dashboard, and pushes to GitHub Pages.

set -e  # stop on first real error, don't silently continue on a broken step

PROJECT_DIR="/Users/fernandomartinez/Documents/python_projects/flight-tracker"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
LOG_FILE="$PROJECT_DIR/logs/run_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$PROJECT_DIR/logs"
cd "$PROJECT_DIR"

{
  echo "=== Run started: $(date) ==="

  echo "--- Regular weekly tracking (Nov 2026 - Jan 2027 Mondays) ---"
  "$PYTHON" yul_yyz_scraper.py \
    --dates 2026-11-02,2026-11-09,2026-11-16,2026-11-23,2026-11-30,2026-12-07,2026-12-14,2026-12-21,2026-12-28,2027-01-04,2027-01-11,2027-01-18,2027-01-25 \
    --nights 1,2

  echo "--- Ad hoc Thursday comparisons ---"
  "$PYTHON" yul_yyz_scraper.py \
    --scan-dates 2026-11-05,2026-11-12,2026-12-17,2027-01-07,2027-01-14 \
    --nights 1

  echo "--- Merging today's CSVs into the dashboard ---"
  TODAY=$(date +%Y%m%d)
  NEW_CSVS=$(ls flights_scraper_*_"$TODAY".csv 2>/dev/null || true)

  if [ -z "$NEW_CSVS" ]; then
    echo "[warn] No CSVs found matching today's date ($TODAY) — scraper may have failed silently. Check above output."
  else
    "$PYTHON" merge_flight_data.py \
      --html PricingAnalysis.html \
      --trends PricingAnalysisTrends.html \
      --csv $NEW_CSVS

    echo "--- Publishing to GitHub Pages ---"
    git add PricingAnalysis.html PricingAnalysisTrends.html flights_scraper_*.csv
    git commit -m "Daily update: $(date +%Y-%m-%d)" || echo "[info] nothing to commit"
    git push
  fi

  echo "=== Run finished: $(date) ==="
} >> "$LOG_FILE" 2>&1
